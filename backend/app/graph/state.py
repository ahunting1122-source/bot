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
