"""MATRIX 13.4 第三轮：``exposed`` → ``wired`` 的**证据**（宿主真的调到了）。

## 这份文件要证明的那件事

上一轮把 4 条 ``exposed`` 能力的"宿主侧入口"都建好了，但每一条都还差**上游那一步**
（chat.py 的输入循环、装配处的 build_router、主循环的发射点、dashboard_api 的 webhook）。
本轮在**宿主自己的文件里**把能补的两步补上了，于是那两条从 ``exposed`` 变成 ``wired``：

| 能力 | 补的那一层 | 谁真的调它 |
|---|---|---|
| ``register_slack_action_handler`` | ``PluginHost.mount_routes`` 的 ``POST /api/plugin/actions``（解析 Block Kit 的 ``actions[].action_id``） | HTTP 请求 → ``dispatch_action`` → 插件回调 |
| ``register_slash_command`` | 同一条路由表里的 ``POST /api/plugin/slash`` | HTTP 请求 → ``dispatch_slash_async`` → 插件 handler |

剩下两条仍 ``exposed``，缺的那一步不在本模块，但**宿主侧取用入口**补齐了：
``PluginHost.memory_provider_for(name)`` 与 ``PluginHost.exporters_for(event)``。

## 判据

"宿主真的调到"不是"表里有这个键"，而是：请求发出去 → 插件里那个函数**真的跑了一次**
→ 它的返回值回到了调用方手里。所以每条用例都走 TestClient / 真实 host 方法，
不用替身。
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import memory_providers, plugins
from app import plugin_capabilities as caps
from app.config import Settings
from app.hooks import HookRegistry, UnknownHookEvent
from app.plugin_host import ACTION_ROUTES, PluginHost, unregister_action_route

# ── 夹具：一个把四条 exposed 能力各登记一次的插件 ────────────────────────────

#: 插件代码：记忆 provider / exporter / slack 动作 / 斜杠命令（同步 + 异步各一条）。
#: 全部走 `app.plugin_capabilities` 的落点函数（`PluginContext` 上的同名方法仍是旧行为）。
PLUGIN_CODE = '''
"""演示插件：13.4 四条 exposed 能力各登记一次。"""
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
        return {"name": self.name, "available": True}


class Exporter:
    name = "plugin-exporter"

    def __init__(self, path):
        self.path = path

    def record(self, payload):
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\\n")


async def _async_slash(rest):
    return f"异步插件收到：{rest}"


def register(ctx):
    expose_memory_provider(ctx, Memory(), provider_id="plugin-mem")
    attach_observability_exporter(ctx, Exporter(str(ctx.data_dir / "export.jsonl")))
    expose_slack_action_handler(ctx, "btn_ok", lambda payload: {"handled": True, "got": payload})
    expose_slash_command(ctx, "demo13", lambda rest: f"插件收到：{rest}", description="演示")
    expose_slash_command(ctx, "slow13", _async_slash, description="异步演示")
'''


def write_plugin(root: Path, name: str, manifest: str, code: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "plugin.yaml").write_text(manifest, encoding="utf-8")
    (directory / "__init__.py").write_text(code, encoding="utf-8")
    return directory


@pytest.fixture()
def wired_host(tmp_path):
    """一个真的加载了演示插件的宿主（每个用例独立：动作路由表要清干净）。"""
    root = tmp_path / "plugins"
    write_plugin(root, "p-wired", "name: p-wired\nkind: tool\n", PLUGIN_CODE)
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    host = PluginHost.from_settings(
        settings, roots=[root], hook_sink=HookRegistry()
    )
    host.start()
    try:
        yield host
    finally:
        unregister_action_route("btn_ok")


@pytest.fixture()
def http_client(wired_host):
    """把宿主挂到一个真 FastAPI app 上（``attach_to_app`` = main.py 要粘的那一行）。"""
    application = FastAPI(title="orbit-capability-wiring")
    wired_host.attach_to_app(application)
    return wired_host, TestClient(application)


# ── 1. memory_provider：按名取到的是插件那一个 ──────────────────────────────


def test_memory_provider_for_returns_the_plugin_instance(wired_host):
    """``memory_provider_for(name)`` 取到的**就是插件登记的那个对象**，而且能交给路由器。

    判据不是"字典里有键"：取出来的 provider 交给 `build_router` 之后，`save()` 的
    `written` 里必须出现它——那才证明"装配层一行就能用上"。
    """
    import asyncio

    collected = wired_host.memory_providers()
    assert list(collected) == ["plugin-mem"]
    assert wired_host.memory_provider_for("plugin-mem") is collected["plugin-mem"]
    # 查不到返回 None（插件可能已被卸载，这时调用方该退回内置 provider，不该炸）
    assert wired_host.memory_provider_for("nope") is None
    assert wired_host.memory_provider_for("") is None

    picked = wired_host.memory_provider_for("plugin-mem")
    router = memory_providers.build_router(providers=[picked], data_dir=None)
    assert router.active() is picked
    outcome = asyncio.run(router.save("note", "插件记忆"))
    assert outcome["written"] == ["plugin-mem"], outcome


# ── 2. exporter：按事件取，且事件真的流到 record() ─────────────────────────


def test_exporters_for_filters_by_event(wired_host):
    """``exporters_for(event)`` 只回**挂在这个事件上**的 exporter。"""
    everything = wired_host.exporters()
    assert list(everything) == ["plugin-exporter"]
    # 真实落点：exporter 被挂在 19 个事件上，所以每个事件都能取到它
    for event in ("pre_tool_call", "post_llm_call", "on_session_start"):
        assert wired_host.exporters_for(event) == everything, event
    # 事件名走 normalize_event：拼错必须当场炸，而不是回一个空字典让人以为"没有 exporter"
    with pytest.raises(UnknownHookEvent):
        wired_host.exporters_for("pre_tool_cal")


def test_exporters_for_excludes_an_exporter_bound_to_another_event(wired_host):
    """过滤**真的在过滤**：只挂了一个事件的 exporter，不会出现在别的事件里。

    这条用一份手搭的台账（而不是插件注册路径）来证明"按 events 过滤"这件事本身：
    真实路径下 exporter 总是挂满 19 个事件，光看它永远看不出过滤有没有生效。
    """
    only_one = SimpleNamespace(name="narrow")
    ledger = {
        "register_observability_exporter": [
            {"name": "narrow", "exporter": only_one, "events": ["pre_tool_call"]}
        ]
    }
    wired_host.runtime = SimpleNamespace(
        loaded=[SimpleNamespace(context=SimpleNamespace(capability_ledger=ledger))]
    )
    assert wired_host.exporters_for("pre_tool_call") == {"narrow": only_one}
    assert wired_host.exporters_for("post_tool_call") == {}


# ── 3. slack 动作：HTTP 入口真的把按钮点击送到插件回调 ──────────────────────


def test_block_kit_route_reaches_the_plugin_callback(http_client):
    """Slack Block Kit 整包进来 → 解析 ``actions[].action_id`` → 插件 lambda 真的跑一次。

    这是 ``register_slack_action_handler`` 从 exposed 升到 wired 的**全部证据**：
    此前 `app/dashboard_api.py` 的 webhook 只把载荷落盘，没人解析这一段。
    """
    host, client = http_client
    assert "btn_ok" in ACTION_ROUTES
    response = client.post(
        "/api/plugin/actions",
        json={
            "type": "block_actions",
            "actions": [{"action_id": "btn_ok", "value": "yes", "block_id": "b1"}],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["handled"][0]["action_id"] == "btn_ok"
    assert body["handled"][0]["outcome"]["handled"] is True
    assert body["handled"][0]["outcome"]["got"]["value"] == "yes"


def test_block_kit_route_ignores_an_unregistered_action(http_client):
    """未登记的 action_id 返回 handled=false，**不是 500**（否则 Slack 会不停重投）。"""
    _host, client = http_client
    response = client.post(
        "/api/plugin/actions", json={"actions": [{"action_id": "btn_gone"}]}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["handled"][0] == {"action_id": "btn_gone", "handled": False, "outcome": None}


def test_explicit_action_route_by_id(http_client):
    """显式 id 的那条路由（不解析 Block Kit）：``POST /api/plugin/actions/{action_id}``。"""
    _host, client = http_client
    response = client.post("/api/plugin/actions/btn_ok", json={"value": "from-path"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["action_id"] == "btn_ok"
    assert body["outcome"]["got"] == {"value": "from-path"}


# ── 4. 斜杠命令：HTTP 入口真的执行插件 handler ──────────────────────────────


def test_inbound_slash_route_executes_the_plugin_handler(http_client):
    """``POST /api/plugin/slash`` 真的调 handler，返回值原样带回。

    这是 ``register_slash_command`` 升到 wired 的证据：此前没有任何代码调
    ``dispatch_slash``（chat.py 的那一行归装配层），现在宿主自己的入站路由就是调用方。
    """
    _host, client = http_client
    hit = client.post("/api/plugin/slash", json={"text": "/demo13 你好"})
    assert hit.status_code == 200, hit.text
    body = hit.json()
    assert body == {"handled": True, "name": "demo13", "outcome": "插件收到：你好"}

    # 不是插件命令：返回 handled=false，调用方把这行当普通消息处理（不吞输入）
    miss = client.post("/api/plugin/slash", json={"text": "普通一句话"})
    assert miss.json()["handled"] is False
    unknown = client.post("/api/plugin/slash", json={"text": "/nope"})
    assert unknown.json()["handled"] is False


def test_async_slash_handler_works_through_the_async_entry(http_client):
    """异步 handler 走 ``dispatch_slash_async``：能 await，不会因为"事件循环在跑"而报错。"""
    _host, client = http_client
    response = client.post("/api/plugin/slash", json={"text": "/slow13 慢慢来"})
    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "异步插件收到：慢慢来"


def test_dispatch_slash_is_the_interface_chat_py_will_call(wired_host):
    """模拟 ``app/cli/commands/chat.py`` 输入循环里的那一行（接口已冻结）。

    chat.py 那边要写的就是：

        outcome = host.dispatch_slash(line)
        if outcome is not None:  # 插件命令已处理，不要再发给 agent
            continue

    这条用例证明：命令能分发、非命令返回 ``None``（**不吞输入**）。
    """
    host = wired_host
    assert host.dispatch_slash("/demo13 你好") == ("demo13", "插件收到：你好")
    assert host.dispatch_slash("这是一句普通的话") is None
    assert host.dispatch_slash("/not-registered") is None
    # 返回值形状就是 (name, outcome)：调用方拿 name 做回显、拿 outcome 决定怎么渲染
    name, outcome = host.dispatch_slash("/demo13 x") or ("", "")
    assert name == "demo13" and outcome == "插件收到：x"


# ── 5. 路由挂载本身：幂等 + 非 FastAPI app 不炸 ─────────────────────────────


def test_routes_are_mounted_once_and_reported(wired_host):
    """挂载幂等，且 ``describe()`` 里能看到挂了哪几条（不靠猜）。"""
    application = FastAPI()
    first = wired_host.mount_routes(application)
    assert len(first["routes"]) == 3
    second = wired_host.mount_routes(application)
    assert second["routes"] == first["routes"]
    assert "幂等" in second["skipped"]
    assert wired_host.describe()["routes"] == first["routes"]
    paths = {route.path for route in application.routes}
    assert "/api/plugin/actions" in paths
    assert "/api/plugin/slash" in paths


def test_mount_routes_is_skipped_for_a_non_fastapi_app(wired_host):
    """不是 FastAPI app（例如测试替身）时**如实跳过**，而不是把宿主起崩。"""

    class FakeApp:
        title = "not-fastapi"

        def __init__(self) -> None:
            self.state = SimpleNamespace()

        def include_router(self, router: Any) -> None:
            return None

    report = wired_host.attach_to_app(FakeApp())
    assert report["routes"]["routes"] == []
    assert "不是 FastAPI app" in report["routes"]["skipped"]
    assert wired_host.describe()["wired_into_main"] is True


# ── 6. 分布：13 / 2 / 4，且每条新 wired 都能指出宿主入口 ────────────────────


def test_capability_distribution_moved_to_13_wired():
    """本轮：wired 11 → 13，exposed 4 → 2，unsupported 4 不变（合计仍 19）。"""
    summary = caps.summary()
    assert summary["total"] == 19
    assert summary["wired"] == 13, summary
    assert summary["exposed"] == 2, summary
    assert summary["unsupported"] == 4, summary
    assert summary["wired"] + summary["exposed"] + summary["unsupported"] == 19
    # 仍 exposed 的正好是"缺的那一步不在本模块"的两条
    assert {item.name for item in caps.by_status(caps.EXPOSED)} == {
        "register_memory_provider",
        "register_observability_exporter",
    }


@pytest.mark.parametrize(
    "capability",
    ["register_slack_action_handler", "register_slash_command"],
)
def test_newly_wired_rows_point_at_a_real_host_entry(capability):
    """新升上来的两条：状态是 wired、接线点指向真实模块、宿主方法真的存在。"""
    item = caps.by_name(capability)
    assert item is not None and item.wired
    assert item.wired_by, f"{capability} 没有接线点证据"
    assert any(".py::" in entry for entry in item.wired_by), item.wired_by
    assert item.host_entry.startswith("app/plugin_host.py::"), item.host_entry
    method = item.host_entry.split("::")[-1].split(".")[-1]
    assert callable(getattr(PluginHost, method, None)), f"{capability} 的宿主入口不存在"


def test_the_two_still_exposed_rows_say_which_layer_is_missing():
    """剩下两条仍是 exposed：缺哪一层必须点名（不许写"暂不支持"）。"""
    for name in ("register_memory_provider", "register_observability_exporter"):
        item = caps.by_name(name)
        assert item is not None and not item.wired
        assert item.ready is True, "宿主侧入口已就位这件事不能因为没 wired 就不报"
        assert "缺" in item.orbit_landing, item.orbit_landing
        assert "暂不支持" not in item.orbit_landing
    # 两条缺的都是"别人那一步"，各自写清了具体文件/结构
    assert "build_router" in caps.landing_of("register_memory_provider")
    assert "WIRING_POINTS" in caps.landing_of("register_observability_exporter")


def test_hermes_coverage_is_still_19_of_19():
    """能力面推进不该打坏 Hermes 侧的 19/19 对账。"""
    coverage = caps.hermes_coverage()
    assert coverage["complete"] is True
    assert coverage["hermes_total"] == 19
    assert len(set(coverage["names"])) == 19
    assert json.dumps(coverage["names"], ensure_ascii=False)


def test_plugin_capabilities_are_importable_without_side_effects():
    """这张表是纯数据：导入不该拉起 FastAPI / 插件运行时（CLI 要零成本导入）。"""
    assert plugins.PLUGIN_CAPABILITIES  # 19 个方法名
    assert len(caps.CAPABILITIES) == 19
