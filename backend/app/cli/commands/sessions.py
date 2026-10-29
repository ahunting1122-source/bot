"""`orbit sessions …` 的真实现 + `orbit session-search` 自有入口。

为什么**不新建一个 `sessions` Command**：声明表（``hermes_parity.py``）里 ``sessions``
已经有 19 个子命令，本轮只实现其中 6 个。若把这 6 个"搬出表"另立一个 ``sessions``，
剩下 13 条会整片从 ``orbit --help`` 和矩阵里消失——命令面反而缩水。所以照
``app/cli/commands/_table.py`` 的约定用 ``register_handler(path, handler)``：表不动，
这里只声明"这个路径我接了"，组装时把 handler 填进去、状态改 ``done``。

为什么另外加一条顶层 ``session-search``：Hermes 的 ``session_search`` 是**模型工具**，
不是 CLI 命令；而 Orbit 侧需要一个人类/脚本也能直接用的入口来验证四形态。这条命令
不占 Hermes 的任何命令位（它是 Orbit 自有名字），所以不会污染对标矩阵。

三处**刻意的语义分歧**（照抄会变成假对齐，所以写在这里而不是悄悄实现）：

1. ``sessions list --source``：Orbit 是单用户本地部署，``conversations`` 表没有平台
   来源列（cli / telegram / discord…）。照抄一个永远匹配不到的过滤条件比不实现更
   误导，所以**接受参数但忽略**，并在 stderr 说明理由。
2. ``sessions export --format``：声明表里的可选值是 Hermes 的导出后端
   （``jsonl/md/qmd/html/trace``），本轮只做 ``jsonl`` 与 ``md``，其余明确报未实现。
3. ``sessions export`` 的位置参数在表里叫 ``output``（Hermes 是导出文件路径）。Orbit
   让它**优先当会话 ID**（于是 ``orbit sessions export <ID> --format md`` 成立）；
   两个都给时 ``--session-id`` 是 ID、位置参数是落盘路径。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select

from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context, truncate
from app.cli.spec import REGISTRY, Command, Option
from app.session_search import (
    DEFAULT_LIMIT,
    DEFAULT_WINDOW,
    SessionSearch,
    SessionSearchError,
)


def _int(value: Any, default: int) -> int:
    """把 CLI 传来的字符串参数读成整数；坏值退回默认（argparse 已挡掉多数情况）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _run(context: Context, coroutine: Any) -> Any:
    """在 CLI 的专用事件循环上跑一次召回，并把模块异常翻成可预期的 CLI 错误。"""
    try:
        return context.run(coroutine)
    except SessionSearchError as exc:
        raise CliError(str(exc)) from exc


def _session_id(value: Any) -> str:
    return str(value or "").strip()


def _short(session_id: str, width: int = 8) -> str:
    """表格里只显示 ID 前 8 位：完整 UUID 会把其他列挤到屏幕外。"""
    return truncate(session_id, width) if len(session_id) > width else session_id


# ── sessions list / browse ──────────────────────────────────────────────────


def _workspace_conversations(context: Context, needle: str) -> set[str]:
    """``--workspace`` 的命中集合：任务目标（``Task.goal``）里含该路径片段的会话。

    取 ``Task.goal`` 而不是磁盘上的 cwd，是因为 Orbit 的会话没有记录工作目录；任务
    目标里的路径（"整理 E:\\reports 里的表格"）是库里唯一真实存在的路径痕迹。
    """
    from app.storage import Task

    lowered = needle.lower()
    tasks = context.run(context.store.rows(select(Task)))
    return {task.conversation_id for task in tasks if lowered in (task.goal or "").lower()}


def _source_conversations(context: Context, source: str) -> set[str]:
    """按来源标签筛出会话 id。

    **为什么要先查 id 集合再过滤**：`searcher.browse()` 返回的是摘要 dict，
    走的是会话搜索那条路（带消息计数），在那里加一个 `source` 条件会牵动它的
    SQL 形状。查一次 id 集合更小、更不容易改坏既有查询。
    """
    from app.storage import Conversation

    rows = context.run(context.store.rows(select(Conversation).where(Conversation.source == source)))
    return {row.id for row in rows}


