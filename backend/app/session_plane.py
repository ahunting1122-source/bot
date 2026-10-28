"""统一 session 平面：交互 / cron / gateway 三个来源的会话，一条时间线看全。

对标 ``handoff/hermes-parity/research/03-persistence-layer.md`` §1.1 / §5.1 / §6
（MATRIX L10 的 10.1 / 10.2 / 10.4）。

## 为什么是「JSONL 追加日志 + 现有 app.sqlite 的读视图」，而不是新表

Hermes 的做法是 26 张业务表同库：``sessions``(56 列) + ``messages``(24 列) +
``delivery_obligations`` + ``compression_locks``…（实测证据：``state.db`` 40 张表、
``messages.id`` 用 ``AUTOINCREMENT`` 且 ``sqlite_sequence`` 的 ``max(id)=14953`` 大于行数
14,951，说明删过行但 id 不复用 → 可安全当回放游标）。Orbit 这一层**不能照抄**，理由三条：

1. **禁改区**。``app/storage.py`` 与 ``alembic/`` 迁移是本轮的铁律禁改区，加表就要改
   迁移脚本。而会话平面需要的字段（压缩谱系、事件类型、来源）在现有
   ``conversations`` / ``messages`` 里没有，硬塞会污染存储层。
2. **语义本来就适合追加日志**。平面事件（"谁压缩了谁"、"某会话被恢复"、"某次投递
   失败"）是**只追加、从不更新**的事实。用表来表达它，每次都要一条 UPDATE 语句和一次
   写锁；用 JSONL 表达它，写就是 ``O_APPEND`` 一行 + fsync。
3. **压缩谱系用父子引用比外键表更直接**。Hermes 用 ``sessions.parent_session_id``
   自引用 + ``end_reason='compression'``；在追加日志里一条事件同时带上
   ``session_id``（子）与 ``parent_session_id``（父），上溯就是查表 + 走链，
   不需要 join，也不需要先建好父行（Hermes 那边父行必须先存在，因为外键）。
   代价是**没有数据库级完整性**——所以 :meth:`SessionPlane.lineage` 自己做循环引用
   检测（脏数据不该让命令挂死）。

读侧仍然读 ``app.sqlite``：``messages`` 与 ``task_events`` 是既有真相源，平面不复制它们，
只在 :meth:`SessionPlane.timeline` 里与自己的事件**合并成一条时间线**。这就是"统一平面"
的全部价值：交互（messages）、cron / 任务（task_events）、平面事件（本模块）三个来源
在同一屏里按时间排好，谁也不用去猜另一处存了什么。

## id 续号的实现方式（10.2 的 Orbit 版）

``AppendLog`` 不靠内存计数器：进程重启后内存从 0 开始，新事件会与旧事件撞 id，
``since(cursor)`` 当场变成"永远返回全部"或"永远返回空"。做法是**进程内锁 + 文件尾读回**：
每次分配 id 前先读文件尾 64KB、倒着找最后一个可解析的 ``id``，与进程内缓存取较大值再 +1。
跨进程并发靠 ``O_APPEND`` 单行写入的原子性——同一微秒两个进程同时追加**有可能**拿到同一个
id，这一点如实标注，不假装它是分布式序号（Hermes 的 SQLite AUTOINCREMENT 才有那个保证；
要用它就必须新表，而新表是禁改区）。
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: 平面日志的落点：``<data_dir>/sessions/plane.jsonl``。
#: 放在 ``sessions/`` 而不是 ``data/`` 根：Hermes 的 ``sessions/`` 目录就是这个角色
#: （调研 03 §4.2），整目录备份/搬走时不会漏。
PLANE_DIRNAME = "sessions"
PLANE_FILENAME = "plane.jsonl"

#: 事件类型（``kind``）常量。留成裸字符串也能写，但这几个是**有语义的**，
#: 尤其是 ``session.compressed``——谱系就是靠它串起来的。
KIND_COMPRESSED = "session.compressed"
KIND_RESUMED = "session.resumed"
KIND_CREATED = "session.created"
KIND_IMPORTED = "session.imported"
KIND_PRUNED = "session.pruned"
#: 归档位（10.3）：Hermes 用 ``sessions.archived`` 标记位，Orbit 加不了列（``storage.py``
#: 是禁改区），所以用两条互为反向的平面事件表达"当前是否归档"——**最后一条说了算**。
#: 事件类型常量放在这里而不是 ``session_archive.py``：``kind`` 是平面日志的公共词表，
#: 散到各模块去定义，迟早出现两处拼写不一致的"归档事件"。
KIND_ARCHIVED = "session.archived"
KIND_UNARCHIVED = "session.unarchived"
#: 系统提示词冻结（10.5）：一条事件记 ``task_id → sha256``，正文落
#: ``<data_dir>/system-prompts/<hash>.txt``（见 ``app/session_archive.py``）。
KIND_PROMPT_FROZEN = "prompt.frozen"

#: 统一时间线的三个来源（也是排序并列时的次序）。
SOURCE_PLANE = "plane"
SOURCE_MESSAGE = "message"
SOURCE_TASK_EVENT = "task_event"

#: 谱系上溯的硬上限。循环检测之外的第二道闸：脏数据里若出现极长链（不是环），
#: 也不能让一条命令把 CLI 拖死。
MAX_LINEAGE_DEPTH = 100

#: 单条时间线条目的正文预算（字符）。时间线是"看全"视图，不是"读全文"视图——
#: 读全文有 ``session-search read``。超长内容在这里截断并保留 ``chars`` 原始长度。
TIMELINE_TEXT_CHARS = 500

#: 读文件尾找最后一个 id 的窗口。64KB 足够覆盖几十条事件，又不会在十万行日志上变慢。
_TAIL_BYTES = 65_536


class SessionPlaneError(RuntimeError):
    """可预期的平面错误（谱系循环、会话不存在等）。

    与"真 bug"区分开：CLI 侧把它翻成 ``CliError``（一行消息 + 返回码），不打栈。
    """


# ── 时间 ────────────────────────────────────────────────────────────────────


def now_iso(moment: float | None = None) -> str:
    """带时区的 ISO 时间串，**与 ``app.storage.now()`` 完全同格式**。

    为什么必须同格式：时间线要跨来源按时间排序，而 ``messages.created_at`` /
    ``task_events.created_at`` 是 ``storage.now()`` 写的 ``...+00:00`` 串。平面事件
    若用 epoch 浮点或本地时间（无时区），同一秒内两类来源的相对顺序就会错乱，
    而且 ``sorted()`` 会退化成"字符串比较"而不是时间比较。
    """
    if moment is None:
        return datetime.now(UTC).isoformat()
    return datetime.fromtimestamp(moment, UTC).isoformat()


def to_epoch(value: Any) -> float:
    """把"可能是 epoch 浮点 / ISO 串 / 空"的时间值折成 epoch 秒。

    坏值返回 ``0.0`` 而不是抛异常：一条脏记录不该让整条时间线消失（它会被排到最前，
    仍然可见——这比静默丢弃更诚实）。
    """
    if isinstance(value, bool):  # bool 是 int 的子类，先挡掉
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        # 无时区的历史数据按 UTC 解释：本机所有写入都是 UTC，猜本地时区会整体偏移。
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.timestamp()


# ── 追加日志 ────────────────────────────────────────────────────────────────


class AppendLog:
    """JSONL 追加日志：单行一条 JSON，``id`` 单调递增且**跨进程/重启续号**。

    三条不变量：

    - ``id`` 严格递增，且**不复用**（与 Hermes ``messages.id AUTOINCREMENT`` 同语义）；
    - 只追加，从不重写（流水账要留下已发生的部分）；
    - 坏行跳过，不抛异常（一个手抖写坏的半行不该让整个平面读不出来）。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        #: 进程内串行化"读尾 + 追加"。没有它，两个线程会读到同一个尾部 id。
        self._lock = threading.Lock()
        #: 进程内已知的最大 id。**只是加速缓存**：真正的权威永远是文件尾，
        #: 所以每次分配都重新读一次（见 :meth:`_next_locked`）。
        self._cached_last = 0

    # ── 读 ──
    def read_all(self) -> list[dict[str, Any]]:
        """按文件顺序读出全部事件（坏行跳过）。"""
        if not self.path.exists():
            return []
        events: list[dict[str, Any]] = []
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(item, dict):
                events.append(item)
        return events

    def since(self, cursor: int = 0, limit: int = 0) -> list[dict[str, Any]]:
        """**按 id 游标**增量读：返回 ``id > cursor`` 的事件。

        为什么按 id 而不按时间戳：同一毫秒内的事件时间戳相同，``ts > cursor_ts``
        会把它们一起漏掉或一起重复。Hermes 的 ``idx_messages_session_id``
        就是为这件事建的（调研 03 §1.1）。

        ``limit <= 0`` 表示不限量。
        """
        try:
            floor = int(cursor)
        except (TypeError, ValueError):
            floor = 0
        fresh = [item for item in self.read_all() if _as_int(item.get("id")) > floor]
        if limit and limit > 0:
            return fresh[:limit]
        return fresh

    def tail_events(self, count: int = 20) -> list[dict[str, Any]]:
        """最近 ``count`` 条事件（按文件顺序，返回正序）。"""
        events = self.read_all()
        if count <= 0:
            return events
        return events[-count:]

    def last_id(self) -> int:
        """当前最大 id（文件尾读回，不是内存计数）。"""
        with self._lock:
            return self._last_id_locked()

    def _last_id_locked(self) -> int:
        tail = self._last_id_from_tail()
        if tail is None:
            # 尾部窗口里没有一条完整可解析的行（文件刚被截断 / 单行超长 / 全坏）：
            # 退回全量扫描。慢，但只在异常路径上发生。
            tail = self._last_id_from_scan()
        self._cached_last = max(self._cached_last, tail)
        return self._cached_last

    def _last_id_from_tail(self) -> int | None:
        """读文件尾 ``_TAIL_BYTES`` 字节，倒着找最后一个带 ``id`` 的行。"""
        if not self.path.exists():
            return 0
        try:
            with self.path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - _TAIL_BYTES))
                chunk = handle.read()
        except OSError:
            return None
        for raw in reversed(chunk.split(b"\n")):
            if not raw.strip():
                continue
            try:
                item = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(item, dict) and "id" in item:
                value = _as_int(item.get("id"))
                if value > 0:
                    return value
        return None

    def _last_id_from_scan(self) -> int:
        best = 0
        for item in self.read_all():
            best = max(best, _as_int(item.get("id")))
        return best

    # ── 写 ──
    def append(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """追加一条事件，返回**真正落盘**的那一行（含分配的 ``id`` 与 ``ts``）。"""
        with self._lock:
            ident = self._last_id_locked() + 1
            self._cached_last = ident
            payload: dict[str, Any] = {"id": ident, "ts": now_iso()}
            # 调用方给的 id 被覆盖（id 由本类分配）；ts 允许调用方指定，且**保留其类型**
            # ——投递账本要写 epoch 浮点（与 gateway 对齐），时间线事件写 ISO 串。
            for key, value in record.items():
                if key in {"id", "ts"}:
                    continue
                payload[key] = value
            if record.get("ts"):
                payload["ts"] = record["ts"]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
                handle.flush()
                # fsync：追加日志的"至少一次"前提是它真的在盘上。少了这一步，
                # 断电后账本会缺行，而缺行意味着"已发生的事没留下痕迹"。
                os.fsync(handle.fileno())
            return payload


def _as_int(value: Any) -> int:
    """宽松整数转换（坏值 0）。``id`` 在坏数据里出现过 ``"12"`` 这种字符串。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ── 统一时间线条目 ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TimelineEntry:
    """统一时间线上的一条。``source`` 说明它来自哪个存储，``ref`` 是回跳用的 id。"""

    source: str
    ts: str
    kind: str
    ref: str
    session_id: str
    text: str
    chars: int = 0
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "ts": self.ts,
            "kind": self.kind,
            "ref": self.ref,
            "session_id": self.session_id,
            "text": self.text,
            "chars": self.chars,
            "detail": self.detail,
        }

    @property
    def sort_key(self) -> tuple[float, int, str]:
        """时间 → 来源次序 → ref。三级并列键保证**同一时间戳下顺序稳定**。"""
        return (to_epoch(self.ts), _SOURCE_RANK.get(self.source, 9), self.ref)


_SOURCE_RANK = {SOURCE_PLANE: 0, SOURCE_MESSAGE: 1, SOURCE_TASK_EVENT: 2}


def _clip(text: str, limit: int = TIMELINE_TEXT_CHARS) -> str:
    """截到 ``limit`` 字符，并如实标注被截断。"""
    raw = text or ""
    return raw if len(raw) <= limit else raw[:limit] + "…"


# ── 平面本体 ────────────────────────────────────────────────────────────────


class SessionPlane:
    """统一 session 平面：追加日志 + 压缩谱系 + 三来源合并时间线。

    ``store`` 参数一律**按需传入**（:meth:`timeline`），本类自己不建 ``Store``：
    这样纯文件操作（``append`` / ``since`` / ``lineage``）在没有数据库的机器上也能用，
    测试也不必为了测一行日志去迁移整个库。
    """

    def __init__(self, data_dir: Path, *, log: AppendLog | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.log = log or AppendLog(self.data_dir / PLANE_DIRNAME / PLANE_FILENAME)

    # ── 路径 ──
    @property
    def path(self) -> Path:
        return self.log.path

    # ── 写 ──
    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """追加一条平面事件。``kind`` 必填——没有类型的流水账没法查询。"""
        payload = dict(event)
        kind = str(payload.get("kind") or "").strip()
        if not kind:
            raise SessionPlaneError("平面事件必须有 kind（例如 session.compressed）")
        payload["kind"] = kind
        payload.setdefault("session_id", "")
        return self.log.append(payload)

    def compress(
        self,
        child_session_id: str,
        parent_session_id: str,
        *,
        reason: str = "compression",
        summary_chars: int = 0,
        summary: str = "",
        ts: str = "",
    ) -> dict[str, Any]:
        """记一次压缩：子会话 ← 父会话。

        一条事件同时携带 ``session_id``（子）与 ``parent_session_id``（父），
        谱系就是靠这一对串起来的（对应 Hermes 的 ``parent_session_id`` +
        ``end_reason='compression'``，调研 03 §5.4）。
        """
        if not child_session_id or not parent_session_id:
            raise SessionPlaneError("压缩事件需要子会话与父会话 id")
        event: dict[str, Any] = {
            "kind": KIND_COMPRESSED,
            "session_id": child_session_id,
            "parent_session_id": parent_session_id,
            "compressed_at": ts or now_iso(),
            "reason": reason,
            "summary_chars": int(summary_chars),
        }
        if summary:
            event["summary"] = summary
        if ts:
            event["ts"] = ts
        return self.append(event)

    # ── 读 ──
    def since(self, cursor: int = 0, limit: int = 0) -> list[dict[str, Any]]:
        """按 id 游标增量读（见 :meth:`AppendLog.since`）。"""
        return self.log.since(cursor, limit)

    def cursor(self) -> int:
        """当前最大 id：调用方保存它，下次用它做增量起点。"""
        return self.log.last_id()

    def events(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """全部事件，可按会话过滤（``None`` = 不过滤）。"""
        items = self.log.read_all()
        if session_id is None:
            return items
        return [item for item in items if str(item.get("session_id") or "") == session_id]

    def tail(self, session_id: str, n: int = 20) -> list[dict[str, Any]]:
        """某会话最近 ``n`` 条**平面事件**（正序返回）。"""
        items = self.events(session_id)
        if n and n > 0:
            return items[-n:]
        return items

    def parent_of(self, session_id: str) -> str | None:
        """该会话的父会话（没有则 ``None``）。同一会话被压缩多次时取**最后一条**。"""
        parent = None
        for item in self.log.read_all():
            if str(item.get("session_id") or "") != session_id:
                continue
            candidate = str(item.get("parent_session_id") or "").strip()
            if candidate:
                parent = candidate
        return parent

    # ── 压缩谱系 ──
    def lineage(self, session_id: str) -> list[dict[str, Any]]:
        """沿父链上溯到根：``[{session_id, parent, depth, summary_chars}]``。

        **循环引用必须检测并报错**：脏数据（手工编辑、两次压缩互相指向）不该让
        命令挂死在一个死循环里。判据是"本次上溯已经见过的 id 再次出现"，
        并且额外给 ``MAX_LINEAGE_DEPTH`` 兜底（不是环但极长的链也停下来）。
        """
        target = str(session_id or "").strip()
        if not target:
            raise SessionPlaneError("lineage 需要会话 id")

        parents: dict[str, str] = {}
        chars: dict[str, int] = {}
        for item in self.log.read_all():
            parent = str(item.get("parent_session_id") or "").strip()
            if not parent:
                continue
            child = str(item.get("session_id") or "").strip()
            if not child:
                continue
            # 后写的事件覆盖先写的：同一子会话被压缩两次时，最后一次才是当前谱系。
            parents[child] = parent
            chars[child] = _as_int(item.get("summary_chars"))

        chain: list[dict[str, Any]] = []
        seen: list[str] = []
        current = target
        while True:
            if current in seen:
                trail = " → ".join([*seen, current])
                raise SessionPlaneError(f"压缩谱系存在循环引用，拒绝上溯：{trail}")
            seen.append(current)
            up: str | None = parents.get(current)
            chain.append(
                {
                    "session_id": current,
                    "parent": up,
                    "depth": len(chain),
                    "summary_chars": chars.get(current, 0),
                }
            )
            if not up:
                return chain
            if len(chain) > MAX_LINEAGE_DEPTH:
                raise SessionPlaneError(
                    f"压缩谱系超过 {MAX_LINEAGE_DEPTH} 层，疑似脏数据，拒绝继续上溯"
                )
            # 必须走 `up`。这里早先写的是 `current = parent`——`parent` 是上面建
            # `parents` 映射时留下的循环变量，循环结束后它恒等于**最后一条事件的父会话**。
            # 结果每条谱系都在第二步就自我循环，被误判成"脏数据"直接报错。
            # 变量名只差一个字母，而症状是"所有谱系都坏了"，很难一眼看出来。
            current = up

    # ── 统一时间线 ──
    async def timeline(
        self,
        session_id: str,
        *,
        store: Any = None,
        limit: int = 0,
        sources: tuple[str, ...] = (SOURCE_PLANE, SOURCE_MESSAGE, SOURCE_TASK_EVENT),
    ) -> list[TimelineEntry]:
        """把三个来源合并成一条按时间排序的时间线。

        三个来源：

        - ``plane``：本模块的追加日志（压缩、恢复、导入、清理等平面事件）；
        - ``message``：``app.sqlite`` 的 ``messages``（交互与工具轨迹的真相源）；
        - ``task_event``：``app.sqlite`` 的 ``task_events``（cron / 任务编排的每一步）。

        ``store`` 为 ``None`` 时只返回平面事件——没有数据库的机器上这条命令仍然可用，
        而不是报错（"平面"不该比它合并的东西更脆弱）。
        """
        target = str(session_id or "").strip()
        if not target:
            raise SessionPlaneError("timeline 需要会话 id")

        entries: list[TimelineEntry] = []
        if SOURCE_PLANE in sources:
            for item in self.events(target):
                entries.append(
                    TimelineEntry(
                        source=SOURCE_PLANE,
                        ts=str(item.get("ts") or ""),
                        kind=str(item.get("kind") or ""),
                        ref=str(item.get("id") or ""),
                        session_id=target,
                        text=_clip(_plane_text(item)),
                        chars=len(_plane_text(item)),
                        detail=item,
                    )
                )
        if store is not None and SOURCE_MESSAGE in sources:
            entries.extend(await self._message_entries(store, target))
        if store is not None and SOURCE_TASK_EVENT in sources:
            entries.extend(await self._task_event_entries(store, target))

        entries.sort(key=lambda item: item.sort_key)
        if limit and limit > 0:
            return entries[-limit:]
        return entries

    async def _message_entries(self, store: Any, session_id: str) -> list[TimelineEntry]:
        """``messages`` 那一来源。只取该会话，按 ``created_at`` + ``id`` 排序（与
        ``session_search`` 的窗口切分同一条并列规则）。"""
        from sqlalchemy import select

        from app.storage import Message

        async with store.sessions() as session:
            rows = list(
                (
                    await session.scalars(
                        select(Message)
                        .where(Message.conversation_id == session_id)
                        .order_by(Message.created_at.asc(), Message.id.asc())
                    )
                ).all()
            )
        return [
            TimelineEntry(
                source=SOURCE_MESSAGE,
                ts=str(row.created_at or ""),
                kind=str(row.role or ""),
                ref=str(row.id or ""),
                session_id=session_id,
                text=_clip(row.content or ""),
                chars=len(row.content or ""),
                detail={"role": row.role, "created_at": row.created_at},
            )
            for row in rows
        ]

    async def _task_event_entries(self, store: Any, session_id: str) -> list[TimelineEntry]:
        """``task_events`` 那一来源。

        ``task_events`` 只挂 ``task_id``，会话归属在 ``tasks.conversation_id`` 上，
        所以要 join 一次——否则 cron 跑的每一步都进不了会话时间线，
        而"cron 的会话看不全"正是本层要解决的问题。
        """
        from sqlalchemy import select

        from app.storage import Task, TaskEvent

        async with store.sessions() as session:
            rows = (
                await session.execute(
                    select(TaskEvent)
                    .join(Task, Task.id == TaskEvent.task_id)
                    .where(Task.conversation_id == session_id)
                    .order_by(TaskEvent.created_at.asc(), TaskEvent.id.asc())
                )
            ).all()
        entries: list[TimelineEntry] = []
        for (event,) in rows:
            payload = event.payload if isinstance(event.payload, dict) else {"value": event.payload}
            text = json.dumps(payload, ensure_ascii=False)
            entries.append(
                TimelineEntry(
                    source=SOURCE_TASK_EVENT,
                    ts=str(event.created_at or ""),
                    kind=str(event.event_type or ""),
                    ref=str(event.id),
                    session_id=session_id,
                    text=_clip(text),
                    chars=len(text),
                    detail={"task_id": event.task_id, "payload": payload},
                )
            )
        return entries


def _plane_text(event: Mapping[str, Any]) -> str:
    """平面事件的显示文本：摘要优先，否则退化成"类型 + 其余字段"。"""
    summary = str(event.get("summary") or "").strip()
    if summary:
        return summary
    detail = {
        key: value
        for key, value in event.items()
        if key not in {"id", "ts", "kind", "session_id"}
    }
    return f"{event.get('kind')}" + (f" {json.dumps(detail, ensure_ascii=False)}" if detail else "")


__all__ = [
    "KIND_ARCHIVED",
    "KIND_COMPRESSED",
    "KIND_CREATED",
    "KIND_IMPORTED",
    "KIND_PRUNED",
    "KIND_PROMPT_FROZEN",
    "KIND_RESUMED",
    "KIND_UNARCHIVED",
    "MAX_LINEAGE_DEPTH",
    "PLANE_DIRNAME",
    "PLANE_FILENAME",
    "SOURCE_MESSAGE",
    "SOURCE_PLANE",
    "SOURCE_TASK_EVENT",
    "TIMELINE_TEXT_CHARS",
    "AppendLog",
    "SessionPlane",
    "SessionPlaneError",
    "TimelineEntry",
    "now_iso",
    "to_epoch",
]
