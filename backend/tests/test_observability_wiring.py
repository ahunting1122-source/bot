"""observability exporter 真的收到事件（MATRIX 13.4 / 15.x 的验收判据）。

## 为什么需要这条

`app/exporters.py` 的 `Exporter.hooks()` 返回的是**生命周期事件名**，而
`graph/nodes.py` 在此之前**一个钩子都不发**——所以 exporter 一直是"代码在、没人调"。
本轮补了 `pre_llm_call` / `post_llm_call` / `pre_tool_call` / `post_tool_call`
四个发射点，`app/observability_sink.py::attach()` 再把 exporter 注册到
`DEFAULT_HOOKS`，这条线才通。

判据只有一句：**发一个钩子，exporter 真的收到。**
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import observability_sink
from app.hooks import DEFAULT_HOOKS


class _Recorder:
    """最小 exporter：把收到的每条事件记在内存里。"""

    name = "recorder"

    def __init__(self, *, usable: bool = True, explode: bool = False) -> None:
        self.usable = usable
        self.explode = explode
        self.events: list[tuple[str, dict]] = []

    def hooks(self) -> tuple[str, ...]:
        return ("pre_llm_call", "post_tool_call")

    def available(self) -> bool:
        return self.usable

    def describe(self) -> dict:
        return {"name": self.name, "reason": "测试里故意不可用"}

    def emit(self, event: str, payload: dict) -> None:
        if self.explode:
            raise RuntimeError("exporter 自己炸了")
        self.events.append((event, payload))

    def flush(self) -> int:
        return len(self.events)


@pytest.fixture()
def clean():
    DEFAULT_HOOKS.clear()
    observability_sink.reset()
    yield
    DEFAULT_HOOKS.clear()
    observability_sink.reset()


def test_attached_exporter_receives_emitted_hooks(clean):
    """**核心判据**：`attach()` 之后发钩子，exporter 真的收到。

    这条在 `attach()` 没接进 `main.py`（或 `nodes.py` 不发钩子）时没有意义——
    所以它同时守住两件事：注册进 `DEFAULT_HOOKS`、且钩子能被发出。
    """
    recorder = _Recorder()
    report = observability_sink.attach({"recorder": recorder})
    assert "recorder" in report["attached"]
    assert report["hooks"] == 2  # 它订阅了两个事件

    DEFAULT_HOOKS.emit("pre_llm_call", {"task_id": "t1", "attempt": 1})
    DEFAULT_HOOKS.emit("post_tool_call", {"task_id": "t1", "tool": "run_command"})

    assert [name for name, _ in recorder.events] == ["pre_llm_call", "post_tool_call"]
    assert recorder.events[0][1]["task_id"] == "t1"


def test_unavailable_exporter_is_skipped_with_a_reason(clean):
    """不可用的 exporter **不进钩子表**，但必须在报告里留下原因。

    静默跳过会让"为什么没有数据"变成谜——那比多打一行原因糟得多。
    """
    recorder = _Recorder(usable=False)
    report = observability_sink.attach({"recorder": recorder})
    assert report["attached"] == []
    assert "recorder" in report["skipped"]
    assert "故意不可用" in report["skipped"]["recorder"]
    # 没注册就不该被触发
    DEFAULT_HOOKS.emit("pre_llm_call", {})
    assert recorder.events == []


def test_exporter_error_does_not_break_the_emit(clean):
    """exporter 抛异常不能中断钩子派发——观测是**旁路**。"""
    boom = _Recorder(explode=True)
    ok = _Recorder()
    observability_sink.attach({"boom": boom, "ok": ok})

    # 不抛（HookRegistry 本身也按 hook 隔离，这里再确认 exporter 那层也兜住）
    DEFAULT_HOOKS.emit("pre_llm_call", {"task_id": "t2"})
    assert observability_sink.STATS["boom"]["failures"] >= 1
    assert [name for name, _ in ok.events] == ["pre_llm_call"]


def test_jsonl_exporter_lands_a_real_line(tmp_path, clean):
    """真 jsonl exporter 端到端：发一个钩子 → 盘上真的多一行 JSON。

    `JsonlExporter` 是唯一**不需要网络**的 exporter，所以它是这条链路
    唯一能端到端验证的样本。
    """
    from app.exporters import JsonlExporter

    exporter = JsonlExporter(tmp_path)
    observability_sink.attach({"jsonl": exporter})
    DEFAULT_HOOKS.emit("pre_llm_call", {"task_id": "t3", "attempt": 1})
    exporter.flush()

    files = [p for p in Path(tmp_path).rglob("*") if p.is_file()]
    assert files, "jsonl exporter 没有落任何文件"
    lines = [ln for p in files for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "落盘文件是空的"
    first = json.loads(lines[0])
    # 至少要能看出这是哪条事件、哪个任务
    assert "pre_llm_call" in json.dumps(first, ensure_ascii=False)
    assert "t3" in json.dumps(first, ensure_ascii=False)
