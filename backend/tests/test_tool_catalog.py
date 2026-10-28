"""MATRIX 5.14：Hermes 94 个内置工具的对照表 + 差距清单 + 本轮新工具的**真跑**用例。

三段各管一件事：

1. **对照表本身**：恰好 94 条、19 个域全覆盖、状态合法、缺的都写了理由，
   且"做不到"的工具**一个都没被注册**（注册了模型就会去调，调了必然失败）；
2. **对账结果**：`covered` / `missing` / `orbit_only` 三组不重叠，映射到真实工具名，
   并且这张表与真实工具表**互相钉住**（声明表里有的名字必须真的存在）；
3. **新工具真跑**：`local_read_file` / `local_search_files` / `process` / `session_search`
   各跑一遍真实动作（分页、三种搜索形态、后台进程起停、会话召回），不是"注册成功"。
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

import pytest

from app import tool_catalog, toolsets
from app.config import Settings
from app.main import migrate
from app.storage import Conversation, Message, Store, Task
from app.tools import LOCAL_TOOLS, ToolRegistry, schema_problems

# ── 夹具 ────────────────────────────────────────────────────────────────────


@pytest.fixture()
async def env(tmp_path):
    """隔离数据目录上的完整运行环境（settings + store + registry + task）。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="工具对照"))
    settings.workspace.mkdir(parents=True, exist_ok=True)
    try:
        yield settings, store, ToolRegistry(settings, store), task.id, conversation.id
    finally:
        await store.engine.dispose()


# ── 1. 对照表 ───────────────────────────────────────────────────────────────


def test_hermes_tools_are_exactly_94_with_unique_names():
    """口径：94 = 86 个注册字面量 + 4 个冻结层注册 + 3 个桥接工具 + 1 个条件注册。"""
    names = [tool.name for tool in tool_catalog.HERMES_TOOLS]
    assert len(names) == 94
    assert len(set(names)) == 94, "工具名重复，覆盖统计会失真"


def test_all_19_domains_declared_and_populated():
    """19 个功能域全部声明、每个域至少一个工具，且没有工具落在未声明的域里。"""
    assert len(tool_catalog.DOMAINS) == 19
    used = {tool.domain for tool in tool_catalog.HERMES_TOOLS}
    assert used == set(tool_catalog.DOMAINS)
    for domain, label in tool_catalog.DOMAINS.items():
        assert label.strip(), f"域 {domain} 缺中文说明"
        assert [t for t in tool_catalog.HERMES_TOOLS if t.domain == domain], f"域 {domain} 是空的"
    # 每条都有一句中文用途（给人看的缺口清单靠它）
    for tool in tool_catalog.HERMES_TOOLS:
        assert tool.purpose.strip(), f"{tool.name} 缺用途说明"


def test_every_status_is_declared_and_gaps_have_reasons():
    """状态只有三种；凡是没有覆盖的（不在 pool 里）都必须写清为什么。"""
    coverage = tool_catalog.orbit_coverage()
    for tool in tool_catalog.HERMES_TOOLS:
        assert tool.orbit_status in tool_catalog.STATUSES, f"{tool.name} 状态非法"
        if tool.name in coverage.missing:
            assert tool.reason.strip(), f"{tool.name} 缺了但没写理由"


def test_unavailable_tools_are_never_registered():
    """做不到的工具**一个都不能**出现在工具表里，也不能有 Orbit 映射。

    这是这条规则的要害：注册了模型就会去调，调了必然失败——"调了才失败"比"没有这个
    工具"糟得多（模型会重试、会把失败写进总结）。所以这里既查映射，也查真名。
    """
    pool = set(tool_catalog.orbit_pool())
    for tool in tool_catalog.HERMES_TOOLS:
        if tool.orbit_status != "unavailable":
            continue
        assert tool.orbit_tools == (), f"{tool.name} 标了做不到却给了 Orbit 映射"
        assert tool.name not in pool, f"{tool.name} 做不到却被注册成了工具"


