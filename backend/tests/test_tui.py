"""`app/tui.py`（最小终端界面）与 `orbit tui` 的契约测试。

## 为什么全程用 `io.StringIO` 而不开伪终端

这台机器上不能起真终端（也不能装 `pexpect` / `pyte`）。所以本模块把
"布局"（:func:`panes` 只算矩形）与"渲染"（:func:`render` 只输出字符画）拆开，
`TuiApp` 的 ``stdin`` / ``stdout`` 又都做成可注入参数——于是按键序列可以用
``io.StringIO("jj\t?q")`` 喂进去，**一个真 TTY 都不用碰**。

## 三条口径

1. ``render`` 输出必须**正好** ``height`` 行，每行**显示宽度正好** ``width``
   （按 CJK 宽度算，不按字符数——中文标题占两列，按 ``len`` 量会歪）。
2. 渲染结果里**不许有转义序列**：颜色/定位归 :class:`Screen` 管，字符画是纯文本。
   ``NO_COLOR`` 或非 TTY 时 ``Screen`` 也要整体降级（一行转义都不发）。
3. 按键循环必须能被 ``q`` 停下、``?`` 出帮助、``Tab`` 切栏、``j`` / ``k`` 移动，
   并且 **EOF 不能卡死**（管道喂完就退出）。
"""

from __future__ import annotations

import io

import pytest

from app.cli.main import main
from app.tui import (
    DEFAULT_COUNTS,
    HELP_LINES,
    MIN_HEIGHT,
    MIN_WIDTH,
    PANE_KEYS,
    PANE_TITLES,
    Screen,
    TuiApp,
    ansi_enabled,
    box_lines,
    display_width,
    panes,
    render,
)

# 导入即接线（`commands/__init__.py` 由主线统一接，这里显式导入一次）。
import app.cli.commands.ui_extra  # noqa: F401  # isort: skip


# ── 渲染：尺寸与内容 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(("width", "height"), [(80, 24), (60, 16), (MIN_WIDTH, MIN_HEIGHT)])
def test_render_matches_canvas_size(width: int, height: int):
    """行数正好 height，每行显示宽度正好 width（含最小尺寸这种极端值）。"""
    frame = render(panes(width, height), width, height)
    lines = frame.splitlines()
    assert len(lines) == height, f"行数 {len(lines)} != {height}"
    assert {display_width(line) for line in lines} == {width}, (
        f"有行宽度不是 {width}：{[(display_width(line), line) for line in lines if display_width(line) != width][:3]}"
    )


def test_render_draws_all_three_panes():
    """三栏的标题都要出现，且每栏都有边框。"""
    frame = render(panes(80, 24), 80, 24)
    for key in PANE_KEYS:
        assert PANE_TITLES[key] in frame, f"{key} 栏的标题没画出来"
    assert frame.count("+") >= 12, "三个框至少 12 个角"
    assert "orbit tui" in frame, "底部状态栏没画出来"


def test_render_marks_the_active_pane_with_a_star():
    """焦点栏靠标题上的 `*` 标出来——无色终端也要看得出焦点在哪。"""
    active = render(panes(80, 24, active="conversation"), 80, 24)
    assert f"*{PANE_TITLES['conversation']}" in active
    assert f"*{PANE_TITLES['sessions']}" not in active


def test_render_contains_no_escape_sequences():
    """字符画是纯文本：颜色/定位只在真正写终端时由 `Screen` 加。"""
    assert "\x1b" not in render(panes(80, 24), 80, 24)


def test_layout_partitions_the_width():
    """三栏横向铺满、不重叠；正文高度 = 总高 - 1（底部状态栏）。"""
    layout = panes(80, 24)
    rects = [layout.rect(key) for key in PANE_KEYS]
    assert sum(rect.width for rect in rects) == 80
    assert rects[0].x == 0
    for left, right in zip(rects, rects[1:], strict=False):
        assert left.right == right.x, "栏与栏之间不该有缝也不该重叠"
    assert {rect.height for rect in rects} == {23}
    assert rects[0].y == 0


def test_help_block_reserves_rows_above_the_status_bar():
    """帮助块从正文里让出空间，而不是盖在正文上。"""
    plain = panes(80, 24)
    helped = panes(80, 24, help_visible=True)
    assert helped.help_rect is not None
    assert helped.help_rect.height == len(HELP_LINES)
    assert helped.rect("sessions").height == plain.rect("sessions").height - len(HELP_LINES)
    assert helped.help_rect.bottom == 23, "帮助块应紧贴底部状态栏上方"
    frame = render(helped, 80, 24)
    assert HELP_LINES[0] in frame
    assert len(frame.splitlines()) == 24


