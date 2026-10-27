"""会话召回（``session_search``）：四形态、**零模型调用**的历史检索。

对标 Hermes ``tools/session_search_tool.py``（1,271 行）。那边模块 docstring 的原话是
"No LLM calls anywhere — every shape returns actual messages from the DB"：所谓"会话
召回"全部由确定性字符串处理构成（窗口切分、截断、compaction 摘要剔除、片段提取），
成本只等于返回内容本身。本模块照这条办——**不许 import 任何模型 / 连接器模块**，
任何需要"理解"的加工都不在这里做（`tests/test_session_search.py` 有一条用例把这条
约束钉住：源码里出现 langchain / openai / providers / connectors 就算失败）。

四形态（Hermes 靠入参推断，这里显式收 ``mode``，因为 CLI 侧需要一个可选值枚举）：

======================  ==============================  ==================================
形态                    入口                            行为
======================  ==============================  ==================================
``discover``            ``discover(query)``             关键词子串匹配 → 锚点列表
``scroll``              ``anchored_view(message_id)``   锚点 ±window，另加首尾 bookend
``read``                ``read(message_id)``            单条消息头 20 行 + 尾 10 行
``browse``              ``browse()``                    最近会话（``updated_at`` 倒序）
======================  ==============================  ==================================

**中文检索为什么不用 FTS5**：本机实测（``handoff/hermes-parity/research/07-memory-layer.md``
§1.2 与 §3.3）SQLite 的 ``unicode61`` 分词把整段中文当成一个 token，``MATCH '记忆'``
命中 **0**；trigram 那一路能命中，但 Orbit 的 ``messages`` 表没有 FTS5 索引，而迁移
文件是禁改区（本轮不许加表）。所以这里走**子串匹配** ``lower(content) LIKE '%词%'``：
中文天然可用，英文靠两侧 ``lower()`` 折大小写，``%`` / ``_`` / ``\\`` 做转义——不转义
的话用户输入 ``100%`` 会变成通配符，把整库捞回来。

**扫描上限是硬约束**：``%词%`` 用不上任何索引，SQLite 只能全表扫；会话库会到十万条
量级，一条无上限的 LIKE 扫描会把 CLI 卡死。所以 ``discover`` 的 ``scan`` 参数直接给
SQL 加 ``LIMIT``（默认 300，与 Hermes 的 ``_DISCOVER_SCAN_LIMIT`` 同值）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, func, or_, select

from app.storage import Conversation, Message, Store

#: 与 Hermes 完全对齐的默认值与夹取区间（``_session_search_impl`` L1046 / ``_scroll`` L570）。
DEFAULT_LIMIT = 3
MAX_LIMIT = 10
DEFAULT_WINDOW = 5
MAX_WINDOW = 20

#: 锚定窗口 / 书挡的字符预算（Hermes ``_shape_message`` 的 4000 / 1200）。
WINDOW_MAX_CHARS = 4_000
BOOKEND_MAX_CHARS = 1_200

#: 单次 discover 最多扫多少行（Hermes ``_DISCOVER_SCAN_LIMIT = 300``）。
#: 上限再给一个 MAX_SCAN：CLI 允许调用方调大，但不能调到"等于全库"。
DEFAULT_SCAN = 300
MAX_SCAN = 1_000

#: bookend 默认补几条（Hermes ``get_anchored_view(window=5, bookend=3)``）。
DEFAULT_BOOKEND = 3

#: read 形态的头尾行数（Hermes ``_read_session(head=20, tail=10)``）。
READ_HEAD = 20
READ_TAIL = 10

#: 生成的"上下文压缩交接摘要"前缀。它们是机器写的元数据，不是用户内容；Hermes 在
#: bookend 里剔除它们，理由是不让压缩载荷借 session_search 回流进新会话（#43175）。
_COMPACTION_PREFIXES = ("[CONTEXT COMPACTION", "[CONTEXT SUMMARY]:")

#: 片段目标宽度（字符）。够看清上下文，又不至于把窗口预算挤满。
SNIPPET_CHARS = 120


class SessionSearchError(RuntimeError):
    """可预期的召回失败（消息/会话不存在、模式非法）。

    与"真 bug"区分开：CLI 侧把它翻成 ``CliError``（一行消息 + 返回码），不打栈。
    """


@dataclass(frozen=True)
class Anchor:
    """discover 的命中锚点。

    只放"够回跳"的字段：``message_id`` 交给 ``anchored_view`` 展开窗口，
    ``conversation_id`` 交给 ``browse`` / ``sessions export`` 定位整场会话。
    """

    message_id: str
    conversation_id: str
    role: str
    created_at: str
    snippet: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "role": self.role,
            "created_at": self.created_at,
            "snippet": self.snippet,
        }


# ── 纯函数 ──────────────────────────────────────────────────────────────────


def _clamp(value: Any, low: int, high: int) -> int:
    """夹取到 ``[low, high]``。坏值退回 ``low``——召回不该因为一个脏参数就崩。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = low
    return max(low, min(number, high))