def run_list(context: Context, args) -> int:
    """`sessions list`：列出近期会话（``--workspace`` 做路径子串过滤）。"""
    limit = _int(getattr(args, "limit", None), 20)
    source = str(getattr(args, "source", "") or "").strip()
    workspace = str(getattr(args, "workspace", "") or "").strip()

    # `--source` **现在真的生效**（第 50 轮）：`conversations.source` 列已加。
    # 之前这里打印「已忽略」，而那比没有这个选项更糟——用户会以为它生效了。

    searcher = SessionSearch(context.store)
    # **过滤推进 SQL**（第 51 轮修）：第一版是「取最近 200 条再在内存里筛」，
    # 那是静默漏数据——第 201 条起的同来源会话永远查不到，且没有任何提示。
    # 换成更大的常数只是把「第 201 条漏」改成「第 2001 条漏」；任何魔法数都会漏。
    workspace_ids = _workspace_conversations(context, workspace) if workspace else None
    sessions = _run(
        context,
        searcher.browse(
            limit=limit, source=source, workspace=workspace, conversation_ids=workspace_ids
        ),
    )

    # **workspace 的过滤已经全部在 SQL 里**（见 `_browse_filters`）：
    # 两半（标题子串 / 任务目标命中的 id 集合）用 `or_` 组在同一个 where 里。
    # 留一半在内存就会互相打架——第一版就是这么错的。
    sessions = sessions[:limit]

    suffix = ""
    if workspace:
        suffix += f"（workspace 含 {workspace}）"
    if source:
        suffix += f"（来源 {source}）"
    context.out(f"会话 {len(sessions)} 条" + suffix)
    context.out()
    context.table(
        ("会话", "标题", "消息数", "更新时间"),
        [
            (_short(item["id"]), truncate(item["title"] or "（无标题）", 40), str(item["message_count"]), item["updated_at"])
            for item in sessions
        ],
    )
    context.emit(
        {
            "ok": True,
            "count": len(sessions),
            "sessions": sessions,
            # 第 50 轮起 `--source` **真的筛选**了，所以这个字段不再是「已忽略」，
            # 而是「本次按什么来源筛的」。**留着旧名字会让读 JSON 的脚本
            # 继续以为参数没生效。**
            "source": source,
            "workspace": workspace,
        }
    )
    return 0


def run_browse(context: Context, args) -> int:
    """`sessions browse`：最近会话列表（标题 / 消息数 / 首条用户消息片段）。"""
    limit = _int(getattr(args, "limit", None), 20)
    source = str(getattr(args, "source", "") or "").strip()
    if source:
        context.warn("`--source` 已忽略：Orbit 没有平台来源维度（见 `sessions list` 的同一条说明）。")

    sessions = _run(context, SessionSearch(context.store).browse(limit=limit))
    context.out(f"最近会话 {len(sessions)} 条")
    context.out()
    context.table(
        ("会话", "标题", "消息数", "更新时间", "首条用户消息"),
        [
            (
                _short(item["id"]),
                truncate(item["title"] or "（无标题）", 24),
                str(item["message_count"]),
                item["updated_at"],
                truncate(item["first_user_snippet"] or "", 40),
            )
            for item in sessions
        ],
    )
    context.emit({"ok": True, "count": len(sessions), "sessions": sessions})
    return 0


# ── sessions stats ──────────────────────────────────────────────────────────


def _scalar(context: Context, statement: Any) -> Any:
    rows = context.run(context.store.rows(statement))
    return rows[0] if rows else None


