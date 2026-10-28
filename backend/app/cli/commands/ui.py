"""`serve` / `dashboard` / `console` / `skin` 四条命令的实现与接线。

## 接线方式（`_table.register_handler`）

这四条命令**已经在声明表里**（`hermes_parity.py`），所以本模块不另立 `Command`，
只声明"这些路径我接了"（照 `security.py` / `sessions.py` 的做法）：

    register_handler("serve", run_serve)
    register_handler("skin list", run_skin_list)
    ...

声明表里**缺的**选项与子命令（`console --allow` / `console --exec` /
`skin show` / `skin reset` / `monitoring metrics`）由 :func:`_install_assembly_hook`
在"表组装"那一刻补挂——那是本模块排在 `hermes_parity` 之前时唯一能拿到的
"之后"时机（同样的手法见 `security.py::mount_audit_options` 的注释）。

## `serve` 只做启动器

**不复制** `app/main.py` 的任何东西：它只检查端口、打印访问地址、然后把
`"app.main:app"` 交给 `uvicorn.run`。`app/main.py` 是禁改文件，这里只**调用**它。

"端口检查 + 参数拼装"被抽成纯函数 :func:`serve_plan`，于是测试不需要真起服务
（本机无网络、也不该在单测里占端口）。同理 `--insecure` 这类 NO-OP 的提示也走
纯函数 :func:`serve_plan` 的 ``notices`` 字段，可以被断言。

## 为什么 `--insecure` 是 NO-OP

Hermes 的原文：*DEPRECATED / NO-OP … a public bind always requires an auth
provider*。Orbit 更彻底：`app/main.py` 的 `local_session` 中间件只接受
`localhost` / `127.0.0.1` / `testserver` 作为 Host，非回环地址的 `/api/*` 一律
403。所以"关掉鉴权"这件事在 Orbit 根本没有对应物——如实告诉用户这个 flag
无效果，比假装接受它要诚实得多。
"""

from __future__ import annotations

import socket
import sys
import webbrowser
from typing import Any

from app import console as console_module
from app import skin as skin_module
from app.cli.commands import _table
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option

#: 默认监听地址与端口。9119 对齐 Hermes（`--port` 的默认值）。
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 9119

#: `app/main.py` 里 FastAPI 实例的位置（`uvicorn` 的 import string）。
ASGI_TARGET = "app.main:app"


# ── serve：纯函数计划 ───────────────────────────────────────────────────────


def port_in_use(host: str, port: int) -> bool:
    """端口是否已被占用。用"能不能 bind 上"判定，不连、不发包。

    **不设 SO_REUSEADDR**：Windows 上那个选项允许两个 socket 绑同一端口，
    探针会永远报"空闲"——一个永远回答"空闲"的健康检查比没有更糟。
    """
    if port <= 0:
        return False
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        probe = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        probe.bind((host, port))
    except OSError:
        return True
    finally:
        probe.close()
    return False


def is_loopback(host: str) -> bool:
    value = (host or "").strip().lower()
    return value in {"localhost", "::1", "127.0.0.1"} or value.startswith("127.")


def serve_plan(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    *,
    insecure: bool = False,
    skip_build: bool = False,
    isolated: bool = False,
    probe: bool = True,
) -> dict[str, Any]:
    """把"能不能起、起在哪儿、怎么起"算成一个**纯数据字典**（不启动任何服务）。

    返回形状（测试直接断言这些键）::

        {
          "host": "127.0.0.1",       # 归一化后的监听地址
          "port": 9119,              # 0 = 由系统分配
          "auto_port": False,
          "url": "http://127.0.0.1:9119/",
          "target": "app.main:app",
          "log_level": "info",
          "loopback": True,          # 是否回环地址（非回环时 /api 会 403）
          "available": True,         # 端口是否可用
          "reason": "",              # available=False 时的原因
          "notices": [...],          # NO-OP flag 的如实提示
          "uvicorn": {"host": ..., "port": ..., "log_level": ...},  # 直接 **kwargs
        }

    ``probe=False`` 时跳过端口探测（只拼参数、不碰 socket），供纯逻辑测试使用。
    """
    clean_host = (host or "").strip() or DEFAULT_HOST
    clean_port = int(port)
    notices: list[str] = []
    if insecure:
        notices.append(
            "`--insecure` 在 Orbit 无效果：本机后端只接受回环地址的 /api 请求"
            "（app/main.py 的 local_session 中间件），没有「关掉鉴权」这个开关"
        )
    if skip_build:
        notices.append(
            "`--skip-build` 无效果：Orbit 的前端是预构建的 `frontend/dist`，没有 npm 构建步骤"
        )
    if isolated:
        notices.append(
            "`--isolated` 无效果：Orbit 只有一个机器级数据目录（profile 隔离尚未接线）"
        )
    if not is_loopback(clean_host):
        notices.append(
            f"绑定 {clean_host} 时 app/main.py 的 local_session 中间件会拒绝所有 /api 请求（403）"
            "——Orbit 只支持本机访问，要远程用请起隧道（如 ssh -L）而不是改绑定地址"
        )
    available, reason = True, ""
    if probe and port_in_use(clean_host, clean_port):
        available = False
        reason = (
            f"端口 {clean_port} 已被占用（{clean_host}:{clean_port}）；"
            "换一个 `--port`，或用 `orbit serve --status` 看占用情况"
        )
    shown = f"http://{clean_host}:{clean_port if clean_port > 0 else '<系统分配>'}/"
    return {
        "host": clean_host,
        "port": clean_port,
        "auto_port": clean_port == 0,
        "url": shown,
        "target": ASGI_TARGET,
        "log_level": "info",
        "loopback": is_loopback(clean_host),
        "available": available,
        "reason": reason,
        "notices": notices,
        "uvicorn": {"host": clean_host, "port": clean_port, "log_level": "info"},
    }


