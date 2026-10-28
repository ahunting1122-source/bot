"""会话重置（MATRIX 13.5）：清空当前对话、保留长期记忆、`on_session_reset` 有发射点。

## 为什么需要「重置」这个动作本身

Orbit 之前**没有「会话重置」**——只有 `delete`（连会话一起删）与 `archive`。
于是钩子事件 `on_session_reset` **一直没有发射点**，而 13.5 的缺口正在这里。

**不能靠「在 delete 里顺手 emit」糊过去**：`delete` 不是 `reset`，
把删除伪装成重置会让订阅者收到一句假话。

## 四件语义，都是刻意选的

1. **清什么**：这条会话的 `Message` 行；
2. **不清什么**：`Conversation` 行本身、**以及 `MEMORY.md`**。
   **破坏性不对称**——不清可以手动清，**清了不可恢复**；长期记忆是跨会话的事实，
   重置的是「这次对话说到哪了」，不是「关于用户知道什么」；
3. **有活跃任务时拒绝**：等 = 用户点了没反应，夺 = 任务被腰斩，两者都不可接受；
4. **必须 `--yes`**：不可恢复的操作要有硬门禁。

## 边界

**活跃任务下的重置语义单独排期**（13.5 第二步），这条只做「无活跃任务时」。
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout


def _run_cli(data_dir, *argv: str) -> tuple[int, str, str]:
    from app.cli.main import main as cli_main

    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = cli_main(["--data-dir", str(data_dir), *argv])
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


async def _seed(tmp_path, *, active_task: bool = False):
    from app.config import Settings
    from app.main import migrate
    from app.storage import Conversation, Message, Store, Task

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    (settings.data_dir / "MEMORY.md").write_text("- 用户喜欢简短回答\n", encoding="utf-8")
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    await store.save(Message(conversation_id=conversation.id, role="user", content="hi"))
    await store.save(Message(conversation_id=conversation.id, role="assistant", content="yo"))
    if active_task:
        await store.save(
            Task(conversation_id=conversation.id, goal="正在跑", status="RUNNING")
        )
    await store.engine.dispose()
    return settings, conversation.id


async def test_reset_clears_messages_but_keeps_memory(tmp_path):
    """**核心判据**：消息清空、会话行与 `MEMORY.md` 都保留。"""
    from sqlalchemy import func, select

    from app.storage import Message, Store

    settings, conversation_id = await _seed(tmp_path)
    code, out, err = _run_cli(
        settings.data_dir, "sessions", "reset", conversation_id, "--yes"
    )
    assert code == 0, f"重置失败：{err}"

    store = Store(settings.data_dir / "app.sqlite")
    try:
        async with store.sessions() as session:
            remaining = (
                await session.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.conversation_id == conversation_id)
                )
            ).scalar_one()
        assert remaining == 0, "消息没被清空"
    finally:
        await store.engine.dispose()

    assert (settings.data_dir / "MEMORY.md").exists(), "长期记忆被误删了"
    assert "长期记忆" in out, "输出没说明「长期记忆保留」——用户不知道清到哪"


async def test_reset_refuses_without_yes(tmp_path):
    """不可恢复的操作要有硬门禁。"""
    settings, conversation_id = await _seed(tmp_path)
    code, _out, err = _run_cli(settings.data_dir, "sessions", "reset", conversation_id)
    assert code != 0, "没加 --yes 却执行了"
    assert "--yes" in err


async def test_reset_refuses_when_a_task_is_active(tmp_path):
    """**并发语义**：有活跃任务时拒绝，并说清为什么不替你决定。

    等 = 用户点了没反应，夺 = 任务被腰斩。**两者都不该由一条清理命令替你选。**
    """
    settings, conversation_id = await _seed(tmp_path, active_task=True)
    code, _out, err = _run_cli(
        settings.data_dir, "sessions", "reset", conversation_id, "--yes"
    )
    assert code != 0, "有活跃任务却执行了重置"
    assert "未结束的任务" in err, f"没说清为什么拒绝：{err}"
    # 消息必须原封不动
    from sqlalchemy import func, select

    from app.storage import Message, Store

    store = Store(settings.data_dir / "app.sqlite")
    try:
        async with store.sessions() as session:
            remaining = (
                await session.execute(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.conversation_id == conversation_id)
                )
            ).scalar_one()
        assert remaining == 2, "拒绝执行却已经动了数据"
    finally:
        await store.engine.dispose()


async def test_the_on_session_reset_hook_really_fires(tmp_path):
    """**13.5 的实质**：`on_session_reset` 必须真的被发射。

    这条钉的是「钩子有发射点」，不是「钩子注册表里有这个名字」——
    后者早就有了，缺的一直是发射。
    """
    from app import hooks

    settings, conversation_id = await _seed(tmp_path)
    seen: list[dict] = []

    def _listener(payload):
        seen.append(dict(payload))

    hooks.DEFAULT_HOOKS.register("on_session_reset", _listener)
    try:
        code, _out, err = _run_cli(
            settings.data_dir, "sessions", "reset", conversation_id, "--yes"
        )
        assert code == 0, f"重置失败：{err}"
    finally:
        hooks.DEFAULT_HOOKS.unregister("on_session_reset", _listener)

    assert seen, "`on_session_reset` 一次都没发射——钩子还是没有发射点"
    assert seen[0]["conversation_id"] == conversation_id
    assert seen[0]["cleared_messages"] == 2
    # `scope` 说清「只清消息」——订阅者据此判断要不要动自己的缓存
    assert seen[0]["scope"] == "messages_only"


def test_the_event_is_now_in_the_emitted_set():
    """**结构性判据**：`on_session_reset` 必须进 `EMITTED_EVENTS`。

    `register()` 会对「已知但没有发射点」的事件打 warning——那个警告是有用的，
    所以一个真接上发射点的事件必须从警告名单里出来。
    """
    from app.hooks import EMITTED_EVENTS

    assert "on_session_reset" in EMITTED_EVENTS, (
        "`on_session_reset` 有发射点了却没进 `EMITTED_EVENTS`——"
        "订阅它的人会继续收到「这个事件没有发射点」的假警告"
    )


def test_the_command_is_registered():
    """命令真的挂在 `orbit sessions` 下面，且 handler 已绑定。"""
    from app.cli import REGISTRY

    parent = REGISTRY.get("sessions")
    assert parent is not None
    sub = next((item for item in parent.subcommands if item.name == "reset"), None)
    assert sub is not None, "`orbit sessions reset` 没注册"
    assert sub.handler is not None, "注册了但没有 handler——`state` 不会变 done"
