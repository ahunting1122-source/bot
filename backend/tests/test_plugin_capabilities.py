"""`app/plugin_capabilities.py` 的契约测试（MATRIX 13.4：19 种 `register_*` 的落点）。

分四块：

1. **表本身**：19 条齐全、每条都有状态与原因、`unsupported` 的都写清缺哪一层、
   `wired` 比上一轮（`plugins.LANDED_CAPABILITIES` 的 9 条）多。
2. **签名不漂**：表里的签名由 `inspect.signature` 现取，测试用同一个函数复核。
3. **Hermes 侧 19/19**：15 个有对应物 + 4 个 Hermes 独有，一个不漏一个不重。
4. **落点真的通**：wired 的落到真实结构（工具表 / hook 表 / 适配器池 / 页面登记表），
   exposed 的能登记但明确"宿主没消费"，unsupported 的抛异常且带缺层说明。
"""

from __future__ import annotations

import pytest

from app import plugin_capabilities as caps
from app import plugins
from app.hooks import HookRegistry
from app.plugin_dashboard import PageRegistry
from app.plugins import PluginContext, PluginManifest


@pytest.fixture(autouse=True)
def _isolate_page_registry():
    """每个用例都从干净的页面登记表开始（它是进程级对象）。"""
    from app import plugin_dashboard

    plugin_dashboard.PAGES.clear()
    yield
    plugin_dashboard.PAGES.clear()


def context(**kwargs) -> PluginContext:
    """造一个插件上下文（默认带一个全新的 hook 注册表，避免串到全局）。"""
    kwargs.setdefault("hooks", HookRegistry())
    return PluginContext(PluginManifest(name="p-demo", kind="tool"), **kwargs)


# ── 1. 表本身 ───────────────────────────────────────────────────────────────


def test_table_has_exactly_nineteen_and_matches_plugin_context():
    """19 条，且名字集合与 `PluginContext` 上的方法集合**完全一致**。

    一致性不是洁癖：`orbit_status == wired` 的断言要靠 ``getattr(PluginContext, name)``
    真的存在才成立，名字对不上就会变成一句空话。
    """
    assert len(caps.CAPABILITIES) == 19
    names = [item.name for item in caps.CAPABILITIES]
    assert len(set(names)) == 19, "能力名不许重复"
    assert set(names) == set(plugins.PLUGIN_CAPABILITIES)


def test_every_capability_has_status_and_reason():
    """每条都必须有状态、落点/缺层说明、以及一句能行动的原因。"""
    for item in caps.CAPABILITIES:
        assert item.orbit_status in caps.CAPABILITY_STATUSES, item.name
        assert item.reason.strip(), f"{item.name} 没写原因"
        assert item.orbit_landing.strip(), f"{item.name} 没写落点或缺哪一层"
        assert len(item.reason) >= 20, f"{item.name} 的原因太短，像是占位"
        assert item.as_dict()["orbit_status"] == item.orbit_status


def test_wired_count_beats_previous_round():
    """`wired` 必须比上一轮多（上一轮 = `plugins.LANDED_CAPABILITIES` 的 9 条）。"""
    summary = caps.summary()
    legacy = len(plugins.LANDED_CAPABILITIES)
    assert legacy == 9, "上一轮快照变了，这个断言的前提要跟着改"
    assert summary["wired"] > legacy, summary
    assert summary["wired"] + summary["exposed"] + summary["unsupported"] == 19
    assert summary["total"] == 19
    # 升上来的条目必须能逐条点名（不是"数量变多了"这种含糊说法）
    upgraded = {item["name"]: item for item in caps.drift()["upgraded"]}
    assert "register_telegram_handler" in upgraded
    assert upgraded["register_telegram_handler"]["current"] == caps.WIRED
    assert upgraded["register_telegram_handler"]["legacy"] == caps.UNSUPPORTED


def test_unsupported_rows_name_the_missing_layer():
    """`unsupported` 的每一条都必须点名**缺哪一层**，并且不许用"暂不支持"糊过去。"""
    rows = caps.by_status(caps.UNSUPPORTED)
    assert rows, "至少要有一条 unsupported，否则这张表是在自夸"
    for item in rows:
        landing = item.orbit_landing
        assert landing.strip()
        # 判据：要么点到具体模块，要么点到具体缺失的层
        assert "app/" in landing or "缺" in landing, f"{item.name} 的缺层说明不够具体：{landing}"
        assert "暂不支持" not in landing
        assert "暂不支持" not in item.reason
    # 全表禁用这句废话（任务书明令）
    for item in (*caps.CAPABILITIES, *caps.HERMES_ONLY):
        assert "暂不支持" not in item.reason
        assert "暂不支持" not in item.orbit_landing


