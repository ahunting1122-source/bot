"""ACP 服务端的最小实现（MATRIX 1.14）。

## ACP 是什么

ACP（Agent Client Protocol）是**编辑器 ↔ agent** 之间的一条 JSON-RPC 2.0 通道：
编辑器（Zed 等）负责界面与文件上下文，agent 负责干活。传输层最常见的就是
**stdio**——父进程起子进程，一行一条 JSON 消息。

## 本实现的范围（重要：这是最小实现，不是完整 ACP 规范）

已实现 4 个方法：

| 方法 | 作用 | 本实现做了什么 |
|---|---|---|
| `initialize` | 握手 | 回协议版本 + 服务信息 + **能力声明**（明确说"只支持 prompt 文本"） |
| `session/new` | 开会话 | 生成 `sessionId`，记住 `cwd` / `mcpServers`（暂不使用） |
| `session/prompt` | 发一轮提示 | 交给注入的 `prompt_fn`，**一次性**返回完整回答 |
| `shutdown` | 收尾 | 置关闭标记，`serve()` 读完当前行就退出 |

**没有实现**（不是"忘了"，是这一波不做，如实列在这里）：

* `session/update` **流式通知**——本实现等 agent 跑完再一次性返回。要流式得把
  `task_events` 的 `message_delta` 转成通知，那是另一套背压/顺序问题。
* `session/load` / `session/list`——历史会话的续接与列举。
* `session/request_permission`——工具执行前向编辑器要授权。Orbit 现在是完全访问
  档，没有审批分支可挂（`app/graph/nodes.py::gate` 的注释里写了原因）。
* `session/cancel`、`$/cancel_request`——中断一轮进行中的 prompt。
* `session/set_mode` / `session/set_model`——运行时切模式与模型。
* `fs/read_text_file` / `fs/write_text_file` / `terminal/*`——这些是**编辑器提供、
  agent 调用**的反向能力（编辑器当服务端）。本实现只做服务端单向应答。
* `authenticate`——ACP 的鉴权流程（Hermes 用 `--setup` 走终端鉴权）。

## 为什么自己写 JSON-RPC 而不用库

本机没有网络，装不了 `agent-client-protocol`。而且这一层要做的事很少：读一行、
判错、回一行。自己写反而能把**输入输出注入**（测试用 `StringIO`，不真起服务）
这件事做干净——见 `AcpServer.serve(stdin, stdout)`。
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
from collections.abc import Callable
from typing import Any, TextIO

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


class AcpServer:
    """stdio 上的最小 ACP 服务端。

    `prompt_fn(session_id, text) -> str` 是**唯一**的 agent 注入点：
    这样协议层可以完全脱离 agent 栈单测（测试传一个返回固定字符串的假函数，
    再用 `StringIO` 跑一轮真实收发，不启服务、不碰数据库）。
    """

    def __init__(
        self,
        *,
        prompt_fn: Callable[[str, str], Any] | None = None,
        protocol_version: str = PROTOCOL_VERSION,
        server_version: str = SERVER_VERSION,
    ) -> None:
        self._prompt_fn = prompt_fn
        self.protocol_version = protocol_version
        self.server_version = server_version
        self.sessions: dict[str, dict[str, Any]] = {}
        self._counter = 0
        self._closed = False

    # ── 方法表 ─────────────────────────────────────────────────────────────
    def _methods(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {
            "initialize": self._initialize,
            "session/new": self._session_new,
            "session/prompt": self._session_prompt,
            "shutdown": self._shutdown,
        }

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """握手。能力声明**照实写**：只支持文本 prompt、一次性返回。"""
        return {
            "protocolVersion": self.protocol_version,
            "serverInfo": {"name": SERVER_NAME, "version": self.server_version},
            "capabilities": {
                "promptCapabilities": {
                    "image": False,
                    "audio": False,
                    "embeddedContext": False,
                },
                # 明确不支持流式通知与权限请求（见模块 docstring 的"没有实现"）。
                "streaming": False,
                "permissions": False,
            },
        }

    def _session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        """开一个会话。id 用递增序号：可读、可复现、不需要 uuid 库。"""
        self._counter += 1
        session_id = f"acp-{self._counter}"
        self.sessions[session_id] = {
            "cwd": str(params.get("cwd") or ""),
            "mcpServers": params.get("mcpServers") or [],
        }
        return {"sessionId": session_id}

    def _session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
        session_id = str(params.get("sessionId") or "")
        if session_id not in self.sessions:
            # 用 INVALID_PARAMS 而不是 INTERNAL_ERROR：这是客户端用错了会话 id，
            # 不是服务端内部坏了——错误码要能让客户端区分"我该重试"还是"我该改请求"。
            raise AcpError(INVALID_PARAMS, f"未知会话：{session_id or '(空)'}")
        text = _prompt_text(params.get("prompt"))
        if not text:
            raise AcpError(INVALID_PARAMS, "prompt 里没有文本内容")
        answer = self._ask(session_id, text)
        return {
            # ACP 的终止原因枚举：这一轮正常说完 = `end_turn`。
            "stopReason": "end_turn",
            "content": [{"type": "text", "text": answer}],
        }

    def _shutdown(self, params: dict[str, Any]) -> None:
        self._closed = True
        return None

    def _ask(self, session_id: str, text: str) -> str:
        if self._prompt_fn is None:
            raise AcpError(
                INTERNAL_ERROR, "没有接上 agent：构造 AcpServer 时未注入 prompt_fn"
            )
        return str(self._resolve(self._prompt_fn(session_id, text)) or "")

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
            session_id = str(params.get("sessionId") or "")
            text = _prompt_text(params.get("prompt")) if isinstance(params, dict) else ""
            if session_id in self.sessions and text and self._prompt_fn is not None:
                try:
                    answer = await _maybe_await(self._prompt_fn(session_id, text))
                except AcpError as exc:
                    return _error(request.get("id"), exc.code, exc.message, exc.data)
                except Exception as exc:  # 与同步路径同款兜底
                    log.warning("ACP session/prompt 内部异常：%s", exc)
                    return _error(request.get("id"), INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
                if "id" not in request:
                    return None
                return {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "result": {
                        "stopReason": "end_turn",
                        "content": [{"type": "text", "text": str(answer or "")}],
                    },
                }
        return self.handle(request)

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
            result = handler(params)
        except AcpError as exc:
            return None if notification else _error(request_id, exc.code, exc.message, exc.data)
        except Exception as exc:  # 兜底：任何未预料的异常都不能让服务循环死掉
            log.warning("ACP 方法 %s 内部异常：%s", method, exc)
            if notification:
                return None
            return _error(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        if notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    # ── 传输 ───────────────────────────────────────────────────────────────
    def serve(self, stdin: TextIO, stdout: TextIO) -> int:
        """从 `stdin` 逐行读、向 `stdout` 逐行写，直到 EOF 或 `shutdown`。

        **输入输出都是参数**，所以测试可以直接塞两个 `StringIO` 跑完整往返，
        不需要起子进程、不需要 socket、不会碰真实终端。
        """
        for raw in stdin:
            line = raw.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                # 解析失败必须**回一条错误**（id 为 null）：静默丢弃会让对端一直等。
                _write(stdout, _error(None, PARSE_ERROR, "无法解析的 JSON"))
                continue
            batch = payload if isinstance(payload, list) else [payload]
            for item in batch:
                response = self.handle(item)
                if response is not None:
                    _write(stdout, response)
            if self._closed:
                break
        return 0


# ── CLI 接线 ───────────────────────────────────────────────────────────────
def _cli_prompt_fn(context: Context) -> Callable[[str, str], str]:
    """把 ACP 的 `session/prompt` 接到真实 agent 上。

    **复用 `chat` 命令的栈**（`_build_stack` / `_submit` / `_drive` / `_answer_of`）
    而不是另起一套：那套东西（事件游标、终态判定、等待用户）已经踩过坑了，
    抄一遍就会有两份真相源。代价是依赖 chat 的私有函数——所以导入放在函数里，
    将来 chat 重构时这里会**当场**报错，而不是静默跑错。

    **绝不调用 `chat._one_turn`**：它在 `WAITING_USER` 时 `input()` 读 stdin，
    而 ACP 的 stdin 是协议通道——读一口就把下一条 JSON 请求吃掉了。
    """
    from app.cli.commands.chat import (
        _answer_of,
        _build_stack,
        _conversation,
        _drive,
        _submit,
    )

    runner, store, _summaries = _build_stack(context)
    conversations: dict[str, str] = {}

    def ask(session_id: str, text: str) -> str:
        if session_id not in conversations:
            conversations[session_id] = _conversation(
                context, store, resume=False, title="ACP 会话"
            ).id
        task_id = _submit(context, store, runner, conversations[session_id], text)
        state = _drive(context, store, runner, task_id, stream=False)
        if state["status"] == "WAITING_USER":
            # 没有审批 UI 可弹：把问题当成回答交回编辑器，让用户再发一条。
            return str(state.get("question") or "任务需要你的确认，请再发一条指令继续。")
        if state["status"] in {"FAILED", "CANCELLED"}:
            raise AcpError(INTERNAL_ERROR, str(state.get("error") or "任务未能完成"))
        return _answer_of(context, store, task_id) or str(state.get("text") or "")

    return ask


def run_acp(context: Context, args) -> int:
    """`orbit acp`：以 ACP 服务方式运行（stdio JSON-RPC）。"""
    if getattr(args, "version", False):
        context.out(f"ACP {PROTOCOL_VERSION}（{SERVER_NAME} {SERVER_VERSION}）")
        return 0
    try:
        prompt_fn = _cli_prompt_fn(context)
    except Exception as exc:  # 栈都搭不起来就别装服务，直接说清楚
        context.warn(f"ACP 启动失败（agent 栈未就绪）：{exc}")
        return 1
    if getattr(args, "check", False):
        context.out("ACP 服务端就绪：initialize / session/new / session/prompt / shutdown")
        return 0
    context.note("ACP 服务已启动（stdio JSON-RPC 2.0）")
    return AcpServer(prompt_fn=prompt_fn).serve(sys.stdin, sys.stdout)


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
]
