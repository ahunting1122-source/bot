"""三套**独立不叠加**的迭代预算（MATRIX 2.2）。

## 这条为什么值得单独一个模块

Hermes 有三个迭代上限，而它们的数值**不是**一回事：

| 名字 | 默认 | 是什么 |
|---|---|---|
| `agent.max_turns` | **90** | 父任务一轮对话最多走几个 think→execute 循环 |
| `delegation.max_iterations` | **50** | 单个子任务自己的循环上限 |
| `DEFAULT_MAX_ITERATIONS` | **250** | **只是 config 缺省兜底**，不是运行值 |
| desktop 入口 | 500 | 桌面壳自己的档位 |

前两个数**必须互不影响**。如果子任务消耗的轮次从父预算里扣，那么"派 5 个子任务"
就等于"父任务只剩 90−5×n 轮"——派得越多，父任务越早被自己的帮手饿死。
这就是"独立不叠加"的实际含义，也是这个模块存在的唯一理由。

第三个（250）容易被误读成"运行上限"，所以这里显式标注它**不是**——把它当运行值
会让所有配置覆盖失效（因为 250 永远大于 90，看起来永远没到上限）。

## 与 `app/budget.py` 的分工

`budget.py` 管的是**单轮之内**的资源（结果大小分级、token 预算、预览裁剪）；
这里管的是**跨轮**的迭代次数。两者叠加使用，但互不扣减。


**与 `app/budget.py::TurnBudget` 的区别（这个命名陷阱害过人）**：
`budget.TurnBudget` 是**工具结果字符累加器**（管单次输出多大），生产里在用；
本模块的 `IterationBudgets` 是**迭代次数上限**（一个任务最多走几轮 think→execute）。
两者名字曾经几乎一样、一个在用一个是死配置——**改名就是为了不再骗人**。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.delegate import CHILD_BUDGET_STEPS, PARENT_BUDGET_STEPS

#: config 的缺省兜底值。**不是运行值**——写在这里是为了让"它不是运行值"这件事
#: 有一个可引用的位置，而不是散落在注释里。
CONFIG_FALLBACK_ITERATIONS = 250

#: desktop 入口自己的档位（Hermes 的桌面壳不读 CLI 的 90）。
DESKTOP_TURNS = 500


@dataclass(frozen=True)
class IterationBudgets:
    """一次运行实际生效的三个上限 + 它们各自的来源。"""

    parent_turns: int
    child_iterations: int
    #: 来源标记，便于 `--json` 时说明"这个数是默认还是你配的"。
    parent_source: str = "default"
    child_source: str = "default"

    def to_dict(self) -> dict[str, Any]:
        return {
            "parent_turns": self.parent_turns,
            "child_iterations": self.child_iterations,
            "parent_source": self.parent_source,
            "child_source": self.child_source,
            "independent": True,
            "config_fallback_is_runtime_value": False,
            "config_fallback": CONFIG_FALLBACK_ITERATIONS,
            "desktop_turns": DESKTOP_TURNS,
        }


def _positive(value: Any) -> int | None:
    """把配置值转成正整数；非法值返回 None 让调用方走默认。

    **非法值不报错而是退回默认**：这两个数是运行保障，不是用户输入校验点。
    一个写错的配置让整个 agent 起不来，比它悄悄用默认值更糟。

    **小数必须拒**：`int(1.5)` 会静默变成 `1`，于是 agent 走一轮就"轮次用尽"，
    而日志里看不出任何异常——这比直接报错难查得多。
    """
    if isinstance(value, bool):  # bool 是 int 的子类，但不是"次数"
        return None
    if isinstance(value, float):
        if not value.is_integer():
            return None
        number = int(value)
    else:
        try:
            number = int(str(value).strip())
        except (TypeError, ValueError):
            return None
    return number if number > 0 else None


def resolve(settings: Any = None) -> IterationBudgets:
    """按「配置 → 默认」解析两个运行预算。

    支持两种配置位置（都读，前者优先）：
    - `settings.turn_budgets = {"parent": 90, "child": 50}`
    - 平铺的 `settings.max_turns` / `settings.max_iterations`
    """
    parent, child = PARENT_BUDGET_STEPS, CHILD_BUDGET_STEPS
    parent_source = child_source = "default"

    nested = getattr(settings, "turn_budgets", None)
    if isinstance(nested, dict):
        found = _positive(nested.get("parent"))
        if found is not None:
            parent, parent_source = found, "settings.turn_budgets.parent"
        found = _positive(nested.get("child"))
        if found is not None:
            child, child_source = found, "settings.turn_budgets.child"

    if parent_source == "default":
        found = _positive(getattr(settings, "max_turns", None))
        if found is not None:
            parent, parent_source = found, "settings.max_turns"
    if child_source == "default":
        found = _positive(getattr(settings, "max_iterations", None))
        if found is not None:
            child, child_source = found, "settings.max_iterations"

    return IterationBudgets(
        parent_turns=parent,
        child_iterations=child,
        parent_source=parent_source,
        child_source=child_source,
    )


def describe(settings: Any = None) -> str:
    """给人看的说明。**要把"250 不是运行值"讲出来**，否则它会被当成上限。"""
    budgets = resolve(settings)
    return (
        f"父任务迭代上限：{budgets.parent_turns}（{budgets.parent_source}）\n"
        f"子任务迭代上限：{budgets.child_iterations}（{budgets.child_source}）\n"
        f"两者**互不扣减**：派 N 个子任务不会让父任务少走 N 轮——\n"
        f"  否则派得越多，父任务越早被自己的帮手饿死。\n"
        f"config 缺省兜底 {CONFIG_FALLBACK_ITERATIONS} **不是运行值**，只是没有配置时的占位；\n"
        f"  把它当上限会让所有配置覆盖看起来永远生效（因为 "
        f"{CONFIG_FALLBACK_ITERATIONS} > {budgets.parent_turns}）。\n"
        f"desktop 入口另有 {DESKTOP_TURNS} 档（桌面壳不读 CLI 的 {budgets.parent_turns}）。"
    )


__all__ = [
    "CONFIG_FALLBACK_ITERATIONS",
    "DESKTOP_TURNS",
    "IterationBudgets",
    "describe",
    "resolve",
]
