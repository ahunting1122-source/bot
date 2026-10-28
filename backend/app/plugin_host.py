"""插件宿主门面（MATRIX 13.3）：把 :class:`app.plugins.PluginRuntime` 变成**宿主一行调用**的东西。

## 它解决的那个具体问题

``PluginRuntime.warmup()`` / ``ensure_lazy_loaded()`` 早就有了，但**没有任何生产代码调它**
（``app/main.py`` / ``app/agent.py`` 是禁改区）。于是"插件层"一直是"写好了但没人跑过"的
状态——这类东西最危险的地方在于：**它可能根本跑不起来，而没人知道**。测试能证明
``warmup()`` 返回了正确的报告，却证明不了"插件的工具真的进了 ToolRegistry、钩子真的
在宿主的 hooks 里"。

所以本模块把「接线」变成一个**动作**而不是一句注释：

    host = PluginHost.from_settings(settings, tool_sink=registry)   # registry = ToolRegistry
    host.start()                                                    # warmup + 接线
    host.describe()      # host_ready=True / wired_into_main=False / 缺哪一行

``start()`` 之后，插件注册的工具**真的**在 ``ToolRegistry.tools`` 里、钩子**真的**在
``HookRegistry`` 里（可 emit、可触发）。生产入口缺失这件事不再靠嘴说：:meth:`describe`
报 ``wired_into_main=False``，并把缺的那一行代码原样贴出来（:data:`MAIN_WIRING_CODE`），
谁接线就粘这一行。

## 为什么"接线"要能补做（reconcile）

插件可能在**两种时序**下被加载：

1. **宿主先就位**（``tool_sink`` / ``hook_sink`` 在 ``PluginRuntime`` 构造时就传进去）——
   ``ctx.register_tool`` 当场把工具推给注册表。这是生产路径。
2. **宿主晚到**（先 ``load()`` 拿到上下文，之后才决定往哪个注册表接）——上一轮的
   ``plugins reload`` / ``plugins override --load`` 就是这种。这时插件早已注册完，
   光把 sink 设进去不会补发任何东西。

:meth:`PluginHost.start` 每次都会跑一遍 :meth:`PluginHost._reconcile`：把已加载上下文里的
工具与 hook **补推**到 sink 上，并按身份去重（``(插件, 工具名)`` / ``(事件, 函数 id)``），
所以第二次 ``start()`` 是幂等的——不会重复注册、不会把同一个 hook 挂两遍。

## 诚实边界（这里必须写清楚）

- ``app/main.py`` / ``app/agent.py`` 是禁改区：**本模块目前没有任何生产启动路径的调用方**。
  ``start()`` 这条链是被 ``tests/test_plugin_host.py`` 完整演练过的（工具真的进
  ``ToolRegistry`` 并能执行、钩子真的能 emit），但"生产里谁调它"这件事还没发生——所以
  :meth:`describe` 的 ``wired_into_main`` 恒为 ``False``，直到有人在 ``main.py`` 里粘上
  :data:`MAIN_WIRING_CODE`。
- 钩子的**发射点**仍不在主循环里（见 ``app/hooks.py::WIRING_POINTS``）：接线保证"注册的
  钩子进了宿主的注册表、能被 emit 到"，不保证"主循环会 emit 那个事件"。
- ``dispatch_slash`` 是斜杠命令的最后一段（真的调 handler），但它同样还没有调用方：
  ``app/cli/commands/chat.py`` 本轮正被另一个 agent 改写，所以那里的一行没有加进来
  （``register_slash_command`` 因此仍报 ``exposed``）。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import plugin_capabilities
from app import plugins as plugins_module
from app.hooks import DEFAULT_HOOKS, HookRegistry

log = logging.getLogger("orbit.plugin_host")

#: 缺的那一行**在哪**（``app/main.py`` 的确切位置）。
MAIN_WIRING_ANCHOR = (
    "app/main.py::create_app —— `app.include_router(dashboard_api.router)` 之后、"
    "`app.mount(\"/\", StaticFiles(...))` 之前"
)

#: 缺的那一行**是什么**。原样贴出来，让接线的人直接复制（含缩进，缩进是 4 个空格）。
MAIN_WIRING_CODE = (
    "    from app.plugin_host import PluginHost\n"
    "    PluginHost.from_settings(app.state.settings, "
    "tool_sink=app.state.registry).attach_to_app(app)"
)

#: 还需要接的另外两行（工具清单与钩子发射点）。``wired_missing`` 里逐条给。
AGENT_WIRING_CODE = "tools = runtime.tools()  # app/agent.py 拼提示词处：首次调用会把 lazy 插件 import 进来"
HOOKS_WIRING_CODE = (
    "app.hooks.emit('pre_tool_call', {'tool': name, 'args': args})  # app/graph/nodes.py 工具派发前后"
)


@dataclass
class HostWiring:
    """一次 ``start()`` 之后**真的接上了什么**（可断言的事实，不是日志）。"""

    #: 这次 start() 新 import 的插件名。
    loaded: list[str] = field(default_factory=list)
    #: 确认已在 tool_sink 里的插件工具名（**本次 start 之后的事实**，不是"这次推了几个"）。
    tools: list[str] = field(default_factory=list)
    #: 本次 reconcile **补推**的工具名（"宿主晚到"时才非空；幂等性就靠它看得见）。
    pushed: list[str] = field(default_factory=list)
    #: 每个事件上（宿主的）hook 数量。
    hooks: dict[str, int] = field(default_factory=dict)
    #: 补推过程中被隔离掉的异常（一个坏插件不影响其它）。
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "loaded": list(self.loaded),
            "tools": list(self.tools),
            "pushed": list(self.pushed),
            "hooks": dict(self.hooks),
            "errors": list(self.errors),
        }


class PluginHost:
    """插件宿主门面：一次 ``start()`` 完成 warmup + 工具/hook 接线。

    构造方式有两种，都用同一条装配路径：

    - :meth:`from_settings`：生产用法（自己 ``discover()`` + ``load_state()``）；
    - 直接构造：测试 / 已经拿到 manifest 列表的场合。

    ``tool_sink`` 与 ``hook_sink`` 的默认值是**宿主的真实结构**（``None`` 与
    ``app.hooks.DEFAULT_HOOKS``）：默认就该接到真的注册表上，否则"默认路径"和"生产路径"
    又会分叉——那正是上一轮的问题。
    """

    def __init__(
        self,
        runtime_or_manifests: Any,
        *,
        tool_sink: Any = None,
        hook_sink: HookRegistry | None = None,
        page_sink: Any = None,
        platform_sink: dict[str, Any] | None = None,
        settings: Any = None,
        state: Any = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        hooks = hook_sink if hook_sink is not None else DEFAULT_HOOKS
        if isinstance(runtime_or_manifests, plugins_module.PluginRuntime):
            runtime = runtime_or_manifests
            # 运行期对象是别人造的：把 sink 补进去（真正的补推在 start() 的 reconcile 里）。
            runtime.hooks = hooks
            if tool_sink is not None:
                runtime.tool_sink = tool_sink
        else:
            runtime = plugins_module.PluginRuntime(
                list(runtime_or_manifests),
                hooks=hooks,
                tool_sink=tool_sink,
                settings=settings,
                environ=environ,
                platform_sink=platform_sink,
                page_sink=page_sink,
                state=state,
            )
        self.runtime = runtime
        self.hook_sink: HookRegistry = hooks
        self.settings = settings
        #: 已 start 过几次（幂等性看得见：第二次不该再 import、不该再推一遍）。
        self.starts = 0
        #: 已经挂到 app 上的宿主自己（``wired_into_main`` 的判据，见 :meth:`describe`）。
        self.attached: list[str] = []
        #: 已推送过的东西（按身份去重，保证 ``start()`` 幂等）。
        self._wired_tools: set[tuple[str, str]] = set()
        self._wired_hooks: set[tuple[str, int]] = set()
        self._wiring = HostWiring()

    # ── 装配 ────────────────────────────────────────────────────────────────
    @classmethod
    def from_settings(
        cls,
        settings: Any,
        *,
        tool_sink: Any = None,
        hook_sink: HookRegistry | None = None,
        page_sink: Any = None,
        platform_sink: dict[str, Any] | None = None,
        roots: Sequence[Path] | None = None,
        state: Any = None,
        environ: dict[str, str] | None = None,
    ) -> PluginHost:
        """按生产口径装配：``discover()`` 找插件、``load_state()`` 读门禁。

        ``state`` 显式传进来时以它为准（"改完 plugins.json 再驱动一次"不必重建对象）。
        """
        discovery = plugins_module.discover(roots, settings=settings)
        gate = state if state is not None else plugins_module.load_state(settings)
        return cls(
            discovery.manifests,
            tool_sink=tool_sink,
            hook_sink=hook_sink,
            page_sink=page_sink,
            platform_sink=platform_sink,
            settings=settings,
            state=gate,
            environ=environ,
        )

    # ── 启动 ────────────────────────────────────────────────────────────────
    def start(
        self,
        state: Any = None,
        *,
        tool_sink: Any = None,
        hook_sink: HookRegistry | None = None,
    ) -> dict[str, Any]:
        """``warmup()`` + 把插件的工具/钩子接到 sink 上。**可重复调用（幂等）**。

        顺序有讲究：先把 sink 就位再 warmup——这样 eager 插件在 ``register(ctx)`` 里注册的
        工具是**当场**推进注册表的（而不是先记在上下文里、等 reconcile 再补），少一次
        中间态。``reconcile`` 兜住"晚到"的那一半（见模块 docstring）。
        """
        if state is not None:
            self.runtime.state = state
        if tool_sink is not None:
            self.runtime.tool_sink = tool_sink
        if hook_sink is not None:
            self.hook_sink = hook_sink
            self.runtime.hooks = hook_sink
        self.starts += 1
        warmed = self.runtime.warmup()
        wiring = self._reconcile()
        wiring.loaded = list(warmed.get("loaded", []))
        wiring.errors.extend(
            f"{item['name']}: {item['error']}" for item in warmed.get("errors", [])
        )
        self._wiring = wiring
        return {
            **warmed,
            "starts": self.starts,
            "wired": wiring.as_dict(),
            "tool_sink": _sink_label(self.runtime.tool_sink),
            "hook_sink": _sink_label(self.hook_sink),
        }

    def _reconcile(self) -> HostWiring:
        """把已加载上下文里的工具与 hook 补推到宿主的 sink 上（按身份去重）。"""
        wiring = HostWiring()
        tool_sink = self.runtime.tool_sink
        for item in self.runtime.loaded:
            context = item.context
            if context is None:
                continue
            for name, tool in context.tools.items():
                key = (context.name, name)
                if tool_sink is None:
                    continue
                if key in self._wired_tools or _sink_has_tool(tool_sink, tool):
                    # 已经在 sink 里（正常路径：sink 先就位，插件注册时当场推进去）。
                    self._wired_tools.add(key)
                    if name not in wiring.tools:
                        wiring.tools.append(name)
                    continue
                try:
                    tool_sink.register(
                        [tool.as_tool()],
                        tool.as_executor(),
                        source=tool.source or f"plugin:{context.name}",
                        override=tool.override,
                    )
                except Exception as exc:  # noqa: BLE001 —— 一个插件推不进去不影响其它
                    wiring.errors.append(
                        f"{context.name} 的工具 {name} 未接进 {_sink_label(tool_sink)}："
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue
                self._wired_tools.add(key)
                wiring.pushed.append(name)
                if name not in wiring.tools:
                    wiring.tools.append(name)
            self._reconcile_hooks(context, wiring)
        return wiring

    def _reconcile_hooks(self, context: Any, wiring: HostWiring) -> None:
        """把上下文里的 hook 搬到宿主的注册表上。

        ``context.hooks is self.hook_sink`` 时什么都不用做（插件注册时就落对了），只需数一遍
        ——**但必须数**，否则报告会漏掉"本来就接对了"这个最常见的好消息。
        """
        for event, fn, priority in context.hooks.entries():
            if context.hooks is self.hook_sink:
                wiring.hooks[event] = wiring.hooks.get(event, 0) + 1
                continue
            key = (event, id(fn))
            if key in self._wired_hooks:
                continue
            try:
                self.hook_sink.register(event, fn, priority=priority)
            except Exception as exc:  # noqa: BLE001
                wiring.errors.append(
                    f"{context.name} 的 hook {event} 未接进宿主注册表：{type(exc).__name__}: {exc}"
                )
                continue
            self._wired_hooks.add(key)
            wiring.hooks[event] = wiring.hooks.get(event, 0) + 1

    # ── 宿主消费面 ──────────────────────────────────────────────────────────
    def dispatch_slash(self, text: str) -> tuple[str, Any] | None:
        """``/name 参数`` → 插件注册的斜杠命令；不是插件命令就返回 ``None``。

        这条链是"登记 → 解析 → 分发"的最后一段：查得到就**真的调** handler，查不到就返回
        ``None``（调用方把这一行当普通消息处理，**不吞输入**）。

        **诚实边界**：本方法已就位并被单测覆盖，但**仓库里还没有调用方**——该调它的地方是
        ``app/cli/commands/chat.py`` 的输入循环（``line = input("> ")`` 之后一行），而那个文件
        本轮正被另一个 agent 改写，两边同时改会互相覆盖。所以
        ``register_slash_command`` 仍报 ``exposed``，缺的就是那一行。
        只遍历**已加载**的上下文：lazy 插件要等 ``tools()`` 之后它的斜杠命令才可见。
        """
        for item in self.runtime.loaded:
            context = item.context
            if context is None:
                continue
            found = plugin_capabilities.resolve_slash_command(context, text)
            if found is None:
                continue
            name, handler, rest = found
            outcome = handler(rest)
            if inspect.isawaitable(outcome):
                outcome = _run_sync(outcome, name)
            return name, outcome
        return None

    def attach_to_app(self, app: Any) -> dict[str, Any]:
        """把宿主挂到一个 FastAPI app 上——``app/main.py`` 需要的**那一行**。

        做三件事：① ``start()``（warmup + 接线）；② 把宿主记到 ``app.state.plugin_host``
        （之后任何请求都能拿到同一个宿主实例）；③ 挂 dashboard 插件路由
        （:func:`app.plugin_dashboard.mount_into_app`，**会执行第三方插件代码**）。

        ``main.py`` 是禁改区，所以本方法目前没有生产调用方；一旦有人调用，
        :meth:`describe` 的 ``wired_into_main`` 立刻变成 ``True``——它查的是"有没有人真的
        调过这个入口"，不是一句自我评价。
        """
        started = self.start()
        state = getattr(app, "state", None)
        if state is not None:
            state.plugin_host = self
        label = str(getattr(app, "title", "") or type(app).__name__)
        if label not in self.attached:
            self.attached.append(label)
        mounted: dict[str, Any] = {}
        try:
            from app import plugin_dashboard

            pages = getattr(self.runtime, "page_sink", None)
            mounted = plugin_dashboard.mount_into_app(
                app, pages if isinstance(pages, plugin_dashboard.PageRegistry) else None
            )
        except Exception as exc:  # noqa: BLE001 —— 挂载失败不该让宿主起不来
            mounted = {"error": f"{type(exc).__name__}: {exc}"}
            log.warning("dashboard 插件路由挂载失败（已隔离）：%s", exc)
        return {"started": started, "mounted": mounted}

    # ── 视图 ────────────────────────────────────────────────────────────────
    def tools(self) -> dict[str, Any]:
        """插件工具清单（内部先补齐 lazy 插件——与 ``PluginRuntime.tools()`` 同义）。"""
        return self.runtime.tools()

    def hooks(self) -> dict[str, int]:
        """宿主注册表上每个事件挂了多少 hook（``{}`` = 一个都没有）。"""
        return {event: len(self.hook_sink.hooks(event)) for event in self.hook_sink.events()}

    def describe(self) -> dict[str, Any]:
        """宿主快照：**host_ready 与 wired_into_main 是两件事**。

        - ``host_ready=True``：这一层本身可用（一行就能起、能接线、可验证）；
        - ``wired_into_main=False``：**没有任何生产启动路径调用它**。判据是"有没有人真的
          调过 :meth:`attach_to_app`"，不是自我评价。
        - ``missing_code``：缺的那一行代码（原样可粘），``missing_at``：粘在哪。
        """
        runtime_view = self.runtime.describe()
        return {
            "host_ready": True,
            "wired_into_main": bool(self.attached),
            "attached_apps": list(self.attached),
            "starts": self.starts,
            "tool_sink": _sink_label(self.runtime.tool_sink),
            "hook_sink": _sink_label(self.hook_sink),
            "tools": sorted(
                name
                for item in self.runtime.loaded
                if item.context is not None
                for name in item.context.tools
            ),
            "hooks": self.hooks(),
            "pages": [str(item.get("plugin", "")) for item in self.runtime.pages()],
            "pending_lazy": runtime_view["pending_lazy"],
            "errors": runtime_view["errors"],
            "wired": self._wiring.as_dict(),
            "missing_at": MAIN_WIRING_ANCHOR,
            "missing_code": MAIN_WIRING_CODE,
            "missing_lines": [
                f"{MAIN_WIRING_ANCHOR}：\n{MAIN_WIRING_CODE}",
                f"app/agent.py（拼提示词 / 列工具处）：{AGENT_WIRING_CODE}",
                f"app/graph/nodes.py（工具派发前后）：{HOOKS_WIRING_CODE}",
            ],
            "reason": (
                "宿主门面已就绪（PluginHost.start() 能完成 warmup + 工具/hook 接线，且已被 "
                "tests/test_plugin_host.py 完整演练过），但 app/main.py 是禁改区，没有调用点；"
                "接线只需粘上 missing_code 那一行"
            ),
        }


def _sink_label(sink: Any) -> str:
    """sink 的展示名（报告里要能看出"接到哪儿去了"）。"""
    if sink is None:
        return "（未注入：只记录在插件上下文里）"
    return f"{type(sink).__module__}.{type(sink).__name__}"


def _sink_has_tool(sink: Any, tool: Any) -> bool:
    """sink 里是否已经有这个 source 的这个工具（避免重复注册同一个插件工具）。"""
    registry_tools = getattr(sink, "tools", None)
    if not isinstance(registry_tools, dict):
        return False
    existing = registry_tools.get(tool.name)
    if existing is None:
        return False
    source = tool.source
    return bool(source) and getattr(existing, "source", None) == source


def _run_sync(awaitable: Any, name: str) -> Any:
    """在同步入口里跑一个协程 hook / 斜杠命令。

    有运行中的事件循环时**不能** ``asyncio.run``（会抛 "already running"），而同步函数
    又没法 await——这时如实报错并指向 ``aemit``/异步入口，而不是悄悄把协程丢掉
    （丢掉的表现是"命令执行了但什么也没发生"，最难查）。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_await(awaitable))
    raise RuntimeError(
        f"斜杠命令 {name} 是协程，但调用方在运行中的事件循环里用了同步入口；"
        "请改用异步入口（或把 handler 写成普通函数）"
    )


async def _await(awaitable: Any) -> Any:
    """``asyncio.run`` 只收协程，这里把任意 awaitable 统一 await 掉。"""
    return await awaitable


__all__ = [
    "AGENT_WIRING_CODE",
    "HOOKS_WIRING_CODE",
    "MAIN_WIRING_ANCHOR",
    "MAIN_WIRING_CODE",
    "HostWiring",
    "PluginHost",
]
