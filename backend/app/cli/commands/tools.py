"""`orbit tools` 命令：按平台查看 / 启用 / 禁用工具（对标 Hermes 的 `hermes tools`）。

命令面照 ``handoff/hermes-parity/research/_raw/sub/tools__*.txt`` 抄：

    orbit tools list   [--platform PLATFORM]
    orbit tools enable [--platform PLATFORM] NAME [NAME ...]
    orbit tools disable[--platform PLATFORM] NAME [NAME ...]
    orbit tools post-setup
    orbit tools --summary

三处刻意的取舍（都是"为什么"）：

1. **名字必须认得**。``enable websearch`` 这种拼错必须当场报错并给候选。静默接受
   然后什么都不发生，是这类命令最坏的失败方式——用户以为关掉了，其实全开着。
2. **写的是"禁用集"**，不是"启用集"（除非本来就在白名单模式）。默认全开 + 只记减法，
   新增工具才不需要回头改每一条历史配置。
3. **``post-setup`` 输出可粘贴的 JSON 快照**。Hermes 那个跑的是供应商安装钩子（Chromium /
   cua-driver / ddgs …），本机无网络、也不许装依赖，照抄只会得到一个必然失败的命令。
   于是保留它的**语义定位**——"给 dashboard / 脚本用的稳定、非交互产物"——把产物换成
   当前平台的启停快照：贴回 ``data/tools.json`` 就能复现同一份暴露面。
"""

from __future__ import annotations

import importlib
import json
import logging

from app import toolsets
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option

log = logging.getLogger("orbit.cli.tools")

#: 平台选项的说明文案。三个子命令共用，改一处即可，避免措辞漂移。
PLATFORM_HELP = "平台（默认 cli；telegram / discord 等按需扩展）"


# ── 工具目录 ────────────────────────────────────────────────────────────────


def _catalog_names() -> list[str]:
    """当前工具目录 = 声明表 ∪ 四张真实工具表。

    为什么两边都取：声明表（``app/toolsets.py``）是**归属**的权威，真实工具表是
    **存在性**的权威。只信声明表会漏掉"代码里加了工具但忘了登记"（这正是要暴露的
    问题），只信工具表则拿不到 ``ask_user`` 这种由图节点注入的工具。

    四张表任一导入失败（缺 playwright 之类的可选依赖）都只跳过它，不让 `tools list`
    整个失败——一个装不全的环境更需要看到"哪些工具在"。
    """
    names = set(toolsets.declared_tool_names())
    for module_name, attribute in (
        ("app.tools", "LOCAL_TOOLS"),
        ("app.browser", "BROWSER_TOOLS"),
        ("app.connectors", "GITHUB_TOOLS"),
        ("app.web", "WEB_TOOLS"),
    ):
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:  # noqa: BLE001 - 可选依赖缺失不该拖垮命令面
            log.warning("跳过 %s：%s", module_name, exc)
            continue
        for tool in getattr(module, attribute, None) or []:
            names.add(tool.name)
    return sorted(names)


def _platform(args) -> str:
    """取 ``--platform``，缺省回落到默认平台。"""
    return str(getattr(args, "platform", None) or toolsets.DEFAULT_PLATFORM).strip()


def _config(context: Context) -> dict:
    return toolsets.load_config(toolsets.config_path(context.data_dir))


# ── list ────────────────────────────────────────────────────────────────────


