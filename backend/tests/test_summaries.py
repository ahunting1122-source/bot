import asyncio
import json
import sqlite3

import pytest
from alembic import command
from alembic.config import Config
from langchain_core.messages import AIMessage

from app.config import ROOT, Settings
from app.storage import Conversation, Message, Store
from app.summaries import SummaryService, list_conversations


def migrate_database(path, revision="head"):
    config = Config(str(ROOT / "backend" / "alembic.ini"))
    config.attributes["database_url"] = f"sqlite:///{path}"
    command.upgrade(config, revision)


@pytest.fixture
async def store(tmp_path):
    path = tmp_path / "app.sqlite"
    migrate_database(path)
    storage = Store(path)
    yield storage
    await storage.engine.dispose()


class SummaryModel:
    def __init__(self):
        self.inputs = []

    async def ainvoke(self, messages):
        payload = json.loads(messages[-1].content)
        self.inputs.append(payload)
        return AIMessage(
            content=json.dumps(
                {"title": "个人助手技术选择", "summary": "主题：个人助手\n结论：使用 FastAPI\n待解决：浏览器登录"},
                ensure_ascii=False,
            )
        )


async def finish(service):
    async with asyncio.timeout(5):
        await asyncio.gather(*service._jobs.values())


async def test_full_conversation_summary_and_recent_activity_order(store):
    first = await store.save(Conversation())
    second = await store.save(Conversation())
    for role, content in [
        ("user", "想做个人助手"),
        ("assistant", "可以用 Flask 或 FastAPI"),
        ("user", "选择 FastAPI，浏览器如何登录还没决定"),
        ("assistant", "已记录技术选择，登录方案待讨论"),
    ]:
        await store.save(Message(conversation_id=first.id, role=role, content=content))
    model = SummaryModel()
    service = SummaryService(Settings(), store, model=model)
    service.schedule(first.id)
    await finish(service)
    result = await list_conversations(store)
    assert [row["id"] for row in result] == [first.id, second.id]
    assert result[0]["summary_status"] == "ready"
    assert result[0]["summary_message_count"] == result[0]["message_count"] == 4
    assert result[0]["title"] == "个人助手技术选择"
    assert "结论：使用 FastAPI" in result[0]["summary"]
    assert "想做个人助手" in model.inputs[0]["messages"]
    assert "登录方案待讨论" in model.inputs[0]["messages"]
    await service.close()


async def test_new_message_during_generation_discards_stale_result(store):
    conversation = await store.save(Conversation())
    await store.save(Message(conversation_id=conversation.id, role="user", content="用 Flask"))
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowModel:
        calls = 0

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls == 1:
                entered.set()
                await release.wait()
            latest = "FastAPI" if "FastAPI" in messages[-1].content else "Flask"
            return AIMessage(content=json.dumps({"title": latest, "summary": latest}))

    model = SlowModel()
    service = SummaryService(Settings(), store, model=model)
    service.schedule(conversation.id)
    async with asyncio.timeout(5):
        await entered.wait()
    await store.save(Message(conversation_id=conversation.id, role="user", content="改为 FastAPI"))
    release.set()
    await finish(service)
    updated = await store.get(Conversation, conversation.id)
    assert updated.summary_status == "ready"
    assert updated.summary == "FastAPI"
    assert updated.summary_message_count == 2
    assert model.calls == 2
    await service.close()


async def test_missing_config_failure_and_explicit_retry(store):
    conversation = await store.save(Conversation())
    await store.save(Message(conversation_id=conversation.id, role="user", content="如何登录浏览器"))
    service = SummaryService(Settings(model="", model_api_key=""), store)
    await service.rebuild(conversation.id)
    await finish(service)
    failed = await store.get(Conversation, conversation.id)
    assert failed.summary_status == "failed"
    assert "配置模型" in failed.summary_error
    assert failed.summary == ""
    service.model = SummaryModel()
    await service.rebuild(conversation.id)
    await finish(service)
    retried = await store.get(Conversation, conversation.id)
    assert retried.summary_status == "ready"
    assert retried.summary_error == ""
    await service.close()


async def test_restore_pending_resumes_after_process_restart(store):
    conversation = await store.save(Conversation(summary_status="pending"))
    await store.save(Message(conversation_id=conversation.id, role="user", content="保留待生成摘要"))
    # Message.save keeps a pending conversation pending and increments its revision.
    model = SummaryModel()
    service = SummaryService(Settings(), store, model=model)
    assert await service.restore_pending() == 1
    await finish(service)
    restored = await store.get(Conversation, conversation.id)
    assert restored.summary_status == "ready"
    assert restored.summary_message_count == 1
    await service.close()


async def test_migration_preserves_history_without_claiming_legacy_excerpt_is_summary(tmp_path):
    path = tmp_path / "legacy.sqlite"
    migrate_database(path, "0001")
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO conversations VALUES (?, ?, ?, ?, ?)",
            ("old", "local", "保留的标题", "最近任务：不是真实摘要", "2026-01-01"),
        )
        connection.execute(
            "INSERT INTO messages VALUES (?, ?, ?, ?, ?)",
            ("message", "old", "user", "保留的消息", "2026-01-02"),
        )
    migrate_database(path)
    storage = Store(path)
    try:
        conversation = await storage.get(Conversation, "old")
        assert conversation.summary == "最近任务：不是真实摘要"
        assert conversation.summary_status == "empty"
        listing = (await list_conversations(storage))[0]
        assert listing["summary"] == ""
        assert listing["message_count"] == 1
        assert listing["updated_at"] == "2026-01-02"
        assert (await storage.get(Message, "message")).content == "保留的消息"
    finally:
        await storage.engine.dispose()
