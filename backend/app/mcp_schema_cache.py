"""MCP 工具 schema 的磁盘缓存（矩阵 8.7）。

## 为什么需要这一层

每连一个 MCP 服务都要走一次 ``tools/list`` 往返。本地 stdio 服务要冷启动子进程，
远程服务要一次 HTTP 往返——十几个服务加起来就是几秒到十几秒的启动延迟，而且抖动很
明显（服务重启、网络抖一下，工具清单就"变了"或"没了"）。Hermes 的做法是把每个服务
的工具清单**落盘**，启动时先读磁盘（``mcp_schema_cache.py``），只有缓存缺失/失效才真
去问服务。

## 缓存的是 schema，不是结果

这句话必须写在最前面，因为它是这一层唯一的边界：缓存里只有"这个服务声明了哪些工具、
每个工具的入参 JSON Schema 长什么样"，**没有**任何一次 ``tools/call`` 的返回值。工具
结果必须每次真调——它们随时在变，缓存结果等于给模型喂过期数据。同样地，缓存也**不能**
替代连接：执行工具仍然需要活的 client，本模块只负责省掉"列清单"这一步。

## 缓存键与失效规则（照 Hermes 抄，两处刻意加严）

Hermes 的键是「服务名 + 连接配置的指纹」（``config_fingerprint``：command / args /
url / transport / tools 过滤器取 sha256 前 16 位）。**光有服务名不够**——用户把
``command`` 从 ``npx foo`` 改成 ``npx bar``，名字没变，命中旧缓存就会把一个已经不存在的
工具注册给模型。所以 :func:`config_fingerprint` 与 Hermes 同形。

失效规则三条，前两条与 Hermes 一致，第三条是 Hermes 没有而我们需要的：

1. **TTL**：Hermes 读 ``tools/list`` 结果里的 ``ttlMs``（SEP-2549，2026-07-28 协议），
   有提示就按提示过期，没提示就**永不过期**。
2. **配置指纹变了即失效**（见上）。
3. **服务自报的版本变了即失效**（``initialize`` 的 ``serverInfo.version``）。Hermes 的
   懒启动路径没有版本可比，Orbit 每次真连，手上就有这个值——服务升级后工具清单可能
   整批变化，版本一变就该重列。**schema 指纹**同理：真去 ``tools/list`` 拿到了新清单，
   指纹与缓存不同就覆盖（见 :meth:`SchemaCache.put`）。

第 4 条是刻意加严：**没给 ``ttlMs`` 的条目我们仍然给 24 小时上限**
（:data:`DEFAULT_TTL_SECONDS`，取值抄自 mcp SDK 的 ``MAX_TTL_MS`` 常量）。Hermes 让
无提示条目永不过期，是因为它的缓存只为"懒启动时别 spawn 子进程"服务；Orbit 每次装配
都会真连一次，永不过期等于把**某一次**抖动/半连接时拿到的残缺清单永久固化下来——
这比多一次往返贵得多。

第 5 条是本机实测逼出来的，必须写清楚，否则下一个人会以为这里的 TTL 坏了：

**``ttlMs: 0`` 默认不当作"别缓存"。** SEP-2549 的原义是 0 = 不可缓存，Hermes 严格照做
（``elapsed >= 0`` 恒成立 → 永远 miss）。但本机实测发现：mcp 2.x 的 ``MCPServer``
**总是**在 ``tools/list`` 响应里发 ``ttlMs: 0`` 和 ``cacheScope: private``——不是"没给"，
是真的发了个 0（用 ``model_dump(exclude_unset=True)`` 验过，见 ``app/mcp.py`` 的
``cache_hints``）。严格照做的话，所有用这个 SDK 实现的服务（也就是绝大多数，含本地
stdio 服务）永远命中不了缓存，整个功能等于没做。

所以默认把 ``ttlMs == 0`` 当成"没给提示"，退回兜底 TTL；要 SEP-2549 原义请构造
``SchemaCache(path, strict_ttl=True)``。这个取舍可以承受：错的是 **schema** 不是
**结果**，多一次 ``tools/list`` 就能纠正；而"缓存永远不命中"换来的只是白写文件。

## 落盘与降级

``<data_dir>/mcp-schema-cache.json``，**原子写**（写临时文件再 ``os.replace``，避免
写到一半断电留下半个 JSON），带 ``schema_version``（缓存格式将来会变，旧文件必须能被
识别成"不认识的格式"而不是被当成有效缓存解析）。文件损坏 / 版本不认识 / 权限不够——
一律**不抛**：记一条 warning，当作没有缓存。缓存坏了让 MCP 整体不可用，是拿一个优化
换掉一个功能，方向反了。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("orbit.mcp.schema_cache")

#: 缓存文件格式版本。**改字段结构就加一**：旧版本号的文件会被当作"没有缓存"忽略，
#: 而不是按新结构硬解析出一个半对的对象。
SCHEMA_VERSION = 1
#: 落盘文件名（与 Hermes 的 ``mcp_schema_cache.json`` 同义，命名统一用中划线）。
CACHE_FILENAME = "mcp-schema-cache.json"
#: 无 ``ttlMs`` 提示时的兜底 TTL（秒）。24 小时，抄 mcp SDK ``client/caching.py`` 的
#: ``MAX_TTL_MS``——那里它是"服务给的 TTL 上限"，这里借用同一个数字当兜底，理由是
#: 同一个：一天的 schema 漂移可以忽略，超过一天就值得重列一次。
DEFAULT_TTL_SECONDS = 24 * 60 * 60.0

#: 进程内串行化读写。缓存文件是"读-改-写"（一个服务一条），CLI 的 handler 跑在专用
#: 线程上，后端跑在事件循环里，两边可能同时写同一个文件。
_LOCK = threading.Lock()


def config_fingerprint(entry: dict) -> str:
    """连接配置的稳定指纹（sha256 前 16 位）。

    参与哈希的字段与 Hermes 的 ``config_fingerprint`` 相同：``command`` / ``args`` /
    ``url`` / ``transport`` / tools 过滤器（Orbit 目前没有 tools 过滤器，保留空列表
    占位，将来加字段不必换算法）。**额外**加入 ``env`` / ``headers`` 的**键名**（不含
    值）：把 ``keyring:A`` 换成 ``keyring:B`` 会让服务看到完全不同的身份，工具清单跟着
    变；而键名不是秘密，进哈希没有泄漏面。

    ``enabled`` / ``auto_approve`` / ``timeout`` 刻意**不**参与：它们不改变服务声明什么
    工具，改一下批准策略就丢掉缓存是白丢。
    """
    tools_filter = entry.get("tools") if isinstance(entry.get("tools"), dict) else {}
    payload = {
        "command": entry.get("command"),
        "args": [str(item) for item in entry.get("args") or []],
        "url": entry.get("url"),
        "transport": entry.get("transport"),
        "tools_include": sorted(str(item) for item in (tools_filter or {}).get("include") or []),
        "tools_exclude": sorted(str(item) for item in (tools_filter or {}).get("exclude") or []),
        # Orbit 独有：换密钥/换 header 键等于换身份，见 docstring。
        "env_keys": sorted(str(key) for key in (entry.get("env") or {})),
        "header_keys": sorted(str(key) for key in (entry.get("headers") or {})),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def normalize_tools(tools: Iterable[Any]) -> list[dict]:
    """把工具清单规范化成缓存里存的形状：``[{name, description, input_schema}]``。

    只接受**已经是 dict** 的载荷（调用方 ``app/mcp.py`` 负责把 SDK 对象 ``model_dump``
    成 dict）。本模块刻意不 import ``app.mcp``：那个模块会 import 本模块，反向依赖会成环，
    而且会把"缓存"和"SDK 细节"两件事绑在一起——将来 SDK 换字段名，要改的只有一处。
    """
    rows: list[dict] = []
    for item in tools:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        schema = item.get("input_schema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        rows.append(
            {
                "name": name,
                "description": str(item.get("description") or ""),
                "input_schema": schema,
            }
        )
    return rows


def schema_fingerprint(tools: Iterable[Any]) -> str:
    """工具清单的指纹（规范化后取 sha256 前 16 位）。

    用途是回答"服务这次报的清单和缓存里那份是不是同一份"。**先规范化再哈希**：服务
    换个字段顺序、多带一个 ``None``，不该被当成"清单变了"。``description`` 参与哈希
    （描述变了提示词就变了，对模型是有意义的差异），``name`` / ``input_schema`` 当然也
    参与。
    """
    canonical = json.dumps(
        normalize_tools(tools), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class CachedSchema:
    """一条缓存条目。"""

    server_key: str
    #: 连接配置指纹（:func:`config_fingerprint`）。改了 command/url 就对不上。
    config_fingerprint: str
    #: 工具清单指纹（:func:`schema_fingerprint`）。
    schema_fingerprint: str
    #: 服务自报的版本（``initialize`` 的 ``serverInfo.version``）。服务不报时为空串——
    #: 空串**不参与**失效判定（无从比较，不能因为"这次没报"就把缓存判死）。
    server_version: str
    tools: tuple[dict, ...]
    #: 服务给的 ``ttlMs``（SEP-2549）。``None`` = 服务没给提示，按兜底 TTL 处理。
    ttl_ms: float | None
    #: 写入时刻（epoch 秒），TTL 的锚点。
    written_at: float
    #: 服务给的 ``cacheScope``（``public`` / ``private``）。本地磁盘缓存本身就满足
    #: ``private``，这里只记录不判定。
    cache_scope: str = ""

    def age(self, now: float | None = None) -> float:
        """条目年龄（秒）。"""
        return (time.time() if now is None else now) - self.written_at

    def is_fresh(
        self,
        *,
        now: float | None = None,
        default_ttl: float | None = DEFAULT_TTL_SECONDS,
        strict_ttl: bool = False,
    ) -> bool:
        """是否仍然新鲜。

        ``ttl_ms > 0`` 时按它（Hermes / SEP-2549 原样）。``ttl_ms == 0`` 有两种读法，由
        ``strict_ttl`` 决定（见模块 docstring 的"第 4 条"）：

        - ``strict_ttl=True``：SEP-2549 的原义 —— 服务说"别缓存"，就读不到；
        - ``strict_ttl=False``（默认）：退回 ``default_ttl``。理由是本机实测发现 mcp 2.x
          的 ``MCPServer`` **总是**发 ``ttlMs: 0``（不是"没给"，是真的发了个 0），严格读
          会让所有用它实现的服务永远命中不了缓存，这个功能等于没做。

        ``ttl_ms is None``（服务真的没给这个键）时按 ``default_ttl``；传 ``None`` 就是
        Hermes 的原语义"永不过期"。
        """
        age = self.age(now)
        if self.ttl_ms is not None:
            ttl = float(self.ttl_ms) / 1000.0
            if ttl > 0:
                return age < ttl
            if strict_ttl:
                return False
        if default_ttl is None:
            return True
        return age < float(default_ttl)

    def as_dict(self) -> dict:
        """落盘载荷。字段名即 JSON 键名，便于直接对账。"""
        payload: dict[str, Any] = {
            "config_fingerprint": self.config_fingerprint,
            "schema_fingerprint": self.schema_fingerprint,
            "server_version": self.server_version,
            "tools": [dict(item) for item in self.tools],
            "written_at": self.written_at,
        }
        if self.ttl_ms is not None:
            payload["ttl_ms"] = self.ttl_ms
        if self.cache_scope:
            payload["cache_scope"] = self.cache_scope
        return payload

    @classmethod
    def from_dict(cls, server_key: str, payload: Any) -> CachedSchema | None:
        """从落盘载荷还原。**坏载荷返回 None**（当作没有这条缓存），不抛。

        刻意逐字段校验而不是 ``CachedSchema(**payload)``：缓存文件是"我们上次自己写的"，
        但它也可能是旧版本写的、被手改过的、被截断后补齐的。一个字段类型不对就让整个
        MCP 起不来，代价与收益完全不成比例。
        """
        if not isinstance(payload, dict):
            return None
        tools = payload.get("tools")
        if not isinstance(tools, list):
            return None
        written_at = payload.get("written_at")
        if not isinstance(written_at, (int, float)):
            return None
        ttl_ms = payload.get("ttl_ms")
        if ttl_ms is not None and not isinstance(ttl_ms, (int, float)):
            ttl_ms = None
        return cls(
            server_key=server_key,
            config_fingerprint=str(payload.get("config_fingerprint") or ""),
            schema_fingerprint=str(payload.get("schema_fingerprint") or ""),
            server_version=str(payload.get("server_version") or ""),
            tools=tuple(normalize_tools(tools)),
            ttl_ms=None if ttl_ms is None else float(ttl_ms),
            written_at=float(written_at),
            cache_scope=str(payload.get("cache_scope") or ""),
        )


class SchemaCache:
    """``mcp-schema-cache.json`` 的读写门面。"""

    def __init__(
        self,
        path: Path,
        *,
        default_ttl: float | None = DEFAULT_TTL_SECONDS,
        strict_ttl: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.default_ttl = default_ttl
        #: ``ttl_ms == 0`` 是否按 SEP-2549 原义"别缓存"处理。默认 False，理由见
        #: :meth:`CachedSchema.is_fresh` 与模块 docstring 第 4 条。
        self.strict_ttl = strict_ttl
        #: 时钟注入点：TTL 测试必须能"假装过了一天"，而不是真 sleep。
        self.clock = clock

    @classmethod
    def for_settings(cls, settings) -> SchemaCache:
        """按 ``Settings`` 构造：落在数据目录下，跟着数据目录一起迁移/备份。"""
        return cls(Path(settings.data_dir) / CACHE_FILENAME)

    # --- 落盘 ---------------------------------------------------------------
    def _load(self) -> dict[str, Any]:
        """读整个缓存文件。任何问题都降级成空字典 + 一条 warning。"""
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            log.warning("MCP schema 缓存读不出来（%s），按无缓存处理：%s", self.path, exc)
            return {}
        try:
            payload = json.loads(raw)
        except ValueError as exc:
            # 损坏的缓存文件不该让 MCP 整体不可用：这是优化层，不是数据源。
            log.warning("MCP schema 缓存已损坏（%s），按无缓存处理：%s", self.path, exc)
            return {}
        if not isinstance(payload, dict):
            log.warning("MCP schema 缓存顶层不是对象（%s），按无缓存处理", self.path)
            return {}
        version = payload.get("schema_version")
        if version != SCHEMA_VERSION:
            log.warning(
                "MCP schema 缓存格式版本是 %r，本程序认 %r，按无缓存处理",
                version,
                SCHEMA_VERSION,
            )
            return {}
        servers = payload.get("servers")
        return servers if isinstance(servers, dict) else {}

    def _save(self, servers: dict[str, Any]) -> None:
        """原子写：先写同目录临时文件，再 ``os.replace`` 换上去。

        ``os.replace`` 在 Windows 上是原子替换（同卷），所以"断电留下半个 JSON"不会发生。
        权限照 Hermes 的 0o600：缓存文件会被当作可信输入读回，不该让同机其它用户可写。
        """
        # mkdir 也要在 try 里：路径被一个同名文件占住时抛的是 FileExistsError
        # （OSError 的子类），那同样是"写不下去"，不该冒出去。
        payload = {"schema_version": SCHEMA_VERSION, "servers": servers}
        text = json.dumps(payload, ensure_ascii=False, indent=1)
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(text, encoding="utf-8")
            # ``os.chmod`` 在 NTFS 上对权限位是"尽力而为"，失败必须静默——但不能把
            # 写盘本身一起吞掉，所以只包住这一句。
            with suppress(OSError):
                os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            # 写不下去（只读盘、权限、磁盘满）不该让连接失败——缓存是优化，不是依赖。
            log.warning("MCP schema 缓存写不进去（%s）：%s", self.path, exc)
            with suppress(OSError):
                tmp.unlink(missing_ok=True)

    # --- 读 -----------------------------------------------------------------
    def get(
        self,
        server_key: str,
        *,
        config: dict | None = None,
        server_version: str = "",
        schema_fingerprint: str = "",
        now: float | None = None,
    ) -> CachedSchema | None:
        """取一条缓存；不新鲜 / 身份对不上 / 清单指纹对不上都返回 ``None``。

        ``config`` / ``server_version`` / ``schema_fingerprint`` 都是**可选的校验依据**：
        调用方能提供就提供，提供多少校验多少。三条判据对应模块 docstring 里的失效规则。
        读路径**不写盘**：一次未命中就顺手删条目，会让并发读变成"互相删缓存"。
        """
        moment = self.clock() if now is None else now
        with _LOCK:
            entry = CachedSchema.from_dict(server_key, self._load().get(server_key))
        if entry is None:
            return None
        if not entry.is_fresh(
            now=moment, default_ttl=self.default_ttl, strict_ttl=self.strict_ttl
        ):
            return None
        if config is not None and entry.config_fingerprint != config_fingerprint(config):
            return None
        if server_version and entry.server_version and entry.server_version != server_version:
            return None
        if schema_fingerprint and entry.schema_fingerprint != schema_fingerprint:
            return None
        return entry

    # --- 写 -----------------------------------------------------------------
    def put(
        self,
        server_key: str,
        tools: Iterable[Any],
        *,
        server_version: str,
        config: dict | None = None,
        ttl_ms: float | None = None,
        cache_scope: str = "",
        now: float | None = None,
    ) -> CachedSchema:
        """写入（或覆盖）一条缓存，返回写进去的那条。

        覆盖是**无条件的**：真去 ``tools/list`` 拿到清单后，缓存就该变成这次看到的样子
        ——配置指纹变了、版本变了、schema 指纹变了，三种情况的结果都是"覆盖"，区别只在
        于要不要记一条 debug 日志（写下来，排查"工具怎么少了一个"时有用）。
        """
        moment = self.clock() if now is None else now
        rows = normalize_tools(tools)
        entry = CachedSchema(
            server_key=server_key,
            config_fingerprint=config_fingerprint(config or {}),
            schema_fingerprint=schema_fingerprint(rows),
            server_version=str(server_version or ""),
            tools=tuple(rows),
            ttl_ms=None if ttl_ms is None else float(ttl_ms),
            written_at=moment,
            cache_scope=str(cache_scope or ""),
        )
        with _LOCK:
            servers = self._load()
            previous = CachedSchema.from_dict(server_key, servers.get(server_key))
            if previous is not None:
                if previous.config_fingerprint != entry.config_fingerprint:
                    log.debug("MCP %s 的连接配置变了，schema 缓存作废重写", server_key)
                elif previous.server_version != entry.server_version:
                    log.debug(
                        "MCP %s 的服务版本 %r → %r，schema 缓存作废重写",
                        server_key,
                        previous.server_version,
                        entry.server_version,
                    )
                elif previous.schema_fingerprint != entry.schema_fingerprint:
                    log.debug("MCP %s 的工具清单变了，schema 缓存重写", server_key)
                elif previous.tools == entry.tools and previous.ttl_ms is None and entry.ttl_ms is None:
                    # 逐字节相同且都不带 TTL：不必重写文件（Hermes 的 write-through 去抖）。
                    # 带 TTL 的条目**必须**重写：written_at 要往前挪，否则无论重连多少次，
                    # 条目都会在**第一次**写入的时刻到期。
                    return previous
            servers[server_key] = entry.as_dict()
            self._save(servers)
        return entry

    def invalidate(self, server_key: str) -> bool:
        """删掉一条缓存。返回是否真删了（"本来就没有"不算失败）。"""
        with _LOCK:
            servers = self._load()
            if server_key not in servers:
                return False
            del servers[server_key]
            self._save(servers)
        return True

    def clear(self) -> int:
        """清空全部缓存，返回删掉的条数。文件本身保留（写一个空表）。"""
        with _LOCK:
            servers = self._load()
            count = len(servers)
            if count:
                self._save({})
        return count

    def prune(self, max_age: float, *, now: float | None = None) -> list[str]:
        """删掉年龄超过 ``max_age`` 秒的条目，返回被删的服务名。

        与 TTL 的区别：TTL 决定"还能不能用"，``prune`` 决定"还留不留着"。过期的条目
        留着没用（读的时候必然 miss），但也不会伤人；真正需要 ``prune`` 的是**已经不在
        配置里**的旧服务名——那些条目再也不会被读，只会在文件里堆积。
        """
        moment = self.clock() if now is None else now
        removed: list[str] = []
        with _LOCK:
            servers = self._load()
            for key in list(servers):
                entry = CachedSchema.from_dict(key, servers[key])
                if entry is None or entry.age(moment) > float(max_age):
                    del servers[key]
                    removed.append(key)
            if removed:
                self._save(servers)
        return removed

    def entries(self, *, now: float | None = None) -> list[dict]:
        """全部条目的公开视图（`mcp cache show` 用）：不含工具正文，只报个数与新鲜度。"""
        moment = self.clock() if now is None else now
        rows: list[dict] = []
        with _LOCK:
            servers = self._load()
        for key in sorted(servers):
            entry = CachedSchema.from_dict(key, servers[key])
            if entry is None:
                # 坏条目照样列出来：`mcp cache prune` 就是用来清这种东西的。
                rows.append(
                    {
                        "server": key,
                        "tools": 0,
                        "age": None,
                        "fresh": False,
                        "version": "",
                        "ttl_ms": None,
                        "broken": True,
                    }
                )
                continue
            rows.append(
                {
                    "server": key,
                    "tools": len(entry.tools),
                    "age": round(entry.age(moment), 1),
                    "fresh": entry.is_fresh(
                        now=moment, default_ttl=self.default_ttl, strict_ttl=self.strict_ttl
                    ),
                    "version": entry.server_version,
                    "ttl_ms": entry.ttl_ms,
                    "config_fingerprint": entry.config_fingerprint,
                    "schema_fingerprint": entry.schema_fingerprint,
                    "broken": False,
                }
            )
        return rows


__all__ = [
    "CACHE_FILENAME",
    "DEFAULT_TTL_SECONDS",
    "SCHEMA_VERSION",
    "CachedSchema",
    "SchemaCache",
    "config_fingerprint",
    "normalize_tools",
    "schema_fingerprint",
]
