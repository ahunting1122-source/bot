"""AgentState 与默认值、旧 checkpoint 兼容读取。

新字段一律带默认值：旧 checkpoint 恢复时缺字段按默认补，读取处统一走 `.get()`。
"""

from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages


class AgentState(TypedDict, total=False):
    # 已有语义，不变
    task_id: str
    messages: Annotated[list[AnyMessage], add_messages]
    calls: list[dict]
    actions: int
    answer: dict
    waiting: dict

    # 进度与预算
    plan: dict
    budget: dict
    refs: dict
    #: 已走过的 think→execute 循环次数（MATRIX 2.2）。
    #:
    #: **必须在这里声明**：LangGraph 的 state 是 TypedDict，`think` 返回的 `updates`
    #: 里出现未声明的键会被**静默丢弃**。所以「在 think 里 `turns + 1`」这件事，
    #: 不加这一行就等于没写——轮次计数器永远是 0，上限永远不触发。
    #: 这正是 2.2 第一版修完仍然不生效的原因（端到端用例抓到的）。
    #:
    #: 与 `actions` 的区别：`actions` 数**工具调用**，`turns` 数**循环次数**。
    #: 一个「每轮只思考、不调工具」的空转循环 `actions` 恒为 0，只靠动作上限拦不住。
    turns: int

    # 可观测与预留
    context_meta: dict
    reflections: list

    # 节点间传递
    last_result: dict
    final: dict


def with_defaults(values: dict | None) -> dict[str, Any]:
    state = defaults()
    state.update({key: value for key, value in (values or {}).items() if key in state})
    return state


def defaults() -> dict[str, Any]:
    return {
        "messages": [],
        "calls": [],
        "actions": 0,
        "turns": 0,
        "answer": {},
        "waiting": {},
        "plan": {},
        "budget": {},
        "refs": {},
        "context_meta": {},
        "reflections": [],
        "last_result": {},
        "final": {},
    }