def run_stats(context: Context, args) -> int:
    """`sessions stats`：会话数 / 消息数 / 最老最新时间 / 平均每会话消息数。

    全部用 SQL 聚合而不是把表读进内存：``messages`` 会到十万条量级，一次
    ``select(Message)`` 只为了数个数，会把 CLI 卡住（也把内存吃掉）。
    """
    from app.storage import Conversation, Message

    conversations = int(_scalar(context, select(func.count(Conversation.id))) or 0)
    messages = int(_scalar(context, select(func.count(Message.id))) or 0)
    oldest = str(_scalar(context, select(func.min(Message.created_at))) or "")
    newest = str(_scalar(context, select(func.max(Message.created_at))) or "")
    average = (messages / conversations) if conversations else 0.0

    context.out("会话库统计")
    context.out()
    context.table(
        ("指标", "值"),
        [
            ("会话数", str(conversations)),
            ("消息数", str(messages)),
            ("最老消息时间", oldest or "（无）"),
            ("最新消息时间", newest or "（无）"),
            ("平均每会话消息数", f"{average:.1f}"),
        ],
    )
    context.emit(
        {
            "ok": True,
            "conversations": conversations,
            "messages": messages,
            "oldest_message_at": oldest,
            "newest_message_at": newest,
            "average_messages_per_conversation": round(average, 2),
        }
    )
    return 0


# ── sessions rename ─────────────────────────────────────────────────────────


def run_rename(context: Context, args) -> int:
    """`sessions rename <ID> <标题>`：改会话标题（写库，唯一会改内容的会话命令之一）。"""
    from app.storage import Conversation

    session_id = _session_id(getattr(args, "session_id", ""))
    title = str(getattr(args, "title", "") or "").strip()
    if not session_id or not title:
        raise CliError("用法：orbit sessions rename <会话ID> <新标题>")
    # 标题会出现在列表与摘要里；80 字符够长，再长就不是标题了（`SummaryResult` 也是 80）。
    title = title[:80]

    async def go() -> Any:
        conversation = await context.store.get(Conversation, session_id)
        if conversation is None:
            raise CliError(f"会话不存在：{session_id}")
        conversation.title = title
        await context.store.save(conversation)
        return conversation

    conversation = context.run(go())
    context.out(f"已改名：{session_id} → {conversation.title}")
    context.emit({"ok": True, "id": session_id, "title": conversation.title})
    return 0


# ── sessions export ─────────────────────────────────────────────────────────


def _render_jsonl(title: str, messages: list[Any]) -> str:
    """一行一条消息的 JSONL（Hermes 的默认导出格式，便于 `jq` 逐行处理）。

    首行是会话头（title / session_id），后续每行一条消息——这样 `head -1` 就能拿到
    标题，而流式读取时也不必先把整个文件解析成数组。
    """
    session_id = messages[0].conversation_id if messages else ""
    lines = [json.dumps({"session_id": session_id, "title": title}, ensure_ascii=False)]
    for message in messages:
        lines.append(
            json.dumps(
                {
                    "id": message.id,
                    "role": message.role,
                    "created_at": message.created_at,
                    "content": message.content,
                },
                ensure_ascii=False,
            )
        )
    return "\n".join(lines) + "\n"


def _render_markdown(title: str, messages: list[Any]) -> str:
    """Markdown 誊本：标题 + 按角色分节。给人读，不做机器解析承诺。"""
    lines = [f"# {title or '（无标题）'}", ""]
    for message in messages:
        lines.append(f"## {message.role} · {message.created_at}")
        lines.append("")
        lines.append(message.content or "")
        lines.append("")
    return "\n".join(lines)


