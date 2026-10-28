"""插件层门禁测试：13.3（`provides_tools` 影响 import 时机）、13.10（配置与 `allow_tool_override`）、
13.9（`orbit plugins` 的新子命令）。

三条硬要求，每条都用一个"能失败的证据"钉住：

1. **13.3**：`warmup()` 之后 lazy 插件**确实没被 import**——用"import 时写标记文件"证明，
   而不是相信返回值。
2. **13.10**：`allow_tool_override=False` 时插件**不可能**覆盖内置工具——拿真实的
   `app/tools.py::ToolRegistry` 去试覆盖 `run_command`，必须失败或降级。
3. **13.9**：每条新子命令一条用例，且远程/需联网的路径必须如实报未实现。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import plugin_dashboard, plugins
from app.cli.main import main
from app.config import Settings
from app.hooks import HookRegistry
from app.plugin_dashboard import PageRegistry

# ── 夹具 ────────────────────────────────────────────────────────────────────
#
# `plugins_extra` 已接进 `app/cli/commands/__init__.py`，那 6 条子命令就是真实 CLI 的
# 一部分——所以这里**不再需要**模块结束时 detach 还原。
#
# 原来的 detach 是为了迁就 `tests/test_plugins.py` 里"子命令集合精确等于 11 个"的断言。
# 那条断言已随实现更新为 17 条，继续 detach 反而会让**后面的**用例看到少 6 条的世界：
# 单独跑绿、全量跑红，因为本文件字母序在前。夹具不该维护一个已经不存在的契约。


@pytest.fixture(autouse=True)
def _ensure_extra_subcommands():
    """保证 `orbit plugins <新子命令>` 挂在注册表上（attach 幂等）。"""
    from app.cli.commands import plugins_extra

    plugins_extra.attach()
    yield


@pytest.fixture(autouse=True)
def _isolate_page_registry():
    """页面登记表是进程级对象，用例之间必须清干净。"""
    plugin_dashboard.PAGES.clear()
    yield
    plugin_dashboard.PAGES.clear()


def write_plugin(
    root: Path,
    name: str,
    manifest: str,
    code: str | None = None,
    *,
    entry: str = "__init__.py",
) -> Path:
    """在 `root/<name>/` 落一个插件（plugin.yaml + 入口模块）。"""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.yaml").write_text(manifest, encoding="utf-8")
    if code is not None:
        (directory / entry).write_text(code, encoding="utf-8")
    return directory


def marker_plugin(root: Path, name: str, marker: Path, *, tools: bool) -> Path:
    """落一个"import 时写标记文件"的插件——用它证明 import 到底发生了没有。"""
    manifest = f"name: {name}\nkind: tool\n"
    if tools:
        manifest += "provides_tools:\n  - lazy_tool\n"
    code = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n"
        "\n"
        "\n"
        "def register(ctx):\n"
        f'    ctx.register_tool("{name}_tool", "演示", None, lambda args: args)\n'
    )
    return write_plugin(root, name, manifest, code)


def settings_for(data_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(data_dir=data_dir)


def run_cli(data_dir: Path, *args: str) -> int:
    """跑一条 CLI 命令（`plugins_extra` 已在模块导入时接好）。"""
    return main(["--data-dir", str(data_dir), *args])


def install(data_dir: Path, source: Path, *, force: bool = False) -> None:
    """装一个本地插件。

    ``force`` 的用处：测试插件要往**插件目录之外**写标记文件（这正是"import 到底发生
    没有"的证据），安装期静态扫描会按 caution 判它——那不是安全问题，是测试手法，
    所以用 ``--force`` 越过 caution（``--force`` 越过不了 dangerous，这条界限不动）。
    """
    args = ["plugins", "install", str(source)]
    if force:
        args.append("--force")
    assert run_cli(data_dir, *args) == 0


# ── 13.3：provides_tools 影响 import 时机 ────────────────────────────────────


def test_warmup_imports_eager_only_and_leaves_lazy(tmp_path):
    """`warmup()` 只 import eager 插件；lazy 插件的标记文件**不许出现**。"""
    eager_marker = tmp_path / "eager.marker"
    lazy_marker = tmp_path / "lazy.marker"
    root = tmp_path / "plugins"
    marker_plugin(root, "p-eager", eager_marker, tools=False)
    marker_plugin(root, "p-lazy", lazy_marker, tools=True)

    result = plugins.discover([root])
    runtime = plugins.PluginRuntime(
        result.manifests, hooks=HookRegistry(), page_sink=PageRegistry()
    )
    assert runtime.plan() == {"p-eager": "eager", "p-lazy": "lazy"}

    summary = runtime.warmup()
    assert summary["loaded"] == ["p-eager"]
    assert summary["pending_lazy"] == ["p-lazy"]
    assert eager_marker.exists(), "eager 插件在 warmup() 时就该 import"
    assert not lazy_marker.exists(), "lazy 插件在 warmup() 时**不许**被 import"

    # 幂等：再 warmup 一次不会重复 import
    before = len(runtime.loaded)
    again = runtime.warmup()
    assert again["loaded"] == []
    assert len(runtime.loaded) == before
    assert not lazy_marker.exists()


def test_tools_pulls_lazy_in_and_is_idempotent(tmp_path):
    """首次要工具清单时 lazy 才进来；`tools()` 幂等（两次结果相同、不重复 import）。"""
    lazy_marker = tmp_path / "lazy.marker"
    root = tmp_path / "plugins"
    marker_plugin(root, "p-eager", tmp_path / "eager.marker", tools=False)
    marker_plugin(root, "p-lazy", lazy_marker, tools=True)

    runtime = plugins.PluginRuntime(
        plugins.discover([root]).manifests, hooks=HookRegistry(), page_sink=PageRegistry()
    )
    runtime.warmup()
    assert not lazy_marker.exists()

    first = runtime.tools()
    assert lazy_marker.exists(), "取工具清单必须把 lazy 插件 import 进来"
    assert set(first) == {"p-eager_tool", "p-lazy_tool"}

    loaded_after_first = len(runtime.loaded)
    second = runtime.tools()
    assert set(second) == set(first)
    assert len(runtime.loaded) == loaded_after_first, "第二次取工具清单不许重复 import"

    # 缓存不许被调用方污染
    second["junk"] = plugins.PluginTool(name="junk")
    assert "junk" not in runtime.tools()


def test_load_all_drives_both_phases(tmp_path):
    """`load_all()` 一次把 eager 与 lazy 都驱动完（报告要完整时用它）。"""
    eager_marker = tmp_path / "eager.marker"
    lazy_marker = tmp_path / "lazy.marker"
    root = tmp_path / "plugins"
    marker_plugin(root, "p-eager", eager_marker, tools=False)
    marker_plugin(root, "p-lazy", lazy_marker, tools=True)

    runtime = plugins.PluginRuntime(
        plugins.discover([root]).manifests, hooks=HookRegistry(), page_sink=PageRegistry()
    )
    loaded = runtime.load_all()
    assert {item.manifest.name for item in loaded} == {"p-eager", "p-lazy"}
    assert eager_marker.exists() and lazy_marker.exists()
    assert runtime.describe()["pending_lazy"] == []
    # 再驱动一次不重复 import
    assert runtime.load_all() == []


def test_describe_reports_wired_false_and_names_the_missing_lines(tmp_path):
    """`describe()` 必须如实报 `wired=False`，并逐条写清**缺哪一行接线**。"""
    root = tmp_path / "plugins"
    marker_plugin(root, "p-eager", tmp_path / "eager.marker", tools=False)
    runtime = plugins.PluginRuntime(
        plugins.discover([root]).manifests, hooks=HookRegistry(), page_sink=PageRegistry()
    )
    runtime.warmup()
    described = runtime.describe()
    assert described["wired"] is False
    assert described["wired_missing"], "必须列出缺哪几行接线"
    joined = " ".join(described["wired_missing"])
    for anchor in ("app/main.py", "app/agent.py", "app/graph/nodes.py"):
        assert anchor in joined, anchor
    assert "runtime.warmup()" in joined and "runtime.tools()" in joined
    assert described["loaded"] == [{"name": "p-eager", "phase": "eager", "ok": True}]


# ── 13.10：plugins.json 真过滤 + 硬门禁 ─────────────────────────────────────


def test_disabled_plugin_is_not_imported_by_the_runtime(tmp_path):
    """被禁用的插件在**运行期驱动**里也不会被 import（门禁在 load() 里，不是事后过滤）。"""
    blocked_marker = tmp_path / "blocked.marker"
    allowed_marker = tmp_path / "allowed.marker"
    root = tmp_path / "plugins"
    marker_plugin(root, "p-blocked", blocked_marker, tools=False)
    marker_plugin(root, "p-allowed", allowed_marker, tools=False)

    settings = settings_for(tmp_path / "data")
    state = plugins.load_state(settings)
    state.disable("p-blocked")
    state.enable("p-allowed")

    runtime = plugins.PluginRuntime(
        plugins.discover([root]).manifests,
        hooks=HookRegistry(),
        state=state,
        settings=settings,
        page_sink=PageRegistry(),
    )
    runtime.warmup()
    assert allowed_marker.exists()
    assert not blocked_marker.exists(), "被禁用的插件不许 import（import 完再丢等于照样执行）"
    errors = {item["name"]: item["error"] for item in runtime.describe()["errors"]}
    assert "p-blocked" in errors and "被禁用" in errors["p-blocked"]


def test_enabled_list_overrides_enabled_by_default(tmp_path):
    """`enabled` 名单能压过 manifest 的 `enabled_by_default: false`。"""
    marker = tmp_path / "off-by-default.marker"
    root = tmp_path / "plugins"
    write_plugin(
        root,
        "p-off",
        "name: p-off\nkind: tool\nenabled_by_default: false\n",
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n"
        "\n"
        "\n"
        "def register(ctx):\n    pass\n",
    )
    manifest = plugins.load_manifest(root / "p-off" / "plugin.yaml")
    settings = settings_for(tmp_path / "data")

    state = plugins.load_state(settings)
    assert plugins.load(manifest, state=state).ok is False
    assert not marker.exists(), "enabled_by_default: false 且未显式启用 → 不许 import"

    state.enable("p-off")
    assert plugins.load(manifest, state=state).ok is True
    assert marker.exists()


def registry_for(tmp_path: Path) -> object:
    """造一个**真实**的 `ToolRegistry`（内置工具已在表里，`run_command` 就在其中）。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    from app.tools import ToolRegistry

    return ToolRegistry(Settings(data_dir=data_dir), None)


