"""「单循环 + 传输适配器归一化多 provider」的守卫（MATRIX 2.1）。

## 这条守的是什么

Hermes 的 `agent/conversation_loop.py` 只有**一个**对话循环，所有 provider 都被
归一化成同一种传输形状——差别只在 `base_url` / 密钥 / 模型名，不在"走哪条代码路径"。

这件事看起来平淡，但它决定了三件实际后果：

1. **换 provider 不需要改循环**。如果每个 provider 一条分支，加一家就是改一次循环，
   而循环是最不该频繁动的地方。
2. **工具调用、重试、预算、中断这些语义只有一份实现**。多份实现必然漂移。
3. **回退链才有意义**。回退的前提是"下一站和这一站用同一套协议说话"，
   否则回退就得带协议翻译，复杂度指数上升。

所以这里断言的是：`app/graph/nodes.py` 里**只有一个** think/execute 循环，
且 `agent.get_model()` 造出来的客户端**只有一种类型**，provider 之间只差参数。
"""

from __future__ import annotations

import ast
from pathlib import Path

NODES = Path(__file__).resolve().parents[1] / "app" / "graph" / "nodes.py"
AGENT = Path(__file__).resolve().parents[1] / "app" / "agent.py"


def test_nodes_defines_exactly_one_think_and_one_execute():
    """一个 `think` + 一个 `execute`。多一份就是多一条协议路径。"""
    tree = ast.parse(NODES.read_text(encoding="utf-8"), filename=str(NODES))
    names = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef))
    ]
    assert names.count("think") == 1, f"think 出现了 {names.count('think')} 次"
    assert names.count("execute") == 1, f"execute 出现了 {names.count('execute')} 次"


def _client_calls(path: Path) -> list[str]:
    """文件里**真的调用**了哪些厂商客户端类（走 AST，不看注释与字符串）。

    为什么必须走 AST：`agent.py` 的注释里就写着「改之前 `ChatOpenAI(...)` 造一个」，
    纯文本匹配会把这句解释当成一次真实调用，测试就变成了"不许写注释"。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name in {"ChatOpenAI", "ChatAnthropic", "ChatBedrock", "ChatGoogleGenerativeAI"}:
                names.append(name)
    return names


def test_model_client_is_constructed_in_exactly_one_place():
    """归一化的落点：`providers._build_openai_chat` 是**唯一**造对话客户端的地方。

    判据是"主循环路径上只有一处构造"——`agent.py` 与 `graph/` 里一处都不能有，
    它们必须走 `providers` 的工厂。多一处构造就是多一条协议路径，而多路径必然漂移。
    （`summaries.py` 另造一个客户端是**会话摘要**用的，不在对话循环上，不在本条范围内。）
    """
    for path in (AGENT, NODES):
        calls = _client_calls(path)
        assert not calls, f"{path.name} 自己造了客户端（{calls}）；归一化要求它只调 providers 的工厂"
    providers = AGENT.parent / "providers.py"
    assert _client_calls(providers) == ["ChatOpenAI"], (
        "`providers.py` 里应当只有一处 `ChatOpenAI(...)`——那就是归一化点"
    )


def test_provider_differences_are_only_parameters():
    """provider 之间只差参数：工厂吃 `base_url`，且没有按 provider 选客户端类。

    正向证据：`_build_openai_chat` 的 kwargs 里必须有 `base_url`（provider 的差别就在这）。
    反向证据：主循环路径上不该出现任何其它厂商客户端类。
    """
    providers = (AGENT.parent / "providers.py").read_text(encoding="utf-8")
    tree = ast.parse(providers, filename="providers.py")
    factory = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "_build_openai_chat"
        ),
        None,
    )
    assert factory is not None, "找不到归一化工厂 `_build_openai_chat`"
    kwargs_keys: set[str] = set()
    for node in ast.walk(factory):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    kwargs_keys.add(key.value)
    assert "base_url" in kwargs_keys, "工厂必须显式传 base_url——provider 的差别就在这里"

    # 反向：主循环路径上不该出现其它厂商的客户端类
    for path in (AGENT, NODES):
        text = path.read_text(encoding="utf-8")
        for marker in ("ChatAnthropic(", "ChatBedrock(", "ChatGoogleGenerativeAI("):
            assert marker not in text, f"{path.name} 里出现了 {marker}——provider 走了不同代码路径"


def test_transport_registry_has_no_second_loop():
    """`providers.py` 的 `TRANSPORT_CLIENTS` 是**声明**，不是第二套循环。

    它描述"这家 provider 说哪种协议"，供 proxy 层翻译用；
    对话循环本身只走一种。这条断言防止有人把 proxy 的翻译层错接进主循环。
    """
    providers = (AGENT.parent / "providers.py").read_text(encoding="utf-8")
    assert "TRANSPORT_CLIENTS" in providers
    # 主循环文件不该引用 proxy 的协议翻译
    assert "openai_to_provider" not in AGENT.read_text(encoding="utf-8")
    assert "openai_to_provider" not in NODES.read_text(encoding="utf-8")
