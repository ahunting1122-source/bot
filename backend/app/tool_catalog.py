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
        status="covered",
        orbit_tools=("browser_back",),
        reason="`app/browser.py::go_back`：`page.go_back()` 后 `settle`，返回新 URL + 标题；"
        "**没有上一页时如实报**（`go_back` 返回 None），不假装成功。"
        "**实测 chromium 能起**：本机 `%TEMP%` 对子进程不可写会让 playwright 的 mkdtemp 报 EPERM，"
        "测试用 autouse fixture 把 TMP/TEMP 指到可写目录后真跑本地站点，7 条真用例全过。",
    ),
    _t(
        "browser_get_images",
        "browser",
        "列出页面图片 URL + alt（喂给视觉模型）",
        status="covered",
        orbit_tools=("browser_get_images",),
        reason="`app/browser.py::images`：DOM 查询取 src/alt/尺寸，**相对 URL 转绝对**、"
        "**过滤 1x1 与 0x0 追踪像素**、支持 limit 与去重。纯逻辑 `normalize_images` 有 21 条离线用例。"
        "**如实边界**：产出的东西主要喂视觉模型，而本机没有可用的多模态调用链——"
        "提取本身是真的，消费方没有。",
    ),
    _t(
        "browser_vision",
        "browser",
        "截图做视觉检查（CAPTCHA / 视觉验证 / 复杂布局）",
        status="unavailable",
        reason="**缺的不是截图，是「让工具能调模型」这条通道**：截图那一半已经做完（`BrowserController.last_screenshot_data_url()` 返回 `data:image/png;base64,…`，且被 `tests/test_browser_lifecycle.py` 覆盖）；而 `app/tool_*.py` 里没有任何一处调模型（工具是纯函数式执行器，模型只在 agent 循环里）。原文写「要截图 + 多模态模型；本机无网络、无视觉 provider」——**「要截图」已不成立，「无网络」也被第 66 轮推翻**。**所以它是架构决定（工具要不要有模型通道），不是缺一条依赖。",
    ),
    _t(
        "browser_console",
        "browser",
        "读取页面 console 日志与 JS 报错",
        status="covered",
        orbit_tools=("browser_console",),
        reason="`app/browser.py::ConsoleLog`：监听器在**页面创建时**挂上"
        "（`_start_locked` / `new_page`，每页一份、防重复挂），**有界缓冲丢最旧**、"
        "同时收 `pageerror`、只留 level/text/at。"
        "**等到调工具时才挂就永远是空的**——这是这类工具最容易做错的地方。",
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
        "处理 alert / confirm / prompt 弹窗",
        status="covered",
        orbit_tools=("browser_dialog",),
        reason="`app/browser.py::DialogLog` + `_on_dialog`：状态机 + accept/dismiss + prompt 文本；"
        "**2 秒兜底 auto-dismiss**——Playwright 不处理 dialog 会冻结整个页面，"
        "所以不能无限期等模型决定，这是取舍，已写进 docstring。",
    ),
    _t(
        "browser_exec",
        "browser",
        "browser-use CLI 后端执行代码",
        status="unavailable",
        reason="要一个「在页面里跑任意 JS」的逃生口 —— 与另一条同类条目是同一类决定（都会绕过 ref 校验）。原文写「需要 browser-use 外部 CLI（本机不能装依赖）」，但「不能装」已被第 66 轮实测推翻；而即使装上，要不要一个绕过 ref 校验的入口仍是设计选择，不是依赖问题。",
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
        reason="需要 **xAI API 凭证**（`XAI_API_KEY`）；**网络是通的**（第 66 轮实测），缺的是凭证。",
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
        ("setup_mcp",),
        reason="**覆盖的是「配置」那一半**（本机真做得到）：增 / 删 / 启 / 停 "
        "`<data>/mcp.json`，走 MCPManager.save_servers 的字段校验与去重，"
        "`orbit mcp` 与运行时读同一份文件。**没覆盖**：从远端安装目录装包（要网络）、"
        "内联同意卡片（要运行时 UI）。实现见 app/tool_extra_impl.py，需装配层注册。",
    ),
    # ── delegation（2）──────────────────────────────────────────────────────
    _t(
        "delegate_task",
        "delegation",
        "在隔离上下文里派生 1..N 个子 agent（支持后台 / 编排）",
        # 第 37 轮：补了 `app/child_runner.py`（真的 `ChildRunner` 实现）之后，
        # 装配层（`main.py`）注入 `graph_child_factory(...)` 并在运行时注册。
        orbit_tools=("delegate_task",),
    ),
    _t(
        "agent_swarm",
        "delegation",
        "用 prompt 模板 + {{item}} 占位符批量起子 agent",
        # 同上：`app/swarm.py::run_plan` 的拓扑序/同层并发/失败隔离是真的，
        # 本轮补了子任务 runner 之后才真的能跑。
        # **仍缺**：Hermes 的限流相位（突发 5 个、之后每 700ms 起 1 个、429 退避）——
        # 见 MATRIX 5.14 的证据栏。
        orbit_tools=("agent_swarm",),
    ),
    # ── 媒体生成（4）────────────────────────────────────────────────────────
    _t(
        "image_generate",
        "media",
        "文生图（编辑/参考能力由 serving 侧动态渲染）",
        status="unavailable",
        reason="**需要图像生成 provider + 工具层到模型的通道**（`app/tool_*.py` 不调模型）。原文写「本机无网络」——**已被第 66 轮推翻**。",
    ),
    _t(
        "video_generate",
        "media",
        "文生视频（schema 在出口处动态重建）",
        status="unavailable",
        reason="**需要视频生成 provider + 工具层到模型的通道**（同上）。原文写「本机无网络」——**已被第 66 轮推翻**。",
    ),
    _t("xai_video_edit", "media", "用 xAI Imagine 编辑已有视频", status="unavailable", reason="需要 **xAI 凭证**；**网络是通的**（第 66 轮实测），缺的是凭证。"),
    _t("xai_video_extend", "media", "用 xAI Imagine 延长已有视频", status="unavailable", reason="需要 **xAI 凭证**；**网络是通的**（第 66 轮实测），缺的是凭证。"),
    # ── 视觉理解（2）────────────────────────────────────────────────────────
    _t(
        "vision_analyze",
        "vision",
        "把图片加载进对话（任意时刻用户提到图片都可调）",
        status="unavailable",
        reason="**缺一个视觉 provider，外加一小段接线** —— `GrowthContext` 里已经有 `settings`（模型配置），所以工具**能**建模型（`providers.build_chat_model(settings)`）；缺的是「把建好的模型传进工具」那一小段，**不是架构障碍**。**为什么不现在就实现**：本机没有视觉模型可验，只能拿 stub 测「消息构造出来了」，**而 stub 不会告诉我 provider 适配层有没有把 `image_url` 块丢掉** —— 那正是「接上了但没有」的形状（测试通过、真实使用静默失效）。**判据：一个功能如果没有能验证它的条件，实现它就是在制造一条假 ✅。",
    ),
    _t(
        "video_analyze",
        "vision",
        "用多模态模型分析 URL 或本地视频",
        status="unavailable",
        reason="**需要多模态 provider + 工具层到模型的通道**（同上，`app/tool_*.py` 不调模型）；另外要抽帧（本机有 `ffmpeg`）。原文写「本机无网络」——**已被第 66 轮推翻**。",
    ),
    # ── 语音合成（1）────────────────────────────────────────────────────────
    _t(
        "text_to_speech",
        "tts",
        "文本转语音，返回 MEDIA: 路径由平台投递",
        ("text_to_speech",),
        reason="**本机可用的是本地通道**：Windows SAPI（离线合成 wav，见 "
        "app/voice_local.py）与 `<data>/voice.json` 的命令通道；19 家云端 provider"
        "（edge / openai / elevenlabs / gemini / xai / minimax / mistral / deepinfra / "
        "neutts / kittentts / piper 里的云那半）一律如实报不可用并给出 install / requires / "
        "reason。实现见 app/tool_extra_impl.py，需装配层注册。",
    ),
    # ── 看板（14）──────────────────────────────────────────────────────────
    # 第 44 轮：看板 4 个已实现（`app/kanban.py` + `app/tool_kanban.py`）。
    # 其余 10 个仍未做（各自带一套状态机与并发语义），条目保持 unavailable。
    _t("kanban_show", "kanban", "读任务完整状态（title/body/assignee/父任务 handoff）", orbit_tools=("kanban_show",)),
    _t("kanban_list", "kanban", "列任务摘要，供 orchestrator 发现可路由的活", orbit_tools=("kanban_list",)),
    _t("kanban_create", "kanban", "建任务，可指定父任务与工作区", orbit_tools=("kanban_create",)),
    _t("kanban_comment", "kanban", "给任务追加跨 run 的持久评论", orbit_tools=("kanban_comment",)),
    _t("kanban_complete", "kanban", "标记完成 + 结构化 handoff 给下游", orbit_tools=("kanban_complete",)),
    _t("kanban_block", "kanban", "停下并按原因路由（dependency 等）", orbit_tools=("kanban_block",)),
    _t("kanban_unblock", "kanban", "解除阻塞（父任务全 done 则 ready）", orbit_tools=("kanban_unblock",)),
    _t("kanban_request_review", "kanban", "交审：等人工或另一个 agent 审", orbit_tools=("kanban_request_review",)),
    _t("kanban_request_changes", "kanban", "审阅结论：退回原实现者并给出必改项", orbit_tools=("kanban_request_changes",)),
    _t("kanban_heartbeat", "kanban", "长操作期间发「我还活着」信号", orbit_tools=("kanban_heartbeat",)),
    _t("kanban_link", "kanban", "给两个已存在任务加父子依赖边", orbit_tools=("kanban_link",)),
    _t("kanban_attach", "kanban", "内联 base64 挂附件到任务", orbit_tools=("kanban_attach",)),
    _t("kanban_attach_url", "kanban", "按 URL 挂附件（server 侧下载）", orbit_tools=("kanban_attach_url",)),
    _t("kanban_attachments", "kanban", "列任务附件（含磁盘绝对路径）", orbit_tools=("kanban_attachments",)),
    # ── 定时（1）───────────────────────────────────────────────────────────
    _t(
        "cronjob",
        "cron",
        "管理定时任务：从 prompt 和/或 skills 建任务、list 查看",
        ("cron_job",),
        reason="cron_job 只做建 / 列 / 停 / 删（复用 app/cron.py 的 CronStore、"
        "parse_schedule、validate_deliver）；**执行仍归 `orbit cron` 守护进程**，"
        "工具不会自己把任务跑起来。实现见 app/tool_extra_impl.py，需装配层注册。",
    ),
    # ── 桌面与窗口（16）────────────────────────────────────────────────────
    _t("desktop_preview", "desktop", "预览面板（app/tool_preview.py）", orbit_tools=("desktop_preview",)),
    _t("open_preview", "desktop", "预览面板（app/tool_preview.py）", orbit_tools=("open_preview",)),
    _t("read_preview", "desktop", "预览面板（app/tool_preview.py）", orbit_tools=("read_preview",)),
    _t("close_preview", "desktop", "预览面板（app/tool_preview.py）", orbit_tools=("close_preview",)),
    _t("drive_preview", "desktop", "驱动预览面板里的网页（登录/填表/点击）", status="unavailable", reason="同上；Orbit 的等价能力在 browser_* 里，不在预览面板上。"),
    _t("annotate_preview", "desktop", "预览面板（app/tool_preview.py）", orbit_tools=("annotate_preview",)),
    _t(
        "read_terminal",
        "desktop",
        "读聊天旁终端面板",
        ("process",),
        reason="**面板 UI 没有**，但「读一个后台进程的输出」这件事由 process poll 覆盖"
        "（日志落在 <data>/processes/<id>.log，退出码也一并给出）。这是能力覆盖，"
        "不是界面覆盖：Orbit 没有桌面终端面板。",
    ),
    _t(
        "close_terminal",
        "desktop",
        "关闭某个后台进程的只读终端标签页",
        status="unavailable",
        reason="Orbit 没有只读终端标签页。最接近的动作是 process kill，但那是**终止进程**"
        "（比关一个视图重得多），拿它当覆盖会把「看一眼就关掉」变成「把任务杀掉」。",
    ),
    _t("read_window_below", "desktop", "识别 Hermes 窗口正后方的应用窗口", status="unavailable", reason="需要窗口枚举与桌面权限，Orbit 没有这层。"),
    _t("focus_pane", "desktop", "聚焦某个桌面面板", status="unavailable", reason="需要桌面客户端运行时（预览面板 / 布局 / 导览 / 气泡），**Orbit 是本地服务 + CLI，没有这层 UI**"),
    _t("apply_layout", "desktop", "应用保存的布局预设", status="unavailable", reason="需要桌面客户端运行时（预览面板 / 布局 / 导览 / 气泡），**Orbit 是本地服务 + CLI，没有这层 UI**"),
    _t("tip", "desktop", "小箭头气泡指向某个 UI 元素", status="unavailable", reason="需要桌面客户端运行时（预览面板 / 布局 / 导览 / 气泡），**Orbit 是本地服务 + CLI，没有这层 UI**"),
    _t("tour", "desktop", "引导式导览（变暗 + 高亮 + popover）", status="unavailable", reason="需要桌面客户端运行时（预览面板 / 布局 / 导览 / 气泡），**Orbit 是本地服务 + CLI，没有这层 UI**"),
    _t("react_to_message", "desktop", "给消息加一个 emoji 反应", status="unavailable", reason="需要消息平台的行级 ID（iMessage tapback 式），Orbit 没有。"),
    _t("desktop_project", "desktop", "桌面 Project（命名工作区）create/switch", status="unavailable", reason="Orbit 的工作目录是单一授权目录，没有多命名工作区。"),
    _t("computer_use", "desktop", "通过 cua-driver 做通用桌面控制", status="unavailable", reason="需要 cua-driver 外部组件，未安装且不能装依赖。"),
    # ── 协作与消息（13）────────────────────────────────────────────────────
    _t(
        "send_message",
        "collab",
        "向已接入的消息平台发消息，或列可用 target",
        ("send_message",),
        reason="**第三轮做了**：`send_message` 工具（实现见 app/tool_extra_impl2.py）"
        "`action=list` 列出全部出站目标（21 个适配器 + 3 个未实现平台，逐条写清缺哪个"
        "凭证/环境变量），`action=send` 走 `app/gateway.py::GatewayService.deliver` —— "
        "适配器发送、失败重试、`deliveries.jsonl` 记账、平面事件都在那条链上。"
        "**本机离线可验的只有 console / file 两条旁路**（file 落到 `<data>/outbox.jsonl`），"
        "其余 17 家要凭证与网络，list 会如实报缺什么。上一轮卡住的那条归组问题本轮解决了："
        "装配层要给 `collab` 域新开一个 toolset（见 `pending_toolset_mappings()`），"
        "不再借 `notion` 的壳。",
    ),
    _t("discord", "collab", "Discord（app/tool_discord.py）", orbit_tools=("discord",)),
    _t("discord_admin", "collab", "Discord（app/tool_discord.py）", orbit_tools=("discord_admin",)),
    _t("feishu_doc_read", "collab", "飞书文档读取（app/tool_feishu.py）", orbit_tools=("feishu_doc_read",)),
    _t("feishu_drive_list_comments", "collab", "列飞书文档评论", status="unavailable", reason="需要飞书开放平台凭证与网络"),
    _t("feishu_drive_list_comment_replies", "collab", "列某条评论线程的全部回复", status="unavailable", reason="需要飞书开放平台凭证与网络"),
    _t("feishu_drive_reply_comment", "collab", "回复局部（引用文本）评论", status="unavailable", reason="需要飞书开放平台凭证与网络"),
    _t("feishu_drive_add_comment", "collab", "加整篇文档评论", status="unavailable", reason="需要飞书开放平台凭证与网络"),
    _t("yb_query_group_info", "collab", "查元宝群基本信息", status="unavailable", reason="需要元宝（腾讯）网关与凭证。"),
    _t("yb_query_group_members", "collab", "查群成员（用于 @某人）", status="unavailable", reason="需要元宝（腾讯）网关与凭证 + 网络"),
    _t("yb_send_dm", "collab", "群里给某人发私信", status="unavailable", reason="需要元宝（腾讯）网关与凭证 + 网络"),
    _t("yb_search_sticker", "collab", "按关键词搜内置贴纸", status="unavailable", reason="需要元宝（腾讯）网关与凭证 + 网络"),
    _t("yb_send_sticker", "collab", "发内置贴纸到当前会话", status="unavailable", reason="需要元宝（腾讯）网关与凭证 + 网络"),
    # ── 智能家居（4）───────────────────────────────────────────────────────
    _t("ha_list_entities", "home", "Home Assistant（app/tool_home.py）", orbit_tools=("ha_list_entities",)),
    _t("ha_get_state", "home", "Home Assistant（app/tool_home.py）", orbit_tools=("ha_get_state",)),
    _t("ha_list_services", "home", "Home Assistant（app/tool_home.py）", orbit_tools=("ha_list_services",)),
    _t("ha_call_service", "home", "Home Assistant（app/tool_home.py）", orbit_tools=("ha_call_service",)),
    # ── 元工具（8）─────────────────────────────────────────────────────────
    _t("todo", "meta", "会话任务清单（无参 = 读）", ("todo",), reason="Orbit 改成「每次调用回读完整清单」，不注入上下文（图节点是禁改区）。"),
    _t("clarify", "meta", "需要决策/澄清时向用户提一个或多个问题", ("clarify",), reason="Orbit 落成带序号的选择题（2-6 选项），不是多问题表单。"),
    _t(
        "compact",
        "meta",
        "触发上下文压缩（context_engine）",
        ("compact",),
        reason="**能真做的那一半做到了**：判定走 `app/compaction.py::plan`（纯函数："
        "该丢哪几组、压完剩多少 token），动手走装配层注入的 "
        "`SummaryService.rebuild(conversation_id)` —— 摘要是运行时真的会读的压缩产物"
        "（`app/context.py::build_volatile_tier(summary=…)`）。**边界**：改不了你正在看的"
        "那份消息列表（那在运行时手里，禁改区），影响的是**下一轮**的上下文。"
        "实现见 app/tool_extra_impl.py，需装配层注册。",
    ),
    _t(
        "context_usage",
        "meta",
        "查当前上下文占用",
        ("context_usage",),
        reason="数字来自**已落库的对话正文**（token 按 4 字符/token 估算）+ "
        "`app/capabilities.py` 的 context_window，判定用 "
        "`app/session_plane.py::maybe_compact`（纯函数）。**口径写进返回值**：不含系统提示、"
        "工具结果原文与本轮未落库的增量；窗口读不到时如实报「未知」并拒绝判定，不猜默认值。"
        "实现见 app/tool_extra_impl.py，需装配层注册。",
    ),
    _t("tool_search", "meta", "桥接工具：按自然语言/BM25 检索被延迟的工具", ("tool_search",), reason="Orbit 是关键词打分（名字 > 描述 > toolset），不是 BM25 词干检索。"),
    _t(
        "tool_describe",
        "meta",
        "桥接工具：取被延迟工具的完整 schema",
        ("tool_describe",),
        reason="配合 toolsets.defer_specs 的延迟披露（MATRIX 5.11）：Tier 2 在首屏只给"
        "「名字 + 一句描述」，参数表由它取。实现见 app/tool_extra_impl.py，需装配层注册。",
    ),
    _t(
        "tool_call",
        "meta",
        "桥接工具：调用被延迟的工具",
        ("tool_call",),
        reason="**本轮做了**：按名字走 `ToolRegistry.execute` 的真实分发链（校验 / 幂等 / "
        "错误包装都在那条链上）。三道闸写在实现里：未知工具给 tool_search 指路、"
        "`confirmation=True` 的工具**一律拒绝**（确认必须发生在对话里，不能从桥里绕）、"
        "不许调用自己。实现见 app/tool_extra_impl.py，需装配层注册。",
    ),
    _t(
        "message_agent",
        "meta",
        "bot 模式下给另一个 agent 发消息（条件注册）",
        ("send_message",),
        reason="**第三轮覆盖的是「给另一个 agent 发消息」这件事**，不是 Hermes 的 bot 模式："
        "`send_message` 的 `peer:<对端名>[/<agent>]` 目标走 `app/peer_client.py::PeerClient.dm`"
        "（`/peer/dm` 同步返回对端回复正文），对端从 `<data>/gateway/peers.json` 里按名字取。"
        "**边界**：对端要先 `orbit peer add` 登记、且网络可达；本机无网络，所以这条链的"
        "证据是 `httpx.MockTransport` 注入下的真跑用例（tests/test_tool_growth_round3.py）。"
        "Orbit 仍然没有 bot 模式，也没有多 agent 会话面 —— 覆盖的是投递动作，不是那个模式。",
    ),
)


