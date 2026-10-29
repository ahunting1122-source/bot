import asyncio
import json
import re
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
import keyring
from pydantic import Field, SecretStr

from app.config import (
    FILE_SCOPES,
    GRANTABLE_TOOLS,
    dump_grants,
    http_url,
    is_notion_host,
    load_grants,
    normalize_file_scope,
)
from app.egress import refuse_cloud_metadata
from app.tools import Args, SearchArgs, Tool


class IssueArgs(Args):
    number: int = Field(gt=0)


GITHUB_TOOLS = [
    Tool(
        "github_search_issues",
        "在设置中指定的 GitHub 仓库搜索 Issue，返回最多 10 条及来源链接。",
        SearchArgs,
    ),
    Tool("github_get_issue", "读取指定仓库的 Issue 详情。", IssueArgs),
]


class ConnectorConfig:
    def __init__(self, settings):
        self.settings = settings
        self.path = settings.data_dir / "settings.json"
        self.service = f"orbit:{settings.data_dir.resolve()}"
        self._model_status: str | None = None
        self._model_checked_at: str | None = None
        self._model_check_error: str | None = None
        self._model_revision = 0

    public_fields = (
        "model",
        "model_base_url",
        "github_repo",
        "notion_url",
        "browser_url",
        "granted_actions",
        "file_scope",
    )

    def load(self):
        if self.path.exists():
            for key, value in json.loads(self.path.read_text(encoding="utf-8")).items():
                if key == "file_scope":
                    # 坏值即降级到更窄的那一档，绝不让损坏的配置把边界放开。
                    self.settings.file_scope = normalize_file_scope(value)
                elif key in self.public_fields:
                    setattr(self.settings, key, value)
        for key in ("model_api_key", "github_token"):
            try:
                secret = keyring.get_password(self.service, key)
            except keyring.errors.KeyringError:
                secret = None
            if secret:
                setattr(self.settings, key, SecretStr(secret))
        # Older versions put every website in notion_url. Move that address once,
        # preserving any already-configured generic website.
        legacy = self.settings.notion_url
        if legacy and not is_notion_host(urlparse(legacy).hostname):
            try:
                http_url(legacy)
            except ValueError:
                return
            if not self.settings.browser_url:
                self.settings.browser_url = legacy
            elif self.settings.browser_url != legacy:
                # Don't discard a second saved address; leave it for an explicit edit.
                return
            self.settings.notion_url = ""
            self._persist()

    def _persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        stored = {key: getattr(self.settings, key) for key in self.public_fields}
        self.path.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")

    def grant(self, host: str, tool: str) -> dict[str, list[str]]:
        """Remember one in-page action for one site. Writes outward are never remembered."""
        if tool not in GRANTABLE_TOOLS:
            raise ValueError("这类操作不能设为免确认")
        if not re.fullmatch(
            r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?",
            host,
        ):
            raise ValueError("站点主机名无效")
        grants = load_grants(self.settings.granted_actions)
        grants.setdefault(host, [])
        if tool not in grants[host]:
            grants[host].append(tool)
        self.settings.granted_actions = dump_grants(grants)
        self._persist()
        return grants

    def revoke(self, host: str) -> dict[str, list[str]]:
        grants = load_grants(self.settings.granted_actions)
        grants.pop(host, None)
        self.settings.granted_actions = dump_grants(grants)
        self._persist()
        return grants

    def _model_configured(self):
        return bool(
            self.settings.model.strip()
            and self.settings.model_base_url.strip()
            and self.settings.model_api_key.get_secret_value()
        )

    def public(self):
        scope = normalize_file_scope(self.settings.file_scope)
        return {
            "model": self.settings.model,
            "model_base_url": self.settings.model_base_url,
            "github_repo": self.settings.github_repo,
            "notion_url": self.settings.notion_url,
            "browser_url": self.settings.browser_url,
            "grants": load_grants(self.settings.granted_actions),
            "workspace": str(self.settings.workspace.resolve()),
            # 对话页的权限级别开关读的就是这两个字段：`file_scope` 是取值，
            # `file_root` 是这一档实际能落到哪里（machine 档没有单一根目录，给空串）。
            "file_scope": scope,
            "file_root": "" if scope == "machine" else str(self.settings.workspace.resolve()),
            "model_key_set": bool(self.settings.model_api_key.get_secret_value()),
            "github_token_set": bool(self.settings.github_token.get_secret_value()),
            "model_status": (
                (self._model_status or "configured") if self._model_configured() else "unconfigured"
            ),
            "model_checked_at": self._model_checked_at,
            "model_check_error": self._model_check_error,
        }

    def update(self, values):
        allowed = {
            "model",
            "model_base_url",
            "github_repo",
            "notion_url",
            "browser_url",
            "model_api_key",
            "github_token",
            "file_scope",
        }
        if set(values) - allowed:
            raise ValueError("包含不支持的配置项")
        if any(not isinstance(value, str) for value in values.values()):
            raise ValueError("配置项必须为文本")
        values = {key: value.strip() for key, value in values.items()}
        if "file_scope" in values:
            if values["file_scope"] not in FILE_SCOPES:
                # 显式改权限级别时不做静默降级：用户点错了要看得见，
                # 而不是以为切到了完全访问、实际还停在旧档。
                raise ValueError("权限级别只能是 workspace（仅工作文件夹）或 machine（完全访问）")
            self.settings.file_scope = values.pop("file_scope")
        repo = values.get("github_repo", "")
        if repo and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("仓库格式为 owner/repo")
        for field, label in (
            ("model_base_url", "模型接口地址"),
            ("browser_url", "网页起始地址"),
            ("notion_url", "Notion 页面地址"),
        ):
            if field in values:
                values[field] = http_url(values[field], label)
        if values.get("notion_url") and not is_notion_host(urlparse(values["notion_url"]).hostname):
            raise ValueError(
                "Notion 专用地址必须属于 notion.so、notion.com 或 notion.site；其他网站请填网页连接"
            )
        model_changed = any(
            key in values and values[key] != getattr(self.settings, key)
            for key in ("model", "model_base_url")
        ) or bool(
            values.get("model_api_key")
            and values["model_api_key"] != self.settings.model_api_key.get_secret_value()
        )
        for key, value in values.items():
            if key in {"model_api_key", "github_token"}:
                if value:
                    keyring.set_password(self.service, key, value)
                    setattr(self.settings, key, SecretStr(value))
            else:
                setattr(self.settings, key, value.strip())
        if model_changed:
            self._model_revision += 1
            self._model_status = self._model_checked_at = self._model_check_error = None
        self._persist()
        return self.public()

    async def check_model(self):
        """Verify one tiny completion; stored credentials alone never imply connectivity."""
        if not self._model_configured():
            return self.public()
        revision = self._model_revision
        model = self.settings.model
        base_url = self.settings.model_base_url
        token = self.settings.model_api_key.get_secret_value()
        error = None
        try:
            http_url(base_url, "模型接口地址")
            # **云元数据硬拦**（MATRIX 14.11）：`base_url` 来自 `settings.model_base_url`，
            # 是用户可配的。这里是把凭证（`token`）真正发出去的那一步。
            refuse_cloud_metadata(base_url, where="模型连通性检查")
            async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
                response = await client.post(
                    base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "Reply only OK."}],
                        "max_tokens": 16,
                        "stream": False,
                    },
                )
            if response.status_code != 200:
                hints = {
                    401: "请检查 API 密钥",
                    403: "请检查模型访问权限",
                    404: "请检查接口地址和模型名称",
                    429: "请检查余额或稍后重试",
                }
                error = f"模型服务返回 HTTP {response.status_code}，{hints.get(response.status_code, '请检查服务状态和配置')}"
            else:
                payload = response.json()
                choices = payload.get("choices", []) if isinstance(payload, dict) else []
                message = (
                    choices[0].get("message", {})
                    if isinstance(choices, list) and choices and isinstance(choices[0], dict)
                    else {}
                )
                if not isinstance(message, dict) or not (
                    message.get("content") or message.get("reasoning_content")
                ):
                    error = "模型接口未返回有效回复，请检查模型名称及接口兼容性"
        except httpx.TimeoutException:
            error = "模型连接超时，请检查网络和接口地址后重试"
        except httpx.RequestError:
            error = "无法连接模型服务，请检查网络、代理和接口地址"
        except (ValueError, TypeError, AttributeError):
            error = "模型接口地址或返回格式无效，请检查 OpenAI 兼容接口配置"
        # A settings save during the check invalidates the response for the old model.
        if revision == self._model_revision:
            self._model_status = "failed" if error else "verified"
            self._model_checked_at = datetime.now(UTC).isoformat()
            self._model_check_error = error
        return self.public()


