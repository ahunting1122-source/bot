"""`app/tool_home.py` 的测试（`5.14` 的 `home` 域 4 条）。

## 为什么用 `httpx.MockTransport` 而不是起本地服务器

这 4 个工具的契约是「按 HA REST 的形状发请求、按形状解析响应」。
**用 `MockTransport` 能精确断言「发出去的请求长什么样」**（路径、方法、`Authorization` 头、
JSON 体），这比起一个通用 echo server 更能咬住错误。

**判据：验「需要凭证的工具」时，验的是「请求构造对不对」，
而不是「对方的服务好不好」—— 把这两件事混在一起，就会把「没凭证」误当成「做不了」。**
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.tool_home import HOME_TOOLS, HomeAssistantConnector

BASE = "http://ha.test:8123"
TOKEN = "long-lived-token"


class FakeSettings:
    def __init__(self, url: str = BASE, token: str = TOKEN) -> None:
        self.home_assistant_url = url
        self.home_assistant_token = _Secret(token)


class _Secret:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


def _connector(handler, **kwargs) -> HomeAssistantConnector:
    return HomeAssistantConnector(FakeSettings(**kwargs), transport=httpx.MockTransport(handler))


def test_four_tools_are_declared():
    assert [t.name for t in HOME_TOOLS] == [
        "ha_list_entities",
        "ha_get_state",
        "ha_list_services",
        "ha_call_service",
    ]


async def test_list_entities_sends_the_token_and_filters_by_domain():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json=[
                {"entity_id": "light.kitchen", "state": "on", "attributes": {"friendly_name": "厨房灯"}},
                {"entity_id": "switch.fan", "state": "off", "attributes": {}},
            ],
        )

    result = await _connector(handler).execute("ha_list_entities", {"domain": "light"}, "t", "o")

    assert seen["url"] == f"{BASE}/api/states"
    assert seen["auth"] == f"Bearer {TOKEN}", "令牌没有按 HA 要求发出去"
    assert result["status"] == "ok"
    assert result["count"] == 1, "按域过滤没生效"
    assert result["entities"][0]["entity_id"] == "light.kitchen"
    assert result["entities"][0]["name"] == "厨房灯"


async def test_list_entities_truncates_and_says_so():
    """**截断要说出来** —— 静默截断会让模型以为「就这么多」。"""
    many = [{"entity_id": f"light.l{i}", "state": "on", "attributes": {}} for i in range(10)]
    result = await _connector(lambda r: httpx.Response(200, json=many)).execute(
        "ha_list_entities", {"limit": 3}, "t", "o"
    )
    assert len(result["entities"]) == 3
    assert result["truncated"] is True
    assert result["count"] == 10, "count 应是总数，不是返回条数"


async def test_get_state_returns_attributes():
    body = {
        "entity_id": "light.kitchen",
        "state": "on",
        "attributes": {"brightness": 128},
        "last_changed": "2026-01-01T00:00:00Z",
    }
    result = await _connector(lambda r: httpx.Response(200, json=body)).execute(
        "ha_get_state", {"entity_id": "light.kitchen"}, "t", "o"
    )
    assert result["status"] == "ok"
    assert result["state"] == "on"
    assert result["attributes"]["brightness"] == 128


async def test_call_service_posts_the_payload():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content or b"{}")
        return httpx.Response(200, json=[{"entity_id": "light.kitchen"}])

    result = await _connector(handler).execute(
        "ha_call_service",
        {"domain": "light", "service": "turn_on", "entity_id": "light.kitchen", "data": {"brightness": 200}},
        "t",
        "o",
    )

    assert seen["method"] == "POST"
    assert seen["url"] == f"{BASE}/api/services/light/turn_on"
    assert seen["body"] == {"brightness": 200, "entity_id": "light.kitchen"}
    assert result["status"] == "ok"


async def test_list_services_groups_by_domain():
    body = [
        {"domain": "light", "services": {"turn_on": {}, "turn_off": {}}},
        {"domain": "switch", "services": {"toggle": {}}},
    ]
    result = await _connector(lambda r: httpx.Response(200, json=body)).execute(
        "ha_list_services", {}, "t", "o"
    )
    assert result["count"] == 2
    assert result["services"][0]["services"] == ["turn_off", "turn_on"]


async def test_unconfigured_url_returns_actionable_error_not_a_crash():
    """**没配就返回一条能照着做的错误** —— 模型看得懂「去设置里配 X」，看不懂 traceback。"""
    result = await _connector(lambda r: httpx.Response(200, json=[]), url="").execute(
        "ha_list_entities", {}, "t", "o"
    )
    assert result["status"] == "error"
    assert "home_assistant_url" in result["error"]


async def test_unconfigured_token_returns_actionable_error():
    result = await _connector(lambda r: httpx.Response(200, json=[]), token="").execute(
        "ha_list_entities", {}, "t", "o"
    )
    assert result["status"] == "error"
    assert "home_assistant_token" in result["error"]


async def test_http_error_becomes_a_readable_error():
    result = await _connector(lambda r: httpx.Response(401, text="unauthorized")).execute(
        "ha_get_state", {"entity_id": "light.x"}, "t", "o"
    )
    assert result["status"] == "error"
    assert "401" in result["error"]


async def test_connection_failure_becomes_a_readable_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    result = await _connector(boom).execute("ha_list_entities", {}, "t", "o")
    assert result["status"] == "error"
    assert "连不上 HA" in result["error"]
