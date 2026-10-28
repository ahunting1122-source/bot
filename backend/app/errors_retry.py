"""把 `app/errors.py` 的失败分类接进主循环，并守住消息配对不变量（MATRIX 2.6 / 2.7 / 2.4）。

## 这个模块为什么存在（而不是把判断写进 `nodes.py`）

`app/errors.py` 只回答一件事：**这是哪一类失败、该不该重试**。但"现在要不要重试"
还取决于循环层才知道的两件事：

1. **是不是已经流出半截正文**。流式回答已经推给前端了，此时重试会把重复内容
   再推一遍——用户看到两段拼接的答案，比直接报错更糟。这条规矩先于分类：
   只要吐过字，一律停，原样抛（保留类型与栈，方便排障）。
2. **这次上下文溢出有没有压缩过**。压缩只能做一次：压完还是溢出，说明窗口就是
   不够大（或请求本身写错了），再压就是死循环。

把"分类 + 这两件事"合成一个纯函数（`decide`），好处是能脱离 runner、脱离模型、
脱离数据库单测——循环层最难测的恰恰是这些分支。

## 为什么中断要走"返回值"而不是"抛异常"

`tool_calls` 与 `tool_results` 的配对是供应商侧的硬约束：一条 `assistant` 消息里
有 N 个 `tool_call`，下一条请求就必须带齐 N 条 `tool` 结果，缺一条直接 400。
工具被中断时**抛异常**意味着那条 `tool` 结果永远不会产生，配对当场非法——
下一次请求就废了，任务连"交回半截结果"的机会都没有。

Hermes 的做法（research 09 §1.4，`tools/interrupt.py:11-14`）是让被中断的工具
**照常返回一条结果**：`{"output": "[interrupted]", "returncode": 130}`。
于是配对天然成立，模型也能读到"这一步被中断了"而不是凭空少一步。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import ToolMessage

from app import errors

#: 重试（同一份消息再来一次）。
RETRY = "retry"
#: 先压缩上下文、再重试一次（`context_overflow` 专用）。
COMPACT = "compact_retry"
#: 立刻停，把失败落成一句能看懂的话。
STOP = "stop"

#: 中断结果的两个固定字段。数值与 Hermes `interrupt.py` 逐字一致：
#: `returncode=130` 是 shell 约定的"被 SIGINT 中断"，下游工具链都认这个数。
INTERRUPTED_OUTPUT = "[interrupted]"
INTERRUPTED_RETURNCODE = 130


@dataclass(frozen=True)
class Decision:
    """一次失败的处置决定。`reason` 给日志与事件用，`failure` 给文案用。"""

    action: str
    failure: errors.Failure
    attempt: int
    attempts: int
    #: `partial`（已吐正文）/ `context_overflow` / `retryable` / `fatal` / `exhausted`
    reason: str

    @property
    def retryable(self) -> bool:
        return self.action in {RETRY, COMPACT}

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "kind": self.failure.kind,
            "attempt": self.attempt,
            "attempts": self.attempts,
            "reason": self.reason,
        }


def decide(
    exc: BaseException,
    *,
    attempt: int,
    attempts: int,
    has_partial: bool = False,
    compacted: bool = False,
) -> Decision:
    """决定这次失败怎么处置。顺序有意义，不要重排：

    1. **已吐正文 → 停**。这条压过一切：重试会把重复内容推给前端。
    2. **上下文溢出且没压过 → 压缩重试**。`context_overflow` 在 `errors.py` 里
       不是"可重试"四类之一，但它有专属补救动作（压完再试一次）。
    3. **可重试且还有次数 → 重试**。
    4. 其余（不可重试 / 次数用完）→ 停。
    """
    failure = errors.classify(exc)
    if has_partial:
        return Decision(STOP, failure, attempt, attempts, "partial")
    if failure.kind == "context_overflow" and not compacted:
        return Decision(COMPACT, failure, attempt, attempts, "context_overflow")
    if failure.retryable and attempt < attempts:
        return Decision(RETRY, failure, attempt, attempts, "retryable")
    return Decision(
        STOP,
        failure,
        attempt,
        attempts,
        "exhausted" if failure.retryable else "fatal",
    )


def interrupted_result(name: str = "") -> dict[str, Any]:
    """被中断的工具"结果"。**返回它，不要抛异常**（理由见模块 docstring）。

    带上工具名与 `status` 是为了两件事：`nodes.execute` 之后的 `digest` 需要
    `status` 才能落库（`should_index` 只索引 `ok`，中断不会被索引）；事件流里
    能看出是哪个工具被中断了，而不是只有一句 `[interrupted]`。
    """
    result: dict[str, Any] = {
        "status": "interrupted",
        "output": INTERRUPTED_OUTPUT,
        "returncode": INTERRUPTED_RETURNCODE,
    }
    if name:
        result["tool"] = name
    return result


def _answered_ids(messages: list[Any]) -> set[str]:
    answered = set()
    for message in messages:
        if getattr(message, "type", "") != "tool":
            continue
        call_id = getattr(message, "tool_call_id", None)
        if call_id:
            answered.add(str(call_id))
    return answered


def missing_tool_results(messages: list[Any]) -> list[str]:
    """列出"被调用但没回结果"的 `tool_call_id`（保持出现顺序）。

    这是配对的**唯一判据**：数 `tool_calls` 与 `tool` 消息的条数不够——两个
    不同 id 各缺一条、又多一条重复的，条数也能相等。
    """
    answered = _answered_ids(messages)
    missing: list[str] = []
    for message in messages:
        for call in getattr(message, "tool_calls", None) or []:
            call_id = str(call.get("id") or "")
            if call_id and call_id not in answered and call_id not in missing:
                missing.append(call_id)
    return missing


def repair_tool_pairs(messages: list[Any]) -> list[ToolMessage]:
    """给每个"没回结果"的 `tool_call` 补一条中断占位，返回**需要追加**的消息。

    为什么要修补而不是"跳过那一步"：任务被取消/进程被杀时，checkpoint 里留下
    一条带 `tool_calls` 的 assistant 消息却没有对应的 `tool` 消息。这种状态一旦
    被原样发给供应商，就是 400，而且**整个会话从此不可用**——每一轮都带着这个
    非法配对。补一条 `[interrupted]` 占位让会话能继续，是唯一的出路
    （Hermes 同款：`message_sanitization` 对畸形 tool_call 也是"宁可补占位"）。

    补的是**中断结果**而不是空对象：模型读到"这一步被中断了"，能自己决定重试
    还是换路；读到 `{}` 只会以为工具什么都没做。

    返回 list 而不是新 messages：调用点用 `add_messages` 追加，不改历史。
    """
    missing = missing_tool_results(messages)
    if not missing:
        return []
    # 延迟导入：`app.tools` 会把 pydantic/工具注册表整条链拉起来，而本模块要被
    # 循环层与测试在"还没有 registry"的场景下使用。
    from app.tools import as_tool_text

    body = as_tool_text(interrupted_result())
    return [ToolMessage(content=body, tool_call_id=call_id) for call_id in missing]


def interrupted_payload_text(name: str = "") -> str:
    """中断结果的 JSON 文本（与 `as_tool_text` 的序列化口径一致，便于测试直接断言）。"""
    return json.dumps(interrupted_result(name), ensure_ascii=False)


__all__ = [
    "COMPACT",
    "INTERRUPTED_OUTPUT",
    "INTERRUPTED_RETURNCODE",
    "RETRY",
    "STOP",
    "Decision",
    "decide",
    "interrupted_payload_text",
    "interrupted_result",
    "missing_tool_results",
    "repair_tool_pairs",
]
