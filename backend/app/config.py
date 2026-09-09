from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


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
    max_actions: int = 20
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
