import asyncio
import json
import logging
import secrets
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from alembic import command
from alembic.config import Config
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select

from app import logging_setup
from app import profile as profile_module
from app.agent import AgentRunner
from app.browser import BROWSER_TOOLS, BrowserController, BrowserSessionError
from app.config import GRANTABLE_TOOLS, ROOT, Settings, load_grants
from app.connectors import GITHUB_TOOLS, ConnectorConfig, GitHubConnector
from app.mcp import MCPManager
from app.storage import (
    Artifact,
    Conversation,
    Memory,
    Message,
    Skill,
    Store,
    Task,
    TaskEvent,
    serialize,
)
from app.summaries import SummaryService, list_conversations
from app.tools import ToolRegistry
from app.vault import Vault

#: 本模块的 logger。此前 main.py 里一条日志都没有——启动失败时只能看到 uvicorn
#: 的栈，看不到"Orbit 自己认为发生了什么"。
log = logging.getLogger("orbit.main")


class TextInput(BaseModel):
    content: str = Field(min_length=1, max_length=16000)


class MemoryInput(BaseModel):
    key: str = Field(min_length=1, max_length=160)
    value: str = Field(min_length=1, max_length=4000)


class ProfileInput(BaseModel):
    body: str = Field(default="", max_length=20000)


class ProfileEntryInput(BaseModel):
    entry: str = Field(min_length=1, max_length=1000)


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


class SkillInput(BaseModel):
    """人工编辑技能：正文 + 可选 scope/triggers。PUT 时不指定则沿用现有值。"""

    body: str = Field(min_length=1, max_length=40000)
    scope: str = Field(default="", max_length=20)
    triggers: list[str] = Field(default_factory=list)


