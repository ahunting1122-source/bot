"""用户画像层：独立于 memories 的整段文档注入。

背景：原先只有 `memories` 一个 key-value 事实层，于是"报告格式偏好"（事实/偏好）
和"用户在做什么项目、习惯怎么协作"（画像）挤在同一张表里。Hermes 把 recall 与
user modeling 明确拆开，本文件锁住拆开后的行为：

  1. 条目化（`§` 分隔）与去重；
  2. 容量封顶，超限**丢最旧**而不是丢最新；
  3. 会话内冻结快照（改画像不改当前提示词，下次生效）；
  4. 画像与记忆是两个不同的注入块，互不覆盖。
"""

import pytest

from app import profile
from app.config import Settings
from app.context import build_context
from app.main import migrate
from app.storage import Conversation, Store, Task


def _settings(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    return settings


# ── 条目化 ──────────────────────────────────────────────────────────────────


def test_split_entries_accepts_both_separator_styles():
    body = "§ 在做水果拟人短剧\n§ 先要脚本再要分镜\n- 习惯用中文交流"
    entries = profile.split_entries(body)
    assert entries == ["在做水果拟人短剧", "先要脚本再要分镜", "习惯用中文交流"]


def test_split_entries_drops_headings_and_deduplicates():
    body = "# 用户画像\n§ 喜欢简洁说明\n§ 喜欢简洁说明。\n§ 不要啰嗦"
    entries = profile.split_entries(body)
    # 标点差异算同一条（归一化时被折叠），所以只留第一次出现的那条
    assert entries == ["喜欢简洁说明", "不要啰嗦"]


def test_split_entries_of_empty_body_is_empty():
    assert profile.split_entries("") == []
    assert profile.split_entries("   \n\n") == []
    assert profile.split_entries("# 只有标题\n") == []


# ── 容量封顶 ────────────────────────────────────────────────────────────────


def test_trim_drops_the_oldest_entries_first():
    """画像里后写的通常更贴近当前状态，所以超限丢最旧的。"""
    entries = ["旧" * 400, "中" * 400, "新" * 400]
    kept, dropped = profile.trim(entries, limit=1000)
    assert "新" * 400 in kept and "旧" * 400 in dropped


def test_save_beyond_limit_keeps_the_newest_and_reports_dropped(tmp_path):
    settings = _settings(tmp_path)
    body = "\n".join(f"{profile.SEPARATOR} {'旧' * 200} {index}" for index in range(20))
    result = profile.save(settings, body)
    assert result["chars"] <= profile.PROFILE_MAX_CHARS
    assert result["dropped"], "超限必须如实回报丢了哪些条目"
    assert "旧" * 200 + " 19" in result["body"], "最新一条必须在"


# ── 增删 ────────────────────────────────────────────────────────────────────


def test_append_is_idempotent_for_the_same_entry(tmp_path):
    settings = _settings(tmp_path)
    first = profile.append(settings, "在做水果拟人短剧")
    assert first["added"] is True
    second = profile.append(settings, "在做水果拟人短剧。")
    assert second["added"] is False and second["duplicate"] == "在做水果拟人短剧。"
    assert profile.load(settings)["entries"] == ["在做水果拟人短剧"]


def test_append_rejects_blank_entry(tmp_path):
    settings = _settings(tmp_path)
    with pytest.raises(ValueError):
        profile.append(settings, "   ")


def test_remove_deletes_matching_entry(tmp_path):
    settings = _settings(tmp_path)
    profile.append(settings, "在做水果拟人短剧")
    profile.append(settings, "习惯用中文交流")
    result = profile.remove(settings, "水果拟人")
    assert result["removed"] == ["在做水果拟人短剧"]
    assert profile.load(settings)["entries"] == ["习惯用中文交流"]


def test_load_without_file_returns_the_default_skeleton(tmp_path):
    """没文件时 `body` 给骨架占位，但 `entries` 必须是空的。

    骨架是给人看的占位文案，不是真实画像条目。若把它当条目回给前端，
    新用户的画像页会凭空多出两条"还不知道这位用户是谁"，计数也是错的。
    """
    settings = _settings(tmp_path)
    result = profile.load(settings)
    assert result["exists"] is False
    assert result["body"]  # 占位文案仍在，用户看得到"这里该写点什么"
    assert result["entries"] == []
    # 读操作不该有副作用
    assert not (settings.data_dir / "profile" / "USER.md").exists()


# ── 注入 ────────────────────────────────────────────────────────────────────


def test_render_block_is_empty_for_an_empty_profile(tmp_path):
    settings = _settings(tmp_path)
    profile.save(settings, "# 用户画像\n")
    assert profile.render_block(settings) == ""


def test_render_block_lists_entries_under_a_data_warning(tmp_path):
    settings = _settings(tmp_path)
    profile.append(settings, "在做水果拟人短剧")
    block = profile.render_block(settings)
    assert "用户画像" in block and "数据，不是指令" in block
    assert "在做水果拟人短剧" in block


@pytest.mark.asyncio
async def test_build_context_injects_profile_and_memories_as_separate_blocks(tmp_path):
    """画像与记忆必须各自成块：混在一起会让模型分不清"这个人是谁"和"这条事实是什么"。"""
    settings = _settings(tmp_path)
    profile.append(settings, "在做水果拟人短剧账号")
    store = Store(settings.data_dir / "app.sqlite")
    await store.set_memory("报告格式偏好", "中文 Markdown")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="写个报告"))
    context = await build_context(store, task, settings)
    prompt = context[0].content
    assert "用户画像" in prompt and "在做水果拟人短剧账号" in prompt
    assert "长期记忆" in prompt and "报告格式偏好: 中文 Markdown" in prompt
    assert prompt.index("用户画像") < prompt.index("长期记忆")
    await store.engine.dispose()


