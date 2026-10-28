"""`orbit terminal` / `orbit tools backends` / `orbit egress {allow,deny,policy,check}`。

## 接线方式：能填的填表，填不了的追加（两条路都有理由）

`_table.register_handler` 只能填**声明表里已经存在**的路径，所以先用
``REGISTRY.get(...)`` 看现状，再决定每条路径怎么接：

- ``egress install / start / stop / restart / reload / disable``：**表里有、且还是
  `todo`**。它们属于一个 Orbit 根本没有的东西（iron-proxy 守护进程），留着通用占位
  报错（"计划波次 W13"）会让人以为"再等等就有了"。这里用 `register_handler` 把它们
  接管成**如实报告**：没有守护进程可起停，Orbit 的出口层是静态检查 + 头部注入。
- ``egress status / config / setup``：**已被 `security.py` 接管**。再登记一次会触发
  `register_handler` 的"重复登记"门禁，所以这里一个字节都不碰。
- ``egress allow / deny / policy / check`` 与 ``tools backends``：**表里没有这些路径**。
  `register_handler` 填不了，另立顶层命令又会挤掉表里未实现的子命令，所以走
  `mcp.py` 的同一套做法——**往已构建的 `Command` 上追加子命令**（追加不覆盖，
  `--help` 里原有的条目一条不少）。追加需要一个已存在的父命令，因此两种导入顺序
  都要成立：本模块排在 `hermes_parity` 之后就直接追加；排在之前就把
  `_table.register_specs` 包一层，等声明表组装完立刻追加。
- ``terminal``：**表里完全没有这条命令**（Hermes 的终端能力是工具不是 CLI 命令）。
  所以它是本模块新建的顶层命令，两个子命令 `list` / `test` 都带 handler。
"""

from __future__ import annotations

import logging
from typing import Any

import app.cli.commands._table as _table
import app.cli.commands.tools as tools_commands  # noqa: F401 —— 导入即登记 `tools`（追加子命令需要父命令）
from app import code_exec, egress, terminal
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context
from app.cli.shorthand import to_option
from app.cli.spec import REGISTRY, Command

log = logging.getLogger("orbit.cli.tools_extra2")

#: "未实现"的退出码。与 `_table._pending` / `mcp.py::UNIMPLEMENTED_CODE` 一致：
#: 脚本可以据此区分"这条能力没有"和"跑了但失败"。
UNIMPLEMENTED_CODE = 3


# ── 终端后端（MATRIX 5.18）──────────────────────────────────────────────────


def _backend_rows(context: Context) -> list[dict[str, Any]]:
    """三个后端的现状。**会做真实探测**（`wsl -l -q` / `docker inspect`）。"""
    return terminal.list_backends(context.settings)


def _print_backend_table(context: Context, rows: list[dict[str, Any]]) -> None:
    context.table(
        ("后端", "可用", "说明", "不可用原因"),
        [
            (
                row["name"],
                "是" if row["available"] else "否",
                row["describe"],
                row["reason"] or "—",
            )
            for row in rows
        ],
    )


def run_tools_catalog(context: Context, args: Any) -> int:
    """`orbit tools catalog`：Hermes 94 个内置工具在 Orbit 的覆盖情况与缺口。

    为什么单独一条命令：`tools list` 回答「模型现在能看见哪些工具」，
    这条回答「Hermes 有 94 个，我们覆盖了哪些、缺的为什么缺」。读者不同
    （一个是暴露面，一个是排期依据），混在一起两边都不好用。
    """
    from app import tool_catalog

    coverage = tool_catalog.orbit_coverage()
    if getattr(args, "json", False):
        context.emit(
            {
                "hermes_total": len(tool_catalog.HERMES_TOOLS),
                "covered": list(coverage.covered),
                "missing": list(coverage.missing),
                "orbit_only": list(coverage.orbit_only),
                "by_domain": [
                    {
                        "domain": item.domain,
                        "label": item.label,
                        "covered": len(item.covered),
                        "total": item.total,
                        "missing": list(item.missing),
                    }
                    for item in coverage.domains.values()
                ],
            }
        )
        return 0
    if getattr(args, "domain", None):
        wanted = str(args.domain).strip().lower()
        rows = [item for item in coverage.domains.values() if item.domain == wanted]
        if not rows:
            known = "、".join(coverage.domains)
            raise CliError(f"没有名为 {wanted} 的功能域（可选：{known}）")
        context.out(tool_catalog.report())
        return 0
    context.out(tool_catalog.report())
    return 0

