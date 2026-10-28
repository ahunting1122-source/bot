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
- ``dispatch_slash`` 是斜杠命令的最后一段（真的调 handler）。**接口已冻结**：
  `dispatch_slash(text) -> (name, outcome) | None`（不是插件命令返回 ``None``，调用方
  把它当普通消息）。异步孪生是 :meth:`PluginHost.dispatch_slash_async`（HTTP 路由走它，
  因为 ``_run_sync`` 在运行中的事件循环里只能如实报错）。
- MATRIX 13.4 的第三轮把 ``exposed`` 里的两条真的推到了 ``wired``，靠的是**在宿主里
  建出真实调用方**而不是改别人的文件：

  * ``register_slack_action_handler`` —— :meth:`PluginHost.mount_routes` 提供
    ``POST /api/plugin/actions``（解析 Slack Block Kit 的 ``actions[].action_id``）与
    ``POST /api/plugin/actions/{action_id}``，两条都真的转给
    :meth:`PluginHost.dispatch_action`；
  * ``register_slash_command`` —— 同一条路由表里的 ``POST /api/plugin/slash`` 真的调
    :meth:`PluginHost.dispatch_slash_async` 并带回 handler 的返回值。

  另外两条仍是 ``exposed``，缺的那一步不在本模块：``register_memory_provider`` 缺装配处
  把 provider 传给 ``build_router``（本模块已给 :meth:`PluginHost.memory_provider_for`），
  ``register_observability_exporter`` 缺主循环的发射点（``app/hooks.py::WIRING_POINTS``，
  本模块已给 :meth:`PluginHost.exporters_for`）。**入口就位 ≠ 已经跑起来**，
  所以 ``ready`` 与 ``wired`` 始终是两个字段。
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

# ── 动作路由表（MATRIX 13.4 的 slack_action_handler）─────────────────────────
#
# `register_slack_action_handler` 一直卡在 exposed，缺的是「action_id → 回调」这张表：
# 插件登记的回调躺在 `PluginContext.capability_ledger` 里，而**没有任何结构能按
# action_id 查到它**。这张表就是那个结构。
#
# 为什么放在模块级而不是每个宿主实例一份：按钮回调进来时（`app/dashboard_api.py` 的
# webhook 收件路径）手上只有 action_id，未必拿得到宿主实例；而"哪个插件处理 btn_ok"
# 是**进程级**的事实，不该取决于调用方手里有没有那个对象。宿主实例仍可覆盖落点
# （`PluginHost.register_action_route`），测试与多实例场景用得上。

#: action_id → 回调。键是 Slack Block Kit 的 `actions[].action_id`（原样，不改大小写：
#: Slack 的 action_id 区分大小写，归一化会让"注册的"和"点进来的"对不上）。
ACTION_ROUTES: dict[str, Any] = {}


def register_action_route(action_id: str, callback: Any, *, override: bool = False) -> str:
    """登记一条 ``action_id → 回调``。返回归一化后的键。

    **同名默认拒绝**（抛 ``ValueError``）：两个插件抢同一个按钮，静默覆盖会让"点了
    没反应"变成一个查不出来的问题 —— 而报错能立刻指出是谁抢了谁。
    """
    key = str(action_id or "").strip()
    if not key:
        raise ValueError("register_action_route 需要非空的 action_id")
    if not callable(callback):
        raise ValueError(f"register_action_route({key}) 需要可调用对象")
    existing = ACTION_ROUTES.get(key)
    if existing is not None and not override:
        raise ValueError(
            f"action_id {key!r} 已经有处理器（{getattr(existing, '__qualname__', existing)}）；"
            "要替换请显式传 override=True"
        )
    ACTION_ROUTES[key] = callback
    return key


def unregister_action_route(action_id: str) -> bool:
    """删一条路由（卸载插件/测试清理用）。返回是否真的删掉了。"""
    return ACTION_ROUTES.pop(str(action_id or "").strip(), None) is not None


