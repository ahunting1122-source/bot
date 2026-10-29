"""`delegate_task` / `agent_swarm` 两个工具（MATRIX 5.14 最后 2 条「有机制缺包装」）。

## 为什么现在才能写

它们一直是「有机制、缺包装」：`app/delegate.py` 的调度与 `app/swarm.py` 的拓扑序
都写好了，但 **`ChildRunner` 协议没有任何实现**——`DelegateRunner()` 一跑就抛
「未注入子任务 runner」。所以「包一层 `Tool`」这个说法是错的：
**缺的是 runner，不是包装。** 本轮先补了 `app/child_runner.py`，这两个工具才有意义。

## 注册一个调不动的工具比不注册更糟

这条是本模块的存在前提。所以：

- **构造器没注入就拒绝注册**（`register_delegate_tools` 返回空列表并告警），
  而不是注册一个必然抛错的工具；
- 执行时 `DELEGATION_DEPTH` 超限就**明确拒绝**，并说清是哪一层。

## 为什么注入的是「构造器」而不是「一个 runner」

子任务必须落在**父任务所在的会话**里，而 `conversation_id` 是每个父任务不同的。
注入一个现成 `DelegateRunner` 就得在构造时定死一个会话——那会把所有子任务
塞进同一个会话，父任务之间互相串味。所以注入 `(conversation_id) -> DelegateRunner`。

## 两个工具的分工

- `delegate_task`：**一个**子任务，等它跑完，交回文本。
- `agent_swarm`：**一批**子任务，按 `depends_on` 拓扑序执行、同层并发、失败隔离。

它们不是同一个东西的两种写法：`swarm` 的价值在**依赖关系**，
串行跑一遍 `delegate_task` 表达不了「B 要等 A」。
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import Field

from app.tools import Args, Tool

log = logging.getLogger("orbit.tool_delegate")

#: 装配层注入的 store（要按 `task_id` 查会话）。
_STORE: Any = None

#: 装配层注入的**委派器构造器**：`(conversation_id) -> DelegateRunner`。
_BUILDER: Any = None

#: `父 task_id -> 这个父任务用过的 DelegateRunner`。
#:
#: **为什么必须按父任务留一份**：父任务被取消时（`AgentRunner.cancel`），
#: 要能找到它派出去的子任务并一并停掉。不留这份，父停了子还在跑——
#: 烧钱、占租约，而且父任务早已结束，那些产出没有任何人读。
_RUNNERS: dict[str, Any] = {}

#: 同时最多派几个。Hermes 的 `agent_swarm` 上限 128，但那要求先有 128 份模型配额；
#: 这里取一个能跑完的数，**超了明确拒绝而不是排队**——排到超时比直接说不更糟。
MAX_SWARM_ITEMS = 16


class DelegateArgs(Args):
    """`delegate_task` 的参数。

    刻意只有 `goal` 与 `budget_steps`：`depth` 由**运行时**记（见 `child_runner`），
    让调用方声明深度等于把递归闸门交给它自己关。
    """

    goal: str = Field(min_length=1, description="交给子 agent 的完整目标，要能独立看懂")
    budget_steps: int = Field(default=0, ge=0, le=500, description="步数预算，0 = 用默认")


class SwarmItem(Args):
    """`agent_swarm` 里的一条子任务。"""

    id: str = Field(min_length=1, description="这条子任务的唯一名字，供 depends_on 引用")
    goal: str = Field(min_length=1, description="这条子任务的目标")
    depends_on: list[str] = Field(
        default_factory=list, description="必须先完成的子任务 id 列表"
    )


class SwarmArgs(Args):
    """`agent_swarm` 的参数。"""

    tasks: list[SwarmItem] = Field(min_length=1, description="子任务列表（按依赖关系组织）")


DELEGATE_TOOLS: list[Tool] = [
    Tool(
        "delegate_task",
        "把一个可独立完成的目标交给子 agent 执行，等它跑完并交回结果。"
        "适合「需要多步工具调用、但中间过程不必占用当前上下文」的活。",
        DelegateArgs,
    ),
    Tool(
        "agent_swarm",
        "批量派生子 agent：按 depends_on 拓扑序执行，同层并发，单个失败不影响其它。"
        "适合「若干互不依赖或部分依赖的子任务」——串行调用 delegate_task 表达不了依赖关系。",
        SwarmArgs,
    ),
]


def configure(store: Any, builder: Any) -> None:
    """装配层注入 `store` 与委派器构造器 `builder(conversation_id) -> DelegateRunner`。"""
    global _STORE, _BUILDER
    _STORE = store
    _BUILDER = builder


def reset() -> None:
    global _STORE, _BUILDER
    _STORE = None
    _BUILDER = None
    _RUNNERS.clear()


async def _runner_for(task_id: str) -> Any:
    """按父任务的会话建一个委派器。"""
    from app.storage import Task

    if _BUILDER is None or _STORE is None:
        raise RuntimeError(
            "委派器没接线：需要装配层调 `tool_delegate.configure(store, builder)`"
        )
    task = await _STORE.get(Task, task_id)
    if task is None:
        raise RuntimeError(f"找不到任务 {task_id}，无法确定子任务该落在哪个会话")
    runner = _BUILDER(task.conversation_id)
    # 按父任务留一份，供 `cancel_children` 在父被取消时找到它。
    _RUNNERS[task_id] = runner
    return runner


async def cancel_children(task_id: str) -> int:
    """停掉这个父任务派出去的所有子任务，返回停掉了几个（④ 中断传播）。

    **由 `AgentRunner.cancel` 调用**。不这么做的话，父任务被取消后子任务会
    继续跑到自己预算耗尽：烧钱、占租约，而且父已经结束，产出没人读。

    失败**不抛**——取消路径上再抛异常只会让用户看到第二个错误，
    而真正的取消已经发生了。但会留一条 warning。
    """
    runner = _RUNNERS.pop(task_id, None)
    if runner is None:
        return 0
    stopped = 0
    try:
        for child_id in list(getattr(runner, "_children", {}) or {}):
            try:
                await runner.cancel(child_id)
                stopped += 1
            except Exception as exc:  # noqa: BLE001 —— 见 docstring
                log.warning("停子任务 %s 失败：%s", child_id, exc)
    except Exception as exc:  # noqa: BLE001
        log.warning("遍历子任务失败（父任务 %s）：%s", task_id, exc)
    return stopped


def _guard_depth() -> None:
    """递归闸门：深度由运行时记，不由调用方声明。

    `DelegateRunner.max_depth` 那一层管的是 `DelegateSpec.depth`，而 `depth` 是
    调用方填的——工具执行器如果永远填 1，那层闸门永远不触发。
    **所以这里必须自己看 `DELEGATION_DEPTH`。**
    """
    from app.child_runner import DELEGATION_DEPTH

    depth = DELEGATION_DEPTH.get()
    if depth >= 2:
        raise ValueError(
            f"委派深度已达上限（当前第 {depth} 层）：子 agent 不能再派子 agent。"
            "要拆得更细，请把目标写在父任务里一次做完。"
        )


async def execute(
    name: str, args: dict[str, Any], task_id: str, operation_id: str
) -> dict[str, Any]:
    """唯一分发点。**异常不吞**——由 `ToolRegistry.execute` 包成 error 载荷。"""
    runner = await _runner_for(task_id)
    _guard_depth()

    if name == "delegate_task":
        result = await runner.run(
            str(args["goal"]), budget_steps=int(args.get("budget_steps") or 0)
        )
        return {
            "ok": bool(result.ok),
            "output": result.output,
            "error": result.error,
            "steps": result.steps,
            "tokens": result.tokens,
            "child_task_id": result.child_task_id,
            "exit_reason": result.exit_reason,
        }

    if name == "agent_swarm":
        return await _run_swarm(args, runner)

    raise ValueError(f"未知的委派工具：{name}")


async def _run_swarm(args: dict[str, Any], runner: Any) -> dict[str, Any]:
    from app import swarm
    from app.delegate import DelegateSpec

    items = list(args.get("tasks") or [])
    if len(items) > MAX_SWARM_ITEMS:
        raise ValueError(
            f"一次最多派 {MAX_SWARM_ITEMS} 个子任务（给了 {len(items)}）。"
            "**明确拒绝而不是排队**：排到超时比直接说不更糟。"
        )
    # `SwarmTask` 收的是 `spec: DelegateSpec` 而不是裸 goal——目标是 `DelegateSpec`
    # 的字段（第一版猜错过）。`depends_on` 才是「B 要等 A」的表达，
    # 也是 swarm 相对串行 delegate 的全部价值。
    plan = swarm.SwarmPlan(
        tasks=tuple(
            swarm.SwarmTask(
                id=str(item["id"]),
                spec=DelegateSpec(goal=str(item["goal"])),
                depends_on=tuple(str(dep) for dep in item.get("depends_on") or ()),
            )
            for item in items
        )
    )
    # **复用同一个 runner**：并发上限、深度闸、`subagent_start/stop` 发射点都在它里面，
    # 另起一个会绕过这些闸门。
    report = await swarm.run_plan(plan, runner=runner)
    # 直接用 `SwarmReport` 自己的 `to_dict()` / `ok`——自己拼一遍属性名迟早对不上。
    body = report.to_dict()
    body["ok"] = bool(report.ok)
    return body


def register_delegate_tools(
    registry: Any, *, store: Any = None, builder: Any = None
) -> list[str]:
    """把两个委派工具注册进 `ToolRegistry`，返回**真的注册成功**的名字。

    **构造器没注入就一个都不注册**并告警——注册一个必然抛错的工具，
    会让模型以为能用、用户以为坏了。
    """
    if builder is not None:
        configure(store, builder)
    if _BUILDER is None:
        log.warning(
            "委派器没注入，`delegate_task` / `agent_swarm` **不注册**"
            "（注册一个调不动的工具比不注册更糟）"
        )
        return []
    try:
        registry.register(DELEGATE_TOOLS, execute, source="delegate")
    except Exception as exc:  # noqa: BLE001 —— 注册失败要说出来，但不拦启动
        log.warning("委派工具注册失败：%s", exc)
        return []
    return [tool.name for tool in DELEGATE_TOOLS if tool.name in registry.tools]


__all__ = [
    "DELEGATE_TOOLS",
    "cancel_children",
    "MAX_SWARM_ITEMS",
    "configure",
    "execute",
    "register_delegate_tools",
    "reset",
]
