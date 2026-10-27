"""用量洞察（`orbit insights`）与网关监控（`orbit monitoring status`）。

这两条在 Hermes 里属于"可观测性层"，且都有一条共同的设计约束：**只读、不新增
采集面**。Hermes 的 monitoring 原话是"content-free by construction"——不导出
prompt / 消息 / 工具参数与结果。Orbit 照这条办：所有数字都从已有表算出来，
不额外埋点，也不把用户内容写进任何导出物。

`insights` 的 `--source` 是个**刻意的语义分歧**，必须说清：Hermes 用它是区分
平台来源（cli / telegram / discord…），而 Orbit 是单用户本地部署，没有平台维度。
如果照抄一个永远为空的过滤参数，那才是假对齐——所以这里把它落到 Orbit 真实存在的
维度上：**任务主要操作过哪个站点**（`tool_results.host`）。"这周在 B 站花了多少"
是这个 agent 真会被问到的问题。
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app import approvals
from app.cli.context import Context
from app.cli.spec import REGISTRY, Command, Option

#: 终态任务状态。只有它们才计入"成功率"——PENDING/RUNNING 不是失败。
TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED")


def _cutoff(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def _human_size(size: int) -> str:
    units = ["B", "KB", "MB", "GB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.1f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{size}B"


def _duration_seconds(start: str, end: str) -> float:
    try:
        return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except (TypeError, ValueError):
        return 0.0


# ── insights ────────────────────────────────────────────────────────────────


def run_insights(context: Context, args) -> int:
    from app.storage import Conversation, Operation, Task, TaskEvent, ToolResult

    days = int(getattr(args, "days", 30) or 30)
    source = str(getattr(args, "source", "") or "").strip().lower()
    cutoff = _cutoff(days) if days > 0 else ""

    tasks = context.run(context.store.rows(select(Task)))
    if cutoff:
        tasks = [task for task in tasks if (task.created_at or "") >= cutoff]
    task_ids = {task.id for task in tasks}

    results = [
        row
        for row in context.run(context.store.rows(select(ToolResult)))
        if row.task_id in task_ids
    ]
    operations = [
        row
        for row in context.run(context.store.rows(select(Operation)))
        if row.task_id in task_ids
    ]
    events = [
        row
        for row in context.run(context.store.rows(select(TaskEvent)))
        if row.task_id in task_ids and row.event_type in {"task_completed", "task_failed", "waiting_user"}
    ]
    conversations = {
        row.id: row for row in context.run(context.store.rows(select(Conversation)))
    }

    # `--source`：按"这个任务主要操作了哪个站点"过滤（见模块 docstring 的语义说明）。
    host_by_task: dict[str, str] = {}
    for row in results:
        if row.host and row.task_id not in host_by_task:
            host_by_task[row.task_id] = row.host
    if source:
        tasks = [
            task
            for task in tasks
            if source in (host_by_task.get(task.id, "") or "").lower()
        ]
        task_ids = {task.id for task in tasks}
        results = [row for row in results if row.task_id in task_ids]
        operations = [row for row in operations if row.task_id in task_ids]
        events = [row for row in events if row.task_id in task_ids]

    status_counts = Counter(task.status for task in tasks)
    finished = sum(status_counts[state] for state in TERMINAL)
    succeeded = status_counts["SUCCEEDED"]
    success_rate = (succeeded / finished * 100) if finished else 0.0

    # token 三路分开记：工具读来的 / 模型自报的 / 摘要另起一次调用花的。
    # 合并成一个数会失去归因——"这钱是读网页读出来的还是摘要摘要出来的"必须能分清。
    tool_tokens = sum(int(row.tokens or 0) for row in results)
    tool_bytes = sum(int(row.bytes or 0) for row in results)
    spilled = sum(1 for row in results if row.truncated)
    model_tokens = 0
    for event in events:
        usage = (event.payload or {}).get("token_usage") or {}
        model_tokens += int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0)
    summary_tokens = sum(
        int(conversations[task.conversation_id].summary_tokens or 0)
        for task in tasks
        if task.conversation_id in conversations
    )

    tool_counts = Counter(row.name for row in operations)
    # 失败口径取自 `tool_results.status`（取值 ok / error），**不是** `tool_calls.status`
    # （取值 DONE / RUNNING，只记控制流，不记成败）。第一版取错了，把 97 次调用全算成失败
    # ——这种错会让"失败率 100%"看起来像真的，所以注释留在这里。
    tool_errors = Counter(row.name for row in results if (row.status or "") != "ok")
    host_counts = Counter(row.host for row in results if row.host)

    durations = [
        _duration_seconds(task.created_at, task.updated_at)
        for task in tasks
        if task.status in TERMINAL
    ]
    avg_duration = sum(durations) / len(durations) if durations else 0.0

    per_day = Counter((task.created_at or "")[:10] for task in tasks if task.created_at)

    context.out(f"用量洞察 · 最近 {days or '全部'} 天" + (f" · 来源含 {source}" if source else ""))
    context.out()
    context.table(
        ("指标", "值"),
        [
            ("任务总数", str(len(tasks))),
            ("已终结", f"{finished}（成功 {succeeded} / 失败 {status_counts['FAILED']} / 取消 {status_counts['CANCELLED']}）"),
            ("成功率", f"{success_rate:.1f}%"),
            # 「含中断等待」必须写出来：这个数是 updated_at - created_at 的墙钟差，
            # 任务中途等用户回答、跨天恢复都算在内。不标注的话，几千秒的均值会被
            # 误读成"agent 很慢"。
            ("平均耗时（含中断等待）", f"{avg_duration:.1f}s"),
            ("工具调用", f"{len(operations)} 次（失败 {sum(tool_errors.values())}）"),
            ("工具结果 token", str(tool_tokens)),
            ("工具结果字节", _human_size(tool_bytes)),
            ("其中被截断", str(spilled)),
            ("模型自报 token", str(model_tokens)),
            ("摘要 token", str(summary_tokens)),
        ],
    )

    if tool_counts:
        context.out()
        context.out("工具使用排行：")
        context.table(
            ("工具", "次数", "失败"),
            [(name, str(count), str(tool_errors.get(name, 0))) for name, count in tool_counts.most_common(10)],
        )
    if host_counts:
        context.out()
        context.out("站点排行：")
        context.table(("站点", "结果条数"), [(host, str(count)) for host, count in host_counts.most_common(10)])
    if per_day:
        context.out()
        context.out("每日任务数：")
        context.table(("日期", "任务数"), [(day, str(count)) for day, count in sorted(per_day.items())])

    context.emit(
        {
            "ok": True,
            "days": days,
            "source": source,
            "tasks": len(tasks),
            "status_counts": dict(status_counts),
            "success_rate": round(success_rate, 2),
            "avg_duration_seconds": round(avg_duration, 2),
            "tool_calls": len(operations),
            "tool_errors": sum(tool_errors.values()),
            "tool_tokens": tool_tokens,
            "tool_bytes": tool_bytes,
            "truncated_results": spilled,
            "model_tokens": model_tokens,
            "summary_tokens": summary_tokens,
            "tools": [{"name": n, "count": c, "errors": tool_errors.get(n, 0)} for n, c in tool_counts.most_common()],
            "hosts": [{"host": h, "count": c} for h, c in host_counts.most_common()],
            "per_day": dict(sorted(per_day.items())),
        }
    )
    return 0


# ── monitoring ──────────────────────────────────────────────────────────────


def run_monitoring_status(context: Context, args) -> int:
    """监控设置 + 导出状态 + 脱敏姿态 + 真实健康指标。

    **不导出任何内容**（prompt / 消息 / 工具参数与结果）——这是 Hermes
    "content-free by construction" 那条约束，也是本命令只读现有表的原因。
    """
    db_path = context.db_path
    wal = Path(str(db_path) + "-wal")

    import sqlite3

    health: list[tuple[str, str]] = []
    if db_path.exists():
        health.append(("主库大小", _human_size(db_path.stat().st_size)))
        if wal.exists():
            health.append(("WAL 大小", _human_size(wal.stat().st_size)))
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
            try:
                integrity = connection.execute("pragma quick_check").fetchone()
                health.append(("完整性", str(integrity[0]) if integrity else "未知"))
                page_count = connection.execute("pragma page_count").fetchone()
                page_size = connection.execute("pragma page_size").fetchone()
                if page_count and page_size:
                    health.append(
                        ("页数 × 页大小", f"{page_count[0]} × {page_size[0]}")
                    )
            finally:
                connection.close()
        except sqlite3.Error as exc:
            health.append(("完整性", f"检查失败：{exc}"))
    else:
        health.append(("主库大小", "数据库不存在"))

    from app.storage import Task

    tasks = context.run(context.store.rows(select(Task)))
    stuck = [task for task in tasks if task.status in {"PENDING", "RUNNING"}]
    health.append(("未终结任务", f"{len(stuck)}（PENDING/RUNNING）"))
    health.append(("会话检查点库", _human_size(context.checkpoint_path.stat().st_size) if context.checkpoint_path.exists() else "不存在"))

    context.out("监控状态")
    context.out()
    context.table(("项", "值"), health)
    context.out()
    context.out("导出：")
    context.table(
        ("项", "值"),
        [
            ("导出端点", "未配置（Orbit 没有 OTLP 导出）"),
            ("导出内容", "无——本命令不采集 prompt / 消息 / 工具参数与结果"),
            ("采集面", "只读现有表（tasks / tool_calls / tool_results / task_events）"),
        ],
    )
    context.out()
    context.out("脱敏姿态：")
    context.table(
        ("项", "值"),
        [
            ("模型密钥", "只存系统 keyring，不落配置文件、不进日志"),
            ("工具输出", "落库前经 tool_results 记录；密钥脱敏未实现（见 MATRIX 14.3）"),
            ("命令裁决", f"审批模式 {approvals.resolve_mode(context.settings)}"),
        ],
    )
    context.emit(
        {
            "ok": True,
            "health": {name: value for name, value in health},
            "export": {"endpoint": "", "configured": False, "content_free": True},
            "stuck_tasks": len(stuck),
        }
    )
    return 0


REGISTRY.add(
    Command(
        name="insights",
        help="分析会话历史：token 用量、工具使用模式、站点分布、活动趋势",
        handler=run_insights,
        group="ops",
        state="done",
        needs_settings=False,
        needs_store=True,
        options=[
            Option(("--days",), "分析最近多少天（0 = 全部）", default=30, metavar="DAYS"),
            Option(
                ("--source",),
                "按任务主要操作的站点过滤（Orbit 是单用户本地部署，没有 Hermes 的平台维度）",
                metavar="SOURCE",
            ),
        ],
    ),
    Command(
        name="monitoring",
        help="监控状态：健康指标 + 导出配置 + 脱敏姿态（不采集任何内容）",
        group="ops",
        state="done",
        needs_settings=False,
        needs_store=True,
        subcommands=[
            Command(
                name="status",
                help="显示监控设置、导出状态与脱敏姿态",
                handler=run_monitoring_status,
                group="ops",
                state="done",
                needs_settings=False,
                needs_store=True,
            ),
        ],
    ),
)
