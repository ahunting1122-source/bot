"""`dashboard pages` / `dashboard replay` / `tui` / `desktop` 的实现与接线（W12）。

## 接线方式：`register_handler` + 一次"组装后补挂"

声明表（``hermes_parity.py``）里**已有**的路径直接接管，不另立 `Command`：

    register_handler("desktop", run_desktop)

表里**没有**的路径（``dashboard pages`` / ``dashboard replay`` / ``tui``）必须自己挂。
为什么表里没有：Hermes 的 ``dashboard`` 只有 ``register`` 一个子命令，没有"列页面"和
"回放会话"；``tui`` 在 Hermes 里是 ``chat --tui`` 这个 flag，不是独立命令。这两条是
Orbit 自己的东西（MATRIX 16.1 / 16.6 / 1.16），所以由本模块登记。

## 为什么要装一个组装钩子

``commands/__init__.py`` 把功能域模块排在 ``hermes_parity`` **之前**导入，于是本模块
被导入时 ``REGISTRY.get("dashboard")`` 还是 ``None``——``Spec.build`` 还没把表变成
``Command``，想"给 dashboard 加子命令"就没有对象可加。做法照 ``ui.py`` 的
:func:`ui._install_assembly_hook`：包一层 ``_table.register_specs``，等表组装完再补挂。
反过来的导入顺序（先组装后导入本模块）也支持：:func:`_install` 会直接补挂一次。
两条路都幂等，重复导入不会重复登记。

## 为什么不碰 ``serve`` / ``dashboard`` / ``console`` / ``skin``

那四条已被 ``ui.py`` 占用（``_bind("dashboard", run_dashboard)`` 等）。本模块只加
**新路径**：``dashboard pages`` / ``dashboard replay`` 是 ``dashboard`` 的子命令，
与父命令自己的 handler 不冲突——argparse 会把 ``orbit dashboard pages`` 分发到子命令，
``orbit dashboard`` 仍然落到 ``ui.run_dashboard``。

## `desktop` / `gui` 为什么如实报未实现

Orbit 没有 ``apps/desktop``，也没有任何 Electron / Tauri 工程；本机无网络，装不了
node 依赖。硬编一个"正在构建…"的假成功，比直接说"没有"更浪费时间。所以这条命令
**只做一件事**：把 Hermes 那套 flag 的真实语义说清楚，并给出零安装的等价做法
（``orbit serve`` + ``orbit dashboard`` 开浏览器）。状态因此标 ``partial`` 而不是
``done``——handler 存在不等于功能存在。
"""

from __future__ import annotations

from typing import Any

from app import dashboard as dashboard_module
from app import tui as tui_module
from app.cli.commands import _table
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context, truncate
from app.cli.spec import REGISTRY, Command, Option

#: `orbit serve` 的默认地址（与 `ui.DEFAULT_PORT` 一致；这里只用于文案，不 import ui
#: ——`ui.py` 在导入时会装自己的钩子，本模块没有必要把它的副作用再拉一遍）。
DEFAULT_URL = "http://127.0.0.1:9119/"

#: `dashboard pages` 的 `--status` 可选值。
STATUS_CHOICES: tuple[str, ...] = dashboard_module.STATUSES

#: 组装钩子是否已装（幂等标记）。
_ASSEMBLY_HOOK_INSTALLED = False


# ── dashboard pages（MATRIX 16.1）────────────────────────────────────────────