def test_report_lists_every_domain_with_numbers_and_reasons():
    """`report()` 是给人看的缺口清单：每个域都要有覆盖数，每个缺口都要有名有因。"""
    coverage = tool_catalog.orbit_coverage()
    text = tool_catalog.report(coverage)
    for domain, label in tool_catalog.DOMAINS.items():
        assert domain in text, f"报告里没有域 {domain}"
        assert label in text
    # 覆盖率总览
    assert f"覆盖 {coverage.covered_count} / {coverage.total}" in text
    # 每个缺的工具名都出现在报告里，且带状态标记
    for name in coverage.missing:
        assert name in text
    assert "[做不到]" in text
    # `[没做]` 只在真的还有 todo 项时出现（第三轮把唯一的 todo 落地了，这一桶空了）
    has_todo = any(tool.orbit_status == "todo" for tool in tool_catalog.HERMES_TOOLS)
    assert ("[没做]" in text) is has_todo, "报告里的状态标记与实际状态不一致"


# ── 2. 对账结果 ─────────────────────────────────────────────────────────────


def test_coverage_groups_do_not_overlap():
    """三组互不重叠：covered 的 Hermes 名不在 missing，orbit_only 与映射值不交叉。"""
    coverage = tool_catalog.orbit_coverage()
    assert not set(coverage.covered) & set(coverage.missing)
    mapped = {name for names in coverage.covered.values() for name in names}
    assert not set(coverage.orbit_only) & mapped
    assert set(coverage.orbit_only) <= set(coverage.pool)
    assert coverage.total == 94
    assert coverage.covered_count + len(coverage.missing) == 94


def test_covered_entries_map_to_real_registered_tools():
    """`covered` 里的每个 Orbit 工具名都必须真的在工具表里（否则这张表在说谎）。

    ``toolset`` 那一半对**第三轮新工具**放宽一格：它们已经在池子里，但
    ``app/toolsets.py`` 的 ``TOOL_TOOLSET`` 还没登记（那个文件不归本轮改），
    待补的名字写在 ``tool_catalog.pending_toolset_mappings()`` 里。
    **只对这几个名字放行**：别的工具没归类照样红。
    """
    coverage = tool_catalog.orbit_coverage()
    pending = tool_catalog.pending_toolset_mappings()
    for hermes_name, orbit_names in coverage.covered.items():
        assert orbit_names, f"{hermes_name} 标了覆盖却没有映射"
        for name in orbit_names:
            assert name in coverage.pool, f"{hermes_name} → {name} 不是真实工具"
            if name in pending:
                continue
            assert toolsets.toolset_of(name) != toolsets.UNCLASSIFIED, name


def test_declared_table_matches_real_tool_tables():
    """声明表与真实工具表互相钉住：池子里的名字都得在 `TOOL_TOOLSET` 里登记过。

    `orbit_pool()` 从声明表读（为了不 import playwright / httpx），所以这条用例就是
    那次"偷懒"的代价：**声明表一旦与真实工具表漂移，这里立刻红**。
    """
    pool = set(tool_catalog.orbit_pool())
    local_names = {tool.name for tool in LOCAL_TOOLS}
    assert local_names <= pool
    # 第三轮那 5 个工具已经在池子里，但 `app/toolsets.py` 的 `TOOL_TOOLSET` 还没登记
    # （那个文件不归本轮改）。放行范围**写死**在 `pending_toolset_mappings()` 里，
    # 别的名字缺席照样红 —— 不是一句"以后再说"。
    pending = tool_catalog.pending_toolset_mappings()
    for name in pool:
        if name in toolsets.TOOL_TOOLSET:
            assert toolsets.toolset_of(name) != toolsets.UNCLASSIFIED, name
            continue
        assert name in pending, f"{name} 在工具池里却没在 TOOL_TOOLSET 登记"


