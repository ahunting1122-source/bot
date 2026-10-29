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
#: 附件落点：`<data_dir>/kanban/attachments/<card_id>/<name>`。
ATTACH_DIRNAME = "attachments"
MAX_ATTACHMENT_BYTES = 8 * 1024 * 1024

#: 状态机（刻意小）：`open` 可被认领 → `doing` → `done`。
#: `blocked` 留了位置但**本轮没有工具会产生它**（`kanban_block` 未实现）——
#: 留着是因为状态机与工具是两件事：先把状态位定下来，加工具时不用改数据形状。
STATUS_OPEN = "open"
STATUS_DOING = "doing"
STATUS_DONE = "done"
STATUS_BLOCKED = "blocked"
#: 交审中（第 46 轮加）：`request_review` 进入，`request_changes` 退回，`complete` 通过。
#: **它与 `doing` 分开**，因为「还在写」与「写完了等人看」对 orchestrator 是两件事。
STATUS_REVIEW = "in_review"
STATUSES = (STATUS_OPEN, STATUS_DOING, STATUS_DONE, STATUS_BLOCKED, STATUS_REVIEW)

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
    #: 前驱卡片 id 列表。空 = 顶层卡片。
    #:
    #: **为什么是列表而不是单个 `parent`**（第 47 轮修）：`link()` 的语义是
    #: 「给两个已存在任务加**依赖边**」（Hermes `kanban_link`）——边可以有多条。
    #: 原来 `parent` 是单个字符串，`link()` 就是**覆盖**：`link(A,B)` 之后
    #: `link(A,C)` 会把 B 那条边**悄悄丢掉**（实测确认）。**那不是加边，是数据丢失。**
    parents: list[str] = field(default_factory=list)
    #: 完成时交给下游的结构化交接物（Hermes 的 handoff）。
    handoff: str = ""
    #: 跨 run 的持久评论（`kanban_comment`）。
    comments: list[dict[str, str]] = field(default_factory=list)
    #: 附件：`[{name, path, bytes}]`。`path` 是**相对 `<data_dir>` 的路径**——
    #: 存绝对路径会让整块板在换机器/换数据目录后全部失效。
    attachments: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""

    @property
    def first_parent(self) -> str:
        """**第一个前驱**（没有前驱时是空串）。

        ## 为什么叫 `first_parent` 而不叫 `parent`

        原来叫 `parent`，而它只返回 `parents[0]`——于是 `if card.parent == x`
        判「是不是 x 的子卡」时**只看第一个前驱、漏检其余**。

        **改 docstring 不管用**：名字叫 `parent` 就会有人当「父卡」用。
        **最便宜的护栏是把语义放进名字**（`first_parent`），
        而不是靠注释提醒。**判据：兼容旧调用方 ≠ 允许旧语义。**

        **依赖判定一律走 `parents` 全集**（`blockers()`）——否则又多一条会漂移的路径。
        """
        return self.parents[0] if self.parents else ""


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
                    # 旧板只有 `parent`（单个字符串）：读的时候升级成列表。
                    # **不迁移文件**——读兼容比迁移便宜，而且旧板不会因此坏掉。
                    parents=(
                        list(item.get("parents") or [])
                        or ([str(item["parent"])] if item.get("parent") else [])
                    ),
                    handoff=str(item.get("handoff", "")),
                    comments=list(item.get("comments") or []),
                    attachments=list(item.get("attachments") or []),
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
        parents=[parent] if parent else [],
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


def comment(data_dir: str | Path, card_id: str, text: str, *, author: str = "") -> Card:
    """给卡片追加一条**跨 run 的持久评论**。

    评论不是"再发一次消息"——它是**留在卡上的记录**：下一个接这张卡的人（可能是
    另一个 agent、也可能是三天后的自己）读 `kanban_show` 就能看到上下文。
    """
    body = (text or "").strip()
    if not body:
        raise KanbanError("评论不能为空——写清你要留什么给下一个人")
    cards = load_board(data_dir)
    for card in cards:
        if card.id != card_id:
            continue
        card.comments.append({"author": author, "text": body, "at": _now()})
        card.updated_at = _now()
        save_board(data_dir, cards)
        return card
    raise KanbanError(f"找不到卡片 {card_id}")