def _escape_like(term: str) -> str:
    """把用户输入里的 LIKE 通配符转义成字面量。

    顺序要紧：先转义反斜杠，否则后面加进去的 ``\\%`` 会被自己再转一次。
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_pattern(term: str) -> str:
    """子串匹配的 LIKE 模式（两侧 ``%``）。"""
    return f"%{_escape_like(term)}%"


def _terms(query: str) -> list[str]:
    """空格分词。**全部词都要命中（AND）**，与 Hermes 的 FTS5 隐式 AND 同语义。

    只有一个词时就是单子串匹配；不做前缀/分词/词干处理——中文分词不可用（见模块
    docstring），而"再聪明的分词"一旦引入，中文子串能力就会静默丢掉。
    """
    return [item for item in (query or "").split() if item]


def _snippet(content: str, terms: list[str], width: int = SNIPPET_CHARS) -> str:
    """截一段以命中词为中心的片段。

    没有命中位置（browse 的预览）时退化成"开头 ``width`` 个字符"；有命中时取**最早
    出现的那处**，因为用户最关心的通常是最先出现的上下文。
    """
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


def _is_compaction_summary(content: str) -> bool:
    """这条内容是不是机器生成的压缩交接摘要。"""
    if not content:
        return False
    stripped = content.lstrip()
    return any(stripped.startswith(prefix) for prefix in _COMPACTION_PREFIXES)


def _shape(message: Message, *, anchor: bool = False, max_chars: int | None = None) -> dict[str, Any]:
    """把消息行裁成响应体。

    **每条都带 ``truncated``**：调用方必须能一眼看出"这段内容被截过"，否则会误以为
    原文就这么短。与 Hermes ``_shape_message`` 的差别只在字段命名（那边是
    ``content_truncated`` + ``original_content_chars``，这里是 ``truncated`` +
    ``chars``），语义一一对应。
    """
    raw = message.content or ""
    limit = max_chars or 0
    truncated = limit > 0 and len(raw) > limit
    entry: dict[str, Any] = {
        "message_id": message.id,
        "role": message.role,
        "created_at": message.created_at,
        "content": (raw[:limit] + "…") if truncated else raw,
        "truncated": truncated,
        "chars": len(raw),
    }
    if anchor:
        entry["anchor"] = True
    return entry


def _before_condition(anchor: Message) -> Any:
    """同一会话里"排在锚点之前"的条件。

    ``created_at`` 相同（同一秒内落库的多条消息）时用 ``id`` 兜底排序，否则窗口
    边界会在并列时间戳上抖动，同一个锚点两次召回给出不同的邻居。
    """
    return or_(
        Message.created_at < anchor.created_at,
        and_(Message.created_at == anchor.created_at, Message.id < anchor.id),
    )


def _after_condition(anchor: Message) -> Any:
    """同一会话里"排在锚点之后"的条件（与 :func:`_before_condition` 严格互补）。"""
    return or_(
        Message.created_at > anchor.created_at,
        and_(Message.created_at == anchor.created_at, Message.id > anchor.id),
    )


# ── 召回本体 ────────────────────────────────────────────────────────────────


class SessionSearch:
    """会话召回。全部方法 async、**只读**，且不 import 任何模型/连接器。"""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def _rows(self, statement: Any) -> list[Message]:
        async with self.store.sessions() as session:
            return list((await session.scalars(statement)).all())

    # ── discover ────────────────────────────────────────────────────────────
    async def discover(
        self,
        query: str,
        *,
        conversation_id: str | None = None,
        limit: int = DEFAULT_LIMIT,
        scan: int = DEFAULT_SCAN,
    ) -> list[Anchor]:
        """DISCOVERY：按关键词找候选消息，返回锚点（不含窗口）。

        ``scan`` 是**硬上限**：``%词%`` 用不上索引，SQLite 只能全表扫；会话库会到
        十万条量级，把结果集卡在 300 行以内，最坏情况也有界（这是"CLI 不许被一次
        搜索卡死"的唯一保障，不是性能优化）。
        """
        terms = _terms(query)
        if not terms:
            return []
        limit = _clamp(limit, 1, MAX_LIMIT)
        scan = _clamp(scan, 1, MAX_SCAN)

        statement = (
            select(Message).order_by(Message.created_at.desc(), Message.id.desc()).limit(scan)
        )
        if conversation_id:
            statement = statement.where(Message.conversation_id == conversation_id)
        for term in terms:
            # 两侧折小写：SQLite 的 LIKE 只对 ASCII 折大小写，显式折一次才与 SQL 方言
            # 无关；中文没有大小写，走同一条子串路径（这就是中文能命中的原因）。
            statement = statement.where(
                func.lower(Message.content).like(_like_pattern(term.lower()), escape="\\")
            )

        rows = await self._rows(statement)
        return [
            Anchor(
                message_id=row.id,
                conversation_id=row.conversation_id,
                role=row.role,
                created_at=row.created_at,
                snippet=_snippet(row.content or "", terms),
            )
            for row in rows[:limit]
        ]

    # ── scroll ──────────────────────────────────────────────────────────────
    async def anchored_view(
        self,
        message_id: str,
        *,
        window: int = DEFAULT_WINDOW,
        bookend: int = DEFAULT_BOOKEND,
        max_chars: int = WINDOW_MAX_CHARS,
        bookend_chars: int = BOOKEND_MAX_CHARS,
    ) -> dict[str, Any]:
        """SCROLL：锚点前后各 ``window`` 条，再往外各补 ``bookend`` 条做上下文。

        窗口 = ``锚点 ±window``（含锚点本身，锚点带 ``anchor: True``）；``before`` /
        ``after`` 是**窗口之外**的书挡。边界处不越界：锚点是首条时 ``before`` 为空，
        是末条时 ``after`` 为空（只有两条 SQL 的条件在起作用，不需要特判）。
        """
        window = _clamp(window, 1, MAX_WINDOW)
        bookend = _clamp(bookend, 0, MAX_WINDOW)

        anchor = await self.store.get(Message, message_id)
        if anchor is None:
            raise SessionSearchError(f"消息不存在：{message_id}")

        # 只取窗口 + 书挡需要的行数，不把整场会话读进内存。
        before_rows = await self._rows(
            select(Message)
            .where(Message.conversation_id == anchor.conversation_id, _before_condition(anchor))
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(window + bookend)
        )
        after_rows = await self._rows(
            select(Message)
            .where(Message.conversation_id == anchor.conversation_id, _after_condition(anchor))
            .order_by(Message.created_at.asc(), Message.id.asc())
            .limit(window + bookend)
        )
        # before 是倒序取回的（最近的在前），翻回正序让调用方按时间读。
        before_rows.reverse()

        window_before = before_rows[len(before_rows) - window :]
        bookend_before = before_rows[: max(0, len(before_rows) - window)]
        window_after = after_rows[:window]
        bookend_after = after_rows[window:]

        return {
            "anchor": _shape(anchor, anchor=True, max_chars=max_chars),
            "window": [
                _shape(row, anchor=row.id == anchor.id, max_chars=max_chars)
                for row in [*window_before, anchor, *window_after]
            ],
            # 书挡里剔除压缩摘要：它们是机器写的交接载荷，回流进新会话会挤爆上下文。
            "before": [
                _shape(row, max_chars=bookend_chars)
                for row in bookend_before
                if not _is_compaction_summary(row.content or "")
            ],
            "after": [
                _shape(row, max_chars=bookend_chars)
                for row in bookend_after
                if not _is_compaction_summary(row.content or "")
            ],
        }

    # ── read ────────────────────────────────────────────────────────────────
    async def read(
        self,
        message_id: str,
        *,
        head: int = READ_HEAD,
        tail: int = READ_TAIL,
    ) -> dict[str, Any]:
        """READ：单条消息的头 ``head`` 行 + 尾 ``tail`` 行，中间标省略行数。

        Hermes 的 read 读整场会话（首 20 + 末 10 **条消息**）；Orbit 是整轮落库的，
        单条消息就可能很长（工具输出整段塞在 ``content`` 里），所以这里按**行**切，
        动机与那边一致：把一条五千行的消息原样塞回上下文，等于一次烧光预算。
        """
        head = _clamp(head, 1, 500)
        tail = _clamp(tail, 0, 500)

        message = await self.store.get(Message, message_id)
        if message is None:
            raise SessionSearchError(f"消息不存在：{message_id}")

        raw = message.content or ""
        lines = raw.splitlines()
        total = len(lines)
        truncated = total > head + tail
        if truncated:
            head_lines = lines[:head]
            tail_lines = lines[-tail:] if tail else []
            omitted = total - head - len(tail_lines)
            rendered = "\n".join([*head_lines, f"…（省略 {omitted} 行）…", *tail_lines])
        else:
            head_lines, tail_lines, omitted = lines, [], 0
            rendered = raw

        return {
            "message_id": message.id,
            "conversation_id": message.conversation_id,
            "role": message.role,
            "created_at": message.created_at,
            "total_lines": total,
            "truncated": truncated,
            "omitted_lines": omitted,
            "head": head_lines,
            "tail": tail_lines,
            "content": rendered,
        }

    # ── browse ──────────────────────────────────────────────────────────────
    async def browse(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """BROWSE：按 ``updated_at`` 倒序列出最近会话（不做关键词过滤）。"""
        limit = _clamp(limit, 1, 200)
        counts = (
            select(Message.conversation_id, func.count(Message.id).label("message_count"))
            .group_by(Message.conversation_id)
            .subquery()
        )
        async with self.store.sessions() as session:
            rows = (
                await session.execute(
                    select(Conversation, func.coalesce(counts.c.message_count, 0))
                    .outerjoin(counts, counts.c.conversation_id == Conversation.id)
                    .order_by(Conversation.updated_at.desc(), Conversation.created_at.desc())
                    .limit(limit)
                )
            ).all()

        previews = await self._first_user_snippets([conversation.id for conversation, _ in rows])
        return [
            {
                "id": conversation.id,
                "title": conversation.title,
                "message_count": int(count),
                "updated_at": conversation.updated_at,
                "first_user_snippet": previews.get(conversation.id, ""),
            }
            for conversation, count in rows
        ]

    async def _first_user_snippets(self, conversation_ids: list[str]) -> dict[str, str]:
        """每条会话第一条 user 消息的片段。

        逐会话一次单行查询，而不是"一次把所有 user 消息捞回来再分组"：后者在长会话
        上没有上限（一场 5000 轮对话就够呛），前者每次只碰一行，且 ``conversation_id``
        上有索引。``limit`` 条会话 = ``limit`` 次毫秒级查询，比无界扫描安全。
        """
        found: dict[str, str] = {}
        for conversation_id in conversation_ids:
            rows = await self._rows(
                select(Message)
                .where(Message.conversation_id == conversation_id, Message.role == "user")
                .order_by(Message.created_at.asc(), Message.id.asc())
                .limit(1)
            )
            if rows:
                found[conversation_id] = _snippet(rows[0].content or "", [])
        return found

    # ── 统一入口 ────────────────────────────────────────────────────────────
    async def search(
        self,
        mode: str,
        *,
        query: str = "",
        message_id: str | None = None,
        conversation_id: str | None = None,
        limit: int = DEFAULT_LIMIT,
        window: int = DEFAULT_WINDOW,
        scan: int = DEFAULT_SCAN,
    ) -> dict[str, Any]:
        """四形态统一入口。非法 ``mode`` 给出可行动的报错（列出可选值）。"""
        normalized = (mode or "").strip().lower()
        if normalized == "discover":
            if not _terms(query):
                raise SessionSearchError("discover 需要关键词：query 不能为空")
            anchors = await self.discover(
                query, conversation_id=conversation_id, limit=limit, scan=scan
            )
            return {
                "mode": "discover",
                "query": query,
                "count": len(anchors),
                "results": [anchor.as_dict() for anchor in anchors],
            }
        if normalized == "scroll":
            if not message_id:
                raise SessionSearchError("scroll 需要 message_id（用 discover 返回的锚点 id）")
            return {"mode": "scroll", **(await self.anchored_view(message_id, window=window))}
        if normalized == "read":
            if not message_id:
                raise SessionSearchError("read 需要 message_id")
            return {"mode": "read", **(await self.read(message_id))}
        if normalized == "browse":
            sessions = await self.browse(limit=limit if limit else 20)
            return {"mode": "browse", "count": len(sessions), "results": sessions}
        raise SessionSearchError(
            f"未知的召回模式：{mode!r}（可选 discover / scroll / read / browse）"
        )