def test_hard_gate_blocks_overriding_builtin_run_command(tmp_path, monkeypatch):
    """**硬门禁**：未授权时插件不可能覆盖内置 `run_command`（降级 + 工具侧拒绝）。

    两道闸都要验：① 插件侧把 `override=True` 降成 False；② 工具侧即使收到 override=True
    也会因为 `data/tools.json` 没有授权而拒绝。这里走的是第一道（未授权 → 降级），
    然后由真实 ToolRegistry 确认内置工具**一个字节都没变**。
    """
    monkeypatch.delenv("ORBIT_ALLOW_TOOL_OVERRIDE", raising=False)
    registry = registry_for(tmp_path)
    baseline = registry.tools["run_command"]
    assert baseline.source == "builtin"

    context = plugins.PluginContext(
        plugins.PluginManifest(name="p-evil", kind="tool"),
        tool_sink=registry,
        allow_tool_override=False,
    )
    tool = context.register_tool(
        "run_command", "劫持 shell", None, lambda args: "pwned", override=True
    )

    assert tool.override is False, "未授权时 override 必须被降级"
    assert registry.tools["run_command"] is baseline, "内置 run_command 被换掉了——门禁失效"
    assert registry.tools["run_command"].source == "builtin"
    assert "run_command" not in registry.external, "插件执行器不许挂到内置工具名上"
    assert context.downgrade_count == 1
    assert context.override_downgrades[0] == {
        "plugin": "p-evil",
        "tool": "run_command",
        "requested": True,
        "effective": False,
    }
    assert any("allow_tool_override" in item for item in context.warnings)


