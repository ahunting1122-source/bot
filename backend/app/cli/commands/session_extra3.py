"""``sessions`` 剩余 10 条 + ``plugins install/search/pack/update`` 的收尾接线。

覆盖两条矩阵条目：

- **MATRIX 10.13**：``sessions optimize / clean-markers / optimize-storage / repair /
  repair-routing / recover / pin / unpin / pinned / retitle-skills``。真值计算全在
  ``app/session_maintenance.py``（那里没有一句 print），本模块只做"打印 + 退出码"。
- **MATRIX 13.9**：``plugins install <zip>`` / ``plugins search``（本地 hub）/
  ``plugins pack zip`` / ``plugins update``（按本地 source 重拉）。

## 为什么 plugins 的 handler 也写在这个文件里

``app/cli/commands/plugins.py`` 与 ``plugins_extra.py`` 都是禁改区：前者持有
``install`` / ``search`` / ``pack`` / ``update`` 四条命令的 handler 与整棵命令树，
后者持有 ``EXTRA_SUBCOMMANDS`` 与 ``attach()``。本轮要补的恰好就是这四条命令的行为
（zip 安装、本地 hub 搜索、zip 打包、按本地 source 重新拉），改不了那两处文件，
于是走仓库里既有的第三条路：**运行期接管**——``register_handler`` 声明路径，
再直接改注册表里那个节点的 ``handler`` / ``state`` / ``options``
（``plugins_extra._claim_capabilities`` 与 ``memory_extra2._bind`` 都是这么做的）。

文件名里带 ``session_extra3`` 只是排期顺序，不是功能边界；把它单独开一个
``plugins_extra2.py`` 反而会让"13.9 的收尾在哪"变成两个地方。

## 一条必须说明的接管边界

``plugins install`` 的**目录安装与远程报错**仍然由 ``plugins.py::run_install`` 执行
（本模块只是转发），所以既有行为、既有文案一个字都没变；本模块只在
"标识符是一个真实存在的 ``.zip`` 文件"时分流到自己的实现。这样做的理由是：
zip 支持是**加法**，不该顺手重写一条已经验证过的路径。

``plugins search`` / ``plugins update`` 则是真的换了实现（要加本地 hub 目录、要按
记录的 source 重拉），但仍然保留原实现里的两句关键文案（"社区索引需要网络"、
"未实现：远程 git pull"）——远程那条路本来就没实现，报告里不能消失。

## 全部 ``--apply`` 类命令的共同约定

默认 dry-run（只算不改）→ 打印"将改什么、改多少行"；``--apply`` → 打印
``cursor.rowcount`` 的真实值；每一行被改的记录都进报告。护栏不靠用户记得加参数。

## 接线顺序（给统一接线的人看）

本模块**要排在 ``hermes_parity`` 之后**（与 ``session_extra2`` 同一组）：它要把子命令
挂到表里已存在的父节点上，需要表先组装好。顺序反了也不会把 CLI 弄挂——
:func:`_bind_sessions` 在父命令还没登记时只登记 ``_table.HANDLERS`` 并返回空列表
（不抛错），表组装时 ``Spec.build()`` 会取到这些 handler 并标 ``done``；此时新增的
``--apply`` / ``--keep-days`` 等选项还没补上，接线方在 ``__init__.py`` 末尾再调一次
:func:`wire` 即可补齐。

## 与声明表**选项**的差异（help 一起改过，不是悄悄变）

- ``recover``：声明表带的是 Hermes"按来源重建会话"的 7 个选项（``--source`` /
  ``--output`` / ``--work-dir`` …），与本实现无关，**整组移除**，换成
  ``--apply`` / ``--stale-minutes``；
- ``repair``：移除 ``--no-backup``（REINDEX 不破坏数据，没有需要备份的东西）；
- ``pin`` / ``unpin``：位置参数从"一个"改成"可多个"（空格或逗号分隔）；
- 其余新增项（``--apply`` / ``--keep-days`` / ``--note`` / ``--root`` / ``--hub`` /
  ``--out``）都是纯加法，不影响既有调用。
"""

from __future__ import annotations

import json
import re
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from app import plugins
from app import session_maintenance as sm
from app.cli.commands import plugins as plugins_cli
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context, truncate
from app.cli.spec import REGISTRY, Command, Handler, Option

#: ``plugins install`` 之后记录本地 source 的文件（在 ``<data_dir>/`` 下）。
#: 记在数据目录而不是插件目录里：插件目录会被 ``plugins remove`` 删掉、被
#: ``plugins update`` 覆盖，记录跟着一起死就永远学不到"它是从哪来的"。
SOURCES_FILE = "plugin-sources.json"

#: zip 解压总量上限（防 zip 炸弹：压缩比 1000:1 的包解出来能塞满磁盘）。
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024

#: 打包时跳过的目录/文件（缓存与 VCS 元数据不该进发布包）。
PACK_SKIP_DIRS = {"__pycache__", ".git", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
PACK_SKIP_SUFFIXES = {".pyc", ".pyo"}


class ZipSlipError(CliError):
    """zip 里有成员会写到目标目录之外——拒绝整包解压。"""


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _size(value: int) -> str:
    """人读的字节数。表格里 6839842 这种数字看不出量级。"""
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):.1f} MB"
    if value >= 1024:
        return f"{value / 1024:.1f} KB"
    return f"{value} B"


def _flag(args: Any, name: str) -> bool:
    return bool(getattr(args, name, False))


def _int_option(args: Any, name: str, default: int) -> int:
    raw = getattr(args, name, None)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


# ── sessions optimize ───────────────────────────────────────────────────────


