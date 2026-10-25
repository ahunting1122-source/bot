import asyncio
import logging
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from sqlalchemy import select

from app.graph import build_graph
from app.graph.nodes import ModelFailureError, Nodes, register_ask_user
from app.policies import Policies
from app.storage import Message, Operation, Task

log = logging.getLogger("orbit.agent")


class AgentRunner:
    """队列、worker、取消与生命周期；链路本身在 app/graph/ 里。"""

    def __init__(self, settings, store, registry, model=None, summaries=None, policies=None):
        self.settings, self.store, self.registry = settings, store, registry
        self.model = model
        self.summaries = summaries
        self.policies = policies or Policies.default(settings, store)
        self.nodes = Nodes(self)
        self.partial: dict[str, str] = {}
        #: 每步模型调用的 token 计量（估算值）：{task_id: {prompt_tokens, completion_tokens, elapsed}}。
        #: 只在收尾时汇总进 task_completed 事件，流式途中不落库（避免每帧一次写盘）。
        self._token_usage: dict[str, dict] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.queued: set[str] = set()
        self.current: dict[str, asyncio.Task] = {}
        register_ask_user(registry)

    def takeover_allowed(self, state, args) -> str:
        """保留在 Runner 上的门面：接管门槛的判定实现在 gate 节点里。"""
        return self.nodes.takeover_allowed(state, args)

    async def start(self):
        self.checkpoint_context = AsyncSqliteSaver.from_conn_string(
            str(self.settings.data_dir / "checkpoints.sqlite")
        )
        self.checkpointer = await self.checkpoint_context.__aenter__()
        self.graph = build_graph(self).compile(checkpointer=self.checkpointer)
        for task in await self.store.rows(
            select(Task).where(Task.status.in_(["PENDING", "RUNNING"]))
        ):
            if task.status == "PENDING":
                self.queued.add(task.id)
                await self.queue.put(task.id)
            else:
                await self.store.transition(
                    task.id,
                    "WAITING_USER",
                    waiting={
                        "reason": "recovery_check",
                        "question": "程序上次执行时退出。继续后会核对操作记录，浏览器将重新观察。",
                    },
                )
        await self._calibrate_vectors()
        self.worker = asyncio.create_task(self.work())

    async def _calibrate_vectors(self) -> None:
        """启动时校准向量表：维度/模型与配置不符就重建并留 warning（见 vectors.ensure_schema）。

        不做这一步的后果是"检索恒空"：vec0 的维度建表时钉死，而 `embedding_dim` 是配置项，
        换模型后如果没人重建表，写入失败、检索 0 召回，却只在 debug 日志里可见。
        """
        ensure = getattr(self.policies.vectors, "ensure_schema", None)
        if ensure is None:  # NullVectorStore：向量路未开启
            return
        try:
            if not await ensure():
                log.warning("向量表校准失败，向量路将退化为空结果")
        except Exception as exc:  # 校准失败不该拖垮启动
            log.warning("向量表校准异常（不影响其它功能）：%s", exc)

    async def close(self):
        self.worker.cancel()
        for task in self.current.values():
            task.cancel()
        await asyncio.gather(self.worker, *self.current.values(), return_exceptions=True)
        await self.checkpoint_context.__aexit__(None, None, None)

    async def enqueue(self, task_id: str, resume: dict | None = None):
        if task_id in self.queued or task_id in self.current:
            return
        await self.store.transition(
            task_id, "PENDING", waiting={"resume": resume} if resume else {}
        )
        self.queued.add(task_id)
        await self.queue.put(task_id)

    async def cancel(self, task_id: str):
        job = self.current.get(task_id)
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def _record_skill_outcome(self, task: Task, status: str) -> None:
        """任务终态回写技能治理（降级淘汰）。

        口径 B：只对本次被 skill_load 的技能记账——从 tool_calls 表查本任务的
        skill_load 调用，取 args.name。治理失败只记日志，绝不影响任务本身。
        """
        try:
            ops = await self.store.rows(
                select(Operation).where(
                    Operation.task_id == task.id, Operation.name == "skill_load"
                )
            )
            used = []
            for op in ops:
                args = op.args or {}
                name = args.get("name")
                if name:
                    used.append(str(name))
            if not used:
                return
            await self.policies.skills.record_outcome(self.store, status, used)
        except Exception as exc:  # 治理回写失败不拖垮任务
            from app.graph.nodes import log

            log.warning("技能治理回写失败（忽略）：%s", exc)

    async def work(self):
        while True:
            task_id = await self.queue.get()
            self.queued.discard(task_id)
            task = await self.store.get(Task, task_id)
            if not task or task.status == "CANCELLED":
                self.queue.task_done()
                continue
            job = asyncio.create_task(self.run(task))
            self.current[task_id] = job
            try:
                await job
            except asyncio.CancelledError:
                worker = asyncio.current_task()
                if worker is not None and worker.cancelling():
                    raise
            finally:
                self.current.pop(task_id, None)
                self.queue.task_done()

    async def run(self, task):
        config: RunnableConfig = {"configurable": {"thread_id": task.id}, "recursion_limit": 200}
        # 记下摘要台账当前位置（M5）：任务结束后把这段时间里产生的摘要开销
        # 补记到任务预算上，否则"摘要另起一次模型调用"的花费永远在账外。
        ledger_mark = self._ledger_index()
        try:
            resume = task.waiting.get("resume")
            await self.store.transition(task.id, "RUNNING", waiting={}, error="")
            # 模型名随事件落库：换了模型之后回看历史任务能分清是哪次改的。
            await self.store.event(task.id, "task_started", model=str(self.settings.model or ""))
            snapshot = await self.graph.aget_state(config)
            if not snapshot.values:
                initial: Any = {
                    "task_id": task.id,
                    "messages": [],
                    "actions": 0,
                    "calls": [],
                    "waiting": {},
                    "answer": {},
                }
            elif snapshot.interrupts:
                initial = Command(resume=resume or {"approved": True, "answer": "继续"})
            else:
                initial = None
            result = await self.graph.ainvoke(initial, config=config)
            snapshot = await self.graph.aget_state(config)
            if snapshot.interrupts:
                waiting = snapshot.interrupts[0].value
                await self.store.transition(task.id, "WAITING_USER", waiting=waiting)
                await self.store.event(task.id, "waiting_user", **waiting)
                return
            # finalize 的产出才是这一轮的回答；状态里的最后一条消息只作兜底。
            values = snapshot.values or (result if isinstance(result, dict) else {})
            final = values.get("final") or {}
            text = str(final.get("text") or "")
            if not text:
                messages = values.get("messages") or []
                text = messages[-1].text if messages else ""
            # Stable IDs prevent duplicate final messages if completion is replayed.
            await self.store.save(
                Message(
                    id=f"answer-{task.id}",
                    conversation_id=task.conversation_id,
                    role="assistant",
                    content=text,
                )
            )
            await self.store.transition(task.id, "SUCCEEDED")
            usage = self._token_usage.pop(task.id, {})
            await self.store.event(task.id, "task_completed", text=text, token_usage=usage)
            await self._record_skill_outcome(task, "SUCCEEDED")
        except asyncio.CancelledError:
            await self.save_partial(task)
            raise
        except Exception as exc:
            await self.save_partial(task)
            messages = {
                "AuthenticationError": "模型密钥未通过验证，请检查连接设置。",
                "PermissionDeniedError": "模型服务拒绝访问，请检查账户权限。",
                "RateLimitError": "模型服务当前限流或余额不足，请稍后重试或检查账户。",
                "APITimeoutError": "模型响应超时，可以重试这次回复。",
                "APIConnectionError": "暂时无法连接模型服务，请检查 API 地址和网络。",
            }
            message = messages.get(type(exc).__name__, "回复中断，可以重试或检查连接设置。")
            if isinstance(exc, ModelFailureError):
                message = str(exc)
            if not self.settings.model_api_key.get_secret_value() and self.model is None:
                message = "请先在连接与设置中填写模型名称、API 地址和密钥。"
            await self.store.transition(task.id, "FAILED", error=message)
            await self.store.event(task.id, "task_failed", error=message)
            await self._record_skill_outcome(task, "FAILED")
        finally:
            self.partial.pop(task.id, None)
            self._token_usage.pop(task.id, None)
            if self.summaries:
                current = await self.store.get(Task, task.id)
                if current and current.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    self.summaries.schedule(task.conversation_id)
                # 摘要开销入账（M5）：发生在本次任务窗口内的摘要花费，落成事件并
                # 记到会话累计上——预算的"任务花销"从此包含摘要这一路。
                await self._record_summary_cost(task, ledger_mark)

    def _ledger_index(self) -> int:
        """摘要开销台账的当前位置。服务不支持记账（如测试替身）时返回 0，退化为不计。

        这里用 getattr 而不是直接调用：`summaries` 是注入点，测试里会用轻量替身，
        不该强迫每个替身都实现记账接口——没有台账就是"这部分开销暂不可见"，
        而不是让任务起不来。
        """
        if not self.summaries:
            return 0
        reader = getattr(self.summaries, "ledger_index", None)
        return int(reader()) if callable(reader) else 0

    async def _record_summary_cost(self, task: Task, ledger_mark: int) -> None:
        """把本次任务期间产生的摘要开销如实记进事件流与会话累计。"""
        reader = getattr(self.summaries, "cost_since", None)
        if not callable(reader):
            return
        try:
            delta = reader(ledger_mark)
        except Exception:  # 台账读失败不该影响任务收尾
            return
        if not delta.get("calls"):
            return
        try:
            await self.store.event(
                task.id,
                "summary_cost",
                calls=delta["calls"],
                tokens=delta["tokens"],
                elapsed=delta["elapsed"],
                scope="task_window",
            )
        except Exception:
            pass

    async def save_partial(self, task):
        text = self.partial.get(task.id, "")
        if text:
            await self.store.save(
                Message(
                    id=f"answer-{task.id}",
                    conversation_id=task.conversation_id,
                    role="assistant",
                    content=text,
                )
            )

    def get_model(self):
        if self.model is not None:
            return self.model
        if not self.settings.model or not self.settings.model_api_key.get_secret_value():
            raise ValueError("模型尚未配置")
        return ChatOpenAI(
            model=self.settings.model,
            base_url=self.settings.model_base_url,
            api_key=self.settings.model_api_key,
            timeout=60,
            max_retries=1,
        )
