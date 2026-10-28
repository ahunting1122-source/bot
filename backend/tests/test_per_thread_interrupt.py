"""per-thread 中断：**跑在路上的那一步真的能被放弃**（MATRIX 2.8）。

## 这一层测的是"边界"，不是"顺利路径"

原来的实现是"标记 + 跳过"：线程被标记后不再**开始**新工具，但已经在跑的那一次会
一直跑到自然结束（一个 `run_command` 跑十分钟，用户点了停止就要等十分钟）。现在
`execute` 把工具调用包成独立 `Task` 并轮询中断标记，命中就 `cancel()`。

用例刻意把工具卡在一个永不完成的 `await` 上（`asyncio.sleep(3600)`）——**不真的等
任何东西**，中断一置位就必须在毫秒级返回。`interrupt_poll_seconds` 调到 5 ms，
于是整组用例不到一秒。

## 唯一不可中断的窗口（如实写在这里）

`cancel()` 能打断"正在等 I/O 的 `await`"，但打不断**已经派到线程/子进程里**的同步活
（`asyncio.to_thread` 的 CPU 活、已经在执行的 `subprocess`）：那一段只会被"放弃等待"，
自己跑完、结果丢弃。要硬杀子进程得区分"谁派的、能不能杀"，是另一件事。
"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessage

from app import errors_retry as retry
from app.graph.nodes import Nodes
from app.heartbeat import Heartbeat

# ── 夹具 ────────────────────────────────────────────────────────────────────


class _Store:
    def __init__(self) -> None:
        self.events: list[dict] = []

    async def event(self, task_id, kind, **payload):
        self.events.append({"task_id": task_id, "kind": kind, **payload})

    def kinds(self) -> list[str]:
        return [item["kind"] for item in self.events]


class _GateRegistry:
    """工具卡在永不完成的 await 上；被取消时记一笔（这就是"真的被中断"的证据）。"""

    browser = None

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}
        self.started = asyncio.Event()
        self.calls = 0
        self.cancelled = False

    async def execute(self, task_id, operation_id, name, args, messages=None):
        self.calls += 1
        self.started.set()
        try:
            await asyncio.sleep(3600)  # 真实世界里这里是"等子进程 / 等网络"
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return {"status": "ok", "data": {"ok": True}}  # pragma: no cover —— 走不到


class _FastRegistry(_GateRegistry):
    """正常路径：立刻返回结果。"""

    async def execute(self, task_id, operation_id, name, args, messages=None):
        self.calls += 1
        self.started.set()
        return {"status": "ok", "data": {"ok": True}}


class _Policies:
    class _Results:
        def project(self, name, result):
            return result

    tool_results = _Results()


class _Runner:
    def __init__(self, *, interrupted: tuple[str, ...] = (), registry=None) -> None:
        self.store = _Store()
        self.partial: dict[str, str] = {}
        self._token_usage: dict[str, dict] = {}
        self.settings = None
        self.registry = registry or _GateRegistry()
        self.policies = _Policies()
        self.heartbeat = Heartbeat()
        self.interrupted_threads: set[str] = set(interrupted)

    def is_interrupted(self, thread_id: str) -> bool:
        return thread_id in self.interrupted_threads

    def interrupt(self, thread_id: str) -> None:
        self.interrupted_threads.add(thread_id)


def _state(task_id: str = "t-a", *, calls: list | None = None) -> dict:
    return {
        "task_id": task_id,
        "calls": calls
        or [
            {"name": "run_command", "args": {"command": "sleep 3600"}, "id": "c1"},
            {"name": "local_read_document", "args": {"path": "a.md"}, "id": "c2"},
        ],
        "actions": 0,
        "answer": {"approved": True},
        "messages": [],
    }


def _nodes(runner) -> Nodes:
    nodes = Nodes(runner)
    nodes.interrupt_poll_seconds = 0.005  # 别为"能不能中断"真等 0.25 s
    return nodes


# ── 1. 在跑的那一步被放弃 ───────────────────────────────────────────────────


async def test_interrupting_an_in_flight_tool_really_cancels_it():
    """中断置位后：工具 Task 被取消，`execute` 立刻返回（不等工具自然结束）。"""
    runner = _Runner()
    nodes = _nodes(runner)
    job = asyncio.create_task(nodes.execute(_state()))
    await asyncio.wait_for(runner.registry.started.wait(), timeout=5)
    runner.interrupt("t-a")
    updates = await asyncio.wait_for(job, timeout=5)

    assert runner.registry.cancelled is True, "工具必须真的被取消，而不是等它跑完"
    assert updates["calls"] == [], "这一轮的调用已全部结清"
    assert updates["last_result"] == {}, "中断不是一次工具结果，不该进 tool_results"


async def test_pairs_stay_legal_when_a_tool_is_interrupted_mid_flight():
    """核心不变量：`tool_calls` 条数 == `ToolMessage` 条数（缺一个就是供应商 400）。"""
    runner = _Runner()
    nodes = _nodes(runner)
    calls = [
        {"name": "run_command", "args": {"command": "sleep 3600"}, "id": "c1"},
        {"name": "local_read_document", "args": {"path": "a.md"}, "id": "c2"},
    ]
    ai = AIMessage(content="一次调两个", tool_calls=calls)
    state = {**_state(calls=calls), "messages": [ai]}
    job = asyncio.create_task(nodes.execute(state))
    await asyncio.wait_for(runner.registry.started.wait(), timeout=5)
    runner.interrupt("t-a")
    updates = await asyncio.wait_for(job, timeout=5)

    merged = [ai, *updates["messages"]]
    assert retry.missing_tool_results(merged) == []
    tool_messages = [message for message in merged if message.type == "tool"]
    tool_calls = sum(len(getattr(message, "tool_calls", None) or []) for message in merged)
    assert tool_calls == len(tool_messages) == 2
    assert all("[interrupted]" in message.content for message in updates["messages"])
    # 每个 ToolMessage 都要对上一个**真实存在**的 call id
    assert {message.tool_call_id for message in tool_messages} == {"c1", "c2"}


async def test_following_steps_are_skipped_after_the_interrupt():
    """中断之后：`think` 不再请求模型（那是没人要的回答，还白花一次 token）。"""

    class _ExplodingModel:
        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):  # pragma: no cover —— 被调用就是失败
            raise AssertionError("中断之后不该再请求模型")

    runner = _Runner(interrupted=("t-a",))
    nodes = _nodes(runner)
    updates = await nodes.think(
        {"task_id": "t-a", "messages": [], "context_meta": {}, "refs": {}}
    )
    assert updates["calls"] == []
    assert updates["messages"][0].type == "ai"
    assert "已被中断" in updates["messages"][0].content
    # 顺带确认隔离：别的线程没被这条标记带走
    assert runner.is_interrupted("t-b") is False


async def test_a_marked_thread_never_starts_the_tool_at_all():
    """入口就判：已经标记的线程根本不调 `registry.execute`（省掉副作用）。"""
    runner = _Runner(interrupted=("t-a",))
    nodes = _nodes(runner)
    updates = await nodes.execute(_state())
    assert runner.registry.calls == 0, "不该启动工具"
    assert len(updates["messages"]) == 2
    assert retry.missing_tool_results(updates["messages"]) == []


async def test_interrupting_one_thread_leaves_the_other_running():
    """per-thread 隔离：t-a 断了，t-b 的工具照常跑完并返回真结果。"""
    runner = _Runner(interrupted=("t-a",), registry=_FastRegistry())
    nodes = _nodes(runner)
    out_a = await nodes.execute(_state("t-a"))
    out_b = await nodes.execute(_state("t-b", calls=[{"name": "run_command", "args": {}, "id": "c9"}]))
    assert "[interrupted]" in out_a["messages"][0].content
    assert "[interrupted]" not in out_b["messages"][0].content
    assert out_b["last_result"]["status"] == "ok"


# ── 2. 外层取消也要带走工具 Task（别留孤儿）────────────────────────────────


async def test_cancelling_the_whole_task_also_cancels_the_tool():
    """`cancel()` 把整个任务取消时，工具 Task 不能留在后台继续跑。"""
    runner = _Runner()
    nodes = _nodes(runner)
    job = asyncio.create_task(nodes.execute(_state()))
    await asyncio.wait_for(runner.registry.started.wait(), timeout=5)
    job.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(job, timeout=5)
    assert runner.registry.cancelled is True


# ── 3. 不中断时行为逐字不变（回归）──────────────────────────────────────────


async def test_normal_tool_call_is_unaffected_by_the_polling_loop():
    """没有中断时：结果原样返回、`calls` 出队、`last_result` 正常填。"""
    runner = _Runner(registry=_FastRegistry())
    nodes = _nodes(runner)
    updates = await nodes.execute(
        _state(calls=[{"name": "run_command", "args": {"command": "ls"}, "id": "c1"}])
    )
    assert updates["calls"] == []
    assert updates["last_result"]["status"] == "ok"
    assert updates["actions"] == 1
    assert "[interrupted]" not in updates["messages"][0].content


async def test_tool_exceptions_still_propagate():
    """轮询不吞异常：工具抛什么，`execute` 就抛什么（与改造前一致）。"""

    class _BoomRegistry(_FastRegistry):
        async def execute(self, task_id, operation_id, name, args, messages=None):
            self.calls += 1
            raise ValueError("工具炸了")

    runner = _Runner(registry=_BoomRegistry())
    nodes = _nodes(runner)
    with pytest.raises(ValueError, match="工具炸了"):
        await nodes.execute(_state(calls=[{"name": "run_command", "args": {}, "id": "c1"}]))
