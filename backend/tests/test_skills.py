"""Skill 层（一期只读）测试：加载器、provider、skill_load 工具、prepare 注入。"""

from app.config import Settings
from app.main import migrate
from app.skills import catalog, load_skill, parse_skill, skill_summary
from app.storage import Conversation, Store, Task
from app.tools import ToolRegistry


def write_skill(root, name, body, triggers, scope="site", status=None):
    (root / name).mkdir(parents=True, exist_ok=True)
    trigger_lines = "".join(f"  - {t}\n" for t in triggers)
    status_line = f"status: {status}\n" if status else ""
    (root / name / "SKILL.md").write_text(
        f"---\nname: {name}\nscope: {scope}\nversion: 3\n{status_line}triggers:\n{trigger_lines}---\n{body}\n",
        encoding="utf-8",
    )


def test_parse_skill_splits_frontmatter_and_body():
    text = "---\nname: demo\nscope: site\nversion: 2\ntriggers:\n  - 搜索\n  - 筛选\n---\n# 正文\n步骤"
    skill = parse_skill(text, "demo")
    assert skill["name"] == "demo"
    assert skill["scope"] == "site"
    assert skill["version"] == 2
    assert skill["triggers"] == ["搜索", "筛选"]
    assert skill["body"].startswith("# 正文")


def test_load_skill_handles_missing_and_bad_files(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "ok", "正文内容", ["触发"])
    assert load_skill(root, "ok")["body"] == "正文内容"
    # .md 后缀可省略
    assert load_skill(root, "ok.md")["body"] == "正文内容"
    # 不存在返回空正文，不抛异常
    missing = load_skill(root, "nope")
    assert missing["name"] == "nope" and missing["body"] == ""
    # 空目录也不抛
    assert catalog(tmp_path / "empty") == []


def test_catalog_skips_bad_files_and_sorts(tmp_path):
    root = tmp_path / "skills"
    write_skill(root, "zeta", "正文", ["触发"])
    write_skill(root, "alpha", "正文", ["触发"])
    (root / "broken").mkdir(parents=True, exist_ok=True)  # 无 SKILL.md → 跳过
    names = [s["name"] for s in catalog(root)]
    assert names == ["alpha", "zeta"]


def test_skill_summary_uses_triggers():
    assert skill_summary({"name": "x", "triggers": ["搜索", "筛选"]}) == "x：搜索；筛选"
    assert skill_summary({"name": "y", "triggers": []}) == "y"


async def test_skill_load_tool_reads_disk(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="站内搜索"))
    write_skill(settings.data_dir / "skills", "web_search", "搜步骤", ["站内搜索"])
    registry = ToolRegistry(settings, store)
    result = await registry.execute(task.id, "s1", "skill_load", {"name": "web_search"})
    assert result["status"] == "ok"
    assert result["data"]["body"] == "搜步骤"
    # 不存在的技能报 error
    missing = await registry.execute(task.id, "s2", "skill_load", {"name": "ghost"})
    assert missing["status"] == "error"
    await store.engine.dispose()


