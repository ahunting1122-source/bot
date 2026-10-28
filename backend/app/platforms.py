"""多平台适配层：22 个平台枚举、适配器基类契约、17 个可离线验证的适配器。

为什么单独一层（而不是把"发消息"塞进 tools 或 cron）：

- Hermes 的 22 个平台之所以能**零改核心**接入，靠的是一套固定的适配器契约
  （``BasePlatformAdapter`` + ``ctx.register_platform()``）。契约在，第三方就能加平台；
  契约不在，每加一个平台都要动一次核心。Orbit 现在整层缺失，先把**契约**钉下来，
  再谈实现——这比"先接一个 telegram"值钱得多。
- 契约的权威来源：``handoff/hermes-parity/research/05-plugin-gateway-layer.md``
  §2.13.1（22 个适配器明细）+ §3（Gateway 机制）+ §6 的第 3️⃣ 项
  （7 必选 / 5 可选发送 / 5 交互 UX / 按钮回调 id 约定 / 5 边缘 hook）。

本轮的诚实边界（不要被下面的类名骗了）：

- **17 个平台真的能发消息**（22 里的 17），分三组，全部由 :class:`WebhookAdapter`
  覆盖，可离线用 ``httpx.MockTransport`` 全链路验证 URL 与 body：
  1. 简单 HTTP POST：``ntfy`` / ``raft`` / ``a2a``（各自的 body 形状写在工厂里）；
  2. 8 个 webhook 平台：``slack`` / ``discord`` / ``google_chat`` / ``teams`` /
     ``dingtalk`` / ``wecom`` / ``feishu`` / ``telegram``（形状在 `WEBHOOK_PLATFORMS`）；
  3. 6 个 REST 平台：``mattermost`` / ``line`` / ``whatsapp``（Cloud API）/ ``sms``
     （Twilio）/ ``homeassistant`` / ``matrix``——它们比纯 webhook 多三样东西：
     **Bearer/Basic 认证**、**URL 路径里带资源 id**（Matrix 的 room + txn）、
     **form-encoded body**（Twilio）。见 `REST_PLATFORMS`。
  剩下 5 个（``buzz`` / ``email`` / ``irc`` / ``photon`` / ``simplex``）只有声明
  （``implemented=False``），一发送就抛 :class:`PlatformNotImplemented` 并说清缺什么
  凭证。**不假装支持**。
- ``ConsoleAdapter`` / ``FileAdapter`` 是**开发旁路**（stdout / ``outbox.jsonl``），
  不对应任何真实平台：它们存在的理由是"本机无网络时也要能端到端验证投递路径"。
- 交互 UX（按钮/菜单/模态/线程/引用回复）默认**返回** :class:`NotSupported`，
  不抛异常——Hermes 的约定是"未覆写则优雅降级为纯文本"，抛异常会让一条带按钮的
  回复整条发不出去。
"""

from __future__ import annotations

import json
import os
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

# ── 平台枚举 ────────────────────────────────────────────────────────────────


class Platform(StrEnum):
    """Hermes ``plugins/platforms/`` 下的 22 个适配器，一个不多一个不少。

    这里只放**标识**（值即 Hermes 的平台名，用于 ``--to telegram:...`` 与
    ``channel_directory.json`` 的键）。一行中文说明、是否已实现、缺什么凭证
    统一放在 :data:`PLATFORMS` 里——单一事实源，免得枚举与表格两处对不上。
    """

    A2A = "a2a"
    BUZZ = "buzz"
    DINGTALK = "dingtalk"
    DISCORD = "discord"
    EMAIL = "email"
    FEISHU = "feishu"
    GOOGLE_CHAT = "google_chat"
    HOMEASSISTANT = "homeassistant"
    IRC = "irc"
    LINE = "line"
    MATRIX = "matrix"
    MATTERMOST = "mattermost"
    NTFY = "ntfy"
    PHOTON = "photon"
    RAFT = "raft"
    SIMPLEX = "simplex"
    SLACK = "slack"
    SMS = "sms"
    TEAMS = "teams"
    TELEGRAM = "telegram"
    WECOM = "wecom"
    WHATSAPP = "whatsapp"


#: 22 个平台的展示顺序（按 Hermes 目录名排序，便于和调研表逐行核对）。
ALL_PLATFORMS: tuple[Platform, ...] = tuple(Platform)


@dataclass(frozen=True)
class PlatformSpec:
    """一个平台的声明。**没有实现也要把"缺什么"写清楚**。

    为什么 ``reason`` 是硬字段：报错只说"未实现"等于没说，用户下一步只能去翻源码。
    写清"需要哪个 SDK / 哪个 env"，用户才知道是去申请凭证还是等排期。
    """

    platform: Platform
    label: str
    #: 一行中文说明：这个平台靠什么接入（长连接 / Webhook / 轮询 / 本地回环）。
    description: str
    #: 需要的环境变量（凭证）。未实现时原样写进报错里。
    credentials: tuple[str, ...] = ()
    implemented: bool = False
    #: ``implemented=True`` 时填适配器注册名；``False`` 时留空。
    adapter: str = ""
    #: 已实现平台的**覆盖范围**（说清"能做什么、不能做什么"）。
    scope: str = ""
    #: 未实现的原因 + 需要哪些凭证。``implemented=False`` 时必填。
    reason: str = ""


