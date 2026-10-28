"""cron 的补充命令面：平台投递、级联紧急停止、平面事件查询。

## 为什么是"补充模块"而不是改 ``cron.py``

``app/cli/commands/cron.py`` 是本波次之前就存在的实现（``cron create/edit/...``
12 个子命令），它登记的是一个**完整的** ``cron`` Command 对象。本轮要加的四条
（``cron deliver`` / ``cron pause-all`` / ``cron resume-all`` / ``cron events``）
在声明表里没有位置，而动 ``cron.py`` 会与本轮"只新建"的边界冲突。所以照仓库既有
做法分两步接线（与 ``session_extra.py`` / ``gateway.py`` 的 ``_bind`` 同一套）：

1. :func:`register_handler` 登记"表里已有的路径"（顶层 ``pause`` / ``resume``）；
2. 往**已登记的** ``cron`` 节点上追加 ``Command``（表里没有这四条子命令）。

两步都做是刻意的：``register_handler`` 只在 ``register_specs`` 组装**之前**生效，
而本模块可能在任何导入顺序下被导入。只做一步，就会有一半场景跑在"计划波次"的
占位实现上。

``--status`` 这个选项还要多一层：它不在声明表里，而表是禁改区，只能在**已构建的**
``pause`` Command 上追加 ``Option``。若本模块被排在 ``hermes_parity`` 之前导入，
那时节点还不存在——所以额外挂了一次"表组装完就补上"的钩子（见
:func:`_install_deferred_fixup`）。**推荐**的接线位置是 ``__init__.py`` 的第二组
（``hermes_parity`` 之后，与 ``session_extra.py`` 同处），那样两个钩子都不必依赖。

## 四条命令各自解决什么

- ``cron deliver``：**手动重投**。任务跑了但群里没收到（网关当时停着 / 网络抖了一下），
  这时不该改调度再等下一轮，而要能立刻把上一次的输出再送一遍。走的是与自动投递
  **同一条** ``cron.deliver_to`` → ``gateway.deliver`` 路径，账本也因此只有一个答案。
- ``cron pause-all`` / ``cron resume-all``：级联紧急停止的显式子命令。``orbit pause``
  是同一件事的顶层入口；两条路径都保留，是因为"在 cron 语境下想停 cron"是最自然的
  心智模型，而"全局停"是应急时的第一反应——两处都指向同一个 :func:`cron.pause_all`。
- ``cron events``：把统一平面上属于 cron 的事件按 ``cron:<job_id>`` 捞出来。
  这是写侧统一（MATRIX 10.1）之后**唯一**能验证"它真的写进去了"的入口。

## 诚实边界

``cron events`` 读的是 ``sessions/plane.jsonl``（JSONL 追加日志），不是数据库表。
``orbit plane timeline`` 会把 plane / messages / task_events 三源合并，而这里只报
cron 那一源——因为 "cron 有没有写平面" 正是本命令要回答的问题，混进别的来源就答不清了。
"""

from __future__ import annotations

import argparse
from typing import Any

from app import cron
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option
from app.session_plane import SOURCE_KEY_CRON, SessionPlane, plane_session_key

# ── 小工具 ──────────────────────────────────────────────────────────────────


def _store(context: Context) -> cron.CronStore:
    return cron.CronStore(context.data_dir)


def _text(args: argparse.Namespace, name: str) -> str:
    return str(getattr(args, name, "") or "")


def _limit(args: argparse.Namespace, default: int = 20) -> int:
    """``--limit`` 的解析与夹取。上限 500：平面是流水账，刷几千行没人读得下去。"""
    raw = getattr(args, "limit", None)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise CliError(f"--limit 只接受整数：{raw!r}") from exc
    return max(1, min(500, value))


def _find_job(store: cron.CronStore, job_id: str) -> cron.Job:
    for job in store.load_jobs():
        if job.id == job_id:
            return job
    raise CliError(f"没有这个任务：{job_id}（`orbit cron list --all` 看全部）")


# ── cron deliver ────────────────────────────────────────────────────────────