def run_optimize(context: Context, args) -> int:
    """``sessions optimize``：三个库各 ``PRAGMA optimize`` + ``VACUUM``，报前后字节数。"""
    report = sm.optimize(context.data_dir)
    context.out(f"整理 {len(report.databases)} 个库（PRAGMA optimize + VACUUM）")
    context.out()
    context.table(
        ("库", "整理前", "整理后", "省下", "说明"),
        [
            (
                item.name,
                _size(item.before),
                _size(item.after),
                _size(item.saved),
                item.note or "-",
            )
            for item in report.databases
        ],
    )
    context.out()
    context.out(
        f"合计：{_size(report.total_before)} → {_size(report.total_after)}，"
        f"省下 {_size(report.total_saved)}"
    )
    context.out("说明：体积按主文件 + WAL/SHM 实测算（只算主文件会骗人）。")
    context.emit({"ok": True, **report.as_dict()})
    return 0


# ── sessions optimize-storage ───────────────────────────────────────────────


def run_optimize_storage(context: Context, args) -> int:
    """``sessions optimize-storage``：``VACUUM INTO`` 重建 + 清理过期 ``task_events``。

    默认 dry-run。``-y/--yes`` 视同 ``--apply``（"跳过确认"在这里就是"真的做"，
    两个参数给同一个语义，免得用户以为 ``-y`` 会动手结果什么也没发生）。
    """
    apply = _flag(args, "apply") or _flag(args, "yes")
    keep_days = _int_option(args, "keep_days", sm.DEFAULT_KEEP_DAYS)
    report = sm.optimize_storage(
        context.data_dir,
        apply=apply,
        keep_days=keep_days,
        vacuum=not _flag(args, "no_vacuum"),
        backup=not _flag(args, "no_backup"),
    )
    context.out(f"目标库：{report.path}")
    context.out(f"保留策略：删除 {report.keep_days} 天前的 task_events（cutoff {report.cutoff}）")
    if report.events_total:
        context.out(
            f"task_events 共 {report.events_total} 行，其中超过保留期 {report.pruned} 行"
        )
    else:
        context.out("task_events 是空表")
    if report.note:
        context.out(report.note)
    if apply:
        context.out()
        context.out(f"已删除 {report.pruned} 行（实际 rowcount）")
        context.out(
            f"体积：{_size(report.before)} → {_size(report.after)}，省下 {_size(report.saved)}"
            + ("（已 VACUUM INTO 重建）" if report.vacuum else "（未 VACUUM，页还留在文件里）")
        )
        if report.backup:
            context.out(f"改动前备份：{report.backup}")
    else:
        context.out()
        context.out(f"dry-run：将删除 {report.pruned} 行，一个字节都没动。")
        context.out(
            f"体积预估（按副本实测）：{_size(report.before)} → {_size(report.after)}，"
            f"预计省下 {_size(report.saved)}"
        )
        context.out("加 --apply 才会真的删。")
    context.emit({"ok": True, **report.as_dict()})
    return 0


# ── sessions repair ─────────────────────────────────────────────────────────


def run_repair(context: Context, args) -> int:
    """``sessions repair``：``integrity_check`` + ``foreign_key_check``，只报不改。

    ``--apply`` 只做 ``REINDEX``——索引是纯派生数据，重建只会变对；其余"修复"
    （重建表、丢坏行）都要先知道业务语义，猜错就是永久丢数据。
    """
    check_only = _flag(args, "check_only")
    apply = _flag(args, "apply") and not check_only
    report = sm.repair(context.data_dir, apply=apply)
    context.out(f"检查 {len(report.checks)} 个库（integrity_check + foreign_key_check）")
    context.out()
    context.table(
        ("库", "结论", "integrity", "外键问题", "索引", "说明"),
        [
            (
                item.database,
                "正常" if item.ok else "有问题",
                "、".join(item.integrity) or "-",
                str(len(item.foreign_keys)) + " 条",
                str(item.indexes),
                item.note or "-",
            )
            for item in report.checks
        ],
    )
    problems = [item for item in report.checks if not item.ok]
    for item in problems:
        for row in item.foreign_keys:
            context.warn(f"{item.database} 外键问题：{row}")
    context.out()
    if apply:
        context.out(f"已 REINDEX {report.reindexed} 个索引（唯一的自动动作）。")
    else:
        context.out("dry-run：没有改动任何库。加 --apply 只做 REINDEX，其余问题需要人工判断。")
    context.emit({"ok": report.ok, "apply": apply, **report.as_dict()})
    return 0


# ── sessions repair-routing ─────────────────────────────────────────────────


def run_repair_routing(context: Context, args) -> int:
    """``sessions repair-routing``：``tasks.conversation_id`` 悬空的行，逐行报将改什么。"""
    apply = _flag(args, "apply")
    report = context.run(sm.repair_routing(context.store, apply=apply))
    context.out(f"任务总数 {report.tasks} 个；悬空路由 {len(report.orphans)} 个")
    if report.note:
        context.out(report.note)
    if report.orphans:
        context.table(
            ("任务", "旧 conversation_id", "目标", "任务目标（截断）"),
            [
                (
                    item.task_id[:12],
                    item.conversation_id[:12] or "（空）",
                    (item.new_conversation_id[:12] or "-") + f"（{item.action}）",
                    truncate(item.goal, 40),
                )
                for item in report.orphans
            ],
        )
    context.out()
    if apply:
        context.out(f"实际改了 {report.changed} 行（逐行见上表）。")
    else:
        context.out(f"dry-run：将改 {len(report.orphans)} 行，一个字节都没动。加 --apply 才改。")
    if _int_option(args, "max_gap_seconds", 0):
        context.out("说明：--max-gap-seconds 不参与本实现（按'最近会话'重挂，不按空闲间隔判定）。")
    context.emit({"ok": True, **report.as_dict()})
    return 0