def run_list(context: Context, args) -> int:
    platform = _platform(args)
    config = _config(context)
    names = _catalog_names()
    rows = toolsets.describe(platform, config, names)
    exposed = [row for row in rows if row["exposed"]]
    hidden = [row for row in rows if not row["exposed"]]

    context.out(f"工具目录（平台：{platform}）")
    context.out()
    context.table(
        ("工具", "toolset", "暴露", "说明"),
        [
            (
                row["name"],
                row["toolset"],
                "是" if row["exposed"] else "否",
                row["reason"] or row["note"] or "",
            )
            for row in rows
        ],
    )
    context.out()
    context.out(f"共 {len(rows)} 个工具，暴露 {len(exposed)} 个，未暴露 {len(hidden)} 个。")
    unclassified = [row["name"] for row in rows if row["toolset"] == toolsets.UNCLASSIFIED]
    if unclassified:
        # 不静默：未归类的工具会永远躺在模型能看见的地方，是暴露面失控的起点。
        context.warn(
            f"{len(unclassified)} 个工具未归类（{', '.join(unclassified)}）——"
            "请在 app/toolsets.py 的 TOOL_TOOLSET 里登记"
        )
    context.note(
        "MCP 工具（mcp__<server>__<tool>）要连上服务后才会出现在这里；"
        "禁用它们可写 `mcp:<server>` 或 `server:tool`。"
    )
    context.emit(
        {
            "ok": True,
            "platform": platform,
            "total": len(rows),
            "exposed": len(exposed),
            "tools": rows,
            "toolsets": toolsets.TOOLSETS,
        }
    )
    return 0


# ── enable / disable ────────────────────────────────────────────────────────


def _apply(context: Context, args, *, enable: bool) -> int:
    """``enable`` 与 ``disable`` 的共用实现：差别只有一个布尔。"""
    platform = _platform(args)
    entries = [str(item) for item in (getattr(args, "name", None) or [])]
    if not entries:
        raise CliError("至少要给一个工具名或 toolset 名（`orbit tools list` 可查）")
    unknown = [entry for entry in entries if not toolsets.is_known(entry)]
    if unknown:
        # 拼错必须报错：静默接受等于让用户以为关掉了，其实全开着。
        details = []
        for entry in unknown:
            guesses = toolsets.suggest(entry)
            details.append(f"{entry}（是不是：{', '.join(guesses)}）" if guesses else entry)
        raise CliError(
            f"不认识这些名字：{'、'.join(details)}。"
            "可用的名字见 `orbit tools list`（toolset 名或工具名都行）。"
        )
    config = _config(context)
    updated = toolsets.toggle(config, platform, entries, enable=enable)
    path = toolsets.save_config(toolsets.config_path(context.data_dir), updated)
    disabled, enabled = toolsets.platform_state(updated, platform)
    action = "启用" if enable else "禁用"
    context.out(f"平台 {platform}：已{action} {len(entries)} 项（{'、'.join(entries)}）")
    context.out(f"当前禁用集：{'、'.join(disabled) or '（空）'}")
    context.out(f"当前启用集：{'、'.join(enabled) or '（空 = 全开模式）'}")
    context.out(f"配置：{path}")
    context.emit(
        {
            "ok": True,
            "platform": platform,
            "action": "enable" if enable else "disable",
            "entries": entries,
            "disabled": disabled,
            "enabled": enabled,
            "path": str(path),
        }
    )
    return 0


def run_enable(context: Context, args) -> int:
    return _apply(context, args, enable=True)


def run_disable(context: Context, args) -> int:
    return _apply(context, args, enable=False)


# ── post-setup ──────────────────────────────────────────────────────────────


def run_post_setup(context: Context, args) -> int:
    """输出当前平台的启停快照（可直接贴进 ``data/tools.json``）。

    ``--json`` 下走结构化出口；否则**只**打印那段 JSON，好让人 `>` 到文件里直接用。
    """
    platform = _platform(args)
    names = _catalog_names()
    snapshot = toolsets.post_setup_snapshot(platform, _config(context), names)
    if not context.json:
        context.out(json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True))
    context.emit({"ok": True, "platform": platform, "snapshot": snapshot})
    return 0


# ── summary ─────────────────────────────────────────────────────────────────


