"""Offline RAG retrieval evaluation (no model server, no browser; 向量路用本地 ONNX 模型).

Fixes the gap surfaced by the RAG interview review: eval_run.py measures
recall@5 / citation coverage at the *agent* level and needs a real model +
browser, so the RAG layer itself (chunking -> indexing -> retrieval -> fusion)
had no runnable quantitative check.

What this script does:

1. Chunk a fixed embedded Chinese corpus with the real `chunk_text()` from
   app.indexing (structure split -> merge 300-500 chars -> overlap 80 ->
   collapse similar).
2. Index into a throwaway SQLite file with the exact production DDL
   (chunks + chunks_fts_trigram + chunks_fts_chars + sync triggers, same as
   migration 0004).
3. 可选地把向量灌进同一文件的 vec0 表（`build_vectors`），用与生产同一套
   `app.vectors` 实现（真 fastembed 模型 + 查询指令前缀）。
4. 逐路打分：trigram / chars / 向量 / RRF 两路 / RRF 三路。
5. 在**真实仓库语料**上跑两套语义查询集并判门：
   - A 集（无词面重叠）：FTS 几乎必 0 召回，衡量向量路的纯语义能力；
   - B 集（词面可命中）：防"为了语义把词面召回搞坏"。
   同时**如实打印 A 集的 FTS 基线**——增益可归因的前提是基线真的接近 0。

Usage (from backend/):

    uv run python ../scripts/eval_rag.py
    uv run python ../scripts/eval_rag.py --gate 1.0 --json out.json
    uv run python ../scripts/eval_rag.py --no-vector      # 跳过向量路（快速回归）
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sqlite3
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

from app.config import Settings  # noqa: E402
from app.indexing import chunk_text, match_expression  # noqa: E402
from app.rerank import FastEmbedReranker  # noqa: E402
from app.retrieval import (  # noqa: E402
    RERANK_POOL,
    RRF_WEIGHTS,
    gate_candidates,
    rerank_hits,
    route_query,
    rrf,
)
from app.vectors import (  # noqa: E402
    MIN_K,
    VEC_TABLE,
    FastEmbedEmbeddings,
    load_extension_sync,
    pack_vector,
    resolve_cache_dir,
    vec_ddl,
)

RECALL_GATE = 0.8
LIST_DEPTH = 10
#: 阶段 6 的两道数字门槛（见 handoff/vector-path-plan.md §6）
GATE_A_RECALL = 0.70
GATE_B_RECALL = 0.85

#: 固定语料：三篇带标题/表格/长段的中文文档，覆盖真实切块路径的全部分支。
CORPUS: dict[str, str] = {
    "site_playbook.md": """# 站点操作手册

## B站搜索与翻页

在 B 站搜索结果页需要翻页抽字段时，先点击搜索框输入关键词，等待结果列表出现后，
读取每个视频卡片的标题、UP 主和播放量。第二页需要点击页码按钮而不是滚动，
因为结果列表是服务端渲染的，滚动不会触发加载。遇到登录墙时直接上报 needs_user，
不要尝试自动登录。

## 机械键盘评测要点

评测机械键盘主要看轴体手感、键帽材质和连接方式。客制化轴体分线性轴和段落轴，
线性轴适合游戏，段落轴适合打字。无线双模键盘要标注续航时间，背光灯效对标称续航
影响很大，关闭 RGB 后续航通常可以延长一倍。

## 价格监控表

| 商品 | 价格 | 备注 |
|---|---|---|
| 机械键盘 | 399 | 蓝牙双模 |
| 电竞鼠标 | 199 | 无线轻量化 |
""",
    "architecture.md": """# 架构手册

## 数据库迁移规则

迁移编号是冻结的，一个对话只能创建自己的 revision，down_revision 必须严格指向
上一个已存在的迁移。目前 0003 是工具结果表，0004 是切块表，0005 预留给向量表，
向量路上线前 0005 不允许被别的迁移占用。

