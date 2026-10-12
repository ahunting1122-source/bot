"""候选嵌入模型同条件对比（真实仓库语料 + A/B 查询集）。

背景：阶段 6 用 `BAAI/bge-small-zh-v1.5`（24M 参数）时，两个数字门槛都没过
（A 集向量单独 recall@5 0.438、三路融合 0.375；B 集 0.667）。本脚本回答一个问题：
**换更大的中文/多语言嵌入模型，向量路的天花板能抬到多高？**

设计约定（与 `eval_rag.py` 保持可比）

- 语料/查询集直接 `import eval_rag` 复用（`repo_corpus()` / `A_CASES` / `REPO_CASES` /
  `build_db` / `fts_rows` / `_flat`），保证与正式门槛评测是同一批数据。
- 检索用**暴力余弦**取 top-k（评测语料归一化后 L2 与余弦同序，排名与 vec0 一致）。
  这样不必为每个候选模型重建 vec0 表，便于快速横向比较；正式门槛仍走 `eval_rag.py`
  的真实 vec0 路径。
- 每个模型分别测「不加前缀」与「加该模型文档里写明的查询前缀」两档。**文档侧前缀**
  单独控制（gemma 要求、Qwen3 不要求）。
- 记录加载耗时、嵌入吞吐、进程工作集峰值——本机只有 16GB 内存且常年占用偏高，
  内存是选型的真实约束。

Usage (from backend/):

    uv run python ../scripts/eval_embedding_models.py
    uv run python ../scripts/eval_embedding_models.py --models bge-small-zh Qwen/Qwen3-Embedding-0.6B-Q
    uv run python ../scripts/eval_embedding_models.py --topk 5 --json ../data/eval_embedding_models.json
"""

from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes
import gc
import json
import os
import pathlib
import sys
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(HERE))

#: 缓存默认落在 %TEMP%\fastembed_cache（C 盘），模型动辄 1–2.4GB，
#: 必须先钉到项目盘再 import fastembed。正式运行也建议显式设置这个变量。
os.environ.setdefault("FASTEMBED_CACHE_PATH", str(ROOT / "data" / "fastembed_cache"))

import eval_rag  # noqa: E402
import numpy as np  # noqa: E402

from app.retrieval import rrf  # noqa: E402

#: 查询指令前缀（文档里写明的原文），`doc_prefix` 同理。空串 = 不加。
#: 参考：BAAI bge v1.5 —— 官方明确不需要前缀；Qwen3-Embedding —— `Instruct: {task}\nQuery:{q}`；
#: embeddinggemma —— query `task: search result | query: {q}`、doc `title: none | text: {t}`。
QWEN3_TASK = "Given a web search query, retrieve relevant passages that answer the query"

#: 融合权重对照档：(trigram, chars, vector)。`eq` 是历史等权，`v2`/`v3` 是给向量路加权。
#: 目的是把"换模型带来的向量路增益"与"RRF 融合吃掉多少增益"分开看。
WEIGHT_OPTIONS: list[tuple[str, tuple[float, float, float]]] = [
    ("eq", (1.0, 1.0, 1.0)),
    ("v2", (1.0, 1.0, 2.0)),
    ("v3", (1.0, 1.0, 3.0)),
]

