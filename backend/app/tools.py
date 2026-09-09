import asyncio
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from pypdf import PdfReader
from sqlalchemy import select

from app.config import Settings
from app.storage import Artifact, Memory, Operation, Store, Task, serialize


class Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Empty(Args):
    pass


class PathArgs(Args):
    path: str = Field(default=".", description="授权工作目录下的相对路径")


class SearchArgs(Args):
    query: str = Field(min_length=1, max_length=200)


class ReportArgs(Args):
    path: str = Field(description="以 .md 结尾的相对路径")
    content: str = Field(max_length=100000)


class MemoryArgs(Args):
    key: str = Field(min_length=1, max_length=160)
    value: str = Field(min_length=1, max_length=4000)


class KeyArgs(Args):
    key: str


class TaskArgs(Args):
    task_id: str


@dataclass
class Tool:
    name: str
    description: str
    schema: type[BaseModel]
    confirmation: bool = False

    def specification(self):
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema.model_json_schema(),
            },
        }


LOCAL_TOOLS = [
    Tool("local_list_files", "列出授权目录的文件。", PathArgs),
    Tool("local_search_text", "在授权目录的 TXT/Markdown 中搜索关键词。", SearchArgs),
    Tool("local_read_document", "读取 TXT、Markdown、文本型 PDF，最多返回 16000 字符。", PathArgs),
    Tool("local_write_report", "在授权目录创建 Markdown 报告，不覆盖不同内容。", ReportArgs),
    Tool("memory_search", "按关键词搜索跨对话长期记忆。", SearchArgs),
    Tool("memory_save", "保存或更新用户明确要求长期记住的偏好；需要用户确认。", MemoryArgs, True),
    Tool("memory_delete", "删除指定长期记忆；需要用户确认。", KeyArgs, True),
    Tool("history_search_tasks", "查找其他对话的历史任务，用户提到以前的工作时使用。", SearchArgs),
    Tool("history_get_task", "读取选中历史任务的目标、结果产物，不载入其他对话原文。", TaskArgs),
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

    def register(self, tools: list[Tool], executor):
        for tool in tools:
            self.tools[tool.name] = tool
            self.external[tool.name] = executor

    def specs(self):
        return [tool.specification() for tool in self.tools.values()]

    def validate(self, name: str, args: dict) -> dict:
        if name not in self.tools:
            raise ValueError(f"未知工具：{name}")
        return self.tools[name].schema.model_validate(args).model_dump()

    async def execute(self, task_id: str, operation_id: str, name: str, args: dict):
        try:
            args = self.validate(name, args)
        except ValueError as exc:
            return {"status": "error", "error": str(exc)[:1000]}
        previous = await self.store.get(Operation, operation_id)
        if previous and previous.status == "DONE":
            return previous.result
        operation = previous or Operation(id=operation_id, task_id=task_id, name=name, args=args)
        await self.store.save(operation)
        await self.store.event(task_id, "tool_started", tool=name)
        try:
            if name in self.external:
                result = await self.external[name](name, args, task_id, operation_id)
            else:
                result = {"status": "ok", "data": await self.local(name, args, task_id)}
        except Exception as exc:
            result = {"status": "error", "error": f"{type(exc).__name__}: {str(exc)[:600]}"}
        operation.status = (
            "DONE" if result["status"] not in {"needs_user", "uncertain"} else "WAITING"
        )
        operation.result = result
        await self.store.save(operation)
        await self.store.event(task_id, "tool_finished", tool=name, result=result)
        return result

    async def local(self, name: str, args: dict, task_id: str):
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
            return serialize(await self.store.set_memory(args["key"], args["value"], task_id))
        if name == "memory_delete":
            async with self.store.lock, self.store.sessions() as session:
                memory = await session.scalar(select(Memory).where(Memory.key == args["key"]))
                if memory:
                    await session.delete(memory)
                    await session.commit()
            return {"deleted": bool(memory)}
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
        raise ValueError("未实现的工具")


def as_tool_text(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)
