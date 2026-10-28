"""核心循环层：失败分类接线、中断配对、后台复审、per-thread 中断。

这一层最难的从来不是"跑通"，而是**出错时**的行为：401 要不要重试、上下文溢出
怎么救、工具被中断后消息还合不合法。所以这里的用例大多在**逼出错路径**。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage, ToolMessage
from sqlalchemy import select

from app import errors_retry as retry
from app.agent import AgentRunner
from app.config import Settings
from app.graph.nodes import MODEL_ATTEMPTS, ModelFailureError, Nodes
from app.heartbeat import Heartbeat
from app.main import migrate
from app.storage import Conversation, Message, Store, Task, TaskEvent
from app.tools import ToolRegistry


# ── 最小 runner：ask_model 只用到 store / partial / heartbeat / settings ──────
class _Store:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def event(self, task_id, kind, **payload):
        self.events.append({"task_id": task_id, "kind": kind, **payload})

    def kinds(self) -> list[str]:
        return [item["kind"] for item in self.events]

    def find(self, kind: str) -> dict:
        return next(item for item in self.events if item["kind"] == kind)


class _Registry:
    browser = None

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    async def execute(self, task_id, operation_id, name, args, messages=None):
        return {"status": "ok", "data": {"ok": True}}


class _Policies:
    class _Results:
        def project(self, name, result):
            return result

    tool_results = _Results()


class _Runner:
    def __init__(self, *, interrupted: tuple[str, ...] = ()) -> None:
        self.store = _Store()
        self.partial: dict[str, str] = {}
        self._token_usage: dict[str, dict] = {}
        self.settings = None
        self.registry = _Registry()
        self.policies = _Policies()
        self.heartbeat = Heartbeat()
        self._interrupted = set(interrupted)

    def is_interrupted(self, thread_id: str) -> bool:
        return thread_id in self._interrupted


def _task(task_id: str = "t1"):
    return SimpleNamespace(id=task_id)


def _identity() -> dict:
    return {"conversation_id": "c1", "message_id": "m1", "response_id": "r1"}


# ── 1. 分类接线（MATRIX 2.6）────────────────────────────────────────────────


class _DeadModel:
    """每次调用都抛同一个错，并记录被调了几次。"""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.calls = 0

    def bind_tools(self, *args, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        raise self.error


async def test_auth_failure_is_called_exactly_once():
    """401 重试三次还是 401，只是让用户多等三个来回——一次就停。"""
    runner = _Runner()
    nodes = Nodes(runner)
    model = _DeadModel(RuntimeError("Error code: 401 - invalid api key"))
    with pytest.raises(ModelFailureError) as caught:
        await nodes.ask_model(model, [HumanMessage(content="hi")], _task(), _identity())
    assert model.calls == 1
    message = str(caught.value)
    assert "auth" in message  # 类别
    assert "API key" in message  # 建议（照 errors.ADVICE 来，不锁死整句）
    assert "已尝试" not in message  # 只试一次时不该提次数
    assert runner.store.kinds() == []  # 没重试就没重试事件


async def test_rate_limit_is_retried_until_it_succeeds():
    """429 属于四类可重试之一：重试到成功，并留下 model_retry 事件。"""

    class _Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("429 too many requests")
            return AIMessage(content="这次成功了")

    runner = _Runner()
    nodes = Nodes(runner)
    model = _Flaky()
    answer = await nodes.ask_model(model, [HumanMessage(content="hi")], _task(), _identity())
    assert answer.text == "这次成功了"
    assert model.calls == 3
    retries = [item for item in runner.store.events if item["kind"] == "model_retry"]
    assert len(retries) == 2
    assert retries[0]["failure_kind"] == "rate_limit"  # 事件里带类别，排障时不用再猜


async def test_exhausted_retries_report_kind_advice_and_attempts():
    """全挂时文案必须带**类别 + 建议 + 次数**——只有"请检查连接"会把人引错方向。"""
    runner = _Runner()
    nodes = Nodes(runner)
    model = _DeadModel(RuntimeError("503 service unavailable"))
    with pytest.raises(ModelFailureError) as caught:
        await nodes.ask_model(model, [HumanMessage(content="hi")], _task(), _identity())
    assert model.calls == MODEL_ATTEMPTS
    message = str(caught.value)
    assert "overloaded" in message
    assert "服务过载" in message
    assert f"已尝试 {MODEL_ATTEMPTS} 次" in message


async def test_half_streamed_text_is_never_retried():
    """已经吐出正文再重试 = 把重复内容推给前端。原样抛，不包成 ModelFailureError。"""

    class _HalfStream:
        def __init__(self) -> None:
            self.calls = 0

        def bind_tools(self, *args, **kwargs):
            return self

        async def astream(self, messages):
            self.calls += 1
            yield AIMessageChunk(content="半截回答")
            raise RuntimeError("connection reset")

    runner = _Runner()
    nodes = Nodes(runner)
    model = _HalfStream()
    runner.partial["t1"] = ""  # 生产路径由 think() 初始化，这里直连 ask_model 要自己给
    with pytest.raises(RuntimeError) as caught:
        await nodes.ask_model(model, [HumanMessage(content="hi")], _task(), _identity())
    assert model.calls == 1
    assert not isinstance(caught.value, ModelFailureError)
    assert runner.partial["t1"] == "半截回答"


# ── 2. 上下文溢出：先压缩再重试一次（MATRIX 2.7）─────────────────────────────


class _OverflowThenOk:
    def __init__(self) -> None:
        self.calls = 0
        self.seen: list[list] = []

    def bind_tools(self, *args, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        self.seen.append(list(messages))
        if self.calls == 1:
            raise RuntimeError("maximum context length is 128000 tokens")
        return AIMessage(content="压完就好了")


def _fat_history() -> list:
    call = {"name": "local_read_document", "args": {"path": "big.log"}, "id": "call-1"}
    return [
        HumanMessage(content="目标：分析日志"),
        AIMessage(content="我先读一下。", tool_calls=[call]),
        ToolMessage(content="日志正文" * 50_000, tool_call_id="call-1"),
    ]


async def test_context_overflow_compacts_then_retries_once():
    runner = _Runner()
    nodes = Nodes(runner)
    model = _OverflowThenOk()
    answer = await nodes.ask_model(model, _fat_history(), _task(), _identity())
    assert answer.text == "压完就好了"
    assert model.calls == 2  # 一次溢出 + 一次压缩后重试
    assert "context_compacted" in runner.store.kinds()
    before = len("".join(str(m.content) for m in model.seen[0]))
    after = len("".join(str(m.content) for m in model.seen[1]))
    assert after < before
    # 压缩不能拆散配对：tool_calls 数与 tool 消息数必须相等。
    compacted = model.seen[1]
    assert retry.missing_tool_results(compacted) == []


async def test_compaction_keeps_pairing_when_it_drops_old_rounds():
    """压缩到要丢轮次时，也只能**整组**丢：留下半个配对就是供应商 400。"""
    rounds = 60
    history: list = []
    for index in range(rounds):
        history.append(
            AIMessage(
                content=f"第 {index} 步",
                tool_calls=[
                    {"name": "run_command", "args": {"command": "ls"}, "id": f"call-{index}"}
                ],
            )
        )
        history.append(ToolMessage(content="输出" * 20_000, tool_call_id=f"call-{index}"))
    nodes = Nodes(_Runner())
    compacted = nodes.compact_messages(history)
    assert len(compacted) < len(history)
    assert retry.missing_tool_results(compacted) == []
    # 最后一组（当前正在做的事）必须留着，否则模型不知道自己刚才在干嘛。
    assert getattr(compacted[-1], "tool_call_id", "") == f"call-{rounds - 1}"


async def test_compaction_leaves_the_frozen_system_prefix_untouched():
    """系统提示是冻结前缀：压它会同时打掉提示缓存与任务内自洽。"""
    from langchain_core.messages import SystemMessage

    nodes = Nodes(_Runner())
    prefix = "S" * 40_000
    compacted = nodes.compact_messages(
        [SystemMessage(content=prefix), HumanMessage(content="目标" * 100_000)]
    )
    assert compacted[0].content == prefix


# ── 3. 中断配对（MATRIX 2.4）───────────────────────────────────────────────


def test_interrupted_result_has_the_documented_shape():
    payload = retry.interrupted_result("run_command")
    assert payload["output"] == retry.INTERRUPTED_OUTPUT == "[interrupted]"
    assert payload["returncode"] == retry.INTERRUPTED_RETURNCODE == 130
    assert payload["tool"] == "run_command"


def test_repair_fills_missing_tool_results():
    """有 tool_calls 却没有 tool 结果：补占位，让配对重新合法。"""
    call = {"name": "run_command", "args": {}, "id": "call-1"}
    history = [AIMessage(content="跑一下", tool_calls=[call])]
    assert retry.missing_tool_results(history) == ["call-1"]
    repaired = retry.repair_tool_pairs(history)
    assert len(repaired) == 1 and repaired[0].tool_call_id == "call-1"
    assert "[interrupted]" in repaired[0].content
    assert retry.missing_tool_results([*history, *repaired]) == []
    # 幂等：已经补齐的再补一次不会多出来。
    assert retry.repair_tool_pairs([*history, *repaired]) == []


async def test_interrupted_execute_still_returns_one_result_per_call():
    """中断的**唯一**合法出口是"每个调用都给一条结果"——不是抛异常、不是跳过。"""
    runner = _Runner(interrupted=("t-a",))
    nodes = Nodes(runner)
    calls = [
        {"name": "run_command", "args": {"command": "ls"}, "id": "c1"},
        {"name": "local_read_document", "args": {"path": "a.md"}, "id": "c2"},
    ]
    ai = AIMessage(content="一次调两个", tool_calls=calls)
    state = {
        "task_id": "t-a",
        "calls": calls,
        "actions": 0,
        "answer": {"approved": True},
        "messages": [ai],
    }
    updates = await nodes.execute(state)
    assert updates["calls"] == []
    assert len(updates["messages"]) == 2
    # 核心不变量：tool_calls 条数 == ToolMessage 条数。
    merged = [ai, *updates["messages"]]
    tool_calls = sum(len(getattr(m, "tool_calls", None) or []) for m in merged)
    tool_messages = [m for m in merged if m.type == "tool"]
    assert tool_calls == len(tool_messages) == 2
    assert retry.missing_tool_results(merged) == []


async def test_interrupted_thread_does_not_call_the_model_again():
    """线程断了就别再请求模型：那是没人要的回答，还白花一次 token。"""
    runner = _Runner(interrupted=("t-a",))
    nodes = Nodes(runner)
    updates = await nodes.think({"task_id": "t-a", "messages": [], "context_meta": {}})
    assert updates["calls"] == []
    assert updates["messages"][0].type == "ai"
    # 顺带确认：中断判定是 per-thread 的，别的线程不会被这条标记带走。
    assert runner.is_interrupted("t-b") is False


# ── 4. per-thread 中断只影响目标线程（MATRIX 2.8）────────────────────────────


async def test_interrupting_one_thread_leaves_the_other_alone():
    runner = _Runner(interrupted=("t-a",))
    nodes = Nodes(runner)
    state_a = {
        "task_id": "t-a",
        "calls": [{"name": "run_command", "args": {"command": "ls"}, "id": "c1"}],
        "actions": 0,
        "answer": {"approved": True},
        "messages": [],
    }
    state_b = {
        "task_id": "t-b",
        "calls": [{"name": "run_command", "args": {"command": "ls"}, "id": "c2"}],
        "actions": 0,
        "answer": {"approved": True},
        "messages": [],
    }
    out_a = await nodes.execute(state_a)
    out_b = await nodes.execute(state_b)
    assert "[interrupted]" in out_a["messages"][0].content
    assert "[interrupted]" not in out_b["messages"][0].content
    assert out_b["calls"] == []  # 正常路径照常推进


# ── 5. 后台复审（MATRIX 2.5）────────────────────────────────────────────────


class _ReviewModel:
    def __init__(self, *, gate: asyncio.Event | None = None, error: BaseException | None = None):
        self.gate = gate
        self.error = error
        self.prompts: list[str] = []

    def bind_tools(self, *args, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.prompts.append(str(messages[-1].content))
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return AIMessage(content="- 先跑测试再交付")


async def _runner_with_task(tmp_path, model, *, bg_review: bool):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    if bg_review:
        (settings.data_dir / "settings.json").write_text(
            json.dumps({"bg_review": True}), encoding="utf-8"
        )
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    await store.save(
        Message(conversation_id=conversation.id, role="user", content="帮我把日志看一遍")
    )
    task = await store.save(Task(conversation_id=conversation.id, goal="看日志"))
    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=model)
    return runner, store, task


async def test_bg_review_is_off_by_default_and_does_nothing(tmp_path):
    """默认关：复审结果现在没有消费方，跑它等于花钱换一条日志。"""
    runner, store, task = await _runner_with_task(tmp_path, _ReviewModel(), bg_review=False)
    assert runner.review_enabled() is False
    runner._spawn_bg_review(task)
    assert runner._bg_reviews == set()
    await asyncio.sleep(0.05)
    events = await store.rows(select(TaskEvent).where(TaskEvent.task_id == task.id))
    assert not any(row.event_type == "bg_review" for row in events)
    await store.engine.dispose()


async def test_bg_review_runs_in_the_background_without_blocking(tmp_path):
    """开启时：立刻返回，主流程不等它；跑完后留下一条 bg_review 事件。"""
    gate = asyncio.Event()
    model = _ReviewModel(gate=gate)
    runner, store, task = await _runner_with_task(tmp_path, model, bg_review=True)
    assert runner.review_enabled() is True
    runner._spawn_bg_review(task)
    # 关键断言：模型还卡在 gate 上，说明 create_task 已经返回、主流程没被阻塞。
    assert len(runner._bg_reviews) == 1
    assert not list(runner._bg_reviews)[0].done()
    gate.set()
    await asyncio.gather(*runner._bg_reviews, return_exceptions=True)
    events = await store.rows(select(TaskEvent).where(TaskEvent.task_id == task.id))
    review = next(row for row in events if row.event_type == "bg_review")
    assert "先跑测试再交付" in review.payload["result"]
    assert review.payload["empty"] is False
    # 复审的那一轮确实是"追加了一轮 Review the conversation above…"
    assert "Review the conversation above" in model.prompts[0]
    await store.engine.dispose()


async def test_bg_review_failure_is_silent(tmp_path):
    """复审是旁路：它挂了不能影响已经成功的任务，也不能抛到外面。"""
    model = _ReviewModel(error=RuntimeError("模型挂了"))
    runner, store, task = await _runner_with_task(tmp_path, model, bg_review=True)
    runner._spawn_bg_review(task)
    await asyncio.gather(*runner._bg_reviews, return_exceptions=True)
    events = await store.rows(select(TaskEvent).where(TaskEvent.task_id == task.id))
    kinds = [row.event_type for row in events]
    assert "bg_review_failed" in kinds
    assert "bg_review" not in kinds
    await store.engine.dispose()


async def test_close_cancels_pending_bg_reviews(tmp_path):
    """进程退出时不能为了一次旁路调用把退出拖住。"""

    class _Checkpoint:
        async def __aexit__(self, *exc):
            return None

    gate = asyncio.Event()
    runner, store, task = await _runner_with_task(
        tmp_path, _ReviewModel(gate=gate), bg_review=True
    )
    runner.worker = asyncio.create_task(asyncio.sleep(30))
    runner.checkpoint_context = _Checkpoint()
    runner._spawn_bg_review(task)
    review = next(iter(runner._bg_reviews))
    await runner.close()
    assert review.cancelled()
    assert runner._bg_reviews == set()
    await store.engine.dispose()


async def test_cancel_thread_marks_only_that_thread(tmp_path):
    """`cancel_thread` 只标记目标线程，别的线程的 `is_interrupted` 不受影响。"""
    runner, store, task = await _runner_with_task(tmp_path, _ReviewModel(), bg_review=False)
    assert runner.is_interrupted("t-a") is False
    known = await runner.cancel_thread("t-a", "子任务取消")
    assert known is False  # 这台 runner 上没在跑它
    assert runner.is_interrupted("t-a") is True
    assert runner.is_interrupted("t-b") is False
    assert await runner.cancel_thread("") is False
    await store.engine.dispose()
