"""工具集（toolset）过滤层：把"哪些工具能被模型看见"变成一份可审计的声明。

对标 Hermes 的 ``toolsets`` 模块（口径见 ``handoff/hermes-parity/research/02-tool-layer.md``
§1「注册表遮蔽规则」与 §3「19 功能域 / 32 toolset」）。三条设计决定，都是"为什么这么做"：

1. **工具必须显式归到某个 toolset**。Hermes 的每个工具在 ``registry.register(name=,
   toolset=)`` 时就把归属写死了，于是"禁用 web"这类操作才有意义。Orbit 原来的工具表
   只有名字和 schema，没有归属维度——想收窄暴露面只能删代码。这里用一张声明式映射
   补上这一维：**没登记的工具会落进 ``unclassified``**，而不是悄悄默认全开。
   为什么让 ``unclassified`` 是"可见的坏状态"而不是"默认放行"：暴露面失控从来不是
   某一次错误的决定，而是某个新工具没人想起来归类，于是永远躺在模型能看见的地方。

2. **per-platform 模型：默认全开，只记禁用集**。配置文件只写"关掉了什么"，
   没写的等于开着。这样新增工具不需要改任何配置就自动生效（否则每加一个工具，所有
   平台的历史配置都会把它意外关掉）。``enabled`` 列表是另一种模式——"只开这几个"，
   用于给 Telegram/Discord 这类窄场景收窄，语义照 Hermes 的 per-platform 启用集。

3. **本模块不 import ``app.tools`` / ``app.browser`` 等重模块**。它是纯数据 + 纯函数，
   于是 CLI、注册表、测试都能零成本用它，也不会踩到 playwright 之类的可选依赖。
   工具名由调用方传进来（``names`` 参数），归属判定由本模块负责。
"""

from __future__ import annotations

import copy
import importlib.util
import json
import logging
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("orbit.toolsets")

#: 默认平台。``orbit tools`` 的所有子命令不带 ``--platform`` 时都落到这里，
#: 与 Hermes 的 ``--platform PLATFORM  Platform to show (default: cli)`` 一致。
DEFAULT_PLATFORM = "cli"

#: 配置文件相对 ``data_dir`` 的文件名。放在数据目录而不是仓库根，是为了让
#: "换一个数据目录"能连工具启停一起换走（与 settings.json / approvals.json 同规矩）。
CONFIG_FILENAME = "tools.json"

#: 未归类工具落点。**故意不是一个合法 toolset**：测试拿它当门禁，命中即失败。
UNCLASSIFIED = "unclassified"

#: toolset → 一行中文说明。``orbit tools list`` 直接展示它，所以每条都要说清
#: "这组工具能让模型干什么"，而不是重复 toolset 名字。
TOOLSETS: dict[str, str] = {
    "core": "对话骨架：提问（ask_user）、选择题（clarify）、任务清单（todo）、工具检索",
    "files": "本地文件读写与检索（local_*、apply_patch）",
    "shell": "在本机执行系统命令与后台进程（run_command、process）",
    # 代码执行（MATRIX 5.17）。**单独立一个 toolset**，不并进 shell：两者风险面不同。
    # `shell` 是"跑一条系统命令"，`code` 是"跑一段模型自己写的代码"——后者能在一个
    # 进程里做任意事，收窄暴露面时往往想单独关掉它（保留 run_command 做构建/查询）。
    "code": "在子进程里执行一段代码（code_execution，非沙箱）",
    "browser": "浏览器观察与操作（browser_*）",
    "vault": "凭据填充与登录（vault_login）",
    "notion": "Notion 页面读写（notion_*）",
    "github": "GitHub 只读检索（github_*）",
    "memory": "跨会话长期记忆（memory_*）",
    "history": "历史任务检索（history_*）",
    # 会话召回（MATRIX 5.14）。**与 `history` 分开**：`history_*` 看的是"任务"这条
    # 记录（目标、状态、产物），`session_search` 找的是**对话原文**。两者查的表不同、
    # 泄漏面也不同（原文里可能有用户随口说的私事），所以必须能分别关。
    "session": "历史会话召回（session_search：按关键词找回以前对话的原话）",
    "retrieval": "已读内容回看（retriever_search）",
    "skills": "技能库读取与沉淀（skill_*）",
    "web": "联网搜索与正文抓取（web_search / web_extract）",
    "mcp": "MCP 服务动态暴露的工具（mcp__<server>__<tool>）",
    # ── 只有域、没有成员（MATRIX 5.15）────────────────────────────────────
    # 下面 8 个条目存在的**唯一**理由：让 19 个功能域各自可查（:func:`describe_domains`）。
    # 空域也必须有条目——否则"这个域确实没有工具"与"这个域根本没人想到"在输出里长得
    # 一模一样，而这两件事的后续动作完全不同（前者写进文档，后者写进排期）。
    # 它们永远不会有成员（原因见 :data:`EMPTY_TOOLSETS`），因此把它们写进
    # ``data/tools.json`` 的 ``disabled`` 是**合法的 no-op**，不是拼写错误。
    # **第 37 轮改口**：原来写「域内无模型工具：委派是图节点行为，不是可调用工具」——
    # 那句话在写的时候是对的（`ChildRunner` 协议零实现，注册了也调不动），
    # 但补上 `app/child_runner.py` 之后它变成了假话。**描述里的"没有"最容易过期**，
    # 所以改写成当前事实。
    "delegation": "委派子 agent（delegate_task 单个 / agent_swarm 按依赖批量）",
    "media": "媒体生成：图片 / 视频（域内无工具：要联网调付费 provider）",
    "vision": "视觉与视频理解（域内无工具：多模态由模型自带，不需要工具桥）",
    "tts": "语音合成（text_to_speech：本机走 Windows SAPI 或命令通道；19 家云端见 app/voice.py）",
    "kanban": "看板（域内无工具：Orbit 用 todo 工具 + 前端面板，没有看板后端）",
    "cron": "定时任务（cron_job：建 / 列 / 停 / 删；真正执行由 `orbit cron` 守护进程负责）",
    "desktop": "桌面与窗口（域内无工具：要 pyautogui 之类的桌面运行时，本机没有）",
    "home": "智能家居（域内无工具：要 Home Assistant 之类的网关与设备凭证）",
    "collab": "协作与消息投递（send_message：20 家出站适配器 + console/file 离线旁路）",
}