def _print_notices(context: Context, plan: dict[str, Any]) -> None:
    for notice in plan["notices"]:
        context.out(f"注意：{notice}")


def _host(args) -> str:
    return str(getattr(args, "host", None) or DEFAULT_HOST)


def _port(args) -> int:
    """`--port` 缺省或非法都退回 9119。`0` 是合法值（系统分配），不能被 `or` 吃掉。"""
    raw = getattr(args, "port", None)
    if raw is None:
        return DEFAULT_PORT
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_PORT


def _status_report(context: Context, host: str, port: int) -> None:
    """`--status`：如实报告"端口上有没有人在听"和"我们能不能列出进程"。"""
    busy = port_in_use(host, port)
    context.table(
        ("项", "值"),
        [
            ("监听地址", f"{host}:{port}"),
            ("端口状态", "已有服务在监听" if busy else "空闲"),
            (
                "进程登记表",
                "无——Orbit 不起守护进程，`serve` 是前台进程；"
                "停服务请用 Ctrl-C，或按端口查 PID（netstat -ano / Get-NetTCPConnection）",
            ),
        ],
    )


def run_serve(context: Context, args) -> int:
    host, port = _host(args), _port(args)
    if getattr(args, "status", False):
        _status_report(context, host, port)
        return 0
    if getattr(args, "stop", False):
        raise CliError(
            "`orbit serve --stop` 未实现：Orbit 没有 web 服务的进程登记表，"
            "没法可靠地停掉「别人的」后端（按 PID 杀可能杀错进程）。"
            "请用 Ctrl-C 停掉前台进程，或按端口查 PID 后自行结束。",
            code=3,
        )
    if getattr(args, "ssh_session_token_file", None) or getattr(args, "ssh_owner_nonce", None):
        context.out("注意：`--ssh-session-token-file` / `--ssh-owner-nonce` 是 Desktop 的 SSH 后端专用，Orbit 无 Desktop，忽略")
    plan = serve_plan(
        host,
        port,
        insecure=bool(getattr(args, "insecure", False)),
        skip_build=bool(getattr(args, "skip_build", False)),
        isolated=bool(getattr(args, "isolated", False)),
    )
    _print_notices(context, plan)
    if not plan["available"]:
        raise CliError(plan["reason"], code=1)
    context.out(f"Orbit 后端 · {plan['target']}")
    context.out(f"访问地址：{plan['url']}（headless，不自动开浏览器）")
    context.out("按 Ctrl-C 停止。")
    import uvicorn

    # 只**调用** app.main（禁改文件）：uvicorn 负责 import string 与生命周期。
    uvicorn.run(plan["target"], **plan["uvicorn"])
    return 0


# ── dashboard ──────────────────────────────────────────────────────────────


