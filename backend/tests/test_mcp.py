import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.mcp import MCPManager, sanitize, tool_name
from app.tools import Tool, ToolRegistry

FIXTURE = Path(__file__).parent / "fixtures" / "echo_mcp_server.py"
RICH_FIXTURE = Path(__file__).parent / "fixtures" / "rich_mcp_server.py"


def from_mcp_module(name: str):
    import app.mcp

    return getattr(app.mcp, name)


def kill_fixture_children() -> int:
    """杀掉所有跑本测试 fixture 的 python 子进程，返回杀掉的个数。

    用 PowerShell 的 CIM 查命令行而不是 ``wmic``（后者被本机安全策略禁用），也不依赖
    ``psutil``（不在依赖里）。只匹配 fixture 文件名，不会误伤别的进程。
    """
    listing = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -like '*_mcp_server.py*' } | "
            "Select-Object -ExpandProperty ProcessId",
        ],
        capture_output=True,
        text=True,
    ).stdout
    pids = [int(x) for x in listing.split() if x.strip().isdigit()]
    pids = [pid for pid in pids if pid != os.getpid()]
    for pid in pids:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
    return len(pids)


async def wait_for(predicate, *, message: str, budget: float = 20.0, interval: float = 0.2):
    """轮询直到 predicate() 为真。

    用"两层判据"而不是单一墙钟：外层一个宽松的 ceiling 兜底，内层按真实进展判断。
    这里没有"进展"可测，所以只用 ceiling——但默认放宽到 20s，因为重连退避首位就是 2s。

    用 sleep 轮询而不是 ``asyncio.Event``：predicate 是调用方传进来的任意同步判断
    （读的是 registry/status 这类普通字典），没有地方可以 attach 事件。测试里毫秒级
    的轮询开销可以忽略。
    """
    try:
        async with asyncio.timeout(budget):
            while not predicate():  # noqa: ASYNC110 - 见 docstring：predicate 无事件可挂
                await asyncio.sleep(interval)
    except TimeoutError:
        raise AssertionError(message) from None


def test_tool_specification_passes_through_external_schema():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
        "required": ["a", "b"],
    }
    tool = Tool(name="mcp__x__add", description="add", parameters=schema, source="mcp:x")
    assert tool.specification()["function"]["parameters"] == schema


def test_names_are_ascii_and_prefixed():
    assert sanitize("我的 服务/v2") == "______v2"
    name = tool_name("filesystem", "read_file")
    assert name == "mcp__filesystem__read_file"
    assert len(tool_name("x" * 80, "y" * 80)) <= 64


def test_registry_passes_external_args_through_but_validates_builtin_ones(tmp_path):
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    registry = ToolRegistry(settings, None)
    registry.tools["mcp__x__add"] = Tool(
        name="mcp__x__add", description="add", parameters={"type": "object"}, source="mcp:x"
    )
    assert registry.validate("mcp__x__add", {"anything": 1}) == {"anything": 1}
    with pytest.raises(ValueError):
        registry.validate("mcp__x__add", "not-an-object")
    with pytest.raises(ValueError):
        registry.validate("local_list_files", {"path": "."} and {"unexpected": 1})


def test_unregister_source_removes_only_that_server(tmp_path):
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    registry = ToolRegistry(settings, None)
    for source in ("mcp:a", "mcp:b"):
        registry.tools[f"{source}_tool"] = Tool(
            name=f"{source}_tool", description="d", parameters={}, source=source
        )
        registry.external[f"{source}_tool"] = None
    registry.unregister_source("mcp:a")
    assert "mcp:a_tool" not in registry.tools
    assert "mcp:b_tool" in registry.tools
    assert "local_list_files" in registry.tools


def test_config_rejects_a_server_without_a_target(tmp_path):
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    manager = MCPManager(settings)
    with pytest.raises(ValueError):
        manager.save_servers([{"name": "broken"}])
    manager.save_servers([{"name": "ok", "command": "python", "args": ["-c", "pass"]}])
    assert manager.servers()[0]["name"] == "ok"


async def test_stdio_server_tools_become_agent_tools(tmp_path):
    """The whole path: spawn a real MCP server, list its tools, call one."""
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    manager = MCPManager(settings)
    manager.save_servers(
        [
            {
                "name": "echo",
                "command": sys.executable,
                "args": [str(FIXTURE)],
                "auto_approve": True,
            }
        ]
    )
    registry = ToolRegistry(settings, None)
    await manager.start(registry)
    try:
        assert manager.status["echo"]["state"] == "ready", manager.status["echo"]["error"]
        assert manager.status["echo"]["tools"] == 2

        names = sorted(tool for tool in registry.tools if tool.startswith("mcp__echo__"))
        assert names == ["mcp__echo__add", "mcp__echo__echo"]
        # auto_approve 的服务器不外乎是用户自己配的，跳过确认；默认则必须确认。
        assert registry.tools["mcp__echo__add"].confirmation is False
        assert registry.tools["mcp__echo__add"].source == "mcp:echo"

        result = await manager.execute("mcp__echo__add", {"a": 2, "b": 40}, "task", "op")
        assert result["status"] == "ok"
        assert "42" in result["data"]["content"]
    finally:
        await manager.close(registry)

    assert not [name for name in registry.tools if name.startswith("mcp__")]


