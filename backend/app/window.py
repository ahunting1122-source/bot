"""上下文窗口自适应的工具结果阈值（MATRIX 2.10）。

## 为什么阈值必须随窗口缩放

`app/budget.py` 的历史默认值（单结果 100_000 字符、整轮 200_000 字符）本来就是
"大模型"的夹取**上限**，不是普适值。原样套到小窗口模型上是灾难：一个 8K 上下文
的模型被塞进一条 100_000 字符（≈25_000 token）的工具结果，请求当场 400。

Hermes 的做法（research 02 §5.1，`budget_config.py:139`）是**先按窗口比例算，
再两头夹取**：

    window_chars = context_length * 4
    per_result  = clamp(window_chars * 0.15,  8_000, 100_000)
    per_turn    = clamp(window_chars * 0.30, 16_000, 200_000)

于是三档行为都是对的：
* 200K+ token 的大模型：比例值超过上限 → 夹到历史默认值，**逐字节不变**（升级
  这个模块不会改变任何既有任务的正文长度）；
* 65K 的小模型：按比例缩小；
* 再小的模型：有地板，保证预览仍然可用（只缩不夹会让小模型连一条报错都看不全）。

这是 Hermes issue #23767 的修复。两头都必须有：只夹上限 = 小模型照样爆窗；
只按比例 = 小模型拿不到任何可读结果。

## 为什么入参是 token、出参是字符

窗口的天然单位是 token（`context_length` 就是 token），而预算层（`app/budget.py`）
全程按**字符**记账（Hermes 的 `_CHARS_PER_TOKEN = 4` 同口径）。所以换算是
4 字符/token，并且**只在这个模块里做一次**——换算口径散到各处，"到底按哪个数判"
就会变成第二个真相源，改一处忘一处。

## 数值来源

`per_result` 0.15 / `per_turn` 0.30（不是 0.5）、地板 8_000 / 16_000：全部照
research 02 §5.1 的实测常量。口径与 Hermes 一致比"看起来更合理"重要——这些数字
是踩过 #23767 之后调出来的，不是拍脑袋。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: 字符/token 的换算比。与 `app/budget.py`、`app/policies.py::estimate_tokens` 同口径。
CHARS_PER_TOKEN = 4

#: 单条工具结果可占窗口的比例与上下限（字符）。
PER_RESULT_FRACTION = 0.15
MIN_RESULT_CHARS = 8_000
MAX_RESULT_CHARS = 100_000

#: 一整轮（一次 assistant 回合里的全部工具结果）可占窗口的比例与上下限（字符）。
PER_TURN_FRACTION = 0.30
MIN_TURN_CHARS = 16_000
MAX_TURN_CHARS = 200_000

#: 窗口未知时的兜底。取 128K：这是当前主流模型常见的一档，算出来的
#: per_result ≈ 76.8K 字符（≈19K token）、per_turn ≈ 153.6K 字符，比历史默认值
#: （100K / 200K）更保守——"窗口不知道"时宁可少塞一点，也不要赌它够大。
DEFAULT_WINDOW_TOKENS = 128_000


@dataclass(frozen=True)
class Thresholds:
    """一个窗口尺寸下的三层阈值。frozen：一次算清，中途不会被谁改一半。"""

    window_tokens: int
    window_chars: int
    #: 单条工具结果允许进上下文的字符上限（第一层，`budget.cap_output` 用）。
    per_result_chars: int
    #: 一整轮工具结果的字符预算（第三层，`budget.TurnBudget` 用）。
    per_turn_chars: int

    def to_dict(self) -> dict[str, int]:
        return {
            "window_tokens": self.window_tokens,
            "window_chars": self.window_chars,
            "per_result_chars": self.per_result_chars,
            "per_turn_chars": self.per_turn_chars,
        }

    def budget_kwargs(self) -> dict[str, int]:
        """转成 `budget.BudgetConfig` 的构造参数。

        `budget.py` 的调用点不归本层改，所以这里给一个**现成的 kwargs**：
        接线时 `BudgetConfig(**thresholds(w).budget_kwargs())` 一行就够，
        不需要调用方知道字段名与夹取规则。
        """
        return {
            "default_result_size": self.per_result_chars,
            "turn_budget": self.per_turn_chars,
        }


def clamp(value: float, low: int, high: int) -> int:
    """夹取到 [low, high]，四舍五入到整数。`low > high` 时以 low 为准（不静默反转）。"""
    rounded = int(round(value))
    if rounded < low:
        return low
    if rounded > high:
        return high
    return rounded


def thresholds(window_tokens: int | float | None) -> Thresholds:
    """按窗口尺寸算三层阈值。纯函数，无 IO，可随意单测。

    边界处理（都是"配置写错"而不是"合法极端值"）：
    * `None` / 0 / 负数 → 按 `DEFAULT_WINDOW_TOKENS` 算。**不能**当零窗口算：
      那会把所有阈值压到地板，等于把"没配置"惩罚成"只能看 8K 结果"。
    * 极大值（如 10_000_000）→ 比例值被夹到上限，与 200K 模型逐字节相同。
    """
    try:
        tokens = int(window_tokens or 0)
    except (TypeError, ValueError):
        tokens = 0
    if tokens <= 0:
        tokens = DEFAULT_WINDOW_TOKENS
    window_chars = tokens * CHARS_PER_TOKEN
    return Thresholds(
        window_tokens=tokens,
        window_chars=window_chars,
        per_result_chars=clamp(
            window_chars * PER_RESULT_FRACTION, MIN_RESULT_CHARS, MAX_RESULT_CHARS
        ),
        per_turn_chars=clamp(window_chars * PER_TURN_FRACTION, MIN_TURN_CHARS, MAX_TURN_CHARS),
    )


def window_of(settings: Any, *, default: int = DEFAULT_WINDOW_TOKENS) -> int:
    """从 settings 读窗口尺寸。

    `Settings` 目前**没有** `context_length` 字段（research 09 §9 把"压缩阈值的
    窗口基数"列为未定项）。所以这里用 `getattr` 读：字段一旦加进 `app/config.py`
    （不归本层改）就自动生效，不需要再改这个模块。
    """
    try:
        value = int(getattr(settings, "context_length", 0) or 0)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def thresholds_for(settings: Any) -> Thresholds:
    """`window_of` + `thresholds` 的便捷组合：调用方只关心"当前这台的阈值"。"""
    return thresholds(window_of(settings))


__all__ = [
    "CHARS_PER_TOKEN",
    "DEFAULT_WINDOW_TOKENS",
    "Thresholds",
    "clamp",
    "thresholds",
    "thresholds_for",
    "window_of",
]