def test_coverage_honours_an_explicit_pool():
    """显式给 pool 时按 pool 算：抽掉一个工具，它对应的 Hermes 工具立刻变成"缺"。"""
    full = tool_catalog.orbit_coverage()
    trimmed = tool_catalog.orbit_coverage(
        name for name in full.pool if name not in {"local_read_file", "local_read_document"}
    )
    assert "read_file" in trimmed.missing
    assert "read_file" not in trimmed.covered
    # 其它域的结论不受影响
    assert "write_file" in trimmed.covered


# ── 3. 新工具：每个都真跑一遍 ───────────────────────────────────────────────


def test_new_tools_have_valid_schemas_and_toolsets():
    """新工具必须 schema 合法（否则整次请求会被供应商 400）且归类完毕。"""
    new_names = {"local_read_file", "local_search_files", "process", "session_search"}
    by_name = {tool.name: tool for tool in LOCAL_TOOLS}
    for name in new_names:
        assert name in by_name, f"{name} 没进 LOCAL_TOOLS，模型看不到它"
        problems = schema_problems(by_name[name].specification())
        assert problems == [], f"{name} 的 schema 不合法：{problems}"
        assert toolsets.toolset_of(name) != toolsets.UNCLASSIFIED, f"{name} 没归类"
    # 归类结果本身也要有据可依
    assert toolsets.toolset_of("local_read_file") == "files"
    assert toolsets.toolset_of("local_search_files") == "files"
    assert toolsets.toolset_of("process") == "shell"
    assert toolsets.toolset_of("session_search") == "session"


async def test_local_read_file_pages_with_line_numbers(env):
    """分页读：offset/limit 生效、带行号、给出 total_lines 与 next_offset。"""
    settings, _store, registry, task_id, _conv = env
    (settings.workspace / "log.txt").write_text(
        "\n".join(f"第 {index} 行" for index in range(1, 11)), encoding="utf-8"
    )
    first = await registry.execute(
        task_id, "read-1", "local_read_file", {"path": "log.txt", "offset": 3, "limit": 4}
    )
    assert first["status"] == "ok"
    data = first["data"]
    assert data["total_lines"] == 10
    assert data["offset"] == 3 and data["returned_lines"] == 4
    assert data["text"].splitlines()[0].endswith("第 3 行")
    assert data["next_offset"] == 7
    assert data["truncated"] is True
    # 接着往下读：next_offset 真的接得上
    rest = await registry.execute(
        task_id, "read-2", "local_read_file", {"path": "log.txt", "offset": data["next_offset"]}
    )
    assert rest["data"]["text"].splitlines()[0].endswith("第 7 行")
    assert rest["data"]["truncated"] is False
    assert rest["data"]["next_offset"] is None


async def test_local_read_file_rejects_out_of_range_and_non_text(env):
    """越界 offset 与非文本扩展名都要**明确拒绝并说下一步**，不能返回空内容。"""
    settings, _store, registry, task_id, _conv = env
    (settings.workspace / "small.txt").write_text("只有一行\n", encoding="utf-8")
    (settings.workspace / "code.py").write_text("print(1)\n", encoding="utf-8")
    over = await registry.execute(
        task_id, "read-3", "local_read_file", {"path": "small.txt", "offset": 99}
    )
    assert over["status"] == "error"
    assert "超过总行数" in over["error"]
    # 仅工作文件夹档沿用文档白名单：要读源码得先切权限（这是权限决策，不是 bug）
    blocked = await registry.execute(task_id, "read-4", "local_read_file", {"path": "code.py"})
    assert blocked["status"] == "error"
    assert "完全访问权限" in blocked["error"]