def run_dashboard_pages(context: Context, args) -> int:
    """列出 Hermes dashboard 的 18 个页面，以及每页在 Orbit 的落地情况。"""
    wanted = str(getattr(args, "status", "") or "").strip()
    if wanted and wanted not in STATUS_CHOICES:
        raise CliError(
            f"未知状态 {wanted!r}，可选：{', '.join(STATUS_CHOICES)}",
            code=2,
        )
    report = dashboard_module.report()
    pages = [page for page in dashboard_module.PAGES if not wanted or page.orbit_status == wanted]

    context.out(f"Hermes dashboard 的 {report['total']} 个页面在 Orbit 的落地情况：")
    context.out(
        f"已覆盖 {report['covered']} · 部分覆盖 {report['partial']} · 缺失 {report['missing']}"
        f"（口径：逐页核对 app/main.py 的真实路由，共 {report['main_routes']} 条）"
    )
    context.table(
        ("页面", "用途", "状态", "Orbit 对应 / 缺口"),
        [
            (
                page.name,
                truncate(page.purpose, 40),
                dashboard_module.STATUS_LABELS[page.orbit_status],
                truncate(page.orbit_route or page.gap, 48),
            )
            for page in pages
        ],
    )
    gaps = [page for page in pages if page.orbit_status == "missing"]
    if gaps:
        context.out()
        context.out("缺失的页面到底缺什么：")
        for page in gaps:
            context.out(f"  {page.name}（{page.title}）：{page.gap}")
    notes = [page for page in pages if page.note]
    if notes:
        context.out()
        context.out("口径说明（同名不同义 / 只有一半的地方）：")
        for page in notes:
            context.out(f"  {page.name}：{page.note}")
    if report["unbacked"]:
        # 这条一旦出现就是本模块的 bug：声明了 app/main.py 里没有的路由。
        context.warn(f"登记里声明了不存在的路由：{report['unbacked']}")

    context.emit(
        {
            "ok": True,
            "total": report["total"],
            "covered": report["covered"],
            "partial": report["partial"],
            "missing": report["missing"],
            "filter": wanted,
            "pages": [page.as_dict() for page in pages],
            "unbacked": report["unbacked"],
        }
    )
    return 0


# ── dashboard replay（MATRIX 16.6）───────────────────────────────────────────


def _speed(args) -> float:
    """`--speed`：字符串转浮点。非法值当场报错，不静默退回原速。"""
    raw = getattr(args, "speed", None)
    if raw is None or str(raw).strip() == "":
        return 1.0
    try:
        value = float(str(raw).strip())
    except ValueError as exc:
        raise CliError(f"`--speed` 需要数字（如 1.0 / 2.0），拿到 {raw!r}", code=2) from exc
    if value <= 0:
        raise CliError("`--speed` 必须大于 0（1.0 = 原速，2.0 = 两倍快进）", code=2)
    return value


def _limit(args) -> int | None:
    """`--limit`：字符串转整数。``None`` = 全部帧。"""
    raw = getattr(args, "limit", None)
    if raw is None or str(raw).strip() == "":
        return None
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise CliError(f"`--limit` 需要整数（如 20），拿到 {raw!r}", code=2) from exc
    if value < 1:
        raise CliError("`--limit` 必须 ≥ 1（要全部帧就别传 --limit）", code=2)
    return value


def _offset(ms: int) -> str:
    """毫秒 → 人能读的相对时间（``0.4s`` / ``1m02.5s``）。"""
    total = max(0, ms) / 1000
    if total < 60:
        return f"{total:.1f}s"
    minutes, seconds = divmod(total, 60)
    return f"{int(minutes)}m{seconds:04.1f}s"