#: 声明式映射：工具名 → toolset。**新增工具必须在这里加一行**，否则落进
#: ``unclassified``（测试会失败）。之所以全量列出而不是只写前缀规则：
#: 这张表就是"暴露面清单"，review 时能一眼看出模型手里到底有什么。
TOOL_TOOLSET: dict[str, str] = {
    # delegation（第 37 轮）
    "delegate_task": "delegation",
    "agent_swarm": "delegation",
    # core
    "ask_user": "core",
    # 元工具：检索可用工具。归 core 而不是新建 toolset——它不该被"禁用 web"这类
    # 操作连带关掉，否则用户关掉一个域就再也找不到别的域有什么工具了。
    "tool_search": "core",
    # 元工具：任务清单（MATRIX 5.12）与结构化澄清（5.13）。同样归 core：它们属于
    # "对话骨架"，关掉任何一个，模型就只能靠记忆干活 / 靠散文提问。
    "todo": "core",
    "clarify": "core",
    # files
    "local_list_files": "files",
    "local_search_text": "files",
    "local_read_document": "files",
    # 分页读（MATRIX 5.14，对齐 Hermes `read_file` 的 offset/limit 口径）：与
    # `local_read_document` 同一个域、同一套权限档，差别只在"能不能读中间那一段"。
    "local_read_file": "files",
    # 递归搜索（MATRIX 5.14，对齐 Hermes `search_files` 的 content/files/count 三形态）。
    "local_search_files": "files",
    "local_write_report": "files",
    "local_write_file": "files",
    # 结构化补丁编辑（MATRIX 5.16）：改文件的另一种方式，所以归 files。
    "apply_patch": "files",
    # shell
    "run_command": "shell",
    # 后台进程管理（MATRIX 5.14，对齐 Hermes `process`）。归 shell 而不是新开一个
    # toolset：它起的就是同一种东西（系统命令），只是不等它结束。用户想"别让模型
    # 在我机器上乱跑命令"时，要一次关掉的正是这两个。
    "process": "shell",
    # code（MATRIX 5.17）：显式登记而不是靠前缀——`code_execution` 没有 `code_` 前缀
    # 之外的习惯可循，靠猜前缀归类正是这张表要防的那件事。
    "code_execution": "code",
    # browser
    "browser_observe": "browser",
    "browser_navigate": "browser",
    "browser_click": "browser",
    "browser_click_at": "browser",
    "browser_fill": "browser",
    "browser_press": "browser",
    "browser_scroll": "browser",
    # vault
    "vault_login": "vault",
    # notion
    "notion_read_page": "notion",
    "notion_append_summary": "notion",
    # github
    "github_search_issues": "github",
    "github_get_issue": "github",
    # memory
    "memory_search": "memory",
    "memory_save": "memory",
    "memory_delete": "memory",
    # history
    "history_search_tasks": "history",
    "history_get_task": "history",
    # session（MATRIX 5.14，对齐 Hermes `session_search`）
    "session_search": "session",
    # retrieval
    "retriever_search": "retrieval",
    # skills
    "skill_load": "skills",
    "skill_list": "skills",
    "skill_save": "skills",
    # web
    "web_search": "web",
    "web_extract": "web",
    # 延迟披露的桥（MATRIX 5.11 / 5.14）。归 core：它是「取被延迟工具正文」的唯一出口，
    # 一旦它自己被延迟，整个分级披露就没人能解套（同 `tool_search` 的理由）。
    "tool_describe": "core",
    # 定时任务（MATRIX 5.14）。执行仍归 `orbit cron` 的守护进程；模型侧这条只做
    # 建 / 列 / 停 / 删——**它不该也不能自己把任务跑起来**（那是调度器的职责）。
    "cron_job": "cron",
    # MATRIX 5.14 第二轮：本机能真跑的工具（实现见 `app/tool_extra_impl.py` 的
    # `GROWTH_TOOLS`）。没有现成前缀能落到正确 toolset，所以必须逐条登记——
    # `toolset_of()` 只认这张表与前缀表，不登记就是 UNCLASSIFIED。
    "tool_call": "core",
    "context_usage": "core",
    "compact": "core",
    "setup_mcp": "mcp",
    "text_to_speech": "tts",
    # MATRIX 5.14 第四轮：4 个浏览器工具（实现在 `app/browser.py`，
    # 由 `BROWSER_TOOLS` 注册，所以 `orbit_pool()` 必须靠这张表才看得见）。
    "browser_back": "browser",
    "browser_get_images": "browser",
    "browser_console": "browser",
    "browser_dialog": "browser",
    # MATRIX 5.14 第三轮（实现见 `app/tool_extra_impl2.py` 的 `ROUND3_TOOLS`）。
    "send_message": "collab",
    "memory_provider_list": "memory",
    "skill_search": "skills",
    "egress_status": "core",
    "budget_status": "core",
}

#: 前缀 → toolset。这是"新增工具自动归类"的兜底：按仓库既有命名习惯（``local_`` /
#: ``browser_`` / ``mcp__`` …）新加的工具会自然落到正确的组，不必等谁想起来改表。
#: **显式登记优先**：``TOOL_TOOLSET`` 里写过的名字不会被前缀规则改写。
#: 顺序有意义——长前缀必须排在同前缀的短前缀之前（当前无冲突，但别打乱）。
PREFIX_TOOLSETS: tuple[tuple[str, str], ...] = (
    ("mcp__", "mcp"),
    ("local_", "files"),
    ("browser_", "browser"),
    ("memory_", "memory"),
    ("skill_", "skills"),
    ("history_", "history"),
    ("notion_", "notion"),
    ("github_", "github"),
    ("web_", "web"),
)


# ── 19 个功能域（MATRIX 6.1 / 5.15）─────────────────────────────────────────
#
# 这张表和 ``app/tool_catalog.py`` 的 ``DOMAINS`` **逐字相同**，但**不能直接 import
# 它**：``tool_catalog`` 在模块顶层 ``from app import toolsets``，反过来 import 就成环。
# 于是这里复制一份，用测试（``tests/test_toolset_filtering.py``）盯住两边不许漂——
# 复制 + 门禁，比"延迟 import 造环"更便宜，也比"两边各写一份没人管"更可靠。

#: 19 个功能域 → 一行中文说明。顺序照 Hermes 报告 §4（与 ``tool_catalog.DOMAINS`` 一致）。
DOMAINS: dict[str, str] = {
    "file": "文件读写与编辑",
    "terminal": "终端与进程",
    "code": "代码执行",
    "browser": "浏览器",
    "web": "网页搜索与抓取",
    "search": "专项检索（X / 历史会话）",
    "memory": "记忆",
    "skills": "技能",
    "mcp": "外部工具接入（MCP）",
    "delegation": "委派与子 agent",
    "media": "媒体生成（图 / 视频）",
    "vision": "视觉与视频理解",
    "tts": "语音合成",
    "kanban": "看板",
    "cron": "定时任务",
    "desktop": "桌面与窗口",
    "collab": "协作与消息平台",
    "home": "智能家居",
    "meta": "元工具（清单 / 澄清 / 工具桥）",
}

#: 域 → 服务它的 toolset（可多个）。**每一层的语义**：
#:
#: * 一个 toolset 只能属于一个域（测试会断言覆盖关系是划分，不是重叠）——
#:   否则"域覆盖"会重复计数，看起来覆盖得比实际好。
#: * 域划分来自 Hermes，但 Hermes 没有 Orbit 独有的 ``vault`` / ``github`` /
#:   ``history`` / ``retrieval`` / ``notion`` 这五组。所以这张表是**Orbit 自己的
#:   归并账**：凭据填充归 browser（同一套浏览器会话）、GitHub 只读检索归 code
#:   （读的是代码托管）、历史任务与已读内容回看归 search（都是"找回以前的东西"）、
#:   Notion 归 collab（协作工具；Slack/Discord 是入站通道 ``app/platforms.py``，
#:   不是模型工具）。**这是归并，不是掩饰**：``describe_domains()`` 会把每个域的
#:   真实工具名列出来，读者一眼能看出某域是被"借"来的工具撑着的。
DOMAIN_TOOLSETS: dict[str, tuple[str, ...]] = {
    "file": ("files",),
    "terminal": ("shell",),
    "code": ("code", "github"),
    "browser": ("browser", "vault"),
    "web": ("web",),
    "search": ("session", "history", "retrieval"),
    "memory": ("memory",),
    "skills": ("skills",),
    "mcp": ("mcp",),
    "delegation": ("delegation",),
    "media": ("media",),
    "vision": ("vision",),
    "tts": ("tts",),
    "kanban": ("kanban",),
    "cron": ("cron",),
    "desktop": ("desktop",),
    "collab": ("notion", "collab"),
    "home": ("home",),
    "meta": ("core",),
}

#: 域里有 toolset 但**永远没有工具**的域 → 原因。``describe_domains()`` 用它填
#: ``empty=True`` 的那一列；缺了原因的空域会被测试拦下来（"空"必须解释得出来）。
EMPTY_TOOLSETS: dict[str, str] = {
    "delegation": (
        "委派在 Orbit 里是图节点行为（app/graph + app/delegate.py），不是模型可调用的工具。"
        "Hermes 的 delegate_task 对应 `orbit delegate` 命令面。"
    ),
    "media": "图片 / 视频生成要联网调付费 provider（DALL·E / Sora 之类），本机无网络、无凭证。",
    "vision": "视觉理解由多模态模型自带（providers.py 的 supports_vision），不需要工具桥。",
    "tts": "语音合成走 app/voice.py 与平台通道，是「输出方式」而不是「模型动作」。",
    "kanban": "Orbit 用 todo 工具 + 前端面板表达任务状态，没有独立看板后端可接。",
    "cron": (
        "定时任务的**执行**由 app/cron.py 与 `orbit cron` 守护进程承担；模型侧只有 "
        "`cron_job`（建/列/停/删，实现见 app/tool_extra_impl.py，需装配层注册），"
        "它不会自己跑任务，所以这个域在「真实工具表」视图里仍然是空的。"
    ),
    "desktop": "桌面 / 窗口控制要 pyautogui 之类的桌面运行时，本机未安装且不允许安装。",
    "home": "智能家居要 Home Assistant 之类的网关与设备凭证，本机没有。",
}


