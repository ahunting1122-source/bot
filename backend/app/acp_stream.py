"""ACP 的 `session/update` 流式通知层（MATRIX 1.14 续）。

## 为什么单独一个模块

`app/cli/commands/acp.py` 只该管"读一行 JSON、判错、回一行 JSON"——协议信封。
把"内部事件 → ACP 载荷"和"有界队列 + 写线程"一起塞进去，那个文件会长到没人愿意读，
而且**测试就没法只断言载荷形状**（会被 serve 循环、会话表、错误码一起搅进来）。
所以这里只放三件事：

1. **载荷构造器**（`message_chunk` / `thought_chunk` / `tool_call` /
   `tool_call_update` / `plan`）：纯函数，输入输出都是 dict；
2. **`UpdateEmitter`**：把载荷封成 JSON-RPC 通知、排队、由一条写线程真的写出去，
   并保证 `close()` 返回时"该写的都写完了"；
3. **`UpdateEmitter.emit_event`**：把 Orbit 内部的 `task_events` 事件类型翻译成
   ACP 载荷（这是"agent 说一句 → 编辑器看见一句"的落点）。

## ACP 的 `session/update` 长什么样

```json
{"jsonrpc": "2.0", "method": "session/update",
 "params": {"sessionId": "acp-1",
            "update": {"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": "你"}}}}
```

`update.sessionUpdate` 是判别字段（tagged union）。本模块产出这几种：

| `sessionUpdate` | 什么时候发 | 关键字段 |
|---|---|---|
| `agent_message_chunk` | agent 往外说了一段话 | `content`（ContentBlock） |
| `agent_thought_chunk` | agent 在思考（`reflection` 事件） | `content` |
| `user_message_chunk` | `session/load` 回放历史时把用户那条发回去 | `content` |
| `tool_call` | 要调一个工具了 | `toolCallId` / `title` / `kind` / `status=pending` |
| `tool_call_update` | 工具跑完了 | `toolCallId` / `status=completed\\|failed` / `content` |
| `plan` | 计划有变 | `entries[]` |

**刻意不发** `available_commands_update` / `current_mode_update`：Orbit 没有"斜杠命令
面板"与"运行模式切换"这两件事（`session/set_mode` 也没实现），发了就是撒谎——
客户端会照着我们说的去渲染一个永远空着的面板。

## 顺序保证（为什么必须有写线程 + `close()`）

ACP 的客户端是这么等一轮的：发出 `session/prompt` → 收一串 `session/update` 通知 →
**收到该请求的 response** 才算这一轮结束。于是有一条硬顺序：

    所有 update  ⊂  response 之前

这条顺序不能靠"调用方自觉"：agent 的事件来自另一条线程（`context.run` 跑在 CLI 循环
线程上），如果就地 `write()`，最后一帧可能还在管道里，response 已经冲出去了，
客户端就会把后面的输出丢掉。所以：

* 写动作全在**一条**写线程里做（FIFO，帧序 = 入队序）；
* `flush()` / `close()` 等到 `unfinished_tasks == 0` 才返回——也就是**最后一帧真的
  写进通道之后**才返回；
* `app/cli/commands/acp.py` 在构造 response 之前调用 `close()`，所以顺序是结构性的，
  不是约定。

## 背压：为什么是有界队列 + 抛错，而不是"等一等"或"丢帧"

* **丢帧**：客户端少收一段话，但这一轮照样"成功结束"。用户看到的是"agent 说到一半
  跳了"，而且没有任何信号说明丢了——这是最坏的一种错。
* **无限缓冲**：慢客户端（或卡死的管道）会让内存一路涨到 OOM，进程被杀，
  用户连"为什么"都看不到。
* **阻塞入队**：写阻塞会**顺着调用链卡住 agent 的事件循环**——模型已经吐完的词也
  送不进库，整轮一起僵住。

所以这里：队列有界（`max_pending`，默认 256 帧），`emit()` **不阻塞**，满了直接抛
`UpdateOverflow`。这个异常会一路冒到 `session/prompt` 的 response 里变成一条
JSON-RPC 错误——客户端明确知道"这一轮没说完，原因是我读得太慢"，而不是收到一个
看起来正常的半截回答。慢客户端该做的是加速读，不是让我们把内存吃光。
"""

from __future__ import annotations

import json
import queue
import threading
from collections.abc import Callable, Mapping, Sequence
from typing import Any

# ── 协议常量 ───────────────────────────────────────────────────────────────
#: JSON-RPC 方法名。ACP 把它当**通知**发：无 `id`，不期待回包。
SESSION_UPDATE = "session/update"