def run_tools_backends(context: Context, args: Any) -> int:
    """`orbit tools backends`：命令能跑在哪几个后端里，各自此刻能不能用。

    为什么不并进 `tools list`：`tools list` 回答的是"模型能看见哪些**工具**"，
    这里回答的是"这些工具里的命令**在哪跑**"。两件事，读者也不同（一个是暴露面，
    一个是执行环境）。
    """
    rows = _backend_rows(context)
    current = terminal.resolve_backend_name(context.settings)
    available = [row["name"] for row in rows if row["available"]]
    context.out("终端后端（run_command 的执行环境）")
    context.out()
    _print_backend_table(context, rows)
    context.out()
    context.out(f"当前默认后端：{current}；可用：{'、'.join(available) or '无'}")
    context.out()
    context.out("代码执行的边界：" + code_exec.describe_sandbox())
    if not available:
        context.warn("没有可用后端——`run_command` 会直接报错，而不是静默换一个环境执行。")
    context.emit(
        {
            "ok": True,
            "command": "tools backends",
            "current": current,
            "backends": rows,
            "sandbox": code_exec.describe_sandbox(),
        }
    )
    return 0


def run_terminal_list(context: Context, args: Any) -> int:
    """`orbit terminal list`：与 `tools backends` 同一份数据，外加"怎么切换"。"""
    rows = _backend_rows(context)
    current = terminal.resolve_backend_name(context.settings)
    context.out("终端后端")
    context.out()
    _print_backend_table(context, rows)
    context.out()
    context.out(f"当前默认后端：{current}")
    context.out(
        "切换方式：在 data/settings.json 里写 "
        '{"terminal_backend": "wsl"}（或 "container"），改完不必重启——'
        "每次执行都会重新读。"
    )
    if current not in terminal.BACKEND_NAMES:
        context.warn(
            f"配置里的后端名 {current!r} 不认识（可用：{'、'.join(terminal.BACKEND_NAMES)}），"
            "执行命令时会直接报错而不是退回 local。"
        )
    context.note("容器后端还需要 terminal_container（或环境变量 ORBIT_DOCKER_CONTAINER）指向一个已在运行的容器。")
    context.emit(
        {
            "ok": True,
            "command": "terminal list",
            "current": current,
            "backends": rows,
        }
    )
    return 0


def run_terminal_test(context: Context, args: Any) -> int:
    """`orbit terminal test`：在指定后端里跑一条无害命令，看它到底通不通。

    探测（`wsl -l -q` / `docker inspect`）只证明"环境在"，不证明"能跑命令"——
    这两件事分开验证过一次，才敢让人把 `terminal_backend` 改过去。
    """
    name = str(getattr(args, "backend", "") or "").strip() or terminal.resolve_backend_name(
        context.settings
    )
    backend = terminal.resolve_backend(name, context.settings)
    ok, reason = backend.probe() if hasattr(backend, "probe") else (backend.available(), "")
    if not ok:
        raise CliError(f"后端 {name} 不可用：{reason}", code=UNIMPLEMENTED_CODE)
    command = "echo orbit-terminal-ok"
    # 容器后端的工作目录是**容器内**路径：传宿主的 data_dir 只会得到
    # "no such file or directory"，所以那里干脆不给 -w（用容器默认工作目录）。
    cwd: Any = "" if backend.name == "container" else context.data_dir
    result = backend.run(command, cwd=cwd, timeout=30)
    context.out(f"后端：{name}")
    context.out(f"命令：{command}")
    context.out(f"工作目录：{result.cwd}")
    context.out(f"退出码：{result.exit_code}（耗时 {result.seconds}s）")
    if result.stdout.strip():
        context.out(f"stdout：{result.stdout.strip()}")
    if result.stderr.strip():
        context.out(f"stderr：{result.stderr.strip()}")
    if result.notice:
        context.note(result.notice)
    if not result.ok:
        context.warn(f"后端 {name} 能起来，但命令没跑成功（退出码 {result.exit_code}）。")
    context.emit(
        {
            "ok": result.ok,
            "command": "terminal test",
            "backend": name,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "seconds": result.seconds,
            "notice": result.notice,
        }
    )
    return 0 if result.ok else 1


# ── 出口策略（MATRIX 4.8 / 14.x 里 Orbit 真能做的那部分）────────────────────


