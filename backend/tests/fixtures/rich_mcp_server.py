"""MCP server fixture that declares tools *and* prompts *and* resources.

Used to prove two things the plain echo fixture cannot:

1. capability probing actually gates the extra calls (this server advertises
   ``prompts``/``resources``; ``echo_mcp_server`` does not, so asking it must be skipped
   rather than raising);
2. prompt/resource payload shapes survive the trip into ``MCPManager``.

Run by the test as a subprocess: `python rich_mcp_server.py`.
"""

from mcp.server.mcpserver import MCPServer

server = MCPServer("rich")


@server.tool()
def ping(text: str) -> str:
    """Return the text with a pong prefix."""
    return f"pong: {text}"


@server.prompt(
    name="summarize",
    description="Summarize a document into three bullet points.",
)
def summarize(topic: str = "anything") -> str:
    return f"请把关于「{topic}」的内容总结成三条要点。"


@server.resource(
    "note://welcome",
    name="welcome",
    description="A short welcome note.",
    mime_type="text/plain",
)
def welcome() -> str:
    return "欢迎使用 rich fixture。"


if __name__ == "__main__":
    server.run()