def domain_of(toolset: str) -> str:
    """toolset → 功能域。查不到返回空串（未归域，测试会拦）。"""
    for domain, group in DOMAIN_TOOLSETS.items():
        if toolset in group:
            return domain
    return ""


def domain_of_tool(name: str) -> str:
    """工具名 → 功能域。先归 toolset 再归域，保证两条路由永远一致。"""
    return domain_of(toolset_of(name))


# ── 分级披露（MATRIX 5.11）──────────────────────────────────────────────────
#
# `tier` 是"这个工具在首次请求里该拿多少披露"的等级，**写进 toolset 声明**而不是
# 逐个工具手写：同组的工具几乎总是同进同退，逐条写只会让新增工具默认掉到最差等级
# （没人记得改），而分级的目的恰恰是"默认别把罕见工具全塞进去"。
#
# 三级的分界不是"重要 / 不重要"，而是**错了的代价不对称**：
# * Tier 0 —— 缺了它这一轮对话根本没法开始（没有 run_command 就没法干活，
#   没有 tool_search 就找不到别的工具）。所以永不裁剪、永不延迟。
# * Tier 1 —— 日常路径，绝大多数轮次会用到（读写文件、跑命令、联网、记忆）。
#   描述保留全文，只有在预算实在压不下来时才动。
# * Tier 2 —— 少数轮次才用到（浏览器、MCP、技能、历史召回…）。预算紧张时
#   最先被压成一句，模型需要细节时用 `tool_search` 取。

#: tier 编号 → 一句话含义。``orbit tools list`` / ``tools domains`` 用它解释列。
TIERS: dict[int, str] = {
    0: "必备：永不裁剪、永不延迟（核心动作）",
    1: "常用：描述保留全文，预算吃紧时最后才动",
    2: "罕见：预算紧张时先压成一句，细节用 tool_search 取",
}

#: toolset → 默认 tier。没登记的 toolset 一律按 Tier 2 处理（**保守方向**：
#: 新增 toolset 默认是"可以晚点披露"，而不是默认抢占首屏预算）。
TOOLSET_TIERS: dict[str, int] = {
    "core": 0,
    "files": 1,
    "shell": 1,
    "web": 1,
    "memory": 1,
    "code": 2,
    "browser": 2,
    "vault": 2,
    "notion": 2,
    "github": 2,
    "history": 2,
    "session": 2,
    "retrieval": 2,
    "skills": 2,
    "mcp": 2,
    # 8 个空域没有成员，写在这里是为了"域 → tier"这张表自身完整、可被测试对账。
    "delegation": 2,
    "media": 2,
    "vision": 2,
    "tts": 2,
    "kanban": 2,
    "cron": 2,
    "desktop": 2,
    "home": 2,
    "collab": 2,
}

#: 单个工具的 tier 覆盖（比 toolset 声明优先）。目前为空，机制先立着：
#: 一旦某个工具和它同组邻居的频率差得远（比如 shell 组里的 `process`），
#: 就在这里单独调，而不是把整组抬上去。
TOOL_TIERS: dict[str, int] = {}

#: **永不延迟**的工具（MATRIX 5.11 的硬要求）。名字是 Orbit 的叫法，括号里是
#: Hermes 的同名工具——两边不同名的地方最容易漏，所以写清楚：
#:
#: * ``run_command``（terminal）—— 没有它，模型连"看看这个仓库有什么"都做不到；
#: * ``local_read_file``（read_file）—— 读文件是最基础的观察动作；
#: * ``local_write_file``（write_file）—— 写文件是最基础的产出动作；
#: * ``apply_patch``（patch）—— 改文件的结构化形态，和 write_file 是同一档；
#: * ``todo``（todo）—— 任务清单：缺了它，多步任务会退化成靠记忆；
#: * ``clarify``（clarify）—— 结构化澄清：缺了它只能靠散文提问；
#: * ``ask_user``（ask_user）—— 人工介入的唯一出口（图节点注册的工具）；
#: * ``tool_search``（tool_search）—— **元工具**：它被延迟就等于"找不到工具的工具
#:   自己也找不到"，整个分级披露当场失效；
#: * ``tool_describe``（tool_describe）—— **延迟披露的另一半**：Tier 2 工具被压成
#:   "名字 + 一句描述"之后，参数表只能从它这里取。它被延迟，被延迟的工具就再也拿不到
#:   参数表（见 :func:`defer_specs`）。
#:
#: GUI 侧（`browser_*`）刻意**不在**这里：浏览器一族要 playwright + chromium，
#: 装了才有意义，属于"按需披露"的典型，而不是"每轮必备"。
ALWAYS_EAGER: frozenset[str] = frozenset(
    {
        "run_command",
        "local_read_file",
        "local_write_file",
        "apply_patch",
        "todo",
        "clarify",
        "ask_user",
        "tool_search",
        "tool_describe",
    }
)

#: 工具 → tier。``ALWAYS_EAGER`` 里的工具**无条件**是 Tier 0（哪怕它所在的 toolset
#: 被声明成 Tier 1/2）——这正是"永不延迟"的实现点，而不是一句注释。
def tier_of(name: str) -> int:
    """工具名 → 披露等级（0/1/2）。"""
    if name in ALWAYS_EAGER:
        return 0
    explicit = TOOL_TIERS.get(name)
    if explicit is not None:
        return explicit
    return TOOLSET_TIERS.get(toolset_of(name), 2)


# ── 预算估算（MATRIX 5.11）─────────────────────────────────────────────────

#: 首次请求里工具清单的 token 预算。
#:
#: 4,000 是 Hermes 的口径（调研 §1「分级披露」）。**它不是一个能达到的硬上限**，
#: 理由见 :func:`app.tools.ToolRegistry.specs` 的 docstring：参数 schema 是调用契约，
#: 不能删；38 个内置工具的 schema 本身就是 ~4,300 token。所以这里的语义是
#: "描述这一维最多占多少"，超了就按 tier 从低到高把描述压成一句。
LISTING_BUDGET_TOKENS = 4000

#: 字符 → token 的粗略系数。**不引 tokenizer**：装不了依赖，而不同家的 tokenizer
#: 对中文/JSON 的偏差本来就大，估个量级足够做"要不要裁剪"这个二值判断。
#: 偏保守（4 字符/token 对中文是低估 token 数），宁可多裁一点也不要爆预算。
CHARS_PER_TOKEN = 4


def estimate_tokens(payload: Any) -> int:
    """把任意 JSON 可序列化对象估成 token 数（字符数 / :data:`CHARS_PER_TOKEN`，向上取整）。

    为什么接受任意对象而不只是 specs：调用方会拿它量"整份清单"、"单条描述"、
    "某个工具的参数 schema"三种粒度，统一一个函数比三份实现更不容易漂。
    """
    if isinstance(payload, str):
        text = payload
    else:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


# ── 延迟披露（MATRIX 5.11 的"出路"）─────────────────────────────────────────
#
# 4,000 token 的**描述预算**在真实工具集上压不下去：参数 schema 是调用契约，删不得，
# 而 38 个内置工具的 schema 地板本身就在 4,300 token 以上（见
# ``app/tools.py::ToolRegistry.specs`` 的 docstring 与 `_trim_specs`）。
#
# 所以真正的出路不是继续压描述，而是**让 Tier 2 的工具根本不进首屏**：首屏只给
# "名字 + 一句描述"，参数表等模型真的要调它时再取（`tool_describe`）。这不是省字，
# 而是把"披露"从一个固定成本变成按需成本。
#
# 两条边界必须写死，否则延迟就是"悄悄摘工具"：
# 1. **一个工具都不删**：被延迟的工具仍在清单里（名字 + 一句描述），模型看得见它存在；
# 2. **Tier 0 永不延迟**（:data:`ALWAYS_EAGER`），尤其是 `tool_search` 与
#    `tool_describe` —— 它们被延迟，"取回被延迟工具"这件事就没人能做了。

