"""MATRIX 5.14 / 5.11：本轮新实现的两个工具（**真跑**）+ 分级披露的预算实测。

三段各管一件事，都不接受"注册成功"当证据：

1. **新工具**：`tool_describe` 与 `cron_job` 走一遍 `ToolRegistry.execute` 的真实分发链
   （不是直接调实现函数），并核对 schema 合法、归到了正确 toolset / 域；
2. **延迟披露（5.11）**：三档 token 实测（不裁剪 / 只压描述 / 延迟 Tier 2），以及
   "延迟之后到底进没进 4,000"这个**结论本身**；同时钉住"一个工具都不删、参数 schema
   只在被延迟的工具上动、Tier 0 逐字不变"；
3. **覆盖（5.14）**：`orbit_coverage()` 的 covered 必须真的涨了，且涨的那几个都对得上
   真实工具名（声明表说谎这条用例会红）。
"""

from __future__ import annotations

import json

import pytest

from app import tool_catalog, tool_extra_impl, toolsets
from app.config import Settings
from app.main import migrate
from app.storage import Conversation, Store, Task
from app.tools import ToolRegistry, schema_problems

# ── 夹具 ────────────────────────────────────────────────────────────────────


@pytest.fixture()
async def env(tmp_path):
    """隔离数据目录上的完整运行环境（settings + store + registry + task）。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="扩展工具"))
    settings.workspace.mkdir(parents=True, exist_ok=True)
    registry = ToolRegistry(settings, store)
    registered = tool_extra_impl.register_extra_tools(registry)
    try:
        yield settings, store, registry, task.id, registered
    finally:
        await store.engine.dispose()


# ── 1. 新工具：声明、schema、归类 ───────────────────────────────────────────


def test_extra_tools_have_valid_schemas_and_are_classified():
    """每个新工具都要：schema 合法（否则整次请求被 400）+ 有 toolset + 有功能域。"""
    assert [tool.name for tool in tool_extra_impl.EXTRA_TOOLS] == ["tool_describe", "cron_job"]
    for tool in tool_extra_impl.EXTRA_TOOLS:
        spec = tool.specification()
        assert schema_problems(spec) == [], f"{tool.name} 的 schema 不合法"
        assert toolsets.toolset_of(tool.name) != toolsets.UNCLASSIFIED, f"{tool.name} 没归类"
        assert toolsets.domain_of_tool(tool.name), f"{tool.name} 归不到功能域"
        assert tool.description.strip(), f"{tool.name} 缺描述（模型靠它决定调不调）"
    # 归类结果本身要有据可依：tool_describe 是桥（core/Tier 0），cron_job 属 cron 域。
    assert toolsets.toolset_of("tool_describe") == "core"
    assert toolsets.tier_of("tool_describe") == 0, "取参数表的桥自己不能被延迟"
    assert toolsets.toolset_of("cron_job") == "cron"
    assert toolsets.domain_of_tool("cron_job") == "cron"


def test_register_extra_tools_puts_them_in_the_registry(env):
    _settings, _store, registry, _task, registered = env
    assert registered == ["tool_describe", "cron_job"]
    assert set(registered) <= set(registry.tools)
    # 二次注册是幂等的（同名同 source 允许，但名字表不该变长）。
    again = tool_extra_impl.register_extra_tools(registry)
    assert again == registered
    assert [name for name in registry.tools].count("tool_describe") == 1


async def test_tool_describe_returns_the_full_parameters_through_the_registry(env):
    """走真实分发链（registry.execute），不是直接调实现函数。"""
    _settings, _store, registry, task_id, _registered = env
    result = await registry.execute(
        task_id, "desc-1", "tool_describe", {"name": "local_read_file"}
    )
    assert result["status"] == "ok"
    data = result["data"]
    assert data["name"] == "local_read_file"
    assert data["toolset"] == "files"
    assert data["domain"] == "file"
    assert "path" in data["argument_names"]
    assert data["parameters"]["type"] == "object"
    assert data["parameters"]["properties"], "参数表不能是空的（那就等于没 describe）"
    assert data["required"] == ["path"]
    assert data["deferred_in_listing"] is False  # Tier 0 的工具不会被延迟


async def test_tool_describe_is_what_makes_deferring_safe(env):
    """5.11 的关键闭环：Tier 2 工具在首屏**没有参数表**，参数表只能从 tool_describe 取。"""
    _settings, _store, registry, task_id, _registered = env
    listing = tool_extra_impl.deferred_listing(registry)
    deferred = {
        spec["function"]["name"]: spec
        for spec in listing
        if spec["function"]["name"] == "session_search"
    }
    assert deferred, "session_search 是 Tier 2，应当被延迟"
    assert deferred["session_search"]["function"]["parameters"] == {
        "type": "object",
        "properties": {},
    }
    assert "tool_describe" in deferred["session_search"]["function"]["description"]

    result = await registry.execute(
        task_id, "desc-2", "tool_describe", {"name": "session_search"}
    )
    assert result["status"] == "ok"
    assert result["data"]["deferred_in_listing"] is True
    assert result["data"]["parameters"]["properties"], "被延迟的工具也必须能取回参数表"


async def test_tool_describe_unknown_name_explains_the_next_step(env):
    _settings, _store, registry, task_id, _registered = env
    result = await registry.execute(task_id, "desc-3", "tool_describe", {"name": "websearch"})
    assert result["status"] == "error"
    assert "tool_search" in result["error"]
    # 拼错时给候选，而不是只说"没有这个工具"。
    assert "web_search" in result["error"] or "相近" in result["error"]


# ── 2. cron_job：真建、真读、真停、真删 ─────────────────────────────────────


async def test_cron_job_creates_lists_pauses_and_removes(env):
    """一条完整的真实动作链：建任务 → 列表里能看到 → 暂停 → 恢复 → 删除。

    任务表落在 `<data>/cron/jobs.json`，直接读盘核对——不看返回值自己说自己。
    """
    settings, _store, registry, task_id, _registered = env

    created = await registry.execute(
        task_id,
        "cron-1",
        "cron_job",
        {"action": "create", "schedule": "30m", "prompt": "看一次构建状态", "name": "构建巡检"},
    )
    assert created["status"] == "ok"
    data = created["data"]
    assert data["count"] == 1
    job = data["jobs"][0]
    assert job["schedule"] == "30m"
    assert job["enabled"] is True
    assert job["next_run_at"], "建任务必须算出下次运行时刻，否则调度器永远不会跑它"
    assert "orbit cron run" in data["notice"], "必须提醒执行需要守护进程在跑"

    # 真的落盘了：读 jobs.json，而不是相信返回值。
    jobs_path = settings.data_dir / "cron" / "jobs.json"
    assert jobs_path.exists()
    on_disk = json.loads(jobs_path.read_text(encoding="utf-8"))
    raw = on_disk["jobs"] if isinstance(on_disk, dict) else on_disk
    assert [item["id"] for item in raw] == [job["job_id"]]

    paused = await registry.execute(
        task_id, "cron-2", "cron_job", {"action": "pause", "job_id": job["job_id"]}
    )
    assert paused["data"]["jobs"][0]["enabled"] is False

    resumed = await registry.execute(
        task_id, "cron-3", "cron_job", {"action": "resume", "job_id": job["job_id"]}
    )
    assert resumed["data"]["jobs"][0]["enabled"] is True

    removed = await registry.execute(
        task_id, "cron-4", "cron_job", {"action": "remove", "job_id": job["job_id"]}
    )
    assert removed["data"]["count"] == 0
    on_disk = json.loads(jobs_path.read_text(encoding="utf-8"))
    assert (on_disk["jobs"] if isinstance(on_disk, dict) else on_disk) == []


async def test_cron_job_rejects_bad_input_with_actionable_messages(env):
    """坏输入必须**当场拒绝并说下一步**，不许静默建一个跑不起来的任务。"""
    _settings, _store, registry, task_id, _registered = env

    no_schedule = await registry.execute(
        task_id, "cron-5", "cron_job", {"action": "create", "prompt": "干点什么"}
    )
    assert no_schedule["status"] == "error"
    assert "schedule" in no_schedule["error"]

    bad_schedule = await registry.execute(
        task_id, "cron-6", "cron_job", {"action": "create", "schedule": "每个月圆之夜", "prompt": "x"}
    )
    assert bad_schedule["status"] == "error"

    unknown = await registry.execute(
        task_id, "cron-7", "cron_job", {"action": "remove", "job_id": "job-nope"}
    )
    assert unknown["status"] == "error"
    assert "action=list" in unknown["error"]


def test_cron_job_uses_the_same_job_table_as_orbit_cron(tmp_path):
    """复用 `app/cron.py` 的 CronStore：工具建的任务，`orbit cron` 也读同一份文件。"""
    from app import cron

    result = tool_extra_impl.run_cron_job(
        {"action": "create", "schedule": "1h", "prompt": "每小时看一眼"}, data_dir=tmp_path
    )
    assert result["count"] == 1
    jobs = cron.CronStore(tmp_path).load_jobs()
    assert [job.prompt for job in jobs] == ["每小时看一眼"]


# ── 3. 分级披露（MATRIX 5.11）：三档实测 ───────────────────────────────────


def test_defer_specs_drops_no_tool_and_keeps_tier0_verbatim(tmp_path):
    """延迟披露的两条硬边界：**一个工具都不删**，Tier 0 逐字不变。"""
    registry = ToolRegistry(Settings(data_dir=tmp_path / "d", workspace=tmp_path / "w"), None)
    full = registry.specs(budget=None)
    deferred, names = toolsets.defer_specs(full)

    assert [spec["function"]["name"] for spec in deferred] == [
        spec["function"]["name"] for spec in full
    ], "延迟不是删除：名字一个都不能少"
    assert set(names) == {name for name in names if toolsets.tier_of(name) == 2}
    assert names, "至少要有几个 Tier 2 工具被延迟，否则这条机制没被验证"

    by_name = {spec["function"]["name"]: spec for spec in full}
    for spec in deferred:
        name = spec["function"]["name"]
        assert schema_problems(spec) == [], f"{name} 被延迟后 schema 反而不合法了"
        if name in names:
            # 被延迟的：描述压成一句（并挂上"参数用 tool_describe 取"），参数表清空。
            assert spec["function"]["parameters"] == {"type": "object", "properties": {}}
            assert "tool_describe" in spec["function"]["description"]
        else:
            # 没被延迟的（含 Tier 0 / Tier 1）：描述与参数表逐字不变。
            assert spec["function"]["description"] == by_name[name]["function"]["description"]
            assert spec["function"]["parameters"] == by_name[name]["function"]["parameters"]


def test_listing_budget_measures_three_tiers_and_lands_under_4000(tmp_path):
    """5.11 的实测判据：三档数字要有，**延迟 Tier 2 之后必须真的进 4,000**。"""
    registry = ToolRegistry(Settings(data_dir=tmp_path / "d", workspace=tmp_path / "w"), None)
    report = tool_extra_impl.listing_budget(registry)

    assert report.budget == toolsets.LISTING_BUDGET_TOKENS == 4000
    assert report.full_tokens > report.trimmed_tokens > report.deferred_tokens
    assert report.deferred, "没有工具被延迟，这个结论就没有意义"
    assert report.within_budget is True, (
        f"延迟 Tier 2 之后仍超预算（{report.deferred_tokens} > {report.budget}）："
        "这条用例钉的是「4,000 能达」，达不到了要改口径而不是改数字"
    )
    # 数字要能被人读到（CLI 直接打印这段）。
    text = report.describe()
    assert "4,000" in text and "延迟" in text and "结论" in text


def test_listing_budget_reports_honestly_when_it_still_does_not_fit(tmp_path):
    """压不下来时 `within_budget` 必须是 False——不许把"还超"写成"达标"。"""
    registry = ToolRegistry(Settings(data_dir=tmp_path / "d", workspace=tmp_path / "w"), None)
    report = tool_extra_impl.listing_budget(registry, budget=500)
    assert report.within_budget is False
    assert "仍然超" in report.describe()
    assert report.as_dict()["within_budget"] is False


def test_tier_one_deferral_is_the_remaining_exit(tmp_path):
    """只延迟 Tier 2 还不够时，放宽到 Tier 1+2 是**唯一还能动的维度**（Tier 0 不动）。"""
    registry = ToolRegistry(Settings(data_dir=tmp_path / "d", workspace=tmp_path / "w"), None)
    tier2 = tool_extra_impl.listing_budget(registry, tiers=(2,))
    both = tool_extra_impl.listing_budget(registry, tiers=(1, 2))
    assert both.deferred_tokens < tier2.deferred_tokens
    assert len(both.deferred) > len(tier2.deferred)
    # Tier 0 在两种策略下都不许被延迟。
    assert "run_command" not in both.deferred
    assert "tool_search" not in both.deferred
    assert "tool_describe" not in both.deferred


# ── 4. 覆盖（MATRIX 5.14）───────────────────────────────────────────────────


def test_coverage_moved_up_and_the_new_entries_are_real():
    """covered 必须真的涨了，且涨的那几条映射到**真实工具名**。"""
    coverage = tool_catalog.orbit_coverage()
    assert coverage.covered_count >= 26, (
        f"覆盖没涨到预期（现在 {coverage.covered_count}）："
        f"missing={list(coverage.missing)[:5]}…"
    )
    # 本轮补上的三条：meta 的 tool_describe、cron 的 cronjob、desktop 的 read_terminal。
    assert coverage.covered["tool_describe"] == ("tool_describe",)
    assert coverage.covered["cronjob"] == ("cron_job",)
    assert coverage.covered["read_terminal"] == ("process",)
    pool = set(coverage.pool)
    for names in coverage.covered.values():
        for name in names:
            assert name in pool, f"声明表在说谎：{name} 不是真实工具"


def test_every_remaining_gap_still_has_a_reason():
    """剩下的缺口**全部**写了原因，且没有一个"做不到"的被打成覆盖。"""
    coverage = tool_catalog.orbit_coverage()
    for tool in tool_catalog.HERMES_TOOLS:
        if tool.name in coverage.missing:
            assert tool.reason.strip(), f"{tool.name} 缺了但没写理由"
        if tool.orbit_status == "unavailable":
            assert tool.orbit_tools == (), f"{tool.name} 标了做不到却给了映射"
    text = tool_catalog.report(coverage)
    assert "[做不到]" in text and "[没做]" in text, "缺口清单必须区分两类状态"


def test_report_separates_implemented_from_registered():
    """"写了代码"与"模型能用"是两件事，报告必须分开说。"""
    text = tool_catalog.report()
    assert "本轮新实现" in text
    assert "register_extra_tools" in text
    assert tool_catalog.extra_tool_names() == ("tool_describe", "cron_job")
    # 这两个还没进 LOCAL_TOOLS（禁改区），所以"已注册"必须是空的。
    assert tool_catalog.registered_extra_tools() == ()
