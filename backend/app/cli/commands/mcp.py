"""`orbit mcp …` 的真实现（列表 / 增删 / 体检 / 配置 / OAuth token / 起服务）。

## 接线方式：`register_handler` 接表里已有的路径，**不另立顶层命令**

声明表（`hermes_parity.py`）里 `mcp` 已有 **11 个子命令**（`serve` / `add` / `remove` /
`list` / `test` / `configure` / `login` / `reauth` / `picker` / `catalog` / `install`）。
另立一个 `mcp` 顶层命令会让 `register_specs` 整条跳过表里那条，剩下未实现的子命令
（`picker` / `catalog` / `install`）当场从 `orbit --help` 里消失——命令面缩水，矩阵也跟着
丢条目。所以照 `_table.py` 的约定：**表不动，只声明"这个路径我接了"**。

本轮接了 8 条：`list` / `add` / `remove` / `test` / `configure` / `login` / `reauth` / `serve`。
`picker` / `catalog` / `install` 保持 `todo`，理由见文末 `UNBOUND_SUBCOMMANDS`。

## 表里没有的 6 条（`enable` / `disable` / `reload` / `resources` / `prompts` / `auth`）

它们是本波次新增的能力，声明表里没有位置。两条路都不能走：`register_handler` 只能填
"表里已有的路径"；另立顶层命令又会挤掉未实现的子命令。所以改用**给已构建的 `mcp`
Command 追加子命令**（:func:`extend_command`）——追加不会覆盖任何东西，`--help` 里
原有 11 条一条不少。

追加的时机有两种，都要成立（:func:`_install` 覆盖）：

- 本模块排在 `hermes_parity` **之后**导入（`import app.cli.commands.mcp` 会先跑完包初始
  化）→ `mcp` Command 已存在，直接追加；
- 本模块排在 **之前**（`commands/__init__.py` 的常规接线，`register_handler` 需要这个
  顺序）→ `mcp` 还不存在，把 `_table.register_specs` 包一层，等声明表组装完立刻追加。

## 与 Hermes 的三处**刻意差异**（照抄会变成假对齐）

1. **`mcp auth` 只做 token 存储与过期检查**，不做 OAuth 授权流程。真流程需要：发现
   authorization server 元数据 → PKCE → **本地回调 HTTP 服务器** → 换 token → 刷新。
   本机无网络、也没有可联调的 OAuth 服务，做出来只能是一段没人验证过的壳。所以
   `login` / `reauth` / `auth login` 一律**如实报未实现并给出手工通道**（`auth set`）。
2. **`mcp test` 是"连一次就断"**，不走 `MCPManager.start()`：那个入口会连全部服务、注册
   工具、起看门狗与后台重连，对一个"体检"命令来说是过量的副作用（还会留下永不自行退出
   的 task）。
3. **`mcp configure` 不弹交互式勾选器**：Hermes 用 TUI 让用户勾工具子集，Orbit 的 CLI 是
   非交互的。这里改为"连上去、把工具清单打出来、告诉你用 `data/tools.json` 怎么限制"，
   而不是假装有个选不动的勾选框。
"""

from __future__ import annotations

import getpass
import logging
import shlex
import sys
from datetime import datetime
from typing import Any

import app.cli.commands._table as _table
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context, truncate
from app.cli.shorthand import to_option
from app.cli.spec import REGISTRY, Command, Option
from app.mcp import MCPManager, OAuthStore, probe, write_servers

#: 状态文案：keyring 里 token 的四种状态（`missing`/`expired`/`expiring`/`valid`）。
AUTH_STATE_LABELS = {
    "missing": "未配置",
    "expired": "已过期",
    "expiring": "即将过期",
    "valid": "有效",
}
#: "未实现"的退出码。与 `_table._pending` 一致，脚本可以据此区分"没做"和"跑失败了"。
UNIMPLEMENTED_CODE = 3


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _manager(context: Context) -> MCPManager:
    return MCPManager(context.settings)


def _find(manager: MCPManager, name: str) -> dict:
    """按名找服务配置，找不到给出可行动的报错（而不是一个 KeyError）。"""
    for entry in manager.servers():
        if str(entry.get("name")) == name:
            return entry
    known = ", ".join(str(item.get("name")) for item in manager.servers()) or "（空）"
    raise CliError(f"没有名为 {name!r} 的 MCP 服务。已配置：{known}")


def _target(entry: dict) -> str:
    """服务的连接目标（远程 URL 或 stdio 命令行），表格里要能一眼看出跑的是什么。"""
    url = str(entry.get("url") or "").strip()
    if url:
        return url
    parts = [str(entry.get("command") or "")] + [str(a) for a in entry.get("args") or []]
    return " ".join(part for part in parts if part)