PLATFORMS: dict[Platform, PlatformSpec] = {
    Platform.A2A: PlatformSpec(
        platform=Platform.A2A,
        label="A2A",
        description="HTTP 服务端：暴露 Agent Card（/.well-known/agent-card.json）与 JSON-RPC 消息端点",
        credentials=("A2A_TOKEN",),
        implemented=True,
        adapter="a2a",
        scope="仅出站文本（JSON-RPC message/send）；Agent Card 发现、入站服务端、流式响应未做",
    ),
    Platform.BUZZ: PlatformSpec(
        platform=Platform.BUZZ,
        label="Buzz",
        description="轮询 + 本地 CLI：shell out 到 buzz 命令，走 Nostr relay 收发",
        credentials=("BUZZ_PRIVATE_KEY",),
        reason="需要 Nostr 私钥与本地 buzz CLI；本机无网络也装不了 buzz，无法验证任何一步",
    ),
    Platform.DINGTALK: PlatformSpec(
        platform=Platform.DINGTALK,
        label="钉钉",
        description="长连接：dingtalk-stream SDK 的 Stream Mode，不需要公网回调地址",
        implemented=True,
        credentials=("DINGTALK_WEBHOOK_URL",),
        adapter="dingtalk",
        scope="仅出站文本（webhook POST / Bot API sendMessage）；无入站（长连接 / 回调 / 轮询）、无媒体、无按钮、无线程",
    ),
    Platform.DISCORD: PlatformSpec(
        platform=Platform.DISCORD,
        label="Discord",
        description="长连接：discord.py 的 Gateway WebSocket，支持 threads 与全套交互按钮",
        implemented=True,
        credentials=("DISCORD_WEBHOOK_URL",),
        adapter="discord",
        scope="仅出站文本（webhook POST / Bot API sendMessage）；无入站（长连接 / 回调 / 轮询）、无媒体、无按钮、无线程",
    ),
    Platform.EMAIL: PlatformSpec(
        platform=Platform.EMAIL,
        label="邮件",
        description="轮询：IMAP 收信 + SMTP 发信，邮件线程当会话线程",
        credentials=("EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_SMTP_HOST"),
        reason="IMAP/SMTP 都是真实网络协议，需要邮箱账号密码与服务器地址；离线只能伪造，等于没实现",
    ),
    Platform.FEISHU: PlatformSpec(
        platform=Platform.FEISHU,
        label="飞书",
        description="双模：lark-oapi WebSocket 长连接，或 Webhook 回调",
        implemented=True,
        credentials=("FEISHU_WEBHOOK_URL",),
        adapter="feishu",
        scope="仅出站文本（webhook POST / Bot API sendMessage）；无入站（长连接 / 回调 / 轮询）、无媒体、无按钮、无线程",
    ),
    Platform.GOOGLE_CHAT: PlatformSpec(
        platform=Platform.GOOGLE_CHAT,
        label="Google Chat",
        description="Webhook 回调或 Cloud Pub/Sub pull，鉴权用 Service Account JSON",
        implemented=True,
        credentials=("GOOGLE_CHAT_WEBHOOK_URL",),
        adapter="google_chat",
        scope="仅出站文本（webhook POST / Bot API sendMessage）；无入站（长连接 / 回调 / 轮询）、无媒体、无按钮、无线程",
    ),
    Platform.HOMEASSISTANT: PlatformSpec(
        platform=Platform.HOMEASSISTANT,
        label="Home Assistant",
        description="WebSocket 长连接订阅事件总线；出站走 REST 服务调用 /api/services/notify/<service>",
        credentials=("HASS_URL", "HASS_TOKEN"),
        implemented=True,
        adapter="homeassistant",
        scope="仅出站文本（POST /api/services/notify/<service>，Bearer 长期令牌）；"
        "无入站（WebSocket 事件订阅 / 回调 / 轮询）、无实体状态读取、无 domain/entity 过滤与冷却",
    ),
    Platform.IRC: PlatformSpec(
        platform=Platform.IRC,
        label="IRC",
        description="长连接：stdlib asyncio 裸 IRC 协议，零外部依赖",
        credentials=("IRC_SERVER", "IRC_CHANNEL", "IRC_NICKNAME"),
        reason="协议是纯 stdlib 可写的，但必须有真实 IRC 服务器才能验证收发；本机无网络",
    ),
    Platform.LINE: PlatformSpec(
        platform=Platform.LINE,
        label="LINE",
        description="入站 Webhook（aiohttp + HMAC-SHA256 签名校验）；出站走 push 文本消息",
        credentials=("LINE_CHANNEL_ACCESS_TOKEN",),
        implemented=True,
        adapter="line",
        scope="仅出站文本（POST /v2/bot/message/push，Bearer channel access token）；"
        "无入站 webhook 与 HMAC 签名校验、无 Template Buttons / 图片 / 贴图、无线程",
    ),
    Platform.MATRIX: PlatformSpec(
        platform=Platform.MATRIX,
        label="Matrix",
        description="长连接同步（mautrix，支持 E2EE 与 threads）；出站走 client-server REST send",
        credentials=("MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN"),
        implemented=True,
        adapter="matrix",
        scope="仅出站文本（PUT /_matrix/client/v3/rooms/<room>/send/m.room.message/<txn>，"
        "Bearer access token；room id 按 RFC 3986 编码）；无入站 /sync 长连接、无端到端加密、"
        "无 threads 与 reaction 交互",
    ),
    Platform.MATTERMOST: PlatformSpec(
        platform=Platform.MATTERMOST,
        label="Mattermost",
        description="WebSocket 长连接 + v4 REST API（出站走 REST）",
        credentials=("MATTERMOST_URL", "MATTERMOST_TOKEN"),
        implemented=True,
        adapter="mattermost",
        scope="仅出站文本（POST /api/v4/posts，Bearer bot/personal token；channel 传 channel_id）；"
        "无入站（WebSocket 长连接 / 回调 / 轮询）、无文件上传、无按钮与 thread 回复",
    ),
    Platform.NTFY: PlatformSpec(
        platform=Platform.NTFY,
        label="ntfy",
        description="HTTP 发布：POST 纯文本到 <server>/<topic>，订阅侧是流式 HTTP",
        credentials=("NTFY_URL",),
        implemented=True,
        adapter="ntfy",
        scope="仅出站文本（ntfy 的 publish 语义就是 POST 正文）；订阅侧流式收信未做",
    ),
    Platform.PHOTON: PlatformSpec(
        platform=Platform.PHOTON,
        label="Photon",
        description="长连接 gRPC：经受监管的 Node sidecar（无 webhook / 无签名密钥）",
        credentials=("PHOTON_PROJECT_ID", "PHOTON_PROJECT_SECRET"),
        reason="需要 spectrum-ts Node sidecar + 项目凭证 + 设备登录 token（auth.json）",
    ),
    Platform.RAFT: PlatformSpec(
        platform=Platform.RAFT,
        label="Raft",
        description="本地回环 HTTP + wake channel：bridge 推 content-free 唤醒提示，不碰消息体",
        credentials=("RAFT_PROFILE",),
        implemented=True,
        adapter="raft",
        scope="仅出站文本到本地回环 bridge URL；wake channel 的唤醒语义未做",
    ),
    Platform.SIMPLEX: PlatformSpec(
        platform=Platform.SIMPLEX,
        label="SimpleX",
        description="长连接 WebSocket：连本地 simplex-chat daemon",
        credentials=("SIMPLEX_WS_URL",),
        reason="需要一个跑起来的 simplex-chat daemon（本机没有），且 SimpleX 联系人 ID 是连接时生成的不透明值",
    ),
    Platform.SLACK: PlatformSpec(
        platform=Platform.SLACK,
        label="Slack",
        description="长连接 Socket Mode（slack-bolt），无需公网 URL，支持 threads 与 Block Kit",
        implemented=True,
        credentials=("SLACK_WEBHOOK_URL",),
        adapter="slack",
        scope="仅出站文本（webhook POST / Bot API sendMessage）；无入站（长连接 / 回调 / 轮询）、无媒体、无按钮、无线程",
    ),
    Platform.SMS: PlatformSpec(
        platform=Platform.SMS,
        label="短信",
        description="Twilio：Webhook 收 + REST 发（form-encoded To/From/Body）",
        credentials=("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"),
        implemented=True,
        adapter="sms",
        scope="仅出站文本（POST /2010-04-01/Accounts/<sid>/Messages.json，form-encoded + "
        "HTTP Basic 认证）；无入站 webhook 回调、无分段与计费对账、**不剥 markdown**"
        "（长度按原文算，调用方自己保证是纯文本）",
    ),
    Platform.TEAMS: PlatformSpec(
        platform=Platform.TEAMS,
        label="Teams",
        description="Webhook：Bot Framework 回调，审批用 Adaptive Card",
        implemented=True,
        credentials=("TEAMS_WEBHOOK_URL",),
        adapter="teams",
        scope="仅出站文本（webhook POST / Bot API sendMessage）；无入站（长连接 / 回调 / 轮询）、无媒体、无按钮、无线程",
    ),
    Platform.TELEGRAM: PlatformSpec(
        platform=Platform.TELEGRAM,
        label="Telegram",
        description="长轮询（默认）或 Webhook：python-telegram-bot，含直连 IP 故障转移",
        implemented=True,
        credentials=("TELEGRAM_BOT_TOKEN",),
        adapter="telegram",
        scope="仅出站文本（webhook POST / Bot API sendMessage）；无入站（长连接 / 回调 / 轮询）、无媒体、无按钮、无线程",
    ),
    Platform.WECOM: PlatformSpec(
        platform=Platform.WECOM,
        label="企业微信",
        description="双模：智能机器人 WebSocket 长连接 + 自建应用 HTTP 回调（AES 加解密）",
        implemented=True,
        credentials=("WECOM_WEBHOOK_URL",),
        adapter="wecom",
        scope="仅出站文本（webhook POST / Bot API sendMessage）；无入站（长连接 / 回调 / 轮询）、无媒体、无按钮、无线程",
    ),
    Platform.WHATSAPP: PlatformSpec(
        platform=Platform.WHATSAPP,
        label="WhatsApp",
        description="双模：本地 Node.js bridge 跑 WhatsApp Web（扫码配对，未实现）/ "
        "Meta Cloud API 出站（已实现）",
        credentials=("WHATSAPP_CLOUD_TOKEN", "WHATSAPP_CLOUD_PHONE_NUMBER_ID"),
        implemented=True,
        adapter="whatsapp_cloud",
        scope="仅出站文本（Meta Graph POST /v20.0/<phone_number_id>/messages，Bearer token；"
        "channel 传对方 E.164 号码）；无入站 webhook 与 X-Hub-Signature 校验、无媒体与模板消息、"
        "本地 Node bridge 的扫码配对未实现",
    ),
}

#: 真的能发消息的平台（其余 5 个一发送就抛 :class:`PlatformNotImplemented`）。
IMPLEMENTED_PLATFORMS: tuple[Platform, ...] = tuple(
    platform for platform, spec in PLATFORMS.items() if spec.implemented
)


def spec_for(platform: Platform | str) -> PlatformSpec:
    """按名字/枚举取声明。未知名字抛 :class:`PlatformNotImplemented`（说清"不是 22 个之一"）。"""
    if isinstance(platform, Platform):
        return PLATFORMS[platform]
    try:
        return PLATFORMS[Platform(str(platform).strip().lower())]
    except ValueError as exc:
        raise PlatformNotImplemented(str(platform)) from exc


# ── 异常与结果 ──────────────────────────────────────────────────────────────