## 检索设计

检索走双路全文索引：trigram 路索引原文，字符级路把中文逐字插空格后再索引，
两路候选用 RRF 按排名融合，不比较原始分数。整句查询必须拆成窗口短语做 OR，
否则 trigram 必然零召回。切块目标 400 字，上下限 300 到 500，相邻块带 80 字重叠。

## 权限模型

当前版本是完全访问、事前零确认：工具直接执行，人工介入只剩模型主动提问和登录墙。
信任模型是事后审计，任务收尾时输出对外动作清单。
""",
    "content_ops.md": """# 内容运营手册

## 水果短剧脚本

水果拟人短剧的脚本结构是开场冲突、三次反转、结尾梗。角色一致性靠固定的人物设定图
提示词保证，分镜图每次生成都要带同一份角色描述，避免水果表情漂移。

## 剪映剪辑流程

成片先在剪映里拼时间线，配音用文本朗读生成后逐句对齐，字幕自动识别后手动校对
多音字。导出参数固定 1080P 60 帧，封面单独用设定图加标题字。
""",
}

#: (query, [期望出现在 top5 并集里的内容标记])。标记用真实语料里的短句片段。
CASES: list[tuple[str, list[str]]] = [
    ("机械键盘哪个轴体适合打字", ["段落轴适合打字"]),
    ("键盘", ["轴体手感"]),  # 2 字查询：trigram 必零召回，字符级路负责
    ("B站搜索结果怎么翻页", ["点击页码按钮而不是滚动"]),
    ("视频卡片要抽哪些字段", ["标题、UP 主和播放量"]),
    ("迁移编号怎么分配", ["down_revision 必须严格指向"]),
    ("双路检索为什么分数不能直接比", ["按排名融合，不比较原始分数"]),
    ("切块的重叠是多少", ["带 80 字重叠"]),
    ("登录墙了怎么办", ["上报 needs_user"]),
    ("价格表里鼠标多少钱", ["199"]),
    ("水果短剧角色怎么保持一致", ["固定的人物设定图提示词"]),
    ("剪映导出什么参数", ["1080P 60 帧"]),
    ("权限什么时候需要确认", ["事前零确认"]),
]

#: 真实语料：仓库自己的中文设计文档（长文，能切出多块，参数与语义才有区分度）。
REPO_DOCS = [
    "ARCHITECTURE.md",
    "AGENT_DESIGN.md",
    "HARNESS_DESIGN.md",
    "AGENT_ORCHESTRATION.md",
    "MVP_PLAN.md",
    "ROADMAP.md",
]

#: B 集：词面可命中的查询（沿用既有风格）。防的是"为了语义把词面召回搞坏"。
REPO_CASES: list[tuple[str, list[str]]] = [
    ("为什么两个字的中文词查不到", ["只匹配 ≥3 字符"]),
    ("回答没有引用来源算完成吗", ["无来源不算完成"]),
    ("MCP 的 resources 怎么进索引", ["异步抓取入索引队列"]),
    ("向量存储用哪个库", ["sqlite-vec"]),
    ("切块的目标长度是多少", ["300–500 中文字"]),
    ("三层预算是哪三层", ["动作 / token / 时间"]),
    ("索引接受什么来源", ["web_page"]),
    ("工具状态不确定时能重发吗", ["禁止重发"]),
    ("记忆和技能有什么区别", ["事实与偏好"]),
    ("密钥存在哪里", ["唯一放真值"]),
    ("重启后不确定的结果怎么处理", ["先回读核对"]),
    ("检索类任务必须给出什么", ["引用回填"]),
]

#: A 集：**刻意避开语料用词**的改写查询，用来把 FTS 的贡献压到最低，
#: 让任何召回都只能归因于向量路。基线是否真的接近 0 由脚本如实打印（不假设）。
A_CASES: list[tuple[str, list[str]]] = [
    ("为什么两个汉字组成的词搜不出来", ["只匹配 ≥3 字符"]),
    ("答案里没写出处能算做完吗", ["无来源不算完成"]),
    ("外部服务的资源怎么被收进检索库里", ["异步抓取入索引队列"]),
    ("承载向量的那个组件用了什么方案", ["sqlite-vec"]),
    ("每段文本典型多长", ["300–500 中文字"]),
    ("刹车的三个维度分别是什么", ["动作 / token / 时间"]),
    ("哪些内容才有资格进索引", ["web_page"]),
    ("结果不明朗时可以再发一次请求吗", ["禁止重发"]),
    ("长期记住的条目与可复用的流程如何分工", ["事实与偏好"]),
    ("敏感凭据保存在什么地方", ["唯一放真值"]),
    ("服务重启后状态不明确的操作该怎么收尾", ["先回读核对"]),
    ("检索得出的结论必须附上什么", ["引用回填"]),
    ("浏览器定位不到元素时有没有退路", ["截图 + 坐标点击"]),
    ("技能攒多了以后重复的怎么处理", ["写入查重"]),
    ("对话太长以后靠什么压缩", ["对话摘要"]),
    ("动作数量最多能有多少轮", ["最多 20 次工具动作"]),
]


def repo_corpus() -> dict[str, str]:
    return {name: (ROOT / name).read_text(encoding="utf-8") for name in REPO_DOCS}


def build_db(path: pathlib.Path, corpus: dict[str, str] | None = None) -> tuple[sqlite3.Connection, int]:
    """用与迁移 0004 一致的 DDL 建临时库并写入切块。返回 (conn, chunk 数)。

    `corpus=None` 时用模块级 `CORPUS`（`eval_chunk_sweep` 靠替换模块属性来换语料）。
    """
    corpus = CORPUS if corpus is None else corpus
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE chunks (
          row_id INTEGER PRIMARY KEY AUTOINCREMENT,
          id TEXT UNIQUE NOT NULL,
          source_type TEXT NOT NULL,
          source_id TEXT NOT NULL DEFAULT '',
          conversation_id TEXT NOT NULL DEFAULT '',
          task_id TEXT NOT NULL DEFAULT '',
          host TEXT NOT NULL DEFAULT '',
          ordinal INTEGER NOT NULL DEFAULT 0,
          text TEXT NOT NULL,
          char_text TEXT NOT NULL DEFAULT '',
          tokens INTEGER NOT NULL DEFAULT 0,
          hash TEXT NOT NULL DEFAULT '',
          origin TEXT NOT NULL DEFAULT '',
          title_path TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT ''
        );
        CREATE VIRTUAL TABLE chunks_fts_trigram USING fts5(
          text, content='chunks', content_rowid='row_id', tokenize='trigram'
        );
        CREATE VIRTUAL TABLE chunks_fts_chars USING fts5(
          char_text, content='chunks', content_rowid='row_id', tokenize='unicode61'
        );
        CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
          INSERT INTO chunks_fts_trigram(rowid, text) VALUES (new.row_id, new.text);
          INSERT INTO chunks_fts_chars(rowid, char_text) VALUES (new.row_id, new.char_text);
        END;
        """
    )
    total = 0
    for name, body in corpus.items():
        for chunk in chunk_text(body):
            conn.execute(
                "INSERT INTO chunks(id, source_type, ordinal, text, char_text, tokens,"
                " hash, origin, title_path, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    f"{name}:{chunk['hash'][:16]}",
                    "local_file",
                    chunk["ordinal"],
                    chunk["text"],
                    chunk["char_text"],
                    chunk["tokens"],
                    chunk["hash"],
                    name,
                    chunk["title_path"],
                    "eval",
                ),
            )
            total += 1
    conn.commit()
    return conn, total


