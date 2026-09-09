import asyncio
import json
import re

import httpx
import keyring
from pydantic import Field, SecretStr

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

    def load(self):
        if self.path.exists():
            for key, value in json.loads(self.path.read_text(encoding="utf-8")).items():
                if key in {"model", "model_base_url", "github_repo", "notion_url"}:
                    setattr(self.settings, key, value)
        for key in ("model_api_key", "github_token"):
            try:
                secret = keyring.get_password(self.service, key)
            except keyring.errors.KeyringError:
                secret = None
            if secret:
                setattr(self.settings, key, SecretStr(secret))

    def public(self):
        return {
            "model": self.settings.model,
            "model_base_url": self.settings.model_base_url,
            "github_repo": self.settings.github_repo,
            "notion_url": self.settings.notion_url,
            "workspace": str(self.settings.workspace.resolve()),
            "model_key_set": bool(self.settings.model_api_key.get_secret_value()),
            "github_token_set": bool(self.settings.github_token.get_secret_value()),
        }

    def update(self, values):
        allowed = {
            "model",
            "model_base_url",
            "github_repo",
            "notion_url",
            "model_api_key",
            "github_token",
        }
        if set(values) - allowed:
            raise ValueError("包含不支持的配置项")
        repo = values.get("github_repo", "")
        if repo and not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValueError("仓库格式为 owner/repo")
        for key, value in values.items():
            if key in {"model_api_key", "github_token"}:
                if value:
                    keyring.set_password(self.service, key, value)
                    setattr(self.settings, key, SecretStr(value))
            else:
                setattr(self.settings, key, value.strip())
        public = self.public()
        stored = {
            key: public[key] for key in ("model", "model_base_url", "github_repo", "notion_url")
        }
        self.path.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
        return public


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
