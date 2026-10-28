"""`orbit sync`（跨设备同步）+ 技能库规模与「内置技能」家族（MATRIX 7.13 / 7.16 / 7.18）。

本模块接三类路径，全部用 ``register_handler``（与 `skills.py` / `skill_extra.py` /
`curator.py` 同一套做法）：

| 命令 | 实现落点 |
|---|---|
| ``sync {status,push,pull,now,enable,disable,device}`` | `app/skill_sync.py`（三方合并 + 本地目录形式的远端） |
| ``skills {seed,coverage}`` | `app/skill_seed.py`（21 类目 / 91 技能的真实种子目录） |
| ``skills {audit,diff,config,list-modified,reset,opt-in,opt-out,repair-official,tap}`` | `app/skill_hub.py` + `app/skill_seed.py` + `app/skill_index.py` |

## 为什么这批「内置技能」命令现在能真做了

`skill_extra.py` 当时把 ``audit`` / ``diff`` / ``config`` / ``reset`` /
``list-modified`` / ``opt-out`` / ``opt-in`` / ``repair-official`` / ``tap`` 全部留在
``todo``，理由写得很清楚：「依赖 bundled 播种通道或远端注册表」。

MATRIX 7.18 把**播种通道**做出来了（`app/skill_seed.py`，91 条真实技能 + 播种清单）。
于是这批命令有了**真的底座**：

- ``list-modified`` / ``diff`` / ``reset`` / ``repair-official`` 都要一个"官方原版"
  可比对——现在是 `SEED_CATALOG`，不是空想；
- ``opt-out`` / ``opt-in`` 要一个能被关掉的播种器——`seed()` 会读
  ``.no-bundled-skills`` 标记；
- ``config`` 要一个真的会被消费的启停名单——`app/skill_index.py` 的
  ``read_disabled`` 现在会让停用的技能**不进索引**；
- ``tap`` 要一个技能来源登记表——本地目录形式的 tap 会作为额外技能根参与扫描。

**仍然做不到的是 ``skills publish``**（发布到注册表：无网络、无账号、无注册表协议）
和 ``sync propose``（组织共享集：同上）。两条都**明确报未实现**，不给空壳。

## 三处「表里表达不了」的选项，在使用点升级（**不是改表**）

`hermes_parity.py` 不在本波次可改范围，而这几处语义它表达不了：

1. ``sync pull`` / ``sync push`` 需要**位置参数**（bundle 路径）。表里这两条没有选项，
   于是 ``orbit sync pull D:\\usb\\sync`` 会报 "unrecognized arguments"。这里补一个
   可选位置参数 ``[bundle]`` / ``[dest]``。
2. ``sync pull`` / ``now`` 需要 ``--ours`` / ``--theirs`` / ``--skip``（冲突策略）。
3. ``skills config`` 需要 ``--enable`` / ``--disable``（表里它是个无参交互命令）。

三处都**只增不减**：原有 flag 一个不少，``--help`` 里只会多出说明。

## 导入顺序

照 `skill_extra.py` / `ui_extra.py` 的做法装一个「表组装」钩子，包一层
``_table.register_specs``。于是两种导入顺序都成立（生产接线里本模块排在
``hermes_parity`` 之前时，钩子在表组装完的那一刻补挂；测试里直接 import 本模块时，
模块体直接补挂一次）。两条路都幂等。
"""

from __future__ import annotations

import difflib
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app import skill_seed
from app.cli.commands import _table
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context, truncate
from app.cli.spec import REGISTRY, Command, Option
from app.skill_hub import SkillHub
from app.skill_index import (
    INDEX_CACHE_TTL_SECONDS,
    INDEX_DESCRIPTION_LIMIT,
    SkillIndex,
    build_index,
    read_disabled,
    read_taps,
    write_disabled,
    write_taps,
)
from app.skill_sync import POLICY_SKIP, SkillSync, SkillSyncError

#: 钩子是否已装（重复导入不重复包 register_specs）。
_ASSEMBLY_HOOK_INSTALLED = False


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _sync(context: Context) -> SkillSync:
    return SkillSync(context.data_dir)


def _hub(context: Context) -> SkillHub:
    return SkillHub(context.data_dir)


def _index(context: Context) -> SkillIndex:
    return build_index(context.data_dir)


def _short(value: str, width: int = 10) -> str:
    """hash 只显示前 10 位：完整 sha256 会把表格挤爆，而前 10 位足以比对。"""
    return (value or "-")[:width]


def _now_iso() -> str:
    """统一 ISO8601（带 UTC 偏移），与 `app/skill_seed.py` 写清单时的形状一致。"""
    return datetime.now(UTC).isoformat()


def _policy(args: Any) -> str:
    """从 ``--ours`` / ``--theirs`` / ``--skip`` 三个开关里取冲突策略。

    默认 ``skip``：自动选边会让另一台设备上的改动**静默消失**，而用户以为同步成功了
    （理由见 `app/skill_sync.py` 的模块 docstring）。
    """
    if getattr(args, "ours", False):
        return "ours"
    if getattr(args, "theirs", False):
        return "theirs"
    if getattr(args, "skip", False):
        return POLICY_SKIP
    return POLICY_SKIP


# ── sync ────────────────────────────────────────────────────────────────────