def run_dashboard_replay(context: Context, args) -> int:
    """把一个会话的消息 + 任务事件按时间合并后回放出来（只读）。"""
    session_id = str(getattr(args, "session_id", "") or "").strip()
    if not session_id:
        raise CliError("用法：orbit dashboard replay <session_id>（会话 ID 见 `orbit sessions list`）", code=2)
    speed, limit = _speed(args), _limit(args)
    result = context.run(
        dashboard_module.session_replay(context.store, session_id, speed=speed, limit=limit)
    )
    if not result["found"]:
        raise CliError(
            f"找不到会话 {session_id}：库里没有这条 conversations 记录"
            "（`orbit sessions list` 看有哪些）",
            code=1,
        )
    if not result["frames"]:
        context.out(f"会话 {session_id}（{result['title']}）没有任何消息或事件，回放为空。")
        context.emit(result)
        return 0

    context.out(
        f"会话回放 · {result['title'] or session_id} · 共 {result['total_frames']} 帧，"
        f"回放 {result['returned']} 帧（倍速 {result['speed']}，跨度 "
        f"{_offset(result['duration_ms'])}）"
    )
    if result["truncated"]:
        context.out(f"（--limit 截掉了最早的 {result['dropped']} 帧）")
    context.table(
        ("#", "偏移", "等待", "类型", "角色/事件", "内容"),
        [
            (
                str(frame["seq"]),
                _offset(frame["offset_ms"]),
                _offset(frame["delay_ms"]),
                "消息" if frame["kind"] == "message" else "事件",
                frame["label"],
                truncate(frame["text"] or "（空）", 56),
            )
            for frame in result["frames"]
        ],
    )
    context.emit(result)
    return 0


# ── tui（MATRIX 1.16）────────────────────────────────────────────────────────

#: `orbit tui --help` 里要写明的诚实边界（与 `app/tui.py` 的模块 docstring 同一口径）。
TUI_BOUNDARY = (
    "这是「能看能走」的最小终端界面，不是 Hermes 那个完整 TUI："
    "不引第三方 TUI 库（本机无网络、装不了 textual / prompt_toolkit），"
    "三栏只算布局、格子里是占位行，不连数据库也不做流式刷新。"
    "要看真实会话用 `orbit sessions`，要看回放用 `orbit dashboard replay`。"
)

#: 支持的全部按键（`orbit tui --help` 与帮助块共用一份）。
TUI_KEYS = "q 退出 · Tab 切栏 · j/k 上下移动 · ? 帮助"


def _dimension(args, name: str, default: int) -> int:
    """`--width` / `--height`：正整数，非法值当场报错。"""
    raw = getattr(args, name, None)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:
        raise CliError(f"`--{name}` 需要整数，拿到 {raw!r}", code=2) from exc
    if value < 1:
        raise CliError(f"`--{name}` 必须 ≥ 1", code=2)
    return value


def run_tui(context: Context, args) -> int:
    """进终端界面（按键循环）；`--snapshot` 只渲染一帧后退出。"""
    width = _dimension(args, "width", 80)
    height = _dimension(args, "height", 24)
    # 尺寸下限在**进 TUI 之前**挡掉：布局函数会为负宽度报错，但那是在画第一帧时，
    # 用户已经看到半屏乱码了才收到异常。这里提前给一句能读的话。
    if width < tui_module.MIN_WIDTH or height < tui_module.MIN_HEIGHT:
        raise CliError(
            f"终端太小：需要至少 {tui_module.MIN_WIDTH}x{tui_module.MIN_HEIGHT}，"
            f"拿到 {width}x{height}（三栏边框会算成负宽度）",
            code=2,
        )
    app = tui_module.TuiApp(width=width, height=height)
    if getattr(args, "snapshot", False):
        frame = app.frame()
        context.out(frame)
        context.out(f"（--snapshot：只渲染一帧，不进按键循环。按键：{TUI_KEYS}）")
        context.emit({"ok": True, "snapshot": True, "lines": frame.splitlines()})
        return 0
    context.out(TUI_BOUNDARY)
    code = app.run()
    context.emit({"ok": code == 0, "frames": len(app.frames), "active": app.state.active})
    return code


# ── desktop / gui：如实报未实现 ──────────────────────────────────────────────

