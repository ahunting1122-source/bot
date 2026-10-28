"""``app/event_log.py`` 的契约测试：seq、幂等、游标、watcher、compact。

三条边界：**不碰数据库**（事件日志是纯文件）、**不依赖时钟**（时间戳只当显示字段）、
**不假设进程外通知**（watcher 只在本进程内）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.event_log import (
    EVENT_ID_CHARS,
    KIND_COMPACTED,
    EventLog,
    EventLogError,
    event_id_for,
    fingerprint,
)


def _log(tmp_path: Path) -> EventLog:
    return EventLog(tmp_path / "events")


# ── seq ─────────────────────────────────────────────────────────────────────


def test_seq_is_monotonic_and_per_stream(tmp_path: Path) -> None:
    """``seq`` 每流独立、单调递增——两条流互不干扰（Hermes 是每张表一个序列）。"""
    log = _log(tmp_path)
    assert log.append("alpha", {"n": 1}) == 1
    assert log.append("alpha", {"n": 2}) == 2
    assert log.append("beta", {"n": 1}) == 1  # 新流从 1 重新开始
    assert log.append("alpha", {"n": 3}) == 3
    assert log.last_seq("alpha") == 3
    assert log.last_seq("beta") == 1
    assert sorted(log.streams()) == ["alpha", "beta"]


def test_since_reads_by_seq_cursor_not_timestamp(tmp_path: Path) -> None:
    """游标是 ``seq``：**同一毫秒**的两条事件也不会被漏读或重读。

    这个坑 ``app/session_plane.py`` 已经踩过（``ts > cursor_ts`` 会把同毫秒的
    事件一起漏掉或一起重复）。这里把"两条事件时间戳完全相同"钉成用例。
    """
    log = _log(tmp_path)
    same = "2026-01-05T08:00:00+00:00"
    log.append("s", {"n": 1}, ts=same)
    log.append("s", {"n": 2}, ts=same)

    first = log.since("s", 0)
    assert [item["seq"] for item in first] == [1, 2]

    # 用第 1 条的时间戳当游标（时间戳增量读的错误做法）：两条都被漏掉。
    assert log.since("s", 0, limit=1)[0]["seq"] == 1
    # 用 seq 当游标：只拿新的那一条，恰好一条。
    rest = log.since("s", 1)
    assert [item["seq"] for item in rest] == [2]
    assert log.since("s", 2) == []


def test_event_id_is_content_addressed(tmp_path: Path) -> None:
    """``event_id = sha256(stream|seq|payload)[:16]``，落盘后不再变。"""
    log = _log(tmp_path)
    log.append("s", {"kind": "x", "n": 1})
    record = log.read_all("s")[0]
    assert record["event_id"] == event_id_for("s", 1, {"kind": "x", "n": 1})
    assert len(record["event_id"]) == EVENT_ID_CHARS
    # 同一个 payload 换一个流 → 不同 id（否则"这条属于哪个流"要靠外部上下文补）。
    assert event_id_for("s", 1, {"n": 1}) != event_id_for("t", 1, {"n": 1})


# ── 幂等 ────────────────────────────────────────────────────────────────────


def test_duplicate_append_is_idempotent(tmp_path: Path) -> None:
    """同一条事件重复 append → **同一个 seq**，文件里只有一行。"""
    log = _log(tmp_path)
    first = log.append("s", {"kind": "heartbeat", "n": 1})
    again = log.append("s", {"kind": "heartbeat", "n": 1})
    assert first == again == 1
    assert len(log.read_all("s")) == 1

    # 内容不同就是另一件事：不能因为"看起来像"就被吃掉。
    assert log.append("s", {"kind": "heartbeat", "n": 2}) == 2
    assert len(log.read_all("s")) == 2


def test_explicit_key_wins_over_content(tmp_path: Path) -> None:
    """显式 ``key=`` 是幂等键：内容不同但 key 相同 → 视为同一条（业务上唯一）。"""
    log = _log(tmp_path)
    assert log.append("s", {"n": 1}, key="job-1:started") == 1
    assert log.append("s", {"n": 999}, key="job-1:started") == 1
    assert len(log.read_all("s")) == 1
    assert log.append("s", {"n": 999}, key="job-1:finished") == 2


def test_fingerprint_ignores_seq_but_uses_stream(tmp_path: Path) -> None:
    """指纹不含 seq（seq 是分配出来的），但含流名——两个流不会互相判重。"""
    payload: dict[str, Any] = {"kind": "x"}
    assert fingerprint("a", payload) == fingerprint("a", dict(payload))
    assert fingerprint("a", payload) != fingerprint("b", payload)


# ── watcher ─────────────────────────────────────────────────────────────────


def test_watch_notifies_and_unsubscribes(tmp_path: Path) -> None:
    log = _log(tmp_path)
    seen: list[dict[str, Any]] = []
    stop = log.watch("s", seen.append)
    log.append("s", {"n": 1})
    assert [item["seq"] for item in seen] == [1]

    stop()
    log.append("s", {"n": 2})
    assert [item["seq"] for item in seen] == [1]  # 退订之后不再收到


def test_wildcard_watcher_sees_every_stream(tmp_path: Path) -> None:
    log = _log(tmp_path)
    seen: list[str] = []
    log.watch("*", lambda record: seen.append(str(record["stream"])))
    log.append("a", {"n": 1})
    log.append("b", {"n": 1})
    assert seen == ["a", "b"]


def test_watcher_exception_does_not_break_the_write(tmp_path: Path) -> None:
    """观察者炸了不该让"已经写成功的事件"变成失败。"""
    log = _log(tmp_path)

    def boom(record: dict[str, Any]) -> None:
        raise RuntimeError("观察者坏了")

    log.watch("s", boom)
    assert log.append("s", {"n": 1}) == 1
    assert len(log.read_all("s")) == 1


# ── compact ─────────────────────────────────────────────────────────────────


def test_compact_keeps_tail_and_records_dropped_range(tmp_path: Path) -> None:
    """只保留最后 N 条，并把**被丢掉的 seq 区间**写进压缩事件（审计要查得到）。"""
    log = _log(tmp_path)
    for index in range(1, 6):
        log.append("s", {"n": index})

    report = log.compact("s", 2)
    assert report["dropped"] == 3
    assert report["dropped_from"] == 1
    assert report["dropped_to"] == 3
    assert report["kept"] == 2
    assert report["compaction_seq"] == 6  # 压缩事件自己占一个新 seq

    records = log.read_all("s")
    assert [item["seq"] for item in records] == [6, 4, 5]
    marker = records[0]["payload"]
    assert marker["kind"] == KIND_COMPACTED
    assert (marker["dropped_from"], marker["dropped_to"], marker["dropped"]) == (1, 3, 3)

    # 压缩之后 seq 继续往上走，不会倒退（否则下游游标会重复消费）。
    assert log.append("s", {"n": 6}) == 7
    assert log.last_seq("s") == 7


def test_compact_is_noop_when_nothing_to_drop(tmp_path: Path) -> None:
    """没裁过就不该在审计里留下"这里被裁过"的假痕迹。"""
    log = _log(tmp_path)
    log.append("s", {"n": 1})
    report = log.compact("s", 5)
    assert report["dropped"] == 0
    assert report["compaction_seq"] == 0
    assert [item["seq"] for item in log.read_all("s")] == [1]


def test_compact_zero_keeps_only_the_marker(tmp_path: Path) -> None:
    log = _log(tmp_path)
    log.append("s", {"n": 1})
    log.append("s", {"n": 2})
    report = log.compact("s", 0)
    assert report["dropped"] == 2
    assert [item["payload"]["kind"] for item in log.read_all("s")] == [KIND_COMPACTED]


# ── 防御 ────────────────────────────────────────────────────────────────────


def test_bad_stream_names_are_rejected(tmp_path: Path) -> None:
    """流名会变成文件名：路径分隔符必须挡掉，否则就是一个目录穿越口子。"""
    log = _log(tmp_path)
    for bad in ("", "  ", "../evil", "a/b", "a\\b", ".."):
        with pytest.raises(EventLogError):
            log.append(bad, {"n": 1})


def test_empty_event_is_rejected(tmp_path: Path) -> None:
    log = _log(tmp_path)
    with pytest.raises(EventLogError):
        log.append("s", {})


def test_broken_lines_are_skipped(tmp_path: Path) -> None:
    """一行写坏不该让整条流读不出来（与 ``plane.jsonl`` 同一个容错口径）。"""
    log = _log(tmp_path)
    log.append("s", {"n": 1})
    path = log.path_for("s")
    with path.open("a", encoding="utf-8") as handle:
        handle.write("{ 这不是 json\n")
    log.append("s", {"n": 2})
    assert [item["payload"]["n"] for item in log.read_all("s")] == [1, 2]


def test_records_are_jsonl_on_disk(tmp_path: Path) -> None:
    """落盘就是一行一条 JSON：可以 diff、可以 grep、可以直接备份。"""
    log = _log(tmp_path)
    log.append("s", {"kind": "x"})
    lines = log.path_for("s").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["stream"] == "s" and record["seq"] == 1 and record["payload"] == {"kind": "x"}