def test_exposed_rows_say_what_the_host_does_not_consume():
    """`exposed` 的每一条都要写清"宿主没有消费方"的那一层。"""
    rows = caps.by_status(caps.EXPOSED)
    assert rows
    for item in rows:
        assert "缺" in item.orbit_landing or "没有" in item.orbit_landing, item.name
        assert item.wired is False
        assert item.missing_layer


# ── 2. 签名不漂 ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("capability", [item.name for item in caps.CAPABILITIES])
def test_signature_matches_the_real_method(capability):
    """表里的签名 = `inspect.signature` 的结果（同一个函数生成，测试再复核）。"""
    item = caps.by_name(capability)
    assert item is not None
    assert item.signature == caps.normalize_signature(getattr(PluginContext, capability))
    assert item.signature.startswith(capability + "(")


# ── 3. Hermes 侧 19/19 ──────────────────────────────────────────────────────


def test_hermes_side_is_complete():
    """Hermes 真实存在的 19 种 `ctx.register_*` 必须被 100% 覆盖，且不重复。"""
    coverage = caps.hermes_coverage()
    assert coverage["complete"] is True
    assert coverage["hermes_total"] == 19
    assert len(set(coverage["names"])) == 19
    assert coverage["mapped"] == 15
    assert coverage["hermes_only"] == 4
    # 4 个 Hermes 独有的必须各自写清缺哪一层
    for row in coverage["missing_rows"]:
        assert row["missing"].strip()
    assert {row["hermes"] for row in coverage["missing_rows"]} == {
        "register_image_gen_provider",
        "register_video_gen_provider",
        "register_browser_provider",
        "register_middleware",
    }


def test_hermes_calls_are_recorded_for_mapped_rows():
    """有对应物的行要带 grep 证据；**3 个文档化 API** 没有字面调用点，必须自陈这一点。"""
    documented = {
        "register_skill",
        "register_auxiliary_task",
        "register_context_engine",
    }
    mapped = [item for item in caps.CAPABILITIES if item.hermes_names]
    assert mapped
    called = 0
    for item in mapped:
        assert item.hermes_signature.strip(), f"{item.name} 没记 Hermes 原文签名"
        if item.hermes_calls >= 1:
            called += 1
            continue
        assert item.name in documented, f"{item.name} 没有 Hermes 调用点证据"
        assert "文档化 API" in item.hermes_signature, f"{item.name} 没说明为什么没有调用点"
    assert called >= 11, f"带调用点证据的行太少：{called}"


def test_drift_keeps_the_legacy_snapshot_honest():
    """旧快照（9/10）与本表的差异必须被显式报出来，不许悄悄不一致。"""
    change = caps.drift()
    assert len(change["legacy"]["landed"]) == 9
    assert len(change["legacy"]["unsupported"]) == 10
    assert change["upgraded"], "本轮至少要把一条 unsupported 升上来"
    assert change["downgraded"] == [], "不许有降级：那是回归"
    assert "旧快照" in change["note"]


# ── 4. 落点真的通 ───────────────────────────────────────────────────────────


def test_dispatch_reaches_wired_landings():
    """wired 的能力走 dispatch 能落到真实结构上。"""
    ctx = context()
    tool = caps.dispatch("register_tool", ctx, "demo_echo", "回显", None, lambda args: args)
    assert tool.name == "demo_echo"
    assert ctx.tools["demo_echo"] is tool

    event = caps.dispatch("register_hook", ctx, "pre_tool_call", lambda payload: None)
    assert event == "pre_tool_call"
    assert ctx.hooks.hooks("pre_tool_call")

    command = caps.dispatch("register_command", ctx, "plugin-caps-cmd", lambda c, a: 0, "演示")
    try:
        from app.cli.spec import REGISTRY

        assert REGISTRY.get(command) is not None
    finally:
        from app.cli.spec import REGISTRY

        REGISTRY._commands.pop(command, None)  # noqa: SLF001 —— Registry 没给删除 API