def block(data_dir: str | Path, card_id: str, *, reason: str = "") -> Card:
    """把卡片标记为阻塞。

    **已经 done 的卡不能阻塞**——完成了就是完成了，把它退回 blocked 会让下游
    看到一张「已完成又阻塞」的卡，比不标更迷惑。
    """
    cards = load_board(data_dir)
    for card in cards:
        if card.id != card_id:
            continue
        if card.status == STATUS_DONE:
            raise KanbanError(
                f"卡片 {card_id} 已经是 done——不能把已完成的卡改回 blocked"
                "（下游会看到一张「已完成又阻塞」的卡，比不标更迷惑）"
            )
        card.status = STATUS_BLOCKED
        if reason:
            card.comments.append({"author": "", "text": f"[blocked] {reason}", "at": _now()})
        card.updated_at = _now()
        save_board(data_dir, cards)
        return card
    raise KanbanError(f"找不到卡片 {card_id}")


def unblock(data_dir: str | Path, card_id: str) -> Card:
    """解除阻塞：**一律放回 `open`，但父卡没完成时记一条依赖提醒**。

    ## 为什么不拒绝（第一版错了）

    第一版在「父卡还没 done」时**报错拒绝**。那是个死胡同：用户除了先去做父卡
    没有别的路，而**父卡可能被别人卡着**——于是这条卡谁也推不动，也没人能表达
    「我知道它还等着，但我先把它放回池子里」。

    **Hermes 的解法是「降级」而不是「拒绝」**（`01-cli-surface.md:795`：
    「`unblock` 把 blocked/scheduled 任务放回 ready，**父任务未完成则回 todo**」；
    `02-tool-layer.md:385` 同）。**既不拒绝（无死胡同），也不 ready（无死循环）。**

    ## 为什么不加 `--force`

    加 `--force` 恰好会制造我原本担心的那个死循环：强制放回可认领状态 → 被重新认领
    → 又卡在同一个依赖上 → 再 block → 再 force……**降级比强制好。**

    ## 边界（如实写）

    Orbit 的 `open` 大致对应 Hermes 的 **`todo`**；Hermes 那个更细的
    **`ready`（可认领）** 这一档 Orbit **没有单独建模**。所以这里能做到的是
    「放回池子 + 留一条依赖提醒」，**做不到「放回 todo 但明确不可认领」**。
    那是状态机的一档缺口，不是本函数的 bug。
    """
    cards = load_board(data_dir)
    by_id = {card.id: card for card in cards}
    for card in cards:
        if card.id != card_id:
            continue
        if card.status != STATUS_BLOCKED:
            raise KanbanError(f"卡片 {card_id} 不在 blocked 状态（当前 {card.status}）")
        pending = blockers(cards, card)
        if pending:
            # **降级而不是拒绝**：放回池子，但把依赖留在卡上。
            detail = "、".join(
                f"{pid}({by_id[pid].status})" for pid in pending if pid in by_id
            )
            card.comments.append(
                {
                    "author": "",
                    "text": (
                        f"[dependency] 前驱未完成：{detail}——"
                        "本卡已放回池子，但先做完前驱才有意义"
                    ),
                    "at": _now(),
                }
            )
        card.status = STATUS_OPEN
        card.updated_at = _now()
        save_board(data_dir, cards)
        return card
    raise KanbanError(f"找不到卡片 {card_id}")


def heartbeat(data_dir: str | Path, card_id: str) -> Card:
    """给卡片打一次「我还活着」。

    用途是**让 orchestrator 分辨「在跑」与「跑挂了」**：只有 `updated_at` 会被
    刷新，状态不变。**已 done 的卡不打**——那会让它看起来还在跑。
    """
    cards = load_board(data_dir)
    for card in cards:
        if card.id != card_id:
            continue
        if card.status == STATUS_DONE:
            raise KanbanError(f"卡片 {card_id} 已经 done，不需要 heartbeat")
        card.updated_at = _now()
        save_board(data_dir, cards)
        return card
    raise KanbanError(f"找不到卡片 {card_id}")