async def test_a_broken_server_reports_its_error_without_blocking_others(tmp_path):
    """坏服务不能拖垮别人；而且它的**失败原因**必须在自动重连期间仍然可读。

    阶段 C 之后这里同时断言两件事：状态会翻成 reconnecting（自动重试中），
    但 error 字段不能因此被清空——否则用户只看到"正在重连"，永远不知道是缺依赖还是路径错。
    """
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    manager = MCPManager(settings)
    manager.save_servers(
        [
            {"name": "broken", "command": sys.executable, "args": ["-c", "raise SystemExit(3)"]},
            {"name": "echo", "command": sys.executable, "args": [str(FIXTURE)]},
        ]
    )
    registry = ToolRegistry(settings, None)
    await manager.start(registry)
    try:
        assert manager.status["broken"]["state"] in {"error", "reconnecting"}
        assert manager.status["broken"]["error"]
        assert manager.status["echo"]["state"] == "ready"
        assert "mcp__echo__echo" in registry.tools
    finally:
        await manager.close(registry)


async def test_a_permanently_broken_server_never_loses_its_reason(tmp_path):
    """反复重连也一直起不来的服务，error 不能再变回空。"""
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    manager = MCPManager(settings)
    manager.save_servers(
        [{"name": "dud", "command": sys.executable, "args": ["-c", "raise SystemExit(7)"]}]
    )
    registry = ToolRegistry(settings, None)
    await manager.start(registry)
    try:
        first = manager.status["dud"]["error"]
        assert first
        # 等一轮退避重连（首位 2s）确实发生过。
        await wait_for(
            lambda: manager.status["dud"]["state"] == "reconnecting",
            message="坏服务没有进入重连状态",
            budget=15.0,
        )
        assert manager.status["dud"]["error"] == first
    finally:
        await manager.close(registry)


async def test_confirmation_defaults_to_required(tmp_path):
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    manager = MCPManager(settings)
    manager.save_servers([{"name": "echo", "command": sys.executable, "args": [str(FIXTURE)]}])
    registry = ToolRegistry(settings, None)
    await manager.start(registry)
    try:
        assert registry.tools["mcp__echo__add"].confirmation is True
    finally:
        await manager.close(registry)


# --- 阶段 C：生命周期加固 -------------------------------------------------


def test_register_rejects_cross_source_shadowing(tmp_path):
    """同名工具跨来源必须被拒——静默覆盖会让"模型调的是谁"查不出来。"""
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    registry = ToolRegistry(settings, None)
    registry.register(
        [Tool(name="dup", description="builtin", schema=None)], lambda *a, **k: None
    )
    assert registry.tools["dup"].source == "builtin"
    registry.register(
        [Tool(name="dup", description="mcp", parameters={}, source="mcp:x")],
        lambda *a, **k: None,
    )
    # 内置在前，MCP 同名被拒——保留原注册。
    assert registry.tools["dup"].description == "builtin"


def test_register_allows_same_source_refresh(tmp_path):
    """同来源重复注册是重连刷新的正常路径，不能拒。"""
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    registry = ToolRegistry(settings, None)
    registry.register(
        [Tool(name="t", description="v1", parameters={}, source="mcp:x")], lambda *a, **k: None
    )
    registry.register(
        [Tool(name="t", description="v2", parameters={}, source="mcp:x")], lambda *a, **k: None
    )
    assert registry.tools["t"].description == "v2"


def test_register_skips_a_non_dict_schema(tmp_path):
    """schema 形状不对应在注册时就挡掉，而不是等运行时 400。"""
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    registry = ToolRegistry(settings, None)
    registry.register(
        [Tool(name="bad", description="d", parameters="not-a-dict", source="mcp:x")],
        lambda *a, **k: None,
    )
    assert "bad" not in registry.tools


