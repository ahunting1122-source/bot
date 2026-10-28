"""`orbit memory` 的记忆层深水区命令（W6）：providers / batch / graph / consolidate / breaker。

接线方式照仓库既有约定：**表不动，实现方声明"这条路径我接了"**（`register_handler`）。

- 表里**已有**的路径（`memory setup` / `status` / `off` / `reset`、`journey list`）
  直接接管：`_bind` 负责两种导入顺序都能成立（表还没组装 → 登记进 `HANDLERS`；
  表已组装 → 回填 handler 与状态）。
- 表里**没有**的子命令（`memory providers` / `batch` / `graph` / `consolidate` / `breaker`）
  挂到已存在的 `memory` 父节点上，**不另立顶层命令**。做法与
  `delegate.mount_worktree_subcommands()` 完全一致（同样的组装钩子 + 幂等挂载）：
  父节点 `memory` 由声明表建立，本模块在表组装完成后往里追加子命令。

为什么这些子命令值得存在：Hermes 的记忆治理面是 `memory reset` + `journey`
（星图 list/delete/edit）。Orbit 的记忆多了 provider 路由、原子批处理、熔断和图谱，
如果只把它们留在 Python API 里，用户就只能靠读源码来治理自己的记忆——那不是治理面。
"""

from __future__ import annotations

import json
import logging
from typing import Any

from app import approvals, memory_write
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Handler, Option
from app.memory_batch import (
    ConsolidationBreaker,
    SqlMemoryBackend,
    apply_batch,
    consolidate,
    plan_batch,
)
from app.memory_graph import build as build_graph
from app.memory_graph import neighbors as graph_neighbors
from app.memory_graph import summary as graph_summary
from app.memory_prompt import MEMORY_MAX_CHARS, render
from app.memory_providers import (
    BUILTIN_NAMES,
    PROVIDER_NAMES,
    build_router,
    load_config,
    save_config,
)

log = logging.getLogger("orbit.cli.memory")


# ── 公共小工具 ───────────────────────────────────────────────────────────────


def _maybe_store(context: Context) -> Any:
    """拿 Store；数据库不存在时返回 None（`memory providers` 之类只读命令不该因此报错）。"""
    try:
        return context.store
    except CliError:
        return None


def _entries(context: Context) -> dict[str, str]:
    """当前全部记忆（key → value）。没有数据库时给可操作的报错。"""
    store = _maybe_store(context)
    if store is None:
        raise CliError(f"数据库不存在：{context.db_path}（先运行 `orbit setup` 或启动一次后端）")
    return context.run(SqlMemoryBackend(store).all_entries())


def _router(context: Context):
    return build_router(settings=context.settings, store=_maybe_store(context))


def _read_operations(context: Context, args: Any) -> list[dict]:
    """从 `--ops`（内联 JSON）或 `--file`（文件）读批处理操作。"""
    raw = getattr(args, "ops", None)
    path = getattr(args, "file", None)
    if raw and path:
        raise CliError("--ops 与 --file 只能给一个")
    if path:
        try:
            with open(path, encoding="utf-8") as handle:
                raw = handle.read()
        except OSError as exc:
            raise CliError(f"读不了操作文件 {path}：{exc}") from exc
    if not raw:
        raise CliError("缺少操作：用 --ops '<JSON>' 或 --file <路径> 给一个 operations 列表")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise CliError(f"operations 不是合法 JSON：{exc}") from exc
    # 两种常见外壳都认：裸列表，或 {"operations": [...]}
    if isinstance(data, dict) and isinstance(data.get("operations"), list):
        data = data["operations"]
    if not isinstance(data, list):
        raise CliError("operations 必须是列表（形如 [{\"op\":\"add\",\"key\":...,\"value\":...}]）")
    return data


# ── memory status / setup / off / reset ──────────────────────────────────────