def run_export(context: Context, args) -> int:
    """`sessions export <ID> --format {jsonl,md}`：导出整场会话。

    ``qmd`` / ``html`` / ``trace`` 属 Hermes 的导出后端（带模板与 lineage 逻辑），
    Orbit 没有对应实现——所以明确报"未实现"，而不是导出一份看起来像、其实缺字段的
    半成品（那种产物比报错更难发现）。
    """
    from app.storage import Conversation, Message

    fmt = str(getattr(args, "format", "") or "jsonl").strip().lower()
    flag_id = _session_id(getattr(args, "session_id", ""))
    positional = str(getattr(args, "output", "") or "").strip()
    session_id, target_path = (flag_id, positional) if flag_id else (positional, "")

    if not session_id:
        raise CliError("用法：orbit sessions export <会话ID> --format jsonl|md")
    if fmt not in {"jsonl", "md"}:
        raise CliError(
            f"`--format {fmt}` 未实现：本波次只做 jsonl 与 md。"
            "qmd / html / trace 是 Hermes 的导出后端，Orbit 没有对应实现。"
        )

    async def go() -> Any:
        conversation = await context.store.get(Conversation, session_id)
        if conversation is None:
            raise CliError(f"会话不存在：{session_id}")
        rows = await context.store.rows(
            select(Message)
            .where(Message.conversation_id == session_id)
            .order_by(Message.created_at.asc(), Message.id.asc())
        )
        return conversation, rows

    conversation, messages = context.run(go())
    text = (
        _render_markdown(conversation.title, messages)
        if fmt == "md"
        else _render_jsonl(conversation.title, messages)
    )

    if target_path:
        Path(target_path).expanduser().write_text(text, encoding="utf-8")
        context.out(f"已导出 {len(messages)} 条消息 → {target_path}")
    else:
        context.out(text)
    context.emit(
        {
            "ok": True,
            "id": session_id,
            "format": fmt,
            "messages": len(messages),
            "path": target_path,
            "content": text,
        }
    )
    return 0


# ── sessions delete ─────────────────────────────────────────────────────────


def run_delete(context: Context, args) -> int:
    """`sessions delete <ID> --yes`：删除会话及其全部关联行。

    **必须显式 ``--yes``**：Hermes 的 prune/delete 都是"给了过滤条件就开删"，而会话
    是用户唯一的原始记录，误删不可恢复（没有备份命令）。所以这里把确认做成硬门禁。
    """
    from app.storage import (
        Artifact,
        Chunk,
        Conversation,
        Message,
        Operation,
        Task,
        TaskEvent,
        ToolResult,
    )

    session_id = _session_id(getattr(args, "session_id", ""))
    if not session_id:
        raise CliError("用法：orbit sessions delete <会话ID> --yes")
    if not getattr(args, "yes", False):
        raise CliError(
            "拒绝执行：删除会话不可撤销，必须显式加 --yes 确认"
            f"（orbit sessions delete {session_id} --yes）"
        )

    async def go() -> Any:
        store = context.store
        async with store.lock, store.sessions() as session:
            conversation = await session.get(Conversation, session_id)
            if conversation is None:
                return None
            message_count = (
                await session.execute(delete(Message).where(Message.conversation_id == session_id))
            ).rowcount or 0
            task_ids = list(
                (await session.scalars(select(Task.id).where(Task.conversation_id == session_id))).all()
            )
            if task_ids:
                # 任务派生表一起清：SQLite 默认不校验外键，孤儿行不会报错，但会让
                # `insights` / `monitoring` 的口径把这批已删除的工作算进去。
                await session.execute(delete(TaskEvent).where(TaskEvent.task_id.in_(task_ids)))
                await session.execute(delete(Operation).where(Operation.task_id.in_(task_ids)))
                await session.execute(delete(ToolResult).where(ToolResult.task_id.in_(task_ids)))
                await session.execute(delete(Artifact).where(Artifact.task_id.in_(task_ids)))
                await session.execute(delete(Task).where(Task.conversation_id == session_id))
            # 检索索引里按会话记的片段也要清，否则 `memory_search` 会召回已删会话的正文。
            await session.execute(delete(Chunk).where(Chunk.conversation_id == session_id))
            await session.delete(conversation)
            await session.commit()
            return {"messages": message_count, "tasks": len(task_ids)}

    removed = context.run(go())
    if removed is None:
        raise CliError(f"会话不存在：{session_id}")
    context.out(
        f"已删除会话 {session_id}（消息 {removed['messages']} 条 / 任务 {removed['tasks']} 个）"
    )
    context.emit({"ok": True, "id": session_id, **removed})
    return 0


# ── 顶层 session-search：四形态的人类入口 ────────────────────────────────────


