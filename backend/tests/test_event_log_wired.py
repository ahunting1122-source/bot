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
