import asyncio
import functools
import hashlib
import json
import logging
import os
import subprocess
import time
import uuid
from dataclasses import dataclass, replace
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pypdf import PdfReader
from sqlalchemy import select

from app import (
    approval_channel,
    approvals,
    budget,
    clarify_delivery,
    code_exec,
    egress,
    hooks,
    memory_plugin_bridge,
    memory_write,
    terminal,
    tirith,
    toolsets,
)
from app.config import GRANTABLE_TOOLS, Settings, load_grants
from app.storage import Artifact, Memory, Message, Operation, Store, Task, serialize

log = logging.getLogger("orbit.tools")

#: retriever_search 每任务调用上限；超限只回提示不再检索（防"再查一次"替代思考）
RETRIEVER_CALL_LIMIT = 4
#: 工具执行并发闸。单 worker 串行下现在天然不超，是给 MCP 工具并发上升预留的上限：
#: 资源面最窄的是浏览器（一个进程一个页面），并发放开时必须先有闸。
TOOL_CONCURRENCY = 4
#: MCP 动态工具的名字前缀。遮蔽门禁对它们**整体豁免**：动态发现的工具在重连/换实现后
#: 同名不同源是常态，走 override 流程只会把正常刷新卡死（照 Hermes 的 `mcp-*` 豁免）。
MCP_TOOL_PREFIX = "mcp__"
#: 结果里携带"这条结果出自哪个工具"的内部字段。存在的唯一理由：`as_tool_text`
#: 需要工具名才能决定要不要包"不可信数据"标记，而它的调用点（`app/graph/nodes.py`）
#: 是禁改区、拿不到名字。序列化前会被剥掉，**不会出现在模型上下文里**。
TOOL_SOURCE_KEY = "_tool"
#: 结果里携带"这条结果该 spill 到哪"的内部字段（MATRIX 5.7）。
#:
#: 为什么用同样的手法（挂在结果 dict 上，序列化前剥掉）：`as_tool_text` 是唯一能
#: 拿到**完整正文**的地方，而它的调用点（禁改的 `app/graph/nodes.py`）只传结果本身，
#: 既没有 `task_id` 也没有 `data_dir`。挂在 dict 上比开一个模块级全局变量安全——
#: 并发/交错执行时全局变量会串味，而每条结果的上下文跟着结果自己走。
TOOL_SPILL_KEY = "_spill_ctx"


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Empty(Args):
    pass


#: 记忆写入的显式指令判据（M2：写入门槛 = 仅显式指令）。
#:
#: 为什么要在 harness 侧判而不是只写提示词：提示词是"建议"，模型在长任务里会
#: 自我发挥——实测真库 5 条同义记忆就是这么攒出来的（模型觉得"这个值得记"就写了）。
#: 所以这里做**确定性拦截**：只有用户自己说的话里出现了下列祈使标记，`memory_save`
#: 才放行；否则直接拒绝并把理由回给模型，让它继续干活而不写库。
#:
#: 判据只扫**最近一条用户消息**，不扫整个会话：用户三小时前说过"记住"不代表现在还授权。
MEMORY_INTENT_MARKERS = (
    "记住",
    "记下来",
    "记一下",
    "帮我记",
    "帮我记住",
    "请记",
    "要记住",
    "得记住",
    "别忘了",
    "不要忘",
    "别忘记",
    "不要忘记",
    "存进记忆",
    "存入记忆",
    "存到记忆",
    "加入记忆",
    "加到记忆",
    "存为记忆",
    "存成记忆",
    "写进记忆",
    "写进长期记忆",
    "存进长期记忆",
    "存入长期记忆",
    "长期记住",
    "永久记住",
    "一直记住",
    "以后都记住",
    "记到长期",
    "remember this",
    "please remember",
    "keep in mind",
    "keep this in mind",
    "save to memory",
    "note this down",
)

#: 只有祈使标记不够："记住"前面若带否定/疑问，语义完全相反。
#: 例："不用记住这个" / "你为什么没记住" / "记住了吗"。这些都**不**算授权写入。
MEMORY_INTENT_VETOES = (
    "不用记",
    "不要记",
    "别记",
    "不必记",
    "无需记",
    "没记住",
    "没记住吗",
    "记住了吗",
    "记不住",
    "忘掉",
    "忘掉吧",
    "忘记吧",
    "删掉这条",
    "删掉记忆",
    "不用保存",
    "不要保存",
    "别保存",
    "不用存",
    "不要存",
    "别存",
)


class MemoryWriteRefused(RuntimeError):
    """用户没有显式要求记住，`memory_save` 被 harness 拦下。

    单独一类异常是为了让分发点把它落成 `status="refused"`，而不是混进通用
    `status="error"`——对模型来说两者含义不同："被拒绝"表示别再试了，
    "出错"表示换个参数可能就能成。


    **`refused` 是非终态**：它记的是"以当时那轮上下文，不该写入"，而不是
    "这次调用失败了"。所以 `Operation.status` 要落 WAITING 而不是 DONE——
    若落 DONE，幂等短路会把这条拒绝永久缓存，之后即使用户补了「记住」，
    同一 operation_id 也永远拿不回成功结果（节点重放 / 断点恢复都会踩到）。
    """


class ExecutionRefused(RuntimeError):
    """执行前裁决拒绝执行：`tirith` 组合扫描、`egress` 出站策略。

    为什么用异常而不是 `return {"status": "refused", ...}`：`ToolRegistry.execute`
    会把本地工具的 dict 返回值统一包成 `{"status": "ok", "data": ...}`，把拒绝态
    **覆盖掉**——`MemoryWriteRefused` 已经踩过这个坑（见 `skill_save` 里那句
    "必须 raise（不能返回 status）"）。走异常是唯一能让拒绝态活着到模型眼前的路径。

    与 `MemoryWriteRefused` 分开成两个类，是因为分发点给的载荷不同：
    记忆拒绝要带 `saved`（"什么都没存"），执行拒绝只需要 `error`。
    """


def memory_write_allowed(recent_user_text: str) -> tuple[bool, str]:
    """判断最近一条用户消息是否给出了显式的记忆写入指令。

    返回 `(是否放行, 拒绝理由)`。放行时理由为空串。
    """
    text = (recent_user_text or "").strip().casefold()
    if not text:
        return False, "还没有收到用户的记忆写入指令。"
    for veto in MEMORY_INTENT_VETOES:
        if veto.casefold() in text:
            return False, f"用户消息里出现了「{veto}」，按其字面意思不应写入记忆。"
    for marker in MEMORY_INTENT_MARKERS:
        if marker.casefold() in text:
            return True, ""
    return False, (
        "用户这次没有明确要求记住任何东西。长期记忆只在用户显式说「记住……」时写入；"
        "请继续完成任务本身，不要自行把偏好或结论沉淀成记忆。"
    )


def latest_user_text(messages) -> str:
    """取最近一条用户消息的纯文本（跳过 harness 自己注入的自省提示）。"""
    for message in reversed(list(messages or [])):
        if getattr(message, "type", "") != "human":
            continue
        content = getattr(message, "content", "")
        if isinstance(content, list):
            content = "".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict)
            )
        text = str(content or "").strip()
        if not text or text.startswith("【流程自省提示"):
            continue  # 自省提示是 harness 产出的，不是用户说的
        return text
    return ""


class PathArgs(Args):
    path: str = Field(
        default=".",
        description=(
            "路径。仅工作文件夹模式下是授权目录内的相对路径；"
            "完全访问权限模式下可给绝对路径（例如 D:\\\\notes）。"
        ),
    )


class SearchArgs(Args):
    query: str = Field(min_length=1, max_length=200)


class ToolSearchArgs(Args):
    """`tool_search` 的参数。`limit` 有上限：检索结果本身也要占预算。"""

    query: str = Field(
        min_length=1,
        max_length=200,
        description="关键词。会匹配工具名、工具说明与 toolset 名，名字命中优先。",
    )
    limit: int = Field(default=8, ge=1, le=20, description="最多返回几条（1-20）")


class FindArgs(Args):
    query: str = Field(min_length=1, max_length=200, description="要搜索的关键词")
    path: str = Field(
        default="",
        description=(
            "搜索的起始目录。仅工作文件夹模式下留空即授权目录；"
            "完全访问权限模式下必须给出（相对授权目录或绝对路径），否则会拒绝执行。"
        ),
    )


class ReportArgs(Args):
    path: str = Field(description="以 .md 结尾的相对路径")
    content: str = Field(max_length=100000)


class WriteFileArgs(Args):
    path: str = Field(description="要写入的文件路径（完全访问权限下可为整机任意绝对路径）")
    content: str = Field(max_length=1000000, description="要写入的完整文本内容")
    overwrite: bool = Field(
        default=False,
        description=(
            "目标已存在时是否覆盖。默认 false：已存在会拒绝并提示；"
            "确认要改写既有文件时再传 true。"
        ),
    )


class CommandArgs(Args):
    command: str = Field(
        min_length=1,
        max_length=8000,
        description=(
            "要执行的系统命令，整条交给 shell（Windows 是 cmd.exe，Linux/macOS 是 sh）。"
            "要串联多步用 shell 自己的连接符（如 && ），不要分多次调用。"
        ),
    )
    cwd: str = Field(
        default="",
        description=(
            "工作目录。留空＝授权目录（仅工作文件夹档）或用户主目录（完全访问档）；"
            "完全访问档可给任意绝对路径。"
        ),
    )
    timeout: int = Field(
        default=120,
        ge=1,
        le=1800,
        description="超时秒数，到点强制终止该进程。命令本就该很快结束，别用它等长任务。",
    )


class CodeExecArgs(Args):
    """`code_execution` 的参数。**故意没有"环境"或"额外文件"这类入口**：
    参数面越小，越容易说清"这段代码到底在什么条件下跑的"。"""

    code: str = Field(
        min_length=1,
        max_length=code_exec.MAX_CODE_CHARS,
        description=(
            "要执行的代码正文（Python）。整段放进 `python -c` 跑，不经过 shell，"
            "所以管道符、重定向符都是代码的一部分而不是 shell 语法。"
            "要打印结果请用 print：只有 stdout/stderr 会回传。"
        ),
    )
    language: str = Field(
        default="python",
        description="语言。本机只保证 python（就是 Orbit 自己的解释器）。",
    )
    timeout: int = Field(
        default=code_exec.DEFAULT_TIMEOUT,
        ge=1,
        le=code_exec.MAX_TIMEOUT,
        description=(
            f"超时秒数（默认 {code_exec.DEFAULT_TIMEOUT}）。到点终止整棵进程树，"
            "并回 exit_code=null + timed_out=true。"
        ),
    )
    cwd: str = Field(
        default="",
        description=(
            "工作目录。留空＝授权目录（仅工作文件夹档）或用户主目录（完全访问档）。"
            "相对路径按授权目录解析。"
        ),
    )


#: `local_read_file` 的分页口径（与 Hermes `read_file` 的 `limit` 默认 2000 行一致）。
#: 上限 5000 行：一次读得再多，进上下文也要被三层治理裁掉，不如让模型自己分页——
#: 分页可预期，截断不可预期。
READ_FILE_DEFAULT_LINES = 2_000
READ_FILE_MAX_LINES = 5_000
#: 单文件大小上限，与 `local_read_document` 同一档（8 MB）。
READ_FILE_MAX_BYTES = 8 * 1024 * 1024
#: 单行渲染宽度上限：超宽行（压缩过的 JSON、minified JS）截断，否则一行就能吃光预算。
READ_FILE_LINE_CHARS = 2_000


class ReadFileArgs(Args):
    """`local_read_file` 的参数。`offset`/`limit` 是"读哪一段"的唯一说法。"""

    path: str = Field(
        description=(
            "要读的文件路径。仅工作文件夹模式下是授权目录内的相对路径；"
            "完全访问权限模式下可给绝对路径（例如 D:\\\\src\\\\main.py）。"
        )
    )
    offset: int = Field(
        default=1,
        ge=1,
        description="起始行号，**从 1 开始**。接上一次返回的 next_offset 就能接着往下读。",
    )
    limit: int = Field(
        default=READ_FILE_DEFAULT_LINES,
        ge=1,
        le=READ_FILE_MAX_LINES,
        description=f"最多读多少行（默认 {READ_FILE_DEFAULT_LINES}，上限 {READ_FILE_MAX_LINES}）",
    )


#: `local_search_files` 的三种输出形态（对齐 ripgrep 的 content / -l / -c）。
SEARCH_OUTPUT_MODES = ("content", "files_with_matches", "count")
#: 单个文件参与匹配的字符上限。超过就只搜前这么多——搜索要的是"哪些文件里有"，
#: 不是把整个仓库读进内存。
SEARCH_FILE_CHARS = 200_000
#: 单条命中行的展示宽度（长行截断，避免一条 minified 结果吃光预算）。
SEARCH_LINE_CHARS = 300


class SearchFilesArgs(Args):
    """`local_search_files` 的参数：按内容找、按名字找、只要计数。"""

    pattern: str = Field(
        min_length=1,
        max_length=400,
        description=(
            "要找的东西。target=content 时是关键词（大小写不敏感）；"
            "target=files 时是文件名/相对路径，支持通配（*.py、src/**/*.md），"
            "不含通配符时按子串匹配。"
        ),
    )
    target: str = Field(
        default="content",
        description="content=搜文件内容（默认）；files=只按名字找文件。",
    )
    path: str = Field(
        default="",
        description=(
            "搜索起点目录。仅工作文件夹模式下留空即授权目录；"
            "完全访问权限模式下**必须给出**，否则拒绝执行。"
        ),
    )
    file_glob: str = Field(
        default="",
        description="只在文件名匹配这个通配符的文件里搜（如 *.py、*.md），留空表示不限。",
    )
    output_mode: str = Field(
        default="content",
        description=(
            "content=逐条命中（带行号与上下文）；files_with_matches=只列命中文件名；"
            "count=每个文件的命中条数。不确定有多少命中时先用 count。"
        ),
    )
    context: int = Field(default=0, ge=0, le=5, description="每条命中前后各带几行上下文（0-5）")
    limit: int = Field(default=50, ge=1, le=200, description="最多返回多少条（1-200）")


#: 后台进程日志目录（相对 data_dir）。放在数据目录里而不是临时目录：日志是
#: "这条长任务跑到哪了"的唯一证据，重启后还要能读。
PROCESS_LOG_SUBDIR = "processes"
#: `process poll` 默认回多少字符的日志尾巴。
PROCESS_POLL_TAIL = 4_000


class ProcessArgs(Args):
    """`process` 的参数：起一个后台长任务，然后 list / poll / kill 它。"""

    action: str = Field(
        default="list",
        description=(
            "start=起一个后台进程（需要 command）；list=看有哪些、还在不在跑；"
            "poll=读某个进程的最新输出与退出码（需要 process_id）；"
            "kill=终止某个进程及其子进程（需要 process_id）。"
        ),
    )
    command: str = Field(default="", description="start 时要执行的命令（整条交给 shell）")
    cwd: str = Field(default="", description="start 时的工作目录，留空同 run_command")
    process_id: str = Field(default="", description="poll / kill 时必填，从 start 或 list 拿")
    tail: int = Field(
        default=PROCESS_POLL_TAIL,
        ge=200,
        le=20_000,
        description="poll 时回多少字符的日志尾巴（默认 4000）",
    )


