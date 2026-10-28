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


def test_budget_node_increments_turns():
    """源码层：**`budget` 节点**必须把 `turns` 加一，否则计数器永远是 0。

    **为什么不是 `think`**：图的边是 `think → gate → execute → digest → budget → gate …`，
    一次 `think` 之后可以**多次** `gate→execute→digest→budget` 循环。把计数放在
    `think` 里会漏掉那些不经过 think 的循环——而那恰恰是最容易失控的形状
    （模型反复要求同一个工具）。`budget` 才是每轮循环必过的节点。

    行为证据在 `tests/test_max_turns_end_to_end.py`（真跑整图，跑飞必须停在
    `max_turns`）；这条只守「那一行在不在」。
    """
    import pathlib

    from app.graph import nodes as nodes_module

    source = pathlib.Path(nodes_module.__file__).read_text(encoding="utf-8")
    assert 'turns = int(state.get("turns") or 0) + 1' in source, (
        "`budget` 节点没有把 `turns` 加一——轮次上限永远读不到真实轮次"
    )
    # 状态 schema 必须声明 `turns`：LangGraph 的 state 是 TypedDict，
    # 未声明的键会被**静默丢弃**——这是第一版修完仍然不生效的根因之一。
    from app.graph.state import AgentState

    assert "turns" in AgentState.__annotations__, (
        "`AgentState` 没声明 `turns`——LangGraph 会静默丢弃它，计数器永远回到 0"
    )
    # settings 也必须真有这个字段：pydantic 对未声明的 kwarg 是**静默丢弃**，
    # 不报错也不生效。这是第二版修完仍然不生效的根因。
    from app.config import Settings

    assert "max_turns" in Settings.model_fields, (
        "`Settings` 没有 `max_turns` 字段——`--max-turns` 会被静默丢弃"
    )
