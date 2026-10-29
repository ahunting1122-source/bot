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

    # 把 `clarify` 的投递通道指向控制台（MATRIX 5.13）。
    #
    # **为什么这里要做**：`clarify_payload()` 不知道用户在哪个平台，任务表里也没有
    # 平台字段。所以由宿主在开始驱动任务前声明通道。不声明的话 `clarify` 只返回
    # `needs_user` 载荷、由前端自己渲染——`render_choices` / `send_clarify` 那套
    # 「≤5 个出按钮、>5 个降级编号文本」的规则就永远不会被执行（这正是之前的缺陷：
    # 那两个函数写好了、测过了，唯一调用方却是 `peer_extra` 的手动演示入口）。
    #
    # 控制台没有按钮，`send_clarify` 会自动退回编号文本——**降级路径本身也值得被走一遍**。
    from app import clarify_delivery, platforms

    clarify_delivery.configure(platforms.ConsoleAdapter(), "cli")
    context.note("clarify 投递通道：console/cli（无按钮，自动退回编号文本）")

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
        source = getattr(context, "session_source", "") or "cli"
        # 记进事件：这是 `--source` **真实且可观测**的效果
        # （`orbit plane timeline` 能看到这条任务从哪来）。
        await store.event(task.id, "task_created", goal=text, source=source)
        # **`--source` 不是平台**（MATRIX 1.7 第 42 轮更正）。
        #
        # Hermes 的 `--source` 是「**会话来源标签，用于过滤**」（调研 01:181：
        # 第三方集成用 `tool` 让它不出现在用户会话列表里）。
        # 而 6.4 的 `platform` 决定的是**工具暴露面**——**两个概念**。
        #
        # 上一版把它接到 `platform` 上，后果是：第三方传 `--source tool`
        # 期望隐藏会话，实际只是把 platform 设成 `tool`（没配置 → 工具集不变）
        # → **净效果仍是 0，而 `--help` 在说谎**。
        # 这是「接上了」的第五种假象：**接错概念**。
        await runner.enqueue(task.id, platform="cli")
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


