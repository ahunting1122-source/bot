"""MoA（Mixture of Agents）运行时：N 个 proposer 并发作答，1 个 aggregator 汇总。

## 为什么值得做

`orbit moa` 之前只**存配置**不执行（`model.py` 的 `run_moa_list` 里那句
"运行时聚合未实现"就是这个现状）。MoA 的价值在于：用几个便宜模型并行作答，
再让一个汇总模型挑长补短，质量常常接近旗舰单模型，而单价低一个量级。

## 三条硬要求（照调研 06 §4.5）

1. **并发**：proposer 必须真并发（`asyncio.gather`），串行跑就失去了"多个便宜模型
   同时干活"的意义——延迟会随模型数线性增长。
2. **失败容忍**：一个 proposer 挂了**不能整批失败**。少一个参考回答，聚合质量降一点，
   但比"整轮什么都没有"好得多。缺了谁必须**标注出来**传给 aggregator，
   否则汇总模型会把"只有 2 份参考"当成"只有 2 个角度"。
3. **预算护栏**：单个 proposer 有输出上限（`Proposer.max_tokens`），整批还有总预算。
   预算是**硬上限**：宁可少答、截断，也不能因为一次 `/moa` 把当月配额烧完。

## 可注入

`run(plan, prompt, *, call=...)` 的 `call` 是注入点：测试给一个假函数就能验证
并发、失败容忍、预算截断三件事，完全不需要网络。生产路径不传 `call`，走
`providers.build_chat_model`。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from app.policies import estimate_tokens

log = logging.getLogger("orbit.moa")

#: 一次 proposer / aggregator 调用。返回 `str` 或 `Awaitable[str]` 都接受——
#: 测试里写同步假函数更省事，生产实现是 async 的。
Call = Callable[["Proposer", str], Any]


@dataclass(frozen=True)
class Proposer:
    """一个参与作答的模型槽。`temperature` 分开配，是为了让几路答案真的不一样——
    全是 0 的话，三个同款模型会给出三段几乎相同的文字，聚合就白做了。
    """

    provider: str
    model: str = ""
    temperature: float = 0.7
    #: 单个 proposer 的输出上限（token）。0 = 用 plan 的默认值。
    max_tokens: int = 0

    def label(self) -> str:
        return f"{self.provider}/{self.model}" if self.model else self.provider

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }


@dataclass(frozen=True)
class MoAPlan:
    """一次 MoA 的完整计划：N 个 proposer + 1 个 aggregator + 两道上限。"""

    proposers: tuple[Proposer, ...]
    aggregator: Proposer
    #: 每个槽位的默认输出上限（token）。Hermes 的 `moa.presets.<name>.max_tokens` 默认 4096。
    max_tokens: int = 4096
    #: 整批总预算（token）。0 = 不限。**不是建议值**，是硬上限。
    budget_tokens: int = 0
    name: str = "default"

    def __post_init__(self) -> None:
        if not self.proposers:
            raise ValueError("MoA 至少需要一个 proposer（否则聚合无从谈起）")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens 必须是正整数")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "proposers": [item.to_dict() for item in self.proposers],
            "aggregator": self.aggregator.to_dict(),
            "max_tokens": self.max_tokens,
            "budget_tokens": self.budget_tokens,
        }


@dataclass(frozen=True)
class Proposal:
    """一个 proposer 的产出。`error` 非空表示它没答上来（不是空答案）。"""

    proposer: Proposer
    text: str = ""
    error: str = ""
    tokens: int = 0
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return not self.error

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposer": self.proposer.label(),
            "ok": self.ok,
            "tokens": self.tokens,
            "truncated": self.truncated,
            "error": self.error,
            "text": self.text,
        }


@dataclass(frozen=True)
class MoAResult:
    """聚合结果。`missing` 是**必须**传出去的：调用方/日志要能说清"这次少了谁"。"""

    text: str
    proposals: list[Proposal] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    aggregator: str = ""
    used_tokens: int = 0
    #: 是否因为总预算被截断过。
    budget_limited: bool = False

    @property
    def ok(self) -> bool:
        return bool(self.text)

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "aggregator": self.aggregator,
            "missing": list(self.missing),
            "used_tokens": self.used_tokens,
            "budget_limited": self.budget_limited,
            "proposals": [item.to_dict() for item in self.proposals],
        }


# ── 预算与截断 ──────────────────────────────────────────────────────────────


def per_slot_cap(plan: MoAPlan) -> int:
    """每个槽位能花多少 token。

    总预算按 **(proposer 数 + 1 个 aggregator)** 均分：aggregator 也要吃饭，
    不给它留份额的话汇总那一步必然超预算。均分而不是"先到先得"，是因为 proposer
    是**并发**跑的——先到先得在并发下等于随机。
    """
    if plan.budget_tokens <= 0:
        return plan.max_tokens
    slots = len(plan.proposers) + 1
    return max(1, min(plan.max_tokens, plan.budget_tokens // slots))


def truncate_to_tokens(text: str, limit: int) -> tuple[str, bool]:
    """按 token 上限截断文本，返回 `(文本, 是否截断过)`。

    用 `policies.estimate_tokens` 的同一把尺子（中文按字、其它按 4 字符≈1 token），
    因为整套系统的 token 计数都走它——换一把尺子会让"预算"和"计量"对不上。
    """
    if limit <= 0 or estimate_tokens(text) <= limit:
        return text, False
    cut = max(1, int(len(text) * limit / max(estimate_tokens(text), 1)))
    out = text[:cut]
    while estimate_tokens(out) > limit and cut > 1:
        cut = max(1, int(cut * 0.9))
        out = text[:cut]
    return out, True


# ── 聚合提示词 ──────────────────────────────────────────────────────────────

AGGREGATOR_SYSTEM = (
    "你是多模型答案的汇总者。下面有若干模型对同一问题的独立回答。"
    "你的任务是：取各家的正确内容、补上彼此的遗漏、消解矛盾，写出一份更好的最终答案。"
    "不要罗列「某某模型说了什么」，直接给结论；"
    "不要因为参考回答数量少而降低质量要求。"
)


def build_aggregator_prompt(plan: MoAPlan, prompt: str, proposals: Sequence[Proposal]) -> str:
    """把各 proposal 拼成 aggregator 的输入，**并把缺了谁写在最前面**。

    缺失信息放最前面而不是末尾：长上下文里末尾的内容更容易被模型忽略，而"这次
    只有 2 份参考"恰恰是它必须先知道的前提。
    """
    missing = [item.proposer.label() for item in proposals if not item.ok]
    head: list[str] = [AGGREGATOR_SYSTEM]
    if missing:
        head.append(
            f"注意：{len(proposals)} 个参考模型中 {len(missing)} 个未能作答"
            f"（{', '.join(missing)}）。不要假设它们的内容存在。"
        )
    head.append(f"## 用户问题\n{prompt}")
    body: list[str] = []
    for index, item in enumerate(proposals, start=1):
        if item.ok:
            body.append(f"## 参考回答 {index}（{item.proposer.label()}）\n{item.text}")
        else:
            body.append(f"## 参考回答 {index}（{item.proposer.label()}）\n（未作答：{item.error}）")
    return "\n\n".join([*head, *body, "## 你的汇总答案"])


# ── 默认调用器（生产路径）──────────────────────────────────────────────────


def provider_caller(settings: Any) -> Call:
    """按 `providers.build_chat_model` 造客户端并调用。**只在真正调用时才构造**，
    所以导入本模块不会碰网络。"""

    async def call(proposer: Proposer, prompt: str) -> str:
        from app import providers as pv

        profile = pv.PROVIDERS.get(proposer.provider)
        if profile is None:
            raise pv.UnknownProvider(f"MoA 槽位里的 provider `{proposer.provider}` 不存在")
        model = pv.build_chat_model(profile, settings, proposer.model or None)
        response = await model.ainvoke([{"role": "user", "content": prompt}])
        return str(getattr(response, "text", "") or "")

    return call


async def _invoke(call: Call, proposer: Proposer, prompt: str) -> str:
    """调用一次，同步假函数也认（`inspect.isawaitable` 判断，不靠 try/except）。"""
    result = call(proposer, prompt)
    if inspect.isawaitable(result):
        result = await result
    return str(result or "")


# ── 主流程 ─────────────────────────────────────────────────────────────────


async def run(
    plan: MoAPlan,
    prompt: str,
    *,
    call: Call | None = None,
    settings: Any = None,
) -> MoAResult:
    """跑一次 MoA：并发 proposer → 聚合。

    `call` 不传时用 `provider_caller(settings)`；两者都不给就抛错——**绝不静默造
    一个假答案**，"没配好"必须让人看见。
    """
    if call is None:
        if settings is None:
            raise ValueError(
                "MoA 需要 `call`（注入的调用器）或 `settings`（用来构造真实模型）之一"
            )
        call = provider_caller(settings)

    cap = per_slot_cap(plan)
    budget_limited = plan.budget_tokens > 0 and cap < plan.max_tokens
    if budget_limited:
        log.warning(
            "MoA 预算收紧：总预算 %s token，%s 个槽位均分后每槽上限 %s（原 %s）",
            plan.budget_tokens,
            len(plan.proposers) + 1,
            cap,
            plan.max_tokens,
        )

    async def one(proposer: Proposer) -> Proposal:
        limit = min(cap, proposer.max_tokens or cap)
        try:
            text = await _invoke(call, proposer, prompt)
        except Exception as exc:  # noqa: BLE001 —— 单槽失败不该拖垮整批
            log.warning("MoA proposer %s 失败（忽略，继续聚合）：%s", proposer.label(), exc)
            return Proposal(proposer=proposer, error=f"{type(exc).__name__}: {exc}")
        if not text.strip():
            log.warning("MoA proposer %s 返回空文本（按未作答处理）", proposer.label())
            return Proposal(proposer=proposer, error="返回空文本")
        text, truncated = truncate_to_tokens(text, limit)
        return Proposal(
            proposer=proposer, text=text, tokens=estimate_tokens(text), truncated=truncated
        )

    proposals = list(await asyncio.gather(*(one(item) for item in plan.proposers)))
    missing = [item.proposer.label() for item in proposals if not item.ok]
    if missing:
        log.warning("MoA 聚合时缺 %s 份参考回答：%s", len(missing), ", ".join(missing))

    if all(not item.ok for item in proposals):
        # 全挂：不要去问 aggregator（它只会拿到一堆错误信息，白花一次钱）。
        detail = "；".join(f"{item.proposer.label()} → {item.error}" for item in proposals)
        raise MoAFailed(f"MoA 全部 {len(proposals)} 个 proposer 都失败：{detail}")

    aggregator_prompt = build_aggregator_prompt(plan, prompt, proposals)
    answer = await _invoke(call, plan.aggregator, aggregator_prompt)
    answer, answer_truncated = truncate_to_tokens(answer, cap)
    used = sum(item.tokens for item in proposals) + estimate_tokens(answer)
    return MoAResult(
        text=answer,
        proposals=proposals,
        missing=missing,
        aggregator=plan.aggregator.label(),
        used_tokens=used,
        budget_limited=budget_limited or answer_truncated,
    )


class MoAFailed(RuntimeError):
    """所有 proposer 都失败——此时聚合没有意义，直接抛（带每一路的原因）。"""


# ── 与 `orbit moa configure` 存的预设对接 ──────────────────────────────────


def _proposer(raw: object, *, default_temperature: float = 0.7) -> Proposer | None:
    if not isinstance(raw, dict):
        return None
    provider = str(raw.get("provider", "") or "").strip()
    if not provider:
        return None
    try:
        temperature = float(raw.get("temperature", default_temperature))
    except (TypeError, ValueError):
        temperature = default_temperature
    try:
        max_tokens = int(raw.get("max_tokens", 0) or 0)
    except (TypeError, ValueError):
        max_tokens = 0
    return Proposer(
        provider=provider,
        model=str(raw.get("model", "") or "").strip(),
        temperature=temperature,
        max_tokens=max_tokens,
    )


def plan_from_preset(preset: object, name: str = "default") -> MoAPlan:
    """把 `settings.json` 里 `moa_presets.<name>` 的形状翻成 `MoAPlan`。

    字段名照 Hermes（`reference_models[]` / `aggregator` / `max_tokens`），
    这样 `orbit moa configure` 存的预设不用改形状就能跑。
    """
    if not isinstance(preset, dict):
        raise ValueError(f"MoA 预设 `{name}` 形状不对（期望 dict）")
    references = preset.get("reference_models")
    proposers = [
        item
        for item in (_proposer(raw) for raw in (references if isinstance(references, list) else []))
        if item is not None
    ]
    aggregator = _proposer(preset.get("aggregator"), default_temperature=0.3)
    if aggregator is None:
        raise ValueError(f"MoA 预设 `{name}` 缺 aggregator（至少一个汇总模型）")
    try:
        max_tokens = int(preset.get("max_tokens", 4096) or 4096)
    except (TypeError, ValueError):
        max_tokens = 4096
    try:
        budget = int(preset.get("budget_tokens", 0) or 0)
    except (TypeError, ValueError):
        budget = 0
    return MoAPlan(
        proposers=tuple(proposers),
        aggregator=aggregator,
        max_tokens=max_tokens,
        budget_tokens=budget,
        name=name,
    )


__all__ = [
    "AGGREGATOR_SYSTEM",
    "Call",
    "MoAFailed",
    "MoAPlan",
    "MoAResult",
    "Proposal",
    "Proposer",
    "build_aggregator_prompt",
    "per_slot_cap",
    "plan_from_preset",
    "provider_caller",
    "run",
    "truncate_to_tokens",
]
