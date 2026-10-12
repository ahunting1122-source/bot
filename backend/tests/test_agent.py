import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk
from sqlalchemy import select

from app.agent import AgentRunner
from app.config import Settings
from app.main import migrate
from app.storage import Conversation, Message, Store, Task, TaskEvent
from app.tools import ToolRegistry


class AskModel:
    """先请求澄清挂起，恢复后继续；用于验证 checkpoint 与恢复语义。"""

    def __init__(self):
        self.outputs = iter(
            [
                AIMessage(
                    content="我需要先确认格式。",
                    tool_calls=[
                        {
                            "name": "ask_user",
                            "args": {"question": "报告用什么格式？", "reason": "clarification"},
                            "id": "ask1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="好的，按中文 Markdown 处理。"),
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
    model = AskModel()
    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=model)
    await runner.start()
    paused = await wait_status(store, task.id, "WAITING_USER")
    assert paused.waiting["question"] == "报告用什么格式？"
    await runner.close()
    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=model)
    await runner.start()
    await runner.enqueue(task.id, {"approved": True, "answer": "中文 Markdown"})
    await wait_status(store, task.id, "SUCCEEDED")
    reply = await store.get(Message, f"answer-{task.id}")
    assert reply.content == "好的，按中文 Markdown 处理。"
    await runner.close()
    await store.engine.dispose()


@pytest.fixture
async def streaming_store(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    yield settings, store
    await store.engine.dispose()


async def create_reply(store, content):
    conversation = await store.save(Conversation())
    await store.save(Message(conversation_id=conversation.id, role="user", content=content))
    task = await store.save(Task(conversation_id=conversation.id, goal=content))
    return conversation, task


class SummarySpy:
    def __init__(self):
        self.scheduled = []

    def schedule(self, conversation_id):
        self.scheduled.append(conversation_id)


async def test_async_chunks_stream_before_completion_and_keep_activity_separate(streaming_store):
    settings, store = streaming_store
    activity = "我先查看本地工作目录中的文件。"
    chunks = ["# 文件说明\n\n" + "这是正文。" * 20, "\n\n" + "这是第二段。" * 20, "\n\n结束。"]
    first_sent, finish_reply = asyncio.Event(), asyncio.Event()
    private_reasoning = "PRIVATE_REASONING_NOT_FOR_DISPLAY"

    class StreamingModel:
        def bind_tools(self, *args, **kwargs):
            return self

        async def astream(self, messages):
            if not any(message.type == "tool" for message in messages):
                yield AIMessageChunk(
                    content=activity,
                    additional_kwargs={"reasoning_content": private_reasoning},
                    tool_call_chunks=[
                        {
                            "name": "local_list_files",
                            "args": '{"path":',
                            "id": "inspect",
                            "index": 0,
                        }
                    ],
                )
                yield AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": None,
                            "args": '"."}',
                            "id": None,
                            "index": 0,
                        }
                    ],
                )
            else:
                yield AIMessageChunk(content=chunks[0])
                first_sent.set()
                await finish_reply.wait()
                yield AIMessageChunk(
                    content=chunks[1], additional_kwargs={"reasoning_content": private_reasoning}
                )
                yield AIMessageChunk(content=chunks[2])

    conversation, task = await create_reply(store, "看看工作目录，再说明内容。")
    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=StreamingModel())
    await runner.start()
    try:
        async with asyncio.timeout(10):
            await first_sent.wait()
        live = await store.rows(
            select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.id)
        )
        assert (await store.get(Task, task.id)).status == "RUNNING"
        assert not await store.rows(select(Message).where(Message.role == "assistant"))
        assert any(
            event.event_type == "message_delta" and event.payload["text"] == chunks[0]
            for event in live
        )
        finish_reply.set()
        await wait_status(store, task.id, "SUCCEEDED")
        await runner.queue.join()
        events = await store.rows(
            select(TaskEvent).where(TaskEvent.task_id == task.id).order_by(TaskEvent.id)
        )
        completed = next(
            event
            for event in events
            if event.event_type == "message_finished" and event.payload["response_kind"] == "answer"
        )
        response_id = completed.payload["response_id"]
        answer_events = [
            event
            for event in events
            if event.event_type == "message_delta" and event.payload["response_id"] == response_id
        ]
        assert len(answer_events) >= 2
        assert all(event.payload["conversation_id"] == conversation.id for event in answer_events)
        assert all(event.payload["message_id"] == f"answer-{task.id}" for event in answer_events)
        answer_messages = await store.rows(select(Message).where(Message.role == "assistant"))
        assert len(answer_messages) == 1
        assert (
            "".join(event.payload["text"] for event in answer_events)
            == answer_messages[0].content
            == "".join(chunks)
        )
        activities = [
            event.payload["text"] for event in events if event.event_type == "activity_summary"
        ]
        assert activities == [activity]
        assert all(item != answer_messages[0].content for item in activities)
        assert private_reasoning not in json.dumps([event.payload for event in events])
        assert private_reasoning not in answer_messages[0].content
    finally:
        finish_reply.set()
        await runner.close()