def run_sync_status(context: Context, args) -> int:
    """`sync status`：设备标签、opt-in 名单、可同步范围、基准条数、已有 bundle。"""
    sync = _sync(context)
    data = sync.status()
    context.out(f"设备标签：{data['device']}")
    context.out(f"纳入同步：{len(data['enabled'])} 条" + (
        "（" + "、".join(data["enabled"]) + "）" if data["enabled"] else "（空——先 `orbit sync enable <名字>`）"
    ))
    context.out(f"可同步：{len(data['eligible'])} 条（用户级技能，排除内置与 hub 安装）")
    builtin = data["not_eligible"]["builtin"]
    hub = data["not_eligible"]["hub"]
    if builtin or hub:
        context.note(
            f"不参与同步：内置 {len(builtin)} 条、hub 安装 {len(hub)} 条"
            "（内置由种子目录提供，同步它们会用旧副本覆盖另一台机器上更新的种子）"
        )
    context.out(f"合并基准（base）：{data['base']} 条技能")
    context.out(f"bundle 目录：{data['sync_root']}")
    if data["bundles"]:
        context.out()
        context.table(
            ("时间", "设备", "技能数", "路径"),
            [
                (row["created_at"][:19], row["device"] or "-", str(row["skills"]),
                 truncate(row["path"], 60))
                for row in data["bundles"][-10:]
            ],
        )
    else:
        context.note("（还没有 bundle：`orbit sync push` 会写第一份）")
    context.out()
    context.out(f"远端：{data['remote']}")
    context.emit({"ok": True, **data})
    return 0


def run_sync_push(context: Context, args) -> int:
    """`sync push [dest]`：把 opt-in 名单里的技能写成一份可传输的 bundle。"""
    sync = _sync(context)
    dest = str(getattr(args, "dest", "") or "").strip() or None
    try:
        result = sync.push(dest)
    except SkillSyncError as exc:
        raise CliError(str(exc)) from exc
    context.out(f"已推送 {len(result['skills'])} 条技能 → {result['bundle']}")
    context.out(f"设备：{result['device']}　时间：{result['created_at'][:19]}")
    context.note(
        "把这个目录（或打成 zip）拷到另一台机器，在那边 `orbit sync pull <路径>`；"
        "本机无网络，所以没有直连远端这一步"
    )
    context.emit({"ok": True, **result})
    return 0


def run_sync_pull(context: Context, args) -> int:
    """`sync pull [bundle]`：读 bundle、三方合并、把该导入的过 quarantine 后写入。"""
    sync = _sync(context)
    raw = str(getattr(args, "bundle", "") or "").strip()
    if raw:
        bundle_path = raw
    else:
        bundles = sync.bundles()
        if not bundles:
            raise CliError(
                f"没有可拉的 bundle：{sync.sync_root} 下没有同步包。"
                "请给出路径（`orbit sync pull D:\\usb\\sync`），或先在另一台机器 `orbit sync push`"
            )
        bundle_path = bundles[-1]["path"]
    policy = _policy(args)
    try:
        result = sync.pull(bundle_path, policy=policy)
    except SkillSyncError as exc:
        raise CliError(str(exc)) from exc

    merge = result["merge"]
    context.out(
        f"合并 {bundle_path}（策略 {policy}）：导入 {len(result['imported'])} 条，"
        f"冲突 {len(result['conflicts'])} 条，拦下 {len(result['blocked'])} 条"
    )
    counts = merge["counts"]
    context.out(
        f"裁决：ours {counts['ours']} / theirs {counts['theirs']} / "
        f"either {counts['either']} / none {counts['none']} / conflict {counts['conflict']}"
    )
    if result["imported"]:
        context.out()
        context.table(
            ("已导入", "扫描结论", "路径"),
            [(row["name"], row.get("verdict", ""), truncate(row.get("path", ""), 56))
             for row in result["imported"]],
        )
    if result["conflicts"]:
        context.out()
        context.out("冲突（**没有自动选边**）：")
        context.table(
            ("技能", "base", "ours", "theirs"),
            [
                (row["name"], _short(row["base"]), _short(row["ours"]), _short(row["theirs"]))
                for row in result["conflicts"]
            ],
        )
        context.out(
            "请逐条决定：`orbit sync pull --ours`（保留本地）/ `--theirs`（取远端）。"
            "默认 skip 是刻意的——自动选边会让另一边的改动静默消失"
        )
    if result["blocked"]:
        context.out()
        context.out("被拦下（留在隔离区，未写入正式目录）：")
        context.table(
            ("技能", "原因", "位置"),
            [(row["name"], truncate(row.get("reason", ""), 40), truncate(row.get("path", ""), 40))
             for row in result["blocked"]],
        )
        context.note("确认安全后用 `orbit skills quarantine <名字> --release` 重扫放行")
    context.emit({"ok": True, **result})
    return 0 if result["ok"] else 2


def run_sync_now(context: Context, args) -> int:
    """`sync now`：先拉最近的 bundle，再推一次（先拉后推，避免旧内容盖掉远端新改动）。"""
    sync = _sync(context)
    try:
        result = sync.now_sync(policy=_policy(args))
    except SkillSyncError as exc:
        raise CliError(str(exc)) from exc
    pulled = result["pulled"]
    if pulled is None:
        context.out("没有本地 bundle 可拉（`orbit sync pull <路径>` 可以指定外部目录）")
    else:
        context.out(
            f"已拉取：导入 {len(pulled['imported'])} 条，冲突 {len(pulled['conflicts'])} 条"
        )
    pushed = result["pushed"]
    if pushed is None:
        context.note("没有技能被纳入同步，跳过推送（`orbit sync enable <名字>`）")
    else:
        context.out(f"已推送 {len(pushed['skills'])} 条 → {pushed['bundle']}")
    context.emit({"ok": True, **result})
    return 0


