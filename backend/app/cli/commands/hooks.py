"""`orbit hooks` —— shell 脚本 hook 的命令面（MATRIX 14.10）。

命令面严格对齐 Hermes 的 4 个子命令（`list` / `test` / `revoke` / `doctor`）。
**管理类动作（add / remove / trust / enable）另放在 `orbit shellhooks` 下**，
不往 `hooks` 里塞：Hermes 的 `hooks` 是"查看与排障"，往里加写操作会让
`hooks --help` 与基准对不上，而命令面对齐是这个项目的验收口径之一。
"""

from __future__ import annotations

import json

from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option
from app.hooks import HOOK_EVENTS
from app.shell_hooks import MAX_TIMEOUT, ShellHookError, ShellHookStore, doctor, run_event


def _store(context: Context) -> ShellHookStore:
    return ShellHookStore(context.data_dir)


def _payload(args) -> dict:
    """`--payload-file` 的 JSON 合并进合成载荷。坏文件要报错，不能静默当空。"""
    raw = getattr(args, "payload_file", None)
    if not raw:
        return {}
    from pathlib import Path

    path = Path(raw).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CliError(f"读不到载荷文件：{exc}") from None
    except ValueError as exc:
        raise CliError(f"载荷文件不是合法 JSON：{exc}") from None
    if not isinstance(payload, dict):
        raise CliError("载荷文件必须是 JSON 对象（顶层是 dict）")
    return payload


# ── hooks list ─────────────────────────────────────────────────────────────


def run_list(context: Context, args) -> int:
    hooks = _store(context).list()
    if not hooks:
        context.out("还没有声明任何 shell hook。")
        context.out("加一条：`orbit shellhooks add <事件> '<命令>'`")
        context.emit({"ok": True, "hooks": []})
        return 0
    rows = [
        [
            hook.id,
            hook.event,
            hook.command[:60],
            f"{hook.timeout}s",
            "已授权" if hook.trusted else "待授权",
            "是" if hook.enabled else "否",
        ]
        for hook in hooks
    ]
    context.table(["id", "事件", "命令", "超时", "状态", "启用"], rows)
    pending = [hook.id for hook in hooks if not hook.trusted]
    if pending:
        context.note(
            f"有 {len(pending)} 条未授权，**不会执行**：`orbit shellhooks trust <id>` 后才会跑。"
        )
    context.emit({"ok": True, "hooks": [hook.as_dict() for hook in hooks]})
    return 0


# ── hooks test ─────────────────────────────────────────────────────────────


def run_test(context: Context, args) -> int:
    event = str(getattr(args, "event", "") or "").strip()
    if event not in HOOK_EVENTS:
        raise CliError(
            f"未知事件：{event!r}。可用事件：{'、'.join(HOOK_EVENTS)}"
        )
    payload = _payload(args)
    for_tool = str(getattr(args, "for_tool", "") or "").strip()
    if for_tool:
        payload.setdefault("tool", for_tool)

    outcomes = run_event(_store(context), event, payload)
    if not outcomes:
        context.warn(f"事件 {event} 下没有 hook（共 {len(HOOK_EVENTS)} 个事件可用）。")
        context.emit({"ok": True, "event": event, "outcomes": []})
        return 0

    for outcome in outcomes:
        state = "执行成功" if outcome.ok else ("未执行" if not outcome.ran else "失败")
        context.out(f"[{outcome.hook_id}] {state}（{outcome.duration_ms}ms）")
        if outcome.reason:
            context.out(f"  原因：{outcome.reason}")
        if outcome.stdout.strip():
            context.out("  stdout：" + outcome.stdout.strip().splitlines()[0][:200])
        if outcome.stderr.strip():
            context.out("  stderr：" + outcome.stderr.strip().splitlines()[0][:200])
    context.emit({"ok": True, "event": event, "outcomes": [item.as_dict() for item in outcomes]})
    # 有 hook 跑了但失败 → 退出码 1；全部未执行（未授权/停用）不算失败。
    return 0 if all(item.ok or not item.ran for item in outcomes) else 1


# ── hooks revoke ───────────────────────────────────────────────────────────


def run_revoke(context: Context, args) -> int:
    command = str(getattr(args, "command", "") or "")
    changed = _store(context).revoke_by_command(command)
    if changed == 0:
        # 找不到不算错误，但要如实说清"没找到"而不是"已撤销"。
        context.warn(f"没有找到这条已授权的 hook：{command!r}")
        context.emit({"ok": False, "revoked": 0})
        return 0
    context.out(f"已撤销 {changed} 条 hook 的授权（声明保留，可用 `shellhooks trust` 重新授权）。")
    context.emit({"ok": True, "revoked": changed})
    return 0


# ── hooks doctor ───────────────────────────────────────────────────────────


