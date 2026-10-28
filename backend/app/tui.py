"""终端 UI（MATRIX 1.16）：三栏布局 + 最小按键循环 + **真实数据的只读刷新**。

## 诚实边界（动这里之前先读）

这不是 Hermes 那个 TUI。Hermes 的 ``--tui`` 是 prompt_toolkit 的全屏应用：
多行编辑、历史、补全、鼠标、异步流式刷新、真 PTY。本机**无网络、装不了任何 TUI 库**
（``textual`` / ``prompt_toolkit`` 都不在依赖里，也不许装），所以这里只做
"能看能走"的最小实现，并且刻意把边界写在代码里：

- **看得见**：清屏、光标定位、画框、横竖线（ANSI 转义）；``NO_COLOR`` 或流不是 TTY
  时**整体降级成纯文本**（不输出任何转义序列）。
- **走得动**：``q`` 退出、``Tab`` 切栏、``j`` / ``k`` 上下移动、``r`` 立刻重读数据、
  ``/`` 过滤、``?`` 帮助。支持的按键全部列在 :data:`HELP_LINES` 里。
- **有真实数据（只读）**：:func:`load_from_store` 从 ``store`` 读会话列表 / 任务状态 /
  最近事件，三栏格子里画的就是它们。**只 SELECT，从不写库**——看界面不该改数据。
- **有刷新**：:meth:`TuiApp.refresh` 按注入的 ``clock`` 决定要不要重读，读数据的动作
  由注入的 ``loader`` 完成。测试用假 clock + 假 loader 驱动，**不 sleep、不碰真库**。

**没做的**（照 Hermes 差得还远）：没有鼠标、没有富文本/颜色主题、没有多行编辑与
历史补全、没有真 PTY 与窗口缩放事件、没有横向滚动。``r`` 之前的"自动刷新"也只在
**按键之间**发生（没有 ``select``/PTY 就读不到"空闲"事件）——真 TTY 上用 ``msvcrt``
轮询兜住这一点，非 Windows 平台退化成"按键时才刷新"。这些都不假装做了。

## 为什么画框用 ASCII ``+ - |`` 而不是 ``┌─┐``

Unicode 制表符（U+2500 区）在东亚宽度里是 **Ambiguous**：CJK 终端可能按 2 列渲染。
一旦被渲染成 2 列，边框就和上面算好的矩形错位，整个界面歪掉。ASCII 三个字符宽度
恒为 1，代价是朴素——朴素但不错位，比好看但错位值钱。

## 为什么布局与渲染分开

``panes()`` 只算矩形，``render()`` 只把矩形与行文本画成字符画，两者都不碰终端。
于是测试可以断言"每行宽度正好是 width、行数正好是 height"，不需要伪终端；
``TuiApp`` 再把 stdin/stdout/clock/loader 全做成可注入参数，测试用 ``io.StringIO``
喂按键、用假 clock 喂时间，**一个真终端、一次真 sleep 都不用**。
"""

from __future__ import annotations

import os
import sys
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

#: 三栏的键与顺序。顺序 = ``Tab`` 的切换顺序 = 左右排列顺序。
PANE_KEYS: tuple[str, ...] = ("sessions", "conversation", "status")

#: 每栏的中文标题。
PANE_TITLES: dict[str, str] = {
    "sessions": "会话",
    "conversation": "对话",
    # 标题刻意短：右栏最窄只有 20 列，`[ 状态·轨迹 ]` 刚好塞得下；
    # 再长就会触发"标题放不下就整行退化成一排 -"，那栏看起来就像没标题。
    "status": "状态·轨迹",
}

#: 画布最小尺寸。再小就没法同时放下三栏边框，算出来的矩形会是负宽度。
MIN_WIDTH = 32
MIN_HEIGHT = 8

#: 左右两栏的起步宽度与地板宽度（宽度不够时按地板收缩，把地方让给中间）。
LEFT_MIN, LEFT_FLOOR = 18, 10
RIGHT_MIN, RIGHT_FLOOR = 20, 10
#: 中间栏（对话）的最小宽度——它才是主场，不能被挤成一条缝。
CENTER_MIN = 12

#: 帮助块的高度（= :data:`HELP_LINES` 的行数），布局要用它预留空间。
HELP_LINES: tuple[str, ...] = (
    "帮助（? 关闭）",
    "  q      退出",
    "  Tab    切换焦点栏（会话 → 对话 → 状态）",
    "  j / k  在当前栏内向下 / 向上移动",
    "  r      立刻重读数据并重画",
    "  /      过滤：输入关键词，回车确认，Esc 取消",
    "  ?      显示 / 隐藏这份帮助",
)