def orbit_pool() -> tuple[str, ...]:
    """Orbit 当前**真实实现**的内置工具名（排序）。

    三个来源，缺一不可：

    * ``app.tools.LOCAL_TOOLS``——工具层自己的表（默认注册表就是它）；
    * ``app.toolsets.TOOL_TOOLSET`` 里的非 MCP 名字——浏览器 / 联网 / 连接器这些
      由别的模块注册的内置工具（``browser_*`` / ``web_*`` / ``notion_*`` /
      ``github_*`` / ``vault_login``）与运行时特殊处理的 ``ask_user``；
    * ``app.tool_extra_impl.EXTRA_TOOLS``——**第一轮新实现的两个工具**
      （``tool_describe`` / ``cron_job``）；
    * ``app.tool_extra_impl.GROWTH_TOOLS``——**第二轮新增的 5 个**；
    * ``app.tool_extra_impl2.ROUND3_TOOLS``——**第三轮新增的 5 个**。

    它们都有真实实现、真实 schema、真实分发，但要装配层各调一次注册函数
    （``register_extra_tools`` / ``register_growth_tools`` / ``register_round3_tools``）
    才会进默认注册表。三批分开列，是为了"哪一轮接了几个"能分开数、回滚也能分开回滚。

    为什么用声明表而不是 import ``app.browser`` / ``app.web`` 再读它们的 ``*_TOOLS``：
    那些模块会拖进 playwright / httpx，而这张对照表要被 CLI 与测试零成本导入。
    声明表与真实工具表的一致性由 ``tests/test_tool_catalog.py`` 钉住（对不上就红）。

    注意：这里回答的是"**有没有这个能力**"，不回答"此刻能不能用"（playwright 没装、
    token 没配时 ``browser_*`` 仍然在表里，但 ``toolsets.probe`` 会把它摘掉）。
    "能力存在但还没接线"这件事由 :func:`report` 单独列出来，不混进覆盖率里——否则
    "写了代码"会被读成"模型能用"。
    """
    from app.tools import LOCAL_TOOLS

    names = {tool.name for tool in LOCAL_TOOLS}
    names |= {name for name in toolsets.TOOL_TOOLSET if not name.startswith("mcp__")}
    names |= set(extra_tool_names())
    # MATRIX 5.14 第二轮新增的 5 个（`GROWTH_TOOLS`）。与上一轮那两个分开列，
    # 因为"上一轮接了什么"与"这一轮新增什么"要能分开数（回滚也要能分开回滚）。
    names |= set(growth_tool_names())
    # MATRIX 5.14 第三轮新增的 5 个（`ROUND3_TOOLS`）：同样是"实现了但还没接线"，
    # 与上面两批分开列的理由一致。
    names |= set(round3_tool_names())
    # MATRIX 5.14 第四轮新增的 2 个（`DELEGATE_TOOLS`：`delegate_task` / `agent_swarm`）。
    #
    # 这一批与前三批的**性质不同**：它们不是"实现了但没接线"，而是
    # **长期没有 runner 可用**（`ChildRunner` 协议零实现，`DelegateRunner()` 一跑就抛），
    # 本轮补了 `app/child_runner.py` 之后才注册得上。
    #
    # 为什么必须列进来：`orbit_coverage()` 用这张**静态表**，而这些工具是
    # **运行时注册**的。不列就会出现「模型看得见、报告说缺失」——
    # 那与「报告说覆盖了、模型看不见」是同一类不一致，只是方向相反。
    names |= set(delegate_tool_names())
    # MATRIX 5.14 第五轮：看板 4 个（`KANBAN_TOOLS`）。
    names |= set(kanban_tool_names())
    return tuple(sorted(names))