def _apply_flags(context: Context, args) -> dict[str, Any]:
    """把全局运行 flag 落到 settings 与运行时状态上，返回本次运行的额外状态。

    **一律只改内存里的 settings，不落盘**——一次性的命令行实验不该把用户的默认配置
    改掉。这也是 `--ignore-user-config` 能成立的前提：它只是"这次不读"，不是"删掉"。
    """
    import os
    from pathlib import Path

    updates: dict[str, Any] = {}
    state: dict[str, Any] = {"skills": (), "image": "", "max_turns": None, "run_budget": None}

    # ── 配置隔离三兄弟 ────────────────────────────────────────────────────
    # 这三个是排查工具：出问题时第一个问题永远是「是不是我的配置/规则把 agent 带歪了」。
    if getattr(args, "safe_mode", False):
        updates["safe_mode"] = True
        updates["ignore_rules"] = True
        state["ignore_user_config"] = True
        context.note(
            "--safe-mode：不注入 AGENTS.md / ORBIT.md、不读 settings.json 的用户配置。"
            "用于隔离「是不是我的配置坏了」。"
        )
    if getattr(args, "ignore_user_config", False):
        state["ignore_user_config"] = True
        context.note("--ignore-user-config：本次不读 settings.json，改用内置默认值。")
    if getattr(args, "ignore_rules", False):
        updates["ignore_rules"] = True
        context.note("--ignore-rules：本次不注入 AGENTS.md / ORBIT.md。")

    # ── 模型与推理 ────────────────────────────────────────────────────────
    model = getattr(args, "model", None)
    provider = getattr(args, "provider", None)
    if model or provider:
        from app.providers import PROVIDERS

        if provider:
            if provider not in PROVIDERS:
                raise CliError(f"未知供应商：{provider}（`orbit model --list` 看全部）")
            profile = PROVIDERS[provider]
            updates["model_provider"] = provider
            updates["model_base_url"] = profile.default_base_url
            updates.setdefault("model", model or profile.default_model)
        if model:
            updates["model"] = model

    level = getattr(args, "reasoning", None)
    if level:
        from app.reasoning import LEVELS

        normalised = str(level).strip().lower()
        # Hermes 还接受 none / minimal，Orbit 的翻译表只有三档——如实说，别静默降级。
        aliases = {"none": "low", "minimal": "low"}
        normalised = aliases.get(normalised, normalised)
        if normalised not in LEVELS:
            raise CliError(
                f"未知的推理力度：{level}（可选：{', '.join(LEVELS)}；"
                "Hermes 的 none/minimal 在 Orbit 落到 low，因为低档之外没有「完全关闭」的翻译）"
            )
        updates["reasoning_level"] = normalised

    # ── 工具与技能 ────────────────────────────────────────────────────────
    raw_toolsets = getattr(args, "toolsets", None)
    if raw_toolsets:
        from app.toolsets import TOOLSETS, resolve

        wanted = tuple(item.strip() for item in str(raw_toolsets).split(",") if item.strip())
        unknown = [name for name in wanted if name not in TOOLSETS]
        if unknown:
            raise CliError(f"未知工具集：{'、'.join(unknown)}（`orbit tools domains` 看全部）")
        state["toolsets"] = resolve(wanted, platform="cli")
    raw_skills = getattr(args, "skills", None) or ()
    if raw_skills:
        if isinstance(raw_skills, str):
            raw_skills = (raw_skills,)
        state["skills"] = tuple(raw_skills)

    # ── 预算 ──────────────────────────────────────────────────────────────
    max_turns = getattr(args, "max_turns", None)
    if max_turns:
        from app.turn_budgets import resolve as resolve_budgets

        if int(max_turns) <= 0:
            raise CliError(f"--max-turns 必须是正整数：{max_turns}")
        state["max_turns"] = int(max_turns)
        # **必须写进 settings**：`policies.budget.start()` 是通过
        # `turn_budgets.resolve(settings)` 取轮次上限的，只放进 `state` 的话
        # 那个值永远到不了 budget——这正是本轮审计查出的 2.2 虚报的根因。
        updates["max_turns"] = int(max_turns)
        state["budgets"] = resolve_budgets(context.settings).to_dict()
    run_budget = getattr(args, "run_budget", None)
    if run_budget:
        if int(run_budget) <= 0:
            raise CliError(f"--run-budget 必须是正整数秒：{run_budget}")
        # **写 `max_seconds`，不是 `run_budget_seconds`**（MATRIX 1.7）。
        #
        # 原来写的是 `updates["run_budget_seconds"]`，而 `Settings` 里根本没有
        # 这个字段——pydantic 会**静默丢掉**未声明的 kwarg，于是 `--run-budget`
        # **一个字节都没生效**：产品读的是 `settings.max_seconds`
        # （`policies.py` 拿它做时间预算闸门）。
        #
        # 这是「**接上了但没生效**」那一族（与 2.2 / 4.3 / 4.4 / 4.9 同族），
        # 而且是最难发现的一种：**flag 能解析、不报错、也没有任何提示**。
        updates["max_seconds"] = int(run_budget)

    # ── 会话与工作目录 ────────────────────────────────────────────────────
    if getattr(args, "pass_session_id", False):
        state["pass_session_id"] = True
    if getattr(args, "source", None):
        # **--source 接到平台机制上**（MATRIX 6.4 / 1.7）。
        #
        # 原来写的是 updates["session_source"]，而全仓**零读取点**——
        # 又是一个「写了没人读」的装饰 flag。
        # --source 的语义就是「这个任务从哪来」，而 6.4 建的
        # enqueue(..., platform=...) 正好消费这个信息（它决定工具暴露面）。
        # 挂在 context 上而不是 state：_submit() 在另一层，拿不到 state。
        context.session_source = str(args.source).strip()
    if getattr(args, "accept_hooks", False):
        state["accept_hooks"] = True
        context.note("--accept-hooks：本次自动信任新出现的 shell hooks（会写进 hooks 信任表）。")

    if updates:
        context.settings = context.settings.model_copy(update=updates)

    in_dir = getattr(args, "in_dir", None)
    if in_dir:
        target = Path(in_dir).expanduser()
        if not target.is_dir():
            raise CliError(f"--in 指定的目录不存在：{target}")
        # 项目上下文（AGENTS.md / ORBIT.md）按 cwd 向上查找，所以切目录要真切。
        os.chdir(target)

    # ── 隔离工作树 ────────────────────────────────────────────────────────
    if getattr(args, "worktree", False):
        from app.worktree import Worktree, WorktreeError

        try:
            worktree = Worktree(context.settings.data_dir)
            info = worktree.create(f"chat-{os.getpid()}")
        except WorktreeError as exc:
            # 没有 git 仓库时如实失败——静默退回主工作目录会让用户以为自己在隔离环境里。
            raise CliError(f"--worktree 失败：{exc}") from exc
        os.chdir(info.path)
        state["worktree"] = info.path
        context.note(f"--worktree：在隔离工作树里运行（{info.path}），不会动你的主工作目录。")

    # ── 文件系统检查点 ────────────────────────────────────────────────────
    if getattr(args, "checkpoints", False):
        from app.shadow_checkpoint import GitUnavailable, ShadowRepo

        try:
            shadow = ShadowRepo(Path.cwd(), data_dir=context.settings.data_dir)
            if not shadow.available():
                raise GitUnavailable("本机没有可用的 git")
            shadow.init()
            shadow.snapshot("chat --checkpoints 起始快照")
        except GitUnavailable as exc:
            raise CliError(f"--checkpoints 不可用：{exc}") from exc
        state["shadow"] = shadow
        context.note("--checkpoints：已对工作目录做起始快照，破坏性改动前会再快照一次。")

    return state