def run_memory_status(context: Context, args: Any) -> int:
    """`orbit memory status`：供应商路由 + 记忆预算用量 + 熔断状态，一次看全。"""
    router = _router(context)
    status = router.status()
    entries = _entries(context) if _maybe_store(context) is not None else {}
    block = render(list(entries.items()))
    breaker = ConsolidationBreaker(context.settings.data_dir)

    context.out("记忆供应商：")
    context.table(
        ["名称", "类型", "可用", "后端"],
        [
            [
                str(row["name"]),
                "当前" if row.get("active") else str(row.get("kind") or ""),
                "是" if row.get("available") else "否",
                str(row.get("backend") or ""),
            ]
            for row in status["providers"]
        ],
    )
    context.out("")
    context.out(
        f"当前读取：{status['active'] or '（无可用供应商）'}"
        f"　多写：{'开' if status['fanout'] else '关'}"
        f"　优先级：{' > '.join(status['priority'])}"
    )
    context.out(
        f"记忆预算：{block.used}/{block.limit} 字符（{block.percent}%，{len(entries)} 条）"
    )
    state = breaker.state()
    context.out(
        f"consolidation 熔断：{'已熔断' if state['tripped'] else '未熔断'}"
        f"（本轮失败 {state['failures']}/{state['max_failures']} 次）"
    )
    context.emit({"ok": True, "router": status, "memory": block.__dict__, "breaker": state})
    return 0


def run_memory_setup(context: Context, args: Any) -> int:
    """`orbit memory setup [provider]`：设置读取优先级（第一个可用的就是当前供应商）。"""
    router = _router(context)
    chosen = str(getattr(args, "provider", "") or "").strip()
    if not chosen:
        context.out("可选的记忆供应商（当前可用标 ✔）：")
        context.table(
            ["名称", "可用", "安装提示"],
            [
                [
                    str(row["name"]),
                    "✔" if row.get("available") else "-",
                    str(row.get("install") or row.get("note") or ""),
                ]
                for row in router.describe()
            ],
        )
        context.out("")
        context.out("用法：orbit memory setup <provider>")
        context.emit({"ok": True, "providers": router.describe(), "active": router.status()["active"]})
        return 0

    if chosen not in PROVIDER_NAMES:
        raise CliError(f"未知的记忆供应商：{chosen}（可选：{', '.join(PROVIDER_NAMES)}）")
    # 把选中的排到最前，其余保持原顺序：换供应商不该丢掉其它已配置的优先级。
    current = [name for name in load_config(context.settings.data_dir)["priority"] if name != chosen]
    priority = [chosen, *current]
    save_config(context.settings.data_dir, priority=priority)
    available = {row["name"]: bool(row.get("available")) for row in router.describe()}
    if not available.get(chosen, False):
        info: dict = next((row for row in router.describe() if row["name"] == chosen), {})
        context.warn(
            f"{chosen} 当前不可用（{info.get('reason') or '未知原因'}）。"
            f"优先级已保存，实际读取会落到下一个可用供应商。"
        )
    context.out(f"已把 {chosen} 排到记忆供应商首位：{' > '.join(priority)}")
    context.emit({"ok": True, "priority": priority, "provider": chosen})
    return 0


def run_memory_off(context: Context, args: Any) -> int:
    """`orbit memory off`：关掉外部供应商，只用内置的三种（local / file / jsonl）。"""
    config = save_config(context.settings.data_dir, priority=list(BUILTIN_NAMES), fanout=False)
    context.out(f"已关闭外部供应商，仅用内置：{' > '.join(config['priority'])}（多写已关）")
    context.emit({"ok": True, **config})
    return 0


