"""会话检索的双 FTS5 索引：独立 ``fts.sqlite``，中文与英文各走一条路。

对标 ``handoff/hermes-parity/research/03-persistence-layer.md`` §1.2（MATRIX 10.12，
补 MATRIX 9.6 的缺口）。Hermes 那边是 ``messages_fts``（unicode61）+ 
``messages_fts_trigram``（trigram）两张 FTS5 虚表 + 6 个触发器，全部建在 ``state.db``
里；Orbit 照抄不了，理由与 ``app/session_plane.py`` 模块 docstring 里那三条一致——
**``app/storage.py`` 与 alembic 迁移是禁改区，不能加表，也就不能加触发器**。

## 为什么是"独立文件 + 增量同步"，而不是触发器

``messages`` 表没有 FTS5 索引，中文查询只能走 ``LIKE '%词%'``（见
``app/session_search.py`` 的模块 docstring，它自己写明了这是线性扫描）。库到十万条
量级时一次搜索就是一次全表扫。要修它，只有两条路：

1. 在 ``app.sqlite`` 里加 FTS5 虚表 + 触发器 → **改 schema，禁改区**；
2. 另开一个 ``<data_dir>/fts.sqlite``，由本模块**主动**增量同步。

选 2。代价是没有触发器那种"写了消息就自动进索引"的强一致，需要显式
``index_messages()``（CLI: ``orbit sessions reindex``）。这个代价是可接受的，因为
索引是**派生数据**：丢了能重建（:meth:`SessionIndex.rebuild`），而真相源一条不动。
好处是主库的写路径完全不受影响——FTS 段合并、重建、删库重来都不会碰
``app.sqlite`` 的锁。

## 两张表怎么分工（本机实测数据）

| 表 | 分词器 | 索引的文本 | 实测 |
|---|---|---|---|
| ``messages_fts`` | ``unicode61`` | ``char_text``（逐字空格切分） | CJK 2 字词靠它 |
| ``messages_fts_trigram`` | ``trigram`` | 原始 ``content`` | CJK ≥3 字、ASCII 子串 |

本机实测（``sqlite 3.50.4``，``ENABLE_FTS5=1``）：

- ``unicode61`` 索引原始中文：``MATCH '怎么定'`` / ``'决定'`` / ``'键盘'`` **全部 0 条**
  ——整段中文被当成一个 token，这就是 MATCH 命中不了中文的原因；
- ``trigram`` 索引原始中文：``'怎么定'`` 命中、``'布局偏好'`` 命中，但 **2 字词
  ``'决定'`` / ``'键盘'`` 仍是 0 条**（trigram 只索引 ≥3 字符的窗口）；
- ``unicode61`` 索引 **逐字切分**的中文（``'怎 么 定'``）：两字窗口查询能命中。

所以"中文走 trigram 就够了"是**不成立的**——两字词会静默返回空。两条路都要：
``app/indexing.py``（既有 RAG 写入侧）也是同一套（trigram 走原文、unicode61 走
``char_text``），这里复用它的 :func:`~app.indexing.split_chars` 与
:func:`~app.indexing.match_expression`，不另写一份查询串拼装——两处拼法不一致时，
"同一个词在会话检索里能命中、在知识库检索里不能"会变成一个没人查得动的 bug。

## 选表策略（:meth:`SessionIndex.plan` 会把它讲清楚）

- **纯 ASCII** → ``unicode61`` 为主（整词命中 + ``bm25`` 排序），再补一次 ``trigram``
  抓子串：本机实测 ``MATCH 'atewa'`` 在 unicode61 上是 0 条、在 trigram 上是 1 条。
  只查一路会让"只记得半个单词"的查询静默返回空。
- **含 CJK** → ``trigram`` 为主（≥3 字精确子串），再补一次 ``unicode61``（char 路径，
  兜住 1–2 字词）。两路结果按 ``row_id`` 去重，trigram 命中的排前面。

**为什么不用 RRF 融合打分**（``app/retrieval.py`` 在知识库那边用了）：会话检索的回答
是"这条消息里有没有出现过这个词"，两路命中的是同一条消息的同一段正文，谁排前面
只影响观感、不影响结论；引入一个用户无法解释的融合分数，反而让"为什么这条排第一"
变得不可复现。去重 + 固定次序就够了。

## 必须可降级

FTS5 是否可用取决于 ``sqlite3`` 的编译选项（``ENABLE_FTS5``）以及分词器版本
（``trigram`` 要 3.34+）。不可用时**明确抛** :class:`FtsUnavailable`，消息里写明
"退回 ``orbit session-search discover``（LIKE 子串）"——静默退回是最坏的一种：
用户以为在用索引，其实每次都在全表扫。
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.indexing import match_expression, split_chars

#: 索引库文件名（与 ``app.sqlite`` 同级，但**完全独立**）。
FTS_FILENAME = "fts.sqlite"

#: 镜像表：``row_id`` = ``app.sqlite`` 里 ``messages.rowid``（整数、单调、可当增量游标）。
#: 为什么不用 ``messages.id``：它是 TEXT（uuid），而 FTS5 的 ``rowid`` 必须是整数。
MIRROR_TABLE = "messages_index"

#: unicode61 那一路（索引 ``char_text``，中文按字切分）。
TABLE_UNICODE = "messages_fts"

#: trigram 那一路（索引原始 ``content``）。
TABLE_TRIGRAM = "messages_fts_trigram"

#: 单次 ``index_messages`` 的默认批量（行）。够大以摊薄事务开销，又不会把十万行
#: 一次性读进内存。
DEFAULT_BATCH = 1000

#: 默认返回条数 / 上限。会话检索是"给我几条能回跳的"，不是导出。
DEFAULT_LIMIT = 10
MAX_LIMIT = 100

#: 片段宽度（字符）。
SNIPPET_CHARS = 120

#: 降级说明。放在常量里而不是散在异常消息里：CLI、测试、文档三处引用同一句。
DEGRADE_HINT = (
    "退回 `orbit session-search discover <词…>`（LIKE 子串匹配，中文天然可用，"
    "但用不上索引、是线性扫描）"
)

_CJK = (("\u4e00", "\u9fff"), ("\u3400", "\u4dbf"), ("\uf900", "\ufaff"))


class FtsUnavailable(RuntimeError):
    """FTS5（或 trigram 分词器）不可用。

    消息里一定带 :data:`DEGRADE_HINT`——这一层"静默退回 LIKE"是不可接受的：
    用户会以为自己在用索引。
    """


class SessionIndexError(RuntimeError):
    """可预期的索引错误（查询为空、索引库损坏等）。"""


def is_cjk(char: str) -> bool:
    """该字符是不是 CJK。与 ``app/indexing.is_cjk`` 同一套区间。"""
    return any(low <= char <= high for low, high in _CJK)


def has_cjk(value: str) -> bool:
    return any(is_cjk(char) for char in (value or ""))


@dataclass(frozen=True)
class SearchPlan:
    """一次查询走哪几张表、为什么。``plan()`` 返回它，CLI 会把它打出来。

    把"选表"做成可观察的值，而不是藏在 ``search`` 里的 if 分支：出问题时
    "这次为什么走了 char 路径"能一眼看到，而不是靠读代码猜。
    """

    mode: str
    tables: tuple[str, ...]
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "tables": list(self.tables), "reason": self.reason}


@dataclass(frozen=True)
class Hit:
    """一条命中。``row_id`` 是 FTS 的 rowid，``message_id`` 是回跳用的 uuid。"""

    row_id: int
    message_id: str
    conversation_id: str
    role: str
    created_at: str
    snippet: str
    table: str
    score: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "row_id": self.row_id,
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "role": self.role,
            "created_at": self.created_at,
            "snippet": self.snippet,
            "table": self.table,
            "score": self.score,
        }


@dataclass(frozen=True)
class IndexReport:
    """一次增量同步的结果。"""

    added: int = 0
    scanned: int = 0
    since_id: int = 0
    watermark: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "added": self.added,
            "scanned": self.scanned,
            "since_id": self.since_id,
            "watermark": self.watermark,
        }


# ── 建表 ────────────────────────────────────────────────────────────────────


def _create_schema(conn: sqlite3.Connection) -> None:
    """建镜像表 + 两张 FTS5 虚表（幂等）。

    FTS5 不可用时 ``CREATE VIRTUAL TABLE`` 会抛 ``sqlite3.OperationalError``
    （``no such module: fts5`` / ``no such tokenizer: trigram``），由调用方翻成
    :class:`FtsUnavailable`。单独成函数是为了让测试能替换它来验证降级路径——
    本机的 sqlite 是支持 FTS5 的，造不出"没编译"的真实环境。
    """
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {MIRROR_TABLE} (
            row_id INTEGER PRIMARY KEY,
            message_id TEXT NOT NULL,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            created_at TEXT NOT NULL,
            content TEXT NOT NULL,
            char_text TEXT NOT NULL
        )
        """
    )
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_mirror_conversation ON {MIRROR_TABLE}(conversation_id)")
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE_UNICODE} USING fts5(char_text)")
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE_TRIGRAM} USING fts5(content, tokenize='trigram')"
    )
    conn.commit()


