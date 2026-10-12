"""RAG 写入侧：切块、双路 FTS5 索引。

trigram 对 2 字中文查询返回 0 条（实测 '键盘' 命中不了），所以每条 chunk 额外存一份
逐字空格切分的 char_text，用 unicode61 建第二张 FTS 表，两路候选在读取侧 RRF 融合。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy import text as sql_text

from app.policies import estimate_tokens
from app.storage import Chunk, Store, now

HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
TABLE_ROW = re.compile(r"^\|.*\|$")
LIST_ITEM = re.compile(r"^([-*+]|\d+[.)])\s+")
SENTENCE_END = "。！？!?；;.\n"

TARGET_CHARS = 400
MIN_CHARS = 300
MAX_CHARS = 500
TABLE_MAX_CHARS = 500
OVERLAP_CHARS = 80
SIMILARITY_MERGE = 0.9
CJK = (("\u4e00", "\u9fff"), ("\u3400", "\u4dbf"), ("\uf900", "\ufaff"))


def is_cjk(char: str) -> bool:
    return any(low <= char <= high for low, high in CJK)


def split_chars(value: str) -> str:
    """CJK 逐字插空格、拉丁与数字保持整词：'机械键盘 B站' -> '机 械 键 盘 B 站'。"""
    out: list[str] = []
    for char in value:
        if is_cjk(char):
            out.append(f" {char} ")
        else:
            out.append(char)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def shingle_similarity(left: str, right: str, size: int = 2) -> float:
    """无向量阶段的最低成本相似度：字符 bigram 的 Jaccard。"""
    if not left or not right:
        return 0.0

    def grams(value: str) -> set[str]:
        if len(value) <= size:
            return {value}
        return {value[index : index + size] for index in range(len(value) - size + 1)}

    first, second = grams(left), grams(right)
    union = first | second
    return len(first & second) / len(union) if union else 0.0


#: 单次 MATCH 表达式里候选短语的上限，避免长目标把查询串撑爆
QUERY_PART_LIMIT = 40


def _clean_part(value: str) -> str:
    return value.replace('"', "").strip()


def trigram_query_parts(query: str) -> list[str]:
    """trigram 只索引 ≥3 字符，所以把查询拆成 3 字窗口做 OR。

    整串 ≤12 字时额外保留整串，短查询仍走精确短语；长目标（例如整个任务目标）
    拆成窗口后才有机会命中——整句当一个短语必然 0 召回。
    """
    compact = re.sub(r"\s+", "", query or "")
    if not compact:
        return []
    parts: list[str] = [compact] if len(compact) <= 12 else []
    if len(compact) >= 3:
        parts.extend(compact[index : index + 3] for index in range(len(compact) - 2))
    else:
        parts.append(compact)
    return parts


def char_query_parts(query: str) -> list[str]:
    """字符级副索引按单字建，两字中文词也要能命中：按相邻两词窗口做 OR。"""
    tokens = split_chars(query or "").split()
    if not tokens:
        return []
    parts: list[str] = []
    if len(tokens) >= 2:
        parts.extend(" ".join(tokens[index : index + 2]) for index in range(len(tokens) - 1))
    parts.append(" ".join(tokens))
    return parts


def match_expression(query: str, mode: str) -> str:
    """把自然语言查询编成 FTS5 的 MATCH 表达式（各候选短语按 OR 连接）。"""
    raw = trigram_query_parts(query) if mode == "trigram" else char_query_parts(query)
    ordered: list[str] = []
    for part in raw:
        cleaned = _clean_part(part)
        if cleaned and cleaned not in ordered:
            ordered.append(cleaned)
    return " OR ".join(f'"{part}"' for part in ordered[:QUERY_PART_LIMIT])


@dataclass
class Block:
    text: str
    title_path: str = ""
    kind: str = "text"
    rows: list[str] = field(default_factory=list)


def split_blocks(source: str) -> list[Block]:
    """①结构切：markdown 标题、表格、空行分段；纯文本退化为按行/空行分段。

    标题除了当后面段落的 title_path，也可能**本身就是页面的全部内容**：SPA 结果列表页
    每个条目就是一个 `### 标题`，标题之间没有正文。这类页面若把标题只当路径用掉，
    整页一块都落不下来（实测 B 站搜索页 2238 字只落成 34 字），检索必然 0 召回。
    所以没等到正文的标题会自己成为一块。
    """
    lines = source.splitlines()
    blocks: list[Block] = []
    titles: list[str] = []
    buffer: list[str] = []
    table: list[str] = []
    pending: tuple[str, str] | None = None  # 还没被正文消费的标题：(标题文本, 父级路径)

    def flush_text() -> None:
        """空行/表格/标题处收束正文段；正文一旦落下来，标题就被消费掉了。"""
        nonlocal pending
        if buffer:
            body = "\n".join(buffer).strip()
            if body:
                blocks.append(Block(text=body, title_path=" > ".join(titles), kind="text"))
            buffer.clear()
            pending = None

    def emit_pending_heading() -> None:
        """标题迟迟等不到正文（下一个标题/EOF）：标题自己就是内容，别丢。"""
        nonlocal pending
        if pending is not None and not buffer:
            blocks.append(Block(text=pending[0], title_path=pending[1], kind="text"))
        pending = None

    def flush_table() -> None:
        if table:
            blocks.append(
                Block(
                    text="\n".join(table).strip(),
                    title_path=" > ".join(titles),
                    kind="table",
                    rows=list(table),
                )
            )
            table.clear()

    for raw in lines:
        line = raw.strip()
        heading = HEADING.match(line)
        if heading:
            flush_text()
            emit_pending_heading()
            flush_table()
            level = len(heading.group(1))
            titles = titles[: level - 1]
            titles.append(heading.group(2).strip())
            pending = (heading.group(2).strip(), " > ".join(titles[:-1]))
            continue
        if TABLE_ROW.match(line):
            flush_text()
            pending = None  # 标题已被这张表消费
            table.append(line)
            continue
        flush_table()
        if not line:
            flush_text()
            continue
        buffer.append(line)
    flush_text()
    emit_pending_heading()
    flush_table()
    return blocks


def split_sentences(value: str) -> list[str]:
    parts: list[str] = []
    current = ""
    for char in value:
        current += char
        if char in SENTENCE_END:
            parts.append(current)
            current = ""
    if current:
        parts.append(current)
    return [part for part in parts if part.strip()]


def hard_split(value: str, maximum: int) -> list[str]:
    """超长单段在句子边界再切；找不到边界就硬切。"""
    pieces: list[str] = []
    current = ""
    for sentence in split_sentences(value):
        if len(current) + len(sentence) > maximum and current:
            pieces.append(current)
            current = ""
        while len(sentence) > maximum:
            pieces.append(sentence[:maximum])
            sentence = sentence[maximum:]
        current += sentence
    if current.strip():
        pieces.append(current)
    return pieces


def split_table(block: Block) -> list[str]:
    """表格行级切块：每个数据行 + 表头独立成块。

    之前「整表 ≤500 一块 / 大表按 400 字行组切」会把目标单元格的信号稀释到
    3–8%（A 集门 2 的缺池根因——目标短语只占块 3–8%）。逐行成块后召回粒度
    精确到单元格。单行超长时按 500 字硬切，表头只带在第一段。
    """
    rows = block.rows
    if len(rows) <= 2:
        # 只有表头、没有数据行：整表一块，超长硬切兜底。
        return hard_split(block.text, TABLE_MAX_CHARS)
    header = rows[:2]
    header_text = "\n".join(header)
    pieces: list[str] = []
    for row in rows[2:]:
        if len(header_text) + len(row) + 1 > TABLE_MAX_CHARS:
            sub = hard_split(row, TABLE_MAX_CHARS)
            pieces.append(f"{header_text}\n{sub[0]}")
            pieces.extend(sub[1:])
        else:
            pieces.append(f"{header_text}\n{row}")
    return pieces


def merge_candidates(blocks: list[Block]) -> list[Block]:
    """②合并到 300–500 中文字，③带 50–100 字 overlap。"""
    merged: list[Block] = []
    buffer = ""
    title = ""
    for block in blocks:
        pieces = split_table(block) if block.kind == "table" else [block.text]
        for piece in pieces:
            if block.kind == "table":
                if buffer:
                    merged.append(Block(text=buffer, title_path=title, kind="text"))
                    buffer = ""
                merged.append(Block(text=piece, title_path=block.title_path, kind="table"))
                continue
            for part in hard_split(piece, MAX_CHARS):
                if buffer and len(buffer) + len(part) + 1 > MAX_CHARS:
                    merged.append(Block(text=buffer, title_path=title, kind="text"))
                    tail = buffer[-OVERLAP_CHARS:]
                    boundary = max((tail.find(sign) for sign in SENTENCE_END), default=-1)
                    buffer = tail[boundary + 1 :] if boundary >= 0 else ""
                    title = block.title_path
                if not buffer:
                    title = block.title_path
                buffer = f"{buffer}\n{part}".strip() if buffer else part
                if len(buffer) >= MIN_CHARS and len(buffer) >= TARGET_CHARS:
                    merged.append(Block(text=buffer, title_path=title, kind="text"))
                    buffer = ""
    if buffer:
        merged.append(Block(text=buffer, title_path=title, kind="text"))
    return merged


def collapse_similar(blocks: list[Block]) -> list[Block]:
    """相邻块相似度 >0.9 合并（同一标题路径下）。"""
    collapsed: list[Block] = []
    for block in blocks:
        if collapsed:
            previous = collapsed[-1]
            if (
                previous.kind == block.kind == "text"
                and previous.title_path == block.title_path
                and len(previous.text) + len(block.text) <= MAX_CHARS * 2
                and shingle_similarity(previous.text, block.text) > SIMILARITY_MERGE
            ):
                collapsed[-1] = Block(
                    text=f"{previous.text}\n{block.text}",
                    title_path=previous.title_path,
                    kind="text",
                )
                continue
        collapsed.append(block)
    return collapsed


def chunk_text(source: str) -> list[dict[str, Any]]:
    """切块主入口：返回 [{text, char_text, title_path, ordinal, tokens, hash}]。"""
    source = (source or "").replace("\r\n", "\n").strip()
    if not source:
        return []
    blocks = collapse_similar(merge_candidates(split_blocks(source)))
    seen: set[str] = set()
    chunks: list[dict[str, Any]] = []
    for ordinal, block in enumerate(blocks):
        body = block.text.strip()
        if not body:
            continue
        text = f"{block.title_path}\n{body}" if block.title_path else body
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if digest in seen:
            continue
        seen.add(digest)
        chunks.append(
            {
                "text": text,
                "char_text": split_chars(text),
                "title_path": block.title_path,
                "ordinal": ordinal,
                "tokens": estimate_tokens(text),
                "hash": digest,
            }
        )
    return chunks


def readable_text(data: Any) -> str:
    """只取人类可读部分：不把 elements/JSON 结构整段塞进索引。"""
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("structured", "text", "content", "summary", "body"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""
    if isinstance(data, list):
        parts: list[str] = []
        for item in data[:30]:
            if isinstance(item, dict):
                for key in ("excerpt", "text", "content", "value", "goal", "title"):
                    value = item.get(key)
                    if isinstance(value, str) and value.strip():
                        parts.append(value.strip())
                        break
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return ""


def origin_of(task, name: str, data: Any) -> tuple[str, str, str]:
    """返回（source_type, origin, title）。

    无真实出处的工具结果不入索引：编不出可引用的 origin，写入后只会产生
    模型检索得到、却无法署名的孤儿 chunk，反而让 finalize 的引用核对失败。
    因此只认可 url（web_page）与 path（local_file）两种出处。
    """
    if isinstance(data, dict):
        url = str(data.get("url") or "")
        path = str(data.get("path") or "")
        if url:
            return "web_page", url, str(data.get("title") or "")
        if path:
            return "local_file", path, str(data.get("title") or "")
    return "", "", ""


def chunks_for_tool_result(task, name: str, result: dict) -> list[dict]:
    """把一个工具结果转成待写入的 chunk 列表（CPU 密集，调用方放线程里跑）。"""
    data = (result or {}).get("data")
    source_type, origin, title = origin_of(task, name, data)
    body = readable_text(data)
    if not origin or not body.strip():
        return []
    chunks = []
    for item in chunk_text(body):
        identifier = hashlib.sha256(f"{origin}|{item['hash']}".encode()).hexdigest()[:32]
        chunks.append(
            {
                "id": identifier,
                "source_type": source_type,
                "source_id": str(getattr(task, "id", "")),
                "conversation_id": str(getattr(task, "conversation_id", "")),
                "task_id": str(getattr(task, "id", "")),
                "host": host_of_origin(origin),
                "ordinal": item["ordinal"],
                "text": item["text"],
                "char_text": item["char_text"],
                "tokens": item["tokens"],
                "hash": item["hash"],
                "origin": origin,
                "title_path": title or item["title_path"],
                "created_at": now(),
            }
        )
    return chunks


#: MCP resource 正文入库上限（与工具结果同一上限，超大资源截断、不整段入库）。
RESOURCE_CHAR_LIMIT = 16000


def chunks_for_resource(uri: str, body: str, server: str) -> list[dict]:
    """把 MCP resource 的内容转成待写入的 chunk 列表（source_type=mcp_resource）。

    MCP resources 在连接时由 MCPManager 拉取，**没有任务上下文**——source_id /
    conversation_id / task_id 全空，只能靠 ``scope="global"`` 召回（语义正确：资源是
    跨任务的全局知识，不属于任何一次任务）。origin 用 URI（可追溯），host 用服务名
    （可按 MCP 服务过滤）。
    """
    body = (body or "").strip()
    if not uri or not body:
        return []
    source_type = "mcp_resource"
    host = f"mcp:{server}"
    chunks = []
    for item in chunk_text(body[:RESOURCE_CHAR_LIMIT]):
        identifier = hashlib.sha256(f"{uri}|{item['hash']}".encode()).hexdigest()[:32]
        chunks.append(
            {
                "id": identifier,
                "source_type": source_type,
                "source_id": "",
                "conversation_id": "",
                "task_id": "",
                "host": host,
                "ordinal": item["ordinal"],
                "text": item["text"],
                "char_text": item["char_text"],
                "tokens": item["tokens"],
                "hash": item["hash"],
                "origin": uri,
                "title_path": uri,
                "created_at": now(),
            }
        )
    return chunks


def host_of_origin(origin: str) -> str:
    value = origin.strip().lower()
    if "://" in value:
        value = value.split("://", 1)[1]
    return value.split("/", 1)[0].split("?", 1)[0]


class FtsIndexer:
    """SQLite FTS5 实现：trigram 走 text，unicode61 走 char_text。"""

    def __init__(self, store: Store):
        self.store = store

    async def write(self, chunks: list[dict]) -> int:
        """写库前按 (origin, hash) 与业务 id 判重：同一页面重复观察不产生重复块。"""
        if not chunks:
            return 0
        origins = {chunk["origin"] for chunk in chunks}
        identifiers = {chunk["id"] for chunk in chunks}
        async with self.store.lock, self.store.sessions() as session:
            known = {
                (row[0], row[1])
                for row in (
                    await session.execute(
                        select(Chunk.origin, Chunk.hash).where(Chunk.origin.in_(origins))
                    )
                ).all()
            }
            existing_ids = {
                row[0]
                for row in (
                    await session.execute(select(Chunk.id).where(Chunk.id.in_(identifiers)))
                ).all()
            }
            written = 0
            for chunk in chunks:
                key = (chunk["origin"], chunk["hash"])
                if chunk["id"] in existing_ids or key in known:
                    continue
                session.add(Chunk(**chunk))
                existing_ids.add(chunk["id"])
                known.add(key)
                written += 1
            await session.commit()
        return written

    async def search(
        self,
        query: str,
        *,
        conversation_id: str = "",
        host: str = "",
        source_type: str = "",
        limit: int = 20,
        mode: str = "trigram",
    ) -> list[dict]:
        query = (query or "").strip()
        if len(query) < 2:
            return []
        if mode == "chars":
            table = "chunks_fts_chars"
        else:
            table = "chunks_fts_trigram"
        match = match_expression(query, mode)
        if not match:
            return []
        filters = []
        params: dict[str, Any] = {"match": match, "limit": limit}
        if conversation_id:
            filters.append("c.conversation_id = :conversation_id")
            params["conversation_id"] = conversation_id
        if host:
            filters.append("c.host = :host")
            params["host"] = host
        if source_type:
            filters.append("c.source_type = :source_type")
            params["source_type"] = source_type
        where = " AND ".join([f"{table} MATCH :match", *filters])
        statement = sql_text(
            f"""SELECT c.row_id AS row_id, c.text AS text, c.origin AS origin,
                       c.source_type AS source_type, c.host AS host, c.task_id AS task_id,
                       bm25({table}) AS score
                FROM {table} JOIN chunks c ON c.row_id = {table}.rowid
                WHERE {where}
                ORDER BY score
                LIMIT :limit"""
        )
        try:
            async with self.store.sessions() as session:
                rows = (await session.execute(statement, params)).mappings().all()
        except Exception:
            # 查询串被 FTS5 语法拒绝时退化为空结果（读取侧如实返回空，不假装成功）。
            return []
        return [dict(row) for row in rows]


def build_indexer(settings, store: Store) -> FtsIndexer:
    return FtsIndexer(store)
