"""Connect external MCP servers and expose their tools to the agent.

Design notes:

- Servers are declared in ``data/mcp.json``. Nothing is trusted by default: every tool a
  server provides needs confirmation, and only an explicit ``auto_approve`` on that server
  changes that. MCP tools are never covered by site-scoped grants.
- One persistent client per server, kept open for the life of the app. A server that fails
  to start is reported with its error and does not stop the others.
- Secrets never live in ``mcp.json``: values written as ``keyring:<name>`` are resolved
  from the system credential store under ``mcp:<server>:<name>``.
- **Tool schemas are cached on disk** (``app/mcp_schema_cache.py``): the ``tools/list``
  round-trip is skipped when a fresh entry exists. What is cached is the *schema*, never a
  call result — a stale tool result is a wrong answer, a stale schema only costs one
  re-list. Invalidation is three-way (connection fingerprint / server-reported version /
  tools fingerprint), so editing ``command`` or upgrading the server cannot silently keep
  a manifest that no longer matches.
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

from app.mcp_schema_cache import SchemaCache
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


def _explicit_fields(obj) -> dict:
    """只取**服务真的发过**的字段（``model_dump(exclude_unset=True)``）。

    与 :func:`dumped` 的区别是"默认值算不算数"：``dumped`` 会把 pydantic 的默认值一起
    摊出来，而判断"服务有没有给某个提示"时必须只看显式字段。拿不到 ``exclude_unset``
    （非 pydantic 对象、老 SDK）就退回 ``dumped``——那只是失去区分能力，不该抛。
    """
    try:
        if hasattr(obj, "model_dump"):
            return obj.model_dump(exclude_unset=True)
    except Exception:  # noqa: BLE001 - 同 dumped：坏载荷不该打断连接
        return {}
    return dumped(obj)


def tool_shapes(tools) -> list[dict]:
    """把 SDK 的工具对象摊平成 ``[{name, description, input_schema}]``。

    这个形状是**两条路共用**的：真去 ``tools/list`` 拿到的清单、以及磁盘缓存里存着的
    清单，都要先变成它，注册工具的那段代码才只有一份。少一份就等于多一处会分叉的地方
    ——缓存路径注册出来的工具与在线路径不一致，是这类缓存最典型的坑。
    """
    rows: list[dict] = []
    for remote in tools:
        payload = dumped(remote)
        label = str(payload.get("name") or "tool")
        rows.append(
            {
                "name": label,
                "description": str(payload.get("description") or label)[:1000],
                "input_schema": schema_of(remote),
            }
        )
    return rows


def cache_hints(listing) -> tuple[float | None, str]:
    """读 ``tools/list`` 结果里的 SEP-2549 提示：``(ttl_ms, cache_scope)``。

    **必须区分"服务没给 ttlMs"和"服务给了 0"**：SDK 的 ``ListToolsResult.ttl_ms`` 默认值
    就是 ``0``，普通 ``model_dump()`` 把两者都摊成 ``0``。而这两件事的后果完全相反——
    没给提示时该用兜底 TTL（Hermes 的做法是"永不过期"，我们加了 24 小时上限），给了 0
    时是 SEP-2549 明说的"别缓存"。混为一谈会让**所有不实现 SEP-2549 的服务**（今天的
    绝大多数，尤其本地 stdio 服务）永远命中不了缓存，整个功能等于没做。

    所以这里走 ``model_dump(exclude_unset=True)``：只有服务真的发过这个键，它才会出现。
    """
    payload = _explicit_fields(listing)
    ttl = payload.get("ttl_ms")
    return (
        float(ttl) if isinstance(ttl, (int, float)) and not isinstance(ttl, bool) else None,
        str(payload.get("cache_scope") or ""),
    )


def server_version(client) -> str:
    """服务自报的版本（``initialize`` 的 ``serverInfo.version``），拿不到返回空串。

    空串在缓存层意味着"无从比较"——**不参与**失效判定。2026 之后的服务可以不报身份，
    把"这次没报"当成"版本变了"会让缓存永远命中不了。
    """
    try:
        info = client.server_info
    except Exception:  # noqa: BLE001 - 老连接/半连接都可能没有这个属性
        return ""
    if info is None:
        return ""
    return str(dumped(info).get("version") or "")


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
        #: 工具 schema 的磁盘缓存（矩阵 8.7）。**缓存的是 schema，不是调用结果**：
        #: 省掉的是 ``tools/list`` 这一次往返，执行工具仍然需要活的 client。
        self.schema_cache = SchemaCache.for_settings(settings)
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
                shapes = await self._tool_shapes(entry, client)
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
        for shape in shapes:
            label = shape["name"]
            tool = Tool(
                name=tool_name(name, label),
                description=shape["description"],
                confirmation=not bool(entry.get("auto_approve")),
                parameters=shape["input_schema"],
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

    async def _tool_shapes(self, entry: dict, client) -> list[dict]:
        """取工具清单：**先读磁盘缓存，未命中才真去 ``tools/list``**。

        缓存的是 schema 不是结果——这里省掉的只是"列清单"这一次往返，工具调用仍然走
        :meth:`execute` 的真连接。三条失效判据（配置指纹 / 服务版本 / schema 指纹）由
        :class:`~app.mcp_schema_cache.SchemaCache` 负责，本方法只负责"按结果做决定"。

        未命中时顺手把新清单写回缓存：``ttl_ms`` / ``cache_scope`` 原样带上（服务说了算），
        服务没给就交给缓存层的兜底 TTL。
        """
        name = str(entry["name"])
        version = server_version(client)
        cached = self.schema_cache.get(name, config=entry, server_version=version)
        if cached is not None:
            log.debug("MCP %s 命中 schema 缓存：%d 个工具（跳过 tools/list）", name, len(cached.tools))
            return [dict(item) for item in cached.tools]
        listing = await client.list_tools()
        shapes = tool_shapes(listing.tools)
        ttl_ms, cache_scope = cache_hints(listing)
        self.schema_cache.put(
            name,
            shapes,
            server_version=version,
            config=entry,
            ttl_ms=ttl_ms,
            cache_scope=cache_scope,
        )
        return shapes

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


# ══════════════════════════════════════════════════════════════════════════════
# W7 追加区：单服务探测 / OAuth token 存储 / 带扩展字段的写盘
#
# 为什么是"追加"而不是改既有实现：上面两个入口各有它服务的调用方，语义不能动——
#
# - ``MCPManager.start()`` 是**常驻**入口：连接全部服务、注册工具、起看门狗和后台
#   重连。CLI 的 `mcp test` 只需要"连一次、问清楚它有什么、断开"，走 start() 会顺带
#   起一堆永不自行退出的 task（``close()`` 之前一直挂着），副作用远大于需求。
# - ``save_servers()`` 是对外配置的**字段白名单**（env/headers/args/…），多余字段被
#   有意丢弃。而 `mcp add --connect-timeout/--auth` 要落的 ``timeout`` / ``auth``
#   恰好在这个白名单之外，所以单开一个写盘函数，而不是放宽白名单。
#
# 本段刻意**不新增文件级 import**（ruff 的 E402 不允许文件中部 import）：需要时间的
# 地方走 :func:`_epoch_now`，函数内 import 一次。
# ══════════════════════════════════════════════════════════════════════════════

#: `save_servers` 白名单之外、W7 新增的服务字段（`mcp add` 要落、连接时要读）。
EXTRA_SERVER_FIELDS = ("timeout", "auth")

#: keyring 里 OAuth token 的键前缀（与 `mcp:<server>:<name>` 的密钥键区分开）。
OAUTH_KEY_PREFIX = "oauth:"
#: 提前多少秒把 token 判成"即将过期"：网络往返与系统时钟偏差都要留余量，
#: 否则会在请求刚发出时过期，报出来的却是 401 —— 那是最难归因的一类失败。
OAUTH_EXPIRY_SKEW = 60.0


def _epoch_now() -> float:
    """当前 epoch 秒（浮点）。

    在函数内 import 而不是文件顶部：本段是追加区，不碰既有导入块（见段首说明）。
    """
    import time

    return time.time()


def _capability(client, name: str) -> bool:
    """服务是否声明了某项能力（``prompts`` / ``resources``）。

    先读 ``initialize_result`` 里的能力声明再决定要不要发请求——这是 Hermes 的
    "能力声明前置"：没声明的服务被调用会抛 ``Method not found``，把它当成"空清单"
    处理会让"服务不支持"和"服务出错了"两件事混成同一个现象。
    """
    try:
        caps = client.server_capabilities
    except Exception:  # noqa: BLE001 - 拿不到能力声明按"不支持"处理
        return False
    return bool(dumped(caps).get(name))


def _prompt_arguments(prompt) -> list[dict]:
    """把 prompt 的参数声明摊平成 ``[{name, required, description}]``。"""
    rows: list[dict] = []
    for item in dumped(prompt).get("arguments") or []:
        if not isinstance(item, dict):
            continue
        label = str(item.get("name") or "")
        if not label:
            continue
        rows.append(
            {
                "name": label,
                "required": bool(item.get("required", False)),
                "description": str(item.get("description") or ""),
            }
        )
    return rows


async def probe(settings, entry: dict, *, limit: float | None = None) -> dict:
    """连一次某个服务，报告它提供什么，然后断开（`mcp test` / `mcp prompts` 用）。

    与 :meth:`MCPManager.start` 的区别是**一次性且无残留**：自己一个 ``AsyncExitStack``，
    不注册工具、不起看门狗、不进 ``self.status``。失败也如实返回（``ok=False`` +
    ``error``），不抛——CLI 要把失败原因原样打给用户，而不是吞成栈。

    超时优先取调用方给的 ``limit``，其次服务配置里的 ``timeout``，最后 ``CONNECT_TIMEOUT``。
    （参数名不用 ``timeout``：ruff 的 ASYNC109 认为异步函数带这个名字是在自己造超时机制，
    而这里统一走 ``asyncio.timeout``。）
    """
    name = str(entry.get("name") or "")
    limit = float(limit or entry.get("timeout") or CONNECT_TIMEOUT)
    report: dict = {
        "name": name,
        "ok": False,
        "error": "",
        "tools": [],
        "prompts": [],
        "resources": [],
        "templates": [],
        #: schema 缓存这一侧发生了什么（``refreshed`` + 工具数）。`mcp test` 会显示它，
        #: 否则"缓存到底有没有在起作用"只能靠猜。
        "cache": {},
    }
    manager = MCPManager(settings)
    inner = AsyncExitStack()
    try:
        async with asyncio.timeout(limit):
            client = await inner.enter_async_context(manager._client(entry))
            listing = await client.list_tools()
            shapes = tool_shapes(listing.tools)
            report["tools"] = [
                {"name": shape["name"], "description": shape["description"]} for shape in shapes
            ]
            # 体检命令**必须看真实结果**（读缓存等于自己骗自己），但顺手刷新缓存是有益的：
            # 这是唯一一个"人主动要求重列清单"的入口，刷新后下一次装配就不用再等往返。
            ttl_ms, cache_scope = cache_hints(listing)
            manager.schema_cache.put(
                name,
                shapes,
                server_version=server_version(client),
                config=entry,
                ttl_ms=ttl_ms,
                cache_scope=cache_scope,
            )
            report["cache"] = {"refreshed": True, "tools": len(shapes)}
            if _capability(client, "prompts"):
                prompts = await client.list_prompts()
                report["prompts"] = [
                    {
                        "name": str(dumped(prompt).get("name") or ""),
                        "description": str(dumped(prompt).get("description") or ""),
                        "arguments": _prompt_arguments(prompt),
                    }
                    for prompt in prompts.prompts
                ]
            if _capability(client, "resources"):
                # 只取第一页：这是"体检"命令，翻 50 页只为报个数不值得多等几秒。
                resources = await client.list_resources()
                report["resources"] = [
                    {
                        "uri": str(dumped(item).get("uri") or ""),
                        "name": str(dumped(item).get("name") or ""),
                        "mime_type": str(dumped(item).get("mime_type") or ""),
                        "size": dumped(item).get("size"),
                    }
                    for item in resources.resources
                ]
                templates = await client.list_resource_templates()
                report["templates"] = [
                    {
                        "uri_template": str(dumped(item).get("uri_template") or ""),
                        "name": str(dumped(item).get("name") or ""),
                    }
                    for item in templates.resource_templates
                ]
    except TimeoutError:
        report["error"] = f"连接或发现工具超过 {limit:g} 秒未完成"
        return report
    except Exception as exc:  # noqa: BLE001 - 探测失败是预期结果，原样报给用户
        report["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        return report
    finally:
        # 顺序要紧：先关 HTTP 客户端（远程服务），再收 stdio 子进程。
        for http_client in manager._http_clients:
            with suppress(Exception):
                await http_client.aclose()
        with suppress(Exception):
            await inner.aclose()
    report["ok"] = True
    return report


class TokenRecord:
    """一个 MCP 服务的 OAuth token。

    刻意不用 ``@dataclass``：本段不新增文件级 import（见段首说明），而 keyring 里
    存的是 JSON，字段少且都带默认值，普通类足够。
    """

    __slots__ = (
        "server",
        "access_token",
        "refresh_token",
        "token_type",
        "expires_at",
        "obtained_at",
        "scope",
    )

    def __init__(
        self,
        server: str,
        access_token: str,
        *,
        refresh_token: str = "",
        token_type: str = "Bearer",
        expires_at: float = 0.0,
        obtained_at: float = 0.0,
        scope: str = "",
    ) -> None:
        self.server = server
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.token_type = token_type
        #: 绝对到期时刻（epoch 秒）。**0 = 服务没给 expires_in，即"不知道何时过期"**，
        #: 与"已过期"是两件事，不能混（混了会把长效 token 每次判死）。
        self.expires_at = float(expires_at or 0.0)
        self.obtained_at = float(obtained_at or 0.0)
        self.scope = scope

    def as_dict(self) -> dict:
        """落 keyring 的 JSON 载荷。字段名与 OAuth 令牌响应同名，便于直接对账。"""
        return {
            "server": self.server,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "obtained_at": self.obtained_at,
            "scope": self.scope,
        }

    @classmethod
    def from_payload(cls, server: str, payload) -> "TokenRecord | None":
        """从 keyring 读回的 JSON 还原。坏载荷返回 None（宁可报"未配置"也不要崩）。"""
        if not isinstance(payload, dict):
            return None
        token = str(payload.get("access_token") or "")
        if not token:
            return None
        try:
            expires_at = float(payload.get("expires_at") or 0.0)
            obtained_at = float(payload.get("obtained_at") or 0.0)
        except (TypeError, ValueError):
            return None
        return cls(
            server,
            token,
            refresh_token=str(payload.get("refresh_token") or ""),
            token_type=str(payload.get("token_type") or "Bearer"),
            expires_at=expires_at,
            obtained_at=obtained_at,
            scope=str(payload.get("scope") or ""),
        )

    def seconds_left(self, now: float | None = None) -> float | None:
        """距到期的秒数；``expires_at == 0``（未知）返回 None，不是 0。"""
        if self.expires_at <= 0:
            return None
        return self.expires_at - (_epoch_now() if now is None else now)

    def is_expired(self, now: float | None = None, *, skew: float = 0.0) -> bool:
        """是否已过期（``skew`` 提前量用于"即将过期"判定）。未知到期时间 → False。"""
        left = self.seconds_left(now)
        return left is not None and left <= skew


class OAuthStore:
    """MCP OAuth token 的存储与过期检查（keyring 后端）。

    **本轮只做这一半**：存取 + 过期判定。真正的授权流程（发现 authorization server
    元数据 → PKCE → 本地回调 HTTP 服务器 → 换 token）没有做，原因写在
    ``app/cli/commands/mcp.py`` 的 `mcp auth login` 报错文案里：它需要开监听端口并等
    浏览器回调，本机无网络、也没有真实 OAuth 服务可联调，做出来只能是不可验证的壳。

    服务名与既有密钥一致：``service = f"orbit:{data_dir}"``，于是同一个数据目录下的
    MCP 密钥与 token 落在同一个 vault 条目下，卸载/迁移时一起走。
    """

    def __init__(self, service: str) -> None:
        self.service = service

    @classmethod
    def for_settings(cls, settings) -> "OAuthStore":
        """按 ``Settings`` 构造：service 名与 `MCPManager.service` 同源。"""
        return cls(f"orbit:{Path(settings.data_dir).resolve()}")

    def _key(self, server: str) -> str:
        return f"{OAUTH_KEY_PREFIX}{server}"

    def set(
        self,
        server: str,
        access_token: str,
        *,
        refresh_token: str = "",
        expires_in: float = 0.0,
        token_type: str = "Bearer",
        scope: str = "",
        now: float | None = None,
    ) -> TokenRecord:
        """存一个 token。``expires_in`` 是相对秒数（OAuth 令牌响应的写法），这里换算成
        绝对时刻再落盘——存相对值的话，进程重启后"还剩多少秒"就无从算起。"""
        moment = _epoch_now() if now is None else now
        try:
            ttl = float(expires_in or 0.0)
        except (TypeError, ValueError):
            ttl = 0.0
        record = TokenRecord(
            server,
            str(access_token),
            refresh_token=str(refresh_token or ""),
            token_type=str(token_type or "Bearer"),
            expires_at=(moment + ttl) if ttl > 0 else 0.0,
            obtained_at=moment,
            scope=str(scope or ""),
        )
        keyring.set_password(self.service, self._key(server), json.dumps(record.as_dict()))
        return record

    def get(self, server: str) -> TokenRecord | None:
        """读 token。keyring 不可用或载荷坏了都返回 None，由调用方报"未配置"。"""
        try:
            raw = keyring.get_password(self.service, self._key(server))
        except Exception:  # noqa: BLE001 - 后端不可用（无 vault）不该让命令崩
            return None
        if not raw:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        return TokenRecord.from_payload(server, payload)

    def clear(self, server: str) -> bool:
        """删 token。返回是否真的删掉了一条（"本来就没有"不算失败）。"""
        if self.get(server) is None:
            return False
        with suppress(Exception):
            keyring.delete_password(self.service, self._key(server))
            return True
        return False

    def status(
        self, server: str, *, now: float | None = None, skew: float = OAUTH_EXPIRY_SKEW
    ) -> dict:
        """一个服务的 token 状态：``missing`` / ``expired`` / ``expiring`` / ``valid``。

        ``expiring`` 与 ``valid`` 分开，是因为两者的处置不同：前者该续期，后者什么都不用做。
        """
        record = self.get(server)
        if record is None:
            return {"server": server, "state": "missing", "expires_at": 0.0, "seconds_left": None}
        left = record.seconds_left(now)
        if left is None:
            state = "valid"
        elif left <= 0:
            state = "expired"
        elif left <= skew:
            state = "expiring"
        else:
            state = "valid"
        return {
            "server": server,
            "state": state,
            "expires_at": record.expires_at,
            "seconds_left": None if left is None else round(left, 1),
        }


def write_servers(manager: "MCPManager", servers: list[dict]) -> list[dict]:
    """把服务清单写回 ``mcp.json``，并保留 ``save_servers`` 白名单外的 W7 字段。

    两步而不是一步，理由是把"校验"和"字段保留"分开：

    1. ``manager.save_servers()`` 做全部校验（name / command-or-url）与规范化并落盘；
    2. 再把 ``timeout`` / ``auth`` 补回文件。

    为什么不给 ``save_servers`` 加字段：它是对外配置的规范化入口，放宽白名单会改变
    既有调用方的行为（多余字段不再被丢弃），而那条语义是有意为之的。
    """
    manager.save_servers(servers)
    extras: dict[str, dict] = {}
    for entry in servers:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        picked = {
            field: entry[field]
            for field in EXTRA_SERVER_FIELDS
            if entry.get(field) not in (None, "", False)
        }
        if name and picked:
            extras[name] = picked
    if not extras:
        return manager.servers()
    payload = json.loads(manager.path.read_text(encoding="utf-8"))
    for record in payload.get("servers") or []:
        extra = extras.get(str(record.get("name") or ""))
        if extra:
            record.update(extra)
    manager.path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manager.servers()


# ══════════════════════════════════════════════════════════════════════════════
# W8 追加区：OAuth 授权流程的"本地那一半"（矩阵 8.3 收尾）
#
# 为什么落在 ``app/mcp_oauth_flow.py`` 而不是这里：那边全是纯函数（PKCE / URL 拼装 /
# 请求体 / 响应解析 / 过期判定），可以脱离网络与 keyring 单测；本模块是**装配层**，
# 只做"从 settings + 服务配置里取出需要的东西，交给纯函数，再把结果翻译成 CLI 能打的
# 报告"。把它塞进这里会让 1160 行的 mcp.py 再长 300 行，而它服务的是 CLI 的
# `login` / `reauth` 两条命令。
#
# 两个函数都用函数内 import：本段是追加区，不新增文件级 import（同 W7 段首说明）。
# ══════════════════════════════════════════════════════════════════════════════


def oauth_login_report(settings, entry: dict, *, now: float | None = None) -> dict:
    """`mcp login` / `mcp reauth` 的**本地**报告：令牌现状 + 能刷新吗 + 授权 URL。

    能真做的三件事都在这里：读已有令牌、判过期、拼出"真流程会打开的那条 authorize
    URL"（含真 PKCE challenge）。真正的换 token 需要出网 + 浏览器 + 本地回调服务器，
    由 ``mcp_oauth_flow.run_flow`` 如实抛 ``NetworkUnavailable``——**不在这里假装**。

    返回的 ``code_verifier`` 是秘密（PKCE 的另一半），只在本次调用里存在，不落盘。
    """
    from app.mcp_oauth_flow import login_report

    return login_report(settings, entry, now=now)


def oauth_refresh(settings, entry: dict, *, transport=None, now: float | None = None) -> dict:
    """尝试用 refresh token 续期（`mcp reauth` 的第二步）。

    三种"不用刷新"的情形直接返回结论而不是报错：没令牌、令牌还有效、没有 refresh
    token。真去刷新时若没有注入 ``transport``，``mcp_oauth_flow.refresh_token`` 会抛
    ``NetworkUnavailable``（本机无网络）——调用方负责把它原样打给用户。
    """
    from app.mcp_oauth_flow import TokenStore, is_expired, refresh_token

    name = str(entry.get("name") or "")
    store = TokenStore.for_settings(settings, name)
    token = store.load()
    if token is None:
        return {"ok": False, "server": name, "reason": "missing", "refreshed": False}
    if not is_expired(token, now=now):
        return {
            "ok": True,
            "server": name,
            "reason": "valid",
            "refreshed": False,
            "expires_at": token.expires_at,
        }
    if not token.refresh_token:
        return {"ok": False, "server": name, "reason": "no_refresh_token", "refreshed": False}
    fresh = refresh_token(
        {
            "url": entry.get("url") or "",
            "client_id": entry.get("client_id") or "",
            "scope": entry.get("scope") or "",
            "token_endpoint": entry.get("token_endpoint") or "",
            "redirect_uri": entry.get("redirect_uri") or "",
        },
        token,
        transport=transport,
        store=store,
    )
    return {
        "ok": True,
        "server": name,
        "reason": "refreshed",
        "refreshed": True,
        "expires_at": fresh.expires_at,
    }
