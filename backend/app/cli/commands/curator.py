"""`orbit curator …` 的真实现（状态机 / 迁移 / 归档恢复 / 账本 / 遥测 / 批量归档）。

为什么用 ``register_handler`` 而不是另立一个 ``curator`` Command：声明表里
``curator`` 已有 **17 个子命令**，本轮实现 9 条。另立会把剩下 8 条
（``pause`` / ``resume`` / ``list-unmanaged`` / ``adopt`` / ``list-archived`` /
``backup`` / ``rollback`` / ``purge``）整片从 ``orbit --help`` 里挤掉——命令面缩水，
矩阵也跟着丢条目。所以照 ``_table.py`` 的约定：表不动，只声明"这个路径我接了"。

**未实现的子命令保持 ``todo``，不接管**。它们缺的都不是"写几十行"的量，而是缺通道：
``pause/resume`` 需要一个后台 tick（Orbit 的定时器在 ``app/cron.py``，接进来属于另一个波次）、
``adopt`` 需要 provenance 声明流程、``backup/rollback --id`` 需要 tar 快照库、
``purge`` 需要 archive TTL 语义（而"永不自动删除"是本层的核心承诺，草率实现会破坏它）。

三处**刻意的语义差异**（照抄会变成假对齐）：

1. ``curator run --consolidate``：Hermes 用它触发"aux 模型把重叠技能合并成伞"的 LLM 轮
   （默认 off，每次真实 run 前还会自动打 tar.gz 备份）。Orbit 本轮不接 LLM 复盘，
   所以 ``--consolidate`` **接受但明确报未实现**——假装跑了合并轮比拒绝更糟。
2. ``curator run --background``：Hermes 起线程立即返回。本实现是**确定性扫描**
   （不调模型、不花 token），耗时以毫秒计，起线程只会让"跑完了吗"变成新问题，
   所以同步跑完并说明理由。
3. ``curator usage --provenance hub``：Orbit 没有 hub 安装通道，恒为空。这是事实，不是 bug。
"""

from __future__ import annotations

from typing import Any

from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context, truncate
from app.cli.spec import REGISTRY
from app.curator import (
    ARCHIVE_DAYS,
    VALID_STATES,
    Curator,
    CuratorError,
    parse_iso,
)


def _curator(context: Context) -> Curator:
    return Curator(context.data_dir)


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _short(moment: Any, width: int = 16) -> str:
    """时间戳只显示到分钟：完整 ISO 会把表格其它列挤爆。"""
    parsed = parse_iso(moment)
    return parsed.strftime("%Y-%m-%d %H:%M") if parsed else truncate(str(moment or ""), width)


# ── curator status ──────────────────────────────────────────────────────────


def run_status(context: Context, args) -> int:
    """`curator status`：状态分布 + 阈值 + 归档区 + 账本规模。"""
    curator = _curator(context)
    report = curator.status()
    thresholds = report["thresholds"]

    context.out(f"技能总数：{report['skills']}")
    context.out()
    context.table(
        ("状态", "技能数"),
        [(state, str(report["states"].get(state, 0))) for state in VALID_STATES],
    )
    context.out()
    context.out(f"已 pin：{report['pinned']}（pin 豁免每一次自动迁移）")
    context.out(f"归档区技能：{report['archived_on_disk']}（{curator.archive_root}）")
    context.out(f"账本条目：{report['ledger_entries']}（{curator.ledger_path}）")
    context.out(f"未加载的坏文件：{report['errors']}　被同名压住：{report['shadowed']}")
    context.out()
    context.out("迁移阈值：")
    context.out(f"  stale：连续失败 ≥ {thresholds['stale_failures']} 次")
    context.out(f"  archived：闲置 > {thresholds['archive_days']} 天，"
                f"或成功率 < {thresholds['archive_min_success_rate']} "
                f"且失败样本 ≥ {thresholds['archive_min_samples']} 个")
    context.out("  锚点：max(last_used_at, last_viewed_at, last_patched_at) → 回退 created_at（不含文件 mtime）")
    context.out("  豁免：use_count == 0 的技能永不因\"未使用\"归档（从未用过 ≠ 陈旧）")
    for item in report["untrusted"]:
        context.warn(f"项目技能根未信任，未纳入统计：{item}（`orbit skills trust`）")

    context.emit({"ok": True, **report})
    return 0


# ── curator run ─────────────────────────────────────────────────────────────


