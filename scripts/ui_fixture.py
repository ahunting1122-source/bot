"""Isolated dependencies for UI checks; never read a user's env file or keyring."""

import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import Request
from fastapi.responses import JSONResponse
from langchain_core.messages import AIMessage, AIMessageChunk
from pydantic import SecretStr
from starlette.middleware.base import BaseHTTPMiddleware

from app.browser import BrowserSessionError
from app.config import Settings
from app.main import create_app
from app.storage import Conversation, Message, now

MARKDOWN = """# 流式 Markdown 验证

这是 **加粗内容**、*强调内容* 和 `inline_code`。

- 第一项
- 第二项

1. 有序项目
2. 第二项

> 这是一段引用。

| 字段 | 值 |
| --- | --- |
| 渲染 | 通过 |

```python
print("markdown-ok")
```

[本地参考链接](/fixture-link)

MARKDOWN_DONE
"""


class ScriptedModel:
    """Drive actual LangGraph states without an external model request."""

    def __init__(self):
        self.retry_failed = False
        self.stream_release = asyncio.Event()
        self.stream_release.set()
        self.stream_finish_release = asyncio.Event()
        self.stream_finish_release.set()
        self.stop_started = asyncio.Event()
        self.stop_calls = 0

    def bind_tools(self, *args, **kwargs):
        return self

    async def astream(self, messages):
        if messages[-1].type == "tool":
            result = json.loads(messages[-1].content)
            if result.get("status") == "denied":
                text = "DENIED_DONE：已尊重你的选择，没有写入。"
            elif "answer" in result:
                text = "CLARIFY_DONE：收到补充，" + result["answer"]
            else:
                text = "APPROVED_DONE：操作已实际保存。"
            yield AIMessageChunk(content=text)
            return
        human = next((str(m.content) for m in reversed(messages) if m.type == "human"), "")
        if "UI_RETRY" in human and not self.retry_failed:
            self.retry_failed = True
            raise RuntimeError("isolated fixture's intentional first-attempt failure")
        if "UI_STOP" in human:
            self.stop_calls += 1
            if self.stop_calls > 1:
                yield AIMessageChunk(content="STOP_RETRY_DONE：停止后重新回复成功。")
                return
            for index in range(300):
                yield AIMessageChunk(content=f"\n\n### 连续输出第 {index + 1} 段\n\n" + "这是一段用于验证长回复滚动和停止交互的正文。" * 8)
                if index == 19:
                    self.stop_started.set()
                await asyncio.sleep(0.08)
            return
        if "UI_ACCEPT" in human or "UI_DENY" in human:
            token = "UI_ACCEPT" if "UI_ACCEPT" in human else "UI_DENY"
            yield AIMessageChunk(
                content="我会先征求确认，再保存这个偏好。",
                tool_call_chunks=[{
                    "index": 0, "id": token, "name": "memory_save",
                    "args": json.dumps({"key": token, "value": "测试偏好"}),
                }],
            )
            return
        if "UI_ASK" in human:
            yield AIMessageChunk(
                content="我需要确认资料范围。",
                tool_call_chunks=[{
                    "index": 0, "id": "UI_ASK", "name": "ask_user",
                    "args": json.dumps({"question": "你希望整理哪个月份的资料？", "reason": "clarification"}),
                }],
            )
            return
        if "UI_STREAM" in human:
            yield AIMessageChunk(content=MARKDOWN[:80])
            await self.stream_release.wait()
            for offset in range(80, len(MARKDOWN), 24):
                if offset + 24 >= len(MARKDOWN):
                    await self.stream_finish_release.wait()
                await asyncio.sleep(0.07)
                yield AIMessageChunk(content=MARKDOWN[offset : offset + 24])
            return
        if "UI_RETRY" in human:
            text = "RETRY_DONE：第二次请求成功。"
        else:
            text = "CHAT_DONE：已收到你的消息。"
        for offset in range(0, len(text), 8):
            await asyncio.sleep(0.07)
            yield AIMessageChunk(content=text[offset : offset + 8])


class SummaryModel:
    def __init__(self):
        self.fail_next = False
        self.release = asyncio.Event()
        self.release.set()

    async def ainvoke(self, messages):
        await self.release.wait()
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("isolated fixture summary failure")
        text = str(messages[-1].content)
        if "资料整理" in text:
            title = "资料整理回顾"
        elif "登录讨论" in text:
            title = "登录讨论回顾"
        else:
            title = "交互验证对话"
        return AIMessage(content=json.dumps({
            "title": title,
            "summary": "主题：回顾实际对话。\n结论：已讨论资料与实现方式。\n待解决：继续补充具体内容。",
        }, ensure_ascii=False))