def test_render_keeps_the_cursor_marker_visible():
    """`j` 移动后，标记要从第 1 行挪到第 2 行（布局里带着光标状态）。"""
    frame = render(panes(80, 24, cursors={"sessions": 1}), 80, 24)
    # 只看左栏那 18 列：另两栏的光标还在第 1 行，不切开量会误判。
    left = [line[:18] for line in frame.splitlines()]
    assert any("> · 占位 2" in line for line in left)
    assert not any("> · 占位 1" in line for line in left)


def test_too_small_terminal_is_rejected_with_a_readable_message():
    """太小的尺寸当场报错：三栏算下去会出现负宽度，静默画歪更难查。"""
    with pytest.raises(ValueError, match="终端太小"):
        panes(MIN_WIDTH - 1, MIN_HEIGHT)
    with pytest.raises(ValueError, match="终端太小"):
        panes(MIN_WIDTH, MIN_HEIGHT - 1)


def test_unknown_active_pane_is_rejected():
    with pytest.raises(ValueError, match="未知焦点栏"):
        panes(80, 24, active="nope")


def test_box_lines_embeds_the_title_in_the_top_border():
    lines = box_lines(20, 4, "会话")
    assert len(lines) == 4
    assert lines[0].startswith("+[ 会话 ]")
    assert display_width(lines[0]) == 20
    assert lines[0].endswith("+") and lines[-1] == "+" + "-" * 18 + "+"
    # 标题太长时整行退化成一排 `-`，绝不把顶边撑歪。
    wide = box_lines(8, 3, "一个特别长的标题")
    assert display_width(wide[0]) == 8
    assert "标" not in wide[0]


# ── Screen：ANSI 与降级 ─────────────────────────────────────────────────────


def test_screen_emits_ansi_sequences_when_enabled():
    screen = Screen(io.StringIO(), ansi=True)
    assert screen.clear() == "\x1b[2J\x1b[H"
    assert screen.move(2, 3) == "\x1b[2;3H"
    assert screen.hline(1, 1, 3) == "\x1b[1;1H---"
    assert screen.vline(5, 2, 2) == "\x1b[2;5H|\x1b[3;5H|"
    boxed = screen.box(panes(80, 24).rect("sessions"), "会话")
    assert boxed.count("\x1b[") == panes(80, 24).rect("sessions").height, "框的每一行都该自带定位"


def test_screen_degrades_to_plain_text_without_ansi():
    """`ansi=False`：一行转义都不许出现（重定向到文件时不能写乱码）。"""
    screen = Screen(io.StringIO(), ansi=False)
    assert screen.clear() == ""
    assert screen.move(2, 3) == ""
    assert screen.reset() == ""
    assert screen.hline(1, 1, 3) == "---"
    assert screen.vline(5, 2, 2) == "|\n|"
    assert "\x1b" not in screen.box(panes(80, 24).rect("sessions"), "会话")


def test_no_color_env_disables_ansi(monkeypatch: pytest.MonkeyPatch):
    """`NO_COLOR` **存在**即降级（空串也算设了，按 NO_COLOR 规范）。"""
    stream = io.StringIO()
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert ansi_enabled(stream) is False, "StringIO 不是 TTY，本来就不该用 ANSI"

    class FakeTty(io.StringIO):
        def isatty(self) -> bool:  # noqa: D102 —— 测试桩
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    assert ansi_enabled(FakeTty()) is True
    monkeypatch.setenv("NO_COLOR", "")
    assert ansi_enabled(FakeTty()) is False
    assert Screen(FakeTty()).ansi is False


def test_screen_flush_writes_the_buffer():
    stream = io.StringIO()
    screen = Screen(stream, ansi=False)
    screen.write("一")
    screen.write("二")
    assert screen.flush() == "一二"
    assert stream.getvalue() == "一二"


# ── 按键循环 ────────────────────────────────────────────────────────────────


def _app(keys: str, **kwargs) -> tuple[TuiApp, io.StringIO]:
    out = io.StringIO()
    app = TuiApp(stdin=io.StringIO(keys), stdout=out, **kwargs)
    return app, out


def test_quit_key_stops_the_loop():
    app, out = _app("q")
    assert app.run() == 0
    assert app.state.running is False
    assert app.state.quit_key == "q"
    assert "orbit tui" in out.getvalue()


def test_ctrl_c_also_quits():
    app, _ = _app("\x03")
    assert app.run() == 0
    assert app.state.quit_key == "\x03"


def test_eof_does_not_hang():
    """管道喂完（EOF）就退出，不等人按键。"""
    app, _ = _app("")
    assert app.run() == 0
    assert app.state.running is True, "EOF 不是按键，不该记成 q"
    assert app.frames, "至少画出首帧"


def test_help_key_toggles_the_help_block():
    app, out = _app("?q")
    assert app.run() == 0
    assert app.state.help_visible is True
    assert HELP_LINES[0] in out.getvalue()
    # 再按一次关掉
    app2, _ = _app("??q")
    assert app2.run() == 0
    assert app2.state.help_visible is False


