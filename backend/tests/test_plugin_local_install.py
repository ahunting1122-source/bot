"""``plugins install <zip>`` / ``plugins pack zip`` / ``plugins search`` / ``plugins update``
的契约测试（MATRIX 13.9 的本地部分）。

三条判据，一条都不能少：

1. **往返可用**：``pack zip`` 打出来的包必须能被 ``install`` 装回去——不然"打包"只是
   生成一个 zip 文件而已，和插件无关；
2. **zip slip 被拒**：构造 ``../evil.txt``、绝对路径、符号链接三种成员，断言解压被拒、
   目标目录外一个文件都没有；
3. **搜索命中本地**：已装插件与本地 hub 目录都要能搜到，远程索引如实报未实现。
"""

from __future__ import annotations

import stat
import zipfile
from pathlib import Path

import pytest

import app.cli.commands.session_extra3 as extra3  # noqa: F401 —— 导入即接线（含 plugins 4 条）
from app.cli.main import main

PLUGIN_CODE = '''
"""最小插件：只注册一个工具。"""


def register(ctx):
    ctx.register_tool("demo_echo", "回显", {"type": "object"}, lambda args: args)
'''


def _argv(tmp_path: Path, *rest: str) -> list[str]:
    return ["--data-dir", str(tmp_path / "data"), *rest]


def write_plugin(root: Path, name: str, manifest: str, code: str | None = PLUGIN_CODE) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.yaml").write_text(manifest, encoding="utf-8")
    if code is not None:
        (directory / "__init__.py").write_text(code, encoding="utf-8")
    return directory


def make_zip(path: Path, members: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as bundle:
        for name, body in members.items():
            bundle.writestr(name, body)
    return path


# ── 1. pack → install 往返 ──────────────────────────────────────────────────


def test_pack_zip_then_install_round_trip(tmp_path, capsys):
    """``pack zip`` 的产物必须能被 ``install <zip>`` 装回来（这是唯一的验收判据）。"""
    source = write_plugin(
        tmp_path / "src", "round-trip", "name: round-trip\nversion: 2.1.0\ndescription: 往返测试\n"
    )
    archive = tmp_path / "dist" / "round-trip-2.1.0.zip"

    assert main(_argv(tmp_path, "plugins", "pack", "zip", str(source), "--out", str(tmp_path / "dist"))) == 0
    assert archive.is_file(), "默认文件名必须是 <name>-<version>.zip"
    capsys.readouterr()

    assert main(_argv(tmp_path, "plugins", "install", str(archive))) == 0
    out = capsys.readouterr().out
    assert "已安装：round-trip" in out
    installed = tmp_path / "data" / "plugins" / "round-trip"
    assert (installed / "plugin.yaml").is_file()
    assert (installed / "__init__.py").is_file()
    # 装完就该被 list 看到（发现根包含 <data_dir>/plugins）
    assert main(_argv(tmp_path, "plugins", "list")) == 0
    assert "round-trip" in capsys.readouterr().out


def test_pack_zip_layout_has_a_single_top_level_directory(tmp_path):
    """包内布局是 ``<name>-<version>/…``：install 靠它认出插件目录。"""
    source = write_plugin(tmp_path / "src", "layout", "name: layout\nversion: 1.0.0\n")
    (source / "__pycache__").mkdir()
    (source / "__pycache__" / "junk.pyc").write_bytes(b"cache")
    (source / ".git").mkdir()
    (source / ".git" / "HEAD").write_text("ref: x", encoding="utf-8")
    archive = tmp_path / "layout-1.0.0.zip"

    assert main(_argv(tmp_path, "plugins", "pack", "zip", str(source), "--out", str(archive))) == 0
    with zipfile.ZipFile(archive) as bundle:
        names = bundle.namelist()

    assert names == sorted(names), "成员顺序要稳定（可复现的包）"
    assert all(name.startswith("layout-1.0.0/") for name in names), names
    assert "layout-1.0.0/plugin.yaml" in names
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in names), names
    assert not any(name.startswith("layout-1.0.0/.git/") for name in names), names


def test_cli_plugins_install_from_local_directory_still_works(tmp_path, capsys):
    """目录安装仍走原实现（本模块只是转发），行为与文案不变。"""
    source = write_plugin(tmp_path / "src", "dir-demo", "name: dir-demo\nversion: 1.0.0\n")
    assert main(_argv(tmp_path, "plugins", "install", str(source))) == 0
    assert "已安装：dir-demo" in capsys.readouterr().out


# ── 2. zip slip ────────────────────────────────────────────────────────────


def test_safe_extract_rejects_parent_traversal_member(tmp_path):
    """``../evil.txt`` 必须被拒，且目标目录外一个文件都不能出现。"""
    archive = make_zip(
        tmp_path / "evil.zip",
        {"demo/plugin.yaml": "name: demo\n", "../evil.txt": "pwned"},
    )
    destination = tmp_path / "unpack"

    with pytest.raises(extra3.ZipSlipError):
        extra3.safe_extract(archive, destination)

    assert not (tmp_path / "evil.txt").exists(), "越界文件绝不允许落盘"
    assert not destination.exists(), "第一遍校验失败时目标目录都不该被创建"


def test_safe_extract_rejects_absolute_member(tmp_path):
    archive = make_zip(tmp_path / "abs.zip", {"/abs/evil.txt": "pwned"})
    with pytest.raises(extra3.ZipSlipError):
        extra3.safe_extract(archive, tmp_path / "unpack")
    assert not (tmp_path / "unpack").exists()


