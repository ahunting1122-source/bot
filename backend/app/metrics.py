"""指标 schema（Prometheus 文本格式）——只算**现有数据里真有的**那部分。

为什么要有这个模块：Hermes 的 `serve` 暴露一份固定的指标清单；Orbit 之前只有
`insights`（人读的聚合）和 `monitoring status`（健康快照），两者都不能被监控系统
采集。这里把"能从 `app.sqlite` / 数据目录真算出来的数"落成一份**固定 schema**，
并把算不出来的如实标成"无数据源"。

三条设计约束（都是真踩过的坑）：

1. **0 和"没有数据源"是两件事。** 空库上 `orbit_tasks_total` 真的是 0（表在、
   行数为 0）；而 `orbit_checkpoint_bytes` 在 `checkpoints.sqlite` 不存在时**不是 0**，
   是"这个指标无从谈起"。填 0 会让告警系统以为"检查点体积正常"，实际是"根本没测到"。
   所以缺数据源一律 ``available=False`` + 原因，渲染时**不输出样本行**。
2. **只读、不埋点。** 全部数字从已有表 / 已有落盘文件算出来（`tool_results`、
   `task_events`、`cron/runs.jsonl`、`gateway/deliveries.jsonl`），不新增采集面，
   也不导出任何 prompt / 消息 / 工具参数与结果（照 `monitoring` 的 content-free 约束）。
   读库走**独立的只读连接**，不经 SQLAlchemy 的异步引擎——指标采集不该和业务写入
   抢同一个连接池，也不该因为业务侧一个未提交事务就被拖住。
3. **三向 token 归因。** 工具读来的 / 模型自报的 / 摘要花的必须分开（见
   `observability.py` 的同名注释）；缓存命中（cached）只有在模型自报
   `token_usage.cached_tokens` 时才有数，没有就标"该系列无数据源"，**不并进 input**
   ——把 cached 算成 0 会让"缓存一点没命中"看起来像结论。

Prometheus 文本格式（`render_prometheus` 的输出，逐条对齐官方 exposition format）::

    # HELP <name> <单行说明>
    # TYPE <name> counter|gauge|histogram
    <name>{<label>="<值>"} <数值>          ← counter/gauge，有 label 时分组多行
    <name> <数值>                          ← 无 label 时单行
    <name>_bucket{le="<上界>"} <累计数>     ← histogram，上界升序，最后一行 le="+Inf"
    <name>_count <样本数>
    <name>_sum <样本值之和>

`#` 开头的其它行是注释，Prometheus 解析器直接忽略——所以"无数据源"的原因就写在
注释里：人可读、可 grep，且**不会**被当成一个 0 值样本。标签值里的 ``\\``、``"``、
换行按规范转义。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.config import Settings

#: 指标类型。只这三种：Prometheus 的 summary 需要分位数，本机没有可算的来源。
KINDS: tuple[str, ...] = ("counter", "gauge", "histogram")

#: 终态任务状态。只有终态才计入耗时直方图——PENDING/RUNNING 的 updated_at 还在动。
TERMINAL_STATUSES: tuple[str, ...] = ("SUCCEEDED", "FAILED", "CANCELLED")

#: 耗时直方图的桶上界（秒）。本机实测：一次带浏览器的任务多在 30–300s，
#: 1s 以下基本只有"模型直接拒答"，900s 以上是预算兜底（`max_seconds`）。
DURATION_BUCKETS: tuple[float, ...] = (1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 900.0)


@dataclass(frozen=True)
class Metric:
    """一个指标的元数据。字段少是刻意的：只放能写进 `# HELP` / `# TYPE` 的东西。"""

    name: str
    kind: str
    unit: str
    help: str
    #: 数据来自哪张表 / 哪个文件。写清楚是为了让"无数据源"的结论可核对。
    source: str

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"{self.name}: 未知指标类型 {self.kind!r}")
        if not self.name or not (self.name[0].isalpha() or self.name[0] == "_"):
            raise ValueError(f"指标名非法：{self.name!r}（须以字母或下划线开头）")
        if not self.help.strip() or "\n" in self.help:
            # `# HELP` 只能一行：换行会把后半句变成一条无意义的注释。
            raise ValueError(f"{self.name}: help 必须是非空单行")


#: 固定指标清单。顺序即渲染顺序，也是 `orbit monitoring metrics` 的展示顺序。
CATALOG: tuple[Metric, ...] = (
    Metric(
        "orbit_tasks_total",
        "counter",
        "tasks",
        "任务总数，按终态/运行态分组",
        "app.sqlite · tasks.status",
    ),
    Metric(
        "orbit_task_duration_seconds",
        "histogram",
        "seconds",
        "任务墙钟耗时（含等待用户与后台续跑，见 insights 的口径说明）",
        "app.sqlite · tasks.created_at → tasks.updated_at（仅终态）",
    ),
    Metric(
        "orbit_task_events_total",
        "counter",
        "events",
        "任务事件数，按事件类型分组",
        "app.sqlite · task_events.event_type",
    ),
    Metric(
        "orbit_messages_total",
        "counter",
        "messages",
        "会话消息数，按 role 分组",
        "app.sqlite · messages.role",
    ),
    Metric(
        "orbit_tool_calls_total",
        "counter",
        "calls",
        "工具调用次数，按工具名分组",
        "app.sqlite · tool_calls.name",
    ),
    Metric(
        "orbit_tool_errors_total",
        "counter",
        "calls",
        "工具失败次数，按工具名分组（口径是 tool_results.status ≠ ok）",
        "app.sqlite · tool_results.status",
    ),
    Metric(
        "orbit_tool_result_bytes",
        "gauge",
        "bytes",
        "工具结果落库总字节（模型实际看到的体积）",
        "app.sqlite · tool_results.bytes",
    ),
    Metric(
        "orbit_llm_tokens_total",
        "counter",
        "tokens",
        "模型 token 数，按三向归因分组：input / output / cached",
        "app.sqlite · task_events.payload.token_usage",
    ),
    Metric(
        "orbit_memory_injections_total",
        "counter",
        "injections",
        "长期记忆被注入上下文的累计次数",
        "app.sqlite · memories.hit_count",
    ),
    Metric(
        "orbit_skill_injections_total",
        "counter",
        "injections",
        "技能被注入/使用的累计次数",
        "app.sqlite · skills.usage_count",
    ),
    Metric(
        "orbit_vector_chunks",
        "gauge",
        "chunks",
        "可检索片段总数（RAG 索引规模）",
        "app.sqlite · chunks",
    ),
    Metric(
        "orbit_db_size_bytes",
        "gauge",
        "bytes",
        "主库文件大小",
        "<data_dir>/app.sqlite",
    ),
    Metric(
        "orbit_checkpoint_bytes",
        "gauge",
        "bytes",
        "LangGraph 检查点库大小",
        "<data_dir>/checkpoints.sqlite",
    ),
    Metric(
        "orbit_cron_runs_total",
        "counter",
        "runs",
        "定时任务执行次数，按结果分组（ok / error）",
        "<data_dir>/cron/runs.jsonl",
    ),
    Metric(
        "orbit_gateway_deliveries_total",
        "counter",
        "deliveries",
        "网关投递尝试次数，按结果分组（至少一次语义，重试各记一行）",
        "<data_dir>/gateway/deliveries.jsonl",
    ),
    Metric(
        "orbit_uptime_seconds",
        "gauge",
        "seconds",
        "进程运行时长",
        "无数据源（Orbit 不登记常驻进程启动时间）",
    ),
)

#: 名字 → 元数据。渲染与查表都靠它，避免各处再拼字符串。
CATALOG_BY_NAME: dict[str, Metric] = {metric.name: metric for metric in CATALOG}


@dataclass(frozen=True)
class MetricValue:
    """一次采集的结果。**可用性是一等公民**，不是一个 magic number。

    ``samples`` 的键是 label 值（空串 = 无 label）。``missing`` 记"这个指标的某个
    系列没有数据源"——三向 token 里的 cached 就是这种情况：整个指标有数，但
    cached 这一向没有，混在 ``samples`` 里填 0 会变成一句假话。
    """

    metric: Metric
    available: bool = True
    reason: str = ""
    label: str = ""
    samples: dict[str, float] = field(default_factory=dict)
    missing: dict[str, str] = field(default_factory=dict)
    buckets: tuple[tuple[float, float], ...] = ()
    count: float = 0.0
    total: float = 0.0

    @property
    def name(self) -> str:
        return self.metric.name

    @property
    def kind(self) -> str:
        return self.metric.kind

    @property
    def unit(self) -> str:
        return self.metric.unit

    @property
    def help(self) -> str:
        return self.metric.help

    @property
    def source(self) -> str:
        return self.metric.source

    @property
    def value(self) -> float:
        """无 label 时的数值；有 label 时返回各样本之和（只为给人看，不是语义）。"""
        return sum(self.samples.values())


# ── 数据源定位 ──────────────────────────────────────────────────────────────


def database_path(store: Any, settings: Settings) -> Path:
    """定位主库文件。

    优先取 ``store.engine.url.database``：CLI 与测试都可能把 Store 指到别处
    （`--data-dir`、tmp_path），此时"按 settings.data_dir 猜路径"会读错库。
    拿不到才退回 ``<data_dir>/app.sqlite``。
    """
    url = getattr(getattr(store, "engine", None), "url", None)
    database = getattr(url, "database", None)
    if isinstance(database, str) and database and database != ":memory:":
        return Path(database)
    return Path(settings.data_dir) / "app.sqlite"


def _open_readonly(path: Path) -> tuple[sqlite3.Connection | None, str]:
    """开一个只读连接。返回 ``(连接, 失败原因)``。

    先试 ``mode=ro``：这是承诺"我只读"的唯一可靠方式。WAL 库在部分环境里
    只读连接打不开（需要写 ``-shm``），所以失败后退回普通连接——**本模块只发
    SELECT**，退回的那条连接也不会写一个字节。
    """
    if not path.exists():
        return None, f"{path} 不存在（先运行 `orbit setup` 或启动一次后端）"
    try:
        return sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5), ""
    except sqlite3.Error:
        pass
    try:
        return sqlite3.connect(str(path), timeout=5), ""
    except sqlite3.Error as exc:
        return None, f"打不开 {path}：{exc}"


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], str]:
    """读 JSONL 落盘账本。坏行跳过（一行坏了不该让整份历史读不出来）。"""
    if not path.exists():
        return [], f"{path} 不存在（这类事件从未发生过）"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], f"读不了 {path}：{exc}"
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records, ""


def _make_value(
    name: str,
    series: dict[str, float] | None,
    reason: str = "",
    *,
    label: str = "",
    missing: dict[str, str] | None = None,
) -> MetricValue:
    metric = CATALOG_BY_NAME[name]
    if series is None:
        return MetricValue(metric=metric, available=False, reason=reason, label=label)
    # 表在、但一行都没有 → 真的是 0。渲染成一条无 label 的 0 样本，而不是不输出。
    return MetricValue(
        metric=metric,
        available=True,
        label=label,
        samples=series or {"": 0.0},
        missing=dict(missing or {}),
    )


def _query_series(
    connection: sqlite3.Connection | None,
    db_reason: str,
    sql: str,
) -> tuple[dict[str, float] | None, str]:
    """跑一条"第一列是 label、第二列是计数"的 SQL。"""
    if connection is None:
        return None, db_reason
    try:
        rows = connection.execute(sql).fetchall()
    except sqlite3.Error as exc:
        return None, f"查询失败：{exc}"
    series: dict[str, float] = {}
    for row in rows:
        key = "" if row[0] is None else str(row[0])
        series[key] = float(row[1] or 0)
    return series, ""


def _query_scalar(
    connection: sqlite3.Connection | None,
    db_reason: str,
    sql: str,
) -> tuple[float | None, str]:
    if connection is None:
        return None, db_reason
    try:
        row = connection.execute(sql).fetchone()
    except sqlite3.Error as exc:
        return None, f"查询失败：{exc}"
    if row is None:
        return 0.0, ""
    return float(row[0] or 0), ""


# ── 采集 ────────────────────────────────────────────────────────────────────


def collect(store: Any, settings: Settings) -> dict[str, MetricValue]:
    """采集全部指标。返回 ``name → MetricValue``，**每条 CATALOG 都有条目**。

    ``store`` 只用来定位库文件（见 :func:`database_path`）——采集本身走独立只读
    连接，不 await、不碰事件循环，因此本函数是同步的，可以在 CLI handler 里直接调。
    """
    data_dir = Path(settings.data_dir)
    db_path = database_path(store, settings)
    connection, db_reason = _open_readonly(db_path)
    values: dict[str, MetricValue] = {}
    try:
        # ── 任务 ──
        series, reason = _query_series(
            connection, db_reason, "select status, count(*) from tasks group by status"
        )
        values["orbit_tasks_total"] = _make_value(
            "orbit_tasks_total", series, reason, label="status"
        )

        # ── 耗时直方图：只取终态任务，PENDING/RUNNING 的 updated_at 还在动 ──
        durations: list[float] | None
        if connection is None:
            durations, duration_reason = None, db_reason
        else:
            try:
                placeholders = ",".join("?" * len(TERMINAL_STATUSES))
                rows = connection.execute(
                    f"select created_at, updated_at from tasks where status in ({placeholders})",
                    TERMINAL_STATUSES,
                ).fetchall()
                durations = []
                for created, updated in rows:
                    seconds = _seconds_between(created, updated)
                    if seconds is not None:
                        durations.append(seconds)
                duration_reason = ""
            except sqlite3.Error as exc:
                durations, duration_reason = None, f"查询失败：{exc}"
        if durations is None:
            values["orbit_task_duration_seconds"] = MetricValue(
                metric=CATALOG_BY_NAME["orbit_task_duration_seconds"],
                available=False,
                reason=duration_reason,
            )
        else:
            buckets = tuple(
                (bound, float(sum(1 for value in durations if value <= bound)))
                for bound in DURATION_BUCKETS
            )
            values["orbit_task_duration_seconds"] = MetricValue(
                metric=CATALOG_BY_NAME["orbit_task_duration_seconds"],
                buckets=buckets,
                count=float(len(durations)),
                total=float(sum(durations)),
            )

        series, reason = _query_series(
            connection, db_reason, "select event_type, count(*) from task_events group by event_type"
        )
        values["orbit_task_events_total"] = _make_value(
            "orbit_task_events_total", series, reason, label="event_type"
        )

        series, reason = _query_series(
            connection, db_reason, "select role, count(*) from messages group by role"
        )
        values["orbit_messages_total"] = _make_value(
            "orbit_messages_total", series, reason, label="role"
        )

        # ── 工具 ──
        series, reason = _query_series(
            connection, db_reason, "select name, count(*) from tool_calls group by name"
        )
        values["orbit_tool_calls_total"] = _make_value(
            "orbit_tool_calls_total", series, reason, label="tool"
        )
        # 失败口径取自 tool_results.status（ok/error），**不是** tool_calls.status
        # （DONE/RUNNING，只记控制流）。这条在 insights 里踩过一次，见那边的注释。
        series, reason = _query_series(
            connection,
            db_reason,
            "select name, count(*) from tool_results where coalesce(status, '') <> 'ok' "
            "group by name",
        )
        values["orbit_tool_errors_total"] = _make_value(
            "orbit_tool_errors_total", series, reason, label="tool"
        )
        total, reason = _query_scalar(
            connection, db_reason, "select coalesce(sum(bytes), 0) from tool_results"
        )
        values["orbit_tool_result_bytes"] = (
            _make_value("orbit_tool_result_bytes", None, reason)
            if total is None
            else MetricValue(metric=CATALOG_BY_NAME["orbit_tool_result_bytes"], samples={"": total})
        )

        # ── 三向 token 归因 ──
        values["orbit_llm_tokens_total"] = _collect_tokens(connection, db_reason)

        # ── 注入面 ──
        total, reason = _query_scalar(
            connection, db_reason, "select coalesce(sum(hit_count), 0) from memories"
        )
        values["orbit_memory_injections_total"] = (
            _make_value("orbit_memory_injections_total", None, reason)
            if total is None
            else MetricValue(
                metric=CATALOG_BY_NAME["orbit_memory_injections_total"], samples={"": total}
            )
        )
        total, reason = _query_scalar(
            connection, db_reason, "select coalesce(sum(usage_count), 0) from skills"
        )
        values["orbit_skill_injections_total"] = (
            _make_value("orbit_skill_injections_total", None, reason)
            if total is None
            else MetricValue(
                metric=CATALOG_BY_NAME["orbit_skill_injections_total"], samples={"": total}
            )
        )
        total, reason = _query_scalar(connection, db_reason, "select count(*) from chunks")
        values["orbit_vector_chunks"] = (
            _make_value("orbit_vector_chunks", None, reason)
            if total is None
            else MetricValue(metric=CATALOG_BY_NAME["orbit_vector_chunks"], samples={"": total})
        )
    finally:
        if connection is not None:
            connection.close()

    # ── 文件类数据源：大小与账本。不存在就是"无数据源"，不是 0 字节 ──
    values["orbit_db_size_bytes"] = _file_size_value("orbit_db_size_bytes", db_path, "主库")
    values["orbit_checkpoint_bytes"] = _file_size_value(
        "orbit_checkpoint_bytes", data_dir / "checkpoints.sqlite", "检查点库"
    )

    records, reason = _read_jsonl(data_dir / "cron" / "runs.jsonl")
    ledger: dict[str, float] | None
    if reason:
        ledger = None
    else:
        ledger = {"ok": 0.0, "error": 0.0}
        for record in records:
            key = "ok" if record.get("ok") else "error"
            ledger[key] += 1
    values["orbit_cron_runs_total"] = _make_value(
        "orbit_cron_runs_total", ledger, reason, label="result"
    )

    records, reason = _read_jsonl(data_dir / "gateway" / "deliveries.jsonl")
    if reason:
        ledger = None
    else:
        ledger = {}
        for record in records:
            key = str(record.get("status") or "unknown")
            ledger[key] = ledger.get(key, 0.0) + 1
    values["orbit_gateway_deliveries_total"] = _make_value(
        "orbit_gateway_deliveries_total", ledger, reason, label="status"
    )

    # uptime 永远无数据源：Orbit 没有常驻进程自报启动时间，从日志首行推出来的是
    # "日志轮转时长"而不是"进程存活时长"——那是个会误导人的数，宁可标缺失。
    values["orbit_uptime_seconds"] = MetricValue(
        metric=CATALOG_BY_NAME["orbit_uptime_seconds"],
        available=False,
        reason=(
            "Orbit 不记录常驻进程的启动时间（`orbit serve` 每次前台启动、没有 pid 登记表）；"
            "从日志首行推算得到的是日志轮转窗口，不是进程存活时长，故不出数"
        ),
    )

    # 补齐 CATALOG 里万一漏写的条目：渲染与测试都假定"每条指标都有结果"。
    for metric in CATALOG:
        values.setdefault(
            metric.name,
            MetricValue(metric=metric, available=False, reason="未实现采集"),
        )
    return values


def _collect_tokens(
    connection: sqlite3.Connection | None, db_reason: str
) -> MetricValue:
    """三向 token 归因。cached 只有模型自报才出数，否则记进 ``missing``。"""
    metric = CATALOG_BY_NAME["orbit_llm_tokens_total"]
    if connection is None:
        return MetricValue(metric=metric, available=False, reason=db_reason)
    try:
        rows = connection.execute(
            "select payload from task_events where event_type = 'task_completed'"
        ).fetchall()
    except sqlite3.Error as exc:
        return MetricValue(metric=metric, available=False, reason=f"查询失败：{exc}")
    prompt = 0.0
    completion = 0.0
    cached = 0.0
    cached_reported = False
    for (payload,) in rows:
        usage = _token_usage(payload)
        prompt += float(usage.get("prompt_tokens") or 0)
        completion += float(usage.get("completion_tokens") or 0)
        # OpenAI 兼容端点把缓存命中放在 prompt_tokens_details.cached_tokens，
        # 也有实现直接给 cached_tokens。两处都看，只认模型自报的那个数。
        details = usage.get("prompt_tokens_details")
        reported = usage.get("cached_tokens")
        if reported is None and isinstance(details, dict):
            reported = details.get("cached_tokens")
        if reported is not None:
            cached_reported = True
            cached += float(reported or 0)
    samples = {"input": prompt, "output": completion}
    missing: dict[str, str] = {}
    if cached_reported:
        samples["cached"] = cached
    else:
        missing["cached"] = (
            "task_events.payload.token_usage 里没有 cached_tokens / "
            "prompt_tokens_details.cached_tokens（只有模型自报缓存命中时才有）"
        )
    return MetricValue(metric=metric, label="direction", samples=samples, missing=missing)


def _token_usage(payload: Any) -> dict[str, Any]:
    """``task_events.payload`` 是 JSON 列，驱动层可能给 dict 也可能给 str。"""
    if isinstance(payload, dict):
        usage = payload.get("token_usage")
        return usage if isinstance(usage, dict) else {}
    if isinstance(payload, str) and payload.strip():
        try:
            parsed = json.loads(payload)
        except ValueError:
            return {}
        if isinstance(parsed, dict):
            usage = parsed.get("token_usage")
            return usage if isinstance(usage, dict) else {}
    return {}


def _seconds_between(start: Any, end: Any) -> float | None:
    """两个 ISO 时间串的差（秒）。解析不了返回 None——不要拿 0 冒充"零耗时"。"""
    from datetime import datetime

    if not isinstance(start, str) or not isinstance(end, str):
        return None
    try:
        return max(0.0, (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds())
    except ValueError:
        return None


def _file_size_value(name: str, path: Path, label: str) -> MetricValue:
    """文件大小类指标。文件不存在 → 无数据源（**不是 0 字节**）。"""
    metric = CATALOG_BY_NAME[name]
    if not path.exists():
        return MetricValue(metric=metric, available=False, reason=f"{path} 不存在（{label}还没创建）")
    try:
        return MetricValue(metric=metric, samples={"": float(path.stat().st_size)})
    except OSError as exc:
        return MetricValue(metric=metric, available=False, reason=f"读不了 {path}：{exc}")


# ── 渲染 ────────────────────────────────────────────────────────────────────


def _escape_help(text: str) -> str:
    """HELP 里只有反斜杠和换行需要处理（换行在构造时已被挡掉）。"""
    return text.replace("\\", "\\\\").replace("\n", " ")


def _escape_label(value: str) -> str:
    """标签值转义：``\\`` → ``\\\\``、``"`` → ``\\"``、换行 → ``\\n``。"""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _number(value: float) -> str:
    """数值格式化。整数不带小数点（`3` 而不是 `3.0`），其余用 6 位有效数字。"""
    if value != value or value in (float("inf"), float("-inf")):  # NaN / ±Inf
        return "NaN" if value != value else ("+Inf" if value > 0 else "-Inf")
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6g}"


def render_prometheus(values: Mapping[str, MetricValue]) -> str:
    """把采集结果渲染成 Prometheus 文本格式（格式说明见模块 docstring）。

    不可用的指标只输出 ``# HELP`` / ``# TYPE`` 和一条解释性注释，**不输出样本行**：
    一个 0 值样本会让人以为"测到了、值是 0"。
    """
    lines: list[str] = []
    for metric in CATALOG:
        value = values.get(metric.name)
        if value is None:
            continue
        lines.append(f"# HELP {metric.name} {_escape_help(metric.help)}")
        lines.append(f"# TYPE {metric.name} {metric.kind}")
        if not value.available:
            lines.append(f"# 无数据源：{_escape_help(value.reason or '未说明原因')}")
            continue
        if metric.kind == "histogram":
            lines.extend(_histogram_lines(metric.name, value))
            continue
        lines.extend(_sample_lines(metric.name, value))
        for series_key, why in value.missing.items():
            lines.append(f"# 系列 {series_key} 无数据源：{_escape_help(why)}")
    return "\n".join(lines) + "\n"


def _sample_lines(name: str, value: MetricValue) -> list[str]:
    samples = value.samples or {"": 0.0}
    lines: list[str] = []
    for key, number in samples.items():
        if value.label and key:
            lines.append(f'{name}{{{value.label}="{_escape_label(key)}"}} {_number(number)}')
        else:
            lines.append(f"{name} {_number(number)}")
    return lines


def _histogram_lines(name: str, value: MetricValue) -> list[str]:
    lines: list[str] = []
    for bound, cumulative in value.buckets:
        lines.append(f'{name}_bucket{{le="{_number(bound)}"}} {_number(cumulative)}')
    # +Inf 桶必须存在且等于样本总数，否则 Prometheus 会把直方图算成残缺。
    lines.append(f'{name}_bucket{{le="+Inf"}} {_number(value.count)}')
    lines.append(f"{name}_count {_number(value.count)}")
    lines.append(f"{name}_sum {_number(value.total)}")
    return lines


# ── 人读视图（`orbit monitoring metrics` 用） ────────────────────────────────


def summarize(values: Mapping[str, MetricValue]) -> list[tuple[str, str, str, str]]:
    """压成人读的行：``(指标, 类型/单位, 值或"无数据源"，来源)``。

    值那一列宁可写"无数据源"也不写 0——这是本模块存在的理由，展示层同样不能破例。
    """
    rows: list[tuple[str, str, str, str]] = []
    for metric in CATALOG:
        value = values.get(metric.name)
        if value is None:
            continue
        if not value.available:
            shown = f"无数据源：{value.reason}"
        elif metric.kind == "histogram":
            shown = f"n={_number(value.count)} sum={_number(value.total)}s"
        elif value.label:
            shown = "，".join(f"{key or '-'}={_number(number)}" for key, number in value.samples.items())
            if value.missing:
                shown += "（缺：" + "、".join(value.missing) + "）"
        else:
            shown = _number(value.value)
        rows.append((metric.name, f"{metric.kind}/{metric.unit}", shown, metric.source))
    return rows


def as_dict(values: Mapping[str, MetricValue]) -> dict[str, Any]:
    """``--json`` 载荷：结构化、可机读，且**显式**带 available/reason。"""
    payload: dict[str, Any] = {}
    for metric in CATALOG:
        value = values.get(metric.name)
        if value is None:
            continue
        entry: dict[str, Any] = {
            "kind": metric.kind,
            "unit": metric.unit,
            "help": metric.help,
            "source": metric.source,
            "available": value.available,
        }
        if not value.available:
            entry["reason"] = value.reason
        elif metric.kind == "histogram":
            entry["count"] = value.count
            entry["sum"] = value.total
            entry["buckets"] = [{"le": bound, "count": count} for bound, count in value.buckets]
        else:
            entry["label"] = value.label
            entry["samples"] = dict(value.samples)
            if value.missing:
                entry["missing"] = dict(value.missing)
        payload[metric.name] = entry
    return payload


def _schema_value(value: MetricValue) -> float:
    """把一次采集压成 schema v2 要求的**单个数字**。

    v2 规定 `available` 为真时必须有数字 `value`，而本模块的采集结果里
    histogram 是 `count`/`total`/`buckets`、counter/gauge 是 `samples`。
    所以这里做一次**显式的**映射，而不是让校验器去猜：

    - histogram → **观测次数 `count`**（"有多少次"永远是个有意义的标量）；
      耗时总和与分桶另放在 `sum` / `buckets` 里，不丢。
    - 其余 → `samples` 求和（无 label 时就是那个值；有 label 时是聚合，
      逐 label 明细另放在 `samples` 里）。
    """
    if value.metric.kind == "histogram":
        # **用 	otal 不用 count**。目录里唯一的 histogram 是
        # orbit_task_duration_seconds（help 写「任务墙钟耗时」）——
        # 若 alue 给次数，下游读到 alue: 42 会认为「42 秒」，
        # 而它其实是「42 次任务」。**通用性不能凌驾于语义一致性。**
        # 次数另以 count 显式给出（uckets 里也有，但显式更清楚）。
        return float(value.total)
    return float(sum(value.samples.values()))


def as_schema_payload(values: Mapping[str, MetricValue]) -> dict[str, Any]:
    """``shared_metrics`` schema **v2** 载荷——**生产方真的按契约产出**（MATRIX 15.4）。

    ## 为什么需要这个函数

    `app/metrics_schema.py` 的 v1/v2 双版本、`schema_version` 字段、单向迁移与
    校验器**都是真的**（有测试）。但**唯一 importer 是 `orbit metrics schema`
    这条报告命令**——`app/metrics.py` 自己**不用 schema**。
    于是校验器校验的是**用户喂进来的任意 JSON**，而不是本产品产出的东西：
    **一个只有校验器、没有产出方的契约，等于没有契约。**

    ## 关键点：产出时**当场校验**

    `validate()` 不是给用户看的门，是**给生产方自己的门**：字段名写错、类型漂移、
    `available` 为真却没有数字 `value`——这些都会在这里**立刻抛**，
    而不是等到某天有人拿 `--json` 的输出喂进校验器才发现。
    **产出方自校验，是让契约活着的唯一方式。**

    ## 边界

    校验通过**只证明形状对**，不证明数值对——数值的正确性由 `collect()` 的测试管。
    这两件事不要混。
    """
    from app import metrics_schema

    metrics: dict[str, Any] = {}
    for metric in CATALOG:
        value = values.get(metric.name)
        if value is None:
            continue
        entry: dict[str, Any] = {
            "available": value.available,
            "source": metric.source,
            "help": metric.help,
        }
        if value.available:
            entry["value"] = _schema_value(value)
            # 明细照带：schema 不禁止额外键，而丢掉它们会让这个载荷比 `as_dict` 更难用
            if metric.kind == "histogram":
                entry["count"] = float(value.count)
                entry["buckets"] = [
                    {"le": float(bound), "count": float(count)} for bound, count in value.buckets
                ]
            else:
                entry["samples"] = {key: float(number) for key, number in value.samples.items()}
        else:
            # 不可用时**必须**有说明——空着等于没说（schema 的硬要求）
            entry["reason"] = value.reason or "无数据源"
        metrics[metric.name] = entry

    payload: dict[str, Any] = {
        "schema_version": metrics_schema.CURRENT_VERSION,
        "metrics": metrics,
    }
    # **产出方自校验**：契约一旦漂移，这里立刻炸，而不是等下游发现
    metrics_schema.validate(payload)
    return payload