#: `update.sessionUpdate` 的取值（ACP 的 tagged union 判别字段）。
AGENT_MESSAGE_CHUNK = "agent_message_chunk"
AGENT_THOUGHT_CHUNK = "agent_thought_chunk"
USER_MESSAGE_CHUNK = "user_message_chunk"
TOOL_CALL = "tool_call"
TOOL_CALL_UPDATE = "tool_call_update"
PLAN = "plan"

#: ACP `ToolCallStatus`。
PENDING = "pending"
IN_PROGRESS = "in_progress"
COMPLETED = "completed"
FAILED = "failed"

#: ACP `PlanEntryStatus`。
PLAN_PENDING = "pending"
PLAN_IN_PROGRESS = "in_progress"
PLAN_COMPLETED = "completed"
PLAN_STATUSES = frozenset({PLAN_PENDING, PLAN_IN_PROGRESS, PLAN_COMPLETED})

#: ACP `PlanEntryPriority`。
PRIORITIES = frozenset({"high", "medium", "low"})

#: 默认队列上限。256 帧 ≈ 几十 KB：够吸收一次 burst（模型一次吐 80 字符的增量），
#: 又不至于让"客户端根本没在读"这件事被掩盖太久。
DEFAULT_MAX_PENDING = 256


# ── 载荷构造器（纯函数）──────────────────────────────────────────────────────
def text_block(text: str) -> dict[str, Any]:
    """ACP 的 ContentBlock（纯文本）。"""
    return {"type": "text", "text": text}


def notification(session_id: str, update: Mapping[str, Any]) -> dict[str, Any]:
    """把一条 update 载荷封成 `session/update` 通知。

    **绝不能带 `id`**：带上 id 就从通知变成请求，对端会等一个永远不来的响应，
    它的"待应答表"也会一路涨。这是 JSON-RPC 里最容易搞错的一处，所以单列成函数、
    并在测试里对每一帧断言 `"id" not in frame`。
    """
    return {
        "jsonrpc": "2.0",
        "method": SESSION_UPDATE,
        "params": {"sessionId": session_id, "update": dict(update)},
    }


def message_chunk(text: str, *, role: str = "agent") -> dict[str, Any] | None:
    """一段正文增量。空增量返回 `None`（**不发空帧**）。

    空帧的代价不小：客户端会闪一条空消息，还白占一格队列——慢客户端时这点浪费
    正好把你顶到 `UpdateOverflow`。返回 `None` 让调用方顺手 `if update:` 跳过。
    """
    if not text:
        return None
    kind = USER_MESSAGE_CHUNK if role == "user" else AGENT_MESSAGE_CHUNK
    return {"sessionUpdate": kind, "content": text_block(text)}


def thought_chunk(text: str) -> dict[str, Any] | None:
    """一段"思考"增量（Orbit 的 `reflection` 事件：自我检查、计划修正）。

    与 `agent_message_chunk` 分开是**有意义的**：ACP 客户端会把 thought 渲染成
    灰掉的旁白，而正文是用户要读的回答。混在一起等于让编辑器把内心独白当结论显示。
    """
    if not text:
        return None
    return {"sessionUpdate": AGENT_THOUGHT_CHUNK, "content": text_block(text)}


#: 工具名前缀 → ACP `ToolKind`。**猜**而不是查表：工具名来自 `ToolRegistry`，
#: 那里没有 kind 字段（改它就是动别人的文件）。猜错的代价很小——`kind` 只影响
#: 编辑器的图标与分组，不影响执行；猜不出来落 `other`（ACP 的兜底值）比乱标诚实。
_TOOL_KIND_PREFIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("read", "cat", "list", "view", "open", "load"), "read"),
    (("write", "edit", "patch", "create", "append", "save"), "edit"),
    (("search", "grep", "find", "glob", "query"), "search"),
    (("shell", "bash", "pwsh", "powershell", "run", "exec", "python", "code"), "execute"),
    (("fetch", "web", "http", "curl", "browse", "browser"), "fetch"),
    (("delete", "remove", "rm"), "delete"),
    (("move", "rename", "copy"), "move"),
    (("think", "plan", "reason"), "think"),
)


def tool_kind(name: str) -> str:
    """按工具名猜 ACP 的 `ToolKind`（read/edit/search/execute/fetch/…）。

    两轮匹配：先看**开头**（`browser_open` 是"打开浏览器"，不是"打开文件"），
    再看 `_` 分隔的**后半截**（`web_search`、`file_read` 这类命名）。只做一轮
    "包含"匹配会把 `browser_open` 判成 read——因为 `_open` 也在 read 的前缀表里。
    """
    lowered = (name or "").lower()
    if not lowered:
        return "other"
    for prefixes, kind in _TOOL_KIND_PREFIXES:
        if any(lowered.startswith(prefix) for prefix in prefixes):
            return kind
    for prefixes, kind in _TOOL_KIND_PREFIXES:
        if any(f"_{prefix}" in lowered for prefix in prefixes):
            return kind
    return "other"


