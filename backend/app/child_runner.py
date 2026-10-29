"""真的子 agent runner（MATRIX 5.14 的 `delegate_task` / `agent_swarm` 的前置）。

## 为什么需要这个模块

`app/delegate.py` 把委派的**调度**写全了（深度/并发/预算三道闸、拓扑序、失败隔离、
`subagent_start`/`subagent_stop` 发射点），但它要一个
`ChildRunnerFactory = Callable[[DelegateSpec, ChildBudget], ChildRunner]`，
而 **`ChildRunner` 协议是空的**：

```python
class ChildRunner(Protocol):
    async def run(self, goal: str, **kwargs) -> dict[str, Any]: ...
    def partial_output(self) -> str: ...
```

`DelegateRunner()` 不传工厂时 `available=False`、`run()` 直接抛
`DelegateError("未注入子任务 runner…")`；`policies.DefaultSubAgentRunner` 是个
`available=False` 的桩。**所以整个子 agent 能力一直是空转的。**

## 实现方式：复用同一个编译好的图，换一个 `thread_id`

不另起一套 agent 循环，而是**复用父任务的 `AgentRunner.graph`**——
LangGraph 的 `thread_id` 天然隔离状态，子任务有自己的 `Task` 行，
所以事件、工件、工具调用都落在子任务名下，**不与父任务串味**。

这样做的三个好处：
1. **行为一致**：子任务和父任务走同一套 `prepare → think → gate → execute → digest →
   budget → finalize`，不会出现「子 agent 是另一套简化逻辑」；
2. **预算可核**：`budget` 节点按 `thread_id` 记 `turns`，子任务的步数是真的；
3. **不用复制代码**：`nodes.py` 里那一堆上下文装配/压缩/钩子，复制一份必然会漂。

## 递归怎么防

子任务和父任务共用同一个 `ToolRegistry`，所以子任务**也看得见**
`delegate_task` / `agent_swarm`。若不管，就是无限递归。

用 `ContextVar` 记当前委派深度（**不是全局变量**——`ContextVar` 在
`asyncio.ensure_future` 建的任务里会被继承，但各分支互不干扰）：

- 工厂在造子 runner 时把深度 +1；
- 工具执行时若 `DELEGATION_DEPTH >= max_depth` 就**拒绝**并说清原因。

**为什么不用 `DelegateRunner` 自己的 `max_depth`**：那一层管的是
「`DelegateSpec.depth` 有没有超」，而 `depth` 是**调用方填的**——
工具执行器如果永远填 1，那一层的闸门就永远不触发。**深度必须由运行时记，
不能由调用方声明。**
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any

log = logging.getLogger("orbit.child_runner")

#: 当前这条执行链上的委派深度。父任务 = 0，它派出的子 = 1。
#:
#: **必须是 `ContextVar` 而不是模块级变量**：一个进程里可能同时有多个父任务在跑
#: （`max_concurrent` 默认 4），模块级变量会让它们互相污染深度。
DELEGATION_DEPTH: ContextVar[int] = ContextVar("orbit_delegation_depth", default=0)


class ChildRunnerError(RuntimeError):
    """子 runner 造不出来 / 跑不起来。会被 `DelegateRunner` 包成一条失败结果。"""


class GraphChildRunner:
    """把一个目标跑成一次完整的 agent 循环，交回最终回答。

    `partial_output()` 交出**到目前为止**已经产生的文本——被取消时
    `DelegateRunner` 靠它做「中断补位」（永不返回空结果）。
    """

    def __init__(
        self,
        parent: Any,
        *,
        goal: str,
        conversation_id: str,
        depth: int,
        max_depth: int,
        budget_steps: int = 0,
    ) -> None:
        self._parent = parent
        self._goal = goal
        self._conversation_id = conversation_id
        self._depth = depth
        self._max_depth = max_depth
        self._budget_steps = budget_steps
        self._partial = ""
        self.child_task_id = ""

    def partial_output(self) -> str:
        return self._partial

    async def run(self, goal: str, **kwargs: Any) -> dict[str, Any]:
        """跑一轮。返回 `delegate._finish` 认的载荷形状。

        `output` / `error` / `exit_reason` / `steps` / `tokens` 这五个键是
        `_finish` 真正读的；多给的键它不看（不会报错，但也没用）。
        """
        from app.storage import Task

        store = self._parent.store
        settings = self._parent.settings
        task = await store.save(
            Task(conversation_id=self._conversation_id, goal=str(goal or self._goal))
        )
        self.child_task_id = task.id
        # 深度 +1 只在**这条执行链**上生效（`ContextVar` 的语义）。
        token = DELEGATION_DEPTH.set(self._depth)
        config: dict[str, Any] = {
            "configurable": {"thread_id": task.id},
            "recursion_limit": 200,
        }
        try:
            await store.transition(task.id, "RUNNING", waiting={}, error="")
            await store.event(task.id, "task_started", model=str(settings.model or ""),
                              delegated_by=self._conversation_id)
            initial: dict[str, Any] = {
                "task_id": task.id,
                "messages": [],
                "actions": 0,
                "calls": [],
                "waiting": {},
                "answer": {},
            }
            await self._parent.graph.ainvoke(initial, config=config)
            snapshot = await self._parent.graph.aget_state(config)
            values = snapshot.values or {}
            if snapshot.interrupts:
                # 子任务要问用户：**不替用户回答**，如实报上去。
                # 自己编一个答案会让父任务以为子任务完成了。
                waiting = snapshot.interrupts[0].value
                await store.transition(task.id, "WAITING_USER", waiting=waiting)
                return {
                    "output": "",
                    "error": f"子任务需要用户确认，已挂起：{waiting}",
                    "exit_reason": "waiting_user",
                }
            final = values.get("final") or {}
            text = str(final.get("text") or "")
            if not text:
                for message in reversed(values.get("messages") or []):
                    body = getattr(message, "content", "")
                    if isinstance(body, str) and body.strip():
                        text = body
                        break
            self._partial = text
            await store.transition(task.id, "DONE")
            return {
                "output": text,
                "steps": int(values.get("turns") or 0),
                "exit_reason": "completed" if text else "empty",
            }
        except Exception as exc:  # noqa: BLE001 —— 子任务失败不该炸父任务
            log.warning("子任务 %s 失败：%s", task.id, exc)
            try:
                await store.transition(task.id, "FAILED", error=str(exc)[:500])
            except Exception:  # noqa: BLE001
                pass
            return {"output": self._partial, "error": str(exc)[:500], "exit_reason": "error"}
        finally:
            DELEGATION_DEPTH.reset(token)


def graph_child_factory(
    parent: Any,
    *,
    conversation_id: str,
    depth: int,
    max_depth: int,
) -> Any:
    """造一个 `ChildRunnerFactory`：`(spec, budget) -> ChildRunner`。

    深度在这里 +1：`depth` 是**父任务这一层**的深度，子任务是 `depth + 1`。
    """

    def factory(spec: Any, budget: Any = None) -> GraphChildRunner:
        child_depth = depth + 1
        if child_depth > max_depth:
            # 在这里就拒，而不是让子任务跑起来再发现超深——
            # 那会白烧一轮模型调用。
            raise ChildRunnerError(
                f"委派深度超限：{child_depth} > {max_depth}（第 {child_depth} 层不再派生）"
            )
        return GraphChildRunner(
            parent,
            goal=str(getattr(spec, "goal", "") or ""),
            conversation_id=conversation_id,
            depth=child_depth,
            max_depth=max_depth,
            budget_steps=int(getattr(spec, "budget_steps", 0) or 0),
        )

    return factory


__all__ = [
    "DELEGATION_DEPTH",
    "ChildRunnerError",
    "GraphChildRunner",
    "graph_child_factory",
]
