"""投递账本（``app/delivery_ledger.py``）的契约测试。

账本这一层最容易"假绿"：只要不抛异常，看起来就"记上了"。所以这里盯的是**判据本身**：

1. **幂等判据**：同一 ``message_key`` 最新记录是 ``delivered`` 时，``claim()`` 必须
   ``already=True``（重复投递要能被识别——这是至少一次语义的必要条件）。
2. **重试不被挡住**：``pending`` / ``failed`` 状态下 ``claim()`` 仍然放行。若挡住，
   进程在"认领后、发送前"崩掉，那条消息就**永远送不出去**；丢失比重复贵得多。
3. **状态流转**：``mark`` 追加一行、``attempts`` 自增、最后一行说了算。
4. **``pending(older_than)`` 按时间筛**，且按 key 去重（否则重试三轮报三条待办）。
5. **与 ``app/gateway.py`` 的账本对齐**：同一个文件、同一套字段名，
   历史 gateway 行（``status="ok"``、无 ``id``/``message_key``）读进来要能归一。
"""

from __future__ import annotations

import json

import pytest

from app import gateway as gw
from app.delivery_ledger import (
    STATUS_DELIVERED,
    STATUS_FAILED,
    STATUS_PENDING,
    DeliveryLedger,
    DeliveryLedgerError,
)


def _ledger(tmp_path) -> DeliveryLedger:
    return DeliveryLedger(tmp_path / "data")


# ── 位置与字段对齐 ──────────────────────────────────────────────────────────


def test_ledger_shares_the_gateway_file(tmp_path):
    """账本落在 gateway 已有的 ``deliveries.jsonl`` 上——**不新造第二个文件**。"""
    ledger = _ledger(tmp_path)
    assert ledger.path == gw.GatewayPaths(tmp_path / "data").deliveries
    assert ledger.path.name == gw.DELIVERIES_FILENAME


def test_written_rows_carry_gateway_field_names(tmp_path):
    """写出的行带 gateway 的字段名（``attempt`` / ``session_key`` / ``gateway_status``）。"""
    ledger = _ledger(tmp_path)
    ledger.claim("k1", channel="c1", platform="ntfy", session_key="ntfy:home")
    raw = json.loads(ledger.path.read_text(encoding="utf-8").splitlines()[0])

    for field in ("ts", "platform", "channel", "session_key", "attempt", "status", "error"):
        assert field in raw, f"缺 gateway 的字段 {field}"
    assert raw["message_key"] == "k1"
    assert raw["attempts"] == raw["attempt"] == 0
    assert raw["gateway_status"] == "failed", "pending 在 gateway 的口径里不是 ok"
    assert isinstance(raw["ts"], float), "ts 是 epoch 秒（与 gateway 一致）"


def test_gateway_reader_can_read_our_rows(tmp_path):
    """``GatewayService.deliveries()`` 读本模块写的行不会缺字段。"""
    ledger = _ledger(tmp_path)
    ledger.claim("k1", platform="ntfy", channel="c1")
    ledger.mark("k1", STATUS_DELIVERED, attempts=1)

    records = gw.GatewayService(tmp_path / "data", adapters={}).deliveries(limit=10)
    assert len(records) == 2
    assert records[-1]["status"] == STATUS_DELIVERED
    assert records[-1]["platform"] == "ntfy"
    assert records[-1]["attempt"] == 1


def test_legacy_gateway_rows_are_normalized(tmp_path):
    """历史 gateway 行（``ok`` + 无 ``id``/``message_key``）读进来要能归一。"""
    ledger = _ledger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "ts": 1_700_000_000.0,
        "platform": "ntfy",
        "channel": "c1",
        "session_key": "ntfy:home",
        "attempt": 2,
        "status": "ok",
        "error": "",
        "message_id": "m-1",
    }
    ledger.path.write_text(json.dumps(legacy) + "\n", encoding="utf-8")

    records = ledger.records()
    assert len(records) == 1
    assert records[0]["status"] == STATUS_DELIVERED, "ok 必须归一成 delivered"
    assert records[0]["attempts"] == 2
    assert records[0]["id"] == 0, "不编造假 id——那会让 since 游标失效"
    assert records[0]["message_key"] == ""


# ── 幂等 ────────────────────────────────────────────────────────────────────


def test_claim_is_idempotent_after_delivery(tmp_path):
    """同 key 第二次 claim：已送达 → ``already=True``，**不重复投递**。"""
    ledger = _ledger(tmp_path)
    first = ledger.claim("k1", platform="ntfy", channel="c1")
    assert first.already is False
    ledger.mark("k1", STATUS_DELIVERED, attempts=1)

    second = ledger.claim("k1", platform="ntfy", channel="c1")
    assert second.already is True
    assert second.record is not None
    assert second.record["status"] == STATUS_DELIVERED
    assert len(ledger.records()) == 2, "already=True 时不该再写新行"


