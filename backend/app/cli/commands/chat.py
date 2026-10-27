"""`orbit chat` 与 oneshot 模式（`-z`）——在终端里直接跟 agent 对话。

在此之前 Orbit **只能通过 Web 工作台使用**：后端要 `scripts/start.ps1` 起、浏览器要开
`127.0.0.1:6185`。一个"命令行 agent"没有命令行入口，是功能面里最显眼的缺口之一
（`chat` 也是 Hermes 的 P0 命令）。

## 与 Web 工作台的差异（都要如实说，不能假装一样）

- **不接 MCP**：`MCPManager.start()` 会拉起外部进程，在一次性 CLI 会话里留下孤儿进程的
  风险高于收益。要 MCP 请用 Web 工作台。工具清单里会少掉 `mcp__*`。
- **浏览器工具按需启动**：CLI 里第一次调用 `browser_*` 才会拉起 Chromium；不想它弹窗口
  就用 `--headless`（默认跟随 settings）。
- **审批**：Orbit 的默认审批模式是 `off`（只拦硬地板）。`--yolo` 因此**本来就是默认行为**，
  保留这个 flag 只是为了命令面对齐；它不会关掉硬地板。

## 流式怎么做的

不自己造事件流：`AgentRunner` 已经把过程写进 `task_events`（`message_delta` 是增量文本，
其余是工具/反思事件）。这里按 `id` 游标增量读，既拿到实时输出，又天然支持"中途失败也能
看到已经发生的事"。
"""

from __future__ import annotations

import sys
import time
from typing import Any

from sqlalchemy import select

from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option

#: 终态：出现这些状态就停止轮询。
TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "WAITING_USER")

#: 轮询间隔（秒）。0.15s 在"够实时"与"别把 SQLite 读爆"之间。
POLL_SECONDS = 0.15


def _build_stack(context: Context):
    """把 agent 栈搭起来。**必须在 CLI 的事件循环线程上调用**（它是协程）。"""
    from app.agent import AgentRunner
    from app.browser import BROWSER_TOOLS, BrowserController
    from app.connectors import GITHUB_TOOLS, GitHubConnector
    from app.main import migrate
    from app.storage import Store
    from app.summaries import SummaryService
    from app.tools import ToolRegistry

    settings = context.settings
    settings.prepare()
    migrate(settings)

    async def _setup():
        store = Store(settings.data_dir / "app.sqlite")
        await store.tune()
        registry = ToolRegistry(settings, store)
        registry.register(GITHUB_TOOLS, GitHubConnector(settings).execute)
        registry.register(BROWSER_TOOLS, BrowserController(settings, store).execute)
        from app.web import WEB_TOOLS, WebTools

        registry.register(WEB_TOOLS, WebTools(settings).execute)
        summaries = SummaryService(settings, store)
        runner = AgentRunner(settings, store, registry, summaries=summaries)
        await runner.start()
        return runner, store, summaries

    return context.run(_setup())


def _conversation(context: Context, store, *, resume: bool, title: str):
    """挑一个会话：`--continue` 取最近一个，否则新建。"""
    from app.storage import Conversation

    async def _pick():
        if resume:
            rows = await store.rows(
                select(Conversation).order_by(Conversation.updated_at.desc()).limit(1)
            )
            if rows:
                return rows[0]
        return await store.save(Conversation(title=title or "命令行对话"))

    return context.run(_pick())


def _submit(context: Context, store, runner, conversation_id: str, text: str):
    """落一条用户消息 + 建任务 + 入队。返回 task_id。"""
    from app.storage import Message, Task

    async def _do():
        await store.save(
            Message(conversation_id=conversation_id, role="user", content=text)
        )
        task = await store.save(Task(conversation_id=conversation_id, goal=text))
        await store.event(task.id, "task_created", goal=text)
        await runner.enqueue(task.id)
        return task.id

    return context.run(_do())


def _drive(context: Context, store, runner, task_id: str, *, stream: bool) -> dict[str, Any]:
    """按 `task_events` 游标推进，直到终态。返回 {status, text, question}。"""
    from app.storage import Task, TaskEvent

    state: dict[str, Any] = {"status": "", "text": "", "question": "", "answer": ""}
    cursor = 0
    printed_tools: set[str] = set()

    async def _tick() -> bool:
        nonlocal cursor
        events = await store.rows(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id, TaskEvent.id > cursor)
            .order_by(TaskEvent.id)
        )
        for event in events:
            cursor = max(cursor, int(event.id))
            payload = event.payload or {}
            if event.event_type == "message_delta":
                delta = str(payload.get("text") or "")
                if delta:
                    state["text"] += delta
                    if stream:
                        sys.stdout.write(delta)
                        sys.stdout.flush()
            elif event.event_type in {"tool_started", "tool_call_started"}:
                name = str(payload.get("name") or payload.get("tool") or "工具")
                if stream:
                    sys.stdout.write(f"\n  · {name} …\n")
                    sys.stdout.flush()
                printed_tools.add(name)
            elif event.event_type == "message_started" and stream:
                # 新一轮回答开始：给个换行，避免和工具行粘在一起。
                if printed_tools:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
        task = await store.get(Task, task_id)
        state["status"] = task.status if task else ""
        state["error"] = task.error if task else ""
        if task and task.status == "WAITING_USER":
            state["question"] = str((task.waiting or {}).get("question") or "")
        return state["status"] in TERMINAL

    while True:
        done = context.run(_tick())
        if done:
            return state
        # 事件循环跑在另一条线程上，主线程这里可以放心睡。
        time.sleep(POLL_SECONDS)


