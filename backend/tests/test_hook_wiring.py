"""钩子**真的会触发**（MATRIX 13.4 的验收判据）。

## 为什么需要这条测试

`app/hooks.py` 有一整套注册表，`plugins` 能把插件的 hook 注册进去，13.4 也把
`register_hook` 标成 wired——但在此之前，生产代码里**没有任何地方 emit**：

    grep 'DEFAULT_HOOKS.emit|hooks.emit(' app/ app/graph/ app/cli/commands/
    → 只有 hooks.py 自己的模块级 emit 定义，零调用方

**插件注册的 hook 永远不会被调用。** 这和 13.8（dashboard 路由只在测试 app 上挂过）
是同一类假绿：机制真、测试真、生产里那条线是断的。

## 测试范围（如实标注）

这里**不跑整张图**，而是直接驱动 `Nodes.ask_model`——整图运行需要一整套
runner / checkpoint 前置状态，而这几个钩子的发射点就在 `ask_model` 与 `run_tool`
里面，直接驱动到那一层就能证明「发射点存在且真的发射」。

工具那条路（`run_tool`）要构造 tool_call 消息才能真跑到，成本更高，
所以用**源码检查**兜住：四个事件名必须都在 `nodes.py` 里。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from app.graph import nodes as nodes_module
from app.hooks import DEFAULT_HOOKS


class _Echo:
    """最小的假模型：只有 `ainvoke`（没有 `astream`），走非流式那条路。"""

    def bind_tools(self, *_args, **_kwargs):
        return self

    async def ainvoke(self, _messages):
        return AIMessage(content="钩子测试的回答。")


async def _noop_event(*_a, **_k):
    return None


async def _noop_save(*_a, **_k):
    return None


async def _noop_rows(*_a, **_k):
    return []


async def _noop_execute(*_a, **_k):
    return {}


@pytest.fixture()
def clean_hooks():
    """每条用例前后清空默认注册表——它是**进程级单例**，不清会污染别的用例。"""
    DEFAULT_HOOKS.clear()
    yield DEFAULT_HOOKS
    DEFAULT_HOOKS.clear()


def _nodes(tmp_path):
    """造一个 `Nodes`，只填 `ask_model` 真正用到的那几个依赖。"""
    from app.config import Settings

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    store = SimpleNamespace(event=_noop_event, save=_noop_save, rows=_noop_rows)
    policies = SimpleNamespace(exposer=SimpleNamespace(expose=lambda specs: specs))
    # `Nodes.__init__(runner)` 只收 runner：store / settings / registry / policies
    # 都是**从 runner 上取的属性**（见 nodes.py 的 property），所以替身要把这些
    # 一并挂上，而不是传给构造参数。
    runner = SimpleNamespace(
        settings=settings,
        store=store,
        registry=SimpleNamespace(specs=lambda: [], execute=_noop_execute),
        policies=policies,
        partial={},
        _token_usage={},
        get_model=lambda: _Echo(),
        cancel=lambda _tid: None,
    )
    return nodes_module.Nodes(runner)


async def test_ask_model_emits_pre_and_post_llm_call(tmp_path, clean_hooks):
    """`ask_model` 必须真的发射 `pre_llm_call` / `post_llm_call`。

    这条在 `nodes.py` 没有发射点时会失败（hook 永远收不到）——所以它真的能红。
    """
    seen: list[tuple[str, dict]] = []
    clean_hooks.register("pre_llm_call", lambda p: seen.append(("pre", dict(p))))
    clean_hooks.register("post_llm_call", lambda p: seen.append(("post", dict(p))))

    nodes = _nodes(tmp_path)
    task = SimpleNamespace(id="task-hook-1", goal="随便聊聊")
    identity = {"conversation_id": "c1", "message_id": "m1", "response_id": "r1"}

    await nodes.ask_model(_Echo(), [], task, identity)

    kinds = [name for name, _ in seen]
    assert "pre" in kinds, f"`pre_llm_call` 没有被触发（收到的：{kinds}）"
    assert "post" in kinds, f"`post_llm_call` 没有被触发（收到的：{kinds}）"
    # payload 里要能对上是哪个任务——只有名字没有 id 的钩子没法排障
    assert any(p.get("task_id") == task.id for name, p in seen if name == "pre")


def test_all_four_hook_emit_points_exist_in_the_graph():
    """源码层：四个事件名必须都在 `nodes.py` 里。

    工具那条路（`run_tool`）要构造 tool_call 消息才能真跑到，成本更高；
    而「发射点存在」本身就是这一层要守的东西。
    """
    source = Path(nodes_module.__file__).read_text(encoding="utf-8")
    for event in ("pre_tool_call", "post_tool_call", "pre_llm_call", "post_llm_call"):
        assert f'"{event}"' in source, f"`nodes.py` 里没有发射 `{event}`"


async def test_hook_errors_do_not_break_the_model_call(tmp_path, clean_hooks):
    """钩子抛异常不能拖垮模型调用——钩子是**旁路**，旁路坏了不该让主循环死。"""

    def _boom(_payload):
        raise RuntimeError("钩子自己炸了")

    clean_hooks.register("pre_llm_call", _boom)

    nodes = _nodes(tmp_path)
    task = SimpleNamespace(id="task-hook-2", goal="随便聊聊")
    identity = {"conversation_id": "c1", "message_id": "m1", "response_id": "r1"}

    # 钩子炸了，但 ask_model 必须照常返回回答
    answer = await nodes.ask_model(_Echo(), [], task, identity)
    assert answer is not None
