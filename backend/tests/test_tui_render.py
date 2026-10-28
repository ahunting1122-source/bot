"""TUI 的**可断言**部分：真实数据、假 clock 驱动的刷新、过滤、StringIO 渲染。

## 为什么全程没有终端、没有 sleep

三个注入点把不确定性全抽走了：

* ``stdin`` / ``stdout``：``io.StringIO`` 喂按键、接画面，一个真 TTY 都不用开；
* ``clock``：假时钟，于是"到点才刷、没到点不刷"可以**精确断言**，不必真等两秒
  （真等会让整包测试变慢，还会在忙机器上偶发失败）；
* ``loader``：假数据源，于是"格子里画的到底是不是库里的东西"可以直接对比。

## 三条口径

1. 每帧**正好** ``height`` 行、每行**显示宽度正好** ``width``（CJK 按 2 列算）；
2. 渲染结果里**不许有转义序列**（颜色/定位归 `Screen`，字符画是纯文本）；
3. **只读**：TUI 从 store 读数据，绝不写库——看界面不该改数据。
"""

from __future__ import annotations

import io

import pytest

from app.tui import (
    HELP_LINES,
    PANE_HINTS,
    PANE_KEYS,
    PANE_TITLES,
    StoreFeed,
    TuiApp,
    TuiData,
    display_width,
    load_from_store,
    pane_rows,
    panes,
    render,
)

# ── 夹具 ────────────────────────────────────────────────────────────────────


class _Clock:
    """假时钟：`advance` 想走多少走多少，不真的等。"""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _Loader:
    """假数据源：记下被读了几次，返回同一份快照。"""

    def __init__(self, data: TuiData) -> None:
        self.data = data
        self.calls = 0

    def __call__(self) -> TuiData:
        self.calls += 1
        return self.data


def _data(sessions: int = 1, *, events: int = 1, tasks: int = 1) -> TuiData:
    return TuiData(
        sessions=tuple(
            {
                "id": f"c{index}",
                "title": f"整理日志 {index}",
                "updated_at": "2026-01-01T00:00:00+00:00",
            }
            for index in range(sessions)
        ),
        tasks=tuple(
            {"id": f"t{index}", "status": "SUCCEEDED", "goal": f"看日志 {index}"}
            for index in range(tasks)
        ),
        events=tuple(
            {
                "task_id": f"t{index}",
                "event_type": "task_completed",
                "created_at": "2026-01-01T00:00:00+00:00",
            }
            for index in range(events)
        ),
    )


def _app(loader=None, clock=None, *, interval: float = 0.0, keys: str | None = None) -> TuiApp:
    return TuiApp(
        stdin=io.StringIO(keys) if keys is not None else None,
        stdout=io.StringIO(),
        loader=loader,
        clock=clock or _Clock(),
        refresh_interval=interval,
    )


