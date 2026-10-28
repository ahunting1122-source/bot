"""事件溯源：每流一条只追加的 JSONL，``seq`` 当游标，``event_id`` 内容寻址。

对标 ``handoff/hermes-parity/research/03-persistence-layer.md`` §5（MATRIX L10 的 10.14）：
Hermes 用 ``hosted_room_*`` 14 张表做事件溯源——``seq`` 单调、``event_id`` UNIQUE、
游标表 + watcher。Orbit 这边**加不了表**（``app/storage.py`` 与 ``alembic/`` 是禁改区，
见 ``app/session_plane.py`` 的模块 docstring），所以用 JSONL 复刻同样的语义：

============  ==========================================================
Hermes 表列   Orbit 的等价物
============  ==========================================================
``seq``       ``<data_dir>/events/<stream>.jsonl`` 里**每流独立**的自增整数
``event_id``  ``sha256(stream|seq|payload)[:16]``，落盘时算一次、永不再变
游标表        调用方自己保存上一次的 ``seq``（``since(stream, cursor)``）
watcher       :meth:`EventLog.watch` 注册的进程内回调
============  ==========================================================

## 游标必须是 ``seq``，不能是时间戳

这条坑 ``session_plane`` 已经踩过并写在它的 docstring 里：同一毫秒内的两条事件
时间戳完全相同，用 ``ts > cursor_ts`` 增量读会把它们**一起漏掉或一起重复**。
``seq`` 没有这个问题，所以本模块的游标语义就是 ``seq > cursor``，
``ts`` 只用于人看与跨流排序。

## 幂等

``event_id`` 里含 ``seq``，所以"同一条事件写两次"会拿到两个不同的 ``seq``、
两个不同的 ``event_id``——**光靠 event_id 去不了重**。做法是把去重放在**分配 seq
之前**：用 ``sha256(stream|payload)`` 当内容指纹，命中已有事件就直接返回它的 ``seq``
（不落新行）。于是：

- 同一条事件重复 append → 返回**同一个 seq**，文件里只有一行；
- 真正重复发生的事（两次心跳、正文恰好相同的两次投递）应当由调用方在 payload 里
  带上区分字段（时间、序号、消息键），或者显式传 ``key=`` 当幂等键
  ——本模块不会替调用方猜"这两条是不是一回事"。

## compact 要留下被裁的痕迹

``compact(stream, keep)`` 只保留最后 ``keep`` 条，并在重写后的文件**第一行**写一条
``log.compacted`` 事件，记下被丢掉的 ``seq`` 区间。审计时"这里被裁过"必须查得到——
只删不记等于把历史悄悄抹掉，那和"事件溯源"的存在意义正好相反。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: 事件的落点：``<data_dir>/events/<stream>.jsonl``。
#: 与 ``sessions/`` 平级：平面记"会话这一侧发生了什么"，事件日志记"某个子系统
#: 的状态机走到了哪一步"，两者的保留策略不同（平面只增不裁，事件流可 compact）。
EVENTS_DIRNAME = "events"

#: 压缩事件的 ``kind``。它自己也是一条事件（占一个新 ``seq``），
#: 这样"最后一条 seq"在压缩后依然是单调的，游标不会倒退。
KIND_COMPACTED = "log.compacted"

#: 流名的合法字符。流名会变成文件名，``/`` 与 ``..`` 必须挡掉（路径穿越）。
_STREAM_SAFE = set("abcdefghijklmnopqrstuvwxyz0123456789-_.")

#: ``event_id`` 取哈希前多少位。16 位十六进制 = 64 bit，对一个本地日志来说
#: 碰撞概率可以忽略，同时还能在表格里一眼读完。
EVENT_ID_CHARS = 16


class EventLogError(RuntimeError):
    """可预期的错误（空流名、非法流名、坏游标）。CLI 侧翻成 ``CliError``。"""


def now_iso(moment: float | None = None) -> str:
    """带时区的 ISO 时间串（与 ``app.session_plane.now_iso`` 同格式，便于跨模块对齐）。"""
    if moment is None:
        return datetime.now(UTC).isoformat()
    return datetime.fromtimestamp(moment, UTC).isoformat()


def event_id_for(stream: str, seq: int, payload: Mapping[str, Any]) -> str:
    """内容寻址的 ``event_id``：``sha256(stream|seq|payload)[:16]``。

    为什么把 ``seq`` 也编进去：同一份 payload 在同一流里出现两次是**两件事**
    （两次真实发生），它们必须有不同的 id；而 ``stream`` 编进去让同一条 payload
    在两个流里不会撞成同一个 id——那样"这条事件属于哪个流"就要靠外部上下文补，
    审计时最容易出错。
    """
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{stream}|{int(seq)}|{body}".encode())
    return digest.hexdigest()[:EVENT_ID_CHARS]


def fingerprint(stream: str, payload: Mapping[str, Any]) -> str:
    """幂等指纹：只看"哪个流 + 什么内容"，**不含 seq**（seq 是分配出来的）。

    与 :func:`event_id_for` 的分工：指纹用来判"这条我是不是已经写过了"，
    ``event_id`` 用来标识"落盘的那一行"。两者混用就会出现"同一个 event_id 对应
    两行"或"重复写时 id 变了导致判重失效"。
    """
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{stream}|{body}".encode()).hexdigest()[:EVENT_ID_CHARS]


# ── 单流文件读写 ────────────────────────────────────────────────────────────


def _atomic_write(path: Path, text: str) -> None:
    """临时文件 + ``os.replace``：compact 重写时中途崩溃不能留下半个文件。

    compact 是**唯一**会重写已有内容的操作，所以它比追加更需要原子性：
    追加崩了只是少一行，重写崩了会把整条流抹掉。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