class PlatformNotImplemented(RuntimeError):
    """该平台本轮没有实现。消息里**必须**带原因与所需凭证。

    用异常而不是 ``SendResult(ok=False)``：调用方（cron 投递、``orbit send``）看到
    "发送失败"会当成网络抖动去重试，而"平台没实现"重试一万次也不会成功。
    """

    def __init__(self, platform: str, *, reason: str = "", credentials: tuple[str, ...] = ()) -> None:
        self.platform = str(platform)
        self.reason = reason
        self.credentials = credentials
        super().__init__(self._message())

    def _message(self) -> str:
        try:
            spec = PLATFORMS[Platform(self.platform)]
        except ValueError:
            return f"平台 {self.platform!r} 不是 Orbit 支持的 22 个平台之一，无法投递。"
        if spec.implemented:
            return f"平台 {spec.label} 已实现，但本次调用越过了实现（不应发生）。"
        needs = "、".join(spec.credentials) or "（无明确凭证要求）"
        return (
            f"平台 {spec.label}（{spec.platform.value}）本轮未实现：{spec.reason}。"
            f"需要凭证：{needs}。"
        )


@dataclass(frozen=True)
class SendResult:
    """一次发送的结果。``ok=False`` 时 ``error`` 必非空。"""

    ok: bool
    platform: str = ""
    channel: str = ""
    message_id: str = ""
    error: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "platform": self.platform,
            "channel": self.channel,
            "message_id": self.message_id,
            "error": self.error,
            **self.detail,
        }


@dataclass(frozen=True)
class NotSupported:
    """能力不支持的**返回值**（不是异常）。

    为什么不用异常：Hermes 的约定是未覆写就优雅降级为纯文本。一条带按钮的回复若因为
    "平台没有按钮"整条失败，用户连正文都收不到——这比降级糟糕得多。
    """

    capability: str
    platform: str = ""
    hint: str = ""

    @property
    def ok(self) -> bool:
        return False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": False,
            "not_supported": self.capability,
            "platform": self.platform,
            "hint": self.hint,
        }


#: 发送类方法的返回值：要么真发了，要么明确"不支持"。
SendOutcome = SendResult | NotSupported


@dataclass(frozen=True)
class AdapterHealth:
    """适配器自检结果。``ok=False`` 时 ``detail`` 要说清"哪一步不成立"。"""

    ok: bool
    platform: str = ""
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "platform": self.platform, "detail": self.detail, **self.extra}


# ── 按钮回调 id 约定 ────────────────────────────────────────────────────────

#: 澄清选择（clarify）：``cl:<id>:<idx>``，``idx`` 可以是序号或字面量 ``other``。
CALLBACK_CLARIFY = "cl"
#: 执行审批（approval）：``appr:<id>:<choice>``，``choice`` ∈ once/session/always/deny。
CALLBACK_APPROVAL = "appr"
#: 斜杠命令确认（slash confirm）：``sc:<choice>:<id>``，注意 **choice 在前**。
CALLBACK_SLASH = "sc"
#: 0.21.0-cn.18 的 telegram 适配器实测用的是 ``ea:<choice>:<id>``（exec approval）。
#: 调研文档 §3 写的是 ``appr:``，两边形状一致、前缀不同；解析时一并认，
#: 构造时仍按文档用 ``appr:``——否则和 Hermes 其它平台对不上。
CALLBACK_APPROVAL_LEGACY = "ea"

#: 每种前缀的字段顺序。``True`` = ``<id>:<choice>``，``False`` = ``<choice>:<id>``。
_CALLBACK_LAYOUT: dict[str, bool] = {
    CALLBACK_CLARIFY: True,
    CALLBACK_APPROVAL: True,
    CALLBACK_SLASH: False,
    CALLBACK_APPROVAL_LEGACY: False,
}

#: 审批的合法选项（照 telegram ``send_exec_approval`` 的四个按钮）。
APPROVAL_CHOICES: tuple[str, ...] = ("once", "session", "always", "deny")
#: 斜杠确认的合法选项（照 telegram ``send_slash_confirm`` 的三个按钮）。
SLASH_CHOICES: tuple[str, ...] = ("once", "always", "cancel")


@dataclass(frozen=True)
class CallbackRef:
    """解析出来的回调引用。"""

    kind: str
    ident: str
    choice: str

    @property
    def raw(self) -> str:
        return build_callback_id(self.kind, self.ident, self.choice)


def build_callback_id(kind: str, ident: str, choice: str) -> str:
    """按约定的字段顺序拼一个回调 id。

    ``kind`` 必须在前缀表里；``ident`` / ``choice`` 里**不能有冒号**——telegram 的
    ``callback_data`` 只有 64 字节，且解析靠 ``split(":")``，多一个冒号就错位。
    """
    prefix = str(kind).strip().lower()
    if prefix not in _CALLBACK_LAYOUT:
        raise ValueError(f"未知的回调前缀：{kind!r}（可选：{'/'.join(_CALLBACK_LAYOUT)}）")
    for label, value in (("ident", ident), ("choice", choice)):
        text = str(value)
        if not text or ":" in text:
            raise ValueError(f"回调 id 的 {label} 必须非空且不含冒号：{value!r}")
    ident_text, choice_text = str(ident), str(choice)
    if _CALLBACK_LAYOUT[prefix]:
        return f"{prefix}:{ident_text}:{choice_text}"
    return f"{prefix}:{choice_text}:{ident_text}"


def parse_callback_id(raw: str) -> CallbackRef | None:
    """解析回调 id。不认识的形状返回 ``None``（**不抛**）。

    为什么不抛：回调是外部输入（用户点按钮），一个陌生按钮 id 不该让整个回调处理器
    崩掉——返回 ``None`` 让调用方"忽略这一下"就够了。
    """
    text = str(raw or "").strip()
    parts = text.split(":")
    if len(parts) != 3:
        return None
    prefix, first, second = parts[0].lower(), parts[1], parts[2]
    if not first or not second:
        return None
    layout = _CALLBACK_LAYOUT.get(prefix)
    if layout is None:
        return None
    if layout:
        return CallbackRef(kind=prefix, ident=first, choice=second)
    return CallbackRef(kind=prefix, ident=second, choice=first)


# ── 交互 UX 能力 ────────────────────────────────────────────────────────────


class UxCapability(StrEnum):
    """交互 UX 的 5 个类别（按钮/菜单/模态/线程/引用回复）。

    它们和下面 5 个 ``send_*`` 方法是**两个视角**：``send_*`` 是"发什么交互"，
    ``UxCapability`` 是"这个平台支不支持这类呈现"。做能力探测时用后者
    （``supports_ux``），做发送时用前者；两者默认都降级，不抛异常。
    """

    BUTTONS = "buttons"
    MENU = "menu"
    MODAL = "modal"
    THREAD = "thread"
    QUOTE_REPLY = "quote_reply"


UX_LABELS: dict[UxCapability, str] = {
    UxCapability.BUTTONS: "按钮",
    UxCapability.MENU: "菜单",
    UxCapability.MODAL: "模态",
    UxCapability.THREAD: "线程",
    UxCapability.QUOTE_REPLY: "引用回复",
}


# ── 适配器基类 ──────────────────────────────────────────────────────────────