def link(data_dir: str | Path, child_id: str, parent_id: str) -> Card:
    """给两张**已存在**的卡加一条父子依赖边。

    ## 必须做环检测

    `create(parent=...)` 只能在建卡时挂父卡，而且那时子卡还不存在，**天然不会有环**。
    `link` 是**在已有卡片之间加边**——`A→B` 之后再 `B→A` 就成环了。
    成环的后果不是报错，是**拓扑序永远算不完**：orchestrator 会认为「这两张互相等着」，
    于是两张都永远不该开始。**死锁比报错更难查。**

    检测方式：从 `parent_id` 沿现有父链上溯，**如果路上遇到 `child_id` 就是环**。
    """
    cards = load_board(data_dir)
    by_id = {card.id: card for card in cards}
    if child_id not in by_id:
        raise KanbanError(f"找不到子卡片 {child_id}")
    if parent_id not in by_id:
        raise KanbanError(f"找不到父卡片 {parent_id}")
    if child_id == parent_id:
        raise KanbanError("不能让一张卡成为自己的父卡")

    # 沿**所有**前驱链上溯：遇到 child 就是环。
    # 用显式栈而不是单链游标——`parents` 是列表，一条边可能分叉。
    stack: list[str] = [parent_id]
    seen: set[str] = set()
    while stack:
        cursor = stack.pop()
        if cursor == child_id:
            raise KanbanError(
                f"会形成环：{child_id} → {parent_id} → … → {child_id}。"
                "**成环不会报错，会让拓扑序永远算不完（两张互相等着、都永远不该开始）**"
            )
        if cursor in seen:  # 已有的环（坏数据）——别让它把我们转死
            continue
        seen.add(cursor)
        node = by_id.get(cursor)
        if node is not None:
            stack.extend(node.parents)

    card = by_id[child_id]
    # **追加，不覆盖**：`link` 的语义是「加一条边」。
    # 重复加同一条边是幂等的（不报错、也不产生重复项）——那不是错误，只是重放。
    if parent_id not in card.parents:
        card.parents.append(parent_id)
    card.updated_at = _now()
    save_board(data_dir, cards)
    return card


def request_review(data_dir: str | Path, card_id: str, *, note: str = "") -> Card:
    """交审：`doing`/`open` → `in_review`。

    **已 done / blocked 的卡不能交审**：done 是终点，blocked 要先把阻塞解掉——
    否则 orchestrator 会看到一张「又阻塞又在等人审」的卡。
    """
    cards = load_board(data_dir)
    for card in cards:
        if card.id != card_id:
            continue
        if card.status in (STATUS_DONE, STATUS_BLOCKED):
            raise KanbanError(
                f"卡片 {card_id} 当前是 {card.status}，不能交审"
                "（done 是终点；blocked 要先把阻塞解掉）"
            )
        card.status = STATUS_REVIEW
        if note:
            card.comments.append({"author": "", "text": f"[review] {note}", "at": _now()})
        card.updated_at = _now()
        save_board(data_dir, cards)
        return card
    raise KanbanError(f"找不到卡片 {card_id}")


def request_changes(data_dir: str | Path, card_id: str, *, changes: str) -> Card:
    """审阅结论：`in_review` → `open`，并记下**必改项**。

    **必改项不能为空**：退回却不说是改什么，等于把问题原样丢回去。
    **只有 `in_review` 的卡能被退回**——从 done 退回会抹掉已完成的记录。
    """
    body = (changes or "").strip()
    if not body:
        raise KanbanError("退回必须写清要改什么（空退回等于把问题原样丢回去）")
    cards = load_board(data_dir)
    for card in cards:
        if card.id != card_id:
            continue
        if card.status != STATUS_REVIEW:
            raise KanbanError(
                f"卡片 {card_id} 不在 in_review（当前 {card.status}）——只有交审中的卡能被退回"
            )
        card.status = STATUS_OPEN
        card.comments.append({"author": "", "text": f"[changes] {body}", "at": _now()})
        card.updated_at = _now()
        save_board(data_dir, cards)
        return card
    raise KanbanError(f"找不到卡片 {card_id}")