class EventLog:
    """每流一条追加日志。``append`` / ``since`` / ``watch`` / ``streams`` / ``compact``。

    ``root`` 是 ``events/`` 目录本身（不是 data_dir）：调用方决定落点，
    测试可以塞 ``tmp_path`` 而不用建整个数据目录。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        #: 追加与 compact 必须互斥：一边在重写文件、一边在追加，行会丢。
        self._lock = threading.RLock()
        #: 每个流的 watcher 列表（进程内）。跨进程通知不存在，也不假装存在。
        self._watchers: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
        #: 每流的幂等索引缓存：``{stream: (文件大小, {指纹: seq})}``。
        #: 按文件大小失效：文件没变就不重扫，变了（本进程或别的进程写过）就重建。
        self._index_cache: dict[str, tuple[int, dict[str, int]]] = {}

    # ── 路径 ──
    def path_for(self, stream: str) -> Path:
        """流文件路径。流名非法直接报错——它会变成文件名，必须挡路径穿越。"""
        name = str(stream or "").strip()
        if not name:
            raise EventLogError("流名不能为空")
        if any(char not in _STREAM_SAFE for char in name) or name in {".", ".."}:
            raise EventLogError(
                f"流名只能含字母数字与 -_.：{name!r}"
                "（流名会变成文件名，放行路径分隔符等于开一个目录穿越口子）"
            )
        return self.root / f"{name}.jsonl"

    def streams(self) -> list[str]:
        """已存在的流名（按文件名排序，稳定的展示顺序）。"""
        if not self.root.exists():
            return []
        names: list[str] = []
        for item in sorted(self.root.iterdir()):
            if item.is_file() and item.suffix == ".jsonl":
                names.append(item.stem)
        return names

    # ── 读 ──
    def read_all(self, stream: str) -> list[dict[str, Any]]:
        """按文件顺序读出整条流。坏行跳过（一行写坏不该让整条流读不出来）。"""
        path = self.path_for(stream)
        if not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        records: list[dict[str, Any]] = []
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = json.loads(stripped)
            except ValueError:
                continue
            if isinstance(item, dict) and "seq" in item:
                records.append(item)
        return records

    def since(self, stream: str, cursor: int = 0, *, limit: int = 0) -> list[dict[str, Any]]:
        """**按 seq 游标**增量读：返回 ``seq > cursor`` 的事件（文件顺序）。

        为什么不是时间戳：见模块 docstring——同毫秒内顺序会乱。
        ``limit > 0`` 时只取最早的那几条（增量消费的语义是"从头接着读"，
        与"看最近 N 条"是两件事，后者用 :meth:`tail`）。
        """
        floor = _as_int(cursor)
        fresh = [item for item in self.read_all(stream) if _as_int(item.get("seq")) > floor]
        if limit and limit > 0:
            return fresh[:limit]
        return fresh

    def tail(self, stream: str, count: int = 20) -> list[dict[str, Any]]:
        """最近 ``count`` 条（正序返回）。"""
        items = self.read_all(stream)
        if count and count > 0:
            return items[-count:]
        return items

    def last_seq(self, stream: str) -> int:
        """当前最大 ``seq``（空流为 0）。调用方把它存起来当下一轮的游标。"""
        best = 0
        for item in self.read_all(stream):
            best = max(best, _as_int(item.get("seq")))
        return best

    def get(self, stream: str, seq: int) -> dict[str, Any] | None:
        """按 seq 取一条（找不到返回 ``None``）。"""
        target = _as_int(seq)
        for item in self.read_all(stream):
            if _as_int(item.get("seq")) == target:
                return item
        return None

    # ── 写 ──
    def append(
        self,
        stream: str,
        event: Mapping[str, Any],
        *,
        key: str = "",
        ts: str = "",
    ) -> int:
        """追加一条事件，返回它的 ``seq``。**重复 append 同一内容返回同一个 seq**。

        ``key`` 是可选的显式幂等键：给了就按它判重（适合"这条事件在业务上唯一"），
        没给就按内容指纹判重（适合"内容相同就是同一条"）。
        """
        payload = dict(event)
        if not payload:
            raise EventLogError("事件不能为空（空事件无法内容寻址，也无法判重）")
        with self._lock:
            path = self.path_for(stream)
            index = self._index(stream)
            mark = str(key or "").strip() or fingerprint(stream, payload)
            hit = index.get(mark)
            if hit is not None:
                return int(hit)
            seq = self._last_seq_locked(stream) + 1
            record = {
                "stream": stream,
                "seq": seq,
                "event_id": event_id_for(stream, seq, payload),
                "ts": ts or now_iso(),
                "payload": payload,
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                handle.flush()
                # fsync：事件溯源的全部价值在于"已经发生的事一定留痕"，
                # 少了这一步，断电后丢的那几行会让下游游标直接跳过一段状态。
                os.fsync(handle.fileno())
            index[mark] = seq
            self._index_cache[stream] = (path.stat().st_size, index)
            self._notify(stream, record)
            return seq

    # ── 订阅 ──
    def watch(self, stream: str, fn: Callable[[dict[str, Any]], None]) -> Callable[[], None]:
        """注册 watcher，返回**退订函数**。

        只在本进程内通知（跨进程要靠轮询 ``since``）：本地进程里没有可用的
        跨进程事件总线，假装有会让调用方依赖一个不存在的保证。
        """
        name = str(stream or "").strip() or "*"
        with self._lock:
            self._watchers.setdefault(name, []).append(fn)

        def unsubscribe() -> None:
            with self._lock:
                bucket = self._watchers.get(name) or []
                if fn in bucket:
                    bucket.remove(fn)

        return unsubscribe

    def _notify(self, stream: str, record: dict[str, Any]) -> None:
        """通知该流与通配（``*``）的 watcher。watcher 抛异常不影响写入。"""
        for name in (stream, "*"):
            for fn in list(self._watchers.get(name) or []):
                try:
                    fn(record)
                except Exception:  # 观察者炸了不该让"已经写成功的事件"变成失败
                    continue

    # ── 压缩 ──
    def compact(self, stream: str, keep: int) -> dict[str, Any]:
        """只保留最后 ``keep`` 条，并在新文件第一行记下被丢掉的 ``seq`` 区间。

        返回 ``{stream, dropped, dropped_from, dropped_to, kept, compaction_seq}``；
        没有可丢的（条数 ≤ ``keep``）时 ``dropped=0`` 且**不写压缩事件**
        ——没裁过就不该在审计里留下"这里被裁过"的假痕迹。
        """
        if keep < 0:
            raise EventLogError(f"keep 必须 ≥ 0：{keep}")
        with self._lock:
            records = self.read_all(stream)
            if len(records) <= keep:
                return {
                    "stream": stream,
                    "dropped": 0,
                    "dropped_from": 0,
                    "dropped_to": 0,
                    "kept": len(records),
                    "compaction_seq": 0,
                    "event_id": "",
                }
            dropped = records[: len(records) - keep]
            kept = records[len(records) - keep :]
            first_seq = _as_int(dropped[0].get("seq"))
            last_seq = _as_int(dropped[-1].get("seq"))
            highest = max(_as_int(item.get("seq")) for item in records)
            compaction_seq = highest + 1
            marker_payload = {
                "kind": KIND_COMPACTED,
                "stream": stream,
                "dropped": len(dropped),
                "dropped_from": first_seq,
                "dropped_to": last_seq,
                "kept": len(kept),
            }
            marker = {
                "stream": stream,
                "seq": compaction_seq,
                "event_id": event_id_for(stream, compaction_seq, marker_payload),
                "ts": now_iso(),
                "payload": marker_payload,
            }
            body = "\n".join(
                json.dumps(item, ensure_ascii=False) for item in [marker, *kept]
            )
            _atomic_write(self.path_for(stream), body + "\n")
            # 索引缓存失效：重写之后指纹集合变了（被丢的事件已经不在文件里）。
            self._index_cache.pop(stream, None)
            self._notify(stream, marker)
            return {
                "stream": stream,
                "dropped": len(dropped),
                "dropped_from": first_seq,
                "dropped_to": last_seq,
                "kept": len(kept),
                "compaction_seq": compaction_seq,
                "event_id": str(marker["event_id"]),
            }

    # ── 内部 ──
    def _index(self, stream: str) -> dict[str, int]:
        """幂等索引（``指纹 → seq``），按文件大小缓存。

        没有单独的索引文件：事件流本身可以 compact、可以被别的进程改写，
        外挂索引与文件之间迟早不一致；而"重扫一遍"在本地日志的规模下足够便宜。
        """
        path = self.path_for(stream)
        size = path.stat().st_size if path.exists() else 0
        cached = self._index_cache.get(stream)
        if cached is not None and cached[0] == size:
            return cached[1]
        index: dict[str, int] = {}
        for item in self.read_all(stream):
            payload = item.get("payload")
            body = payload if isinstance(payload, Mapping) else {}
            index.setdefault(fingerprint(stream, body), _as_int(item.get("seq")))
        self._index_cache[stream] = (size, index)
        return index

    def _last_seq_locked(self, stream: str) -> int:
        return self.last_seq(stream)


__all__ = [
    "EVENTS_DIRNAME",
    "EVENT_ID_CHARS",
    "EventLog",
    "EventLogError",
    "KIND_COMPACTED",
    "event_id_for",
    "fingerprint",
    "now_iso",
]
