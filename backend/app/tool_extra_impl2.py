"""MATRIX 5.14 第三轮的落地文件：**仓库里已经有了、只是没接成工具**的 5 条能力。

## 为什么再开一个文件（而不是塞进 `tool_extra_impl.py`）

`EXTRA_TOOLS`（第一轮 2 个）与 `GROWTH_TOOLS`（第二轮 5 个）的名字表被既有测试逐字
钉住，`tool_extra_impl.py` 里还放着分级披露的实测数字。本轮新增的放进
:data:`ROUND3_TOOLS` + 独立的注册函数，于是「第一轮 2 / 第二轮 5 / 第三轮 5」三批
可以分开数、分开回滚，也不动那两份测试的断言。

## 这一轮复核的结论：不是"再找活"，是"把已实现的接出来"

上一轮的结论是"剩下 63 个缺口 = 网络/凭证 + 浏览器/桌面运行时 + 看板服务端 + 工具层
结构上做不到"。本轮按**「`app/` 下有没有已经实现、只是没暴露成工具的能力」**重看了一遍，
逐条结论（每条都说清"为什么不加"或"加了以后涨不涨覆盖"）：

* ``app/session_search.py`` —— 四形态（discover / scroll / read / browse）已由
  `session_search` 全覆盖，`sessions` 的 27 条命令里剩下的 `pin` / `optimize`
  **不该给模型**：`pin` 是"这条别被压缩掉"的人工决定，`optimize` 会重写会话正文
  （不可逆）。它们不是能力缺口，是**权限边界**，跟 `skill_manage` 的删除那半同理。
* ``app/skill_hub.py`` —— `skill_save` 有、`skill_search` 没有。本地 hub 目录
  （`<data>/skills-hub/`）本来就能按名字/描述/identifier 搜（`SkillHub.search`），
  于是补了 :func:`run_skill_search`。**它不涨 Hermes 覆盖**：Hermes 只有
  `skills_list` / `skill_view` / `skill_manage` 三个，Orbit 早已覆盖。
* ``app/memory_providers.py`` —— 15 个 provider、其中 4 个是真实现。补
  :func:`run_memory_provider_list`（可用性 / 依赖是否装了 / 安装提示 / 为什么不可用，
  逐条如实报）。同样不涨覆盖：Hermes 的 `memory` 是一个工具。
* ``app/egress.py::describe()`` —— 补 :func:`run_egress_status`，让模型能查**自己的**
  出站策略（allow / deny / default / 凭证**键名**，永不回密钥值）。不涨覆盖。
* ``app/budget.py`` / ``app/turn_budgets.py`` / ``app/window.py`` —— 补
  :func:`run_budget_status`：一次说清"为什么我的工具输出被截断了"（窗口 → 单条/整轮
  字符阈值 → 落盘目录里堆了多少 → 要不要清理）。不涨覆盖。
* ``app/gateway.py::deliver`` + ``app/platforms.py``（19 个出站适配器 + console/file
  两条离线旁路）+ ``app/peer_client.py``（5 个端点的对等协议）—— **这条真的涨覆盖**：
  补 :func:`run_send_message`，覆盖 Hermes 的 `send_message`；顺带覆盖
  `message_agent`（"给另一个 agent 发消息"在仓库里本来就是现成的
  `PeerClient.dm` + `gateway/peers.json`，只是没接成工具）。
* ``app/worktree.py``（git worktree）**不**拿来冒充 Hermes 的 `desktop_project`：
  后者是切换**桌面客户端的命名工程**，Orbit 没有桌面客户端。"命名工作区"这个词对得上，
  语义对不上 —— 这类"词对得上、语义不对"的映射正是 `app/tool_catalog.py` 反复警告的
  假账，宁可留着缺口。

## 所以：31 → 33（+2），**不是 36+**

剩下 61 个缺口**全部**落在五类里，一条都不注册（注册了模型就会去调，调了必然失败，
而"调了才失败"比"没有这个工具"坏得多）：

| 类别 | 条数 | 谁 |
|---|---|---|
| 网络 / 平台凭证 | 17 | `x_search`、12 条 collab（discord / 飞书 / 元宝）、4 条 `ha_*` |
| 浏览器运行时 | 7 | `browser_back` / `_get_images` / `_vision` / `_console` / `_cdp` / `_dialog` / `_exec` |
| 桌面运行时 | 15 | `desktop_preview` 一族 + `tip` / `tour` / `computer_use` … |
| 看板服务端 | 14 | `kanban_*`（`app/` 下 grep 不到任何看板后端） |
| 要 provider / 结构性 | 6 | `image_generate` / `video_generate` / `xai_video_*` / `vision_analyze` / `video_analyze` |
| 委派链未通 | 2 | `delegate_task` / `agent_swarm`（`DelegateRunner` 的 `runner_factory` 全仓库无人注入） |

**36+ 这个目标本轮做不到，原因不是没找到活，而是剩下的活都要网络、要运行时，
或者要别人先把装配层那一条链接上。**

## 装配层要做什么（两处，各一行）

1. 注册工具：``tool_extra_impl2.register_round3_tools(registry)``；
2. 把 :data:`PENDING_TOOLSET_MAPPINGS` 里的 5 行抄进 ``app/toolsets.py`` 的
   ``TOOL_TOOLSET``（那个文件不归本文件改；没抄之前这 5 个工具会被判成
   ``UNCLASSIFIED``，而 `tests/test_tool_catalog.py` 的登记门禁会指到这里）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from app.tools import Tool

# ── 装配层要补的 toolset 映射 ────────────────────────────────────────────────
#
# `app/toolsets.py` 的 `TOOL_TOOLSET` 是"工具名 → toolset"的唯一真值源，而那个文件
# 本轮不归这里改。于是把"该加哪几行"写成数据：装配层照抄即可，测试也能拿它对账
# （`tests/test_tool_catalog.py` 的登记门禁允许这 5 个名字暂时缺席，别的名字缺席照样红）。
#
# **为什么 `send_message` 要新开一个 `collab` toolset**：`collab` 域现在只有
# `notion` 一个 toolset 在借撑（见 `toolsets.DOMAIN_TOOLSETS`），把"往消息平台投递"
# 塞进 notion 组会让 `orbit tools list` 的暴露面清单误导人。给这个域一个自己的
# toolset 之后，"关掉投递但保留 Notion 读写"才第一次成为可能。

#: 工具名 → 应该加进 `app/toolsets.py::TOOL_TOOLSET` 的 toolset。
#: 前四条是现成 toolset（`memory` / `skills` / `core`），第五条要求新开 `collab`。
PENDING_TOOLSET_MAPPINGS: dict[str, str] = {
    "send_message": "collab",
    "memory_provider_list": "memory",
    "skill_search": "skills",
    "egress_status": "core",
    "budget_status": "core",
}

#: 需要**新开**的 toolset（装配层要在 `toolsets.TOOLSETS` / `TOOLSET_TIERS` /
#: `DOMAIN_TOOLSETS` 三处各加一行）。空 = 不需要新开。
PENDING_NEW_TOOLSETS: dict[str, str] = {
    "collab": "协作与消息投递（send_message：19 家出站适配器 + console/file 离线旁路）",
}

# ── 参数模型 ────────────────────────────────────────────────────────────────


class SendMessageArgs(BaseModel):
    """`send_message` 的参数。``action`` 决定其余字段哪些有意义。"""

    action: Literal["list", "send"] = Field(
        default="list",
        description="list=看有哪些投递目标（连同每个平台为什么不可用）、send=真的发一条",
    )
    target: str = Field(
        default="",
        description="send 必填。写法：`platform` / `platform:channel` / "
        "`peer:<对端名>` / `peer:<对端名>/<agent>`（platform 见 action=list 的结果）",
    )
    text: str = Field(default="", description="send：要发出去的正文")
    attempts: int = Field(default=1, description="send：失败重试次数（1 = 不重试）")


class MemoryProviderArgs(BaseModel):
    """`memory_provider_list` 的参数。"""

    available_only: bool = Field(default=False, description="只列此刻真的可用的 provider")


class SkillSearchArgs(BaseModel):
    """`skill_search` 的参数。"""

    query: str = Field(
        default="",
        description="关键词（匹配技能名 / 描述 / hub 里的相对路径，大小写不敏感）；留空 = 列全部",
    )
    limit: int = Field(default=20, description="最多返回几条")


class EgressStatusArgs(BaseModel):
    """`egress_status` 的参数。"""

    url: str = Field(
        default="",
        description="可选：给一个目标 URL/主机名，就按**静态规则**判一次能不能出站"
        "（本机不出网，所以不做 DNS 解析与 IP 校验，返回值里会写明这一点）",
    )


class BudgetStatusArgs(BaseModel):
    """`budget_status` 的参数。"""

    action: Literal["show", "cleanup"] = Field(
        default="show", description="show=看阈值与落盘占用、cleanup=清掉过期的落盘文件"
    )
    max_age_hours: int = Field(default=24, description="cleanup：只删超过这么多小时的文件")


# ── 工具声明 ────────────────────────────────────────────────────────────────
#
# 描述照仓库写法：先说是什么，再说什么时候用，最后说和谁分工（模型就是靠这三句
# 决定调不调，缺了第三句就会和邻居工具互相抢活）。

ROUND3_TOOLS: list[Tool] = [
    Tool(
        "send_message",
        "往已接入的消息平台发一条文本（`action=list` 先看有哪些目标）。"
        "target 写成 `platform` / `platform:channel`（platform 见 list 的结果），"
        "或 `peer:<对端名>[/<agent>]` 发给另一台 Orbit 上的 agent。"
        "**本机离线可用的只有 console 与 file 两条旁路**，其余 19 家要凭证与网络，"
        "list 会把每个平台缺什么如实列出来。分工：把文本变成语音用 text_to_speech，"
        "定时发用 cron_job（它的 deliver 参数）。",
        SendMessageArgs,
    ),
    Tool(
        "memory_provider_list",
        "列出**长期记忆的后端**（provider）与它们此刻能不能用：15 家里 4 个是真实现"
        "（local / file / jsonl / holographic），其余只有接口，会逐条给出缺哪个依赖、"
        "怎么装、为什么不可用。排查「我存的东西为什么搜不到」时先看它——"
        "分工：真正读记忆用 memory_search，写用 memory_save，它只回答「谁在生效」。",
        MemoryProviderArgs,
    ),
    Tool(
        "skill_search",
        "在**本地技能 hub**（`<data>/skills-hub/`）里按关键词搜可安装的技能包，"
        "返回名字、相对路径、描述、以及是否已安装。没有网络也能用——"
        "hub 是一个本地目录（模拟远端索引），远程注册表（github / clawhub）本机不可用。"
        "分工：看已经装上的技能用 skill_list，读某个技能正文用 skill_load。",
        SkillSearchArgs,
    ),
    Tool(
        "egress_status",
        "查**当前出站策略**：allow / deny 规则、默认动作、凭证注入规则（只给键名，"
        "永远不回密钥值），以及这层「有什么、没有什么」。给 url 就按静态规则判一次"
        "能不能出站。**它不做 DNS 解析**（本机不出网），所以 IP 校验那一步会如实标成"
        "「未做」。要真的发请求用 web_extract / run_command。",
        EgressStatusArgs,
    ),
    Tool(
        "budget_status",
        "查**工具结果的预算与落盘**：上下文窗口推出单条 / 整轮字符阈值、超过阈值的"
        "结果被落盘到哪、目录里现在堆了多少。模型看到自己的输出被截断时用它确认"
        "「是预算截的，还是工具只返回了这些」。`action=cleanup` 清掉过期落盘文件。"
        "分工：查上下文占用用 context_usage，压缩用 compact。",
        BudgetStatusArgs,
    ),
]


# ── 执行上下文 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Round3Context:
    """ROUND3_TOOLS 执行时能拿到的东西。**全部由装配层注入**（与另外两批同一约定）。

    ``peer_transport`` 是 :class:`app.peer_client.PeerClient` 的 httpx transport 注入点：
    本机没有网络，对端投递这条链只有注入 ``httpx.MockTransport`` 才验得了。生产留空
    = 真的连 peers.json 里登记的那个地址。
    """

    settings: Any = None
    store: Any = None
    data_dir: Path | None = None
    peer_transport: Any = None


def _require_data_dir(context: Round3Context) -> Path:
    """取 data_dir；拿不到就抛**能行动**的错（不猜 `Path.cwd()`）。"""
    if context.data_dir is None:
        raise RuntimeError(
            "这个工具需要装配层注入 `data_dir=`（用 register_round3_tools(registry) 接线时"
            "会自动从 registry.settings 取）。"
        )
    return context.data_dir


# ── 1. send_message ─────────────────────────────────────────────────────────


def _adapter_rows(service: Any) -> list[dict[str, Any]]:
    """逐个适配器问一句"你能用吗、缺什么"。**只读配置，不发请求**。

    ``health()`` 在 WebhookAdapter 里是纯配置判断（有没有 URL / 缺哪些环境变量），
    所以这一步不会碰到网络——这正是"离线也要能如实回答"的前提。
    """
    rows: list[dict[str, Any]] = []
    for name in sorted(service.adapters):
        adapter = service.adapters[name]
        try:
            report = adapter.health()
            ok = bool(report.ok)
            detail = str(report.detail)
            extra = report.extra if isinstance(report.extra, dict) else {}
            configured = bool(extra.get("configured", True))
        except Exception as exc:  # noqa: BLE001 —— 一个适配器自检炸了不该让清单整片空
            ok, detail, configured, extra = False, f"{type(exc).__name__}: {exc}", False, {}
        rows.append(
            {
                "platform": name,
                "usable": ok,
                "configured": configured,
                "detail": detail,
                # 缺哪个环境变量要**点名**：只说"未配置"等于让人自己去翻源码。
                "env": [str(item) for item in (extra.get("env") or [])],
                "missing_env": [str(item) for item in (extra.get("missing_env") or [])],
            }
        )
    return rows


def list_send_targets(data_dir: Path) -> dict[str, Any]:
    """`send_message(action=list)`：列出**所有**能投递的地方，含不可用的原因。

    为什么把不可用的也列出来：只列可用的，模型会以为"没有 telegram 这个平台"，
    于是改用 run_command 去 curl——那既绕过了策略，也拿不到投递账本。
    """
    from app import gateway, platforms

    service = gateway.GatewayService(data_dir)
    directory = service.directory.load()
    rows = _adapter_rows(service)
    known = {str(item.value) for item in platforms.PLATFORMS}
    pending = sorted(known - set(service.adapters))
    peers = {
        name: {"url": str(record.get("url") or ""), "key": gateway.PeerRegistry.mask(str(record.get("key") or ""))}
        for name, record in service.peers.load().items()
    }
    return {
        "count": len(rows),
        "targets": rows,
        "pending_platforms": pending,
        "channels": directory["platforms"],
        "peers": peers,
        "notice": (
            "本机离线可验证的只有 console（打到 stdout）与 file（追加 <data>/outbox.jsonl）；"
            "其余平台要么缺凭证要么要网络，list 里逐条写了原因。"
            "`pending_platforms` 是本轮**没有适配器**的平台（发送会明确报未实现，不假装成功）。"
        ),
    }


def send_via_platform(
    data_dir: Path, target: str, text: str, *, attempts: int = 1
) -> dict[str, Any]:
    """走 `gateway.deliver`：适配器发送 + 重试 + 记账（`deliveries.jsonl` + 平面事件）。"""
    from app import gateway, platforms

    parsed = platforms.parse_target(target)
    service = gateway.GatewayService(data_dir)
    result = service.deliver(
        text, platform=parsed.platform, channel=parsed.channel, attempts=max(1, int(attempts))
    )
    if not result.ok:
        hint = (
            "（这个平台本机没有适配器：先 action=list 看可用目标）"
            if result.not_implemented
            else "（先 action=list 看它缺哪个凭证/环境变量）"
        )
        raise ValueError(f"投递到 {parsed.raw} 失败：{result.error}{hint}")
    payload = result.as_dict()
    payload["ok"] = True
    if parsed.platform == "file":
        payload["outbox"] = str(data_dir / "outbox.jsonl")
    payload["notice"] = (
        "已投递。每一次尝试都记在 <data>/gateway/deliveries.jsonl，"
        "`orbit send --list` 能看到频道目录。"
    )
    return payload


def send_via_peer(
    data_dir: Path, target: str, text: str, *, transport: Any = None
) -> dict[str, Any]:
    """走 `PeerClient.dm`：把一句话发给**另一台 Orbit 上的 agent**（Hermes 的 message_agent）。

    为什么要单独一条路而不是塞进平台适配器：对端是 Orbit 自己（`/peer/dm` 同步返回
    对端回复正文），与"往某个平台的频道投一条文本"是两种协议——混在一起之后
    "对端回了我什么"这件事就没地方放了。
    """
    from app import gateway
    from app.peer_client import PeerClient, PeerError

    rest = target.split(":", 1)[1] if ":" in target else ""
    peer_name, _, agent = rest.partition("/")
    peer_name = peer_name.strip()
    if not peer_name:
        raise ValueError(
            "peer 目标写法是 `peer:<对端名>` 或 `peer:<对端名>/<agent>`；"
            "对端要先用 `orbit peer add` 登记进 <data>/gateway/peers.json。"
        )
    record = gateway.PeerRegistry(gateway.GatewayPaths(data_dir).peers).load().get(peer_name)
    if record is None:
        known = "、".join(sorted(gateway.PeerRegistry(gateway.GatewayPaths(data_dir).peers).load()))
        raise ValueError(
            f"没有登记名为 {peer_name!r} 的对端"
            + (f"（现有：{known}）" if known else "（一个都没有）")
            + "。先用 `orbit peer add <名字> <url> --key <令牌>` 登记。"
        )
    url = str(record.get("url") or "").strip()
    if not url:
        raise ValueError(f"对端 {peer_name!r} 的登记里没有 url，无法投递。")
    client = PeerClient(url, token=str(record.get("key") or ""), transport=transport)
    try:
        outcome = client.dm(agent.strip(), text)
    except PeerError as exc:  # 对端不可达 / 没有这个端点：说清是哪一步，别只说"失败"
        raise ValueError(f"发给对端 {peer_name!r} 失败：{exc}") from exc
    return {
        "ok": True,
        "peer": peer_name,
        "agent": outcome.get("agent", ""),
        "reply": outcome.get("reply", ""),
        "notice": "对端是另一台 Orbit：`reply` 是它这一轮的回复正文（同步返回）。",
    }


def run_send_message(args: Mapping[str, Any], context: Round3Context) -> dict[str, Any]:
    """`send_message`：列目标，或真的发一条（平台适配器 / 对端 agent 两条路）。"""
    data_dir = _require_data_dir(context)
    action = str(args.get("action") or "list").strip().lower()
    if action == "list":
        return list_send_targets(data_dir)
    if action != "send":
        raise ValueError(f"未知 action：{action}（可选 list / send）")
    text = str(args.get("text") or "")
    if not text.strip():
        raise ValueError("send 需要 text：要发出去的正文是空的。")
    target = str(args.get("target") or "").strip()
    if not target:
        raise ValueError(
            "send 需要 target：写法是 `platform` / `platform:channel` / `peer:<对端名>`；"
            "不确定有哪些目标就先 action=list。"
        )
    if target.lower().startswith("peer:"):
        return send_via_peer(data_dir, target, text, transport=context.peer_transport)
    return send_via_platform(data_dir, target, text, attempts=int(args.get("attempts") or 1))


# ── 2. memory_provider_list ─────────────────────────────────────────────────


def run_memory_provider_list(args: Mapping[str, Any], context: Round3Context) -> dict[str, Any]:
    """`memory_provider_list`：15 个记忆后端各自能不能用、缺什么。

    为什么复用 `build_router` 而不是自己遍历 `PROVIDER_NAMES`：路由器的
    `status()` 里带着**优先级排序**与 `active`（真正在生效的那一个）——"我配了
    file 但它没生效"这个问题只有它答得了。
    """
    from app import memory_providers

    data_dir = context.data_dir
    router = memory_providers.build_router(store=context.store, data_dir=data_dir)
    status = router.status()
    rows = list(status.get("providers") or [])
    if bool(args.get("available_only")):
        rows = [row for row in rows if row.get("available")]
    config = memory_providers.config_path(data_dir)
    return {
        "active": status.get("active", ""),
        "fanout": status.get("fanout", True),
        "priority": status.get("priority", []),
        "available": status.get("available", []),
        "count": len(rows),
        "providers": rows,
        "config_path": str(config) if config is not None else "",
        "notice": (
            "`available=false` 的 provider 只有接口：读写会抛 ProviderUnavailable（带安装提示），"
            "路由器不会选它们。优先级与是否多写写在上面那个 config_path 里。"
        ),
    }


# ── 3. skill_search ────────────────────────────────────────────────────────


def run_skill_search(args: Mapping[str, Any], context: Round3Context) -> dict[str, Any]:
    """`skill_search`：在本地技能 hub 里按关键词找可安装的技能包。"""
    from app import skill_hub

    data_dir = _require_data_dir(context)
    hub = skill_hub.SkillHub(data_dir)
    query = str(args.get("query") or "").strip()
    limit = max(1, int(args.get("limit") or 20))
    entries = hub.search(query, limit=limit)
    return {
        "query": query,
        "count": len(entries),
        "results": entries,
        "hub_root": str(hub.hub_root),
        "notice": (
            f"搜的是本地 hub 目录（{hub.hub_root}），没有网络也能用；"
            "远程注册表（github / clawhub）本机不可用。装某个包走 `orbit skills install`。"
        ),
    }


# ── 4. egress_status ───────────────────────────────────────────────────────


def run_egress_status(args: Mapping[str, Any], context: Round3Context) -> dict[str, Any]:
    """`egress_status`：当前出站策略 + （可选）按静态规则判一个目标。

    ``resolve=lambda host: []`` 是**故意**的：本机不出网，真查 DNS 既会卡住也会让
    结论依赖当时能不能解析。空解析结果在 `_guard_resolved_ip` 里会落成
    "域名未解析成功，未做 IP 校验"——这句话必须原样到模型手里，不能改写成"已校验通过"。
    """
    from app import egress

    data_dir = _require_data_dir(context)
    policy = egress.EgressPolicy.load(data_dir)
    payload: dict[str, Any] = {
        "policy": policy.as_dict(),
        "summary": policy.describe(),
        "notice": (
            "这份策略只有 4 层（静态规则 / 重定向链 / 请求头注入 / DNS rebinding 防护），"
            "**没有代理进程**：真密钥仍由调用方持有，绕过 Orbit 代码的请求不受管。"
        ),
    }
    url = str(args.get("url") or "").strip()
    if url:
        decision = policy.check(url, resolve=lambda _host: [])
        payload["decision"] = decision.as_dict()
        payload["decision_note"] = (
            "只做静态规则判定：本机不出网，没有做 DNS 解析，所以 IP 校验那一步未做。"
            "要看真结果请在能出网的机器上跑 `orbit egress check <url>`。"
        )
    return payload


# ── 5. budget_status ───────────────────────────────────────────────────────


def run_budget_status(args: Mapping[str, Any], context: Round3Context) -> dict[str, Any]:
    """`budget_status`：窗口 → 阈值 → 落盘占用的那条账。

    为什么要把三样放在一份返回里：模型看到"输出被截断了"时无法区分**预算截的**
    还是**工具只返回了这些**。三样一起给，它就能自己判断该不该换个读法
    （例如改用 `local_read_file` 分页）。
    """
    from app import budget, turn_budgets, window

    data_dir = _require_data_dir(context)
    turns = turn_budgets.resolve(context.settings)
    limits = window.thresholds_for(context.settings)
    config = budget.BudgetConfig(**limits.budget_kwargs())
    spill_dir = budget.spillover_dir(data_dir)
    files = 0
    total_bytes = 0
    if spill_dir.is_dir():
        for entry in spill_dir.iterdir():
            try:
                if entry.is_file():
                    files += 1
                    total_bytes += entry.stat().st_size
            except OSError:
                continue
    payload: dict[str, Any] = {
        "window": limits.to_dict(),
        "turn_budgets": turns.to_dict(),
        "thresholds": {
            "default_result_size": config.default_result_size,
            "turn_budget": config.turn_budget,
            "preview_size": config.preview_size,
            "mcp_result_size": config.mcp_result_size,
            "max_bytes": config.max_bytes,
            "max_lines": config.max_lines,
            "max_line_length": config.max_line_length,
            "pinned_never_spilled": sorted(budget.PINNED_THRESHOLDS),
        },
        "spillover": {
            "dir": str(spill_dir),
            "files": files,
            "bytes": total_bytes,
            "max_age_hours": budget.SPILLOVER_MAX_AGE_HOURS,
        },
        "notice": (
            "单条结果超过 `default_result_size` 字符就落盘（只留头尾预览），"
            "整轮合计超过 `turn_budget` 字符就按大小降序继续落盘。"
            "`read_file` 永不落盘（否则「落盘 → 读文件 → 又落盘」会死循环）。"
        ),
    }
    action = str(args.get("action") or "show").strip().lower()
    if action == "cleanup":
        age = int(args.get("max_age_hours") or budget.SPILLOVER_MAX_AGE_HOURS)
        removed = budget.cleanup_spillover(data_dir, max_age_hours=age)
        payload["cleanup"] = {"max_age_hours": age, "removed": removed}
        payload["notice"] = f"已清理 {removed} 个超过 {age} 小时的落盘文件。" + payload["notice"]
    elif action != "show":
        raise ValueError(f"未知 action：{action}（可选 show / cleanup）")
    return payload


# ── 分发 ────────────────────────────────────────────────────────────────────


def execute_round3(name: str, args: Mapping[str, Any], context: Round3Context) -> dict[str, Any]:
    """ROUND3_TOOLS 的唯一分发点。返回 dict；**异常不吞**（由注册表包成 error 载荷）。"""
    if name == "send_message":
        return run_send_message(args, context)
    if name == "memory_provider_list":
        return run_memory_provider_list(args, context)
    if name == "skill_search":
        return run_skill_search(args, context)
    if name == "egress_status":
        return run_egress_status(args, context)
    if name == "budget_status":
        return run_budget_status(args, context)
    raise ValueError(
        f"未知的扩展工具：{name}（可用：{'、'.join(tool.name for tool in ROUND3_TOOLS)}）"
    )


def register_round3_tools(
    registry: Any,
    *,
    settings: Any = None,
    store: Any = None,
    data_dir: str | Path | None = None,
    peer_transport: Any = None,
) -> list[str]:
    """把 :data:`ROUND3_TOOLS` 注册进一个 `ToolRegistry`，返回**真的注册成功**的名字。

    ``settings`` / ``store`` 留空时从注册表自己身上取（`ToolRegistry` 两者都有），
    所以装配层的调用是**一行**：``register_round3_tools(registry)``。

    返回名字而不是 None：注册表的遮蔽门禁会**静默跳过**跨 source 的同名注册，
    不把"到底注册上了没有"返回给调用方，"接了一行但工具没出现"就查不出来。
    """
    resolved_settings = settings if settings is not None else getattr(registry, "settings", None)
    resolved_store = store if store is not None else getattr(registry, "store", None)
    root = data_dir if data_dir is not None else getattr(resolved_settings, "data_dir", None)
    context = Round3Context(
        settings=resolved_settings,
        store=resolved_store,
        data_dir=Path(root) if root is not None else None,
        peer_transport=peer_transport,
    )

    async def executor(
        name: str, args: Mapping[str, Any], task_id: str, operation_id: str
    ) -> dict[str, Any]:
        """外部执行器必须**自带 `status`**：`ToolRegistry.execute` 直接读它。"""
        return {"status": "ok", "data": execute_round3(name, args, context)}

    registry.register(ROUND3_TOOLS, executor, source="builtin-round3")
    return [tool.name for tool in ROUND3_TOOLS if tool.name in getattr(registry, "tools", {})]


__all__ = [
    "PENDING_NEW_TOOLSETS",
    "PENDING_TOOLSET_MAPPINGS",
    "ROUND3_TOOLS",
    "BudgetStatusArgs",
    "EgressStatusArgs",
    "MemoryProviderArgs",
    "Round3Context",
    "SendMessageArgs",
    "SkillSearchArgs",
    "execute_round3",
    "list_send_targets",
    "register_round3_tools",
    "run_budget_status",
    "run_egress_status",
    "run_memory_provider_list",
    "run_send_message",
    "run_skill_search",
    "send_via_peer",
    "send_via_platform",
]
