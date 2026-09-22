"""Connect external MCP servers and expose their tools to the agent.

Design notes:

- Servers are declared in ``data/mcp.json``. Nothing is trusted by default: every tool a
  server provides needs confirmation, and only an explicit ``auto_approve`` on that server
  changes that. MCP tools are never covered by site-scoped grants.
- One persistent client per server, kept open for the life of the app. A server that fails
  to start is reported with its error and does not stop the others.
- Secrets never live in ``mcp.json``: values written as ``keyring:<name>`` are resolved
  from the system credential store under ``mcp:<server>:<name>``.
"""

import asyncio
import json
import re
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import Any

import httpx2
import keyring
from mcp.client import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client

from app.tools import Tool

NAME_PATTERN = re.compile(r"[^A-Za-z0-9_-]")
MAX_TOOL_NAME = 64
KEYRING_PREFIX = "keyring:"
CONNECT_TIMEOUT = 20.0


def dumped(obj) -> dict:
    """Read SDK objects through model_dump().

    mcp 2.x exposes snake_case fields (``input_schema``) while the raw protocol uses
    camelCase (``inputSchema``). Touching an attribute that does not exist on those models
    is not safe — one probe hung on it — so every read goes through a plain dict instead.
    """
    try:
        return obj.model_dump() if hasattr(obj, "model_dump") else {}
    except Exception:  # noqa: BLE001 - a malformed payload must not break the run
        return {}


def schema_of(tool) -> dict:
    payload = dumped(tool)
    for key in ("input_schema", "inputSchema"):
        value = payload.get(key)
        if isinstance(value, dict):
            return value
    return {"type": "object", "properties": {}}


def sanitize(value: str) -> str:
    return NAME_PATTERN.sub("_", str(value))[:40]


def tool_name(server: str, remote: str) -> str:
    """Model-facing name: ASCII, stable, and never collides with a built-in tool."""
    return f"mcp__{sanitize(server)}__{sanitize(remote)}"[:MAX_TOOL_NAME]