def _snippet(content: str, terms: list[str], width: int = SNIPPET_CHARS) -> str:
    """以最早出现的命中词为中心截一段（与 ``session_search._snippet`` 同一口径）。"""
    text = " ".join((content or "").split())
    if not text:
        return ""
    lowered = text.lower()
    hits = [index for index in (lowered.find(term.lower()) for term in terms) if index >= 0]
    start = max(0, min(hits) - width // 4) if hits else 0
    end = min(len(text), start + width)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def _terms(query: str) -> list[str]:
    """空格分词（与 ``session_search`` 一致：全部词都要命中）。"""
    return [item for item in (query or "").split() if item]


def _escape_like(term: str) -> str:
    """把用户输入里的 LIKE 通配符转义成字面量（顺序要紧：先反斜杠）。

    不转义的话，用户输入 ``100%`` 会变成"匹配一切"，多词 AND 当场失效。
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SessionIndex:
    """``fts.sqlite`` 的读写：增量同步 + 双路检索 + 重建。

    每次调用开一个**新连接**：sqlite3 连接不能跨线程共享，而本模块的写入会跑在
    ``asyncio.to_thread`` 里；按调用开连接（毫秒级）比维护连接池和线程绑定简单得多。
    """

    def __init__(self, data_dir: Path, *, path: Path | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.path = Path(path) if path is not None else self.data_dir / FTS_FILENAME

    # ── 连接 ──
    def connect(self) -> sqlite3.Connection:
        """打开（必要时创建）索引库。FTS5 不可用时抛 :class:`FtsUnavailable`。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path)
        try:
            _create_schema(conn)
        except sqlite3.OperationalError as exc:
            conn.close()
            raise FtsUnavailable(
                f"FTS5 索引不可用（{exc}）。{DEGRADE_HINT}。"
                f"索引库路径：{self.path}（删掉它不会丢任何消息，消息真相源在 app.sqlite）"
            ) from exc
        return conn

    def available(self) -> bool:
        """FTS5 能不能用（CLI 的 `sessions stats` 之类要据此给提示）。"""
        try:
            self.connect().close()
        except FtsUnavailable:
            return False
        return True

    @contextmanager
    def _open(self) -> Iterator[sqlite3.Connection]:
        """用完即关。``sqlite3.Connection`` 的 ``with`` 只管事务**不关连接**，
        拿它当连接管理器会漏句柄（十万条重建时会漏到句柄耗尽）。"""
        conn = self.connect()
        try:
            yield conn
        finally:
            conn.close()

    # ── 选表 ──
    def plan(self, query: str) -> SearchPlan:
        """这次查询走哪几张表。规则见模块 docstring 的"选表策略"。"""
        terms = _terms(query)
        if not terms:
            raise SessionIndexError("查询不能为空")
        if not any(has_cjk(term) for term in terms):
            return SearchPlan(
                mode="ascii",
                tables=(TABLE_UNICODE, TABLE_TRIGRAM),
                reason="纯 ASCII：unicode61 先出整词命中，trigram 再补子串（unicode61 对词内子串 0 命中）",
            )
        short = [term for term in terms if has_cjk(term) and len(term) < 3]
        if short:
            return SearchPlan(
                mode="cjk-short",
                tables=(TABLE_UNICODE, TABLE_TRIGRAM),
                reason=(
                    "含 CJK 且有关键词不足 3 字（"
                    + "、".join(short)
                    + "）：trigram 只索引 ≥3 字符窗口，这类词靠 unicode61 的逐字路径兜"
                ),
            )
        return SearchPlan(
            mode="cjk",
            tables=(TABLE_TRIGRAM, TABLE_UNICODE),
            reason="含 CJK 且关键词都 ≥3 字：trigram 先出精确子串，unicode61 兜住混在里面的短词",
        )

    # ── 检索 ──
    def search(self, query: str, limit: int = DEFAULT_LIMIT) -> list[Hit]:
        """按 :meth:`plan` 的策略检索，返回去重后的命中（含片段）。

        多词是 **AND**（见 :meth:`_query`），两路结果按 ``row_id`` 去重，
        :meth:`plan` 里排在前面的表优先占位。
        """
        text = normalize_query(query)
        plan = self.plan(text)
        wanted = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
        terms = _terms(text)

        hits: list[Hit] = []
        seen: set[int] = set()
        with self._open() as conn:
            for table in plan.tables:
                mode = "trigram" if table == TABLE_TRIGRAM else "chars"
                expression = match_expression(text, mode)
                if not expression:
                    continue
                for hit in self._query(conn, table, expression, wanted, terms):
                    if hit.row_id in seen:
                        continue
                    seen.add(hit.row_id)
                    hits.append(hit)
                    if len(hits) >= wanted:
                        return hits
        return hits

    def _query(
        self,
        conn: sqlite3.Connection,
        table: str,
        expression: str,
        limit: int,
        terms: list[str],
    ) -> list[Hit]:
        """单表查询。``bm25`` 越小越相关，所以 ``ORDER BY score`` 是升序。

        **多词在这里被收成 AND**：``app.indexing.match_expression`` 把多个候选短语用
        ``OR`` 连起来（那是知识库召回的语义——宁可多召回，后面还有重排）。会话检索不同：
        用户打两个词就是想找"两个词都出现"的那条，OR 会把只含一个词的消息也塞进来
        （实测 ``'退避 键盘'`` 会同时命中两条不同的消息）。所以 MATCH 出候选之后，再用
        ``LIKE`` 逐词确认一遍——与 ``app/session_search.py`` 的 AND 语义对齐。
        """
        clauses = [f"{table} MATCH :match"]
        params: dict[str, Any] = {"match": expression, "limit": limit}
        for index, term in enumerate(terms):
            key = f"term{index}"
            clauses.append(f"m.content LIKE :{key} ESCAPE '\\'")
            params[key] = f"%{_escape_like(term)}%"
        sql = f"""
            SELECT m.row_id AS row_id, m.message_id AS message_id,
                   m.conversation_id AS conversation_id, m.role AS role,
                   m.created_at AS created_at, m.content AS content,
                   bm25({table}) AS score
            FROM {table}
            JOIN {MIRROR_TABLE} m ON m.row_id = {table}.rowid
            WHERE {" AND ".join(clauses)}
            ORDER BY score
            LIMIT :limit
        """
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            # 查询串被 FTS5 语法拒绝时如实报错，不假装"没命中"——两者的含义完全不同。
            raise SessionIndexError(f"FTS 查询失败（{exc}）：{expression!r}") from exc
        return [
            Hit(
                row_id=int(row[0]),
                message_id=str(row[1]),
                conversation_id=str(row[2]),
                role=str(row[3]),
                created_at=str(row[4]),
                snippet=_snippet(str(row[5] or ""), terms),
                table=table,
                score=float(row[6] or 0.0),
            )
            for row in rows
        ]

    # ── 同步 ──
    async def index_messages(
        self,
        store: Any,
        *,
        since_id: int = 0,
        limit: int = 0,
    ) -> IndexReport:
        """把 ``app.sqlite`` 的 ``messages`` 增量同步进 ``fts.sqlite``。

        游标是 ``messages.rowid``（整数、单调、不复用）。``since_id`` 与索引库自己的
        水位取**较大值**：传小了不会重复索引（水位兜住），传大了也不会跳过——
        水位才是权威，``since_id`` 只是调用方的补充约束。

        ``limit`` 是单批行数（0 = 一次到底）。
        """
        watermark = self.watermark()
        floor = max(int(since_id or 0), watermark)
        rows = await self._read_messages(store, floor, int(limit or 0))
        if not rows:
            return IndexReport(added=0, scanned=0, since_id=floor, watermark=watermark)
        added = await asyncio.to_thread(self._write_rows, rows)
        return IndexReport(
            added=added,
            scanned=len(rows),
            since_id=floor,
            watermark=self.watermark(),
        )

    async def rebuild(self, store: Any, *, batch: int = DEFAULT_BATCH) -> IndexReport:
        """丢掉索引库重来一遍（**不动 ``app.sqlite``**）。

        什么时候需要它：消息被删过（``sessions delete`` / ``prune``）之后，镜像里
        还留着那条消息的残影。增量同步只加不删，所以清残影只能重建。
        """
        if self.path.exists():
            self.path.unlink()
        total = IndexReport()
        added = scanned = 0
        while True:
            report = await self.index_messages(store, limit=batch or DEFAULT_BATCH)
            added += report.added
            scanned += report.scanned
            total = report
            if not batch or report.scanned < batch:
                break
        return IndexReport(
            added=added,
            scanned=scanned,
            since_id=0,
            watermark=total.watermark,
        )

    async def _read_messages(self, store: Any, since: int, limit: int) -> list[dict[str, Any]]:
        """从 ``app.sqlite`` 读新消息。

        用 ``messages.rowid`` 而不是 ``messages.id`` 排序/过滤：``id`` 是 uuid，既不能
        比大小也不能当 FTS 的 rowid。``rowid`` 由 SQLite 维护，插入即递增且不复用
        （与 Hermes 的 ``AUTOINCREMENT`` 同一个性质，这就是它能当游标的原因）。
        """
        from sqlalchemy import text as sql_text

        sql = (
            "SELECT rowid AS row_id, id AS message_id, conversation_id, role, content, created_at "
            "FROM messages WHERE rowid > :since ORDER BY rowid"
        )
        params: dict[str, Any] = {"since": int(since)}
        if limit and limit > 0:
            sql += " LIMIT :limit"
            params["limit"] = int(limit)
        async with store.sessions() as session:
            result = (await session.execute(sql_text(sql), params)).mappings().all()
        return [dict(row) for row in result]

    def _write_rows(self, rows: list[dict[str, Any]]) -> int:
        """把一批行写进镜像与两张 FTS 表（同步，由 ``to_thread`` 调用）。

        ``INSERT OR IGNORE`` 保证重复同步不产生重复行（镜像表 ``row_id`` 是主键）；
        只有**真的新插入**的行才写 FTS——否则重建一次就会把倒排表撑成两倍。
        """
        added = 0
        with self._open() as conn:
            for row in rows:
                cursor = conn.execute(
                    f"""
                    INSERT OR IGNORE INTO {MIRROR_TABLE}
                        (row_id, message_id, conversation_id, role, created_at, content, char_text)
                    VALUES (:row_id, :message_id, :conversation_id, :role, :created_at, :content, :char_text)
                    """,
                    {
                        "row_id": int(row["row_id"]),
                        "message_id": str(row["message_id"] or ""),
                        "conversation_id": str(row["conversation_id"] or ""),
                        "role": str(row["role"] or ""),
                        "created_at": str(row["created_at"] or ""),
                        "content": str(row["content"] or ""),
                        "char_text": split_chars(str(row["content"] or "")),
                    },
                )
                if cursor.rowcount == 0:
                    continue
                content = str(row["content"] or "")
                conn.execute(
                    f"INSERT INTO {TABLE_UNICODE}(rowid, char_text) VALUES (?, ?)",
                    (int(row["row_id"]), split_chars(content)),
                )
                conn.execute(
                    f"INSERT INTO {TABLE_TRIGRAM}(rowid, content) VALUES (?, ?)",
                    (int(row["row_id"]), content),
                )
                added += 1
            conn.commit()
        return added

    # ── 统计 ──
    def watermark(self) -> int:
        """索引库当前同步到哪个 ``rowid``（空库为 0）。"""
        if not self.path.exists():
            return 0
        try:
            with self._open() as conn:
                row = conn.execute(f"SELECT COALESCE(MAX(row_id), 0) FROM {MIRROR_TABLE}").fetchone()
        except sqlite3.OperationalError:
            return 0
        return int(row[0] or 0) if row else 0

    def stats(self) -> dict[str, Any]:
        """索引库现状：路径、镜像行数、两张表行数、水位、字节数。"""
        if not self.path.exists():
            return {
                "path": str(self.path),
                "available": self.available(),
                "rows": 0,
                "unicode_rows": 0,
                "trigram_rows": 0,
                "watermark": 0,
                "bytes": 0,
            }
        with self._open() as conn:
            rows = int(conn.execute(f"SELECT COUNT(*) FROM {MIRROR_TABLE}").fetchone()[0])
            unicode_rows = int(conn.execute(f"SELECT COUNT(*) FROM {TABLE_UNICODE}").fetchone()[0])
            trigram_rows = int(conn.execute(f"SELECT COUNT(*) FROM {TABLE_TRIGRAM}").fetchone()[0])
            watermark = int(
                conn.execute(f"SELECT COALESCE(MAX(row_id), 0) FROM {MIRROR_TABLE}").fetchone()[0]
            )
        return {
            "path": str(self.path),
            "available": True,
            "rows": rows,
            "unicode_rows": unicode_rows,
            "trigram_rows": trigram_rows,
            "watermark": watermark,
            "bytes": self.path.stat().st_size,
        }


def open_index(data_dir: Path) -> SessionIndex:
    """工厂函数：与 ``app.indexing.build_indexer`` 同一命名习惯。"""
    return SessionIndex(data_dir)


def normalize_query(query: str) -> str:
    """把查询压成单行（片段截取与 MATCH 拼装都假设输入没有换行）。"""
    return re.sub(r"\s+", " ", (query or "").strip())


__all__ = [
    "DEFAULT_BATCH",
    "DEFAULT_LIMIT",
    "DEGRADE_HINT",
    "FTS_FILENAME",
    "MAX_LIMIT",
    "MIRROR_TABLE",
    "TABLE_TRIGRAM",
    "TABLE_UNICODE",
    "FtsUnavailable",
    "Hit",
    "IndexReport",
    "SearchPlan",
    "SessionIndex",
    "SessionIndexError",
    "has_cjk",
    "is_cjk",
    "normalize_query",
    "open_index",
]
