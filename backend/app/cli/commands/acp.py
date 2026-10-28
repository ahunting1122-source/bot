"""ACP 服务端（MATRIX 1.14）：stdio 上的 JSON-RPC 2.0，**带流式通知**。

## ACP 是什么

ACP（Agent Client Protocol）是**编辑器 ↔ agent** 之间的一条 JSON-RPC 2.0 通道：
编辑器（Zed 等）负责界面与文件上下文，agent 负责干活。传输层最常见的就是
**stdio**——父进程起子进程，一行一条 JSON 消息。

## 一个 ACP 客户端接上来会看到什么（按顺序，这就是 `run_stdio` 的完整剧本）

```
→ {"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":1}}
← {"jsonrpc":"2.0","id":1,"result":{"protocolVersion":"0.1",
                                    "serverInfo":{"name":"orbit-acp","version":"0.1.0"},
                                    "capabilities":{...,"streaming":true,"loadSession":true},
                                    "authMethods":[]}}

→ {"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"E:\\work","mcpServers":[]}}
← {"jsonrpc":"2.0","id":2,"result":{"sessionId":"acp-1"}}

→ {"jsonrpc":"2.0","id":3,"method":"session/prompt",
   "params":{"sessionId":"acp-1","prompt":[{"type":"text","text":"看看今天的日志"}]}}
← {"jsonrpc":"2.0","method":"session/update",                  ← 通知：没有 id
   "params":{"sessionId":"acp-1",
             "update":{"sessionUpdate":"agent_thought_chunk",
                       "content":{"type":"text","text":"先确认日志在哪"}}}}
← {"jsonrpc":"2.0","method":"session/update",
   "params":{"sessionId":"acp-1",
             "update":{"sessionUpdate":"tool_call","toolCallId":"acp-1:tool:1",
                       "title":"read_file","kind":"read","status":"pending"}}}
← {"jsonrpc":"2.0","method":"session/update",
   "params":{"sessionId":"acp-1",
             "update":{"sessionUpdate":"tool_call_update","toolCallId":"acp-1:tool:1",
                       "status":"completed","content":[{"type":"content",
                         "content":{"type":"text","text":"…日志前 2000 字…"}}]}}}
← {"jsonrpc":"2.0","method":"session/update",
   "params":{"sessionId":"acp-1",
             "update":{"sessionUpdate":"agent_message_chunk",
                       "content":{"type":"text","text":"今天有三处报错"}}}}
← {"jsonrpc":"2.0","id":3,"result":{"stopReason":"end_turn",
                                    "content":[{"type":"text","text":"今天有三处报错"}]}}
                                                                 ↑ response 永远最后

→ {"jsonrpc":"2.0","method":"session/cancel","params":{"sessionId":"acp-1"}}   ← 用户按了停止
   （正在跑的那一轮会在下一个轮询点收尾，response 变成 stopReason: "cancelled"）

→ {"jsonrpc":"2.0","id":4,"method":"session/load","params":{"sessionId":"<库里的会话 id>"}}
← 先把历史逐条回放成 user_message_chunk / agent_message_chunk 通知，再回 id=4 的 result。

→ {"jsonrpc":"2.0","id":5,"method":"shutdown"}
← {"jsonrpc":"2.0","id":5,"result":null}     之后不再读 stdin
```

**顺序保证**：`session/prompt` 的 response 一定排在**该轮所有 `session/update` 之后**。
它不是靠"记得先 flush"，而是结构性的——见 `UpdateEmitter.close()` 与 `_finish()`。

## 已实现的方法

| 方法 | 作用 | 本实现做了什么 |
|---|---|---|
| `initialize` | 握手 | 回协议版本 + 服务信息 + **照实的能力声明** |
| `authenticate` | 鉴权 | 空操作：本机单用户、无凭据门槛（`authMethods: []`） |
| `session/new` | 开会话 | 生成 `sessionId`，记住 `cwd` / `mcpServers` |
| `session/load` | 续接历史会话 | 读 `store` 的会话与消息 → 回放成通知 + 返回会话表示；**只读** |
| `session/list` | 列历史会话 | 列最近若干条（Orbit 扩展：ACP 目前没有这个方法） |
| `session/prompt` | 发一轮提示 | 交给注入的 `prompt_fn`，**边跑边发 `session/update`** |
| `session/cancel` | 打断当前轮 | 置取消标记 → 该轮 response 变 `stopReason: "cancelled"` |
| `session/close` | 关会话 | 从内存里摘掉（不动库）。Orbit 扩展：ACP 规范里没有它 |
| `shutdown` | 收尾 | 置关闭标记，`serve()` 读完当前行就退出 |

## 仍然**没有**实现（逐条写清为什么，不是"忘了"）

* `session/request_permission`——工具执行前向编辑器要授权。Orbit 现在是完全访问档，
  没有审批分支可挂（`app/graph/nodes.py::gate` 的注释里写了原因）。声明里
  `permissions: false`，客户端就不会来问。
* `session/set_mode` / `session/set_model`——运行时切模式与模型。Orbit 的模型与推理档
  是**启动时**从配置/环境读定的（`AgentRunner.get_model`），进程内没有安全的切换点；
  硬做等于让同一轮里的模型前后不一致。要换请重开进程。
* `fs/read_text_file` / `fs/write_text_file` / `terminal/*`——这三个是**编辑器提供、
  agent 调用**的**反向**能力（编辑器当服务端）。本实现是单向的：服务端只应答，从不下发
  请求。真要做需要"同一个 stdio 上双向请求/响应 + 请求 id 关联"的调度器（我们这边在
  同步循环里等一个来自客户端的回包，得让读循环与等待分离），这一波不做；而且让 ACP
  客户端能直接读写本机任意路径是**安全面扩大**，值得单独一轮设计。
  声明里 `fs: false` / `terminal: false`，客户端不会尝试委派。
* `available_commands_update` / `current_mode_update` 通知——见 `app/acp_stream.py`
  的模块注释：Orbit 没有斜杠命令面板与模式切换，发了就是撒谎。

## 为什么自己写 JSON-RPC 而不用库

本机没有网络，装不了 `agent-client-protocol`。而且这一层要做的事很少：读一行、
判错、回一行。自己写反而能把**输入输出注入**（测试用 `StringIO`，不真起服务）
这件事做干净——见 `AcpServer.serve(stdin, stdout)` / `run_stdio()`。

## 线程模型（为什么读 stdin 是另一条线程）

`session/cancel` 只有在**一轮 prompt 正在跑**的时候才有意义，而那时主循环正堵在
`handle(session/prompt)` 里——如果读 stdin 也在主线程，取消永远读不到。所以：
一条读线程只管"读一行 + 解析 + 把取消立刻置位"，主线程只做"派发 + 写响应"。
**所有写都发生在主线程**（读线程一帧都不写），所以输出不会被两条线程撕开；
而 update 由 emitter 的写线程写，那时主线程正堵在 prompt 里，两者也不会同时写。

输入队列同样有界（`INPUT_QUEUE`）：客户端狂发而 agent 跑得慢时，读线程会**堵在
`put` 上**——这就是对 stdin 的自然背压，而不是把内存吃光。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import queue
import sys
import threading
import time
from collections.abc import Callable
from typing import Any, TextIO

from app.acp_stream import (
    PromptCancelled,
    UpdateEmitter,
    UpdateOverflow,
    message_chunk,
    text_block,
)
from app.cli.commands._table import register_handler
from app.cli.context import Context

log = logging.getLogger("orbit.acp")

# ── 协议常量 ───────────────────────────────────────────────────────────────
#: 本服务端实现的 ACP 协议版本。ACP 还在演进，这里如实标"我们说的是哪一版"。
PROTOCOL_VERSION = "0.1"
SERVER_NAME = "orbit-acp"
SERVER_VERSION = "0.1.0"

# ── JSON-RPC 2.0 错误码（规范固定值，不要自创）──────────────────────────────
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

#: 一次 prompt 最多能压多少帧 update 未写出（见 `app/acp_stream.py` 的背压说明）。
DEFAULT_MAX_PENDING = 256

#: stdin 侧的待派发队列上限：客户端狂发时的自然背压。
INPUT_QUEUE = 64

#: 轮询 `task_events` 的间隔（秒）。与 `chat.POLL_SECONDS` 同值：0.15s 在"够实时"
#: 与"别把 SQLite 读爆"之间。
POLL_SECONDS = 0.15

#: 出现这些任务状态就停止轮询（与 `app/cli/commands/chat.py` 的 `TERMINAL` 一致）。
TERMINAL = ("SUCCEEDED", "FAILED", "CANCELLED", "WAITING_USER")

_EOF = object()
_PARSE_FAILURE = object()


class AcpError(Exception):
    """带 JSON-RPC 错误码的协议级错误。抛出点即"这个请求哪里不对"。"""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


def _prompt_text(prompt: Any) -> str:
    """从 ACP 的 `prompt` 里取纯文本。

    ACP 的 prompt 是一组内容块（`[{"type": "text", "text": ...}]`），也允许直接给
    字符串（本地测试与简易客户端）。非文本块**直接丢掉**：本实现的能力声明里
    `image`/`audio` 都是 false，静默把图片当文本塞给模型只会得到一句胡话。
    """
    if isinstance(prompt, str):
        return prompt.strip()
    if not isinstance(prompt, list):
        return ""
    parts: list[str] = []
    for block in prompt:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(part for part in parts if part).strip()


def _error(request_id: Any, code: int, message: str, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def _write(stream: TextIO, payload: dict[str, Any]) -> None:
    """写一行 JSON 并立刻 flush。

    必须 flush：对端（编辑器）是**按行读**的，缓冲着不写等于让对面干等——
    表现是"agent 启动了但永远不回应"。
    """
    stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
    stream.flush()


async def _maybe_await(value: Any) -> Any:
    """`prompt_fn` 可能返回协程也可能返回值：异步路径统一在这里抹平。"""
    return await value if inspect.isawaitable(value) else value


def _wants_emitter(fn: Callable[..., Any] | None) -> bool:
    """`prompt_fn` 要不要第三个参数（emitter）？

    为什么不干脆改签名：`tests/test_acp.py` 与所有既有接线都传 `(session_id, text)`，
    改签名等于把别人的调用点一起打碎。这里按**签名形状**判断，两种都支持：

    * `(session_id, text)` —— 跑完一次性返回（能力声明里 `streaming: false`）；
    * `(session_id, text, emitter)` —— 边跑边推（`streaming: true`）。

    能力声明跟着这个判断走，所以"能不能推流"永远和"实际会不会推"一致。
    """
    if fn is None:
        return False
    try:
        parameters = inspect.signature(fn).parameters
    except (TypeError, ValueError):  # 某些内建/装饰过的可调用对象没有签名
        return False
    if any(param.kind is inspect.Parameter.VAR_POSITIONAL for param in parameters.values()):
        return True
    if "emitter" in parameters:
        return True
    positional = [
        param
        for param in parameters.values()
        if param.kind
        in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
    ]
    return len(positional) >= 3


class _Prompt:
    """一轮 prompt 的运行时句柄：会话 id、正文、emitter、取消标记。

    打成一个对象是为了让"开一轮 / 收一轮"成对出现——`emitter.close()` 漏调一次，
    客户端就会永远等那条 response 之前的最后一帧。
    """

    __slots__ = ("session_id", "text", "emitter", "cancel")

    def __init__(
        self,
        session_id: str,
        text: str,
        emitter: UpdateEmitter | None,
        cancel: threading.Event,
    ) -> None:
        self.session_id = session_id
        self.text = text
        self.emitter = emitter
        self.cancel = cancel


class AcpServer:
    """stdio 上的 ACP 服务端。

    三个注入点，各自负责一件事：

    * `prompt_fn(session_id, text[, emitter]) -> str` —— agent。两参（一次性返回）
      与三参（边跑边推）都支持，见 `_wants_emitter`；
    * `history_fn(session_id) -> dict | None` —— `session/load` 的读库口。
      返回 `None` 表示"这个会话不存在"，服务端据此回标准错误码；
    * `list_fn(limit) -> list[dict]` —— `session/list` 的读库口。

    再加上 `write`（update 的输出通道，默认由 `serve()` 指向 stdout）：
    协议层因此可以完全脱离 agent 栈与数据库单测——测试传假函数 + `StringIO`，
    不启服务、不碰数据库。
    """

    def __init__(
        self,
        *,
        prompt_fn: Callable[..., Any] | None = None,
        history_fn: Callable[[str], dict[str, Any] | None] | None = None,
        list_fn: Callable[[int], list[dict[str, Any]]] | None = None,
        write: Callable[[dict[str, Any]], None] | None = None,
        protocol_version: str = PROTOCOL_VERSION,
        server_version: str = SERVER_VERSION,
        max_pending: int = DEFAULT_MAX_PENDING,
    ) -> None:
        self._prompt_fn = prompt_fn
        self._history_fn = history_fn
        self._list_fn = list_fn
        self._write = write
        self.protocol_version = protocol_version
        self.server_version = server_version
        self.max_pending = max_pending
        self.sessions: dict[str, dict[str, Any]] = {}
        self._counter = 0
        self._closed = False
        #: 正在跑的 prompt 的取消标记（session_id → Event）。
        self._cancels: dict[str, threading.Event] = {}
        #: 取消请求早于 prompt 到达时先记在这里，下一次 `_open_prompt` 消费掉。
        self._cancel_requests: set[str] = set()
        #: 正在跑的 prompt 的 request id → session id（`$/cancel_request` 要用）。
        self._request_sessions: dict[Any, str] = {}

    # ── 能力 ───────────────────────────────────────────────────────────────
    @property
    def streaming(self) -> bool:
        """真的会推流吗？两个条件同时成立：有输出通道 + prompt_fn 接受 emitter。

        **照实写**是这一层的底线：声称支持流式却没推，编辑器会一直等一条永远不来的
        通知（表现为"agent 卡住了"），比直接说不支持糟糕得多。
        """
        return self._write is not None and _wants_emitter(self._prompt_fn)

    # ── 方法表 ─────────────────────────────────────────────────────────────
    def _methods(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {
            "initialize": self._initialize,
            "authenticate": self._authenticate,
            "session/new": self._session_new,
            "session/load": self._session_load,
            "session/list": self._session_list,
            "session/prompt": self._session_prompt,
            "session/cancel": self._session_cancel,
            "session/close": self._session_close,
            "shutdown": self._shutdown,
        }

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """握手。能力声明**照实写**，包括"能不能推流""能不能续接历史"。"""
        return {
            "protocolVersion": self.protocol_version,
            "serverInfo": {"name": SERVER_NAME, "version": self.server_version},
            "capabilities": {
                "promptCapabilities": {
                    "image": False,
                    "audio": False,
                    "embeddedContext": False,
                },
                "streaming": self.streaming,
                "loadSession": self._history_fn is not None,
                # `session/list` / `session/close` 是 Orbit 扩展（ACP 目前没有这两个方法）。
                "listSessions": self._list_fn is not None,
                # 反向能力：fs/terminal 是"编辑器提供、agent 调用"，本实现从不下发请求。
                "permissions": False,
                "fs": False,
                "terminal": False,
            },
            # 本机单用户、凭据在 keyring 里，ACP 这条通道没有鉴权门槛。列空表示
            # "客户端不需要做任何鉴权"，客户端据此就不会来调 `authenticate`。
            "authMethods": [],
        }

    def _authenticate(self, params: dict[str, Any]) -> dict[str, Any]:
        """空操作：Orbit 的 ACP 通道没有鉴权门槛。

        为什么回 `{}` 而不是报错：`authMethods` 是空数组，说明"不需要鉴权"。客户端
        仍然调过来（有些客户端不管声明都先调一次）时，回错误会让它以为"鉴权方法
        不匹配，该换一个重试"，而真相是"这里根本没有这道门"。
        """
        log.debug("ACP authenticate 被调用（本服务端无需鉴权）：%s", params)
        return {}

    def _session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        """开一个会话。id 用递增序号：可读、可复现、不需要 uuid 库。"""
        self._counter += 1
        session_id = f"acp-{self._counter}"
        self.sessions[session_id] = {
            "cwd": str(params.get("cwd") or ""),
            "mcpServers": params.get("mcpServers") or [],
            # 空串表示"还没落库的会话"：第一次 prompt 时才建 Conversation。
            "conversationId": "",
        }
        return {"sessionId": session_id}

    def _session_load(self, params: dict[str, Any]) -> dict[str, Any]:
        """从已有会话 id 恢复一个 ACP 会话（**只读**）。

        ## 为什么"会话不存在"必须回错误码

        回一个空会话看起来更"友好"，实际是最坏的答案：客户端会以为**历史丢了**
        （而不是"这个 id 不对"），然后把这个空会话当真继续用——库里就多出一条
        跟原会话平行的分叉。所以这里回 `-32602`（参数里的会话 id 不存在，是客户端
        该改请求，不是服务端该重试）。

        ## 只读

        全程只有 `history_fn` 里的 `SELECT`。**不建 Conversation、不写 Message、
        不改 `updated_at`**——`session/load` 是"打开",不是"新建"。库里状态由测试
        逐字段断言（`tests/test_acp_session_load.py`）。
        """
        session_id = str(params.get("sessionId") or "")
        if not session_id:
            raise AcpError(INVALID_PARAMS, "session/load 缺少 sessionId")
        if self._history_fn is None:
            raise AcpError(
                METHOD_NOT_FOUND,
                "session/load 未接线：构造 AcpServer 时未注入 history_fn",
            )
        record = self._history_fn(session_id)
        if record is None:
            raise AcpError(INVALID_PARAMS, f"会话不存在：{session_id}")
        messages = [item for item in (record.get("messages") or []) if isinstance(item, dict)]
        cwd = str(params.get("cwd") or record.get("cwd") or "")
        # 只登记在内存里：之后的 session/prompt 会接在同一个 conversationId 上，
        # 这才是"续接"；库里一个字节都没动。
        self.sessions[session_id] = {
            "cwd": cwd,
            "mcpServers": params.get("mcpServers") or [],
            "conversationId": session_id,
            "loaded": True,
            "title": str(record.get("title") or ""),
        }
        self._replay(session_id, messages)
        return {
            "sessionId": session_id,
            "cwd": cwd,
            "title": str(record.get("title") or ""),
            # ACP 的 `LoadSessionResponse` 只约定"会话已就绪"，历史是靠通知回放的。
            # `messages` 是 Orbit 扩展：直接给客户端一份结构化历史，省得它自己拼通知。
            "messages": [
                {
                    "role": str(item.get("role") or "user"),
                    "content": [text_block(str(item.get("content") or ""))],
                }
                for item in messages
            ],
        }

    def _session_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """列最近的会话（Orbit 扩展：ACP 目前没有 `session/list`）。"""
        if self._list_fn is None:
            raise AcpError(
                METHOD_NOT_FOUND,
                "session/list 未接线：构造 AcpServer 时未注入 list_fn",
            )
        try:
            limit = int(params.get("limit") or 20)
        except (TypeError, ValueError):
            raise AcpError(INVALID_PARAMS, "limit 必须是整数") from None
        rows = self._list_fn(max(1, min(limit, 200)))
        return {
            "sessions": [
                {
                    "sessionId": str(row.get("id") or ""),
                    "title": str(row.get("title") or ""),
                    "updatedAt": str(row.get("updated_at") or ""),
                }
                for row in rows
            ]
        }

    def _session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        """跑一轮：边跑边推 `session/update`，最后才回 response。"""
        prompt = self._open_prompt(params)
        try:
            answer = self._ask(prompt)
        except PromptCancelled:
            return self._cancelled_result()
        finally:
            self._finish(prompt)
        return self._end_turn_result(answer)

    def _session_cancel(self, params: dict[str, Any]) -> None:
        """`session/cancel`（客户端通知）：把这一轮的取消标记置位。

        只置位、不等待：真正的收尾在那一轮自己的轮询点发生（事件泵每轮
        `emitter.check_cancelled()`），response 随之变成 `stopReason: "cancelled"`。
        """
        session_id = str(params.get("sessionId") or "")
        event = self._cancels.get(session_id)
        if event is None:
            # 还没有正在跑的一轮：记下来，下一次 prompt 一开始就取消。
            # 丢掉它等于"用户按了停止，agent 照样开跑"。
            self._cancel_requests.add(session_id)
            log.debug("session/cancel 早于 prompt 到达：%s", session_id)
            return
        event.set()

    def _session_close(self, params: dict[str, Any]) -> None:
        """关会话（Orbit 扩展）：只从内存里摘掉，**不动库**。

        幂等：重复关不报错。客户端的清理路径通常"能关的都关一遍"，对它报错只会让
        退出流程卡住，而且没有任何信息量。
        """
        session_id = str(params.get("sessionId") or "")
        self.sessions.pop(session_id, None)
        self._cancels.pop(session_id, None)
        self._cancel_requests.discard(session_id)

    def _shutdown(self, params: dict[str, Any]) -> None:
        self._closed = True
        return None

    # ── 一轮 prompt 的三个动作 ──────────────────────────────────────────────
    def _open_prompt(self, params: dict[str, Any]) -> _Prompt:
        session_id = str(params.get("sessionId") or "")
        if session_id not in self.sessions:
            # 用 INVALID_PARAMS 而不是 INTERNAL_ERROR：这是客户端用错了会话 id，
            # 不是服务端内部坏了——错误码要能让客户端区分"我该重试"还是"我该改请求"。
            raise AcpError(INVALID_PARAMS, f"未知会话：{session_id or '(空)'}")
        text = _prompt_text(params.get("prompt"))
        if not text:
            raise AcpError(INVALID_PARAMS, "prompt 里没有文本内容")
        if self._prompt_fn is None:
            raise AcpError(
                INTERNAL_ERROR, "没有接上 agent：构造 AcpServer 时未注入 prompt_fn"
            )
        cancel = threading.Event()
        if session_id in self._cancel_requests:
            # 取消比 prompt 早到（客户端连发两条，读线程先看见 cancel）。这是"别开始"
            # 的意思，不是可以丢掉的消息——所以立刻生效，否则用户按了停止却还在跑。
            self._cancel_requests.discard(session_id)
            cancel.set()
        self._cancels[session_id] = cancel
        return _Prompt(session_id, text, self._prompt_emitter(session_id, cancel), cancel)

    def _finish(self, prompt: _Prompt) -> None:
        """收尾：注销取消标记，并**把队列里剩下的帧写完**。

        这一步返回之前 response 绝不能发出去——这就是 ACP 的顺序要求。放在
        `finally` 里是因为失败路径也要收尾（否则写线程留着、队列里的帧丢了）。
        """
        self._cancels.pop(prompt.session_id, None)
        self._close_emitter(prompt.emitter)

    def _close_emitter(self, emitter: UpdateEmitter | None) -> None:
        """关掉 emitter 并等它写完（顺序保证的落点）。

        `in_flight` 那一支是刻意的：收尾常常发生在 `finally` 里，此时已经有一条
        更重要的异常在往上走（prompt 失败/被取消）。用"update 写不动了"把它盖掉，
        客户端看到的失败原因就完全跑偏了——所以只记一条 warning。
        """
        if emitter is None:
            return
        in_flight = sys.exc_info()[0] is not None
        try:
            emitter.close()
        except Exception as exc:
            if in_flight:
                log.warning("ACP update 收尾失败（已有异常在飞）：%s", exc)
                return
            raise

    def _ask(self, prompt: _Prompt) -> str:
        """调 agent（同步路径）：三参接线拿到 emitter 自己推流。"""
        return str(self._resolve(self._invoke(prompt)) or "")

    async def _ask_async(self, prompt: _Prompt) -> str:
        """调 agent（异步路径）：除了 `await` 之外与 `_ask` 完全一致。"""
        return str(await _maybe_await(self._invoke(prompt)) or "")

    def _invoke(self, prompt: _Prompt) -> Any:
        """按 `prompt_fn` 的签名形状决定怎么传 emitter（见 `_wants_emitter`）。

        两个分支必须都覆盖：`emitter` 是形参名就按关键字传（这样接线方可以把它放在
        任何位置、甚至只收关键字参数）；否则按位置传第三个。
        """
        fn = self._prompt_fn
        assert fn is not None  # `_open_prompt` 已保证
        if not _wants_emitter(fn):
            return fn(prompt.session_id, prompt.text)
        if "emitter" in inspect.signature(fn).parameters:
            return fn(prompt.session_id, prompt.text, emitter=prompt.emitter)
        return fn(prompt.session_id, prompt.text, prompt.emitter)

    def _update_emitter(
        self, session_id: str, cancel: threading.Event | None = None
    ) -> UpdateEmitter | None:
        """有输出通道就给一个 emitter；没有就**不发**（而不是假装发了）。"""
        if self._write is None:
            return None
        return UpdateEmitter(
            self._write,
            session_id=session_id,
            max_pending=self.max_pending,
            cancel_event=cancel,
        )

    def _prompt_emitter(
        self, session_id: str, cancel: threading.Event
    ) -> UpdateEmitter | None:
        """prompt 用的 emitter：还要求 `prompt_fn` 真的接受 emitter。

        两参接线（老的、以及 `tests/test_acp.py` 里的假 agent）拿不到 emitter，
        所以**根本不建**——建了也没人往里发，而一旦真往里发，客户端就会收到它没
        声明过（`streaming: false`）的通知，有些客户端会当成协议错误。
        """
        if not self.streaming:
            return None
        return self._update_emitter(session_id, cancel)

    def _replay(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """把历史逐条回放成 `user_message_chunk` / `agent_message_chunk`。

        ACP 的 `session/load` 语义是"把会话接回来"，历史由服务端用通知回放给客户端
        （客户端那边是一条空白的会话记录）。回放走同一条 emitter，因此同样受
        `close()` 的顺序保证约束：所有回放帧都在 load 的 response 之前。
        """
        emitter = self._update_emitter(session_id)
        if emitter is None:
            return
        try:
            for item in messages:
                role = str(item.get("role") or "")
                text = str(item.get("content") or "")
                emitter.emit(message_chunk(text, role="user" if role == "user" else "agent"))
        finally:
            self._close_emitter(emitter)

    def _end_turn_result(self, answer: str) -> dict[str, Any]:
        # ACP 的终止原因枚举：这一轮正常说完 = `end_turn`。
        return {"stopReason": "end_turn", "content": [text_block(answer)]}

    def _cancelled_result(self) -> dict[str, Any]:
        """用户按了停止：`cancelled` 是**正常终止原因**，不是错误。

        回一条 error 会让编辑器弹"出错了"，而用户明明自己按的停止。
        """
        return {"stopReason": "cancelled", "content": []}

    def _resolve(self, value: Any) -> Any:
        """`prompt_fn` 允许返回协程（agent 栈本身是异步的）。

        同步上下文里没有事件循环，就自己起一个跑完；已经在事件循环里还走同步
        `handle()` 是调用方的错——如实报出来，而不是偷偷再起一个循环（那会炸）。
        """
        if not inspect.isawaitable(value):
            return value
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(value)  # type: ignore[arg-type]
        raise AcpError(
            INTERNAL_ERROR,
            "在事件循环内调用了同步 handle()：请改用 ahandle()",
        )

    async def ahandle(self, request: Any) -> dict[str, Any] | None:
        """异步入口：在事件循环里调用时用它，`prompt_fn` 可以是协程。

        同步 `handle()` 里遇到协程只能自己起一个循环（见 `_resolve`）；已经在循环
        里再起一个会炸，所以这里显式给出异步版本，而不是让调用方猜。
        """
        if isinstance(request, dict) and request.get("method") == "session/prompt":
            params = request.get("params") or {}
            if not isinstance(params, dict):
                return _error(request.get("id"), INVALID_PARAMS, "params 必须是对象")
            try:
                result = await self._session_prompt_async(params)
            except AcpError as exc:
                return _error(request.get("id"), exc.code, exc.message, exc.data)
            except Exception as exc:  # 与同步路径同款兜底
                log.warning("ACP session/prompt 内部异常：%s", exc)
                return _error(
                    request.get("id"), INTERNAL_ERROR, f"{type(exc).__name__}: {exc}"
                )
            if "id" not in request:
                return None
            return {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
        return self.handle(request)

    async def _session_prompt_async(self, params: dict[str, Any]) -> dict[str, Any]:
        """`_session_prompt` 的异步版：除了 `await` 之外顺序完全一致。"""
        prompt = self._open_prompt(params)
        try:
            answer = await self._ask_async(prompt)
        except PromptCancelled:
            return self._cancelled_result()
        finally:
            self._finish(prompt)
        return self._end_turn_result(answer)

    # ── 分发 ───────────────────────────────────────────────────────────────
    def handle(self, request: Any) -> dict[str, Any] | None:
        """处理一条请求，返回响应（通知返回 None）。

        顺序按 JSON-RPC 2.0 规范来：**先判信封、再判方法、最后才执行**。
        反过来的话，一个畸形请求可能先被执行、再被报错。
        """
        if not isinstance(request, dict):
            return _error(None, INVALID_REQUEST, "请求必须是 JSON 对象")
        request_id = request.get("id")
        if request.get("jsonrpc") != "2.0":
            return _error(request_id, INVALID_REQUEST, 'jsonrpc 必须是 "2.0"')
        method = request.get("method")
        if not isinstance(method, str) or not method:
            return _error(request_id, INVALID_REQUEST, "缺少 method")
        params = request.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params 必须是对象")
        notification = "id" not in request
        handler = self._methods().get(method)
        try:
            if handler is None:
                # 标准错误码 -32601：客户端据此知道"这个方法这条路走不通"，
                # 而不是当成服务端崩了去重试。
                raise AcpError(METHOD_NOT_FOUND, f"不支持的方法：{method}")
            if method == "session/prompt" and request_id is not None:
                # 记下 id → 会话，`$/cancel_request` 才能按请求 id 取消（见 `_apply_cancel`）。
                self._request_sessions[request_id] = str(params.get("sessionId") or "")
            result = handler(params)
        except AcpError as exc:
            return None if notification else _error(request_id, exc.code, exc.message, exc.data)
        except UpdateOverflow as exc:
            # 背压顶爆：**如实回错误**，不要回一个看起来正常的半截回答。
            return None if notification else _error(request_id, INTERNAL_ERROR, str(exc))
        except Exception as exc:  # 兜底：任何未预料的异常都不能让服务循环死掉
            log.warning("ACP 方法 %s 内部异常：%s", method, exc)
            if notification:
                return None
            return _error(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        finally:
            if request_id is not None:
                self._request_sessions.pop(request_id, None)
        if notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # ── 传输 ───────────────────────────────────────────────────────────────
    def serve(self, stdin: TextIO, stdout: TextIO) -> int:
        """从 `stdin` 逐行读、向 `stdout` 逐行写，直到 EOF 或 `shutdown`。

        **输入输出都是参数**，所以测试可以直接塞两个 `StringIO` 跑完整往返，
        不需要起子进程、不需要 socket、不会碰真实终端。

        读 stdin 由一条独立线程做（见模块注释"线程模型"）：主线程堵在
        `handle(session/prompt)` 里的时候，`session/cancel` 也必须能被读到。
        读线程**一帧都不写**，所有写都在主线程——输出不会被两条线程撕开。
        """
        write = self._write
        if write is None:
            write = lambda frame: _write(stdout, frame)  # noqa: E731 - 就是个别名
            self._write = write
        incoming: queue.Queue[Any] = queue.Queue(maxsize=INPUT_QUEUE)
        reader = threading.Thread(
            target=self._read_lines,
            args=(stdin, incoming),
            name="acp-stdin",
            daemon=True,
        )
        reader.start()
        try:
            while True:
                item = incoming.get()
                if item is _EOF:
                    break
                if item is _PARSE_FAILURE:
                    # 解析失败必须**回一条错误**（id 为 null）：静默丢弃会让对端一直等。
                    write(_error(None, PARSE_ERROR, "无法解析的 JSON"))
                    continue
                batch = item if isinstance(item, list) else [item]
                for one in batch:
                    response = self.handle(one)
                    if response is not None:
                        write(response)
                if self._closed:
                    break
        finally:
            # 读线程可能还堵在 stdin 上（真实终端不会 EOF）：daemon + 超时 join，
            # 退出路径不能等一条永远不会来的输入。
            reader.join(timeout=2)
        return 0

    def _read_lines(self, stdin: TextIO, incoming: queue.Queue[Any]) -> None:
        """读线程：读一行 → 解析 → 入队；`session/cancel` 当场生效。

        取消不能走队列：此刻主循环正堵在 prompt 里，等它轮到这条消息，那一轮早就
        跑完了。置位是纯内存操作（`Event.set()`），跨线程安全。
        """
        try:
            for raw in stdin:
                line = raw.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    incoming.put(_PARSE_FAILURE)
                    continue
                if self._is_cancel(payload):
                    self._apply_cancel(payload)
                    continue
                incoming.put(payload)  # 队列满时**堵在这里**：对 stdin 的自然背压
        finally:
            incoming.put(_EOF)

    @staticmethod
    def _is_cancel(payload: Any) -> bool:
        return (
            isinstance(payload, dict)
            and "id" not in payload
            and payload.get("method") in {"session/cancel", "$/cancel_request"}
        )

    def _apply_cancel(self, payload: dict[str, Any]) -> None:
        params = payload.get("params") or {}
        if not isinstance(params, dict):
            return
        if payload.get("method") == "session/cancel":
            session_id = str(params.get("sessionId") or "")
        else:  # `$/cancel_request`：按请求 id 找会话
            session_id = self._request_sessions.get(params.get("requestId"), "")
        event = self._cancels.get(session_id)
        if event is None:
            self._cancel_requests.add(session_id)
            log.debug("取消请求早于 prompt 到达：%s", session_id)
            return
        event.set()


# ── CLI 接线 ───────────────────────────────────────────────────────────────
def _cli_prompt_fn(context: Context) -> Callable[..., str]:
    """把 ACP 的 `session/prompt` 接到真实 agent 上（**三参**，会推流）。

    **复用 `chat` 命令的栈**（`_build_stack` / `_submit` / `_answer_of`）而不是另起
    一套：那套东西（事件游标、终态判定、等待用户）已经踩过坑了，抄一遍就会有两份
    真相源。代价是依赖 chat 的私有函数——所以导入放在函数里，将来 chat 重构时这里
    会**当场**报错，而不是静默跑错。

    **绝不调用 `chat._one_turn`**：它在 `WAITING_USER` 时 `input()` 读 stdin，
    而 ACP 的 stdin 是协议通道——读一口就把下一条 JSON 请求吃掉了。
    """
    from app.cli.commands.chat import (
        _answer_of,
        _build_stack,
        _conversation,
        _submit,
    )

    runner, store, _summaries = _build_stack(context)
    conversations: dict[str, str] = {}

    def conversation_of(session_id: str) -> str:
        """ACP 会话 id → 库里 Conversation id。

        `session/load` 传进来的 id **就是** conversation id（那才是客户端手里
        唯一稳定的东西）；`session/new` 给的是 `acp-N`，第一次 prompt 时才建。
        """
        known = conversations.get(session_id)
        if known:
            return known
        existing = _existing_conversation(context, store, session_id)
        conversation_id = existing or _conversation(
            context, store, resume=False, title="ACP 会话"
        ).id
        conversations[session_id] = conversation_id
        return conversation_id

    def ask(session_id: str, text: str, emitter: UpdateEmitter | None = None) -> str:
        conversation_id = conversation_of(session_id)
        task_id = _submit(context, store, runner, conversation_id, text)
        try:
            state = _pump(context, store, task_id, emitter)
        except PromptCancelled:
            # 光把这一轮"看起来结束"是不够的：库里那条任务会继续跑到失败。
            # 真取消它，用户按停止才是真的停止。
            try:
                context.run(runner.cancel(task_id))
            except Exception as exc:  # 取消失败不该把"已取消"这件事变成报错
                log.warning("ACP 取消任务失败（%s）：%s", task_id, exc)
            raise
        if state["status"] == "WAITING_USER":
            # 没有审批 UI 可弹：把问题当成回答交回编辑器，让用户再发一条。
            return str(state.get("question") or "任务需要你的确认，请再发一条指令继续。")
        if state["status"] in {"FAILED", "CANCELLED"}:
            raise AcpError(INTERNAL_ERROR, str(state.get("error") or "任务未能完成"))
        return _answer_of(context, store, task_id) or str(state.get("text") or "")

    return ask


def _existing_conversation(context: Context, store: Any, session_id: str) -> str:
    """`session_id` 恰好是一条真实会话的 id 就返回它，否则空串。"""
    from app.storage import Conversation

    async def _do() -> str:
        row = await store.get(Conversation, session_id)
        return str(row.id) if row is not None else ""

    return str(context.run(_do()) or "")


def _pump(
    context: Context,
    store: Any,
    task_id: str,
    emitter: UpdateEmitter | None,
) -> dict[str, Any]:
    """按 `task_events` 游标推进到终态，同时把每条事件翻译成 `session/update`。

    为什么另写一份而不是复用 `chat._drive`：那份只往 `sys.stdout` 打字，而 ACP 要把
    **同一批事件**变成协议通知。给它加一个 emitter 参数会把 chat 的输出路径也搅进
    协议层（CLI 的 stdout 是给人看的，ACP 的 stdout 是给客户端解析的——这两件事
    绝不能合流）。两边共享的是事件类型名与终态集合，不是代码。

    返回 `{status, text, question, error}`，与 `chat._drive` 同形状。
    """
    from sqlalchemy import select

    from app.storage import Task, TaskEvent

    state: dict[str, Any] = {"status": "", "text": "", "question": "", "error": ""}
    cursor = 0

    async def _tick() -> tuple[list[Any], Any]:
        events = await store.rows(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id, TaskEvent.id > cursor)
            .order_by(TaskEvent.id)
        )
        return list(events), await store.get(Task, task_id)

    while True:
        events, task = context.run(_tick())
        for event in events:
            cursor = max(cursor, int(event.id))
            payload = event.payload or {}
            if event.event_type == "message_delta":
                state["text"] += str(payload.get("text") or "")
            if emitter is not None:
                # 背压/顺序/形状都在 emitter 里；这里只负责"把事件喂给它"。
                emitter.emit_event(event.event_type, payload)
        if task is not None:
            state["status"] = task.status
            state["error"] = task.error
            if task.status == "WAITING_USER":
                state["question"] = str((task.waiting or {}).get("question") or "")
        if emitter is not None:
            # 取消检查放在事件之后：先把已经产生的输出推完，再决定停不停。
            emitter.check_cancelled()
        if state["status"] in TERMINAL:
            return state
        # 事件循环跑在另一条线程上，主线程这里可以放心睡。
        time.sleep(POLL_SECONDS)


def _cli_history_fn(context: Context) -> Callable[[str], dict[str, Any] | None]:
    """`session/load` 的读库口：**只读**。

    刻意用 `context.store`（而不是 agent 栈里那个 Store）：载入历史不该拖起
    ToolRegistry / BrowserController / AgentRunner 一整套东西。WAL 已经由
    `_build_stack` 打开，两个连接同时读写同一个库没问题。
    """

    def load(session_id: str) -> dict[str, Any] | None:
        from sqlalchemy import select

        from app.storage import Conversation, Message

        async def _do() -> dict[str, Any] | None:
            store = context.store
            conversation = await store.get(Conversation, session_id)
            if conversation is None:
                return None
            rows = await store.rows(
                select(Message)
                .where(Message.conversation_id == session_id)
                .order_by(Message.created_at, Message.id)
            )
            return {
                "id": conversation.id,
                "title": conversation.title,
                "messages": [
                    {
                        "role": row.role,
                        "content": row.content,
                        "createdAt": row.created_at,
                    }
                    for row in rows
                ],
            }

        return context.run(_do())

    return load


def _cli_list_fn(context: Context) -> Callable[[int], list[dict[str, Any]]]:
    """`session/list` 的读库口：最近 `limit` 条会话（只读）。"""

    def list_recent(limit: int) -> list[dict[str, Any]]:
        from sqlalchemy import select

        from app.storage import Conversation

        async def _do() -> list[dict[str, Any]]:
            rows = await context.store.rows(
                select(Conversation).order_by(Conversation.updated_at.desc()).limit(limit)
            )
            return [
                {"id": row.id, "title": row.title, "updated_at": row.updated_at}
                for row in rows
            ]

        return list(context.run(_do()))

    return list_recent


def run_stdio(
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    *,
    server: AcpServer | None = None,
) -> int:
    """真实入口：把 `stdin` / `stdout` 接到服务端上跑，直到 EOF 或 `shutdown`。

    默认取 `sys.stdin` / `sys.stdout`（真客户端接上来时的样子），但**两者都可注入**
    ——测试塞两个 `StringIO` 就能跑完整回放，不启子进程、不碰终端。

    一个 ACP 客户端接上来会看到什么（完整剧本见模块 docstring）：握手 → 开会话 →
    一轮 prompt 期间收到一串无 `id` 的 `session/update` 通知 → 最后才收到那条
    `session/prompt` 的 response（`stopReason: end_turn`）。中途按停止发
    `session/cancel`，该轮 response 变成 `stopReason: cancelled`。
    """
    target = server or AcpServer()
    return target.serve(
        sys.stdin if stdin is None else stdin,
        sys.stdout if stdout is None else stdout,
    )


def run_acp(context: Context, args) -> int:
    """`orbit acp`：以 ACP 服务方式运行（stdio JSON-RPC）。"""
    if getattr(args, "version", False):
        context.out(f"ACP {PROTOCOL_VERSION}（{SERVER_NAME} {SERVER_VERSION}）")
        return 0
    try:
        prompt_fn = _cli_prompt_fn(context)
        history_fn = _cli_history_fn(context)
        list_fn = _cli_list_fn(context)
    except Exception as exc:  # 栈都搭不起来就别装服务，直接说清楚
        context.warn(f"ACP 启动失败（agent 栈未就绪）：{exc}")
        return 1
    if getattr(args, "check", False):
        context.out(
            "ACP 服务端就绪：initialize / authenticate / session/new / session/load / "
            "session/list / session/prompt（流式 session/update）/ session/cancel / "
            "session/close / shutdown"
        )
        return 0
    server = AcpServer(prompt_fn=prompt_fn, history_fn=history_fn, list_fn=list_fn)
    context.note("ACP 服务已启动（stdio JSON-RPC 2.0，带 session/update 流式通知）")
    return run_stdio(server=server)


register_handler("acp", run_acp)

__all__ = [
    "AcpError",
    "AcpServer",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "PROTOCOL_VERSION",
    "register_handler",
    "run_acp",
    "run_stdio",
]
