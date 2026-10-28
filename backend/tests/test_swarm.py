"""swarm 的调度语义：拓扑序、环依赖、失败隔离。

三条判据都是"错了会很难查"的那种：

* 拓扑序错了 → 下游拿到的是前驱**还没写完**的产物，结果对但时序错，最难复现；
* 环依赖不报错 → 执行阶段互相等待，表现成"卡住"，而不是一条能读的错误；
* 单个失败中断整批 → 一条 429 让 127 个本来能成的子任务全部白跑。
"""

from __future__ import annotations

import asyncio

import pytest

from app.delegate import DelegateResult, DelegateRunner, DelegateSpec
from app.swarm import (
    STATUS_FAILED,
    STATUS_OK,
    STATUS_SKIPPED,
    SwarmError,
    SwarmPlan,
    SwarmTask,
    run_plan,
)


class Recorder:
    """记录执行顺序的假 runner；可指定"这个节点会失败"。"""

    def __init__(self, name: str, order: list[str], *, fail: bool = False, delay: float = 0.0):
        self.name = name
        self.order = order
        self.fail = fail
        self.delay = delay

    async def run(self, goal: str, **kwargs):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.order.append(self.name)
        if self.fail:
            return {"output": "", "error": f"{self.name} 炸了"}
        return {"output": f"{self.name} 完成"}

    def partial_output(self) -> str:
        return ""


def _runner(order: list[str], failing: set[str] | None = None, **kwargs) -> DelegateRunner:
    bad = failing or set()
    return DelegateRunner(
        runner_factory=lambda spec, budget: Recorder(spec.goal, order, fail=spec.goal in bad),
        **kwargs,
    )


def _plan(edges: dict[str, tuple[str, ...]]) -> SwarmPlan:
    return SwarmPlan(
        tasks=tuple(
            SwarmTask(id=node, spec=DelegateSpec(goal=node, depth=1), depends_on=deps)
            for node, deps in edges.items()
        )
    )


# ── 拓扑序 ──────────────────────────────────────────────────────────────────


def test_levels_are_topological():
    plan = _plan({"a": (), "b": ("a",), "c": ("a",), "d": ("b", "c")})
    assert plan.levels() == [["a"], ["b", "c"], ["d"]]


def test_chain_becomes_one_node_per_level():
    plan = _plan({"a": (), "b": ("a",), "c": ("b",)})
    assert plan.levels() == [["a"], ["b"], ["c"]]


def test_independent_nodes_share_one_level():
    plan = _plan({"a": (), "b": (), "c": ()})
    assert plan.levels() == [["a", "b", "c"]]


async def test_run_plan_executes_in_topological_order():
    """用记录执行顺序的假 runner 断言：前驱永远先完成。"""
    order: list[str] = []
    plan = _plan({"a": (), "b": ("a",), "c": ("a",), "d": ("b", "c")})
    report = await run_plan(plan, runner=_runner(order))

    assert report.ok is True
    assert report.order[0] == "a"
    assert set(report.order[1:3]) == {"b", "c"}
    assert report.order[-1] == "d"
    assert order == report.order  # 假 runner 看到的顺序 = 账本记的顺序
    # 依赖关系逐条核对：任何前驱都必须比它的下游更早出现
    for task in plan.tasks:
        for dep in task.depends_on:
            assert order.index(dep) < order.index(task.id)


async def test_same_level_runs_concurrently():
    """同层并发：三个各 40 ms 的节点，总耗时远小于串行的 120 ms。"""
    order: list[str] = []
    plan = _plan({"a": (), "b": (), "c": ()})
    runner = DelegateRunner(
        runner_factory=lambda spec, budget: Recorder(spec.goal, order, delay=0.04)
    )
    loop = asyncio.get_running_loop()
    started = loop.time()
    report = await run_plan(plan, runner=runner)
    elapsed = loop.time() - started
    assert report.ok is True
    assert elapsed < 0.12, f"同层应当并发，实际耗时 {elapsed:.3f}s"


# ── 环依赖 ──────────────────────────────────────────────────────────────────


def test_cycle_is_detected_and_named():
    plan = _plan({"a": ("b",), "b": ("a",)})
    with pytest.raises(SwarmError) as excinfo:
        plan.levels()
    message = str(excinfo.value)
    assert "环" in message
    assert "a" in message and "b" in message


