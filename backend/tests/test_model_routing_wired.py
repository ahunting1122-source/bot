"""按模型名路由真的进了生产路径（MATRIX 4.3 的验收判据）。

## 之前缺的是什么

`app/model_router.py::route()` 写好了、单测也过，**有 1 个调用方**
（`model_extra.py` 的 `orbit model route` **CLI 诊断**命令——所以它有用，不能删）。
**但运行时不拿它做 per-model 路由**：`agent.py` 选客户端读的是 per-provider 静态
`profile.transport`，与模型名无关。

后果：`model = "deepseek-chat"` 配一个空的 `model_provider` 时，`resolve_provider`
只能靠 base_url 反查或退回 `custom`——**模型名里那条最可靠的信息被丢掉了**。

## 判据（含一条「必须听用户的」）

1. **用户没表达 provider 意图**（`model_provider` 与 `model_base_url` 都空）→ 让模型名说话；
2. **用户说了就听用户的**：`model_provider=openai` 时，哪怕模型名在别家表里，
   **也必须用 openai**；
3. 用户只给了 `model_base_url`（自建端点）→ 不路由，走原有解析。

**第 2 条是这条功能最容易做错的地方**：`route()` 的优先级是「显式 provider
**且该家模型表里有这个模型**」，所以 `model_provider=openai` + 一个在别家表里的
模型名时它会**反查成别家**。直接接上去就成了「用户明说了却被悄悄改掉」。
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def runner_factory(tmp_path):
    """按需造 `AgentRunner`（只为拿 `_primary_profile()`，不跑任务）。"""
    from app.agent import AgentRunner
    from app.config import Settings
    from app.storage import Store
    from app.tools import ToolRegistry

    created = []

    def _make(**kwargs):
        settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files", **kwargs)
        settings.prepare()
        store = Store(settings.data_dir / "app.sqlite")
        runner = AgentRunner(settings, store, ToolRegistry(settings, store))
        created.append(store)
        return runner

    yield _make
    # 同步清理：这些 store 从没起过任务，`asyncio.run` 起个新循环 dispose 就够。
    # **不用 `asyncio.get_event_loop()`**：3.12 起它没有当前循环时只给弃用警告，
    # 而且拿到的循环可能已被别的测试关掉。
    import asyncio

    for store in created:
        asyncio.run(store.engine.dispose())


def test_model_name_decides_the_provider_when_the_user_says_nothing(
    runner_factory, monkeypatch
):
    """**核心判据**：没有 provider 提示时，模型名里的 provider 信息要用上。

    **必须证明 `route()` 真的被调到**——第一版这条测试是**假阳性**：
    `model_base_url` 会**由模型名自动派生**（`model="deepseek-chat"` →
    `https://api.deepseek.com`），而当时的路由条件是「两个都为空」，
    **在默认配置下永不成立**。于是 `_primary_profile()` 返回 `deepseek`
    靠的是 `resolve_provider` 的 **base_url 反查**，与路由无关。

    **共同特征（与 13.8 / `register_hook` / 2.2 / 4.4 同族的新亚型）**：
    接线正确、条件正确，**但条件在默认配置下永不成立**；
    而测试因为走了另一条等价路径所以是绿的。
    """
    from app import model_router

    seen: list[str] = []
    original = model_router.route

    def _spy(name, settings=None, **kwargs):
        seen.append(name)
        return original(name, settings, **kwargs)

    monkeypatch.setattr(model_router, "route", _spy)

    runner = runner_factory(model="deepseek-chat")
    profile = runner._primary_profile()

    assert seen, (
        "`route()` 一次都没被调用——路由分支没走到。"
        "`deepseek` 这个结果可能是 `resolve_provider` 的 base_url 反查给的，"
        "那证明不了路由接线（第一版就栽在这里）"
    )
    assert profile.id == "deepseek"


def test_explicit_provider_always_wins(runner_factory, monkeypatch):
    """**必须「听用户的」**：显式 `model_provider` 不许被模型名反查覆盖。

    `route()` 自己的优先级是「显式 provider **且该家模型表里有这个模型**」，
    所以 `model_provider=openai` + `deepseek-chat` 时它会反查成 deepseek。
    **那种情况必须根本不问路由**——用户明说了却被悄悄改掉，比不路由糟得多。
    """
    from app import model_router

    seen: list[str] = []
    monkeypatch.setattr(
        model_router, "route", lambda *a, **k: seen.append(a) or model_router.route(*a, **k)
    )

    runner = runner_factory(model="deepseek-chat", model_provider="openai")
    profile = runner._primary_profile()

    assert profile.id == "openai", (
        f"用户显式指定了 openai，实际却用了 {profile.id!r}——"
        "路由把用户的显式选择覆盖掉了"
    )
    assert not seen, "显式 provider 时不该问路由（问了就可能被反查覆盖）"


def test_a_custom_base_url_is_not_rerouted(runner_factory, monkeypatch):
    """用户给了自建 `model_base_url` → 不路由，走原有解析到 `custom`。

    **断言必须是 `== "custom"`**：原来的 `!= "deepseek"` 太松——`openai` /
    `anthropic` 也都满足，它证明不了「走了 resolve_provider」。
    """
    from app import model_router

    seen: list[str] = []
    original = model_router.route

    def _spy(name, settings=None, **kwargs):
        seen.append(name)
        return original(name, settings, **kwargs)

    monkeypatch.setattr(model_router, "route", _spy)

    runner = runner_factory(model="deepseek-chat", model_base_url="http://127.0.0.1:9/v1")
    profile = runner._primary_profile()

    assert profile.id == "custom", (
        f"自建端点应落到 custom，实际 {profile.id!r}——请求会打到别家的端点上"
    )
    # 路由**可以**被问到（问了才知道 base_url 与那家默认不一致），
    # 但结果必须被丢掉。这里只断言最终落到 custom 即可。


def test_an_unknown_model_name_falls_back_to_the_old_resolution(runner_factory):
    """模型名路由不出来时，行为与接线前一致（不猜、不崩）。"""
    runner = runner_factory(model="some-totally-unknown-model")
    profile = runner._primary_profile()
    assert profile is not None
    assert profile.id, "路由落空时必须退回原有解析，不能返回空 profile"


def test_the_router_is_reachable_from_production_code():
    """防回归：`agent.py` 里必须真的调 `model_router.route`。

    4.3 的虚报形状是「机制写好了、生产里零调用方」——
    只验 `route()` 能用是不够的，必须钉住调用点。
    """
    import pathlib

    from app import agent as agent_module

    source = pathlib.Path(agent_module.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert "model_router.route(" in code, (
        "`agent.py` 不再调 `model_router.route` 了——per-model 路由又变成死机制"
    )