#: Hermes `desktop` 的 flag 与它在 Orbit 为什么空转（抄自
#: `handoff/hermes-parity/research/_raw/cmd-74-desktop.txt`）。
DESKTOP_FLAGS: tuple[tuple[str, str], ...] = (
    ("--source", "用 `electron .` 跑源码而不是打包版——Orbit 没有 apps/desktop"),
    ("--build-only", "只构建不启动——没有可构建的桌面产物"),
    ("--fake-boot", "注入确定的启动延迟以验证启动画面——没有启动画面"),
    ("--ignore-existing", "忽略 PATH 上已有的 hermes CLI——Orbit 的后端解析不走这条"),
    ("--hermes-root", "覆盖源码根目录（HERMES_DESKTOP_HERMES_ROOT）——Orbit 无此概念"),
    ("--cwd", "桌面会话的初始项目目录（HERMES_DESKTOP_CWD）——无桌面会话"),
    ("--skip-build", "跳过 npm install/打包，直接跑已有产物——没有产物"),
    ("--force-build", "强制全量重建——没有构建步骤"),
    ("--setup-tcc-identity", "macOS 专用：建自签名证书让 TCC 授权扛过重建——非 macOS，无此问题"),
    ("--identity", "自签名证书名——同上"),
)


def desktop_message() -> str:
    """`desktop` / `gui` 的如实说明。单独抽成函数，测试直接断言措辞。"""
    flags = "\n".join(f"  {flag:<22}{why}" for flag, why in DESKTOP_FLAGS)
    return (
        "`orbit desktop` 未实现：Orbit 没有原生桌面壳。\n"
        "Hermes 的 desktop 是一个 Electron 应用（apps/desktop：装 node 依赖 → 打包 → 启动），"
        "Orbit 既没有 apps/desktop，也没有任何 Electron / Tauri 工程；本机无网络，"
        "装不了 node 依赖，所以这条命令无法「构建后启动」。\n"
        "\n"
        "等价做法（浏览器面板，功能等价、零安装）：\n"
        "  1) orbit serve --host 127.0.0.1 --port 9119   # 起后端 + 预构建的前端 dist\n"
        f"  2) orbit dashboard                            # 探测端口并打开浏览器（{DEFAULT_URL}）\n"
        "  只想要「一个窗口」的话，用浏览器的应用模式：\n"
        "     msedge --app=http://127.0.0.1:9119/    或    chrome --app=http://127.0.0.1:9119/\n"
        "\n"
        "Hermes 的那些 flag 在 Orbit 全是空转（不是「没实现细节」，是没有对应物）：\n"
        f"{flags}"
    )


def run_desktop(context: Context, args) -> int:
    """`desktop` / `gui`：如实报未实现，并给出零安装的等价做法。"""
    raise CliError(desktop_message(), code=3)


# ── 接线 ────────────────────────────────────────────────────────────────────


def _bind(path: str, handler: Any, *, state: str = "done") -> None:
    """登记某条路径的实现；表已组装时顺手填到 ``Command`` 上。

    与 ``ui.py::_bind`` 同一套做法，区别只有一处：``register_handler`` 前先查
    ``HANDLERS``，于是"导入两次""钩子与直接补挂各跑一次"都不会重复登记报错。
    """
    key = " ".join(path.split())
    if key not in _table.HANDLERS:
        register_handler(key, handler)
    head, _, leaf = key.partition(" ")
    parent = REGISTRY.get(head)
    if parent is None:
        return
    target = parent.child(leaf) if leaf else parent
    if target is not None:
        target.handler = handler
        target.state = state


def _pages_command() -> Command:
    return Command(
        name="pages",
        help="列出 Hermes dashboard 的 18 个页面及 Orbit 的覆盖情况",
        handler=run_dashboard_pages,
        group="run",
        parity="P2",
        hermes="（Hermes dashboard 无此子命令：页面清单只存在于前端路由表）",
        state="done",
        needs_settings=False,
        options=[
            # 刻意**不**挂 argparse 的 `choices`：那样非法值会以英文 usage 错误 +
            # SystemExit 结束，脚本拿到的是异常而不是退出码，人也读不到"可选哪些"。
            # 校验放在 handler 里，走 CliError → 中文提示 + 返回码 2。
            Option(
                ("--status",),
                "只看某一种覆盖状态（covered / partial / missing）",
                metavar="STATE",
            )
        ],
    )