# ── sessions recover ────────────────────────────────────────────────────────


def run_recover(context: Context, args) -> int:
    """``sessions recover``：僵尸 ``RUNNING`` 任务（超时没更新）→ ``FAILED``。

    与声明表 help 的语义不同（那里写的是"把会话数据重建到干净目录"）：本仓库没有
    "按来源重建会话"的真值来源，硬做只能造假数据；僵尸任务则有一条硬真值
    （``tasks.status`` + ``updated_at``），所以换成了它。
    """
    apply = _flag(args, "apply")
    minutes = _int_option(args, "stale_minutes", sm.DEFAULT_STALE_MINUTES)
    report = context.run(sm.recover(context.store, apply=apply, stale_minutes=minutes))
    context.out(
        f"RUNNING 任务 {report.running} 个；超过 {report.stale_minutes} 分钟没更新的 "
        f"{len(report.zombies)} 个（cutoff {report.cutoff}）"
    )
    if report.zombies:
        context.table(
            ("任务", "闲置（分钟）", "上次更新", "目标", "动作"),
            [
                (
                    item.task_id[:12],
                    "时间戳坏" if item.idle_minutes < 0 else f"{item.idle_minutes:.0f}",
                    item.updated_at[:19] or "-",
                    truncate(item.goal, 32),
                    item.action,
                )
                for item in report.zombies
            ],
        )
    context.out()
    if apply:
        context.out(f"实际改了 {report.changed} 行（status=FAILED, end_reason=recovered）。")
        context.out(
            "说明：tasks 表没有 end_reason 列（storage.py 是禁改区），"
            "所以 end_reason 落在 tasks.error、tasks.waiting.end_reason 与一条 recovered 事件里。"
        )
    else:
        context.out(f"dry-run：将改 {len(report.zombies)} 行，一个字节都没动。加 --apply 才改。")
    context.emit({"ok": True, **report.as_dict()})
    return 0


# ── sessions clean-markers ──────────────────────────────────────────────────


def run_clean_markers(context: Context, args) -> int:
    """``sessions clean-markers``：清理正文开头的上下文压缩标记（默认 dry-run）。"""
    apply = _flag(args, "apply") and not _flag(args, "dry_run")
    backup = None
    if apply and not _flag(args, "no_backup"):
        backup = sm.backup_database(context.db_path, context.data_dir)
        context.out(f"改动前备份：{backup}")
    report = context.run(sm.clean_markers(context.store, apply=apply, backup=backup))
    context.out(
        f"扫描 {report.scanned} 条消息；命中标记 {len(report.hits)} 条"
        f"（前缀来自 app/session_search.py 的 _COMPACTION_PREFIXES）"
    )
    if report.hits:
        context.table(
            ("消息", "会话", "标记", "动作", "字符数"),
            [
                (
                    item.message_id[:12],
                    item.conversation_id[:12],
                    item.marker,
                    item.action,
                    f"{item.before_chars}→{item.after_chars}",
                )
                for item in report.hits
            ],
        )
    if report.note:
        context.out(report.note)
    context.out()
    if apply:
        context.out(f"实际改动：剥掉标记 {report.stripped} 条、整条删除 {report.deleted} 条。")
    else:
        context.out(
            f"dry-run：将剥掉标记 {len([h for h in report.hits if '剥掉' in h.action])} 条、"
            f"整条删除 {len([h for h in report.hits if '删除' in h.action])} 条，一个字节都没动。"
        )
    context.emit({"ok": True, **report.as_dict()})
    return 0


# ── sessions retitle-skills ─────────────────────────────────────────────────


def run_retitle_skills(context: Context, args) -> int:
    """``sessions retitle-skills``：按 SKILL.md frontmatter 的 ``name`` 重写技能索引名。

    与声明表 help 的语义不同（那里写的是"按技能重命名**会话**标题"）：本仓库没有
    "会话该叫什么"的真值来源，但"技能该叫什么"有——frontmatter 的 ``name``。
    所以这条命令重命名的是 ``skills`` 表的索引名（磁盘是真值，表是索引）。
    """
    apply = _flag(args, "apply")
    raw_root = str(getattr(args, "root", "") or "").strip()
    root = Path(raw_root).expanduser() if raw_root else plugins.skills_root(context.settings)
    limit = _int_option(args, "limit", 0)
    report = context.run(sm.retitle_skills(context.store, root, apply=apply, limit=limit))
    context.out(f"技能根：{report.root}（磁盘 {report.files} 个目录 / 索引 {report.rows} 行）")
    if report.renames:
        context.table(
            ("目录", "索引旧名", "frontmatter 名", "动作"),
            [
                (item.directory, item.old_name, item.new_name, item.action)
                for item in report.renames
            ],
        )
    else:
        context.out("索引名与 frontmatter 全部一致，无需重命名。")
    if report.orphans:
        context.out("磁盘上有、索引里没有（需要先跑一次技能索引同步）：" + "、".join(report.orphans))
    context.out()
    if apply:
        context.out(f"实际改了 {report.changed} 行。")
    else:
        context.out(f"dry-run：将改 {len(report.renames)} 行，一个字节都没动。加 --apply 才改。")
    context.emit({"ok": True, **report.as_dict()})
    return 0


# ── sessions pin / unpin / pinned ───────────────────────────────────────────


def _ids(args: Any) -> list[str]:
    return sm.split_ids(getattr(args, "session_ids", None))


