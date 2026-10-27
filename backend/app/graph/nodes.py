"""链上的节点：prepare / think / gate / execute / digest / reflect / budget / finalize。

plan 只留签名与纯函数（Planner 在 policies.py），不额外占模型调用；
reflect 已接进图，产出的是由轨迹推导的确定性提示，不调模型。
每个节点只写自己负责的字段，出问题能直接归因到节点。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from uuid import uuid4

from langchain_core.messages import (
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_chunk_to_message,
)
from langgraph.types import interrupt
from pydantic import Field
from sqlalchemy import select

from app.budget import TurnBudget
from app.context import FROZEN_PREFIX_KEY, PROMPT_VERSION, frozen_prefix, prefix_of
from app.indexing import chunks_for_tool_result
from app.policies import estimate_tokens, normalized_origin
from app.retrieval import PREFETCH_MIN_SCORE
from app.storage import Task, ToolResult
from app.tools import Args, Tool, as_tool_text
from app.vault import Vault

log = logging.getLogger("orbit.nodes")

PREFETCH_TOP_K = 5
OUTWARD_PREFIXES = ("browser_", "vault_", "notion_")
#: 模型单步调用尝试次数：流式还没吐出正文时的失败可安全重试
MODEL_ATTEMPTS = 3
#: 技能简述注入系统提示的上限（同簇只取第一个命中）
SKILL_INJECT_LIMIT = 5
STOP_LABELS = {
    "max_actions": "达到工具动作上限",
    "max_tokens": "达到 token 预算上限",
}


class ModelFailureError(RuntimeError):
    """模型连续多次调用失败（重试后仍拿不到正文）。文案直接给用户看。"""


class AskArgs(Args):
    question: str = Field(description="需要用户回答或在浏览器处理的具体事项")
    reason: str = Field(default="clarification", description="clarification、login 或 verification")
    tried: str = Field(
        default="",
        description="请求接管前已经尝试过的做法，例如换过哪些入口网址、是否用密钥存储登录过",
    )


def tried_tool(state, name: str) -> bool:
    for message in state.get("messages", []):
        for call in getattr(message, "tool_calls", None) or []:
            if call.get("name") == name:
                return True
    return False


def _chunks_and_vectors(task, name: str, result: dict, embeddings):
    """切块与嵌入放同一个线程里做（嵌入是同步 CPU 调用，绝不能占事件循环）。

    嵌入失败**只影响向量**，切块结果照常返回——向量路不能拖垮 FTS 索引。
    """
    chunks = chunks_for_tool_result(task, name, result)
    if not chunks or embeddings is None:
        return chunks, []
    try:
        return chunks, embeddings.embed([chunk["text"] for chunk in chunks])
    except Exception as exc:
        log.warning("嵌入失败（向量路跳过）：%s", exc)
        return chunks, []


class Nodes:
    """节点实现。依赖通过 AgentRunner 暴露，方便测试时替换 model 与 policies。"""

    def __init__(self, runner):
        self.runner = runner

    @property
    def settings(self):
        return self.runner.settings

    @property
    def store(self):
        return self.runner.store

    @property
    def registry(self):
        return self.runner.registry

    @property
    def policies(self):
        return self.runner.policies

    def current_host(self) -> str:
        browser = self.registry.browser
        return browser.current_host() if browser else ""

    # ── prepare ────────────────────────────────────────────────────────────
    async def prepare(self, state):
        """装载上下文并初始化进度字段；只有缺 messages 时才写入（避免重复追加）。"""
        task = await self.store.get(Task, state["task_id"])
        updates: dict[str, Any] = {
            "calls": list(state.get("calls") or []),
            "actions": int(state.get("actions") or 0),
            "answer": {},
            "waiting": {},
            "plan": dict(state.get("plan") or {}),
            "refs": dict(state.get("refs") or {}),
            "reflections": list(state.get("reflections") or []),
            "last_result": {},
            "final": {},
        }
        if not state.get("messages"):
            built = await self.policies.context.build(self.store, task, self.settings)
            updates["messages"] = built
        else:
            built = None

        budget = dict(state.get("budget") or {}) or self.policies.budget.start(self.settings)
        budget["used_actions"] = updates["actions"]
        updates["budget"] = budget

        context_meta = dict(state.get("context_meta") or {})
        context_meta.setdefault("skills", [])
        context_meta.setdefault("retrieved", [])
        context_meta.setdefault("compressed", {})
        # 「冻结前缀」：任务开始时算一次系统提示，之后每步复用同一条字符串。
        # 好处有三：① 提示前缀逐字节稳定（保住提示缓存）；② 记忆/画像/摘要在任务中途
        # 被改动也不会改变模型看到的世界，任务内部自洽；③ think 不必每次重装上下文。
        # 恢复旧 checkpoint 时若缺这个键，就在下一轮 think 里按当前内容补冻（见 think）。
        if not frozen_prefix(context_meta):
            # 本任务第一次装配：上面刚装好的 messages 第一条就是系统提示，直接取用。
            # 绝不再调一次 build_prefix：那会**第二次**跑 build_context，而装配本身会
            # 累加记忆命中数（M1 的 `_touch_memories`），新任务的 hit_count 便被凭空翻倍，
            # "哪条记忆是死数据"的判据随之失真。回归见 `tests/test_graph.py::
            # test_one_task_counts_each_memory_hit_once`。
            prefix = prefix_of(built if built is not None else state.get("messages"))
            if prefix:
                context_meta[FROZEN_PREFIX_KEY] = prefix
            else:
                # 既没有新装配结果、state 里也没有系统提示（异常路径）：按当前内容算一次，
                # 并显式 `touch=False`——这份前缀不是一次新的"注入"，不应该再计一次命中。
                context_meta[FROZEN_PREFIX_KEY] = await self.policies.context.build_prefix(
                    self.store, task, self.settings
                )
        await self.store.event(state["task_id"], "node_entered", node="prepare")
        hits = await self.prefetch(task, state)
        if hits:
            context_meta["retrieved"] = [
                {
                    "origin": str(hit.get("origin") or ""),
                    "source_type": str(hit.get("source_type") or ""),
                    "score": round(float(hit.get("score") or 0), 4),
                }
                for hit in hits
            ]
            context_meta["retrieved_block"] = self.policies.context.render_hits(hits)
            await self.store.event(
                state["task_id"],
                "retrieval_injected",
                trigger="prepare",
                count=len(hits),
                origins=[item["origin"] for item in context_meta["retrieved"]],
                scores=[item["score"] for item in context_meta["retrieved"]],
            )
        skills = await self.related_skills(task.goal or "")
        if skills:
            context_meta["skills"] = [
                {"name": s.get("name") or "", "scope": s.get("scope") or "global"}
                for s in skills
            ]
            context_meta["skills_block"] = self.render_skills(skills)
            await self.store.event(
                state["task_id"],
                "skills_injected",
                count=len(skills),
                names=[s["name"] for s in context_meta["skills"]],
            )
        updates["context_meta"] = context_meta
        return updates

    async def prefetch(self, task, state) -> list[dict]:
        """按目标预取（≤5 片段）。保守：命中分数不过阈值就不注入，避免噪声撑爆上下文。"""
        query = (task.goal or "").strip()
        if len(query) < 2:
            return []
        try:
            hits = await self.policies.retriever.search(
                query,
                scope="task",
                conversation_id=task.conversation_id,
                top_k=PREFETCH_TOP_K,
            )
        except Exception as exc:  # 检索失败不能拖垮任务
            log.warning("预取失败：%s", exc)
            await self.store.event(state["task_id"], "retrieval_failed", trigger="prepare")
            return []
        return [hit for hit in hits if float(hit.get("score") or 0) >= PREFETCH_MIN_SCORE]

    async def related_skills(self, goal: str) -> list[dict]:
        """按触发词字面匹配召回相关技能，并按治理状态注入择优（二期）。

        召回：任一 trigger 与任务目标有 ≥2 字公共子串。
        择优：
          1. 过滤 unreviewed（新沉淀待确认）与 archived（已归档）
          2. 排序 scope(site>project>global) → success_rate desc → usage_count desc → name
          3. 同簇去重：triggers 互标「备选」的技能算一簇，只保留排序最高的冠军
        上限 SKILL_INJECT_LIMIT。
        """
        goal = (goal or "").strip()
        if not goal:
            return []
        try:
            skills = await self.policies.skills.catalog_with_stats(
                self.store, self.policies.skills.root
            )
        except Exception as exc:  # 技能目录/表坏了也不能拖垮任务
            log.warning("技能目录读取失败：%s", exc)
            return []
        order = {"site": 0, "project": 1, "global": 2}
        matched = []
        for skill in skills:
            if skill.get("status") in ("unreviewed", "archived"):
                continue  # 未确认/已归档不注入
            triggers = skill.get("triggers") or []
            if any(self._trigger_matches(trigger, goal) for trigger in triggers):
                matched.append(skill)
        # 排序：scope 优先 → 成功率 → 使用次数 → 名字稳定
        matched.sort(
            key=lambda s: (
                order.get(s.get("scope"), 3),
                -float(s.get("success_rate") or 0.0),
                -int(s.get("usage_count") or 0),
                s.get("name") or "",
            )
        )
        # 同簇去重：被「备选」标记引用的技能归为一簇，只留冠军
        champion: list[dict] = []
        claimed: set[str] = set()
        for skill in matched:
            name = skill.get("name") or ""
            # 该技能是否被更靠前的冠军标注为备选
            if name in claimed:
                continue
            champion.append(skill)
            for trigger in skill.get("triggers") or []:
                if str(trigger).startswith("备选："):
                    alt = str(trigger)[len("备选："):].strip()
                    if alt:
                        claimed.add(alt)
            if len(champion) >= SKILL_INJECT_LIMIT:
                break
        return champion

    @staticmethod
    def _trigger_matches(trigger: str, goal: str) -> bool:
        """触发词与目标有 ≥2 字连续公共子串即算命中（中文两字起步）。"""
        trigger = (trigger or "").strip()
        if len(trigger) < 2:
            return False
        # 粗粒度：按字符级两字滑窗找公共子串，避免分词依赖
        for index in range(len(goal) - 1):
            if goal[index : index + 2] in trigger:
                return True
        return False

    def render_skills(self, skills: list[dict]) -> str:
        """把技能简述渲染成注入系统提示的一段（与检索片段区分开）。"""
        if not skills:
            return ""
        lines = [
            "以下技能（流程做法）可能相关，判断相关时用 skill_load 取全文："
        ]
        for skill in skills:
            name = skill.get("name") or ""
            triggers = (skill.get("triggers") or [])[:2]
            scope = skill.get("scope") or "global"
            lines.append(f"- {name}（{scope}）：{'；'.join(triggers)}")
        return "\n".join(lines)

    # ── think ──────────────────────────────────────────────────────────────
    async def think(self, state):
        runner = self.runner
        task = await self.store.get(Task, state["task_id"])
        meta = state.get("context_meta") or {}
        # 「冻结前缀」（M4）：系统提示在本任务内是常量。只在缺失时才补算——
        # 正常路径由 prepare 冻好；恢复旧 checkpoint（没有这个键）时才在首轮 think 补冻。
        # 绝不每步重装：每轮重排前缀会同时打掉提示缓存、并让记忆/画像的改动在任务中途生效。
        prefix = frozen_prefix(meta)
        meta_updates: dict[str, Any] = {}
        if not prefix:
            # 补冻：优先从已装配的 messages 里零成本取系统提示；实在没有才重算，
            # 且重算必须 `touch=False`——这是"补齐缓存"而不是一次新的上下文注入，
            # 不能重复累加记忆命中数（M1 死数据判据）。
            prefix = prefix_of(state.get("messages")) or await self.policies.context.build_prefix(
                self.store, task, self.settings
            )
            meta_updates[FROZEN_PREFIX_KEY] = prefix
        extra = "\n\n".join(
            block
            for block in (meta.get("skills_block"), meta.get("retrieved_block"))
            if block
        )
        head = f"{prefix}\n{extra}" if extra else prefix
        context = [SystemMessage(content=head)]
        messages = [*context, *[m for m in state.get("messages") or [] if m.type != "system"]]
        specs = self.policies.exposer.expose(self.registry.specs())
        model = runner.get_model().bind_tools(specs, parallel_tool_calls=False)
        response_id = uuid4().hex
        identity = {
            "conversation_id": task.conversation_id,
            "message_id": f"answer-{task.id}",
            "response_id": response_id,
            "prompt_version": PROMPT_VERSION,
        }
        runner.partial[task.id] = ""
        await self.store.event(task.id, "message_started", **identity)
        answer = await self.ask_model(model, messages, task, identity)
        kind = "activity" if answer.tool_calls else "answer"
        await self.store.event(task.id, "message_finished", **identity, response_kind=kind)
        if answer.tool_calls:
            runner.partial[task.id] = ""
            if answer.text:
                await self.store.event(task.id, "activity_summary", text=answer.text)
        plan = self.policies.planner.derive(task.goal, list((state.get("refs") or {}).values()))
        updates: dict[str, Any] = {
            "messages": [answer],
            "calls": answer.tool_calls,
            "waiting": {},
            "answer": {},
            "plan": plan,
        }
        if meta_updates:
            # 恢复旧 checkpoint 时补冻的前缀要写回状态，否则每轮 think 都会重算一次。
            updates["context_meta"] = {**meta, **meta_updates}
        return updates

    async def ask_model(self, model, messages, task, identity):
        """模型调用，带有限重试。

        只在"还没吐出任何正文"时重试——已经流出半截回答再重试会把重复内容推给前端。
        用完尝试次数后抛 `ModelFailureError`，由 AgentRunner 落成一句能看懂的错误。
        """
        task_id = task.id
        last: Exception | None = None
        for attempt in range(1, MODEL_ATTEMPTS + 1):
            try:
                return await self.stream_model(model, messages, task_id, identity)
            except Exception as exc:
                last = exc
                if self.runner.partial.get(task_id):
                    raise
                if attempt >= MODEL_ATTEMPTS:
                    break
                log.warning("模型调用失败（第 %s 次）：%s", attempt, exc)
                await self.store.event(
                    task_id, "model_retry", attempt=attempt, error=type(exc).__name__
                )
        raise ModelFailureError(
            f"模型连续 {MODEL_ATTEMPTS} 次调用失败（{type(last).__name__}）："
            "请检查连接设置、API 地址与密钥，或换一个模型重试。"
        ) from last

    async def stream_model(self, model, messages, task_id, identity) -> Any:
        """一次流式调用：边收边发事件，返回完整响应。空响应按失败处理（可重试）。"""
        response: Any = None
        if hasattr(model, "astream"):
            full = None
            pending = ""
            last_sent = time.monotonic()
            started = time.monotonic()
            total_chars = 0
            async for chunk in model.astream(messages):
                full = chunk if full is None else full + chunk
                # Only outward answer text is transmitted; raw model reasoning is not a summary.
                delta = chunk.text
                if delta:
                    self.runner.partial[task_id] += delta
                    pending += delta
                    total_chars += len(delta)
                if pending and (time.monotonic() - last_sent >= 0.06 or len(pending) >= 80):
                    await self.store.event(
                        task_id,
                        "message_delta",
                        **identity,
                        text=pending,
                        # 速率与消耗：字符数是流式途中的真实计量，token 按 estimate_tokens 折算。
                        chars=total_chars,
                        tokens=estimate_tokens(self.runner.partial.get(task_id, "")),
                        tokens_per_second=round(
                            estimate_tokens(self.runner.partial.get(task_id, ""))
                            / max(time.monotonic() - started, 0.05),
                            1,
                        ),
                    )
                    pending = ""
                    last_sent = time.monotonic()
            if pending:
                await self.store.event(
                    task_id,
                    "message_delta",
                    **identity,
                    text=pending,
                    chars=total_chars,
                    tokens=estimate_tokens(self.runner.partial.get(task_id, "")),
                    tokens_per_second=round(
                        estimate_tokens(self.runner.partial.get(task_id, ""))
                        / max(time.monotonic() - started, 0.05),
                        1,
                    ),
                )
            if full is None:
                raise RuntimeError("模型返回了空响应")
            response = message_chunk_to_message(full)
            # 输入 token 用本步发送的 messages 估算（系统提示 + 历史 + 工具结果），
            # 输出 token 用累计字符折算——两者都是"量级"不是账单，与 cost_summary 口径一致。
            # 累计到任务级：一个任务可能多次进入 think（工具调用→再思考），总量才是真实消耗。
            prior = self.runner._token_usage.get(task_id, {"prompt_tokens": 0, "completion_tokens": 0, "elapsed": 0.0})
            self.runner._token_usage[task_id] = {
                "prompt_tokens": int(prior.get("prompt_tokens", 0))
                + estimate_tokens("".join(getattr(m, "content", "") or "" for m in messages)),
                "completion_tokens": int(prior.get("completion_tokens", 0))
                + estimate_tokens(self.runner.partial.get(task_id, "")),
                "elapsed": round(float(prior.get("elapsed", 0.0)) + (time.monotonic() - started), 2),
            }
        else:
            # Scripted test models may provide only ainvoke; production models use astream.
            response = await model.ainvoke(messages)
            self.runner.partial[task_id] = response.text
            if response.text:
                await self.store.event(task_id, "message_delta", **identity, text=response.text)
            prior = self.runner._token_usage.get(task_id, {"prompt_tokens": 0, "completion_tokens": 0, "elapsed": 0.0})
            self.runner._token_usage[task_id] = {
                "prompt_tokens": int(prior.get("prompt_tokens", 0))
                + estimate_tokens("".join(getattr(m, "content", "") or "" for m in messages)),
                "completion_tokens": int(prior.get("completion_tokens", 0))
                + estimate_tokens(response.text or ""),
                "elapsed": float(prior.get("elapsed", 0.0)),
            }
        if not response.text and not response.tool_calls:
            raise RuntimeError("模型没有返回正文或工具调用")
        return response

    # ── gate ───────────────────────────────────────────────────────────────
    async def gate(self, state):
        """只处理人工介入：ask_user 与工具返回的 needs_user。

        v1 完全访问：事前零确认，确认分支整体移除（grants 退化为审计分组标签）。
        """
        call = state["calls"][0]
        tool = self.registry.tools.get(call["name"])
        if not tool:
            return {"answer": {"approved": True}}
        question = state.get("waiting")
        if not question and call["name"] == "ask_user":
            blocked = self.takeover_allowed(state, call["args"] or {})
            if blocked:
                # Hand the request back as a rejection so the model keeps working instead
                # of pushing the problem to the user at the first sign of a login wall.
                return {"answer": {"approved": False, "answer": blocked}}
            question = call["args"]
        if question:
            answer = interrupt(question)
            return {"answer": answer, "waiting": {}}
        return {"answer": {"approved": True}}

    def takeover_allowed(self, state, args) -> str:
        """Empty string means the takeover request may proceed.

        A login wall the agent could clear itself with saved credentials is not a reason
        to interrupt the user: try vault_login first, hand over only when that fails.
        """
        if str(args.get("reason", "")) not in {"login", "verification"}:
            return ""
        browser = self.registry.browser
        host = browser.current_host() if browser else ""
        if not tried_tool(state, "vault_login") and host and Vault(self.settings).site_credentials(host):
            return (
                f"密钥存储里有 {host} 的账号，请先用 vault_login 尝试登录；"
                "只有它失败、或出现验证码/二次验证时再请求接管。"
            )
        # One bounded nudge: the first request must say what was already attempted. The
        # second one goes through, so a genuinely blocked run is never stuck here.
        if not str(args.get("tried", "")).strip() and not tried_tool(state, "ask_user"):
            return (
                "请求接管前请在 tried 里写清已经试过什么：换过哪些入口网址、"
                "是否用密钥存储登录过、重新观察后看到什么。确认没有别的办法再请求接管。"
            )
        return ""

    # ── execute ────────────────────────────────────────────────────────────
    async def execute(self, state):
        call = state["calls"][0]
        task_id = state["task_id"]
        if not state["answer"].get("approved", True):
            result = {
                "status": "denied",
                "message": state["answer"].get("answer", "用户拒绝该操作"),
            }
            return {
                "messages": [ToolMessage(content=as_tool_text(result), tool_call_id=call["id"])],
                "calls": state["calls"][1:],
                "actions": state["actions"] + 1,
                "answer": {},
                "waiting": {},
                "last_result": {},
            }
        if call["name"] == "ask_user":
            result = {
                "status": "ok",
                "answer": state["answer"].get("answer", "用户已完成接管，请重新观察页面"),
            }
            return {
                "messages": [ToolMessage(content=as_tool_text(result), tool_call_id=call["id"])],
                "calls": state["calls"][1:],
                "actions": state["actions"] + 1,
                "answer": {},
                "waiting": {},
                "last_result": {},
            }
        operation_id = f"{task_id}:{call['id']}"
        # 传 messages 是因为「记忆写入门槛」（M2）必须读到用户原话：
        # 只认用户显式说过的「记住……」，模型自行沉淀一律拒绝。
        result = await self.registry.execute(
            task_id, operation_id, call["name"], call["args"], state.get("messages")
        )
        if result["status"] in {"needs_user", "uncertain"}:
            return {
                "waiting": {
                    "reason": result.get("reason", "verification"),
                    "question": result.get("message", "请检查浏览器，完成后继续"),
                    "tool": call["name"],
                },
                "answer": {},
                "last_result": {},
            }
        # 模型只看得到 project() 之后的结果：结构化正文等大字段不进上下文。
        projected = self.policies.tool_results.project(call["name"], result)
        # 第三层（整轮预算）的累加器：一次 assistant 轮次里的工具结果在这里累计，
        # 超过 turn_budget 就按大小降序把最大的先落盘（见 app/budget.py）。
        # 挂在实例字典上而不是 state 里：state 要过 checkpoint 序列化，塞自定义对象
        # 会破坏任务恢复；按 task_id 分开是因为一个 Nodes 实例被同进程的多个并发任务共用。
        turns = getattr(self, "_turn_budgets", None)
        if turns is None:
            turns = {}
            self._turn_budgets = turns
        turn = turns.get(task_id)
        if turn is None:
            turn = TurnBudget(data_dir=getattr(self.settings, "data_dir", None))
            turns[task_id] = turn
        messages: list[Any] = [
            ToolMessage(content=as_tool_text(projected), tool_call_id=call["id"])
        ]
        # 视觉兜底：点击/填写因元素失效失败时，把最新截图喂给多模态模型，
        # 让模型看图报坐标（用 browser_click_at），而不是盲目重试或直接放弃。
        if (
            result.get("status") == "error"
            and call["name"] in {"browser_click", "browser_fill"}
            and self.registry.browser is not None
        ):
            image = self.registry.browser.last_screenshot_data_url()
            if image:
                messages.append(
                    HumanMessage(
                        content=[
                            {
                                "type": "text",
                                "text": (
                                    "上面的操作因「元素已变化」失败。这是当前页面的最新截图，"
                                    "请你仔细看图，找出应该点击/操作的目标元素位置，"
                                    "然后用 browser_click_at 工具报出它的视口坐标（x 为中心横坐标、"
                                    "y 为中心纵坐标）完成操作。若图中确实找不到目标，"
                                    "请说明并请求重新观察。"
                                ),
                            },
                            {"type": "image_url", "image_url": {"url": image}},
                        ]
                    )
                )
        return {
            "messages": messages,
            "calls": state["calls"][1:],
            "actions": state["actions"] + 1,
            "answer": {},
            "waiting": {},
            "last_result": {
                "operation_id": operation_id,
                "name": call["name"],
                "status": result["status"],
                "result": result,
                "host": self.current_host(),
            },
        }

    # ── digest ─────────────────────────────────────────────────────────────
    async def digest(self, state):
        """原文落库（checksum 幂等）+ 引用登记 + 大小观测；模型看到的内容不变。"""
        last = state.get("last_result") or {}
        task_id = state["task_id"]
        if not last:
            return {"last_result": {}}
        name = str(last.get("name") or "")
        status = str(last.get("status") or "")
        result = last.get("result") or {}
        operation_id = str(last.get("operation_id") or "")
        host = str(last.get("host") or "")
        projected = self.policies.tool_results.project(name, result)
        text = as_tool_text(projected)
        # 本轮累加器（execute 里创建）：交给 record 去做第三层结算。
        turn = (getattr(self, "_turn_budgets", None) or {}).get(task_id)
        meta = await self.policies.tool_results.record(
            self.store,
            task_id=task_id,
            operation_id=operation_id,
            name=name,
            status=status,
            host=host,
            text=text,
            turn=turn,
        )
        if turn is not None and not state.get("calls"):
            # 本轮最后一个工具结果已经落库：累加器归零，下一轮重新计数。
            turn.reset()
        await self.store.event(
            task_id,
            "tool_result_stored",
            tool=name,
            bytes=meta["bytes"],
            tokens=meta["tokens"],
            truncated=meta["truncated"],
            duplicate=meta["duplicate"],
        )
        refs = dict(state.get("refs") or {})
        refs[operation_id] = {
            "tool": name,
            "status": status,
            "host": host,
            "bytes": meta["bytes"],
            "tokens": meta["tokens"],
        }
        budget = dict(state.get("budget") or {})
        budget["token_estimate"] = int(budget.get("token_estimate") or 0) + int(meta["tokens"])
        context_meta = dict(state.get("context_meta") or {})
        if name == "retriever_search":
            collected = [
                {"origin": str(hit.get("origin") or ""), "source_type": str(hit.get("source_type") or "")}
                for hit in (result.get("data") or {}).get("hits", [])
                if hit.get("origin")
            ]
            if collected:
                merged = list(context_meta.get("retrieved") or [])
                known = {item.get("origin") for item in merged}
                merged.extend(item for item in collected if item["origin"] not in known)
                context_meta["retrieved"] = merged
        await self.index(task_id, name, status, result, last)
        return {
            "last_result": {},
            "refs": refs,
            "budget": budget,
            "context_meta": context_meta,
        }

    async def index(self, task_id: str, name: str, status: str, result: dict, last: dict) -> None:
        """索引写入：切块 + 嵌入一起走线程、失败只记日志（不阻塞、不返工工具结果）。"""
        if not self.policies.tool_results.should_index(name, status):
            return
        # 关闭向量路时不碰 policies.embeddings（假 policies 可能没有这个字段）
        embed_vectors = bool(getattr(self.settings, "vector_enabled", False))
        try:
            task = await self.store.get(Task, task_id)
            chunks, vectors = await asyncio.to_thread(
                _chunks_and_vectors,
                task,
                name,
                result,
                self.policies.embeddings if embed_vectors else None,
            )
            if not chunks:
                return
            written = await self.policies.indexer.write(chunks)
            await self.store.event(task_id, "chunks_indexed", tool=name, count=written)
            await self._index_vectors(task_id, chunks, vectors)
        except Exception as exc:
            log.warning("索引写入失败（只记日志）：%s", exc)
            await self.store.event(task_id, "chunks_index_failed", tool=name, error=str(exc)[:200])

    async def _index_vectors(self, task_id: str, chunks: list[dict], vectors: list) -> None:
        """向量写入独立一步：失败只记日志，已落库的 chunks 不受影响。"""
        if not vectors or len(vectors) != len(chunks):
            return
        rows = [(chunk["id"], vector) for chunk, vector in zip(chunks, vectors, strict=True)]
        try:
            count = await self.policies.vectors.write(rows)
            await self.store.event(task_id, "vectors_indexed", count=count)
        except Exception as exc:
            log.warning("向量写入失败（只记日志）：%s", exc)
            await self.store.event(task_id, "vectors_index_failed", error=str(exc)[:200])

    # ── reflect ────────────────────────────────────────────────────────────
    async def reflect(self, state):
        """周期性自省（budget 之后、think 之前）。

        不满足触发条件时返回空 dict：行为与没接这条边时逐字一致。
        命中时把**轨迹推导出的提醒**作为一条 harness 消息塞回上下文——注入的是
        "别再重复失败参数 / 别重复已完成步骤"这类纠偏，不是新的待办。
        """
        if not self.policies.reflector.should_reflect(state):
            return {}
        task_id = state["task_id"]
        steps = len(state.get("refs") or {})
        notes = [note for note in self.policies.reflector.reflect(state) if note]
        reflections = list(state.get("reflections") or [])
        reflections.append({"step": steps, "notes": notes})
        await self.store.event(task_id, "reflection", step=steps, notes=notes)
        if not notes:
            return {"reflections": reflections}
        body = "\n".join(f"- {note}" for note in notes)
        return {
            "reflections": reflections,
            "messages": [
                HumanMessage(content=f"【流程自省提示，不是用户消息】\n{body}")
            ],
        }

    # ── budget ─────────────────────────────────────────────────────────────
    async def budget(self, state):
        budget = dict(state.get("budget") or {})
        budget["used_actions"] = int(state.get("actions") or 0)
        started = float(budget.get("started_at") or time.time())
        budget["elapsed"] = round(time.time() - started, 2)
        # 摘要开销并入预算视野（M5）：`budget.check` 判的是"工具结果 + 摘要"总量。
        # 摘要由后台另起一次模型调用，不并进来预算就永远看不到这部分真实花费。
        summaries = getattr(self.runner, "summaries", None)
        if summaries is not None:
            try:
                delta = summaries.cost_since(int(budget.get("summary_mark") or 0))
                if delta.get("calls"):
                    budget["summary_cost"] = delta
            except Exception:  # 台账读失败只影响归因，不拦任务
                pass
        # 软时间闸：到点转入后台继续，而不是硬停。只在第一次越线时发一次事件，
        # 之后按 report_interval 定期发进度报告——报告由同一条 budget 边驱动，
        # 只要任务还在推进，它就会按节奏触发，不需要另起定时线程。
        max_seconds = int(budget.get("max_seconds") or 0)
        report_interval = int(getattr(self.settings, "report_interval", 60) or 60)
        if max_seconds and not budget.get("backgrounded") and budget["elapsed"] >= max_seconds:
            budget["backgrounded"] = True
            budget["last_report_at"] = 0.0
            await self.store.event(
                state["task_id"],
                "backgrounded",
                elapsed=budget["elapsed"],
                max_seconds=max_seconds,
            )
            await self.store.event(
                state["task_id"],
                "activity_summary",
                text=(
                    f"任务已运行 {int(budget['elapsed'])} 秒、超过 {max_seconds} 秒上限，"
                    f"转入后台继续执行，期间会定期汇报进度。当前已执行 {budget['used_actions']} 个动作。"
                ),
            )
        elif budget.get("backgrounded"):
            last_report = float(budget.get("last_report_at") or 0.0)
            if budget["elapsed"] - last_report >= report_interval:
                budget["last_report_at"] = budget["elapsed"]
                await self.store.event(
                    state["task_id"],
                    "background_report",
                    elapsed=budget["elapsed"],
                    actions=budget["used_actions"],
                    refs=len(state.get("refs") or {}),
                )
        decision = self.policies.budget.check({**state, "budget": budget})
        budget["stop_reason"] = decision
        if decision:
            await self.store.event(
                state["task_id"],
                "budget_stopped",
                reason=decision,
                used_actions=budget["used_actions"],
                max_actions=budget.get("max_actions"),
                elapsed=budget["elapsed"],
            )
        return {"budget": budget}

    # ── finalize ───────────────────────────────────────────────────────────
    async def finalize(self, state):
        """收尾：回答 + 完成度清单 + 对外动作清单 + 引用回填校验。"""
        task_id = state["task_id"]
        budget = dict(state.get("budget") or {})
        stop = str(budget.get("stop_reason") or "")
        plan = dict(state.get("plan") or {})
        outward = await self.outward_actions(task_id)
        answer = self.last_answer(state)
        text = self.compose(answer, stop, plan, outward, budget)
        citation = self.citation_check(state, text)
        if citation["checked"] and not citation["matched"]:
            text = (
                f"{text}\n\n> 本次回答没有引用任何检索到的来源，已在任务记录里标记为部分完成。"
            )
        cost = self.cost_summary(state, citation)
        await self.store.event(
            task_id,
            "citation_checked",
            checked=citation["checked"],
            matched=len(citation["matched"]),
            missing=len(citation["missing"]),
            missing_origins=citation["missing"][:5],
        )
        await self.store.event(
            task_id,
            "finalize_completed",
            stop_reason=stop,
            outward_actions=len(outward),
            **cost,
        )
        return {
            "final": {
                "text": text,
                "stop_reason": stop,
                "partial": bool(citation["checked"] and not citation["matched"]),
                "outward_actions": outward,
                "cost": cost,
            }
        }

    def cost_summary(self, state: dict, citation: dict) -> dict:
        """这次任务花了多少：按 tool_results 登记的 refs 汇总，配动作数与耗时。

        token 是估算值（只算工具结果正文），给的是量级与分布，不是账单。
        **摘要开销单列**（M5）：它由后台另一次模型调用产生，混进工具 token 里会让
        "这条任务为什么花了这么多"失去归因能力，所以分开报。
        """
        refs = state.get("refs") or {}
        budget = state.get("budget") or {}
        summary = dict(budget.get("summary_cost") or {})
        return {
            "refs": len(refs),
            "tokens": sum(int(item.get("tokens") or 0) for item in refs.values()),
            "bytes": sum(int(item.get("bytes") or 0) for item in refs.values()),
            "actions": int(state.get("actions") or 0),
            "elapsed": float(budget.get("elapsed") or 0),
            "citations": len(citation["matched"]),
            "summary_calls": int(summary.get("calls") or 0),
            "summary_tokens": int(summary.get("tokens") or 0),
            "summary_elapsed": float(summary.get("elapsed") or 0.0),
        }

    def last_answer(self, state) -> str:
        for message in reversed(state.get("messages") or []):
            if message.type != "ai" or getattr(message, "tool_calls", None):
                continue
            text = (getattr(message, "text", "") or "").strip()
            if text:
                return text
        return ""

    def compose(self, answer: str, stop: str, plan: dict, outward: list[dict], budget: dict) -> str:
        if not stop:
            return answer or "任务已结束，但没有产生可展示的正文。"
        steps = plan.get("steps") or []
        done = [step for step in steps if step.get("status") == "done"]
        pending = [step for step in steps if step.get("status") != "done"]
        lines = [answer] if answer else []
        lines.append(f"任务在这里暂停：{STOP_LABELS.get(stop, stop)}。")
        lines.append("")
        lines.append("**已完成**")
        if done:
            for index, step in enumerate(done, 1):
                evidence = f"（{step.get('evidence')}）" if step.get("evidence") else ""
                lines.append(f"- 第 {index} 步 {step.get('goal')}{evidence}")
        else:
            lines.append("- 还没有完成任何步骤")
        lines.append("")
        lines.append("**未完成 / 下一步建议**")
        if pending:
            lines.extend(f"- 第 {index} 步 {step.get('goal')}" for index, step in enumerate(pending, 1))
        else:
            lines.append("- 已经执行的动作都已完成；如果要继续，请再给我一条更具体的指令。")
        lines.append("- 可以让我继续这个任务（我会接着上次的进度往下做），或缩小范围重新开始。")
        lines.append("")
        lines.append("**对外动作清单**")
        if outward:
            lines.extend(
                f"- {item['host'] or '未记录站点'}：{item['name']} {item['count']} 次"
                for item in outward
            )
        else:
            lines.append("- 本次没有在站点上执行写操作。")
        return "\n".join(lines)

    async def outward_actions(self, task_id: str) -> list[dict]:
        rows = await self.store.rows(select(ToolResult).where(ToolResult.task_id == task_id))
        counts: dict[tuple[str, str], int] = {}
        for row in rows:
            if row.status != "ok" or not row.name.startswith(OUTWARD_PREFIXES):
                continue
            key = (row.host or "", row.name)
            counts[key] = counts.get(key, 0) + 1
        return [
            {"host": host, "name": name, "count": count}
            for (host, name), count in sorted(counts.items())
        ]

    def citation_check(self, state: dict, text: str) -> dict:
        """引用回填硬约束：只在本次注入过片段时校验，避免误伤无检索的任务。"""
        retrieved = (state.get("context_meta") or {}).get("retrieved") or []
        if not retrieved or not text.strip():
            return {"checked": False, "matched": [], "missing": []}
        body = text.lower()
        matched, missing = [], []
        for item in retrieved:
            origin = str(item.get("origin") or "")
            if not origin:
                continue
            target = normalized_origin(origin)
            host = target.split("/", 1)[0]
            rest = target.split("/", 1)[1] if "/" in target else ""
            segment = rest.split("/", 1)[0]
            if host and host in body and (not rest or rest in body or (segment and segment in body)):
                matched.append(origin)
            else:
                missing.append(origin)
        return {"checked": True, "matched": matched, "missing": missing}


def register_ask_user(registry) -> None:
    registry.tools["ask_user"] = Tool(
        "ask_user", "请求澄清或浏览器人工接管，回答后继续原任务。", AskArgs
    )


__all__ = ["AskArgs", "Nodes", "register_ask_user", "tried_tool"]