def _conversation(args) -> str | None:
    value = str(getattr(args, "conversation", "") or "").strip()
    return value or None


def run_search_discover(context: Context, args) -> int:
    """`session-search discover <词…>`：关键词 → 锚点列表。"""
    raw = getattr(args, "query", None) or []
    query = " ".join(raw) if isinstance(raw, list) else str(raw)
    anchors = _run(
        context,
        SessionSearch(context.store).discover(
            query,
            conversation_id=_conversation(args),
            limit=_int(getattr(args, "limit", None), DEFAULT_LIMIT),
        ),
    )
    context.out(f"命中 {len(anchors)} 条（关键词：{query}）")
    context.out()
    context.table(
        ("会话", "消息", "角色", "时间", "片段"),
        [
            (
                _short(anchor.conversation_id),
                _short(anchor.message_id),
                anchor.role,
                anchor.created_at,
                truncate(anchor.snippet, 50),
            )
            for anchor in anchors
        ],
    )
    context.emit(
        {
            "ok": True,
            "mode": "discover",
            "query": query,
            "count": len(anchors),
            "results": [anchor.as_dict() for anchor in anchors],
        }
    )
    return 0


def run_search_scroll(context: Context, args) -> int:
    """`session-search scroll <消息ID>`：锚点 ±window + 首尾书挡。"""
    message_id = _session_id(getattr(args, "message_id", ""))
    if not message_id:
        raise CliError("用法：orbit session-search scroll <消息ID> [--window N]")
    view = _run(
        context,
        SessionSearch(context.store).anchored_view(
            message_id, window=_int(getattr(args, "window", None), DEFAULT_WINDOW)
        ),
    )
    context.out(f"锚点 {message_id}（窗口 {len(view['window'])} 条）")
    context.out()
    for item in view["before"]:
        context.out(f"[书挡] {item['role']} · {item['created_at']}｜{truncate(item['content'], 60)}")
    for item in view["window"]:
        mark = "▶" if item.get("anchor") else " "
        context.out(f"{mark} {item['role']} · {item['created_at']}｜{truncate(item['content'], 60)}")
    for item in view["after"]:
        context.out(f"[书挡] {item['role']} · {item['created_at']}｜{truncate(item['content'], 60)}")
    context.emit({"ok": True, "mode": "scroll", **view})
    return 0


def run_search_read(context: Context, args) -> int:
    """`session-search read <消息ID>`：单条消息头 20 行 + 尾 10 行。"""
    message_id = _session_id(getattr(args, "message_id", ""))
    if not message_id:
        raise CliError("用法：orbit session-search read <消息ID>")
    payload = _run(context, SessionSearch(context.store).read(message_id))
    suffix = f"，省略 {payload['omitted_lines']} 行" if payload["truncated"] else ""
    context.out(
        f"{payload['role']} · {payload['created_at']}（{payload['total_lines']} 行{suffix}）"
    )
    context.out()
    context.out(payload["content"])
    context.emit({"ok": True, "mode": "read", **payload})
    return 0


def run_search_browse(context: Context, args) -> int:
    """`session-search browse`：最近会话（时间倒序）。"""
    sessions = _run(
        context,
        SessionSearch(context.store).browse(limit=_int(getattr(args, "limit", None), 20)),
    )
    context.out(f"最近会话 {len(sessions)} 条")
    context.out()
    context.table(
        ("会话", "标题", "消息数", "更新时间", "首条用户消息"),
        [
            (
                _short(item["id"]),
                truncate(item["title"] or "（无标题）", 24),
                str(item["message_count"]),
                item["updated_at"],
                truncate(item["first_user_snippet"] or "", 40),
            )
            for item in sessions
        ],
    )
    context.emit({"ok": True, "mode": "browse", "count": len(sessions), "results": sessions})
    return 0


# ── 接线 ────────────────────────────────────────────────────────────────────


