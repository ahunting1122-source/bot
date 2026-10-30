"""注册表级集成测试：新加的工具必须**通过注册表**能调到（`5.14`）。

## 为什么单测不够 —— 第 113 轮的真实教训

`tests/test_tool_preview.py` 直接调 `preview.execute(...)`，**全绿**。
而通过真实注册表调时炸了：`TypeError: object dict can't be used in 'await' expression` ——
**因为 `ToolRegistry.execute` 是 `await self.external[name](...)`，而那个执行器是同步的。**

**单测验的是「这个函数返回什么」，不是「注册表能不能调它」。**

## 这个测试守什么

对每个**新加的工具**：走 `registry.execute()`（真实分发路径），
用唯一 `operation_id`，断言它**不抛结构性异常**。

**「没配置」是可接受的返回**（`status=error` + 一句可照做的话）；
**`TypeError` / `AttributeError` 不可接受** —— 那说明接线方式错了。

**为什么只测这几个工具、不全量扫**：全量扫会用空参调 `browser_*` / `terminal`，
**那会真的开浏览器、跑 shell** —— 我试过，挂住了。**集成测试不能有副作用。**
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.main import create_app

#: 我新加/改过的工具 + 一个安全的空参（它们都会走到「没配置」分支，不碰网络）。
NEW_TOOLS: tuple[tuple[str, dict], ...] = (
    ("ha_list_entities", {}),
    ("ha_get_state", {"entity_id": "light.x"}),
    ("ha_list_services", {}),
    ("ha_call_service", {"domain": "light", "service": "turn_on"}),
    ("discord", {"action": "read", "channel_id": "c1"}),
    ("discord_admin", {"action": "list_channels", "guild_id": "g1"}),
    ("feishu_doc_read", {"doc_token": "doccn1"}),
    ("desktop_preview", {"target": "https://example.com"}),
    ("open_preview", {"target": "https://example.com"}),
    ("read_preview", {}),
    ("annotate_preview", {"note": "x"}),
    ("close_preview", {}),
    ("context_usage", {}),
)


@pytest.fixture(scope="module")
def live_app(tmp_path_factory):
    root = tmp_path_factory.mktemp("reg")
    settings = Settings(data_dir=root / "d", workspace=root / "w")
    settings.prepare()
    app = create_app(settings=settings)
    with TestClient(app):
        yield app


@pytest.mark.parametrize(("name", "args"), NEW_TOOLS, ids=[n for n, _ in NEW_TOOLS])
async def test_tool_dispatches_through_the_registry(live_app, name, args):
    """**通过真实注册表调一次** —— 这是唯一能发现「同步执行器」的路径。"""
    registry = live_app.state.registry
    assert name in registry.tools, f"{name} 没注册进应用"

    # **每个调用要用唯一 operation_id** —— 复用会被幂等短路，你测的就是缓存。
    result = await registry.execute("itest", f"itest-{name}", name, args)

    assert isinstance(result, dict), f"{name} 返回了 {type(result).__name__}"
    assert result.get("status") in ("ok", "error"), result
    if result.get("status") == "error":
        # 「没配置」是可接受的；**空错误消息不可接受**（模型不知道该做什么）。
        assert result.get("error"), f"{name} 报错但没给原因"


def test_every_new_tool_is_registered_and_has_a_toolset(live_app):
    """注册了、也有 toolset 归属 —— 没有归属的可能被 toolset 过滤掉。"""
    from app import toolsets

    registry = live_app.state.registry
    for name, _args in NEW_TOOLS:
        assert name in registry.tools, f"{name} 没注册"
        assert toolsets.toolset_of(name), f"{name} 没有 toolset 归属"
