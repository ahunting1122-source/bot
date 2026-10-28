"""`orbit tools discover` / `tools explain` / `tools domains`：工具注册层的三条诊断命令。

## 为什么是这三条，而不是把信息塞进 `tools list`

`tools list` 回答"模型现在能看见哪些工具、为什么看不见"。这三条回答的是另外三个
**不同读者**的问题，混进一张表里会让三种答案互相稀释：

* ``tools discover`` —— "**这个工具定义在哪**"。读的人是排查"我加了工具怎么没生效"
  的人，要的是 ``模块:行号``，不是暴露面。
* ``tools explain <tool>`` —— "**为什么这个工具此刻不可见**"。四层过滤（平台 / 场景 /
  profile / 委托）每层都会拦人，只给一句"不可见"等于让人回头读代码。
* ``tools domains`` —— "**19 个功能域各自覆盖了多少**"。读的人是排期的人，
  要的是"哪个域还是空的、为什么空"，不是某一个工具的状态。

## 接线方式：`register_handler` + 追加子命令（两条都做，理由不同）

``_table.register_handler`` 只能填**声明表里已经存在**的路径。``tools discover`` /
``explain`` / ``domains`` 三条都**不在**声明表里（``orbit tools`` 现有的 7 个子命令
``list`` / ``enable`` / ``disable`` / ``post-setup`` / ``probe`` / ``catalog`` /
``backends`` 已被真实现接管），所以：

1. 用 ``register_handler`` 登记路径 —— 这样**万一**将来声明表里出现同名路径
   （或有人把它们从实现搬回表里），handler 会自动绑上，不用再改这里；
2. 把三条子命令**追加**到已构建的 ``tools`` 命令上 —— 这才是让它们现在就能跑的那一步。
   追加不覆盖：父命令原有的 7 条一条不少（与 ``tools_extra2`` 追加 ``egress`` 子命令
   同一套做法）。

导入 ``app.cli.commands.tools`` 是必须的：``tools`` 这条命令由它 ``REGISTRY.add``，
不先导入就没有父节点可挂。
"""

from __future__ import annotations

import logging
from typing import Any

import app.cli.commands._table as _table
import app.cli.commands.tools as tools_commands  # noqa: F401 —— 导入即登记 `tools` 父命令
from app import tool_discovery, toolsets
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option

log = logging.getLogger("orbit.cli.tools_extra3")


# ── tools discover ──────────────────────────────────────────────────────────


def run_tools_discover(context: Context, args: Any) -> int:
    """`orbit tools discover`：跑一次 AST 扫描，把索引与"真实收益"一起打出来。

    刻意**同时**打印索引分布和 ``tool_discovery.describe()`` 的自述：只给一张
    ``模块 → 工具数`` 的表，读者很容易把它读成"懒发现省了很多启动时间"；
    自述里那句"24 个内置工具在同一个文件里，省不下 import 成本"必须和数字一起出现。
    """
    index = tool_discovery.discover(
        data_dir=context.data_dir, force=bool(getattr(args, "force", False))
    )
    known, missing = tool_discovery.covered_names(index, tools_commands._catalog_names())
    grouped = index.modules()

    context.out(tool_discovery.describe())
    context.out()
    context.out("按模块分布")
    context.table(
        ("模块", "工具数", "工具"),
        [(module, str(len(names)), "、".join(names)) for module, names in grouped.items()],
    )
    context.out()
    context.out("按工具查位置")
    context.table(
        ("工具", "模块", "位置"),
        [
            (info.name, info.module, f"{info.path}:{info.lineno}")
            for _name, info in sorted(index.entries.items())
        ],
    )
    context.out()
    if missing:
        context.warn(
            f"{len(missing)} 个工具在索引里找不到：{'、'.join(missing[:8])}"
            f"{'…' if len(missing) > 8 else ''}。"
            "它们多半是运行时拼出来的名字（工具名不是字符串字面量）。"
        )
    else:
        context.out(f"对账：{len(known)} 个工具全部有索引条目。")
    context.out(f"索引文件：{tool_discovery.index_path(context.data_dir)}")
    context.emit(
        {
            "ok": True,
            "command": "tools discover",
            "generation": index.stamp,
            "scans": index.scans,
            "scanned_at": index.scanned_at,
            "total": len(index.entries),
            "modules": grouped,
            "tools": {name: info.as_dict() for name, info in sorted(index.entries.items())},
            "covered": known,
            "missing": missing,
            "index_path": str(tool_discovery.index_path(context.data_dir)),
            "describe": tool_discovery.describe(),
        }
    )
    return 0


