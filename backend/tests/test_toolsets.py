"""工具集过滤 + 注册表遮蔽门禁 + `orbit tools` 命令的契约测试。

三层各测各的，失败时能直接看出是哪一层坏了：

1. **归类**：工具必须归到某个 toolset，``unclassified`` 必须为空。这是"别让新工具
   悄悄变成不可控暴露面"的门禁——没人归类就红。
2. **注册表**：遮蔽门禁的三条语义（同 source 允许 / 跨 source 默认拒 / override+授权
   放行）与 ``unregister_source`` 的墓碑，外加 ``specs()`` 的回归保护（默认行为必须
   与引入过滤层之前逐名相等）。
3. **命令面**：``list`` / ``enable`` / ``disable`` / ``post-setup`` 的真实行为。

``app.cli.commands.tools`` 在这里**显式导入**：``commands/__init__.py`` 的接线由另一处
统一做，本文件不依赖那个顺序，也就不会因为接线早晚而红。
"""

from __future__ import annotations

import json
import logging

import pytest

import app.cli.commands.tools  # noqa: F401 —— 导入即登记 `tools` 命令（见模块 docstring）
from app import toolsets
from app.browser import BROWSER_TOOLS
from app.cli.main import main
from app.config import Settings
from app.connectors import GITHUB_TOOLS
from app.tools import LOCAL_TOOLS, Tool, ToolRegistry
from app.web import WEB_TOOLS

#: 四张真实工具表。归类门禁逐个检查它们，而不是只看声明表——声明表自己当然全绿。
REAL_TOOL_LISTS = (LOCAL_TOOLS, BROWSER_TOOLS, GITHUB_TOOLS, WEB_TOOLS)


