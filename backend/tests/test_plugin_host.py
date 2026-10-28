"""插件宿主门面（MATRIX 13.3）与能力面推进（MATRIX 13.4）的契约测试。

## 这份测试要证明的那件事

上一轮 ``PluginRuntime.warmup()`` 存在、也返回了正确的报告，但**没有任何生产代码调它**，
所以"插件的工具真的进了 ToolRegistry、钩子真的在 hooks 里"这句话从未被验证过。
本文件把「接线」当动作演练：

1. ``PluginHost.start()`` 之后，插件注册的工具**真的**在 ``ToolRegistry.tools`` 里，
   并且能从注册表里**执行**（``registry.external[name]`` 就是插件 handler 的包装）；
2. 插件注册的钩子**真的**在宿主的 ``HookRegistry`` 里，``emit`` 一次插件函数就被调用；
3. ``describe()`` 报 ``host_ready=True`` / ``wired_into_main=False``，并给出**可粘贴**的
   那一行代码（含缩进）；``attach_to_app`` 之后 ``wired_into_main`` 才变 ``True``；
4. 二次 ``start()`` 幂等：不重复 import、不重复注册工具、不把同一个 hook 挂两遍；
5. ``register_web_provider`` 本轮从 exposed 升到 wired：宿主表真的被 ``build_providers``
   读到，且排在兜底 provider 之前（否则永远选不到）；
6. 斜杠命令的分发入口（``dispatch_slash``）真的会调插件 handler，未知命令返回 ``None``
   ——它现在仍是 exposed，缺的是 ``app/cli/commands/chat.py`` 的那一行调用。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import plugin_capabilities as caps
from app import plugin_dashboard as dash
from app import plugins, web_providers
from app.config import Settings
from app.hooks import HookRegistry
from app.plugin_host import MAIN_WIRING_ANCHOR, MAIN_WIRING_CODE, PluginHost
from app.storage import Store
from app.tools import ToolRegistry

# ── 夹具 ────────────────────────────────────────────────────────────────────

#: 一个"注册工具 + 注册钩子 + 注册斜杠命令"的插件：三件事都要在 start() 之后可验证。
PLUGIN_CODE = '''
"""演示插件：工具 + 钩子 + 斜杠命令各一条。"""
from app.plugin_capabilities import expose_slash_command


def register(ctx):
    ctx.register_tool("demo_echo", "回显参数", {"type": "object"}, lambda args: args)
    ctx.register_hook("pre_tool_call", lambda payload: {"seen": payload.get("tool", "")})
    expose_slash_command(ctx, "demo", lambda rest: f"插件收到：{rest}", description="演示")
'''

#: lazy 插件：声明 provides_tools，所以 warmup() **不会** import 它。
LAZY_CODE = '''
def register(ctx):
    ctx.register_tool("lazy_echo", "懒加载工具", {"type": "object"}, lambda args: args)
'''


def write_plugin(root: Path, name: str, manifest: str, code: str) -> Path:
    """在 ``root/<name>/`` 落一个普通插件（plugin.yaml + ``__init__.py``）。"""
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.yaml").write_text(manifest, encoding="utf-8")
    (directory / "__init__.py").write_text(code, encoding="utf-8")
    return directory


def settings_for(data_dir: Path) -> Settings:
    settings = Settings(data_dir=data_dir, workspace=data_dir / "files")
    settings.prepare()
    return settings


def host_for(root: Path, *, tool_sink: ToolRegistry | None = None) -> PluginHost:
    """按生产口径装配一个宿主（发现 + 门禁 + 显式的 sink）。"""
    settings = settings_for(root.parent / "data")
    return PluginHost.from_settings(
        settings,
        roots=[root],
        tool_sink=tool_sink,
        hook_sink=HookRegistry(),
    )


def registry_for(settings: Settings) -> ToolRegistry:
    """真的 ToolRegistry（插件工具的落点就是它，不造替身）。"""
    return ToolRegistry(settings, Store(settings.data_dir / "app.sqlite"))


# ── 1. start() 之后工具真的在 ToolRegistry 里 ───────────────────────────────


def test_start_puts_plugin_tools_into_the_real_registry(tmp_path):
    """``start()`` 之后插件的工具在 ``ToolRegistry.tools`` 里，并且**真的能执行**。"""
    root = tmp_path / "plugins"
    write_plugin(root, "p-demo", "name: p-demo\nkind: tool\n", PLUGIN_CODE)
    settings = settings_for(tmp_path / "data")
    registry = registry_for(settings)

    host = PluginHost.from_settings(
        settings, roots=[root], tool_sink=registry, hook_sink=HookRegistry()
    )
    report = host.start()

    assert report["loaded"] == ["p-demo"]
    assert "demo_echo" in registry.tools, "插件工具没有进 ToolRegistry"
    assert registry.tools["demo_echo"].source == "plugin:p-demo"
    assert "demo_echo" in registry.external, "注册表里没有可执行体"
    # 不是"列进去了"就算：真的通过注册表的执行体跑一遍插件 handler
    outcome = asyncio.run(registry.external["demo_echo"]("demo_echo", {"a": 1}, "", ""))
    assert outcome == {"a": 1}
    assert "demo_echo" in report["wired"]["tools"]


def test_start_puts_plugin_hooks_into_the_host_registry(tmp_path):
    """钩子真的在宿主的注册表里，``emit`` 一次插件函数就被调用（不是"注册了但没人发"）。"""
    root = tmp_path / "plugins"
    write_plugin(root, "p-demo", "name: p-demo\nkind: tool\n", PLUGIN_CODE)
    settings = settings_for(tmp_path / "data")
    hooks = HookRegistry()
    host = PluginHost.from_settings(settings, roots=[root], hook_sink=hooks)
    host.start()

    assert hooks.hooks("pre_tool_call"), "插件钩子没有进宿主注册表"
    results = hooks.emit("pre_tool_call", {"tool": "run_command"})
    assert any(item.result == {"seen": "run_command"} for item in results)
    assert host.hooks()["pre_tool_call"] >= 1


def test_lazy_plugin_tools_arrive_when_tools_are_asked_for(tmp_path):
    """lazy 插件的工具在 ``host.tools()`` 之后才出现，出现时就**已经**在注册表里。"""
    root = tmp_path / "plugins"
    write_plugin(root, "p-lazy", "name: p-lazy\nkind: tool\nprovides_tools:\n  - t\n", LAZY_CODE)
    settings = settings_for(tmp_path / "data")
    registry = registry_for(settings)
    host = PluginHost.from_settings(
        settings, roots=[root], tool_sink=registry, hook_sink=HookRegistry()
    )
    report = host.start()
    assert report["pending_lazy"] == ["p-lazy"]
    assert "lazy_echo" not in registry.tools, "lazy 插件在 warmup 阶段不该被 import"

    assert "lazy_echo" in host.tools()
    assert "lazy_echo" in registry.tools, "lazy 插件加载后工具必须已经在注册表里"


# ── 2. describe()：host_ready 与 wired_into_main 是两件事 ───────────────────


def test_describe_reports_ready_but_not_wired_into_main(tmp_path):
    root = tmp_path / "plugins"
    write_plugin(root, "p-demo", "name: p-demo\nkind: tool\n", PLUGIN_CODE)
    host = host_for(root)
    host.start()

    described = host.describe()
    assert described["host_ready"] is True
    assert described["wired_into_main"] is False, "没人调过 attach_to_app，不许报已接线"
    assert described["attached_apps"] == []
    # 缺的那一行必须**能直接粘**：给位置、给代码、代码带缩进
    assert described["missing_at"] == MAIN_WIRING_ANCHOR
    assert described["missing_code"] == MAIN_WIRING_CODE
    assert described["missing_code"].startswith("    from app.plugin_host import PluginHost")
    assert "PluginHost.from_settings" in described["missing_code"]
    assert ".attach_to_app(app)" in described["missing_code"]
    joined = " ".join(described["missing_lines"])
    for anchor in ("app/main.py", "app/agent.py", "app/graph/nodes.py"):
        assert anchor in joined, anchor
    # 工具/hook 的现状要能在同一份快照里看到
    assert described["tools"] == ["demo_echo"]
    assert described["hooks"]["pre_tool_call"] >= 1


def test_attach_to_app_flips_wired_into_main(tmp_path):
    """``attach_to_app`` 就是 main.py 要粘的那一行：调过之后 ``wired_into_main`` 才为真。"""
    root = tmp_path / "plugins"
    write_plugin(root, "p-demo", "name: p-demo\nkind: tool\n", PLUGIN_CODE)
    host = host_for(root)

    class FakeApp:
        title = "orbit-test"

        def __init__(self) -> None:
            self.state = SimpleNamespace()
            self.routers: list[object] = []

        def include_router(self, router) -> None:
            self.routers.append(router)

    app = FakeApp()
    report = host.attach_to_app(app)

    assert report["started"]["loaded"] == ["p-demo"]
    assert app.state.plugin_host is host
    described = host.describe()
    assert described["wired_into_main"] is True
    assert described["attached_apps"] == ["orbit-test"]


# ── 3. 幂等 ─────────────────────────────────────────────────────────────────


def test_second_start_is_idempotent(tmp_path):
    """二次 ``start()`` 不重复 import、不重复注册工具、不把 hook 挂两遍。"""
    root = tmp_path / "plugins"
    write_plugin(root, "p-demo", "name: p-demo\nkind: tool\n", PLUGIN_CODE)
    settings = settings_for(tmp_path / "data")
    hooks = HookRegistry()
    registry = registry_for(settings)
    host = PluginHost.from_settings(
        settings, roots=[root], tool_sink=registry, hook_sink=hooks
    )

    host.start()
    hook_count = hooks.count()
    assert hook_count == 1

    again = host.start()
    assert again["loaded"] == [], "第二次不该再 import 任何插件"
    assert again["wired"]["pushed"] == [], "第二次不该再补推工具"
    assert again["wired"]["tools"] == ["demo_echo"], "已接线的工具要如实报（不是报 0）"
    assert hooks.count() == hook_count, "hook 被挂了第二遍"
    assert host.starts == 2
    assert len(registry.tools) == len(set(registry.tools)), "工具被重复注册"


def test_late_sink_gets_backfilled(tmp_path):
    """先 load 后给 sink（宿主晚到）：reconcile 把已有工具补推过去。"""
    root = tmp_path / "plugins"
    write_plugin(root, "p-demo", "name: p-demo\nkind: tool\n", PLUGIN_CODE)
    settings = settings_for(tmp_path / "data")
    runtime = plugins.PluginRuntime(plugins.discover([root]).manifests, hooks=HookRegistry())
    runtime.load_all()
    assert "demo_echo" not in {}, "前置条件：这时还没有任何 tool_sink"

    registry = registry_for(settings)
    host = PluginHost(runtime, hook_sink=HookRegistry())
    host.start(tool_sink=registry)
    assert "demo_echo" in registry.tools, "晚到的 sink 没有被补推"


# ── 4. 斜杠命令：分发入口真的调 handler（仍是 exposed，缺 chat 那一行）──────


def test_dispatch_slash_calls_the_plugin_handler(tmp_path):
    root = tmp_path / "plugins"
    write_plugin(root, "p-demo", "name: p-demo\nkind: tool\n", PLUGIN_CODE)
    host = host_for(root)
    host.start()

    assert host.dispatch_slash("/demo 你好") == ("demo", "插件收到：你好")
    # 不是插件命令就返回 None：chat 要把这行当普通消息发出去，不能吞输入
    assert host.dispatch_slash("/nope") is None
    assert host.dispatch_slash("你好") is None


def test_slash_command_is_still_exposed_and_says_which_line_is_missing():
    """斜杠命令仍是 exposed：缺的是 chat 输入循环里的一行调用（不是一层能力）。"""
    item = caps.by_name("register_slash_command")
    assert item is not None
    assert item.orbit_status == caps.EXPOSED
    assert "app/cli/commands/chat.py" in item.orbit_landing
    assert "dispatch_slash" in item.orbit_landing
    # 分发入口本身必须存在（否则"只差一行调用"这句话是假的）
    assert callable(PluginHost.dispatch_slash)
    assert callable(caps.resolve_slash_command)


# ── 5. 能力面推进：register_web_provider 从 exposed 到 wired ────────────────


class DemoProvider:
    """一个最小可用的搜索 provider（宿主表要求的契约：name / available / search）。"""

    name = "demo-search"
    requires_key = False

    def available(self, settings: object = None) -> bool:
        return True

    async def search(self, query: str, *, count: int = 5, freshness: str | None = None):
        return [web_providers.SearchResult(title="t", url="https://example.invalid", snippet=query)]


def test_web_provider_lands_in_the_host_table_and_is_actually_used():
    """注册 → 宿主表 → ``build_providers``/``select_provider`` 真的会选到它。"""
    settings = SimpleNamespace()
    context = plugins.PluginContext(plugins.PluginManifest(name="p-web", kind="tool"))
    try:
        assert caps.expose_web_provider(context, DemoProvider()) == "demo-search"
        # 证据一：宿主表里有它
        assert web_providers.EXTRA_PROVIDERS["demo-search"].name == "demo-search"
        # 证据二：build_providers 会带上它（app/web.py::WebTools 构造时读的就是这个）
        names = [provider.name for provider in web_providers.build_providers(settings)]
        assert "demo-search" in names
        # 证据三：它排在兜底 ddgs 之前——否则 ddgs.available() 恒真，永远轮不到它
        assert names.index("demo-search") < names.index(web_providers.FALLBACK_PROVIDER)
        # 证据四：真的会被选中（这是"宿主用到了"，不是"表里躺着"）
        assert web_providers.select_provider(settings).name == "demo-search"
    finally:
        web_providers.unregister_provider("demo-search")
    assert "demo-search" not in web_providers.EXTRA_PROVIDERS
    assert caps.by_name("register_web_provider").orbit_status == caps.WIRED


def test_web_provider_without_contract_is_refused():
    """缺 available()/search() 的 provider 当场报错，而不是静默躺进宿主表。"""
    context = plugins.PluginContext(plugins.PluginManifest(name="p-web", kind="tool"))
    with pytest.raises(ValueError):
        caps.expose_web_provider(context, SimpleNamespace(name="junk"))
    assert "junk" not in web_providers.EXTRA_PROVIDERS


# ── 6. 分布：wired 比上一轮多，且每条新 wired 都有"宿主真的用到"的证据 ─────


def test_wired_beats_the_previous_round_and_each_new_row_has_a_consumer():
    """上一轮是 wired 10 / exposed 5 / unsupported 4；本轮 wired 至少多一条。"""
    summary = caps.summary()
    assert summary["wired"] > 10, summary
    assert summary["wired"] + summary["exposed"] + summary["unsupported"] == 19
    # 新升上来的每条都必须写清是谁在消费（wired_by 不能空，且必须指向真实模块）
    for name in ("register_web_provider", "register_dashboard_page", "register_telegram_handler"):
        item = caps.by_name(name)
        assert item is not None and item.wired, name
        assert item.wired_by, f"{name} 没有接线点证据"
        assert any(".py::" in entry for entry in item.wired_by), item.wired_by
    # exposed / unsupported 每一条都要写清缺哪一层
    for item in caps.by_status(caps.EXPOSED):
        assert item.missing_layer.strip(), item.name
        assert "缺" in item.missing_layer, item.name
    for item in caps.by_status(caps.UNSUPPORTED):
        assert "缺" in item.missing_layer, item.name


def test_dashboard_page_registration_reaches_the_registry_and_the_mount_layer():
    """``register_dashboard_page`` 的落点是登记表，且挂载入口能把它变成真路由（见另一份测试）。"""
    item = caps.by_name("register_dashboard_page")
    assert item is not None and item.wired
    assert callable(dash.build_router) and callable(dash.mount)
    assert "PageRegistry" in item.orbit_landing


def test_manifest_of_the_demo_plugin_is_discovered(tmp_path):
    """前置条件复核：``discover`` 认得出这些插件（否则上面所有断言都是空转）。"""
    root = tmp_path / "plugins"
    write_plugin(root, "p-demo", "name: p-demo\nkind: tool\n", PLUGIN_CODE)
    result = plugins.discover([root])
    assert [manifest.name for manifest in result] == ["p-demo"]
    assert json.dumps(result.manifests[0].as_dict(), ensure_ascii=False)


# ── 7. 13.4 第二轮：4 条 exposed 能力的**宿主侧入口**（宿主真的能调到）────────
#
# 这一节只证明一件事：那 4 条暴露出来的能力，宿主里已经有一个**能被调到**的入口。
# 它**不**证明"装配层已经在用它们"——那几行分别在 app/memory_providers.py 的调用方、
# app/exporters.py 的池子合并、app/dashboard_api.py 的 webhook 收件路径与
# app/cli/commands/chat.py 的输入循环里。所以状态仍是 exposed，只多了一个 ready。

CAPABILITY_PLUGIN_CODE = '''
"""演示插件：13.4 第二轮的四条 exposed 能力各登记一次。"""
import json

from app.plugin_capabilities import (
    attach_observability_exporter,
    expose_memory_provider,
    expose_slack_action_handler,
    expose_slash_command,
)


class Memory:
    name = "plugin-mem"

    def available(self):
        return True

    async def save(self, key, value):
        return {"saved": True, "key": key, "value": value}

    async def search(self, query, limit=10):
        return []

    async def delete(self, key):
        return True

    async def list_keys(self):
        return []

    def describe(self):
        return {"name": self.name}


class Exporter:
    name = "plugin-exporter"

    def __init__(self, path):
        self.path = path

    def record(self, payload):
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\\n")


def register(ctx):
    expose_memory_provider(ctx, Memory(), provider_id="plugin-mem")
    attach_observability_exporter(ctx, Exporter(str(ctx.data_dir / "export.jsonl")))
    expose_slack_action_handler(ctx, "btn_ok", lambda payload: {"handled": True, "got": payload})
    expose_slash_command(ctx, "demo13", lambda rest: f"插件收到：{rest}")
'''


@pytest.fixture()
def capability_host(tmp_path):
    """一个真的加载了"四条能力各一条"插件的宿主（每个用例独立，动作路由表要清干净）。"""
    root = tmp_path / "plugins"
    write_plugin(root, "p-134", "name: p-134\nkind: tool\n", CAPABILITY_PLUGIN_CODE)
    host = host_for(root)
    host.start()
    try:
        yield host
    finally:
        from app.plugin_host import unregister_action_route

        unregister_action_route("btn_ok")


def test_slack_action_routes_reach_the_plugin_callback(capability_host):
    """`action_id` → 回调：宿主按 action_id 查到**并真的调用**插件处理器。

    这是 ``register_slack_action_handler`` 缺的那张表。判据不是"表里有键"，而是
    "调用之后插件的 lambda 真的跑了一次、返回值回到了调用方"。
    """
    from app import plugin_host as host_module

    host = capability_host
    assert "btn_ok" in host.action_routes, "插件登记的动作没有搬进宿主路由表"
    assert "btn_ok" in host_module.ACTION_ROUTES, "进程级路由表也要有（webhook 拿不到宿主实例）"
    assert host.describe()["action_routes"] == ["btn_ok"]

    outcome = host.dispatch_action("btn_ok", {"action_id": "btn_ok", "value": "yes"})
    assert outcome == {"handled": True, "got": {"action_id": "btn_ok", "value": "yes"}}

    # 未知 action_id 返回 None（而不是抛）：按钮可能来自已经卸载的插件
    assert host.dispatch_action("btn_gone") is None
    # 模块级入口同样能派发（gateway/webhook 那条路走的是它）
    assert host_module.dispatch_action("btn_ok", {"x": 1}) == {"handled": True, "got": {"x": 1}}


def test_action_route_conflict_is_refused_not_silently_overwritten():
    """两个插件抢同一个 action_id：当场报错（静默覆盖会让"点了没反应"查不出来）。"""
    from app.plugin_host import (
        ACTION_ROUTES,
        dispatch_action,
        register_action_route,
        unregister_action_route,
    )

    try:
        register_action_route("btn_conflict", lambda payload: "first")
        assert dispatch_action("btn_conflict") == "first"
        with pytest.raises(ValueError, match="已经有处理器"):
            register_action_route("btn_conflict", lambda payload: "second")
        assert dispatch_action("btn_conflict") == "first", "被拒的注册不许改掉原处理器"
        # 显式 override 才允许替换
        register_action_route("btn_conflict", lambda payload: "third", override=True)
        assert dispatch_action("btn_conflict") == "third"
    finally:
        unregister_action_route("btn_conflict")
    assert "btn_conflict" not in ACTION_ROUTES


def test_memory_providers_are_collected_and_router_accepts_them(capability_host):
    """`host.memory_providers()` 收得到，且**能直接交给** `build_router(providers=…)`。

    判据是"路由器真的调到了插件的 provider"：`router.save()` 的 `written` 里出现插件
    的 provider 名 —— 不是"字典里有个键"。
    """
    from app import memory_providers

    providers = capability_host.memory_providers()
    assert list(providers) == ["plugin-mem"]
    assert providers["plugin-mem"].available() is True

    router = memory_providers.build_router(
        providers=list(providers.values()), data_dir=None
    )
    assert router.active() is not None
    assert router.active().name == "plugin-mem"
    outcome = asyncio.run(router.save("note", "插件记忆"))
    assert outcome["ok"] is True
    assert outcome["written"] == ["plugin-mem"], outcome


def test_exporters_are_collected_and_receive_events(capability_host, tmp_path):
    """`host.exporters()` 收得到，且宿主 emit 一次事件**真的**流到插件的 record()。

    这里刻意走真实链路：`attach_observability_exporter` 把桥挂在宿主的 HookRegistry 上，
    `host.hook_sink.emit(...)` 触发桥 → 调 exporter.record → 写文件。文件内容就是证据。
    """
    from app import exporters

    collected = capability_host.exporters()
    assert list(collected) == ["plugin-exporter"]
    # 形状与 default_exporters() 的池子一致，装配层可以直接合并
    pool = {**exporters.default_exporters(tmp_path / "data"), **collected}
    assert "plugin-exporter" in pool

    capability_host.hook_sink.emit("pre_tool_call", {"tool": "run_command"})
    spool = capability_host.runtime.loaded[0].context.data_dir / "export.jsonl"
    assert spool.exists(), "事件没有流到 exporter（桥没挂上或发射没触发）"
    assert "run_command" in spool.read_text(encoding="utf-8")


def test_slash_dispatch_is_ready_and_still_exposed(capability_host):
    """斜杠命令：宿主真的能分发（返回值就是 handler 的输出），状态仍 exposed。"""
    host = capability_host
    assert host.dispatch_slash("/demo13 你好") == ("demo13", "插件收到：你好")
    readiness = host.describe()["capabilities"]
    row = readiness["register_slash_command"]
    assert row["ready"] is True
    assert "dispatch_slash" in row["host_entry"]
    assert "chat.py" in row["missing"], "缺的那一行必须点名文件"
    assert caps.status_of("register_slash_command") == caps.EXPOSED


def test_all_four_exposed_capabilities_report_a_ready_host_entry():
    """4 条 exposed 能力的宿主侧入口都在，且每条都能说出"还缺哪一行"。"""
    summary = caps.summary()
    assert summary["exposed"] == 4
    assert summary["ready"] == 4, summary
    entries = caps.host_entries()
    for name in (
        "register_slack_action_handler",
        "register_memory_provider",
        "register_observability_exporter",
        "register_slash_command",
    ):
        assert name in entries, name
        assert entries[name].startswith("app/plugin_host.py::"), entries[name]
        item = caps.by_name(name)
        assert item is not None and item.ready
        assert "缺" in item.orbit_landing, f"{name} 没写清还缺哪一层"
