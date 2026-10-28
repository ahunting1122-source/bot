"""HRR 全息图记忆的契约测试（`app/holographic.py`，MATRIX 9.11）。

三条要锁住的东西，对应模块 docstring 里对 Hermes 的引用：

1. **可复现**：同一文本、同一维度 → 逐元素相同的向量。哈希派生，不碰 `random`
   （`random` 依赖全局状态，别处 seed 一次就废）。
2. **bind→unbind 能近似还原**：相似度 > 0.8。这条在真 HRR 的随机实向量上做不到
   （单对还原的余弦上限约 1/√2），所以本模块把原子定义在频域（单位模长随机相位，
   时域自相关严格是 delta）。**多词叠加之后再 bind→unbind 会有串扰**，那是 HRR 的
   统计性质，单独一条测试把它钉住，免得下一个人当成回归。
3. **三路权重 0.4 / 0.3 / 0.3**：照 research 07 §4.1 的数值，不是自己拍的。
"""

from __future__ import annotations

import numpy as np
import pytest

from app import holographic as hh

# ── 编码：可复现 ────────────────────────────────────────────────────────────


def test_encode_is_deterministic():
    """同一文本同向量——这是能落库、能跨进程比对的前提。"""
    first = hh.encode("张三喜欢咖啡")
    second = hh.encode("张三喜欢咖啡")
    assert first.shape == second.shape == (hh.DIM,)
    assert np.array_equal(first, second)


def test_encode_differs_for_different_text():
    assert hh.similarity(hh.encode("张三"), hh.encode("李四")) < 0.2


def test_default_dim_is_1024_and_configurable():
    """1024 是 Hermes 的 `hrr_dim` 取值（research 07 §4.1），不是随手挑的。"""
    assert hh.DIM == 1024
    assert hh.encode("随便", 256).shape == (256,)
    with pytest.raises(ValueError):
        hh.encode_atom("x", 1)


def test_empty_text_encodes_to_a_zero_vector():
    vector = hh.encode("   ，。  ")
    assert not np.any(vector)
    # 零向量与任何东西的相似度都是 0（给 1.0 会让空记忆霸占榜首）。
    assert hh.similarity(vector, hh.encode("张三")) == 0.0


# ── bind / unbind ───────────────────────────────────────────────────────────


def test_bind_unbind_restores_the_original_atom():
    """`unbind(bind(a, b), b) ≈ a`，实测余弦 1.0（远高于要求的 0.8）。"""
    a = hh.encode_atom("张三")
    b = hh.encode_atom("喜欢咖啡")
    recovered = hh.unbind(hh.bind(a, b), b)
    assert hh.similarity(recovered, a) > 0.8
    # 还原出来的是 a，不是 b、也不是别的词。
    assert hh.similarity(recovered, b) < 0.2
    assert hh.similarity(recovered, hh.encode_atom("李四")) < 0.2


def test_bind_output_does_not_look_like_either_input():
    """绑定的意义就是"绑完就搜不到了"，只能靠 unbind 取回。"""
    a, b = hh.encode_atom("张三"), hh.encode_atom("咖啡")
    bound = hh.bind(a, b)
    assert abs(hh.similarity(bound, a)) < 0.2
    assert abs(hh.similarity(bound, b)) < 0.2


def test_multi_token_bind_unbind_is_approximate_not_exact():
    """多词叠加后 bind→unbind 有串扰（实测 ≈0.78）：这是 HRR 的统计性质，不是 bug。

    原子的频谱是单位模长，但**叠加之后**的频谱不再是——自相关出现旁瓣，于是
    `unbind(bind(a,b),b)` 得到的是 `a + 噪声`。真 HRR（随机实向量）在这里只有 ≈0.71，
    本模块的频域原子好一些，但仍不是精确还原。
    """
    a, b = hh.encode("张三"), hh.encode("喜欢咖啡")
    similarity = hh.similarity(hh.unbind(hh.bind(a, b), b), a)
    assert 0.6 < similarity < 1.0, f"实测 {similarity}：既不该精确为 1，也不该塌到 0"


def test_bind_rejects_mismatched_dimensions():
    with pytest.raises(ValueError):
        hh.bind(hh.encode("a", 64), hh.encode("b", 128))


def test_superpose_correlates_with_its_members():
    a, b = hh.encode_atom("张三"), hh.encode_atom("咖啡")
    bundle = hh.superpose([a, b])
    assert hh.similarity(bundle, a) > 0.5
    assert hh.similarity(bundle, b) > 0.5


