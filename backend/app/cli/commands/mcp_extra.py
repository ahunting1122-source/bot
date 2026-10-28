"""`orbit mcp` 的补充实现：目录 / 选择器 / 安装 / schema 缓存（矩阵 8.10 + 8.7）。

本模块接的是**声明表里还停在 ``todo`` 的三条路径**（``catalog`` / ``picker`` /
``install``，用 ``register_handler``，与 ``mcp.py`` 同一套做法），外加一条表里没有位置的
``mcp cache``（靠 :func:`extend_command` 追加，理由见 ``app/cli/commands/mcp.py`` 的
模块 docstring：另立顶层命令会让表里未实现的子命令从 ``--help`` 里消失）。

## 避开 ``mcp.py`` 已接管的路径

``mcp.py`` 已经接了 11 条里的 8 条（``list`` / ``add`` / ``remove`` / ``test`` /
``configure`` / ``login`` / ``reauth`` / ``serve``）并追加了 6 条（``enable`` /
``disable`` / ``reload`` / ``resources`` / ``prompts`` / ``auth``）。本模块**不碰**它们
——重复 ``register_handler`` 会直接抛 ``ValueError``，那是设计上的提醒，不是意外。

## 为什么还要改 ``login`` / ``reauth`` 的 handler

``mcp.py`` 的 ``run_login`` / ``run_reauth`` 只报"OAuth 未实现"。矩阵 8.3 收尾后，
``app/mcp_oauth_flow.py`` 已经有能真跑的本地部分（读令牌 / 判过期 / 拼 authorize URL），
所以这两条命令**不该继续什么都不做**。``register_handler`` 对已登记的路径会抛错，因此
这里走"**在已构建的 Command 节点上换 handler**"（``mcp.py`` 自己的 ``_bind_path`` 用的
也是这条路）。换上去的实现是原行为的**超集**：原来那句"未实现 + 手工通道"仍然在里面，
只是前面多了三段真结论。``UNBOUND_SUBCOMMANDS`` 里不再列它们。

## 三处如实说明（不假装）

1. ``picker`` **不弹交互式选择器**：Orbit 的 CLI 没有 TTY 交互层，做出来会是个选不动的
   勾选框。这里给编号清单 + "用 `orbit mcp install <名称>` 选它"。
2. ``install`` **只写配置、不装包**：本机无网络，``npx`` / ``uvx`` / ``pip`` 都跑不了。
   返回值固定带 ``needs_network=True``，CLI 把它原样打给用户。
3. ``login`` / ``reauth`` 里真正需要出网/浏览器的那一步仍然报"缺什么"，而不是假装成功。
"""

from __future__ import annotations

import app.cli.commands._table as _table
from app import mcp_catalog
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context, truncate
from app.cli.shorthand import to_option
from app.cli.spec import REGISTRY, Command
from app.mcp import MCPManager, oauth_login_report, oauth_refresh
from app.mcp_oauth_flow import NetworkUnavailable
from app.mcp_schema_cache import DEFAULT_TTL_SECONDS, SchemaCache

#: "未实现"的退出码。与 `_table._pending` / `mcp.py` 一致，脚本可据此区分"没做"和"跑失败了"。
UNIMPLEMENTED_CODE = 3
#: `mcp cache prune` 的默认阈值：7 天。缓存条目过期后读不到，留 7 天是给"临时停用某个
#: 服务又启用回来"留余量，再长就只是在文件里堆垃圾。
DEFAULT_PRUNE_AGE = 7 * 24 * 3600.0
#: token 状态的中文标签（与 `mcp.py` 的 `AUTH_STATE_LABELS` 同义，这里只需要这三态）。
STATE_LABELS = {"missing": "未配置", "expired": "已过期", "expiring": "即将过期", "valid": "有效"}


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _cache(context: Context) -> SchemaCache:
    return SchemaCache.for_settings(context.settings)


def _manager(context: Context) -> MCPManager:
    return MCPManager(context.settings)


def _find(manager: MCPManager, name: str) -> dict | None:
    """按名找服务配置；找不到返回 None（由调用方决定报错文案）。"""
    for entry in manager.servers():
        if str(entry.get("name")) == name:
            return entry
    return None


def _iso(epoch: float) -> str:
    """epoch 秒 → 本地时间；0 表示"服务没给到期时间"，显示成"未知"。"""
    if not epoch:
        return "未知"
    from datetime import datetime

    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "未知"


