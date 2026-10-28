"""懒发现（MATRIX 5.2）的契约测试：AST 扫描 / generation / ensure_loaded / 索引落盘。

四条硬要求，每条对应一类"看起来做了、其实没做"：

1. **扫描真的不 import**。如果扫描顺手 import 了被测模块，那它在"没装依赖的机器"
   上就会失败——而那正是最需要索引的场景（playwright 没装也要能索引出 browser_*）。
   测试用一个"一 import 就炸"的模块来证明这一点。
2. **扫描结果与真实工具表一致**。索引少一条的代价是"这个工具找不到模块"，
   在运行时很难联想到扫描器。
3. **generation 变了才重扫**。这是"generation 计数失效"的落地形态：没人维护计数器，
   指纹自己说话。测试既断言"没变不重扫"，也断言"变了必须重扫"。
4. **``ensure_loaded`` 幂等**。重复注册会踩注册表的遮蔽门禁，也会让 MCP 重连路径
   走进"同名跨源"的分支。
"""

from __future__ import annotations

import json
import os
import textwrap
import time
from pathlib import Path

import pytest

from app import tool_discovery as td
from app.browser import BROWSER_TOOLS
from app.config import Settings
from app.connectors import GITHUB_TOOLS
from app.tools import LOCAL_TOOLS, ToolRegistry
from app.web import WEB_TOOLS

#: 四张真实工具表里的全部工具名（``ask_user`` 由图节点直接注入，不在这四张表里）。
REAL_NAMES = [tool.name for tool in (*LOCAL_TOOLS, *BROWSER_TOOLS, *GITHUB_TOOLS, *WEB_TOOLS)]