# ── 三路权重 ───────────────────────────────────────────────────────────────


def test_weights_are_forty_thirty_thirty():
    """照 research 07 §4.1：fts 0.4 / jaccard 0.3 / hrr 0.3。"""
    assert (hh.W_FTS, hh.W_JACCARD, hh.W_HRR) == (0.4, 0.3, 0.3)
    assert hh.W_FTS + hh.W_JACCARD + hh.W_HRR == pytest.approx(1.0)


def test_each_signal_contributes_its_own_weight():
    assert hh.combine(1, 0, 0) == pytest.approx(0.4)
    assert hh.combine(0, 1, 0) == pytest.approx(0.3)
    assert hh.combine(0, 0, 1) == pytest.approx(0.3)
    assert hh.combine(1, 1, 1) == pytest.approx(1.0)


def test_score_is_exactly_the_weighted_sum_of_its_three_signals():
    query, memory = "咖啡", "喜欢咖啡"
    expected = (
        hh.W_FTS * hh.fts_score(query, memory)
        + hh.W_JACCARD * hh.jaccard_score(query, memory)
        + hh.W_HRR * hh.hrr_score(query, memory)
    )
    assert hh.score(query, memory) == pytest.approx(expected)
    detail = hh.explain(query, memory)
    # `explain` 里的分量是四舍五入到 4 位的展示值，所以按 1e-4 比。
    assert detail["total"] == pytest.approx(expected, abs=1e-4)
    assert detail["weights"] == {"fts": 0.4, "jaccard": 0.3, "hrr": 0.3}


def test_out_of_range_signals_are_clamped():
    """上游给超界值不该把总分带飞。"""
    assert hh.combine(2, 2, 2) == pytest.approx(1.0)
    assert hh.combine(-1, -1, -1) == pytest.approx(0.0)


def test_fts_score_is_a_coverage_proxy_that_handles_chinese():
    """`memories` 表没有 FTS5 虚表，这一路是"查询词覆盖率"代理，权重不变。"""
    assert hh.fts_score("咖啡", "喜欢咖啡") == pytest.approx(1.0)
    assert hh.fts_score("茶", "喜欢咖啡") == pytest.approx(0.0)
    assert hh.fts_score("", "喜欢咖啡") == pytest.approx(0.0)


def test_hrr_score_is_never_negative():
    """负相似在词袋模型里只是随机相位反相，当噪声处理。"""
    for other in ("张三", "李四", "王五", "项目在 E 盘"):
        assert hh.hrr_score("咖啡", other) >= 0.0


# ── MemoryGraph ─────────────────────────────────────────────────────────────


def test_memory_graph_query_ranks_the_matching_entry_first():
    graph = hh.MemoryGraph({"偏好": "喜欢咖啡，不加糖", "项目": "在 E 盘做短剧账号"})
    rows = graph.query("咖啡", limit=5)
    assert rows and rows[0]["key"] == "偏好"
    assert {"fts", "jaccard", "hrr", "total"} <= set(rows[0])
    assert rows[0]["total"] >= rows[-1]["total"]


def test_memory_graph_query_on_empty_or_blank_returns_nothing():
    assert hh.MemoryGraph().query("咖啡") == []
    assert hh.MemoryGraph({"a": "b"}).query("   ") == []


def test_memory_graph_neighbors_reuses_the_shared_graph_edges():
    """邻居走 `memory_graph` 的三条启发式（同前缀/引用/同时间段），不另造一套边。"""
    graph = hh.MemoryGraph(
        {"project/orbit/stack": "Python + SQLite", "project/orbit/deploy": "单机部署"}
    )
    found = graph.neighbors("project/orbit/stack", 1)
    assert [row["id"] for row in found] == ["project/orbit/deploy"]
    assert found[0]["via"] == "same_prefix"


def test_memory_graph_add_remove_and_bank():
    graph = hh.MemoryGraph()
    assert len(graph.keys()) == 0
    assert graph.bank_norm() == pytest.approx(0.0)  # 空库返回零向量，不抛异常
    graph.add("a", "内容")
    assert graph.keys() == ["a"]
    assert graph.bank_norm() == pytest.approx(1.0)
    assert graph.remove("a") is True
    assert graph.remove("a") is False
    with pytest.raises(ValueError):
        graph.add("  ", "没有 key")


def test_memory_graph_dim_is_honoured():
    graph = hh.MemoryGraph({"a": "b"}, dim=128)
    assert graph.vector("a").shape == (128,)
    with pytest.raises(ValueError):
        hh.MemoryGraph({}, dim=1)