class SkillStatusInput(BaseModel):
    status: str = Field(min_length=1, max_length=20)


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
        # 先把日志装好，再做任何别的事——否则启动阶段的失败恰恰是"没有日志可查"
        # 的那一段。`console=False`：常驻服务不该把同一行同时写进文件和终端。
        logging_setup.configure(settings.data_dir, level=settings.log_level)
        log.info("Orbit 后端启动 · 数据目录 %s", settings.data_dir)
        await asyncio.to_thread(migrate, settings)
        app.state.store = Store(settings.data_dir / "app.sqlite")
        await app.state.store.tune()
        # 会话平面写侧统一（MATRIX 10.1）：把 `store.event()` 换成"写完会话库顺手写平面"。
        #
        # 为什么必须在这里挂：`agent.py` 与 `graph/nodes.py` 里的每一处 `await
        # self.store.event(...)` 都走实例属性，所以在这里替换一次，那两处**不用改**就能
        # 自动进平面。少了这一行，CLI/TUI/gateway/cron 四处的事件仍然各写各的——
        # 读侧早就合并了，写侧不统一，时间线就会缺交互会话那一段。
        from app.session_plane import PlaneSink, SessionPlane

        app.state.plane_sink = PlaneSink(SessionPlane(settings.data_dir), app.state.store)
        app.state.plane_detach = app.state.plane_sink.attach()
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
        # 联网检索与正文抽取。Orbit 是"网页操作特化"的 agent，但在此之前它**没有搜索工具**
        # ——上网全靠浏览器点，成本高且容易被页面结构变化打穿。这两个工具补的就是这个缺口：
        # `web_search` 只负责"找到候选 URL"，`web_extract` 只负责"把已知 URL 读成正文"。
        from app.web import WEB_TOOLS, WebTools

        app.state.web = WebTools(settings)
        registry.register(WEB_TOOLS, app.state.web.execute)
        registry.browser = app.state.browser
        app.state.registry = registry
        # 插件宿主（MATRIX 13.3）：把插件的工具与钩子真的接进宿主。
        #
        # 为什么必须在这里而不是更早：`PluginHost.start()` 要把工具推进 `ToolRegistry`、
        # 钩子推进 `HookRegistry`，两者都得先存在。放在 registry 建好之后一行就够。
        # 插件加载失败不该让整个服务起不来——`from_settings` 内部按插件隔离，
        # 坏插件只进 `skipped`，所以这里不包 try。
        # 钩子注册表是**进程级单例** `app.hooks.DEFAULT_HOOKS`：插件注册到 A、
        # 节点发射到 B 的话，钩子会静默不生效（hooks.py 的注释已为这件事留过话）。
        from app.hooks import DEFAULT_HOOKS
        from app.plugin_host import PluginHost

        app.state.plugin_host = PluginHost.from_settings(
            settings, tool_sink=registry, hook_sink=DEFAULT_HOOKS
        )
        app.state.plugin_host.attach_to_app(app)
        # 插件记忆 provider 真的装进路由器（MATRIX 13.4）。
        #
        # **之前 `memory_providers.build_router()` 零生产调用方**——那套
        # 「15 个 provider 名字 + 路由器」的抽象从来没被装配过，生产里的记忆就是
        # 一个 `MEMORY.md` 文件。插件能注册 provider 对象，但**没有任何生产代码
        # 去读它或写它**。
        #
        # 这里把插件 provider 并进路由器的 provider 列表，再交给桥（读/写/记账）。
        # **失败不拦启动**：记忆 provider 是增强项，装不上不该让整个服务起不来。
        try:
            from app import memory_plugin_bridge
            from app.memory_providers import build_router, default_providers

            _plugin_memory = list(app.state.plugin_host.memory_providers().values())
            _router = build_router(
                settings=settings,
                store=app.state.store,
                providers=[*default_providers(store=app.state.store, data_dir=settings.data_dir),
                           *_plugin_memory],
                data_dir=settings.data_dir,
            )
            memory_plugin_bridge.configure(_router, plugin_providers=_plugin_memory)
            app.state.memory_bridge = memory_plugin_bridge.describe()
        except Exception as exc:  # noqa: BLE001 —— 增强项，不该拦启动
            log.warning("插件记忆 provider 装配失败（不影响核心记忆）：%s", exc)
        # 工具层额外的真实现（MATRIX 5.14）：`tool_describe` / `cron_job` 写在
        # `app/tool_extra_impl.py`，而 `LOCAL_TOOLS` 是禁改区。不在这里注册的话，
        # 覆盖率报告会说"覆盖了"，但模型看不见——**那种不一致比没做更糟**。
        from app.tool_extra_impl import register_extra_tools

        app.state.extra_tools = register_extra_tools(registry, data_dir=settings.data_dir)
        # 第三轮工具（MATRIX 5.14）：`send_message` / `memory_provider_list` /
        # `skill_search` / `egress_status` / `budget_status`。
        # 同样不能漏——漏了就是"报告说覆盖了、模型看不见"。
        from app.tool_extra_impl2 import register_round3_tools

        app.state.round3_tools = register_round3_tools(
            registry, settings=settings, store=app.state.store, data_dir=settings.data_dir
        )
        # 委派工具（MATRIX 5.14 的最后 2 条）：`delegate_task` / `agent_swarm`。
        #
        # 它们一直是「有机制缺包装」——`delegate.py` 的调度与 `swarm.py` 的拓扑序都写好了，
        # 但 `ChildRunner` 协议**没有任何实现**，`DelegateRunner()` 一跑就抛
        # 「未注入子任务 runner」。所以本轮先补 `app/child_runner.py`，这里才注册得上。
        #
        # **注入的是构造器不是现成 runner**：子任务必须落在**父任务所在的会话**里，
        # 而 `conversation_id` 每个父任务不同——注入现成 runner 会把所有子任务
        # 塞进同一个会话，父任务之间互相串味。
        from app import child_runner, tool_delegate
        from app.delegate import DelegateRunner

        def _delegate_builder(conversation_id: str) -> DelegateRunner:
            return DelegateRunner(
                runner_factory=child_runner.graph_child_factory(
                    app.state.runner,
                    conversation_id=conversation_id,
                    depth=0,
                    max_depth=2,
                ),
                data_dir=settings.data_dir,
            )

        app.state.delegate_tools = tool_delegate.register_delegate_tools(
            registry, store=app.state.store, builder=_delegate_builder
        )
        # 观测 exporter 接到钩子上（MATRIX 13.4 / 15.x）。
        #
        # 为什么现在才有意义：`exporters.Exporter.hooks()` 返回的是**生命周期事件名**
        # （`pre_llm_call` / `post_tool_call` / …），而 `graph/nodes.py` 在此之前
        # **一个钩子都不发**。本轮补了发射点，这条线才通。
        # `attach()` 只注册 `available()` 为真的 exporter，不可用的进 `skipped` 并给原因
        # ——**静默跳过会让"没数据"变成谜**。
        from app import exporters as _exporters
        from app import observability_sink

        app.state.exporters = _exporters.default_exporters(settings.data_dir)
        app.state.observability = observability_sink.attach(app.state.exporters)

        # 启动时清一次孤儿账本（MATRIX 10.7）。
        #
        # **之前 `reap()` 的唯一调用方是 `orbit plane reap`**——也就是说，进程崩过之后
        # 残留的登记条目只有用户自己想起来敲那条命令才会被清。于是 `orbit plane`
        # 会一直报着几个早就死掉的进程，而"报告说谎"比"不报告"更糟。
        #
        # **为什么这里敢用 `dry_run=False`**：`OrphanReaper.reap()` 的 docstring
        # 写死了「**绝不 kill 进程**」——它只删 `processes.json` / `spawn-ledger.json` 里
        # 判为 dead 或 reused 的条目，以及判为死的 `dispatcher.lock` 文件本身。
        # 纯账本清理，没有副作用风险。
        #
        # 失败不拦启动：回收是卫生工作，不该让服务起不来。
        try:
            from app.orphan_reaper import OrphanReaper

            _report = OrphanReaper(settings.data_dir).reap(dry_run=False)
            if _report.removed or _report.lock_removed:
                log.info(
                    "启动回收：清掉 %s 条孤儿登记%s",
                    _report.removed,
                    "，并删掉残留的 dispatcher.lock" if _report.lock_removed else "",
                )
        except Exception as exc:  # noqa: BLE001 —— 卫生工作不该拦启动
            log.warning("启动孤儿回收失败（不影响服务）：%s", exc)
        # 资源入 RAG 需要索引写入器：MCPManager 连接时把 index_resources 服务的内容写进 chunks。
        from app.indexing import build_indexer

        app.state.mcp = MCPManager(settings, indexer=build_indexer(settings, app.state.store))
        await app.state.mcp.start(registry)
        # 多源技能：MCP prompts 成为技能层的第二来源（只读）。
        registry.mcp = app.state.mcp
        app.state.runner = None
        if run_agent:
            app.state.runner = AgentRunner(
                settings, app.state.store, registry, summaries=app.state.summaries
            )
            # 注入装配层把 MCP prompts 挂到技能目录的第二来源上，注入择优对两路一视同仁。
            app.state.runner.policies.skills.prompt_source = app.state.mcp.prompt_skills
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
            "file_scope": settings.file_scope,
            # 命令执行是常驻能力（两档都有），这里只是让前端/探针一眼看到它开着。
            "command_execution": True,
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
            await app.state.runner.enqueue(task.id, platform="api")
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
            # heartbeat 是前端 3.5s 看门狗的判活信号，限频到 1s：
            # 事件轮询保持 0.1s（低延迟），空闲时不再每 100ms 刷一帧心跳。
            last_beat = 0.0
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
                    last_beat = asyncio.get_running_loop().time()
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
                now = asyncio.get_running_loop().time()
                if now - last_beat >= 1.0:
                    last_beat = now
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
        await app.state.runner.enqueue(task_id, body.model_dump(), platform="api")
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
                await app.state.runner.enqueue(replacement.id, platform="api")
            return serialize(replacement)

    @app.get("/api/memories")
    async def memories():
        return [
            serialize(m)
            for m in await app.state.store.rows(select(Memory).order_by(Memory.updated_at.desc()))
        ]

    @app.post("/api/memories")
    async def remember(body: MemoryInput):
        memory, _ = await app.state.store.set_memory(body.key, body.value)
        return serialize(memory)

    @app.patch("/api/memories/{memory_id}")
    async def edit_memory(memory_id: str, body: MemoryInput):
        memory = await require(Memory, memory_id)
        if memory.key != body.key:
            raise HTTPException(422, "请保持记忆名称不变，或删除后新建")
        updated, _ = await app.state.store.set_memory(body.key, body.value)
        return serialize(updated)

    @app.delete("/api/memories/{memory_id}", status_code=204)
    async def forget(memory_id: str):
        await require(Memory, memory_id)
        async with app.state.store.lock, app.state.store.sessions() as db:
            memory = await db.get(Memory, memory_id)
            await db.delete(memory)
            await db.commit()

    @app.get("/api/profile")
    async def get_profile():
        """用户画像：整段文档，不是 key-value 列表（与 /api/memories 分工不同）。"""
        return await asyncio.to_thread(profile_module.load, settings)

    @app.put("/api/profile")
    async def put_profile(body: ProfileInput):
        return await asyncio.to_thread(profile_module.save, settings, body.body)

    @app.post("/api/profile/entries")
    async def add_profile_entry(body: ProfileEntryInput):
        result = await asyncio.to_thread(profile_module.append, settings, body.entry)
        if not result.get("added"):
            result["notice"] = "这条画像已经存在，没有重复添加。"
        return result

    @app.delete("/api/profile/entries")
    async def drop_profile_entry(entry: str):
        result = await asyncio.to_thread(profile_module.remove, settings, entry)
        if not result.get("removed"):
            result["notice"] = f"没有找到匹配「{entry}」的画像条目。"
        return result

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

    # --- 技能操作面 ---------------------------------------------------------
    # 文件是真值（`data/skills/<name>/SKILL.md`），`skills` 表是索引+统计。这套端点
    # 给技能一个"操作面"：此前它是全项目唯一没有任何 API / 前端的能力，只能手工编辑 md。

    def _skill_root():
        return settings.data_dir / "skills"

    def _clean_skill_name(name: str) -> str:
        from app.skills import _validate_name

        try:
            return _validate_name(name)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    async def _skill_row(name: str):
        return await app.state.store.rows(select(Skill).where(Skill.name == name))

    @app.get("/api/skills")
    async def list_skills():
        """技能列表：文件元数据 + 表统计合并（治理态 + 用量 + 成功率），并含 MCP prompts。"""
        from app.skills import catalog, sync_to_store

        root = _skill_root()
        await sync_to_store(app.state.store, root)
        rows = {r.name: r for r in await app.state.store.rows(select(Skill))}
        result = []
        for skill in catalog(root, limit=10_000):
            row = rows.get(skill["name"])
            result.append(
                {
                    "name": skill["name"],
                    "scope": skill["scope"],
                    "version": skill["version"],
                    "triggers": skill["triggers"],
                    "status": str(getattr(row, "status", "active") or "active"),
                    "usage_count": int(getattr(row, "usage_count", 0) or 0),
                    "success_rate": float(getattr(row, "success_rate", 0.0) or 0.0),
                    "consecutive_failures": int(getattr(row, "consecutive_failures", 0) or 0),
                    "last_used_at": str(getattr(row, "last_used_at", "") or ""),
                    "updated_at": str(getattr(row, "updated_at", "") or ""),
                    "source": str(getattr(row, "source", "local") or "local"),
                }
            )
        # 第二来源：MCP prompts（只读，随连接生灭）。
        mcp = getattr(app.state, "mcp", None)
        if mcp is not None:
            for prompt in mcp.prompt_skills():
                result.append(
                    {
                        "name": prompt["name"],
                        "scope": prompt.get("scope", "global"),
                        "version": 0,
                        "triggers": prompt.get("triggers") or [],
                        "status": prompt.get("status", "active"),
                        "usage_count": 0,
                        "success_rate": 0.0,
                        "consecutive_failures": 0,
                        "last_used_at": "",
                        "updated_at": "",
                        "source": prompt.get("source", "mcp"),
                    }
                )
        return result

    @app.get("/api/skills/{name}")
    async def get_skill(name: str):
        """单个技能：正文 + 元数据 + 治理统计。"""
        from app.skills import load_skill, sync_to_store

        root = _skill_root()
        clean = _clean_skill_name(name)
        await sync_to_store(app.state.store, root)
        skill = load_skill(root, clean)
        if not skill["body"]:
            raise HTTPException(404, "技能不存在")
        rows = await _skill_row(clean)
        row = rows[0] if rows else None
        return {
            "name": skill["name"],
            "scope": skill["scope"],
            "version": skill["version"],
            "triggers": skill["triggers"],
            "body": skill["body"],
            "status": str(getattr(row, "status", "active") or "active"),
            "usage_count": int(getattr(row, "usage_count", 0) or 0),
            "success_rate": float(getattr(row, "success_rate", 0.0) or 0.0),
            "consecutive_failures": int(getattr(row, "consecutive_failures", 0) or 0),
            "last_used_at": str(getattr(row, "last_used_at", "") or ""),
        }

    @app.put("/api/skills/{name}")
    async def put_skill(name: str, body: SkillInput):
        """保存/更新技能正文（原样写文件，更新时 version+1）。不做治理决策。

        - 新建 → 状态 `unreviewed`（待确认，确认前不注入）
        - 更新 → 状态 `active`（人工编辑即确认；stale/archived 因 version 递增而复活）
        """
        from app.skills import load_skill, save_skill, sync_to_store

        root = _skill_root()
        clean = _clean_skill_name(name)
        existing = load_skill(root, clean)

        if existing["body"]:
            scope = body.scope or existing["scope"]
            triggers = body.triggers if body.triggers else existing["triggers"]
            version = existing["version"] + 1
            status = "active"
            notice = f"已更新到 v{version}"
        else:
            scope = body.scope or "global"
            triggers = body.triggers
            version = 1
            status = "unreviewed"
            notice = "新建（待确认启用）"

        try:
            skill = save_skill(root, clean, scope, triggers, body.body, status=status, version=version)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        await sync_to_store(app.state.store, root)
        return {**skill, "status": status, "notice": notice}

    @app.delete("/api/skills/{name}")
    async def delete_skill(name: str):
        """删文件 + 标 archived（保留统计，可恢复）。不做真删（`?purge=1` 无此功能）。"""
        import shutil

        from app.skills import sync_to_store
        from app.storage import now

        root = _skill_root()
        clean = _clean_skill_name(name)
        target = root / clean
        removed = False
        if target.is_dir():
            shutil.rmtree(target)
            removed = True
        await sync_to_store(app.state.store, root)  # 文件没了 → 行标 archived
        async with app.state.store.lock, app.state.store.sessions() as session:
            row = await session.scalar(select(Skill).where(Skill.name == clean))
            if row is not None:
                row.status = "archived"
                row.updated_at = now()
                await session.commit()
        return {"name": clean, "status": "archived", "removed": removed, "notice": "已移除（保留统计，可恢复）"}

    @app.post("/api/skills/{name}/status")
    async def set_skill_status(name: str, body: SkillStatusInput):
        """治理态转移。

        文件是 truth：对 `active`/`unreviewed` 这类文件态，同时重写文件 frontmatter 的
        status 让文件与表一致；`stale`/`archived` 是治理态，只动表（文件保持，下次
        同步靠"version 未递增则保留治理态"的规则不再复活）。

        恢复（archived → active 且文件已删）：用表里保留的正文重建文件。
        这里**不能**在末尾再 `sync_to_store`——文件里若是旧 status，同步会把刚改的表
        状态又覆盖回去（unreviewed → 表标 active 又被 sync 拽回 unreviewed）。
        """
        from app.skills import load_skill, save_skill
        from app.storage import now

        root = _skill_root()
        clean = _clean_skill_name(name)
        new_status = body.status
        if new_status not in ("active", "unreviewed", "stale", "archived"):
            raise HTTPException(422, "非法状态（active/unreviewed/stale/archived）")

        file_skill = load_skill(root, clean)

        # 恢复：文件已删 → 用表里保留的正文重建（先读表拿 body）
        if new_status == "active" and not file_skill["body"]:
            rows = await _skill_row(clean)
            if not rows:
                raise HTTPException(404, "技能不存在")
            row = rows[0]
            restored_body = str(row.body or "").strip()
            if not restored_body:
                raise HTTPException(422, "技能正文已丢失，无法恢复")
            save_skill(
                root,
                clean,
                str(row.scope or "global"),
                list(row.triggers or []),
                restored_body,
                status="active",
                version=int(row.version or 0) + 1,
            )
        # 文件态：确认/降级到 unreviewed 时，让文件 frontmatter 与目标一致
        elif new_status in ("active", "unreviewed") and file_skill["body"]:
            save_skill(
                root,
                clean,
                file_skill["scope"],
                file_skill["triggers"],
                file_skill["body"],
                status=new_status,
                version=file_skill["version"],
            )

        async with app.state.store.lock, app.state.store.sessions() as session:
            row = await session.scalar(select(Skill).where(Skill.name == clean))
            if row is None:
                raise HTTPException(404, "技能不存在")
            row.status = new_status
            row.updated_at = now()
            if new_status == "active":
                # 确认/复活即视为一次有效使用，记录时间锚点，防止"刚启用就被超期归档"
                row.last_used_at = now()
            await session.commit()
        return {"name": clean, "status": new_status}

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

    # dashboard 的 docs / files / webhooks 三页后端（MATRIX 16.1）。
    # 单独成模块的理由：它们共享同一套路径校验，而那套校验是最容易写漏的部分——
    # 放一起才能只写一次。**必须在 mount("/") 之前**，否则静态站会把 /api/* 吃掉。
    from app import dashboard_api

    app.include_router(dashboard_api.router)

    # 插件声明的 dashboard 页面路由（MATRIX 13.8）。
    #
    # **这一行是 13.8 从「机制在、没挂上」变成「真挂上」的唯一差别**：`build_router()`
    # 本身早就写好、测试也过，但之前只在测试自己的 app 上挂过——生产里
    # `/api/plugins/<name>/…` 会 404。模块 docstring 里那五处「main.py 是禁改区」
    # 是给子agent 的写入约束，不是设计决定，现已作废。
    #
    # **必须先 discover 再 mount**：`mount_into_app()` 读的是全局 `PAGES`，
    # 而 `discover()` 只返回清单、**不往 `PAGES` 里填**。少了发现这一步，
    # `mount_into_app` 会安静地挂 0 条路由——那种"挂上了但没内容"比 404 更难查。
    #
    # 放在 `mount("/")` 之前：静态站会把 /api/* 吃掉。
    # 插件抛异常已在 `build_router` 里逐条隔离，所以这里不包 try。
    from app import plugin_dashboard

    found = plugin_dashboard.discover(settings.data_dir / "plugins")
    for manifest in found.manifests:
        plugin_dashboard.PAGES.add(manifest)
    app.state.plugin_pages = plugin_dashboard.mount_into_app(app)

    dist = ROOT / "frontend" / "dist"
    if dist.exists():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app


app = create_app()