def run_summary(context: Context, args) -> int:
    """各平台一行摘要（Hermes 的 ``tools --summary``）。"""
    config = _config(context)
    names = _catalog_names()
    platforms = sorted({toolsets.DEFAULT_PLATFORM, *toolsets.platforms_in(config)})
    rows = []
    for platform in platforms:
        disabled, enabled = toolsets.platform_state(config, platform)
        exposed = toolsets.resolve(names, platform, config)
        rows.append(
            (
                platform,
                str(len(exposed)),
                str(len(disabled)),
                "是" if enabled else "否",
            )
        )
    context.out("各平台工具暴露摘要")
    context.out()
    context.table(("平台", "暴露", "禁用项", "白名单收窄"), rows)
    context.emit(
        {
            "ok": True,
            "platforms": [
                {"platform": row[0], "exposed": int(row[1]), "disabled": int(row[2])}
                for row in rows
            ],
        }
    )
    return 0


# ── 登记 ────────────────────────────────────────────────────────────────────

TOOLS_COMMAND = Command(
    name="tools",
    help="按平台配置工具启停",
    handler=run_summary,
    group="ext",
    hermes="tools",
    parity="P0",
    state="done",
    needs_settings=False,
    options=[Option(("--summary",), "打印各平台启用摘要", action="store_true")],
    subcommands=[
        Command(
            name="list",
            help="列出全部工具及启停状态",
            handler=run_list,
            group="ext",
            parity="P0",
            state="done",
            needs_settings=False,
            options=[
                Option(
                    ("--platform",),
                    PLATFORM_HELP,
                    default=toolsets.DEFAULT_PLATFORM,
                    metavar="PLATFORM",
                )
            ],
        ),
        Command(
            name="enable",
            help="启用 toolset 或 MCP 工具",
            handler=run_enable,
            group="ext",
            parity="P0",
            state="done",
            needs_settings=False,
            options=[
                Option(
                    ("--platform",),
                    PLATFORM_HELP,
                    default=toolsets.DEFAULT_PLATFORM,
                    metavar="PLATFORM",
                ),
                Option(("name",), "toolset 名或工具名（MCP 工具写 server:tool）", nargs="+"),
            ],
        ),
        Command(
            name="disable",
            help="禁用 toolset 或 MCP 工具",
            handler=run_disable,
            group="ext",
            parity="P0",
            state="done",
            needs_settings=False,
            options=[
                Option(
                    ("--platform",),
                    PLATFORM_HELP,
                    default=toolsets.DEFAULT_PLATFORM,
                    metavar="PLATFORM",
                ),
                Option(("name",), "toolset 名或工具名（MCP 工具写 server:tool）", nargs="+"),
            ],
        ),
        Command(
            name="post-setup",
            help="输出当前平台的启停快照（可直接贴进 data/tools.json）",
            handler=run_post_setup,
            group="ext",
            parity="P0",
            state="done",
            needs_settings=False,
            options=[
                Option(
                    ("--platform",),
                    PLATFORM_HELP,
                    default=toolsets.DEFAULT_PLATFORM,
                    metavar="PLATFORM",
                )
            ],
        ),
    ],
)


def _register(command: Command) -> None:
    """登记 `tools`；声明表已登记过同名命令时**就地接管**而不是报错。

    为什么要有接管分支：本模块与 ``hermes_parity`` 都会登记 ``tools``。正常接线是
    ``commands/__init__.py`` 先 import 本模块、再 import ``hermes_parity``（后者
    ``register_specs`` 见到同名就跳过）。但测试或脚本可能反过来先 import 声明表，
    那时 ``REGISTRY.add`` 会撞名抛 ``ValueError``——"命令面重复登记"是 bug 级错误，
    不该由导入顺序决定成败。于是这里退一步：把真实现塞进已登记的那条命令里。
    """
    existing = REGISTRY.get(command.name)
    if existing is None:
        REGISTRY.add(command)
        return
    existing.handler = command.handler
    existing.help = command.help
    existing.group = command.group
    existing.parity = command.parity
    existing.state = command.state
    existing.options = command.options
    existing.subcommands = command.subcommands
    existing.needs_settings = command.needs_settings
    existing.needs_store = command.needs_store


_register(TOOLS_COMMAND)