def run_run(context: Context, args) -> int:
    """`curator run`：跑一轮生命周期治理。``--dry-run`` 只看不动（**默认安全**）。"""
    dry_run = bool(getattr(args, "dry_run", False))
    if getattr(args, "consolidate", False):
        raise CliError(
            "`--consolidate`（LLM 伞合并轮）本轮未实现：它需要 aux 模型与备份快照库，"
            "两者都不在 W5 波次范围内。不加该选项的 run 是确定性扫描，不花 token。"
        )
    if getattr(args, "background", False):
        context.warn(
            "`--background` 已忽略：本轮 run 是确定性扫描（不调模型、不花 token），"
            "耗时以毫秒计，起线程只会让\"跑完了吗\"变成新问题。"
        )
    curator = _curator(context)
    try:
        transitions = curator.run(dry_run=dry_run)
    except CuratorError as exc:
        raise CliError(str(exc)) from exc

    mode = "预览（dry-run，未落盘）" if dry_run else "已执行"
    context.out(f"curator run — {mode}：{len(transitions)} 条迁移")
    if transitions:
        context.out()
        context.table(
            ("技能", "从", "到", "理由"),
            [
                (item["name"], item["from"], item["to"], truncate(item["reason"], 60))
                for item in transitions
            ],
        )
    else:
        context.out()
        context.out("（没有需要迁移的技能）")
    if dry_run:
        context.note()
        context.note("要真落盘，去掉 `--dry-run` 再跑一次。")

    context.emit({"ok": True, "dry_run": dry_run, "transitions": transitions})
    return 0


# ── curator pin / unpin ─────────────────────────────────────────────────────


def run_pin(context: Context, args) -> int:
    """`curator pin`：pin 住一个技能，豁免每一次自动迁移。"""
    name = str(getattr(args, "skill", "") or "").strip()
    if not name:
        raise CliError("用法：orbit curator pin <技能名>")
    curator = _curator(context)
    try:
        entry_id = curator.pin(name)
    except CuratorError as exc:
        raise CliError(str(exc)) from exc
    if entry_id:
        context.out(f"已 pin：{name}（账本 {entry_id}）")
    else:
        context.out(f"{name} 本来就是 pin 状态（未重复记账）")
    context.emit({"ok": True, "skill": name, "pinned": True, "ledger_id": entry_id})
    return 0


def run_unpin(context: Context, args) -> int:
    """`curator unpin`：解除 pin。"""
    name = str(getattr(args, "skill", "") or "").strip()
    if not name:
        raise CliError("用法：orbit curator unpin <技能名>")
    curator = _curator(context)
    try:
        entry_id = curator.unpin(name)
    except CuratorError as exc:
        raise CliError(str(exc)) from exc
    if entry_id:
        context.out(f"已 unpin：{name}（账本 {entry_id}）")
    else:
        context.out(f"{name} 本来就没 pin（未重复记账）")
    context.emit({"ok": True, "skill": name, "pinned": False, "ledger_id": entry_id})
    return 0


# ── curator archive / restore ───────────────────────────────────────────────


def run_archive(context: Context, args) -> int:
    """`curator archive`：手动归档（搬到归档区，可恢复，永不自动删除）。"""
    name = str(getattr(args, "skill", "") or "").strip()
    if not name:
        raise CliError("用法：orbit curator archive <技能名>")
    curator = _curator(context)
    try:
        entry_id = curator.archive(name)
    except CuratorError as exc:
        raise CliError(str(exc)) from exc
    context.out(f"已归档：{name} → {curator.archive_root / name}")
    context.out(f"账本：{entry_id}（可 `orbit curator restore {name}` 恢复）")
    context.emit({"ok": True, "skill": name, "archived": True, "ledger_id": entry_id})
    return 0


def run_restore(context: Context, args) -> int:
    """`curator restore`：从归档区恢复（搬回用户级根，扁平布局）。"""
    name = str(getattr(args, "skill", "") or "").strip()
    if not name:
        raise CliError("用法：orbit curator restore <技能名>")
    curator = _curator(context)
    try:
        entry_id = curator.restore(name)
    except CuratorError as exc:
        raise CliError(str(exc)) from exc
    context.out(f"已恢复：{name} → {curator.index.user_root / name}")
    context.out(f"账本：{entry_id}")
    context.emit({"ok": True, "skill": name, "restored": True, "ledger_id": entry_id})
    return 0


# ── curator ledger ──────────────────────────────────────────────────────────


def run_ledger(context: Context, args) -> int:
    """`curator ledger`：逐变更审计账本（全部 actor：curator / agent / user）。"""
    skill = str(getattr(args, "skill", "") or "").strip() or None
    limit = max(1, _int(getattr(args, "limit", None), 20))
    curator = _curator(context)
    entries = curator.ledger_entries(skill=skill, limit=limit)

    context.out(f"账本 {len(entries)} 条（最新在前，上限 {limit}）　文件：{curator.ledger_path}")
    context.out()
    context.table(
        ("id", "时间", "actor", "动作", "技能", "证据"),
        [
            (
                str(entry.get("id") or ""),
                _short(entry.get("ts")),
                str(entry.get("actor") or ""),
                str(entry.get("action") or ""),
                truncate(str(entry.get("skill") or ""), 28),
                truncate(str((entry.get("evidence") or {}).get("reason") or ""), 40),
            )
            for entry in entries
        ],
    )
    context.emit({"ok": True, "count": len(entries), "entries": entries})
    return 0


