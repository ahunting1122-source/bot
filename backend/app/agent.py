import asyncio
import time
from typing import Annotated, Any, TypedDict
from uuid import uuid4

from langchain_core.messages import AnyMessage, ToolMessage, message_chunk_to_message
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from pydantic import Field
from sqlalchemy import select

from app.context import build_context
from app.storage import Message, Task
from app.tools import Args, Tool, as_tool_text
from app.vault import Vault


class AskArgs(Args):
    question: str = Field(description="需要用户回答或在浏览器处理的具体事项")
    reason: str = Field(default="clarification", description="clarification、login 或 verification")
    tried: str = Field(
        default="",
        description="请求接管前已经尝试过的做法，例如换过哪些入口网址、是否用密钥存储登录过",
    )


def tried_tool(state, name: str) -> bool:
    for message in state.get("messages", []):
        for call in getattr(message, "tool_calls", None) or []:
            if call.get("name") == name:
                return True
    return False


class AgentState(TypedDict):
    task_id: str
    messages: Annotated[list[AnyMessage], add_messages]
    calls: list[dict]
    actions: int
    answer: dict
    waiting: dict


class AgentRunner:
    def __init__(self, settings, store, registry, model=None, summaries=None):
        self.settings, self.store, self.registry = settings, store, registry
        self.model = model
        self.summaries = summaries
        self.partial: dict[str, str] = {}
        self.queue: asyncio.Queue[str] = asyncio.Queue()
        self.queued: set[str] = set()
        self.current: dict[str, asyncio.Task] = {}
        registry.tools["ask_user"] = Tool(
            "ask_user", "请求澄清或浏览器人工接管，回答后继续原任务。", AskArgs
        )

    async def start(self):
        self.checkpoint_context = AsyncSqliteSaver.from_conn_string(
            str(self.settings.data_dir / "checkpoints.sqlite")
        )
        self.checkpointer = await self.checkpoint_context.__aenter__()
        graph = StateGraph(AgentState)
        graph.add_node("think", self.think)
        graph.add_node("gate", self.gate)
        graph.add_node("execute", self.execute)
        graph.add_edge(START, "think")
        graph.add_conditional_edges("think", lambda state: "gate" if state["calls"] else END)
        graph.add_edge("gate", "execute")
        graph.add_conditional_edges("execute", lambda state: "gate" if state["calls"] else "think")
        self.graph = graph.compile(checkpointer=self.checkpointer)
        for task in await self.store.rows(
            select(Task).where(Task.status.in_(["PENDING", "RUNNING"]))
        ):
            if task.status == "PENDING":
                self.queued.add(task.id)
                await self.queue.put(task.id)
            else:
                await self.store.transition(
                    task.id,
                    "WAITING_USER",
                    waiting={
                        "reason": "recovery_check",
                        "question": "程序上次执行时退出。继续后会核对操作记录，浏览器将重新观察。",
                    },
                )
        self.worker = asyncio.create_task(self.work())

    async def close(self):
        self.worker.cancel()
        for task in self.current.values():
            task.cancel()
        await asyncio.gather(self.worker, *self.current.values(), return_exceptions=True)
        await self.checkpoint_context.__aexit__(None, None, None)

    async def enqueue(self, task_id: str, resume: dict | None = None):
        if task_id in self.queued or task_id in self.current:
            return
        await self.store.transition(
            task_id, "PENDING", waiting={"resume": resume} if resume else {}
        )
        self.queued.add(task_id)
        await self.queue.put(task_id)

    async def cancel(self, task_id: str):
        job = self.current.get(task_id)
        if job:
            job.cancel()
            await asyncio.gather(job, return_exceptions=True)

    async def work(self):
        while True:
            task_id = await self.queue.get()
            self.queued.discard(task_id)
            task = await self.store.get(Task, task_id)
            if not task or task.status == "CANCELLED":
                self.queue.task_done()
                continue
            job = asyncio.create_task(self.run(task))
            self.current[task_id] = job
            try:
                await job
            except asyncio.CancelledError:
                worker = asyncio.current_task()
                if worker is not None and worker.cancelling():
                    raise
            finally:
                self.current.pop(task_id, None)
                self.queue.task_done()

    async def run(self, task):
        config: RunnableConfig = {"configurable": {"thread_id": task.id}, "recursion_limit": 200}
        try:
            resume = task.waiting.get("resume")
            await self.store.transition(task.id, "RUNNING", waiting={}, error="")
            await self.store.event(task.id, "task_started")
            snapshot = await self.graph.aget_state(config)
            if not snapshot.values:
                initial: Any = {
                    "task_id": task.id,
                    "messages": await build_context(self.store, task, self.settings),
                    "actions": 0,
                    "calls": [],
                    "waiting": {},
                    "answer": {},
                }
            elif snapshot.interrupts:
                initial = Command(resume=resume or {"approved": True, "answer": "继续"})
            else:
                initial = None
            result = await self.graph.ainvoke(initial, config=config)
            snapshot = await self.graph.aget_state(config)
            if snapshot.interrupts:
                waiting = snapshot.interrupts[0].value
                await self.store.transition(task.id, "WAITING_USER", waiting=waiting)
                await self.store.event(task.id, "waiting_user", **waiting)
                return
            text = result["messages"][-1].text
            # Stable IDs prevent duplicate final messages if completion is replayed.
            await self.store.save(
                Message(
                    id=f"answer-{task.id}",
                    conversation_id=task.conversation_id,
                    role="assistant",
                    content=text,
                )
            )
            await self.store.transition(task.id, "SUCCEEDED")
            await self.store.event(task.id, "task_completed", text=text)
        except asyncio.CancelledError:
            await self.save_partial(task)
            raise
        except Exception as exc:
            await self.save_partial(task)
            messages = {
                "AuthenticationError": "模型密钥未通过验证，请检查连接设置。",
                "PermissionDeniedError": "模型服务拒绝访问，请检查账户权限。",
                "RateLimitError": "模型服务当前限流或余额不足，请稍后重试或检查账户。",
                "APITimeoutError": "模型响应超时，可以重试这次回复。",
                "APIConnectionError": "暂时无法连接模型服务，请检查 API 地址和网络。",
            }
            message = messages.get(type(exc).__name__, "回复中断，可以重试或检查连接设置。")
            if not self.settings.model_api_key.get_secret_value() and self.model is None:
                message = "请先在连接与设置中填写模型名称、API 地址和密钥。"
            await self.store.transition(task.id, "FAILED", error=message)
            await self.store.event(task.id, "task_failed", error=message)
        finally:
            self.partial.pop(task.id, None)
            if self.summaries:
                current = await self.store.get(Task, task.id)
                if current and current.status in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                    self.summaries.schedule(task.conversation_id)

    async def save_partial(self, task):
        text = self.partial.get(task.id, "")
        if text:
            await self.store.save(
                Message(
                    id=f"answer-{task.id}",
                    conversation_id=task.conversation_id,
                    role="assistant",
                    content=text,
                )
            )

    def get_model(self):
        if self.model is not None:
            return self.model
        if not self.settings.model or not self.settings.model_api_key.get_secret_value():
            raise ValueError("模型尚未配置")
        return ChatOpenAI(
            model=self.settings.model,
            base_url=self.settings.model_base_url,
            api_key=self.settings.model_api_key,
            timeout=60,
            max_retries=1,
        )

    async def think(self, state):
        if state["actions"] >= self.settings.max_actions:
            raise RuntimeError("已达到最大工具动作数")
        task = await self.store.get(Task, state["task_id"])
        context = await build_context(self.store, task, self.settings)
        messages = [context[0], *[m for m in state["messages"] if m.type != "system"]]
        model = self.get_model().bind_tools(self.registry.specs(), parallel_tool_calls=False)
        response_id = uuid4().hex
        identity = {
            "conversation_id": task.conversation_id,
            "message_id": f"answer-{task.id}",
            "response_id": response_id,
        }
        self.partial[task.id] = ""
        await self.store.event(task.id, "message_started", **identity)
        response: Any = None
        if hasattr(model, "astream"):
            full = None
            pending = ""
            last_sent = time.monotonic()
            async for chunk in model.astream(messages):
                full = chunk if full is None else full + chunk
                # Only outward answer text is transmitted; raw model reasoning is not a summary.
                delta = chunk.text
                if delta:
                    self.partial[task.id] += delta
                    pending += delta
                if pending and (time.monotonic() - last_sent >= 0.06 or len(pending) >= 80):
                    await self.store.event(task.id, "message_delta", **identity, text=pending)
                    pending = ""
                    last_sent = time.monotonic()
            if pending:
                await self.store.event(task.id, "message_delta", **identity, text=pending)
            if full is None:
                raise RuntimeError("模型返回了空响应")
            response = message_chunk_to_message(full)
        else:
            # Scripted test models may provide only ainvoke; production models use astream.
            response = await model.ainvoke(messages)
            self.partial[task.id] = response.text
            if response.text:
                await self.store.event(task.id, "message_delta", **identity, text=response.text)
        answer: Any = response
        if not answer.text and not answer.tool_calls:
            raise RuntimeError("模型没有返回正文或工具调用")
        kind = "activity" if answer.tool_calls else "answer"
        await self.store.event(task.id, "message_finished", **identity, response_kind=kind)
        if answer.tool_calls:
            self.partial[task.id] = ""
            if answer.text:
                await self.store.event(task.id, "activity_summary", text=answer.text)
        return {"messages": [answer], "calls": answer.tool_calls, "waiting": {}, "answer": {}}

    async def gate(self, state):
        call = state["calls"][0]
        tool = self.registry.tools.get(call["name"])
        if not tool:
            return {"answer": {"approved": True}}
        question = state.get("waiting")
        if not question and call["name"] == "ask_user":
            blocked = self.takeover_allowed(state, call["args"] or {})
            if blocked:
                # Hand the request back as a rejection so the model keeps working instead
                # of pushing the problem to the user at the first sign of a login wall.
                return {"answer": {"approved": False, "answer": blocked}}
            question = call["args"]
        if not question and tool.confirmation and not self.registry.granted(call["name"]):
            actions = {
                "memory_save": "记住这个偏好",
                "memory_delete": "忘记这个偏好",
                "browser_click": "点击页面中的这个位置",
                "browser_fill": "在网页中填写这些内容",
                "browser_press": "向网页发送按键",
                "notion_append_summary": "将这些内容追加到 Notion",
                "vault_login": "用密钥存储里保存的账号密码登录这个站点",
            }
            question = {
                "reason": "approval",
                "question": f"允许我{actions.get(call['name'], '进行这项操作')}吗？",
                "tool": call["name"],
                "args": call["args"],
            }
        if question:
            answer = interrupt(question)
            return {"answer": answer, "waiting": {}}
        return {"answer": {"approved": True}}

    def takeover_allowed(self, state, args) -> str:
        """Empty string means the takeover request may proceed.

        A login wall the agent could clear itself with saved credentials is not a reason
        to interrupt the user: try vault_login first, hand over only when that fails.
        """
        if str(args.get("reason", "")) not in {"login", "verification"}:
            return ""
        browser = self.registry.browser
        host = browser.current_host() if browser else ""
        if not tried_tool(state, "vault_login") and host and Vault(self.settings).site_credentials(host):
            return (
                f"密钥存储里有 {host} 的账号，请先用 vault_login 尝试登录；"
                "只有它失败、或出现验证码/二次验证时再请求接管。"
            )
        # One bounded nudge: the first request must say what was already attempted. The
        # second one goes through, so a genuinely blocked run is never stuck here.
        if not str(args.get("tried", "")).strip() and not tried_tool(state, "ask_user"):
            return (
                "请求接管前请在 tried 里写清已经试过什么：换过哪些入口网址、"
                "是否用密钥存储登录过、重新观察后看到什么。确认没有别的办法再请求接管。"
            )
        return ""

    async def execute(self, state):
        call = state["calls"][0]
        if not state["answer"].get("approved", True):
            result = {
                "status": "denied",
                "message": state["answer"].get("answer", "用户拒绝该操作"),
            }
        elif call["name"] == "ask_user":
            result = {
                "status": "ok",
                "answer": state["answer"].get("answer", "用户已完成接管，请重新观察页面"),
            }
        else:
            result = await self.registry.execute(
                state["task_id"], f"{state['task_id']}:{call['id']}", call["name"], call["args"]
            )
        if result["status"] in {"needs_user", "uncertain"}:
            return {
                "waiting": {
                    "reason": result.get("reason", "verification"),
                    "question": result.get("message", "请检查浏览器，完成后继续"),
                    "tool": call["name"],
                },
                "answer": {},
            }
        return {
            "messages": [ToolMessage(content=as_tool_text(result), tool_call_id=call["id"])],
            "calls": state["calls"][1:],
            "actions": state["actions"] + 1,
            "answer": {},
            "waiting": {},
        }
