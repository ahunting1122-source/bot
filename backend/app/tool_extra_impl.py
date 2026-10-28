"""MATRIX 5.14 / 5.11 的落地文件：本机真做得成的工具 + 分级披露的预算实测。

## 这个文件为什么单独存在（而不是加进 `app/tools.py`）

`app/tools.py` 是**禁改区**（本轮的任务书把它划成只读：只许读 `LOCAL_TOOLS` /
`Tool` / 分发链签名）。而 Hermes 的 94 个工具里有几个是"纯本地、本机能真跑、但没有
任何人在 Orbit 里实现"的缺口——把实现写在这里，既不动那 2,900 行的大文件，也让
"这一轮到底补了什么"有一个可 diff 的落点。

代价必须说清：这些工具**不会自动出现在默认注册表里**。装配层要显式调一次
:func:`register_extra_tools`（一行）。没接之前，模型看不到它们——所以
`app/tool_catalog.py` 的 `report()` 会把"已实现 / 已注册"分开报，而不是把"写了代码"
算成"模型能用"。

## 里面有什么

* :data:`EXTRA_TOOLS` —— 上一轮的两个真工具：`tool_describe`（取工具完整参数表）与
  `cron_job`（定时任务的建 / 列 / 停 / 删）；
* :data:`GROWTH_TOOLS` —— **本轮（5.14 第二轮）新增的 5 个**：`tool_call` /
  `context_usage` / `compact` / `setup_mcp` / `text_to_speech`（每个都有真实实现与真跑
  用例，见下方那段"为什么是这 5 个"）；
* :func:`execute` / :func:`execute_growth` —— 两个分发点（与 `ToolRegistry.local()`
  同一形状：返回 dict，异常往上抛由注册表包成 `{"status": "error"}`）；
* :func:`register_extra_tools` / :func:`register_growth_tools` —— 装配层各一行；
* :func:`listing_budget` —— MATRIX 5.11 的**实测数字**：完整清单 / 压描述 / 延迟
  Tier 2 三档各多少 token，以及延迟之后到底进没进 4,000。

## 两批工具为什么要分开（`EXTRA_TOOLS` vs `GROWTH_TOOLS`）

`EXTRA_TOOLS` 是上一轮定下的**稳定契约**：既有测试逐字钉住那两个名字，装配层也按名
核对。把新工具塞进去，会让"上一轮接了几个"这个数字当场失真、也让回滚只能整批回滚。
所以新工具单独一批、单独一个注册函数 —— 代价是装配层多一行，换来的是两批进度可分开
回答。

## 为什么是这 5 个（其余 63 个缺口为什么不做）

`orbit_coverage()` 现在是 **31 / 94**（上一轮 26）。剩下 63 个里，绝大多数是"本机做不到"：
要联网（web / xAI / Home Assistant / Discord / 飞书 / 元宝 / 图像视频模型）、要真实浏览器
（`browser_*` 的 7 个）、要桌面运行时（`desktop_*` 的 15 个）、要看板服务端（kanban 14 个）、
要平台凭证（collab 13 个）。**这类一律不注册**：注册了模型就会去调，调了必然失败，
而"调了才失败"比"没有这个工具"坏得多（模型会绕圈重试、把失败写进总结）。

本轮**重新审了一遍**上一轮"35+ 不可达"的判定，逐个核过能不能在本机自证。结论是：
没有判过头的，但**有几条判得太保守**——`meta` 的三条（`tool_call` / `context_usage` /
`compact`）与 `setup_mcp`、`text_to_speech` 都能真做，只是上一轮没找到落点：

* `context_usage` 上一轮的理由是"外部执行器拿不到 messages"。**对，但它不需要 messages**：
  `task_id` 就在执行器参数里，而 `ToolRegistry` 自带 `store`，所以"已落库的对话正文"
  读得到；窗口从 `app/capabilities.py` 读。数字是真的，口径写在返回值里。
* `compact` 上一轮的理由是"压缩在运行时被拦，工具层只能是个 stub"。**对了一半**：
  改不了运行时手里那份列表，但**能**请求重算摘要，而摘要是运行时下一轮真的会读的压缩
  产物（`app/context.py::build_volatile_tier(summary=…)`）。
* `tool_call` 上一轮的理由是"多一层桥会绕过确认门禁"。**这个担心是对的**，所以本轮
  的实现里**显式拒绝** `confirmation=True` 的工具——闸不是绕过，是照旧。

真正做不到的仍然一条不注册，两个容易被"顺手做掉"的特别记在这里：

* `delegate_task` / `agent_swarm`：`app/delegate.py` 的 `DelegateRunner` 在**全仓库没有
  任何地方注入 `runner_factory`**（`app/cli/commands/delegate.py` 自己就说"未注入子任务
  runner"）。注册了必然调一次错一次。
* `kanban_*`（14 个）：`app/` 下 grep 不到任何看板后端（`dashboard.py` 是页面路由，不是
  看板）。Hermes 的 kanban 是**多 worker 认领/交接的服务端**，单机文件版不是同一个东西。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from app import toolsets
from app.tools import Tool

# ── 参数模型 ────────────────────────────────────────────────────────────────


class ToolDescribeArgs(BaseModel):
    """`tool_describe` 的参数。"""

    name: str = Field(description="工具名（在 tool_search 的结果或系统提示的工具清单里拿）")


class CronJobArgs(BaseModel):
    """`cron_job` 的参数。``action`` 决定其余字段哪些有意义。"""

    action: Literal["list", "create", "pause", "resume", "remove"] = Field(
        default="list",
        description="list=列任务、create=新建、pause/resume=启停、remove=删除",
    )
    job_id: str = Field(default="", description="pause / resume / remove 要给的任务 id")
    schedule: str = Field(
        default="",
        description="create 必填：`30m` / `2h` / `1d` 这类间隔，或 5 段 cron 表达式",
    )
    prompt: str = Field(default="", description="create：到点要让 agent 做什么")
    name: str = Field(default="", description="create：给任务起个人能认的名字")
    deliver: str = Field(
        default="",
        description="create：结果投递到哪（留空=只落盘，`platform:<名字>` / `file:<路径>`）",
    )
    repeat: int = Field(default=0, description="create：跑几次后自动停（0 = 不限）")


# ── 工具声明 ────────────────────────────────────────────────────────────────
#
# 描述文字照仓库的写法：**先说是什么，再说什么时候用，最后说和谁分工**。
# 模型就是靠这三句决定调不调它的，缺了第三句就会和邻居工具互相抢活。

EXTRA_TOOLS: list[Tool] = [
    Tool(
        "tool_describe",
        "取一个工具的**完整参数表**（名字 / 说明 / 每个参数的类型与是否必填）。"
        "首屏工具清单里 Tier 2 的工具只给名字和一句描述，参数表要用它取——"
        "不确定参数怎么填时先 describe 一次，别猜。分工：找工具用 tool_search，"
        "取某个工具的正文用 tool_describe。",
        ToolDescribeArgs,
    ),
    Tool(
        "cron_job",
        "管理定时任务：list 看现有任务、create 新建（给 schedule + prompt）、"
        "pause/resume 启停、remove 删除。**它只登记任务，不会自己把任务跑起来**——"
        "真正到点执行的是 `orbit cron` 的守护进程；所以建完要告诉用户"
        "「需要 `orbit cron run` 在跑」。一次性等待用 run_command，别建任务。",
        CronJobArgs,
    ),
]


# ── 实现 ────────────────────────────────────────────────────────────────────


def describe_tool(name: str, specs: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """从**完整** spec 列表里取出一个工具的定义（含参数表）。

    为什么要吃一整份 specs 而不是自己去注册表里翻：`tool_describe` 的调用点拿不到
    注册表（它是个工具，不是注册表的方法），而装配层可以把"取当前完整清单"这件事以
    闭包形式注入（见 :func:`register_extra_tools`）。这样本函数仍是纯函数、可单测。

    找不到时**抛 ValueError 并给候选**：静默返回空 dict 会让模型以为"这工具没有参数"，
    然后拿一份空入参去调用。
    """
    key = str(name or "").strip()
    if not key:
        raise ValueError("给一个工具名：tool_describe 需要 name 参数")
    for spec in specs:
        body = spec.get("function") if isinstance(spec, Mapping) else None
        if not isinstance(body, Mapping) or str(body.get("name") or "") != key:
            continue
        parameters = body.get("parameters") or {"type": "object", "properties": {}}
        properties = parameters.get("properties") if isinstance(parameters, Mapping) else {}
        return {
            "name": key,
            "description": str(body.get("description") or ""),
            "parameters": parameters,
            "required": list(parameters.get("required") or [])
            if isinstance(parameters, Mapping)
            else [],
            "argument_names": sorted(properties) if isinstance(properties, Mapping) else [],
            "toolset": toolsets.toolset_of(key),
            "domain": toolsets.domain_of_tool(key),
            "tier": toolsets.tier_of(key),
            # 只有 Tier 2 会在首屏被压成"名字 + 一句描述"（见 toolsets.defer_specs）。
            "deferred_in_listing": toolsets.tier_of(key) >= 2,
            "notice": "参数表以本次返回为准；调用时把参数放进 arguments 的 JSON 对象里。",
        }
    candidates = toolsets.suggest(key)
    hint = f"（相近的有：{'、'.join(candidates)}）" if candidates else ""
    raise ValueError(
        f"没有名为 {key} 的工具{hint}。用 tool_search 按关键词找，或核对系统提示里的工具清单。"
    )


def run_cron_job(args: Mapping[str, Any], *, data_dir: str | Path) -> dict[str, Any]:
    """`cron_job` 的实现：对 `app/cron.py` 的任务表做增删查（**不触发执行**）。

    为什么复用 `app/cron.py` 而不是自己写一份 json：定时任务的调度解析（`parse_schedule`
    / `next_run`）、投递目标校验（`validate_deliver`）、id 生成、原子落盘都在那里，
    而 CLI 的 `orbit cron` 也读同一份 `cron/jobs.json`。自己写一份就会出现"工具建的任务
    调度器看不见"——那种假象比没有这个工具更糟。

    每个分支都回**当前完整任务表**：模型看不到任何隐式状态，不回读就只能靠记忆。
    """
    from app import cron

    store = cron.CronStore(data_dir)
    action = str(args.get("action") or "list").strip().lower()
    notice = ""

    def rows() -> list[dict[str, Any]]:
        return [
            {
                "job_id": job.id,
                "name": job.name or job.display_name,
                "schedule": job.schedule,
                "enabled": job.enabled,
                "next_run_at": job.next_run_at,
                "deliver": job.deliver,
                "prompt": job.prompt[:120],
            }
            for job in store.load_jobs()
        ]

    if action == "create":
        schedule = str(args.get("schedule") or "").strip()
        if not schedule:
            raise ValueError(
                "create 需要 schedule：`30m` / `2h` / `1d` 这类间隔，或 5 段 cron 表达式"
            )
        job = cron.create_job(
            store,
            schedule=schedule,
            prompt=str(args.get("prompt") or ""),
            name=str(args.get("name") or ""),
            deliver=str(args.get("deliver") or cron.DELIVER_NONE),
            repeat=int(args.get("repeat") or 0),
        )
        notice = (
            f"已建任务 {job.id}（下次 {job.next_run_at}）。**执行需要 `orbit cron run` "
            "在跑**，否则它只会躺在 jobs.json 里。"
        )
    elif action in {"pause", "resume"}:
        job_id = str(args.get("job_id") or "").strip()
        jobs = store.load_jobs()
        target = next((job for job in jobs if job.id == job_id), None)
        if target is None:
            raise ValueError(f"没有这个任务：{job_id!r}。先 action=list 看现有任务 id。")
        target.enabled = action == "resume"
        store.save_jobs(jobs)
        notice = f"任务 {job_id} 已{'恢复' if target.enabled else '暂停'}。"
    elif action == "remove":
        job_id = str(args.get("job_id") or "").strip()
        if not cron.remove_job(store, job_id):
            raise ValueError(f"没有这个任务：{job_id!r}。先 action=list 看现有任务 id。")
        notice = f"任务 {job_id} 已删除。"
    elif action != "list":
        raise ValueError(
            f"未知 action：{action}（可选 list / create / pause / resume / remove）"
        )

    items = rows()
    return {
        "action": action,
        "count": len(items),
        "jobs": items,
        "notice": (notice + " " if notice else "") + "任务表以这次返回为准。",
    }


# ── 分发 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ExtraContext:
    """新工具执行时能拿到的东西。**全部由装配层注入**，本模块不自己去找全局状态。

    为什么不在这里 import `Settings` / `ToolRegistry`：那会让本模块依赖整个装配链，
    而它要能被 `tool_catalog`（零成本导入的目标）和测试直接引用。两个字段刚好够：
    ``data_dir`` 给 `cron_job` 找任务表，``specs`` 给 `tool_describe` 取完整清单。
    """

    data_dir: Path | None = None
    specs: Callable[[], Sequence[Mapping[str, Any]]] | None = None


def execute(name: str, args: Mapping[str, Any], context: ExtraContext) -> dict[str, Any]:
    """唯一分发点。返回 dict；**异常不吞**（由 `ToolRegistry.execute` 包成 error 载荷）。

    与 `app/tools.py::ToolRegistry.local()` 同一约定：实现函数只负责"做成 / 抛错"，
    载荷形状统一由上层决定。
    """
    if name == "tool_describe":
        if context.specs is None:
            raise RuntimeError(
                "tool_describe 需要装配层注入 `specs=`（取当前完整工具清单的回调）；"
                "用 register_extra_tools(registry) 接线时会自动注入。"
            )
        return describe_tool(str(args.get("name") or ""), context.specs())
    if name == "cron_job":
        if context.data_dir is None:
            raise RuntimeError(
                "cron_job 需要装配层注入 `data_dir=`（定时任务表在 <data>/cron/jobs.json）。"
            )
        return run_cron_job(args, data_dir=context.data_dir)
    raise ValueError(f"未知的扩展工具：{name}（可用：{'、'.join(t.name for t in EXTRA_TOOLS)}）")


def register_extra_tools(
    registry: Any,
    *,
    data_dir: str | Path | None = None,
    specs: Callable[[], Sequence[Mapping[str, Any]]] | None = None,
) -> list[str]:
    """把 :data:`EXTRA_TOOLS` 注册进一个 `ToolRegistry`，返回**真的注册成功**的名字。

    为什么返回名字而不是 None：注册表的遮蔽门禁会**静默跳过**跨 source 的同名注册
    （见 `ToolRegistry.register` 的注释）。不把"到底注册上了没有"返回给调用方，
    "接了一行但工具没出现"就会变成查不出来的问题。

    ``specs`` 留空 = 用 ``registry.specs(budget=None)``（完整清单，不做预算裁剪）——
    `tool_describe` 要的正是完整参数表，拿被裁剪过的清单等于把参数表也压成一句。
    """
    target = Path(data_dir) if data_dir is not None else getattr(
        getattr(registry, "settings", None), "data_dir", None
    )
    provider = specs or (lambda: registry.specs(budget=None))
    context = ExtraContext(data_dir=target, specs=provider)

    async def executor(
        name: str, args: Mapping[str, Any], task_id: str, operation_id: str
    ) -> dict[str, Any]:
        """外部执行器必须**自带 `status`**：`ToolRegistry.execute` 会直接读它
        （`result["status"]`），本地工具那条路才由注册表包 `{"status": "ok", ...}`。
        包错这一层，工具第一次被调用就会在注册表里 KeyError。"""
        return {"status": "ok", "data": execute(name, args, context)}

    registry.register(EXTRA_TOOLS, executor, source="builtin-extra")
    return [tool.name for tool in EXTRA_TOOLS if tool.name in getattr(registry, "tools", {})]


# ── MATRIX 5.14 第二轮：能本机真跑的 5 个工具 ────────────────────────────────
#
# ## 为什么是这 5 个（以及为什么不是别的）
#
# 94 个 Hermes 工具里，能**在本机自证**的只有这些。判定口径是三条：
# ① 有真实实现（不是把声明翻译一遍）；② `schema_problems()` 能过；
# ③ 至少一条**真跑**用例。三条缺一条就不注册 —— 注册了模型就会去调，调了必然失败，
# 而"调了才失败"比"没有这个工具"坏得多。
#
# 逐个说清（其余 57 个缺口的理由在 `app/tool_catalog.py` 里）：
#
# * `tool_call` —— 按名字调工具。**纯本地**：走的就是 `ToolRegistry.execute`。
#   唯一的硬要求是**不许绕过确认门禁**（`confirmation=True` 的工具必须让用户在对话里
#   明确同意，不能从桥里偷偷调）。
# * `context_usage` —— 上下文占用。数字来自**已落库的对话正文** +
#   `app/capabilities.py` 的 `context_window`，判定用 `app/session_plane.py::maybe_compact`
#   （纯函数）。**不假装知道系统提示与本轮未落库的增量**，返回值里写清口径。
# * `compact` —— 上下文压缩。判定用 `app/compaction.py::plan`（纯函数，给出该丢哪几组、
#   压完剩多少），真要动手时调装配层注入的 `SummaryService.rebuild(conversation_id)`
#   —— 那条路是**运行时真的会读**的摘要（`app/context.py::build_volatile_tier(summary=…)`）。
# * `setup_mcp` —— 增删 MCP 服务。落点是 `<data>/mcp.json`（`MCPManager.save_servers`
#   的校验、keyring 引用、去重都在那边），**不联网**。
# * `text_to_speech` —— 语音合成。本机真能跑的是 `app/voice_local.py` 的 SAPI 与
#   `voice.json` 的命令通道；19 家云端一律如实说不可用。
#
# ## 为什么不放进 `EXTRA_TOOLS`
#
# `EXTRA_TOOLS`（`tool_describe` / `cron_job`）是上一轮的**稳定契约**：既有测试逐字钉住
# 那两个名字，装配层也按名核对。新工具放 :data:`GROWTH_TOOLS` + 独立注册函数，于是
# "上一轮接了几个"与"这一轮新增几个"分开可数，回滚也能分开回滚。

class ToolCallArgs(BaseModel):
    """`tool_call` 的参数。"""

    name: str = Field(description="要调用的工具名（必须已经在当前工具清单里）")
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="传给那个工具的参数对象（不确定就先 tool_describe）"
    )


class ContextUsageArgs(BaseModel):
    """`context_usage` 的参数。"""

    window: int = Field(
        default=0,
        description="上下文窗口（token）。0 = 自动读模型能力表；读不到就如实说"
        "「窗口未知」而不是猜一个",
    )


class CompactArgs(BaseModel):
    """`compact` 的参数。"""

    apply: bool = Field(
        default=False,
        description="False=只判定（给数字与计划）；True=同时请求重算摘要"
        "（需要装配层注入 SummaryService，否则如实报错）",
    )
    window: int = Field(default=0, description="上下文窗口（token）。0 = 自动")


class SetupMcpArgs(BaseModel):
    """`setup_mcp` 的参数。``action`` 决定其余字段哪些有意义。"""

    action: Literal["list", "add", "remove", "enable", "disable"] = Field(
        default="list", description="list=看现有服务、add=新增、remove=删、enable/disable=启停"
    )
    name: str = Field(default="", description="服务名（add / remove / enable / disable 必填）")
    command: str = Field(default="", description="add：本地进程的可执行文件（与 url 二选一）")
    args: list[str] = Field(default_factory=list, description="add：传给 command 的参数")
    url: str = Field(default="", description="add：远程服务的 URL（与 command 二选一）")
    headers: dict[str, str] = Field(default_factory=dict, description="add(url)：请求头")
    env: dict[str, str] = Field(default_factory=dict, description="add(command)：环境变量")
    auto_approve: bool = Field(default=False, description="add：该服务的工具是否免确认")


class TextToSpeechArgs(BaseModel):
    """`text_to_speech` 的参数。"""

    text: str = Field(description="要合成的文本")
    voice: str = Field(
        default="", description="音色名（SAPI 音色 / 命令通道自己解释）；留空用第一个可用音色"
    )
    path: str = Field(
        default="", description="落盘路径（相对数据目录）；留空写到 <data>/media/tts-<hash>.wav"
    )


GROWTH_TOOLS: list[Tool] = [
    Tool(
        "tool_call",
        "按**名字**调用另一个工具（参数放 arguments）。用途只有一类：工具清单被延迟披露、"
        "你从 tool_search / tool_describe 拿到参数表后想直接调用。"
        "**需要用户确认的工具（memory_save / memory_delete 这类）它一律拒绝**——"
        "确认必须发生在对话里，不能从桥里绕过。分工：找工具用 tool_search，"
        "看参数用 tool_describe，真要执行才用它。",
        ToolCallArgs,
    ),
    Tool(
        "context_usage",
        "查**当前对话的上下文占用**：已落库正文的 token 数、窗口大小、占比，"
        "以及「要不要压缩」的判定。数字口径写清在返回值里（不含系统提示与本轮未落库的增量）；"
        "窗口读不到时如实说「未知」而不是猜。要压缩用 compact。",
        ContextUsageArgs,
    ),
    Tool(
        "compact",
        "对当前对话做**上下文压缩**：先给判定（该丢哪几组、压完剩多少 token），"
        "apply=true 时再请求重算摘要（摘要是运行时真的会读的压缩产物）。"
        "**它不会当场改写你正在看的消息列表**——那归运行时；它改变的是下一轮装配上下文"
        "所用的摘要。只想看数字用 context_usage。",
        CompactArgs,
    ),
    Tool(
        "setup_mcp",
        "管理 MCP 服务：list 看现有配置、add 新增（command 或 url 二选一）、"
        "remove 删除、enable/disable 启停。**它只改 `<data>/mcp.json`，不会当场连接**——"
        "连接发生在服务启动 / `orbit mcp reload` 时；所以建完要告诉用户「重启或 reload 生效」。"
        "分工：想看某个服务连没连上用 `orbit mcp status`。",
        SetupMcpArgs,
    ),
    Tool(
        "text_to_speech",
        "把文本合成成 **wav 文件**并返回路径。本机可用的是 Windows SAPI（离线）与 "
        "`voice.json` 里配的命令通道；19 家云端 provider 本机不可用（会如实报缺什么）。"
        "要发给别人/贴到平台时把返回的 path 传下去即可。",
        TextToSpeechArgs,
    ),
]


@dataclass(frozen=True)
class GrowthContext:
    """GROWTH_TOOLS 执行时能拿到的东西。**全部由装配层注入**。

    为什么把 `registry` 也放进来：`tool_call` 要"按名字调另一个工具"，而唯一正确的
    调用路径就是 `ToolRegistry.execute`（它负责参数校验、幂等短路、确认门禁与错误包装）。
    自己另写一条分发等于把那些闸全部绕开。
    """

    registry: Any = None
    settings: Any = None
    store: Any = None
    #: 装配层注入的摘要服务（`app/summaries.py::SummaryService`）。`compact(apply=True)`
    #: 靠它真的触发压缩；没注入就如实报"没接"。
    summary_service: Any = None

    @property
    def data_dir(self) -> Path | None:
        root = getattr(self.settings, "data_dir", None)
        return Path(root) if root is not None else None


async def _conversation_of(store: Any, task_id: str) -> tuple[Any, Any]:
    """task_id → `(Task, Conversation)`。拿不到就抛**能行动**的错。"""
    if store is None:
        raise RuntimeError(
            "这个工具需要装配层注入 `store=`（ToolRegistry 自带一个，"
            "用 register_growth_tools(registry) 接线时会自动带上）。"
        )
    from app.storage import Conversation, Task

    task = await store.get(Task, str(task_id or ""))
    if task is None:
        raise ValueError(f"找不到任务 {task_id!r}：这个工具只能查当前会话，不能查别人的。")
    conversation = await store.get(Conversation, task.conversation_id)
    return task, conversation


async def _messages_of(store: Any, conversation_id: str) -> list[Any]:
    """读一场对话的**已落库**消息（按时间 + id 排序，与摘要器同口径）。"""
    from sqlalchemy import select

    from app.storage import Message

    rows = await store.rows(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at, Message.id)
    )
    return list(rows)


def _window_for(settings: Any, override: int = 0) -> tuple[int, str]:
    """上下文窗口 `(token, 来源)`。``0`` = 不知道（**不猜默认值**）。

    来源优先级：调用方显式给的 → `app/capabilities.py` 的能力表（override / cache /
    snapshot 三层，`context_window` 字段）→ 未知。猜一个 8192 会让"占比 3%"这种
    假结论流到用户面前，而真值是"我不知道窗口多大"。
    """
    if int(override or 0) > 0:
        return int(override), "调用方指定"
    model = str(getattr(settings, "model", "") or "")
    if not model:
        return 0, "未配置模型"
    try:
        from app import capabilities

        entry = capabilities.resolve(
            model,
            str(getattr(settings, "model_provider", "") or ""),
            data_dir=getattr(settings, "data_dir", None),
        )
    except Exception:  # noqa: BLE001 —— 能力表读不到不该让工具崩
        return 0, "能力表不可读"
    window = getattr(entry, "context_window", None)
    if isinstance(window, int) and window > 0:
        return window, f"能力表（{model}，来源 {getattr(entry, 'source', '?')}）"
    return 0, f"能力表里没有 {model} 的 context_window"


def _usage_payload(
    *, messages: list[Any], conversation: Any, window: int, window_source: str
) -> dict[str, Any]:
    """把"占用了多少"算成一份可核对的账（context_usage 与 compact 共用）。"""
    from app import compaction, session_plane

    used = compaction.tokens_for(messages)
    verdict = session_plane.maybe_compact(used, window)
    summary = str(getattr(conversation, "summary", "") or "")
    return {
        "messages": len(messages),
        "used_tokens": used,
        "window": window,
        "window_source": window_source,
        "used_ratio": round(verdict.used_ratio, 4),
        "target_tokens": verdict.target_tokens,
        "headroom_tokens": verdict.headroom_tokens,
        "compaction_needed": verdict.needed,
        "compaction_reason": verdict.reason,
        "summary_status": str(getattr(conversation, "summary_status", "") or ""),
        "summary_chars": len(summary),
        "summary_covers_messages": int(getattr(conversation, "summary_message_count", 0) or 0),
        "caveat": (
            "口径：只统计**已落库**的对话正文（用户/助手消息），不含系统提示、"
            "工具结果原文与本轮尚未落库的增量；token 数是按字符估算的（4 字符/token）。"
            "窗口读不到时为 0 且不判定压缩。"
        ),
    }


async def run_tool_call(args: Mapping[str, Any], context: GrowthContext, *, task_id: str) -> dict[str, Any]:
    """`tool_call`：按名字调另一个工具（**走注册表的真实分发链**）。

    三道闸，缺一不可：

    1. **未知工具**：报错并给 tool_search 指路（静默返回空 dict 会让模型以为"调了但没结果"）。
    2. **确认门禁**：`confirmation=True` 的工具（`memory_save` / `memory_delete`）**拒绝**。
       确认的意义就是"用户在对话里点了同意"，从桥里调等于把这道闸拆掉。
    3. **不许递归**：拒绝调用 `tool_call` 自己（无限递归只会把调用栈烧穿）。
    """
    registry = context.registry
    if registry is None:
        raise RuntimeError(
            "tool_call 需要装配层注入 registry（用 register_growth_tools(registry) 接线时会自动带上）。"
        )
    name = str(args.get("name") or "").strip()
    if not name:
        raise ValueError("给一个工具名：tool_call 需要 name（不确定就先 tool_search）")
    if name == "tool_call":
        raise ValueError("tool_call 不能调用自己（无限递归）。要调别的工具请直接给名字。")
    tool = getattr(registry, "tools", {}).get(name)
    if tool is None:
        raise ValueError(
            f"没有名为 {name} 的工具。先用 tool_search 按关键词找，或核对系统提示里的工具清单。"
        )
    if bool(getattr(tool, "confirmation", False)):
        raise ValueError(
            f"{name} 需要用户确认，不能从 tool_call 里调用：确认必须发生在对话里"
            "（用户在界面上点同意），从桥里调等于绕开这道闸。请直接在对话里说明你要做什么。"
        )
    arguments = args.get("arguments") or {}
    if not isinstance(arguments, Mapping):
        raise ValueError("arguments 必须是 JSON 对象")
    operation_id = f"tool_call:{name}:{uuid4().hex[:12]}"
    result = await registry.execute(task_id, operation_id, name, dict(arguments))
    return {
        "called": name,
        "status": str(result.get("status", "")),
        "result": result,
        "notice": (
            "结果原样来自注册表分发链（含它的校验、幂等与错误包装）；"
            "status=error 时不要重试同一个调用，先看 error。"
        ),
    }


async def run_context_usage(args: Mapping[str, Any], context: GrowthContext, *, task_id: str) -> dict[str, Any]:
    """`context_usage`：当前对话的占用（真数字 + 真判定 + 口径说明）。"""
    _task, conversation = await _conversation_of(context.store, task_id)
    if conversation is None:
        raise ValueError("这场对话不存在（可能已被删除）。")
    messages = await _messages_of(context.store, conversation.id)
    window, source = _window_for(context.settings, int(args.get("window") or 0))
    return _usage_payload(
        messages=messages, conversation=conversation, window=window, window_source=source
    )


async def run_compact(args: Mapping[str, Any], context: GrowthContext, *, task_id: str) -> dict[str, Any]:
    """`compact`：判定 + （可选）请求重算摘要。

    ## 为什么"压缩"落在摘要上，而不是当场改消息列表
    Orbit 里真正会缩短上下文的是运行时（`app/graph/nodes.py` 的 `compact_messages`，
    禁改区），它读的是**装配层**手里那份消息列表；工具拿不到那份列表，能改的只有
    `<data>/app.sqlite` 里的持久事实。而运行时下一轮装配上下文时会用
    `Conversation.summary`（`app/context.py::build_volatile_tier(summary=…)`）——
    所以"请求重算摘要"是工具层**唯一**能真的影响下一轮上下文的压缩动作。
    不假装能改当前这轮：返回值里 `applied` 字段说的就是这件事。
    """
    from app import compaction

    _task, conversation = await _conversation_of(context.store, task_id)
    if conversation is None:
        raise ValueError("这场对话不存在（可能已被删除）。")
    messages = await _messages_of(context.store, conversation.id)
    window, source = _window_for(context.settings, int(args.get("window") or 0))
    usage = _usage_payload(
        messages=messages, conversation=conversation, window=window, window_source=source
    )
    carried = len(str(getattr(conversation, "summary", "") or "")) // 4
    plan = compaction.plan(messages, window=window, carried_tokens=carried)

    payload: dict[str, Any] = {
        **usage,
        "plan": plan.as_dict(),
        "applied": False,
        "summary_requested": False,
    }
    if not bool(args.get("apply")):
        payload["notice"] = (
            "只做了判定（apply=false）。要真的压缩请传 apply=true，"
            "它会请求重算摘要（下一轮装配上下文时生效）。"
        )
        return payload

    service = context.summary_service
    if service is None or not callable(getattr(service, "rebuild", None)):
        raise RuntimeError(
            "compact(apply=true) 需要装配层注入 `summary_service=`（"
            "`app/summaries.py::SummaryService(settings, store)`，装配层已有这个对象）；"
            "没有它就只能判定、不能压缩。"
        )
    await service.rebuild(conversation.id)
    payload["applied"] = True
    payload["summary_requested"] = True
    payload["notice"] = (
        "已请求重算摘要：摘要生成后，**下一轮**装配上下文会用它（这一轮正在看的消息不变）。"
        "摘要由模型生成，需要模型可用；`orbit sessions` 里能看到 summary_status。"
    )
    return payload


def run_setup_mcp(args: Mapping[str, Any], context: GrowthContext) -> dict[str, Any]:
    """`setup_mcp`：对 `<data>/mcp.json` 做增删改查（**不连接、不联网**）。

    为什么复用 `MCPManager.save_servers` 而不是自己写 json：那里的字段清洗（`name`
    必填、`command` 与 `url` 二选一、keyring 引用的 `keyring:` 前缀）、去重与原子写
    都在，而且 `orbit mcp` 与运行时的 `MCPManager` 读的是同一份文件。自己写一份
    就会出现"工具加的服务，`orbit mcp list` 看不见"。
    """
    settings = context.settings
    if settings is None:
        raise RuntimeError("setup_mcp 需要装配层注入 settings（register_growth_tools 会自动带上）。")
    from app.mcp import MCPManager

    manager = MCPManager(settings)
    action = str(args.get("action") or "list").strip().lower()
    servers = manager.servers()
    name = str(args.get("name") or "").strip()
    notice = ""

    def rows() -> list[dict[str, Any]]:
        return [
            {
                "name": str(entry.get("name", "")),
                "enabled": bool(entry.get("enabled", True)),
                "target": str(entry.get("url") or entry.get("command") or ""),
                "args": list(entry.get("args") or []),
                "auto_approve": bool(entry.get("auto_approve", False)),
            }
            for entry in servers
        ]

    if action == "add":
        if not name:
            raise ValueError("add 需要 name（服务名，之后 `orbit mcp` 与工具名都用它）")
        if any(str(entry.get("name", "")) == name for entry in servers):
            raise ValueError(f"已经有一个叫 {name} 的 MCP 服务；要改配置请先 remove 再 add。")
        entry: dict[str, Any] = {
            "name": name,
            "command": str(args.get("command") or ""),
            "args": [str(item) for item in (args.get("args") or [])],
            "url": str(args.get("url") or ""),
            "headers": {str(k): str(v) for k, v in (args.get("headers") or {}).items()},
            "env": {str(k): str(v) for k, v in (args.get("env") or {}).items()},
            "auto_approve": bool(args.get("auto_approve")),
        }
        manager.save_servers([*servers, entry])
        servers = manager.servers()
        notice = (
            f"已写入 <data>/mcp.json。**连接要等重启或 `orbit mcp reload`**；"
            f"连接后它的工具会以 mcp__{name}__<tool> 出现在清单里。"
        )
    elif action in {"remove", "enable", "disable"}:
        if not name:
            raise ValueError(f"{action} 需要 name")
        target = next((entry for entry in servers if str(entry.get("name", "")) == name), None)
        if target is None:
            raise ValueError(f"没有叫 {name} 的 MCP 服务。先 action=list 看现有服务。")
        if action == "remove":
            manager.save_servers([entry for entry in servers if entry is not target])
            notice = f"已删除 {name}（下次 reload 之后它的工具会从清单里消失）。"
        else:
            target["enabled"] = action == "enable"
            manager.save_servers(servers)
            notice = f"{name} 已{'启用' if target['enabled'] else '禁用'}（reload 后生效）。"
        servers = manager.servers()
    elif action != "list":
        raise ValueError(f"未知 action：{action}（可选 list / add / remove / enable / disable）")

    return {
        "action": action,
        "count": len(servers),
        "servers": rows(),
        "path": str(getattr(manager, "path", "")),
        "notice": (notice + " " if notice else "") + "配置以这次返回为准（不会当场连接）。",
    }


def run_text_to_speech(args: Mapping[str, Any], context: GrowthContext) -> dict[str, Any]:
    """`text_to_speech`：合成 wav 并落盘，返回**真实路径**。

    为什么先试命令通道再试 SAPI：命令通道是用户显式配的（`<data>/voice.json` 的
    `tts_command`），它优先；没配才退回系统自带的 SAPI。两家都不可用时**把各自的
    原因原样抛出去**（缺什么、怎么办），而不是只说"不可用"。
    """
    from app import voice, voice_local

    text = str(args.get("text") or "").strip()
    if not text:
        raise ValueError("text 是空的：没什么可合成的。")
    data_dir = context.data_dir
    config = voice.VoiceConfig.load(data_dir)
    providers: list[Any] = [voice.CommandTts(config, data_dir=data_dir), voice_local.LOCAL_SAPI_TTS]
    reasons: list[str] = []
    chosen: Any = None
    for provider in providers:
        if provider.available():
            chosen = provider
            break
        reasons.append(provider.describe())
    if chosen is None:
        raise RuntimeError(
            "本机没有可用的 TTS：① 命令通道未配置（`<data>/voice.json` 的 tts_command）；"
            "② Windows SAPI 不可用。逐条原因：" + " ｜ ".join(reasons)
        )
    voice_name = str(args.get("voice") or "").strip()
    audio = chosen.synthesize(text, voice_name or None)
    if not audio.startswith(b"RIFF"):
        raise RuntimeError(f"{chosen.name} 返回的不是 wav（前 4 字节 {audio[:4]!r}）。")
    relative = str(args.get("path") or "").strip() or (
        f"media/tts-{uuid4().hex[:10]}.wav"
    )
    target = Path(relative)
    if not target.is_absolute():
        if data_dir is None:
            raise RuntimeError("text_to_speech 需要装配层注入 settings（含 data_dir）。")
        target = data_dir / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(audio)
    return {
        "path": str(target),
        "bytes": len(audio),
        "provider": str(chosen.name),
        "voice": voice_name or "（默认）",
        "format": "wav (RIFF)",
        "notice": (
            "文件已落盘。要发到平台请把 path 交给平台投递（本机 19 家云端 TTS 不可用，"
            "这里用的是本地通道）。"
        ),
    }


async def execute_growth(
    name: str,
    args: Mapping[str, Any],
    context: GrowthContext,
    *,
    task_id: str = "",
) -> dict[str, Any]:
    """GROWTH_TOOLS 的唯一分发点（异步：`tool_call` 与 `compact` 都要 await）。"""
    if name == "tool_call":
        return await run_tool_call(args, context, task_id=task_id)
    if name == "context_usage":
        return await run_context_usage(args, context, task_id=task_id)
    if name == "compact":
        return await run_compact(args, context, task_id=task_id)
    if name == "setup_mcp":
        return run_setup_mcp(args, context)
    if name == "text_to_speech":
        return run_text_to_speech(args, context)
    raise ValueError(
        f"未知的扩展工具：{name}（可用：{'、'.join(tool.name for tool in GROWTH_TOOLS)}）"
    )


def register_growth_tools(
    registry: Any,
    *,
    settings: Any = None,
    store: Any = None,
    summary_service: Any = None,
    data_dir: str | Path | None = None,
) -> list[str]:
    """把 :data:`GROWTH_TOOLS` 注册进 `ToolRegistry`，返回**真的注册成功**的名字。

    ``settings`` / ``store`` 留空时从注册表自己身上取（`ToolRegistry` 两者都有），
    所以装配层的调用是**一行**：

        tool_extra_impl.register_growth_tools(registry, summary_service=summary_service)

    ``summary_service`` 不传也能注册：`context_usage` / `setup_mcp` / `text_to_speech` /
    `tool_call` 都不需要它，只有 `compact(apply=true)` 需要 —— 那时它会抛一条
    "该注入什么"的错，而不是静默什么都不做。
    """
    resolved_settings = settings if settings is not None else getattr(registry, "settings", None)
    resolved_store = store if store is not None else getattr(registry, "store", None)
    if data_dir is not None and resolved_settings is not None:
        # 显式给的 data_dir 优先（测试与多实例场景）：用一个浅包装覆盖掉 settings.data_dir。
        resolved_settings = _SettingsWithDataDir(resolved_settings, Path(data_dir))
    context = GrowthContext(
        registry=registry,
        settings=resolved_settings,
        store=resolved_store,
        summary_service=summary_service,
    )

    async def executor(
        name: str, args: Mapping[str, Any], task_id: str, operation_id: str
    ) -> dict[str, Any]:
        """外部执行器必须**自带 `status`**（`ToolRegistry.execute` 直接读它）。"""
        return {"status": "ok", "data": await execute_growth(name, args, context, task_id=task_id)}

    registry.register(GROWTH_TOOLS, executor, source="builtin-growth")
    return [tool.name for tool in GROWTH_TOOLS if tool.name in getattr(registry, "tools", {})]


@dataclass(frozen=True)
class _SettingsWithDataDir:
    """把 `data_dir` 覆盖到 settings 上的薄包装（其余字段原样透传）。

    为什么不 `dataclasses.replace`：`Settings` 是 pydantic 模型，字段很多且带校验；
    只为了改一个路径去重建它，等于把"配置校验"再跑一遍。这里只需要 `data_dir`。
    """

    _inner: Any
    data_dir: Path

    def __getattr__(self, item: str) -> Any:
        return getattr(self._inner, item)


# ── 5.11 的实测数字 ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ListingBudget:
    """一份工具清单的三档 token 数 + 延迟之后进没进预算。

    三档对应三种披露策略，缺任何一档这个结论都没法验：

    * ``full_tokens`` —— 不裁剪（``budget=None``，旧行为）；
    * ``trimmed_tokens`` —— 现行为：只压描述，**参数 schema 一个字节不动**；
    * ``deferred_tokens`` —— 本轮的出路：指定 tier 压成"名字 + 一句描述 + 空参数表"。

    ``within_budget`` 是**如实位**：延迟之后仍超就是 False，不许四舍五入成"达标"。
    """

    budget: int
    full_tokens: int
    trimmed_tokens: int
    deferred_tokens: int
    deferred: tuple[str, ...]
    tiers: tuple[int, ...] = (2,)

    @property
    def within_budget(self) -> bool:
        return self.deferred_tokens <= self.budget

    @property
    def saved_by_deferring(self) -> int:
        return max(0, self.trimmed_tokens - self.deferred_tokens)

    def as_dict(self) -> dict[str, Any]:
        return {
            "budget": self.budget,
            "tiers": list(self.tiers),
            "full_tokens": self.full_tokens,
            "trimmed_tokens": self.trimmed_tokens,
            "deferred_tokens": self.deferred_tokens,
            "deferred_count": len(self.deferred),
            "deferred": list(self.deferred),
            "saved_by_deferring": self.saved_by_deferring,
            "within_budget": self.within_budget,
        }

    def describe(self) -> str:
        """给人看的实测结论。**三档数字必须一起出现**：只报"延迟后达标"而不给前两档，
        读者无法判断省下来的是描述还是参数契约。"""
        verdict = (
            f"进了 {self.budget:,}（余 {self.budget - self.deferred_tokens:,}）"
            if self.within_budget
            else f"仍然超 {self.budget:,}（多 {self.deferred_tokens - self.budget:,}）"
        )
        scope = "+".join(str(item) for item in self.tiers)
        return "\n".join(
            [
                f"工具清单预算：{self.budget:,} token（描述这一维的上限，口径同 Hermes）",
                f"  1) 不裁剪：{self.full_tokens:,} token",
                f"  2) 只压描述（现行为，参数 schema 不动）：{self.trimmed_tokens:,} token",
                f"  3) 延迟 Tier {scope}（名字 + 一句描述 + 空参数表）："
                f"{self.deferred_tokens:,} token",
                f"  延迟 {len(self.deferred)} 个工具（{'、'.join(self.deferred) or '无'}），"
                f"比只压描述再省 {self.saved_by_deferring:,} token",
                f"  结论：延迟 Tier {scope} 之后{verdict}",
            ]
        )


def listing_budget(
    registry: Any, *, budget: int | None = None, tiers: Sequence[int] = (2,)
) -> ListingBudget:
    """量三档 token 数（MATRIX 5.11 的实测判据）。

    ``registry`` 只要有 ``specs(budget=...)`` 就行（鸭子类型）：测试可以塞一个只有
    这一条方法的替身，不必建整台机器。
    """
    limit = toolsets.LISTING_BUDGET_TOKENS if budget is None else int(budget)
    full = list(registry.specs(budget=None))
    trimmed = list(registry.specs(budget=limit))
    deferred_specs, deferred = toolsets.defer_specs(full, tiers=tiers)
    return ListingBudget(
        budget=limit,
        full_tokens=toolsets.estimate_tokens(full),
        trimmed_tokens=toolsets.estimate_tokens(trimmed),
        deferred_tokens=toolsets.estimate_tokens(deferred_specs),
        deferred=deferred,
        tiers=tuple(int(item) for item in tiers),
    )


def deferred_listing(registry: Any, *, tiers: Sequence[int] = (2,)) -> list[dict[str, Any]]:
    """延迟指定 tier 之后**真正要发出去的**那份清单（`specs(budget=)` 的替代出路）。

    为什么是独立函数而不是 `specs(budget=..., defer=True)`：`app/tools.py` 是禁改区，
    签名不能动。这里给出等价能力，装配层要换的时候只改一个调用点。
    """
    specs, _names = toolsets.defer_specs(list(registry.specs(budget=None)), tiers=tiers)
    return specs


__all__ = [
    "EXTRA_TOOLS",
    "GROWTH_TOOLS",
    "CompactArgs",
    "ContextUsageArgs",
    "CronJobArgs",
    "ExtraContext",
    "GrowthContext",
    "ListingBudget",
    "SetupMcpArgs",
    "TextToSpeechArgs",
    "ToolCallArgs",
    "ToolDescribeArgs",
    "deferred_listing",
    "describe_tool",
    "execute",
    "execute_growth",
    "listing_budget",
    "register_extra_tools",
    "register_growth_tools",
    "run_compact",
    "run_context_usage",
    "run_cron_job",
    "run_setup_mcp",
    "run_text_to_speech",
    "run_tool_call",
]