CANDIDATES: list[dict] = [
    {
        "name": "BAAI/bge-small-zh-v1.5",
        "dim": 512,
        "tag": "基线（当前）",
        "query_prefix": "",
        "doc_prefix": "",
        "prefix_variants": [
            ("none", "", ""),
        ],
    },
    {
        "name": "jinaai/jina-embeddings-v2-base-zh",
        "dim": 768,
        "tag": "中文专用 · 0.64GB",
        "query_prefix": "",
        "doc_prefix": "",
        "prefix_variants": [
            ("none", "", ""),
        ],
    },
    {
        "name": "google/embeddinggemma-300m",
        "dim": 768,
        "tag": "Google · 1.24GB · MRL",
        "query_prefix": "task: search result | query: ",
        "doc_prefix": "title: none | text: ",
        "prefix_variants": [
            ("none", "", ""),
            ("task", "task: search result | query: ", "title: none | text: "),
            ("task@512", "task: search result | query: ", "title: none | text: ", 512),
        ],
    },
    {
        "name": "Qwen/Qwen3-Embedding-0.6B-Q",
        "dim": 1024,
        "tag": "Qwen3 int8 · 1.12GB · MRL",
        "query_prefix": f"Instruct: {QWEN3_TASK}\nQuery:",
        "doc_prefix": "",
        "prefix_variants": [
            ("instruct", f"Instruct: {QWEN3_TASK}\nQuery:", ""),
            ("instruct@512", f"Instruct: {QWEN3_TASK}\nQuery:", "", 512),
            ("instruct@768", f"Instruct: {QWEN3_TASK}\nQuery:", "", 768),
            ("none", "", ""),
        ],
    },
    {
        "name": "Qwen/Qwen3-Embedding-0.6B",
        "dim": 1024,
        "tag": "Qwen3 全精度 · 2.38GB · MRL",
        "query_prefix": f"Instruct: {QWEN3_TASK}\nQuery:",
        "doc_prefix": "",
        "prefix_variants": [
            ("instruct", f"Instruct: {QWEN3_TASK}\nQuery:", ""),
            ("instruct@512", f"Instruct: {QWEN3_TASK}\nQuery:", "", 512),
        ],
    },
]


def working_set_mb() -> float:
    """当前进程工作集（MB）。Windows 专用，失败返回 0。"""
    try:
        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", ctypes.wintypes.DWORD),
                ("PageFaultCount", ctypes.wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        counters = PMC()
        counters.cb = ctypes.sizeof(PMC)
        handle = ctypes.windll.kernel32.GetCurrentProcess()
        ok = ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.wintypes.HANDLE(handle), ctypes.byref(counters), counters.cb
        )
        if not ok:
            return 0.0
        return counters.PeakWorkingSetSize / 1024 / 1024
    except Exception:
        return 0.0