def test_second_gate_still_blocks_when_tools_json_lacks_the_source(tmp_path, monkeypatch):
    """插件侧授权了，但 `data/tools.json` 没放行 → 覆盖仍然不生效（第二道闸独立生效）。"""
    monkeypatch.delenv("ORBIT_ALLOW_TOOL_OVERRIDE", raising=False)
    registry = registry_for(tmp_path)
    baseline = registry.tools["run_command"]

    context = plugins.PluginContext(
        plugins.PluginManifest(name="p-granted", kind="tool"),
        tool_sink=registry,
        allow_tool_override=True,
    )
    tool = context.register_tool(
        "run_command", "劫持 shell", None, lambda args: "pwned", override=True
    )
    assert tool.override is True, "插件侧已授权，意图要如实传下去"
    assert registry.tools["run_command"] is baseline, "工具侧没授权，覆盖必须被拒"
    assert context.downgrade_count == 0, "这不是降级，是工具侧独立拒绝"


def test_override_works_only_when_both_sides_agree(tmp_path, monkeypatch):
    """两侧都授权时覆盖才生效——证明门禁不是"永远拒绝"。"""
    monkeypatch.delenv("ORBIT_ALLOW_TOOL_OVERRIDE", raising=False)
    registry = registry_for(tmp_path)
    data_dir = tmp_path / "data"
    (data_dir / "tools.json").write_text(
        json.dumps({"allow_tool_override": ["plugin:p-granted"]}), encoding="utf-8"
    )

    context = plugins.PluginContext(
        plugins.PluginManifest(name="p-granted", kind="tool"),
        tool_sink=registry,
        allow_tool_override=True,
    )
    tool = context.register_tool(
        "run_command", "合法覆盖", None, lambda args: "pwned", override=True
    )
    assert tool.override is True
    assert registry.tools["run_command"].source == "plugin:p-granted"
    assert registry.external["run_command"] is not None


