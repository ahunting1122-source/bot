"""Gateway 服务层：单 dispatcher 互斥、四个状态文件、会话路由与投递账本。

这一层回答的问题：**多个平台同时在线时，谁负责接收、消息该落到哪个会话、发出去失败
了怎么记账**。契约来源 ``handoff/hermes-parity/research/05-plugin-gateway-layer.md``
§3（Gateway 机制）+ §6 的第 4️⃣ 项。

四条设计取舍（动这里之前先读）：

1. **单 dispatcher 互斥靠锁文件，不靠"看起来在跑"**。Hermes 的原话是
   "a second dispatcher escapes the service and can corrupt shared gateway state"。
   所以 ``<data_dir>/gateway/dispatcher.lock`` 里写 ``pid + host_start_time``，
   启动时校验"这个 pid 还活着**且**它的创建时间没变"——只看 pid 会在 PID 复用后
   把别人的进程当成自己的网关，于是第二个 dispatcher 被放行。
2. **状态一律落盘、坏文件当空**。四个状态文件都是"给下一个进程看的"，所以写盘一律
   临时文件 + ``os.replace``；读坏文件当空而不是抛异常（一个手抖写坏的 JSON 不该让
   整个网关从此起不来）。
3. **本轮不起线程、不开监听**。:class:`GatewayService.start` 只写状态文件、拿锁、
   扫孤儿。真正的长连接要等各平台适配器实现（见 :mod:`app.platforms` 的 19 个
   ``implemented=False``）。先把**可测的投递路径与状态契约**做对，比先起一个
   半死不活的守护进程值钱。
4. **投递不抛异常**（:meth:`GatewayService.deliver`）。调用方是 cron 与 CLI，
   抛出去只会让"投递失败"变成一个没人捕获的栈；这里返回 ``ok=False`` 并把每一次
   尝试写进 ``deliveries.jsonl``——至少一次语义（at-least-once）要求"发过就要留痕"。
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from app.platforms import (
    IMPLEMENTED_PLATFORMS,
    PLATFORMS,
    PlatformAdapter,
    PlatformNotImplemented,
    default_adapters,
    resolve_adapter,
)
from app.session_plane import (
    KIND_GATEWAY_DELIVERED,
    KIND_GATEWAY_DELIVERY_FAILED,
    SOURCE_KEY_GATEWAY,
    SessionPlane,
    plane_session_key,
)

#: gateway 的全部落盘都在这个子目录下（与 ``cron/`` 平级，便于整目录备份/搬走）。
GATEWAY_DIRNAME = "gateway"

#: 单 dispatcher 锁。内容是 JSON：``pid`` + ``host_start_time``（+ 便于排障的字段）。
LOCK_FILENAME = "dispatcher.lock"
#: 每次启动追加一行 epoch 秒（**纯文本、无表头**，照 Hermes 的真实样本）。
STARTS_FILENAME = "gateway-starts.log"
#: 进程登记簿（JSON 数组）。身份三元组 ``(pid, host_start_time)`` 防 PID 复用。
PROCESSES_FILENAME = "processes.json"
#: 机器级 spawn 账本（JSON 数组）：``pid / create_time / purpose``，供启动期回收孤儿。
SPAWN_LEDGER_FILENAME = "spawn-ledger.json"
#: 频道目录：``{updated_at, platforms: {platform: [channel...]}}``。
CHANNEL_DIRECTORY_FILENAME = "channel_directory.json"
#: 投递账本（JSONL，每次尝试一行）。
DELIVERIES_FILENAME = "deliveries.jsonl"
#: webhook 订阅表。
WEBHOOKS_FILENAME = "webhooks.json"
#: 对等网关登记表。
PEERS_FILENAME = "peers.json"
#: 配对状态目录（照 Hermes 的 ``<home>/pairing/``）。
PAIRING_DIRNAME = "pairing"
#: 紧急停止哨兵（MATRIX 11.6 的 gateway 一侧）。与 ``cron/paused`` 同一个机制：
#: 任何进程（运维脚本、另一个终端）写一个文件就生效，读侧只判 ``exists()``。
#: 为什么不用配置字段：配置要先读出来才生效，而"配置读不出来"恰恰是最需要停的时候。
PAUSED_FILENAME = "paused"

#: 崩溃循环判据：窗口内启动次数 ≥ 阈值就报"反复重启"。
#: 为什么是 10 分钟 / 3 次：Hermes 本机样本里 30 分钟约 10 次重启被判成崩溃循环，
#: 换算到 Orbit 更保守的默认值——3 次/10 分钟已经不可能是一次人工重启。
CRASH_LOOP_WINDOW_SECONDS = 600.0
CRASH_LOOP_MIN_STARTS = 3

#: 投递默认尝试次数（至少一次语义：失败重试，仍失败就如实记 failed）。
DELIVER_ATTEMPTS = 2


# ── 时间与原子写 ────────────────────────────────────────────────────────────


def _iso_now(moment: float | None = None) -> str:
    """本地时间 ISO 串（**不带时区后缀**，与 Hermes ``channel_directory.json`` 一致）。"""
    stamp = datetime.fromtimestamp(moment if moment is not None else time.time())
    return stamp.isoformat()


def now_iso(moment: float | None = None) -> str:
    """公开入口：与落盘格式完全一致的时间串（CLI 写别的状态文件时复用同一格式）。"""
    return _iso_now(moment)


def write_json(path: Path, payload: Any) -> None:
    """公开入口：原子写一份 JSON（CLI 也走同一套落盘写法，避免出现两种风格）。"""
    _atomic_write(Path(path), json.dumps(payload, ensure_ascii=False, indent=2))


def _atomic_write(path: Path, text: str) -> None:
    """临时文件 + ``os.replace``：中途崩溃只留旧文件，不留半个 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle_fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _read_json(path: Path, default: Any) -> Any:
    """读 JSON。缺失/损坏都返回 ``default``（损坏的那份改名留证）。"""
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        try:
            os.replace(path, path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d%H%M%S')}"))
        except OSError:
            pass
        return default


def _append_line(path: Path, line: str) -> None:
    """追加一行并 fsync。**追加而非重写**：流水账要留下已发生的部分。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


# ── 进程身份（防 PID 复用） ─────────────────────────────────────────────────
#
# 为什么自己写而不是用 psutil：本机无网络、装不了新依赖。而这件事又必须做对：
# 只凭 pid 判活，PID 一被系统复用就会把"别人的进程"当成自己的网关，于是第二个
# dispatcher 被放行——那正是 Hermes 明确要防的事故。
#
# 平台差异：
# - Windows：``os.kill(pid, 0)`` **不是**探测（CPython 在 Windows 上对非控制信号的
#   sig 会直接 TerminateProcess）。必须走 ``OpenProcess`` + ``GetProcessTimes``。
# - POSIX：``/proc/<pid>/stat`` 第 22 个字段是启动时刻（clock ticks since boot）。

#: 本进程启动时刻的兜底值（拿不到真实创建时间时用它，仍是"本进程唯一"的稳定值）。
_FALLBACK_START_TIME = time.time()

_EPOCH_AS_FILETIME = 11644473600  # 1601-01-01 → 1970-01-01 的秒数


def _windows_start_time(pid: int) -> float | None:
    """Windows：``GetProcessTimes`` 的创建时间 → epoch 秒。失败返回 ``None``。"""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # pragma: no cover - 非 Windows 解释器
        return None
    process_query_limited_information = 0x1000
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover
        return None
    handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
    if not handle:
        return None
    try:
        created = wintypes.FILETIME()
        exited = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        ok = kernel32.GetProcessTimes(
            handle,
            ctypes.byref(created),
            ctypes.byref(exited),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        if not ok:
            return None
        ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
        return ticks / 10_000_000 - _EPOCH_AS_FILETIME
    finally:
        kernel32.CloseHandle(handle)


def _posix_start_time(pid: int) -> float | None:
    """POSIX：``/proc/<pid>/stat`` 的第 22 字段（启动时刻，单位 clock tick）。"""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # comm 字段可能含空格和括号，所以从最后一个 ')' 之后开始切。
    tail = raw.rsplit(")", 1)[-1].split()
    if len(tail) < 20:
        return None
    try:
        start_ticks = int(tail[19])
    except ValueError:
        return None
    try:
        clock_ticks = os.sysconf("SC_CLK_TCK")  # type: ignore[attr-defined]
    except (AttributeError, ValueError, OSError):
        clock_ticks = 100
    try:
        boot = float(Path("/proc/stat").read_text(encoding="utf-8").split("btime")[1].split()[0])
    except (OSError, IndexError, ValueError):
        return None
    return boot + start_ticks / clock_ticks


def process_start_time(pid: int) -> float | None:
    """进程创建时刻（epoch 秒）。拿不到返回 ``None``（调用方须容忍）。"""
    if pid <= 0:
        return None
    if pid == os.getpid():
        # 自己：优先取真实值，拿不到就用进程内兜底常量（仍能区分不同进程）。
        return (_windows_start_time(pid) if sys.platform == "win32" else _posix_start_time(pid)) or (
            _FALLBACK_START_TIME
        )
    if sys.platform == "win32":
        return _windows_start_time(pid)
    return _posix_start_time(pid)


def pid_alive(pid: int) -> bool:
    """进程是否活着。**Windows 上绝不使用 ``os.kill(pid, 0)``**（会杀掉对方）。"""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        return _windows_start_time(pid) is not None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但没权限发信号
    except OSError:
        return False
    return True


def identity(pid: int | None = None) -> tuple[int, float | None]:
    """本进程（或指定 pid）的身份：``(pid, host_start_time)``。"""
    target = os.getpid() if pid is None else int(pid)
    return target, process_start_time(target)


# ── 路径 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GatewayPaths:
    """``<data_dir>/gateway/`` 下的全部落点。集中在一处，免得各处拼字符串。"""

    data_dir: Path

    @property
    def root(self) -> Path:
        return self.data_dir / GATEWAY_DIRNAME

    @property
    def lock(self) -> Path:
        return self.root / LOCK_FILENAME

    @property
    def starts(self) -> Path:
        return self.root / STARTS_FILENAME

    @property
    def processes(self) -> Path:
        return self.root / PROCESSES_FILENAME

    @property
    def spawn_ledger(self) -> Path:
        return self.root / SPAWN_LEDGER_FILENAME

    @property
    def channel_directory(self) -> Path:
        return self.root / CHANNEL_DIRECTORY_FILENAME

    @property
    def deliveries(self) -> Path:
        return self.root / DELIVERIES_FILENAME

    @property
    def webhooks(self) -> Path:
        return self.root / WEBHOOKS_FILENAME

    @property
    def peers(self) -> Path:
        return self.root / PEERS_FILENAME

    @property
    def pairing(self) -> Path:
        return self.data_dir / PAIRING_DIRNAME

    @property
    def paused(self) -> Path:
        """紧急停止哨兵（``<data_dir>/gateway/paused``）。"""
        return self.root / PAUSED_FILENAME

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.pairing.mkdir(parents=True, exist_ok=True)


# ── 单 dispatcher 互斥 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class LockOutcome:
    """拿锁的结果。``ok=False`` 时 ``reason`` 就是给人看的那句话。"""

    ok: bool
    reclaimed: bool = False
    holder: dict[str, Any] = field(default_factory=dict)
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "reclaimed": self.reclaimed,
            "holder": self.holder,
            "reason": self.reason,
        }


class DispatcherLock:
    """``<data_dir>/gateway/dispatcher.lock``：同一数据目录只允许一个 dispatcher。

    三种情形，处理方式不同：

    - 锁不存在 → 写入，成功。
    - 锁在、holder 活着**且**启动时间对得上 → 拒绝（第二个 dispatcher 会写坏共享状态）。
    - 锁在、holder 已死**或** PID 被复用（启动时间对不上）→ **回收孤儿锁**并接管。
    """

    def __init__(self, path: Path, *, pid: int | None = None, now: float | None = None) -> None:
        self.path = Path(path)
        self.pid, self.host_start_time = identity(pid)
        self.acquired_at = now if now is not None else time.time()

    # ── 读 ──
    def read_holder(self) -> dict[str, Any]:
        raw = _read_json(self.path, {})
        return raw if isinstance(raw, dict) else {}

    def holder_alive(self, holder: Mapping[str, Any] | None = None) -> bool:
        """holder 是否"同一个进程且还活着"。启动时间对不上 = PID 被复用，视为已死。"""
        record = dict(holder if holder is not None else self.read_holder())
        try:
            pid = int(record.get("pid") or 0)
        except (TypeError, ValueError):
            return False
        if pid <= 0 or not pid_alive(pid):
            return False
        recorded = record.get("host_start_time")
        actual = process_start_time(pid)
        if recorded is None or actual is None:
            # 一边拿不到创建时间：只能退回 pid 判活（并如实说明这是弱判据）。
            return True
        try:
            return abs(float(recorded) - float(actual)) <= 1.0
        except (TypeError, ValueError):
            return True

    # ── 写 ──
    def acquire(self, *, force: bool = False) -> LockOutcome:
        holder = self.read_holder()
        if holder and not force:
            if self.holder_alive(holder):
                return LockOutcome(
                    ok=False,
                    holder=holder,
                    reason=(
                        f"已有 dispatcher 在运行（pid={holder.get('pid')}，"
                        f"启动于 {_iso_now(float(holder.get('acquired_at') or 0))}）。"
                        "第二个 dispatcher 会写坏共享的 gateway 状态；"
                        "确认对方是僵死进程时用 --force 强制接管。"
                    ),
                )
            # 死进程 / PID 复用 → 孤儿锁，回收后接管。
            self._write()
            return LockOutcome(
                ok=True,
                reclaimed=True,
                holder=holder,
                reason=f"回收孤儿锁（pid={holder.get('pid')} 已不在或已被复用）",
            )
        if holder and force:
            self._write()
            return LockOutcome(
                ok=True,
                reclaimed=True,
                holder=holder,
                reason=f"--force 强制接管（原 holder pid={holder.get('pid')}）",
            )
        self._write()
        return LockOutcome(ok=True)

    def _write(self) -> None:
        payload = {
            "pid": self.pid,
            "host_start_time": self.host_start_time,
            "acquired_at": self.acquired_at,
            "data_dir": str(self.path.parent.parent),
        }
        _atomic_write(self.path, json.dumps(payload, ensure_ascii=False, indent=2))

    def release(self) -> bool:
        """只释放**自己**持有的锁。别人的锁不动（否则就成了互相踩踏）。"""
        holder = self.read_holder()
        try:
            holder_pid = int(holder.get("pid") or 0)
        except (TypeError, ValueError):
            holder_pid = 0
        if holder_pid and holder_pid != self.pid:
            return False
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            return False
        return True


# ── 崩溃循环检测 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CrashLoopReport:
    """``gateway-starts.log`` 的诊断结果。"""

    crash_loop: bool
    starts_in_window: int
    window_seconds: float
    total_starts: int
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "crash_loop": self.crash_loop,
            "starts_in_window": self.starts_in_window,
            "window_seconds": self.window_seconds,
            "total_starts": self.total_starts,
            "detail": self.detail,
        }


def read_start_stamps(path: Path) -> list[float]:
    """读启动戳序列。坏行跳过（一行坏了不该让整份历史读不出来）。"""
    stamps: list[float] = []
    for line in _read_lines(path):
        try:
            stamps.append(float(line))
        except ValueError:
            continue
    return stamps


def detect_crash_loop(
    path: Path,
    *,
    now: float | None = None,
    window: float = CRASH_LOOP_WINDOW_SECONDS,
    minimum: int = CRASH_LOOP_MIN_STARTS,
) -> CrashLoopReport:
    """判"是不是在反复崩溃重启"。

    判据：**窗口内启动次数 ≥ 阈值**。为什么用"启动次数"而不是"退出码"：网关被
    systemd / 计划任务拉起时，崩溃与"人工重启"在外部看来完全一样，唯一可靠的信号
    就是"启动得太频繁"。未来时刻的戳（时钟回拨/手改）不计入，否则一条坏数据会永久
    报崩溃循环。
    """
    moment = now if now is not None else time.time()
    stamps = read_start_stamps(path)
    # 只容忍 1 秒的时钟抖动：明显来自未来的戳（时钟回拨 / 手改）一律不计入，
    # 否则一条坏数据会永久报崩溃循环，提示就彻底失去意义。
    horizon = moment + 1.0
    recent = [stamp for stamp in stamps if moment - window <= stamp <= horizon]
    loop = len(recent) >= minimum
    detail = (
        f"最近 {window / 60:.0f} 分钟内启动 {len(recent)} 次（阈值 {minimum} 次），"
        "进程可能在崩溃重启循环里——查 gateway 日志与平台凭证，别只重启。"
        if loop
        else f"最近 {window / 60:.0f} 分钟内启动 {len(recent)} 次，未构成崩溃循环。"
    )
    return CrashLoopReport(
        crash_loop=loop,
        starts_in_window=len(recent),
        window_seconds=window,
        total_starts=len(stamps),
        detail=detail,
    )


# ── 四个状态文件 ────────────────────────────────────────────────────────────


class ProcessRegistry:
    """``processes.json``：被托管的进程登记簿。

    落盘是 **JSON 数组**（照 Hermes 真实样本），内存里以 ``(pid, host_start_time)``
    为键去重——只用 pid 当键，PID 复用后会把两条记录合成一条。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> list[dict[str, Any]]:
        raw = _read_json(self.path, [])
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    def save(self, records: Iterable[dict[str, Any]]) -> None:
        _atomic_write(self.path, json.dumps(list(records), ensure_ascii=False, indent=2))

    def register(self, record: dict[str, Any]) -> list[dict[str, Any]]:
        key = (record.get("pid"), record.get("host_start_time"))
        kept = [
            item for item in self.load() if (item.get("pid"), item.get("host_start_time")) != key
        ]
        kept.append(record)
        self.save(kept)
        return kept

    def unregister(self, pid: int, host_start_time: float | None = None) -> list[dict[str, Any]]:
        kept = [
            item
            for item in self.load()
            if not (
                item.get("pid") == pid
                and (host_start_time is None or item.get("host_start_time") == host_start_time)
            )
        ]
        self.save(kept)
        return kept

    def live(self) -> list[dict[str, Any]]:
        """过滤掉已死的条目（**不写盘**：查询不该有副作用）。"""
        return [item for item in self.load() if self._alive(item)]

    @staticmethod
    def _alive(record: Mapping[str, Any]) -> bool:
        try:
            pid = int(record.get("pid") or 0)
        except (TypeError, ValueError):
            return False
        if not pid_alive(pid):
            return False
        recorded = record.get("host_start_time")
        actual = process_start_time(pid)
        if recorded is None or actual is None:
            return True
        try:
            return abs(float(recorded) - float(actual)) <= 1.0
        except (TypeError, ValueError):
            return True


class SpawnLedger:
    """``spawn-ledger.json``：机器级 spawn 账本，供**启动期回收孤儿**。

    与 ``processes.json`` 的分工（照 Hermes）：前者记"网关/工具起的任务型后台进程"
    （有会话归属、有输出守望），后者只记"Orbit 自己起的辅助子进程"，唯一目的是
    父进程异常退出（被 kill / 断电）后，下次启动能凭 ``create_time`` 精确回收，
    而不会因 PID 复用误杀无关进程。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> list[dict[str, Any]]:
        raw = _read_json(self.path, [])
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    def save(self, records: Iterable[dict[str, Any]]) -> None:
        _atomic_write(self.path, json.dumps(list(records), ensure_ascii=False, indent=2))

    def register_child(self, pid: int, purpose: str, *, now: float | None = None) -> dict[str, Any]:
        """登记一个子进程。身份三元组是 ``(pid, create_time, purpose)``。"""
        record = {
            "pid": int(pid),
            "create_time": process_start_time(int(pid)),
            "purpose": str(purpose),
            "registered_at": now if now is not None else time.time(),
        }
        records = [
            item for item in self.load() if not (item.get("pid") == record["pid"])
        ]
        records.append(record)
        self.save(records)
        return record

    def reap_orphans(self) -> list[dict[str, Any]]:
        """回收已死的条目，返回被回收的那些（写盘后的账本只留活的）。"""
        kept: list[dict[str, Any]] = []
        reaped: list[dict[str, Any]] = []
        for item in self.load():
            try:
                pid = int(item.get("pid") or 0)
            except (TypeError, ValueError):
                reaped.append(item)
                continue
            recorded = item.get("create_time")
            actual = process_start_time(pid)
            alive = pid_alive(pid)
            if alive and recorded is not None and actual is not None:
                try:
                    alive = abs(float(recorded) - float(actual)) <= 1.0
                except (TypeError, ValueError):
                    alive = True
            (kept if alive else reaped).append(item)
        self.save(kept)
        return reaped


class ChannelDirectory:
    """``channel_directory.json``：平台频道目录（路由 + ``send --list`` 的数据源）。

    结构照 Hermes：``{updated_at, platforms: {platform: [channel, ...]}}``。
    额外加一个 ``sessions`` 键存"频道 → 会话键"的显式绑定（见
    :meth:`GatewayService.route` 里关于 W8 之前临时方案的说明）。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        raw = _read_json(self.path, {})
        if not isinstance(raw, dict):
            raw = {}
        platforms = raw.get("platforms")
        if not isinstance(platforms, dict):
            platforms = {}
        sessions = raw.get("sessions")
        if not isinstance(sessions, dict):
            sessions = {}
        return {
            "updated_at": str(raw.get("updated_at") or ""),
            "platforms": {
                str(name): [str(item) for item in (channels or [])]
                for name, channels in platforms.items()
                if isinstance(channels, list)
            },
            "sessions": {str(key): str(value) for key, value in sessions.items()},
        }

    def save(self, data: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
        payload = {
            "updated_at": _iso_now(now),
            "platforms": dict(data.get("platforms") or {}),
            "sessions": dict(data.get("sessions") or {}),
        }
        _atomic_write(self.path, json.dumps(payload, ensure_ascii=False, indent=2))
        return payload

    def channels(self, platform: str) -> list[str]:
        return list(self.load()["platforms"].get(str(platform), []))

    def remember(self, platform: str, channel: str, *, session_key: str = "") -> dict[str, Any]:
        """把"见过这个频道"记进目录（**会话反推**这条数据源）。"""
        data = self.load()
        name = str(platform)
        if channel:
            bucket = data["platforms"].setdefault(name, [])
            if channel not in bucket:
                bucket.append(channel)
        if session_key:
            data["sessions"][f"{name}:{channel or 'home'}"] = session_key
        return self.save(data)

    def rebuild(
        self, adapters: Mapping[str, PlatformAdapter], *, now: float | None = None
    ) -> dict[str, Any]:
        """重建目录：**主动枚举**（适配器可选实现 ``list_channels()``）+ 保留**会话反推**。

        为什么要合并而不是覆盖：大多数平台（telegram / whatsapp / signal）根本枚举不了
        频道，只能从历史会话反推。覆盖式重建会把这类平台整片清空——Hermes 的
        ``ADDING_A_PLATFORM.md`` §11 明确把平台分成"能枚举"和"只能反推"两类。
        """
        data = self.load()
        for name, adapter in adapters.items():
            enumerator = getattr(adapter, "list_channels", None)
            if not callable(enumerator):
                continue
            try:
                found = enumerator()
            except Exception:  # 枚举失败不该让整次重建失败
                continue
            if not isinstance(found, (list, tuple)):
                continue
            bucket = data["platforms"].setdefault(str(name), [])
            for channel in found:
                text = str(channel)
                if text and text not in bucket:
                    bucket.append(text)
        return self.save(data, now=now)


# ── 投递 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeliveryResult:
    """一次投递的最终结果（含重试）。"""

    ok: bool
    platform: str
    channel: str = ""
    session_key: str = ""
    attempts: int = 0
    message_id: str = ""
    error: str = ""
    #: 平台本轮没实现（区别于"网络抖动"：前者不该重试）。
    not_implemented: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "platform": self.platform,
            "channel": self.channel,
            "session_key": self.session_key,
            "attempts": self.attempts,
            "message_id": self.message_id,
            "error": self.error,
            "not_implemented": self.not_implemented,
        }


@dataclass(frozen=True)
class StartOutcome:
    """``GatewayService.start`` 的结果。"""

    ok: bool
    pid: int
    host_start_time: float | None
    reclaimed: bool = False
    reason: str = ""
    reaped_orphans: tuple[dict[str, Any], ...] = ()
    channels: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "pid": self.pid,
            "host_start_time": self.host_start_time,
            "reclaimed": self.reclaimed,
            "reason": self.reason,
            "reaped_orphans": list(self.reaped_orphans),
            "channels": self.channels,
        }


