"""推理（reasoning）线格式翻译表：把"思考过程"在四类落点之间来回翻。

## 为什么必须逐家写，没有通用解

调研 06 §3.5 的结论很直白：16 家 provider 把推理参数放在**五个不同的落点**，
还带互斥规则、拒绝规则和强制规则。没有通用解，只能逐家写一张表：

| 落点 | 谁在用 | 形状 |
|---|---|---|
| `extra_body.thinking` | deepseek / zai / kimi | `{"type": "enabled"}` 或 `{"type": "disabled"}` |
| 顶层 `reasoning_effort` | deepseek / zai / kimi / openai-o | `"low"` / `"medium"` / `"high"` |
| `thinking` 对象（带预算） | anthropic | `{"type": "enabled", "budget_tokens": 4096}` |
| `generationConfig.thinkingConfig` | gemini | `{"thinkingBudget": 4096, "includeThoughts": true}` |
| `extra_body.reasoning` 对象 | openrouter / nous | `{"enabled": true, "effort": "high"}` |

**响应侧同样五花八门**：`reasoning_content`（deepseek / zai / kimi）、`reasoning`
（openai-o / Responses）、`content[].thinking` 块（anthropic）、`parts[].thought`
（gemini）、以及正文里的 `<thinking>` 标签（Ollama 之类本地模型）。抽错了不会报错，
只会让"思考过程"静默混进给用户看的正文里。

## 三条不许违反的规则

1. **互斥不能破**：Kimi 同时发 `thinking` 与 `reasoning_effort` 会 400
   `"cannot specify both"`。所以 `kimi-coding` 的翻译只给一边。
2. **拒绝的词表不能发**：GLM-5.2 最低 `high`（发 `low` 会 400）、Kimi 不认
   `medium`、Ollama Cloud 不认 `minimal`。翻译表负责 **clamp**，不是把词原样丢过去。
3. **不支持就说不支持**：没有翻译器的 provider 抛 `NotSupported`，绝不返回一个
   "看起来能跑"的空 dict——那会让用户以为"关掉思考"生效了（Z.AI GLM-4.5+ 默认
   思考开，不发 `disabled` 就等于没关，这个坑 Hermes 修过）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: 统一的推理档位。**只有这三档**——多一档就得多维护一张 16 行的映射表，
#: 而调用方（`orbit model reasoning --level`）根本用不到更细的粒度。
LEVELS: tuple[str, ...] = ("low", "medium", "high")

#: 额外的"关掉"档。它不是"最低档"：`low` 仍然是开着的思考，`off` 才是关。
#: 两者混为一谈会导致"我说关掉，实际还在烧 token"。
OFF = "off"

#: 本模块认识的 provider（= `TRANSLATORS` 的键）。写在这里是为了给报错文案用，
#: 免得每处都去 `sorted(TRANSLATORS)`。
SUPPORTED_PROVIDERS: tuple[str, ...] = (
    "anthropic",
    "deepseek",
    "gemini",
    "kimi-coding",
    "ollama",
    "openai",
    "zai",
)


class NotSupported(RuntimeError):
    """该 provider 没有推理线格式翻译器。

    消息里必须同时给出"哪些支持"和"为什么这家不支持"，否则用户只会去猜是不是
    自己配错了 provider 名。
    """


@dataclass(frozen=True)
class ReasoningPart:
    """从响应里抽出来的推理内容。

    `signature` 只对 anthropic 有意义：思考块带签名，**多轮回放时必须原样带回**，
    否则下一轮 400。`raw` 留原始块，便于排障时看到"到底抽的是哪一段"。
    """

    text: str
    provider: str
    #: 内容来自哪个字段/形态（`reasoning_content` / `content[].thinking` / …）。
    field_name: str = ""
    signature: str = ""
    raw: Any = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "field": self.field_name,
            "chars": len(self.text),
            "has_signature": bool(self.signature),
            "text": self.text,
        }


@dataclass(frozen=True)
class Translator:
    """一家的推理线格式：响应里在哪、请求里发什么、有哪些坑。"""

    provider: str
    #: 响应侧落点的人类可读名字（进 CLI 表格）。
    field_name: str
    #: 该家真正接受的档位词表（`LEVELS` 的子集，或含 `off`）。
    levels: tuple[str, ...]
    #: 该家的坑，写清"为什么这么翻"。CLI 会原样展示。
    note: str
    extract: Callable[[Any], ReasoningPart | None]
    request: Callable[[str], dict[str, Any]]


# ── 取值小工具（dict 与对象都认）────────────────────────────────────────────


def _dig(obj: Any, *keys: str | int) -> Any:
    """按路径取值。dict / 对象 / 列表下标都认，任一层缺失就返回 None。

    为什么要这么宽容：同一个 provider 的响应可能是**原始 JSON**（httpx 客户端）、
    也可能是 **LangChain 的 `AIMessage`**（`ChatOpenAI`），两者形状完全不同。
    解析器要是只认一种，换条路径就静默抽不到推理内容。
    """
    current = obj
    for key in keys:
        if current is None:
            return None
        if isinstance(key, int):
            if not isinstance(current, (list, tuple)) or len(current) <= key:
                return None
            current = current[key]
        elif isinstance(current, dict):
            current = current.get(key)
        else:
            current = getattr(current, key, None)
    return current


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return ""


def _join(values: list[Any]) -> str:
    return "\n".join(part for part in (_text(item) for item in values) if part)


# ── deepseek / zai / kimi：`reasoning_content` ─────────────────────────────


def _extract_reasoning_content(provider: str) -> Callable[[Any], ReasoningPart | None]:
    """`reasoning_content` 三处都要认：LangChain 塞进 `additional_kwargs`，
    原始 OpenAI 面在 `choices[0].message`，有些网关直接摊平在顶层。"""

    def extract(response: Any) -> ReasoningPart | None:
        candidates = (
            _dig(response, "additional_kwargs", "reasoning_content"),
            _dig(response, "choices", 0, "message", "reasoning_content"),
            _dig(response, "message", "reasoning_content"),
            _dig(response, "reasoning_content"),
        )
        for value in candidates:
            text = _text(value)
            if text:
                return ReasoningPart(
                    text=text, provider=provider, field_name="reasoning_content", raw=response
                )
        return None

    return extract


def _deepseek_request(level: str) -> dict[str, Any]:
    """DeepSeek V4+：`thinking` 开关在 extra_body，**档位走顶层**。

    `extra_body` 里放顶层字段会变成"以为发了顶层字段其实埋在 extra_body 里"的
    静默错误（`providers.py` 的 `ExtraBodyHook` 就是为这件事写的）。
    """
    if level == OFF:
        return {"extra_body": {"thinking": {"type": "disabled"}}, "top_level": {}}
    return {
        "extra_body": {"thinking": {"type": "enabled"}},
        "top_level": {"reasoning_effort": level},
    }


def _zai_request(level: str) -> dict[str, Any]:
    """GLM：默认思考**是开的**，不发 `disabled` 就等于没关。

    GLM-5.2 的档位词表最低 `high`（发 `low`/`medium` 会 400），所以向上 clamp。
    """
    if level == OFF:
        return {"extra_body": {"thinking": {"type": "disabled"}}, "top_level": {}}
    return {
        "extra_body": {"thinking": {"type": "enabled"}},
        "top_level": {"reasoning_effort": "high" if level in ("low", "medium") else level},
    }


def _kimi_request(level: str) -> dict[str, Any]:
    """Kimi：`thinking` 与顶层 `reasoning_effort` **互斥**，同时发 → 400。

    有档位时只发顶层，且词表是 low/high/max —— `medium` 向上 clamp 成 `high`。
    """
    if level == OFF:
        return {"extra_body": {"thinking": {"type": "disabled"}}, "top_level": {}}
    return {
        "extra_body": {},
        "top_level": {"reasoning_effort": "high" if level == "medium" else level},
    }


# ── anthropic：`content[]` 里的 thinking 块 ────────────────────────────────

#: 思考预算（token）。Anthropic 的下限是 1024，`low` 贴着下限取。
_ANTHROPIC_BUDGET = {"low": 1024, "medium": 4096, "high": 16384}


def _extract_anthropic(response: Any) -> ReasoningPart | None:
    blocks = _dig(response, "content")
    if isinstance(blocks, list):
        texts, signature = [], ""
        for block in blocks:
            if _dig(block, "type") != "thinking":
                continue
            text = _text(_dig(block, "thinking")) or _text(_dig(block, "text"))
            if text:
                texts.append(text)
                signature = signature or _text(_dig(block, "signature"))
        if texts:
            return ReasoningPart(
                text="\n".join(texts),
                provider="anthropic",
                field_name="content[].thinking",
                signature=signature,
                raw=response,
            )
    # LangChain 的 Anthropic 适配器会把思考块摊到 additional_kwargs。
    text = _text(_dig(response, "additional_kwargs", "thinking"))
    if text:
        return ReasoningPart(
            text=text, provider="anthropic", field_name="additional_kwargs.thinking", raw=response
        )
    return None


def _anthropic_request(level: str) -> dict[str, Any]:
    """Anthropic 用带预算的 `thinking` 对象；**关掉的方式是省略字段**。

    没有 `{"type": "disabled"}` 这种写法——硬塞会 400。所以 `off` 返回空 dict。
    """
    if level == OFF:
        return {"extra_body": {}, "top_level": {}}
    return {
        "extra_body": {},
        "top_level": {
            "thinking": {"type": "enabled", "budget_tokens": _ANTHROPIC_BUDGET[level]}
        },
    }


# ── gemini：`parts[].thought` ───────────────────────────────────────────────

#: 思考预算（token）。0 = 关闭，这是 Gemini 唯一真正"关得掉"的写法。
_GEMINI_BUDGET = {"low": 1024, "medium": 8192, "high": 24576}


def _extract_gemini(response: Any) -> ReasoningPart | None:
    parts = _dig(response, "candidates", 0, "content", "parts")
    if isinstance(parts, list):
        texts = [
            _text(_dig(part, "text"))
            for part in parts
            if _dig(part, "thought") is True
        ]
        joined = _join(texts)
        if joined:
            return ReasoningPart(
                text=joined, provider="gemini", field_name="parts[].thought", raw=response
            )
    return None


def _gemini_request(level: str) -> dict[str, Any]:
    """Gemini 原生面走 `generationConfig.thinkingConfig`（走 `/openai` 兼容子路径时
    才需要多套一层 `extra_body.google`，本层只做原生面，见 `providers.GeminiClient`）。
    """
    budget = 0 if level == OFF else _GEMINI_BUDGET[level]
    return {
        "extra_body": {},
        "top_level": {
            "generationConfig": {
                "thinkingConfig": {
                    "thinkingBudget": budget,
                    "includeThoughts": level != OFF,
                }
            }
        },
    }


# ── openai-o：顶层 `reasoning_effort` / Responses 的 `reasoning` 对象 ──────


def _extract_openai(response: Any) -> ReasoningPart | None:
    """三处都认：Responses 的 `output[].summary[].text`、`message.reasoning`、
    LangChain 的 `additional_kwargs.reasoning`。"""
    output = _dig(response, "output")
    if isinstance(output, list):
        texts: list[str] = []
        for item in output:
            if _dig(item, "type") != "reasoning":
                continue
            summary = _dig(item, "summary")
            if isinstance(summary, list):
                texts.extend(_text(_dig(entry, "text")) for entry in summary)
        joined = _join(texts)
        if joined:
            return ReasoningPart(
                text=joined, provider="openai", field_name="output[].reasoning.summary",
                raw=response,
            )
    for path, name in (
        (("additional_kwargs", "reasoning"), "additional_kwargs.reasoning"),
        (("choices", 0, "message", "reasoning"), "message.reasoning"),
        (("reasoning",), "reasoning"),
    ):
        value = _dig(response, *path)
        if isinstance(value, dict):
            summary = value.get("summary")
            if isinstance(summary, list):
                joined = _join([_text(_dig(entry, "text")) for entry in summary])
                if joined:
                    return ReasoningPart(
                        text=joined, provider="openai", field_name=name, raw=response
                    )
        text = _text(value)
        if text:
            return ReasoningPart(text=text, provider="openai", field_name=name, raw=response)
    return None


def _openai_request(level: str) -> dict[str, Any]:
    """o 系只认 low/medium/high；**关掉靠 `minimal`**（`none` 会被拒）。

    Responses API 那边是 `reasoning: {"effort": ...}` 对象，走哪条线由 transport 决定，
    本层只给"参数该长什么样"，不替 transport 做选择。
    """
    return {
        "extra_body": {},
        "top_level": {"reasoning_effort": "minimal" if level == OFF else level},
    }


# ── ollama / 本地模型：正文里的 `<thinking>` 标签 ──────────────────────────

#: 标签式思考。`re.DOTALL` 必须加：思考内容几乎总是多行的。
_TAG = re.compile(r"<thinking>(.*?)</thinking>", re.DOTALL | re.IGNORECASE)


def _extract_tagged(response: Any) -> ReasoningPart | None:
    """正文里的 `<thinking>…</thinking>`。

    抽出来之后调用方应该**把标签段从正文里剥掉**（`strip_tags`），否则思考内容会
    原样显示给用户——这是本地模型最常见的"答案里混着内心独白"。
    """
    content = _text(_dig(response, "content")) or _text(
        _dig(response, "choices", 0, "message", "content")
    )
    match = _TAG.search(content)
    if match is None:
        return None
    return ReasoningPart(
        text=match.group(1).strip(), provider="ollama", field_name="content.<thinking>",
        raw=response,
    )


def _ollama_request(level: str) -> dict[str, Any]:
    """Ollama 认 `extra_body.think`（布尔）。**只在 Ollama 端点上合法**：
    Mistral / Groq 等 OpenAI 兼容端点收到 `think` 会 422。
    """
    return {"extra_body": {"think": level != OFF}, "top_level": {}}


def strip_tags(text: str) -> str:
    """去掉正文里的 `<thinking>…</thinking>` 段（本地模型回放时用）。"""
    return _TAG.sub("", text or "").strip()


# ── 翻译表 ─────────────────────────────────────────────────────────────────

TRANSLATORS: dict[str, Translator] = {
    "deepseek": Translator(
        provider="deepseek",
        field_name="reasoning_content",
        levels=(OFF, *LEVELS),
        note=(
            "V4+ 才认 thinking（deepseek-v3* 收到也不生效）；"
            "**不回显 reasoning_content 会 400**，多轮里必须把上一轮的推理内容原样带回。"
        ),
        extract=_extract_reasoning_content("deepseek"),
        request=_deepseek_request,
    ),
    "zai": Translator(
        provider="zai",
        field_name="reasoning_content",
        levels=(OFF, *LEVELS),
        note=(
            "GLM-4.5+ **默认思考是开的**，不发 thinking=disabled 等于没关；"
            "GLM-5.2 的档位词表最低 high，low/medium 由翻译表向上 clamp。"
        ),
        extract=_extract_reasoning_content("zai"),
        request=_zai_request,
    ),
    "kimi-coding": Translator(
        provider="kimi-coding",
        field_name="reasoning_content",
        levels=(OFF, *LEVELS),
        note=(
            "thinking 与顶层 reasoning_effort **互斥**，同时发 → 400 "
            '"cannot specify both"；词表无 medium，由翻译表向上 clamp 成 high。'
        ),
        extract=_extract_reasoning_content("kimi-coding"),
        request=_kimi_request,
    ),
    "anthropic": Translator(
        provider="anthropic",
        field_name="content[].thinking",
        levels=(OFF, *LEVELS),
        note=(
            "思考块带 signature，多轮回放必须原样带回；budget_tokens 下限 1024；"
            "关掉的方式是**省略字段**（没有 type=disabled 这种写法）。"
        ),
        extract=_extract_anthropic,
        request=_anthropic_request,
    ),
    "gemini": Translator(
        provider="gemini",
        field_name="parts[].thought",
        levels=(OFF, *LEVELS),
        note=(
            "只有 thought=true 的 part 才是推理；thinkingBudget=0 才真正关闭；"
            "走 /openai 兼容子路径时要多套一层 extra_body.google（本层只做原生面）。"
        ),
        extract=_extract_gemini,
        request=_gemini_request,
    ),
    "openai": Translator(
        provider="openai",
        field_name="reasoning",
        levels=(OFF, *LEVELS),
        note=(
            "o 系/推理模型只认 low/medium/high，关闭用 minimal（none 会被拒）；"
            "Responses API 走 reasoning.effort 对象，具体线协议由 transport 决定。"
        ),
        extract=_extract_openai,
        request=_openai_request,
    ),
    "ollama": Translator(
        provider="ollama",
        field_name="content.<thinking>",
        levels=(OFF, "low", "medium", "high"),
        note=(
            "本地模型把思考写在正文的 <thinking> 标签里，抽出来后必须从正文剥掉，"
            "否则内心独白会直接显示给用户；extra_body.think 只在 Ollama 端点上合法。"
        ),
        extract=_extract_tagged,
        request=_ollama_request,
    ),
}


def _translator(provider: str) -> Translator:
    key = (provider or "").strip().lower()
    translator = TRANSLATORS.get(key)
    if translator is None:
        raise NotSupported(
            f"`{provider or '（空）'}` 没有推理线格式翻译器。已支持："
            f"{', '.join(sorted(TRANSLATORS))}。"
            "没有翻译器时**不会**发任何推理参数——假装支持会让\"关掉思考\"静默失效。"
        )
    return translator


def check_level(level: str, provider: str) -> str:
    """校验档位是否在该家的词表里；不合法就抛 `ValueError`（带该家的词表）。"""
    normalized = (level or "").strip().lower()
    translator = _translator(provider)
    if normalized not in translator.levels:
        raise ValueError(
            f"{translator.provider} 不接受档位 `{level}`（可用：{' / '.join(translator.levels)}）"
        )
    return normalized


def extract(response: Any, provider: str) -> ReasoningPart | None:
    """从响应里抽出推理内容。抽不到返回 None（**不是**空串——两者含义不同）。"""
    return _translator(provider).extract(response)


def to_request(level: str, provider: str) -> dict[str, Any]:
    """把统一档位翻成该家的请求参数。

    返回 `{"extra_body": {...}, "top_level": {...}}` 两段，**故意不合并**：
    `providers.py` 的 `ExtraBodyHook` 说明了混在一起的后果——顶层字段被埋进
    `extra_body`，请求 400 而日志看起来一切正常。
    """
    normalized = check_level(level, provider)
    return _translator(provider).request(normalized)


def describe(provider: str) -> dict[str, Any]:
    """CLI 表格用：这家抽哪、发哪、有哪些坑。"""
    translator = _translator(provider)
    return {
        "provider": translator.provider,
        "field": translator.field_name,
        "levels": list(translator.levels),
        "note": translator.note,
    }


__all__ = [
    "LEVELS",
    "OFF",
    "SUPPORTED_PROVIDERS",
    "TRANSLATORS",
    "NotSupported",
    "ReasoningPart",
    "Translator",
    "check_level",
    "describe",
    "extract",
    "strip_tags",
    "to_request",
]
