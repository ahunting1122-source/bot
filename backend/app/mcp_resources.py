"""MCP resources → RAG 索引（增量 + 二进制跳过）。

## 复用哪一套检索，为什么

**不新造向量库，也不新造切块器**。这里只做"从 MCP 取正文 → 交给既有写入侧"这一段：

- 切块：``app.indexing.chunks_for_resource(uri, body, server)``——它已经把 MCP resource
  的语义钉死了（``source_type="mcp_resource"``、``origin=uri``、``host="mcp:<server>"``、
  ``source_id/conversation_id/task_id`` 全空，因为资源不属于任何一次任务）。
- 写入：``app.indexing.FtsIndexer.write(chunks)``——双路 FTS5（trigram + 逐字），按
  ``(origin, hash)`` 判重。
- 读取：``app.retrieval.FtsRetriever.search(..., source_type="mcp_resource", scope="global")``，
  也就是 :func:`search_resources`。三路召回（trigram / chars / vector）+ RRF + 重排都在
  `app/retrieval.py` 里，本模块一行都不重复实现。

换句话说：**资源进 RAG 之后，和网页正文、工具结果走同一条检索链路**。单独一套索引会让
"为什么这条资源搜不到"变成要同时怀疑两套切块与两套打分的问题。

## 增量判据：``(uri, etag/mtime/size)``

每个资源记两个签名：

- ``list_signature``：**不读正文**就能从 ``list_resources`` 拿到的信号（``_meta.etag`` /
  ``annotations.last_modified`` / ``size``）。命中即"没变"，省掉一次 ``read_resource``
  往返——这是增量真正的收益所在。
- ``content_signature``：正文的 sha256。列表没给任何信号时它是唯一判据；给了信号时它
  兜住"etag 没变但内容变了"这种服务端不老实的情况。

内容变了要先**删旧块**再写新块：``FtsIndexer.write`` 按 ``(origin, hash)`` 判重，
只写不删的话，同一个 URI 的旧版本会一直留在索引里，检索出来的是过期内容——比索引不到更糟。

资源从列表里消失时同样删块（``dropped``）：索引里留着已经不存在于服务的 URI，模型引用
它时会拿到一个无法回溯的出处。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.indexing import chunks_for_resource
from app.mcp import (
    RESOURCE_FETCH_GAP,
    RESOURCE_MAX_PAGES,
    RESOURCE_READ_TIMEOUT,
    dumped,
)

log = logging.getLogger("orbit.mcp.resources")

#: 明确的文本 MIME 白名单（其余 ``application/*`` 一律当二进制，不猜）。
TEXT_MIME_EXACT = frozenset(
    {
        "application/json",
        "application/ld+json",
        "application/xml",
        "application/xhtml+xml",
        "application/yaml",
        "application/x-yaml",
        "application/toml",
        "application/javascript",
        "application/typescript",
        "application/sql",
        "application/graphql",
        "application/csv",
        "application/x-sh",
        "application/x-ndjson",
    }
)

#: 状态文件版本号。改结构就升它，旧文件按"空状态"处理（宁可全量重建，不要读错）。
STATE_VERSION = 1


def _shape(value) -> dict:
    """读一个 SDK 对象**或普通 dict**。

    `app.mcp.dumped` 只认带 ``model_dump`` 的 pydantic 模型（dict 会得到空字典），而本
    模块的输入既可能来自真实 MCP 客户端，也可能来自测试/离线快照里的 dict。
    """
    if isinstance(value, dict):
        return value
    return dumped(value)


def _items(result, key: str) -> list:
    """从 ``list_*`` 结果里取条目列表（SDK 对象走属性，dict 走键）。"""
    rows = getattr(result, key, None)
    if rows is None:
        rows = _shape(result).get(key)
    return list(rows or [])


def is_textual(mime: str) -> bool:
    """这个 MIME 是否可按文本索引。

    **没声明 MIME 时返回 True**：很多服务不填 ``mime_type``，把"未声明"当成二进制会
    把绝大多数资源静默跳过——那是最坏的一种"看起来跑了"。读回来之后还有一道
    ``blob`` 检查兜底（见 :meth:`ResourceIndexer._read`）。
    """
    value = (mime or "").split(";")[0].strip().lower()
    if not value:
        return True
    if value.startswith("text/"):
        return True
    if value.endswith(("+json", "+xml", "+yaml")):
        return True
    return value in TEXT_MIME_EXACT


def binary_reason(mime: str) -> str:
    """二进制资源的跳过原因（写进报告，给 `mcp resources` 打出来）。"""
    return f"二进制资源（{mime or '未声明 MIME'}），未索引：本层只索引文本，不解码图片/PDF/音视频"


def listing_signature(shape: dict) -> str:
    """不读正文就能算出的增量签名；列表里没给任何信号则返回空串。

    取 ``_meta`` 与 ``annotations`` 两处，是因为 MCP 规范把这类"带外元数据"放在
    ``_meta``，而各家服务历史上也塞过 ``annotations.last_modified``。

    **把所有可用信号拼起来，而不是取第一个就返回**。早先的写法是「找到 etag 就
    返回」，于是「etag 没变但 size 变了」这种服务端 bug 完全看不出来——而 etag
    写错恰恰是自建 MCP 服务最常见的问题。信号越全，假的"未变"越少。

    代价：拼接后的签名与旧版不同，**升级后第一次会全量重建一遍**。这是一次性成本，
    而漏掉一次真实变更是持续的错——用一次重抓换长期正确，划算。
    """
    parts: list[str] = []
    for bucket in (shape.get("meta"), shape.get("annotations")):
        if not isinstance(bucket, dict):
            continue
        for key in ("etag", "ETag", "lastModified", "last_modified", "updated_at"):
            value = bucket.get(key)
            if value not in (None, ""):
                parts.append(f"{key}:{value}")
    size = shape.get("size")
    if size not in (None, ""):
        parts.append(f"size:{size}")
    return "|".join(parts)


def content_signature(body: str) -> str:
    """正文签名。列表不给信号时它就是唯一判据。"""
    return "sha256:" + hashlib.sha256((body or "").encode("utf-8")).hexdigest()


@dataclass
class ResourceReport:
    """一次索引的完整结果。**每一类都要有数**，否则"跳过"会变成看不见的失败。"""

    server: str
    indexed: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    templates: list[dict[str, str]] = field(default_factory=list)
    chunks: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "indexed": list(self.indexed),
            "unchanged": list(self.unchanged),
            "skipped": [dict(item) for item in self.skipped],
            "dropped": list(self.dropped),
            "errors": [dict(item) for item in self.errors],
            "templates": [dict(item) for item in self.templates],
            "chunks": self.chunks,
        }


class ResourceIndexer:
    """把 MCP resources 增量索引进 RAG。

    ``indexer`` 只要满足 ``async write(chunks) -> int``（生产里是 ``FtsIndexer``）；
    额外需要"删旧块"时才会用到它的 ``.store``。没有 ``.store`` 的假索引器照样能跑，
    只是删不掉旧块——这一点会在报告里体现（``dropped`` 只记状态变化）。
    """

    def __init__(
        self,
        indexer,
        state_path: Path | str | None = None,
        *,
        max_pages: int = RESOURCE_MAX_PAGES,
        gap: float = RESOURCE_FETCH_GAP,
        read_timeout: float = RESOURCE_READ_TIMEOUT,
    ) -> None:
        self.indexer = indexer
        self.state_path = Path(state_path) if state_path is not None else None
        self.max_pages = max(1, int(max_pages))
        self.gap = max(0.0, float(gap))
        self.read_timeout = max(0.0, float(read_timeout))
        #: ``{server: {uri: {"signature":…, "list_signature":…, "chunks":…, "indexed_at":…}}}``
        self._state: dict[str, dict[str, dict[str, Any]]] = self._load()
        self._reports: dict[str, ResourceReport] = {}

    # —— 状态持久化 ——

    def _load(self) -> dict[str, dict[str, dict[str, Any]]]:
        """读状态文件。坏文件按空状态处理——**宁可全量重建，也不要读出一个错状态**。"""
        if self.state_path is None or not self.state_path.exists():
            return {}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("MCP 资源索引状态文件读不出来，按空状态处理：%s", self.state_path)
            return {}
        if not isinstance(payload, dict) or payload.get("version") != STATE_VERSION:
            return {}
        servers = payload.get("servers")
        if not isinstance(servers, dict):
            return {}
        state: dict[str, dict[str, dict[str, Any]]] = {}
        for server, entries in servers.items():
            if not isinstance(entries, dict):
                continue
            state[str(server)] = {
                str(uri): dict(record)
                for uri, record in entries.items()
                if isinstance(record, dict)
            }
        return state

    def _save(self) -> None:
        if self.state_path is None:
            return
        payload = {
            "version": STATE_VERSION,
            "servers": {server: dict(entries) for server, entries in self._state.items()},
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            self.state_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("MCP 资源索引状态写不出去（忽略）：%s", exc)

    def reset(self, server: str | None = None) -> None:
        """丢掉增量状态，让下一次索引全量重来。

        `orbit mcp reload` 用它：配置或服务实现换掉之后，"上次索引过什么"这个判断
        可能已经不成立（同一个 URI 换了解析方式、etag 规则变了），此时全量重建是唯一
        不会留下半新半旧索引的做法。**它不删已入库的块**，只让下一轮重新比对与覆盖。
        """
        if server is None:
            self._state.clear()
        else:
            self._state.pop(server, None)
        self._save()

    def report(self, server: str) -> dict[str, Any] | None:
        found = self._reports.get(server)
        return None if found is None else found.as_dict()

    def indexed_uris(self, server: str) -> list[str]:
        return sorted(self._state.get(server, {}))

    # —— 索引主流程 ——

    async def index_server(self, server: str, client, *, force: bool = False) -> ResourceReport:
        """索引一个服务的全部 resources。

        ``force=True`` 忽略签名、全部重建。单个资源失败只进 ``errors``，不影响同批其它
        资源——一批里有一条坏 URI 就把整批丢掉，是"看起来跑通了但什么都没有"的经典成因。
        """
        report = ResourceReport(server=server)
        entries = self._state.setdefault(server, {})
        seen: set[str] = set()
        cursor: str | None = None
        for _ in range(self.max_pages):
            try:
                result = await client.list_resources(cursor=cursor)
            except Exception as exc:  # noqa: BLE001 - 列表失败整批结束，原因写进报告
                report.errors.append({"uri": "", "error": f"列出 resources 失败：{exc}"})
                break
            for resource in _items(result, "resources"):
                await self._index_one(server, client, resource, report, entries, seen, force)
                if self.gap:
                    await asyncio.sleep(self.gap)
            next_cursor = _shape(result).get("next_cursor")
            if not next_cursor:
                break
            cursor = str(next_cursor)

        # 消失的资源：状态与索引一起失效（否则检索会命中一个服务上已不存在的 URI）。
        gone = [uri for uri in entries if uri not in seen]
        if gone:
            await self._drop_chunks(gone)
            for uri in gone:
                entries.pop(uri, None)
            report.dropped.extend(gone)

        report.templates = await self._templates(client)
        self._reports[server] = report
        self._save()
        return report

    async def _index_one(
        self,
        server: str,
        client,
        resource,
        report: ResourceReport,
        entries: dict[str, dict[str, Any]],
        seen: set[str],
        force: bool,
    ) -> None:
        """处理单个 resource：先看列表信号，再决定要不要读正文。"""
        shape = _shape(resource)
        uri = str(shape.get("uri") or "")
        if not uri:
            return
        seen.add(uri)
        declared = str(shape.get("mime_type") or "")
        if not is_textual(declared):
            report.skipped.append({"uri": uri, "reason": binary_reason(declared)})
            return

        known = entries.get(uri) or {}
        listed = listing_signature(shape)
        if not force and listed and known.get("list_signature") == listed:
            report.unchanged.append(uri)
            return

        try:
            body, mime, reason = await self._read(client, uri)
        except Exception as exc:  # noqa: BLE001 - 坏 URI 只跳过这条
            report.errors.append({"uri": uri, "error": f"{type(exc).__name__}: {str(exc)[:200]}"})
            return
        if reason:
            report.skipped.append({"uri": uri, "reason": reason})
            return
        if not is_textual(mime):
            report.skipped.append({"uri": uri, "reason": binary_reason(mime)})
            return
        if not body.strip():
            report.skipped.append({"uri": uri, "reason": "服务返回的正文为空，没有可索引的内容"})
            return

        signature = content_signature(body)
        if not force and known.get("signature") == signature:
            # 列表信号变了但内容没变（服务端换了 etag 而已）：只更新信号，不重写索引。
            entries[uri] = {**known, "list_signature": listed, "signature": signature}
            report.unchanged.append(uri)
            return

        chunks = chunks_for_resource(uri, body, server)
        if not chunks:
            report.skipped.append({"uri": uri, "reason": "切块后为空（正文过短或全是结构标记）"})
            return
        # 先删旧块：FtsIndexer 按 (origin, hash) 判重，只写不删会把旧版本永久留在索引里。
        if known.get("signature"):
            await self._drop_chunks([uri])
        try:
            written = await self.indexer.write(chunks)
        except Exception as exc:  # noqa: BLE001 - 写入失败只记这条
            report.errors.append({"uri": uri, "error": f"写入索引失败：{exc}"})
            return
        entries[uri] = {
            "signature": signature,
            "list_signature": listed,
            "chunks": len(chunks),
            "written": written,
            "indexed_at": asyncio.get_event_loop().time(),
        }
        report.indexed.append(uri)
        report.chunks += len(chunks)

    async def _read(self, client, uri: str) -> tuple[str, str, str]:
        """读一个资源，返回 ``(正文, MIME, 跳过原因)``。原因非空 = 不该索引。

        三类"看起来读到了但没法索引"的情况要分开报：只有 blob（二进制）、MIME 是二进制、
        正文为空。笼统地记一句"跳过"等于把三种不同的故障混成一团。
        """
        async with asyncio.timeout(self.read_timeout):
            result = await client.read_resource(uri)
        texts: list[str] = []
        mimes: list[str] = []
        has_blob = False
        for item in _shape(result).get("contents") or []:
            if not isinstance(item, dict):
                continue
            mime = str(item.get("mime_type") or "")
            if mime:
                mimes.append(mime)
            text = item.get("text")
            if isinstance(text, str):
                texts.append(text)
            elif item.get("blob"):
                has_blob = True
        mime = mimes[0] if mimes else ""
        if texts:
            return "\n".join(texts).strip(), mime, ""
        if has_blob:
            return "", mime, binary_reason(mime or "blob")
        return "", mime, ""

    async def _templates(self, client) -> list[dict[str, str]]:
        """列 resource templates。

        **不索引**它们：模板是带参数的 URI（``file:///{path}``），没有实参就读不出来。
        但必须报出来——"服务有 12 个模板一条都没索引"和"服务什么都没有"是两件事。
        """
        try:
            result = await client.list_resource_templates()
        except Exception as exc:  # noqa: BLE001 - 不支持 / 报错都按"没有模板"
            log.debug("列出 resource templates 失败：%s", exc)
            return []
        rows: list[dict[str, str]] = []
        for item in _items(result, "resource_templates"):
            shape = _shape(item)
            rows.append(
                {
                    "uri_template": str(shape.get("uri_template") or ""),
                    "name": str(shape.get("name") or ""),
                }
            )
        return rows

    async def _drop_chunks(self, uris: list[str]) -> int:
        """按 origin 删掉这些 URI 的全部块（连同两路 FTS，靠 0004 迁移里的 AD 触发器）。

        没有 ``.store``（假索引器 / 只读路径）时返回 0：删不掉就说删不掉，不假装。
        """
        store = getattr(self.indexer, "store", None)
        if store is None or not uris:
            return 0
        from sqlalchemy import delete

        from app.storage import Chunk

        try:
            async with store.lock, store.sessions() as session:
                result = await session.execute(delete(Chunk).where(Chunk.origin.in_(uris)))
                await session.commit()
        except Exception as exc:  # noqa: BLE001 - 删旧块失败不该让整轮索引失败
            log.warning("删除 MCP 资源旧块失败（忽略）：%s", exc)
            return 0
        return int(result.rowcount or 0)


async def search_resources(retriever, query: str, *, limit: int = 5) -> list[dict]:
    """在已索引的 MCP resources 里检索。

    薄封装，只为把"资源检索的口径"钉在一处：``scope="global"``（资源不属于任何任务，
    见 `chunks_for_resource` 的注释）+ ``source_type="mcp_resource"``。这两处任何一处
    写错，症状都是"资源明明入库了却搜不到"，而调用点散落各处时根本查不出来。

    ``retriever`` 是 ``app.retrieval.FtsRetriever``（生产里由
    ``app.retrieval.build_retriever`` 构造），本模块不自己造检索器。
    """
    return await retriever.search(
        query, scope="global", source_type="mcp_resource", top_k=limit
    )


def build_indexer(settings, store, state_path: Path | str | None = None) -> ResourceIndexer:
    """生产构造：`FtsIndexer` + ``<data_dir>/mcp-resources.json`` 状态文件。"""
    from app.indexing import build_indexer as build_fts

    path = state_path
    if path is None:
        path = Path(settings.data_dir) / "mcp-resources.json"
    return ResourceIndexer(build_fts(settings, store), path)
