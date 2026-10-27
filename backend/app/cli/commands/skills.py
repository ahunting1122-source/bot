"""`orbit skills …` 的真实现（多根索引 / 校验 / 体检 / 快照 / trust）。

为什么用 ``register_handler`` 而不是另立一个 ``skills`` Command：声明表
（``hermes_parity.py``）里 ``skills`` 已有 **22 个子命令**，本轮只实现其中 8 条。
若把这 8 条"搬出表"另立一个 ``skills``，剩下 14 条会整片从 ``orbit --help``
和矩阵里消失——命令面反而缩水。所以照 ``_table.py`` 的约定：表不动，
这里只声明"这个路径我接了"。

**未实现的子命令保持 ``todo``，不接管**：``search`` / ``install`` / ``update`` /
``uninstall`` / ``audit`` / ``reset`` / ``list-modified`` / ``diff`` / ``opt-out`` /
``opt-in`` / ``repair-official`` / ``publish`` / ``tap`` / ``config`` 都依赖远端注册表
或 bundled 播种通道，本机没有网络也没有 hub，硬做出来只会是"看起来能用"的空壳。

三处**刻意的语义差异**（照抄会变成假对齐，所以写在这里而不是偷偷实现）：

1. ``skills list --source {hub,builtin}``：Orbit 没有 hub 安装通道、也没有 bundled
   播种，这两个枚举值恒为空。**接受参数但如实报空**，并在 stderr 说明理由。
2. ``skills check``：Hermes 的这个命令查的是"hub 技能有没有更新"（需要网络）。
   Orbit 没有远端，所以把它落到**本地体检**（frontmatter 硬校验 + 威胁扫描）——
   这才是本机唯一有意义的 "check"。
3. ``skills snapshot import``：Hermes 用它"照快照把技能装回来"。Orbit 没有安装通道，
   所以 import 只做**索引对账**（新增/缺失/变化），不落盘装技能。``--force``
   接受但忽略（它本来只管"装的时候要不要压过 caution 判决"）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context, truncate
from app.cli.spec import REGISTRY
from app.skill_index import (
    THREAT_PATTERN_COUNT,
    TIER_LABELS,
    TIER_PROJECT,
    TIER_USER,
    TIERS,
    SkillIndex,
    SkillIndexError,
    build_index,
)

#: ``--source`` 枚举 → tier 集合。hub / builtin 恒空（见模块 docstring 第 1 条）。
SOURCE_TIERS: dict[str, tuple[str, ...]] = {
    "all": TIERS,
    "local": (TIER_PROJECT, TIER_USER),
    "builtin": (),
    "hub": (),
}


def _index(context: Context) -> SkillIndex:
    """构造索引。一次命令一个实例，缓存生命周期 = 一次命令。"""
    return build_index(context.data_dir)


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _skill_or_error(index: SkillIndex, name: str) -> dict[str, Any]:
    try:
        return index.load(name)
    except SkillIndexError as exc:
        raise CliError(str(exc)) from exc


# ── skills list ─────────────────────────────────────────────────────────────


def run_list(context: Context, args) -> int:
    """`skills list`：列出已加载技能（名称 / tier / 索引描述）。"""
    source = str(getattr(args, "source", "") or "all")
    enabled_only = bool(getattr(args, "enabled_only", False))
    tiers = SOURCE_TIERS.get(source, TIERS)
    if source in ("hub", "builtin"):
        context.warn(
            f"`--source {source}` 在 Orbit 恒为空：本库没有远端 hub 安装通道，"
            "也没有 bundled 技能播种（bundled 是 Hermes 的分发机制，不是运行时根）。"
        )

    index = _index(context)
    result = index.index()
    rows = [record for record in result.records if record.tier in tiers]

    hidden_archived = 0
    if enabled_only:
        # Orbit 没有 `skills.disabled` 配置，`--enabled-only` 落到"隐藏已归档技能"：
        # 归档技能仍在磁盘上（可恢复），但不再参与装配，正是"不启用"的语义。
        from app.curator import STATE_ARCHIVED, Curator

        archived = {
            name
            for name, state in Curator(context.data_dir, index=index).state_map().items()
            if state == STATE_ARCHIVED
        }
        kept = []
        for record in rows:
            if record.name in archived:
                hidden_archived += 1
                continue
            kept.append(record)
        rows = kept

    for root_path in result.untrusted:
        context.warn(
            f"项目技能根未信任，已跳过：{root_path}（`orbit skills trust` 后才加载——"
            "仓库里的 SKILL.md 是别人写的内容，自动注入系统提示等于一条免审批的注入通道）"
        )

    context.out(f"技能 {len(rows)} 条" + (f"（隐藏已归档 {hidden_archived} 条）" if hidden_archived else ""))
    context.out()
    context.table(
        ("技能", "tier", "索引描述"),
        [(record.name, record.tier, truncate(record.description or "（无描述）", 60)) for record in rows],
    )
    if result.shadowed:
        context.note()
        context.note(f"被高优先级同名技能压住 {len(result.shadowed)} 条（first-wins，不是合并）：")
        for shadow in result.shadowed:
            context.note(
                f"  {shadow['name']}: {shadow['tier']} {shadow['path']}"
                f" ← 被 {shadow['shadowed_by_tier']} {shadow['shadowed_by_path']} 压住"
            )
    if result.errors:
        context.note()
        context.note(f"未加载的坏文件 {len(result.errors)} 条（详见 `orbit skills check`）：")
        for bad in result.errors:
            context.note(f"  {bad['path']}: {bad['error']}")

    context.emit(
        {
            "ok": True,
            "count": len(rows),
            "skills": [record.as_index() for record in rows],
            "shadowed": result.shadowed,
            "errors": result.errors,
            "untrusted": result.untrusted,
            "source": source,
        }
    )
    return 0


# ── skills inspect ──────────────────────────────────────────────────────────


def run_inspect(context: Context, args) -> int:
    """`skills inspect`：装/用之前先看清一个技能（frontmatter 全字段 + 正文前 40 行）。"""
    identifier = str(getattr(args, "identifier", "") or "").strip()
    if not identifier:
        raise CliError("`skills inspect` 需要一个技能标识（frontmatter name 或目录名）")
    index = _index(context)
    skill = _skill_or_error(index, identifier)

    context.out(f"技能：{skill['name']}（tier={skill['tier']}）")
    context.out(f"路径：{skill['path']}")
    if skill["error"]:
        context.out(f"校验：不通过 — {skill['error']}")
    context.out(f"描述：{skill['description'] or '（无）'}")
    if skill["triggers"]:
        context.out(f"触发词：{'、'.join(skill['triggers'])}")
    context.out()
    context.out("frontmatter：")
    frontmatter = skill["frontmatter"] or {}
    if not frontmatter:
        context.out("  （空）")
    for key in sorted(frontmatter):
        context.out(f"  {key}: {frontmatter[key]}")
    for warning in skill["warnings"]:
        context.warn(f"frontmatter 提示：{warning}")
    context.out()
    body_lines = (skill["body"] or "").splitlines()
    context.out(f"正文前 {min(len(body_lines), 40)} 行（共 {len(body_lines)} 行，全文请直接读文件）：")
    for line in body_lines[:40]:
        context.out(f"  {line}")

    context.emit(
        {
            "ok": True,
            "skill": {
                "name": skill["name"],
                "tier": skill["tier"],
                "path": skill["path"],
                "description": skill["description"],
                "triggers": skill["triggers"],
                "frontmatter": frontmatter,
                "warnings": skill["warnings"],
                "body_lines": len(body_lines),
                "body": skill["body"],
            },
        }
    )
    return 0


# ── skills check ────────────────────────────────────────────────────────────


def run_check(context: Context, args) -> int:
    """`skills check`：跑 frontmatter 硬校验 + 威胁扫描（本地体检，不需要网络）。"""
    name = str(getattr(args, "name", "") or "").strip() or None
    index = _index(context)
    result = index.index()

    errors = result.errors
    if name is not None:
        errors = [item for item in errors if item["name"] == name]
    try:
        reports = index.check(name)
    except SkillIndexError as exc:
        raise CliError(str(exc)) from exc

    context.out(f"体检 {len(reports) + len(errors)} 个技能（威胁模式库 {THREAT_PATTERN_COUNT} 条，Hermes 107 条的子集）")
    context.out()
    rows: list[tuple[str, str, str, str]] = []
    # 硬失败（退出码 1）与提示（只报告）必须分开：unknown-field 这类 frontmatter
    # 提示是 advisory，把它算成失败会让"体检"变成"任何人加个字段就红"。
    failures = 0
    warnings_count = 0
    for bad in errors:
        failures += 1
        rows.append((bad["name"], bad["tier"], "未加载", bad["error"]))
    for report in reports:
        warnings_count += len(report["warnings"])
        findings = report["findings"]
        if not report["ok"] or findings:
            failures += 1 if not report["ok"] else 0
            failures += len(findings)
        verdict = "通过" if report["ok"] and not findings else "有问题"
        detail = report["error"] or "；".join(
            [f"警告 {warning}" for warning in report["warnings"]]
            + [
                f"{finding['severity']}:{finding['pattern_id']}({finding['where']}:{finding['line']})"
                for finding in findings
            ]
        )
        rows.append((report["name"], report["tier"], verdict, truncate(detail or "—", 70)))
    context.table(("技能", "tier", "结论", "详情"), rows)
    if failures:
        context.out()
        context.warn(
            f"共 {failures} 处硬问题（另有 {warnings_count} 条提示）。"
            "硬校验失败的文件不会被加载（其余技能照常可用）。"
        )
    elif warnings_count:
        context.out()
        context.note(f"另有 {warnings_count} 条 frontmatter 提示（不影响加载）。")

    context.emit(
        {
            "ok": not failures,
            "failures": failures,
            "warnings": warnings_count,
            "reports": reports,
            "errors": errors,
        }
    )
    return 1 if failures else 0


# ── skills browse ───────────────────────────────────────────────────────────


def run_browse(context: Context, args) -> int:
    """`skills browse`：按 tier 分组浏览（project > user > external）。"""
    page = max(1, _int(getattr(args, "page", None), 1))
    size = max(1, _int(getattr(args, "size", None), 20))
    source = str(getattr(args, "source", "") or "all")
    if source not in ("all", "local"):
        context.warn(
            f"`--source {source}` 在 Orbit 恒为空（无 hub 通道 / 无 bundled 播种）；按本地三根浏览。"
        )

    index = _index(context)
    result = index.index()
    grouped = result.by_tier()
    flat = [record for tier in TIERS for record in grouped.get(tier, [])]
    start = (page - 1) * size
    window = flat[start : start + size]

    context.out(f"共 {len(flat)} 条，第 {page} 页，每页 {size} 条")
    for tier in TIERS:
        records = [record for record in window if record.tier == tier]
        if not records:
            continue
        context.out()
        context.out(f"[{tier}] {TIER_LABELS[tier]}")
        context.table(
            ("技能", "索引描述"),
            [(record.name, truncate(record.description or "（无描述）", 60)) for record in records],
        )
    if not window:
        context.out()
        context.out("（本页没有技能）")

    context.emit(
        {
            "ok": True,
            "page": page,
            "size": size,
            "total": len(flat),
            "skills": [record.as_index() for record in window],
            "by_tier": {tier: len(grouped.get(tier, [])) for tier in TIERS},
        }
    )
    return 0


# ── skills snapshot export / import ─────────────────────────────────────────


def run_snapshot_export(context: Context, args) -> int:
    """`skills snapshot export`：把索引快照写成 JSON（``-`` = stdout）。"""
    output = str(getattr(args, "output", "") or "").strip()
    if not output:
        raise CliError("`skills snapshot export` 需要一个输出路径（`-` 表示 stdout）")
    snapshot = _index(context).snapshot()
    text = json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n"
    if output == "-":
        print(text, end="")
    else:
        path = Path(output).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        context.out(f"已导出 {len(snapshot['skills'])} 条技能索引 → {path}")
    context.emit({"ok": True, "skills": len(snapshot["skills"]), "output": output})
    return 0


def run_snapshot_import(context: Context, args) -> int:
    """`skills snapshot import`：把快照与本机索引对账（不装技能，见模块 docstring 第 3 条）。"""
    source = str(getattr(args, "input", "") or "").strip()
    if not source:
        raise CliError("`skills snapshot import` 需要一个输入路径")
    if getattr(args, "force", False):
        context.warn("`--force` 已忽略：本命令只做索引对账，不安装技能。")
    path = Path(source).expanduser()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CliError(f"读不出快照文件：{path}（{exc}）") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("skills"), list):
        raise CliError(f"快照格式不对：{path}（顶层需要 skills 数组）")

    index = _index(context)
    diff = index.diff_snapshot(payload)
    context.out(
        f"对账结果：新增 {len(diff['added'])} / 缺失 {len(diff['removed'])} / 描述变化 {len(diff['changed'])}"
    )
    for label, key in (("新增（快照有、本机无）", "added"), ("缺失（本机有、快照无）", "removed"),
                       ("描述变化", "changed")):
        if diff[key]:
            context.out(f"{label}：{'、'.join(diff[key])}")
    context.note()
    context.note(
        "本命令不安装技能：Orbit 没有远端注册表通道，"
        "快照只能当索引对账用。要真的搬技能，直接拷目录或恢复归档（`orbit curator restore`）。"
    )
    context.emit({"ok": True, "diff": diff, "source": str(path)})
    return 0


# ── skills trust / untrust ──────────────────────────────────────────────────


def run_trust(context: Context, args) -> int:
    """`skills trust`：信任一个项目根，让它的 ``.orbit/skills`` 参与加载。"""
    raw = str(getattr(args, "path", "") or "").strip()
    index = _index(context)
    target = index.trust(raw or None)
    context.out(f"已信任：{target}")
    context.out(f"项目技能根：{index.project_root}")
    context.emit({"ok": True, "trusted": target, "project_root": str(index.project_root)})
    return 0


def run_untrust(context: Context, args) -> int:
    """`skills untrust`：撤销信任。幂等——本来没信任也返回 0。"""
    raw = str(getattr(args, "path", "") or "").strip()
    index = _index(context)
    target = index.untrust(raw or None)
    if target:
        context.out(f"已撤销信任：{target}")
    else:
        context.out(f"本来就没有信任记录：{target}")
    context.emit({"ok": True, "untrusted": target})
    return 0


# ── 接线 ────────────────────────────────────────────────────────────────────


def _bind(path: str, handler: Any) -> None:
    """把实现登记到声明表的某条命令路径上（含三级路径）。

    两种导入顺序都必须成立：生产接线里本模块排在 ``hermes_parity`` **之前**，于是
    ``register_handler`` 生效、表组装时直接把 handler 填进去；但
    ``app/cli/commands/__init__.py`` 是包初始化，任何 ``import app.cli.commands.skills``
    都会先跑它——此时声明表可能**已经组装完毕**，``HANDLERS`` 里的登记不会再回填到
    已构建的 ``Command`` 上（它会停在"尚未实现"的占位 handler）。所以这里补一次回填。
    没有这一步，测试里跑 ``skills list`` 只会拿到"计划波次 W5"的报错。
    """
    register_handler(path, handler)
    parts = path.split()
    node = REGISTRY.get(parts[0])
    for part in parts[1:]:
        if node is None:
            return
        node = node.child(part)
    if node is not None and not node.subcommands:
        node.handler = handler
        node.state = "done"


_bind("skills list", run_list)
_bind("skills inspect", run_inspect)
_bind("skills check", run_check)
_bind("skills browse", run_browse)
_bind("skills snapshot export", run_snapshot_export)
_bind("skills snapshot import", run_snapshot_import)
_bind("skills trust", run_trust)
_bind("skills untrust", run_untrust)

#: 显式列出"本模块没接的路径"，让"未实现"这件事可被 grep 到（而不是靠记忆）。
UNBOUND_SUBCOMMANDS: tuple[str, ...] = (
    "skills search",
    "skills install",
    "skills update",
    "skills uninstall",
    "skills audit",
    "skills reset",
    "skills list-modified",
    "skills diff",
    "skills opt-out",
    "skills opt-in",
    "skills repair-official",
    "skills publish",
    "skills tap list",
    "skills tap add",
    "skills tap remove",
    "skills config",
)