def run_doctor(context: Context, args) -> int:
    reports = doctor(_store(context))
    if not reports:
        context.out("没有 hook 需要体检。")
        context.emit({"ok": True, "reports": []})
        return 0
    rows = [
        [
            report.hook_id,
            report.event,
            "通过" if report.ok else "有问题",
            "；".join([*report.problems, *report.warnings])[:80] or "-",
        ]
        for report in reports
    ]
    context.table(["id", "事件", "结论", "详情"], rows)
    problems = sum(1 for report in reports if not report.ok)
    context.emit(
        {"ok": problems == 0, "problems": problems, "reports": [report.__dict__ for report in reports]}
    )
    if problems:
        context.warn(f"共 {problems} 条 hook 有硬问题（这些不会执行）。")
        return 1
    return 0


# ── Orbit 自有：shellhooks 管理面 ──────────────────────────────────────────


def run_add(context: Context, args) -> int:
    store = _store(context)
    try:
        hook = store.add(
            str(getattr(args, "event", "") or ""),
            str(getattr(args, "command", "") or ""),
            timeout=int(getattr(args, "timeout", None) or 10),
            note=str(getattr(args, "note", "") or ""),
        )
    except ShellHookError as exc:
        raise CliError(str(exc)) from None
    context.out(f"已添加 hook {hook.id}（{hook.event}）。")
    context.out(f"**未授权，不会执行**：`orbit shellhooks trust {hook.id}` 后才生效。")
    context.emit({"ok": True, "hook": hook.as_dict()})
    return 0


def run_remove(context: Context, args) -> int:
    hook_id = str(getattr(args, "hook_id", "") or "")
    if not _store(context).remove(hook_id):
        raise CliError(f"没有这个 hook：{hook_id}")
    context.out(f"已删除 hook {hook_id}。")
    context.emit({"ok": True})
    return 0


def run_trust(context: Context, args) -> int:
    hook_id = str(getattr(args, "hook_id", "") or "")
    if not _store(context).trust(hook_id):
        raise CliError(f"没有这个 hook：{hook_id}")
    context.out(f"已授权 {hook_id}。它现在会在事件发生时执行——请确认你读过它的命令。")
    context.emit({"ok": True})
    return 0


def run_toggle(context: Context, args, *, enabled: bool) -> int:
    hook_id = str(getattr(args, "hook_id", "") or "")
    if not _store(context).set_enabled(hook_id, enabled):
        raise CliError(f"没有这个 hook：{hook_id}")
    context.out(f"已{'启用' if enabled else '停用'} {hook_id}。")
    context.emit({"ok": True})
    return 0


def run_enable(context: Context, args) -> int:
    return run_toggle(context, args, enabled=True)


def run_disable(context: Context, args) -> int:
    return run_toggle(context, args, enabled=False)


def run_events(context: Context, args) -> int:
    """列出全部事件名。为什么单独一条命令：`hooks add` 要事件名，而没人记得住 17 个。"""
    for name in HOOK_EVENTS:
        context.out(f"  {name}")
    context.emit({"ok": True, "events": list(HOOK_EVENTS)})
    return 0


# ── 接线 ───────────────────────────────────────────────────────────────────

#: Hermes 的 4 个子命令，逐条接管。
register_handler("hooks list", run_list)
register_handler("hooks test", run_test)
register_handler("hooks revoke", run_revoke)
register_handler("hooks doctor", run_doctor)

_HOOK_ID = Option(("hook_id",), "hook 的 id（见 `orbit hooks list`）", metavar="ID")

REGISTRY.add(
    Command(
        name="shellhooks",
        help="声明 shell 脚本 hook（进程外，按生命周期事件触发）",
        group="ops",
        state="done",
        hermes="",
        parity="P2",
        subcommands=[
            Command(
                name="add",
                help="添加一条 hook（**添加后未授权，不会执行**）",
                handler=run_add,
                state="done",
                needs_settings=True,
                options=[
                    Option(("event",), "事件名（见 `orbit shellhooks events`）", metavar="EVENT"),
                    Option(("command",), "要执行的 shell 命令", metavar="CMD"),
                    Option(("--timeout",), f"超时秒数（上限 {MAX_TIMEOUT}）", metavar="SEC"),
                    Option(("--note",), "备注，给自己看的", metavar="TEXT"),
                ],
            ),
            Command(
                name="remove",
                help="删除一条 hook",
                handler=run_remove,
                state="done",
                needs_settings=True,
                options=[_HOOK_ID],
            ),
            Command(
                name="trust",
                help="授权一条 hook 开始执行（这是唯一入口）",
                handler=run_trust,
                state="done",
                needs_settings=True,
                options=[_HOOK_ID],
            ),
            Command(
                name="enable",
                help="启用（不改变授权状态）",
                handler=run_enable,
                state="done",
                needs_settings=True,
                options=[_HOOK_ID],
            ),
            Command(
                name="disable",
                help="停用（保留声明与授权）",
                handler=run_disable,
                state="done",
                needs_settings=True,
                options=[_HOOK_ID],
            ),
            Command(
                name="events",
                help="列出全部可用事件名",
                handler=run_events,
                state="done",
            ),
        ],
    ),
)
