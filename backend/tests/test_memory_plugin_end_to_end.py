"""13.4 的**端到端**验证：插件记忆真的进了系统提示。

## 为什么还差这一条

上一轮的 `test_memory_plugin_bridge.py` 验的是**桥本身**（`mirror` / `recall` 被调到、
失败被记账）。但 13.4 的虚报形状是「机制写好了、生产里没人用」——
**桥通了不等于装配层真的把它接进了提示**。

这条从**真实装配路径**走一遍：

    configure(router, plugin_providers=[...])
      → `context.build_context(store, task, settings)`
      → 系统提示里出现插件记忆

**只有这条能证明「用户记住了，模型也看得到」。**

## 顺带记一个事实

15 个内置 provider 里本机**只有 3 个可用**（`file` / `jsonl` / `holographic`），
另外 12 个需要网络或凭证。这个数字写进 `describe()`，免得「15 个 provider」被
读成「15 个都能用」。
"""

from __future__ import annotations

import pytest


class _PluginProvider:
    """最小插件 provider：`save` / `search` / `name`。"""

    name = "plugin-mem"

    def __init__(self) -> None:
        self.saved: list[tuple[str, str]] = []
        self.queries: list[str] = []

    def save(self, key, value):
        self.saved.append((key, value))

    def search(self, query, limit=5):
        self.queries.append(query)
        return ["用户在做水果拟人短剧，主角是柠檬"]


@pytest.fixture()
def bridge():
    from app import memory_plugin_bridge

    memory_plugin_bridge.reset()
    yield memory_plugin_bridge
    memory_plugin_bridge.reset()


async def _task(tmp_path):
    """造一个真实 Store + Task（`build_context` 需要）。"""
    from app.config import Settings
    from app.main import migrate
    from app.storage import Conversation, Store, Task

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(
        Task(conversation_id=conversation.id, goal="继续写水果拟人短剧的第三集")
    )
    return store, task, settings


async def test_plugin_memory_reaches_the_system_prompt(tmp_path, bridge):
    """**端到端**：插件记忆必须出现在 `build_context` 产出的提示里。

    这条红了就意味着「用户记住了，但模型看不到」——13.4 的读路径断了。
    """
    from app.context import build_context

    store, task, settings = await _task(tmp_path)
    provider = _PluginProvider()
    bridge.configure(object(), plugin_providers=[provider])
    try:
        built = await build_context(store, task, settings, touch=False)
        text = "\n".join(str(getattr(m, "content", "") or "") for m in built)

        assert provider.queries, "`build_context` 没拿任务目标去查插件 provider"
        assert provider.queries[0] == task.goal, (
            f"检索词不是任务目标：{provider.queries[0]!r}"
        )
        assert "柠檬" in text, (
            "插件 provider 检索到的内容没进系统提示——"
            "只写不读是半个功能，比没有更糟"
        )
    finally:
        await store.engine.dispose()


async def test_without_a_plugin_provider_the_prompt_is_unchanged(tmp_path, bridge):
    """没配插件 provider 时，提示里不能多出任何东西（零副作用）。"""
    from app.context import build_context

    store, task, settings = await _task(tmp_path)
    try:
        built = await build_context(store, task, settings, touch=False)
        text = "\n".join(str(getattr(m, "content", "") or "") for m in built)
        assert "插件记忆" not in text
    finally:
        await store.engine.dispose()


def test_describe_reports_how_many_builtin_providers_are_usable():
    """`describe()` 必须自曝「15 个里几个真能用」。

    **不写这个数字，「15 个 provider」会被读成「15 个都能用」**——
    而本机实际只有 3 个（`file` / `jsonl` / `holographic`），另外 12 个要网络或凭证。
    """
    from app.memory_plugin_bridge import describe

    info = describe()
    assert "builtin" in info, f"describe() 没报内置 provider 情况：{info}"
    builtin = info["builtin"]
    assert builtin["total"] >= 10, f"内置 provider 数量不对：{builtin}"
    # 可用数必须 <= 总数，且两个都要报出来（不是只报一个）
    assert 0 <= builtin["available"] <= builtin["total"]
