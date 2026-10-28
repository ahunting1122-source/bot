"""双 FTS5 索引（``app/session_index.py``）的契约测试。

钉住五件事，每件都对应一个"看起来能用、实际会坏"的失败模式：

1. **中文查询能命中**——这是本模块存在的全部理由。本机实测 ``unicode61`` 索引原始
   中文时 ``MATCH '决定'`` 返回 0 条（整段中文是一个 token），而 trigram 只索引
   ≥3 字符窗口，2 字词照样 0 条。所以两条路都要有，且都要被这条用例钉住。
2. **英文查询能命中**（含词内子串：``atewa`` 只在 trigram 上命中）。
3. **增量同步不重复**：跑两次同步，镜像与两张倒排表的行数都不许翻倍。
4. **FTS5 不可用时明确报错**：抛 ``FtsUnavailable`` 且消息里带降级路径
   （``orbit session-search discover``）。静默退回 LIKE 是最坏的：用户以为在用索引。
5. **重建能清掉残影**：消息被删过之后，增量同步只加不删，只能靠 ``rebuild``。
"""

from __future__ import annotations

import sqlite3

import pytest

import app.cli.commands.session_extra  # noqa: F401 —— 导入即接线（sessions reindex/search）
import app.session_index as index_module
from app.cli.main import main
from app.config import Settings
from app.main import migrate
from app.session_index import (
    DEGRADE_HINT,
    FTS_FILENAME,
    TABLE_TRIGRAM,
    TABLE_UNICODE,
    FtsUnavailable,
    SessionIndex,
)
from app.storage import Conversation, Message, Store

#: 造库用的四条消息：一条中文长句、一条英文、一条两字中文词、一条带 ASCII 子串的英文。
SEED = [
    ("m1", "user", "上周那个决定是怎么定的？我想复盘一下"),
    ("m2", "assistant", "部署网关时用指数退避重试，最多三次"),
    ("m3", "user", "键盘布局偏好是 Dvorak"),
    ("m4", "assistant", "the gateway retries with exponential backoff"),
]


def _settings(tmp_path) -> Settings:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    return settings


async def _seeded(tmp_path) -> tuple[Settings, Store]:
    settings = _settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    await store.save(Conversation(id="c1", title="复盘"))
    for ident, role, content in SEED:
        await store.save(Message(id=ident, conversation_id="c1", role=role, content=content))
    return settings, store


# ── 中文与英文命中 ──────────────────────────────────────────────────────────


async def test_chinese_query_hits(tmp_path):
    """**核心价值**：中文查询能命中。三种长度都要覆盖。"""
    settings, store = await _seeded(tmp_path)
    index = SessionIndex(settings.data_dir)
    report = await index.index_messages(store)
    await store.engine.dispose()
    assert report.added == 4

    # 3 字以上：走 trigram 的精确子串
    assert [hit.message_id for hit in index.search("怎么定")] == ["m1"]
    assert [hit.message_id for hit in index.search("布局偏好")] == ["m3"]
    assert [hit.message_id for hit in index.search("退避重试")] == ["m2"]
    # 2 字词：trigram 索引不了，靠 unicode61 的逐字路径兜
    assert [hit.message_id for hit in index.search("决定")] == ["m1"]
    assert [hit.message_id for hit in index.search("键盘")] == ["m3"]


async def test_chinese_hit_carries_a_readable_snippet(tmp_path):
    settings, store = await _seeded(tmp_path)
    index = SessionIndex(settings.data_dir)
    await index.index_messages(store)
    await store.engine.dispose()

    hit = index.search("怎么定")[0]
    assert "怎么定" in hit.snippet
    assert hit.conversation_id == "c1"
    assert hit.role == "user"
    assert hit.created_at, "回跳要按时间定位，不能是空串"


async def test_english_query_hits(tmp_path):
    settings, store = await _seeded(tmp_path)
    index = SessionIndex(settings.data_dir)
    await index.index_messages(store)
    await store.engine.dispose()

    hits = index.search("gateway")
    assert [hit.message_id for hit in hits] == ["m4"]
    assert hits[0].table == TABLE_UNICODE, "整词命中走 unicode61"


async def test_english_substring_hits_via_trigram(tmp_path):
    """``atewa`` 只在 trigram 上命中（本机实测 unicode61 对词内子串 0 命中）。"""
    settings, store = await _seeded(tmp_path)
    index = SessionIndex(settings.data_dir)
    await index.index_messages(store)
    await store.engine.dispose()

    hits = index.search("atewa")
    assert [hit.message_id for hit in hits] == ["m4"]
    assert hits[0].table == TABLE_TRIGRAM


