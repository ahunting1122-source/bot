"""看板（kanban）：跨 run 的持久任务板（MATRIX 5.14 的 `kanban_*` 一组）。

## 为什么需要它

Hermes 有 **14 个 `kanban_*` 工具**，构成一套**多 agent 协作协议**：
建任务 → 认领 → 评论 → 交审 → 退回 / 完成并 handoff → 阻塞 / 解阻塞 → 挂附件。
Orbit 之前只有 `todo`（**单个任务自己的**清单）和一个前端面板，
**没有跨 run、跨 agent 的任务板**——`todo` 的粒度是「我这一步要做啥」，
看板的粒度是「谁在做什么、谁等谁」。

## 本轮只做 4 个（其余 10 个如实列在后面）

`kanban_create` / `kanban_list` / `kanban_show` / `kanban_complete`。

**为什么不一次做 14 个**：`request_review` / `request_changes` / `block` / `unblock` /
`link` / `heartbeat` / `attach*` 各自带一套状态机与并发语义，一次性铺开会留下
「注册了但没验过」的工具——而**接上了但有洞比没接上更危险**。
先做**能被完整验证**的 4 个，把存储与分发打通，剩下的按同一形状加。

## 存储为什么用文件而不是新表

`todo` 就是文件式的（`load_todos(data_dir, task_id)`），而看板要**跨任务**共享，
所以放 `<data_dir>/kanban/board.json`——**一张板**。
用文件而不是加 SQLite 表，是为了**不需要迁移**：迁移会牵动所有既有测试的建库路径，
而这个模块的价值在协议而不在存储引擎。**将来要加索引/并发再换表，接口不变。**
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BOARD_DIRNAME = "kanban"
BOARD_FILENAME = "board.json"

#: 状态机（刻意小）：`open` 可被认领 → `doing` → `done`。
#: `blocked` 留了位置但**本轮没有工具会产生它**（`kanban_block` 未实现）——
#: 留着是因为状态机与工具是两件事：先把状态位定下来，加工具时不用改数据形状。
STATUS_OPEN = "open"
STATUS_DOING = "doing"
STATUS_DONE = "done"
STATUS_BLOCKED = "blocked"
STATUSES = (STATUS_OPEN, STATUS_DOING, STATUS_DONE, STATUS_BLOCKED)

MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 20000
MAX_CARDS = 2000

_lock = threading.Lock()


class KanbanError(RuntimeError):
    """看板操作失败（找不到卡片 / 状态非法 / 超上限）。"""


@dataclass
class Card:
    """一张卡片。字段对齐 Hermes `kanban_show` 暴露的那些。"""

    id: str
    title: str
    body: str = ""
    status: str = STATUS_OPEN
    #: 谁在负责（agent 名 / 用户名）。空 = 没人认领。
    assignee: str = ""
    #: 父任务 id。空 = 顶层卡片。
    parent: str = ""
    #: 完成时交给下游的结构化交接物（Hermes 的 handoff）。
    handoff: str = ""
    #: 跨 run 的持久评论（`kanban_comment` 未实现，先留字段）。
    comments: list[dict[str, str]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def board_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / BOARD_DIRNAME / BOARD_FILENAME


def load_board(data_dir: str | Path) -> list[Card]:
    """读板。坏文件/不存在都返回空板——**看板坏了不该让工具直接崩**。"""
    path = board_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, dict):
        return []
    cards = raw.get("cards")
    if not isinstance(cards, list):
        return []
    out: list[Card] = []
    for item in cards:
        if not isinstance(item, dict):
            continue
        try:
            out.append(
                Card(
                    id=str(item["id"]),
                    title=str(item.get("title", "")),
                    body=str(item.get("body", "")),
                    status=str(item.get("status", STATUS_OPEN)),
                    assignee=str(item.get("assignee", "")),
                    parent=str(item.get("parent", "")),
                    handoff=str(item.get("handoff", "")),
                    comments=list(item.get("comments") or []),
                    created_at=str(item.get("created_at", "")),
                    updated_at=str(item.get("updated_at", "")),
                )
            )
        except (KeyError, TypeError):
            continue
    return out


def save_board(data_dir: str | Path, cards: list[Card]) -> None:
    """原子写：先写临时文件再 `os.replace`，避免写一半断电留下半张板。"""
    path = board_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": 1, "cards": [asdict(card) for card in cards]}
    with _lock:
        handle, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def _new_id(cards: list[Card]) -> str:
    """短、可读、板内唯一的 id（`k-0001` 递增）。"""
    used = {card.id for card in cards}
    for index in range(1, MAX_CARDS + 1):
        candidate = f"k-{index:04d}"
        if candidate not in used:
            return candidate
    raise KanbanError(f"看板已满（上限 {MAX_CARDS} 张卡片）")


def create(
    data_dir: str | Path,
    title: str,
    *,
    body: str = "",
    parent: str = "",
    assignee: str = "",
) -> Card:
    title = (title or "").strip()
    if not title:
        raise KanbanError("建卡需要 title（一句话说清这张卡要做什么）")
    if len(title) > MAX_TITLE_CHARS:
        raise KanbanError(f"title 不能超过 {MAX_TITLE_CHARS} 字符（这条 {len(title)}）")
    if len(body) > MAX_BODY_CHARS:
        raise KanbanError(f"body 不能超过 {MAX_BODY_CHARS} 字符（这条 {len(body)}）")
    cards = load_board(data_dir)
    if parent and parent not in {card.id for card in cards}:
        raise KanbanError(f"父卡片 {parent} 不存在——先建它，再挂子卡")
    card = Card(
        id=_new_id(cards),
        title=title,
        body=body,
        parent=parent,
        assignee=assignee,
        created_at=_now(),
        updated_at=_now(),
    )
    cards.append(card)
    save_board(data_dir, cards)
    return card


def list_cards(data_dir: str | Path, *, status: str = "", assignee: str = "") -> list[Card]:
    cards = load_board(data_dir)
    if status:
        cards = [card for card in cards if card.status == status]
    if assignee:
        cards = [card for card in cards if card.assignee == assignee]
    return cards


def show(data_dir: str | Path, card_id: str) -> Card:
    for card in load_board(data_dir):
        if card.id == card_id:
            return card
    raise KanbanError(f"找不到卡片 {card_id}")


def complete(data_dir: str | Path, card_id: str, *, handoff: str = "") -> Card:
    """标记完成并写 handoff。

    **已经 done 的卡再 complete 会报错**，不是幂等——重复完成通常意味着
    两个 agent 同时在推同一张卡，那件事应该被看见，而不是被静默吞掉。
    """
    cards = load_board(data_dir)
    for card in cards:
        if card.id != card_id:
            continue
        if card.status == STATUS_DONE:
            raise KanbanError(
                f"卡片 {card_id} 已经是 done——重复完成通常意味着两个 agent 在推同一张卡，"
                "请先 `kanban_show` 确认是谁做完的"
            )
        card.status = STATUS_DONE
        card.handoff = handoff
        card.updated_at = _now()
        save_board(data_dir, cards)
        return card
    raise KanbanError(f"找不到卡片 {card_id}")


def summarize(cards: list[Card]) -> list[dict[str, Any]]:
    """`kanban_list` 的载荷：**摘要**，不带 body（那是 `show` 的事）。"""
    return [
        {
            "id": card.id,
            "title": card.title,
            "status": card.status,
            "assignee": card.assignee,
            "parent": card.parent,
        }
        for card in cards
    ]


__all__ = [
    "BOARD_DIRNAME",
    "Card",
    "KanbanError",
    "STATUSES",
    "STATUS_BLOCKED",
    "STATUS_DOING",
    "STATUS_DONE",
    "STATUS_OPEN",
    "board_path",
    "complete",
    "create",
    "list_cards",
    "load_board",
    "save_board",
    "show",
    "summarize",
]
