"""清理检查点库里已不存在任务的残留状态。

`data/checkpoints.sqlite` 是 LangGraph 的图状态库（`AsyncSqliteSaver`），
`thread_id` 就是 task_id。任务被删（例如清理评测数据）后，这里的检查点不会自动
消失，于是库会一直涨——本机实测 70MB 里绝大部分是评测任务的残留。

本脚本以 `app.sqlite` 的 tasks 表为准，删除检查点中**不在该集合内**的 thread。
默认 dry-run，要真删传 `--apply`。

用法：
    uv run python ../scripts/cleanup_checkpoints.py
    uv run python ../scripts/cleanup_checkpoints.py --apply
"""

from __future__ import annotations

import argparse
import pathlib
import sqlite3
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP_DB = ROOT / "data" / "app.sqlite"
CKPT_DB = ROOT / "data" / "checkpoints.sqlite"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真删；不加则只报告")
    args = parser.parse_args()

    for path in (APP_DB, CKPT_DB):
        if not path.exists():
            print(f"找不到 {path}")
            return 1

    alive = [r[0] for r in sqlite3.connect(APP_DB).execute("SELECT id FROM tasks")]
    print(f"app.sqlite 现存任务 {len(alive)} 个")

    db = sqlite3.connect(CKPT_DB)
    total_threads = db.execute("SELECT COUNT(DISTINCT thread_id) FROM checkpoints").fetchone()[0]
    total_ckpt = db.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
    total_writes = db.execute("SELECT COUNT(*) FROM writes").fetchone()[0]
    before = CKPT_DB.stat().st_size

    marks = ",".join("?" * len(alive)) if alive else "''"
    doomed = db.execute(
        f"SELECT COUNT(DISTINCT thread_id) FROM checkpoints WHERE thread_id NOT IN ({marks})",
        tuple(alive),
    ).fetchone()[0]
    doomed_ckpt = db.execute(
        f"SELECT COUNT(*) FROM checkpoints WHERE thread_id NOT IN ({marks})", tuple(alive)
    ).fetchone()[0]
    doomed_writes = db.execute(
        f"SELECT COUNT(*) FROM writes WHERE thread_id NOT IN ({marks})", tuple(alive)
    ).fetchone()[0]

    print(f"检查点库：{total_threads} 个 thread / {total_ckpt} 条 checkpoints / "
          f"{total_writes} 条 writes（{before / 1048576:.2f} MB）")
    print(f"待删：{doomed} 个 thread / {doomed_ckpt} 条 checkpoints / {doomed_writes} 条 writes")

    if not args.apply:
        print("DRY-RUN 结束。加 --apply 才会真删。")
        return 0

    with db:
        db.execute(f"DELETE FROM checkpoints WHERE thread_id NOT IN ({marks})", tuple(alive))
        db.execute(f"DELETE FROM writes WHERE thread_id NOT IN ({marks})", tuple(alive))

    print("合并 WAL 并 VACUUM 回收空间…")
    # 顺序很重要：WAL 模式下必须先 TRUNCATE checkpoint 把 -wal 里的页并回主库，
    # 否则 VACUUM 看到的空间与磁盘实际占用不一致，回收量会显示为 0。
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("VACUUM")
    after = CKPT_DB.stat().st_size
    print(f"  {before / 1048576:.2f} MB → {after / 1048576:.2f} MB"
          f"（回收 {(before - after) / 1048576:.2f} MB）")
    print(f"剩余 thread {db.execute('SELECT COUNT(DISTINCT thread_id) FROM checkpoints').fetchone()[0]} 个")
    return 0


if __name__ == "__main__":
    sys.exit(main())