#: 描述压成一句时的字符上限。与 ``app/tools.py::TRIMMED_DESCRIPTION_CHARS`` 同一个值，
#: 但**不能 import 那个常量**：``app/tools.py`` 在模块顶层 ``from app import toolsets``，
#: 反向 import 会成环。两边由测试钉住不许漂。
TRIMMED_DESCRIPTION_CHARS = 80

#: 被延迟的工具在描述后面挂的提示。**写在描述里而不是加自定义字段**：供应商对 tool
#: 对象做严格校验，多一个 `x_orbit_deferred` 之类的键会被 400 拒掉；而描述本来就是
#: 给人（模型）看的，多一句"参数怎么取"正是它该说的话。
DEFERRED_HINT = "（参数用 tool_describe 取）"


def one_line(description: str, limit: int = TRIMMED_DESCRIPTION_CHARS) -> str:
    """把一段多句描述压成"第一句 + 省略号"。

    取第一句而不是截前 N 个字符：Orbit 的工具描述都是"先一句是什么，再若干句什么时候
    用"，截断正好砍在"什么时候用"上，留下半句没有主语的话。
    """
    text = " ".join(str(description or "").split())
    if len(text) <= limit:
        return text
    for mark in ("。", "；", ". ", "！", "？"):
        index = text.find(mark)
        if 0 < index < limit:
            return text[: index + len(mark)]
    return text[: limit - 1] + "…"