# ── tools explain ───────────────────────────────────────────────────────────


def _filter_context(args: Any) -> toolsets.FilterContext:
    """从命令行选项拼 :class:`toolsets.FilterContext`（坏场景名由它自己抛 ValueError）。"""
    scene = str(getattr(args, "scene", None) or toolsets.DEFAULT_SCENE).strip()
    try:
        return toolsets.FilterContext(
            platform=str(
                getattr(args, "platform", None) or toolsets.DEFAULT_PLATFORM
            ).strip(),
            scene=scene,
            profile=str(getattr(args, "profile", None) or "").strip(),
            delegated=bool(getattr(args, "delegated", False)),
        )
    except ValueError as exc:
        raise CliError(str(exc)) from exc


def run_tools_explain(context: Context, args: Any) -> int:
    """`orbit tools explain <tool>`：逐层说清"为什么可见 / 不可见"。

    工具名拼错时**报错并给候选**（与 ``tools disable`` 同一条规矩）：静默输出
    "不可见"会让人以为过滤层把它拦了，实际是名字写错了——排查方向完全跑偏。
    """
    name = str(getattr(args, "tool", "") or "").strip()
    if not name:
        raise CliError("给一个工具名：orbit tools explain web_search")
    if not toolsets.is_known(name) and name not in tools_commands._catalog_names():
        candidates = toolsets.suggest(name)
        hint = f"（相近的有：{'、'.join(candidates)}）" if candidates else ""
        raise CliError(f"不认识工具或 toolset：{name}{hint}")
    filter_context = _filter_context(args)
    config = toolsets.load_config(toolsets.config_path(context.data_dir))
    text = toolsets.explain(name, filter_context, config)
    context.out(text)
    context.out()
    context.out(
        "四层顺序：平台（data/tools.json 的 platforms）→ 场景（chat / cron / subagent / acp）"
        "→ profile（data/tools.json 的 profiles）→ 委托（delegated=True 时）。"
    )
    context.emit(
        {
            "ok": True,
            "command": "tools explain",
            "tool": name,
            "toolset": toolsets.toolset_of(name),
            "domain": toolsets.domain_of_tool(name),
            "tier": toolsets.tier_of(name),
            "always_eager": name in toolsets.ALWAYS_EAGER,
            "platform": filter_context.platform,
            "scene": filter_context.scene,
            "profile": filter_context.profile,
            "delegated": filter_context.delegated,
            "explain": text,
        }
    )
    return 0


# ── tools domains ───────────────────────────────────────────────────────────