def _apply_state(context: Context, runner, state: dict[str, Any]) -> None:
    """把 flag 解析出来的状态作用到**这一轮真正会跑的东西**上。

    `--toolsets` 走的是"从注册表里摘掉不属于这些工具集的工具"，而不是"只给模型看
    一部分"——两件事的后果不同：前者模型调不到（干净），后者调了才报错（难看）。
    """
    wanted = state.get("toolsets")
    if wanted:
        from app.toolsets import toolset_of

        allowed = set(wanted)
        registry = runner.registry
        kept: dict[str, Any] = {}
        dropped: list[str] = []
        for name, tool in registry.tools.items():
            if toolset_of(name) in allowed:
                kept[name] = tool
            else:
                dropped.append(name)
        registry.tools.clear()
        registry.tools.update(kept)
        context.note(
            f"--toolsets：只保留 {len(kept)} 个工具"
            f"（摘掉 {len(dropped)} 个：{'、'.join(sorted(dropped)[:6])}"
            f"{'…' if len(dropped) > 6 else ''}）"
        )

    skills = state.get("skills") or ()
    if skills:
        # **不要写 `settings.skills_preload`**：那是 `model_copy(update=...)` 塞进去的
        # 裸属性——pydantic 不校验也不报错，所以它"存在"，但**全仓没有任何地方读它**。
        # 真正的路径是 `state["skills"]` → `_apply_state()` 放进 Context 层。
        # **死写入比不写更糟**：读代码的人会以为那个设置起作用。
        context.note(f"--skills：预载 {len(skills)} 个技能（正文进 Context 层，不进 Stable 层）。")

    # `state["pass_session_id"]` 是唯一真路径（见上面 `_apply_flags` 开头），
    # 不要再往 settings 上写一份——那份没人读。

