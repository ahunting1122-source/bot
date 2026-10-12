"""工具结果治理：原文落库、大小/token 观测、checksum 幂等、不进上下文的字段被剥离。"""

from sqlalchemy import select

from app.config import Settings
from app.main import migrate
from app.policies import TOOL_RESULT_LIMIT, DefaultToolResultPolicy, estimate_tokens
from app.storage import Conversation, Store, Task, ToolResult


def test_project_keeps_structured_text_out_of_the_model_context():
    policy = DefaultToolResultPolicy()
    result = {
        "status": "ok",
        "data": {"url": "https://www.bilibili.com", "text": "正文", "structured": "# 标题\n正文"},
    }
    projected = policy.project("browser_observe", result)
    assert "structured" not in projected["data"]
    assert projected["data"]["url"] == "https://www.bilibili.com"
    # 原对象不能被动过：digest 还要拿它去索引。
    assert result["data"]["structured"] == "# 标题\n正文"


def test_project_leaves_other_tools_untouched():
    policy = DefaultToolResultPolicy()
    result = {"status": "ok", "data": {"text": "文件正文"}}
    assert policy.project("local_read_document", result) == result


def test_token_estimate_counts_chinese_by_character():
    assert estimate_tokens("机械键盘") == 4
    assert estimate_tokens("abcdefgh") == 2


async def test_record_is_idempotent_per_operation_id(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="看看排行"))
    policy = DefaultToolResultPolicy()
    kwargs = {
        "task_id": task.id,
        "operation_id": f"{task.id}:call1",
        "name": "browser_observe",
        "status": "ok",
        "host": "www.bilibili.com",
        "text": "机械键盘排行：" + "正文" * 50,
    }
    first = await policy.record(store, **kwargs)
    second = await policy.record(store, **kwargs)
    assert first["duplicate"] is False
    assert second["duplicate"] is True
    assert second["id"] == first["id"]
    rows = await store.rows(select(ToolResult))
    assert len(rows) == 1
    assert rows[0].bytes > 0 and rows[0].tokens > 0
    assert rows[0].host == "www.bilibili.com"
    await store.engine.dispose()


async def test_record_truncates_oversized_results(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="长结果"))
    policy = DefaultToolResultPolicy()
    meta = await policy.record(
        store,
        task_id=task.id,
        operation_id="op-long",
        name="local_read_document",
        status="ok",
        host="",
        text="字" * (TOOL_RESULT_LIMIT + 500),
    )
    assert meta["truncated"] is True
    assert (await store.get(ToolResult, meta["id"])).text == "字" * TOOL_RESULT_LIMIT
    await store.engine.dispose()


def test_should_index_only_successful_non_retrieval_calls():
    policy = DefaultToolResultPolicy()
    assert policy.should_index("browser_observe", "ok") is True
    assert policy.should_index("browser_observe", "error") is False
    assert policy.should_index("ask_user", "ok") is False
    assert policy.should_index("retriever_search", "ok") is False
