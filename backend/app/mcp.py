"""Connect external MCP servers and expose their tools to the agent.

Design notes:

- Servers are declared in ``data/mcp.json``. Nothing is trusted by default: every tool a
  server provides needs confirmation, and only an explicit ``auto_approve`` on that server
  changes that. MCP tools are never covered by site-scoped grants.
- One persistent client per server, kept open for the life of the app. A server that fails
  to start is reported with its error and does not stop the others.
- Secrets never live in ``mcp.json``: values written as ``keyring:<name>`` are resolved
  from the system credential store under ``mcp:<server>:<name>``.
- **Liveness is a background concern, not a user-facing one.** A vanished server used to be
  permanent until someone clicked "重新连接"; now each one runs its own heartbeat, and a
  detected loss unregisters its tools and starts an exponential-backoff reconnect. The failure
  path is deliberately damped (see ``HEARTBEAT_GRACE_SECONDS``): a probe that flaps must not
  silently strip a whole toolset, because the model then reports "工具不存在" and the real
  cause — one jittery ping — is nowhere in the transcript.
"""

import asyncio
import json
import logging
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

log = logging.getLogger("orbit.mcp")

NAME_PATTERN = re.compile(r"[^A-Za-z0-9_-]")
MAX_TOOL_NAME = 64
KEYRING_PREFIX = "keyring:"
CONNECT_TIMEOUT = 20.0

# --- 存活检测 / 重连参数 ---------------------------------------------------
#
# 这里踩过一个大坑，记下来免得以后有人"优化"回去：
#
#   ``client.send_ping()`` 在 2026-07-28 协议上**已被移除**（SDK 只在 ``mode="legacy"``
#   下保留它）。实测：无论服务是死是活，调用它都直接抛 ``MCPError: Method not found``。
#   也就是说它**恒失败**——用它当心跳，宽限期一过就会把所有服务判死、无限重连。
#   同理 ``list_tools()`` 打在响应缓存上，服务停摆时照样返回旧清单，也证明不了活着。
#
# 现代线上的正解是 ``client.listen(...)``：它开一条长驻订阅流，服务**非优雅断开**时
# 立刻抛 ``SubscriptionLost``。这是事件驱动而非轮询，代价是一次 ack 往返。
# 老协议（<2026-07-28）不支持 listen，退回 send_ping——那时它还在。
#
# 宽限期只对**轮询式**探针有意义（抖动抑制）。事件式的 SubscriptionLost 是确定信号，
# 但网络类泵仍可能误报，所以保留一次确认的机会（见 ``_probe_once``）。
LIVENESS_RETRY = 1
#: 老协议轮询心跳的间隔与单次超时。
LIVENESS_INTERVAL = 15.0
LIVENESS_TIMEOUT = 10.0
#: 取单个 prompt 正文的超时（网络调用，失败只记日志）。
PROMPT_BODY_TIMEOUT = 5.0
#: 读单个 resource 正文的超时。
RESOURCE_READ_TIMEOUT = 10.0
#: 相邻两个 resource 抓取之间的间隔（限速，防止打爆服务）。
RESOURCE_FETCH_GAP = 0.1
#: list_resources 翻页上限（防 forever-cursor 死循环）。
RESOURCE_MAX_PAGES = 50
#: ``listen`` 可用的最低协商协议版本（字符串比较对该格式成立）。
LISTEN_MIN_VERSION = "2026-07-28"
#: 退避重连的间隔序列（秒），末位为封顶值。
RECONNECT_BACKOFF = (2.0, 4.0, 8.0, 16.0, 32.0, 60.0)

STATE_IDLE = "idle"
STATE_DISABLED = "disabled"
STATE_READY = "ready"
STATE_RECONNECTING = "reconnecting"
STATE_ERROR = "error"


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


