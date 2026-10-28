"""metrics schema 版本化与 exporter 插件的契约测试（MATRIX 15.4 / 15.5）。"""

from __future__ import annotations

import json

import pytest

from app import metrics_schema as ms
from app.exporters import (
    LANGFUSE_HOOKS,
    JsonlExporter,
    LangfuseExporter,
    NemoRelayExporter,
    NetworkUnavailable,
    default_exporters,
)

# ── 15.4 schema 版本 ────────────────────────────────────────────────────────


def test_v1_payload_validates():
    payload = {"schema_version": 1, "metrics": {"tasks": 3, "tokens": 1.5}}
    assert ms.validate(payload) == 1


def test_v1_rejects_non_numeric_metric():
    payload = {"schema_version": 1, "metrics": {"tasks": "三"}}
    with pytest.raises(ms.SchemaError, match="tasks"):
        ms.validate(payload)


def test_v1_rejects_bool_as_number():
    """`True` 是 `int` 的子类，但它不是"数字指标"——放过去会让看板把开关当计数。"""
    payload = {"schema_version": 1, "metrics": {"flag": True}}
    with pytest.raises(ms.SchemaError, match="数字"):
        ms.validate(payload)


def test_v2_payload_validates():
    payload = {
        "schema_version": 2,
        "collected_at": "2026-01-01T00:00:00Z",
        "metrics": {
            "tasks": {"value": 3, "available": True, "source": "tasks 表", "help": "任务数"},
        },
        "degraded": [],
    }
    assert ms.validate(payload) == 2


def test_v2_unavailable_metric_must_explain_why():
    """标了不可用却不说原因，等于让用户自己去猜——这条必须被拦住。"""
    payload = {
        "schema_version": 2,
        "metrics": {"latency": {"available": False}},
        "degraded": ["latency"],
    }
    with pytest.raises(ms.SchemaError, match="source/help"):
        ms.validate(payload)


def test_v2_available_metric_must_have_number():
    payload = {"schema_version": 2, "metrics": {"tasks": {"available": True}}}
    with pytest.raises(ms.SchemaError, match="没有数字"):
        ms.validate(payload)


def test_unknown_schema_version_is_rejected():
    with pytest.raises(ms.SchemaError, match="未知的 schema_version"):
        ms.validate({"schema_version": 99, "metrics": {}})


def test_migrate_v1_to_v2_marks_everything_available():
    """v1 的 0 是"有数据且为 0"，不是"缺数据"——猜错方向会让人去查不存在的故障。"""
    migrated = ms.migrate_v1_to_v2({"schema_version": 1, "metrics": {"tasks": 0}})
    assert migrated["schema_version"] == 2
    assert migrated["metrics"]["tasks"]["value"] == 0
    assert migrated["metrics"]["tasks"]["available"] is True
    assert ms.validate(migrated) == 2


def test_dumps_refuses_invalid_payload():
    with pytest.raises(ms.SchemaError):
        ms.dumps({"schema_version": 2, "metrics": {"x": {"available": True}}})


def test_dumps_round_trips():
    payload = {
        "schema_version": 2,
        "collected_at": "2026-01-01T00:00:00Z",
        "metrics": {"tasks": {"value": 2, "available": True, "source": "s", "help": ""}},
        "degraded": [],
    }
    assert json.loads(ms.dumps(payload))["schema_version"] == 2


def test_current_version_is_supported():
    assert ms.CURRENT_VERSION in ms.SUPPORTED_VERSIONS


# ── 15.5 exporter ──────────────────────────────────────────────────────────


def test_jsonl_exporter_is_available_and_persists(tmp_path):
    exporter = JsonlExporter(tmp_path)
    assert exporter.available() is True
    exporter.emit("post_llm_call", {"model": "gpt-x", "tokens": 12})
    exporter.emit("post_tool_call", {"tool": "run_command"})
    assert exporter.flush() == 2
    lines = exporter.spool_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "post_llm_call"
    assert first["payload"]["tokens"] == 12


def test_remote_exporters_spool_even_though_they_cannot_send(tmp_path):
    """**发不出去也要存下来**：否则"没网"就等于"这段时间的 trace 永久缺失"。"""
    exporter = LangfuseExporter(tmp_path)
    assert exporter.available() is False
    exporter.emit("pre_llm_call", {"model": "m"})
    assert len(exporter.pending()) == 1
    with pytest.raises(NetworkUnavailable):
        exporter.flush()
    # 抛了错，事件仍在 spool 里
    assert len(exporter.pending()) == 1


def test_remote_exporter_reports_missing_env(tmp_path):
    exporter = LangfuseExporter(tmp_path)
    info = exporter.describe()
    assert info["available"] is False
    assert "HERMES_LANGFUSE_PUBLIC_KEY" in info["missing_env"]
    assert info["note"]


def test_remote_exporter_with_env_still_unavailable_offline(tmp_path, monkeypatch):
    """凭证齐了也不可用——本机没有网络。假装可用会让上层以为 trace 在传。"""
    monkeypatch.setenv("HERMES_LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("HERMES_LANGFUSE_SECRET_KEY", "sk")
    exporter = LangfuseExporter(tmp_path)
    assert exporter.missing_env() == []
    assert exporter.available() is False
    with pytest.raises(NetworkUnavailable, match="无网络"):
        exporter.flush()


def test_hooks_match_hermes_plugin_yaml():
    """11 条钩子照 Hermes 的 plugin.yaml 抄——订阅错了钩子 trace 就缺段。"""
    assert len(LANGFUSE_HOOKS) == 11
    for hook in ("pre_api_request", "post_llm_call", "subagent_stop"):
        assert hook in LANGFUSE_HOOKS
    assert JsonlExporter("/tmp").hooks() == LANGFUSE_HOOKS
    assert NemoRelayExporter("/tmp").hooks() == LANGFUSE_HOOKS


def test_bad_spool_line_is_skipped_not_fatal(tmp_path):
    """断电会截断最后一行。丢一条好过丢全部。"""
    exporter = JsonlExporter(tmp_path)
    exporter.emit("a", {})
    with exporter.spool_path.open("a", encoding="utf-8") as handle:
        handle.write('{"broken": \n')
    assert len(exporter.pending()) == 1


def test_default_exporters_pool(tmp_path):
    pool = default_exporters(tmp_path)
    assert set(pool) == {"jsonl", "langfuse", "nemo_relay"}
    assert pool["jsonl"].available() is True
    assert pool["langfuse"].available() is False
    assert pool["nemo_relay"].available() is False
