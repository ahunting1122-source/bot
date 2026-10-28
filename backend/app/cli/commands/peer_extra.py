"""``orbit peer`` 与 ``orbit gateway`` 的**协议面**子命令（MATRIX 12.9 / 12.10 / 5.13）。

## 本模块补的路径，以及每条"到底能做到哪一步"

| 路径 | 状态 | 说明 |
|---|---|---|
| ``peer health`` | 真实现 | ``GET <peer>/peer/health``：对端活着吗、声明了哪些能力 |
| ``peer run`` | 真实现（**待接管**） | ``POST <peer>/peer/run``：起异步 run 拿 ``run_id``；对端没端点时如实抛 ``PeerUnsupported`` |
| ``peer stop`` | 真实现（**待接管**） | ``POST <peer>/peer/stop``：只停这一个 run |
| ``gateway clarify`` | 真实现 | ``clarify`` 走 gateway：有 ``send_buttons`` 就用按钮，没有就退回编号文本 |
| ``gateway service-unit`` | 真实现 | **生成** systemd/launchd/Windows 计划任务的内容与手工步骤（**不安装**） |
| ``gateway enroll-plan`` | 真实现（打印清单） | 中继注册**要什么**：relay 地址格式 / wake URL 契约 / 凭证 / 轮换 |
| ``gateway instances`` | 真实现 | 读 ``data/gateway/instances.json`` 的多实例登记表 |

## 为什么 ``peer run`` / ``peer stop`` 是"待接管"

这两条路径在声明表里已经由 ``app/cli/commands/platform_extra.py`` 登记成"如实报未实现"
（``_table.register_handler`` 对同一路径**重复登记会抛 ValueError**，所以这里不能硬接）。
本模块的处理是：

1. 先查 ``_table.HANDLERS``：路径**空闲** → 用本模块的真实现接管（走
   :func:`app.peer_client.PeerClient`，对端没端点时抛 ``PeerUnsupported`` 并点名
   ``<base>/peer/run`` 与 ``<base>/peer/stop``）；
2. 路径**已被占** → 不抢，记 ``WIRING["peer run"] = "occupied"``，把真实现留着
   （:func:`run_peer_run` / :func:`run_peer_stop` 仍是可直接调用的公开函数）。

于是"要不要让真实现生效"变成一次**显式的接线决定**：删掉 ``platform_extra.py`` 里那两行
``_bind``，本模块的客户端路径立刻接管；不删也不会出现两条实现打架。

## 导入顺序

和 ``model_extra.py`` 同一套约定：父节点已存在就直接 ``subcommands.append``（子命令列表
本来就是分步拼装的），父节点还没登记（本模块排在 ``hermes_parity`` 之前）就把声明挂进
``register_specs`` 的钩子里，等表组装时一起登记。两条顺序都成立，不会"静默少几条命令"。
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx

from app import gateway as gw
from app import platforms
from app.cli.commands import _table
from app.cli.commands._table import Spec, register_handler
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option
from app.peer_client import (
    PEER_HEALTH_PATH,
    PROTOCOL,
    RUN_CONTRACT,
    PeerClient,
    PeerError,
    PeerResponseError,
    PeerUnavailable,
    PeerUnsupported,
)

#: 对等节点请求的 transport。**必须可替换**：本机无网络，测试注入
#: ``httpx.MockTransport``，否则这条链路在离线环境里只能"看着像实现了"。
PEER_TRANSPORT: httpx.BaseTransport | None = None

#: 对等节点请求超时（秒）。比 webhook 的 5s 长：对端可能要真跑一轮 agent。
PEER_TIMEOUT = 30.0

#: 路径 → 实际接线方式（``appended`` / ``claimed`` / ``occupied`` / ``deferred``）。
#: 测试与排障都读它：**"接上了没有、用的是哪种方式"必须能一句话回答**。
WIRING: dict[str, str] = {}


# ── 公共小工具 ──────────────────────────────────────────────────────────────


def _subcommand_name(args) -> str:
    """当前子命令名（用 ``args._command``，而不是 argparse 的 ``_<parent>_command``）。"""
    command = getattr(args, "_command", None)
    return str(getattr(command, "name", "") or "")


def _text(args, name: str, default: str = "") -> str:
    value = getattr(args, name, None)
    return default if value is None else str(value)


def _flag(args, name: str) -> bool:
    return bool(getattr(args, name, False))


def _peers(context: Context) -> gw.PeerRegistry:
    return gw.PeerRegistry(context.data_dir / gw.GATEWAY_DIRNAME / gw.PEERS_FILENAME)


def _split_peer(target: str) -> tuple[str, str]:
    """``<peer>[/<agent>]``：``/`` 之后是多路复用 peer 上的具名 profile。"""
    text = str(target or "").strip()
    name, _, agent = text.partition("/")
    return name.strip().lower(), agent.strip()


def _read_message(args, *, usage: str) -> str:
    """正文：位置参数优先，省略则读 stdin（照 Hermes 的 ``[message]`` 语义）。"""
    text = _text(args, "message")
    if text.strip():
        return text
    if sys.stdin is not None and not sys.stdin.isatty():
        piped = sys.stdin.read()
        if piped.strip():
            return piped
    raise CliError(f"{usage}：需要正文（或从 stdin 管道喂入）", code=2)


def _client(context: Context, name: str, agent: str = "") -> PeerClient:
    """按 ``peer add`` 的登记信息建一个客户端。**不猜 URL**：没登记就报错。"""
    peers = _peers(context).load()
    record = peers.get(name)
    if record is None:
        raise CliError(
            f"没有这个对等节点：{name}（先 `orbit peer add {name} --url http://host:port`）",
            code=1,
        )
    base = str(record.get("url", "")).rstrip("/")
    if not base:
        raise CliError(f"对等节点 {name} 没有登记 URL（重新 `peer add`）", code=1)
    return PeerClient(
        base,
        token=str(record.get("key", "")),
        agent=agent,
        transport=PEER_TRANSPORT,
        timeout=PEER_TIMEOUT,
    )


def _fail(exc: PeerError) -> CliError:
    """把客户端异常翻成退出码：**"对端不支持"是 3，"连不上/HTTP 错"是 1**。

    为什么必须分开：退出码 3 是本仓库约定的"命令面在、功能不在"，脚本据此不再重试；
    而连不上对端是**可重试**的临时故障。混成一个码，运维脚本就没法判断该不该重试。
    """
    if isinstance(exc, PeerUnsupported):
        return CliError(str(exc), code=3)
    if isinstance(exc, PeerUnavailable):
        return CliError(str(exc), code=1)
    if isinstance(exc, PeerResponseError):
        return CliError(str(exc), code=1)
    return CliError(str(exc), code=1)


# ── peer health ─────────────────────────────────────────────────────────────


def run_peer_health(context: Context, args) -> int:
    """``peer health [peer]``：探测对端活着吗、声明了哪些能力。

    省略 peer 时**逐个探测**已登记的对等节点——"我配的那几个还活着吗"是这条命令
    最常见的用法，要求用户一个个手敲等于没做。
    """
    target = _text(args, "target").strip()
    names = [_split_peer(target)[0]] if target else sorted(_peers(context).load())
    if not names:
        raise CliError(
            "还没有登记任何对等节点：先 `orbit peer add <name> --url http://host:port`", code=1
        )
    rows: list[tuple[str, str, str]] = []
    results: list[dict[str, Any]] = []
    for name in names:
        agent = _split_peer(target)[1] if target else ""
        try:
            client = _client(context, name, agent)
            payload = client.health()
        except PeerError as exc:
            rows.append((name, "失败", str(exc)[:80]))
            results.append({"peer": name, "ok": False, "error": str(exc)})
            continue
        except CliError as exc:
            rows.append((name, "未登记", str(exc)[:80]))
            results.append({"peer": name, "ok": False, "error": str(exc)})
            continue
        capabilities = payload.get("capabilities") or []
        rows.append(
            (
                name,
                "在线",
                "、".join(capabilities) if capabilities else "（未声明 capabilities）",
            )
        )
        results.append({"peer": name, **payload})
    context.table(("对等节点", "状态", "能力"), rows)
    context.out(
        f"协议：GET {PEER_HEALTH_PATH}；run/stop 的契约是「{RUN_CONTRACT}」。"
        "对端没声明 run/stop 能力时，`peer run` 会如实报未实现而不是假装排队。"
    )
    context.emit({"ok": all(item.get("ok") for item in results), "peers": results})
    return 0 if all(item.get("ok") for item in results) else 1


# ── peer run / stop（真实现；被 platform_extra 占位时不接管）─────────────────


def run_peer_run(context: Context, args) -> int:
    """``peer run <peer>[/<agent>] [message]``：在对端起一个异步 run，打印 run_id。

    对端没有 ``/peer/run`` 时抛 :class:`PeerUnsupported`（退出码 3），消息里点名
    「对端需要提供 ``<base>/peer/run`` 与 ``<base>/peer/stop``」——**绝不返回假 run_id**：
    一个查不到的 run id 会让用户拿着它等一个永远不来的结果。
    """
    name, agent = _split_peer(_text(args, "target"))
    if not name:
        raise CliError("用法：orbit peer run <peer>[/<agent>] [message]", code=2)
    message = _read_message(args, usage=f"orbit peer run {name}")
    client = _client(context, name, agent)
    key = _text(args, "idempotency_key").strip()
    try:
        result = client.run(agent, message, idempotency_key=key)
    except PeerError as exc:
        raise _fail(exc) from exc
    context.out(f"已提交到 {name}{'/' + agent if agent else ''}：run_id={result['run_id']}")
    context.out(
        "查询：`orbit peer status "
        f"{name}{'/' + agent if agent else ''} {result['run_id']}`；"
        f"停止：`orbit peer stop {name}{'/' + agent if agent else ''} {result['run_id']}`"
    )
    context.emit({**result, "protocol": PROTOCOL})
    return 0


def run_peer_stop(context: Context, args) -> int:
    """``peer stop <peer>[/<agent>] <run_id>``：停掉单个异步 run。"""
    name, agent = _split_peer(_text(args, "target"))
    run_id = _text(args, "run_id").strip()
    if not name or not run_id:
        raise CliError("用法：orbit peer stop <peer>[/<agent>] <run_id>", code=2)
    client = _client(context, name, agent)
    try:
        result = client.stop(run_id)
    except PeerError as exc:
        raise _fail(exc) from exc
    context.out(f"已请求停止 {name} 上的 run {run_id}（对端回 stopped={result['stopped']}）")
    context.emit(result)
    return 0


# ── gateway clarify（MATRIX 5.13：clarify 走 gateway 的 send_buttons）─────────


#: ``gateway clarify`` 不给 ``--option`` 时用的演示选项。**只用于手动验证**：
#: 它让"按钮 / 编号文本"两条路都能一条命令试出来，而不用先接一个真平台。
DEMO_OPTIONS: tuple[str, ...] = ("用 Markdown", "用纯文本", "先给我看草稿")


def run_gateway_clarify(context: Context, args) -> int:
    """``gateway clarify --to <platform:channel> --question ...``：把 clarify 发出去。

    这是 ``app.platforms.send_clarify`` 的**手动验证入口**：``--to console`` 走
    ``ConsoleAdapter``（没有 ``send_buttons`` → 退回编号文本），``--to file`` 同理；
    接一个真平台（telegram / slack …）就会走按钮那条路。

    为什么要这条命令：``clarify`` 元工具已经能产出"问题 + 选项"，但那条链路此前
    **没有经过 gateway**——于是"有按钮的平台用按钮、没按钮的降级成编号文本"这条
    规则从来没有被任何一条命令真正跑过。能手动跑一次的链路，才配叫"实现了"。
    """
    target = _text(args, "to").strip() or "console"
    question = _text(args, "question").strip() or "这次要用哪种呈现？"
    options = tuple(str(item).strip() for item in (getattr(args, "option", None) or []) if str(item).strip())
    if not options:
        options = DEMO_OPTIONS
        context.warn(f"没给 --option：用演示选项 {list(options)}（这条命令主要是手动验证链路）")
    if len(options) < 2:
        raise CliError(
            f"clarify 至少要 2 个选项（现在 {len(options)} 个）：只有一个说明这不是选择题",
            code=2,
        )
    parsed = platforms.parse_target(target)
    service = gw.GatewayService(context.data_dir)
    try:
        adapter = service.adapter_for(parsed.platform)
    except platforms.PlatformNotImplemented as exc:
        raise CliError(str(exc), code=1) from exc
    clarify_id = _text(args, "clarify_id").strip() or f"{parsed.platform}-{parsed.channel or 'home'}"
    delivery = platforms.send_clarify(
        adapter,
        parsed.channel,
        question,
        options,
        clarify_id=clarify_id,
        allow_free_text=not _flag(args, "no_free_text"),
    )
    rendered = delivery.rendered
    context.out(
        f"已发送（{delivery.platform} → {delivery.channel or '(默认)'}）："
        f"呈现方式 = {'按钮' if delivery.used == 'buttons' else '编号文本'}"
    )
    if rendered is not None and rendered.degraded:
        context.out(f"降级原因：{rendered.reason}")
    if rendered is not None and rendered.buttons:
        context.table(
            ("按钮", "回调 id"), [(label, callback) for label, callback in rendered.buttons]
        )
    if not delivery.ok:
        raise CliError(f"clarify 发送失败：{delivery.error}", code=1)
    context.emit(delivery.as_dict())
    return 0


# ── gateway service-unit（MATRIX 12.10：install / uninstall 的手工步骤）─────


def run_gateway_service_unit(context: Context, args) -> int:
    """``gateway service-unit``：**生成**后台服务单元并给出手工步骤（不安装）。

    ``gateway install`` / ``uninstall`` 如实未实现——它们要写 systemd / launchd /
    Windows 服务的系统级路径，超出 Orbit"只写 data_dir"的写入范围。但"要写什么"
    是完全确定的，所以这里把**文件内容真的生成出来**（有测试逐行核对），
    用户复制粘贴即可安装；``--output`` 只允许写进 ``data_dir``，否则拒绝。
    """
    kind = _text(args, "kind").strip() or "systemd-user"
    try:
        unit = gw.render_service_unit(
            kind,
            data_dir=context.data_dir,
            executable=_text(args, "executable").strip() or "orbit",
        )
    except ValueError as exc:
        raise CliError(str(exc), code=2) from exc
    out_path = _text(args, "output").strip()
    written = ""
    if out_path:
        target = Path(out_path).expanduser()
        resolved = target.resolve()
        root = context.data_dir.resolve()
        # 只允许写 data_dir 之内：这是 Orbit 的写入范围铁律，install 未实现的同一个理由。
        if root not in resolved.parents and resolved != root:
            raise CliError(
                f"拒绝写到 {resolved}：Orbit 只写 data_dir（{root}）之内。"
                "系统级路径请按下面的步骤自己放——这也是 `gateway install` 未实现的同一个原因。",
                code=1,
            )
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(unit.content, encoding="utf-8", newline="\n")
        written = str(resolved)
    context.out(f"服务单元（{unit.kind}）→ {unit.path}")
    if written:
        context.out(f"已写入：{written}")
    context.out("── 文件内容（**真的生成了**，只是没有替你安装）──")
    for line in unit.content.splitlines():
        context.out(line)
    context.out("── 安装步骤（手工执行）──")
    for step in unit.install_steps:
        context.out(f"  {step}")
    context.out("── 卸载步骤（手工执行）──")
    for step in unit.uninstall_steps:
        context.out(f"  {step}")
    if unit.note:
        context.out(f"注意：{unit.note}")
    context.emit({**unit.as_dict(), "written": written})
    return 0


# ── gateway enroll-plan（MATRIX 12.10：enroll 的可执行清单）──────────────────


def run_gateway_enroll_plan(context: Context, args) -> int:
    """``gateway enroll-plan``：打印中继注册**要什么**（enroll 本身仍未实现）。

    这条命令**成功**（退出码 0）——它要做的事就是"把清单说清楚"，这件事做到了。
    但输出的第一行必须写明 ``gateway enroll`` 仍未实现，否则用户会以为注册成功了。
    """
    plan = gw.enroll_requirements(
        connector_url=_text(args, "connector_url").strip(),
        gateway_id=_text(args, "gateway_id").strip(),
        wake_url=_text(args, "wake_url").strip(),
    )
    context.out("`orbit gateway enroll` 仍未实现（本命令只输出清单，没有注册任何东西）：")
    context.out(plan.reason)
    context.table(
        ("要求", "怎么满足"),
        [(item.requirement, item.how) for item in plan.requirements],
    )
    context.out("按顺序做的事：")
    for step in plan.steps:
        context.out(f"  {step}")
    context.emit(plan.as_dict())
    return 0


# ── gateway instances（MATRIX 12.10：多实例登记表）──────────────────────────


def run_gateway_instances(context: Context, args) -> int:
    """``gateway instances``：读 ``data/gateway/instances.json`` 并显示多实例表。

    与 ``gateway list`` 的分工：``list`` 看**本机 dispatcher 状态**，``instances``
    看**登记表**（可能有多条、可能跨机器）。本机只有一条时如实说明——
    Orbit 没有 Hermes 的 profile 多实例语义，这不是缺项，是事实。
    """
    view = gw.instance_rows(context.data_dir)
    context.table(
        ("实例", "数据目录", "主机", "dispatcher", "启动次数", "平台"),
        [
            (
                str(row["id"]),
                str(row["data_dir"]),
                str(row["host"]),
                str(row["dispatcher"]),
                str(row["starts"]),
                str(row["platforms"]),
            )
            for row in view["instances"]
        ],
    )
    context.out(view["note"])
    context.out(f"登记表：{view['registry']}（直接编辑这份 JSON 就能补其它机器的条目）")
    context.emit(view)
    return 0


# ── 声明（表里没有这些子命令，所以要自己挂）────────────────────────────────


def _health_command() -> Command:
    return Command(
        name="health",
        help="探测对等节点的健康与能力（GET /peer/health）",
        handler=run_peer_health,
        group="platform",
        parity="P1",
        state="done",
        hermes="peer health",
        options=[Option(("[target]",), "对等节点（<peer>[/<agent>]；省略则探测全部已登记的）")],
    )


def _clarify_command() -> Command:
    return Command(
        name="clarify",
        help="把 clarify 的问题与选项经网关发出去（有按钮用按钮，没有则降级为编号文本）",
        handler=run_gateway_clarify,
        group="run",
        parity="P1",
        state="done",
        hermes="gateway clarify",
        options=[
            Option(("-t", "--to"), "投递目标 platform:channel（默认 console）", metavar="TARGET"),
            Option(("--question",), "要用户决定的问题（默认用一条演示问题）", metavar="TEXT"),
            Option(("--option",), "一个选项标签（可重复；不给则用演示选项）",
                   metavar="LABEL", action="append"),
            Option(("--clarify-id",), "回调 id 的 ident 段（默认 platform-channel）",
                   metavar="ID", dest="clarify_id"),
            Option(("--no-free-text",), "只允许选选项、不接受自由作答",
                   action="store_true", dest="no_free_text"),
        ],
    )


def _service_unit_command() -> Command:
    return Command(
        name="service-unit",
        help="生成后台服务单元内容与手工安装步骤（不安装：系统级路径超出写入范围）",
        handler=run_gateway_service_unit,
        group="run",
        parity="P1",
        state="done",
        hermes="gateway install",
        options=[
            Option(("--kind",), f"单元种类：{'/'.join(gw.SERVICE_KINDS)}（默认 systemd-user）",
                   metavar="KIND", choices=gw.SERVICE_KINDS),
            Option(("--executable",), "orbit 可执行文件路径（默认 orbit）", metavar="PATH"),
            Option(("-o", "--output"), "把内容写进这个路径（**只允许 data_dir 之内**）",
                   metavar="PATH"),
        ],
    )


def _enroll_plan_command() -> Command:
    return Command(
        name="enroll-plan",
        help="打印中继注册需要什么（enroll 本身仍未实现）",
        handler=run_gateway_enroll_plan,
        group="run",
        parity="P1",
        state="done",
        hermes="gateway enroll",
        options=[
            Option(("--connector-url",), "中继（connector）地址，用于核对格式", metavar="URL"),
            Option(("--gateway-id",), "网关 id（拼进注册路径）", metavar="ID"),
            Option(("--wake-url",), "对端反向唤醒本机的 URL，用于核对契约", metavar="URL"),
        ],
    )


def _instances_command() -> Command:
    return Command(
        name="instances",
        help="列出 data/gateway/instances.json 里的网关实例（多实例登记表）",
        handler=run_gateway_instances,
        group="run",
        parity="P1",
        state="done",
        hermes="gateway list",
    )


#: 子命令名 → (父命令, Command 构造器, 声明表里的 Spec 形态)。
#:
#: 为什么要 Spec 形态：本模块可能排在 ``hermes_parity`` **之前**导入，那一刻
#: ``REGISTRY.get("peer")`` 还是 ``None``，直接 append 会静默丢掉整条命令。
#: 有 Spec 就能在表组装时插进去（和 ``gateway.py`` 注入 ``doctor`` 同一套做法）。
_EXTRA: tuple[tuple[str, str, Any, Spec], ...] = (
    ("peer", "health", _health_command,
     Spec(name="health", help="探测对等节点的健康与能力", group="platform", parity="P1")),
    ("gateway", "clarify", _clarify_command,
     Spec(name="clarify", help="把 clarify 的问题与选项经网关发出去", group="run", parity="P1")),
    ("gateway", "service-unit", _service_unit_command,
     Spec(name="service-unit", help="生成后台服务单元内容与手工安装步骤",
          group="run", parity="P1")),
    ("gateway", "enroll-plan", _enroll_plan_command,
     Spec(name="enroll-plan", help="打印中继注册需要什么", group="run", parity="P1")),
    ("gateway", "instances", _instances_command,
     Spec(name="instances", help="列出网关实例登记表", group="run", parity="P1")),
)


def _attach(path: str, command: Command) -> str:
    """把一条新子命令挂进注册表，返回实际接线方式（``appended`` / ``deferred``）。"""
    head, _, tail = path.partition(" ")
    parent = REGISTRY.get(head)
    if parent is None:
        WIRING[path] = "deferred"
        return "deferred"
    existing = REGISTRY.resolve_child(parent, tail) if tail else parent
    if existing is not None:
        if command.handler is not None:
            existing.handler = command.handler
            existing.state = "done"
        WIRING[path] = "claimed"
        return "claimed"
    parent.subcommands.append(command)
    WIRING[path] = "appended"
    return "appended"


def _inject_specs(specs: tuple[Spec, ...]) -> tuple[Spec, ...]:
    """把本模块的 5 条子命令声明插进 ``peer`` / ``gateway`` 的 Spec（表组装前）。"""
    wanted: dict[str, tuple[Spec, ...]] = {}
    for parent_name, _leaf, _builder, spec in _EXTRA:
        wanted[parent_name] = (*wanted.get(parent_name, ()), spec)
    patched: list[Spec] = []
    for spec in specs:
        extra = wanted.get(spec.name, ())
        missing = tuple(
            item for item in extra if all(sub.name != item.name for sub in spec.subcommands)
        )
        patched.append(
            replace(spec, subcommands=(*spec.subcommands, *missing)) if missing else spec
        )
    return tuple(patched)


def _install_spec_hook() -> None:
    """包一层 ``register_specs``：表组装时把本模块的子命令一起登记。"""
    current = _table.register_specs
    if getattr(current, "_orbit_peer_extra_hook", False):
        return

    def wrapper(specs: tuple[Spec, ...]) -> list[str]:
        return current(_inject_specs(specs))

    setattr(wrapper, "_orbit_peer_extra_hook", True)  # noqa: B010 - 打标记防重复包装
    _table.register_specs = wrapper


def _claim(path: str, handler: Any) -> str:
    """把**表里已登记**的路径换成真实现；已被别人占用时不抢。"""
    key = " ".join(path.split())
    if key in _table.HANDLERS:
        WIRING[path] = "occupied"
        return "occupied"
    register_handler(path, handler)
    head, _, leaf = path.partition(" ")
    parent = REGISTRY.get(head)
    target = parent.child(leaf) if (parent is not None and leaf) else None
    if target is not None and not target.subcommands:
        target.handler = handler
        target.state = "partial"
    WIRING[path] = "claimed"
    return "claimed"


def _wire() -> None:
    """一次性接线：5 条新子命令 + 2 条待接管的 run/stop。"""
    if REGISTRY.get("peer") is None or REGISTRY.get("gateway") is None:
        _install_spec_hook()
    for parent_name, leaf, builder, _spec in _EXTRA:
        path = f"{parent_name} {leaf}"
        command = builder()
        # 先登记 handler：表若在之后组装，`Spec.build()` 才找得到它。
        if " ".join(path.split()) not in _table.HANDLERS and command.handler is not None:
            register_handler(path, command.handler)
        _attach(path, command)
    # `peer run` / `peer stop`：真实现已就位，但只在路径空闲时接管（见模块文档）。
    _claim("peer run", run_peer_run)
    _claim("peer stop", run_peer_stop)


_wire()


__all__ = [
    "DEMO_OPTIONS",
    "PEER_TIMEOUT",
    "PEER_TRANSPORT",
    "WIRING",
    "run_gateway_clarify",
    "run_gateway_enroll_plan",
    "run_gateway_instances",
    "run_gateway_service_unit",
    "run_peer_health",
    "run_peer_run",
    "run_peer_stop",
]
