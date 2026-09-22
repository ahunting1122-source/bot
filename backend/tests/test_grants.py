import asyncio

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.agent import AgentRunner
from app.browser import BROWSER_TOOLS
from app.config import Settings, dump_grants, load_grants
from app.connectors import ConnectorConfig
from app.main import migrate
from app.storage import Conversation, Message, Store, Task
from app.tools import ToolRegistry


def test_grant_roundtrip_ignores_broken_payloads():
    assert load_grants("") == {}
    assert load_grants("not json") == {}
    assert load_grants('["a"]') == {}
    grants = {"www.bilibili.com": ["browser_click", "browser_click", "browser_press"]}
    assert load_grants(dump_grants(grants)) == {
        "www.bilibili.com": ["browser_click", "browser_press"]
    }


def test_grant_persists_and_refuses_outward_writes(tmp_path):
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    config = ConnectorConfig(settings)
    assert config.grant("www.bilibili.com", "browser_click") == {
        "www.bilibili.com": ["browser_click"]
    }
    assert config.grant("www.bilibili.com", "browser_fill")["www.bilibili.com"] == [
        "browser_click",
        "browser_fill",
    ]
    # Reloading from disk keeps the remembered actions.
    reloaded = ConnectorConfig(Settings(data_dir=tmp_path, workspace=tmp_path / "files"))
    reloaded.load()
    assert reloaded.settings.granted_actions == settings.granted_actions
    with pytest.raises(ValueError):
        config.grant("www.bilibili.com", "notion_append_summary")
    with pytest.raises(ValueError):
        config.grant("www.bilibili.com", "memory_delete")
    with pytest.raises(ValueError):
        config.grant("*.bilibili.com", "browser_click")
    assert "notion_append_summary" not in settings.granted_actions


def test_revoke_clears_only_that_site(tmp_path):
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    config = ConnectorConfig(settings)
    config.grant("www.bilibili.com", "browser_click")
    config.grant("www.ithome.com", "browser_click")
    config.revoke("www.bilibili.com")
    assert load_grants(settings.granted_actions) == {"www.ithome.com": ["browser_click"]}


class StubBrowser:
    def __init__(self, host="www.bilibili.com"):
        self.host = host

    def current_host(self):
        return self.host

    async def execute(self, name, args, task_id, operation_id):
        return {"status": "ok", "data": {"clicked": True}}


def test_registry_only_skips_grantable_actions_on_the_open_site(tmp_path):
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    settings.granted_actions = dump_grants({"www.bilibili.com": ["browser_click"]})
    registry = ToolRegistry(settings, None)
    registry.browser = StubBrowser()
    assert registry.granted("browser_click") is True
    assert registry.granted("browser_fill") is False
    assert registry.granted("notion_append_summary") is False
    registry.browser = StubBrowser("www.ithome.com")
    assert registry.granted("browser_click") is False
    registry.browser = None
    assert registry.granted("browser_click") is False


class ClickModel:
    def __init__(self):
        self.outputs = iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "browser_click",
                            "args": {"ref": "e1", "version": 1},
                            "id": "call1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="点完了。"),
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


async def run_click_task(tmp_path, granted_host):
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    await store.save(Message(conversation_id=conversation.id, role="user", content="点一下排序"))
    task = await store.save(Task(conversation_id=conversation.id, goal="点一下排序"))
    if granted_host:
        config = ConnectorConfig(settings)
        config.grant(granted_host, "browser_click")
    browser = StubBrowser()
    registry = ToolRegistry(settings, store)
    registry.register(BROWSER_TOOLS, browser.execute)
    registry.browser = browser
    runner = AgentRunner(settings, store, registry, model=ClickModel())
    await runner.start()
    await runner.enqueue(task.id)
    return store, task, runner


async def test_remembered_click_runs_without_asking_again(tmp_path):
    store, task, runner = await run_click_task(tmp_path, "www.bilibili.com")
    await wait_status(store, task.id, "SUCCEEDED")
    replies = [m for m in await store.rows(select(Message)) if m.role == "assistant"]
    assert replies and replies[-1].content == "点完了。"
    await runner.close()
    await store.engine.dispose()


async def test_click_still_asks_when_nothing_was_remembered(tmp_path):
    store, task, runner = await run_click_task(tmp_path, None)
    paused = await wait_status(store, task.id, "WAITING_USER")
    assert paused.waiting["reason"] == "approval"
    assert paused.waiting["tool"] == "browser_click"
    await runner.close()
    await store.engine.dispose()


async def test_remembered_site_does_not_cover_another_site(tmp_path):
    store, task, runner = await run_click_task(tmp_path, "www.ithome.com")
    paused = await wait_status(store, task.id, "WAITING_USER")
    assert paused.waiting["tool"] == "browser_click"
    await runner.close()
    await store.engine.dispose()
