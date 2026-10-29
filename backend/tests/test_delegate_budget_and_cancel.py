"""委派的两个真缺口：② 预算记账、④ 中断传播（MATRIX 5.14）。

## ② 预算记账

子任务跑自己的 `budget` 节点（默认 50 步），但**从不向父预算记账**。
父预算 90 步 + 3 个子任务各 50 步 = 实际可以烧 240 步，
而 `budget` 节点只看父自己的 `actions` —— **预算闸门形同虚设**。

## ④ 中断传播

`DelegateRunner` 只在自己被 `cancel()` 时才停子任务，而父任务被取消走的是
`AgentRunner.cancel`。**不连带的话父停了子还在跑**：烧钱、占租约，
而且父任务早已结束，产出没有任何人读。
"""

from __future__ import annotations

# ── ② 预算记账 ─────────────────────────────────────────────────────────────


def test_child_steps_are_read_from_the_payload():
    """只有委派工具会带 `steps`，其余工具必须恒为 0。"""
    from app.graph.nodes import _child_steps

    assert _child_steps({"steps": 7}) == 7
    assert _child_steps({"output": "x"}) == 0, "普通工具被记了步数——预算会虚高"
    assert _child_steps(None) == 0
    assert _child_steps("not-a-dict") == 0
    assert _child_steps({"steps": "5"}) == 5
    assert _child_steps({"steps": -3}) == 0, "负步数会把父预算反向加回去"
    assert _child_steps({"steps": "abc"}) == 0


def test_the_execute_node_charges_child_steps_to_the_parent():
    """**防回归**：`execute` 节点的 `actions` 必须带上子任务步数。

    这条钉的是「接线存在」——只验 `_child_steps` 能用是不够的，
    5.14 的虚报形状正是「机制写好了、没人调」。
    """
    import pathlib

    from app.graph import nodes as nodes_module

    source = pathlib.Path(nodes_module.__file__).read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
    assert "_child_steps(result)" in code, (
        "`execute` 节点不再把子任务步数记到父账上——预算闸门又形同虚设"
    )


# ── ④ 中断传播 ─────────────────────────────────────────────────────────────


class _FakeRunner:
    """记下 `cancel` 被调了几次、对谁。"""

    def __init__(self, children=("child-1", "child-2")):
        self._children = {child: object() for child in children}
        self.cancelled: list[str] = []

    async def cancel(self, child_id):
        self.cancelled.append(child_id)


async def test_cancel_children_stops_every_child_of_that_parent():
    """**核心判据**：父任务被取消时，它派出去的子任务必须一并停掉。"""
    from app import tool_delegate

    tool_delegate.reset()
    runner = _FakeRunner()
    tool_delegate._RUNNERS["parent-task"] = runner
    try:
        stopped = await tool_delegate.cancel_children("parent-task")
        assert stopped == 2, f"只停了 {stopped} 个子任务"
        assert sorted(runner.cancelled) == ["child-1", "child-2"]
    finally:
        tool_delegate.reset()


async def test_cancelling_an_unknown_parent_is_a_no_op():
    """没有子任务的父任务取消时不能抛——取消路径上抛错只会多一个错误。"""
    from app import tool_delegate

    tool_delegate.reset()
    assert await tool_delegate.cancel_children("never-seen") == 0


async def test_a_broken_child_does_not_stop_the_others():
    """**异常隔离**：一个子任务停不掉，不能连累其它子任务。"""
    from app import tool_delegate

    tool_delegate.reset()

    class _Picky(_FakeRunner):
        async def cancel(self, child_id):
            if child_id == "child-1":
                raise RuntimeError("停不掉")
            await super().cancel(child_id)

    runner = _Picky()
    tool_delegate._RUNNERS["parent-task"] = runner
    try:
        stopped = await tool_delegate.cancel_children("parent-task")
        assert stopped == 1, "一个炸了就不停另一个了"
        assert runner.cancelled == ["child-2"]
    finally:
        tool_delegate.reset()


def test_the_agent_cancel_really_calls_cancel_children():
    """**防回归**：`AgentRunner.cancel` 必须调 `tool_delegate.cancel_children`。

    只验 `cancel_children` 能用是不够的——④ 的虚报形状正是「写好了但没人调」。
    """
    import pathlib

    from app import agent as agent_module

    source = pathlib.Path(agent_module.__file__).read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
    assert "tool_delegate.cancel_children(task_id)" in code, (
        "`AgentRunner.cancel` 不再连带停子任务——父停了子还在跑"
    )

