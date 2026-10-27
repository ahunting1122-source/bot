"""`app/hooks.py` 的契约测试：事件表、优先级、异常隔离、同步/异步发射。

只测注册表本身——"事件有没有被真的接线"不在这里测（接线点在 `app/graph/*`、
`app/agent.py`、`app/summaries.py`，本轮禁改；:data:`app.hooks.WIRING_POINTS`
把该插的位置写成了文档，并由本文件断言它覆盖了必须落地的 5 个事件）。
"""

from __future__ import annotations

import asyncio

import pytest

from app.hooks import (
    ALL_EVENTS,
    EVENT_ALIASES,
    EVENT_EVIDENCE,
    HOOK_EVENTS,
    INFERRED_EVENTS,
    WIRING_POINTS,
    HookRegistry,
    UnknownHookEvent,
    normalize_event,
)


def test_seventeen_evidenced_events_and_two_inferred():
    """17 个实证事件 + 2 个推断候选；两者**不能混算**（不许把推断当实证）。"""
    assert len(HOOK_EVENTS) == 17
    assert len(set(HOOK_EVENTS)) == 17, "事件名有重复"
    assert len(INFERRED_EVENTS) == 2
    assert set(INFERRED_EVENTS) == {"post_compaction", "pre_verify"}
    assert not set(HOOK_EVENTS) & set(INFERRED_EVENTS)
    assert len(ALL_EVENTS) == 19
    for event in HOOK_EVENTS:
        assert EVENT_EVIDENCE[event] == "manifest", event
    for event in INFERRED_EVENTS:
        assert EVENT_EVIDENCE[event] == "inferred", event


def test_wiring_points_cover_the_five_required_events():
    """必须落地接口的 5 个事件都在接线表里，且指向具体文件。"""
    required = {"pre_tool_call", "post_tool_call", "session_start", "session_end", "pre_compaction"}
    assert set(WIRING_POINTS) == required
    for event, where in WIRING_POINTS.items():
        assert normalize_event(event) in ALL_EVENTS, event
        assert ".py::" in where, f"{event} 的接线点没写清文件与函数：{where}"


def test_aliases_normalize_to_canonical_names():
    """短名（任务书/内部文案）与 Hermes 实名都能用，但统一归一后再存。"""
    assert EVENT_ALIASES["session_start"] == "on_session_start"
    assert EVENT_ALIASES["pre_compaction"] == "on_pre_compress"
    assert normalize_event("session_end") == "on_session_end"
    assert normalize_event("pre_compaction") == "on_pre_compress"
    # 实名本身就是实名
    assert normalize_event("on_session_end") == "on_session_end"


def test_unknown_event_is_rejected_loudly():
    """未知事件必须抛——静默返回空列表就是"插件以为挂上了"的经典坑。"""
    registry = HookRegistry()
    with pytest.raises(UnknownHookEvent) as excinfo:
        registry.register("pre_whatever", lambda payload: None)
    assert "pre_whatever" in str(excinfo.value)
    assert "on_session_start" in str(excinfo.value), "报错里要列出可用事件"
    with pytest.raises(UnknownHookEvent):
        registry.emit("nope", {})


def test_register_and_emit_sync():
    registry = HookRegistry()
    seen: list[dict] = []
    registry.register("pre_tool_call", lambda payload: seen.append(payload) or "ok")
    results = registry.emit("pre_tool_call", {"tool": "local_read_document"})
    assert seen == [{"tool": "local_read_document"}]
    assert [item.result for item in results] == ["ok"]
    assert results[0].event == "pre_tool_call"
    assert registry.hooks("pre_tool_call"), "hooks() 要能列出已注册的名字"
    assert registry.events() == ["pre_tool_call"]