def _supports_listen(client) -> bool:
    """订阅流是 2026-07-28 起才有的能力；老连接只能轮询。

    ``listen()`` 自己会在老版本上抛 ``ListenNotSupportedError``，但那是"发起后才失败"。
    这里提前从协商到的协议版本判定，省掉一次注定失败的往返。
    """
    try:
        session = client.session
        version = session.protocol_version
    except Exception:  # noqa: BLE001
        return False
    return bool(version) and version >= LISTEN_MIN_VERSION


def _render_prompt(result) -> str:
    """把 ``get_prompt`` 的结果摊平成一段文本（prompt 模板正文）。

    一条 PromptMessage 的 content 通常是 ``TextContent``（``{"type":"text","text":...}``），
    但也可能直接是字符串。逐条抽 text，拼成模板正文。
    """
    shape = dumped(result)
    blocks = []
    for item in shape.get("messages") or []:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if isinstance(content, dict):
            text = content.get("text")
            blocks.append(str(text) if text is not None else "")
        elif isinstance(content, str):
            blocks.append(content)
        elif content is not None:
            blocks.append(str(content))
    return "\n".join(blocks).strip()[:16000]


def _render_resource(result) -> str:
    """把 ``read_resource`` 的结果摊平成一段文本（只取 text 类 content）。"""
    shape = dumped(result)
    blocks = []
    for item in shape.get("contents") or []:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            blocks.append(text)
        elif text is not None:
            blocks.append(str(text))
    return "\n".join(blocks).strip()