def run_dashboard(context: Context, args) -> int:
    """面板入口：**不自己起服务**，只把浏览器指到已经在跑的后端上。

    与 Hermes 的差别必须说清：Hermes 的 `dashboard` 会构建前端并起服务；
    Orbit 的前端是预构建 dist，由 `orbit serve` 挂载，所以这里只做两件事——
    探测端口 + 开浏览器。端口没人在听时**不开浏览器**：打开一个连不上的页面
    比不开更让人困惑。
    """
    host, port = _host(args), _port(args)
    if getattr(args, "status", False):
        _status_report(context, host, port)
        return 0
    if getattr(args, "stop", False):
        raise CliError(
            "`orbit dashboard --stop` 未实现：面板就是 `orbit serve` 挂的同一份前端，"
            "没有独立进程可停。请 Ctrl-C 停掉 `orbit serve`。",
            code=3,
        )
    plan = serve_plan(
        host,
        port,
        insecure=bool(getattr(args, "insecure", False)),
        skip_build=bool(getattr(args, "skip_build", False)),
        isolated=bool(getattr(args, "isolated", False)),
    )
    _print_notices(context, plan)
    if plan["available"]:
        context.out(f"面板未在监听（{plan['url']}）。先运行：orbit serve --host {host} --port {port}")
        context.out("dashboard 只是浏览器入口，不自己起服务——这样端口/鉴权只有一处定义。")
        return 0
    context.out(f"面板在 {plan['url']} 上监听。")
    if getattr(args, "no_open", False):
        context.out("（--no-open：不自动打开浏览器）")
    else:
        opened = False
        try:
            opened = bool(webbrowser.open(plan["url"]))
        except Exception:  # noqa: BLE001 —— 无图形环境时开浏览器失败不该让命令失败
            opened = False
        context.out("已在浏览器打开。" if opened else f"没能自动打开浏览器，请手动访问 {plan['url']}")
    return 0


# ── console ────────────────────────────────────────────────────────────────


def _print_result(context: Context, result: console_module.ConsoleResult) -> None:
    if not result.allowed:
        context.warn(f"拒绝：{result.reason}")
        return
    if result.output.strip():
        context.out(result.output.rstrip("\n"))
    if result.error.strip():
        print(result.error.rstrip("\n"), file=sys.stderr)


def run_console(context: Context, args) -> int:
    """安全命令台。白名单与注入防护见 `app/console.py` 的模块 docstring。"""
    extras = getattr(args, "allow", None) or []
    policy = console_module.ConsolePolicy.default().with_extra(*extras)
    data_dir = context.data_dir

    once = str(getattr(args, "exec", "") or "").strip()
    if once:
        # 用 shlex 只做**分词**，绝不交给 shell：分完仍然是列表，仍要过白名单。
        import shlex

        try:
            argv = shlex.split(once, posix=False)
        except ValueError as exc:
            raise CliError(f"命令无法分词：{exc}", code=2) from exc
        result = console_module.execute(argv, policy=policy, data_dir=data_dir)
        _print_result(context, result)
        context.emit(
            {
                "ok": result.allowed and result.code == 0,
                "argv": list(result.argv),
                "allowed": result.allowed,
                "reason": result.reason,
                "code": result.code,
            }
        )
        return result.code

    context.out("Orbit 安全命令台 —— 只允许白名单里的只读命令，不经过 shell。")
    context.out(f"审计：{console_module.audit_path(data_dir)}")
    context.out("白名单：")
    for entry in policy.entries():
        context.out(f"  orbit {entry}")
    context.out("输入 exit 退出；help 重印这份清单。")
    if not sys.stdin or not sys.stdin.isatty():
        # 非交互（管道 / 远程消息桥 / 测试）：不读 stdin，直接把策略打出来。
        # 远程场景请用 `orbit console --exec "<命令>"`，那是可审计的一次性调用。
        context.out("（stdin 不是终端：不做交互读取。单次执行请用 `orbit console --exec \"orbit status\"`）")
        return 0

    while True:
        try:
            line = input("orbit> ")
        except (EOFError, KeyboardInterrupt):
            context.out()
            return 0
        command = line.strip()
        if not command:
            continue
        if command.lower() in {"exit", "quit", ":q"}:
            return 0
        if command.lower() in {"help", "?"}:
            for entry in policy.entries():
                context.out(f"  orbit {entry}")
            continue
        result = console_module.execute(command, policy=policy, data_dir=data_dir)
        _print_result(context, result)


# ── skin ───────────────────────────────────────────────────────────────────


def _no_color(args) -> bool:
    """`--no-color` 全局 flag（SUPPRESS 默认值，所以只能 getattr）。"""
    return bool(getattr(args, "no_color", False))


def run_skin_list(context: Context, args) -> int:
    active = skin_module.current(context.settings)
    rows = [
        (
            name + ("（当前）" if name == active else ""),
            skin.name,
            skin.description,
        )
        for name, skin in skin_module.SKINS.items()
    ]
    context.out("内置皮肤：")
    context.table(("名字", "标识", "说明"), rows)
    if skin_module.no_color_requested(flag=_no_color(args)):
        context.out("（NO_COLOR / --no-color 生效：所有颜色 token 降级为空串）")
    context.emit(
        {
            "ok": True,
            "active": active,
            "skins": [
                {"name": skin.name, "description": skin.description, "colors": skin.colors}
                for skin in skin_module.SKINS.values()
            ],
        }
    )
    return 0


