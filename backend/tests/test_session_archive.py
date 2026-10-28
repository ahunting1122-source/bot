"""归档不删 + 提示词内容寻址（``app/session_archive.py``）的契约测试。

钉住四件事，每件都对应一个"看起来能用、实际会坏"的失败模式：

1. **归档不删消息**：归档前后 ``messages`` 行数必须完全一致。这条用例存在的理由很直白
   ——"归档"这个词在别的系统里就是"删掉、但先打个标记"，实现时顺手 ``delete()`` 一下
   也不会有人当场发现，直到有人问"上个月那个决定是怎么定的"。
2. **unarchive 真的复原**：平面日志是只追加的，"当前是否归档"由最后一条事件推导；
   少写一个分支就会出现"取消归档了但列表里还在"。
3. **内容寻址只存一份**：同一份系统提示被两个任务复用时，盘上只能有一个文件。
4. **hash 不符必须报错**：文件名就是内容摘要，对不上说明文件被改过——这时返回内容
   等于把"当时模型看到的提示"变成一句谎话。
"""

from __future__ import annotations

import hashlib

import pytest

import app.cli.commands.session_extra  # noqa: F401 —— 导入即接线（sessions archive/unarchive/archived）
from app.cli.main import main
from app.config import Settings
from app.main import migrate
from app.session_archive import (
    ArchiveError,
    PromptMismatch,
    PromptStore,
    SessionArchive,
)
from app.session_plane import KIND_ARCHIVED, KIND_PROMPT_FROZEN, KIND_UNARCHIVED
from app.storage import Conversation, Message, Store


def _settings(tmp_path) -> Settings:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    return settings


async def _seed(store: Store, *, messages: int = 3) -> None:
    await store.save(Conversation(id="c1", title="复盘上周的决定"))
    await store.save(Conversation(id="c2", title="另一个会话"))
    for index in range(messages):
        await store.save(
            Message(
                id=f"m{index}",
                conversation_id="c1",
                role="user" if index % 2 == 0 else "assistant",
                content=f"第 {index} 条消息",
            )
        )


async def _message_count(store: Store) -> int:
    from sqlalchemy import func, select

    async with store.sessions() as session:
        return int(await session.scalar(select(func.count(Message.id))) or 0)


# ── 归档不删 ────────────────────────────────────────────────────────────────


async def test_archive_keeps_every_message(tmp_path):
    """归档后 ``messages`` 一条不少——这是本模块最硬的一条约束。"""
    settings = _settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    await _seed(store)
    before = await _message_count(store)
    assert before == 3

    archive = SessionArchive(settings.data_dir)
    event = archive.archive("c1", reason="测试", messages=before)

    after = await _message_count(store)
    await store.engine.dispose()
    assert after == before, "归档不许删消息"
    assert event["kind"] == KIND_ARCHIVED
    assert archive.is_archived("c1")
    assert not archive.is_archived("c2")


def test_archive_is_idempotent(tmp_path):
    """重复归档不再追加事件（否则"最近一次归档是什么时候"会被同义事件埋掉）。"""
    archive = SessionArchive(tmp_path / "data")
    first = archive.archive("c1", reason="a")
    second = archive.archive("c1", reason="b")
    assert second["id"] == first["id"]
    assert len(archive.plane.events("c1")) == 1


def test_unarchive_restores_the_session(tmp_path):
    archive = SessionArchive(tmp_path / "data")
    archive.archive("c1")
    archive.unarchive("c1")
    assert not archive.is_archived("c1")
    assert archive.list_archived() == []
    kinds = [item["kind"] for item in archive.plane.events("c1")]
    assert kinds == [KIND_ARCHIVED, KIND_UNARCHIVED]


def test_unarchive_a_session_that_is_not_archived_raises(tmp_path):
    archive = SessionArchive(tmp_path / "data")
    with pytest.raises(ArchiveError):
        archive.unarchive("c1")