def run_tools_domains(context: Context, args: Any) -> int:
    """`orbit tools domains`：19 个功能域各自的覆盖情况（MATRIX 5.15 的判据）。

    空域**必须**打印原因（``empty`` 那一列只是标记，原因才是交付物）：看到
    "delegation 0 个工具"而不知道"委派是图节点行为、不是模型工具"的人，
    会去开一张"补委派工具"的工单——那张工单从第一行起就是错的。
    """
    rows = toolsets.describe_domains(tools_commands._catalog_names())
    context.out("19 个功能域的覆盖情况（域清单对齐 app/tool_catalog.py 的 DOMAINS）")
    context.out()
    context.table(
        ("域", "说明", "toolset", "工具数", "空"),
        [
            (
                row["domain"],
                row["label"],
                "、".join(row["toolsets"]) or "—",
                str(row["count"]),
                "是" if row["empty"] else "否",
            )
            for row in rows
        ],
    )
    empty = [row for row in rows if row["empty"]]
    if empty:
        context.out()
        context.out("空域及原因")
        for row in empty:
            mark = "（运行时动态）" if row["dynamic"] else ""
            context.out(f"  {row['domain']}：{row['reason']}{mark}")
    total = sum(row["count"] for row in rows)
    context.out()
    context.out(
        f"合计 {len(rows)} 个域、{total} 个工具；其中 {len(empty)} 个域没有工具"
        f"（{len([r for r in empty if r['dynamic']])} 个是运行时动态生成）。"
    )
    context.emit(
        {
            "ok": True,
            "command": "tools domains",
            "domains": rows,
            "total_tools": total,
            "empty_domains": [row["domain"] for row in empty],
        }
    )
    return 0


# ── 命令登记 ────────────────────────────────────────────────────────────────

#: 本模块接管的三条路径。写在这里是为了让"接了什么"可被 grep。
EXTRA_PATHS: tuple[str, ...] = (
    "tools discover",
    "tools explain",
    "tools domains",
)


def _subcommands() -> list[Command]:
    """三条新子命令的声明。"""
    platform_option = Option(
        ("--platform",),
        "平台（默认 cli）",
        default=toolsets.DEFAULT_PLATFORM,
        metavar="PLATFORM",
    )
    return [
        Command(
            name="discover",
            help="跑一次 AST 扫描：每个工具定义在哪个模块第几行（不 import 被测模块）",
            handler=run_tools_discover,
            group="ext",
            parity="P1",
            state="done",
            needs_settings=False,
            options=[
                Option(
                    ("--force",),
                    "强制重扫（默认只在源码指纹变化时重扫）",
                    action="store_true",
                )
            ],
        ),
        Command(
            name="explain",
            help="逐层解释一个工具为什么可见 / 不可见（平台 / 场景 / profile / 委托）",
            handler=run_tools_explain,
            group="ext",
            parity="P1",
            state="done",
            needs_settings=False,
            options=[
                Option(("tool",), "工具名（也可给 toolset 名）"),
                platform_option,
                Option(
                    ("--scene",),
                    "场景：chat / cron / subagent / acp（默认 chat）",
                    default=toolsets.DEFAULT_SCENE,
                    metavar="SCENE",
                ),
                Option(
                    ("--profile",),
                    "profile 名（对应 data/tools.json 的 profiles.<name>）",
                    metavar="PROFILE",
                ),
                Option(
                    ("--delegated",),
                    "按「被委派的一轮」判定（委托层生效）",
                    action="store_true",
                ),
            ],
        ),
        Command(
            name="domains",
            help="19 个功能域各自的覆盖情况，含空域与空的原因",
            handler=run_tools_domains,
            group="ext",
            parity="P1",
            state="done",
            needs_settings=False,
        ),
    ]


def extend_registry() -> list[str]:
    """把三条子命令追加到已存在的 ``tools`` 命令上（**幂等**）。返回新增的路径。"""
    parent = REGISTRY.get("tools")
    if parent is None:  # 理论上不会：本模块顶部已 import tools_commands
        return []
    existing = {sub.name for sub in parent.subcommands}
    added: list[str] = []
    for child in _subcommands():
        if child.name in existing:
            continue
        parent.subcommands.append(child)
        added.append(f"tools {child.name}")
    return added


def _bind_handlers() -> None:
    """把三条路径登记进 ``_table.HANDLERS``（供将来声明表里出现同名路径时自动绑定）。

    重复导入（测试、热重载）不重复登记：``register_handler`` 撞名会抛错。
    """
    handlers = {
        "tools discover": run_tools_discover,
        "tools explain": run_tools_explain,
        "tools domains": run_tools_domains,
    }
    for path, handler in handlers.items():
        if path in _table.HANDLERS:
            continue
        register_handler(path, handler)


_bind_handlers()
extend_registry()
