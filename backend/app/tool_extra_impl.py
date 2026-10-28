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

* :data:`EXTRA_TOOLS` —— 两个真工具：`tool_describe`（取工具完整参数表）与
  `cron_job`（定时任务的建 / 列 / 停 / 删）；
* :func:`execute` —— 唯一分发点（与 `ToolRegistry.local()` 同一形状：返回 dict，
  异常往上抛由注册表包成 `{"status": "error"}`）；
* :func:`register_extra_tools` —— 装配层的一行接线；
* :func:`listing_budget` —— MATRIX 5.11 的**实测数字**：完整清单 / 压描述 / 延迟
  Tier 2 三档各多少 token，以及延迟之后到底进没进 4,000。

## 为什么只做这两个（其余 68 个缺口为什么不做）

`orbit_coverage()` 之前是 23 / 94。缺的 71 个里，绝大多数是"本机做不到"：要联网
（web / xAI / Home Assistant / Discord / 飞书 / 元宝 / 图像视频模型）、要真实浏览器
（`browser_*` 的 7 个）、要桌面运行时（`desktop_*` 的 15 个）、要看板服务端（kanban 14 个）、
要平台凭证（collab 13 个）。**这类一律不注册**：注册了模型就会去调，调了必然失败，
而"调了才失败"比"没有这个工具"坏得多（模型会绕圈重试、把失败写进总结）。

真正做得成的只有 meta / cron 这两处——就是本文件里的两个工具。这个结论不是"没排期"，
是**逐个核过能不能在本机自证**之后的结论，每个不做的都在 `tool_catalog.HERMES_TOOLS`
里写了具体原因。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

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
    "CronJobArgs",
    "ExtraContext",
    "ListingBudget",
    "ToolDescribeArgs",
    "deferred_listing",
    "describe_tool",
    "execute",
    "listing_budget",
    "register_extra_tools",
    "run_cron_job",
]