def run_pin(context: Context, args) -> int:
    """``sessions pin <id|前缀...>``：钉住会话（免自动清理）。"""
    ids = _ids(args)
    if not ids:
        raise CliError("用法：orbit sessions pin <会话ID或唯一前缀>[,更多] [--note 备注]")
    note = str(getattr(args, "note", "") or "")
    outcome = context.run(sm.pin(context.store, ids, note=note))
    for session_id in outcome.pinned:
        context.out(f"已钉住：{session_id}")
    for raw, reason in outcome.skipped:
        context.warn(f"{raw}：{reason}")
    context.out(f"本次钉住 {len(outcome.pinned)} 个（表 {sm.PIN_TABLE}，幂等 upsert）。")
    context.emit({"ok": bool(outcome.pinned), **outcome.as_dict()})
    return 0 if outcome.pinned else 1


def run_unpin(context: Context, args) -> int:
    """``sessions unpin <id|前缀...>``：取消钉住。"""
    ids = _ids(args)
    if not ids:
        raise CliError("用法：orbit sessions unpin <会话ID或唯一前缀>[,更多]")
    outcome = context.run(sm.unpin(context.store, ids))
    for session_id in outcome.pinned:
        context.out(f"已取消：{session_id}")
    for raw, reason in outcome.skipped:
        context.warn(f"{raw}：{reason}")
    context.out(f"本次取消 {len(outcome.pinned)} 个。")
    context.emit({"ok": bool(outcome.pinned), **outcome.as_dict()})
    return 0 if outcome.pinned else 1


def run_pinned(context: Context, args) -> int:
    """``sessions pinned``：列出被钉住的会话，并检出**悬空 pin**（会话已删）。"""
    entries = context.run(sm.pinned(context.store))
    dangling = [item for item in entries if item.dangling]
    context.out(f"钉住 {len(entries)} 个会话（其中悬空 {len(dangling)} 个）")
    if entries:
        context.table(
            ("会话", "标题", "钉住时间", "备注", "状态"),
            [
                (
                    item.session_id[:12],
                    truncate(item.title, 24) or "（会话已不存在）",
                    item.pinned_at[:19],
                    truncate(item.note, 20) or "-",
                    "悬空" if item.dangling else "正常",
                )
                for item in entries
            ],
        )
    else:
        context.out("还没有钉住任何会话（`orbit sessions pin <id>`）。")
    for item in dangling:
        context.warn(f"悬空 pin：{item.session_id}（会话已不存在，`sessions unpin {item.session_id}` 可清理）")
    context.emit(
        {"ok": True, "pins": [item.as_dict() for item in entries], "dangling": len(dangling)}
    )
    return 0


# ── plugins：zip 解压与打包 ─────────────────────────────────────────────────


def _member_path(info: zipfile.ZipInfo) -> PurePosixPath:
    """zip 成员名统一成 POSIX 相对路径（Windows 打包的 zip 常带反斜杠）。"""
    return PurePosixPath(info.filename.replace("\\", "/"))


def _validate_members(infos: list[zipfile.ZipInfo], destination: Path) -> None:
    """**逐成员**校验路径不逃出 ``destination``（zip slip 的唯一防线）。

    拦四种真实攻击面：
      1. 绝对路径（``/etc/passwd``）与盘符（``C:\\Windows\\...``）；
      2. ``..`` 段（``../../evil.txt``）；
      3. 符号链接成员（zip 能存 symlink，解出来指向目录外）；
      4. 归一化之后仍然落在目录外的路径（``a/../../b``）。
    前三种靠字符串就能判，第四种必须 ``resolve()``——前三种都过了的路径仍可能
    因为软链接/大小写等原因归一化到外面。
    """
    total = 0
    for info in infos:
        raw = info.filename
        relative = _member_path(info)
        if relative.is_absolute() or re.match(r"^[A-Za-z]:", raw) or ".." in relative.parts:
            raise ZipSlipError(f"拒绝解压：成员 {raw!r} 的路径越出目标目录（zip slip）")
        mode = (info.external_attr >> 16) & 0o170000
        if mode == stat.S_IFLNK:
            raise ZipSlipError(f"拒绝解压：成员 {raw!r} 是符号链接（可指向目标目录之外）")
        target = (destination / relative).resolve()
        if not target.is_relative_to(destination):
            raise ZipSlipError(f"拒绝解压：成员 {raw!r} 归一化后落在 {target}（目标目录之外）")
        total += int(info.file_size or 0)
    if total > MAX_UNCOMPRESSED_BYTES:
        raise ZipSlipError(
            f"拒绝解压：解压后共 {_size(total)}，超过上限 {_size(MAX_UNCOMPRESSED_BYTES)}（zip 炸弹？）"
        )


def safe_extract(archive: Path, destination: Path) -> list[str]:
    """解压 zip 到 ``destination``，返回落盘成员的相对路径列表。

    **两遍**：先把所有成员校验一遍，全过了才开始写。一遍写一遍校验的做法在
    "第 5 个成员越界"时会在目标目录里留下前 4 个文件——那就不叫"拒绝解压"了。
    """
    destination = Path(destination).resolve()
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        _validate_members(infos, destination)
        destination.mkdir(parents=True, exist_ok=True)
        written: list[str] = []
        for info in infos:
            relative = _member_path(info)
            if info.is_dir():
                continue
            target = (destination / relative).resolve()
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, open(target, "wb") as sink:
                shutil.copyfileobj(source, sink)
            written.append(str(relative))
    return written