def _write_package(root: Path, files: dict[str, str]) -> Path:
    """在临时目录里造一个小包（带 ``__init__.py``，于是模块名带前缀）。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / "__init__.py").write_text("", encoding="utf-8")
    for relative, source in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(source), encoding="utf-8")
    return root


# ── 1. 扫描不 import ────────────────────────────────────────────────────────


def test_scan_does_not_import_the_scanned_module(tmp_path):
    """一 import 就抛的模块，依然必须能被索引出来。

    这条用例的全部意义：``app/browser.py`` 在没装 playwright 的机器上 import 会失败，
    但 ``orbit tools discover`` 仍然要能告诉你 browser_* 定义在哪。
    """
    root = _write_package(
        tmp_path / "pkg",
        {
            "boom.py": """
                raise RuntimeError("这个模块一 import 就炸")

                class Tool:
                    def __init__(self, name, *args, **kwargs):
                        self.name = name

                LOCAL = [Tool("boom_tool", "说明")]
            """,
        },
    )
    index = td.scan_modules(root)
    assert "boom_tool" in index
    info = index["boom_tool"]
    assert info.module == "pkg.boom"
    assert info.lineno > 0


# ── 2. 与真实工具表一致 ─────────────────────────────────────────────────────


def test_scan_finds_every_real_tool():
    """AST 扫描出来的名字必须覆盖四张真实工具表里的每一个工具。"""
    scanned = td.scan_modules(td.app_root())
    missing = [name for name in REAL_NAMES if name not in scanned]
    assert missing == [], f"索引漏了这些工具：{missing}"
    assert len(scanned) >= len(REAL_NAMES)


def test_scan_also_finds_graph_injected_tools():
    """``ask_user`` 由 ``app/graph/nodes.py`` 直接写进注册表，也必须被扫到。

    它不在任何 ``XXX_TOOLS`` 列表里——正是"只认一种摆放方式就会漏"的样本。
    """
    scanned = td.scan_modules(td.app_root())
    info = scanned.get("ask_user")
    assert info is not None
    assert info.module == "app.graph.nodes"


def test_scan_records_module_and_lineno():
    """索引必须能指到 ``文件:行号``，否则它只是个更慢的 grep。"""
    scanned = td.scan_modules(td.app_root())
    info = scanned.get("run_command")
    assert info is not None
    assert info.module == "app.tools"
    assert Path(info.path).name == "tools.py"
    source_lines = Path(info.path).read_text(encoding="utf-8").splitlines()
    # 行号指向的那一行附近必须真的提到这个工具名（行号不能是编的）。
    window = "\n".join(source_lines[info.lineno - 1 : info.lineno + 3])
    assert "run_command" in window


def test_scan_recognises_register_calls(tmp_path):
    """``registry.register(name="x")`` 这种形态也要认——插件与 MCP 走的是它。"""
    root = _write_package(
        tmp_path / "pkg",
        {
            "plugin.py": """
                def install(registry):
                    registry.register(name="plugin_tool", toolset="plugin")
            """,
        },
    )
    index = td.scan_modules(root)
    assert "plugin_tool" in index
    assert index["plugin_tool"].module == "pkg.plugin"


def test_scan_skips_unparsable_files_instead_of_failing(tmp_path):
    """一个语法错的模块不该让整次扫描失败（仓库正被改到一半时也要能用）。"""
    root = _write_package(
        tmp_path / "pkg",
        {
            "good.py": """
                class Tool:
                    def __init__(self, name, *a, **k):
                        self.name = name

                T = Tool("good_tool")
            """,
            "bad.py": "def broken(:\n",
        },
    )
    index = td.scan_modules(root)
    assert "good_tool" in index


# ── 3. generation ───────────────────────────────────────────────────────────


def test_generation_is_stable_while_sources_are_unchanged():
    root = td.app_root()
    assert td.generation(root) == td.generation(root)


def test_index_only_rescans_when_generation_changes(tmp_path):
    """指纹没变 → 不重扫；改了文件 → 必须重扫并看见新工具。"""
    root = _write_package(
        tmp_path / "pkg",
        {
            "one.py": """
                class Tool:
                    def __init__(self, name, *a, **k):
                        self.name = name

                T = [Tool("tool_one")]
            """,
        },
    )
    index = td.Index(root=root)
    assert index.refresh() is True
    assert index.scans == 1
    assert index.find("tool_one") is not None

    # 没改文件：不重扫。
    assert index.refresh() is False
    assert index.scans == 1

    # 加一个工具：指纹必变，重扫后能看见。
    (root / "two.py").write_text(
        textwrap.dedent(
            """
            class Tool:
                def __init__(self, name, *a, **k):
                    self.name = name

            T = [Tool("tool_two")]
            """
        ),
        encoding="utf-8",
    )
    assert index.refresh() is True
    assert index.scans == 2
    assert index.find("tool_two") is not None


def test_generation_changes_when_a_file_is_rewritten(tmp_path):
    """同路径改写内容 → 指纹必须变（只比路径的索引会漏掉这种最常见的修改）。"""
    root = _write_package(tmp_path / "pkg", {"mod.py": "X = 1\n"})
    before = td.generation(root)
    target = root / "mod.py"
    target.write_text("X = 2\n", encoding="utf-8")
    # 显式推进 mtime：某些文件系统的时间精度不足以区分同一毫秒内的两次写入。
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    after = td.generation(root)
    assert before != after


# ── 4. ensure_loaded ────────────────────────────────────────────────────────


def _registry(tmp_path: Path) -> ToolRegistry:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    return ToolRegistry(settings, None)


def test_ensure_loaded_is_idempotent(tmp_path):
    """第一次真的加载，第二次什么都不做，注册表里恰好一份。"""
    index = td.discover()
    registry = _registry(tmp_path)
    assert "vault_login" not in registry.tools

    async def noop(*args, **kwargs):
        return {}

    assert index.ensure_loaded("vault_login", registry, executor=noop) is True
    assert "vault_login" in registry.tools
    assert index.ensure_loaded("vault_login", registry, executor=noop) is False
    assert index.ensure_loaded("vault_login", registry, executor=noop) is False
    assert list(registry.tools).count("vault_login") == 1


def test_ensure_loaded_is_noop_for_already_registered_tools(tmp_path):
    """内置工具在 ``ToolRegistry.__init__`` 里就有了，不该再注册一次。"""
    index = td.discover()
    registry = _registry(tmp_path)
    assert "tool_search" in registry.tools
    assert index.ensure_loaded("tool_search", registry) is False


def test_ensure_loaded_refuses_external_modules_without_executor(tmp_path):
    """外部模块的执行器只有装配层知道——**不许编一个假的**。"""
    index = td.discover()
    registry = _registry(tmp_path)
    with pytest.raises(td.DiscoveryError) as excinfo:
        index.ensure_loaded("browser_observe", registry)
    assert "executor" in str(excinfo.value)


def test_ensure_loaded_reports_unknown_names_actionably(tmp_path):
    index = td.discover()
    registry = _registry(tmp_path)
    with pytest.raises(td.DiscoveryError) as excinfo:
        index.ensure_loaded("no_such_tool_zzz", registry)
    assert "tools discover" in str(excinfo.value)


def test_local_modules_need_no_executor(tmp_path):
    """``app.tools`` 的工具由 ``ToolRegistry.local`` 分发，所以直接进 ``tools`` 就够。"""
    assert "app.tools" in td.LOCAL_MODULES


# ── 5. 索引落盘 ─────────────────────────────────────────────────────────────


def test_index_roundtrip(tmp_path):
    index = td.discover()
    path = td.save_index(index, td.index_path(tmp_path / "data"))
    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["generation"] == index.stamp
    assert len(raw["tools"]) == len(index.entries)

    back = td.load_index(path)
    assert back is not None
    assert back.stamp == index.stamp
    assert set(back.entries) == set(index.entries)
    assert back.find("run_command") == index.find("run_command")


def test_load_index_tolerates_a_broken_file(tmp_path):
    """索引是旁路数据：坏了退回重扫，不该让命令直接失败。"""
    path = tmp_path / "tool-index.json"
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    assert td.load_index(path) is None
    assert td.load_index(tmp_path / "missing.json") is None


def test_discover_writes_the_index_file(tmp_path):
    data_dir = tmp_path / "data"
    td.discover(data_dir=data_dir)
    assert td.index_path(data_dir).exists()


# ── 6. 自述必须诚实 ─────────────────────────────────────────────────────────


def test_describe_admits_the_builtin_tools_gain_nothing():
    """自述里必须写明"内置工具在同一个文件里，省不下 import 成本"。

    这条用例防的是把懒发现吹成"启动快了几百毫秒"——那个数字在 Orbit 上不存在。
    """
    text = td.describe()
    assert "同一个文件" in text
    assert "MCP" in text
    assert "省不下" in text


def test_describe_counts_match_the_index():
    index = td.discover()
    text = td.describe()
    assert f"工具总数：{len(index.entries)}" in text
    for module in index.modules():
        assert module in text


def test_modules_grouping_is_sorted_and_complete():
    index = td.discover()
    grouped = index.modules()
    flat = [name for names in grouped.values() for name in names]
    assert sorted(flat) == sorted(index.entries)
    assert list(grouped) == sorted(grouped)


def test_scan_is_not_slower_than_a_sleep(tmp_path):
    """扫描是纯 AST 解析，不该有隐藏的等待（防有人塞进真实 import 或网络探测）。"""
    started = time.monotonic()
    td.scan_modules(td.app_root())
    assert time.monotonic() - started < 5.0