class SessionSearchArgs(Args):
    """`session_search` 的参数：找回以前对话里的原话。"""

    query: str = Field(
        default="",
        max_length=200,
        description=(
            "要找回的关键词（多个词按 AND 处理）。留空且没给 message_id 时，"
            "列出最近几场对话（不知道关键词、只想看看聊过什么时用）。"
        ),
    )
    session_id: str = Field(default="", description="只在某一场对话里搜（可选）")
    message_id: str = Field(
        default="",
        description="给了就展开这条消息前后的对话（scroll），用于把命中点看全。",
    )
    limit: int = Field(default=3, ge=1, le=10, description="最多返回几个命中点（1-10）")
    window: int = Field(default=5, ge=1, le=20, description="scroll 时命中点前后各展开几条消息")


class MemoryArgs(Args):
    key: str = Field(
        min_length=1,
        max_length=160,
        description=(
            "记忆的稳定键名，用简短名词短语，例如「报告格式偏好」「默认语言」。"
            "同一条信息必须复用同一个 key——再次保存会更新原条目而不是新增。"
            "不要把具体值、日期、编号或本次运行的标识写进 key（例如别说"
            "「报告校验码 09301925」）；这类会变的内容放在 value 里。"
        ),
    )
    value: str = Field(min_length=1, max_length=4000)


class KeyArgs(Args):
    key: str = Field(
        description=(
            "要删除记忆的键名。支持部分匹配：给「报告」会删掉所有键名含「报告」的记忆。"
            "不确定键名时先调用 memory_search 查看现有记忆。"
        )
    )


class TaskArgs(Args):
    task_id: str


class RetrievalArgs(Args):
    query: str = Field(min_length=2, max_length=200, description="要检索的关键词或短语")
    scope: str = Field(
        default="task",
        description="task=只在当前会话内容里找（默认）；global=在所有历史内容里找",
    )
    source_type: str = Field(
        default="",
        description="限定来源：web_page、local_file、mcp_resource；留空表示全部",
    )
    top_k: int = Field(default=5, ge=1, le=5, description="返回片段数上限")


class SkillLoadArgs(Args):
    name: str = Field(min_length=1, max_length=80, description="要加载的技能名，如 web_search")


class SkillListArgs(Args):
    pass


class SkillSaveArgs(Args):
    name: str = Field(min_length=1, max_length=80, description="技能名（唯一标识，字母数字下划线连字符）")
    scope: str = Field(default="global", description="作用域：global / project / site")
    triggers: list[str] = Field(default_factory=list, description="触发条件（自然语言短语列表）")
    body: str = Field(min_length=1, description="技能正文：触发条件 + 步骤 + 坑 + 绕法")


#: `todo` 的容量上限。**单任务 50 条 / 单条 200 字符**。
#:
#: 为什么这么小：清单是"模型给自己写的备忘"，不是文档库。条目一多，每次调用回读的
#: 完整清单就开始吃上下文（50 × 200 ≈ 10K 字符），收益却是零——超过 50 步的任务该拆。
#: 超限**直接拒绝并说明**，不做静默截断：截断会让模型以为自己记下了，实际丢了。
TODO_MAX_ITEMS = 50
TODO_MAX_CHARS = 200
#: 条目状态机。`in_progress` 允许同时有多条（真实任务本来就并行），不强制唯一。
TODO_STATUSES = ("pending", "in_progress", "done")
#: 状态的中文标签（渲染清单用）。
TODO_STATUS_LABELS = {"pending": "待办", "in_progress": "进行中", "done": "已完成"}


class TodoArgs(Args):
    """`todo` 的参数。

    **Orbit 的取舍：回读，不是注入**。Hermes 的 `todo` 会把清单**注入**进下一轮上下文
    （`todo_tool.format_for_injection()`，由 agent 运行时拼进系统提示），模型不必调用
    就能看见。Orbit 这边 `app/storage.py`（不能加表）与 `app/graph/*`（不能改注入）
    都在禁改区，所以改成"**每次调用都返回完整清单**"：功能可达（增删改查 + 看全量），
    但**不完全等价**——模型必须主动调用一次才能看到当前状态，漏调就可能凭记忆干活。
    这就是为什么 `action` 的默认值是 `list`：最省事的调用就是回读。
    """

    action: str = Field(
        default="list",
        description=(
            "add=追加条目；update=改某条状态；complete=把某条标成已完成；"
            "remove=删掉某条；list=读完整清单（默认）；clear=清空。"
            "**每次调用都会返回完整清单**（Orbit 不会自动注入），所以开工前先 list 一次。"
        ),
    )
    items: list[str] = Field(
        default_factory=list,
        description=f"add 时要追加的条目，每条一句可执行的小步骤（单条≤{TODO_MAX_CHARS}字符，"
        f"单任务上限 {TODO_MAX_ITEMS} 条）",
    )
    index: int = Field(
        default=0,
        ge=0,
        description="条目序号，**从 1 开始**（update / complete / remove 必填）",
    )
    status: str = Field(
        default="",
        description=f"update 时的新状态：{' / '.join(TODO_STATUSES)}",
    )


def _inline_refs(node: Any, defs: dict) -> Any:
    """把 ``$defs``/``$ref`` 就地展开成内联 schema（递归）。

    为什么必须展开：供应商对 JSON Schema 的支持参差不齐，``$ref`` 在 OpenAI 兼容
    端点上经常被 400 拒掉——而一次 400 会让**整轮请求**发不出去（见
    :func:`schema_problems` 的注释）。内联之后形状是"普通嵌套对象"，谁都能吃。
    """
    if isinstance(node, dict):
        reference = node.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            merged = dict(_inline_refs(defs.get(reference.rsplit("/", 1)[-1], {}), defs))
            for key, value in node.items():
                if key != "$ref":
                    merged[key] = _inline_refs(value, defs)
            return merged
        return {key: _inline_refs(value, defs) for key, value in node.items()}
    if isinstance(node, list):
        return [_inline_refs(item, defs) for item in node]
    return node


class ClarifyOption(BaseModel):
    """`clarify` 的一个选项。"""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=80, description="选项短标签，例如「用 Markdown」")
    description: str = Field(
        default="",
        max_length=400,
        description="一句话说清选它会带来什么后果（模型该在这里写清代价，而不是重复标签）",
    )


class ClarifyArgs(Args):
    """`clarify` 的参数。

    与 `ask_user` 的**分工**（写进 docstring 是因为模型真的会混）：

    - `ask_user` = **我卡住了，要你介入**（登录墙、验证码、需要人肉操作的页面）。
      它打断的是"执行"，用户回答完 agent 继续干活。
    - `clarify` = **我需要你在几个选项里选一个**（需求有歧义、方案要拍板）。
      它打断的是"决策"，答案会直接决定下一步走哪条路。

    所以：能自己判断的低风险问题别问；有明确选项的用 `clarify`；需要人动手的用
    `ask_user`。
    """

    question: str = Field(
        min_length=1,
        max_length=2000,
        description="要用户决定的问题本身。**不要把选项写进问题文本**——选项放 options。",
    )
    options: list[ClarifyOption] = Field(
        min_length=2,
        max_length=6,
        description="2-6 个选项。把你推荐的那个放第一个，并在 description 里写清理由。",
    )
    allow_free_text: bool = Field(
        default=True,
        description="是否允许用户不选选项、直接用自己的话回答。默认 True；"
        "只有「必须是这几个之一」时才关掉。",
    )

    @field_validator("options", mode="before")
    @classmethod
    def _check_option_count(cls, value):
        """数量校验在 schema 之外再做一次，只为给出**中文**拒绝理由。

        pydantic 的 `min_length` 报错是英文长句（"List should have at least 2 items…"），
        模型看到它会去猜"到底要几个"。这里先拦一道，把话说清楚。
        """
        if isinstance(value, (list, tuple)) and not 2 <= len(value) <= 6:
            raise ValueError(
                f"选项数量必须是 2-6 个，现在给了 {len(value)} 个。"
                "只有一个选项说明这不是选择题（自己决定就行）；超过 6 个请先合并。"
            )
        return value

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict:
        """暴露给模型的 schema 用**内联**形状（展开 ``$defs``，见 :func:`_inline_refs`）。"""
        schema = super().model_json_schema(*args, **kwargs)
        defs = schema.pop("$defs", None)
        return _inline_refs(schema, defs) if isinstance(defs, dict) and defs else schema


class ApplyPatchArgs(Args):
    """`apply_patch` 的参数。"""

    patch: str = Field(
        min_length=1,
        max_length=200000,
        description=(
            "补丁正文。格式：`*** Begin Patch` → `*** Update File: 路径` / "
            "`*** Add File: 路径` / `*** Delete File: 路径` → 可选 `@@` 提示 → "
            "以 `-`/`+`/空格 开头的行 → `*** End Patch`。"
            "**只写被改的行**，不要重发整个文件。"
        ),
    )


@dataclass
class Tool:
    """A callable the model may pick.

    Built-in tools declare a Pydantic model so arguments are validated strictly. External
    tools (MCP servers, HTTP endpoints) arrive with a JSON Schema that we pass through
    unchanged — we did not define their contract and must not pretend to validate it.
    """

    name: str
    description: str
    schema: type[BaseModel] | None = None
    confirmation: bool = False
    parameters: dict | None = None
    source: str = "builtin"

    def specification(self):
        if self.parameters is not None:
            arguments = self.parameters
        elif self.schema is not None:
            arguments = self.schema.model_json_schema()
        else:
            arguments = {"type": "object", "properties": {}}
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": arguments,
            },
        }


LOCAL_TOOLS = [
    Tool(
        "tool_search",
        "按关键词检索当前可用的工具（返回名字、说明、所属 toolset、参数名）。"
        "不确定该用哪个工具时先搜一下；只返回**当前平台已暴露**的工具，搜到的都能直接调用。",
        ToolSearchArgs,
    ),
    Tool(
        "todo",
        "多步任务的任务清单（增删改查）。**清单不会自动出现在你的上下文里**——"
        "每次调用都返回完整清单，所以开工前先 list 一次、做完一步再 complete 一次。"
        f"单任务上限 {TODO_MAX_ITEMS} 条、单条 {TODO_MAX_CHARS} 字符；超限会被拒绝。"
        "三步以上的任务就该用它，别靠记忆。",
        TodoArgs,
    ),
    Tool(
        "clarify",
        "需要用户在几个方案里做决定时，给出一道**选择题**（2-6 个选项）。"
        "选项会渲染成带序号的可点列表交给用户。"
        "分工：需要用户动手介入（登录、验证码、页面操作）用 ask_user；"
        "需求有歧义、要拍板走哪条路，用 clarify。",
        ClarifyArgs,
    ),
    Tool(
        "local_list_files",
        "列出文件。仅工作文件夹模式下只看授权目录；完全访问权限模式下绝对路径可点任意目录，"
        "路径留空会列出整机盘符。",
        PathArgs,
    ),
    Tool(
        "local_read_document",
        "读取文档，最多返回 16000 字符。仅工作文件夹模式下支持 TXT、Markdown、文本型 PDF；"
        "完全访问权限模式下任意可解码为 UTF-8 文本的文件都能读。"
        "**要读大文件的中段、或需要反复往下读，用 local_read_file（带 offset/limit 分页）。**",
        PathArgs,
    ),
    Tool(
        "local_read_file",
        "按行分页读一个文本文件，返回**带行号**的片段与 total_lines / next_offset。"
        "几千行的日志、长源码用它：给 offset（起始行，从 1 开始）+ limit（行数）就能读任意一段，"
        "接上次的 next_offset 继续往下读。要一次性看全文（短文档）用 local_read_document。",
        ReadFileArgs,
    ),
    Tool(
        "local_search_text",
        "在文本文件里搜索关键词。仅工作文件夹模式下只搜授权目录的 TXT/Markdown；"
        "完全访问权限模式下必须给出起始目录，可搜任意文本文件。"
        "**要按文件名找文件、要只看命中条数、或要限定 *.py 这类后缀，用 local_search_files。**",
        FindArgs,
    ),
    Tool(
        "local_search_files",
        "递归搜索文件：content=搜内容（带行号/上下文）、files=按文件名或通配符找文件、"
        "count=每个文件的命中条数。可限定 file_glob（如 *.py）与起始目录。"
        "先 count 估规模，再 content 看具体命中，比逐个读文件快得多。",
        SearchFilesArgs,
    ),
    Tool("local_write_report", "在授权目录创建 Markdown 报告，不覆盖不同内容。", ReportArgs),
    Tool(
        "local_write_file",
        "写入或改写文本文件（新建、覆盖）。「仅工作文件夹」档只能落在授权目录内且不覆盖"
        "工作台自身数据；「完全访问权限」档可写整机任意路径、不设限。",
        WriteFileArgs,
    ),
    Tool(
        "apply_patch",
        "用**结构化补丁**改文件，而不是整文件重写（长文件整段重发必然丢内容）。"
        "格式：`*** Begin Patch` / `*** Update File: 路径` / `@@ 可选上下文提示` / "
        "以 `-` 删、`+` 增、空格 表示上下文 / `*** End Patch`；新建用 Add File，"
        "删除用 Delete File。**全部 hunk 先校验再落盘**：任何一处上下文对不上就整体不写，"
        "报错会指出第几个 hunk、第几行。只允许改授权工作目录内的文件。",
        ApplyPatchArgs,
    ),
    Tool(
        "run_command",
        "在本机执行系统命令，返回 stdout / stderr 与退出码。需要装依赖、跑构建、查进程、"
        "调用命令行工具时用它。"
        "「仅工作文件夹」档：工作目录固定在工作目录内，且不可逆命令（格式化、关机、递归删除"
        "根目录等）会被拒绝；「完全访问权限」档：不限工作目录、不限命令。",
        CommandArgs,
    ),
    Tool(
        "code_execution",
        "把一段 **Python 代码**放进独立子进程里执行，返回 stdout / stderr / 退出码。"
        "算数、批处理文件、跑一段一次性逻辑用它；要调用系统命令、装依赖、跑构建用 run_command。"
        "**不是沙箱**：文件系统、网络、内存都不受限，且子进程继承当前环境变量。"
        "超时（默认 30 秒）会杀掉整棵进程树。执行前会过与 run_command 同一套危险裁决。",
        CodeExecArgs,
    ),
    Tool(
        "process",
        "后台进程管理。`start` 起一个长任务（起开发服务器、跑整套测试、长时间构建）并立刻返回 "
        "process_id，**不占住这一轮对话**；之后 `poll` 读它的最新输出与退出码、`list` 看还有哪些在跑、"
        "`kill` 终止它（连子进程一起）。日志落在数据目录的 processes/ 下，重启后仍能读。"
        "一条几秒就结束的命令用 run_command，别用它。",
        ProcessArgs,
    ),
    Tool("memory_search", "按关键词搜索跨对话长期记忆。", SearchArgs),
    Tool(
        "memory_save",
        "保存或更新长期记忆。**只在用户本轮明确说「记住……」时才调用**；"
        "用户没有显式要求时调用会被拒绝。记忆不授予任何工具权限。",
        MemoryArgs,
        True,
    ),
    Tool(
        "memory_delete",
        "删除长期记忆。只在用户要求忘记某件事时调用，支持键名部分匹配。",
        KeyArgs,
        True,
    ),
    Tool("history_search_tasks", "查找其他对话的历史任务，用户提到以前的工作时使用。", SearchArgs),
    Tool("history_get_task", "读取选中历史任务的目标、结果产物，不载入其他对话原文。", TaskArgs),
    Tool(
        "session_search",
        "找回以前对话里的**原话**：按关键词搜历史消息（多个词按 AND），命中后可用 message_id "
        "展开前后文；什么关键词都想不起来时，留空直接列出最近几场对话。"
        "分工：要看「当时那个任务做成什么样」用 history_*；要引用户当初的原话用 session_search。",
        SessionSearchArgs,
    ),
    Tool(
        "retriever_search",
        "检索此前读过的网页正文、工具结果与本地文件片段，返回带来源地址的片段。"
        "只在需要回看已读过的内容时使用，不要用它回答常识问题。",
        RetrievalArgs,
    ),
    Tool(
        "skill_load",
        "读取一个已登记技能（流程做法）的完整正文。技能名会出现在系统提示的技能清单里，"
        "只在判断该技能相关时调用。",
        SkillLoadArgs,
    ),
    Tool(
        "skill_list",
        "列出所有已登记技能的名字与触发条件（用于了解有哪些流程做法可用）。",
        SkillListArgs,
    ),
    Tool(
        "skill_save",
        "把这次任务摸索出的新流程做法沉淀成技能（写成 SKILL.md，标 unreviewed 待确认）。"
        "只在用户要求沉淀、或任务成功且踩到了值得复用的新坑时使用；不要复制轨迹，要参数化步骤。",
        SkillSaveArgs,
    ),
]


