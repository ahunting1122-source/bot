"""委派的三道闸 + 中断补位 + 接口自检。

每个用例对应一条**必须成立的硬约束**（都是"委派跑飞"的真实形态）：

* 深度上限：第 3 层被拒，且错误消息里带出上限值（不是"请勿递归"这种提示词）；
* 并发上限：同时真正在跑的子任务数不超过 semaphore；
* 预算隔离：子任务把预算烧光，父预算一分不少；
* 中断补位：取消后父任务拿到**非空**的已完成部分，而不是空结果；
* 接口自检：``satisfies_protocol()`` 对真实现返回 True，对缺方法的实现返回 False
  —— 自检本身也要有判据，否则它只是一句"我觉得对"。

全部用假 runner（``runner_factory`` 注入），**不跑模型、不联网**。
"""

from __future__ import annotations

import asyncio

import pytest

from app.budget import BudgetConfig, TurnBudget
from app.delegate import (
    DEFAULT_MAX_CONCURRENT,
    DEFAULT_MAX_DEPTH,
    INTERRUPTED_PLACEHOLDER,
    MIN_CHILD_BUDGET_TOKENS,
    ChildBudget,
    DelegateError,
    DelegateResult,
    DelegateRunner,
    DelegateSpec,
    satisfies_protocol,
)