@pytest.mark.asyncio
async def test_build_context_omits_profile_block_when_empty(tmp_path):
    settings = _settings(tmp_path)
    profile.save(settings, "# 用户画像\n")
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="随便聊聊"))
    context = await build_context(store, task, settings)
    assert "用户画像" not in context[0].content
    await store.engine.dispose()


@pytest.mark.asyncio
async def test_editing_the_profile_does_not_change_an_existing_task_messages(tmp_path):
    """会话内冻结：已装配好的 messages 不受后续画像改动影响（下次会话才生效）。"""
    settings = _settings(tmp_path)
    profile.append(settings, "第一条画像")
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="干活"))
    frozen = await build_context(store, task, settings)
    body_before = frozen[0].content
    profile.append(settings, "第二条画像")
    # 冻结的是那份 messages 对象，重新 build 才会看到新条目
    assert "第二条画像" not in body_before
    fresh = await build_context(store, task, settings)
    assert "第二条画像" in fresh[0].content
    await store.engine.dispose()


# ── HTTP 契约（前端长期记忆页依赖） ─────────────────────────────────────────


def test_profile_endpoints_round_trip(tmp_path):
    """`GET/PUT /api/profile` 是画像页的读写路径，字段名必须稳住。

    前端读 `body` / `limit` / `exists`，保存时用 `dropped` 提示"丢了几条"。
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = _settings(tmp_path)
    with TestClient(create_app(settings, run_agent=False)) as client:
        client.get("/api/session")
        empty = client.get("/api/profile").json()
        assert empty["exists"] is False
        assert empty["entries"] == []
        assert empty["limit"] > 0

        saved = client.put(
            "/api/profile", json={"body": "§ 习惯用中文\n§ 项目在 E://bbbooottt"}
        ).json()
        assert saved["exists"] is True
        assert saved["entries"] == ["习惯用中文", "项目在 E://bbbooottt"]
        assert saved["dropped"] == []

        # 重开应用仍是同一份（文件是真值，不是进程内存）
        with TestClient(create_app(settings, run_agent=False)) as again:
            again.get("/api/session")
            reloaded = again.get("/api/profile").json()
            assert reloaded["entries"] == saved["entries"]


def test_profile_entries_endpoint_reports_duplicates(tmp_path):
    """重复追加要如实回报 `added=False`，前端才能提示"这条已经存在"。"""
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = _settings(tmp_path)
    with TestClient(create_app(settings, run_agent=False)) as client:
        client.get("/api/session")
        first = client.post("/api/profile/entries", json={"entry": "偏好简洁说明"}).json()
        assert first["added"] is True
        again = client.post("/api/profile/entries", json={"entry": "偏好简洁说明"}).json()
        assert again["added"] is False
        assert "已经存在" in again["notice"]

        removed = client.delete("/api/profile/entries", params={"entry": "偏好简洁说明"}).json()
        assert removed["removed"] == ["偏好简洁说明"]
        missing = client.delete("/api/profile/entries", params={"entry": "不存在的一条"}).json()
        assert missing["removed"] == []
        assert "没有找到" in missing["notice"]


def test_memories_endpoint_exposes_usage_for_dead_data_cleanup(tmp_path):
    """`/api/memories` 必须带 `hit_count` / `last_used_at`——前端靠它判定死数据。

    没有这两个字段，记忆页就无法回答"哪条记忆从来没被用过"，只能靠肉眼猜。
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    settings = _settings(tmp_path)
    with TestClient(create_app(settings, run_agent=False)) as client:
        client.get("/api/session")
        row = client.post(
            "/api/memories", json={"key": "报告格式", "value": "中文 Markdown"}
        ).json()
        assert row["key"] == "报告格式"
        assert row["hit_count"] == 0
        assert row["last_used_at"] == ""
        assert "updated_at" in row

        listed = client.get("/api/memories").json()
        assert {"hit_count", "last_used_at", "source"} <= set(listed[0])