async def test_modern_wire_does_not_rely_on_ping(tmp_path):
    """守住这个坑：2026-07-28 协议上 send_ping 恒失败，绝不能当存活探针。

    这条用例是回归闸——将来若有人把心跳改回 `send_ping`，它会立刻红。
    """
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    manager = MCPManager(settings)
    manager.save_servers([{"name": "echo", "command": sys.executable, "args": [str(FIXTURE)]}])
    registry = ToolRegistry(settings, None)
    await manager.start(registry)
    try:
        client = manager._clients["echo"]
        version = client.session.protocol_version
        assert version is not None
        if version >= "2026-07-28":
            # 服务活得好好的，ping 依然抛错——证明它不能用来判存活。
            from mcp.shared.exceptions import MCPDeprecationWarning

            with pytest.warns(MCPDeprecationWarning), pytest.raises(Exception) as caught:
                async with asyncio.timeout(5):
                    await client.send_ping()
            assert "not found" in str(caught.value).lower()
            assert from_mcp_module("_supports_listen")(client) is True
    finally:
        await manager.close(registry)


async def test_server_death_is_detected_and_tools_are_withdrawn(tmp_path):
    """杀掉 MCP 子进程 → 订阅流报失联 → 工具被摘除（全程无人工）。"""
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    manager = MCPManager(settings)
    manager.save_servers(
        [
            {
                "name": "echo",
                "command": sys.executable,
                "args": [str(FIXTURE)],
                "auto_approve": True,
            }
        ]
    )
    registry = ToolRegistry(settings, None)
    await manager.start(registry)
    try:
        assert "mcp__echo__echo" in registry.tools
        killed = kill_fixture_children()
        assert killed >= 1, "没找到 MCP 子进程，测试本身失效"
        await wait_for(
            lambda: "mcp__echo__echo" not in registry.tools,
            message="子进程被杀后工具未被摘除",
        )
        assert manager.status["echo"]["state"] == "reconnecting"
    finally:
        await manager.close(registry)


async def test_reconnect_restores_tools_without_manual_intervention(tmp_path):
    """摘除之后必须自己回来——这正是"全程无人工"的那一半。"""
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    manager = MCPManager(settings)
    manager.save_servers(
        [
            {
                "name": "echo",
                "command": sys.executable,
                "args": [str(FIXTURE)],
                "auto_approve": True,
            }
        ]
    )
    registry = ToolRegistry(settings, None)
    await manager.start(registry)
    try:
        assert "mcp__echo__echo" in registry.tools
        kill_fixture_children()
        await wait_for(
            lambda: "mcp__echo__echo" not in registry.tools,
            message="失联后工具未被摘除",
        )
        # 退避序列首项 2s，给足重连窗口。
        await wait_for(
            lambda: "mcp__echo__echo" in registry.tools and manager.status["echo"]["state"] == "ready",
            message="服务未自动恢复注册",
            budget=45.0,
        )
        result = await manager.execute("mcp__echo__add", {"a": 1, "b": 1}, "t", "o")
        assert result["status"] == "ok"
    finally:
        await manager.close(registry)


async def test_close_leaves_no_watcher_tasks(tmp_path):
    """close() 必须把看门狗全部取消，否则进程退出时挂着一堆 task。"""
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    manager = MCPManager(settings)
    manager.save_servers([{"name": "echo", "command": sys.executable, "args": [str(FIXTURE)]}])
    registry = ToolRegistry(settings, None)
    await manager.start(registry)
    assert manager._watchers, "看门狗没起来"
    await manager.close(registry)
    assert not manager._watchers


async def test_no_asyncio_task_leaks_after_close(tmp_path):
    """验收第 3 条：close() 之后不能有残留 asyncio task。"""
    before = {id(t) for t in asyncio.all_tasks()}
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    manager = MCPManager(settings)
    manager.save_servers([{"name": "echo", "command": sys.executable, "args": [str(FIXTURE)]}])
    registry = ToolRegistry(settings, None)
    await manager.start(registry)
    await manager.close(registry)
    # 取消是异步落地的，让出一次循环再数。
    await asyncio.sleep(0.1)
    leaked = [
        t
        for t in asyncio.all_tasks()
        if id(t) not in before and t is not asyncio.current_task() and not t.done()
    ]
    assert leaked == [], f"close() 后仍有 {len(leaked)} 个任务未结束"


# --- 阶段 B：MCP prompts → 技能多源 ---------------------------------------


async def test_prompts_become_skills_with_mcp_source(tmp_path):
    """声明了 prompts 的服务 → 其 prompt 以 source=mcp:<服务> 出现在技能目录里。"""
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    manager = MCPManager(settings)
    manager.save_servers(
        [{"name": "rich", "command": sys.executable, "args": [str(RICH_FIXTURE)]}]
    )
    registry = ToolRegistry(settings, None)
    await manager.start(registry)
    try:
        skills = manager.prompt_skills()
        assert [s["name"] for s in skills] == ["mcp__rich__summarize"]
        assert skills[0]["source"] == "mcp:rich"
        assert skills[0]["status"] == "active"
        # 正文是 prompt 模板（get_prompt 拉到），不是 dict repr
        assert "总结成三条要点" in skills[0]["body"]
    finally:
        await manager.close(registry)


