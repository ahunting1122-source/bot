"""清理评测/测试在真库里留下的数据，并回收空间。

背景（见 handoff 与本轮排查）：`eval_run.py` 每条评测 turn 都新建会话，且
`memory_across_conversations` 用例每次生成新 run_id 且从不清理，于是真库被两轮
评测（09-21 55 个会话、09-30 135 个会话）灌成测试场地，memories 表叠了 5 条
同义 key 的垃圾。

本脚本做三件事：
  1. 删 memories 里的评测残留（保留白名单里那条）；
  2. 删评测会话及其**全部级联数据**；
  3. VACUUM 回收空间。

**默认只报告不执行**（dry-run）。要真删必须显式传 `--apply`。
证据不丢：`test-results/` 下的 report.json 不归本脚本管。

用法：
    uv run python ../scripts/cleanup_eval_data.py            # 只报告
    uv run python ../scripts/cleanup_eval_data.py --apply    # 真删
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "app.sqlite"

#: 要清理的评测日期。这两天的会话整批是评测产生的（09-21 55 个 / 09-30 135 个）。
EVAL_DAYS = ("2026-09-21", "2026-09-30")
#: 明确保留的记忆（内容最干净、无 run_id 污染）。
KEEP_MEMORY_IDS = ("fa3fcf4e-9e4e-46d7-8b97-27b2415704b4",)

#: 级联删除顺序：先子后父（外键指向 conversations / tasks）。
#: `chunk_vectors` 是 vec0 虚表，**必须在 chunks 之前删**——它按 `chunks.row_id`
#: 关联，chunks 一旦删掉就查不到该删哪些向量，会留下删不掉的孤儿。
#: 注意 vec0 的主键列名是 `row_id`，**不是** SQLite 内置的 `rowid`。
CASCADE_BY_CONVERSATION = (
    ("chunk_vectors", "row_id IN (SELECT row_id FROM chunks WHERE conversation_id IN ({ids}))"),
    ("chunks", "conversation_id IN ({ids})"),
    ("tool_results", "task_id IN (SELECT id FROM tasks WHERE conversation_id IN ({ids}))"),
    ("tool_calls", "task_id IN (SELECT id FROM tasks WHERE conversation_id IN ({ids}))"),
    ("artifacts", "task_id IN (SELECT id FROM tasks WHERE conversation_id IN ({ids}))"),
    ("task_events", "task_id IN (SELECT id FROM tasks WHERE conversation_id IN ({ids}))"),
    ("tasks", "conversation_id IN ({ids})"),
    ("messages", "conversation_id IN ({ids})"),
    ("conversations", "id IN ({ids})"),
)


def table_exists(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?", (name,)
    ).fetchone()
    return row is not None


def load_vec_extension(db: sqlite3.Connection) -> bool:
    """vec0 是虚拟表：不加载 sqlite-vec 扩展就既不能 SELECT 也不能 DELETE，报
    `no such module: vec0`。清理前必须先加载，否则向量会变成删不掉的孤儿。
    扩展不可用时返回 False，调用方跳过该表而不是让整个清理失败。
    """
    try:
        import sqlite_vec
    except ImportError:
        return False
    try:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
    except Exception:
        return False
    return True


def count(db: sqlite3.Connection, sql: str, params: tuple) -> int:
    try:
        return db.execute(f"SELECT COUNT(*) FROM ({sql})", params).fetchone()[0]
    except sqlite3.OperationalError:
        return -1  # 表不存在（例如未装 sqlite-vec 时的虚表）


def describe(db: sqlite3.Connection, ids: list[str], vec_ready: bool = True) -> dict[str, int]:
    """逐表统计将受影响的行数。vec_ready=False 时跳过 vec0 虚表。"""
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    report: dict[str, int] = {}
    for table, where in CASCADE_BY_CONVERSATION:
        if not table_exists(db, table):
            continue
        if table == "chunk_vectors" and not vec_ready:
            continue
        sql = f"SELECT * FROM {table} WHERE {where.format(ids=marks)}"
        report[table] = count(db, sql, tuple(ids))
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="真删；不加则只报告")
    parser.add_argument("--days", nargs="*", default=list(EVAL_DAYS), help="要清理的评测日期")
    args = parser.parse_args()

    if not DB.exists():
        print(f"找不到数据库：{DB}")
        return 1

    db = sqlite3.connect(DB)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=OFF")
    vec_ready = load_vec_extension(db)
    if not vec_ready and table_exists(db, "chunk_vectors"):
        print("提示：sqlite-vec 未加载，chunk_vectors 将被跳过（向量会残留）。")

    before_size = DB.stat().st_size
    mode = "APPLY（会真删）" if args.apply else "DRY-RUN（只报告）"

    # ── 1. memories ────────────────────────────────────────────────────────
    doomed = [
        dict(row)
        for row in db.execute(
            "SELECT id, key, value FROM memories WHERE id NOT IN ({}) ORDER BY updated_at".format(
                ",".join("?" * len(KEEP_MEMORY_IDS))
            ),
            KEEP_MEMORY_IDS,
        )
    ]
    kept = [
        dict(row)
        for row in db.execute(
            "SELECT id, key FROM memories WHERE id IN ({})".format(",".join("?" * len(KEEP_MEMORY_IDS))),
            KEEP_MEMORY_IDS,
        )
    ]

    # ── 2. 评测会话 ────────────────────────────────────────────────────────
    day_marks = ",".join("?" * len(args.days))
    ids = [
        r[0]
        for r in db.execute(
            f"SELECT id FROM conversations WHERE substr(created_at,1,10) IN ({day_marks})",
            tuple(args.days),
        )
    ]
    cascade = describe(db, ids, vec_ready)

    # ── 报告 ───────────────────────────────────────────────────────────────
    print(f"=== 清理模式：{mode} ===")
    print(f"数据库：{DB}（{before_size / 1048576:.2f} MB）")
    print()
    print(f"── memories：保留 {len(kept)} 条 / 删除 {len(doomed)} 条 ──")
    for row in kept:
        print(f"   KEEP  {row['id']}  {row['key']}")
    for row in doomed:
        print(f"   DEL   {row['id']}  {row['key'][:60]}")
    print()
    print(f"── 评测会话（{', '.join(args.days)}）：{len(ids)} 个 ──")
    for table, rows in cascade.items():
        print(f"   {table:16s} {rows} 行")
    print()

    if not args.apply:
        print("DRY-RUN 结束。加 --apply 才会真删。")
        return 0

    # ── 执行 ───────────────────────────────────────────────────────────────
    deleted: dict[str, int] = {}
    marks = ",".join("?" * len(ids))
    with db:
        for table, where in CASCADE_BY_CONVERSATION:
            if not table_exists(db, table):
                continue
            if table == "chunk_vectors" and not vec_ready:
                deleted[table] = -1  # 无法处理，如实标记
                continue
            if not ids:
                deleted[table] = 0
                continue
            sql = f"DELETE FROM {table} WHERE {where.format(ids=marks)}"
            try:
                cur = db.execute(sql, tuple(ids))
                deleted[table] = cur.rowcount
            except sqlite3.OperationalError as exc:
                print(f"   !! {table} 删除失败：{exc}")
                deleted[table] = -1
        cur = db.execute(
            "DELETE FROM memories WHERE id NOT IN ({})".format(",".join("?" * len(KEEP_MEMORY_IDS))),
            KEEP_MEMORY_IDS,
        )
        deleted["memories"] = cur.rowcount

    print("── 实际删除 ──")
    for table, rows in deleted.items():
        note = "（vec0 未加载，已跳过）" if rows == -1 else " 行"
        print(f"   {table:16s} {rows} {note}" if rows == -1 else f"   {table:16s} {rows} 行")

    print()
    print("合并 WAL 并 VACUUM 回收空间…")
    # WAL 模式下必须先 TRUNCATE checkpoint，否则 VACUUM 的回收量与实际磁盘不一致。
    db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    db.execute("VACUUM")
    after_size = DB.stat().st_size
    print(f"   {before_size / 1048576:.2f} MB → {after_size / 1048576:.2f} MB"
          f"（回收 {(before_size - after_size) / 1048576:.2f} MB）")

    # ── 清理报告 ───────────────────────────────────────────────────────────
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S")
    out = ROOT / "data" / f"cleanup-{time.strftime('%Y-%m-%d')}.json"
    out.write_text(
        json.dumps(
            {
                "at": stamp,
                "days": args.days,
                "conversations": len(ids),
                "deleted": deleted,
                "memories_kept": kept,
                "memories_deleted": [r["key"] for r in doomed],
                "size_before": before_size,
                "size_after": after_size,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"清理报告已写入 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
