"""向量路（阶段 6）：sqlite-vec vec0 虚表 + 本地嵌入。

**扩展加载的关键约束（别改回去）**

aiosqlite 把真实的 `sqlite3.Connection` 放在**独立 worker 线程**，所有语句都编组到
那个线程执行。因此：

- **不能用 SQLAlchemy 的 `event.listens_for(engine.sync_engine, "connect")` 加载扩展**。
  那个回调跑在事件循环线程上，碰连接会撞 sqlite3 的同线程守卫（`sqlite3.ProgrammingError`）。
- 正解：本模块**自持 aiosqlite 短连接**，走 aiosqlite 0.18+ 的公开异步方法
  `enable_load_extension()` / `load_extension()`——它们内部就在该连接的 worker 线程上跑，
  不阻塞事件循环。`aiosqlite>=0.20` 已满足要求。

**vec0 的两个实测行为（Windows / sqlite-vec 0.1.9 实测）**

1. **不支持 `INSERT OR REPLACE`**（报 `UNIQUE constraint failed on <table> primary key`），
   也不支持裸 INSERT 覆盖已有 rowid。幂等写入必须 `DELETE FROM ... WHERE row_id = ?`
   再 `INSERT`。
2. `WHERE embedding MATCH ? AND k = ?` 里的 `k` **可以用绑定参数**（不必内联字面量）；
   `k` 大于总行数不报错，只是返回全部。

sqlite-vec 是 pre-v1（官方明说会有破坏性变更），版本在 `pyproject.toml` 里钉死。
"""

from __future__ import annotations

import logging
import math
import sqlite3
from pathlib import Path
from typing import Any

import aiosqlite

from app.storage import now

log = logging.getLogger("orbit.vectors")

#: 兜底默认值（正常路径由 `Settings.embedding_model` / `embedding_dim` 驱动）。
#: 换模型时**必须同步改这两个常量与 `Settings` 的默认值**：vec0 的维度建表时钉死在
#: `float[N]` 里，不一致会被 `ensure_schema()` 判定为过期并清空重建。
EMBED_DIM = 768
DEFAULT_EMBEDDING_MODEL = "google/embeddinggemma-300m"
#: bge **v1.0** 检索任务的查询指令前缀。**v1.5 起官方明确不再需要**，
#: 实测加上反而掉分（见 `Settings.vector_query_prefix` 注释），所以默认不启用。
#: 只加在查询侧、文档侧不加——这个机制本身仍由 `query_prefix` 参数保留。
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："

VEC_TABLE = "chunk_vectors"
META_TABLE = "vector_meta"

#: 后过滤缓冲：先取这么多近邻，再按会话/站点/来源过滤（vec0 的过滤列语法跨版本不稳）
MIN_K = 50
K_FACTOR = 8