def fts_search(conn: sqlite3.Connection, query: str, table: str, limit: int) -> list[str]:
    match = match_expression(query, "trigram" if table == "chunks_fts_trigram" else "chars")
    if not match:
        return []
    try:
        rows = conn.execute(
            f"SELECT c.text FROM {table} JOIN chunks c ON c.row_id = {table}.rowid"
            f" WHERE {table} MATCH ? ORDER BY bm25({table}) LIMIT ?",
            (match, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [row[0] for row in rows]


def fts_rows(conn: sqlite3.Connection, query: str, table: str, limit: int) -> list[dict]:
    """带真实 row_id 的候选：RRF 按 row_id 去重，只有真 row_id 才能让同一块跨路加成。"""
    match = match_expression(query, "trigram" if table == "chunks_fts_trigram" else "chars")
    if not match:
        return []
    try:
        rows = conn.execute(
            f"SELECT c.row_id AS row_id, c.text AS text FROM {table}"
            f" JOIN chunks c ON c.row_id = {table}.rowid"
            f" WHERE {table} MATCH ? ORDER BY bm25({table}) LIMIT ?",
            (match, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [{"row_id": row[0], "text": row[1]} for row in rows]


def build_embeddings() -> FastEmbedEmbeddings:
    """按 `Settings` 构造嵌入器——评测必须与线上配置**同一套**模型与两侧前缀。

    早期这里直接 `FastEmbedEmbeddings()`，用的是构造函数默认（空前缀），于是"评测通过"
    与"线上行为"可能不一致，而前缀正是最容易配错的一处（换模型即变）。
    """
    settings = Settings()
    return FastEmbedEmbeddings(
        model_name=settings.embedding_model,
        dim=settings.embedding_dim,
        query_prefix=settings.vector_query_prefix,
        doc_prefix=settings.vector_doc_prefix,
        cache_dir=resolve_cache_dir(settings),
        cpu_mem_arena=settings.embedding_cpu_mem_arena,
        threads=settings.embedding_threads,
    )


def build_vectors(conn: sqlite3.Connection, embeddings) -> int:
    """在同一评测库里建 vec0 表并写入全部 chunk 的向量。返回写入条数。"""
    load_extension_sync(conn)
    conn.execute(vec_ddl(int(embeddings.dim)))
    rows = conn.execute("SELECT row_id, text FROM chunks ORDER BY row_id").fetchall()
    if not rows:
        return 0
    vectors = embeddings.embed([row[1] for row in rows])
    if len(vectors) != len(rows):
        raise RuntimeError(f"嵌入数 {len(vectors)} != chunk 数 {len(rows)}")
    for (row_id, _), vector in zip(rows, vectors, strict=True):
        conn.execute(
            f"INSERT INTO {VEC_TABLE}(row_id, embedding) VALUES (?,?)",
            (row_id, pack_vector(vector)),
        )
    conn.commit()
    return len(rows)


def vector_rows(conn: sqlite3.Connection, query: str, embeddings, limit: int) -> list[dict]:
    payload = embeddings.embed([query], query=True)
    if not payload:
        return []
    rows = conn.execute(
        f"SELECT c.row_id AS row_id, c.text AS text FROM {VEC_TABLE} v"
        f" JOIN chunks c ON c.row_id = v.row_id"
        f" WHERE v.embedding MATCH ? AND k = ? ORDER BY v.distance",
        (pack_vector(payload[0]), max(MIN_K, limit * 8)),
    ).fetchall()
    return [{"row_id": row[0], "text": row[1]} for row in rows[:limit]]


def fused(conn: sqlite3.Connection, query: str, limit: int = LIST_DEPTH) -> list[str]:
    """遗留的两路融合（沿用合成 row_id 序号，保留给 eval_chunk_sweep 的基线）。"""
    lists = [
        [{"row_id": index, "text": text} for index, text in enumerate(fts_search(conn, query, table, limit))]
        for table in ("chunks_fts_trigram", "chunks_fts_chars")
    ]
    merged = rrf(lists)
    return [item["text"] for item in merged]


def fused_rows(
    conn: sqlite3.Connection,
    query: str,
    embeddings,
    paths: int,
    limit: int = LIST_DEPTH,
    weights: list[float] | None = None,
) -> list[str]:
    """按真实 row_id 融合：paths=2 走双 FTS，paths=3 再加向量路。

    `weights=None` 用**生产权重** `RRF_WEIGHTS`（trigram/chars/vector = 1/1/2）；
    显式传 `[1.0, 1.0, 1.0]` 可复现历史等权行为，用于对照。权重按实际路径数截断，
    所以两路调用在两种取值下完全一致（前两位都是 1.0）。
    """
    lists = [
        fts_rows(conn, query, "chunks_fts_trigram", limit),
        fts_rows(conn, query, "chunks_fts_chars", limit),
    ]
    if paths >= 3 and embeddings is not None:
        lists.append(vector_rows(conn, query, embeddings, limit))
    effective = list(RRF_WEIGHTS if weights is None else weights)[: len(lists)]
    return [item["text"] for item in rrf(lists, weights=effective)]


def build_settings() -> Settings:
    """评测用的 Settings：与线上同一套默认（路由阈值、重排池、模型都从这里取）。"""
    return Settings()


def fused_routed(
    conn: sqlite3.Connection,
    query: str,
    embeddings,
    reranker=None,
    *,
    settings: Settings | None = None,
    do_rerank: bool = True,
    limit: int | None = None,
    routing_only: bool = False,
) -> list[str]:
    """**与线上同一套**的检索路径：路由 → 门控 → RRF → （可选）重排。

    直接复用 `app.retrieval` 的 `route_query` / `gate_candidates` / `rerank_hits`，
    不在评测里复刻一份逻辑——复刻必然漂移（此前 build_embeddings 就踩过这个坑）。
    """
    settings = settings or build_settings()
    route = route_query(
        query,
        routing=bool(settings.query_routing),
        lexical_min_coverage=float(settings.fts_min_coverage),
    )
    use_trigram, use_chars, use_vector = route.paths
    trigram = fts_rows(conn, query, "chunks_fts_trigram", LIST_DEPTH) if use_trigram else []
    chars = fts_rows(conn, query, "chunks_fts_chars", LIST_DEPTH) if use_chars else []
    if route.min_coverage > 0.0:
        trigram = gate_candidates(trigram, query, "trigram", route.min_coverage)
        chars = gate_candidates(chars, query, "chars", route.min_coverage)
    vector = (
        vector_rows(conn, query, embeddings, LIST_DEPTH)
        if use_vector and embeddings is not None
        else []
    )
    merged = rrf([trigram, chars, vector], weights=list(RRF_WEIGHTS))
    if do_rerank and not routing_only and route.rerank:
        merged = rerank_hits(merged, query, reranker, int(settings.rerank_pool))
    texts = [item["text"] for item in merged]
    return texts if limit is None else texts[:limit]


def route_kinds(cases) -> dict[str, int]:
    """各查询集的路由分类计数——用来说明"路由对这批查询到底做了什么"。"""
    settings = build_settings()
    counts: dict[str, int] = {}
    for query, _ in cases:
        kind = route_query(
            query,
            routing=bool(settings.query_routing),
            lexical_min_coverage=float(settings.fts_min_coverage),
        ).kind
        counts[kind] = counts.get(kind, 0) + 1
    return counts


def _flat(value: str) -> str:
    """命中判定用：去掉所有空白。语料换行会切断子串，按原文匹配会误判。"""
    return "".join((value or "").split())


def score(cases, ranker, conn, verbose: bool = False, k: int = 5) -> tuple[float, float]:
    """返回 (recall@k, MRR@k)。命中判定：去空白后 hit 包含任一期望标记。

    `k` 默认为 5（门槛口径）。评测候选池天花板时会传 `k=RERANK_POOL`。
    """
    recalls, rrs = [], []
    for query, expected in cases:
        markers = [_flat(m) for m in expected]
        hits = ranker(conn, query)
        top = [_flat(h) for h in hits[:k]]
        found = sum(1 for marker in markers if any(marker in hit for hit in top))
        recalls.append(found / len(markers))
        rr = 0.0
        for rank, hit in enumerate(top, 1):
            if any(marker in hit for marker in markers):
                rr = 1 / rank
                break
        rrs.append(rr)
        if verbose and found < len(markers):
            missing = [m for m in markers if not any(m in hit for hit in top)]
            print(f"    MISS: {query!r} missing={missing} top={[h[:40] for h in top]}")
    return sum(recalls) / len(recalls), sum(rrs) / len(rrs)


def _flat_marker_hits(cases, ranker, conn) -> float:
    """只报 recall@5，用于 A 集的 FTS 基线诊断。"""
    return score(cases, ranker, conn)[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gate", type=float, default=RECALL_GATE, help="合成语料两路融合 recall@5 门槛")
    parser.add_argument("--json", type=pathlib.Path, default=None, help="另存 JSON 报告")
    parser.add_argument("--verbose", action="store_true", help="打印未命中的案例明细")
    parser.add_argument("--no-vector", action="store_true", help="跳过向量路（不加载模型）")
    parser.add_argument(
        "--rerank",
        action="store_true",
        help="启用 cross-encoder 重排（首次会下载 bge-reranker-base ≈1GB）",
    )
    args = parser.parse_args()

    use_vector = not args.no_vector
    settings = build_settings()
    embeddings = build_embeddings() if use_vector else None
    reranker = None
    if args.rerank:
        reranker = FastEmbedReranker(
            cache_dir=resolve_cache_dir(settings),
            cpu_mem_arena=settings.embedding_cpu_mem_arena,
            threads=settings.embedding_threads,
        )
        print(f"[rerank] model={reranker.model} pool={settings.rerank_pool}")
    failures: list[str] = []

    with tempfile.TemporaryDirectory() as tmp:
        # ── 1. 合成语料：与既有基线可比的那一列 ─────────────────────────
        conn, total = build_db(pathlib.Path(tmp) / "eval.sqlite", CORPUS)
        try:
            if total == 0:
                print("FAIL: corpus produced 0 chunks — chunking path is broken")
                return 2
            report: dict = {"chunks": total, "queries": len(CASES), "results": {}}
            if use_vector:
                report["vector_chunks"] = build_vectors(conn, embeddings)
            for label, ranker in (
                ("trigram", lambda c, q: fts_search(c, q, "chunks_fts_trigram", LIST_DEPTH)),
                ("chars", lambda c, q: fts_search(c, q, "chunks_fts_chars", LIST_DEPTH)),
                ("rrf_fusion", fused),
                *(
                    [
                        ("vector", lambda c, q: [row["text"] for row in vector_rows(c, q, embeddings, LIST_DEPTH)]),
                        ("rrf3_fusion", lambda c, q: fused_rows(c, q, embeddings, 3)),
                    ]
                    if use_vector
                    else []
                ),
            ):
                recall, mrr = score(CASES, ranker, conn, verbose=args.verbose and label == "rrf_fusion")
                report["results"][label] = {"recall_at_5": round(recall, 3), "mrr_at_5": round(mrr, 3)}
        finally:
            conn.close()

        print(f"[synthetic] chunks indexed: {report['chunks']}  queries: {report['queries']}")
        for label, metrics in report["results"].items():
            print(f"  {label:<12} recall@5={metrics['recall_at_5']:.3f}  mrr@5={metrics['mrr_at_5']:.3f}")
        fused_recall = report["results"]["rrf_fusion"]["recall_at_5"]
        if fused_recall < args.gate:
            failures.append(f"合成语料两路融合 recall@5 {fused_recall} < 门槛 {args.gate}")

        if not use_vector:
            report["skipped_vector"] = True
        else:
            # ── 2. 真实仓库语料：A/B 两套查询集 + 门 2 / 门 3 ───────────
            conn, total = build_db(pathlib.Path(tmp) / "repo.sqlite", repo_corpus())
            try:
                indexed = build_vectors(conn, embeddings)
                gates: dict = {"chunks": total, "vector_chunks": indexed, "sets": {}}
                for name, cases, gate in (
                    ("A_no_overlap", A_CASES, GATE_A_RECALL),
                    ("B_mixed", REPO_CASES, GATE_B_RECALL),
                ):
                    rows = {}
                    rankers = [
                        ("trigram", lambda c, q: fts_search(c, q, "chunks_fts_trigram", LIST_DEPTH)),
                        ("chars", lambda c, q: fts_search(c, q, "chunks_fts_chars", LIST_DEPTH)),
                        ("rrf_fusion", lambda c, q: fused_rows(c, q, None, 2)),
                        ("vector", lambda c, q: [row["text"] for row in vector_rows(c, q, embeddings, LIST_DEPTH)]),
                        ("rrf3_fusion", lambda c, q: fused_rows(c, q, embeddings, 3)),
                        # 历史等权，仅作对照：用来量化"给向量路加权"这一步本身贡献了多少
                        ("rrf3_equal", lambda c, q: fused_rows(c, q, embeddings, 3, weights=[1.0, 1.0, 1.0])),
                        # 新增：路由 + 门控（不重排）——与线上默认（rerank_enabled=False）一致
                        ("routed", lambda c, q: fused_routed(c, q, embeddings, do_rerank=False)),
                    ]
                    if reranker is not None:
                        rankers.append(
                            ("rerank", lambda c, q: fused_routed(c, q, embeddings, reranker))
                        )
                    for label, ranker in rankers:
                        recall, mrr = score(cases, ranker, conn)
                        rows[label] = {"recall_at_5": round(recall, 3), "mrr_at_5": round(mrr, 3)}
                    rows["gate"] = gate
                    gates["sets"][name] = rows

                    print(f"\n[repo_docs · {name}] chunks {total} / 向量 {indexed} / 查询 {len(cases)}")
                    print(f"  路由分类：{route_kinds(cases)}")
                    for label, metrics in rows.items():
                        if label == "gate":
                            continue
                        print(f"  {label:<12} recall@5={metrics['recall_at_5']:.3f}  mrr@5={metrics['mrr_at_5']:.3f}")

                    # 重排的天花板：RRF 融合后取 pool 大小，重排最多只能把池内正确块提上来
                    ceiling = score(
                        cases,
                        lambda c, q: fused_routed(
                            c, q, embeddings, do_rerank=False, limit=RERANK_POOL
                        ),
                        conn,
                        k=RERANK_POOL,
                    )[0]
                    fts_baseline = rows["rrf_fusion"]["recall_at_5"]
                    fused_value = rows["rrf3_fusion"]["recall_at_5"]
                    pipeline_label = "rerank" if "rerank" in rows else "routed"
                    pipeline_value = rows[pipeline_label]["recall_at_5"]
                    print(
                        f"  FTS 两路基线 {fts_baseline:.3f} → 三路融合 {fused_value:.3f}"
                        f" → 管线({pipeline_label}) {pipeline_value:.3f}（门槛 {gate}）"
                    )
                    print(f"  候选池天花板 recall@{RERANK_POOL}={ceiling:.3f}（重排的理论上限）")
                    if name == "A_no_overlap" and fts_baseline > 0.0:
                        print(f"  注：A 集 FTS 基线不为 0（{fts_baseline:.3f}），增益不能完全归因于向量路。")
                    if pipeline_value < gate:
                        failures.append(
                            f"{name} 管线({pipeline_label}) recall@5 {pipeline_value} < 门槛 {gate}"
                        )
                report["gates"] = gates
            finally:
                conn.close()

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\njson report -> {args.json}")

    if failures:
        for line in failures:
            print(f"FAIL: {line}")
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