class _FakeStore:
    """只读假 store：三次 `rows()` 依次给出会话 / 任务 / 事件；写方法一律记违规。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.batches = [
            [
                type(
                    "Row",
                    (),
                    {
                        "id": "c1",
                        "title": "整理日志",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                        "summary_status": "ready",
                    },
                )()
            ],
            [
                type(
                    "Row",
                    (),
                    {
                        "id": "t1",
                        "status": "RUNNING",
                        "goal": "看日志",
                        "updated_at": "2026-01-01T00:00:01+00:00",
                    },
                )()
            ],
            [
                type(
                    "Row",
                    (),
                    {
                        "task_id": "t1",
                        "event_type": "task_started",
                        "created_at": "2026-01-01T00:00:02+00:00",
                    },
                )()
            ],
        ]
        self.statements: list = []
        self.writes = 0
        self.fail = fail

    async def rows(self, statement):
        self.statements.append(statement)
        if self.fail:
            raise RuntimeError("库读不了")
        return self.batches.pop(0)

    async def save(self, row):  # pragma: no cover —— 被调用就是违约
        self.writes += 1
        raise AssertionError("TUI 不许写库")

    async def event(self, task_id, kind, **payload):  # pragma: no cover
        self.writes += 1
        raise AssertionError("TUI 不许写库")


# ── 1. 真实数据：只读快照 ───────────────────────────────────────────────────


async def test_load_from_store_reads_the_three_panes_and_never_writes():
    store = _FakeStore()
    data = await load_from_store(store)
    assert len(store.statements) == 3, "会话 / 任务 / 事件各一次查询"
    assert store.writes == 0, "只读：一次写都没有"
    assert [item["title"] for item in data.sessions] == ["整理日志"]
    assert [item["status"] for item in data.tasks] == ["RUNNING"]
    assert [item["event_type"] for item in data.events] == ["task_started"]


async def test_store_feed_caches_a_snapshot_for_the_sync_loop():
    """同步按键循环拿不到事件循环：由 `StoreFeed.refresh()` 更新，UI 只读缓存。"""
    feed = StoreFeed(_FakeStore())
    assert feed().is_empty() is True
    await feed.refresh()
    assert feed().is_empty() is False
    assert feed.refreshes == 1
    assert feed.last_error == ""


async def test_store_feed_keeps_the_last_snapshot_when_reading_fails():
    """读库失败不该让界面崩，也不该把上一帧的数据清空。"""
    feed = StoreFeed(_FakeStore())
    await feed.refresh()
    good = feed()
    feed.store = _FakeStore(fail=True)
    await feed.refresh()
    assert feed() is good, "旧快照要留着（比一片空白有用）"
    assert "库读不了" in feed.last_error


def test_pane_rows_lays_the_snapshot_into_three_columns():
    rows = pane_rows(_data())
    assert rows["sessions"][0].startswith("整理日志 0")
    assert rows["status"] == ("SUCCEEDED 看日志 0",)
    # 时间只留 HH:MM:SS：ISO 全串 25 字符会把"事件类型"挤出 20 列的右栏。
    assert rows["conversation"] == ("00:00:00 task_completed",)


async def test_load_from_store_against_a_real_database(tmp_path):
    """真库跑一遍：假 store 只能证明"调用形态对"，证明不了表名/列名对。

    这里连真的 SQLite（`migrate` 出来的 schema）读一次，并顺手核对**只读**：
    读完任务状态不该被界面改动。
    """
    from app.config import Settings
    from app.main import migrate
    from app.storage import Conversation, Message, Store, Task

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    try:
        conversation = await store.save(Conversation(title="整理日志"))
        await store.save(
            Message(conversation_id=conversation.id, role="user", content="看日志")
        )
        task = await store.save(Task(conversation_id=conversation.id, goal="看日志"))
        await store.event(task.id, "task_started", model="test-model")

        data = await load_from_store(store)
        assert [item["title"] for item in data.sessions] == ["整理日志"]
        assert [item["status"] for item in data.tasks] == ["PENDING"]
        assert [item["event_type"] for item in data.events] == ["task_started"]

        again = await store.get(Task, task.id)
        assert again.status == "PENDING", "只读：界面不该改任务状态"
    finally:
        await store.engine.dispose()


# ── 2. 假 clock 驱动的刷新（不 sleep）──────────────────────────────────────


def test_refresh_honours_the_interval_without_sleeping():
    clock = _Clock()
    loader = _Loader(_data())
    app = _app(loader, clock, interval=2.0)

    assert app.refresh(0, force=True) is True, "首帧要立刻读一次"
    assert loader.calls == 1

    clock.advance(1.0)
    assert app.refresh(2.0) is False, "没到间隔：不重读"
    assert loader.calls == 1

    clock.advance(1.5)
    assert app.refresh(2.0) is True, "过了间隔：重读"
    assert loader.calls == 2


def test_r_key_forces_a_refresh():
    clock = _Clock()
    loader = _Loader(_data())
    app = _app(loader, clock, interval=60.0)
    app.refresh(0, force=True)
    assert loader.calls == 1
    assert app.handle_key("r") is True
    assert loader.calls == 2, "`r` 忽略间隔"
    assert app.refreshes == 2


def test_refresh_without_a_loader_keeps_the_placeholder_rows():
    app = _app()
    assert app.refresh(0, force=True) is False
    assert app.rows["sessions"] == ()
    assert "占位 1" in app.frame()


def test_a_failing_loader_does_not_crash_the_ui():
    def broken():
        raise RuntimeError("数据源挂了")

    app = TuiApp(stdout=io.StringIO(), loader=broken, clock=_Clock())
    assert app.refresh(0, force=True) is False
    assert "数据源挂了" in app.last_error
    assert "占位 1" in app.frame(), "读不到数据就画占位，界面不许崩"


def test_data_rows_replace_the_placeholders():
    app = _app(_Loader(_data()))
    app.refresh(0, force=True)
    frame = app.frame()
    assert "整理日志 0" in frame
    # 右栏只有 18 列正文：断言"任务状态真的画出来了"，不锁死被裁掉的那半句。
    assert "SUCCEEDED" in frame
    assert "task_completed" in frame
    assert "占位 1" not in frame, "有真实数据就不该再画占位行"
    assert PANE_HINTS["sessions"] not in frame


def test_cursor_is_clamped_when_the_data_is_shorter_than_the_placeholder_list():
    app = _app(_Loader(_data(sessions=1)))
    app.refresh(0, force=True)
    for _ in range(5):
        app.handle_key("j")
    assert app.state.cursors["sessions"] == 0, "只有一行时 j 停在原地"


def test_cursor_moves_within_the_real_rows():
    app = _app(_Loader(_data(sessions=3)))
    app.refresh(0, force=True)
    app.handle_key("j")
    app.handle_key("j")
    assert app.state.cursors["sessions"] == 2
    app.handle_key("j")
    assert app.state.cursors["sessions"] == 2, "到底就停"


# ── 3. 过滤（`/`）──────────────────────────────────────────────────────────


def test_filter_narrows_the_rows_and_shows_in_the_footer():
    app = _app(_Loader(_data(sessions=3)))
    app.refresh(0, force=True)
    assert app.handle_key("/") is True
    assert app.state.filtering is True
    for char in "日志 1":
        app.handle_key(char)
    assert app.state.filter == "日志 1"
    assert app.visible("sessions") == (pane_rows(_data(sessions=3))["sessions"][1],)
    frame = app.frame()
    assert "过滤：日志 1" in frame

    assert app.handle_key("\r") is True
    assert app.state.filtering is False
    assert app.state.filter == "日志 1", "回车是**确认**，不是清空"


def test_filter_keys_are_typed_not_interpreted():
    """过滤态里按 `q` 是字母 q，不是退出。"""
    app = _app(_Loader(_data()))
    app.refresh(0, force=True)
    app.handle_key("/")
    app.handle_key("q")
    assert app.state.running is True
    assert app.state.filter == "q"


def test_escape_cancels_the_filter():
    app = _app(_Loader(_data()))
    app.refresh(0, force=True)
    app.handle_key("/")
    app.handle_key("日")
    assert app.state.filter == "日"
    app.handle_key("\x1b")
    assert app.state.filtering is False
    assert app.state.filter == "", "Esc 是取消：把打了一半的词清掉"


def test_backspace_deletes_the_last_filter_character():
    app = _app(_Loader(_data()))
    app.handle_key("/")
    for char in "日志":
        app.handle_key(char)
    app.handle_key("\x7f")
    assert app.state.filter == "日"


def test_filter_that_matches_nothing_leaves_no_rows_to_walk():
    app = _app(_Loader(_data(sessions=2)))
    app.refresh(0, force=True)
    app.handle_key("/")
    for char in "zzz":
        app.handle_key(char)
    app.handle_key("\r")
    assert app.visible("sessions") == ()
    assert app.limit_for("sessions") == 0


# ── 4. 渲染到 StringIO（不要求真终端）──────────────────────────────────────


def test_run_renders_assertable_frames_to_a_stringio():
    app = _app(_Loader(_data()), _Clock(), keys="q")
    assert app.run() == 0
    out = app.stdout.getvalue()
    assert "orbit tui" in out
    assert app.frames, "至少要画一帧"
    for frame in app.frames:
        lines = frame.splitlines()
        assert len(lines) == 24
        assert {display_width(line) for line in lines} == {80}
        assert "\x1b" not in frame, "字符画里不许有转义序列"


def test_frames_contain_the_real_data_after_refresh():
    app = _app(_Loader(_data()), _Clock(), keys="rq")
    assert app.run() == 0
    assert any("整理日志 0" in frame for frame in app.frames)
    assert any("task_completed" in frame for frame in app.frames)


def test_render_without_rows_still_draws_the_three_panes():
    """默认（不传 rows）画占位行：`--snapshot` 与老测试都靠这个默认值。"""
    frame = render(panes(80, 24), 80, 24)
    for key in PANE_KEYS:
        assert PANE_TITLES[key] in frame
    assert "占位 1" in frame


def test_footer_documents_the_refresh_and_filter_keys():
    frame = render(panes(120, 24), 120, 24)
    assert "r 刷新" in frame
    assert "/ 过滤" in frame
    text = "\n".join(HELP_LINES)
    for key in ("q", "Tab", "j / k", "r", "/", "?"):
        assert key in text, f"帮助里没提 {key}"


def test_help_block_fits_and_keeps_the_frame_size():
    app = _app(_Loader(_data()), _Clock(), keys="?q")
    assert app.run() == 0
    last = app.frames[-1]
    assert HELP_LINES[0] in last
    assert len(last.splitlines()) == 24
    assert {display_width(line) for line in last.splitlines()} == {80}


@pytest.mark.parametrize(("width", "height"), [(80, 24), (60, 16), (120, 30)])
def test_every_frame_keeps_the_canvas_size_with_data(width: int, height: int):
    app = TuiApp(
        stdout=io.StringIO(), width=width, height=height, loader=_Loader(_data(3, events=5)), clock=_Clock()
    )
    app.refresh(0, force=True)
    frame = app.frame()
    lines = frame.splitlines()
    assert len(lines) == height
    assert {display_width(line) for line in lines} == {width}
