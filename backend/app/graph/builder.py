"""链的组装。

START → prepare → think ⇄ gate → execute → digest → budget →（继续→ reflect → think / 收尾→finalize）
plan 只留 state 字段与节点签名（不额外占模型调用）；reflect 已接进图（budget 之后、think 之前）。
"""

from langgraph.graph import END, START, StateGraph

from app.graph.nodes import Nodes
from app.graph.state import AgentState


def think_route(state) -> str:
    return "gate" if state.get("calls") else "finalize"


def execute_route(state) -> str:
    """needs_user 等挂起载荷要回到 gate 去 interrupt，其余进 digest。"""
    return "gate" if state.get("waiting") else "digest"


def budget_route(state) -> str:
    if (state.get("budget") or {}).get("stop_reason"):
        return "finalize"
    if state.get("calls"):
        return "gate"
    # 继续下一轮之前先过自省：什么也不满足时它空转，行为不变。
    return "reflect"


def build_graph(runner):
    nodes = getattr(runner, "nodes", None) or Nodes(runner)
    graph = StateGraph(AgentState)
    graph.add_node("prepare", nodes.prepare)
    graph.add_node("think", nodes.think)
    graph.add_node("gate", nodes.gate)
    graph.add_node("execute", nodes.execute)
    graph.add_node("digest", nodes.digest)
    graph.add_node("reflect", nodes.reflect)
    graph.add_node("budget", nodes.budget)
    graph.add_node("finalize", nodes.finalize)

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "think")
    graph.add_conditional_edges("think", think_route, ["gate", "finalize"])
    graph.add_edge("gate", "execute")
    graph.add_conditional_edges("execute", execute_route, ["gate", "digest"])
    graph.add_edge("digest", "budget")
    graph.add_conditional_edges("budget", budget_route, ["gate", "reflect", "finalize"])
    graph.add_edge("reflect", "think")
    graph.add_edge("finalize", END)
    return graph