def run_deliver(context: Context, args: argparse.Namespace) -> int:
    """``orbit cron deliver <job_id>``：把任务最近一次的输出（或 ``--text``）投出去。

    三种输入来源，按优先级：``--text`` > 任务最近一次运行的 ``output`` > 报错。
    为什么不默认"跑一次任务再投"：跑任务要模型/脚本、要预算，而这条命令的用途是
    **补投**——重跑一遍会产生新的副作用（再发一次邮件、再写一次文件），
    那正是运维最不想要的事。
    """
    store = _store(context)
    job = _find_job(store, _text(args, "job_id"))
    body = _text(args, "text")
    run_id = ""
    if not body.strip():
        records = [
            item
            for item in store.load_runs()
            if str(item.get("job_id") or "") == job.id
        ]
        if not records:
            raise CliError(
                f"{job.id} 还没有执行记录，没有可补投的输出。"
                "用 --text 直接给一段文本，或先 `orbit cron run "
                f"{job.id}` 让下一轮 tick 跑一次。"
            )
        body = str(records[-1].get("output") or "")
        # 补投沿用**那一次运行**的 run_id：账本里因此能对上"这条消息是哪一轮的产物"。
        run_id = str(records[-1].get("run_id") or "")
    if not body.strip():
        raise CliError(
            f"{job.id} 最近一次运行的输出是空的（空输出按 Hermes 语义不投递）。"
            "用 --text 显式给一段文本。"
        )

    raw_target = _text(args, "to").strip() or job.deliver
    try:
        mode, platform, channel = cron.parse_deliver(raw_target)
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    if mode != "platform":
        raise CliError(
            f"投递目标 {raw_target!r} 不是平台投递（模式 {mode}）。"
            "`cron deliver` 只做平台投递；写文件请用 `--deliver file:<路径>` 的调度，"
            "或直接把输出重定向到文件。"
        )

    result = cron.deliver_to(
        platform,
        channel,
        body,
        data_dir=context.data_dir,
        message_key=f"cron:{job.id}:{run_id}" if run_id else f"cron:{job.id}:manual",
    )
    landing = f"{platform}:{channel or 'home'}"
    if not result.ok:
        raise CliError(
            f"投递到 {landing} 失败：{result.error or '未给原因'}"
            "（账本见 <data_dir>/gateway/deliveries.jsonl）"
        )
    context.out(
        f"已投递 {job.id} → {landing}（尝试 {result.attempts} 次，"
        f"message_id={result.message_id or '-'}）"
    )
    context.emit(
        {
            "ok": True,
            "job_id": job.id,
            "run_id": run_id,
            "platform": platform,
            "channel": channel,
            "message_id": result.message_id,
            "attempts": result.attempts,
        }
    )
    return 0


# ── 级联紧急停止 ────────────────────────────────────────────────────────────


def _print_status(context: Context, status: cron.CascadeStatus) -> None:
    """把三个子系统的状态摆成一张表。**逐个报**而不是只给一个布尔：
    紧急停止最怕的不是没停，而是"以为停了、其实只停了一半"。"""
    rows = [
        (
            name,
            "已暂停" if state.get("paused") else "正常",
            str(state.get("detail") or ""),
            str(state.get("path") or "-"),
        )
        for name, state in status.subsystems.items()
    ]
    context.table(("子系统", "状态", "说明", "哨兵 / 登记"), rows)


def run_pause(context: Context, args: argparse.Namespace) -> int:
    """顶层 ``orbit pause``：级联紧急停止（cron + gateway + 正在跑的任务）。

    ``--status`` 只读不写：先看清"现在停了什么"，再决定要不要停——避免在排查时
    又一次把系统按下去。
    """
    if bool(getattr(args, "status", False)):
        status = cron.cascade_status(context.data_dir)
        context.out(f"紧急停止状态：{'已暂停' if status.paused else '未暂停'}")
        context.out()
        _print_status(context, status)
        if status.paused:
            context.out()
            context.out("解除：orbit resume")
        context.emit(status.as_dict())
        return 0

    reason = _text(args, "reason").strip() or "orbit pause"
    status = cron.pause_all(context.data_dir, reason=reason)
    context.out("已级联紧急停止：")
    context.out()
    _print_status(context, status)
    context.out()
    context.out(
        "说明：正在跑的任务**只标记不强杀**（执行体在别的进程里，跨进程强杀的风险更高）。"
        "解除：orbit resume"
    )
    context.emit(status.as_dict())
    return 0


def run_resume(context: Context, args: argparse.Namespace) -> int:
    """顶层 ``orbit resume``：解除级联紧急停止。"""
    status = cron.resume_all(context.data_dir)
    context.out("已解除紧急停止：")
    context.out()
    _print_status(context, status)
    context.emit(status.as_dict())
    return 0


def run_pause_all(context: Context, args: argparse.Namespace) -> int:
    """``orbit cron pause-all``：与顶层 ``orbit pause`` 完全同一件事。

    两条入口都保留：cron 语境下想停 cron 是最自然的心智模型，而应急时第一反应是
    "全局停"。两处都指向 :func:`app.cron.pause_all`，不会出现"停了一半"的分叉。
    """
    return run_pause(context, args)


def run_resume_all(context: Context, args: argparse.Namespace) -> int:
    """``orbit cron resume-all``：与顶层 ``orbit resume`` 同一件事。"""
    return run_resume(context, args)


# ── cron events ─────────────────────────────────────────────────────────────


