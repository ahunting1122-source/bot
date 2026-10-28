"""辅助模型槽位：压缩 / 摘要 / 标题 / 分类这些小活不该烧主模型的配额。

## 为什么需要槽位

一次对话里真正"贵"的往往不是主对话，而是那些**每轮都跑一次**的边角任务：
上下文压缩摘要、会话标题、工具结果分类。它们用主模型（`claude-opus-5` 之类）跑
等于"用卡车送一封信"——又贵又慢，还挤占主对话的配额。

Hermes 的做法是 `auxiliary.*` 下 18 个槽位（调研 06 §4.3），每个槽位可以单独配
`provider / model / base_url / api_key / timeout / extra_body / reasoning_effort`。
本模块把 18 个槽位**全部登记**（`SLOTS`），并且把"继承语义"这件事做对了——这是
18 个槽位里真正容易错的部分。

## 18 个槽位怎么来的（照 research 的清单，一个不多一个不少）

调研 06 §4.3 列出 18 个 `auxiliary.*` slot。Orbit 的映射关系：

| Orbit 槽位 | Hermes slot | 备注 |
|---|---|---|
| `summary` / `title` | `title_generation` | Hermes 是**一个**槽位，Orbit 早先拆成两个（既有行为，不改） |
| `compression` | `compression` | |
| `classifier` | `triage_specifier` | |
| `vision` | `vision` | 图片分析（`vision_analyze`），Orbit 之前完全没登记 |
| `skills_hub` / `approval` / `mcp` / `background_review` | 同名 | |
| `memory_query_rewrite` / `tts_audio_tags` / `kanban_decomposer` | 同名 | |
| `profile_describer` / `goal_judge` / `curator` / `monitor` | 同名 | |
| `moa_reference` / `moa_aggregator` | 同名 | MoA 的 reference / aggregator 槽（执行见 `app/moa.py`） |

于是"18 个 Hermes 槽位" ↔ "18 个 Orbit 槽位"靠两条**合并**对齐：

1. Hermes 的 `title_generation` 在 Orbit 占两个名字（`summary` + `title`）；
2. Hermes 的 `review` 与 `background_review` 在 Orbit 合成一个 `background_review`
   （前者没有默认 timeout、唯一独有字段是 `api_mode: ''`，后者是同一件事的后台形态）。

两条合并都写进 `HERMES_ALIASES`（Hermes 名 → Orbit 名），测试会断言"18 个 Hermes
槽位全部被覆盖"，避免以后有人以为漏抄了。

## 槽位清单（顺序固定，前 4 个不许动）

`summary` / `title` / `compression` / `classifier` / `vision` / `skills_hub` /
`approval` / `mcp` / `memory_query_rewrite` / `tts_audio_tags` /
`kanban_decomposer` / `profile_describer` / `goal_judge` / `curator` / `monitor` /
`background_review` / `moa_reference` / `moa_aggregator`。

## 继承语义：`inherited=True` 才是重点

`resolve_slot()` 一定返回一个**能用**的 `SlotModel`：槽位没配就退回主模型。
但"退回主模型"和"专门配了一个同款模型"在**成本上完全不是一回事**，而调用方
（预算护栏、`orbit model --aux` 的表格）必须能区分。所以返回值带 `inherited`：

- `inherited=False` → 用户显式配了这个槽位，可以放心按它算钱；
- `inherited=True` → **其实没配**，现在跑的是主模型。CLI 会把它显示成
  "继承（未配置）"，预算面板也会把它计入主模型那一路。

把这件事做静默（只返回模型名、不标来源），用户就会以为"我配了便宜的辅助模型"，
直到账单出来才发现一直在用主模型。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app import providers as pv

#: 本模块落地的槽位：**18 个**，与 Hermes 的 `auxiliary.*` 一一对齐（映射见模块
#: docstring 的表）。前 4 个是 Orbit 既有的名字，**顺序与名字都不许动**——CLI 的
#: `--aux` 表格与回归用例都按它们的前缀断言。
SLOTS: tuple[str, ...] = (
    # ── 既有 4 个（行为不变）──
    "summary",
    "title",
    "compression",
    "classifier",
    # ── 本轮补齐的 14 个 ──
    "vision",
    "skills_hub",
    "approval",
    "mcp",
    "memory_query_rewrite",
    "tts_audio_tags",
    "kanban_decomposer",
    "profile_describer",
    "goal_judge",
    "curator",
    "monitor",
    "background_review",
    "moa_reference",
    "moa_aggregator",
)

#: Hermes 侧对应的槽位名（`auxiliary.<name>`）。**照抄 Hermes 的名字**，
#: 这样用户从 Hermes 迁过来的 `config.yaml` 片段能直接对上，不用猜别名。
HERMES_NAMES: dict[str, str] = {
    "summary": "title_generation",  # 摘要与标题在 Hermes 侧是两个槽位，这里合并登记
    "title": "title_generation",
    "compression": "compression",
    "classifier": "triage_specifier",
    "vision": "vision",
    "skills_hub": "skills_hub",
    "approval": "approval",
    "mcp": "mcp",
    "memory_query_rewrite": "memory_query_rewrite",
    "tts_audio_tags": "tts_audio_tags",
    "kanban_decomposer": "kanban_decomposer",
    "profile_describer": "profile_describer",
    "goal_judge": "goal_judge",
    "curator": "curator",
    "monitor": "monitor",
    "background_review": "background_review",
    "moa_reference": "moa_reference",
    "moa_aggregator": "moa_aggregator",
}

#: Hermes 槽位名 → Orbit 槽位名（反向索引，含**合并**关系）。
#:
#: `review` 指向 `background_review`：两者是同一件事的同步/异步两面，Orbit 只留一个
#: 槽位名（否则 18 个 Hermes 槽位会撑成 19 个 Orbit 槽位）。写进表里而不是只在
#: 注释里说，是为了让"迁移 Hermes 配置"这件事可以按表查、也可以被测试断言。
HERMES_ALIASES: dict[str, str] = {
    **{hermes: orbit for orbit, hermes in HERMES_NAMES.items()},
    "review": "background_review",
}

#: 每个槽位的默认超时（秒）。数值照 Hermes `auxiliary.*` 的默认值（调研 06 §4.3）：
#: 压缩/审查类给 120（要读完整段历史），标题/分类给 30（只读一小段），
#: MoA 两槽给 900（多模型汇总，Hermes 原文就是 900）。
#:
#: **既有 4 个的值一个都不改**（`classifier` 保持 30，尽管 Hermes 的
#: `triage_specifier` 是 120——改它会静默改变已上线行为）。
DEFAULT_TIMEOUT: dict[str, int] = {
    "summary": 120,
    "title": 30,
    "compression": 120,
    "classifier": 30,
    "vision": 120,
    "skills_hub": 30,
    "approval": 30,
    "mcp": 30,
    "memory_query_rewrite": 8,
    "tts_audio_tags": 30,
    "kanban_decomposer": 180,
    "profile_describer": 60,
    "goal_judge": 60,
    "curator": 600,
    "monitor": 60,
    "background_review": 120,
    "moa_reference": 900,
    "moa_aggregator": 900,
}

#: 每个槽位"该用多小的模型"的意图说明。CLI 会展示，避免用户配了个旗舰模型
#: 还以为是省钱的。
SLOT_PURPOSE: dict[str, str] = {
    "summary": "会话摘要（多轮压缩后的事实提要）",
    "title": "会话标题生成",
    "compression": "上下文压缩（超窗时把历史压成摘要）",
    "classifier": "小任务分类 / 分诊判定",
    "vision": "图片分析（vision_analyze 工具）",
    "skills_hub": "技能 hub 检索",
    "approval": "审批判定（这条命令要不要拦）",
    "mcp": "MCP 工具描述处理",
    "memory_query_rewrite": "记忆检索 query 改写",
    "tts_audio_tags": "TTS 音频标签生成",
    "kanban_decomposer": "看板任务拆解",
    "profile_describer": "profile 描述生成",
    "goal_judge": "目标达成判定",
    "curator": "技能策展",
    "monitor": "监控判定",
    "background_review": "后台审查（含 Hermes 的 review 语义）",
    "moa_reference": "MoA reference 槽（多模型并行作答）",
    "moa_aggregator": "MoA aggregator 槽（汇总成最终答案）",
}

#: 槽位类型：`text` / `vision` / `audio`。**不是装饰**：
#: - `vision` 槽位必须挑一个 `supports_vision=True` 的模型（`capabilities.resolve`
#:   可以查），配错会静默"看不见图"；
#: - `audio` 槽位跑的是 TTS 标签生成，输入带时长/语速信息，超时与配额算法不同。
#: CLI 按这个字段分组展示，调用方按它决定要不要做多模态编码。
SLOT_KIND: dict[str, str] = {
    **{name: "text" for name in SLOTS},
    "vision": "vision",
    "tts_audio_tags": "audio",
}

#: 单价表：`provider/model → (输入, 输出)`，单位 **USD / 1M tokens**。
#:
#: **来源**：models.dev 快照（Hermes `0.21.0-cn.18` 内置的
#: `agent/models_dev_snapshot.json`，`cost.input` / `cost.output` 字段）。
#: 这是**公开标价**，会变（也会因为阶梯价、缓存价、区域价而不同）；本表只用于
#: "这个槽位大概花多少"的量级判断，**不能当账单**。取不到的模型走
#: `FALLBACK_PRICE`，并在返回值里标 `priced=False`——估不出来就说估不出来，
#: 不要给一个看起来很确定的 0。
PRICES: dict[tuple[str, str], tuple[float, float]] = {
    # deepseek
    ("deepseek", "deepseek-v4-pro"): (0.435, 0.87),
    ("deepseek", "deepseek-v4-flash"): (0.14, 0.28),
    ("deepseek", "deepseek-chat"): (0.14, 0.28),
    # openai
    ("openai", "gpt-5.4"): (2.5, 15.0),
    ("openai", "gpt-5.4-mini"): (0.75, 4.5),
    ("openai", "gpt-5.5"): (5.0, 30.0),
    # anthropic
    ("anthropic", "claude-opus-5"): (5.0, 25.0),
    ("anthropic", "claude-sonnet-5"): (2.0, 10.0),
    ("anthropic", "claude-haiku-4-5-20251001"): (1.0, 5.0),
    # gemini
    ("gemini", "gemini-3.5-flash"): (1.5, 9.0),
    ("gemini", "gemini-3.6-flash"): (1.5, 7.5),
    ("gemini", "gemini-4-argon"): (1.5, 7.5),
    # zai
    ("zai", "glm-5.2"): (1.4, 4.4),
    ("zai", "glm-5"): (1.0, 3.2),
    ("zai", "glm-4.5-flash"): (0.0, 0.0),
    # kimi / moonshot
    ("kimi-coding", "kimi-k3"): (3.0, 15.0),
    ("kimi-coding", "kimi-k2.5"): (0.6, 3.0),
    ("moonshot", "kimi-k2.5"): (0.6, 3.0),
    ("moonshot", "moonshot-v1-128k"): (0.6, 3.0),
    ("moonshot", "moonshot-v1-32k"): (0.6, 3.0),
    # qwen
    ("qwen", "qwen3-max"): (1.2, 6.0),
    ("qwen", "qwen-flash"): (0.05, 0.4),
    ("qwen", "qwen-vl-max"): (0.8, 3.2),
    # minimax
    ("minimax", "MiniMax-M2.7"): (0.3, 1.2),
    ("minimax", "MiniMax-M3"): (0.3, 1.2),
    # mistral / groq / xai
    ("mistral", "mistral-large-latest"): (0.5, 1.5),
    ("mistral", "mistral-small-latest"): (0.15, 0.6),
    ("groq", "llama-3.3-70b-versatile"): (0.59, 0.79),
    ("groq", "openai/gpt-oss-120b"): (0.15, 0.6),
    ("xai", "grok-4.5"): (2.0, 6.0),
    # 本地 / 自建端点：没有账单，按 0 计（`priced=False` 会一起返回）
    ("ollama", "*"): (0.0, 0.0),
    ("custom", "*"): (0.0, 0.0),
}

#: 查不到单价时的兜底：按"中档云端模型"估，**并标 `priced=False`**。
#: 取 0 会让"未知模型"看起来免费，比估高更危险。
FALLBACK_PRICE: tuple[float, float] = (1.0, 5.0)


@dataclass(frozen=True)
class SlotModel:
    """一个槽位最终用哪个模型。

    `inherited=True` 表示"槽位没配，退回主模型"——这不是错误，但必须可见。
    """

    slot: str
    provider: str
    model: str
    base_url: str = ""
    inherited: bool = True
    timeout: int = 30
    reasoning_effort: str = ""
    #: 配置来自哪里：`slot`（槽位专用配置）/ `main`（主模型）/ `default`（出厂兜底）。
    source: str = "main"
    #: 该槽位想干什么（CLI 展示用）。
    purpose: str = ""

    def label(self) -> str:
        return f"{self.provider}/{self.model}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "inherited": self.inherited,
            "timeout": self.timeout,
            "reasoning_effort": self.reasoning_effort,
            "source": self.source,
            "purpose": self.purpose,
        }


@dataclass(frozen=True)
class CostEstimate:
    """一次成本估算。`priced=False` = 单价是兜底值，不是查到的。"""

    slot: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
    input_price: float
    output_price: float
    usd: float
    priced: bool = True
    inherited: bool = True
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "provider": self.provider,
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "input_price": self.input_price,
            "output_price": self.output_price,
            "usd": round(self.usd, 6),
            "priced": self.priced,
            "inherited": self.inherited,
            "note": self.note,
        }


def _aux_config(settings: Any) -> dict[str, Any]:
    """读槽位配置。

    两个来源，都只读不写：

    1. `settings.auxiliary`（如果 `Settings` 将来加了字段，直接可用）；
    2. `<data_dir>/settings.json` 里的 `auxiliary` 键——与 CLI 的运行时设置同一个
       文件，用户 `orbit model aux --set summary=...` 写进去后这里立刻读得到。

    坏文件当空：配置文件坏了只该退回"全部继承主模型"，不该让摘要直接崩。
    """
    inline = getattr(settings, "auxiliary", None)
    if isinstance(inline, dict):
        return inline
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is None:
        return {}
    try:
        data = json.loads((Path(data_dir) / "settings.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    aux = data.get("auxiliary")
    return aux if isinstance(aux, dict) else {}


def _main_slot(settings: Any) -> tuple[str, str, str]:
    """主模型那一站：(provider, model, base_url)。"""
    try:
        profile = pv.resolve_provider(settings)
        provider = profile.id
    except pv.ProviderError:
        provider = "custom"
    model = str(getattr(settings, "model", "") or "")
    base_url = str(getattr(settings, "model_base_url", "") or "")
    return provider, model, base_url


def resolve_slot(name: str, settings: Any) -> SlotModel:
    """决定一个槽位用哪个模型：**有专用配置用专用，没有就退回主模型**。

    专用配置的字段与 Hermes 一致（`provider` / `model` / `base_url` /
    `timeout` / `reasoning_effort`），但**不认明文 `api_key`**：密钥只进系统
    keyring（`doctor` 的 ORBIT-002 通告就是这个承诺）。
    """
    slot = (name or "").strip().lower()
    if slot not in SLOTS:
        raise ValueError(f"未知槽位 `{name}`（可用：{' / '.join(SLOTS)}）")

    main_provider, main_model, main_base = _main_slot(settings)
    raw = _aux_config(settings).get(slot)
    entry = raw if isinstance(raw, dict) else {}

    provider = str(entry.get("provider", "") or "").strip() or main_provider
    model = str(entry.get("model", "") or "").strip()
    base_url = str(entry.get("base_url", "") or "").strip()

    if model or str(entry.get("provider", "") or "").strip():
        # 只要填了 provider 或 model 任一项，就算"显式配过"。
        # 只填 provider 时模型名取该家默认值——否则会拼出一个不存在的组合。
        if not model:
            profile = pv.PROVIDERS.get(provider)
            model = profile.default_model if profile is not None else ""
        inherited = False
        source = "slot"
        if not base_url:
            profile = pv.PROVIDERS.get(provider)
            base_url = profile.default_base_url if profile is not None else ""
    else:
        inherited = True
        source = "main"
        model = main_model
        base_url = main_base

    timeout = entry.get("timeout")
    try:
        timeout_value = int(timeout) if timeout is not None else DEFAULT_TIMEOUT[slot]
    except (TypeError, ValueError):
        timeout_value = DEFAULT_TIMEOUT[slot]

    return SlotModel(
        slot=slot,
        provider=provider,
        model=model,
        base_url=base_url,
        inherited=inherited,
        timeout=timeout_value,
        reasoning_effort=str(entry.get("reasoning_effort", "") or "").strip(),
        source=source,
        purpose=SLOT_PURPOSE[slot],
    )


def resolve_all(settings: Any) -> list[SlotModel]:
    """全部槽位的解析结果（CLI 表格用）。"""
    return [resolve_slot(name, settings) for name in SLOTS]


def _split_tokens(tokens: int | dict[str, int]) -> tuple[int, int]:
    """`tokens` 既可以是总数（按 8:2 拆输入/输出），也可以是显式映射。

    8:2 是摘要类任务的典型比例：读一大段历史、吐一小段摘要。估错方向会让
    "贵不贵"的判断整体偏一档，所以把比例写在注释里而不是散在代码里。
    """
    if isinstance(tokens, dict):
        return int(tokens.get("input", 0)), int(tokens.get("output", 0))
    total = max(0, int(tokens))
    return int(total * 0.8), total - int(total * 0.8)


def price_of(provider: str, model: str) -> tuple[tuple[float, float], bool]:
    """查单价，返回 `((输入, 输出), 是否查到了)`。"""
    hit = PRICES.get((provider, model))
    if hit is not None:
        return hit, True
    wildcard = PRICES.get((provider, "*"))
    if wildcard is not None:
        # 本地/自建端点：真的没有账单，0 是**正确**的值，不是"估不出来"。
        return wildcard, True
    return FALLBACK_PRICE, False


def slot_kind(name: str) -> str:
    """槽位类型（`text` / `vision` / `audio`）。未知槽位直接抛错，与 `resolve_slot` 一致。"""
    slot = (name or "").strip().lower()
    if slot not in SLOTS:
        raise ValueError(f"未知槽位 `{name}`（可用：{' / '.join(SLOTS)}）")
    return SLOT_KIND[slot]


def estimate_cost(
    slot: str,
    tokens: int | dict[str, int],
    settings: Any = None,
    *,
    model: str = "",
    provider: str = "",
) -> CostEstimate:
    """按单价表估算一个槽位这次调用花多少美元。

    单价来源见 `PRICES` 的注释：**公开标价，会变**。查不到就用 `FALLBACK_PRICE`
    并把 `priced` 置 False——一个看起来很确定的错数字比"估不出来"更糟。
    """
    resolved = resolve_slot(slot, settings) if settings is not None else None
    provider = provider or (resolved.provider if resolved else "")
    model = model or (resolved.model if resolved else "")
    if not provider or not model:
        raise ValueError(
            f"槽位 `{slot}` 没有可用模型：要么传 settings，要么显式给 provider/model"
        )
    input_tokens, output_tokens = _split_tokens(tokens)
    (input_price, output_price), priced = price_of(provider, model)
    usd = (input_tokens / 1_000_000) * input_price + (output_tokens / 1_000_000) * output_price
    note = "" if priced else "单价表里没有这个模型，按中档云端模型兜底估值"
    return CostEstimate(
        slot=slot,
        provider=provider,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        input_price=input_price,
        output_price=output_price,
        usd=usd,
        priced=priced,
        inherited=resolved.inherited if resolved is not None else True,
        note=note,
    )


__all__ = [
    "DEFAULT_TIMEOUT",
    "FALLBACK_PRICE",
    "HERMES_ALIASES",
    "HERMES_NAMES",
    "PRICES",
    "SLOTS",
    "SLOT_KIND",
    "SLOT_PURPOSE",
    "CostEstimate",
    "SlotModel",
    "estimate_cost",
    "price_of",
    "resolve_all",
    "resolve_slot",
    "slot_kind",
]