def defer_specs(
    specs: Sequence[Mapping[str, Any]], *, tiers: Sequence[int] = (2,)
) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    """把指定 tier 的工具压成"名字 + 一句描述 + 空参数表"，返回 ``(新 specs, 被延迟的名字)``。

    ``tiers`` 默认只延迟 Tier 2。**可以放宽到 ``(1, 2)``**：全量工具面（含 browser /
    web / github）只延迟 Tier 2 仍可能超预算，那时再延迟 Tier 1 是唯一还能动的维度
    （Tier 0 永不延迟，参数 schema 永不删）。

    **不改入参**（返回新的 dict），因为调用方常常把同一份清单复用于"量预算"与"发请求"，
    原地改会让日志里出现一份没发出去的清单。

    参数表换成 ``{"type": "object", "properties": {}}`` 而不是删掉：OpenAI 系要求
    `parameters.type == "object"`，删掉或给 `null` 会被判畸形 schema
    （``app/tools.py::schema_problems`` 会拦）。空对象是唯一既合法、又"什么都没承诺"的
    形状——模型看到它就知道"参数得另取"。
    """
    targets = {int(item) for item in tiers}
    out: list[dict[str, Any]] = []
    deferred: list[str] = []
    for spec in specs:
        body = spec.get("function")
        name = str(body.get("name") or "") if isinstance(body, Mapping) else ""
        if not isinstance(body, Mapping) or tier_of(name) not in targets:
            out.append(dict(spec))
            continue
        description = str(body.get("description") or "")
        short = one_line(description)
        if DEFERRED_HINT not in short:
            short = f"{short}{DEFERRED_HINT}"
        out.append(
            {
                **spec,
                "function": {
                    **body,
                    "description": short,
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        )
        deferred.append(name)
    return out, tuple(deferred)


# ── 归属判定 ────────────────────────────────────────────────────────────────


def toolset_of(name: str) -> str:
    """工具名 → toolset。查不到就返回 :data:`UNCLASSIFIED`（而不是猜一个）。"""
    explicit = TOOL_TOOLSET.get(name)
    if explicit is not None:
        return explicit
    for prefix, toolset in PREFIX_TOOLSETS:
        if name.startswith(prefix):
            return toolset
    return UNCLASSIFIED


def declared_tool_names() -> list[str]:
    """声明表里登记过的全部工具名（排序后，便于 diff 与输出稳定）。"""
    return sorted(TOOL_TOOLSET)


def declared_toolsets() -> list[str]:
    """全部 toolset 名（声明顺序即输出顺序：先具体功能域，最后 mcp）。"""
    return list(TOOLSETS)


def members(toolset: str, names: Iterable[str] | None = None) -> list[str]:
    """某个 toolset 下的工具名。``names`` 为空时只看声明表。"""
    pool = list(names) if names is not None else declared_tool_names()
    return [name for name in pool if toolset_of(name) == toolset]


# ── 配置文件读写 ────────────────────────────────────────────────────────────


def config_path(data_dir: str | Path) -> Path:
    """``data/tools.json`` 的绝对路径。"""
    return Path(data_dir) / CONFIG_FILENAME


def empty_config() -> dict[str, Any]:
    """空配置 = 全开。所有平台都缺省，因此没有任何工具被排除。"""
    return {"platforms": {}}


#: 读盘缓存：``path -> (mtime, config)``。
#:
#: 为什么需要：``ToolRegistry.specs()`` 每一轮都会走一次过滤，而它落在请求路径上。
#: 每次 turn 都读一次 JSON 文件既浪费又会在多进程下放大磁盘抖动。用 mtime 当失效
#: 指纹就够了——``orbit tools enable/disable`` 写完盘 mtime 必变，下一次读自然刷新。
_CONFIG_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def load_config(path: str | Path) -> dict[str, Any]:
    """读配置文件。**任何异常都退回"全开"**，绝不让坏配置把工具面打成空。

    理由：配置读不出来时，模型手里一个工具都没有，等于整个 agent 失效。相比之下
    "多暴露了几个工具"是更轻的故障。坏文件会打 warning 留证据。
    """
    target = Path(path)
    try:
        stamp = target.stat().st_mtime
    except OSError:
        return empty_config()
    cached = _CONFIG_CACHE.get(str(target))
    if cached is not None and cached[0] == stamp:
        return copy.deepcopy(cached[1])
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("工具集配置 %s 读取失败，按全开处理：%s", target, exc)
        return empty_config()
    config = raw if isinstance(raw, dict) else empty_config()
    if not isinstance(config.get("platforms"), dict):
        config = {**config, "platforms": {}}
    _CONFIG_CACHE[str(target)] = (stamp, copy.deepcopy(config))
    return config


def save_config(path: str | Path, config: Mapping[str, Any]) -> Path:
    """写配置文件（先写临时文件再替换，避免半截 JSON）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(config), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    _CONFIG_CACHE.pop(str(target), None)
    return target


def _str_list(value: Any) -> list[str]:
    """把配置里的列表字段读成去重保序的字符串列表；坏值当空。"""
    if not isinstance(value, (list, tuple)):
        return []
    seen: dict[str, None] = {}
    for item in value:
        text = str(item).strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)


def platform_state(
    config: Mapping[str, Any] | None, platform: str = DEFAULT_PLATFORM
) -> tuple[list[str], list[str]]:
    """返回该平台的 ``(disabled, enabled)``。

    ``enabled`` 为空 = 不收窄（默认全开模式）；非空 = 只暴露 ``enabled`` 里的。
    这正是 Hermes 的不对称语义：禁用是"减法"，默认不写就是开；收窄是"白名单"，
    只在用户显式列了东西时生效。
    """
    platforms = (config or {}).get("platforms")
    if not isinstance(platforms, Mapping):
        return [], []
    entry = platforms.get(platform)
    if not isinstance(entry, Mapping):
        return [], []
    return _str_list(entry.get("disabled")), _str_list(entry.get("enabled"))


def platforms_in(config: Mapping[str, Any] | None) -> list[str]:
    """配置里出现过的平台名（供 ``tools --summary`` 枚举）。"""
    platforms = (config or {}).get("platforms")
    if not isinstance(platforms, Mapping):
        return []
    return [str(name) for name in platforms]


def _hits(entry: str, name: str, toolset: str) -> bool:
    """一条配置项是否命中某个工具。

    四种粒度都要认：单个工具名（``web_search``）、toolset 名（``web``）、
    Hermes 命令面的 ``server:tool``（``github:create_issue``）、server 级
    ``mcp:<server>``。为什么不能只比工具名：Hermes 的启用单位是"内置按 toolset、
    MCP 按单工具"，两套粒度必须在同一份配置里共存。
    """
    text = entry.strip()
    if not text:
        return False
    if text.startswith("mcp__"):
        # mcp__<server>__<tool> 精确匹配；mcp__<server> 按 server 前缀匹配
        if len(text.split("__")) >= 3:
            return name == text
        return name.startswith(text + "__")
    if text.startswith("mcp:"):
        return name.startswith(f"mcp__{text[4:]}__")
    if ":" in text:
        server, _, tool = text.partition(":")
        return name == f"mcp__{server}__{tool}"
    return text == name or text == toolset


def _matching_entry(entries: Sequence[str], name: str, toolset: str) -> str:
    for entry in entries:
        if _hits(entry, name, toolset):
            return entry
    return ""


# ── 可用性探针（Hermes 的 `check_fn`，MATRIX 5.3）────────────────────────────
#
# 三层过滤的第二层：**工具在不在** 与 **工具能不能用** 是两件事。`github_*` 没配
# token 时仍然"在"，但调它必然报错；`browser_*` 没装 chromium 时同理。把这类工具
# 照常暴露给模型，只会换来一串"我试了但失败了"的回合。
#
# 但"探针失败就摘工具"是个陷阱：一次超时（负载下的 `playwright` 启动、网络抖一下）
# 就会把整个 toolset 从正在干活的 agent 上摘掉，子 agent 随即报"工具不存在"。
# Hermes 为此加了 **30s TTL + 60s 抖动宽限**（registry.py:514-519），这里照抄同一
# 组数字，理由一致。

#: 探针结果的有效期。期内重复询问直接返回缓存，不再真的探测。
PROBE_TTL_SECONDS = 30.0
#: 抖动宽限：距上次"探测成功"不到这么久时，本次失败按抖动处理——**不摘工具**，
#: 也不缓存这次失败（下次重新探）。
PROBE_GRACE_SECONDS = 60.0

#: 工具 → 它需要什么。键可以是**精确工具名**，也可以是以 ``_`` 结尾的**前缀**。
#:
#: 只登记"不满足就必然调不通"的需求。刻意**不**登记 `local_*` / `memory_*` 这类
#: 无外部依赖的工具：给它们编需求只会制造误摘的机会。
TOOL_REQUIREMENTS: dict[str, str] = {
    "browser_": "playwright",  # 浏览器全族要 playwright + chromium 在
    "notion_": "playwright",  # notion_* 走浏览器会话，同一份依赖
    "github_": "github_token",  # GitHub API 没 token 会被限流/拒绝
    "web_": "search_provider",  # 搜索要至少一个可用 provider
    "run_command": "shell",  # 要有可执行的 shell
    "process": "shell",  # 后台进程同样要 shell（起的是同一种东西，只是不等它结束）
}


def requirement_of(name: str) -> str:
    """工具名 → 需求 id（空串 = 没有声明的需求 = 无条件可用）。"""
    exact = TOOL_REQUIREMENTS.get(name)
    if exact:
        return exact
    for key, requirement in TOOL_REQUIREMENTS.items():
        if key.endswith("_") and name.startswith(key):
            return requirement
    return ""


def _check_playwright(settings: Any = None) -> tuple[bool, str]:
    """playwright 能不能用：包在 + chromium 在。

    **为什么不去真的启动一次**：``sync_playwright().start()`` 会 spawn 一个 node 驱动
    进程，几百毫秒起步、在负载下还可能挂住。探针落在每轮请求的暴露面计算路径上，
    这个代价付不起（Hermes 的 ``subprocess.run(["docker","version"])`` 教训就是这个）。
    真正"启动失败"的信号更准、更便宜——第一次调用工具时的报错。所以这里的判据是
    "依赖齐不齐"，不是"能不能跑起来"。
    """
    if importlib.util.find_spec("playwright") is None:
        return False, "playwright 未安装"
    candidates: list[Path] = []
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if override:
        candidates.append(Path(override))
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        candidates.append(Path(local) / "ms-playwright")
    candidates.append(Path.home() / ".cache" / "ms-playwright")
    for base in candidates:
        try:
            if not base.is_dir():
                continue
            if any(item.name.startswith("chromium") for item in base.iterdir()):
                return True, ""
        except OSError:
            continue
    return False, "playwright 已安装但找不到 chromium（先执行 playwright install chromium）"


def _check_github_token(settings: Any = None) -> tuple[bool, str]:
    """GitHub 工具要 token：没有它就必然 403 / 限流。"""
    raw = getattr(settings, "github_token", None) if settings is not None else None
    getter = getattr(raw, "get_secret_value", None)
    token = str(getter() or "") if callable(getter) else str(raw or "")
    token = token.strip() or os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return False, "未配置 github_token（设置里填，或设环境变量 GITHUB_TOKEN）"
    return True, ""


def _check_search_provider(settings: Any = None) -> tuple[bool, str]:
    """web_* 要至少一个可用 provider。

    `settings` 为 None 时**按可用处理**（fail-open）：没有配置对象就无从判断，
    这时摘工具等于凭空少一个功能。
    """
    if settings is None:
        return True, ""
    try:
        from app.web_providers import available_providers

        providers = available_providers(settings)
    except Exception as exc:  # noqa: BLE001 - 探针本身不该把命令面打崩
        return True, f"provider 探测失败（按可用处理）：{exc}"
    if any(item.get("available") for item in providers):
        return True, ""
    return False, "没有可用的搜索 provider（配置 searxng_url / brave_search_api_key 等）"


def _check_shell(settings: Any = None) -> tuple[bool, str]:
    """run_command 要有一个能执行的 shell。"""
    if os.name == "nt":
        return True, ""  # cmd.exe 是系统组件，不存在的话整个平台都不成立
    return (True, "") if Path("/bin/sh").exists() else (False, "找不到 /bin/sh")


#: 需求 id → 检查函数。返回 ``(可用?, 不可用原因)``。
REQUIREMENT_CHECKS = {
    "playwright": _check_playwright,
    "github_token": _check_github_token,
    "search_provider": _check_search_provider,
    "shell": _check_shell,
}


@dataclass
class ProbeState:
    """一次探针的结论。"""

    available: bool
    reason: str
    checked_at: float
    #: 上次**判定可用**的时刻。``None`` = 从未成功过（区别于"成功时刻恰好是 0"，
    #: 所以这里不用 0 当哨兵值——假时钟测试会踩到）。
    last_good_at: float | None = None


#: 探针缓存（进程内）。键是工具名：同一个 requirement 被多个工具共享时各记一份，
#: 这样"某个工具名被摘掉"不会牵连同组的其它工具（Hermes 的 key 是 ``(fn, scope)``）。
_PROBE_CACHE: dict[str, ProbeState] = {}


def probe(
    name: str,
    settings: Any = None,
    *,
    now: float | None = None,
    trigger: bool = True,
) -> tuple[bool, str]:
    """问一句"这个工具现在可用吗"。返回 ``(可用?, 不可用原因)``。

    ``trigger=False`` 是**只读缓存**：过期或没探测过一律按"可用"返回，绝不触发真实
    探测。暴露面计算（:func:`resolve`）必须走这条路——它每轮都会跑，而探针会读盘、
    读密钥、glob 浏览器目录。

    ``trigger=True`` 的语义照 Hermes：

    1. 缓存新鲜（< :data:`PROBE_TTL_SECONDS`）→ 直接返回，**不重复探测**；
    2. 探测成功 → 记缓存，并更新 `last_good_at`；
    3. 探测失败但距上次成功 < :data:`PROBE_GRACE_SECONDS` → 判为抖动，返回可用且
       **不缓存失败**（下次重新探）；
    4. 否则缓存失败并返回不可用。
    """
    requirement = requirement_of(name)
    if not requirement:
        return True, ""
    check = REQUIREMENT_CHECKS.get(requirement)
    if check is None:  # 声明了需求却没有检查器：按可用处理，别让笔误变成摘工具
        return True, ""
    stamp = time.monotonic() if now is None else now
    state = _PROBE_CACHE.get(name)
    if state is not None and stamp - state.checked_at < PROBE_TTL_SECONDS:
        return state.available, state.reason
    if not trigger:
        return True, ""
    try:
        ok, reason = check(settings)
    except Exception as exc:  # noqa: BLE001 - 探针炸了算失败，但不该把调用方打崩
        ok, reason = False, f"探针异常：{type(exc).__name__}: {exc}"
    if ok:
        _PROBE_CACHE[name] = ProbeState(True, "", stamp, stamp)
        return True, ""
    last_good = state.last_good_at if state is not None else None
    if last_good is not None and stamp - last_good < PROBE_GRACE_SECONDS:
        log.info("工具 %s 的探针失败（%s），但距上次成功不足宽限期，按抖动处理", name, reason)
        return True, ""
    _PROBE_CACHE[name] = ProbeState(False, reason, stamp, last_good)
    # 用 info 而不是 warning：CLI（`orbit tools probe`）会把不可用原因直接打印给人，
    # 再往 stderr 打一份 warning 就是重复噪声。长期运行的 agent 侧需要这条记录，
    # info 级别在默认日志配置里照样落盘。
    log.info("工具 %s 不可用：%s", name, reason)
    return False, reason


def cached_probe(name: str, *, now: float | None = None) -> tuple[bool, str]:
    """:func:`probe` 的只读形式（不触发探测）。"""
    return probe(name, trigger=False, now=now)


def probe_all(
    names: Iterable[str], settings: Any = None, *, now: float | None = None
) -> list[dict[str, Any]]:
    """逐个探测（会触发真实检查），返回可直接展示的行。"""
    rows: list[dict[str, Any]] = []
    for name in names:
        requirement = requirement_of(name)
        available, reason = probe(name, settings, now=now) if requirement else (True, "")
        rows.append(
            {
                "name": name,
                "toolset": toolset_of(name),
                "requirement": requirement,
                "available": available,
                "reason": reason,
            }
        )
    return rows


def invalidate_probe_cache() -> None:
    """清空探针缓存（``orbit tools enable/disable`` 之后应当调用）。"""
    _PROBE_CACHE.clear()


# ── 四层过滤（MATRIX 6.2）───────────────────────────────────────────────────
#
# 只有"平台层"是不够的。同一个工具在四个不同维度上会有四种答案：
#
#   1. **平台层**——`data/tools.json` 的 `platforms.<platform>`。回答"这个渠道上有
#      没有"（Telegram 不给 run_command，CLI 给）。
#   2. **场景层**——这一轮是谁在跑（`chat` / `cron` / `subagent` / `acp`）。
#      回答"此刻有没有意义"。cron 里没人能回答 clarify 的问题，给了只会空转。
#   3. **profile 层**——`data/tools.json` 的 `profiles.<name>`。回答"这个人/这个
#      配置有没有开"。同一台机器上，只读审查 profile 和开发 profile 的暴露面不同。
#   4. **委托层**——这一轮是不是被委派出来的。回答"能不能再往下传"。
#
# 为什么要分成四层而不是一张大表：**每一层的失败模式不同**，排查时必须能定位到
# 是哪一层拦的。所以 :func:`explain` 逐层给结论，而不是一句"不可见"。

#: 允许的场景。新增场景必须同时想清楚"它和 chat 差在哪"——差别说不出来就不该新开。
SCENES: tuple[str, ...] = ("chat", "cron", "subagent", "acp")

#: 默认场景：交互式对话。与 :data:`DEFAULT_PLATFORM` 同一个道理——不带参数时落到
#: 最常用、限制最少的那个。
DEFAULT_SCENE = "chat"

#: 场景 → 该场景**额外排除**的条目（工具名 / toolset 名 / MCP 写法，同 ``_hits`` 语义）。
#:
#: 只写"这个场景里做了也没用"的条目，不写"风险高"的条目——风险归平台层和 profile 层，
#: 混在一起会让"为什么 cron 没有 run_command"变成查不出来的问题。
SCENE_EXCLUDES: dict[str, tuple[str, ...]] = {
    # chat：交互式对话，什么都不额外排除。
    "chat": (),
    # cron：定时任务跑在没人看着的时钟上。交互类工具问出去的问题**永远不会有回答**，
    # 模型只能空转到超时，然后把"我提了个问题"写进结果。整组摘掉比让它试一次好。
    "cron": ("ask_user", "clarify", "todo"),
    # subagent：子 agent 不该再起子 agent。递归没有深度上限，一次失控就是指数级 spawn，
    # 而且每一层都带着自己的上下文预算——账单会先炸。
    "subagent": ("delegate_task",),
    # acp：编辑器内嵌面板。它在**编辑器的进程里**跑，弹浏览器会跑到用户的桌面会话上
    # （窗口从哪冒出来的没人知道，也没人能接管登录），所以凭据填充不给。
    "acp": ("vault_login",),
}

#: 委托层：``FilterContext.delegated=True``（这一轮是被委派出来的）时排除的条目。
#:
#: * ``delegate_task``：防无限递归（与 subagent 场景同一理由，但这一层独立存在——
#:   一个 delegated 的 run 完全可能声明自己是 ``scene="chat"``）；
#: * ``ask_user`` / ``clarify``：被委派者与用户之间隔着委派者。直接问用户等于绕过
#:   委派者的汇总与判断，用户会看到一堆互不相关的问题。
DELEGATION_EXCLUDES: tuple[str, ...] = ("delegate_task", "ask_user", "clarify")

#: 层名 → 中文标签。``explain()`` 用它，让输出与代码里的层一一对应。
LAYERS: dict[str, str] = {
    "platform": "平台层",
    "scene": "场景层",
    "profile": "profile 层",
    "delegated": "委托层",
}


@dataclass(frozen=True)
class FilterContext:
    """一轮请求的过滤上下文：四层各一个维度。

    frozen：它会被当作参数在调用链上传来传去，中途被谁改了会让"同一轮里两次
    ``resolve`` 结果不同"——那是最难查的一类 bug。
    """

    platform: str = DEFAULT_PLATFORM
    scene: str = DEFAULT_SCENE
    #: profile 名（``data/tools.json`` 的 ``profiles.<name>``）。空串 = 不做 profile 收窄。
    profile: str = ""
    #: True = 这一轮是被委派出来的（``delegate_task`` 起的那条链）。
    delegated: bool = False

    def __post_init__(self) -> None:
        if self.scene not in SCENES:
            raise ValueError(f"未知场景 {self.scene!r}（可选：{' / '.join(SCENES)}）")


def profile_state(
    config: Mapping[str, Any] | None, profile: str
) -> tuple[list[str], list[str]]:
    """返回该 profile 的 ``(disabled, enabled)``。语义与 :func:`platform_state` 一致。

    ``profile`` 为空、或配置里没有这一段，都返回 ``([], [])``（= 不收窄）。
    为什么 profile 层不默认"全关"：profile 是**加法的便利**（给某类工作收窄），
    不是安全边界；安全边界在平台层。把 profile 当成"没配就等于全关"，会让新增
    profile 的人先把 agent 弄成哑巴。
    """
    if not profile:
        return [], []
    profiles = (config or {}).get("profiles")
    if not isinstance(profiles, Mapping):
        return [], []
    entry = profiles.get(profile)
    if not isinstance(entry, Mapping):
        return [], []
    return _str_list(entry.get("disabled")), _str_list(entry.get("enabled"))


def _layer_exclusion(
    name: str, layer: str, context: FilterContext, config: Mapping[str, Any] | None
) -> str:
    """某一层是否排除这个工具。返回**命中的条目**（空串 = 这一层放行）。

    四层各一段，故意不写成一张表：每层的判定来源不同（配置 / 常量 / 布尔标志），
    塞进同一张表就要加一堆 ``if layer == ...``，反而看不出每层在判什么。
    """
    toolset = toolset_of(name)
    if layer == "platform":
        disabled, enabled = platform_state(config, context.platform)
        if disabled and (entry := _matching_entry(disabled, name, toolset)):
            return entry
        if enabled and not _matching_entry(enabled, name, toolset):
            return "（不在 enabled 白名单里）"
        return ""
    if layer == "scene":
        return _matching_entry(SCENE_EXCLUDES.get(context.scene, ()), name, toolset)
    if layer == "profile":
        disabled, enabled = profile_state(config, context.profile)
        if disabled and (entry := _matching_entry(disabled, name, toolset)):
            return entry
        if enabled and not _matching_entry(enabled, name, toolset):
            return "（不在 enabled 白名单里）"
        return ""
    if layer == "delegated":
        if not context.delegated:
            return ""
        return _matching_entry(DELEGATION_EXCLUDES, name, toolset)
    raise ValueError(f"未知过滤层：{layer}")


def _layer_reason(name: str, layer: str, hit: str, context: FilterContext) -> str:
    """把"命中哪一层 + 命中哪条"翻成一句人话。**这是 explain 的全部价值所在**：
    用户看到的必须是"cron 场景不给交互类工具，因为没人能回答"，而不是"被排除了"。"""
    if layer == "platform":
        if hit.startswith("（"):
            return f"{context.platform} 平台：{hit.strip('（）')}"
        return f"{context.platform} 平台的禁用集命中「{hit}」"
    if layer == "scene":
        if context.scene == "cron" and hit in {"ask_user", "clarify", "todo"}:
            return f"cron 场景不给交互类工具（命中「{hit}」）：定时任务没人能回答，问了只会空转到超时"
        if context.scene == "subagent" and hit == "delegate_task":
            return "subagent 场景不给 delegate_task：子 agent 再起子 agent 会无限递归"
        if context.scene == "acp" and hit == "vault_login":
            return "acp 场景不给 vault_login：编辑器内嵌面板里没人能接管浏览器登录"
        return f"{context.scene} 场景排除「{hit}」"
    if layer == "profile":
        if hit.startswith("（"):
            return f"profile「{context.profile}」的白名单里没有它"
        return f"profile「{context.profile}」的禁用集命中「{hit}」"
    if hit == "delegate_task":
        return "委托链里不给 delegate_task：防无限递归"
    return f"委托链里不给「{hit}」：被委派者与用户之间隔着委派者，直接问会绕过它"


def explain(
    tool: str,
    context: FilterContext,
    config: Mapping[str, Any] | None = None,
) -> str:
    """逐层解释一个工具为什么可见 / 不可见（MATRIX 6.2 的"每一层都要能解释"）。

    返回多行文本，**每一层都有一行**——包括放行的层。只打印拦它的那一层看起来更短，
    但排查时最常问的是"另外三层为什么不拦它"，缺了就得回头读代码。
    """
    toolset = toolset_of(tool)
    lines = [
        f"{tool}（toolset={toolset}，域={domain_of(toolset) or '未归域'}，"
        f"tier={tier_of(tool)}，场景={context.scene}，平台={context.platform}，"
        f"profile={context.profile or '（未指定）'}）"
    ]
    blocked_by = ""
    for layer in ("platform", "scene", "profile", "delegated"):
        hit = _layer_exclusion(tool, layer, context, config)
        label = LAYERS[layer]
        if hit:
            reason = _layer_reason(tool, layer, hit, context)
            lines.append(f"  {label}：排除 —— {reason}")
            blocked_by = blocked_by or label
        else:
            detail = {
                "platform": f"{context.platform} 上没有针对它的禁用 / 白名单条目",
                "scene": f"{context.scene} 场景不排除它",
                "profile": (
                    "未指定 profile，本层不收窄"
                    if not context.profile
                    else f"profile「{context.profile}」不排除它"
                ),
                "delegated": (
                    "委托链里也不排除它"
                    if context.delegated
                    else "不是被委派的一轮，本层不生效"
                ),
            }[layer]
            lines.append(f"  {label}：放行 —— {detail}")
    # 可用性探针是第五个（不属于四层，但同样决定"调得到调不到"），
    # 只读缓存：explain 不该顺手触发真实探测。
    available, reason = cached_probe(tool)
    lines.append(
        "  可用性：可用" if available else f"  可用性：不可用 —— {reason}"
    )
    lines.append(f"  结论：{'不可见（' + blocked_by + '拦下）' if blocked_by else '可见'}")
    return "\n".join(lines)


# ── 解析 / 视图 ─────────────────────────────────────────────────────────────


def resolve(
    names: Iterable[str],
    platform: str = DEFAULT_PLATFORM,
    config: Mapping[str, Any] | None = None,
    *,
    context: FilterContext | None = None,
    now: float | None = None,
) -> list[str]:
    """算出**实际暴露**的工具名（保持传入顺序）。

    判定顺序（先禁用、再收窄、最后看可用性）：
    1. 平台层：命中 ``disabled`` → 排除；``enabled`` 非空且不命中 → 排除（白名单）；
    2. 场景层：命中 :data:`SCENE_EXCLUDES` → 排除（给了 ``context`` 才生效）；
    3. profile 层：命中 ``profiles.<profile>`` 的禁用 / 白名单 → 排除（同上）；
    4. 委托层：``context.delegated`` 且命中 :data:`DELEGATION_EXCLUDES` → 排除；
    5. 探针缓存里**明确判定不可用** → 排除；
    6. 其余暴露。

    禁用优先于启用：同时写了 ``disable web`` 和 ``enable web_search`` 时，显式禁用赢。
    理由是"关掉"通常是安全决策，"开着"只是便利决策，冲突时该选保守的那个。

    ``context`` 留空 = **只有平台层生效**，与引入四层之前的签名与行为逐字一致。
    这是刻意的：``ToolRegistry.specs()`` 每轮都走这条路，把它一次性改成四层会让
    所有既有调用点（以及几十条测试）一起漂；四层是**显式传入**才生效的。

    探针那一步**只读缓存**（``probe(trigger=False)``），绝不触发真实探测：``resolve``
    落在每轮请求的暴露面计算上，而探针会读盘、读密钥、glob 浏览器目录。没探测过
    或结果过期时按"可用"处理——**宁多暴露一个调不通的工具，也不因为没探测就把工具
    悄悄摘掉**。真正的探测由 ``orbit tools probe`` / ``probe_all`` 触发。
    """
    if context is not None:
        platform = context.platform
    exposed: list[str] = []
    for name in _config_exposure(names, platform, config):
        if context is not None and any(
            _layer_exclusion(name, layer, context, config)
            for layer in ("scene", "profile", "delegated")
        ):
            continue
        if not cached_probe(name, now=now)[0]:
            continue
        exposed.append(name)
    return exposed


def _config_exposure(
    names: Iterable[str],
    platform: str = DEFAULT_PLATFORM,
    config: Mapping[str, Any] | None = None,
) -> list[str]:
    """**只看配置**的暴露面（不看探针）。

    与 :func:`resolve` 分开是因为两者语义不同：``resolve`` 回答"此刻能暴露什么"，
    这个函数回答"配置允许暴露什么"。``post_setup_snapshot`` 必须用后者——把
    "此刻恰好不可用"（没装 chromium、没配 token）写进配置快照，等于把一次瞬时状态
    固化成永久禁用，之后用户装好了依赖，工具还是不会回来。
    """
    disabled, enabled = platform_state(config, platform)
    exposed: list[str] = []
    for name in names:
        toolset = toolset_of(name)
        if disabled and _matching_entry(disabled, name, toolset):
            continue
        if enabled and not _matching_entry(enabled, name, toolset):
            continue
        exposed.append(name)
    return exposed


def describe(
    platform: str = DEFAULT_PLATFORM,
    config: Mapping[str, Any] | None = None,
    names: Iterable[str] | None = None,
    *,
    settings: Any = None,
    probe_fresh: bool = True,
    by_domain: bool = False,
) -> list[dict[str, Any]]:
    """``orbit tools list`` 的完整视图：工具名 / toolset / 是否暴露 / 为什么被排除。

    ``reason`` 是给人看的诊断信息——"看不到这个工具"必须能当场解释，否则用户只能
    靠猜是没注册、被禁用、还是被白名单挡了。

    ``note`` 是**不改变暴露结果**的提醒（目前只有"未归类"一种）。为什么未归类不直接
    排除：``resolve`` 里若按"没归类就不暴露"处理，新加的工具会静默消失，模型只会报
    "工具不存在"，排查方向完全跑偏。所以运行时放行 + 打标，由测试和 ``tools list``
    把问题顶到明面上。

    ``probe_fresh``（默认开）：``tools list`` 是**人主动发起的一次性诊断命令**，
    在这里跑一次探针是合理的——不然"为什么这个工具用不了"永远只能靠猜。反过来
    ``resolve``（每轮请求都跑）只读缓存，两者刻意不对称。

    ``by_domain=True``：改按 **19 个功能域**输出（MATRIX 5.15 的判据是"每个域的覆盖
    情况可查"，逐工具的行读不出这件事）。默认 ``False`` 保持逐工具的旧形状——``tools
    list`` 与既有测试都按那个形状写的，换形状等于把回归闸一起拆了。
    """
    if by_domain:
        return describe_domains(names)
    pool = list(names) if names is not None else declared_tool_names()
    disabled, enabled = platform_state(config, platform)
    rows: list[dict[str, Any]] = []
    for name in pool:
        toolset = toolset_of(name)
        reason = ""
        if disabled and (entry := _matching_entry(disabled, name, toolset)):
            reason = f"禁用集命中「{entry}」"
        elif enabled and not _matching_entry(enabled, name, toolset):
            reason = "不在 enabled 白名单里"
        else:
            available, unavailable_reason = (
                probe(name, settings) if probe_fresh else cached_probe(name)
            )
            if not available:
                reason = f"不可用：{unavailable_reason}"
        note = ""
        if toolset == UNCLASSIFIED:
            note = "未归类：请在 app/toolsets.py 的 TOOL_TOOLSET 里登记"
        rows.append(
            {
                "name": name,
                "toolset": toolset,
                # 域也放进逐工具视图：`tools list` 的输出里能一眼看出"这个工具算哪个
                # 功能域"，不必再跑一次 `tools domains` 反查。
                "domain": domain_of(toolset),
                "tier": tier_of(name),
                "exposed": not reason,
                "reason": reason,
                "note": note,
                "requirement": requirement_of(name),
            }
        )
    return rows


def describe_domains(names: Iterable[str] | None = None) -> list[dict[str, Any]]:
    """**按 19 个功能域**输出覆盖情况（MATRIX 5.15 的判据就是这一条）。

    每一行回答四件事，缺一件这个域就没法"可查"：

    * ``toolsets`` —— 服务这个域的 toolset（可能是空的，见 :data:`DOMAIN_TOOLSETS`）；
    * ``tools`` —— 真实工具名（空列表就是"这个域确实没有工具"，不是"忘了填"）；
    * ``empty`` + ``reason`` —— 空域**必须**给出原因。没原因的空域等于一个没人负责的
      坑：下一个人看到 `empty=True` 只会以为是笔误。
    * ``dynamic`` —— 该域的工具是运行时才知道名字的（只有 ``mcp``）。这类域的
      ``tools`` 空不代表没能力，所以不能跟真空域混成一类。

    ``names`` 留空时只看声明表；调用方通常传真实工具名（注册表里的全部），
    这样"声明有、实际没注册"的工具也能被算出来。
    """
    pool = list(names) if names is not None else declared_tool_names()
    rows: list[dict[str, Any]] = []
    for domain, label in DOMAINS.items():
        toolsets_of_domain = DOMAIN_TOOLSETS.get(domain, ())
        tools = [name for name in pool if toolset_of(name) in toolsets_of_domain]
        dynamic = domain == "mcp"  # MCP 工具名运行时生成（mcp__<server>__<tool>）
        reason = ""
        if not tools:
            declared_here = [
                name for name in declared_tool_names() if toolset_of(name) in toolsets_of_domain
            ]
            if dynamic:
                reason = "工具名由 MCP 服务在运行时给出，连接前无法枚举——空列表不等于没能力"
            elif declared_here:
                # 域本身有工具，只是**这一批**没带上（注册表还没加载 / 调用方只传了子集）。
                # 与"这个域本来就没有工具"必须分开说，否则一次子集查询会被读成功能缺口。
                reason = "该域有声明过的工具，但都不在当前工具池里（未注册或未加载）"
            else:
                reason = EMPTY_TOOLSETS.get(
                    toolsets_of_domain[0] if toolsets_of_domain else "",
                    "该域已声明但没有登记任何工具，且没有写原因（测试会拦）",
                )
        rows.append(
            {
                "domain": domain,
                "label": label,
                "toolsets": list(toolsets_of_domain),
                "tools": sorted(tools),
                "count": len(tools),
                "empty": not tools,
                "dynamic": dynamic,
                "reason": reason,
            }
        )
    return rows


# ── 配置变更（给 `orbit tools enable/disable` 用）────────────────────────────


def toggle(
    config: Mapping[str, Any] | None,
    platform: str,
    entries: Sequence[str],
    *,
    enable: bool,
) -> dict[str, Any]:
    """在配置里启用/禁用若干条目，返回**新**配置（不改原对象）。

    语义细节（照 Hermes 的启用/禁用模型）：
    - 禁用：加进 ``disabled``，并从 ``enabled`` 里摘掉（否则两条规则打架）；
    - 启用：从 ``disabled`` 里摘掉；**若当前处于白名单模式**（``enabled`` 非空），
      再把它加进 ``enabled``——否则"启用"在收窄模式里毫无效果，用户会以为命令坏了。
    """
    updated = copy.deepcopy(dict(config or empty_config()))
    platforms = updated.setdefault("platforms", {})
    if not isinstance(platforms, dict):  # 坏配置就地修好，而不是整份丢掉
        platforms = {}
        updated["platforms"] = platforms
    state = platforms.setdefault(platform, {})
    if not isinstance(state, dict):
        state = {}
        platforms[platform] = state
    disabled = _str_list(state.get("disabled"))
    enabled = _str_list(state.get("enabled"))
    for entry in entries:
        text = str(entry).strip()
        if not text:
            continue
        if enable:
            disabled = [item for item in disabled if item != text]
            if enabled and text not in enabled:
                enabled.append(text)
        else:
            if text not in disabled:
                disabled.append(text)
            enabled = [item for item in enabled if item != text]
    state["disabled"] = disabled
    state["enabled"] = enabled
    return updated


def is_known(entry: str) -> bool:
    """这个条目是不是一个**认得的**工具名 / toolset 名 / MCP 写法。

    存在的意义是让拼错的名字当场报错。静默接受 ``websearch`` 然后什么都不发生，
    是这类命令最坏的失败方式——用户以为关掉了，其实全开着。
    """
    text = entry.strip()
    if not text:
        return False
    if text in TOOLSETS or text in TOOL_TOOLSET:
        return True
    if text.startswith("mcp:") or text.startswith("mcp__"):
        return True  # 动态前缀：连接前无法枚举，只能放行
    if ":" in text:  # Hermes 的 server:tool 写法
        return True
    for prefix, _toolset in PREFIX_TOOLSETS:
        if text.startswith(prefix):
            return True
    return False


def suggest(entry: str, limit: int = 3) -> list[str]:
    """给拼错的条目找几个相近的候选（``difflib`` 是标准库，不引依赖）。"""
    from difflib import get_close_matches

    pool = list(TOOLSETS) + declared_tool_names()
    return get_close_matches(entry.strip(), pool, n=limit, cutoff=0.6)


def post_setup_snapshot(
    platform: str = DEFAULT_PLATFORM,
    config: Mapping[str, Any] | None = None,
    names: Iterable[str] | None = None,
) -> dict[str, Any]:
    """``orbit tools post-setup`` 的输出：一段可直接贴进 ``data/tools.json`` 的快照。

    照 Hermes ``tools post-setup`` 的定位——"给 dashboard / 脚本用的稳定、非交互产物"。
    Hermes 那个跑的是供应商安装钩子（本机无网络，装不了任何东西），所以 Orbit 这边
    落成**当前平台的启停快照**：把"此刻模型能看见什么"固化成一段 JSON，贴回配置即可
    复现同一份暴露面。这比打印一句"完成"有用得多。
    """
    pool = list(names) if names is not None else declared_tool_names()
    disabled, enabled = platform_state(config, platform)
    exposed = _config_exposure(pool, platform, config)
    exposed_set = set(exposed)
    return {
        "platforms": {
            platform: {
                "disabled": sorted(
                    {*disabled, *(name for name in pool if name not in exposed_set)}
                ),
                "enabled": enabled,
            }
        }
    }