def _bind(path: str, handler: Any) -> None:
    """把实现登记到声明表的某条命令路径上。

    两种导入顺序都必须成立：生产接线里本模块排在 ``hermes_parity`` **之前**，于是
    ``register_handler`` 生效、表组装时直接把 handler 填进去；但
    ``app/cli/commands/__init__.py`` 是包初始化，任何 ``import
    app.cli.commands.sessions`` 都会先跑它——此时声明表可能**已经组装完毕**，
    ``HANDLERS`` 里的登记不会再回填到已构建的 ``Command`` 上（它会停在"尚未实现"的
    占位 handler）。所以这里补一次回填：路径已存在就替换 handler、状态改 ``done``；
    不存在就什么都不做，留给 ``register_specs`` 去填。没有这一步，测试里跑
    ``sessions list`` 只会拿到"计划波次 W8"的报错。
    """
    register_handler(path, handler)
    head, _, leaf = path.partition(" ")
    parent = REGISTRY.get(head)
    target = parent.child(leaf) if (parent is not None and leaf) else parent
    if target is not None and not target.subcommands:
        target.handler = handler
        target.state = "done"


# ── sessions reset ─────────────────────────────────────────────────────────


def run_reset(context: Context, args) -> int:
    """``sessions reset <ID>``：清空当前对话上下文，**保留长期记忆**（MATRIX 13.5）。

    ## 为什么需要这条

    Orbit 之前**没有「会话重置」这个动作**——只有 ``delete``（连会话一起删）与
    ``archive``。于是钩子事件 ``on_session_reset`` **一直没有发射点**。
    而这不能靠「在 delete 里顺手 emit」糊过去：**``delete`` 不是 ``reset``**，
    把删除伪装成重置会让订阅者收到一句假话。

    ## 语义（四件，都是刻意选的）

    1. **清什么**：这条会话的 ``Message`` 行（当前对话上下文）。
    2. **不清什么**：``Conversation`` 行本身、**以及 ``MEMORY.md`` / 长期记忆**。
       理由是**破坏性不对称**——不清可以手动清，**清了不可恢复**；
       长期记忆是跨会话的事实，重置的是「这次对话说到哪了」，
       不是「关于用户知道什么」。
    3. **有活跃任务时拒绝**：任务在跑（``PENDING`` / ``RUNNING`` / ``WAITING_USER``）
       就报错退出。**这不是实现细节，是并发语义**：等 = 用户点了没反应，
       夺 = 任务被腰斩，两者都不可接受，所以先拒绝并说清楚。
       **活跃任务下的重置语义单独排期**（13.5 第二步），不在这条里蒙过去。
    4. **必须显式 ``--yes``**：与 ``delete`` 同一条理由——不可恢复的操作要有硬门禁。

    ## 必须说清边界

    输出里明写「**已清空当前对话；长期记忆保留**」。不写这句，用户不知道清到哪，
    会以为记忆也被清了（或反之）。
    """
    from sqlalchemy import delete, func, select

    from app import hooks
    from app.storage import Conversation, Message, Task

    session_id = _session_id(getattr(args, "session_id", ""))
    if not session_id:
        raise CliError("用法：orbit sessions reset <会话ID> --yes")
    if not getattr(args, "yes", False):
        raise CliError(
            "拒绝执行：重置会清空当前对话且不可恢复，必须显式加 --yes 确认"
            f"（orbit sessions reset {session_id} --yes）"
        )

    async def go() -> Any:
        store = context.store
        async with store.lock, store.sessions() as session:
            conversation = await session.get(Conversation, session_id)
            if conversation is None:
                return None
            active = (
                await session.execute(
                    select(func.count())
                    .select_from(Task)
                    .where(
                        Task.conversation_id == session_id,
                        Task.status.in_(["PENDING", "RUNNING", "WAITING_USER"]),
                    )
                )
            ).scalar_one()
            if active:
                raise CliError(
                    f"拒绝执行：会话 {session_id} 还有 {active} 个未结束的任务。"
                    "**重置只支持无活跃任务的会话**——任务在跑时「等」会让用户点了没反应、"
                    "「夺」会把任务腰斩，两种都不该由一条清理命令替你决定。"
                    "请先 `orbit sessions list` 确认，或等任务结束后再试。"
                )
            cleared = (
                await session.execute(
                    delete(Message).where(Message.conversation_id == session_id)
                )
            ).rowcount or 0
            await session.commit()
        return {"cleared": int(cleared)}

    result = context.run(go())
    if result is None:
        raise CliError(f"找不到会话：{session_id}")
    cleared = result["cleared"]

    # **发射点在这里**（MATRIX 13.5）：Orbit 之前没有「重置」这个动作，
    # 所以 `on_session_reset` 一直没地方发。现在有了，就必须发。
    hooks.emit(
        "on_session_reset",
        {
            "conversation_id": session_id,
            "cleared_messages": cleared,
            "scope": "messages_only",
        },
    )

    context.out(f"已重置会话 {session_id}：清空 {cleared} 条消息。")
    context.out("**已清空当前对话；长期记忆（MEMORY.md / USER.md）保留。**")
    context.emit(
        {"ok": True, "id": session_id, "cleared_messages": cleared, "memory_kept": True}
    )
    return 0


