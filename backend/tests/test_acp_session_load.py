"""`session/load`：从库里续接一个 ACP 会话（MATRIX 1.14 续）。

三条硬要求，这里逐条钉住：

1. **真会话能恢复**：读 `store` 的会话与消息，转成 ACP 的会话表示；
2. **不存在的会话回标准错误码**（`-32602`），而不是回一个空会话——空会话会让
   客户端以为历史丢了，然后在库里分叉出一条平行会话；
3. **只读**：`session/load` 一个字节都不该改库（逐字段断言，不是"看起来没改"）。

**真库、真迁移**：用 `tmp_path` 下的临时 SQLite + `migrate()`，跑的是生产的那套
`_cli_history_fn`（读库口），不是测试里另写的替代品——否则测的是测试。
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from app.acp_stream import (
    AGENT_MESSAGE_CHUNK,
    SESSION_UPDATE,
    USER_MESSAGE_CHUNK,
)
from app.cli.commands.acp import (
    INVALID_PARAMS,
    METHOD_NOT_FOUND,
    AcpServer,
    _cli_history_fn,
    _cli_list_fn,
    _existing_conversation,
)
from app.cli.context import Context
from app.config import Settings
from app.main import migrate
from app.storage import Conversation, Message, Task


# ── 夹具 ───────────────────────────────────────────────────────────────────
@pytest.fixture
def context(tmp_path) -> Iterator[Context]:
    """一个指向临时库的真 `Context`（存储层是 SQLAlchemy async，必须走它的事件循环）。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    ctx = Context(settings)
    try:
        yield ctx
    finally:
        ctx.close()


def _seed(context: Context, *, title: str = "ACP 载入") -> str:
    """造一条真会话 + 两条真消息，返回会话 id。"""

    async def _do() -> str:
        store = context.store
        conversation = await store.save(Conversation(title=title))
        await store.save(
            Message(conversation_id=conversation.id, role="user", content="看看今天的日志")
        )
        await store.save(
            Message(conversation_id=conversation.id, role="assistant", content="今天有三处报错")
        )
        return str(conversation.id)

    return str(context.run(_do()))


def _snapshot(context: Context) -> dict[str, Any]:
    """把库里所有可能被"顺手改一下"的字段抓一份快照，用来断言只读。"""

    async def _do() -> dict[str, Any]:
        store = context.store
        conversations = await store.rows(select(Conversation))
        messages = await store.rows(select(Message))
        tasks = await store.rows(select(Task))
        return {
            "conversations": [
                (
                    row.id,
                    row.title,
                    row.updated_at,
                    row.message_revision,
                    row.summary_status,
                    row.summary_message_count,
                )
                for row in conversations
            ],
            "messages": [
                (row.id, row.conversation_id, row.role, row.content, row.created_at)
                for row in messages
            ],
            "tasks": [(row.id, row.status) for row in tasks],
        }

    return dict(context.run(_do()))


def _server(context: Context, **kwargs: Any) -> AcpServer:
    return AcpServer(
        history_fn=_cli_history_fn(context),
        list_fn=_cli_list_fn(context),
        **kwargs,
    )


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


# ── 1. 真会话恢复 ──────────────────────────────────────────────────────────
def test_load_restores_a_real_conversation(context: Context):
    conversation_id = _seed(context)
    server = _server(context)

    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "session/load",
            "params": {"sessionId": conversation_id, "cwd": "E:\\work"},
        }
    )
    assert response is not None
    result = response["result"]
    assert result["sessionId"] == conversation_id
    assert result["title"] == "ACP 载入"
    assert result["cwd"] == "E:\\work"
    # 消息按时间序、角色与正文都在。
    assert [item["role"] for item in result["messages"]] == ["user", "assistant"]
    assert [item["content"][0]["text"] for item in result["messages"]] == [
        "看看今天的日志",
        "今天有三处报错",
    ]
    # 载入之后这个会话是"活的"：登记进内存，且指向同一个 conversationId（续接，不是新开）。
    assert server.sessions[conversation_id]["conversationId"] == conversation_id
    assert server.sessions[conversation_id]["loaded"] is True


def test_load_unknown_session_returns_invalid_params_and_creates_nothing(context: Context):
    """不存在的会话必须回错误码。

    回一个空会话看起来"友好"，实际是最坏的答案：客户端会以为历史丢了，然后拿这个
    空会话继续用——库里就多出一条跟原会话平行的分叉。
    """
    server = _server(context)
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "session/load",
            "params": {"sessionId": "不存在的会话"},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS == -32602
    assert "不存在" in response["error"]["message"]
    # 关键：不能顺手建一条空会话出来。
    assert server.sessions == {}