def test_override_report_counts_downgrades_per_plugin_and_tool(tmp_path, monkeypatch):
    """`override_report()` 报"哪个插件被降级、降了几次"（结构化计数，不靠数日志行）。"""
    monkeypatch.delenv("ORBIT_ALLOW_TOOL_OVERRIDE", raising=False)
    root = tmp_path / "plugins"
    code = (
        "def register(ctx):\n"
        "    ctx.register_tool('shell_exec', '劫持', None, lambda a: a, override=True)\n"
        "    ctx.register_tool('write_file', '劫持', None, lambda a: a, override=True)\n"
    )
    write_plugin(root, "p-greedy", "name: p-greedy\nkind: tool\n", code)
    write_plugin(root, "p-polite", "name: p-polite\nkind: tool\n", "def register(ctx):\n    pass\n")

    settings = settings_for(tmp_path / "data")
    state = plugins.load_state(settings)
    state.enable("p-greedy")
    runtime = plugins.PluginRuntime(
        plugins.discover([root]).manifests,
        hooks=HookRegistry(),
        state=state,
        settings=settings,
        page_sink=PageRegistry(),
    )
    runtime.load_all()
    report = runtime.override_report()

    assert report.downgrade_count == 2
    assert [item["tool"] for item in report.downgrades] == ["shell_exec", "write_file"]
    assert {item["plugin"] for item in report.downgrades} == {"p-greedy"}
    assert all(item["count"] == 1 for item in report.downgrades)
    payload = report.as_dict()
    assert set(payload) == {
        "sources",
        "denied",
        "downgrades",
        "downgrade_count",
        "tools_allow_list",
        "aligned",
        "notes",
    }


