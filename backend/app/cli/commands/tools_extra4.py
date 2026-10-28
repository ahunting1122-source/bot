"""`orbit tools budget` / `tools coverage` / `tools extra`：工具层收尾的三条自证命令。

## 为什么是这三条

前三条诊断命令（``discover`` / ``explain`` / ``domains``，见 ``tools_extra3.py``）回答的是
"工具在哪、为什么不可见、19 个域各有多少"。这一轮补的两块工作（MATRIX 5.11 的分级披露、
5.14 的覆盖推进）各自需要一个**能当场跑出数字**的出口，否则"预算压下去了"和"覆盖推到
多少了"都只能靠读代码相信：

* ``tools budget`` —— 三档 token 实测（不裁剪 / 只压描述 / 延迟 Tier 2），以及
  **延迟之后到底进没进 4,000**。这是 5.11 唯一可验证的判据。
* ``tools coverage`` —— Hermes 94 个内置工具的缺口清单（按域），以及"本轮新实现但还没
  接线"的那几个。它比 `tools domains` 多的是**对照**：Orbit 有 19 个域，Hermes 有 94 个
  工具，两者对不上时缺的到底是哪些。
* ``tools extra`` —— 本轮新实现的工具本身：名字、toolset、tier、schema 是否合法、
  以及**到底进没进默认注册表**（"写了代码"与"模型能用"是两件事）。

## 接线方式：与 `tools_extra3` 同一套（`register_handler` + 追加子命令）

两条都做，理由见 `tools_extra3.py` 的模块 docstring：``register_handler`` 保证将来声明表
里出现同名路径时自动绑上，追加子命令保证它们**现在**就能跑。父命令原有的子命令一条不少。
"""

from __future__ import annotations

import logging
from typing import Any

import app.cli.commands._table as _table
import app.cli.commands.tools as tools_commands  # noqa: F401 —— 导入即登记 `tools` 父命令
from app import tool_catalog, tool_extra_impl, toolsets
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option

log = logging.getLogger("orbit.cli.tools_extra4")


# ── tools budget（MATRIX 5.11）───────────────────────────────────────────────


def _parse_tiers(raw: Any) -> tuple[int, ...]:
    """`--tiers 2` / `--tiers 1,2` → 元组。**坏值当场报错**（静默回退会给出假数字）。"""
    text = str(raw if raw is not None else "2").strip()
    tiers: list[int] = []
    for item in text.replace("，", ",").split(","):
        piece = item.strip()
        if not piece:
            continue
        try:
            value = int(piece)
        except ValueError as exc:
            raise CliError(f"`--tiers` 只认 0/1/2，收到 {piece!r}") from exc
        if value not in toolsets.TIERS:
            raise CliError(f"`--tiers` 只认 0/1/2（收到 {value}）：{toolsets.TIERS}")
        tiers.append(value)
    return tuple(sorted(set(tiers))) or (2,)


def run_tools_budget(context: Context, args: Any) -> int:
    """`orbit tools budget`：三档 token 实测 + 延迟后是否进预算。

    三档必须一起打印。只给"延迟后达标"那一行，读者无法判断省下来的是描述还是参数契约——
    而"省掉参数契约"等于把工具变成调不动，那不是优化。
    """
    from app.tools import ToolRegistry

    tiers = _parse_tiers(getattr(args, "tiers", None))
    raw_budget = getattr(args, "budget", None)
    try:
        budget = toolsets.LISTING_BUDGET_TOKENS if raw_budget is None else int(raw_budget)
    except (TypeError, ValueError) as exc:
        raise CliError(f"`--budget` 要整数，收到 {raw_budget!r}") from exc
    # `store=None`：量预算只读 `specs()`，不碰数据库。与 `mcp.py` 里那句
    # `ToolRegistry(context.settings, None)  # type: ignore[arg-type]` 同一个取舍——
    # 为了一条纯读的诊断命令去开 sqlite 只会让命令变慢，还可能顺手建出空库。
    registry = ToolRegistry(context.settings, None)  # type: ignore[arg-type]
    report = tool_extra_impl.listing_budget(registry, budget=budget, tiers=tiers)
    context.out(report.describe())
    context.out()
    context.out(
        "为什么参数 schema 不裁：它是调用契约，删了模型不是「晚点知道」，而是永远调不动。"
        "所以 4,000 只能靠**延迟工具本身**（Tier 2 只给名字 + 一句描述，参数表用 "
        "tool_describe 取）来达，而不是靠删 schema。"
    )
    if not report.within_budget:
        context.warn(
            f"延迟 Tier {'+'.join(str(t) for t in tiers)} 之后仍超 {budget:,}："
            "再往上就得延迟 Tier 1（`--tiers 1,2`）或收窄工具面（`orbit tools disable`）。"
        )
    context.emit({"ok": True, "command": "tools budget", **report.as_dict()})
    return 0


# ── tools coverage（MATRIX 5.14）────────────────────────────────────────────