def test_claim_allows_retry_while_pending(tmp_path):
    """``pending`` 状态下 claim 仍然放行：崩在"认领后、发送前"的消息不能永远送不出去。"""
    ledger = _ledger(tmp_path)
    assert ledger.claim("k1").already is False
    assert ledger.claim("k1").already is False
    ledger.mark("k1", STATUS_FAILED, error="网络超时")
    assert ledger.claim("k1").already is False, "failed 也必须允许再试"


def test_claim_rejects_an_empty_key(tmp_path):
    """空 key 无法判重，必须当场报错而不是记一条永远匹配不上的账。"""
    with pytest.raises(DeliveryLedgerError):
        _ledger(tmp_path).claim("   ")


def test_claim_different_keys_are_independent(tmp_path):
    ledger = _ledger(tmp_path)
    ledger.claim("k1")
    ledger.mark("k1", STATUS_DELIVERED)
    assert ledger.claim("k2").already is False
    assert ledger.claim("k1").already is True


# ── 状态流转与统计 ──────────────────────────────────────────────────────────


def test_mark_increments_attempts(tmp_path):
    """``attempts`` 不传时按上次 +1：账本必须记清"试了几次"（放弃与否的唯一依据）。"""
    ledger = _ledger(tmp_path)
    ledger.claim("k1")
    ledger.mark("k1", STATUS_FAILED, error="第一次失败")
    ledger.mark("k1", STATUS_FAILED, error="第二次失败")
    latest = ledger.latest("k1")
    assert latest is not None
    assert latest["attempts"] == 2
    assert latest["error"] == "第二次失败"
    assert latest["status"] == STATUS_FAILED


def test_mark_rejects_unknown_status(tmp_path):
    with pytest.raises(DeliveryLedgerError):
        _ledger(tmp_path).mark("k1", "sent-ish")


def test_mark_accepts_the_legacy_ok_word(tmp_path):
    """``ok`` 是 gateway 的词，写进来要归一成 ``delivered``（别造两套词表）。"""
    ledger = _ledger(tmp_path)
    ledger.claim("k1")
    ledger.mark("k1", "ok", attempts=1)
    latest = ledger.latest("k1")
    assert latest is not None and latest["status"] == STATUS_DELIVERED


def test_stats_count_keys_not_rows(tmp_path):
    """行数与 key 数是两件事：同一 key 的多次尝试只算一条账。"""
    ledger = _ledger(tmp_path)
    ledger.claim("k1")
    ledger.mark("k1", STATUS_DELIVERED, attempts=1)
    ledger.claim("k2")
    ledger.mark("k2", STATUS_FAILED, error="挂了")

    stats = ledger.stats()
    assert stats["rows"] == 4
    assert stats["keys"] == 2
    assert stats["delivered"] == 1
    assert stats["failed"] == 1
    assert stats["undelivered"] == 1


def test_pending_filters_by_age(tmp_path):
    """``pending(older_than)`` 只返回"够老"的未送达条目，且按 key 去重。"""
    ledger = _ledger(tmp_path)
    ledger.claim("old", now=1000.0)
    ledger.claim("fresh", now=1990.0)
    ledger.claim("done", now=1000.0)
    ledger.mark("done", STATUS_DELIVERED, attempts=1, now=1001.0)

    waiting = ledger.pending(older_than=60.0, now=2000.0)
    assert [item["message_key"] for item in waiting] == ["old"]
    assert {item["status"] for item in waiting} == {STATUS_PENDING}

    # older_than=0 表示"所有未送达的"（含刚认领的）。
    everything = ledger.pending(older_than=0.0, now=2000.0)
    assert {item["message_key"] for item in everything} == {"old", "fresh"}


def test_pending_keeps_only_the_latest_row_per_key(tmp_path):
    """同一 key 重试三轮：待办只有一条，不是三条。"""
    ledger = _ledger(tmp_path)
    for _ in range(3):
        ledger.claim("k1", now=1000.0)
        ledger.mark("k1", STATUS_FAILED, error="还是不行", now=1000.0)
    waiting = ledger.pending(older_than=0.0, now=2000.0)
    assert len(waiting) == 1
    assert waiting[0]["attempts"] == 3


def test_ledger_is_append_only(tmp_path):
    """每次状态变化都追加一行——原地 UPDATE 会丢掉失败原因，事后查不了。"""
    ledger = _ledger(tmp_path)
    ledger.claim("k1")
    ledger.mark("k1", STATUS_FAILED, error="第一次")
    ledger.mark("k1", STATUS_DELIVERED, attempts=2)
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert [json.loads(line)["status"] for line in lines] == [
        STATUS_PENDING,
        STATUS_FAILED,
        STATUS_DELIVERED,
    ]