def _seconds(raw: Any, flag: str) -> float:
    """把 ``--connect-timeout`` / ``--expires-in`` 的原始值转成秒数。

    单独抽出来是因为两处都要同一句报错文案：``float("abc")`` 抛的是
    ``could not convert string to float: 'abc'``，用户看不出是哪条命令的哪个选项。
    """
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise CliError(f"`{flag}` 需要秒数，收到 {raw!r}") from None


def _iso(epoch: float) -> str:
    """epoch 秒 → 本地时间。0 表示"服务没给到期时间"，显示成"未知"而不是 1970。"""
    if not epoch:
        return "未知"
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "未知"


def _split_args(raw: Any) -> list[str]:
    """``--args`` 的值切成参数列表。

    Hermes 的写法是 ``--args ...``（吃掉后面全部参数，必须放最后）。Orbit 的声明表只能
    表达"一个值"，所以这里按 shell 规则切分：``--args "-y foo"`` → ``["-y", "foo"]``。
    引号由 shlex 处理，切不开时退回空白切分（不让一个奇怪的值把命令打崩）。
    """
    text = str(raw or "").strip()
    if not text:
        return []
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _parse_env(raw: Any) -> dict[str, str]:
    """``--env KEY=VALUE``（可重复）→ dict。没有 ``=`` 的值报错，不静默丢弃。"""
    values: list[str] = []
    if isinstance(raw, (list, tuple)):
        values = [str(item) for item in raw]
    elif raw:
        values = [str(raw)]
    env: dict[str, str] = {}
    for item in values:
        text = item.strip()
        if not text:
            continue
        if "=" not in text:
            raise CliError(f"`--env` 需要 KEY=VALUE 形式，收到 {item!r}")
        key, _, value = text.partition("=")
        if not key.strip():
            raise CliError(f"`--env` 的键不能为空：{item!r}")
        env[key.strip()] = value
    return env


def _oauth_servers(manager: MCPManager) -> list[str]:
    """配置里声明了 ``auth: oauth`` 的服务名（`auth status` / `reauth --all` 用）。

    keyring 无法枚举键，所以"哪些服务用了 OAuth"只能从配置里读——这也正是配置里
    必须落 ``auth`` 字段的原因（见 `app.mcp.write_servers`）。
    """
    return [
        str(entry.get("name"))
        for entry in manager.servers()
        if str(entry.get("auth") or "").lower() == "oauth"
    ]


# ── mcp list ────────────────────────────────────────────────────────────────


def run_list(context: Context, args) -> int:
    """`mcp list`：配置了哪些服务。

    **不打连接状态**：连接由正在运行的后端持有，CLI 是另一个进程，看不到也管不着。
    在这一行里写个 ``idle`` 只会让人以为服务真的闲着——所以明说"状态不在本进程"。
    """
    manager = _manager(context)
    servers = manager.servers()
    if not servers:
        context.out("没有配置任何 MCP 服务。")
        context.note("添加：orbit mcp add <name> --command <可执行文件> --args \"…\"")
        context.note("或：  orbit mcp add <name> --url https://example.com/mcp")
        context.emit({"ok": True, "servers": [], "count": 0})
        return 0
    rows: list[dict[str, Any]] = []
    for entry in servers:
        rows.append(
            {
                "name": str(entry.get("name")),
                "target": _target(entry),
                "enabled": bool(entry.get("enabled", True)),
                "auto_approve": bool(entry.get("auto_approve", False)),
                "index_resources": bool(entry.get("index_resources", False)),
                "auth": str(entry.get("auth") or ""),
                "timeout": entry.get("timeout"),
                "secrets": manager.secret_names(str(entry.get("name"))),
            }
        )
    cells: list[tuple[str, ...]] = []
    for row in rows:
        secrets = row["secrets"]
        cells.append(
            (
                str(row["name"]),
                truncate(str(row["target"]), 48),
                "是" if row["enabled"] else "否",
                "是" if row["auto_approve"] else "否",
                "是" if row["index_resources"] else "否",
                str(row["auth"]) or "-",
                ",".join(str(item) for item in secrets) or "-",
            )
        )
    context.table(
        ("名称", "目标", "启用", "自动批准", "索引资源", "鉴权", "密钥"),
        cells,
    )
    context.note()
    context.note("连接状态不在此进程：MCP 连接由正在运行的后端持有（`orbit mcp test <name>` 可单点体检）。")
    context.emit({"ok": True, "servers": rows, "count": len(rows)})
    return 0


# ── mcp add / remove ────────────────────────────────────────────────────────


