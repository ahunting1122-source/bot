"""MATRIX 5.14 第二轮：`GROWTH_TOOLS` 的 5 个新工具，每个都**真跑**一遍。

## 这份文件为什么单独存在

`tests/test_tool_extra_impl.py` 钉的是上一轮那两个工具（`tool_describe` / `cron_job`）
与分级披露的预算实测。这一轮新增的 5 个（`tool_call` / `context_usage` / `compact` /
`setup_mcp` / `text_to_speech`）各自都有自己的落点与失败路径，塞进那份文件会让
"这一轮到底加了什么"要读两份历史才看得清。

## 判据（每条用例都按它写）

1. **走真实分发链**：全部经 `ToolRegistry.execute(...)`，不是直接调实现函数；
2. **有副作用可核对**：文件真的落盘、DB 真的变、注册表真的被调到；
3. **失败路径说得出下一步**：坏输入必须当场拒绝并指名要改哪个文件 / 哪个参数；
4. **覆盖数字真的涨了**，且涨的那几条映射到**真实工具名**。
"""

from __future__ import annotations

import json
import sys
from uuid import uuid4

import pytest

from app import tool_catalog, tool_extra_impl, toolsets
from app.config import Settings
from app.main import migrate
from app.storage import Conversation, Message, Store, Task
from app.tools import ToolRegistry, schema_problems

# ── 夹具 ────────────────────────────────────────────────────────────────────


