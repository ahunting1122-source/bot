"""技能索引注入的契约测试（`app/graph/nodes.py::Nodes.render_skills`）。

为什么单独测这一小块：技能索引进的是**每一轮的系统提示**，它的体积是成本，
它的准确度是召回率。两个要求互相拉扯：

- 描述要给够，否则模型判断不出"该不该取全文"（召回失效）
- 每条又不能超 60 字符，否则技能一多就把提示词预算烧光，而且是正反馈——
  越相关的技能越多、越烧

Hermes 的取舍是「索引只放 name + 60 字符描述，正文靠按需 load」。这里锁死这个口径，
外加一条兜底：老技能没写 `description` 时要退回触发词，否则升级一次索引就把历史技能
全变成不可发现的。
"""

from __future__ import annotations

from app.graph.nodes import Nodes
from app.skill_index import INDEX_DESCRIPTION_LIMIT, index_description


def _nodes() -> Nodes:
    """`render_skills` 不用 `self`，给个 None runner 就够。"""
    return Nodes(None)  # type: ignore[arg-type]


def test_empty_skills_render_nothing():
    assert _nodes().render_skills([]) == ""


def test_description_is_used_and_truncated_to_the_index_budget():
    long_description = "这是一个非常长的技能描述，" * 10
    block = _nodes().render_skills(
        [{"name": "报告技能", "scope": "global", "description": long_description}]
    )
    line = block.splitlines()[1]
    assert "报告技能" in line
    # 描述部分不能超过索引预算（含截断省略号）。
    payload = line.split("：", 1)[1]
    assert len(payload) <= INDEX_DESCRIPTION_LIMIT


def test_short_description_is_kept_verbatim():
    block = _nodes().render_skills(
        [{"name": "小技能", "scope": "global", "description": "把周报转成要点"}]
    )
    assert "- 小技能（global）：把周报转成要点" in block


def test_missing_description_falls_back_to_triggers():
    """老技能（frontmatter 没写 description）仍要能被发现。"""
    block = _nodes().render_skills(
        [{"name": "老技能", "scope": "project", "triggers": ["周报", "汇总"]}]
    )
    assert "周报" in block and "汇总" in block


def test_no_description_and_no_triggers_still_lists_the_skill():
    block = _nodes().render_skills([{"name": "裸技能"}])
    assert "- 裸技能（global）" in block


def test_body_is_never_included():
    """索引里不许出现正文——那是 `skill_load` 的事。"""
    block = _nodes().render_skills(
        [
            {
                "name": "带正文",
                "scope": "global",
                "description": "短描述",
                "body": "BODY_MARKER_9F3A 这段正文不该出现在索引里",
            }
        ]
    )
    assert "BODY_MARKER_9F3A" not in block


def test_index_description_helper_is_the_single_source_of_truth():
    """注入侧与索引层用的是同一个截断函数——两处各写一份迟早会漂。"""
    long_text = "字" * 200
    assert index_description(long_text) == index_description(long_text)
    assert len(index_description(long_text)) <= INDEX_DESCRIPTION_LIMIT
