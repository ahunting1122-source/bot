"""dashboard 插件契约（13.8）与插件层四处改动（13.3 / 13.4 / 13.9 / 13.10）的契约测试。

四块：

1. **dashboard manifest**：8 字段必填、缺字段要能**逐条点名**、``routes_for`` 生成的路径
   必须是 ``/api/plugins/<name>/…`` 形态、``describe`` 必须承认"真正挂载未实现"。
2. **13.3 import 时机**：``provides_tools`` 的插件是 ``lazy``——``PluginRuntime.start()``
   之后它**没有被 import**（用磁盘标记证明，不靠 ``sys.modules`` 的命名巧合），
   直到首次要工具清单时才 import。
3. **13.9 ``orbit plugins`` 的 install / search**：本地目录可用、远程源如实报未实现。
4. **13.10 门禁**：``plugins.json`` 的 ``disabled`` 让插件**不被 import**、
   ``allow_tool_override`` 能被读到并真的把 ``override=True`` 降级。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import plugin_dashboard as dash
from app import plugins
from app.cli.main import main
from app.hooks import HookRegistry

#: 显式导入即接线（生产里由 ``app/cli/commands/__init__.py`` 统一接线）。
import app.cli.commands.plugins as plugins_cli  # noqa: E402,F401  isort: skip

# ── 夹具 ────────────────────────────────────────────────────────────────────

#: 一份照 research §1.7 的真实形状写的 manifest（kanban 的 9 个键：8 必填 + css）。
KANBAN_MANIFEST = {
    "name": "kanban",
    "label": "Kanban",
    "description": "Multi-agent collaboration board",
    "icon": "Package",
    "version": "1.0.0",
    "tab": {"path": "/kanban", "position": "after:skills"},
    "entry": "dist/index.js",
    "css": "dist/style.css",
    "api": "plugin_api.py",
}

#: 真实 ``plugin_api.py`` 的两个硬契约：模块级 router + 不依赖 dashboard 也能 import。
PLUGIN_API = '''
"""Kanban dashboard plugin — backend API routes.