def run_add(context: Context, args) -> int:
    """`mcp add`：追加一个服务配置。

    ``--preset`` 如实报未实现：Orbit 没有内置的 MCP 预设目录（Hermes 的 optional-mcps
    63 条模板，见矩阵 8.10），编一份出来只会让人以为预设能用。
    """
    name = str(getattr(args, "name", "") or "").strip()
    if not name:
        raise CliError("需要服务名：orbit mcp add <name> --command <可执行文件> | --url <端点>")
    if getattr(args, "preset", None):
        raise CliError(
            f"`--preset {args.preset}` 未实现：Orbit 没有内置 MCP 预设目录"
            "（Hermes 的 optional-mcps 63 条模板，矩阵 8.10 尚未落地）。"
            "请改用 `--command` / `--url` 直接给出连接目标。",
            code=UNIMPLEMENTED_CODE,
        )
    url = str(getattr(args, "url", "") or "").strip()
    command = str(getattr(args, "command", "") or "").strip()
    if url and command:
        raise CliError("`--url`（远程 HTTP/SSE）与 `--command`（本地 stdio）只能二选一")
    if not url and not command:
        raise CliError("需要 `--url`（远程 HTTP/SSE）或 `--command`（本地 stdio 进程）")
    manager = _manager(context)
    servers = manager.servers()
    if any(str(entry.get("name")) == name for entry in servers):
        raise CliError(f"{name!r} 已存在。先 `orbit mcp remove {name}` 再添加。")
    entry: dict[str, Any] = {"name": name, "enabled": True}
    if url:
        entry["url"] = url
    else:
        entry["command"] = command
        entry["args"] = _split_args(getattr(args, "args", None))
        env = _parse_env(getattr(args, "env", None))
        if env:
            entry["env"] = env
    auth = str(getattr(args, "auth", "") or "").strip().lower()
    if auth:
        if auth not in {"oauth", "header"}:
            raise CliError(f"`--auth` 只支持 oauth / header，收到 {auth!r}")
        entry["auth"] = auth
    raw_timeout = getattr(args, "connect_timeout", None)
    if raw_timeout not in (None, ""):
        entry["timeout"] = _seconds(raw_timeout, "--connect-timeout")
    write_servers(manager, [*servers, entry])
    context.out(f"已添加 MCP 服务：{name}（{_target(entry)}）")
    if auth == "oauth":
        context.warn(
            "已记下 auth=oauth，但**OAuth 授权流程本轮未实现**（需要本地回调服务器 + 浏览器授权）。"
            "拿到 token 后用 `orbit mcp auth set "
            f"{name}` 手工写入 keyring，`orbit mcp auth status` 可查过期时间。"
        )
    if auth == "header":
        context.note("auth=header：请用 `orbit mcp auth set` 之外的方式把 header 写进 data/mcp.json 的 headers（keyring:名字）。")
    context.note("下一步：orbit mcp test " + name)
    context.emit({"ok": True, "server": entry})
    return 0


def run_remove(context: Context, args) -> int:
    """`mcp remove`：删掉一个服务配置（连带它的密钥名记录，但**不删 keyring 里的密钥**）。"""
    name = str(getattr(args, "name", "") or "").strip()
    manager = _manager(context)
    _find(manager, name)
    remaining = [entry for entry in manager.servers() if str(entry.get("name")) != name]
    manager.save_servers(remaining)
    secrets = manager.secret_names(name)
    context.out(f"已移除 MCP 服务：{name}")
    if secrets:
        context.note(
            f"它在 keyring 里的密钥（{', '.join(secrets)}）仍保留：删配置不该顺手动别人的凭证。"
            "要清掉请用系统凭据管理器，或 `orbit mcp auth clear " + name + "`（仅 OAuth token）。"
        )
    context.emit({"ok": True, "removed": name, "remaining": len(remaining)})
    return 0


# ── mcp enable / disable ────────────────────────────────────────────────────


def _set_enabled(context: Context, args, enabled: bool) -> int:
    """启用/禁用的公共实现。``enabled`` 在 `save_servers` 的白名单里，所以走它即可。"""
    name = str(getattr(args, "name", "") or "").strip()
    manager = _manager(context)
    _find(manager, name)
    updated = []
    for entry in manager.servers():
        if str(entry.get("name")) == name:
            entry = {**entry, "enabled": enabled}
        updated.append(entry)
    manager.save_servers(updated)
    context.out(f"已{'启用' if enabled else '禁用'} MCP 服务：{name}")
    if not enabled:
        context.note("禁用的服务不会在启动时连接，也不注册任何工具；配置保留，随时可再启用。")
    context.emit({"ok": True, "server": name, "enabled": enabled})
    return 0


def run_enable(context: Context, args) -> int:
    """`mcp enable`：让服务在下次装配时连接。"""
    return _set_enabled(context, args, True)


