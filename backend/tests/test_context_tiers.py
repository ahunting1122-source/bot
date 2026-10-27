"""三层 prompt 装配（Stable / Context / Volatile）的契约测试。

对齐 Hermes 的 `agent/prompt_builder.py`。三层顺序本身就是设计：越靠前越稳定，
提示缓存能命中的前缀就越长。所以这里既测"内容在不在"，也测**顺序**——
顺序错了缓存不命中，功能看起来却完全正常，是最难发现的那类退化。
"""

from __future__ import annotations

from app.config import Settings
from app.context import (
    build_context,
    build_context_tier,
    build_stable,
    build_volatile_tier,
    load_project_context,
    load_soul,
)
from app.main import migrate
from app.storage import Conversation, Message, Store, Task


async def _build(tmp_path, *, soul: str = "", project: str = "", memory: bool = True):
    data = tmp_path / "data"
    settings = Settings(data_dir=data, workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    if soul:
        (data / "SOUL.md").write_text(soul, encoding="utf-8")
    store = Store(data / "app.sqlite")
    conversation = await store.save(Conversation())
    await store.save(Message(conversation_id=conversation.id, role="user", content="你好"))
    task = await store.save(Task(conversation_id=conversation.id, goal="打个招呼"))
    if memory:
        # 造一条长期记忆：没有记忆时记忆块**整段不出现**（不拿空标签占预算），
        # 所以凡是要断言"记忆块在"的用例都得先有一条。
        from app.storage import Memory

        await store.save(Memory(key="报告格式偏好", value="中文 Markdown"))
    return settings, store, task


# ── SOUL.md ─────────────────────────────────────────────────────────────────


def test_soul_is_empty_when_absent(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    assert load_soul(settings) == ""


def test_soul_is_loaded_from_the_data_dir(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "SOUL.md").write_text("我是 Orbit。", encoding="utf-8")
    settings = Settings(data_dir=data, workspace=tmp_path / "files")
    assert load_soul(settings) == "我是 Orbit。"


async def test_soul_comes_first_in_the_system_prompt(tmp_path):
    """人格在最前面、行为规则在它后面：冲突时规则能压住人格，反过来没有兜底。"""
    settings, store, task = await _build(tmp_path, soul="人格：说话简短。")
    messages = await build_context(store, task, settings)
    prompt = str(messages[0].content)
    assert prompt.startswith("人格：说话简短。")
    # agent.md 的第一句必须排在 SOUL 之后。
    assert prompt.index("人格：说话简短。") < prompt.index("你是 Orbit")
    await store.engine.dispose()


# ── 项目上下文 ──────────────────────────────────────────────────────────────


def test_project_context_walks_up_and_stops_at_git_root(tmp_path):
    root = tmp_path / "repo"
    nested = root / "a" / "b"
    nested.mkdir(parents=True)
    (root / ".git").mkdir()
    (root / "AGENTS.md").write_text("项目规则：用中文。", encoding="utf-8")
    text, path = load_project_context(nested)
    assert text == "项目规则：用中文。"
    assert path == str(root / "AGENTS.md")


def test_project_context_prefers_agents_md_over_orbit_md(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "AGENTS.md").write_text("agents", encoding="utf-8")
    (root / "ORBIT.md").write_text("orbit", encoding="utf-8")
    text, _path = load_project_context(root)
    assert text == "agents"


def test_project_context_returns_empty_when_nothing_found(tmp_path):
    empty = tmp_path / "nothing"
    empty.mkdir()
    assert load_project_context(empty) == ("", "")


async def test_project_context_lands_between_stable_and_memory(tmp_path):
    settings, store, task = await _build(tmp_path)
    # 直接把项目文件写进 cwd 的上级链会污染别的测试，所以只验证分层函数本身。
    stable = build_stable(settings)
    context_block = build_context_tier(settings, memory_text="键: 值", profile_block="")
    assert "你是 Orbit" in stable
    assert "键: 值" in context_block
    # 分层函数的产物里不该出现 volatile 内容。
    assert "权限级别" not in context_block
    await store.engine.dispose()


# ── 三层顺序 ────────────────────────────────────────────────────────────────


async def test_tiers_appear_in_stable_context_volatile_order(tmp_path):
    settings, store, task = await _build(tmp_path)
    messages = await build_context(store, task, settings)
    prompt = str(messages[0].content)
    # Stable（agent.md）→ Context（长期记忆）→ Volatile（权限级别 / 会话摘要）
    assert prompt.index("你是 Orbit") < prompt.index("长期记忆")
    assert prompt.index("长期记忆") < prompt.index("权限级别")
    assert prompt.index("权限级别") < prompt.index("当前会话摘要")
    await store.engine.dispose()


def test_permission_level_is_in_the_volatile_tier(tmp_path):
    """权限级别每轮都可能变。放进稳定层会让模型在切档后仍按旧边界行事。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    volatile = build_volatile_tier(settings, file_access="权限级别：仅工作文件夹。", summary="")
    assert "权限级别" in volatile
    assert "当前会话摘要" in volatile


async def test_prompt_still_carries_every_legacy_block(tmp_path):
    """回归：分层重构不许悄悄丢掉任何一段。"""
    settings, store, task = await _build(tmp_path)
    messages = await build_context(store, task, settings)
    prompt = str(messages[0].content)
    for expected in ("你是 Orbit", "权限级别", "已配置网页", "Notion页面", "长期记忆", "当前会话摘要"):
        assert expected in prompt, expected
    await store.engine.dispose()