def _policy(context: Context) -> egress.EgressPolicy:
    return egress.EgressPolicy.load(context.data_dir)


def _print_policy(context: Context, policy: egress.EgressPolicy) -> None:
    context.out(f"策略文件：{policy.path}")
    context.out(f"默认动作：{policy.default}")
    context.out()
    context.table(
        ("类型", "规则"),
        [("allow", rule) for rule in policy.allow]
        + [("deny", rule) for rule in policy.deny]
        + [
            ("credentials", f"{rule.host} → {rule.header}（keyring 键 {rule.key or '<未填>'}）")
            for rule in policy.credentials
        ],
    )


def run_egress_policy(context: Context, args: Any) -> int:
    """`orbit egress policy`：当前策略 + 这层到底是什么（如实说明）。"""
    policy = _policy(context)
    context.out("出口策略（静态检查 + 请求头注入）")
    context.out()
    _print_policy(context, policy)
    context.out()
    context.out(policy.describe())
    context.emit(
        {
            "ok": True,
            "command": "egress policy",
            "policy": policy.as_dict(),
            "describe": policy.describe(),
        }
    )
    return 0


def _add_rule(context: Context, args: Any, *, allow: bool) -> int:
    """allow/deny 的共同实现：写盘是**显式**动作，且两边互斥。"""
    raw = str(getattr(args, "host", "") or "").strip()
    host = egress.normalize_host(raw)
    if not host:
        raise CliError(
            f"给一个主机名或 URL（收到 {raw!r}）：例如 orbit egress "
            f"{'allow' if allow else 'deny'} api.example.com"
        )
    # 通配规则按原样存（`*.example.com` 不能先被 normalize_host 拆掉）。
    rule = raw.strip().lower() if ("*" in raw or "?" in raw) else host
    policy = _policy(context)
    if allow:
        if rule not in policy.allow:
            policy.allow.append(rule)
        policy.deny = [item for item in policy.deny if item != rule]
    else:
        if rule not in policy.deny:
            policy.deny.append(rule)
        policy.allow = [item for item in policy.allow if item != rule]
    path = policy.save()
    verb = "allow" if allow else "deny"
    context.out(f"已写入 {verb} 规则：{rule}")
    context.out(f"策略文件：{path}")
    context.note(
        "deny 优先于 allow（冲突时选保守的那个）。这条策略只作用于经过 Orbit 代码的调用点，"
        "不是防火墙。"
    )
    context.emit(
        {
            "ok": True,
            "command": f"egress {verb}",
            "rule": rule,
            "path": str(path),
            "policy": policy.as_dict(),
        }
    )
    return 0


def run_egress_allow(context: Context, args: Any) -> int:
    """`orbit egress allow <host>`：把这个主机加进白名单。"""
    return _add_rule(context, args, allow=True)


def run_egress_deny(context: Context, args: Any) -> int:
    """`orbit egress deny <host>`：显式禁止这个主机出站。"""
    return _add_rule(context, args, allow=False)


def run_egress_check(context: Context, args: Any) -> int:
    """`orbit egress check <url>`：这条 URL 现在能不能出去、会注入哪些头。

    只打印**键名与布尔**，绝不打印密钥值——这条命令的输出会被贴进 issue 和日志。
    """
    url = str(getattr(args, "url", "") or "").strip()
    if not url:
        raise CliError("给一个 URL 或主机名：orbit egress check https://api.example.com/v1")
    policy = _policy(context)
    decision = policy.check(url)
    plan = policy.injection_plan(url)
    context.out(f"目标：{url}")
    context.out(f"主机：{decision.host or '<解析不出>'}")
    context.out(f"判定：{decision.action}")
    context.out(f"理由：{decision.reason}")
    if decision.rule:
        context.out(f"命中规则：{decision.rule}")
    if plan:
        context.out()
        context.table(
            ("注入头", "值来源（keyring 键）", "此刻可取"),
            [
                (row["header"], row["key"] or "<未填>", "是" if row["resolved"] else "否")
                for row in plan
            ],
        )
    else:
        context.out()
        context.out("没有匹配的注入规则（不会添加任何请求头）。")
    context.emit(
        {
            "ok": True,
            "command": "egress check",
            "decision": decision.as_dict(),
            "injection": plan,
        }
    )
    return 0


