"""per-model 路由：给定一个模型名，决定"走哪家、哪个端点、能干什么"。

## 为什么需要这一层（而不是只靠 `providers.resolve_provider`）

`resolve_provider()` 回答的是"**当前配置**指向哪家"，输入是 `Settings`；
本模块回答的是"**这个模型名**属于哪家、该走哪条线协议"，输入是模型名。
两件事在真实场景里必须分开：

1. **反查天生有歧义**。同一个端点可能对应多个 profile（`api.moonshot.cn/v1` 既是
   kimi-coding-cn 也是 moonshot 开放平台；OpenRouter 的 389 个模型全都挂在同一个
   base_url 下）。只按端点反查，`claude-sonnet-5` 到底该走 `anthropic` 还是
   `openrouter` 是没有答案的。
2. **用户显式选过的 provider 不该被"猜"覆盖**。Hermes 的 `hermes_cli/providers.py`
   里那张 `host_mandated_api_mode` 强制表就是这个教训：路由必须能被显式指定，
   且显式指定优先于任何推断。
3. **能力元数据是 per-model 的**。`context_window` / `supports_tools` /
   `supports_reasoning` 挂在模型上而不是 provider 上（`gpt-5.4` 支持 tools，
   `deepseek-chat` 不支持 reasoning）。发错能力直接 4xx，而不是"效果差一点"。

所以本模块**只读** `app/providers.py` 的 `PROVIDERS` / `models` /
`capability_with_source()`，不新增任何模型知识；它做的只是把"模型名 → 站"这一步
显式化，并在**无法确定时明确说不支持**——猜错的代价是 400 和一轮白花的 token。

## 不猜的三种情形

- 未知模型（所有 profile 的 `models` 里都没有，且该家不是"清单未知"型）→
  `supported=False`，理由里写清"没在任何 profile 的模型表里"。
- 歧义模型（多个 profile 都有同名模型，且用户没显式指定 provider）→
  `supported=False`，理由里列出候选，让调用方去显式指定。
- 线协议未实现（`bedrock_converse`）→ `supported=False`，理由照抄
  `providers.UNSUPPORTED_TRANSPORTS`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app import providers as pv

#: 反查结果里"这条结论从哪来"的取值。
SOURCE_EXPLICIT = "explicit"
SOURCE_MODEL = "model"
SOURCE_AMBIGUOUS = "ambiguous"
SOURCE_UNKNOWN = "unknown"


@dataclass(frozen=True)
class RouteDecision:
    """一个模型名该走哪条路。

    `supported=False` 时**只有** `model` / `provider` / `reason` 有意义，其余字段
    保持默认值——不填"看起来合理"的猜测值，否则调用方会误以为能用。
    """

    model: str
    provider: str = ""
    transport: str = ""
    base_url: str = ""
    context_window: int = 0
    supports_tools: bool = False
    supports_reasoning: bool = False
    supports_vision: bool = False
    supported: bool = False
    #: 不支持时的原因 / 支持时的判定依据（如 `explicit` / `model`）。
    reason: str = ""
    source: str = ""
    #: 每个能力字段的取值来源（`manual` / `cache` / `factory`），见
    #: `providers.capability_with_source`。让"这个数字是查到的还是兜底的"可查。
    capability_sources: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "transport": self.transport,
            "base_url": self.base_url,
            "context_window": self.context_window,
            "supports_tools": self.supports_tools,
            "supports_reasoning": self.supports_reasoning,
            "supports_vision": self.supports_vision,
            "supported": self.supported,
            "reason": self.reason,
            "source": self.source,
            "capability_sources": dict(self.capability_sources),
        }


def _unsupported(model: str, reason: str, source: str, provider: str = "") -> RouteDecision:
    return RouteDecision(model=model, provider=provider, supported=False, reason=reason, source=source)


def split_model(model_name: str) -> tuple[str, str]:
    """`provider/model` → (provider, model)；不带前缀时返回 ("", 原名)。

    只切**第一个**分隔符：OpenRouter 的模型名本身带斜杠
    （`openrouter/anthropic/claude-sonnet-4.6`），全切会把模型名拆碎。
    前缀必须是已知 provider id，否则整串当模型名——`qwen/qwen3-plus` 这种
    "看起来像前缀但其实不是"的写法不会被误切。
    """
    raw = (model_name or "").strip()
    head, sep, tail = raw.partition("/")
    if sep and tail.strip() and head.strip().lower() in pv.PROVIDERS:
        return head.strip().lower(), tail.strip()
    return "", raw


def candidates(model: str) -> list[pv.ProviderProfile]:
    """模型名出现在哪些 profile 的 `models` 表里（顺序即注册顺序，稳定可断言）。"""
    return [profile for profile in pv.PROVIDERS.values() if model in profile.models]


def route(model_name: str, settings: Any = None, *, data_dir: Any = None) -> RouteDecision:
    """决定一个模型名走哪家、哪个端点、有哪些能力。

    优先级（"显式"只用来**消歧**，不是无条件覆盖）：

    1. 模型名自带 `provider/` 前缀，且该家模型表里有它 → 用那个 provider；
    2. `settings.model_provider` 显式指定，且该家模型表里有它 → 用那个 provider
       （**不被反查覆盖**：同一端点可能对应多个 profile，猜错就是把请求发错家）；
    3. 显式指定的那家 `models_known=False`（`custom` / `bedrock`，清单只能实时拉取）
       → 仍然信用户，能力按出厂兜底算；
    4. 否则退回模型表反查：唯一命中 → 用它；多个命中 → 歧义，`supported=False`；
    5. 反查落空时，把**整串**再当模型名试一次（OpenRouter 的
       `qwen/qwen3-plus` 会被第 1 步误切，这里补回来）；
    6. 全都没有 → 未知，`supported=False`（不猜）。

    `data_dir` 传进来时才会查运行时能力缓存（二级），否则只用 profile 手工表与
    出厂兜底——纯函数、无 IO 是默认行为，避免"查一次路由读一次盘"。
    """
    raw = (model_name or "").strip()
    explicit, model = split_model(raw)
    if not model:
        return _unsupported(model_name, "模型名为空", SOURCE_UNKNOWN)

    explicit = explicit or str(getattr(settings, "model_provider", "") or "").strip().lower()
    if explicit and explicit not in pv.PROVIDERS:
        return _unsupported(
            model,
            f"未知 provider `{explicit}`（`orbit model --list` 看全部 {len(pv.PROVIDERS)} 家）",
            SOURCE_EXPLICIT,
            provider=explicit,
        )

    profile: pv.ProviderProfile | None = None
    source = ""
    if explicit:
        pinned = pv.PROVIDERS[explicit]
        if not pinned.models_known or model in pinned.models:
            # 清单未知（custom / bedrock）时不能因为"表里没有"就否掉用户自填的模型；
            # 表里有它就是明确的命中。两种情况都信显式选择。
            profile, source = pinned, SOURCE_EXPLICIT

    if profile is None:
        found = candidates(model)
        if len(found) > 1:
            names = ", ".join(item.id for item in found)
            return _unsupported(
                model,
                f"`{model}` 在多家 provider 里都有（{names}），不猜。"
                "用 `provider/model` 或显式设置 `model_provider` 指定。",
                SOURCE_AMBIGUOUS,
            )
        if len(found) == 1:
            profile, source = found[0], SOURCE_MODEL
        else:
            # 整串再试一次：`qwen/qwen3-plus` 的第 1 步会把 `qwen` 当成 provider 前缀，
            # 但它其实是 OpenRouter 目录里的一个模型名。
            whole = candidates(raw) if raw != model else []
            if len(whole) > 1:
                names = ", ".join(item.id for item in whole)
                return _unsupported(
                    raw,
                    f"`{raw}` 在多家 provider 里都有（{names}），不猜。"
                    "用显式设置 `model_provider` 指定。",
                    SOURCE_AMBIGUOUS,
                )
            if len(whole) == 1:
                profile, model, source = whole[0], raw, SOURCE_MODEL
            else:
                hint = (
                    f"{explicit} 的模型表里没有 `{model}`，其它 provider 也没有。"
                    if explicit
                    else f"`{model}` 不在任何 provider 的模型表里。"
                )
                return _unsupported(
                    model,
                    hint
                    + "用 `provider/model` 指定，或先用 `orbit model --refresh` 拉取该家的实时清单。",
                    SOURCE_UNKNOWN,
                    provider=explicit,
                )

    if not profile.supported:
        reason = pv.UNSUPPORTED_TRANSPORTS.get(profile.transport, "该线协议未实现")
        return _unsupported(
            model,
            f"{profile.id}：线协议 `{profile.transport}` 未实现 —— {reason}",
            source,
            provider=profile.id,
        )

    caps: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for name in pv.CAPABILITY_FIELDS:
        value, origin = pv.capability_with_source(profile, model, name, data_dir=data_dir)
        caps[name] = value
        sources[name] = origin

    return RouteDecision(
        model=model,
        provider=profile.id,
        transport=profile.transport,
        base_url=_base_url_for(profile, settings),
        context_window=int(caps["context_window"]),
        supports_tools=bool(caps["supports_tools"]),
        supports_reasoning=bool(caps["supports_reasoning"]),
        supports_vision=bool(caps["supports_vision"]),
        supported=True,
        reason=f"由 `{source}` 判定为 {profile.id}",
        source=source,
        capability_sources=sources,
    )


def _base_url_for(profile: pv.ProviderProfile, settings: Any) -> str:
    """端点：只有"当前配置指的就是这家"时才用 `settings.model_base_url`。

    否则一律用该家默认值——用户把 base_url 指向自建网关时，别的 provider 的模型
    不该被顺带打到那个网关上（那会得到一个 404 而不是"你没配这家"）。
    """
    if settings is not None:
        try:
            current = pv.resolve_provider(settings)
        except pv.ProviderError:
            current = None
        if current is not None and current.id == profile.id:
            base = str(getattr(settings, "model_base_url", "") or "").strip()
            if base:
                return base
    return profile.default_base_url


__all__ = [
    "SOURCE_AMBIGUOUS",
    "SOURCE_EXPLICIT",
    "SOURCE_MODEL",
    "SOURCE_UNKNOWN",
    "RouteDecision",
    "candidates",
    "route",
    "split_model",
]
