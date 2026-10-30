"""飞书文档读取（`5.14` 的 `collab` 域，第 105 轮）。

## 为什么只做这一条，不做另外 4 条评论工具

`feishu_doc_read` 的链路短且明确：
1. `POST /open-apis/auth/v3/tenant_access_token/internal`（`app_id` + `app_secret`）→ 令牌；
2. `GET /open-apis/docx/v1/documents/{doc_token}/raw_content` → 纯文本。

**另外 4 条评论工具**（`feishu_drive_*`）的请求体形状（尤其 `reply_list` 的嵌套）
**我没有可靠参考** —— 凭猜写出来的工具会通过我自己写的 `MockTransport`
（**用我的假设验证我的假设**），那是假覆盖。

**判据：能写出「请求形状」的工具才写；写不准的先记清「缺哪份 API 参考」。**

## 令牌缓存

`tenant_access_token` 有效期 2 小时，**每次调用都重新换令牌会被飞书限流**。
这里在进程内缓存，提前 5 分钟过期。
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from pydantic import Field

from app.tools import Args, Tool

OPEN_BASE = "https://open.feishu.cn/open-apis"
TIMEOUT_SECONDS = 20.0
#: 令牌提前过期的余量（秒）。
TOKEN_SKEW = 300


class FeishuDocReadArgs(Args):
    doc_token: str = Field(min_length=1, description="飞书文档的 doc_token（URL 里 /docx/ 后面那串）")


FEISHU_TOOLS = [
    Tool("feishu_doc_read", "读飞书 / Lark 文档全文为纯文本", FeishuDocReadArgs),
]


class FeishuConnector:
    """把 `feishu_doc_read` 接到飞书开放平台。

    **没配 app_id / app_secret 就返回能照着做的错误** ——
    模型看得懂「去设置里配 X」，看不懂 traceback。
    """

    def __init__(self, settings: Any, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport
        self._token: str = ""
        self._token_deadline: float = 0.0

    def _secret(self, field: str) -> str:
        value = getattr(self.settings, field, None)
        if value is None:
            return ""
        if hasattr(value, "get_secret_value"):
            return str(value.get_secret_value())
        return str(value)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=OPEN_BASE, timeout=TIMEOUT_SECONDS, transport=self.transport
        )

    async def _tenant_token(self, client: httpx.AsyncClient) -> str:
        """换 `tenant_access_token`，进程内缓存到快过期为止。"""
        now = time.monotonic()
        if self._token and now < self._token_deadline:
            return self._token
        response = await client.post(
            "/auth/v3/tenant_access_token/internal",
            json={
                "app_id": self._secret("feishu_app_id"),
                "app_secret": self._secret("feishu_app_secret"),
            },
        )
        response.raise_for_status()
        body = response.json()
        if body.get("code") not in (0, None):
            raise RuntimeError(f"换令牌失败：code={body.get('code')} msg={body.get('msg')}")
        token = str(body.get("tenant_access_token") or "")
        if not token:
            raise RuntimeError("换令牌失败：响应里没有 tenant_access_token")
        expire = int(body.get("expire") or 7200)
        self._token = token
        self._token_deadline = now + max(60, expire - TOKEN_SKEW)
        return token

    async def execute(self, name, args, task_id, operation_id):
        if name != "feishu_doc_read":
            raise ValueError(f"未知的飞书工具：{name}")

        if not self._secret("feishu_app_id").strip():
            return {"status": "error", "error": "请在设置里配置 `feishu_app_id`（飞书开放平台应用的 App ID）"}
        if not self._secret("feishu_app_secret").strip():
            return {
                "status": "error",
                "error": "请在设置里配置 `feishu_app_secret`（飞书开放平台应用的 App Secret）",
            }

        doc_token = str(args["doc_token"])
        async with self._client() as client:
            try:
                token = await self._tenant_token(client)
                response = await client.get(
                    f"/docx/v1/documents/{doc_token}/raw_content",
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                body = response.json()
                if body.get("code") not in (0, None):
                    return {
                        "status": "error",
                        "error": f"飞书返回 code={body.get('code')} msg={body.get('msg')}",
                    }
                content = str((body.get("data") or {}).get("content") or "")
                return {
                    "status": "ok",
                    "doc_token": doc_token,
                    "chars": len(content),
                    "content": content,
                }
            except httpx.HTTPStatusError as exc:
                return {
                    "status": "error",
                    "error": f"飞书返回 {exc.response.status_code}：{exc.response.text[:200]}",
                }
            except httpx.HTTPError as exc:
                return {"status": "error", "error": f"连不上飞书：{type(exc).__name__}: {exc}"}
            except RuntimeError as exc:
                return {"status": "error", "error": str(exc)}