def run_proxy_unavailable(context: Context, args: Any) -> int:
    """iron-proxy 生命周期命令的统一答复：**没有守护进程可起停**。

    为什么接管这些路径而不是让它们继续显示"计划波次 W13"：那个占位语在暗示
    "等一等就有了"，而这是一个产品决定——Orbit 不做代理进程。说清楚比排期更诚实。
    """
    raise CliError(
        "Orbit 没有 iron-proxy 代理进程，这条命令没有可操作的对象。\n"
        "现在有的是：`orbit egress policy`（allow/deny 静态检查）、"
        "`orbit egress check <url>`（这条 URL 能不能出去）、"
        "`orbit egress allow|deny <host>`（改策略）。\n"
        "真正的令牌代换需要：独立守护进程 + 自签 CA + 按供应商铸造令牌 + 强制出站改道"
        "（防火墙/命名空间）。本机没有容器也没有网络，做不了，也不假装有。",
        code=UNIMPLEMENTED_CODE,
    )


# ── 命令对象与接线 ──────────────────────────────────────────────────────────


def _terminal_command() -> Command:
    """`terminal` 顶层命令（声明表里没有这一条，所以是本模块新建的）。"""
    return Command(
        name="terminal",
        help="命令执行后端（本机 / WSL / 容器）的查看与连通性测试",
        group="run",
        hermes="terminal",
        parity="P1",
        state="done",
        needs_settings=True,
        subcommands=[
            Command(
                name="list",
                help="列出三个后端并说明各自此刻能不能用",
                handler=run_terminal_list,
                group="run",
                hermes="terminal list",
                parity="P1",
                state="done",
            ),
            Command(
                name="test",
                help="在指定后端里跑一条无害命令，验证它真的能执行",
                handler=run_terminal_test,
                group="run",
                hermes="terminal test",
                parity="P1",
                state="done",
                options=[
                    to_option(
                        "--backend {local,wsl,container}",
                        "后端名（默认取 settings.json 的 terminal_backend，再缺省 local）",
                    )
                ],
            ),
        ],
    )


def _extra_subcommands() -> dict[str, list[Command]]:
    """按父命令分组的新增子命令。每条都带 `hermes=`（`parity_rows` 要求每行有对标依据）。"""
    return {
        "tools": [
            Command(
                name="catalog",
                help="Hermes 94 个内置工具在 Orbit 的覆盖情况与逐条缺口原因",
                handler=run_tools_catalog,
                group="ext",
                hermes="tools catalog",
                parity="P2",
                state="done",
                options=[
                    to_option("--json", "以 JSON 输出（给排期/对账用）"),
                    to_option("--domain DOMAIN", "只看某一个功能域"),
                ],
            ),
            Command(
                name="backends",
                help="列出命令执行后端（本机 / WSL / 容器）与可用性",
                handler=run_tools_backends,
                group="ext",
                hermes="tools backends",
                parity="P1",
                state="done",
            )
        ],
        "egress": [
            Command(
                name="policy",
                help="查看出口策略（allow/deny 规则）与这层的真实能力边界",
                handler=run_egress_policy,
                group="ops",
                hermes="egress policy",
                parity="P1",
                state="done",
            ),
            Command(
                name="allow",
                help="把主机加进出站白名单",
                handler=run_egress_allow,
                group="ops",
                hermes="egress allow",
                parity="P1",
                state="done",
                options=[to_option("host", "主机名或 URL（支持 *.example.com）")],
            ),
            Command(
                name="deny",
                help="显式禁止某个主机出站（优先于 allow）",
                handler=run_egress_deny,
                group="ops",
                hermes="egress deny",
                parity="P1",
                state="done",
                options=[to_option("host", "主机名或 URL（支持 *.example.com）")],
            ),
            Command(
                name="check",
                help="判定一条 URL 能否出站、会注入哪些请求头（不回显密钥）",
                handler=run_egress_check,
                group="ops",
                hermes="egress check",
                parity="P1",
                state="done",
                options=[to_option("url", "要判定的 URL 或主机名")],
            ),
        ],
    }


def extend_registry() -> list[str]:
    """把新增子命令追加到已存在的父命令上（**幂等**，返回新增的 `父 子` 路径）。

    追加而不是替换：父命令上原有的子命令一条不少。父命令不存在时返回空列表
    （声明表还没组装完，等 :func:`_install` 的回调再来一次）。
    """
    added: list[str] = []
    for parent_name, children in _extra_subcommands().items():
        parent = REGISTRY.get(parent_name)
        if parent is None:
            continue
        existing = {sub.name for sub in parent.subcommands}
        for child in children:
            if child.name in existing:
                continue
            parent.subcommands.append(child)
            added.append(f"{parent_name} {child.name}")
    return added