async def test_run_plan_raises_before_running_anything_on_cycle():
    """环在**执行之前**就被拒——不能跑一半才卡住。"""
    order: list[str] = []
    plan = _plan({"a": ("c",), "b": ("a",), "c": ("b",)})
    with pytest.raises(SwarmError):
        await run_plan(plan, runner=_runner(order))
    assert order == [], "有环时一个节点都不该跑"


def test_self_dependency_is_rejected():
    plan = _plan({"a": ("a",)})
    with pytest.raises(SwarmError, match="依赖自己"):
        plan.levels()


def test_unknown_dependency_is_rejected():
    plan = _plan({"a": ("ghost",)})
    with pytest.raises(SwarmError, match="不存在"):
        plan.levels()


def test_duplicate_id_is_rejected():
    plan = SwarmPlan(
        tasks=(
            SwarmTask(id="a", spec=DelegateSpec(goal="a")),
            SwarmTask(id="a", spec=DelegateSpec(goal="b")),
        )
    )
    with pytest.raises(SwarmError, match="重复"):
        plan.levels()


# ── 失败隔离 ────────────────────────────────────────────────────────────────


async def test_single_failure_does_not_stop_the_batch():
    order: list[str] = []
    plan = _plan({"a": (), "b": (), "c": ()})
    report = await run_plan(plan, runner=_runner(order, failing={"b"}))

    assert report.statuses["a"] == STATUS_OK
    assert report.statuses["b"] == STATUS_FAILED
    assert report.statuses["c"] == STATUS_OK, "一个节点失败不许把整批带崩"
    assert report.ok is False
    assert report.failed == ["b"]
    assert "炸了" in report.reasons["b"]


async def test_downstream_of_failure_is_skipped_with_reason():
    order: list[str] = []
    plan = _plan({"a": (), "b": ("a",), "c": ("b",)})
    report = await run_plan(plan, runner=_runner(order, failing={"a"}))

    assert report.statuses["a"] == STATUS_FAILED
    assert report.statuses["b"] == STATUS_SKIPPED
    assert report.statuses["c"] == STATUS_SKIPPED
    assert report.skipped == ["b", "c"]
    assert "a" in report.reasons["b"] and "前驱" in report.reasons["b"]
    # 被跳过的节点真的没跑
    assert order == ["a"]
    # 被跳过的节点没有结果对象（没跑过就不该伪造一份）
    assert "b" not in report.results


async def test_exception_inside_node_is_recorded_as_failure():
    class Boom:
        async def run(self, goal: str, **kwargs):
            raise RuntimeError("连不上了")

        def partial_output(self) -> str:
            return ""

    plan = _plan({"a": (), "b": ()})
    runner = DelegateRunner(
        runner_factory=lambda spec, budget: Boom() if spec.goal == "a" else Recorder(spec.goal, [])
    )
    report = await run_plan(plan, runner=runner)
    assert report.statuses["a"] == STATUS_FAILED
    assert "连不上了" in report.reasons["a"]
    assert report.statuses["b"] == STATUS_OK


async def test_unwired_runner_fails_each_node_without_raising():
    """没注入 runner 时：每个节点都是 failed，但 run_plan 本身不炸。"""
    plan = _plan({"a": (), "b": ("a",)})
    report = await run_plan(plan, runner=DelegateRunner())
    assert report.statuses["a"] == STATUS_FAILED
    assert report.statuses["b"] == STATUS_SKIPPED
    assert report.ok is False


# ── 计划构造与报告 ──────────────────────────────────────────────────────────


def test_from_specs_assigns_stable_ids():
    plan = SwarmPlan.from_specs(
        [DelegateSpec(goal="查资料"), DelegateSpec(goal="写报告")],
        depends_on={"t1": ("t0",)},
    )
    assert plan.ids() == ["t0", "t1"]
    assert plan.by_id()["t1"].depends_on == ("t0",)
    assert plan.levels() == [["t0"], ["t1"]]


def test_report_to_dict_is_json_shaped():
    report_result = DelegateResult(ok=True, output="ok", child_task_id="c1")
    from app.swarm import SwarmReport

    report = SwarmReport(
        results={"a": report_result},
        statuses={"a": STATUS_OK},
        order=["a"],
        levels=[["a"]],
    )
    payload = report.to_dict()
    assert payload["ok"] is True
    assert payload["order"] == ["a"]
    assert payload["results"]["a"]["output"] == "ok"
    assert set(payload["levels"][0]) == {"a"}
