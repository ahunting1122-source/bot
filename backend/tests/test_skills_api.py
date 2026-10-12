"""技能操作面（阶段 A）测试：HTTP 契约 + 写入侧守门 + 归档判据修正。

覆盖三条线的正确性：
  1. 5 个端点（list / get / put / delete / status）的字段契约与状态机；
  2. A5 `_refuse_capture` 写入侧守门（五类禁止项 + 放行正例）；
  3. A3b 归档锚点：usage_count==0 是"缺乏证据"，不提前归档。
"""

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app, migrate


def _settings(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    return settings


# ── HTTP 契约 ──────────────────────────────────────────────────────────────


def test_skill_endpoints_round_trip(tmp_path):
    """新建 → 列表 unreviewed → 确认 active → 删除归档 → 恢复。"""
    settings = _settings(tmp_path)
    with TestClient(create_app(settings, run_agent=False)) as client:
        client.get("/api/session")

        # 空列表
        assert client.get("/api/skills").json() == []

        # 新建：默认 unreviewed
        created = client.put(
            "/api/skills/web_search",
            json={"body": "步骤正文", "scope": "site", "triggers": ["站内搜索"]},
        ).json()
        assert created["name"] == "web_search"
        assert created["status"] == "unreviewed"
        assert created["version"] == 1

        listing = client.get("/api/skills").json()
        assert listing[0]["name"] == "web_search"
        assert listing[0]["status"] == "unreviewed"
        assert listing[0]["usage_count"] == 0

        # 单个读取含正文
        single = client.get("/api/skills/web_search").json()
        assert single["body"] == "步骤正文"

        # 确认启用：unreviewed → active
        confirmed = client.post("/api/skills/web_search/status", json={"status": "active"}).json()
        assert confirmed["status"] == "active"
        assert client.get("/api/skills").json()[0]["status"] == "active"

        # 删除 → archived，列表消失，统计保留（表里仍是 archived，可恢复）
        deleted = client.delete("/api/skills/web_search").json()
        assert deleted["status"] == "archived"
        assert client.get("/api/skills").json() == []

        # 恢复：archived → active，文件重建
        restored = client.post("/api/skills/web_search/status", json={"status": "active"}).json()
        assert restored["status"] == "active"
        back = client.get("/api/skills").json()
        assert back[0]["name"] == "web_search"
        assert back[0]["status"] == "active"
        # 正文从表里保留的内容重建
        assert client.get("/api/skills/web_search").json()["body"] == "步骤正文"


async def test_skill_put_updates_and_revives_stale(tmp_path):
    """改正文保存 → version+1，且从 stale 恢复 active。"""
    settings = _settings(tmp_path)
    from sqlalchemy import select

    from app.skills import sync_to_store
    from app.storage import Skill, Store

    root = settings.data_dir / "skills"
    (root / "web_search").mkdir(parents=True, exist_ok=True)
    (root / "web_search" / "SKILL.md").write_text(
        "---\nname: web_search\nscope: site\nversion: 3\nstatus: active\ntriggers:\n  - 搜索\n---\n旧正文\n",
        encoding="utf-8",
    )
    store = Store(settings.data_dir / "app.sqlite")
    await sync_to_store(store, root)
    # 治理降权为 stale
    async with store.lock, store.sessions() as session:
        row = await session.scalar(select(Skill).where(Skill.name == "web_search"))
        row.status = "stale"
        await session.commit()
    await store.engine.dispose()

    with TestClient(create_app(settings, run_agent=False)) as client:
        client.get("/api/session")
        updated = client.put(
            "/api/skills/web_search", json={"body": "新正文"}
        ).json()
        assert updated["version"] == 4
        assert updated["status"] == "active"
        assert client.get("/api/skills").json()[0]["status"] == "active"


def test_skill_put_rejects_bad_name(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings, run_agent=False)) as client:
        client.get("/api/session")
        # `.` 不在技能名允许字符集内（字母数字下划线连字符），且不触发 URL 归一化
        resp = client.put("/api/skills/bad.name", json={"body": "x"})
        assert resp.status_code == 422


