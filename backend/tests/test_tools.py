from langchain_core.messages import HumanMessage

from app.config import Settings
from app.main import migrate
from app.storage import Conversation, Store, Task
from app.tools import RETRIEVER_CALL_LIMIT, ToolRegistry, memory_write_allowed


async def test_local_read_report_and_memory(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="整理笔记"))
    registry = ToolRegistry(settings, store)
    (settings.workspace / "notes.md").write_text("# 项目\n使用 FastAPI", encoding="utf-8")
    read = await registry.execute(task.id, "read1", "local_read_document", {"path": "notes.md"})
    assert "FastAPI" in read["data"]["text"]
    args = {"path": "reports/result.md", "content": "# 完成\n基于笔记"}
    first = await registry.execute(task.id, "write1", "local_write_report", args)
    second = await registry.execute(task.id, "write1", "local_write_report", args)
    assert first == second and first["data"]["verified"]
    denied = await registry.execute(
        task.id, "read2", "local_read_document", {"path": "../outside.txt"}
    )
    assert denied["status"] == "error"
    # M2 写入门槛：用户没说「记住」时 memory_save 被拒（模型不能自行沉淀）。
    refused = await registry.execute(
        task.id,
        "remember0",
        "memory_save",
        {"key": "format", "value": "Markdown"},
        [HumanMessage(content="帮我整理一下笔记")],
    )
    assert refused["status"] == "refused" and "没有明确要求记住" in refused["notice"]
    # 用户显式说了「记住」才放行。
    saved = await registry.execute(
        task.id,
        "remember1",
        "memory_save",
        {"key": "format", "value": "Markdown"},
        [HumanMessage(content="记住：我写报告都用 Markdown")],
    )
    assert saved["status"] == "ok"
    result = await registry.execute(task.id, "lookup1", "memory_search", {"query": "format"})
    assert result["data"][0]["value"] == "Markdown"
    await store.engine.dispose()


def test_memory_write_gate_requires_an_explicit_user_instruction():
    allowed = memory_write_allowed("记住：我写报告都用中文 Markdown")
    assert allowed == (True, "")
    assert memory_write_allowed("请记一下我的报告校验码是 123")[0] is True
    assert memory_write_allowed("Remember this: I prefer dark mode")[0] is True
    # 用户没说 → 拒绝，并给出可读理由（模型据此继续干活而不是反复重试）
    ok, reason = memory_write_allowed("帮我整理一下笔记")
    assert ok is False and "没有明确要求记住" in reason


def test_memory_write_gate_does_not_mistake_negation_for_consent():
    """「不用记住」和「你为什么没记住」都不能被当成写入授权。"""
    for text in ("这个不用记住", "别记这个了", "你根本没记住我上次说的", "记住了吗？"):
        ok, _ = memory_write_allowed(text)
        assert ok is False, text
    ok, reason = memory_write_allowed("这个不用记住")
    assert "不用记" in reason


def test_memory_write_gate_ignores_blank_input():
    ok, reason = memory_write_allowed("   ")
    assert ok is False and reason


async def test_refused_memory_write_is_not_cached_as_done(tmp_path):
    """拒绝是非终态：不能因为幂等短路把"拒绝"永久钉死。

    回归：`refused` 曾被当成 DONE 落库，导致同一 operation_id 再执行时直接
    命中幂等缓存返回旧的拒绝结果——即使用户随后补了「记住」，节点重放
    （checkpoint 恢复 / 断点续跑）也永远拿不回成功结果。
    """
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="整理笔记"))
    registry = ToolRegistry(settings, store)
    args = {"key": "格式", "value": "Markdown"}

    refused = await registry.execute(
        task.id, "same-op", "memory_save", args, [HumanMessage(content="整理一下笔记")]
    )
    assert refused["status"] == "refused"
    # 落库状态必须是 WAITING（非终态），不能是 DONE
    from app.storage import Operation

    stored = await store.get(Operation, "same-op")
    assert stored.status == "WAITING", f"拒绝不该落成终态，实际 {stored.status}"

    # 同一 operation_id 再次执行：用户这回明确说了「记住」→ 必须真的写入
    saved = await registry.execute(
        task.id, "same-op", "memory_save", args, [HumanMessage(content="记住：我用 Markdown")]
    )
    assert saved["status"] == "ok", f"被拒后的重放应能成功，实际 {saved['status']}"
    found = await registry.execute(task.id, "lookup2", "memory_search", {"query": "格式"})
    assert found["data"] and found["data"][0]["value"] == "Markdown"
    await store.engine.dispose()


async def test_retriever_search_stops_after_the_per_task_limit(tmp_path):
    """第 N+1 次调用只回提示，不再真正检索（防多轮补检失控）。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="回看之前读过的页面"))
    registry = ToolRegistry(settings, store)
    calls = {}
    allowed = 0
    for index in range(RETRIEVER_CALL_LIMIT + 1):
        result = await registry.execute(
            task.id, f"find{index}", "retriever_search", {"query": "机械键盘"}
        )
        assert result["status"] == "ok"
        calls[index] = result["data"]
        if "notice" not in result["data"]:
            allowed += 1
    assert allowed == RETRIEVER_CALL_LIMIT
    assert "上限" in calls[RETRIEVER_CALL_LIMIT]["notice"]
    assert calls[RETRIEVER_CALL_LIMIT]["hits"] == []
    await store.engine.dispose()