async def test_related_skills_matches_by_trigger_substring(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    from app.graph.nodes import Nodes
    from app.policies import Policies

    write_skill(settings.data_dir / "skills", "web_search", "正文", ["站内搜索", "筛选"])
    write_skill(settings.data_dir / "skills", "bili_video_detail", "正文", ["视频详情"])
    policies = Policies.default(settings, store)

    class Runner:
        def __init__(self):
            self.settings = settings
            self.store = store
            self.policies = policies

    nodes = Nodes(Runner())
    # 命中"搜索"相关技能
    hits = await nodes.related_skills("在 B 站搜索机械键盘并排序")
    assert [s["name"] for s in hits] == ["web_search"]
    # 不相关目标不注入
    assert await nodes.related_skills("1+1 等于几") == []
    # site 优先于 global
    write_skill(settings.data_dir / "skills", "generic", "正文", ["搜索"], scope="global")
    hits = await nodes.related_skills("在站内搜索内容")
    scopes = [s["scope"] for s in hits]
    assert scopes[0] == "site"
    await store.engine.dispose()


def test_trigger_matches_requires_two_char_overlap():
    from app.graph.nodes import Nodes

    assert Nodes._trigger_matches("站内搜索", "在 B 站搜索机械键盘") is True
    assert Nodes._trigger_matches("视频详情", "点进视频详情页") is True
    assert Nodes._trigger_matches("搜索", "登录账号") is False


async def test_skill_provider_load_returns_body(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    write_skill(settings.data_dir / "skills", "web_search", "完整正文", ["搜索"])
    from app.policies import DefaultSkillProvider

    provider = DefaultSkillProvider(settings)
    assert provider.load("web_search") == "完整正文"
    assert [s["name"] for s in provider.catalog()] == ["web_search"]
    # 无 settings 时空转（不抛）
    empty = DefaultSkillProvider()
    assert empty.catalog() == [] and empty.load("x") == ""


# --- 二期过渡：建表、skill_save、表同步、usage_count ---


def test_save_skill_writes_file_and_rejects_bad_names(tmp_path):
    from app.skills import save_skill

    root = tmp_path / "skills"
    skill = save_skill(root, "my_flow", "site", ["触发A", "触发B"], "步骤正文")
    assert skill["name"] == "my_flow"
    assert skill["scope"] == "site"
    assert skill["version"] == 1
    # 文件落盘，frontmatter 带 status=unreviewed
    text = (root / "my_flow" / "SKILL.md").read_text(encoding="utf-8")
    assert "status: unreviewed" in text
    assert "步骤正文" in text
    # 目录穿越 / 非法名被拒
    import pytest

    with pytest.raises(ValueError):
        save_skill(root, "../evil", "global", [], "x")
    with pytest.raises(ValueError):
        save_skill(root, "a b", "global", [], "x")
    # 空正文被拒
    with pytest.raises(ValueError):
        save_skill(root, "empty", "global", [], "  ")


async def test_skill_save_tool_writes_and_syncs(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="沉淀流程"))
    registry = ToolRegistry(settings, store)
    result = await registry.execute(
        task.id, "s1", "skill_save",
        {"name": "new_flow", "scope": "site", "triggers": ["登录"], "body": "做法"},
    )
    assert result["status"] == "ok"
    assert result["data"]["saved"] == "new_flow"
    assert result["data"]["status"] == "unreviewed"
    # 已同步进 skills 表
    from sqlalchemy import select

    from app.storage import Skill

    rows = await store.rows(select(Skill).where(Skill.name == "new_flow"))
    assert len(rows) == 1
    assert rows[0].scope == "site"
    assert rows[0].status == "unreviewed"  # skill_save 落盘标 unreviewed，镜像后保持待确认
    await store.engine.dispose()


async def test_sync_to_store_upserts_and_archives(tmp_path):
    from sqlalchemy import select

    from app.skills import sync_to_store
    from app.storage import Skill

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    root = settings.data_dir / "skills"
    write_skill(root, "keep", "正文", ["触发"])
    write_skill(root, "drop", "正文", ["触发"])
    # 首次同步：两行都 active
    assert await sync_to_store(store, root) == 2
    # 删掉 drop 文件再同步 → drop 变 archived，keep 仍 active
    (root / "drop" / "SKILL.md").unlink()
    (root / "drop").rmdir()
    await sync_to_store(store, root)
    rows = {r.name: r for r in await store.rows(select(Skill))}
    assert rows["keep"].status == "active"
    assert rows["drop"].status == "archived"
    await store.engine.dispose()


async def test_skill_load_increments_usage_count(tmp_path):
    from sqlalchemy import select

    from app.storage import Skill

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="搜索"))
    write_skill(settings.data_dir / "skills", "web_search", "正文", ["搜索"])
    registry = ToolRegistry(settings, store)
    await registry.execute(task.id, "s1", "skill_load", {"name": "web_search"})
    await registry.execute(task.id, "s2", "skill_load", {"name": "web_search"})
    rows = await store.rows(select(Skill).where(Skill.name == "web_search"))
    assert rows[0].usage_count == 2
    await store.engine.dispose()