@dataclass(frozen=True)
class HealthReport:
    """``GatewayService.health`` 的结果：**问题列表为空才算健康**。"""

    healthy: bool
    issues: tuple[str, ...] = ()
    crash_loop: CrashLoopReport | None = None
    dispatcher: dict[str, Any] = field(default_factory=dict)
    state_files: dict[str, bool] = field(default_factory=dict)
    platforms: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "issues": list(self.issues),
            "crash_loop": self.crash_loop.as_dict() if self.crash_loop else {},
            "dispatcher": dict(self.dispatcher),
            "state_files": dict(self.state_files),
            "platforms": dict(self.platforms),
        }


# ── GatewayService ──────────────────────────────────────────────────────────


class GatewayService:
    """网关服务：拿锁、写状态、路由、投递。**不起线程、不开监听**。

    ``adapters`` 可注入：测试用 ``httpx.MockTransport`` 造一个 ``ntfy`` 适配器进来，
    就能在完全无网络的环境里跑通"路由 → 发送 → 记账"整条链路。
    """

    def __init__(
        self,
        data_dir: str | Path,
        *,
        adapters: Mapping[str, PlatformAdapter] | None = None,
        pid: int | None = None,
        now: float | None = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.paths = GatewayPaths(self.data_dir)
        self.adapters: dict[str, PlatformAdapter] = dict(
            adapters if adapters is not None else default_adapters(self.data_dir)
        )
        self.pid, self.host_start_time = identity(pid)
        self._now = now if now is not None else time.time()
        self.lock = DispatcherLock(self.paths.lock, pid=self.pid, now=self._now)
        self.processes = ProcessRegistry(self.paths.processes)
        self.spawn_ledger = SpawnLedger(self.paths.spawn_ledger)
        self.directory = ChannelDirectory(self.paths.channel_directory)
        self.webhooks = WebhookStore(self.paths.webhooks)
        self.peers = PeerRegistry(self.paths.peers)
        self.pairing = PairingStore(self.paths.pairing)

    # ── 生命周期 ──
    def start(self, *, force: bool = False) -> StartOutcome:
        """拿锁 + 写四个状态文件。**幂等性刻意不给**：重复 start 会被互斥挡住。"""
        self.paths.ensure()
        outcome = self.lock.acquire(force=force)
        if not outcome.ok:
            return StartOutcome(
                ok=False,
                pid=self.pid,
                host_start_time=self.host_start_time,
                reason=outcome.reason,
            )
        # ① 启动戳：先落盘再干活。进程若在下面任何一步崩掉，这一行就是崩溃循环的证据。
        _append_line(self.paths.starts, f"{self._now:.6f}")
        # ② 进程登记簿
        self.processes.register(
            {
                "pid": self.pid,
                "host_start_time": self.host_start_time,
                "pid_scope": "host",
                "purpose": "gateway",
                "cwd": str(self.data_dir),
                "started_at": self._now,
                "session_key": "",
            }
        )
        # ③ spawn 账本：启动期扫一遍孤儿（父进程被 kill 后留下的辅助子进程）
        reaped = self.spawn_ledger.reap_orphans()
        # ④ 频道目录：主动枚举 + 会话反推
        rebuilt = self.directory.rebuild(self.adapters, now=self._now)
        for adapter in self.adapters.values():
            adapter.on_connect()
        return StartOutcome(
            ok=True,
            pid=self.pid,
            host_start_time=self.host_start_time,
            reclaimed=outcome.reclaimed,
            reason=outcome.reason,
            reaped_orphans=tuple(reaped),
            channels=sum(len(items) for items in rebuilt["platforms"].values()),
        )

    def stop(self) -> dict[str, Any]:
        """停：断开适配器、注销进程、释放锁。重复 stop 不报错。"""
        for adapter in self.adapters.values():
            try:
                adapter.stop()
            except PlatformNotImplemented:
                continue
            except Exception:  # 停止失败不该阻止释放锁
                continue
            adapter.on_disconnect()
        self.processes.unregister(self.pid, self.host_start_time)
        released = self.lock.release()
        return {"ok": True, "released": released, "pid": self.pid}

    # ── 紧急停止（MATRIX 11.6 的 gateway 一侧） ──
    def paused(self) -> bool:
        """哨兵文件是否存在。**只判存在**，不解析内容——解析失败就等于停不下来。"""
        return self.paths.paused.exists()

    def pause(self, *, reason: str = "", now: float | None = None) -> dict[str, Any]:
        """写哨兵：此后 ``deliver`` 一律不发（并如实记一条 ``gateway.delivery_failed``）。

        为什么把出站也一起挡住：``orbit pause`` 的语义是"新网关轮次别再动"
        （见命令表里那条 help）。只挡入站不挡出站，等于停了半台机器。
        """
        stamp = time.time() if now is None else float(now)
        record = {
            "paused_at": _iso_now(stamp),
            "reason": str(reason or ""),
            "pid": self.pid,
        }
        write_json(self.paths.paused, record)
        return {"ok": True, "paused": True, **record, "path": str(self.paths.paused)}

    def resume(self) -> dict[str, Any]:
        """删哨兵。返回 ``released`` 说明之前是否真的停着（重复 resume 不报错）。"""
        existed = self.paths.paused.exists()
        try:
            self.paths.paused.unlink(missing_ok=True)
        except OSError as exc:
            return {"ok": False, "released": False, "error": f"删不掉哨兵文件：{exc}"}
        return {"ok": True, "released": existed, "paused": False}

    def pause_status(self) -> dict[str, Any]:
        """哨兵状态（给 ``orbit pause --status`` 用）。"""
        record = _read_json(self.paths.paused, {})
        return {
            "paused": self.paths.paused.exists(),
            "path": str(self.paths.paused),
            "detail": record if isinstance(record, dict) else {},
        }

    # ── 状态 ──
    def status(self) -> dict[str, Any]:
        holder = self.lock.read_holder()
        running = self.lock.holder_alive(holder) if holder else False
        directory = self.directory.load()
        return {
            "ok": True,
            "data_dir": str(self.data_dir),
            "gateway_dir": str(self.paths.root),
            "dispatcher": {
                "running": running,
                "holder": holder,
                "live": self.lock.holder_alive(holder) if holder else False,
            },
            "processes": self.processes.live(),
            "state_files": {
                "dispatcher.lock": self.paths.lock.exists(),
                "gateway-starts.log": self.paths.starts.exists(),
                "processes.json": self.paths.processes.exists(),
                "spawn-ledger.json": self.paths.spawn_ledger.exists(),
                "channel_directory.json": self.paths.channel_directory.exists(),
                "deliveries.jsonl": self.paths.deliveries.exists(),
            },
            "starts": detect_crash_loop(self.paths.starts).as_dict(),
            "channels": {
                name: len(channels) for name, channels in directory["platforms"].items()
            },
            "channels_total": sum(len(items) for items in directory["platforms"].values()),
            "platforms": {
                "total": len(PLATFORMS),
                "implemented": [platform.value for platform in IMPLEMENTED_PLATFORMS],
                "pending": len(PLATFORMS) - len(IMPLEMENTED_PLATFORMS),
            },
            "adapters": sorted(self.adapters),
            "webhooks": len(self.webhooks.load()),
            "peers": len(self.peers.load()),
        }

    def health(self) -> HealthReport:
        """自检。**只有问题列表为空才算健康**——"看着像在跑"不是健康。"""
        issues: list[str] = []
        holder = self.lock.read_holder()
        alive = self.lock.holder_alive(holder) if holder else False
        if holder and not alive:
            issues.append(
                f"孤儿锁：dispatcher.lock 指向 pid={holder.get('pid')}，但该进程已不在"
                "（或 PID 已被复用）。下次 start 会自动回收。"
            )
        crash = detect_crash_loop(self.paths.starts)
        if crash.crash_loop:
            issues.append(crash.detail)
        for name, adapter in self.adapters.items():
            try:
                report = adapter.health()
            except PlatformNotImplemented:
                continue
            except Exception as exc:  # 适配器自检炸了也算不健康
                issues.append(f"适配器 {name} 自检异常：{type(exc).__name__}: {exc}")
                continue
            if not report.ok:
                issues.append(f"适配器 {name} 不健康：{report.detail}")
            elif report.extra.get("configured") is False:
                # 未配置 = 未启用，不是故障（否则干净环境下 doctor 永远红灯）。
                continue
        state_files = {
            "gateway-starts.log": self.paths.starts.exists(),
            "processes.json": self.paths.processes.exists(),
            "spawn-ledger.json": self.paths.spawn_ledger.exists(),
            "channel_directory.json": self.paths.channel_directory.exists(),
        }
        if not all(state_files.values()):
            missing = [name for name, present in state_files.items() if not present]
            issues.append(f"状态文件缺失（从未成功启动过？）：{'、'.join(missing)}")
        return HealthReport(
            healthy=not issues,
            issues=tuple(issues),
            crash_loop=crash,
            dispatcher={"running": alive, "holder": holder},
            state_files=state_files,
            platforms={
                "total": len(PLATFORMS),
                "implemented": len(IMPLEMENTED_PLATFORMS),
                "pending": len(PLATFORMS) - len(IMPLEMENTED_PLATFORMS),
            },
        )

    # ── 路由 ──
    def route(self, platform: str, channel: str = "") -> str:
        """把 (平台, 频道) 映射成 Orbit 的会话键。

        **这是 W8 之前的临时方案**：Orbit 目前没有统一 session 平面（会话键由各入口
        自己拼），所以本轮用 ``channel_directory.json`` 里的 ``sessions`` 记映射，
        派生规则是确定性的 ``<platform>:<channel|home>``。W8 之后应当改成"写进会话表、
        由会话服务分配键"——否则同一个频道在两个进程里会拿到两个会话。
        """
        name = str(platform or "").strip().lower()
        chan = str(channel or "").strip()
        key = f"{name}:{chan or 'home'}"
        data = self.directory.load()
        existing = data["sessions"].get(key)
        if existing:
            return existing
        self.directory.remember(name, chan, session_key=key)
        return key

    # ── 投递 ──
    def adapter_for(self, platform: str) -> PlatformAdapter:
        """取适配器。已知但未实现的平台返回 :class:`UnimplementedAdapter`（发送时才抛）；
        **未知平台名**当场抛 :class:`PlatformNotImplemented`（写错了不该等到发送才发现）。"""
        return resolve_adapter(platform, self.adapters)

    def deliver(
        self,
        message: str,
        *,
        platform: str,
        channel: str = "",
        attempts: int = DELIVER_ATTEMPTS,
        retry_delay: float = 0.0,
        message_key: str = "",
    ) -> DeliveryResult:
        """走适配器发送，失败重试，每次尝试都记 ``deliveries.jsonl``。

        三条硬约定：

        - **不往上抛**。cron 与 CLI 都不该因为"投递失败"拿到一个未捕获的栈。
        - **至少一次**：每次尝试单独一行账本，最后一行才是结论（``ok`` / ``failed``）。
        - ``PlatformNotImplemented`` 单独标出来：那不是网络问题，重试没有意义，
          调用方应该把它变成"平台未实现"的报错，而不是"稍后再试"。

        本轮新增两件事（MATRIX 10.1 的写侧统一 + 11.6 的紧急停止）：

        - **结论落一条平面事件**（``gateway.delivered`` / ``gateway.delivery_failed``），
          这样 ``orbit plane timeline gateway:telegram`` 能看到这个出口发生过什么；
        - 哨兵存在时**直接不发**并返回失败——紧急停止必须能真的挡住出站。

        ``message_key`` 是幂等键：调用方（cron）给一个跨重试稳定的键，
        同一次投递重复写不会在平面上留下两条；不给则每次生成唯一的键
        （"同一段文本发两次"是两件真事，不能被内容去重掉）。
        """
        result = self._deliver_inner(
            message,
            platform=platform,
            channel=channel,
            attempts=attempts,
            retry_delay=retry_delay,
        )
        self._record_plane(result, message_key=message_key)
        return result

    def _deliver_inner(
        self,
        message: str,
        *,
        platform: str,
        channel: str = "",
        attempts: int = DELIVER_ATTEMPTS,
        retry_delay: float = 0.0,
    ) -> DeliveryResult:
        """真正的发送与重试循环（不含平面事件，见 :meth:`deliver`）。"""
        name = str(platform or "").strip().lower()
        if self.paused():
            # 停着就一行都不发：**返回失败而不是空成功**，否则调用方会以为送达了。
            reason = "网关已紧急停止（哨兵文件存在，`orbit resume` 解除），本次投递未发送"
            self._log_delivery(
                platform=name,
                channel=channel,
                attempt=1,
                status="failed",
                error=reason,
                session_key=f"{name}:{str(channel or '').strip() or 'home'}",
            )
            return DeliveryResult(
                ok=False,
                platform=name,
                channel=channel,
                session_key=f"{name}:{str(channel or '').strip() or 'home'}",
                attempts=1,
                error=reason,
            )
        try:
            adapter = self.adapter_for(name)
        except PlatformNotImplemented as exc:
            # 连"是不是 22 个平台之一"都不成立：不是网络问题，也不该重试。
            session_key = f"{name}:home"
            self._log_delivery(
                platform=name,
                channel=channel,
                attempt=1,
                status="failed",
                error=str(exc),
                session_key=session_key,
            )
            return DeliveryResult(
                ok=False,
                platform=name,
                channel=channel,
                session_key=session_key,
                attempts=1,
                error=str(exc),
                not_implemented=True,
            )
        session_key = self.route(name, channel)
        budget = max(1, int(attempts))
        last_error = ""
        for attempt in range(1, budget + 1):
            try:
                result = adapter.send_text(channel, message)
            except PlatformNotImplemented as exc:
                self._log_delivery(
                    platform=name,
                    channel=channel,
                    attempt=1,
                    status="failed",
                    error=str(exc),
                    session_key=session_key,
                )
                return DeliveryResult(
                    ok=False,
                    platform=name,
                    channel=channel,
                    session_key=session_key,
                    attempts=1,
                    error=str(exc),
                    not_implemented=True,
                )
            except Exception as exc:  # 适配器内部炸了也要记账
                last_error = f"{type(exc).__name__}: {exc}"
                adapter.on_error(exc)
            else:
                if result.ok:
                    self._log_delivery(
                        platform=name,
                        channel=channel,
                        attempt=attempt,
                        status="ok",
                        error="",
                        session_key=session_key,
                        message_id=result.message_id,
                    )
                    return DeliveryResult(
                        ok=True,
                        platform=name,
                        channel=channel,
                        session_key=session_key,
                        attempts=attempt,
                        message_id=result.message_id,
                    )
                last_error = result.error or "发送失败（适配器未给原因）"
            self._log_delivery(
                platform=name,
                channel=channel,
                attempt=attempt,
                status="failed",
                error=last_error,
                session_key=session_key,
            )
            if attempt < budget and retry_delay:
                time.sleep(retry_delay)
        return DeliveryResult(
            ok=False,
            platform=name,
            channel=channel,
            session_key=session_key,
            attempts=budget,
            error=last_error,
        )

    def _log_delivery(
        self,
        *,
        platform: str,
        channel: str,
        attempt: int,
        status: str,
        error: str,
        session_key: str = "",
        message_id: str = "",
    ) -> None:
        record = {
            "ts": time.time(),
            "platform": platform,
            "channel": channel,
            "session_key": session_key,
            "attempt": attempt,
            "status": status,
            "error": error,
            "message_id": message_id,
        }
        try:
            _append_line(self.paths.deliveries, json.dumps(record, ensure_ascii=False))
        except OSError:
            # 账本写不进去不该让投递本身失败（但也绝不假装写过）。
            pass

    def deliveries(self, *, limit: int = 50) -> list[dict[str, Any]]:
        """读投递账本（时间正序，坏行跳过）。"""
        records: list[dict[str, Any]] = []
        for line in _read_lines(self.paths.deliveries):
            try:
                item = json.loads(line)
            except ValueError:
                continue
            if isinstance(item, dict):
                records.append(item)
        return records[-limit:] if limit > 0 else records

    # ── 平面（写侧统一：MATRIX 10.1） ──
    def _record_plane(self, result: DeliveryResult, *, message_key: str = "") -> None:
        """把**一次投递的结论**写进统一 session 平面（``gateway:<platform>``）。

        三处刻意的选择：

        1. **一次投递一条**，不是一次尝试一条。尝试次数在 ``attempts`` 字段里；
           平面回答"这个出口今天成了几次、败了几次"，逐次尝试归投递账本。
        2. 幂等键把**结论**编进去（``<key>|delivered``）：先失败后成功是一次真实的重试，
           若按 message_key 去重，账面上就只剩那条失败，看起来像永远没送到。
        3. 平面写失败**不影响投递结果**：日志写不进去不该让已经发出去的消息变成失败，
           但也绝不假装写过（异常照实抛出给调用方的是投递结果，不是"写成功"）。
        """
        key = str(message_key or "").strip()
        if not key:
            # 没有业务键：生成唯一键。内容相同的两次发送是两件真事，不能被去重。
            key = f"gw-{uuid.uuid4().hex[:12]}"
        outcome = "delivered" if result.ok else "failed"
        kind = KIND_GATEWAY_DELIVERED if result.ok else KIND_GATEWAY_DELIVERY_FAILED
        event = {
            "kind": kind,
            "session_id": plane_session_key(SOURCE_KEY_GATEWAY, result.platform),
            "platform": result.platform,
            "channel": result.channel,
            "session_key": result.session_key,
            "message_key": key,
            "attempts": int(result.attempts),
            "message_id": result.message_id,
            "error": result.error,
        }
        try:
            SessionPlane(self.data_dir).append_once(event, message_key=f"{key}|{outcome}")
        except Exception:  # 平面写坏了不该把"已经发出去"变成"投递失败"
            pass


# ── webhook 订阅 ────────────────────────────────────────────────────────────


@dataclass
class WebhookSubscription:
    """一条 webhook 订阅。照 Hermes ``webhook subscribe`` 的 9 个选项。"""

    name: str
    prompt: str = ""
    events: list[str] = field(default_factory=list)
    description: str = ""
    skills: list[str] = field(default_factory=list)
    deliver: str = "log"
    deliver_chat_id: str = ""
    secret: str = ""
    deliver_only: bool = False
    script: str = ""
    created_at: str = ""

    @property
    def path(self) -> str:
        """HTTP 路由：``/webhooks/<name>``。"""
        return f"/webhooks/{self.name}"

    def render(self, payload: Mapping[str, Any] | None = None) -> str:
        """渲染 ``{dot.notation}`` 模板。缺失的字段原样保留，便于排障。"""
        body = dict(payload or {})

        def lookup(match: str) -> str:
            node: Any = body
            for part in match.split("."):
                if not isinstance(node, Mapping) or part not in node:
                    return "{" + match + "}"
                node = node[part]
            return str(node)

        import re

        return re.sub(r"\{([A-Za-z0-9_.\-]+)\}", lambda m: lookup(m.group(1)), self.prompt)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "prompt": self.prompt,
            "events": list(self.events),
            "description": self.description,
            "skills": list(self.skills),
            "deliver": self.deliver,
            "deliver_chat_id": self.deliver_chat_id,
            "secret": self.secret,
            "deliver_only": self.deliver_only,
            "script": self.script,
            "created_at": self.created_at,
            "path": self.path,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> WebhookSubscription:
        return cls(
            name=str(raw.get("name") or ""),
            prompt=str(raw.get("prompt") or ""),
            events=[str(item) for item in raw.get("events") or []],
            description=str(raw.get("description") or ""),
            skills=[str(item) for item in raw.get("skills") or []],
            deliver=str(raw.get("deliver") or "log"),
            deliver_chat_id=str(raw.get("deliver_chat_id") or ""),
            secret=str(raw.get("secret") or ""),
            deliver_only=bool(raw.get("deliver_only")),
            script=str(raw.get("script") or ""),
            created_at=str(raw.get("created_at") or ""),
        )


class WebhookStore:
    """``<data_dir>/gateway/webhooks.json``：订阅表。

    为什么用 JSON 而不是数据库：订阅是"用户手改最想改、备份最想备"的东西，
    可读可 diff 比事务值钱（和 ``cron/jobs.json`` 同一个理由）。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, WebhookSubscription]:
        raw = _read_json(self.path, {})
        items = raw.get("subscriptions") if isinstance(raw, dict) else raw
        if not isinstance(items, list):
            return {}
        out: dict[str, WebhookSubscription] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            subscription = WebhookSubscription.from_dict(item)
            if subscription.name:
                out[subscription.name] = subscription
        return out

    def save(self, items: Mapping[str, WebhookSubscription]) -> None:
        payload = {"version": 1, "subscriptions": [item.as_dict() for item in items.values()]}
        _atomic_write(self.path, json.dumps(payload, ensure_ascii=False, indent=2))

    def upsert(self, subscription: WebhookSubscription) -> WebhookSubscription:
        items = self.load()
        items[subscription.name] = subscription
        self.save(items)
        return subscription

    def remove(self, name: str) -> bool:
        items = self.load()
        if name not in items:
            return False
        items.pop(name)
        self.save(items)
        return True


# ── 配对 ────────────────────────────────────────────────────────────────────


class PairingStore:
    """``<data_dir>/pairing/``：DM 配对码与已授权用户。

    语义（照 Hermes §3.3）：陌生人第一次 DM bot 时**不直接服务**，回一个配对码；
    管理员在宿主侧批准后才授权。这是"未授权用户不得触发 agent"的默认防线。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    @property
    def pending_path(self) -> Path:
        return self.root / "pending.json"

    @property
    def approved_path(self) -> Path:
        return self.root / "approved.json"

    def _read(self, path: Path) -> list[dict[str, Any]]:
        raw = _read_json(path, [])
        return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []

    def _write(self, path: Path, records: Iterable[dict[str, Any]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, json.dumps(list(records), ensure_ascii=False, indent=2))

    def pending(self) -> list[dict[str, Any]]:
        return self._read(self.pending_path)

    def approved(self) -> list[dict[str, Any]]:
        return self._read(self.approved_path)

    def add_pending(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """入站侧（将来由适配器调用）：登记一条待批准请求。"""
        entry = {
            "request_id": str(record.get("request_id") or secrets.token_hex(4)),
            "code": str(record.get("code") or secrets.token_hex(3)).upper(),
            "platform": str(record.get("platform") or ""),
            "user_id": str(record.get("user_id") or ""),
            "user_name": str(record.get("user_name") or ""),
            "created_at": str(record.get("created_at") or _iso_now()),
        }
        items = self.pending()
        items.append(entry)
        self._write(self.pending_path, items)
        return entry

    def approve(self, platform: str, request_id_or_code: str) -> dict[str, Any] | None:
        """按 request id **或** bot DM 给用户的配对码批准。找不到返回 ``None``。"""
        wanted = str(request_id_or_code or "").strip()
        platform_name = str(platform or "").strip().lower()
        items = self.pending()
        chosen: dict[str, Any] | None = None
        for item in items:
            if str(item.get("platform") or "").lower() != platform_name:
                continue
            if wanted in {str(item.get("request_id") or ""), str(item.get("code") or "")} or (
                wanted.upper() == str(item.get("code") or "").upper()
            ):
                chosen = item
                break
        if chosen is None:
            return None
        remaining = [item for item in items if item is not chosen]
        self._write(self.pending_path, remaining)
        approved = self.approved()
        approved.append({**chosen, "approved_at": _iso_now()})
        self._write(self.approved_path, approved)
        return chosen

    def revoke(self, platform: str, user_id: str) -> bool:
        platform_name = str(platform or "").strip().lower()
        target = str(user_id or "").strip()
        approved = self.approved()
        kept = [
            item
            for item in approved
            if not (
                str(item.get("platform") or "").lower() == platform_name
                and str(item.get("user_id") or "") == target
            )
        ]
        if len(kept) == len(approved):
            return False
        self._write(self.approved_path, kept)
        return True

    def clear_pending(self) -> int:
        items = self.pending()
        self._write(self.pending_path, [])
        return len(items)


# ── 对等网关（peer） ────────────────────────────────────────────────────────


class PeerRegistry:
    """``<data_dir>/gateway/peers.json``：跨机对等网关登记表。

    Hermes 把对端 ``API_SERVER_KEY`` 写进 ``~/.hermes/.env``（不进配置文件）。
    Orbit 本轮没有 .env 管理器，先落 ``peers.json``，但**输出一律脱敏**——
    密钥不该在 ``peer list`` 里被截图带走。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, dict[str, Any]]:
        raw = _read_json(self.path, {})
        items = raw.get("peers") if isinstance(raw, dict) else raw
        if isinstance(items, dict):
            return {str(name): dict(value) for name, value in items.items() if isinstance(value, dict)}
        if isinstance(items, list):
            return {
                str(item.get("name")): dict(item)
                for item in items
                if isinstance(item, dict) and item.get("name")
            }
        return {}

    def save(self, peers: Mapping[str, Mapping[str, Any]]) -> None:
        payload = {"version": 1, "peers": {name: dict(value) for name, value in peers.items()}}
        _atomic_write(self.path, json.dumps(payload, ensure_ascii=False, indent=2))

    def add(
        self, name: str, url: str, *, key: str = "", note: str = "", now: float | None = None
    ) -> dict[str, Any]:
        peers = self.load()
        record = {
            "name": name,
            "url": url,
            "key": key,
            "note": note,
            "updated_at": _iso_now(now),
        }
        peers[name] = record
        self.save(peers)
        return record

    def remove(self, name: str) -> bool:
        peers = self.load()
        if name not in peers:
            return False
        peers.pop(name)
        self.save(peers)
        return True

    @staticmethod
    def mask(key: str) -> str:
        """密钥脱敏：只留前 4 位。空值明确写成"（未设置）"。"""
        text = str(key or "")
        if not text:
            return "（未设置）"
        if len(text) <= 4:
            return "*" * len(text)
        return f"{text[:4]}{'*' * max(3, len(text) - 4)}"


# ── 模块级便捷入口 ──────────────────────────────────────────────────────────


def service(data_dir: str | Path | None = None) -> GatewayService:
    """按数据目录建一个服务实例（默认取 ``Settings().data_dir``）。"""
    if data_dir is None:
        from app.config import Settings

        data_dir = Settings().data_dir
    return GatewayService(data_dir)


def route(platform: str, channel: str = "", *, gateway: GatewayService | None = None) -> str:
    """模块级路由入口（等价于 :meth:`GatewayService.route`）。"""
    return (gateway or service()).route(platform, channel)


def deliver(
    message: str,
    *,
    platform: str,
    channel: str = "",
    gateway: GatewayService | None = None,
    attempts: int = DELIVER_ATTEMPTS,
    message_key: str = "",
) -> DeliveryResult:
    """模块级投递入口（等价于 :meth:`GatewayService.deliver`）。"""
    return (gateway or service()).deliver(
        message,
        platform=platform,
        channel=channel,
        attempts=attempts,
        message_key=message_key,
    )


__all__ = [
    "CHANNEL_DIRECTORY_FILENAME",
    "CRASH_LOOP_MIN_STARTS",
    "CRASH_LOOP_WINDOW_SECONDS",
    "ChannelDirectory",
    "CrashLoopReport",
    "DELIVER_ATTEMPTS",
    "DELIVERIES_FILENAME",
    "DeliveryResult",
    "DispatcherLock",
    "GATEWAY_DIRNAME",
    "GatewayPaths",
    "GatewayService",
    "HealthReport",
    "LOCK_FILENAME",
    "LockOutcome",
    "PAUSED_FILENAME",
    "PEERS_FILENAME",
    "PROCESSES_FILENAME",
    "PairingStore",
    "PeerRegistry",
    "ProcessRegistry",
    "SPAWN_LEDGER_FILENAME",
    "STARTS_FILENAME",
    "SpawnLedger",
    "StartOutcome",
    "WEBHOOKS_FILENAME",
    "WebhookStore",
    "WebhookSubscription",
    "deliver",
    "detect_crash_loop",
    "identity",
    "now_iso",
    "pid_alive",
    "process_start_time",
    "read_start_stamps",
    "route",
    "service",
    "write_json",
]