#: 每栏的**占位**行数：没有 loader（或 loader 还没读出东西）时用它，
#: 只为了让 ``j`` / ``k`` 有东西可走。有真实数据时行数来自数据本身。
DEFAULT_COUNTS: dict[str, int] = {"sessions": 6, "conversation": 8, "status": 5}

#: 每栏内部的第一行提示（**只在没有数据时**出现）。写清楚"这里为什么是空的"，
#: 免得看图的人以为"库里真的没有会话"。
PANE_HINTS: dict[str, str] = {
    "sessions": "（无数据：接上 store 后这里是会话列表）",
    "conversation": "（无数据：这里是最近事件）",
    "status": "（无数据：这里是任务状态）",
}

#: 一次快照各栏最多取多少行。**有上限是有意的**：TUI 是"看全"视图，不是"读全文"
#: 视图（读全文有 ``orbit session-search read``）；而且每帧重画，行数直接等于渲染成本。
SESSIONS_LIMIT = 20
TASKS_LIMIT = 20
EVENTS_LIMIT = 30


# ── 宽度 ────────────────────────────────────────────────────────────────────


def char_width(char: str) -> int:
    """单个字符的终端显示宽度。CJK 全角占 2 列，不算就会让边框错位。"""
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def display_width(text: str) -> int:
    """整串的显示宽度。"""
    return sum(char_width(char) for char in text)


def fit(text: str, width: int) -> str:
    """把一行裁/补到**显示宽度**正好 ``width``。

    按宽度裁而不是按 ``len`` 裁：``"会话"`` 是 2 个字符但占 4 列，
    按字符数裁会让中文标题把右边框顶出去。
    """
    if width <= 0:
        return ""
    if display_width(text) <= width:
        return text + " " * (width - display_width(text))
    out: list[str] = []
    used = 0
    for char in text:
        step = char_width(char)
        if used + step > width - 1:
            break
        out.append(char)
        used += step
    clipped = "".join(out)
    return clipped + "…" + " " * max(0, width - used - 1)


# ── Screen：ANSI 终端原语 ───────────────────────────────────────────────────


def ansi_enabled(stream: Any = None) -> bool:
    """这个流能不能用 ANSI 转义。

    两条判据，任一不满足就降级：

    1. ``NO_COLOR`` 环境变量**存在**即禁用（按 NO_COLOR 规范，空串也算"设了"）；
    2. 流必须自称 TTY——管道、文件、``StringIO`` 都拿不到光标定位能力，
       硬发转义序列只会在日志里留下乱码。
    """
    if "NO_COLOR" in os.environ:
        return False
    target = stream if stream is not None else sys.stdout
    try:
        return bool(target.isatty())
    except Exception:  # noqa: BLE001 —— 奇怪的流（已关闭 / 无 isatty）一律当非 TTY
        return False


def box_lines(width: int, height: int, title: str = "") -> list[str]:
    """纯文本画框（ASCII）。``ansi=False`` 时直接用它，测试也断言它。

    标题嵌在顶边里：``+[ 会话 ]-----+``。标题太长就整行退化成一排 ``-``
    ——宁可没有标题，也不要把顶边撑歪。
    """
    if width < 2 or height < 2:
        return []
    inner = width - 2
    label = f"[ {title} ]" if title else ""
    if label and display_width(label) <= inner:
        top = "+" + label + "-" * (inner - display_width(label)) + "+"
    else:
        top = "+" + "-" * inner + "+"
    middle = "|" + " " * inner + "|"
    bottom = "+" + "-" * inner + "+"
    return [top] + [middle] * (height - 2) + [bottom]


