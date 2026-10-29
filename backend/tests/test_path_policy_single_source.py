"""`14.8`：路径安全**只有一份判定**（`tools.resolve_path` 走 `PathPolicy`）。

## 之前是什么

`app/path_security.py::PathPolicy` 的检查**本身是真的**（有测试）：设备命名空间、
ADS（`file.txt:stream`）、保留名（`CON`/`NUL`）、符号链接跟随、deny 模式、云元数据。
**但生产路径不走它**——工具走 `tools.py` 里另一份 `resolve_path`，
那份只做「包含性」一条检查。

**两份实现必然漂移，而安全边界的漂移比没有更危险**：两边各改一半，
最后没人知道真实边界在哪。

## 判据

1. `resolve_path` **真的**问 `PathPolicy`（不是复制一份检查）；
2. 因此 `PathPolicy` 那几类检查**第一次真正保护工具调用**——
   设备命名空间与 ADS 路径现在会被拒，而它们以前能过；
3. `machine=True` 的既有语义不变（不设允许根 = 不做包含校验）。
"""

from __future__ import annotations

import pathlib

import pytest


@pytest.fixture()
def root(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


def test_ordinary_paths_still_work(root):
    """**既有行为不变**：工作目录内的普通路径照常解析。"""
    from app.tools import resolve_path

    target = root / "a" / "b.txt"
    assert resolve_path(root, "a/b.txt") == target.resolve()


def test_escaping_the_workspace_is_still_refused(root):
    """**既有行为不变**：`../` 逃逸仍然被拒（这是历史行为，不能松）。"""
    from app.tools import resolve_path

    with pytest.raises(ValueError):
        resolve_path(root, "../outside.txt")


def test_machine_mode_still_skips_containment(root):
    """**既有语义不变**：`machine=True` 不做包含校验（完全访问权限的意义就在这）。"""
    from app.tools import resolve_path

    outside = root.parent / "outside.txt"
    assert resolve_path(root, str(outside), machine=True) == outside.resolve()


def test_a_device_namespace_path_is_now_refused(root):
    """**边界真的变强了**：设备命名空间以前能过，现在被拒。

    `PathPolicy` 早就有这条检查（`DEVICE_PREFIXES`），但生产路径不走它——
    所以这条**不是把检查搬个家，是让它第一次生效**。
    """
    from app.tools import resolve_path

    with pytest.raises(ValueError) as excinfo:
        resolve_path(root, "\\\\?\\C:\\Windows\\System32\\config\\SAM")
    assert "device_path" in str(excinfo.value), (
        f"拒绝理由没带 `kind`——「为什么被拒」比「被拒了」有用：{excinfo.value}"
    )


def test_an_ads_stream_is_now_refused(root):
    """**边界真的变强了**：NTFS 备用数据流（`file.txt:hidden`）以前能过。

    它是最容易被忽略的一种绕过：文件内容看不出来，但流里能藏任意数据。
    """
    from app.tools import resolve_path

    with pytest.raises(ValueError) as excinfo:
        resolve_path(root, "note.txt:hidden")
    assert "ads" in str(excinfo.value).lower(), f"没按 ADS 拒：{excinfo.value}"


def test_resolve_path_really_asks_the_policy():
    """**防回归**：`resolve_path` 里必须出现 `PathPolicy`。

    14.8 的虚报形状是「两份实现并存」——只验「某些路径被拒」不够，
    要钉住**判定来自同一处**。
    """
    from app import tools as tools_module

    source = pathlib.Path(tools_module.__file__).read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
    assert "PathPolicy(" in code and "policy.check(" in code, (
        "`resolve_path` 又不问 `PathPolicy` 了——两份实现会重新漂移"
    )