def _answer_of(context: Context, store, task_id: str) -> str:
    from app.storage import Message

    async def _do():
        return await store.get(Message, f"answer-{task_id}")

    row = context.run(_do())
    return str(row.content) if row is not None else ""


def _one_turn(context: Context, store, runner, conversation_id: str, text: str, *, stream: bool) -> str:
    """跑一轮：提交 → 驱动 → 处理 WAITING_USER → 取最终回答。"""
    task_id = _submit(context, store, runner, conversation_id, text)
    while True:
        state = _drive(context, store, runner, task_id, stream=stream)
        if state["status"] == "WAITING_USER":
            question = state["question"] or "需要你的确认"
            if stream:
                sys.stdout.write(f"\n? {question}\n> ")
                sys.stdout.flush()
            try:
                reply = input()
            except EOFError:
                raise CliError("任务在等你回答，但输入已结束（非交互环境）。") from None
            _resume(context, store, runner, task_id, reply)
            continue
        if state["status"] == "FAILED":
            error = state.get("error") or "任务失败"
            raise CliError(str(error))
        if state["status"] == "CANCELLED":
            raise CliError("任务被取消")
        answer = _answer_of(context, store, task_id) or state["text"]
        return answer


def _resume(context: Context, store, runner, task_id: str, reply: str) -> None:
    """把用户的回答落成消息并让任务继续（`enqueue` 带 resume 载荷）。"""
    from app.storage import Message, Task

    async def _do():
        task = await store.get(Task, task_id)
        if task is not None:
            await store.save(
                Message(conversation_id=task.conversation_id, role="user", content=reply)
            )
        await runner.enqueue(task_id, {"approved": True, "answer": reply})

    context.run(_do())


def run_chat(context: Context, args) -> int:
    prompt = getattr(args, "oneshot", None)
    resume = bool(getattr(args, "continue_session", None))
    if getattr(args, "reasoning", None):
        raise CliError(
            "--reasoning 未实现：Orbit 没有按 provider 翻译推理线格式的那一层（见 MATRIX 4.4）。"
        )
    if getattr(args, "toolsets", None):
        raise CliError("--toolsets 未实现：用 `orbit tools disable <名字>` 收窄暴露面（见 MATRIX 6.2）。")
    if getattr(args, "yolo", False):
        context.note("--yolo 是默认行为（审批模式 off 只拦硬地板），该 flag 只为命令面对齐保留。")

    # 本次运行内的覆盖：直接改内存里的 settings，**不落盘**——一次性的命令行实验
    # 不该把用户的默认配置改掉。
    model = getattr(args, "model", None)
    provider = getattr(args, "provider", None)
    if model or provider:
        from app.providers import PROVIDERS

        updates: dict[str, Any] = {}
        if provider:
            if provider not in PROVIDERS:
                raise CliError(f"未知供应商：{provider}（`orbit model --list` 看全部）")
            profile = PROVIDERS[provider]
            updates["model_provider"] = provider
            updates["model_base_url"] = profile.default_base_url
            updates.setdefault("model", model or profile.default_model)
        if model:
            updates["model"] = model
        context.settings = context.settings.model_copy(update=updates)

    in_dir = getattr(args, "in_dir", None)
    if in_dir:
        import os
        from pathlib import Path

        target = Path(in_dir).expanduser()
        if not target.is_dir():
            raise CliError(f"--in 指定的目录不存在：{target}")
        # 项目上下文（AGENTS.md / ORBIT.md）按 cwd 向上查找，所以切目录要真切。
        os.chdir(target)

    runner, store, summaries = _build_stack(context)
    conversation = _conversation(context, store, resume=resume, title="命令行对话")
    try:
        if prompt:
            answer = _one_turn(context, store, runner, conversation.id, str(prompt), stream=False)
            context.out(answer)
            context.emit({"ok": True, "answer": answer, "conversation_id": conversation.id})
            return 0

        context.out(f"Orbit 命令行会话 · {conversation.id[:8]}（Ctrl+C 或空行退出）")
        context.out()
        while True:
            try:
                line = input("> ")
            except (EOFError, KeyboardInterrupt):
                context.out()
                break
            text = line.strip()
            if not text:
                break
            if text in {"/exit", "/quit", ":q"}:
                break
            try:
                _one_turn(context, store, runner, conversation.id, text, stream=True)
            except CliError as exc:
                context.warn(str(exc))
            context.out()
        return 0
    finally:
        context.run(runner.close())


REGISTRY.add(
    Command(
        name="chat",
        help="在终端里与 agent 对话（不接 MCP；浏览器工具按需启动）",
        handler=run_chat,
        group="run",
        state="done",
        options=[
            # `-z/--oneshot` **不在这里声明**：它挂在全局 parent parser 上，
            # 而 `chat` 的 parser 继承了那个 parent。这里再声明一次会 argparse 冲突
            # （实测：argument -z/--oneshot: conflicting option strings）。
            Option(("--continue",), "接着最近一个会话继续（给值则忽略，仅为命令面对齐）", nargs="?", dest="continue_session"),
            Option(("-m", "--model"), "覆盖本次使用的模型名", metavar="MODEL"),
            Option(("--provider",), "覆盖本次使用的供应商（见 `orbit model --list`）", metavar="PROVIDER"),
            Option(("-t", "--toolsets"), "只开指定工具集（未实现，会报错并给出替代做法）", metavar="TOOLSETS"),
            Option(("--reasoning",), "推理力度（未实现，会报错并说明原因）", metavar="LEVEL"),
            Option(("--in",), "把工作目录切到该路径（影响项目上下文加载）", metavar="DIR", dest="in_dir"),
            Option(("--yolo",), "跳过确认（Orbit 默认即此行为，flag 仅为对齐保留）", action="store_true"),
        ],
    ),
)
