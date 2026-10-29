"""`delegate_task` / `agent_swarm` 真的注册并能跑（MATRIX 5.14）。

## 判据

1. **runner 没注入 → 一个都不注册**（注册一个调不动的工具比不注册更糟）；
2. runner 注入后**两个工具都注册成功**；
3. `delegate_task` 走一遍真实的委派并交回结果；
4. `agent_swarm` **按 `depends_on` 拓扑序**执行（串行 delegate 表达不了依赖）；
5. **递归闸门**：子 agent 不能再派子 agent，且拒绝理由要说清。
"""

from __future__ import annotations

import pytest


class _FakeGraph:
    def __init__(self, text="子任务完成"):
        self.text = text

    async def ainvoke(self, initial, config=None):
        return dict(initial or {})

    async def aget_state(self, config=None):
        class _Snapshot:
            pass

        snapshot = _Snapshot()
        snapshot.values = {"final": {"text": self.text}, "turns": 2, "messages": []}
        snapshot.interrupts = []
        return snapshot


class _FakeParent:
    def __init__(self, store, settings):
        self.store = store
        self.settings = settings
        self.graph = _FakeGraph()


@pytest.fixture()
async def env(tmp_path):
    from app.config import Settings
    from app.main import migrate
    from app.storage import Conversation, Store
    from app.tools import ToolRegistry

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    # **父任务必须真的存在**：工具要按 `task_id` 查它属于哪个会话，
    # 查不到就没法决定子任务该落在哪里（第一版测试用了不存在的 id，暴露了这个设计）。
    from app.storage import Task

    parent_task = await store.save(
        Task(conversation_id=conversation.id, goal="父任务", status="RUNNING")
    )
    parent = _FakeParent(store, settings)
    registry = ToolRegistry(settings, store)
    yield parent, store, settings, registry, parent_task
    await store.engine.dispose()


def _builder(parent):
    """`(conversation_id) -> DelegateRunner`——子任务落在父任务那个会话里。"""
    from app.child_runner import graph_child_factory
    from app.delegate import DelegateRunner

    def build(conversation_id: str):
        return DelegateRunner(
            runner_factory=graph_child_factory(
                parent, conversation_id=conversation_id, depth=0, max_depth=2
            )
        )

    return build


def _wire(registry, parent, store):
    """按生产装配的方式接线：`register_delegate_tools(registry, store=…, builder=…)`。"""
    from app import tool_delegate

    return tool_delegate.register_delegate_tools(
        registry, store=store, builder=_builder(parent)
    )


def test_without_a_runner_nothing_is_registered(env):
    """**判据 ①**：没接线就一个都不注册，并留下告警。

    注册一个必然抛错的工具，会让模型以为能用、用户以为坏了。
    """
    from app import tool_delegate

    _parent, _store, _settings, registry, _conversation = env
    tool_delegate.reset()
    names = tool_delegate.register_delegate_tools(registry)

    assert names == [], f"没有委派器却注册了 {names}"
    assert "delegate_task" not in registry.tools


async def test_both_tools_register_with_a_runner(env):
    """**判据 ②**：注入了 runner，两个工具都要注册上。"""
    from app import tool_delegate

    parent, _store, _settings, registry, parent_task = env
    names = _wire(registry, parent, _store)
    try:
        assert set(names) == {"delegate_task", "agent_swarm"}
        assert "delegate_task" in registry.tools
        assert "agent_swarm" in registry.tools
    finally:
        tool_delegate.reset()


async def test_delegate_task_runs_a_real_delegation(env):
    """**判据 ③**：`delegate_task` 真的跑完并交回子任务结果。"""
    from app import tool_delegate

    parent, _store, _settings, registry, parent_task = env
    _wire(registry, parent, _store)
    try:
        payload = await tool_delegate.execute(
            "delegate_task", {"goal": "写一段大纲"}, parent_task.id, "op-1"
        )
        assert payload["ok"] is True, f"委派失败：{payload.get('error')}"
        assert payload["output"] == "子任务完成"
        assert payload["child_task_id"], "没交回子任务 id"
    finally:
        tool_delegate.reset()


async def test_agent_swarm_respects_dependencies(env):
    """**判据 ④**：`agent_swarm` 按 `depends_on` 拓扑序执行。

    这是 swarm 相对「串行调 delegate_task」的全部价值——依赖关系表达不出来，
    两个工具就只是同一件事的两种写法。
    """
    from app import tool_delegate

    parent, _store, _settings, registry, parent_task = env
    _wire(registry, parent, _store)
    try:
        payload = await tool_delegate.execute(
            "agent_swarm",
            {
                "tasks": [
                    {"id": "a", "goal": "第一件", "depends_on": []},
                    {"id": "b", "goal": "第二件", "depends_on": ["a"]},
                ]
            },
            parent_task.id,
            "op-2",
        )
        # a 与 b 有依赖 → 必须分成两层，不能同层
        assert payload["levels"] == [["a"], ["b"]], f"依赖没生效：{payload.get('levels')}"
    finally:
        tool_delegate.reset()


async def test_nested_delegation_is_refused(env):
    """**判据 ⑤**：深度由运行时记，子 agent 不能再派子 agent。"""
    from app import tool_delegate
    from app.child_runner import DELEGATION_DEPTH

    parent, _store, _settings, registry, parent_task = env
    _wire(registry, parent, _store)
    token = DELEGATION_DEPTH.set(2)
    try:
        with pytest.raises(ValueError) as excinfo:
            await tool_delegate.execute("delegate_task", {"goal": "再派一层"}, parent_task.id, "op-3")
        assert "委派深度" in str(excinfo.value)
    finally:
        DELEGATION_DEPTH.reset(token)
        tool_delegate.reset()


async def test_too_many_swarm_items_are_refused(env):
    """超上限**明确拒绝而不是排队**——排到超时比直接说不更糟。"""
    from app import tool_delegate

    parent, _store, _settings, registry, parent_task = env
    _wire(registry, parent, _store)
    try:
        too_many = [
            {"id": f"t{index}", "goal": "x", "depends_on": []}
            for index in range(tool_delegate.MAX_SWARM_ITEMS + 1)
        ]
        with pytest.raises(ValueError) as excinfo:
            await tool_delegate.execute("agent_swarm", {"tasks": too_many}, parent_task.id, "op-4")
        assert "最多派" in str(excinfo.value)
    finally:
        tool_delegate.reset()
