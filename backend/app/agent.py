from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from pydantic import SecretStr
from sqlalchemy import select

from app import fallback as fb
from app import hooks, session_leases
from app import providers as pv
from app.graph import build_graph
from app.graph.nodes import ModelFailureError, Nodes, register_ask_user
from app.heartbeat import Heartbeat
from app.policies import Policies
from app.storage import Message, Operation, Task

log = logging.getLogger("orbit.agent")

#: 后台复审（MATRIX 2.5）：任务成功后追加的那一轮用户消息。
#: 前半句照抄 Hermes 的学习回路（research 09 §1.5 的 `Review the conversation above…`），
#: 后半句是给中文模型的行为约束——不约束它会写一整篇读后感。
BG_REVIEW_PROMPT = (
    "Review the conversation above and note anything worth remembering. "
    "只挑真正可复用的东西：可重复使用的做法、关于用户环境/偏好的稳定事实、"
    "以及不该再犯的错误。最多 5 条短句；如果什么都不值得记，只回复 none。"
)
#: 复审的系统提示。刻意**不复用**任务里的冻结前缀：复审是旁路，它要的是"局外人
#: 视角"，带上任务那套"你是执行者"的提示反而会让它继续干活而不是提炼经验。
BG_REVIEW_SYSTEM = (
    "你是一个复盘者，不执行任务、不调用工具。你的唯一工作是从上面这段对话里"
    "提炼值得长期记住的经验。没有就回复 none。"
)
#: 复审带多少条历史。20 条足够看清"做了什么、怎么成的、错在哪"，
#: 再多只是把成本推高——复审读的是全文，一条大工具结果就能吃掉几千 token。
BG_REVIEW_HISTORY = 20


@dataclass(frozen=True)
class Route:
    """这次**实际**用的是谁。

    存在的理由：回退链一旦接进运行时，"我配的模型"和"这轮真跑的模型"就不再是
    同一个东西。把这件事做成返回值的一部分（而不是只写进日志），调用方、事件流、
    测试都能直接断言，不需要去 grep 日志猜。
    """

    provider: str
    model: str
    base_url: str
    #: 钥匙的**标签**（`pool#2` / `primary`），永远不含明文。
    key_label: str = "primary"
    #: 第几次尝试成功（0 = 主目标第一把钥匙就成）。
    attempt: int = 0
    #: 首次失败的原因（没切换过时为空）。
    reason: str = ""

    def label(self) -> str:
        return f"{self.provider}/{self.model}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "key": self.key_label,
            "attempt": self.attempt,
            "switched": self.attempt > 0,
            "reason": self.reason,
        }


class _TargetSettings:
    """把"这一站的模型 / 端点 / 钥匙"包成 `providers.ModelSettings` 的最小实现。

    **为什么不直接传 `Settings`**：`Settings.model_base_url` 是**主模型**的端点，
    把它传给回退站会让回退请求打到主模型那家的地址上——一个静默的"回退到自己"。
    这里显式按站组装，端点与钥匙都只描述这一站。

    `model_api_key` 写成只读属性而不是普通字段：`providers.ModelSettings` 把它声明
    为只读属性（协变），普通字段（不变）在 mypy 里不满足协议。
    """

    __slots__ = ("_key", "model", "model_base_url", "model_provider")

    def __init__(self, *, model: str, base_url: str, provider: str, key: str) -> None:
        self.model = model
        self.model_base_url = base_url
        self.model_provider = provider
        self._key = key

    @property
    def model_api_key(self) -> SecretStr:
        return SecretStr(self._key)


def _resolved_model(target: fb.FallbackTarget) -> str:
    """链目没写模型名时用该家默认值——`Route` 里要显示真正在跑的模型名。"""
    if target.model:
        return target.model
    profile = pv.PROVIDERS.get(target.provider)
    return profile.default_model if profile is not None else ""


def _route_of(candidate: fb.Candidate, attempt: int, reason: str = "") -> Route:
    return Route(
        provider=candidate.target.provider,
        model=_resolved_model(candidate.target),
        base_url=candidate.target.base_url,
        key_label=candidate.key.label,
        attempt=attempt,
        reason=reason,
    )


