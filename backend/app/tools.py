import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader
from sqlalchemy import select

from app.config import GRANTABLE_TOOLS, Settings, load_grants
from app.storage import Artifact, Memory, Operation, Store, Task, serialize

log = logging.getLogger("orbit.tools")

#: retriever_search 每任务调用上限；超限只回提示不再检索（防"再查一次"替代思考）
RETRIEVER_CALL_LIMIT = 4
#: 工具执行并发闸。单 worker 串行下现在天然不超，是给 MCP 工具并发上升预留的上限：
#: 资源面最窄的是浏览器（一个进程一个页面），并发放开时必须先有闸。
TOOL_CONCURRENCY = 4


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
    path: str = Field(default=".", description="授权工作目录下的相对路径")


class SearchArgs(Args):
    query: str = Field(min_length=1, max_length=200)


class ReportArgs(Args):
    path: str = Field(description="以 .md 结尾的相对路径")
    content: str = Field(max_length=100000)


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
    Tool("local_list_files", "列出授权目录的文件。", PathArgs),
    Tool("local_search_text", "在授权目录的 TXT/Markdown 中搜索关键词。", SearchArgs),
    Tool("local_read_document", "读取 TXT、Markdown、文本型 PDF，最多返回 16000 字符。", PathArgs),
    Tool("local_write_report", "在授权目录创建 Markdown 报告，不覆盖不同内容。", ReportArgs),
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


def resolve_path(root: Path, relative: str) -> Path:
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("路径超出授权工作目录")
    return path


def read_document(root: Path, relative: str) -> dict:
    path = resolve_path(root, relative)
    if not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("文件不存在或超过 8 MB")
    if path.suffix.lower() == ".pdf":
        text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages[:50])
    elif path.suffix.lower() in {".md", ".txt"}:
        text = path.read_text(encoding="utf-8-sig")
    else:
        raise ValueError("暂时支持 TXT、Markdown、文本型 PDF")
    return {
        "path": str(path.relative_to(root.resolve())),
        "text": text[:16000],
        "truncated": len(text) > 16000,
    }


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

    def granted(self, name: str) -> bool:
        """True when the user permanently approved this action on the site currently open."""
        if name not in GRANTABLE_TOOLS or self.browser is None:
            return False
        host = self.browser.current_host()
        if not host:
            return False
        return name in load_grants(self.settings.granted_actions).get(host, [])

    def register(self, tools: list[Tool], executor):
        """注册一批工具。

        同名阴影规则（照 Hermes 的 registry）：**同 source 重复注册允许**（重连刷新需要，
        工具清单本就可能变），**跨 source 遮蔽拒绝并记日志**——MCP 服务与内置工具同名时
        静默覆盖会让"模型调的是谁"变成一个查不出来的问题。
        """
        for tool in tools:
            if tool.parameters is not None and not isinstance(tool.parameters, dict):
                log.warning("工具 %s 的参数声明不是 dict，已跳过注册", tool.name)
                continue
            existing = self.tools.get(tool.name)
            if existing is not None and existing.source != tool.source:
                log.warning(
                    "工具 %s 已被 %s 注册，来自 %s 的同名注册被拒绝",
                    tool.name,
                    existing.source,
                    tool.source,
                )
                continue
            self.tools[tool.name] = tool
            self.external[tool.name] = executor

    def unregister_source(self, source: str) -> None:
        """Drop every tool that came from one source, e.g. an MCP server that went away."""
        for name in [n for n, tool in self.tools.items() if tool.source == source]:
            self.tools.pop(name, None)
            self.external.pop(name, None)

    def specs(self):
        return [tool.specification() for tool in self.tools.values()]

    def validate(self, name: str, args: dict) -> dict:
        tool = self.tools.get(name)
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
                    result = {
                        "status": "ok",
                        "data": await self.local(name, args, task_id, messages),
                    }
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

    async def local(self, name: str, args: dict, task_id: str, messages=None):
        root = self.settings.workspace.resolve()
        if name == "local_list_files":
            path = resolve_path(root, args["path"])
            entries = []
            for item in sorted(path.iterdir()):
                if item.resolve().is_relative_to(root):
                    entries.append(
                        {"path": str(item.relative_to(root)), "directory": item.is_dir()}
                    )
                if len(entries) >= 100:
                    break
            return entries
        if name == "local_read_document":
            return await asyncio.to_thread(read_document, root, args["path"])
        if name == "local_search_text":

            def search():
                matches: list[dict] = []
                for index, path in enumerate(root.rglob("*")):
                    if index > 1000 or len(matches) >= 30:
                        break
                    if path.suffix.lower() not in {
                        ".md",
                        ".txt",
                    } or not path.resolve().is_relative_to(root):
                        continue
                    try:
                        text = read_document(root, str(path.relative_to(root)))["text"]
                        position = text.casefold().find(args["query"].casefold())
                        if position >= 0:
                            matches.append(
                                {
                                    "path": str(path.relative_to(root)),
                                    "excerpt": text[max(0, position - 100) : position + 500],
                                }
                            )
                    except (ValueError, OSError, UnicodeError):
                        continue
                return matches

            return await asyncio.to_thread(search)
        if name == "local_write_report":
            path = resolve_path(root, args["path"])
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
            memory, merged_from = await self.store.set_memory(
                args["key"], args["value"], task_id
            )
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
        raise ValueError("未实现的工具")


def as_tool_text(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)
