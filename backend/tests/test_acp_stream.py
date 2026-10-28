"""`session/update` 流式通知：载荷形状、顺序保证、背压（MATRIX 1.14 续）。

**不真起服务**：`UpdateEmitter(write=...)` 的出口是一个可注入的记录器，
`AcpServer.serve(stdin, stdout)` 的两端是 `StringIO`。所以这里能断言"发出去的
每一帧长什么样、按什么顺序发"，而不启子进程、不开端口、不碰终端。

这一层测的三件事，对应 ACP 客户端真正会踩的三个坑：

1. **形状**：`sessionUpdate` 判别字段与各分支的必填字段（客户端按它 switch 渲染）；
2. **顺序**：`session/prompt` 的 response 必须排在**该轮所有 update 之后**
   —— 客户端收到 response 就认为这一轮结束，之后到达的 update 会被丢掉；
3. **背压**：客户端读得慢时，我们**如实报错**而不是静默丢帧或把内存吃光。
"""

from __future__ import annotations

import io
import json
import threading

import pytest

from app.acp_stream import (
    AGENT_MESSAGE_CHUNK,
    AGENT_THOUGHT_CHUNK,
    COMPLETED,
    FAILED,
    PENDING,
    PLAN,
    SESSION_UPDATE,
    TOOL_CALL,
    TOOL_CALL_UPDATE,
    USER_MESSAGE_CHUNK,
    PromptCancelled,
    UpdateEmitter,
    UpdateOverflow,
    message_chunk,
    notification,
    plan,
    result_status,
    thought_chunk,
    tool_call,
    tool_call_update,
    tool_kind,
)
from app.cli.commands.acp import INTERNAL_ERROR, AcpServer


# ── 夹具与工具 ─────────────────────────────────────────────────────────────
class Recorder:
    """记录器：`UpdateEmitter(write=recorder)` 里那个"通道"。

    带锁是为了让断言不会读到写线程写到一半的状态——`close()` 之后其实不需要，
    但一个不加锁的记录器迟早会在别的测试里咬人。
    """

    def __init__(self) -> None:
        self.frames: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, frame: dict) -> None:
        with self._lock:
            self.frames.append(frame)

    def kinds(self) -> list[str]:
        return [frame["params"]["update"]["sessionUpdate"] for frame in self.frames]

    def texts(self) -> list[str]:
        out = []
        for frame in self.frames:
            content = frame["params"]["update"].get("content")
            if isinstance(content, dict):
                out.append(content.get("text") or "")
        return out


def _lines(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]


def _updates(lines: list[dict]) -> list[dict]:
    return [line for line in lines if line.get("method") == SESSION_UPDATE]


# ── 1. 载荷形状 ────────────────────────────────────────────────────────────
def test_update_is_a_notification_not_a_request():
    """`session/update` 是通知：**没有 id**。

    带上 id 就变成"请求"，客户端会等一个永远不来的响应，它的待应答表也会一路涨。
    """
    frame = notification("acp-1", message_chunk("你好") or {})
    assert frame["jsonrpc"] == "2.0"
    assert frame["method"] == SESSION_UPDATE
    assert "id" not in frame
    assert "result" not in frame and "error" not in frame
    assert frame["params"]["sessionId"] == "acp-1"
    assert frame["params"]["update"]["sessionUpdate"] == AGENT_MESSAGE_CHUNK


def test_agent_message_chunk_shape():
    update = message_chunk("今天有三处报错")
    assert update == {
        "sessionUpdate": AGENT_MESSAGE_CHUNK,
        "content": {"type": "text", "text": "今天有三处报错"},
    }


def test_empty_text_produces_no_frame():
    """空增量不发帧：客户端会闪一条空消息，还白占一格队列。"""
    assert message_chunk("") is None
    assert thought_chunk("") is None
    assert plan([]) is None


def test_thought_chunk_is_its_own_kind():
    """思考与正文分开：客户端把 thought 渲染成旁白，混进正文等于把内心独白当结论。"""
    update = thought_chunk("先确认日志在哪")
    assert update is not None
    assert update["sessionUpdate"] == AGENT_THOUGHT_CHUNK
    assert update["content"]["text"] == "先确认日志在哪"


