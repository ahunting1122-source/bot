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

#: 用户画像（USER.md）的标题行模板。**与 MEMORY 用同一个模板形状**是刻意的：
#: 两块预算的读数必须能横向比较（"记忆 98%、画像 101%"一眼就懂），
#: 而两种不同版式的百分比会让人以为它们量的不是同一件事。
USER_HEADER = "用户画像（USER）[{pct}% — {used}/{limit} 字符]"

#: 一条记忆渲染成什么样。`key: value` 与 Orbit 既有格式一致（测试与提示词都认它）。
LINE = "- {key}: {value}"

#: 一条画像条目渲染成什么样。与 `app.profile.render_block` 的 `§ 条目` 完全一致——
#: 画像正文的真值是 `data/profile/USER.md`，渲染层换个符号就等于换了格式。
USER_LINE = "§ {entry}"

#: 超预算的省略提示。两个 store 分开写，因为"省略了 3 条记忆"和"省略了 3 条画像条目"
#: 是两种不同的损失，用同一句话会让用户搞不清丢的是哪一块。
NOTICE = "（因预算省略 {dropped} 条更早的记忆）"
USER_NOTICE = "（因预算省略 {dropped} 条更早的画像条目）"

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


def format_header(used: int, limit: int, template: str = HEADER) -> str:
    """按模板拼标题行。百分比取整（`[99% — ...]`，不写 98.7%）。

    为什么单独抽出来：`budget` 类命令在**空块**时也要报一行 `[0% — 0/1600 字符]`，
    而空块走的是"整段不出现"的分支（见 `render`）。两处各写一遍格式化，
    迟早会出现"有内容时 99%、没内容时 0.0%"这种口径不一致。
    """
    percent = int(round(used / limit * 100)) if limit else 0
    return template.format(pct=percent, used=used, limit=limit)


def render_entry(key: str, value: str) -> str:
    """把一条记忆渲染成一行。空 key 直接丢掉（渲染出来是 `- : xxx`，没有信息量）。"""
    name = (key or "").strip()
    if not name:
        return ""
    body = " ".join((value or "").split())
    if len(body) > VALUE_LIMIT:
        body = body[:VALUE_LIMIT] + "…"
    return LINE.format(key=name, value=body)


def render_user_entry(entry: str) -> str:
    """把一条画像条目渲染成一行。空条目丢掉（`§ ` 后面什么都没有，纯噪声）。"""
    body = " ".join((entry or "").split())
    if not body:
        return ""
    if len(body) > VALUE_LIMIT:
        body = body[:VALUE_LIMIT] + "…"
    return USER_LINE.format(entry=body)


def _render(
    items: list,
    *,
    limit: int,
    header: str,
    formatter,
    notice: str,
) -> MemoryBlock:
    """两块预算共用的渲染引擎：标题自曝用量 + 超预算从最旧开始裁。

    `header` / `formatter` / `notice` 是参数而不是分支：MEMORY 与 USER 的差别**只有**
    这三处文案与行格式，裁剪算法、标题收敛、丢最旧的策略完全相同。写成两个函数会有
    两份裁剪循环，而裁剪方向（丢最旧）正是最容易被改歪、也最容易被漏测的一处。
    """
    rendered = [line for line in (formatter(item) for item in items) if line]
    total = len(rendered)
    if not rendered:
        return MemoryBlock(text="", used=0, limit=limit, total=0)

    def header_for(used: int) -> str:
        return format_header(used, limit, header)

    def assemble(kept: list[str], dropped: int) -> tuple[str, int]:
        """拼出最终文本，并让标题里的用量**等于整块的真实长度**。

        这里必须迭代：标题里写着用量，而标题本身也占长度——写死一个估算值会让
        标题说 2,181、实际 2,205，用户按这个数字判断"还剩多少"就会判断错。
        标题长度只在位数变化时跳一下，几轮就收敛。
        """
        lines = "\n".join(kept)
        warning = notice.format(dropped=dropped) if dropped else ""
        body = len(lines) + (len(warning) + 1 if warning else 0)
        used = body
        for _ in range(8):
            head = header_for(used)
            candidate = len(head) + 1 + body
            if candidate == used:
                break
            used = candidate
        head = header_for(used)
        text = head + "\n" + lines + (("\n" + warning) if warning else "")
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


def render(
    pairs: list[tuple[str, str]],
    *,
    limit: int = MEMORY_MAX_CHARS,
) -> MemoryBlock:
    """渲染记忆块，超预算时**从最旧的开始裁**。

    `pairs` 的顺序约定：**旧 → 新**。调用方负责排好；这里不做排序，
    因为"新旧"的判据属于存储层（`updated_at`），渲染层不该猜。
    """
    return _render(
        list(pairs),
        limit=limit,
        header=HEADER,
        formatter=lambda pair: render_entry(pair[0], pair[1]),
        notice=NOTICE,
    )


def render_block(pairs: list[tuple[str, str]], *, limit: int = MEMORY_MAX_CHARS) -> str:
    """只要文本的便捷入口。空记忆返回空串（不占提示词预算）。"""
    return render(pairs, limit=limit).text


def render_user(
    entries: list[str],
    limit: int | None = None,
) -> MemoryBlock:
    """渲染用户画像块（USER.md），带与 MEMORY 同口径的用量自曝头。

    MATRIX 9.2：MEMORY 早就有 `[99% — 2,181/2,200 字符]` 头，USER 一直没有。
    后果是模型只看得到记忆还剩多少，看不到画像还剩多少——于是它会在画像上
    **写满才发现写不进去**（本机 Hermes 的 USER.md 实测已到 101.3% 超顶）。
    两块预算同源（都是"每次会话整段注入"），读数就该同一个版式。

    `limit` 默认取 `profile.PROFILE_MAX_CHARS`（1,600）。为什么用**延迟导入**而不是
    在文件头 import：`profile` 将来若要复用本模块的渲染，模块级互相 import 会成环；
    一个常量不值得冒这个风险。
    """
    if limit is None:
        from app.profile import PROFILE_MAX_CHARS

        limit = PROFILE_MAX_CHARS
    return _render(
        list(entries),
        limit=int(limit),
        header=USER_HEADER,
        formatter=render_user_entry,
        notice=USER_NOTICE,
    )


def render_user_block(entries: list[str], limit: int | None = None) -> str:
    """只要文本的便捷入口（空画像返回空串，不占提示词预算）。"""
    return render_user(entries, limit).text