def locate_plugin_dir(root: Path, archive: Path) -> Path:
    """在解压结果里找插件目录（含 ``plugin.yaml`` 的那一层）。"""
    if (root / "plugin.yaml").is_file():
        return root
    candidates = sorted(
        item for item in root.iterdir() if item.is_dir() and (item / "plugin.yaml").is_file()
    )
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise CliError(f"{archive} 里没有 plugin.yaml（这个 zip 不是一个插件包）")
    names = "、".join(item.name for item in candidates)
    raise CliError(f"{archive} 里有多个插件目录：{names}（一次只能装一个）")


def pack_members(source: Path) -> list[str]:
    """要打进包的文件（相对 POSIX 路径，排序后输出以便可复现）。"""
    members: list[str] = []
    for path in sorted(source.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        if any(part in PACK_SKIP_DIRS for part in relative.parts):
            continue
        if path.suffix in PACK_SKIP_SUFFIXES or path.name == ".DS_Store":
            continue
        members.append(relative.as_posix())
    return members


# ── plugins install（zip 分支）/ search / pack zip / update ─────────────────


def _sources_path(context: Context) -> Path:
    return context.data_dir / SOURCES_FILE


def load_sources(context: Context) -> dict[str, dict[str, str]]:
    """读"插件名 → 本地 source"记录（坏了就当空表，不让维护命令因此失败）。"""
    path = _sources_path(context)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(key): dict(value) for key, value in data.items() if isinstance(value, dict)}


def remember_source(context: Context, name: str, source: Path, kind: str) -> None:
    """记下本地 source（``plugins update`` 靠它真的重拉，而不是"未实现"）。"""
    data = load_sources(context)
    data[name] = {"source": str(source), "kind": kind, "recorded_at": _now_iso()}
    path = _sources_path(context)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _install_from_zip(context: Context, args, archive: Path) -> int:
    """从本地 zip 装插件：解压到临时目录 → 走 ``plugins.install_local`` 的同一套门禁。"""
    if not archive.is_file():
        raise CliError(f"本地安装源不存在：{archive}")
    if getattr(args, "ref", None):
        raise CliError("--ref 只对远程安装有意义（本机无网络，远程安装未实现）")
    want_enable = _flag(args, "enable")
    want_disable = _flag(args, "no_enable")
    if want_enable and want_disable:
        raise CliError("--enable 与 --no-enable 不能同时给")
    enable = True if want_enable else (False if want_disable else None)
    context.data_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="orbit-zip-", dir=str(context.data_dir)) as workspace:
        root = Path(workspace) / "unpacked"
        written = safe_extract(archive, root)
        plugin_dir = locate_plugin_dir(root, archive)
        context.out(f"已解压 {len(written)} 个成员（zip slip 校验通过）")
        try:
            result = plugins.install_local(
                plugin_dir,
                context.settings,
                force=_flag(args, "force"),
                enable=enable,
            )
        except plugins.PluginGuardError as exc:
            raise CliError(str(exc)) from exc
        except plugins.PluginError as exc:
            raise CliError(str(exc)) from exc
    remember_source(context, result.name, archive, "zip")
    context.out(f"已安装：{result.name} → {result.target}")
    context.out(f"扫描裁决：{result.verdict}（{result.findings} 项发现）· {result.reason}")
    context.out("状态：" + ("已启用" if result.enabled else "已装但禁用（用 enable 开启）"))
    context.out(f"来源：{archive}（zip，已记入 {SOURCES_FILE}，`plugins update` 可据此重拉）")
    context.emit({"ok": True, **result.as_dict(), "source": str(archive), "kind": "zip"})
    return 0


def run_plugins_install(context: Context, args) -> int:
    """``plugins install``：目录与远程报错仍走原实现；本地 ``.zip`` 走本模块。"""
    identifier = str(getattr(args, "identifier", "") or "").strip()
    source = Path(identifier).expanduser() if identifier else None
    is_zip = (
        source is not None
        and source.suffix.lower() == ".zip"
        and not plugins_cli._is_remote(identifier)  # noqa: SLF001 —— 同包内复用远程判定
    )
    if is_zip:
        assert source is not None
        return _install_from_zip(context, args, source)
    code = plugins_cli.run_install(context, args)
    if code == 0 and source is not None and source.is_dir():
        try:
            manifest = plugins.load_manifest(source / "plugin.yaml", root=source)
        except plugins.PluginError:
            pass
        else:
            remember_source(context, manifest.name, source, "dir")
    return code


