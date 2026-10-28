"""轮次上限在**真跑整图**时会停（MATRIX 2.2 的端到端判据）。

## 为什么这条必须有

上一条 `test_max_turns_enforced.py` 只验了「`check()` 会在 turns 到顶时返回
`max_turns`」——那是**单元层**。但 2.2 原本的虚报恰恰是「值算得出来、但没人读」，
所以必须有一条**真跑图**的用例回答最后一问：

**`turns` 这个计数器能不能穿过 checkpoint 一直累加？**

如果它每轮被重置（比如 `think` 的 `updates` 没被 checkpoint 收进去），
`check()` 永远看不到真实轮次，上限就永远不会到——**那 2.2 仍然是假的**，
只是假得更隐蔽。这条用例就是为了让那种情况变红。

## 构造

假模型**每次都返回一个工具调用**（`local_list_files`），于是图会一直
think → execute → think。把 `max_turns` 压到 3，任务必须在几轮内以
`stop_reason == "max_turns"` 收尾，而不是转到 `max_actions` 或永不停止。
"""

from __future__ import annotations

import asyncio

from langchain_core.messages import AIMessage

from app.agent import AgentRunner
from app.config import Settings
from app.main import migrate
from app.storage import Conversation, Store, Task
from app.tools import ToolRegistry


class _AlwaysCallsATool:
    """每次都要求调用 `local_list_files` —— 让图一直转下去。

    **call id 必须每次不同**：用同一个 id 会撞上「重复工具调用」检测
    （`tool_result_stored ... duplicate: True`），任务以「回复中断」失败，
    而不是走到轮次上限——那测的就不是 2.2 了。
    """

    def __init__(self) -> None:
        self.calls = 0

    def bind_tools(self, *_args, **_kwargs):
        return self

    async def ainvoke(self, _messages):
        self.calls += 1
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "local_list_files",
                    "args": {"path": "."},
                    "id": f"call-{self.calls}",
                },
            ],
        )


async def _wait_terminal(store, task_id, *, ceiling=90.0, stall=25.0):
    """等任务到终态。**不用固定墙钟**：整包跑时 CPU 被压满，纯本地活也会慢十几秒。"""
    started = asyncio.get_running_loop().time()
    last_progress = started
    last_seen = None
    while True:
        task = await store.get(Task, task_id)
        if task.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            return task
        if task.updated_at != last_seen:
            last_seen = task.updated_at
            last_progress = asyncio.get_running_loop().time()
        now = asyncio.get_running_loop().time()
        assert now - last_progress < stall, f"任务卡住不动：{task.status}"
        assert now - started < ceiling, "超过总上限"
        await asyncio.sleep(0.05)


async def test_runaway_loop_stops_on_max_turns(tmp_path):
    """一个「每轮都调工具」的循环，必须在 `max_turns` 处停下。

    `max_actions` 设得很大（100），`max_turns` 压到 3——这样停下来的原因
    **只可能是 `max_turns`**，不会是动作数上限碰巧撞上。
    """
    settings = Settings(
        data_dir=tmp_path / "data",
        workspace=tmp_path / "files",
        max_actions=100,
        max_turns=3,
        max_seconds=0,  # 关掉软时间闸，别让它把结论搅混
    )
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="一直转下去"))

    runner = AgentRunner(
        settings, store, ToolRegistry(settings, store), model=_AlwaysCallsATool()
    )
    await runner.start()
    try:
        await runner.enqueue(task.id)
        row = await _wait_terminal(store, task.id)

        # ① 真的停了（不是靠超时被动结束）
        assert row.status in {"SUCCEEDED", "FAILED"}, f"没到终态：{row.status}"

        # ② 停的原因必须是 max_turns —— 这是这条用例的核心判据
        events = await _events(store, task.id)
        stops = [e for e in events if e.get("event_type") == "budget_stopped"]
        assert stops, f"没有任何 budget_stopped 事件，说明循环不是被预算停下的：{events[-5:]}"
        reasons = {str((e.get("payload") or {}).get("reason")) for e in stops}
        assert "max_turns" in reasons, (
            f"停下来的原因不是 max_turns（实际：{reasons}）——"
            "说明 `turns` 计数器没有穿过 checkpoint 累加，2.2 仍然是假的"
        )

        # ③ **停了要告诉用户怎么办**：只说「被预算停下」等于让用户自己猜改哪个开关。
        # 90 够不够是用户的偏好，但「停了不说怎么调」是产品缺陷——这条可验、且不慢。
        hints = [
            str((e.get("payload") or {}).get("hint") or "")
            for e in stops
            if (e.get("payload") or {}).get("reason") == "max_turns"
        ]
        assert hints and any("--max-turns" in h for h in hints), (
            f"budget_stopped 没有可操作提示（实际：{hints}）"
        )

        # ④ 轮次确实到了上限附近，不是别的东西先停
        turns = _max_turns_recorded(stops)
        assert turns >= 3, f"停时只跑了 {turns} 轮，小于 max_turns=3"
    finally:
        await runner.close()
        await store.engine.dispose()


def _max_turns_recorded(stops: list[dict]) -> int:
    """`budget_stopped` 事件里没直接记 turns，用 actions 兜一层判断跑了多少步。

    图里每轮 think→execute 至少一次工具调用，所以 actions 是轮次的下界。
    """
    best = 0
    for event in stops:
        payload = event.get("payload") or {}
        try:
            best = max(best, int(payload.get("used_actions") or 0))
        except (TypeError, ValueError):
            continue
    return best


async def _events(store, task_id: str) -> list[dict]:
    from sqlalchemy import text

    async with store.engine.connect() as conn:
        rows = (
            await conn.execute(
                text(
                    "SELECT event_type, payload FROM task_events "
                    "WHERE task_id = :t ORDER BY created_at"
                ),
                {"t": task_id},
            )
        ).fetchall()
    import json

    out = []
    for event_type, payload in rows:
        try:
            decoded = json.loads(payload) if isinstance(payload, str) else (payload or {})
        except json.JSONDecodeError:
            decoded = {}
        out.append({"event_type": event_type, "payload": decoded})
    return out