def attach(data_dir: str | Path, card_id: str, name: str, data: bytes) -> Card:
    """把一段字节挂到卡片上（`kanban_attach` 收 base64，这里收已解码的字节）。

    ## 落点与存储形状

    文件落在 `<data_dir>/kanban/attachments/<card_id>/<name>`，
    **卡上存相对 `<data_dir>` 的路径**——存绝对路径会让整块板在换机器/换数据目录后
    全部失效，而那种失效**不会报错，只会让附件"凭空消失"**。

    ## 名字要清洗

    `name` 来自模型，**不能直接当文件名**：`../` 会写到附件目录外，
    绝对路径会覆盖任意文件。所以只取 basename，并挡掉 `.` / `..` / 空名。
    """
    from pathlib import PurePath

    clean = PurePath(str(name or "")).name.strip()
    if clean in ("", ".", ".."):
        raise KanbanError(f"附件名不合法：{name!r}（只取文件名部分，且不能是 . / ..）")
    if len(data) > MAX_ATTACHMENT_BYTES:
        raise KanbanError(
            f"附件过大：{len(data)} 字节（上限 {MAX_ATTACHMENT_BYTES}）。"
            "大文件请用 `kanban_attach_url`（未实现）或放到工作目录再引用路径"
        )

    cards = load_board(data_dir)
    for card in cards:
        if card.id != card_id:
            continue
        target_dir = Path(data_dir) / BOARD_DIRNAME / ATTACH_DIRNAME / card_id
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / clean
        target.write_bytes(data)
        rel = target.relative_to(Path(data_dir)).as_posix()
        # 同名覆盖：**替换那条记录**而不是追加两条——否则列出来会有两个同路径条目
        card.attachments = [item for item in card.attachments if item.get("name") != clean]
        card.attachments.append({"name": clean, "path": rel, "bytes": len(data)})
        card.updated_at = _now()
        save_board(data_dir, cards)
        return card
    raise KanbanError(f"找不到卡片 {card_id}")


def attach_url(
    data_dir: str | Path, card_id: str, url: str, *, name: str = "", timeout: float = 60.0
) -> Card:
    """按 URL 挂附件：**服务端下载后落盘**，然后走与 `attach()` 同一条路径。

    ## 为什么必须先过云元数据闸

    「按 URL 下载」是**最经典的 SSRF 入口**：攻击者只要能让这个 URL 指向
    `169.254.169.254`，就能把云实例凭证下载成「附件」再读出来。
    所以这里第一件事是 `refuse_cloud_metadata(url)` —— 与其余出站点同一条规则。

    ## 为什么在下载过程中判大小

    先下完再判大小，等于让攻击者用 100MB 的响应把进程内存和磁盘打满。
    这里按块累加，**超限立刻停**。

    ## 为什么复用 `attach()`

    下载完之后的「写文件 + 改卡片 + 同名覆盖」逻辑 `attach()` 已经做过并验过了。
    **重写一份必然漂移。**
    """
    import httpx

    from app.egress import refuse_cloud_metadata

    refuse_cloud_metadata(url, where="附件下载")

    # 名字没给就从 URL 路径里取；取不到就用一个安全默认名。
    if not name:
        from pathlib import PurePath
        from urllib.parse import urlparse

        candidate = PurePath(urlparse(str(url)).path or "").name
        name = candidate or "attachment"

    chunks: list[bytes] = []
    total = 0
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        with client.stream("GET", str(url)) as response:
            response.raise_for_status()
            for block in response.iter_bytes(65536):
                total += len(block)
                if total > MAX_ATTACHMENT_BYTES:
                    raise KanbanError(
                        f"附件过大：已下 {total} 字节（上限 {MAX_ATTACHMENT_BYTES}）。"
                        "**下载过程中就停，不是先下完再拒绝**——否则一个大响应就能把内存打满。"
                    )
                chunks.append(block)

    return attach(data_dir, card_id, name, b"".join(chunks))


def attachments(data_dir: str | Path, card_id: str) -> list[dict[str, Any]]:
    """列附件，**并把相对路径还原成绝对路径**交回去。

    模型拿到的是相对路径就没法直接读——而这个工具的用途就是「告诉我附件在哪」，
    所以还原成绝对路径。**板里仍然只存相对路径**（换机器不失效），
    绝对路径是**查询时算出来的**。
    """
    card = show(data_dir, card_id)
    out: list[dict[str, Any]] = []
    for item in card.attachments:
        rel = str(item.get("path", ""))
        out.append(
            {
                "name": str(item.get("name", "")),
                "path": str(Path(data_dir) / rel) if rel else "",
                "bytes": int(item.get("bytes") or 0),
            }
        )
    return out