def run_sync_enable(context: Context, args) -> int:
    """`sync enable <skill>`：把技能纳入同步（Hermes 的 opt-in 默认关闭，同口径）。"""
    sync = _sync(context)
    name = str(getattr(args, "skill", "") or "").strip()
    try:
        names = sync.enable(name)
    except SkillSyncError as exc:
        raise CliError(str(exc)) from exc
    context.out(f"已纳入同步：{name}（当前 {len(names)} 条）")
    context.note("内置技能与 hub 安装的技能不走同步：它们由种子目录 / `skills update` 管")
    context.emit({"ok": True, "enabled": list(names)})
    return 0


def run_sync_disable(context: Context, args) -> int:
    """`sync disable <skill>`：移出同步（只改名单，不动磁盘文件）。"""
    sync = _sync(context)
    name = str(getattr(args, "skill", "") or "").strip()
    names = sync.disable(name)
    context.out(f"已移出同步：{name}（当前 {len(names)} 条）")
    context.emit({"ok": True, "enabled": list(names)})
    return 0


def run_sync_device(context: Context, args) -> int:
    """`sync device [--name X]`：查看或设置本机标签（写进 manifest 的 device 字段）。"""
    sync = _sync(context)
    name = str(getattr(args, "name", "") or "").strip()
    if not name:
        context.out(f"本机标签：{sync.device()}")
        context.note("设置：`orbit sync device --name \"Ben's Laptop\"`")
        context.emit({"ok": True, "device": sync.device()})
        return 0
    value = sync.set_device(name)
    context.out(f"本机标签已设为：{value}")
    context.emit({"ok": True, "device": value})
    return 0


def run_sync_propose(context: Context, args) -> int:
    """`sync propose`：组织共享集——**未实现**（无网络、无组织账号）。

    明确报错而不是"记下来以后再说"：``propose`` 的全部价值在于"同事那边能看到"，
    本地记一条永远不出去的东西，会让人以为技能已经共享了。
    """
    name = str(getattr(args, "name", "") or "").strip()
    raise CliError(
        f"`sync propose` 未实现：组织共享集需要远端同步平面（无网络、无组织账号），"
        f"无法把 {name or '技能'} 提交上去。本地做法：`orbit sync enable {name or '<名字>'}` "
        "再 `orbit sync push`，把 bundle 交给同事 `orbit sync pull`",
        code=3,
    )


# ── skills seed / coverage（MATRIX 7.18）──────────────────────────────────────


def run_skills_seed(context: Context, args) -> int:
    """`skills seed`：把种子目录写进 `<data>/skills/`，**默认 dry_run**。

    默认只看不动：一次播种会往技能库里写 88 个目录，先让人看清要写什么再落盘，
    比"播完发现不对再删"便宜得多。
    """
    apply = bool(getattr(args, "apply", False))
    dry_run = not apply
    only = [str(item) for item in (getattr(args, "name", None) or [])]
    category = str(getattr(args, "category", "") or "").strip()
    if category:
        picked = [skill.name for skill in skill_seed.SEED_CATALOG if skill.category == category]
        if not picked:
            raise CliError(
                f"没有类目 {category!r}。可用：{', '.join(skill_seed.SEED_CATEGORIES)}"
            )
        only = sorted(set(only) | set(picked))
    try:
        result = skill_seed.seed(
            context.data_dir,
            only=only or None,
            dry_run=dry_run,
            skip_unavailable=bool(getattr(args, "skip_unavailable", False)),
            overwrite=bool(getattr(args, "force", False)),
        )
    except OSError as exc:
        raise CliError(f"播种失败：{exc}") from exc

    if result.get("opted_out"):
        raise CliError(str(result.get("error") or "已 opt-out：不播种"))

    mode = "预览（dry-run，未写盘）" if dry_run else "已写入"
    target = result["would_write"] if dry_run else result["written"]
    context.out(f"skills seed — {mode}：{len(target)} 条 → {result['target_root']}")
    counts: dict[str, int] = {}
    for row in result["rows"]:
        counts[row["action"]] = counts.get(row["action"], 0) + 1
    context.out(
        "　".join(f"{key} {value}" for key, value in sorted(counts.items())) or "（无事可做）"
    )
    if result["unknown"]:
        raise CliError(f"种子里没有这些技能：{', '.join(result['unknown'])}")
    if result["blocked"]:
        context.out()
        context.out("被 lint 拦下（**没有写盘**）：")
        context.table(
            ("技能", "错误"), [(row["name"], truncate("；".join(row["errors"]), 70))
                             for row in result["blocked"]]
        )
    if result["oversized"]:
        context.out()
        for row in result["oversized"]:
            context.warn(
                f"{row['name']} 渲染后 {row['chars']} 字符，超过索引上限 {row['limit']}："
                "索引不会加载它（需人工把细节拆到 references/）"
            )
    if result["warnings"]:
        context.note(
            f"lint 警告涉及 {len(result['warnings'])} 条技能（不阻断）："
            + "、".join(f"{row['name']}({'/'.join(row['codes'])})" for row in result["warnings"][:6])
            + ("…" if len(result["warnings"]) > 6 else "")
        )
    cov = result["coverage"]
    context.out()
    context.out(
        f"覆盖度：类目 {cov['categories'][0]}/{cov['categories'][1]}，"
        f"技能 {cov['skills'][0]}/{cov['skills'][1]}"
    )
    if dry_run:
        context.note("这是预览。确认无误后加 `--apply` 真的写盘。")
    else:
        context.note("每个写入的技能都已过 lint（零 ERROR 才落盘）；"
                     "`orbit skills coverage` 可复核，`orbit skills list-modified` 可查改动")
    context.emit({"ok": True, "dry_run": dry_run, **result})
    return 0 if not result["blocked"] else 2