def run_memory_reset(context: Context, args: Any) -> int:
    """`orbit memory reset`：清空记忆。

    Hermes 的 `--target` 分 `memory` / `user` 两个 store；Orbit 目前**只有一个**
    `memories` 表（用户画像走 `data/profile/USER.md`，是另一条链路），
    所以两个 target 都落到这张表，并在输出里如实说明——假装支持两个 store 才是错的。
    """
    target = str(getattr(args, "target", "all") or "all")
    confirmed = bool(getattr(args, "yes", False))
    entries = _entries(context)
    if not entries:
        context.out("没有记忆需要清空。")
        context.emit({"ok": True, "removed": 0, "target": target})
        return 0
    if not confirmed:
        raise CliError(
            f"这会删除 {len(entries)} 条记忆，不可恢复。确认请加 --yes。", code=1
        )
    store = _maybe_store(context)
    assert store is not None  # _entries 已经验证过
    context.run(SqlMemoryBackend(store).commit([(key, None) for key in entries]))
    note = ""
    if target in ("all", "user"):
        note = "（Orbit 只有一个 memories 表；用户画像在 data/profile/USER.md，本次不动）"
    context.out(f"已清空 {len(entries)} 条记忆{note}")
    context.emit({"ok": True, "removed": len(entries), "target": target})
    return 0


# ── 挂上去的新子命令 ─────────────────────────────────────────────────────────


def run_memory_providers(context: Context, args: Any) -> int:
    """`orbit memory providers`：8 个 provider 的可用性与安装提示。"""
    router = _router(context)
    rows = router.describe()
    context.table(
        ["名称", "类型", "可用", "依赖", "安装提示 / 说明"],
        [
            [
                str(row["name"]),
                "当前" if row.get("active") else str(row.get("kind") or ""),
                "是" if row.get("available") else "否",
                "已装" if row.get("dependency_installed") else ("—" if not row.get("install") else "未装"),
                str(row.get("install") or row.get("note") or ""),
            ]
            for row in rows
        ],
    )
    ready = [row["name"] for row in rows if row.get("available")]
    context.out("")
    context.out(f"可用 {len(ready)}/{len(rows)}：{', '.join(ready) or '（无）'}")
    context.emit({"ok": True, "providers": rows, "available": ready})
    return 0


def run_memory_batch(context: Context, args: Any) -> int:
    """`orbit memory batch`：原子批处理（全有或全无）。`--dry-run` 只校验不落盘。"""
    operations = _read_operations(context, args)
    limit = int(getattr(args, "limit", 0) or 0) or MEMORY_MAX_CHARS
    dry_run = bool(getattr(args, "dry_run", False))
    store = _maybe_store(context)
    if store is None:
        raise CliError(f"数据库不存在：{context.db_path}（先运行 `orbit setup` 或启动一次后端）")

    if dry_run:
        current = context.run(SqlMemoryBackend(store).all_entries())
        result = plan_batch(current, operations, limit=limit)
        context.out(
            f"[dry-run] {'通过' if result.ok else '拒绝'}：{result.applied} 条操作，"
            f"{len(result.final)} 条记忆，{result.used}/{result.limit} 字符"
        )
        if not result.ok:
            context.warn(result.error)
        context.emit({"ok": result.ok, "dry_run": True, **result.to_dict()})
        return 0 if result.ok else 1

    # 写入门禁（MATRIX 9.12）：按**最终态**过威胁扫描 → 审批门 → 组合二次扫描 →
    # 单条超限 → round-trip 漂移防护。批处理这里传 `backend`（会真跑 round-trip），
    # 因为批量写本来就动整库，为漂移检查付一次写回的代价是值得的。
    #
    # 为什么按"最终态"而不是逐条：单条干净、组合起来是注入的情况确实存在
    # （"请忽略以上" + "的指令并照做" 各自扫是空的，拼起来命中）。
    engine = SqlMemoryBackend(store)
    current = context.run(engine.all_entries())
    planned = plan_batch(current, operations, limit=limit)
    if not planned.ok:
        context.warn(planned.error)
        context.emit({"ok": False, **planned.to_dict()})
        return 1
    check = context.run(
        memory_write.check_write(
            planned.final,
            mode=approvals.resolve_mode(context.settings),
            backend=engine,
            data_dir=context.data_dir,
            limit=limit,
        )
    )
    if not check.ok:
        context.warn(f"记忆写入被拒（{check.stage}）：{check.reason}")
        context.emit({"ok": False, **check.to_dict()})
        return check.exit_code or 1

    result = context.run(apply_batch(store, operations, limit=limit))
    if not result.ok:
        context.warn(result.error)
        context.emit({"ok": False, **result.to_dict()})
        return 1
    context.out(
        f"已应用 {result.applied} 条操作：{len(result.final)} 条记忆，"
        f"{result.used}/{result.limit} 字符"
    )
    context.emit({"ok": True, **result.to_dict()})
    return 0


