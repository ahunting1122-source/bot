"""`app/metrics.py` 的契约测试：schema 完整性、可用性语义、Prometheus 文本格式。

三条口径锁死（改实现前先读）：

1. **`CATALOG` 是固定 schema。** 指标名/类型/单位写死，测试按名字断言——这样
   "悄悄改了一个指标名"会让监控侧的抓取规则静默失效，必须被测试挡住。
2. **0 和"没有数据源"是两件事。** 空库上 `orbit_tasks_total` 是 0（表在、行数为 0），
   而 `orbit_checkpoint_bytes` 是 `available=False` + 原因。**断言不填 0**：
   一个假的 0 会让告警以为"体积正常"，实际是"根本没测到"。
3. **渲染输出要能被 Prometheus 解析。** 这里逐行校验 `# HELP` / `# TYPE` / 样本行
   的形状（而不是只断言"输出里有某个名字"）——格式错一个引号，抓取端整份丢弃。
"""

from __future__ import annotations

import re

import pytest

from app.config import Settings
from app.main import migrate
from app.metrics import (
    CATALOG,
    CATALOG_BY_NAME,
    KINDS,
    collect,
    render_prometheus,
    summarize,
)
from app.storage import Conversation, Memory, Message, Operation, Skill, Store, Task, ToolResult

# ── Prometheus 文本格式的行级校验 ────────────────────────────────────────────

#: 合法的 `# HELP` 行。
HELP_LINE = re.compile(r"^# HELP ([a-zA-Z_:][a-zA-Z0-9_:]*) (\S.*)$")
#: 合法的 `# TYPE` 行。
TYPE_LINE = re.compile(r"^# TYPE ([a-zA-Z_:][a-zA-Z0-9_:]*) (counter|gauge|histogram)$")
#: 合法的标签组：`{k="v",k2="v2"}`，值里只允许转义过的引号与反斜杠。
LABELS = re.compile(
    r'^\{[a-zA-Z_][a-zA-Z0-9_]*="([^"\\]|\\.)*"(,[a-zA-Z_][a-zA-Z0-9_]*="([^"\\]|\\.)*")*\}$'
)
#: 合法的样本行：`name{labels} value`，数值可以是整数/小数/科学计数/+Inf/NaN。
SAMPLE_LINE = re.compile(
    r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{.*\})? "
    r"(-?\d+(\.\d+)?([eE][+-]?\d+)?|\+Inf|-Inf|NaN)$"
)


def parse_prometheus(text: str) -> dict[str, dict]:
    """把渲染结果解析回结构，**顺带断言每一行都合法**。

    这是"输出可被 Prometheus 解析"的可执行版本：任何一行不匹配上面三条正则
    （或 `#` 注释）就直接失败，而不是等到部署后抓取端报错。
    """
    assert text.endswith("\n"), "文本格式必须以换行结尾"
    parsed: dict[str, dict] = {}
    seen_help: dict[str, bool] = {}
    for raw in text.splitlines():
        if not raw:
            raise AssertionError("空行不是合法的 exposition 行")
        if raw.startswith("#"):
            help_match = HELP_LINE.match(raw)
            if help_match:
                name = help_match.group(1)
                assert name not in parsed, f"{name} 的 # HELP 出现了两次"
                parsed[name] = {"help": help_match.group(2), "type": None, "samples": []}
                seen_help[name] = True
                continue
            type_match = TYPE_LINE.match(raw)
            if type_match:
                name, kind = type_match.group(1), type_match.group(2)
                assert name in parsed, f"{name} 的 # TYPE 出现在 # HELP 之前"
                assert parsed[name]["type"] is None, f"{name} 的 # TYPE 出现了两次"
                parsed[name]["type"] = kind
                continue
            # 其余 `#` 行是注释：Prometheus 解析器直接忽略，用来写"无数据源"的原因。
            continue
        sample = SAMPLE_LINE.match(raw)
        assert sample, f"非法的样本行：{raw!r}"
        name, labels = sample.group(1), sample.group(2)
        if labels is not None:
            assert LABELS.match(labels), f"非法的标签组：{labels!r}"
        base = name
        for suffix in ("_bucket", "_count", "_sum"):
            if name.endswith(suffix):
                base = name[: -len(suffix)]
                break
        assert base in parsed, f"样本 {name} 没有对应的 # HELP"
        parsed[base]["samples"].append((name, labels, sample.group(3)))
    for name, entry in parsed.items():
        assert entry["type"] in KINDS, f"{name} 缺 # TYPE"
    return parsed


