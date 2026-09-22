import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import keyring
from pydantic import SecretStr

# Only these keys may be read or written. Without a whitelist the API would turn the
# system keyring into a general key-value store for anything that can reach the port.
KNOWN_KEYS = {
    "model_api_key": "模型密钥",
    "github_token": "GitHub 访问 Token",
}

SITE_PREFIX = "site:"
HOST_PATTERN = re.compile(
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
)

# Cookie names that usually mean "this browser is signed in". The judgement is a hint,
# never a guarantee, so the UI labels it as 疑似.
SESSION_HINTS = (
    "sess",
    "session",
    "token",
    "auth",
    "login",
    "uid",
    "user",
    "sid",
    "jwt",
    "ticket",
)


def now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def normalize_host(host: str) -> str:
    host = (host or "").strip().lower().lstrip(".")
    if not host or len(host) > 253 or not HOST_PATTERN.fullmatch(host):
        raise ValueError("站点主机名无效")
    return host


def cookie_host_matches(host: str, cookie_domain: str) -> bool:
    domain = (cookie_domain or "").lower().lstrip(".")
    if not domain:
        return False
    return host == domain or host.endswith("." + domain)


class Vault:
    """Credentials live in the system keyring; only names and timestamps touch disk."""

    def __init__(self, settings):
        self.settings = settings
        self.service = f"orbit:{settings.data_dir.resolve()}"
        self.meta_path: Path = settings.data_dir / "vault.json"

    def _meta(self) -> dict:
        if not self.meta_path.exists():
            return {"sites": [], "updated": {}}
        try:
            loaded = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {"sites": [], "updated": {}}
        sites = [s for s in loaded.get("sites", []) if isinstance(s, str)]
        return {"sites": sites, "updated": dict(loaded.get("updated", {}))}

    def _save_meta(self, meta: dict) -> None:
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        self.meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def _secret(self, key: str) -> str:
        try:
            return keyring.get_password(self.service, key) or ""
        except keyring.errors.KeyringError:
            return ""

    def _store(self, key: str, value: str) -> None:
        keyring.set_password(self.service, key, value)

    def _forget(self, key: str) -> None:
        try:
            keyring.delete_password(self.service, key)
        except keyring.errors.PasswordDeleteError:
            return
        except keyring.errors.KeyringError:
            return

    # --- 密钥 ---------------------------------------------------------------
    def keys(self) -> list[dict]:
        updated = self._meta()["updated"]
        result = []
        for key, label in KNOWN_KEYS.items():
            value = self._secret(key)
            result.append(
                {
                    "key": key,
                    "label": label,
                    "value": value,
                    "set": bool(value),
                    "updated_at": updated.get(key, ""),
                }
            )
        return result

    def set_key(self, key: str, value: str) -> dict:
        if key not in KNOWN_KEYS:
            raise ValueError("不支持这个密钥名称")
        if not value:
            raise ValueError("密钥不能为空")
        self._store(key, value)
        meta = self._meta()
        meta["updated"][key] = now_iso()
        self._save_meta(meta)
        # Keep the live settings in step so the running app uses the new value at once.
        setattr(self.settings, key, SecretStr(value))
        return next(item for item in self.keys() if item["key"] == key)

    def delete_key(self, key: str) -> None:
        if key not in KNOWN_KEYS:
            raise ValueError("不支持这个密钥名称")
        self._forget(key)
        meta = self._meta()
        meta["updated"].pop(key, None)
        self._save_meta(meta)

    # --- 站点账号 -----------------------------------------------------------
    def sites(self) -> list[dict]:
        meta = self._meta()
        updated = meta["updated"]
        result = []
        for host in meta["sites"]:
            stored = self._secret(SITE_PREFIX + host)
            try:
                payload = json.loads(stored) if stored else {}
            except ValueError:
                payload = {}
            result.append(
                {
                    "host": host,
                    "username": str(payload.get("username", "")),
                    "password": str(payload.get("password", "")),
                    "updated_at": updated.get(SITE_PREFIX + host, ""),
                }
            )
        return result

    def set_site(self, host: str, username: str, password: str) -> dict:
        host = normalize_host(host)
        if not username and not password:
            raise ValueError("用户名和密码不能同时为空")
        self._store(SITE_PREFIX + host, json.dumps({"username": username, "password": password}))
        meta = self._meta()
        if host not in meta["sites"]:
            meta["sites"].append(host)
        meta["updated"][SITE_PREFIX + host] = now_iso()
        self._save_meta(meta)
        return next(item for item in self.sites() if item["host"] == host)

    def delete_site(self, host: str) -> None:
        host = normalize_host(host)
        self._forget(SITE_PREFIX + host)
        meta = self._meta()
        meta["sites"] = [item for item in meta["sites"] if item != host]
        meta["updated"].pop(SITE_PREFIX + host, None)
        self._save_meta(meta)

    def site_credentials(self, host: str) -> dict | None:
        """Used by the fill tool. The value never travels through the model."""
        host = normalize_host(host)
        stored = self._secret(SITE_PREFIX + host)
        if not stored:
            return None
        try:
            payload = json.loads(stored)
        except ValueError:
            return None
        return {
            "username": str(payload.get("username", "")),
            "password": str(payload.get("password", "")),
        }


def summarize_logins(hosts: list[str], cookies: list[dict]) -> list[dict]:
    """Guess sign-in state per host from the persistent browser profile's cookies."""
    result = []
    for host in hosts:
        owned = [c for c in cookies if cookie_host_matches(host, c.get("domain", ""))]
        session_cookies = [
            c for c in owned if any(hint in str(c.get("name", "")).lower() for hint in SESSION_HINTS)
        ]
        evidence = [str(c.get("name", "")) for c in session_cookies]
        # Use the longest-lived session cookie. Sites also set short helper cookies
        # (B 站 b_lsid 之类), and judging by the earliest one would always cry "expiring".
        expiries = [
            float(c["expires"])
            for c in session_cookies
            if isinstance(c.get("expires"), int | float) and float(c["expires"]) > 0
        ]
        latest = max(expiries) if expiries else None
        if evidence:
            status = "logged_in"
        elif owned:
            status = "unknown"
        else:
            status = "none"
        soon = latest is not None and latest < (datetime.now(UTC) + timedelta(days=3)).timestamp()
        result.append(
            {
                "host": host,
                "status": status,
                "cookies": len(owned),
                "evidence": sorted(set(evidence))[:6],
                "expires_at": (
                    datetime.fromtimestamp(latest, UTC).replace(microsecond=0).isoformat()
                    if latest
                    else ""
                ),
                "expiring_soon": bool(evidence) and soon,
            }
        )
    return result
