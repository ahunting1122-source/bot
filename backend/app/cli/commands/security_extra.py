"""`security {path,url,osv,threat}` 与 `approvals {queue,decide,channel,expire}`（MATRIX 14.1 / 14.5 / 14.8）。

## 接线方式：能填的填表，填不了的追加

与 `app/cli/commands/tools_extra2.py` 同一套做法，先用 ``REGISTRY.get(...)`` 看现状：

- ``security audit``：**已被 `security.py` 接管**（venv / 插件 / MCP 三项）。
  再登记一次会触发 `_table.register_handler` 的"重复登记"门禁，整个 CLI 会起不来，
  所以本模块**不在导入时抢它**。OSV 供应链那半独立成 ``security osv``；
  若哪天 `security audit` 被腾空，:func:`claim_audit_path` 能让本模块的
  "三项 + OSV"完整实现接手（显式调用，不自动抢）。
- ``security path / url / osv / threat``：**表里没有这些路径**。`register_handler`
  填不了，另立顶层命令又会挤掉表里未实现的子命令，所以走"**往已构建的 `Command`
  上追加子命令**"（追加不覆盖，`--help` 里原有的条目一条不少）。
- ``approvals queue / decide / channel / expire``：`approvals` 由
  `app/cli/commands/approvals.py` 自己 `REGISTRY.add` 成一个完整命令（不是声明表
  建的），同样只能追加。

追加需要一个已存在的父命令，因此两种导入顺序都要成立：本模块排在 `hermes_parity`
之后就直接追加；排在之前就把 `_table.register_specs` 包一层，等声明表组装完立刻追加。

## 为什么这些命令值得存在

MATRIX 14.8 的四样东西（`path_security` / `url_safety` / `website_policy` /
`threat_patterns`）原本只有库、没有命令面——"实现了但没法验证"与"没实现"在排查
时几乎无法区分。这四条命令就是它们的验证入口：能当场看到判据名（`kind`）与理由。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import app.cli.commands._table as _table
from app import approval_channel, osv, path_security
from app.approvals import resolve_mode
from app.cli.commands._table import register_handler
from app.cli.context import CliError, Context
from app.cli.shorthand import to_option
from app.cli.spec import REGISTRY, Command, Option

#: "能力缺失 / 需要联网"的退出码。与 `_table._pending` / `mcp.py::UNIMPLEMENTED_CODE` 一致。
UNIMPLEMENTED_CODE = 3

#: 被拒的退出码。与 `app/approvals.py::EXIT_CODES` 的 deny 对齐（脚本可以统一判）。
DENIED_CODE = 3

#: 需要人工处置的退出码（OSV 需要联网、队列里有待审批项）。照 Hermes 的 `ask`。
ASK_CODE = 2


def _many(args: Any, name: str) -> tuple[str, ...]:
    """读一个 `action="append"` 的选项。单值/None 都能吃（不因为写法不同就漏项）。"""
    raw = getattr(args, name, None)
    if raw is None:
        return ()
    if isinstance(raw, str):
        return (raw,) if raw.strip() else ()
    return tuple(str(item) for item in raw if str(item).strip())


# ── security path ───────────────────────────────────────────────────────────


def run_path(context: Context, args: Any) -> int:
    """判定一条路径能不能被安全使用（`path_security.PathPolicy`）。

    默认**不限制根目录**：这条命令回答的是"这个名字本身有没有问题"（保留名 / 数据流 /
    上跳 / 符号链接）。要连根目录包含一起判，用 `--root` 显式声明。
    """
    target = str(getattr(args, "path", "") or "").strip()
    if not target:
        raise CliError("用法：orbit security path <路径> [--root ROOT ...] [--deny PATTERN ...]")
    policy = path_security.PathPolicy(
        allowed_roots=tuple(Path(item) for item in _many(args, "root")),
        deny_patterns=_many(args, "deny"),
        follow_symlinks=bool(getattr(args, "follow_symlinks", False)),
        allow_dotdot=bool(getattr(args, "allow_dotdot", False)),
    )
    decision = policy.check(target)
    context.out(f"路径：{decision.path}")
    if decision.resolved:
        context.out(f"解析：{decision.resolved}")
    context.out(f"结论：{'通过' if decision.allowed else '拒绝'}（{decision.kind}）")
    context.out(f"理由：{decision.reason}")
    context.emit({"ok": decision.allowed, "command": "security path", "decision": decision.as_dict()})
    return 0 if decision.allowed else DENIED_CODE


# ── security url ────────────────────────────────────────────────────────────


def run_url(context: Context, args: Any) -> int:
    """判定一条 URL 能不能被安全访问（`path_security.UrlPolicy`）。

    `--resolve` 会做 DNS 解析再判一次 IP。**默认不做**：本机离线，解析必然失败，
    而"解析失败就拒"会让所有域名一律判死——那道闸就永远在说"不"。
    """
    target = str(getattr(args, "url", "") or "").strip()
    if not target:
        raise CliError("用法：orbit security url <URL> [--resolve] [--deny-host PATTERN ...]")
    resolver = None
    if getattr(args, "resolve", False):
        from app.web import resolve_host

        resolver = resolve_host
    policy = path_security.UrlPolicy(
        deny_hosts=_many(args, "deny_host"),
        allow_private=bool(getattr(args, "allow_private", False)),
        resolve=resolver,
    )
    decision = policy.check(target)
    context.out(f"网址：{decision.url}")
    context.out(f"主机：{decision.host or '（解析不出）'}　协议：{decision.scheme or '（空）'}")
    context.out(f"结论：{'通过' if decision.allowed else '拒绝'}（{decision.kind}）")
    context.out(f"理由：{decision.reason}")
    if not decision.resolved:
        context.out("说明：本轮没有做 DNS 解析（离线），域名指向内网这类变体查不出来。")
    context.emit({"ok": decision.allowed, "command": "security url", "decision": decision.as_dict()})
    return 0 if decision.allowed else DENIED_CODE


# ── security threat ─────────────────────────────────────────────────────────


def run_threat(context: Context, args: Any) -> int:
    """`threat_*`：按 severity 汇总 tirith 的规则，或对一条命令做组合扫描。

    **规则来自 `app/tirith.py`，这里不复制正则**——抄一份出来两边迟早漂移，
    而漂移的安全边界比没有更危险。
    """
    command = str(getattr(args, "command", "") or "").strip()
    if command:
        payload = path_security.threat_scan(command, settings=context.settings)
        context.out(f"命令：{command}")
        context.out(f"严重度：{payload['severity']}　拦截：{'是' if payload['blocked'] else '否'}")
        if payload["findings"]:
            context.table(
                ("严重度", "规则", "说明"),
                [(item["severity"], item["kind"], item["detail"]) for item in payload["findings"]],
            )
        else:
            context.out("没有命中任何组合规则。")
        context.emit({"ok": not payload["blocked"], "command": "security threat", **payload})
        return DENIED_CODE if payload["blocked"] else 0

    summary = path_security.threat_summary()
    context.out(f"威胁规则来源：{summary['source']}（共 {summary['total']} 条）")
    context.out(f"默认拦截阈值：{summary['block_at']}")
    context.table(
        ("严重度", "条数"),
        [(name, str(count)) for name, count in summary["by_severity"].items()],
    )
    context.out()
    context.table(
        ("严重度", "规则", "说明"),
        [(item["severity"], item["kind"], item["detail"]) for item in summary["rules"]],
    )
    context.emit({"ok": True, "command": "security threat", **summary})
    return 0


# ── security osv ────────────────────────────────────────────────────────────


def run_osv(context: Context, args: Any) -> int:
    """OSV 供应链审计的**离线那半**（`app/osv.py`）。

    三种结局，退出码不同（脚本要能区分"没命中"与"根本没比"）：

    - 命中 → 1（与 `security audit` 一致：任何命中都非零，不按级别放宽）
    - 需要联网 / 没有本地库 → 2（**不是 0**：一次比对都没发生）
    - 真的比过且没命中 → 0
    """
    raw_db = getattr(args, "offline_db", None)
    database = None
    if raw_db:
        try:
            database = osv.apply_offline_db(Path(str(raw_db)))
        except (OSError, ValueError) as exc:
            raise CliError(f"读不了本地 OSV 导出：{exc}", code=UNIMPLEMENTED_CODE) from exc
    strict = bool(getattr(args, "strict", False))
    try:
        report = osv.audit_local(
            root=getattr(args, "root", None) or None,
            site_packages=getattr(args, "site_packages", None) or None,
            database=database,
            strict=strict,
        )
    except osv.NetworkUnavailable as exc:
        # `--strict`：把"需要联网"升级成硬失败。CI 里应当用它——
        # 退出码比一段文字更难被忽略。
        raise CliError(f"{exc}", code=UNIMPLEMENTED_CODE) from exc

    context.out("OSV 供应链审计（离线）")
    context.out()
    context.out(f"■ 依赖清单：{report.scanned} 条　{report.headline()}")
    if report.dependencies:
        context.table(
            ("包", "版本", "声明", "来源"),
            [
                (
                    dependency.name,
                    dependency.version or "（未知）",
                    dependency.spec or "（无约束）",
                    dependency.source,
                )
                for dependency in report.dependencies
            ],
        )
    if report.findings:
        context.table(
            ("严重度", "对象", "说明"),
            [(item.severity, item.target, item.detail) for item in report.findings],
        )
    if report.note:
        # 这段必须原样打出来：它是"这不是安全结论"的唯一依据。
        context.out()
        context.out(f"[注意] {report.note}")
    if report.network_required:
        context.out()
        context.out("要做完整的漏洞比对，需要联网刷新通告缓存，或挂一份本地 OSV 导出：")
        context.out("  orbit security osv --offline-db <osv-export.json>")
    context.out()
    context.out(osv.describe())

    payload = {"ok": report.passed(), "command": "security osv", **report.as_dict()}
    context.emit(payload)
    if report.findings:
        return 1
    if report.network_required:
        return ASK_CODE
    return 0


# ── security audit（完整版：仅由显式接线调用）────────────────────────────────


def run_security_audit(context: Context, args: Any) -> int:
    """`security audit` 的完整版：既有三项审计 **+** OSV 离线那半。

    正常情况这条函数**不会被调到**：`security.py` 已经接管了 `security audit`，
    再登记会触发重复登记门禁、整个 CLI 起不来。它存在的意义是——若那条路径被
    腾空（重构/合并），:func:`claim_audit_path` 能让这份"三项 + OSV"实现直接顶上，
    而不是留下一个空洞。两个退出码取**更严重的那个**：审计命中（1）不该被
    "需要联网"（2）盖掉，反之亦然。
    """
    from app.cli.commands.security import run_audit

    code = run_audit(context, args)
    context.out()
    context.out("─" * 40)
    return max(code, run_osv(context, args))


def claim_audit_path() -> bool:
    """把 ``security audit`` 接到 :func:`run_security_audit` 上。返回是否接手。

    **不自动调用**。理由：`security.py` 现在占着这条路径，抢它会抛
    "命令实现重复登记"并让整个 CLI 起不来。所以这件事必须是显式的——
    谁腾空了那条路径，谁调它。
    """
    return _claim("security audit", run_security_audit)


# ── approvals queue / decide / channel / expire ─────────────────────────────


def _queue(context: Context) -> approval_channel.ApprovalQueue:
    return approval_channel.ApprovalQueue.load(context.data_dir)


def run_queue(context: Context, args: Any) -> int:
    """列出待审批队列（`<data>/approvals/queue.json`）。"""
    store = _queue(context)
    max_age = getattr(args, "expire", None)
    expired: list[str] = []
    if max_age is not None:
        try:
            expired = store.expire(float(max_age))
        except (TypeError, ValueError) as exc:
            raise CliError(f"--expire 只接受秒数：{max_age!r}") from exc
    show_all = bool(getattr(args, "all", False))
    rows = store.entries if show_all else store.pending()
    context.out(f"队列文件：{store.path}")
    context.out()
    if rows:
        context.table(
            ("ID", "状态", "模式", "命令", "创建时间"),
            [
                (
                    str(entry.get("id", "")),
                    str(entry.get("status", "")),
                    str(entry.get("mode", "")),
                    str(entry.get("command", ""))[:60],
                    str(entry.get("created_at", "")),
                )
                for entry in rows
            ],
        )
    else:
        context.out("没有待审批项。" if not show_all else "队列为空。")
    if expired:
        context.out()
        context.out(f"已标记 {len(expired)} 条为超时（不是拒绝）：{'、'.join(expired)}")
    if not show_all and store.entries and not rows:
        context.out()
        context.out("历史条目见 `orbit approvals queue --all`。")
    context.emit(
        {
            "ok": True,
            "command": "approvals queue",
            "path": str(store.path),
            "pending": len(store.pending()),
            "entries": rows,
            "expired": expired,
        }
    )
    return 0


def run_decide(context: Context, args: Any) -> int:
    """裁决一条待审批。`--approve` / `--deny` 必须二选一。

    队列**不是权限来源**：这里只改记录状态，不会让任何命令被执行。改完要真正
    执行，仍然得回到那条命令本身走一遍全部闸。
    """
    identifier = str(getattr(args, "id", "") or "").strip()
    if not identifier:
        raise CliError("用法：orbit approvals decide <ID> --approve|--deny [--note 备注]")
    approve = bool(getattr(args, "approve", False))
    deny = bool(getattr(args, "deny", False))
    if approve == deny:
        raise CliError("必须且只能给一个：--approve 或 --deny")
    store = _queue(context)
    note = str(getattr(args, "note", "") or "")
    if not store.decide(identifier, approve, note=note):
        entry = store.get(identifier)
        if entry is None:
            raise CliError(f"队列里没有这条：{identifier}")
        raise CliError(f"这条已经有裁决了（{entry.get('status')}），不覆盖：{identifier}")
    status = approval_channel.APPROVED if approve else approval_channel.DENIED
    context.out(f"已记录：{identifier} → {status}" + (f"（备注：{note}）" if note else ""))
    context.emit(
        {"ok": True, "command": "approvals decide", "id": identifier, "status": status, "note": note}
    )
    return 0


def run_expire(context: Context, args: Any) -> int:
    """把等待过久的待审批标成超时。**超时与拒绝分开记**（见 `app/approval_channel.py`）。"""
    raw = getattr(args, "max_age", None)
    try:
        max_age = float(raw or 0)
    except (TypeError, ValueError) as exc:
        raise CliError(f"--max-age 只接受秒数：{raw!r}") from exc
    if max_age <= 0:
        raise CliError("用法：orbit approvals expire --max-age SECONDS（必须大于 0）")
    store = _queue(context)
    expired = store.expire(max_age)
    if expired:
        context.out(f"已标记 {len(expired)} 条为超时（不是拒绝）：{'、'.join(expired)}")
    else:
        context.out(f"没有等待超过 {max_age:g} 秒的待审批项。")
    context.emit({"ok": True, "command": "approvals expire", "expired": expired, "max_age": max_age})
    return 0


def run_channel(context: Context, args: Any) -> int:
    """说明当前审批模式会选哪个通道、等多久。**不提问**。"""
    mode = str(getattr(args, "mode", "") or "").strip() or resolve_mode(context.settings)
    info = approval_channel.describe_channel(mode, context.settings)
    context.out(f"审批模式：{info['mode']}")
    context.out(f"需要交互通道：{'是' if info['needs_channel'] else '否'}")
    context.out(f"通道：{info['channel'] or '（不需要）'}")
    context.out(f"配置值：{info['configured'] or '（未配置，按默认选）'}")
    if not info["configured_known"]:
        context.out("  [注意] 配置里的通道名不认识，已退回 auto-deny（fail closed）")
    context.out(f"等待上限：{info['timeout']:g} 秒")
    if info["directory"]:
        context.out(f"请求/响应目录：{info['directory']}")
    context.out()
    context.out(info["note"])
    context.out()
    context.out(
        "通道：console（终端问答）/ file（写请求 + 轮询响应，给 gateway/cron）/ "
        "auto-deny（无人值守默认，**拒绝**而不是放行）"
    )
    context.out("超时 = 拒绝，但原因会写清是超时不是拒绝——两者在记录里不混。")
    context.emit({"ok": True, "command": "approvals channel", **info})
    return 0


# ── 命令面 ──────────────────────────────────────────────────────────────────

#: 要追加到已存在父命令上的子命令。每条都带 `hermes=`（`parity_rows` 要求有对标依据）。
EXTRA_SUBCOMMANDS: dict[str, list[Command]] = {
    "security": [
        Command(
            name="path",
            help="判定一条路径能否安全使用（保留名 / 数据流 / 上跳 / 符号链接 / 根目录）",
            handler=run_path,
            group="ops",
            hermes="security path",
            parity="P1",
            state="done",
            needs_settings=False,
            options=[
                Option(("--root",), "允许的根目录（可重复；不给则不限制根）", action="append", metavar="PATH"),
                Option(("--deny",), "拒绝模式，如 *.pem（可重复）", action="append", metavar="PATTERN"),
                Option(("--follow-symlinks",), "允许路径经过符号链接（默认不允许）", action="store_true"),
                Option(("--allow-dotdot",), "允许路径含 ..（默认不允许）", action="store_true"),
                to_option("path", "要检查的路径"),
            ],
        ),
        Command(
            name="url",
            help="判定一条 URL 能否安全访问（协议 / 内网 / 云元数据 / 凭据内嵌）",
            handler=run_url,
            group="ops",
            hermes="security url",
            parity="P1",
            state="done",
            needs_settings=False,
            options=[
                Option(("--deny-host",), "主机拒绝规则，如 *.corp（可重复）", action="append", metavar="PATTERN"),
                Option(("--allow-private",), "允许本机与私有网段（云元数据仍然拒）", action="store_true"),
                Option(("--resolve",), "做 DNS 解析后再判一次 IP（离线时必然失败）", action="store_true"),
                to_option("url", "要检查的 URL"),
            ],
        ),
        Command(
            name="osv",
            help="OSV 供应链审计的离线那半：依赖清单 + 本地通告库比对",
            handler=run_osv,
            group="ops",
            hermes="security audit",
            parity="P1",
            state="done",
            needs_settings=False,
            options=[
                to_option("--root ROOT", "读 pyproject.toml / requirements.txt 的目录"),
                to_option("--site-packages PATH", "用哪个 site-packages 确定实际安装版本"),
                to_option("--offline-db PATH", "本地 OSV 导出（JSON/NDJSON），挂了就能真比对"),
                Option(("--strict",), "没有可比对的通告库时硬失败（CI 用）", action="store_true"),
            ],
        ),
        Command(
            name="threat",
            help="按严重度汇总 tirith 的 16 条组合规则；给 --command 则直接扫描",
            handler=run_threat,
            group="ops",
            hermes="security threat",
            parity="P2",
            state="done",
            needs_settings=False,
            options=[to_option("--command CMD", "要扫描的命令（不给则只汇总规则表）")],
        ),
    ],
    "approvals": [
        Command(
            name="queue",
            help="列出待审批队列（超时与拒绝分开记）",
            handler=run_queue,
            group="ops",
            hermes="approvals queue",
            parity="P1",
            state="done",
            options=[
                Option(("--all",), "连已裁决的历史条目一起列出", action="store_true"),
                to_option("--expire SECONDS", "先把等待超过这么多秒的标成超时"),
            ],
        ),
        Command(
            name="decide",
            help="裁决一条待审批（只改记录，不执行命令）",
            handler=run_decide,
            group="ops",
            hermes="approvals decide",
            parity="P1",
            state="done",
            options=[
                Option(("--approve",), "允许", action="store_true"),
                Option(("--deny",), "拒绝", action="store_true"),
                to_option("--note NOTE", "备注（会写进队列记录）"),
                to_option("id", "请求 ID（见 approvals queue）"),
            ],
        ),
        Command(
            name="expire",
            help="把等待过久的待审批标成超时（不是拒绝）",
            handler=run_expire,
            group="ops",
            hermes="approvals expire",
            parity="P2",
            state="done",
            options=[to_option("--max-age SECONDS", "等待超过这么多秒就标超时")],
        ),
        Command(
            name="channel",
            help="说明当前模式会选哪个审批通道、等多久（不提问）",
            handler=run_channel,
            group="ops",
            hermes="approvals channel",
            parity="P1",
            state="done",
            needs_settings=False,
            options=[to_option("--mode MODE", "按哪个模式看（默认取当前配置）")],
        ),
    ],
}


def extend_registry() -> list[str]:
    """把新增子命令追加到已存在的父命令上（**幂等**，返回新增的 `父 子` 路径）。

    追加而不是替换：父命令上原有的子命令一条不少。父命令不存在时返回空列表
    （声明表还没组装完，等 :func:`install` 的回调再来一次）。
    """
    added: list[str] = []
    for parent_name, children in EXTRA_SUBCOMMANDS.items():
        parent = REGISTRY.get(parent_name)
        if parent is None:
            continue
        existing = {sub.name for sub in parent.subcommands}
        for child in children:
            if child.name in existing:
                continue
            parent.subcommands.append(child)
            added.append(f"{parent_name} {child.name}")
    return added


def _claim(path: str, handler: Any) -> bool:
    """表里这条路径**还没人接**才登记。返回是否登记成功。

    为什么不直接 `register_handler`：重复登记会抛 `ValueError`，而那是**导入期**
    异常——整个 CLI 起不来，`--help` 都打不开。所以"接手"必须是按事实发生的：
    没人接就接，有人接就让开。
    """
    key = " ".join(path.split())
    if key in _table.HANDLERS:
        return False
    try:
        register_handler(key, handler)
    except ValueError:
        return False
    head, _, leaf = key.partition(" ")
    parent = REGISTRY.get(head)
    if parent is not None:
        target = parent.child(leaf) if leaf else parent
        if target is not None and not target.subcommands:
            target.handler = handler
            target.state = "done"
    return True


def install() -> list[str]:
    """接上所有新增命令面。返回本次新增的路径（父命令还没组装就是空列表）。幂等。"""
    return extend_registry()


#: 声明表组装钩子的幂等标记。
_WRAPPED = False


def wrap_register_specs() -> bool:
    """把 `_table.register_specs` 包一层：声明表组装完立刻 :func:`install`。

    本模块排在 `hermes_parity` **之前**导入时，`security` / `approvals` 这两个父命令
    还不存在，追加无从谈起。唯一的接缝就是等它组装完（与
    `tools_extra2.wrap_register_specs` 同一套做法，**幂等**：包过就返回 False，
    避免 wrapper 套成洋葱）。
    """
    global _WRAPPED
    if _WRAPPED:
        return False
    original = _table.register_specs
    if getattr(original, "_orbit_security_extra_wrapped", False):
        _WRAPPED = True
        return False

    def wrapped(specs: Any) -> list[str]:
        skipped = original(specs)
        install()
        return skipped

    wrapped._orbit_security_extra_wrapped = True  # type: ignore[attr-defined]
    _table.register_specs = wrapped
    _WRAPPED = True
    return True


# 导入即接线：父命令已在就立即追加，否则等声明表组装完再追加。
if REGISTRY.get("security") is None or REGISTRY.get("approvals") is None:
    wrap_register_specs()
install()
