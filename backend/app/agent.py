from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command
from pydantic import SecretStr
from sqlalchemy import select

from app import fallback as fb
from app import providers as pv
from app.graph import build_graph
from app.graph.nodes import ModelFailureError, Nodes, register_ask_user
from app.policies import Policies
from app.storage import Message, Operation, Task

log = logging.getLogger("orbit.agent")


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
        self.nodes = Nodes(self)
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
        """当前 provider。显式覆盖 → base_url 反查 → custom（见 `resolve_provider`）。"""
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
