"""``orbit sessions archive|unarchive|archived|reindex|search|leases|reap`` 的真实现。

本模块补 MATRIX L10 剩下的四条：归档不删（10.3）、提示词内容寻址的读侧（10.5）、
存活租约（10.8）、双 FTS 检索（10.12）。实现在三个独立模块里
（``app/session_archive.py`` / ``app/session_leases.py`` / ``app/session_index.py``），
这里只做"命令面 ↔ 实现"的翻译与接线。

## 接线方式：一半走 ``register_handler``，一半补节点

``app/cli/commands/_table.py`` 的约定是"表不动，实现方声明路径"。但本模块要接的七条里，
**只有 ``sessions archive`` 在声明表里**（Hermes 有这条），其余六条 Hermes 没有
（``unarchive`` / ``archived`` / ``reindex`` / ``search`` / ``leases`` / ``reap``）：
Orbit 需要"归档要能取消、租约要看得到"，而 Hermes 那边取消归档是桌面端的按钮、
租约是内部文件。所以分两种接法：

- ``sessions archive``：``register_handler`` + 回填（与 ``sessions.py`` 的 ``_bind`` 同理）；
- 其余六条（``unarchive`` / ``archived`` / ``reindex`` / ``search`` / ``leases`` / ``reap``）
  加上一条 Orbit 自有的 ``prompt``：**在运行时把 ``Command`` 节点补到已登记的
  ``sessions`` 下面**。为什么必须这样：``register_handler`` 只在 ``register_specs``
  组装声明表时生效，而表里根本没有这几个名字，登记进去永远没人组装——
  ``orbit sessions unarchive`` 会以"未识别参数"失败。

## 导入顺序不敏感

``app/cli/commands/__init__.py`` 的约定是"先导功能域、最后导声明表"。若本模块被排在
声明表**之前**导入，``REGISTRY.get("sessions")`` 还是 ``None``，补节点无处可补。
所以模块顶部**显式导入声明表**（幂等：``register_specs`` 会跳过已登记的名字），
让"本模块一定跑在表组装之后"。代价是表的组装时机提前，收益是接线顺序随便排都不会
静默少掉六条命令——静默少命令是最难发现的一类故障。

## 三处刻意的语义分歧

1. ``sessions archive`` 在 Hermes 里是**按过滤条件批量归档**，没有 ``session_id``。
   这里额外加一个**可选位置参数** ``[session_id]``：给了一个 id 就只归档它
   （"这个会话先收起来"是最常见的手动操作），不给才按过滤条件批量。
2. ``sessions archive`` **不设 ``--yes`` 门禁**（表里那个 ``-y`` 仍被接受但不用）：
   归档是可逆的（``unarchive`` 一条命令回来），消息一条不删。硬门禁留给
   ``sessions delete``——两者危险程度差一个量级，用同一套门禁会让门禁失去意义。
3. ``sessions reap`` 与 ``plane reap`` 是**两件事**：前者清 ``runtime/active_sessions.json``
   里失效的会话租约，后者清 ``gateway/`` 下三个登记簿里的孤儿条目。两个都只清登记、
   都绝不 kill 进程，但管的文件不同，所以没有合并成一条。
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from app.cli.commands import hermes_parity  # noqa: F401 —— 保证声明表已组装（见模块 docstring）
from app.cli.commands._table import register_handler
from app.cli.commands.session_plane import _UNSUPPORTED_PRUNE, _cutoff, _int, _short
from app.cli.context import CliError, Context, truncate
from app.cli.spec import REGISTRY, Command, Option
from app.session_archive import ArchiveError, PromptStore, SessionArchive
from app.session_index import FtsUnavailable, SessionIndex, SessionIndexError
from app.session_leases import DEFAULT_MAX_AGE, LeaseError, LeaseStore
from app.session_plane import to_epoch

#: 归档的"未实现过滤条件"口径。**复用** ``sessions prune`` 的那张表（同一批列在
#: ``conversations`` 里都不存在，复制一份迟早两边说法不一致），只覆盖一条：
#: prune 那张表说"Orbit 没有归档态"——本模块之后这句话不成立了。
_UNSUPPORTED_FILTERS: dict[str, str] = {
    **_UNSUPPORTED_PRUNE,
    "include_archived": "归档态记在平面日志里（用 `sessions archived` 查看），conversations 表没有可过滤的列",
    "include_pinned": "Orbit 没有置顶态（pin 未实现）",
}

#: 裸 ``sessions archive``（不给 id、也不给任何过滤条件）的默认行为是**报错**，
#: 不是"归档全部"。归档虽然可逆，但"手滑把整库收起来"会让所有列表当场变空。
_FILTER_HINT = (
    "给一个会话 id（`orbit sessions archive <ID>`），或至少一个过滤条件"
    "（--older-than / --newer-than / --before / --after / --title / "
    "--min-messages / --max-messages）。裸 archive 不归档全部：那会让所有列表当场变空。"
)


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _archive(context: Context) -> SessionArchive:
    return SessionArchive(context.data_dir)


def _prompts(context: Context) -> PromptStore:
    return PromptStore(context.data_dir)


def _leases(context: Context, *, max_age: Any = None) -> LeaseStore:
    """``--max-age`` 坏值退回默认，而不是报错：它只影响判定松紧，不影响数据。"""
    age = _int(max_age, int(DEFAULT_MAX_AGE)) if max_age not in (None, "") else int(DEFAULT_MAX_AGE)
    return LeaseStore(context.data_dir, max_age=float(max(age, 0)))


def _has_db(context: Context) -> bool:
    return context.db_path.exists()


def _counts(context: Context, session_ids: list[str]) -> dict[str, int]:
    """批量数每个会话有多少条消息（一条 SQL，不是逐会话查）。

    只在"库存在"时做：``sessions archive`` 不依赖数据库也能用（归档只写平面日志），
    强行 require_db 会让"库还没建好时先记一笔归档"这种场景直接报错。
    """
    if not session_ids or not _has_db(context):
        return {}
    from sqlalchemy import func, select

    from app.storage import Message

    async def go() -> Any:
        async with context.store.sessions() as session:
            rows = (
                await session.execute(
                    select(Message.conversation_id, func.count(Message.id))
                    .where(Message.conversation_id.in_(session_ids))
                    .group_by(Message.conversation_id)
                )
            ).all()
        return {str(row[0]): int(row[1]) for row in rows}

    return dict(context.run(go()))


def _titles(context: Context, session_ids: list[str]) -> dict[str, str]:
    """批量取标题（库不存在或会话已删时缺项，调用方自己兜底）。"""
    if not session_ids or not _has_db(context):
        return {}
    from sqlalchemy import select

    from app.storage import Conversation

    async def go() -> Any:
        async with context.store.sessions() as session:
            rows = (
                await session.execute(
                    select(Conversation.id, Conversation.title).where(
                        Conversation.id.in_(session_ids)
                    )
                )
            ).all()
        return {str(row[0]): str(row[1] or "") for row in rows}

    return dict(context.run(go()))


def _exists(context: Context, session_id: str) -> bool:
    """会话在不在库里。库不存在时返回 ``True``（不因为"没库"拒绝归档）。"""
    if not _has_db(context):
        return True
    return session_id in _titles(context, [session_id])


def _candidates(context: Context) -> list[tuple[str, str, str, int]]:
    """``(会话 id, 标题, updated_at, 消息数)``：与 ``sessions prune`` 同一条聚合查询。"""
    if not _has_db(context):
        return []
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
                    select(
                        Conversation.id,
                        Conversation.title,
                        Conversation.updated_at,
                        func.coalesce(counts.c.n, 0),
                    ).outerjoin(counts, counts.c.conversation_id == Conversation.id)
                )
            ).all()

    return [
        (str(row[0]), str(row[1] or ""), str(row[2] or ""), int(row[3])) for row in context.run(go())
    ]


def _json_option() -> Option:
    """子命令上的 ``--json``（``default=SUPPRESS`` 的理由见 ``sessions.py`` 的同名函数）。"""
    return Option(
        ("--json",), "以 JSON 输出（脚本友好）", action="store_true", default=argparse.SUPPRESS
    )


# ── sessions archive ────────────────────────────────────────────────────────


def run_archive(context: Context, args) -> int:
    """``sessions archive [ID] [过滤条件] [--dry-run]``：只打归档标记，**不删消息**。"""
    unsupported = [
        f"--{name.replace('_', '-')}（{why}）"
        for name, why in _UNSUPPORTED_FILTERS.items()
        if getattr(args, name, None) not in (None, False, "")
    ]
    if unsupported:
        raise CliError(
            "这些过滤条件未实现：" + "、".join(unsupported) + "。"
            "静默忽略会让你以为按条件归档了，实际归档的是另一批会话。"
            "可用：--older-than / --newer-than / --before / --after / --title / "
            "--min-messages / --max-messages / --dry-run"
        )

    session_id = str(getattr(args, "session_id", "") or "").strip()
    dry_run = bool(getattr(args, "dry_run", False))
    archive = _archive(context)

    if session_id:
        if not _exists(context, session_id):
            raise CliError(f"会话不存在：{session_id}")
        counts = _counts(context, [session_id])
        if dry_run:
            context.out("命中 1 个会话（--dry-run：未改动任何数据）")
            context.out()
            context.table(
                ("会话", "消息数", "已归档"),
                [
                    (
                        _short(session_id),
                        str(counts.get(session_id, 0)),
                        "是" if archive.is_archived(session_id) else "否",
                    )
                ],
            )
            context.emit(
                {
                    "ok": True,
                    "dry_run": True,
                    "count": 1,
                    "sessions": [
                        {"id": session_id, "messages": counts.get(session_id, 0),
                         "archived": archive.is_archived(session_id)}
                    ],
                }
            )
            return 0
        event = archive.archive(
            session_id,
            reason="cli",
            messages=counts.get(session_id, 0),
        )
        context.out(
            f"已归档 {session_id}（消息 {counts.get(session_id, 0)} 条一条未动；"
            f"平面事件 #{event.get('id')}）"
        )
        context.out("说明：归档只打标记，不删任何消息。恢复用 `orbit sessions unarchive <ID>`。")
        context.emit(
            {
                "ok": True,
                "dry_run": False,
                "count": 1,
                "sessions": [{"id": session_id, "messages": counts.get(session_id, 0)}],
                "event": event,
            }
        )
        return 0

    # 批量：按过滤条件挑
    now = time.time()
    older_than = getattr(args, "older_than", None)
    newer_than = getattr(args, "newer_than", None)
    before = getattr(args, "before", None)
    after = getattr(args, "after", None)
    title = str(getattr(args, "title", "") or "").strip()
    min_messages = getattr(args, "min_messages", None)
    max_messages = getattr(args, "max_messages", None)
    filters = [older_than, newer_than, before, after, title, min_messages, max_messages]
    if not any(item not in (None, "") for item in filters):
        raise CliError(_FILTER_HINT)

    cut_older = _cutoff(older_than, now=now) if older_than else None
    cut_newer = _cutoff(newer_than, now=now) if newer_than else None
    cut_before = _cutoff(before, now=now) if before else None
    cut_after = _cutoff(after, now=now) if after else None
    lowest = _int(min_messages, 0) if min_messages not in (None, "") else None
    highest = _int(max_messages, 0) if max_messages not in (None, "") else None

    picked: list[tuple[str, str, int]] = []
    for ident, name, updated_at, count in _candidates(context):
        stamp = to_epoch(updated_at)
        if cut_older is not None and stamp >= cut_older:
            continue
        if cut_newer is not None and stamp < cut_newer:
            continue
        if cut_before is not None and stamp >= cut_before:
            continue
        if cut_after is not None and stamp < cut_after:
            continue
        if title and title.lower() not in name.lower():
            continue
        if lowest is not None and count < lowest:
            continue
        if highest is not None and count > highest:
            continue
        if archive.is_archived(ident):
            continue  # 已归档的不重复记（幂等，且不让日志被同义事件灌满）
        picked.append((ident, name, count))

    if dry_run or not picked:
        context.out(f"命中 {len(picked)} 个会话" + ("（--dry-run：未改动任何数据）" if picked else ""))
        context.out()
        context.table(
            ("会话", "标题", "消息数"),
            [(_short(item[0]), truncate(item[1] or "（无标题）", 40), str(item[2])) for item in picked],
        )
        context.emit(
            {
                "ok": True,
                "dry_run": True,
                "count": len(picked),
                "sessions": [
                    {"id": item[0], "title": item[1], "messages": item[2]} for item in picked
                ],
            }
        )
        return 0

    for ident, _name, count in picked:
        archive.archive(ident, reason="cli-batch", messages=count)
    context.out(f"已归档 {len(picked)} 个会话（消息一条未删；恢复用 `orbit sessions unarchive`）")
    context.emit(
        {
            "ok": True,
            "dry_run": False,
            "count": len(picked),
            "sessions": [{"id": item[0], "messages": item[2]} for item in picked],
        }
    )
    return 0


# ── sessions unarchive / archived ───────────────────────────────────────────


def run_unarchive(context: Context, args) -> int:
    """``sessions unarchive <ID>``：取消归档标记（消息同样一条不碰）。"""
    session_id = str(getattr(args, "session_id", "") or "").strip()
    if not session_id:
        raise CliError("用法：orbit sessions unarchive <会话ID>")
    try:
        event = _archive(context).unarchive(session_id, reason="cli")
    except ArchiveError as exc:
        raise CliError(str(exc)) from exc
    context.out(f"已取消归档 {session_id}（平面事件 #{event.get('id')}）")
    context.emit({"ok": True, "id": session_id, "event": event})
    return 0


def run_archived(context: Context, args) -> int:
    """``sessions archived``：列出已归档会话（最近归档的在前）。"""
    limit = _int(getattr(args, "limit", None), 50)
    entries = _archive(context).list_archived(limit=limit if limit > 0 else 0)
    titles = _titles(context, [item.session_id for item in entries])
    context.out(f"已归档会话 {len(entries)} 个")
    context.out()
    context.table(
        ("会话", "标题", "归档时间", "消息数", "原因"),
        [
            (
                _short(item.session_id),
                truncate(titles.get(item.session_id) or "（不在本库）", 32),
                item.archived_at,
                str(item.messages),
                truncate(item.reason or "-", 16),
            )
            for item in entries
        ],
    )
    context.out()
    context.out("说明：归档不删消息。恢复用 `orbit sessions unarchive <ID>`。")
    context.emit(
        {
            "ok": True,
            "count": len(entries),
            "sessions": [
                {**item.as_dict(), "title": titles.get(item.session_id, "")} for item in entries
            ],
        }
    )
    return 0


# ── sessions reindex / search ───────────────────────────────────────────────


def _index(context: Context) -> SessionIndex:
    return SessionIndex(context.data_dir)


def run_reindex(context: Context, args) -> int:
    """``sessions reindex [--rebuild]``：把 messages 增量同步进 fts.sqlite。"""
    context.require_db()
    index = _index(context)
    rebuild = bool(getattr(args, "rebuild", False))
    batch = _int(getattr(args, "batch", None), 0)

    async def go() -> Any:
        if rebuild:
            return await index.rebuild(context.store, batch=batch or 1000)
        return await index.index_messages(context.store, limit=batch)

    try:
        report = context.run(go())
    except FtsUnavailable as exc:
        raise CliError(str(exc)) from exc
    stats = index.stats()
    context.out(
        f"索引{'重建' if rebuild else '增量同步'}完成：新增 {report.added} 条"
        f"（扫到 {report.scanned} 条，水位 rowid={report.watermark}）"
    )
    context.out()
    context.table(
        ("项", "值"),
        [
            ("索引库", stats["path"]),
            ("镜像行数", str(stats["rows"])),
            ("unicode61 行数", str(stats["unicode_rows"])),
            ("trigram 行数", str(stats["trigram_rows"])),
            ("同步水位", str(stats["watermark"])),
            ("文件字节", str(stats["bytes"])),
        ],
    )
    context.emit({"ok": True, "rebuild": rebuild, **report.as_dict(), "stats": stats})
    return 0


def run_search(context: Context, args) -> int:
    """``sessions search <词…>``：走双 FTS 索引（中文与英文各走一条路）。"""
    raw = getattr(args, "query", None) or []
    query = " ".join(raw) if isinstance(raw, list) else str(raw)
    query = query.strip()
    if not query:
        raise CliError("用法：orbit sessions search <关键词…> [--limit N]")
    index = _index(context)
    limit = _int(getattr(args, "limit", None), 10)
    try:
        plan = index.plan(query)
        hits = index.search(query, limit)
    except FtsUnavailable as exc:
        # 明确报错 + 给降级路径，不静默退回 LIKE（见 session_index 模块 docstring）。
        raise CliError(str(exc)) from exc
    except SessionIndexError as exc:
        raise CliError(str(exc)) from exc

    context.out(f"命中 {len(hits)} 条（查询：{query}）")
    context.out(f"选表：{plan.mode} → {' + '.join(plan.tables)}")
    context.out(f"理由：{plan.reason}")
    context.out()
    context.table(
        ("会话", "消息", "角色", "时间", "表", "片段"),
        [
            (
                _short(hit.conversation_id),
                _short(hit.message_id),
                hit.role,
                hit.created_at,
                "trigram" if hit.table.endswith("trigram") else "unicode61",
                truncate(hit.snippet, 48),
            )
            for hit in hits
        ],
    )
    context.emit(
        {
            "ok": True,
            "query": query,
            "count": len(hits),
            "plan": plan.as_dict(),
            "results": [hit.as_dict() for hit in hits],
        }
    )
    return 0


# ── sessions leases / reap ──────────────────────────────────────────────────


def run_leases(context: Context, args) -> int:
    """``sessions leases``：看 ``runtime/active_sessions.json`` 里谁还活着。"""
    store = _leases(context, max_age=getattr(args, "max_age", None))
    verdicts = store.verdicts()
    live = [item for item in verdicts if item.live]
    context.out(
        f"存活租约 {len(live)} / 登记 {len(verdicts)}（心跳超时 {store.max_age:.0f}s；"
        f"判据 = (pid, host_start_time) 二元组 + 心跳新鲜度）"
    )
    context.out()
    context.table(
        ("会话", "pid", "判定", "心跳距今", "平台"),
        [
            (
                _short(item.session_id),
                str(item.pid),
                item.kind + ("（心跳过期）" if item.expired else ""),
                f"{item.age:.0f}s",
                (item.lease.platform if item.lease else "") or "-",
            )
            for item in verdicts
        ],
    )
    context.out()
    context.out(f"登记簿：{store.path}")
    context.emit(
        {
            "ok": True,
            "path": str(store.path),
            "max_age": store.max_age,
            "count": len(verdicts),
            "live": len(live),
            "leases": [item.as_dict() for item in verdicts],
        }
    )
    return 0


def run_reap(context: Context, args) -> int:
    """``sessions reap [--apply]``：清掉失效的会话租约。**默认只报告，绝不 kill 进程**。"""
    dry_run = not bool(getattr(args, "apply", False))
    if getattr(args, "dry_run", False) and getattr(args, "apply", False):
        raise CliError("--dry-run 与 --apply 互斥：要么只报告，要么真清理")
    store = _leases(context, max_age=getattr(args, "max_age", None))
    try:
        report = store.reap_stale(dry_run=dry_run)
    except LeaseError as exc:
        raise CliError(str(exc)) from exc

    mode = "只报告（dry-run）" if report.dry_run else "已清理"
    context.out(f"会话租约回收 · {mode}")
    context.out()
    context.table(
        ("会话", "pid", "判定", "心跳距今"),
        [
            (
                _short(item.session_id),
                str(item.pid),
                item.kind + ("（心跳过期）" if item.expired else ""),
                f"{item.age:.0f}s",
            )
            for item in report.stale
        ],
    )
    if not report.stale:
        context.out("（没有失效租约）")
    else:
        context.out()
        context.out(
            f"失效 {len(report.stale)} 条"
            + (f"；已清 {report.removed} 条登记" if not report.dry_run else "；未改动任何文件")
        )
    context.out("说明：本命令只清登记，绝不 kill 进程（判据基于进程创建时间，属尽力而为）。")
    context.emit({"ok": True, **report.as_dict()})
    return 0


# ── 提示词内容寻址：读侧入口 ────────────────────────────────────────────────


def run_prompt(context: Context, args) -> int:
    """``sessions prompt <任务ID>``：取回该任务当时冻结的系统提示（含 hash 校验）。

    这条命令不在 Hermes 的命令面里（那边的 ``system_prompts`` 只被内部读取），
    但"当时模型到底看到了什么"必须能被人直接查——否则内容寻址只是省了磁盘，
    没换来可追溯。
    """
    task_id = str(getattr(args, "task_id", "") or "").strip()
    if not task_id:
        raise CliError("用法：orbit sessions prompt <任务ID>（先用 PromptStore.bind 冻结过）")
    store = _prompts(context)
    try:
        digest = store.hash_for(task_id)
        if not digest:
            raise CliError(f"任务没有冻结过系统提示：{task_id}")
        text = store.read(digest)
    except ArchiveError as exc:
        raise CliError(str(exc)) from exc
    context.out(f"任务 {task_id} 的系统提示（sha256 {digest[:16]}…，{len(text)} 字符）")
    context.out()
    context.out(text)
    context.emit({"ok": True, "task_id": task_id, "hash": digest, "content": text})
    return 0


# ── 接线 ────────────────────────────────────────────────────────────────────


def _bind(path: str, handler: Any) -> None:
    """把实现登记到**声明表里已有的**命令路径上（与 ``sessions.py`` 的 ``_bind`` 同理）。

    两步都要做：``register_handler`` 供"表还没组装"的导入顺序使用；回填供"表已经
    组装完"的顺序使用。少任何一步，都会有一半场景跑在"计划波次 W8"的占位实现上。
    """
    register_handler(path, handler)
    head, _, leaf = path.partition(" ")
    parent = REGISTRY.get(head)
    target = parent.child(leaf) if (parent is not None and leaf) else parent
    if target is not None and not target.subcommands:
        target.handler = handler
        target.state = "done"


def _attach(
    name: str,
    handler: Any,
    *,
    help_text: str,
    options: list[Option],
    needs_store: bool = False,
) -> Command:
    """把一条**声明表里没有**的子命令补到 ``sessions`` 下面。

    为什么需要这一步：``register_handler`` 只影响 ``register_specs`` 的组装过程，
    而表里没有这几个名字，登记进去没人组装。所以直接在已登记的 ``sessions`` 节点上
    追加 ``Command``——``build_parser`` 每次都从 ``REGISTRY`` 重新编译，追加即时生效。
    """
    parent = REGISTRY.get("sessions")
    if parent is None:  # pragma: no cover - 模块顶部已强制导入声明表，走到这里说明注册表被清过
        raise RuntimeError("sessions 命令未登记：session_extra 必须在声明表之后导入")
    existing = parent.child(name)
    if existing is not None:
        existing.handler = handler
        existing.state = "done"
        return existing
    command = Command(
        name=name,
        help=help_text,
        handler=handler,
        options=options,
        group="ops",
        parity="P1",
        state="done",
        # 纯读或只写平面日志：不 prepare()（不建目录），库在不在都能跑。
        needs_settings=False,
        needs_store=needs_store,
    )
    parent.subcommands.append(command)
    return command


def _session_option() -> Option:
    return Option(("session_id",), "会话 ID", metavar="SESSION_ID")


_bind("sessions archive", run_archive)
# 声明表里的 ``sessions archive`` 没有位置参数（Hermes 是按条件批量归档）。补一个
# **可选** id：给 id 就只归档它。位置参数必须排在选项之后声明，argparse 才认。
_archive_command = REGISTRY.get("sessions")
_archive_child = _archive_command.child("archive") if _archive_command is not None else None
if _archive_child is not None:
    _archive_child.options.append(
        Option(
            ("session_id",),
            "只归档这一个会话（省略则按过滤条件批量归档）",
            metavar="SESSION_ID",
            nargs="?",
        )
    )

_attach(
    "unarchive",
    run_unarchive,
    help_text="取消归档标记（消息一条不删）",
    options=[
        _session_option(),
        Option(("--reason",), "记一笔取消归档的原因（写进平面事件）", metavar="REASON"),
        _json_option(),
    ],
)
_attach(
    "archived",
    run_archived,
    help_text="列出已归档会话（归档只打标记，不删消息）",
    options=[
        Option(("--limit",), "最多列出多少条（默认 50，0 = 全部）", metavar="LIMIT"),
        _json_option(),
    ],
)
_attach(
    "reindex",
    run_reindex,
    help_text="把 messages 同步进 fts.sqlite（双 FTS5 索引，--rebuild 重建）",
    options=[
        Option(("--rebuild",), "丢掉索引库重建（清掉已删消息的残影）", action="store_true"),
        Option(("--batch",), "单批行数（默认 1000）", metavar="BATCH"),
        _json_option(),
    ],
    needs_store=True,
)
_attach(
    "search",
    run_search,
    help_text="走双 FTS 索引检索（含 CJK 走 trigram，纯 ASCII 走 unicode61）",
    options=[
        Option(("query",), "关键词；可给多个词，全部命中才算命中", metavar="QUERY", nargs="+"),
        Option(("--limit",), "返回条数（夹取 [1,100]，默认 10）", metavar="LIMIT"),
        _json_option(),
    ],
)
_attach(
    "leases",
    run_leases,
    help_text="查看存活租约（runtime/active_sessions.json）",
    options=[
        Option(("--max-age",), f"心跳超时秒数（默认 {int(DEFAULT_MAX_AGE)}）", metavar="SECONDS", dest="max_age"),
        _json_option(),
    ],
)
_attach(
    "reap",
    run_reap,
    help_text="清掉失效的会话租约（默认只报告，--apply 才清；绝不 kill 进程）",
    options=[
        Option(("--max-age",), f"心跳超时秒数（默认 {int(DEFAULT_MAX_AGE)}）", metavar="SECONDS", dest="max_age"),
        Option(("--dry-run",), "只报告不动登记簿（默认行为）", action="store_true"),
        Option(("--apply",), "真的清理登记（与 --dry-run 互斥）", action="store_true"),
        _json_option(),
    ],
)
_attach(
    "prompt",
    run_prompt,
    help_text="按任务 ID 取回当时冻结的系统提示（sha256 校验）",
    options=[
        Option(("task_id",), "任务 ID", metavar="TASK_ID"),
        _json_option(),
    ],
)

__all__ = [
    "run_archive",
    "run_archived",
    "run_leases",
    "run_prompt",
    "run_reap",
    "run_reindex",
    "run_search",
    "run_unarchive",
]
