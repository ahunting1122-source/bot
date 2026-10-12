"""向量表：chunk_vectors（vec0 虚表）+ vector_meta。

阶段 6。vec0 是 sqlite-vec 的虚表模块，SQLAlchemy 无法建模，只能手写 SQL；
且**创建虚表前必须在该连接上加载扩展**。

迁移用的是同步 `create_engine`（见 `migrations/env.py`），所以这里能直接对
stdlib `sqlite3.Connection` 调 `enable_load_extension`。**不能改走 SQLAlchemy 的
connect 事件**——那个回调在运行时是给 aiosqlite 用的，且在事件循环线程上执行会撞
sqlite3 的同线程守卫（详见 `app/vectors.py` 模块 docstring）。

sqlite-vec 已是主依赖，所以这里**不做静默降级**：加载失败就带可读信息硬失败，
否则会留下一个 schema 与代码不匹配的库，比升级失败更难排查。

**关于向量维度（`VEC_DDL` 里的 `float[N]`）**：这里建的是**当时的默认模型**的维度。
vec0 的维度建表时钉死，而 `embedding_dim` 是配置项（换模型即换维度），所以"表维度"
本质上跟随配置而不是跟随迁移——因此**校准放在运行时**：`SQLiteVecStore.ensure_schema()`
在启动（`AgentRunner._calibrate_vectors`）与回填脚本里比对 `vector_meta` 与当前配置，
不一致就 DROP 重建并留可读 warning。换模型后必须重跑 `scripts/backfill_vectors.py`。
`0008` 因此不被本方向占用（留给审计时间线）。
"""

from alembic import op

from app.vectors import META_DDL, META_TABLE, VEC_DDL, VEC_TABLE, load_extension_sync

revision = "0005"
down_revision = "0004"


def _raw_sqlite_connection():
    """取本迁移所在连接的原始 sqlite3 Connection（要能 enable_load_extension）。"""
    bind = op.get_bind()
    for attribute in ("driver_connection", "dbapi_connection", "connection"):
        candidate = getattr(bind, attribute, None)
        if candidate is not None and hasattr(candidate, "enable_load_extension"):
            return candidate
    raise RuntimeError("迁移 0005 拿不到 sqlite3 连接，无法加载 sqlite-vec 扩展")


def upgrade():
    raw = _raw_sqlite_connection()
    try:
        load_extension_sync(raw)
        raw.execute("SELECT vec_version()").fetchone()
    except Exception as exc:  # 缺包 / Python 未编译扩展支持 / 加载被拒
        raise RuntimeError(
            "迁移 0005 需要 sqlite-vec 扩展（已是主依赖），但加载失败："
            f"{exc}。请确认 `uv sync` 已安装 sqlite-vec，"
            "且所用 Python 构建支持 sqlite3 的 enable_load_extension。"
        ) from exc
    op.execute(VEC_DDL)
    op.execute(META_DDL)


def downgrade():
    # DROP 一张虚表同样需要模块在位，否则报 `no such module: vec0`（实测）。
    try:
        load_extension_sync(_raw_sqlite_connection())
    except Exception as exc:
        raise RuntimeError(f"迁移 0005 降级需要 sqlite-vec 扩展，但加载失败：{exc}") from exc
    op.execute(f"DROP TABLE IF EXISTS {VEC_TABLE}")
    op.execute(f"DROP TABLE IF EXISTS {META_TABLE}")