def run_skills_coverage(context: Context, args) -> int:
    """`skills coverage`：技能库规模——类目 / 技能的 (have, total) 与缺口清单。"""
    cov = skill_seed.coverage(context.data_dir)
    have_categories, total_categories = cov["categories"]
    have_skills, total_skills = cov["skills"]
    context.out(
        f"类目：{have_categories}/{total_categories}　技能：{have_skills}/{total_skills}"
    )
    context.out(f"技能根：{cov['root']}")
    root = skill_seed.skills_root(context.data_dir)
    missing = [
        skill
        for skill in skill_seed.SEED_CATALOG
        if not (root / skill.name / skill_seed.SKILL_FILENAME).is_file()
    ]
    if missing:
        context.out()
        context.out(f"还没播的 {len(missing)} 条（按类目）：")
        grouped: dict[str, list[str]] = {}
        for skill in missing:
            grouped.setdefault(skill.category, []).append(skill.name)
        context.table(
            ("类目", "缺多少", "举例"),
            [
                (category, str(len(names)), ", ".join(names[:3]))
                for category, names in sorted(grouped.items())
            ],
        )
        context.note("`orbit skills seed`（预览）或 `orbit skills seed --apply`（落盘）")
    if cov["duplicates"]:
        context.note(
            f"源库有 {cov['duplicates']} 组重名技能，扁平落盘只能留一份——"
            "所以技能数上限是 88/91，不是 91/91（`orbit skills coverage` 分母保持 91，"
            "让这个差额一直可见）"
        )
    context.emit({"ok": True, **cov})
    return 0


# ── skills audit / diff（内置技能家族）────────────────────────────────────────


def run_skills_audit(context: Context, args) -> int:
    """`skills audit`：重扫已安装的 hub 技能（威胁模式 + lint），``--deep`` 出完整报告。

    与 `skills check` 的分工：``check`` 体检**索引里的全部技能**（含手写的、播种的），
    ``audit`` 只重扫 **hub 安装的那批**——它们是从外面拿进来的内容，值得单独一轮
    "再确认一次没被换过"。
    """
    hub = _hub(context)
    installed = hub.list_installed()
    if not installed:
        context.out("没有 hub 安装的技能可审计（`orbit skills install <名字>`）")
        context.emit({"ok": True, "audited": 0, "reports": []})
        return 0
    name = str(getattr(args, "name", "") or "").strip()
    deep = bool(getattr(args, "deep", False))
    if name:
        installed = [row for row in installed if row["name"] == name]
        if not installed:
            raise CliError(f"没有 hub 安装的技能：{name}")
    reports: list[dict[str, Any]] = []
    for row in installed:
        directory = hub.install_root / row["name"]
        if not directory.is_dir():
            reports.append(
                {"name": row["name"], "verdict": "missing", "path": str(directory),
                 "allowed": False, "files": 0, "critical": [], "lint": []}
            )
            continue
        report = hub.scan_dir(directory, row["name"]).as_dict()
        reports.append(report)
    blocked = [row for row in reports if not row.get("allowed", True)]
    context.out(
        f"审计 {len(reports)} 条 hub 技能："
        f"{sum(1 for row in reports if row.get('verdict') == 'safe')} safe / "
        f"{sum(1 for row in reports if row.get('verdict') == 'caution')} caution / "
        f"{len(blocked)} 有问题"
    )
    context.out()
    context.table(
        ("技能", "结论", "文件", "critical", "lint"),
        [
            (
                row["name"],
                row.get("verdict", "?"),
                str(row.get("files", 0)),
                str(len(row.get("critical") or [])),
                str(len(row.get("lint") or [])),
            )
            for row in reports
        ],
    )
    if deep:
        for row in reports:
            findings = row.get("lint") or []
            if not findings:
                continue
            context.out()
            context.out(f"—— {row['name']} 的 lint 明细 ——")
            for finding in findings:
                badge = "✗" if finding.get("severity") == "error" else "⚠"
                where = f"（第 {finding['line']} 行）" if finding.get("line") else ""
                context.out(f"  {badge} [{finding.get('code')}] {finding.get('message')}{where}")
    context.note("结论口径与安装时一致：零 critical 才放行，high/medium 只记录")
    context.emit({"ok": not blocked, "audited": len(reports), "reports": reports})
    return 0 if not blocked else 2


def run_skills_diff(context: Context, args) -> int:
    """`skills diff <name>`：本地副本与**官方原版**（种子目录）的统一 diff。

    "官方原版"来自 `app/skill_seed.py::SEED_CATALOG`（真实 SKILL.md 的逐字节副本），
    不是凭空生成的一份——否则 diff 出来的每一行都是噪声。
    """
    name = str(getattr(args, "name", "") or "").strip().removesuffix(".md")
    if not name:
        raise CliError("需要一个技能名（`orbit skills list-modified` 里第一列）")
    seed = skill_seed.find_seed(name)
    if seed is None:
        raise CliError(
            f"{name} 不是内置技能：种子里没有它，没有「官方原版」可比。"
            "自己写的技能请用 `orbit skills inspect` 看内容"
        )
    path = skill_seed.skills_root(context.data_dir) / name / skill_seed.SKILL_FILENAME
    if not path.is_file():
        raise CliError(f"本地没有 {name}（{path}）。`orbit skills seed --apply` 可以播回来")
    local = path.read_text(encoding="utf-8")
    stock = skill_seed.render_skill_md(seed)
    if local == stock:
        context.out(f"{name}：与官方原版一致（无差异）")
        context.emit({"ok": True, "name": name, "changed": False, "diff": ""})
        return 0
    diff = "".join(
        difflib.unified_diff(
            stock.splitlines(keepends=True),
            local.splitlines(keepends=True),
            fromfile=f"official/{name}/SKILL.md",
            tofile=f"local/{name}/SKILL.md",
        )
    )
    context.out(diff.rstrip("\n"))
    context.note("确认改动无误后可保留；要恢复原版：`orbit skills reset <名字> --restore -y`")
    context.emit({"ok": True, "name": name, "changed": True, "diff": diff})
    return 0