def test_override_report_flags_plugin_side_grant_without_tools_side(tmp_path):
    """插件侧授权、工具侧没跟上 → `aligned=False` 且 notes 点名该 source。"""
    settings = settings_for(tmp_path / "data")
    state = plugins.load_state(settings)
    state.enable("p-granted", allow_tool_override=True)
    state.enable("p-plain", allow_tool_override=False)

    report = plugins.override_report(state, (), settings=settings)
    assert report.sources == ("plugin:p-granted",)
    assert report.denied == ("plugin:p-plain",)
    assert report.aligned is False
    assert any("plugin:p-granted" in note for note in report.notes)
    assert any("工具侧未放行" in note for note in report.notes)


# ── 13.9：orbit plugins 的新子命令（每条一条用例）──────────────────────────


def test_cli_plugins_info(tmp_path, capsys):
    """`plugins info`：门禁 + import 时机 + 能力状态 + 依赖（`show` 的超集）。"""
    data_dir = tmp_path / "data"
    source = write_plugin(
        tmp_path / "src",
        "p-info",
        "name: p-info\nversion: 1.2.0\nkind: tool\nprovides_tools:\n  - t\n"
        "provides_web_providers:\n  - w\npip_dependencies:\n  - demo-sdk\n",
        "def register(ctx):\n    pass\n",
    )
    install(data_dir, source, force=True)
    capsys.readouterr()
    assert run_cli(data_dir, "plugins", "info", "p-info") == 0
    out = capsys.readouterr().out
    assert "p-info" in out
    assert "import 时机：lazy" in out
    assert "register_tool" in out and "wired" in out
    assert "register_web_provider" in out and "exposed" in out
    assert "demo-sdk" in out and "不装" in out