async def test_local_search_files_three_output_modes(env):
    """三种形态各司其职：content 带行号与上下文、files_with_matches 只列文件、count 给条数。"""
    settings, _store, registry, task_id, _conv = env
    (settings.workspace / "a.md").write_text("前言\n目标行\n尾巴\n目标行\n", encoding="utf-8")
    (settings.workspace / "b.md").write_text("没有命中\n", encoding="utf-8")

    content = await registry.execute(
        task_id, "s-1", "local_search_files", {"pattern": "目标行", "context": 1}
    )
    data = content["data"]
    assert data["matched_files"] == 1 and data["count"] == 2
    assert [item["line"] for item in data["matches"]] == [2, 4]
    assert any("前言" in line for line in data["matches"][0]["context"])

    files = await registry.execute(
        task_id, "s-2", "local_search_files", {"pattern": "目标行", "output_mode": "files_with_matches"}
    )
    assert files["data"]["matches"] == [{"path": "a.md"}]

    counts = await registry.execute(
        task_id, "s-3", "local_search_files", {"pattern": "目标行", "output_mode": "count"}
    )
    assert counts["data"]["counts"] == [{"path": "a.md", "count": 2}]


async def test_local_search_files_finds_files_by_glob(env):
    """`target=files` 按名字找（通配符与子串两种写法都要认）。

    注意：仅工作文件夹档的遍历只认 TXT/Markdown（`walk_text_files` 的白名单，
    与 `local_search_text` 同一套权限口径），所以这里用 `.md` / `.txt` 造样本。
    """
    settings, _store, registry, task_id, _conv = env
    (settings.workspace / "notes.md").write_text("x\n", encoding="utf-8")
    (settings.workspace / "script.txt").write_text("x = 1\n", encoding="utf-8")
    globbed = await registry.execute(
        task_id, "s-4", "local_search_files", {"pattern": "*.md", "target": "files"}
    )
    assert [item["path"] for item in globbed["data"]["files"]] == ["notes.md"]
    substring = await registry.execute(
        task_id, "s-5", "local_search_files", {"pattern": "script", "target": "files"}
    )
    assert [item["path"] for item in substring["data"]["files"]] == ["script.txt"]
    missing = await registry.execute(
        task_id, "s-6", "local_search_files", {"pattern": "*.rs", "target": "files"}
    )
    assert missing["data"]["files"] == []
    assert "没有命中" in missing["data"]["notice"]


async def test_local_search_files_requires_start_dir_in_machine_mode(env):
    """完全访问档下必须给起点：整机递归搜索会把磁盘和上下文一起拖死。"""
    settings, store, _registry, task_id, _conv = env
    # `machine_wide` 是 `file_scope == "machine"` 的派生属性，只能通过 file_scope 设置。
    wide = Settings(
        data_dir=settings.data_dir, workspace=settings.workspace, file_scope="machine"
    )
    assert wide.machine_wide is True
    registry = ToolRegistry(wide, store)
    refused = await registry.execute(task_id, "s-7", "local_search_files", {"pattern": "x"})
    assert refused["status"] == "error"
    assert "指定起始目录" in refused["error"]


async def test_process_runs_in_background_and_reports_output(env):
    """`process`：start 立刻返回、poll 读到输出与退出码、list 能看到它。"""
    _settings, _store, registry, task_id, _conv = env
    started = await registry.execute(
        task_id, "p-1", "process", {"action": "start", "command": "echo orbit-bg"}
    )
    assert started["status"] == "ok"
    process_id = started["data"]["process_id"]
    assert started["data"]["running"] is True

    polled = started
    for _ in range(100):  # 进程几十毫秒就结束；上限 ~10s，避免死等
        polled = await registry.execute(
            task_id, "p-2", "process", {"action": "poll", "process_id": process_id}
        )
        if not polled["data"]["running"]:
            break
        await asyncio.sleep(0.1)
    assert polled["data"]["running"] is False
    assert polled["data"]["exit_code"] == 0
    assert "orbit-bg" in polled["data"]["output"]

    listed = await registry.execute(task_id, "p-3", "process", {"action": "list"})
    assert listed["data"]["count"] == 1
    assert listed["data"]["processes"][0]["process_id"] == process_id