def test_load_without_a_store_wiring_says_so(context: Context):
    """没接 store 时如实报 -32601，而不是假装"会话不存在"。"""
    server = AcpServer()
    response = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "session/load", "params": {"sessionId": "x"}}
    )
    assert response is not None
    assert response["error"]["code"] == METHOD_NOT_FOUND
    assert "history_fn" in response["error"]["message"]


def test_load_requires_a_session_id(context: Context):
    response = _server(context).handle(
        {"jsonrpc": "2.0", "id": 1, "method": "session/load", "params": {}}
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


# ── 2. 只读 ────────────────────────────────────────────────────────────────
def test_load_does_not_touch_the_database(context: Context):
    """`session/load` 是"打开"，不是"新建"：库里的状态必须逐字段一致。"""
    conversation_id = _seed(context)
    before = _snapshot(context)
    server = _server(context)

    response = server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "session/load", "params": {"sessionId": conversation_id}}
    )
    assert response is not None and "result" in response

    after = _snapshot(context)
    assert after == before
    # 顺带钉一下最容易被动的地方：`updated_at` 与 `message_revision` 不该因为"打开"而变。
    assert after["conversations"][0][2] == before["conversations"][0][2]
    assert after["conversations"][0][3] == before["conversations"][0][3]


def test_load_replays_history_as_updates_before_the_response(context: Context):
    """历史用 `session/update` 回放给客户端，且全部排在 load 的 response 之前。"""
    conversation_id = _seed(context)
    server = _server(context)
    stdin = io.StringIO(
        "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "session/load",
                        "params": {"sessionId": conversation_id},
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "id": 3, "method": "shutdown"}),
            ]
        )
        + "\n"
    )
    stdout = io.StringIO()
    assert server.serve(stdin, stdout) == 0

    lines = _lines(stdout)
    responses = [line for line in lines if "id" in line]
    assert [line["id"] for line in responses] == [1, 2, 3]
    # 能力声明照实写：接了 history_fn 就是能 load。
    assert responses[0]["result"]["capabilities"]["loadSession"] is True

    updates = [line for line in lines if line.get("method") == SESSION_UPDATE]
    assert [frame["params"]["update"]["sessionUpdate"] for frame in updates] == [
        USER_MESSAGE_CHUNK,
        AGENT_MESSAGE_CHUNK,
    ]
    assert [frame["params"]["update"]["content"]["text"] for frame in updates] == [
        "看看今天的日志",
        "今天有三处报错",
    ]
    load_response = next(line for line in responses if line["id"] == 2)
    assert all(lines.index(frame) < lines.index(load_response) for frame in updates)
    assert all("id" not in frame for frame in updates)


# ── 3. 会话列表（Orbit 扩展）────────────────────────────────────────────────
def test_session_list_returns_recent_conversations(context: Context):
    _seed(context, title="旧的")
    newest = _seed(context, title="新的")
    response = _server(context).handle(
        {"jsonrpc": "2.0", "id": 1, "method": "session/list", "params": {"limit": 1}}
    )
    assert response is not None
    sessions = response["result"]["sessions"]
    assert len(sessions) == 1
    # 按 updated_at 倒序：刚写的那条在最前。
    assert sessions[0]["sessionId"] == newest
    assert sessions[0]["title"] == "新的"


def test_session_list_rejects_a_bad_limit(context: Context):
    response = _server(context).handle(
        {"jsonrpc": "2.0", "id": 1, "method": "session/list", "params": {"limit": "很多"}}
    )
    assert response is not None
    assert response["error"]["code"] == INVALID_PARAMS


# ── 4. 续接：prompt 要接回同一条会话 ───────────────────────────────────────
def test_existing_conversation_lets_a_loaded_session_continue_itself(context: Context):
    """载入后的 prompt 必须落回**同一条**会话，而不是新开一条。"""
    conversation_id = _seed(context)
    store = context.store
    assert _existing_conversation(context, store, conversation_id) == conversation_id
    # `session/new` 给的 `acp-N` 不是会话 id：查不到就该返回空串（调用方会新建）。
    assert _existing_conversation(context, store, "acp-1") == ""


def test_close_drops_the_session_from_memory_only(context: Context):
    conversation_id = _seed(context)
    before = _snapshot(context)
    server = _server(context)
    server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "session/load", "params": {"sessionId": conversation_id}}
    )
    assert conversation_id in server.sessions
    closed = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/close",
            "params": {"sessionId": conversation_id},
        }
    )
    assert closed is not None and closed["result"] is None
    assert conversation_id not in server.sessions
    # 关闭是幂等的：重复关不报错（客户端的清理路径会一路关下去）。
    again = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "session/close",
            "params": {"sessionId": conversation_id},
        }
    )
    assert again is not None and "result" in again
    assert _snapshot(context) == before