def _age(seconds: float | None) -> str:
    """秒 → 人能读的时长（缓存年龄用）。``None`` 表示条目坏了、读不出年龄。"""
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{int(seconds)} 秒"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分"
    if seconds < 86400:
        return f"{seconds / 3600:.1f} 小时"
    return f"{seconds / 86400:.1f} 天"


def _catalog_row(entry: mcp_catalog.CatalogEntry) -> tuple[str, ...]:
    """目录条目在表格里的一行。"""
    state = "抄不全" if entry.unknown else "可装"
    return (
        entry.name,
        entry.transport,
        truncate(entry.target or "-", 52),
        entry.auth,
        state,
    )


# ── mcp catalog ─────────────────────────────────────────────────────────────


def run_catalog(context: Context, args) -> int:
    """`mcp catalog`：列出目录里的全部模板（Hermes 的 optional-mcps）。

    列表本身就是交付物：用户要靠它知道自己能装什么、装的是远程还是本地、要不要登录。
    所以**连 unknown 的条目也列**（标"抄不全"）——把抄不全的藏起来，等于让人以为目录里
    没有这个服务。
    """
    rows = mcp_catalog.list_catalog()
    stats = mcp_catalog.summary()
    context.table(
        ("名称", "传输", "目标", "鉴权", "状态"),
        [_catalog_row(entry) for entry in mcp_catalog.CATALOG],
    )
    context.note()
    context.note(
        f"共 {stats['count']} 条：远程 {stats['http']} / 本地 {stats['stdio']}，"
        f"需要 OAuth 的 {stats['oauth']} 条，抄不全的 {stats['unknown']} 条。"
    )
    context.note("安装：orbit mcp install <名称>（只写配置，首次连接才需要网络与依赖）")
    for entry in mcp_catalog.CATALOG:
        if entry.unknown:
            context.note(f"  {entry.name} 抄不全：{entry.note}")
    context.emit({"ok": True, **stats, "entries": rows})
    return 0


# ── mcp picker ──────────────────────────────────────────────────────────────


def run_picker(context: Context, args) -> int:
    """`mcp picker`：**不做**交互式选择器，给编号清单 + 可执行的下一步。

    Hermes 这里是 TUI 勾选器。Orbit 的 CLI 非交互，硬做一个"选不动的勾选框"是最糟的
    一种交付：看起来能用，按下去什么都不发生。所以这里把"挑"这件事降级成"看编号 + 按名
    安装"，并把这条路径写清楚。
    """
    context.out(f"目录里有 {len(mcp_catalog.CATALOG)} 条可装的 MCP 服务：")
    context.out()
    width = len(str(len(mcp_catalog.CATALOG)))
    for index, entry in enumerate(mcp_catalog.CATALOG, 1):
        mark = "（抄不全）" if entry.unknown else ""
        context.out(
            f"  {index:>{width}}. {entry.name:<18} {entry.transport:<5} "
            f"{truncate(entry.description, 56)}{mark}"
        )
    context.out()
    context.out("挑中哪条就用它的名字安装：orbit mcp install <名称>")
    context.warn(
        "交互式选择器未实现：Orbit 的 CLI 没有 TTY 交互层，做出来是个选不动的勾选框。"
        "请用编号对应的名字安装：orbit mcp install <名称>"
    )
    context.emit(
        {
            "ok": True,
            "interactive": False,
            "count": len(mcp_catalog.CATALOG),
            "entries": mcp_catalog.list_catalog(),
        }
    )
    return 0


# ── mcp install ─────────────────────────────────────────────────────────────


def run_install(context: Context, args) -> int:
    """`mcp install <identifier>`：把目录里的一条写进 `data/mcp.json`。

    **不装包、不发网络请求**。写完立刻说明"首次连接需要网络与依赖"，否则用户会以为
    `orbit mcp test <name>` 马上就能通。抄不全的条目（``unknown=True``）拒绝安装并说明
    原因——写一条跑不起来的配置比报错更糟。
    """
    identifier = str(getattr(args, "identifier", "") or "").strip()
    if not identifier:
        raise CliError("需要目录条目名：orbit mcp install <名称>（用 `orbit mcp catalog` 查）")
    entry = mcp_catalog.show(identifier)
    if entry is None:
        raise CliError(
            f"目录里没有 {identifier!r}。用 `orbit mcp catalog` 看可用的 "
            f"{len(mcp_catalog.CATALOG)} 条。"
        )
    if entry.unknown:
        raise CliError(
            f"目录条目 {entry.name} 抄不到可用的连接方式：{entry.note or '（未说明）'}。"
            "手动添加：orbit mcp add <name> --command <可执行文件> --args \"…\"",
            code=UNIMPLEMENTED_CODE,
        )
    try:
        result = mcp_catalog.install(context.settings, identifier)
    except mcp_catalog.CatalogError as exc:
        raise CliError(str(exc)) from None
    context.out(f"已把 {result['name']} 的配置写入 data/mcp.json（{result['target']}）")
    context.warn(
        "只写了配置，**没有安装任何依赖、也没有发网络请求**："
        "首次连接时需要网络与依赖（远程服务还要完成 OAuth 登录）。"
    )
    if result["auth"] == "oauth":
        context.note(f"下一步：orbit mcp login {result['name']}（需要网络与浏览器授权）")
    else:
        context.note(f"下一步：orbit mcp test {result['name']}（会真连一次）")
    context.emit(result)
    return 0


