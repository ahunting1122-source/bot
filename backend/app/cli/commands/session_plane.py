"""`orbit sessions import|prune` 与 Orbit 自有入口 `orbit plane …` 的真实现。

## 接线方式：只接声明表里**还没被接走**的路径

``app/cli/commands/_table.py`` 的约定是"表不动，实现方声明路径"。而 ``sessions`` 的
19 个子命令里，``list`` / ``browse`` / ``stats`` / ``rename`` / ``export`` / ``delete``
**已经被 ``app/cli/commands/sessions.py`` 接管**（那是 session_search 的四形态召回面）。
所以本模块只接剩下的两条与本层相关、且真的实现得了的：

- ``sessions import``（``--from {claude,codex}`` + 位置参数 ``path``）
- ``sessions prune``（时间 / 标题 / 消息数过滤 + ``--dry-run`` + ``--yes``）

``show`` / ``lineage`` / ``resume`` / ``tail`` 这四条**不在声明表里**（Hermes 的 19 条
子命令里没有它们），所以它们落在 ``orbit plane …`` 下——那是 Orbit 自有名字，
不占 Hermes 的任何命令位，也就不会污染对标矩阵。

## 为什么另开 `orbit plane`

三个理由，缺一个都足以让它不该塞进 `sessions`：

1. **`plane` 是"看"的入口，`sessions` 是"管"的入口**。``plane timeline`` 把三个来源
   合并成一条时间线，``plane deliveries`` 看投递账本，``plane reap`` 清孤儿登记——
   它们都不是"管理会话历史"，而是"看这个平面的状态"。
2. **Hermes 没有对应命令**。硬塞进 ``sessions`` 会让矩阵里 ``sessions`` 的子命令数
   从 19 变成 23，那条"19 子命令"的证据当场失真。
3. **`sessions` 已经有六个真实现**，再往里加只会让一个 600 行的模块继续膨胀，
   而这一层的失败模式（谱系循环、账本脏行、孤儿锁）与召回完全无关。

## 三条刻意的语义分歧（照抄会变成假对齐）

1. ``sessions prune`` 只实现**能落地的过滤条件**：``--older-than`` / ``--newer-than`` /
   ``--before`` / ``--after`` / ``--title`` / ``--min-messages`` / ``--max-messages``。
   ``--model`` / ``--provider`` / ``--min-tokens`` / ``--min-cost`` / ``--branch`` /
   ``--cwd`` / ``--chat-id`` / ``--end-reason`` 等 Hermes 字段在 Orbit 的
   ``conversations`` 表里**根本不存在**，所以给这些选项时**明确报未实现**并列出它们，
   而不是默默忽略（默默忽略 = 用户以为"按模型清理了"，实际删了一批别的会话）。
2. ``sessions import`` 不实现"交互式挑选器"（Hermes 不给 path 时会在
   ``~/.claude/projects`` / ``~/.codex/sessions`` 里让你选）。Orbit 要求显式路径：
   猜一个目录去读别人的私有会话数据，比报一句用法错更冒犯。
3. ``plane reap`` **默认就是 dry-run**。孤儿判据依赖"读进程创建时间"这种尽力而为的
   探测，在不可靠判据上默认执行不可逆动作是不可接受的；要真清必须显式 ``--apply``。
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context, truncate
from app.cli.spec import REGISTRY, Command, Option
from app.delivery_ledger import STATUSES, DeliveryLedger
from app.orphan_reaper import OrphanReaper
from app.session_plane import (
    KIND_IMPORTED,
    KIND_PRUNED,
    KIND_RESUMED,
    SessionPlane,
    SessionPlaneError,
    to_epoch,
)

#: ``sessions prune`` 里"表里有、Orbit 库没有对应列"的选项 → 一句人话的理由。
#: 收到这些选项时**报错**，不静默忽略（见模块 docstring 第 1 条分歧）。
_UNSUPPORTED_PRUNE = {
    "source": "conversations 表没有来源列（单用户本地部署）",
    "end_reason": "conversations 表没有结束原因列",
    "cwd": "conversations 表没有工作目录列",
    "model": "conversations 表没有模型列",
    "provider": "conversations 表没有计费渠道列",
    "user": "conversations 表没有多用户（user_id 恒为 local）",
    "chat_id": "conversations 表没有频道列",
    "chat_type": "conversations 表没有频道类型列",
    "branch": "conversations 表没有 git 分支列",
    "min_tokens": "没有逐会话 token 记账",
    "max_tokens": "没有逐会话 token 记账",
    "min_cost": "没有逐会话成本记账",
    "max_cost": "没有逐会话成本记账",
    "min_tool_calls": "没有逐会话工具调用计数",
    "max_tool_calls": "没有逐会话工具调用计数",
    "include_archived": "Orbit 没有归档态（会话要么在要么删了）",
    "include_pinned": "Orbit 没有置顶态（pin 未实现）",
    "never_active": "需要 sessions.source 与 message_count 口径，Orbit 缺来源列",
}

#: 时间单位的秒数。``5h`` / ``2d`` / ``1w`` 照 Hermes 的写法。
_UNITS = {"s": 1.0, "m": 60.0, "h": 3600.0, "d": 86_400.0, "w": 604_800.0}

#: 裸 ``prune`` 不带任何过滤条件时的默认窗口（Hermes 的 90 天）。
DEFAULT_PRUNE_DAYS = 90


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _plane(context: Context) -> SessionPlane:
    return SessionPlane(context.data_dir)


def _ledger(context: Context) -> DeliveryLedger:
    return DeliveryLedger(context.data_dir)


def _session_id(value: Any) -> str:
    return str(value or "").strip()


def _short(session_id: str, width: int = 8) -> str:
    """表格里只显示 ID 前 8 位：完整 UUID 会把其他列挤到屏幕外。"""
    return truncate(session_id, width) if len(session_id) > width else session_id


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _flat(text: str) -> str:
    """把多行内容压成一行：表格/单行输出里换行会把排版拆散。"""
    return " ".join((text or "").split())


def _store_or_none(context: Context) -> Any:
    """有库就返回 Store，没有就返回 ``None``。

    为什么允许"没有库"：``plane timeline`` 的价值之一就是"这台机器上什么都没建好时
    也能看平面日志"。硬要求数据库会让最简单的排障场景直接报错。
    """
    return context.store if context.db_path.exists() else None


def _cutoff(value: Any, *, now: float | None = None) -> float:
    """把 ``5h`` / ``2d`` / ``1w`` / 裸数字（天） / ISO 时间折成 epoch 秒。

    解析失败必须报错而不是退回默认值：``--older-than 3x`` 若被当成"90 天"，
    用户会以为只删了三天内的东西，实际删掉的是一批老会话。
    """
    text = str(value or "").strip()
    if not text:
        raise CliError("时间参数不能为空（支持 5h / 2d / 1w、裸数字=天、或 ISO 时间）")
    moment = time.time() if now is None else float(now)
    unit = text[-1].lower()
    if unit in _UNITS:
        number = text[:-1]
        try:
            return moment - float(number) * _UNITS[unit]
        except ValueError:
            pass
    try:
        return moment - float(text) * 86_400.0
    except ValueError:
        pass
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError as exc:
        raise CliError(
            f"无法解析时间：{value!r}（支持 5h / 2d / 1w、裸数字=天、或 ISO 时间）"
        ) from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.timestamp()


# ── sessions import ─────────────────────────────────────────────────────────


def _blocks_to_text(value: Any) -> str:
    """Claude Code 的 ``message.content`` 可能是块数组，折成纯文本。

    只取 ``text`` 块：``tool_use`` / ``tool_result`` 块里没有人类可读正文，
    拼进来只会让导入后的会话出现一堆 JSON 噪声。
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for block in value:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(part for part in parts if part)
    return ""


