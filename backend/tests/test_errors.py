"""失败分类的契约测试（MATRIX 2.6 / 2.7）。

核心断言：**只有 4 类可重试**（限流 / 超时 / 连接 / 服务过载）。401 重试三次还是
401，只是让用户多等三个来回，而且最后的统一错误信息会把真实原因抹平——用户会
照着"检查密钥"去查一个 400。

**当前状态：分类层已实现，`Nodes.ask_model` 的接线还没落地**（那一轮 `app/agent.py`
正被另一个子agent 重写，同时改会互相覆盖）。所以这里只锁分类与文案；"不可重试时
只调用一次"那条要等接线后用 `ask_model` 直接观察调用次数，别用 `classify()` 冒充。
"""

from __future__ import annotations

import pytest

from app import errors

# ── 分类 ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Error code: 401 - invalid api key", "auth"),
        ("403 permission denied", "auth"),
        ("429 too many requests", "rate_limit"),
        ("insufficient_quota: billing hard limit", "quota"),
        ("400 - unsupported parameter tools", "bad_request"),
        ("maximum context length is 128000 tokens", "context_overflow"),
        ("404 no such model", "not_found"),
        ("APITimeoutError: request timed out", "timeout"),
        ("APIConnectionError: connection error", "connection"),
        ("500 internal server error", "server"),
        ("503 service unavailable", "overloaded"),
        ("content_filter: blocked by safety", "content_filter"),
        ("CancelledError: cancelled by user", "cancelled"),
        ("something nobody has seen before", "unknown"),
    ],
)
def test_every_kind_is_reachable(message, expected):
    assert errors.classify(RuntimeError(message)).kind == expected


def test_only_four_kinds_are_retryable():
    assert errors.RETRYABLE == {"rate_limit", "timeout", "connection", "overloaded"}
    for kind in errors.ADVICE:
        failure = errors.Failure(kind=kind, retryable=kind in errors.RETRYABLE, advice="", detail="")
        assert failure.retryable is (kind in errors.RETRYABLE)


def test_advice_covers_all_thirteen_kinds():
    assert len(errors.ADVICE) == 13
    assert all(errors.ADVICE.values())


def test_timeout_error_type_is_enough_without_a_message():
    """`TimeoutError` 没有可读消息也要能分类——类型本身就是信息。"""
    assert errors.classify(TimeoutError()).kind == "timeout"


def test_unknown_is_not_forced_into_a_wrong_bucket():
    """错归类会把用户引到错的方向，所以宁可 unknown。"""
    failure = errors.classify(RuntimeError("完全没见过的一句话"))
    assert failure.kind == "unknown"
    assert failure.retryable is False


def test_summary_leads_with_kind_and_advice():
    """先说类别与建议，再说试了几次——顺序反了人只看到数字。"""
    text = errors.summary(errors.classify(RuntimeError("401 unauthorized")), 1)
    assert "auth" in text
    assert "API key" in text
    assert "已尝试" not in text  # 只试一次时不该提次数


def test_summary_mentions_attempts_when_more_than_one():
    text = errors.summary(errors.classify(RuntimeError("429 rate limit")), 3)
    assert "已尝试 3 次" in text


def test_is_context_overflow_helper():
    assert errors.is_context_overflow(RuntimeError("maximum context length exceeded"))
    assert not errors.is_context_overflow(RuntimeError("401 unauthorized"))
