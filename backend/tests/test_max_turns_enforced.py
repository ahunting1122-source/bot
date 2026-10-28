"""轮次上限**真的生效**（MATRIX 2.2 的验收判据）。

## 为什么需要这条

第 17 轮审计查出 2.2 是虚报：`app/turn_budgets.py` 能算出父 90 / 子 50，
`orbit chat --max-turns` 也把值写进 `state`，**但图里没有任何地方读它**
（`grep max_turns app/graph/ app/agent.py` 零命中）。所以实际上**没有轮次上限**——
一个不收敛的工具循环会一直跑。

`max_actions` 拦不住这种循环：它数的是**工具调用**，而「每轮只思考、不调工具」的
循环 `actions` 永远是 0。所以必须另有一个**循环次数**计数器。

判据三层：
1. `budget.start()` 里真的有 `max_turns`；
2. `check()` 在轮次到顶时返回 `max_turns`；
3. `think` 每轮真的把 `turns` 加一。
"""

from __future__ import annotations

from app.config import Settings
from app.policies import DefaultBudgetPolicy


def test_budget_start_carries_max_turns(tmp_path):
    """`budget.start()` 必须把轮次上限放进 budget——不然 `check()` 无处可读。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    budget = DefaultBudgetPolicy().start(settings)
    assert "max_turns" in budget, "budget 里没有 max_turns，`check()` 永远读不到"
    assert int(budget["max_turns"]) > 0


def test_check_stops_on_max_turns(tmp_path):
    """轮次到顶时 `check()` 必须返回 `max_turns`。

    这条在接线前会失败（`check()` 只认 `max_actions` / `max_tokens`）——所以它真的能红。
    """
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    policy = DefaultBudgetPolicy()
    budget = policy.start(settings)
    limit = int(budget["max_turns"])

    state = {"actions": 0, "turns": limit - 1, "budget": budget}
    assert policy.check(state) != "max_turns", "还没到顶就不该停"

    state["turns"] = limit
    assert policy.check(state) == "max_turns", f"到顶（{limit}）了却没停"


def test_max_turns_binds_even_when_no_tool_is_called(tmp_path):
    """**关键用例**：一个「只思考、不调工具」的循环，`actions` 恒为 0，
    只靠 `max_actions` 拦不住——必须靠 `turns`。

    这正是审计指出的漏洞形状：动作数上限对"不调工具的空转循环"完全无效。
    """
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    policy = DefaultBudgetPolicy()
    budget = policy.start(settings)
    limit = int(budget["max_turns"])

    # actions 一直是 0（没调过工具），但轮次已经到顶
    state = {"actions": 0, "turns": limit, "budget": budget}
    assert policy.check(state) == "max_turns"
    # 反证：如果把轮次拿掉，`max_actions` 确实拦不住它
    assert policy.check({"actions": 0, "budget": budget}) == ""


def test_think_increments_turns():
    """源码层：`think` 必须把 `turns` 加一，否则计数器永远是 0。

    行为测试要跑整张图（成本高且依赖 checkpoint 状态），而「有没有那一行」
    是这一层要守的东西。
    """
    import pathlib

    from app.graph import nodes as nodes_module

    source = pathlib.Path(nodes_module.__file__).read_text(encoding="utf-8")
    assert '"turns": int(state.get("turns") or 0) + 1' in source, (
        "`think` 没有把 `turns` 加一——轮次上限永远读不到真实轮次"
    )