def test_user_message_chunk_is_used_for_history_replay():
    update = message_chunk("看看今天的日志", role="user")
    assert update is not None
    assert update["sessionUpdate"] == USER_MESSAGE_CHUNK


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("read_file", "read"),
        ("web_search", "fetch"),
        ("file_read", "read"),
        ("browser_open", "fetch"),
        ("shell", "execute"),
        ("write_file", "edit"),
        ("delete_path", "delete"),
        ("", "other"),
    ],
)
def test_tool_kind_is_guessed_from_the_name(name, expected):
    assert tool_kind(name) == expected


def test_tool_call_shape():
    update = tool_call("acp-1:tool:1", "read_file", args={"path": "a.log"})
    assert update["sessionUpdate"] == TOOL_CALL
    assert update["toolCallId"] == "acp-1:tool:1"
    assert update["kind"] == "read"
    assert update["status"] == PENDING
    assert update["title"] == "read_file"
    assert update["rawInput"] == {"path": "a.log"}


def test_tool_call_update_shape_and_failure_status():
    ok = tool_call_update("acp-1:tool:1", status=COMPLETED, text="3 处报错")
    assert ok["sessionUpdate"] == TOOL_CALL_UPDATE
    assert ok["toolCallId"] == "acp-1:tool:1"
    assert ok["status"] == COMPLETED
    assert ok["content"] == [{"type": "content", "content": {"type": "text", "text": "3 处报错"}}]
    # 被策略拒绝和真失败对用户是一回事：都不该显示成绿色。
    assert result_status({"status": "refused"}) == FAILED
    assert result_status({"status": "ok"}) == COMPLETED


def test_plan_shape():
    update = plan([{"content": "读日志", "status": "completed"}, "给出结论"])
    assert update is not None
    assert update["sessionUpdate"] == PLAN
    assert [entry["status"] for entry in update["entries"]] == ["completed", "pending"]
    assert all(entry["priority"] in {"high", "medium", "low"} for entry in update["entries"])


# ── 2. 事件翻译 ────────────────────────────────────────────────────────────
def test_emit_event_translates_agent_events_and_pairs_tool_calls():
    """`task_events` 的事件名 → ACP 载荷；`tool_started`/`tool_finished` 必须配对。"""
    recorder = Recorder()
    emitter = UpdateEmitter(recorder, session_id="acp-1")
    assert emitter.emit_event("message_delta", {"text": "先说一句"})
    assert emitter.emit_event("reflection", {"notes": "再查一下"})
    assert emitter.emit_event("tool_started", {"tool": "web_search"})
    assert emitter.emit_event("tool_finished", {"tool": "web_search", "result": {"status": "ok"}})
    emitter.close()

    assert recorder.kinds() == [
        AGENT_MESSAGE_CHUNK,
        AGENT_THOUGHT_CHUNK,
        TOOL_CALL,
        TOOL_CALL_UPDATE,
    ]
    started = recorder.frames[2]["params"]["update"]
    finished = recorder.frames[3]["params"]["update"]
    # id 必须一致，否则编辑器把"开始"和"结束"渲染成两条互不相干的记录。
    assert started["toolCallId"] == finished["toolCallId"]
    assert finished["status"] == COMPLETED


def test_events_without_an_acp_equivalent_are_not_invented():
    """`task_started` / `chunks_indexed` 在 ACP 里没有对应物：硬塞成正文就是撒谎。"""
    recorder = Recorder()
    emitter = UpdateEmitter(recorder, session_id="acp-1")
    assert emitter.emit_event("task_started", {"model": "x"}) is False
    assert emitter.emit_event("chunks_indexed", {"count": 3}) is False
    assert emitter.emit_event("message_delta", {"text": ""}) is False
    emitter.close()
    assert recorder.frames == []