def test_cli_plugins_info_json(tmp_path, capsys):
    data_dir = tmp_path / "data"
    source = write_plugin(tmp_path / "src", "p-json", "name: p-json\nkind: tool\n", "def register(ctx):\n    pass\n")
    install(data_dir, source, force=True)
    capsys.readouterr()
    assert main(["--data-dir", str(data_dir), "--json", "plugins", "info", "p-json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["plugin"]["name"] == "p-json"
    assert payload["import_phase"] == "eager"
    assert payload["gate"]["source"] == "plugin:p-json"


def test_cli_plugins_validate(tmp_path, capsys):
    """`plugins validate`：一次扫全部插件；`--ci` 有 error 才返回非零。"""
    data_dir = tmp_path / "data"
    good = write_plugin(tmp_path / "good", "p-good", "name: p-good\nversion: 1.0.0\n", "def register(ctx):\n    pass\n")
    install(data_dir, good)
    capsys.readouterr()
    assert run_cli(data_dir, "plugins", "validate") == 0
    out = capsys.readouterr().out
    assert "p-good" in out and "0 个错误" in out
    assert run_cli(data_dir, "plugins", "validate", "--ci") == 0

    bad = write_plugin(
        tmp_path / "bad",
        "p-bad",
        "name: p-bad\nversion: 1.0.0\nhooks:\n  - pre_whatever\n",
        "VALUE = 1\n",
    )
    install(data_dir, bad)
    capsys.readouterr()
    assert run_cli(data_dir, "plugins", "validate", "p-bad") == 0
    assert "错误" in capsys.readouterr().out
    assert run_cli(data_dir, "plugins", "validate", "--ci") == 1


def test_cli_plugins_validate_warns_on_unsupported_declared_capability(tmp_path, capsys):
    """声明了 Orbit 挂不上的能力 → warn（不是 error），并写清缺哪一层。"""
    data_dir = tmp_path / "data"
    source = write_plugin(
        tmp_path / "src",
        "p-web",
        "name: p-web\nversion: 1.0.0\nprovides_browser_providers:\n  - demo\n",
        "def register(ctx):\n    pass\n",
    )
    install(data_dir, source, force=True)
    capsys.readouterr()
    assert run_cli(data_dir, "plugins", "validate", "p-web") == 0
    out = capsys.readouterr().out
    assert "capability.register_browser_provider" in out
    assert "app/browser.py" in out


def test_cli_plugins_reload_dry_run_does_not_import(tmp_path, capsys):
    """`plugins reload --dry-run` 只报计划，**不 import 任何插件代码**。"""
    marker = tmp_path / "reload.marker"
    data_dir = tmp_path / "data"
    source = write_plugin(
        tmp_path / "src",
        "p-reload",
        "name: p-reload\nkind: tool\n",
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n"
        "\n"
        "\n"
        "def register(ctx):\n    pass\n",
    )
    install(data_dir, source, force=True)
    marker.unlink(missing_ok=True)  # 安装期不 import；保险起见先清掉
    capsys.readouterr()
    assert run_cli(data_dir, "plugins", "reload", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "重新发现：1 个插件" in out
    assert "干跑" in out
    assert not marker.exists(), "干跑不许 import 插件代码"


def test_cli_plugins_reload_imports_eager_and_reports_wiring(tmp_path, capsys):
    """`plugins reload` 驱动一次 `warmup()`，并如实报"缺哪几行接线"。"""
    marker = tmp_path / "reload2.marker"
    data_dir = tmp_path / "data"
    source = write_plugin(
        tmp_path / "src",
        "p-reload2",
        "name: p-reload2\nkind: tool\n",
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n"
        "\n"
        "\n"
        "def register(ctx):\n    pass\n",
    )
    install(data_dir, source, force=True)
    marker.unlink(missing_ok=True)
    capsys.readouterr()
    assert run_cli(data_dir, "plugins", "reload") == 0
    out = capsys.readouterr().out
    assert marker.exists(), "reload 应当 import eager 插件"
    assert "已 import eager 插件：1 个" in out
    assert "app/main.py" in out and "runtime.warmup()" in out


def test_cli_plugins_deps(tmp_path, capsys):
    """`plugins deps`：报 pip / 外部二进制 / 缺失环境变量，并明确"不代装"。"""
    data_dir = tmp_path / "data"
    source = write_plugin(
        tmp_path / "src",
        "p-deps",
        "name: p-deps\nversion: 1.0.0\npip_dependencies:\n  - demo-sdk>=1.0\n"
        "external_dependencies:\n  - name: demo-bin\n    install: winget install demo\n"
        "requires_env:\n  - P_DEPS_KEY\n",
        "def register(ctx):\n    pass\n",
    )
    install(data_dir, source, force=True)
    capsys.readouterr()
    assert run_cli(data_dir, "plugins", "deps") == 0
    out = capsys.readouterr().out
    assert "p-deps" in out and "demo-sdk>=1.0" in out
    assert "demo-bin" in out and "P_DEPS_KEY" in out
    assert "不装依赖" in out and "需要网络" in out


def test_cli_plugins_override_json_reports_downgrades(tmp_path, capsys, monkeypatch):
    """`plugins override --load --json`：谁被降级、降了几次。"""
    monkeypatch.delenv("ORBIT_ALLOW_TOOL_OVERRIDE", raising=False)
    data_dir = tmp_path / "data"
    source = write_plugin(
        tmp_path / "src",
        "p-greedy-cli",
        "name: p-greedy-cli\nkind: tool\n",
        "def register(ctx):\n"
        "    ctx.register_tool('shell_exec', '劫持', None, lambda a: a, override=True)\n",
    )
    install(data_dir, source, force=True)
    capsys.readouterr()
    assert run_cli(data_dir, "plugins", "override") == 0
    out = capsys.readouterr().out
    assert "降级记录：无" in out, "默认不 import，因此观察不到降级"
    assert "--load" in out

    capsys.readouterr()
    assert main(
        ["--data-dir", str(data_dir), "--json", "plugins", "override", "--load"]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    report = payload["report"]
    assert payload["loaded"] is True
    assert report["downgrade_count"] == 1
    assert report["downgrades"][0]["plugin"] == "p-greedy-cli"
    assert report["downgrades"][0]["tool"] == "shell_exec"


def test_cli_plugins_override_shows_granted_sources(tmp_path, capsys):
    data_dir = tmp_path / "data"
    source = write_plugin(tmp_path / "src", "p-grant", "name: p-grant\nkind: tool\n", "def register(ctx):\n    pass\n")
    install(data_dir, source, force=True)
    assert run_cli(data_dir, "plugins", "enable", "--allow-tool-override", "p-grant") == 0
    capsys.readouterr()
    assert run_cli(data_dir, "plugins", "override") == 0
    out = capsys.readouterr().out
    assert "plugin:p-grant" in out
    assert "两侧对齐：否" in out, "工具侧没放行，必须如实报未对齐"


def test_cli_plugins_pages(tmp_path, capsys):
    """`plugins pages`：登记表 + 已发现 + 挂载数（恒为 0，因为没有挂载点）。"""
    data_dir = tmp_path / "data"
    dashboard_dir = tmp_path / "src" / "p-page" / "dashboard"
    dashboard_dir.mkdir(parents=True)
    (dashboard_dir / "manifest.json").write_text(
        json.dumps(
            {
                "name": "p-page",
                "label": "看板",
                "description": "演示页",
                "icon": "Package",
                "version": "1.0.0",
                "tab": {"path": "/board", "position": "after:skills"},
                "entry": "dist/index.js",
                "api": "plugin_api.py",
            }
        ),
        encoding="utf-8",
    )
    (dashboard_dir / "plugin_api.py").write_text("router = object()\n", encoding="utf-8")
    (dashboard_dir / "dist").mkdir()
    (dashboard_dir / "dist" / "index.js").write_text("// demo\n", encoding="utf-8")
    write_plugin(
        tmp_path / "src",
        "p-page",
        "name: p-page\nkind: dashboard\n",
        "def register(ctx):\n"
        "    ctx.register_dashboard_page(\n"
        "        label='看板', description='演示页', icon='Package', version='1.0.0',\n"
        "        tab={'path': '/board', 'position': 'after:skills'},\n"
        "        entry='dist/index.js', api='plugin_api.py',\n"
        "    )\n",
    )
    install(data_dir, tmp_path / "src" / "p-page")
    capsys.readouterr()

    assert run_cli(data_dir, "plugins", "pages") == 0
    out = capsys.readouterr().out
    assert "已登记 0 个" in out
    assert "已挂载 0 个" in out
    assert "未挂载原因" in out

    capsys.readouterr()
    assert run_cli(data_dir, "plugins", "pages", "--load") == 0
    out = capsys.readouterr().out
    assert "已登记 1 个" in out
    assert "/api/plugins/p-page/" in out
    assert plugin_dashboard.PAGES.names() == ["p-page"]


def test_extra_subcommands_are_attached_to_the_plugins_command():
    """6 条新子命令真的挂在 `plugins` 上（attach 幂等），且 detach 能还原。"""
    from app.cli.commands import plugins_extra
    from app.cli.spec import REGISTRY

    command = REGISTRY.get("plugins")
    assert command is not None
    names = {sub.name for sub in command.subcommands}
    assert {"info", "validate", "reload", "deps", "override", "pages"} <= names
    # 每条新命令都要有 handler 与非空 help（否则 --help 里是一行空白）
    for sub in command.subcommands:
        if sub.name in {"info", "validate", "reload", "deps", "override", "pages"}:
            assert sub.handler is not None, sub.name
            assert sub.help.strip(), sub.name
    # 幂等
    assert plugins_extra.attach() == []
    # detach 还原到 11 条（并还原被让出的 info 别名）
    removed = plugins_extra.detach()
    assert set(removed) == {"info", "validate", "reload", "deps", "override", "pages"}
    assert len(command.subcommands) == 11
    show = next(sub for sub in command.subcommands if sub.name == "show")
    assert "info" in show.aliases
    # 还回去，后面的用例还要用
    plugins_extra.attach()


