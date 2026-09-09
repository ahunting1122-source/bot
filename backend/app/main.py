import asyncio
import json
import secrets
from contextlib import asynccontextmanager

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agent import AgentRunner
from app.browser import BROWSER_TOOLS, BrowserController
from app.config import ROOT, Settings
from app.connectors import GITHUB_TOOLS, ConnectorConfig, GitHubConnector
from app.storage import Artifact, Conversation, Memory, Message, Store, Task, TaskEvent, serialize
from app.tools import ToolRegistry


class TextInput(BaseModel):
    content: str = Field(min_length=1, max_length=16000)


class MemoryInput(BaseModel):
    key: str = Field(min_length=1, max_length=160)
    value: str = Field(min_length=1, max_length=4000)


class ResumeInput(BaseModel):
    answer: str = Field(default="继续", max_length=16000)
    approved: bool = True


def migrate(settings: Settings) -> None:
    config = Config(str(ROOT / "backend" / "alembic.ini"))
    config.attributes["database_url"] = f"sqlite:///{settings.data_dir / 'app.sqlite'}"
    command.upgrade(config, "head")


def create_app(settings: Settings | None = None, *, run_agent: bool = True) -> FastAPI:
    settings = settings or Settings()
    token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        settings.prepare()
        await asyncio.to_thread(migrate, settings)
        app.state.store = Store(settings.data_dir / "app.sqlite")
        app.state.connector_config = ConnectorConfig(settings)
        await asyncio.to_thread(app.state.connector_config.load)
        app.state.browser = BrowserController(settings, app.state.store)
        app.state.github = GitHubConnector(settings)
        registry = ToolRegistry(settings, app.state.store)
        registry.register(GITHUB_TOOLS, app.state.github.execute)
        registry.register(BROWSER_TOOLS, app.state.browser.execute)
        app.state.runner = None
        if run_agent:
            app.state.runner = AgentRunner(settings, app.state.store, registry)
            await app.state.runner.start()
        try:
            yield
        finally:
            if app.state.runner:
                await app.state.runner.close()
            await app.state.browser.close()
            await app.state.github.close()
            await app.state.store.engine.dispose()

    app = FastAPI(title="Orbit · 个人助手", lifespan=lifespan)
    app.state.settings = settings
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Content-Type"],
    )

    @app.middleware("http")
    async def local_session(request: Request, call_next):
        if request.url.path.startswith("/api/"):
            origin = request.headers.get("origin")
            if origin and origin not in settings.allowed_origins:
                return Response("Origin not allowed", status_code=403)
            if request.url.hostname not in {"localhost", "127.0.0.1", "testserver"}:
                return Response("Local access only", status_code=403)
            if request.url.path != "/api/session" and not secrets.compare_digest(
                request.cookies.get("orbit_session", ""), token
            ):
                return Response("Local session required", status_code=401)
        return await call_next(request)

    async def require(model, key):
        row = await app.state.store.get(model, key)
        if row is None:
            raise HTTPException(404, "记录不存在")
        return row

    @app.get("/api/session")
    async def session(response: Response):
        response.set_cookie("orbit_session", token, httponly=True, samesite="strict")
        return {"user": "local"}

    @app.get("/api/health")
    async def health():
        return {
            "status": "ok",
            "model_configured": bool(settings.model and settings.model_api_key.get_secret_value()),
            "model": settings.model,
            "workspace": str(settings.workspace.resolve()),
        }

    @app.get("/api/conversations")
    async def conversations():
        return [
            serialize(c)
            for c in await app.state.store.rows(
                select(Conversation).order_by(Conversation.created_at.desc())
            )
        ]

    @app.post("/api/conversations", status_code=201)
    async def create_conversation():
        return serialize(await app.state.store.save(Conversation()))

    @app.get("/api/conversations/{conversation_id}/messages")
    async def messages(conversation_id: str):
        await require(Conversation, conversation_id)
        return [
            serialize(m)
            for m in await app.state.store.rows(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at)
            )
        ]

    @app.post("/api/conversations/{conversation_id}/messages", status_code=202)
    async def submit(conversation_id: str, body: TextInput):
        conversation = await require(Conversation, conversation_id)
        if not body.content.strip():
            raise HTTPException(422, "请输入任务内容")
        store = app.state.store
        active = await store.rows(
            select(Task).where(
                Task.conversation_id == conversation_id,
                Task.status.in_(["PENDING", "RUNNING", "WAITING_USER"]),
            )
        )
        if active:
            raise HTTPException(409, "当前对话有未结束任务，请继续或取消该任务")
        await store.save(
            Message(conversation_id=conversation_id, role="user", content=body.content)
        )
        if conversation.title == "新对话":
            conversation.title = body.content[:32]
            await store.save(conversation)
        task = await store.save(Task(conversation_id=conversation_id, goal=body.content))
        await store.event(task.id, "task_created", goal=body.content)
        if app.state.runner:
            await app.state.runner.enqueue(task.id)
        return serialize(task)

    @app.get("/api/tasks")
    async def tasks(conversation_id: str | None = None):
        query = select(Task).order_by(Task.created_at.desc()).limit(100)
        if conversation_id:
            query = query.where(Task.conversation_id == conversation_id)
        return [serialize(t) for t in await app.state.store.rows(query)]

    @app.get("/api/tasks/{task_id}")
    async def task(task_id: str):
        result = serialize(await require(Task, task_id))
        result["artifacts"] = [
            serialize(a)
            for a in await app.state.store.rows(select(Artifact).where(Artifact.task_id == task_id))
        ]
        return result

    @app.get("/api/tasks/{task_id}/events")
    async def events(task_id: str, request: Request, after: int = 0):
        await require(Task, task_id)
        try:
            cursor = max(after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(422, "无效的事件游标") from None

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                rows = await app.state.store.rows(
                    select(TaskEvent)
                    .where(TaskEvent.task_id == task_id, TaskEvent.id > cursor)
                    .order_by(TaskEvent.id)
                )
                for event in rows:
                    cursor = event.id
                    yield f"id: {event.id}\ndata: {json.dumps(serialize(event), ensure_ascii=False)}\n\n"
                current = await require(Task, task_id)
                if not rows and current.status in {
                    "SUCCEEDED",
                    "FAILED",
                    "CANCELLED",
                    "WAITING_USER",
                }:
                    yield "event: settled\ndata: {}\n\n"
                    break
                yield ": keepalive\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/tasks/{task_id}/cancel")
    async def cancel(task_id: str):
        current = await require(Task, task_id)
        if current.status not in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            await app.state.store.transition(task_id, "CANCELLED")
            if app.state.runner:
                await app.state.runner.cancel(task_id)
            await app.state.store.event(task_id, "task_cancelled")
        return serialize(await require(Task, task_id))

    @app.post("/api/tasks/{task_id}/resume")
    async def resume(task_id: str, body: ResumeInput):
        current = await require(Task, task_id)
        if current.status != "WAITING_USER":
            raise HTTPException(409, "任务当前不在等待状态")
        if not app.state.runner:
            raise HTTPException(503, "执行器尚未就绪")
        await app.state.runner.enqueue(task_id, body.model_dump())
        return {"status": "PENDING"}

    @app.get("/api/memories")
    async def memories():
        return [
            serialize(m)
            for m in await app.state.store.rows(select(Memory).order_by(Memory.updated_at.desc()))
        ]

    @app.post("/api/memories")
    async def remember(body: MemoryInput):
        return serialize(await app.state.store.set_memory(body.key, body.value))

    @app.patch("/api/memories/{memory_id}")
    async def edit_memory(memory_id: str, body: MemoryInput):
        memory = await require(Memory, memory_id)
        if memory.key != body.key:
            raise HTTPException(422, "请保持记忆名称不变，或删除后新建")
        return serialize(await app.state.store.set_memory(body.key, body.value))

    @app.delete("/api/memories/{memory_id}", status_code=204)
    async def forget(memory_id: str):
        await require(Memory, memory_id)
        async with app.state.store.lock, app.state.store.sessions() as db:
            memory = await db.get(Memory, memory_id)
            await db.delete(memory)
            await db.commit()

    @app.get("/api/artifacts/{artifact_id}")
    async def artifact(artifact_id: str):
        from pathlib import Path

        record = await require(Artifact, artifact_id)
        path = Path(record.location).resolve()
        if (
            not any(
                path.is_relative_to(root.resolve())
                for root in [settings.workspace, settings.data_dir / "artifacts"]
            )
            or not path.is_file()
        ):
            raise HTTPException(404, "产物不存在或不在授权目录")
        return FileResponse(path, filename=path.name)

    @app.get("/api/connectors")
    async def connector_settings():
        return app.state.connector_config.public()

    @app.put("/api/connectors")
    async def configure_connectors(body: dict[str, str]):
        active = await app.state.store.rows(
            select(Task).where(Task.status.in_(["RUNNING", "PENDING"]))
        )
        if active:
            raise HTTPException(409, "请等待当前任务结束或取消任务后修改设置")
        try:
            return await asyncio.to_thread(app.state.connector_config.update, body)
        except Exception as exc:
            raise HTTPException(
                400, f"设置未保存，请检查输入及系统凭证存储 ({type(exc).__name__})"
            ) from None

    @app.post("/api/browser/session")
    async def browser_session():
        try:
            page = await app.state.browser.start()
            if page.url == "about:blank" and settings.notion_url:
                app.state.browser.allowed(settings.notion_url)
                await page.goto(settings.notion_url, wait_until="domcontentloaded")
            await page.bring_to_front()
            return {"url": page.url}
        except Exception as exc:
            raise HTTPException(
                503, f"浏览器无法启动，请安装 Chromium 或检查网址 ({type(exc).__name__})"
            ) from None

    dist = ROOT / "frontend" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app


app = create_app()
