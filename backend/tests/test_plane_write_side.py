"""写侧统一（10.1）+ 压缩阈值（10.4）+ CJK 检索收尾（9.6）的契约测试。

钉住四件事，每件都对应一个"看起来能用、实际会坏"的失败模式：

1. **``task_events`` 与平面双向对齐**：``PlaneSink.record_task_event`` 一次写两侧、
   同一个 ``message_key`` 幂等；``backfill_from_store`` 可重复跑而不重复写。
   失败模式是"跑两遍补出一倍的事件"——平面日志翻倍之后时间线就没法读了。
2. **压缩阈值是 0.50 / 0.20**：边界（正好 50%）必须触发，且与 ``nodes.py`` 的硬编码
   目标比对得上。失败模式是"两边各写一套数字，谁也不知道哪个生效"。
3. **CJK 检索优先走 FTS**：建了索引就走索引，索引不在/过期就退回 LIKE，并且
   **说清楚这次走的是哪条路**。失败模式是"以为在用索引，其实每次全表扫"。
4. **CLI 每条新命令真的能跑**：``plane backfill`` / ``plane compact-check``
   （影子仓库那五条在 ``tests/test_shadow_checkpoint.py``）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from sqlalchemy import select

import app.cli.commands.session_extra2  # noqa: F401 —— 导入即接线
import app.session_search as session_search_module
from app.cli.main import build_parser, main
from app.config import Settings
from app.main import migrate
from app.session_index import SessionIndex
from app.session_plane import (
    COMPACTION_PREFIXES,
    COMPACTION_TARGET_RATIO,
    COMPACTION_THRESHOLD,
    KIND_TASK_EVENT,
    NODES_COMPACT_TARGET_RATIO,
    PlaneSink,
    SessionPlane,
    SessionPlaneError,
    backfill_from_store,
    compaction_needed,
    compaction_target,
    describe,
    maybe_compact,
    task_event_key,
    tokens_for_chars,
)
from app.session_search import ROUTE_FTS, ROUTE_LIKE, SessionSearch
from app.storage import Conversation, Message, Store, Task, TaskEvent

BASE = "2026-01-01T00:00:{:02d}+00:00"


# ── 夹具 ────────────────────────────────────────────────────────────────────


def _settings(tmp_path) -> Settings:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    return settings


async def _seeded(tmp_path) -> tuple[Settings, Store]:
    """一场会话 + 一个任务：``task_events`` 与平面都挂在它下面。"""
    settings = _settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    await store.save(Conversation(id="c1", title="复盘", created_at=BASE.format(0)))
    await store.save(Task(id="t1", conversation_id="c1", goal="复盘上周的决定"))
    return settings, store


# ── PlaneSink：两侧都写、同一个 key 幂等 ────────────────────────────────────


async def test_record_task_event_writes_both_sides(tmp_path):
    settings, store = await _seeded(tmp_path)
    plane = SessionPlane(settings.data_dir)
    sink = PlaneSink(plane, store)

    outcome = await sink.record_task_event("t1", "node_entered", node="prepare")
    assert outcome.written_plane and outcome.written_store
    assert outcome.store_id > 0, "task_events 必须真的多一行（否则读侧仍看不到）"
    assert outcome.session_id == "c1", "session_id 要按 tasks.conversation_id 解析"

    rows = await store.rows(select(TaskEvent))
    assert len(rows) == 1
    events = plane.events("c1")
    assert len(events) == 1
    assert events[0]["kind"] == KIND_TASK_EVENT
    assert events[0]["event_type"] == "node_entered"
    assert events[0]["task_event_id"] == outcome.store_id, "平面要能回跳到会话库那一行"
    await store.engine.dispose()


async def test_record_task_event_is_idempotent_for_the_same_content(tmp_path):
    """同一任务、同一类型、同一 payload 重复记：两侧都只有一行。"""
    settings, store = await _seeded(tmp_path)
    plane = SessionPlane(settings.data_dir)
    sink = PlaneSink(plane, store)

    first = await sink.record_task_event("t1", "node_entered", node="prepare")
    second = await sink.record_task_event("t1", "node_entered", node="prepare")
    assert second.written_plane is False and second.written_store is False
    assert second.plane_id == first.plane_id, "幂等命中要返回当初那一行"
    assert second.message_key == first.message_key

    rows = await store.rows(select(TaskEvent))
    assert len(rows) == 1, "会话库不能多出一行"
    assert len(plane.events("c1")) == 1, "平面不能多出一行"
    await store.engine.dispose()


async def test_record_task_event_distinguishes_different_payloads(tmp_path):
    settings, store = await _seeded(tmp_path)
    plane = SessionPlane(settings.data_dir)
    sink = PlaneSink(plane, store)

    await sink.record_task_event("t1", "node_entered", node="prepare")
    await sink.record_task_event("t1", "node_entered", node="think")
    assert len(plane.events("c1")) == 2
    await store.engine.dispose()


async def test_explicit_message_key_wins_over_content(tmp_path):
    """内容相同的两次真实发生，用显式 ``message_key`` 就能各留一行。"""
    settings, store = await _seeded(tmp_path)
    plane = SessionPlane(settings.data_dir)
    sink = PlaneSink(plane, store)

    await sink.record_task_event("t1", "ping", message_key="ping:1")
    await sink.record_task_event("t1", "ping", message_key="ping:2")
    assert len(plane.events("c1")) == 2
    await store.engine.dispose()


async def test_task_event_key_is_stable_and_content_addressed():
    left = task_event_key("t1", "node_entered", {"node": "prepare", "step": 1})
    right = task_event_key("t1", "node_entered", {"step": 1, "node": "prepare"})
    assert left == right, "键必须对字段顺序不敏感（否则 backfill 与实时写入永远对不上）"
    assert left.startswith("task:t1:node_entered:")
    assert task_event_key("t2", "node_entered", {"node": "prepare"}) != left


# ── backfill：可重复跑、不重复写 ────────────────────────────────────────────


async def test_backfill_is_repeatable(tmp_path):
    settings, store = await _seeded(tmp_path)
    for index in range(3):
        await store.event("t1", "node_entered", node=f"step{index}")
    plane = SessionPlane(settings.data_dir)

    first = await backfill_from_store(store, plane=plane)
    assert (first.scanned, first.written, first.skipped) == (3, 3, 0)
    assert len(plane.events("c1")) == 3

    second = await backfill_from_store(store, plane=plane)
    assert (second.scanned, second.written, second.skipped) == (3, 0, 3)
    assert len(plane.events("c1")) == 3, "重复跑不许把平面撑成两倍"
    assert second.cursor == first.cursor
    await store.engine.dispose()


async def test_backfill_keeps_the_original_timestamp(tmp_path):
    """补进去的事件必须落在它真正发生的位置，而不是"现在"。"""
    settings, store = await _seeded(tmp_path)
    await store.event("t1", "node_entered", node="prepare")
    plane = SessionPlane(settings.data_dir)
    await backfill_from_store(store, plane=plane)

    row = (await store.rows(select(TaskEvent)))[0]
    event = plane.events("c1")[0]
    assert event["ts"] == row.created_at
    await store.engine.dispose()


async def test_backfill_maps_task_events_to_the_conversation(tmp_path):
    """``task_events`` 只挂 task_id，补进平面时必须换成会话 id（否则补不进时间线）。"""
    settings, store = await _seeded(tmp_path)
    await store.event("t1", "node_entered", node="prepare")
    plane = SessionPlane(settings.data_dir)
    await backfill_from_store(store, plane=plane)

    assert plane.events("c1"), "按会话查不到 = 白补"
    assert plane.events("t1") == []
    await store.engine.dispose()


async def test_backfill_limit_only_limits_the_batch(tmp_path):
    settings, store = await _seeded(tmp_path)
    for index in range(5):
        await store.event("t1", "node_entered", node=f"step{index}")
    plane = SessionPlane(settings.data_dir)

    report = await backfill_from_store(store, plane=plane, limit=2)
    assert report.scanned == 2 and report.written == 2
    assert len(plane.events("c1")) == 2
    await store.engine.dispose()


async def test_timeline_does_not_double_count_backfilled_events(tmp_path):
    """回填之后同一条事实会以"平面镜像 + task_event 原行"两种身份出现。

    时间线是给人看的视图，一条事实只该出现一行；而且留下的必须是**原行**
    （``ref`` 能回跳 ``task_events.id``），不是镜像。
    """
    settings, store = await _seeded(tmp_path)
    await store.event("t1", "node_entered", node="prepare")
    plane = SessionPlane(settings.data_dir)
    await backfill_from_store(store, plane=plane)

    entries = await plane.timeline("c1", store=store)
    kinds = [entry.kind for entry in entries]
    assert kinds.count("node_entered") == 1, f"同一步出现了两遍：{kinds}"
    assert [entry.source for entry in entries] == ["task_event"]

    row = (await store.rows(select(TaskEvent)))[0]
    assert entries[0].ref == str(row.id), "留下的必须是原行（ref 可回跳），不是平面镜像"
    await store.engine.dispose()


# ── attach：把 store.event 挂成"顺手写平面" ─────────────────────────────────


async def test_attach_makes_store_event_flow_into_the_plane(tmp_path):
    """**10.1 缺的那一行**：挂上之后 ``store.event`` 自己就进平面了。"""
    settings, store = await _seeded(tmp_path)
    plane = SessionPlane(settings.data_dir)
    sink = PlaneSink(plane, store)

    detach = sink.attach()
    await store.event("t1", "node_entered", node="prepare")
    await store.event("t1", "node_entered", node="think")
    assert len(plane.events("c1")) == 2
    assert sink.errors == []

    detach()
    await store.event("t1", "node_entered", node="gate")
    assert len(plane.events("c1")) == 2, "卸载之后不该继续写平面"
    await store.engine.dispose()


async def test_attach_refuses_to_hook_twice(tmp_path):
    settings, store = await _seeded(tmp_path)
    sink = PlaneSink(SessionPlane(settings.data_dir), store)
    sink.attach()
    with pytest.raises(SessionPlaneError):
        sink.attach()
    await store.engine.dispose()


# ── 压缩阈值（10.4）────────────────────────────────────────────────────────


def test_thresholds_are_the_hermes_numbers():
    assert COMPACTION_THRESHOLD == 0.50
    assert COMPACTION_TARGET_RATIO == 0.20


def test_compaction_needed_at_the_boundary():
    """**正好 50% 就压**（``>=``）：到一半已经是"再塞一个结果就可能溢出"。"""
    assert compaction_needed(500, 1000) is True
    assert compaction_needed(499, 1000) is False
    assert compaction_needed(501, 1000) is True
    assert compaction_needed(1, 2) is True


def test_compaction_needed_refuses_to_guess_without_a_window():
    """窗口未知（0 / 负数 / 坏值）不触发：凭一个假的 100% 去压是"惩罚没配置"。"""
    assert compaction_needed(10_000, 0) is False
    assert compaction_needed(10_000, -5) is False
    assert compaction_needed(0, 1000) is False
    assert compaction_needed("坏值", 1000) is False


def test_compaction_target_is_20_percent():
    assert compaction_target(1000) == 200
    assert compaction_target(128_000) == 25_600
    assert compaction_target(0) == 0


def test_compaction_prefixes_match_hermes_and_session_search():
    """前缀是**跨模块契约**：``session_search`` 靠它剔除压缩载荷。"""
    assert COMPACTION_PREFIXES == ("[CONTEXT COMPACTION", "[CONTEXT SUMMARY]:")
    assert session_search_module._COMPACTION_PREFIXES == COMPACTION_PREFIXES


def test_maybe_compact_reports_the_numbers():
    plan = maybe_compact(600, 1000)
    assert plan.needed is True
    assert plan.used_ratio == pytest.approx(0.6)
    assert plan.target_tokens == 200
    assert plan.headroom_tokens == 400
    assert "50%" in plan.reason and "20%" in plan.reason

    idle = maybe_compact(100, 1000)
    assert idle.needed is False
    assert idle.reason.startswith("已用 100 token")


def _nodes_source() -> str:
    return (Path(__file__).resolve().parents[1] / "app" / "graph" / "nodes.py").read_text(
        encoding="utf-8"
    )


def test_describe_matches_nodes_py_target_ratio():
    """``nodes.py`` 是禁改区，不能 import（会拉起 langgraph），所以**解析源码**核对。

    这条用例是"复写会漂移"的解药：``session_plane`` 里的
    ``NODES_COMPACT_TARGET_RATIO`` 一旦与那边不一致，这里当场失败。
    """
    match = re.search(r"^COMPACT_TARGET_RATIO\s*=\s*([0-9.]+)", _nodes_source(), re.MULTILINE)
    assert match is not None, "nodes.py 里的 COMPACT_TARGET_RATIO 没了？口径需要重新对齐"
    assert float(match.group(1)) == NODES_COMPACT_TARGET_RATIO == COMPACTION_TARGET_RATIO


def test_describe_reports_the_trigger_gap():
    """如实报差异：目标比一致，但那边**没有** 0.50 的提前判定。"""
    policy = describe(128_000)
    assert policy["threshold"] == COMPACTION_THRESHOLD
    assert policy["target_tokens"] == compaction_target(128_000)
    assert policy["nodes_py"]["target_matches"] is True
    assert policy["nodes_py"]["trigger_matches"] is False
    assert "context_overflow" in policy["nodes_py"]["trigger"]
    # 那边的压缩路径确实存在（只是被动）：源码里得有它
    assert "def compact_messages" in _nodes_source()


def test_tokens_for_chars_uses_the_four_chars_per_token_rule():
    assert tokens_for_chars(4000) == 1000
    assert tokens_for_chars(0) == 0
    assert tokens_for_chars("坏值") == 0


# ── 9.6：CJK 检索优先走 FTS ────────────────────────────────────────────────


async def _cjk_seeded(tmp_path) -> tuple[Settings, Store]:
    settings, store = await _seeded(tmp_path)
    for ident, role, content in (
        ("m1", "user", "上周那个决定是怎么定的？我想复盘一下"),
        ("m2", "assistant", "键盘布局偏好是 Dvorak"),
        ("m3", "user", "报告还没写"),
    ):
        await store.save(Message(id=ident, conversation_id="c1", role=role, content=content))
    return settings, store


async def test_chinese_query_goes_through_fts_when_the_index_exists(tmp_path):
    """建了索引就走索引：2 字中文词（``键盘``）靠 unicode61 的逐字路径命中。"""
    settings, store = await _cjk_seeded(tmp_path)
    index = SessionIndex(settings.data_dir)
    await index.index_messages(store)

    searcher = SessionSearch(store)
    anchors = await searcher.discover("键盘")
    assert [anchor.message_id for anchor in anchors] == ["m2"]
    assert "键盘" in anchors[0].snippet
    assert searcher.last_route is not None
    assert searcher.last_route.backend == ROUTE_FTS
    assert "trigram" in searcher.last_route.reason or "unicode61" in searcher.last_route.reason
    assert searcher.last_route.plan == "cjk-short"
    await store.engine.dispose()


async def test_falls_back_to_like_and_says_so_when_there_is_no_index(tmp_path):
    """没有 ``fts.sqlite`` → 退回 LIKE，**并且说明为什么退**。"""
    settings, store = await _cjk_seeded(tmp_path)
    searcher = SessionSearch(store)
    anchors = await searcher.discover("键盘")

    assert [anchor.message_id for anchor in anchors] == ["m2"]
    assert searcher.last_route is not None
    assert searcher.last_route.backend == ROUTE_LIKE
    assert "索引库不存在" in searcher.last_route.reason
    assert "sessions reindex" in searcher.last_route.reason
    assert not (settings.data_dir / "fts.sqlite").exists(), "不该顺手建一个空索引库"
    await store.engine.dispose()


async def test_stale_index_does_not_hide_a_message(tmp_path):
    """索引过期时 FTS 0 命中，必须退回 LIKE——静默丢结果比慢几百毫秒严重得多。"""
    settings, store = await _cjk_seeded(tmp_path)
    index = SessionIndex(settings.data_dir)
    await index.index_messages(store)
    # 索引建好之后才写进来的消息（真实场景：还没跑 reindex）
    await store.save(
        Message(
            id="m4",
            conversation_id="c1",
            role="user",
            content="新加的一条：显示器亮度调到 80",
            created_at=BASE.format(50),
        )
    )

    searcher = SessionSearch(store)
    anchors = await searcher.discover("亮度")
    assert [anchor.message_id for anchor in anchors] == ["m4"]
    assert searcher.last_route is not None
    assert searcher.last_route.backend == ROUTE_LIKE
    assert "0 命中" in searcher.last_route.reason
    await store.engine.dispose()


async def test_search_payload_carries_the_route(tmp_path):
    settings, store = await _cjk_seeded(tmp_path)
    payload = await SessionSearch(store).search("discover", query="键盘")
    assert payload["count"] == 1
    assert payload["route"]["backend"] == ROUTE_LIKE
    assert payload["route"]["reason"]
    await store.engine.dispose()


# ── CLI：plane backfill / plane compact-check ───────────────────────────────


def _argv(settings: Settings, *rest: str) -> list[str]:
    return ["--data-dir", str(settings.data_dir), *rest]


def test_new_command_help_renders():
    """每条新命令（含子命令）的 ``--help`` 都必须能渲染。

    比"命令登记了"更强的一层保证：登记了但 ``--help`` 崩掉，等于没有。
    **实测踩过**：选项说明里写了 ``目标 20%）``，argparse 会拿它做
    ``help % params``，于是 ``ValueError: unsupported format character``——整棵树
    在渲染帮助时炸掉。写成 ``20%%`` 才对，这条用例把它钉住。
    """
    parser = build_parser()
    for argv in (
        ("plane", "backfill"),
        ("plane", "compact-check"),
        ("sessions", "shadow"),
        ("sessions", "shadow", "init"),
        ("sessions", "shadow", "snapshot"),
        ("sessions", "shadow", "restore"),
        ("sessions", "shadow", "log"),
        ("sessions", "shadow", "diff"),
    ):
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args([*argv, "--help"])
        assert excinfo.value.code == 0, argv


async def test_cli_plane_backfill(tmp_path, capsys):
    settings, store = await _seeded(tmp_path)
    await store.event("t1", "node_entered", node="prepare")
    await store.engine.dispose()

    assert main(_argv(settings, "plane", "backfill")) == 0
    assert "新写 1 条" in capsys.readouterr().out

    # 再跑一次：一条都不新增（幂等）
    assert main(_argv(settings, "--json", "plane", "backfill")) == 0
    assert '"written": 0' in capsys.readouterr().out
    assert len(SessionPlane(settings.data_dir).events("c1")) == 1


def test_cli_plane_compact_check(tmp_path, capsys):
    settings = _settings(tmp_path)
    code = main(
        _argv(settings, "plane", "compact-check", "--window", "1000", "--used-tokens", "600")
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "该压缩" in out
    assert "50%" in out and "20%" in out
    assert "nodes.py" in out

    code = main(
        _argv(
            settings, "--json", "plane", "compact-check", "--window", "1000", "--used-tokens", "100"
        )
    )
    assert code == 0
    assert '"needed": false' in capsys.readouterr().out


async def test_cli_plane_compact_check_measures_a_real_session(tmp_path, capsys):
    settings, store = await _cjk_seeded(tmp_path)
    await store.engine.dispose()

    assert main(_argv(settings, "plane", "compact-check", "--session", "c1")) == 0
    out = capsys.readouterr().out
    assert "会话 c1" in out and "3 条消息" in out
    assert "不需要压缩" in out