def tool_call(
    tool_call_id: str,
    name: str,
    *,
    args: Mapping[str, Any] | None = None,
    status: str = PENDING,
    title: str | None = None,
) -> dict[str, Any]:
    """工具调用**开始**。`toolCallId` 必须与随后的 `tool_call_update` 一致，
    否则编辑器会把"开始"和"结束"渲染成两条互不相干的记录。"""
    update: dict[str, Any] = {
        "sessionUpdate": TOOL_CALL,
        "toolCallId": tool_call_id,
        "title": title or name or "工具",
        "kind": tool_kind(name),
        "status": status,
    }
    if args:
        update["rawInput"] = dict(args)
    return update


def tool_call_update(
    tool_call_id: str,
    *,
    status: str = COMPLETED,
    text: str = "",
    title: str | None = None,
) -> dict[str, Any]:
    """工具调用**结束**（或进度更新）。"""
    update: dict[str, Any] = {
        "sessionUpdate": TOOL_CALL_UPDATE,
        "toolCallId": tool_call_id,
        "status": status,
    }
    if title:
        update["title"] = title
    if text:
        # ACP 的 ToolCallContent 判别式：`content` 里包一个普通 ContentBlock。
        update["content"] = [{"type": "content", "content": text_block(text)}]
    return update


def plan(entries: Sequence[Any], *, priority: str = "medium") -> dict[str, Any] | None:
    """计划列表。`entries` 接受字符串或 `{content,status,priority}` 形状的 dict。

    空计划返回 `None`：发一个 `entries: []` 会把编辑器里已经显示的计划**清空**，
    那不是"没有计划"，那是"计划被撤销了"。
    """
    items: list[dict[str, Any]] = []
    for entry in entries:
        if isinstance(entry, Mapping):
            content = str(
                entry.get("content") or entry.get("step") or entry.get("text") or ""
            ).strip()
            status = str(entry.get("status") or PLAN_PENDING)
            entry_priority = str(entry.get("priority") or priority)
        else:
            content, status, entry_priority = str(entry).strip(), PLAN_PENDING, priority
        if not content:
            continue
        if status not in PLAN_STATUSES:
            status = PLAN_PENDING
        if entry_priority not in PRIORITIES:
            entry_priority = priority if priority in PRIORITIES else "medium"
        items.append({"content": content, "status": status, "priority": entry_priority})
    if not items:
        return None
    return {"sessionUpdate": PLAN, "entries": items}


def result_text(result: Any, *, limit: int = 2000) -> str:
    """把工具结果压成一小段给编辑器看。

    **必须截断**：这段文本会直接渲染进会话流，一个网页正文（几十 KB）能把客户端
    卡住。完整结果在库里（`tool_calls.result`），编辑器真要的话去查，别塞进通知。
    """
    if result is None:
        return ""
    if isinstance(result, str):
        text = result
    else:
        try:
            text = json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):  # 有不可序列化的值：退回 str()，别再炸一次
            text = str(result)
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def result_status(result: Any) -> str:
    """工具结果的 `status` 字段 → ACP `ToolCallStatus`。

    `refused` 也算 failed：对用户来说"被策略拒绝"和"执行失败"一样是"这件事没做成"，
    编辑器该显示成红色而不是绿色。
    """
    if isinstance(result, Mapping) and str(result.get("status") or "") in {
        "error",
        "refused",
        "failed",
    }:
        return FAILED
    return COMPLETED


# ── 异常 ───────────────────────────────────────────────────────────────────
class UpdateOverflow(RuntimeError):
    """有界队列满了：客户端读得太慢。**不丢帧**，如实报错。"""


class PromptCancelled(Exception):
    """这一轮 prompt 被 `session/cancel` 打断。

    它不是"错误"：ACP 里对应的是 `stopReason: "cancelled"` 这个**正常终止原因**。
    单独一个异常类型，就是为了让协议层能把"用户按了停止"和"跑挂了"分开处理。
    """


