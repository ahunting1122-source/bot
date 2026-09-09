import asyncio
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from pydantic import Field
from sqlalchemy import select

from app.context import build_context
from app.storage import Conversation, Message, Task
from app.tools import Args, Tool, as_tool_text


class AskArgs(Args):
    question: str = Field(description="需要用户回答或在浏览器处理的具体事项")
    reason: str = Field(default="clarification", description="clarification、login 或 verification")


class AgentState(TypedDict):
    task_id: str
    messages: Annotated[list[AnyMessage], add_messages]
    calls: list[dict]
    actions: int
    answer: dict
    waiting: dict


class AgentRunner:
    def __init__(self, settings, store, registry, model=None):
        self.settings, self.store, self.registry = settings, store, registry
        self.model = model
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
        if task_id in self.current:
            self.current[task_id].cancel()

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
            conversation = await self.store.get(Conversation, task.conversation_id)
            conversation.summary = f"最近任务：{task.goal[:1000]}\n结果：{text[:2500]}"
            await self.store.save(conversation)
            await self.store.transition(task.id, "SUCCEEDED")
            await self.store.event(task.id, "task_completed", text=text)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = (
                "模型调用或执行失败，请检查配置和连接后新建任务。"
                if self.settings.model_api_key.get_secret_value()
                else "请先在设置中配置模型名称、API 地址和 API Key。"
            )
            await self.store.transition(
                task.id, "FAILED", error=f"{message} ({type(exc).__name__})"
            )
            await self.store.event(
                task.id, "task_failed", error=f"{message} ({type(exc).__name__})"
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
        response = await model.ainvoke(messages)
        if response.text:
            await self.store.event(task.id, "plan_updated", text=response.text)
        return {"messages": [response], "calls": response.tool_calls, "waiting": {}, "answer": {}}

    async def gate(self, state):
        call = state["calls"][0]
        tool = self.registry.tools.get(call["name"])
        if not tool:
            return {"answer": {"approved": True}}
        question = state.get("waiting")
        if not question and call["name"] == "ask_user":
            question = call["args"]
        if not question and tool.confirmation:
            question = {
                "reason": "approval",
                "question": f"确认执行 {call['name']}？",
                "tool": call["name"],
                "args": call["args"],
            }
        if question:
            answer = interrupt(question)
            return {"answer": answer, "waiting": {}}
        return {"answer": {"approved": True}}

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
