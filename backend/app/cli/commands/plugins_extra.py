"""`orbit plugins` 的收尾子命令（MATRIX 13.9）：info / validate / reload / deps / override / pages。

## 接线方式（先说清为什么不是 `register_handler`）

任务书要求的做法是「用 :func:`app.cli.commands._table.register_handler` 接表里还是
``todo`` 的路径」。本轮先查了 ``REGISTRY.get("plugins")``：**11 条子命令全部有真实现，
一条 todo 都没有**（``install``/``search``/``pack install``/``update`` 是 ``partial``——
它们"报了未实现"，不是"没实现"，两者不该混为一谈）。

更关键的一条：``plugins`` 这个 ``Command`` **不是声明表构建出来的**，而是
``app/cli/commands/plugins.py`` 自己 ``REGISTRY.add`` 的（``hermes_parity`` 见到同名会
跳过）。而 ``register_handler`` 只对"表里已有的路径"生效，所以它在这里无处施展。

于是本模块走仓库里的另一条既有做法：**往已有父节点追加子命令**
（``cron_extra`` / ``session_extra`` / ``model_extra2`` 都这么干）。同时仍然把每条新路径
登记进 ``_table.HANDLERS``——将来声明表真的补上 ``plugins info`` 这类条目时，
``Spec.build()`` 会直接取到这里的 handler，不用改两处。

``attach()`` 是幂等的，并且 ``detach()`` 能把追加的条目摘干净：测试要能还原全局注册表，
否则 ``tests/test_plugins.py`` 里那条"子命令集合必须精确等于 11 个"的断言会被本模块污染
（那条断言在 ``plugins_extra`` 被 ``__init__.py`` 正式接线后**需要加上这 6 个名字**）。

## 一处必须知道的让名

``plugins show`` 有别名 ``info``，而 argparse 不允许"子命令名"与"别的子命令的别名"重名
（``add_parser("info")`` 会抛 ``conflicting subparser``，**整棵命令树都编译不出来**）。
``plugins.py``（CLI）是禁改区，所以 :func:`attach` 在运行期把 ``show`` 的 ``info`` 别名让给
新命令：``orbit plugins info`` 走本模块（输出是 ``show`` 的超集），``detach()`` 原样还回去。

## 诚实边界

- **远程源一律报未实现**：``info``/``validate``/``deps``/``pages`` 只读本地磁盘；
  任何"去社区索引查一下"的路径都明确写"需要网络"。
- ``reload`` 与 ``override --load`` 会**真的 import 插件代码**（第三方代码，同进程同权限）。
  所以两者都有"不 import"的默认档（``reload --dry-run`` / ``override`` 不带 ``--load``），
  把执行代码做成显式选择，而不是顺手就执行。
- ``pages`` 只报登记表与挂载计划，``mounted`` 恒为 0：``app/main.py`` 没有插件路由挂载点。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app import plugin_capabilities, plugin_dashboard, plugins
from app.cli.commands import plugins as plugins_cli
from app.cli.commands._table import HANDLERS, register_handler
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option

# ── 小工具 ──────────────────────────────────────────────────────────────────


def _manifest_of(context: Context, name: str) -> plugins.PluginManifest:
    """按名字找已装插件（复用 `plugins.py` 的查找，报错文案保持一致）。"""
    return plugins_cli._find(context, name)  # noqa: SLF001 —— 同包内复用，报错口径要一致


def _discovery(context: Context) -> plugins.DiscoveryResult:
    return plugins_cli._discovery(context)  # noqa: SLF001


def _state(context: Context) -> plugins.PluginState:
    return plugins_cli._state(context)  # noqa: SLF001


def _is_user(context: Context, manifest: plugins.PluginManifest) -> bool:
    return plugins_cli._is_user_plugin(context, manifest)  # noqa: SLF001


def _runtime(context: Context, manifests: Any) -> plugins.PluginRuntime:
    """按"生产该怎么接"的方式造一个运行期对象（页面落点用宿主的登记表）。"""
    return plugins.PluginRuntime(
        manifests,
        state=_state(context),
        settings=context.settings,
    )


def _dashboard_dir(manifest: plugins.PluginManifest) -> Path | None:
    """插件目录里有没有 dashboard 契约（``manifest.json``）。"""
    for candidate in (manifest.directory / "dashboard" / "manifest.json", manifest.directory / "manifest.json"):
        if candidate.is_file():
            return candidate.parent
    return None


def _platform_ok(manifest: plugins.PluginManifest) -> tuple[bool, str]:
    """``platforms`` 约束是否满足当前系统。"""
    if not manifest.platforms:
        return True, ""
    import sys

    current = {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")
    if current in manifest.platforms:
        return True, current
    return False, f"声明只支持 {'、'.join(manifest.platforms)}，当前 {current}"


# ── info ────────────────────────────────────────────────────────────────────


def run_info(context: Context, args) -> int:
    """一个插件的**完整**画像：门禁 + import 时机 + 能力状态 + 依赖 + 页面。

    与 ``plugins show`` 的分工：``show`` 回答"它声明了什么"（manifest 字段 + 扫描裁决），
    ``info`` 回答"它在这个仓库里到底能不能用、卡在哪"——所以多了门禁快照、import 时机、
    能力状态（wired/exposed/unsupported）与 dashboard 登记情况。
    """
    manifest = _manifest_of(context, str(getattr(args, "name", "") or ""))
    state = _state(context)
    gate = state.gate(manifest.name, default=manifest.enabled_by_default)
    declared = plugins_cli._declared_capabilities(manifest)  # noqa: SLF001
    capability_rows = plugin_capabilities.declared_status(declared)
    platform_ok, platform_note = _platform_ok(manifest)
    pages = plugin_dashboard.PAGES.by_name(manifest.name)

    context.out(f"插件：{manifest.name}（{manifest.kind} · {manifest.version or '无版本'}）")
    context.out(f"路径：{manifest.path}（{'用户安装' if _is_user(context, manifest) else '随包'}）")
    context.out(f"import 时机：{manifest.import_phase}（{plugins.IMPORT_PHASES[manifest.import_phase]}）")
    context.out(f"门禁：{'启用' if gate.enabled else '禁用'} · 内置工具覆盖授权：{'是' if gate.allow_tool_override else '否'}")
    context.out(f"来源标记：{gate.source}")
    if not platform_ok:
        context.warn(f"{manifest.name} 的 platforms 约束不满足：{platform_note}")
    context.out()
    context.table(
        ("声明能力", "状态", "落点 / 缺哪一层"),
        [(row["name"], row["status"], row["landing"]) for row in capability_rows],
    )
    if not capability_rows:
        context.out("（manifest 没有声明任何能力）")
    context.out()
    context.table(
        ("依赖项", "内容", "缺失"),
        [
            ("pip", "、".join(manifest.pip_dependencies) or "-", "不装（本机无网络）"),
            (
                "外部二进制",
                "、".join(str(item.get("name", "")) for item in manifest.external_dependencies) or "-",
                "-",
            ),
            ("必填环境变量", "、".join(item.name for item in manifest.requires_env) or "-",
             "、".join(manifest.missing_env()) or "-"),
        ],
    )
    if pages is not None:
        context.out()
        context.out(
            f"dashboard 页面：{pages.get('route_prefix', '')}（已登记 · mounted={pages.get('mounted')}）"
        )
    context.emit(
        {
            "ok": True,
            "plugin": manifest.as_dict(),
            "gate": gate.as_dict(),
            "import_phase": manifest.import_phase,
            "capabilities": capability_rows,
            "platform_ok": platform_ok,
            "platform_note": platform_note,
            "missing_env": list(manifest.missing_env()),
            "dashboard_page": pages,
        }
    )
    return 0


# ── validate ────────────────────────────────────────────────────────────────


def _validate_one(
    context: Context, manifest: plugins.PluginManifest
) -> list[dict[str, str]]:
    """按真实运行期契约校验一个插件，返回发现列表（``level`` ∈ ok/warn/error）。"""
    findings: list[dict[str, str]] = []

    def add(level: str, check: str, detail: str) -> None:
        findings.append({"level": level, "check": check, "detail": detail})

    for item in plugins.doctor(manifest, data_dir=context.data_dir):
        add(item.level, item.name, item.detail)

    # dashboard 契约（有就校验，没有不算错）
    dashboard_dir = _dashboard_dir(manifest)
    if dashboard_dir is not None:
        try:
            dashboard = plugin_dashboard.load_manifest(dashboard_dir / "manifest.json")
            validation = plugin_dashboard.validate(dashboard)
        except plugin_dashboard.DashboardError as exc:
            add("error", "dashboard", str(exc))
        else:
            for issue in validation.issues:
                add(issue.level, f"dashboard.{issue.field}", issue.detail)
            if validation.ok:
                add("ok", "dashboard", f"{dashboard.route_prefix}（{len(plugin_dashboard.routes_for(dashboard))} 条路由，未挂载）")

    # 声明的能力里有没有 Orbit 根本挂不上的
    for row in plugin_capabilities.declared_status(
        plugins_cli._declared_capabilities(manifest)  # noqa: SLF001
    ):
        if row["status"] == "unsupported":
            add("warn", f"capability.{row['name']}", row["landing"])
        elif row["status"] == "unknown":
            add("warn", f"capability.{row['name']}", "不在 Orbit 的 19 种能力面里（拼写？）")

    platform_ok, platform_note = _platform_ok(manifest)
    if not platform_ok:
        add("warn", "platforms", platform_note)

    if not findings:
        add("ok", "validate", "无发现")
    return findings


def run_capabilities(context: Context, args) -> int:
    """`orbit plugins capabilities`：19 种 `ctx.register_*` 的真实落地情况。

    数据来自 `app/plugin_capabilities.py`（`wired` / `exposed` / `unsupported` 三态），
    不是早期那份 9/10 的静态表。`--json` 给对账用。
    """
    # plugins capabilities [name]：给了名字就看那一个插件的声明能力
    # （与 plugins info 同一套逻辑——同一个问题不该有两个答案）。
    if getattr(args, "name", None):
        return run_info(context, args)
    summary = plugin_capabilities.summary()
    if getattr(args, "json", False):
        context.emit({**summary, "drift": plugin_capabilities.drift()})
        return 0
    context.out(
        f"能力面：{summary.get('total', 0)} 种 register_*"
        f"（wired {summary.get('wired', 0)} / exposed {summary.get('exposed', 0)} / "
        f"unsupported {summary.get('unsupported', 0)}）"
    )
    context.out()
    for status, meaning in (summary.get("statuses") or {}).items():
        context.out(f"  {status}：{meaning}")
    context.out()
    rows: list[list[str]] = []
    for item in plugin_capabilities.CAPABILITIES:
        rows.append(
            [
                str(item.name),
                str(item.orbit_status),
                str(item.orbit_landing or ""),
            ]
        )
    context.table(["register_*", "状态", "落点 / 缺哪一层"], rows)
    # `upgraded` 是**名字列表**（不是 dict 列表）——照 summary() 的真实形状取。
    upgraded = [str(item) for item in (summary.get("upgraded") or [])]
    if upgraded:
        context.out()
        context.out(f"本轮升级：{'、'.join(str(name) for name in upgraded)}")
    # summary()["hermes_only"] 是**计数**不是列表；名字在模块级 HERMES_ONLY。
    hermes_only = [getattr(item, "name", item) for item in plugin_capabilities.HERMES_ONLY]
    if hermes_only:
        context.out()
        context.out(
            f"Hermes 另有 {len(hermes_only)} 种 Orbit 完全没有整层："
            + "、".join(str(name) for name in hermes_only)
        )
    return 0

def run_validate(context: Context, args) -> int:
    """校验一个插件或全部插件（``--ci`` 有 error 时返回非零）。

    与 ``doctor`` 的差别：``doctor`` 只按 manifest 的运行期契约查一个目标；
    ``validate`` 多查三件事——dashboard 契约、声明能力在本仓库里的可用性、平台约束，
    并且**能一次扫全部已发现插件**（``--ci`` 就是给 CI 用的那个形态）。
    """
    target = str(getattr(args, "target", "") or "").strip()
    if target:
        manifests = [_manifest_of(context, target)]
    else:
        result = _discovery(context)
        manifests = list(result.manifests)
        for error in result.errors:
            context.warn(f"{error.path}：{error.message}")
        if not manifests:
            context.out("没有发现任何插件。")
            context.emit({"ok": True, "plugins": [], "errors": 0, "warnings": 0})
            return 0

    rows: list[tuple[str, str, str, str]] = []
    payload: list[dict[str, Any]] = []
    errors = warns = 0
    for manifest in manifests:
        findings = _validate_one(context, manifest)
        for item in findings:
            if item["level"] == "error":
                errors += 1
            elif item["level"] == "warn":
                warns += 1
            rows.append((manifest.name, item["level"], item["check"], item["detail"]))
        payload.append({"plugin": manifest.name, "findings": findings})

    context.table(("插件", "级别", "检查项", "说明"), rows)
    context.out()
    context.out(f"结论：{len(manifests)} 个插件 · {errors} 个错误 · {warns} 个警告")
    context.emit(
        {"ok": not errors, "plugins": payload, "errors": errors, "warnings": warns}
    )
    if errors and getattr(args, "ci", False):
        return 1
    return 0


# ── reload ──────────────────────────────────────────────────────────────────


def run_reload(context: Context, args) -> int:
    """重新发现插件并**重新过门禁**；``--all`` 时连 lazy 插件一起 import。

    为什么要有它：``plugins.json`` 与插件目录都可能被外部改动（手改配置、`plugins install`
    之后），而进程里没有任何"重扫"入口。本命令给出一个可验证的动作：
    重新 ``discover`` → 重新 ``PluginState.load`` → 驱动一次 ``PluginRuntime``。

    ``--dry-run``（默认不做 import）只报计划：**执行第三方代码必须是显式选择**。
    """
    result = _discovery(context)
    state = _state(context)
    runtime = plugins.PluginRuntime(result.manifests, state=state, settings=context.settings)
    plan = runtime.plan()
    disabled = [name for name in plan if not state.is_enabled(name, True)]

    for error in result.errors:
        context.warn(f"{error.path}：{error.message}")

    context.out(f"重新发现：{len(result.manifests)} 个插件（{len(result.errors)} 个坏 manifest）")
    context.table(
        ("插件", "import 时机", "门禁"),
        [
            (
                manifest.name,
                plan[manifest.name],
                "启用" if state.is_enabled(manifest.name, manifest.enabled_by_default) else "禁用",
            )
            for manifest in result.manifests
        ],
    )

    payload: dict[str, Any] = {
        "ok": True,
        "discovered": len(result.manifests),
        "disabled": disabled,
        "plan": plan,
        "dry_run": bool(getattr(args, "dry_run", False)),
    }
    if getattr(args, "dry_run", False):
        context.out()
        context.out("干跑：没有 import 任何插件代码（加 --all 才真的驱动一遍）。")
        context.emit(payload)
        return 0

    if getattr(args, "all", False):
        loaded = runtime.load_all()
        context.out()
        context.out(f"已驱动全部插件：{len(loaded)} 个（含 lazy）")
    else:
        summary = runtime.warmup()
        loaded = []
        context.out()
        context.out(f"已 import eager 插件：{len(summary['loaded'])} 个")
        if summary["pending_lazy"]:
            context.out(
                "留着没 import 的 lazy 插件（首次需要工具清单时才进来）："
                + "、".join(summary["pending_lazy"])
            )
    described = runtime.describe()
    for item in described["errors"]:
        context.warn(f"{item['name']}：{item['error']}")
    context.out(f"工具清单：{len(runtime.tools())} 个 · dashboard 页面：{len(runtime.pages())} 个")
    context.out()
    context.out("接线仍未完成（宿主是禁改区）：")
    for line in described["wired_missing"]:
        context.out(f"  - {line}")
    payload.update(
        {
            "loaded": [item.manifest.name for item in runtime.loaded],
            "pending_lazy": described["pending_lazy"],
            "tools": sorted(runtime.tools()),
            "pages": [str(item.get("plugin", "")) for item in runtime.pages()],
            "errors": described["errors"],
            "wired": described["wired"],
            "wired_missing": described["wired_missing"],
        }
    )
    context.emit(payload)
    return 0


# ── deps ────────────────────────────────────────────────────────────────────


def run_deps(context: Context, args) -> int:
    """依赖报告：pip 包 / 外部二进制 / 环境变量 / 平台约束。

    **Orbit 不装依赖**（本机无网络，且装依赖等于在 agent 进程里执行任意代码）。
    所以这条命令的定位是"如实报缺什么、去哪装"，不是"帮你装好"。
    """
    name = str(getattr(args, "name", "") or "").strip()
    if name:
        manifests = [_manifest_of(context, name)]
    else:
        manifests = list(_discovery(context).manifests)

    rows: list[tuple[str, str, str, str]] = []
    payload: list[dict[str, Any]] = []
    for manifest in manifests:
        missing_env = manifest.missing_env()
        platform_ok, platform_note = _platform_ok(manifest)
        rows.append(
            (
                manifest.name,
                "、".join(manifest.pip_dependencies) or "-",
                "、".join(str(item.get("name", "")) for item in manifest.external_dependencies) or "-",
                "、".join(missing_env) or "齐备",
            )
        )
        payload.append(
            {
                "plugin": manifest.name,
                "pip": list(manifest.pip_dependencies),
                "external": [dict(item) for item in manifest.external_dependencies],
                "requires_env": [item.name for item in manifest.requires_env],
                "missing_env": list(missing_env),
                "platforms": list(manifest.platforms),
                "platform_ok": platform_ok,
                "platform_note": platform_note,
                "min_orbit_version": manifest.min_orbit_version,
                "install_hint": (
                    "pip install 需联网；本机无网络，Orbit 不代装"
                    if manifest.pip_dependencies
                    else ""
                ),
            }
        )

    context.out("依赖报告（Orbit 不装依赖：只报缺什么、去哪装）")
    context.table(("插件", "pip 包", "外部二进制", "缺失环境变量"), rows)
    context.out()
    context.out(
        "远程依赖解析未实现：社区索引 / PyPI 查询都需要网络。"
        "pip 依赖请自行 `pip install`，外部二进制请按 plugin.yaml 的 install 字段手动装。"
    )
    context.emit({"ok": True, "plugins": payload})
    return 0


# ── override ────────────────────────────────────────────────────────────────


def run_override(context: Context, args) -> int:
    """工具覆盖授权的**完整报告**：谁被授权、谁被降级、降了几次、两侧对齐没有。

    默认**不 import 插件**（只读 ``plugins.json`` 与 ``data/tools.json``）：
    "降了几次"只在 ``register()`` 执行时才能观察到，所以要看降级次数必须加 ``--load``
    ——那会真的执行第三方代码，因此做成显式开关。
    """
    state = _state(context)
    loaded: list[plugins.LoadedPlugin] = []
    if getattr(args, "load", False):
        runtime = _runtime(context, list(_discovery(context).manifests))
        loaded = runtime.load_all()
    report = plugins.override_report(state, loaded, settings=context.settings)

    context.out("工具覆盖授权（MATRIX 13.10）")
    context.out(
        "插件侧已授权："
        + ("、".join(report.sources) if report.sources else "（无）")
    )
    context.out(
        "插件侧明确拒绝："
        + ("、".join(report.denied) if report.denied else "（无）")
    )
    context.out(
        "工具侧放行（data/tools.json 的 allow_tool_override）："
        + ("、".join(report.tools_allow_list) if report.tools_allow_list else "（无）")
    )
    context.out(f"两侧对齐：{'是' if report.aligned else '否'}")
    if report.downgrades:
        context.out()
        context.table(
            ("插件", "工具", "请求覆盖", "实际生效", "降级次数"),
            [
                (
                    str(item["plugin"]),
                    str(item["tool"]),
                    "是" if item["requested"] else "否",
                    "是" if item["effective"] else "否",
                    str(item["count"]),
                )
                for item in report.downgrades
            ],
        )
    else:
        context.out()
        context.out("降级记录：无" + ("" if getattr(args, "load", False) else "（未 import 插件，加 --load 才能观察降级）"))
    if report.notes:
        context.out()
        for note in report.notes:
            context.out(f"  - {note}")
    context.emit({"ok": True, "report": report.as_dict(), "loaded": bool(loaded)})
    return 0


# ── pages ───────────────────────────────────────────────────────────────────


def run_pages(context: Context, args) -> int:
    """dashboard 页面：已登记的（真实落点）+ 已发现未登记的（还要一次 import）。

    ``mounted`` 恒为 0——``app/main.py`` 没有插件路由挂载点，所以这里只报"登记了什么、
    要挂在哪、还缺什么"，不谎称挂上了。
    """
    registry = plugin_dashboard.PAGES
    discovered: list[dict[str, Any]] = []
    for root in plugins.plugin_roots(context.settings):
        if not root.is_dir():
            continue
        for manifest in plugin_dashboard.discover(root):
            discovered.append(manifest.as_dict())

    if getattr(args, "load", False):
        runtime = _runtime(context, list(_discovery(context).manifests))
        runtime.load_all()

    snapshot = registry.as_dict()
    context.out(
        f"dashboard 页面：已登记 {snapshot['registered']} 个 · 已发现未登记 {len(discovered)} 个"
        f" · 已挂载 {snapshot['mounted']} 个"
    )
    if snapshot["pages"]:
        context.table(
            ("页面", "前端路由", "API 前缀", "状态"),
            [
                (
                    str(item.get("plugin", "")),
                    str(item.get("frontend_path", "")),
                    str(item.get("route_prefix", "")),
                    str(item.get("state", "")),
                )
                for item in snapshot["pages"]
            ],
        )
    if discovered:
        context.out()
        context.table(
            ("已发现", "前端路由", "API 前缀", "说明"),
            [
                (
                    str(item.get("name", "")),
                    str(item.get("fields", {}).get("tab", {}).get("path", "")),
                    str(item.get("route_prefix", "")),
                    "加 --load 驱动插件后会登记进登记表",
                )
                for item in discovered
            ],
        )
    context.out()
    context.out(f"未挂载原因：{snapshot['reason']}")
    context.out("要真的挂上去还缺：")
    for line in plugin_dashboard.describe(
        plugin_dashboard.DashboardManifest(name="(任意页面)")
    )["needed"]:
        context.out(f"  - {line}")
    context.emit({"ok": True, "registry": snapshot, "discovered": discovered})
    return 0


# ── 命令树 ──────────────────────────────────────────────────────────────────

EXTRA_SUBCOMMANDS: tuple[Command, ...] = (
    Command(
        name="info",
        help="一个插件的完整画像（门禁 + import 时机 + 能力状态 + 依赖 + 页面）",
        handler=run_info,
        group="ext",
        state="done",
        needs_settings=False,
        options=[Option(("name",), "插件名", metavar="NAME")],
    ),
    Command(
        name="validate",
        help="校验插件（manifest + dashboard 契约 + 能力可用性 + 平台约束）",
        handler=run_validate,
        group="ext",
        state="done",
        needs_settings=False,
        options=[
            Option(("--ci",), "有 error 时以非零码退出", action="store_true"),
            Option(("target",), "插件名（省略则校验全部已发现插件）", nargs="?"),
        ],
    ),
    Command(
        name="reload",
        help="重新发现插件并重新过门禁（--dry-run 不 import 代码）",
        handler=run_reload,
        group="ext",
        state="done",
        needs_settings=True,
        options=[
            Option(("--dry-run",), "只报计划，不 import 任何插件代码", action="store_true"),
            Option(("--all",), "连 lazy 插件一起 import（默认只 import eager）", action="store_true"),
        ],
    ),
    Command(
        name="deps",
        help="依赖报告：pip 包 / 外部二进制 / 环境变量 / 平台约束（不代装）",
        handler=run_deps,
        group="ext",
        state="done",
        needs_settings=False,
        options=[Option(("name",), "插件名（省略则报全部）", nargs="?")],
    ),
    Command(
        name="override",
        help="工具覆盖授权报告：谁被降级、降了几次、两侧是否对齐",
        handler=run_override,
        group="ext",
        state="done",
        needs_settings=True,
        options=[
            Option(
                ("--load",),
                "import 全部插件以统计降级次数（会执行第三方代码）",
                action="store_true",
            ),
        ],
    ),
    Command(
        name="pages",
        help="dashboard 页面：已登记 / 已发现 / 已挂载（恒为 0，没有挂载点）",
        handler=run_pages,
        group="ext",
        state="done",
        needs_settings=False,
        options=[
            Option(
                ("--load",),
                "驱动插件把页面登记进登记表（会执行第三方代码）",
                action="store_true",
            ),
        ],
    ),
)


def _release_alias_conflicts(command: Command) -> None:
    """把**与新增子命令同名**的旧别名摘掉（原始别名记进 :data:`_ALIAS_BACKUP`）。

    为什么必须做：``plugins show`` 有一个别名 ``info``，而 argparse 的 subparsers 不允许
    「子命令名」与「另一个子命令的别名」重名——``add_parser("info")`` 会直接抛
    ``ArgumentError: conflicting subparser: info``，整个命令树编译不出来（不是只影响
    ``plugins``：``orbit`` 的任何命令都用同一个 parser）。

    ``app/cli/commands/plugins.py`` 是禁改区，所以这里在**运行时**把 ``show`` 的 ``info``
    别名让给新命令：``orbit plugins info`` 从此走本模块（输出是 ``show`` 的超集：多了门禁
    快照、import 时机、能力状态、依赖与页面）。``detach()`` 会原样还回去。

    备份只在**第一次**看到冲突时记录（``setdefault``）：``attach()`` 会被反复调用
    （测试夹具就是这么用的），每次都覆盖备份会让 ``detach()`` 还不出原来的别名。
    """
    ours = {sub.name for sub in EXTRA_SUBCOMMANDS}
    for sub in command.subcommands:
        if sub.name in ours:
            continue
        if any(alias in ours for alias in sub.aliases):
            _ALIAS_BACKUP.setdefault(sub.name, sub.aliases)
            sub.aliases = tuple(alias for alias in sub.aliases if alias not in ours)


def attach() -> list[str]:
    """把上面的子命令追加到已登记的 ``plugins`` 命令上，返回本次追加的名字。

    幂等：已经挂过的跳过。**必须在 ``app.cli.commands.plugins`` 之后跑**——本模块开头
    已经 ``from app.cli.commands import plugins as plugins_cli``，导入即保证顺序。
    """
    command = REGISTRY.get("plugins")
    if command is None:  # 理论上不可能：上面那行 import 已经登记过它
        raise CliError("`plugins` 命令未登记，无法追加子命令（app/cli/commands/plugins.py 没被导入？）")
    existing = {sub.name for sub in command.subcommands}
    _release_alias_conflicts(command)
    added: list[str] = []
    for sub in EXTRA_SUBCOMMANDS:
        if sub.name not in existing:
            command.subcommands.append(sub)
            added.append(sub.name)
        key = f"plugins {sub.name}"
        if key not in HANDLERS and sub.handler is not None:
            # 声明表将来补上这些路径时，Spec.build() 直接取到这里的 handler（不必改两处）。
            register_handler(key, sub.handler)
    return added


def detach() -> list[str]:
    """摘掉本模块追加的子命令、还原被让出的别名（测试隔离用），返回被摘掉的名字。"""
    command = REGISTRY.get("plugins")
    if command is None:
        return []
    ours = {sub.name for sub in EXTRA_SUBCOMMANDS}
    kept = [sub for sub in command.subcommands if sub.name not in ours]
    removed = [sub.name for sub in command.subcommands if sub.name in ours]
    command.subcommands[:] = kept
    for name, aliases in _ALIAS_BACKUP.items():
        for sub in command.subcommands:
            if sub.name == name:
                sub.aliases = aliases
    return removed


#: 被让出的别名（``detach()`` 用它还原）。只在进程内存在，不落盘，**不清理**。
_ALIAS_BACKUP: dict[str, tuple[str, ...]] = {}


_ATTACHED = attach()


def _claim_capabilities() -> None:
    """把 `plugins capabilities` 接管成新实现（幂等）。

    `register_handler` 只影响**表还没组装**的情况；表已组装时还要直接改节点，
    与 `memory_extra2._bind` 同一套做法。
    """
    try:
        register_handler("plugins capabilities", run_capabilities)
    except ValueError:
        pass  # 已被本模块接过
    parent = REGISTRY.get("plugins")
    if parent is None:
        return
    node = parent.child("capabilities")
    if node is not None and not node.subcommands:
        node.handler = run_capabilities
        node.state = "done"



__all__ = [
    "EXTRA_SUBCOMMANDS",
    "attach",
    "detach",
    "run_deps",
    "run_info",
    "run_override",
    "run_pages",
    "run_reload",
    "run_validate",
]


_claim_capabilities()
