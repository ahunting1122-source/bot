"""会话租约真的拦住并发（MATRIX 10.8 的验收判据）。

## 之前缺的是什么

`LeaseStore` 有 `acquire` / `release` / `heartbeat` / `verdicts` / `reap_stale`，
测试也齐。但 **`acquire()` 零生产调用方**——`orbit plane` 只是构造它来报告状态，
所以 `runtime/active_sessions.json` 在生产里**永远不会被创建**。

后果：同一个会话被两条路径同时驱动（CLI 一个任务没跑完又开一个、或 gateway 与 CLI 撞上）
时，**两边都在往同一份 checkpoint 写**，消息历史交错。**这类损坏不报错**，
只会在事后表现为「对话内容莫名其妙」——最难查的一类。

## 判据
1. 任务跑起来之后，`active_sessions.json` 里**真的有这个会话**；
2. 任务结束后（含失败）**释放掉**——否则那个会话永远显示"有人持有"；
3. **被别人接管后，原任务自己停下**（`stop_reason == "session_takeover"`），
   而不是继续往同一份 checkpoint 写。
"""

from __future__ import annotations

import asyncio
import json

from langchain_core.messages import AIMessage


class _Loops:
    """每轮都调一个工具——给「中途被接管」留出足够的循环窗口。"""

    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, *_a, **_k):
        return self

    async def ainvoke(self, _m):
        self.calls += 1
        return AIMessage(
            content="",
            tool_calls=[
                {"name": "local_list_files", "args": {"path": "."}, "id": f"c{self.calls}"}
            ],
        )


class _Once:
    def bind_tools(self, *_a, **_k):
        return self

    async def ainvoke(self, _m):
        return AIMessage(content="好了。")


async def _stack(tmp_path, model):
    from app.agent import AgentRunner
    from app.config import Settings
    from app.main import migrate
    from app.storage import Conversation, Store, Task
    from app.tools import ToolRegistry

    settings = Settings(
        data_dir=tmp_path / "data",
        workspace=tmp_path / "files",
        max_actions=200,
        max_turns=200,
        max_seconds=0,
    )
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="跑一下"))
    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=model)
    await runner.start()
    return runner, store, task, conversation, settings


async def _wait(store, task_id, *, ceiling=40.0):
    from app.storage import Task

    loop = asyncio.get_running_loop()
    started = loop.time()
    while True:
        row = await store.get(Task, task_id)
        if row.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return row
        assert loop.time() - started < ceiling, "任务没在预期内到终态"
        await asyncio.sleep(0.03)


async def test_active_sessions_file_is_created_and_cleaned(tmp_path):
    """租约文件真的被写、也被清——之前它在生产里从不出现。"""
    runner, store, task, conversation, settings = await _stack(tmp_path, _Once())
    ledger = settings.data_dir / "runtime" / "active_sessions.json"
    try:
        await runner.enqueue(task.id)
        await _wait(store, task.id)

        # **必须轮询等释放，不能查一次就断言**：状态转成终态发生在 try 里，
        # 而 `release` 在 `finally` 里——两者之间有窗口。单跑时窗口小到看不出来，
        # 全量跑（CPU 被压满）就会红。**这不是产品的错，是断言了还没到的状态。**
        for _ in range(400):
            data = json.loads(ledger.read_text(encoding="utf-8")) if ledger.exists() else {}
            if conversation.id not in data:
                return
            await asyncio.sleep(0.03)
        raise AssertionError(
            f"任务结束 12 秒后会话仍被登记为持有：{data}——下次会以为有人在跑"
        )
    finally:
        await runner.close()
        await store.engine.dispose()


async def test_takeover_makes_the_original_task_yield(tmp_path, monkeypatch):
    """**被别人接管后，原任务必须自己停下**——这是这条功能存在的理由。"""
    from sqlalchemy import text

    # 产品里心跳有 10 秒节流（`heartbeat` 是文件读写，不能每轮做）。
    # 测试要立刻看到反应，所以把节流关掉——**验的是接管语义，不是节流间隔**。
    from app.graph import nodes as nodes_module

    monkeypatch.setattr(nodes_module, "LEASE_HEARTBEAT_SECONDS", 0.0)

    runner, store, task, conversation, settings = await _stack(tmp_path, _Loops())
    try:
        await runner.enqueue(task.id)

        # 等它真的跑起来并登记了租约
        ledger = settings.data_dir / "runtime" / "active_sessions.json"
        for _ in range(200):
            if ledger.exists() and conversation.id in ledger.read_text(encoding="utf-8"):
                break
            await asyncio.sleep(0.03)
        else:
            raise AssertionError("任务跑起来了却没登记会话租约——acquire 没接上")

        # 模拟「另一个进程接管了这个会话」
        runner.leases.acquire(conversation.id, platform="other", pid=999_998, start_time=1.0)

        row = await _wait(store, task.id)
        assert row.status in {"SUCCEEDED", "FAILED"}, f"没到终态：{row.status}"

        async with store.engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT payload FROM task_events "
                        "WHERE task_id=:t AND event_type='budget_stopped'"
                    ),
                    {"t": task.id},
                )
            ).fetchall()
        reasons = {str(json.loads(p).get("reason")) for (p,) in rows if p}
        assert "session_takeover" in reasons, (
            f"被接管后没有停下（budget_stopped 的原因：{reasons}）——"
            "两边会继续往同一份 checkpoint 交错写"
        )
    finally:
        await runner.close()
        await store.engine.dispose()