# ── mcp cache ───────────────────────────────────────────────────────────────


def run_cache_show(context: Context, args) -> int:
    """`mcp cache show`：看看 schema 缓存里有什么、还新鲜吗。

    **缓存的是 schema 不是结果**：这里列的是"每个服务声明了哪些工具"的磁盘副本，
    与任何一次工具调用的返回值无关。过期的条目仍会列出来（标"否"），因为
    `mcp cache prune` 才是清理入口——读路径顺手删条目会让并发读互相踩。
    """
    cache = _cache(context)
    rows = cache.entries()
    if not rows:
        context.out(f"schema 缓存是空的（{cache.path}）。")
        context.note("缓存会在连上服务并列出工具后自动写入；`orbit mcp test <name>` 也会刷新它。")
        context.emit({"ok": True, "path": str(cache.path), "entries": [], "count": 0})
        return 0
    context.table(
        ("服务", "工具", "年龄", "新鲜", "服务版本", "TTL"),
        [
            (
                str(row["server"]),
                str(row["tools"]),
                _age(row["age"]),
                "是" if row["fresh"] else "否",
                truncate(str(row["version"]) or "-", 18),
                "-" if row["ttl_ms"] is None else f"{float(row['ttl_ms']) / 1000:g} 秒",
            )
            for row in rows
        ],
    )
    context.note()
    context.note(f"文件：{cache.path}")
    context.note(
        f"兜底 TTL {DEFAULT_TTL_SECONDS / 3600:g} 小时（服务自己给了 ttlMs 时以服务为准）。"
    )
    context.emit({"ok": True, "path": str(cache.path), "entries": rows, "count": len(rows)})
    return 0


def run_cache_clear(context: Context, args) -> int:
    """`mcp cache clear`：清空 schema 缓存（下次连接会重新列一次工具）。"""
    cache = _cache(context)
    name = str(getattr(args, "name", "") or "").strip()
    if name:
        removed = cache.invalidate(name)
        context.out(f"已清除 {name} 的 schema 缓存" if removed else f"{name} 本来就没有缓存")
        context.emit({"ok": True, "server": name, "removed": removed})
        return 0
    count = cache.clear()
    context.out(f"已清空 schema 缓存（{count} 条）")
    context.note("下次连接每个服务时会重新列一次工具；这不影响任何已注册的工具。")
    context.emit({"ok": True, "removed": count})
    return 0


def run_cache_prune(context: Context, args) -> int:
    """`mcp cache prune`：删掉老条目（默认 7 天）。

    与 TTL 的分工：TTL 决定"还能不能用"，``prune`` 决定"还留不留着"。真正需要它的是
    **已经从配置里删掉的服务**——那些条目再也不会被读，只会在文件里堆积。
    """
    raw = getattr(args, "max_age", None)
    max_age = DEFAULT_PRUNE_AGE
    if raw not in (None, ""):
        try:
            max_age = float(str(raw))
        except (TypeError, ValueError):
            raise CliError(f"`--max-age` 需要秒数，收到 {raw!r}") from None
        if max_age <= 0:
            raise CliError("`--max-age` 必须大于 0（0 会把刚写的条目也删掉）")
    cache = _cache(context)
    removed = cache.prune(max_age)
    if removed:
        context.out(f"已删除 {len(removed)} 条超过 {_age(max_age)} 的缓存：{', '.join(removed)}")
    else:
        context.out(f"没有超过 {_age(max_age)} 的缓存条目。")
    context.emit({"ok": True, "removed": removed, "max_age": max_age})
    return 0


# ── mcp login / reauth（换成"至少做点真事"的实现）────────────────────────────