async def test_multi_word_query_is_an_and(tmp_path):
    """多词 AND（与 ``session_search`` 的语义一致）：少一个词就 0 命中。"""
    settings, store = await _seeded(tmp_path)
    index = SessionIndex(settings.data_dir)
    await index.index_messages(store)
    await store.engine.dispose()

    assert index.search("退避 重试"), "两个词都在同一条里"
    assert index.search("退避 键盘") == [], "分属两条消息，AND 语义下不该命中"


async def test_search_does_not_cross_into_other_conversations(tmp_path):
    settings, store = await _seeded(tmp_path)
    await store.save(Conversation(id="c2", title="另一个"))
    await store.save(Message(id="m9", conversation_id="c2", role="user", content="无关内容"))
    index = SessionIndex(settings.data_dir)
    await index.index_messages(store)
    await store.engine.dispose()

    hits = index.search("无关")
    assert [hit.conversation_id for hit in hits] == ["c2"]


# ── 选表策略 ────────────────────────────────────────────────────────────────


def test_plan_prefers_trigram_for_cjk(tmp_path):
    index = SessionIndex(tmp_path / "data")
    plan = index.plan("怎么定")
    assert plan.mode == "cjk"
    assert plan.tables[0] == TABLE_TRIGRAM
    assert "trigram" in plan.reason


def test_plan_falls_back_to_the_char_path_for_short_cjk(tmp_path):
    """两字中文词 trigram 索引不了，必须换 unicode61 的逐字路径——静默 0 命中不可接受。"""
    index = SessionIndex(tmp_path / "data")
    plan = index.plan("决定")
    assert plan.mode == "cjk-short"
    assert plan.tables[0] == TABLE_UNICODE
    assert "决定" in plan.reason


def test_plan_uses_unicode61_for_ascii(tmp_path):
    index = SessionIndex(tmp_path / "data")
    plan = index.plan("gateway")
    assert plan.mode == "ascii"
    assert plan.tables[0] == TABLE_UNICODE


def test_empty_query_is_rejected(tmp_path):
    from app.session_index import SessionIndexError

    index = SessionIndex(tmp_path / "data")
    with pytest.raises(SessionIndexError):
        index.plan("   ")


# ── 增量同步 ────────────────────────────────────────────────────────────────


async def test_second_sync_adds_nothing(tmp_path):
    """增量同步不重复：水位是 ``messages.rowid``，跑两次行数不变。"""
    settings, store = await _seeded(tmp_path)
    index = SessionIndex(settings.data_dir)

    first = await index.index_messages(store)
    second = await index.index_messages(store)
    stats = index.stats()

    assert first.added == 4
    assert second.added == 0
    assert second.watermark == first.watermark
    assert stats["rows"] == 4
    assert stats["unicode_rows"] == 4
    assert stats["trigram_rows"] == 4
    assert stats["path"].endswith(FTS_FILENAME)
    await store.engine.dispose()


async def test_new_messages_are_picked_up_incrementally(tmp_path):
    settings, store = await _seeded(tmp_path)
    index = SessionIndex(settings.data_dir)
    await index.index_messages(store)

    await store.save(
        Message(id="m5", conversation_id="c1", role="user", content="后来追加的一句话")
    )
    report = await index.index_messages(store)
    await store.engine.dispose()

    assert report.added == 1
    assert [hit.message_id for hit in index.search("后来追加")] == ["m5"]


async def test_since_id_cannot_skip_rows(tmp_path):
    """``since_id`` 给得比水位大也不会漏——水位才是权威。"""
    settings, store = await _seeded(tmp_path)
    index = SessionIndex(settings.data_dir)
    await index.index_messages(store)
    report = await index.index_messages(store, since_id=9_999)
    await store.engine.dispose()
    assert report.added == 0
    assert report.scanned == 0


async def test_rebuild_clears_the_index(tmp_path):
    """重建之后索引仍在、行数不翻倍（残影清理与幂等性同时验证）。"""
    settings, store = await _seeded(tmp_path)
    index = SessionIndex(settings.data_dir)
    await index.index_messages(store)

    report = await index.rebuild(store)
    stats = index.stats()
    await store.engine.dispose()

    assert report.added == 4
    assert stats["rows"] == 4
    assert stats["unicode_rows"] == 4
    assert stats["trigram_rows"] == 4


