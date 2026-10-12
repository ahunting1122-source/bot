"""RAG 读取侧：混合检索、RRF 融合与第三档重排。

三路候选各自出候选，按排名做 RRF 融合：FTS5 trigram ‖ FTS5 字符级 ‖ 向量。
向量路（阶段 6：sqlite-vec + fastembed）未开启或不可用时**如实返回空列表**，
不假装检索成功——融合时它不贡献候选，不会污染排名。

融合之后接**第三档重排**（`HARNESS_DESIGN.md §4.3` 的阶段 3，`app/rerank.py`）：
RRF 只负责召回候选池，排序交给 cross-encoder。这条是实测逼出来的——见 `RRF_WEIGHTS`
的长注释与 `handoff/2026-10-08-hermes-deep-dive.md §6`。

另有一条**查询类型路由**（`route_query`）：专名/标识符/路径/引号 → 走 FTS 优先且门控
更严；≤2 字 → 关掉必然零召回的 trigram 路；其余自然语言问句 → 三路均衡。
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from app.indexing import FtsIndexer, char_query_parts, split_chars, trigram_query_parts
from app.storage import Store

log = logging.getLogger("orbit.retrieval")

RRF_K = 60
LIST_DEPTH = 10
#: 三路融合的路径权重 (trigram, chars, vector)。**默认等权 = 历史行为**。
#:
#: `rrf` 开放了 `weights` 参数，但这里默认不改权重，因为**实测不支持加权**。
#: 在 195 块真实语料 + A/B 双集上对照过 (1,1,1) / (1,1,2) / (1,1,3) 三档
#: （见 `scripts/eval_embedding_models.py` 的 WEIGHT_OPTIONS）：
#:
#: - bge-small-zh：加权把 A 集拉起来（0.375→0.562）却把 B 集压下去（0.750→0.667）；
#: - embeddinggemma-300m 带 task 前缀（现默认）：三档在 A 集是 0.500/0.500/0.562、
#:   B 集是 0.750/0.750/0.750——差异不超过 1 条查询（A 集 16 条 → 每条 6.25%）。
#:
#: 结论：**加权只是把 RRF 的损失在 A/B 之间搬来搬去，没有消除损失**。真正的问题是
#: 两路 FTS 用 OR 词组查询几乎总能返回满 10 条候选（即使全是噪声），其 rank-1 噪声
#: 拿到 1/61 就能与向量路的正确命中同量级。**对症的做法是重排（`app/rerank.py`）与
#: 候选质量门控（`fts_min_coverage`），而不是继续调权重**——这就是 Hermes 把
#: rrf/linear/cross-encoder 并列为三档、而真正治"融合失真"的是第三档的原因。
RRF_WEIGHTS = (1.0, 1.0, 1.0)
#: 预取门槛：至少在某一路的 top5 里出现过（1/(k+5)）才注入，避免噪声撑爆上下文
PREFETCH_MIN_SCORE = 1 / (RRF_K + 5)
#: 单次检索注入上下文的上限
INJECT_CHARS = 2500
SNIPPET_CHARS = 400
#: 重排候选池大小的默认值（`Settings.rerank_pool` 覆盖）。
#: **重排只能重排池内的**——`recall@pool` 是重排效果的硬天花板。
RERANK_POOL = 20
#: 词面路由（lexical）下的候选覆盖率门槛（`Settings.fts_min_coverage` 覆盖）。
#: 这类查询理应被目标块**逐字**覆盖；覆盖率不足说明只是沾上一个词窗，按噪声丢弃。
LEXICAL_MIN_COVERAGE = 0.5


def best_rank_score(rank: int) -> float:
    return 1 / (RRF_K + max(1, rank))


def rrf(
    ranked_lists: list[list[dict]],
    k: int = RRF_K,
    weights: list[float] | None = None,
) -> list[dict]:
    """Reciprocal Rank Fusion：只用排名，不用原始分数，避免两路分数不可比。

    `weights` 是**路径权重**，按下标与 `ranked_lists` 对齐；不传即等权。开放这个参数
    是为了让"给某一路加权"这件事可以被实测，而不是埋在公式里。目前的默认是等权，
    因为加权实测无效（见 `RRF_WEIGHTS` 的注释与对照数据）。

    另外注意 `rrf` 的**绝对分数**还被 `PREFETCH_MIN_SCORE`（= 1/(RRF_K+5)）当预取门槛
    用：把任何一路的权重降到 1.0 以下，都会让原本能过门槛的命中掉到门槛之下，等于
    顺手改了预取行为。要调权重就上调（>1.0），不要下调。

    传进来的空列表（未启用的路径）不贡献候选，但**仍然占一个下标**，这样
    `paths` 标签 `("trigram","chars","vector")[index]` 恒与真实路径对齐。
    """
    if weights is None:
        weights = [1.0] * len(ranked_lists)
    merged: dict[object, dict] = {}
    for index, ranked in enumerate(ranked_lists):
        weight = weights[index] if index < len(weights) else 1.0
        for rank, item in enumerate(ranked, 1):
            key = item.get("row_id")
            if key is None:
                continue
            entry = merged.get(key)
            if entry is None:
                entry = {**item, "score": 0.0, "paths": []}
                merged[key] = entry
            entry["score"] += weight / (k + rank)
            entry["paths"].append(("trigram", "chars", "vector")[index])
    return sorted(merged.values(), key=lambda item: (-item["score"], item.get("row_id") or 0))


def snippet(value: str, limit: int = SNIPPET_CHARS) -> str:
    body = " ".join((value or "").split())
    return body[:limit]


# ── 查询类型路由 ────────────────────────────────────────────────────────────
#: 引号包住的片段：用户在刻意指定字面量
_QUOTED = re.compile(r"[\"'“”‘’「」『』][^\"'“”‘’「」『』]+[\"'“”‘’「」『』]")
#: ASCII 标识符（工具名、函数名、字段名）：≥2 个字符的字母开头串
_ASCII_IDENT = re.compile(r"[A-Za-z][A-Za-z0-9_]{1,}")
#: 路径或带扩展名的文件名
_PATH_OR_EXT = re.compile(
    r"[\\/]|\.(?:md|py|ts|tsx|js|jsx|json|ya?ml|sql|toml|ini|css|html|sh|ps1)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QueryRoute:
    """一次检索的路由决策（纯数据，可单测）。"""

    kind: str  # lexical | short | semantic
    paths: tuple[bool, bool, bool]  # (trigram, chars, vector) 是否参与查询
    min_coverage: float  # 候选覆盖率门控阈值；0 = 不门控
    rerank: bool
    note: str = ""


def route_query(
    query: str,
    *,
    routing: bool = True,
    lexical_min_coverage: float = LEXICAL_MIN_COVERAGE,
) -> QueryRoute:
    """按查询表面特征选路（Hermes 的"不同查询要不同工具"）。

    - `lexical`：引号片段 / ASCII 标识符 / 路径或扩展名。这类查询**词面命中比语义更可靠**
      （Hermes 的判词：向量可能召回"语义相邻但用错了 API"的块），所以门控收紧到
      `lexical_min_coverage`，宁可少要候选也不要噪声。
    - `short`：去空白后 ≤2 字。trigram 只索引 ≥3 字符，**必然 0 召回**，关掉它省一次查询。
    - `semantic`：其余自然语言问句，三路均衡、不门控。

    注意：纯中文散文查询落进 `semantic` 时这里是**恒等变换**——如实记录，
    不要把它当成"路由解决了融合问题"。
    """
    text = query or ""
    compact = re.sub(r"\s+", "", text)
    if not routing:
        return QueryRoute("semantic", (True, True, True), 0.0, True, "routing_off")
    if _QUOTED.search(text) or _PATH_OR_EXT.search(text) or _ASCII_IDENT.search(text):
        return QueryRoute(
            "lexical", (True, True, True), max(0.0, lexical_min_coverage), True, "identifier"
        )
    if len(compact) <= 2:
        return QueryRoute("short", (False, True, True), 0.0, True, "short_query")
    return QueryRoute("semantic", (True, True, True), 0.0, True, "natural_language")


def _windows(query: str, mode: str) -> list[str]:
    """查询词窗（与 `match_expression` 同源），去重保序。"""
    raw = trigram_query_parts(query) if mode == "trigram" else char_query_parts(query)
    ordered: list[str] = []
    for part in raw:
        cleaned = (part or "").replace('"', "").strip()
        if cleaned and cleaned not in ordered:
            ordered.append(cleaned)
    return ordered


def coverage(query: str, text: str, mode: str) -> float:
    """命中块覆盖了查询词窗的几分之几（0–1）。

    FTS5 的 OR 词组只要命中**任意一个**词窗就返回整行，于是"只沾上一个 3 字窗"的块
    也会进候选并拿到 rank。这个比值就是用来识别这类噪声的朴素信号。
    """
    parts = _windows(query, mode)
    if not parts:
        return 0.0
    if mode == "trigram":
        body = re.sub(r"\s+", "", text or "")
        if not body:
            return 0.0
        return sum(1 for part in parts if part in body) / len(parts)
    tokens = split_chars(text or "").split()
    if not tokens:
        return 0.0
    grams = {" ".join(tokens)}
    grams.update(" ".join(tokens[index : index + 2]) for index in range(len(tokens) - 1))
    return sum(1 for part in parts if part in grams) / len(parts)


def gate_candidates(
    candidates: list[dict], query: str, mode: str, threshold: float
) -> list[dict]:
    """按覆盖率丢掉噪声候选（保留者的相对顺序不变，保证 RRF 排名语义）。"""
    if threshold <= 0.0 or not candidates:
        return candidates
    return [item for item in candidates if coverage(query, str(item.get("text") or ""), mode) >= threshold]


async def vector_candidates(
    query: str,
    conversation_id: str,
    host: str,
    source_type: str,
    limit: int,
    *,
    embeddings=None,
    vectors=None,
) -> list[dict]:
    """向量路：查询嵌入 → vec0 KNN。

    候选**必须带 `row_id`**：`rrf()` 里 `if key is None: continue` 会把不带 row_id 的
    候选静默丢掉，表现是"接了向量路但召回毫无变化"——极难排查，故有单测锁住。

    嵌入是同步 CPU 调用，放 `to_thread`；任何失败都退化为空列表（不拖垮检索）。
    """
    if embeddings is None or vectors is None:
        return []
    try:
        payload = await asyncio.to_thread(embeddings.embed, [query], query=True)
    except Exception as exc:  # 模型缺失 / 下载失败 / 推理异常
        log.warning("查询嵌入失败，向量路退化为空：%s", exc)
        return []
    if not payload:
        return []
    return await vectors.search(
        payload[0],
        limit=limit,
        conversation_id=conversation_id,
        host=host,
        source_type=source_type,
    )


def rerank_hits(hits: list[dict], query: str, reranker, pool: int) -> list[dict]:
    """候选池 → cross-encoder 重排；池外内容原序接在后面（不丢召回）。

    **同步 CPU 调用**（调用方放 `to_thread`）。抽成模块级纯函数是为了让离线评测
    （`scripts/eval_rag.py`）能与线上**走同一段代码**，避免评测复刻一份而漂移。
    """
    if not hits or not getattr(reranker, "enabled", False):
        return hits
    pool_items = hits[: max(1, int(pool or 1))]
    tail = hits[len(pool_items) :]
    documents = [str(hit.get("text") or "") for hit in pool_items]
    order = reranker.rerank(query, documents, top_k=len(documents))
    valid = [index for index in order if 0 <= index < len(pool_items)]
    missing = [index for index in range(len(pool_items)) if index not in set(valid)]
    head = []
    for index in valid + missing:
        hit = pool_items[index]
        hit["reranked"] = True
        head.append(hit)
    return head + tail


class FtsRetriever:
    """默认检索器：FTS5 双路 + 向量路 → RRF → （可选）cross-encoder 重排。"""

    def __init__(
        self,
        store: Store,
        indexer: FtsIndexer | None = None,
        vectors=None,
        embeddings=None,
        reranker=None,
        *,
        routing: bool = True,
        lexical_min_coverage: float = LEXICAL_MIN_COVERAGE,
        rerank_pool: int = RERANK_POOL,
    ):
        self.store = store
        self.indexer = indexer or FtsIndexer(store)
        self.vectors = vectors
        self.embeddings = embeddings
        self.reranker = reranker
        self.routing = routing
        self.lexical_min_coverage = lexical_min_coverage
        self.rerank_pool = max(1, int(rerank_pool or RERANK_POOL))

    async def _leg(
        self, query: str, mode: str, *, conversation_id: str, host: str, source_type: str
    ) -> list[dict]:
        return await self.indexer.search(
            query,
            conversation_id=conversation_id,
            host=host,
            source_type=source_type,
            limit=LIST_DEPTH,
            mode=mode,
        )

    async def _rerank(self, query: str, hits: list[dict]) -> list[dict]:
        try:
            return await asyncio.to_thread(
                rerank_hits, hits, query, self.reranker, self.rerank_pool
            )
        except Exception as exc:  # 模型加载/推理失败 → 如实退回原顺序
            log.warning("重排失败，退回 RRF 顺序：%s", exc)
            return hits

    async def search(
        self,
        query: str,
        *,
        scope: str = "task",
        conversation_id: str = "",
        host: str = "",
        source_type: str = "",
        top_k: int = 5,
    ) -> list[dict]:
        query = (query or "").strip()
        if len(query) < 2:
            return []
        route = route_query(
            query, routing=self.routing, lexical_min_coverage=self.lexical_min_coverage
        )
        # 检索范围默认按任务收窄（当前会话）；全局检索必须显式指定 scope="global"。
        narrowed = "" if scope == "global" else conversation_id
        use_trigram, use_chars, use_vector = route.paths
        lexical = (
            await self._leg(
                query, "trigram", conversation_id=narrowed, host=host, source_type=source_type
            )
            if use_trigram
            else []
        )
        chars = (
            await self._leg(
                query, "chars", conversation_id=narrowed, host=host, source_type=source_type
            )
            if use_chars
            else []
        )
        # 词面路由下收紧候选：只沾一个词窗的块按噪声丢弃（见 coverage 的 docstring）
        if route.min_coverage > 0.0:
            lexical = gate_candidates(lexical, query, "trigram", route.min_coverage)
            chars = gate_candidates(chars, query, "chars", route.min_coverage)
        vector = (
            await vector_candidates(
                query,
                narrowed,
                host,
                source_type,
                LIST_DEPTH,
                embeddings=self.embeddings,
                vectors=self.vectors,
            )
            if use_vector
            else []
        )
        hits = rrf([lexical, chars, vector], weights=list(RRF_WEIGHTS))
        if route.rerank and getattr(self.reranker, "enabled", False) and len(hits) > 1:
            hits = await self._rerank(query, hits)
        for hit in hits:
            hit["snippet"] = snippet(hit.get("text") or "")
        return hits[: max(0, top_k)]


def format_hits(query: str, scope: str, hits: list[dict], limit: int = INJECT_CHARS) -> dict:
    """给 retriever_search 的返回体：总量受控，空结果如实返回。"""
    payload: list[dict] = []
    used = 0
    for hit in hits:
        body = snippet(hit.get("text") or "", 800)
        cost = len(body)
        if used + cost > limit:
            break
        used += cost
        payload.append(
            {
                "text": body,
                "origin": str(hit.get("origin") or ""),
                "source_type": str(hit.get("source_type") or ""),
                "score": round(float(hit.get("score") or 0), 4),
            }
        )
    return {"hits": payload, "count": len(payload), "scope": scope, "query": query}


def build_retriever(settings, store: Store, reranker=None) -> FtsRetriever:
    from app.vectors import build_embeddings, build_vector_store

    if reranker is None:
        from app.rerank import build_reranker

        reranker = build_reranker(settings)
    return FtsRetriever(
        store,
        vectors=build_vector_store(settings, store),
        embeddings=build_embeddings(settings),
        reranker=reranker,
        routing=bool(getattr(settings, "query_routing", True)),
        lexical_min_coverage=float(
            getattr(settings, "fts_min_coverage", LEXICAL_MIN_COVERAGE) or 0.0
        ),
        rerank_pool=int(getattr(settings, "rerank_pool", RERANK_POOL) or RERANK_POOL),
    )
