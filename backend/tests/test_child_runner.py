"""子 agent runner（`delegate_task` / `agent_swarm` 的前置，MATRIX 5.14）。

## 之前缺的是什么

`app/delegate.py` 的**调度**是完整的（深度/并发/预算三道闸、拓扑序、失败隔离、
`subagent_start`/`subagent_stop` 发射点），但它要一个 `ChildRunnerFactory`，
而 `ChildRunner` 协议**没有任何实现**：

- `DelegateRunner()` 不传工厂 → `available=False`、`run()` 抛 `DelegateError`
- `policies.DefaultSubAgentRunner` 是 `available=False` 的桩

**所以整个子 agent 能力一直是空转的**，`delegate_task` / `agent_swarm`
也因此没法注册成工具——注册一个每次调用都抛错的工具，比不注册更糟。

## 判据

1. `graph_child_factory` 造出的 runner **真的能跑通一次完整循环**并交回文本；
2. **深度由运行时记**（`ContextVar`），不由调用方声明——否则工具执行器永远填 1，
   `DelegateRunner.max_depth` 那道闸门就永远不触发；
3. 子任务**有自己的 `Task` 行**，事件不串到父任务名下；
4. 子任务失败/挂起时**如实上报**，不编一个答案；
5. `partial_output()` 在中断时能交出已产生的部分。
"""

from __future__ import annotations

import pytest


class _FakeGraph:
    """最小的图替身：`ainvoke` 记下初始状态，`aget_state` 交回一个终态。"""

    def __init__(self, text: str = "子任务完成", *, interrupts: bool = False):
        self.text = text
        self.interrupts = interrupts
        self.invoked: list[dict] = []

    async def ainvoke(self, initial, config=None):
        self.invoked.append({"initial": initial, "config": config})
        return dict(initial or {})

    async def aget_state(self, config=None):
        class _Snapshot:
            pass

        snapshot = _Snapshot()
        if self.interrupts:
            snapshot.values = {}
            snapshot.interrupts = [type("I", (), {"value": {"question": "选哪个？"}})()]
        else:
            snapshot.values = {
                "final": {"text": self.text},
                "turns": 3,
                "messages": [],
            }
            snapshot.interrupts = []
        return snapshot


class _FakeParent:
    def __init__(self, store, settings, graph):
        self.store = store
        self.settings = settings
        self.graph = graph


@pytest.fixture()
def parent(tmp_path):
    from app.config import Settings
    from app.main import migrate
    from app.storage import Store

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    return _FakeParent(store, settings, _FakeGraph()), store, settings


async def _conversation(store):
    from app.storage import Conversation

    return await store.save(Conversation())


async def test_a_delegated_task_really_runs_and_returns_text(parent):
    """**核心判据**：工厂造出的 runner 能跑通一次循环并交回文本。"""
    from app.child_runner import graph_child_factory
    from app.delegate import DelegateRunner

    fake_parent, store, _settings = parent
    conversation = await _conversation(store)
    try:
        runner = DelegateRunner(
            runner_factory=graph_child_factory(
                fake_parent,
                conversation_id=conversation.id,
                depth=0,
                max_depth=2,
            )
        )
        assert runner.available is True, "传了工厂却仍报不可用"

        result = await runner.run("把柠檬那集的大纲写出来")

        assert result.ok is True, f"委派失败：{result.error}"
        assert result.output == "子任务完成"
        assert result.steps == 3, "步数没从子任务的 `turns` 取回来"
    finally:
        await store.engine.dispose()


async def test_the_child_gets_its_own_task_row(parent):
    """**子任务有自己的 `Task` 行**——事件不会串到父任务名下。"""
    from sqlalchemy import func, select

    from app.child_runner import graph_child_factory
    from app.delegate import DelegateRunner
    from app.storage import Task

    fake_parent, store, _settings = parent
    conversation = await _conversation(store)
    try:
        runner = DelegateRunner(
            runner_factory=graph_child_factory(
                fake_parent, conversation_id=conversation.id, depth=0, max_depth=2
            )
        )
        await runner.run("子任务")
        async with store.sessions() as session:
            count = (
                await session.execute(
                    select(func.count()).select_from(Task).where(
                        Task.conversation_id == conversation.id
                    )
                )
            ).scalar_one()
        assert count == 1, f"子任务没有自己的 Task 行（现有 {count} 条）"
    finally:
        await store.engine.dispose()


async def test_depth_is_recorded_by_the_runtime_not_by_the_caller(parent):
    """**深度由运行时记**：超过 `max_depth` 时工厂就拒，不白烧一轮模型调用。"""
    from app.child_runner import ChildRunnerError, graph_child_factory

    fake_parent, store, _settings = parent
    conversation = await _conversation(store)
    try:
        factory = graph_child_factory(
            fake_parent, conversation_id=conversation.id, depth=1, max_depth=2
        )
        # depth=1 的父 → 子会是 2（允许）；再下一层就超了
        ok_child = factory(type("S", (), {"goal": "x", "budget_steps": 0})())
        assert ok_child is not None

        deeper = graph_child_factory(
            fake_parent, conversation_id=conversation.id, depth=2, max_depth=2
        )
        with pytest.raises(ChildRunnerError):
            deeper(type("S", (), {"goal": "x", "budget_steps": 0})())
    finally:
        await store.engine.dispose()


async def test_a_child_that_needs_the_user_is_reported_not_faked(parent):
    """子任务要问用户时**如实上报**，不编一个答案。

    编一个答案会让父任务以为子任务完成了——那是最坏的一种"成功"。
    """
    from app.child_runner import graph_child_factory
    from app.delegate import DelegateRunner

    fake_parent, store, _settings = parent
    fake_parent.graph = _FakeGraph(interrupts=True)
    conversation = await _conversation(store)
    try:
        runner = DelegateRunner(
            runner_factory=graph_child_factory(
                fake_parent, conversation_id=conversation.id, depth=0, max_depth=2
            )
        )
        result = await runner.run("需要确认的子任务")
        assert result.ok is False, "子任务挂起却被报成成功"
        assert "需要用户确认" in result.error
        assert result.exit_reason == "waiting_user"
    finally:
        await store.engine.dispose()


async def test_partial_output_is_available_for_interrupted_children(parent):
    """`partial_output()` 要能交出已产生的部分——中断补位靠它。"""
    from app.child_runner import GraphChildRunner

    fake_parent, store, _settings = parent
    conversation = await _conversation(store)
    try:
        runner = GraphChildRunner(
            fake_parent,
            goal="半途",
            conversation_id=conversation.id,
            depth=1,
            max_depth=2,
        )
        assert runner.partial_output() == "", "还没跑就交出了内容"
        await runner.run("半途")
        assert runner.partial_output() == "子任务完成", "跑完之后没有可补位的部分"
    finally:
        await store.engine.dispose()