def extra_tool_names() -> tuple[str, ...]:
    """``app/tool_extra_impl.EXTRA_TOOLS`` 的名字（**上一轮的两个工具**）。

    单独一个函数是因为有两处要问同一个问题：``orbit_pool()``（算覆盖）与
    ``report()``（说清"实现了但还没接线"）。两处各写一份 import 迟早会漂。
    """
    try:
        from app.tool_extra_impl import EXTRA_TOOLS
    except Exception:  # noqa: BLE001 - 可选依赖缺失不该让覆盖表整个不可用
        return ()
    return tuple(tool.name for tool in EXTRA_TOOLS)


def growth_tool_names() -> tuple[str, ...]:
    """``app/tool_extra_impl.GROWTH_TOOLS`` 的名字（**本轮新增的 5 个**）。

    与 :func:`extra_tool_names` 分开的理由见 ``orbit_pool``：两批工具分开可数，
    既有的"上一轮两个"断言也不会被这一轮的增量打红。
    """
    try:
        from app.tool_extra_impl import GROWTH_TOOLS
    except Exception:  # noqa: BLE001
        return ()
    return tuple(tool.name for tool in GROWTH_TOOLS)


def kanban_tool_names() -> tuple[str, ...]:
    """第五轮（看板）那 4 个工具的名字。与前面几批同形：只读声明表，不 import 实现模块。"""
    from app.tool_kanban import KANBAN_TOOLS

    return tuple(tool.name for tool in KANBAN_TOOLS)


