"""`orbit memory` / `orbit profile` 的第二批接线（W6 收尾）。

分工：本模块**只做接线**，业务逻辑全在
`app/memory_write.py`（写入门禁 + 漂移防护）、`app/holographic.py`（HRR 记忆图）、
`app/profile_cli.py`（14 条画像命令）里。为什么这么切：这三个能力的入口不止 CLI
（HTTP、工具层、将来的 TUI 都会用），把逻辑写在 handler 里就等于把它们锁死在命令行上。

接线方式与 `memory_extra.py` 完全一致（同一套 `register_handler` + 组装钩子 +
幂等挂载），理由见那个模块的 docstring：**表不动，实现方声明"这条路径我接了"**。
本模块避开 `memory_extra.py` 已接管的 5 条路径（`memory setup/status/off/reset`、
`journey list`），也避开 `skill_extra.py` 已接管的 `journey edit` / `journey delete`
（那两条实现的是**技能时间线**的旁注与墓碑，不是记忆节点的删改）。

## 本模块接了什么

- **表里已有、状态还是 `todo`** 的路径（`register_handler` 接管）：
  `profile show` / `profile export` / `profile import` —— 与用户画像语义相容
  （"看画像 / 导出画像 / 导入画像"）。
- **表里没有**的子命令（挂到已存在的父节点上，不另立顶层命令）：
  - `memory write-check` / `memory holographic`；
  - `profile set/add/remove/clear/path/edit/trim/entries/diff/check/budget`（11 条）。
    加上上面接管的 3 条，`orbit profile` 的**用户画像**子命令正好 14 条。

## 刻意**没有**接的 10 条

`profile list/use/create/delete/describe/alias/rename/install/update/info` 抄的是
**Hermes 的 profile distribution**（`hermes profile install <git-url>`，一整套可切换的
发行版配置）。把它实现成"用户画像的增删改"是**抄错语义**，比留 todo 更糟——
留 todo 至少诚实地说明"这块没做"。所以它们保持 `todo` 不动。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app import approvals, holographic, memory_write, profile_cli
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Handler, Option
from app.memory_batch import SqlMemoryBackend, plan_batch
from app.memory_prompt import MEMORY_MAX_CHARS

log = logging.getLogger("orbit.cli.memory_extra2")


# ── 公共小工具 ───────────────────────────────────────────────────────────────


def _maybe_store(context: Context) -> Any:
    """拿 Store；数据库不存在时返回 None（只读命令不该因此报错）。"""
    try:
        return context.store
    except CliError:
        return None


def _require_store(context: Context) -> Any:
    store = _maybe_store(context)
    if store is None:
        raise CliError(f"数据库不存在：{context.db_path}（先运行 `orbit setup` 或启动一次后端）")
    return store


def _entries(context: Context) -> dict[str, str]:
    """当前全部记忆（key → value）。"""
    return context.run(SqlMemoryBackend(_require_store(context)).all_entries())


def _allowlist(context: Context) -> tuple[str, ...]:
    """读 `<data_dir>/approvals.json` 的白名单规则名。

    复用 `approvals.AllowList` 而不是另开一个记忆白名单：`orbit approvals allow add
    memory_write` 是用户已经会用的那条命令，再造一个"记忆专用白名单"等于让他学两遍。
    """
    path = Path(context.settings.data_dir) / "approvals.json"
    return approvals.AllowList.load(path).rules


def _read_operations(context: Context, args: Any) -> list[dict]:
    """从 `--ops`（内联 JSON）或 `--file`（文件）读批处理操作。

    直接复用 `memory_extra.run_memory_batch` 的解析器（延迟导入，避免模块级互相依赖）：
    两个命令收同一种输入，解析规则必须一致，否则同一个 JSON 在两条命令里行为不同。
    本模块的 `write-check` 与那条命令的选项名（`--ops` / `--file`）刻意同名，
    就是为了能共用这一个函数。
    """
    from app.cli.commands.memory_extra import _read_operations as shared

    return shared(context, args)


def _report_write_check(context: Context, result: memory_write.WriteCheck, *, what: str) -> int:
    """把一次写入裁决打成人话 + 结构化载荷。返回退出码。"""
    state = "放行" if result.ok else ("待处置" if result.verdict == memory_write.GATE_STAGED else "拒绝")
    context.out(f"{what}：{state}（阶段 {result.stage or '-'}）")
    if result.reason:
        context.out(f"原因：{result.reason}")
    context.out(f"预算：{result.used}/{result.limit} 字符")
    if result.oversize:
        context.table(
            ["key", "字符", "上限"],
            [[row["key"], str(row["chars"]), str(row["limit"])] for row in result.oversize],
        )
    if result.findings:
        context.table(
            ["范围", "key", "类别", "说明"],
            [
                [
                    str(item.get("scope", "")),
                    str(item.get("key", "") or "-"),
                    str(item.get("kind", "")),
                    str(item.get("detail", "")),
                ]
                for item in result.findings
            ],
        )
    if result.pending:
        context.out(f"待处置文件：{result.pending}")
    context.emit(result.to_dict())
    code = result.exit_code
    if code != 0:
        # `main` 只在**成功**时刷 `--json` 载荷（`if code == 0: context.finish()`），
        # 而拒绝恰恰是非 0 退出。不在这里自己刷一次，"拒绝"在 `--json` 下就什么都不输出，
        # 与"每个拒绝都要能 --json 输出原因"直接冲突。
        context.finish()
    return code


# ── memory write-check ───────────────────────────────────────────────────────


def run_memory_write_check(context: Context, args: Any) -> int:
    """`orbit memory write-check`：写入前的三段门禁 + 漂移防护。

    三种用法：

    1. `--ops '<JSON>'` / `--file <PATH>`：检查"这批操作**全部生效后**的全库"能不能过门禁
       （用 `plan_batch` 先算出最终态——门禁要判的是最终态，不是中间态）；
    2. 什么都不给：检查**当前库**（漂移 + 已有条目的威胁特征），相当于一次体检；
    3. `--apply <文件>`：回放一份 `smart` 模式暂存下来的待处置写入。

    退出码照 `approvals` 的 0/2/3：0 放行 / 2 待处置 / 3 拒绝。
    """
    apply_path = str(getattr(args, "apply", "") or "").strip()
    limit = int(getattr(args, "limit", 0) or 0) or MEMORY_MAX_CHARS
    store = _maybe_store(context)
    backend = SqlMemoryBackend(store) if store is not None else None

    if apply_path:
        if backend is None:
            raise CliError(f"数据库不存在：{context.db_path}")
        try:
            outcome = context.run(memory_write.apply_pending(apply_path, backend))
        except (OSError, ValueError) as exc:
            raise CliError(f"回放待处置写入失败：{exc}") from exc
        if not outcome.get("ok"):
            context.warn(str(outcome.get("reason", "")))
            context.emit(outcome)
            return 1
        context.out(f"已回放 {outcome['applied']} 条待处置写入：{'、'.join(outcome['keys'])}")
        context.emit(outcome)
        return 0

    mode = str(getattr(args, "mode", "") or "").strip() or approvals.resolve_mode(context.settings)
    raw = getattr(args, "ops", None)
    path = getattr(args, "file", None)

    if raw or path:
        if backend is None:
            raise CliError(f"数据库不存在：{context.db_path}")
        operations = _read_operations(context, args)
        current = context.run(backend.all_entries())
        planned = plan_batch(current, operations, limit=limit)
        if not planned.ok:
            context.warn(f"批处理校验未通过（第 {planned.failed_op or '-'} 条）：{planned.error}")
            context.emit({"ok": False, "stage": "batch", "reason": planned.error, **planned.to_dict()})
            context.finish()  # 同上：非 0 退出时 main 不会刷载荷
            return 1
        candidate = planned.final
        touched = sorted(key for key in set(current) | set(candidate) if current.get(key) != candidate.get(key))
        what = f"批处理后 {len(candidate)} 条记忆"
    else:
        if backend is None:
            raise CliError(f"数据库不存在：{context.db_path}")
        candidate = context.run(backend.all_entries())
        touched = None
        what = f"当前 {len(candidate)} 条记忆"

    result = context.run(
        memory_write.check_write(
            candidate,
            mode=mode,
            backend=backend,
            data_dir=context.settings.data_dir,
            limit=limit,
            allowlist=_allowlist(context),
            touched=touched,
        )
    )
    return _report_write_check(context, result, what=f"{what}（审批模式 {mode}）")


# ── memory holographic ──────────────────────────────────────────────────────


def run_memory_holographic(context: Context, args: Any) -> int:
    """`orbit memory holographic`：用 HRR 全息向量给记忆排序 / 看库的整体向量。

    `--query` 给了就按 `score`（fts 0.4 / jaccard 0.3 / hrr 0.3）排序，并把三路分量
    一起报出来——只报总分，用户没法回答"为什么这条排第一"。
    """
    entries = _entries(context)
    dim = int(getattr(args, "dim", 0) or 0) or holographic.DIM
    limit = int(getattr(args, "limit", 0) or 0) or 10
    try:
        graph = holographic.MemoryGraph(entries, dim=dim)
    except ValueError as exc:
        raise CliError(str(exc)) from exc

    query = str(getattr(args, "query", "") or "").strip()
    if not query:
        context.out(
            f"全息图记忆：{len(entries)} 条，维度 {dim}，"
            f"库向量模长 {graph.bank_norm():.4f}"
        )
        context.out(
            f"权重：fts {holographic.W_FTS} / jaccard {holographic.W_JACCARD} / hrr {holographic.W_HRR}"
        )
        if not entries:
            context.out("还没有记忆。（`orbit memory holographic --query <文本>` 在空库上不会返回结果）")
        context.emit(
            {
                "ok": True,
                "count": len(entries),
                "dim": dim,
                "weights": {
                    "fts": holographic.W_FTS,
                    "jaccard": holographic.W_JACCARD,
                    "hrr": holographic.W_HRR,
                },
            }
        )
        return 0

    rows = graph.query(query, limit=limit)
    if not rows:
        context.out(f"没有匹配「{query}」的记忆。")
        context.emit({"ok": True, "query": query, "hits": []})
        return 0
    context.table(
        ["key", "总分", "fts", "jaccard", "hrr"],
        [
            [
                str(row["key"]),
                f"{row['total']:.4f}",
                f"{row['fts']:.4f}",
                f"{row['jaccard']:.4f}",
                f"{row['hrr']:.4f}",
            ]
            for row in rows
        ],
    )
    context.emit({"ok": True, "query": query, "dim": dim, "hits": rows})
    return 0


# ── journey delete / edit：**不由本模块接管** ────────────────────────────────
#
# `journey edit` / `journey delete` 已经被 `skill_extra.py` 接管（它实现的是
# **技能时间线**那一侧：`edit` 写旁注到 `journey-notes.json`、`delete` 打墓碑而不删
# 审计数据）。记忆那一半只有 `journey list` 在 `memory_extra.py` 里。
#
# 为什么不把记忆节点的删/改也挂到这两个名字上：`journey delete <node>` 在技能侧的
# 语义是"从时间线上隐藏一条事件（可回溯）"，在记忆侧会变成"真删一条记忆（不可逆）"。
# 同一条命令两种不可逆程度，比留 todo 更危险。要删记忆用 `orbit memory reset`
# 或 `orbit memory batch`（那条路径有全有或全无的事务保证）。


# ── 挂上去的新子命令 ─────────────────────────────────────────────────────────

#: 声明表里**没有**的 `memory` 子命令：`(名字, 中文说明, 选项, handler)`。
EXTRA_MEMORY_SUBCOMMANDS: tuple[tuple[str, str, tuple[Option, ...], Handler], ...] = (
    (
        "write-check",
        "写入前的门禁体检：威胁扫描 → 审批三态 → 快照二次扫描 → 漂移防护",
        (
            Option(("--ops",), "内联 JSON 操作列表（检查这批操作生效后的最终态）", metavar="JSON"),
            Option(("--file",), "从文件读操作列表（JSON）", metavar="PATH"),
            Option(
                ("--mode",),
                "审批模式（默认取当前配置：off/smart/manual）",
                choices=("off", "smart", "manual"),
            ),
            Option(("--limit",), "字符预算（默认 2200）", default=0, metavar="N"),
            Option(("--apply",), "回放一份待处置写入文件（smart 模式暂存的那份）", metavar="PATH"),
        ),
        run_memory_write_check,
    ),
    (
        "holographic",
        "HRR 全息图记忆：按 fts0.4/jaccard0.3/hrr0.3 排序，或看库向量",
        (
            Option(("--query",), "检索文本（不给则只报库的整体向量）", metavar="TEXT"),
            Option(("--limit",), "返回条数（默认 10）", default=0, metavar="N"),
            Option(("--dim",), "HRR 维度（默认 1024，Hermes 的取值）", default=0, metavar="N"),
        ),
        run_memory_holographic,
    ),
)

#: 声明表里**没有**的 `profile` 子命令（11 条，另外 3 条走 `register_handler`）。
EXTRA_PROFILE_SUBCOMMANDS: tuple[tuple[str, str, tuple[Option, ...], Handler], ...] = (
    (
        "set",
        profile_cli.help_for("set"),
        (
            Option(("text",), "画像正文（用 § 或 markdown 列表分隔条目）", metavar="TEXT", nargs="?"),
            Option(("--file",), "从文件读画像正文", metavar="PATH"),
        ),
        profile_cli.run_set,
    ),
    (
        "add",
        profile_cli.help_for("add"),
        (Option(("entry",), "要追加的画像条目", metavar="ENTRY"),),
        profile_cli.run_add,
    ),
    (
        "remove",
        profile_cli.help_for("remove"),
        (Option(("entry",), "要删除的画像条目（归一化后全等或包含）", metavar="ENTRY"),),
        profile_cli.run_remove,
    ),
    (
        "clear",
        profile_cli.help_for("clear"),
        (Option(("--yes",), "确认清空（不可恢复）", action="store_true"),),
        profile_cli.run_clear,
    ),
    (
        "path",
        profile_cli.help_for("path"),
        (),
        profile_cli.run_path,
    ),
    (
        "edit",
        profile_cli.help_for("edit"),
        (Option(("--editor",), "编辑器命令（默认取 $VISUAL / $EDITOR）", metavar="CMD"),),
        profile_cli.run_edit,
    ),
    (
        "trim",
        profile_cli.help_for("trim"),
        (
            Option(("--limit",), "字符预算（默认 1600）", default=0, metavar="N"),
            Option(("--dry-run",), "只报不写", action="store_true"),
        ),
        profile_cli.run_trim,
    ),
    (
        "entries",
        profile_cli.help_for("entries"),
        (Option(("--limit",), "字符预算（默认 1600）", default=0, metavar="N"),),
        profile_cli.run_entries,
    ),
    (
        "diff",
        profile_cli.help_for("diff"),
        (Option(("source",), "要对比的文件", metavar="PATH"),),
        profile_cli.run_diff,
    ),
    (
        "check",
        profile_cli.help_for("check"),
        (Option(("--limit",), "字符预算（默认 1600）", default=0, metavar="N"),),
        profile_cli.run_check,
    ),
    (
        "budget",
        profile_cli.help_for("budget"),
        (Option(("--limit",), "字符预算（默认 1600）", default=0, metavar="N"),),
        profile_cli.run_budget,
    ),
)

#: 本模块已经处理过的路径（`_bind` 幂等用）。
_BOUND: set[str] = set()


def _bind(path: str, handler: Handler, options: tuple[Option, ...] | None = None) -> None:
    """把实现接到声明表的某条路径上（两种导入顺序都要成立）。

    与 `memory_extra._bind` 同一套做法，不再解释。多一个 `options` 参数，理由如下：

    `profile show` / `export` / `import` 三条在表里**已经有声明**，而那份声明抄的是
    Hermes 的 profile distribution（`show` 要 `profile_name`、`export` 要 `-o/--output`、
    `import` 要 `archive`）。本模块接管的是**用户画像**，参数形状必须跟着语义走，
    否则 `orbit profile show` 会强迫用户给一个"画像名"——一个不存在的东西。
    所以接管时**连同选项一起换掉**：路径是我的，形状也该是我的。
    """
    key = " ".join(path.split())
    if key not in _BOUND:
        try:
            register_handler(key, handler)
        except ValueError:
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
        if options is not None:
            node.options = list(options)


def _mount(parent_name: str, items: tuple[tuple[str, str, tuple[Option, ...], Handler], ...]) -> list[str]:
    """把一批子命令挂到已存在的父节点上。**幂等**：已存在的名字跳过。"""
    parent = REGISTRY.get(parent_name)
    if parent is None:
        return []
    mounted: list[str] = []
    for name, help_text, options, handler in items:
        if parent.child(name) is not None:
            continue
        parent.subcommands.append(
            Command(
                name=name,
                help=help_text,
                handler=handler,
                group=parent.group,
                state="done",
                options=list(options),
            )
        )
        mounted.append(name)
    # 父节点状态要重算：声明表组装时它的子命令还全是 todo。
    states = {child.state for child in parent.subcommands}
    if states == {"done"}:
        parent.state = "done"
    elif states & {"done", "partial"}:
        parent.state = "partial"
    return mounted


def mount_memory_subcommands() -> list[str]:
    """挂 `memory write-check` / `memory holographic`。"""
    return _mount("memory", EXTRA_MEMORY_SUBCOMMANDS)


def mount_profile_subcommands() -> list[str]:
    """挂 `profile` 的 11 条新子命令（另 3 条由 `register_handler` 接管）。"""
    return _mount("profile", EXTRA_PROFILE_SUBCOMMANDS)


def mounted_profile_names() -> list[str]:
    """`orbit profile` 下**本模块负责的** 14 条名字（测试用它逐条核对）。

    顺序照 `profile_cli.PROFILE_COMMANDS`，于是"有没有漏一条"是一行 `len()`。
    """
    return [name for name, _, _ in profile_cli.PROFILE_COMMANDS]


#: 组装钩子是否已装过（幂等标记）。理由同 `memory_extra.py`。
_ASSEMBLY_HOOK_INSTALLED = False


def _install_assembly_hook() -> bool:
    """在 `_table.register_specs` 外面包一层，表组装完立刻补挂。

    必须**链式**包装（读当前的 `register_specs` 再包），不能覆盖成自己的版本：
    `memory_extra.py` 已经装过一个钩子，覆盖掉会让它的 5 条子命令一起消失。
    """
    from app.cli.commands import _table as table_module

    global _ASSEMBLY_HOOK_INSTALLED
    if _ASSEMBLY_HOOK_INSTALLED:
        return False
    original = table_module.register_specs

    def register_specs(specs: Any) -> list[str]:
        skipped = original(specs)
        mount_memory_subcommands()
        mount_profile_subcommands()
        return skipped

    table_module.register_specs = register_specs
    _ASSEMBLY_HOOK_INSTALLED = True
    return True


#: 表里已有、还是 todo 的路径：接管，并换掉抄自 Hermes distribution 的选项形状。
#: 做成表而不是三次 `_bind` 调用，是为了能在表组装完成后**重放**一遍
#: （`_bind` 在导入时记 `_BOUND`，那时表还不存在，节点更新会被跳过）。
_PROFILE_BINDINGS: tuple[tuple[str, Handler, tuple[Option, ...]], ...] = (
    ("profile show", profile_cli.run_show, ()),
    (
        "profile export",
        profile_cli.run_export,
        (Option(("-o", "--out", "--output"), "输出路径（省略则打到 stdout）", metavar="PATH"),),
    ),
    (
        "profile import",
        profile_cli.run_import,
        (
            Option(("archive",), "要导入的画像文件路径", metavar="PATH"),
            Option(("--apply",), "真的落盘（默认只对比，dry-run）", action="store_true"),
        ),
    ),
)

for _path, _handler, _options in _PROFILE_BINDINGS:
    _bind(_path, _handler, options=_options)


def rebind_profile_options() -> list[str]:
    """表组装完成后重放 profile 三条的节点更新（幂等），返回改到的路径。

    为什么必须重放：`_bind` 只在路径**第一次**出现时更新节点，而本模块在
    `hermes_parity` 之前导入——那一刻 `REGISTRY.get("profile")` 还是 None，
    节点更新被静默跳过，选项就还是 Hermes distribution 的形状。
    症状很具体：`orbit profile show` 报 `the following arguments are required:
    PROFILE_NAME`，而 Orbit 的画像根本没有"画像名"这个东西。
    """
    changed: list[str] = []
    for path, handler, options in _PROFILE_BINDINGS:
        parts = path.split()
        node = REGISTRY.get(parts[0])
        for part in parts[1:]:
            if node is None:
                break
            node = node.child(part)
        if node is None or node.subcommands:
            continue
        if node.handler is not handler or list(node.options) != list(options):
            node.handler = handler
            node.options = list(options)
            node.state = "done"
            changed.append(path)
    return changed

# 表还没组装 → 装钩子；已组装 → 立即挂上。
if REGISTRY.get("memory") is None or REGISTRY.get("profile") is None:
    _install_assembly_hook()
mount_memory_subcommands()
mount_profile_subcommands()
