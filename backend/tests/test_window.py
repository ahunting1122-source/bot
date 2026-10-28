"""上下文窗口自适应阈值的边界夹取（MATRIX 2.10）。

数值口径照 research 02 §5.1（`budget_config.py:139`）：

    per_result = clamp(window_chars * 0.15,  8_000, 100_000)
    per_turn   = clamp(window_chars * 0.30, 16_000, 200_000)

**两头都要有**才是修复（Hermes issue #23767）：只夹上限 = 小模型照样爆窗；
只按比例 = 小模型连一条报错都看不全。
"""

from __future__ import annotations

from app.config import Settings
from app.window import (
    DEFAULT_WINDOW_TOKENS,
    MAX_RESULT_CHARS,
    MAX_TURN_CHARS,
    MIN_RESULT_CHARS,
    MIN_TURN_CHARS,
    clamp,
    thresholds,
    thresholds_for,
    window_of,
)


def test_small_window_falls_back_to_the_floor_not_to_zero():
    """8K 窗口：比例值（4 800 / 9 600 字符）低于地板，必须抬到地板。"""
    limits = thresholds(8_000)
    assert limits.window_chars == 32_000
    assert limits.per_result_chars == MIN_RESULT_CHARS
    assert limits.per_turn_chars == MIN_TURN_CHARS


def test_large_window_is_capped_at_the_historical_defaults():
    """200K+ token 的模型：比例值超过上限 → 夹住，与历史默认值逐字节一致。"""
    for tokens in (200_000, 400_000, 1_000_000):
        limits = thresholds(tokens)
        assert limits.per_result_chars == MAX_RESULT_CHARS
        assert limits.per_turn_chars == MAX_TURN_CHARS


def test_mid_sized_window_scales_by_ratio():
    """32K 窗口：比例值在地板与上限之间，必须**原样按比例**算出来。"""
    limits = thresholds(32_000)
    assert limits.window_chars == 128_000
    assert limits.per_result_chars == int(128_000 * 0.15)
    assert limits.per_turn_chars == int(128_000 * 0.30)


def test_extreme_and_invalid_windows_degrade_to_the_default():
    """None / 0 / 负数 / 垃圾值 = "没配置"，按默认窗口算，**不能**当零窗口。"""
    default = thresholds(DEFAULT_WINDOW_TOKENS)
    for value in (None, 0, -1, -999_999):
        assert thresholds(value) == default


def test_per_result_never_exceeds_per_turn():
    """不变量：单条结果的上限不能比整轮预算还大，否则第一层形同虚设。"""
    for tokens in (1, 8_000, 32_000, 128_000, 200_000, 10_000_000):
        limits = thresholds(tokens)
        assert limits.per_result_chars <= limits.per_turn_chars


def test_clamp_holds_both_ends():
    assert clamp(1, 10, 20) == 10
    assert clamp(15.4, 10, 20) == 15
    assert clamp(99, 10, 20) == 20


def test_budget_kwargs_uses_the_field_names_budget_expects():
    """接线口：`BudgetConfig(**limits.budget_kwargs())` 必须能直接构造。"""
    from app.budget import BudgetConfig

    limits = thresholds(32_000)
    config = BudgetConfig(**limits.budget_kwargs())
    assert config.default_result_size == limits.per_result_chars
    assert config.turn_budget == limits.per_turn_chars


def test_window_of_reads_context_length_when_the_setting_exists(tmp_path):
    """`Settings` 还没有 `context_length` 字段：有就按它算，没有就用默认值。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    assert window_of(settings) == DEFAULT_WINDOW_TOKENS
    assert thresholds_for(settings) == thresholds(DEFAULT_WINDOW_TOKENS)
    # 字段一旦加进 config.py，这个模块不需要改。
    object.__setattr__(settings, "context_length", 32_000)
    assert window_of(settings) == 32_000
    assert thresholds_for(settings).per_result_chars == int(128_000 * 0.15)