# ── schema ──────────────────────────────────────────────────────────────────


def test_catalog_has_at_least_twelve_metrics():
    assert len(CATALOG) >= 12, f"只有 {len(CATALOG)} 个指标，少于要求的 12 个"
    names = [metric.name for metric in CATALOG]
    assert len(names) == len(set(names)), "指标名重复"
    for metric in CATALOG:
        assert metric.name.startswith("orbit_"), metric.name
        assert metric.kind in KINDS, metric.name
        assert metric.unit, f"{metric.name} 缺单位"
        assert metric.help.strip() and "\n" not in metric.help, f"{metric.name} 的 help 必须单行"
        assert metric.source.strip(), f"{metric.name} 没写数据来源"


def test_catalog_covers_the_required_metrics():
    """规格书点名的指标一个都不能少（改名 = 监控规则静默失效）。"""
    required = {
        "orbit_tasks_total",
        "orbit_task_duration_seconds",
        "orbit_tool_calls_total",
        "orbit_tool_errors_total",
        "orbit_llm_tokens_total",
        "orbit_memory_injections_total",
        "orbit_skill_injections_total",
        "orbit_checkpoint_bytes",
        "orbit_db_size_bytes",
        "orbit_cron_runs_total",
        "orbit_gateway_deliveries_total",
        "orbit_uptime_seconds",
    }
    assert required <= set(CATALOG_BY_NAME)


def test_duration_metric_is_a_histogram():
    assert CATALOG_BY_NAME["orbit_task_duration_seconds"].kind == "histogram"


# ── 采集：空库 ──────────────────────────────────────────────────────────────