async def test_echo_server_without_prompts_yields_no_prompt_skills(tmp_path):
    """不声明 prompts 的服务 → 能力探测跳过，不产生空技能、也不抛错。"""
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    manager = MCPManager(settings)
    manager.save_servers([{"name": "echo", "command": sys.executable, "args": [str(FIXTURE)]}])
    registry = ToolRegistry(settings, None)
    await manager.start(registry)
    try:
        assert manager.prompt_skills() == []
        assert manager.prompt_body("mcp__echo__anything") is None
    finally:
        await manager.close(registry)


async def test_skill_load_routes_mcp_prompt_to_body(tmp_path):
    """skill_load 对 `mcp__*` 名路由到远程 prompt 正文，而非本地文件。"""
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    from app.main import migrate

    migrate(settings)
    from app.storage import Conversation, Store, Task

    store = Store(settings.data_dir / "app.sqlite")
    manager = MCPManager(settings)
    manager.save_servers(
        [{"name": "rich", "command": sys.executable, "args": [str(RICH_FIXTURE)]}]
    )
    registry = ToolRegistry(settings, store)
    registry.mcp = manager
    await manager.start(registry)
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="总结"))
    try:
        result = await registry.execute(task.id, "s1", "skill_load", {"name": "mcp__rich__summarize"})
        assert result["status"] == "ok"
        assert "总结成三条要点" in result["data"]["body"]
    finally:
        await manager.close(registry)
        await store.engine.dispose()


# --- 阶段 D：MCP resources → RAG -------------------------------------------


async def test_resources_indexed_when_enabled(tmp_path):
    """index_resources=true 的服务 → 其 resource 进入 chunks 表，origin 可追溯。"""
    from sqlalchemy import select

    from app.indexing import build_indexer
    from app.main import migrate
    from app.storage import Chunk, Store

    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    manager = MCPManager(settings, indexer=build_indexer(settings, store))
    manager.save_servers(
        [
            {
                "name": "rich",
                "command": sys.executable,
                "args": [str(RICH_FIXTURE)],
                "index_resources": True,
            }
        ]
    )
    registry = ToolRegistry(settings, store)
    await manager.start(registry)
    try:
        # 资源索引是后台任务，轮询 chunks 表直到出现。
        rows: list = []
        async with asyncio.timeout(15.0):
            while not rows:
                rows = await store.rows(select(Chunk).where(Chunk.source_type == "mcp_resource"))
                if not rows:
                    await asyncio.sleep(0.1)
        assert [r.origin for r in rows] == ["note://welcome"]
        assert rows[0].host == "mcp:rich"
        assert rows[0].task_id == ""  # 无任务上下文，只能 scope=global 召回
        assert "欢迎使用" in rows[0].text
        # 真正可召回（入 RAG 的完整闭环）：global scope 下能检索到该资源。
        from app.retrieval import build_retriever

        retriever = build_retriever(settings, store)
        hits = await retriever.search("欢迎使用 rich", scope="global", top_k=5)
        origins = {h.get("origin") for h in hits}
        assert "note://welcome" in origins
    finally:
        await manager.close(registry)
        await store.engine.dispose()


async def test_resources_not_indexed_by_default(tmp_path):
    """不显式开 index_resources → 资源不入库（默认隔离，不主动抓取）。"""
    from sqlalchemy import select

    from app.indexing import build_indexer
    from app.main import migrate
    from app.storage import Chunk, Store

    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    manager = MCPManager(settings, indexer=build_indexer(settings, store))
    manager.save_servers(
        [{"name": "rich", "command": sys.executable, "args": [str(RICH_FIXTURE)]}]
    )
    registry = ToolRegistry(settings, store)
    await manager.start(registry)
    try:
        assert manager._resource_tasks == {}  # 没有起资源索引任务
        rows = await store.rows(select(Chunk).where(Chunk.source_type == "mcp_resource"))
        assert rows == []
    finally:
        await manager.close(registry)
        await store.engine.dispose()


async def test_resource_index_failure_does_not_block_connect(tmp_path):
    """索引写入抛异常也不影响连接（隔离原则：资源失败绝不拖垮主链路）。"""
    from app.main import migrate
    from app.storage import Store

    class BoomIndexer:
        async def write(self, chunks):
            raise RuntimeError("索引炸了")

    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    manager = MCPManager(settings, indexer=BoomIndexer())
    manager.save_servers(
        [
            {
                "name": "rich",
                "command": sys.executable,
                "args": [str(RICH_FIXTURE)],
                "index_resources": True,
            }
        ]
    )
    registry = ToolRegistry(settings, store)
    await manager.start(registry)
    try:
        # 连接仍然 ready，工具正常注册
        assert manager.status["rich"]["state"] == "ready"
        assert "mcp__rich__ping" in registry.tools
    finally:
        await manager.close(registry)
        await store.engine.dispose()