def run_skills_list_modified(context: Context, args) -> int:
    """`skills list-modified`：列出本地被改过 / 删掉的内置技能。

    判据是**播种那一刻的内容 hash**（播种清单里记着），不是与当前种子目录比——
    后者会把"我们升级了种子"误报成"用户改过"。
    """
    rows = skill_seed.modified(context.data_dir)
    if not rows:
        context.out("没有被改过的内置技能（播种清单为空也算：先 `orbit skills seed --apply`）")
        context.emit({"ok": True, "modified": []})
        return 0
    context.out(f"被改过的内置技能：{len(rows)} 条")
    context.out()
    context.table(
        ("技能", "状态", "路径"),
        [(row["name"], row["state"], truncate(row["path"], 60)) for row in rows],
    )
    context.note("`orbit skills diff <名字>` 看差异；`orbit skills reset <名字> --restore -y` 恢复原版")
    context.emit({"ok": True, "modified": rows})
    return 0


def run_skills_reset(context: Context, args) -> int:
    """`skills reset <name> [--restore]`：清掉播种清单里的记录，``--restore`` 顺便恢复原版。

    清清单 = 以后 `skills list-modified` 不再把它算成"内置技能被改过"。
    ``--restore`` 会用**当前种子目录**覆盖本地副本（不可逆，所以要 ``-y``）。
    """
    name = str(getattr(args, "name", "") or "").strip().removesuffix(".md")
    if not name:
        raise CliError("需要一个技能名（`orbit skills list-modified` 里第一列）")
    restore = bool(getattr(args, "restore", False))
    if restore and not getattr(args, "yes", False):
        raise CliError(
            f"拒绝执行：--restore 会用官方原版覆盖本地 {name}（改动不可恢复）。确认请加 -y"
        )
    manifest = skill_seed.read_manifest(context.data_dir)
    had = name in manifest
    if had:
        manifest.pop(name, None)
        skill_seed.write_manifest(context.data_dir, manifest)
    if not restore:
        context.out(f"已清除 {name} 的内置标记" if had else f"{name} 本来就不在内置清单里")
        context.emit({"ok": True, "name": name, "cleared": had, "restored": False})
        return 0
    seed = skill_seed.find_seed(name)
    if seed is None:
        raise CliError(f"{name} 不是内置技能：种子里没有它，无法恢复原版")
    target = skill_seed.skills_root(context.data_dir) / name
    target.mkdir(parents=True, exist_ok=True)
    text = skill_seed.render_skill_md(seed)
    (target / skill_seed.SKILL_FILENAME).write_text(text, encoding="utf-8")
    _index(context).invalidate()
    context.out(f"已用官方原版恢复 {name} → {target}")
    context.emit({"ok": True, "name": name, "cleared": had, "restored": True,
                  "path": str(target)})
    return 0


def run_skills_repair_official(context: Context, args) -> int:
    """`skills repair-official <name|all> [--restore]`：回填内置标记 / 从种子恢复。

    默认只**回填**：把"磁盘上确实存在、且与官方原版逐字节一致"的技能补进播种清单
    （用于清单丢了/换机器拷过来的场景）。``--restore`` 才会覆盖被改过或缺失的副本。
    """
    raw = str(getattr(args, "name", "") or "").strip().removesuffix(".md")
    restore = bool(getattr(args, "restore", False))
    if not raw:
        raise CliError("需要一个技能名，或 `all`（全部内置技能）")
    if restore and not getattr(args, "yes", False):
        raise CliError("拒绝执行：--restore 会覆盖本地副本（不可恢复）。确认请加 -y")
    targets = (
        [skill.name for skill in skill_seed.SEED_CATALOG]
        if raw == "all"
        else [raw]
    )
    root = skill_seed.skills_root(context.data_dir)
    manifest = skill_seed.read_manifest(context.data_dir)
    rows: list[dict[str, Any]] = []
    for name in targets:
        seed = skill_seed.find_seed(name)
        if seed is None:
            rows.append({"name": name, "action": "unknown"})
            continue
        path = root / name / skill_seed.SKILL_FILENAME
        stock = skill_seed.render_skill_md(seed)
        if not path.is_file():
            if restore:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(stock, encoding="utf-8")
                manifest[name] = _manifest_entry(seed, stock)
                rows.append({"name": name, "action": "restored"})
            else:
                rows.append({"name": name, "action": "missing"})
            continue
        current = path.read_text(encoding="utf-8")
        if current == stock:
            manifest[name] = _manifest_entry(seed, stock)
            rows.append({"name": name, "action": "backfilled"})
        elif restore:
            path.write_text(stock, encoding="utf-8")
            manifest[name] = _manifest_entry(seed, stock)
            rows.append({"name": name, "action": "restored"})
        else:
            rows.append({"name": name, "action": "modified"})
    if any(row["action"] in ("backfilled", "restored") for row in rows):
        skill_seed.write_manifest(context.data_dir, manifest)
        _index(context).invalidate()
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["action"]] = counts.get(row["action"], 0) + 1
    context.out("repair-official：" + "　".join(f"{k} {v}" for k, v in sorted(counts.items())))
    context.out()
    context.table(
        ("技能", "动作"),
        [(row["name"], row["action"]) for row in rows if row["action"] != "backfilled"][:40]
        or [(row["name"], row["action"]) for row in rows[:10]],
    )
    context.note("`modified` 表示本地被改过——加 `--restore -y` 才会覆盖它")
    context.emit({"ok": True, "rows": rows})
    return 0


