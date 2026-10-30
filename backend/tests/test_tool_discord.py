"""`app/tool_discord.py` 的测试（`5.14` 的 `collab` 域前两条）。

## 用 `httpx.MockTransport` 断言「请求形状」

Discord 的契约是「按 v10 REST 的形状发请求」。
**MockTransport 能精确断言路径、方法、`Authorization: Bot …` 头、JSON 体**，
这比起一个通用 echo server 更能咬住错误。

**注意这条测试的边界**：它验的是**我对 Discord API 的理解**是否被代码正确表达，
不是「Discord 真的会接受」。**所以只对我有把握的 API 写工具** ——
飞书 / 元宝我没有可靠参考，凭猜写就是假覆盖。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.tool_discord import API_BASE, DISCORD_TOOLS, DiscordConnector

TOKEN = "bot-token-abc"


class _Secret:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


class FakeSettings:
    def __init__(self, token: str = TOKEN) -> None:
        self.discord_bot_token = _Secret(token)


def _connector(handler, token: str = TOKEN) -> DiscordConnector:
    return DiscordConnector(FakeSettings(token), transport=httpx.MockTransport(handler))


def test_two_tools_are_declared():
    assert [t.name for t in DISCORD_TOOLS] == ["discord", "discord_admin"]


async def test_read_uses_the_bot_auth_scheme():
    """**Discord 用 `Bot <token>`，不是 Bearer** —— 写错就是 401。"""
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(
            200,
            json=[
                {
                    "id": "m1",
                    "channel_id": "c1",
                    "content": "hello",
                    "timestamp": "2026-01-01T00:00:00Z",
                    "author": {"username": "alice", "id": "u1"},
                }
            ],
        )

    result = await _connector(handler).execute("discord", {"action": "read", "channel_id": "c1"}, "t", "o")

    assert seen["url"] == f"{API_BASE}/channels/c1/messages?limit=50"
    assert seen["auth"] == f"Bot {TOKEN}"
    assert result["status"] == "ok"
    assert result["messages"][0]["author"] == "alice"


async def test_send_posts_content():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content or b"{}")
        return httpx.Response(200, json={"id": "m2", "channel_id": "c1", "content": "hi", "author": {"username": "me"}})

    result = await _connector(handler).execute(
        "discord", {"action": "send", "channel_id": "c1", "content": "hi"}, "t", "o"
    )

    assert seen["method"] == "POST"
    assert seen["url"] == f"{API_BASE}/channels/c1/messages"
    assert seen["body"] == {"content": "hi"}
    assert result["status"] == "ok"
    assert result["message"]["id"] == "m2"


async def test_search_flattens_the_per_hit_arrays():
    """Discord 的搜索结果每条命中是一个**数组**，要取第一个元素。"""

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/guilds/g1/messages/search" in str(request.url)
        return httpx.Response(
            200,
            json={
                "total_results": 2,
                "messages": [
                    [{"id": "a", "content": "x", "author": {"username": "u"}}],
                    [{"id": "b", "content": "y", "author": {"username": "v"}}],
                ],
            },
        )

    result = await _connector(handler).execute(
        "discord", {"action": "search", "guild_id": "g1", "content": "x"}, "t", "o"
    )
    assert result["total"] == 2
    assert [m["id"] for m in result["messages"]] == ["a", "b"]


async def test_unknown_action_lists_the_valid_ones():
    """**未知 action 要列出可选值** —— 否则模型只能瞎猜重试。"""
    result = await _connector(lambda r: httpx.Response(200, json=[])).execute(
        "discord", {"action": "explode"}, "t", "o"
    )
    assert result["status"] == "error"
    assert "read" in result["error"] and "send" in result["error"]


async def test_create_channel_posts_type_zero():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content or b"{}")
        return httpx.Response(201, json={"id": "c9", "name": "general-2"})

    result = await _connector(handler).execute(
        "discord_admin", {"action": "create_channel", "guild_id": "g1", "name": "general-2"}, "t", "o"
    )
    assert seen["body"] == {"name": "general-2", "type": 0}
    assert result["channel"]["id"] == "c9"


async def test_set_permission_puts_the_overwrite():
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content or b"{}")
        return httpx.Response(204)

    result = await _connector(handler).execute(
        "discord_admin",
        {"action": "set_permission", "channel_id": "c1", "overwrite_id": "r1", "allow": "1024"},
        "t",
        "o",
    )
    assert seen["method"] == "PUT"
    assert seen["url"] == f"{API_BASE}/channels/c1/permissions/r1"
    assert seen["body"]["allow"] == "1024"
    assert seen["body"]["type"] == 0
    assert result["status"] == "ok"


async def test_unconfigured_token_returns_actionable_error():
    result = await _connector(lambda r: httpx.Response(200, json=[]), token="").execute(
        "discord", {"action": "read", "channel_id": "c1"}, "t", "o"
    )
    assert result["status"] == "error"
    assert "discord_bot_token" in result["error"]


async def test_http_error_becomes_a_readable_error():
    result = await _connector(lambda r: httpx.Response(403, text="Missing Access")).execute(
        "discord", {"action": "read", "channel_id": "c1"}, "t", "o"
    )
    assert result["status"] == "error"
    assert "403" in result["error"]


async def test_connection_failure_becomes_a_readable_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    result = await _connector(boom).execute("discord", {"action": "read", "channel_id": "c1"}, "t", "o")
    assert result["status"] == "error"
    assert "连不上 Discord" in result["error"]
