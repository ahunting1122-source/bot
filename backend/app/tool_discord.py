"""Discord 工具（`5.14` 的 `collab` 域前两条）。

## 为什么先做这两条，不做飞书 / 元宝

**Discord 的 REST API（v10）是公开且稳定的**，我能把请求形状写准。
飞书与元宝我**没有可靠的 API 参考** —— 凭猜写出来的工具会通过我自己写的
`MockTransport` 测试（等于**用我的假设验证我的假设**），**那是假覆盖**。

**判据：能写出「请求形状」的工具才写；写不准的先记清「缺哪份 API 参考」，
不要用自测通过冒充实现对。**

## 认证形状

`Authorization: Bot <token>`（Discord bot token，**不是** `Bearer`）。
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import Field

from app.tools import Args, Tool

API_BASE = "https://discord.com/api/v10"
TIMEOUT_SECONDS = 20.0
#: 读消息的默认条数（Discord 上限 100）。
READ_LIMIT = 50


class DiscordArgs(Args):
    action: str = Field(description="read / send / search")
    channel_id: str = Field(default="", description="频道 id（read / send 必填）")
    guild_id: str = Field(default="", description="服务器 id（search 必填）")
    content: str = Field(default="", description="要发的文本（send 必填）或搜索词（search 必填）")
    limit: int = Field(default=READ_LIMIT, ge=1, le=100, description="返回条数上限")


class DiscordAdminArgs(Args):
    action: str = Field(description="list_channels / create_channel / list_roles / set_permission")
    guild_id: str = Field(default="", description="服务器 id")
    channel_id: str = Field(default="", description="频道 id（set_permission 必填）")
    name: str = Field(default="", description="新频道名（create_channel 必填）")
    overwrite_id: str = Field(default="", description="被授权的角色/用户 id（set_permission 必填）")
    allow: str = Field(default="0", description="允许位（十进制字符串）")
    deny: str = Field(default="0", description="拒绝位（十进制字符串）")


DISCORD_TOOLS = [
    Tool("discord", "Discord 核心动作集（读消息 / 发消息 / 搜消息）", DiscordArgs),
    Tool(
        "discord_admin",
        "Discord 管理动作集（列频道 / 建频道 / 列角色 / 设权限覆盖）",
        DiscordAdminArgs,
    ),
]


def _brief(message: dict[str, Any]) -> dict[str, Any]:
    """把一条 Discord 消息压成模型够用的形状（原始对象字段太多，会撑爆上下文）。"""
    author = message.get("author") or {}
    return {
        "id": message.get("id"),
        "channel_id": message.get("channel_id"),
        "author": author.get("username") or author.get("id"),
        "content": message.get("content"),
        "timestamp": message.get("timestamp"),
    }


class DiscordConnector:
    """把 2 个 Discord 工具接到 bot token 上。

    **没配 token 就返回一条能照着做的错误** —— 模型看得懂「去设置里配 X」，看不懂 traceback。
    """

    def __init__(self, settings: Any, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport

    def _raw_token(self) -> str:
        token = getattr(self.settings, "discord_bot_token", None)
        if token is None:
            return ""
        if hasattr(token, "get_secret_value"):
            return str(token.get_secret_value())
        return str(token)

    def _headers(self) -> dict[str, str]:
        # Discord 用的是 `Bot <token>`，不是 Bearer。
        return {"Authorization": f"Bot {self._raw_token()}", "Content-Type": "application/json"}

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=API_BASE, timeout=TIMEOUT_SECONDS, transport=self.transport
        )

    async def execute(self, name, args, task_id, operation_id):
        if not self._raw_token().strip():
            return {
                "status": "error",
                "error": "请在设置里配置 `discord_bot_token`（Discord 开发者门户里的 Bot token）",
            }

        async with self._client() as client:
            try:
                if name == "discord":
                    return await self._core(client, args)
                if name == "discord_admin":
                    return await self._admin(client, args)
            except httpx.HTTPStatusError as exc:
                return {
                    "status": "error",
                    "error": f"Discord 返回 {exc.response.status_code}：{exc.response.text[:200]}",
                }
            except httpx.HTTPError as exc:
                return {"status": "error", "error": f"连不上 Discord：{type(exc).__name__}: {exc}"}

        raise ValueError(f"未知的 Discord 工具：{name}")

    async def _core(self, client: httpx.AsyncClient, args) -> dict[str, Any]:
        action = str(args.get("action") or "")
        if action == "read":
            channel_id = str(args.get("channel_id") or "")
            if not channel_id:
                return {"status": "error", "error": "read 需要 channel_id"}
            response = await client.get(
                f"/channels/{channel_id}/messages",
                headers=self._headers(),
                params={"limit": int(args.get("limit") or READ_LIMIT)},
            )
            response.raise_for_status()
            return {"status": "ok", "messages": [_brief(m) for m in response.json()]}

        if action == "send":
            channel_id = str(args.get("channel_id") or "")
            content = str(args.get("content") or "")
            if not channel_id or not content:
                return {"status": "error", "error": "send 需要 channel_id 与 content"}
            response = await client.post(
                f"/channels/{channel_id}/messages",
                headers=self._headers(),
                json={"content": content},
            )
            response.raise_for_status()
            return {"status": "ok", "message": _brief(response.json())}

        if action == "search":
            guild_id = str(args.get("guild_id") or "")
            content = str(args.get("content") or "")
            if not guild_id or not content:
                return {"status": "error", "error": "search 需要 guild_id 与 content"}
            response = await client.get(
                f"/guilds/{guild_id}/messages/search",
                headers=self._headers(),
                params={"content": content, "limit": int(args.get("limit") or READ_LIMIT)},
            )
            response.raise_for_status()
            body = response.json()
            flat: list[dict[str, Any]] = []
            for hit in body.get("messages") or []:
                # Discord 的搜索结果是「每条命中一个数组」，取第一个元素。
                if isinstance(hit, list) and hit:
                    flat.append(_brief(hit[0]))
            return {"status": "ok", "total": body.get("total_results"), "messages": flat}

        return {"status": "error", "error": f"未知 action：{action}（read / send / search）"}

    async def _admin(self, client: httpx.AsyncClient, args) -> dict[str, Any]:
        action = str(args.get("action") or "")
        guild_id = str(args.get("guild_id") or "")

        if action == "list_channels":
            if not guild_id:
                return {"status": "error", "error": "list_channels 需要 guild_id"}
            response = await client.get(f"/guilds/{guild_id}/channels", headers=self._headers())
            response.raise_for_status()
            return {
                "status": "ok",
                "channels": [
                    {"id": c.get("id"), "name": c.get("name"), "type": c.get("type")}
                    for c in response.json()
                ],
            }

        if action == "create_channel":
            name = str(args.get("name") or "")
            if not guild_id or not name:
                return {"status": "error", "error": "create_channel 需要 guild_id 与 name"}
            response = await client.post(
                f"/guilds/{guild_id}/channels",
                headers=self._headers(),
                # type 0 = 文本频道
                json={"name": name, "type": 0},
            )
            response.raise_for_status()
            body = response.json()
            return {"status": "ok", "channel": {"id": body.get("id"), "name": body.get("name")}}

        if action == "list_roles":
            if not guild_id:
                return {"status": "error", "error": "list_roles 需要 guild_id"}
            response = await client.get(f"/guilds/{guild_id}/roles", headers=self._headers())
            response.raise_for_status()
            return {
                "status": "ok",
                "roles": [
                    {"id": r.get("id"), "name": r.get("name"), "permissions": r.get("permissions")}
                    for r in response.json()
                ],
            }

        if action == "set_permission":
            channel_id = str(args.get("channel_id") or "")
            overwrite_id = str(args.get("overwrite_id") or "")
            if not channel_id or not overwrite_id:
                return {"status": "error", "error": "set_permission 需要 channel_id 与 overwrite_id"}
            response = await client.put(
                f"/channels/{channel_id}/permissions/{overwrite_id}",
                headers=self._headers(),
                json={
                    "allow": str(args.get("allow") or "0"),
                    "deny": str(args.get("deny") or "0"),
                    "type": 0,  # 0 = 角色
                },
            )
            response.raise_for_status()
            return {"status": "ok", "channel_id": channel_id, "overwrite_id": overwrite_id}

        return {
            "status": "error",
            "error": f"未知 action：{action}（list_channels / create_channel / list_roles / set_permission）",
        }