def _manifest_entry(seed: skill_seed.SeedSkill, text: str) -> dict[str, Any]:
    """构造一条播种清单记录（与 `skill_seed.seed` 写入的形状一致）。"""
    return {
        "category": seed.category,
        "source": seed.source,
        "hash": skill_seed.content_hash(text),
        "seeded_at": _now_iso(),
        "triggers": list(seed.triggers),
    }


def run_skills_opt_out(context: Context, args) -> int:
    """`skills opt-out [--remove]`：写 ``.no-bundled-skills`` 标记，停止播种。

    ``--remove`` 会**顺便删掉未被改过**的播种技能（改过的、手写的、hub 装的一律不动）：
    "opt-out" 与 "opt-out 且清干净" 是两个意图，混成一个会让前者变得危险。
    """
    remove = bool(getattr(args, "remove", False))
    if remove and not getattr(args, "yes", False):
        raise CliError(
            "拒绝执行：--remove 会删除未被改过的内置技能目录。确认请加 -y"
        )
    changed = skill_seed.set_opt_out(context.data_dir, True)
    context.out(
        f"已写入 opt-out 标记：{skill_seed.opt_out_path(context.data_dir)}"
        if changed
        else "opt-out 标记本来就在"
    )
    removed: list[str] = []
    if remove:
        modified = {row["name"] for row in skill_seed.modified(context.data_dir)}
        root = skill_seed.skills_root(context.data_dir)
        for name in skill_seed.seeded_names(context.data_dir):
            if name in modified:
                continue
            target = root / name
            if not target.is_dir():
                continue
            shutil.rmtree(target, ignore_errors=True)
            removed.append(name)
        if removed:
            _index(context).invalidate()
        context.out(f"已删除 {len(removed)} 条未被改过的内置技能")
    context.note("恢复：`orbit skills opt-in`（标记清掉，下次 seed 才会再播）")
    context.emit({"ok": True, "opted_out": True, "changed": changed, "removed": removed})
    return 0


def run_skills_opt_in(context: Context, args) -> int:
    """`skills opt-in [--sync]`：清掉标记；``--sync`` 立刻重播（不等下次 seed）。"""
    changed = skill_seed.set_opt_out(context.data_dir, False)
    context.out(
        "已清除 opt-out 标记" if changed else "opt-out 标记本来就不在（已是 opt-in 状态）"
    )
    if getattr(args, "sync", False):
        result = skill_seed.seed(context.data_dir, dry_run=False)
        context.out(f"已重新播种 {len(result['written'])} 条技能")
        if result["blocked"]:
            context.warn(f"{len(result['blocked'])} 条被 lint 拦下，未写盘")
        _index(context).invalidate()
    else:
        context.note("加 `--sync` 可立刻重播，否则等下一次 `orbit skills seed --apply`")
    context.emit({"ok": True, "opted_out": False, "changed": changed})
    return 0


# ── skills config / tap ─────────────────────────────────────────────────────


def run_skills_config(context: Context, args) -> int:
    """`skills config`：查看生效的技能索引配置，并启停单个技能。

    Hermes 的这个命令是**交互式**的（勾选启停）。CLI 没有 TUI 勾选循环，所以拆成
    ``--disable NAME`` / ``--enable NAME`` 两条显式动作——一个只有 TUI 才存在的开关，
    在 CLI 里等于不存在。

    停用名单落在 ``<data>/skills-disabled.json``，由 `SkillIndex._scan_all` **真的**消费
    （停用的技能不进索引）。为什么不写进 frontmatter：那是跟着技能目录走的内容，
    会让"我在本机关掉的技能"在同事那里也消失。
    """
    data_dir = context.data_dir
    index = _index(context)
    disable = str(getattr(args, "disable", "") or "").strip().removesuffix(".md")
    enable = str(getattr(args, "enable", "") or "").strip().removesuffix(".md")
    disabled = read_disabled(data_dir)
    if disable or enable:
        result = index.index()
        known = {record.name for record in result.records} | set(result.disabled)
        target = disable or enable
        if target not in known:
            raise CliError(
                f"索引里没有技能 {target!r}（`orbit skills list` 看看有什么）"
            )
        names = set(disabled)
        if disable:
            names.add(disable)
        else:
            names.discard(enable)
        write_disabled(data_dir, names)
        index.invalidate()
        action = "停用" if disable else "启用"
        context.out(f"已{action} {target}（索引已失效，下一轮装配生效）")
        disabled = frozenset(names)
    result = index.index()
    context.out(f"技能根：{', '.join(str(root) for _tier, root in index.active_roots()) or '（无）'}")
    context.out(
        f"索引：{len(result.records)} 条已装载 / {len(result.disabled)} 条停用 / "
        f"{len(result.errors)} 条读不出来 / {len(result.shadowed)} 条被同名压住"
    )
    context.out(
        f"缓存 TTL：{index.ttl:g} 秒（Hermes `_SKILLS_CACHE_TTL_SECONDS`）　"
        f"描述预算：{INDEX_DESCRIPTION_LIMIT} 字符"
    )
    if index.ttl != INDEX_CACHE_TTL_SECONDS:
        context.warn(f"TTL 被改成 {index.ttl}，与规格 {INDEX_CACHE_TTL_SECONDS} 不一致")
    taps = read_taps(data_dir)
    if taps:
        context.out(f"技能源（tap）：{len(taps)} 个 → " + "、".join(
            f"{row.get('name')}={row.get('path')}" for row in taps
        ))
    if disabled:
        context.out()
        context.table(("停用的技能",), [(name,) for name in sorted(disabled)])
    context.note("启停：`orbit skills config --disable <名字>` / `--enable <名字>`")
    context.emit(
        {
            "ok": True,
            "roots": [str(root) for _tier, root in index.active_roots()],
            "loaded": len(result.records),
            "disabled": sorted(result.disabled),
            "errors": len(result.errors),
            "shadowed": len(result.shadowed),
            "ttl": index.ttl,
            "description_limit": INDEX_DESCRIPTION_LIMIT,
            "taps": taps,
        }
    )
    return 0