def resolve_path(root: Path, relative: str, machine: bool = False) -> Path:
    """把模型给的路径解析成真实路径。**权限级别的唯一落地点。**

    - `machine=False`（仅工作文件夹）：一律相对授权目录解析，并校验结果仍在目录内。
      这就是 `../outside.txt` 被拒的地方，也是历史行为。
    - `machine=True`（完全访问权限）：绝对路径直接采用，相对路径仍相对授权目录解析，
      **不做包含校验**，因此能落到整机任意位置。
    """
    if machine:
        candidate = Path(relative).expanduser()
        if candidate.is_absolute():
            return candidate.resolve()
        return (root / candidate).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("路径超出授权工作目录")
    return path


def display_path(path: Path, root: Path, machine: bool) -> str:
    """回传给模型的路径表示。能相对授权目录说清楚就相对说（省 token、更好读）。"""
    try:
        return str(path.relative_to(root.resolve()))
    except ValueError:
        return str(path)


def machine_roots() -> list[Path]:
    """整机入口。Windows 枚举盘符；其他平台退回根目录。"""
    listdrives = getattr(os, "listdrives", None)
    if listdrives is not None:
        try:
            drives = [Path(drive + os.sep) for drive in listdrives()]
            if drives:
                return drives
        except OSError:
            pass
    return [Path(os.sep)]


#: 说明：危险命令的拦截表**已搬到 `app/approvals.py`**（两级：硬地板 / 危险模式）。
#: 这里不再维护第二份列表——两份列表必然漂移，而漂移的安全边界比没有更危险。
#:
#: 设计取舍没有变：这份判据的职责**只有一件事**——"把机器搞坏且不可恢复"，
#: 不是通用白名单，也不是沙箱。它挡不住 `python -c "..."` 这类任意代码，
#: 那要靠权限档位与预算刹车，硬堆正则只会制造漏斗式的安全感。


def dangerous_command(command: str) -> str:
    """命中**硬地板**命令时返回它的中文名，放行返回空串。

    实现在 `app/approvals.py`——那里分两级（硬地板 / 危险模式），因为
    "机器会坏且不可恢复"与"可能不可逆但常见"是两种东西，处置方式不同。
    这里保留这个薄封装是为了向后兼容（既有测试与调用点都认这个名字）。
    """
    verdict = approvals.classify(command, env_type="local")
    return verdict.label if verdict.verdict == approvals.VERDICT_DENY else ""


def _refusal(verdict, machine: bool, mode: str) -> str:
    """把裁决结果翻成"模型能据此改动作"的一句话。

    拒绝文案必须给出**下一步怎么做**：只说"不允许"，模型会换个写法反复重试，
    既浪费预算也让人看不出它到底想干什么。
    """
    if verdict.verdict == approvals.VERDICT_DENY:
        if machine:
            return (
                f"拒绝执行：命中硬地板规则「{verdict.label}」（{verdict.rule}），"
                "不可恢复。完全访问档默认不设限，但 `approvals.hardline_in_machine` "
                "已被打开，所以这里仍然拦截。要执行请自行在终端里做，"
                "或关掉该开关（不推荐）。"
            )
        return (
            f"「仅工作文件夹」档拒绝执行可能不可逆的命令（{verdict.label}，"
            f"规则 {verdict.rule}）。确认要在本机执行，请在对话页把权限级别切到"
            "「完全访问权限」。"
        )
    return (
        f"拒绝执行：命中危险模式「{verdict.label}」（{verdict.rule}），"
        f"审批模式 `{mode}` 下需要人工处置。"
        f"可以自己执行这条命令，或先 `orbit approvals allow add {verdict.rule}` 加白再重试。"
    )


def decode_output(raw: bytes | None) -> str:
    """把子进程输出解成文本。Windows 的 cmd 常输出 GBK，所以 UTF-8 失败后退到 GBK。"""
    if not raw:
        return ""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def clip_text(text: str, limit: int = 8000) -> tuple[str, bool]:
    """输出过长时保留**头 + 尾**：报错总结在尾部（构建日志），正文/列表在头部。"""
    if len(text) <= limit:
        return text, False
    head = limit * 3 // 5
    tail = limit - head
    omitted = len(text) - limit
    return f"{text[:head]}\n…（中间省略 {omitted} 字符）…\n{text[-tail:]}", True


def schema_problems(spec: dict) -> list[str]:
    """检查一份工具 schema 是否会被供应商拒绝。返回问题列表（空 = 合法）。

    为什么要在**暴露之前**查而不是等供应商报错：畸形 schema 会让整次请求 400，
    而错误信息通常只说「第 N 个函数的参数非法」，定位成本极高。常见畸形有：
    `parameters.type` 不是 `"object"`（OpenAI 系硬要求）、`required` 里写了
    `properties` 里不存在的键、`properties` 不是对象。这些在本地一秒就能查出来。

    两种形状都吃：Orbit 内部发的是 OpenAI 函数调用形状
    （``{"type": "function", "function": {...}}``），裸形状（直接 ``{name, ...}``）
    也兼容——将来换 transport 时不必改这里。
    """
    problems: list[str] = []
    if not isinstance(spec, dict):
        return [f"schema 不是 dict，而是 {type(spec).__name__}"]
    body: dict = spec
    candidate = spec.get("function")
    if isinstance(candidate, dict):
        body = candidate
    if not body.get("name"):
        problems.append("缺 name")
    if not isinstance(body.get("description"), str) or not body.get("description"):
        problems.append("缺 description")
    parameters = body.get("parameters")
    if parameters is None:
        # 无参工具允许省略 parameters（调用方按空对象处理），不算问题。
        return problems
    if not isinstance(parameters, dict):
        problems.append(f"parameters 不是 dict，而是 {type(parameters).__name__}")
        return problems
    if parameters.get("type") != "object":
        problems.append(f"parameters.type 必须是 'object'，实际是 {parameters.get('type')!r}")
    properties = parameters.get("properties", {})
    if not isinstance(properties, dict):
        problems.append("parameters.properties 必须是 dict")
        properties = {}
    required = parameters.get("required", [])
    if not isinstance(required, list):
        problems.append("parameters.required 必须是 list")
    else:
        unknown = [key for key in required if key not in properties]
        if unknown:
            problems.append(f"required 里有 properties 不存在的键：{'、'.join(map(str, unknown))}")
    return problems


#: 描述裁剪后的一句话上限（字符）。80 字符 ≈ 一句中文说明，够模型判断"这工具干嘛的"，
#: 不够判断"参数怎么填"——后者本来就该走 `tool_search` 拿完整 schema。
TRIMMED_DESCRIPTION_CHARS = 80


def estimate_tokens(payload: Any) -> int:
    """估算 token 数（字符数 / 4）。**不引 tokenizer**：本机装不了依赖。

    实现放在 ``app/toolsets.py``（纯函数、零依赖），这里再导出一次：裁剪预算属于
    工具层的事，读代码的人不该为了一个字符除法跳去另一个模块。
    """
    return toolsets.estimate_tokens(payload)


def _one_line(description: str, limit: int = TRIMMED_DESCRIPTION_CHARS) -> str:
    """把一段多句描述压成"第一句 + 省略号"。

    为什么取第一句而不是截前 N 个字符：Orbit 的工具描述都是「先一句是什么，再若干句
    什么时候用、和谁分工」。截断会正好砍在"什么时候用"上，留下半句没有主语的话；
    取第一句至少保住了"这工具是干什么的"。
    """
    text = " ".join(str(description or "").split())
    if len(text) <= limit:
        return text
    for mark in ("。", "；", ". ", "！", "？"):
        index = text.find(mark)
        if 0 < index < limit:
            return text[: index + len(mark)]
    return text[: limit - 1] + "…"


def _trim_specs(
    specs: list[dict], budget: int
) -> tuple[list[dict], bool]:
    """按 tier 逐级把描述压成一句，返回 ``(裁剪后的 specs, 是否仍然超预算)``。

    规则（MATRIX 5.11）：

    * **Tier 0 永不裁剪**（``toolsets.ALWAYS_EAGER``）：它们缺了这一轮根本没法开始；
    * 超预算时先裁 Tier 2，仍超再裁 Tier 1；
    * **参数 schema 一个字节都不动**——理由见 ``ToolRegistry.specs`` 的 docstring；
    * **一个工具都不删**：删掉工具等于让模型"调不动"，比描述长得多严重得多。

    返回值里那个 ``over_budget`` 是**诚实位**：schema 地板本身超预算时它就是 True，
    调用方（CLI / 诊断）可以据此说"预算压不下来，原因是参数契约"，而不是假装达标。
    悄悄返回一份仍然超预算的清单，是这类预算表最常见的谎。
    """
    trimmed = [dict(spec) for spec in specs]
    # `function` 这一层也要复制：`_one_line` 会**原地改** description，而它挂在
    # `spec["function"]` 下面。只复制外层等于改了调用方的对象——`specification()`
    # 目前每次返回新 dict，但这条隐含假设不该由裁剪函数来赌。
    for index, spec in enumerate(trimmed):
        body = spec.get("function")
        if isinstance(body, dict):
            trimmed[index] = {**spec, "function": {**body}}
    if toolsets.estimate_tokens(trimmed) <= budget:
        return trimmed, False
    for tier in (2, 1):
        for spec in trimmed:
            body = spec.get("function")
            if not isinstance(body, dict):
                continue
            name = str(body.get("name") or "")
            if toolsets.tier_of(name) != tier:
                continue
            description = str(body.get("description") or "")
            short = _one_line(description)
            if short and short != description:
                body["description"] = short
        if toolsets.estimate_tokens(trimmed) <= budget:
            return trimmed, False
    # 描述已全部压到一句（Tier 0 除外）仍然超：地板是参数 schema，不再动。
    return trimmed, toolsets.estimate_tokens(trimmed) > budget