# ── 3. 顺序保证 ────────────────────────────────────────────────────────────
def test_emitter_writes_frames_in_fifo_order_and_close_flushes():
    """`close()` 返回时，最后一帧**真的已经写出去了**。

    这就是"response 排在所有 update 之后"能成立的原因：协议层在 close 之后才构造
    response。如果 close 只是"把帧丢给线程"，最后一帧就可能比 response 晚到。
    """
    recorder = Recorder()
    emitter = UpdateEmitter(recorder, session_id="acp-1")
    for text in ("一", "二", "三"):
        assert emitter.emit(message_chunk(text))
    emitter.close()

    assert recorder.texts() == ["一", "二", "三"]
    assert emitter.sent == emitter.written == 3
    assert all("id" not in frame for frame in recorder.frames)


def test_replay_streams_updates_before_the_prompt_response():
    """回放式端到端：一串请求进、一串帧出，顺序与形状都得对。

    这是"真客户端接上来"的最小复刻：握手 → 开会话 → 一轮 prompt（带流）→ 收尾。
    """

    def agent(session_id: str, text: str, emitter: UpdateEmitter) -> str:
        emitter.emit(thought_chunk("先看看有什么"))
        emitter.emit_event("tool_started", {"tool": "read_file"})
        emitter.emit_event("tool_finished", {"tool": "read_file", "result": {"status": "ok"}})
        emitter.emit(message_chunk("今天有三处报错"))
        return "今天有三处报错"

    server = AcpServer(prompt_fn=agent)
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
                        "params": {
                            "sessionId": "acp-1",
                            "prompt": [{"type": "text", "text": "看看今天的日志"}],
                        },
                    }
                ),
                json.dumps({"jsonrpc": "2.0", "id": 4, "method": "shutdown"}),
            ]
        )
        + "\n"
    )
    stdout = io.StringIO()
    assert server.serve(stdin, stdout) == 0

    lines = _lines(stdout)
    # 响应只有 4 条（update 是通知，不能算进来）。
    responses = [line for line in lines if "id" in line]
    assert [line["id"] for line in responses] == [1, 2, 3, 4]
    # 有输出通道 + prompt_fn 接受 emitter ⇒ 能力声明必须说支持流式。
    assert responses[0]["result"]["capabilities"]["streaming"] is True

    updates = _updates(lines)
    assert [frame["params"]["update"]["sessionUpdate"] for frame in updates] == [
        AGENT_THOUGHT_CHUNK,
        TOOL_CALL,
        TOOL_CALL_UPDATE,
        AGENT_MESSAGE_CHUNK,
    ]
    # **顺序**：所有 update 都排在 id=3 的 response 之前。
    prompt_response = next(line for line in responses if line["id"] == 3)
    assert all(lines.index(frame) < lines.index(prompt_response) for frame in updates)
    # 每一帧都是通知（无 id）、都带会话 id。
    assert all("id" not in frame for frame in updates)
    assert all(frame["params"]["sessionId"] == "acp-1" for frame in updates)
    # 流出来的文本与最终回答一致：客户端把 chunk 拼起来就该得到 response 里的答案。
    streamed = "".join(
        frame["params"]["update"]["content"]["text"]
        for frame in updates
        if frame["params"]["update"]["sessionUpdate"] == AGENT_MESSAGE_CHUNK
    )
    assert streamed == prompt_response["result"]["content"][0]["text"]
    assert prompt_response["result"]["stopReason"] == "end_turn"


def test_cancel_notification_stops_the_running_prompt():
    """`session/cancel` 真的能打断正在跑的一轮：response 变 `stopReason: cancelled`。

    注意这条只有在"读 stdin 与派发不在同一条线程"时才可能成立——主线程此刻正堵在
    prompt 里，取消是读线程读到并置位的。
    """

    def agent(session_id: str, text: str, emitter: UpdateEmitter) -> str:
        assert emitter.cancel_event is not None
        # 等读线程把 session/cancel 送进来（真客户端会立刻发）。
        assert emitter.cancel_event.wait(5), "取消没有被送到正在跑的这一轮"
        raise PromptCancelled("用户按了停止")

    server = AcpServer(prompt_fn=agent)
    stdin = io.StringIO(
        "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "session/new"}),
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "session/prompt",
                        "params": {"sessionId": "acp-1", "prompt": "跑一个长任务"},
                    }
                ),
                # 通知：无 id，不期待回包。
                json.dumps({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": "acp-1"}}),
                json.dumps({"jsonrpc": "2.0", "id": 3, "method": "shutdown"}),
            ]
        )
        + "\n"
    )
    stdout = io.StringIO()
    assert server.serve(stdin, stdout) == 0

    responses = [line for line in _lines(stdout) if "id" in line]
    assert [line["id"] for line in responses] == [1, 2, 3]
    assert responses[1]["result"]["stopReason"] == "cancelled"
    assert responses[1]["result"]["content"] == []