def run_skills_tap_list(context: Context, args) -> int:
    """`skills tap list`：列出登记过的技能源（本地目录）。"""
    taps = read_taps(context.data_dir)
    if not taps:
        context.out("还没有技能源。`orbit skills tap add <目录>` 可以把一个本地技能目录登记进来")
        context.emit({"ok": True, "taps": []})
        return 0
    context.out(f"技能源：{len(taps)} 个")
    context.out()
    context.table(
        ("名字", "路径", "技能数", "存在"),
        [
            (
                str(row.get("name") or "-"),
                truncate(str(row.get("path") or ""), 56),
                str(row.get("skills", "-")),
                "是" if Path(str(row.get("path") or "")).is_dir() else "否",
            )
            for row in taps
        ],
    )
    context.emit({"ok": True, "taps": taps})
    return 0


def run_skills_tap_add(context: Context, args) -> int:
    """`skills tap add <目录>`：把一个**本地目录**登记为额外技能源。

    远端仓库（``owner/repo``、URL）**明确拒绝**：本机无网络，登记一个永远拉不下来的源
    等于撒谎。做法是先把仓库 clone 到本地，再登记那个目录——这也让 tap 真的可用。
    """
    raw = str(getattr(args, "repo", "") or "").strip()
    if not raw:
        raise CliError("需要一个本地目录路径（远端仓库请先 clone 到本地）")
    if raw.lower().startswith(("http://", "https://", "git@", "ssh://")):
        raise CliError(
            f"远端技能源未实现：本机无网络，无法拉取 {raw}。"
            "请先把仓库 clone 到本地，再 `orbit skills tap add <本地目录>`"
        )
    directory = Path(raw).expanduser()
    if not directory.is_dir():
        raise CliError(
            f"{directory} 不是一个目录。远端仓库（owner/repo）请先 clone 到本地再登记"
        )
    count = sum(1 for _path in directory.rglob("SKILL.md") if _path.is_file())
    if count == 0:
        raise CliError(f"{directory} 下没有 SKILL.md——那不是一个技能目录")
    taps = read_taps(context.data_dir)
    name = directory.name or str(directory)
    for row in taps:
        if Path(str(row.get("path") or "")) == directory:
            raise CliError(f"已经登记过：{directory}")
    taps.append(
        {
            "name": name,
            "path": str(directory),
            "skills": count,
            "added_at": _now_iso(),
        }
    )
    write_taps(context.data_dir, taps)
    _index(context).invalidate()
    context.out(f"已登记技能源：{name} → {directory}（{count} 条技能）")
    context.note("tap 以**外部级**优先级参与扫描（低于项目级与用户级），同名技能会被压住并留痕")
    context.emit({"ok": True, "taps": taps})
    return 0


def run_skills_tap_remove(context: Context, args) -> int:
    """`skills tap remove <名字>`：移除一个技能源（只改登记表，不删目录）。"""
    raw = str(getattr(args, "name", "") or "").strip()
    if not raw:
        raise CliError("需要一个技能源名字（`orbit skills tap list` 第一列）")
    taps = read_taps(context.data_dir)
    remaining = [
        row
        for row in taps
        if raw not in (str(row.get("name") or ""), str(row.get("path") or ""))
    ]
    if len(remaining) == len(taps):
        raise CliError(f"没有这个技能源：{raw}")
    write_taps(context.data_dir, remaining)
    _index(context).invalidate()
    context.out(f"已移除技能源：{raw}（目录本身没动）")
    context.emit({"ok": True, "taps": remaining})
    return 0


# ── 接线 ────────────────────────────────────────────────────────────────────


def _bind(path: str, handler: Any) -> None:
    """登记某条路径的实现；表已组装时顺手填到 ``Command`` 上（与 `skill_extra.py` 同）。"""
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


def _add_options(path: str, options: list[Option]) -> bool:
    """给**表里已有的**命令补选项（只增不减，幂等）。返回是否有改动。"""
    parts = path.split()
    node = REGISTRY.get(parts[0])
    for part in parts[1:]:
        if node is None:
            return False
        node = node.child(part)
    if node is None:
        return False
    changed = False
    for option in options:
        if any(set(option.flags) & set(existing.flags) for existing in node.options):
            continue
        node.options.append(option)
        changed = True
    return changed