class MCPManager:
    def __init__(self, settings, indexer=None):
        self.settings = settings
        self.path: Path = settings.data_dir / "mcp.json"
        self.service = f"orbit:{settings.data_dir.resolve()}"
        #: 索引写入器（RAG 的 FTS5 侧），供 `index_resources` 服务把 resource 内容入 RAG。
        #: 由装配层注入；None = 不索引（测试/降级路径）。
        self.indexer = indexer
        self.status: dict[str, dict] = {}
        self._clients: dict[str, Any] = {}
        self._routes: dict[str, tuple[str, str]] = {}
        self._entries: dict[str, dict] = {}
        #: 老协议轮询心跳的连续失败计数；被判定为抖动时清零。
        self._failures: dict[str, int] = {}
        #: 每服务已拉取的 prompts（多源技能的第二个来源），键为服务名，值为技能字典列表。
        self._prompts: dict[str, list[dict]] = {}
        #: 每服务的资源索引后台任务（`index_resources` 服务才有），close() 必须取消。
        self._resource_tasks: dict[str, asyncio.Task] = {}
        self._http_clients: list[Any] = []
        self._stack: AsyncExitStack | None = None
        self._lock = asyncio.Lock()
        #: 每服务一个后台看门狗（心跳 + 重连），close() 必须全部取消。
        self._watchers: dict[str, asyncio.Task] = {}
        self._closed = False

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
                "index_resources": bool(entry.get("index_resources", False)),
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
            self._closed = False
            self._stack = AsyncExitStack()
            for entry in self.servers():
                name = str(entry["name"])
                self._entries[name] = entry
                if not entry.get("enabled", True):
                    self.status[name] = {"state": STATE_DISABLED, "tools": 0, "error": ""}
                    continue
                await self._connect_locked(entry, registry)
                # 连接失败也起看门狗：否则一个"启动时就不在"的服务永远等不到重连，
                # 而它的失败恰恰是最常见的一种失联（子进程依赖缺失、端口没起）。
                self._spawn_watcher(name, registry)

    async def _connect_locked(self, entry: dict, registry) -> bool:
        """连接一个服务并注册它的工具。调用方必须已持 ``self._lock``。

        每次尝试**自己一个 ``AsyncExitStack``**：半连接不能泄漏子进程，挂死的服务也不能
        拖住整个启动。成功才把内层栈挂到外层，保证 ``close()`` 能一次收干净。
        """
        name = str(entry["name"])
        limit = float(entry.get("timeout") or CONNECT_TIMEOUT)
        if self._stack is None:  # 不变量：只在 start()/重连路径下调用，栈必已建好
            raise RuntimeError("MCPManager 未启动")
        inner = AsyncExitStack()
        try:
            async with asyncio.timeout(limit):
                client = await inner.enter_async_context(self._client(entry))
                listing = await client.list_tools()
                prompts = await self._fetch_prompts(name, client)
        except TimeoutError:
            await inner.aclose()
            self.status[name] = {
                "state": STATE_ERROR,
                "tools": 0,
                "error": f"连接或列出工具超过 {limit:g} 秒未完成",
            }
            return False
        except Exception as exc:  # noqa: BLE001 - one bad server must not stop the rest
            await inner.aclose()
            self.status[name] = {
                "state": STATE_ERROR,
                "tools": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
            }
            return False
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
        self._prompts[name] = prompts
        registry.register(tools, self.execute)
        self._failures[name] = 0
        self.status[name] = {"state": STATE_READY, "tools": len(tools), "error": ""}
        # 资源入 RAG：后台隔离任务，只有显式 index_resources 且注入了 indexer 才做。
        if entry.get("index_resources") and self.indexer is not None:
            task = asyncio.create_task(self._index_resources(name, client))
            self._resource_tasks[name] = task
        return True

    async def _fetch_prompts(self, server: str, client) -> list[dict]:
        """拉取服务声明的 prompts，映射成技能字典（多源技能的第二来源）。

        先读能力声明再调用（Hermes 依据）：服务不支持 prompts 就直接跳过，而不是每次
        抛错。``list_prompts`` 翻页设上限，防 forever-cursor。``get_prompt`` 是网络调用，
        失败只记日志——该 prompt 仍以"仅描述"占位出现（body 为空），由消费方决定跳过。
        """
        caps = None
        try:
            caps = client.server_capabilities
        except Exception:  # noqa: BLE001
            caps = None
        if caps is None:
            return []
        cap_prompts = dumped(caps).get("prompts")
        if not cap_prompts:
            return []
        try:
            result = await client.list_prompts()
        except Exception as exc:  # noqa: BLE001
            log.debug("MCP %s 列出 prompts 失败：%s", server, exc)
            return []
        skills: list[dict] = []
        for prompt in result.prompts:
            shape = dumped(prompt)
            label = str(shape.get("name") or "")
            if not label:
                continue
            body = ""
            try:
                async with asyncio.timeout(PROMPT_BODY_TIMEOUT):
                    fetched = await client.get_prompt(label)
                body = _render_prompt(fetched)
            except Exception as exc:  # noqa: BLE001 - 取正文失败只记日志
                log.debug("MCP %s 取 prompt %s 正文失败：%s", server, label, exc)
            skills.append(
                {
                    "name": f"mcp__{sanitize(server)}__{sanitize(label)}",
                    "scope": "global",
                    "triggers": [str(shape.get("description") or label)],
                    "status": "active",
                    "source": f"mcp:{server}",
                    "body": body,
                    "usage_count": 0,
                    "success_rate": 0.0,
                    "consecutive_failures": 0,
                    "last_used_at": "",
                }
            )
        return skills

    def prompt_skills(self) -> list[dict]:
        """全部已连接服务的 prompt 技能（多源技能的 in-memory 部分）。"""
        skills: list[dict] = []
        for prompts in self._prompts.values():
            skills.extend(prompts)
        return skills

    def prompt_body(self, skill_name: str) -> str | None:
        """给定 ``mcp__<服务>__<prompt>`` 技能名，返回其 prompt 正文；不命中返回 None。"""
        for prompts in self._prompts.values():
            for skill in prompts:
                if skill["name"] == skill_name:
                    return skill.get("body") or ""
        return None

    async def _index_resources(self, server: str, client) -> None:
        """后台把 MCP resources 的内容索引进 RAG（隔离：失败只记日志，绝不阻塞连接/主链路）。

        顺序抓取 + 小间隔限速；``list_resources`` 翻页设上限防 forever-cursor；
        ``read_resource`` 带超时；坏 URI 只跳过该条，不拖垮整批。
        """
        from app.indexing import chunks_for_resource

        if self.indexer is None:
            return
        pages = 0
        cursor: str | None = None
        while pages < RESOURCE_MAX_PAGES:
            try:
                result = await client.list_resources(cursor=cursor)
            except Exception as exc:  # noqa: BLE001
                log.debug("MCP %s 列出 resources 失败：%s", server, exc)
                return
            for resource in result.resources:
                shape = dumped(resource)
                uri = str(shape.get("uri") or "")
                if not uri:
                    continue
                try:
                    async with asyncio.timeout(RESOURCE_READ_TIMEOUT):
                        fetched = await client.read_resource(uri)
                except Exception as exc:  # noqa: BLE001 - 坏 URI 只跳过这条
                    log.debug("MCP %s 读 resource %s 失败：%s", server, uri, exc)
                    continue
                body = _render_resource(fetched)
                if not body.strip():
                    continue
                try:
                    chunks = chunks_for_resource(uri, body, server)
                    if chunks:
                        await self.indexer.write(chunks)
                except Exception as exc:  # noqa: BLE001
                    log.debug("MCP %s 索引 resource %s 失败：%s", server, uri, exc)
                await asyncio.sleep(RESOURCE_FETCH_GAP)
            next_cursor = dumped(result).get("next_cursor")
            if not next_cursor:
                return
            cursor = str(next_cursor)
            pages += 1

    def _spawn_watcher(self, name: str, registry) -> None:
        """给一个服务起看门狗。已在跑的（重连成功后重入）不重复起。"""
        existing = self._watchers.get(name)
        if existing is not None and not existing.done():
            return
        self._watchers[name] = asyncio.create_task(self._watch(name, registry))

    async def _watch(self, name: str, registry) -> None:
        """单个服务的常驻循环：连上就守着，断了就退避重连。永不自行退出。

        取消（``close()``）是本任务唯一的正常终止路径。
        """
        attempt = 0
        while not self._closed:
            entry = self._entries.get(name)
            if entry is None or not entry.get("enabled", True):
                return

            client = self._clients.get(name)
            if client is None:
                # 还没连上（启动失败或刚被摘）：退避重连，成功就进下一轮的"守着它"。
                await self._reconnect_once(name, entry, registry, attempt)
                if self._clients.get(name) is not None:
                    attempt = 0
                else:
                    attempt = min(attempt + 1, len(RECONNECT_BACKOFF) - 1)
                continue

            # 连上了：守着它，直到它断。
            graceful = await self._supervise(name, client)
            if self._closed:
                return
            if graceful:
                log.info("MCP %s 的订阅流优雅结束，准备重连", name)
            await self._mark_lost(name, registry)
            attempt = 0

    async def _supervise(self, name: str, client) -> bool:
        """守着一条订阅流。返回 ``True`` 表示是优雅结束，``False`` 表示异常断开。

        现代协议走 ``listen()``：服务非优雅断开时抛 ``SubscriptionLost``。老协议退回
        ``send_ping()`` 轮询（那时 ping 还在，且是唯一可用的探针）。
        """
        caps = None
        try:
            caps = client.server_capabilities
        except Exception:  # noqa: BLE001
            caps = None
        modern = _supports_listen(client)

        if modern:
            try:
                async with client.listen(tools_list_changed=True) as sub:
                    async for _event in sub:
                        # 事件本身暂时不用，它的价值在于"流还活着"。将来要做
                        # 工具/资源变更的即时刷新，接在这里。
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - SubscriptionLost 及任何泵错误
                if self._closed:
                    return False
                log.debug("MCP %s 订阅流中断：%s: %s", name, type(exc).__name__, exc)
                return False
            return True

        return await self._poll_liveness(name, client, caps)

    async def _poll_liveness(self, name: str, client, caps) -> bool:
        """老协议的轮询心跳。返回 ``True`` 表示仍然活着。

        只有这条路需要宽限期：轮询本来就会碰到瞬时抖动，一次失败不该摘整个 toolset。
        """
        while not self._closed:
            try:
                async with asyncio.timeout(LIVENESS_TIMEOUT):
                    await client.send_ping()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self._failures[name] = self._failures.get(name, 0) + 1
                log.debug("MCP %s 心跳失败 %d 次：%s", name, self._failures[name], exc)
                if self._failures[name] > LIVENESS_RETRY:
                    return False
            else:
                self._failures[name] = 0
            await asyncio.sleep(LIVENESS_INTERVAL)
        return False

    async def _reconnect_once(self, name: str, entry: dict, registry, attempt: int) -> None:
        """退避一次，然后尝试重连。失败不抛，交给循环进入下一轮。"""
        delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)] if attempt else 0.0
        # 保留上一次的错误原因：一个永远起不来的服务如果只显示"正在重连"，
        # 用户就看不到它到底为什么起不来（缺依赖 / 路径错 / 端口占用）。
        # 第一次失败时是 error（原因刚写下），开始重试后转 reconnecting 但**不丢原因**。
        previous_error = self.status.get(name, {}).get("error", "")
        if delay:
            self.status[name] = {
                "state": STATE_RECONNECTING,
                "tools": 0,
                "error": previous_error,
            }
            await asyncio.sleep(delay)
        if self._closed:
            return
        async with self._lock:
            if self._closed or name not in self._entries:
                return
            # 上一个连接的工具先摘掉，避免与重连后的注册撞名。
            registry.unregister_source(f"mcp:{name}")
            self._clients.pop(name, None)
            self._routes = {k: v for k, v in self._routes.items() if v[0] != name}
            await self._connect_locked(entry, registry)

    async def _mark_lost(self, name: str, registry) -> None:
        """把服务标记为失联并摘掉它的工具，避免模型调到一个已知不可用的工具。"""
        log.warning("MCP 服务 %s 连接丢失，摘除其工具并开始重连", name)
        async with self._lock:
            registry.unregister_source(f"mcp:{name}")
            self._clients.pop(name, None)
            self._routes = {k: v for k, v in self._routes.items() if v[0] != name}
            self._prompts.pop(name, None)  # 其 prompts 跟着一起消失（与工具同一条路径）
            self.status[name] = {
                "state": STATE_RECONNECTING,
                "tools": 0,
                "error": "连接丢失，正在重连",
            }

    async def _stop_locked(self, registry) -> None:
        self._closed = True
        for task in self._watchers.values():
            task.cancel()
        if self._watchers:
            await asyncio.gather(*self._watchers.values(), return_exceptions=True)
        self._watchers.clear()
        for task in self._resource_tasks.values():
            task.cancel()
        if self._resource_tasks:
            await asyncio.gather(*self._resource_tasks.values(), return_exceptions=True)
        self._resource_tasks.clear()
        for name in list(self._clients):
            registry.unregister_source(f"mcp:{name}")
        self._clients.clear()
        self._routes.clear()
        self._entries.clear()
        self._failures.clear()
        self._prompts.clear()
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
            # 工具已被摘除（失联时主动 unregister），正常路径模型看不到它。走到这里说明
            # 模型拿着旧的工具清单在调 —— 给它一句能读懂的原因，而不是一个裸 name。
            return {
                "status": "error",
                "error": f"{name} 已不可用，MCP 服务可能已断开或正在重连",
            }
        server, remote = route
        client = self._clients.get(server)
        if client is None:
            state = self.status.get(server, {}).get("state", "")
            if state == STATE_RECONNECTING:
                return {"status": "error", "error": f"MCP 服务 {server} 暂不可用，正在重连"}
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
