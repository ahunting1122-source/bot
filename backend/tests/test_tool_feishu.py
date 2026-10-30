"""`app/tool_feishu.py` 的测试（`5.14` 的 `collab` 域）。"""

from __future__ import annotations

import json
from typing import Any

import httpx

from app.tool_feishu import FEISHU_TOOLS, FeishuConnector

APP_ID = "cli_abc"
APP_SECRET = "secret-xyz"
TOKEN = "t-tenant-123"


class _Secret:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


class FakeSettings:
    def __init__(self, app_id: str = APP_ID, app_secret: str = APP_SECRET) -> None:
        self.feishu_app_id = app_id
        self.feishu_app_secret = _Secret(app_secret)


def _connector(handler, **kwargs) -> FeishuConnector:
    return FeishuConnector(FakeSettings(**kwargs), transport=httpx.MockTransport(handler))


def _two_step(calls: list[str], content: str = "文档正文"):
    """一个会先发令牌、再给文档的 handler，并记录调用顺序。"""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/tenant_access_token/internal"):
            body = json.loads(request.content or b"{}")
            assert body["app_id"] == APP_ID
            assert body["app_secret"] == APP_SECRET
            return httpx.Response(200, json={"code": 0, "tenant_access_token": TOKEN, "expire": 7200})
        return httpx.Response(200, json={"code": 0, "data": {"content": content}})

    return handler


def test_one_tool_is_declared():
    assert [t.name for t in FEISHU_TOOLS] == ["feishu_doc_read"]


async def test_it_exchanges_a_token_then_reads_the_doc():
    """**两步链路**：先换 `tenant_access_token`，再读 `raw_content`。"""
    calls: list[str] = []
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": TOKEN, "expire": 7200}
            )
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"code": 0, "data": {"content": "正文"}})

    result = await _connector(handler).execute("feishu_doc_read", {"doc_token": "doccn1"}, "t", "o")

    assert calls == [
        "/open-apis/auth/v3/tenant_access_token/internal",
        "/open-apis/docx/v1/documents/doccn1/raw_content",
    ]
    assert seen["auth"] == f"Bearer {TOKEN}", "文档请求没带上换来的令牌"
    assert result["status"] == "ok"
    assert result["content"] == "正文"
    assert result["chars"] == 2


async def test_the_token_is_cached_across_calls():
    """**每次调用都重新换令牌会被飞书限流** —— 进程内要缓存。"""
    calls: list[str] = []
    connector = _connector(_two_step(calls))

    await connector.execute("feishu_doc_read", {"doc_token": "a"}, "t", "o1")
    await connector.execute("feishu_doc_read", {"doc_token": "b"}, "t", "o2")

    token_calls = [c for c in calls if c.endswith("/tenant_access_token/internal")]
    assert len(token_calls) == 1, f"换了 {len(token_calls)} 次令牌，应该只换 1 次"


async def test_unconfigured_app_id_returns_actionable_error():
    result = await _connector(lambda r: httpx.Response(200, json={}), app_id="").execute(
        "feishu_doc_read", {"doc_token": "x"}, "t", "o"
    )
    assert result["status"] == "error"
    assert "feishu_app_id" in result["error"]


async def test_unconfigured_secret_returns_actionable_error():
    result = await _connector(lambda r: httpx.Response(200, json={}), app_secret="").execute(
        "feishu_doc_read", {"doc_token": "x"}, "t", "o"
    )
    assert result["status"] == "error"
    assert "feishu_app_secret" in result["error"]


async def test_api_error_code_becomes_a_readable_error():
    """飞书用 `code != 0` 表示业务错误 —— 要把它翻译出来，而不是当成成功。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/tenant_access_token/internal"):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": TOKEN, "expire": 7200})
        return httpx.Response(200, json={"code": 1770002, "msg": "doc not found"})

    result = await _connector(handler).execute("feishu_doc_read", {"doc_token": "nope"}, "t", "o")
    assert result["status"] == "error"
    assert "1770002" in result["error"]


async def test_http_error_becomes_a_readable_error():
    result = await _connector(lambda r: httpx.Response(401, text="bad app")).execute(
        "feishu_doc_read", {"doc_token": "x"}, "t", "o"
    )
    assert result["status"] == "error"
    assert "401" in result["error"]


async def test_connection_failure_becomes_a_readable_error():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("nope", request=request)

    result = await _connector(boom).execute("feishu_doc_read", {"doc_token": "x"}, "t", "o")
    assert result["status"] == "error"
    assert "连不上飞书" in result["error"]