class UIFixture:
    def __init__(self, directory: Path, base_url: str):
        self.base_url = base_url
        self.model = ScriptedModel()
        self.summary_model = SummaryModel()
        self.secrets: dict[tuple[str, str], str] = {}
        self.browser_calls: list[str] = []
        self.model_checks: list[str] = []
        self.ids: dict[str, str] = {}
        self.cut_sse = False
        self.sse_cut = asyncio.Event()
        self.settings = Settings(
            _env_file=None, data_dir=directory / "data", workspace=directory / "workspace",
            model="ui-fixture", model_api_key=SecretStr("ui-fixture-not-a-real-key"),
            model_base_url=base_url + "/fixture-model", github_token=SecretStr(""),
            github_repo="", notion_url="", browser_url="", headless=True,
            allowed_origins=(base_url,),
        )
        self.app = create_app(self.settings)
        fixture = self

        class CutEventStream(BaseHTTPMiddleware):
            async def dispatch(self, request, call_next):
                response = await call_next(request)
                if request.url.path.endswith("/events"):
                    original = response.body_iterator

                    async def interrupted():
                        async for chunk in original:
                            if fixture.cut_sse:
                                fixture.cut_sse = False
                                fixture.sse_cut.set()
                                break
                            yield chunk

                    response.body_iterator = interrupted()
                return response

        self.app.add_middleware(CutEventStream)
        original_lifespan = self.app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app):
            with (
                patch("app.connectors.keyring.get_password", side_effect=self.get_secret),
                patch("app.connectors.keyring.set_password", side_effect=self.set_secret),
                # keyring is the same module object for every importer, so this also keeps the
                # 密钥存储 page away from the real Windows credential store.
                patch("app.connectors.keyring.delete_password", side_effect=self.delete_secret),
            ):
                async with original_lifespan(app):
                    app.state.runner.model = self.model
                    app.state.summaries.model = self.summary_model
                    app.state.browser.open_session = self.open_browser
                    await app.state.github.client.aclose()
                    app.state.github.client = httpx.AsyncClient(
                        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"items": []}))
                    )
                    await self.seed()
                    yield

        self.app.router.lifespan_context = lifespan

        @self.app.post("/fixture-model/chat/completions")
        async def completion(request: Request):
            body = await request.json()
            self.model_checks.append(body["model"])
            if body["model"] == "fixture-fail":
                return JSONResponse({"error": "intentional fixture failure"}, status_code=401)
            return {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}

        self.app.router.routes.insert(0, self.app.router.routes.pop())

        @self.app.get("/fixture-link")
        async def reference():
            return {"reference": "local UI test link"}

        self.app.router.routes.insert(0, self.app.router.routes.pop())

    def get_secret(self, service, account):
        return self.secrets.get((service, account))

    def set_secret(self, service, account, value):
        self.secrets[(service, account)] = value

    def delete_secret(self, service, account):
        self.secrets.pop((service, account), None)

    async def open_browser(self, url=None):
        target = url or self.settings.browser_url or self.settings.notion_url
        self.app.state.browser.validate_url(target)
        self.browser_calls.append(target)
        if "fail" in target:
            raise BrowserSessionError("navigation", "测试网页暂时无法打开，请重试。")
        return {"url": target, "title": "隔离网页窗口", "mode": "headed", "visible": True}

    async def seed(self):
        store = self.app.state.store
        for key, title, content in [
            ("ready", "资料整理", "资料整理：决定使用 Markdown 保存每周笔记。"),
            ("empty", "登录讨论", "登录讨论：需要人工确认网页登录。"),
            ("blank", "尚未开始", ""),
        ]:
            row = await store.save(Conversation(title=title))
            self.ids[key] = row.id
            if content:
                message = await store.save(Message(conversation_id=row.id, role="user", content=content))
                await store.save(Message(conversation_id=row.id, role="assistant", content="已经了解。"))
                if key == "ready":
                    row = await store.get(Conversation, row.id)
                    row.summary = "主题：资料整理。结论：每周使用 Markdown 笔记。"
                    row.summary_status = "ready"
                    row.summary_updated_at = now()
                    row.summary_message_count = 2
                    row.summary_message_id = message.id
                    await store.save(row)