def run_plugins_search(context: Context, args) -> int:
    """``plugins search``：本地已装插件 + 本地 hub 目录里按名字/描述/tag 搜。

    远程索引如实报未实现（``--refresh`` 直接报错）。本地 hub 目录默认是
    ``<data_dir>/plugin-hub/``（有就用），也可以用 ``--hub`` 指一个未安装的插件源码树。
    """
    if _flag(args, "refresh"):
        raise CliError(
            "未实现：--refresh 需要联网重取社区插件索引；本机无网络，"
            "本轮 search 只在本地（已装插件 + 本地 hub 目录）里匹配"
        )
    term = str(getattr(args, "term", "") or "").strip().lower()
    capability = str(getattr(args, "capability", "") or "").strip().lower()
    discovered = plugins.discover(settings=context.settings)
    manifests = list(discovered.manifests)

    hub_roots: list[Path] = []
    raw_hub = str(getattr(args, "hub", "") or "").strip()
    if raw_hub:
        hub_roots.append(Path(raw_hub).expanduser())
    else:
        default_hub = context.data_dir / "plugin-hub"
        if default_hub.is_dir():
            hub_roots.append(default_hub)

    hub_manifests: list[plugins.PluginManifest] = []
    for root in hub_roots:
        if not root.is_dir():
            context.warn(f"hub 目录不存在：{root}")
            continue
        for manifest_path in sorted(root.rglob("plugin.yaml"))[:200]:
            try:
                hub_manifests.append(plugins.load_manifest(manifest_path, root=manifest_path.parent))
            except plugins.PluginError as exc:
                context.warn(f"{manifest_path}：{exc}")

    hits: list[dict[str, Any]] = []
    for origin, group in (("installed", manifests), ("hub", hub_manifests)):
        for manifest in group:
            tags = manifest.raw.get("tags") if isinstance(manifest.raw, dict) else None
            tag_text = " ".join(str(item) for item in tags) if isinstance(tags, list) else ""
            haystack = " ".join(
                [manifest.name, manifest.description, manifest.label, manifest.kind, tag_text]
            ).lower()
            if term and term not in haystack:
                continue
            declared = plugins_cli._declared_capabilities(manifest)  # noqa: SLF001
            if capability and not any(capability in item for item in declared):
                continue
            hits.append(
                {
                    "name": manifest.name,
                    "version": manifest.version,
                    "kind": manifest.kind,
                    "origin": origin,
                    "path": str(manifest.directory),
                    "description": manifest.description.splitlines()[0]
                    if manifest.description
                    else "",
                    "capabilities": declared,
                    "tags": tag_text,
                }
            )

    context.out(
        f"搜索范围：本地已装插件 {len(manifests)} 个 + 本地 hub 目录 {len(hub_manifests)} 个"
        "（社区索引需要网络，未实现）"
    )
    if hits:
        context.table(
            ("插件", "版本", "来源", "声明能力", "说明"),
            [
                (
                    row["name"],
                    row["version"] or "-",
                    "已装" if row["origin"] == "installed" else "hub",
                    "、".join(row["capabilities"]) or "-",
                    truncate(row["description"], 40),
                )
                for row in hits
            ],
        )
    else:
        context.out("没有匹配的插件。")
    context.emit({"ok": True, "term": term, "capability": capability, "results": hits})
    return 0


def run_plugins_pack_zip(context: Context, args) -> int:
    """``plugins pack zip <插件目录|已装插件名>``：打成 ``<name>-<version>.zip``。

    包内布局是 ``<name>-<version>/…``（单层目录），所以 ``plugins install <zip>``
    解压后能直接认出插件目录——往返可用是这条命令唯一的验收判据。
    """
    raw = str(getattr(args, "source", "") or "").strip()
    if not raw:
        raise CliError("用法：orbit plugins pack zip <插件目录|已装插件名> [--out 输出路径]")
    candidate = Path(raw).expanduser()
    if candidate.is_dir():
        try:
            manifest = plugins.load_manifest(candidate / "plugin.yaml", root=candidate)
        except plugins.PluginError as exc:
            raise CliError(str(exc)) from exc
        source = candidate
    else:
        manifest = plugins_cli._find(context, raw)  # noqa: SLF001 —— 报错口径保持一致
        source = manifest.directory

    stem = f"{manifest.name}-{manifest.version or '0.0.0'}"
    raw_out = str(getattr(args, "out", "") or "").strip()
    if raw_out:
        out = Path(raw_out).expanduser()
        target = out / f"{stem}.zip" if out.is_dir() or not out.suffix else out
    else:
        target = Path.cwd() / f"{stem}.zip"
    members = pack_members(source)
    if not members:
        raise CliError(f"{source} 里没有可打包的文件")
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for relative in members:
            bundle.write(source / relative, arcname=f"{stem}/{relative}")
    context.out(f"已打包：{target}（{len(members)} 个文件，{_size(target.stat().st_size)}）")
    context.out(f"装回来：orbit plugins install {target}")
    context.emit(
        {
            "ok": True,
            "name": manifest.name,
            "version": manifest.version,
            "archive": str(target),
            "members": len(members),
            "bytes": target.stat().st_size,
        }
    )
    return 0


def run_plugins_update(context: Context, args) -> int:
    """``plugins update``：按记录的本地 source 真的重拉；远程如实报未实现。"""
    name = str(getattr(args, "name", "") or "")
    manifest = plugins_cli._find(context, name)  # noqa: SLF001
    if not plugins_cli._is_user_plugin(context, manifest):  # noqa: SLF001
        raise CliError(
            f"{manifest.name} 是随包插件，更新它等于改仓库代码；随包插件随 Orbit 一起升级"
        )
    scan = plugins.scan_plugin(
        manifest.directory, source=str(manifest.path), data_dir=context.data_dir
    )
    context.out(f"{manifest.name} 是本地安装（无远程源可 pull）。")
    context.out("已就地重新扫描当前目录：")
    context.out(plugins.format_scan_report(scan))

    record = load_sources(context).get(manifest.name)
    context.out()
    if record is None:
        context.out(
            "没有记录本地 source（这条插件不是经 `plugins install` 装的，或装于本版之前），"
            "无法本地重拉。"
        )
    else:
        raw_source = Path(str(record.get("source", ""))).expanduser()
        kind = str(record.get("kind", "dir"))
        if not raw_source.exists():
            context.out(f"记录的本地 source 已不存在：{raw_source}（无法本地重拉）")
        else:
            context.out(f"按记录的本地 source 重新拉：{raw_source}（{kind}）")
            try:
                if kind == "zip":
                    # 解到临时目录再装：`install_local` 会把目录复制走，装完临时目录就能删。
                    with tempfile.TemporaryDirectory(
                        prefix="orbit-update-", dir=str(context.data_dir)
                    ) as workspace:
                        plugin_dir = locate_plugin_dir(
                            _extract_into(raw_source, Path(workspace) / "unpacked"), raw_source
                        )
                        result = plugins.install_local(plugin_dir, context.settings, force=True)
                else:
                    result = plugins.install_local(raw_source, context.settings, force=True)
            except (plugins.PluginError, CliError) as exc:
                context.warn(f"本地重拉失败：{exc}")
            else:
                after = result.name
                try:
                    fresh = plugins.load_manifest(result.target / "plugin.yaml", root=result.target)
                    version = fresh.version or "无版本"
                except plugins.PluginError:
                    version = "无版本"
                context.out(f"已从本地 source 重装：{after}（{version}）→ {result.target}")
                context.out(f"扫描裁决：{result.verdict}（{result.findings} 项发现）")
    context.out()
    context.out(
        "未实现：远程 git pull 更新（本机无网络，也没有实现 commit SHA 钉版与 diff 审查）。"
        "要换版本请 `plugins remove` 后 `plugins install <新目录|新zip>`。"
    )
    context.emit(
        {
            "ok": True,
            "plugin": manifest.name,
            "scan": scan.as_dict(),
            "source": record,
            "remote": "未实现（需要网络）",
        }
    )
    return 0


