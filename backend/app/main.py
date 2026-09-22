import asyncio
import json
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.agent import AgentRunner
from app.browser import BROWSER_TOOLS, BrowserController, BrowserSessionError
from app.config import GRANTABLE_TOOLS, ROOT, Settings, load_grants
from app.connectors import GITHUB_TOOLS, ConnectorConfig, GitHubConnector
from app.mcp import MCPManager
from app.storage import Artifact, Conversation, Memory, Message, Store, Task, TaskEvent, serialize
from app.summaries import SummaryService, list_conversations
from app.tools import ToolRegistry
from app.vault import Vault


class TextInput(BaseModel):
    content: str = Field(min_length=1, max_length=16000)


class MemoryInput(BaseModel):
    key: str = Field(min_length=1, max_length=160)
    value: str = Field(min_length=1, max_length=4000)


class ResumeInput(BaseModel):
    answer: str = Field(default="继续", max_length=16000)
    approved: bool = True
    remember: bool = False


class GrantInput(BaseModel):
    host: str = Field(min_length=1, max_length=253)


class SecretInput(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=8000)


class SiteSecretInput(BaseModel):
    host: str = Field(min_length=1, max_length=253)
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=400)


class BrowserInput(BaseModel):
    url: str | None = None


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
        await app.state.store.tune()
        app.state.connector_config = ConnectorConfig(settings)
        await asyncio.to_thread(app.state.connector_config.load)
        app.state.browser = BrowserController(settings, app.state.store)
        app.state.vault = Vault(settings)

        def login_hosts() -> list[str]:
            """Sites worth reporting a sign-in state for: configured ones plus remembered sites."""
            candidates = [
                settings.browser_url,
                settings.notion_url,
                *load_grants(settings.granted_actions),
                *app.state.vault._meta()["sites"],
            ]
            hosts: list[str] = []
            for value in candidates:
                host = urlparse(value).hostname if "//" in value else value
                host = (host or "").lower().lstrip(".")
                if host and host not in hosts:
                    hosts.append(host)
            return hosts

        app.state.login_hosts = login_hosts
        app.state.github = GitHubConnector(settings)
        app.state.summaries = SummaryService(settings, app.state.store)
        await app.state.summaries.restore_pending()
        app.state.chat_lock = asyncio.Lock()
        registry = ToolRegistry(settings, app.state.store)
        registry.register(GITHUB_TOOLS, app.state.github.execute)
        registry.register(BROWSER_TOOLS, app.state.browser.execute)
        registry.browser = app.state.browser
        app.state.registry = registry
        app.state.mcp = MCPManager(settings)
        await app.state.mcp.start(registry)
        app.state.runner = None
        if run_agent:
            app.state.runner = AgentRunner(
                settings, app.state.store, registry, summaries=app.state.summaries
            )
            await app.state.runner.start()
        try:
            yield
        finally:
            if app.state.runner:
                await app.state.runner.close()
            await app.state.mcp.close(app.state.registry)
            await app.state.summaries.close()
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
        return await list_conversations(app.state.store)

    @app.post("/api/conversations/{conversation_id}/summary", status_code=202)
    async def summarize(conversation_id: str):
        await require(Conversation, conversation_id)
        await app.state.summaries.rebuild(conversation_id)
        return {"status": "pending"}

    @app.post("/api/conversations", status_code=201)
    async def create_conversation():
        return serialize(await app.state.store.save(Conversation()))

    @app.get("/api/conversations/{conversation_id}/messages")
    async def messages(conversation_id: str):
        await require(Conversation, conversation_id)
        replies = await app.state.store.rows(
            select(Task).where(Task.conversation_id == conversation_id)
        )
        states = {f"answer-{t.id}": t.status for t in replies}
        rows = await app.state.store.rows(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.created_at)
        )
        return [{**serialize(m), "reply_status": states.get(m.id)} for m in rows]

    @app.post("/api/conversations/{conversation_id}/messages", status_code=202)
    async def submit(conversation_id: str, body: TextInput):
        async with app.state.chat_lock:
            return await submit_message(conversation_id, body)

    async def submit_message(conversation_id: str, body: TextInput):
        conversation = await require(Conversation, conversation_id)
        if not body.content.strip():
            raise HTTPException(422, "请输入消息内容")
        store = app.state.store
        active = await store.rows(
            select(Task).where(
                Task.conversation_id == conversation_id,
                Task.status.in_(["PENDING", "RUNNING", "WAITING_USER"]),
            )
        )
        if active:
            raise HTTPException(409, "上一条回复还未结束，请补充回答或先停止回复。")
        if conversation.title == "新对话":
            conversation.title = body.content[:32]
            await store.save(conversation)
        await store.save(
            Message(conversation_id=conversation_id, role="user", content=body.content)
        )
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
                # shield: a browser closing the stream must not cancel a query mid-flight,
                # which would kill the SQLite connection without a clean rollback.
                rows = await asyncio.shield(
                    app.state.store.rows(
                        select(TaskEvent)
                        .where(TaskEvent.task_id == task_id, TaskEvent.id > cursor)
                        .order_by(TaskEvent.id)
                    )
                )
                for event in rows:
                    cursor = event.id
                    yield f"id: {event.id}\ndata: {json.dumps(serialize(event), ensure_ascii=False)}\n\n"
                current = await asyncio.shield(require(Task, task_id))
                if not rows and current.status in {
                    "SUCCEEDED",
                    "FAILED",
                    "CANCELLED",
                    "WAITING_USER",
                }:
                    yield "event: settled\ndata: {}\n\n"
                    break
                yield "event: heartbeat\ndata: {}\n\n"
                await asyncio.sleep(0.1)

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
            app.state.summaries.schedule(current.conversation_id)
        return serialize(await require(Task, task_id))

    @app.post("/api/tasks/{task_id}/resume")
    async def resume(task_id: str, body: ResumeInput):
        async with app.state.chat_lock:
            return await resume_reply(task_id, body)

    async def resume_reply(task_id: str, body: ResumeInput):
        current = await require(Task, task_id)
        if current.status != "WAITING_USER":
            raise HTTPException(409, "这条回复当前不需要补充信息。")
        if not app.state.runner:
            raise HTTPException(503, "助手尚未就绪，请稍后重试。")
        if body.answer.strip():
            await app.state.store.save(Message(
                conversation_id=current.conversation_id, role="user", content=body.answer
            ))
        if body.remember and body.approved:
            tool = current.waiting.get("tool", "")
            host = app.state.browser.current_host()
            # Actions outside the grantable set simply are not remembered. The approval
            # itself must still go through, and failing to remember is the safe direction:
            # the next attempt asks again.
            if tool in GRANTABLE_TOOLS and host:
                try:
                    app.state.connector_config.grant(host, tool)
                except ValueError as exc:
                    raise HTTPException(422, str(exc)) from None
        await app.state.runner.enqueue(task_id, body.model_dump())
        return {"status": "PENDING"}

    @app.post("/api/tasks/{task_id}/retry", status_code=202)
    async def retry(task_id: str):
        async with app.state.chat_lock:
            current = await require(Task, task_id)
            if current.status not in {"FAILED", "CANCELLED"}:
                raise HTTPException(409, "这条回复无需重试。")
            active = await app.state.store.rows(select(Task).where(
                Task.conversation_id == current.conversation_id,
                Task.status.in_(["PENDING", "RUNNING", "WAITING_USER"]),
            ))
            if active:
                raise HTTPException(409, "请先等待当前回复结束。")
            replacement = await app.state.store.save(Task(
                conversation_id=current.conversation_id, goal=current.goal
            ))
            await app.state.store.event(replacement.id, "task_created", goal=current.goal)
            if app.state.runner:
                await app.state.runner.enqueue(replacement.id)
            return serialize(replacement)

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
            raise HTTPException(409, "请等待当前回复结束，或先停止回复后修改设置。")
        try:
            return await asyncio.to_thread(app.state.connector_config.update, body)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except Exception as exc:
            raise HTTPException(
                400, f"设置未保存，请检查输入及系统凭证存储 ({type(exc).__name__})"
            ) from None

    @app.post("/api/connectors/model/check")
    async def check_model():
        return await app.state.connector_config.check_model()

    class MCPServersInput(BaseModel):
        servers: list[dict] = Field(default_factory=list)

    class MCPSecretInput(BaseModel):
        server: str = Field(min_length=1, max_length=80)
        name: str = Field(min_length=1, max_length=80)
        value: str = Field(min_length=1, max_length=4000)

    @app.get("/api/mcp")
    async def mcp_status():
        return {"servers": app.state.mcp.public(), "config": app.state.mcp.servers()}

    @app.put("/api/mcp")
    async def mcp_save(body: MCPServersInput):
        try:
            app.state.mcp.save_servers(body.servers)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        await app.state.mcp.reload(app.state.registry)
        return {"servers": app.state.mcp.public()}

    @app.post("/api/mcp/reload")
    async def mcp_reload():
        await app.state.mcp.reload(app.state.registry)
        return {"servers": app.state.mcp.public()}

    @app.post("/api/mcp/secret")
    async def mcp_secret(body: MCPSecretInput):
        app.state.mcp.set_secret(body.server, body.name, body.value)
        return {"servers": app.state.mcp.public()}

    @app.get("/api/tools")
    async def tool_inventory():
        """What the model can currently call — built-ins plus everything MCP contributed."""
        return [
            {"name": tool.name, "source": tool.source, "confirmation": tool.confirmation}
            for tool in app.state.registry.tools.values()
        ]

    @app.get("/api/secrets")
    async def list_secrets():
        vault = app.state.vault
        hosts = app.state.login_hosts()
        try:
            status = await app.state.browser.login_status(hosts)
        except BrowserSessionError as exc:
            status = {"ready": False, "reason": exc.message, "logins": []}
        return {
            "keys": vault.keys(),
            "sites": vault.sites(),
            "logins": status["logins"],
            "logins_ready": status["ready"],
            "logins_reason": status["reason"],
            "hosts": hosts,
        }

    @app.post("/api/secrets")
    async def save_secret(body: SecretInput):
        try:
            return app.state.vault.set_key(body.key, body.value)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.delete("/api/secrets/{key}")
    async def remove_secret(key: str):
        try:
            app.state.vault.delete_key(key)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"status": "deleted"}

    @app.post("/api/secrets/sites")
    async def save_site_secret(body: SiteSecretInput):
        try:
            return app.state.vault.set_site(body.host, body.username, body.password)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/secrets/sites/remove")
    async def remove_site_secret(body: GrantInput):
        try:
            app.state.vault.delete_site(body.host)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"status": "deleted"}

    @app.post("/api/grants/revoke")
    async def revoke_grant(body: GrantInput):
        return {"grants": app.state.connector_config.revoke(body.host.strip().lower())}

    @app.post("/api/browser/session")
    async def browser_session(body: BrowserInput | None = None):
        try:
            return await app.state.browser.open_session(body.url if body else None)
        except BrowserSessionError as exc:
            raise HTTPException(
                exc.status_code, exc.message, headers={"X-Orbit-Browser-Stage": exc.stage}
            ) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    dist = ROOT / "frontend" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app


app = create_app()
