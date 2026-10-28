"""`orbit profile` 的用户画像命令面（MATRIX 9.16，14 个子命令）。

`app/profile.py` 已经把画像做成了**真值在文件里**的一层：`data/profile/USER.md` 是正文，
`profiles` 表只是索引，1,600 字符预算、`trim`（丢最旧）、`render_block` 都已实现。
本模块只补**命令面**——本模块**只读** `app/profile.py`，一行都不改它：

    只用它的公开 API：`load / save / append / remove / trim / split_entries /
    persisted_entries / render / profile_path / PROFILE_MAX_CHARS / SEPARATOR`

## 为什么命令面要补齐 14 条

画像是最容易被"写满才发现写不进去"的一块（本机 Hermes 的 USER.md 实测 101.3% 超顶）。
没有 `budget` / `trim` / `diff`，用户对这块预算就是全盲的：他既看不见还剩多少，
也不敢删——删错了没法回退。所以命令面里**读侧必须和写侧一样厚**。

## 为什么 `diff` / `import` 默认 dry-run

它们都会**整体替换**画像正文。画像是一整段注入的东西，换掉一条就可能让模型的默认假设
全变（"这个人做短剧"→"这个人做后端"）。整体替换是不可逆操作，默认必须是"先看后做"，
要落盘得显式 `--apply`。这与 `orbit skills diff` / `orbit memory batch --dry-run`
是同一个取舍：**不可逆的动作默认只报告**。

## 为什么 `check` 与 `memory_write` 共用一套扫描

画像和记忆是同一个威胁面（都会被整段注入提示词），两套规则表迟早会一边严一边松，
而"松的那一边"就是漏洞。所以 `check` 直接调 `memory_write.threat_scan`，
`set` / `add` 也走同一套（写路径不该有后门）。

## 与 `app/cli/commands/hermes_parity.py` 里那 13 条 `profile` 的关系

那张表里的 `profile {list,use,create,delete,describe,show,alias,rename,export,import,
install,update,info}` 抄的是 **Hermes 的 profile distribution**（`hermes profile
install <git-url>`，一整套可切换的发行版配置）。本模块实现的是**用户画像**（USER.md），
两件事。重叠的三个名字（`show` / `export` / `import`）语义相容，由本模块接管；
剩下 10 条（distribution 那一族）**保持 todo 不动**——把它们的语义改成画像才是真的抄错。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from app import memory_prompt, profile
from app.cli.context import CliError, Context
from app.memory_write import describe_findings, threat_scan

#: 导出/导入的文件后缀提示（不强制，`split_entries` 两种格式都认）。
EXPORT_SUFFIX = ".md"

#: `edit` 找不到编辑器时的提示。这里不 fallback 到 `notepad`：在无 GUI 的会话里
#: 拉起记事本会**挂住整个命令**，那比报错难排查得多。
EDITOR_HINT = "请设 $EDITOR，或用 --editor <命令>"


# ── 公共小工具 ───────────────────────────────────────────────────────────────


def _first_arg(args: Any, *names: str) -> str:
    """按顺序取第一个非空的参数值。

    为什么需要：`profile export` / `profile import` 在声明表里原本抄的是 Hermes 的
    distribution 命令（参数名 `output` / `archive`），本模块接管后语义变成"用户画像"，
    参数名跟着改（`out` / `source`）。两种 dest 都认，才不会因为导入顺序不同
    （先表后模块 / 先模块后表）导致取值取空。
    """
    for name in names:
        value = getattr(args, name, None)
        if value:
            return str(value).strip()
    return ""


def _entries(context: Context) -> list[str]:
    """当前**已落盘**的画像条目。没有文件时是空列表，不是默认骨架。"""
    return list(profile.persisted_entries(context.settings))


def _limit(args: Any) -> int:
    """预算上限。`--limit 0`（默认）表示用 `PROFILE_MAX_CHARS`。"""
    value = int(getattr(args, "limit", 0) or 0)
    return value if value > 0 else profile.PROFILE_MAX_CHARS


def _budget_line(entries: list[str], limit: int) -> str:
    """用量自曝行，口径与 MEMORY 的 `[99% — 2,181/2,200 字符]` 完全一致。

    复用 `memory_prompt.render_user`：标题行的格式只能有一处定义，
    否则"记忆说 99%、画像说 98.7%"这种不一致会立刻出现。
    """
    block = memory_prompt.render_user(entries, limit)
    return memory_prompt.format_header(block.used, block.limit, memory_prompt.USER_HEADER)


def _scan_or_refuse(context: Context, entries: list[str], *, action: str) -> None:
    """写入前的威胁扫描。命中就抛 `CliError`，消息里带命中类别。"""
    findings: list[dict] = []
    for index, entry in enumerate(entries, 1):
        for hit in threat_scan(entry):
            findings.append({**hit, "entry": index})
    if findings:
        raise CliError(
            f"{action}被拒绝：画像条目命中威胁特征 {describe_findings(findings)}。"
            "（画像会被整段注入提示词，所以与记忆写入同一套规则）",
            code=1,
        )


def _read_incoming(path_text: str) -> list[str]:
    """读一份外部画像正文（导入/对比用）。读不出来给可操作的报错。"""
    path = Path(path_text).expanduser()
    if not path.is_file():
        raise CliError(f"找不到文件：{path}")
    try:
        return profile.split_entries(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError) as exc:
        raise CliError(f"读不了画像文件 {path}：{exc}") from exc


def _normalize(entry: str) -> str:
    """条目归一化（判重用）。与 `profile._normalize_entry` 同口径。"""
    return profile._normalize_entry(entry)  # noqa: SLF001 —— 判重口径必须与存储层一致


def _diff(current: list[str], incoming: list[str]) -> dict:
    """两组条目的差异。`added` 是"导进来会多出来的"，`removed` 是"会被删掉的"。"""
    have = {_normalize(item): item for item in current}
    want = {_normalize(item): item for item in incoming}
    return {
        "added": [item for key, item in want.items() if key not in have],
        "removed": [item for key, item in have.items() if key not in want],
        "common": [item for key, item in want.items() if key in have],
    }


def _report_diff(context: Context, diff: dict, *, source: str) -> None:
    """把差异打成三行。空差异要说"没有差异"，不能什么都不打印。"""
    context.out(f"与 {source} 相比：")
    for label, key in (("新增", "added"), ("删除", "removed"), ("保留", "common")):
        items = diff.get(key) or []
        context.out(f"  {label} {len(items)} 条")
        for item in items[:20]:
            context.out(f"    {'+' if key == 'added' else ('-' if key == 'removed' else ' ')} {item}")
        if len(items) > 20:
            context.out(f"    …还有 {len(items) - 20} 条")
    if not diff.get("added") and not diff.get("removed"):
        context.out("  两份内容没有差异。")


# ── 1. show ─────────────────────────────────────────────────────────────────


def run_show(context: Context, args: Any) -> int:
    """`orbit profile show`：看画像正文 + 用量。"""
    current = profile.load(context.settings)
    entries = list(current["entries"])
    limit = _limit(args)
    context.out(_budget_line(entries, limit))
    context.out(f"文件：{current['path']}{'' if current['exists'] else '（尚未创建）'}")
    context.out("")
    body = current["body"].rstrip("\n")
    context.out(body if body.strip() else "（空画像：还没有任何条目）")
    context.emit({"ok": True, **{k: v for k, v in current.items() if k != "body"}, "budget": _budget_line(entries, limit)})
    return 0


# ── 2. set ──────────────────────────────────────────────────────────────────


def run_set(context: Context, args: Any) -> int:
    """`orbit profile set`：**整篇覆盖**画像（`§` 或 markdown 列表都认）。"""
    text = getattr(args, "text", None)
    path = getattr(args, "file", None)
    if text and path:
        raise CliError("TEXT 与 --file 只能给一个")
    if path:
        entries = _read_incoming(str(path))
    elif text:
        entries = profile.split_entries(str(text))
    else:
        raise CliError("缺少内容：给一个 TEXT 参数或 --file <路径>")
    if not entries:
        raise CliError("内容里没有解析出任何条目（空画像请用 `orbit profile clear --yes`）")
    _scan_or_refuse(context, entries, action="整篇覆盖")
    result = profile.save(context.settings, profile.render(entries))
    context.out(f"已写入 {len(result['entries'])} 条：{_budget_line(result['entries'], result['limit'])}")
    if result["dropped"]:
        context.warn(f"超预算，已丢弃最旧的 {len(result['dropped'])} 条")
    context.emit({"ok": True, "dropped": result["dropped"], "entries": result["entries"]})
    return 0


# ── 3. add ──────────────────────────────────────────────────────────────────


def run_add(context: Context, args: Any) -> int:
    """`orbit profile add <ENTRY>`：追加一条（去重）。"""
    entry = str(getattr(args, "entry", "") or "").strip()
    if not entry:
        raise CliError("缺少条目内容")
    _scan_or_refuse(context, [entry], action="追加")
    result = profile.append(context.settings, entry)
    if not result.get("added", True):
        context.out(f"这条已经存在：{result.get('duplicate', entry)}")
        context.emit({"ok": True, "added": False, "duplicate": result.get("duplicate", "")})
        return 0
    context.out(f"已追加：{_budget_line(result['entries'], result['limit'])}")
    if result["dropped"]:
        context.warn(f"超预算，已丢弃最旧的 {len(result['dropped'])} 条")
    context.emit({"ok": True, "added": True, "entries": result["entries"]})
    return 0


# ── 4. remove ───────────────────────────────────────────────────────────────


def run_remove(context: Context, args: Any) -> int:
    """`orbit profile remove <ENTRY>`：删掉匹配的条目（归一化后全等或包含）。"""
    entry = str(getattr(args, "entry", "") or "").strip()
    if not entry:
        raise CliError("缺少要删除的条目内容")
    result = profile.remove(context.settings, entry)
    removed = result["removed"]
    if not removed:
        context.out(f"没有找到匹配「{entry}」的条目（用 `orbit profile entries` 看现有条目）")
        context.emit({"ok": True, "removed": [], "entries": result["entries"]})
        return 0
    context.out(f"已删除 {len(removed)} 条：{'；'.join(removed)}")
    context.out(_budget_line(result["entries"], result["limit"]))
    context.emit({"ok": True, "removed": removed, "entries": result["entries"]})
    return 0


# ── 5. clear ────────────────────────────────────────────────────────────────


def run_clear(context: Context, args: Any) -> int:
    """`orbit profile clear --yes`：清空画像。**必须显式确认**。"""
    entries = _entries(context)
    if not entries:
        context.out("画像已经是空的。")
        context.emit({"ok": True, "removed": 0})
        return 0
    if not bool(getattr(args, "yes", False)):
        raise CliError(f"这会删掉 {len(entries)} 条画像条目，不可恢复。确认请加 --yes", code=1)
    profile.save(context.settings, profile.render([]))
    context.out(f"已清空 {len(entries)} 条画像条目")
    context.emit({"ok": True, "removed": len(entries)})
    return 0


# ── 6. path ─────────────────────────────────────────────────────────────────


def run_path(context: Context, args: Any) -> int:
    """`orbit profile path`：打印画像文件路径（脚本里拼路径用）。"""
    path = profile.profile_path(context.settings)
    context.out(str(path))
    context.emit({"ok": True, "path": str(path), "exists": path.is_file()})
    return 0


# ── 7. edit ─────────────────────────────────────────────────────────────────


def run_edit(context: Context, args: Any) -> int:
    """`orbit profile edit`：用外部编辑器改画像正文。

    `--editor` 优先于 `$EDITOR` / `$VISUAL`。都没配就报错并给出路径——
    静默拉起一个可能不存在的编辑器会让命令挂住，那比报错难排查得多。
    """
    path = profile.profile_path(context.settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.is_file():
        path.write_text(profile.render([]), encoding="utf-8")
    editor = str(getattr(args, "editor", "") or "").strip()
    if not editor:
        editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or ""
    if not editor:
        raise CliError(f"没有可用的编辑器：{EDITOR_HINT}。画像文件在 {path}", code=1)
    before = path.read_text(encoding="utf-8")
    try:
        # `shell=False` + 拆分：编辑器命令可能带参数（`code -w`），但不该走 shell。
        completed = subprocess.run(  # noqa: S603 —— 命令来自用户自己的环境变量
            [*editor.split(), str(path)], check=False
        )
    except OSError as exc:
        raise CliError(f"启动编辑器失败（{editor}）：{exc}。画像文件在 {path}", code=1) from exc
    after = path.read_text(encoding="utf-8")
    changed = before != after
    entries = profile.split_entries(after)
    context.out(
        f"编辑器退出码 {completed.returncode}；"
        f"{'内容已修改' if changed else '内容未改动'}：{_budget_line(entries, _limit(args))}"
    )
    context.emit({"ok": True, "changed": changed, "entries": entries})
    return 0


# ── 8. trim ─────────────────────────────────────────────────────────────────


def run_trim(context: Context, args: Any) -> int:
    """`orbit profile trim`：把超预算的画像裁到预算内（丢最旧）。

    默认**落盘**（`--dry-run` 只报）。为什么 trim 与 diff/import 不同：它只删
    "本来就装不下"的条目，结果与 `profile.save` 的自动裁剪完全一致——不落盘反而是
    假装预算没超。真正需要"先看后做"的是**替换内容**那类操作。
    """
    entries = _entries(context)
    limit = _limit(args)
    kept, dropped = profile.trim(entries, limit)
    if not dropped:
        context.out(f"画像在预算内（{_budget_line(entries, limit)}），无需裁剪。")
        context.emit({"ok": True, "dropped": [], "entries": kept})
        return 0
    context.out(f"需要丢弃 {len(dropped)} 条最旧的条目：")
    for item in dropped:
        context.out(f"  - {item}")
    if bool(getattr(args, "dry_run", False)):
        context.out(f"[dry-run] 未落盘。保留 {len(kept)} 条：{_budget_line(kept, limit)}")
        context.emit({"ok": True, "dry_run": True, "dropped": dropped, "entries": kept})
        return 0
    result = profile.save(context.settings, profile.render(kept))
    context.out(f"已裁剪：保留 {len(result['entries'])} 条，{_budget_line(result['entries'], limit)}")
    context.emit({"ok": True, "dry_run": False, "dropped": dropped, "entries": result["entries"]})
    return 0


# ── 9. entries ──────────────────────────────────────────────────────────────


def run_entries(context: Context, args: Any) -> int:
    """`orbit profile entries`：逐条列出（带序号，方便配 `remove`）。"""
    entries = _entries(context)
    limit = _limit(args)
    if not entries:
        context.out("画像里还没有条目。（`orbit profile add <内容>` 加一条）")
        context.emit({"ok": True, "entries": [], "count": 0, "budget": _budget_line([], limit)})
        return 0
    context.table(
        ["#", "字符", "条目"],
        [[str(index), str(len(item)), item] for index, item in enumerate(entries, 1)],
    )
    context.out("")
    context.out(f"{len(entries)} 条：{_budget_line(entries, limit)}")
    context.emit(
        {
            "ok": True,
            "entries": entries,
            "count": len(entries),
            "budget": _budget_line(entries, limit),
        }
    )
    return 0


# ── 10. export ──────────────────────────────────────────────────────────────


def run_export(context: Context, args: Any) -> int:
    """`orbit profile export [--out PATH]`：把画像正文导出成一份 Markdown。"""
    body = profile.render(_entries(context))
    out = _first_arg(args, "out", "output")
    if not out:
        context.out(body.rstrip("\n"))
        context.emit({"ok": True, "body": body, "path": ""})
        return 0
    path = Path(out).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    context.out(f"已导出到 {path}（{len(body)} 字符）")
    context.emit({"ok": True, "body": body, "path": str(path)})
    return 0


# ── 11. import ──────────────────────────────────────────────────────────────


def run_import(context: Context, args: Any) -> int:
    """`orbit profile import <PATH> [--apply]`：从文件导入画像。**默认 dry-run**。"""
    source = _first_arg(args, "source", "archive")
    if not source:
        raise CliError("缺少文件路径")
    incoming = _read_incoming(source)
    if not incoming:
        raise CliError(f"{source} 里没有解析出任何条目")
    current = _entries(context)
    diff = _diff(current, incoming)
    limit = _limit(args)
    _report_diff(context, diff, source=source)
    context.out("")
    context.out(f"导入后：{_budget_line(incoming, limit)}")
    if not bool(getattr(args, "apply", False)):
        context.out("[dry-run] 未落盘。确认请加 --apply")
        context.emit({"ok": True, "dry_run": True, "applied": False, **diff})
        return 0
    _scan_or_refuse(context, incoming, action="导入")
    result = profile.save(context.settings, profile.render(incoming))
    context.out(f"已导入 {len(result['entries'])} 条：{_budget_line(result['entries'], limit)}")
    if result["dropped"]:
        context.warn(f"超预算，已丢弃最旧的 {len(result['dropped'])} 条")
    context.emit({"ok": True, "dry_run": False, "applied": True, **diff})
    return 0


# ── 12. diff ────────────────────────────────────────────────────────────────


def run_diff(context: Context, args: Any) -> int:
    """`orbit profile diff <PATH>`：把现有画像与一份文件逐条对比（只读）。"""
    source = str(getattr(args, "source", "") or "").strip()
    if not source:
        raise CliError("缺少文件路径")
    incoming = _read_incoming(source)
    current = _entries(context)
    diff = _diff(current, incoming)
    _report_diff(context, diff, source=source)
    context.emit({"ok": True, **diff})
    return 0


# ── 13. check ───────────────────────────────────────────────────────────────


def run_check(context: Context, args: Any) -> int:
    """`orbit profile check`：跑与 `memory_write` **同一套**威胁扫描。

    扫两遍：逐条扫（定位到具体条目）+ 整段扫（画像是一次性整段注入的，
    跨条组合的注入只在整段里才看得见）。
    """
    current = profile.load(context.settings)
    entries = list(current["entries"])
    findings: list[dict] = []
    for index, entry in enumerate(entries, 1):
        for hit in threat_scan(entry):
            findings.append({**hit, "scope": "entry", "entry": index})
    for hit in threat_scan(current["body"]):
        if not any(
            item["kind"] == hit["kind"] and item["detail"] == hit["detail"] for item in findings
        ):
            findings.append({**hit, "scope": "body", "entry": 0})
    if not findings:
        context.out(f"画像干净：{len(entries)} 条，{_budget_line(entries, _limit(args))}")
        context.emit({"ok": True, "findings": [], "count": len(entries)})
        return 0
    context.out(f"发现 {len(findings)} 处可疑特征：")
    context.table(
        ["范围", "条目", "类别", "说明"],
        [
            [
                str(item.get("scope", "")),
                str(item.get("entry", 0) or "-"),
                str(item.get("kind", "")),
                str(item.get("detail", "")),
            ]
            for item in findings
        ],
    )
    context.emit({"ok": False, "findings": findings, "count": len(entries)})
    # `main` 只在成功时刷 `--json` 载荷，而"查出问题"恰恰是非 0 退出——
    # 不在这里自己刷一次，`--json` 下就只剩一个退出码。
    context.finish()
    return 1


# ── 14. budget ──────────────────────────────────────────────────────────────


def run_budget(context: Context, args: Any) -> int:
    """`orbit profile budget`：用量自曝（对齐 MEMORY.md 的 `[99% — n/N 字符]` 口径）。"""
    entries = _entries(context)
    limit = _limit(args)
    block = memory_prompt.render_user(entries, limit)
    line = memory_prompt.format_header(block.used, block.limit, memory_prompt.USER_HEADER)
    context.out(line)
    context.out(f"条目：{len(entries)} 条　剩余：{max(0, limit - block.used)} 字符")
    if block.dropped:
        context.warn(f"已超出预算，当前注入会丢掉最旧的 {block.dropped} 条（用 `profile trim` 处理）")
    context.emit(
        {
            "ok": True,
            "used": block.used,
            "limit": block.limit,
            "percent": block.percent,
            "count": len(entries),
            "dropped": block.dropped,
            "header": line,
        }
    )
    return 0


# ── 命令面声明 ──────────────────────────────────────────────────────────────

#: 14 条子命令的 `(名字, 中文说明, handler)`。
#:
#: 为什么写成数据而不是一堆 `Command(...)`：`memory_extra2.mount_profile_subcommands`
#: 要拿它和声明表逐条核对（表里已有的 3 条走 `register_handler`，其余 11 条挂上去）。
#: 数据化之后"14 条有没有漏"是一行 `len()`，而不是数 14 个函数名。
PROFILE_COMMANDS: tuple[tuple[str, str, Any], ...] = (
    ("show", "查看画像正文与用量", run_show),
    ("set", "整篇覆盖画像（TEXT 或 --file）", run_set),
    ("add", "追加一条画像条目（去重）", run_add),
    ("remove", "删除匹配的画像条目", run_remove),
    ("clear", "清空画像（需 --yes）", run_clear),
    ("path", "打印画像文件路径", run_path),
    ("edit", "用外部编辑器改画像正文", run_edit),
    ("trim", "把超预算的画像裁到预算内（丢最旧）", run_trim),
    ("entries", "逐条列出画像条目", run_entries),
    ("export", "导出画像为 Markdown", run_export),
    ("import", "从文件导入画像（默认 dry-run）", run_import),
    ("diff", "与一份文件逐条对比（只读）", run_diff),
    ("check", "跑与记忆写入同一套威胁扫描", run_check),
    # 说明里**不能出现裸 `%`**：argparse 会用 `help % params` 做格式化，
    # 一个孤立的百分号会让 `--help` 直接抛 `ValueError: unsupported format character`。
    # 所以这里写成"百分比"而不是 `[99% — …]`（同一个坑在 `orbit profile --help` 里
    # 表现为整个命令树都渲染不出来）。
    ("budget", "用量自曝（百分比 + n/N 字符）", run_budget),
)

#: 声明表 `profile` 节点里**已经存在**的路径。这 3 条用 `register_handler` 接管，
#: 不能再挂一份同名子命令（`REGISTRY` 会因为子命令重名直接报错）。
EXISTING_PATHS: frozenset[str] = frozenset({"show", "export", "import"})

#: 本模块要实现、但声明表里**没有**的 11 条（挂到已存在的 `profile` 父节点上）。
NEW_NAMES: tuple[str, ...] = tuple(
    name for name, _, _ in PROFILE_COMMANDS if name not in EXISTING_PATHS
)


def handler_for(name: str) -> Any:
    """按子命令名取 handler（`memory_extra2` 组装命令时用）。"""
    for item_name, _, handler in PROFILE_COMMANDS:
        if item_name == name:
            return handler
    raise KeyError(f"未知的 profile 子命令：{name}")


def help_for(name: str) -> str:
    """按子命令名取中文说明。"""
    for item_name, help_text, _ in PROFILE_COMMANDS:
        if item_name == name:
            return help_text
    raise KeyError(f"未知的 profile 子命令：{name}")


def payload(entries: list[str], limit: int | None = None) -> dict:
    """给别的模块（HTTP / 测试）复用的小工具：条目 + 用量行。"""
    budget = limit if limit is not None else profile.PROFILE_MAX_CHARS
    return {
        "entries": list(entries),
        "count": len(entries),
        "budget": _budget_line(list(entries), budget),
        "limit": budget,
    }