# ── ④ 的**时序**测试：这正是 10.8 的租约保护被绕过的路径 ──────────────────


async def test_cancelling_the_parent_stops_children_before_releasing_the_lease(tmp_path):
    """**时序判据**：父跑 → 派出子 → cancel 父 → **子必须先停**，租约才能释放。

    ## 为什么这条比「`cancel_children` 能停子任务」重要得多

    监视子agent 查出的真实时序是：

    ```
    父 acquire(conversation)                 ← agent.py 按**会话**取租约
    spawn 子（子共享父的会话，但**自己不 acquire**）
    用户 cancel → 父 finally **release**      ← 租约消失
    子**继续写同一个 conversation**           ← 两个写者，无警告
    用户重开 → 新任务 acquire 成功 → 冲突
    ```

    **这正是 10.8 建来防的场景，而保护在「取消」这条路径上被完全绕过**——
    而且**比不保护更危险**：租约被占着，用户至少看到「有人持有」；
    租约空着，**没人会怀疑**。

    所以这条测试断言的是**顺序**：子任务在父的 job 被 cancel **之前**就停了。
    """
    import asyncio

    from app import tool_delegate
    from app.agent import AgentRunner
    from app.config import Settings
    from app.storage import Store
    from app.tools import ToolRegistry

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    store = Store(settings.data_dir / "app.sqlite")
    runner = AgentRunner(settings, store, ToolRegistry(settings, store))
    task_id = "parent-task"

    order: list[str] = []

    class _Child:
        def __init__(self):
            self._children = {"c1": object()}

        async def cancel(self, child_id):
            order.append(f"child:{child_id}")

    tool_delegate.reset()
    tool_delegate._RUNNERS[task_id] = _Child()

    gate = asyncio.Event()

    async def _job():
        await gate.wait()
        order.append("parent-finished")

    job = asyncio.create_task(_job())
    runner.current[task_id] = job
    try:
        await runner.cancel(task_id)
        assert order and order[0] == "child:c1", (
            f"父任务先被取消了、子任务还没停——租约会在子任务仍在写时释放：{order}"
        )
        assert job.cancelled(), "父任务的 job 没被取消"
    finally:
        tool_delegate.reset()
        await store.engine.dispose()


async def test_the_delegation_depth_is_visible_inside_the_node():
    """**节点内部**也必须能读到深度，不能只在工具执行器里设。

    监视子agent 实测：`ContextVar` 在 `create_task` / `gather` 里会继承，
    **唯一盲区是把同步活丢进 `run_in_executor` 而不带 `copy_context()`**——
    那里读到 0，闸门静默失效。

    这条钉的是「深度在**节点所在的那个上下文**里是非零的」，不是「我设过」。
    """
    from app.child_runner import DELEGATION_DEPTH

    async def _child_context():
        # 模拟 `GraphChildRunner.run` 里 set / reset 的用法
        token = DELEGATION_DEPTH.set(1)
        try:
            import asyncio as _asyncio

            # 节点是通过 `graph.ainvoke` 在**子任务**里跑的，等价于 create_task
            async def _node():
                return DELEGATION_DEPTH.get()

            return await _asyncio.create_task(_node())
        finally:
            DELEGATION_DEPTH.reset(token)

    assert await _child_context() == 1, "深度没传进节点——递归闸门会静默失效"
    assert DELEGATION_DEPTH.get() == 0, "reset 没生效，深度泄漏到外层"

# ── 同步 `cancel`：真实 `DelegateRunner.cancel` 是**同步**的 ────────────────