def _parse_jsonl(text: str) -> tuple[str, list[dict[str, str]]]:
    """解析会话 JSONL，返回 ``(标题, [{role, content, created_at}])``。

    认三种形状（都在本机真实存在）：

    1. **Orbit 自己的导出**（``sessions export --format jsonl``）：首行是
       ``{"session_id", "title"}`` 头，后续每行一条消息；
    2. **Claude Code**：``{"type": "user"|"assistant", "message": {...}, "timestamp"}``；
    3. **通用**：任何带 ``role`` + ``content`` 的行。

    认不出任何消息就报错——导出一场空会话不是"导入成功"。
    """
    title = ""
    rows: list[dict[str, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            item = json.loads(stripped)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        if "role" not in item and "message" not in item and "type" not in item:
            # 头行（Orbit 导出的第一行）
            if not title:
                title = str(item.get("title") or "")
            continue
        # Claude Code 把正文包在 ``message`` 里；Orbit 导出与通用形状是平铺的。
        nested = item.get("message")
        payload: dict[str, Any] = nested if isinstance(nested, dict) else item
        role = str(payload.get("role") or item.get("type") or "").strip()
        if role in {"system", "summary"}:
            continue
        content = _blocks_to_text(payload.get("content"))
        if not role or not content:
            continue
        created_at = str(item.get("timestamp") or payload.get("created_at") or "")
        rows.append({"role": role, "content": content, "created_at": created_at})
    return title, rows


def run_import(context: Context, args) -> int:
    """``sessions import <path> [--from claude|codex]``：把外部会话拉进 Orbit 会话库。"""
    from app.storage import Conversation, Message, now

    source = str(getattr(args, "from", "") or "").strip().lower()
    raw_path = str(getattr(args, "path", "") or "").strip()
    if not raw_path:
        raise CliError(
            "用法：orbit sessions import <会话JSONL路径> [--from claude|codex]"
            "（Orbit 不做交互式挑选：猜一个目录去读别人的私有会话数据比报用法错更冒犯）"
        )
    path = Path(raw_path).expanduser()
    if not path.exists():
        raise CliError(f"文件不存在：{path}")
    if source and source not in {"claude", "codex"}:
        raise CliError(f"`--from {source}` 不认识（可选 claude / codex）")

    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise CliError(f"读不了文件：{path}（{exc}）") from exc
    title, rows = _parse_jsonl(text)
    if not rows:
        raise CliError(
            f"{path} 里没有可导入的消息（认 Orbit 导出 / Claude Code / 通用 role+content 三种 JSONL）"
        )

    stamp = now()

    async def go() -> Any:
        store = context.store
        conversation = await store.save(
            Conversation(title=title or f"导入自 {path.name}", created_at=stamp, updated_at=stamp)
        )
        for row in rows:
            await store.save(
                Message(
                    conversation_id=conversation.id,
                    role=row["role"],
                    content=row["content"],
                    created_at=row["created_at"] or stamp,
                )
            )
        return conversation

    conversation = context.run(go())
    plane = _plane(context)
    plane.append(
        {
            "kind": KIND_IMPORTED,
            "session_id": conversation.id,
            "source_tool": source or "auto",
            "path": str(path),
            "messages": len(rows),
            "summary": f"从 {path.name} 导入 {len(rows)} 条消息（{source or 'auto'}）",
        }
    )
    context.out(f"已导入 {len(rows)} 条消息 → 会话 {conversation.id}")
    context.emit(
        {
            "ok": True,
            "id": conversation.id,
            "messages": len(rows),
            "title": conversation.title,
            "path": str(path),
            "source": source or "auto",
        }
    )
    return 0


# ── sessions prune ──────────────────────────────────────────────────────────


def _prune_rows(context: Context) -> list[tuple[Any, int]]:
    """取 ``(会话, 消息数)``。用 SQL 聚合而不是把 messages 读进内存。"""
    from sqlalchemy import func, select

    from app.storage import Conversation, Message

    counts = (
        select(Message.conversation_id, func.count(Message.id).label("n"))
        .group_by(Message.conversation_id)
        .subquery()
    )

    async def go() -> Any:
        async with context.store.sessions() as session:
            return (
                await session.execute(
                    select(Conversation, func.coalesce(counts.c.n, 0)).outerjoin(
                        counts, counts.c.conversation_id == Conversation.id
                    )
                )
            ).all()

    return list(context.run(go()))


def run_prune(context: Context, args) -> int:
    """``sessions prune``：按过滤条件删除旧会话（默认 dry-run 之外的硬门禁是 ``--yes``）。"""
    unsupported = [
        f"--{name.replace('_', '-')}（{why}）"
        for name, why in _UNSUPPORTED_PRUNE.items()
        if getattr(args, name, None) not in (None, False, "")
    ]
    if unsupported:
        raise CliError(
            "这些过滤条件未实现：" + "、".join(unsupported) + "。"
            "Orbit 的 conversations 表没有对应列——静默忽略会让你以为按条件清理了，"
            "实际删掉的是另一批会话。可用：--older-than / --newer-than / --before / "
            "--after / --title / --min-messages / --max-messages / --dry-run / --yes"
        )

    now = time.time()
    older_than = getattr(args, "older_than", None)
    newer_than = getattr(args, "newer_than", None)
    before = getattr(args, "before", None)
    after = getattr(args, "after", None)
    title = str(getattr(args, "title", "") or "").strip()
    min_messages = getattr(args, "min_messages", None)
    max_messages = getattr(args, "max_messages", None)
    dry_run = bool(getattr(args, "dry_run", False))

    filters = [older_than, newer_than, before, after, title, min_messages, max_messages]
    if not any(item not in (None, "") for item in filters):
        # 裸 prune 必须有个窗口，否则等于"删全部"。Hermes 的默认是 90 天，照抄。
        older_than = str(DEFAULT_PRUNE_DAYS)
        context.warn(
            f"没给任何过滤条件，按 {DEFAULT_PRUNE_DAYS} 天处理（与 Hermes 的裸 prune 一致）"
        )

    cut_older = _cutoff(older_than, now=now) if older_than else None
    cut_newer = _cutoff(newer_than, now=now) if newer_than else None
    cut_before = _cutoff(before, now=now) if before else None
    cut_after = _cutoff(after, now=now) if after else None
    lowest = _int(min_messages, 0) if min_messages not in (None, "") else None
    highest = _int(max_messages, 0) if max_messages not in (None, "") else None

    picked: list[tuple[str, str, int]] = []
    for conversation, count in _prune_rows(context):
        stamp = to_epoch(conversation.updated_at)
        if cut_older is not None and stamp >= cut_older:
            continue
        if cut_newer is not None and stamp < cut_newer:
            continue
        if cut_before is not None and stamp >= cut_before:
            continue
        if cut_after is not None and stamp < cut_after:
            continue
        if title and title.lower() not in (conversation.title or "").lower():
            continue
        if lowest is not None and int(count) < lowest:
            continue
        if highest is not None and int(count) > highest:
            continue
        picked.append((conversation.id, conversation.title or "", int(count)))

    if dry_run or not picked:
        context.out(
            f"命中 {len(picked)} 个会话" + ("（--dry-run：未改动任何数据）" if picked else "")
        )
        context.out()
        context.table(
            ("会话", "标题", "消息数"),
            [(_short(item[0]), truncate(item[1] or "（无标题）", 40), str(item[2])) for item in picked],
        )
        context.emit({"ok": True, "dry_run": True, "count": len(picked), "sessions": [
            {"id": item[0], "title": item[1], "message_count": item[2]} for item in picked
        ]})
        return 0

    if not getattr(args, "yes", False):
        raise CliError(
            f"拒绝执行：将删除 {len(picked)} 个会话且不可撤销，必须显式加 --yes 确认"
            "（先跑一次 --dry-run 看清楚）"
        )

    # 复用 `sessions delete` 的级联删除，而不是在这里再写一遍：两处各写一份，
    # 迟早有一处漏删（task_events / chunks 漏删会让 insights 与召回口径失真）。
    from app.cli.commands.sessions import run_delete as delete_session

    removed = 0
    plane = _plane(context)
    for session_id, title_text, count in picked:
        delete_session(context, argparse.Namespace(session_id=session_id, yes=True))
        plane.append(
            {
                "kind": KIND_PRUNED,
                "session_id": session_id,
                "reason": "prune",
                "messages": count,
                "summary": f"prune 删除会话「{title_text}」（{count} 条消息）",
            }
        )
        removed += 1

    context.out(f"已删除 {removed} 个会话")
    context.emit({"ok": True, "dry_run": False, "removed": removed, "count": len(picked)})
    return 0


# ── orbit plane: timeline / lineage / tail / show / resume ───────────────────


def _timeline(context: Context, session_id: str, limit: int) -> list[Any]:
    plane = _plane(context)
    try:
        return context.run(
            plane.timeline(session_id, store=_store_or_none(context), limit=limit)
        )
    except SessionPlaneError as exc:
        raise CliError(str(exc)) from exc


def run_timeline(context: Context, args) -> int:
    """``plane timeline <会话ID>``：三个来源（plane / messages / task_events）合并时间线。"""
    session_id = _session_id(getattr(args, "session_id", ""))
    if not session_id:
        raise CliError("用法：orbit plane timeline <会话ID> [--limit N]")
    limit = _int(getattr(args, "limit", None), 0)
    entries = _timeline(context, session_id, limit)

    context.out(f"会话 {session_id} 的统一时间线（{len(entries)} 条）")
    context.out()
    context.table(
        ("时间", "来源", "类型", "内容"),
        [
            (item.ts, item.source, truncate(item.kind, 18), truncate(_flat(item.text), 60))
            for item in entries
        ],
    )
    context.emit(
        {"ok": True, "id": session_id, "count": len(entries), "entries": [item.as_dict() for item in entries]}
    )
    return 0


def run_tail(context: Context, args) -> int:
    """``plane tail <会话ID> [-n N]``：该会话**最近** N 条（合并时间线的尾部）。"""
    session_id = _session_id(getattr(args, "session_id", ""))
    if not session_id:
        raise CliError("用法：orbit plane tail <会话ID> [-n 20]")
    count = _int(getattr(args, "n", None), 20)
    if count <= 0:
        count = 20
    entries = _timeline(context, session_id, count)
    context.out(f"会话 {session_id} 最近 {len(entries)} 条")
    context.out()
    for item in entries:
        context.out(f"{item.ts} · {item.source}/{item.kind}｜{truncate(_flat(item.text), 70)}")
    context.emit(
        {"ok": True, "id": session_id, "count": len(entries), "entries": [item.as_dict() for item in entries]}
    )
    return 0


def run_lineage(context: Context, args) -> int:
    """``plane lineage <会话ID>``：沿压缩谱系上溯到根（循环引用会报错，不会挂死）。"""
    session_id = _session_id(getattr(args, "session_id", ""))
    if not session_id:
        raise CliError("用法：orbit plane lineage <会话ID>")
    try:
        chain = _plane(context).lineage(session_id)
    except SessionPlaneError as exc:
        raise CliError(str(exc)) from exc

    context.out(f"压缩谱系（{len(chain)} 层，自子向父）")
    context.out()
    context.table(
        ("深度", "会话", "父会话", "摘要字符数"),
        [
            (
                str(item["depth"]),
                _short(str(item["session_id"])),
                _short(str(item["parent"])) if item["parent"] else "（根）",
                str(item["summary_chars"]),
            )
            for item in chain
        ],
    )
    context.emit({"ok": True, "id": session_id, "depth": len(chain), "lineage": chain})
    return 0


def run_show(context: Context, args) -> int:
    """``plane show <会话ID>``：一个会话的平面视图（元数据 + 计数 + 谱系 + 最近事件）。"""
    from sqlalchemy import func, select

    from app.storage import Conversation, Message, Task

    session_id = _session_id(getattr(args, "session_id", ""))
    if not session_id:
        raise CliError("用法：orbit plane show <会话ID>")

    async def go() -> Any:
        async with context.store.sessions() as session:
            conversation = await session.get(Conversation, session_id)
            if conversation is None:
                return None, 0, 0
            messages = int(
                await session.scalar(
                    select(func.count(Message.id)).where(Message.conversation_id == session_id)
                )
                or 0
            )
            tasks = int(
                await session.scalar(
                    select(func.count(Task.id)).where(Task.conversation_id == session_id)
                )
                or 0
            )
            return conversation, messages, tasks

    conversation, messages, tasks = context.run(go())
    if conversation is None:
        raise CliError(f"会话不存在：{session_id}")

    plane = _plane(context)
    events = plane.events(session_id)
    try:
        chain = plane.lineage(session_id)
    except SessionPlaneError as exc:
        raise CliError(str(exc)) from exc

    context.out(f"会话 {session_id}")
    context.out()
    context.table(
        ("字段", "值"),
        [
            ("标题", conversation.title or "（无标题）"),
            ("消息数", str(messages)),
            ("任务数", str(tasks)),
            ("创建时间", conversation.created_at),
            ("更新时间", conversation.updated_at),
            ("平面事件", str(len(events))),
            ("谱系深度", str(len(chain))),
            ("父会话", chain[1]["session_id"] if len(chain) > 1 else "（根）"),
            ("摘要状态", conversation.summary_status),
        ],
    )
    context.emit(
        {
            "ok": True,
            "id": session_id,
            "title": conversation.title,
            "messages": messages,
            "tasks": tasks,
            "created_at": conversation.created_at,
            "updated_at": conversation.updated_at,
            "plane_events": len(events),
            "lineage": chain,
        }
    )
    return 0


def run_resume(context: Context, args) -> int:
    """``plane resume <会话ID>``：记一次恢复，并把"接着聊要做什么"说清楚。

    **它不跑模型**：真正的对话循环在 ``orbit chat``。这里做两件事——
    在平面上留一条 ``session.resumed`` 痕迹（审计用），以及把该会话的尾部打出来，
    让人不用再查一次就知道自己接的是哪一段。
    """
    from app.storage import Conversation

    session_id = _session_id(getattr(args, "session_id", ""))
    if not session_id:
        raise CliError("用法：orbit plane resume <会话ID>")

    async def go() -> Any:
        return await context.store.get(Conversation, session_id)

    conversation = context.run(go())
    if conversation is None:
        raise CliError(f"会话不存在：{session_id}")

    entries = _timeline(context, session_id, _int(getattr(args, "n", None), 5))
    _plane(context).append(
        {
            "kind": KIND_RESUMED,
            "session_id": session_id,
            "summary": f"恢复会话「{conversation.title or '（无标题）'}」",
        }
    )
    context.out(f"已记录恢复：{session_id}（{conversation.title or '（无标题）'}）")
    context.out()
    for item in entries:
        context.out(f"{item.ts} · {item.source}/{item.kind}｜{truncate(_flat(item.text), 70)}")
    context.out()
    context.out(
        "说明：本命令只记录恢复并回显尾部；真正的对话循环是 `orbit chat`"
        "（它的 `--continue` 只能续最近一个会话，还不能按 ID 续）。"
    )
    context.emit(
        {
            "ok": True,
            "id": session_id,
            "title": conversation.title,
            "tail": [item.as_dict() for item in entries],
            "note": "对话循环在 orbit chat；本命令只记录恢复事件",
        }
    )
    return 0


# ── orbit plane: reap / deliveries ──────────────────────────────────────────


def run_reap(context: Context, args) -> int:
    """``plane reap [--apply]``：报告/清理"登记过但进程已死"的条目。

    **默认 dry-run**（只报告）。``--apply`` 才清账本；本命令**绝不 kill 进程**。
    """
    dry_run = not bool(getattr(args, "apply", False))
    if getattr(args, "dry_run", False) and getattr(args, "apply", False):
        raise CliError("--dry-run 与 --apply 互斥：要么只报告，要么真清理")
    reaper = OrphanReaper(context.data_dir)
    report = reaper.reap(dry_run=dry_run)

    mode = "只报告（dry-run）" if report.dry_run else "已清理"
    context.out(f"孤儿回收 · {mode}")
    context.out()
    context.table(
        ("来源", "pid", "判定", "说明"),
        [
            (item.source, str(item.pid), item.kind, truncate(item.reason, 50))
            for item in [*report.orphans, *report.stale]
        ],
    )
    if not report.orphans and not report.stale:
        context.out("（没有无效登记）")
    else:
        context.out()
        context.out(
            f"孤儿 {len(report.orphans)} 条 / 身份不符（PID 复用）{len(report.stale)} 条"
            + (f"；已清 {report.removed} 条登记" if not report.dry_run else "；未改动任何文件")
            + ("；已删孤儿锁" if report.lock_removed else "")
        )
    context.out("说明：本命令只清登记，绝不 kill 进程（判据基于进程创建时间，属尽力而为）。")
    context.emit({"ok": True, **report.as_dict()})
    return 0


def run_deliveries(context: Context, args) -> int:
    """``plane deliveries``：投递账本（按 key 的最新状态 + 概览）。"""
    limit = _int(getattr(args, "limit", None), 20)
    wanted = str(getattr(args, "status", "") or "").strip().lower()
    ledger = _ledger(context)
    stats = ledger.stats()
    records = ledger.records(limit=limit if limit > 0 else 0)
    if wanted:
        if wanted not in STATUSES:
            raise CliError(f"未知状态 {wanted!r}（可选 {'/'.join(STATUSES)}）")
        records = [item for item in records if item.get("status") == wanted]

    context.out(
        f"投递账本 · {ledger.path}"
        f"（{stats['keys']} 个 key / {stats['rows']} 行；已送达 {stats['delivered']}、"
        f"未送达 {stats['undelivered']}）"
    )
    context.out()
    context.table(
        ("key", "平台", "频道", "状态", "尝试", "错误"),
        [
            (
                truncate(str(item.get("message_key") or "（无 key）"), 24),
                str(item.get("platform") or ""),
                truncate(str(item.get("channel") or ""), 16),
                str(item.get("status") or ""),
                str(item.get("attempts") or 0),
                truncate(str(item.get("error") or ""), 30),
            )
            for item in records
        ],
    )
    context.emit({"ok": True, "path": str(ledger.path), "stats": stats, "records": records})
    return 0


# ── 接线 ────────────────────────────────────────────────────────────────────


def _bind(path: str, handler: Any) -> None:
    """把实现登记到声明表的某条命令路径上。

    两种导入顺序都必须成立（与 ``app/cli/commands/sessions.py`` 的 ``_bind`` 同理）：
    本模块排在 ``hermes_parity`` 之前时，``register_handler`` 生效、组装时直接填 handler；
    但任何 ``import app.cli.commands.session_plane`` 都会先跑包初始化，声明表可能**已经
    组装完毕**，此时 ``HANDLERS`` 的登记不会回填到已构建的 ``Command`` 上。
    所以这里补一次回填：路径已存在就替换 handler、状态改 ``done``。
    """
    register_handler(path, handler)
    head, _, leaf = path.partition(" ")
    parent = REGISTRY.get(head)
    target = parent.child(leaf) if (parent is not None and leaf) else parent
    if target is not None and not target.subcommands:
        target.handler = handler
        target.state = "done"


# 只接声明表里**还没被 sessions.py 接走**的两条（list/browse/stats/rename/export/delete
# 已是 done，重复接会触发 `register_handler` 的重复登记报错——那是刻意的门禁）。
_bind("sessions import", run_import)
_bind("sessions prune", run_prune)


def _json_option() -> Option:
    """子命令上的 ``--json``（``default=SUPPRESS`` 的理由见 sessions.py 的同名函数）。"""
    return Option(
        ("--json",), "以 JSON 输出（脚本友好）", action="store_true", default=argparse.SUPPRESS
    )


def _session_option() -> Option:
    return Option(("session_id",), "会话 ID", metavar="SESSION_ID")


REGISTRY.add(
    Command(
        name="plane",
        help="统一 session 平面：时间线 / 压缩谱系 / 投递账本 / 孤儿回收（Orbit 自有入口）",
        group="ops",
        state="done",
        parity="P1",
        # 纯读（reap 只清登记），不需要 prepare() 建目录。
        needs_settings=False,
        needs_store=False,
        subcommands=[
            Command(
                name="timeline",
                help="把平面事件与 messages / task_events 合并成一条时间线",
                handler=run_timeline,
                group="ops",
                state="done",
                needs_settings=False,
                needs_store=False,
                options=[
                    _session_option(),
                    Option(("--limit",), "只显示最近 N 条（0 = 全部）", metavar="LIMIT"),
                    _json_option(),
                ],
            ),
            Command(
                name="tail",
                help="某会话最近 N 条（合并时间线的尾部）",
                handler=run_tail,
                group="ops",
                state="done",
                needs_settings=False,
                needs_store=False,
                options=[
                    _session_option(),
                    Option(("-n", "--lines"), "显示多少条（默认 20）", metavar="N", dest="n"),
                    _json_option(),
                ],
            ),
            Command(
                name="lineage",
                help="沿压缩谱系上溯到根（循环引用会报错）",
                handler=run_lineage,
                group="ops",
                state="done",
                needs_settings=False,
                needs_store=False,
                options=[_session_option(), _json_option()],
            ),
            Command(
                name="show",
                help="一个会话的平面视图（元数据 / 计数 / 谱系深度）",
                handler=run_show,
                group="ops",
                state="done",
                needs_settings=False,
                needs_store=True,
                options=[_session_option(), _json_option()],
            ),
            Command(
                name="resume",
                help="记一次恢复并回显会话尾部（不跑模型）",
                handler=run_resume,
                group="ops",
                state="done",
                needs_settings=False,
                needs_store=True,
                options=[
                    _session_option(),
                    Option(("-n", "--lines"), "回显多少条（默认 5）", metavar="N", dest="n"),
                    _json_option(),
                ],
            ),
            Command(
                name="reap",
                help="报告/清理孤儿登记（默认只报告，--apply 才清账本；绝不 kill）",
                handler=run_reap,
                group="ops",
                state="done",
                needs_settings=False,
                needs_store=False,
                options=[
                    Option(("--dry-run",), "只报告不动账本（默认行为，显式写出来更清楚）", action="store_true"),
                    Option(("--apply",), "真的清理账本条目（与 --dry-run 互斥）", action="store_true"),
                    _json_option(),
                ],
            ),
            Command(
                name="deliveries",
                help="投递账本（至少一次语义的幂等账）",
                handler=run_deliveries,
                group="ops",
                state="done",
                needs_settings=False,
                needs_store=False,
                options=[
                    Option(("--limit",), "最近多少行（默认 20）", metavar="LIMIT"),
                    Option(
                        ("--status",),
                        "只看某个状态（pending/delivered/failed/dead）",
                        metavar="STATUS",
                    ),
                    _json_option(),
                ],
            ),
        ],
    )
)

__all__ = [
    "run_deliveries",
    "run_import",
    "run_lineage",
    "run_prune",
    "run_reap",
    "run_resume",
    "run_show",
    "run_tail",
    "run_timeline",
]
