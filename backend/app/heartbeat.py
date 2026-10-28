"""心跳与"卡死"判定（MATRIX 2.3）。

## 为什么不能只看总时长

工具执行本来就可能是长任务：跑一次全量测试、编译一个大项目、抓一个慢页面，
几十分钟都正常。用同一个阈值去判"是不是卡死了"，会掉进两难：
阈值调小了，正常的长编译被误判成卡死；阈值调大了，真卡死时几小时都没人发现。

Hermes 的答案是**双阈值**（research 09 §1.3，`delegate_tool.py:1169-1185`）：

    _HEARTBEAT_INTERVAL             = 30 s
    _HEARTBEAT_STALE_CYCLES_IDLE    = 15 → 450 s   两轮之间毫无进展
    _HEARTBEAT_STALE_CYCLES_IN_TOOL = 40 → 1200 s  卡在同一个工具里

差别来自**先验**：
* 空闲 450 s 一定是出事了——模型早就该回、事件循环没堵的话 `await` 早该回来；
* 工具里 1200 s 仍然可能是健康的——那是别人机器上的 `pytest` 或 `cargo build`。

所以判据是"**有没有进展**"，不是"跑了多久"：任何一次 `beat()` 都是进展，计时清零。
计数用"周期"而不是秒来表达，是因为阈值必须与心跳间隔绑定——改间隔不改周期数，
判定语义就跟着变了（30 s × 15 ≠ 10 s × 15）。

## 判到 stuck 之后做什么：什么都不做

Hermes 只**撤掉续命**（`break`），把"杀不杀"交给上一级 gateway 的 inactivity
超时——级联中断，不在这一层动手。本模块照做：只**报**卡在哪一步，不动手杀。
理由很实在：只有调用方知道"这一刀会不会砍掉一个本来能跑完的编译"，
而本模块只知道"多久没动静"。
"""

from __future__ import annotations

import time
from collections.abc import Callable

#: 心跳周期（秒）。与 Hermes 的 `_HEARTBEAT_INTERVAL` 一致。
INTERVAL_SECONDS = 30.0

#: 空闲判定：连续多少个周期毫无进展就算卡死（15 × 30 s = 450 s）。
IDLE_CYCLES = 15

#: 工具执行中判定：连续多少个周期没进展才算卡死（40 × 30 s = 1200 s）。
IN_TOOL_CYCLES = 40

#: `beat()` 的 kind 用这个前缀表示"正在执行某个工具"，例如 `tool:run_command`。
TOOL_PREFIX = "tool:"


class Heartbeat:
    """单条执行线程的心跳计时器。

    **为什么记 kind 而不是只记时间戳**：卡死时用户要的是"卡在哪"，不是"卡了多久"。
    只存时间戳的话，事后只能从日志里翻最后一行；存下最后一次 `beat()` 的 kind，
    `status()` 就能直接说出"停在 `think` 之后"或"工具 run_command 执行中"。

    **为什么区分"在工具里"**：见模块 docstring——同一把尺子量两种任务必然出错。

    线程安全：只在事件循环里用（`Nodes` 各节点同一时刻只有一个在跑），不加锁。
    """

    def __init__(
        self,
        *,
        interval: float = INTERVAL_SECONDS,
        idle_cycles: int = IDLE_CYCLES,
        in_tool_cycles: int = IN_TOOL_CYCLES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.interval = float(interval)
        self.idle_cycles = int(idle_cycles)
        self.in_tool_cycles = int(in_tool_cycles)
        #: 空闲上限（秒）= 周期 × 间隔。算一次存下来，避免每次 status 都重算。
        self.idle_limit = self.interval * self.idle_cycles
        self.in_tool_limit = self.interval * self.in_tool_cycles
        self._clock = clock
        self._last = self._clock()
        self._step = "start"
        self._tool: str | None = None
        self._tool_started: float | None = None
        self.beats = 0

    def beat(self, kind: str) -> None:
        """报一次进展。`kind` 是节点名，或 `tool:<工具名>` 表示正在执行工具。"""
        now = self._clock()
        self._last = now
        self.beats += 1
        self._step = kind
        if kind.startswith(TOOL_PREFIX):
            name = kind[len(TOOL_PREFIX) :]
            # 换工具 = 新的一次工具执行，计时从头开始：前一个工具跑很久不代表
            # 这个工具也卡了（`run_command` 之后紧接着 `local_read_document`）。
            if name != self._tool:
                self._tool = name
                self._tool_started = now
        else:
            # 离开工具：工具计时清零，否则"工具里跑过 900 s"会污染之后的空闲判定。
            self._tool = None
            self._tool_started = None

    def status(self) -> dict[str, object]:
        """当前状态。`stuck` 为真时 `stuck_at` 说明卡在哪一步、超了哪个上限。"""
        now = self._clock()
        idle = max(0.0, now - self._last)
        in_tool = 0.0 if self._tool_started is None else max(0.0, now - self._tool_started)
        if self._tool_started is not None:
            stuck = in_tool >= self.in_tool_limit
            elapsed, limit = in_tool, self.in_tool_limit
            where = (
                f"工具 `{self._tool}` 执行中：{in_tool:.0f}s 无进展"
                f"（工具内上限 {self.in_tool_limit:.0f}s）"
            )
        else:
            stuck = idle >= self.idle_limit
            elapsed, limit = idle, self.idle_limit
            where = (
                f"停在 `{self._step}` 之后：{idle:.0f}s 无进展"
                f"（空闲上限 {self.idle_limit:.0f}s）"
            )
        return {
            "alive": not stuck,
            "idle_seconds": round(idle, 1),
            "in_tool_seconds": round(in_tool, 1),
            "stuck": stuck,
            # 没卡死时给空串而不是 None：调用方（事件流/CLI 表格）不用再判一次类型。
            "stuck_at": where if stuck else "",
            "step": self._step,
            "tool": self._tool or "",
            "elapsed_seconds": round(elapsed, 1),
            "limit_seconds": round(limit, 1),
            "beats": self.beats,
        }


__all__ = [
    "IDLE_CYCLES",
    "INTERVAL_SECONDS",
    "IN_TOOL_CYCLES",
    "TOOL_PREFIX",
    "Heartbeat",
]