def vec_ddl(dim: int = EMBED_DIM) -> str:
    return (
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {VEC_TABLE} USING vec0("
        f"row_id INTEGER PRIMARY KEY, embedding float[{int(dim)}])"
    )


VEC_DDL = vec_ddl()
META_DDL = (
    f"CREATE TABLE IF NOT EXISTS {META_TABLE} ("
    "model TEXT PRIMARY KEY, dim INTEGER NOT NULL, created_at TEXT NOT NULL)"
)

_available: bool | None = None


def load_extension_sync(conn: sqlite3.Connection) -> None:
    """同步加载 vec0 扩展。迁移（同步连接）与可用性探针用这个。"""
    import sqlite_vec

    conn.enable_load_extension(True)
    try:
        sqlite_vec.load(conn)
    finally:
        conn.enable_load_extension(False)


async def _load_extension(conn: aiosqlite.Connection) -> None:
    """在 aiosqlite 连接的 worker 线程上加载扩展（见模块 docstring）。"""
    import sqlite_vec

    await conn.enable_load_extension(True)
    try:
        await conn.load_extension(sqlite_vec.loadable_path())
    finally:
        await conn.enable_load_extension(False)


def vec_available() -> bool:
    """sqlite-vec 是否可用。进程内只探一次；不可用则向量路整体降级。"""
    global _available
    if _available is None:
        conn = sqlite3.connect(":memory:")
        try:
            load_extension_sync(conn)
            conn.execute("SELECT vec_version()").fetchone()
            _available = True
        except Exception as exc:  # 缺包 / 未编译扩展支持 / 加载被拒
            log.warning("sqlite-vec 不可用，向量路停用：%s", exc)
            _available = False
        finally:
            conn.close()
    return bool(_available)


def reset_availability_cache() -> None:
    """测试用：清掉进程内探测缓存。"""
    global _available
    _available = None


def l2_normalize(vector: list[float]) -> list[float]:
    """归一化后 L2 距离与余弦单调等价，检索端不必再换距离函数。"""
    norm = math.sqrt(sum(value * value for value in vector))
    if not norm:
        return list(vector)
    return [value / norm for value in vector]


def pack_vector(vector: list[float]) -> bytes:
    """打包成 sqlite-vec 认的紧凑 float32 BLOB。"""
    import numpy as np

    return np.asarray(vector, dtype=np.float32).tobytes()


class NullEmbeddings:
    """默认空转：向量路未开启时不做任何事，行为与接入前逐字一致。"""

    dim = EMBED_DIM

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        return []


class FastEmbedEmbeddings:
    """fastembed + 本地 ONNX 模型：不依赖 torch，内容不出本机。

    首次运行会按 `embedding_model` 下载模型（0.09–2.4GB，不进系统临时目录）；模型是
    **同步 CPU 调用**，调用方必须放 `asyncio.to_thread` 里，绝不直接在事件循环里调。

    三个必须按模型文档配的参数（都在 `Settings` 里，默认对 bge v1.5 成立）：

    - `query_prefix` / `doc_prefix`：指令前缀。**只加在对应一侧**，两侧不对称是常态
      （Qwen3 只在查询侧、gemma 两侧都要、bge v1.5 两侧都不要）。
    - `cpu_mem_arena=False`：关掉 onnxruntime 的 CPU arena。本机实测一个 1.24GB 的模型
      开着 arena 会把进程工作集顶到 5.3GB，直接把这台 16GB 的机器压死。
    """

    def __init__(
        self,
        model_name: str = DEFAULT_EMBEDDING_MODEL,
        dim: int = EMBED_DIM,
        query_prefix: str = "",
        doc_prefix: str = "",
        cache_dir: str | None = None,
        cpu_mem_arena: bool = False,
        threads: int = 0,
    ):
        self.model_name = model_name
        self.dim = dim
        self.query_prefix = query_prefix
        self.doc_prefix = doc_prefix
        self.cache_dir = cache_dir
        self.cpu_mem_arena = cpu_mem_arena
        self.threads = threads
        self._model: Any = None

    def _ensure_model(self):
        if self._model is None:
            from fastembed import TextEmbedding

            kwargs: dict[str, Any] = {
                "model_name": self.model_name,
                "enable_cpu_mem_arena": self.cpu_mem_arena,
            }
            if self.cache_dir:
                kwargs["cache_dir"] = self.cache_dir
            if self.threads:
                kwargs["threads"] = self.threads
            try:
                self._model = TextEmbedding(**kwargs)
            except Exception as exc:  # 首次要联网下载，代理/墙会拦
                raise RuntimeError(
                    f"加载嵌入模型 {self.model_name} 失败：{exc}。"
                    "首次运行需联网下载模型（缓存到 embedding_cache_dir，之后离线）；"
                    "若被网络或代理拦截，可设 HF_ENDPOINT=https://hf-mirror.com 后重试"
                    "（注意：镜像对 Xet 托管的仓库会返回 401，此时改用 HF_HUB_DISABLE_XET=1）。"
                ) from exc
        return self._model

    def embed(self, texts: list[str], *, query: bool = False) -> list[list[float]]:
        bodies = [text for text in (texts or []) if text and text.strip()]
        if not bodies:
            return []
        prefix = self.query_prefix if query else self.doc_prefix
        prepared = [f"{prefix}{text}" for text in bodies] if prefix else bodies
        model = self._ensure_model()
        return [l2_normalize([float(value) for value in row]) for row in model.embed(prepared)]


class NullVectorStore:
    """默认空转：写入与检索都如实返回空，不假装成功。"""

    async def write(self, rows: list[tuple[str, list[float]]]) -> int:
        return 0

    async def search(
        self,
        vector: list[float],
        *,
        limit: int,
        conversation_id: str = "",
        host: str = "",
        source_type: str = "",
    ) -> list[dict]:
        return []


class SQLiteVecStore:
    """vec0 实现：自持 aiosqlite 短连接（原因见模块 docstring）。

    连接与 SQLAlchemy 引擎**不是同一条**，靠 WAL + busy_timeout 并存；幂等由
    `DELETE` + `INSERT` 保证（vec0 不支持 `INSERT OR REPLACE`）。
    """

    def __init__(
        self,
        db_path,
        dim: int = EMBED_DIM,
        model: str = DEFAULT_EMBEDDING_MODEL,
    ):
        self.db_path = str(db_path)
        self.dim = dim
        self.model = model

    async def _connect(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout=20000")
        await _load_extension(conn)
        return conn

    async def ensure_schema(self) -> bool:
        """建 vec0 虚表与元信息表；**与配置不一致时重建并清空向量**。

        两个必须在这里拦住的不一致（否则表现是"检索恒返空"，极难排查）：

        1. **维度不一致**：vec0 的维度建表时钉死在 `float[N]` 里，而 `embedding_dim`
           是可配置的（换模型即换维度）。维度不符时写入会失败、检索恒 0 召回。
        2. **模型不一致**：同维度的两个模型混用会得到"两套不可比的向量"，
           检索能跑但结果是错的——比维度错误更危险。

        `vector_meta` 因此按**单行**语义使用（记录当前表里那批向量来自哪个模型，
        `model` 虽是主键但每次先清空）。命中不一致就 DROP 表 + 清 meta + 记可读 warning，
        回填由 `scripts/backfill_vectors.py` 负责——不做静默混用。
        """
        if not vec_available():
            return False
        try:
            conn = await self._connect()
            try:
                await conn.execute(META_DDL)
                cursor = await conn.execute(f"SELECT model, dim FROM {META_TABLE} LIMIT 1")
                row = await cursor.fetchone()
                active: tuple[str, int] | None = (str(row[0]), int(row[1])) if row else None

                cursor = await conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (VEC_TABLE,)
                )
                ddl_row = await cursor.fetchone()
                ddl = str(ddl_row[0]) if ddl_row and ddl_row[0] else ""

                stale = ""
                if ddl and f"float[{self.dim}]" not in ddl:
                    stale = f"表维度与配置不符（配置 {self.dim}）"
                elif active and active[0] != self.model:
                    stale = f"模型 {active[0]} → {self.model}"
                elif active and active[1] != self.dim:
                    stale = f"维度 {active[1]} → {self.dim}"

                if stale:
                    log.warning(
                        "向量表与当前配置不一致（%s）：清空向量并重建；"
                        "之后必须重跑 scripts/backfill_vectors.py 回填，否则向量路恒空。",
                        stale,
                    )
                    await conn.execute(f"DROP TABLE IF EXISTS {VEC_TABLE}")
                    await conn.execute(f"DELETE FROM {META_TABLE}")

                await conn.execute(vec_ddl(self.dim))
                await conn.execute(f"DELETE FROM {META_TABLE}")
                await conn.execute(
                    f"INSERT INTO {META_TABLE}(model, dim, created_at) VALUES (?,?,?)",
                    (self.model, self.dim, now()),
                )
                await conn.commit()
            finally:
                await conn.close()
        except Exception as exc:
            log.warning("向量表不可用：%s", exc)
            return False
        return True

    async def write(self, rows: list[tuple[str, list[float]]]) -> int:
        """rows = [(chunks.id, vector)]。

        `chunks` 的写入在别处（`FtsIndexer.write` 只返回计数、不返回 row_id），
        所以这里按 `chunks.id` 反查 row_id——不改 `Indexer` 协议的返回类型。
        """
        if not rows or len(rows[0][1]) != self.dim:
            return 0
        try:
            conn = await self._connect()
        except Exception as exc:
            log.warning("向量写入跳过（连接失败）：%s", exc)
            return 0
        try:
            identifiers = [row[0] for row in rows]
            marks = ",".join("?" for _ in identifiers)
            cursor = await conn.execute(
                f"SELECT id, row_id FROM chunks WHERE id IN ({marks})", identifiers
            )
            mapping = {row["id"]: row["row_id"] for row in await cursor.fetchall()}
            written = 0
            for identifier, vector in rows:
                row_id = mapping.get(identifier)
                if row_id is None:
                    continue
                # vec0 不支持 INSERT OR REPLACE / upsert，幂等只能删了再插。
                await conn.execute(f"DELETE FROM {VEC_TABLE} WHERE row_id = ?", (row_id,))
                await conn.execute(
                    f"INSERT INTO {VEC_TABLE}(row_id, embedding) VALUES (?,?)",
                    (row_id, pack_vector(vector)),
                )
                written += 1
            await conn.commit()
            return written
        except Exception as exc:
            log.warning("向量写入失败（只记日志）：%s", exc)
            return 0
        finally:
            await conn.close()

    async def search(
        self,
        vector: list[float],
        *,
        limit: int,
        conversation_id: str = "",
        host: str = "",
        source_type: str = "",
    ) -> list[dict]:
        if not vector or len(vector) != self.dim:
            return []
        # 先取够多的近邻，再按范围过滤：vec0 的过滤列语法跨版本不稳，JOIN 后过滤更稳。
        k = max(MIN_K, limit * K_FACTOR)
        filters = ["v.embedding MATCH ? AND k = ?"]
        params: list[Any] = [pack_vector(vector), k]
        if conversation_id:
            filters.append("c.conversation_id = ?")
            params.append(conversation_id)
        if host:
            filters.append("c.host = ?")
            params.append(host)
        if source_type:
            filters.append("c.source_type = ?")
            params.append(source_type)
        params.append(limit)
        statement = (
            "SELECT c.row_id AS row_id, c.text AS text, c.origin AS origin,"
            " c.source_type AS source_type, c.host AS host, c.task_id AS task_id,"
            " v.distance AS distance"
            f" FROM {VEC_TABLE} v JOIN chunks c ON c.row_id = v.row_id"
            f" WHERE {' AND '.join(filters)} ORDER BY v.distance LIMIT ?"
        )
        try:
            conn = await self._connect()
        except Exception as exc:
            log.warning("向量检索跳过（连接失败）：%s", exc)
            return []
        try:
            cursor = await conn.execute(statement, params)
            return [dict(row) for row in await cursor.fetchall()]
        except Exception as exc:
            # 表还没建 / 扩展掉了：退化为空结果，不假装检索成功。
            log.warning("向量检索失败（退化为空）：%s", exc)
            return []
        finally:
            await conn.close()


def resolve_cache_dir(settings) -> str:
    """模型缓存目录：`embedding_cache_dir` 优先，否则 `<data_dir>/fastembed_cache`。

    同时把 `FASTEMBED_CACHE_PATH` 导出到环境——fastembed 内部还有其它入口读这个变量，
    不设的话会回落到 `%TEMP%/fastembed_cache`（模型 0.09–2.4GB，不该放系统临时目录）。
    """
    import os

    configured = str(getattr(settings, "embedding_cache_dir", "") or "")
    if configured:
        path = Path(configured)
    else:
        data_dir = getattr(settings, "data_dir", None)
        path = (Path(data_dir) if data_dir else Path.cwd()) / "fastembed_cache"
    path.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("FASTEMBED_CACHE_PATH", str(path))
    return str(path)


def build_embeddings(settings) -> Any:
    if not getattr(settings, "vector_enabled", False):
        return NullEmbeddings()
    return FastEmbedEmbeddings(
        model_name=str(getattr(settings, "embedding_model", DEFAULT_EMBEDDING_MODEL)),
        dim=int(getattr(settings, "embedding_dim", EMBED_DIM) or EMBED_DIM),
        query_prefix=str(getattr(settings, "vector_query_prefix", "") or ""),
        doc_prefix=str(getattr(settings, "vector_doc_prefix", "") or ""),
        cache_dir=resolve_cache_dir(settings),
        cpu_mem_arena=bool(getattr(settings, "embedding_cpu_mem_arena", False)),
        threads=int(getattr(settings, "embedding_threads", 0) or 0),
    )


def build_vector_store(settings, store) -> Any:
    if not getattr(settings, "vector_enabled", False):
        return NullVectorStore()
    return SQLiteVecStore(
        db_path=settings.data_dir / "app.sqlite",
        dim=int(getattr(settings, "embedding_dim", EMBED_DIM) or EMBED_DIM),
        model=str(getattr(settings, "embedding_model", DEFAULT_EMBEDDING_MODEL)),
    )