class MCPManager:
    def __init__(self, settings):
        self.settings = settings
        self.path: Path = settings.data_dir / "mcp.json"
        self.service = f"orbit:{settings.data_dir.resolve()}"
        self.status: dict[str, dict] = {}
        self._clients: dict[str, Any] = {}
        self._routes: dict[str, tuple[str, str]] = {}
        self._http_clients: list[Any] = []
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()

    # --- 配置 ---------------------------------------------------------------
    def servers(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return []
        servers = loaded.get("servers") if isinstance(loaded, dict) else None
        return [s for s in servers or [] if isinstance(s, dict) and s.get("name")]

    def save_servers(self, servers: list[dict]) -> None:
        cleaned = []
        for entry in servers:
            if not isinstance(entry, dict) or not str(entry.get("name", "")).strip():
                raise ValueError("每个 MCP 服务都需要 name")
            name = str(entry["name"]).strip()
            has_url = bool(str(entry.get("url") or "").strip())
            has_command = bool(str(entry.get("command") or "").strip())
            if not has_url and not has_command:
                raise ValueError(f"{name} 需要 command（本地进程）或 url（远程服务）")
            record = {
                "name": name,
                "enabled": bool(entry.get("enabled", True)),
                "auto_approve": bool(entry.get("auto_approve", False)),
            }
            if has_url:
                record["url"] = str(entry["url"]).strip()
                if entry.get("headers"):
                    record["headers"] = {
                        str(k): str(v) for k, v in dict(entry["headers"]).items()
                    }
            else:
                record["command"] = str(entry["command"]).strip()
                record["args"] = [str(a) for a in entry.get("args") or []]
                if entry.get("env"):
                    record["env"] = {str(k): str(v) for k, v in dict(entry["env"]).items()}
            cleaned.append(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"servers": cleaned}, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _resolve(self, server: str, values: dict | None) -> dict:
        resolved = {}
        for key, value in (values or {}).items():
            text = str(value)
            if text.startswith(KEYRING_PREFIX):
                secret = keyring.get_password(
                    self.service, f"mcp:{server}:{text[len(KEYRING_PREFIX):]}"
                )
                if secret:
                    resolved[key] = secret
                continue
            resolved[key] = text
        return resolved

    def secret_names(self, server: str) -> list[str]:
        names: list[str] = []
        for entry in self.servers():
            if entry.get("name") != server:
                continue
            for field in ("env", "headers"):
                for value in (entry.get(field) or {}).values():
                    text = str(value)
                    if text.startswith(KEYRING_PREFIX):
                        names.append(text[len(KEYRING_PREFIX):])
        return names

    def set_secret(self, server: str, name: str, value: str) -> None:
        keyring.set_password(self.service, f"mcp:{server}:{name}", value)

    def delete_secret(self, server: str, name: str) -> None:
        with suppress(Exception):
            keyring.delete_password(self.service, f"mcp:{server}:{name}")

    def public(self) -> list[dict]:
        rows = []
        for entry in self.servers():
            name = str(entry["name"])
            state = self.status.get(name, {"state": "idle", "tools": 0, "error": ""})
            target = entry.get("url") or " ".join(
                [str(entry.get("command", ""))] + [str(a) for a in entry.get("args") or []]
            )
            rows.append(
                {
                    "name": name,
                    "target": target,
                    "enabled": bool(entry.get("enabled", True)),
                    "auto_approve": bool(entry.get("auto_approve", False)),
                    "secrets": self.secret_names(name),
                    **state,
                }
            )
        return rows

    # --- 连接 ---------------------------------------------------------------
    def _client(self, entry: dict) -> Client:
        name = str(entry["name"])
        url = str(entry.get("url") or "").strip()
        if url:
            headers = self._resolve(name, entry.get("headers"))
            http_client = httpx2.AsyncClient(headers=headers or None)
            self._http_clients.append(http_client)
            return Client(streamable_http_client(url, http_client=http_client))
        command = str(entry.get("command") or "").strip()
        if not command:
            raise ValueError("需要 command 或 url")
        return Client(
            StdioServerParameters(
                command=command,
                args=[str(a) for a in entry.get("args") or []],
                env=self._resolve(name, entry.get("env")) or None,
            )
        )

    async def start(self, registry) -> None:
        async with self._lock:
            await self._stop_locked(registry)
            self._stack = AsyncExitStack()
            for entry in self.servers():
                name = str(entry["name"])
                if not entry.get("enabled", True):
                    self.status[name] = {"state": "disabled", "tools": 0, "error": ""}
                    continue
                limit = float(entry.get("timeout") or CONNECT_TIMEOUT)
                # Each attempt gets its own stack: a half-connected server must not leak a
                # subprocess, and a hung one must not hold up the whole startup.
                inner = AsyncExitStack()
                try:
                    async with asyncio.timeout(limit):
                        client = await inner.enter_async_context(self._client(entry))
                        listing = await client.list_tools()
                except TimeoutError:
                    await inner.aclose()
                    self.status[name] = {
                        "state": "error",
                        "tools": 0,
                        "error": f"连接或列出工具超过 {limit:g} 秒未完成",
                    }
                    continue
                except Exception as exc:  # noqa: BLE001 - one bad server must not stop the rest
                    await inner.aclose()
                    self.status[name] = {
                        "state": "error",
                        "tools": 0,
                        "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                    }
                    continue
                self._stack.push_async_callback(inner.aclose)
                tools = []
                for remote in listing.tools:
                    shape = dumped(remote)
                    label = str(shape.get("name") or "tool")
                    tool = Tool(
                        name=tool_name(name, label),
                        description=str(shape.get("description") or label)[:1000],
                        confirmation=not bool(entry.get("auto_approve")),
                        parameters=schema_of(remote),
                        source=f"mcp:{name}",
                    )
                    tools.append(tool)
                    self._routes[tool.name] = (name, label)
                self._clients[name] = client
                registry.register(tools, self.execute)
                self.status[name] = {"state": "ready", "tools": len(tools), "error": ""}

    async def _stop_locked(self, registry) -> None:
        for name in list(self._clients):
            registry.unregister_source(f"mcp:{name}")
        self._clients.clear()
        self._routes.clear()
        for http_client in self._http_clients:
            with suppress(Exception):
                await http_client.aclose()
        self._http_clients.clear()
        if self._stack is not None:
            with suppress(Exception):
                await self._stack.aclose()
            self._stack = None

    async def reload(self, registry) -> None:
        await self.start(registry)

    async def close(self, registry) -> None:
        async with self._lock:
            await self._stop_locked(registry)

    # --- 执行 ---------------------------------------------------------------
    async def execute(self, name: str, args: dict, task_id: str, operation_id: str) -> dict:
        route = self._routes.get(name)
        if not route:
            return {"status": "error", "error": f"{name} 已不可用，MCP 服务可能已断开"}
        server, remote = route
        client = self._clients.get(server)
        if client is None:
            return {"status": "error", "error": f"MCP 服务 {server} 未连接"}
        try:
            result = await client.call_tool(remote, args or {})
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:600]}"}
        return {"status": "ok", "data": self.render(result)}

    @staticmethod
    def render(result) -> dict:
        """Flatten an MCP tool result into the shape the rest of the app expects."""
        shape = dumped(result)
        blocks = []
        for item in shape.get("content") or []:
            if isinstance(item, dict) and item.get("text") is not None:
                blocks.append(str(item["text"]))
            else:
                blocks.append(f"[{item.get('type', 'content') if isinstance(item, dict) else 'content'}]")
        payload: dict = {"content": "\n".join(blocks)[:16000]}
        structured = shape.get("structured_content")
        if structured is None:
            structured = shape.get("structuredContent")
        if structured is not None:
            payload["structured"] = structured
        if shape.get("is_error") or shape.get("isError"):
            payload["is_error"] = True
        return payload