def test_safe_extract_rejects_symlink_member(tmp_path):
    """zip 能存符号链接，解出来可以指向目录外——必须拦。"""
    archive = tmp_path / "link.zip"
    info = zipfile.ZipInfo("demo/link")
    info.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr(info, "/etc/passwd")

    with pytest.raises(extra3.ZipSlipError):
        extra3.safe_extract(archive, tmp_path / "unpack")
    assert not (tmp_path / "unpack").exists()


def test_cli_install_rejects_zip_slip_without_writing_anything(tmp_path, capsys):
    """CLI 层同样要拒（防线不能只在库里，用户走的是命令行）。"""
    archive = make_zip(
        tmp_path / "evil.zip",
        {"demo/plugin.yaml": "name: demo\n", "../escape.txt": "pwned"},
    )

    code = main(_argv(tmp_path, "plugins", "install", str(archive)))

    assert code == 1
    assert "拒绝解压" in capsys.readouterr().err
    assert not (tmp_path / "escape.txt").exists()
    assert not (tmp_path / "data" / "plugins" / "demo").exists()


def test_safe_extract_writes_every_member_of_a_clean_zip(tmp_path):
    """干净包正常解压（拒绝逻辑不能把正常包也拒了）。"""
    archive = make_zip(
        tmp_path / "ok.zip",
        {"demo/plugin.yaml": "name: demo\n", "demo/__init__.py": "x = 1\n"},
    )
    written = extra3.safe_extract(archive, tmp_path / "unpack")
    assert written == ["demo/plugin.yaml", "demo/__init__.py"]
    assert (tmp_path / "unpack" / "demo" / "plugin.yaml").read_text(encoding="utf-8") == "name: demo\n"


# ── 3. search（本地）────────────────────────────────────────────────────────


def test_cli_plugins_search_finds_installed_plugin(tmp_path, capsys):
    source = write_plugin(
        tmp_path / "src",
        "searchable",
        "name: searchable\ndescription: 本地搜索用的插件\nversion: 1.0.0\n",
    )
    assert main(_argv(tmp_path, "plugins", "install", str(source))) == 0
    capsys.readouterr()

    assert main(_argv(tmp_path, "plugins", "search", "本地搜索")) == 0
    out = capsys.readouterr().out
    assert "searchable" in out
    assert "社区索引需要网络" in out, "搜索范围必须自陈"


def test_cli_plugins_search_covers_a_local_hub_directory(tmp_path, capsys):
    """未安装的本地 hub 目录也要能搜到（这是"本地搜索"与"已装插件搜索"的差别）。"""
    hub = tmp_path / "hub"
    write_plugin(hub / "nested", "hub-only", "name: hub-only\ndescription: 只在 hub 里\n")

    assert main(_argv(tmp_path, "plugins", "search", "hub-only", "--hub", str(hub))) == 0
    out = capsys.readouterr().out
    assert "hub-only" in out
    assert "hub" in out


def test_cli_plugins_search_refresh_reports_unimplemented(tmp_path, capsys):
    assert main(_argv(tmp_path, "plugins", "search", "--refresh", "x")) == 1
    assert "未实现" in capsys.readouterr().err


# ── 4. update（按记录的本地 source 重拉）────────────────────────────────────


def test_cli_plugins_update_repulls_from_the_recorded_zip_source(tmp_path, capsys):
    """装 zip → 换掉 zip（新版本）→ update 必须真的把新版本拉进来。"""
    source = write_plugin(tmp_path / "src", "upd", "name: upd\nversion: 1.0.0\n")
    first = tmp_path / "dist" / "upd-1.0.0.zip"
    assert main(_argv(tmp_path, "plugins", "pack", "zip", str(source), "--out", str(first))) == 0
    assert main(_argv(tmp_path, "plugins", "install", str(first))) == 0
    capsys.readouterr()
    installed = tmp_path / "data" / "plugins" / "upd" / "plugin.yaml"
    assert "1.0.0" in installed.read_text(encoding="utf-8")

    # 换一版（同名插件、新版本号），打到**同一个 zip 路径**——source 记录指的就是它
    (source / "plugin.yaml").write_text("name: upd\nversion: 2.0.0\n", encoding="utf-8")
    assert main(_argv(tmp_path, "plugins", "pack", "zip", str(source), "--out", str(first))) == 0
    capsys.readouterr()

    assert main(_argv(tmp_path, "plugins", "update", "upd")) == 0
    out = capsys.readouterr().out
    assert "2.0.0" in installed.read_text(encoding="utf-8"), "本地 source 重拉必须真的生效"
    assert "未实现" in out, "远程 git pull 仍未实现，报告里不能消失"


def test_cli_plugins_update_without_a_record_says_so(tmp_path, capsys):
    """没有 source 记录时如实报，不假装更新过。"""
    source = write_plugin(tmp_path / "src", "plain", "name: plain\nversion: 1.0.0\n")
    data_plugins = tmp_path / "data" / "plugins"
    data_plugins.mkdir(parents=True)
    import shutil

    shutil.copytree(source, data_plugins / "plain")  # 绕过 install，故意不留 source 记录
    capsys.readouterr()

    assert main(_argv(tmp_path, "plugins", "update", "plain")) == 0
    out = capsys.readouterr().out
    assert "没有记录本地 source" in out
    assert "未实现" in out