def _extract_into(archive: Path, destination: Path) -> Path:
    """解 zip 到指定目录（``plugins update`` 的 zip source 用）。

    与 ``_install_from_zip`` 共用 ``safe_extract``：**解压入口只有一处**，
    zip slip 的防线才不会出现"这条路径忘了校验"的第二种写法。
    """
    safe_extract(archive, destination)
    return destination


# ── 接线 ────────────────────────────────────────────────────────────────────


def _ensure_option(node: Command, option: Option) -> None:
    """给已有节点补一个选项（已有同名 flag 就不动，argparse 会报冲突）。"""
    taken = {flag for item in node.options for flag in item.flags}
    if taken & set(option.flags):
        return
    node.options.append(option)


def _bind(
    path: str,
    handler: Handler,
    *,
    help_text: str = "",
    options: list[Option] | None = None,
    needs_store: bool | None = None,
) -> Command | None:
    """声明一条路径的实现，并就地更新注册表里的那个节点（两种导入顺序都正确）。

    - 先 ``register_handler``：将来声明表若重建成同名路径，``Spec.build()`` 直接取到
      这里的 handler，不用改两处；
    - 再改节点：本模块在 ``hermes_parity`` **之后**导入（禁改 ``__init__.py``，
      接线由别人统一做），那时表已经组装完，只声明路径不会生效。
    """
    try:
        register_handler(path, handler)
    except ValueError:
        pass  # 已被本模块接过（重复导入）
    parent_name, _, child_name = path.rpartition(" ")
    parent = REGISTRY.get(parent_name)
    if parent is None:  # pragma: no cover - 父命令没登记说明导入顺序错了
        return None
    node = parent.child(child_name)
    if node is None:  # pragma: no cover
        return None
    node.handler = handler
    node.state = "done"
    if help_text:
        # 声明表的 help 描述的是 Hermes 的语义；本实现换成别的语义后必须改掉，
        # 否则 `--help` 会当着用户的面说谎。
        node.help = help_text
    if needs_store is not None:
        node.needs_store = needs_store
    for option in options or []:
        _ensure_option(node, option)
    return node


def _apply_option(help_text: str) -> Option:
    return Option(("--apply",), help_text, action="store_true")


#: ``sessions`` 的 10 条：``(路径, handler, 覆盖的 help, 新增选项, 是否需要库)``。
#:
#: 用一张表而不是十段重复的接线代码：**路径只写一次**，于是"漏接一条""接错 handler"
#: 这类错在结构上就不可能发生，测试也能直接遍历它逐条核对。
#: ``help`` 非空表示本实现的语义与声明表不同，必须把 help 一起改掉（否则 ``--help``
#: 会当着用户的面说谎）。
_SESSION_BINDINGS: tuple[tuple[str, Handler, str, tuple[Option, ...], bool], ...] = (
    # 三个库各 PRAGMA optimize + VACUUM：不碰库里的行，所以不需要 store。
    ("sessions optimize", run_optimize, "", (), False),
    (
        "sessions clean-markers",
        run_clean_markers,
        "清理正文开头的上下文压缩标记（默认 dry-run，--apply 才改）",
        (_apply_option("真的改（默认只预览）"),),
        True,
    ),
    (
        "sessions optimize-storage",
        run_optimize_storage,
        "VACUUM INTO 重建 + 清理过期 task_events（默认 dry-run，--apply 才删）",
        (
            _apply_option("真的删行并重建（默认只预览）"),
            Option(("--keep-days",), "保留多少天的 task_events（默认 30）", metavar="DAYS"),
            Option(
                ("--no-backup",),
                "改动前不备份（默认会先 VACUUM INTO 一份备份）",
                action="store_true",
            ),
        ),
        False,
    ),
    (
        "sessions repair",
        run_repair,
        "integrity_check + foreign_key_check（只报不改；--apply 只做 REINDEX）",
        (_apply_option("执行 REINDEX（唯一的自动动作）"),),
        False,
    ),
    (
        "sessions repair-routing",
        run_repair_routing,
        "找出 tasks.conversation_id 悬空的行（--apply 逐行重挂到最近会话）",
        (),
        True,
    ),
    (
        "sessions recover",
        run_recover,
        "把卡住的僵尸任务（RUNNING 且超时未更新）标成 FAILED（默认 dry-run）",
        (
            _apply_option("真的改状态（默认只预览）"),
            Option(("--stale-minutes",), "多久没更新算僵尸（默认 30 分钟）", metavar="MINUTES"),
        ),
        True,
    ),
    ("sessions pin", run_pin, "", (), True),
    ("sessions unpin", run_unpin, "", (), True),
    ("sessions pinned", run_pinned, "", (), True),
    (
        "sessions retitle-skills",
        run_retitle_skills,
        "按 SKILL.md frontmatter 的 name 重写技能索引名（默认 dry-run）",
        (Option(("--root",), "技能根（默认 <data>/skills）", metavar="PATH"),),
        True,
    ),
)