def _policy_options() -> list[Option]:
    """冲突策略三开关。默认全 False = ``skip``（见 `app/skill_sync.py` docstring）。"""
    return [
        Option(("--ours",), "冲突时保留本地版本（远端的改动会被丢弃）", action="store_true"),
        Option(("--theirs",), "冲突时取远端版本（覆盖本地）", action="store_true"),
        Option(("--skip",), "冲突时跳过并列出（**默认**）", action="store_true"),
    ]


def _upgrade_sync() -> bool:
    """给 ``sync push/pull/now`` 补位置参数与冲突策略（表里没有这些）。"""
    changed = _add_options("sync push", [Option(("dest",), "bundle 落点（省略 = <data>/sync/<时间戳>）",
                                               metavar="DEST", nargs="?")])
    changed |= _add_options(
        "sync pull",
        [
            Option(("bundle",), "bundle 目录或 zip（省略 = 本机最近的 bundle）",
                   metavar="BUNDLE", nargs="?"),
            *_policy_options(),
        ],
    )
    changed |= _add_options("sync now", _policy_options())
    return bool(changed)


def _upgrade_skills_config() -> bool:
    """给 ``skills config`` 补 ``--enable`` / ``--disable``（表里它是无参交互命令）。"""
    return bool(
        _add_options(
            "skills config",
            [
                Option(("--disable",), "停用某个技能（不进索引）", metavar="NAME"),
                Option(("--enable",), "重新启用某个技能", metavar="NAME"),
            ],
        )
    )


def _skill_extra2_commands() -> list[Command]:
    """`skills` 下两条表里**没有**的子命令（seed / coverage）。

    Hermes 命令面里没有它们：``hermes skills`` 的内置技能由安装器在装的时候播好，
    没有"按需播种"这个概念。Orbit 没有安装器阶段（技能库一开始是空的），
    所以把这件事做成显式命令——否则 91 条技能永远进不来。
    """
    return [
        Command(
            name="seed",
            help="把内置技能目录（21 类目 / 91 技能）播进技能库，默认只预览",
            handler=run_skills_seed,
            group="ext",
            parity="P1",
            hermes="（Hermes 无此子命令：内置技能由安装器播种，见 skills_sync.py）",
            state="done",
            options=[
                Option(("name",), "只播这些技能（可重复）", metavar="NAME", nargs="*"),
                Option(("--category",), "只播某个类目（如 creative）", metavar="CATEGORY"),
                Option(("--apply",), "真的写盘（默认 dry-run）", action="store_true"),
                Option(("--skip-unavailable",),
                       "跳过依赖缺失的技能（真实探测命令/环境变量/Python 包）",
                       action="store_true"),
                Option(("--force",), "覆盖已存在的技能目录（会盖掉本地改动）",
                       action="store_true"),
            ],
        ),
        Command(
            name="coverage",
            help="技能库规模：类目 / 技能的 (已播, 总数) 与缺口",
            handler=run_skills_coverage,
            group="ext",
            parity="P1",
            hermes="（Hermes 无此子命令：它的技能库规模是固定的）",
            state="done",
        ),
    ]


def mount_skill_extra2() -> list[str]:
    """给 `skills` 补 seed / coverage（**幂等**）。"""
    parent = REGISTRY.get("skills")
    if parent is None:
        return []
    mounted: list[str] = []
    for command in _skill_extra2_commands():
        if parent.child(command.name) is not None:
            continue
        parent.subcommands.append(command)
        mounted.append(command.name)
    return mounted


def _mount_everything() -> None:
    """把本模块负责的路径一次性接上。两条导入顺序下都成立、都可重复调用。"""
    mount_skill_extra2()
    _upgrade_sync()
    _upgrade_skills_config()
    _bind("sync status", run_sync_status)
    _bind("sync push", run_sync_push)
    _bind("sync pull", run_sync_pull)
    _bind("sync now", run_sync_now)
    _bind("sync enable", run_sync_enable)
    _bind("sync disable", run_sync_disable)
    _bind("sync device", run_sync_device)
    _bind("sync propose", run_sync_propose)
    _bind("skills audit", run_skills_audit)
    _bind("skills diff", run_skills_diff)
    _bind("skills list-modified", run_skills_list_modified)
    _bind("skills reset", run_skills_reset)
    _bind("skills repair-official", run_skills_repair_official)
    _bind("skills opt-out", run_skills_opt_out)
    _bind("skills opt-in", run_skills_opt_in)
    _bind("skills config", run_skills_config)
    _bind("skills tap list", run_skills_tap_list)
    _bind("skills tap add", run_skills_tap_add)
    _bind("skills tap remove", run_skills_tap_remove)


def _install_assembly_hook() -> bool:
    """在「表组装」这一个缝上补挂（照 `skill_extra.py` / `ui_extra.py`）。"""
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
#: 理由：`skills publish` 需要注册表协议与账号，`skills tap` 的远端形态需要网络。
UNBOUND_SUBCOMMANDS: tuple[str, ...] = (
    "skills publish",
)

__all__ = [
    "UNBOUND_SUBCOMMANDS",
    "mount_skill_extra2",
    "run_skills_audit",
    "run_skills_config",
    "run_skills_coverage",
    "run_skills_diff",
    "run_skills_list_modified",
    "run_skills_opt_in",
    "run_skills_opt_out",
    "run_skills_repair_official",
    "run_skills_reset",
    "run_skills_seed",
    "run_skills_tap_add",
    "run_skills_tap_list",
    "run_skills_tap_remove",
    "run_sync_device",
    "run_sync_disable",
    "run_sync_enable",
    "run_sync_now",
    "run_sync_propose",
    "run_sync_pull",
    "run_sync_push",
    "run_sync_status",
]