def _event_text(event: dict[str, Any]) -> str:
    """一行摘要：把事件里最有信息量的字段挑出来，而不是整段 JSON 糊上去。"""
    pieces: list[str] = []
    for key in ("ok", "error", "delivered", "duration_ms", "unavailable", "cancel_requested"):
        if key in event and event[key] not in (None, "", False):
            pieces.append(f"{key}={event[key]}")
    if "file_scope" in event:
        pieces.append(f"file_scope={event.get('file_scope')}")
    if "approval_mode" in event:
        pieces.append(f"approval_mode={event.get('approval_mode')}")
    return " ".join(pieces)


def run_events(context: Context, args: argparse.Namespace) -> int:
    """``orbit cron events``：读统一平面上属于 cron 的事件。

    ``--job-id`` 按 ``cron:<job_id>`` 精确过滤；不给则列出所有 ``cron:`` 开头的会话
    （``--session`` 可显式指定一个会话名，用于查非 cron 来源的事件）。
    """
    plane = SessionPlane(context.data_dir)
    session = _text(args, "session").strip()
    job_id = _text(args, "job_id").strip()
    if not session and job_id:
        session = plane_session_key(SOURCE_KEY_CRON, job_id)
    events = plane.log.read_all()
    if session:
        events = [item for item in events if str(item.get("session_id") or "") == session]
    else:
        prefix = f"{SOURCE_KEY_CRON}:"
        events = [
            item for item in events if str(item.get("session_id") or "").startswith(prefix)
        ]
    kind = _text(args, "kind").strip()
    if kind:
        events = [item for item in events if str(item.get("kind") or "") == kind]
    events = events[-_limit(args) :]
    if not events:
        where = session or f"{SOURCE_KEY_CRON}:*"
        context.out(f"没有事件（会话 {where}，平面 {plane.path}）。")
        context.emit({"ok": True, "events": [], "session": session, "path": str(plane.path)})
        return 0
    context.table(
        ("#", "时间", "事件", "会话", "摘要"),
        [
            (
                str(item.get("id") or ""),
                str(item.get("ts") or ""),
                str(item.get("kind") or ""),
                str(item.get("session_id") or ""),
                _event_text(item)[:70],
            )
            for item in events
        ],
    )
    context.emit(
        {
            "ok": True,
            "session": session,
            "path": str(plane.path),
            "events": events,
        }
    )
    return 0


# ── 接线 ────────────────────────────────────────────────────────────────────


def _bind(path: str, handler: Any) -> None:
    """接**表里已有**的路径：``register_handler`` + 回填已构建的 ``Command``。

    两步缺一不可（见模块 docstring）：前者管"表还没组装"，后者管"表已经组装完"。
    """
    key = " ".join(path.split())
    from app.cli.commands._table import HANDLERS

    if key not in HANDLERS:
        register_handler(key, handler)
    head, _, leaf = key.partition(" ")
    parent = REGISTRY.get(head)
    target = parent.child(leaf) if (parent is not None and leaf) else parent
    if target is not None:
        target.handler = handler
        target.state = "done"


def _install_pause_option() -> None:
    """给表里的 ``pause`` 补 ``--status``。

    声明表是禁改区，所以只能往**已构建的** ``Command`` 上追加 ``Option``
    （``build_parser`` 每次从 ``REGISTRY`` 重新编译，追加即时生效）。
    表还没组装时这里是空操作，由 :func:`_install_deferred_fixup` 兜住。
    """
    command = REGISTRY.get("pause")
    if command is None:
        return
    flags = {flag for option in command.options for flag in option.flags}
    if "--status" in flags:
        return
    command.options.append(
        Option(("--status",), "只查看当前被暂停的子系统，不做任何修改", action="store_true")
    )


def _install_deferred_fixup() -> None:
    """表还没组装时，等它组装完再补 ``--status``。

    为什么要这一步：本模块可能被排在 ``hermes_parity`` **之前**导入（那时表里的
    ``pause`` 节点还不存在），也可能排在**之后**（``session_extra.py`` 就是那么放的）。
    两种顺序都必须能补上 ``--status``——否则 ``orbit pause --status`` 在其中一种接线
    方式下会直接变成 argparse 的 "unrecognized arguments"，而那种失败只在接线方式变化
    时出现，最难查。

    手段是给 ``Registry.add`` 套一层**只做这一件事**的钩子：``register_specs``
    组装完表之后一定会走 ``REGISTRY.add(*fresh)``，那是唯一可靠的"表已就绪"信号。
    刻意不 import ``hermes_parity``：那样会把整张表提前组装，后面才导入的功能域模块
    （``tui`` / ``terminal`` / 插件等）就会撞上"命令重复登记"——那是整条 CLI 起不来
    级别的事故，比少一个 ``--status`` 严重得多。
    """
    from app.cli.spec import Registry

    # 用 Any 视图操作类对象：给一个类**追加方法**不是常规写法，显式 Any 比逐个压掉
    # 类型告警更能说明"这里是有意为之的运行时补丁"。
    registry_type: Any = Registry
    if getattr(registry_type, "_cron_extra_fixup", False):
        return
    original = registry_type.add

    def patched(self: Any, *commands: Any) -> Any:
        result = original(self, *commands)
        _install_pause_option()
        return result

    registry_type.add = patched
    registry_type._cron_extra_fixup = True