#: ``plugins`` 的三条重接。``pack zip`` 是新节点，由 :func:`_bind_plugins` 追加。
_PLUGIN_BINDINGS: tuple[tuple[str, Handler], ...] = (
    ("plugins install", run_plugins_install),
    ("plugins search", run_plugins_search),
    ("plugins update", run_plugins_update),
)


def _bind_sessions() -> list[str]:
    """接上 ``sessions`` 的 10 条。返回接上的路径（测试用）。

    父命令还没登记（本模块被排在 ``hermes_parity`` **之前**导入）时**不抛错**：
    那时 ``register_handler`` 已经把路径声明下来了，表组装时 ``Spec.build()`` 会取到
    这些 handler 并标 ``done``，命令照样能用——只是 ``--apply`` 这类**新增选项**
    加不上去（没有节点可改）。这种情况下调用方只需在表组装完后再调一次
    :func:`wire`，选项就补齐了。抛错会让"导入顺序"变成整棵 CLI 起不来的单点故障，
    而那个故障的表象是"orbit 什么都跑不了"，排查成本极高。
    """
    parent = REGISTRY.get("sessions")
    bound: list[str] = []
    for path, handler, help_text, options, needs_store in _SESSION_BINDINGS:
        node = _bind(
            path, handler, help_text=help_text, options=list(options), needs_store=needs_store
        )
        if node is not None:
            bound.append(path)
    if parent is None:  # pragma: no cover - 只在导入顺序颠倒时出现
        return []

    for name in ("pin", "unpin"):
        node = parent.child(name)
        if node is None:  # pragma: no cover
            continue
        # 位置参数改成"可多个"：`pin a b` 与 `pin a,b` 都要能用（声明表只登记了一个）。
        node.options = [
            Option(
                ("session_ids",),
                "会话 ID 或唯一前缀（可多个：空格或逗号分隔）",
                metavar="SESSION_IDS",
                nargs="*",
            )
            if "session_ids" in item.flags
            else item
            for item in node.options
        ]
    pin_node = parent.child("pin")
    if pin_node is not None:
        _ensure_option(
            pin_node,
            Option(("--note",), "钉住时记一句备注（写进 session_pins.note）", metavar="TEXT"),
        )

    # `recover` 在声明表里带的是 Hermes"按来源重建会话"的 7 个选项（--source / --output /
    # --work-dir …），与"把僵尸任务标成失败"毫无关系。留着它们等于让 --help 承诺一堆
    # 本实现根本不看的语义——那是最难发现的一类说谎，所以整组换掉。
    # `repair --no-backup` 同理：REINDEX 是纯派生数据重建，不破坏任何东西，也就无需备份。
    recover_node = parent.child("recover")
    if recover_node is not None:
        recover_node.options = [
            item for item in recover_node.options if item.flags[0] in {"--apply", "--stale-minutes"}
        ]
    repair_node = parent.child("repair")
    if repair_node is not None:
        repair_node.options = [
            item for item in repair_node.options if "--no-backup" not in item.flags
        ]
    return bound


def _bind_plugins() -> list[str]:
    """接上 ``plugins install/search/update`` 与新增的 ``plugins pack zip``。"""
    parent = REGISTRY.get("plugins")
    bound: list[str] = []
    for path, handler in _PLUGIN_BINDINGS:
        if _bind(path, handler, needs_store=False) is not None:
            bound.append(path)
    if parent is None:  # pragma: no cover - plugins 由 plugins.py 自己登记，正常不会走到
        return bound

    search_node = parent.child("search")
    if search_node is not None:
        _ensure_option(
            search_node,
            Option(("--hub",), "额外搜索一个本地 hub 目录（未安装的插件源码树）", metavar="DIR"),
        )

    pack_node = parent.child("pack")
    if pack_node is not None:
        existing = pack_node.child("zip")
        if existing is None:
            pack_node.subcommands.append(
                Command(
                    name="zip",
                    help="把一个插件目录打成 <name>-<version>.zip（可用 plugins install <zip> 装回来）",
                    handler=run_plugins_pack_zip,
                    group="ext",
                    state="done",
                    needs_settings=False,
                    needs_store=False,
                    options=[
                        Option(("source",), "插件目录或已装插件名", metavar="SOURCE"),
                        Option(("--out", "-o"), "输出 zip 路径或目录（默认当前目录）", metavar="PATH"),
                    ],
                )
            )
        else:  # pragma: no cover - 重复导入
            existing.handler = run_plugins_pack_zip
            existing.state = "done"
        bound.append("plugins pack zip")
    return bound


def wire() -> list[str]:
    """接上全部路径（幂等）。返回本次确认接上的路径列表。"""
    return _bind_sessions() + _bind_plugins()


WIRED: list[str] = wire()


__all__ = [
    "SOURCES_FILE",
    "ZipSlipError",
    "WIRED",
    "load_sources",
    "locate_plugin_dir",
    "pack_members",
    "remember_source",
    "run_clean_markers",
    "run_optimize",
    "run_optimize_storage",
    "run_pin",
    "run_pinned",
    "run_plugins_install",
    "run_plugins_pack_zip",
    "run_plugins_search",
    "run_plugins_update",
    "run_recover",
    "run_repair",
    "run_repair_routing",
    "run_retitle_skills",
    "run_unpin",
    "safe_extract",
    "wire",
]
