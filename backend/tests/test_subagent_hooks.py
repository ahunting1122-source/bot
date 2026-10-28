"""子 agent 的生命周期钩子真的会触发（MATRIX 13.5）。

## 为什么需要这条

`exporters.py` 的 `LANGFUSE_HOOKS` 订阅了 `subagent_start` / `subagent_stop`——
观测端靠它们把子 agent 的 span 挂到父任务下面。**没有发射点时，
Langfuse 上的子 agent 就是一个没有起点的孤儿 span**（或只有 start 没有 stop 的悬挂 span，
比没有更误导）。

判据三层：
1. 正常委派：`start` 与 `stop` 各发一次，且带 `child_id` 能把两条对上；
2. **失败的子任务也要发 stop**——否则悬挂；
3. `EMITTED_EVENTS` 里必须有这两个名字（否则订阅时会被 `log.warning` 点名）。
"""

from __future__ import annotations

import pytest

from app import hooks
from app.delegate import DelegateRunner, DelegateSpec


class FakeChild:
    """最小的假子任务 runner（`tests/` 不是包，所以不能从 `test_delegate` 导入）。"""

    def __init__(self, *, output: str = "done", error: str = "") -> None:
        self.output = output
        self.error = error

    async def run(self, goal: str, **kwargs):
        payload: dict = {"output": self.output}
        if self.error:
            payload["error"] = self.error
        return payload


@pytest.fixture()
def recorded():
    """挂两个记录器，收完就卸——**不要污染 `DEFAULT_HOOKS`**。

    注意钩子的签名是 **`fn(payload)` 一个参数**（事件名在注册时就定了），
    不是 `fn(event, payload)`。
    """
    seen: list[tuple[str, dict]] = []

    def _make(event: str):
        def _record(payload: dict) -> None:
            seen.append((event, dict(payload)))

        return _record

    hooks.DEFAULT_HOOKS.register("subagent_start", _make("subagent_start"))
    hooks.DEFAULT_HOOKS.register("subagent_stop", _make("subagent_stop"))
    try:
        yield seen
    finally:
        hooks.DEFAULT_HOOKS.clear()


async def test_subagent_start_and_stop_fire_and_can_be_paired(recorded):
    """正常委派：start 与 stop 各一次，`child_id` 能对上。"""
    runner = DelegateRunner(runner_factory=lambda spec, budget: FakeChild())

    result = await runner.run(DelegateSpec(goal="数一下文件"))
    assert result.ok is True

    starts = [p for e, p in recorded if e == "subagent_start"]
    stops = [p for e, p in recorded if e == "subagent_stop"]
    assert len(starts) == 1, f"subagent_start 应该发一次，实际 {len(starts)}"
    assert len(stops) == 1, f"subagent_stop 应该发一次，实际 {len(stops)}"

    # **能配对**才有用：观测端靠 child_id 把 stop 挂回 start 的 span
    assert starts[0]["child_id"] == stops[0]["child_id"]
    assert starts[0]["goal"] == "数一下文件"
    # start 报预算上限，stop 报实际用量——两者都在，才能算利用率
    assert "steps" in starts[0]["budget"]
    assert "steps_used" in stops[0]


async def test_failed_subagent_still_emits_stop(recorded):
    """**失败的子任务也必须发 stop**——否则观测端留下悬挂 span。"""
    runner = DelegateRunner(
        runner_factory=lambda spec, budget: FakeChild(error="子任务自己报错")
    )

    result = await runner.run(DelegateSpec(goal="会失败的任务"))
    assert result.ok is False

    starts = [p for e, p in recorded if e == "subagent_start"]
    stops = [p for e, p in recorded if e == "subagent_stop"]
    assert len(starts) == 1
    assert len(stops) == 1, "失败的子任务没发 subagent_stop —— 观测端会留下悬挂 span"
    assert starts[0]["child_id"] == stops[0]["child_id"]


async def test_rejected_delegation_emits_neither():
    """**被预检拒绝的委派不发事件**——它没有真的起过一个子 agent。

    这条守的是「别为了凑事件数而虚报」：拒绝发生在 `runner_factory` 之前，
    没有 span 可开，也就不该有 start。
    """
    seen: list[str] = []

    def _record(payload: dict) -> None:
        seen.append("fired")

    hooks.DEFAULT_HOOKS.register("subagent_start", _record)
    hooks.DEFAULT_HOOKS.register("subagent_stop", _record)
    try:
        runner = DelegateRunner(runner_factory=lambda spec, budget: FakeChild())
        # depth=3 超过上限 2，会被 `_preflight` 拒
        result = await runner.run(DelegateSpec(goal="太深了", depth=3))
        assert result.ok is False
        assert seen == [], f"被拒的委派不该发子 agent 事件，实际发了 {seen}"
    finally:
        hooks.DEFAULT_HOOKS.clear()


def test_both_events_are_declared_as_emitted():
    """`EMITTED_EVENTS` 必须包含这两个——否则订阅时会被点名警告。"""
    assert "subagent_start" in hooks.EMITTED_EVENTS
    assert "subagent_stop" in hooks.EMITTED_EVENTS
