"""插件能力面（MATRIX 13.4）：19 种 ``ctx.register_*`` 的完整清单、落点与**状态**。

## 这张表回答什么

`orbit plugins capabilities` 之前只能回答「哪些能力有落点」（一个二值判断），于是
「这个能力到底卡在哪一步」要靠人去读源码。本模块把它拆成三档，每档都有明确判据：

| 状态 | 判据 | 读者该做什么 |
|---|---|---|
| ``wired`` | 调用后**落到宿主真实结构上**，且该结构有消费方（工具注册表、hook 注册表、适配器池、CLI 注册表、页面登记表…） | 放心用 |
| ``exposed`` | API 存在、能登记，但**宿主里没有任何消费方**（没有那张表 / 没有那条链） | 能用但别指望生效，报告里写清缺哪一层 |
| ``unsupported`` | 连 API 落点都没有：**缺哪一层**逐条写在 ``orbit_landing`` 里 | 先补那一层 |

「缺哪一层」必须具体到模块与结构（例如「缺 ``app/retrieval.py`` 的可注入 provider
注册表」），**不许写「暂不支持」**——那句话对写插件的人零信息量，是本项目明令禁止的措辞。

## 与 Hermes 的 19 种怎么对上

Hermes 源码里真实出现的 ``ctx.register_*`` 恰好 19 个（对 ``_internal/plugins/**/*.py``
做调用点匹配，含 3 个平台扩展点与 3 个文档化 API）。其中 15 个在本表里有对应的 Orbit
方法（:attr:`Capability.hermes_name`），4 个 Orbit 根本没有（:data:`HERMES_ONLY`：
文生图 / 文生视频 / 浏览器后端 / 执行中间件）。两边合起来覆盖 19/19，用
:func:`hermes_coverage` 可以逐条核对——**不合并成一张表是为了不撒谎**：把
``register_image_gen_provider`` 硬塞进 Orbit 的方法列表，只会让「它有没有落点」
这个问题失去意义（Orbit 连文生图层都没有）。

## 为什么行键是 Orbit 的方法名

``orbit_status`` 要能被代码验证——「wired」意味着 ``getattr(PluginContext, name)``
真的存在且真的落到某个结构上。行键一旦换成 Hermes 的拼写，这个断言就做不到了。
所以：行键 = Orbit 方法名（= ``app.plugins.PLUGIN_CAPABILITIES``，19 个，一个不多一个
不少），Hermes 拼写记在 ``hermes_name`` 里。

## 与 `app/plugins.py` 旧快照的关系（读这段省一次困惑）

``app/plugins.py`` 里还有两张旧表 ``LANDED_CAPABILITIES``（9）与
``UNSUPPORTED_CAPABILITIES``（10）。它们**故意没有跟着本表一起改**：既有测试把 9/10
这个分布、以及「UNSUPPORTED 里的方法必须抛」都钉死了
（``tests/test_plugins.py::test_capability_split_is_exact_and_disjoint``）。所以本轮
升级出来的落点走**本模块的落点函数**（``wire_*`` / ``expose_*`` / :func:`dispatch`），
而 ``PluginContext`` 上的方法签名与旧行为保持不变。两边的差异用 :func:`drift` 全部
列出来，不藏在代码里。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from app import hooks as hooks_module
from app.plugins import PluginContext, PluginError

# ── 状态 ────────────────────────────────────────────────────────────────────

#: wired：有真实落点。
WIRED = "wired"
#: exposed：有 API、能登记，但宿主没有消费方。
EXPOSED = "exposed"
#: unsupported：连 API 落点都没有。
UNSUPPORTED = "unsupported"

CAPABILITY_STATUSES: dict[str, str] = {
    WIRED: "有真实落点：调用后落到宿主结构上，且该结构有消费方",
    EXPOSED: "有 API、能登记，但宿主里没有消费方（缺哪一层写在 orbit_landing 里）",
    UNSUPPORTED: "连 API 落点都没有（缺哪一层写在 orbit_landing 里）",
}

#: 状态的完成度排序（越大越「通」）。只给 :func:`drift` 与测试用。
_STATUS_RANK: dict[str, int] = {UNSUPPORTED: 0, EXPOSED: 1, WIRED: 2}


class CapabilityUnavailable(RuntimeError):
    """调用了一个 Orbit 里没有落点的能力。

    **为什么抛而不是静默返回**：静默返回等于「插件以为注册成功、宿主永远不会调用它」，
    这是插件系统里最难查的一类故障（没有报错、没有日志、功能就是不生效）。
    ``reason`` 与 ``missing_layer`` 一起带出去，让人知道下一步该补哪一层。
    """

    def __init__(self, capability: str, reason: str, missing_layer: str = "") -> None:
        self.capability = capability
        self.reason = reason
        self.missing_layer = missing_layer
        detail = f"{capability} 在 Orbit 里没有落点：{reason}"
        if missing_layer:
            detail += f"（缺：{missing_layer}）"
        super().__init__(detail)


# ── 数据结构 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Capability:
    """一条能力面记录。

    ``signature`` **不是手抄的**：模块导入时用 :func:`inspect.signature` 从
    ``PluginContext`` 上取（见 :func:`normalize_signature`），所以「表里写的签名」与
    「代码里真实的签名」不可能漂——测试也拿同一个函数复核。
    """

    #: Orbit 侧方法名（= ``PluginContext`` 上的属性名）。
    name: str
    #: Orbit 侧真实签名（由 :func:`normalize_signature` 生成，非手抄）。
    signature: str
    #: ``wired`` / ``exposed`` / ``unsupported``。
    orbit_status: str
    #: 真实落点（wired/exposed）或**缺哪一层**（unsupported，以及 exposed 的缺口）。
    orbit_landing: str
    #: 为什么是这个状态（给人读的一句话，必须能行动）。
    reason: str
    #: Hermes 实名（源码里的拼写）；Orbit 独有形态留空。
    hermes_name: str = ""
    #: Hermes 原文签名（调研 05 §1.5.1 的写法 + 真实调用点）。
    hermes_signature: str = ""
    #: Hermes bundled 插件里的真实调用点数（grep 证据，含注释行）。
    hermes_calls: int = 0
    #: 本轮为它建的接线点（``模块::函数``）。空 = 落点就是 PluginContext 自己。
    wired_by: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.orbit_status not in CAPABILITY_STATUSES:
            raise ValueError(f"{self.name}: 未知状态 {self.orbit_status!r}")
        if not self.reason.strip():
            raise ValueError(f"{self.name}: reason 不能为空（状态必须能解释）")
        if not self.orbit_landing.strip():
            raise ValueError(f"{self.name}: orbit_landing 不能为空（要么写落点，要么写缺哪层）")

    @property
    def wired(self) -> bool:
        return self.orbit_status == WIRED

    @property
    def hermes_names(self) -> tuple[str, ...]:
        """Hermes 实名列表。一个 Orbit 方法可能对应多个 Hermes 拼写。

        真实例子：``register_command``（TUI 斜杠命令）与 ``register_cli_command``
        （``hermes <子命令>``）在 Hermes 里是两个方法，在 Orbit 里落到**同一个** CLI
        注册表，所以这一行用「、」并列两个实名——不拆成两行是因为「落点」确实只有一个。
        """
        return tuple(part for part in (self.hermes_name or "").split("、") if part)

    @property
    def missing_layer(self) -> str:
        """缺哪一层（只有非 wired 才有意义）。"""
        return "" if self.wired else self.orbit_landing

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "signature": self.signature,
            "orbit_status": self.orbit_status,
            "orbit_landing": self.orbit_landing,
            "reason": self.reason,
            "hermes_name": self.hermes_name,
            "hermes_signature": self.hermes_signature,
            "hermes_calls": self.hermes_calls,
            "wired_by": list(self.wired_by),
        }


# ── 签名规范化 ──────────────────────────────────────────────────────────────


def normalize_signature(fn: Callable[..., Any], *, drop_self: bool = True) -> str:
    """把 ``inspect.signature`` 压成 ``name(a, b='', *, c=False)`` 这种一行形态。

    **为什么要这个函数**：能力表里的签名必须与代码一致。手抄一定会漂（改一次参数名
    就多一处谎言），所以两边都用同一个函数生成，测试再复核一次。
    """
    signature = inspect.signature(fn)
    parts: list[str] = []
    seen_star = False
    for parameter in signature.parameters.values():
        if drop_self and parameter.name == "self":
            continue
        if parameter.kind is inspect.Parameter.VAR_POSITIONAL:
            parts.append(f"*{parameter.name}")
            seen_star = True
            continue
        if parameter.kind is inspect.Parameter.VAR_KEYWORD:
            parts.append(f"**{parameter.name}")
            continue
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY and not seen_star:
            parts.append("*")
            seen_star = True
        text = parameter.name
        if parameter.default is not inspect.Parameter.empty:
            text += f"={parameter.default!r}"
        parts.append(text)
    return f"{getattr(fn, '__name__', 'callable')}({', '.join(parts)})"


def _cap(
    name: str,
    *,
    orbit_status: str,
    orbit_landing: str,
    reason: str,
    hermes_name: str = "",
    hermes_signature: str = "",
    hermes_calls: int = 0,
    wired_by: Sequence[str] = (),
) -> Capability:
    """构造一条记录，签名从 ``PluginContext`` 上现取（不手抄）。"""
    return Capability(
        name=name,
        signature=normalize_signature(getattr(PluginContext, name)),
        orbit_status=orbit_status,
        orbit_landing=orbit_landing,
        reason=reason,
        hermes_name=hermes_name,
        hermes_signature=hermes_signature,
        hermes_calls=hermes_calls,
        wired_by=tuple(wired_by),
    )


# ── 19 种能力（行键 = PluginContext 的方法名）───────────────────────────────

CAPABILITIES: tuple[Capability, ...] = (
    _cap(
        "register_tool",
        orbit_status=WIRED,
        orbit_landing=(
            "app/tools.py::ToolRegistry.register(tools, executor, source='plugin:<name>', "
            "override=有效值)——工具进 `registry.tools`，`specs()` 每轮都会列它"
        ),
        reason=(
            "真实落点：宿主把 ToolRegistry 当 tool_sink 注入后注册**当场生效**；未注入时"
            "只记在上下文里（测试/无宿主场景），这一点在 capabilities() 里看得见"
        ),
        hermes_name="register_tool",
        hermes_signature="register_tool(name, toolset=…, schema=…, handler=…, check_fn=…, emoji=…)",
        hermes_calls=4,
    ),
    _cap(
        "register_tools",
        orbit_status=WIRED,
        orbit_landing="同 register_tool（批量形态：逐个走 ToolRegistry.register）",
        reason=(
            "Orbit 侧便捷形态（Hermes 没有批量方法）：一次注册一组工具，但每个元素都单独"
            "过同名遮蔽门禁——它不是「整批放行」的后门"
        ),
    ),
    _cap(
        "register_hook",
        orbit_status=WIRED,
        orbit_landing=(
            "app/hooks.py::HookRegistry（17 个实证事件 + 2 个推断事件，优先级排序，"
            "单个 hook 抛异常不影响其它 hook）"
        ),
        reason=(
            "落点真实：注册即进注册表，`hooks.events()` 查得到。**但发射点还没接线**"
            "（5 处列在 app/hooks.py::WIRING_POINTS 里），所以「注册成功」不等于「会被触发」"
        ),
        hermes_name="register_hook",
        hermes_signature="register_hook(event, fn)",
        hermes_calls=38,
    ),
    _cap(
        "register_platform",
        orbit_status=WIRED,
        orbit_landing=(
            "platform_sink = app/platforms.py 的适配器池（gateway 的 resolve_adapter、"
            "`orbit send --to <name>`、cron 的 --deliver 都按名字从它取）"
        ),
        reason="真实落点：注入 platform_sink 后适配器当场可投递；不注入则只记在上下文里",
        hermes_name="register_platform",
        hermes_signature="register_platform(adapter)",
        hermes_calls=25,
    ),
    _cap(
        "register_platform_handler",
        orbit_status=WIRED,
        orbit_landing=(
            "适配器的边缘 hook `PlatformAdapter.on_message`：入站消息到达时插件处理器"
            "**先**跑，插件抛异常被就地隔离，不影响后续投递"
        ),
        reason=(
            "派发点已就位（包在 on_message 上）。**收信端还没有**：Orbit 的适配器只做"
            "出站文本，没有长轮询/webhook 入口，所以这条 hook 目前不会被真实入站消息触发"
        ),
        hermes_name="register_platform_handler",
        hermes_signature="register_platform_handler(platform, handler)",
        hermes_calls=15,
    ),
    _cap(
        "register_slack_action_handler",
        orbit_status=EXPOSED,
        orbit_landing=(
            "登记进 PluginContext.capability_ledger（键 = action_id，值 = 回调），并可由宿主"
            "注入的 sink 直接收下；**缺**入站派发：app/dashboard_api.py 的 "
            "`POST /api/webhooks/{id}` 已经提供了入站入口 + x-orbit-secret 校验，但它只把"
            "载荷落进收件箱（jsonl），没有 action_id 路由表，也没有任何代码把 Block Kit 动作"
            "翻译成插件回调的调用"
        ),
        reason=(
            "缺的是「收件箱 → action_id 路由表 → 插件回调」这最后一段：入站入口与签名校验"
            "（app/dashboard_api.py）已经有了，但没人解析 payload 里的 actions[].action_id，"
            "所以登记了也没有任何代码会来取它。要推成 wired，得在 webhook 收件路径上加一张"
            "action_id → callback 表并真的调用（那是 dashboard_api.py 的活，本轮不动它）"
        ),
        hermes_name="register_slack_action_handler",
        hermes_signature="register_slack_action_handler(action_id, cb)",
        hermes_calls=1,
        wired_by=("app/plugin_capabilities.py::expose_slack_action_handler",),
    ),
    _cap(
        "register_telegram_handler",
        orbit_status=WIRED,
        orbit_landing=(
            "app/plugin_capabilities.py::wire_telegram_handler → "
            "PluginContext.register_platform_handler('telegram', handler) → telegram 适配器"
            "的 on_message 边缘 hook（与通用平台处理器同一条落点，Hermes 里它本来就是别名）"
        ),
        reason=(
            "Hermes 原文把这个方法写成 register_platform_handler 的别名，Orbit 照抄该语义。"
            "注意 `PluginContext.register_telegram_handler` 本身仍抛（旧快照 9/10 的分布被"
            "既有测试钉住），落点入口是本模块的 wire_telegram_handler"
        ),
        hermes_name="register_telegram_handler",
        hermes_signature="register_telegram_handler(handler)  # = register_platform_handler('telegram', …)",
        hermes_calls=1,
        wired_by=("app/plugin_capabilities.py::wire_telegram_handler",),
    ),
    _cap(
        "register_model_provider",
        orbit_status=WIRED,
        orbit_landing=(
            "app/providers.py::PROVIDERS（模块级可变表，`orbit model` 与 resolve_provider "
            "都读它）"
        ),
        reason="真实落点：provider 定义当场进表，字段写错会当场报错而不是静默塞进去",
        hermes_signature="（Hermes 无此方法：model provider 由 kind: model-provider 目录 + 配置表达）",
    ),
    _cap(
        "register_memory_provider",
        orbit_status=EXPOSED,
        orbit_landing=(
            "PluginContext.capability_ledger + 宿主可注入 sink；**缺**装配处那一步："
            "app/memory_providers.py 其实有 provider 表（PROVIDER_TYPES / build_provider / "
            "build_router(providers=...)），但构造 MemoryRouter 的调用方不读插件登记的 provider"
            "——缺的是「把插件 provider 塞进 build_router」这一行，不是整张表"
        ),
        reason=(
            "登记完没有消费者：app/memory_providers.py::build_router 收 `providers=` 参数，"
            "可 CLI/装配层（app/cli/commands/memory_extra.py、app/agent.py）调它时只传内置的 "
            "default_providers()。要推成 wired，得让那几处把 ctx 里的 provider 一并传进去"
        ),
        hermes_name="register_memory_provider",
        hermes_signature="register_memory_provider(provider)",
        hermes_calls=8,
        wired_by=("app/plugin_capabilities.py::expose_memory_provider",),
    ),
    _cap(
        "register_web_provider",
        orbit_status=WIRED,
        orbit_landing=(
            "app/web_providers.py::EXTRA_PROVIDERS（模块级可变表）→ build_providers() 把它插在"
            "兜底的 ddgs 之前 → app/web.py::WebTools 构造时读 build_providers，"
            "select_provider()/available_providers() 也读它——注册后下一次搜索就会走到它"
        ),
        reason=(
            "真实落点（本轮补上）：上一轮 build_providers() 的工厂字典写在函数体里，插件 provider "
            "无处可读，所以只能算 exposed。现在宿主表 + register_provider() 契约校验都在，"
            "缺 available()/search() 的 provider 当场报错而不是静默躺进表里。"
            "**边界**：有 key 的内置 provider 仍优先（它们是操作员显式配的），插件 provider 排在"
            "ddgs 兜底之前——不这么排就永远选不到（ddgs.available() 恒真）"
        ),
        hermes_name="register_web_search_provider",
        hermes_signature="register_web_search_provider(p)",
        hermes_calls=10,
        wired_by=(
            "app/plugin_capabilities.py::expose_web_provider",
            "app/web_providers.py::register_provider",
            "app/web_providers.py::EXTRA_PROVIDERS",
        ),
    ),
    _cap(
        "register_cron_provider",
        orbit_status=UNSUPPORTED,
        orbit_landing=(
            "缺「调度后端抽象」这一层：app/cron.py 只有内置 interval/cron 表达式解析 + "
            "CronStore，没有 provider 接口，也没有按 provider 名分派的调用点"
        ),
        reason=(
            "要支持它得先造一个 CronScheduler 协议（submit/remove/list）并让 app/cron.py 的"
            "调度循环通过它派发；现在连「谁来调」都不存在"
        ),
        hermes_name="register_cron_scheduler",
        hermes_signature="register_cron_scheduler(p)",
        hermes_calls=1,
    ),
    _cap(
        "register_dashboard_page",
        orbit_status=WIRED,
        orbit_landing=(
            "app/plugin_dashboard.py::PageRegistry（注入 page_sink 后页面进宿主登记表，"
            "`orbit plugins pages` 直接消费）"
        ),
        reason=(
            "真实落点（本轮补上）：上一轮只写进 PluginContext.dashboard_pages，上下文一死就"
            "查不到了。**挂载仍未实现**——main.py 没有 include_router 的插件分支，所以"
            "describe() 里 mounted 恒为 False"
        ),
        hermes_signature="（Hermes 无此方法：dashboard 插件用 manifest.json + plugin_api.router 表达）",
        wired_by=(
            "app/plugin_dashboard.py::PageRegistry",
            "app/plugin_capabilities.py::wire_dashboard_page",
        ),
    ),
    _cap(
        "register_observability_exporter",
        orbit_status=EXPOSED,
        orbit_landing=(
            "app/plugin_capabilities.py::attach_observability_exporter → 把 exporter 的 "
            "record(payload) 挂到 ctx.hooks 的 19 个事件上（落点是真实的 HookRegistry）；"
            "**缺**推送端：app/metrics.py 的 CATALOG 是静态元组、collect() 是写死的采集"
            "函数，没有 exporter 注册表，且主循环的 5 个发射点未接线"
        ),
        reason=(
            "缺推送端：app/metrics.py 的 CATALOG 是静态元组、collect() 是写死的采集函数，"
            "没有 exporter 注册表；事件桥虽然挂上了，但主循环的 5 个发射点未接线"
            "（app/hooks.py::WIRING_POINTS），所以实际一个事件都收不到"
        ),
        hermes_signature="（Hermes 明确没有这个能力：可观测性导出靠 hook + middleware 组合）",
        wired_by=("app/plugin_capabilities.py::attach_observability_exporter",),
    ),
    _cap(
        "register_dashboard_auth",
        orbit_status=UNSUPPORTED,
        orbit_landing=(
            "缺认证层：Orbit 的 Web 服务没有认证中间件、没有 session token 机制"
            "（本机单用户 loopback，app/main.py 直接放行），auth provider 无处挂载"
        ),
        reason=(
            "要支持它得先有「谁在访问」这个前提：一个 auth provider 接口 + 中间件 + token "
            "签发/校验；三样都没有，注册进来等于给一个不存在的门配钥匙"
        ),
        hermes_name="register_dashboard_auth_provider",
        hermes_signature="register_dashboard_auth_provider(p)",
        hermes_calls=4,
    ),
    _cap(
        "register_skill",
        orbit_status=WIRED,
        orbit_landing=(
            "复制进 <data_dir>/skills/<name>/，app/skills.py 的 load_skill / catalog 每次"
            "直接读盘，`orbit skills list` 立刻看得到"
        ),
        reason=(
            "真实落点：复制而不是链接——卸载插件后技能仍然可用（用户可能正在用它），"
            "与 Hermes 的 register_skill 语义一致"
        ),
        hermes_name="register_skill",
        hermes_signature="register_skill(name, path)  # 文档化 API：bundled 树内无字面调用点",
    ),
    _cap(
        "register_auxiliary_task",
        orbit_status=UNSUPPORTED,
        orbit_landing=(
            "缺后台辅助任务队列这一层：Orbit 没有任务槽/调度器/重试语义（app/cron.py 是"
            "面向用户的调度器，不是插件维护任务的宿主）"
        ),
        reason=(
            "要支持它得先有「插件注册的周期性维护任务」这个容器：任务登记表 + 触发时机 + "
            "失败重试 + 与 cron 的边界；现在一条都没有"
        ),
        hermes_name="register_auxiliary_task",
        hermes_signature="register_auxiliary_task(task)  # 文档化 API：bundled 树内无字面调用点",
    ),
    _cap(
        "register_context_engine",
        orbit_status=UNSUPPORTED,
        orbit_landing=(
            "缺 engine 抽象：app/context.py 的三层上下文（stable/tier/volatile）是静态函数"
            "拼接，没有 ContextEngine 接口，也没有收集器去发现实现"
        ),
        reason=(
            "Hermes 用 _EngineCollector 捕获插件注册的引擎并在拼上下文时换掉实现；Orbit 的"
            "拼接是硬编码函数调用，换引擎等于改 app/context.py（禁改区）"
        ),
        hermes_name="register_context_engine",
        hermes_signature="register_context_engine(engine)  # 文档化 API：bundled 树内无字面调用点",
    ),
    _cap(
        "register_command",
        orbit_status=WIRED,
        orbit_landing=(
            "app/cli/spec.py::REGISTRY（构造 Command 登记进去，`orbit <name>` 立刻可用；"
            "与内置命令重名直接报错）"
        ),
        reason=(
            "真实落点：注册即出现在 `orbit --help` 与 REGISTRY 里。**语义差异要记住**："
            "Hermes 的 register_command 是 TUI 斜杠命令，Orbit 落到 CLI 子命令——两者都是"
            "「用户敲一个名字执行一段代码」，落点一致"
        ),
        hermes_name="register_command、register_cli_command",
        hermes_signature=(
            "register_command(name, handler, description='', args_hint='') / "
            "register_cli_command(name=…, help=…, setup_fn=…, handler_fn=…, description=…)"
        ),
        hermes_calls=6,
    ),
    _cap(
        "register_slash_command",
        orbit_status=EXPOSED,
        orbit_landing=(
            "登记（本模块 expose_slash_command）+ 解析（parse_slash_command）+ 分发入口"
            "（resolve_slash_command / app/plugin_host.py::PluginHost.dispatch_slash，"
            "已单测覆盖）都就位；**缺**输入循环里的那一行：app/cli/commands/chat.py 的 "
            "`line = input(\"> \")` 之后加 `outcome = host.dispatch_slash(text)`"
        ),
        reason=(
            "缺的是一行调用，不是一层能力：Orbit 只有 `orbit chat` 有输入循环，而该文件本轮"
            "正被另一个 agent 改写（git status 显示 M），两边同时改会互相覆盖，所以本轮不动它。"
            "接线方式已写死在 HOST_SURFACES['slash_dispatcher'] 与 wired_by 里，加一行即 wired"
        ),
        hermes_signature="（Hermes 无独立方法：斜杠命令就是 register_command）",
        wired_by=(
            "app/plugin_capabilities.py::expose_slash_command",
            "app/plugin_capabilities.py::resolve_slash_command",
            "app/plugin_host.py::PluginHost.dispatch_slash",
        ),
    ),
)

#: Hermes 真实存在、但 Orbit 的 19 个方法里**没有对应物**的能力。
#: 单独成表而不是硬塞进上面：塞进去会让「它有没有落点」变成假问题（Orbit 连那一层都没有）。
HERMES_ONLY: tuple[Capability, ...] = (
    Capability(
        name="register_image_gen_provider",
        signature="register_image_gen_provider(p)",
        orbit_status=UNSUPPORTED,
        orbit_landing=(
            "缺整个文生图层：app/ 下没有 image_gen 模块、没有 provider 表，"
            "app/tools.py 的 LOCAL_TOOLS 里也没有任何图像生成工具入口"
        ),
        reason=(
            "Hermes 有 7 个文生图后端插件（flux / recraft / …）；Orbit 一个都没有，"
            "所以缺的不是「注册表」而是「整层功能」"
        ),
        hermes_name="register_image_gen_provider",
        hermes_signature="register_image_gen_provider(p)",
        hermes_calls=7,
    ),
    Capability(
        name="register_video_gen_provider",
        signature="register_video_gen_provider(p)",
        orbit_status=UNSUPPORTED,
        orbit_landing=(
            "缺整个文生视频层：与文生图同因（没有模块、没有表、没有工具入口），且视频还要"
            "多一层「任务轮询」——长耗时任务的状态机"
        ),
        reason="Hermes 有 3 个文生视频后端；Orbit 没有这一层，注册进来没有任何代码会调用",
        hermes_name="register_video_gen_provider",
        hermes_signature="register_video_gen_provider(p)",
        hermes_calls=3,
    ),
    Capability(
        name="register_browser_provider",
        signature="register_browser_provider(p)",
        orbit_status=UNSUPPORTED,
        orbit_landing=(
            "缺浏览器后端注册表：app/browser.py 的驱动是写死的（playwright），没有 "
            "BROWSER_PROVIDERS 表，也没有按名字 build 的分派点"
        ),
        reason=(
            "要支持它得先让浏览器层可插拔：后端协议（launch / new_context / close）+ 注册表 "
            "+ settings 里选后端；现在只有一种实现，且是硬编码构造"
        ),
        hermes_name="register_browser_provider",
        hermes_signature="register_browser_provider(p)",
        hermes_calls=3,
    ),
    Capability(
        name="register_middleware",
        signature="register_middleware(kind, fn)",
        orbit_status=EXPOSED,
        orbit_landing=(
            "app/plugin_capabilities.py::attach_middleware 把 fn 挂到对应的 pre/post hook 上"
            "（llm_execution → pre_llm_call/post_llm_call；tool_execution → "
            "pre_tool_call/post_tool_call）"
        ),
        reason=(
            "缺中间件链：app/graph/nodes.py 的工具派发是直接 await，没有可插入的 chain；"
            "hook 只能观察（transform_tool_result 可改写结果，但没有拦截/短路语义），"
            "所以只能降级成 hook 订阅"
        ),
        hermes_name="register_middleware",
        hermes_signature="register_middleware(kind, fn)  # kind ∈ llm_execution / tool_execution",
        hermes_calls=2,
        wired_by=("app/plugin_capabilities.py::attach_middleware",),
    ),
)

#: 中间件 kind → 落到哪两个 hook（Hermes 的两个 kind 只有这两个）。
MIDDLEWARE_EVENTS: dict[str, tuple[str, str]] = {
    "llm_execution": ("pre_llm_call", "post_llm_call"),
    "tool_execution": ("pre_tool_call", "post_tool_call"),
}

#: 宿主真实消费面清单（值 = 谁在消费）。写在这里是为了让 wired 有据可查：
#: 说某个能力 wired，就得能指着这张表说出消费者是谁。
HOST_SURFACES: dict[str, str] = {
    "tool_sink": "app/tools.py::ToolRegistry.specs()（每轮拼提示词都会读）",
    "hook_registry": "app/hooks.py::HookRegistry.emit()/aemit()（发射点见 WIRING_POINTS）",
    "platform_sink": "app/platforms.py 适配器池 → gateway resolve_adapter / orbit send",
    "cli_registry": "app/cli/spec.py::REGISTRY → app/cli/main.py::build_parser()",
    "skills_root": "app/skills.py::catalog()/load_skill()（每次直接读盘）",
    "providers_table": "app/providers.py::PROVIDERS → orbit model / resolve_provider",
    "page_registry": "app/plugin_dashboard.py::PAGES → orbit plugins pages",
    "plugin_routes": (
        "app/plugin_dashboard.py::mount()/mount_into_app() → fastapi include_router；"
        "宿主 app/main.py 还没调它（禁改区），但入口与逐条隔离都已就位"
    ),
    "slash_dispatcher": (
        "（入口就位、调用方缺）app/plugin_host.py::PluginHost.dispatch_slash 已能分发并被单测"
        "覆盖，但 app/cli/commands/chat.py 的输入循环还没调它（该文件本轮正被另一个 agent 改写）"
    ),
    # 下面这些**不存在**，所以对应的能力最多到 exposed：
    "memory_provider_registry": (
        "（不存在）app/memory_providers.py 有 provider 表，但构造 MemoryRouter 的调用方不传插件 provider"
    ),
    "web_provider_registry": "（不存在）app/web_providers.py::build_providers 工厂硬编码在函数体里",
    "inbound_webhook": (
        "（部分存在）app/dashboard_api.py::receive_webhook 只落盘；缺 action_id 路由表与派发"
    ),
    "middleware_chain": "（不存在）app/graph/nodes.py 直接 await，没有可插入的链",
}



# ── 落点函数：wired / exposed 两种 ──────────────────────────────────────────


def _push(sink: Any, key: str, value: Any) -> bool:
    """把登记项推进宿主 sink（``register(key, value)`` 或直接 ``sink[key] = value``）。"""
    if sink is None:
        return False
    register = getattr(sink, "register", None)
    if callable(register):
        register(key, value)
        return True
    try:
        sink[key] = value
    except (TypeError, KeyError):  # 不可变/只读的 sink：只登记不注入
        return False
    return True


def _require_text(value: Any, *, what: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise PluginError(f"{what} 需要一个非空名字")
    return text


def wire_telegram_handler(ctx: PluginContext, handler: Any) -> str:
    """``register_telegram_handler`` 的真实落点：委托给通用平台入站处理器。

    Hermes 原文把这个方法写成 *alias of register_platform_handler*，Orbit 照抄该语义——
    落到 telegram 适配器的 ``on_message`` 边缘 hook 上。**不另造一条派发路径**：
    两套入口指向同一个结构，才不会出现「一个触发、另一个永不触发」。
    """
    ctx.note_capability("register_telegram_handler")
    return ctx.register_platform_handler("telegram", handler)


def wire_dashboard_page(
    ctx: PluginContext,
    page: Any = None,
    *,
    sink: Any = None,
    **fields: Any,
) -> dict[str, Any]:
    """``register_dashboard_page`` 的落点入口（可临时指定 sink）。

    与直接调 ``ctx.register_dashboard_page`` 的差别只有一条：这里允许把 sink 传进来，
    于是「装配时才发现登记表」的场景也能落到真实结构上。
    """
    ctx.note_capability("register_dashboard_page")
    if sink is not None:
        ctx.page_sink = sink
    return ctx.register_dashboard_page(page, **fields)


def expose_memory_provider(
    ctx: PluginContext, provider: Any = None, *, provider_id: str = "", sink: Any = None
) -> str:
    """登记一个外部记忆 provider（**exposed**：宿主没有那张表）。

    ``sink`` 传进来才算真的注入（宿主自己造一个注册表即可把它推到 wired）。返回值是
    provider id——与 Hermes 的「排他激活」语义对齐：同一时刻只有一个生效，所以宿主换
    provider 时要能按 id 找到它。
    """
    key = _require_text(provider_id or getattr(provider, "id", ""), what="memory provider")
    ctx.record_capability(
        "register_memory_provider",
        {"id": key, "provider": provider, "injected": _push(sink, key, provider)},
    )
    return key


def expose_web_provider(
    ctx: PluginContext, provider: Any = None, *, provider_id: str = "", sink: Any = None
) -> str:
    """登记一个 web 搜索/抽取 provider——**真实落点是宿主表** ``web_providers.EXTRA_PROVIDERS``。

    这一条本轮从 ``exposed`` 升到 ``wired``：``app/web_providers.py::build_providers`` 现在会
    带上这张表，而 ``app/web.py::WebTools`` 构造时就会调它——所以"注册完没有消费者"不再成立。
    ``sink`` 仍可覆盖落点（测试 / 多实例隔离）；不传就是宿主那张进程级表。
    """
    key = _require_text(provider_id or getattr(provider, "name", ""), what="web provider")
    # 契约检查（available/search）在宿主表那边做：缺方法的 provider 放进去只会变成
    # "注册成功、永远不被调用"——那正是 exposed 档最典型的故障形态。
    from app import web_providers

    web_providers.register_provider(key, provider)
    target = sink if sink is not None else web_providers.EXTRA_PROVIDERS
    if target is not web_providers.EXTRA_PROVIDERS:
        _push(target, key, provider)
    ctx.record_capability(
        "register_web_provider",
        {"id": key, "provider": provider, "injected": True},
    )
    return key


def expose_slack_action_handler(
    ctx: PluginContext, action_id: str, callback: Any, *, sink: Any = None
) -> str:
    """登记一个 Slack Block Kit 动作处理器（**exposed**：没有入站 webhook）。"""
    key = _require_text(action_id, what="Slack action_id")
    if not callable(callback):
        raise PluginError(f"register_slack_action_handler({key}) 需要可调用对象")
    ctx.record_capability(
        "register_slack_action_handler",
        {"action_id": key, "callback": callback, "injected": _push(sink, key, callback)},
    )
    return key


def expose_slash_command(
    ctx: PluginContext,
    name: str,
    handler: Any,
    *,
    description: str = "",
    sink: Any = None,
) -> str:
    """登记一个斜杠命令（**exposed**：没有分发循环）。

    归一规则照 Hermes 的 ``_EngineCollector.register_command``：小写、去掉前导 ``/``、
    空格换连字符——两边对同一个输入必须得到同一个名字，否则「注册的」和「用户敲的」
    永远对不上。
    """
    clean = str(name or "").strip().lower().lstrip("/").replace(" ", "-")
    key = _require_text(clean, what="斜杠命令")
    if not callable(handler):
        raise PluginError(f"register_slash_command({key}) 需要可调用对象")
    ctx.record_capability(
        "register_slash_command",
        {
            "name": key,
            "handler": handler,
            "description": description,
            "injected": _push(sink, key, handler),
        },
    )
    return key


def parse_slash_command(ctx: PluginContext, text: str) -> tuple[str, str] | None:
    """把 ``/name 其余`` 解析成 ``(name, 其余)``；不是已登记的命令就返回 ``None``。

    这是解析层：只回答"这行输入指的是哪个命令、参数是什么"，不负责调用。
    """
    body = str(text or "").strip()
    if not body.startswith("/"):
        return None
    head, _, rest = body[1:].partition(" ")
    key = head.strip().lower().replace(" ", "-")
    known = {
        str(item.get("name", ""))
        for item in ctx.capability_ledger.get("register_slash_command", [])
    }
    if key not in known:
        return None
    return key, rest.strip()


def resolve_slash_command(
    ctx: PluginContext, text: str
) -> tuple[str, Callable[..., Any], str] | None:
    """``/name 其余`` → ``(name, handler, 其余)``；不是已登记的命令就返回 ``None``。

    **与 :func:`parse_slash_command` 的分工**：那个只解析，这个把 handler 一并取出来——
    因为 ``register_slash_command`` 从 ``exposed`` 升到 ``wired`` 靠的就是"有人真的去调
    handler"。消费方是 ``app/plugin_host.py::PluginHost.dispatch_slash``（``orbit chat``
    的输入循环），所以"注册了但没人调"这件事现在有了唯一的判定点。

    查不到就返回 ``None``（**不抛**）：chat 要把这行当普通消息发给 agent，不能因为用户
    敲了个没注册的 ``/foo`` 就中断对话。
    """
    parsed = parse_slash_command(ctx, text)
    if parsed is None:
        return None
    key, rest = parsed
    for item in ctx.capability_ledger.get("register_slash_command", []):
        if str(item.get("name", "")) != key:
            continue
        handler = item.get("handler")
        if not callable(handler):
            return None
        return key, handler, rest
    return None


def attach_observability_exporter(
    ctx: PluginContext, exporter: Any = None, *, sink: Any = None
) -> str:
    """把 exporter 挂到 ``ctx.hooks`` 的 19 个事件上（**exposed**：没有推送端）。

    为什么还是做这件事：exporter 的 ``record(payload)`` 契约先定下来，接线时只需要让
    主循环真的 emit——不用再改插件侧。缺的是发射点，不是 exporter。
    """
    name = _require_text(
        getattr(exporter, "name", "") or type(exporter).__name__, what="exporter"
    )
    record = getattr(exporter, "record", None)
    if not callable(record):
        raise PluginError(f"exporter {name} 没有 record(payload) 方法，接进来也没有调用约定")
    # 先钉住类型再进闭包：`record` 是 `Any | None`，直接当默认值 mypy 会报类型不符。
    recorder: Callable[[dict[str, Any]], Any] = record

    def bridge(payload: dict[str, Any], _record: Callable[[dict[str, Any]], Any] = recorder) -> None:
        _record(dict(payload))

    events: list[str] = []
    for event in hooks_module.ALL_EVENTS:
        events.append(ctx.hooks.register(event, bridge))
    ctx.record_capability(
        "register_observability_exporter",
        {"name": name, "events": events, "injected": _push(sink, name, exporter)},
    )
    return name


def attach_middleware(ctx: PluginContext, kind: str, fn: Any, *, sink: Any = None) -> str:
    """把 Hermes 的 ``register_middleware`` 降级挂到对应的 pre/post hook 上（**exposed**）。

    降级的代价必须说清：hook 拿不到「改写工具结果」的能力（除了
    ``transform_tool_result``），也没有短路语义。真正的中间件链要等
    ``app/graph/nodes.py`` 有可插入的 chain。
    """
    key = _require_text(kind, what="middleware kind")
    if key not in MIDDLEWARE_EVENTS:
        raise PluginError(
            f"未知 middleware kind：{key}（合法取值：{'/'.join(sorted(MIDDLEWARE_EVENTS))}）"
        )
    if not callable(fn):
        raise PluginError(f"middleware {key} 需要可调用对象")
    registered: list[str] = []
    for event in MIDDLEWARE_EVENTS[key]:
        registered.append(ctx.hooks.register(event, fn))
    ctx.record_capability(
        "register_middleware",
        {"kind": key, "events": registered, "injected": _push(sink, key, fn)},
    )
    return key


#: 需要走本模块落点函数的能力（其余能力直接调 ``PluginContext`` 上的同名方法）。
#: **为什么 exposed 的也在这里**：它们在 ``PluginContext`` 上仍是「抛异常」的旧行为
#: （见模块 docstring 最后一段），落点只能由本模块提供。
LANDING_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "register_telegram_handler": wire_telegram_handler,
    "register_dashboard_page": wire_dashboard_page,
    "register_memory_provider": expose_memory_provider,
    "register_web_provider": expose_web_provider,
    "register_slack_action_handler": expose_slack_action_handler,
    "register_slash_command": expose_slash_command,
    "register_observability_exporter": attach_observability_exporter,
    "register_middleware": attach_middleware,
}


def dispatch(capability: str, ctx: PluginContext, *args: Any, **kwargs: Any) -> Any:
    """按能力名调用**当前最合适**的落点（wired/exposed 都走，unsupported 抛）。

    这是宿主与插件作者唯一需要记的入口：不用知道「这个方法在 PluginContext 上，还是在本
    模块的落点函数里」。返回落点函数/方法自己的返回值。
    """
    record = by_name(capability)
    if record is None:
        known = "、".join(item.name for item in (*CAPABILITIES, *HERMES_ONLY))
        raise CapabilityUnavailable(capability, f"不在能力面清单里（已知：{known}）")
    if record.orbit_status == UNSUPPORTED:
        raise CapabilityUnavailable(capability, record.reason, record.missing_layer)
    landing = LANDING_FUNCTIONS.get(capability)
    if landing is not None:
        return landing(ctx, *args, **kwargs)
    method = getattr(ctx, capability, None)
    if not callable(method):
        raise CapabilityUnavailable(capability, "PluginContext 上没有这个方法，也没有落点函数")
    return method(*args, **kwargs)


# ── 查询与汇总 ──────────────────────────────────────────────────────────────


def by_name(capability: str) -> Capability | None:
    """按名字查一条记录（两张表都查）。"""
    for item in (*CAPABILITIES, *HERMES_ONLY):
        if item.name == capability:
            return item
    return None


def status_of(capability: str) -> str:
    """某个能力的状态；不认识就 ``unknown``（不抛：报告类调用不该炸）。"""
    record = by_name(capability)
    return record.orbit_status if record else "unknown"


def landing_of(capability: str) -> str:
    """某个能力的落点或缺层说明；不认识返回空串。"""
    record = by_name(capability)
    return record.orbit_landing if record else ""


def by_status(status: str) -> tuple[Capability, ...]:
    """某一档的全部能力（只在 ``CAPABILITIES`` 内，不含 Hermes 独有项）。"""
    return tuple(item for item in CAPABILITIES if item.orbit_status == status)


def summary() -> dict[str, Any]:
    """19 种能力的分布 + 与旧快照的对比（`orbit plugins capabilities --json` 的数据源）。"""
    counts = {status: 0 for status in CAPABILITY_STATUSES}
    for item in CAPABILITIES:
        counts[item.orbit_status] += 1
    change = drift()
    return {
        "total": len(CAPABILITIES),
        "wired": counts[WIRED],
        "exposed": counts[EXPOSED],
        "unsupported": counts[UNSUPPORTED],
        "hermes_only": len(HERMES_ONLY),
        "legacy_landed": len(change["legacy"]["landed"]),
        "upgraded": [item["name"] for item in change["upgraded"]],
        "statuses": dict(CAPABILITY_STATUSES),
    }


def drift() -> dict[str, Any]:
    """本表与 `app/plugins.py` 旧快照（9 landed / 10 unsupported）的差异。

    **为什么要报这个**：旧快照没有跟着升级（既有测试钉住了 9/10 的分布），于是仓库里
    同时存在两套口径。把它显式列出来，比让读代码的人自己发现要好。
    """
    from app import plugins as plugins_module

    legacy_landed = tuple(sorted(plugins_module.LANDED_CAPABILITIES))
    legacy_unsupported = tuple(sorted(plugins_module.UNSUPPORTED_CAPABILITIES))
    upgraded: list[dict[str, str]] = []
    downgraded: list[dict[str, str]] = []
    unchanged: list[str] = []
    for item in CAPABILITIES:
        if item.name in legacy_landed:
            legacy = WIRED
        elif item.name in legacy_unsupported:
            legacy = UNSUPPORTED
        else:  # 旧快照没覆盖（不该发生：两边的名字集合应当一致）
            upgraded.append(
                {"name": item.name, "legacy": "unknown", "current": item.orbit_status}
            )
            continue
        if _STATUS_RANK[item.orbit_status] > _STATUS_RANK[legacy]:
            upgraded.append({"name": item.name, "legacy": legacy, "current": item.orbit_status})
        elif _STATUS_RANK[item.orbit_status] < _STATUS_RANK[legacy]:
            downgraded.append({"name": item.name, "legacy": legacy, "current": item.orbit_status})
        else:
            unchanged.append(item.name)
    return {
        "upgraded": upgraded,
        "downgraded": downgraded,
        "unchanged": unchanged,
        "legacy": {"landed": legacy_landed, "unsupported": legacy_unsupported},
        "note": (
            "旧快照（app/plugins.py 的 LANDED/UNSUPPORTED）保持 9/10 不变：既有测试钉住了"
            "这个分布，且 PluginContext 上的方法行为不变。升级出来的落点走 "
            "app/plugin_capabilities.py 的落点函数与 dispatch()"
        ),
    }


def hermes_coverage() -> dict[str, Any]:
    """核对 19 种 Hermes 能力的覆盖：15 个有对应物 + 4 个 Hermes 独有。

    ``register_command`` / ``register_cli_command`` 并列在同一行（落点相同），所以
    「有对应物」的实名数是 15，行数是 14。
    """
    mapped: list[dict[str, str]] = []
    for item in CAPABILITIES:
        for hermes in item.hermes_names:
            mapped.append({"hermes": hermes, "orbit": item.name, "status": item.orbit_status})
    missing = [
        {"hermes": item.name, "status": item.orbit_status, "missing": item.orbit_landing}
        for item in HERMES_ONLY
    ]
    names = [item["hermes"] for item in mapped] + [item["hermes"] for item in missing]
    return {
        "hermes_total": len(names),
        "mapped": len(mapped),
        "hermes_only": len(missing),
        "complete": len(names) == 19 and len(set(names)) == 19,
        "names": sorted(names),
        "mapped_rows": mapped,
        "missing_rows": missing,
    }


def render_table(records: Iterable[Capability] = CAPABILITIES) -> list[tuple[str, str, str]]:
    """给 CLI 表格用的三列（名字 / 状态 / 落点或缺层）。"""
    return [(item.name, item.orbit_status, item.orbit_landing) for item in records]


def declared_status(declared: Iterable[str]) -> list[dict[str, str]]:
    """把 manifest 推导出来的「声明能力」翻译成状态行（`orbit plugins info` 用）。

    不认识的名字（manifest 里写了 Orbit 完全没有的能力）标 ``unknown`` 并保留原名——
    报「不认识」比悄悄丢掉诚实。
    """
    rows: list[dict[str, str]] = []
    for name in declared:
        record = by_name(name)
        rows.append(
            {
                "name": name,
                "status": record.orbit_status if record else "unknown",
                "landing": record.orbit_landing if record else "（不在 Orbit 的能力面里）",
            }
        )
    return rows


__all__ = [
    "CAPABILITIES",
    "CAPABILITY_STATUSES",
    "EXPOSED",
    "HERMES_ONLY",
    "HOST_SURFACES",
    "LANDING_FUNCTIONS",
    "MIDDLEWARE_EVENTS",
    "UNSUPPORTED",
    "WIRED",
    "Capability",
    "CapabilityUnavailable",
    "attach_middleware",
    "attach_observability_exporter",
    "by_name",
    "by_status",
    "declared_status",
    "dispatch",
    "drift",
    "expose_memory_provider",
    "expose_slack_action_handler",
    "expose_slash_command",
    "expose_web_provider",
    "hermes_coverage",
    "landing_of",
    "normalize_signature",
    "parse_slash_command",
    "render_table",
    "resolve_slash_command",
    "status_of",
    "summary",
    "wire_dashboard_page",
    "wire_telegram_handler",
]