def test_priority_order_is_high_first_then_fifo():
    """数值大者先跑；同优先级按注册顺序（FIFO）——不靠 import 顺序。"""
    registry = HookRegistry()
    order: list[str] = []
    registry.register("post_tool_call", lambda payload: order.append("low"), priority=0)
    registry.register("post_tool_call", lambda payload: order.append("high"), priority=10)
    registry.register("post_tool_call", lambda payload: order.append("low2"), priority=0)
    registry.register("post_tool_call", lambda payload: order.append("neg"), priority=-5)
    registry.emit("post_tool_call", {})
    assert order == ["high", "low", "low2", "neg"]


def test_one_hook_raising_does_not_stop_others():
    """单个 hook 抛异常不影响其它 hook，且异常被记进 failures。"""
    registry = HookRegistry()
    order: list[str] = []

    def boom(payload):
        order.append("boom")
        raise RuntimeError("插件炸了")

    registry.register("on_session_end", boom)
    registry.register("on_session_end", lambda payload: order.append("after") or "ok")
    results = registry.emit("on_session_end", {})
    assert order == ["boom", "after"]
    assert results[0].error and "插件炸了" in results[0].error
    assert results[1].result == "ok"
    assert len(registry.failures) == 1
    assert registry.failures[0].event == "on_session_end"


def test_async_hook_via_sync_emit_without_running_loop():
    """没有运行中的事件循环时，同步 emit 会就地跑完异步 hook 并回填结果。"""
    registry = HookRegistry()
    seen: list[str] = []

    async def hook(payload):
        seen.append(payload["x"])
        return "async-ok"

    registry.register("pre_llm_call", hook)
    results = registry.emit("pre_llm_call", {"x": "hello"})
    assert seen == ["hello"]
    assert results[0].result == "async-ok"
    assert results[0].deferred is False


async def test_aemit_awaits_sync_and_async_hooks():
    """`aemit` 同时支持同步与异步 hook，并按优先级顺序等待。"""
    registry = HookRegistry()
    order: list[str] = []

    def sync_hook(payload):
        order.append("sync")
        return 1

    async def async_hook(payload):
        await asyncio.sleep(0)
        order.append("async")
        return 2

    registry.register("pre_api_request", async_hook, priority=5)
    registry.register("pre_api_request", sync_hook, priority=1)
    results = await registry.aemit("pre_api_request", {})
    assert order == ["async", "sync"]
    assert [item.result for item in results] == [2, 1]


async def test_async_hook_isolated_in_aemit():
    registry = HookRegistry()

    async def boom(payload):
        raise ValueError("异步也炸")

    registry.register("post_llm_call", boom)
    registry.register("post_llm_call", lambda payload: "fine")
    results = await registry.aemit("post_llm_call", {})
    assert results[0].error and "异步也炸" in results[0].error
    assert results[1].result == "fine"


async def test_sync_emit_defers_async_hook_when_loop_is_running():
    """有运行中的循环时，同步 emit 不阻塞：异步 hook 被挂成任务并标 deferred。"""
    registry = HookRegistry()
    ran: list[str] = []

    async def hook(payload):
        ran.append("ran")
        return "later"

    registry.register("on_session_start", hook)
    results = registry.emit("on_session_start", {})
    assert results[0].deferred is True
    await asyncio.sleep(0)  # 让挂上去的任务跑完
    assert ran == ["ran"]


def test_unregister_and_clear():
    registry = HookRegistry()

    def hook(payload):
        return None

    registry.register("subagent_start", hook)
    assert registry.count() == 1
    assert registry.unregister("subagent_start", hook) is True
    assert registry.unregister("subagent_start", hook) is False
    registry.register("subagent_stop", hook)
    registry.clear()
    assert registry.count() == 0


def test_payload_is_copied_per_emit():
    """每次发射给每个 hook 的是同一份 payload 副本；hook 改它不该污染调用方。"""
    registry = HookRegistry()
    received: list[dict] = []
    registry.register("transform_tool_result", lambda payload: received.append(payload))
    payload = {"result": "原始"}
    registry.emit("transform_tool_result", payload)
    received[0]["result"] = "被改了"
    assert payload == {"result": "原始"}
