"""插件 hook 事件注册表：17 个事件的登记、排序、发射与错误隔离。

**为什么要有这一层**：插件最需要的能力不是"加一个工具"，而是"在 agent 生命周期的
某个时刻插一脚"（记审计、改工具结果、压缩前抢救记忆）。Hermes 把这件事做成
``ctx.register_hook(event, fn)`` —— 17 个事件名是**宿主与插件之间的契约**：插件侧
写错事件名必须立刻报错，不能静默丢弃（静默丢弃 = 插件作者以为挂上了，实际永远不触发，
是这类系统里最难查的一类 bug）。所以 :meth:`HookRegistry.register` 对未知事件
**抛** :class:`UnknownHookEvent`，而不是记个 warning 就算了。

## 17 个事件：哪些是实证、哪些是推断

依据 `handoff/hermes-parity/research/05-plugin-gateway-layer.md §1.5.2`（10 个
`plugin.yaml` 的 `hooks:` 声明 + 全部 `ctx.register_hook("...")` 调用点的并集）与
`09-core-loop-layer.md §8.3`（本机可证 15 个 + 上游候选 2 个）。

- :data:`HOOK_EVENTS`（17 个）：**全部实证**。每个都能指到具体证据（内置插件
  manifest 的字面声明，或随包源码里的调用点），证据见 :data:`EVENT_EVIDENCE`
  与每条注释里的出处。
- :data:`INFERRED_EVENTS`（2 个）：**推断**。``VALID_HOOKS`` 定义在 PYZ 内的
  ``hermes_cli/plugins.py``，本机读不到（exe 字节扫描 0 命中），所以"精确的 17 项"
  无法硬证；09 §8.3 给出两个最可能的补齐候选（``post_compaction`` / ``pre_verify``）。
  它们**不计入** 17，单独登记为"预留事件"——允许注册、允许发射，但在
  ``orbit plugins capabilities`` 里明确标 ``推断``，不许冒充实证。

注意 09 与 05 的口径不同，这里两个都保留、都不隐瞒：09 的"15 个实证"只统计了
observability 两个 manifest + 源码调用点；05 的并集多出 ``on_pre_compress``
（byterover 唯一使用）与 ``transform_tool_result``（security-guidance 唯一使用）——
这两条同样是 manifest 里的字面声明，所以在本模块里按实证处理。

## 接线点在哪、为什么本轮没接

本模块只提供**接口**，不改调用方（``app/graph/*``、``app/agent.py``、``app/summaries.py``
是 W10 波次的禁改区：改它们会让基线测试的图/上下文行为整体漂移，且会与
"只新建文件"的交付约束冲突）。真正要插的五处见 :data:`WIRING_POINTS`：

| 事件 | 应该插在哪 | 插进去要做什么 |
|---|---|---|
| ``pre_tool_call`` | ``app/graph/nodes.py::Nodes.execute``（工具派发前） | ``emit("pre_tool_call", {"tool": name, "args": args})``，非空返回可决定拦截 |
| ``post_tool_call`` | 同上，工具返回之后 | ``emit("post_tool_call", {"tool": name, "result": result})`` |
| ``session_start`` | ``app/agent.py::AgentRunner.run``（任务开跑） | ``emit("session_start", {"task_id": ...})`` |
| ``session_end`` | ``app/graph/nodes.py::Nodes.finalize``（终态收尾） | ``emit("session_end", {"status": ...})`` |
| ``pre_compaction`` | ``app/summaries.py::SummaryService._run``（真正压缩处） | ``emit("pre_compaction", {"conversation_id": ...})`` |

接线的正确姿势是"取一个 :data:`DEFAULT_HOOKS` 之外注入的注册表"——所以注册表是
**对象**而不是模块级全局单例被各处 import 死：宿主把同一个 ``HookRegistry`` 实例
递给插件上下文与节点，测试里换一个新实例即可隔离。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("orbit.hooks")

#: hook 函数签名：一个 payload 字典进，任意可等待/普通返回值出。
Hook = Callable[[dict[str, Any]], Any]

# ── 事件表 ──────────────────────────────────────────────────────────────────

#: 17 个实证事件（05 §1.5.2 的并集）。顺序照"生命周期 → LLM → HTTP → 工具 → 审批
#: → 子 agent"的推进顺序排，便于人读；**顺序不承载语义**。
HOOK_EVENTS: tuple[str, ...] = (
    # 会话生命周期
    "on_session_start",     # 实证：nemo_relay/plugin.yaml
    "on_session_end",       # 实证：langfuse + nemo_relay manifest
    "on_session_finalize",  # 实证：langfuse + nemo_relay manifest
    "on_session_reset",     # 实证：nemo_relay/plugin.yaml（/new、/reset）
    "on_pre_compress",      # 实证：byterover/plugin.yaml（全仓唯一使用者）
    # 模型调用
    "pre_llm_call",         # 实证：两个 observability manifest + hook_output_spill 文档
    "post_llm_call",        # 实证：同上
    # HTTP 请求层
    "pre_api_request",      # 实证：同上
    "post_api_request",     # 实证：同上
    "api_request_error",    # 实证：同上
    # 工具调用
    "pre_tool_call",        # 实证：两个 manifest + security-guidance / disk-cleanup
    "post_tool_call",       # 实证：同上
    "transform_tool_result",  # 实证：security-guidance/plugin.yaml（全仓唯一使用者）
    # 审批流
    "pre_approval_request",   # 实证：approval.py:172/4100/4466/4563/4704/5443
    "post_approval_response",  # 实证：approval.py:181/4111/4497/4616/4720/4803
    # 子 agent
    "subagent_start",       # 实证：delegate_tool.py:2283-2294
    "subagent_stop",        # 实证：delegate_tool.py:3778-3792
)

#: 【推断】两个候选（09 §8.3）。``VALID_HOOKS`` 在 PYZ 里读不到，所以它们不是实证；
#: 单独登记是为了让"照上游写的插件"不会因为事件名不认识而当场炸掉，同时不谎称实证。
INFERRED_EVENTS: tuple[str, ...] = (
    "post_compaction",  # 推断：每次提交压缩之后的观察者事件（与 sessions.compression_* 字段对应）
    "pre_verify",       # 推断：编辑代码后准备收尾时触发，可返回 {"action": "continue"} 续跑
)

#: 事件 → 证据等级。``manifest`` = 实证（plugin.yaml 字面 / 随包源码调用点），
#: ``inferred`` = 推断（只在上游 ``VALID_HOOKS`` 里有旁证）。
EVENT_EVIDENCE: dict[str, str] = {
    **{event: "manifest" for event in HOOK_EVENTS},
    **{event: "inferred" for event in INFERRED_EVENTS},
}

#: 事件 → 中文语义（``orbit plugins capabilities`` 与 ``doctor`` 直接展示）。
EVENT_NOTES: dict[str, str] = {
    "on_session_start": "会话开始",
    "on_session_end": "会话结束",
    "on_session_finalize": "会话终局化（终态收尾）",
    "on_session_reset": "会话被 /new、/reset 清空",
    "on_pre_compress": "上下文压缩前",
    "pre_llm_call": "模型调用前（可返回 context 注入）",
    "post_llm_call": "模型调用后",
    "pre_api_request": "每次 HTTP 请求前",
    "post_api_request": "每次 HTTP 请求后",
    "api_request_error": "HTTP 请求失败",
    "pre_tool_call": "工具执行前（可决定拦截）",
    "post_tool_call": "工具执行后",
    "transform_tool_result": "改写工具结果（非阻塞注入）",
    "pre_approval_request": "审批弹窗发出前",
    "post_approval_response": "审批有结论后",
    "subagent_start": "子 agent 启动",
    "subagent_stop": "子 agent 结束",
    "post_compaction": "上下文压缩提交后（推断）",
    "pre_verify": "编辑代码后准备收尾时（推断）",
}

#: 别名 → 实名。**为什么要有别名**：任务书与 Orbit 内部文案用短名
#: （``session_start`` / ``pre_compaction``），而 Hermes 的实名带 ``on_`` 前缀。
#: 两种写法都收，但**统一归一到实名再存**，于是"插件 A 用短名注册、宿主用实名发射"
#: 依然能触发——否则这就是一个只会静默失效的坑。
EVENT_ALIASES: dict[str, str] = {
    "session_start": "on_session_start",
    "session_end": "on_session_end",
    "session_finalize": "on_session_finalize",
    "session_reset": "on_session_reset",
    "pre_compaction": "on_pre_compress",
}

#: 本轮**必须落地接口**的 5 个调用点（值 = 应该插进去的位置）。
#: 之所以只列 5 个：这五个覆盖了 Orbit 主循环的四个边界（任务起、工具前后、压缩前、
#: 任务终），其余 12 个事件的调用点分布在 gateway/审批/委派层，Orbit 还没有那些层。
WIRING_POINTS: dict[str, str] = {
    "pre_tool_call": "app/graph/nodes.py::Nodes.execute（工具派发前）",
    "post_tool_call": "app/graph/nodes.py::Nodes.execute（工具返回后）",
    "session_start": "app/agent.py::AgentRunner.run（任务开跑）",
    "session_end": "app/graph/nodes.py::Nodes.finalize（终态收尾）",
    "pre_compaction": "app/summaries.py::SummaryService._run（真正压缩处）",
}

#: 全部可用事件（实证 17 + 推断 2）。
ALL_EVENTS: tuple[str, ...] = HOOK_EVENTS + INFERRED_EVENTS

#: **当前真有发射点**的事件。其余事件订阅了**不会触发**（MATRIX 13.5）。
#:
#: 为什么把它和 `HOOK_EVENTS` 并列：**清单里有名字 = 承诺它会触发**。
#: 只写「17 个里 4 个能用」这种散文，第三方开发者仍不知道**哪 4 个**能用；
#: 这个集合是可编程的契约——插件可以在注册前先 `if ev in EMITTED_EVENTS`。
#:
#: 目前 12 个发射点：
#:   - `pre_llm_call` / `post_llm_call`（`graph/nodes.py::ask_model` 每次**模型调用**，
#:     含重试与压缩重来）
#:   - `pre_api_request` / `post_api_request` / `api_request_error`
#:     （`graph/nodes.py::stream_model` 每次**真实 HTTP 请求**；重试三次就有三个 span，
#:     用 `llm_call` 只能看到一个。`api_request_error` **不含取消**——用户主动中断
#:     不该算成 provider 故障，否则污染错误率）
#:   - `pre_tool_call` / `post_tool_call`（`graph/nodes.py::run_tool` 每次调用）
#:   - `on_session_start` / `on_session_end`（`agent.py` 任务起止；`end` 在 `finally` 里）
#:   - `on_session_finalize`（`graph/nodes.py::finalize`；与 `on_session_end` 分开，
#:     才能区分「跑完并收尾」与「跑完但没收尾」）
#:   - `subagent_start` / `subagent_stop`（`delegate.py::_execute`）
#:   - `pre_approval_request` / `post_approval_response`（`tools.py::_approve_command`；
#:     **审批审计是事后追责的唯一凭据**。`outcome` 原样上报三态——**不要压成布尔**，
#:     否则「超时」会被读成「用户拒绝」，而那两件事的处置完全不同）
#:   - `on_pre_compress`（`graph/nodes.py::think`，**压缩之前**发——压缩之后原文就没了，
#:     事后钩子只能看到摘要结果、看不到丢了什么）
#:   - `transform_tool_result`（`graph/nodes.py::digest`，**治理之后**发——订阅方看到的
#:     是模型真正拿到的那份，不是原始结果）
#:
#: **只剩 `on_session_reset` 没有发射点，而且这不是钩子缺口、是功能缺口**：
#: Orbit 没有「会话重置」这个动作（`orbit sessions` 有 `delete` / `archive`，但没有
#: `/new` / `/reset` 那种"清空当前会话继续聊"的语义）。**给它编一个发射点
#: 会把 `delete` 谎报成 `reset`**，订阅方会收到一个名字对、语义错的事件——
#: 那比没有更糟。要补它得先实现重置功能。
EMITTED_EVENTS = frozenset(
    {
        "pre_llm_call",
        "post_llm_call",
        "pre_api_request",
        "post_api_request",
        "api_request_error",
        "pre_tool_call",
        "post_tool_call",
        "on_session_start",
        "on_session_end",
        "on_session_finalize",
        "subagent_start",
        "subagent_stop",
        "pre_approval_request",
        "post_approval_response",
        "on_pre_compress",
        "transform_tool_result",
    }
)


class UnknownHookEvent(KeyError):
    """注册/发射了一个不在契约里的事件名。

    故意抛而不是记 warning：事件名拼错是**静默失效**的典型来源（插件作者以为
    挂上了，运行期永远不触发），只有当场报错才能在 ``orbit plugins doctor`` 里
    把它变成一条可修的错。
    """

    def __init__(self, event: str) -> None:
        self.event = event
        super().__init__(
            f"未知 hook 事件：{event!r}。可用事件（{len(ALL_EVENTS)} 个）："
            f"{'、'.join(ALL_EVENTS)}"
        )


def normalize_event(event: str) -> str:
    """别名归一 + 校验。返回实名；不认识则抛 :class:`UnknownHookEvent`。"""
    name = str(event or "").strip()
    name = EVENT_ALIASES.get(name, name)
    if name not in EVENT_EVIDENCE:
        raise UnknownHookEvent(event)
    return name


# ── 结果对象 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class HookResult:
    """一次 hook 调用的结果。``error`` 非空 = 这个 hook 抛了，但**其它 hook 照跑**。"""

    event: str
    name: str
    result: Any = None
    error: str = ""
    #: True = 这是个异步 hook，在"已有运行中事件循环"的同步 ``emit`` 里被挂成任务，
    #: 结果不会回填到本次调用（要结果请 ``await aemit(...)``）。
    deferred: bool = False


@dataclass(frozen=True)
class HookFailure:
    """被隔离掉的一次异常（记在注册表上，供 ``doctor`` / 日志排查）。"""

    event: str
    name: str
    error: str


@dataclass
class _Entry:
    """内部登记项。``order`` 是注册序号，用来给同优先级的 hook 保 FIFO 顺序。"""

    fn: Hook
    priority: int
    order: int
    name: str = field(default="")


class HookRegistry:
    """一个 hook 注册表。**同步与异步 hook 都支持**，异常互相隔离。

    三条硬规则（每条都对应一个真实事故）：

    1. **单个 hook 抛异常不影响其它 hook**。可观测性插件和审计插件挂在同一个
       事件上，一个写日志失败不该让另一个的追踪数据也丢。
    2. **优先级高者先跑，同优先级按注册顺序**（FIFO）。让"必须先跑的"（比如
       ``pre_tool_call`` 里的策略闸）能确定性地排在观察者前面，而不靠 import 顺序。
    3. **发射未知事件直接抛**（见 :class:`UnknownHookEvent`），不静默返回空列表。
    """

    def __init__(self) -> None:
        self._hooks: dict[str, list[_Entry]] = {}
        self._counter = 0
        #: 已发生的异常（``doctor`` 与测试用它断言"隔离生效"）。
        self.failures: list[HookFailure] = []

    # ── 注册 ────────────────────────────────────────────────────────────────
    def register(self, event: str, fn: Hook, priority: int = 0) -> str:
        """注册一个 hook，返回归一后的事件名。

        ``priority`` 越大越先跑；负数表示"排在所有默认 hook 之后"。
        """
        name = normalize_event(event)
        if not callable(fn):
            raise TypeError(f"hook 必须是可调用对象：{fn!r}")
        # 订阅一个**当前没有发射点**的事件时响亮提示（MATRIX 13.5）。
        # **注册一个永远不会被调用的回调，比不注册更糟**——它让插件作者以为接上了。
        # 用 warning 不用抛错：钩子是旁路，抛错会让整个插件装不上；
        # 而且将来接上第 5 个发射点时，不该需要改所有订阅方。
        if name in HOOK_EVENTS and name not in EMITTED_EVENTS:
            log.warning(
                "hook 事件 %r 当前**没有发射点**，订阅了也不会触发。"
                "真有发射点的事件见 app.hooks.EMITTED_EVENTS：%s",
                name,
                sorted(EMITTED_EVENTS),
            )
        self._counter += 1
        entry = _Entry(fn=fn, priority=int(priority), order=self._counter, name=_hook_name(fn))
        bucket = self._hooks.setdefault(name, [])
        bucket.append(entry)
        # 排序键：先按优先级降序，再按注册序号升序。每次注册重排——hook 数量是个位数，
        # 用不着增量插入的复杂度。
        bucket.sort(key=lambda item: (-item.priority, item.order))
        return name

    def unregister(self, event: str, fn: Hook) -> bool:
        """摘掉某个 hook（测试与热重载用）。返回是否真的摘掉了。"""
        name = normalize_event(event)
        bucket = self._hooks.get(name, [])
        for index, entry in enumerate(bucket):
            if entry.fn is fn:
                bucket.pop(index)
                return True
        return False

    def clear(self, event: str | None = None) -> None:
        """清空某个事件（``None`` = 全部）。测试隔离用。"""
        if event is None:
            self._hooks.clear()
            self.failures.clear()
            return
        self._hooks.pop(normalize_event(event), None)

    def hooks(self, event: str) -> list[str]:
        """列出某个事件上挂着的 hook 名（按执行顺序）。"""
        return [entry.name for entry in self._hooks.get(normalize_event(event), [])]

    def entries(self, event: str | None = None) -> list[tuple[str, Hook, int]]:
        """列出某事件（``None`` = 全部事件）上的 ``(事件, 函数, 优先级)``，按执行顺序。

        **为什么要这个公开访问器**：宿主把插件上下文创建在注册表 A 上、而真正的发射点
        读的是注册表 B 时（``app/plugin_host.py::PluginHost`` 的"晚到接线"场景），唯一
        正确的补救是**把已注册的 hook 搬到 B**。没有它就只能读私有 ``_hooks``，或者重新
        import 一遍插件——后者等于再执行一次第三方代码，代价大得多。

        返回的是**函数本身**而不是名字：搬家要按函数身份去重（同名函数可能是两个不同的
        闭包），名字在这里只能用于展示。
        """
        if event is None:
            names = [name for name in ALL_EVENTS if self._hooks.get(name)]
        else:
            names = [normalize_event(event)]
        out: list[tuple[str, Hook, int]] = []
        for name in names:
            for entry in self._hooks.get(name, []):
                out.append((name, entry.fn, entry.priority))
        return out

    def events(self) -> list[str]:
        """当前有 hook 的事件（按事件表顺序，便于稳定输出）。"""
        return [event for event in ALL_EVENTS if self._hooks.get(event)]

    def count(self) -> int:
        return sum(len(bucket) for bucket in self._hooks.values())

    # ── 发射 ────────────────────────────────────────────────────────────────
    def emit(self, event: str, payload: dict[str, Any] | None = None) -> list[HookResult]:
        """同步发射。返回每个 hook 的结果（含被隔离掉的异常）。

        异步 hook 的处理：没有运行中的事件循环时用 ``asyncio.run`` 就地跑完并回填
        结果；**有**运行中的循环时（说明调用方本身在 async 里，却用了同步 ``emit``）
        只能把协程挂成任务并标 ``deferred``——这是唯一不阻塞、也不丢协程的做法，
        想要结果请改用 ``await aemit(...)``。
        """
        name = normalize_event(event)
        body = dict(payload or {})
        results: list[HookResult] = []
        pending: list[tuple[_Entry, Awaitable[Any]]] = []
        for entry in list(self._hooks.get(name, [])):
            try:
                value = entry.fn(body)
            except Exception as exc:  # 隔离：记下来，继续跑下一个
                results.append(self._fail(name, entry, exc))
                continue
            if inspect.isawaitable(value):
                pending.append((entry, value))
                continue
            results.append(HookResult(event=name, name=entry.name, result=value))
        results.extend(self._settle(name, pending))
        return results

    async def aemit(
        self, event: str, payload: dict[str, Any] | None = None
    ) -> list[HookResult]:
        """异步发射：同步 hook 直接跑，异步 hook 逐个 await，异常同样隔离。"""
        name = normalize_event(event)
        body = dict(payload or {})
        results: list[HookResult] = []
        for entry in list(self._hooks.get(name, [])):
            try:
                value = entry.fn(body)
                if inspect.isawaitable(value):
                    value = await value
            except Exception as exc:
                results.append(self._fail(name, entry, exc))
                continue
            results.append(HookResult(event=name, name=entry.name, result=value))
        return results

    # ── 内部 ────────────────────────────────────────────────────────────────
    def _settle(
        self, event: str, pending: list[tuple[_Entry, Awaitable[Any]]]
    ) -> list[HookResult]:
        if not pending:
            return []
        out: list[HookResult] = []
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            for entry, awaitable in pending:
                # 用 ensure_future 而不是 create_task：后者只收 Coroutine，
                # 而 hook 可能返回任意 awaitable（Future / 自定义 awaitable）。
                asyncio.ensure_future(awaitable, loop=loop)
                out.append(HookResult(event=event, name=entry.name, deferred=True))
            return out
        for entry, awaitable in pending:
            try:
                value = asyncio.run(_await(awaitable))
            except Exception as exc:
                out.append(self._fail(event, entry, exc))
                continue
            out.append(HookResult(event=event, name=entry.name, result=value))
        return out

    def _fail(self, event: str, entry: _Entry, exc: BaseException) -> HookResult:
        message = f"{type(exc).__name__}: {exc}"
        self.failures.append(HookFailure(event=event, name=entry.name, error=message))
        log.warning("hook %s（事件 %s）抛异常，已隔离：%s", entry.name, event, message)
        return HookResult(event=event, name=entry.name, error=message)


async def _await(awaitable: Awaitable[Any]) -> Any:
    """把"已经是协程或 future"的东西统一 await 掉（``asyncio.run`` 只收协程）。"""
    return await awaitable


def _hook_name(fn: Hook) -> str:
    """hook 的展示名。``functools.partial`` 之类没有 ``__name__``，退回类型名。"""
    return getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or type(fn).__name__


#: 进程级默认注册表。**宿主应把它注入**（``PluginContext(hooks=DEFAULT_HOOKS)``），
#: 而不是让每个模块自己 ``HookRegistry()`` —— 否则插件注册到 A、节点发射到 B，
#: 两边都"成功"，事件却永远不触发。
DEFAULT_HOOKS = HookRegistry()


def emit(event: str, payload: dict[str, Any] | None = None) -> list[HookResult]:
    """在默认注册表上发射（接线点写起来最短的形式）。"""
    return DEFAULT_HOOKS.emit(event, payload)


def register(event: str, fn: Hook, priority: int = 0) -> str:
    """在默认注册表上注册。"""
    return DEFAULT_HOOKS.register(event, fn, priority)


__all__ = [
    "ALL_EVENTS",
    "DEFAULT_HOOKS",
    "EVENT_ALIASES",
    "EVENT_EVIDENCE",
    "EVENT_NOTES",
    "HOOK_EVENTS",
    "INFERRED_EVENTS",
    "WIRING_POINTS",
    "Hook",
    "HookFailure",
    "HookRegistry",
    "HookResult",
    "UnknownHookEvent",
    "emit",
    "normalize_event",
    "register",
]
