"""长期记忆渲染的契约测试（`app/memory_prompt.py`）。

对齐 Hermes 的 `MEMORY.md`（2,200 字符上限、按字符不按 token、标题行自曝用量）。
重点在**裁剪方向**与**如实报数**：

- 超预算时从**最旧**开始丢：刚记住的东西第一个消失，用户会以为"记忆没生效"
- 丢了却不报数量，是静默丢数据——比报错更糟

另外锁一条"空记忆不占预算"：没有记忆时整段不出现，而不是留一个空标签。
"""

from __future__ import annotations

from app import memory_prompt as mp


def test_empty_memory_renders_nothing():
    """没有记忆就整段不出现——空标签会白占提示词预算。"""
    block = mp.render([])
    assert block.text == ""
    assert block.used == 0
    assert mp.render_block([]) == ""


def test_header_reports_usage_and_percent():
    block = mp.render([("报告格式", "中文 Markdown")])
    first = block.text.splitlines()[0]
    assert first.startswith("长期记忆（MEMORY）[")
    assert f"/{mp.MEMORY_MAX_CHARS} 字符]" in first
    assert "%" in first
    assert "- 报告格式: 中文 Markdown" in block.text
    assert block.used <= mp.MEMORY_MAX_CHARS


def test_empty_key_is_dropped():
    block = mp.render([("", "孤立的值"), ("  ", "又一个")])
    assert block.text == ""


def test_long_value_is_truncated():
    block = mp.render([("长条目", "x" * 2000)])
    assert "…" in block.text
    # 单条 value 不能把整块预算吃光。
    assert block.text.count("x") <= mp.VALUE_LIMIT + 10


def test_whitespace_is_collapsed():
    block = mp.render([("键", "  多行\n\n  内容   ")])
    assert "- 键: 多行 内容" in block.text


def test_over_budget_drops_the_oldest_and_reports_the_count():
    pairs = [(f"第{i}条", "内容" * 40) for i in range(40)]
    block = mp.render(pairs, limit=mp.MEMORY_MAX_CHARS)
    assert block.used <= mp.MEMORY_MAX_CHARS
    assert block.dropped > 0
    assert block.total == 40
    # 丢的是**最旧的**：第一条不该在，最后一条该在。
    assert "- 第0条:" not in block.text
    assert f"- 第{39}条:" in block.text
    # 丢了必须报出来，不许静默。
    assert f"因预算省略 {block.dropped} 条" in block.text


def test_budget_is_respected_exactly():
    """`used` 是真实字符数，不是估算——超一字符就是超。"""
    pairs = [(f"k{i}", "v" * 50) for i in range(60)]
    block = mp.render(pairs, limit=1200)
    assert len(block.text) == block.used
    assert block.used <= 1200


def test_single_oversized_entry_still_fits_the_budget():
    """只剩一条也必须装得下：不能因为"至少留一条"就把预算撑破。"""
    block = mp.render([("唯一", "y" * 5000)], limit=600)
    assert block.used <= 600


def test_percent_is_rounded_for_display():
    block = mp.render([("k", "v" * 1000)])
    assert 0 <= block.percent <= 100
    assert f"[{block.percent}%" in block.text