async def test_process_kill_stops_a_running_command(env):
    """`kill`：真的终止（整棵进程树），并如实回报"杀没杀到"。"""
    _settings, _store, registry, task_id, _conv = env
    command = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    started = await registry.execute(
        task_id, "p-4", "process", {"action": "start", "command": command}
    )
    process_id = started["data"]["process_id"]
    killed = await registry.execute(
        task_id, "p-5", "process", {"action": "kill", "process_id": process_id}
    )
    assert killed["data"]["killed"] is True
    for _ in range(100):
        polled = await registry.execute(
            task_id, "p-6", "process", {"action": "poll", "process_id": process_id}
        )
        if not polled["data"]["running"]:
            break
        await asyncio.sleep(0.1)
    assert polled["data"]["running"] is False
    assert polled["data"]["exit_code"] is not None


async def test_process_start_goes_through_the_same_approval_gates(env):
    """后台执行不能成为绕开审批的后门：硬地板命令在 start 阶段就被拒。"""
    _settings, _store, registry, task_id, _conv = env
    refused = await registry.execute(
        task_id, "p-7", "process", {"action": "start", "command": "rm -rf /"}
    )
    assert refused["status"] == "error"
    assert "拒绝" in refused["error"]
    listed = await registry.execute(task_id, "p-8", "process", {"action": "list"})
    assert listed["data"]["count"] == 0  # 拒绝之后什么都没起


async def test_process_unknown_id_is_explained(env):
    """给错 process_id 要告诉模型"先 list"，而不是抛一个空异常。"""
    _settings, _store, registry, task_id, _conv = env
    failed = await registry.execute(
        task_id, "p-9", "process", {"action": "poll", "process_id": "proc-nope"}
    )
    assert failed["status"] == "error"
    assert "action=list" in failed["error"]


async def test_session_search_finds_and_scrolls(env):
    """`session_search`：按关键词找回原话（search）、展开命中点（scroll）、列最近会话（browse）。"""
    settings, store, registry, task_id, conversation_id = env
    await store.save(
        Message(
            conversation_id=conversation_id,
            role="user",
            content="上次说好了报告用蓝色主题，别改。",
        )
    )
    found = await registry.execute(
        task_id, "ss-1", "session_search", {"query": "蓝色主题"}
    )
    assert found["status"] == "ok"
    assert found["data"]["mode"] == "search"
    assert found["data"]["count"] == 1
    anchor = found["data"]["anchors"][0]
    assert "蓝色主题" in anchor["snippet"]

    scrolled = await registry.execute(
        task_id, "ss-2", "session_search", {"message_id": anchor["message_id"], "window": 2}
    )
    assert scrolled["data"]["mode"] == "scroll"
    assert scrolled["data"]["anchor"]["message_id"] == anchor["message_id"]
    assert scrolled["data"]["session_id"] == conversation_id

    browsed = await registry.execute(task_id, "ss-3", "session_search", {"limit": 3})
    assert browsed["data"]["mode"] == "browse"
    assert browsed["data"]["count"] >= 1

    nothing = await registry.execute(
        task_id, "ss-4", "session_search", {"query": "压根没有的词"}
    )
    assert nothing["data"]["count"] == 0
    assert "没有会话消息命中" in nothing["data"]["notice"]


def test_process_and_session_tools_are_reachable_through_specs(tmp_path):
    """新工具必须真的出现在暴露面里（`specs()` 会做 schema 前置校验并剔除坏工具）。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    registry = ToolRegistry(settings, None)
    names = [spec["function"]["name"] for spec in registry.specs("cli")]
    for name in ("local_read_file", "local_search_files", "process", "session_search"):
        assert name in names
    # 全部内置工具的 schema 都不许畸形
    for spec in registry.specs("cli"):
        assert schema_problems(spec) == []
    assert json.dumps(names, ensure_ascii=False)  # 名字是纯字符串，可序列化


def test_catalog_is_stable_across_calls():
    """同一进程内两次对账结果一致（不依赖 import 顺序 / 时间）。"""
    first = tool_catalog.orbit_coverage()
    time.sleep(0.01)
    second = tool_catalog.orbit_coverage()
    assert first == second