_bind("sessions list", run_list)
_bind("sessions browse", run_browse)
_bind("sessions stats", run_stats)
_bind("sessions rename", run_rename)
_bind("sessions export", run_export)
_bind("sessions delete", run_delete)
_bind("sessions reset", run_reset)


def _json_option() -> Option:
    """子命令上的 ``--json``。

    ``default=argparse.SUPPRESS`` 不能省：argparse 会用**子解析器的默认值**覆盖父级
    已经解析出来的同名属性，写成 ``False`` 就会把 `orbit --json session-search …`
    里的 ``--json`` 抹掉（`main.py` 的全局选项正是为此用了 SUPPRESS）。
    """
    return Option(
        ("--json",), "以 JSON 输出（脚本友好）", action="store_true", default=argparse.SUPPRESS
    )


REGISTRY.add(
    Command(
        name="session-search",
        help="会话召回：discover / scroll / read / browse 四形态（零模型调用）",
        group="core",
        state="done",
        # 纯只读：不 prepare()（不建目录），但要库存在才能查。
        needs_settings=False,
        needs_store=True,
        subcommands=[
            Command(
                name="discover",
                help="按关键词找候选消息，返回锚点（子串匹配，多词 AND）",
                handler=run_search_discover,
                group="core",
                state="done",
                needs_settings=False,
                needs_store=True,
                options=[
                    Option(("query",), "关键词；可给多个词，全部命中才算命中", metavar="QUERY", nargs="+"),
                    Option(("--conversation",), "只在某个会话内检索", metavar="CONVERSATION_ID"),
                    Option(("--limit",), "返回条数（夹取 [1,10]，默认 3）", metavar="LIMIT"),
                    _json_option(),
                ],
            ),
            Command(
                name="scroll",
                help="围绕锚点展开 ±window 条，并补首尾书挡",
                handler=run_search_scroll,
                group="core",
                state="done",
                needs_settings=False,
                needs_store=True,
                options=[
                    Option(("message_id",), "锚点消息 ID（discover 返回的 message_id）", metavar="MESSAGE_ID"),
                    Option(("--window",), "锚点前后各取几条（夹取 [1,20]，默认 5）", metavar="WINDOW"),
                    _json_option(),
                ],
            ),
            Command(
                name="read",
                help="读单条消息的头 20 行 + 尾 10 行（中间标省略行数）",
                handler=run_search_read,
                group="core",
                state="done",
                needs_settings=False,
                needs_store=True,
                options=[
                    Option(("message_id",), "要读的消息 ID", metavar="MESSAGE_ID"),
                    _json_option(),
                ],
            ),
            Command(
                name="browse",
                help="按时间倒序列出最近会话",
                handler=run_search_browse,
                group="core",
                state="done",
                needs_settings=False,
                needs_store=True,
                options=[
                    Option(("--limit",), "列出多少条最近会话（默认 20）", metavar="LIMIT"),
                    _json_option(),
                ],
            ),
        ],
    )
)
