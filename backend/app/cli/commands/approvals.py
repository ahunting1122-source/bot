"""审批命令：离线裁决预演、历史挖掘、白名单与模式管理。

对齐 Hermes 的两条：`approvals test`（三态退出码 0 allow / 2 ask / 3 deny）与
`approvals suggest`（从会话历史里挖"反复出现且用户实际批准过"的危险模式，
**破坏类永不提议**）。另加两条 Orbit 自有的管理命令 `allow` / `mode`——
没有它们，白名单只能手改 JSON，等于把安全边界交给手误。
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select

from app import approvals
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option


def _policy(context: Context) -> tuple[str, tuple[str, ...]]:
    settings = context.settings
    mode = approvals.resolve_mode(settings)
    allowlist = approvals.AllowList.load(Path(settings.data_dir) / "approvals.json").rules
    return mode, allowlist


# ── test ────────────────────────────────────────────────────────────────────


def run_test(context: Context, args) -> int:
    """离线裁决，**不执行、不提示、不落盘**。"""
    tokens = list(getattr(args, "command", []) or [])
    # `REMAINDER` 会把分隔用的 `--` 也当位置参数收进来（它只是"别把后面的当选项"，
    # 并不从结果里删掉）。裁决对象是命令本身，不是那个分隔符。
    while tokens and tokens[0] == "--":
        tokens.pop(0)
    if not tokens:
        raise CliError(
            "用法：orbit approvals test [--env-type TYPE] -- <命令>\n"
            "命令自己的 flag 会跟 ours 打架，所以要用 `--` 隔开。"
        )
    command = " ".join(tokens)
    mode, allowlist = _policy(context)
    verdict = approvals.classify(
        command,
        mode=mode,
        allowlist=allowlist,
        env_type=str(getattr(args, "env_type", None) or "local"),
        hardline_in_machine=approvals.resolve_hardline_in_machine(context.settings),
        machine_wide=context.settings.machine_wide,
    )
    context.out(f"命令：{command}")
    context.out(f"归一化：{verdict.normalized}")
    context.out(f"裁决：{verdict.verdict}")
    context.out(f"规则：{verdict.rule or '（未命中）'}")
    context.out(f"说明：{verdict.reason}")
    context.out(f"审批模式：{mode}" + ("（完全访问档）" if context.settings.machine_wide else "（仅工作文件夹）"))
    context.emit({"ok": True, "command": command, **verdict.as_dict(), "mode": mode})
    return verdict.exit_code


# ── suggest ─────────────────────────────────────────────────────────────────


def _history_commands(context: Context, days: int) -> list[tuple[str, str]]:
    """从 `tool_calls` 里取出历史 `run_command` 的命令，附带任务创建时间。

    `tool_calls` 表本身**没有时间戳**（它只有 id / task_id / name / status / args / result），
    所以时间窗口只能靠 join 到 `tasks.created_at`。这一点必须如实——假装能按
    工具调用时间过滤，会让 `--days` 变成一个骗人的参数。
    """
    from app.storage import Operation, Task

    statement = select(Operation).where(Operation.name == "run_command")
    operations = context.run(context.store.rows(statement))
    tasks = {task.id: task for task in context.run(context.store.rows(select(Task)))}
    cutoff = ""
    if days > 0:
        from datetime import UTC, datetime, timedelta

        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    out: list[tuple[str, str]] = []
    for operation in operations:
        task = tasks.get(operation.task_id)
        created = (task.created_at if task else "") or ""
        if cutoff and created and created < cutoff:
            continue
        command = str((operation.args or {}).get("command") or "").strip()
        if command:
            out.append((command, created))
    return out


def run_suggest(context: Context, args) -> int:
    days = int(getattr(args, "days", 90) or 0)
    min_count = int(getattr(args, "min_count", 2) or 1)
    limit = int(getattr(args, "limit", 20) or 20)
    history = _history_commands(context, days)
    groups: dict[str, dict] = {}
    for command, _created in history:
        # 用 off 模式裁决：问的是"这条命令**在放行模式下**是否属于危险类"，
        # 而不是"它当时被拦了没有"。后者只会挖出被拒的记录，提不出有用建议。
        verdict = approvals.classify(command, mode="off", env_type="local")
        if not verdict.rule:
            continue
        bucket = groups.setdefault(
            verdict.normalized,
            {
                "normalized": verdict.normalized,
                "rule": verdict.rule,
                "label": verdict.label,
                "destructive": verdict.destructive,
                "count": 0,
            },
        )
        bucket["count"] += 1

    proposals = [
        item
        for item in groups.values()
        if item["count"] >= min_count and not item["destructive"]
    ]
    proposals.sort(key=lambda item: item["count"], reverse=True)
    proposals = proposals[:limit]

    skipped = sorted(
        {item["rule"] for item in groups.values() if item["destructive"] and item["count"] >= min_count}
    )
    context.out(f"扫描范围：最近 {days or '全部'} 天 · {len(history)} 条历史命令")
    context.out()
    if proposals:
        context.table(
            ("#", "次数", "规则", "归一化命令"),
            [
                (str(index), str(item["count"]), item["rule"], item["normalized"])
                for index, item in enumerate(proposals, start=1)
            ],
        )
    else:
        context.out("没有可提议的白名单条目。")
    if skipped:
        context.out()
        context.out(f"以下破坏类**永不提议**（{len(skipped)} 条）：{'、'.join(skipped)}")

    applied: list[str] = []
    raw_apply = getattr(args, "apply", None)
    if raw_apply:
        indexes = []
        for chunk in str(raw_apply).split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            if not chunk.isdigit():
                raise CliError(f"--apply 只接受编号（如 1,3）：{chunk!r}")
            indexes.append(int(chunk))
        store = approvals.AllowList.load(Path(context.settings.data_dir) / "approvals.json")
        for index in indexes:
            if not 1 <= index <= len(proposals):
                raise CliError(f"编号越界：{index}（本次只有 {len(proposals)} 条提议）")
            rule = proposals[index - 1]["rule"]
            if store.add(rule, note=f"approvals suggest · {proposals[index - 1]['normalized']}"):
                applied.append(rule)
        context.out()
        context.out(f"已加白：{'、'.join(applied) if applied else '（都已存在）'}")
    else:
        context.out()
        context.out("以上只是建议，未写入。要采纳：orbit approvals suggest --apply 1,3")

    context.emit(
        {
            "ok": True,
            "days": days,
            "scanned": len(history),
            "proposals": proposals,
            "never_proposed": skipped,
            "applied": applied,
        }
    )
    return 0


# ── allow / mode ────────────────────────────────────────────────────────────


def _allowlist(context: Context) -> approvals.AllowList:
    return approvals.AllowList.load(Path(context.settings.data_dir) / "approvals.json")


def _subcommand_name(args) -> str:
    """取当前子命令名。

    用 `args._command`（`main` 在解析时挂上的 Command 对象）而不是 `args._xxx_command`
    ——后者是 argparse 的内部 dest 命名，改一次 `_add_command` 就会静默失效。
    """
    command = getattr(args, "_command", None)
    return str(getattr(command, "name", "") or "")


def run_allow(context: Context, args) -> int:
    action = _subcommand_name(args)
    store = _allowlist(context)
    if action == "list":
        if store.entries:
            context.table(
                ("规则", "备注"),
                [(str(item.get("rule", "")), str(item.get("note", ""))) for item in store.entries],
            )
        else:
            context.out("白名单为空。")
        context.emit({"ok": True, "allow": store.entries})
        return 0
    rule = str(getattr(args, "rule", "") or "").strip()
    if not rule:
        raise CliError("用法：orbit approvals allow {add,remove} <规则名>")
    if action == "add":
        known = {entry[1] for entry in approvals.DANGEROUS_PATTERNS} | {
            entry[1] for entry in approvals.HARDLINE_PATTERNS
        }
        if rule not in known:
            raise CliError(
                f"未知规则名：{rule}。可用规则见 `orbit approvals list-rules`。"
            )
        if any(entry[1] == rule for entry in approvals.HARDLINE_PATTERNS):
            raise CliError(
                f"{rule} 是硬地板规则，加白没有意义——硬地板与白名单无关，任何模式都拦。"
            )
        added = store.add(rule)
        context.out(f"{'已加白' if added else '已在白名单里'}：{rule}")
        context.emit({"ok": True, "rule": rule, "added": added})
        return 0
    removed = store.remove(rule)
    context.out(f"{'已移除' if removed else '不在白名单里'}：{rule}")
    context.emit({"ok": True, "rule": rule, "removed": removed})
    return 0


def run_list_rules(context: Context, args) -> int:
    context.out("硬地板（任何模式都拦）：")
    context.table(
        ("规则", "说明"),
        [(rule, label) for _pattern, rule, label in approvals.HARDLINE_PATTERNS],
    )
    context.out()
    context.out("危险模式（按审批模式处置）：")
    context.table(
        ("规则", "破坏类", "说明"),
        [
            (rule, "是" if destructive else "否", label)
            for _pattern, rule, label, destructive in approvals.DANGEROUS_PATTERNS
        ],
    )
    context.emit(
        {
            "ok": True,
            "hardline": [
                {"rule": rule, "label": label}
                for _pattern, rule, label in approvals.HARDLINE_PATTERNS
            ],
            "dangerous": [
                {"rule": rule, "label": label, "destructive": destructive}
                for _pattern, rule, label, destructive in approvals.DANGEROUS_PATTERNS
            ],
        }
    )
    return 0


def run_mode(context: Context, args) -> int:
    """查看或切换审批模式。写 `data/settings.json`，**不需要重启后端**。"""
    requested = getattr(args, "value", None)
    if not requested:
        mode = approvals.resolve_mode(context.settings)
        hardline = approvals.resolve_hardline_in_machine(context.settings)
        context.out(f"审批模式：{mode}")
        context.out(f"完全访问档守硬地板：{'是' if hardline else '否'}")
        context.out()
        context.out("可选模式：off（只拦硬地板）/ smart（危险模式需白名单）/ manual（危险模式一律拒）")
        context.emit({"ok": True, "mode": mode, "hardline_in_machine": hardline})
        return 0
    value = str(requested).strip().lower()
    if value not in approvals.APPROVAL_MODES:
        raise CliError(f"未知模式：{value}（可选：{'/'.join(approvals.APPROVAL_MODES)}）")
    path = context.settings_path
    data: dict = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
    data["approval_mode"] = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    context.out(f"已写入 {path}：approval_mode={value}")
    context.emit({"ok": True, "mode": value})
    return 0


REGISTRY.add(
    Command(
        name="approvals",
        help="命令审批：离线裁决预演、白名单、模式管理",
        group="ops",
        state="done",
        needs_settings=False,
        subcommands=[
            Command(
                name="test",
                help="离线裁决一条命令（不执行、不提示、不落盘）",
                handler=run_test,
                group="ops",
                state="done",
                needs_settings=False,
                options=[
                    Option(("--env-type",), "按哪个终端后端裁决（默认 local；容器类后端跳过守卫）", metavar="ENV_TYPE"),
                    Option(("command",), "要裁决的命令；用 `--` 隔开它自己的 flag", nargs="..."),
                ],
            ),
            Command(
                name="suggest",
                help="从历史命令里挖可加白的危险模式（破坏类永不提议）",
                handler=run_suggest,
                group="ops",
                state="done",
                needs_settings=False,
                needs_store=True,
                options=[
                    Option(("--apply",), "采纳指定编号的提议（如 1,3）", metavar="N[,M...]"),
                    Option(("--days",), "回溯天数（0 = 全部）", default=90, metavar="DAYS"),
                    Option(("--min-count",), "提议所需的最少出现次数", default=2, metavar="MIN_COUNT"),
                    Option(("--limit",), "最多显示多少条提议", default=20, metavar="LIMIT"),
                ],
            ),
            Command(
                name="allow",
                help="管理命令白名单",
                group="ops",
                state="done",
                needs_settings=False,
                subcommands=[
                    Command(
                        name="list",
                        help="列出白名单",
                        handler=run_allow,
                        group="ops",
                        state="done",
                        needs_settings=False,
                    ),
                    Command(
                        name="add",
                        help="加白一条危险规则",
                        handler=run_allow,
                        group="ops",
                        state="done",
                        needs_settings=True,
                        options=[Option(("rule",), "危险规则名（见 `orbit approvals list-rules`）", nargs="?")],
                    ),
                    Command(
                        name="remove",
                        help="从白名单移除一条规则",
                        handler=run_allow,
                        group="ops",
                        state="done",
                        needs_settings=True,
                        options=[Option(("rule",), "危险规则名", nargs="?")],
                    ),
                ],
            ),
            Command(
                name="list-rules",
                help="列出全部硬地板与危险模式规则",
                handler=run_list_rules,
                group="ops",
                state="done",
                needs_settings=False,
            ),
            Command(
                name="mode",
                help="查看或切换审批模式（off / smart / manual）",
                handler=run_mode,
                group="ops",
                state="done",
                # 会写 settings.json，所以要让 main 先 prepare 出数据目录。
                needs_settings=True,
                options=[Option(("value",), "新模式；不给则显示当前值", nargs="?")],
            ),
        ],
    ),
)