def delegate_tool_names() -> tuple[str, ...]:
    """第四轮（委派）那 2 个工具的名字。

    与 `extra_tool_names()` / `growth_tool_names()` / `round3_tool_names()` 同形：
    只读声明表，不 import 实现模块（`tool_delegate` 会拖进 pydantic 的 Args 与
    `app.tools`，而这张表要被 CLI 零成本导入）。
    """
    from app.tool_delegate import DELEGATE_TOOLS

    return tuple(tool.name for tool in DELEGATE_TOOLS)


def round3_tool_names() -> tuple[str, ...]:
    """``app.tool_extra_impl2.ROUND3_TOOLS`` 的名字（**第三轮新增的 5 个**）。

    为什么单独一个函数而不是把三批合并成一个常量：``report()`` 要能分别回答
    "第一轮 / 第二轮 / 第三轮各接了什么"，而测试也逐批钉名字表（见
    ``tests/test_tool_growth_round3.py``）。
    """
    try:
        from app.tool_extra_impl2 import ROUND3_TOOLS
    except Exception:  # noqa: BLE001 - 可选依赖缺失不该让覆盖表整个不可用
        return ()
    return tuple(tool.name for tool in ROUND3_TOOLS)


def pending_toolset_mappings() -> dict[str, str]:
    """第三轮那 5 个工具**还需要装配层补**的 toolset 映射（工具名 → toolset）。

    这张表存在的原因：``app/toolsets.py`` 本轮不归这里改，而 ``TOOL_TOOLSET`` 是
    "工具名 → toolset"的唯一真值源。把"该加哪几行"写成数据，于是

    * 装配层照抄即可（``orbit tools list`` 里它们才会归到正确的组）；
    * ``tests/test_tool_catalog.py`` 的登记门禁能**只对这 5 个名字**放行，
      别的名字缺席照样红 —— 放行范围写死在这里，不是一句"以后再说"。

    空字典 = 全部已登记（装配层补完之后这个函数仍可调用，返回值不再被用到）。
    """
    try:
        from app.tool_extra_impl2 import PENDING_TOOLSET_MAPPINGS
    except Exception:  # noqa: BLE001
        return {}
    return dict(PENDING_TOOLSET_MAPPINGS)