def run_skin_show(context: Context, args) -> int:
    name = str(getattr(args, "name", "") or "")
    target = skin_module.resolve(name).name if name else skin_module.current(context.settings)
    pairs = skin_module.describe(context.settings, target, flag=_no_color(args))
    context.out(f"皮肤：{target}")
    context.table(("token", "颜色"), pairs)
    context.emit({"ok": True, "skin": target, "colors": dict(pairs)})
    return 0


def run_skin_use(context: Context, args) -> int:
    name = str(getattr(args, "name", "") or "").strip()
    if not name:
        raise CliError("用法：orbit skin use <名字>（可用名字见 `orbit skin list`）", code=2)
    try:
        path = skin_module.apply(name, context.settings)
    except ValueError as exc:
        raise CliError(str(exc), code=2) from exc
    context.out(f"已切换到皮肤 {name}（写入 {path}）")
    context.emit({"ok": True, "skin": name, "path": str(path)})
    return 0


def run_skin_set(context: Context, args) -> int:
    key = str(getattr(args, "key", "") or "")
    value = str(getattr(args, "value", "") or "")
    try:
        path = skin_module.set_color(
            context.settings, key, value, skin=str(getattr(args, "skin", "") or "")
        )
    except ValueError as exc:
        raise CliError(str(exc), code=2) from exc
    context.out(f"已设置 {key} = {value}（写入 {path}）")
    context.emit({"ok": True, "key": key, "value": value, "path": str(path)})
    return 0


def run_skin_reset(context: Context, args) -> int:
    removed = skin_module.reset(context.settings)
    context.out(
        f"已删除 {skin_module.skin_path(context.settings)}，回到默认皮肤"
        if removed
        else "没有 skin.json，已经是默认皮肤"
    )
    context.emit({"ok": True, "removed": removed, "skin": skin_module.DEFAULT_SKIN})
    return 0


# ── metrics（挂到 `monitoring` 下的 `metrics`） ─────────────────────────────


def run_metrics(context: Context, args) -> int:
    """把 `app/metrics.py` 的采集结果露出来：Prometheus 文本 / 人读表 / JSON。

    为什么挂在 `monitoring` 下面而不是新开一条顶层命令：Hermes 的监控面就是
    `monitoring`，而 Prometheus 抓取点是"监控导出"的一部分，不是一条独立功能。
    """
    from app import metrics as metrics_module

    values = metrics_module.collect(context.store, context.settings)
    if getattr(args, "prometheus", False):
        # 抓取端点要的是纯文本：**绕过** `context.out`（它会在 --json 下静默）。
        print(metrics_module.render_prometheus(values), end="")
    else:
        context.out("Prometheus 指标（无数据源的指标如实标注，不填 0）：")
        context.table(("指标", "类型/单位", "值", "来源"), metrics_module.summarize(values))
    # `--json` 载荷走 **shared_metrics schema v2**（MATRIX 15.4）。
    #
    # 之前 `as_dict()` 的载荷是自造形状：`metrics_schema.py` 的 v1/v2 与校验器
    # 都写好了、也有测试，但**唯一 importer 是 `orbit metrics schema` 这条报告命令**
    # ——`metrics.py` 自己不用 schema。于是校验器校验的是**用户喂进来的任意 JSON**，
    # 不是本产品产出的东西：**只有校验器、没有产出方的契约，等于没有契约**。
    #
    # 现在产出方自校验（`as_schema_payload` 里当场 `validate()`），契约一旦漂移
    # 就立刻炸，而不是等下游发现。
    payload = metrics_module.as_schema_payload(values)
    context.emit(
        {
            "ok": True,
            "schema_version": payload["schema_version"],
            "metrics": payload["metrics"],
        }
    )
    return 0


# ── 接线 ────────────────────────────────────────────────────────────────────


def _bind(path: str, handler: Any) -> None:
    """登记实现到声明表的某条路径（两种导入顺序都要成立）。

    与 `sessions.py::_bind` / `security.py::_bind` 同一套做法：`register_handler`
    负责"表还没组装"的情况，回填负责"表已经组装完"的情况。

    与那两处的唯一差别：**父命令本身也可以有 handler**（`dashboard` 既有子命令
    `register`，也要能单独跑）。所以"有子命令就不回填"这条限制只对子命令路径成立。
    """
    register_handler(path, handler)
    head, _, leaf = path.partition(" ")
    parent = REGISTRY.get(head)
    if parent is None:
        return
    target = parent.child(leaf) if leaf else parent
    if target is not None and (not leaf or not target.subcommands):
        target.handler = handler
        target.state = "done"


