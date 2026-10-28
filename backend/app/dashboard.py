"""Dashboard 页面语义登记（MATRIX 16.1）+ 会话回放（MATRIX 16.6）。

## 这个模块**不实现页面**

Hermes 的 dashboard 是一套 React 面板，路由表在
``hermes_cli/web_dist/assets/index-*.js`` 里（``path:`/analytics``` … 一共 18 条
顶层路由；``/profiles/:id/build`` 是子页，不计入）。Orbit 没有那套前端
（``frontend/src`` 只有 6 个视图：工作台 / 对话摘要 / 长期记忆 / 技能 / 密钥 / 设置）。

所以本模块只做三件事，**不假装有页面**：

1. 把 18 个页面的语义钉成数据：这页解决什么问题、需要哪些数据；
2. 逐页去 ``backend/app/main.py`` 里核对**真实存在**的 API 路由，给出覆盖状态；
3. 提供会话回放（消息 + 任务事件按时间合并），给 CLI 与前端共用。

## 覆盖状态的判定口径（改判定前先读这段）

- ``covered``：Orbit **有前端页面或 API 路由直接服务该页的核心目的**；
- ``partial``：能力/数据源存在（模块、CLI、单条路由），但缺页面，或缺 API 面的关键部分；
- ``missing``：连数据源都没有，``gap`` 必须写清缺什么。

**不许把没数据源的页面标 covered。** 这条不靠自觉：
:func:`unbacked_routes` 会把每个页面 ``orbit_route`` 里声明的 ``/api/...`` 拿去
``app/main.py`` 里逐条比对，对不上就返回出来；:func:`report` 的 ``unbacked`` 字段
必须是空字典，``tests/test_dashboard.py`` 直接断言这一点。

## 为什么不 import ``app.main`` 而读文件

``import app.main`` 会把 FastAPI、浏览器控制器、Agent 图全拉起来（本机无网络，
且导入即建进程级副作用）。这里只需要"路由表"这一条事实，读文件更便宜，也更诚实
——**文件里没有的路由，运行时也不会有**。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: 覆盖状态取值。顺序即 CLI 表里的展示顺序（先报好的，再报缺的）。
STATUSES: tuple[str, ...] = ("covered", "partial", "missing")

#: 状态的中文标签，CLI 与前端共用一套措辞。
STATUS_LABELS: dict[str, str] = {
    "covered": "已覆盖",
    "partial": "部分覆盖",
    "missing": "缺失",
}

#: ``app/main.py`` 的路径。用 ``__file__`` 定位而不是 ``cwd``：命令可能在任意目录下跑。
MAIN_PATH: Path = Path(__file__).with_name("main.py")

#: 抽 FastAPI 路由声明的正则。只认 ``@app.<method>("<path>")`` 这一种写法——
#: ``app/main.py`` 里全部路由都是这个形状，放宽只会让"没找到"变成"猜"。
_ROUTE_RE = re.compile(r"""@app\.(get|post|put|patch|delete|websocket)\(\s*["']([^"']+)["']""")

#: 从 ``orbit_route`` 里抽 ``/api/...`` 路由的字面量。
_ROUTE_TOKEN_RE = re.compile(r"/api/[A-Za-z0-9_/{}\-]+")

#: 回放里单帧正文的最大长度。回放是"看发生了什么"，不是"导出全文"；
#: 真要看全文去 ``/api/conversations/{id}/messages`` 或前端。
MAX_FRAME_TEXT = 500


@dataclass(frozen=True)
class DashboardPage:
    """Hermes dashboard 的一个页面，以及它在 Orbit 里的落地情况。"""

    #: 页面名，与 Hermes 的路由同名（``/analytics`` → ``analytics``）。
    name: str
    #: 中文标题。
    title: str
    #: 一句话说明这页解决什么问题。
    purpose: str
    #: 这页需要的数据：``app.sqlite`` 的表、落盘文件、或（Hermes 侧的）API 路由。
    data_sources: tuple[str, ...]
    #: ``covered`` / ``partial`` / ``missing``，口径见模块 docstring。
    orbit_status: str
    #: 对应 Orbit 的 API 路由或前端文件路径。``covered`` / ``partial`` 必填。
    orbit_route: str = ""
    #: ``missing`` 时说明缺什么（要有可行动的信息，不能只写"没有"）。
    gap: str = ""
    #: 额外的口径说明，主要用来点出"同名但语义不同"的坑。
    note: str = ""

    def __post_init__(self) -> None:
        if self.orbit_status not in STATUSES:
            raise ValueError(f"{self.name}: 未知覆盖状态 {self.orbit_status!r}")
        if not self.purpose.strip():
            raise ValueError(f"{self.name}: 缺 purpose——页面语义不能空着")
        if not self.data_sources:
            raise ValueError(f"{self.name}: 缺 data_sources——判不了覆盖就说明没查清楚")
        if self.orbit_status == "missing":
            if not self.gap.strip():
                raise ValueError(f"{self.name}: 标了 missing 就必须写 gap")
        elif not self.orbit_route.strip():
            raise ValueError(f"{self.name}: {self.orbit_status} 必须给 orbit_route")

    def as_dict(self) -> dict[str, Any]:
        """结构化输出（``--json`` 与前端共用）。"""
        return {
            "name": self.name,
            "title": self.title,
            "purpose": self.purpose,
            "data_sources": list(self.data_sources),
            "orbit_status": self.orbit_status,
            "orbit_status_label": STATUS_LABELS[self.orbit_status],
            "orbit_route": self.orbit_route,
            "gap": self.gap,
            "note": self.note,
        }


# ── 18 个页面 ───────────────────────────────────────────────────────────────
#
# 逐页的来源：Hermes 路由表（web_dist 的 path: 定义）+ 每页实际调用的
# ``/api/...``（web_dist/assets/api-*.js）。Orbit 侧则逐条回 ``app/main.py`` 核对。
# 顺序照 Hermes 的路由顺序，便于两边对照。

PAGES: tuple[DashboardPage, ...] = (
    DashboardPage(
        name="analytics",
        title="用量与成本分析",
        purpose="回答「这几天花了多少 token / 多少钱、哪个模型最贵」，把用量从会话里汇总成趋势。",
        data_sources=(
            "app.sqlite · tasks",
            "app.sqlite · messages",
            "Hermes 路由 /api/analytics、/api/analytics/usage、/api/analytics/models",
        ),
        orbit_status="partial",
        orbit_route="app/metrics.py（collect() 已汇总任务数/token/耗时）+ CLI `orbit monitoring metrics`",
        note="采集与 CLI 都在，但没有 /api/analytics 路由，前端也没有分析页。",
    ),
    DashboardPage(
        name="channels",
        title="消息渠道",
        purpose="管理 Telegram / WhatsApp / 飞书等消息平台的接入与登录态，让 agent 能在这些平台上收发。",
        data_sources=(
            "data/connectors.json（平台凭据）",
            "Hermes 路由 /api/messaging/platforms*（含 onboarding / test）",
        ),
        orbit_status="partial",
        orbit_route="app/platforms.py（22 平台适配器契约）+ app/gateway.py（投递账本）",
        note="适配器与网关都有，但没有面板页、也没有 /api/messaging 路由。",
    ),
    DashboardPage(
        name="chat",
        title="对话",
        purpose="主对话界面：发消息、看流式回复、审批工具调用。",
        data_sources=(
            "app.sqlite · conversations",
            "app.sqlite · messages",
            "app.sqlite · tasks",
            "app.sqlite · task_events（SSE 事件流）",
        ),
        orbit_status="covered",
        orbit_route="/api/conversations/{conversation_id}/messages + frontend/src/App.tsx",
    ),
    DashboardPage(
        name="config",
        title="运行配置",
        purpose="改运行配置（模型、路径、开关），并看清默认值与来源。",
        data_sources=(
            "data/settings.json",
            "环境变量 / .env",
            "Hermes 路由 /api/config、/api/config/raw、/api/config/schema、/api/config/defaults",
        ),
        orbit_status="partial",
        orbit_route="/api/connectors + frontend/src/Settings.tsx",
        note="只覆盖「连接与模型」这一块；没有 schema 驱动的全量配置编辑，也没有"
        "「这一项来自环境变量还是 settings.json」的来源标注。",
    ),
    DashboardPage(
        name="cron",
        title="定时任务",
        purpose="管定时任务：什么时候跑、跑成什么样、失败了怎么办。",
        data_sources=(
            "data/cron/jobs.json（任务定义）",
            "data/cron/runs.jsonl（执行流水）",
            "Hermes 路由 /api/cron/jobs*、/api/cron/blueprints、/api/cron/delivery-targets",
        ),
        orbit_status="partial",
        orbit_route="app/cron.py + CLI `orbit cron list/runs/incidents`",
        note="调度与失败治理齐全，但没有面板页与 /api/cron 路由。",
    ),
    DashboardPage(
        name="docs",
        title="面板内文档",
        purpose="在面板里读文档/帮助，不用切出去找 README。",
        data_sources=(
            "内置静态文档页（Hermes 侧无 /api 路由，纯前端）",
            "仓库根目录的 .md（README / ARCHITECTURE / ROADMAP …）",
        ),
        orbit_status="missing",
        gap="Orbit 没有面板内文档页，前端也没有对应视图；文档散在仓库根目录的 .md 里。",
    ),
    DashboardPage(
        name="env",
        title="环境变量与密钥",
        purpose="查看和改环境变量/密钥（哪些已配、哪些是空），同时不把密钥打到屏幕上。",
        data_sources=(
            "环境变量 / .env",
            "系统 keyring（app/secrets.py 的落点）",
            "Hermes 路由 /api/env、/api/env/reveal",
        ),
        orbit_status="partial",
        orbit_route="/api/secrets + frontend/src/Vault.tsx",
        note="Orbit 走 keyring 加密库而不是 .env 明文，能列/写/删密钥；"
        "但没有「全量环境变量只读视图」。",
    ),
    DashboardPage(
        name="files",
        title="文件浏览",
        purpose="浏览工作目录、读文件、上传/下载，看看 agent 到底动了哪些文件。",
        data_sources=(
            "workspace 文件系统",
            "app.sqlite · artifacts",
            "Hermes 路由 /api/files*（list / read / mkdir / upload）",
        ),
        orbit_status="missing",
        gap="只有 /api/artifacts/{artifact_id} 单件下载，没有目录浏览/读写/上传路由，"
        "前端也没有文件页——agent 产出的文件只能靠「知道 artifact id」去取。",
    ),
    DashboardPage(
        name="logs",
        title="日志",
        purpose="直接看后端日志，出问题不用先去翻文件路径。",
        data_sources=(
            "data/*.log（app/logging_setup.py 落盘，按大小轮转）",
            "Hermes 路由 /api/logs",
        ),
        orbit_status="partial",
        orbit_route="app/logging_setup.py + CLI `orbit logs`",
        note="日志真的落盘了、CLI 能读；没有 /api/logs 路由与面板页。",
    ),
    DashboardPage(
        name="mcp",
        title="MCP 服务",
        purpose="管 MCP 服务：装了哪些、连没连上、每个服务暴露了哪些工具。",
        data_sources=(
            "data/mcp.json（服务定义）",
            "系统 keyring（MCP secret）",
            "app.sqlite · chunks（MCP 资源入 RAG 的落点）",
        ),
        orbit_status="covered",
        orbit_route="/api/mcp + frontend/src/Settings.tsx",
        note="列表 / 保存 / 重载 / 写密钥都有；没有 Hermes 的 MCP 目录（catalog）一键安装。",
    ),
    DashboardPage(
        name="models",
        title="模型与供应商",
        purpose="选模型与供应商、看当前生效的是哪个、换之前先测一下连通性。",
        data_sources=(
            "data/settings.json（model_* 字段）",
            "系统 keyring（model_api_key）",
            "Hermes 路由 /api/model/options、/api/model/set、/api/model/info",
        ),
        orbit_status="partial",
        orbit_route="/api/connectors + /api/connectors/model/check",
        note="只有「一套模型配置 + 连通性检查」，没有模型目录，也没有多模型切换。",
    ),
    DashboardPage(
        name="pairing",
        title="设备配对",
        purpose="配对设备/客户端：批准新设备接入、吊销已批准的。",
        data_sources=(
            "data/pairing.json（待批 / 已批清单）",
            "Hermes 路由 /api/pairing、/api/pairing/approve、/api/pairing/revoke",
        ),
        orbit_status="partial",
        orbit_route="CLI `orbit pairing list/approve/revoke/clear-pending`",
        note="配对逻辑在 CLI 里，没有 /api/pairing 路由与面板页。",
    ),
    DashboardPage(
        name="plugins",
        title="插件",
        purpose="看装了哪些插件、各自贡献了什么工具/钩子，并启停。",
        data_sources=(
            "plugins/<name>/plugin.yaml（manifest）",
            "Hermes 路由 /api/dashboard/plugins*、/api/dashboard/agent-plugins*",
        ),
        orbit_status="partial",
        orbit_route="app/plugins.py + CLI `orbit plugins list/show/enable/disable`",
        note="发现 / 加载 / 启停 / 能力清单都有；没有面板页与 /api/dashboard/plugins 路由。",
    ),
    DashboardPage(
        name="profiles",
        title="多套 profile",
        purpose="在多套 profile（人格 + 模型 + 工作目录）之间切换，不同项目用不同 agent。",
        data_sources=(
            "data/profile/USER.md（Orbit 侧只有用户画像正文）",
            "app.sqlite · profiles",
            "Hermes 路由 /api/profiles*、/api/profiles/active",
        ),
        orbit_status="partial",
        orbit_route="/api/profile + frontend/src/Memories.tsx",
        note="**同名不同义**：Orbit 的 profile 是「用户画像」（这个人是谁、怎么协作），"
        "Hermes 的 profiles 是「多套 agent 配置」（人格/模型/目录）。多 profile 切换在 "
        "Orbit 不存在，别被同名骗过去。",
    ),
    DashboardPage(
        name="sessions",
        title="会话",
        purpose="会话列表与检索：翻历史、找那次聊了什么、导出/清理。",
        data_sources=(
            "app.sqlite · conversations",
            "app.sqlite · messages",
            "app.sqlite · chunks（检索索引）",
        ),
        orbit_status="covered",
        orbit_route="/api/conversations + frontend/src/Summaries.tsx",
        note="列表/新建/摘要都有；Hermes 的批量删除与 prune 只在 CLI（`orbit sessions`）。",
    ),
    DashboardPage(
        name="skills",
        title="技能",
        purpose="管技能（流程知识）：有哪些、何时触发、手动改正文、停用。",
        data_sources=(
            "data/skills/<name>/SKILL.md（正文真值）",
            "app.sqlite · skills（索引与统计）",
        ),
        orbit_status="covered",
        orbit_route="/api/skills + frontend/src/Skills.tsx",
        note="读/改/删/改状态都有；Hermes 的技能市场（hub）在 Orbit 没有。",
    ),
    DashboardPage(
        name="system",
        title="系统与运维",
        purpose="系统总览与运维动作：版本、进程、备份、体检、更新。",
        data_sources=(
            "data/backups/*.zip",
            "checkpoints.sqlite",
            "Hermes 路由 /api/system/stats、/api/ops/*、/api/gateway、/api/hermes/update",
        ),
        orbit_status="partial",
        orbit_route="/api/health",
        note="只有一个只读探针 /api/health；备份/体检/更新全在 CLI"
        "（`orbit doctor` / `orbit backup` / `orbit monitoring status`）。",
    ),
    DashboardPage(
        name="webhooks",
        title="Webhook 入站",
        purpose="管入站 webhook：谁可以打进来、打到哪个会话、怎么鉴权。",
        data_sources=(
            "Hermes 路由 /api/webhooks*（enable / 单个开关）",
            "Hermes data/webhooks.json",
        ),
        orbit_status="missing",
        gap="Orbit 没有 webhook 入口（无路由、无落盘配置）；外部触发只能靠消息平台或 cron。",
    ),
)


# ── 路由核对（诚实性的机械保证）──────────────────────────────────────────────


def main_routes(path: Path | None = None) -> dict[str, str]:
    """读 ``app/main.py``，返回 ``{path: method}`` 形式的**真实**路由表。

    只解析 ``@app.<method>("<path>")`` 装饰器——这是 Orbit 里唯一的声明形状。
    文件不存在时返回空字典（而不是抛）：那样"路由核对"会把全部 ``/api/...``
    判成对不上，比静默放行安全。
    """
    target = path or MAIN_PATH
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return {}
    routes: dict[str, str] = {}
    for method, route in _ROUTE_RE.findall(text):
        routes.setdefault(route, method.upper())
    return routes


def unbacked_routes(page: DashboardPage, routes: dict[str, str] | None = None) -> list[str]:
    """``orbit_route`` 里声明了、但 ``app/main.py`` 里**找不到**的 ``/api/...`` 路由。

    只核对 ``orbit_route``，不核对 ``data_sources``：后者刻意保留了 Hermes 侧的路由
    （前缀写「Hermes 路由」），用来对照"这页需要什么"，不是 Orbit 的承诺。
    """
    known = main_routes() if routes is None else routes
    claimed = _ROUTE_TOKEN_RE.findall(page.orbit_route)
    return [route for route in claimed if route not in known]


def report() -> dict[str, Any]:
    """汇总覆盖情况：三类计数 + 每页明细 + 对不上的路由（应为空）。"""
    counts = {status: 0 for status in STATUSES}
    routes = main_routes()
    unbacked: dict[str, list[str]] = {}
    for page in PAGES:
        counts[page.orbit_status] += 1
        missing_routes = unbacked_routes(page, routes)
        if missing_routes:
            unbacked[page.name] = missing_routes
    return {
        "total": len(PAGES),
        "covered": counts["covered"],
        "partial": counts["partial"],
        "missing": counts["missing"],
        "pages": [page.as_dict() for page in PAGES],
        "main_routes": len(routes),
        "unbacked": unbacked,
    }


def page_by_name(name: str) -> DashboardPage | None:
    """按页面名取一条登记（CLI 与测试共用）。"""
    wanted = (name or "").strip().lower()
    for page in PAGES:
        if page.name == wanted:
            return page
    return None


# ── 会话回放（MATRIX 16.6）──────────────────────────────────────────────────


@dataclass(frozen=True)
class ReplayFrame:
    """回放里的一帧：一条消息，或一条任务事件。"""

    #: 从 1 开始的序号（按合并后的时间序）。
    seq: int
    #: 原始时间戳（``created_at``，UTC ISO 字符串）。
    at: str
    #: 相对**本段首帧**的毫秒偏移。
    offset_ms: int
    #: 按 ``speed`` 折算后应等待的毫秒（``offset_ms / speed``）。
    delay_ms: int
    #: ``message`` 或 ``event``。
    kind: str
    #: ``message.id`` / ``task_events.id``。
    source_id: str
    #: 消息的 role，或事件的 ``event_type``。
    label: str
    #: 正文（过长会截断到 :data:`MAX_FRAME_TEXT`）。
    text: str
    #: 正文被截断时为 True（``text`` 不是全文）。
    truncated: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "at": self.at,
            "offset_ms": self.offset_ms,
            "delay_ms": self.delay_ms,
            "kind": self.kind,
            "source_id": self.source_id,
            "label": self.label,
            "text": self.text,
            "truncated": self.truncated,
        }


def _clip(text: str) -> tuple[str, bool]:
    """按字符数截断（不是按字节：中文按字节切会切出半个字）。"""
    body = text or ""
    if len(body) <= MAX_FRAME_TEXT:
        return body, False
    return body[: MAX_FRAME_TEXT - 1] + "…", True


def _event_text(payload: Any) -> str:
    """把事件 payload 压成一行人能读的字（``key=value`` 用空格连）。

    事件 payload 是自由 JSON（``{"tool": "web_search", "status": "DONE"}``），
    回放不需要原样复原结构，只需要"这一步发生了什么"。
    """
    if not isinstance(payload, dict):
        return "" if payload is None else str(payload)
    parts: list[str] = []
    for key, value in payload.items():
        if isinstance(value, dict | list):
            parts.append(f"{key}=<{type(value).__name__}>")
        else:
            parts.append(f"{key}={value}")
    return " ".join(parts)


def _parse_at(value: str) -> datetime | None:
    """解析 ``created_at``。坏值返回 None——回放不该因为一条脏时间戳整体失败。"""
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def session_replay(
    store: Any,
    session_id: str,
    *,
    speed: float = 1.0,
    limit: int | None = None,
) -> dict[str, Any]:
    """把一个会话回放成一条按时间排好的帧序列。

    ## 合并口径（改这里之前先读）

    - **取哪些行**：``messages``（``conversation_id == session_id``）+
      ``task_events``（其 ``task`` 属于该会话）。Orbit 的事件挂在 task 上，
      所以要先按会话把 task id 取出来再取事件。
    - **怎么排**：按 ``created_at`` 升序。``now()`` 落的是 UTC ISO 串，字典序即时间序；
      同一时刻**消息先于事件**（先有输入/输出，才有围绕它的事件），再按原始 id 稳定排序。
      时间戳坏掉的行不丢，按原顺序垫在末尾。
    - **``limit``**：取**尾部最近 N 帧**（回放通常关心"刚才发生了什么"），
      并用 ``truncated`` 标出前面还有多少帧没回放。
    - **``offset_ms``**：相对**保留下来的首帧**计算——截尾之后还从 0 开始等，
      否则 ``--limit 5`` 会让人先干等半小时。
    - **``speed``**：``delay_ms = offset_ms / speed``。``speed <= 0`` 直接报错，
      不做"当成原速"的兜底——静默兜底会让人以为倍速生效了。

    找不到会话不抛异常，返回 ``found=False`` 的空结果：空会话/写错的 id 都该
    给一句能读的话，而不是栈。
    """
    if speed <= 0:
        raise ValueError("speed 必须大于 0（1.0 = 原速，2.0 = 两倍快进）")
    if limit is not None and limit < 1:
        raise ValueError("limit 必须 ≥ 1（要全部帧就别传 limit）")

    from sqlalchemy import select

    from app.storage import Conversation, Message, Task, TaskEvent

    conversation = await store.get(Conversation, session_id)
    if conversation is None:
        return {
            "session_id": session_id,
            "found": False,
            "title": "",
            "speed": speed,
            "limit": limit,
            "total_frames": 0,
            "returned": 0,
            "dropped": 0,
            "truncated": False,
            "started_at": "",
            "duration_ms": 0,
            "frames": [],
        }

    messages = await store.rows(
        select(Message).where(Message.conversation_id == session_id).order_by(Message.created_at)
    )
    # `Store.rows` 返回的是 `list(scalars(...).all())`，所以选 id 列拿到的就是 id 列表。
    task_ids = list(await store.rows(select(Task.id).where(Task.conversation_id == session_id)))
    events: list[Any] = []
    if task_ids:
        events = await store.rows(
            select(TaskEvent).where(TaskEvent.task_id.in_(task_ids)).order_by(TaskEvent.id)
        )

    # (排序键, 记录) —— 排序键统一是 (时间戳, kind 次序, 原始 id) 三元组，
    # 位置类型一致才不会被 Python 的跨类型比较坑到。
    records: list[tuple[tuple[str, int, str], dict[str, Any]]] = []
    for message in messages:
        body, truncated = _clip(message.content or "")
        records.append(
            (
                (message.created_at or "", 0, message.id),
                {
                    "at": message.created_at or "",
                    "kind": "message",
                    "source_id": message.id,
                    "label": message.role or "user",
                    "text": body,
                    "truncated": truncated,
                },
            )
        )
    for event in events:
        body, truncated = _clip(_event_text(event.payload))
        records.append(
            (
                (event.created_at or "", 1, f"{event.id:012d}"),
                {
                    "at": event.created_at or "",
                    "kind": "event",
                    "source_id": str(event.id),
                    "label": event.event_type or "event",
                    "text": body,
                    "truncated": truncated,
                },
            )
        )
    records.sort(key=lambda item: item[0])

    total = len(records)
    kept = records if limit is None else records[max(0, total - limit) :]
    base = _parse_at(kept[0][0][0]) if kept else None

    frames: list[ReplayFrame] = []
    previous_offset = 0
    for index, (_, payload) in enumerate(kept, start=1):
        moment = _parse_at(payload["at"])
        offset = previous_offset
        if base is not None and moment is not None:
            # 时间戳乱序时不让偏移回退：回放可以"不等"，但不能"倒流"。
            offset = max(previous_offset, int((moment - base).total_seconds() * 1000))
        previous_offset = offset
        frames.append(
            ReplayFrame(
                seq=index,
                at=payload["at"],
                offset_ms=offset,
                delay_ms=int(offset / speed),
                kind=payload["kind"],
                source_id=payload["source_id"],
                label=payload["label"],
                text=payload["text"],
                truncated=payload["truncated"],
            )
        )

    return {
        "session_id": session_id,
        "found": True,
        "title": conversation.title or "",
        "speed": speed,
        "limit": limit,
        "total_frames": total,
        "returned": len(frames),
        "dropped": total - len(frames),
        "truncated": total > len(frames),
        "started_at": frames[0].at if frames else "",
        "duration_ms": frames[-1].offset_ms if frames else 0,
        "frames": [frame.as_dict() for frame in frames],
    }
