"""终端 UI（MATRIX 1.16）：三栏布局 + 最小按键循环，**只用标准库**。

## 诚实边界（动这里之前先读）

这不是 Hermes 那个 TUI。Hermes 的 ``--tui`` 是 prompt_toolkit 的全屏应用：
多行编辑、历史、补全、鼠标、异步流式刷新、真 PTY。本机**无网络、装不了任何 TUI 库**
（``textual`` / ``prompt_toolkit`` 都不在依赖里，也不许装），所以这里只做
"能看能走"的最小实现，并且刻意把边界写在代码里：

- **看得见**：清屏、光标定位、画框、横竖线（ANSI 转义）；``NO_COLOR`` 或流不是 TTY
  时**整体降级成纯文本**（不输出任何转义序列）。
- **走得动**：``q`` 退出、``Tab`` 切栏、``j`` / ``k`` 上下移动、``?`` 帮助。
  支持的按键全部列在 :data:`HELP_LINES` 里，``orbit tui --help`` 也抄了一份。
- **不渲染真实内容**：三栏只算布局矩形，格子里是**占位行**。它不连数据库、不订阅
  事件流、不做流式刷新。要看真实会话用 ``orbit sessions`` / ``orbit dashboard``
  （浏览器面板），要看回放用 ``orbit dashboard replay``。

## 为什么画框用 ASCII ``+ - |`` 而不是 ``┌─┐``

Unicode 制表符（U+2500 区）在东亚宽度里是 **Ambiguous**：CJK 终端可能按 2 列渲染。
一旦被渲染成 2 列，边框就和上面算好的矩形错位，整个界面歪掉。ASCII 三个字符宽度
恒为 1，代价是朴素——朴素但不错位，比好看但错位值钱。

## 为什么布局与渲染分开

``panes()`` 只算矩形，``render()`` 只把矩形画成字符画，两者都不碰终端。
于是测试可以断言"每行宽度正好是 width、行数正好是 height"，不需要伪终端；
``TuiApp`` 再把 stdin/stdout 做成可注入参数，测试用 ``io.StringIO`` 喂按键序列即可，
**一个真终端都不用开**。
"""

from __future__ import annotations

import os
import sys
import unicodedata
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
    "  ?      显示 / 隐藏这份帮助",
)

#: 每栏的占位行数。**是占位**：没有真实数据源，只为了让 ``j`` / ``k`` 有东西可走。
DEFAULT_COUNTS: dict[str, int] = {"sessions": 6, "conversation": 8, "status": 5}

#: 每栏内部的第一行提示。写清楚"这里不是真实内容"，免得看图的人误会。
PANE_HINTS: dict[str, str] = {
    "sessions": "（占位：真实会话来自 orbit sessions）",
    "conversation": "（只画布局，不渲染真实对话）",
    "status": "（占位：真实轨迹看 orbit dashboard replay）",
}


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
    #: 每栏的占位行数。
    counts: dict[str, int] = field(default_factory=dict)

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
        }