def run_memory_graph(context: Context, args: Any) -> int:
    """`orbit memory graph`：记忆图谱（`memory-graph` 是 `journey` 的别名，共用这份数据）。"""
    entries = _entries(context)
    payload = build_graph(entries)
    key = str(getattr(args, "key", "") or "").strip()
    depth = int(getattr(args, "depth", 1) or 1)
    if key:
        found = graph_neighbors(key, depth, payload=payload)
        if not found:
            raise CliError(f"记忆里没有「{key}」，或者它在图里没有邻居（用 orbit memory graph 看节点）")
        context.out(f"「{key}」的邻居（depth={depth}）：")
        context.table(
            ["节点", "距离", "关系"],
            [[row["id"], str(row["distance"]), row["via"]] for row in found],
        )
        context.emit({"ok": True, "key": key, "depth": depth, "neighbors": found})
        return 0

    stats = graph_summary(entries)
    context.table(
        ["节点", "前缀", "度数", "关系", "字符"],
        [
            [
                str(node["id"]),
                str(node["prefix"]),
                str(node["degree"]),
                ",".join(node["kinds"]) or "-",
                str(node["chars"]),
            ]
            for node in payload["nodes"]
        ],
    )
    context.out("")
    context.out(
        f"{stats['nodes']} 个节点 / {stats['edges']} 条边"
        f"（references {stats['by_kind']['references']}、"
        f"same_prefix {stats['by_kind']['same_prefix']}、"
        f"temporal {stats['by_kind']['temporal']}）"
    )
    if stats["isolated"]:
        context.note(f"孤立节点 {len(stats['isolated'])} 个：{', '.join(stats['isolated'][:5])}…")
    context.emit({"ok": True, **payload, "summary": stats})
    return 0


def run_memory_consolidate(context: Context, args: Any) -> int:
    """`orbit memory consolidate`：把超预算的记忆合并/裁剪到预算内。"""
    entries = _entries(context)
    pairs = list(entries.items())
    limit = int(getattr(args, "limit", 0) or 0) or MEMORY_MAX_CHARS
    result = consolidate(pairs, limit=limit)
    if bool(getattr(args, "dry_run", False)):
        context.out(
            f"[dry-run] 保留 {len(result.kept)} 条、合并 {result.merged} 条、"
            f"丢弃 {result.dropped} 条；整理后 {result.used}/{result.limit} 字符"
        )
        context.emit({"ok": True, "dry_run": True, **result.to_dict()})
        return 0
    if not result.merged and not result.dropped:
        context.out(f"记忆在预算内（{result.used}/{result.limit} 字符），无需整理。")
        context.emit({"ok": True, **result.to_dict()})
        return 0
    store = _maybe_store(context)
    if store is None:
        raise CliError(f"数据库不存在：{context.db_path}")
    kept = dict(result.kept)
    mutations: list[tuple[str, str | None]] = [(key, value) for key, value in kept.items()]
    mutations += [(key, None) for key in entries if key not in kept]
    context.run(SqlMemoryBackend(store).commit(mutations))
    context.out(
        f"已整理：保留 {len(result.kept)} 条、合并 {result.merged} 条、丢弃 {result.dropped} 条；"
        f"{result.used}/{result.limit} 字符"
    )
    context.emit({"ok": True, **result.to_dict()})
    return 0