class Screen:
    """ANSI 终端画布原语：清屏、光标定位、画框、横线、竖线。

    ``ansi=False``（``NO_COLOR`` / 非 TTY / 显式指定）时**所有方法都不含转义序列**：
    清屏与定位退化成空串，画框退化成多行纯文本。降级不是"少一点颜色"，
    而是"一行转义序列都不许出现"——否则重定向到文件就会写出乱码。
    """

    def __init__(self, stream: Any = None, *, ansi: bool | None = None) -> None:
        self.stream = stream if stream is not None else sys.stdout
        self.ansi = ansi_enabled(self.stream) if ansi is None else bool(ansi)
        self._pending: list[str] = []

    def clear(self) -> str:
        """清屏并把光标移到左上角。"""
        return "\x1b[2J\x1b[H" if self.ansi else ""

    def reset(self) -> str:
        """复位 SGR（颜色/样式）。降级时空串。"""
        return "\x1b[0m" if self.ansi else ""

    def move(self, y: int, x: int) -> str:
        """光标定位到 ``(y, x)``，**1 起算**（ANSI 的 CUP 就是从 1 开始）。"""
        if not self.ansi:
            return ""
        return f"\x1b[{max(1, y)};{max(1, x)}H"

    def hline(self, y: int, x: int, length: int, char: str = "-") -> str:
        """横线。降级时只剩字符本身（调用方负责换行）。"""
        body = char * max(0, length)
        return (self.move(y, x) + body) if self.ansi else body

    def vline(self, x: int, y: int, length: int, char: str = "|") -> str:
        """竖线。ANSI 模式下逐行定位；降级时按行拼接。"""
        if length <= 0:
            return ""
        if not self.ansi:
            return "\n".join(char for _ in range(length))
        return "".join(self.move(y + index, x) + char for index in range(length))

    def box(self, rect: Rect, title: str = "") -> str:
        """按矩形画一个框。ANSI 模式每行自带定位，降级模式用换行分隔。"""
        lines = box_lines(rect.width, rect.height, title)
        if not lines:
            return ""
        if not self.ansi:
            return "\n".join(lines)
        return "".join(
            self.move(rect.y + index + 1, rect.x + 1) + line for index, line in enumerate(lines)
        )

    def write(self, text: str) -> None:
        """把一段输出塞进缓冲（:meth:`flush` 才真正写出去）。"""
        self._pending.append(text)

    def flush(self) -> str:
        """把缓冲写到流上并返回写出去的内容（测试可直接断言返回值）。"""
        payload = "".join(self._pending)
        self._pending.clear()
        if payload:
            self.stream.write(payload)
            try:
                self.stream.flush()
            except Exception:  # noqa: BLE001 —— StringIO / 已关闭流 flush 失败不该让 UI 崩
                pass
        return payload


# ── 数据（只读快照）─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TuiData:
    """一帧要显示的真实数据快照。**只读**：TUI 从不写库。

    三个字段就是三栏：``sessions`` → 左栏、``events`` → 中栏、``tasks`` → 右栏。
    存 ``dict`` 而不是 ORM 行：快照要能跨事件循环传递（loader 可能在别的循环里
    读出来），ORM 对象绑着 session/loop，传过去就是隐患。
    """

    sessions: tuple[dict[str, Any], ...] = ()
    tasks: tuple[dict[str, Any], ...] = ()
    events: tuple[dict[str, Any], ...] = ()
    #: 这份快照是什么时候读出来的（epoch 秒，由调用方的 clock 给）。
    loaded_at: float = 0.0

    def is_empty(self) -> bool:
        return not (self.sessions or self.tasks or self.events)


def _short(value: Any, limit: int) -> str:
    """压成一行短文本：换行折成空格、超长截断。"""
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _clock_time(value: Any) -> str:
    """ISO 时间串 → ``HH:MM:SS``。

    为什么只留时分秒：一行里已经有"事件类型"这个更重要的字段，而 ISO 全串
    （``2026-01-01T00:00:00+00:00``，25 字符）在 20 列的栏里会把类型挤出去。
    拿不到标准格式就原样截短——**猜不出时间就显示原文**，不编一个时间出来。
    """
    text = str(value or "")
    if len(text) >= 19 and text[10] == "T":
        return text[11:19]
    return _short(text, 8)


def pane_rows(data: TuiData) -> dict[str, tuple[str, ...]]:
    """把快照摊成三栏的行文本。**纯函数**，测试直接断言它。

    行文本刻意做得短：每栏最窄只有 10 列，长句会被 ``fit()`` 裁掉一半，
    不如一开始就把"这条是什么"放在最前面（时间/状态在前，正文在后）。
    """
    return {
        "sessions": tuple(
            f"{_short(item.get('title'), 24)} · {_short(item.get('updated_at'), 16)}"
            for item in data.sessions
        ),
        "conversation": tuple(
            f"{_clock_time(item.get('created_at'))} {_short(item.get('event_type'), 18)}"
            for item in data.events
        ),
        "status": tuple(
            f"{_short(item.get('status'), 12)} {_short(item.get('goal'), 20)}"
            for item in data.tasks
        ),
    }