async def test_skill_list_tool_returns_names(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="列技能"))
    write_skill(settings.data_dir / "skills", "web_search", "正文", ["搜索"])
    write_skill(settings.data_dir / "skills", "bili_video_detail", "正文", ["视频"])
    registry = ToolRegistry(settings, store)
    result = await registry.execute(task.id, "s1", "skill_list", {})
    assert result["status"] == "ok"
    names = [s["name"] for s in result["data"]]
    assert names == ["bili_video_detail", "web_search"]
    await store.engine.dispose()


# --- 二期：治理四道闸 ---


async def test_sync_to_store_does_not_resurrect_stale(tmp_path):
    """治理态 stale 不被文件声明 active 静默复活（文件 version 未递增时）。"""
    from sqlalchemy import select

    from app.skills import sync_to_store
    from app.storage import Skill

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    root = settings.data_dir / "skills"
    write_skill(root, "web_search", "正文", ["搜索"], status="active")  # 声明 active（手写常态）
    await sync_to_store(store, root)
    # 治理标 stale（模拟 record_outcome 降权）
    async with store.lock, store.sessions() as session:
        row = await session.scalar(select(Skill).where(Skill.name == "web_search"))
        row.status = "stale"
        await session.commit()
    # 再同步：文件声明 active 但 version 未递增 → 保留 stale，不复活
    await sync_to_store(store, root)
    row = (await store.rows(select(Skill).where(Skill.name == "web_search")))[0]
    assert row.status == "stale"
    await store.engine.dispose()


async def test_sync_to_store_resurrects_when_version_bumps(tmp_path):
    """治理态 stale 只有文件 version 递增（用户真实改过）才复活。"""
    from sqlalchemy import select

    from app.skills import sync_to_store
    from app.storage import Skill

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    root = settings.data_dir / "skills"
    write_skill(root, "web_search", "正文", ["搜索"], status="active")  # version=3
    await sync_to_store(store, root)
    async with store.lock, store.sessions() as session:
        row = await session.scalar(select(Skill).where(Skill.name == "web_search"))
        row.status = "stale"
        await session.commit()
    # 用户改文件：version 3 → 4，且声明 active
    write_skill(root, "web_search", "正文改", ["搜索"], status="active")
    (root / "web_search" / "SKILL.md").write_text(
        "---\nname: web_search\nscope: site\nversion: 4\nstatus: active\ntriggers:\n  - 搜索\n---\n正文改\n",
        encoding="utf-8",
    )
    await sync_to_store(store, root)  # version 递增 → 复活
    row = (await store.rows(select(Skill).where(Skill.name == "web_search")))[0]
    assert row.status == "active"
    assert row.version == 4
    await store.engine.dispose()


async def test_sync_to_store_promotes_unreviewed_to_active(tmp_path):
    """人工确认：unreviewed → active（不需 version 递增，这是核心确认场景）。"""
    from sqlalchemy import select

    from app.skills import sync_to_store
    from app.storage import Skill

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    root = settings.data_dir / "skills"
    write_skill(root, "web_search", "正文", ["搜索"], status="unreviewed")
    await sync_to_store(store, root)
    row = (await store.rows(select(Skill).where(Skill.name == "web_search")))[0]
    assert row.status == "unreviewed"
    # 用户把 unreviewed 改成 active（同 version，只改 status 字段）
    write_skill(root, "web_search", "正文", ["搜索"], status="active")
    await sync_to_store(store, root)
    row = (await store.rows(select(Skill).where(Skill.name == "web_search")))[0]
    assert row.status == "active"
    await store.engine.dispose()


async def test_find_conflict_by_trigger_overlap():
    from app.skills import find_conflict

    existing = [
        {"name": "web_search", "scope": "site", "triggers": ["站内搜索", "筛选"]},
        {"name": "other", "scope": "site", "triggers": ["视频详情"]},
        {"name": "global_x", "scope": "global", "triggers": ["站内搜索"]},
    ]
    # 同 scope + 触发词"搜索"重叠 → 命中 web_search
    hits = find_conflict(existing, "site", ["搜索某商品"])
    assert [h["name"] for h in hits] == ["web_search"]
    # 不同 scope 不命中
    assert find_conflict(existing, "project", ["搜索某商品"]) == []
    # 无重叠不命中
    assert find_conflict(existing, "site", ["登录账号"]) == []