Mounted at /api/plugins/kanban/ by the dashboard plugin system.
"""
try:
    from fastapi import APIRouter
except Exception:  # 无 dashboard 依赖时也能 import（照 Hermes 的兜底写法）
    class APIRouter:  # type: ignore
        def get(self, *_a, **_k):
            return lambda fn: fn

        def post(self, *_a, **_k):
            return lambda fn: fn


router = APIRouter()


@router.get("/items")
def items():
    return []


@router.post("/items/{item_id}/move")
def move(item_id: str):
    return {"moved": item_id}


@router.delete("/items/{item_id}")
def remove(item_id: str):
    return {"removed": item_id}
'''


def write_dashboard(root: Path, name: str = "kanban", *, manifest: dict | None = None) -> Path:
    """按 Hermes 布局落一个 dashboard 插件：``<root>/<name>/dashboard/``。"""
    directory = root / name / "dashboard"
    (directory / "dist").mkdir(parents=True, exist_ok=True)
    (directory / "dist" / "index.js").write_text("export default {};\n", encoding="utf-8")
    (directory / "dist" / "style.css").write_text("/* x */\n", encoding="utf-8")
    (directory / "plugin_api.py").write_text(PLUGIN_API, encoding="utf-8")
    payload = dict(manifest if manifest is not None else KANBAN_MANIFEST)
    payload.setdefault("name", name)
    (directory / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return directory


def settings_for(data_dir: Path) -> SimpleNamespace:
    return SimpleNamespace(data_dir=data_dir)


def write_plugin(
    root: Path,
    name: str,
    manifest: str,
    code: str | None = None,
    *,
    entry: str = "__init__.py",
) -> Path:
    """在 ``root/<name>/`` 落一个普通插件（plugin.yaml + 入口模块）。"""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.yaml").write_text(manifest, encoding="utf-8")
    if code is not None:
        (directory / entry).write_text(code, encoding="utf-8")
    return directory


def _run(tmp_path, *argv: str) -> int:
    return main(["--data-dir", str(tmp_path / "data"), *argv])


# ── 1. dashboard manifest：8 字段 / 校验 / 路由 ─────────────────────────────


def test_manifest_field_table_has_eight_required_fields():
    """8 个必填字段一个不少；``css`` 单独登记为可选（调研正文写 8、字段表列 9）。"""
    assert len(dash.DASHBOARD_FIELDS) == 8
    assert set(dash.DASHBOARD_FIELDS) == {
        "name",
        "label",
        "description",
        "icon",
        "version",
        "tab",
        "entry",
        "api",
    }
    assert set(dash.OPTIONAL_DASHBOARD_FIELDS) == {"css"}
    for field_name, meaning in (*dash.DASHBOARD_FIELDS.items(),
                                *dash.OPTIONAL_DASHBOARD_FIELDS.items()):
        assert meaning.strip(), f"{field_name} 没写含义"


def test_parse_and_validate_real_manifest(tmp_path):
    directory = write_dashboard(tmp_path)
    manifest = dash.load_manifest(directory / "manifest.json")
    assert manifest.name == "kanban"
    assert manifest.tab.path == "/kanban"
    assert manifest.tab.position == "after:skills"
    assert manifest.route_prefix == "/api/plugins/kanban/"
    assert manifest.frontend_path == "/kanban"
    validation = dash.validate(manifest)
    assert validation.ok, [item.as_dict() for item in validation.issues]
    assert validation.issues == ()
    assert validation.manifest is manifest


@pytest.mark.parametrize(
    "missing",
    ["label", "description", "icon", "version", "tab", "entry", "api"],
)
def test_validate_rejects_missing_field_and_names_it(missing):
    """缺字段要被**逐条点名**（"manifest 坏"这种报错等于没说）。"""
    payload = {key: value for key, value in KANBAN_MANIFEST.items() if key != missing}
    validation = dash.validate(payload)
    assert validation.ok is False
    fields = {item.field for item in validation.issues}
    assert missing in fields
    detail = next(item.detail for item in validation.issues if item.field == missing)
    assert missing in detail


def test_validate_rejects_bad_tab_and_missing_router(tmp_path):
    directory = write_dashboard(
        tmp_path,
        manifest={**KANBAN_MANIFEST, "tab": {"path": "kanban", "position": "somewhere"}},
    )
    validation = dash.validate(dash.load_manifest(directory / "manifest.json"))
    assert validation.ok is False
    fields = {item.field for item in validation.issues}
    assert "tab.path" in fields
    assert "tab.position" in fields

    # api 模块里没有模块级 router：宿主没有东西可挂 → error
    directory = write_dashboard(tmp_path / "norouter", name="nokit")
    (directory / "plugin_api.py").write_text("def items():\n    return []\n", encoding="utf-8")
    validation = dash.validate(dash.load_manifest(directory / "manifest.json"))
    assert validation.ok is False
    assert "router" in " ".join(item.detail for item in validation.issues)


def test_routes_for_generates_api_plugin_paths(tmp_path):
    """``routes_for`` 的路径必须是 ``/api/plugins/<name>/…`` 形态（带挂载前缀）。"""
    directory = write_dashboard(tmp_path)
    manifest = dash.load_manifest(directory / "manifest.json")
    routes = dash.routes_for(manifest)
    assert [(item.method, item.path) for item in routes] == [
        ("GET", "/api/plugins/kanban/items"),
        ("POST", "/api/plugins/kanban/items/{item_id}/move"),
        ("DELETE", "/api/plugins/kanban/items/{item_id}"),
    ]
    for item in routes:
        assert item.path.startswith("/api/plugins/kanban/")
        assert item.endpoint == "plugin_api.py"


def test_routes_for_also_reads_add_api_route():
    manifest = dash.parse_manifest(KANBAN_MANIFEST)
    source = 'router.add_api_route("/health", handler)\n'
    routes = dash.routes_for(manifest, source=source)
    assert [item.path for item in routes] == ["/api/plugins/kanban/health"]


def test_describe_admits_mounting_is_not_implemented(tmp_path):
    """``describe`` 必须写清"真正挂载未实现"——不假装能挂。"""
    directory = write_dashboard(tmp_path)
    described = dash.describe(dash.load_manifest(directory / "manifest.json"))
    assert described["state"] == "partial"
    assert described["mounted"] is False
    assert described["route_prefix"] == "/api/plugins/kanban/"
    assert described["routes"]
    assert "没有" in described["reason"]
    assert any("include_router" in item for item in described["needed"])


def test_discover_finds_dashboard_plugins_and_keeps_errors(tmp_path):
    write_dashboard(tmp_path, "kanban")
    write_dashboard(tmp_path, "achievements", manifest={
        **KANBAN_MANIFEST,
        "name": "achievements",
        "label": "Achievements",
        "icon": "Star",
        "tab": {"path": "/achievements", "position": "after:analytics"},
    })
    broken = tmp_path / "broken" / "dashboard"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{not json", encoding="utf-8")

    result = dash.discover(tmp_path)
    assert sorted(item.name for item in result) == ["achievements", "kanban"]
    assert len(result) == 2
    assert result.by_name("kanban") is not None
    assert len(result.errors) == 1
    assert "JSON" in result.errors[0][1]


# ── 2. 13.3：import 时机（eager / lazy）─────────────────────────────────────


def _marker_plugin(root: Path, name: str, marker: Path, *, tools: bool) -> Path:
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
        f'    ctx.register_tool("{name}_tool", "演示", {{"type": "object"}}, '
        "lambda args: args)\n"
    )
    return write_plugin(root, name, manifest, code)


def test_import_phase_separates_eager_and_lazy(tmp_path):
    eager_marker = tmp_path / "eager.marker"
    lazy_marker = tmp_path / "lazy.marker"
    root = tmp_path / "plugins"
    _marker_plugin(root, "p-eager", eager_marker, tools=False)
    _marker_plugin(root, "p-lazy", lazy_marker, tools=True)

    result = plugins.discover([root])
    runtime = plugins.PluginRuntime(result.manifests, hooks=HookRegistry())
    assert runtime.plan() == {"p-eager": "eager", "p-lazy": "lazy"}

    runtime.start()
    assert eager_marker.exists(), "eager 插件在 start() 时就该 import"
    assert not lazy_marker.exists(), "lazy 插件在 start() 时**不许**被 import"

    tools = runtime.tools()
    assert lazy_marker.exists(), "首次要工具清单时必须把 lazy 插件 import 进来"
    assert "p-eager_tool" in tools and "p-lazy_tool" in tools
    # 幂等：再要一次不会重复 import（loaded 不增长）
    before = len(runtime.loaded)
    runtime.tools()
    assert len(runtime.loaded) == before
    described = runtime.describe()
    assert described["lazy_loaded"] is True
    assert described["wired"] is False  # 宿主接线是禁改区，如实报未接线


def test_load_records_import_phase(tmp_path):
    directory = write_plugin(
        tmp_path, "p-tools", "name: p-tools\nkind: tool\nprovides_tools:\n  - t\n",
        "def register(ctx):\n    pass\n",
    )
    loaded = plugins.load(plugins.load_manifest(directory / "plugin.yaml"))
    assert loaded.ok
    assert loaded.import_phase == "lazy"


# ── 3. 13.9：plugins install / search（仅本地目录）──────────────────────────

SAFE_PLUGIN_CODE = '''
"""最小插件：只注册一个工具。"""


def register(ctx):
    ctx.register_tool("demo_echo", "回显", {"type": "object"}, lambda args: args)
'''


def test_cli_plugins_install_from_local_directory(tmp_path, capsys):
    source = write_plugin(
        tmp_path / "src", "local-demo", "name: local-demo\nversion: 1.0.0\n",
        SAFE_PLUGIN_CODE,
    )
    assert _run(tmp_path, "plugins", "install", str(source)) == 0
    out = capsys.readouterr().out
    assert "已安装：local-demo" in out
    assert (tmp_path / "data" / "plugins" / "local-demo" / "plugin.yaml").is_file()
    # 装完就该被 list 看到（发现根包含 <data_dir>/plugins）
    assert _run(tmp_path, "plugins", "list") == 0
    assert "local-demo" in capsys.readouterr().out


def test_cli_plugins_install_rejects_remote_source(tmp_path, capsys):
    """远程源明确报未实现（本机无网络），而不是静默失败或假装装上了。"""
    assert _run(tmp_path, "plugins", "install", "https://example.com/demo.git") == 1
    err = capsys.readouterr().err
    assert "远程安装未实现" in err
    assert "本地目录" in err


def test_cli_plugins_search_only_searches_local(tmp_path, capsys):
    source = write_plugin(
        tmp_path / "src", "searchable-demo", "name: searchable-demo\n"
        "description: 本地搜索用的插件\n", SAFE_PLUGIN_CODE,
    )
    assert _run(tmp_path, "plugins", "install", str(source)) == 0
    capsys.readouterr()
    assert _run(tmp_path, "plugins", "search", "searchable") == 0
    out = capsys.readouterr().out
    assert "searchable-demo" in out
    assert "社区索引需要网络" in out  # 搜索范围必须自陈
    # --refresh 要联网：明确报未实现
    assert _run(tmp_path, "plugins", "search", "--refresh", "x") == 1
    assert "未实现" in capsys.readouterr().err


# ── 4. 13.10：plugins.json 门禁 + allow_tool_override ───────────────────────


def test_state_gate_reads_enabled_disabled_and_override(tmp_path):
    settings = settings_for(tmp_path / "data")
    state = plugins.load_state(settings)
    state.enable("granted", allow_tool_override=True)
    state.enable("plain")
    state.disable("blocked")

    assert state.gate("granted").allow_tool_override is True
    assert state.gate("granted").source == "plugin:granted"
    assert state.gate("plain").allow_tool_override is False
    assert state.gate("blocked").enabled is False
    assert "disabled" in state.gate("blocked").reason
    assert state.override_sources() == ("plugin:granted",)

    # 门禁快照要能被序列化（CLI --json 与 doctor 都吃这个形状）
    snapshot = state.gates()
    assert set(snapshot) == {"granted", "plain", "blocked"}
    assert all(set(item.as_dict()) == {
        "name", "enabled", "allow_tool_override", "source", "reason"
    } for item in snapshot.values())


def test_disabled_plugin_is_not_imported(tmp_path):
    """被 plugins.json 禁用的插件**不许 import**（import 完再丢掉等于照样执行代码）。"""
    marker = tmp_path / "blocked.marker"
    root = tmp_path / "plugins"
    code = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n"
        "\n"
        "\n"
        "def register(ctx):\n"
        "    pass\n"
    )
    directory = write_plugin(root, "p-blocked", "name: p-blocked\n", code)
    manifest = plugins.load_manifest(directory / "plugin.yaml")

    state = plugins.load_state(settings_for(tmp_path / "data"))
    state.disable("p-blocked")
    loaded = plugins.load(manifest, state=state)
    assert loaded.ok is False
    assert "被禁用" in loaded.error
    assert not marker.exists()

    # 启用之后能正常加载（门禁不是"永久拒绝"）
    state.enable("p-blocked")
    loaded = plugins.load(manifest, state=state)
    assert loaded.ok, loaded.error
    assert marker.exists()


def test_allow_tool_override_gate_downgrades_unauthorized_override(tmp_path):
    """未授权时 ``override=True`` 被降级成 False 并留一条 warning（不抛、不静默）。"""
    pushed: list[bool] = []

    class Sink:
        def register(self, tools, executor, source=None, override=False):
            pushed.append(override)

    manifest = plugins.PluginManifest(name="p", kind="tool")
    blocked = plugins.PluginContext(manifest, tool_sink=Sink())
    tool = blocked.register_tool("demo", "演示", None, lambda args: args, override=True)
    assert tool.override is False
    assert pushed[-1] is False
    assert any("allow_tool_override" in item for item in blocked.warnings)
    assert "register_tool" in blocked.capabilities()["landed"]

    granted = plugins.PluginContext(manifest, tool_sink=Sink(), allow_tool_override=True)
    tool = granted.register_tool("demo", "演示", None, lambda args: args, override=True)
    assert tool.override is True
    assert pushed[-1] is True
    assert granted.warnings == []


def test_override_grants_come_from_plugins_json(tmp_path):
    """端到端：``plugins.json`` 里的授权位 → 门禁 → 上下文里的 override 有效值。"""
    settings = settings_for(tmp_path / "data")
    state = plugins.load_state(settings)
    state.enable("p-granted", allow_tool_override=True)

    directory = write_plugin(
        tmp_path / "plugins", "p-granted", "name: p-granted\nkind: tool\n",
        "def register(ctx):\n"
        "    ctx.register_tool('shadow', '覆盖内置', None, lambda a: a, override=True)\n",
    )
    manifest = plugins.load_manifest(directory / "plugin.yaml")
    assert state.gate("p-granted").allow_tool_override is True
    loaded = plugins.load(manifest, state=state)
    assert loaded.ok, loaded.error
    assert loaded.context is not None
    assert loaded.context.tools["shadow"].override is True


# ── 13.4：有真实落点的能力（register_platform / dashboard_page）──────────────


def test_register_platform_lands_in_adapter_pool():
    from app.platforms import ConsoleAdapter

    pool: dict[str, object] = {}
    context = plugins.PluginContext(
        plugins.PluginManifest(name="p", kind="platform"), platform_sink=pool
    )
    adapter = ConsoleAdapter(name="plugin-console")
    assert context.register_platform(adapter) == "plugin-console"
    assert pool["plugin-console"] is adapter
    assert "register_platform" in context.capabilities()["landed"]


def test_register_platform_rejects_non_adapter():
    context = plugins.PluginContext(plugins.PluginManifest(name="p"))
    with pytest.raises(plugins.PluginError) as excinfo:
        context.register_platform(object(), name="junk")
    assert "send_text" in str(excinfo.value)


def test_register_platform_handler_wraps_adapter_on_message():
    """入站处理器包在适配器的边缘 hook 上；插件抛异常不影响后续 hook。"""
    from app.platforms import ConsoleAdapter

    adapter = ConsoleAdapter(name="plugin-console")
    seen: list[tuple[str, str]] = []
    context = plugins.PluginContext(
        plugins.PluginManifest(name="p", kind="platform"),
        platform_sink={"plugin-console": adapter},
    )

    def handler(channel: str, text: str, *, user_id: str = "") -> None:
        seen.append((channel, text))
        raise RuntimeError("插件处理器炸了")

    context.register_platform_handler("plugin-console", handler)
    adapter.on_message("chan", "hi", user_id="u1")  # 不抛：边缘 hook 必须隔离
    assert seen == [("chan", "hi")]
    assert any("插件处理器炸了" in item for item in context.errors)
    assert "register_platform_handler" in context.capabilities()["landed"]


def test_register_dashboard_page_uses_contract_and_admits_partial():
    context = plugins.PluginContext(plugins.PluginManifest(name="p", kind="dashboard"))
    described = context.register_dashboard_page(
        label="Kanban",
        description="board",
        icon="Package",
        version="1.0.0",
        tab={"path": "/kanban", "position": "after:skills"},
        entry="dist/index.js",
        api="plugin_api.py",
    )
    assert described["state"] == "partial"
    assert described["mounted"] is False
    assert described["route_prefix"] == "/api/plugins/p/"
    assert context.dashboard_pages == [described]
    assert "register_dashboard_page" in context.capabilities()["landed"]

    with pytest.raises(plugins.PluginError) as excinfo:
        context.register_dashboard_page({"label": "缺很多字段"})
    assert "label" in str(excinfo.value) or "icon" in str(excinfo.value)
