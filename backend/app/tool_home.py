"""Home Assistant 工具（`5.14` 的 `home` 域 4 条）。

## 为什么这 4 条可以做

Hermes 的 `ha_*` 工具**同样需要**一个 HA 实例地址 + 长期令牌 ——
**「Orbit 没有这个工具」和「Orbit 没有凭证」是两件事。**
补上工具之后，配好 `home_assistant_url` / `home_assistant_token` 就能用，
**与 `piper` 要 `PIPER_BIN` 是同一类「需要配置」，不是「做不到」。**

## 为什么能离线验证

HA 的 REST API 是普通 HTTP：`GET /api/states`、`GET /api/states/<id>`、
`GET /api/services`、`POST /api/services/<domain>/<service>`。
**测试起一个本地 stub 服务端就够了** —— 不依赖真的 HA 实例。

**判据：需要凭证的工具仍然可以被验证 —— 验的是「请求构造对不对」，
而不是「对方的服务好不好」。把这两件事混在一起，就会把「没凭证」误当成「做不了」。**
"""

from __future__ import annotations

from typing import Any

import httpx
from pydantic import Field

from app.tools import Args, Tool

#: 单次请求超时。HA 的本地实例通常很快，给 20 秒足够。
TIMEOUT_SECONDS = 20.0

#: 列实体时的默认上限（HA 实例动辄上千个实体，全量会撑爆上下文）。
LIST_LIMIT = 200


class HaListEntitiesArgs(Args):
    domain: str = Field(default="", description="按域过滤，如 `light` / `switch`（空 = 全部）")
    limit: int = Field(default=LIST_LIMIT, ge=1, le=1000, description="最多返回多少个实体")


class HaGetStateArgs(Args):
    entity_id: str = Field(min_length=1, description="实体 id，如 `light.kitchen`")


class HaListServicesArgs(Args):
    domain: str = Field(default="", description="按域过滤（空 = 全部）")


class HaCallServiceArgs(Args):
    domain: str = Field(min_length=1, description="服务域，如 `light`")
    service: str = Field(min_length=1, description="服务名，如 `turn_on`")
    entity_id: str = Field(default="", description="目标实体 id（空 = 用 data 里的）")
    data: dict[str, Any] = Field(default_factory=dict, description="其余服务数据")


HOME_TOOLS = [
    Tool(
        "ha_list_entities",
        "列 Home Assistant 实体（可按域过滤；默认最多 200 个）",
        HaListEntitiesArgs,
    ),
    Tool("ha_get_state", "查单个实体的状态与全部属性", HaGetStateArgs),
    Tool("ha_list_services", "列 Home Assistant 可用的服务（动作）", HaListServicesArgs),
    Tool("ha_call_service", "调一个服务去控制设备（如 light.turn_on）", HaCallServiceArgs),
]


class HomeAssistantConnector:
    """把 4 个 `ha_*` 工具接到一个 HA 实例上。

    **`base_url` 与 `token` 从 settings 读** —— 没配就返回一条能照着做的错误，
    而不是抛异常（模型看得懂「去设置里配 X」，看不懂 traceback）。
    """

    def __init__(self, settings: Any, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        #: 测试可以注入 `httpx.MockTransport`；生产为 None（走真网络）。
        self.transport = transport

    def _base(self) -> str:
        return str(getattr(self.settings, "home_assistant_url", "") or "").rstrip("/")

    def _raw_token(self) -> str:
        token = getattr(self.settings, "home_assistant_token", None)
        if token is None:
            return ""
        if hasattr(token, "get_secret_value"):
            return str(token.get_secret_value())
        return str(token)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._raw_token()}",
            "Content-Type": "application/json",
        }

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=TIMEOUT_SECONDS, transport=self.transport, follow_redirects=False
        )

    def _missing(self) -> dict[str, Any] | None:
        """没配就返回一条可照做的错误；配了返回 None。"""
        if not self._base():
            return {
                "status": "error",
                "error": "请在设置里配置 `home_assistant_url`（如 http://homeassistant.local:8123）",
            }
        # **直接看原始令牌** —— 先 strip 再 removeprefix 会把 "Bearer " 变成 "Bearer"，判不出来。
        if not self._raw_token().strip():
            return {
                "status": "error",
                "error": "请在设置里配置 `home_assistant_token`（HA 的长期访问令牌）",
            }
        return None

    async def execute(self, name, args, task_id, operation_id):
        missing = self._missing()
        if missing is not None:
            return missing

        base = self._base()
        async with self._client() as client:
            try:
                if name == "ha_list_entities":
                    response = await client.get(f"{base}/api/states", headers=self._headers())
                    response.raise_for_status()
                    states = response.json()
                    domain = str(args.get("domain") or "")
                    if domain:
                        states = [s for s in states if str(s.get("entity_id", "")).startswith(f"{domain}.")]
                    limit = int(args.get("limit") or LIST_LIMIT)
                    return {
                        "status": "ok",
                        "count": len(states),
                        "truncated": len(states) > limit,
                        "entities": [
                            {
                                "entity_id": s.get("entity_id"),
                                "state": s.get("state"),
                                "name": (s.get("attributes") or {}).get("friendly_name"),
                            }
                            for s in states[:limit]
                        ],
                    }

                if name == "ha_get_state":
                    entity_id = str(args["entity_id"])
                    response = await client.get(
                        f"{base}/api/states/{entity_id}", headers=self._headers()
                    )
                    response.raise_for_status()
                    body = response.json()
                    return {
                        "status": "ok",
                        "entity_id": body.get("entity_id"),
                        "state": body.get("state"),
                        "attributes": body.get("attributes") or {},
                        "last_changed": body.get("last_changed"),
                    }

                if name == "ha_list_services":
                    response = await client.get(f"{base}/api/services", headers=self._headers())
                    response.raise_for_status()
                    body = response.json()
                    domain = str(args.get("domain") or "")
                    if domain:
                        body = [d for d in body if d.get("domain") == domain]
                    return {
                        "status": "ok",
                        "count": len(body),
                        "services": [
                            {
                                "domain": d.get("domain"),
                                "services": sorted((d.get("services") or {}).keys()),
                            }
                            for d in body
                        ],
                    }

                if name == "ha_call_service":
                    domain = str(args["domain"])
                    service = str(args["service"])
                    payload: dict[str, Any] = dict(args.get("data") or {})
                    if args.get("entity_id"):
                        payload["entity_id"] = args["entity_id"]
                    response = await client.post(
                        f"{base}/api/services/{domain}/{service}",
                        headers=self._headers(),
                        json=payload,
                    )
                    response.raise_for_status()
                    return {
                        "status": "ok",
                        "domain": domain,
                        "service": service,
                        "result": response.json() if response.content else [],
                    }
            except httpx.HTTPStatusError as exc:
                return {
                    "status": "error",
                    "error": f"HA 返回 {exc.response.status_code}：{exc.response.text[:200]}",
                }
            except httpx.HTTPError as exc:
                return {"status": "error", "error": f"连不上 HA（{base}）：{type(exc).__name__}: {exc}"}

        raise ValueError(f"未知的 Home Assistant 工具：{name}")
