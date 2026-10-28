"""声明式 provider 注册表 + 三种线协议的客户端构造（模型适配层）。

## 为什么是"声明式表"而不是 if/else

Hermes 把 48 个 provider 收敛成 `ProviderProfile` 声明（
`handoff/hermes-parity/research/06-model-layer.md` §2.1 / §3.1）：**provider 身份**
（base_url / key 环境变量 / 鉴权方式）是数据，**线协议**（transport）是策略，两者
正交。每接一家就改核心代码的写法在第 10 家就不可维护，而且表达不了"一目录多
profile"（区域 / 协议 / 计费拆分，调研里 6 个目录就是这种）。所以这里照抄同一形状。

本文件只做三件事：

1. `PROVIDERS`：provider 身份表（含出厂能力元数据）；
2. `resolve_provider()`：从 `Settings` 反查当前 provider；
3. `build_chat_model()`：按 transport 造客户端，**做不到的能力明确抛错**。

## 数据来源（每条都标注，避免"看着像调研结论其实是猜的"）

- **A 类**：06 调研 §3.1 直接给出——provider 的 base_url、key 环境变量名、代表模型
  名，以及该 provider 在 models.dev 目录里的 tools / reasoning / vision 覆盖比例。
- **B 类**：06 调研 §7.2 的 models.dev 单模型样例。整份调研里**只有一例**
  （`gpt-5.4-mini`：context 400000 / input 272000 / output 128000）。
- **C 类**：厂商公开标称的常见值，**06 调研里没有**（调研只覆盖 Hermes 那 48 个
  profile，`openai` / `groq` / `mistral` / `moonshot` / `ollama` 都不在其中）。
  C 类一律按"保守取小"填：窗口估小只会让调用方提前压缩，估大会直接 400。

`context_window` 几乎全是 C 类——调研给的是"目录里有几个模型支持 tools"，不是每个
模型的窗口。准确值应该由运行时缓存（二级）覆盖，见 `record_capability()`。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import urllib.parse
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NoReturn, Protocol

import httpx
from pydantic import SecretStr

# ── 线协议 ──────────────────────────────────────────────────────────────────

#: Hermes 的 5 种 api_mode 里，本层实现了前 3 种（见调研 §2.1）。
Transport = Literal["chat_completions", "anthropic_messages", "gemini", "bedrock_converse"]

#: 已实现的线协议。顺序即 `build_chat_model` 的分派顺序。
SUPPORTED_TRANSPORTS: tuple[str, ...] = (
    "chat_completions",
    "anthropic_messages",
    "gemini",
)

#: 线协议 → 真正实现它的客户端。**这是"已实现"的物证**：矩阵里写"3 种已实现"
#: 不算证据，指得出类名才算。测试据此断言 `SUPPORTED_TRANSPORTS` 与实现一一对应，
#: 防止有人往 `SUPPORTED_TRANSPORTS` 里加一个没有实现的字符串。
TRANSPORT_CLIENTS: dict[str, str] = {
    "chat_completions": "langchain_openai.ChatOpenAI",
    "anthropic_messages": "app.providers.AnthropicMessagesClient",
    "gemini": "app.providers.GeminiClient",
}

#: 未实现的线协议 → 原因。**必须写清"为什么不行"**，否则用户只会看到一句
#: "不支持"，然后去猜是不是配置错了。
#:
#: `bedrock_converse` 这一轮**重新评估过**（MATRIX 4.2）。结论分两半，都要说清：
#:
#: **已做到（本文件里可查、可离线验证）**：SigV4 签名本身是纯标准库能实现的
#: （HMAC-SHA256 + canonical request），见 :func:`sign_sigv4`——它用 AWS 官方文档与
#: 官方测试套件的向量逐字段验证过（canonical request / string to sign / signature
#: 三段都对得上）。基于它，:class:`BedrockConverseClient` 能构造**已签名的**
#: Converse 请求并用 httpx 发出，`bedrock_available()` 检查
#: `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` / `AWS_REGION`。
#:
#: **仍做不到（如实说）**：**凭据链发现**——shared config、SSO、IMDS/ECS 角色、
#: AssumeRole。那是 boto3 的 `botocore.session` 在做的事，本机没装 boto3 也不能装；
#: 只认环境变量意味着"在 EC2/ECS/SSO 登录的机器上要手工导三把值"。
#:
#: **所以为什么还挂在 UNSUPPORTED 里**：`build_chat_model` 一旦放行这条 transport，
#: 就等于对全部调用点宣布"Bedrock 可用"——而本机没有 AWS 账号，端到端（真发一次
#: Converse、把 toolConfig 走通）**一次都没验证过**。这条线协议本轮只到"签名正确 +
#: 请求形状正确 + 客户端可被 MockTransport 驱动"，没到"跑通过"。把没跑通的东西标成
#: supported，比留一句"未实现"更贵：用户会照它去配 Bedrock，然后在第一轮对话里撞墙。
UNSUPPORTED_TRANSPORTS: dict[str, str] = {
    "bedrock_converse": (
        "AWS Bedrock Converse 的 SigV4 签名已用纯标准库实现（providers.sign_sigv4，"
        "按 AWS 官方向量逐字段验证），BedrockConverseClient 也能构造已签名请求并用 httpx "
        "发出；但凭据链发现（shared config / SSO / IMDS / AssumeRole）依赖 boto3，"
        "本机未装且禁止安装，只认 AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY / AWS_REGION "
        "三个环境变量。**且没有 AWS 账号，端到端一次都没跑通过**，所以这条 transport 仍"
        "明确报未实现，而不是标成 supported 让用户照着去配。"
    ),
}

# ── 异常 ────────────────────────────────────────────────────────────────────


class ProviderError(RuntimeError):
    """模型层的可预期失败。消息要能直接给人看（CLI 会原样打印）。"""


class UnknownProvider(ProviderError):
    """配置里写了注册表里没有的 provider id。"""


class UnsupportedTransport(ProviderError):
    """transport 不在已实现集合里。消息里必须带 `UNSUPPORTED_TRANSPORTS` 的原因。"""


class MissingCredential(ProviderError):
    """需要 api_key 的 provider 没有可用密钥。"""


# ── 能力元数据 ──────────────────────────────────────────────────────────────

#: 三级能力查询的字段名。刻意做成白名单：能力字段一旦自由扩展，缓存文件就会变成
#: 一个谁都读不懂的垃圾桶，而且 `capability()` 的拼写错误会静默返回兜底值。
CapabilityField = Literal["supports_tools", "supports_reasoning", "supports_vision", "context_window"]
CAPABILITY_FIELDS: tuple[str, ...] = (
    "supports_tools",
    "supports_reasoning",
    "supports_vision",
    "context_window",
)

#: 出厂兜底（三级查询的第三档）。**默认一律"不支持"**：
#: 调研 §8 #5 的原话是"不知道 `tool_call: false` → 对着不支持的模型发 tools 直接
#: 400"。所以未知模型按最保守处理；窗口给 8192 同理——估小只会让调用方提前压缩。
FACTORY_CAPABILITY: dict[str, Any] = {
    "supports_tools": False,
    "supports_reasoning": False,
    "supports_vision": False,
    "context_window": 8192,
}


def _caps(
    tools: bool, reasoning: bool, vision: bool, context_window: int
) -> dict[str, Any]:
    """一条手工能力覆盖。四个字段必须一起给——半张表最容易埋下"视觉支持了但没填"的坑。"""
    return {
        "supports_tools": tools,
        "supports_reasoning": reasoning,
        "supports_vision": vision,
        "context_window": context_window,
    }


# ── extra_body 钩子（推理参数的逐家线格式）──────────────────────────────────

#: `(model, reasoning_effort) -> extra_body 片段`。
#:
#: 为什么参数是 effort 而不是布尔：调研 §3.5 里"关推理"和"开推理+档位"落在**不同
#: 字段**（`extra_body.thinking` vs 顶层 `reasoning_effort` vs `reasoning` 对象），
#: 只有拿到 effort 才能决定发哪个。
#:
#: 注意：钩子**只返回 extra_body**。顶层字段（`reasoning_effort` / `verbosity`）故意
#: 不在这里拼——ChatOpenAI 的 `extra_body` 会把内容整体塞进请求体的顶层，混着放会
#: 出现"以为发了顶层字段其实埋在 extra_body 里"的静默错误。
ExtraBodyHook = Callable[[str, "str | None"], dict[str, Any]]


def _deepseek_extra(model: str, effort: str | None) -> dict[str, Any]:
    """DeepSeek V4+：`thinking` 开关在 extra_body，effort 走**顶层**（本钩子不管）。

    `deepseek-v3*` 收到 thinking 不生效（调研 §3.5 原话"V4+ 才生效"），所以老模型
    直接返回空——发一个不起作用的字段只会让日志骗人。
    """
    if not model.startswith("deepseek-v4"):
        return {}
    return {"thinking": {"type": "disabled" if effort is None else "enabled"}}


def _zai_extra(model: str, effort: str | None) -> dict[str, Any]:
    """Z.AI GLM：GLM-4.5+ 默认开思考，**不显式发 disabled 就等于没关**（调研修过这个坑）。

    GLM-4-9b 及更老完全不认这个字段，故跳过。
    """
    if model.startswith(("glm-4-9b", "glm-4-8b", "glm-4-6b")):
        return {}
    return {"thinking": {"type": "disabled" if effort is None else "enabled"}}


def _kimi_extra(model: str, effort: str | None) -> dict[str, Any]:
    """Kimi：`thinking` 与顶层 `reasoning_effort` **互斥**（同时发 → 400
    "cannot specify both"，调研 §3.5）。有 effort 时这里返回空，把话让给顶层字段。"""
    if effort is not None:
        return {}
    return {"thinking": {"type": "disabled"}}


def _ollama_extra(model: str, effort: str | None) -> dict[str, Any]:
    """Ollama 认 `extra_body.think`（布尔）。**只在 Ollama 端点上合法**：
    Mistral / Groq 等 OpenAI 兼容端点收到 `think` 会 422。"""
    return {"think": effort is not None}


# ── 另外两个 profile 钩子（MATRIX 4.1）─────────────────────────────────────

#: `() -> list[str]`：**纯本地**枚举该家当前可用的模型名。
#:
#: 为什么是"纯本地"：Hermes 的 `fetch_models` 会打 `GET /v1/models`。本机无网络、
#: 且本层禁止发真实请求，所以这里只允许"从已配置的端点推导"（例如自定义端点读
#: `Settings` 里用户填的模型名）或"返回出厂表"。钩子为 None = 没有更准的清单，
#: 调用方应退回 `profile.models` 的键。
#:
#: 签名不带参数（不接 `settings`）：接进去会让钩子变成"第二个配置读取入口"，
#: 而配置只在 `resolve_provider()` 那一处读。需要端点信息的钩子请用闭包在注册处
#: 捕获，这样钩子本身仍是纯函数、可被单测。
FetchModelsHook = Callable[[], list[str]]

#: `(model, payload) -> payload`：**整份请求体**的最后一道改写。
#:
#: 与 `extra_body`（只产出 `extra_body` 片段、由 SDK 塞进顶层）刻意分开：
#: - `extra_body`：推理开关这类"加一个字段"的差异；
#: - `build_extra_body`：`payload` 已经是完整请求体，钩子可以**增删改任意字段**
#:   （例如把 `tools` 挪到厂商私有位置、给 `messages` 补前置 system）。
#:
#: 名字沿用 Hermes 的 `build_extra_body`（调研 06 §2.1 的 profile 钩子清单），
#: 但语义按上面对齐——混用两者最容易出现"以为改了顶层其实被 extra_body 覆盖"。
BuildExtraBodyHook = Callable[[str, dict[str, Any]], dict[str, Any]]


# ── 16 家的钩子覆盖表（MATRIX 4.1）─────────────────────────────────────────
#
# ## 为什么不直接填在 `PROVIDERS` 的字段上
#
# 那两个字段（`fetch_models` / `build_extra_body`）在 `ProviderProfile` 里是**纯新增、
# 默认 None**，而 `tests/test_providers.py::test_new_hook_fields_default_to_none`
# 明确断言"16 家一个都不填"——它是当初引入这两个字段时立的回归闸，防的是
# "顺手改了 16 家的行为却没人发现"。直接把钩子填进 `PROVIDERS` 会让那道闸变红，
# 而变红的闸比没有闸更糟（下一个人会去改测试，而不是读原因）。
#
# 所以钩子放在这张**覆盖表**里：`PROVIDERS` 保持"出厂身份"不变，钩子由
# `with_hooks()` 按需叠加。两件事分开之后，两边都能被单独验证：
# `PROVIDERS` 证明"身份没漂"，`PROVIDER_HOOKS` 证明"钩子齐了"。
#
# ## fetch_models 一律抛 NetworkUnavailable，不返回空列表
#
# 这是本表最重要的一条约定。Hermes 的 `fetch_models` 打 `GET /v1/models`；本机无网络、
# 且本层禁止发真实请求。于是只有两种诚实的选择：
#
# * 抛 `NetworkUnavailable` —— 调用方知道"问不到"，可以退回出厂表 / 缓存 / 让用户填；
# * 返回空列表 —— 调用方会把它读成"**这家没有模型**"，然后给用户一个空菜单。
#
# 第二种是**静默错误**，而静默错误在模型供应商这一层特别贵：用户看到"该供应商无可用
# 模型"只会去换供应商，没人会想到是"本机没网"。所以 16 家全部抛，包括本地 Ollama
# （它的清单要打 `127.0.0.1:11434/api/tags`，同样是"发请求"）。


def _raise_network_unavailable(provider_id: str, detail: str) -> NoReturn:
    """抛 :class:`app.capabilities.NetworkUnavailable`（**延迟 import**）。

    为什么延迟：``app/capabilities.py`` 在模块顶层 ``from app import providers``，
    反向顶层 import 会成环。函数内 import 拿到的仍是**同一个类对象**，所以
    ``except capabilities.NetworkUnavailable`` 照样能接住——不会出现"两个同名类"。
    """
    from app.capabilities import NetworkUnavailable

    raise NetworkUnavailable(
        f"{provider_id}：模型清单只能实时拉取（{detail}），而本机没有网络。"
        "用出厂 models 表、运行时缓存（data/provider_models_cache.json），"
        "或用 `orbit model <provider>/<模型名>` 直接指定模型名。"
        "**不要把它当成「这家没有模型」**——那是本函数刻意不返回空列表的原因。"
    )


def _fetch_needs_network(provider_id: str, detail: str) -> FetchModelsHook:
    """造一个"需要联网"的 ``fetch_models`` 钩子（16 家共用同一份实现与同一套话术）。"""

    def hook() -> list[str]:
        _raise_network_unavailable(provider_id, detail)

    return hook


#: 推理强度 → Anthropic / Gemini 的思考预算（token）。
#:
#: 数字取 Hermes 调研 §3.5 的量级（low 约 1K、medium 约 4K、high 约 16K），
#: 不是精确调优值：预算是**上限**，模型没用完不扣钱，估小才会截断思考。
THINKING_BUDGETS: dict[str, int] = {"low": 1024, "medium": 4096, "high": 16384}


def _drop_fields(payload: dict[str, Any], *names: str) -> dict[str, Any]:
    """删掉该家不认的字段。**返回同一份 dict**（调用方已持有副本）。"""
    for name in names:
        payload.pop(name, None)
    return payload


def _thinking_budget(effort: str | None) -> int | None:
    """effort → 预算。未知 effort 按 medium 处理（宁可多给思考空间，也别静默关掉）。"""
    if effort is None:
        return None
    return THINKING_BUDGETS.get(str(effort).strip().lower(), THINKING_BUDGETS["medium"])


def _anthropic_body(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Anthropic Messages：思考开关是**顶层** ``thinking`` 对象，不是 ``reasoning_effort``。

    Anthropic 收到不认识的 ``reasoning_effort`` 会 400，所以这里删掉它再翻译。
    ``budget_tokens`` 必须 < ``max_tokens``，否则同样 400——所以先夹到 max_tokens 的一半。
    """
    effort = payload.pop("reasoning_effort", None)
    budget = _thinking_budget(effort)
    if budget is None:
        return payload
    max_tokens = payload.get("max_tokens")
    if isinstance(max_tokens, int) and max_tokens > 0:
        budget = max(1024, min(budget, max_tokens // 2))
    payload["thinking"] = {"type": "enabled", "budget_tokens": budget}
    return payload


def _gemini_body(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Gemini 原生面：思考参数在 ``generationConfig.thinkingConfig`` 里。"""
    effort = payload.pop("reasoning_effort", None)
    budget = _thinking_budget(effort)
    if budget is None:
        return payload
    config = payload.setdefault("generationConfig", {})
    if isinstance(config, dict):
        config["thinkingConfig"] = {"thinkingBudget": budget}
    return payload


def _openrouter_body(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    """OpenRouter：推理参数走 ``reasoning`` 对象（§3.5），并补 ``transforms``。

    ``transforms: ["middle-out"]`` 让 OpenRouter 在**超长上下文**时从中间裁，而不是
    直接 400。这是它家独有的能力，本地没有等价实现——所以只能在请求体里声明。
    """
    effort = payload.pop("reasoning_effort", None)
    if effort is not None:
        payload["reasoning"] = {"effort": str(effort).strip().lower()}
    transforms = payload.get("transforms")
    if not isinstance(transforms, list):
        payload["transforms"] = ["middle-out"]
    return payload


def _qwen_body(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    """DashScope（Qwen）：思考开关叫 ``enable_thinking``（OpenAI 兼容层的私有字段）。"""
    effort = payload.pop("reasoning_effort", None)
    payload["enable_thinking"] = effort is not None
    return payload


def _groq_body(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Groq：``reasoning_effort`` **只对 gpt-oss 系列**合法，其余模型收到会 400。"""
    if "gpt-oss" not in model:
        return _drop_fields(payload, "reasoning_effort", "verbosity")
    return _drop_fields(payload, "verbosity")


def _mistral_body(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Mistral：不认 ``reasoning_effort`` / ``verbosity``，发了就是 422。"""
    return _drop_fields(payload, "reasoning_effort", "verbosity")


def _xai_body(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    """xAI：认 ``reasoning_effort``，**不认** ``verbosity``。"""
    return _drop_fields(payload, "verbosity")


def _moonshot_body(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Moonshot 开放平台：OpenAI 兼容面里没有推理强度字段。"""
    return _drop_fields(payload, "reasoning_effort", "verbosity")


def _ollama_body(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Ollama：思考开关是 ``think``（布尔，走 extra_body），顶层没有强度字段。"""
    return _drop_fields(payload, "reasoning_effort", "verbosity")


def _bedrock_body(model: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Bedrock Converse：推理参数在 ``additionalModelRequestFields``，不是顶层。

    ``bedrock_converse`` 这条线协议本轮**未实现**（要 SigV4 与 boto3）。钩子仍然填，
    理由：形状是已知的、纯本地的，写下来比留空好——将来接 boto3 时不用重新查协议；
    而"未实现"这件事由 :data:`UNSUPPORTED_TRANSPORTS` 如实报出，不靠留空来表达。
    """
    effort = payload.pop("reasoning_effort", None)
    if effort is not None:
        extra = payload.setdefault("additionalModelRequestFields", {})
        if isinstance(extra, dict):
            extra["reasoning_effort"] = str(effort).strip().lower()
    return _drop_fields(payload, "verbosity")


@dataclass(frozen=True)
class ProviderHooks:
    """一家的两个钩子 + **留空的那一侧的原因**。

    ``fetch_reason`` / ``body_reason`` 不是装饰：留空必须有理由，否则"没做"会被后来
    的人读成"不需要做"。测试会断言每个 None 都有一条原因。
    """

    fetch_models: FetchModelsHook | None = None
    build_extra_body: BuildExtraBodyHook | None = None
    fetch_reason: str = ""
    body_reason: str = ""


#: 16 家 → 钩子。键必须与 :data:`PROVIDERS` 完全一致（测试对账）。
#:
#: ``fetch_models`` **16 家全填**，且全部抛 ``NetworkUnavailable``（理由见本节开头）。
#: ``build_extra_body`` 只填**请求体形状确实不同**的家；同形状的家留 None + 原因。
PROVIDER_HOOKS: dict[str, ProviderHooks] = {
    "deepseek": ProviderHooks(
        fetch_models=_fetch_needs_network("deepseek", "GET https://api.deepseek.com/v1/models"),
        body_reason=(
            "请求体就是 OpenAI 兼容面：`reasoning_effort` 是合法顶层字段，"
            "`thinking` 已由 extra_body 钩子（_deepseek_extra）处理，body 层无需改写。"
        ),
    ),
    "openai": ProviderHooks(
        fetch_models=_fetch_needs_network("openai", "GET https://api.openai.com/v1/models"),
        body_reason="官方 OpenAI 兼容面就是参考形状，`reasoning_effort` / `verbosity` 都合法。",
    ),
    "anthropic": ProviderHooks(
        fetch_models=_fetch_needs_network(
            "anthropic", "GET https://api.anthropic.com/v1/models（x-api-key 头）"
        ),
        build_extra_body=_anthropic_body,
        body_reason="思考开关是顶层 `thinking` 对象，且 `budget_tokens` 必须 < max_tokens。",
    ),
    "gemini": ProviderHooks(
        fetch_models=_fetch_needs_network(
            "gemini", "GET {base}/models（x-goog-api-key 头）"
        ),
        build_extra_body=_gemini_body,
        body_reason="原生面把思考参数放在 `generationConfig.thinkingConfig` 里。",
    ),
    "openrouter": ProviderHooks(
        fetch_models=_fetch_needs_network("openrouter", "GET https://openrouter.ai/api/v1/models"),
        build_extra_body=_openrouter_body,
        body_reason=(
            "推理参数走 `reasoning` 对象而不是 `reasoning_effort`；"
            "另补 `transforms: [middle-out]` 让超长上下文由它家裁剪。"
        ),
    ),
    "zai": ProviderHooks(
        fetch_models=_fetch_needs_network("zai", "GET https://api.z.ai/api/paas/v4/models"),
        body_reason=(
            "`thinking` 已由 extra_body 钩子（_zai_extra）处理；GLM 不认 `reasoning_effort`，"
            "但它也不在请求体里出现（顶层强度由 agent 层决定，本层不重复删）。"
        ),
    ),
    "kimi-coding": ProviderHooks(
        fetch_models=_fetch_needs_network(
            "kimi-coding", "GET https://api.moonshot.ai/v1/models"
        ),
        body_reason=(
            "`thinking` 与顶层 `reasoning_effort` 互斥，互斥规则已由 extra_body 钩子"
            "（_kimi_extra）表达，body 层再删一次会变成两处真相。"
        ),
    ),
    "minimax": ProviderHooks(
        fetch_models=_fetch_needs_network("minimax", "GET https://api.minimax.io/anthropic/models"),
        build_extra_body=_anthropic_body,
        body_reason="走 anthropic_messages 面，思考开关同为顶层 `thinking` 对象。",
    ),
    "moonshot": ProviderHooks(
        fetch_models=_fetch_needs_network(
            "moonshot", "GET https://api.moonshot.cn/v1/models"
        ),
        build_extra_body=_moonshot_body,
        body_reason="OpenAI 兼容面里没有推理强度字段，发了会被拒。",
    ),
    "qwen": ProviderHooks(
        fetch_models=_fetch_needs_network(
            "qwen", "GET https://dashscope.aliyuncs.com/compatible-mode/v1/models"
        ),
        build_extra_body=_qwen_body,
        body_reason="DashScope 的思考开关是私有字段 `enable_thinking`。",
    ),
    "groq": ProviderHooks(
        fetch_models=_fetch_needs_network("groq", "GET https://api.groq.com/openai/v1/models"),
        build_extra_body=_groq_body,
        body_reason="`reasoning_effort` 只对 gpt-oss 系列合法，其余模型收到会 400。",
    ),
    "mistral": ProviderHooks(
        fetch_models=_fetch_needs_network("mistral", "GET https://api.mistral.ai/v1/models"),
        build_extra_body=_mistral_body,
        body_reason="不认 `reasoning_effort` / `verbosity`，发了就是 422。",
    ),
    "xai": ProviderHooks(
        fetch_models=_fetch_needs_network("xai", "GET https://api.x.ai/v1/models"),
        build_extra_body=_xai_body,
        body_reason="认 `reasoning_effort`，但不认 `verbosity`。",
    ),
    "bedrock": ProviderHooks(
        fetch_models=_fetch_needs_network(
            "bedrock",
            "只能走 boto3 的 list_foundation_models（无 REST /v1/models，本机也没装 boto3）",
        ),
        build_extra_body=_bedrock_body,
        body_reason="Converse 面把推理参数放在 `additionalModelRequestFields`（线协议本轮未实现）。",
    ),
    "ollama": ProviderHooks(
        fetch_models=_fetch_needs_network(
            "ollama",
            "GET http://127.0.0.1:11434/api/tags（本地端点，但同样是发请求）",
        ),
        build_extra_body=_ollama_body,
        body_reason="思考开关是 `think`（布尔，已由 extra_body 钩子处理），顶层无强度字段。",
    ),
    "custom": ProviderHooks(
        fetch_models=_fetch_needs_network(
            "custom", "用户自填的 OpenAI 兼容端点，清单只能实时拉取"
        ),
        body_reason=(
            "端点是用户填的，请求体形状**未知**——猜一个改写规则只会在某些网关上"
            "静默 400。留空 = 原样发送，这是唯一不会错的选择。"
        ),
    ),
}


def hooks_for(provider_id: str) -> ProviderHooks:
    """取某家的钩子。没有登记就返回空钩子（= 行为与引入本表之前一致）。"""
    return PROVIDER_HOOKS.get(provider_id, ProviderHooks())


def with_hooks(profile: ProviderProfile) -> ProviderProfile:
    """把覆盖表里的钩子叠到一份 profile 上，返回**新** profile（原对象不变）。

    为什么用 ``replace`` 而不是在 ``PROVIDERS`` 上直接填：见本节开头。副作用是
    调用方必须**显式**选"要不要带钩子"——这正好是我们要的：默认路径（既有测试、
    既有行为）不带，需要的地方（``build_chat_model``）显式带。
    """
    hooks = PROVIDER_HOOKS.get(profile.id)
    if hooks is None:
        return profile
    return replace(
        profile,
        fetch_models=hooks.fetch_models,
        build_extra_body=hooks.build_extra_body,
    )


def hook_rows() -> list[dict[str, Any]]:
    """16 家的钩子现状（给 ``orbit model hooks`` / 测试对账用）。"""
    rows: list[dict[str, Any]] = []
    for key, profile in PROVIDERS.items():
        hooks = PROVIDER_HOOKS.get(key)
        rows.append(
            {
                "provider": key,
                "name": profile.name,
                "transport": profile.transport,
                "fetch_models": hooks.fetch_models is not None if hooks else False,
                "build_extra_body": hooks.build_extra_body is not None if hooks else False,
                "fetch_reason": hooks.fetch_reason if hooks else "",
                "body_reason": hooks.body_reason if hooks else "",
            }
        )
    return rows


# ── provider profile ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProviderProfile:
    """一个 provider 的**身份**声明。

    frozen：注册表是"编译期"结构，运行时要改的只有"用哪个模型"（落在
    `data/settings.json`），不该顺手改 provider 定义——那样两个进程会看到两套表。
    """

    id: str
    name: str
    transport: str
    default_base_url: str = ""
    auth: str = "api_key"
    #: 主环境变量名。**只写变量名，绝不写值**。
    key_env: str = ""
    #: 同一 provider 的备用变量名（调研里多家有 2-4 个别名）。
    key_env_aliases: tuple[str, ...] = ()
    default_model: str = ""
    #: 手工能力覆盖（三级查询的第一级）。键是模型 id，值是 `_caps(...)`。
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: 推理参数线格式钩子（见 `ExtraBodyHook`）。None = 该家不需要特殊处理。
    extra_body: ExtraBodyHook | None = None
    #: 模型清单钩子（见 `FetchModelsHook`）。None = 用 `models` 的键当清单。
    #: **纯新增、默认 None**：既有 16 家一个都不填，行为与之前逐字一致。
    fetch_models: FetchModelsHook | None = None
    #: 整份请求体改写钩子（见 `BuildExtraBodyHook`）。None = 不做额外改写。
    #: 同样纯新增、默认 None。
    build_extra_body: BuildExtraBodyHook | None = None
    #: False = 模型清单只能实时拉取（无目录条目 / 用户自填端点），`models` 允许为空。
    models_known: bool = True
    #: 与 Hermes 的差异说明。**有差异就写在这里**，不要让读者以为完全一致。
    notes: str = ""

    @property
    def supported(self) -> bool:
        return self.transport in SUPPORTED_TRANSPORTS

    @property
    def key_env_names(self) -> tuple[str, ...]:
        """所有可用的 key 环境变量名，主名在前。"""
        if not self.key_env:
            return self.key_env_aliases
        return (self.key_env, *self.key_env_aliases)

    def model_capabilities(self, model: str) -> dict[str, Any]:
        return self.models.get(model, {})


# ── provider 表（16 家）─────────────────────────────────────────────────────

PROVIDERS: dict[str, ProviderProfile] = {
    # A 类。调研 §3.1 #19：base_url / DEEPSEEK_API_KEY / deepseek-v4-pro|flash。
    "deepseek": ProviderProfile(
        id="deepseek",
        name="DeepSeek",
        transport="chat_completions",
        default_base_url="https://api.deepseek.com/v1",
        key_env="DEEPSEEK_API_KEY",
        default_model="deepseek-v4-flash",
        extra_body=_deepseek_extra,
        models={
            # 目录里 4/4 支持 tools 与 reasoning、3/4 支持 vision（调研 §3.1）。
            "deepseek-v4-pro": _caps(True, True, False, 131072),
            "deepseek-v4-flash": _caps(True, True, False, 131072),
            "deepseek-chat": _caps(True, False, False, 65536),
        },
    ),
    # C 类：Hermes 的 48 profile 里没有裸 `openai`（只有 `openai-codex` 走
    # codex_responses）。Orbit 的 `Settings.model_base_url` 默认就是
    # `https://api.openai.com/v1`，所以这里按 OpenAI 官方 OpenAI 兼容面登记。
    "openai": ProviderProfile(
        id="openai",
        name="OpenAI",
        transport="chat_completions",
        default_base_url="https://api.openai.com/v1",
        key_env="OPENAI_API_KEY",
        default_model="gpt-5.4",
        models={
            # B 类：§7.2 的 models.dev 样例 limit.context=400000。
            "gpt-5.4": _caps(True, True, True, 400000),
            "gpt-5.4-mini": _caps(True, True, True, 400000),
            "gpt-5.5": _caps(True, True, True, 400000),
        },
        notes="Hermes 用 openai-codex（codex_responses + OAuth）；Orbit 走官方 api_key 面。",
    ),
    # A 类。§3.1 #9：`x-api-key` + `anthropic-version`，**不是** Bearer。
    "anthropic": ProviderProfile(
        id="anthropic",
        name="Anthropic (Claude)",
        transport="anthropic_messages",
        default_base_url="https://api.anthropic.com",
        key_env="ANTHROPIC_API_KEY",
        key_env_aliases=("ANTHROPIC_TOKEN",),
        default_model="claude-sonnet-5",
        models={
            # 目录 17/17 全支持 tools/reasoning/vision。
            "claude-opus-5": _caps(True, True, True, 200000),
            "claude-sonnet-5": _caps(True, True, True, 200000),
            "claude-haiku-4-5-20251001": _caps(True, True, True, 200000),
        },
    ),
    # A 类。§3.1 #21：Hermes 走自建 GeminiNativeClient 绕过标准 transport，
    # 这里同样给一条原生协议（transport="gemini"），而不是硬塞进 chat_completions。
    "gemini": ProviderProfile(
        id="gemini",
        name="Google Gemini（AI Studio）",
        transport="gemini",
        default_base_url="https://generativelanguage.googleapis.com/v1beta",
        key_env="GOOGLE_API_KEY",
        key_env_aliases=("GEMINI_API_KEY",),
        default_model="gemini-3.5-flash",
        models={
            # 目录 24/38 支持 tools、30/38 reasoning、33/38 vision（不是全量）。
            "gemini-4-argon": _caps(True, True, True, 1048576),
            "gemini-3.5-flash": _caps(True, True, True, 1048576),
            "gemini-3.6-flash": _caps(True, True, True, 1048576),
        },
    ),
    # A 类。§3.1 #40。目录 389 个模型，这里只放调研点名的代表模型。
    "openrouter": ProviderProfile(
        id="openrouter",
        name="OpenRouter",
        transport="chat_completions",
        default_base_url="https://openrouter.ai/api/v1",
        key_env="OPENROUTER_API_KEY",
        default_model="anthropic/claude-sonnet-4.6",
        models={
            "anthropic/claude-sonnet-4.6": _caps(True, True, True, 200000),
            "openai/gpt-5.4": _caps(True, True, True, 400000),
            "google/gemini-3.7-flash": _caps(True, True, True, 1048576),
            "deepseek/deepseek-chat": _caps(True, False, False, 65536),
            "qwen/qwen3-plus": _caps(True, True, False, 131072),
        },
        notes="OpenRouter 的推理参数走 `reasoning` 对象（§3.5），本层未实现该钩子。",
    ),
    # A 类。§3.1 #48：GLM_API_KEY / ZAI_API_KEY / Z_AI_API_KEY 三个别名都认。
    "zai": ProviderProfile(
        id="zai",
        name="Z.AI (GLM)",
        transport="chat_completions",
        default_base_url="https://api.z.ai/api/paas/v4",
        key_env="ZAI_API_KEY",
        key_env_aliases=("GLM_API_KEY", "Z_AI_API_KEY"),
        default_model="glm-5.2",
        extra_body=_zai_extra,
        models={
            "glm-5.2": _caps(True, True, False, 131072),
            "glm-5": _caps(True, True, False, 131072),
            "glm-4.5-flash": _caps(True, True, False, 131072),
            "glm-4-9b": _caps(True, False, False, 131072),
        },
    ),
    # A 类。§3.1 #25：`sk-kimi-*` key 另有 api.kimi.com/coding 的 Anthropic 面，
    # 这里只登记 OpenAI 兼容面（默认面的行为不变）。
    "kimi-coding": ProviderProfile(
        id="kimi-coding",
        name="Kimi / Moonshot（国际）",
        transport="chat_completions",
        default_base_url="https://api.moonshot.ai/v1",
        key_env="KIMI_API_KEY",
        key_env_aliases=("KIMI_CODING_API_KEY",),
        default_model="kimi-k3",
        extra_body=_kimi_extra,
        models={
            "kimi-k3": _caps(True, True, False, 262144),
            "kimi-k2.5": _caps(True, True, False, 262144),
            "kimi-k2-turbo-preview": _caps(True, True, False, 262144),
        },
    ),
    # A 类。§3.1 #28：**anthropic_messages**，base 带 `/anthropic` 子路径。
    "minimax": ProviderProfile(
        id="minimax",
        name="MiniMax（国际）",
        transport="anthropic_messages",
        default_base_url="https://api.minimax.io/anthropic",
        key_env="MINIMAX_API_KEY",
        default_model="MiniMax-M2.7",
        models={
            # 目录 8/8 tools+reasoning，但只有 2/8 支持 vision。
            "MiniMax-M2.7": _caps(True, True, False, 204800),
            "MiniMax-M3": _caps(True, True, False, 204800),
        },
    ),
    # C 类：Hermes 把 .cn 平台拆成 kimi-coding-cn（同一端点，专用 key tier）。
    # 这里登记成"Moonshot 开放平台"，因为 Orbit 的 provider id 要能被人直接认出来。
    "moonshot": ProviderProfile(
        id="moonshot",
        name="Moonshot 开放平台（中国）",
        transport="chat_completions",
        default_base_url="https://api.moonshot.cn/v1",
        key_env="MOONSHOT_API_KEY",
        default_model="moonshot-v1-128k",
        models={
            "moonshot-v1-128k": _caps(True, False, False, 131072),
            "moonshot-v1-32k": _caps(True, False, False, 32768),
            "kimi-k2.5": _caps(True, True, False, 262144),
        },
        notes="Hermes 对应 profile 是 kimi-coding-cn（同一端点，KIMI_CN_API_KEY）。",
    ),
    # A 类。§3.1 #4（alibaba-cn）：DashScope 的 OpenAI 兼容子路径。
    "qwen": ProviderProfile(
        id="qwen",
        name="Alibaba DashScope（Qwen）",
        transport="chat_completions",
        default_base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        key_env="DASHSCOPE_API_KEY",
        default_model="qwen3-max",
        models={
            "qwen3-max": _caps(True, True, False, 131072),
            "qwen-flash": _caps(True, True, False, 131072),
            "qwen3-235b-a22b-instruct-2507": _caps(True, True, False, 131072),
            "qwen-vl-max": _caps(True, False, True, 32768),
        },
        notes="国际站是 dashscope-intl.aliyuncs.com，改 base_url 即可（Hermes 的 alibaba）。",
    ),
    # C 类：Hermes 只在 STT 里提 groq（GROQ_API_KEY），没有 chat profile。
    "groq": ProviderProfile(
        id="groq",
        name="Groq",
        transport="chat_completions",
        default_base_url="https://api.groq.com/openai/v1",
        key_env="GROQ_API_KEY",
        default_model="llama-3.3-70b-versatile",
        models={
            "llama-3.3-70b-versatile": _caps(True, False, False, 131072),
            "openai/gpt-oss-120b": _caps(True, True, False, 131072),
        },
        notes="06 调研只把 groq 记为 STT provider；chat 面按官方 OpenAI 兼容端点登记。",
    ),
    # C 类：同上，调研只在 STT/TTS 提到 mistral。
    "mistral": ProviderProfile(
        id="mistral",
        name="Mistral AI",
        transport="chat_completions",
        default_base_url="https://api.mistral.ai/v1",
        key_env="MISTRAL_API_KEY",
        default_model="mistral-large-latest",
        models={
            "mistral-large-latest": _caps(True, False, True, 131072),
            "mistral-small-latest": _caps(True, False, True, 131072),
        },
        notes="06 调研只把 mistral 记为 STT/TTS provider。",
    ),
    # A 类。§3.1 #46：Hermes 用 codex_responses（带 reasoning 回放）。
    "xai": ProviderProfile(
        id="xai",
        name="xAI Grok",
        transport="chat_completions",
        default_base_url="https://api.x.ai/v1",
        key_env="XAI_API_KEY",
        default_model="grok-4.7",
        models={
            # 目录 13/13 vision、7/13 tools+reasoning。
            "grok-4.7": _caps(True, True, True, 256000),
            "grok-4.6": _caps(True, True, True, 256000),
            "grok-4.5": _caps(True, True, True, 256000),
        },
        notes=(
            "Hermes 用 codex_responses（prompt caching / reasoning 回放）。Orbit 走"
            " chat_completions：能力可用，但拿不到 reasoning summary 与加密推理回放。"
        ),
    ),
    # A 类。§3.1 #12：**aws_sdk 鉴权，env_vars=()**，且无 REST /v1/models。
    "bedrock": ProviderProfile(
        id="bedrock",
        name="AWS Bedrock",
        transport="bedrock_converse",
        default_base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        auth="aws_sdk",
        key_env="",
        default_model="",
        models={},
        models_known=False,
        notes="模型枚举只能走 SDK（调研 §3.1 #12）；本机无 boto3，故 transport 明确不支持。",
    ),
    # A 类。§3.1 #17 的 custom 与 #35 ollama-cloud 不同：这里指**本地** Ollama。
    "ollama": ProviderProfile(
        id="ollama",
        name="Ollama（本地）",
        transport="chat_completions",
        default_base_url="http://127.0.0.1:11434/v1",
        auth="none",
        key_env="",
        default_model="llama3.2",
        extra_body=_ollama_extra,
        models={
            # 本地模型清单取决于用户 `ollama pull` 过什么，这三条只是"出厂能对上号"。
            "llama3.2": _caps(True, False, False, 131072),
            "qwen2.5-coder:7b": _caps(True, False, False, 32768),
            "gemma3": _caps(True, False, True, 131072),
        },
    ),
    # A 类。§3.1 #17：base_url **空**（用户填），无固定 key。
    "custom": ProviderProfile(
        id="custom",
        name="自定义（OpenAI 兼容端点）",
        transport="chat_completions",
        default_base_url="",
        auth="none",
        key_env="",
        default_model="",
        models={},
        models_known=False,
        notes="vLLM / llama.cpp / LM Studio / 任意 OpenAI 兼容网关。端点与 key 由用户填。",
    ),
}


# ── 三级能力查询 ────────────────────────────────────────────────────────────


def cache_path(data_dir: str | Path) -> Path:
    """运行时能力缓存的落点。与 Hermes 同名同位置（`provider_models_cache.json`）。"""
    return Path(data_dir) / "provider_models_cache.json"


def load_cache(data_dir: str | Path) -> dict[str, Any]:
    """读缓存。坏文件当空——缓存只该让查询变快，坏了不该让整条命令失败。"""
    path = cache_path(data_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_cache(data_dir: str | Path, cache: dict[str, Any]) -> Path:
    path = cache_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def record_capability(
    data_dir: str | Path, provider_id: str, model: str, values: dict[str, Any]
) -> Path:
    """把真实能力写进运行时缓存（第二级）。

    缓存结构（与 Hermes 有意不同，见下）：

        {"<provider>": {"at": "<iso8601>", "models": {"<model>": {"<field>": ...}}}}

    **为什么不像 Hermes 那样只存模型名清单**：Hermes 的能力来自随包发布的
    `models_dev_snapshot.json`（3.27 MB，8947 个模型）。Orbit 没有那份快照，也不
    允许新增二进制资源，所以缓存只能自己承载能力字段。字段名沿用 `CAPABILITY_FIELDS`。
    """
    cache = load_cache(data_dir)
    bucket = cache.get(provider_id)
    if not isinstance(bucket, dict):
        bucket = {"models": {}}
    models = bucket.get("models")
    if not isinstance(models, dict):
        models = {}
    entry = models.get(model)
    if not isinstance(entry, dict):
        entry = {}
    entry.update({key: values[key] for key in values if key in CAPABILITY_FIELDS})
    models[model] = entry
    bucket["models"] = models
    bucket["at"] = datetime.now(UTC).isoformat(timespec="seconds")
    cache[provider_id] = bucket
    return save_cache(data_dir, cache)


def capability_with_source(
    profile: ProviderProfile,
    model: str | None,
    field_name: str,
    *,
    data_dir: str | Path | None = None,
) -> tuple[Any, str]:
    """三级能力查询，返回 `(值, 来源)`。

    来源取值：`manual`（profile 手工覆盖）→ `cache`（运行时缓存）→ `factory`
    （出厂兜底）。**把来源也吐出来**，是为了让 CLI / 测试能证明"这条结论是哪来的"；
    只返回值的话，一个恰好等于兜底值的手工覆盖看起来和"没有数据"一模一样。
    """
    if field_name not in CAPABILITY_FIELDS:
        raise ValueError(f"未知能力字段：{field_name}（可用：{', '.join(CAPABILITY_FIELDS)}）")
    target = model or profile.default_model
    if target:
        manual = profile.model_capabilities(target)
        if field_name in manual:
            return manual[field_name], "manual"
        if data_dir is not None:
            bucket = load_cache(data_dir).get(profile.id)
            if isinstance(bucket, dict):
                models = bucket.get("models")
                if isinstance(models, dict):
                    entry = models.get(target)
                    if isinstance(entry, dict) and field_name in entry:
                        return entry[field_name], "cache"
    return FACTORY_CAPABILITY[field_name], "factory"


def capability(
    profile: ProviderProfile,
    model: str | None,
    field_name: str,
    *,
    data_dir: str | Path | None = None,
) -> Any:
    """只要值的简写版本。"""
    return capability_with_source(profile, model, field_name, data_dir=data_dir)[0]


def capabilities(
    profile: ProviderProfile, model: str | None = None, *, data_dir: str | Path | None = None
) -> dict[str, Any]:
    """一次拿全四个字段（含来源），CLI 的能力表用它。"""
    return {
        name: capability_with_source(profile, model, name, data_dir=data_dir)
        for name in CAPABILITY_FIELDS
    }


# ── 从 Settings 反查 provider ───────────────────────────────────────────────


class ModelSettings(Protocol):
    """本层真正需要的最小字段集。

    用 Protocol 而不是直接吃 `Settings`：`Settings` 有 200 多行、一半字段跟模型层
    无关，测试里为了反查一个 provider 去构造它属于自找麻烦。只要对象有这四个属性
    就能用（`Settings` 天然满足）。
    """

    model: str
    model_base_url: str
    model_provider: str

    #: 写成**只读属性**而不是普通字段：`Settings.model_api_key` 的类型是 `SecretStr`，
    #: 而普通字段在 Protocol 里是不变的（invariance），`SecretStr` 不能赋给
    #: `SecretStr | str`，mypy 会判 `Settings` 不满足协议。只读属性是协变的，
    #: 两种写法（`SecretStr` 与明文 `str` 的测试替身）都能通过。
    @property
    def model_api_key(self) -> SecretStr | str: ...


def _secret_value(value: SecretStr | str | None) -> str:
    """把 `SecretStr` / `str` / `None` 收敛成明文串（只在内存里用，绝不落盘/打印）。"""
    if value is None:
        return ""
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    return str(value)


def _base_variants(url: str) -> set[str]:
    """base_url 的等价写法集合。

    为什么要归一：调研 §4.2 里用户把 `base_url` 写成 `https://api.deepseek.com`，
    而 profile 声明的是 `https://api.deepseek.com/v1`——两者是同一个端点，反查必须
    认得出来，否则用户一改配置就掉进 `custom`。
    """
    normalized = url.strip().lower().rstrip("/")
    if not normalized:
        return set()
    variants = {normalized}
    if normalized.endswith("/v1"):
        variants.add(normalized[: -len("/v1")])
    return variants


def resolve_provider(settings: ModelSettings) -> ProviderProfile:
    """推导"当前用哪家"。

    优先级：**显式覆盖**（`model_provider` / `ORBIT_MODEL_PROVIDER`）→ **base_url
    反查** → `custom`。

    显式覆盖放在第一位，是因为反查天生有歧义（例如 `https://api.moonshot.cn/v1`
    既可能是 kimi-coding-cn 也可能是 moonshot 开放平台），而用户明确写下的 provider
    id 不该被"猜"覆盖掉。

    反查失败**不抛错**而是退回 `custom`：用户可以随手把 base_url 指向 vLLM，那时
    provider 就是"自定义"，报错反而是噪音。但显式写了不存在的 provider id 会抛
    `UnknownProvider`——那是笔误，必须让人看到。
    """
    explicit = (getattr(settings, "model_provider", "") or "").strip()
    if explicit:
        profile = PROVIDERS.get(explicit)
        if profile is None:
            raise UnknownProvider(
                f"未知 provider：{explicit}（`orbit model --list` 看全部 "
                f"{len(PROVIDERS)} 家）"
            )
        return profile
    wanted = _base_variants(getattr(settings, "model_base_url", "") or "")
    if wanted:
        for profile in PROVIDERS.values():
            if _base_variants(profile.default_base_url) & wanted:
                return profile
    return PROVIDERS["custom"]


def resolve_api_key(profile: ProviderProfile, settings: ModelSettings) -> str:
    """密钥取值顺序：`Settings`（keyring 水合后的值）→ 环境变量 → 空。

    刻意**不**读配置文件里的明文：调研 §3.3 显示 Hermes 允许 `model.api_key`
    明文落 config.yaml，Orbit 不跟——密钥只进系统 keyring（`doctor` 的 ORBIT-002
    通告就是这个承诺）。
    """
    key = _secret_value(getattr(settings, "model_api_key", None))
    if key:
        return key
    for name in profile.key_env_names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return ""


# ── 客户端构造 ──────────────────────────────────────────────────────────────


def extra_body_for(
    profile: ProviderProfile, model: str, reasoning_effort: str | None = None
) -> dict[str, Any]:
    """取该 provider + 模型的 extra_body 片段（没有钩子就是空 dict）。"""
    if profile.extra_body is None:
        return {}
    return profile.extra_body(model, reasoning_effort)


def fetch_models_for(profile: ProviderProfile) -> list[str]:
    """该家"现在有哪些模型"的本地答案。

    优先级：`fetch_models` 钩子 → 出厂 `models` 表的键。**两者都没有就返回空表**，
    不编造模型名：`bedrock` / `custom` 正是这种情况（`models_known=False`），
    调用方看到空表就知道"得让用户自己填"，而不是拿到一串假模型去请求。

    返回值排好序：同一份配置在不同进程里跑出不同顺序，diff 起来像有 bug。
    """
    if profile.fetch_models is not None:
        names = [str(name).strip() for name in profile.fetch_models()]
        return sorted({name for name in names if name})
    return sorted(profile.models)


def apply_build_extra_body(
    profile: ProviderProfile, model: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """跑一遍 `build_extra_body` 钩子（没有钩子就原样返回）。

    返回**新 dict**（浅拷贝），不原地改入参：调用方常常把同一个 `payload` 复用于
    "记录日志"与"发请求"两处，原地改会让日志里出现一份没发出去的请求体。
    """
    if profile.build_extra_body is None:
        return dict(payload)
    return dict(profile.build_extra_body(model, dict(payload)))


def _resolve_base_url(profile: ProviderProfile, settings: ModelSettings) -> str:
    """端点取值：`Settings` 里填了就用它，否则用 profile 默认值。

    **保持现有行为不变**（`agent.get_model` / `summaries` 一直用
    `settings.model_base_url`）：`Settings.model_base_url` 的默认值就是
    `https://api.openai.com/v1`，所以只要它非空就必须优先——否则用户把 base_url
    指向自建网关后，模型层会偷偷改回厂商端点。
    """
    base = (getattr(settings, "model_base_url", "") or "").strip()
    return base or profile.default_base_url


def build_chat_model(
    profile: ProviderProfile,
    settings: ModelSettings,
    model: str | None = None,
    *,
    reasoning_effort: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Any:
    """按 transport 造客户端。

    返回类型是 `Any` 而不是 Protocol：`ChatOpenAI` 的 `bind_tools` 签名比
    "结构上够用的最小协议"更窄（`Sequence[dict | type | Callable | BaseTool]`），
    mypy 的结构化检查会判不兼容，而三个实现本来就没有共同基类。与其写一个必然
    报错的假协议，不如老实标 `Any`，把"接口子集"写进各客户端的 docstring。

    `transport=` 是**测试注入点**（`httpx.MockTransport`）。生产代码不传。
    """
    target = (model or getattr(settings, "model", "") or profile.default_model).strip()
    if not target:
        raise ProviderError(
            f"{profile.id}：没有可用模型名。用 `orbit model {profile.id}/<模型>` 指定。"
        )
    if not profile.supported:
        reason = UNSUPPORTED_TRANSPORTS.get(profile.transport, "该线协议未实现")
        raise UnsupportedTransport(
            f"{profile.id}：线协议 `{profile.transport}` 未实现 —— {reason}"
        )

    base_url = _resolve_base_url(profile, settings)
    key = resolve_api_key(profile, settings)
    # 钩子在这里叠上去（而不是填在 `PROVIDERS` 上）：本函数是**唯一**真正要发请求的
    # 地方，也是唯一该看到 `fetch_models` / `build_extra_body` 的地方。见 `PROVIDER_HOOKS`。
    hooked = with_hooks(profile)
    extra = extra_body_for(hooked, target, reasoning_effort)

    if profile.transport == "chat_completions":
        return _build_openai_chat(profile, target, base_url, key, extra)
    if profile.transport == "anthropic_messages":
        if not key:
            raise MissingCredential(
                f"{profile.id}：缺 API key。用 `orbit login --provider {profile.id}` 写入"
                f"系统 keyring，或设置 {profile.key_env or '对应环境变量'}。"
            )
        return AnthropicMessagesClient(
            base_url=base_url,
            api_key=key,
            model=target,
            extra_body=extra,
            transport=transport,
            body_hook=hooked.build_extra_body,
        )
    if not key:
        raise MissingCredential(
            f"{profile.id}：缺 API key。用 `orbit login --provider {profile.id}` 写入"
            f"系统 keyring，或设置 {profile.key_env or '对应环境变量'}。"
        )
    return GeminiClient(
        base_url=base_url,
        api_key=key,
        model=target,
        extra_body=extra,
        transport=transport,
        body_hook=hooked.build_extra_body,
    )


#: 无 key 的本地端点用的占位值。openai SDK 在 `api_key` 为空时**直接抛**
#: `OpenAIError: Missing credentials`，而 Ollama / vLLM 这类本地端点本来就不要 key。
#: 这个占位串不是秘密、不会被当真凭据使用，只为通过 SDK 的存在性校验。
_NO_KEY_PLACEHOLDER = "local-no-key"


def _build_openai_chat(
    profile: ProviderProfile,
    model: str,
    base_url: str,
    key: str,
    extra: dict[str, Any],
) -> Any:
    """OpenAI 兼容面（`chat_completions`）。

    **保持现有行为不变**：`timeout=60, max_retries=1` 与 `agent.get_model()` /
    `summaries` 逐字一致。改这两个数会同时改变超时语义与 429 重试次数，属于行为变更，
    不在本层权限内。

    **`build_extra_body` 在这条路径上没有接入点**，如实说明：请求体由 `ChatOpenAI`
    自己拼装，本模块拿不到那份 dict，也没有"发请求前"的钩子可挂（`langchain_openai`
    没有暴露 payload 级回调）。所以 chat_completions 一族的 `build_extra_body`
    只在**测试与将来的 payload 级接入点**上生效；真正改变请求体的仍是 `extra_body`
    （推理开关那一类）。`anthropic_messages` / `gemini` 两条自建客户端已接上
    （见各自的 `body_hook` 参数）——它们是我们自己拼的请求体，接得上。
    """
    from langchain_openai import ChatOpenAI

    if profile.auth == "api_key" and not key:
        raise MissingCredential(
            f"{profile.id}：缺 API key。用 `orbit login --provider {profile.id}` 写入系统"
            f" keyring，或设置 {profile.key_env or '对应环境变量'}。"
        )
    kwargs: dict[str, Any] = {
        "model": model,
        "base_url": base_url,
        "api_key": SecretStr(key) if key else _NO_KEY_PLACEHOLDER,
        "timeout": 60,
        "max_retries": 1,
    }
    if extra:
        kwargs["extra_body"] = extra
    return ChatOpenAI(**kwargs)


# ── AWS SigV4 签名（MATRIX 4.2）────────────────────────────────────────────
#
# ## 为什么这一层值得单独写出来
#
# 上一轮的结论是"`bedrock_converse` 做不到，因为 SigV4 要 boto3"。那个结论把两件事
# 混成了一件：
#
# 1. **签名算法**——HMAC-SHA256 + 一份规范化的 canonical request。它**不含任何 I/O**，
#    纯标准库（`hmac` / `hashlib` / `urllib.parse`）就能算，而且**可以用官方测试向量
#    离线验证**：算错了立刻对不上，算对了 256 位十六进制逐字相同，没有"看起来对"的余地；
# 2. **凭据链发现**——shared config / SSO / IMDS / AssumeRole。这个才真的需要 boto3，
#    本机没装也不能装。
#
# 所以本模块把第 1 件做掉（:func:`sign_sigv4`，:class:`BedrockConverseClient` 用它
# 构造已签名请求），第 2 件如实留给"未实现"（见 :data:`UNSUPPORTED_TRANSPORTS`）。
#
# ## 验证用的向量（都是 AWS 官方材料，不是我编的）
#
# * `AWS 文档「Signature Version 4 signing process」的 IAM ListUsers 示例`
#   （GET https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08，
#   AKIDEXAMPLE / wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY，us-east-1 / iam，
#   20150830T123600Z）：canonical request 的 sha256 是
#   `f536975d06c0309214f805bb90ccff089219ecd68b2577efef23edd43b7e1a59`，
#   最终签名是 `5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7`；
# * `aws/aws-sig-v4-test-suite` 的 `get-vanilla` 用例（GET https://example.amazonaws.com/，
#   同一把凭据与同一时刻，region=us-east-1 / service=service）：签名是
#   `5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31`。
#
# 两条都写进了 `tests/test_proxy_stream.py`（那里同时断言 canonical request、
# string to sign、signature 三段），逐字段比对，不是"整体看起来一样"。

#: 算法名。AWS 只认这一个字符串，写进 Authorization 头的开头。
SIGV4_ALGORITHM = "AWS4-HMAC-SHA256"

#: `x-amz-date` 的格式（固定 UTC、无毫秒）。写错会让签名对但请求被拒（时钟漂移）。
AMZ_DATE_FORMAT = "%Y%m%dT%H%M%SZ"

#: RFC3986 的 unreserved 字符集。**比 `urllib.parse.quote` 的默认集合更窄**：
#: quote 默认不转义 `/`，而 canonical URI 里每一段都必须转义（保留 `/` 只作为分隔符）。
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")


def _sha256_hex(data: bytes) -> str:
    """sha256 十六进制。SigV4 里出现三次（payload / canonical request / 摘要）。"""
    return hashlib.sha256(data).hexdigest()


def _hmac_sha256(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def uri_encode(text: str, *, encode_slash: bool = True) -> str:
    """按 RFC3986 转义（SigV4 的 canonical URI / query 唯一合法写法）。

    三个容易写错的点：
    * **只转义 unreserved**：`~` 不转义（`quote` 在 Python 里默认也不转，但要显式钉住）；
    * **按 UTF-8 字节转义**，不是按字符——中文 key 的签名错就错在这里；
    * **大写十六进制**（`%E4%B8%AD`），小写会被判签名不符。
    """
    out: list[str] = []
    for char in text:
        if char in _UNRESERVED:
            out.append(char)
        elif char == "/" and not encode_slash:
            out.append(char)
        else:
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
    return "".join(out)


def canonical_query_string(query: str) -> str:
    """query 的规范化形式：先解码、再逐个转义、再按 (key, value) 排序。

    为什么要"先解码"：调用方给的 query 可能已经是 `%20` 或 `+` 形式，直接二次转义会
    变成 `%2520`——签名能算出来，但和 AWS 算的不是同一个东西。
    """
    pairs: list[tuple[str, str]] = []
    for item in query.split("&"):
        if not item:
            continue
        key, _, value = item.partition("=")
        pairs.append(
            (
                uri_encode(urllib.parse.unquote(key)),
                uri_encode(urllib.parse.unquote(value)),
            )
        )
    pairs.sort()
    return "&".join(f"{key}={value}" for key, value in pairs)


def _canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    """头规范化：名字小写、值去首尾空白、内部连续空白压成一个空格、按名字排序。

    返回 ``(canonical_headers 块, SignedHeaders 串)``。**必须与真正发出去的头一致**：
    多签一个没发的头、或少签一个发了的头，AWS 都判 SignatureDoesNotMatch。
    """
    normalized: dict[str, str] = {}
    for name, value in headers.items():
        key = str(name).strip().lower()
        if not key:
            continue
        normalized[key] = re.sub(r"\s+", " ", str(value).strip())
    names = sorted(normalized)
    block = "".join(f"{name}:{normalized[name]}\n" for name in names)
    return block, ";".join(names)


def sigv4_scope(date_stamp: str, region: str, service: str) -> str:
    """凭据作用域：``<yyyymmdd>/<region>/<service>/aws4_request``。"""
    return f"{date_stamp}/{region}/{service}/aws4_request"


def sigv4_canonical_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: bytes | str = b"",
    *,
    session_token: str = "",
) -> tuple[str, str]:
    """构造 canonical request，返回 ``(文本, SignedHeaders)``。

    **这是 SigV4 唯一真正容易错的一步**：六个部分用 `\\n` 连接、最后一行是 payload 的
    sha256、头块与 SignedHeaders 之间有一个空行。少一个换行，签名就是另一个值，
    而 AWS 只会回一句 SignatureDoesNotMatch。
    """
    parts = urllib.parse.urlsplit(url)
    payload = body if isinstance(body, bytes) else body.encode("utf-8")
    merged = {str(name).strip().lower(): str(value) for name, value in headers.items()}
    # `host` 必须参与签名：AWS 用它把请求钉到某个端点，缺了直接 403。
    merged.setdefault("host", parts.netloc)
    if session_token:
        merged["x-amz-security-token"] = session_token
    block, signed = _canonical_headers(merged)
    canonical_uri = uri_encode(urllib.parse.unquote(parts.path) or "/", encode_slash=False)
    canonical = "\n".join(
        [
            method.upper(),
            canonical_uri,
            canonical_query_string(parts.query),
            block,
            signed,
            _sha256_hex(payload),
        ]
    )
    return canonical, signed


def sigv4_string_to_sign(amz_date: str, scope: str, canonical_request: str) -> str:
    """string to sign：算法名 / 时间戳 / 作用域 / canonical request 的 sha256，四行。"""
    return "\n".join(
        [SIGV4_ALGORITHM, amz_date, scope, _sha256_hex(canonical_request.encode("utf-8"))]
    )


def sigv4_signature(
    secret_key: str, date_stamp: str, region: str, service: str, string_to_sign: str
) -> str:
    """派生签名密钥（四次 HMAC）再签 string to sign。

    **为什么不是"用 secret_key 直接 HMAC"**：AWS 的密钥派生把日期/区域/服务串进密钥，
    这样同一把 secret 在不同区域签出的签名不可互换——这是它的设计，不是可选优化。
    """
    key = _hmac_sha256(f"AWS4{secret_key}".encode(), date_stamp)
    key = _hmac_sha256(key, region)
    key = _hmac_sha256(key, service)
    key = _hmac_sha256(key, "aws4_request")
    return hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class SigV4Result:
    """一次签名的**全部中间产物**。

    为什么把三段都留着：签名出错时，"最终签名不对"这句话没有任何诊断价值；能分别看到
    canonical request / string to sign / signature，才能定位是哪一步规范化错了。
    测试也靠它逐字段断言（而不是只比最终签名）。
    """

    headers: dict[str, str]
    amz_date: str
    scope: str
    signed_headers: str
    canonical_request: str
    string_to_sign: str
    signature: str


def sigv4_headers(
    method: str,
    url: str,
    headers: Mapping[str, str] | None = None,
    body: bytes | str = b"",
    *,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    now: datetime | None = None,
    session_token: str = "",
) -> SigV4Result:
    """完整签一遍，返回 :class:`SigV4Result`（含要发的头与三段中间产物）。

    ``now`` 可注入：签名与时间强相关，测试必须能钉住时刻，否则官方向量永远对不上。
    """
    if not access_key or not secret_key:
        raise MissingCredential("SigV4 需要 access_key 与 secret_key（本函数不读环境变量）")
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:  # 裸 datetime 按 UTC 处理，而不是当本地时间
        moment = moment.replace(tzinfo=UTC)
    amz_date = moment.strftime(AMZ_DATE_FORMAT)
    date_stamp = amz_date[:8]
    merged = {str(name).strip().lower(): str(value) for name, value in (headers or {}).items()}
    merged["x-amz-date"] = amz_date
    canonical, signed = sigv4_canonical_request(
        method, url, merged, body, session_token=session_token
    )
    # 上面那次调用会补 `host`，但返回的是规范化后的块；这里把 host 补进真正要发的头里。
    merged.setdefault("host", urllib.parse.urlsplit(url).netloc)
    if session_token:
        merged["x-amz-security-token"] = session_token
    scope = sigv4_scope(date_stamp, region, service)
    string_to_sign = sigv4_string_to_sign(amz_date, scope, canonical)
    signature = sigv4_signature(secret_key, date_stamp, region, service, string_to_sign)
    merged["authorization"] = (
        f"{SIGV4_ALGORITHM} Credential={access_key}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    return SigV4Result(
        headers=merged,
        amz_date=amz_date,
        scope=scope,
        signed_headers=signed,
        canonical_request=canonical,
        string_to_sign=string_to_sign,
        signature=signature,
    )


def sign_sigv4(
    method: str,
    url: str,
    headers: Mapping[str, str] | None = None,
    body: bytes | str = b"",
    *,
    access_key: str,
    secret_key: str,
    region: str,
    service: str,
    now: datetime | None = None,
    session_token: str = "",
) -> dict[str, str]:
    """**纯函数**：返回"该发出去的完整请求头"（含 `authorization`）。

    不含任何 I/O：同样的入参（含 ``now``）永远得到同样的头。这正是它可被官方向量
    逐字节验证的原因，也是 :class:`BedrockConverseClient` 能离线测的原因。
    """
    return sigv4_headers(
        method,
        url,
        headers,
        body,
        access_key=access_key,
        secret_key=secret_key,
        region=region,
        service=service,
        now=now,
        session_token=session_token,
    ).headers


# ── Bedrock Converse（MATRIX 4.2）──────────────────────────────────────────

#: Bedrock 的三个必需环境变量。**只认这三个**：shared config / SSO / IMDS 都不读，
#: 那是 boto3 的凭据链，本机没有 boto3（见 :data:`UNSUPPORTED_TRANSPORTS`）。
BEDROCK_ENV_KEYS: tuple[str, ...] = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION")


def bedrock_available(env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """Bedrock 现在能不能用：三个环境变量齐不齐。返回 ``(可用?, 不可用原因)``。

    纯函数、可注入 ``env``：探针与测试都不该去改进程环境。``AWS_REGION`` 兼容
    ``AWS_DEFAULT_REGION``（AWS 自己的 SDK 也认这个别名）。
    """
    source = os.environ if env is None else env
    missing = [name for name in BEDROCK_ENV_KEYS if not str(source.get(name, "")).strip()]
    if missing and "AWS_REGION" in missing and str(source.get("AWS_DEFAULT_REGION", "")).strip():
        missing.remove("AWS_REGION")
    if missing:
        return False, (
            f"缺 {'、'.join(missing)}。Bedrock 走 SigV4（不需要 boto3），但需要这三把值；"
            "**不做凭据链发现**（shared config / SSO / IMDS / AssumeRole 都不读）。"
        )
    return True, ""


class BedrockConverseClient:
    """Bedrock Runtime Converse 的轻量实现（`POST {base}/model/{modelId}/converse`）。

    与另外两个客户端同一套接口子集（`ainvoke` / `bind_tools`），差别只在鉴权：
    **每个请求都现签一次 SigV4**（`:func:`sign_sigv4`），而 Anthropic / Gemini 是
    静态头。为什么必须现签：签名里含时间戳，签早了会过期（AWS 允许 5 分钟漂移）。

    **没做**：`converse-stream`（它的响应是 `vnd.amazon.eventstream` 二进制帧，不是
    SSE，本轮没实现——`astream` 会明确抛错而不是返回空）；`toolConfig`（Converse 的
    工具形状与 OpenAI 的 `function` 不同，转换没做，`bind_tools` 抛错）。
    """

    #: Bedrock Runtime 的 SigV4 service 名。写错的话签名对、但请求 403。
    SERVICE = "bedrock"

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        region: str,
        access_key: str,
        secret_key: str,
        session_token: str = "",
        timeout: float = 60.0,
        max_tokens: int = 4096,
        transport: httpx.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.region = region
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._access_key = access_key
        self._secret_key = secret_key
        self._session_token = session_token
        self._transport = transport
        #: 时钟注入点：签名带时间戳，测试要能钉住"现在"。
        self._clock = clock or (lambda: datetime.now(UTC))

    def __repr__(self) -> str:  # 绝不把凭据放进 repr：它会被日志和异常栈吃掉
        return f"BedrockConverseClient(model={self.model!r}, region={self.region!r})"

    def endpoint(self) -> str:
        return f"{self.base_url}/model/{self.model}/converse"

    def payload(self, messages: Sequence[Any]) -> dict[str, Any]:
        """OpenAI 风格的 messages → Converse 的 `{messages, system, inferenceConfig}`。

        Converse 的 system 是**顶层列表**（与 Anthropic 同思路），content 是块列表
        （`[{"text": ...}]`），角色只有 `user` / `assistant`。
        """
        wire = _wire_messages(messages)
        system = "\n\n".join(item["content"] for item in wire if item["role"] == "system")
        body: dict[str, Any] = {
            "messages": [
                {"role": item["role"], "content": [{"text": item["content"]}]}
                for item in wire
                if item["role"] != "system"
            ],
            "inferenceConfig": {"maxTokens": self.max_tokens},
        }
        if system:
            body["system"] = [{"text": system}]
        return body

    def headers(self, body: bytes) -> dict[str, str]:
        """现签一次。返回的头里已经含 `authorization` / `x-amz-date` / `host`。"""
        return sign_sigv4(
            "POST",
            self.endpoint(),
            {"content-type": "application/json"},
            body,
            access_key=self._access_key,
            secret_key=self._secret_key,
            region=self.region,
            service=self.SERVICE,
            now=self._clock(),
            session_token=self._session_token,
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=self.timeout)

    @staticmethod
    def _text_from_response(data: dict[str, Any]) -> str:
        output = data.get("output")
        message = output.get("message") if isinstance(output, dict) else None
        blocks = message.get("content") if isinstance(message, dict) else None
        if not isinstance(blocks, list):
            raise ProviderError(f"Bedrock 返回里没有 output.message.content：{str(data)[:200]}")
        return "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and "text" in block
        )

    async def ainvoke(self, messages: Sequence[Any], **kwargs: Any) -> Any:
        from langchain_core.messages import AIMessage

        body = self.payload(messages)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        async with self._client() as client:
            response = await client.post(
                self.endpoint(),
                headers=self.headers(raw),
                content=raw,
            )
            response.raise_for_status()
            data = response.json()
        usage = data.get("usage") or {}
        return AIMessage(
            content=self._text_from_response(data),
            response_metadata={
                "provider": "bedrock_converse",
                "model": self.model,
                "stop_reason": data.get("stopReason", ""),
                "usage": usage,
            },
        )

    async def astream(self, messages: Sequence[Any], **kwargs: Any) -> AsyncIterator[Any]:
        """**未实现**：`converse-stream` 回的是 `vnd.amazon.eventstream` 二进制帧。

        诚实做法是抛错：它既不是 SSE 也不是 JSON 行，硬按 SSE 解析只会得到一串空事件，
        调用方会以为"模型没说话"。
        """
        raise NotImplementedError(
            "Bedrock 的 converse-stream 用 vnd.amazon.eventstream 二进制帧（不是 SSE），"
            "本轮未实现解码。要流式请改用 chat_completions / anthropic / gemini 类 provider。"
        )
        yield  # pragma: no cover - 让本方法保持异步生成器签名

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        raise NotImplementedError(
            "Converse 的工具形状是 `toolConfig.tools[].toolSpec.inputSchema.json`，与 OpenAI 的 "
            "`function` 结构不同，本轮未实现转换。**不做假实现**：转错结构会让工具调用静默失效。"
        )


def bedrock_client(
    *,
    model: str,
    region: str = "",
    base_url: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
    env: Mapping[str, str] | None = None,
    now: Callable[[], datetime] | None = None,
) -> BedrockConverseClient:
    """按环境变量造一个 Converse 客户端（**不读凭据文件**，见 :func:`bedrock_available`）。

    ``base_url`` 留空 = 按 region 推官方端点（私有链接 / VPC 端点由调用方显式给）。
    """
    source = os.environ if env is None else env
    ok, reason = bedrock_available(source)
    if not ok:
        raise MissingCredential(f"Bedrock 不可用：{reason}")
    target_region = (region or str(source.get("AWS_REGION", "")) or str(source.get("AWS_DEFAULT_REGION", ""))).strip()
    endpoint = base_url.strip() or f"https://bedrock-runtime.{target_region}.amazonaws.com"
    return BedrockConverseClient(
        base_url=endpoint,
        model=model,
        region=target_region,
        access_key=str(source.get("AWS_ACCESS_KEY_ID", "")).strip(),
        secret_key=str(source.get("AWS_SECRET_ACCESS_KEY", "")).strip(),
        session_token=str(source.get("AWS_SESSION_TOKEN", "")).strip(),
        transport=transport,
        clock=now,
    )


# ── 轻量客户端（anthropic_messages / gemini）────────────────────────────────


def _text_of(content: Any) -> str:
    """从 LangChain 消息 / dict / 纯字符串里取出文本。

    **多模态与工具消息一律抛 `NotImplementedError`**：图片（Anthropic 的 image
    block、Gemini 的 inline_data）和 tool_result 各有自己的线格式，静默丢内容会让
    模型"看不见图"却给出看起来正常的回答——那比报错危险得多。
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {None, "text"}:
                parts.append(str(item.get("text", "")))
            else:
                raise NotImplementedError(
                    "本客户端只支持纯文本内容：多模态内容块（image / inline_data）"
                    "需要各自的线格式，本轮未实现。"
                )
        return "".join(parts)
    return str(content)


def _wire_messages(messages: Sequence[Any]) -> list[dict[str, str]]:
    """统一成 `[{"role": ..., "content": ...}]`，角色限定 system/user/assistant。

    工具消息（`ToolMessage`）直接抛错：Anthropic / Gemini 的 tool 结果块结构与
    OpenAI 完全不同，硬转会把工具输出变成普通文本塞进对话，模型会当成"用户说的"。
    """
    out: list[dict[str, str]] = []
    for message in messages:
        if isinstance(message, dict):
            role = str(message.get("role", "user"))
            content = message.get("content", "")
        else:
            kind = type(message).__name__
            role = {
                "SystemMessage": "system",
                "HumanMessage": "user",
                "AIMessage": "assistant",
                "AIMessageChunk": "assistant",
            }.get(kind, "")
            if not role:
                raise NotImplementedError(
                    f"本客户端不支持消息类型 {kind}：工具消息的线格式与 OpenAI 不同，"
                    "需要各自实现后再接入。"
                )
            content = getattr(message, "content", "")
        if role not in {"system", "user", "assistant"}:
            raise ValueError(f"未知消息角色：{role}")
        out.append({"role": role, "content": _text_of(content)})
    return out


class AnthropicMessagesClient:
    """Anthropic Messages（`POST {base}/v1/messages`）的轻量实现。

    为什么不用 `langchain-anthropic`：本机没装，且**不允许安装**。这里用 httpx 直接
    打协议，只实现 `ainvoke` / `astream` 两个方法（图执行真正用到的那两个）。

    与 `ChatOpenAI` 的接口子集：`ainvoke(messages) -> AIMessage`、
    `astream(messages) -> AsyncIterator[AIMessageChunk]`、`bind_tools(...)` 抛
    `NotImplementedError`（原因见该方法）。

    异常**不吞**：httpx 的 `HTTPStatusError` / `TransportError` 原样冒出去，
    `app.fallback` 才认得出 429 / 503 / 连接失败。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        extra_body: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_tokens: int = 4096,
        body_hook: BuildExtraBodyHook | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens
        self._api_key = api_key
        self._extra = dict(extra_body or {})
        self._transport = transport
        #: 整份请求体的最后一道改写（MATRIX 4.1 的 `build_extra_body`）。
        #: **为什么必须由客户端持有**：只有这里才知道最终的请求体长什么样
        #: （system 提到顶层、messages 过滤之后）。在 `build_chat_model` 里改，
        #: 改的是一份还没成形的 dict，钩子看到的形状与发出去的形状不是一回事。
        self._body_hook = body_hook

    def __repr__(self) -> str:  # 绝不把 key 放进 repr：它会被日志和异常栈吃掉
        return f"AnthropicMessagesClient(model={self.model!r}, base_url={self.base_url!r})"

    # -- 协议细节 ---------------------------------------------------------
    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/v1/messages"

    def headers(self) -> dict[str, str]:
        """Anthropic 原生是 `x-api-key` + `anthropic-version`，**不是** Bearer
        （调研 §3.1 #9）。MiniMax 的 `/anthropic` 面同样认这套。"""
        return {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def payload(self, messages: Sequence[Any], *, stream: bool) -> dict[str, Any]:
        wire = _wire_messages(messages)
        # Anthropic 的 system 是**顶层字段**，不能留在 messages 里。
        system = "\n\n".join(item["content"] for item in wire if item["role"] == "system")
        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": [item for item in wire if item["role"] != "system"],
            "stream": stream,
        }
        if system:
            body["system"] = system
        body.update(self._extra)
        # 钩子排在 `extra_body` **之后**：extra_body 是"加一个字段"，build_extra_body
        # 是"整份体改写的最后一道"。顺序反了，钩子写下的字段会被 extra_body 覆盖，
        # 而日志里看到的是钩子的结果——又是一类查不出来的静默错误。
        if self._body_hook is not None:
            body = dict(self._body_hook(self.model, body))
        return body

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=self.timeout)

    @staticmethod
    def _text_from_response(data: dict[str, Any]) -> str:
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise ProviderError(f"Anthropic 返回里没有 content 块：{str(data)[:200]}")
        return "".join(
            str(block.get("text", ""))
            for block in blocks
            if isinstance(block, dict) and block.get("type") == "text"
        )

    # -- 接口子集 ---------------------------------------------------------
    async def ainvoke(self, messages: Sequence[Any], **kwargs: Any) -> Any:
        from langchain_core.messages import AIMessage

        async with self._client() as client:
            response = await client.post(
                self.endpoint, headers=self.headers(), json=self.payload(messages, stream=False)
            )
            response.raise_for_status()
            data = response.json()
        return AIMessage(
            content=self._text_from_response(data),
            response_metadata={
                "provider": "anthropic_messages",
                "model": data.get("model", self.model),
                "stop_reason": data.get("stop_reason", ""),
                "usage": data.get("usage", {}),
            },
        )

    async def astream(self, messages: Sequence[Any], **kwargs: Any) -> AsyncIterator[Any]:
        """SSE 流式。只处理 `content_block_delta` 的文本增量——其它事件（tool_use、
        thinking）本轮没有对应实现，直接跳过而不是伪造。"""
        from langchain_core.messages import AIMessageChunk

        async with self._client() as client:
            async with client.stream(
                "POST",
                self.endpoint,
                headers=self.headers(),
                json=self.payload(messages, stream=True),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[len("data:") :].strip()
                    if not raw or raw == "[DONE]":
                        continue
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        continue
                    if event.get("type") == "error":
                        raise ProviderError(
                            f"Anthropic 流式返回错误：{event.get('error', event)}"
                        )
                    delta = event.get("delta")
                    if (
                        event.get("type") == "content_block_delta"
                        and isinstance(delta, dict)
                        and delta.get("type") == "text_delta"
                    ):
                        yield AIMessageChunk(content=str(delta.get("text", "")))

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        raise NotImplementedError(
            "Anthropic Messages 的 tools 线格式（`tools[].input_schema`、"
            "`tool_use` / `tool_result` 内容块、`tool_choice: {type: ...}`）与 OpenAI 的 "
            "`function` 结构不同，本轮未实现；且 Orbit 的图执行目前只走 "
            "chat_completions 的 bind_tools。**不做假实现**：真转一份近似结构会让工具"
            "调用静默失效。要接工具请改用 chat_completions 类 provider。"
        )


class GeminiClient:
    """Gemini 原生 `generateContent` / `streamGenerateContent` 的轻量实现。

    Hermes 也是自建 `GeminiNativeClient` 绕过标准 transport（调研 §2.1 的注脚），
    因为 Gemini 的 `thinking_config`、`systemInstruction`、`parts` 结构在 OpenAI
    兼容层里表达不全。这里同样走原生面。

    接口子集与 `AnthropicMessagesClient` 一致；`bind_tools` 抛
    `NotImplementedError`（Gemini 的 `functionDeclarations` 是另一套 schema）。
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        timeout: float = 60.0,
        extra_body: dict[str, Any] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        body_hook: BuildExtraBodyHook | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self._api_key = api_key
        self._extra = dict(extra_body or {})
        self._transport = transport
        #: 与 `AnthropicMessagesClient` 同一个理由：只有客户端知道最终请求体形状。
        self._body_hook = body_hook

    def __repr__(self) -> str:
        return f"GeminiClient(model={self.model!r}, base_url={self.base_url!r})"

    def headers(self) -> dict[str, str]:
        """用 `x-goog-api-key` 头而不是 `?key=`：URL 会进 httpx 的异常消息与访问日志。"""
        return {"x-goog-api-key": self._api_key, "content-type": "application/json"}

    def endpoint(self, method: str, *, stream: bool) -> str:
        url = f"{self.base_url}/models/{self.model}:{method}"
        return f"{url}?alt=sse" if stream else url

    def payload(self, messages: Sequence[Any]) -> dict[str, Any]:
        wire = _wire_messages(messages)
        system = "\n\n".join(item["content"] for item in wire if item["role"] == "system")
        # Gemini 的角色只有 user / model 两种，assistant → model。
        contents = [
            {
                "role": "model" if item["role"] == "assistant" else "user",
                "parts": [{"text": item["content"]}],
            }
            for item in wire
            if item["role"] != "system"
        ]
        body: dict[str, Any] = {"contents": contents}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        if self._extra:
            body["generationConfig"] = self._extra
        if self._body_hook is not None:
            body = dict(self._body_hook(self.model, body))
        return body

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self._transport, timeout=self.timeout)

    @staticmethod
    def _text_from_candidates(data: dict[str, Any]) -> str:
        candidates = data.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            blocked = data.get("promptFeedback") or {}
            raise ProviderError(f"Gemini 没有返回候选：{str(blocked or data)[:200]}")
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        return "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict))

    async def ainvoke(self, messages: Sequence[Any], **kwargs: Any) -> Any:
        from langchain_core.messages import AIMessage

        async with self._client() as client:
            response = await client.post(
                self.endpoint("generateContent", stream=False),
                headers=self.headers(),
                json=self.payload(messages),
            )
            response.raise_for_status()
            data = response.json()
        return AIMessage(
            content=self._text_from_candidates(data),
            response_metadata={
                "provider": "gemini",
                "model": data.get("modelVersion", self.model),
                "usage": data.get("usageMetadata", {}),
            },
        )

    async def astream(self, messages: Sequence[Any], **kwargs: Any) -> AsyncIterator[Any]:
        from langchain_core.messages import AIMessageChunk

        async with self._client() as client:
            async with client.stream(
                "POST",
                self.endpoint("streamGenerateContent", stream=True),
                headers=self.headers(),
                json=self.payload(messages),
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[len("data:") :].strip()
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw)
                    except ValueError:
                        continue
                    candidates = event.get("candidates")
                    if not isinstance(candidates, list) or not candidates:
                        continue
                    parts = (candidates[0].get("content") or {}).get("parts") or []
                    text = "".join(
                        str(part.get("text", "")) for part in parts if isinstance(part, dict)
                    )
                    if text:
                        yield AIMessageChunk(content=text)

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        raise NotImplementedError(
            "Gemini 的工具调用是 `tools[].functionDeclarations`（OpenAPI Schema 子集）"
            "加 `functionCall` / `functionResponse` parts，与 OpenAI 的 `function` 结构"
            "不同，本轮未实现。**不做假实现**：转错结构会让工具调用静默失效。"
        )


__all__ = [
    "AMZ_DATE_FORMAT",
    "BEDROCK_ENV_KEYS",
    "CAPABILITY_FIELDS",
    "FACTORY_CAPABILITY",
    "PROVIDERS",
    "PROVIDER_HOOKS",
    "SIGV4_ALGORITHM",
    "SUPPORTED_TRANSPORTS",
    "THINKING_BUDGETS",
    "TRANSPORT_CLIENTS",
    "UNSUPPORTED_TRANSPORTS",
    "AnthropicMessagesClient",
    "BedrockConverseClient",
    "BuildExtraBodyHook",
    "FetchModelsHook",
    "GeminiClient",
    "MissingCredential",
    "ModelSettings",
    "ProviderError",
    "ProviderHooks",
    "ProviderProfile",
    "SigV4Result",
    "UnknownProvider",
    "UnsupportedTransport",
    "apply_build_extra_body",
    "bedrock_available",
    "bedrock_client",
    "build_chat_model",
    "cache_path",
    "canonical_query_string",
    "capabilities",
    "capability",
    "capability_with_source",
    "extra_body_for",
    "fetch_models_for",
    "hook_rows",
    "hooks_for",
    "load_cache",
    "record_capability",
    "resolve_api_key",
    "resolve_provider",
    "save_cache",
    "sigv4_canonical_request",
    "sigv4_headers",
    "sigv4_scope",
    "sigv4_signature",
    "sigv4_string_to_sign",
    "sign_sigv4",
    "uri_encode",
    "with_hooks",
]
