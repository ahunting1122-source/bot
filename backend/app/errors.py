"""模型调用失败分类：决定**要不要重试**，以及给用户一句能行动的话。

## 为什么不能"失败就重试三次"

原来的 `Nodes.ask_model` 对任何异常都重试 `MODEL_ATTEMPTS` 次。这看起来稳健，
实际有两个问题：

1. **认证失败重试没有意义**。401/403 是密钥或地址错了，重试三次还是错，只是把
   用户多等三个来回。真正该做的是立刻停并告诉他"密钥无效"。
2. **错误信息被抹平**。三次都失败后统一说"请检查连接设置、API 地址与密钥"，
   而真实原因是 400（请求格式不对）时，用户会去查密钥——查错了方向。

Hermes 的做法是把失败分成若干类，**只有 4 类值得自动重试**（限流、超时、连接、
服务过载），其余立刻停并给出针对性建议。这里照做。

## 13 类怎么来的

按"用户下一步该做什么"分，而不是按 HTTP 状态码分——同一个 400 可能意味着
"上下文太长"（该压缩）或"参数不合法"（该改配置），它们的处理完全不同。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: 值得自动重试的四类。其余立刻停——重试改变不了结果，只是让用户多等。
RETRYABLE = frozenset({"rate_limit", "timeout", "connection", "overloaded"})

#: 全部 13 类。每类给一句"下一步做什么"。
ADVICE: dict[str, str] = {
    "auth": "密钥无效或没有该模型的权限：检查 API key，或换一个 provider。",
    "rate_limit": "触发限流：稍等重试，或换一个 key（`orbit auth` 里可以配多个）。",
    "quota": "额度用尽：充值或换 provider。重试不会恢复额度。",
    "bad_request": "请求格式被拒：多半是模型名不对，或该模型不支持当前参数（如 tools）。",
    "context_overflow": "上下文超出窗口：让 Orbit 压缩历史后重试（`/compact`），或换窗口更大的模型。",
    "not_found": "模型或端点不存在：检查模型名与 base_url（`orbit model show`）。",
    "timeout": "请求超时：网络慢或服务端排队，重试通常有效。",
    "connection": "连不上：检查网络、代理与 base_url。重试通常有效。",
    "server": "服务端 5xx：这是对方的问题，稍后重试或换 provider。",
    "overloaded": "服务过载：稍等重试，或换 provider。",
    "content_filter": "被内容策略拦下：改写提问，或换一个不过滤的模型。",
    "cancelled": "调用被取消（用户中断或超时上限到了）。",
    "unknown": "未识别的失败：把这条错误的原文发给开发者。",
}

#: 分类特征。**顺序有意义**：先判更具体的（auth 里的 401 也可能带 "quota" 字样）。
#: 每条是 (kind, 正则)。匹配对象是 `f"{type(exc).__name__}: {exc}"` 的小写形式。
PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cancelled", re.compile(r"cancell?ed|asyncio\.timeouterror.*cancel|interrupted")),
    (
        "context_overflow",
        re.compile(
            r"context[_ ]length|maximum context|too many tokens|context window|"
            r"prompt is too long|exceeds? the (?:maximum|max) (?:context|token)"
        ),
    ),
    (
        "quota",
        re.compile(r"insufficient[_ ]quota|quota exceeded|billing|payment required|402"),
    ),
    (
        "auth",
        re.compile(
            r"401|403|unauthor|invalid[_ ]api[_ ]key|invalid api key|authentication|"
            r"permission denied|no access|api key not valid"
        ),
    ),
    ("rate_limit", re.compile(r"429|rate[_ ]limit|too many requests|slow down")),
    ("overloaded", re.compile(r"overloaded|503|service unavailable|at capacity")),
    ("timeout", re.compile(r"timeout|timed out|read timeout|deadline exceeded")),
    ("connection", re.compile(r"connection|connect error|network|dns|ssl|proxy|unreachable")),
    ("not_found", re.compile(r"404|not found|no such model|unknown model|does not exist")),
    ("content_filter", re.compile(r"content[_ ]filter|safety|blocked by|flagged")),
    ("bad_request", re.compile(r"400|422|invalid[_ ]request|bad request|unsupported|malformed")),
    ("server", re.compile(r"500|502|504|internal server error|bad gateway|gateway timeout")),
)


@dataclass(frozen=True)
class Failure:
    """一次失败的分类结果。`kind` 是 13 类之一，`retryable` 由它推出。"""

    kind: str
    retryable: bool
    advice: str
    detail: str

    def as_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "retryable": self.retryable,
            "advice": self.advice,
            "detail": self.detail,
        }


def classify(exc: BaseException) -> Failure:
    """把异常归到 13 类之一。

    先看类型（`TimeoutError` / `ConnectionError` 这类没有消息也能判），再看消息。
    都不匹配就归 `unknown`——**不要硬塞进某一类**，错归类会把用户引到错的方向。
    """
    name = type(exc).__name__
    detail = f"{name}: {exc}"[:500]
    haystack = detail.lower()

    if isinstance(exc, TimeoutError):
        kind = "timeout"
    elif isinstance(exc, (ConnectionError, OSError)) and "timeout" not in haystack:
        kind = "connection"
    else:
        kind = "unknown"
        for candidate, pattern in PATTERNS:
            if pattern.search(haystack):
                kind = candidate
                break

    return Failure(
        kind=kind,
        retryable=kind in RETRYABLE,
        advice=ADVICE.get(kind, ADVICE["unknown"]),
        detail=detail,
    )


def summary(failure: Failure, attempts: int) -> str:
    """给用户看的一句话。**先说类别与建议**，再说试了几次——顺序反了人就只看到数字。"""
    prefix = f"模型调用失败（{failure.kind}）"
    if attempts > 1:
        prefix += f"，已尝试 {attempts} 次"
    return f"{prefix}：{failure.advice}"


def is_context_overflow(exc: BaseException) -> bool:
    """单独一个判断函数：压缩重试的路径要它，别处也用得上。"""
    return classify(exc).kind == "context_overflow"