async def test_a_synchronous_cancel_is_supported_and_counted(caplog):
    """**真 bug 的回归测试**：`DelegateRunner.cancel` 是**同步方法**，返回 bool。

    ## 第一版错在哪

    ```python
    await runner.cancel(child_id)     # cancel() 真的执行了（inner.cancel() 生效）
    # 然后 await True → TypeError → 被 except 吞掉
    ```

    三个后果：① **`stopped` 恒为 0**（函数自述「返回停掉了几个」是假话）
    ② **每次都留一条假失败 warning**（真失败会被淹没）
    ③ 没有任何东西 await 子任务的**真正退出**。

    ## 为什么原来的测试没抓到

    我原来的假件写的是 `async def cancel`，而**真实类是 `def cancel`**——
    **两件事在测试里长得一样，在运行时差一个 await。**
    所以这条用一个**同步**假件（与真实类同形）。

    **判据**：`assert cancel_children(...) == 子任务数`——第一版恒为 0，加上立刻红。
    """
    from app import tool_delegate

    class _SyncRunner:
        """与真实 `DelegateRunner` 同形：`cancel` 是同步的、返回 bool。"""

        def __init__(self, children=("c1", "c2")):
            self._children = {child: object() for child in children}
            self._inner: dict = {}
            self.cancelled: list[str] = []

        def cancel(self, child_id) -> bool:
            self.cancelled.append(child_id)
            return True

    tool_delegate.reset()
    runner = _SyncRunner()
    tool_delegate._RUNNERS["parent-task"] = runner
    try:
        with caplog.at_level("WARNING"):
            stopped = await tool_delegate.cancel_children("parent-task")
        assert stopped == 2, (
            f"只报了 {stopped} 个——同步 cancel 上的 `await` 会抛 TypeError 被吞，"
            "计数恒为 0"
        )
        assert sorted(runner.cancelled) == ["c1", "c2"]
        # 不该有「停子任务失败」的假告警
        fake = [r.getMessage() for r in caplog.records if "停子任务" in r.getMessage()]
        assert not fake, f"留下了假失败告警，真失败会被淹没：{fake}"
    finally:
        tool_delegate.reset()


async def test_the_child_s_exit_is_awaited_not_just_requested():
    """`inner.cancel()` 只是**发出**请求；必须**等它真的退出**。

    不等的话，父的 `finally → release()` 会在 `cancel_children` 立即返回后执行——
    **窗口变小了但没消失，而 10.8 的全部意义是「没有窗口」。**
    """
    import asyncio

    from app import tool_delegate

    finished = asyncio.Event()

    class _Runner:
        def __init__(self):
            self._children = {"c1": object()}
            self._inner = {"c1": asyncio.create_task(self._child())}

        async def _child(self):
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                # 模拟子任务的 `finally`：写状态、清理。
                # **刻意不在取消处理里再 await**——那会被立刻再次取消。
                # 这个测试自己踩过：`await` 在 `except CancelledError` 里拿不到
                # 执行机会，于是 `finished` 永不置位，看起来像产品的错。
                finished.set()
                raise

        def cancel(self, child_id) -> bool:
            self._inner[child_id].cancel()
            return True

    tool_delegate.reset()
    holder = _Runner()
    tool_delegate._RUNNERS["parent-task"] = holder
    # **必须让子任务先真的跑起来一步**：一个在第一次执行前就被 cancel 的 Task
    # 根本不会进入协程体，`except CancelledError` 也就不会执行——
    # 那会让 `finished` 永不置位，看起来像产品的错（这个测试自己踩过）。
    await asyncio.sleep(0)
    try:
        await tool_delegate.cancel_children("parent-task")
        assert finished.is_set(), (
            "`cancel_children` 返回时子任务还没跑完自己的 finally——"
            "父的 release 会在子任务仍在写时执行"
        )
    finally:
        tool_delegate.reset()


def test_runners_are_cleaned_up_when_the_parent_task_ends():
    """**防泄漏**：父任务正常结束时必须清掉 `_RUNNERS` 记录。

    原来只有 `cancel_children` 里 pop —— 而**绝大多数父任务是正常跑完的**，
    那些记录会永远留着（每个带一个 `DelegateRunner`，内含两个 dict），
    长跑的 gateway 会无界增长。

    放在 `release` 旁边是有意的：**「父任务结束」和「清理子任务资源」该在同一处。**
    """
    import pathlib as _pathlib

    from app import agent as agent_module

    source = _pathlib.Path(agent_module.__file__).read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
    assert "tool_delegate._RUNNERS.pop(task.id, None)" in code, (
        "父任务结束时没清 `_RUNNERS`——正常跑完的父任务会泄漏委派器记录"
    )