def mount_console_options() -> bool:
    """给 `console` 补 `--allow` / `--exec`（声明表里 console 没有选项）。返回是否挂上。"""
    command = REGISTRY.get("console")
    if command is None:
        return False
    mounted = False
    if not any("--allow" in option.flags for option in command.options):
        command.options.append(
            Option(
                ("--allow",),
                "临时加一条白名单（可重复，如 `--allow \"orbit skin list\"`）；只在本次会话生效",
                action="append",
                metavar="COMMAND",
            )
        )
        mounted = True
    if not any("--exec" in option.flags for option in command.options):
        command.options.append(
            Option(
                ("--exec",),
                "执行一条命令后退出（远程/脚本用；不做交互读取）",
                metavar="COMMAND",
            )
        )
        mounted = True
    return mounted


def mount_skin_subcommands() -> list[str]:
    """补 `skin show` / `skin reset`（声明表里只有 list / use / set）。**幂等**。"""
    parent = REGISTRY.get("skin")
    if parent is None:
        return []
    wanted: tuple[tuple[str, str, tuple[Option, ...], Any], ...] = (
        (
            "show",
            "显示某个皮肤的颜色 token（默认当前皮肤）",
            # 不重复声明 `--no-color`：它是**全局**选项，`_add_command` 会把全局
            # 选项作为 parents 传给子命令，重复声明会直接 argparse 冲突。
            (Option(("name",), "皮肤名（省略 = 当前皮肤）", nargs="?"),),
            run_skin_show,
        ),
        ("reset", "删除 skin.json，回到默认皮肤", (), run_skin_reset),
    )
    mounted: list[str] = []
    for name, help_text, options, handler in wanted:
        if parent.child(name) is not None:
            continue
        parent.subcommands.append(
            Command(
                name=name,
                help=help_text,
                handler=handler,
                group="core",
                state="done",
                needs_settings=name != "show",
                options=list(options),
            )
        )
        mounted.append(name)
    return mounted


def mount_monitoring_metrics() -> bool:
    """给 `monitoring` 补 `metrics` 子命令（Prometheus 抓取面）。**幂等**。"""
    parent = REGISTRY.get("monitoring")
    if parent is None or parent.child("metrics") is not None:
        return False
    parent.subcommands.append(
        Command(
            name="metrics",
            help="输出 Prometheus 指标（无数据源的指标如实标注）",
            handler=run_metrics,
            group="ops",
            state="done",
            needs_settings=False,
            needs_store=True,
            options=[
                Option(
                    ("--prometheus",),
                    "只输出 Prometheus 文本格式（供抓取，不做表格）",
                    action="store_true",
                )
            ],
        )
    )
    return True


#: 组装钩子是否已装过（幂等标记）。
_ASSEMBLY_HOOK_INSTALLED = False


def _install_assembly_hook() -> bool:
    """在"表组装"这个唯一接缝上补挂缺失的命令面（同 `security.py` 的手法）。

    为什么需要：`commands/__init__.py` 把功能域模块排在 `hermes_parity` **之前**，
    所以导入本模块时表还没组装（`REGISTRY.get("console")` 是 None），上面三个
    `mount_*` 无从下手。包一层 `register_specs`，跑完原函数后再补挂。
    """
    global _ASSEMBLY_HOOK_INSTALLED
    if _ASSEMBLY_HOOK_INSTALLED:
        return False
    original = _table.register_specs

    def register_specs(specs: Any) -> list[str]:
        skipped = original(specs)
        mount_console_options()
        mount_skin_subcommands()
        mount_monitoring_metrics()
        return skipped

    _table.register_specs = register_specs
    _ASSEMBLY_HOOK_INSTALLED = True
    return True


_bind("serve", run_serve)
_bind("dashboard", run_dashboard)
_bind("console", run_console)
_bind("skin list", run_skin_list)
_bind("skin use", run_skin_use)
_bind("skin set", run_skin_set)
_bind("skin show", run_skin_show)
_bind("skin reset", run_skin_reset)
_bind("monitoring metrics", run_metrics)

if REGISTRY.get("console") is None:
    _install_assembly_hook()
mount_console_options()
mount_skin_subcommands()
mount_monitoring_metrics()