async def test_index_is_a_separate_file_from_app_sqlite(tmp_path):
    """**不许动 app.sqlite 的 schema**：消息的 FTS 索引只在独立文件里。

    注意 ``app.sqlite`` 里本来就有 ``chunks_fts_*``（那是 RAG 写入侧的 chunks 索引，
    由 alembic 迁移建的，属禁改区）。所以这里查的是**消息**索引有没有偷偷跑进主库。
    """
    settings, store = await _seeded(tmp_path)
    index = SessionIndex(settings.data_dir)
    await index.index_messages(store)
    await store.engine.dispose()

    assert index.path.exists()
    with sqlite3.connect(settings.data_dir / "app.sqlite") as conn:
        names = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','trigger')")
        }
    offenders = {name for name in names if name.startswith("messages_fts") or name == "messages_index"}
    assert offenders == set(), f"app.sqlite 里出现了消息 FTS 对象：{offenders}"


# ── 降级 ────────────────────────────────────────────────────────────────────


def test_fts_unavailable_is_explicit(tmp_path, monkeypatch):
    """FTS5 不可用时抛 ``FtsUnavailable``，且消息里给出退回 LIKE 的做法。"""

    def boom(conn):
        raise sqlite3.OperationalError("no such module: fts5")

    monkeypatch.setattr(index_module, "_create_schema", boom)
    index = SessionIndex(tmp_path / "data")

    with pytest.raises(FtsUnavailable) as excinfo:
        index.search("决定")
    message = str(excinfo.value)
    assert "no such module: fts5" in message, "要把底层原因带上，否则无从排查"
    assert "session-search discover" in message
    assert DEGRADE_HINT.split("（")[0] in message
    assert index.available() is False


def test_missing_trigram_tokenizer_is_also_fts_unavailable(tmp_path, monkeypatch):
    """trigram 分词器缺失（旧 sqlite）走同一条降级路径，不是崩在别处。"""

    def boom(conn):
        raise sqlite3.OperationalError("no such tokenizer: trigram")

    monkeypatch.setattr(index_module, "_create_schema", boom)
    with pytest.raises(FtsUnavailable) as excinfo:
        SessionIndex(tmp_path / "data").connect()
    assert "trigram" in str(excinfo.value)


def test_stats_on_a_missing_index_reports_zero(tmp_path):
    stats = SessionIndex(tmp_path / "data").stats()
    assert stats["rows"] == 0
    assert stats["watermark"] == 0
    assert stats["available"] is True


# ── CLI ─────────────────────────────────────────────────────────────────────


async def test_cli_reindex_then_search(tmp_path, capsys):
    settings, store = await _seeded(tmp_path)
    await store.engine.dispose()
    data = str(settings.data_dir)

    assert main(["--data-dir", data, "sessions", "reindex"]) == 0
    assert "新增 4 条" in capsys.readouterr().out

    assert main(["--data-dir", data, "sessions", "search", "怎么定"]) == 0
    out = capsys.readouterr().out
    assert "命中 1 条" in out
    assert "trigram" in out
    assert "上周那个决定是怎么定的" in out


async def test_cli_search_reports_the_chosen_tables(tmp_path, capsys):
    settings, store = await _seeded(tmp_path)
    await store.engine.dispose()
    data = str(settings.data_dir)
    assert main(["--data-dir", data, "sessions", "reindex"]) == 0
    capsys.readouterr()

    assert main(["--data-dir", data, "sessions", "search", "决定"]) == 0
    out = capsys.readouterr().out
    assert "cjk-short" in out, "两字词要说明为什么走了 unicode61"


async def test_cli_search_json_payload(tmp_path, capsys):
    import json

    settings, store = await _seeded(tmp_path)
    await store.engine.dispose()
    data = str(settings.data_dir)
    assert main(["--data-dir", data, "sessions", "reindex"]) == 0
    capsys.readouterr()

    assert main(["--data-dir", data, "--json", "sessions", "search", "gateway"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["count"] == 1
    assert payload["plan"]["mode"] == "ascii"
    assert payload["results"][0]["message_id"] == "m4"


async def test_cli_reindex_rebuild_flag(tmp_path, capsys):
    settings, store = await _seeded(tmp_path)
    await store.engine.dispose()
    data = str(settings.data_dir)
    assert main(["--data-dir", data, "sessions", "reindex"]) == 0
    capsys.readouterr()

    assert main(["--data-dir", data, "sessions", "reindex", "--rebuild"]) == 0
    assert "索引重建完成：新增 4 条" in capsys.readouterr().out


async def test_cli_search_surfaces_fts_unavailable(tmp_path, capsys, monkeypatch):
    settings, store = await _seeded(tmp_path)
    await store.engine.dispose()

    def boom(conn):
        raise sqlite3.OperationalError("no such module: fts5")

    monkeypatch.setattr(index_module, "_create_schema", boom)
    code = main(["--data-dir", str(settings.data_dir), "sessions", "search", "决定"])
    assert code == 1
    err = capsys.readouterr().err
    assert "FTS5 索引不可用" in err
    assert "session-search discover" in err