def dispatch_action(action_id: str, payload: Any = None) -> Any:
    """按 action_id 查到回调并**真的调用**它。查不到返回 ``None``。

    查不到为什么返回 ``None`` 而不是抛：按钮点击可能来自已经卸载的插件（消息还在
    聊天记录里，插件没了），这时"什么都不做"是正确行为；抛异常会让整条 webhook 请求
    变成 500，Slack 会不停重投同一个点击。
    """
    callback = ACTION_ROUTES.get(str(action_id or "").strip())
    if callback is None:
        return None
    return callback(payload)


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
    #: 补进 ``ACTION_ROUTES`` 的 action_id（slack_action_handler 的落点）。
    actions: list[str] = field(default_factory=list)
    #: 补推过程中被隔离掉的异常（一个坏插件不影响其它）。
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "loaded": list(self.loaded),
            "tools": list(self.tools),
            "pushed": list(self.pushed),
            "hooks": dict(self.hooks),
            "actions": list(self.actions),
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
        #: 本宿主收到的 ``action_id → 回调``（从插件上下文搬来；模块级 ``ACTION_ROUTES``
        #: 是同一份事实的进程级视图，供拿不到宿主实例的调用方查）。
        self.action_routes: dict[str, Any] = {}
        #: 已经挂到 FastAPI app 上的入站路由（挂过就不再挂第二遍）。
        self._mounted_routes: list[str] = []

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
            self._reconcile_actions(context, wiring)
        return wiring

    def _reconcile_actions(self, context: Any, wiring: HostWiring) -> None:
        """把插件登记的 slack 动作处理器搬进 ``ACTION_ROUTES``。

        这是 ``register_slack_action_handler`` 从"只能登记"变成"能被查到并派发"的那一步：
        登记的落点是 ``capability_ledger``（一个按能力分组的列表），**没有任何结构能按
        action_id 查它**；搬进 ``ACTION_ROUTES`` 之后，按钮回调只需 action_id 就能找到
        处理器（见 :func:`dispatch_action`）。
        """
        for item in context.capability_ledger.get("register_slack_action_handler", []):
            if not isinstance(item, dict):
                continue
            action_id = str(item.get("action_id") or "").strip()
            callback = item.get("callback")
            if not action_id or not callable(callback):
                continue
            self.action_routes[action_id] = callback
            if ACTION_ROUTES.get(action_id) is None:
                try:
                    register_action_route(action_id, callback)
                except ValueError as exc:  # 已被别人占住：记下来，不覆盖
                    wiring.errors.append(
                        f"{context.name} 的动作 {action_id} 未进 ACTION_ROUTES：{exc}"
                    )
                    continue
            if action_id not in wiring.actions:
                wiring.actions.append(action_id)

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

    async def dispatch_slash_async(self, text: str) -> tuple[str, Any] | None:
        """``dispatch_slash`` 的**异步孪生**（给 HTTP / 异步入口用）。

        为什么不能只有同步那个：``_run_sync`` 在"已经有运行中事件循环"时会**如实报错**
        （它没法 await）。FastAPI 的路由天然在事件循环里，所以异步入口必须走这条路，
        否则"插件写了个 async 斜杠命令"就变成"点了没反应 + 一条看不懂的异常"。

        ``dispatch_slash`` 的签名**保持不变**（它已被测试与文档冻结成
        ``dispatch_slash(text) -> (name, outcome) | None``），这里只是加一个兄弟方法。
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
                outcome = await outcome
            return name, outcome
        return None

    def mount_routes(self, app: Any) -> dict[str, Any]:
        """把宿主自己的**入站路由**挂到 FastAPI app 上（幂等）。返回挂了哪几条。

        这三条路由就是 MATRIX 13.4 里"缺的那一层"——它们都是**真实的生产调用方**：

        * ``POST /api/plugin/actions/{action_id}`` —— 显式 id 的动作回调（Slack 交互式
          按钮点进来的最小形态）；
        * ``POST /api/plugin/actions`` —— **Slack Block Kit 整包**：从 ``actions[].action_id``
          里取出 id 再转给 :meth:`dispatch_action`。`app/dashboard_api.py` 的 webhook
          收件路径此前只把载荷落进收件箱、没有解析这一段，所以插件登记的回调永远等不到
          人来调；这一段补上之后 `register_slack_action_handler` 才算真的有消费者；
        * ``POST /api/plugin/slash`` —— 斜杠命令的 HTTP 入口：调
          :meth:`dispatch_slash_async`，**真的执行插件 handler** 并把返回值原样带回。

        为什么放在宿主而不是 `app/dashboard_api.py`：那三条路由属于"插件宿主"这一层，
        与 dashboard 的页面/静态资源无关；放在这里，`main.py` 粘一行
        :data:`MAIN_WIRING_CODE` 就同时得到路由，不需要再改第二个文件。

        **边界**：这些路由只有 `attach_to_app` 被调过之后才存在（也就是 `main.py` 粘上
        那一行之后）。本机单用户 loopback，与 `/api/*` 其余端点同一暴露面；
        真部署到公网时必须先有认证层（`register_dashboard_auth` 仍是 unsupported 的原因）。
        """
        post = getattr(app, "post", None)
        if not callable(post):
            return {
                "routes": [],
                "skipped": f"{type(app).__name__} 没有 post()（不是 FastAPI app，跳过入站路由）",
            }
        if self._mounted_routes:
            return {"routes": list(self._mounted_routes), "skipped": "已挂载过（幂等）"}

        async def plugin_action(
            action_id: str, payload: dict[str, Any] | None = None
        ) -> dict[str, Any]:
            body = dict(payload or {})
            outcome = self.dispatch_action(action_id, body)
            return {
                "ok": outcome is not None,
                "action_id": action_id,
                "outcome": _jsonable(outcome),
            }

        async def plugin_block_kit(payload: dict[str, Any] | None = None) -> dict[str, Any]:
            body = dict(payload or {})
            raw = body.get("actions")
            handled: list[dict[str, Any]] = []
            for item in raw if isinstance(raw, list) else []:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("action_id") or "").strip()
                if not key:
                    continue
                outcome = self.dispatch_action(key, item)
                handled.append(
                    {"action_id": key, "handled": outcome is not None, "outcome": _jsonable(outcome)}
                )
            return {
                "ok": bool(handled),
                "handled": handled,
                # 查不到的 action_id 不是错误：按钮可能来自已经卸载的插件（消息还在
                # 聊天记录里），这时"什么都不做"是正确行为，Slack 也不该被 500 触发重投。
                "note": "按 actions[].action_id 逐条派发；未登记的 action_id 返回 handled=false。",
            }

        async def plugin_slash(payload: dict[str, Any] | None = None) -> dict[str, Any]:
            text = str((payload or {}).get("text") or "")
            found = await self.dispatch_slash_async(text)
            if found is None:
                return {
                    "handled": False,
                    "text": text,
                    "note": "不是已登记的插件斜杠命令（调用方应把它当普通消息处理，不吞输入）。",
                }
            name, outcome = found
            return {"handled": True, "name": name, "outcome": _jsonable(outcome)}

        routes: list[str] = []
        try:
            post("/api/plugin/actions/{action_id}", name="orbit_plugin_action")(plugin_action)
            post("/api/plugin/actions", name="orbit_plugin_block_kit")(plugin_block_kit)
            post("/api/plugin/slash", name="orbit_plugin_slash")(plugin_slash)
            routes = [
                "POST /api/plugin/actions/{action_id}",
                "POST /api/plugin/actions",
                "POST /api/plugin/slash",
            ]
        except Exception as exc:  # noqa: BLE001 —— 挂不上不该让宿主起不来
            log.warning("插件入站路由挂载失败（已隔离）：%s", exc)
            return {"routes": [], "skipped": f"{type(exc).__name__}: {exc}"}
        self._mounted_routes = routes
        return {"routes": list(routes), "skipped": ""}

    def attach_to_app(self, app: Any) -> dict[str, Any]:
        """把宿主挂到一个 FastAPI app 上——``app/main.py`` 需要的**那一行**。

        做四件事：① ``start()``（warmup + 接线）；② 把宿主记到 ``app.state.plugin_host``
        （之后任何请求都能拿到同一个宿主实例）；③ 挂 dashboard 插件路由
        （:func:`app.plugin_dashboard.mount_into_app`，**会执行第三方插件代码**）；
        ④ 挂宿主自己的入站路由（:meth:`mount_routes`：动作回调与斜杠命令的 HTTP 入口）。

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
        try:
            routes = self.mount_routes(app)
        except Exception as exc:  # noqa: BLE001
            routes = {"routes": [], "skipped": f"{type(exc).__name__}: {exc}"}
        return {"started": started, "mounted": mounted, "routes": routes}

    # ── 视图 ────────────────────────────────────────────────────────────────
    def tools(self) -> dict[str, Any]:
        """插件工具清单（内部先补齐 lazy 插件——与 ``PluginRuntime.tools()`` 同义）。"""
        return self.runtime.tools()

    def hooks(self) -> dict[str, int]:
        """宿主注册表上每个事件挂了多少 hook（``{}`` = 一个都没有）。"""
        return {event: len(self.hook_sink.hooks(event)) for event in self.hook_sink.events()}

    # ── 13.4 的三个宿主侧入口（供装配层取，本模块不接线）───────────────────
    def memory_providers(self) -> dict[str, Any]:
        """插件登记的长期记忆 provider：``{provider_id: provider}``。

        形状刻意与 ``app/memory_providers.py::build_router(providers=…)`` 对得上，
        装配层只需一行：

            router = build_router(providers=[*host.memory_providers().values(),
                                             *default_providers(store=..., data_dir=...)])

        **为什么不在这里调 build_router**：路由器的优先级、fanout、data_dir 都是装配层
        的决定；宿主替它决定，等于把"插件能不能被选中"写死在插件层里。
        """
        found: dict[str, Any] = {}
        for item in self.runtime.loaded:
            context = item.context
            if context is None:
                continue
            for record in context.capability_ledger.get("register_memory_provider", []):
                if not isinstance(record, dict):
                    continue
                provider = record.get("provider")
                key = str(record.get("id") or getattr(provider, "name", "") or "").strip()
                if key and provider is not None:
                    found[key] = provider
        return found

    def exporters(self) -> dict[str, Any]:
        """插件挂上的可观测性 exporter：``{name: exporter}``。

        与 ``app/exporters.py::default_exporters()`` 的池子同形状，装配层合并即可：

            pool = {**default_exporters(data_dir), **host.exporters()}

        **边界**：exporter 的 ``record()`` 已经挂在宿主的 HookRegistry 上（19 个事件），
        但主循环的发射点未接线（``app/hooks.py::WIRING_POINTS``），所以此刻还没有事件流过。
        这个入口解决的是"取不到 exporter"，不解决"没有事件"。
        """
        found: dict[str, Any] = {}
        for item in self.runtime.loaded:
            context = item.context
            if context is None:
                continue
            for record in context.capability_ledger.get(
                "register_observability_exporter", []
            ):
                if not isinstance(record, dict):
                    continue
                exporter = record.get("exporter")
                key = str(record.get("name") or getattr(exporter, "name", "") or "").strip()
                if key and exporter is not None:
                    found[key] = exporter
        return found

    def memory_provider_for(self, name: str) -> Any:
        """按 id 取**插件登记的那一个**记忆 provider（取不到返回 ``None``）。

        为什么装配层需要这个而不是自己翻字典：装配处真正要的是"按名字换掉当前生效的
        provider"（Hermes 的 register_memory_provider 是**排他激活**语义：同一时刻只有
        一个生效）。有了它，装配层那一行是：

            router = build_router(providers=[host.memory_provider_for("plugin-mem") or
                                             *default_providers(store=..., data_dir=...)])

        查不到返回 ``None`` 而不是抛：插件可能已被卸载，而调用方这时该退回内置 provider
        ——抛异常会把"插件没了"变成"记忆整体不可用"。
        """
        key = str(name or "").strip()
        if not key:
            return None
        return self.memory_providers().get(key)

    def exporters_for(self, event: str) -> dict[str, Any]:
        """只取**挂在这个事件上**的 exporter：``{name: exporter}``。

        与 :meth:`exporters` 的差别：那个是"有哪些 exporter"，这个是"这个事件会流到谁"。
        差别很重要——`exporters()` 拿去做池子合并时，装配层无法回答"我 emit 一次
        ``pre_tool_call``，哪些 exporter 会收到"。这里按 ``attach_observability_exporter``
        记进台账的 ``events`` 过滤，答案就是确定的。

        事件名走 :func:`app.hooks.normalize_event`（别名归一 + 未知事件当场抛）：
        拼错的事件名在这里必须炸，否则"查不到 exporter"会被读成"没有 exporter"。
        """
        wanted = hooks_module.normalize_event(event)
        found: dict[str, Any] = {}
        for item in self.runtime.loaded:
            context = item.context
            if context is None:
                continue
            for record in context.capability_ledger.get(
                "register_observability_exporter", []
            ):
                if not isinstance(record, dict):
                    continue
                events = record.get("events")
                if not isinstance(events, (list, tuple)) or wanted not in events:
                    continue
                exporter = record.get("exporter")
                key = str(record.get("name") or getattr(exporter, "name", "") or "").strip()
                if key and exporter is not None:
                    found[key] = exporter
        return found

    def register_action_route(self, action_id: str, callback: Any, *, override: bool = False) -> str:
        """把一条 ``action_id → 回调`` 同时记进本宿主与模块级表（供 webhook 直接查）。"""
        key = register_action_route(action_id, callback, override=override)
        self.action_routes[key] = callback
        return key

    def dispatch_action(self, action_id: str, payload: Any = None) -> Any:
        """按 ``action_id`` 查到处理器并**真的调用**；查不到返回 ``None``。

        查的顺序：本宿主收到的（``self.action_routes``）→ 模块级 ``ACTION_ROUTES``。
        先查本宿主是因为它只包含"这次真的加载了的插件"：进程级表里可能还留着上一次
        加载的处理器，先命中它会把按钮派给一个已经不在的插件。
        """
        key = str(action_id or "").strip()
        callback = self.action_routes.get(key) or ACTION_ROUTES.get(key)
        if callback is None:
            return None
        return callback(payload)


    def describe(self) -> dict[str, Any]:
        """宿主快照：**host_ready 与 wired_into_main 是两件事**。

        - ``host_ready=True``：这一层本身可用（一行就能起、能接线、可验证）；
        - ``wired_into_main=False``：**没有任何生产启动路径调用它**。判据是"有没有人真的
          调过 :meth:`attach_to_app`"，不是自我评价。
        - ``missing_code``：缺的那一行代码（原样可粘），``missing_at``：粘在哪。
        - ``capabilities``（MATRIX 13.4）：4 条 ``exposed`` 能力的**宿主侧就位情况**。
          每条给出 ``ready`` / ``host_entry`` / ``missing``：``ready=True`` 表示入口已经
          建好、单测证明能被调到；``missing`` 是装配层还差的那一行。**不把 ready 说成
          wired**：入口在 ≠ 有人来取。
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
            "memory_providers": sorted(self.memory_providers()),
            "exporters": sorted(self.exporters()),
            "action_routes": sorted(self.action_routes),
            # 已经挂到 app 上的入站路由（空 = 还没人调过 attach_to_app）
            "routes": list(self._mounted_routes),
            "capabilities": self.capability_readiness(),
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
                "宿主门面已就绪（PluginHost.start() 能完成 warmup + 工具/hook/动作路由接线，"
                "且已被 tests/test_plugin_host.py 完整演练过），但 app/main.py 是禁改区，"
                "没有调用点；接线只需粘上 missing_code 那一行"
            ),
        }

    def capability_readiness(self) -> dict[str, dict[str, Any]]:
        """``exposed`` 那几条能力的就位情况：``ready`` / ``host_entry`` / ``missing``。

        列的判据是"**还没 wired** 或者 **入口已就位**"（``EXPOSED`` 或 ``ready``）：
        ``unsupported`` 的连入口都没有（缺的是整层，写在 landing 里），所以不在表里；
        而第三轮升成 ``wired`` 的两条（斜杠命令 / slack 动作）入口仍在、仍值得报，
        于是靠 ``ready`` 留在表里 —— 状态变了不该让"入口在哪"这件事从快照里消失。
        """
        rows: dict[str, dict[str, Any]] = {}
        for item in plugin_capabilities.CAPABILITIES:
            if item.orbit_status != plugin_capabilities.EXPOSED and not item.ready:
                continue
            rows[item.name] = {
                "ready": item.ready,
                "host_entry": item.host_entry,
                "missing": item.orbit_landing,
                "reason": item.reason,
            }
        return rows


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


def _jsonable(value: Any) -> Any:
    """把插件回调的返回值转成能塞进 JSON 响应的形态（转不动就退回字符串）。

    为什么不直接原样返回：插件 handler 返回自定义对象是合法写法（它不知道宿主会把它
    塞进 HTTP 响应），而路由里抛序列化异常会让 Slack 收到 500 并**不停重投同一个点击**。
    退回 ``str()`` 至少让人看到"它返回了什么"，比 500 好查得多。
    """
    if value is None or isinstance(value, (str, int, float, bool, list, dict)):
        return value
    try:
        from fastapi.encoders import jsonable_encoder

        return jsonable_encoder(value)
    except Exception:  # noqa: BLE001 —— 序列化失败不该变成 500
        return str(value)


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
    "ACTION_ROUTES",
    "AGENT_WIRING_CODE",
    "HOOKS_WIRING_CODE",
    "MAIN_WIRING_ANCHOR",
    "MAIN_WIRING_CODE",
    "HostWiring",
    "PluginHost",
    "dispatch_action",
    "register_action_route",
    "unregister_action_route",
]