class GitHubConnector:
    def __init__(self, settings, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)

    async def close(self):
        await self.client.aclose()

    async def execute(self, name, args, task_id, operation_id):
        repo = self.settings.github_repo
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            return {"status": "error", "error": "请在设置中配置 GitHub 仓库，格式 owner/repo"}
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        token = self.settings.github_token.get_secret_value()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if name == "github_search_issues":
            url = "https://api.github.com/search/issues"
            # Treat input as text, not search qualifiers that could broaden repository scope.
            query = args["query"].replace('"', " ").replace("\\", " ")
            params = {"q": f'"{query}" repo:{repo} is:issue', "per_page": "10", "sort": "updated"}
        else:
            url = f"https://api.github.com/repos/{repo}/issues/{args['number']}"
            params = {}
        for attempt in range(3):
            try:
                response = await self.client.get(url, params=params, headers=headers)
            except httpx.TransportError:
                if attempt == 2:
                    return {"status": "error", "error": "GitHub 网络连接失败", "retryable": True}
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if response.status_code >= 500 and attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if response.status_code != 200:
                return {
                    "status": "error",
                    "error": f"GitHub 返回 {response.status_code}，请检查凭证、仓库权限或限流状态",
                }
            payload = response.json()
            records = payload.get("items", []) if name == "github_search_issues" else [payload]
            return {
                "status": "ok",
                "data": [
                    {
                        "number": r["number"],
                        "title": r["title"],
                        "state": r["state"],
                        "url": r["html_url"],
                        "body": (r.get("body") or "")[: 10000 if len(records) == 1 else 1200],
                    }
                    for r in records
                ],
            }
        return {"status": "error", "error": "GitHub 请求失败"}