def run_chat(context: Context, args) -> int:
    # `-q/--query` 与 `-z/--oneshot` 都能给一次性的问题；两者同时给时以前者为准
    # （`--query` 是 Hermes 的正名，`-z` 是 Orbit 早先的写法）。
    prompt = getattr(args, "query", None) or getattr(args, "oneshot", None)
    query_file = getattr(args, "query_file", None)
    if query_file:
        from pathlib import Path as _Path

        path = _Path(query_file).expanduser()
        if not path.is_file():
            raise CliError(f"--query-file 指定的文件不存在：{path}")
        prompt = path.read_text(encoding="utf-8").strip()
        if not prompt:
            raise CliError(f"--query-file 是空的：{path}")
    resume = bool(getattr(args, "continue_session", None)) or bool(getattr(args, "resume", None))
    if getattr(args, "yolo", False):
        context.note("--yolo 是默认行为（审批模式 off 只拦硬地板），该 flag 只为命令面对齐保留。")

    state = _apply_flags(context, args)

    if getattr(args, "tui", False):
        # 有 `--tui` 就走 TUI，而不是假装接受然后开 REPL——那是最容易让人困惑的做法。
        from app.cli.commands.ui_extra import run_tui

        return run_tui(context, args)
    if getattr(args, "dev", False):
        context.note("--dev 只对 Hermes 的 TypeScript TUI 有意义（跳过 dist 直接用 tsx）；Orbit 的 TUI 是 Python，无需编译，该 flag 无效果。")

    # `--quiet` 由**全局框架**的 `Context.quiet` 处理（父解析器上的 `--quiet`），
    # 不要再往 settings 上写一份——那份没人读。
    #
    # `--verbose` 在 `chat` 里**没有实现**：全仓没有任何地方读它。按本文件既有做法
    # （见上面的 `--dev`）如实说出来，而不是静默收下。
    if getattr(args, "verbose", False):
        context.note("--verbose 在 `orbit chat` 里没有实现（不会改变输出）。要更详细的日志：`orbit logs`。")

    runner, store, summaries = _build_stack(context)
    _apply_state(context, runner, state)
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
            # ── 一次性提问 ──
            # --query **不给 -q 短名**：全局 parent parser 上 -q 已经是 --quiet，
            # 再声明一次会让整棵命令树编译不出来（argparse conflicting option string）。
            Option(("--query",), "给一个问题，回答后退出（不给则进 REPL）", metavar="QUERY"),
            Option(("--query-file",), "从文件读那一个问题（省去 shell 转义）", metavar="PATH", dest="query_file"),
            Option(("--image",), "给随问题附一张本地图片（需视觉模型；本机无视觉 provider 时会如实报错）", metavar="PATH"),
            # ── 会话 ──
            Option(("--continue",), "接着最近一个会话继续", nargs="?", dest="continue_session"),
            Option(("--resume", "-r"), "恢复指定会话 id", metavar="SESSION_ID"),
            Option(("--create-if-missing",), "配合 --continue <名字>：没有匹配的会话就新建", action="store_true", dest="create_if_missing"),
            Option(("--no-restore-cwd",), "恢复会话时不切回它记录的目录", action="store_true", dest="no_restore_cwd"),
            Option(("--pass-session-id",), "把会话 id 写进上下文，便于引用与对账", action="store_true", dest="pass_session_id"),
            Option(
            ("--source",),
            "会话来源标签（默认 cli）：**记进任务事件**供排查；"
            "**不做会话列表筛选**（Hermes 的筛选语义需要 Session.source 列，Orbit 未实现）",
            metavar="SOURCE",
        ),
            # ── 模型与推理 ──
            Option(("-m", "--model"), "覆盖本次使用的模型名", metavar="MODEL"),
            Option(("--provider",), "覆盖本次使用的供应商（见 `orbit model --list`）", metavar="PROVIDER"),
            Option(("--reasoning",), "推理力度：low / medium / high（none、minimal 落到 low）", metavar="LEVEL"),
            # ── 工具与技能 ──
            Option(("-t", "--toolsets"), "只开这些工具集（逗号分隔，`orbit tools domains` 看全部）", metavar="TOOLSETS"),
            Option(("-s", "--skills"), "预载技能名（可重复），正文注入 Context 层", metavar="SKILL", action="append"),
            Option(("--accept-hooks",), "自动信任新出现的 shell hooks（写进信任表）", action="store_true", dest="accept_hooks"),
            # ── 预算 ──
            Option(
                ("--max-turns",),
                "本轮最多几个 think→execute 循环（默认取 agent.max_turns = 90；"
                "Hermes 的 CLI 帮助写 500，那是它 CLI 侧的默认，见 MATRIX 2.2）",
                metavar="N",
                dest="max_turns",
            ),
            Option(("--run-budget",), "本轮墙钟预算（秒）", metavar="SECONDS", dest="run_budget"),
            # ── 工作目录与隔离 ──
            Option(("--in",), "把工作目录切到该路径（影响项目上下文加载）", metavar="DIR", dest="in_dir"),
            Option(("--worktree", "-w"), "在隔离的 git worktree 里运行（并行 agent 用）", action="store_true"),
            Option(("--checkpoints",), "运行前对工作目录做一次影子仓库快照（可回滚）", action="store_true"),
            # ── 配置隔离（排查用）──
            Option(("--ignore-user-config",), "本次不读 settings.json，改用内置默认值", action="store_true", dest="ignore_user_config"),
            Option(("--ignore-rules",), "本次不注入 AGENTS.md / ORBIT.md", action="store_true", dest="ignore_rules"),
            Option(("--safe-mode",), "排查模式：同时忽略规则与用户配置", action="store_true", dest="safe_mode"),
            # ── 界面与输出 ──
            Option(("--tui",), "改用 TUI（等价于 `orbit tui`）", action="store_true"),
            Option(("--cli",), "强制用经典 REPL（默认行为，flag 用于显式覆盖 --tui）", action="store_true"),
            Option(("--dev",), "只对 Hermes 的 TypeScript TUI 有意义；Orbit 的 TUI 是 Python，无效果", action="store_true"),
            # -v/--verbose 与 -Q/--quiet **也不在这里声明**：它们已经在全局
            # parent parser 上。重复声明会 argparse 冲突，让整棵命令树编译不出来。
            # 代码里照常读 rgs.verbose / rgs.quiet——值来自全局那一份。
            Option(("--yolo",), "跳过确认（Orbit 默认即此行为，flag 仅为对齐保留）", action="store_true"),
        ],
    ),
)
