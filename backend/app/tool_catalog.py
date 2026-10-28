"""Hermes 内置工具的对照表与差距清单（MATRIX 5.14）。

## 这张表是干什么的

Hermes（CN 0.21.0-cn.18）对模型暴露 **94 个内置工具**，分 19 个功能域（口径见
``handoff/hermes-parity/research/02-tool-layer.md`` §0「94 的三口径」与 §4「全量清单」）。
Orbit 的工具层只覆盖其中一部分，于是有两个问题必须能**当场回答**，而不是靠翻笔记：

1. **还缺哪些**（按域看）——这就是 :func:`report` 的输出，它是给人看的缺口清单，
   不是给模型看的；
2. **缺的那个是"没做"还是"做不到"**——两者的后续动作完全不同。做不到的（要网络 /
   要浏览器 / 要平台凭证）**绝不注册成工具**：注册了模型就会去调，调了必然失败，
   而"调了才失败"是比"没有这个工具"坏得多的体验（模型会绕圈重试、把失败写进总结）。
   所以这类在表里标 ``orbit_status="unavailable"`` 并写清 ``reason``。

## 为什么用一张声明表而不是"扫注册表算覆盖"

覆盖关系不是同名映射：Hermes 的 ``read_file`` 在 Orbit 里叫 ``local_read_file``，
``terminal`` 叫 ``run_command``，``memory`` 一个工具在 Orbit 拆成三个
（``memory_save`` / ``memory_search`` / ``memory_delete``）。这层语义只能**显式写下来**，
自动推断（按前缀/按描述相似度）只会产出"看起来覆盖了"的假账。
所以表里每个 Hermes 工具都带 ``orbit_tools``：**覆盖它的 Orbit 工具名**（可以多个，也可以空）。

## 与 `app/toolsets.py` 的分工

``toolsets`` 管"已注册的工具归哪个 toolset、这轮暴露哪些"；本模块管"对照 Hermes，
还差哪些"。两者都读同一份事实（真实的工具表），但**结论不同**：``toolsets`` 的
``UNCLASSIFIED`` 是运行时门禁，本模块的 ``missing`` 是功能缺口清单。

本模块**不 import** ``app.tools`` 之外的重模块，且对 ``app.tools`` 的导入放在函数里
（:func:`orbit_pool`）：``app.tools`` 会拖进 pydantic / pypdf / sqlalchemy，
而这个模块要被 CLI 与测试零成本导入。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app import toolsets

#: 19 个功能域 → 一行中文说明。**顺序即 :func:`report` 的输出顺序**（照 Hermes 报告 §4）。
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

#: 允许的三种状态。
#: * ``covered`` —— Orbit 里已有能顶上的工具（``orbit_tools`` 非空且真的在工具表里）；
#: * ``unavailable`` —— 本机做不到（网络 / 浏览器 / 平台凭证 / 桌面运行时），**不注册**；
#: * ``todo`` —— 做得到、但这一轮没做（依赖现成模块，缺的是接线与验证）。
STATUSES = ("covered", "unavailable", "todo")


@dataclass(frozen=True)
class HermesTool:
    """一个 Hermes 内置工具，以及它在 Orbit 里的处境。"""

    name: str
    domain: str
    purpose: str
    #: 覆盖它的 Orbit 工具名（空元组 = 没有）。**必须是真实注册的工具名**，
    #: 测试会拿它跟工具表逐条对账——对不上就是这张表在说谎。
    orbit_tools: tuple[str, ...] = ()
    orbit_status: str = "covered"
    #: 为什么做不到 / 为什么还没做。``covered`` 也可以写（用来记"部分覆盖"的缺口）。
    reason: str = ""


def _t(
    name: str,
    domain: str,
    purpose: str,
    orbit_tools: tuple[str, ...] = (),
    *,
    status: str = "covered",
    reason: str = "",
) -> HermesTool:
    """构造一条对照记录（只为让下面那张长表读起来像清单，而不是一堆括号）。"""
    return HermesTool(
        name=name,
        domain=domain,
        purpose=purpose,
        orbit_tools=orbit_tools,
        orbit_status=status,
        reason=reason,
    )


#: Hermes 内置工具全表：**恰好 94 条**。
#:
#: 怎么抄的（可复现）：以 ``research/_tools_raw.json`` 里 AST 扫出来的 86 个
#: ``registry.register(name=…)`` 字面量为底，再补上报告 §0 明确列为"schema 在 tools/
#: 下、注册发生在冻结层"的 4 个（``send_message`` / ``open_preview`` / ``read_preview`` /
#: ``close_preview``）与 3 个 tool-search 桥接工具（``tool_search`` / ``tool_describe`` /
#: ``tool_call``），最后加条件注册的 ``message_agent``（``bot_mode_dm.py``）。
#: 86 + 4 + 3 + 1 = 94。**不含** MCP 动态工具（``mcp__<server>__<tool>``，运行时按
#: server 生成、没有固定名字）与插件工具。
HERMES_TOOLS: tuple[HermesTool, ...] = (
    # ── file（4）────────────────────────────────────────────────────────────
    _t(
        "read_file",
        "file",
        "带行号分页读文本文件（offset/limit），替代 cat/head/tail",
        ("local_read_file", "local_read_document"),
        reason="Hermes 的 fuzzy 匹配与 2000 行默认值已对齐；Orbit 没有它的 8 MB 以上分块读。",
    ),
    _t(
        "write_file",
        "file",
        "全量覆盖写文件，替代 echo/heredoc",
        ("local_write_file", "local_write_report"),
    ),
    _t(
        "patch",
        "file",
        "定向 find-and-replace 编辑（fuzzy 匹配 9 策略），替代 sed/awk",
        ("apply_patch",),
        reason="Orbit 用结构化补丁（*** Begin Patch）而不是 old/new 字符串；同样「只写被改的行」。",
    ),
    _t(
        "search_files",
        "file",
        "内容搜索 + 按名找文件（ripgrep 后端），替代 grep/rg/find/ls",
        ("local_search_files", "local_search_text", "local_list_files"),
        reason="三种输出形态（content/files/count）已实现；后端是纯 Python 遍历，不是 ripgrep。",
    ),
    # ── terminal（2）────────────────────────────────────────────────────────
    _t(
        "terminal",
        "terminal",
        "在 8 种后端执行命令（local / docker / ssh / modal / …）",
        ("run_command",),
        reason="Orbit 只有 local 后端（app/terminal.py 留了抽象，未接容器后端）。",
    ),
    _t(
        "process",
        "terminal",
        "管理后台进程：list / poll / kill / 等",
        ("process",),
        reason="Orbit 只支持本进程起过的后台任务；登记表在内存里，重启即清空（日志留在盘上）。",
    ),
    # ── code_execution（1）──────────────────────────────────────────────────
    _t(
        "execute_code",
        "code",
        "模型写 Python 脚本，脚本内通过 RPC 调工具（Programmatic Tool Calling）",
        ("code_execution",),
        reason="Orbit 执行代码，但脚本内没有 RPC 桥（不能从代码里回调工具）。",
    ),
    # ── browser（13）────────────────────────────────────────────────────────
    _t("browser_navigate", "browser", "打开 URL 并建立浏览器会话", ("browser_navigate",)),
    _t(
        "browser_snapshot",
        "browser",
        "无障碍树文本快照，产出可点的 ref ID",
        ("browser_observe",),
    ),
    _t("browser_click", "browser", "按 ref ID 点击元素", ("browser_click",)),
    _t("browser_type", "browser", "按 ref ID 输入文本（先清空）", ("browser_fill",)),
    _t("browser_press", "browser", "按键 / 快捷键", ("browser_press",)),
    _t("browser_scroll", "browser", "上下滚动页面", ("browser_scroll",)),
    _t(
        "browser_back",
        "browser",
        "后退到上一页",
        status="unavailable",
        reason="需要页面历史栈；Orbit 的浏览器层只暴露观察/点击/填写，且本机没有 chromium 可验证。",
    ),
    _t(
        "browser_get_images",
        "browser",
        "列出页面图片 URL + alt（喂给视觉模型）",
        status="unavailable",
        reason="产出的东西只有视觉模型能用，本机没有可用的多模态调用链。",
    ),
    _t(
        "browser_vision",
        "browser",
        "截图做视觉检查（CAPTCHA / 视觉验证 / 复杂布局）",
        status="unavailable",
        reason="要截图 + 多模态模型；本机无网络、无视觉 provider。",
    ),
    _t(
        "browser_console",
        "browser",
        "读 console 输出与 JS 未捕获错误",
        status="unavailable",
        reason="需要订阅页面 console 事件；本机没有 chromium，无法自证实现正确。",
    ),
    _t(
        "browser_cdp",
        "browser",
        "原始 Chrome DevTools Protocol 逃生口",
        status="unavailable",
        reason="Orbit 的浏览器层刻意只暴露高层动作，不做协议逃生口（它会绕过 ref 校验）。",
    ),
    _t(
        "browser_dialog",
        "browser",
        "响应原生 JS 对话框（alert/confirm/prompt/beforeunload）",
        status="unavailable",
        reason="需要页面级 dialog 事件处理；本机没有 chromium，无法自证实现正确。",
    ),
    _t(
        "browser_exec",
        "browser",
        "browser-use CLI 后端执行代码",
        status="unavailable",
        reason="需要 browser-use 这个外部 CLI（未安装，且本机不能装依赖）。",
    ),
    # ── web（2）─────────────────────────────────────────────────────────────
    _t("web_search", "web", "网页搜索，最多 5 条结果", ("web_search",)),
    _t("web_extract", "web", "把 URL 抽成 markdown / 纯文本（支持 PDF）", ("web_extract",)),
    # ── 专项检索（2）────────────────────────────────────────────────────────
    _t(
        "x_search",
        "search",
        "用 xAI 内置 X Search 搜 X/Twitter 帖子、主页、线程",
        status="unavailable",
        reason="需要 xAI API 与凭证，本机无网络。",
    ),
    _t(
        "session_search",
        "search",
        "搜历史会话消息（FTS），或在某场会话里翻页",
        ("session_search",),
        reason="Orbit 用 LIKE 扫描 + 锚点窗口，不是 FTS5 索引；语义（搜索/展开/浏览）已对齐。",
    ),
    # ── memory（1）──────────────────────────────────────────────────────────
    _t(
        "memory",
        "memory",
        "保存跨会话高信号事实（一次调用批量提交所有变更）",
        ("memory_save", "memory_search", "memory_delete"),
        reason="Orbit 拆成三个工具，且写入要过 M2 显式指令判据 + 写入门禁（更严）。",
    ),
    # ── skills（3）──────────────────────────────────────────────────────────
    _t("skills_list", "skills", "列出可用技能（name + description）", ("skill_list",)),
    _t("skill_view", "skills", "加载技能全文或链接文件（references/templates/scripts）", ("skill_load",)),
    _t(
        "skill_manage",
        "skills",
        "创建/更新/删除技能（= 程序性记忆）",
        ("skill_save",),
        reason="创建/更新已覆盖；**删除没做成工具**（走 `orbit skills` 命令面）——删技能是不可逆动作，"
        "不适合由模型在一次工具调用里完成。",
    ),
    # ── mcp（1）─────────────────────────────────────────────────────────────
    _t(
        "setup_mcp",
        "mcp",
        "以同意卡片提议一个 MCP server（装目录项 / 重启被禁 server / 自定义命令）",
        status="unavailable",
        reason="需要安装目录与网络下载，外加内联同意卡片（运行时 UI）；工具层做不到这两件事。",
    ),
    # ── delegation（2）──────────────────────────────────────────────────────
    _t(
        "delegate_task",
        "delegation",
        "在隔离上下文里派生 1..N 个子 agent（支持后台 / 编排）",
        status="unavailable",
        reason="委派编排在图运行时里（app/graph 是禁改区）；工具层单方面起子 agent 无法自证"
        "（谁回收、谁限并发、失败怎么回传都归图管）。",
    ),
    _t(
        "agent_swarm",
        "delegation",
        "用 prompt 模板 + {{item}} 占位符批量起子 agent",
        status="unavailable",
        reason="同上，且要并发调度器与限流（Hermes 的 swarm_scheduler）。",
    ),
    # ── 媒体生成（4）────────────────────────────────────────────────────────
    _t(
        "image_generate",
        "media",
        "文生图（编辑/参考能力由 serving 侧动态渲染）",
        status="unavailable",
        reason="需要图像模型 provider，本机无网络。",
    ),
    _t(
        "video_generate",
        "media",
        "文生视频（schema 在出口处动态重建）",
        status="unavailable",
        reason="需要视频模型 provider，本机无网络。",
    ),
    _t("xai_video_edit", "media", "用 xAI Imagine 编辑已有视频", status="unavailable", reason="需要 xAI 凭证，本机无网络。"),
    _t("xai_video_extend", "media", "用 xAI Imagine 延长已有视频", status="unavailable", reason="需要 xAI 凭证，本机无网络。"),
    # ── 视觉理解（2）────────────────────────────────────────────────────────
    _t(
        "vision_analyze",
        "vision",
        "把图片加载进对话（任意时刻用户提到图片都可调）",
        status="unavailable",
        reason="需要多模态模型 provider，本机无网络。",
    ),
    _t(
        "video_analyze",
        "vision",
        "用多模态模型分析 URL 或本地视频",
        status="unavailable",
        reason="需要多模态模型 provider，本机无网络。",
    ),
    # ── 语音合成（1）────────────────────────────────────────────────────────
    _t(
        "text_to_speech",
        "tts",
        "文本转语音，返回 MEDIA: 路径由平台投递",
        status="unavailable",
        reason="需要 TTS provider（elevenlabs / openai / gemini / xai），本机无网络。",
    ),
    # ── 看板（14）──────────────────────────────────────────────────────────
    _t("kanban_show", "kanban", "读任务完整状态（title/body/assignee/父任务 handoff）", status="unavailable", reason="kanban 是 Hermes 的服务端看板（多 worker 认领/交接），Orbit 没有对应服务端。"),
    _t("kanban_list", "kanban", "列任务摘要，供 orchestrator 发现可路由的活", status="unavailable", reason="同上。"),
    _t("kanban_create", "kanban", "建任务，可指定父任务与工作区", status="unavailable", reason="同上。"),
    _t("kanban_comment", "kanban", "给任务追加跨 run 的持久评论", status="unavailable", reason="同上。"),
    _t("kanban_complete", "kanban", "标记完成 + 结构化 handoff 给下游", status="unavailable", reason="同上。"),
    _t("kanban_block", "kanban", "停下并按原因路由（dependency 等）", status="unavailable", reason="同上。"),
    _t("kanban_unblock", "kanban", "解除阻塞（父任务全 done 则 ready）", status="unavailable", reason="同上。"),
    _t("kanban_request_review", "kanban", "交审：等人工或另一个 agent 审", status="unavailable", reason="同上。"),
    _t("kanban_request_changes", "kanban", "审阅结论：退回原实现者并给出必改项", status="unavailable", reason="同上。"),
    _t("kanban_heartbeat", "kanban", "长操作期间发「我还活着」信号", status="unavailable", reason="同上。"),
    _t("kanban_link", "kanban", "给两个已存在任务加父子依赖边", status="unavailable", reason="同上。"),
    _t("kanban_attach", "kanban", "内联 base64 挂附件到任务", status="unavailable", reason="同上。"),
    _t("kanban_attach_url", "kanban", "按 URL 挂附件（server 侧下载）", status="unavailable", reason="同上，且要 server 侧下载能力。"),
    _t("kanban_attachments", "kanban", "列任务附件（含磁盘绝对路径）", status="unavailable", reason="同上。"),
    # ── 定时（1）───────────────────────────────────────────────────────────
    _t(
        "cronjob",
        "cron",
        "管理定时任务：从 prompt 和/或 skills 建任务、list 查看",
        status="todo",
        reason="app/cron.py 已有 create_job / CronStore / CronRunner，做得到；本轮没接工具层——"
        "投递目标（deliver）要先跟平台配置对账，否则会出现「建了但没人跑」的假象。",
    ),
    # ── 桌面与窗口（16）────────────────────────────────────────────────────
    _t("desktop_preview", "desktop", "预览面板（open/read/close 复合动作）", status="unavailable", reason="需要桌面客户端运行时（预览面板），Orbit 是本地服务 + CLI。"),
    _t("open_preview", "desktop", "打开聊天旁的预览面板", status="unavailable", reason="同上。"),
    _t("read_preview", "desktop", "读预览面板内容", status="unavailable", reason="同上。"),
    _t("close_preview", "desktop", "关闭预览面板或其中一个标签页", status="unavailable", reason="同上。"),
    _t("drive_preview", "desktop", "驱动预览面板里的网页（登录/填表/点击）", status="unavailable", reason="同上；Orbit 的等价能力在 browser_* 里，不在预览面板上。"),
    _t("annotate_preview", "desktop", "在预览页留持久标记", status="unavailable", reason="同上。"),
    _t("read_terminal", "desktop", "读聊天旁终端面板", status="unavailable", reason="同上；Orbit 用 process poll 读后台进程日志，没有终端面板。"),
    _t("close_terminal", "desktop", "关闭某个后台进程的只读终端标签页", status="unavailable", reason="同上；等价动作是 process kill。"),
    _t("read_window_below", "desktop", "识别 Hermes 窗口正后方的应用窗口", status="unavailable", reason="需要窗口枚举与桌面权限，Orbit 没有这层。"),
    _t("focus_pane", "desktop", "聚焦某个桌面面板", status="unavailable", reason="同上。"),
    _t("apply_layout", "desktop", "应用保存的布局预设", status="unavailable", reason="同上。"),
    _t("tip", "desktop", "小箭头气泡指向某个 UI 元素", status="unavailable", reason="同上。"),
    _t("tour", "desktop", "引导式导览（变暗 + 高亮 + popover）", status="unavailable", reason="同上。"),
    _t("react_to_message", "desktop", "给消息加一个 emoji 反应", status="unavailable", reason="需要消息平台的行级 ID（iMessage tapback 式），Orbit 没有。"),
    _t("desktop_project", "desktop", "桌面 Project（命名工作区）create/switch", status="unavailable", reason="Orbit 的工作目录是单一授权目录，没有多命名工作区。"),
    _t("computer_use", "desktop", "通过 cua-driver 做通用桌面控制", status="unavailable", reason="需要 cua-driver 外部组件，未安装且不能装依赖。"),
    # ── 协作与消息（13）────────────────────────────────────────────────────
    _t("send_message", "collab", "向已接入的消息平台发消息，或列可用 target", status="unavailable", reason="需要平台接入（Telegram/Discord 等）与凭证，本机无网络。"),
    _t("discord", "collab", "Discord 核心动作集（读消息/发消息/搜）", status="unavailable", reason="需要 DISCORD_BOT_TOKEN 与网络。"),
    _t("discord_admin", "collab", "Discord 管理动作集（频道/角色/权限）", status="unavailable", reason="同上。"),
    _t("feishu_doc_read", "collab", "读飞书/Lark 文档全文为纯文本", status="unavailable", reason="需要飞书开放平台凭证与网络。"),
    _t("feishu_drive_list_comments", "collab", "列飞书文档评论", status="unavailable", reason="同上。"),
    _t("feishu_drive_list_comment_replies", "collab", "列某条评论线程的全部回复", status="unavailable", reason="同上。"),
    _t("feishu_drive_reply_comment", "collab", "回复局部（引用文本）评论", status="unavailable", reason="同上。"),
    _t("feishu_drive_add_comment", "collab", "加整篇文档评论", status="unavailable", reason="同上。"),
    _t("yb_query_group_info", "collab", "查元宝群基本信息", status="unavailable", reason="需要元宝（腾讯）网关与凭证。"),
    _t("yb_query_group_members", "collab", "查群成员（用于 @某人）", status="unavailable", reason="同上。"),
    _t("yb_send_dm", "collab", "群里给某人发私信", status="unavailable", reason="同上。"),
    _t("yb_search_sticker", "collab", "按关键词搜内置贴纸", status="unavailable", reason="同上。"),
    _t("yb_send_sticker", "collab", "发内置贴纸到当前会话", status="unavailable", reason="同上。"),
    # ── 智能家居（4）───────────────────────────────────────────────────────
    _t("ha_list_entities", "home", "列实体，可按 domain/area 过滤", status="unavailable", reason="需要 Home Assistant 实例地址与长期令牌，本机无网络。"),
    _t("ha_get_state", "home", "单个实体的详细状态 + 全部属性", status="unavailable", reason="同上。"),
    _t("ha_list_services", "home", "列可用服务（动作）", status="unavailable", reason="同上。"),
    _t("ha_call_service", "home", "调服务控制设备", status="unavailable", reason="同上。"),
    # ── 元工具（8）─────────────────────────────────────────────────────────
    _t("todo", "meta", "会话任务清单（无参 = 读）", ("todo",), reason="Orbit 改成「每次调用回读完整清单」，不注入上下文（图节点是禁改区）。"),
    _t("clarify", "meta", "需要决策/澄清时向用户提一个或多个问题", ("clarify",), reason="Orbit 落成带序号的选择题（2-6 选项），不是多问题表单。"),
    _t(
        "compact",
        "meta",
        "触发上下文压缩（context_engine）",
        status="unavailable",
        reason="压缩在 agent 运行时被拦截（Hermes 也是 dispatch 层拦），工具层注册只会是个 stub。",
    ),
    _t(
        "context_usage",
        "meta",
        "查当前上下文占用",
        status="unavailable",
        reason="上下文占用只有运行时知道；工具层算不出来，算出来也是假的。",
    ),
    _t("tool_search", "meta", "桥接工具：按自然语言/BM25 检索被延迟的工具", ("tool_search",), reason="Orbit 是关键词打分（名字 > 描述 > toolset），不是 BM25 词干检索。"),
    _t(
        "tool_describe",
        "meta",
        "桥接工具：取被延迟工具的完整 schema",
        status="unavailable",
        reason="Orbit 把全部工具 schema 一次暴露，没有「被延迟的工具」可描述；注册它只会诱导模型绕一层。",
    ),
    _t(
        "tool_call",
        "meta",
        "桥接工具：调用被延迟的工具",
        status="unavailable",
        reason="同上：没有延迟工具可调，多一层桥只会多一层失败点。",
    ),
    _t(
        "message_agent",
        "meta",
        "bot 模式下给另一个 agent 发消息（条件注册）",
        status="unavailable",
        reason="只在 Hermes 的 bot 模式注册；Orbit 没有 bot 模式与多 agent 会话面。",
    ),
)


def orbit_pool() -> tuple[str, ...]:
    """Orbit 当前**真实注册**的内置工具名（排序）。

    两个来源，缺一不可：

    * ``app.tools.LOCAL_TOOLS``——工具层自己的表（本轮改动的主要对象）；
    * ``app.toolsets.TOOL_TOOLSET`` 里的非 MCP 名字——浏览器 / 联网 / 连接器这些
      由别的模块注册的内置工具（``browser_*`` / ``web_*`` / ``notion_*`` /
      ``github_*`` / ``vault_login``）与运行时特殊处理的 ``ask_user``。

    为什么用声明表而不是 import ``app.browser`` / ``app.web`` 再读它们的 ``*_TOOLS``：
    那些模块会拖进 playwright / httpx，而这张对照表要被 CLI 与测试零成本导入。
    声明表与真实工具表的一致性由 ``tests/test_tool_catalog.py`` 钉住（对不上就红）。

    注意：这里回答的是"**有没有这个能力**"，不回答"此刻能不能用"（playwright 没装、
    token 没配时 ``browser_*`` 仍然在表里，但 ``toolsets.probe`` 会把它摘掉）。
    """
    from app.tools import LOCAL_TOOLS

    names = {tool.name for tool in LOCAL_TOOLS}
    names |= {name for name in toolsets.TOOL_TOOLSET if not name.startswith("mcp__")}
    return tuple(sorted(names))


@dataclass(frozen=True)
class DomainCoverage:
    """一个功能域的覆盖情况。"""

    domain: str
    label: str
    total: int
    covered: tuple[str, ...]
    missing: tuple[str, ...]

    @property
    def rate(self) -> float:
        """覆盖率（0..1）。``total`` 为 0 时按 1.0——没有东西可缺。"""
        return 1.0 if self.total == 0 else len(self.covered) / self.total


@dataclass(frozen=True)
class Coverage:
    """一次对账的完整结论。三组**互不重叠**：covered / missing / orbit_only。"""

    #: 参与对账的 Orbit 工具名（默认 :func:`orbit_pool`）。
    pool: tuple[str, ...]
    #: Hermes 工具名 → 覆盖它的 Orbit 工具名（只记真的在 pool 里的）。
    covered: dict[str, tuple[str, ...]]
    #: Hermes 有、Orbit 没有的（**含**做不到的，两者靠 :data:`HERMES_TOOLS` 的
    #: ``orbit_status`` 区分：``missing`` 只说"缺"，``report`` 才说"为什么缺"）。
    missing: tuple[str, ...]
    #: Orbit 有、Hermes 没有对应物的工具（本地特有，不是缺口）。
    orbit_only: tuple[str, ...]
    #: 按域拆开的覆盖情况（顺序同 :data:`DOMAINS`）。
    domains: dict[str, DomainCoverage]

    @property
    def total(self) -> int:
        """Hermes 工具总数（94）。"""
        return len(self.covered) + len(self.missing)

    @property
    def covered_count(self) -> int:
        return len(self.covered)


def orbit_coverage(pool: Iterable[str] | None = None) -> Coverage:
    """把 :data:`HERMES_TOOLS` 与 Orbit 的工具池逐条对账。

    ``pool`` 留空 = :func:`orbit_pool()`。允许显式传入是为了两件事：
    测试能构造"故意缺几个"的场景；将来若要把插件工具算进来，不必改这里。

    "覆盖"的判定很窄：**``orbit_tools`` 里至少有一个名字真的在 pool 里**。
    不按名字相似度、不按描述、不按前缀推断——那样能凑出漂亮的数字，但模型真正
    调用时会发现工具不存在，比诚实地报"缺"糟糕得多。
    """
    names = tuple(sorted(set(pool if pool is not None else orbit_pool())))
    present = set(names)

    covered: dict[str, tuple[str, ...]] = {}
    missing: list[str] = []
    mapped: set[str] = set()
    for tool in HERMES_TOOLS:
        hits = tuple(name for name in tool.orbit_tools if name in present)
        mapped |= set(hits)
        if hits:
            covered[tool.name] = hits
        else:
            missing.append(tool.name)

    domains: dict[str, DomainCoverage] = {}
    for domain, label in DOMAINS.items():
        members = [tool for tool in HERMES_TOOLS if tool.domain == domain]
        domains[domain] = DomainCoverage(
            domain=domain,
            label=label,
            total=len(members),
            covered=tuple(tool.name for tool in members if tool.name in covered),
            missing=tuple(tool.name for tool in members if tool.name not in covered),
        )

    return Coverage(
        pool=names,
        covered=covered,
        missing=tuple(missing),
        orbit_only=tuple(name for name in names if name not in mapped),
        domains=domains,
    )


def _status_of(name: str) -> str:
    """Hermes 工具名 → 状态（缺的那部分要靠它说清"做不到"还是"还没做"）。"""
    for tool in HERMES_TOOLS:
        if tool.name == name:
            return tool.orbit_status
    return "unknown"


def _reason_of(name: str) -> str:
    for tool in HERMES_TOOLS:
        if tool.name == name:
            return tool.reason
    return ""


def report(coverage: Coverage | None = None) -> str:
    """给人看的缺口清单：**19 个域各自覆盖多少、缺哪些、为什么缺**。

    为什么是字符串而不是结构化数据：这份东西的读者是人（review 时看"这一轮到底补了
    什么、还差什么"）。要机器可读的形态就直接用 :func:`orbit_coverage` 的返回值，
    两者不要混——把给人看的排版塞进 dict 只会让两边都难用。

    每个缺的工具都带状态标记：``[做不到]`` / ``[没做]``。没有这个标记的缺口清单
    会让人误以为"再排一期就能补齐"，而其中大部分在本机永远补不上（要网络 / 浏览器 /
    平台凭证）。
    """
    data = coverage if coverage is not None else orbit_coverage()
    lines = [
        f"Hermes 内置工具 {data.total} 个 / {len(DOMAINS)} 个功能域；"
        f"Orbit 内置工具 {len(data.pool)} 个",
        f"覆盖 {data.covered_count} / {data.total}"
        f"（{data.covered_count / data.total:.0%}），"
        f"缺 {len(data.missing)}，Orbit 独有 {len(data.orbit_only)}",
        "",
    ]
    for domain, info in data.domains.items():
        mark = "✓" if not info.missing else "·"
        lines.append(f"{mark} {domain:<9} {info.label}：{len(info.covered)}/{info.total}")
        for name in info.missing:
            status = _status_of(name)
            label = "做不到" if status == "unavailable" else ("没做" if status == "todo" else "缺")
            reason = _reason_of(name)
            lines.append(f"    [{label}] {name}" + (f" —— {reason}" if reason else ""))
    lines.append("")
    if data.orbit_only:
        lines.append("Orbit 独有（Hermes 无对应物，不算缺口）：" + "、".join(data.orbit_only))
    return "\n".join(lines)


__all__ = [
    "DOMAINS",
    "HERMES_TOOLS",
    "STATUSES",
    "Coverage",
    "DomainCoverage",
    "HermesTool",
    "orbit_coverage",
    "orbit_pool",
    "report",
]