def _registry(tmp_path) -> ToolRegistry:
    """隔离数据目录上的注册表。``store=None`` 足够：这些用例只碰注册/列举。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    return ToolRegistry(settings, None)


def _tool(name: str, source: str = "builtin") -> Tool:
    return Tool(name=name, description=name, parameters={"type": "object"}, source=source)


def _names() -> list[str]:
    return [tool.name for tool_list in REAL_TOOL_LISTS for tool in tool_list]


def _config(platform: str, *, disabled=(), enabled=()) -> dict:
    return {"platforms": {platform: {"disabled": list(disabled), "enabled": list(enabled)}}}


# ── 1. 归类 ─────────────────────────────────────────────────────────────────


def test_every_real_tool_belongs_to_a_toolset():
    """四张工具表里的每个工具都必须有 toolset，且不是 unclassified。"""
    unclassified = [name for name in _names() if toolsets.toolset_of(name) == toolsets.UNCLASSIFIED]
    assert unclassified == [], f"这些工具没归类，暴露面失控：{unclassified}"


def test_every_toolset_is_declared_and_documented():
    """归类结果必须是声明过的 toolset，且每个 toolset 有一行中文说明。"""
    for name in _names():
        toolset = toolsets.toolset_of(name)
        assert toolset in toolsets.TOOLSETS, f"{name} 归到了未声明的 toolset：{toolset}"
    for toolset, description in toolsets.TOOLSETS.items():
        assert description.strip(), f"toolset {toolset} 缺中文说明"


def test_required_toolsets_exist():
    """路线图要求的 13 个功能域都在。少一个就等于少一块可收窄的暴露面。"""
    required = {
        "core",
        "files",
        "shell",
        "browser",
        "vault",
        "notion",
        "github",
        "memory",
        "history",
        "retrieval",
        "skills",
        "web",
        "mcp",
    }
    assert required <= set(toolsets.TOOLSETS)


def test_dynamic_mcp_prefix_is_classified():
    """MCP 工具是运行时才知道名字的，只能靠前缀归类。"""
    assert toolsets.toolset_of("mcp__github__create_issue") == "mcp"
    assert toolsets.toolset_of("mcp__filesystem__read") == "mcp"


# ── 2. resolve 的三种情形 ───────────────────────────────────────────────────


def test_resolve_defaults_to_everything_exposed():
    """默认全开：没有配置文件时，工具面必须与"没有过滤层"完全一致。"""
    names = _names()
    assert toolsets.resolve(names, "cli", None) == names
    assert toolsets.resolve(names, "cli", toolsets.empty_config()) == names


def test_resolve_excludes_a_disabled_tool():
    """禁用单个工具：只摘掉它，同 toolset 的邻居不受影响。"""
    config = _config("cli", disabled=["web_search"])
    assert toolsets.resolve(["web_search", "web_extract"], "cli", config) == ["web_extract"]


def test_resolve_excludes_a_whole_disabled_toolset():
    """禁用 toolset：该组下所有工具一起消失（Hermes 的启用单位是 toolset）。"""
    config = _config("cli", disabled=["web"])
    assert toolsets.resolve(["web_search", "web_extract", "run_command"], "cli", config) == [
        "run_command"
    ]


def test_resolve_enabled_list_narrows_to_just_those():
    """白名单收窄：enabled 非空时只暴露命中的（工具名与 toolset 名都认）。"""
    config = _config("cli", enabled=["files", "run_command"])
    exposed = toolsets.resolve(
        ["local_list_files", "local_write_file", "run_command", "web_search"], "cli", config
    )
    assert exposed == ["local_list_files", "local_write_file", "run_command"]


def test_resolve_is_per_platform():
    """同一个配置里，别的平台的禁用不影响 cli。"""
    config = _config("telegram", disabled=["web"])
    assert toolsets.resolve(["web_search"], "cli", config) == ["web_search"]
    assert toolsets.resolve(["web_search"], "telegram", config) == []


def test_resolve_accepts_mcp_server_tool_notation():
    """MCP 的 `server:tool` 写法要能命中 `mcp__server__tool` 这个名字。"""
    config = _config("cli", disabled=["github:create_issue"])
    assert toolsets.resolve(
        ["mcp__github__create_issue", "mcp__github__list_issues"], "cli", config
    ) == ["mcp__github__list_issues"]
    server_wide = _config("cli", disabled=["mcp:github"])
    assert toolsets.resolve(["mcp__github__create_issue"], "cli", server_wide) == []


def test_describe_explains_why_a_tool_is_hidden():
    """看不到的工具必须能当场解释原因，否则用户只能猜。"""
    config = _config("cli", disabled=["web"])
    rows = {row["name"]: row for row in toolsets.describe("cli", config, ["web_search"])}
    assert rows["web_search"]["exposed"] is False
    assert "禁用集" in rows["web_search"]["reason"]
    assert rows["web_search"]["toolset"] == "web"


def test_describe_flags_unclassified_without_hiding_it():
    """未归类要打标但**不隐藏**：静默消失会让排查方向跑偏。"""
    rows = toolsets.describe("cli", None, ["totally_new_tool"])
    assert rows[0]["toolset"] == toolsets.UNCLASSIFIED
    assert rows[0]["exposed"] is True
    assert "未归类" in rows[0]["note"]


# ── 3. 遮蔽门禁 ─────────────────────────────────────────────────────────────


def test_cross_source_shadow_is_rejected_without_raising(tmp_path, caplog):
    """跨 source 同名默认被拒：不抛异常，记 log.error，保留原注册。"""
    registry = _registry(tmp_path)
    registry.register([_tool("dup")], lambda *a, **k: None)
    with caplog.at_level(logging.ERROR, logger="orbit.tools"):
        registry.register([_tool("dup", source="plugin:x")], lambda *a, **k: None)
    assert registry.tools["dup"].source == "builtin"
    assert any("同名注册被拒绝" in record.getMessage() for record in caplog.records)


def test_override_without_authorisation_is_still_rejected(tmp_path):
    """override=True 只是意图声明；没有操作员授权仍然拒绝。"""
    registry = _registry(tmp_path)
    registry.register([_tool("dup")], lambda *a, **k: None)
    registry.register([_tool("dup", source="plugin:x")], lambda *a, **k: None, override=True)
    assert registry.tools["dup"].source == "builtin"


def test_override_with_authorisation_replaces_the_tool(tmp_path):
    """override=True + data/tools.json 里授权 → 放行，并留下审计日志。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    toolsets.save_config(
        toolsets.config_path(settings.data_dir), {"allow_tool_override": ["plugin:x"]}
    )
    registry = ToolRegistry(settings, None)
    registry.register([_tool("dup")], lambda *a, **k: None)
    registry.register([_tool("dup", source="plugin:x")], lambda *a, **k: None, override=True)
    assert registry.tools["dup"].source == "plugin:x"


