import asyncio

from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.agent import AgentRunner
from app.config import Settings
from app.main import migrate
from app.storage import Conversation, Memory, Message, Store, Task
from app.tools import ToolRegistry


class ScriptModel:
    def __init__(self):
        self.outputs = iter(
            [
                AIMessage(
                    content="我会记住你的报告格式。",
                    tool_calls=[
                        {
                            "name": "memory_save",
                            "args": {"key": "format", "value": "中文 Markdown"},
                            "id": "call1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="已保存报告格式偏好。"),
            ]
        )

    def bind_tools(self, *args, **kwargs):
        return self

    async def ainvoke(self, messages):
        return next(self.outputs)


async def wait_status(store, task_id, target):
    async with asyncio.timeout(10):
        while True:
            task = await store.get(Task, task_id)
            if task.status == target:
                return task
            assert task.status != "FAILED", task.error
            await asyncio.sleep(0.05)


async def test_graph_pause_restart_resume(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    await store.save(
        Message(conversation_id=conversation.id, role="user", content="记住报告使用中文 Markdown")
    )
    task = await store.save(Task(conversation_id=conversation.id, goal="记住报告使用中文 Markdown"))
    model = ScriptModel()
    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=model)
    await runner.start()
    paused = await wait_status(store, task.id, "WAITING_USER")
    assert paused.waiting["reason"] == "approval"
    await runner.close()
    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=model)
    await runner.start()
    await runner.enqueue(task.id, {"approved": True, "answer": "确认"})
    await wait_status(store, task.id, "SUCCEEDED")
    assert (await store.rows(select(Memory)))[0].value == "中文 Markdown"
    await runner.close()
    await store.engine.dispose()
