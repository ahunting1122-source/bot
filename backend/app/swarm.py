"""批量并发（swarm）：拓扑序执行 + 同层并发 + 失败隔离。

对标 Hermes 的 ``agent_swarm``（模板化批量派生子 agent，``items`` 上限 128）与
``swarm_scheduler``（突发 5 个、之后每 700 ms 起 1 个、429 退避 3s×2^(n-1)——
见 ``handoff/hermes-parity/research/09-core-loop-layer.md`` §4.2）。Orbit 这一轮
只做**调度模型**里最要紧的三件事，限流相位留给将来接真 provider 时再说：

1. **拓扑序**：``depends_on`` 声明的前驱必须先完成。同层并发、有依赖的等前驱。
2. **环依赖必须报错**，而不是死锁。Kahn 拓扑排序剩下来的节点就是环上的节点，
   直接抛 ``SwarmError`` 并把它们列出来——一个环跑到超时才发现，排查成本高得多。
3. **单个失败不中断整批**：失败记 ``failed``，其余继续；依赖它的下游标 ``skipped``
   并写明原因。这条来自 Hermes 的限流/失败规则："用户取消保留已完成结果、把在飞/
   未启标为 aborted，非用户取消整批拒绝；单任务超时只失败该任务、不进入限流相位"
   （【实证】``02-tool-layer.md`` §8 引 ``swarm_scheduler``）。

为什么**同层屏障**而不是"前驱一完成就起后继"：屏障的调度语义更容易解释和断言
（``SwarmReport.levels`` 就是实际执行的批次），而省下的那点时间在本地单机场景
可以忽略。真要改成无屏障流水线，只需把 :func:`run_plan` 里的 ``gather`` 换成
按完成事件驱动的循环，``levels()`` 仍然复用。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from app.delegate import DelegateResult, DelegateRunner, DelegateSpec

log = logging.getLogger("orbit.swarm")

#: 节点状态。``skipped`` 是"没跑，因为前驱没成"——它**不是**失败，
#: 但也不该算成功，所以单独一档（否则"整批是否成功"只能靠猜）。
STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


class SwarmError(RuntimeError):
    """计划本身不合法（重名、未知依赖、成环）。**在执行之前**就抛。"""


@dataclass(frozen=True)
class SwarmTask:
    """一个 swarm 节点：id + 委派规格 + 依赖。"""

    id: str
    spec: DelegateSpec
    #: 必须先完成的节点 id。空 = 同层即起。
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class SwarmPlan:
    """一批带依赖关系的委派任务。

    ``DelegateSpec`` 自己没有 id（它描述的是"怎么跑一个子任务"），而依赖关系需要
    稳定引用，所以节点包一层 :class:`SwarmTask`。想省事就直接
    :meth:`from_specs`：按顺序给 ``t0, t1, …`` 当 id，依赖用 id 写。
    """

    tasks: tuple[SwarmTask, ...] = ()

    @classmethod
    def from_specs(
        cls,
        specs: Any,
        depends_on: dict[str, tuple[str, ...]] | None = None,
        *,
        prefix: str = "t",
    ) -> SwarmPlan:
        """从一串 ``DelegateSpec`` 造计划；id 默认 ``t0..tN``。"""
        deps = depends_on or {}
        tasks = tuple(
            SwarmTask(id=f"{prefix}{index}", spec=spec, depends_on=tuple(deps.get(f"{prefix}{index}", ())))
            for index, spec in enumerate(specs)
        )
        return cls(tasks=tasks)

    # ── 结构查询 ────────────────────────────────────────────────────────────
    def by_id(self) -> dict[str, SwarmTask]:
        return {task.id: task for task in self.tasks}

    def ids(self) -> list[str]:
        return [task.id for task in self.tasks]

    def validate(self) -> None:
        """计划自洽性检查：重名 / 空 id / 未知依赖 / 自依赖。"""
        seen: set[str] = set()
        for task in self.tasks:
            if not task.id.strip():
                raise SwarmError("swarm 节点 id 不能为空")
            if task.id in seen:
                raise SwarmError(f"swarm 节点 id 重复：{task.id}")
            seen.add(task.id)
        for task in self.tasks:
            for dep in task.depends_on:
                if dep == task.id:
                    raise SwarmError(f"swarm 节点 {task.id} 依赖自己：这不可能是笔误以外的东西")
                if dep not in seen:
                    raise SwarmError(f"swarm 节点 {task.id} 依赖了不存在的节点：{dep}")

    def levels(self) -> list[list[str]]:
        """按 Kahn 拓扑排序分层：第 i 层可以在前一层全部完成后并发跑。

        成环时抛 ``SwarmError``（而不是返回一个残缺的分层，让执行阶段卡死）。
        """
        self.validate()
        pending = {task.id: set(task.depends_on) for task in self.tasks}
        order = self.ids()
        levels: list[list[str]] = []
        while pending:
            ready = [node for node in order if node in pending and not pending[node]]
            if not ready:
                # 剩下的每个节点都还有未满足的前驱 → 它们必然在环上（或环的下游）
                raise SwarmError(
                    "swarm 计划里有环依赖："
                    + "、".join(sorted(pending))
                    + "。拓扑排序无法完成——环会让节点互相等待，必须显式拒绝而不是等它超时。"
                )
            levels.append(ready)
            for node in ready:
                del pending[node]
            for deps in pending.values():
                deps.difference_update(ready)
        return levels


@dataclass
class SwarmReport:
    """一次 swarm 执行的完整账。"""

    #: 节点 id → 结果（``skipped`` 的节点没有结果）。
    results: dict[str, DelegateResult] = field(default_factory=dict)
    #: 节点 id → ok / failed / skipped。
    statuses: dict[str, str] = field(default_factory=dict)
    #: 节点 id → 人话原因（失败原因 / 被跳过原因）。
    reasons: dict[str, str] = field(default_factory=dict)
    #: **实际完成顺序**（用于断言拓扑序；同层内顺序不保证）。
    order: list[str] = field(default_factory=list)
    #: 实际执行的批次（= ``plan.levels()``，留档便于对照）。
    levels: list[list[str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """整批是否全绿：没有任何 failed（skipped 由 failed 引起，故已隐含）。"""
        return all(status == STATUS_OK for status in self.statuses.values())

    @property
    def failed(self) -> list[str]:
        return [node for node, status in self.statuses.items() if status == STATUS_FAILED]

    @property
    def skipped(self) -> list[str]:
        return [node for node, status in self.statuses.items() if status == STATUS_SKIPPED]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "statuses": dict(self.statuses),
            "reasons": dict(self.reasons),
            "order": list(self.order),
            "levels": [list(level) for level in self.levels],
            "results": {node: result.to_dict() for node, result in self.results.items()},
        }


async def run_plan(plan: SwarmPlan, *, runner: DelegateRunner | None = None) -> SwarmReport:
    """按拓扑序跑完整个计划，返回账本。

    ``runner`` 默认新建一个（上限取默认值）；批内并发由 ``runner.max_concurrent``
    管，这里不再叠一层信号量——两处各卡一次只会让"为什么只有 2 个在跑"更难解释。
    """
    active = runner or DelegateRunner()
    levels = plan.levels()  # 环/重名/未知依赖在这里就抛，不会跑一半才发现
    by_id = plan.by_id()
    report = SwarmReport(levels=[list(level) for level in levels])

    for level in levels:
        runnable: list[str] = []
        for node in level:
            blocker = _first_blocking_dep(by_id[node], report)
            if blocker is not None:
                report.statuses[node] = STATUS_SKIPPED
                report.reasons[node] = (
                    f"前驱 {blocker} {report.statuses.get(blocker, STATUS_FAILED)}"
                    f"（{report.reasons.get(blocker) or '无进一步原因'}）："
                    "依赖没完成，下游跑起来只会浪费预算"
                )
                log.info("swarm 跳过 %s：%s", node, report.reasons[node])
                continue
            runnable.append(node)

        if not runnable:
            continue

        # 同层并发；每层的兄弟数参与 token 预算均分（父剩余 50% ÷ 本批个数）。
        siblings = len(runnable)
        results = await asyncio.gather(
            *(_run_one(active, by_id[node].spec, siblings) for node in runnable)
        )
        for node, result in zip(runnable, results, strict=True):
            report.results[node] = result
            report.statuses[node] = STATUS_OK if result.ok else STATUS_FAILED
            if result.error:
                report.reasons[node] = result.error
            report.order.append(node)

    return report


async def _run_one(
    runner: DelegateRunner, spec: DelegateSpec, siblings: int
) -> DelegateResult:
    """跑一个节点；异常一律折成失败结果（单个节点不许把整批带崩）。"""
    try:
        return await runner.run(spec, siblings=siblings)
    except asyncio.CancelledError:
        raise  # 取消是控制流，不是"这个节点失败了"
    except Exception as exc:  # noqa: BLE001 —— 故意兜全部：批量任务里一条异常不该中断整批
        log.warning("swarm 节点异常：%s", exc)
        return DelegateResult(
            ok=False,
            error=f"子任务异常：{exc}",
            status="failed",
            exit_reason="error",
        )


def _first_blocking_dep(task: SwarmTask, report: SwarmReport) -> str | None:
    """返回第一个"没成"的前驱 id（``None`` = 前驱全绿，可以跑）。"""
    for dep in task.depends_on:
        if report.statuses.get(dep) != STATUS_OK:
            return dep
    return None