def _auth_note(name: str) -> str:
    """真正做不到的那一步，逐条说清缺什么（不是一句"未实现"）。"""
    return (
        "换 token 这一步做不到，缺的是：出网访问授权服务器（发现元数据 + 动态客户端注册）"
        "、在本机回环端口起回调服务器、以及一个能打开浏览器的真人会话。"
        f"已有的通道：`orbit mcp auth set {name}` 手工写入 token（走 keyring），"
        f"`orbit mcp auth status {name}` 查过期时间。"
    )


def run_login(context: Context, args) -> int:
    """`mcp login <name>`：查令牌 → 能刷新就刷 → 打印 authorize URL → 说明还缺什么。

    与旧实现的区别：旧版只有最后一句"未实现"。现在前三步都是本地计算，能真给出结论
    （令牌还有效吗？能不能刷新？真流程会打开哪条 URL），最后一步仍然如实报缺项。
    """
    name = str(getattr(args, "name", "") or "").strip()
    manager = _manager(context)
    entry = _find(manager, name)
    if entry is None:
        known = ", ".join(str(item.get("name")) for item in manager.servers()) or "（空）"
        raise CliError(f"没有名为 {name!r} 的 MCP 服务。已配置：{known}")
    report = oauth_login_report(context.settings, entry)
    if str(entry.get("auth") or "").lower() != "oauth":
        context.warn(
            f"{name} 的配置里没有 auth=oauth（当前：{entry.get('auth') or '未声明'}）。"
            "重新鉴权前先 `orbit mcp remove` 再用 `--auth oauth` 添加。"
        )
    context.out(
        f"{name} 的 OAuth token：{STATE_LABELS.get(report['state'], report['state'])}"
        f"（到期：{_iso(report['expires_at'])}）"
    )
    for step in report["steps"]:
        context.note(f"  [{'✓' if step['done'] else ' '}] {step['step']}：{step['detail']}")
    if report["authorize_url"]:
        context.out()
        context.out("真流程会打开这条授权 URL（PKCE S256，state 已生成）：")
        context.out(f"  {report['authorize_url']}")
    elif report["authorize_url_error"]:
        context.note(f"拼不出授权 URL：{report['authorize_url_error']}")
    context.emit({"ok": False, **report})
    raise CliError(_auth_note(name), code=UNIMPLEMENTED_CODE)


def run_reauth(context: Context, args) -> int:
    """`mcp reauth [--all] [name]`：逐个服务走"能刷新就刷、否则报缺什么"。"""
    name = str(getattr(args, "name", "") or "").strip()
    manager = _manager(context)
    if name:
        entry = _find(manager, name)
        if entry is None:
            raise CliError(f"没有名为 {name!r} 的 MCP 服务。")
        entries = [entry]
    else:
        entries = [
            entry
            for entry in manager.servers()
            if str(entry.get("auth") or "").lower() == "oauth"
        ]
        if not entries:
            raise CliError(
                "配置里没有声明 auth=oauth 的服务。"
                "（`orbit mcp add <name> --url … --auth oauth` 会写下这个字段）"
            )
        context.out(
            f"要对 {len(entries)} 个 OAuth 服务重新鉴权："
            + ", ".join(str(entry.get("name")) for entry in entries)
        )
    failed: list[dict] = []
    for entry in entries:
        server = str(entry.get("name") or "")
        try:
            result = oauth_refresh(context.settings, entry)
        except NetworkUnavailable as exc:
            # 刷新要出网：把"缺什么"原样打出来，而不是把它吞成一句"失败"。
            context.warn(f"{server} 刷新失败：{exc}")
            failed.append({"server": server, "error": str(exc)})
            continue
        if result["refreshed"]:
            context.out(f"{server} 已刷新（到期：{_iso(result['expires_at'])}）")
        elif result["reason"] == "valid":
            context.out(f"{server} 的令牌仍然有效（到期：{_iso(result['expires_at'])}），无需刷新")
        elif result["reason"] == "missing":
            context.note(f"{server} 没有 token：{_auth_note(server)}")
        else:
            context.note(f"{server} 没有 refresh token：需要重新授权（{_auth_note(server)}）")
    servers = [str(entry.get("name") or "") for entry in entries]
    context.emit({"ok": not failed, "servers": servers, "failed": failed})
    if failed:
        raise CliError(f"{len(failed)} 个服务的令牌需要重新授权，而刷新需要网络。", code=UNIMPLEMENTED_CODE)
    return 0


# ── 把实现接到声明表上 ──────────────────────────────────────────────────────


