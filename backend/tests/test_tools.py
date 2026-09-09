from app.config import Settings
from app.main import migrate
from app.storage import Conversation, Store, Task
from app.tools import ToolRegistry


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
    await registry.execute(
        task.id, "remember1", "memory_save", {"key": "format", "value": "Markdown"}
    )
    result = await registry.execute(task.id, "lookup1", "memory_search", {"query": "format"})
    assert result["data"][0]["value"] == "Markdown"
    await store.engine.dispose()