def run_memory_breaker(context: Context, args: Any) -> int:
    """`orbit memory breaker`：看/复位 consolidation 熔断状态。"""
    breaker = ConsolidationBreaker(context.settings.data_dir)
    if bool(getattr(args, "reset", False)):
        turn = str(getattr(args, "turn", "") or "")
        state = breaker.reset(turn)
        context.out(f"已复位熔断状态（turn={turn or '（空）'}）")
        context.emit({"ok": True, "reset": True, "breaker": state})
        return 0
    state = breaker.state(str(getattr(args, "turn", "") or ""))
    context.out(f"状态文件：{state['path'] or '（未落盘：纯内存）'}")
    context.out(f"轮次：{state['turn'] or '（空）'}")
    context.out(f"本轮失败：{state['failures']}/{state['max_failures']}")
    context.out(f"熔断：{'已打开（本轮不再尝试 consolidation）' if state['tripped'] else '未打开'}")
    context.emit({"ok": True, "breaker": state})
    return 0


def run_journey_list(context: Context, args: Any) -> int:
    """`journey list`（`memory-graph` 的别名）：记忆节点清单。

    Hermes 的 `journey` 是技能 + 记忆的时间轴星图。本模块只接**记忆那一半**——
    技能那一半属于技能层，不由这里代管。
    """
    entries = _entries(context)
    payload = build_graph(entries)
    if not payload["nodes"]:
        context.out("还没有记忆。（journey 的记忆节点来自 `memories` 表）")
        context.emit({"ok": True, "nodes": [], "edges": []})
        return 0
    context.table(
        ["节点 id", "前缀", "度数"],
        [[node["id"], node["prefix"], str(node["degree"])] for node in payload["nodes"]],
    )
    context.out("")
    context.out(f"{len(payload['nodes'])} 个记忆节点 / {len(payload['edges'])} 条边")
    context.emit({"ok": True, **payload})
    return 0


#: 声明表里**没有**的 `memory` 子命令：`(名字, 中文说明, 选项, handler)`。
#:
#: 为什么补：Hermes 的 `memory` 只有 setup/status/off/reset，记忆的"深水区"能力
#: （provider 路由、原子批处理、熔断、图谱、整理）在 Hermes 里全藏在工具与插件内部，
#: 没有可脚本化的入口。Orbit 既然把这些能力做成了独立模块，就该有对应的治理命令。
EXTRA_MEMORY_SUBCOMMANDS: tuple[tuple[str, str, tuple[Option, ...], Handler], ...] = (
    (
        "providers",
        "列出 8 个记忆供应商的可用性与安装提示",
        (),
        run_memory_providers,
    ),
    (
        "batch",
        "原子批处理记忆（add/update/delete 混合，全有或全无）",
        (
            Option(("--ops",), "内联 JSON 操作列表", metavar="JSON"),
            Option(("--file",), "从文件读操作列表（JSON）", metavar="PATH"),
            Option(("--limit",), "字符预算（默认 2200）", default=0, metavar="N"),
            Option(("--dry-run",), "只校验，不落盘", action="store_true"),
        ),
        run_memory_batch,
    ),
    (
        "graph",
        "记忆图谱：节点/边，或某个节点的邻居（BFS）",
        (
            Option(("--key",), "只看这个记忆节点的邻居", metavar="KEY"),
            Option(("--depth",), "BFS 深度（默认 1）", default=1, metavar="N"),
        ),
        run_memory_graph,
    ),
    (
        "consolidate",
        "把超预算的记忆合并/裁剪到预算内",
        (
            Option(("--limit",), "字符预算（默认 2200）", default=0, metavar="N"),
            Option(("--dry-run",), "只报不写", action="store_true"),
        ),
        run_memory_consolidate,
    ),
    (
        "breaker",
        "看/复位 consolidation 熔断状态（一轮 3 次失败即熔断）",
        (
            Option(("--reset",), "复位计数与熔断标记", action="store_true"),
            Option(("--turn",), "轮次标识（默认取当前存的状态）", default="", metavar="TURN"),
        ),
        run_memory_breaker,
    ),
)