def wrap_register_specs(table_module: Any, after: Any) -> bool:
    """把 `register_specs` 包一层：声明表组装完立刻调 `after()`。

    本模块排在 `hermes_parity` **之前**导入时，`egress` 这个父命令还不存在，
    追加无从谈起。唯一的接缝就是等它组装完（与 `mcp.py::wrap_register_specs` 同一套
    做法，**幂等**：已经包过就返回 False，避免 wrapper 套成洋葱）。
    """
    original = table_module.register_specs
    if getattr(original, "_orbit_tools_extra2_wrapped", False):
        return False

    def wrapped(specs):
        skipped = original(specs)
        after()
        return skipped

    wrapped._orbit_tools_extra2_wrapped = True  # type: ignore[attr-defined]
    table_module.register_specs = wrapped
    return True


def _install() -> None:
    """接上所有新增子命令；父命令还没组装就推迟到声明表组装完。"""
    if REGISTRY.get("terminal") is None:
        REGISTRY.add(_terminal_command())
    if REGISTRY.get("egress") is not None:
        extend_registry()
        return
    wrap_register_specs(_table, extend_registry)


#: 表里**由本模块接管**的路径（iron-proxy 生命周期命令 → 如实报告没有代理进程）。
#: 用 `register_handler` 而不是另立命令：这些路径本来就在表里，接管后 `--help` 里的
#: 说明与状态都会如实反映"Orbit 没有这一层"。
PROXY_PATHS: tuple[str, ...] = (
    "egress install",
    "egress start",
    "egress stop",
    "egress restart",
    "egress reload",
    "egress disable",
)

#: 表里**已被 `security.py` 接管**的路径。写在这里是为了让"为什么没接"可被 grep 到：
#: 再登记一次会触发 `register_handler` 的重复登记门禁。
OWNED_ELSEWHERE: tuple[str, ...] = ("egress status", "egress config", "egress setup")


def _bind_proxy_paths() -> None:
    """把 iron-proxy 生命周期路径接管成如实报告（两种导入顺序都成立）。

    `register_handler` 负责"表还没组装"的情况；回填负责"表已经组装完"的情况
    （`security.py::_bind` / `mcp.py::_bind_path` 同一套做法）。
    """
    for path in PROXY_PATHS:
        if path in _table.HANDLERS:
            # 已经接过（模块被重复导入）：不重复登记——`register_handler` 会抛错，
            # 而重复导入在测试与热重载里都可能发生。
            continue
        register_handler(path, run_proxy_unavailable)
        head, _, leaf = path.partition(" ")
        parent = REGISTRY.get(head)
        if parent is None:
            continue
        target = parent.child(leaf) if leaf else parent
        if target is not None and not target.subcommands:
            target.handler = run_proxy_unavailable
            # **`partial` 而不是 `done`**：这条命令存在、也会给出一句考虑过的解释
            # （「Orbit 没有 iron-proxy 代理进程，这条命令没有可操作的对象」），
            # 但**功能本身没实现**。标 done 会把"如实说没有"算成"有"——
            # 这正是这个项目反复要防的那类虚报。
            target.state = "partial"


def mark_proxy_paths_partial() -> list[str]:
    """把 iron-proxy 生命周期命令的状态按事实改成 `partial`，返回改到的路径。

    为什么需要单独一个函数：本模块在 `hermes_parity` **之前**导入，那时表还没组装，
    `REGISTRY.get("egress")` 是 None，`_bind_proxy_paths` 里的回填只能 `continue`。
    之后 `Spec.build()` 见到 `HANDLERS[path]` 存在就把状态设成 `done`——于是
    「如实报告没有代理进程」被算成了「已实现」。

    `app/cli/commands/__init__.py` 在 `hermes_parity` 之后调用本函数把状态改回来。
    **标 done 会把"如实说没有"算成"有"**，这正是这个项目反复要防的那类虚报。
    """
    changed: list[str] = []
    for path in PROXY_PATHS:
        head, _, leaf = path.partition(" ")
        parent = REGISTRY.get(head)
        if parent is None:
            continue
        target = parent.child(leaf) if leaf else parent
        if target is not None and not target.subcommands and target.state != "partial":
            target.state = "partial"
            changed.append(path)
    return changed


_bind_proxy_paths()
_install()
