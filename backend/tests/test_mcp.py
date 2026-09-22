import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.mcp import MCPManager, sanitize, tool_name
from app.tools import Tool, ToolRegistry

FIXTURE = Path(__file__).parent / "fixtures" / "echo_mcp_server.py"


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
        assert manager.status["broken"]["state"] == "error"
        assert manager.status["broken"]["error"]
        assert manager.status["echo"]["state"] == "ready"
        assert "mcp__echo__echo" in registry.tools
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