class RoutedModel:
    """带容错的模型门面：属性透传给当前客户端，调用失败时沿链推进。

    ## 为什么必须包一层

    `nodes.py` 只做两件事：`get_model().bind_tools(specs)`，然后 `astream` / `ainvoke`。
    所以这里必须做到：

    1. **属性透传**，且 `astream` 只在底层客户端真的有流式接口时才暴露——`nodes.py`
       靠 `hasattr(model, "astream")` 区分"真模型走流式 / 脚本替身只有 ainvoke"，
       包一层就把这个判断弄成永远 True，脚本替身会当场崩。
    2. **调用时也能换站**：401/429 是调用时才发生的，构造阶段一切正常。
       只在构造时挑一次，等于回退链只在"配置写错"的时候有用。
    """

    def __init__(
        self,
        plan: fb.ModelPlan,
        client: Any,
        route: Route,
        *,
        binder: Callable[[Any], Any] | None = None,
        on_switch: Callable[[Route], None] | None = None,
    ) -> None:
        self._plan = plan
        self._client = client
        self._binder = binder
        self._on_switch = on_switch
        self.route = route

    # -- 透传 -------------------------------------------------------------
    def __getattr__(self, name: str) -> Any:
        # `astream` 是特例：底层没有流式接口时必须 AttributeError，否则 `hasattr`
        # 判断失真（见类 docstring 第 1 条）。
        if name == "astream" and hasattr(self._client, "astream"):
            return self._stream
        return getattr(self._client, name)

    def __repr__(self) -> str:
        return f"RoutedModel({self.route.label()}#{self.route.key_label})"

    # -- 绑定 -------------------------------------------------------------
    def bind_tools(self, tools: Any, **kwargs: Any) -> RoutedModel:
        """绑定工具后仍要保留容错能力：换站时对新客户端重新绑一次。"""
        return RoutedModel(
            self._plan,
            self._client.bind_tools(tools, **kwargs),
            self.route,
            binder=lambda client: client.bind_tools(tools, **kwargs),
            on_switch=self._on_switch,
        )

    # -- 推进 -------------------------------------------------------------
    def _swap(self, exc: BaseException) -> None:
        """换到下一站；耗尽则抛 `FallbackExhausted`（消息里带整条链的原因）。"""
        decision = fb.classify(exc)
        if decision == fb.ABORT:
            # 未知错误 / 编程错误：原样抛，保留类型与栈。重试只会把真 bug 掩盖成
            # "所有 provider 都挂了"。
            raise exc
        outcome = self._plan.advance(exc, decision)
        if outcome is None:
            raise fb.FallbackExhausted(self._plan.attempts) from exc
        client, candidate = outcome
        if self._binder is not None:
            client = self._binder(client)
        self._client = client
        self.route = _route_of(candidate, len(self._plan.attempts) - 1, fb.describe_error(exc))
        if self._on_switch is not None:
            self._on_switch(self.route)

    # -- 调用 -------------------------------------------------------------
    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        while True:
            try:
                return await self._client.ainvoke(messages, **kwargs)
            except Exception as exc:  # noqa: BLE001 —— 判据决定要不要换站
                self._swap(exc)

    async def _stream(self, messages: Any, **kwargs: Any) -> Any:
        """流式调用。

        **首块之前**失败可以换站重来；**已经吐出正文之后**绝不换——用户会看到两段
        拼接起来的答案，那比直接报错更糟（与 `FallbackChain` 只做非流式的理由一致）。
        底层没有 `astream` 时退化成一次 `ainvoke` 再整块吐出，保证换站后的新客户端
        也能用。
        """
        while True:
            if not hasattr(self._client, "astream"):
                yield await self.ainvoke(messages, **kwargs)
                return
            started = False
            try:
                async for chunk in self._client.astream(messages, **kwargs):
                    started = True
                    yield chunk
                return
            except Exception as exc:  # noqa: BLE001 —— 同上
                if started:
                    raise
                self._swap(exc)


