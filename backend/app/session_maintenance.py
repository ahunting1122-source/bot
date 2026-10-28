"""会话维护的真实现（MATRIX 10.13 的 10 条命令）。

## 为什么单独一个模块，而不是直接写在 CLI handler 里

这一层做的是**动数据**的事：VACUUM、删行、改状态、建表。写进 CLI handler 里就没法
脱离命令行验证（要么只能靠读 stdout 猜，要么只能靠"跑一遍看看有没有炸"）。所以这里
只做"算 + 改 + 返回结构化报告"，一个 print 都没有；打印与 ``--json`` 由
``app/cli/commands/session_extra3.py`` 负责。

## 诚实三件套（每条命令都必须满足）

1. **默认 dry-run**：所有**破坏性**维护操作默认只算不改（`sessions archive` 不在此列：它只打标记、不删消息，所以默认就是真写），并把"将改什么、改多少行"打出来；
2. **``apply=True`` 时报告实际改了多少行**：不是"将改"，是 `cursor.rowcount` 的真实值；
3. **绝不静默改数据**：每一行被改的记录都进报告（任务 id / 消息 id / 新值），
   让人能逐行核对，而不是只给一个总数。

## 与声明表（``hermes_parity.py``）help 文本的语义差异——必须知道

| 命令 | 声明表写的是 | 本实现做的是 | 为什么 |
|---|---|---|---|
| ``optimize`` | 合并 FTS 段并 VACUUM | 三个库各 ``PRAGMA optimize`` + ``VACUUM`` | 一致（顺带把 checkpoints/fts 也收一遍） |
| ``clean-markers`` | 清理残留的**工具调用**标记 | 清理**上下文压缩**标记 | ``app/session_search.py`` 的 ``_COMPACTION_PREFIXES`` 是唯一权威来源 |
| ``optimize-storage`` | 迁移搜索索引到紧凑布局 | ``VACUUM INTO`` 重建 + 清理过期 ``task_events`` | 索引布局由 alembic 管，本层不该动；能省的字节主要在事件表 |
| ``repair`` | 修复 ``state.db`` 结构 | ``integrity_check`` + ``foreign_key_check`` 只报，``--apply`` 只 REINDEX | 自动改数据库是危险动作，不做 |
| ``repair-routing`` | 重写丢失路由的**网关会话** | 找出 ``tasks.conversation_id`` 悬空的行 | 本仓库的路由真值就是这张外键 |
| ``recover`` | 把会话数据重建到干净目录 | 把僵尸 ``RUNNING`` 任务标成 ``FAILED`` | 见下 |
| ``retitle-skills`` | 按技能重命名**会话**标题 | 按 frontmatter 的 ``name`` 重写**技能索引**名 | 见下 |

``recover`` 与 ``retitle-skills`` 是刻意换掉的语义：Hermes 那两条分别依赖"按来源重建
会话目录"和"从会话正文反推技能"，本仓库都没有对应的真值来源；硬做只能造假数据。
换成语义后它们各自有一条可验证的真值：``tasks.status`` 与 SKILL.md 的 frontmatter。

## 表结构与迁移

``session_pins`` 是**本模块自己建的表**（``CREATE TABLE IF NOT EXISTS``，幂等）。
为什么不写进 ``app/storage.py``：那张表一旦进 ``Base.metadata``，就要一条 alembic 迁移
才能让老库跟上；而"钉住会话"只是一个纯附加的偏好表，没有外键依赖、没有读侧契约，
用幂等 DDL 建反而更安全——建不出来时只影响 pin 系列命令，不影响任何既有功能。
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select, text

from app import skills as skills_mod
from app.session_search import _COMPACTION_PREFIXES  # noqa: SLF001 —— 权威来源，只读不改
from app.storage import Message, Skill, Task, now

#: 需要维护的库（顺序即报告顺序）。三个库都在 ``<data_dir>`` 下，缺哪个就跳过哪个。
DATABASE_NAMES = ("app.sqlite", "checkpoints.sqlite", "fts.sqlite")

#: ``optimize-storage`` 默认保留多少天的 ``task_events``。
DEFAULT_KEEP_DAYS = 30

#: ``recover`` 判定僵尸任务的默认阈值（分钟）。
DEFAULT_STALE_MINUTES = 30

#: 钉住表。三列：主键会话 id、钉住时间、备注。没有外键——会话被删掉后 pin 要能被
#: ``pinned`` 检出来（"悬空 pin"），有外键就会让删除失败或级联删掉证据。
PIN_TABLE = "session_pins"
PIN_DDL = (
    f"CREATE TABLE IF NOT EXISTS {PIN_TABLE}"
    "(session_id TEXT PRIMARY KEY, pinned_at TEXT, note TEXT)"
)


class MaintenanceError(RuntimeError):
    """可预期的维护失败（库打不开、表不存在、名字有歧义）。

    与"真 bug"分开：CLI 侧翻成一行可行动的消息，不打栈。
    """


# ── 时间小工具 ──────────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def parse_timestamp(raw: str) -> datetime | None:
    """解析库里存的时间戳。解析不了返回 ``None``（坏时间戳不该让整条命令崩）。"""
    text_value = (raw or "").strip()
    if not text_value:
        return None
    try:
        parsed = datetime.fromisoformat(text_value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# ── sqlite 文件级小工具 ─────────────────────────────────────────────────────


def _connect(path: Path) -> sqlite3.Connection:
    """打开库。``isolation_level=None`` 是硬要求：``VACUUM`` 不能在事务里跑。"""
    return sqlite3.connect(str(path), isolation_level=None, timeout=20)


def database_bytes(path: Path) -> int:
    """一个库**实际占盘**多少字节（主文件 + WAL + SHM）。

    只算主文件会骗人：WAL 模式下刚删完的行还躺在 ``-wal`` 里，
    报出来的"省了多少"就会是负的。
    """
    total = 0
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.is_file():
            total += candidate.stat().st_size
    return total


def _integrity_ok(connection: sqlite3.Connection) -> list[str]:
    return [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]


def rebuild_database(path: Path) -> int:
    """用 ``VACUUM INTO`` 重建一个库，返回重建后的字节数。

    为什么不直接 ``VACUUM``：``VACUUM INTO`` 先写一个全新文件，成功且校验通过才
    原子替换；直接 VACUUM 半途失败（磁盘满、被锁）会留下半新半旧的库。
    这里宁可失败时保留旧库——维护命令没有"尽力而为地损坏数据"这个选项。
    """
    temporary = path.parent / f".{path.name}.rebuild"
    temporary.unlink(missing_ok=True)
    connection = _connect(path)
    try:
        # 先把 WAL 并回主文件：否则 VACUUM INTO 导出的快照可能少最后几笔提交。
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM INTO ?", (str(temporary),))
    finally:
        connection.close()
    check = _connect(temporary)
    try:
        problems = _integrity_ok(check)
    finally:
        check.close()
    if problems != ["ok"]:
        temporary.unlink(missing_ok=True)
        raise MaintenanceError(f"重建结果未通过 integrity_check：{problems[:3]}")
    # 旧库的 -wal/-shm 属于旧文件，留着会和新主文件不匹配（SQLite 会当成脏 WAL）。
    for suffix in ("-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    os.replace(temporary, path)
    return path.stat().st_size


def backup_database(db_path: Path, data_dir: Path) -> Path:
    """用 ``VACUUM INTO`` 备份一个库，返回备份文件路径。

    不用 ``shutil.copy2``：WAL 模式下主文件可能比真实数据旧，复制出来的备份会
    少最后几笔提交——那种备份最坏，因为它看起来是好的。
    """
    target_dir = Path(data_dir) / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    stamp = _now().strftime("%Y%m%d-%H%M%S-%f")
    target = target_dir / f"{Path(db_path).stem}-{stamp}.sqlite"
    connection = _connect(Path(db_path))
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM INTO ?", (str(target),))
    finally:
        connection.close()
    return target


# ── 1. optimize ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DatabaseSize:
    """一个库整理前后的体积。``note`` 非空表示这次没整理（并说明为什么）。"""

    name: str
    path: str
    before: int
    after: int
    note: str = ""

    @property
    def saved(self) -> int:
        return max(0, self.before - self.after)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "before": self.before,
            "after": self.after,
            "saved": self.saved,
            "note": self.note,
        }


@dataclass(frozen=True)
class OptimizeReport:
    databases: tuple[DatabaseSize, ...]

    @property
    def total_before(self) -> int:
        return sum(item.before for item in self.databases)

    @property
    def total_after(self) -> int:
        return sum(item.after for item in self.databases)

    @property
    def total_saved(self) -> int:
        return max(0, self.total_before - self.total_after)

    def as_dict(self) -> dict[str, Any]:
        return {
            "databases": [item.as_dict() for item in self.databases],
            "total_before": self.total_before,
            "total_after": self.total_after,
            "total_saved": self.total_saved,
        }


def optimize(data_dir: Path) -> OptimizeReport:
    """对三个库逐个 ``PRAGMA optimize`` + ``VACUUM``，报告前后字节数。

    这条命令唯一的价值就是**让人看见省了多少**：没有前后字节数的 VACUUM 报告
    等于什么都没说。所以哪怕一个字节都没省，也要把三个数（前/后/省）都报出来。
    """
    results: list[DatabaseSize] = []
    for name in DATABASE_NAMES:
        path = Path(data_dir) / name
        if not path.is_file():
            results.append(DatabaseSize(name, str(path), 0, 0, "文件不存在，跳过"))
            continue
        before = database_bytes(path)
        try:
            connection = _connect(path)
            try:
                # `PRAGMA optimize` 按查询统计重算索引/统计信息，VACUUM 才回收页。
                connection.execute("PRAGMA optimize")
                connection.execute("VACUUM")
            finally:
                connection.close()
        except sqlite3.Error as exc:
            results.append(DatabaseSize(name, str(path), before, before, f"整理失败：{exc}"))
            continue
        results.append(DatabaseSize(name, str(path), before, database_bytes(path)))
    return OptimizeReport(tuple(results))


# ── 2. optimize-storage ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class StorageReport:
    """``optimize-storage`` 的报告。

    ``after`` 在 dry-run 下是**实测预估**：把库导出一份副本、在副本上真的删一遍再
    VACUUM，量副本的大小。比"估算"诚实——用户要看的就是"删了到底能省多少"。
    """

    path: str
    before: int
    after: int
    cutoff: str
    keep_days: int
    events_total: int
    pruned: int
    kept: int
    apply: bool
    vacuum: bool
    backup: str = ""
    note: str = ""

    @property
    def saved(self) -> int:
        return max(0, self.before - self.after)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "before": self.before,
            "after": self.after,
            "saved": self.saved,
            "cutoff": self.cutoff,
            "keep_days": self.keep_days,
            "events_total": self.events_total,
            "pruned": self.pruned,
            "kept": self.kept,
            "apply": self.apply,
            "vacuum": self.vacuum,
            "backup": self.backup,
            "note": self.note,
        }


def _count_events(connection: sqlite3.Connection, cutoff: str) -> tuple[int, int]:
    """``(总行数, 早于 cutoff 的行数)``。``created_at`` 是 ISO 字符串，可直接比较。"""
    total = int(connection.execute("SELECT count(*) FROM task_events").fetchone()[0])
    old = int(
        connection.execute(
            "SELECT count(*) FROM task_events WHERE created_at < ?", (cutoff,)
        ).fetchone()[0]
    )
    return total, old


def optimize_storage(
    data_dir: Path,
    *,
    apply: bool = False,
    keep_days: int = DEFAULT_KEEP_DAYS,
    vacuum: bool = True,
    backup: bool = True,
) -> StorageReport:
    """``VACUUM INTO`` 重建 + 清理过期 ``task_events``（默认 dry-run）。

    只动 ``app.sqlite``：``checkpoints.sqlite`` 是 langgraph 的检查点真值，
    ``fts.sqlite`` 是检索索引（删行会漏召回）——两者的行都不该按"多久以前"删。
    """
    path = Path(data_dir) / "app.sqlite"
    days = max(0, int(keep_days))
    cutoff = _iso(_now() - timedelta(days=days))
    if not path.is_file():
        return StorageReport(
            str(path), 0, 0, cutoff, days, 0, 0, 0, apply, vacuum, note="app.sqlite 不存在，无事可做"
        )

    before = database_bytes(path)
    connection = _connect(path)
    try:
        total, old = _count_events(connection, cutoff)
    finally:
        connection.close()

    if not apply:
        # dry-run：在**副本**上真删一遍，量出"删完到底多大"。原库一个字节都不动。
        after = before
        note = "dry-run：原库未改动，体积按副本实测"
        if old:
            probe = path.parent / f".{path.name}.preview"
            probe.unlink(missing_ok=True)
            source = _connect(path)
            try:
                source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                source.execute("VACUUM INTO ?", (str(probe),))
            finally:
                source.close()
            try:
                copy = _connect(probe)
                try:
                    copy.execute("DELETE FROM task_events WHERE created_at < ?", (cutoff,))
                    if vacuum:
                        copy.execute("VACUUM")
                finally:
                    copy.close()
                after = database_bytes(probe)
            finally:
                probe.unlink(missing_ok=True)
                Path(f"{probe}-wal").unlink(missing_ok=True)
                Path(f"{probe}-shm").unlink(missing_ok=True)
        # `pruned` 在 dry-run 下是"将删多少行"——同一个字段两种模式语义一致，
        # 打印层不用再区分"将删/已删"两套算术（那种算术最容易把数字算错）。
        return StorageReport(
            str(path), before, after, cutoff, days, total, old, total - old, False, vacuum, note=note
        )

    saved_to = ""
    if backup and old:
        saved_to = str(backup_database(path, data_dir))
    connection = _connect(path)
    try:
        deleted = 0
        if old:
            deleted = connection.execute(
                "DELETE FROM task_events WHERE created_at < ?", (cutoff,)
            ).rowcount
    finally:
        connection.close()
    after = before
    if vacuum and deleted:
        after = rebuild_database(path)
    elif not vacuum:
        after = database_bytes(path)
    return StorageReport(
        str(path),
        before,
        after,
        cutoff,
        days,
        total,
        int(deleted or 0),
        max(0, total - int(deleted or 0)),
        True,
        vacuum,
        backup=saved_to,
        note="" if deleted else "没有过期行可删",
    )


# ── 3. repair ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RepairCheck:
    """一个库的两项只读检查（``integrity_check`` / ``foreign_key_check``）。"""

    database: str
    path: str
    integrity: tuple[str, ...]
    foreign_keys: tuple[str, ...]
    indexes: int = 0
    note: str = ""

    @property
    def ok(self) -> bool:
        return not self.note and self.integrity == ("ok",) and not self.foreign_keys

    def as_dict(self) -> dict[str, Any]:
        return {
            "database": self.database,
            "path": self.path,
            "integrity": list(self.integrity),
            "foreign_keys": list(self.foreign_keys),
            "indexes": self.indexes,
            "ok": self.ok,
            "note": self.note,
        }


@dataclass(frozen=True)
class RepairReport:
    checks: tuple[RepairCheck, ...]
    apply: bool
    reindexed: int

    @property
    def ok(self) -> bool:
        return all(item.ok for item in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "checks": [item.as_dict() for item in self.checks],
            "apply": self.apply,
            "reindexed": self.reindexed,
            "ok": self.ok,
        }


def repair(data_dir: Path, *, apply: bool = False) -> RepairReport:
    """``integrity_check`` + ``foreign_key_check``，**只报告不自动改**。

    自动修数据库是危险动作：``REINDEX`` 之外的"修复"（重建表、丢坏行）都要先知道
    业务语义，猜错的代价是永久丢数据。所以 ``apply=True`` 只做一件绝对安全的事
    ——``REINDEX``（索引是纯派生数据，重建只会变对，不会变错）。
    """
    checks: list[RepairCheck] = []
    reindexed = 0
    for name in DATABASE_NAMES:
        path = Path(data_dir) / name
        if not path.is_file():
            checks.append(RepairCheck(name, str(path), (), (), note="文件不存在，跳过"))
            continue
        try:
            connection = _connect(path)
            try:
                integrity = tuple(_integrity_ok(connection))
                foreign = tuple(
                    str(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()
                )
                indexes = int(
                    connection.execute(
                        "SELECT count(*) FROM sqlite_master WHERE type='index'"
                    ).fetchone()[0]
                )
                if apply:
                    connection.execute("REINDEX")
                    reindexed += indexes
            finally:
                connection.close()
        except sqlite3.Error as exc:
            checks.append(RepairCheck(name, str(path), (), (), note=f"检查失败：{exc}"))
            continue
        checks.append(RepairCheck(name, str(path), integrity, foreign, indexes))
    return RepairReport(tuple(checks), apply, reindexed)


# ── 4. repair-routing ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class RoutingRow:
    task_id: str
    conversation_id: str
    goal: str
    action: str
    new_conversation_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "conversation_id": self.conversation_id,
            "goal": self.goal,
            "action": self.action,
            "new_conversation_id": self.new_conversation_id,
        }


@dataclass(frozen=True)
class RoutingReport:
    tasks: int
    orphans: tuple[RoutingRow, ...]
    latest: str
    apply: bool
    changed: int
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tasks": self.tasks,
            "orphans": [item.as_dict() for item in self.orphans],
            "latest": self.latest,
            "apply": self.apply,
            "changed": self.changed,
            "note": self.note,
        }


async def repair_routing(store: Any, *, apply: bool = False) -> RoutingReport:
    """找出 ``tasks.conversation_id`` 指向不存在会话的行，``apply`` 时重挂到最近会话。

    **逐行打印将改什么**：这类修复最容易出的错是"顺手挂到错误的会话上"，
    所以报告里带上每个任务的旧值/新值/目标标题前缀，改完能一条条核对。

    为什么是"最近会话"而不是置空：``tasks.conversation_id`` 是 NOT NULL 外键，
    置空会被约束拒绝（而且会让任务从所有会话视图里消失）。库里连一个会话都没有时
    无从重挂，此时如实报"无法修复"，不做任何改动。
    """
    try:
        async with store.sessions() as session:
            total = int((await session.execute(text("SELECT count(*) FROM tasks"))).scalar() or 0)
            latest = (
                await session.execute(
                    text("SELECT id FROM conversations ORDER BY updated_at DESC, created_at DESC LIMIT 1")
                )
            ).scalar()
            rows = (
                await session.execute(
                    text(
                        "SELECT t.id, t.conversation_id, t.goal FROM tasks t "
                        "LEFT JOIN conversations c ON c.id = t.conversation_id "
                        "WHERE c.id IS NULL ORDER BY t.created_at, t.id"
                    )
                )
            ).all()
    except Exception as exc:  # noqa: BLE001 —— 库结构缺失/打不开都翻成一行可行动的话
        raise MaintenanceError(f"读不了任务表：{exc}") from exc

    latest_id = str(latest or "")
    fixes: list[RoutingRow] = []
    changed = 0
    if not rows:
        return RoutingReport(total, (), latest_id, apply, 0, note="没有悬空路由")
    if not latest_id:
        for row in rows:
            fixes.append(RoutingRow(str(row[0]), str(row[1]), str(row[2] or ""), "无法修复"))
        return RoutingReport(
            total, tuple(fixes), "", apply, 0, note="库里一个会话都没有，无从重挂（未改动任何行）"
        )
    async with store.sessions() as session:
        for row in rows:
            task_id = str(row[0])
            if apply:
                result = await session.execute(
                    text("UPDATE tasks SET conversation_id = :new WHERE id = :id"),
                    {"new": latest_id, "id": task_id},
                )
                changed += int(result.rowcount or 0)
            fixes.append(
                RoutingRow(task_id, str(row[1]), str(row[2] or ""), "重挂到最近会话", latest_id)
            )
        if apply:
            await session.commit()
    return RoutingReport(total, tuple(fixes), latest_id, apply, changed)


# ── 5. recover ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ZombieTask:
    task_id: str
    goal: str
    status: str
    updated_at: str
    idle_minutes: float
    action: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status,
            "updated_at": self.updated_at,
            "idle_minutes": round(self.idle_minutes, 1),
            "action": self.action,
        }


@dataclass(frozen=True)
class RecoverReport:
    stale_minutes: int
    cutoff: str
    running: int
    zombies: tuple[ZombieTask, ...]
    apply: bool
    changed: int
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "stale_minutes": self.stale_minutes,
            "cutoff": self.cutoff,
            "running": self.running,
            "zombies": [item.as_dict() for item in self.zombies],
            "apply": self.apply,
            "changed": self.changed,
            "note": self.note,
        }


async def recover(
    store: Any, *, apply: bool = False, stale_minutes: int = DEFAULT_STALE_MINUTES
) -> RecoverReport:
    """把"卡在 RUNNING 且很久没动"的僵尸任务标成 ``FAILED``。

    ``tasks`` 表**没有 ``end_reason`` 列**（``app/storage.py`` 是禁改区，也不该为了
    一条命令加迁移），所以 ``end_reason='recovered'`` 落到两处可见的地方：
    ``tasks.error`` 写成 ``recovered``，``tasks.waiting`` 这个 JSON 列里加
    ``{"end_reason": "recovered"}``，并写一条 ``recovered`` 事件进 ``task_events``。
    三处都是真值，任何读侧都能看见，不靠"内存里标记一下"。
    """
    minutes = max(1, int(stale_minutes))
    cutoff = _iso(_now() - timedelta(minutes=minutes))
    try:
        async with store.sessions() as session:
            rows = list((await session.scalars(select(Task).where(Task.status == "RUNNING"))).all())
    except Exception as exc:  # noqa: BLE001
        raise MaintenanceError(f"读不了任务表：{exc}") from exc

    zombies: list[ZombieTask] = []
    changed = 0
    current = _now()
    for task in rows:
        stamp = parse_timestamp(str(task.updated_at or ""))
        if stamp is None:
            # 时间戳坏掉的 RUNNING 任务也是僵尸（永远等不到超时判定），要报出来。
            idle = float("inf")
        else:
            idle = (current - stamp).total_seconds() / 60.0
        if idle <= minutes:
            continue
        zombies.append(
            ZombieTask(
                task_id=task.id,
                goal=str(task.goal or "")[:60],
                status=task.status,
                updated_at=str(task.updated_at or ""),
                idle_minutes=idle if idle != float("inf") else -1.0,
                action="置为 FAILED（end_reason=recovered）",
            )
        )
    if apply and zombies:
        ids = [item.task_id for item in zombies]
        # 必须在**新会话里重新取**一次对象：上面那批 Task 是上一个 session 里读出来的，
        # 那个 session 已经关了，直接改它们的属性不会写进库（改了个没人管的游离对象）。
        async with store.sessions() as session:
            for task_id in ids:
                task = await session.get(Task, task_id)
                if task is None:
                    continue
                task.status = "FAILED"
                task.error = "recovered"
                waiting = dict(task.waiting or {})
                waiting["end_reason"] = "recovered"
                waiting["recovered_at"] = _iso(current)
                task.waiting = waiting
                task.updated_at = now()
                changed += 1
            await session.commit()
        for task_id in ids:
            await store.event(task_id, "recovered", end_reason="recovered", previous_status="RUNNING")
    return RecoverReport(minutes, cutoff, len(rows), tuple(zombies), apply, changed)


# ── 6. clean-markers ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MarkerHit:
    message_id: str
    conversation_id: str
    marker: str
    action: str
    before_chars: int
    after_chars: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "marker": self.marker,
            "action": self.action,
            "before_chars": self.before_chars,
            "after_chars": self.after_chars,
        }


@dataclass(frozen=True)
class MarkerReport:
    scanned: int
    hits: tuple[MarkerHit, ...]
    apply: bool
    stripped: int
    deleted: int
    backup: str = ""
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "scanned": self.scanned,
            "hits": [item.as_dict() for item in self.hits],
            "apply": self.apply,
            "stripped": self.stripped,
            "deleted": self.deleted,
            "backup": self.backup,
            "note": self.note,
        }


def split_marker(content: str) -> tuple[str, str]:
    """把消息正文拆成 ``(标记行, 其余正文)``。

    判定口径与 ``app/session_search.py::_is_compaction_summary`` **完全一致**：只认
    正文**开头**的标记。中段出现的同名字符串可能是用户自己在讨论这件事，删掉就是
    破坏用户内容。
    """
    text_value = (content or "").lstrip("\ufeff \t\r\n")
    line, _, rest = text_value.partition("\n")
    return line, rest


def marker_of(content: str) -> str:
    """命中的是哪个前缀（没命中返回空串）。"""
    stripped = (content or "").lstrip("\ufeff \t\r\n")
    for prefix in _COMPACTION_PREFIXES:
        if stripped.startswith(prefix):
            return prefix
    return ""


async def clean_markers(
    store: Any, *, apply: bool = False, backup: Path | None = None
) -> MarkerReport:
    """清理正文开头的上下文压缩标记（默认 dry-run）。

    两种处理，逐行说明：**只有标记行**的消息整条删（它本来就是机器写的交接载荷，
    没有任何用户内容）；**标记行 + 正文**的消息只剥掉标记行（正文是用户/助手写的，
    一个字都不能丢）。
    """
    conditions = [Message.content.like(f"{prefix}%") for prefix in _COMPACTION_PREFIXES]
    try:
        async with store.sessions() as session:
            scanned = int(
                (await session.execute(text("SELECT count(*) FROM messages"))).scalar() or 0
            )
            rows = list((await session.scalars(select(Message).where(or_(*conditions)))).all())
    except Exception as exc:  # noqa: BLE001
        raise MaintenanceError(f"读不了消息表：{exc}") from exc

    hits: list[MarkerHit] = []
    stripped = deleted = 0
    for message in rows:
        content = message.content or ""
        marker = marker_of(content)
        if not marker:
            continue
        line, rest = split_marker(content)
        rest_text = rest.strip()
        action = "整条删除（只有标记行）" if not rest_text else "剥掉标记行"
        hits.append(
            MarkerHit(
                message_id=message.id,
                conversation_id=message.conversation_id,
                marker=marker,
                action=action,
                before_chars=len(content),
                after_chars=len(rest) if rest_text else 0,
            )
        )
        if not apply:
            continue
        if not rest_text:
            async with store.sessions() as session:
                existing = await session.get(Message, message.id)
                if existing is not None:
                    await session.delete(existing)
                    await session.commit()
                    deleted += 1
        else:
            message.content = rest
            await store.save(message)
            stripped += 1
    note = ""
    if not rows:
        note = "没有命中标记的消息"
    return MarkerReport(
        scanned,
        tuple(hits),
        apply,
        stripped,
        deleted,
        backup=str(backup) if backup else "",
        note=note,
    )


# ── 7. retitle-skills ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class SkillRename:
    directory: str
    old_name: str
    new_name: str
    action: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "directory": self.directory,
            "old_name": self.old_name,
            "new_name": self.new_name,
            "action": self.action,
        }


@dataclass(frozen=True)
class SkillReport:
    root: str
    files: int
    rows: int
    renames: tuple[SkillRename, ...]
    orphans: tuple[str, ...]
    apply: bool
    changed: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "files": self.files,
            "rows": self.rows,
            "renames": [item.as_dict() for item in self.renames],
            "orphans": list(self.orphans),
            "apply": self.apply,
            "changed": self.changed,
        }


async def retitle_skills(
    store: Any, root: Path, *, apply: bool = False, limit: int = 0
) -> SkillReport:
    """按 SKILL.md frontmatter 的 ``name`` 重写 ``skills`` 表的索引名。

    真值在文件（``frontmatter.name``），表只是索引；两边漂移时以文件为准。
    目录名与 frontmatter 不一致是常态（早期手写技能、外部拷进来的技能包），
    而 ``skills`` 表的读侧按 ``name`` 查，漂移会让"技能明明在磁盘上却查不到"。

    只读 ``app/skills.py`` 的接口（``load_skill``），不自己解析 YAML——两套解析器
    迟早给出两个答案。
    """
    base = Path(root)
    directories = sorted(item for item in base.iterdir() if item.is_dir()) if base.is_dir() else []
    if limit and limit > 0:
        directories = directories[:limit]
    try:
        async with store.sessions() as session:
            rows = list((await session.scalars(select(Skill))).all())
    except Exception as exc:  # noqa: BLE001
        raise MaintenanceError(f"读不了技能表：{exc}") from exc

    by_name = {row.name: row for row in rows}
    renames: list[SkillRename] = []
    orphans: list[str] = []
    for directory in directories:
        loaded = skills_mod.load_skill(base, directory.name)
        frontmatter = str(loaded.get("name") or "").strip()
        if not frontmatter:
            continue
        row = by_name.get(directory.name) or by_name.get(frontmatter)
        if row is None:
            orphans.append(directory.name)
            continue
        if row.name == frontmatter:
            continue
        if frontmatter in by_name and by_name[frontmatter] is not row:
            renames.append(
                SkillRename(directory.name, row.name, frontmatter, "跳过：目标名已被另一行占用")
            )
            continue
        renames.append(SkillRename(directory.name, row.name, frontmatter, "改名为 frontmatter.name"))

    changed = 0
    if apply:
        async with store.sessions() as session:
            for item in renames:
                if item.action.startswith("跳过"):
                    continue
                row = by_name.get(item.old_name)
                if row is None:
                    continue
                fresh = await session.get(Skill, row.id)
                if fresh is None:
                    continue
                fresh.name = item.new_name
                fresh.updated_at = now()
                changed += 1
            await session.commit()
    return SkillReport(str(base), len(directories), len(rows), tuple(renames), tuple(orphans), apply, changed)


# ── 8. pin / unpin / pinned ─────────────────────────────────────────────────


@dataclass(frozen=True)
class PinOutcome:
    pinned: tuple[str, ...]
    skipped: tuple[tuple[str, str], ...]
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "pinned": list(self.pinned),
            "skipped": [{"input": item[0], "reason": item[1]} for item in self.skipped],
            "note": self.note,
        }


@dataclass(frozen=True)
class PinnedEntry:
    session_id: str
    title: str
    pinned_at: str
    note: str
    dangling: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "title": self.title,
            "pinned_at": self.pinned_at,
            "note": self.note,
            "dangling": self.dangling,
        }


async def ensure_pin_table(store: Any) -> None:
    """建 ``session_pins`` 表（幂等）。不迁移、不改 ``storage.py``。"""
    async with store.sessions() as session:
        await session.execute(text(PIN_DDL))
        await session.commit()


def split_ids(raw: Any) -> list[str]:
    """把位置参数拆成多个 id：支持 ``a,b`` / ``a b`` / 多次传参。"""
    if raw is None:
        return []
    if isinstance(raw, str):
        items: Iterable[str] = [raw]
    else:
        items = [str(item) for item in raw]
    out: list[str] = []
    for item in items:
        for piece in str(item).replace(",", " ").split():
            if piece.strip():
                out.append(piece.strip())
    return out


async def resolve_sessions(store: Any, raws: Sequence[str]) -> tuple[list[str], list[tuple[str, str]]]:
    """把 id 或**唯一前缀**解析成会话 id。返回 ``(命中的 id, [(输入, 原因)])``。

    歧义前缀**不猜**：报出候选让人自己选。猜错的代价是钉住/删除另一个会话。
    """
    async with store.sessions() as session:
        rows = (
            await session.execute(text("SELECT id FROM conversations ORDER BY updated_at DESC"))
        ).all()
    known = [str(row[0]) for row in rows]
    resolved: list[str] = []
    skipped: list[tuple[str, str]] = []
    for raw in raws:
        if raw in known:
            resolved.append(raw)
            continue
        matches = [item for item in known if item.startswith(raw)]
        if len(matches) == 1:
            resolved.append(matches[0])
        elif not matches:
            skipped.append((raw, "没有这个会话"))
        else:
            skipped.append((raw, f"前缀有歧义：{'、'.join(item[:8] for item in matches[:5])}"))
    return resolved, skipped


async def pin(store: Any, raws: Sequence[str], *, note: str = "") -> PinOutcome:
    """钉住会话（``INSERT OR REPLACE``，重复钉只刷新时间与备注）。"""
    await ensure_pin_table(store)
    resolved, skipped = await resolve_sessions(store, raws)
    stamp = _iso(_now())
    async with store.sessions() as session:
        for session_id in resolved:
            await session.execute(
                text(
                    f"INSERT OR REPLACE INTO {PIN_TABLE}(session_id, pinned_at, note) "
                    "VALUES (:id, :at, :note)"
                ),
                {"id": session_id, "at": stamp, "note": note},
            )
        await session.commit()
    return PinOutcome(tuple(resolved), tuple(skipped))


async def unpin(store: Any, raws: Sequence[str]) -> PinOutcome:
    """取消钉住。**在 pin 表里**解析 id/前缀（不是会话表）。

    为什么要分开解析：悬空 pin 指向的会话已经不存在了，如果拿会话表解析，
    ``unpin`` 就永远删不掉它——而"清掉悬空 pin"正是 ``pinned`` 提示用户去做的事。
    """
    await ensure_pin_table(store)
    async with store.sessions() as session:
        pinned_ids = [
            str(row[0])
            for row in (
                await session.execute(text(f"SELECT session_id FROM {PIN_TABLE} ORDER BY pinned_at"))
            ).all()
        ]
    removed: list[str] = []
    skipped: list[tuple[str, str]] = []
    targets: list[str] = []
    for raw in raws:
        if raw in pinned_ids:
            targets.append(raw)
            continue
        matches = [item for item in pinned_ids if item.startswith(raw)]
        if len(matches) == 1:
            targets.append(matches[0])
        elif not matches:
            skipped.append((raw, "没有钉住过"))
        else:
            skipped.append((raw, f"前缀有歧义：{'、'.join(item[:8] for item in matches[:5])}"))
    async with store.sessions() as session:
        for session_id in targets:
            result = await session.execute(
                text(f"DELETE FROM {PIN_TABLE} WHERE session_id = :id"), {"id": session_id}
            )
            if int(result.rowcount or 0):
                removed.append(session_id)
            else:
                skipped.append((session_id, "没有钉住过"))
        await session.commit()
    return PinOutcome(tuple(removed), tuple(skipped))


async def pinned(store: Any) -> tuple[PinnedEntry, ...]:
    """列出被钉住的会话，并标出**悬空 pin**（指向已删除的会话）。"""
    await ensure_pin_table(store)
    async with store.sessions() as session:
        pins = (
            await session.execute(
                text(f"SELECT session_id, pinned_at, note FROM {PIN_TABLE} ORDER BY pinned_at DESC")
            )
        ).all()
        conversations = {
            str(row[0]): str(row[1] or "")
            for row in (await session.execute(text("SELECT id, title FROM conversations"))).all()
        }
    entries: list[PinnedEntry] = []
    for session_id, pinned_at, note in pins:
        key = str(session_id)
        entries.append(
            PinnedEntry(
                session_id=key,
                title=conversations.get(key, ""),
                pinned_at=str(pinned_at or ""),
                note=str(note or ""),
                dangling=key not in conversations,
            )
        )
    return tuple(entries)


__all__ = [
    "DATABASE_NAMES",
    "DEFAULT_KEEP_DAYS",
    "DEFAULT_STALE_MINUTES",
    "PIN_DDL",
    "PIN_TABLE",
    "DatabaseSize",
    "MaintenanceError",
    "MarkerHit",
    "MarkerReport",
    "OptimizeReport",
    "PinOutcome",
    "PinnedEntry",
    "RecoverReport",
    "RepairCheck",
    "RepairReport",
    "RoutingReport",
    "RoutingRow",
    "SkillRename",
    "SkillReport",
    "StorageReport",
    "ZombieTask",
    "backup_database",
    "clean_markers",
    "database_bytes",
    "ensure_pin_table",
    "marker_of",
    "optimize",
    "optimize_storage",
    "parse_timestamp",
    "pin",
    "pinned",
    "rebuild_database",
    "recover",
    "repair",
    "repair_routing",
    "resolve_sessions",
    "retitle_skills",
    "split_ids",
    "split_marker",
    "unpin",
]