async def test_skill_save_updates_existing_same_name(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="沉淀"))
    write_skill(settings.data_dir / "skills", "web_search", "旧正文", ["搜索"])
    registry = ToolRegistry(settings, store)
    result = await registry.execute(
        task.id, "s1", "skill_save",
        {"name": "web_search", "scope": "site", "triggers": ["搜索"], "body": "新正文补充"},
    )
    assert result["status"] == "ok"
    assert result["data"]["updated"] is True
    assert result["data"]["version"] >= 4  # 原 version 3 + 1
    # 正文合并了新旧
    text = (settings.data_dir / "skills" / "web_search" / "SKILL.md").read_text(encoding="utf-8")
    assert "旧正文" in text and "新正文补充" in text
    await store.engine.dispose()


async def test_skill_save_marks_alternatives_on_conflict(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="沉淀"))
    write_skill(settings.data_dir / "skills", "web_search", "正文", ["站内搜索"])
    registry = ToolRegistry(settings, store)
    result = await registry.execute(
        task.id, "s1", "skill_save",
        {"name": "alt_search", "scope": "site", "triggers": ["站内搜索商品"], "body": "另一种做法"},
    )
    assert result["status"] == "ok"
    assert result["data"]["alternatives"] == ["web_search"]
    # 新技能与旧技能都标了「备选」
    new_text = (settings.data_dir / "skills" / "alt_search" / "SKILL.md").read_text(encoding="utf-8")
    old_text = (settings.data_dir / "skills" / "web_search" / "SKILL.md").read_text(encoding="utf-8")
    assert "备选：web_search" in new_text
    assert "备选：alt_search" in old_text
    await store.engine.dispose()


async def test_related_skills_filters_unreviewed_and_archived(tmp_path):
    from app.graph.nodes import Nodes
    from app.policies import Policies

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    write_skill(settings.data_dir / "skills", "good", "正文", ["搜索"])
    write_skill(settings.data_dir / "skills", "pending", "正文", ["搜索"], status="unreviewed")
    policies = Policies.default(settings, store)

    class Runner:
        def __init__(self):
            self.settings = settings
            self.store = store
            self.policies = policies

    nodes = Nodes(Runner())
    hits = await nodes.related_skills("搜索内容")
    names = [s["name"] for s in hits]
    assert "good" in names
    assert "pending" not in names  # unreviewed 不注入
    await store.engine.dispose()


async def test_related_skills_same_cluster_only_champion(tmp_path):
    """同簇（互标备选）只注入排序冠军。"""
    from app.graph.nodes import Nodes
    from app.policies import Policies

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    write_skill(settings.data_dir / "skills", "alpha", "正文", ["站内搜索", "备选：beta"])
    write_skill(settings.data_dir / "skills", "beta", "正文", ["站内搜索", "备选：alpha"])
    policies = Policies.default(settings, store)

    class Runner:
        def __init__(self):
            self.settings = settings
            self.store = store
            self.policies = policies

    nodes = Nodes(Runner())
    hits = await nodes.related_skills("站内搜索内容")
    names = [s["name"] for s in hits]
    # 两者同簇，只出冠军（排序靠前的那个）
    assert len(names) == 1
    assert names[0] == "alpha"
    await store.engine.dispose()


async def test_record_outcome_marks_stale_after_consecutive_failures(tmp_path):
    from sqlalchemy import select

    from app.policies import Policies
    from app.storage import Skill

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    write_skill(settings.data_dir / "skills", "web_search", "正文", ["搜索"])
    policies = Policies.default(settings, store)
    # 模拟两次失败
    await policies.skills.record_outcome(store, "FAILED", ["web_search"])
    await policies.skills.record_outcome(store, "FAILED", ["web_search"])
    row = (await store.rows(select(Skill).where(Skill.name == "web_search")))[0]
    assert row.consecutive_failures == 2
    assert row.status == "stale"
    await store.engine.dispose()


async def test_record_outcome_resets_on_success(tmp_path):
    from sqlalchemy import select

    from app.policies import Policies
    from app.storage import Skill

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    write_skill(settings.data_dir / "skills", "web_search", "正文", ["搜索"])
    policies = Policies.default(settings, store)
    await policies.skills.record_outcome(store, "FAILED", ["web_search"])
    await policies.skills.record_outcome(store, "SUCCEEDED", ["web_search"])
    row = (await store.rows(select(Skill).where(Skill.name == "web_search")))[0]
    assert row.consecutive_failures == 0
    assert row.status == "active"
    await store.engine.dispose()