def test_dispatch_refuses_unsupported_with_reason_and_missing_layer():
    """unsupported 的抛 `CapabilityUnavailable`，消息里必须有原因与缺层。"""
    ctx = context()
    with pytest.raises(caps.CapabilityUnavailable) as excinfo:
        caps.dispatch("register_cron_provider", ctx, object())
    error = excinfo.value
    assert error.capability == "register_cron_provider"
    assert error.reason.strip()
    assert error.missing_layer.strip()
    assert "app/cron.py" in str(error)
    assert "没有落点" in str(error)


def test_dispatch_rejects_unknown_capability():
    ctx = context()
    with pytest.raises(caps.CapabilityUnavailable) as excinfo:
        caps.dispatch("register_teleporter", ctx)
    assert "不在能力面清单里" in str(excinfo.value)


def test_telegram_handler_lands_on_adapter_edge_hook():
    """`register_telegram_handler` 落到 telegram 适配器的 on_message 边缘 hook。"""
    from app.platforms import ConsoleAdapter

    adapter = ConsoleAdapter(name="telegram")
    seen: list[str] = []
    ctx = context(platform_sink={"telegram": adapter})

    def handler(channel: str, text: str, *, user_id: str = "") -> None:
        seen.append(text)
        raise RuntimeError("插件处理器炸了")

    assert caps.wire_telegram_handler(ctx, handler) == "telegram"
    adapter.on_message("chan", "hi", user_id="u1")  # 不抛：边缘 hook 必须隔离
    assert seen == ["hi"]
    assert any("插件处理器炸了" in item for item in ctx.errors)
    assert "register_telegram_handler" in ctx.used
    assert caps.status_of("register_telegram_handler") == caps.WIRED


def test_dashboard_page_lands_in_the_page_registry():
    """`register_dashboard_page` 落到 `plugin_dashboard.PageRegistry`（真实登记表）。"""
    registry = PageRegistry()
    ctx = context(page_sink=registry)
    described = caps.wire_dashboard_page(
        ctx,
        label="Kanban",
        description="board",
        icon="Package",
        version="1.0.0",
        tab={"path": "/kanban", "position": "after:skills"},
        entry="dist/index.js",
        api="plugin_api.py",
    )
    assert registry.names() == ["p-demo"]
    assert registry.by_name("p-demo") is described
    snapshot = registry.as_dict()
    assert snapshot["registered"] == 1
    assert snapshot["mounted"] == 0, "没有挂载点，不许谎称挂上了"
    assert snapshot["pending_mount"] == ["p-demo"]
    assert "app/main.py" in snapshot["reason"]


def test_exposed_capabilities_register_but_admit_no_consumer():
    """exposed 的三种：能登记（查得到），但状态明确写着宿主没有消费方。

    ``register_web_provider`` 本轮已经升到 wired（宿主表 ``EXTRA_PROVIDERS`` 真的被
    ``build_providers`` 读到），所以它不在这一组里——证据见
    ``tests/test_plugin_host.py::test_web_provider_lands_in_the_host_table_and_is_actually_used``。
    """
    ctx = context()

    class Provider:
        id = "demo-provider"

    assert caps.expose_memory_provider(ctx, Provider()) == "demo-provider"
    assert caps.expose_slack_action_handler(ctx, "btn_ok", lambda payload: None) == "btn_ok"
    assert caps.expose_slash_command(ctx, "/Demo Cmd", lambda args: None) == "demo-cmd"

    ledger = ctx.capabilities()["ledger"]
    assert ledger["register_memory_provider"] == 1
    assert ledger["register_slack_action_handler"] == 1
    assert ledger["register_slash_command"] == 1
    for name in (
        "register_memory_provider",
        "register_slack_action_handler",
        "register_slash_command",
    ):
        assert caps.status_of(name) == caps.EXPOSED, name
        assert "缺" in caps.landing_of(name), name


def test_web_provider_graduated_to_wired_with_a_real_host_table():
    """``register_web_provider`` 从 exposed 升到 wired：落点是宿主的模块级 provider 表。"""
    from app import web_providers

    class Provider:
        name = "demo-web"
        requires_key = False

        def available(self, settings=None) -> bool:
            return True

        async def search(self, query, *, count=5, freshness=None):
            return []

    ctx = context()
    try:
        assert caps.expose_web_provider(ctx, Provider()) == "demo-web"
        assert caps.status_of("register_web_provider") == caps.WIRED
        assert "EXTRA_PROVIDERS" in caps.landing_of("register_web_provider")
        # 宿主真的会读它（这就是"wired"的判据，不是"API 存在"）
        assert "demo-web" in [item.name for item in web_providers.build_providers(None)]
    finally:
        web_providers.unregister_provider("demo-web")


