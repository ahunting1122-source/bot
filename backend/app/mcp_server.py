"""把 Orbit 暴露成 MCP 服务（stdio）：Orbit 的工具表 → MCP tools。

## 为什么是"工具表 → 定义"的纯函数，而不是逐个手写 @server.tool()

MCP 的 ``@server.tool()`` 装饰器从**函数签名**推导参数 schema。Orbit 的工具参数来自
``ToolRegistry``（内置工具是 pydantic 模型、外部工具是服务给的 JSON Schema），签名里
根本没有这些信息。手写一层转发函数等于把 25+ 个工具的 schema 抄第二遍，而抄出来的那份
一定会和真实工具表漂移——``orbit mcp serve`` 报出去的参数和 ``orbit tools list`` 显示的
不一致，是最难发现的一类 bug。

所以本模块的做法是：**声明从工具表来，实现只留一个转发壳**。于是
:func:`to_mcp_tools` 是一个纯函数（输入 spec 列表，输出 MCP ``Tool`` 定义），可以脱离
进程与网络单测——这正是本层唯一"必须可测"的部分。

## 执行侧：**本轮未接线**，如实报错而不是假装成功

``executor`` 为 None（当前唯一的生产用法）时，工具调用会返回一条明确的错误：本进程没有
绑定 Orbit 的 ``ToolRegistry``/``Store``，因此只能**声明**工具而无法执行。原因写在这里，
免得下一个人以为是漏了：

- 执行一个 Orbit 工具需要 ``Settings`` + ``Store``（操作幂等表）+ 任务上下文
  （``task_id`` / ``conversation_id``），还要和正在跑的后端**共享同一个 SQLite 库**；
- ``mcp serve`` 是另一个进程，草率地在里面开库会与后端抢写锁（WAL 也挡不住跨进程的
  长事务），而且没有任务上下文可挂——写进去的 ``Operation`` 行会成为孤儿；
- 因此真实执行属于"会话面"（`app/session_plane.py` 那一层）该解决的问题，不是本波次。

## 参数校验为什么要动 SDK 内部

转发壳的签名只能是 ``**arguments``，SDK 据此推出的校验模型是个只认 ``kwargs`` 的模型。
把 ``parameters`` 换成真实 schema 之后，校验仍走那个模型，合法调用会在校验阶段就被拒。
所以给每个工具挂一个**透传**的参数模型（:class:`_PassThroughArgs`）：不做字段校验
（外部契约本来就不该由我们校验，见 ``app/tools.py`` 的 ``Tool`` docstring），原样把
参数交给转发壳。这段依赖 SDK 的 ``ArgModelBase``，导入失败时降级为"不挂透传模型"并记
日志——**降级后工具仍能列出**，只是执行不可用，而执行本来就还没接线。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.tools.base import Tool as ServerTool
from mcp.types import Tool as McpTool
from pydantic import ConfigDict

log = logging.getLogger("orbit.mcp.server")

#: 对外报的服务名。
SERVER_NAME = "orbit"
#: 服务自我介绍（MCP 的 ``instructions``，客户端会把它拼进上下文）。
INSTRUCTIONS = (
    "Orbit 的本地工具集（文件读写、命令执行、长期记忆、历史检索、技能）。"
    "工具参数 schema 与 Orbit 自身的工具表同源。"
)
#: 单个工具的说明上限：MCP 没有硬上限，但说明是要进对端上下文的，不能无界。
MAX_DESCRIPTION = 1000
#: 工具名合法字符（SEP-986：``[A-Za-z0-9._-]{1,128}``）。
ALLOWED_NAME = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
MAX_NAME = 128
#: 没声明参数时的兜底 schema（MCP 要求 ``inputSchema`` 是 object 根）。
EMPTY_SCHEMA: dict[str, Any] = {"type": "object", "properties": {}}


def tool_schema(spec: Any) -> dict[str, Any]:
    """从一条工具声明里取出 ``{name, description, parameters}``。

    接受两种形状：``ToolRegistry.specs()`` 的 OpenAI 风格
    （``{"type":"function","function":{…}}``）与裸的 ``{"name","description","parameters"}``。
    两种都认，是因为 CLI 侧与测试侧手里的东西不一定是同一种——为这点差异逼调用方转换
    一次，转换代码迟早会和真实形状脱节。
    """
    if not isinstance(spec, dict):
        return {}
    body = spec.get("function")
    if not isinstance(body, dict):
        body = spec
    name = str(body.get("name") or "").strip()
    parameters = body.get("parameters")
    return {
        "name": name,
        "description": str(body.get("description") or "").strip(),
        "parameters": parameters if isinstance(parameters, dict) else EMPTY_SCHEMA,
    }


def valid_name(name: str) -> bool:
    """工具名是否满足 SEP-986。不合规的工具**跳过并记日志**，不改名——改名会让对端看到的
    名字与 Orbit 工具表对不上，等于制造了一个查不出出处的别名。"""
    return bool(name) and len(name) <= MAX_NAME and set(name) <= ALLOWED_NAME


def to_mcp_tools(specs: Any) -> list[McpTool]:
    """``ToolRegistry.specs()`` → MCP ``Tool`` 定义列表（**纯函数**：无 IO、无全局状态）。

    行为：

    - 名字不合规（SEP-986）或为空的条目**跳过**并记日志；
    - ``description`` 截到 :data:`MAX_DESCRIPTION`；
    - 参数 schema **原样透传**（外部契约不由我们校验，见 `app/tools.py`）；
    - 同名条目只保留第一条（``MCPServer`` 遇到重名会告警并忽略后来的，这里提前去重，
      免得日志里出现一条看不出谁赢了的 warning）。
    """
    tools: list[McpTool] = []
    seen: set[str] = set()
    for spec in specs or []:
        shape = tool_schema(spec)
        name = shape["name"]
        if not name:
            continue
        if not valid_name(name):
            log.warning("工具名 %r 不符合 MCP 命名规则，未暴露", name)
            continue
        if name in seen:
            log.warning("工具名 %r 重复，只暴露第一条", name)
            continue
        seen.add(name)
        tools.append(
            McpTool(
                name=name,
                description=shape["description"][:MAX_DESCRIPTION],
                input_schema=shape["parameters"],
            )
        )
    return tools


def _pass_through_args() -> Any:
    """透传参数模型：不做字段校验，原样把 ``arguments`` 交给转发壳。

    ``ArgModelBase.model_dump_one_level`` 只 dump ``model_fields``，所以这里必须覆盖它
    把 ``model_extra`` 一起交出去，否则参数会在校验阶段被静默丢掉（症状是工具收到
    空参数，而且不报错）。
    """
    try:
        from mcp.server.mcpserver.utilities.func_metadata import ArgModelBase
    except Exception as exc:  # noqa: BLE001 - SDK 换了内部结构：降级，不挂透传模型
        log.warning("无法构造 MCP 参数透传模型（SDK 内部结构变化？）：%s", exc)
        return None

    class PassThrough(ArgModelBase):
        model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

        def model_dump_one_level(self) -> dict[str, Any]:
            return dict(self.model_extra or {})

    return PassThrough


def _render_result(value: Any) -> str:
    """执行结果 → MCP 文本内容。dict/list 走 JSON（对端能直接解析），其余走 str。"""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(value)


def _forwarder(
    tool_name: str,
    executor: Callable[[str, dict], Awaitable[Any]] | None,
) -> Callable[..., Awaitable[str]]:
    """给一个工具造转发壳。

    工具名走**闭包**而不是函数参数：SDK 拒绝以下划线开头的参数名
    （``InvalidSignature: Parameter … cannot start with '_'``），而把名字写成普通参数
    又会出现在对端看到的 schema 里。
    """

    async def invoke(**arguments: Any) -> str:
        if executor is None:
            # 抛而不是返回文本：MCP 有 isError 语义，把失败伪装成成功的文本会被对端
            # 模型当成"工具真的跑过了"。
            raise RuntimeError(
                f"工具 {tool_name} 未接线：本进程没有绑定 Orbit 的 ToolRegistry/Store，"
                "只能声明工具而无法执行（原因见 app/mcp_server.py 的模块 docstring）"
            )
        return _render_result(await executor(tool_name, dict(arguments)))

    return invoke


def build_server(
    specs: Any,
    *,
    executor: Callable[[str, dict], Awaitable[Any]] | None = None,
    name: str = SERVER_NAME,
) -> MCPServer:
    """按工具声明组装一个 MCP 服务对象（**不启动**，测试可以直接检查它）。

    每个工具挂一个转发壳：有 ``executor`` 就交给它，没有就抛一条能读懂的错误。
    抛错而不是返回一段"未实现"的文本，是因为 MCP 有 ``isError`` 语义——把失败伪装成
    成功的文本结果，对端模型会当成工具真的跑过了。
    """
    tools: list[ServerTool] = []
    pass_through = _pass_through_args()
    for spec in specs or []:
        shape = tool_schema(spec)
        tool_name = shape["name"]
        if not tool_name or not valid_name(tool_name):
            continue
        tool = ServerTool.from_function(
            _forwarder(tool_name, executor),
            name=tool_name,
            description=shape["description"][:MAX_DESCRIPTION],
        )
        # 声明用工具表的 schema，而不是从 `**arguments` 推出来的那个。
        tool = tool.model_copy(update={"parameters": shape["parameters"]})
        if pass_through is not None:
            tool.fn_metadata.arg_model = pass_through
        tools.append(tool)
    server = MCPServer(name, instructions=INSTRUCTIONS, tools=tools)
    log.debug("MCP 服务 %s 已组装，暴露 %d 个工具", name, len(tools))
    return server


async def serve(
    specs: Any,
    *,
    executor: Callable[[str, dict], Awaitable[Any]] | None = None,
    name: str = SERVER_NAME,
) -> None:
    """在**当前进程的 stdin/stdout** 上跑 MCP stdio 服务（阻塞直到客户端断开）。

    注意：stdio 协议占用了 stdout，本函数之前/之后任何 ``print`` 都会污染协议流。
    调用方（`orbit mcp serve`）必须把提示信息写到 stderr。
    """
    server = build_server(specs, executor=executor, name=name)
    await server.run_stdio_async()


def registry_specs(registry) -> list[dict]:
    """从 ``ToolRegistry`` 取工具声明（**只读**，不改注册表）。

    平台过滤沿用 ``ToolRegistry.specs()`` 的默认值：`mcp serve` 是"把当前 CLI 能用的
    工具暴露出去"，不是另一套工具面。要换子集请改 ``data/tools.json``，而不是在这里
    另造一份过滤逻辑。
    """
    return list(registry.specs())