def run_tools_coverage(context: Context, args: Any) -> int:
    """`orbit tools coverage`：Hermes 94 个工具的缺口清单（按 19 个域）。

    直接打印 `tool_catalog.report()`：它是**给人看的**排期输入（"哪个域还空着、为什么空"）。
    要机器可读的形态用 `--json`（`emit` 里的 `covered` / `missing` 两组）。
    """
    coverage = tool_catalog.orbit_coverage()
    context.out(tool_catalog.report(coverage))
    context.out()
    rows = [
        (
            info.domain,
            info.label,
            f"{len(info.covered)}/{info.total}",
            f"{info.rate:.0%}",
            "、".join(info.missing[:4]) + ("…" if len(info.missing) > 4 else ""),
        )
        for info in coverage.domains.values()
    ]
    context.table(("域", "说明", "覆盖", "比例", "还缺"), rows)
    context.emit(
        {
            "ok": True,
            "command": "tools coverage",
            "total": coverage.total,
            "covered_count": coverage.covered_count,
            "missing_count": len(coverage.missing),
            "pool": list(coverage.pool),
            "covered": {name: list(names) for name, names in coverage.covered.items()},
            "missing": list(coverage.missing),
            "orbit_only": list(coverage.orbit_only),
            "extra_tools": list(tool_catalog.extra_tool_names()),
            "extra_tools_registered": list(tool_catalog.registered_extra_tools()),
            "report": tool_catalog.report(coverage),
        }
    )
    return 0


# ── tools extra（本轮新实现）────────────────────────────────────────────────


def run_tools_extra(context: Context, args: Any) -> int:
    """`orbit tools extra`：本轮新实现的工具 + **进没进默认注册表**。

    这一列不是装饰：`register_extra_tools()` 还没被装配层调用时，工具"存在但模型看不见"。
    不把它显示出来，"接了一行但没生效"就会变成查不出来的问题。
    """
    from app.tools import LOCAL_TOOLS, schema_problems

    local = {tool.name for tool in LOCAL_TOOLS}
    rows = []
    for tool in tool_extra_impl.EXTRA_TOOLS:
        spec = tool.specification()
        problems = schema_problems(spec)
        rows.append(
            (
                tool.name,
                toolsets.toolset_of(tool.name),
                toolsets.domain_of_tool(tool.name) or "—",
                f"Tier {toolsets.tier_of(tool.name)}",
                "合法" if not problems else "；".join(problems),
                "是" if tool.name in local else "否（需 register_extra_tools）",
            )
        )
    context.out("本轮新实现的工具（app/tool_extra_impl.py）")
    context.out()
    context.table(("工具", "toolset", "域", "tier", "schema", "进默认注册表"), rows)
    pending = [row[0] for row in rows if row[5] != "是"]
    if pending:
        context.warn(
            f"{len(pending)} 个工具还没进默认注册表（{'、'.join(pending)}）："
            "装配层调一次 `register_extra_tools(registry)` 即可，"
            "在此之前模型看不到它们。"
        )
    context.out()
    context.note(
        "测试证据：tests/test_tool_extra_impl.py 对每个工具都有真跑用例（建任务、取参数表），"
        "不是「注册成功」。"
    )
    context.emit(
        {
            "ok": True,
            "command": "tools extra",
            "tools": [
                {
                    "name": row[0],
                    "toolset": row[1],
                    "domain": row[2],
                    "tier": toolsets.tier_of(row[0]),
                    "schema_ok": row[4] == "合法",
                    "registered": row[5] == "是",
                }
                for row in rows
            ],
        }
    )
    return 0


# ── 命令登记 ────────────────────────────────────────────────────────────────

#: 本模块接管的三条路径（写在这里是为了让"接了什么"可被 grep）。
EXTRA_PATHS: tuple[str, ...] = (
    "tools budget",
    "tools coverage",
    "tools extra",
)


def _subcommands() -> list[Command]:
    """三条新子命令的声明。"""
    return [
        Command(
            name="budget",
            help="工具清单三档 token 实测：不裁剪 / 只压描述 / 延迟 Tier 2，以及是否进 4,000",
            handler=run_tools_budget,
            group="ext",
            parity="P1",
            state="done",
            options=[
                Option(
                    ("--tiers",),
                    "要延迟的 tier（默认 2；可给 1,2）",
                    default="2",
                    metavar="TIERS",
                ),
                Option(
                    ("--budget",),
                    "描述预算（默认取 toolsets.LISTING_BUDGET_TOKENS = 4000）",
                    metavar="TOKENS",
                ),
            ],
        ),
        Command(
            name="coverage",
            help="Hermes 94 个内置工具的覆盖缺口（按 19 个域，含每个缺口的原因）",
            handler=run_tools_coverage,
            group="ext",
            parity="P1",
            state="done",
            needs_settings=False,
        ),
        Command(
            name="extra",
            help="本轮新实现的工具：schema 是否合法、有没有进默认注册表",
            handler=run_tools_extra,
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
        "tools budget": run_tools_budget,
        "tools coverage": run_tools_coverage,
        "tools extra": run_tools_extra,
    }
    for path, handler in handlers.items():
        if path in _table.HANDLERS:
            continue
        register_handler(path, handler)


_bind_handlers()
extend_registry()