class AgentRunner:
    """队列、worker、取消与生命周期；链路本身在 app/graph/ 里。"""

    def __init__(self, settings, store, registry, model=None, summaries=None, policies=None):
        self.settings, self.store, self.registry = settings, store, registry
        self.model = model
        self.summaries = summaries
        self.policies = policies or Policies.default(settings, store)
        # 心跳（MATRIX 2.3）：进程级一个，`Nodes` 各节点进入时 beat 一次，
        # CLI/事件流读 `status()` 就知道"卡在哪一步、多久没动静"。
        self.heartbeat = Heartbeat()
        #: 被要求中断的线程（任务 id）。语义见 `cancel_thread`：不在跑的工具直接跳过，
        #: **在跑的那一次也会被 `Nodes.run_tool` 轮询到并放弃**（唯一断不掉的是已经
        #: 派到线程/子进程里的同步活）。
        self.interrupted_threads: set[str] = set()
        #: 后台复审任务（MATRIX 2.5）。留在集合里只为一件事：`close()` 时要取消，
        #: 否则进程退出时它还在等一次模型调用，会把退出拖住。
        self._bg_reviews: set[asyncio.Task] = set()
        #: 延迟入队的等待者（见 `enqueue`）：job 收尾后替调用方重新入队。
        self._deferred: set[asyncio.Task] = set()
        self.nodes = Nodes(self)
        #: 会话租约（MATRIX 10.8）：**同一会话不允许两个任务同时跑**。
        #:
        #: 之前 `LeaseStore.acquire()` **零生产调用方**——`orbit plane` 只是构造它来
        #: 报告状态，所以 `runtime/active_sessions.json` 在生产里永远不会被创建。
        #: 后果：同一个会话被两条路径同时驱动（比如 CLI 一个任务没跑完又开一个，
        #: 或 gateway 与 CLI 撞上）时，**两边都在往同一份 checkpoint 写**，
        #: 消息历史会交错。这类损坏不报错，只会在事后表现为"对话内容莫名其妙"。
        #:
        #: 机制是**退让而非抢占**（`session_leases` 的 docstring）：`acquire` 后写者持有，
        #: 旧持有者下一次 `heartbeat` 会拿到 `LeaseError` 并自己停下。
        #: **本模块绝不 kill 进程**——单用户本地部署里"让后写的赢"比"等 90 秒租约过期"
        #: 更符合直觉，也不会把用户正在用的进程杀掉。
        self.leases = session_leases.LeaseStore(settings.data_dir)
        #: `task_id → conversation_id`：`budget` 节点只有 task_id，而租约按会话记。
        self._lease_sessions: dict[str, str] = {}
        #: `task_id -> 来源平台`（MATRIX 6.4）。工具暴露面按平台过滤
        #: （`toolsets.resolve` 的 `platforms.<name>.disabled/enabled`），
        #: 而**平台必须由驱动任务的那一层告诉运行时**——它自己猜不出来
        #: （`Task` / `Conversation` 都没有平台字段）。
        #: 没登记就是 `cli`：既有行为逐字不变。
        self._task_platform: dict[str, str] = {}
        self.partial: dict[str, str] = {}
        #: 每步模型调用的 token 计量（估算值）：{task_id: {prompt_tokens, completion_tokens, elapsed}}。
        #: 只在收尾时汇总进 task_completed 事件，流式途中不落库（避免每帧一次写盘）。
        self._token_usage: dict[str, dict] = {}
        #: 最近一次 `get_model()` 实际选中的站与钥匙。回退链接进运行时之后，
        #: "配的模型"和"真跑的模型"可能不是同一个——留在这里供事件流与排障读取。
        self.last_route: Route | None = None
        #: 最近一次规划里的全部尝试（耗尽时 `FallbackExhausted` 里也带一份）。
        self.last_attempts: list[fb.Attempt] = []
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
        # 后台复审是旁路：进程要退就直接取消，绝不为它多等一次模型调用。
        for review in list(self._bg_reviews):
            review.cancel()
        for waiter in list(self._deferred):
            waiter.cancel()
        await asyncio.gather(
            self.worker,
            *self.current.values(),
            *self._bg_reviews,
            *self._deferred,
            return_exceptions=True,
        )
        self._bg_reviews.clear()
        self._deferred.clear()
        await self.checkpoint_context.__aexit__(None, None, None)

    def platform_for(self, task_id: str) -> str:
        """该任务的来源平台（MATRIX 6.4）；没登记就是 `cli`（既有行为逐字不变）。"""
        return self._task_platform.get(task_id, "cli")

    async def enqueue(
        self, task_id: str, resume: dict | None = None, *, platform: str = "cli"
    ):
        """入队。**同一任务正在跑时不丢这次请求**，而是等它收尾后再入队。

        为什么不能直接 `return`（原来的写法）：任务进入 `WAITING_USER` 时，状态
        已经落库、但 job 还没从 `self.current` 里摘掉——中间还隔着事件写入与
        `finally` 里的摘要收尾。调用方看到"已等待用户"就立刻 `enqueue(..., resume)`，
        此时 `task_id in self.current` 仍为真，那次 enqueue 会被**静默丢弃**：
        用户明明回答了，任务却永远停在等待态。这是真实存在的竞态
        （`tests/test_graph.py::test_ask_user_still_interrupts_and_resumes` 偶发挂死）。

        用"等 job 结束再入队"而不是"阻塞调用方"：调用方可能是 HTTP 请求，
        不能为一个跑几分钟的任务一直挂着。
        """
        # 记下来源平台（MATRIX 6.4）。**放在最前面**：下面有两条早退分支
        # （任务在跑时延后入队、已排队时忽略），平台信息不该因为走了早退就丢——
        # 延后入队那条尤其重要，它会在原任务收尾后**重新**入队。
        self._task_platform[task_id] = str(platform or "cli")
        job = self.current.get(task_id)
        if job is not None and not job.done():
            deferred = asyncio.create_task(
                self._enqueue_after(job, task_id, resume, self._task_platform.get(task_id, platform))
            )
            self._deferred.add(deferred)
            deferred.add_done_callback(self._deferred.discard)
            return
        if task_id in self.queued:
            return
        await self.store.transition(
            task_id, "PENDING", waiting={"resume": resume} if resume else {}
        )
        self.queued.add(task_id)
        await self.queue.put(task_id)

    async def _enqueue_after(
        self, job: asyncio.Task, task_id: str, resume: dict | None, platform: str = "cli"
    ):
        """等当前 job 收尾后再入队。`job.done()` 之后不再判 `current`，否则会自激。

        **`platform` 必须由调用方捕获后传回来**（MATRIX 6.4 的一个真 bug 修在这里）。

        为什么不能在这个函数里读 `self._task_platform[task_id]`：入队路径的时序是

        1. `enqueue(platform="api")` **最前面**记下 `"api"`；
        2. 发现任务正在跑 → 走延后分支 → 本函数等它收尾；
        3. 原 job 收尾时 `finally` 里 `pop` 掉记录；
        4. **本函数才重新入队**——此时记录已经被删，读回来只能是默认 `cli`。

        所以必须在**第 2 步（记录还在的时候）**把平台捕获进闭包/参数里。
        **「把信息放在最前面」解决的是「早退时丢信息」，不解决「重新入队时用什么值」
        ——两个问题看起来是一个，实际是两步。**

        后果不是理论上的：**延后入队正是 `WAITING_USER` 恢复的主路径**——
        用户在 API 上回答澄清 → 恢复后平台被降级成 `cli` → 工具集跟着换掉，
        而且**发生在任务中途**。
        """
        await asyncio.gather(job, return_exceptions=True)
        await self.enqueue(task_id, resume, platform=platform)

    async def cancel(self, task_id: str):
        """取消一个任务，**并把它派出去的子任务一起停掉**（MATRIX 5.14 ④）。

        ## 为什么必须连带停子任务

        `DelegateRunner` 只在自己被 `cancel()` 时才停子任务，而父任务被取消走的是
        这里。**不连带的话，父停了子还在跑**：烧钱、占着会话租约，
        而且父任务早已结束，那些子任务的产出没有任何人读。

        调用顺序是**先停子再停父**：反过来的话父的 job 一 cancel，
        这条协程可能就被打断在 `await` 上，子任务就漏掉了。

        失败**不抛**——取消路径上再抛只会让用户看到第二个错误，
        而真正的取消已经发生了。`cancel_children` 内部自己记 warning。
        """
        try:
            from app import tool_delegate

            await tool_delegate.cancel_children(task_id)
        except Exception as exc:  # noqa: BLE001 —— 见 docstring
            log.warning("停子任务失败（父任务 %s，继续取消父任务）：%s", task_id, exc)
        job = self.current.get(task_id)
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    def is_interrupted(self, thread_id: str) -> bool:
        """这条线程是否被要求中断。`Nodes` 用它决定"还要不要继续这一步"。"""
        return thread_id in self.interrupted_threads

    async def cancel_thread(self, thread_id: str, reason: str = "") -> bool:
        """只打断**一条线程**，其它线程照常（MATRIX 2.8）。

        ## 哪些部分是真隔离，哪些是降级

        **真隔离**：Orbit 里"一条线程"= 一个独立任务（`run()` 里的
        `RunnableConfig.configurable.thread_id` 就是任务 id）。每个任务一个
        asyncio job、一份 checkpoint、一套状态，标记/跳过这一条完全不影响别的任务。

        **任务内（本轮升级过，如实说清能断到哪一步）**：同一个任务内部的步骤是一条
        串行图（prepare → think → gate → execute → …），没有能单独杀掉的子线程。
        但"正在跑的那一步"现在**真的能被放弃**，靠的是 `Nodes.run_tool` 把工具调用
        包成独立 `Task` 并每 `INTERRUPT_POLL_SECONDS` 轮询这个标记：

        * 还没开始的下一个工具：直接跳过（`Nodes.execute` 入口就判）；
        * **已经发出、正在等 I/O 的那一次**：`cancel()` 它，`CancelledError` 注在
          下一个 await 点——HTTP/浏览器/模型等待都会被立刻打断；
        * 每个待执行调用仍然各得一条 `[interrupted]` 结果（配对不能断，见
          `app/errors_retry.py`），`Nodes.think` 之后不再请求模型。

        **唯一断不掉的一小段**：工具已经把活派到线程/子进程里、正在跑同步代码
        （`asyncio.to_thread` 的 CPU 活、已经在执行的 `subprocess`）。`cancel()`
        只能**放弃等待**，那个线程/子进程会自己跑完、结果丢弃——Python 没有跨平台
        的安全线程中止，硬杀 subprocess 又要区分"谁派的、能不能杀"，属于另一件事。

        返回 True 表示这条线程确实在这台 runner 上（在跑或排队中）；False 表示
        不认识它——但**标记仍然生效**，将来它真跑起来会直接跳过执行。
        """
        thread_id = str(thread_id or "")
        if not thread_id:
            return False
        known = thread_id in self.current or thread_id in self.queued
        self.interrupted_threads.add(thread_id)
        if known:
            try:
                await self.store.event(
                    thread_id, "thread_interrupted", reason=reason or "user"
                )
            except Exception:  # 审计写失败不该让中断本身失败
                log.warning("中断事件写入失败（忽略）：%s", thread_id)
        return known

    # ── 后台复审（MATRIX 2.5）──────────────────────────────────────────────
    def review_enabled(self) -> bool:
        """复审开关。**默认关**，理由见 `_bg_review` 的 docstring。

        两级来源，与 `fallback.load_chain` 同款读法：
        1. `settings.bg_review`（`app/config.py` 将来加了字段就自动生效）；
        2. `<data_dir>/settings.json` 里的 `bg_review`（用户运行时改，不用重启配置）。
        """
        inline = getattr(self.settings, "bg_review", None)
        if inline is not None:
            return bool(inline)
        try:
            data = json.loads(
                (Path(self.settings.data_dir) / "settings.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return False
        return bool(data.get("bg_review", False)) if isinstance(data, dict) else False

    def _spawn_bg_review(self, task: Task) -> None:
        """起后台复审任务。**立刻返回**，主流程不等它（复审是旁路）。"""
        if not self.review_enabled():
            return
        job = asyncio.create_task(self._bg_review(task))
        self._bg_reviews.add(job)
        job.add_done_callback(self._bg_reviews.discard)

    async def _bg_review(self, task: Task) -> None:
        """任务成功后的后台复审：让模型自己提炼可复用经验（MATRIX 2.5）。

        ## 为什么默认**关**

        Hermes 的 `bg-review` 线程是默认跑的（research 09 §1.5 的实证日志），但那是
        有前提的：它跑在辅助槽位上、而且有"无事可做就不调用模型"的短路。Orbit 现在
        缺两样东西：

        1. **消费方还没有**。提炼出来的经验要有地方去（记忆沉淀/技能库）。现在只能
           落成一条事件——也就是"花钱换一条日志"，纯亏。
        2. **它是每个成功任务一次的整段上下文模型调用**。在按量计费的 key 上这是
           一笔静默的倍增成本，而且它发生在任务"已经成功"之后，预算闸门根本看不到它
           （`budget` 只统计工具结果与摘要）。

        所以：**默认关，接线后可以一行打开**（`settings.json` 里 `{"bg_review": true}`）。
        等记忆沉淀那条路打通、复审结果有地方落，再默认开。

        ## 失败必须静默

        复审是旁路：它挂了不能影响已经成功的任务，也不该在用户界面上留一条红字。
        所以只记日志 + 一条 `bg_review_failed` 事件。
        """
        try:
            rows = await self.store.rows(
                select(Message)
                .where(Message.conversation_id == task.conversation_id)
                .order_by(Message.created_at.desc(), Message.id.desc())
                .limit(BG_REVIEW_HISTORY)
            )
            history = []
            for row in reversed(list(rows)):
                if not (row.content or "").strip():
                    continue
                cls = AIMessage if row.role == "assistant" else HumanMessage
                history.append(cls(content=row.content))
            if not history:
                return
            payload = [
                SystemMessage(content=BG_REVIEW_SYSTEM),
                *history,
                HumanMessage(content=BG_REVIEW_PROMPT),
            ]
            response = await self.get_model().ainvoke(payload)
            text = str(getattr(response, "text", "") or "").strip()
            await self.store.event(
                task.id,
                "bg_review",
                result=text[:2000],
                empty=(not text or text.lower() == "none"),
                messages=len(history),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 复审是旁路，失败一律静默
            log.info("后台复审失败（静默忽略）：%s", exc)
            try:
                await self.store.event(
                    task.id, "bg_review_failed", error=f"{type(exc).__name__}: {str(exc)[:300]}"
                )
            except Exception:
                pass

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
            # 登记「我在持有这个会话」（MATRIX 10.8）。
            # 失败**不拦任务**：租约是并发保护，不是准入条件——文件不可写时
            # 宁可并发风险也不要任务跑不起来。但要说出来。
            try:
                self.leases.acquire(task.conversation_id, platform="cli")
                # 记住 task → conversation 的对应：`budget` 节点只有 task_id，
                # 而租约是按**会话**记的。不放这张表，节点里就只能猜。
                self._lease_sessions[task.id] = task.conversation_id
            except Exception as exc:  # noqa: BLE001
                log.warning("会话租约登记失败（任务继续，但有并发风险）：%s", exc)
            # 模型名随事件落库：换了模型之后回看历史任务能分清是哪次改的。
            await self.store.event(task.id, "task_started", model=str(self.settings.model or ""))
            # `on_session_start` 发射点（MATRIX 13.5）。
            #
            # 为什么加：`app/exporters.py` 的 `LANGFUSE_HOOKS` 订阅了这个事件，
            # 而在此之前**17 个钩子事件里只有 4 个有发射点**——Langfuse / Nemo 那些
            # exporter 订阅了会话生命周期事件却永远收不到。**订阅了不会触发的回调，
            # 比没订阅更糟**（插件作者以为接上了）。
            hooks.emit(
                "on_session_start",
                {
                    "task_id": task.id,
                    "conversation_id": task.conversation_id,
                    "resumed": bool(resume),
                },
            )
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
            # 后台复审（MATRIX 2.5）：只起任务、不等结果——主流程的成功路径
            # 绝不能因为"旁路的复盘"多等一次模型调用。
            self._spawn_bg_review(task)
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
            # 释放会话租约（MATRIX 10.8）。**放在 `finally` 里**：失败/取消也要放，
            # 否则一个崩掉的任务会让那个会话永远显示"有人持有"，下次启动的
            # `reap_stale` 才能清掉——而那时用户已经困惑过了。
            try:
                self.leases.release(task.conversation_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("会话租约释放失败（不影响任务结果）：%s", exc)
            self._lease_sessions.pop(task.id, None)
            self._task_platform.pop(task.id, None)
            # `on_session_end` 发射点（MATRIX 13.5）：与 `on_session_start` 配对。
            # 放在 `finally` 里，**失败/取消的任务也要发**——否则观测端的会话数永远少于
            # 实际跑过的任务，而"少记"比"记错"更难发现。
            try:
                _final = await self.store.get(Task, task.id)
                hooks.emit(
                    "on_session_end",
                    {
                        "task_id": task.id,
                        "conversation_id": task.conversation_id,
                        "status": str(getattr(_final, "status", "") or ""),
                        "error": str(getattr(_final, "error", "") or ""),
                    },
                )
            except Exception as exc:  # noqa: BLE001 —— 观测钩子不该影响任务收尾
                log.warning("on_session_end 发射失败（不影响任务）：%s", exc)
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

    # ── 模型构造：回退链 + 凭证池真的接在这里 ────────────────────────────────
    #
    # 改之前：`get_model()` 只 `ChatOpenAI(...)` 造一个，主 provider 429 就整轮失败。
    # 改之后：按"主模型 → settings.json 里的 fallback_chain"顺序展开候选，同一家
    # 再按凭证池的钥匙顺序轮换。判据在 `fallback.classify()` 一处定义，这里只搬运。

    def get_model(
        self,
        *,
        factory: Callable[[fb.Candidate], Any] | None = None,
        reasoning_effort: str | None = None,
    ) -> Any:
        """取本轮要用的模型客户端（带容错）。

        `factory` 是**注入点**：测试传一个假工厂就能观察"到底换了几站、换了哪把 key"，
        不需要网络也不需要真钥匙串。生产路径不传，走 `providers.build_chat_model`。

        返回值不是裸客户端，而是 `RoutedModel`：它把"这次实际用的是谁"放在 `route`
        上，并在**调用时**撞到 401/429 时继续沿链推进（构造阶段一切正常，401 只有
        真调一次才知道——只在构造时挑一次等于"回退链只在配置错的时候有用"）。
        """
        if self.model is not None:
            # 显式注入的模型（测试替身 / 外部覆盖）原样返回，不包一层：
            # 替身不该被迫实现 `route`，也不该被卷进回退逻辑。
            return self.model
        if not self.settings.model:
            raise ValueError("模型尚未配置")
        plan = fb.ModelPlan(
            self._targets(),
            factory or self._client_builder(reasoning_effort),
            keys=self._key_ring(),
        )
        client, candidate = plan.open()
        route = _route_of(candidate, len(plan.attempts) - 1)
        self.last_route = route
        self.last_attempts = plan.attempts
        if route.attempt > 0:
            log.warning(
                "模型已切换：主目标不可用，实际使用 %s（第 %s 次尝试）",
                route.label(),
                route.attempt + 1,
            )
        return RoutedModel(plan, client, route, on_switch=self._on_route_switch)

    def _on_route_switch(self, route: Route) -> None:
        """调用时切换的回调：把"现在真的在用谁"记在 runner 上，并留一条日志。"""
        self.last_route = route
        log.warning("模型已切换：实际使用 %s（原因：%s）", route.label(), route.reason or "未记录")

    def _targets(self) -> list[fb.FallbackTarget]:
        """候选顺序：主模型在前，`fallback_chain` 依次在后。

        链里**不存主模型**（`model.py` 的 `_primary_target` 也不存），否则会出现
        "改了主模型但链里还是旧那条"的错位。
        """
        profile = self._primary_profile()
        primary = fb.FallbackTarget(
            provider=profile.id,
            model=self.settings.model,
            base_url=self.settings.model_base_url,
        )
        chain = fb.load_chain(self.settings.data_dir)
        # 与主模型完全相同的链目没有意义（立刻会以同样方式失败），去掉。
        return [primary, *[item for item in chain if item != primary]]

    def _primary_profile(self) -> pv.ProviderProfile:
        """当前 provider：**按模型名路由** → 显式覆盖 → base_url 反查 → custom。

        **路由放在最前（MATRIX 4.3）**：之前 `app/model_router.py::route()` 写好了、
        单测也过，但**生产里零调用方**——选客户端读的是 per-provider 静态
        `profile.transport`，与模型名无关。

        **两个前提条件，缺一不可**（第一版写错过，被监视子agent 查出是假阳性）：

        1. **用户没有显式指定 `model_provider`**。`route()` 自己的优先级是
           「显式 provider **且该家模型表里有这个模型**」，所以
           `model_provider=openai` + 一个在别家表里的模型名时它会**反查成别家**——
           直接接上去就成了「用户明说了却被悄悄改掉」。**用户说了话就听用户的。**
        2. **`model_base_url` 不携带额外信息**——即它为空，或**等于被路由那家的默认
           端点**。这一条是第一版漏掉的：`model_base_url` 会**由模型名自动派生**
           （`model="deepseek-chat"` → `https://api.deepseek.com`），所以「两个都为空」
           这个条件在默认配置下**永不成立**，路由分支其实是惰性的。
           判据改成「base_url 等于该家默认」既覆盖派生的情况，又能在用户**真填了
           自建端点**时正确地让 `resolve_provider` 说了算。
        """
        if not str(getattr(self.settings, "model_provider", "") or "").strip():
            from app import model_router

            try:
                decision = model_router.route(self.settings.model, self.settings)
            except Exception as exc:  # noqa: BLE001 —— 路由是加分项，不该拦建模
                log.warning("模型路由失败（退回原有解析）：%s", exc)
                decision = None
            if decision is not None and decision.provider in pv.PROVIDERS:
                profile = pv.PROVIDERS[decision.provider]
                configured = str(getattr(self.settings, "model_base_url", "") or "").strip()
                default = str(getattr(profile, "default_base_url", "") or "").strip()
                # 用户没填端点，或填的正是这家的默认端点 → 端点不携带额外信息，听模型名的
                if not configured or configured.rstrip("/") == default.rstrip("/"):
                    return profile
        try:
            return pv.resolve_provider(self.settings)
        except pv.UnknownProvider as exc:
            # 显式写了不存在的 provider id 是笔误，必须让人看到；但不能让整轮任务
            # 因为一个配置字段挂掉——退回 custom 并留一条 warning。
            log.warning("模型 provider 配置无效，退回自定义端点：%s", exc)
            return pv.PROVIDERS["custom"]

    def _key_ring(self) -> fb.KeyRing:
        """钥匙来源：凭证池优先，池里没钥匙时退回主钥匙。

        `service` 必须与 `Context.hydrate()` / `model.py::_pool()` 完全一致
        （`orbit:<data_dir 的绝对路径>`），否则 `orbit auth add` 写进去的钥匙
        运行时读不到——"加进去了但没用"。
        """
        pool = fb.CredentialPool(fb.KeyringStore(f"orbit:{Path(self.settings.data_dir).resolve()}"))
        return fb.PoolKeyRing(pool, primary=self._primary_slot)

    def _primary_slot(self, provider: str) -> fb.KeySlot | None:
        """该家的"配置里那一把"钥匙（`orbit login` / `ORBIT_MODEL_API_KEY` / 环境变量）。

        故意**总是**返回一个 slot（哪怕密钥为空）：让 `build_chat_model` 去抛
        `MissingCredential`，那条错误消息里有"用 `orbit login --provider x` 写入"，
        比这里返回 None 后的一句"回退链耗尽（0 次尝试）"有用得多。
        """
        return fb.KeySlot(label="primary")

    def _client_builder(self, reasoning_effort: str | None) -> Callable[[fb.Candidate], Any]:
        """生产用的候选工厂：按站组装 `providers.ModelSettings` 再交给 `build_chat_model`。"""

        def build(candidate: fb.Candidate) -> Any:
            target = candidate.target
            profile = pv.PROVIDERS.get(target.provider)
            if profile is None:
                raise pv.UnknownProvider(
                    f"回退链里的 provider `{target.provider}` 不存在"
                    f"（可用：{', '.join(pv.PROVIDERS)}）"
                )
            # 端点优先级：链目显式 base_url → 该家默认值。**绝不用主模型的 base_url**：
            # 那会让回退请求打到主模型那家的端点上，静默变成"回退到自己"。
            base_url = target.base_url or profile.default_base_url
            key = candidate.key.secret
            if not key and target.key_env:
                # Hermes `fallback_model` 的 `key_env`：自建端点用自己的环境变量。
                key = os.environ.get(target.key_env, "").strip()
            if not key:
                key = pv.resolve_api_key(profile, self.settings)
            return pv.build_chat_model(
                profile,
                _TargetSettings(
                    model=target.model or profile.default_model,
                    base_url=base_url,
                    provider=profile.id,
                    key=key,
                ),
                target.model or None,
                reasoning_effort=reasoning_effort,
            )

        return build