def available_mb() -> float:
    try:
        class MSX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        m = MSX()
        m.dwLength = ctypes.sizeof(MSX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.ullAvailPhys / 1024 / 1024
    except Exception:
        return 0.0


def topk(query_vector: np.ndarray, matrix: np.ndarray, k: int) -> list[int]:
    """归一化向量上按点积取 top-k（与余弦同序）。"""
    scores = matrix @ query_vector
    k = min(k, scores.shape[0])
    idx = np.argpartition(-scores, k - 1)[:k]
    return [int(i) for i in idx[np.argsort(-scores[idx])]]


def score_vectors(cases, queries_vec: dict[str, np.ndarray], matrix: np.ndarray, texts: list[str], k: int) -> tuple[float, float]:
    recalls, rrs = [], []
    for query, expected in cases:
        markers = [eval_rag._flat(m) for m in expected]
        hits = [texts[i] for i in topk(queries_vec[query], matrix, k)]
        top = [eval_rag._flat(h) for h in hits]
        found = sum(1 for marker in markers if any(marker in hit for hit in top))
        recalls.append(found / len(markers))
        rr = 0.0
        for rank, hit in enumerate(top, 1):
            if any(marker in hit for marker in markers):
                rr = 1 / rank
                break
        rrs.append(rr)
    return sum(recalls) / len(recalls), sum(rrs) / len(rrs)


def run_candidate(spec: dict, conn, texts: list[str], topk_n: int) -> dict:
    from fastembed import TextEmbedding

    arena = spec.get("mem_arena", False)
    print(f"\n=== {spec['name']}  ({spec['tag']}, dim={spec['dim']}) ===")
    print(f"    可用内存 {available_mb():.0f} MB · 缓存 {os.environ['FASTEMBED_CACHE_PATH']} · cpu_mem_arena={arena}")
    t0 = time.perf_counter()
    try:
        kwargs: dict = {"model_name": spec["name"], "enable_cpu_mem_arena": arena}
        if spec.get("threads"):
            kwargs["threads"] = spec["threads"]
        model = TextEmbedding(**kwargs)
    except Exception as exc:
        print(f"    加载失败：{type(exc).__name__}: {exc}")
        return {"model": spec["name"], "error": f"{type(exc).__name__}: {exc}"}
    load_s = time.perf_counter() - t0
    # 预热：onnxruntime 首次推理含 session 初始化，不预热会把吞吐低估一个量级。
    t_warm = time.perf_counter()
    list(model.embed(["预热文本"]))
    warmup_s = time.perf_counter() - t_warm
    peak = working_set_mb()
    print(f"    加载 {load_s:.1f}s · 预热 {warmup_s:.1f}s · 加载后工作集峰值 {peak:.0f} MB")

    result: dict = {
        "model": spec["name"],
        "dim": spec["dim"],
        "tag": spec["tag"],
        "cpu_mem_arena": arena,
        "load_seconds": round(load_s, 1),
        "warmup_seconds": round(warmup_s, 1),
        "peak_working_set_mb_after_load": round(peak),
        "variants": {},
    }

    try:
        for variant_spec in spec["prefix_variants"]:
            label, q_prefix, d_prefix = variant_spec[0], variant_spec[1], variant_spec[2]
            truncate = variant_spec[3] if len(variant_spec) > 3 else None
            tag = f"{label}{'' if truncate is None else f'@{truncate}'}"
            t1 = time.perf_counter()
            docs = [f"{d_prefix}{t}" for t in texts] if d_prefix else list(texts)
            doc_vecs = np.asarray(list(model.embed(docs)), dtype=np.float32)
            embed_s = time.perf_counter() - t1
            if truncate:
                doc_vecs = doc_vecs[:, :truncate]
            norms = np.linalg.norm(doc_vecs, axis=1, keepdims=True)
            norms[norms == 0] = 1.0
            matrix = doc_vecs / norms
            actual_dim = matrix.shape[1]
            peak2 = working_set_mb()

            variant: dict = {
                "prefix": label,
                "query_prefix": q_prefix,
                "doc_prefix": d_prefix,
                "truncate_to": truncate,
                "embed_seconds": round(embed_s, 2),
                "embed_qps": round(len(docs) / embed_s, 1) if embed_s else None,
                "dim_actual": int(actual_dim),
                "peak_working_set_mb": round(peak2),
                "score_error": "",
                "sets": {},
            }
            if actual_dim != spec["dim"] and not truncate:
                variant["score_error"] = f"实际维度 {actual_dim} != 声明 {spec['dim']}"

            a_queries = [q for q, _ in eval_rag.A_CASES]
            b_queries = [q for q, _ in eval_rag.REPO_CASES]
            all_queries = list(dict.fromkeys(a_queries + b_queries))
            prepared = [f"{q_prefix}{q}" for q in all_queries] if q_prefix else all_queries
            q_vecs_raw = np.asarray(list(model.embed(prepared)), dtype=np.float32)
            if truncate:
                q_vecs_raw = q_vecs_raw[:, :truncate]
            q_norms = np.linalg.norm(q_vecs_raw, axis=1, keepdims=True)
            q_norms[q_norms == 0] = 1.0
            q_vecs = q_vecs_raw / q_norms
            queries_vec = {q: q_vecs[i] for i, q in enumerate(all_queries)}

            for set_name, cases in (("A_no_overlap", eval_rag.A_CASES), ("B_mixed", eval_rag.REPO_CASES)):
                recall, mrr = score_vectors(cases, queries_vec, matrix, texts, topk_n)
                # 三路融合：等权 vs 向量加权（看 RRF 是否正在吃掉向量路的增益）
                variant["sets"][set_name] = {
                    "vector_recall_at_5": round(recall, 3),
                    "vector_mrr_at_5": round(mrr, 3),
                }
                for wlabel, weights in WEIGHT_OPTIONS:
                    recalls, rrs = [], []
                    for query, expected in cases:
                        v_rows = [
                            {"row_id": int(i), "text": texts[int(i)]}
                            for i in topk(queries_vec[query], matrix, eval_rag.LIST_DEPTH)
                        ]
                        lists = [
                            eval_rag.fts_rows(conn, query, "chunks_fts_trigram", eval_rag.LIST_DEPTH),
                            eval_rag.fts_rows(conn, query, "chunks_fts_chars", eval_rag.LIST_DEPTH),
                            v_rows,
                        ]
                        merged = rrf(lists, weights=list(weights))
                        markers = [eval_rag._flat(m) for m in expected]
                        top = [eval_rag._flat(item["text"]) for item in merged[:topk_n]]
                        found = sum(1 for marker in markers if any(marker in hit for hit in top))
                        recalls.append(found / len(markers))
                        rr = 0.0
                        for rank, hit in enumerate(top, 1):
                            if any(marker in hit for marker in markers):
                                rr = 1 / rank
                                break
                        rrs.append(rr)
                    variant["sets"][set_name][f"rrf3_{wlabel}"] = {
                        "recall_at_5": round(sum(recalls) / len(recalls), 3),
                        "mrr_at_5": round(sum(rrs) / len(rrs), 3),
                    }
                print(
                    f"    [{tag:<12}] {set_name:<13} 向量 recall@5={recall:.3f} mrr={mrr:.3f}"
                    + "".join(
                        f" | 融合{wlabel} {variant['sets'][set_name][f'rrf3_{wlabel}']['recall_at_5']:.3f}"
                        for wlabel, _ in WEIGHT_OPTIONS
                    )
                )
            result["variants"][label] = variant

            del doc_vecs, matrix, q_vecs_raw, q_vecs
            gc.collect()
    finally:
        del model
        gc.collect()
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="*", default=None, help="只跑指定模型（名字或别名）")
    parser.add_argument("--topk", type=int, default=5, help="recall@k 的 k")
    parser.add_argument("--json", type=pathlib.Path, default=None, help="另存 JSON")
    args = parser.parse_args()

    specs = CANDIDATES
    if args.models:
        wanted = [m.lower() for m in args.models]
        specs = [s for s in CANDIDATES if any(w in s["name"].lower() for w in wanted)]
        if not specs:
            print(f"没有匹配的候选：{args.models}")
            return 2

    with tempfile.TemporaryDirectory() as tmp:
        db_path = pathlib.Path(tmp) / "repo.sqlite"
        conn, total = eval_rag.build_db(db_path, eval_rag.repo_corpus())
        texts = [row[0] for row in conn.execute("SELECT text FROM chunks ORDER BY row_id").fetchall()]
        print(f"真实仓库语料：{total} 块 · A 集 {len(eval_rag.A_CASES)} 条 · B 集 {len(eval_rag.REPO_CASES)} 条")
        print(f"内存总计 {available_mb():.0f} MB 可用 · topk={args.topk}")

        reports = []
        for spec in specs:
            reports.append(run_candidate(spec, conn, texts, args.topk))
        conn.close()

    print("\n====== 汇总（recall@5：向量单路 / 三路融合各权重档）======")
    header = (
        f"{'模型':<30}{'档':<11}{'维':>4}{'A向量':>7}{'A融合eq':>8}{'A融合v2':>8}"
        f"{'B向量':>7}{'B融合eq':>8}{'B融合v2':>8}{'加载s':>7}{'峰值MB':>7}"
    )
    print(header)
    print("-" * len(header))
    for rep in reports:
        if rep.get("error"):
            print(f"{rep['model']:<30}  加载失败：{rep['error'][:60]}")
            continue
        for label, variant in rep["variants"].items():
            a = variant["sets"]["A_no_overlap"]
            b = variant["sets"]["B_mixed"]
            tag = f"{label}{'' if variant['truncate_to'] is None else '@' + str(variant['truncate_to'])}"
            print(
                f"{rep['model']:<30}{tag:<11}{variant['dim_actual']:>4}"
                f"{a['vector_recall_at_5']:>7.3f}{a['rrf3_eq']['recall_at_5']:>8.3f}"
                f"{a['rrf3_v2']['recall_at_5']:>8.3f}"
                f"{b['vector_recall_at_5']:>7.3f}{b['rrf3_eq']['recall_at_5']:>8.3f}"
                f"{b['rrf3_v2']['recall_at_5']:>8.3f}"
                f"{rep['load_seconds']:>7.1f}{variant['peak_working_set_mb']:>7}"
            )
    print("\n门槛参考：A 集三路融合 ≥ 0.70、B 集三路融合 ≥ 0.85（见 vector-path-plan.md §6）")

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(
            json.dumps({"chunks": total, "reports": reports}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"json -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
