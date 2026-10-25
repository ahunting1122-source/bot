"""表格切块粒度对照：整表 / 行级 / 行组(≈250字) 三档，比 A、B 集管线(rerank) recall@5。

动机：行级切块「去稀释」的同时也「去上下文」——A 集有得有失（净 +1）。
本脚本一次加载模型，对三种表格切块策略重建库并打分，让粒度由数据选出。

用法（在 backend/ 下）：
    DIAG_DB=../data/diag_table_split.db HF_HUB_OFFLINE=1 uv run python -u ../scripts/diag_table_split.py
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

import app.indexing as idx  # noqa: E402
from app.rerank import FastEmbedReranker  # noqa: E402


def _flat(text: str) -> str:
    return "".join((text or "").split())


def split_whole(block):
    """原策略：整表 ≤500 一块；大表按 400 字行组切。"""
    if len(block.text) <= idx.TABLE_MAX_CHARS:
        return [block.text]
    rows = block.rows
    if len(rows) <= 2:
        return idx.hard_split(block.text, idx.TABLE_MAX_CHARS)
    header = rows[:2]
    header_text = "\n".join(header)
    pieces: list[str] = []
    group: list[str] = []
    size = len(header_text)
    for row in rows[2:]:
        if size + len(row) > idx.TARGET_CHARS and group:
            pieces.append("\n".join([header_text, *group]))
            group = []
            size = len(header_text)
        group.append(row)
        size += len(row)
    if group:
        pieces.append("\n".join([header_text, *group]))
    return pieces


def split_row(block):
    """现策略：每个数据行 + 表头独立成块。"""
    rows = block.rows
    if len(rows) <= 2:
        return idx.hard_split(block.text, idx.TABLE_MAX_CHARS)
    header_text = "\n".join(rows[:2])
    pieces: list[str] = []
    for row in rows[2:]:
        if len(header_text) + len(row) + 1 > idx.TABLE_MAX_CHARS:
            sub = idx.hard_split(row, idx.TABLE_MAX_CHARS)
            pieces.append(f"{header_text}\n{sub[0]}")
            pieces.extend(sub[1:])
        else:
            pieces.append(f"{header_text}\n{row}")
    return pieces


def make_group(target: int):
    def split_group(block):
        rows = block.rows
        if len(rows) <= 2:
            return idx.hard_split(block.text, idx.TABLE_MAX_CHARS)
        header_text = "\n".join(rows[:2])
        pieces: list[str] = []
        group: list[str] = []
        size = len(header_text)
        for row in rows[2:]:
            if group and size + len(row) + 1 > target:
                pieces.append("\n".join([header_text, *group]))
                group = []
                size = len(header_text)
            group.append(row)
            size += len(row) + 1
        if group:
            pieces.append("\n".join([header_text, *group]))
        return pieces

    return split_group


def main() -> int:
    tmp = pathlib.Path(os.environ.get("DIAG_DB", "diag_table_split.db"))
    embeddings = E.build_embeddings()
    st = E.build_settings()
    rk = FastEmbedReranker(cache_dir=E.resolve_cache_dir(st), cpu_mem_arena=False)

    strategies = [
        ("整表(原)", split_whole),
        ("行级(现)", split_row),
        ("行组250", make_group(250)),
        ("行组400", make_group(400)),
    ]

    print(f"{'策略':<12} {'块数':>5} | {'A@5':>6} {'A@20':>6} | {'B@5':>6}", flush=True)
    print("-" * 46, flush=True)
    for name, fn in strategies:
        idx.split_table = fn  # chunk_text 内部按模块全局解析，替换即生效
        if tmp.exists():
            tmp.unlink()
        conn, n = E.build_db(tmp, E.repo_corpus())
        E.build_vectors(conn, embeddings)

        def r(cases, k, conn=conn):
            # `conn` 显式绑成默认参数：循环体里每轮都换新连接，闭包若靠外层作用域
            # 取变量，语义依赖"立即调用"这个巧合（ruff B023 就是抓这个）。
            return E.score(
                cases,
                lambda c, q: E.fused_routed(c, q, embeddings, rk, settings=st, do_rerank=True),
                conn,
                k=k,
            )[0]

        a5, a20, b5 = r(E.A_CASES, 5), r(E.A_CASES, 20), r(E.REPO_CASES, 5)
        print(f"{name:<12} {n:>5} | {a5:>6.3f} {a20:>6.3f} | {b5:>6.3f}", flush=True)
        conn.close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
