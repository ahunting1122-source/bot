"""``orbit sessions shadow …`` 与 ``orbit plane backfill|compact-check`` 的真实现。

补 MATRIX L10 的最后两条可落地入口：

- **10.9 影子仓库**：``sessions shadow {init,snapshot,restore,log,diff}``——实现在
  ``app/shadow_checkpoint.py``（直接调 git CLI，不引 gitpython）；
- **10.1 写侧统一**：``plane backfill``——把历史 ``task_events`` 一次性补进平面；
- **10.4 压缩阈值**：``plane compact-check``——把阈值/目标/与 ``nodes.py`` 的口径差异
  摊开，并可选地拿某个会话的真实字符数算一遍。

## 接线方式：接"声明表里没有的路径"

``app/cli/commands/_table.py`` 的约定是"表不动，实现方声明路径"，但 ``shadow`` /
``backfill`` / ``compact-check`` 这三条 Hermes 都没有（``shadow`` 是 Hermes 内部的
快照机制，没有对应用户命令），所以：

- 先 ``register_handler(path, handler)``：将来声明表里若真的补上同名路径，它自动
  接管，不需要再改这里；
- 再把 ``Command`` 节点补到**已登记**的 ``sessions`` / ``plane`` 下面
  （``build_parser`` 每次从 ``REGISTRY`` 重新编译，追加即时生效）。

``plane`` 由 ``app/cli/commands/session_plane.py`` 登记，所以本模块顶部**显式导入它**
（幂等），避免"导入顺序不同 → ``REGISTRY.get('plane')`` 是 ``None`` → 静默少两条命令"。
静默少命令是最难发现的一类故障。

## 三条刻意的语义分歧

1. **``sessions shadow`` 而不是顶层 ``shadow``**：它是"会话/工作目录的持久化"，
   不是独立功能域；塞进 ``sessions`` 也避免给对标矩阵凭空加一条 Hermes 没有的顶层命令。
2. **``restore`` 默认 ``--dry-run`` 关闭、但必做安全快照**：``--dry-run`` 是可选的，
   "回滚前先快照当前状态"不是（见 ``ShadowRepo.restore``）。危险动作的护栏不该靠
   用户记得加参数。
3. **``backfill`` 没有 ``--apply`` 门禁**：它只往平面**追加**幂等事件，一条消息、
   一个任务都不改；重复跑等于重复确认（幂等索引会让第二次全部 skipped）。
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from app.cli.commands import session_plane as _session_plane_cli  # noqa: F401 —— 保证 plane 已登记
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context, truncate
from app.cli.spec import REGISTRY, Command, Option
from app.session_plane import (
    COMPACTION_TARGET_RATIO,
    COMPACTION_THRESHOLD,
    PlaneSink,
    SessionPlane,
    SessionPlaneError,
    compaction_target,
    describe,
    maybe_compact,
    tokens_for_chars,
)
from app.shadow_checkpoint import GitUnavailable, ShadowError, ShadowRepo
from app.window import window_of

# ── 小工具 ──────────────────────────────────────────────────────────────────


def _plane(context: Context) -> SessionPlane:
    return SessionPlane(context.data_dir)


def _shadow(context: Context, args) -> ShadowRepo:
    """``--root`` 缺省取 ``settings.workspace``（Orbit 唯一的工作目录）。

    默认值刻意是工作目录而不是 ``cwd``：在任意目录下跑 CLI 都会得到同一个影子仓库，
    而不是"我在哪跑就在哪建仓库"——后者会让快照历史随 shell 的 cwd 分裂。
    """
    raw = str(getattr(args, "root", "") or "").strip()
    root = Path(raw) if raw else Path(context.settings.workspace)
    return ShadowRepo(root, data_dir=context.data_dir)


def _int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _json_option() -> Option:
    """子命令上的 ``--json``（``default=SUPPRESS`` 的理由见 ``sessions.py`` 的同名函数）。"""
    return Option(
        ("--json",), "以 JSON 输出（脚本友好）", action="store_true", default=argparse.SUPPRESS
    )


def _root_option() -> Option:
    return Option(
        ("--root",),
        "工作目录（默认 settings.workspace；影子仓库永远建在 <data>/shadow/ 下）",
        metavar="PATH",
    )


# ── sessions shadow ─────────────────────────────────────────────────────────


def run_shadow_init(context: Context, args) -> int:
    """``sessions shadow init``：建影子仓库（幂等，绝不碰用户的 ``.git``）。"""
    repo = _shadow(context, args)
    try:
        path = repo.init()
    except (GitUnavailable, ShadowError) as exc:
        raise CliError(str(exc)) from exc
    context.out(f"影子仓库已就绪：{path}")
    context.out(f"工作目录：{repo.root}")
    context.out("说明：GIT_DIR 指向上面那个目录，工作目录里真正的 .git 从头到尾没被碰过。")
    context.emit({"ok": True, "shadow_dir": str(path), "root": str(repo.root)})
    return 0


def run_shadow_snapshot(context: Context, args) -> int:
    """``sessions shadow snapshot [-m 说明]``：把工作目录当前状态提交成一次快照。"""
    repo = _shadow(context, args)
    message = str(getattr(args, "message", "") or "").strip()
    try:
        snapshot = repo.snapshot(message)
    except (GitUnavailable, ShadowError) as exc:
        raise CliError(str(exc)) from exc
    if snapshot.created:
        context.out(f"已快照 {snapshot.sha[:12]}：{snapshot.message}")
    else:
        context.out(f"没有改动，当前快照仍是 {snapshot.sha[:12]}（不产生空提交）")
    context.out(f"影子仓库：{repo.shadow_dir}")
    context.emit({"ok": True, **snapshot.as_dict(), "shadow_dir": str(repo.shadow_dir)})
    return 0


def run_shadow_restore(context: Context, args) -> int:
    """``sessions shadow restore <sha> [--dry-run]``：回滚工作目录（先自动快照当前状态）。"""
    repo = _shadow(context, args)
    sha = str(getattr(args, "sha", "") or "").strip()
    dry_run = bool(getattr(args, "dry_run", False))
    try:
        payload = repo.restore(sha, dry_run=dry_run)
    except (GitUnavailable, ShadowError) as exc:
        raise CliError(str(exc)) from exc
    context.out(f"目标快照：{payload['sha'][:12]}")
    context.out(f"安全快照：{payload['safety_sha'][:12]}（回滚前的工作目录已存下）")
    if payload["changed"]:
        context.out(f"涉及 {len(payload['changed'])} 个文件（?? = 快照里没有，回滚后会消失）：")
        context.table(
            ("状态", "文件"),
            [(item["status"], truncate(item["path"], 70)) for item in payload["changed"]],
        )
    else:
        context.out("工作目录与该快照一致，无需改动。")
    context.out(
        "dry-run：工作目录一个字节都没动。"
        if dry_run
        else "已回滚（工作目录现在就是那个快照的样子；刚才的状态在安全快照里）"
    )
    context.emit({"ok": True, **payload})
    return 0


def run_shadow_log(context: Context, args) -> int:
    """``sessions shadow log``：最近几次快照（新→旧）。"""
    repo = _shadow(context, args)
    limit = _int(getattr(args, "limit", None), 20)
    try:
        snapshots = repo.log(limit)
    except (GitUnavailable, ShadowError) as exc:
        raise CliError(str(exc)) from exc
    context.out(f"快照 {len(snapshots)} 条（{repo.shadow_dir}）")
    context.out()
    context.table(
        ("sha", "时间", "说明"),
        [(item.sha[:12], item.ts, truncate(item.message, 60)) for item in snapshots],
    )
    context.emit({"ok": True, "shadow_dir": str(repo.shadow_dir), "snapshots": [s.as_dict() for s in snapshots]})
    return 0


def run_shadow_diff(context: Context, args) -> int:
    """``sessions shadow diff <sha>``：工作目录相对该快照改了什么。"""
    repo = _shadow(context, args)
    sha = str(getattr(args, "sha", "") or "").strip()
    try:
        payload = repo.diff(sha)
    except (GitUnavailable, ShadowError) as exc:
        raise CliError(str(exc)) from exc
    if payload["empty"]:
        context.out(f"工作目录与快照 {payload['sha'][:12]} 一致。")
    else:
        context.out(f"相对快照 {payload['sha'][:12]} 有 {len(payload['changed'])} 个文件不同：")
        context.table(
            ("状态", "文件"),
            [(item["status"], truncate(item["path"], 70)) for item in payload["changed"]],
        )
        if payload["stat"]:
            context.out()
            context.out(payload["stat"])
    context.emit({"ok": True, **payload})
    return 0


# ── plane backfill / compact-check ──────────────────────────────────────────


def run_backfill(context: Context, args) -> int:
    """``plane backfill``：把历史 ``task_events`` 补进平面（幂等，可重复跑）。"""
    plane = _plane(context)
    sink = PlaneSink(plane, context.store)
    limit = _int(getattr(args, "limit", None), 0)
    try:
        report = context.run(sink.backfill(limit=limit))
    except SessionPlaneError as exc:
        raise CliError(str(exc)) from exc
    context.out(
        f"扫描 {report.scanned} 条 task_events：新写 {report.written} 条，"
        f"已存在跳过 {report.skipped} 条"
    )
    context.out(f"平面日志：{plane.path}（游标 {report.cursor}）")
    if report.written == 0 and report.scanned:
        context.out("说明：一条都没新增，说明之前已经补过（幂等键相同）。")
    context.emit({"ok": True, **report.as_dict(), "path": str(plane.path)})
    return 0


def _session_tokens(context: Context, session_id: str) -> tuple[int, int]:
    """某个会话的 ``(字符数, 消息数)``。库不存在时报可行动的错误。"""
    from sqlalchemy import func, select

    from app.storage import Message

    async def go() -> Any:
        async with context.store.sessions() as session:
            row = (
                await session.execute(
                    select(func.coalesce(func.sum(func.length(Message.content)), 0), func.count(Message.id))
                    .where(Message.conversation_id == session_id)
                )
            ).one()
        return int(row[0] or 0), int(row[1] or 0)

    return context.run(go())


def run_compact_check(context: Context, args) -> int:
    """``plane compact-check``：阈值 0.50 / 目标 20% 的判定，以及和 ``nodes.py`` 的差异。"""
    window = _int(getattr(args, "window", None), 0) or window_of(context.settings)
    session_id = str(getattr(args, "session", "") or "").strip()
    used = _int(getattr(args, "used_tokens", None), -1)

    chars = 0
    messages = 0
    if used < 0:
        if session_id:
            if not context.db_path.exists():
                raise CliError(
                    f"数据库不存在：{context.db_path}（先运行 `orbit setup`，或直接给 --used-tokens）"
                )
            chars, messages = _session_tokens(context, session_id)
            used = tokens_for_chars(chars)
        else:
            used = 0

    plan = maybe_compact(used, window)
    policy = describe(window)
    context.out(f"窗口 {window} token，压缩阈值 {COMPACTION_THRESHOLD:.0%}，目标 {COMPACTION_TARGET_RATIO:.0%}")
    if session_id:
        context.out(f"会话 {session_id}：{messages} 条消息 / {chars} 字符 ≈ {used} token")
    context.out()
    context.out(f"结论：{'该压缩' if plan.needed else '不需要压缩'}")
    context.out(f"理由：{plan.reason}")
    context.out(f"目标：{compaction_target(window)} token（当前余量 {plan.headroom_tokens} token）")
    context.out()
    context.out("与 app/graph/nodes.py 的口径对比：")
    context.out(
        f"  - 目标比：nodes.py 硬编码 {policy['nodes_py']['target_ratio']}"
        f"（{'一致' if policy['nodes_py']['target_matches'] else '不一致'}）"
    )
    context.out(f"  - 触发：{policy['nodes_py']['trigger']}（0.50 的提前判定尚未接线）")
    context.emit({"ok": True, "policy": policy, "plan": plan.as_dict(), "session_id": session_id})
    return 0


# ── 接线 ────────────────────────────────────────────────────────────────────


def _attach(
    parent_name: str,
    name: str,
    handler: Any,
    *,
    help_text: str,
    options: list[Option],
    needs_store: bool = False,
    subcommands: list[Command] | None = None,
) -> Command:
    """把一条（或一组）命令挂到**已登记**的父节点下。已存在则替换 handler、改 done。

    为什么不 ``REGISTRY.add`` 一个新的顶层命令：``sessions`` / ``plane`` 已经在表里，
    再加一个同名顶层会触发"命令重复登记"，整个 CLI 起不来。
    """
    parent = REGISTRY.get(parent_name)
    if parent is None:  # pragma: no cover - 模块顶部已强制导入，走到这里说明注册表被清过
        raise RuntimeError(f"{parent_name} 命令未登记：session_extra2 必须在声明表之后导入")
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
        subcommands=subcommands or [],
        group="ops",
        parity="P1",
        state="done",
        # 影子仓库与平面都是文件操作：不 prepare()（不建目录），库在不在都能跑。
        needs_settings=False,
        needs_store=needs_store,
    )
    parent.subcommands.append(command)
    return command


_SHADOW_SUBCOMMANDS = [
    Command(
        name="init",
        help="建影子 git 仓库（<data>/shadow/<hash>/，绝不碰用户的 .git）",
        handler=run_shadow_init,
        group="ops",
        parity="P1",
        state="done",
        needs_settings=False,
        needs_store=False,
        options=[_root_option(), _json_option()],
    ),
    Command(
        name="snapshot",
        help="把工作目录当前状态提交成一次快照（大目录与 <data> 已排除）",
        handler=run_shadow_snapshot,
        group="ops",
        parity="P1",
        state="done",
        needs_settings=False,
        needs_store=False,
        options=[
            _root_option(),
            Option(("-m", "--message"), "快照说明（默认带时间戳）", metavar="TEXT", dest="message"),
            _json_option(),
        ],
    ),
    Command(
        name="restore",
        help="回滚工作目录到某个快照（先自动快照当前状态；--dry-run 只算不动）",
        handler=run_shadow_restore,
        group="ops",
        parity="P1",
        state="done",
        needs_settings=False,
        needs_store=False,
        options=[
            Option(("sha",), "快照 sha（用 `sessions shadow log` 查）", metavar="SHA"),
            _root_option(),
            Option(("--dry-run",), "只列出会被改动的文件，一个字节都不写", action="store_true"),
            _json_option(),
        ],
    ),
    Command(
        name="log",
        help="列出最近几次快照（新→旧）",
        handler=run_shadow_log,
        group="ops",
        parity="P1",
        state="done",
        needs_settings=False,
        needs_store=False,
        options=[
            _root_option(),
            Option(("--limit",), "最多列几条（默认 20）", metavar="N"),
            _json_option(),
        ],
    ),
    Command(
        name="diff",
        help="工作目录相对某个快照改了什么（--stat + 逐文件状态）",
        handler=run_shadow_diff,
        group="ops",
        parity="P1",
        state="done",
        needs_settings=False,
        needs_store=False,
        options=[
            Option(("sha",), "快照 sha（用 `sessions shadow log` 查）", metavar="SHA"),
            _root_option(),
            _json_option(),
        ],
    ),
]


_attach(
    "sessions",
    "shadow",
    None,
    help_text="工作目录影子仓库：快照 / 回滚（直接调 git CLI，绝不碰用户的 .git）",
    options=[],
    subcommands=_SHADOW_SUBCOMMANDS,
)

_attach(
    "plane",
    "backfill",
    run_backfill,
    help_text="把历史 task_events 补进平面（幂等，可重复跑）",
    options=[
        Option(("--limit",), "本批最多扫描多少行（0 = 全部）", metavar="N"),
        _json_option(),
    ],
    needs_store=True,
)

_attach(
    "plane",
    "compact-check",
    run_compact_check,
    help_text="压缩阈值判定（0.50 触发 / 目标 20%%）并与 nodes.py 的口径对比",
    options=[
        Option(("--window",), "窗口 token 数（默认取 settings 的 context_length）", metavar="TOKENS"),
        Option(("--used-tokens",), "直接给已用 token（省略则按 --session 实测）", metavar="TOKENS"),
        Option(("--session",), "按这个会话的实际字符数估算已用 token", metavar="SESSION_ID"),
        _json_option(),
    ],
    needs_store=False,
)

# 声明表里没有这几条路径，`register_handler` 只是"先声明下来"：将来表里真补上同名
# 路径时，它会自动接管，不需要再改这里（与 `session_extra.py` 的 `_bind` 同一套机制）。
for _path, _handler in (
    ("sessions shadow init", run_shadow_init),
    ("sessions shadow snapshot", run_shadow_snapshot),
    ("sessions shadow restore", run_shadow_restore),
    ("sessions shadow log", run_shadow_log),
    ("sessions shadow diff", run_shadow_diff),
    ("plane backfill", run_backfill),
    ("plane compact-check", run_compact_check),
):
    register_handler(_path, _handler)


__all__ = [
    "run_backfill",
    "run_compact_check",
    "run_shadow_diff",
    "run_shadow_init",
    "run_shadow_log",
    "run_shadow_restore",
    "run_shadow_snapshot",
]
