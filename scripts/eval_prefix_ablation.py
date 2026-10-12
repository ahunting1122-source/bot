"""查询指令前缀消融：bge 的 instruction 前缀在 v1.5 上到底是加分还是扣分。

背景：bge **v1.0** 的检索用法要求在**查询侧**加 instruction 前缀（文档侧不加）。
设计稿据此把前缀写进了默认配置，但那是个未经验证的假设，而且 v1.5 起官方已明确
不再需要。本脚本在真实仓库语料上做对照，用**向量路单独的 recall@5 / MRR@5** 判定。

结论（2026-10-08，188 块真实语料）：**加上前缀反而掉分**，所以默认前缀改为空。

    A 集（改写查询）  recall@5 0.312 → 0.438   MRR 0.219 → 0.231
    B 集（词面查询）  recall@5 0.417 → 0.583   MRR 0.417 → 0.368

换嵌入模型时应当重跑本脚本，别把某个模型的经验带过去。

用法（在 backend/ 下）：

    uv run python ../scripts/eval_prefix_ablation.py
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(HERE))

import eval_rag  # noqa: E402

from app.vectors import QUERY_PREFIX, FastEmbedEmbeddings  # noqa: E402


def evaluate(embeddings, conn, cases, label: str) -> float:
    recall, mrr = eval_rag.score(
        cases,
        lambda c, q: [
            row["text"] for row in eval_rag.vector_rows(c, q, embeddings, eval_rag.LIST_DEPTH)
        ],
        conn,
    )
    print(f"    {label:<24} recall@5={recall:.3f}  mrr@5={mrr:.3f}", flush=True)
    return recall


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        conn, total = eval_rag.build_db(pathlib.Path(tmp) / "repo.sqlite", eval_rag.repo_corpus())
        print(f"repo chunks: {total}", flush=True)
        with_prefix = FastEmbedEmbeddings(query_prefix=QUERY_PREFIX)
        without_prefix = FastEmbedEmbeddings(query_prefix="")
        print("  indexing vectors...", flush=True)
        eval_rag.build_vectors(conn, with_prefix)  # 两者模型相同，向量只灌一次
        for name, cases in (("A_no_overlap", eval_rag.A_CASES), ("B_mixed", eval_rag.REPO_CASES)):
            print(f"  [{name}]", flush=True)
            with_value = evaluate(with_prefix, conn, cases, "带指令前缀")
            without_value = evaluate(without_prefix, conn, cases, "不加指令前缀")
            better = "去掉前缀更好" if without_value > with_value else "加前缀更好"
            print(f"    → {better}（{with_value:.3f} vs {without_value:.3f}）", flush=True)
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
