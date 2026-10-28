"""投递账本：至少一次（at-least-once）投递的幂等账，与 gateway 共用同一份文件。

对标 ``handoff/hermes-parity/research/03-persistence-layer.md`` §1.3 / §5.2
（MATRIX L10 的 10.6）：Hermes 的 ``delivery_obligations(obligation_id, state,
attempts, last_error, owner_pid)`` —— 123 行全部 ``delivered``，语义是
"发过就要留痕，崩溃重启后不重复、不丢"。

## 为什么不新造一份账本

``app/gateway.py`` 已经有这个雏形（``_log_delivery`` → ``gateway/deliveries.jsonl``，
每次尝试一行）。**再造一份互不兼容的账本，等于让"到底送没送到"有两个互相矛盾的答案。**
所以本模块：

- **落盘位置复用 gateway 的**：``<data_dir>/gateway/deliveries.jsonl``
  （规格书写的是 ``<data_dir>/deliveries.jsonl``；实际账本早就在 ``gateway/`` 下，
  这里选"同一个文件"而不是"规格书上的路径"，因为两份账本比路径不一致糟得多。
  需要别的落点时显式传 ``path=``）。
- **字段对齐**：gateway 的字段是 ``ts / platform / channel / session_key / attempt /
  status / error / message_id``，本模块写的一行同时带这些名字
  （``attempt`` 与 ``attempts`` 同值、``session_key`` 照旧），所以
  ``GatewayService.deliveries()`` 读本模块写的行不会缺字段。
- **状态词表对齐**：gateway 用 ``ok`` / ``failed``，Hermes 用 ``delivered``。
  写侧统一写 ``delivered`` 并额外写 ``gateway_status``（``ok``/``failed``），
  读侧把历史的 ``ok`` 归一成 ``delivered``——两边都能读，谁也不用改。

## 幂等判据（本模块唯一的硬约定）

``claim(message_key)`` 在**同一 ``message_key`` 已有 ``delivered`` 记录**时返回
``already=True``，调用方据此跳过重复投递。除此之外一律放行：

- 最新记录是 ``pending`` / ``failed`` / ``dead`` → 允许再试（``already=False``）。

为什么 ``pending`` 也放行：``claim`` 之后进程崩了，消息根本没发出去。若 ``pending``
就挡住重试，那条消息**永远送不出去**——而至少一次语义下"重复"比"丢失"便宜得多
（重复还能被 ``message_key`` 识别出来，丢失连发现都难）。这条取舍写在这里，
而不是悄悄按"看起来更严格"的方式实现。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.gateway import DELIVERIES_FILENAME, GATEWAY_DIRNAME
from app.session_plane import AppendLog

#: 状态词表。``pending`` = 已认领未确认，``delivered`` = 送达（终态），
#: ``failed`` = 本次失败可重试，``dead`` = 放弃（超过重试预算，不再自动重投）。
STATUS_PENDING = "pending"
STATUS_DELIVERED = "delivered"
STATUS_FAILED = "failed"
STATUS_DEAD = "dead"

STATUSES = (STATUS_PENDING, STATUS_DELIVERED, STATUS_FAILED, STATUS_DEAD)

#: 终态：``delivered`` 之后不再改（重复投递的判据就是它）。
_FINAL = {STATUS_DELIVERED}

#: 历史 gateway 记录里的 ``status`` 词 → 本模块词表。
_LEGACY_STATUS = {"ok": STATUS_DELIVERED, "sent": STATUS_DELIVERED, "failed": STATUS_FAILED}


class DeliveryLedgerError(RuntimeError):
    """可预期的账本错误（空 key、非法状态）。CLI 侧翻成 ``CliError``。"""


@dataclass(frozen=True)
class Claim:
    """一次认领的结果。``already=True`` 时 ``record`` 是那条已送达的历史记录。"""

    message_key: str
    already: bool
    record: dict[str, Any] | None = None
    ident: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_key": self.message_key,
            "already": self.already,
            "record": self.record,
            "id": self.ident,
        }


class DeliveryLedger:
    """``deliveries.jsonl`` 的读写门面：认领、标记、待办、统计。

    追加日志的语义在这里同样成立：**同一 ``message_key`` 可以有多行**，
    读侧"最后一行说了算"。这样"认领 → 失败 → 重试 → 送达"四步都能留痕，
    而不用原地 UPDATE（原地改会丢掉失败原因，事后查不了）。
    """

    def __init__(self, data_dir: Path, *, path: Path | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.log = AppendLog(path or (self.data_dir / GATEWAY_DIRNAME / DELIVERIES_FILENAME))

    # ── 路径 ──
    @property
    def path(self) -> Path:
        return self.log.path

    # ── 读 ──
    def records(self, *, limit: int = 0) -> list[dict[str, Any]]:
        """全部记录（归一化后，文件顺序）。``limit>0`` 时取**最近** limit 条。"""
        items = [_normalize(item) for item in self.log.read_all()]
        if limit and limit > 0:
            return items[-limit:]
        return items

    def latest(self, message_key: str) -> dict[str, Any] | None:
        """某 key 的最新一条记录（没有则 ``None``）。"""
        found = None
        for item in self.records():
            if item.get("message_key") == message_key:
                found = item
        return found

    def keys(self) -> list[str]:
        """出现过 message_key（保持首次出现顺序）。"""
        seen: list[str] = []
        for item in self.records():
            key = str(item.get("message_key") or "")
            if key and key not in seen:
                seen.append(key)
        return seen

    def pending(self, older_than: float = 0.0, *, now: float | None = None) -> list[dict[str, Any]]:
        """未送达、且最后活动时间已超过 ``older_than`` 秒的条目（每个 key 一条）。

        ``older_than`` 按秒算（``3600`` = 一小时），默认 ``0`` 表示"所有未送达的"。
        按 key 去重：同一 key 的多次失败只算一件待办，否则重试三轮就会报三条。
        """
        moment = time.time() if now is None else float(now)
        latest: dict[str, dict[str, Any]] = {}
        for item in self.records():
            key = str(item.get("message_key") or "")
            if not key:
                continue
            latest[key] = item
        waiting: list[dict[str, Any]] = []
        for item in latest.values():
            if str(item.get("status") or "") in _FINAL:
                continue
            age = moment - _as_float(item.get("ts"))
            if age >= max(0.0, float(older_than)):
                waiting.append(item)
        return waiting

    def stats(self, *, now: float | None = None) -> dict[str, Any]:
        """账本概览：按 key 统计终态分布 + 行数（行数与 key 数不同是正常的）。"""
        moment = time.time() if now is None else float(now)
        latest: dict[str, dict[str, Any]] = {}
        rows = 0
        for item in self.records():
            rows += 1
            key = str(item.get("message_key") or "")
            if key:
                latest[key] = item
        counts = {status: 0 for status in STATUSES}
        for item in latest.values():
            status = str(item.get("status") or STATUS_PENDING)
            counts[status] = counts.get(status, 0) + 1
        stamps = [_as_float(item.get("ts")) for item in latest.values()]
        stamps = [value for value in stamps if value > 0]
        return {
            "rows": rows,
            "keys": len(latest),
            "delivered": counts.get(STATUS_DELIVERED, 0),
            "failed": counts.get(STATUS_FAILED, 0),
            "pending": counts.get(STATUS_PENDING, 0),
            "dead": counts.get(STATUS_DEAD, 0),
            "undelivered": sum(
                1 for item in latest.values() if str(item.get("status") or "") not in _FINAL
            ),
            "last_ts": max(stamps) if stamps else 0.0,
            "now": moment,
        }

    # ── 写 ──
    def claim(
        self,
        message_key: str,
        *,
        channel: str = "",
        platform: str = "",
        session_key: str = "",
        message_id: str = "",
        now: float | None = None,
    ) -> Claim:
        """认领一条投递：已有 ``delivered`` 则 ``already=True``，否则记一条 ``pending``。

        幂等判据只有一条：**同一 ``message_key`` 的最新记录是 ``delivered``**。
        （不是"存在过任何记录"——见模块 docstring 里关于 pending 重试的取舍。）
        """
        key = str(message_key or "").strip()
        if not key:
            raise DeliveryLedgerError("claim 需要 message_key（空 key 无法判重）")

        previous = self.latest(key)
        if previous is not None and str(previous.get("status") or "") in _FINAL:
            return Claim(
                message_key=key,
                already=True,
                record=previous,
                ident=_as_int(previous.get("id")),
            )

        record = self._write(
            key,
            STATUS_PENDING,
            channel=channel,
            platform=platform,
            session_key=session_key,
            message_id=message_id,
            # 认领**不清零**已试次数：清零会让"重试了几轮"这件事从账本里消失，
            # 而那是判断"该不该放弃"的唯一依据。
            attempts=_as_int((previous or {}).get("attempts")),
            error="",
            now=now,
        )
        return Claim(message_key=key, already=False, record=record, ident=_as_int(record.get("id")))

    def mark(
        self,
        message_key: str,
        status: str,
        error: str | None = None,
        *,
        channel: str = "",
        platform: str = "",
        session_key: str = "",
        message_id: str = "",
        attempts: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """把某 key 的状态推进到 ``status``（追加一行，最后一行说了算）。

        ``attempts`` 不传时按"上一次的 attempts + 1"递增：调用方通常不知道总次数，
        但账本必须记清"试了几次"——那是判断"该不该放弃"的唯一依据。
        """
        key = str(message_key or "").strip()
        if not key:
            raise DeliveryLedgerError("mark 需要 message_key")
        normalized = str(status or "").strip().lower()
        normalized = _LEGACY_STATUS.get(normalized, normalized)
        if normalized not in STATUSES:
            raise DeliveryLedgerError(
                f"未知投递状态：{status!r}（可选 {'/'.join(STATUSES)}）"
            )
        previous = self.latest(key) or {}
        total = attempts if attempts is not None else _as_int(previous.get("attempts")) + 1
        return self._write(
            key,
            normalized,
            channel=channel or str(previous.get("channel") or ""),
            platform=platform or str(previous.get("platform") or ""),
            session_key=session_key or str(previous.get("session_key") or ""),
            message_id=message_id or str(previous.get("message_id") or ""),
            attempts=total,
            error="" if error is None else str(error),
            now=now,
        )

    def _write(
        self,
        message_key: str,
        status: str,
        *,
        channel: str,
        platform: str,
        session_key: str,
        message_id: str,
        attempts: int,
        error: str,
        now: float | None,
    ) -> dict[str, Any]:
        """写一行。字段集刻意与 ``app/gateway.py`` 的 ``_log_delivery`` 对齐。"""
        stamp = time.time() if now is None else float(now)
        return self.log.append(
            {
                "ts": stamp,
                "channel": str(channel),
                "platform": str(platform),
                "message_key": message_key,
                "status": status,
                # gateway 的字段名：旧读者（GatewayService.deliveries）照样能读。
                "gateway_status": "ok" if status == STATUS_DELIVERED else "failed",
                "session_key": str(session_key),
                "attempt": int(attempts),
                "attempts": int(attempts),
                "message_id": str(message_id),
                "error": error,
            }
        )


def _normalize(item: Mapping[str, Any]) -> dict[str, Any]:
    """把"本模块写的行"与"gateway 历史行"折成同一形状。

    历史行的差别：没有 ``id`` / ``message_key`` / ``attempts``，``status`` 是 ``ok``。
    ``id`` 缺失时补 ``0``（不编造一个假 id——那会让 ``since`` 游标失效）。
    """
    record = dict(item)
    status = str(record.get("status") or "").strip().lower()
    record["status"] = _LEGACY_STATUS.get(status, status or STATUS_PENDING)
    record["id"] = _as_int(record.get("id"))
    record["ts"] = _as_float(record.get("ts"))
    if "attempts" not in record:
        record["attempts"] = _as_int(record.get("attempt"))
    record["message_key"] = str(record.get("message_key") or "")
    record["platform"] = str(record.get("platform") or "")
    record["channel"] = str(record.get("channel") or "")
    record["error"] = str(record.get("error") or "")
    return record


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_float(value: Any) -> float:
    if isinstance(value, bool):
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
    # 兼容 ISO 串（有人手工往账本里补过一行人类可读时间）。
    try:
        from datetime import UTC, datetime

        stamp = datetime.fromisoformat(text)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=UTC)
        return stamp.timestamp()
    except ValueError:
        return 0.0


__all__ = [
    "STATUSES",
    "STATUS_DEAD",
    "STATUS_DELIVERED",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "Claim",
    "DeliveryLedger",
    "DeliveryLedgerError",
]