#: 本模块已经处理过的路径（`_bind` 幂等用）。
_BOUND: set[str] = set()


def _bind(path: str, handler: Handler) -> None:
    """把实现接到声明表的某条路径上（两种导入顺序都要成立）。

    与 `delegate.py::_bind` 同一套做法：`register_handler` 负责"表还没组装"的情况，
    回填负责"表已经组装完"的情况（`import app.cli.commands.memory_extra` 会先跑包
    初始化，那时表可能已经建好，`HANDLERS` 里的登记不会自动回填）。
    """
    key = " ".join(path.split())
    if key not in _BOUND:
        try:
            register_handler(key, handler)
        except ValueError:
            # 已经有人接了这条路径。`journey list` 是记忆图谱与技能时间线共用的入口，
            # 两边都可能接管——重复登记不该让整个 CLI 导入失败（那会连带 74 条命令一起挂）。
            log.warning("命令路径 %s 已被其它模块登记，本模块不接管", key)
            _BOUND.add(key)
            return
        _BOUND.add(key)
    parts = key.split()
    node = REGISTRY.get(parts[0])
    for part in parts[1:]:
        if node is None:
            return
        node = node.child(part)
    if node is not None and not node.subcommands:
        node.handler = handler
        node.state = "done"


def mount_memory_subcommands() -> list[str]:
    """把 `EXTRA_MEMORY_SUBCOMMANDS` 挂到已存在的 `memory` 上。**幂等**。"""
    parent = REGISTRY.get("memory")
    if parent is None:
        return []
    mounted: list[str] = []
    for name, help_text, options, handler in EXTRA_MEMORY_SUBCOMMANDS:
        if parent.child(name) is not None:
            continue
        parent.subcommands.append(
            Command(
                name=name,
                help=help_text,
                handler=handler,
                group="core",
                state="done",
                options=list(options),
            )
        )
        mounted.append(name)
    # 父节点的状态要重算：声明表组装时它的子命令还全是 todo，现在有 done 的了。
    states = {child.state for child in parent.subcommands}
    if states == {"done"}:
        parent.state = "done"
    elif states & {"done", "partial"}:
        parent.state = "partial"
    return mounted


#: 组装钩子是否已装过（幂等标记）。理由同 `delegate.py`：本模块若被排在
#: `hermes_parity` **之前**导入，`REGISTRY.get("memory")` 还是 None，只能等表组装完再补挂。
_ASSEMBLY_HOOK_INSTALLED = False


def _install_assembly_hook() -> bool:
    from app.cli.commands import _table as table_module

    global _ASSEMBLY_HOOK_INSTALLED
    if _ASSEMBLY_HOOK_INSTALLED:
        return False
    original = table_module.register_specs

    def register_specs(specs: Any) -> list[str]:
        skipped = original(specs)
        mount_memory_subcommands()
        return skipped

    table_module.register_specs = register_specs
    _ASSEMBLY_HOOK_INSTALLED = True
    return True


# 表里已有的路径：直接接管（`memory setup` 等 4 条 + journey 的记忆清单）。
_bind("memory setup", run_memory_setup)
_bind("memory status", run_memory_status)
_bind("memory off", run_memory_off)
_bind("memory reset", run_memory_reset)
_bind("journey list", run_journey_list)

# 表还没组装（本模块排在 `hermes_parity` 之前）→ 装组装钩子；已组装 → 立即挂上。
if REGISTRY.get("memory") is None:
    _install_assembly_hook()
mount_memory_subcommands()