async def _empty(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    return settings, store


async def test_collect_on_empty_database_does_not_crash(tmp_path):
    settings, store = await _empty(tmp_path)
    values = collect(store, settings)
    assert set(values) == set(CATALOG_BY_NAME), "每条 CATALOG 都必须有结果"
    await store.engine.dispose()


async def test_missing_data_sources_are_marked_unavailable_not_zero(tmp_path):
    """缺数据源的指标 `available=False` 且**不出数**——这是本模块的核心承诺。"""
    settings, store = await _empty(tmp_path)
    values = collect(store, settings)
    for name in (
        "orbit_checkpoint_bytes",
        "orbit_cron_runs_total",
        "orbit_gateway_deliveries_total",
        "orbit_uptime_seconds",
    ):
        value = values[name]
        assert value.available is False, f"{name} 的数据源不存在，却报成可用"
        assert value.reason.strip(), f"{name} 标了不可用但没说原因"
        assert value.samples == {}, f"{name} 不可用时不许填 0：{value.samples}"
    # 表在、行数为 0 的指标是**真的 0**，必须可用（两者不能混为一谈）。
    for name in ("orbit_tasks_total", "orbit_memory_injections_total", "orbit_vector_chunks"):
        value = values[name]
        assert value.available is True, f"{name} 的表存在，0 行就是 0，不该标不可用"
        assert value.value == 0
    await store.engine.dispose()


async def test_cached_token_direction_is_reported_as_missing(tmp_path):
    """模型不报缓存命中时，cached 这一向是"无数据源"，不并进 input、也不填 0。"""
    settings, store = await _empty(tmp_path)
    values = collect(store, settings)
    tokens = values["orbit_llm_tokens_total"]
    assert tokens.available is True
    assert set(tokens.samples) == {"input", "output"}
    assert "cached" in tokens.missing and tokens.missing["cached"].strip()
    await store.engine.dispose()


async def test_collect_without_database_reports_reason(tmp_path):
    """库文件都不存在时不能崩：库类指标全部 `available=False` 并给原因。"""
    settings = Settings(data_dir=tmp_path / "nowhere", workspace=tmp_path / "files")
    values = collect(store=None, settings=settings)
    assert values["orbit_tasks_total"].available is False
    assert "不存在" in values["orbit_tasks_total"].reason
    assert values["orbit_db_size_bytes"].available is False
    assert values["orbit_task_duration_seconds"].available is False


# ── 采集：有数据 ────────────────────────────────────────────────────────────


async def _seed(tmp_path):
    """造一个最小但真实形状的库 + 三份落盘账本（cron / gateway / checkpoint）。"""
    settings, store = await _empty(tmp_path)
    conversation = await store.save(Conversation(title="测试会话"))
    await store.save(Message(conversation_id=conversation.id, role="user", content="查一下"))
    ok_task = await store.save(Task(conversation_id=conversation.id, goal="成功的任务"))
    bad_task = await store.save(Task(conversation_id=conversation.id, goal="失败的任务"))
    await store.transition(ok_task.id, "SUCCEEDED")
    await store.transition(bad_task.id, "FAILED", error="模型超时")

    await store.save(
        Operation(id="op-1", task_id=ok_task.id, name="web_search", status="DONE", args={})
    )
    await store.save(
        Operation(id="op-2", task_id=ok_task.id, name="web_search", status="DONE", args={})
    )
    await store.save(
        ToolResult(
            operation_id="op-1",
            task_id=ok_task.id,
            name="web_search",
            status="error",
            host="example.com",
            text="boom",
            bytes=5,
            tokens=2,
            hash="a",
        )
    )
    await store.save(
        ToolResult(
            operation_id="op-2",
            task_id=ok_task.id,
            name="run_command",
            status="ok",
            host="",
            text="hello",
            bytes=7,
            tokens=1,
            hash="b",
        )
    )
    await store.event(
        ok_task.id,
        "task_completed",
        token_usage={
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "cached_tokens": 4,
        },
    )
    await store.save(Memory(key="报告格式", value="markdown", hit_count=3))
    await store.save(Skill(name="weekly", body="正文", usage_count=2))
    await store.engine.dispose()

    cron = settings.data_dir / "cron"
    cron.mkdir(parents=True, exist_ok=True)
    (cron / "runs.jsonl").write_text(
        '{"ok": true, "job_id": "j1"}\n{"ok": false, "job_id": "j2"}\n坏行\n',
        encoding="utf-8",
    )
    gateway = settings.data_dir / "gateway"
    gateway.mkdir(parents=True, exist_ok=True)
    (gateway / "deliveries.jsonl").write_text(
        '{"status": "ok"}\n{"status": "ok"}\n{"status": "failed"}\n',
        encoding="utf-8",
    )
    (settings.data_dir / "checkpoints.sqlite").write_bytes(b"x" * 64)
    return settings, Store(settings.data_dir / "app.sqlite")


async def test_collect_counts_from_real_tables(tmp_path):
    settings, store = await _seed(tmp_path)
    values = collect(store, settings)
    assert values["orbit_tasks_total"].samples == {"SUCCEEDED": 1.0, "FAILED": 1.0}
    assert values["orbit_messages_total"].samples == {"user": 1.0}
    assert values["orbit_tool_calls_total"].samples == {"web_search": 2.0}
    # 失败口径是 tool_results.status ≠ ok（不是 tool_calls.status）。
    assert values["orbit_tool_errors_total"].samples == {"web_search": 1.0}
    assert values["orbit_tool_result_bytes"].value == 12
    assert values["orbit_memory_injections_total"].value == 3
    assert values["orbit_skill_injections_total"].value == 2
    assert values["orbit_cron_runs_total"].samples == {"ok": 1.0, "error": 1.0}
    assert values["orbit_gateway_deliveries_total"].samples == {"ok": 2.0, "failed": 1.0}
    assert values["orbit_checkpoint_bytes"].value == 64
    assert values["orbit_db_size_bytes"].value > 0
    await store.engine.dispose()


async def test_token_attribution_has_three_directions(tmp_path):
    """三向归因：input / output / cached 各占一条，绝不合并。"""
    settings, store = await _seed(tmp_path)
    values = collect(store, settings)
    tokens = values["orbit_llm_tokens_total"]
    assert tokens.samples == {"input": 10.0, "output": 5.0, "cached": 4.0}
    assert tokens.missing == {}
    await store.engine.dispose()


async def test_duration_histogram_has_buckets_and_inf(tmp_path):
    settings, store = await _seed(tmp_path)
    values = collect(store, settings)
    histogram = values["orbit_task_duration_seconds"]
    assert histogram.available is True
    assert histogram.count == 2, "只有终态任务计入耗时"
    assert [bound for bound, _ in histogram.buckets] == list(
        [1.0, 5.0, 15.0, 30.0, 60.0, 120.0, 300.0, 900.0]
    )
    # 累计桶：上界越大计数不减。
    counts = [count for _, count in histogram.buckets]
    assert counts == sorted(counts)
    assert counts[-1] == histogram.count
    await store.engine.dispose()


# ── 渲染 ────────────────────────────────────────────────────────────────────


async def test_render_is_parseable_prometheus(tmp_path):
    settings, store = await _seed(tmp_path)
    text = render_prometheus(collect(store, settings))
    parsed = parse_prometheus(text)
    assert parsed["orbit_tasks_total"]["type"] == "counter"
    assert parsed["orbit_task_duration_seconds"]["type"] == "histogram"
    assert parsed["orbit_db_size_bytes"]["type"] == "gauge"
    # 每条指标都要有 HELP / TYPE，包括"无数据源"的那些。
    for metric in CATALOG:
        assert metric.name in parsed, f"{metric.name} 没出现在输出里"
    await store.engine.dispose()


async def test_render_emits_no_sample_for_unavailable_metric(tmp_path):
    """不可用的指标只留 HELP/TYPE + 注释，**不出样本行**（否则就是假 0）。"""
    settings, store = await _empty(tmp_path)
    text = render_prometheus(collect(store, settings))
    parsed = parse_prometheus(text)
    for name in ("orbit_checkpoint_bytes", "orbit_uptime_seconds"):
        assert parsed[name]["samples"] == [], f"{name} 不可用却输出了样本行"
        assert "# 无数据源：" in text
    assert "orbit_checkpoint_bytes 0" not in text
    await store.engine.dispose()


async def test_render_histogram_lines_are_complete(tmp_path):
    settings, store = await _seed(tmp_path)
    text = render_prometheus(collect(store, settings))
    lines = [line for line in text.splitlines() if line.startswith("orbit_task_duration_seconds")]
    assert any(line.startswith('orbit_task_duration_seconds_bucket{le="+Inf"}') for line in lines)
    assert any(line.startswith("orbit_task_duration_seconds_count ") for line in lines)
    assert any(line.startswith("orbit_task_duration_seconds_sum ") for line in lines)
    # +Inf 桶是最后一行桶，且等于 count。
    buckets = [line for line in lines if "_bucket{" in line]
    assert buckets[-1].startswith('orbit_task_duration_seconds_bucket{le="+Inf"}')
    count = next(line for line in lines if line.startswith("orbit_task_duration_seconds_count "))
    assert buckets[-1].rsplit(" ", 1)[1] == count.rsplit(" ", 1)[1]
    await store.engine.dispose()


async def test_render_labels_are_grouped_per_value(tmp_path):
    settings, store = await _seed(tmp_path)
    text = render_prometheus(collect(store, settings))
    assert 'orbit_tasks_total{status="SUCCEEDED"} 1' in text
    assert 'orbit_tasks_total{status="FAILED"} 1' in text
    assert 'orbit_llm_tokens_total{direction="cached"} 4' in text
    await store.engine.dispose()


def test_summarize_keeps_unavailable_visible():
    """人读视图同样不能把"没数据"写成 0。"""
    settings = Settings(data_dir="不存在的目录", workspace="不存在的目录")
    values = collect(None, settings)
    rows = {row[0]: row[2] for row in summarize(values)}
    assert rows["orbit_uptime_seconds"].startswith("无数据源")
    assert rows["orbit_checkpoint_bytes"].startswith("无数据源")


def test_metric_rejects_bad_kind():
    from app.metrics import Metric

    with pytest.raises(ValueError):
        Metric("orbit_bad", "summary", "x", "说明", "来源")