class PlatformAdapter(ABC):
    """平台适配器契约。**照 Hermes ``BasePlatformAdapter`` 的形状，逐条对齐**。

    契约分五组（数量写死，测试会核对）：

    1. **7 个必选**（``REQUIRED_*``）：``name`` + ``start`` / ``stop`` / ``send_text`` /
       ``send_media`` / ``health`` / ``standalone_sender_fn``。
    2. **5 个可选发送**（``OPTIONAL_SEND_METHODS``）：typing / reaction / edit / delete /
       buttons。基类给"不支持"的默认值。
    3. **5 个交互 UX**（``INTERACTION_METHODS``）：clarify / 审批 / 斜杠确认 / 模型选择 /
       选择器。**默认返回** :class:`NotSupported`（降级为纯文本，不抛）。
    4. **按钮回调 id 约定**：见 :func:`build_callback_id` / :func:`parse_callback_id`。
    5. **5 个边缘 hook**（``EDGE_HOOKS``）：连接/断开/错误/限流/入站消息。

    关于 ``standalone_sender_fn`` 为什么是**必选**（这是整层最难排查的坑）：
    Hermes 里它是可选 hook，缺了以后 ``deliver=<platform>`` 的 cron 任务**触发正常、
    发送静默失败**，只在日志里留一行 ``No live adapter for platform '<name>'``。
    定时任务于是"跑了但没结果"，用户完全看不出来。Orbit 把它设成抽象方法：
    漏实现就是 ``TypeError``（类都建不起来），而不是半夜静默丢消息。
    """

    #: 适配器注册名（``--to <name>`` 与 ``ADAPTERS`` 的键）。子类必须给。
    name: str = ""
    #: 对应的平台；开发旁路（console / file）为 ``None``。
    platform: Platform | None = None

    #: 7 个必选（``name`` 是属性，其余是方法）。
    REQUIRED_ATTRS: tuple[str, ...] = ("name",)
    REQUIRED_METHODS: tuple[str, ...] = (
        "start",
        "stop",
        "send_text",
        "send_media",
        "health",
        "standalone_sender_fn",
    )
    #: 5 个可选发送方法（基类有默认桩）。
    OPTIONAL_SEND_METHODS: tuple[str, ...] = (
        "send_typing",
        "send_reaction",
        "edit_message",
        "delete_message",
        "send_buttons",
    )
    #: 5 个交互 UX 方法（默认降级，返回 NotSupported）。
    INTERACTION_METHODS: tuple[str, ...] = (
        "send_clarify",
        "send_exec_approval",
        "send_slash_confirm",
        "send_model_picker",
        "send_choice_picker",
    )
    #: 5 个边缘 hook（默认空实现，gateway 负责在对应时机调用）。
    EDGE_HOOKS: tuple[str, ...] = (
        "on_connect",
        "on_disconnect",
        "on_error",
        "on_rate_limited",
        "on_message",
    )

    def __init__(self, *, name: str = "") -> None:
        if name:
            self.name = name
        if not self.name:
            raise ValueError(f"{type(self).__name__} 必须给 name（适配器注册名）")

    # ── 7 个必选 ──
    @abstractmethod
    def start(self) -> None:
        """开始接收入站消息（长连接 / 轮询 / 起 HTTP 服务端）。"""

    @abstractmethod
    def stop(self) -> None:
        """停止接收。必须可重复调用（重复 stop 不报错）。"""

    @abstractmethod
    def send_text(self, channel: str, text: str) -> SendResult:
        """发纯文本。``channel`` 是平台原生 chat id 或 ``#频道名``。"""

    @abstractmethod
    def send_media(self, channel: str, path: str, *, caption: str = "") -> SendOutcome:
        """发附件（图片/文件）。平台不支持就返回 :class:`NotSupported`。"""

    @abstractmethod
    def health(self) -> AdapterHealth:
        """自检：凭证在不在、连接活不活。**不能**因为没连上就抛异常。"""

    @abstractmethod
    def standalone_sender_fn(self) -> StandaloneSender:
        """返回"进程外投递"函数：cron 与 gateway 不同进程时靠它发送。

        签名 ``(channel, text) -> SendResult``。见类文档里"缺它会让 cron 投递
        静默失败"的说明——所以它是必选，不是可选。
        """

    # ── 5 个可选发送（默认"不支持"，不抛） ──
    def send_typing(self, channel: str) -> SendOutcome:
        """「正在输入」指示。"""
        return self._not_supported("send_typing", "该平台没有 typing 指示")

    def send_reaction(self, channel: str, message_id: str, emoji: str) -> SendOutcome:
        """给某条消息加表情回应。"""
        return self._not_supported("send_reaction", "该平台没有表情回应接口")

    def edit_message(self, channel: str, message_id: str, text: str) -> SendOutcome:
        """就地编辑一条已发出的消息。"""
        return self._not_supported("edit_message", "该平台不支持编辑已发消息")

    def delete_message(self, channel: str, message_id: str) -> SendOutcome:
        """撤回一条消息。"""
        return self._not_supported("delete_message", "该平台不支持撤回")

    def send_buttons(
        self, channel: str, text: str, buttons: tuple[tuple[str, str], ...]
    ) -> SendOutcome:
        """带按钮的消息。``buttons`` 是 ``(标签, 回调 id)`` 的序列。"""
        return self._not_supported("send_buttons", "该平台没有按钮，降级为纯文本")

    # ── 5 个交互 UX（默认降级，返回 NotSupported） ──
    def send_clarify(
        self, channel: str, question: str, options: tuple[str, ...], *, clarify_id: str = ""
    ) -> SendOutcome:
        """澄清提问（按钮/菜单）。不支持时调用方应退回纯文本提问。"""
        return self._not_supported("send_clarify", "没有交互按钮，降级为纯文本提问")

    def send_exec_approval(
        self, channel: str, command: str, *, approval_id: str = "", description: str = ""
    ) -> SendOutcome:
        """执行审批（allow once / session / always / deny 四个按钮）。"""
        return self._not_supported("send_exec_approval", "没有审批按钮，降级为文本 /approve 流程")

    def send_slash_confirm(
        self, channel: str, title: str, message: str, *, confirm_id: str = ""
    ) -> SendOutcome:
        """斜杠命令确认（once / always / cancel）。"""
        return self._not_supported("send_slash_confirm", "没有确认按钮，降级为文本确认")

    def send_model_picker(self, channel: str, models: tuple[str, ...]) -> SendOutcome:
        """模型选择器（按钮或下拉）。"""
        return self._not_supported("send_model_picker", "没有选择器，降级为文本列表")

    def send_choice_picker(self, channel: str, choices: tuple[str, ...]) -> SendOutcome:
        """通用选择器。"""
        return self._not_supported("send_choice_picker", "没有选择器，降级为文本列表")

    # ── 能力探测 ──
    def ux_capabilities(self) -> frozenset[UxCapability]:
        """该适配器支持的交互 UX 类别。默认**一个都没有**。"""
        return frozenset()

    def supports_ux(self, capability: UxCapability | str) -> bool:
        try:
            wanted = UxCapability(str(capability))
        except ValueError:
            return False
        return wanted in self.ux_capabilities()

    # ── 5 个边缘 hook（默认空实现） ──
    def on_connect(self) -> None:
        """连上之后（可用来重建频道目录、补投积压消息）。"""
        return None

    def on_disconnect(self) -> None:
        """断开之后（可用来标记健康度、退避重连）。"""
        return None

    def on_error(self, error: BaseException) -> None:
        """任何一步出错（**不改变**该错误的传播语义，只用于记录/计数）。"""
        return None

    def on_rate_limited(self, retry_after: float = 0.0) -> None:
        """被平台限流。``retry_after`` 是平台给的等待秒数（没给就是 0）。"""
        return None

    def on_message(self, channel: str, text: str, *, user_id: str = "") -> None:
        """收到一条入站消息（gateway 在路由到会话之前调它）。"""
        return None

    # ── 辅助 ──
    def _not_supported(self, capability: str, hint: str) -> NotSupported:
        return NotSupported(capability=capability, platform=self.name, hint=hint)

    def describe(self) -> dict[str, Any]:
        """给 ``gateway status`` / ``doctor`` 用的一行描述。"""
        return {
            "name": self.name,
            "platform": self.platform.value if self.platform else "",
            "required": list(self.REQUIRED_METHODS),
            "optional_send": list(self.OPTIONAL_SEND_METHODS),
            "interaction": list(self.INTERACTION_METHODS),
            "edge_hooks": list(self.EDGE_HOOKS),
            "ux": sorted(cap.value for cap in self.ux_capabilities()),
        }


#: 进程外投递函数的签名。
StandaloneSender = Callable[..., SendResult]


# ── 三个可离线验证的适配器 ──────────────────────────────────────────────────


class ConsoleAdapter(PlatformAdapter):
    """开发/单机用：把消息打到 stdout。

    为什么需要它：``orbit send --to console "hi"`` 是唯一**零依赖、零凭证**的
    端到端验证入口。网关、cron 投递、路由任何一环断了，跑这条命令就能定位。
    """

    name = "console"
    platform = None

    def start(self) -> None:  # 没有入站，起停都是空操作
        return None

    def stop(self) -> None:
        return None

    def send_text(self, channel: str, text: str) -> SendResult:
        target = channel or "(默认)"
        print(f"[console → {target}] {text}")
        return SendResult(ok=True, platform=self.name, channel=target, message_id="console")

    def send_media(self, channel: str, path: str, *, caption: str = "") -> SendOutcome:
        suffix = f"（{caption}）" if caption else ""
        print(f"[console → {channel or '(默认)'}] MEDIA:{path}{suffix}")
        return SendResult(ok=True, platform=self.name, channel=channel, message_id="console")

    def health(self) -> AdapterHealth:
        return AdapterHealth(ok=True, platform=self.name, detail="stdout 恒可用")

    def standalone_sender_fn(self) -> StandaloneSender:
        return self.send_text


