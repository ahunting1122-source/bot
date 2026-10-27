"""长期记忆的提示词渲染：字符预算 + 用量自曝 + 溢出裁剪。

对标 Hermes 的 `MEMORY.md`（上限 2,200 字符，**按字符不按 token**）。为什么是字符：

- 按字符算与模型无关。换模型（分词器变了）不该让"我的记忆还能装多少"跟着变。
- 字符是**可解释的**：用户看到 `[99% — 2,181/2,200 字符]` 能立刻判断"该整理了"；
  看到 `[87% of 1,300 tokens]` 只能相信系统。

## 为什么要自曝用量

Hermes 的实测值：本机 `MEMORY.md` 已到 **98.7%**、`USER.md` 到 **101.3%（超顶）**，
而提示词里如果没有这个数字，模型会一直往里加，直到写不进去才发现。把用量摆在记忆块
的标题行，模型每轮都能看到"还剩多少"，用户的整理动作也有了触发点。

## 为什么裁剪要"从头丢"

新写入的记忆在尾部（`memories` 表按 `updated_at` 倒序取，这里再反转成"旧→新"）。
超预算时**从最旧的开始丢**：刚记住的东西被丢，用户立刻会发现；丢最旧的，损失最小。
丢掉的条目会在标题行里如实报数量——静默丢数据是最坏的选项。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 记忆正文的字符上限。对齐 Hermes 的 `MEMORY.md` 默认 2,200。
MEMORY_MAX_CHARS = 2200

#: 标题行模板。`{pct}` 用整数百分比，`{used}`/`{limit}` 用字符数。
HEADER = "长期记忆（MEMORY）[{pct}% — {used}/{limit} 字符]"

#: 一条记忆渲染成什么样。`key: value` 与 Orbit 既有格式一致（测试与提示词都认它）。
LINE = "- {key}: {value}"

#: 单条 value 的截断长度。超长的 value 会把整块预算吃光，只剩一条记忆可用。
VALUE_LIMIT = 600


@dataclass
class MemoryBlock:
    """渲染结果。`dropped` 是**因预算被裁掉**的条数，必须如实报出去。"""

    text: str
    used: int
    limit: int
    total: int
    dropped: int = 0
    entries: list[str] = field(default_factory=list)

    @property
    def percent(self) -> int:
        return int(round(self.used / self.limit * 100)) if self.limit else 0


def render_entry(key: str, value: str) -> str:
    """把一条记忆渲染成一行。空 key 直接丢掉（渲染出来是 `- : xxx`，没有信息量）。"""
    name = (key or "").strip()
    if not name:
        return ""
    body = " ".join((value or "").split())
    if len(body) > VALUE_LIMIT:
        body = body[:VALUE_LIMIT] + "…"
    return LINE.format(key=name, value=body)


def render(
    pairs: list[tuple[str, str]],
    *,
    limit: int = MEMORY_MAX_CHARS,
) -> MemoryBlock:
    """渲染记忆块，超预算时**从最旧的开始裁**。

    `pairs` 的顺序约定：**旧 → 新**。调用方负责排好；这里不做排序，
    因为"新旧"的判据属于存储层（`updated_at`），渲染层不该猜。
    """
    rendered = [line for line in (render_entry(k, v) for k, v in pairs) if line]
    total = len(rendered)
    if not rendered:
        return MemoryBlock(text="", used=0, limit=limit, total=0)

    def header_for(used: int) -> str:
        percent = int(round(used / limit * 100)) if limit else 0
        return HEADER.format(pct=percent, used=used, limit=limit)

    def assemble(kept: list[str], dropped: int) -> tuple[str, int]:
        """拼出最终文本，并让标题里的用量**等于整块的真实长度**。

        这里必须迭代：标题里写着用量，而标题本身也占长度——写死一个估算值会让
        标题说 2,181、实际 2,205，用户按这个数字判断"还剩多少"就会判断错。
        标题长度只在位数变化时跳一下，几轮就收敛。
        """
        lines = "\n".join(kept)
        notice = f"（因预算省略 {dropped} 条更早的记忆）" if dropped else ""
        body = len(lines) + (len(notice) + 1 if notice else 0)
        used = body
        for _ in range(8):
            header = header_for(used)
            candidate = len(header) + 1 + body
            if candidate == used:
                break
            used = candidate
        header = header_for(used)
        text = header + "\n" + lines + (("\n" + notice) if notice else "")
        return text, len(text)

    kept = list(rendered)
    dropped = 0
    while True:
        text, used = assemble(kept, dropped)
        if used <= limit:
            return MemoryBlock(
                text=text, used=used, limit=limit, total=total, dropped=dropped, entries=kept
            )
        if len(kept) > 1:
            # 丢最旧的一条。丢最新的会让用户以为"刚记的没生效"。
            kept.pop(0)
            dropped += 1
            continue
        # 只剩一条还是超：截断这一条本身。不截的话"至少留一条"会把预算撑破，
        # 而撑破预算的后果是整块被上游裁掉——那时连一条记忆都不剩。
        over = used - limit
        line = kept[0]
        if len(line) <= over + 3:
            return MemoryBlock(
                text="", used=0, limit=limit, total=total, dropped=total, entries=[]
            )
        kept[0] = line[: len(line) - over - 3] + "…"


def render_block(pairs: list[tuple[str, str]], *, limit: int = MEMORY_MAX_CHARS) -> str:
    """只要文本的便捷入口。空记忆返回空串（不占提示词预算）。"""
    return render(pairs, limit=limit).text
