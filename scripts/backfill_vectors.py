"""把已有 chunks 补上向量（阶段 6）。可重入，换模型用 --rebuild。

只回填、不改 chunks；已存在向量的 row_id 默认跳过，所以中断后直接重跑即可。
不做自动回填——避免"升级即触发 92MB 模型下载"的惊吓。

用法（在 backend/ 下）：

    uv run python ../scripts/backfill_vectors.py
    uv run python ../scripts/backfill_vectors.py --rebuild
    uv run python ../scripts/backfill_vectors.py --batch 16 --limit 100
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sqlite3
import sys

BACKEND = pathlib.Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.config import Settings  # noqa: E402
from app.vectors import (  # noqa: E402
    EMBED_DIM,
    VEC_TABLE,
    FastEmbedEmbeddings,
    SQLiteVecStore,
    load_extension_sync,
)


def read_chunks(db_path: pathlib.Path, done: set[int], limit: int | None) -> list[tuple[str, int, str]]:
    """待补向量的 (chunks.id, row_id, text)。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT id, row_id, text FROM chunks ORDER BY row_id").fetchall()
    finally:
        conn.close()
    pending = [(row[0], row[1], row[2]) for row in rows if row[1] not in done]
    return pending[:limit] if limit else pending


def existing_row_ids(db_path: pathlib.Path) -> set[int]:
    """已经写过向量的 row_id（读 vec0 需要扩展，所以走同步加载体）。"""
    conn = sqlite3.connect(db_path)
    try:
        load_extension_sync(conn)
        return {row[0] for row in conn.execute(f"SELECT row_id FROM {VEC_TABLE}")}
    except sqlite3.OperationalError:
        return set()  # 表还没建
    finally:
        conn.close()


def clear_vectors(db_path: pathlib.Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        load_extension_sync(conn)
        before = conn.execute(f"SELECT count(*) FROM {VEC_TABLE}").fetchone()[0]
        conn.execute(f"DELETE FROM {VEC_TABLE}")
        conn.commit()
        return int(before)
    finally:
        conn.close()


async def run(args) -> int:
    settings = Settings()
    db_path = settings.data_dir / "app.sqlite"
    if not db_path.exists():
        print(f"FAIL: 找不到数据库 {db_path}")
        return 2

    model = args.model or settings.embedding_model
    dim = int(args.dim or settings.embedding_dim or EMBED_DIM)
    store = SQLiteVecStore(db_path, dim=dim, model=model)
    if not await store.ensure_schema():
        print("FAIL: 向量表不可用（sqlite-vec 扩展加载失败）")
        return 2

    if args.rebuild:
        removed = clear_vectors(db_path)
        print(f"rebuild: 清掉 {removed} 条旧向量")

    done = set() if args.rebuild else existing_row_ids(db_path)
    pending = read_chunks(db_path, done, args.limit)
    print(f"模型 {model} · 维度 {dim} · 已有向量 {len(done)} 条 · 待补 {len(pending)} 条")
    if not pending:
        print("PASS: 没有待补的 chunk")
        return 0

    from app.vectors import resolve_cache_dir

    embeddings = FastEmbedEmbeddings(
        model_name=model,
        dim=dim,
        query_prefix=settings.vector_query_prefix,
        doc_prefix=settings.vector_doc_prefix,
        cache_dir=resolve_cache_dir(settings),
        cpu_mem_arena=settings.embedding_cpu_mem_arena,
        threads=settings.embedding_threads,
    )
    written = 0
    for start in range(0, len(pending), args.batch):
        batch = pending[start : start + args.batch]
        vectors = await asyncio.to_thread(embeddings.embed, [item[2] for item in batch])
        if len(vectors) != len(batch):
            print(f"FAIL: 第 {start} 批嵌入数 {len(vectors)} != chunk 数 {len(batch)}")
            return 1
        written += await store.write(
            [(item[0], vector) for item, vector in zip(batch, vectors, strict=True)]
        )
        print(f"  {min(start + len(batch), len(pending))}/{len(pending)} 已写 {written} 条", flush=True)

    print(f"PASS: 写入 {written} 条向量")
    if not settings.vector_enabled:
        print("提醒：settings.vector_enabled 仍为 False，检索侧不会用到这些向量。"
              "要启用请设 ORBIT_VECTOR_ENABLED=true 或改 .env。")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rebuild", action="store_true", help="先清空向量表再全量重算（换模型时用）")
    parser.add_argument("--batch", type=int, default=8, help="每批嵌入条数")
    parser.add_argument("--limit", type=int, default=None, help="最多补多少条（调试用）")
    parser.add_argument("--model", default="", help="覆盖嵌入模型")
    parser.add_argument("--dim", type=int, default=0, help="覆盖向量维度")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