# ── 4. 背压 ────────────────────────────────────────────────────────────────
def test_bounded_queue_reports_overflow_instead_of_dropping_frames():
    """写端卡住时，队列到上限就**抛错**：不静默丢帧，也不无限缓冲。

    做法：写端在 `gate` 上堵住（模拟一个不读的客户端），然后一直发——第
    `max_pending + 2` 帧一定放不进去。
    """
    gate = threading.Event()
    entered = threading.Event()

    def stuck_write(frame: dict) -> None:
        entered.set()
        gate.wait(5)

    emitter = UpdateEmitter(stuck_write, session_id="acp-1", max_pending=2)
    assert emitter.emit(message_chunk("1"))
    assert entered.wait(5), "写线程没有开始写第一帧"
    assert emitter.emit(message_chunk("2"))
    assert emitter.emit(message_chunk("3"))
    with pytest.raises(UpdateOverflow) as caught:
        emitter.emit(message_chunk("4"))
    assert "不静默丢帧" in str(caught.value)

    # 放开写端后正常收尾：已经入队的帧一帧都不能少（"超了报错"≠"已经收下的丢了"）。
    gate.set()
    emitter.close()
    assert emitter.sent == emitter.written == 3


def test_queue_overflow_becomes_a_jsonrpc_error_not_a_half_answer():
    """溢出要变成**协议层错误**：客户端必须知道这一轮没说完，而不是收到半截回答。"""
    gate = threading.Event()

    def agent(session_id: str, text: str, emitter: UpdateEmitter) -> str:
        try:
            for index in range(20):
                emitter.emit(message_chunk(f"#{index}"))
        except UpdateOverflow:
            gate.set()  # 放开写端，让收尾能把哨兵放进队列（否则 close 会等满超时）
            raise
        return "不该走到这里"

    recorder = Recorder()

    def write(frame: dict) -> None:
        if not gate.is_set():
            gate.wait(5)
        recorder(frame)

    server = AcpServer(prompt_fn=agent, write=write, max_pending=2)
    server.handle({"jsonrpc": "2.0", "id": 1, "method": "session/new"})
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "session/prompt",
            "params": {"sessionId": "acp-1", "prompt": "说很长一段"},
        }
    )
    assert response is not None
    assert response["error"]["code"] == INTERNAL_ERROR
    assert "session/update" in response["error"]["message"]
    assert "result" not in response


def test_write_failure_surfaces_on_close():
    """写端抛异常不能被吞掉：静默吞掉会让客户端少收一段话却以为正常结束。"""

    def boom(frame: dict) -> None:
        raise OSError("管道断了")

    emitter = UpdateEmitter(boom, session_id="acp-1")
    assert emitter.emit(message_chunk("hi"))
    with pytest.raises(UpdateOverflow) as caught:
        emitter.close()
    assert "管道断了" in str(caught.value)


def test_emit_after_close_is_refused():
    emitter = UpdateEmitter(Recorder(), session_id="acp-1")
    emitter.close()
    with pytest.raises(RuntimeError):
        emitter.emit(message_chunk("迟到的帧"))


def test_check_cancelled_raises_the_dedicated_exception():
    """取消是**正常终止原因**，不是错误：单独的异常类型让协议层能分开处理。"""
    cancel = threading.Event()
    emitter = UpdateEmitter(Recorder(), session_id="acp-1", cancel_event=cancel)
    emitter.check_cancelled()  # 没取消：什么都不发生
    cancel.set()
    assert emitter.cancelled is True
    with pytest.raises(PromptCancelled):
        emitter.check_cancelled()
    emitter.close()
