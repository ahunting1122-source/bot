"""链上的节点：prepare / think / gate / execute / digest / reflect / budget / finalize。

plan 只留签名与纯函数（Planner 在 policies.py），不额外占模型调用；
reflect 已接进图，产出的是由轨迹推导的确定性提示，不调模型。
每个节点只写自己负责的字段，出问题能直接归因到节点。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any
from uuid import uuid4

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
    message_chunk_to_message,
)
from langgraph.types import interrupt
from pydantic import Field
from sqlalchemy import select

from app import compaction, errors, hooks
from app import errors_retry as retry
from app.budget import TurnBudget
from app.context import FROZEN_PREFIX_KEY, PROMPT_VERSION, frozen_prefix, prefix_of
from app.heartbeat import TOOL_PREFIX, Heartbeat
from app.indexing import chunks_for_tool_result
from app.policies import estimate_tokens, normalized_origin
from app.retrieval import PREFETCH_MIN_SCORE
from app.storage import Task, ToolResult
from app.tools import Args, Tool, as_tool_text
from app.vault import Vault
from app.window import Thresholds, thresholds, thresholds_for, window_of

log = logging.getLogger("orbit.nodes")

PREFETCH_TOP_K = 5
OUTWARD_PREFIXES = ("browser_", "vault_", "notion_")
#: 模型单步调用尝试次数：流式还没吐出正文时的失败可安全重试。
#: 只对 `errors.RETRYABLE` 四类（限流/超时/连接/过载）生效；其余失败一次就停。
MODEL_ATTEMPTS = 3
#: 上下文压缩到窗口的多少。**数值以 `app/compaction.py` 为准**（那边 0.20），这里
#: 保留一份是因为确定性截断路径（`compact_messages`）自己要用，而
#: `app/session_plane.py::describe()` 会解析这一行核对两边一致（复写会漂移，
#: 断言不会）。只压到"刚够用"会让下一轮工具结果立刻又撑爆，压到 20% 才有余量。
COMPACT_TARGET_RATIO = 0.2
#: 触发提前压缩的占用比。与 `app/compaction.py::COMPACT_THRESHOLD` 同值，判据本身
#: 走 `compaction.should_compact`——这里只给 `describe()` 一个可解析的锚点。
COMPACT_THRESHOLD_RATIO = 0.5
#: 提前压缩折叠到哪里的两个 state 键（存在 `context_meta` 里，随 checkpoint 落盘）。
#: 为什么不改 `messages`：它挂了 `add_messages` reducer，节点只能**追加**不能替换，
#: 想在 state 里"删掉最老的几轮"没有合法写法。所以折叠事实记在这里：已折叠的条数
#: 加一份摘要文本，下一轮从 `messages[folded:]` 接着用。
COMPACT_FOLDED_KEY = "compacted_messages"
COMPACT_SUMMARY_KEY = "compaction_summary"
#: 摘要连续失败几次就暂时别再试了。摘要是一次模型调用，模型挂了的时候"每轮都试一次
#: 摘要"等于把失败成本翻倍；而"永不重试"又会在模型恢复后再也不压。所以给它一个
#: 上限：连败 `COMPACT_SUMMARY_ATTEMPTS` 次后直接走确定性截断，任务照常跑完。
COMPACT_FAILED_KEY = "compaction_summary_failures"
COMPACT_SUMMARY_ATTEMPTS = 2
#: 单条消息压缩后的地板。压到几十字符等于把工具结果变成噪声，模型会开始猜。
MIN_COMPACT_CHARS = 2_000
#: 截断标记。**必须显式告诉模型"这里被截了"**：静默截断会让它以为输出就到这儿，
#: 于是照着半截日志下结论。
COMPACT_MARK = "…[上下文压缩：此处已截断]"
#: 工具执行期检查中断的间隔（秒）。0.25 s 是"用户按下中断到这一步真的停下"的
#: 上限代价：再小就是空转轮询，再大就会让"我点了停止，它还卡了两秒"。
INTERRUPT_POLL_SECONDS = 0.25
#: 线程被中断时的终态正文。说清"为什么停"比只说"已停止"有用：用户知道这不是
#: 模型放弃，而是有人/某条路径主动叫停，可以接着再发一次。
INTERRUPTED_ANSWER = "这条线程已被中断，任务在这里停止。可以重新发一次指令继续。"
#: 技能简述注入系统提示的上限（同簇只取第一个命中）
SKILL_INJECT_LIMIT = 5
STOP_LABELS = {
    "max_actions": "达到工具动作上限",
    "max_tokens": "达到 token 预算上限",
}


class ModelFailureError(RuntimeError):
    """模型调用失败且已无补救（不可重试 / 次数用完 / 已吐出半截正文）。

    文案由 `errors.summary(failure, attempts)` 生成：**先说类别与建议，再说试了几次**。
    这句话会原样进任务终态（`Task.error`）给用户看，所以它必须说"下一步做什么"，
    而不是只有一句"请检查连接设置"——真实原因是 400 时，那句话会让人去查密钥。
    """


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

    def __init__(self, runner, *, summarizer=None):
        self.runner = runner
        # 心跳：优先用 runner 上的那个（进程级、CLI/事件流可读 `status()`）；
        # 假 runner（测试替身）没有就本地造一个——心跳不该成为"能不能跑"的前提。
        self.heartbeat = getattr(runner, "heartbeat", None) or Heartbeat()
        # 摘要注入点（MATRIX 2.7）：`callable(messages, prior="") -> str | Awaitable[str]`。
        # 构造参数优先，其次 `runner.summarizer`——两条路都是为了让测试把"摘要这一步"
        # 换成假实现，**不真连模型**（压缩路径的正确性不该依赖网络）。
        self.summarizer = summarizer if summarizer is not None else getattr(
            runner, "summarizer", None
        )
        # 工具执行期轮询中断的间隔。测试把它调到毫秒级，就不必为"能不能中断"
        # 真等 0.25 s（那会让每个中断用例慢一个数量级）。
        self.interrupt_poll_seconds = INTERRUPT_POLL_SECONDS

    def beat(self, kind: str) -> None:
        """节点进入时报一次进展。**不改编排**：图结构、边、路由一律不动。"""
        self.heartbeat.beat(kind)

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

    # ── 中断（MATRIX 2.4 / 2.8）────────────────────────────────────────────
    def interrupted(self, thread_id: str) -> bool:
        """这条线程是否已被要求中断。

        `runner` 支持 per-thread 中断时问它；测试替身没有这个能力就返回 False
        （"永不中断"是安全的默认：不会因为少一个方法就把正常任务当成被中断）。

        三处用它，语义各不相同：

        * `think`：断了就不再请求模型（省一次没人要的回答）；
        * `execute` 入口：断了就整轮跳过，直接补中断结果；
        * `run_tool` 轮询：**已经发出**的那一次调用也会被放弃——这是 2.8 从
          "标记 + 跳过"升级成"真的打断"的那一处，边界写在 `run_tool` 的 docstring 里。
        """
        checker = getattr(self.runner, "is_interrupted", None)
        return bool(checker(thread_id)) if callable(checker) else False

    def interrupted_updates(self, state) -> dict[str, Any]:
        """把**当前这一轮所有**待执行调用一次性补成中断结果。

        为什么不只补一个：一条 assistant 消息里可能有 N 个 `tool_call`，
        供应商要求 N 个都有结果。逐个补也行，但中间还要再走一遍
        digest → budget → gate，白跑几轮；一次补完最省也最不容易漏。
        """
        calls = list(state.get("calls") or [])
        messages = []
        for call in calls:
            result = retry.interrupted_result(str(call.get("name") or ""))
            messages.append(
                ToolMessage(content=as_tool_text(result), tool_call_id=call["id"])
            )
        return {
            "messages": messages,
            "calls": [],
            "actions": int(state.get("actions") or 0) + len(calls),
            "answer": {},
            "waiting": {},
            # 故意不写 `last_result`：中断不是一次工具结果，不该进 `tool_results`
            # 表（那会让"这个工具被调用过几次"的统计失真）。
            "last_result": {},
        }

    def interrupted_answer(self, state) -> dict[str, Any]:
        """被中断的线程不再调模型：直接给一句终态正文，让图走 finalize 收尾。

        不调模型是有意的：线程都断了，再去请求一次只会多花一次 token，
        而且拿回来的回答属于"没人要的线程"。
        """
        log.warning("线程 %s 已被中断，跳过模型调用", state["task_id"])
        return {
            "messages": [AIMessage(content=INTERRUPTED_ANSWER)],
            "calls": [],
            "waiting": {},
            "answer": {},
        }

    # ── prepare ────────────────────────────────────────────────────────────
    async def prepare(self, state):
        """装载上下文并初始化进度字段；只有缺 messages 时才写入（避免重复追加）。"""
        self.beat("prepare")
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
        """把技能索引渲染成注入系统提示的一段（与检索片段区分开）。

        每条只给 **name + 60 字符描述**，不给正文：索引是**路由信号**，模型据此
        判断"要不要 `skill_load` 取全文"。把正文塞进索引等于把提示词预算一次性烧光，
        而且越相关的技能越多、越烧——这是个正反馈。

        描述缺失时退回触发词：老技能（frontmatter 里没写 description）仍要能被召回，
        否则升级一次索引就把历史技能全变成不可发现的。
        """
        if not skills:
            return ""
        from app.skill_index import index_description

        lines = [
            "以下技能（流程做法）可能相关，判断相关时用 skill_load 取全文："
        ]
        for skill in skills:
            name = skill.get("name") or ""
            scope = skill.get("scope") or "global"
            summary = index_description(skill.get("description") or "")
            if not summary:
                triggers = (skill.get("triggers") or [])[:2]
                summary = "；".join(triggers)
            lines.append(f"- {name}（{scope}）：{summary}" if summary else f"- {name}（{scope}）")
        return "\n".join(lines)

    # ── think ──────────────────────────────────────────────────────────────
    async def think(self, state):
        self.beat("think")
        if self.interrupted(state["task_id"]):
            # 被中断的线程不再请求模型（见 `interrupted_answer`）。
            return self.interrupted_answer(state)
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
        history = [m for m in state.get("messages") or [] if m.type != "system"]
        # ── 提前压缩（MATRIX 10.4 / 2.7）───────────────────────────────────
        # **每轮开始先判一次**，而不是等供应商报 `context_overflow`。差这一次往返
        # 的意义不只是省 token：溢出那一轮必然失败，用户看到的是报错，还要多等
        # 几百毫秒到几秒的重试。判据本身是纯函数（`app/compaction.py::plan`）。
        folded = int(meta.get(COMPACT_FOLDED_KEY) or 0)
        summary = str(meta.get(COMPACT_SUMMARY_KEY) or "")
        if folded > len(history):
            # 脏 checkpoint（折叠数比历史还多）：按"没折过"重来，而不是切出空历史。
            folded, summary = 0, ""
        tail = history[folded:]
        window_tokens = window_of(self.settings)
        # `carried_tokens` 是"不在 tail 里、但确实会进上下文"的占用：系统提示与
        # 已有摘要。它们不能参与分组——摘要是最老的一条，被当成一组就会先被丢掉。
        carried = compaction.tokens_for(context) + (
            compaction.estimate_tokens(summary) if summary else 0
        )
        decision = compaction.plan(tail, window=window_tokens, carried_tokens=carried)
        compacted_now = False
        if decision.needed:
            failures = int(meta.get(COMPACT_FAILED_KEY) or 0)
            if failures >= COMPACT_SUMMARY_ATTEMPTS:
                # 摘要连着失败过几次：这一轮别再花一次调用去试，直接走截断。
                await self.store.event(
                    task.id,
                    "context_compacted",
                    reason="summary_skipped",
                    failures=failures,
                    before_tokens=decision.used_tokens,
                    window_tokens=decision.window,
                    target_tokens=decision.target_tokens,
                )
            else:
                folded, summary, compacted_now, attempted = await self.proactive_compact(
                    tail, decision, task, folded=folded, summary=summary
                )
                tail = history[folded:]
                if attempted:
                    new_failures = 0 if compacted_now else failures + 1
                    if new_failures:
                        meta_updates[COMPACT_FAILED_KEY] = new_failures
                    elif failures:
                        meta_updates[COMPACT_FAILED_KEY] = 0
            if folded:
                meta_updates[COMPACT_FOLDED_KEY] = folded
            if summary:
                meta_updates[COMPACT_SUMMARY_KEY] = summary
        # 配对修补（MATRIX 2.4）：任务被取消/进程被杀时，checkpoint 里可能留下
        # "有 tool_calls 却没有 tool 结果"的 assistant 消息。这种消息原样发出去就是
        # 供应商 400，而且此后**每一轮**都带着这个非法配对，整个会话等于废了。
        # 这里补一条中断占位再发（不改 state，历史保持原样）。
        repaired = retry.repair_tool_pairs(tail)
        if repaired:
            log.warning("补齐 %d 条缺失的工具结果（中断占位）", len(repaired))
            await self.store.event(task.id, "tool_pairs_repaired", count=len(repaired))
        digest = [HumanMessage(content=summary)] if summary else []
        messages = [*context, *digest, *tail, *repaired]
        if decision.needed and not compacted_now:
            # 摘要不可用 → 退回**确定性截断**（MATRIX 2.7 的原路径）。只影响本轮
            # 发送，不改 state：下一轮再判一次，模型恢复了就还能走摘要。
            body = self.compact_messages([*context, *tail, *repaired], window_tokens=window_tokens)
            messages = [body[0], *digest, *body[1:]]
        elif decision.needed and compaction.tokens_for(messages) > decision.target_tokens:
            # 摘要成功但最后一组自己就超预算（比如一次吐了 20 万字符的工具结果）：
            # 只截正文，不丢组——摘要已经把更早的历史接住了。
            messages = self.truncate_bodies(messages, decision.target_tokens)
        specs = self.policies.exposer.expose(self.registry.specs())
        base_model = runner.get_model()
        try:
            model = base_model.bind_tools(specs, parallel_tool_calls=False)
        except NotImplementedError as exc:
            # 有些传输不支持工具绑定（`bedrock_converse` 的早期实现、自建的
            # anthropic / gemini 客户端都抛 NotImplementedError）。
            #
            # **不能让整个任务崩在这**——那等于"这个供应商完全不能用"。
            # 但也**不能假装有工具**：清空 specs，模型会如实看到"没有工具可用"，
            # 而不是收到一份它调不动、调了必然失败的清单。
            # 记一条事件，这样 `orbit sessions show` 里能查到原因。
            model = base_model
            specs = []
            await self.store.event(
                state["task_id"],
                "tool_binding_unsupported",
                reason=str(exc)[:300],
                provider=str(getattr(self.settings, "model_provider", "") or ""),
            )
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
            # 轮次计数（MATRIX 2.2）：`actions` 数工具调用，**这一条数循环次数**。
            # 两者必须分开：一个"每轮只思考、不调工具"的循环 `actions` 永远是 0，
            # 只靠 `max_actions` 拦不住它，任务会一直转下去。
            "turns": int(state.get("turns") or 0) + 1,
        }
        if meta_updates:
            # 恢复旧 checkpoint 时补冻的前缀要写回状态，否则每轮 think 都会重算一次。
            updates["context_meta"] = {**meta, **meta_updates}
        return updates

    async def proactive_compact(self, tail, decision, task, *, folded, summary):
        """提前压缩：把要丢的那几组交给模型压成一段（MATRIX 10.4）。

        返回 `(folded, summary, ok, attempted)`：

        * `attempted=False`：根本没有可折叠的轮次（只有当前这一组），调用方什么
          都不用做（`ok` 也是 False）；
        * `ok=True`：摘要已生成，`folded` 增加、`summary` 换成**合并后**的新摘要；
        * `ok=False`（且 `attempted=True`）：摘要不可用（模型失败 / 摘要太短），
          调用方退回确定性截断，并把"又失败了一次"记进 `context_meta`。
          **压缩失败绝不能让任务失败**——它是优化，不是功能。

        摘要必须与已有摘要**合并**而不是追加：`prior` 一并交给模型，否则每压一次
        就丢一段更早的历史，任务越跑越失忆（这是"分段摘要"最常见的坑）。
        """
        task_id = task.id
        dropped = [tail[index] for index in decision.summarized]
        if not dropped:
            return folded, summary, False, False
        digest = await self.summarize(dropped, task, prior=summary)
        if not compaction.usable_summary(digest):
            await self.store.event(
                task_id,
                "context_compacted",
                reason="summary_failed",
                before_tokens=decision.used_tokens,
                dropped=len(dropped),
                window_tokens=decision.window,
                target_tokens=decision.target_tokens,
            )
            log.warning(
                "提前压缩：摘要不可用，退回确定性截断（%s token）", decision.used_tokens
            )
            return folded, summary, False, True
        folded_now = folded + len(dropped)
        text = compaction.summary_message(
            digest,
            folded=folded_now,
            window=decision.window,
            target=decision.target_tokens,
        )
        await self.store.event(
            task_id,
            "context_compacted",
            reason="proactive",
            before_tokens=decision.used_tokens,
            after_tokens=decision.projected_tokens,
            dropped=len(dropped),
            folded=folded_now,
            window_tokens=decision.window,
            target_tokens=decision.target_tokens,
        )
        return folded_now, text, True, True

    async def summarize(self, messages, task, *, prior: str = "") -> str:
        """把这几条消息压成一段摘要。**失败返回空串，绝不抛。**

        注入点：`Nodes(summarizer=...)` 或 `runner.summarizer`，签名
        `callable(messages, prior="") -> str | Awaitable[str]`。测试用它把"摘要这一步"
        换成假实现，不真连模型——压缩路径的正确性不该依赖网络。

        生产路径走 `ask_model`（同一个模型、同一套失败分类），但
        `allow_compact=False`：摘要请求自己再溢出时不能再触发压缩，否则就是递归。

        **必须还原 `runner.partial`**：`stream_model` 会把流式正文累加进去，而
        `partial` 是"这一轮已经吐给用户的半截回答"的判据。不还原的话，主调用一旦
        失败就会被误判成"已流式输出"，于是本该重试的失败被直接判死。
        """
        injected = self.summarizer
        if injected is not None:
            try:
                try:
                    value = injected(list(messages), prior=prior)
                except TypeError:
                    # 只收一个位置参数的假实现（测试里最省事的写法）也要能接。
                    value = injected(list(messages))
                if inspect.isawaitable(value):
                    value = await value
                return str(value or "").strip()
            except Exception as exc:  # 注入实现挂了同样只退化成截断
                log.warning("注入的摘要实现失败（退回确定性截断）：%s", exc)
                return ""
        get_model = getattr(self.runner, "get_model", None)
        if not callable(get_model):
            return ""  # 测试替身没有模型：不是错误，直接走截断
        task_id = task.id
        identity = {
            "conversation_id": getattr(task, "conversation_id", ""),
            "message_id": f"compaction-{task_id}",
            "response_id": uuid4().hex,
            "prompt_version": PROMPT_VERSION,
        }
        partials = getattr(self.runner, "partial", None)
        had_partial = isinstance(partials, dict) and task_id in partials
        saved = partials.get(task_id, "") if isinstance(partials, dict) else ""
        if isinstance(partials, dict):
            partials.setdefault(task_id, "")
        try:
            prompt = [HumanMessage(content=compaction.summary_prompt(messages, prior=prior))]
            answer = await self.ask_model(
                get_model(), prompt, task, identity, allow_compact=False
            )
            return str(getattr(answer, "text", "") or "").strip()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 压缩是优化：任何失败都退回截断
            log.warning("摘要压缩失败（退回确定性截断）：%s", exc)
            return ""
        finally:
            if isinstance(partials, dict):
                if had_partial:
                    partials[task_id] = saved
                else:
                    partials.pop(task_id, None)

    async def ask_model(self, model, messages, task, identity, *, allow_compact: bool = True):
        """模型调用：按失败类别决定"重试 / 先压缩再重试 / 立刻停"（MATRIX 2.6 / 2.7）。

        **改之前**：任何异常都重试 `MODEL_ATTEMPTS` 次，最后统一说"请检查连接设置、
        API 地址与密钥"。这有两个问题：401 重试三次还是 401，只是让用户多等三个
        来回；而真实原因是 400 时，那句统一文案把人引去查密钥——查错了方向。

        **改之后**：判据全在 `app/errors_retry.py::decide`（分类来自 `app/errors.py`），
        这里只执行：
        * `COMPACT`：上下文溢出 → 先压缩消息再重试一次（压缩不占常规次数）。
          压缩走 `compact_context`：**摘要优先，失败退回确定性截断**；
        * `RETRY`：只有限流/超时/连接/过载四类会走到这里；
        * `STOP`：不可重试、次数用完、或**已流出半截正文**——直接抛
          `ModelFailureError(errors.summary(...))`，文案带类别 + 建议 + 次数。

        `allow_compact=False` 只给"摘要请求自己"用（见 `summarize`）：那条路径上再
        触发压缩就是递归，所以溢出时直接按失败处理，由调用方退回截断。

        **保留的旧规矩**：已吐出正文时**原样抛**（不是包成 `ModelFailureError`）。
        流式回答已经推给前端了，重试会把重复内容再推一遍；而原异常带着真实类型与栈，
        比一句包装过的话更好排障。
        """
        task_id = task.id
        attempts = 0
        compacted = False
        working = list(messages)
        last: BaseException | None = None
        failure: errors.Failure | None = None
        while True:
            attempts += 1
            # 钩子发射点（MATRIX 13.4）：插件的 `register_hook` 订阅的就是这些名字。
            # **包 try**：钩子是旁路，旁路出问题不该让主循环死——但也不能静默，
            # 所以记一条 warning（`orbit logs` 能看到）。
            try:
                hooks.emit(
                    "pre_llm_call",
                    {"task_id": task_id, "attempt": attempts, "model": str(model)},
                )
            except Exception as exc:  # noqa: BLE001 —— 旁路，见上
                log.warning("pre_llm_call 钩子出错（已忽略）：%s", exc)
            try:
                result = await self.stream_model(model, working, task_id, identity)
                try:
                    hooks.emit(
                        "post_llm_call",
                        {"task_id": task_id, "attempt": attempts, "ok": True},
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("post_llm_call 钩子出错（已忽略）：%s", exc)
                return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last = exc
                decision = retry.decide(
                    exc,
                    attempt=attempts,
                    attempts=MODEL_ATTEMPTS,
                    has_partial=bool(self.runner.partial.get(task_id)),
                    compacted=compacted,
                )
                failure = decision.failure
                if decision.reason == "partial":
                    # 半截正文已经在路上：重试只会让用户看到重复内容。
                    raise
                if decision.action == retry.COMPACT:
                    if not allow_compact:
                        # 摘要请求自己溢出：不能再压（递归），按失败处理。
                        break
                    compacted = True
                    before = self._context_size(working)
                    working, path = await self.compact_context(working, task)
                    after = self._context_size(working)
                    attempts -= 1  # 压缩重试是"这一次调用的补救"，不占常规次数
                    log.warning(
                        "上下文溢出，压缩后重试（%s）：%s → %s 字符（%s）",
                        path,
                        before,
                        after,
                        failure.detail[:120],
                    )
                    await self.store.event(
                        task_id,
                        "context_compacted",
                        before_chars=before,
                        after_chars=after,
                        window_tokens=thresholds_for(self.settings).window_tokens,
                        reason=failure.kind,
                        path=path,
                    )
                    continue
                if decision.action != retry.RETRY:
                    break
                log.warning(
                    "模型调用失败（第 %s 次，%s）：%s", attempts, failure.kind, exc
                )
                await self.store.event(
                    task_id,
                    "model_retry",
                    attempt=attempts,
                    error=type(exc).__name__,
                    # 注意：载荷里**不能**用 `kind` 这个键——`Store.event` 的第二个
                    # 位置参数就叫 `kind`（事件类型），同名的关键字会直接 TypeError。
                    failure_kind=failure.kind,
                )
        assert failure is not None  # 循环只可能从 except 分支走到 break
        raise ModelFailureError(errors.summary(failure, attempts)) from last

    def _context_size(self, messages) -> int:
        """消息总字符数。只用于"压缩前后有没有变小"的观测与断言。"""
        total = 0
        for message in messages:
            content = getattr(message, "content", "")
            total += len(content) if isinstance(content, str) else len(str(content))
        return total

    async def compact_context(self, messages, task, *, window_tokens: int | None = None):
        """被动路径（供应商已经报了溢出）：**摘要优先，失败退回确定性截断**。

        返回 `(messages, path)`，`path` 是 `"summary"` / `"truncate"`，只用于事件与日志。

        与 `proactive_compact` 的区别：那边把"折叠到第几条"记进 state（下一轮继续
        从那儿接着用），这边是一次性补救——压缩结果只用于本次重试，state 里的历史
        保持原样（`messages` 挂 `add_messages`，节点无法替换，只能追加）。
        """
        window = int(window_tokens or window_of(self.settings))
        target = compaction.target_tokens(window)
        items = list(messages)
        system = [m for m in items if compaction.is_system(m)]
        rest = [m for m in items if not compaction.is_system(m)]
        groups = compaction.group_messages(rest)
        # 最后一组是"当前正在做的事"，压它等于让模型忘记自己刚才在干嘛。
        dropped_index = (
            [index for group in groups[:-1] for index in group] if len(groups) > 1 else []
        )
        if dropped_index:
            dropped = [rest[index] for index in dropped_index]
            digest = await self.summarize(dropped, task)
            if compaction.usable_summary(digest):
                keep = [
                    message
                    for index, message in enumerate(rest)
                    if index not in set(dropped_index)
                ]
                head = HumanMessage(
                    content=compaction.summary_message(
                        digest, folded=len(dropped), window=window, target=target
                    )
                )
                compacted = [*system, head, *keep]
                if compaction.tokens_for(compacted) > target:
                    compacted = self.truncate_bodies(compacted, target)
                return compacted, "summary"
        return self.compact_messages(messages, window_tokens=window), "truncate"

    def truncate_bodies(self, messages, target_tokens: int):
        """只截正文、**不删消息、不拆配对**：给每条正文一个平均配额。

        用在"摘要已经接住了更早的历史，但最后一组自己就超预算"的场合：这时候再丢组
        就是把当前这一步也丢了，只能把正文压短。
        """
        system = [m for m in messages if compaction.is_system(m)]
        rest = [m for m in messages if not compaction.is_system(m)]
        if not rest:
            return list(messages)
        budget = max(MIN_COMPACT_CHARS, int(target_tokens) * compaction.CHARS_PER_TOKEN)
        allowance = max(MIN_COMPACT_CHARS, budget // len(rest))
        return [*system, *[self._truncate_message(m, allowance) for m in rest]]

    def compact_messages(self, messages, *, window_tokens: int | None = None):
        """上下文溢出后的**本地确定性压缩**（MATRIX 2.7 的兜底动作）。

        ## 压什么、不压什么

        1. **系统提示一字不动**：它是"冻结前缀"，任务内必须逐字节稳定（提示缓存 +
           任务自洽，见 `prepare` 的注释）。压它会同时打掉缓存和语义一致性。
        2. **只压正文，不拆配对**：`tool_calls` 与 `tool` 结果必须成对存在（供应商硬
           约束）。所以绝不"删掉中间几条消息"，而是先把每条正文截短；仍然超预算时，
           才**整组**丢弃最老的轮次（一组 = 一条带 tool_calls 的 assistant + 它后面
           全部 tool 结果），永远不留下半个配对。
        3. **目标是窗口的 20%**（`COMPACT_TARGET_RATIO`，同 Hermes
           `compression.target_ratio: 0.2`）：只压到"刚好不溢出"会让下一轮工具结果
           立刻又撑爆，来回压缩比直接报错更糟。

        ## 为什么它退居"兜底"（原来它是一线）

        原来这里是**唯一**的压缩手段：不花 token、不依赖网络、可单测，代价是丢掉正文
        细节而不是压成摘要。现在一线是 `summarize`（走同一个模型，`app/compaction.py`
        组装请求），本地截断只在两种情况下接手：

        1. 摘要失败（模型挂了 / 超时 / 回了一句"好的"）；
        2. 摘要请求本身也溢出（`allow_compact=False`，不能递归）。

        **压缩是优化，不该让任务失败**——所以这条兜底路径必须永远可用，绝不能删。
        """
        limits: Thresholds = (
            thresholds(window_tokens) if window_tokens else thresholds_for(self.settings)
        )
        target = max(MIN_COMPACT_CHARS, int(limits.window_chars * COMPACT_TARGET_RATIO))
        kept = [m for m in messages if getattr(m, "type", "") == "system"]
        rest = [m for m in messages if getattr(m, "type", "") != "system"]
        if not rest:
            return list(messages)
        allowance = max(MIN_COMPACT_CHARS, target // len(rest))
        compacted = [self._truncate_message(m, allowance) for m in rest]
        if self._context_size(compacted) > target:
            compacted = self._drop_oldest_rounds(compacted, target)
        return [*kept, *compacted]

    @staticmethod
    def _truncate_message(message, limit: int):
        """截短单条消息正文（保留头 2/3 + 尾 1/3，中间打标记）。

        为什么留尾巴：工具输出（日志、报错、命令结果）的**结论在末尾**，
        只留头部会把"最后报了什么错"截掉，模型于是照着半截输出下结论。
        """
        content = getattr(message, "content", "")
        if not isinstance(content, str) or len(content) <= limit:
            return message
        head = max(0, limit * 2 // 3)
        tail = max(0, limit - head)
        body = content[:head] + f"\n{COMPACT_MARK}\n" + (content[-tail:] if tail else "")
        copy = getattr(message, "model_copy", None)
        if not callable(copy):
            return message
        return message.model_copy(update={"content": body})

    def _drop_oldest_rounds(self, messages, target: int):
        """按"整组"从最老的开始丢，直到总字符数进预算。

        一组 = 一条 assistant 消息 + 紧随其后的全部 `tool` 结果（配对不拆开）。
        **永远保留最后一组**：那是"当前正在做的事"，丢掉它模型就不知道自己
        刚才在干嘛了。
        """
        groups: list[list] = []
        for message in messages:
            if groups and getattr(message, "type", "") == "tool":
                groups[-1].append(message)
            else:
                groups.append([message])
        total = self._context_size(messages)
        while total > target and len(groups) > 1:
            dropped = groups.pop(0)
            total -= self._context_size(dropped)
        return [message for group in groups for message in group]

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
        self.beat("gate")
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
        self.beat("execute")
        call = state["calls"][0]
        task_id = state["task_id"]
        if self.interrupted(task_id):
            # per-thread 中断（MATRIX 2.8）：这条线程被标记后**不再真的执行工具**，
            # 但仍然为**每一个**待执行的调用产出一条正常的工具结果——见
            # `errors_retry` 的模块 docstring：抛异常会让 `tool_calls` 与
            # `tool_results` 当场不配对，下一次请求直接被供应商 400。
            return self.interrupted_updates(state)
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
        # 工具执行期的心跳（MATRIX 2.3）：带 `tool:` 前缀的 beat 会把计时切成
        # "工具内"档（上限 1200 s 而不是 450 s）——编译、跑测试本来就慢，
        # 用空闲档去量它们必然误判。见 app/heartbeat.py。
        self.beat(f"{TOOL_PREFIX}{call['name']}")
        # 传 messages 是因为「记忆写入门槛」（M2）必须读到用户原话：
        # 只认用户显式说过的「记住……」，模型自行沉淀一律拒绝。
        result, was_interrupted = await self.run_tool(task_id, operation_id, call, state)
        if was_interrupted:
            # 跑到一半被中断（MATRIX 2.8）：这一调用**放弃**，并且**这一轮剩下的
            # 调用一起补成中断结果**。不能只补一条——一条 assistant 消息里的 N 个
            # `tool_call` 必须各有一条结果，缺一个下一次请求就是 400。
            return self.interrupted_updates(state)
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

    async def run_tool(self, task_id, operation_id, call, state):
        """执行一次工具调用，**能被 per-thread 中断放弃**（MATRIX 2.8）。

        返回 `(result, was_interrupted)`。中断时 `result` 为 `None`，由调用方补
        `[interrupted]` 结果（配对不能断，见 `app/errors_retry.py`）。

        ## 到底能中断到什么程度（边界，如实写清）

        工具调用被包成一个独立 `Task`，这里每 `interrupt_poll_seconds` 秒问一次
        `interrupted(task_id)`，命中就 `cancel()` 它并**不再等结果**。所以：

        * **还没发出的**：直接被跳过（`execute` 入口就判，根本不会调到这里）；
        * **已经发出、还在等 I/O 的**（模型/HTTP/浏览器等待、`await` 在事件循环上）：
          真的会被打断——`CancelledError` 注入在下一个 await 点；
        * **已经派到线程/子进程里、正在跑的同步活**（`asyncio.to_thread` 包住的
          CPU 活、`subprocess` 已经在执行的命令）：`cancel()` 只会**放弃等待**，
          那个线程/子进程不会被 kill——Python 没有跨平台的安全线程中止，硬杀
          subprocess 又要区分"谁派的、能不能杀"。这一小段是**唯一**不可中断的窗口，
          它会自己跑完（结果被丢弃）。

        不中断就得等它自然结束：一个 `run_command` 跑十分钟，用户点了停止也要等
        十分钟——那正是这次要修的东西。
        """
        # 钩子发射点（MATRIX 13.4）：工具派发前后。
        # `pre_tool_call` 拿到的是**将要执行的参数**，插件可以据此审计或记账；
        # 它**不能改参数**——改参数会让"日志里看到的"和"实际跑的"不一致，
        # 那正是审计最不能容忍的事。
        tool_name = str(call.get("name") or "")
        try:
            hooks.emit(
                "pre_tool_call",
                {"task_id": task_id, "tool": tool_name, "args": call.get("args") or {}},
            )
        except Exception as exc:  # noqa: BLE001 —— 旁路，见上
            log.warning("pre_tool_call 钩子出错（已忽略）：%s", exc)
        job = asyncio.ensure_future(
            self.registry.execute(
                task_id, operation_id, call["name"], call["args"], state.get("messages")
            )
        )
        try:
            while True:
                done, _ = await asyncio.wait({job}, timeout=self.interrupt_poll_seconds)
                if done:
                    break
                if self.interrupted(task_id):
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)
                    log.warning(
                        "线程 %s 在工具 %s 执行中被中断，已放弃这一调用",
                        task_id,
                        call.get("name"),
                    )
                    return None, True
            result = job.result()
            try:
                hooks.emit(
                    "post_tool_call",
                    {
                        "task_id": task_id,
                        "tool": tool_name,
                        "ok": True,
                        "result_chars": len(str(result)),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("post_tool_call 钩子出错（已忽略）：%s", exc)
            return result, False
        except asyncio.CancelledError:
            # 外层（`cancel()` / 进程退出）把整个任务取消了：别把工具 Task 留在
            # 后台孤儿跑——它还会继续写库、继续操作浏览器。
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)
            raise

    # ── digest ─────────────────────────────────────────────────────────────
    async def digest(self, state):
        """原文落库（checksum 幂等）+ 引用登记 + 大小观测；模型看到的内容不变。"""
        self.beat("digest")
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
        self.beat("reflect")
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
        self.beat("budget")
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
        self.beat("finalize")
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