# ── 事件翻译 + 有界队列发射器 ────────────────────────────────────────────────
class UpdateEmitter:
    """把 update 载荷排进有界队列，由一条写线程真的写出去。

    `write` 是**唯一**的出口，测试传一个记录器就能断言"发出去的每一帧形状与顺序"：

    ```python
    frames: list[dict] = []
    emitter = UpdateEmitter(frames.append, session_id="acp-1")
    emitter.emit(message_chunk("你好"))
    emitter.close()                      # 返回时 frames 里一定已经有那一帧
    assert frames[0]["method"] == "session/update"
    ```

    `cancel_event` 是这一轮 prompt 的取消标记（`session/cancel` 置位）。放在这里
    而不是另开一个参数：两者都是"这一轮 prompt 的运行时句柄"，一起下发才不会被
    某一方忘了传。
    """

    def __init__(
        self,
        write: Callable[[dict[str, Any]], None],
        *,
        session_id: str = "",
        max_pending: int = DEFAULT_MAX_PENDING,
        cancel_event: threading.Event | None = None,
    ) -> None:
        if max_pending < 1:
            raise ValueError("max_pending 必须 ≥ 1：0 意味着第一帧就必然溢出")
        self._write = write
        self.session_id = session_id
        self.max_pending = max_pending
        self.cancel_event = cancel_event
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue(maxsize=max_pending)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._error: BaseException | None = None
        self._closed = False
        #: 成功入队的帧数。
        self.sent = 0
        #: 真正写进通道的帧数。与 `sent` 的差额就是"没写出去"的部分——正常收尾时
        #: 两者必须相等，不等就说明有帧被吞了。
        self.written = 0
        #: 工具调用配对：`tool_started` 分配 id 入栈，`tool_finished` 按名出栈。
        self._tool_seq = 0
        self._open_tools: list[tuple[str, str]] = []

    # ── 发送 ───────────────────────────────────────────────────────────────
    def emit(self, update: Mapping[str, Any] | None) -> bool:
        """发一条 update。`None` 表示"这条事件没有对应的 ACP 表示"，直接跳过。

        返回是否真的入了队——调用方（事件泵）用它统计，不用自己猜。
        """
        if update is None:
            return False
        if self._closed:
            raise RuntimeError("emitter 已关闭：这一轮的 update 不能再发")
        self._ensure_started()
        frame = notification(self.session_id, update)
        try:
            self._queue.put_nowait(frame)
        except queue.Full:
            raise UpdateOverflow(
                f"session/update 队列已满（{self.max_pending} 帧未写出）："
                "客户端读得太慢，本轮中止——不静默丢帧"
            ) from None
        self.sent += 1
        return True

    def emit_event(self, event_type: str, payload: Mapping[str, Any] | None = None) -> bool:
        """把一条 `task_events` 事件翻译成 ACP update 并发出去。

        这是"内部事件 → 协议载荷"的**唯一**翻译点：`app/graph/nodes.py` 与
        `app/tools.py` 里写的是 `message_delta` / `tool_started` / `tool_finished` /
        `reflection`，ACP 那边认的是 `agent_message_chunk` / `tool_call` / …。
        翻译放这里，是因为改事件名的人只会看事件的生产者，不会看协议层。
        """
        data: Mapping[str, Any] = payload or {}
        if event_type == "message_delta":
            return self.emit(message_chunk(str(data.get("text") or "")))
        if event_type in {"reflection", "thinking"}:
            notes = str(data.get("notes") or data.get("text") or "")
            return self.emit(thought_chunk(notes))
        if event_type in {"tool_started", "tool_call_started"}:
            name = str(data.get("tool") or data.get("name") or "工具")
            call_id = self._open_tool(name)
            args = data.get("args") if isinstance(data.get("args"), Mapping) else None
            return self.emit(tool_call(call_id, name, args=args))
        if event_type in {"tool_finished", "tool_call_finished", "tool_result"}:
            name = str(data.get("tool") or data.get("name") or "工具")
            call_id = self._close_tool(name)
            result = data.get("result")
            return self.emit(
                tool_call_update(
                    call_id,
                    status=result_status(result),
                    text=result_text(result),
                )
            )
        if event_type in {"plan", "plan_updated"}:
            entries = data.get("entries") or data.get("steps") or []
            if isinstance(entries, Sequence) and not isinstance(entries, (str, bytes)):
                return self.emit(plan(entries))
            return False
        # 其余事件（task_started / waiting_user / task_completed / chunks_indexed …）
        # 在 ACP 里**没有对应物**。硬塞成 `plan` 或正文会让客户端显示假信息，
        # 所以如实不发。
        return False

    # ── 收尾 ───────────────────────────────────────────────────────────────
    def flush(self, timeout: float | None = None) -> None:
        """等队列里已入队的帧**全部写完**。

        这就是"response 必须排在所有 update 之后"这条顺序保证的落点：调用方在这里
        返回之后再写 response，顺序就是结构性的。
        """
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                if not self._queue.all_tasks_done.wait(timeout):
                    raise UpdateOverflow(
                        f"写端 {timeout}s 内没消化完队列（还剩 "
                        f"{self._queue.unfinished_tasks} 帧）：输出通道被卡住了"
                    )
        self._raise_if_failed()

    def close(self, timeout: float = 5.0) -> None:
        """停写线程并等它退干净。**幂等**：收尾路径可能从多个分支走到这里。"""
        if self._closed:
            self._raise_if_failed()
            return
        self._closed = True
        if self._thread is not None:
            try:
                self._queue.put(None, timeout=timeout)
            except queue.Full:
                raise UpdateOverflow(
                    f"收尾时队列仍满（{self.max_pending} 帧）：写端没有在消费"
                ) from None
            self._thread.join(timeout)
            if self._thread.is_alive():
                raise UpdateOverflow(f"写线程 {timeout}s 内没有退出：输出通道被卡住")
        self._raise_if_failed()

    def __enter__(self) -> UpdateEmitter:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    # ── 取消 ───────────────────────────────────────────────────────────────
    @property
    def cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def check_cancelled(self) -> None:
        """被 `session/cancel` 打断就抛 `PromptCancelled`。事件泵每轮调用一次。"""
        if self.cancelled:
            raise PromptCancelled("这一轮被 session/cancel 打断")

    # ── 内部 ───────────────────────────────────────────────────────────────
    @property
    def pending(self) -> int:
        """队列里还没被写线程取走的帧数（测试与诊断用）。"""
        return self._queue.qsize()

    def _ensure_started(self) -> None:
        if self._thread is not None:
            return
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._drain,
                name=f"acp-update-{self.session_id or 'anon'}",
                daemon=True,
            )
            self._thread.start()

    def _drain(self) -> None:
        """写线程主体：FIFO 取帧 → 写 → `task_done()`。

        `task_done()` 放在 `finally`：写失败时如果漏了它，`flush()` 会永远等下去
        （队列计数不归零），表现是"agent 跑完了但 ACP 不回应"——最难查的一种挂死。
        """
        while True:
            frame = self._queue.get()
            try:
                if frame is None:  # `close()` 放的哨兵
                    return
                if self._error is None:
                    self._write(frame)
                    self.written += 1
                # 已经写失败过就只排空不写：错误已记在 `_error`，`flush/close` 会抛，
                # 但队列必须继续排空，否则调用方卡在 flush 里。
            except BaseException as exc:  # 写端异常不能让写线程悄悄死掉
                if self._error is None:
                    self._error = exc
            finally:
                self._queue.task_done()

    def _raise_if_failed(self) -> None:
        if self._error is not None:
            raise UpdateOverflow(f"写 session/update 失败：{self._error!r}") from self._error

    def _open_tool(self, name: str) -> str:
        self._tool_seq += 1
        call_id = f"{self.session_id or 'acp'}:tool:{self._tool_seq}"
        self._open_tools.append((name, call_id))
        return call_id

    def _close_tool(self, name: str) -> str:
        """按名找最近一次未关闭的同名调用；找不到就退到最近一次任意调用。

        为什么要"退到任意"：事件游标可能从**中间**开始（`session/load` 之后接着跑、
        或任务在别的进程里已经开始），这时会先看到 `tool_finished` 而没有对应的
        `tool_started`。发一个新 id 的 update 至少能让编辑器把这条结果标成完成；
        直接不发会丢掉"工具跑完了"这个信息。
        """
        for index in range(len(self._open_tools) - 1, -1, -1):
            if self._open_tools[index][0] == name:
                return self._open_tools.pop(index)[1]
        if self._open_tools:
            return self._open_tools.pop()[1]
        return self._open_tool(name)


__all__ = [
    "AGENT_MESSAGE_CHUNK",
    "AGENT_THOUGHT_CHUNK",
    "COMPLETED",
    "DEFAULT_MAX_PENDING",
    "FAILED",
    "IN_PROGRESS",
    "PENDING",
    "PLAN",
    "PLAN_COMPLETED",
    "PLAN_IN_PROGRESS",
    "PLAN_PENDING",
    "PromptCancelled",
    "SESSION_UPDATE",
    "TOOL_CALL",
    "TOOL_CALL_UPDATE",
    "USER_MESSAGE_CHUNK",
    "UpdateEmitter",
    "UpdateOverflow",
    "message_chunk",
    "notification",
    "plan",
    "result_status",
    "result_text",
    "text_block",
    "thought_chunk",
    "tool_call",
    "tool_call_update",
    "tool_kind",
]
