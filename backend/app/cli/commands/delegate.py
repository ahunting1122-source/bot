"""委派 / swarm / worktree 的命令面。

三条命令线，来源不同，接线方式也不同——这里必须写清楚，否则下一个人会以为
"表里没有就是没做"：

1. **``worktree list`` / ``worktree prune``**：Hermes 声明表里**已有**这两条
   （``hermes_parity.py`` 的 ``worktree`` 段，``list`` 别名 ``ls``/``audit``）。
   本轮只把实现接上去（``register_handler`` + 回填），**不改表**。
2. **``worktree create`` / ``remove`` / ``cleanup``**：表里没有。Hermes 的
   ``worktree`` 只有 ``list/audit/prune``（"attended reclaim"——回收是人工确认的），
   而 Orbit 这一轮要实现"给子任务起一棵树"，所以必须能建、能按名字删。
   这三条由本模块 ``mount_worktree_subcommands()`` 挂到已存在的 ``worktree`` 上，
   做法与 ``security.py`` 补 ``secrets`` 子命令完全一致（含"表还没组装"时的
   组装钩子）。
3. **``delegate`` / ``swarm`` 顶层命令**：**Orbit 自有，Hermes 没有同名 CLI**——
   Hermes 的委派面是 ``delegate_task`` / ``agent_swarm`` 两个**工具**，不是命令。
   所以这两条用 ``REGISTRY.add`` 建，``hermes=""``（不占 Hermes 命令位）。

账本：``<data_dir>/delegations.jsonl``，append-only。为什么要有它：
``DelegateRunner`` 是**进程内**对象，一次 CLI 进程退出后什么都不剩，
``delegate list/show/cancel`` 就无从谈起。所以每次 ``delegate run`` / ``swarm run``
落一行账（谁、什么目标、什么状态、什么时候）。``cancel`` 追加一条
``status=cancelled`` 事件而不是改写旧行——append-only 的文件被就地改写，
断电就是半个 JSON，而"最后一条有效记录获胜"这种折叠规则不会。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option
from app.delegate import (
    CHILD_BUDGET_STEPS,
    PARENT_BUDGET_STEPS,
    DelegateRunner,
    DelegateSpec,
)
from app.swarm import SwarmError, SwarmPlan, SwarmTask
from app.worktree import Worktree, WorktreeError

#: 账本文件名（相对 ``data_dir``）。
LEDGER_NAME = "delegations.jsonl"


# ── 账本 ────────────────────────────────────────────────────────────────────


def _ledger_path(context: Context) -> Path:
    return context.data_dir / LEDGER_NAME


def _append_ledger(context: Context, record: dict[str, Any]) -> None:
    """追加一条账。写不进去只警告不失败——记账是**辅助**，不该让命令本身失败。"""
    path = _ledger_path(context)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except OSError as exc:
        context.warn(f"委派账本写入失败（不影响本次结果）：{exc}")


def _read_ledger(context: Context) -> list[dict[str, Any]]:
    """读账本并**按 id 折叠**：同一 id 的最后一条记录获胜（见模块 docstring）。"""
    path = _ledger_path(context)
    if not path.exists():
        return []
    folded: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue  # 半行（断电/并发写）：跳过，不让一条坏行毁掉整个账本
        if not isinstance(record, dict):
            continue
        key = str(record.get("id") or "")
        if not key:
            continue
        if key not in folded:
            order.append(key)
            folded[key] = {}
        # 合并而不是覆盖：`delegate cancel` 只追加了 status，若整条替换，
        # `delegate list` 里的 goal/depth 就会凭空消失。
        folded[key].update(record)
    return [folded[key] for key in order]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ── delegate ────────────────────────────────────────────────────────────────


def _spec_from_args(args: Any) -> DelegateSpec:
    """把命令行参数折成 ``DelegateSpec``。``--dry-run`` 与真跑共用这一处。"""
    goal = str(getattr(args, "goal", "") or "").strip()
    if not goal:
        raise CliError("goal 不能为空：`orbit delegate run \"<子任务目标>\"`")
    tools = getattr(args, "allow_tools", None) or ""
    return DelegateSpec(
        goal=goal,
        depth=int(getattr(args, "depth", 1) or 1),
        budget_steps=int(getattr(args, "budget_steps", 0) or 0),
        budget_tokens=int(getattr(args, "budget_tokens", 0) or 0),
        worktree=str(getattr(args, "worktree", "") or ""),
        allowed_tools=tuple(item.strip() for item in str(tools).split(",") if item.strip()),
        model=str(getattr(args, "model", "") or ""),
    )


def run_delegate_run(context: Context, args) -> int:
    """跑一个子任务。没有注入 runner 时**明确失败**，不假装跑过。"""
    spec = _spec_from_args(args)
    runner = DelegateRunner()
    if getattr(args, "dry_run", False):
        budget = runner.budget_preview(spec, 1)
        context.out("委派预演（--dry-run，未启动任何子任务）")
        context.out(f"  goal       : {spec.goal}")
        context.out(f"  depth      : {spec.depth}（上限 {runner.max_depth}）")
        context.out(f"  并发上限   : {runner.max_concurrent}")
        context.out(f"  步数预算   : {budget.steps}（父 {PARENT_BUDGET_STEPS} / 子默认 {CHILD_BUDGET_STEPS}）")
        context.out(f"  token 预算 : {budget.tokens}（地板 2000，父剩余 50% 均分）")
        context.out(f"  worktree   : {spec.worktree or '（不隔离）'}")
        context.out(f"  允许工具   : {'、'.join(spec.allowed_tools) or '（不限制）'}")
        context.out(f"  runner     : {'已注入' if runner.available else '未注入（真跑会失败）'}")
        context.emit({"ok": True, "dry_run": True, "spec": _spec_dict(spec), "budget": budget.summary()})
        return 0
    if not runner.available:
        raise CliError(
            "未注入子任务 runner：`orbit delegate run` 需要 runner_factory（接线在 "
            "app/agent.py / app/policies.py，见 app/delegate.py 模块 docstring）。"
            "先看预演结果用 `orbit delegate run --dry-run \"<目标>\"`。",
            code=3,
        )
    result = context.run(runner.run(spec))
    _append_ledger(
        context,
        {
            "kind": "delegate",
            "id": result.child_task_id,
            "goal": spec.goal,
            "status": result.status,
            "exit_reason": result.exit_reason,
            "depth": spec.depth,
            "worktree": spec.worktree,
            "steps": result.steps,
            "tokens": result.tokens,
            "at": _now(),
        },
    )
    context.out(result.output or "（子任务没有正文输出）")
    if not result.ok:
        context.warn(f"{result.status}：{result.error}")
    context.emit({"ok": result.ok, **result.to_dict()})
    return 0 if result.ok else 1


def _spec_dict(spec: DelegateSpec) -> dict[str, Any]:
    return {
        "goal": spec.goal,
        "depth": spec.depth,
        "budget_steps": spec.budget_steps,
        "budget_tokens": spec.budget_tokens,
        "worktree": spec.worktree,
        "allowed_tools": list(spec.allowed_tools),
        "model": spec.model,
    }


def run_delegate_list(context: Context, args) -> int:
    records = [item for item in _read_ledger(context) if item.get("kind") == "delegate"]
    limit = int(getattr(args, "limit", 20) or 20)
    shown = records[-limit:] if limit > 0 else records
    if not shown:
        context.out("（还没有委派记录）")
        context.note("起一次委派：orbit delegate run --dry-run \"<子任务目标>\"（预演不需要模型）")
        context.emit({"ok": True, "count": 0, "records": []})
        return 0
    context.table(
        ("id", "状态", "层", "步", "worktree", "目标"),
        [
            (
                str(item.get("id") or ""),
                str(item.get("status") or ""),
                str(item.get("depth") or ""),
                str(item.get("steps") or ""),
                str(item.get("worktree") or "-"),
                str(item.get("goal") or "")[:40],
            )
            for item in shown
        ],
    )
    context.emit({"ok": True, "count": len(shown), "records": shown})
    return 0


def run_delegate_show(context: Context, args) -> int:
    wanted = str(getattr(args, "child_task_id", "") or "").strip()
    if not wanted:
        raise CliError("请给子任务 id：`orbit delegate show <id>`（id 用 `orbit delegate list` 查）")
    for record in _read_ledger(context):
        if str(record.get("id") or "") == wanted:
            for key, value in record.items():
                context.out(f"{key:12}: {value}")
            context.emit({"ok": True, "record": record})
            return 0
    raise CliError(f"账本里没有这个子任务：{wanted}（`orbit delegate list` 看现有的）")


def run_delegate_cancel(context: Context, args) -> int:
    wanted = str(getattr(args, "child_task_id", "") or "").strip()
    if not wanted:
        raise CliError("请给子任务 id：`orbit delegate cancel <id>`")
    known = {str(item.get("id") or "") for item in _read_ledger(context)}
    if wanted not in known:
        raise CliError(f"账本里没有这个子任务：{wanted}")
    _append_ledger(
        context,
        {"kind": "delegate", "id": wanted, "status": "cancelled", "at": _now()},
    )
    context.out(f"已在账本上把 {wanted} 标记为 cancelled")
    context.note(
        "说明：真正的中断由 DelegateRunner.cancel(child_task_id) 完成（子任务交出 "
        "partial_output，父任务拿到 status=interrupted）。CLI 是一次性进程，够不到"
        "另一个进程里的子任务，所以这里只落标记。"
    )
    context.emit({"ok": True, "id": wanted, "status": "cancelled"})
    return 0


# ── swarm ───────────────────────────────────────────────────────────────────


def _plan_from_file(path_text: str) -> SwarmPlan:
    """读一份 JSON 计划：``{"tasks":[{"id","goal","depends_on",...}]}``。"""
    path = Path(str(path_text)).expanduser()
    if not path.exists():
        raise CliError(f"计划文件不存在：{path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise CliError(f"计划文件不是合法 JSON：{path}（{exc}）") from exc
    raw_tasks = data.get("tasks") if isinstance(data, dict) else data
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise CliError("计划里没有 tasks：需要 {\"tasks\": [{\"id\": ..., \"goal\": ...}, ...]}")
    tasks: list[SwarmTask] = []
    for index, item in enumerate(raw_tasks):
        if not isinstance(item, dict):
            raise CliError(f"tasks[{index}] 不是对象")
        node_id = str(item.get("id") or f"t{index}")
        tasks.append(
            SwarmTask(
                id=node_id,
                spec=DelegateSpec(
                    goal=str(item.get("goal") or ""),
                    depth=int(item.get("depth") or 1),
                    budget_steps=int(item.get("budget_steps") or 0),
                    budget_tokens=int(item.get("budget_tokens") or 0),
                    worktree=str(item.get("worktree") or ""),
                    allowed_tools=tuple(item.get("allowed_tools") or ()),
                    model=str(item.get("model") or ""),
                ),
                depends_on=tuple(str(dep) for dep in (item.get("depends_on") or ())),
            )
        )
    return SwarmPlan(tasks=tuple(tasks))


def run_swarm_run(context: Context, args) -> int:
    plan = _plan_from_file(str(getattr(args, "plan", "") or ""))
    try:
        levels = plan.levels()
    except SwarmError as exc:
        raise CliError(str(exc)) from exc
    if getattr(args, "dry_run", False):
        context.out(f"swarm 预演：{len(plan.tasks)} 个节点、{len(levels)} 层（未启动任何子任务）")
        for index, level in enumerate(levels, 1):
            context.out(f"  第 {index} 层（并发 {len(level)}）：{'、'.join(level)}")
        context.emit({"ok": True, "dry_run": True, "levels": levels})
        return 0
    runner = DelegateRunner()
    if not runner.available:
        raise CliError(
            "未注入子任务 runner：`orbit swarm run` 需要 runner_factory（接线在 "
            "app/agent.py / app/policies.py）。先看拓扑分层用 "
            "`orbit swarm run --plan <file> --dry-run`。",
            code=3,
        )
    report = context.run(_run_plan_async(plan, runner))
    _append_ledger(
        context,
        {
            "kind": "swarm",
            "id": f"swarm-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}",
            "nodes": len(plan.tasks),
            "ok": report.ok,
            "status": "ok" if report.ok else "failed",
            "failed": report.failed,
            "skipped": report.skipped,
            "order": report.order,
            "at": _now(),
        },
    )
    context.table(
        ("节点", "状态", "原因"),
        [(node, status, (report.reasons.get(node) or "")[:50]) for node, status in report.statuses.items()],
    )
    context.emit(report.to_dict())
    return 0 if report.ok else 1


async def _run_plan_async(plan: SwarmPlan, runner: DelegateRunner):
    from app.swarm import run_plan

    return await run_plan(plan, runner=runner)


def run_swarm_status(context: Context, args) -> int:
    records = [item for item in _read_ledger(context) if item.get("kind") == "swarm"]
    limit = int(getattr(args, "limit", 10) or 10)
    shown = records[-limit:] if limit > 0 else records
    if not shown:
        context.out("（还没有跑过的 swarm）")
        context.note(
            "计划格式：{\"tasks\":[{\"id\":\"a\",\"goal\":\"…\",\"depends_on\":[]}]}；"
            "预演：orbit swarm run --plan <file> --dry-run"
        )
        context.emit({"ok": True, "count": 0, "records": []})
        return 0
    context.table(
        ("id", "状态", "节点", "失败", "跳过", "时间"),
        [
            (
                str(item.get("id") or ""),
                str(item.get("status") or ""),
                str(item.get("nodes") or ""),
                "、".join(item.get("failed") or []) or "-",
                "、".join(item.get("skipped") or []) or "-",
                str(item.get("at") or ""),
            )
            for item in shown
        ],
    )
    context.emit({"ok": True, "count": len(shown), "records": shown})
    return 0


# ── worktree ────────────────────────────────────────────────────────────────


def _worktree(context: Context, args) -> Worktree:
    """按 ``--repo`` 造一个 Worktree；没给就用 ``settings.workspace``。

    默认值选 workspace 而不是 cwd：``orbit`` 可能从任何目录被调用，而"我要管理的
    是哪个仓库"必须是显式的、可复现的。
    """
    repo = getattr(args, "repo", None)
    target = Path(str(repo)).expanduser() if repo else Path(str(context.settings.workspace))
    return Worktree(context.data_dir, target)


def run_worktree_list(context: Context, args) -> int:
    manager = _worktree(context, args)
    try:
        infos = manager.list()
    except WorktreeError as exc:
        raise CliError(str(exc)) from exc
    if not infos:
        context.out("（这个仓库没有工作树）")
        context.emit({"ok": True, "count": 0, "worktrees": []})
        return 0
    context.table(
        ("名字", "归属", "分支", "改动", "结论", "路径"),
        [
            (
                info.name,
                "orbit" if info.managed else "外部",
                info.branch.replace("refs/heads/", "") or "-",
                "有" if info.dirty else ("?" if info.status_error else "无"),
                info.verdict,
                str(info.path),
            )
            for info in infos
        ],
    )
    context.emit({"ok": True, "count": len(infos), "worktrees": [info.to_dict() for info in infos]})
    return 0


def run_worktree_create(context: Context, args) -> int:
    manager = _worktree(context, args)
    try:
        info = manager.create(
            None,
            str(getattr(args, "name", "") or ""),
            branch=str(getattr(args, "branch", "") or ""),
            base=str(getattr(args, "base", "") or ""),
        )
    except WorktreeError as exc:
        raise CliError(str(exc)) from exc
    context.out(f"已创建 worktree {info.name} → {info.path}（分支 {info.branch}）")
    context.emit({"ok": True, "worktree": info.to_dict()})
    return 0


def run_worktree_remove(context: Context, args) -> int:
    manager = _worktree(context, args)
    try:
        info = manager.remove(str(getattr(args, "name", "") or ""), force=bool(getattr(args, "force", False)))
    except WorktreeError as exc:
        raise CliError(str(exc)) from exc
    context.out(f"已删除 worktree {info.name}（{info.path}）")
    context.note("分支没有删：worktree 只是工作区，分支里的提交还在。")
    context.emit({"ok": True, "removed": info.to_dict()})
    return 0


def run_worktree_cleanup(context: Context, args) -> int:
    """回收 prunable 的工作树。``--dry-run`` 只报不删。"""
    manager = _worktree(context, args)
    dry_run = bool(getattr(args, "dry_run", False))
    try:
        removed = manager.cleanup_stale(dry_run=dry_run)
    except WorktreeError as exc:
        raise CliError(str(exc)) from exc
    mode = "预览（--dry-run，未删除任何东西）" if dry_run else "已执行"
    context.out(f"worktree 回收 — {mode}：{len(removed)} 棵")
    if removed:
        context.out("  " + "、".join(removed))
    else:
        context.note("  （没有 prunable 的工作树；有改动的树永远不会被这里删掉）")
    if getattr(args, "trees_only", False) or getattr(args, "branches_only", False):
        context.note("  注：--trees-only / --branches-only 已接受，但本实现**不动分支**——"
                     "删分支要看是否已合并，那是独立的一次决策。")
    context.emit({"ok": True, "dry_run": dry_run, "removed": removed})
    return 0


# ── 接线 ────────────────────────────────────────────────────────────────────


def _bind(path: str, handler: Any) -> None:
    """把实现接到声明表的某条路径上（两种导入顺序都要成立）。

    与 ``curator.py::_bind`` 同一套做法：``register_handler`` 负责"表还没组装"的
    情况，回填负责"表已经组装完"（``import app.cli.commands.delegate`` 会先跑包
    初始化，那时表可能已经建好，``HANDLERS`` 里的登记不会自动回填）。
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