class FakeChild:
    """最小的假子任务 runner：可指定延迟、输出、是否失败、以及被取消时交出的部分。"""

    def __init__(
        self,
        *,
        delay: float = 0.0,
        output: str = "done",
        error: str = "",
        partial: str = "",
        on_run: object = None,
    ) -> None:
        self.delay = delay
        self.output = output
        self.error = error
        self._partial = partial
        self._on_run = on_run
        self.calls: list[dict] = []

    async def run(self, goal: str, **kwargs):
        self.calls.append({"goal": goal, **kwargs})
        if callable(self._on_run):
            self._on_run(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        payload: dict = {"output": self.output}
        if self.error:
            payload["error"] = self.error
        return payload

    def partial_output(self) -> str:
        return self._partial


def _runner(child: FakeChild, **kwargs) -> DelegateRunner:
    return DelegateRunner(runner_factory=lambda spec, budget: child, **kwargs)


# ── 深度 ────────────────────────────────────────────────────────────────────


async def test_depth_limit_rejects_third_level_and_names_the_limit():
    """父 → 子 → 孙最多两层；第 3 层必须被拒，且错误里带出上限值。"""
    runner = _runner(FakeChild())
    assert runner.max_depth == DEFAULT_MAX_DEPTH == 2

    third = await runner.run(DelegateSpec(goal="孙的孙", depth=3))
    assert third.ok is False
    assert third.exit_reason == "depth_exceeded"
    assert str(DEFAULT_MAX_DEPTH) in third.error
    assert "深度超限" in third.error

    # 第 2 层（孙）仍然允许——上限是"最多两层"，不是"只能一层"。
    grandchild = await runner.run(DelegateSpec(goal="孙", depth=2))
    assert grandchild.ok is True


async def test_depth_zero_is_normalised_to_first_level():
    """``depth=0``（父自己）按第 1 层算：父调 run 时传 0 是很自然的笔误。"""
    runner = _runner(FakeChild())
    result = await runner.run(DelegateSpec(goal="子", depth=0))
    assert result.ok is True


async def test_empty_goal_is_rejected_before_spending_anything():
    runner = _runner(FakeChild())
    result = await runner.run(DelegateSpec(goal="   "))
    assert result.ok is False
    assert result.exit_reason == "rejected"


async def test_max_depth_must_be_at_least_one():
    with pytest.raises(DelegateError):
        DelegateRunner(max_depth=0)


# ── 并发 ────────────────────────────────────────────────────────────────────


async def test_concurrency_peak_never_exceeds_semaphore():
    """同时真正在跑的子任务数 ≤ max_concurrent（用计数假 runner 断言峰值）。"""
    state = {"now": 0, "peak": 0}

    class Counter:
        async def run(self, goal: str, **kwargs):
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
            try:
                await asyncio.sleep(0.02)
                return {"output": goal}
            finally:
                state["now"] -= 1

        def partial_output(self) -> str:
            return ""

    runner = DelegateRunner(max_concurrent=2, runner_factory=lambda spec, budget: Counter())
    results = await asyncio.gather(
        *(runner.run(DelegateSpec(goal=f"g{index}", depth=1)) for index in range(6))
    )
    assert all(result.ok for result in results)
    assert state["peak"] == 2, f"峰值应正好卡在上限 2，实际 {state['peak']}"


def test_default_concurrency_is_documented_value():
    assert DelegateRunner().max_concurrent == DEFAULT_MAX_CONCURRENT == 4
    with pytest.raises(DelegateError):
        DelegateRunner(max_concurrent=0)


# ── 预算 ────────────────────────────────────────────────────────────────────


async def test_child_budget_is_isolated_from_parent_budget():
    """子任务把预算烧光，父预算**一分不少**——否则一个跑飞的子任务能把父饿死。"""
    parent = TurnBudget()
    captured: dict[str, ChildBudget] = {}

    class Flooder:
        async def run(self, goal: str, **kwargs):
            budget: ChildBudget = kwargs["budget"]
            captured["budget"] = budget
            # 一条 5000 字符的工具结果，远超给它的整轮预算（1000）
            budget.turn.add("terminal", "y" * 5000)
            for _ in range(3):
                budget.charge_step(tokens=100)
            return {"output": "flooded", "steps": budget.steps_used, "tokens": budget.tokens_used}

        def partial_output(self) -> str:
            return ""

    runner = DelegateRunner(
        parent_budget=parent,
        runner_factory=lambda spec, budget: Flooder(),
        child_budget_config=BudgetConfig(turn_budget=1000),
    )
    result = await runner.run(DelegateSpec(goal="flood", depth=1, budget_steps=8))

    child_budget = captured["budget"]
    assert result.ok is True
    # 父预算：没被记过一笔账
    assert parent.total == 0
    assert parent.spilled == 0
    # 子预算：独立实例，自己那份被治理到阈值以内，且如实记了降级
    assert child_budget.turn is not parent
    assert child_budget.turn.total <= 1000
    assert child_budget.turn.degraded is True
    assert child_budget.steps_used == 3
    assert child_budget.tokens_used == 300
    assert child_budget.steps == 8


def test_token_budget_is_half_of_parent_remainder_split_by_siblings():
    """父剩余 × 50% ÷ 兄弟数；剩余太少时落到地板 2000。"""
    runner = DelegateRunner(parent_budget=TurnBudget())  # 默认 200_000 字符预算，未消耗
    assert runner.budget_preview(DelegateSpec(goal="x"), 1).tokens == 100_000
    assert runner.budget_preview(DelegateSpec(goal="x"), 4).tokens == 25_000

    tiny = DelegateRunner(parent_budget=TurnBudget(config=BudgetConfig(turn_budget=1000)))
    assert tiny.budget_preview(DelegateSpec(goal="x"), 1).tokens == MIN_CHILD_BUDGET_TOKENS

    # 显式给了 budget_tokens 就用它，不再分配
    explicit = runner.budget_preview(DelegateSpec(goal="x", budget_tokens=777), 1)
    assert explicit.tokens == 777


async def test_child_that_hits_its_step_budget_is_marked_max_iterations():
    """子任务没自报终态但预算已满 → 记 max_iterations，且 truncated 为真。"""
    runner = _runner(FakeChild(), child_steps=2)

    class Exhausting:
        async def run(self, goal: str, **kwargs):
            budget: ChildBudget = kwargs["budget"]
            while budget.charge_step():
                pass
            return {"output": "跑到没预算了"}

        def partial_output(self) -> str:
            return ""

    runner = DelegateRunner(runner_factory=lambda spec, budget: Exhausting(), child_steps=2)
    result = await runner.run(DelegateSpec(goal="x", depth=1))
    assert result.ok is False
    assert result.exit_reason == "max_iterations"
    assert result.truncated is True


# ── 中断补位 ────────────────────────────────────────────────────────────────


async def test_cancelled_child_hands_partial_output_to_parent():
    """取消后父任务拿到**非空**的已完成部分（补位），不是空结果。"""
    started = asyncio.Event()

    class Slow:
        def __init__(self) -> None:
            self.text = ""

        async def run(self, goal: str, **kwargs):
            started.set()
            for index in range(500):
                await asyncio.sleep(0.002)
                self.text += f"步骤{index};"
            return {"output": self.text}

        def partial_output(self) -> str:
            return self.text

    child = Slow()
    runner = _runner(child)
    child_id = runner.submit(DelegateSpec(goal="慢慢跑", depth=1))
    await started.wait()
    await asyncio.sleep(0.03)  # 让它真的产出一点部分结果
    assert runner.cancel(child_id) is True

    result = await runner.wait(child_id)
    assert result.status == "interrupted"
    assert result.exit_reason == "interrupted"
    assert result.ok is False
    assert result.partial_output, "中断必须交出已完成部分，不能是空串"
    assert "步骤" in result.partial_output
    assert result.output == result.partial_output
    # 缓存：再等一次拿到同一条结果
    assert (await runner.wait(child_id)) is result


async def test_cancel_without_any_partial_falls_back_to_placeholder():
    """子任务一个字都没交出来时退回占位串——父任务永远拿到**非空**结果。"""
    started = asyncio.Event()

    class Silent:
        async def run(self, goal: str, **kwargs):
            started.set()
            await asyncio.sleep(30)
            return {"output": "never"}

        def partial_output(self) -> str:
            return ""

    runner = _runner(Silent())
    child_id = runner.submit(DelegateSpec(goal="静默", depth=1))
    await started.wait()
    runner.cancel(child_id)
    result = await runner.wait(child_id)
    assert result.status == "interrupted"
    assert result.partial_output == INTERRUPTED_PLACEHOLDER
    assert result.partial_output  # 非空


async def test_cancel_unknown_child_is_false_not_an_exception():
    runner = _runner(FakeChild())
    assert runner.cancel("child-does-not-exist") is False


async def test_parent_cancellation_propagates_and_keeps_partial_for_the_ledger():
    """父任务自己被取消时：不吞掉取消信号，但把已完成部分留档。"""
    started = asyncio.Event()

    class Slow:
        def __init__(self) -> None:
            self.text = ""

        async def run(self, goal: str, **kwargs):
            started.set()
            for index in range(500):
                await asyncio.sleep(0.002)
                self.text += f"片段{index};"
            return {"output": self.text}

        def partial_output(self) -> str:
            return self.text

    runner = _runner(Slow())
    task = asyncio.ensure_future(runner.run(DelegateSpec(goal="被父取消", depth=1)))
    await started.wait()
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.interrupted_partials, "父被取消时也要把子任务已完成部分留档"
    assert "片段" in next(iter(runner.interrupted_partials.values()))


# ── 结果与失败 ──────────────────────────────────────────────────────────────


async def test_failed_child_reports_error_and_partial():
    runner = _runner(FakeChild(output="一半的结论", error="模型 429"))
    result = await runner.run(DelegateSpec(goal="会失败", depth=1))
    assert result.ok is False
    assert result.status == "failed"
    assert result.exit_reason == "error"
    assert "429" in result.error
    assert result.partial_output == "一半的结论"


async def test_exception_inside_child_becomes_a_failed_result():
    class Boom:
        async def run(self, goal: str, **kwargs):
            raise RuntimeError("工具崩了")

        def partial_output(self) -> str:
            return ""

    runner = DelegateRunner(runner_factory=lambda spec, budget: Boom())
    result = await runner.run(DelegateSpec(goal="x", depth=1))
    assert result.ok is False
    assert "工具崩了" in result.error


async def test_default_factory_says_not_wired_instead_of_faking_success():
    """没注入 runner 时明确报错，而不是返回一个假成功的空结果。"""
    runner = DelegateRunner()
    assert runner.available is False
    with pytest.raises(DelegateError, match="未注入子任务 runner"):
        await runner.run(DelegateSpec(goal="x", depth=1))


def test_result_to_dict_has_protocol_keys():
    payload = DelegateResult(ok=True, output="done", steps=1, tokens=2, child_task_id="c1").to_dict()
    for key in ("ok", "output", "steps", "tokens", "error", "child_task_id", "partial_output"):
        assert key in payload


# ── 接口自检 ────────────────────────────────────────────────────────────────


def test_satisfies_protocol_accepts_delegate_runner():
    assert satisfies_protocol() is True
    assert satisfies_protocol(DelegateRunner()) is True


def test_satisfies_protocol_rejects_missing_method():
    """故意去掉 `run`：自检必须变 False（这条用例本身也是自检的判据）。"""

    class MissingRun:
        available = True

    assert satisfies_protocol(MissingRun) is False


def test_satisfies_protocol_rejects_sync_run():
    class SyncRun:
        def run(self, goal: str, **kwargs):
            return {}

    assert satisfies_protocol(SyncRun) is False


def test_satisfies_protocol_rejects_run_without_kwargs():
    class NoKwargs:
        async def run(self, goal: str):
            return {}

    assert satisfies_protocol(NoKwargs) is False


def test_satisfies_protocol_rejects_incompatible_return_shape():
    """Protocol 写 `-> dict`；返回既不是 dict 也没有 to_dict() 就算不兼容。"""

    class WrongReturn:
        async def run(self, goal: str, **kwargs) -> object:
            return object()

    assert satisfies_protocol(WrongReturn) is False


def test_satisfies_protocol_accepts_dict_returning_run():
    class PlainDict:
        async def run(self, goal: str, **kwargs) -> dict:
            return {}

    assert satisfies_protocol(PlainDict) is True


def test_protocol_shape_matches_policies_module():
    """确认真的是在对 `policies.SubAgentRunner` 自检，而不是一个本地影子协议。"""
    from app.policies import SubAgentRunner

    assert satisfies_protocol(DelegateRunner, SubAgentRunner) is True
    assert set(vars(SubAgentRunner)) & {"run"} == {"run"}