def _replay_command() -> Command:
    return Command(
        name="replay",
        help="把一个会话的消息 + 任务事件按时间回放（MATRIX 16.6）",
        handler=run_dashboard_replay,
        group="run",
        parity="P2",
        hermes="（Hermes 无此子命令：sessions 只有列表与导出）",
        state="done",
        # `needs_store=False` 是刻意的：`main` 会在 handler 之前预检数据库，那样
        # `--speed 0` 这种"参数先错"的情况会被"数据库不存在"盖住。改成 handler 里
        # 先校验参数、再取 `context.store`（取的时候照样给出可读的缺库提示）。
        needs_store=False,
        options=[
            Option(
                ("session_id",),
                "会话 ID（`orbit sessions list` 可查）",
                # 省略时走 handler 的中文用法提示，而不是 argparse 的英文 usage 错误。
                nargs="?",
            ),
            Option(
                ("--speed",),
                "回放倍速（1.0 = 原速，2.0 = 两倍快进；只影响 delay，不改变顺序）",
                default="1.0",
                metavar="SPEED",
            ),
            Option(
                ("--limit",),
                "只回放最近 N 帧（省略 = 全部）",
                metavar="N",
            ),
        ],
    )


def _tui_command() -> Command:
    return Command(
        name="tui",
        help="最小终端界面：三栏布局 + q/Tab/j/k/? 按键（不是 Hermes 那个完整 TUI）",
        handler=run_tui,
        group="run",
        parity="P2",
        hermes="chat --tui",
        state="done",
        needs_settings=False,
        description=TUI_BOUNDARY,
        epilog=f"按键：{TUI_KEYS}。布局：左=会话列表、中=对话、右=状态/工具轨迹。",
        options=[
            Option(("--width",), "画布宽度（列，默认 80；最小 32）", metavar="N"),
            Option(("--height",), "画布高度（行，默认 24；最小 8）", metavar="N"),
            Option(
                ("--snapshot",),
                "只渲染一帧就退出（给脚本/测试用，不进按键循环）",
                action="store_true",
            ),
        ],
    )


def mount_dashboard_subcommands() -> list[str]:
    """给 `dashboard` 补 `pages` / `replay` 两个子命令（**幂等**）。"""
    parent = REGISTRY.get("dashboard")
    if parent is None:
        return []
    mounted: list[str] = []
    for factory in (_pages_command, _replay_command):
        command = factory()
        if parent.child(command.name) is not None:
            continue
        parent.subcommands.append(command)
        mounted.append(command.name)
    return mounted


def mount_tui() -> bool:
    """登记顶层 `tui`（表里没有这条，只能自己加）。**幂等**。"""
    if REGISTRY.get("tui") is not None:
        return False
    REGISTRY.add(_tui_command())
    return True


def _install_assembly_hook() -> bool:
    """在「表组装」这一个缝上补挂子命令（照 ``ui.py::_install_assembly_hook``）。"""
    global _ASSEMBLY_HOOK_INSTALLED
    if _ASSEMBLY_HOOK_INSTALLED:
        return False
    original = _table.register_specs

    def register_specs(specs: Any) -> list[str]:
        skipped = original(specs)
        _mount_everything()
        return skipped

    _table.register_specs = register_specs
    _ASSEMBLY_HOOK_INSTALLED = True
    return True


def _mount_everything() -> None:
    """把本模块负责的路径一次性接上。两条导入顺序下都成立、都可重复调用。"""
    mount_tui()
    mount_dashboard_subcommands()
    _bind("dashboard pages", run_dashboard_pages)
    _bind("dashboard replay", run_dashboard_replay)
    # `desktop` 标 partial 而不是 done：handler 存在，但它只报"没有桌面壳"。
    _bind("desktop", run_desktop, state="partial")


_install_assembly_hook()
_mount_everything()