#: 声明表里**缺的** `worktree` 子命令：``(名字, 中文说明, 选项, handler)``。
#:
#: 为什么由本模块补：Hermes 的 `worktree` 只有 list/audit/prune（"人工确认后回收"），
#: 没有"起一棵树"这条——因为 Hermes 是在 `hermes -w` 里隐式建树的。Orbit 要把
#: "给子任务起一棵树"变成可脚本化的显式动作，所以补 create/remove/cleanup。
EXTRA_WORKTREE_SUBCOMMANDS: tuple[tuple[str, str, tuple[Option, ...], Any], ...] = (
    (
        "create",
        "在 <data_dir>/worktrees/<name> 建一棵隔离工作树（默认分支 orbit/<name>）",
        (
            Option(("name",), "工作树名字（单层目录名，不能含路径分隔符）"),
            Option(("--repo",), "仓库路径（默认 settings.workspace）", metavar="REPO"),
            Option(("--branch",), "新建的分支名（默认 orbit/<name>）", metavar="BRANCH"),
            Option(("--base",), "从哪个引用拉（默认 HEAD，即父当前 commit）", metavar="BASE"),
        ),
        run_worktree_create,
    ),
    (
        "remove",
        "删除一棵工作树（有未提交改动时拒绝，除非 --force）",
        (
            Option(("name",), "工作树名字"),
            Option(("--repo",), "仓库路径（默认 settings.workspace）", metavar="REPO"),
            Option(("--force",), "有未提交改动也删（不可逆）", action="store_true"),
        ),
        run_worktree_remove,
    ),
    (
        "cleanup",
        "回收 prunable 的工作树（有改动的树永远不删）",
        (
            Option(("--repo",), "仓库路径（默认 settings.workspace）", metavar="REPO"),
            Option(("--dry-run",), "只报不删", action="store_true"),
        ),
        run_worktree_cleanup,
    ),
)


