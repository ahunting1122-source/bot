"""记忆图谱：从记忆的 key/value 里抽实体与关系（MATRIX 9.x）。

对标 Hermes 的 `memory-graph`（= `journey` 的别名，星图/时间线的终端版）。
`orbit memory graph` 与 `journey` 共用这里的数据。

## 为什么不引入图数据库

Orbit 的记忆规模在**几百条**量级（实测 Hermes 的 `MEMORY.md` 是 20 条，本机
`memories` 表是个位到几十条）。本模块两两比较是 O(n²) 的纯字符串操作，
n=1000 时约 10⁶ 次子串判断，几十毫秒——为这个量级引入 Neo4j / networkx /
SQLite 图扩展，换来的只有部署负担与一个新的故障点。等到记忆上千条、或者
需要真正的多跳查询（Hermes 的 holographic 用 HRR 向量做的那套），再换实现；
在那之前，内存里算完就丢是更诚实的选择。

## 三条启发式（全部是纯函数，可单测、可解释）

1. `same_prefix`：key 里的**层级分隔符**（`/` `:` `.` `>`）切出的第一段相同
   → 两条记忆属于同一个"主题簇"。例：`project/orbit/stack` 与 `project/orbit/deploy`
   都有前缀 `project`。分隔符为空（扁平 key）时不产边——"所有 key 都同前缀"
   等于全连通，那种图没有信息量。
2. `references`：一条记忆的 **value 里出现了另一条记忆的 key**（或它的最后一段，
   且长度 ≥ `MIN_MENTION`）→ 有向边，方向是"提到者 → 被提到者"。
   这是"实体关系"里唯一能靠纯文本确定的关系。
3. `temporal`：key 或 value 里出现同一个**时间标记**（`2025` / `2025-06` / `Q3`）
   → 两条记忆在同一时间段，产无向边。没有时间戳字段时的替代品：
   `memories` 表只有 `updated_at`（写入时间），不是"这件事发生的时间"。
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

#: key 的层级分隔符。与 `memory_batch._prefix_of` 保持同一套。
SEPARATORS: tuple[str, ...] = ("/", ":", ".", ">")

#: value 里提到别的 key 时，被提到的名字至少要有这么长，否则 `id`/`url` 这类
#: 短名字会在几乎每条记忆里"命中"，产出一堆噪声边。
MIN_MENTION = 3

#: 时间标记：`2025` / `2025-06` / `2025-06-18` / `2025/06` / `Q3`。
TEMPORAL_PATTERN = re.compile(r"(?<![\d-])((?:19|20)\d{2}(?:[-/]\d{1,2}){0,2}|Q[1-4])(?![\d-])")

#: 边的种类（顺序即输出排序优先级）。
EDGE_KINDS: tuple[str, ...] = ("references", "same_prefix", "temporal")


def _pairs(source: Any) -> list[tuple[str, str]]:
    """接受 dict / [(key, value)] 两种入参，统一成有序 pair 列表。"""
    if source is None:
        return []
    if isinstance(source, dict):
        return [(str(key), str(value)) for key, value in source.items()]
    pairs: list[tuple[str, str]] = []
    for item in source:
        try:
            key, value = item
        except (TypeError, ValueError):
            continue
        name = str(key or "").strip()
        if name:
            pairs.append((name, "" if value is None else str(value)))
    return pairs


def prefix_of(key: str) -> str:
    """key 的层级前缀（第一个分隔符之前）。没有分隔符返回空串。"""
    for separator in SEPARATORS:
        if separator in key:
            head = key.split(separator, 1)[0].strip()
            if head:
                return head
    return ""


def last_segment(key: str) -> str:
    """key 的最后一段（`project/orbit/stack` → `stack`）。"""
    for separator in SEPARATORS:
        if separator in key:
            return key.rsplit(separator, 1)[1].strip() or key
    return key


def temporal_marks(text: str) -> set[str]:
    """抽出文本里的时间标记。"""
    return set(TEMPORAL_PATTERN.findall(text or ""))


@dataclass
class Node:
    """一个记忆节点。`degree` 由边算出，不是自己数出来的。"""

    id: str
    label: str
    prefix: str
    chars: int
    degree: int = 0
    kinds: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "prefix": self.prefix,
            "chars": self.chars,
            "degree": self.degree,
            "kinds": list(self.kinds),
        }


@dataclass
class Edge:
    """一条边。`reason` 是**给人看的证据**——图谱没有证据就只是装饰。"""

    source: str
    target: str
    kind: str
    weight: int = 1
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "target": self.target,
            "kind": self.kind,
            "weight": self.weight,
            "reason": self.reason,
        }


def build(source: Any) -> dict:
    """从记忆构造 `{nodes, edges}`。

    边的去重键是 `(source, target, kind)`：同一对记忆可以同时"同前缀"又"同时间"，
    这是两种不同的关系，都该留着；但同一种关系只留一条。
    """
    pairs = _pairs(source)
    if not pairs:
        # 空记忆不是错误：没有记忆的用户第一次跑 `memory graph` 应该看到空图，
        # 而不是一个 IndexError。
        return {"nodes": [], "edges": []}

    nodes: dict[str, Node] = {}
    values: dict[str, str] = {}
    for key, value in pairs:
        nodes[key] = Node(
            id=key,
            label=last_segment(key),
            prefix=prefix_of(key),
            chars=len(key) + len(value),
        )
        values[key] = value

    edges: dict[tuple[str, str, str], Edge] = {}

    def add_edge(source_key: str, target_key: str, kind: str, weight: int, reason: str) -> None:
        if source_key == target_key:
            return  # 自环对"邻居"没有意义，只会把 degree 虚高
        if source_key not in nodes or target_key not in nodes:
            return
        token = (source_key, target_key, kind)
        if token in edges:
            return
        edges[token] = Edge(
            source=source_key, target=target_key, kind=kind, weight=weight, reason=reason
        )

    # 1) same_prefix：同前缀两两相连。
    by_prefix: dict[str, list[str]] = defaultdict(list)
    for key, node in nodes.items():
        if node.prefix:
            by_prefix[node.prefix].append(key)
    for prefix, keys in by_prefix.items():
        for index, left in enumerate(keys):
            for right in keys[index + 1 :]:
                add_edge(left, right, "same_prefix", 1, f"同前缀「{prefix}」")

    # 2) references：value 里提到了别的 key。
    for key, value in values.items():
        haystack = value.lower()
        if not haystack:
            continue
        for other, node in nodes.items():
            if other == key:
                continue
            candidates = {other.lower(), node.label.lower()}
            hit = next(
                (
                    name
                    for name in candidates
                    if len(name) >= MIN_MENTION and name in haystack
                ),
                "",
            )
            if hit:
                add_edge(key, other, "references", 2, f"正文提到「{hit}」")

    # 3) temporal：共享时间标记。
    marks: dict[str, set[str]] = {}
    for key, value in values.items():
        marks[key] = temporal_marks(key) | temporal_marks(value)
    mark_index: dict[str, list[str]] = defaultdict(list)
    for key, found in marks.items():
        for mark in found:
            mark_index[mark].append(key)
    for mark, keys in mark_index.items():
        for index, left in enumerate(keys):
            for right in keys[index + 1 :]:
                add_edge(left, right, "temporal", 1, f"同时间段「{mark}」")

    for edge in edges.values():
        for endpoint in (edge.source, edge.target):
            node = nodes[endpoint]
            node.degree += 1
            if edge.kind not in node.kinds:
                node.kinds.append(edge.kind)

    ordered_nodes = sorted(
        nodes.values(), key=lambda node: (-node.degree, node.prefix, node.id)
    )
    ordered_edges = sorted(
        edges.values(),
        key=lambda edge: (EDGE_KINDS.index(edge.kind), edge.source, edge.target),
    )
    return {
        "nodes": [node.to_dict() for node in ordered_nodes],
        "edges": [edge.to_dict() for edge in ordered_edges],
    }


def graph(source: Any = None) -> dict:
    """`build` 的别名——`memory_graph.graph(pairs)` 是给 CLI/测试用的主入口。"""
    return build(source)


def _adjacency(payload: dict) -> dict[str, list[tuple[str, str]]]:
    """无向邻接表：`邻居 -> [(边种类, 方向)]`。BFS 不区分方向，但把方向留着做展示。"""
    table: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for edge in payload.get("edges") or []:
        source, target, kind = edge["source"], edge["target"], edge["kind"]
        table[source].append((target, kind))
        table[target].append((source, kind))
    return table


def neighbors(
    key: str,
    depth: int = 1,
    *,
    source: Any = None,
    payload: dict | None = None,
) -> list[dict]:
    """BFS 找 `key` 的邻居，返回 `[{id, distance, via}]`（按距离、再按 id 排序）。

    给 `payload` 就复用一个已经算好的图；给 `source` 就现算。两者都不给等于空图。
    `depth` 至少为 1（0 或负数直接返回空——"0 跳的邻居"没有定义）。
    """
    if depth < 1:
        return []
    data = payload if payload is not None else build(source)
    if not any(node["id"] == key for node in data.get("nodes") or []):
        return []
    table = _adjacency(data)
    visited: dict[str, int] = {key: 0}
    via: dict[str, str] = {}
    queue: list[str] = [key]
    for current in queue:
        distance = visited[current]
        if distance >= depth:
            continue
        for neighbor, kind in table.get(current, []):
            if neighbor in visited:
                continue
            visited[neighbor] = distance + 1
            via[neighbor] = kind
            queue.append(neighbor)
    return [
        {"id": node, "distance": distance, "via": via.get(node, "")}
        for node, distance in sorted(visited.items(), key=lambda item: (item[1], item[0]))
        if node != key
    ]


def summary(source: Any = None) -> dict:
    """一句话统计，给 `memory graph` 的头部行用。"""
    data = build(source)
    counts: dict[str, int] = {kind: 0 for kind in EDGE_KINDS}
    for edge in data["edges"]:
        counts[edge["kind"]] = counts.get(edge["kind"], 0) + 1
    isolated = [node["id"] for node in data["nodes"] if node["degree"] == 0]
    return {
        "nodes": len(data["nodes"]),
        "edges": len(data["edges"]),
        "by_kind": counts,
        "isolated": isolated,
    }