def terminate_tree(process: subprocess.Popen) -> None:
    """终止整棵进程树。

    `shell=True` 下 `process.pid` 只是外壳（Windows 的 cmd.exe / POSIX 的 sh），
    **只杀外壳会把真正的子进程留成孤儿**——管道不关闭，`communicate()` 会一直阻塞到
    子进程自己结束，超时形同虚设。所以必须按树杀：Windows 用 `taskkill /T`，POSIX 杀进程组。
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                timeout=15,
            )
        else:
            import signal

            # 非 Windows 才走到这里；mypy 按 win32 平台分析，故显式忽略。
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)  # type: ignore[attr-defined]
    except Exception:  # 树杀失败也要继续兜底，不能把异常抛回给工具调用
        log.debug("进程树终止失败，退回单进程 kill", exc_info=True)
    finally:
        try:
            process.kill()
        except Exception:
            pass


def run_shell(
    command: str, cwd: Path, timeout: int, env: dict[str, str] | None = None
) -> dict:
    """同步执行一条 shell 命令并回收结果。`shell=True`：Windows 走 cmd.exe，其余走 sh。

    用 `Popen` 而不是 `subprocess.run`：run 在超时时只杀直接子进程，随后仍会阻塞在
    `communicate()` 上等孤儿进程关掉管道，实测把 1 秒超时拖成 30 秒。这里改成"杀整棵树 +
    有界回收"，超时是真的超时。

    `env` 默认 `None`（= 完全继承当前进程环境，与引入终端后端抽象之前逐字一致）；
    `app/terminal.py::LocalBackend` 需要注入变量时才显式传。
    """
    started = time.time()
    process = subprocess.Popen(
        command,
        shell=True,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        start_new_session=(os.name != "nt"),
    )
    timed_out = False
    try:
        raw_out, raw_err = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_tree(process)
        try:
            raw_out, raw_err = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            raw_out, raw_err = b"", b""
    stdout, cut_out = clip_text(decode_output(raw_out))
    stderr, cut_err = clip_text(decode_output(raw_err))
    result = {
        "command": command,
        "cwd": str(cwd),
        "exit_code": None if timed_out else process.returncode,
        "timed_out": timed_out,
        "stdout": stdout,
        "stderr": stderr,
        "truncated": cut_out or cut_err,
        "seconds": round(time.time() - started, 2),
    }
    if timed_out:
        result["notice"] = f"命令超过 {timeout} 秒仍未结束，已终止整棵进程树。"
    return result


def extract_text(path: Path) -> str:
    """把一个文件读成文本：PDF 走 pypdf，其余按 UTF-8 解码。"""
    if path.suffix.lower() == ".pdf":
        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages[:50])
    return path.read_text(encoding="utf-8-sig")


def read_document(root: Path, relative: str, machine: bool = False) -> dict:
    path = resolve_path(root, relative, machine)
    if not path.is_file():
        raise ValueError("文件不存在或超过 8 MB")
    suffix = path.suffix.lower()
    # 有限档只认三种文档类型；完全访问档不再按扩展名设限——能按 UTF-8 解码的
    # 都算文本（配置文件、源码、日志都能读），解不出来的明确报错而不是塞乱码进上下文。
    if not machine and suffix not in {".md", ".txt", ".pdf"}:
        raise ValueError("暂时支持 TXT、Markdown、文本型 PDF")
    if path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("文件不存在或超过 8 MB")
    try:
        text = extract_text(path)
    except UnicodeDecodeError as exc:
        raise ValueError("该文件不是 UTF-8 文本，无法作为文档读取") from exc
    return {
        "path": display_path(path, root, machine),
        "text": text[:16000],
        "truncated": len(text) > 16000,
    }


def walk_text_files(base: Path, machine: bool, root: Path, visit_limit: int = 20000):
    """惰性遍历候选文本文件。`os.walk` + `onerror` 吞掉权限错误——完全访问档会走到
    系统目录，那里 PermissionError 是常态，不能让它中断整次搜索。

    两个上限保证有界：访问条数（visit_limit）与调用方自己的命中条数。
    """
    visited = 0
    for directory, _subdirs, names in os.walk(base, onerror=lambda _error: None):
        for name in names:
            visited += 1
            if visited > visit_limit:
                return
            path = Path(directory) / name
            try:
                if machine:
                    if path.stat().st_size > 1024 * 1024:
                        continue
                elif path.suffix.lower() not in {".md", ".txt"} or not path.resolve().is_relative_to(
                    root
                ):
                    continue
            except OSError:
                continue
            yield path


# ── 分页读文件（local_read_file）────────────────────────────────────────────
#
# 为什么在 `read_document` 之外再要一个：那个工具把全文塞到 16,000 字符就停，
# **没有翻页**——几千行的日志/源码只能看到开头，而"读中间那一段"是排查问题的常规
# 动作（看第 3000 行附近的报错）。分页靠 offset/limit 两个数说清"读的是哪一段"，
# 模型据此能一段段往下读，而不是靠一次比一次大的 limit 撞治理层。


def read_file_page(
    root: Path,
    relative: str,
    *,
    offset: int = 1,
    limit: int = READ_FILE_DEFAULT_LINES,
    machine: bool = False,
) -> dict:
    """按行分页读文本文件，返回带行号的片段与分页信息。

    行号是**渲染进正文**的（`   12\\t内容`）而不是另开一个数组：模型引用行号时
    要能一眼对上，数组形式还得自己数。`next_offset` 是给模型"接着读"的唯一抓手——
    没有它，模型只能猜下一段从哪开始。
    """
    path = resolve_path(root, relative, machine)
    if not path.is_file():
        raise ValueError(f"文件不存在：{relative}")
    if path.stat().st_size > READ_FILE_MAX_BYTES:
        raise ValueError(
            f"文件超过 {READ_FILE_MAX_BYTES // (1024 * 1024)} MB。"
            "先用 local_search_files 定位到具体行，再用 offset 读那一段。"
        )
    # 仅工作文件夹档沿用 `read_document` 的扩展名白名单：放宽读取范围是权限决策，
    # 不该顺手由一个"分页"工具做掉。完全访问档不限扩展名（能解码成文本就行）。
    if not machine and path.suffix.lower() not in {".md", ".txt", ".pdf"}:
        raise ValueError(
            "仅工作文件夹档只支持 TXT / Markdown / 文本型 PDF。"
            "要读源码或配置文件，请在对话页把权限级别切到「完全访问权限」。"
        )
    try:
        text = extract_text(path)
    except UnicodeDecodeError as exc:
        raise ValueError("该文件不是 UTF-8 文本，无法按行读取") from exc

    lines = text.splitlines()
    total = len(lines)
    start = max(1, int(offset))
    if total and start > total:
        raise ValueError(f"offset {start} 超过总行数 {total}；文件只有 {total} 行")
    window = lines[start - 1 : start - 1 + max(1, int(limit))]
    rendered = "\n".join(
        f"{start + index:>6}\t"
        + (line if len(line) <= READ_FILE_LINE_CHARS else line[:READ_FILE_LINE_CHARS] + "…[本行已截断]")
        for index, line in enumerate(window)
    )
    end = start - 1 + len(window)
    truncated = end < total
    return {
        "path": display_path(path, root, machine),
        "offset": start,
        "returned_lines": len(window),
        "total_lines": total,
        "end_line": end,
        "truncated": truncated,
        "next_offset": end + 1 if truncated else None,
        "text": rendered,
    }


# ── 递归搜索（local_search_files）──────────────────────────────────────────
#
# 为什么在 `local_search_text` 之外再要一个：那个工具只回答"哪些文件里有这个词"
# （每个文件一条摘录，最多 30 条），而实际排查要问的是另外三种问题——
# "这个仓库里有没有 X 这个文件"、"有几个文件命中"、"第几行命中的、上下文是什么"。
# 三种输出形态（content / files_with_matches / count）就是对着这三个问题来的，
# 照 ripgrep 的口径：不确定规模先 `count`，再 `content`。


def _line_hits(lines: list[str], needle: str, limit: int) -> list[int]:
    """命中行的下标（大小写不敏感），最多收 `limit` 条。"""
    lowered = needle.casefold()
    hits: list[int] = []
    for index, line in enumerate(lines):
        if lowered in line.casefold():
            hits.append(index)
            if len(hits) >= limit:
                break
    return hits


def search_files(
    root: Path,
    *,
    pattern: str,
    target: str = "content",
    path: str = "",
    file_glob: str = "",
    output_mode: str = "content",
    context: int = 0,
    limit: int = 50,
    machine: bool = False,
) -> dict:
    """递归搜索：按内容（带行号/上下文）、按文件名、或只要计数。

    有界性靠三处：`walk_text_files` 的访问条数上限、每文件只搜前
    :data:`SEARCH_FILE_CHARS` 字符、以及本函数的 `limit` 条命中上限。
    搜索必须"最坏情况也有界"，否则一次 `pattern=.` 就能把整轮对话拖死。
    """
    needle = (pattern or "").strip()
    if not needle:
        raise ValueError("pattern 不能为空")
    start = str(path or "").strip()
    if machine and not start:
        # 与 `local_search_text` 同一条规矩：整机递归搜索必须有起点。
        raise ValueError("完全访问权限下搜索需要指定起始目录（如 D:\\\\notes）")
    base = resolve_path(root, start or ".", machine)
    if not base.is_dir():
        raise ValueError("搜索起点必须是目录")
    mode = output_mode if output_mode in SEARCH_OUTPUT_MODES else "content"
    glob_pat = (file_glob or "").strip()
    wanted = "files" if str(target or "").strip().lower() == "files" else "content"
    has_wildcard = any(char in needle for char in "*?[")

    scanned = 0
    matched_files = 0
    total_hits = 0
    entries: list[dict] = []
    counts: list[dict] = []
    truncated = False

    for candidate in walk_text_files(base, machine, root):
        scanned += 1
        name = candidate.name
        relative = display_path(candidate, root, machine)
        if glob_pat and not fnmatch(name, glob_pat):
            continue

        if wanted == "files":
            # 通配符走 fnmatch（同时比对文件名与相对路径，`src/**/*.py` 这类才有效）；
            # 不带通配符按子串匹配——模型常直接给 "config"，不是 "*.config"。
            folded = needle.casefold()
            if has_wildcard:
                hit = fnmatch(name, needle) or fnmatch(relative, needle)
            else:
                hit = folded in name.casefold() or folded in relative.casefold()
            if not hit:
                continue
            matched_files += 1
            entries.append({"path": relative})
            if len(entries) >= limit:
                truncated = True
                break
            continue

        try:
            text = extract_text(candidate)[:SEARCH_FILE_CHARS]
        except (OSError, UnicodeError, ValueError):
            # 二进制/坏编码/加密 PDF：跳过而不是报错——一次搜索撞上一个坏文件
            # 不该让整次搜索失败（否则整机上永远搜不出东西）。
            continue
        lines = text.splitlines()
        # count 模式要的是**真实条数**，所以每文件上限放得很宽（否则报出来的数字
        # 只是"被 limit 截过的条数"，比不报更误导）；content 模式才按 limit 收。
        hits = _line_hits(lines, needle, 10_000 if mode == "count" else limit)
        if not hits:
            continue
        matched_files += 1
        total_hits += len(hits)
        if mode == "count":
            counts.append({"path": relative, "count": len(hits)})
            if len(counts) >= limit:
                truncated = True
                break
            continue
        if mode == "files_with_matches":
            entries.append({"path": relative})
            if len(entries) >= limit:
                truncated = True
                break
            continue
        for index in hits:
            entry: dict[str, Any] = {
                "path": relative,
                "line": index + 1,
                "text": lines[index].strip()[:SEARCH_LINE_CHARS],
            }
            if context > 0:
                low = max(0, index - context)
                high = min(len(lines), index + context + 1)
                entry["context"] = [
                    f"{number + 1}: {lines[number].strip()[:SEARCH_LINE_CHARS]}"
                    for number in range(low, high)
                    if number != index
                ]
            entries.append(entry)
            if len(entries) >= limit:
                truncated = True
                break
        if truncated:
            break

    payload: dict[str, Any] = {
        "pattern": needle,
        "target": wanted,
        "output_mode": mode,
        "path": display_path(base, root, machine),
        "files_scanned": scanned,
        "matched_files": matched_files,
        # `count` 的口径随模式变：content/count 模式是**命中行数**，files 模式是
        # **命中文件数**（那里没有"行"这个概念，硬报 0 会让模型以为没搜到）。
        "count": matched_files if wanted == "files" else total_hits,
        "truncated": truncated,
    }
    if wanted == "files":
        payload["files"] = entries
    elif mode == "count":
        payload["counts"] = counts
    else:
        payload["matches"] = entries
    if not matched_files:
        payload["notice"] = (
            f"在 {payload['path']} 下扫了 {scanned} 个文件，没有命中「{needle}」。"
            "可以换关键词、放宽 file_glob，或确认搜索起点是否正确。"
        )
    elif truncated:
        payload["notice"] = (
            f"结果已达上限 {limit} 条（共扫 {scanned} 个文件、{matched_files} 个文件命中）。"
            "要看全就先缩小 file_glob 或换个更具体的关键词。"
        )
    return payload


# ── 任务清单（todo）的落盘 ──────────────────────────────────────────────────
#
# 状态放 `<data_dir>/todos/<task_id>.json`：`app/storage.py` 是禁改区（不能加表），
# `app/graph/*` 也是禁改区（不能改注入），所以清单只能落在数据目录里的一等文件上。
# 为什么不用内存：任务会跨轮次、跨进程（断点恢复 / 重启后接着干），内存里的清单
# 一重启就没了，而"我做到第几步"正是最不能丢的状态。


def _safe_task_id(task_id: str) -> str:
    """把 task_id 收敛成安全的文件名片段（不含路径分隔符与保留字符）。"""
    cleaned = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in task_id)
    return (cleaned or "unknown")[:120]


def todos_path(data_dir: Path, task_id: str) -> Path:
    """清单文件路径。"""
    return Path(data_dir) / "todos" / f"{_safe_task_id(task_id)}.json"


def load_todos(data_dir: Path, task_id: str) -> list[dict]:
    """读清单。文件不存在/坏掉都返回空列表——清单坏了不该让任务起不来。"""
    path = todos_path(data_dir, task_id)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(raw, list):
        return []
    items: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        text = str(entry.get("text") or "").strip()
        if not text:
            continue
        status = str(entry.get("status") or "pending")
        items.append(
            {"text": text, "status": status if status in TODO_STATUSES else "pending"}
        )
    return items[:TODO_MAX_ITEMS]


def save_todos(data_dir: Path, task_id: str, items: list[dict]) -> Path:
    """原子写清单：临时文件 + ``os.replace``。

    为什么必须原子：中途崩溃留下半截 JSON 时，`load_todos` 会把它读成空清单——
    模型会以为自己一步都没做，然后从头再来一遍。原子替换让"旧清单 / 新清单"
    二选一，永远不存在"半个清单"。
    """
    path = todos_path(data_dir, task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(items, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)
    return path


def render_todos(items: list[dict]) -> str:
    """把清单渲染成给模型/人看的多行文本（带序号与状态）。"""
    if not items:
        return "（清单为空）"
    lines = []
    for index, item in enumerate(items, start=1):
        label = TODO_STATUS_LABELS.get(item.get("status", "pending"), "待办")
        mark = "x" if item.get("status") == "done" else " "
        lines.append(f"{index}. [{mark}] ({label}) {item['text']}")
    return "\n".join(lines)


def clarify_payload(args: dict) -> dict:
    """把 `clarify` 的参数变成一份"等用户回答"的载荷。

    **为什么返回 `needs_user` 而不是 `ok`**：`app/graph/nodes.py`（禁改区）里
    `execute()` 看到 `status in {"needs_user", "uncertain"}` 就会把
    `{reason, question: message, tool}` 塞进 `waiting`，随后 `gate` 调
    `langgraph.types.interrupt(...)` 把问题交给前端——这正是 `ask_user` 走的同一条
    通路。所以 `clarify` 只要**返回同样形状的载荷**，就自动获得了 CLI / Web / gateway
    三端的提问能力，一行 `nodes.py` 都不用改。

    **选项必须渲染进 `message`**：`nodes.py` 只把 `reason` / `message` / `tool` 三个
    字段搬进 `waiting`，其余键会被丢掉。所以结构化字段（`options`）是给富前端看的
    补充，而**任何前端都能用的那份**是 `message` 里的 `1) … 2) …` 文本。
    """
    question = str(args.get("question") or "").strip()
    options = list(args.get("options") or [])
    allow_free_text = bool(args.get("allow_free_text", True))
    if not question:
        raise ValueError("clarify 需要 question（要用户决定什么）")
    if not 2 <= len(options) <= 6:
        raise ValueError(
            f"选项数量必须是 2-6 个，现在给了 {len(options)} 个。"
            "只有一个选项说明这不是选择题（自己决定就行）；超过 6 个请先合并。"
        )
    rendered_options = []
    lines = [f"需要你决定：{question}", "", "选项："]
    for index, option in enumerate(options, start=1):
        label = str(option.get("label") or "").strip()
        description = str(option.get("description") or "").strip()
        rendered_options.append({"index": index, "label": label, "description": description})
        lines.append(f"{index}) {label}" + (f" —— {description}" if description else ""))
    lines.append("")
    if allow_free_text:
        lines.append("回复序号即可；也可以直接用自己的话说明。")
    else:
        # 明确写死"只能选"，否则模型/用户会以为还能自由作答。
        lines.append(f"**只能从上面选一个**：请回复序号（1-{len(options)}），不接受其它回答。")
    payload: dict[str, Any] = {
        "status": "needs_user",
        "reason": "clarification",
        "message": "\n".join(lines),
        "question": question,
        "options": rendered_options,
        "allow_free_text": allow_free_text,
        "tool": "clarify",
        "notice": "拿到回答后继续原任务，不要在等待期间重复提问。",
    }
    # 投递（MATRIX 5.13）：宿主配了平台通道才真的发按钮/编号文本。
    # **没配就什么都不做**——`needs_user` 载荷照常返回，前端自己渲染，
    # 行为与之前完全一致。投递失败也不抛：用户连问题都看不到比"没发按钮"糟得多。
    labels: list[str] = [str(item["label"]) for item in rendered_options]
    delivery = clarify_delivery.deliver(
        question, labels, allow_free_text=allow_free_text
    )
    if delivery is not None:
        payload["delivery"] = delivery
    return payload


class ToolRegistry:
    def __init__(self, settings: Settings, store: Store):
        self.settings, self.store = settings, store
        self.tools = {tool.name: tool for tool in LOCAL_TOOLS}
        self.external: dict[str, Any] = {}
        self.browser: Any = None
        #: MCP 管理器引用（装配层在 MCP 启动后注入），供 skill_load 把 `mcp__*` 名
        #: 路由到远程 prompt 正文。无 MCP 时保持 None（测试/降级路径）。
        self.mcp: Any = None
        self._retriever: Any = None
        #: 后台进程登记表（`process` 工具）。**故意只放内存**：它记的是"本进程起过的
        #: 子进程"，重启之后那些进程要么已随父进程一起死了、要么成了孤儿——两种情况下
        #: 假装"还记得它"都比不记得更糟（模型会去 poll 一个不存在的 pid）。
        #: 日志本身落在 `<data>/processes/<id>.log`，重启后仍可读。
        self._processes: dict[str, dict[str, Any]] = {}
        #: 已注销但原 owner 仍需记住的工具名（`name -> source`）。
        #:
        #: 存在的唯一理由：堵住"先 unregister_source 再 register"绕过遮蔽门禁的路。
        #: 槽位被合法填回时清除，所以它只记"最近一次被摘掉的名字"，不会无限增长。
        self._shadows: dict[str, str] = {}
        self.slots = asyncio.Semaphore(TOOL_CONCURRENCY)

    def retriever(self):
        """检索器与 AgentRunner 用的是同一实现；这里惰性构造，避免启动即触库。"""
        if self._retriever is None:
            from app.retrieval import build_retriever

            self._retriever = build_retriever(self.settings, self.store)
        return self._retriever

    async def _touch_skill(self, name: str):
        """技能被加载时：同步索引、累加 usage_count、记录 last_used_at（治理输入）。

        只做索引镜像与计数，不做治理决策（stale/archived 由 record_outcome 负责）。
        同步失败只记日志，绝不影响 skill_load 本身。
        """
        try:
            from app.skills import sync_to_store
            from app.storage import Skill, now

            await sync_to_store(self.store, self.settings.data_dir / "skills")
            async with self.store.lock, self.store.sessions() as session:
                row = await session.scalar(select(Skill).where(Skill.name == name))
                if row is not None:
                    row.usage_count += 1
                    row.last_used_at = now()
                    await session.commit()
        except Exception as exc:  # 计数失败不拖垮加载
            log.warning("技能计数失败（忽略）：%s", exc)

    async def _set_skill_status(self, name: str, status: str) -> None:
        """显式覆盖表里某技能的状态（skill_save 沉淀的权威结果）。

        绕过 sync_to_store 的镜像规则：沉淀是显式动作，无论表里之前是 active/stale/
        archived（治理态或文件删了），写文件后都应按 save_skill 的声明重设为新状态。
        """
        try:
            from app.storage import Skill

            async with self.store.lock, self.store.sessions() as session:
                row = await session.scalar(select(Skill).where(Skill.name == name))
                if row is not None:
                    row.status = status
                    await session.commit()
        except Exception as exc:
            log.warning("技能状态覆盖失败（忽略）：%s", exc)

    def todo(self, task_id: str, args: dict) -> dict:
        """`todo` 的实现：读-改-写，并且**每次都回读完整清单**。

        回读不是"顺手带一下"，是这个工具在 Orbit 里的**唯一可见性机制**：清单不会
        被注入上下文（`app/graph/*` 禁改），模型只能从返回值里看到它。所以每条分支
        ——包括 `list` 之外的所有写操作——都返回同一份完整清单。
        """
        data_dir = self.settings.data_dir
        action = str(args.get("action") or "list").strip().lower()
        items = load_todos(data_dir, task_id)
        notice = ""
        if action == "add":
            incoming = [str(entry).strip() for entry in (args.get("items") or [])]
            incoming = [entry for entry in incoming if entry]
            if not incoming:
                raise ValueError("add 需要给 items：要追加的条目列表（每条形如一句可执行的小步骤）")
            for entry in incoming:
                if len(entry) > TODO_MAX_CHARS:
                    raise ValueError(
                        f"单条清单不能超过 {TODO_MAX_CHARS} 字符（这条 {len(entry)} 字符）："
                        f"{entry[:60]}…。请拆成更小的步骤，或把它写成一句摘要。"
                    )
            if len(items) + len(incoming) > TODO_MAX_ITEMS:
                raise ValueError(
                    f"单任务清单上限 {TODO_MAX_ITEMS} 条，现在已有 {len(items)} 条、"
                    f"还要加 {len(incoming)} 条。请先 complete/remove 掉已完成或不再需要的条目，"
                    "或者把任务拆成更少的步骤。"
                )
            for entry in incoming:
                items.append({"text": entry, "status": "pending"})
            notice = f"已追加 {len(incoming)} 条。"
        elif action in {"update", "complete", "remove"}:
            index = int(args.get("index") or 0)
            if not 1 <= index <= len(items):
                raise ValueError(
                    f"index 必须在 1-{len(items)} 之间（当前清单共 {len(items)} 条），收到 {index}。"
                    "序号从 1 开始；不确定就先 list 一次。"
                )
            if action == "remove":
                removed = items.pop(index - 1)
                notice = f"已删除第 {index} 条「{removed['text'][:40]}」。"
            else:
                status = "done" if action == "complete" else str(args.get("status") or "").strip()
                if status not in TODO_STATUSES:
                    raise ValueError(
                        f"status 必须是 {' / '.join(TODO_STATUSES)} 之一，收到 {status!r}。"
                    )
                items[index - 1]["status"] = status
                notice = f"第 {index} 条已标为「{TODO_STATUS_LABELS[status]}」。"
        elif action == "clear":
            notice = f"已清空 {len(items)} 条。"
            items = []
        elif action != "list":
            raise ValueError(
                f"未知 action：{action}（可选 add / update / complete / remove / list / clear）"
            )
        if action != "list":
            save_todos(data_dir, task_id, items)
        pending = sum(1 for item in items if item["status"] != "done")
        return {
            "task_id": task_id,
            "action": action,
            "count": len(items),
            "pending": pending,
            "items": [
                {"index": index, "text": item["text"], "status": item["status"]}
                for index, item in enumerate(items, start=1)
            ],
            "rendered": render_todos(items),
            "notice": (notice + " " if notice else "")
            + "清单不会自动出现在上下文里，以这次返回的完整清单为准。",
        }

    def granted(self, name: str) -> bool:
        """True when the user permanently approved this action on the site currently open."""
        if name not in GRANTABLE_TOOLS or self.browser is None:
            return False
        host = self.browser.current_host()
        if not host:
            return False
        return name in load_grants(self.settings.granted_actions).get(host, [])

    # ── 注册 / 注销 / 列举（暴露面的唯一入口，改之前先读这三段注释）────────────

    def register(
        self,
        tools: list[Tool],
        executor,
        source: str | None = None,
        override: bool = False,
    ) -> None:
        """注册一批工具。同名遮蔽门禁照 Hermes ``registry.register()`` 的语义。

        四条规则，每条都有它挡的那个具体事故：

        1. **同 source 重复注册允许**。MCP 重连 / 刷新时工具清单本就会变，重新注册是
           正常路径；拦下来等于"服务重启一次，它的工具就永远回不来"。
        2. **跨 source 同名默认拒绝，且不抛异常**——只 ``log.error`` 并跳过。不抛是因为
           注册发生在模块导入、服务连接这些启动链路上，一个插件抢名不该把 agent 带崩；
           只记日志是因为静默覆盖会让"模型调到的到底是谁"变成查不出来的问题。
        3. **``override=True`` 且操作员显式授权**才放行。两道闸分开：``override`` 是
           调用方的意图声明，``data/tools.json`` 的 ``allow_tool_override`` 是授权。
           只有意图没有授权 = 拒绝（照 Hermes 的 ``plugins.entries.*.allow_tool_override``）。
        4. **``mcp__`` 前缀豁免**。MCP 工具是动态发现的，同一服务换实现后同名不同源是
           常态，走 override 流程只会把正常刷新卡死。

        ``source`` 留空 = 沿用每个 :class:`Tool` 自带的 ``source``（MCP 就是这么传的：
        工具自带 ``mcp:<server>``）。**故意不给 ``source="builtin"`` 这种默认字符串**：
        那会把 MCP 工具全洗成 builtin，遮蔽门禁的 source 维度当场失效。
        """
        for tool in tools:
            if tool.parameters is not None and not isinstance(tool.parameters, dict):
                log.warning("工具 %s 的参数声明不是 dict，已跳过注册", tool.name)
                continue
            incoming = source or tool.source
            if incoming != tool.source:
                tool = replace(tool, source=incoming)
            if not self._may_register(tool, override=override):
                continue
            self.tools[tool.name] = tool
            self.external[tool.name] = executor
            # 槽位被合法填上后墓碑就没意义了，清掉免得下次误判。
            self._shadows.pop(tool.name, None)

    def _may_register(self, tool: Tool, *, override: bool) -> bool:
        """遮蔽门禁本体。返回 False = 这次注册被拒（**不抛异常**）。"""
        if tool.name.startswith(MCP_TOOL_PREFIX):
            return True
        existing = self.tools.get(tool.name)
        # 槽位空了也可能有主：`unregister_source` 会把原 owner 记成墓碑。
        owner = existing.source if existing is not None else self._shadows.get(tool.name, "")
        if not owner or owner == tool.source:
            return True
        if not override:
            log.error(
                "工具 %s 已被 %s 注册，来自 %s 的同名注册被拒绝（要替换请传 override=True，"
                "并在 data/tools.json 的 allow_tool_override 里授权）",
                tool.name,
                owner,
                tool.source,
            )
            return False
        if not self._override_allowed(tool.source):
            log.error(
                "工具 %s 的 override=True 被拒：%s 未获操作员授权。"
                "把 %r 加进 data/tools.json 的 allow_tool_override 列表才放行。",
                tool.name,
                tool.source,
                tool.source,
            )
            return False
        log.info(
            "工具 %s：%s 覆盖了 %s 的注册（override=True 且已授权）",
            tool.name,
            tool.source,
            owner,
        )
        return True

    def _override_allowed(self, source: str) -> bool:
        """操作员是否授权某个 source 覆盖别人已注册的工具。

        授权来源与 Hermes 的 ``allow_tool_override`` 对齐，但落在 Orbit 的数据目录里：
        ``data/tools.json`` 的 ``allow_tool_override`` 列表（支持 ``"*"`` 通配），
        另认 ``ORBIT_ALLOW_TOOL_OVERRIDE=1`` 供临时排查（不必改文件、改完记得撤销）。
        """
        if os.environ.get("ORBIT_ALLOW_TOOL_OVERRIDE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }:
            return True
        allowed = self.toolsets_config().get("allow_tool_override")
        if not isinstance(allowed, list):
            return False
        return "*" in allowed or source in allowed

    def toolsets_config(self) -> dict[str, Any]:
        """读 ``data/tools.json``（带 mtime 缓存）。坏文件按"全开"处理，见 toolsets.load_config。"""
        return toolsets.load_config(toolsets.config_path(self.settings.data_dir))

    def refresh_tool_availability(self) -> list[dict[str, Any]]:
        """跑一遍可用性探针，把结果写进 ``app/toolsets`` 的进程内缓存。

        **为什么必须有人显式调用**：``specs()`` 每轮都会算暴露面，而探针会读密钥、
        glob 浏览器目录，不能挂在每轮请求上（Hermes 的心跳也是"按需探针 + TTL 缓存"，
        不是定时器）。``toolsets.resolve()`` 只读缓存，所以探测必须由外部触发：
        ``orbit tools probe`` 或装配层在启动 / 每隔若干轮调一次这个方法。
        """
        rows = toolsets.probe_all(list(self.tools), self.settings)
        unavailable = [row["name"] for row in rows if not row["available"]]
        if unavailable:
            log.info("可用性探针：%d 个工具当前不可用（%s）", len(unavailable), "、".join(unavailable))
        return rows

    def unregister_source(self, source: str) -> None:
        """摘掉某个 source 注册的全部工具（MCP 服务下线 / 重连前清理）。

        同一道门禁：**"先删再注册"不能成为绕过遮蔽检查的后门**。``register()`` 只在
        "槽位里已有东西"时才做冲突检查，于是"先 deregister 别人的工具，再普通 register
        自己的"就能零成本抢名——Hermes 的 ``deregister()`` 注释专门点了这条路径。
        这里用墓碑（``self._shadows``）堵住：删掉的非 MCP 名字会记住原 owner，
        之后跨 source 注册同名仍然要过 override 门禁。
        ``mcp__*`` 豁免：动态发现本来就是 nuke-and-repave。
        """
        for name in [n for n, tool in self.tools.items() if tool.source == source]:
            dropped = self.tools.pop(name)
            self.external.pop(name, None)
            if not name.startswith(MCP_TOOL_PREFIX):
                self._shadows[name] = dropped.source

    def specs(
        self,
        platform: str = toolsets.DEFAULT_PLATFORM,
        *,
        budget: int | None = toolsets.LISTING_BUDGET_TOKENS,
        context: toolsets.FilterContext | None = None,
    ):
        """当前暴露给模型的工具 schema，按 ``app/toolsets.resolve()`` 过滤。

        硬要求：``platform="cli"`` 且没有 ``data/tools.json`` 时**行为与引入过滤层之前
        完全一致**（全开、同顺序）。否则既有测试与真实行为会一起漂——过滤层必须先是
        "能关"，才谈得上"该关"。

        另外做一次**schema 前置校验**（见 :func:`schema_problems`）：畸形 schema 会让
        整次请求被供应商 400 拒掉，而错误信息往往只说"第 N 个工具参数非法"，很难定位。
        坏工具**排除并记 error 日志**——排除一个工具比让整轮对话发不出去损失小得多，
        而日志会点名是哪个。

        ## 分级披露（MATRIX 5.11）与 4,000 token 预算：能做什么、不能做什么

        ``budget`` 控制**描述这一维**的披露量，规则是 :func:`_trim_specs`：

        1. Tier 0（:data:`toolsets.ALWAYS_EAGER` 里的核心动作）**永不裁剪**；
        2. 超预算时先把 Tier 2 的描述压成一句，仍超再压 Tier 1；
        3. **参数 schema 一律不动**。

        第 3 条是这张预算表最容易被误读的地方，所以写清楚：**4,000 token 不是这份
        清单能达到的硬上限**。38 个内置工具的参数 schema 本身就约 4,300 token
        （实测 ``estimate_tokens`` 在"描述全删"的地板上仍 > 4,000），而 schema 是
        **调用契约**——删了它，模型不是"晚点知道"，而是**永远调不动这个工具**。
        把契约当预算裁掉，换来的是一个看起来达标的数字和一个不会用工具的模型。

        所以这里的诚实口径是：``budget`` 压的是描述，压到底仍然超出时
        :func:`_trim_specs` 会返回 ``over_budget=True``，由调用方如实报出来。
        真正的出路不是继续裁 schema，而是**延迟工具本身**——那是
        ``app/tool_discovery.py``（懒发现）与 ``tool_search`` 的职责：
        Tier 2 的工具可以根本不进首屏，等模型搜到再 ``ensure_loaded``。

        ``budget=None`` = 不裁剪（旧行为，供对照与测试用）。
        ``context`` 传入时走四层过滤（MATRIX 6.2）；留空只有平台层生效。
        """
        exposed = set(
            toolsets.resolve(
                list(self.tools), platform, self.toolsets_config(), context=context
            )
        )
        result = []
        for name, tool in self.tools.items():
            if name not in exposed:
                continue
            spec = tool.specification()
            problems = schema_problems(spec)
            if problems:
                log.error("工具 %s 的 schema 不合法，已从暴露面移除：%s", name, "；".join(problems))
                continue
            result.append(spec)
        if budget is None:
            return result
        trimmed, _over = _trim_specs(result, budget)
        return trimmed

    #: 懒加载开关（MATRIX 5.2）。默认开；关掉就退回「全部启动时注册」的旧行为。
    lazy_load: bool = True

    def _lazy_tool(self, name: str) -> Any:
        """按索引试着把 `name` import 进注册表；拿不到就返回 None（不抛）。

        **为什么吞掉异常**：这里处在「未知工具」的兜底路径上，原始错误
        「未知工具：x」才是用户该看到的；懒加载失败只是补充信息。
        把 `DiscoveryError` 抛出去会让一个拼错的工具名变成一段索引报错。
        """
        if not self.lazy_load:
            return None
        from app import tool_discovery

        try:
            loaded = tool_discovery.ensure_loaded(name, self)
        except Exception as exc:  # noqa: BLE001 —— 见 docstring
            log.debug("懒加载 %s 失败（按未知工具处理）：%s", name, exc)
            return None
        if not loaded:
            return None
        return self.tools.get(name)

    def validate(self, name: str, args: dict) -> dict:
        tool = self.tools.get(name)
        if tool is None:
            # 懒加载（MATRIX 5.2）：模型报了一个当前注册表里没有的工具名时，
            # 先试着按索引把它 import 进来再查一次。
            #
            # **之前 `ensure_loaded()` 零外部调用方**——`tools_extra3.py` 只用
            # `discover` / `describe` / `index_path` 三个报告函数，所以「按需 import」
            # 这件事从没发生过：工具全在启动时一次性 import 完。
            #
            # **能做什么、不能做什么（如实说）**：
            # 只有 `LOCAL_MODULES`（`app.tools`）里的工具能在没有执行器的情况下
            # 进注册表并由 `ToolRegistry.local` 分发；其它模块（`app.browser` /
            # `app.web` / `app.connectors`）的工具**必须由装配层传 executor**，
            # 本方法拿不到——**编一个假执行器只会换来"工具调了但什么也没发生"，
            # 比报错难查得多**，所以那种情况如实报原因，不假装加载成功。
            tool = self._lazy_tool(name)
        if tool is None:
            raise ValueError(f"未知工具：{name}")
        if tool.parameters is not None:
            # External contract: only check the shape we must send.
            if not isinstance(args, dict):
                raise ValueError("参数必须是 JSON 对象")
            return args
        if tool.schema is None:
            raise ValueError(f"{name} 没有参数定义")
        return tool.schema.model_validate(args).model_dump()

    async def execute(self, task_id: str, operation_id: str, name: str, args: dict, messages=None):
        """唯一工具分发点。`messages` 是本轮上下文（只有需要读用户原话的工具会用到）。"""
        try:
            args = self.validate(name, args)
        except ValueError as exc:
            return {"status": "error", "error": str(exc)[:1000]}
        previous = await self.store.get(Operation, operation_id)
        if previous and previous.status == "DONE":
            # 幂等短路：同一 operation_id 只真正执行一次。
            # 注意 `refused` 不在此列（它落的是 WAITING）——拒绝是"当时不该写"，
            # 不是"这件事已经做完了"，不能用缓存把拒绝态钉死。
            return previous.result
        operation = previous or Operation(id=operation_id, task_id=task_id, name=name, args=args)
        await self.store.save(operation)
        await self.store.event(task_id, "tool_started", tool=name)
        try:
            async with self.slots:
                if name in self.external:
                    result = await self.external[name](name, args, task_id, operation_id)
                else:
                    data = await self.local(name, args, task_id, messages)
                    # `clarify` 这类元工具要"把问题交回给用户"，而图节点只认
                    # `status=needs_user/uncertain`（见 app/graph/nodes.py:execute）。
                    # 本地工具正常只会被包成 {"status": "ok", "data": ...}，所以这里
                    # 让"自带 status 的载荷"原样穿过——这是不改 nodes.py 就能复用
                    # ask_user 那条 interrupt 通路的唯一口子。
                    if isinstance(data, dict) and data.get("status") in {
                        "needs_user",
                        "uncertain",
                    }:
                        result = data
                    else:
                        result = {"status": "ok", "data": data}
            if isinstance(result, dict):
                # 把工具名挂在结果上：`as_tool_text` 是模型侧唯一漏斗，而它拿不到
                # 工具名（调用点是禁改的 nodes.py）。来源判断（是否要包"不可信数据"
                # 标记）只能靠这个字段，序列化前会被剥掉，不进模型上下文。
                result.setdefault(TOOL_SOURCE_KEY, name)
                # 同理挂上 spill 需要的落点信息（MATRIX 5.7）：`as_tool_text` 只拿得到
                # 结果本身，拿不到 task_id / data_dir。**这一处挂、那一处剥**，成对出现。
                result.setdefault(
                    TOOL_SPILL_KEY,
                    {"task_id": task_id, "data_dir": str(self.settings.data_dir)},
                )
        except ExecutionRefused as exc:
            # 执行前裁决的拒绝（tirith 组合扫描 / egress 出站策略）。载荷形状照
            # 需求方约定：`{"status": "refused", "error": ...}`，**不带 data**——
            # 带 data 会让模型以为"有结果但被拒"，从而拿半份结果继续编。
            result = {"status": "refused", "error": str(exc)[:2000]}
        except MemoryWriteRefused as exc:
            result = {
                "status": "refused",
                "saved": "",
                "error": str(exc)[:1000],
                "notice": str(exc)[:1000],
            }
        except Exception as exc:
            result = {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:600]}"}
        operation.status = (
            "DONE" if result["status"] not in {"needs_user", "uncertain", "refused"} else "WAITING"
        )
        operation.result = result
        await self.store.save(operation)
        await self.store.event(task_id, "tool_finished", tool=name, result=result)
        return result

    # ── 执行前的两道闸（tirith 组合扫描 / egress 出站策略）──────────────────

    async def _tirith_gate(self, task_id: str, tool: str, text: str) -> None:
        """执行前的**组合**风险扫描（MATRIX 14.4 的接线点）。

        ## 为什么 tirith 排在 approvals 之前

        两层判的不是同一件事，顺序也不是随便定的：

        - `tirith` 看**组合**：`curl … | sh`、`find -exec rm`、`echo $DATA | base64 -d | bash`。
          单看每一段都无害，合起来才是入侵脚本的形状。它的语义**更宽**（跨管道、
          跨 `&&`/`;`、跨命令替换），所以先跑：先按"整条命令像不像攻击链"过一遍。
        - `approvals` 看**单条命令的硬地板**：`mkfs`、`diskpart`、`rm -rf /`。
          它逐条匹配危险模式，回答的是"这条命令本身会不会把机器搞坏"。

        反过来（approvals 先）会留下一个真实的洞：`curl x | sh` 里 approvals 的
        `pipe_to_shell` 只在 smart/manual 模式下拦，而 `off`（Orbit 默认）直接放行——
        于是组合攻击在默认配置下**完全不设防**。tirith 不受审批模式影响（它有自己的
        `tirith_enabled` / `tirith_block_severity`），所以它必须在前。

        顺序也不改变"谁能拦"：任一层拒绝都会让命令不执行。先跑 tirith 只是让
        "组合攻击"的拒绝信息先出现——那才是用户真正需要看懂的那条。
        """
        result = tirith.scan(text, settings=self.settings)
        if not result.blocked:
            return
        kinds = [finding.kind for finding in result.findings]
        # 被拦了要能从事件流里查到：只回一句 error，事后复盘只能靠猜"当时拦的是什么"。
        if self.store is not None:
            try:
                await self.store.event(
                    task_id,
                    "tirith_blocked",
                    tool=tool,
                    kinds=kinds,
                    severity=result.severity,
                    block_at=result.block_at,
                    excerpt=(text or "")[:200],
                )
            except Exception as exc:  # 事件落库失败不能把拒绝变成放行
                log.warning("tirith 拒绝事件落库失败（忽略）：%s", exc)
        raise ExecutionRefused(tirith.refusal(result))

    def _egress_gate(self, command: str) -> None:
        """出站前检查：`curl` / `wget` 的目标 host 过一遍 egress 策略。

        **只接 run_command，不接 web 工具**：`app/web.py` 不归本轮改动，它自己的
        `url_safety` 闸管的是"别连本机/内网/元数据地址"，与本层的"这个 host 允不允许
        出站"是两件事。这里能做的是"命令行里**显式写出**的目标 host"，所以它对
        `curl https://…` 有效，对 `curl $(cat u)` 无效——如实写在 `app/egress.py` 的
        模块 docstring 里，不在这里假装是防火墙。

        没有策略文件时 `check()` 一律返回 allow（`default=allow`），这一步对既有行为
        零影响。
        """
        hosts = egress.command_hosts(command)
        if not hosts:
            return
        policy = egress.EgressPolicy.load(self.settings.data_dir)
        for host in hosts:
            decision = policy.check(host)
            if decision.allowed:
                continue
            raise ExecutionRefused(
                f"出站被 egress 策略拦下：{host}（{decision.action}）。{decision.reason}。"
                f"确认要放行就执行 `orbit egress allow {host}`（或写进 data/egress.json）。"
            )

    async def _approve_command(
        self, task_id: str, tool: str, text: str, *, egress_check: bool = True
    ) -> None:
        """执行前裁决的统一入口：tirith 组合扫描 → approvals 两级裁决 → egress 出站策略。

        抽成一个方法而不是在 `run_command` / `code_execution` / `process` 里各抄一遍：
        这三条路都能把机器搞坏，抄三份必然漂移，而**漂移的安全边界比没有更危险**
        （`app/tools.py` 里 `dangerous_command` 那段注释已经为同一件事留过话）。

        顺序的语义见 :meth:`_tirith_gate` 的 docstring，改顺序前先读它。

        `egress_check` 只对 `code_execution` 关掉：代码正文里的 host 不是"这条命令
        要连的目标"（代码可能只是**打印**了一个 URL），按命令行口径解析会误拦。
        """
        await self._tirith_gate(task_id, tool, text)
        mode = approvals.resolve_mode(self.settings)
        allowlist: tuple[str, ...] = ()
        if mode != "off":
            allowlist = approvals.AllowList.load(
                Path(self.settings.data_dir) / "approvals.json"
            ).rules
        verdict = approvals.classify(
            text,
            mode=mode,
            allowlist=allowlist,
            env_type="local",
            hardline_in_machine=approvals.resolve_hardline_in_machine(self.settings),
            machine_wide=self.settings.machine_wide,
        )
        if verdict.verdict == approvals.VERDICT_ASK:
            # 三态里的 `ask` 到这里才真的去问人（MATRIX 14.1）。
            #
            # 为什么放在 deny 判定**之前**：`ask` 不是拒绝，是"我需要一个人来决定"。
            # 如果先按 deny 处理，`smart` / `manual` 两档就退化成"一律拒绝"，
            # 用户会以为审批模式坏了。
            #
            # 通道选择与超时语义都在 `approval_channel` 里：无 TTY / 无人值守一律
            # **deny**，且超时是第三态 `timeout`——把"超时"说成"你拒绝了"，
            # 用户会以为自己点过拒绝，然后去找一个不存在的操作记录。
            #
            # `pre_approval_request` / `post_approval_response` 发射点（MATRIX 13.5）：
            # 审批审计是**事后追责的唯一凭据**——谁在什么时候批准了哪条危险命令。
            # 之前这两个事件只有名字、没有发射点，于是审批过程在观测端完全不可见。
            try:
                hooks.emit(
                    "pre_approval_request",
                    {
                        "task_id": task_id,
                        "tool": tool,
                        "command": text,
                        "mode": mode,
                        "reason": str(getattr(verdict, "reason", "") or ""),
                    },
                )
            except Exception as exc:  # noqa: BLE001 —— 旁路，不该拦审批
                log.warning("pre_approval_request 钩子出错（已忽略）：%s", exc)
            decision = approval_channel.approve_or_refuse(
                text,
                mode=mode,
                settings=self.settings,
                task_id=task_id,
                reason=str(getattr(verdict, "reason", "") or ""),
            )
            try:
                hooks.emit(
                    "post_approval_response",
                    {
                        "task_id": task_id,
                        "tool": tool,
                        "allowed": bool(decision.allowed),
                        # 三态（allow / deny / timeout）原样上报——**不要压成布尔**，
                        # 否则「超时」会被读成「用户拒绝」，而那两件事的处置完全不同。
                        "outcome": str(decision.outcome),
                        "reason": str(decision.reason),
                    },
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("post_approval_response 钩子出错（已忽略）：%s", exc)
            if not decision.allowed:
                raise ValueError(
                    f"审批未通过（{decision.outcome}）：{decision.reason}"
                )
        elif verdict.verdict != approvals.VERDICT_ALLOW:
            raise ValueError(_refusal(verdict, self.settings.machine_wide, mode))
        if egress_check:
            self._egress_gate(text)

    # ── 后台进程（`process` 工具）────────────────────────────────────────────

    async def manage_processes(self, args: dict, task_id: str) -> dict:
        """`process` 的实现：start / list / poll / kill。

        为什么需要它：`run_command` 是**同步**的——一条 `npm run dev` 或整套测试会把
        这一轮对话卡到超时上限。要"起一个长任务、过一会儿看它跑到哪"，就必须有个地方
        记着 pid。这里的选择是**进程内登记表 + 落盘日志**：

        * 日志写 `<data>/processes/<id>.log`，进程输出**直接重定向到文件**而不是管道——
          管道要有人一直读，读的人一走进程就阻塞（或者缓冲区写满），后台任务会假死；
        * 登记表只在内存里：重启后 `list` 是空的，如实告诉模型"没记着"，而不是让它去
          poll 一个早已不存在的 pid；
        * `start` 走与 `run_command` **完全同一套闸**（tirith → approvals → egress），
          否则后台执行就成了绕开审批的后门。
        """
        action = str(args.get("action") or "list").strip().lower()
        if action == "start":
            command = str(args.get("command") or "").strip()
            if not command:
                raise ValueError("start 需要给 command：要放到后台跑的那条命令")
            await self._approve_command(task_id, "process", command)
            root = self.settings.workspace.resolve()
            machine = self.settings.machine_wide
            start = str(args.get("cwd") or "").strip()
            if machine:
                cwd = resolve_path(root, start or str(Path.home()), True)
            else:
                cwd = resolve_path(root, start or ".", False)
            if not cwd.is_dir():
                raise ValueError(f"工作目录不存在：{cwd}")
            # 起进程是阻塞调用（Popen 本身很快，但开文件/建目录要碰盘），扔线程里做。
            return await asyncio.to_thread(self._spawn_background, command, cwd)

        if action == "list":
            rows = [self._describe_process(pid, entry) for pid, entry in self._processes.items()]
            return {
                "count": len(rows),
                "processes": rows,
                "notice": (
                    "登记表只在内存里：重启后这里会是空的（日志文件仍留在数据目录）。"
                    if rows
                    else "当前没有本进程起过的后台任务。用 action=start 起一个。"
                ),
            }

        process_id = str(args.get("process_id") or "").strip()
        entry = self._processes.get(process_id)
        if entry is None:
            raise ValueError(
                f"没有 process_id={process_id!r} 的后台进程。先 action=list 看有哪些在跑"
                "（重启后登记表会清空，这是预期的）。"
            )
        if action == "poll":
            row = self._describe_process(process_id, entry)
            row["output"] = self._process_output(entry, int(args.get("tail") or PROCESS_POLL_TAIL))
            if not row["running"]:
                row["notice"] = (
                    f"进程已结束（exit_code={row['exit_code']}）。output 是日志尾部，"
                    f"完整日志在 {row['log_path']}。"
                )
            return row
        if action == "kill":
            process = entry["process"]
            was_running = process.poll() is None
            terminate_tree(process)
            self._close_process_log(entry)
            row = self._describe_process(process_id, entry)
            row["killed"] = was_running
            row["notice"] = (
                "已终止整棵进程树。" if was_running else "这个进程本来就已经结束了，什么都没做。"
            )
            return row
        raise ValueError(f"未知 action：{action}（可选 start / list / poll / kill）")

    def _spawn_background(self, command: str, cwd: Path) -> dict:
        """真的起一个后台进程。同步实现，由 :meth:`manage_processes` 放进线程跑。"""
        directory = Path(self.settings.data_dir) / PROCESS_LOG_SUBDIR
        directory.mkdir(parents=True, exist_ok=True)
        process_id = f"proc-{uuid.uuid4().hex[:8]}"
        log_path = directory / f"{process_id}.log"
        handle = log_path.open("wb")
        try:
            child = subprocess.Popen(
                command,
                shell=True,
                cwd=str(cwd),
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            handle.close()
            raise ValueError(f"后台进程起不来：{exc}") from exc
        self._processes[process_id] = {
            "process": child,
            "command": command,
            "cwd": str(cwd),
            "log": log_path,
            "handle": handle,
            "started": time.time(),
        }
        log.info("后台进程已启动：%s pid=%s", process_id, child.pid)
        return {
            "process_id": process_id,
            "pid": child.pid,
            "command": command,
            "cwd": str(cwd),
            "log_path": str(log_path),
            "running": True,
            "notice": (
                "已在后台启动，**不会阻塞这一轮**。过一会儿用 action=poll 读输出与退出码；"
                "不再需要时用 action=kill 收掉它。"
            ),
        }

    def _describe_process(self, process_id: str, entry: dict[str, Any]) -> dict[str, Any]:
        """一条后台进程的当前状态（`list` / `poll` / `kill` 共用同一份字段）。"""
        process = entry["process"]
        code = process.poll()
        if code is not None:
            self._close_process_log(entry)
        return {
            "process_id": process_id,
            "pid": process.pid,
            "command": entry["command"],
            "cwd": entry["cwd"],
            "running": code is None,
            "exit_code": code,
            "seconds": round(time.time() - float(entry["started"]), 2),
            "log_path": str(entry["log"]),
        }

    @staticmethod
    def _close_process_log(entry: dict[str, Any]) -> None:
        """进程结束后关掉日志句柄（重复调用安全）。不关的话文件句柄会一直占着。"""
        handle = entry.get("handle")
        if handle is None:
            return
        try:
            handle.close()
        except OSError:  # 已经关了 / 被系统回收：不该影响状态查询
            pass
        entry["handle"] = None

    @staticmethod
    def _process_output(entry: dict[str, Any], tail: int) -> str:
        """读日志的尾巴。**每次 poll 都重新读盘**，因为后台进程还在往文件里写。"""
        path = Path(entry["log"])
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        limit = max(0, int(tail))
        return text[-limit:] if limit and len(text) > limit else text

    async def local(self, name: str, args: dict, task_id: str, messages=None):
        root = self.settings.workspace.resolve()
        machine = self.settings.machine_wide
        if name == "todo":
            return self.todo(task_id, args)
        if name == "clarify":
            return clarify_payload(args)
        if name == "apply_patch":
            # 补丁**永远**按"仅工作文件夹"的边界解析（见 app/patch.py::_resolve）：
            # 它是批量、不可见的写入，风险比单次写文件高一个量级。
            from app.patch import parse_and_apply

            outcome = await asyncio.to_thread(parse_and_apply, args["patch"], root)
            if not outcome.ok:
                raise ValueError(outcome.error)
            return {
                "applied": outcome.applied,
                "summary": outcome.summary(),
                "verified": all(
                    (root / item["path"]).exists() or item["op"] == "delete"
                    for item in outcome.applied
                ),
            }
        if name == "local_list_files":
            if machine and str(args["path"]).strip() in {"", "."}:
                # 完全访问档下 "." 的语义是"从整机开始看"。把盘符摆出来当入口，
                # 否则模型只看得到工作目录，这个权限级别就形同虚设。
                return [{"path": str(drive), "directory": True} for drive in machine_roots()]
            path = resolve_path(root, args["path"], machine)
            entries = []
            for item in sorted(path.iterdir()):
                if machine or item.resolve().is_relative_to(root):
                    entries.append(
                        {"path": display_path(item, root, machine), "directory": item.is_dir()}
                    )
                if len(entries) >= 100:
                    break
            return entries
        if name == "local_read_document":
            return await asyncio.to_thread(read_document, root, args["path"], machine)
        if name == "local_read_file":
            return await asyncio.to_thread(
                functools.partial(
                    read_file_page,
                    root,
                    args["path"],
                    offset=int(args.get("offset") or 1),
                    limit=int(args.get("limit") or READ_FILE_DEFAULT_LINES),
                    machine=machine,
                )
            )
        if name == "local_search_files":
            return await asyncio.to_thread(
                functools.partial(
                    search_files,
                    root,
                    pattern=args["pattern"],
                    target=str(args.get("target") or "content"),
                    path=str(args.get("path") or ""),
                    file_glob=str(args.get("file_glob") or ""),
                    output_mode=str(args.get("output_mode") or "content"),
                    context=int(args.get("context") or 0),
                    limit=int(args.get("limit") or 50),
                    machine=machine,
                )
            )
        if name == "local_search_text":
            start = str(args["path"]).strip()
            if machine and not start:
                # 不让它在整机上无目的地 rglob：系统目录动辄几十万文件，
                # 还会一路吃 PermissionError。要搜哪里必须说清楚。
                raise ValueError("完全访问权限下搜索需要指定起始目录（如 D:\\notes）")
            base = resolve_path(root, start or ".", machine)
            if not base.is_dir():
                raise ValueError("搜索起点必须是目录")
            query = args["query"].casefold()

            def search():
                matches: list[dict] = []
                for index, path in enumerate(walk_text_files(base, machine, root)):
                    if index > 1000 or len(matches) >= 30:
                        break
                    try:
                        text = extract_text(path)[:16000]
                    except (OSError, UnicodeError, ValueError):
                        continue
                    position = text.casefold().find(query)
                    if position >= 0:
                        matches.append(
                            {
                                "path": display_path(path, root, machine),
                                "excerpt": text[max(0, position - 100) : position + 500],
                            }
                        )
                return matches

            return await asyncio.to_thread(search)
        if name == "local_write_report":
            path = resolve_path(root, args["path"], machine)
            if path.suffix.lower() != ".md":
                raise ValueError("报告必须为 .md 文件")
            content = args["content"]
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                if path.read_text(encoding="utf-8") != content:
                    raise ValueError("文件已存在且内容不同，请选择新文件名")
            else:
                with path.open("x", encoding="utf-8") as output:
                    output.write(content)
            artifact_id = hashlib.sha256(f"{task_id}:{path}".encode()).hexdigest()[:32]
            await self.store.save(
                Artifact(
                    id=artifact_id,
                    task_id=task_id,
                    kind="report",
                    location=str(path),
                    label=path.name,
                )
            )
            await self.store.event(
                task_id, "artifact_created", artifact_id=artifact_id, path=str(path)
            )
            return {
                "path": str(path),
                "artifact_id": artifact_id,
                "verified": path.read_text(encoding="utf-8") == content,
            }
        if name == "local_write_file":
            # 两档都可用，差别只在"能写到哪"与"要不要防误覆盖"：
            #   窄档 = 授权目录内 + 不碰工作台自身文件 + 覆盖既有文件要显式确认；
            #   完全访问档 = 整机任意路径、不加任何前置条件（用户要的就是"不设限制"）。
            try:
                path = resolve_path(root, args["path"], machine)
            except ValueError as exc:
                if machine:
                    raise
                # 拒绝信息要把"怎么才能做"讲清楚，否则模型会换个参数反复重试。
                raise ValueError(
                    f"{exc}。工作目录内可以直接写；要写工作目录之外，"
                    "请在对话页把权限级别切到「完全访问权限」。"
                ) from exc
            if path.is_dir():
                raise ValueError("目标是目录，不能作为文件写入")
            content = args["content"]
            existed = path.exists()
            if not machine:
                # 工作台自己的数据库与配置文件不交给工具改写：一次误写就能把会话历史、
                # 记忆、技能和本机密钥索引一起打坏。**只保护窄档**——完全访问档刻意不设限。
                protected = {
                    (self.settings.data_dir / item).resolve()
                    for item in ("app.sqlite", "checkpoints.sqlite", "settings.json")
                }
                if path in protected:
                    raise ValueError("这是工作台自身的数据库或配置文件，不能由工具改写")
                if existed and not args["overwrite"]:
                    raise ValueError(
                        "文件已存在；确认要改写既有内容请带 overwrite=true 再调用一次"
                    )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return {
                "path": str(path),
                "bytes": len(content.encode("utf-8")),
                "overwritten": existed,
                "verified": path.read_text(encoding="utf-8") == content,
            }
        if name == "run_command":
            command = str(args["command"]).strip()
            if not command:
                raise ValueError("命令不能为空")
            start = str(args.get("cwd") or "").strip()
            # 执行前裁决四道，顺序有语义（改顺序前先读 `_tirith_gate` 的 docstring）：
            #   ① tirith 组合扫描——跨管道/跨 `&&` 的组合语义，不受审批模式影响；
            #   ② approvals 单条命令的两级裁决（硬地板 / 危险模式），判据在 `app/approvals.py`；
            #   ③ egress 出站策略——`curl`/`wget` 的目标 host 允不允许出去；
            #   ④ 交给终端后端真正执行。
            # 前三道统一收在 `_approve_command` 里（`process` 复用同一套，否则后台
            # 就成了绕开审批的后门）。**两档都走 ①②**：硬地板是否在完全访问档也生效
            # 由 `hardline_in_machine` 决定（默认关，尊重 Orbit 既有的决策）。
            await self._approve_command(task_id, "run_command", command)
            if machine:
                # 完全访问档：不限工作目录，默认落用户主目录（比工作目录更"像整机"）。
                cwd = resolve_path(root, start or str(Path.home()), True)
            else:
                cwd = resolve_path(root, start or ".", False)
            if not cwd.is_dir():
                raise ValueError(f"工作目录不存在：{cwd}")
            timeout = int(args.get("timeout") or self.settings.command_timeout or 120)
            # 终端后端抽象（MATRIX 5.18）：默认 `local`，与引入这一层之前逐字一致
            # （`TerminalResult.as_dict()` 的键名/取值就是原 `run_shell` 的返回）。
            # 后端不可用时 `run()` 抛 ValueError，错误信息里带原因（缺 wsl.exe / 容器没起等）。
            backend = terminal.resolve_backend(None, self.settings)
            # `functools.partial` 而不是直接传 `backend.run` + 关键字：`asyncio.to_thread`
            # 的泛型推断在同一作用域里会被上面那次 `to_thread(parse_and_apply, …)`
            # 带偏（mypy 把返回类型解成 `ApplyResult`）。包一层是最省事的确定性写法。
            runner = functools.partial(backend.run, command, cwd=cwd, timeout=timeout)
            # 变量名**不复用**上面的 `outcome`：mypy 会拿第一次赋值的类型
            # （`ApplyResult`）当上下文去反推 `to_thread` 的泛型，复用名字就会连带推断出错。
            terminal_result = await asyncio.to_thread(runner)
            return terminal_result.as_dict()
        if name == "process":
            return await self.manage_processes(args, task_id)
        if name == "code_execution":
            code = str(args["code"])
            start = str(args.get("cwd") or "").strip()
            # 与 run_command 同一套闸（顺序也一致）：组合扫描在前、硬地板在后。
            # 代码正文也过 approvals：`os.system("rm -rf /")` 正是这条链要拦的形状。
            # 代价是"字符串里恰好出现同类字面量"会误拦，拒绝信息里会点名命中的规则，
            # 用户可以改写代码或拆成两步——比放过一条真破坏命令好。
            # 唯一的差别是不跑 egress：代码里的 host 不是"这条命令要连的目标"。
            await self._approve_command(task_id, "code_execution", code, egress_check=False)
            if machine:
                cwd = resolve_path(root, start or str(Path.home()), True)
            else:
                cwd = resolve_path(root, start or ".", False)
            if not cwd.is_dir():
                raise ValueError(f"工作目录不存在：{cwd}")
            timeout = int(args.get("timeout") or code_exec.DEFAULT_TIMEOUT)
            # 同上：包一层 `partial`，且**不复用** `runner` 这个名字（同一个作用域里
            # 两个不同的返回类型共用变量名会把 mypy 的泛型推断搅在一起）。
            code_runner = functools.partial(
                code_exec.run_code,
                code,
                language=str(args.get("language") or "python"),
                timeout=timeout,
                cwd=cwd,
            )
            code_result = await asyncio.to_thread(code_runner)
            return code_result.as_dict()
        if name == "memory_search":
            memories = await self.store.rows(select(Memory).where(Memory.user_id == "local"))
            return [
                serialize(m)
                for m in memories
                if args["query"].casefold() in (m.key + m.value).casefold()
            ][:10]
        if name == "memory_save":
            # M2 写入门槛：只认用户的显式指令。模型自己想沉淀一律拦下。
            # 判据放在 harness 侧（不是提示词里），因为提示词挡不住长任务里的自我发挥。
            allowed, refusal = memory_write_allowed(latest_user_text(messages))
            if not allowed:
                raise MemoryWriteRefused(refusal)
            # 写入门禁（MATRIX 9.12）：威胁扫描 → 审批门 → 快照期二次扫描 → 单条超限。
            #
            # 为什么在 M2 判据**之后**：M2 问的是"用户有没有要求记住"，门禁问的是
            # "内容本身能不能进库"。两个问题都要过，但先答便宜的、语义更根本的那个。
            #
            # 为什么这里传 `backend=None`（跳过 round-trip 漂移检查）：那一步会**写回一次
            # 全库**，而 `memory_save` 只是写一条。为了检查一条新记录去重写整库，风险和
            # 收益不成比例——漂移防护留给 `memory batch`（那里本来就是整批写）。
            check = await memory_write.check_write(
                {args["key"]: args["value"]},
                mode=approvals.resolve_mode(self.settings),
                data_dir=self.settings.data_dir,
            )
            if not check.ok:
                # 抛异常而不是返回 dict：`execute()` 会把本地工具的 dict 统一包成
                # `{"status": "ok"}`，一次被拒的写入会被显示成成功（这个坑踩过）。
                raise MemoryWriteRefused(f"记忆写入被拒（{check.stage}）：{check.reason}")
            memory, merged_from = await self.store.set_memory(
                args["key"], args["value"], task_id
            )
            # 镜像到插件 provider（MATRIX 13.4）。**必须在这里**——即 `MEMORY.md`
            # 已经过完三段闸并写成功之后。
            #
            # **顺序不能反**：`MEMORY.md` 是权威，provider 是镜像。先写权威，
            # 成功了才镜像；镜像失败只记账、不回滚——否则一个可选插件就能阻止
            # 核心记忆写入，方向就错了。
            memory_plugin_bridge.mirror(memory.key, str(args["value"]))
            payload = serialize(memory)
            if merged_from:
                # 如实告诉模型发生了合并，让它知道键名已被归一，避免下次再换写法
                payload["merged_into"] = memory.key
                payload["notice"] = (
                    f"已合并到已有记忆「{memory.key}」（你这次给的是「{args['key']}」）。"
                    "以后同一条信息请复用这个键名。"
                )
            return payload
        if name == "memory_delete":
            pattern = str(args["key"]).strip()
            async with self.store.lock, self.store.sessions() as session:
                if pattern:
                    # 支持部分匹配：模型很难一字不差复述 key（实测常带 run_id 后缀），
                    # 精确匹配会让清理变得不可能。先按包含匹配，命中多条一并删。
                    memories = list(
                        (
                            await session.scalars(
                                select(Memory).where(Memory.key.contains(pattern, autoescape=True))
                            )
                        ).all()
                    )
                else:
                    memories = []
                if not memories:
                    return {
                        "deleted": 0,
                        "keys": [],
                        "notice": (
                            f"没有键名包含「{pattern}」的记忆。先调用 memory_search 查看现有记忆。"
                        ),
                    }
                keys = [m.key for m in memories]
                for memory in memories:
                    await session.delete(memory)
                await session.commit()
            return {"deleted": len(keys), "keys": keys}
        if name == "history_search_tasks":
            tasks = await self.store.rows(
                select(Task)
                .where(Task.goal.contains(args["query"], autoescape=True), Task.id != task_id)
                .order_by(Task.created_at.desc())
                .limit(10)
            )
            return [
                {"id": t.id, "goal": t.goal, "status": t.status, "created_at": t.created_at}
                for t in tasks
            ]
        if name == "retriever_search":
            from app.retrieval import format_hits

            task = await self.store.get(Task, task_id)
            scope = args.get("scope") or "task"
            # 每任务检索次数上限：防止模型用"再查一次"替代思考，把延迟和成本拉爆。
            # 计次走 Operation 表（本工具的调用已由 registry.execute 落库），重启后依然有效。
            used = len(
                await self.store.rows(
                    select(Operation).where(
                        Operation.task_id == task_id, Operation.name == "retriever_search"
                    )
                )
            )
            if used > RETRIEVER_CALL_LIMIT:
                return {
                    "hits": [],
                    "count": 0,
                    "scope": scope,
                    "query": args["query"],
                    "notice": (
                        f"本次任务的检索次数已达上限（{RETRIEVER_CALL_LIMIT} 次），"
                        "请基于已经拿到的片段作答；确实缺信息就说明缺什么。"
                    ),
                }
            hits = await self.retriever().search(
                args["query"],
                scope=scope,
                conversation_id=getattr(task, "conversation_id", ""),
                source_type=args.get("source_type") or "",
                top_k=int(args.get("top_k") or 5),
            )
            return format_hits(args["query"], scope, hits)

        if name == "history_get_task":
            task = await self.store.get(Task, args["task_id"])
            if not task:
                raise ValueError("历史任务不存在")
            artifacts = await self.store.rows(select(Artifact).where(Artifact.task_id == task.id))
            return {
                "goal": task.goal,
                "status": task.status,
                "artifacts": [serialize(a) for a in artifacts],
            }
        if name == "session_search":
            from app.session_search import SessionSearch

            searcher = SessionSearch(self.store)
            message_id = str(args.get("message_id") or "").strip()
            if message_id:
                # scroll：把命中点前后的对话展开。搜索给的是"锚点"，锚点周围才是
                # 人真正要看的东西（一句话离开上下文经常读不出意思）。
                view = await searcher.anchored_view(
                    message_id, window=int(args.get("window") or 5)
                )
                # `anchored_view` 的 anchor 形状是给模型看的消息视图，**不含**
                # conversation_id，所以"这是哪场对话"要自己查一次——后续想继续
                # 在这条会话里搜（session_id）时，这个值就是唯一的抓手。
                anchor_row = await self.store.get(Message, message_id)
                return {
                    "mode": "scroll",
                    "session_id": anchor_row.conversation_id if anchor_row is not None else "",
                    "anchor": view["anchor"],
                    "window": view["window"],
                    "before": view["before"],
                    "after": view["after"],
                }
            query = str(args.get("query") or "").strip()
            if query:
                anchors = await searcher.discover(
                    query,
                    conversation_id=str(args.get("session_id") or "").strip() or None,
                    limit=int(args.get("limit") or 3),
                )
                found: dict[str, Any] = {
                    "mode": "search",
                    "query": query,
                    "count": len(anchors),
                    "anchors": [anchor.as_dict() for anchor in anchors],
                }
                if anchors:
                    found["notice"] = (
                        "命中点是**锚**，不是全部上下文：要看那一段对话请带 message_id "
                        "再调一次（mode=scroll）。"
                    )
                else:
                    found["notice"] = (
                        f"没有会话消息命中「{query}」。换个关键词试试，"
                        "或留空 query 先列出最近几场对话。"
                    )
                return found
            sessions = await searcher.browse(limit=max(1, int(args.get("limit") or 3)))
            return {
                "mode": "browse",
                "count": len(sessions),
                "sessions": sessions,
                "notice": (
                    "这是最近几场对话（不含关键词）。想到某个词时用 query 搜具体消息。"
                    if sessions
                    else "本地还没有任何历史会话。"
                ),
            }
        if name == "skill_load":
            from app.skills import load_skill

            requested = args["name"]
            # 多源：`mcp__<服务>__<prompt>` 名路由到远程 prompt 正文，而非本地文件。
            if str(requested).startswith("mcp__") and self.mcp is not None:
                body = self.mcp.prompt_body(str(requested))
                if body is not None:
                    if not body:
                        raise ValueError(f"外部 prompt {requested} 的正文暂时取不到，请稍后重试")
                    return {
                        "name": str(requested),
                        "scope": "global",
                        "version": 0,
                        "body": body,
                    }
            skill = load_skill(self.settings.data_dir / "skills", args["name"])
            if not skill["body"]:
                raise ValueError(f"没有名为 {args['name']} 的技能，先看系统提示里的技能清单")
            # 加载即视为一次使用：同步索引并累加 usage_count（二期治理的输入）
            await self._touch_skill(skill["name"])
            return {
                "name": skill["name"],
                "scope": skill["scope"],
                "version": skill["version"],
                "body": skill["body"],
            }
        if name == "skill_list":
            from app.skills import catalog

            skills = catalog(self.settings.data_dir / "skills")
            return [
                {
                    "name": s["name"],
                    "scope": s["scope"],
                    "triggers": s["triggers"],
                }
                for s in skills
            ]
        if name == "skill_save":
            from app.skills import (
                _refuse_capture,
                catalog,
                find_conflict,
                load_skill,
                save_skill,
                sync_to_store,
            )

            root = self.settings.data_dir / "skills"
            new_name = args["name"]
            new_scope = args.get("scope") or "global"
            new_triggers = args.get("triggers") or []
            new_body = args["body"]

            # ⑤ 写入侧内容守门：拦住"失败叙事/环境故障/负面断言"等不该沉淀的内容。
            # 必须 raise（不能返回 status）——execute() 会把本地返回包成 {"status":"ok"}，
            # 把拒绝覆盖掉（M2 已踩过同一个坑）。
            refusal = _refuse_capture(new_body)
            if refusal:
                raise ValueError(f"拒绝沉淀为技能：{refusal}")

            existing = catalog(root)
            # ① 写入查重：同名 → 更新（version+1 合并差异），而非新建
            same = load_skill(root, new_name)
            if same["body"]:
                merged_body = same["body"]
                if new_body.strip() not in merged_body:
                    merged_body = f"{merged_body}\n\n{new_body.strip()}"
                skill = save_skill(
                    root,
                    new_name,
                    new_scope,
                    new_triggers or same["triggers"],
                    merged_body,
                    status="active",  # 已存在的技能更新后视为在用
                    version=same["version"] + 1,
                )
                await sync_to_store(self.store, root)
                await self._set_skill_status(skill["name"], "active")
                return {
                    "saved": skill["name"],
                    "scope": skill["scope"],
                    "version": skill["version"],
                    "status": "active",
                    "updated": True,
                    "notice": f"已更新同名技能 {skill['name']} 到 v{skill['version']}（合并差异）。",
                }

            # ② 矛盾转备选：不同名但同 scope 且触发词重叠 → 互标「备选」，不合并
            conflicts = find_conflict(existing, new_scope, new_triggers)
            if conflicts:
                skill = save_skill(
                    root, new_name, new_scope, new_triggers, new_body
                )
                # 给每个冲突技能标「备选：新技能名」，并反向给新技能标「备选：冲突技能名」
                for c in conflicts:
                    c_name = c["name"]
                    c_triggers = list(c.get("triggers") or [])
                    if f"备选：{new_name}" not in c_triggers:
                        c_triggers.append(f"备选：{new_name}")
                    save_skill(
                        root,
                        c_name,
                        c.get("scope") or "global",
                        c_triggers,
                        load_skill(root, c_name)["body"],
                        status="active",
                        version=(c.get("version") or 0) + 1,
                    )
                # 反向：新技能 triggers 里标所有冲突技能为备选
                for c in conflicts:
                    if f"备选：{c['name']}" not in new_triggers:
                        new_triggers.append(f"备选：{c['name']}")
                skill = save_skill(
                    root, new_name, new_scope, new_triggers, new_body
                )
                await sync_to_store(self.store, root)
                await self._set_skill_status(skill["name"], "unreviewed")
                return {
                    "saved": skill["name"],
                    "scope": skill["scope"],
                    "version": skill["version"],
                    "status": "unreviewed",
                    "alternatives": [c["name"] for c in conflicts],
                    "notice": (
                        f"技能 {skill['name']} 已落盘，并与 {[c['name'] for c in conflicts]} "
                        "互标为备选（同 scope 触发词重叠，视为同一件事的不同做法，不合并）。"
                    ),
                }

            # 无冲突 → 正常新建
            skill = save_skill(root, new_name, new_scope, new_triggers, new_body)
            # 落盘后立即镜像进 skills 表，保证索引一致
            await sync_to_store(self.store, root)
            await self._set_skill_status(skill["name"], "unreviewed")
            return {
                "saved": skill["name"],
                "scope": skill["scope"],
                "version": skill["version"],
                "status": "unreviewed",
                "notice": "技能已落盘（标 unreviewed 待确认，确认前不会注入）。",
            }
        if name == "tool_search":
            return self.search_tools(args["query"], int(args.get("limit") or 8))
        raise ValueError("未实现的工具")

    def search_tools(self, query: str, limit: int = 8) -> list[dict]:
        """按关键词在**当前暴露的工具**里检索，返回名字/说明/toolset。

        为什么需要它（Hermes 的 `tool_search`）：工具一多，把全部 schema 塞进每次
        请求既贵又稀释注意力。有了检索，模型可以"先找再调"。Orbit 的内置工具已经从
        十几个涨到三十几个，逐个读 schema 已经不现实，所以这一层从"为规模准备"变成了
        日常路径——但接口没变：按名字/说明/toolset 命中打分。

        只搜**已暴露**的：搜到却调不了的工具比搜不到更糟。
        """
        needle = (query or "").strip().lower()
        if not needle:
            return []
        exposed = set(toolsets.resolve(list(self.tools), "cli", self.toolsets_config()))
        hits: list[tuple[int, dict]] = []
        for name, tool in self.tools.items():
            if name not in exposed:
                continue
            spec = tool.specification().get("function", {})
            description = str(spec.get("description") or "")
            toolset = toolsets.toolset_of(name)
            # 打分：名字命中 > 描述命中；同类内按命中位置排（越靠前越相关）。
            score = 0
            if needle in name.lower():
                score = 100 - name.lower().index(needle)
            elif needle in description.lower():
                score = 50 - min(description.lower().index(needle), 40)
            elif needle in toolset.lower():
                score = 10
            if score > 0:
                hits.append(
                    (
                        score,
                        {
                            "name": name,
                            "description": description[:200],
                            "toolset": toolset,
                            # tier 一起返回（MATRIX 5.11）：模型搜到 Tier 2 工具时能看出
                            # "它本来就没进首屏"，不必怀疑自己是不是看漏了清单。
                            "tier": toolsets.tier_of(name),
                            "parameters": sorted(
                                (spec.get("parameters") or {}).get("properties", {})
                            ),
                        },
                    )
                )
        hits.sort(key=lambda pair: (-pair[0], pair[1]["name"]))
        return [entry for _score, entry in hits[: max(1, limit)]]


def as_tool_text(result: dict, name: str = "") -> str:
    """把工具结果转成模型看到的文本。**这是密钥脱敏的唯一漏斗**。

    为什么在这里脱敏：工具结果是最容易带出密钥的一条路——`read_file` 读到 `.env`、
    `run_command` 打印 `env`、web 抓取回来一段含 token 的 JSON。模型看到什么，
    就可能把什么写进回复或记忆里，之后再想收回来就晚了。

    为什么先脱敏 dict 再序列化（而不是脱敏 JSON 字符串）：字符串级替换可能命中
    JSON 的语法字符，把结构改坏；`redact_obj` 只碰值、不碰结构。

    **注入包裹排在脱敏之后**，顺序不能反：先包后脱敏的话，包裹标记本身会被当成
    正文参与模式匹配（既可能被脱敏改坏标记，也可能让标记里的字样触发误报）；
    而先脱敏后包裹，得到的是"干净的正文 + 完整的分隔标记"。

    **只包高风险来源**（`app/untrusted.py::is_high_risk`：`web_*`、`browser_*`、
    `run_command`、`local_read_document`）。来源靠工具名判断——`name` 参数优先，
    其次读 :data:`TOOL_SOURCE_KEY`（`ToolRegistry.execute` 挂上去的）。两者都没有
    就不包：宁可漏包一条，也不要把 `memory_search` 这类"用户自己的内容"也标成
    不可信，那样模型会对一切都半信半疑，真该警惕的那段反而被稀释掉。

    ## hook 上下文落盘（MATRIX 5.7）接在哪、为什么只接这一处

    序列化之后、注入包裹之前过一遍 :func:`app.budget.spill_safe`：超过 10,000 字符的
    结果落盘，上下文里只留头尾 500 + 一句"中间被截了、完整内容在哪个文件"的提示。

    **只在 `as_tool_text` 接**，不在 `ToolRegistry.execute` / `govern` / 各工具内部再截一次：
    这个函数是模型侧最后一道、也是唯一一道出口（`app/graph/nodes.py` 的四个调用点都
    走它）。多接一处就会出现"两次截断"——第二次截的是第一次的预览，于是提示行和文件
    路径被剪掉，模型看到一段没头没尾的文本，还以为是工具返回了全部内容。

    顺序上它排在 `govern`（三层治理，`app/policies.py`）**之后**：三层治理按结果大小
    分级（100K / 200K / 1.5K 预览），本层按上下文预算裁剪（10K）。先分级、再裁剪，
    两层各自只截一次，账也各记各的。理由详见 `app/budget.py::spill_safe`。
    """
    from app import redact, untrusted

    payload = result
    source = (name or "").strip()
    spill_ctx: dict[str, Any] = {}
    if isinstance(result, dict):
        if TOOL_SOURCE_KEY in result:
            source = source or str(result[TOOL_SOURCE_KEY] or "")
        raw_ctx = result.get(TOOL_SPILL_KEY)
        if isinstance(raw_ctx, dict):
            spill_ctx = raw_ctx
        payload = {
            key: value
            for key, value in result.items()
            if key not in {TOOL_SOURCE_KEY, TOOL_SPILL_KEY}
        }
    text = json.dumps(redact.redact_obj(payload), ensure_ascii=False, default=str)
    if spill_ctx:
        # 没有 spill 上下文（比如 `errors_retry` 自己造的载荷）就原样返回：
        # 那一侧拿不到 data_dir，硬塞一个默认落点会把文件写到不该写的地方。
        text = budget.spill_safe(
            text,
            task_id=str(spill_ctx.get("task_id") or ""),
            tool=source,
            data_dir=spill_ctx.get("data_dir"),
        )
    if untrusted.is_high_risk(source):
        findings = untrusted.scan(text)
        if findings:
            # **只记日志，不改正文**：检测是启发式的，真网页里讲提示注入的文章天天有。
            # 把"疑似注入"写进模型上下文会让它对一切都半信半疑，而这里要的是审计
            # 痕迹——出事后能查出"哪次工具结果里带了这类字样"。
            log.warning(
                "工具 %s 的结果命中 %d 条注入特征（%s），已按不可信数据包裹",
                source,
                len(findings),
                "、".join(sorted({finding.kind for finding in findings})),
            )
        return untrusted.wrap(source, text)
    return text
