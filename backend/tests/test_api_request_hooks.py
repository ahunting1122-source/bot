"""API 请求级钩子与「零告警」性质（MATRIX 13.5）。

## 为什么这两条值得单独存在

**① API 请求级 ≠ 模型调用级。** `pre_llm_call` 包的是整次模型调用（含重试与压缩重来），
`pre_api_request` 包的是**一次真实 HTTP 请求**。`ask_model` 的 while 循环每转一圈就是
一次请求——**重试三次就有三个 span，用 `llm_call` 只能看到一个**。观测端做 tracing
要的正是后者。

**② 「零告警」是可机械验证的性质。** `EMITTED_EVENTS` 的价值在于：
**订阅了不会触发的事件时会响亮告警**。所以「起一次应用，零条『没有发射点』告警」
等价于「所有内置 exporter 订阅的事件都真的有发射点」——
**这比逐个数事件名可靠得多**：exporter 将来加了订阅，这条会自动红。
"""

from __future__ import annotations

import logging
import pathlib

import pytest
from langchain_core.messages import AIMessage


def _record(events: list[tuple[str, dict]], *names: str):
    from app import hooks

    def _make(name: str):
        def _rec(payload: dict) -> None:
            events.append((name, dict(payload)))

        return _rec

    for name in names:
        hooks.DEFAULT_HOOKS.register(name, _make(name))


@pytest.fixture()
def clean_hooks():
    from app import hooks

    hooks.DEFAULT_HOOKS.clear()
    yield
    hooks.DEFAULT_HOOKS.clear()


def test_starting_the_app_emits_zero_missing_emitter_warnings(tmp_path):
    """**起一次应用，不能有任何「没有发射点」的告警。**

    这条是 `EMITTED_EVENTS` 那个机制的**总闸**：内置 exporter 订阅了哪个事件，
    就必须有哪个事件的发射点。将来 exporter 加订阅、或谁删掉一个发射点，这条会红。
    """
    from starlette.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    records: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record.getMessage())

    logger = logging.getLogger("orbit.hooks")
    handler = _Capture()
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
        settings.prepare()
        with TestClient(create_app(settings=settings)):
            pass
    finally:
        logger.removeHandler(handler)

    warns = [r for r in records if "没有发射点" in r]
    assert warns == [], (
        "内置 exporter 订阅了没有发射点的事件（这些订阅永远不会触发）：\n  "
        + "\n  ".join(warns)
    )


class _FailingModel:
    """每次调用都抛错——用来验 `api_request_error`。"""

    def bind_tools(self, *_a, **_k):
        return self

    async def ainvoke(self, _messages):
        raise RuntimeError("provider 挂了")


class _OkModel:
    def bind_tools(self, *_a, **_k):
        return self

    async def ainvoke(self, _messages):
        return AIMessage(content="好了。")


async def _run_once(tmp_path, model, events: list[tuple[str, dict]]) -> None:
    """真跑一次任务，把钩子事件收进 `events`。"""
    import asyncio

    from app.agent import AgentRunner
    from app.config import Settings
    from app.main import migrate
    from app.storage import Conversation, Store, Task
    from app.tools import ToolRegistry

    settings = Settings(
        data_dir=tmp_path / "data",
        workspace=tmp_path / "files",
        max_actions=5,
        max_seconds=0,
    )
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="随便"))

    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=model)
    await runner.start()
    try:
        await runner.enqueue(task.id)
        for _ in range(1200):
            row = await store.get(Task, task.id)
            if row.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                return
            await asyncio.sleep(0.05)
        raise AssertionError("任务没在预期内到终态")
    finally:
        await runner.close()
        await store.engine.dispose()


async def test_api_request_events_fire_per_request(tmp_path, clean_hooks):
    """成功路径：`pre_api_request` 与 `post_api_request` 成对出现。"""
    events: list[tuple[str, dict]] = []
    _record(
        events,
        "pre_api_request",
        "post_api_request",
        "api_request_error",
    )

    await _run_once(tmp_path, _OkModel(), events)

    names = [name for name, _ in events]
    assert "pre_api_request" in names, f"没有 pre_api_request：{names}"
    assert "post_api_request" in names, f"没有 post_api_request：{names}"
    assert "api_request_error" not in names, "成功路径不该发 api_request_error"
    # 成对：每一次 pre 都要有对应的 post（同一次请求）
    assert names.count("pre_api_request") == names.count("post_api_request")


async def test_api_request_error_fires_on_failure(tmp_path, clean_hooks):
    """失败路径：必须发 `api_request_error`，且错误里带得出原因。"""
    events: list[tuple[str, dict]] = []
    _record(events, "pre_api_request", "post_api_request", "api_request_error")

    await _run_once(tmp_path, _FailingModel(), events)

    errors = [payload for name, payload in events if name == "api_request_error"]
    assert errors, "provider 挂了却没发 api_request_error——观测端看不到这次失败"
    assert any("RuntimeError" in str(e.get("error", "")) for e in errors), (
        f"错误里没有原因：{errors}"
    )


def test_the_four_api_level_events_are_declared():
    """这 4 个名字必须在 `EMITTED_EVENTS` 里。"""
    from app.hooks import EMITTED_EVENTS

    for name in (
        "pre_api_request",
        "post_api_request",
        "api_request_error",
        "on_session_finalize",
    ):
        assert name in EMITTED_EVENTS, name


def test_nodes_source_has_no_unwrapped_stream_model_call():
    """防回归：`stream_model`（带发射点的包装层）必须真的被调用。

    如果有人把调用改回 `_stream_model_once`（绕过包装层），API 级事件就会静默消失。
    这条检查**调用点存在**，而不只是「函数存在」。
    """
    from app.graph import nodes as nodes_module

    code = "\n".join(
        line
        for line in pathlib.Path(nodes_module.__file__).read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("#")
    )
    assert "await self.stream_model(" in code, (
        "`ask_model` 不再调 `stream_model` 了——API 级钩子会被绕过"
    )
