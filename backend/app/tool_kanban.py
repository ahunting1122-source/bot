"""`kanban_*` 工具（MATRIX 5.14）：跨 run 的持久任务板。

## 已做哪些、未做哪些：**看 `orbit_coverage()`，不要看这里**

**这段原来手写了一份「已做 / 未做」清单，而它已经过期了**——写着「未做 10 个」，
实际只剩 `kanban_attach_url` 一个（它要联网下载，本机做不到）。

**一份过期的「如实清单」比没有清单更糟，因为读者会信它。**
而这正是本项目打了二十多轮的「清单断言会过期」——**它出现在一个专门用来
「如实列出没做什么」的 docstring 里，格外讽刺。**

**唯一事实源是 `app/tool_catalog.py::orbit_coverage()`**：它把 `KANBAN_TOOLS`
与 `HERMES_TOOLS` 逐条对账，`covered` / `missing` 都是现算的。
`tests/test_kanban_tools.py` 也按「生产装配后实际注册了几个」断言，不按名单。

**为什么不一次做 14 个**：后 10 个各自带一套状态机与并发语义
（交审 → 退回 → 再交审的循环、阻塞的依赖判定、heartbeat 的超时窗口、附件的
base64 与磁盘落点）。一次性铺开会留下「注册了但没验过」的工具——
**接上了但有洞比没接上更危险**。先做**能被完整验证**的 4 个，把存储与分发打通。

## 与 `todo` 的分工

`todo` 是**单个任务自己的**清单（「我这一步要做啥」）；
看板是**跨 run、跨 agent** 的任务板（「谁在做什么、谁等谁」）。
两者不是替代关系，所以看板没有复用 `todo` 的存储。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from pydantic import Field

from app.tools import Args, Tool

log = logging.getLogger("orbit.tool_kanban")

#: 装配层注入的 `data_dir`。None = 没接线，此时**不注册工具**。
_DATA_DIR: Path | None = None


class CreateArgs(Args):
    title: str = Field(min_length=1, max_length=200, description="一句话说清这张卡要做什么")
    body: str = Field(default="", description="详细说明（可空）")
    parent: str = Field(default="", description="父卡片 id（可空；用于拆子任务）")
    assignee: str = Field(default="", description="负责人（可空 = 没人认领）")


class ListArgs(Args):
    status: str = Field(
        default="", description="按状态过滤：open / doing / done / blocked（空 = 全部）"
    )
    assignee: str = Field(default="", description="按负责人过滤（空 = 全部）")


class ShowArgs(Args):
    id: str = Field(min_length=1, description="卡片 id")


class CompleteArgs(Args):
    id: str = Field(min_length=1, description="卡片 id")
    handoff: str = Field(
        default="", description="交给下游的结构化交接物（做了什么 / 产出在哪 / 还差什么）"
    )


class CommentArgs(Args):
    id: str = Field(min_length=1, description="卡片 id")
    text: str = Field(min_length=1, description="要留的话（写给下一个接这张卡的人）")
    author: str = Field(default="", description="署名（可空）")


class BlockArgs(Args):
    id: str = Field(min_length=1, description="卡片 id")
    reason: str = Field(default="", description="为什么卡住（会作为一条评论留在卡上）")


class CardIdArgs(Args):
    id: str = Field(min_length=1, description="卡片 id")


class LinkArgs(Args):
    child_id: str = Field(min_length=1, description="子卡片 id")
    parent_id: str = Field(min_length=1, description="父卡片 id（必须先完成它）")


class ReviewArgs(Args):
    id: str = Field(min_length=1, description="卡片 id")
    note: str = Field(default="", description="交审说明（可空）")


class ChangesArgs(Args):
    id: str = Field(min_length=1, description="卡片 id")
    changes: str = Field(min_length=1, description="必改项（不能为空）")


class AttachArgs(Args):
    id: str = Field(min_length=1, description="卡片 id")
    name: str = Field(min_length=1, description="附件文件名（只取文件名部分，不能是 . / ..）")
    content_base64: str = Field(min_length=1, description="文件内容的 base64 编码")


KANBAN_TOOLS: list[Tool] = [
    Tool(
        "kanban_create",
        "在看板上建一张卡（跨 run 持久）。用于把一件事派给别人、或给自己留一条待办。",
        CreateArgs,
    ),
    Tool(
        "kanban_list",
        "列看板上的卡片摘要（可按状态/负责人过滤）。用来发现有哪些活可以接。",
        ListArgs,
    ),
    Tool(
        "kanban_show",
        "读一张卡的完整状态（含正文、父卡、handoff、评论）。",
        ShowArgs,
    ),
    Tool(
        "kanban_complete",
        "把一张卡标记完成并写下 handoff（做了什么 / 产出在哪 / 还差什么）。"
        "重复完成会报错——那通常意味着两个 agent 在推同一张卡。",
        CompleteArgs,
    ),
    Tool(
        "kanban_comment",
        "给一张卡留一条持久评论（跨 run 可见）。下一个接这张卡的人读 show 就能看到上下文。",
        CommentArgs,
    ),
    Tool(
        "kanban_block",
        "把卡片标记为阻塞并说明原因。已完成（done）的卡不能阻塞。",
        BlockArgs,
    ),
    Tool(
        "kanban_unblock",
        "解除阻塞。**父卡全 done 才回到 open**——否则会立刻又被卡住。",
        CardIdArgs,
    ),
    Tool(
        "kanban_heartbeat",
        "给卡片打一次「我还活着」，让 orchestrator 能分辨「在跑」与「跑挂了」。",
        CardIdArgs,
    ),
    Tool(
        "kanban_link",
        "给两张已存在的卡加父子依赖边（**会做环检测**：成环会让拓扑序永远算不完）。",
        LinkArgs,
    ),
    Tool(
        "kanban_request_review",
        "交审：把卡标成 in_review 等人看。done 是终点、blocked 要先解，都不能交审。",
        ReviewArgs,
    ),
    Tool(
        "kanban_request_changes",
        "审阅结论：把 in_review 的卡退回 open，并写清**必改项**（不能为空）。",
        ChangesArgs,
    ),
    Tool(
        "kanban_attach",
        "把一段内容（base64）挂到卡片上。落盘在 <data_dir>/kanban/attachments/，"
        "卡上只存相对路径（换机器不失效）。",
        AttachArgs,
    ),
    Tool(
        "kanban_attachments",
        "列一张卡的附件（**交回绝对路径**，可以直接拿去读）。",
        CardIdArgs,
    ),
]


def configure(data_dir: str | Path) -> None:
    global _DATA_DIR
    _DATA_DIR = Path(data_dir)


def reset() -> None:
    global _DATA_DIR
    _DATA_DIR = None


def _require_data_dir() -> Path:
    if _DATA_DIR is None:
        raise RuntimeError("看板没接线：需要装配层调 `tool_kanban.configure(data_dir)`")
    return _DATA_DIR


async def execute(
    name: str, args: dict[str, Any], task_id: str, operation_id: str
) -> dict[str, Any]:
    """唯一分发点。**异常不吞**——由 `ToolRegistry.execute` 包成 error 载荷。"""
    from app import kanban

    data_dir = _require_data_dir()

    if name == "kanban_create":
        card = kanban.create(
            data_dir,
            str(args["title"]),
            body=str(args.get("body") or ""),
            parent=str(args.get("parent") or ""),
            assignee=str(args.get("assignee") or ""),
        )
        return {"ok": True, "card": _full(card)}

    if name == "kanban_list":
        cards = kanban.list_cards(
            data_dir,
            status=str(args.get("status") or ""),
            assignee=str(args.get("assignee") or ""),
        )
        return {"ok": True, "count": len(cards), "cards": kanban.summarize(cards)}

    if name == "kanban_show":
        return {"ok": True, "card": _full(kanban.show(data_dir, str(args["id"])))}

    if name == "kanban_complete":
        card = kanban.complete(data_dir, str(args["id"]), handoff=str(args.get("handoff") or ""))
        return {"ok": True, "card": _full(card)}

    if name == "kanban_comment":
        card = kanban.comment(
            data_dir,
            str(args["id"]),
            str(args["text"]),
            author=str(args.get("author") or ""),
        )
        return {"ok": True, "card": _full(card)}

    if name == "kanban_block":
        card = kanban.block(data_dir, str(args["id"]), reason=str(args.get("reason") or ""))
        return {"ok": True, "card": _full(card)}

    if name == "kanban_unblock":
        return {"ok": True, "card": _full(kanban.unblock(data_dir, str(args["id"])))}

    if name == "kanban_heartbeat":
        return {"ok": True, "card": _full(kanban.heartbeat(data_dir, str(args["id"])))}

    if name == "kanban_link":
        card = kanban.link(data_dir, str(args["child_id"]), str(args["parent_id"]))
        return {"ok": True, "card": _full(card)}

    if name == "kanban_request_review":
        card = kanban.request_review(data_dir, str(args["id"]), note=str(args.get("note") or ""))
        return {"ok": True, "card": _full(card)}

    if name == "kanban_request_changes":
        card = kanban.request_changes(data_dir, str(args["id"]), changes=str(args["changes"]))
        return {"ok": True, "card": _full(card)}

    if name == "kanban_attach":
        import base64

        try:
            data = base64.b64decode(str(args["content_base64"]), validate=True)
        except Exception as exc:  # noqa: BLE001 —— base64 坏了要说清是编码问题
            raise ValueError(f"content_base64 不是合法 base64：{exc}") from exc
        card = kanban.attach(data_dir, str(args["id"]), str(args["name"]), data)
        return {"ok": True, "card": _full(card)}

    if name == "kanban_attachments":
        items = kanban.attachments(data_dir, str(args["id"]))
        return {"ok": True, "count": len(items), "attachments": items}

    raise ValueError(f"未知的看板工具：{name}")


def _full(card: Any) -> dict[str, Any]:
    from dataclasses import asdict

    payload = asdict(card)
    # **parent 是派生属性**（第 47 轮改成多父边之后），sdict() 不会带上它。
    # 但工具载荷里必须留着：模型和既有调用方按 parent 读，突然消失会让它们
    # 静默拿到 None——**字段消失比字段改名更难发现**。
    payload["parent"] = card.first_parent
    return payload


def register_kanban_tools(registry: Any, *, data_dir: str | Path | None = None) -> list[str]:
    """注册看板工具，返回**真的注册成功**的名字。

    **`data_dir` 没注入就一个都不注册**——注册一个必然抛错的工具比不注册更糟。
    """
    if data_dir is not None:
        configure(data_dir)
    if _DATA_DIR is None:
        log.warning("看板没接线，`kanban_*` **不注册**（注册一个调不动的工具比不注册更糟）")
        return []
    try:
        registry.register(KANBAN_TOOLS, execute, source="kanban")
    except Exception as exc:  # noqa: BLE001 —— 注册失败要说出来，但不拦启动
        log.warning("看板工具注册失败：%s", exc)
        return []
    return [tool.name for tool in KANBAN_TOOLS if tool.name in registry.tools]


__all__ = [
    "KANBAN_TOOLS",
    "configure",
    "execute",
    "register_kanban_tools",
    "reset",
]