def mount_worktree_subcommands() -> list[str]:
    """把 ``EXTRA_WORKTREE_SUBCOMMANDS`` 挂到已存在的 ``worktree`` 上。**幂等**。"""
    parent = REGISTRY.get("worktree")
    if parent is None:
        return []
    mounted: list[str] = []
    for name, help_text, options, handler in EXTRA_WORKTREE_SUBCOMMANDS:
        if parent.child(name) is not None:
            continue
        parent.subcommands.append(
            Command(
                name=name,
                help=help_text,
                handler=handler,
                group="run",
                state="done",
                options=list(options),
            )
        )
        mounted.append(name)
    return mounted


#: 组装钩子是否已装过（幂等标记）。与 ``security.py`` 同一个理由：本模块若被排在
#: ``hermes_parity`` **之前**导入，`REGISTRY.get("worktree")` 还是 None，
#: 只能等表组装完再补挂。
_ASSEMBLY_HOOK_INSTALLED = False


def _install_assembly_hook() -> bool:
    from app.cli.commands import _table as table_module

    global _ASSEMBLY_HOOK_INSTALLED
    if _ASSEMBLY_HOOK_INSTALLED:
        return False
    original = table_module.register_specs

    def register_specs(specs: Any) -> list[str]:
        skipped = original(specs)
        mount_worktree_subcommands()
        return skipped

    table_module.register_specs = register_specs
    _ASSEMBLY_HOOK_INSTALLED = True
    return True