def _bind_path(path: str, handler) -> None:
    """登记某条命令路径的实现，并在表已组装时**回填**到已构建的节点上。

    照 `mcp.py` 的 `_bind_path`：生产接线里本模块排在 `hermes_parity` 之前，
    ``register_handler`` 生效；直接 ``import app.cli.commands.mcp_extra`` 时表已组装完，
    ``HANDLERS`` 的登记不会回填到已构建的 ``Command``，所以要补一次。
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


def _cache_command() -> Command:
    """`mcp cache` 子命令组（声明表里没有位置，靠 :func:`extend_command` 追加）。

    三条都用 ``hermes=`` 标了对标依据：``Registry.parity_rows()`` 要求每行都有，
    留空会在 `test_parity_rows_cover_subcommands` 里当场红。
    """
    return Command(
        name="cache",
        help="MCP 工具 schema 的磁盘缓存（缓存的是 schema，不是调用结果）",
        group="ext",
        hermes="mcp cache",
        parity="P1",
        state="done",
        subcommands=[
            Command(
                name="show",
                help="列出缓存里的服务、工具数与新鲜度",
                handler=run_cache_show,
                group="ext",
                hermes="mcp cache show",
                parity="P1",
                state="done",
            ),
            Command(
                name="clear",
                help="清空 schema 缓存（可指定单个服务）",
                handler=run_cache_clear,
                group="ext",
                hermes="mcp cache clear",
                parity="P1",
                state="done",
                options=[to_option("[name]", "只清某个服务")],
            ),
            Command(
                name="prune",
                help="删掉超过指定年龄的缓存条目",
                handler=run_cache_prune,
                group="ext",
                hermes="mcp cache prune",
                parity="P1",
                state="done",
                options=[to_option("--max-age MAX_AGE", "年龄阈值（秒），默认 7 天")],
            ),
        ],
    )


def extend_command(command: Command) -> list[str]:
    """往已构建的 `mcp` Command 上追加本模块的子命令并回填 handler（**幂等**）。

    追加而不是替换：表里 11 条一条不少。``login`` / ``reauth`` 是**换 handler**（不是换
    节点），因为矩阵 8.3 收尾后这两条有了能真跑的一半，不该继续只报"未实现"。
    """
    added: list[str] = []
    existing = {sub.name for sub in command.subcommands}
    if "cache" not in existing:
        command.subcommands.append(_cache_command())
        added.append("cache")
    for name, handler in (("login", run_login), ("reauth", run_reauth)):
        node = command.child(name)
        if node is None or node.handler is handler:
            continue
        node.handler = handler
        node.state = "done"
        added.append(f"{name}（换实现）")
    return added


def _extend_if_present() -> list[str]:
    """`mcp` Command 已在注册表里就扩展，否则什么都不做。"""
    command = REGISTRY.get("mcp")
    if command is None:
        return []
    return extend_command(command)


def mount_mcp_extras() -> list[str]:
    """两种导入顺序都成立的挂载入口（幂等，返回这次做了什么）。

    - 表还没组装（生产接线）：包一层 ``_table.register_specs``，等表组装完立刻扩展；
    - 表已组装（测试里直接 import）：模块体里已经调过一次，这里再调也是幂等的。
    """
    added = _extend_if_present()
    if REGISTRY.get("mcp") is not None:
        return added
    original = _table.register_specs
    if getattr(original, "_orbit_mcp_extra_wrapped", False):
        return []

    def wrapped(specs):
        skipped = original(specs)
        _extend_if_present()
        return skipped

    wrapped._orbit_mcp_extra_wrapped = True  # type: ignore[attr-defined]
    _table.register_specs = wrapped
    return ["（已挂载：表组装后扩展 mcp 子命令）"]


#: 声明表里本模块**没接**的路径。写在这里是为了让"未实现"可被 grep 到，而不是靠记忆。
#: 本轮把 ``catalog`` / ``picker`` / ``install`` 都接掉了，所以这里为空；``login`` /
#: ``reauth`` 换了实现（见 :func:`extend_command`），也不在未接清单里。
UNBOUND_SUBCOMMANDS: tuple[str, ...] = ()


_bind_path("mcp catalog", run_catalog)
_bind_path("mcp picker", run_picker)
_bind_path("mcp install", run_install)
mount_mcp_extras()


__all__ = [
    "UNBOUND_SUBCOMMANDS",
    "extend_command",
    "mount_mcp_extras",
    "run_cache_clear",
    "run_cache_prune",
    "run_cache_show",
    "run_catalog",
    "run_install",
    "run_login",
    "run_picker",
    "run_reauth",
]
