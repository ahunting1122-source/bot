"""记忆图谱的契约测试（`app/memory_graph.py`）。

三条启发式各锁一条，外加 `neighbors(depth=2)` 与"空记忆不崩"。
图谱这类东西最容易写成"看起来有图、其实边全是噪声"，所以每条边都要连**理由**一起断言。
"""

from __future__ import annotations

from app.config import Settings
from app.main import migrate
from app.memory_graph import build, graph, last_segment, neighbors, prefix_of, summary
from app.storage import Store


def _nodes(payload) -> dict[str, dict]:
    return {node["id"]: node for node in payload["nodes"]}


def _edges(payload, kind: str) -> list[dict]:
    return [edge for edge in payload["edges"] if edge["kind"] == kind]


# ── 三条启发式 ───────────────────────────────────────────────────────────────


def test_same_prefix_edges():
    payload = build(
        {
            "项目/Orbit/存储": "SQLite",
            "项目/Orbit/前端": "React",
            "项目/Hermes/存储": "state.db",
        }
    )
    same = _edges(payload, "same_prefix")
    pairs = {(edge["source"], edge["target"]) for edge in same}
    # 同前缀两两相连：项目下三条 → 3 条边
    assert pairs == {
        ("项目/Orbit/存储", "项目/Orbit/前端"),
        ("项目/Orbit/存储", "项目/Hermes/存储"),
        ("项目/Orbit/前端", "项目/Hermes/存储"),
    }
    assert all("项目" in edge["reason"] for edge in same)


def test_flat_keys_do_not_create_same_prefix_edges():
    """扁平 key 没有前缀：若把它们全连起来，图就成了一个没有信息量的团。"""
    payload = build({"语言": "中文", "时区": "UTC+8"})
    assert _edges(payload, "same_prefix") == []
    assert _nodes(payload)["语言"]["degree"] == 0


def test_references_edges_are_directional():
    payload = build(
        {
            "项目/Orbit/存储": "用 SQLite + aiosqlite",
            "项目/Orbit/部署": "部署细节见 项目/Orbit/存储 那条",
        }
    )
    refs = _edges(payload, "references")
    assert len(refs) == 1
    assert refs[0]["source"] == "项目/Orbit/部署"
    assert refs[0]["target"] == "项目/Orbit/存储"
    assert "正文提到" in refs[0]["reason"]


def test_references_ignores_short_names():
    """`id` / `url` 这种短名字在几乎每条记忆里都出现，产边就是噪声。"""
    payload = build({"id": "abc", "别的": "这里的 id 出现了但太短"})
    assert _edges(payload, "references") == []


def test_temporal_edges():
    payload = build(
        {
            "里程碑/上线": "2025-06 上线",
            "会议/复盘": "2025-06 复盘，结论见里程碑那条",
        }
    )
    temporal = _edges(payload, "temporal")
    assert len(temporal) == 1
    assert "2025-06" in temporal[0]["reason"]


def test_temporal_does_not_fire_on_shared_year_only_when_absent():
    payload = build({"a": "没有时间", "b": "也没有"})
    assert _edges(payload, "temporal") == []


# ── 节点 ─────────────────────────────────────────────────────────────────────


def test_nodes_carry_label_prefix_and_degree():
    payload = build({"项目/Orbit/存储": "SQLite", "项目/Orbit/前端": "React"})
    nodes = _nodes(payload)
    node = nodes["项目/Orbit/存储"]
    assert node["label"] == "存储"
    assert node["prefix"] == "项目"
    assert node["degree"] == 1
    assert node["chars"] == len("项目/Orbit/存储") + len("SQLite")


def test_edges_are_sorted_and_deduplicated():
    payload = build({"项目/a": "x", "项目/b": "y", "项目/c": "z"})
    kinds = [edge["kind"] for edge in payload["edges"]]
    assert kinds == sorted(kinds, key=lambda kind: ("references", "same_prefix", "temporal").index(kind))
    tokens = [(edge["source"], edge["target"], edge["kind"]) for edge in payload["edges"]]
    assert len(tokens) == len(set(tokens))