def test_slash_command_parsing_entry_works():
    """exposed 的斜杠命令有真实解析入口（缺的只是"谁来调"）。"""
    ctx = context()
    caps.expose_slash_command(ctx, "lcm", lambda args: None, description="上下文压缩")
    assert caps.parse_slash_command(ctx, "/lcm now") == ("lcm", "now")
    assert caps.parse_slash_command(ctx, "/lcm") == ("lcm", "")
    assert caps.parse_slash_command(ctx, "/unknown") is None
    assert caps.parse_slash_command(ctx, "lcm") is None


def test_observability_exporter_bridges_onto_the_hook_registry():
    """exporter 的落点是真实的 HookRegistry：19 个事件都挂上了，emit 真能送到。"""
    received: list[dict] = []

    class Exporter:
        name = "demo-exporter"

        def record(self, payload: dict) -> None:
            received.append(payload)

    ctx = context()
    assert caps.attach_observability_exporter(ctx, Exporter()) == "demo-exporter"
    from app.hooks import ALL_EVENTS

    assert set(ctx.hooks.events()) == set(ALL_EVENTS)
    ctx.hooks.emit("pre_tool_call", {"tool": "run_command"})
    assert received and received[0]["tool"] == "run_command"
    # 状态仍是 exposed：发射点未接线，主循环不会 emit
    assert caps.status_of("register_observability_exporter") == caps.EXPOSED


def test_middleware_kinds_map_to_hook_pairs():
    """`register_middleware` 降级挂到 pre/post hook；未知 kind 当场报错。"""
    seen: list[dict] = []
    ctx = context()
    assert caps.attach_middleware(ctx, "tool_execution", lambda payload: seen.append(payload)) == (
        "tool_execution"
    )
    assert ctx.hooks.hooks("pre_tool_call")
    assert ctx.hooks.hooks("post_tool_call")
    ctx.hooks.emit("pre_tool_call", {"tool": "x"})
    assert seen == [{"tool": "x"}]
    with pytest.raises(plugins.PluginError) as excinfo:
        caps.attach_middleware(ctx, "teleport_execution", lambda payload: None)
    assert "未知 middleware kind" in str(excinfo.value)
    assert caps.MIDDLEWARE_EVENTS["llm_execution"] == ("pre_llm_call", "post_llm_call")


def test_declared_status_translates_manifest_capabilities():
    """manifest 推导出来的能力名能翻译成状态行（`plugins info` 用的就是它）。"""
    rows = caps.declared_status(
        ["register_tool", "register_web_provider", "register_browser_provider", "register_nope"]
    )
    by_name = {row["name"]: row for row in rows}
    assert by_name["register_tool"]["status"] == caps.WIRED
    assert by_name["register_web_provider"]["status"] == caps.WIRED  # 本轮升上来的
    assert by_name["register_browser_provider"]["status"] == caps.UNSUPPORTED
    assert by_name["register_nope"]["status"] == "unknown"
    assert "不在 Orbit 的能力面里" in by_name["register_nope"]["landing"]


def test_host_surfaces_name_the_consumer():
    """`wired` 必须有消费者可指：宿主面表里每一条都要写清"谁在读它"。"""
    assert caps.HOST_SURFACES
    for key, detail in caps.HOST_SURFACES.items():
        assert detail.strip(), key
    for key in (
        "tool_sink",
        "hook_registry",
        "platform_sink",
        "cli_registry",
        "page_registry",
        "plugin_routes",
    ):
        assert not caps.HOST_SURFACES[key].startswith("（不存在）"), key
    # 这几处确实还没有消费方：表里必须自陈，不许含糊
    for key in ("memory_provider_registry", "web_provider_registry"):
        assert caps.HOST_SURFACES[key].startswith("（不存在）"), key
    # 斜杠命令：入口就位、调用方缺——必须写成"缺"，不能写成已接线
    assert caps.HOST_SURFACES["slash_dispatcher"].startswith("（入口就位、调用方缺）")