def test_same_source_reregistration_is_allowed(tmp_path):
    """同 source 重复注册是 MCP 重连刷新的正常路径，必须放行。"""
    registry = _registry(tmp_path)
    registry.register([_tool("t", source="mcp:x")], lambda *a, **k: None)
    registry.register(
        [Tool(name="t", description="v2", parameters={}, source="mcp:x")], lambda *a, **k: None
    )
    assert registry.tools["t"].description == "v2"


def test_unregister_source_cannot_be_used_to_bypass_the_gate(tmp_path):
    """先删再注册不能绕过门禁：删掉的非 MCP 名字会留下墓碑。"""
    registry = _registry(tmp_path)
    registry.register([_tool("dup")], lambda *a, **k: None)
    registry.unregister_source("builtin")
    assert "dup" not in registry.tools
    registry.register([_tool("dup", source="plugin:x")], lambda *a, **k: None)
    assert "dup" not in registry.tools, "先删再注册绕过了遮蔽门禁"
    # 授权之后才允许填回这个槽位。
    toolsets.save_config(
        toolsets.config_path(tmp_path / "data"), {"allow_tool_override": ["plugin:x"]}
    )
    registry.register([_tool("dup", source="plugin:x")], lambda *a, **k: None, override=True)
    assert registry.tools["dup"].source == "plugin:x"


def test_mcp_prefix_is_exempt_from_the_gate(tmp_path):
    """`mcp__*` 豁免：动态发现本来就是 nuke-and-repave，同名换源是常态。"""
    registry = _registry(tmp_path)
    registry.register([_tool("mcp__srv__add", source="mcp:a")], lambda *a, **k: None)
    registry.register([_tool("mcp__srv__add", source="mcp:b")], lambda *a, **k: None)
    assert registry.tools["mcp__srv__add"].source == "mcp:b"
    registry.unregister_source("mcp:b")
    registry.register([_tool("mcp__srv__add", source="mcp:a")], lambda *a, **k: None)
    assert registry.tools["mcp__srv__add"].source == "mcp:a"


def test_register_source_argument_overrides_the_default(tmp_path):
    """显式给 source 时以它为准（默认留空 = 沿用 Tool 自带的 source）。"""
    registry = _registry(tmp_path)
    registry.register([_tool("t")], lambda *a, **k: None, source="plugin:y")
    assert registry.tools["t"].source == "plugin:y"


def test_direct_dict_assignment_still_works(tmp_path):
    """`registry.tools["ask_user"] = ...` 这种直接赋值必须继续可用（图节点就是这么注入的）。"""
    registry = _registry(tmp_path)
    registry.tools["ask_user"] = _tool("ask_user")
    assert [spec["function"]["name"] for spec in registry.specs()][-1] == "ask_user"


# ── 4. specs() 回归保护 ─────────────────────────────────────────────────────


def test_specs_cli_matches_the_pre_filter_surface(tmp_path):
    """`specs(platform="cli")` 必须与改动前的工具集合**逐名相等**（顺序也一致）。

    这条是回归闸：过滤层先是"能关"，默认必须还是"全开"。一旦这里漂了，真实行为
    与几十条既有用例会一起变，而没人会立刻发现。
    """
    registry = _registry(tmp_path)
    registry.register(BROWSER_TOOLS, lambda *a, **k: None)
    registry.register(GITHUB_TOOLS, lambda *a, **k: None)
    registry.register(WEB_TOOLS, lambda *a, **k: None)
    expected = [tool.name for tool in (*LOCAL_TOOLS, *BROWSER_TOOLS, *GITHUB_TOOLS, *WEB_TOOLS)]
    assert [spec["function"]["name"] for spec in registry.specs()] == expected
    assert [spec["function"]["name"] for spec in registry.specs(platform="cli")] == expected