def blockers(cards: list[Card], card: Card) -> list[str]:
    """这张卡**还没做完的前驱**（父卡）。空 = 依赖已满足。

    抽成函数是因为它有两个调用方（`claim()` 的闸门、`summarize()` 的 `ready` 标记），
    而**两处各写一遍必然漂移**——漂移的后果是「列表说能接、认领说不能」。
    """
    by_id = {item.id: item for item in cards}
    # **全部前驱**，不是只看第一个——`link` 可以加多条边。
    return [
        pid
        for pid in card.parents
        if (by_id.get(pid) is not None and by_id[pid].status != STATUS_DONE)
    ]


def claim(data_dir: str | Path, card_id: str, *, assignee: str = "") -> Card:
    """认领一张卡：`open` → `doing`。**父卡没 done 就拒绝**。

    ## 为什么闸门必须在「认领」这一步

    上一版把 `unblock` 改成「父卡没完成也放回 `open`」——**关掉了死胡同**
    （用户不再无路可走），**但没关掉死循环**：`open` **是可认领的**
    （见本模块顶部的状态机注释），所以那张卡仍会被认领、又卡在同一个依赖上。

    而 `[dependency]` 那条评论**只是记录，不是闸门**——没有任何东西会因为它而不认领。

    **判据：闸门要放在危害真正发生的那一步。** 这里危害发生在「认领」，
    不在「状态值」——所以闸门放这里，而不是靠一个看起来相关的字段。
    （同族：第 34 轮 `_slot_client` 的「闸门位置错了」。）

    ## 为什么不加一个 `ready` 状态

    Hermes 用 `todo`（不可认领）/ `ready`（可认领）两档表达同一件事。
    加一档要动所有状态判断处；而**闸门放在认领这一步，效果相同、改动面小一个数量级**。
    """
    cards = load_board(data_dir)
    for card in cards:
        if card.id != card_id:
            continue
        if card.status != STATUS_OPEN:
            raise KanbanError(f"卡片 {card_id} 不是 open（当前 {card.status}），不能认领")
        pending = blockers(cards, card)
        if pending:
            raise KanbanError(
                f"卡片 {card_id} 的前驱还没完成（{', '.join(pending)}）——"
                "现在认领会立刻又卡在同一个依赖上（死循环），先让前驱完成"
            )
        card.status = STATUS_DOING
        if assignee:
            card.assignee = assignee
        card.updated_at = _now()
        save_board(data_dir, cards)
        return card
    raise KanbanError(f"找不到卡片 {card_id}")


def summarize(cards: list[Card]) -> list[dict[str, Any]]:
    """`kanban_list` 的载荷：**摘要**，不带 body（那是 `show` 的事）。

    **带 `ready` 与 `blocked_by`**：`kanban_list` 是 orchestrator「发现可路由的活」
    的那一步——**如果它只报 `status`，orchestrator 会把一张「open 但父卡没 done」
    的卡当成可接的**，然后认领时才被拒。**把判据提前到发现那一步，
    错误就不会发生**（而不是发生后再报错）。
    """
    out: list[dict[str, Any]] = []
    for card in cards:
        pending = blockers(cards, card)
        out.append(
            {
                "id": card.id,
                "title": card.title,
                "status": card.status,
                "assignee": card.assignee,
                "parent": card.first_parent,
                # 可认领 = 状态对 **且** 依赖已满足
                "ready": card.status == STATUS_OPEN and not pending,
                "blocked_by": pending,
                # **两种「不可开工」的原因必须分开**（第 47 轮）：
                # `waiting_on_parent` = 等前驱做完（再等等就好）；
                # `blocked` = 有人显式叫停（要人/agent 去解）。
                # 只报 `ready=False` 会把这两件事混成一件，而处理方式完全不同。
                "blocked_reason": (
                    "blocked"
                    if card.status == STATUS_BLOCKED
                    else ("waiting_on_parent" if pending else "")
                ),
            }
        )
    return out


__all__ = [
    "BOARD_DIRNAME",
    "Card",
    "KanbanError",
    "STATUSES",
    "STATUS_BLOCKED",
    "STATUS_DOING",
    "STATUS_DONE",
    "STATUS_OPEN",
    "STATUS_REVIEW",
    "ATTACH_DIRNAME",
    "MAX_ATTACHMENT_BYTES",
    "attach",
    "attach_url",
    "attachments",
    "blockers",
    "board_path",
    "claim",
    "block",
    "comment",
    "complete",
    "create",
    "heartbeat",
    "link",
    "list_cards",
    "load_board",
    "save_board",
    "show",
    "request_changes",
    "request_review",
    "summarize",
    "unblock",
]