def test_archive_twice_after_unarchive_takes_the_last_event(tmp_path):
    """归档 → 取消 → 再归档：状态由**最后一条**事件决定，不是"曾经归档过"。"""
    archive = SessionArchive(tmp_path / "data")
    archive.archive("c1", ts="2026-01-01T00:00:00+00:00")
    archive.unarchive("c1")
    assert not archive.is_archived("c1")
    archive.archive("c1", ts="2026-01-02T00:00:00+00:00")
    entry = archive.entry("c1")
    assert entry is not None
    assert entry.archived_at == "2026-01-02T00:00:00+00:00"


def test_list_archived_is_newest_first(tmp_path):
    archive = SessionArchive(tmp_path / "data")
    archive.archive("c1", ts="2026-01-01T00:00:00+00:00")
    archive.archive("c2", ts="2026-01-03T00:00:00+00:00")
    archive.archive("c3", ts="2026-01-02T00:00:00+00:00")
    archive.unarchive("c1")
    listed = [item.session_id for item in archive.list_archived()]
    assert listed == ["c2", "c3"]
    assert [item.session_id for item in archive.list_archived(limit=1)] == ["c2"]


def test_archive_requires_a_session_id(tmp_path):
    with pytest.raises(ArchiveError):
        SessionArchive(tmp_path / "data").archive("  ")


# ── 提示词内容寻址 ──────────────────────────────────────────────────────────


def test_same_prompt_is_stored_once(tmp_path):
    """同一份提示被两个任务复用 → 盘上只有一个文件，两个任务都能取回。"""
    store = PromptStore(tmp_path / "data")
    first = store.bind("t1", "你是一个很有耐心的助手。")
    second = store.bind("t2", "你是一个很有耐心的助手。")

    assert first == second
    assert len(store.files()) == 1
    assert store.files()[0].name == f"{first}.txt"
    assert store.prompt_for("t1") == "你是一个很有耐心的助手。"
    assert store.prompt_for("t2") == store.prompt_for("t1")


def test_different_prompts_get_different_files(tmp_path):
    store = PromptStore(tmp_path / "data")
    first = store.bind("t1", "提示 A")
    second = store.bind("t2", "提示 B")
    assert first != second
    assert len(store.files()) == 2


def test_hash_is_sha256_of_the_utf8_bytes(tmp_path):
    store = PromptStore(tmp_path / "data")
    digest = store.bind("t1", "中文提示")
    assert digest == hashlib.sha256("中文提示".encode()).hexdigest()
    assert store.hash_for("t1") == digest


def test_prompt_for_unknown_task_raises(tmp_path):
    store = PromptStore(tmp_path / "data")
    with pytest.raises(ArchiveError):
        store.prompt_for("nope")


def test_tampered_file_raises_instead_of_returning_content(tmp_path):
    """文件被改过时**必须报错**：返回内容等于伪造"当时模型看到的提示"。"""
    store = PromptStore(tmp_path / "data")
    digest = store.bind("t1", "原始提示")
    path = store.path_for(digest)
    path.write_text("被手改过的提示", encoding="utf-8")

    with pytest.raises(PromptMismatch) as excinfo:
        store.prompt_for("t1")
    assert "摘要对不上" in str(excinfo.value)


def test_put_detects_a_corrupted_file_with_the_same_name(tmp_path):
    """同名文件内容不符时 ``put`` 也不能装作存过了。"""
    store = PromptStore(tmp_path / "data")
    digest = store.bind("t1", "原始提示")
    store.path_for(digest).write_text("别的正文", encoding="utf-8")
    with pytest.raises(PromptMismatch):
        store.put("原始提示")


def test_bind_hash_reuses_an_existing_prompt(tmp_path):
    """``bind_hash``：正文已由别的任务存过，只挂 hash（从 Hermes 导入时走这条）。"""
    store = PromptStore(tmp_path / "data")
    digest = store.bind("t1", "共享提示")
    store.bind_hash("t2", digest, model="m")
    assert store.hash_for("t2") == digest
    assert store.prompt_for("t2") == "共享提示"
    assert len(store.files()) == 1


def test_bind_hash_rejects_an_unknown_hash(tmp_path):
    store = PromptStore(tmp_path / "data")
    with pytest.raises(ArchiveError):
        store.bind_hash("t1", "0" * 64)