@pytest.fixture()
async def env(tmp_path):
    """隔离数据目录上的完整运行环境（settings + store + registry + task + 已注册的新工具）。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="覆盖增长"))
    settings.workspace.mkdir(parents=True, exist_ok=True)
    registry = ToolRegistry(settings, store)
    tool_extra_impl.register_extra_tools(registry)
    registered = tool_extra_impl.register_growth_tools(registry)
    try:
        yield settings, store, registry, task.id, conversation.id, registered
    finally:
        await store.engine.dispose()


async def call(registry, task_id, name, args, *, op="op"):
    """走真实分发链调一次，返回完整载荷（`{"status": ..., "data": ...}`）。

    ``operation_id`` **每次都要不同**：注册表按 operation_id 做幂等短路，同一个 id 的第二次
    调用会直接返回第一次的结果 —— 拿固定 id 写用例，测出来的全是缓存。
    """
    return await registry.execute(task_id, f"{op}-{name}-{uuid4().hex[:8]}", name, args)


# ── 1. 声明、schema、归类 ───────────────────────────────────────────────────


def test_growth_tools_have_valid_schemas_and_are_classified():
    """5 个新工具：schema 合法（否则整次请求被 400）+ 归到正确 toolset + 有功能域。"""
    assert [tool.name for tool in tool_extra_impl.GROWTH_TOOLS] == [
        "tool_call",
        "context_usage",
        "compact",
        "setup_mcp",
        "text_to_speech",
    ]
    expected = {
        "tool_call": ("core", "meta"),
        "context_usage": ("core", "meta"),
        "compact": ("core", "meta"),
        "setup_mcp": ("mcp", "mcp"),
        "text_to_speech": ("tts", "tts"),
    }
    for tool in tool_extra_impl.GROWTH_TOOLS:
        spec = tool.specification()
        assert schema_problems(spec) == [], f"{tool.name} 的 schema 不合法"
        assert tool.description.strip(), f"{tool.name} 缺描述（模型靠它决定调不调）"
        toolset, domain = expected[tool.name]
        assert toolsets.toolset_of(tool.name) == toolset, tool.name
        assert toolsets.domain_of_tool(tool.name) == domain, tool.name
    # 元工具（core/Tier 0）不能被延迟：被延迟的桥自己都找不到
    assert toolsets.tier_of("tool_call") == 0
    assert toolsets.tier_of("context_usage") == 0


def test_register_growth_tools_is_idempotent_and_reports_really_registered(env):
    _settings, _store, registry, _task, _conv, registered = env
    assert registered == [tool.name for tool in tool_extra_impl.GROWTH_TOOLS]
    assert set(registered) <= set(registry.tools)
    again = tool_extra_impl.register_growth_tools(registry)
    assert again == registered, "二次注册必须幂等（名字表不该变长）"
    for name in registered:
        assert list(registry.tools).count(name) == 1
    # 上一轮那两个没有被这一轮的注册打乱
    assert "tool_describe" in registry.tools and "cron_job" in registry.tools


# ── 2. tool_call：按名字调另一个工具（走真实分发链）────────────────────────


async def test_tool_call_really_writes_a_file_through_the_registry(env):
    """`tool_call` → `local_write_file`：文件**真的**落盘，结果原样透传。"""
    settings, _store, registry, task_id, _conv, _registered = env
    result = await call(
        registry,
        task_id,
        "tool_call",
        {"name": "local_write_file", "arguments": {"path": "hello.txt", "content": "你好"}},
    )
    assert result["status"] == "ok"
    data = result["data"]
    assert data["called"] == "local_write_file"
    assert data["status"] == "ok"
    # 证据：文件在盘上，内容就是刚才那句话
    written = settings.workspace / "hello.txt"
    assert written.exists() and written.read_text(encoding="utf-8") == "你好"
    # 再调一次读回来（换个工具，证明它不是只认识一个名字）
    read_back = await call(
        registry, task_id, "tool_call", {"name": "local_read_file", "arguments": {"path": "hello.txt"}}
    )
    assert read_back["data"]["status"] == "ok"
    assert "你好" in json.dumps(read_back["data"]["result"], ensure_ascii=False)


async def test_tool_call_refuses_unknown_recursive_and_confirmation_tools(env):
    """三道闸：未知工具 / 调自己 / 需要确认的工具，全部**当场拒绝并说下一步**。"""
    _settings, _store, registry, task_id, _conv, _registered = env

    unknown = await call(registry, task_id, "tool_call", {"name": "websearch"})
    assert unknown["status"] == "error"
    assert "tool_search" in unknown["error"], "拼错时要指向 tool_search，而不是只说没有"

    recursive = await call(registry, task_id, "tool_call", {"name": "tool_call"})
    assert recursive["status"] == "error"
    assert "递归" in recursive["error"]

    # memory_save 是 confirmation=True：从桥里调等于绕开确认门禁，必须拒绝
    guarded = await call(
        registry,
        task_id,
        "tool_call",
        {"name": "memory_save", "arguments": {"key": "k", "value": "v"}},
    )
    assert guarded["status"] == "error"
    assert "确认" in guarded["error"]


async def test_tool_call_reports_the_inner_tools_error(env):
    """内层工具报错时，外层如实带出（不是把失败吞成"成功但没结果"）。"""
    _settings, _store, registry, task_id, _conv, _registered = env
    result = await call(
        registry, task_id, "tool_call", {"name": "local_read_file", "arguments": {"path": "nope.txt"}}
    )
    assert result["status"] == "ok", "外层调用本身成功了（它确实调到了内层工具）"
    assert result["data"]["status"] == "error", "内层的失败必须原样出现在结果里"


# ── 3. context_usage：真数字 + 真判定 + 口径 ────────────────────────────────


async def seed(store: Store, conversation_id: str, count: int, text: str) -> None:
    for index in range(count):
        await store.save(Message(conversation_id=conversation_id, role="user", content=f"{text}{index}"))


async def test_context_usage_reports_real_numbers_from_the_stored_transcript(env):
    _settings, store, registry, task_id, conversation_id, _registered = env
    await seed(store, conversation_id, 3, "这是一段用来占位的对话正文")
    result = await call(registry, task_id, "context_usage", {})
    assert result["status"] == "ok"
    data = result["data"]
    assert data["messages"] == 3, "条数必须等于真的落库条数"
    assert data["used_tokens"] > 0
    assert data["window"] == 0 and data["window_source"], (
        "窗口读不到时必须如实说读不到（window=0 + 说明来源），而不是猜一个默认值"
    )
    assert "context_window" in data["window_source"] or "未配置" in data["window_source"]
    assert data["compaction_needed"] is False, "窗口未知时不判定压缩（session_plane 的口径）"
    assert "口径" in data["caveat"], "必须写清数字不含什么（系统提示 / 未落库增量）"


async def test_context_usage_can_judge_with_an_explicit_window(env):
    _settings, store, registry, task_id, conversation_id, _registered = env
    await seed(store, conversation_id, 5, "很长的正文" * 200)
    result = await call(registry, task_id, "context_usage", {"window": 50})
    data = result["data"]
    assert data["window"] == 50 and data["window_source"] == "调用方指定"
    assert data["used_ratio"] > 0
    assert data["compaction_needed"] is True, "占用远超窗口时必须判定需要压缩"
    assert "阈值" in data["compaction_reason"]


# ── 4. compact：判定 + 摘要触发 ─────────────────────────────────────────────


async def test_compact_plans_which_groups_to_drop(env):
    """`compact` 给出的是**真计划**：丢哪几组、压完剩多少，全部来自 compaction.plan。"""
    _settings, store, registry, task_id, conversation_id, _registered = env
    await seed(store, conversation_id, 6, "第一组" * 300)
    result = await call(registry, task_id, "compact", {"window": 40})
    assert result["status"] == "ok"
    plan = result["data"]["plan"]
    assert plan["needed"] is True
    assert plan["dropped"], "要压缩就必须指出丢哪几组"
    assert plan["kept"], "永远保留最后一组（当前正在做的事）"
    assert plan["projected_tokens"] < plan["used_tokens"]
    assert result["data"]["applied"] is False, "apply=false 时不许声称已压缩"
    assert "apply=true" in result["data"]["notice"]


async def test_compact_without_a_summary_service_refuses_with_what_to_inject(env):
    """`apply=true` 但没注入摘要服务：**抛错并指名要注入什么**，不许静默什么都不做。"""
    _settings, store, registry, task_id, conversation_id, _registered = env
    await seed(store, conversation_id, 2, "正文")
    result = await call(registry, task_id, "compact", {"apply": True})
    assert result["status"] == "error"
    assert "summary_service" in result["error"]
    assert "SummaryService" in result["error"]


async def test_compact_with_a_summary_service_asks_for_a_rebuild(env):
    """注入摘要服务后 `apply=true` 真的会去请求重算（拿得到 conversation_id）。"""
    _settings, _store, registry, task_id, conversation_id, _registered = env
    calls: list[str] = []

    class FakeSummaries:
        async def rebuild(self, target: str) -> None:
            calls.append(target)

    tool_extra_impl.register_growth_tools(registry, summary_service=FakeSummaries())
    result = await call(registry, task_id, "compact", {"apply": True, "window": 40})
    assert result["status"] == "ok"
    assert result["data"]["summary_requested"] is True
    assert calls == [conversation_id], "必须把**这场**对话的 id 交给摘要服务"


# ── 5. setup_mcp：真写 <data>/mcp.json ──────────────────────────────────────


async def test_setup_mcp_adds_lists_disables_and_removes(env):
    settings, _store, registry, task_id, _conv, _registered = env
    added = await call(
        registry,
        task_id,
        "setup_mcp",
        {
            "action": "add",
            "name": "demo",
            "command": "python",
            "args": ["-m", "demo_server"],
            "auto_approve": True,
        },
    )
    assert added["status"] == "ok"
    assert added["data"]["count"] == 1
    assert "reload" in added["data"]["notice"], "必须提醒连接要等 reload/重启"

    # 证据：真的写进了 <data>/mcp.json（不是只改了内存）
    path = settings.data_dir / "mcp.json"
    assert path.exists()
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert [entry["name"] for entry in on_disk["servers"]] == ["demo"]
    assert on_disk["servers"][0]["args"] == ["-m", "demo_server"]

    listed = await call(registry, task_id, "setup_mcp", {"action": "list"})
    assert listed["data"]["servers"][0]["target"] == "python"

    disabled = await call(registry, task_id, "setup_mcp", {"action": "disable", "name": "demo"})
    assert disabled["data"]["servers"][0]["enabled"] is False

    removed = await call(registry, task_id, "setup_mcp", {"action": "remove", "name": "demo"})
    assert removed["data"]["count"] == 0
    assert json.loads(path.read_text(encoding="utf-8"))["servers"] == []


async def test_setup_mcp_rejects_bad_input_with_next_steps(env):
    _settings, _store, registry, task_id, _conv, _registered = env
    no_name = await call(registry, task_id, "setup_mcp", {"action": "add"})
    assert no_name["status"] == "error" and "name" in no_name["error"]

    no_target = await call(registry, task_id, "setup_mcp", {"action": "add", "name": "x"})
    assert no_target["status"] == "error", "command 与 url 都没给时必须拒绝"

    missing = await call(registry, task_id, "setup_mcp", {"action": "remove", "name": "nope"})
    assert missing["status"] == "error"
    assert "action=list" in missing["error"], "要指向能查出正确名字的那一步"


# ── 6. text_to_speech：真落盘一个 wav ──────────────────────────────────────


async def test_text_to_speech_writes_a_real_wav_via_the_command_channel(env, tmp_path):
    """命令通道（离线可验）：合成结果真的落到 `<data>/media/` 下。

    这里刻意用命令通道而不是 SAPI：SAPI 在本机的 venv python 子进程里被完整性级别挡住
    （见 `tests/test_voice_local.py` 文件头），命令通道则是**确定性**的 —— 它验证的是
    "工具 → 语音层 → 落盘"这条链，而不是某个 provider 的可用性。
    """
    settings, _store, registry, task_id, _conv, _registered = env
    code = "import sys; sys.stdout.buffer.write(b'RIFFxxxxWAVEfmt ')"

    from app import voice

    voice.save_config(
        settings.data_dir,
        voice.VoiceConfig(tts_command=f'"{sys.executable}" -c "{code}"'),
    )
    result = await call(registry, task_id, "text_to_speech", {"text": "你好"})
    assert result["status"] == "ok"
    data = result["data"]
    assert data["provider"] == "command"
    assert data["format"] == "wav (RIFF)"
    target = data["path"]
    assert target.endswith(".wav")
    with open(target, "rb") as handle:
        assert handle.read(4) == b"RIFF"
    assert data["bytes"] > 0


async def test_text_to_speech_without_any_provider_explains_both_paths(env):
    """两家都不可用时，必须**逐条**给出原因（而不是只说"不可用"）。"""
    _settings, _store, registry, task_id, _conv, _registered = env
    result = await call(registry, task_id, "text_to_speech", {"text": "你好"})
    if result["status"] == "ok":  # 这台机器上 SAPI 可用（非沙箱环境）
        assert result["data"]["provider"] in {"sapi", "command"}
        return
    error = result["error"]
    assert "命令通道" in error and "SAPI" in error
    assert "requires:" in error or "install:" in error, "原因里要带怎么办"


async def test_text_to_speech_refuses_empty_text(env):
    _settings, _store, registry, task_id, _conv, _registered = env
    result = await call(registry, task_id, "text_to_speech", {"text": "   "})
    assert result["status"] == "error"
    assert "text" in result["error"]


# ── 7. 覆盖：26 → 31，且涨的那几条都是真工具 ───────────────────────────────


def test_coverage_moved_from_26_to_31_and_the_new_entries_are_real():
    coverage = tool_catalog.orbit_coverage()
    assert coverage.total == 94
    # 第二轮把覆盖推到 31；第三轮又推到 33（send_message + message_agent）。
    # 这里用 `>=` 而不是钉死数字：每轮都钉一次会让这份测试变成"改数字"的仪式，
    # 真正该守的是"涨的那些都指向真实工具"（下面几条）。
    assert coverage.covered_count >= 31, (
        f"覆盖至少要到 31（上一轮 26 + 第二轮 5），现在 {coverage.covered_count}："
        f"missing={list(coverage.missing)[:5]}…"
    )
    for hermes_name in ("tool_call", "context_usage", "compact", "setup_mcp", "text_to_speech"):
        assert coverage.covered[hermes_name] == (hermes_name,), hermes_name
    pool = set(coverage.pool)
    # 第三轮新工具的 toolset 还没在 `app/toolsets.py` 登记（那个文件不归本轮改），
    # 待补的名字写在 `tool_catalog.pending_toolset_mappings()` 里；别的名字照样要归类。
    pending = tool_catalog.pending_toolset_mappings()
    for names in coverage.covered.values():
        for name in names:
            assert name in pool, f"声明表在说谎：{name} 不是真实工具"
            if name in pending:
                continue
            assert toolsets.toolset_of(name) != toolsets.UNCLASSIFIED, name


def test_growth_tools_are_in_the_pool_and_report_separates_the_two_batches():
    pool = set(tool_catalog.orbit_pool())
    for tool in tool_extra_impl.GROWTH_TOOLS:
        assert tool.name in pool, tool.name
    # 两批工具分开可数：上一轮 2 个，本轮 5 个
    assert tool_catalog.extra_tool_names() == ("tool_describe", "cron_job")
    assert tool_catalog.growth_tool_names() == tuple(
        tool.name for tool in tool_extra_impl.GROWTH_TOOLS
    )
    text = tool_catalog.report()
    assert "本轮新实现" in text
    # 剩下 63 个缺口必须逐个写了原因，且区分"做不到"与"没做"
    for tool in tool_catalog.HERMES_TOOLS:
        if tool.name in tool_catalog.orbit_coverage().missing:
            assert tool.reason.strip(), f"{tool.name} 缺了但没写理由"
    assert "[做不到]" in text
    # `[没做]` 只在真的还有 todo 项时出现（第三轮把唯一的 todo 落地了，这一桶空了）
    has_todo = any(tool.orbit_status == "todo" for tool in tool_catalog.HERMES_TOOLS)
    assert ("[没做]" in text) is has_todo, "报告里的状态标记与实际状态不一致"


def test_compact_and_context_usage_are_not_marked_unavailable_any_more():
    """这两条上一轮标的是"做不到"，本轮必须改成覆盖，且理由写清边界。"""
    by_name = {tool.name: tool for tool in tool_catalog.HERMES_TOOLS}
    for name in ("tool_call", "context_usage", "compact", "setup_mcp", "text_to_speech"):
        record = by_name[name]
        assert record.orbit_status == "covered", name
        assert record.orbit_tools == (name,), name
        assert "实现见 app/tool_extra_impl.py" in record.reason, name


def test_delegation_is_covered_and_the_kanban_board_is_partially_covered():
    """**性质断言**（不是清单断言）：委派已覆盖、看板仍缺。

    原来这条叫 `test_delegation_and_kanban_stay_unavailable_with_their_reasons`，
    断言「委派**永远**不可用」——那在写的时候是对的（`ChildRunner` 协议零实现），
    **但它把当时的实现状态写成了永久性质**。第 37 轮补上 `app/child_runner.py`
    之后这条就过期了。

    **清单断言会过期，性质断言不会**：这里改成断言「两边各自的**性质**」——
    委派**必须**覆盖（能力存在），看板**必须**仍是 unavailable 且**必须带原因**
    （缺能力时不能悄悄消失）。
    """
    by_name = {tool.name: tool for tool in tool_catalog.HERMES_TOOLS}
    # **委派已覆盖**（第 37 轮补了 `app/child_runner.py`）：它必须有真实的 Orbit 映射，
    # 且**不再需要** `reason`——`reason` 是「为什么做不到」的位置，做到了就该空着。
    delegate = by_name["delegate_task"]
    assert delegate.orbit_status == "covered", delegate.orbit_status
    assert delegate.orbit_tools == ("delegate_task",), delegate.orbit_tools
    swarm = by_name["agent_swarm"]
    assert swarm.orbit_status == "covered", swarm.orbit_status
    # **看板部分覆盖**（第 44 轮做了 4 个）：覆盖的那几个必须有真实映射，
    # 没做的那几个**必须仍带原因**——缺能力时不能悄悄消失。
    assert by_name["kanban_show"].orbit_status == "covered"
    assert by_name["kanban_show"].orbit_tools == ("kanban_show",)
    # **不写死是哪一张**：写死名字的话，每覆盖一个就要改一次测试——
    # 而这轮已经为此红了三次。改成从「实际仍缺的看板工具」里取，
    # 断言的是**性质**（缺的必须带原因），不是**清单**（哪几个缺）。
    coverage = tool_catalog.orbit_coverage()
    # **看板家族已 14/14 全实现**（第 70 轮）——所以这里不再断言「还有缺的看板工具」，
    # 改成断言「已覆盖的看板工具都有真实映射」+「整个家族一条不缺」。
    kanban_all = [t for t in tool_catalog.HERMES_TOOLS if t.domain == "kanban"]
    kanban_missing = [t.name for t in kanban_all if t.name in set(coverage.missing)]
    assert not kanban_missing, f"看板家族还缺：{kanban_missing}"
    for tool in kanban_all:
        assert tool.orbit_tools == (tool.name,), f"{tool.name} 没有真实映射"
