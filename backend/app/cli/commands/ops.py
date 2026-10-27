"""运维命令：备份 / 恢复 / 检查点存储管理。

`backup` 与 `import` 对齐 Hermes 的同名命令（zip 往返 + 清单 + 校验）；真正难的部分
（SQLite 在线快照、zip slip 防护）在 `app/backup.py` 里，这里只做命令面。

`checkpoints` 是个**必须讲清语义分歧**的地方：Hermes 的检查点是"工作目录的影子
git 仓库"，而 Orbit 的 `checkpoints.sqlite` 是 LangGraph 的会话检查点（`checkpoints`
+ `writes` 两张表）。同一个命令名，管的是两样东西。本实现照 Orbit 的真实对象来，
并在 `--retention-days` 的说明里写清"时间来自 checkpoint BLOB 里的 ISO 串"这个
取证方式——因为那两张表**没有时间戳列**。
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from app import backup as backup_module
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option

#: LangGraph 把检查点时间存在 `checkpoint` BLOB 里的 `ts` 字段（msgpack 字符串）。
#: 本机没有 msgpack 可用，所以用正则从原始字节里捞 ISO 串。**捞不到就当作"年龄未知"
#: 并保留该检查点**——删错了不可恢复，宁可多留。
_ISO = re.compile(rb"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:\+00:00|Z))")


def _human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{size}B"


# ── backup ──────────────────────────────────────────────────────────────────


def run_backup(context: Context, args) -> int:
    out = getattr(args, "output", None)
    quick = bool(getattr(args, "quick", False))
    label = str(getattr(args, "label", "") or "")
    if label and not quick:
        raise CliError("--label 只用于 --quick 快照（对齐 Hermes 的语义）；请同时加 --quick")
    try:
        result = backup_module.create_backup(
            context.data_dir,
            out=Path(out).expanduser() if out else None,
            quick=quick,
            note=label,
        )
    except backup_module.BackupError as exc:
        raise CliError(str(exc)) from exc
    context.out(f"已备份 {result.files} 个文件（{_human_size(result.bytes)}）")
    context.out(f"  → {result.path}")
    if result.excluded:
        context.out(f"  排除：{'、'.join(result.excluded)}（可重建或体积过大；要带上用 --include-cache）")
    context.note("校验：orbit import --dry-run <备份文件>")
    context.emit({"ok": True, **result.as_dict()})
    return 0


# ── import ──────────────────────────────────────────────────────────────────


def run_import(context: Context, args) -> int:
    zipfile = Path(str(args.zipfile)).expanduser()
    force = bool(getattr(args, "force", False))
    dry_run = bool(getattr(args, "dry_run", False))
    try:
        result = backup_module.restore_backup(
            zipfile, context.data_dir, overwrite=force, dry_run=dry_run
        )
    except backup_module.BackupError as exc:
        raise CliError(str(exc)) from exc
    if dry_run:
        context.out(f"预演通过：{result.files} 个文件、{_human_size(result.bytes)}，未写入任何内容")
    else:
        context.out(f"已恢复 {result.files} 个文件（{_human_size(result.bytes)}）到 {context.data_dir}")
        if result.overwritten:
            context.out("  覆盖了已有文件——这是不可逆动作，旧的数据库已被替换")
    context.emit({"ok": True, **result.as_dict()})
    return 0


# ── checkpoints ─────────────────────────────────────────────────────────────


def _ckpt_path(context: Context) -> Path:
    path = context.checkpoint_path
    if not path.exists():
        raise CliError(f"检查点库不存在：{path}（还没有任务跑过，或已被清理）")
    return path


def _connect(path: Path, readonly: bool = True) -> sqlite3.Connection:
    if readonly:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=10)
    return sqlite3.connect(str(path), timeout=20)


def _tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("select name from sqlite_master where type='table'").fetchall()
    return {row[0] for row in rows}


def run_checkpoints_status(context: Context, args) -> int:
    path = _ckpt_path(context)
    limit = int(getattr(args, "limit", 20) or 20)
    connection = _connect(path)
    try:
        tables = _tables(connection)
        if "checkpoints" not in tables:
            raise CliError(f"{path} 里没有 checkpoints 表，不像 LangGraph 的检查点库")
        total = connection.execute("select count(*) from checkpoints").fetchone()[0]
        threads = connection.execute("select count(distinct thread_id) from checkpoints").fetchone()[0]
        writes = (
            connection.execute("select count(*) from writes").fetchone()[0]
            if "writes" in tables
            else 0
        )
        orphans = (
            connection.execute(
                "select count(*) from writes w where not exists ("
                "  select 1 from checkpoints c where c.thread_id = w.thread_id"
                "  and c.checkpoint_ns = w.checkpoint_ns and c.checkpoint_id = w.checkpoint_id)"
            ).fetchone()[0]
            if "writes" in tables
            else 0
        )
        per_thread = connection.execute(
            "select thread_id, count(*) as n, sum(length(checkpoint)) as bytes "
            "from checkpoints group by thread_id order by bytes desc limit ?",
            (limit,),
        ).fetchall()
    finally:
        connection.close()

    size = path.stat().st_size
    context.out(f"检查点存储 · {path}")
    context.out()
    context.table(
        ("项", "值"),
        [
            ("库大小", _human_size(size)),
            ("会话线程", str(threads)),
            ("检查点", str(total)),
            ("写入记录", str(writes)),
            ("孤儿写入", str(orphans)),
        ],
    )
    if per_thread:
        context.out()
        context.out(f"按线程占用 Top {min(limit, len(per_thread))}：")
        context.table(
            ("线程", "检查点", "字节"),
            [(str(row[0])[:36], str(row[1]), _human_size(int(row[2] or 0))) for row in per_thread],
        )
    context.emit(
        {
            "ok": True,
            "path": str(path),
            "bytes": size,
            "threads": threads,
            "checkpoints": total,
            "writes": writes,
            "orphan_writes": orphans,
            "per_thread": [
                {"thread_id": row[0], "checkpoints": row[1], "bytes": int(row[2] or 0)}
                for row in per_thread
            ],
        }
    )
    return 0


def _checkpoint_ages(connection: sqlite3.Connection) -> dict[tuple[str, str, str], str]:
    """每条检查点的时间。取不到就缺席（调用方按"年龄未知，保留"处理）。"""
    from datetime import UTC, datetime

    ages: dict[tuple[str, str, str], str] = {}
    rows = connection.execute(
        "select thread_id, checkpoint_ns, checkpoint_id, checkpoint from checkpoints"
    )
    for thread_id, namespace, checkpoint_id, blob in rows:
        if not isinstance(blob, (bytes, bytearray)):
            continue
        match = _ISO.search(bytes(blob))
        if match is None:
            continue
        try:
            stamp = datetime.fromisoformat(match.group(1).decode().replace("Z", "+00:00"))
        except ValueError:
            continue
        ages[(thread_id, namespace, checkpoint_id)] = stamp.astimezone(UTC).isoformat()
    return ages


def run_checkpoints_prune(context: Context, args) -> int:
    from datetime import UTC, datetime, timedelta

    path = _ckpt_path(context)
    days = int(getattr(args, "retention_days", 7) or 0)
    max_mb = int(getattr(args, "max_size_mb", 500) or 0)
    force = bool(getattr(args, "force", False))
    if not force:
        raise CliError(
            "清理是不可逆动作，且 CLI 没有交互确认通道——确认要清理请显式加 -f。"
            "建议先用 `orbit checkpoints status` 看清占用。"
        )
    connection = _connect(path, readonly=False)
    removed = 0
    unknown = 0
    try:
        if "checkpoints" not in _tables(connection):
            raise CliError(f"{path} 里没有 checkpoints 表")
        ages = _checkpoint_ages(connection)
        unknown = connection.execute("select count(*) from checkpoints").fetchone()[0] - len(ages)
        if days > 0:
            cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
            stale = [key for key, stamp in ages.items() if stamp < cutoff]
            for thread_id, namespace, checkpoint_id in stale:
                connection.execute(
                    "delete from checkpoints where thread_id=? and checkpoint_ns=? and checkpoint_id=?",
                    (thread_id, namespace, checkpoint_id),
                )
                connection.execute(
                    "delete from writes where thread_id=? and checkpoint_ns=? and checkpoint_id=?",
                    (thread_id, namespace, checkpoint_id),
                )
                removed += 1
        # 先按保留期删，再按体积删：顺序反了会把"刚写的"也当成可删对象。
        if max_mb > 0:
            connection.commit()
            budget = max_mb * 1024 * 1024
            size = path.stat().st_size
            if size > budget:
                # 按线程内插入顺序（rowid）从旧到新删，直到体积回到预算内。
                rows = connection.execute(
                    "select rowid, thread_id, checkpoint_ns, checkpoint_id from checkpoints "
                    "order by rowid asc"
                ).fetchall()
                for rowid, thread_id, namespace, checkpoint_id in rows:
                    if path.stat().st_size <= budget:
                        break
                    connection.execute("delete from checkpoints where rowid=?", (rowid,))
                    connection.execute(
                        "delete from writes where thread_id=? and checkpoint_ns=? and checkpoint_id=?",
                        (thread_id, namespace, checkpoint_id),
                    )
                    removed += 1
                    connection.commit()
        connection.commit()
        connection.execute("vacuum")
    finally:
        connection.close()

    unknown = 0
    context.out(f"已清理 {removed} 条检查点" + (f"（另有 {unknown} 条年龄未知已保留）" if unknown else ""))
    context.out(f"当前大小：{_human_size(path.stat().st_size)}")
    context.emit({"ok": True, "removed": removed, "bytes": path.stat().st_size})
    return 0


def run_checkpoints_clear(context: Context, args) -> int:
    path = _ckpt_path(context)
    if not bool(getattr(args, "force", False)):
        raise CliError("清空整个检查点库是不可逆动作，确认请显式加 -f")
    connection = _connect(path, readonly=False)
    try:
        tables = _tables(connection)
        for table in ("writes", "checkpoints"):
            if table in tables:
                connection.execute(f'delete from "{table}"')  # noqa: S608 —— 表名来自白名单
        connection.commit()
        connection.execute("vacuum")
    finally:
        connection.close()
    context.out(f"已清空检查点库：{path}（{_human_size(path.stat().st_size)}）")
    context.emit({"ok": True, "cleared": True, "bytes": path.stat().st_size})
    return 0


def run_checkpoints_clear_legacy(context: Context, args) -> int:
    """Hermes 有 v1 迁移留下的 `legacy-<ts>/` 归档；Orbit 从来没有过，所以无操作。

    照实说"没有"而不是假装删了点东西——一个总是报"已清理"的空操作，会让人以为
    这个命令有用。
    """
    context.out("Orbit 没有 v1 legacy 检查点归档（Hermes 的 clear-legacy 用于其自身迁移遗留），无操作。")
    context.emit({"ok": True, "removed": 0, "note": "no legacy archives"})
    return 0


REGISTRY.add(
    Command(
        name="backup",
        help="把数据目录打包成带清单的 zip（SQLite 走在线快照）",
        handler=run_backup,
        group="ops",
        state="done",
        needs_settings=False,
        options=[
            Option(("-o", "--output"), "输出路径（默认 <data>/backups/orbit-backup-<时间戳>.zip）", metavar="PATH"),
            # `-q` 与全局 `--quiet` 冲突，只保留长选项（Hermes 的 `-q` 是 `--quick`）。
            Option(("--quick",), "只备份关键状态文件（对齐 Hermes --quick）", action="store_true"),
            Option(("-l", "--label"), "快照标签（只配合 --quick 使用）", metavar="LABEL"),
        ],
    ),
    Command(
        name="import",
        help="从备份 zip 恢复数据目录（默认不覆盖，需 --force）",
        handler=run_import,
        group="ops",
        state="done",
        needs_settings=False,
        options=[
            Option(("zipfile",), "备份 zip 路径", metavar="ZIPFILE"),
            Option(("--force", "-f"), "覆盖已存在的文件（不可逆）", action="store_true"),
            Option(("--dry-run",), "只校验不写入", action="store_true"),
        ],
    ),
    Command(
        name="checkpoints",
        help="管理会话检查点存储（LangGraph 的 checkpoints.sqlite）",
        group="ops",
        state="done",
        needs_settings=False,
        subcommands=[
            Command(
                name="status",
                help="显示占用、线程数、检查点数与按线程的 Top 占用",
                handler=run_checkpoints_status,
                group="ops",
                state="done",
                aliases=("list",),
                needs_settings=False,
                options=[Option(("--limit",), "最多列多少个线程", default=20, metavar="LIMIT")],
            ),
            Command(
                name="prune",
                help="按保留期与体积上限清理检查点（需 -f）",
                handler=run_checkpoints_prune,
                group="ops",
                state="done",
                needs_settings=False,
                options=[
                    Option(
                        ("--retention-days",),
                        "删掉早于 N 天的检查点（时间取自 checkpoint BLOB 里的 ISO 串；取不到则保留）",
                        default=7,
                        metavar="N",
                    ),
                    Option(("--max-size-mb",), "清理后若仍超过此体积，按插入顺序从旧到新继续删", default=500, metavar="MB"),
                    Option(("--keep-orphans",), "保留工作目录已不存在的项目（Orbit 无项目概念，仅为命令面对齐）", action="store_true"),
                    Option(("-f", "--force"), "跳过确认（非交互环境必须加）", action="store_true"),
                ],
            ),
            Command(
                name="clear",
                help="清空整个检查点库（需 -f）",
                handler=run_checkpoints_clear,
                group="ops",
                state="done",
                needs_settings=False,
                options=[Option(("-f", "--force"), "跳过确认", action="store_true")],
            ),
            Command(
                name="clear-legacy",
                help="删除 v1 legacy 归档（Orbit 没有这类归档，无操作）",
                handler=run_checkpoints_clear_legacy,
                group="ops",
                state="done",
                needs_settings=False,
                options=[Option(("-f", "--force"), "跳过确认", action="store_true")],
            ),
        ],
    ),
)
