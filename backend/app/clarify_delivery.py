"""`clarify` 的**投递**：把选项真的发到用户所在的平台（MATRIX 5.13）。

## 之前缺的是什么

`app/platforms.py` 有 `render_choices()`（≤5 个出按钮、>5 个降级编号文本）与
`send_clarify()`（有 `send_buttons` 走按钮、遇 `NotSupported` 退回文本），
测试也齐。但**唯一的调用方是 `peer_extra.py` 的手动演示入口**——`clarify` 工具本身
走的是 `clarify_payload()` 那条路，**从来没经过它们**。

所以「≤5 个出按钮 / >5 个降级」这条规则在真实对话里从没生效过。

## 为什么需要一个「当前通道」

`clarify` 在 agent 循环里执行，它**不知道用户在哪个平台**——任务表里也没有平台字段。
所以设计成一个**进程级的可配置通道**：

- 宿主（`orbit chat` / gateway）在开始驱动任务前调 `configure(adapter, channel)`；
- `clarify_payload()` 投递时读它；
- **没配置就什么都不做**，行为与之前完全一致（返回 `needs_user` 载荷，由前端渲染）。

这样"没配"是安全的默认，"配了"才真的发按钮——**不会因为没接通道就让提问失败**。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("orbit.clarify")

#: 当前投递通道：`(adapter, channel_id)`。进程级、可被宿主替换。
_CHANNEL: tuple[Any, str] | None = None

#: 最近一次投递的结果，供 `describe()` 与测试读。
LAST: dict[str, Any] = {}


def configure(adapter: Any, channel: str) -> None:
    """设置当前投递通道。``adapter`` 为 None 等于清除。"""
    global _CHANNEL
    if adapter is None or not str(channel or "").strip():
        _CHANNEL = None
        return
    _CHANNEL = (adapter, str(channel).strip())


def reset() -> None:
    """清空通道与最近结果（测试用）。"""
    global _CHANNEL
    _CHANNEL = None
    LAST.clear()


def active() -> tuple[Any, str] | None:
    return _CHANNEL


def deliver(
    question: str,
    options: list[str],
    *,
    clarify_id: str = "",
    allow_free_text: bool = True,
) -> dict[str, Any] | None:
    """把提问发到当前通道；**没配置通道就返回 None**（调用方保持原行为）。

    返回一份可塞进载荷的摘要，让调用方/前端知道"这次真的发出去了、走的是按钮还是文本"。
    投递失败**不抛**：提问本身（`needs_user` 载荷）必须照常返回，否则用户连问题都看不到。
    """
    if _CHANNEL is None:
        return None
    adapter, channel = _CHANNEL
    from app import platforms

    try:
        result = platforms.send_clarify(
            adapter,
            channel,
            question,
            options,
            clarify_id=clarify_id,
            allow_free_text=allow_free_text,
        )
    except Exception as exc:  # noqa: BLE001 —— 投递失败不该让提问本身失败
        log.warning("clarify 投递失败（提问仍会返回给前端）：%s", exc)
        LAST.clear()
        LAST.update({"delivered": False, "error": f"{type(exc).__name__}: {exc}"})
        return dict(LAST)

    summary = {
        # `ClarifyDelivery` 的真实字段是 `ok / used / platform / channel / message_id / error / rendered`。
        # `used` 才是「走了按钮还是文本」——**读对字段名**比多写几个兜底重要：
        # 我第一版猜 `mode` / `buttons`，结果两个都取不到，摘要永远是 "unknown"。
        "delivered": bool(getattr(result, "ok", False)),
        "used": str(getattr(result, "used", "") or ""),
        "platform": str(getattr(result, "platform", "") or getattr(adapter, "name", "") or ""),
        "channel": str(getattr(result, "channel", "") or channel),
        "message_id": str(getattr(result, "message_id", "") or ""),
        "error": str(getattr(result, "error", "") or ""),
        # `rendered` 里才有「实际渲染成几个选项 / 有没有按钮」
        "rendered": getattr(result, "rendered", None),
    }
    LAST.clear()
    LAST.update(summary)
    return summary


def describe() -> dict[str, Any]:
    """自曝：有没有通道、上一次投递的结果。"""
    return {
        "configured": _CHANNEL is not None,
        "channel": _CHANNEL[1] if _CHANNEL else "",
        "platform": str(getattr(_CHANNEL[0], "platform", "") or "") if _CHANNEL else "",
        "last": dict(LAST),
    }
