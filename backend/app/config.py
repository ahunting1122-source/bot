import json
from pathlib import Path
from urllib.parse import urlparse

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


def http_url(value: str, label: str = "网址") -> str:
    """Validate configured URLs without echoing credentials or query strings in errors."""
    value = value.strip()
    if not value:
        return value
    try:
        parsed = urlparse(value)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not any(char.isspace() for char in value)
            and "\\" not in value
        )
        # Accessing port validates its syntax and range.
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"{label}须为有效的 HTTP(S) 地址，且不能包含用户名或密码")
    return value


# Site-scoped approvals may only cover in-page actions. Anything that writes outward,
# deletes, or spends stays confirmed one by one, no matter what the user remembered.
GRANTABLE_TOOLS = frozenset(
    {"browser_click", "browser_fill", "browser_press", "browser_scroll"}
)


def load_grants(raw: str) -> dict[str, list[str]]:
    """Stored as JSON: {"host": ["browser_click", ...]}. Broken data is ignored, never fatal."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        str(host): [str(tool) for tool in tools]
        for host, tools in parsed.items()
        if isinstance(tools, list)
    }


def dump_grants(grants: dict[str, list[str]]) -> str:
    return json.dumps(
        {host: sorted(set(tools)) for host, tools in grants.items() if tools},
        ensure_ascii=False,
    )


def is_notion_host(host: str | None) -> bool:
    if not host:
        return False
    return any(
        host == domain or host.endswith("." + domain)
        for domain in ("notion.so", "notion.com", "notion.site")
    )


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="ORBIT_", env_file=ROOT / "backend" / ".env", extra="ignore"
    )
    data_dir: Path = ROOT / "data"
    workspace: Path = ROOT / "data" / "workspace"
    model: str = ""
    model_base_url: str = "https://api.openai.com/v1"
    model_api_key: SecretStr = SecretStr("")
    github_token: SecretStr = SecretStr("")
    github_repo: str = ""
    notion_url: str = ""
    browser_url: str = ""
    granted_actions: str = ""
    max_actions: int = 20
    # Background tool use may opt into headless; explicit login always opens a visible window.
    headless: bool = False
    allowed_origins: tuple[str, ...] = (
        "http://127.0.0.1:6185",
        "http://localhost:6185",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )

    def prepare(self) -> None:
        for directory in (self.data_dir, self.workspace, self.data_dir / "artifacts"):
            directory.mkdir(parents=True, exist_ok=True)