_bind("worktree list", run_worktree_list)
# `worktree prune` 与 `worktree cleanup` 是同一件事的两个名字：prune 是 Hermes 的
# 叫法（表里已有），cleanup 是 Orbit 补的。两者都走 cleanup_stale。
_bind("worktree prune", run_worktree_cleanup)

REGISTRY.add(
    Command(
        name="delegate",
        help="子任务委派：深度 / 并发 / 预算三道闸（Hermes 没有同名 CLI，委派面是 delegate_task 工具）",
        group="run",
        parity="P1",
        state="done",
        hermes="",
        subcommands=[
            Command(
                name="run",
                help="跑一个子任务（--dry-run 只看预算与上限，不需要模型）",
                handler=run_delegate_run,
                group="run",
                state="done",
                options=[
                    Option(("goal",), "子任务目标"),
                    Option(("--depth",), "本子任务所在层级（1 = 父派出的子任务）", default=1, metavar="N"),
                    Option(("--budget-steps",), "迭代步数预算（0 = 默认 50）", default=0, metavar="N"),
                    Option(("--budget-tokens",), "token 预算（0 = 父剩余 50% 均分，地板 2000）", default=0, metavar="N"),
                    Option(("--worktree",), "在 <data_dir>/worktrees/<name> 里跑（git 隔离）", metavar="NAME"),
                    Option(("--allow-tools",), "允许的工具名，逗号分隔（默认不限制）", metavar="LIST"),
                    Option(("--model",), "子任务用的模型（默认继承父）", metavar="MODEL"),
                    Option(("--dry-run",), "只打印解析后的上限/预算，不启动子任务", action="store_true"),
                ],
            ),
            Command(
                name="list",
                help="列委派账本（最近 N 条）",
                handler=run_delegate_list,
                group="run",
                state="done",
                options=[Option(("--limit",), "最多显示多少条（0 = 全部）", default=20, metavar="N")],
            ),
            Command(
                name="show",
                help="看一条委派记录的完整字段",
                handler=run_delegate_show,
                group="run",
                state="done",
                options=[Option(("child_task_id",), "子任务 id（delegate list 里查）")],
            ),
            Command(
                name="cancel",
                help="把账本上的子任务标记为 cancelled（进程内中断走 DelegateRunner.cancel）",
                handler=run_delegate_cancel,
                group="run",
                state="done",
                options=[Option(("child_task_id",), "子任务 id")],
            ),
        ],
    ),
    Command(
        name="swarm",
        help="批量并发：按 depends_on 拓扑序跑，同层并发（Hermes 侧是 agent_swarm 工具）",
        group="run",
        parity="P1",
        state="done",
        hermes="",
        subcommands=[
            Command(
                name="run",
                help="按计划文件跑一批子任务（--dry-run 只打印拓扑分层）",
                handler=run_swarm_run,
                group="run",
                state="done",
                options=[
                    Option(("--plan",), "计划文件（JSON：{\"tasks\":[{\"id\",\"goal\",\"depends_on\"}]}）", metavar="FILE"),
                    Option(("--dry-run",), "只打印拓扑分层，不启动子任务", action="store_true"),
                ],
            ),
            Command(
                name="status",
                help="看最近跑过的 swarm（读委派账本）",
                handler=run_swarm_status,
                group="run",
                state="done",
                options=[Option(("--limit",), "最多显示多少条（0 = 全部）", default=10, metavar="N")],
            ),
        ],
    ),
)

# 表还没组装（本模块排在 `hermes_parity` 之前）→ 装组装钩子；已组装 → 立即挂上。
if REGISTRY.get("worktree") is None:
    _install_assembly_hook()
mount_worktree_subcommands()
