"""网关 / 平台 / 出站 / 订阅 / 配对 / 对等节点：把声明表里的命令填成真实现。

接线方式照 ``_table.py`` 的约定：**表不动**，本模块只声明"这些路径我接了"
（:func:`register_handler`）。为什么不另立 ``gateway`` / ``webhook`` / ``pairing`` /
``peer`` 顶层命令——表里它们分别有 11 / 4 / 4 / 7 个子命令，另立一个同名 Command
会把剩下没实现的整片挤掉，``--help`` 里就再也看不见它们了（命令面缩水 = 功能面缩水）。

``gateway doctor`` 是**唯一**表里没有的子命令：Hermes 的诊断散在
``gateway status --deep`` / ``doctor`` / ``monitoring status`` 三处，而 Orbit 需要一个
"一条命令看网关是否健康"的入口。它由本模块在导入时追加到已构建的 ``gateway`` 命令上
（:func:`_attach_extra_subcommands`），所以本模块应当在 ``hermes_parity`` **之后**
导入——它不登记顶层命令，不需要抢在表前面。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from app import gateway as gw
from app.cli.commands import _table
from app.cli.commands._table import Spec, register_handler
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command
from app.platforms import (
    ALL_PLATFORMS,
    IMPLEMENTED_PLATFORMS,
    PLATFORMS,
    NotSupported,
    parse_target,
)

#: 平台名/订阅名/对等节点名允许的形状（小写 slug）。用正则而不是 `str.isalnum`：
#: 名字会进 HTTP 路由（``/webhooks/<name>``）与文件名，允许大写/空格等于埋坑。
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


# ── 公共辅助 ────────────────────────────────────────────────────────────────


def _service(context: Context) -> gw.GatewayService:
    """按当前数据目录建服务实例（适配器用默认集合：全部已实现平台 + console/file）。"""
    return gw.GatewayService(context.data_dir)


def _subcommand_name(args) -> str:
    """当前子命令名。用 ``args._command`` 而不是 argparse 的 ``_xxx_command`` dest。"""
    command = getattr(args, "_command", None)
    return str(getattr(command, "name", "") or "")


def _flag(args, name: str) -> bool:
    return bool(getattr(args, name, False))


def _text(args, name: str, default: str = "") -> str:
    value = getattr(args, name, None)
    return default if value is None else str(value)


def _slug(raw: str, label: str) -> str:
    text = str(raw or "").strip().lower()
    if not _SLUG_RE.match(text):
        raise CliError(f"{label} 不合法：{raw!r}（只允许小写字母/数字与 _ . -，最长 64）")
    return text


def _split_csv(raw: Any) -> list[str]:
    """逗号分隔 → 列表。空项丢弃（``--events a,,b`` 不该产生空事件）。"""
    if raw is None:
        return []
    if isinstance(raw, (list, tuple)):
        chunks = [str(item) for item in raw]
    else:
        chunks = str(raw).split(",")
    return [chunk.strip() for chunk in chunks if chunk.strip()]


# ── gateway status / setup / start / stop / restart / doctor ────────────────


def run_status(context: Context, args) -> int:
    """``gateway status``：锁、四个状态文件、频道目录、平台覆盖度。"""
    if _flag(args, "system"):
        context.warn("--system 在 Orbit 里没有对应物：本轮不安装系统级服务，忽略该选项。")
    service = _service(context)
    data = service.status()
    dispatcher = data["dispatcher"]
    if dispatcher["running"]:
        holder = dispatcher["holder"]
        context.out(
            f"dispatcher：运行中（pid={holder.get('pid')}，启动于 {holder.get('acquired_at')}）"
        )
    elif dispatcher["holder"]:
        holder = dispatcher["holder"]
        context.out(f"dispatcher：未运行（锁文件残留 pid={holder.get('pid')}，下次 start 会回收）")
    else:
        context.out("dispatcher：未运行（没有锁文件）")
    context.out(f"数据目录：{data['data_dir']}")
    context.out(
        f"启动记录：共 {data['starts']['total_starts']} 次"
        f"（最近 {data['starts']['window_seconds'] / 60:.0f} 分钟 "
        f"{data['starts']['starts_in_window']} 次）"
    )
    if data["starts"]["crash_loop"]:
        context.warn(data["starts"]["detail"])
    context.out()
    context.out("状态文件：")
    context.table(
        ("文件", "存在"),
        [(name, "是" if present else "否") for name, present in data["state_files"].items()],
    )
    context.out()
    platforms = data["platforms"]
    context.out(
        f"平台：{platforms['total']} 个已登记，"
        f"{len(platforms['implemented'])} 个已实现（{'、'.join(platforms['implemented'])}），"
        f"{platforms['pending']} 个仅声明"
    )
    channels = data["channels"]
    context.out(
        f"频道目录：{data['channels_total']} 个频道"
        + (
            f"（{'、'.join(f'{name}:{count}' for name, count in channels.items())}）"
            if channels
            else "（尚未发现，先跑一次 send）"
        )
    )
    context.out(f"订阅 / 对等节点：{data['webhooks']} / {data['peers']}")
    if _flag(args, "deep"):
        context.out()
        report = service.health()
        context.out(f"健康度：{'正常' if report.healthy else '有问题'}")
        for issue in report.issues:
            context.out(f"  - {issue}")
        rows = []
        for name, adapter in sorted(service.adapters.items()):
            health = adapter.health()
            if health.extra.get("configured") is False:
                state = "未配置"
            else:
                state = "正常" if health.ok else "异常"
            rows.append((name, state, health.detail))
        context.out()
        context.table(("适配器", "状态", "说明"), rows)
    context.emit(data)
    return 0


def run_setup(context: Context, args) -> int:
    """``gateway setup``：把 22 个平台与所需凭证摊开，并落一份 ``platforms.json``。

    本轮**不做交互向导**（Hermes 是 TTY 问答）：向导在没有 TTY 的环境里行为不确定，
    而"列出平台 + 缺什么"本身就够用户决定下一步。已实现的三个平台会被写进
    ``platforms.json`` 的 ``enabled`` 里，作为网关启动时启用哪些适配器的声明。
    """
    context.out("消息平台清单（22 个，照 Hermes plugins/platforms）：")
    context.out()
    rows = []
    for platform in ALL_PLATFORMS:
        spec = PLATFORMS[platform]
        rows.append(
            (
                spec.platform.value,
                spec.label,
                "已实现" if spec.implemented else "未实现",
                "、".join(spec.credentials) or "（无）",
                spec.description,
            )
        )
    context.table(("平台", "名称", "状态", "需要的凭证", "接入方式"), rows)
    context.out()
    context.out("已实现的三个：" + "、".join(platform.value for platform in IMPLEMENTED_PLATFORMS))
    for platform in IMPLEMENTED_PLATFORMS:
        context.out(f"  - {platform.value}：{PLATFORMS[platform].scope}")
    payload = {
        "updated_at": gw.now_iso(),
        "enabled": [platform.value for platform in IMPLEMENTED_PLATFORMS],
        "platforms": {
            platform.value: {
                "implemented": PLATFORMS[platform].implemented,
                "credentials": list(PLATFORMS[platform].credentials),
                "reason": PLATFORMS[platform].reason,
            }
            for platform in ALL_PLATFORMS
        },
    }
    path = context.data_dir / gw.GATEWAY_DIRNAME / "platforms.json"
    gw.write_json(path, payload)
    context.out()
    context.out(f"已写入 {path}（enabled = 已实现的平台）")
    context.out("要启用未实现的平台，需要先补适配器（见 app/platforms.py 的 PLATFORMS）。")
    context.emit({"ok": True, "path": str(path), **payload})
    return 0


def run_start(context: Context, args) -> int:
    """``gateway start``：拿锁 + 写四个状态文件。**不起线程、不开监听**。"""
    if _flag(args, "system"):
        context.warn("--system 在 Orbit 里没有对应物：不安装系统级服务，忽略该选项。")
    service = _service(context)
    outcome = service.start(force=_flag(args, "force"))
    if not outcome.ok:
        raise CliError(outcome.reason or "启动失败：已有 dispatcher 在运行。")
    if _flag(args, "all"):
        context.note("--all：本轮只有一个数据目录，无需跨 profile 清理。")
    context.out(f"dispatcher 已启动：pid={outcome.pid}（host_start_time={outcome.host_start_time}）")
    if outcome.reclaimed:
        context.out(f"注意：{outcome.reason}")
    if outcome.reaped_orphans:
        context.out(f"回收孤儿进程 {len(outcome.reaped_orphans)} 个")
    context.out(f"频道目录：{outcome.channels} 个频道")
    context.out("说明：本轮只写状态文件与锁，不建立平台长连接（适配器见 gateway status）。")
    context.emit(outcome.as_dict())
    return 0


def run_stop(context: Context, args) -> int:
    if _flag(args, "system"):
        context.warn("--system 在 Orbit 里没有对应物：忽略该选项。")
    service = _service(context)
    result = service.stop()
    context.out(
        f"dispatcher 已停止：pid={result['pid']}"
        + ("" if result["released"] else "（锁不属于本进程，未释放）")
    )
    context.emit(result)
    return 0


def run_restart(context: Context, args) -> int:
    """``gateway restart``：先停再起。起不来就如实报错，别假装重启成功。"""
    service = _service(context)
    service.stop()
    outcome = service.start(force=_flag(args, "force"))
    if not outcome.ok:
        raise CliError(outcome.reason or "重启失败：启动阶段被互斥挡住。")
    context.out(f"已重启：pid={outcome.pid}")
    if outcome.reclaimed:
        context.out(f"注意：{outcome.reason}")
    context.emit(outcome.as_dict())
    return 0


def run_doctor(context: Context, args) -> int:
    """``gateway doctor``：一条命令回答"网关能不能用、哪里不行"。"""
    service = _service(context)
    report = service.health()
    checks: list[dict[str, Any]] = []

    gateway_dir = context.data_dir / gw.GATEWAY_DIRNAME
    try:
        gateway_dir.mkdir(parents=True, exist_ok=True)
        writable = True
        detail = str(gateway_dir)
    except OSError as exc:
        writable = False
        detail = f"不可写：{exc}"
    checks.append({"name": "gateway 目录", "ok": writable, "detail": detail})

    holder = report.dispatcher.get("holder") or {}
    running = bool(report.dispatcher.get("running"))
    orphan = bool(holder) and not running
    checks.append(
        {
            "name": "dispatcher 互斥",
            "ok": not orphan,
            "detail": (
                "运行中" if running else ("孤儿锁（下次 start 自动回收）" if orphan else "未运行")
            ),
        }
    )

    crash = report.crash_loop
    checks.append(
        {
            "name": "崩溃循环",
            "ok": not (crash and crash.crash_loop),
            "detail": crash.detail if crash else "无启动记录",
        }
    )

    missing = [name for name, present in report.state_files.items() if not present]
    checks.append(
        {
            "name": "四个状态文件",
            "ok": not missing,
            "detail": "齐全" if not missing else f"缺：{'、'.join(missing)}（先跑一次 gateway start）",
        }
    )

    unhealthy = []
    unconfigured = []
    for name, adapter in sorted(service.adapters.items()):
        health = adapter.health()
        if health.extra.get("configured") is False:
            unconfigured.append(name)
            continue
        if not health.ok:
            unhealthy.append(f"{name}：{health.detail}")
    checks.append(
        {
            "name": "适配器自检",
            "ok": not unhealthy,
            "detail": (
                "全部正常"
                if not unhealthy
                else "；".join(unhealthy)
            )
            + (f"（未配置：{'、'.join(unconfigured)}）" if unconfigured else ""),
        }
    )

    implemented = len(IMPLEMENTED_PLATFORMS)
    checks.append(
        {
            "name": "平台覆盖度",
            "ok": True,
            "detail": (
                f"{implemented}/{len(PLATFORMS)} 个平台已实现"
                f"（未实现 {len(PLATFORMS) - implemented} 个，发消息会明确报错）"
            ),
        }
    )

    context.table(
        ("检查项", "结果", "说明"),
        [(item["name"], "通过" if item["ok"] else "失败", item["detail"]) for item in checks],
    )
    failed = [item for item in checks if not item["ok"]]
    if failed:
        context.warn(f"{len(failed)} 项未通过：{'、'.join(item['name'] for item in failed)}")
    else:
        context.out()
        context.out("网关自检全部通过。")
    context.emit({"ok": not failed, "checks": checks, "health": report.as_dict()})
    return 0 if not failed else 1


# ── send ────────────────────────────────────────────────────────────────────


def _read_stdin() -> str:
    """从管道读正文。没有管道时如实报错，而不是发一条空消息出去。"""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            raise CliError("没有消息正文：用位置参数、--file PATH 或管道传入。")
        text = sys.stdin.read()
    except CliError:
        raise
    except Exception as exc:  # pytest 的 stdin 是 DontReadFromInput，读会直接抛
        raise CliError(f"无法从 stdin 读正文：{exc}") from exc
    if not text.strip():
        raise CliError("没有消息正文：stdin 是空的。")
    return text


def _read_body(context: Context, args) -> str:
    """正文来源优先级：位置参数 > ``--file`` > stdin（照 Hermes ``send``）。"""
    message = _text(args, "message").strip()
    if message:
        return message
    raw_file = _text(args, "file")
    if raw_file:
        if raw_file == "-":
            return _read_stdin()
        path = Path(raw_file).expanduser()
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CliError(f"读不到文件 {path}：{exc}") from exc
    return _read_stdin()


def _target_rows(service: gw.GatewayService) -> list[tuple[str, str, str]]:
    """``send --list`` 的行：平台 / 频道 / 说明。已实现平台在前。"""
    rows: list[tuple[str, str, str]] = []
    for platform in IMPLEMENTED_PLATFORMS:
        channels = service.directory.channels(platform.value)
        if channels:
            rows.extend((platform.value, channel, "频道目录（会话反推）") for channel in channels)
        else:
            rows.append((platform.value, "", "home channel（目录里还没有频道）"))
    for dev in ("console", "file"):
        rows.append((dev, "", "开发旁路（stdout / outbox.jsonl）"))
    return rows


def run_send(context: Context, args) -> int:
    """``send``：脚本化出站。**不需要网关在线、不经过 agent loop**。

    退出码照 Hermes：``0`` 成功 / ``1`` 投递或后端错误 / ``2`` 用法错误。
    """
    service = _service(context)
    if _flag(args, "list"):
        # Hermes 允许 `hermes send --list telegram`：位置参数在这里当过滤器用。
        wanted = _text(args, "message").strip().lower()
        rows = [row for row in _target_rows(service) if not wanted or wanted in row[0]]
        context.out("可用投递目标：")
        context.table(("平台", "频道", "说明"), rows)
        pending = [
            platform.value for platform in ALL_PLATFORMS if platform not in IMPLEMENTED_PLATFORMS
        ]
        context.out()
        context.out(
            f"另有 {len(pending)} 个平台仅声明（发消息会明确报 PlatformNotImplemented）："
            f"{'、'.join(pending)}"
        )
        context.emit({"ok": True, "targets": [row[0] for row in rows], "pending": pending})
        return 0

    raw_target = _text(args, "to").strip()
    if not raw_target:
        raise CliError("用法：orbit send --to <platform[:chat_id[:thread_id]]> [message]")
    try:
        target = parse_target(raw_target)
    except ValueError as exc:
        raise CliError(f"{exc}（用法错误）", code=2) from exc
    known = {platform.value for platform in ALL_PLATFORMS} | set(service.adapters)
    if target.platform not in known:
        raise CliError(
            f"未知平台：{target.platform!r}。可用：{'、'.join(sorted(known))}",
            code=2,
        )
    body = _read_body(context, args)
    subject = _text(args, "subject").strip()
    if subject:
        body = f"{subject}\n{body}"

    media = body.strip()
    if media.startswith("MEDIA:"):
        # `MEDIA:<path>` 是 Hermes 约定的附件语法。附件没实现就如实报错，
        # 绝不把路径当文本发出去（那会让用户以为图已经发出去了）。
        path = media[len("MEDIA:") :].strip()
        adapter = service.adapter_for(target.platform)
        outcome = adapter.send_media(target.channel, path)
        if isinstance(outcome, NotSupported):
            raise CliError(
                f"平台 {target.platform} 不支持发附件：{outcome.hint}。（本轮只实现了文本投递）"
            )
        if not outcome.ok:
            raise CliError(f"投递失败：{outcome.error}")
        context.out(f"已发送附件：{target.raw} → {path}")
        context.emit({"ok": True, "target": target.raw, "media": path})
        return 0

    result = service.deliver(body, platform=target.platform, channel=target.channel)
    if result.not_implemented:
        # 平台没实现不是"稍后再试"，直接给可行动的话（原因 + 需要哪些凭证）。
        raise CliError(result.error)
    if not result.ok:
        raise CliError(
            f"投递失败（尝试 {result.attempts} 次）：{result.error}"
            "。账本见 <data_dir>/gateway/deliveries.jsonl"
        )
    context.out(
        f"已发送：{target.raw} → 会话 {result.session_key}"
        f"（尝试 {result.attempts} 次，message_id={result.message_id or '-'}）"
    )
    context.emit(result.as_dict())
    return 0


# ── webhook ─────────────────────────────────────────────────────────────────


def _webhooks(context: Context) -> gw.WebhookStore:
    return gw.WebhookStore(context.data_dir / gw.GATEWAY_DIRNAME / gw.WEBHOOKS_FILENAME)


def run_webhook(context: Context, args) -> int:
    """``webhook {subscribe,list,remove,test}``：事件驱动唤醒的入口。"""
    action = _subcommand_name(args)
    store = _webhooks(context)
    if action == "list":
        items = store.load()
        if not items:
            context.out("没有任何 webhook 订阅。创建：orbit webhook subscribe <name> --prompt '...'")
            context.emit({"ok": True, "subscriptions": []})
            return 0
        context.table(
            ("名称", "路由", "事件", "投递", "仅投递", "创建时间"),
            [
                (
                    item.name,
                    item.path,
                    "、".join(item.events) or "（全部）",
                    item.deliver + (f":{item.deliver_chat_id}" if item.deliver_chat_id else ""),
                    "是" if item.deliver_only else "否",
                    item.created_at or "-",
                )
                for item in sorted(items.values(), key=lambda entry: entry.name)
            ],
        )
        context.emit({"ok": True, "subscriptions": [item.as_dict() for item in items.values()]})
        return 0

    name = _slug(_text(args, "name"), "订阅名")
    if action == "remove":
        if not store.remove(name):
            raise CliError(f"没有这个订阅：{name}")
        context.out(f"已删除订阅：{name}")
        context.emit({"ok": True, "removed": name})
        return 0

    if action == "test":
        items = store.load()
        subscription = items.get(name)
        if subscription is None:
            raise CliError(f"没有这个订阅：{name}")
        raw_payload = _text(args, "payload").strip()
        payload: dict[str, Any] = {"event": "test", "test": True}
        if raw_payload:
            try:
                parsed = json.loads(raw_payload)
            except ValueError as exc:
                raise CliError(f"--payload 不是合法 JSON：{exc}") from exc
            if not isinstance(parsed, dict):
                raise CliError("--payload 必须是 JSON 对象。")
            payload = parsed
        body = subscription.render(payload)
        signature = hmac.new(
            subscription.secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        context.out(f"路由：POST {subscription.path}")
        context.out(f"渲染后的正文：{body or '（空——prompt 没配）'}")
        context.out(f"X-Orbit-Signature: sha256={signature}")
        delivered = ""
        if subscription.deliver and subscription.deliver != "log":
            service = _service(context)
            result = service.deliver(
                body or f"[webhook {name}]",
                platform=subscription.deliver.split(":")[0],
                channel=subscription.deliver_chat_id,
            )
            if not result.ok:
                raise CliError(f"测试投递失败：{result.error}")
            delivered = result.session_key
            context.out(f"已投递到 {subscription.deliver}（会话 {delivered}）")
        else:
            context.out("投递目标是 log：只记日志，不发消息（--deliver-only 需要真实目标）。")
        context.emit(
            {
                "ok": True,
                "name": name,
                "path": subscription.path,
                "payload": payload,
                "rendered": body,
                "signature": signature,
                "delivered": delivered,
            }
        )
        return 0

    # subscribe
    deliver = _text(args, "deliver", "log").strip() or "log"
    deliver_only = _flag(args, "deliver_only")
    if deliver_only and deliver == "log":
        raise CliError(
            "--deliver-only 需要真实投递目标（不能是 log）：它的语义是跳过 agent、"
            "把渲染后的正文直接发出去。"
        )
    secret = _text(args, "secret").strip() or secrets.token_hex(16)
    subscription = gw.WebhookSubscription(
        name=name,
        prompt=_text(args, "prompt"),
        events=_split_csv(getattr(args, "events", None)),
        description=_text(args, "description"),
        skills=_split_csv(getattr(args, "skills", None)),
        deliver=deliver,
        deliver_chat_id=_text(args, "deliver_chat_id"),
        secret=secret,
        deliver_only=deliver_only,
        script=_text(args, "script"),
        created_at=gw.now_iso(),
    )
    store.upsert(subscription)
    context.out(f"已创建订阅：{name}")
    context.out(f"  路由：POST {subscription.path}")
    context.out(f"  HMAC 密钥：{secret}（只显示这一次，请保存）")
    if subscription.script:
        context.out(
            f"  过滤脚本：{subscription.script}"
            "（stdin 收 JSON；空输出/[SILENT]/非零退出 ⇒ 忽略）"
        )
    context.emit(subscription.as_dict())
    return 0


# ── pairing ─────────────────────────────────────────────────────────────────


def run_pairing(context: Context, args) -> int:
    """``pairing {list,approve,revoke,clear-pending}``：DM 配对的授权面。"""
    action = _subcommand_name(args)
    store = gw.PairingStore(context.data_dir / gw.PAIRING_DIRNAME)
    if action == "list":
        pending = store.pending()
        approved = store.approved()
        context.out(f"待批准：{len(pending)} 条")
        if pending:
            context.table(
                ("平台", "请求 ID", "配对码", "用户", "时间"),
                [
                    (
                        str(item.get("platform", "")),
                        str(item.get("request_id", "")),
                        str(item.get("code", "")),
                        str(item.get("user_name") or item.get("user_id") or "-"),
                        str(item.get("created_at", "")),
                    )
                    for item in pending
                ],
            )
        context.out()
        context.out(f"已批准：{len(approved)} 个用户")
        if approved:
            context.table(
                ("平台", "用户 ID", "用户", "批准时间"),
                [
                    (
                        str(item.get("platform", "")),
                        str(item.get("user_id", "")),
                        str(item.get("user_name") or "-"),
                        str(item.get("approved_at", "")),
                    )
                    for item in approved
                ],
            )
        context.emit({"ok": True, "pending": pending, "approved": approved})
        return 0

    if action == "clear-pending":
        cleared = store.clear_pending()
        context.out(f"已清空 {cleared} 条待批准请求。")
        context.emit({"ok": True, "cleared": cleared})
        return 0

    platform = _text(args, "platform").strip().lower()
    if not platform:
        raise CliError("用法：orbit pairing {approve,revoke} <platform> <id>")
    if action == "approve":
        wanted = _text(args, "request_id_or_code").strip()
        if not wanted:
            raise CliError("用法：orbit pairing approve <platform> <request-id|code>")
        entry = store.approve(platform, wanted)
        if entry is None:
            raise CliError(
                f"在 {platform} 的待批准列表里找不到 {wanted!r}"
                "（用 `orbit pairing list` 看请求 ID 与配对码）。"
            )
        context.out(
            f"已批准：{platform} 用户 {entry.get('user_id')}（{entry.get('user_name') or '-'}）"
        )
        context.emit({"ok": True, "platform": platform, "approved": entry})
        return 0

    # revoke
    user_id = _text(args, "user_id").strip()
    if not user_id:
        raise CliError("用法：orbit pairing revoke <platform> <user_id>")
    if not store.revoke(platform, user_id):
        raise CliError(f"{platform} 的授权名单里没有用户 {user_id!r}")
    context.out(f"已吊销：{platform} 用户 {user_id}")
    context.emit({"ok": True, "platform": platform, "revoked": user_id})
    return 0


# ── peer ────────────────────────────────────────────────────────────────────


def _peers(context: Context) -> gw.PeerRegistry:
    return gw.PeerRegistry(context.data_dir / gw.GATEWAY_DIRNAME / gw.PEERS_FILENAME)


def _split_peer(target: str) -> tuple[str, str]:
    """``<peer>[/<agent>]``：``/`` 之后是多路复用 peer 上的具名 profile。"""
    text = str(target or "").strip()
    name, _, agent = text.partition("/")
    return name.strip().lower(), agent.strip()


def run_peer(context: Context, args) -> int:
    """``peer {add,list,remove,status}``：跨机 bot 间私信的登记面。"""
    action = _subcommand_name(args)
    registry = _peers(context)
    if action == "list":
        peers = registry.load()
        if not peers:
            context.out("没有已注册的对等节点。添加：orbit peer add <name> --url http://host:port")
            context.emit({"ok": True, "peers": []})
            return 0
        context.table(
            ("名称", "URL", "密钥", "备注", "更新时间"),
            [
                (
                    name,
                    str(item.get("url", "")),
                    gw.PeerRegistry.mask(str(item.get("key", ""))),
                    str(item.get("note", "")),
                    str(item.get("updated_at", "")),
                )
                for name, item in sorted(peers.items())
            ],
        )
        context.emit(
            {
                "ok": True,
                "peers": [
                    {**item, "key": gw.PeerRegistry.mask(str(item.get("key", "")))}
                    for item in peers.values()
                ],
            }
        )
        return 0

    if action == "remove":
        name = _slug(_text(args, "name"), "对等节点名")
        if not registry.remove(name):
            raise CliError(f"没有这个对等节点：{name}")
        context.out(f"已移除：{name}")
        context.emit({"ok": True, "removed": name})
        return 0

    if action == "status":
        name, agent = _split_peer(_text(args, "target"))
        run_id = _text(args, "run_id").strip()
        if not name or not run_id:
            raise CliError("用法：orbit peer status <peer>[/<agent>] <run_id>")
        peers = registry.load()
        if name not in peers:
            raise CliError(f"没有这个对等节点：{name}（先 `orbit peer add`）")
        # 如实说明：异步 run 的查询要靠对端的 api_server 平台，而它本轮没实现。
        raise CliError(
            f"对等运行状态查询尚未实现：{name}{'/' + agent if agent else ''} 的 run {run_id!r}"
            " 需要远端跑 api_server 平台（Orbit 本轮未实现该平台），本地也没有 run 账本可查。"
            "登记信息本身是好的（peer list 可见）。",
            code=3,
        )

    # add
    name = _slug(_text(args, "name"), "对等节点名")
    url = _text(args, "url").strip()
    if not url:
        raise CliError("peer add 需要 --url（对端网关地址，如 http://spark.lan:8377）")
    if not url.startswith(("http://", "https://")):
        raise CliError(f"--url 必须是 http(s) 地址：{url!r}")
    record = registry.add(
        name, url, key=_text(args, "key").strip(), note=_text(args, "note").strip()
    )
    context.out(f"已登记对等节点：{name} → {url}")
    context.out(f"  密钥：{gw.PeerRegistry.mask(str(record.get('key', '')))}（脱敏显示）")
    context.emit({**record, "key": gw.PeerRegistry.mask(str(record.get("key", "")))})
    return 0


# ── 接线 ────────────────────────────────────────────────────────────────────


def _bind(path: str, handler: Any) -> None:
    """把实现登记到声明表的某条命令路径上（两种导入顺序都要成立）。

    与 ``sessions.py`` 的 ``_bind`` 同一个理由：生产接线里本模块可以排在
    ``hermes_parity`` 之后（它不登记顶层命令），此时声明表**已经组装完毕**，
    ``HANDLERS`` 里的登记不会再回填到已构建的 ``Command`` 上（它会停在"尚未实现"
    的占位 handler）。所以这里补一次回填：路径已存在就替换 handler、状态改 ``done``；
    不存在就什么都不做，留给 ``register_specs`` 去填。
    """
    register_handler(path, handler)
    head, _, leaf = path.partition(" ")
    parent = REGISTRY.get(head)
    target = parent.child(leaf) if (parent is not None and leaf) else parent
    if target is not None and not target.subcommands:
        target.handler = handler
        target.state = "done"


def _doctor_command() -> Command:
    """``gateway doctor`` 的 Command 形态（表已构建时直接挂上去用）。"""
    return Command(
        name="doctor",
        help="检查网关健康度（状态文件 / 锁 / 崩溃循环 / 适配器）",
        handler=run_doctor,
        group="run",
        state="done",
        hermes="gateway status --deep",
        parity="P1",
        needs_settings=True,
    )


#: ``gateway doctor`` 的**声明**形态。表里没有它，所以要么挂到已构建的 Command 上，
#: 要么在表组装前塞进 ``gateway`` 的 ``Spec.subcommands`` 里（见下）。
_DOCTOR_SPEC = Spec(
    name="doctor",
    help="检查网关健康度（状态文件 / 锁 / 崩溃循环 / 适配器）",
    group="run",
    parity="P1",
)


def _inject_doctor_spec(specs: tuple[Spec, ...]) -> tuple[Spec, ...]:
    """把 ``doctor`` 插进 ``gateway`` 的声明里（表组装**前**生效）。"""
    patched: list[Spec] = []
    for spec in specs:
        if spec.name == "gateway" and all(sub.name != "doctor" for sub in spec.subcommands):
            patched.append(replace(spec, subcommands=(*spec.subcommands, _DOCTOR_SPEC)))
        else:
            patched.append(spec)
    return tuple(patched)


def _install_spec_hook() -> None:
    """包一层 ``register_specs``，让表组装时把 ``doctor`` 一起登记。

    为什么需要这一步：``app/cli/commands/__init__.py`` 把本模块排在
    ``hermes_parity`` **之前**（和别的功能域模块一样），所以本模块导入时声明表还
    没组装，``REGISTRY.get("gateway")`` 是 ``None``——直接 append 会静默丢掉
    ``gateway doctor``。这里改成钩住表组装：``register_specs`` 拿到 SPECS 后先插
    我们的子命令声明，再照常登记。两条路径（前/后）都成立，接线顺序就不再是坑。
    """
    current = _table.register_specs
    if getattr(current, "_orbit_gateway_hook", False):
        return

    def wrapper(specs: tuple[Spec, ...]) -> list[str]:
        return current(_inject_doctor_spec(specs))

    setattr(wrapper, "_orbit_gateway_hook", True)  # noqa: B010 - 打标记防重复包装
    _table.register_specs = wrapper


def _attach_extra_subcommands() -> None:
    """把表里没有的 ``gateway doctor`` 挂上去（两种导入顺序都要成立）。

    表里没有它，是因为 Hermes 把诊断散在 ``gateway status --deep`` / ``doctor`` /
    ``monitoring status`` 三处；Orbit 需要一个"一条命令回答网关是否健康"的入口。
    """
    parent = REGISTRY.get("gateway")
    if parent is not None:
        if parent.child("doctor") is None:
            parent.subcommands.append(_doctor_command())
        return
    _install_spec_hook()


_bind("gateway status", run_status)
_bind("gateway setup", run_setup)
_bind("gateway start", run_start)
_bind("gateway stop", run_stop)
_bind("gateway restart", run_restart)
_bind("send", run_send)
_bind("webhook subscribe", run_webhook)
_bind("webhook list", run_webhook)
_bind("webhook remove", run_webhook)
_bind("webhook test", run_webhook)
_bind("pairing list", run_pairing)
_bind("pairing approve", run_pairing)
_bind("pairing revoke", run_pairing)
_bind("pairing clear-pending", run_pairing)
_bind("peer add", run_peer)
_bind("peer list", run_peer)
_bind("peer remove", run_peer)
_bind("peer status", run_peer)
# `gateway doctor` 是表里没有的扩展子命令：**必须先把 handler 登记进 HANDLERS**，
# 再让 `_attach_extra_subcommands()` 注入它的声明——否则 `_DOCTOR_SPEC.build()`
# 查不到 handler，`gateway doctor` 会停在"尚未实现"的占位实现上（实测踩过）。
_bind("gateway doctor", run_doctor)
_attach_extra_subcommands()

__all__ = [
    "run_doctor",
    "run_pairing",
    "run_peer",
    "run_restart",
    "run_send",
    "run_setup",
    "run_start",
    "run_status",
    "run_stop",
    "run_webhook",
]