# ── curator usage ───────────────────────────────────────────────────────────


def run_usage(context: Context, args) -> int:
    """`curator usage`：全技能使用遥测（含 provenance 与生命周期状态）。"""
    sort = str(getattr(args, "sort", "") or "activity")
    provenance = str(getattr(args, "provenance", "") or "").strip() or None
    curator = _curator(context)
    rows = curator.usage_report(sort=sort, provenance=provenance)
    if provenance == "hub":
        context.warn(
            "`--provenance hub` 在 Orbit 恒为空：本库没有远端 hub 安装通道。"
            "这是事实而非缺陷，所以不伪造条目。"
        )

    context.out(f"技能 {len(rows)} 条（排序：{sort}）")
    context.out()
    context.table(
        ("技能", "tier", "归属", "状态", "用", "看", "改", "成功", "失败", "闲置天数", "最近活动"),
        [
            (
                row["name"],
                row["tier"],
                row["provenance"] or "（无标记）",
                row["state"],
                str(row["use_count"]),
                str(row["view_count"]),
                str(row["patch_count"]),
                str(row["successes"]),
                str(row["failures"]),
                "—" if row["idle_days"] is None else f"{row['idle_days']:.0f}",
                _short(row["last_active_at"]) or "—",
            )
            for row in rows
        ],
    )
    context.emit({"ok": True, "count": len(rows), "skills": rows, "sort": sort})
    return 0


# ── curator prune ───────────────────────────────────────────────────────────


def run_prune(context: Context, args) -> int:
    """`curator prune`：批量归档闲置 ≥ N 天的技能（``--days`` 默认 90）。

    **必须显式 ``-y`` 才真的搬目录**（``--dry-run`` 除外）：这条命令会一次搬走多个
    技能目录，虽然可恢复（归档不删除），但"批量搬走"仍然是要人点头的动作。
    """
    days = _int(getattr(args, "days", None), ARCHIVE_DAYS)
    dry_run = bool(getattr(args, "dry_run", False))
    if days <= 0:
        raise CliError("`--days` 必须是正整数（默认 90）")
    if not dry_run and not getattr(args, "yes", False):
        raise CliError(
            f"拒绝执行：这会批量归档闲置 ≥ {days} 天的技能（搬到归档区，可恢复）。"
            "确认请加 -y（或先用 --dry-run 看清单）"
        )

    curator = _curator(context)
    transitions = curator.prune(days=days, dry_run=dry_run)
    mode = "预览（dry-run，未搬动）" if dry_run else "已执行"
    context.out(f"curator prune — {mode}：{len(transitions)} 个技能（阈值 {days} 天）")
    if transitions:
        context.out()
        context.table(
            ("技能", "从", "到", "理由"),
            [
                (item["name"], item["from"], item["to"], truncate(item["reason"], 60))
                for item in transitions
            ],
        )
    else:
        context.out()
        context.out("（没有达到阈值的技能；use_count == 0 的技能不计入——从未用过 ≠ 陈旧）")

    context.emit(
        {"ok": True, "dry_run": dry_run, "days": days, "count": len(transitions),
         "transitions": transitions}
    )
    return 0


# ── 接线 ────────────────────────────────────────────────────────────────────


def _bind(path: str, handler: Any) -> None:
    """把实现登记到声明表的某条命令路径上。

    两种导入顺序都必须成立：生产接线里本模块排在 ``hermes_parity`` **之前**，
    ``register_handler`` 生效；而 ``import app.cli.commands.curator`` 会先跑包初始化
    ``__init__.py``，此时声明表可能已组装完毕，``HANDLERS`` 里的登记不会回填到
    已构建的 ``Command`` 上（它会停在"尚未实现"的占位 handler）。所以补一次回填。
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


_bind("curator status", run_status)
_bind("curator run", run_run)
_bind("curator pin", run_pin)
_bind("curator unpin", run_unpin)
_bind("curator archive", run_archive)
_bind("curator restore", run_restore)
_bind("curator ledger", run_ledger)
_bind("curator usage", run_usage)
_bind("curator prune", run_prune)

#: 显式列出"本模块没接的路径"，让"未实现"这件事可被 grep 到（而不是靠记忆）。
UNBOUND_SUBCOMMANDS: tuple[str, ...] = (
    "curator pause",
    "curator resume",
    "curator list-unmanaged",
    "curator adopt",
    "curator list-archived",
    "curator backup",
    "curator rollback",
    "curator purge",
)