def test_summary_counts_isolated_nodes():
    payload = build({"项目/a": "x", "项目/b": "y", "孤立": "z"})
    stats = summary({"项目/a": "x", "项目/b": "y", "孤立": "z"})
    assert stats["nodes"] == 3
    assert stats["edges"] == len(payload["edges"])
    assert stats["isolated"] == ["孤立"]


# ── 邻居（BFS） ──────────────────────────────────────────────────────────────


def test_neighbors_depth_one_and_two():
    source = {
        "项目/A": "根",
        "项目/B": "直接邻居",
        "其它/C": "通过 B 才能到",
    }
    one = neighbors("项目/A", 1, source=source)
    assert {row["id"] for row in one} == {"项目/B"}
    assert all(row["distance"] == 1 for row in one)

    # depth=2 才够到 C（A-B 同前缀，B-C 靠 temporal 之外的引用/前缀）
    source = {
        "项目/A": "根",
        "项目/B": "提到 项目/A",
        "孤立/C": "提到 项目/B",
    }
    two = neighbors("项目/A", 2, source=source)
    assert {row["id"] for row in two} == {"项目/B", "孤立/C"}
    assert {row["id"]: row["distance"] for row in two} == {"项目/B": 1, "孤立/C": 2}


def test_neighbors_of_unknown_key_is_empty():
    assert neighbors("不存在的键", 2, source={"项目/A": "x"}) == []


def test_neighbors_depth_zero_is_empty():
    assert neighbors("项目/A", 0, source={"项目/A": "x", "项目/B": "y"}) == []


def test_neighbors_reuses_payload():
    payload = build({"项目/A": "x", "项目/B": "y"})
    assert [row["id"] for row in neighbors("项目/A", 1, payload=payload)] == ["项目/B"]


# ── 边界 ─────────────────────────────────────────────────────────────────────


def test_empty_memory_does_not_crash():
    assert graph({}) == {"nodes": [], "edges": []}
    assert graph(None) == {"nodes": [], "edges": []}
    assert graph([]) == {"nodes": [], "edges": []}
    assert neighbors("任意", 3, source={}) == []


def test_helpers():
    assert prefix_of("项目/Orbit/存储") == "项目"
    assert prefix_of("语言") == ""
    assert last_segment("项目/Orbit/存储") == "存储"
    assert last_segment("语言") == "语言"


def test_duplicate_keys_in_pair_list_collapse():
    payload = build([("项目/A", "第一次"), ("项目/A", "第二次")])
    assert len(payload["nodes"]) == 1


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_memory_graph_and_journey_list(tmp_path, capsys):
    import app.cli.commands.memory_extra  # noqa: F401 —— 接线
    from app.cli.main import main

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")

    import asyncio

    from app.memory_batch import apply_batch

    async def seed():
        # 用批处理直接写：`store.set_memory` 会做同义 key 合并，
        # 两个只差两个字的 key 会被合成一条，测图谱就不能走那条路。
        await apply_batch(
            store,
            [
                {"op": "add", "key": "项目/Orbit/存储", "value": "SQLite + aiosqlite"},
                {"op": "add", "key": "项目/Orbit/部署", "value": "上线前先跑 migrate，见 项目/Orbit/存储"},
            ],
        )
        await store.engine.dispose()

    asyncio.run(seed())

    data_dir = str(settings.data_dir)
    assert main(["--data-dir", data_dir, "memory", "graph"]) == 0
    out = capsys.readouterr().out
    assert "项目/Orbit/存储" in out
    assert "2 个节点 / 2 条边" in out

    # 邻居视图
    assert main(["--data-dir", data_dir, "memory", "graph", "--key", "项目/Orbit/部署", "--depth", "1"]) == 0
    assert "邻居" in capsys.readouterr().out

    # `memory-graph` 是 `journey` 的别名，journey list 输出同一批节点
    assert main(["--data-dir", data_dir, "journey", "list"]) == 0
    assert "项目/Orbit/存储" in capsys.readouterr().out
