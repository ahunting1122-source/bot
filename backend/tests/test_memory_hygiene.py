"""记忆层的卫生保障：同义 key 合并、可观测字段、模糊删除。

背景：真库抽查发现 `memories` 表 6 条里 5 条是同一条信息的近重复——模型给同一条
信息起了 7 种 key（含把整个句子当 key、把 run_id 拼进 key），`key` 唯一约束形同虚设。
这些用例锁住修复：
  1. `synonym_key` 能认出同义 key，且**不会误合并**语义不同的事实；
  2. `set_memory` 命中同义 key 时更新原条目而不是新增；
  3. 注入上下文会累加 `hit_count` / `last_used_at`（"哪条记忆是死数据"的唯一依据）；
  4. `memory_delete` 支持部分匹配（否则模型一字不差复述 key 才能删，实际做不到）。
"""

import pytest
from sqlalchemy import select

from app.config import Settings
from app.main import migrate
from app.storage import Conversation, Memory, Store, Task, synonym_key


def _store(tmp_path):
    """建一个跑过迁移的真库（`set_memory` 需要 memories 表存在）。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    return settings, Store(settings.data_dir / "app.sqlite")


# ── synonym_key：纯函数，不需数据库 ─────────────────────────────────────────


def test_synonym_key_matches_identical_after_normalizing():
    assert synonym_key("报告 格式 偏好", ["报告格式偏好"]) == "报告格式偏好"
    assert synonym_key("ReportFormat", ["reportformat"]) == "reportformat"


def test_synonym_key_does_not_merge_unrelated_facts():
    assert synonym_key("默认语言", ["报告格式偏好"]) == ""
    assert synonym_key("报告格式偏好", ["默认语言", "工作目录"]) == ""


def test_synonym_key_empty_candidate_is_safe():
    assert synonym_key("", ["报告格式偏好"]) == ""
    assert synonym_key("   ", ["报告格式偏好"]) == ""


# ── set_memory ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_set_memory_updates_synonymous_key_instead_of_duplicating(tmp_path):
    _, store = _store(tmp_path)

    await store.set_memory("报告格式偏好", "中文 Markdown")
    memory, merged_from = await store.set_memory("报告 格式 偏好", "中文 Markdown 格式")

    rows = await store.rows(select(Memory))
    assert len(rows) == 1, "同义 key 应合并，而不是新增一条"
    assert memory.value == "中文 Markdown 格式"
    assert merged_from == "报告格式偏好"
    await store.engine.dispose()


@pytest.mark.asyncio
async def test_set_memory_exact_key_updates_and_reports_no_merge(tmp_path):
    _, store = _store(tmp_path)

    await store.set_memory("报告格式偏好", "第一版")
    memory, merged_from = await store.set_memory("报告格式偏好", "第二版")

    rows = await store.rows(select(Memory))
    assert len(rows) == 1
    assert memory.value == "第二版"
    assert merged_from == "", "精确命中不算合并"
    await store.engine.dispose()


@pytest.mark.asyncio
async def test_set_memory_keeps_distinct_facts_separate(tmp_path):
    _, store = _store(tmp_path)

    await store.set_memory("报告格式偏好", "中文 Markdown")
    await store.set_memory("默认语言", "中文")

    rows = await store.rows(select(Memory))
    assert len(rows) == 2, "不相关的事实不应被合并"
    await store.engine.dispose()


@pytest.mark.asyncio
async def test_new_memory_starts_with_zero_hits(tmp_path):
    _, store = _store(tmp_path)
    memory, _ = await store.set_memory("报告格式偏好", "中文 Markdown")
    assert memory.hit_count == 0
    assert memory.last_used_at == ""
    await store.engine.dispose()


# ── 注入统计：`build_context` 会累加 hit_count / last_used_at ────────────────


@pytest.mark.asyncio
async def test_build_context_records_memory_hits(tmp_path):
    from app.context import build_context

    settings, store = _store(tmp_path)
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="写报告"))
    await store.set_memory("报告格式偏好", "中文 Markdown")

    for _ in range(3):
        await build_context(store, task, settings)

    memory = (await store.rows(select(Memory)))[0]
    assert memory.hit_count == 3, "每次注入都应累加命中计数"
    assert memory.last_used_at, "命中后应记录上次使用时间"
    await store.engine.dispose()


@pytest.mark.asyncio
async def test_touch_memories_swallows_failure(tmp_path):
    """统计失败必须被吞掉——它是观测，不是主流程。

    直接测 `_touch_memories` 而不是打桩 `store.sessions`：后者会连带打断
    `build_context` 开头的 `store.get()`，测到的就不是统计这一环了。
    """
    from app.context import _touch_memories

    _, store = _store(tmp_path)
    await store.set_memory("报告格式偏好", "中文 Markdown")
    memories = await store.rows(select(Memory))

    class Boom:
        def __enter__(self):
            raise RuntimeError("模拟统计失败")

        def __exit__(self, *exc):
            return False

    store.sessions = lambda: Boom()
    await _touch_memories(store, memories)  # 不应抛出


@pytest.mark.asyncio
async def test_build_context_injects_memory_text(tmp_path):
    """记忆正文要真的进到系统提示里（合并/统计都不能影响这一点）。"""
    from app.context import build_context

    settings, store = _store(tmp_path)
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="写报告"))
    await store.set_memory("报告格式偏好", "中文 Markdown")

    messages = await build_context(store, task, settings)
    assert "报告格式偏好" in messages[0].content
    assert "中文 Markdown" in messages[0].content
    await store.engine.dispose()


# ── 模糊删除：`memory_delete` 支持部分匹配 ───────────────────────────────────


@pytest.mark.asyncio
async def test_memory_delete_matches_partial_key(tmp_path):
    from app.tools import ToolRegistry

    settings, store = _store(tmp_path)
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="整理"))
    registry = ToolRegistry(settings, store)

    await store.set_memory("报告格式偏好", "中文 Markdown")
    await store.set_memory("报告校验码", "09301811")
    await store.set_memory("默认语言", "中文")

    result = await registry.execute(task.id, "del1", "memory_delete", {"key": "报告"})
    keys = result["data"]["keys"]
    assert result["data"]["deleted"] == 2
    assert set(keys) == {"报告格式偏好", "报告校验码"}
    remaining = [m.key for m in await store.rows(select(Memory))]
    assert remaining == ["默认语言"], "不匹配的记忆必须保留"
    await store.engine.dispose()


@pytest.mark.asyncio
async def test_memory_delete_reports_when_nothing_matches(tmp_path):
    from app.tools import ToolRegistry

    settings, store = _store(tmp_path)
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="整理"))
    registry = ToolRegistry(settings, store)

    result = await registry.execute(task.id, "del1", "memory_delete", {"key": "不存在的东西"})
    assert result["data"]["deleted"] == 0
    assert "memory_search" in result["data"]["notice"]
    await store.engine.dispose()
