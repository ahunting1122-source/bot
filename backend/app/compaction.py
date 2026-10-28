"""任务内上下文压缩：**提前判定**（MATRIX 10.4）+ **摘要式压缩**（MATRIX 2.7）。

## 这个模块为什么单独存在（而不是继续写在 `nodes.py` 里）

`app/graph/nodes.py` 一被 import 就会把 langgraph / langchain 整条链拉起来。而
"该不该压、要丢哪几组、压完大概剩多少"这三件事**全是纯算术**，没有理由绑在图实现上。
拆出来之后：

* `app/session_plane.py::describe()` 能核对同一组数字（它是纯文件命令的依赖，
  不能为了读一个常量去 import 整个图）；
* `tests/test_compaction.py` 能只测算术，不用起模型、不用起数据库；
* 阈值只在这一处定义，`nodes.py` 不再各写一份——"到底按哪个数判"只有一个答案。

## 口径（照抄 Hermes 的 `compression` 段，research 06 §20）

* **阈值 0.50**：上下文用到窗口一半就开始压，而不是等溢出。等溢出再压意味着这一轮
  已经失败了（供应商返回 400），用户看到的是报错而不是一次平滑的压缩；而这次失败
  的往返要花几百毫秒到几秒，纯粹是白等。
* **目标 0.20**：压到窗口的 20%。只压到"刚好够用"会让下一轮工具结果立刻又撑爆，
  来回压缩比直接报错更糟。

两个数字与 `app/session_plane.py` 的 `COMPACTION_THRESHOLD` / `COMPACTION_TARGET_RATIO`
逐字相同，`tests/test_compaction.py` 钉住这件事（复写会漂移，断言不会）。

## 纯函数边界

`plan()` **不调模型、不读库、不写日志**：它只回答"该丢哪几组、保留什么、压完大概多少"。
真正把摘要文本弄出来的是 `nodes.Nodes.summarize`（会走一次模型调用），它失败时退回
`nodes.Nodes.compact_messages` 的确定性截断——**压缩是优化，不该让任务失败**。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

#: 触发压缩的占用比（Hermes ``compression.threshold: 0.5``）。**正好 50% 就压**。
COMPACT_THRESHOLD = 0.50
#: 压缩目标占窗口的比例（Hermes ``compression.target_ratio: 0.2``）。
COMPACT_TARGET_RATIO = 0.20
#: 字符/token 换算比。与 `app/window.py`、`app/policies.py::estimate_tokens` 同口径。
CHARS_PER_TOKEN = 4

#: 压缩产物的前缀，**逐字**照抄 Hermes。`app/session_search.py` 靠它们识别"这是
#: 机器写的交接载荷"并在书挡里剔除；两处字符串一旦不一致，压缩载荷就会借召回
#: 回流进新会话。`app/session_plane.py` 里也有同名常量（同一份字符串的第二个读者）。
COMPACTION_PREFIX = "[CONTEXT COMPACTION"
SUMMARY_PREFIX = "[CONTEXT SUMMARY]:"

#: 摘要本身要占多少 token 的**预估**。只用于"压完大概剩多少"的报数，不参与判定：
#: 摘要长度由模型决定，估不准是正常的；把它当成硬预算会让报告看起来比实际更精确。
SUMMARY_RESERVE_TOKENS = 400

#: 摘要短于这个字符数视为**无效**（模型吐了半句话 / 只回了"好的"）：与其把一句噪声
#: 塞进上下文冒充摘要，不如退回确定性截断——后者至少不会骗模型"这里有总结"。
MIN_SUMMARY_CHARS = 200

#: 送进摘要请求的单条消息字符上限。摘要请求本身也会溢出：把 20 万字符的工具输出
#: 原样塞回去，等于在"压缩"这一步再触发一次 `context_overflow`。
PROMPT_MESSAGE_CHARS = 4_000

#: 摘要指令。写清"只输出正文"与"保留什么"，否则模型会回一份"好的，我已压缩"。
SUMMARY_INSTRUCTION = (
    "你在为一次正在进行的任务压缩上下文。下面几轮历史马上要从上下文里删掉，"
    "它们将被你写的摘要取代。请写一份摘要，要求：\n"
    "1. 保留后续步骤仍然需要的事实：任务目标、已经做过的动作与它们的结论、"
    "关键数据（网址、路径、文件名、报错原文里的关键行）、尚未解决的问题；\n"
    "2. 丢掉过程性噪声：重复的试探、已经失效的元素引用、冗长的日志正文；\n"
    "3. 用条目写，中文，不要编造原文里没有的信息；\n"
    "4. 只输出摘要正文，不要复述这份指令，不要调用任何工具。"
)


# ── token 估算（与 policies 同口径）─────────────────────────────────────────


def estimate_tokens(text: Any) -> int:
    """粗估 token：中文按字、其余按 4 字符 ≈ 1 token。

    与 `app/policies.py::estimate_tokens` 逐字同公式，但**不复用那个函数**：
    `app.policies` 会连带拉起 sqlalchemy 与上下文装配（本模块要能被纯文件命令
    import）。`tests/test_compaction.py` 直接对比两者，防止口径漂移。
    """
    value = text if isinstance(text, str) else str(text or "")
    cjk = sum(1 for char in value if "\u4e00" <= char <= "\u9fff")
    return cjk + max(0, len(value) - cjk) // 4


def message_text(message: Any) -> str:
    """一条消息的**可计费正文**。

    多模态消息的 `content` 是块列表：只算文本块，图片块按占位算——一张图的
    base64 有几十万字符，按字符数算会把它当成 50k token 的历史，压缩判定于是
    永远命中（真实占用由供应商按图算，不按 base64 长度算）。
    """
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, dict) and block.get("type") == "image_url":
                parts.append("[图片]")
        return "".join(parts)
    return str(content or "")


def message_tokens(message: Any) -> int:
    """单条消息的 token 估算（正文 + tool_calls 的参数字面量）。"""
    total = estimate_tokens(message_text(message))
    for call in getattr(message, "tool_calls", None) or []:
        if isinstance(call, dict):
            total += estimate_tokens(str(call.get("name") or ""))
            total += estimate_tokens(str(call.get("args") or ""))
    return total


def tokens_for(messages: Sequence[Any]) -> int:
    """一组消息的 token 估算。"""
    return sum(message_tokens(message) for message in messages)


def is_system(message: Any) -> bool:
    """是不是系统提示。**冻结前缀永远不压**（提示缓存 + 任务内自洽）。"""
    return str(getattr(message, "type", "")) == "system"


# ── 阈值 ────────────────────────────────────────────────────────────────────


def should_compact(used_tokens: Any, window: Any) -> bool:
    """要不要压缩：占用比 **≥ 0.50**。

    边界口径：**正好 50% 就压**（``>=``，不是 ``>``）。"到一半"已经是"再塞一个
    工具结果就可能溢出"的状态，而压缩是廉价且可重放的本地操作；把边界放在 ``>``
    只是把同一件事推迟一轮，代价是那一轮可能直接 400。

    窗口未知（``window <= 0``）**不触发**：算不出比例就不该假装算得出。
    """
    try:
        size = int(window)
        used = int(used_tokens)
    except (TypeError, ValueError):
        return False
    if size <= 0 or used <= 0:
        return False
    return used >= size * COMPACT_THRESHOLD


def target_tokens(window: Any) -> int:
    """压缩后应占多少 token：窗口的 **20%**（向下取整）。

    向下取整而不是四舍五入：目标值只用于"够不够短"的判定，多给一个 token 没有
    收益，少给一个 token 只会让压缩稍微激进一点——后者是安全方向。
    """
    try:
        size = max(0, int(window))
    except (TypeError, ValueError):
        return 0
    return int(size * COMPACT_TARGET_RATIO)


# ── 分组（配对不变量的地基）─────────────────────────────────────────────────


def group_messages(messages: Sequence[Any]) -> list[list[int]]:
    """按"轮次"分组，返回**下标**而不是消息：调用方要拿下标去切原列表。

    一组 = 一条非 `tool` 消息 + 紧随其后的全部 `tool` 结果。

    为什么必须整组丢：`tool_calls` 与 `tool` 结果成对是供应商的硬约束，留下半个
    配对就是 400。按组切是唯一能保证"永不拆配对"的切法（逐条删一定会拆）。

    系统提示**不进组**：它在 `plan()` 里恒被保留（冻结前缀），混进组里就有被
    "整组丢掉"的风险。
    """
    groups: list[list[int]] = []
    for index, message in enumerate(messages):
        if is_system(message):
            continue
        if groups and str(getattr(message, "type", "")) == "tool":
            groups[-1].append(index)
            continue
        groups.append([index])
    return groups


# ── 压缩计划 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CompactionPlan:
    """一次"压不压、怎么压"的判定结果。**纯数据，不含 IO**。

    ``needed=False`` 时其余字段仍然有值（目标、预计剩余），因为调用方常要把
    "现在多少、压了会到多少"一起打出来——判 false 就全变 0 会让报告失去解释力。

    ``kept`` / ``dropped`` / ``summarized`` 都是**下标**（指向传进来的那个列表）：
    ``dropped`` 与 ``summarized`` 现在相同（丢掉的都要进摘要），分开留是因为
    "丢掉"与"送进摘要"是两件事——将来若要"只丢不摘"（比如丢的是图片），
    这两个集合就会分叉。
    """

    needed: bool
    used_tokens: int
    window: int
    target_tokens: int
    kept: tuple[int, ...]
    dropped: tuple[int, ...]
    summarized: tuple[int, ...]
    projected_tokens: int
    groups: int
    reason: str

    @property
    def used_ratio(self) -> float:
        return (self.used_tokens / self.window) if self.window > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "needed": self.needed,
            "used_tokens": self.used_tokens,
            "window": self.window,
            "used_ratio": round(self.used_ratio, 4),
            "target_tokens": self.target_tokens,
            "kept": list(self.kept),
            "dropped": list(self.dropped),
            "summarized": list(self.summarized),
            "projected_tokens": self.projected_tokens,
            "groups": self.groups,
            "reason": self.reason,
        }


def plan(
    messages: Sequence[Any],
    *,
    window: Any,
    target: Any = None,
    carried_tokens: Any = 0,
) -> CompactionPlan:
    """算出"该丢哪几组、保留什么、压完大概剩多少"。**纯函数**。

    ``carried_tokens`` 是"不在 ``messages`` 里、但确实会进上下文"的占用：系统提示
    （冻结前缀）与**已有的压缩摘要**。它们不能进分组（摘要一旦被当成一组，它是最老
    的一组，会在下一轮被优先丢掉——那就等于把整段历史丢第二次）。

    丢弃策略：
    * 系统提示恒保留；
    * 从**最老的整组**开始丢，直到预计占用进目标；
    * **永远保留最后一组**：那是"当前正在做的事"，丢掉它模型就不知道自己刚才在
      干嘛了（与 `nodes._drop_oldest_rounds` 同一条规矩）。
    """
    items = list(messages)
    try:
        carried = max(0, int(carried_tokens))
    except (TypeError, ValueError):
        carried = 0
    try:
        size = max(0, int(window))
    except (TypeError, ValueError):
        size = 0
    total = tokens_for(items) + carried
    goal = target_tokens(size) if target is None else max(0, int(target))
    groups = group_messages(items)
    system_index = tuple(index for index, item in enumerate(items) if is_system(item))
    needed = should_compact(total, size)

    if not needed:
        return CompactionPlan(
            needed=False,
            used_tokens=total,
            window=size,
            target_tokens=goal,
            kept=tuple(range(len(items))),
            dropped=(),
            summarized=(),
            projected_tokens=total,
            groups=len(groups),
            reason=(
                f"已用 {total} token = 窗口的 {total / size:.1%}，未到阈值 "
                f"{COMPACT_THRESHOLD:.0%}"
                if size > 0
                else "窗口未知（context_length 未配置或为 0），不判定压缩"
            ),
        )

    if len(groups) <= 1:
        # 只有一组：丢掉它就是丢掉"当前正在做的事"。此时如实报"压不动"，
        # 让调用方去走别的路（截断正文 / 让供应商报错），而不是假装压过。
        return CompactionPlan(
            needed=True,
            used_tokens=total,
            window=size,
            target_tokens=goal,
            kept=tuple(range(len(items))),
            dropped=(),
            summarized=(),
            projected_tokens=total,
            groups=len(groups),
            reason=(
                f"已用 {total} token = 窗口的 {total / size:.1%}，已到阈值 "
                f"{COMPACT_THRESHOLD:.0%}，但只有一组可折叠的轮次（就是当前这轮），"
                f"压不动；只能截断正文"
            ),
        )

    keep = list(groups)
    while len(keep) > 1:
        kept_tokens = carried + tokens_for(
            [items[index] for group in keep for index in group]
        )
        projected = kept_tokens + (SUMMARY_RESERVE_TOKENS if len(keep) < len(groups) else 0)
        if projected <= goal:
            break
        keep.pop(0)

    kept_set = set(system_index)
    for group in keep:
        kept_set.update(group)
    dropped = tuple(index for index in range(len(items)) if index not in kept_set)
    projected = carried + tokens_for([items[index] for index in sorted(kept_set)])
    if dropped:
        projected += SUMMARY_RESERVE_TOKENS
    return CompactionPlan(
        needed=True,
        used_tokens=total,
        window=size,
        target_tokens=goal,
        kept=tuple(sorted(kept_set)),
        dropped=dropped,
        summarized=dropped,
        projected_tokens=projected,
        groups=len(groups),
        reason=(
            f"已用 {total} token = 窗口的 {total / size:.1%}，达到阈值 "
            f"{COMPACT_THRESHOLD:.0%}；折叠最老的 {len(dropped)} 条消息后约 "
            f"{projected} token（目标 {goal}）"
        ),
    )


# ── 摘要产物 ────────────────────────────────────────────────────────────────


def render_history(messages: Sequence[Any], *, limit: int = PROMPT_MESSAGE_CHARS) -> str:
    """把待压缩的几轮渲染成摘要请求里的正文（逐条截断，防止请求本身溢出）。"""
    labels = {"human": "用户", "ai": "助手", "tool": "工具结果", "system": "系统"}
    lines: list[str] = []
    for message in messages:
        kind = str(getattr(message, "type", "unknown"))
        text = message_text(message)
        if len(text) > limit:
            head = limit * 2 // 3
            tail = limit - head
            text = f"{text[:head]}\n…[此处省略 {len(text) - limit} 字符]…\n{text[-tail:]}"
        label = labels.get(kind, kind)
        call_id = getattr(message, "tool_call_id", "")
        if call_id:
            label = f"{label}（{call_id}）"
        lines.append(f"[{label}] {text}")
    return "\n\n".join(lines)


def summary_prompt(messages: Sequence[Any], *, prior: str = "") -> str:
    """组装摘要请求的正文。

    ``prior`` 是**上一份摘要**：这次折叠的是它后面的历史，新摘要必须与它合并
    （否则每压一次就丢一段更早的历史，任务越跑越失忆）。
    """
    parts = [SUMMARY_INSTRUCTION]
    if prior:
        parts.append(
            "已有摘要（它覆盖更早的历史；请把下面几轮与它合并成**一份**摘要，"
            "仍然有效的事实不要丢）：\n" + prior
        )
    parts.append("需要压缩的这几轮：\n" + render_history(messages))
    parts.append("请只输出合并后的摘要正文。")
    return "\n\n".join(parts)


def summary_message(
    digest: str,
    *,
    folded: int,
    window: int = 0,
    target: int = 0,
) -> str:
    """把摘要正文包成一条**会进上下文的载荷**，前缀逐字照 Hermes。

    两个前缀都要出现：
    * ``[CONTEXT COMPACTION`` 在头部——`session_search` 靠它识别"机器写的交接载荷"；
    * ``[CONTEXT SUMMARY]:`` 在正文前——同一件事的第二个读者（书挡剔除）用它。
    """
    head = (
        f"{COMPACTION_PREFIX} 任务内压缩：{folded} 条历史已折叠，原文不再在上下文里]"
    )
    if window:
        head = head.replace("]", f" · 窗口 {window} · 目标 {target}]", 1)
    body = str(digest or "").strip()
    return f"{head}\n{SUMMARY_PREFIX}\n{body}"


def is_summary_payload(text: Any) -> bool:
    """这段正文是不是压缩产物（供排查与"别把摘要再摘一次"的判断）。"""
    value = str(text or "").lstrip()
    return value.startswith(COMPACTION_PREFIX) or value.startswith(SUMMARY_PREFIX)


def usable_summary(text: Any) -> bool:
    """摘要能不能用：够长、且不是一句"好的"。

    判据放在这里而不是 nodes：调用方（`Nodes.summarize`）只该问"这段能用吗"，
    不该各自定一个长度阈值——两处阈值不同就会出现"这里算有效、那里算无效"。
    """
    body = str(text or "").strip()
    if len(body) < MIN_SUMMARY_CHARS:
        return False
    return body.lower() not in {"none", "ok", "好的", "无"}


__all__ = [
    "CHARS_PER_TOKEN",
    "COMPACTION_PREFIX",
    "COMPACT_TARGET_RATIO",
    "COMPACT_THRESHOLD",
    "MIN_SUMMARY_CHARS",
    "PROMPT_MESSAGE_CHARS",
    "SUMMARY_INSTRUCTION",
    "SUMMARY_PREFIX",
    "SUMMARY_RESERVE_TOKENS",
    "CompactionPlan",
    "estimate_tokens",
    "group_messages",
    "is_summary_payload",
    "is_system",
    "message_text",
    "message_tokens",
    "plan",
    "render_history",
    "should_compact",
    "summary_message",
    "summary_prompt",
    "target_tokens",
    "tokens_for",
    "usable_summary",
]
