"""A 集残差归因：区分 (a) 目标块不在候选池 还是 (b) 在池但排不进 top5。

临时诊断脚本，不改生产代码。用法（在 backend/ 下）：
    HF_HUB_OFFLINE=1 uv run python ../scripts/diag_a_residual.py
"""
from __future__ import annotations

import os
import pathlib
import sys

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(HERE))

import eval_rag as E  # noqa: E402

from app.rerank import FastEmbedReranker  # noqa: E402


def _flat(text: str) -> str:
    return "".join((text or "").split())


def main() -> int:
    tmp = pathlib.Path(os.environ.get("DIAG_DB", "diag_a_residual.db"))
    if tmp.exists():
        tmp.unlink()
    corpus = E.repo_corpus()
    conn, n = E.build_db(tmp, corpus)
    embeddings = E.build_embeddings()
    written = E.build_vectors(conn, embeddings)
    print(f"chunks={n} vectors={written}")

    st = E.build_settings()
    rk = FastEmbedReranker(cache_dir=E.resolve_cache_dir(st), cpu_mem_arena=False)

    ok = in_pool = miss_pool = 0
    for q, marks in E.A_CASES:
        hits = E.fused_routed(
            conn, q, embeddings, rk, settings=st, do_rerank=True, limit=20
        )
        flat_marks = [_flat(m) for m in marks]

        def has_mark(h: str, _fm: list[str] = flat_marks) -> bool:
            flat = _flat(h)
            return any(m in flat for m in _fm)

        rank = next((i + 1 for i, h in enumerate(hits) if has_mark(h)), None)
        if rank is None:
            flag = "缺池"
            miss_pool += 1
        elif rank <= 5:
            flag = "OK "
            ok += 1
        else:
            flag = "池内"
            in_pool += 1
        print(f"{flag} rank={str(rank):>3}  {q}   <- {marks}")

    total = len(E.A_CASES)
    print(f"\nOK={ok}  池内未进top5={in_pool}  缺池={miss_pool}  合计={total}")
    print(f"recall@5={ok / total:.3f}  池天花板@20={(ok + in_pool) / total:.3f}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
