"""`orbit skills` 的补充实现 + `orbit bundles` + `orbit journey`（L7 收尾）。

本模块接的是**声明表里已经存在、但一直停在 ``todo`` 的路径**（用
``register_handler``，与 ``skills.py`` / ``curator.py`` 同一套做法）：

| 命令 | 实现落点 |
|---|---|
| ``skills search`` | `app/skill_hub.py` 的本地 hub 目录（模拟远端） |
| ``skills install`` / ``update`` / ``uninstall`` | hub → quarantine → 扫描 → 正式目录 |
| ``skills lint`` / ``stage`` / ``approve`` / ``quarantine`` | `app/skill_lint.py` + `app/skill_hub.py` |
| ``bundles {create,list,show,delete,reload}`` | `app/bundles.py` |
| ``journey {list,edit,delete}``（含别名 ``learning`` / ``memory-graph``） | `app/journey.py` |

## ``journey list`` 是**两边共用**的入口（不要把它当成"谁抢到算谁的"）

`memory_extra.py` 先一步接了 ``journey list`` 的**记忆那一半**（`memory graph` 的
节点/边）。Hermes 的 `journey` 原文是「a timeline … of learned skills **and
memories**」，所以这里的 ``run_journey_list`` **两半都打**：先是技能学习时间线
（本模块），再是记忆节点（复用 `memory_graph.build`）。后导入的模块把先导入的功能
悄悄顶掉，是最难排查的一类回归——「我加了时间线，记忆图谱就没了」。

## 避开已被接管的路径

``skills.py`` 已经接了 ``list`` / ``inspect`` / ``check`` / ``browse`` /
``snapshot {export,import}`` / ``trust`` / ``untrust``；``curator.py`` 接了
``status`` / ``run`` / ``pin`` / ``unpin`` / ``archive`` / ``restore`` /
``ledger`` / ``usage`` / ``prune``。本模块**一条都不碰**——重复 ``register_handler``
会直接抛 ``ValueError``，那是设计上的提醒，不是意外。

## 仍然保持 ``todo`` 的路径（缺的是通道，不是几十行代码）

``skills audit`` / ``reset`` / ``list-modified`` / ``diff`` / ``opt-out`` /
``opt-in`` / ``repair-official`` / ``publish`` / ``tap`` / ``config`` 全部依赖
**bundled 播种通道或远端注册表**：Orbit 没有 `bundled-skills` 目录、也没有网络，
做出来只能是「看起来能用」的空壳。``curator pause`` / ``resume`` / ``adopt`` /
``backup`` / ``rollback`` / ``purge`` / ``list-*`` 同理（缺后台 tick、provenance
声明流程、tar 快照库）。它们留在 ``todo`` 里，是矩阵诚实的一部分。

## 三处「表里表达不了」的选项，在使用点升级（**不是改表**）

`hermes_parity.py` 不在本波次可改范围，而这三处语义它表达不了：

1. ``bundles create --skill`` 必须是**可重复**的（Hermes 原文「repeat for
   multiple」）。表里声明成单值，argparse 只会留最后一个 —— 于是「--skill a
   --skill b」会静默只装 a。这里把该 ``Option`` 换成 ``action="append"``。
2. ``journey edit`` 需要 ``--note``（Hermes 用 ``$EDITOR``；本 CLI 没有编辑器
   循环，旁注直接给值）。表里只声明了 ``node``。
3. ``journey list`` 需要 ``--days``（时间线窗口）。表里只有 ``--no-color``。

三处都**只增不减**：原有 flag 一个不少，``--help`` 里只会多出说明。

## 导入顺序

照 ``ui_extra.py`` 的做法：装一个「表组装」钩子，包一层
``_table.register_specs``。于是**两种导入顺序都成立**——本模块排在
``hermes_parity`` 之前（生产接线）时，钩子在表组装完的那一刻补挂；排在之后
（测试里直接 ``import app.cli.commands.skill_extra``）时，模块体直接补挂一次。
两条路都幂等。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from app.bundles import Bundles, BundlesError
from app.cli.commands import _table
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context, truncate
from app.cli.spec import REGISTRY, Command, Option
from app.curator import parse_iso
from app.journey import DEFAULT_DAYS, Journey, JourneyError
from app.skill_hub import SkillHub, SkillHubError
from app.skill_index import build_index
from app.skill_lint import (
    RULE_COUNT,
    SkillLintError,
    SkillStaging,
    format_findings,
    has_errors,
    lint_file,
)

#: 钩子是否已装（重复导入不重复包 register_specs）。
_ASSEMBLY_HOOK_INSTALLED = False


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _short_ts(raw: str, width: int = 16) -> str:
    """时间戳只显示到分钟：完整 ISO 会把表格其它列挤爆。"""
    parsed = parse_iso(raw)
    if parsed is not None:
        return parsed.strftime("%Y-%m-%d %H:%M")
    return truncate(raw or "", width)


def _hub(context: Context) -> SkillHub:
    return SkillHub(context.data_dir)


def _staging(context: Context) -> SkillStaging:
    return SkillStaging(context.data_dir)


def _bundles(context: Context) -> Bundles:
    return Bundles(context.data_dir)


def _journey(context: Context) -> Journey:
    return Journey(context.data_dir)


def _looks_remote(identifier: str) -> bool:
    """判断标识是不是「远端」形状：URL / ``git@`` / 三段式 ``owner/repo/name``。

    本地 hub 的类目嵌套最多两层（``mlops/eval-harness``），所以三段以上按远端处理。
    误判的方向很重要：把本地名当远端会给出「未实现」的报错（可纠正），把远端标识
    当本地名会得到「本地 hub 里没有」的报错（也能纠正）——两种都不静默。
    """
    text = (identifier or "").strip()
    if text.lower().startswith(("http://", "https://", "git@", "ssh://")):
        return True
    parts = [part for part in text.replace("\\", "/").split("/") if part]
    return len(parts) >= 3


# ── skills search ───────────────────────────────────────────────────────────


def run_skills_search(context: Context, args) -> int:
    """`skills search`：搜本地 hub 目录（**远端通道未实现**，见 `install_remote`）。"""
    query = str(getattr(args, "query", "") or "").strip()
    source = str(getattr(args, "source", "") or "all").strip()
    limit = max(1, _int(getattr(args, "limit", None), 20))
    if source and source != "all":
        context.warn(
            f"`--source {source}` 在本机恒为空：没有远端注册表可查（无网络）。"
            "这里只搜本地 hub 目录。"
        )
    hub = _hub(context)
    rows = hub.search(query, limit=limit)
    context.out(f"本地 hub：{hub.hub_root}（模拟远端，远端通道未实现）")
    context.out(f"查询：{query or '（全部）'}　命中 {len(rows)} 条")
    context.out()
    if rows:
        context.table(
            ("技能", "identifier", "索引描述", "已装"),
            [
                (
                    str(row["name"]),
                    str(row["identifier"]),
                    truncate(str(row["description"] or "（无描述）"), 50),
                    "是" if row["installed"] else "",
                )
                for row in rows
            ],
        )
    else:
        context.out("（没有匹配的技能）")
        context.note()
        context.note(
            f"放技能包：{hub.hub_root}\\<名字>\\SKILL.md（类目可嵌套），"
            "然后 `orbit skills install <名字>`——它会先落隔离区，扫完才生效。"
        )
    context.emit({"ok": True, "query": query, "count": len(rows), "skills": rows, "source": source})
    return 0


# ── skills install / update / uninstall ─────────────────────────────────────


def run_skills_install(context: Context, args) -> int:
    """`skills install`：从本地 hub 装一个技能（quarantine → 扫描 → 正式目录）。"""
    identifier = str(getattr(args, "identifier", "") or "").strip()
    if not identifier:
        raise CliError("用法：orbit skills install <本地 hub 里的名字或 类目/名字>")
    force = bool(getattr(args, "force", False))
    override = str(getattr(args, "name", "") or "").strip() or None
    category = str(getattr(args, "category", "") or "").strip()
    hub = _hub(context)
    if category:
        context.warn(
            f"`--category {category}` 已忽略：Orbit 的技能根是扁平布局（与 `curator restore` "
            "一致）。类目只是展示分组，重建层级需要额外记录原路径——这一层不做。"
        )
    if _looks_remote(identifier):
        try:
            hub.install_remote(identifier)
        except SkillHubError as exc:
            raise CliError(str(exc)) from exc
        return 1  # pragma: no cover - install_remote 必抛
    try:
        result = hub.install(identifier, force=force, name=override)
    except SkillHubError as exc:
        raise CliError(str(exc)) from exc

    if not result["ok"]:
        context.out(f"已隔离：{result['name']} → {result['path']}")
        context.out(f"扫描判决：{result['verdict']}（零 critical 才放行）")
        critical = (result.get("scan") or {}).get("critical") or []
        if critical:
            context.out()
            context.table(
                ("severity", "pattern", "位置", "行", "摘录"),
                [
                    (
                        "critical",
                        str(item.get("pattern_id") or ""),
                        str(item.get("where") or ""),
                        str(item.get("line") or ""),
                        truncate(str(item.get("excerpt") or ""), 50),
                    )
                    for item in critical
                ],
            )
        context.warn(
            "安装被 quarantine 拦下。修好内容后 `orbit skills quarantine --release "
            f"{result['name']}`（它会重扫；critical 不能强装），或 `--purge` 删掉。"
        )
        context.emit({"ok": False, **result})
        return 1

    context.out(f"已安装：{result['name']} → {result['path']}")
    context.out(f"扫描判决：{result['verdict']}（隔离区已清空，索引已刷新）")
    lint_rows = (result.get("scan") or {}).get("lint") or []
    if lint_rows:
        context.note()
        context.note(f"linter 提示 {len(lint_rows)} 条（advisory，不阻断）：")
        for item in lint_rows[:8]:
            context.note(f"  [{item.get('code')}] {truncate(str(item.get('message') or ''), 70)}")
    context.emit({"ok": True, **result})
    return 0


def run_skills_update(context: Context, args) -> int:
    """`skills update`：把 hub 里有更新的技能重装一遍。

    「本地改过」与「hub 更新过」是两件事：前者默认**跳过**（否则用户的修改静默消失），
    ``--force`` 才覆盖。这与 Hermes 的 ``--force``（Overwrite skills you have edited
    locally — they are skipped by default）同一语义。
    """
    name = str(getattr(args, "name", "") or "").strip() or None
    force = bool(getattr(args, "force", False))
    hub = _hub(context)
    rows = hub.updates()
    if name is not None:
        rows = [row for row in rows if row["name"] == name]
        if not rows:
            raise CliError(f"没有 {name!r} 的 hub 安装记录（`orbit skills list --source hub` 可查）")

    updated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for row in rows:
        if not row["outdated"]:
            continue
        if row["local_modified"] and not force:
            skipped.append(row)
            continue
        try:
            result = hub.install(row["identifier"], force=True, name=row["name"])
        except SkillHubError as exc:
            context.warn(f"{row['name']} 更新失败：{exc}")
            continue
        updated.append({"name": row["name"], "ok": bool(result.get("ok")),
                        "verdict": result.get("verdict")})
        if not result.get("ok"):
            context.warn(f"{row['name']} 的新版本被隔离区拦下（critical），旧版本保留")

    context.out(f"检查 {len(rows)} 个 hub 技能：更新 {len(updated)} 个，跳过 {len(skipped)} 个")
    if updated:
        context.out()
        context.table(("技能", "结果", "判决"),
                      [(item["name"], "已更新" if item["ok"] else "被隔离", str(item["verdict"]))
                       for item in updated])
    if skipped:
        context.out()
        context.note("以下技能本地改过，已跳过（要覆盖加 --force，你的修改会丢失）：")
        for row in skipped:
            context.note(f"  {row['name']}（{row['identifier']}）")
    if not updated and not skipped:
        context.note()
        context.note("（都是最新版本，没有要更新的）")
    context.emit({"ok": True, "updated": updated, "skipped": skipped, "checked": len(rows)})
    return 0


def run_skills_uninstall(context: Context, args) -> int:
    """`skills uninstall`：删掉一个 **hub 安装的** 技能（必须 ``-y``）。"""
    name = str(getattr(args, "name", "") or "").strip()
    if not name:
        raise CliError("用法：orbit skills uninstall <技能名> -y")
    if not getattr(args, "yes", False):
        raise CliError(
            f"拒绝执行：这会删除已安装的技能目录 {name}（不可恢复，"
            "可恢复的路径是 `orbit curator archive`）。确认请加 -y"
        )
    hub = _hub(context)
    try:
        result = hub.uninstall(name)
    except SkillHubError as exc:
        raise CliError(str(exc)) from exc
    context.out(f"已卸载：{name}（{result['path']}）")
    context.emit(result)
    return 0


# ── skills lint / stage / approve / quarantine ──────────────────────────────


def run_skills_lint(context: Context, args) -> int:
    """`skills lint`：12 条 advisory 规则 + AST 审计（`skills check` 的软伴生）。"""
    name = str(getattr(args, "name", "") or "").strip() or None
    index = build_index(context.data_dir)
    result = index.index()
    records = result.records
    if name is not None:
        record = result.find(name)
        if record is None:
            raise CliError(f"技能不存在：{name}（`orbit skills list` 可查）")
        records = [record]

    rows: list[tuple[str, str, str]] = []
    failures = 0
    for record in records:
        try:
            findings = lint_file(record.path, name=record.name)
        except SkillLintError as exc:
            raise CliError(str(exc)) from exc
        if has_errors(findings):
            failures += 1
        detail = "；".join(f"[{item.code}] {item.message}" for item in findings[:3])
        rows.append(
            (
                record.name,
                "有问题" if findings else "干净",
                truncate(detail or "—", 78),
            )
        )
    # 硬校验失败的文件不在 records 里（索引层已拒），但 lint 恰恰最该看它们
    for bad in result.errors:
        if name is not None and str(bad.get("name") or "") != name:
            continue
        try:
            findings = lint_file(Path(str(bad["path"])), name=str(bad.get("name") or ""))
        except SkillLintError:
            continue
        if has_errors(findings):
            failures += 1
        detail = "；".join(f"[{item.code}] {item.message}" for item in findings[:3])
        rows.append((str(bad.get("name") or "?"), "未加载", truncate(detail or "—", 78)))

    context.out(f"lint {len(rows)} 个技能（{RULE_COUNT} 条规则 + AST 审计；advisory，不阻断安装）")
    context.out()
    context.table(("技能", "结论", "前 3 条问题"), rows)
    if failures:
        context.out()
        context.warn(
            f"共 {failures} 个技能有 ERROR 级问题（`skills stage` 的审批门会拒绝这类内容）。"
        )
    context.emit({"ok": not failures, "failures": failures, "reports": rows})
    return 1 if failures else 0


def run_skills_stage(context: Context, args) -> int:
    """`skills stage`：把新技能写进待审区；**不带名字时列出待审清单**。"""
    name = str(getattr(args, "name", "") or "").strip()
    staging = _staging(context)
    if not name:
        pending = staging.list()
        context.out(f"待审 {len(pending)} 条（{staging.root}）")
        context.out()
        if pending:
            context.table(
                ("id", "技能", "写入时间", "frontmatter", "问题数", "备注"),
                [
                    (
                        item.staging_id,
                        item.name,
                        truncate(item.staged_at, 19),
                        "通过" if item.frontmatter_ok else "不通过",
                        str(len(item.findings)),
                        truncate(item.note or "—", 30),
                    )
                    for item in pending
                ],
            )
            context.note()
            context.note("审批：`orbit skills approve <id>`　驳回：`orbit skills approve <id> --reject`")
        else:
            context.out("（没有待审技能）")
            context.note()
            context.note(
                "写入一个：`orbit skills stage <名字> --file <SKILL.md 路径>`"
                "（或把内容从 stdin 传进来）"
            )
        context.emit(
            {"ok": True, "count": len(pending), "staged": [i.as_dict() for i in pending]}
        )
        return 0

    file_path = str(getattr(args, "file", "") or "").strip()
    if file_path:
        path = Path(file_path).expanduser()
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise CliError(f"读不出技能文件：{path}（{exc}）") from exc
    else:
        if sys.stdin is None or sys.stdin.isatty():
            raise CliError(
                "需要技能内容：`--file <SKILL.md>`，或把内容从 stdin 传进来"
                "（技能正文太长，不适合直接写在命令行上）"
            )
        text = sys.stdin.read()

    try:
        staging_id = staging.stage(name, text, note=str(getattr(args, "note", "") or ""))
    except SkillLintError as exc:
        raise CliError(str(exc)) from exc
    staged = staging.get(staging_id)
    context.out(f"已暂存：{staged.name}（id={staging_id}）→ {staged.path}")
    context.out(f"frontmatter：{'通过' if staged.frontmatter_ok else '不通过 — ' + staged.frontmatter_error}")
    if staged.findings:
        context.out()
        context.out(f"linter 提示 {len(staged.findings)} 条（审批时 ERROR 会拒绝）：")
        for line in format_findings(list(staged.findings)).splitlines()[:12]:
            context.out(f"  {line}")
    context.out()
    context.note(f"审批：`orbit skills approve {staging_id}`　驳回：`orbit skills approve {staging_id} --reject`")
    context.emit({"ok": True, **staged.as_dict()})
    return 0


def run_skills_approve(context: Context, args) -> int:
    """`skills approve`：审批通过（搬进正式目录）或 ``--reject`` 驳回（删掉）。"""
    staging_id = str(getattr(args, "staging_id", "") or "").strip()
    if not staging_id:
        raise CliError("用法：orbit skills approve <待审 id>（列表见 `orbit skills stage`）")
    staging = _staging(context)
    try:
        if getattr(args, "reject", False):
            removed = staging.reject(staging_id)
            context.out(f"已驳回并删除：{staging_id}" if removed else f"没有删掉：{staging_id}")
            context.emit({"ok": True, "staging_id": staging_id, "rejected": True})
            return 0
        result = staging.approve(staging_id)
    except SkillLintError as exc:
        raise CliError(str(exc)) from exc
    context.out(f"已批准：{result['name']} → {result['path']}")
    context.note()
    context.note("索引会在下一次 `orbit skills list` 时刷新（缓存按 mtime + 条目数失效）。")
    context.emit({"ok": True, **result})
    return 0


def run_skills_quarantine(context: Context, args) -> int:
    """`skills quarantine`：列隔离区；``--release`` 重扫放行，``--purge`` 删掉。"""
    name = str(getattr(args, "name", "") or "").strip()
    hub = _hub(context)
    if name and getattr(args, "release", False):
        try:
            result = hub.release(name)
        except SkillHubError as exc:
            raise CliError(str(exc)) from exc
        context.out(f"已放行：{result['name']} → {result['path']}（判决 {result['verdict']}）")
        context.emit({"ok": True, **result})
        return 0
    if name and getattr(args, "purge", False):
        removed = hub.purge(name)
        if not removed:
            raise CliError(f"隔离区里没有技能：{name}")
        context.out(f"已删除隔离副本：{name}")
        context.emit({"ok": True, "name": name, "purged": True})
        return 0

    rows = hub.list_quarantine()
    context.out(f"隔离区 {len(rows)} 条（{hub.quarantine_root}）")
    context.out()
    if rows:
        context.table(
            ("技能", "identifier", "判决", "critical", "扫描时间"),
            [
                (
                    str(row["name"]),
                    truncate(str(row["identifier"] or "—"), 30),
                    str(row["verdict"]),
                    str(len(row["critical"])),
                    truncate(str(row["scanned_at"] or "—"), 19),
                )
                for row in rows
            ],
        )
        context.note()
        context.note(
            "放行：`orbit skills quarantine <名字> --release`（会重扫，critical 不能强装）"
            "　删除：`--purge`"
        )
    else:
        context.out("（隔离区是空的）")
    context.emit({"ok": True, "count": len(rows), "quarantine": rows})
    return 0


# ── bundles ─────────────────────────────────────────────────────────────────


def run_bundles_list(context: Context, args) -> int:
    """`bundles list`：列出技能别名集合。"""
    bundles = _bundles(context)
    items = bundles.list()
    context.out(f"技能包 {len(items)} 个（{bundles.path}）")
    context.out()
    if items:
        context.table(
            ("名字", "技能数", "描述", "创建时间"),
            [
                (item.name, str(len(item.skills)), truncate(item.description or "—", 44),
                 truncate(item.created_at, 19))
                for item in items
            ],
        )
        context.note()
        context.note("装载：`/<名字>`（一个斜杠命令一次装载全部技能）")
    else:
        context.out("（还没有技能包）")
        context.note()
        context.note("新建：`orbit bundles create <名字> --skill a --skill b -d 说明`")
    context.emit({"ok": True, "count": len(items), "bundles": [i.as_dict() for i in items]})
    return 0


def run_bundles_show(context: Context, args) -> int:
    """`bundles show`：查看一个技能包的内容。"""
    name = str(getattr(args, "name", "") or "").strip()
    if not name:
        raise CliError("用法：orbit bundles show <名字>")
    bundles = _bundles(context)
    try:
        bundle = bundles.show(name)
    except BundlesError as exc:
        raise CliError(str(exc)) from exc
    known = bundles.known_skills()
    tiers = {record.name: record.tier for record in bundles.index.index().records}
    context.out(f"技能包：/{bundle.name}")
    context.out(f"描述：{bundle.description or '（无）'}")
    context.out(f"创建：{bundle.created_at or '（未知）'}")
    if bundle.instruction:
        context.out(f"额外指导（前置拼接）：{bundle.instruction}")
    context.out()
    context.table(
        ("技能", "存在", "tier"),
        [
            (
                skill,
                "是" if skill in known else "否（`orbit bundles reload` 会报出来）",
                tiers.get(skill, "—"),
            )
            for skill in bundle.skills
        ],
    )
    context.emit({"ok": True, "bundle": bundle.as_dict()})
    return 0


def run_bundles_create(context: Context, args) -> int:
    """`bundles create`：新建技能包（``--skill`` 可重复）。"""
    name = str(getattr(args, "name", "") or "").strip()
    if not name:
        raise CliError("用法：orbit bundles create <名字> --skill <技能> [--skill …]")
    raw = getattr(args, "skill", None)
    if isinstance(raw, str):
        skills = [raw]
    elif isinstance(raw, list):
        skills = [str(item) for item in raw]
    else:
        skills = []
    if not skills:
        raise CliError(
            "至少要一个 `--skill <技能名>`（可重复）。"
            "Orbit 没有交互式输入通道，省略后等待输入只会让人以为命令挂了"
        )
    bundles = _bundles(context)
    try:
        bundle = bundles.create(
            name,
            skills,
            description=str(getattr(args, "description", "") or ""),
            instruction=str(getattr(args, "instruction", "") or ""),
            force=bool(getattr(args, "force", False)),
        )
    except BundlesError as exc:
        raise CliError(str(exc)) from exc
    context.out(f"已创建技能包：/{bundle.name}（{len(bundle.skills)} 个技能）")
    for skill in bundle.skills:
        context.out(f"  - {skill}")
    context.emit({"ok": True, "bundle": bundle.as_dict()})
    return 0


def run_bundles_delete(context: Context, args) -> int:
    """`bundles delete`：删除技能包（只删定义，不动技能本身）。"""
    name = str(getattr(args, "name", "") or "").strip()
    if not name:
        raise CliError("用法：orbit bundles delete <名字>")
    removed = _bundles(context).delete(name)
    if not removed:
        raise CliError(f"没有这个技能包：{name}")
    context.out(f"已删除技能包：{name}（技能本身没有被删除）")
    context.emit({"ok": True, "name": name, "deleted": True})
    return 0


def run_bundles_reload(context: Context, args) -> int:
    """`bundles reload`：把技能包里的技能重新读一遍（手工改完文件后刷新索引）。"""
    bundles = _bundles(context)
    report = bundles.reload()
    context.out(
        f"重载 {len(report.reloaded)} 个技能引用，缺失 {len(report.missing)} 个"
        f"（索引已失效重扫）"
    )
    if report.reloaded:
        context.out()
        context.table(
            ("技能包", "技能", "tier", "正文行数", "校验"),
            [
                (
                    str(item["bundle"]),
                    str(item["skill"]),
                    str(item["tier"]),
                    str(item["body_lines"]),
                    "通过" if item["ok"] else f"不通过：{truncate(str(item['error']), 40)}",
                )
                for item in report.reloaded
            ],
        )
    if report.missing:
        context.out()
        context.warn(f"引用了不存在的技能（共 {len(report.missing)} 条）：{'、'.join(report.missing)}")
    context.emit({"ok": not report.missing, **report.as_dict()})
    return 0 if not report.missing else 1


# ── journey ─────────────────────────────────────────────────────────────────


def _memory_section(context: Context) -> dict[str, Any]:
    """记忆那一半：`memory graph` 的节点/边（Hermes 的 journey 是**技能 + 记忆**同一条轴）。

    为什么要合到 `journey list` 里：`memory-graph` 是 `journey` 的别名（声明表里
    `journey` 的 `aliases=("learning","memory-graph")`），`memory_extra.py` 已经先一步
    接管了这条路径的**记忆那一半**。两条命令共用一个入口时，正确做法是**两边都打**
    （Hermes 的 `journey` 原文就是「learned skills **and memories**」），而不是让
    后导入的模块把先导入的悄悄顶掉——那种「我加了功能，别人的功能就没了」是最难
    排查的一类回归。

    数据库不存在时返回空而不是报错：技能时间线不依赖数据库，没有 DB 不该让整条
    命令失败。
    """
    if not context.db_path.exists():
        return {"nodes": [], "edges": []}
    from app.memory_batch import SqlMemoryBackend
    from app.memory_graph import build as build_graph

    entries = context.run(SqlMemoryBackend(context.store).all_entries())
    return build_graph(entries)


def run_journey_list(context: Context, args) -> int:
    """`journey list`：学习时间线（最旧在上、最新在下）+ 记忆节点。

    ``orbit learning list`` 与 ``orbit memory-graph list`` 走的是同一个 handler
    （声明表里它们是 ``journey`` 的别名）。
    """
    days = max(1, _int(getattr(args, "days", None), DEFAULT_DAYS))
    for flag in ("reveal", "play", "fps", "width", "height"):
        if getattr(args, flag, None) not in (None, False):
            context.warn(
                f"`--{flag}` 未实现：那套可播放的星座擦洗器（星图动画）本轮不做，"
                "这里只给时间线列表。"
            )
            break
    journey = _journey(context)
    entries = journey.list(days)
    context.out(f"最近 {days} 天：{len(entries)} 条事件（来源：.usage.json + skills-state.json + ledger）")
    context.out()
    if entries:
        context.table(
            ("时间", "类型", "技能", "详情", "旁注"),
            [
                (
                    _short_ts(entry.ts),
                    entry.kind,
                    truncate(entry.skill, 26),
                    truncate(entry.detail or "—", 46),
                    truncate(entry.note or "", 24),
                )
                for entry in entries
            ],
        )
        context.note()
        context.note("旁注：`orbit journey edit <节点 id> --note 文字`　隐藏：`orbit journey delete <节点 id> -y`")
        context.note("节点 id（写旁注/隐藏时用）：")
        for entry in entries:
            context.note(f"  {entry.entry_id}  {entry.kind:<8} {entry.skill}")
    else:
        context.out("（这个窗口里没有事件）")
        context.note()
        context.note("时间线只读审计数据（.usage.json / skills-state.json / ledger），"
                     "先有使用或治理记录才会有内容。")

    memory = _memory_section(context)
    nodes = memory.get("nodes") or []
    context.out()
    context.out(f"记忆节点 {len(nodes)} 个 / {len(memory.get('edges') or [])} 条边（`memory-graph` 别名共用这一份数据）")
    if nodes:
        context.table(
            ("记忆节点 id", "前缀", "度数"),
            [
                (str(node.get("id")), str(node.get("prefix")), str(node.get("degree")))
                for node in nodes
            ],
        )
    else:
        context.out("（还没有记忆，或数据库尚未建立）")

    context.emit(
        {
            "ok": True,
            "days": days,
            "count": len(entries),
            "entries": [entry.as_dict() for entry in entries],
            "memory": {"nodes": nodes, "edges": memory.get("edges") or []},
        }
    )
    return 0


def run_journey_edit(context: Context, args) -> int:
    """`journey edit`：给一条事件写旁注（**只写 journey-notes.json**）。"""
    node = str(getattr(args, "node", "") or "").strip()
    if not node:
        raise CliError("用法：orbit journey edit <节点 id> --note <旁注>")
    note = str(getattr(args, "note", "") or "")
    if not note:
        if sys.stdin is None or sys.stdin.isatty():
            raise CliError(
                "需要旁注内容：`--note <文字>`（本 CLI 没有 $EDITOR 循环，"
                "也不会替你打开编辑器）"
            )
        note = sys.stdin.read()
    journey = _journey(context)
    try:
        entry = journey.edit(node, note)
    except JourneyError as exc:
        raise CliError(str(exc)) from exc
    context.out(f"已记旁注：{entry.entry_id}（{entry.kind} / {entry.skill}）")
    context.out(f"旁注：{entry.note}")
    context.note("审计数据（.usage.json / ledger）一个字节都没改——时间线只写自己的旁注文件。")
    context.emit({"ok": True, **entry.as_dict()})
    return 0


def run_journey_delete(context: Context, args) -> int:
    """`journey delete`：从时间线上隐藏一条事件（墓碑，不删审计数据，必须 ``-y``）。"""
    node = str(getattr(args, "node", "") or "").strip()
    if not node:
        raise CliError("用法：orbit journey delete <节点 id> -y")
    if not getattr(args, "yes", False):
        raise CliError(
            "拒绝执行：这会在学习时间线上隐藏该事件（审计数据不会被删除，"
            "但列表里不再显示）。确认请加 -y"
        )
    journey = _journey(context)
    try:
        created = journey.delete(node)
    except JourneyError as exc:
        raise CliError(str(exc)) from exc
    context.out(
        f"已隐藏：{node}" if created else f"这条本来就被隐藏了：{node}"
    )
    context.note("旁注文件里打的是墓碑（deleted: true）；.usage.json 与 ledger 原样保留。")
    context.emit({"ok": True, "node": node, "hidden": True, "changed": created})
    return 0


# ── 接线 ────────────────────────────────────────────────────────────────────


def _bind(path: str, handler: Any) -> None:
    """登记某条路径的实现；表已组装时顺手填到 ``Command`` 上。

    与 ``ui_extra.py::_bind`` 同一套做法：``register_handler`` 前先查 ``HANDLERS``，
    于是「导入两次」「钩子与直接补挂各跑一次」都不会重复登记报错。
    """
    key = " ".join(path.split())
    if key not in _table.HANDLERS:
        register_handler(key, handler)
    parts = key.split()
    node = REGISTRY.get(parts[0])
    for part in parts[1:]:
        if node is None:
            return
        node = node.child(part)
    if node is not None and not node.subcommands:
        node.handler = handler
        node.state = "done"


def _skill_extra_commands() -> list[Command]:
    """`skills` 下三条表里**没有**的子命令（lint / stage / approve / quarantine）。

    它们在 Hermes 命令面里也没有对应条目——``hermes skills`` 没有 lint/stage，
    审批走 TUI 的 `/skills approve`。Orbit 没有 TUI 审批通道，所以把这条能力做成
    显式命令：一个只有 TUI 才存在的闸，在 CLI 里等于不存在。
    """
    return [
        Command(
            name="lint",
            help="按 12 条 advisory 规则 + AST 审计体检技能（软伴生，不阻断安装）",
            handler=run_skills_lint,
            group="ext",
            parity="P1",
            hermes="（Hermes 无此子命令：linter 只在 create 路径内部 advisory 出报告）",
            state="done",
            options=[
                Option(("name",), "只体检一个技能（省略 = 全部）", metavar="NAME", nargs="?"),
            ],
        ),
        Command(
            name="stage",
            help="把新技能写进待审区（模型写的东西不该直接生效）",
            handler=run_skills_stage,
            group="ext",
            parity="P1",
            hermes="（Hermes 走 TUI 的 /skills approve + write_approval staging）",
            state="done",
            options=[
                Option(("name",), "技能名（省略 = 列出待审清单）", metavar="NAME", nargs="?"),
                Option(("--file",), "从文件读技能全文（省略则读 stdin）", metavar="FILE"),
                Option(("--note",), "审批时显示的说明", metavar="NOTE"),
            ],
        ),
        Command(
            name="approve",
            help="审批待审技能：搬进正式目录，或 --reject 驳回删除",
            handler=run_skills_approve,
            group="ext",
            parity="P1",
            hermes="（Hermes: /skills approve 回放 pending/skills/<id>.json）",
            state="done",
            options=[
                Option(("staging_id",), "待审 id（`orbit skills stage` 列表第一列）",
                       metavar="STAGING_ID"),
                Option(("--reject",), "驳回并删除这条待审记录", action="store_true"),
            ],
        ),
        Command(
            name="quarantine",
            help="查看隔离区；--release 重扫放行（critical 不能强装）/ --purge 删除",
            handler=run_skills_quarantine,
            group="ext",
            parity="P1",
            hermes="（Hermes 的 quarantine 只在安装流程内部可见，没有对应命令）",
            state="done",
            options=[
                Option(("name",), "隔离区里的技能名（省略 = 列清单）", metavar="NAME", nargs="?"),
                Option(("--release",), "重扫并放行（零 critical 才搬进正式目录）",
                       action="store_true"),
                Option(("--purge",), "删除隔离副本（不可恢复）", action="store_true"),
            ],
        ),
    ]


def mount_skills_extras() -> list[str]:
    """给 `skills` 补 lint/stage/approve/quarantine（**幂等**）。"""
    parent = REGISTRY.get("skills")
    if parent is None:
        return []
    mounted: list[str] = []
    for command in _skill_extra_commands():
        if parent.child(command.name) is not None:
            continue
        parent.subcommands.append(command)
        mounted.append(command.name)
    return mounted


def _upgrade_bundles_create() -> bool:
    """把 ``bundles create --skill`` 升级成可重复（``action="append"``）。幂等。"""
    parent = REGISTRY.get("bundles")
    node = parent.child("create") if parent is not None else None
    if node is None:
        return False
    changed = False
    for index, option in enumerate(list(node.options)):
        if "--skill" not in option.flags:
            continue
        if option.action == "append":
            return False
        node.options[index] = Option(
            ("--skill", "-s"),
            "技能名（可重复：--skill a --skill b；会变成 /斜杠命令装载的全部技能）",
            action="append",
            metavar="SKILL",
            dest="skill",
        )
        changed = True
    return changed


def _upgrade_journey() -> bool:
    """给 ``journey list`` 补 ``--days``、给 ``journey edit`` 补 ``--note``。幂等。"""
    parent = REGISTRY.get("journey")
    if parent is None:
        return False
    changed = False
    listing = parent.child("list")
    if listing is not None and not any("--days" in option.flags for option in listing.options):
        listing.options.append(
            Option(("--days",), f"回看窗口（天，默认 {DEFAULT_DAYS}）", metavar="DAYS",
                   default=DEFAULT_DAYS)
        )
        changed = True
    editing = parent.child("edit")
    if editing is not None and not any("--note" in option.flags for option in editing.options):
        editing.options.append(
            Option(("--note",), "旁注内容（省略则从 stdin 读；只写 journey-notes.json）",
                   metavar="NOTE")
        )
        changed = True
    return changed


def _mount_everything() -> None:
    """把本模块负责的路径一次性接上。两条导入顺序下都成立、都可重复调用。"""
    mount_skills_extras()
    _upgrade_bundles_create()
    _upgrade_journey()
    _bind("skills search", run_skills_search)
    _bind("skills install", run_skills_install)
    _bind("skills update", run_skills_update)
    _bind("skills uninstall", run_skills_uninstall)
    _bind("bundles list", run_bundles_list)
    _bind("bundles show", run_bundles_show)
    _bind("bundles create", run_bundles_create)
    _bind("bundles delete", run_bundles_delete)
    _bind("bundles reload", run_bundles_reload)
    _bind("journey list", run_journey_list)
    _bind("journey edit", run_journey_edit)
    _bind("journey delete", run_journey_delete)


def _install_assembly_hook() -> bool:
    """在「表组装」这一个缝上补挂（照 ``ui.py`` / ``ui_extra.py``）。"""
    global _ASSEMBLY_HOOK_INSTALLED
    if _ASSEMBLY_HOOK_INSTALLED:
        return False
    original = _table.register_specs

    def register_specs(specs: Any) -> list[str]:
        skipped = original(specs)
        _mount_everything()
        return skipped

    _table.register_specs = register_specs
    _ASSEMBLY_HOOK_INSTALLED = True
    return True


_install_assembly_hook()
_mount_everything()

#: 本模块**没有**接的路径，显式列出让「未实现」可被 grep 到（而不是靠记忆）。
#: 理由见模块 docstring「仍然保持 todo 的路径」一节。
UNBOUND_SUBCOMMANDS: tuple[str, ...] = (
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
    "curator pause",
    "curator resume",
    "curator list-unmanaged",
    "curator adopt",
    "curator list-archived",
    "curator backup",
    "curator rollback",
    "curator purge",
)

__all__ = [
    "UNBOUND_SUBCOMMANDS",
    "mount_skills_extras",
    "run_bundles_create",
    "run_bundles_delete",
    "run_bundles_list",
    "run_bundles_reload",
    "run_bundles_show",
    "run_journey_delete",
    "run_journey_edit",
    "run_journey_list",
    "run_skills_approve",
    "run_skills_install",
    "run_skills_lint",
    "run_skills_quarantine",
    "run_skills_search",
    "run_skills_stage",
    "run_skills_uninstall",
    "run_skills_update",
]