def registered_extra_tools() -> tuple[str, ...]:
    """其中**已经进了默认注册表**（``LOCAL_TOOLS``）的那些。目前恒为空。

    这个函数存在的意义就是让 ``report()`` 能如实说"实现了几个、接了几个"。
    """
    from app.tools import LOCAL_TOOLS

    local = {tool.name for tool in LOCAL_TOOLS}
    return tuple(name for name in extra_tool_names() if name in local)


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
    # "实现了" 与 "接上了" 是两件事，必须分开报：把写了代码算成模型能用，是这类
    # 覆盖表最常见的一种谎（模型调不动，用户却在报表里看到"已覆盖"）。
    extra = extra_tool_names()
    if extra:
        wired = registered_extra_tools()
        pending = tuple(name for name in extra if name not in wired)
        lines.append("")
        lines.append(
            f"本轮新实现 {len(extra)} 个工具（app/tool_extra_impl.py）："
            + "、".join(extra)
        )
        if pending:
            lines.append(
                "  其中**还没进默认注册表**（需装配层调一次 "
                "`register_extra_tools(registry)`）：" + "、".join(pending)
            )
        if wired:
            lines.append("  已进默认注册表：" + "、".join(wired))
    # 第三轮（MATRIX 5.14 第三轮）：又一批"实现了但还没接线"的工具。**单列一段**，
    # 理由同上：三批的进度要能分开回答，回滚也要能分开回滚。
    round3 = round3_tool_names()
    if round3:
        lines.append("")
        lines.append(
            f"第三轮新实现 {len(round3)} 个工具（app/tool_extra_impl2.py）：" + "、".join(round3)
        )
        mappings = pending_toolset_mappings()
        if mappings:
            lines.append(
                "  其中**还没在 `toolsets.TOOL_TOOLSET` 登记**（装配层加这几行即可）："
                + "、".join(f"{name} → {toolset}" for name, toolset in sorted(mappings.items()))
            )
    return "\n".join(lines)


__all__ = [
    "DOMAINS",
    "HERMES_TOOLS",
    "STATUSES",
    "Coverage",
    "DomainCoverage",
    "HermesTool",
    "extra_tool_names",
    "orbit_coverage",
    "orbit_pool",
    "pending_toolset_mappings",
    "registered_extra_tools",
    "report",
    "round3_tool_names",
]
