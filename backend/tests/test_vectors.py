"""向量路（阶段 6）：RRF 接线（门 1）、vec0 读写幂等、查询前缀、降级与迁移。

嵌入模型不参与单测（要下载 92MB），一律用确定性假实现；只有显式标 `requires_vec`
的用例才真的碰 sqlite-vec 扩展。
"""

import os
import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from app.config import ROOT, Settings
from app.indexing import FtsIndexer, chunks_for_tool_result
from app.main import migrate
from app.retrieval import RRF_WEIGHTS, FtsRetriever, build_retriever, rrf
from app.storage import Chunk, Conversation, Store, Task
from app.vectors import (
    DEFAULT_EMBEDDING_MODEL,
    EMBED_DIM,
    QUERY_PREFIX,
    FastEmbedEmbeddings,
    NullEmbeddings,
    NullVectorStore,
    SQLiteVecStore,
    l2_normalize,
    pack_vector,
    resolve_cache_dir,
    vec_available,
)

requires_vec = pytest.mark.skipif(not vec_available(), reason="sqlite-vec 扩展不可用")


def make_settings(tmp_path, **overrides):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files", **overrides)
    settings.prepare()
    migrate(settings)
    return settings


def alembic_config(settings):
    config = Config(str(ROOT / "backend" / "alembic.ini"))
    config.attributes["database_url"] = f"sqlite:///{settings.data_dir / 'app.sqlite'}"
    return config


async def seed(store, indexer, conversation_id, task_id, origin, body):
    task = Task(id=task_id, conversation_id=conversation_id, goal="搜键盘")
    chunks = chunks_for_tool_result(
        task,
        "browser_observe",
        {"status": "ok", "data": {"url": origin, "title": "搜索", "structured": body}},
    )
    assert await indexer.write(chunks) >= 1
    return chunks


async def first_row_id(store) -> int:
    return (await store.rows(select(Chunk.row_id)))[0]


class FakeEmbeddings:
    """确定性假嵌入：不下载模型，向量恒定。"""

    dim = EMBED_DIM

    def __init__(self):
        self.calls = []

    def embed(self, texts, *, query=False):
        self.calls.append({"texts": list(texts), "query": query})
        return [[1.0] * EMBED_DIM for _ in texts]


class ExplodingEmbeddings:
    """模拟模型缺失/下载失败：向量路必须降级，不能拖垮检索。"""

    dim = EMBED_DIM

    def embed(self, texts, *, query=False):
        raise RuntimeError("模型不可用")


class FakeVectorStore:
    """按预设 row_id 出候选，用来验证 RRF 接线（门 1）。"""

    def __init__(self, row_ids):
        self.row_ids = list(row_ids)
        self.calls = []

    async def write(self, rows):
        return 0

    async def search(self, vector, *, limit, conversation_id="", host="", source_type=""):
        self.calls.append({"limit": limit, "conversation_id": conversation_id})
        return [
            {
                "row_id": row_id,
                "text": "向量候选",
                "origin": "https://a.example.com/1",
                "source_type": "web_page",
                "host": "a.example.com",
                "task_id": "t1",
                "distance": 0.1 * (index + 1),
            }
            for index, row_id in enumerate(self.row_ids[:limit])
        ]


# ── 门 1：结构性断言（向量候选必须真的进融合）────────────────────────────


async def test_vector_candidates_participate_in_rrf(tmp_path):
    """门 1。防的是 `rrf()` 的 `if key is None: continue`：候选漏带 row_id 时
    整条向量路会被**静默丢掉**，表现是"接了向量路但召回毫无变化"，几乎没法排查。
    """
    settings = make_settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    indexer = FtsIndexer(store)
    conversation = await store.save(Conversation())
    await seed(store, indexer, conversation.id, "t1", "https://a.example.com/1", "# 机械键盘\n甲")
    row_id = await first_row_id(store)

    embeddings = FakeEmbeddings()
    vectors = FakeVectorStore([row_id])
    retriever = FtsRetriever(store, indexer=indexer, vectors=vectors, embeddings=embeddings)
    hits = await retriever.search("机械键盘", conversation_id=conversation.id, top_k=5)

    assert hits
    assert any("vector" in hit["paths"] for hit in hits), "向量候选没有进入 RRF 融合"
    assert await store.rows(select(Chunk.row_id)) == [row_id]
    await store.engine.dispose()