def test_skill_status_rejects_unknown_state(tmp_path):
    settings = _settings(tmp_path)
    with TestClient(create_app(settings, run_agent=False)) as client:
        client.get("/api/session")
        client.put("/api/skills/demo", json={"body": "正文"})
        resp = client.post("/api/skills/demo/status", json={"status": "bogus"})
        assert resp.status_code == 422


# ── A5 写入侧守门 ─────────────────────────────────────────────────────────


def test_refuse_capture_blocks_the_five_categories():
    from app.skills import _refuse_capture

    cases = [
        "缺少依赖：请先安装 numpy 才能跑",  # ① 环境故障
        "浏览器工具不能用，别调用它",  # ② 负面断言
        "重试后成功了，第一次是网络抖动",  # ③ 已解决的瞬时错误
        "今天的行情总结任务",  # ④ 一次性任务
        "试了三次还没解决，暂时没辙",  # ⑤ 未解决失败
    ]
    for body in cases:
        assert _refuse_capture(body), f"应该拒绝：{body!r}"


def test_refuse_capture_allows_a_legitimate_workflow():
    from app.skills import _refuse_capture

    good = (
        "站内搜索流程：先打开搜索页，输入关键词，再按时间排序。"
        "如果结果为空，用更短的关键词重试。"
    )
    assert _refuse_capture(good) == ""


async def test_skill_save_tool_rejects_refused_content(tmp_path):
    """被守门拦下的 skill_save 必须返回 error（不是被包成 ok）。"""
    from app.storage import Conversation, Store, Task
    from app.tools import ToolRegistry

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="沉淀"))
    registry = ToolRegistry(settings, store)
    result = await registry.execute(
        task.id, "s1", "skill_save",
        {"name": "bad_flow", "scope": "site", "triggers": ["x"], "body": "这个工具不能用，坏了"},
    )
    assert result["status"] == "error"
    assert "拒绝沉淀" in result["error"]
    await store.engine.dispose()


# ── A3b 归档判据 ──────────────────────────────────────────────────────────


async def test_record_outcome_never_archives_a_never_used_skill(tmp_path):
    """usage_count==0 是缺乏证据不是陈旧证据：刚沉淀未被用的技能不被归档。"""
    from sqlalchemy import select

    from app.policies import Policies
    from app.skills import sync_to_store
    from app.storage import Skill, Store

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    root = settings.data_dir / "skills"
    (root / "fresh").mkdir(parents=True, exist_ok=True)
    (root / "fresh" / "SKILL.md").write_text(
        "---\nname: fresh\nscope: site\nversion: 1\nstatus: active\ntriggers:\n  - 搜索\n---\n正文\n",
        encoding="utf-8",
    )
    await sync_to_store(store, root)
    policies = Policies.default(settings, store)
    # 即使任务失败，这个从未被使用的技能也不该被"超期"归档
    await policies.skills.record_outcome(store, "FAILED", ["fresh"])
    row = (await store.rows(select(Skill).where(Skill.name == "fresh")))[0]
    assert row.usage_count == 0
    assert row.status in ("active", "unreviewed")
    await store.engine.dispose()


async def test_record_outcome_archives_a_never_used_skill_only_by_created_at_age(tmp_path):
    """唯一允许 usage_count==0 归档的情况：创建本身已超期（created_at 距今 > 90 天）。"""
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import select

    from app.policies import Policies
    from app.skills import sync_to_store
    from app.storage import Skill, Store

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    root = settings.data_dir / "skills"
    (root / "old").mkdir(parents=True, exist_ok=True)
    (root / "old" / "SKILL.md").write_text(
        "---\nname: old\nscope: site\nversion: 1\nstatus: active\ntriggers:\n  - 搜索\n---\n正文\n",
        encoding="utf-8",
    )
    await sync_to_store(store, root)
    # 把创建时间拨回 100 天前，模拟"创建已久仍无人用"
    old_stamp = (datetime.now(UTC) - timedelta(days=100)).isoformat()
    async with store.lock, store.sessions() as session:
        row = await session.scalar(select(Skill).where(Skill.name == "old"))
        row.created_at = old_stamp
        await session.commit()

    policies = Policies.default(settings, store)
    await policies.skills.record_outcome(store, "FAILED", ["old"])
    row = (await store.rows(select(Skill).where(Skill.name == "old")))[0]
    assert row.status == "archived"
    await store.engine.dispose()