def test_specs_honours_the_disabled_set(tmp_path):
    """禁用写进 data/tools.json 后，specs() 立刻少掉那些工具（validate 仍认得它们）。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    registry = ToolRegistry(settings, None)
    registry.register(WEB_TOOLS, lambda *a, **k: None)
    toolsets.save_config(toolsets.config_path(settings.data_dir), _config("cli", disabled=["web"]))
    names = [spec["function"]["name"] for spec in registry.specs()]
    assert "web_search" not in names
    assert "local_list_files" in names
    # 暴露面收窄不等于注销：直接调用仍然可用，只是不再进模型请求。
    assert registry.tools["web_search"] is not None


def test_broken_config_falls_back_to_everything_exposed(tmp_path):
    """坏配置退回全开——配置读不出来不该让 agent 一个工具都没有。"""
    path = toolsets.config_path(tmp_path / "data")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    names = ["web_search", "run_command"]
    assert toolsets.resolve(names, "cli", toolsets.load_config(path)) == names


# ── 5. 命令面 ───────────────────────────────────────────────────────────────


def test_cli_list_shows_toolset_and_exposure(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "tools", "list"])
    out = capsys.readouterr().out
    assert code == 0
    assert "web_search" in out
    assert "web" in out
    assert "toolset" in out


def test_cli_list_json_is_machine_readable(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "--json", "tools", "list"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["platform"] == "cli"
    rows = {row["name"]: row for row in payload["tools"]}
    assert rows["web_search"]["exposed"] is True
    assert rows["web_search"]["toolset"] == "web"


def test_cli_disable_hides_and_enable_restores(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    assert main(["--data-dir", data_dir, "tools", "disable", "web"]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "tools", "list"]) == 0
    hidden = capsys.readouterr().out
    assert "web_search" in hidden  # 仍然列出来，只是标成不暴露
    assert "web_extract" in hidden
    rows = {
        line.split()[0]: line
        for line in hidden.splitlines()
        if line.startswith(("web_search", "run_command"))
    }
    assert "否" in rows["web_search"]
    assert "是" in rows["run_command"]

    assert main(["--data-dir", data_dir, "tools", "enable", "web"]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "--json", "tools", "list"]) == 0
    payload = json.loads(capsys.readouterr().out)
    exposed = {row["name"] for row in payload["tools"] if row["exposed"]}
    assert {"web_search", "web_extract"} <= exposed


def test_cli_rejects_a_misspelled_name(tmp_path, capsys):
    """拼错必须报错并给候选——静默接受等于让用户以为关掉了。"""
    code = main(["--data-dir", str(tmp_path / "data"), "tools", "disable", "websearch"])
    assert code == 1
    assert "不认识" in capsys.readouterr().err


def test_cli_disable_writes_the_platform_config(tmp_path, capsys):
    data_dir = tmp_path / "data"
    assert main(["--data-dir", str(data_dir), "tools", "disable", "run_command"]) == 0
    capsys.readouterr()
    raw = json.loads(toolsets.config_path(data_dir).read_text(encoding="utf-8"))
    assert raw["platforms"]["cli"]["disabled"] == ["run_command"]
    assert raw["platforms"]["cli"]["enabled"] == []


def test_cli_disable_is_per_platform(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    assert main(["--data-dir", data_dir, "tools", "disable", "web", "--platform", "telegram"]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "--json", "tools", "list"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert {row["name"] for row in payload["tools"] if row["exposed"]} >= {"web_search"}


def test_cli_post_setup_prints_pasteable_json(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "tools", "post-setup"])
    out = capsys.readouterr().out
    assert code == 0
    snapshot = json.loads(out)  # 能解析 = 能直接贴进 data/tools.json
    assert "cli" in snapshot["platforms"]
    assert snapshot["platforms"]["cli"]["disabled"] == []


def test_cli_summary_runs(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "tools", "--summary"])
    out = capsys.readouterr().out
    assert code == 0
    assert "cli" in out


@pytest.mark.parametrize("subcommand", ["list", "enable", "disable", "post-setup"])
def test_cli_help_renders(subcommand):
    """四个子命令的 `--help` 都得能渲染（登记了但 help 崩掉等于没有）。"""
    from app.cli.main import build_parser

    parser = build_parser()
    with pytest.raises(SystemExit) as excinfo:
        parser.parse_args(["tools", subcommand, "--help"])
    assert excinfo.value.code == 0