async def load_from_store(
    store: Any,
    *,
    sessions: int = SESSIONS_LIMIT,
    tasks: int = TASKS_LIMIT,
    events: int = EVENTS_LIMIT,
) -> TuiData:
    """从 ``store`` 读一份只读快照：会话列表 / 任务状态 / 最近事件。

    **只 SELECT，不写库**（也没有 ``store.save`` / ``transition`` 的调用）：看界面
    不该改数据，更不该因为"打开了一个界面"而改动 ``updated_at``。

    三个查询各自 ``limit``：TUI 是"看全"视图，全表拉进内存再画前 20 行是纯浪费，
    而且库大了之后每帧重画会卡。

    延迟 import（``sqlalchemy`` / ``app.storage``）：布局与渲染必须能在**没有任何
    依赖**的情况下被 import（``NO_COLOR`` 快照、单测、文档生成都用得到），只有真读
    库的那一步才把 ORM 拉起来。
    """
    from sqlalchemy import select

    from app.storage import Conversation, Task, TaskEvent

    conversations = await store.rows(
        select(Conversation).order_by(Conversation.updated_at.desc()).limit(max(1, sessions))
    )
    task_rows = await store.rows(
        select(Task).order_by(Task.updated_at.desc()).limit(max(1, tasks))
    )
    event_rows = await store.rows(
        select(TaskEvent).order_by(TaskEvent.id.desc()).limit(max(1, events))
    )
    return TuiData(
        sessions=tuple(
            {
                "id": row.id,
                "title": row.title,
                "updated_at": row.updated_at,
                "summary_status": getattr(row, "summary_status", ""),
            }
            for row in conversations
        ),
        tasks=tuple(
            {
                "id": row.id,
                "status": row.status,
                "goal": row.goal,
                "updated_at": row.updated_at,
            }
            for row in task_rows
        ),
        events=tuple(
            {
                "task_id": row.task_id,
                "event_type": row.event_type,
                "created_at": row.created_at,
            }
            for row in event_rows
        ),
        loaded_at=time.time(),
    )


class StoreFeed:
    """把**异步** store 读成**同步**快照，喂给同步按键循环。

    为什么需要这一层：``Store`` 是 async（aiosqlite），而按键循环是同步的（没有
    asyncio 也能跑，这是"能看能走"的前提）。所以拆成两半——有事件循环的那一侧
    ``await refresh()`` 更新快照，UI 那一侧只读缓存（:meth:`__call__` 就是 loader）。

    用法：

        feed = StoreFeed(store)
        await feed.refresh()          # 在事件循环里
        app = TuiApp(loader=feed, refresh_interval=2.0)

    读失败不抛：把原因记在 ``last_error`` 并保留上一份快照——界面上一帧的旧数据
    比一片空白有用（用户至少知道刚才是什么样），而且 TUI 不该因为一次读库失败就崩。
    """

    def __init__(self, store: Any) -> None:
        self.store = store
        self.data = TuiData()
        self.last_error = ""
        self.refreshes = 0

    async def refresh(self) -> TuiData:
        """重读一次快照。失败时保留旧数据并记下原因。"""
        try:
            self.data = await load_from_store(self.store)
            self.last_error = ""
            self.refreshes += 1
        except Exception as exc:  # noqa: BLE001 —— 读库失败不该让界面崩
            self.last_error = f"{type(exc).__name__}: {exc}"
        return self.data

    def __call__(self) -> TuiData:
        return self.data


# ── 布局 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Rect:
    """一个矩形区域。坐标 **0 起算**（内部换算成 ANSI 的 1 起算在 :class:`Screen`）。"""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def as_dict(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass
class Layout:
    """三栏布局：**只算矩形，不含内容**。"""

    width: int
    height: int
    panes: dict[str, Rect]
    active: str = "sessions"
    help_visible: bool = False
    #: 帮助块占的区域（``help_visible=False`` 时为 None）。
    help_rect: Rect | None = None
    #: 每栏的光标行号（0 起算）。
    cursors: dict[str, int] = field(default_factory=dict)
    #: 每栏的行数（有真实数据时来自数据，否则是占位行数）。
    counts: dict[str, int] = field(default_factory=dict)
    #: 当前过滤词（空串 = 不过滤）。渲染时显示在底部状态栏。
    filter: str = ""

    def rect(self, key: str) -> Rect:
        return self.panes[key]

    def as_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "active": self.active,
            "help_visible": self.help_visible,
            "panes": {key: rect.as_dict() for key, rect in self.panes.items()},
            "help": self.help_rect.as_dict() if self.help_rect else None,
            "cursors": dict(self.cursors),
            "counts": dict(self.counts),
            "filter": self.filter,
        }


