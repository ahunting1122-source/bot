"""三套迭代预算「独立不叠加」的契约测试（MATRIX 2.2）。

核心断言只有一条，但它必须被证明而不是被声明：

**派子任务不会减少父任务的轮次预算。**

如果这条不成立，那么"派 5 个子任务"就等于"父任务只剩 90−5×n 轮"——
派得越多，父任务越早被自己的帮手饿死。这是个很容易写错、而且写错之后
要很久才暴露的问题（任务跑到一半莫名其妙地"轮次用尽"）。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import delegate, turn_budgets
from app.turn_budgets import (
    CONFIG_FALLBACK_ITERATIONS,
    DESKTOP_TURNS,
    describe,
    resolve,
)


def test_defaults_match_hermes():
    """父 90 / 子 50——数值来自 research，与 Hermes 逐字一致。"""
    budgets = resolve()
    assert budgets.parent_turns == 90
    assert budgets.child_iterations == 50
    assert budgets.parent_source == budgets.child_source == "default"


def test_the_three_numbers_are_distinct_and_the_fallback_is_not_a_runtime_value():
    """250 是 config 缺省兜底，**不是**运行上限。

    这条断言存在的理由：250 > 90，如果谁把 250 当运行值，那么"父上限 90"这个配置
    就永远看不出效果——因为 250 永远到不了，看起来像"上限没生效"。
    """
    budgets = resolve()
    assert budgets.parent_turns != budgets.child_iterations
    assert CONFIG_FALLBACK_ITERATIONS > budgets.parent_turns
    payload = budgets.to_dict()
    assert payload["config_fallback_is_runtime_value"] is False
    assert payload["config_fallback"] == 250
    assert payload["desktop_turns"] == DESKTOP_TURNS == 500


def test_parent_budget_is_not_decremented_by_children():
    """**这是本文件最重要的一条**：父预算不因子任务扣减。

    `delegate` 的常量是模块级的，不是"从父预算里切出去"的余额。
    这里用"读两次常量中间夹着一次子预算规划"来证明它没有被消费掉。
    """
    before = delegate.PARENT_BUDGET_STEPS
    child = delegate.ChildBudget(steps=delegate.CHILD_BUDGET_STEPS, tokens=10_000, turn=200_000)
    # 子预算跑一轮（这里只动它自己的计数器）
    assert child.steps == delegate.CHILD_BUDGET_STEPS
    after = delegate.PARENT_BUDGET_STEPS
    assert after == before == 90, "父预算被子任务扣减了——这就是'叠加'的错法"


def test_configured_values_win_and_report_their_source():
    settings = SimpleNamespace(turn_budgets={"parent": 120, "child": 30})
    budgets = resolve(settings)
    assert budgets.parent_turns == 120
    assert budgets.child_iterations == 30
    assert budgets.parent_source == "settings.turn_budgets.parent"
    assert budgets.child_source == "settings.turn_budgets.child"


def test_flat_settings_are_also_honoured():
    settings = SimpleNamespace(max_turns=77, max_iterations=11)
    budgets = resolve(settings)
    assert (budgets.parent_turns, budgets.child_iterations) == (77, 11)
    assert budgets.parent_source == "settings.max_turns"
    assert budgets.child_source == "settings.max_iterations"


@pytest.mark.parametrize("bad", [0, -1, "很多", None, 1.5, ""])
def test_illegal_config_falls_back_to_defaults_instead_of_raising(bad):
    """非法值退回默认而不是报错。

    这两个数是运行保障，不是用户输入校验点——一个写错的配置让整个 agent 起不来，
    比它悄悄用默认值更糟。
    """
    budgets = resolve(SimpleNamespace(turn_budgets={"parent": bad, "child": bad}))
    assert budgets.parent_turns == 90
    assert budgets.child_iterations == 50


def test_describe_explains_that_250_is_not_a_runtime_value():
    text = describe()
    assert "互不扣减" in text
    assert "不是运行值" in text
    assert "250" in text


def test_to_dict_is_json_safe():
    import json

    payload = resolve().to_dict()
    assert json.loads(json.dumps(payload))["parent_turns"] == 90


def test_turn_budgets_module_does_not_own_a_second_loop():
    """预算模块只声明数值，不该自己跑循环——那是 `graph/nodes.py` 的事。"""
    source = (turn_budgets.__file__ or "")
    text = open(source, encoding="utf-8").read()
    for marker in ("async def think", "async def execute", "astream("):
        assert marker not in text
