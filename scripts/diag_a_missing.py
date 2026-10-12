"""A 集「缺池」归因：目标标记在语料里到底存不存在？在哪一路（trigram/chars/vector）排名多少？

用法（在 backend/ 下）：
    HF_HUB_OFFLINE=1 uv run python ../scripts/diag_a_missing.py
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


def _flat(text: str) -> str:
    return "".join((text or "").split())


#: 上一轮诊断里 rank=None（不在 20 条池内）的三条
MISSING: list[tuple[str, list[str]]] = [
    ("答案里没写出处能算做完吗", ["无来源不算完成"]),
    ("刹车的三个维度分别是什么", ["动作 / token / 时间"]),
    ("结果不明朗时可以再发一次请求吗", ["禁止重发"]),
]


def main() -> int:
    tmp = pathlib.Path(os.environ.get("DIAG_DB", "diag_a_missing.db"))
    if tmp.exists():
        tmp.unlink()
    corpus = E.repo_corpus()
    conn, n = E.build_db(tmp, corpus)
    embeddings = E.build_embeddings()
    E.build_vectors(conn, embeddings)

    # 1) 标记是否存在于语料切块？打印所在块的 row_id。
    flat_marks = {_flat(m): m for _, ms in MISSING for m in ms}
    rows = conn.execute("SELECT row_id, text FROM chunks ORDER BY row_id").fetchall()
    print("== 标记落块检查 ==")
    for flat, raw in flat_marks.items():
        where = [rid for rid, text in rows if flat in _flat(text)]
        print(f"  {raw!r}  -> row_ids={where or '**语料里不存在**'}")

    # 2) 三路各自给这些块的排名
    print("\n== 三路排名（None = 该路 limit 内没召回）==")
    for q, marks in MISSING:
        target = next(
            (rid for rid, text in rows if any(_flat(m) in _flat(text) for m in marks)),
            None,
        )
        tri = [r["row_id"] for r in E.fts_rows(conn, q, "chunks_fts_trigram", 100)]
        cha = [r["row_id"] for r in E.fts_rows(conn, q, "chunks_fts_chars", 100)]
        vec = [r["row_id"] for r in E.vector_rows(conn, q, embeddings, 100)]

        def rk(seq: list[int], _t: int | None = target) -> int | None:
            return seq.index(_t) + 1 if _t is not None and _t in seq else None

        print(f"  {q}")
        print(f"     target_row={target}  trigram={rk(tri)}  chars={rk(cha)}  vector={rk(vec)}")

    # 3) 目标块原文片段，确认是不是被切碎了
    print("\n== 目标块文本片段 ==")
    for _q, marks in MISSING:
        for rid, text in rows:
            if any(_flat(m) in _flat(text) for m in marks):
                print(f"  row {rid}: {text[:150]!r}")
                break
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