class FileAdapter(PlatformAdapter):
    """离线断言用：把消息**追加**到 ``<data_dir>/outbox.jsonl``。

    追加而不是重写：outbox 是流水账，进程被杀也要留下已发生的那部分（和
    ``cron/runs.jsonl`` 同一个理由）。测试因此可以"发三条、读三行"地断言。
    """

    name = "file"
    platform = None

    def __init__(self, data_dir: str | Path, *, name: str = "file") -> None:
        super().__init__(name=name)
        self.data_dir = Path(data_dir)

    @property
    def outbox_path(self) -> Path:
        return self.data_dir / "outbox.jsonl"

    def start(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def stop(self) -> None:
        return None

    def send_text(self, channel: str, text: str) -> SendResult:
        return self._append(kind="text", channel=channel, text=text)

    def send_media(self, channel: str, path: str, *, caption: str = "") -> SendOutcome:
        return self._append(kind="media", channel=channel, text=path, caption=caption)

    def health(self) -> AdapterHealth:
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return AdapterHealth(ok=False, platform=self.name, detail=f"数据目录不可写：{exc}")
        return AdapterHealth(ok=True, platform=self.name, detail=str(self.outbox_path))

    def standalone_sender_fn(self) -> StandaloneSender:
        return self.send_text

    def _append(self, *, kind: str, channel: str, text: str, caption: str = "") -> SendResult:
        record = {
            "ts": time.time(),
            "kind": kind,
            "platform": self.name,
            "channel": channel,
            "text": text,
        }
        if caption:
            record["caption"] = caption
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with self.outbox_path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            return SendResult(ok=False, platform=self.name, channel=channel, error=f"写 outbox 失败：{exc}")
        return SendResult(ok=True, platform=self.name, channel=channel, message_id=str(record["ts"]))


class WebhookAdapter(PlatformAdapter):
    """出站 HTTP 请求。**17 个已实现平台的出站全部是它**。

    **httpx transport 必须可注入**（``transport=``）：本机无网络，测试用
    ``httpx.MockTransport`` 造成功 / 5xx / 超时三种响应。可注入不是为了"方便测试"，
    而是为了这一层能**在无网络环境下被完整验证**——否则它只是一段没人跑过的代码。

    重试语义（照 Hermes 的退避思路，但简化）：
    - 5xx / 连接错误 / 超时 → 重试；
    - 4xx → 立刻失败（客户端错，重试一万次也一样）；
    - 每次尝试都记在 ``SendResult.detail["attempts"]`` 里，便于对账。

    比"纯 webhook"多出来的三件东西（6 个 REST 平台要用，见 `REST_PLATFORMS`）：

    - ``method``：Matrix 的 send 是 **PUT**（不是 POST）；
    - ``url_template``：资源 id 在**路径**里（Matrix 的 room + txn），且必须按
      RFC 3986 编码（``!abc:example.org`` → ``%21abc%3Aexample.org``）；
    - ``auth`` / ``env_names``：Bearer 与 HTTP Basic 两种认证，以及"缺哪个环境变量"
      的精确报错。
    """

    name = "webhook"
    platform = None

    #: 请求体形状。``text`` = 纯文本正文（ntfy publish）；``json`` = ``{"text":...}``；
    #: ``jsonrpc`` = A2A 的 ``message/send``；``template`` = 用 ``body_template``
    #: 渲染平台各自的 JSON 形状（slack / discord / feishu / line / matrix 等）；
    #: ``form`` = 用 ``body_template`` 渲染 **form-encoded** 字段（Twilio 的 To/From/Body）。
    BODY_SHAPES: tuple[str, ...] = ("text", "json", "jsonrpc", "template", "form")

    def __init__(
        self,
        url: str = "",
        *,
        name: str = "webhook",
        platform: Platform | None = None,
        body_shape: str = "json",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 5.0,
        attempts: int = 3,
        retry_delay: float = 0.0,
        headers: Mapping[str, str] | None = None,
        body_template: Mapping[str, Any] | None = None,
        url_hint: str = "NTFY_URL",
        method: str = "POST",
        url_template: str = "",
        quote_channel: bool = False,
        auth: tuple[str, str] | None = None,
        env_names: tuple[str, ...] = (),
        missing_env: tuple[str, ...] = (),
    ) -> None:
        super().__init__(name=name)
        if body_shape not in self.BODY_SHAPES:
            raise ValueError(f"未知请求体形状：{body_shape!r}（可选：{'/'.join(self.BODY_SHAPES)}）")
        if body_shape in {"template", "form"} and body_template is None:
            raise ValueError(f"body_shape={body_shape!r} 时必须给 body_template")
        self.url = url
        self.platform = platform
        self.body_shape = body_shape
        self.body_template = body_template
        self.transport = transport
        self.timeout = timeout
        self.attempts = max(1, int(attempts))
        self.retry_delay = max(0.0, float(retry_delay))
        self.headers = dict(headers or {})
        #: 未配置时该提示哪个环境变量。写死 `NTFY_URL` 会误导——8 个 webhook 平台
        #: 各有自己的变量名（`SLACK_WEBHOOK_URL` / `TELEGRAM_BOT_TOKEN` …），
        #: 提示错名字等于让人去改一个不存在的配置。
        self.url_hint = url_hint
        #: HTTP 方法。Matrix 的 send 是 PUT，其余都是 POST。
        self.method = str(method or "POST").upper()
        #: URL 模板（可选）。支持 ``{base}`` / ``{channel}`` / ``{txn}``；留空 = 用 ``self.url``。
        self.url_template = url_template
        #: True = 把 channel 当**路径段**按 RFC 3986 编码（Matrix 的 room id 必须编）。
        self.quote_channel = quote_channel
        #: HTTP Basic 认证（Twilio 用 SID:AuthToken）。Bearer 走 headers，不走这里。
        self.auth = auth
        #: 这个适配器读哪些环境变量（用于"缺哪个变量"的报错，以及 health 的展示）。
        self.env_names = tuple(env_names)
        #: 构造时已经确认**缺失**的环境变量（工厂函数算好传进来）。
        self.missing_env = tuple(missing_env)

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def send_text(self, channel: str, text: str) -> SendResult:
        # txn 在**重试循环之外**生成一次：Matrix 的 `/<txn>` 是幂等键，每次重试换一个
        # 就等于"同一条消息发 N 遍"（用户看到重复，服务端也多算）。
        txn = uuid.uuid4().hex
        target = self._target(channel, txn)
        if not target or self.missing_env:
            return SendResult(
                ok=False,
                platform=self.name,
                channel=channel,
                error=self._unconfigured_message(),
            )
        payload = self._payload(channel, text)
        headers = {"User-Agent": "orbit-gateway/1", **self.headers}
        if self.body_shape == "text":
            headers.setdefault("Content-Type", "text/plain; charset=utf-8")
        last_error = ""
        used = 0
        # 客户端只建一次：MockTransport / 真实 transport 都能跨重试复用连接。
        with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
            for attempt in range(1, self.attempts + 1):
                used = attempt
                try:
                    response = client.request(
                        self.method, target, headers=headers, auth=self.auth, **payload
                    )
                except httpx.TimeoutException as exc:
                    last_error = f"超时（第 {attempt} 次）：{exc!r}"
                except httpx.HTTPError as exc:
                    last_error = f"连接错误（第 {attempt} 次）：{exc!r}"
                else:
                    if response.status_code < 400:
                        return SendResult(
                            ok=True,
                            platform=self.name,
                            channel=channel,
                            message_id=str(response.headers.get("id") or response.status_code),
                            detail={"attempts": used, "status": response.status_code},
                        )
                    last_error = f"HTTP {response.status_code}（第 {attempt} 次）"
                    if response.status_code < 500:
                        # 4xx：请求本身有问题，别浪费重试预算。
                        break
                if attempt < self.attempts and self.retry_delay:
                    time.sleep(self.retry_delay)
        return SendResult(
            ok=False,
            platform=self.name,
            channel=channel,
            error=last_error or "未知发送错误",
            detail={"attempts": used},
        )

    def _target(self, channel: str, txn: str) -> str:
        """算出这次请求的完整 URL（``url_template`` 里填 ``{base}/{channel}/{txn}``）。

        ``quote_channel`` 的用途：Matrix 的 room id 形如 ``!abc:example.org``，感叹号与
        冒号直接塞进路径会被服务端当成别的资源（或直接 404），必须先按 RFC 3986 编码。
        """
        if not self.url:
            return ""
        if not self.url_template:
            return self.url
        room = quote(str(channel), safe="") if self.quote_channel else str(channel)
        return (
            self.url_template.replace("{base}", self.url.rstrip("/"))
            .replace("{channel}", room)
            .replace("{txn}", txn)
        )

    def _unconfigured_message(self) -> str:
        """未配置时的报错：**点名这个平台自己的环境变量**，绝不写别人的名字。"""
        names = self.env_names or (self.url_hint,)
        if self.missing_env:
            return (
                f"未配置 {self.name} 的凭证：设置环境变量 {'、'.join(self.missing_env)}"
                f"（该适配器读：{'、'.join(names)}）后重试"
            )
        return (
            f"未配置出站地址：设置环境变量 {'、'.join(names)} 或显式传 url"
            f"（{self.name} 的出站形状见 app/platforms.py 的 "
            f"WEBHOOK_PLATFORMS / REST_PLATFORMS）"
        )

    def send_media(self, channel: str, path: str, *, caption: str = "") -> SendOutcome:
        # 附件上传是 multipart + 平台各异的上传端点，本轮不做——如实说不支持，
        # 而不是把路径当文本发出去（那会让用户以为图发出去了）。
        return self._not_supported("send_media", "本适配器只发文本，附件上传未实现")

    def health(self) -> AdapterHealth:
        if not self.url or self.missing_env:
            # "没配 URL" 是**未启用**，不是故障：把未配置算成不健康会让 `gateway doctor`
            # 在干净环境里永远失败，于是用户学会忽略它——那比不检查更坏。
            detail = (
                self._unconfigured_message()
                if self.missing_env
                else "未配置出站 URL（未启用；设置对应环境变量后生效）"
            )
            return AdapterHealth(
                ok=True,
                platform=self.name,
                detail=detail,
                extra={
                    "configured": False,
                    "env": list(self.env_names),
                    "missing_env": list(self.missing_env),
                },
            )
        return AdapterHealth(
            ok=True,
            platform=self.name,
            detail=f"{self.method} {self.body_shape} → {self.url}",
            extra={
                "attempts": self.attempts,
                "timeout": self.timeout,
                "configured": True,
                "env": list(self.env_names),
            },
        )

    def standalone_sender_fn(self) -> StandaloneSender:
        return self.send_text

    def _payload(self, channel: str, text: str) -> dict[str, Any]:
        if self.body_shape == "form" and self.body_template is not None:
            # Twilio 要 form-encoded（application/x-www-form-urlencoded），不是 JSON；
            # 字段值必须都是字符串，否则 httpx 会按 `str()` 拼出 "None" / "True"。
            rendered = _render(self.body_template, channel, text)
            return {"data": {str(key): str(value) for key, value in rendered.items()}}
        if self.body_shape == "template" and self.body_template is not None:
            # 平台各自的 JSON 形状差别很大（slack 要 `text`、discord 要 `content`、
            # feishu 要 `msg_type` + 嵌套 `content.text`）。用一个模板表达，比在
            # `_payload` 里堆 if 更不容易写错，也更容易逐平台对照官方文档核对。
            return {"json": _render(self.body_template, channel, text)}
        if self.body_shape == "text":
            return {"content": text.encode("utf-8")}
        if self.body_shape == "jsonrpc":
            return {
                "json": {
                    "jsonrpc": "2.0",
                    "id": uuid.uuid4().hex,
                    "method": "message/send",
                    "params": {
                        "message": {
                            "role": "user",
                            "parts": [{"kind": "text", "text": text}],
                            "metadata": {"channel": channel},
                        }
                    },
                }
            }
        return {"json": {"text": text, "channel": channel}}


class UnimplementedAdapter(PlatformAdapter):
    """5 个未实现平台的占位适配器：**每个发送方法都抛** :class:`PlatformNotImplemented`。

    为什么不给"空成功"：假装支持比不支持更坏——cron 会以为投递成功，用户永远等不到
    那条消息，而且没有任何日志线索。
    """

    def __init__(self, platform: Platform | str) -> None:
        self.spec = spec_for(platform)
        super().__init__(name=self.spec.platform.value)
        self.platform = self.spec.platform

    def _refuse(self) -> PlatformNotImplemented:
        return PlatformNotImplemented(
            self.spec.platform.value,
            reason=self.spec.reason,
            credentials=self.spec.credentials,
        )

    def start(self) -> None:
        raise self._refuse()

    def stop(self) -> None:
        raise self._refuse()

    def send_text(self, channel: str, text: str) -> SendResult:
        raise self._refuse()

    def send_media(self, channel: str, path: str, *, caption: str = "") -> SendOutcome:
        raise self._refuse()

    def health(self) -> AdapterHealth:
        return AdapterHealth(
            ok=False,
            platform=self.name,
            detail=f"未实现：{self.spec.reason}",
            extra={"credentials": list(self.spec.credentials)},
        )

    def standalone_sender_fn(self) -> StandaloneSender:
        raise self._refuse()

    # 可选发送/交互 UX 也一并拒绝：未实现的平台连"降级"都没有承载方。
    def send_typing(self, channel: str) -> SendOutcome:
        raise self._refuse()

    def send_reaction(self, channel: str, message_id: str, emoji: str) -> SendOutcome:
        raise self._refuse()

    def edit_message(self, channel: str, message_id: str, text: str) -> SendOutcome:
        raise self._refuse()

    def delete_message(self, channel: str, message_id: str) -> SendOutcome:
        raise self._refuse()

    def send_buttons(
        self, channel: str, text: str, buttons: tuple[tuple[str, str], ...]
    ) -> SendOutcome:
        raise self._refuse()


# ── 适配器注册表 ────────────────────────────────────────────────────────────


def ntfy_adapter(
    *, url: str | None = None, transport: httpx.BaseTransport | None = None
) -> WebhookAdapter:
    """ntfy：publish 的语义就是"POST 纯文本到 <server>/<topic>"。"""
    return WebhookAdapter(
        url if url is not None else os.environ.get("NTFY_URL", ""),
        name="ntfy",
        platform=Platform.NTFY,
        body_shape="text",
        transport=transport,
    )


def raft_adapter(
    *, url: str | None = None, transport: httpx.BaseTransport | None = None
) -> WebhookAdapter:
    """raft：本地回环 HTTP bridge，默认指向 127.0.0.1（不猜端口之外的语义）。"""
    return WebhookAdapter(
        url if url is not None else os.environ.get("RAFT_URL", "http://127.0.0.1:8377/raft"),
        name="raft",
        platform=Platform.RAFT,
        body_shape="json",
        transport=transport,
    )


def a2a_adapter(
    *, url: str | None = None, transport: httpx.BaseTransport | None = None
) -> WebhookAdapter:
    """a2a：出站是 JSON-RPC 的 ``message/send``。"""
    return WebhookAdapter(
        url if url is not None else os.environ.get("A2A_URL", ""),
        name="a2a",
        platform=Platform.A2A,
        body_shape="jsonrpc",
        transport=transport,
    )


def _render(template: Mapping[str, Any], channel: str, text: str) -> dict[str, Any]:
    """把 ``{channel}`` / ``{text}`` 占位符填进 JSON 模板（递归，只碰字符串值）。

    为什么用模板而不是每平台写一个 if 分支：8 个平台的出站形状两两不同，
    分支写法很容易在某个平台的嵌套层级上写错，而模板能逐平台对照官方文档核。
    占位符只在**字符串**里替换——key 不参与替换，避免用户输入把结构改掉。
    """

    def fill(value: Any) -> Any:
        if isinstance(value, str):
            return value.replace("{channel}", channel).replace("{text}", text)
        if isinstance(value, dict):
            return {key: fill(item) for key, item in value.items()}
        if isinstance(value, list):
            return [fill(item) for item in value]
        return value

    rendered = fill(template)
    return rendered if isinstance(rendered, dict) else dict(rendered)


#: 平台 → (环境变量名, JSON 模板)。**形状照各家官方文档**，逐条都能核对：
#: slack/discord/google_chat/teams 是 `text`/`content`；钉钉/企微是 `msgtype` + 嵌套
#: `text.content`；飞书是 `msg_type` + `content.text`；telegram 的 chat_id 在 body 里。
WEBHOOK_PLATFORMS: dict[Platform, tuple[str, dict[str, Any]]] = {
    Platform.SLACK: ("SLACK_WEBHOOK_URL", {"text": "{text}"}),
    Platform.DISCORD: ("DISCORD_WEBHOOK_URL", {"content": "{text}"}),
    Platform.GOOGLE_CHAT: ("GOOGLE_CHAT_WEBHOOK_URL", {"text": "{text}"}),
    Platform.TEAMS: ("TEAMS_WEBHOOK_URL", {"text": "{text}"}),
    Platform.DINGTALK: (
        "DINGTALK_WEBHOOK_URL",
        {"msgtype": "text", "text": {"content": "{text}"}},
    ),
    Platform.WECOM: (
        "WECOM_WEBHOOK_URL",
        {"msgtype": "text", "text": {"content": "{text}"}},
    ),
    Platform.FEISHU: (
        "FEISHU_WEBHOOK_URL",
        {"msg_type": "text", "content": {"text": "{text}"}},
    ),
    Platform.TELEGRAM: (
        "TELEGRAM_BOT_TOKEN",
        {"chat_id": "{channel}", "text": "{text}"},
    ),
}


def webhook_platform_adapter(
    platform: Platform,
    *,
    url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> WebhookAdapter:
    """按 `WEBHOOK_PLATFORMS` 造一个 webhook 适配器。

    telegram 特殊：URL 里要带 bot token（`/bot<token>/sendMessage`），所以它的
    环境变量存的是 **token** 而不是 URL——这一点容易搞错，单独在这里处理并注释。
    """
    env_name, template = WEBHOOK_PLATFORMS[platform]
    if platform is Platform.TELEGRAM:
        token = url if url is not None else os.environ.get(env_name, "")
        target = f"https://api.telegram.org/bot{token}/sendMessage" if token else ""
    else:
        target = url if url is not None else os.environ.get(env_name, "")
    return WebhookAdapter(
        target,
        name=platform.value,
        platform=platform,
        body_shape="template",
        body_template=template,
        transport=transport,
        url_hint=env_name,
    )


#: 6 个 REST 平台的出站契约（URL 形状 + 认证 + body）。**逐条照官方文档**，
#: 和 `WEBHOOK_PLATFORMS` 一样是"可核对的事实表"，不是散文：
#: - URL 里的 ``{base}`` / ``{sid}`` / ``{phone_id}`` 是构造期填好的，``{channel}``
#:   是**每次发送**才填的（Matrix 的 room 还要按 RFC 3986 编码）。
#: - ``Bearer`` 走 ``Authorization`` 头；``Basic`` 走 httpx 的 ``auth=`` 参数。
REST_PLATFORMS: dict[Platform, str] = {
    Platform.MATTERMOST: "POST {base}/api/v4/posts · Bearer MATTERMOST_TOKEN · "
    '{channel_id, message}',
    Platform.LINE: "POST https://api.line.me/v2/bot/message/push · "
    "Bearer LINE_CHANNEL_ACCESS_TOKEN · "
    '{"to": …, "messages": [{"type": "text", "text": …}]}',
    Platform.WHATSAPP: "POST https://graph.facebook.com/v20.0/{phone_number_id}/messages · "
    "Bearer WHATSAPP_CLOUD_TOKEN · "
    '{"messaging_product": "whatsapp", "to": …, "type": "text", "text": {"body": …}}',
    Platform.SMS: "POST https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json · "
    "Basic SID:AuthToken · form-encoded To/From/Body",
    Platform.HOMEASSISTANT: "POST {base}/api/services/notify/{service} · "
    "Bearer HASS_TOKEN · {message}",
    Platform.MATRIX: "PUT {base}/_matrix/client/v3/rooms/{room}/send/m.room.message/{txn} · "
    "Bearer MATRIX_ACCESS_TOKEN · "
    '{"msgtype": "m.text", "body": …}',
}


def _missing_env(names: tuple[str, ...]) -> tuple[str, ...]:
    """哪些环境变量没配（只看有没有值，不判断值对不对）。"""
    return tuple(name for name in names if not os.environ.get(name))


def mattermost_adapter(
    *,
    url: str | None = None,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> WebhookAdapter:
    """Mattermost：``POST <server>/api/v4/posts``，Bearer token，channel 传 channel_id。"""
    env_names = ("MATTERMOST_URL", "MATTERMOST_TOKEN")
    base = url if url is not None else os.environ.get("MATTERMOST_URL", "")
    bearer = token if token is not None else os.environ.get("MATTERMOST_TOKEN", "")
    missing = tuple(
        name
        for name, value in (("MATTERMOST_URL", base), ("MATTERMOST_TOKEN", bearer))
        if not value
    )
    return WebhookAdapter(
        f"{base.rstrip('/')}/api/v4/posts" if base else "",
        name="mattermost",
        platform=Platform.MATTERMOST,
        body_shape="template",
        body_template={"channel_id": "{channel}", "message": "{text}"},
        headers={"Authorization": f"Bearer {bearer}"} if bearer else {},
        transport=transport,
        url_hint="MATTERMOST_URL",
        env_names=env_names,
        missing_env=missing,
    )


def line_adapter(
    *,
    token: str | None = None,
    url: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> WebhookAdapter:
    """LINE：``POST /v2/bot/message/push``，Bearer channel access token。

    URL 是**固定**的（LINE 只有一个 push 端点），所以"配没配"只看 token——
    这也是 ``missing_env`` 存在的理由：URL 齐了但 token 空，仍然算未配置。
    """
    env_names = ("LINE_CHANNEL_ACCESS_TOKEN",)
    bearer = token if token is not None else os.environ.get("LINE_CHANNEL_ACCESS_TOKEN", "")
    return WebhookAdapter(
        url if url is not None else "https://api.line.me/v2/bot/message/push",
        name="line",
        platform=Platform.LINE,
        body_shape="template",
        body_template={"to": "{channel}", "messages": [{"type": "text", "text": "{text}"}]},
        headers={"Authorization": f"Bearer {bearer}"} if bearer else {},
        transport=transport,
        url_hint="LINE_CHANNEL_ACCESS_TOKEN",
        env_names=env_names,
        missing_env=() if bearer else env_names,
    )


def whatsapp_cloud_adapter(
    *,
    token: str | None = None,
    phone_number_id: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> WebhookAdapter:
    """WhatsApp Cloud API：``POST /v20.0/<phone_number_id>/messages``，Bearer token。

    ``to`` 是对方号码（E.164，不带 ``+``）——Cloud API 的 ``to`` 不接受 ``+``，
    这一点与"短信"平台不同，所以 channel 的写法由调用方按平台约定给。
    """
    env_names = ("WHATSAPP_CLOUD_TOKEN", "WHATSAPP_CLOUD_PHONE_NUMBER_ID")
    bearer = token if token is not None else os.environ.get("WHATSAPP_CLOUD_TOKEN", "")
    phone = (
        phone_number_id
        if phone_number_id is not None
        else os.environ.get("WHATSAPP_CLOUD_PHONE_NUMBER_ID", "")
    )
    missing = tuple(
        name
        for name, value in (("WHATSAPP_CLOUD_TOKEN", bearer),
                            ("WHATSAPP_CLOUD_PHONE_NUMBER_ID", phone))
        if not value
    )
    return WebhookAdapter(
        f"https://graph.facebook.com/v20.0/{phone}/messages" if phone else "",
        name="whatsapp_cloud",
        platform=Platform.WHATSAPP,
        body_shape="template",
        body_template={
            "messaging_product": "whatsapp",
            "to": "{channel}",
            "type": "text",
            "text": {"body": "{text}"},
        },
        headers={"Authorization": f"Bearer {bearer}"} if bearer else {},
        transport=transport,
        url_hint="WHATSAPP_CLOUD_TOKEN",
        env_names=env_names,
        missing_env=missing,
    )


def sms_adapter(
    *,
    account_sid: str | None = None,
    auth_token: str | None = None,
    from_number: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> WebhookAdapter:
    """短信（Twilio）：``POST /2010-04-01/Accounts/<sid>/Messages.json``。

    两个细节容易踩：① body 是 **form-encoded**（``To``/``From``/``Body`` 首字母大写），
    不是 JSON；② 认证是 **HTTP Basic**（SID 当用户名、Auth Token 当密码），
    不是 Bearer。
    """
    env_names = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER")
    sid = account_sid if account_sid is not None else os.environ.get("TWILIO_ACCOUNT_SID", "")
    secret = auth_token if auth_token is not None else os.environ.get("TWILIO_AUTH_TOKEN", "")
    sender = from_number if from_number is not None else os.environ.get("TWILIO_PHONE_NUMBER", "")
    missing = tuple(
        name
        for name, value in (
            ("TWILIO_ACCOUNT_SID", sid),
            ("TWILIO_AUTH_TOKEN", secret),
            ("TWILIO_PHONE_NUMBER", sender),
        )
        if not value
    )
    return WebhookAdapter(
        f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json" if sid else "",
        name="sms",
        platform=Platform.SMS,
        body_shape="form",
        body_template={"To": "{channel}", "From": sender, "Body": "{text}"},
        auth=(sid, secret) if (sid and secret) else None,
        transport=transport,
        url_hint="TWILIO_ACCOUNT_SID",
        env_names=env_names,
        missing_env=missing,
    )


def homeassistant_adapter(
    *,
    url: str | None = None,
    token: str | None = None,
    service: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> WebhookAdapter:
    """Home Assistant：``POST <hass>/api/services/notify/<service>``，Bearer 长期令牌。

    ``service`` 是 HA 里的通知服务名（``persistent_notification`` / ``mobile_app_xxx``），
    默认 ``notify``——猜一个具体的服务名等于替用户做决定，而发到错误的 service 是
    "消息凭空消失"这类最难查的故障。
    """
    env_names = ("HASS_URL", "HASS_TOKEN")
    base = url if url is not None else os.environ.get("HASS_URL", "")
    bearer = token if token is not None else os.environ.get("HASS_TOKEN", "")
    name = (
        service
        if service is not None
        else os.environ.get("HASS_NOTIFY_SERVICE", "notify")
    ) or "notify"
    missing = tuple(
        name_
        for name_, value in (("HASS_URL", base), ("HASS_TOKEN", bearer))
        if not value
    )
    return WebhookAdapter(
        f"{base.rstrip('/')}/api/services/notify/{name}" if base else "",
        name="homeassistant",
        platform=Platform.HOMEASSISTANT,
        body_shape="template",
        body_template={"message": "{text}"},
        headers={"Authorization": f"Bearer {bearer}"} if bearer else {},
        transport=transport,
        url_hint="HASS_URL",
        env_names=env_names,
        missing_env=missing,
    )


def matrix_adapter(
    *,
    homeserver: str | None = None,
    token: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> WebhookAdapter:
    """Matrix：``PUT /_matrix/client/v3/rooms/<room>/send/m.room.message/<txn>``。

    三处必须照规范：① 是 **PUT**（不是 POST）；② room id 在**路径**里且必须编码
    （``!abc:example.org`` → ``%21abc%3Aexample.org``）；③ txn 是幂等键，重试要复用
    同一个值（所以它在 `send_text` 的循环外生成）。
    """
    env_names = ("MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN")
    base = homeserver if homeserver is not None else os.environ.get("MATRIX_HOMESERVER", "")
    bearer = token if token is not None else os.environ.get("MATRIX_ACCESS_TOKEN", "")
    missing = tuple(
        name
        for name, value in (("MATRIX_HOMESERVER", base), ("MATRIX_ACCESS_TOKEN", bearer))
        if not value
    )
    return WebhookAdapter(
        base,
        name="matrix",
        platform=Platform.MATRIX,
        body_shape="template",
        body_template={"msgtype": "m.text", "body": "{text}"},
        headers={"Authorization": f"Bearer {bearer}"} if bearer else {},
        transport=transport,
        url_hint="MATRIX_HOMESERVER",
        method="PUT",
        url_template="{base}/_matrix/client/v3/rooms/{channel}/send/m.room.message/{txn}",
        quote_channel=True,
        env_names=env_names,
        missing_env=missing,
    )


#: 6 个 REST 平台的工厂表（`default_adapters` 遍历它，顺序即展示顺序）。
REST_ADAPTER_FACTORIES: dict[Platform, Any] = {
    Platform.MATTERMOST: mattermost_adapter,
    Platform.LINE: line_adapter,
    Platform.WHATSAPP: whatsapp_cloud_adapter,
    Platform.SMS: sms_adapter,
    Platform.HOMEASSISTANT: homeassistant_adapter,
    Platform.MATRIX: matrix_adapter,
}


def default_adapters(
    data_dir: str | Path | None = None, *, transport: httpx.BaseTransport | None = None
) -> dict[str, PlatformAdapter]:
    """默认适配器集合：**17 个已实现平台** + 2 个开发旁路（console / file）。

    17 个真实平台分三组：
    - 简单 HTTP POST：`ntfy` / `raft` / `a2a`（各自的 body 形状写在工厂里）
    - webhook 平台：`slack` / `discord` / `google_chat` / `teams` / `dingtalk` /
      `wecom` / `feishu` / `telegram`（形状在 `WEBHOOK_PLATFORMS`，逐条可对照官方文档）
    - REST 平台：`mattermost` / `line` / `whatsapp_cloud` / `sms` / `homeassistant` /
      `matrix`（形状在 `REST_PLATFORMS`；比 webhook 多 Bearer/Basic 认证、
      路径里的资源 id 与 form-encoded body）

    其余 5 个平台（`buzz` / `email` / `irc` / `photon` / `simplex`）仍然没有适配器，
    `resolve_adapter` 会返回 `UnimplementedAdapter`，发送时抛
    `PlatformNotImplemented` 并给出所需凭证与 SDK。**没有假装支持。**
    """
    pool: dict[str, PlatformAdapter] = {
        "console": ConsoleAdapter(),
        "file": FileAdapter(data_dir or Path.cwd()),
        "ntfy": ntfy_adapter(transport=transport),
        "raft": raft_adapter(transport=transport),
        "a2a": a2a_adapter(transport=transport),
    }
    for platform in WEBHOOK_PLATFORMS:
        pool[platform.value] = webhook_platform_adapter(platform, transport=transport)
    for platform, factory in REST_ADAPTER_FACTORIES.items():
        adapter = factory(transport=transport)
        # 注册名用适配器自己的 name（`whatsapp_cloud`），不是平台枚举值（`whatsapp`）：
        # `--to whatsapp_cloud:...` 与 `--to whatsapp:...` 都要能找到同一个适配器，
        # 所以两个键都登记（见下）。
        pool[adapter.name] = adapter
        if adapter.name != platform.value:
            pool[platform.value] = adapter
    return pool


def resolve_adapter(
    platform: str, adapters: Mapping[str, PlatformAdapter] | None = None
) -> PlatformAdapter:
    """按平台名取适配器。已知但没实现 → :class:`UnimplementedAdapter`（发送时才抛）。

    为什么不在取的时候就抛（对**已知未实现**的平台）：``gateway status`` 需要
    **列出**未实现的平台（连同原因），那是正常展示而不是错误。但**未知**平台名
    （不是 22 个之一，也不是 console/file 这类旁路）当场抛——名字写错了不该等到
    发送才发现。
    """
    name = str(platform or "").strip().lower()
    pool = adapters if adapters is not None else {}
    if name in pool:
        return pool[name]
    return UnimplementedAdapter(name)


# ── 目标语法（``--to`` 的解析） ─────────────────────────────────────────────


@dataclass(frozen=True)
class SendTarget:
    """``--to`` 解析结果。照 Hermes：``platform`` / ``platform:chat_id`` /
    ``platform:chat_id:thread_id`` / ``platform:#channel-name``。"""

    platform: str
    channel: str = ""
    thread: str = ""

    @property
    def raw(self) -> str:
        parts = [self.platform]
        if self.channel:
            parts.append(self.channel)
        if self.thread:
            parts.append(self.thread)
        return ":".join(parts)


def parse_target(raw: str) -> SendTarget:
    """解析 ``--to``。空串/只给平台名都合法（平台名 = 该平台的 home channel）。"""
    text = str(raw or "").strip()
    if not text:
        raise ValueError("投递目标为空：写法是 platform / platform:chat_id / platform:chat_id:thread_id")
    pieces = text.split(":")
    platform = pieces[0].strip().lower()
    if not platform:
        raise ValueError(f"投递目标缺少平台名：{raw!r}")
    channel = pieces[1].strip() if len(pieces) > 1 else ""
    thread = pieces[2].strip() if len(pieces) > 2 else ""
    if len(pieces) > 3:
        raise ValueError(f"投递目标层级过多（最多 platform:chat_id:thread_id）：{raw!r}")
    return SendTarget(platform=platform, channel=channel, thread=thread)


#: 22 个平台里已实现的 17 个（给 ``gateway status`` 直接引用，避免各处再算一遍）。
__all__ = [
    "ALL_PLATFORMS",
    "APPROVAL_CHOICES",
    "AdapterHealth",
    "CALLBACK_APPROVAL",
    "CALLBACK_APPROVAL_LEGACY",
    "CALLBACK_CLARIFY",
    "CALLBACK_SLASH",
    "CallbackRef",
    "ConsoleAdapter",
    "FileAdapter",
    "IMPLEMENTED_PLATFORMS",
    "NotSupported",
    "PLATFORMS",
    "Platform",
    "PlatformAdapter",
    "PlatformNotImplemented",
    "PlatformSpec",
    "REST_ADAPTER_FACTORIES",
    "REST_PLATFORMS",
    "SLASH_CHOICES",
    "SendOutcome",
    "SendResult",
    "SendTarget",
    "StandaloneSender",
    "UxCapability",
    "UX_LABELS",
    "UnimplementedAdapter",
    "WEBHOOK_PLATFORMS",
    "WebhookAdapter",
    "a2a_adapter",
    "build_callback_id",
    "default_adapters",
    "homeassistant_adapter",
    "line_adapter",
    "mattermost_adapter",
    "matrix_adapter",
    "ntfy_adapter",
    "parse_callback_id",
    "parse_target",
    "raft_adapter",
    "resolve_adapter",
    "sms_adapter",
    "spec_for",
    "whatsapp_cloud_adapter",
]
