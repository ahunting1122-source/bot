"""孤儿回收：清掉"登记过、但进程早已不在"的账本条目。

对标 ``handoff/hermes-parity/research/03-persistence-layer.md`` §5.3
（MATRIX L10 的 10.7 / 10.8）：Hermes 实测 ``sessions.end_reason`` 里
``startup_orphan_reap`` **67 条**、``ws_orphan_reap`` 3 条——即"进程启动时把上次崩溃
遗留的未正常结束的东西批量标记掉"。Orbit 侧对应的落点是
``<data_dir>/gateway/`` 下的三个登记簿：``processes.json`` / ``spawn-ledger.json`` /
``dispatcher.lock``。

## 判据：``(pid, host_start_time)`` 二元组，不是 pid

只看 pid 会在 **PID 复用**后把"别人的进程"当成自己的登记项——那正是
``app/gateway.py`` 模块 docstring 第 1 条要防的事故（Windows 上 ``os.kill(pid, 0)``
甚至会直接杀掉对方）。所以这里**复用** gateway 已经写好的
:func:`app.gateway.pid_alive` 与 :func:`app.gateway.process_start_time`
（Windows 走 ``OpenProcess``+``GetProcessTimes``，POSIX 读 ``/proc/<pid>/stat``），
不另写一套平台探测。

三种判定，**只有第一种算孤儿**：

=================  ============================================  ==================
判定               含义                                          本模块的处理
=================  ============================================  ==================
``dead``           登记了，但 pid 已经不在 → **孤儿**            报告；``reap`` 清登记
``reused``         pid 活着，但创建时间对不上 → PID 被复用      报告为"身份不符"，
                                                                  **不算孤儿**；
                                                                  ``reap`` 也清登记
``alive``          同一进程且还活着                              什么都不做
=================  ============================================  ==================

为什么 ``reused`` 不算孤儿：孤儿指的是"该在的进程没了"。PID 复用恰恰说明**有另一个
无关进程活着**，把它算进孤儿会让"孤儿数"变成一个恐怖但无意义的数字，也会诱导后来的人
在这里加一句 kill。它仍然是**无效登记**（那条登记对应的进程确实没了），所以 ``reap``
在非 dry-run 时会清掉——清理登记与"回收孤儿"是两件事，报告里分开列。

## 绝不 kill 进程

本模块**只清登记**，不 ``taskkill``、不 ``SIGTERM``、不发信号。理由：孤儿判定基于
"读进程创建时间"这种尽力而为的探测（权限不足、进程刚退出、容器里 /proc 不可读都会让
探测退化），在不可靠判据上执行不可逆动作（杀进程）是不可接受的。真要收进程树，
那是 ``app/gateway.py`` 的职责（它有 ``--force`` 与显式的用户意图）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.gateway import (
    GatewayPaths,
    ProcessRegistry,
    SpawnLedger,
    pid_alive,
    process_start_time,
)

#: 判定词表。
KIND_DEAD = "dead"
KIND_REUSED = "reused"
KIND_ALIVE = "alive"

#: 登记来源名（报告里用）。
SOURCE_PROCESSES = "processes.json"
SOURCE_SPAWN_LEDGER = "spawn-ledger.json"
SOURCE_LOCK = "dispatcher.lock"

#: 创建时间比对的容差（秒）。与 ``app/gateway.ProcessRegistry._alive`` 同值：
#: 两处判据必须一致，否则同一个条目在 `gateway doctor` 与 `orbit plane reap` 里
#: 会得出两种结论，谁都不敢信。
START_TIME_TOLERANCE = 1.0


@dataclass(frozen=True)
class Registration:
    """一条被判定为"无效"的登记（``dead`` 或 ``reused``）。"""

    source: str
    pid: int
    host_start_time: float | None
    kind: str
    record: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "pid": self.pid,
            "host_start_time": self.host_start_time,
            "kind": self.kind,
            "reason": self.reason,
            "record": self.record,
        }


@dataclass(frozen=True)
class ReapReport:
    """一次回收的结果。``dry_run=True`` 时 ``removed`` 恒为 0（只报告不动账本）。"""

    dry_run: bool
    orphans: list[Registration] = field(default_factory=list)
    stale: list[Registration] = field(default_factory=list)
    removed: int = 0
    lock_removed: bool = False

    @property
    def total(self) -> int:
        return len(self.orphans) + len(self.stale)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "orphans": [item.as_dict() for item in self.orphans],
            "stale": [item.as_dict() for item in self.stale],
            "orphan_count": len(self.orphans),
            "stale_count": len(self.stale),
            "removed": self.removed,
            "lock_removed": self.lock_removed,
        }


class OrphanReaper:
    """扫三个登记簿，找"登记过但进程已死"的条目。

    ``alive`` / ``start_time`` 两个探针**可注入**：测试要造"pid 活着但创建时间对不上"
    这种状态，不该真的去起进程再等它被复用（那既慢又不确定）。
    默认值就是 gateway 的真实实现，生产路径上没有替身。
    """

    def __init__(
        self,
        data_dir: Path,
        *,
        alive: Any = None,
        start_time: Any = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.paths = GatewayPaths(self.data_dir)
        self.processes = ProcessRegistry(self.paths.processes)
        self.spawn_ledger = SpawnLedger(self.paths.spawn_ledger)
        self._alive = alive or pid_alive
        self._start_time = start_time or process_start_time

    # ── 判定 ──
    def classify(self, pid: Any, recorded: Any) -> str:
        """``(pid, 登记时的创建时间)`` → ``alive`` / ``dead`` / ``reused``。"""
        try:
            number = int(pid)
        except (TypeError, ValueError):
            return KIND_DEAD  # 连 pid 都不是数字：这条登记一定是坏的
        if number <= 0:
            return KIND_DEAD
        if not self._alive(number):
            return KIND_DEAD
        actual = self._start_time(number)
        if recorded is None or actual is None:
            # 一边拿不到创建时间：只能退回 pid 判活。这是弱判据，但不因此报孤儿——
            # 把"探测不到"当成"进程已死"会误清活着的登记。
            return KIND_ALIVE
        try:
            if abs(float(recorded) - float(actual)) <= START_TIME_TOLERANCE:
                return KIND_ALIVE
        except (TypeError, ValueError):
            return KIND_ALIVE
        return KIND_REUSED

    def scan(self) -> list[Registration]:
        """扫全部登记，返回**无效**的那些（``alive`` 的丢掉）。"""
        found: list[Registration] = []
        for record in self.processes.load():
            entry = self._entry(SOURCE_PROCESSES, record, record.get("host_start_time"))
            if entry is not None:
                found.append(entry)
        for record in self.spawn_ledger.load():
            # spawn-ledger 的时间字段叫 ``create_time``（见 SpawnLedger.register_child）。
            entry = self._entry(SOURCE_SPAWN_LEDGER, record, record.get("create_time"))
            if entry is not None:
                found.append(entry)
        lock = self._lock_entry()
        if lock is not None:
            found.append(lock)
        return found

    def find_orphans(self) -> list[Registration]:
        """孤儿 = 登记过、进程已死。**PID 复用（身份不符）不算孤儿**（见模块 docstring）。"""
        return [item for item in self.scan() if item.kind == KIND_DEAD]

    def find_stale(self) -> list[Registration]:
        """身份不符（PID 复用）的无效登记：不是孤儿，但登记确实该清。"""
        return [item for item in self.scan() if item.kind == KIND_REUSED]

    def _entry(
        self, source: str, record: Mapping[str, Any], recorded: Any
    ) -> Registration | None:
        kind = self.classify(record.get("pid"), recorded)
        if kind == KIND_ALIVE:
            return None
        try:
            pid = int(record.get("pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        return Registration(
            source=source,
            pid=pid,
            host_start_time=_as_float(recorded),
            kind=kind,
            record=dict(record),
            reason=(
                f"pid={pid} 已不在（登记于 {source}）"
                if kind == KIND_DEAD
                else f"pid={pid} 的创建时间与登记不符（PID 已被复用，登记于 {source}）"
            ),
        )

    def _lock_entry(self) -> Registration | None:
        """``dispatcher.lock`` 指向死进程 / 被复用进程时，它也是一条无效登记。"""
        path = self.paths.lock
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            # 锁文件坏掉：这不是"孤儿锁"，是"读不出来"。如实报出来，不静默当没有。
            return Registration(
                source=SOURCE_LOCK,
                pid=0,
                host_start_time=None,
                kind=KIND_DEAD,
                record={},
                reason=f"{SOURCE_LOCK} 无法解析（文件损坏或写到一半）",
            )
        if not isinstance(raw, dict):
            return Registration(
                source=SOURCE_LOCK,
                pid=0,
                host_start_time=None,
                kind=KIND_DEAD,
                record={},
                reason=f"{SOURCE_LOCK} 内容不是 JSON 对象",
            )
        return self._entry(SOURCE_LOCK, raw, raw.get("host_start_time"))

    # ── 回收 ──
    def reap(self, *, dry_run: bool = True) -> ReapReport:
        """默认只报告（``dry_run=True``）；``dry_run=False`` 才清账本条目。

        **绝不 kill 进程**（见模块 docstring）。清理范围：
        ``processes.json`` / ``spawn-ledger.json`` 里判为 dead 或 reused 的条目，
        以及判为 dead/reused 的 ``dispatcher.lock`` 文件本身。
        """
        orphans = self.find_orphans()
        stale = self.find_stale()
        if dry_run:
            return ReapReport(dry_run=True, orphans=orphans, stale=stale)

        targets = orphans + stale
        removed = 0
        if targets:
            removed += self._drop(
                self.processes,
                [item for item in targets if item.source == SOURCE_PROCESSES],
                "host_start_time",
            )
            removed += self._drop(
                self.spawn_ledger,
                [item for item in targets if item.source == SOURCE_SPAWN_LEDGER],
                "create_time",
            )
        lock_removed = False
        if any(item.source == SOURCE_LOCK for item in targets):
            try:
                self.paths.lock.unlink(missing_ok=True)
                lock_removed = True
            except OSError:
                lock_removed = False
        return ReapReport(
            dry_run=False,
            orphans=orphans,
            stale=stale,
            removed=removed,
            lock_removed=lock_removed,
        )

    @staticmethod
    def _drop(registry: Any, entries: list[Registration], time_field: str) -> int:
        """按 ``(pid, 创建时间)`` 精确删条目——**不用 pid 单键**，否则 PID 复用后
        会把新登记一起删掉（与 ``ProcessRegistry.register`` 的去重键保持同一口径）。
        """
        if not entries:
            return 0
        keys = {(item.pid, _as_float(item.record.get(time_field))) for item in entries}
        kept: list[dict[str, Any]] = []
        dropped = 0
        for record in registry.load():
            try:
                pid = int(record.get("pid") or 0)
            except (TypeError, ValueError):
                pid = 0
            key = (pid, _as_float(record.get(time_field)))
            if key in keys:
                dropped += 1
                continue
            kept.append(record)
        if dropped:
            registry.save(kept)
        return dropped


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "KIND_ALIVE",
    "KIND_DEAD",
    "KIND_REUSED",
    "SOURCE_LOCK",
    "SOURCE_PROCESSES",
    "SOURCE_SPAWN_LEDGER",
    "START_TIME_TOLERANCE",
    "OrphanReaper",
    "ReapReport",
    "Registration",
]
