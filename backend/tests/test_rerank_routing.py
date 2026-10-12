"""融合第三档（cross-encoder 重排）与查询类型路由。

重排模型不参与单测（要下载约 1GB），一律用确定性假重排器。这里锁的是这些不变量：
路由是纯函数、门控只丢噪声、重排只重排池内候选、任何缺失/失败都**如实降级**。
"""

import pytest

from app.config import Settings
from app.main import migrate
from app.policies import Policies
from app.rerank import DEFAULT_RERANK_MODEL, FastEmbedReranker, NullReranker, build_reranker
from app.retrieval import (
    LEXICAL_MIN_COVERAGE,
    FtsRetriever,
    coverage,
    gate_candidates,
    rerank_hits,
    route_query,
    rrf,
)
from app.storage import Store


def make_settings(tmp_path, **overrides):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files", **overrides)
    settings.prepare()
    migrate(settings)
    return settings


# ── 查询类型路由（纯函数）────────────────────────────────────────────────


def test_route_query_flags_identifiers_as_lexical():
    """专名/标识符/路径/引号 → 词面优先：这类查询向量可能召回"语义相邻但用错技术"的块。"""
    assert route_query('"精确短语"').kind == "lexical"
    assert route_query("sqlite-vec 用哪个库").kind == "lexical"
    assert route_query("down_revision 怎么分配").kind == "lexical"
    assert route_query("看 ARCHITECTURE.md 里的切块参数").kind == "lexical"
    assert route_query("app/retrieval.py 在哪").kind == "lexical"


def test_route_query_short_disables_trigram():
    """≤2 字：trigram 只索引 ≥3 字符，必然 0 召回，关掉这一路省一次查询。"""
    route = route_query("键盘")
    assert route.kind == "short"
    assert route.paths == (False, True, True)
    assert route.min_coverage == 0.0


def test_route_query_natural_language_is_identity():
    """纯中文散文查询落进 semantic —— 路由对它是恒等变换（如实记录，不夸大战功）。"""
    route = route_query("为什么两个字的中文词查不到")
    assert route.kind == "semantic"
    assert route.paths == (True, True, True)
    assert route.min_coverage == 0.0


def test_route_query_can_be_disabled():
    route = route_query("sqlite-vec", routing=False)
    assert route.kind == "semantic"
    assert route.paths == (True, True, True)
    assert route.min_coverage == 0.0


def test_lexical_route_tightens_coverage_gate():
    assert route_query("sqlite-vec").min_coverage >= LEXICAL_MIN_COVERAGE
    assert route_query("为什么两个字的中文词查不到").min_coverage == 0.0


# ── 候选覆盖率与门控 ──────────────────────────────────────────────────────


def test_coverage_counts_query_windows_present_in_chunk():
    assert coverage("机械键盘轴体", "关于机械键盘轴体的说明", "trigram") == 1.0
    assert coverage("键盘", "机械键盘很好用", "chars") == 1.0
    # 只沾上一个 3 字窗的块，覆盖率被显著拉低
    partial = coverage("机械键盘轴体手感", "机械键盘是好的", "trigram")
    assert 0.0 < partial < 0.5


def test_gate_candidates_drops_noise_only():
    query = "机械键盘轴体手感"
    strong = {"row_id": 1, "text": "机械键盘轴体手感的说明"}
    weak = {"row_id": 2, "text": "机械键盘是好的"}
    assert [c["row_id"] for c in gate_candidates([strong, weak], query, "trigram", 0.5)] == [1]
    # 阈值为 0 = 不门控（自然语言路径的默认）
    assert gate_candidates([strong, weak], query, "trigram", 0.0) == [strong, weak]
    # 空输入不炸
    assert gate_candidates([], query, "trigram", 0.5) == []


# ── RRF 路径标签对齐 ─────────────────────────────────────────────────────


def test_rrf_labels_stay_aligned_when_a_leg_is_disabled():
    """短查询路由会关掉 trigram 路；必须传空列表而不是少传一路，否则标签整体错位。"""
    hits = rrf([[], [{"row_id": 1, "text": "a"}], [{"row_id": 1, "text": "a"}]])
    assert hits[0]["paths"] == ["chars", "vector"]


# ── 重排 ────────────────────────────────────────────────────────────────


class ScriptedReranker:
    """按预设下标返回顺序，并记录收到的文档。"""

    enabled = True
    model = "scripted"

    def __init__(self, order):
        self.order = list(order)
        self.calls = []

    def rerank(self, query, documents, *, top_k):
        self.calls.append({"query": query, "documents": list(documents), "top_k": top_k})
        return self.order[:top_k]


def test_rerank_hits_reorders_pool_and_keeps_tail_order():
    hits = [{"row_id": index, "text": f"doc{index}"} for index in range(5)]
    reranker = ScriptedReranker([2, 0, 1])
    out = rerank_hits(hits, "q", reranker, pool=3)
    assert [hit["row_id"] for hit in out] == [2, 0, 1, 3, 4]
    assert all(hit.get("reranked") for hit in out[:3])
    assert "reranked" not in out[3]
    # 只把池内文档送去打分，池外不浪费推理
    assert reranker.calls[0]["documents"] == ["doc0", "doc1", "doc2"]


