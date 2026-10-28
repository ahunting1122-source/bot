"""心跳的双阈值判卡死（MATRIX 2.3）。

两条判据来自 research 09 §1.3（`delegate_tool.py:1169-1185`）：
空闲 15 × 30 s = **450 s**，工具执行中 40 × 30 s = **1200 s**。

核心断言是"**同一把尺子量两种任务会误判**"：工具里跑 1000 s 时，
如果只看空闲时长（1000 > 450）就会把一次正常的编译判成卡死。
"""

from __future__ import annotations

from app.graph.nodes import Nodes
from app.heartbeat import IDLE_CYCLES, IN_TOOL_CYCLES, INTERVAL_SECONDS, Heartbeat


class Clock:
    """可推进的假时钟。用真 `time.monotonic` 就得 `sleep(450)`，测试不能那么跑。"""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_thresholds_are_derived_from_the_cycles():
    """周期 × 间隔才是秒数：改间隔不改周期，判定语义必须跟着变。"""
    heartbeat = Heartbeat(clock=Clock())
    assert IDLE_CYCLES * INTERVAL_SECONDS == 450
    assert IN_TOOL_CYCLES * INTERVAL_SECONDS == 1200
    assert heartbeat.idle_limit == 450
    assert heartbeat.in_tool_limit == 1200


def test_idle_past_450_seconds_is_stuck_and_says_where():
    clock = Clock()
    heartbeat = Heartbeat(clock=clock)
    heartbeat.beat("think")
    clock.advance(449)
    assert heartbeat.status()["stuck"] is False
    clock.advance(1)
    status = heartbeat.status()
    assert status["stuck"] is True
    assert status["alive"] is False
    assert status["idle_seconds"] == 450
    # "卡在哪一步"是 status 的一半价值：只有秒数的话，事后还得翻日志。
    assert "think" in str(status["stuck_at"])


def test_a_beat_clears_stuck():
    """心跳判的是"有没有进展"，不是"跑了多久"：报一次进展就该清零。"""
    clock = Clock()
    heartbeat = Heartbeat(clock=clock)
    heartbeat.beat("think")
    clock.advance(600)
    assert heartbeat.status()["stuck"] is True
    heartbeat.beat("budget")
    assert heartbeat.status()["stuck"] is False
    assert heartbeat.beats == 2


def test_tool_running_1000_seconds_is_not_stuck():
    """关键用例：工具里 1000 s 时，空闲时长早就过了 450 s 的线，但不该判卡死。"""
    clock = Clock()
    heartbeat = Heartbeat(clock=clock)
    heartbeat.beat("tool:run_command")
    clock.advance(1000)
    status = heartbeat.status()
    assert status["idle_seconds"] == 1000  # 空闲口径确实越线了
    assert status["in_tool_seconds"] == 1000
    assert status["stuck"] is False  # 但用的是工具内上限 1200 s
    assert status["tool"] == "run_command"


def test_tool_running_1200_seconds_is_stuck():
    clock = Clock()
    heartbeat = Heartbeat(clock=clock)
    heartbeat.beat("tool:pytest")
    clock.advance(1200)
    status = heartbeat.status()
    assert status["stuck"] is True
    assert "pytest" in str(status["stuck_at"])


def test_switching_tools_restarts_the_tool_clock():
    """换了工具就是新的一次执行：上一个工具跑很久不代表这个也卡了。"""
    clock = Clock()
    heartbeat = Heartbeat(clock=clock)
    heartbeat.beat("tool:run_command")
    clock.advance(1100)
    heartbeat.beat("tool:local_read_document")
    clock.advance(600)
    status = heartbeat.status()
    assert status["in_tool_seconds"] == 600
    assert status["stuck"] is False


def test_leaving_the_tool_restores_the_idle_threshold():
    """离开工具后回到空闲档：否则"工具里跑过 900 s"会污染之后的空闲判定。"""
    clock = Clock()
    heartbeat = Heartbeat(clock=clock)
    heartbeat.beat("tool:run_command")
    clock.advance(900)
    heartbeat.beat("digest")
    assert heartbeat.status()["in_tool_seconds"] == 0
    clock.advance(451)
    status = heartbeat.status()
    assert status["stuck"] is True
    assert "digest" in str(status["stuck_at"])


class _Store:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    async def event(self, task_id, kind, **payload):
        self.events.append((kind, payload))


class _Registry:
    browser = None

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    async def execute(self, task_id, operation_id, name, args, messages=None):
        return {"status": "ok", "data": {"ok": True}}


class _Policies:
    class _Results:
        def project(self, name, result):
            return result

    tool_results = _Results()


class _Runner:
    """最小 runner：只要 store / registry / policies / heartbeat 四样。"""

    def __init__(self) -> None:
        self.store = _Store()
        self.registry = _Registry()
        self.policies = _Policies()
        self.settings = None
        self.partial: dict[str, str] = {}
        self.heartbeat = Heartbeat(clock=Clock())


def test_nodes_reuses_the_runner_heartbeat_and_beats_on_entry():
    """心跳接在 `Nodes` 上（每个节点进入时 beat 一次），不改编排结构。"""
    runner = _Runner()
    nodes = Nodes(runner)
    assert nodes.heartbeat is runner.heartbeat  # 进程级那一个，不是各造各的
    nodes.beat("prepare")
    assert runner.heartbeat.beats == 1


async def test_execute_switches_the_heartbeat_into_tool_mode():
    """进入工具执行时 beat 带 `tool:` 前缀 —— 这是双阈值能生效的唯一入口。"""
    runner = _Runner()
    nodes = Nodes(runner)
    state = {
        "task_id": "t1",
        "calls": [{"name": "local_list_files", "args": {"path": "."}, "id": "c1"}],
        "actions": 0,
        "answer": {"approved": True},
        "messages": [],
    }
    await nodes.execute(state)
    status = runner.heartbeat.status()
    assert status["tool"] == "local_list_files"
    assert status["step"] == "tool:local_list_files"