def panes(
    width: int,
    height: int,
    *,
    active: str = "sessions",
    help_visible: bool = False,
    cursors: dict[str, int] | None = None,
    counts: dict[str, int] | None = None,
    filter: str = "",
) -> Layout:
    """算三栏布局：左会话、中对话、右状态；底部一行状态栏，帮助块压在它上面。

    宽度分配：左右按比例起步，中间拿剩下的；中间被挤到 ``CENTER_MIN`` 以下时，
    先收缩右栏、再收缩左栏（对话是主场，最后才动它）。
    """
    if width < MIN_WIDTH or height < MIN_HEIGHT:
        raise ValueError(
            f"终端太小：需要至少 {MIN_WIDTH}x{MIN_HEIGHT}，拿到 {width}x{height}"
            "（再小三栏边框会算成负宽度）"
        )
    if active not in PANE_KEYS:
        raise ValueError(f"未知焦点栏 {active!r}，可选：{', '.join(PANE_KEYS)}")

    help_height = len(HELP_LINES) if help_visible else 0
    # 底部一行状态栏（按键提示），帮助块在它上面。
    body_height = max(0, height - 1 - help_height)

    left = max(LEFT_MIN, width // 5)
    right = max(RIGHT_MIN, width // 4)
    if width - left - right < CENTER_MIN:
        overflow = CENTER_MIN - (width - left - right)
        give = min(overflow, max(0, right - RIGHT_FLOOR))
        right -= give
        overflow -= give
        if overflow > 0:
            left -= min(overflow, max(0, left - LEFT_FLOOR))
    center = width - left - right

    rects = {
        "sessions": Rect(x=0, y=0, width=left, height=body_height),
        "conversation": Rect(x=left, y=0, width=center, height=body_height),
        "status": Rect(x=left + center, y=0, width=right, height=body_height),
    }
    help_rect = (
        Rect(x=0, y=height - 1 - help_height, width=width, height=help_height)
        if help_visible
        else None
    )
    return Layout(
        width=width,
        height=height,
        panes=rects,
        active=active,
        help_visible=help_visible,
        help_rect=help_rect,
        cursors={key: max(0, (cursors or {}).get(key, 0)) for key in PANE_KEYS},
        counts={key: max(1, (counts or {}).get(key, DEFAULT_COUNTS[key])) for key in PANE_KEYS},
        filter=str(filter or ""),
    )


# ── 渲染 ────────────────────────────────────────────────────────────────────


class _Canvas:
    """定宽字符画布。宽字符占两格：第二格留空串，于是 ``display_width`` 仍然对得上。"""

    def __init__(self, width: int, height: int) -> None:
        self.width = max(1, width)
        self.height = max(1, height)
        self._rows: list[list[str]] = [[" "] * self.width for _ in range(self.height)]

    def put(self, x: int, y: int, text: str) -> None:
        if not 0 <= y < self.height:
            return
        col = x
        for char in text:
            step = char_width(char)
            if col >= self.width:
                break
            self._rows[y][col] = char
            if step == 2 and col + 1 < self.width:
                self._rows[y][col + 1] = ""
            col += step

    def box(self, rect: Rect, title: str = "") -> None:
        for index, line in enumerate(box_lines(rect.width, rect.height, title)):
            self.put(rect.x, rect.y + index, line)

    def render(self) -> str:
        return "\n".join("".join(row) for row in self._rows)


def _pane_body(
    key: str,
    rect: Rect,
    layout: Layout,
    rows: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    """某栏的内部行（不含边框）。

    有真实数据就画数据行（第一行就是数据，不占一行提示），没有数据才退回
    "一行提示 + 若干占位行"——占位行的唯一作用是把 ``j`` / ``k`` 的移动演示出来。
    """
    interior_height = rect.height - 2
    interior_width = rect.width - 2
    if interior_height <= 0 or interior_width <= 0:
        return []
    data = tuple((rows or {}).get(key) or ())
    lines = [] if data else [fit(PANE_HINTS[key], interior_width)]
    count = len(data) if data else layout.counts.get(key, 1)
    cursor = min(layout.cursors.get(key, 0), max(0, count - 1))
    for index in range(count):
        marker = ">" if index == cursor else " "
        text = data[index] if index < len(data) else f"· 占位 {index + 1}"
        lines.append(fit(f"{marker} {text}", interior_width))
    return lines[:interior_height]


def render(
    layout: Layout,
    width: int,
    height: int,
    *,
    rows: dict[str, tuple[str, ...]] | None = None,
) -> str:
    """把布局画成字符画：``height`` 行，每行显示宽度正好 ``width``。

    纯文本，**不含任何转义序列**——颜色/定位由 :class:`Screen` 在真正写终端时加。
    ``width`` / ``height`` 是画布尺寸；要和布局一致就用 ``panes(width, height)``
    算出来的那对值（尺寸变了请重新算布局，不要只改这里）。

    ``rows`` 是"每栏要画的行文本"。不传就画占位行——**测试与 ``--snapshot`` 都靠
    这个默认值**：只算布局、不碰数据，也能断言"每行宽度正好是 width"。
    """
    canvas = _Canvas(width, height)
    for key in PANE_KEYS:
        rect = layout.panes[key]
        title = PANE_TITLES[key]
        if key == layout.active:
            title = "*" + title  # 焦点栏在标题上打星，不靠颜色（无色终端也要看得出）
        canvas.box(rect, title)
        for offset, line in enumerate(_pane_body(key, rect, layout, rows)):
            canvas.put(rect.x + 1, rect.y + 1 + offset, line)
    if layout.help_visible and layout.help_rect is not None:
        for offset, line in enumerate(HELP_LINES):
            canvas.put(1, layout.help_rect.y + offset, fit(line, width - 2))
    footer = (
        f" orbit tui · {width}x{height} · 焦点：{PANE_TITLES[layout.active]}"
        + (f" · 过滤：{layout.filter}" if layout.filter else "")
        + " · q 退出 · Tab 切栏 · j/k 移动 · r 刷新 · / 过滤 · ? 帮助"
    )
    canvas.put(0, height - 1, fit(footer, width))
    return canvas.render()


# ── 应用 ────────────────────────────────────────────────────────────────────


@dataclass
class TuiState:
    """按键循环的全部状态。测试直接读它，不必解析屏幕。"""

    active: str = "sessions"
    cursors: dict[str, int] = field(default_factory=lambda: {key: 0 for key in PANE_KEYS})
    help_visible: bool = False
    running: bool = True
    #: 让循环停下来的那个键（``q`` 或 Ctrl-C），便于测试断言"是被 q 退出的"。
    quit_key: str = ""
    #: 当前过滤词（空串 = 不过滤）。
    filter: str = ""
    #: 是否正在输入过滤词（``/`` 进入，回车确认，Esc 取消）。
    filtering: bool = False


def _kbhit() -> Any:
    """Windows 上的"有没有按键"探针；其它平台返回 None。

    为什么用它：按键循环是同步的，``sys.stdin.read(1)`` 会一直阻塞，"空闲时按
    ``refresh_interval`` 重读数据"就永远轮不到。``msvcrt`` 是标准库、零依赖，
    刚好能做这件事。非 Windows 没有等价的跨平台标准库方案，于是如实退化成
    "只在按键时才刷新"（写在模块 docstring 的边界里）。
    """
    try:
        import msvcrt  # noqa: PLC0415 —— 平台相关，只有 Windows 有
    except ImportError:
        return None
    return msvcrt


class TuiApp:
    """最小 TUI：算布局、画一帧、读按键、按 clock 重读数据、重画。

    ``stdin`` / ``stdout`` / ``clock`` / ``loader`` 都可注入：

    * 测试用 ``io.StringIO`` 喂按键序列、用假 clock 喂时间、用假 loader 喂数据，
      **一个真终端、一次真 sleep 都不用**；
    * 都不注入时退化成"读真实 stdin"；真实 stdin 不是 TTY（管道、CI、测试）时
      只渲染一帧就返回，**不会阻塞等人按键**。

    ``loader`` 是 ``callable() -> TuiData``（同步）。真实 store 是 async，用
    :class:`StoreFeed` 把两者接起来（见它的 docstring）。
    """

    def __init__(
        self,
        *,
        stdin: Any = None,
        stdout: Any = None,
        width: int = 80,
        height: int = 24,
        counts: dict[str, int] | None = None,
        loader: Callable[[], TuiData] | None = None,
        clock: Callable[[], float] | None = None,
        refresh_interval: float = 0.0,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.width = width
        self.height = height
        self.counts = {**DEFAULT_COUNTS, **(counts or {})}
        self.state = TuiState()
        #: 已经画出去的帧。测试与 `--snapshot` 都读它。
        self.frames: list[str] = []
        self.loader = loader
        # 时间源可注入：测试给假 clock 就能精确断言"到点才刷、没到点不刷"，
        # 不必真等一秒（真等会让整包测试慢一个数量级，还会偶发）。
        self.clock = clock or time.monotonic
        self.refresh_interval = max(0.0, float(refresh_interval))
        self.data = TuiData()
        self.rows: dict[str, tuple[str, ...]] = {key: () for key in PANE_KEYS}
        self.refreshes = 0
        self.last_error = ""
        self.last_refresh = self.clock()

    # ── 数据 / 刷新 ────────────────────────────────────────────────────────
    def visible(self, key: str) -> tuple[str, ...]:
        """某栏**过滤后**的行文本。过滤是大小写不敏感的子串匹配。"""
        rows = tuple(self.rows.get(key) or ())
        needle = self.state.filter.strip().lower()
        if not needle:
            return rows
        return tuple(row for row in rows if needle in row.lower())

    def limit_for(self, key: str) -> int:
        """某栏光标能走到的最大行号。有数据时按**过滤后**的行数算。"""
        rows = self.visible(key)
        if rows:
            return max(0, len(rows) - 1)
        if self.rows.get(key):
            return 0  # 有数据但被过滤空了：没有可停的行
        return max(0, self.counts.get(key, 1) - 1)

    def refresh(self, interval: float | None = None, *, force: bool = False) -> bool:
        """按注入的 clock 决定要不要重读数据。**不 sleep**，返回"这次真重读了吗"。

        * ``force=True``（``r`` 键）：立刻重读，不管间隔；
        * 否则 ``now - last_refresh < interval`` 就跳过——**这是流式刷新的节流**：
          每帧都读库会把 CPU 与 SQLite 一起按住，而屏幕上根本看不出差别。

        没有 loader 时返回 False（保持"只有占位行"的老行为，也保证既有测试不变）。
        """
        gap = self.refresh_interval if interval is None else max(0.0, float(interval))
        now = self.clock()
        if not force and (now - self.last_refresh) < gap:
            return False
        if self.loader is None:
            return False
        self.last_refresh = now
        try:
            data = self.loader()
        except Exception as exc:  # noqa: BLE001 —— 读数据失败不该让界面崩
            self.last_error = f"{type(exc).__name__}: {exc}"
            return False
        self.data = data if isinstance(data, TuiData) else TuiData()
        self.rows = pane_rows(self.data)
        # 行数跟着数据走：`j` / `k` 的上限与 `_pane_body` 的循环次数都读 counts。
        self.counts = {
            key: max(1, len(self.rows[key]) or self.counts.get(key, DEFAULT_COUNTS[key]))
            for key in PANE_KEYS
        }
        for key in PANE_KEYS:  # 数据变短了，光标不能停在不存在的那一行
            self.state.cursors[key] = min(
                self.state.cursors.get(key, 0), max(0, len(self.visible(key)) - 1)
            )
        self.last_error = ""
        self.refreshes += 1
        return True

    # ── 布局 / 渲染 ────────────────────────────────────────────────────────
    def layout(self) -> Layout:
        return panes(
            self.width,
            self.height,
            active=self.state.active,
            help_visible=self.state.help_visible,
            cursors=self.state.cursors,
            counts=self.counts,
            filter=self.state.filter,
        )

    def frame(self) -> str:
        """当前状态对应的一帧字符画（不写终端）。"""
        rows = {key: self.visible(key) for key in PANE_KEYS}
        return render(self.layout(), self.width, self.height, rows=rows)

    # ── 按键 ────────────────────────────────────────────────────────────────
    def handle_key(self, key: str) -> bool:
        """处理一个按键，返回**是否需要重画**。

        过滤态优先：正在输入过滤词时，``q`` 是字母 q 而不是退出键。
        ``q`` / Ctrl-C 退出；``Tab`` 切栏；``j`` / ``k`` 在当前栏内移动（到底/到顶就停，
        不环绕——环绕会让人分不清"到头了"和"跳回去了"）；``r`` 立刻重读数据；
        ``/`` 进入过滤；``?`` 开关帮助。
        """
        if self.state.filtering:
            return self._filter_key(key)
        if key in {"q", "Q", "\x03"}:
            self.state.running = False
            self.state.quit_key = key
            return True
        if key == "\t":
            index = PANE_KEYS.index(self.state.active)
            self.state.active = PANE_KEYS[(index + 1) % len(PANE_KEYS)]
            return True
        if key == "j":
            self._move(+1)
            return True
        if key == "k":
            self._move(-1)
            return True
        if key == "r":
            # 手动刷新：忽略间隔。读失败也照样重画（状态栏/数据不变，但不假装按过）。
            self.refresh(0, force=True)
            return True
        if key == "/":
            self.state.filtering = True
            return True
        if key == "?":
            self.state.help_visible = not self.state.help_visible
            return True
        return False

    def _filter_key(self, key: str) -> bool:
        """过滤输入态：回车确认、Esc 取消、退格删字、其余可见字符追加。"""
        if key in {"\r", "\n"}:
            self.state.filtering = False
            return True
        if key == "\x1b":
            # Esc 取消：把过滤词清空，而不是"留着刚才打了一半的词"。
            self.state.filtering = False
            self.state.filter = ""
            return True
        if key in {"\x7f", "\b"}:
            self.state.filter = self.state.filter[:-1]
            return True
        if key and key.isprintable():
            self.state.filter += key
            return True
        return False

    def _move(self, step: int) -> None:
        key = self.state.active
        limit = self.limit_for(key)
        current = self.state.cursors.get(key, 0)
        self.state.cursors[key] = min(limit, max(0, current + step))

    # ── 主循环 ──────────────────────────────────────────────────────────────
    def _next_key(self, timeout: float) -> str | None:
        """读一个按键。

        返回 ``None`` = 这一轮没有按键（可以去做周期刷新）；``""`` = EOF（该退出）。

        注入了 ``stdin`` 就等于"测试/脚本喂按键"，必须当交互处理并**阻塞读**——
        否则 ``StringIO`` 的 ``isatty()`` 是 False，会被误判成非交互而一帧就退出。
        """
        if self.stdin is not None:
            return self.stdin.read(1)
        keyboard = _kbhit()
        if keyboard is not None and timeout > 0 and not keyboard.kbhit():
            # 真终端 + 有周期刷新：小睡一下再看，把 CPU 让出去（界面空闲时不该满载）。
            time.sleep(min(0.05, timeout))
            return None
        return sys.stdin.read(1)

    def run(self) -> int:
        """跑按键循环。返回进程退出码（正常退出 0）。"""
        out = self.stdout if self.stdout is not None else sys.stdout
        screen = Screen(out)
        self.refresh(0, force=True)  # 首帧就用真实数据（有 loader 时）
        self._emit(out, screen)
        # 注入了 stdin 就等于"测试/脚本喂按键"，必须当交互处理；
        # 否则 StringIO 的 isatty() 是 False，会被误判成非交互而一帧就退出。
        interactive = self.stdin is not None or ansi_enabled(sys.stdin)
        if not interactive:
            self._note(
                out,
                "（stdin 不是终端：只渲染一帧，不进按键循环。交互请直接在终端里跑 `orbit tui`）",
            )
            return 0
        while self.state.running:
            key = self._next_key(self.refresh_interval)
            if key is None:
                # 空闲轮：到点就重读数据并重画（这就是"流式刷新"在同步循环里的形态）。
                if self.refresh(self.refresh_interval):
                    self._emit(out, screen)
                continue
            if not key:  # EOF：管道读完了，等价于退出
                break
            changed = self.handle_key(key)
            if self.refresh(self.refresh_interval):
                changed = True
            if changed:
                self._emit(out, screen)
        return 0

    def _emit(self, out: Any, screen: Screen) -> None:
        """写出一帧（TTY 上先清屏；非 TTY 直接追加，日志才读得懂）。"""
        text = self.frame()
        self.frames.append(text)
        if screen.ansi:
            out.write(screen.clear())
        out.write(text + "\n")
        self._flush(out)

    def _note(self, out: Any, note: str) -> None:
        """写一句说明。**不**追加帧——说明不是画面，混进 `frames` 会让"画了几帧"失真。"""
        out.write(note + "\n")
        self._flush(out)

    @staticmethod
    def _flush(out: Any) -> None:
        try:
            out.flush()
        except Exception:  # noqa: BLE001 —— flush 失败不该让 UI 崩
            pass