def test_tab_cycles_focus_across_panes():
    app, _ = _app("\tq")
    assert app.run() == 0
    assert app.state.active == "conversation"
    app, _ = _app("\t\tq")
    assert app.run() == 0
    assert app.state.active == "status"
    app, _ = _app("\t\t\tq")
    assert app.run() == 0
    assert app.state.active == "sessions", "三栏应当环绕"


def test_j_and_k_move_the_cursor_within_the_active_pane():
    app, _ = _app("jjjq")
    assert app.run() == 0
    assert app.state.cursors["sessions"] == 3
    app, _ = _app("jjjkq")
    assert app.run() == 0
    assert app.state.cursors["sessions"] == 2
    # 到顶就停，不环绕（环绕会让人分不清"到头了"和"跳回去了"）
    app, _ = _app("kkkkkq")
    assert app.run() == 0
    assert app.state.cursors["sessions"] == 0
    # 到底也停
    app, _ = _app("j" * (DEFAULT_COUNTS["sessions"] + 5) + "q")
    assert app.run() == 0
    assert app.state.cursors["sessions"] == DEFAULT_COUNTS["sessions"] - 1


def test_moving_only_affects_the_focused_pane():
    app, _ = _app("j\tjq")
    assert app.run() == 0
    assert app.state.cursors["sessions"] == 1
    assert app.state.cursors["conversation"] == 1
    assert app.state.cursors["status"] == 0


def test_unknown_keys_do_not_redraw():
    app, _ = _app("xq")
    app.run()
    assert len(app.frames) == 2, "x 不该触发重画（首帧 + q 那一帧）"


def test_frame_reflects_state_changes():
    app, out = _app("j?q")
    app.run()
    last = app.frames[-1]
    assert "> · 占位 2" in last
    assert HELP_LINES[0] in last
    assert len(last.splitlines()) == 24
    assert {display_width(line) for line in last.splitlines()} == {80}
    assert out.getvalue().count("orbit tui") >= 2, "每帧都要写出去"


def test_non_tty_without_injected_stdin_renders_one_frame(monkeypatch: pytest.MonkeyPatch):
    """没有注入 stdin、真实 stdin 又不是 TTY（管道 / CI）时：只渲染一帧就返回。"""

    class NotATty(io.StringIO):
        def isatty(self) -> bool:  # noqa: D102 —— 测试桩
            return False

        def read(self, size: int = -1) -> str:  # noqa: D102 —— 真读了就说明卡住了
            raise AssertionError("非 TTY 下不该读 stdin")

    monkeypatch.setattr("sys.stdin", NotATty(""))
    out = io.StringIO()
    app = TuiApp(stdout=out)
    assert app.run() == 0
    assert len(app.frames) == 1
    assert "stdin 不是终端" in out.getvalue()


def test_help_lists_every_supported_key():
    """帮助里必须列全支持的按键——列不全就等于没实现。"""
    text = "\n".join(HELP_LINES)
    for key in ("q", "Tab", "j", "k", "?"):
        assert key in text, f"帮助里没提 {key}"
    assert "退出" in text and "切" in text


# ── CLI：orbit tui ──────────────────────────────────────────────────────────


def test_cli_tui_snapshot_renders_one_frame(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "tui", "--snapshot"])
    out = capsys.readouterr().out
    assert code == 0
    assert "orbit tui" in out
    for key in PANE_KEYS:
        assert PANE_TITLES[key] in out
    assert "--snapshot" in out


def test_cli_tui_snapshot_honours_size(tmp_path, capsys):
    code = main(
        ["--data-dir", str(tmp_path / "data"), "tui", "--snapshot", "--width", "60", "--height", "16"]
    )
    out = capsys.readouterr().out
    assert code == 0
    frame = out.split("\n（--snapshot")[0]
    assert len(frame.splitlines()) == 16
    assert {display_width(line) for line in frame.splitlines()} == {60}


def test_cli_tui_rejects_too_small_terminal(tmp_path, capsys):
    code = main(
        ["--data-dir", str(tmp_path / "data"), "tui", "--snapshot", "--width", "20"]
    )
    assert code == 2
    assert "终端太小" in capsys.readouterr().err


def test_cli_tui_help_documents_the_boundary(capsys):
    """`orbit tui --help` 必须写明"这不是 Hermes 那个完整 TUI"与全部按键。"""
    from app.cli.main import build_parser

    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["tui", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "最小终端界面" in out
    assert "不是 Hermes 那个完整 TUI" in out, "诚实边界没写进 --help"
    for key in ("q 退出", "Tab 切栏", "j/k 上下移动", "? 帮助"):
        assert key in out, f"--help 里没列按键：{key}"