async def test_cancel_during_stream_preserves_partial_and_worker_keeps_running(streaming_store):
    settings, store = streaming_store
    first_sent, hold_open = asyncio.Event(), asyncio.Event()
    partial = "已经输出的这部分应在停止后保留。" * 8

    class InterruptedModel:
        calls = 0

        def bind_tools(self, *args, **kwargs):
            return self

        async def astream(self, messages):
            self.calls += 1
            if self.calls == 1:
                yield AIMessageChunk(content=partial)
                first_sent.set()
                await hold_open.wait()
                yield AIMessageChunk(content="这部分不应在停止后出现。")
            else:
                yield AIMessageChunk(content="后续回复正常。")

    conversation, task = await create_reply(store, "请详细解释。")
    summaries = SummarySpy()
    runner = AgentRunner(
        settings,
        store,
        ToolRegistry(settings, store),
        model=InterruptedModel(),
        summaries=summaries,
    )
    await runner.start()
    try:
        async with asyncio.timeout(10):
            await first_sent.wait()
        # The cancel endpoint transitions first, then interrupts the running stream.
        await store.transition(task.id, "CANCELLED")
        await runner.cancel(task.id)
        await runner.queue.join()
        assert (await store.get(Task, task.id)).status == "CANCELLED"
        messages = await store.rows(select(Message).where(Message.role == "assistant"))
        assert len(messages) == 1
        assert messages[0].id == f"answer-{task.id}"
        assert messages[0].content == partial
        events = await store.rows(select(TaskEvent).where(TaskEvent.task_id == task.id))
        assert not any(event.event_type in {"task_completed", "task_failed"} for event in events)
        assert not runner.worker.done()
        assert summaries.scheduled == [conversation.id]
        followup = await store.save(Task(conversation_id=conversation.id, goal="继续聊。"))
        await runner.enqueue(followup.id)
        await wait_status(store, followup.id, "SUCCEEDED")
        await runner.queue.join()
        assert (await store.get(Message, f"answer-{followup.id}")).content == "后续回复正常。"
    finally:
        hold_open.set()
        await runner.close()


async def test_failed_stream_keeps_partial_and_refreshes_summary(streaming_store):
    settings, store = streaming_store

    class FailedModel:
        def bind_tools(self, *args, **kwargs):
            return self

        async def astream(self, messages):
            yield AIMessageChunk(content="这部分回复已经生成。")
            raise ConnectionError("The stream was interrupted.")

    conversation, task = await create_reply(store, "解释一下连接过程。")
    summaries = SummarySpy()
    runner = AgentRunner(
        settings, store, ToolRegistry(settings, store), model=FailedModel(), summaries=summaries
    )
    await runner.start()
    try:
        await wait_status(store, task.id, "FAILED")
        await runner.queue.join()
        assert (await store.get(Message, f"answer-{task.id}")).content == "这部分回复已经生成。"
        assert summaries.scheduled == [conversation.id]
    finally:
        await runner.close()
