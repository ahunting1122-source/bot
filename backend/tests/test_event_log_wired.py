"""事件日志不再孤立（MATRIX 10.14 的验收判据）。

## 之前缺的是什么

`app/event_log.py` 写好了 append-only 流、`cursors`、`watermarks`、`compact`、
`watch`，测试也齐。但 **`grep event_log app/` 零命中**——只有 tests 引用它。
**没有任何生产代码写入或读取它。** 一个「机制完备但没人调用」的孤立文件。

## 判据

1. `Store.event()` 写的每条事件**同时**进日志（`seq` 递增、`kind` 保留）；
2. **`task_events` 表照常写**——日志是追加的审计轨，不是替代品；
3. **日志写失败不回滚主存储，但必须记账**——让一个可选诊断设施能阻止任务推进，
   方向就错了；
4. 源码层：`Store.event` 里必须真的调 `event_log.append`（防它又变回孤立文件）。
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def store(tmp_path):
    from app.config import Settings
    from app.main import migrate
    from app.storage import EVENT_LOG_FAILURES, Store

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    EVENT_LOG_FAILURES.clear()
    instance = Store(settings.data_dir / "app.sqlite")
    yield instance
    EVENT_LOG_FAILURES.clear()


async def test_every_event_also_lands_in_the_log(store):
    """**核心判据**：`Store.event()` 写的每条事件都要进日志。"""
    await store.event("task-1", "task_started", model="x")
    await store.event("task-1", "budget_stopped", reason="max_turns")

    log = store.event_log
    assert log.streams() == ["task-1"], f"日志里没有这个流：{log.streams()}"
    assert log.last_seq("task-1") == 2, "seq 没有递增"

    kinds = [record["payload"].get("kind") for record in log.read_all("task-1")]
    assert kinds == ["task_started", "budget_stopped"]
    # payload 也要在（不然审计轨只剩事件名）
    assert log.read_all("task-1")[1]["payload"].get("reason") == "max_turns"


async def test_the_table_is_still_written(store):
    """日志是**追加的审计轨**，不是替代品——`task_events` 表必须照常写。"""
    from sqlalchemy import text

    await store.event("task-2", "task_started", model="y")
    async with store.engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT event_type FROM task_events WHERE task_id=:t"), {"t": "task-2"}
            )
        ).fetchall()
    assert [row[0] for row in rows] == ["task_started"]


async def test_a_broken_log_does_not_block_the_table(store):
    """**失败隔离**：日志写失败不能拦主存储，但**必须记账**。"""
    from app.storage import EVENT_LOG_FAILURES

    def _boom(*_args, **_kwargs):
        raise RuntimeError("磁盘满了")

    store.event_log.append = _boom  # type: ignore[method-assign]

    # 不该抛
    await store.event("task-3", "task_started", model="z")

    assert EVENT_LOG_FAILURES, "日志写失败没记账——审计轨静默坏掉比没有更难发现"
    assert "磁盘满了" in EVENT_LOG_FAILURES[-1]

    # 主存储照常
    from sqlalchemy import text

    async with store.engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT event_type FROM task_events WHERE task_id=:t"), {"t": "task-3"}
            )
        ).fetchall()
    assert [row[0] for row in rows] == ["task_started"]


def test_the_log_is_reachable_from_the_event_write_path():
    """**防回归**：`Store.event` 里必须真的调 `event_log.append`。

    10.14 的虚报形状是「机制写好了、`grep` 零命中」——
    只验 `EventLog` 能用是不够的，必须钉住调用点存在。
    """
    import pathlib

    from app import storage as storage_module

    source = pathlib.Path(storage_module.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert "self.event_log.append(" in code, (
        "`Store.event` 不再写事件日志了——`app/event_log.py` 又变成孤立文件"
    )


def test_the_failure_buffer_is_bounded(store):
    """**有界**：失败记账不能无限增长。

    原来是无界 `list`，而一个**持续失败**的审计轨会让它无限长——
    **一个失败的诊断设施把进程内存吃掉**，这个失败模式本身很讽刺。
    """
    from app.storage import EVENT_LOG_FAILURES

    for index in range(EVENT_LOG_FAILURES.maxlen + 50):
        EVENT_LOG_FAILURES.append(f"failure-{index}")

    assert len(EVENT_LOG_FAILURES) == EVENT_LOG_FAILURES.maxlen
    # 留的是**最近**的（诊断要看最近发生了什么，不是最早）
    assert EVENT_LOG_FAILURES[-1] == f"failure-{EVENT_LOG_FAILURES.maxlen + 49}"


async def test_the_failure_buffer_has_a_reader(store):
    """**有读点**：`describe_event_log()` 必须能报出审计轨正在坏。

    没有这个读点，「失败必须可见」就只是一句注释——
    而**「只写不读」正是我给 10.14 留 🟡 的那把尺子**，它也得量自己。
    """
    from app.storage import EVENT_LOG_FAILURES

    healthy = store.describe_event_log()
    assert healthy["ok"] is True
    assert healthy["capacity"] == EVENT_LOG_FAILURES.maxlen

    def _boom(*_args, **_kwargs):
        raise RuntimeError("磁盘满了")

    store.event_log.append = _boom  # type: ignore[method-assign]
    await store.event("task-h", "task_started", model="x")

    broken = store.describe_event_log()
    assert broken["ok"] is False, "审计轨在坏，但 describe 说它健康"
    assert broken["count"] >= 1
    assert "磁盘满了" in broken["last"]


# ── 读路径（10.14 从「只写不读」变成「读写都有」）────────────────────────────


def _run_cli(data_dir, *argv: str) -> tuple[int, str, str]:
    """跑一次 CLI，返回 (退出码, stdout, stderr)。"""
    import io
    from contextlib import redirect_stderr, redirect_stdout

    from app.cli.main import main as cli_main

    out, err = io.StringIO(), io.StringIO()
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_main(["--data-dir", str(data_dir), *argv])
    except SystemExit as exc:  # argparse 的 --help 等
        code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


async def test_plane_tail_reads_the_event_log(tmp_path):
    """**读路径真的存在**：`orbit plane tail --event-log <task_id>` 能读出事件。

    这条是 10.14 从「只写不读」变成「读写都有」的分界。
    """
    from app.config import Settings
    from app.main import migrate
    from app.storage import Store

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    try:
        await store.event("task-abc", "task_started", model="deepseek")
        await store.event("task-abc", "budget_stopped", reason="max_turns")
    finally:
        await store.engine.dispose()

    code, out, err = _run_cli(
        settings.data_dir, "plane", "tail", "task-abc", "-n", "5", "--event-log"
    )
    assert code == 0, f"读事件日志失败：{err}"
    assert "task_started" in out, f"没读到第一条事件：{out}"
    assert "budget_stopped" in out
    # **带 seq** 才是这份日志独有的价值（表只有时间戳）
    assert "#1" in out and "#2" in out, f"没显示出 seq：{out}"


async def test_an_unknown_stream_says_it_is_a_task_id(tmp_path):
    """查不到流时必须**说清是名字用错了**，不能静默返回空。

    事件日志的流名是 `task_id` 而 `plane tail` 的位置参数是**会话 id**——
    不解释的话用户会以为「这个会话没有事件」，而实际是查错了名字。
    """
    from app.config import Settings
    from app.main import migrate

    settings = Settings(data_dir=tmp_path / "data2", workspace=tmp_path / "files2")
    settings.prepare()
    migrate(settings)

    code, _out, err = _run_cli(
        settings.data_dir, "plane", "tail", "not-a-task", "--event-log"
    )
    assert code != 0, "查不到流却返回成功——用户会以为这个会话没有事件"
    assert "task_id" in err, f"没解释「流名是 task_id」：{err}"


def test_doctor_reports_the_event_log_health(tmp_path):
    """**`EVENT_LOG_FAILURES` 的生产读点**：`orbit doctor` 必须报它。

    上一轮加了 `event_log_health()` 但**没有任何用户能触达的地方调它**——
    那还是「只写不读」的另一种形态。这条钉住它出现在 `doctor` 的输出里。
    """
    from app.config import Settings

    settings = Settings(data_dir=tmp_path / "data3", workspace=tmp_path / "files3")
    settings.prepare()

    code, out, _err = _run_cli(settings.data_dir, "doctor")
    # doctor 可能因为别的检查项返回非 0；这里只关心事件日志那一行出现
    assert "事件日志" in out, f"`orbit doctor` 没报事件日志：{out[:400]}"
    assert "缓冲上限" in out, "没报缓冲区上限——「100 条」会被读成「只失败了 100 次」"
    assert code in (0, 1)
