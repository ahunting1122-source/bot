"""Minimal MCP server over stdio, used by the integration test.

Run by the test as a subprocess: `python echo_mcp_server.py`.

mcp 2.x renamed FastMCP to MCPServer; the server API is otherwise the same.
"""

from mcp.server.mcpserver import MCPServer

server = MCPServer("echo")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@server.tool()
def echo(text: str) -> str:
    """Return the text unchanged."""
    return f"echo: {text}"


if __name__ == "__main__":
    server.run()