def test_path_for_rejects_a_malformed_hash(tmp_path):
    """hash 会被拼进文件路径，必须先校验格式（``../..`` 之类不能进路径）。"""
    store = PromptStore(tmp_path / "data")
    with pytest.raises(ArchiveError):
        store.path_for("../../etc/passwd")


def test_stats_report_what_was_saved(tmp_path):
    store = PromptStore(tmp_path / "data")
    store.bind("t1", "重复的提示")
    store.bind("t2", "重复的提示")
    store.bind("t3", "另一份")
    stats = store.stats()
    assert stats.files == 2
    assert stats.bindings == 3
    assert stats.saved_files == 1
    assert stats.saved_bytes > 0


def test_prompt_event_lands_on_the_plane(tmp_path):
    """``task_id → hash`` 记在平面日志里（不占禁改区的表结构）。"""
    store = PromptStore(tmp_path / "data")
    digest = store.bind("t1", "提示", session_id="c1")
    events = [item for item in store.plane.events("c1") if item["kind"] == KIND_PROMPT_FROZEN]
    assert len(events) == 1
    assert events[0]["task_id"] == "t1"
    assert events[0]["hash"] == digest


def test_rebinding_a_task_takes_the_last_hash(tmp_path):
    store = PromptStore(tmp_path / "data")
    store.bind("t1", "旧提示")
    fresh = store.bind("t1", "新提示")
    assert store.hash_for("t1") == fresh
    assert store.prompt_for("t1") == "新提示"


# ── CLI ─────────────────────────────────────────────────────────────────────


async def test_cli_archive_keeps_messages_and_unarchive_restores(tmp_path, capsys):
    settings = _settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    await _seed(store)
    before = await _message_count(store)
    await store.engine.dispose()
    data = str(settings.data_dir)

    assert main(["--data-dir", data, "sessions", "archive", "c1"]) == 0
    assert "已归档" in capsys.readouterr().out

    assert main(["--data-dir", data, "sessions", "archived"]) == 0
    listed = capsys.readouterr().out
    assert "c1" in listed
    assert "复盘上周的决定" in listed

    assert main(["--data-dir", data, "sessions", "unarchive", "c1"]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data, "sessions", "archived"]) == 0
    assert "已归档会话 0 个" in capsys.readouterr().out

    # 归档 → 取消归档走完一圈，消息数仍然是原来那个数。
    store = Store(settings.data_dir / "app.sqlite")
    assert await _message_count(store) == before
    await store.engine.dispose()


def test_cli_archive_requires_a_filter_when_no_id(tmp_path, capsys):
    """裸 ``sessions archive`` 报错而不是"归档全部"：手滑会把所有列表清空。"""
    settings = _settings(tmp_path)
    code = main(["--data-dir", str(settings.data_dir), "sessions", "archive"])
    assert code == 1
    assert "不归档全部" in capsys.readouterr().err


def test_cli_archive_rejects_unimplemented_filters(tmp_path, capsys):
    settings = _settings(tmp_path)
    code = main(["--data-dir", str(settings.data_dir), "sessions", "archive", "--model", "gpt"])
    assert code == 1
    assert "未实现" in capsys.readouterr().err


async def test_cli_archive_dry_run_touches_nothing(tmp_path, capsys):
    settings = _settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    await _seed(store)
    await store.engine.dispose()
    data = str(settings.data_dir)

    assert main(["--data-dir", data, "sessions", "archive", "--title", "复盘", "--dry-run"]) == 0
    assert "命中 1 个会话" in capsys.readouterr().out
    assert SessionArchive(settings.data_dir).list_archived() == []


async def test_cli_archive_unknown_session_is_a_clean_error(tmp_path, capsys):
    settings = _settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    await _seed(store)
    await store.engine.dispose()
    code = main(["--data-dir", str(settings.data_dir), "sessions", "archive", "no-such-id"])
    assert code == 1
    assert "会话不存在" in capsys.readouterr().err