def _attach(name: str, command: Command) -> Command:
    """把表里**没有**的子命令挂到已登记的 ``cron`` 节点上（与 ``session_extra._attach`` 同理）。

    ``build_parser`` 每次都从 ``REGISTRY`` 重新编译，所以往 ``subcommands`` 追加即时生效。
    重复调用（模块被重复导入）只更新 handler，不会挂出两条同名子命令。
    """
    parent = REGISTRY.get("cron")
    if parent is None:  # pragma: no cover - 走到这里说明注册表被清过
        raise RuntimeError("cron 命令未登记：cron_extra 无法挂载子命令")
    existing = parent.child(name)
    if existing is not None:
        existing.handler = command.handler
        existing.options = command.options
        existing.state = command.state
        return existing
    parent.subcommands.append(command)
    return command


def _bind_cron_path(path: str, handler: Any) -> None:
    """给新挂的子命令补一条 ``register_handler`` 记录。

    表里目前没有这四条路径，登记是**为将来留位**：若某天声明表补上
    ``cron deliver``，``register_specs`` 组装时会直接认这个 handler，而不是给出
    "计划波次"的占位实现。重复登记会抛错，所以先查 ``HANDLERS``。
    """
    from app.cli.commands._table import HANDLERS

    key = " ".join(path.split())
    if key not in HANDLERS:
        register_handler(key, handler)


def _install() -> None:
    """接线：顶层 pause/resume 走表里已有路径，其余四条追加到 ``cron`` 下。"""
    # cron 必须先登记：本模块可能排在 ``cron`` 之前被导入，而挂子命令要求父节点已存在。
    # 显式 import 一次是幂等的（模块缓存），也不会提前组装声明表。
    import app.cli.commands.cron  # noqa: F401

    _install_deferred_fixup()
    _bind("pause", run_pause)
    _bind("resume", run_resume)
    # ``--status`` 不在声明表里（表是禁改区）：表已组装就地追加，没组装则由
    # ``_install_deferred_fixup`` 在组装完成后补上。"只读状态"必须能在停之前查，
    # 否则排查时又要按一次停止键。
    _install_pause_option()

    _bind_cron_path("cron deliver", run_deliver)
    _attach(
        "deliver",
        Command(
            name="deliver",
            help="把任务最近一次的输出（或 --text）投递到平台",
            handler=run_deliver,
            group="run",
            parity="P1",
            state="done",
            needs_settings=False,
            options=[
                Option(("job_id",), "要投递的任务 ID", nargs="?"),
                Option(
                    ("--to",),
                    "覆盖投递目标，写法 platform:<平台>[:<频道>]",
                    metavar="TARGET",
                ),
                Option(("--text",), "直接投这段文本（默认取最近一次运行的输出）", metavar="TEXT"),
            ],
        ),
    )

    _bind_cron_path("cron pause-all", run_pause_all)
    _attach(
        "pause-all",
        Command(
            name="pause-all",
            help="级联紧急停止（cron + gateway + 正在跑的任务），等同 orbit pause",
            handler=run_pause_all,
            group="run",
            parity="P1",
            state="done",
            options=[
                Option(("--reason",), "记录停止原因（写进哨兵文件）", metavar="REASON"),
                Option(("--status",), "只查看当前状态，不做任何修改", action="store_true"),
            ],
        ),
    )

    _bind_cron_path("cron resume-all", run_resume_all)
    _attach(
        "resume-all",
        Command(
            name="resume-all",
            help="解除级联紧急停止，等同 orbit resume",
            handler=run_resume_all,
            group="run",
            parity="P1",
            state="done",
            needs_settings=False,
        ),
    )

    _bind_cron_path("cron events", run_events)
    _attach(
        "events",
        Command(
            name="events",
            help="查看 cron 写进统一 session 平面的事件（cron:<job_id>）",
            handler=run_events,
            group="run",
            parity="P1",
            state="done",
            needs_settings=False,
            options=[
                Option(("--job-id",), "只看某个任务（会话 cron:<job_id>）", metavar="JOB_ID"),
                Option(("--session",), "显式指定会话名（覆盖 --job-id）", metavar="SESSION"),
                Option(("--kind",), "按事件类型过滤（如 cron.run.failed）", metavar="KIND"),
                Option(("--limit",), "显示条数（1-500，默认 20）", default=20, metavar="LIMIT"),
            ],
        ),
    )


_install()