async def test_record_outcome_ignores_unused_skills(tmp_path):
    """未被 skill_load 的技能不记账。"""
    from sqlalchemy import select

    from app.policies import Policies
    from app.skills import sync_to_store
    from app.storage import Skill

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    write_skill(settings.data_dir / "skills", "web_search", "正文", ["搜索"])
    await sync_to_store(store, settings.data_dir / "skills")  # 先让行存在
    policies = Policies.default(settings, store)
    await policies.skills.record_outcome(store, "FAILED", [])  # 空列表：未用任何技能
    row = (await store.rows(select(Skill).where(Skill.name == "web_search")))[0]
    assert row.failures == 0
    assert row.consecutive_failures == 0
    await store.engine.dispose()


async def test_skill_save_reactivates_archived_skill(tmp_path):
    """文件删了归档后，重新 skill_save 同名技能应重新进入 unreviewed（不保持 archived）。"""
    from sqlalchemy import select

    from app.skills import sync_to_store
    from app.storage import Skill

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    root = settings.data_dir / "skills"
    write_skill(root, "web_search", "正文", ["搜索"])
    await sync_to_store(store, root)
    # 删文件 → archived
    (root / "web_search" / "SKILL.md").unlink()
    (root / "web_search").rmdir()
    await sync_to_store(store, root)
    row = (await store.rows(select(Skill).where(Skill.name == "web_search")))[0]
    assert row.status == "archived"
    # 重新 skill_save 同名 → 应重新进入 unreviewed
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="重新沉淀"))
    registry = ToolRegistry(settings, store)
    result = await registry.execute(
        task.id, "s1", "skill_save",
        {"name": "web_search", "scope": "site", "triggers": ["搜索"], "body": "重新沉淀的正文"},
    )
    assert result["status"] == "ok"
    row = (await store.rows(select(Skill).where(Skill.name == "web_search")))[0]
    assert row.status == "unreviewed"  # 重新沉淀，不再是 archived
    await store.engine.dispose()


# --- 阶段 B：多源合并与 (name, source) 唯一性 -------------------------------


async def test_catalog_with_stats_merges_mcp_prompt_source(tmp_path):
    """第二来源（MCP prompts）与本地文件技能在 catalog_with_stats 里合并，且带 source。"""
    from app.policies import DefaultSkillProvider

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    root = settings.data_dir / "skills"
    write_skill(root, "web_search", "本地正文", ["站内搜索"])

    provider = DefaultSkillProvider(settings)
    provider.prompt_source = lambda: [
        {
            "name": "mcp__rich__summarize",
            "scope": "global",
            "triggers": ["Summarize a document"],
            "status": "active",
            "source": "mcp:rich",
            "body": "请总结成三条要点",
            "usage_count": 0,
            "success_rate": 0.0,
            "consecutive_failures": 0,
            "last_used_at": "",
        }
    ]
    merged = await provider.catalog_with_stats(store, root)
    names = {s["name"] for s in merged}
    assert "web_search" in names
    assert "mcp__rich__summarize" in names
    by_name = {s["name"]: s for s in merged}
    assert by_name["web_search"]["source"] == "local"
    assert by_name["mcp__rich__summarize"]["source"] == "mcp:rich"
    await store.engine.dispose()


async def test_skills_table_allows_same_name_different_source(tmp_path):
    """(name, source) 复合唯一：同名的本地技能与外部技能可以共存，不撞车。"""
    from sqlalchemy import select

    from app.storage import Skill

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    await store.save(Skill(name="dup", source="local", scope="global", triggers=[], body="本地"))
    await store.save(Skill(name="dup", source="mcp:rich", scope="global", triggers=[], body="外部"))
    rows = await store.rows(select(Skill).where(Skill.name == "dup"))
    assert len(rows) == 2
    assert {r.source for r in rows} == {"local", "mcp:rich"}
    await store.engine.dispose()