def run_disable(context: Context, args) -> int:
    """`mcp disable`：停用服务但保留配置。"""
    return _set_enabled(context, args, False)


# ── mcp test / configure ────────────────────────────────────────────────────


def run_test(context: Context, args) -> int:
    """`mcp test`：连一次、把发现到的东西打出来、断开。

    失败走 `CliError`（退出码 1）而不是 0：脚本里 `orbit mcp test x && ...` 必须能靠
    退出码判断通不通。
    """
    name = str(getattr(args, "name", "") or "").strip()
    manager = _manager(context)
    entry = _find(manager, name)
    if not entry.get("enabled", True):
        context.warn(f"{name} 当前是禁用状态，仍按配置试连一次。")
    report = context.run(probe(context.settings, entry))
    if not report["ok"]:
        raise CliError(f"MCP 服务 {name} 连接失败：{report['error']}")
    context.out(f"MCP 服务 {name} 连接成功：{_target(entry)}")
    context.out()
    context.table(
        ("项", "数量"),
        (
            ("tools", str(len(report["tools"]))),
            ("prompts", str(len(report["prompts"]))),
            ("resources", str(len(report["resources"]))),
            ("resource templates", str(len(report["templates"]))),
        ),
    )
    for tool in report["tools"]:
        context.note(f"  tool  {tool['name']}  {truncate(tool['description'], 60)}")
    for prompt in report["prompts"]:
        context.note(f"  prompt  {prompt['name']}  {truncate(prompt['description'], 60)}")
    for resource in report["resources"]:
        context.note(f"  resource  {resource['uri']}  {resource['mime_type'] or '-'}")
    context.emit({"ok": True, "name": name, **report})
    return 0


def run_configure(context: Context, args) -> int:
    """`mcp configure`：把这个服务的工具清单摊开，并说明怎么限制子集。

    Hermes 这里是交互式勾选器（TUI）。Orbit 的 CLI 非交互，所以**不做**一个选不动的
    勾选框，而是把"能改什么、改在哪里"说清楚。
    """
    name = str(getattr(args, "name", "") or "").strip()
    manager = _manager(context)
    entry = _find(manager, name)
    report = context.run(probe(context.settings, entry))
    if not report["ok"]:
        raise CliError(f"无法列出 {name} 的工具：{report['error']}")
    context.out(f"{name} 提供 {len(report['tools'])} 个工具（前缀 mcp__{name}__）：")
    context.out()
    for tool in report["tools"]:
        context.out(f"  {tool['name']}  {truncate(tool['description'], 70)}")
    context.out()
    context.warn(
        "交互式勾选未实现：Orbit 的 CLI 没有 TTY 交互层，做出来只会是个选不动的勾选框。"
        "要限制子集，改 data/tools.json（`orbit tools list` / `orbit tools disable`）。"
    )
    context.emit({"ok": True, "name": name, "tools": report["tools"]})
    return 0


# ── mcp login / reauth（OAuth：只有 token 这一半）─────────────────────────────


def _auth_note(name: str) -> str:
    return (
        "OAuth 授权流程本轮未实现，原因：它需要发现 authorization server 元数据 → PKCE → "
        "**在本机开一个回调 HTTP 服务器**等浏览器重定向 → 换 token，而本机无网络、也没有"
        "可联调的 OAuth 服务，做出来是一段没人验证过的壳。"
        f"已有的通道：`orbit mcp auth set {name}` 手工写入 token（走 keyring），"
        f"`orbit mcp auth status {name}` 查过期时间。"
    )


def run_login(context: Context, args) -> int:
    """`mcp login`：强制重新鉴权。本轮只报 token 现状 + 未实现原因。"""
    name = str(getattr(args, "name", "") or "").strip()
    manager = _manager(context)
    _find(manager, name)
    status = OAuthStore.for_settings(context.settings).status(name)
    context.out(
        f"{name} 的 OAuth token：{AUTH_STATE_LABELS.get(status['state'], status['state'])}"
        f"（到期：{_iso(status['expires_at'])}）"
    )
    raise CliError(_auth_note(name), code=UNIMPLEMENTED_CODE)


def run_reauth(context: Context, args) -> int:
    """`mcp reauth`：给一个或全部 OAuth 服务重新鉴权。同上，本轮如实报未实现。"""
    name = str(getattr(args, "name", "") or "").strip()
    manager = _manager(context)
    names = [name] if name else _oauth_servers(manager)
    if name:
        _find(manager, name)
    if not names:
        raise CliError(
            "配置里没有声明 auth=oauth 的服务。"
            "（`orbit mcp add <name> --url … --auth oauth` 会写下这个字段）"
        )
    if getattr(args, "all", False) and not name:
        context.out(f"要对 {len(names)} 个 OAuth 服务重新鉴权：{', '.join(names)}")
    raise CliError(_auth_note(names[0]), code=UNIMPLEMENTED_CODE)