def panes(
    width: int,
    height: int,
    *,
    active: str = "sessions",
    help_visible: bool = False,
    cursors: dict[str, int] | None = None,
    counts: dict[str, int] | None = None,
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
    body_height = height - 1 - help_height

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


def _pane_body(key: str, rect: Rect, layout: Layout) -> list[str]:
    """某栏的内部行（不含边框）。第一行是提示，后面是带光标的占位行。"""
    interior_height = rect.height - 2
    interior_width = rect.width - 2
    if interior_height <= 0 or interior_width <= 0:
        return []
    lines = [fit(PANE_HINTS[key], interior_width)]
    cursor = min(layout.cursors.get(key, 0), max(0, layout.counts.get(key, 1) - 1))
    for index in range(layout.counts.get(key, 1)):
        marker = ">" if index == cursor else " "
        lines.append(fit(f"{marker} · 占位 {index + 1}", interior_width))
    return lines[:interior_height]


def render(layout: Layout, width: int, height: int) -> str:
    """把布局画成字符画：``height`` 行，每行显示宽度正好 ``width``。

    纯文本，**不含任何转义序列**——颜色/定位由 :class:`Screen` 在真正写终端时加。
    ``width`` / ``height`` 是画布尺寸；要和布局一致就用 ``panes(width, height)``
    算出来的那对值（尺寸变了请重新算布局，不要只改这里）。
    """
    canvas = _Canvas(width, height)
    for key in PANE_KEYS:
        rect = layout.panes[key]
        title = PANE_TITLES[key]
        if key == layout.active:
            title = "*" + title  # 焦点栏在标题上打星，不靠颜色（无色终端也要看得出）
        canvas.box(rect, title)
        for offset, line in enumerate(_pane_body(key, rect, layout)):
            canvas.put(rect.x + 1, rect.y + 1 + offset, line)
    if layout.help_visible and layout.help_rect is not None:
        for offset, line in enumerate(HELP_LINES):
            canvas.put(1, layout.help_rect.y + offset, fit(line, width - 2))
    footer = (
        f" orbit tui · {width}x{height} · 焦点：{PANE_TITLES[layout.active]}"
        " · q 退出 · Tab 切栏 · j/k 移动 · ? 帮助"
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


class TuiApp:
    """最小 TUI：算布局、画一帧、读按键、重画。

    ``stdin`` / ``stdout`` 都可注入——测试用 ``io.StringIO`` 喂按键序列，
    一个真终端都不用开。都不注入时退化成"读真实 stdin"；真实 stdin 不是 TTY
    （管道、CI、测试）时只渲染一帧就返回，**不会阻塞等人按键**。
    """

    def __init__(
        self,
        *,
        stdin: Any = None,
        stdout: Any = None,
        width: int = 80,
        height: int = 24,
        counts: dict[str, int] | None = None,
    ) -> None:
        self.stdin = stdin
        self.stdout = stdout
        self.width = width
        self.height = height
        self.counts = {**DEFAULT_COUNTS, **(counts or {})}
        self.state = TuiState()
        #: 已经画出去的帧。测试与 `--snapshot` 都读它。
        self.frames: list[str] = []

    # ── 布局 / 渲染 ────────────────────────────────────────────────────────
    def layout(self) -> Layout:
        return panes(
            self.width,
            self.height,
            active=self.state.active,
            help_visible=self.state.help_visible,
            cursors=self.state.cursors,
            counts=self.counts,
        )

    def frame(self) -> str:
        """当前状态对应的一帧字符画（不写终端）。"""
        return render(self.layout(), self.width, self.height)

    # ── 按键 ────────────────────────────────────────────────────────────────
    def handle_key(self, key: str) -> bool:
        """处理一个按键，返回**是否需要重画**。

        ``q`` / Ctrl-C 退出；``Tab`` 切栏；``j`` / ``k`` 在当前栏内移动（到底/到顶就停，
        不环绕——环绕会让人分不清"到头了"和"跳回去了"）；``?`` 开关帮助。
        """
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
        if key == "?":
            self.state.help_visible = not self.state.help_visible
            return True
        return False

    def _move(self, step: int) -> None:
        key = self.state.active
        limit = max(0, self.counts.get(key, 1) - 1)
        current = self.state.cursors.get(key, 0)
        self.state.cursors[key] = min(limit, max(0, current + step))

    # ── 主循环 ──────────────────────────────────────────────────────────────
    def run(self) -> int:
        """跑按键循环。返回进程退出码（正常退出 0）。"""
        out = self.stdout if self.stdout is not None else sys.stdout
        screen = Screen(out)
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
        source = self.stdin if self.stdin is not None else sys.stdin
        while self.state.running:
            key = source.read(1)
            if not key:  # EOF：管道读完了，等价于退出
                break
            if self.handle_key(key):
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
