"""`--usage-file`（MATRIX 1.7 的洞，第 64 轮实现）。

## Hermes 语义（`research/01-cli-surface.md:1834`）

> `--usage-file PATH` —— **仅 one-shot 模式**：运行后把 JSON 用量报告
> （估算成本、token 数、模型、`api_calls`）写到该路径

## 本仓哪些字段精确、哪些近似、哪些没有

- `tokens` **精确**（`Chunk` 按 `task_id` 汇总）
- `model` / `duration_seconds` **精确**
- `api_calls` **近似**（`TaskEvent` 里 `event_type` 含 `model` 的条数，不等于 Hermes 的口径）
- `cost` **没有**（本仓无单价表）→ 写 `null` 并说明原因

**「这一项没有」比「这一项是错的」好。**
"""

from __future__ import annotations

import json

from test_chat import _stub_stack

from app.cli.main import main


def test_usage_file_is_written_after_a_oneshot(tmp_path, monkeypatch):
    """**one-shot 跑完必须写出报告**，且字段是实测来的不是编的。"""
    _stub_stack(tmp_path, monkeypatch)
    target = tmp_path / "usage.json"

    main(["--data-dir", str(tmp_path / "data"), "-z", "问一句", "--usage-file", str(target)])

    assert target.is_file(), "--usage-file 没写出文件"
    report = json.loads(target.read_text(encoding="utf-8"))
    assert report["task_id"], report
    assert "model" in report
    # 台账缺失时为 null（不是 0）——两种都合法，但不能是编的数
    assert report["tokens"] is None or isinstance(report["tokens"], int), report
    assert report["status"], report


def test_cost_is_null_with_a_reason_not_a_fabricated_number(tmp_path, monkeypatch):
    """**成本写 `null` 而不是 `0.0`** —— 本仓没有单价表。

    凭空写 `0.0` 或按硬编码单价算，都是给读者一个假数字。
    """
    _stub_stack(tmp_path, monkeypatch)
    target = tmp_path / "usage.json"
    main(["--data-dir", str(tmp_path / "data"), "-z", "问一句", "--usage-file", str(target)])

    report = json.loads(target.read_text(encoding="utf-8"))
    assert report["cost"] is None, f"成本被编成了 {report['cost']!r}"
    assert "单价表" in report["cost_note"], report["cost_note"]


def test_the_ledger_source_is_named_in_the_report(tmp_path, monkeypatch):
    """**近似就要标近似**——`TaskEvent.event_type` 是本仓自己定的名字。"""
    _stub_stack(tmp_path, monkeypatch)
    target = tmp_path / "usage.json"
    main(["--data-dir", str(tmp_path / "data"), "-z", "问一句", "--usage-file", str(target)])

    report = json.loads(target.read_text(encoding="utf-8"))
    assert "summary_cost" in report["ledger_note"], report["ledger_note"]


def test_no_usage_file_means_no_file(tmp_path, monkeypatch):
    """**不给 `--usage-file` 就不该凭空写文件**（成对断言：防「总是写」的实现）。"""
    _stub_stack(tmp_path, monkeypatch)
    main(["--data-dir", str(tmp_path / "data"), "-z", "问一句"])

    assert not (tmp_path / "usage.json").exists()


def test_an_unwritable_path_warns_but_does_not_fail_the_answer(tmp_path, monkeypatch, capsys):
    """**写报告失败不该让成功的问答变成失败。**"""
    _stub_stack(tmp_path, monkeypatch)
    bad = tmp_path / "usage.json" / "nested" / "deep.json"  # 父级是文件，建不了目录
    (tmp_path / "usage.json").write_text("占位", encoding="utf-8")

    code = main(["--data-dir", str(tmp_path / "data"), "-z", "问一句", "--usage-file", str(bad)])
    assert code == 0, "写报告失败把退出码改成了非 0"