# ── mcp serve ───────────────────────────────────────────────────────────────


def run_serve(context: Context, args) -> int:
    """`mcp serve`：把 Orbit 的工具表以 MCP stdio 服务暴露出去。

    **stdout 属于协议**：任何提示都必须走 stderr，否则对端会收到一行不是 JSON-RPC 的
    噪声并直接断开（现象是"服务秒退"，看不出原因）。`--json` 与它天然冲突，直接拒。
    """
    if context.json:
        raise CliError("`mcp serve` 不能与 `--json` 同用：stdio 协议独占了 stdout。")
    if getattr(args, "accept_hooks", False):
        context.warn(
            "`--accept-hooks` 已接受但未接线：它控制「未审阅 shell 钩子免确认」，"
            "而 MCP 服务进程里没有钩子执行通道（钩子在后端进程里跑）。"
        )
    level = logging.INFO if getattr(args, "verbose", False) else logging.WARNING
    logging.basicConfig(level=level, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s")
    from app.mcp_server import registry_specs, serve
    from app.tools import ToolRegistry

    # store 传 None：`specs()` 只读工具表与 data/tools.json，不碰库。真实执行才需要
    # Store，而本波次没接线（见 app/mcp_server.py 的模块 docstring）。
    registry = ToolRegistry(context.settings, None)  # type: ignore[arg-type]
    specs = registry_specs(registry)
    print(
        f"orbit mcp serve：stdio 模式，暴露 {len(specs)} 个工具（日志走 stderr）",
        file=sys.stderr,
    )
    context.run(serve(specs))
    return 0


# ── mcp reload ──────────────────────────────────────────────────────────────


def run_reload(context: Context, args) -> int:
    """`mcp reload`：重新校验配置 + 清空资源索引的增量状态。

    **不能重连正在运行的后端**：MCP 连接活在后端进程里，CLI 拿不到它的管理器。假装
    "已重连"是最糟的一种——用户会以为工具回来了。所以这里只做两件真能做的事，并把
    边界说清楚。
    """
    from app.mcp_resources import ResourceIndexer

    manager = _manager(context)
    servers = manager.servers()
    problems: list[str] = []
    for entry in servers:
        name = str(entry.get("name") or "")
        if not str(entry.get("url") or entry.get("command") or "").strip():
            problems.append(f"{name}：既没有 url 也没有 command")
    ResourceIndexer(None, context.data_dir / "mcp-resources.json").reset()
    context.out(f"已重新读取配置：{len(servers)} 个服务")
    if problems:
        for problem in problems:
            context.warn(f"配置不完整：{problem}")
    else:
        context.out("配置校验通过（每个服务都有 url 或 command）")
    context.out("已清空 MCP 资源索引的增量状态：下次索引会全量重抓（不删已入库的块）。")
    context.warn(
        "正在运行的后端不会被这个命令重连：MCP 连接活在后端进程里。"
        "要让改动生效，重启后端（或等它下一次装配）。"
    )
    context.emit(
        {"ok": True, "servers": len(servers), "problems": problems, "state_reset": True}
    )
    return 0 if not problems else 1


# ── mcp resources ───────────────────────────────────────────────────────────


async def _resource_rows(store) -> list[tuple]:
    """按 (origin, host) 聚合已入库的 MCP 资源块。"""
    from sqlalchemy import func, select

    from app.storage import Chunk

    async with store.sessions() as session:
        result = await session.execute(
            select(
                Chunk.origin,
                Chunk.host,
                func.count(Chunk.id),
                func.max(Chunk.created_at),
            )
            .where(Chunk.source_type == "mcp_resource")
            .group_by(Chunk.origin, Chunk.host)
            .order_by(Chunk.origin)
        )
        return [tuple(row) for row in result.all()]


def run_resources(context: Context, args) -> int:
    """`mcp resources`：列出**已经入库**的 MCP 资源（来自 chunks 表，不是去问服务）。

    这是"索引结果"而不是"服务声明"——两者必须分开看，否则"服务有资源但一条没索引"
    会被误读成"服务没有资源"。服务声明请用 `orbit mcp test <name>`。
    """
    name = str(getattr(args, "name", "") or "").strip()
    rows = context.run(_resource_rows(context.store))
    if name:
        rows = [row for row in rows if str(row[1]) == f"mcp:{name}"]
    if not rows:
        scope = f"服务 {name} 的" if name else ""
        context.out(f"没有已入库的 MCP 资源{scope}。")
        context.note("资源入索引需要在配置里开 index_resources（`orbit mcp add` 后手改 data/mcp.json）。")
        context.emit({"ok": True, "resources": [], "count": 0})
        return 0
    context.table(
        ("资源", "服务", "块数", "最近写入"),
        [
            (truncate(str(origin), 56), str(host), str(count), str(created)[:16])
            for origin, host, count, created in rows
        ],
    )
    context.emit(
        {
            "ok": True,
            "count": len(rows),
            "resources": [
                {"uri": str(origin), "host": str(host), "chunks": int(count), "created_at": str(created)}
                for origin, host, count, created in rows
            ],
        }
    )
    return 0


# ── mcp prompts ─────────────────────────────────────────────────────────────


def run_prompts(context: Context, args) -> int:
    """`mcp prompts`：列出服务声明了哪些 prompt（`mcp__<服务>__<prompt>` 命名）。

    这一步要**连服务**（prompt 清单只在线上），所以它是显式的"刷新动作"，不是技能
    目录的读路径——技能目录读的是缓存，见 `app/mcp_prompts.py` 的桥接说明。
    """
    from app.mcp_prompts import prompt_skill_name

    name = str(getattr(args, "name", "") or "").strip()
    manager = _manager(context)
    entries = manager.servers()
    if name:
        entries = [_find(manager, name)]
    entries = [entry for entry in entries if entry.get("enabled", True)]
    if not entries:
        context.out("没有启用的 MCP 服务可查。")
        context.emit({"ok": True, "prompts": [], "count": 0})
        return 0
    rows: list[dict[str, Any]] = []
    failed: list[dict[str, str]] = []
    for entry in entries:
        server = str(entry.get("name"))
        report = context.run(probe(context.settings, entry))
        if not report["ok"]:
            failed.append({"server": server, "error": report["error"]})
            continue
        for prompt in report["prompts"]:
            rows.append(
                {
                    "server": server,
                    "prompt": prompt["name"],
                    "skill": prompt_skill_name(server, prompt["name"]),
                    "description": prompt["description"],
                    "arguments": prompt["arguments"],
                }
            )
    if rows:
        context.table(
            ("技能名", "服务", "prompt", "参数", "说明"),
            [
                (
                    row["skill"],
                    row["server"],
                    row["prompt"],
                    ",".join(item["name"] for item in row["arguments"]) or "-",
                    truncate(row["description"], 50),
                )
                for row in rows
            ],
        )
    else:
        context.out("（没有服务声明 prompts）")
    for item in failed:
        context.warn(f"{item['server']} 探测失败：{item['error']}")
    context.emit({"ok": True, "prompts": rows, "count": len(rows), "failed": failed})
    return 0 if not failed else 1


# ── mcp auth（token 存取 + 过期检查）─────────────────────────────────────────


def run_auth_status(context: Context, args) -> int:
    """`mcp auth status`：每个 OAuth 服务的 token 状态与到期时间。"""
    name = str(getattr(args, "name", "") or "").strip()
    manager = _manager(context)
    store = OAuthStore.for_settings(context.settings)
    names = [name] if name else _oauth_servers(manager)
    if not names:
        context.out("配置里没有声明 auth=oauth 的服务。")
        context.note("添加：orbit mcp add <name> --url <端点> --auth oauth")
        context.emit({"ok": True, "tokens": [], "count": 0})
        return 0
    rows = [store.status(item) for item in names]
    context.table(
        ("服务", "状态", "到期", "剩余"),
        [
            (
                row["server"],
                AUTH_STATE_LABELS.get(row["state"], row["state"]),
                _iso(row["expires_at"]),
                "-" if row["seconds_left"] is None else f"{int(row['seconds_left'])} 秒",
            )
            for row in rows
        ],
    )
    if any(row["state"] == "missing" for row in rows):
        context.note()
        context.note("写入 token：orbit mcp auth set <name>（从 stdin 读，不进 shell 历史）")
    context.emit({"ok": True, "tokens": rows, "count": len(rows)})
    return 0


def run_auth_set(context: Context, args) -> int:
    """`mcp auth set`：把 access token 写进 keyring（服务名 ``orbit:<data_dir>``）。

    从 **stdin** 读而不是命令行参数：命令行会进 shell 历史与进程列表，token 泄漏的
    最常见路径就是这个。TTY 下用 ``getpass`` 不回显。
    """
    name = str(getattr(args, "name", "") or "").strip()
    manager = _manager(context)
    _find(manager, name)
    if sys.stdin.isatty():
        token = getpass.getpass("access token（不回显）：").strip()
    else:
        token = sys.stdin.readline().strip()
    if not token:
        raise CliError("没有读到 token（stdin 为空）")
    expires_in = getattr(args, "expires_in", None)
    ttl = 0.0
    if expires_in not in (None, ""):
        ttl = _seconds(expires_in, "--expires-in")
    record = OAuthStore.for_settings(context.settings).set(
        name, token, refresh_token=str(getattr(args, "refresh_token", "") or ""), expires_in=ttl
    )
    context.out(f"已写入 {name} 的 token（到期：{_iso(record.expires_at)}）")
    if not record.expires_at:
        context.note("没给 `--expires-in`，到期时间记为未知（不会因此判成过期）。")
    context.emit({"ok": True, "server": name, "expires_at": record.expires_at})
    return 0


def run_auth_clear(context: Context, args) -> int:
    """`mcp auth clear`：删掉 keyring 里的 OAuth token。"""
    name = str(getattr(args, "name", "") or "").strip()
    manager = _manager(context)
    _find(manager, name)
    removed = OAuthStore.for_settings(context.settings).clear(name)
    if removed:
        context.out(f"已删除 {name} 的 OAuth token")
    else:
        context.out(f"{name} 本来就没有 OAuth token")
    context.emit({"ok": True, "server": name, "removed": removed})
    return 0


def run_auth_login(context: Context, args) -> int:
    """`mcp auth login`：真 OAuth 流程。本轮如实报未实现。"""
    name = str(getattr(args, "name", "") or "").strip()
    manager = _manager(context)
    _find(manager, name)
    raise CliError(_auth_note(name), code=UNIMPLEMENTED_CODE)


# ── 把实现接到声明表上 ──────────────────────────────────────────────────────


def _bind_path(path: str, handler) -> None:
    """登记某条命令路径的实现，两种导入顺序都成立（照 `curator.py` 的 `_bind`）。

    生产接线里本模块排在 `hermes_parity` **之前**，`register_handler` 生效；而直接
    ``import app.cli.commands.mcp`` 会先跑完包初始化，此时声明表已组装完毕，
    ``HANDLERS`` 里的登记不会回填到已构建的 `Command` 上（它会停在占位 handler）。
    所以补一次回填。
    """
    register_handler(path, handler)
    parts = path.split()
    node = REGISTRY.get(parts[0])
    for part in parts[1:]:
        if node is None:
            return
        node = node.child(part)
    if node is not None and not node.subcommands:
        node.handler = handler
        node.state = "done"


def _extra_commands() -> list[Command]:
    """本波次新增的 6 条子命令（声明表里没有位置，靠 :func:`extend_command` 追加）。

    每条都带 `hermes=`：`Registry.parity_rows()` 要求每一行都有对标依据，留空会在
    `test_parity_rows_cover_subcommands` 里当场红。
    """
    return [
        Command(
            name="enable",
            help="启用一个 MCP 服务（下次装配时连接）",
            handler=run_enable,
            group="ext",
            hermes="mcp enable",
            parity="P1",
            state="done",
            options=[to_option("name", "服务名")],
        ),
        Command(
            name="disable",
            help="禁用一个 MCP 服务（保留配置）",
            handler=run_disable,
            group="ext",
            hermes="mcp disable",
            parity="P1",
            state="done",
            options=[to_option("name", "服务名")],
        ),
        Command(
            name="reload",
            help="重新校验配置并清空资源索引的增量状态",
            handler=run_reload,
            group="ext",
            hermes="mcp reload",
            parity="P1",
            state="done",
        ),
        Command(
            name="resources",
            help="列出已入库的 MCP 资源（chunks 表）",
            handler=run_resources,
            group="ext",
            hermes="mcp resources",
            parity="P1",
            state="done",
            options=[to_option("[name]", "只看某个服务")],
            needs_store=True,
        ),
        Command(
            name="prompts",
            help="列出 MCP 服务声明的 prompts（mcp__<服务>__<prompt>）",
            handler=run_prompts,
            group="ext",
            hermes="mcp prompts",
            parity="P1",
            state="done",
            options=[to_option("[name]", "只看某个服务")],
        ),
        Command(
            name="auth",
            help="MCP OAuth token 的存取与过期检查（授权流程未实现）",
            group="ext",
            hermes="mcp auth",
            parity="P1",
            state="done",
            subcommands=[
                Command(
                    name="status",
                    help="查看 token 状态与到期时间",
                    handler=run_auth_status,
                    group="ext",
                    hermes="mcp auth status",
                    parity="P1",
                    state="done",
                    options=[to_option("[name]", "只看某个服务")],
                ),
                Command(
                    name="set",
                    help="把 access token 写进 keyring（从 stdin 读）",
                    handler=run_auth_set,
                    group="ext",
                    hermes="mcp auth set",
                    parity="P1",
                    state="done",
                    options=[
                        to_option("name", "服务名"),
                        to_option("--expires-in EXPIRES_IN", "有效期秒数（OAuth 的 expires_in；不给=到期时间未知）"),
                        to_option("--refresh-token REFRESH_TOKEN", "刷新令牌（可选）"),
                    ],
                ),
                Command(
                    name="clear",
                    help="删除 keyring 里的 OAuth token",
                    handler=run_auth_clear,
                    group="ext",
                    hermes="mcp auth clear",
                    parity="P1",
                    state="done",
                    options=[to_option("name", "服务名")],
                ),
                Command(
                    name="login",
                    help="走完整 OAuth 授权流程（本轮未实现，如实报错）",
                    handler=run_auth_login,
                    group="ext",
                    hermes="mcp auth login",
                    parity="P1",
                    state="done",
                    options=[to_option("name", "服务名")],
                ),
            ],
        ),
    ]


def extend_command(command: Command) -> list[str]:
    """把本模块新增的子命令挂到已有的 `mcp` Command 上（**幂等**，返回新增的名字）。

    追加而不是替换：表里 11 条一条不少，`picker`/`catalog`/`install` 继续显示为未实现。
    顺带把 `add --env` 改成可重复——声明表的 help 写的是"可重复"，但短字符串语法只能
    表达一个值，改之前那句话是假的（改完 `--env` 真的能重复给）。
    """
    existing = {sub.name for sub in command.subcommands}
    added: list[str] = []
    for sub in _extra_commands():
        if sub.name in existing:
            continue
        command.subcommands.append(sub)
        added.append(sub.name)
    for sub in command.subcommands:
        if sub.name != "add":
            continue
        patched: list[Option] = []
        for item in sub.options:
            if item.flags == ("--env",):
                # 原来的 `--env ENV` 只能留最后一个值，与 help 的"可重复"不符。
                patched.append(
                    Option(("--env",), item.help, action="append", metavar="ENV")
                )
            else:
                patched.append(item)
        sub.options = patched
    return added


def _extend_if_present() -> list[str]:
    """`mcp` Command 已在注册表里就追加，否则什么都不做（返回新增的名字）。"""
    command = REGISTRY.get("mcp")
    if command is None:
        return []
    return extend_command(command)


def wrap_register_specs(table_module, after) -> bool:
    """把 ``table_module.register_specs`` 包一层：声明表组装完立刻调 ``after()``。

    这是"本模块在 `hermes_parity` 之前导入"时唯一能用的接缝。为什么必须这么绕：

    - ``register_handler`` 只能填**表里已有的路径**，而 `enable`/`disable`/`reload`/
      `resources`/`prompts`/`auth` 在表里没有位置；
    - 另立一个顶层 `mcp` 命令会让 ``register_specs`` 整条跳过表里那条，`picker`/
      `catalog`/`install` 三条未实现的子命令当场从 `--help` 里消失；
    - 而追加子命令需要一个**已存在**的 `mcp` Command，它要等 ``register_specs`` 跑完。

    所以只能等那个时刻。**幂等**：已经包过就返回 False，不重复包（重复包会让
    ``after`` 被调多次，也可能把 wrapper 套成洋葱）。

    ``table_module`` 与 ``after`` 都是参数而不是闭包里的全局量，是为了这条机制能被单测
    直接驱动（否则只能在特定导入顺序下才跑到）。
    """
    original = table_module.register_specs
    if getattr(original, "_orbit_mcp_wrapped", False):
        return False

    def wrapped(specs):
        skipped = original(specs)
        after()
        return skipped

    wrapped._orbit_mcp_wrapped = True  # type: ignore[attr-defined]
    table_module.register_specs = wrapped
    return True


def _install() -> None:
    """把新增子命令挂上去；`mcp` 还不存在就推迟到声明表组装完（见模块 docstring）。"""
    if REGISTRY.get("mcp") is not None:
        _extend_if_present()
        return
    wrap_register_specs(_table, _extend_if_present)


#: 声明表里本模块**没接**的路径。写在这里是为了让"未实现"可被 grep 到，而不是靠记忆。
UNBOUND_SUBCOMMANDS: tuple[str, ...] = (
    # 交互式目录选择器：需要 TUI，CLI 无交互层。
    "mcp picker",
    # 官方目录：Orbit 没有内置 MCP 目录（Hermes 的 optional-mcps 63 条模板，矩阵 8.10）。
    "mcp catalog",
    # 目录安装：依赖 catalog，同上。
    "mcp install",
)


_bind_path("mcp list", run_list)
_bind_path("mcp add", run_add)
_bind_path("mcp remove", run_remove)
_bind_path("mcp test", run_test)
_bind_path("mcp configure", run_configure)
_bind_path("mcp login", run_login)
_bind_path("mcp reauth", run_reauth)
_bind_path("mcp serve", run_serve)
_install()
