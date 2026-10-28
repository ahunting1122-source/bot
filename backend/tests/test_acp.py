"""ACP 服务端的最小协议往返（MATRIX 1.14）。

**不真起服务**：`AcpServer.serve(stdin, stdout)` 的输入输出都是参数，
测试直接塞两个 `StringIO`，不启子进程、不开端口、不碰终端。
"""

from __future__ import annotations

import io
import json

from app.cli.commands.acp import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    AcpServer,
)


def _echo(session_id: str, text: str) -> str:
    """假 agent：回一句能看出"哪条会话、什么内容"的话。"""
    return f"{session_id}: {text}"


def _server() -> AcpServer:
    return AcpServer(prompt_fn=_echo)


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def test_initialize_returns_version_and_honest_capabilities():
    response = _server().handle({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert response is not None
    result = response["result"]
    assert result["protocolVersion"]
    assert result["serverInfo"]["name"] == "orbit-acp"
    # 能力声明必须照实：不支持流式就别声称支持，否则编辑器会等一条永远不来的通知。
    assert result["capabilities"]["streaming"] is False


def test_session_new_then_prompt_round_trip():
    server = _server()
    created = server.handle({"jsonrpc": "2.0", "id": 1, "method": "session/new"})
    assert created is not None
    session_id = created["result"]["sessionId"]
    assert session_id in server.sessions
    answered = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/prompt",
            "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": "你好"}]},
        }
    )
    assert answered is not None
    assert answered["id"] == 2
    assert answered["result"]["stopReason"] == "end_turn"
    assert answered["result"]["content"][0]["text"] == f"{session_id}: 你好"


def test_prompt_on_unknown_session_is_an_invalid_params_error():
    response = _server().handle(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "method": "session/prompt",
            "params": {"sessionId": "nope", "prompt": "hi"},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


def test_unknown_method_returns_the_standard_code():
    response = _server().handle({"jsonrpc": "2.0", "id": 3, "method": "session/teleport"})
    assert response is not None
    # -32601 是 JSON-RPC 标准码：客户端据此知道"这条路走不通"，不会当成服务端崩了去重试。
    assert response["error"]["code"] == METHOD_NOT_FOUND == -32601


def test_malformed_envelope_is_rejected_before_dispatch():
    server = _server()
    assert server.handle("not an object")["error"]["code"] == INVALID_REQUEST
    assert server.handle({"id": 1, "method": "initialize"})["error"]["code"] == INVALID_REQUEST
    assert server.handle({"jsonrpc": "2.0", "id": 2})["error"]["code"] == INVALID_REQUEST


def test_serve_reads_lines_and_writes_lines_without_a_real_server():
    stdin = io.StringIO(
        "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "session/new"}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "session/prompt",
                        "params": {"sessionId": "acp-1", "prompt": "干活"},
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "id": 4, "method": "shutdown"}),
                # shutdown 之后的行不该再被处理。
                json.dumps({"jsonrpc": "2.0", "id": 5, "method": "initialize"}),
            ]
        )
        + "\n"
    )
    stdout = io.StringIO()
    code = _server().serve(stdin, stdout)
    assert code == 0
    responses = _lines(stdout)
    assert [item["id"] for item in responses] == [1, 2, 3, 4]
    assert responses[2]["result"]["content"][0]["text"] == "acp-1: 干活"


def test_bad_json_gets_a_parse_error_instead_of_silence():
    """解析失败必须回一条错误（id 为 null）：静默丢弃会让对端一直等。"""
    stdout = io.StringIO()
    _server().serve(io.StringIO("{ 这不是 JSON\n"), stdout)
    responses = _lines(stdout)
    assert len(responses) == 1
    assert responses[0]["error"]["code"] == PARSE_ERROR
    assert responses[0]["id"] is None


def test_notifications_get_no_response():
    """没有 `id` 的请求是通知，按规范**不得**回包。"""
    stdout = io.StringIO()
    _server().serve(io.StringIO(json.dumps({"jsonrpc": "2.0", "method": "initialize"}) + "\n"), stdout)
    assert _lines(stdout) == []


def test_prompt_failure_becomes_an_internal_error_not_a_crash():
    def boom(session_id: str, text: str) -> str:
        raise RuntimeError("agent 栈没起来")

    server = AcpServer(prompt_fn=boom)
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "session/new"})
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/prompt",
            "params": {"sessionId": "acp-1", "prompt": "hi"},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INTERNAL_ERROR
    assert "RuntimeError" in response["error"]["message"]


def test_missing_prompt_fn_is_reported_as_internal_error():
    """没接 agent 时要说清"没接线"，而不是回一个空回答骗客户端。"""
    server = AcpServer()
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "session/new"})
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/prompt",
            "params": {"sessionId": "acp-1", "prompt": "hi"},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INTERNAL_ERROR


async def test_async_prompt_fn_works_through_ahandle():
    """agent 栈本身是异步的：`prompt_fn` 返回协程时要走 `ahandle`。"""

    async def slow(session_id: str, text: str) -> str:
        return f"{session_id}/{text}"

    server = AcpServer(prompt_fn=slow)
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "session/new"})
    response = await server.ahandle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/prompt",
            "params": {"sessionId": "acp-1", "prompt": "异步"},
        }
    )
    assert response is not None
    assert response["result"]["content"][0]["text"] == "acp-1/异步"
