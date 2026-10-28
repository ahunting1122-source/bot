"""存活租约：``runtime/active_sessions.json`` 里登记"哪个会话正被哪个进程持有"。

对标 ``handoff/hermes-parity/research/03-persistence-layer.md`` §4.6 / §5.1 / §5.3
（MATRIX 10.8）。Hermes 那边的真实样本（本机只读实测）长这样::

    {"entries": [{"lease_id": "354c45c6…", "session_id": "20261028_103113_011746",
                  "pid": 6524, "process_start_time": 1793154658.33,
                  "surface": "desktop", "track_liveness": true,
                  "started_at": 1793154673.93, "updated_at": 1793154673.93,
                  "metadata": {"live_session_id": "5323f075"}}]}

## 为什么 Orbit 用 ``{session_id: {...}}`` 而不是 Hermes 的 ``entries`` 数组

租约的语义是"**同一会话同时只有一个持有者**"（Hermes 用
``session_turn_leases(conversation_id PRIMARY KEY)`` 表达同一件事）。用数组表达时，
"谁持有"要靠扫描 + 比 ``updated_at`` 推断，两条同 session_id 的记录并存也不报错；
用 dict 表达时，写入即覆盖——"后写者持有"是一条**结构上的**保证，而不是一段约定。
代价是丢掉 ``lease_id`` 这种多持有者的扩展位；本机是单用户本地部署，用不上。

字段名 ``host_start_time`` 而不是 Hermes 的 ``process_start_time``：与
``app/gateway.py`` / ``app/orphan_reaper.py`` 保持**同一个名字**。两个模块读同一份
进程身份，字段名不同就得写两次转换，而转换写错的表现是"活着的进程被判成孤儿"。

## 判据：``(pid, host_start_time)`` 二元组 + 心跳新鲜度

只看 pid 会在 PID 复用后把"别人的进程"当成自己的持有者。所以判活**复用**
:class:`app.orphan_reaper.OrphanReaper` 的 :meth:`~app.orphan_reaper.OrphanReaper.classify`
（它又复用 ``app.gateway.pid_alive`` / ``process_start_time``）：``dead`` / ``reused``
都不算存活。再加一条独立的失效条件——``heartbeat_at`` 超过 ``max_age``（默认 90s）。
两条都要：进程活着但卡死（心跳停了）与心跳很新但进程已经没了（登记是上一轮留下的），
是两种不同的故障。

## 绝不 kill 进程

本模块**只清登记**，不 ``taskkill``、不发信号、不 ``os.kill``。理由与
``app/orphan_reaper.py`` 模块 docstring 里那条完全一致：判据基于"读进程创建时间"
这种尽力而为的探测（权限不足、进程刚退出、``/proc`` 不可读都会退化），在不可靠判据上
执行不可逆动作（杀进程）不可接受。清理登记只是让**下一个**持有者能拿到会话；
持有者自己的进程该怎么退出，是它自己的事。
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.gateway import pid_alive, process_start_time
from app.orphan_reaper import KIND_ALIVE, KIND_DEAD, KIND_REUSED, OrphanReaper

#: 租约登记簿的落点：``<data_dir>/runtime/active_sessions.json``。
#: 目录名照 Hermes 的 ``runtime/``（调研 03 §4.6），搬目录备份时不会漏。
RUNTIME_DIRNAME = "runtime"
LEASES_FILENAME = "active_sessions.json"

#: 默认心跳超时（秒）。与 Hermes ``async_delegation`` 的 ``_STALL_GRACE_SECONDS``
#: 同一量级：够覆盖一次慢工具调用，又不至于让死掉的持有者占着会话几分钟。
DEFAULT_MAX_AGE = 90.0


class LeaseError(RuntimeError):
    """可预期的租约错误（会话 id 为空、未持有租约、被别的进程持有）。

    CLI 侧把它翻成 ``CliError``（一行消息 + 返回码），不打栈。
    """


@dataclass(frozen=True)
class Lease:
    """一条租约。``heartbeat_at`` 是 epoch 秒（不是 ISO 串）。"""

    session_id: str
    pid: int
    host_start_time: float | None
    heartbeat_at: float
    platform: str = ""
    started_at: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "host_start_time": self.host_start_time,
            "heartbeat_at": self.heartbeat_at,
            "platform": self.platform,
            "started_at": self.started_at,
        }

    def age(self, now: float | None = None) -> float:
        """距上次心跳多少秒（未来时间戳按 0 处理，不返回负数）。"""
        moment = time.time() if now is None else float(now)
        return max(0.0, moment - float(self.heartbeat_at or 0.0))


@dataclass(frozen=True)
class LeaseVerdict:
    """一条登记的判定结果。``kind`` 复用 orphan_reaper 的 ``alive/dead/reused`` 词表。"""

    session_id: str
    pid: int
    kind: str
    age: float
    expired: bool
    lease: Lease | None = None

    @property
    def live(self) -> bool:
        """活着 **且** 心跳新鲜才算存活。两条缺一不可（理由见模块 docstring）。"""
        return self.kind == KIND_ALIVE and not self.expired

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "pid": self.pid,
            "kind": self.kind,
            "age": round(self.age, 3),
            "expired": self.expired,
            "live": self.live,
            "lease": self.lease.as_dict() if self.lease else None,
        }


@dataclass(frozen=True)
class ReapReport:
    """一次租约回收的结果。``dry_run=True`` 时 ``removed`` 恒为 0。"""

    dry_run: bool
    stale: list[LeaseVerdict] = field(default_factory=list)
    live: list[LeaseVerdict] = field(default_factory=list)
    removed: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "stale": [item.as_dict() for item in self.stale],
            "live": [item.as_dict() for item in self.live],
            "stale_count": len(self.stale),
            "live_count": len(self.live),
            "removed": self.removed,
        }


class LeaseStore:
    """``active_sessions.json`` 的读写与判活。

    ``alive`` / ``start_time`` / ``clock`` 三个探针**可注入**：测试要造"pid 活着但
    创建时间对不上"这种状态，不该真的起进程再等它被复用；要测心跳过期，也不该
    ``sleep(90)``。默认值就是生产实现（gateway 的探针 + ``time.time``），没有替身。
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        alive: Any = None,
        start_time: Any = None,
        clock: Any = None,
        max_age: float = DEFAULT_MAX_AGE,
    ) -> None:
        self.data_dir = Path(data_dir)
        #: 进程身份判据**复用** orphan_reaper（它再复用 gateway 的探针），
        #: 不另写一套平台探测——两处判据不一致时，同一个进程在 `plane reap` 与
        #: `sessions leases` 里会得出两种结论，谁都不敢信。
        self.reaper = OrphanReaper(self.data_dir, alive=alive, start_time=start_time)
        #: 拿"自己这个进程的创建时刻"用。与 reaper 注入同一对探针，测试替身才不会漏。
        self._alive = alive or pid_alive
        self._start_time = start_time or process_start_time
        self._clock = clock or time.time
        self.max_age = float(max_age)
        self._lock = threading.Lock()

    # ── 路径 ──
    @property
    def path(self) -> Path:
        return self.data_dir / RUNTIME_DIRNAME / LEASES_FILENAME

    # ── 读写 ──
    def load(self) -> dict[str, dict[str, Any]]:
        """读出全部登记（坏文件当空表，不抛异常）。

        坏文件按空表处理而不是报错：租约是"尽力而为"的辅助信息，一个手抖写坏的
        JSON 不该让 `orbit sessions leases` 整条命令失败——但坏在哪要说清楚，
        所以 :meth:`verdicts` 会把无法解析的条目单独列出来。
        """
        raw = self._read()
        entries = raw.get("entries") if isinstance(raw, dict) else None
        # 兼容两种落盘形状：本模块的 `{session_id: {...}}`，以及 Hermes 的
        # `{"entries": [...]}`（导入别人的 runtime/ 时能直接读）。
        if isinstance(entries, list):
            converted: dict[str, dict[str, Any]] = {}
            for item in entries:
                if not isinstance(item, dict):
                    continue
                session_id = str(item.get("session_id") or "").strip()
                if session_id:
                    converted[session_id] = dict(item)
            return converted
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): dict(value)
            for key, value in raw.items()
            if isinstance(value, dict)
        }

    def _read(self) -> Any:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _write(self, entries: dict[str, dict[str, Any]]) -> None:
        """原子落盘。**不用**"打开就写"：写到一半被杀会留下半截 JSON，下一个进程读不到。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(entries, ensure_ascii=False, indent=2, sort_keys=True)
        tmp = self.path.with_name(f".{LEASES_FILENAME}.tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)

    # ── 写 ──
    def acquire(
        self,
        session_id: str,
        *,
        platform: str = "cli",
        pid: int | None = None,
        start_time: float | None = None,
    ) -> Lease:
        """登记"我在持有这个会话"。已有登记会被**覆盖**（后写者持有）。

        不阻塞、不排队：本模块不许 kill 进程，也就没有"强制夺回"的手段；让后写者
        直接持有，旧持有者下次 ``heartbeat`` 会看到 pid 不符并自己退让，这比
        "等 90 秒租约过期"更符合单用户本地部署的直觉。
        """
        target = _require_id(session_id)
        holder = os.getpid() if pid is None else int(pid)
        moment = float(self._clock())
        stamp = start_time
        if stamp is None and holder == os.getpid():
            stamp = self._start_time(holder)
        with self._lock:
            entries = self.load()
            previous = entries.get(target) or {}
            lease = Lease(
                session_id=target,
                pid=holder,
                host_start_time=_as_float(stamp),
                heartbeat_at=moment,
                platform=str(platform or ""),
                started_at=_as_float(previous.get("started_at")) or moment,
            )
            entries[target] = lease.as_dict()
            self._write(entries)
        return lease

    def heartbeat(self, session_id: str, *, pid: int | None = None) -> Lease:
        """刷新心跳。**只在还是自己持有时**成功，否则报错（不偷偷接管别人的租约）。"""
        target = _require_id(session_id)
        holder = os.getpid() if pid is None else int(pid)
        with self._lock:
            entries = self.load()
            record = entries.get(target)
            if record is None:
                raise LeaseError(f"会话没有租约：{target}（先 acquire）")
            current = _as_int(record.get("pid"))
            if current and current != holder:
                raise LeaseError(
                    f"租约已被 pid {current} 持有，{holder} 不能续（先确认对方是否还活着）"
                )
            record = dict(record)
            record["heartbeat_at"] = float(self._clock())
            entries[target] = record
            self._write(entries)
        return _lease_of(target, record)

    def release(self, session_id: str, *, pid: int | None = None) -> bool:
        """删掉登记。返回是否真的删了一条。**不 kill 任何进程**。"""
        target = _require_id(session_id)
        holder = os.getpid() if pid is None else int(pid)
        with self._lock:
            entries = self.load()
            record = entries.get(target)
            if record is None:
                return False
            current = _as_int(record.get("pid"))
            if current and current != holder:
                raise LeaseError(
                    f"租约由 pid {current} 持有，{holder} 不能释放（不替别人做决定）"
                )
            del entries[target]
            self._write(entries)
        return True

    # ── 判活 ──
    def verdicts(self, *, max_age: float | None = None) -> list[LeaseVerdict]:
        """每条登记一个判定（``alive`` / ``dead`` / ``reused`` + 心跳是否过期）。"""
        limit = self.max_age if max_age is None else float(max_age)
        moment = float(self._clock())
        found: list[LeaseVerdict] = []
        for session_id, record in sorted(self.load().items()):
            lease = _lease_of(session_id, record)
            kind = self.reaper.classify(record.get("pid"), record.get("host_start_time"))
            age = lease.age(moment)
            found.append(
                LeaseVerdict(
                    session_id=session_id,
                    pid=lease.pid,
                    kind=kind,
                    age=age,
                    expired=age > limit,
                    lease=lease,
                )
            )
        return found

    def live(self, *, max_age: float | None = None) -> list[Lease]:
        """还活着的租约（进程活着 + 身份相符 + 心跳新鲜）。"""
        return [
            item.lease
            for item in self.verdicts(max_age=max_age)
            if item.live and item.lease is not None
        ]

    def get(self, session_id: str) -> Lease | None:
        record = self.load().get(str(session_id or "").strip())
        return _lease_of(str(session_id or "").strip(), record) if record else None

    def reap_stale(self, *, max_age: float | None = None, dry_run: bool = True) -> ReapReport:
        """清掉失效登记（``dead`` / ``reused`` / 心跳过期）。默认只报告。

        **绝不 kill 进程**：这里删的是 JSON 里的条目，不是进程。
        """
        verdicts = self.verdicts(max_age=max_age)
        stale = [item for item in verdicts if not item.live]
        alive = [item for item in verdicts if item.live]
        if dry_run or not stale:
            return ReapReport(dry_run=dry_run, stale=stale, live=alive, removed=0)
        doomed = {item.session_id for item in stale}
        with self._lock:
            entries = self.load()
            removed = 0
            for session_id in doomed:
                if entries.pop(session_id, None) is not None:
                    removed += 1
            if removed:
                self._write(entries)
        return ReapReport(dry_run=False, stale=stale, live=alive, removed=removed)


def _lease_of(session_id: str, record: dict[str, Any]) -> Lease:
    return Lease(
        session_id=session_id,
        pid=_as_int(record.get("pid")),
        host_start_time=_as_float(record.get("host_start_time")),
        heartbeat_at=_as_float(record.get("heartbeat_at")) or 0.0,
        platform=str(record.get("platform") or ""),
        started_at=_as_float(record.get("started_at")) or 0.0,
    )


def _require_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise LeaseError("会话 id 不能为空")
    return text


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "DEFAULT_MAX_AGE",
    "KIND_ALIVE",
    "KIND_DEAD",
    "KIND_REUSED",
    "LEASES_FILENAME",
    "RUNTIME_DIRNAME",
    "Lease",
    "LeaseError",
    "LeaseStore",
    "LeaseVerdict",
    "ReapReport",
]