def test_rerank_hits_backfills_indices_the_reranker_omits():
    hits = [{"row_id": index, "text": f"doc{index}"} for index in range(3)]
    out = rerank_hits(hits, "q", ScriptedReranker([1]), pool=3)
    assert [hit["row_id"] for hit in out] == [1, 0, 2]


def test_rerank_hits_is_identity_without_a_reranker():
    hits = [{"row_id": index, "text": f"doc{index}"} for index in range(3)]
    assert rerank_hits(hits, "q", NullReranker(), pool=3) == hits
    assert rerank_hits(hits, "q", None, pool=3) == hits


def test_null_reranker_is_identity_and_reports_disabled():
    reranker = NullReranker()
    assert reranker.enabled is False
    assert reranker.rerank("q", ["a", "b"], top_k=5) == [0, 1]
    assert reranker.rerank("q", [], top_k=5) == []


class _StubEncoder:
    def __init__(self, scores):
        self.scores = scores

    def rerank(self, query, documents, batch_size=64, **kwargs):
        return list(self.scores)[: len(list(documents))]


def test_fastembed_reranker_orders_by_score_without_downloading():
    reranker = FastEmbedReranker()
    reranker._encoder = _StubEncoder([0.1, 0.9, 0.5])
    assert reranker.rerank("q", ["a", "b", "c"], top_k=3) == [1, 2, 0]
    assert reranker.rerank("q", ["a", "b", "c"], top_k=2) == [1, 2]


def test_fastembed_reranker_rejects_score_count_mismatch():
    reranker = FastEmbedReranker()
    reranker._encoder = _StubEncoder([0.1])
    with pytest.raises(RuntimeError):
        reranker.rerank("q", ["a", "b"], top_k=2)


def test_build_reranker_is_off_by_default_and_on_when_asked():
    assert build_reranker(Settings(rerank_enabled=False)).enabled is False
    enabled = build_reranker(Settings(rerank_enabled=True))
    assert enabled.enabled is True
    assert enabled.model == DEFAULT_RERANK_MODEL


# ── 检索器集成 ───────────────────────────────────────────────────────────


class FakeIndexer:
    def __init__(self, trigram, chars):
        self.trigram = list(trigram)
        self.chars = list(chars)
        self.modes = []

    async def write(self, chunks):
        return 0

    async def search(
        self, query, *, conversation_id="", host="", source_type="", limit=20, mode="trigram"
    ):
        self.modes.append(mode)
        return list(self.trigram if mode == "trigram" else self.chars)


async def test_search_reranks_fused_candidates(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    indexer = FakeIndexer(
        [
            {"row_id": 1, "text": "甲"},
            {"row_id": 2, "text": "乙"},
            {"row_id": 3, "text": "丙"},
        ],
        [],
    )
    retriever = FtsRetriever(store, indexer=indexer, reranker=ScriptedReranker([2, 1, 0]))
    hits = await retriever.search("键盘垫子", top_k=3)
    assert [hit["row_id"] for hit in hits] == [3, 2, 1]
    assert hits[0]["reranked"] is True


async def test_search_skips_rerank_when_reranker_is_null(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    indexer = FakeIndexer(
        [{"row_id": 1, "text": "甲"}, {"row_id": 2, "text": "乙"}], []
    )
    retriever = FtsRetriever(store, indexer=indexer, reranker=NullReranker())
    hits = await retriever.search("键盘垫子", top_k=2)
    assert [hit["row_id"] for hit in hits] == [1, 2]
    assert "reranked" not in hits[0]


async def test_search_short_query_skips_trigram_leg(tmp_path):
    settings = make_settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    indexer = FakeIndexer([{"row_id": 9, "text": "甲"}], [{"row_id": 1, "text": "乙"}])
    retriever = FtsRetriever(store, indexer=indexer)
    hits = await retriever.search("键盘")
    assert [hit["row_id"] for hit in hits] == [1]
    assert "trigram" not in indexer.modes


def test_policies_default_wires_a_reranker(tmp_path):
    """默认开的**类默认值**是 True（2026-10-08 收口）。

    测试套件有 autouse 夹具设 `ORBIT_RERANK_ENABLED=0`（避免加载 1GB 模型），
    环境变量会盖过字段默认值，故断言 `Settings` 的声明默认值；再显式传 `True`
    验证"开着时注入的是真重排器"。
    """
    from app.config import Settings as _S

    assert _S.model_fields["rerank_enabled"].default is True

    settings = make_settings(tmp_path, rerank_enabled=True)
    store = Store(settings.data_dir / "app.sqlite")
    policies = Policies.default(settings, store)
    assert hasattr(policies.reranker, "rerank")
    assert policies.reranker.enabled is True


def test_policies_reranker_can_be_disabled(tmp_path):
    """显式关掉时退回 NullReranker 空转（如实降级，不炸）。"""
    settings = make_settings(tmp_path, rerank_enabled=False)
    store = Store(settings.data_dir / "app.sqlite")
    policies = Policies.default(settings, store)
    assert policies.reranker.enabled is False