async def test_vector_candidates_carry_row_id_and_query_flag(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    indexer = FtsIndexer(store)
    conversation = await store.save(Conversation())
    await seed(store, indexer, conversation.id, "t1", "https://a.example.com/1", "# 键盘\n甲")
    row_id = await first_row_id(store)

    embeddings = FakeEmbeddings()
    vectors = FakeVectorStore([row_id])
    retriever = FtsRetriever(store, indexer=indexer, vectors=vectors, embeddings=embeddings)
    await retriever.search("机械键盘", conversation_id=conversation.id, top_k=5)

    # 查询侧必须走 query=True（bge 的中文检索前缀只加在查询侧）
    assert embeddings.calls and embeddings.calls[0]["query"] is True
    # 范围收窄要透传给向量路，否则向量路会把别的会话的内容捞进来
    assert vectors.calls and vectors.calls[0]["conversation_id"] == conversation.id
    await store.engine.dispose()


async def test_vector_failure_degrades_to_keyword_only(tmp_path):
    """嵌入或向量检索失败时，FTS 双路照常工作（只降级、不报错）。"""
    settings = make_settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    indexer = FtsIndexer(store)
    conversation = await store.save(Conversation())
    await seed(store, indexer, conversation.id, "t1", "https://a.example.com/1", "# 机械键盘\n甲")

    retriever = FtsRetriever(
        store, indexer=indexer, vectors=FakeVectorStore([]), embeddings=ExplodingEmbeddings()
    )
    hits = await retriever.search("机械键盘", conversation_id=conversation.id, top_k=5)
    assert hits, "向量路失败不能影响 FTS 召回"
    assert all("vector" not in hit["paths"] for hit in hits)
    await store.engine.dispose()


async def test_disabled_vector_path_is_a_noop(tmp_path):
    """`vector_enabled=False` 时接线到位但行为与接入前逐字一致。

    默认值 2026-10-08 收口为 **True**（向量路的意义就是语义召回，模型已本地就位、
    失败可降级），所以这里显式关掉再断言"关掉即空实现"这条不变量仍然成立。
    """
    settings = make_settings(tmp_path, vector_enabled=False)
    assert settings.vector_enabled is False
    store = Store(settings.data_dir / "app.sqlite")
    retriever = build_retriever(settings, store)
    assert isinstance(retriever.embeddings, NullEmbeddings)
    assert isinstance(retriever.vectors, NullVectorStore)
    await store.engine.dispose()


async def test_vector_path_enabled_by_default(tmp_path):
    """默认开的**类默认值**是 True（2026-10-08 收口）。

    注意：测试套件有 autouse 夹具设 `ORBIT_VECTOR_ENABLED=0`（避免加载 1.24GB 模型），
    环境变量会盖过字段默认值，所以这里断言的是 `Settings` 的**声明默认值**而非实例值；
    再显式传 `True` 验证"开着时确实拿到真实现"。
    """
    from app.config import Settings as _S

    assert _S.model_fields["vector_enabled"].default is True

    settings = make_settings(tmp_path, vector_enabled=True)
    store = Store(settings.data_dir / "app.sqlite")
    retriever = build_retriever(settings, store)
    assert not isinstance(retriever.embeddings, NullEmbeddings)
    assert not isinstance(retriever.vectors, NullVectorStore)
    await store.engine.dispose()


# ── 嵌入层：前缀只加在查询侧 ────────────────────────────────────────────


def test_query_prefix_only_applies_to_queries_and_defaults_off(monkeypatch):
    """机制：query=True 时加 query_prefix、文档侧用 doc_prefix（两侧互不影响）。

    注意这里测的是**机制**。`Settings` 的默认前缀是跟着默认模型走的（gemma 两侧都要），
    换模型就得跟着换——`test_settings_defaults_are_self_consistent` 锁那一条。
    这里锁的是"构造函数的兜底默认是空"，因为兜底值不该悄悄替某个模型做主。
    """
    seen: list[list[str]] = []

    class StubModel:
        def embed(self, texts):
            seen.append(list(texts))
            return [[0.5, 0.5] for _ in texts]

    assert FastEmbedEmbeddings().query_prefix == "", "构造函数兜底默认应为空"

    embeddings = FastEmbedEmbeddings(dim=2, query_prefix=QUERY_PREFIX)
    monkeypatch.setattr(embeddings, "_ensure_model", lambda: StubModel())
    embeddings.embed(["文档正文"])
    embeddings.embed(["检索问题"], query=True)

    assert seen[0] == ["文档正文"], "文档侧不能加查询指令前缀"
    assert seen[1] == [QUERY_PREFIX + "检索问题"], "查询侧必须加检索指令前缀"


def test_settings_defaults_are_self_consistent():
    """默认配置必须自洽：模型、维度、两侧前缀三者对齐。

    换模型最容易漏这三个字段中的一个，而且**漏了不会报错**：维度不符会被
    `ensure_schema` 判定过期并清空重建（召回恒空但日志可见），前缀漏配只是静默降质。
    所以用一条用例把三者绑在一起。
    """
    settings = Settings()
    assert settings.embedding_model == DEFAULT_EMBEDDING_MODEL
    assert settings.embedding_dim == EMBED_DIM
    assert settings.vector_query_prefix, "默认模型需要查询侧前缀"
    assert settings.vector_doc_prefix, "默认模型需要文档侧前缀"


def test_rrf_weight_parameter_scales_scores():
    """`rrf` 的 weights 参数必须真的按路加权（保留它是为了能实测"给某路加权"）。

    合成语料/ B 集的门禁都建立在 `rrf` 的绝对分数上（预取门槛用 1/(k+5)），所以这条
    同时钉住"权重只缩放、不改变单路内部顺序"。
    """
    lists = [[{"row_id": 1}], [], [{"row_id": 1}]]
    assert rrf(lists)[0]["score"] == pytest.approx(2 / 61)
    assert rrf(lists, weights=[1.0, 1.0, 2.0])[0]["score"] == pytest.approx(3 / 61)
    # 默认等权 = 历史行为
    assert RRF_WEIGHTS == (1.0, 1.0, 1.0)


def test_doc_prefix_only_applies_to_documents(monkeypatch):
    """文档侧与查询侧前缀**互相独立**——这是换模型时最容易配错的一处。

    实测过的三种情况都不一样：bge v1.5 两侧都不要；Qwen3-Embedding 只在查询侧加
    `Instruct: {任务}\\nQuery:`；embeddinggemma 两侧都要（query `task: search result | query: `
    / doc `title: none | text: `）。两侧合成一个字段就必然配错其中一种。
    """
    seen: list[list[str]] = []

    class StubModel:
        def embed(self, texts):
            seen.append(list(texts))
            return [[0.5, 0.5] for _ in texts]

    embeddings = FastEmbedEmbeddings(dim=2, query_prefix="Q:", doc_prefix="D:")
    monkeypatch.setattr(embeddings, "_ensure_model", lambda: StubModel())
    embeddings.embed(["正文"])
    embeddings.embed(["问题"], query=True)

    assert seen[0] == ["D:正文"], "文档侧应加 doc_prefix"
    assert seen[1] == ["Q:问题"], "查询侧应加 query_prefix"
    assert FastEmbedEmbeddings().doc_prefix == "", "doc_prefix 默认必须为空"


def test_resolve_cache_dir_defaults_under_data_dir(tmp_path, monkeypatch):
    """模型缓存默认落在 `<data_dir>/fastembed_cache`，而不是 fastembed 的 `%TEMP%`。

    模型有 0.09–2.4GB：放系统临时目录既有被清理的风险，又会吃系统盘空间。
    """
    monkeypatch.delenv("FASTEMBED_CACHE_PATH", raising=False)
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    resolved = Path(resolve_cache_dir(settings))
    assert resolved == tmp_path / "data" / "fastembed_cache"
    assert resolved.is_dir()
    assert os.environ["FASTEMBED_CACHE_PATH"] == str(resolved)

    explicit = resolve_cache_dir(Settings(data_dir=tmp_path / "data", embedding_cache_dir=str(tmp_path / "elsewhere")))
    assert Path(explicit) == tmp_path / "elsewhere"


def test_l2_normalize_and_pack_vector():
    assert l2_normalize([3.0, 4.0]) == pytest.approx([0.6, 0.8])
    assert l2_normalize([0.0, 0.0]) == [0.0, 0.0]
    assert len(pack_vector([0.1] * EMBED_DIM)) == EMBED_DIM * 4


async def test_null_components_return_honest_empties():
    assert NullEmbeddings().embed(["甲"], query=True) == []
    assert await NullVectorStore().write([("x", [1.0])]) == 0
    assert await NullVectorStore().search([1.0], limit=5) == []


# ── vec0 实现（需要扩展）──────────────────────────────────────────────


@requires_vec
async def test_vec_store_write_is_idempotent_and_search_filters_scope(tmp_path):
    """vec0 不支持 INSERT OR REPLACE，实现走 DELETE + INSERT；重复写不能产生重复行。"""
    settings = make_settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    indexer = FtsIndexer(store)
    conversation = await store.save(Conversation())
    chunks = await seed(store, indexer, conversation.id, "t1", "https://a.example.com/1", "# 键盘\n甲")
    row_id = await first_row_id(store)

    vec_store = SQLiteVecStore(settings.data_dir / "app.sqlite", dim=EMBED_DIM)
    assert await vec_store.ensure_schema() is True
    vector = l2_normalize([1.0] * EMBED_DIM)

    assert await vec_store.write([(chunks[0]["id"], vector)]) == 1
    assert await vec_store.write([(chunks[0]["id"], vector)]) == 1  # 幂等，不重复落行

    hits = await vec_store.search(vector, limit=5, conversation_id=conversation.id)
    assert [hit["row_id"] for hit in hits] == [row_id]
    assert hits[0]["origin"] == "https://a.example.com/1"

    # 范围收窄：别的会话查不到（后过滤走 JOIN chunks）
    assert await vec_store.search(vector, limit=5, conversation_id="other") == []
    assert await vec_store.search(vector, limit=5, source_type="local_file") == []
    await store.engine.dispose()


@requires_vec
async def test_vec_store_rejects_vector_with_wrong_dimension(tmp_path):
    settings = make_settings(tmp_path)
    vec_store = SQLiteVecStore(settings.data_dir / "app.sqlite", dim=EMBED_DIM)
    assert await vec_store.write([("x", [1.0, 2.0])]) == 0
    assert await vec_store.search([1.0, 2.0], limit=5) == []


@requires_vec
def test_migration_creates_vector_tables_and_downgrade_drops_them(tmp_path):
    settings = make_settings(tmp_path)
    path = settings.data_dir / "app.sqlite"

    def table_names():
        conn = sqlite3.connect(path)
        try:
            return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()

    names = table_names()
    assert {"chunk_vectors", "vector_meta"} <= names

    # 0005 插在 0004 与 0006 之间：降级到 0004 应把向量表摘掉且不破坏后续迁移
    command.downgrade(alembic_config(settings), "0004")
    assert "chunk_vectors" not in table_names()

    command.upgrade(alembic_config(settings), "head")
    assert {"chunk_vectors", "vector_meta"} <= table_names()


# ── 换模型 / 换维度：必须重建而不是混用 ──────────────────────────────────


def _vec_ddl_and_meta(settings) -> tuple[str, list[tuple]]:
    conn = sqlite3.connect(settings.data_dir / "app.sqlite")
    try:
        row = conn.execute("SELECT sql FROM sqlite_master WHERE name='chunk_vectors'").fetchone()
        ddl = row[0] if row else ""
        meta = conn.execute("SELECT model, dim FROM vector_meta").fetchall()
    finally:
        conn.close()
    return ddl, [tuple(item) for item in meta]


@requires_vec
async def test_ensure_schema_is_idempotent_when_config_unchanged(tmp_path):
    """配置没变时反复 ensure_schema **不能**清掉已有向量。

    这条防的是回归：校准逻辑一旦写成"每次都重建"，向量路会在每次启动后静默清空。
    """
    settings = make_settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    indexer = FtsIndexer(store)
    conversation = await store.save(Conversation())
    chunks = await seed(store, indexer, conversation.id, "t1", "https://a.example.com/1", "# 键盘\n甲")

    vec_store = SQLiteVecStore(settings.data_dir / "app.sqlite", dim=EMBED_DIM)
    vector = l2_normalize([1.0] * EMBED_DIM)
    assert await vec_store.ensure_schema()
    assert await vec_store.write([(chunks[0]["id"], vector)]) == 1
    assert await vec_store.ensure_schema() is True
    assert await vec_store.ensure_schema() is True
    assert await vec_store.search(vector, limit=5) != [], "配置未变却把向量清掉了"
    await store.engine.dispose()


@requires_vec
async def test_ensure_schema_rebuilds_when_dimension_changes(tmp_path):
    """vec0 的维度建表时钉死在 `float[N]`，而 `embedding_dim` 是配置项。

    不一致时写入会失败、检索恒 0 召回——表现是"接了向量路但永远召回为空"，
    只在 debug 日志里可见。校准必须显式重建。
    """
    settings = make_settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    indexer = FtsIndexer(store)
    conversation = await store.save(Conversation())
    chunks = await seed(store, indexer, conversation.id, "t1", "https://a.example.com/1", "# 键盘\n甲")

    new_dim = EMBED_DIM + 256
    before = SQLiteVecStore(settings.data_dir / "app.sqlite", dim=EMBED_DIM, model="m/a")
    assert await before.ensure_schema()
    assert await before.write([(chunks[0]["id"], l2_normalize([1.0] * EMBED_DIM))]) == 1
    assert await before.search(l2_normalize([1.0] * EMBED_DIM), limit=5) != []

    after = SQLiteVecStore(settings.data_dir / "app.sqlite", dim=new_dim, model="m/a")
    assert await after.ensure_schema()
    ddl, meta = _vec_ddl_and_meta(settings)
    assert f"float[{new_dim}]" in ddl, "表没有按新维度重建"
    assert meta == [("m/a", new_dim)]
    assert await after.search(l2_normalize([1.0] * new_dim), limit=5) == [], "旧维度的向量必须被清掉"

    # 新维度可以正常写入与检索
    assert await after.write([(chunks[0]["id"], l2_normalize([1.0] * new_dim))]) == 1
    assert await after.search(l2_normalize([1.0] * new_dim), limit=5) != []
    await store.engine.dispose()


@requires_vec
async def test_ensure_schema_rebuilds_when_model_changes_at_same_dim(tmp_path):
    """同维度换模型比换维度更危险：两套不可比的向量混用，检索能跑但结果是错的。"""
    settings = make_settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    indexer = FtsIndexer(store)
    conversation = await store.save(Conversation())
    chunks = await seed(store, indexer, conversation.id, "t1", "https://a.example.com/1", "# 键盘\n甲")

    first = SQLiteVecStore(settings.data_dir / "app.sqlite", dim=EMBED_DIM, model="m/a")
    vector = l2_normalize([1.0] * EMBED_DIM)
    assert await first.ensure_schema()
    assert await first.write([(chunks[0]["id"], vector)]) == 1
    assert await first.search(vector, limit=5) != []

    second = SQLiteVecStore(settings.data_dir / "app.sqlite", dim=EMBED_DIM, model="m/b")
    assert await second.ensure_schema()
    _, meta = _vec_ddl_and_meta(settings)
    assert meta == [("m/b", EMBED_DIM)], "vector_meta 必须按单行语义只留当前模型"
    assert await second.search(vector, limit=5) == [], "换模型后必须清空旧向量，不能混用"
    await store.engine.dispose()
