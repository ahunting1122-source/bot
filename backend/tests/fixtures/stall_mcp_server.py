"""An MCP server fixture that can be told to stop answering, from the outside.

Why a separate fixture instead of `kill`ing the echo process: on Windows the child is a
``python.exe`` and killing it leaves the stdio pipe half-open, which the SDK reports as a
clean EOF rather than the timeout we want to exercise. So the failure mode is produced
*inside* the server — it stops responding to pings while keeping the process alive — which
is exactly the "hung, not dead" case the grace period exists for.

Control channel is a file: the test writes ``stop`` into it and the server starts ignoring
everything. Absent file (or any other content) means "behave normally".

Run by the test as a subprocess: `python stall_mcp_server.py <control_file>`.
"""

import sys
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

CONTROL = Path(sys.argv[1]) if len(sys.argv) > 1 else None

server = MCPServer("stall")


def _stalled() -> bool:
    if CONTROL is None:
        return False
    try:
        return CONTROL.read_text(encoding="utf-8").strip() == "stop"
    except OSError:
        return False


@server.tool()
def now() -> str:
    """Return the current epoch seconds."""
    return str(int(time.time()))


if __name__ == "__main__":
    server.run()
