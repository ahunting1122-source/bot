import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

import app.browser as browser_module
import app.vault as vault_module
from app.agent import AgentRunner
from app.browser import BROWSER_TOOLS, BrowserController
from app.config import GRANTABLE_TOOLS, Settings
from app.tools import ToolRegistry
from app.vault import Vault, summarize_logins


@pytest.fixture
def vault(tmp_path, monkeypatch):
    secrets: dict[str, str] = {}
    monkeypatch.setattr(
        vault_module.keyring, "set_password", lambda service, key, value: secrets.update({key: value})
    )
    monkeypatch.setattr(
        vault_module.keyring, "get_password", lambda service, key: secrets.get(key)
    )

    def remove(service, key):
        secrets.pop(key, None)

    monkeypatch.setattr(vault_module.keyring, "delete_password", remove)
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    return Vault(settings), settings, secrets


def test_key_whitelist_blocks_arbitrary_keyring_entries(vault):
    store, _settings, secrets = vault
    store.set_key("model_api_key", "sk-fixture-1234")
    assert [k["value"] for k in store.keys() if k["key"] == "model_api_key"] == ["sk-fixture-1234"]
    with pytest.raises(ValueError):
        store.set_key("some_other_secret", "x")
    with pytest.raises(ValueError):
        store.delete_key("some_other_secret")
    with pytest.raises(ValueError):
        store.set_key("model_api_key", "")
    assert "some_other_secret" not in secrets


def test_only_names_and_timestamps_are_written_to_disk(vault):
    store, settings, _secrets = vault
    store.set_key("github_token", "ghp-fixture-secret")
    store.set_site("www.bilibili.com", "alice", "hunter2")
    stored = (settings.data_dir / "vault.json").read_text(encoding="utf-8")
    assert "ghp-fixture-secret" not in stored
    assert "hunter2" not in stored
    assert "alice" not in stored
    assert json.loads(stored)["sites"] == ["www.bilibili.com"]


def test_site_roundtrip_and_delete(vault):
    store, _settings, secrets = vault
    saved = store.set_site("bilibili.com", "alice", "hunter2")
    assert saved["host"] == "bilibili.com"
    assert store.sites() == [saved]
    assert store.site_credentials("bilibili.com") == {"username": "alice", "password": "hunter2"}
    with pytest.raises(ValueError):
        store.set_site("*.bilibili.com", "alice", "hunter2")
    with pytest.raises(ValueError):
        store.set_site("bilibili.com", "", "")
    store.delete_site("bilibili.com")
    assert store.sites() == []
    assert store.site_credentials("bilibili.com") is None
    assert "site:bilibili.com" not in secrets


def test_login_summary_is_a_hint_not_a_claim():
    cookies = [
        {"name": "SESSDATA", "domain": ".bilibili.com", "expires": 4102444800},
        {"name": "buvid3", "domain": ".bilibili.com", "expires": 4102444800},
        {"name": "cf_clearance", "domain": ".ithome.com", "expires": 1},
    ]
    by_host = {row["host"]: row for row in summarize_logins(["bilibili.com", "www.ithome.com"], cookies)}
    assert by_host["bilibili.com"]["status"] == "logged_in"
    assert by_host["bilibili.com"]["evidence"] == ["SESSDATA"]
    assert by_host["www.ithome.com"]["status"] == "unknown"


def test_expiring_soon_is_flagged():
    cookies = [{"name": "SESSIONID", "domain": "example.com", "expires": 1}]
    row = summarize_logins(["example.com"], cookies)[0]
    assert row["status"] == "logged_in"
    assert row["expiring_soon"] is True


def test_short_helper_cookies_do_not_raise_a_false_alarm():
    # B 站会同时下发长期会话 Cookie 和当天到期的辅助 Cookie。
    cookies = [
        {"name": "SESSDATA", "domain": ".bilibili.com", "expires": 4102444800},
        {"name": "b_lsid", "domain": ".bilibili.com", "expires": 1},
    ]
    row = summarize_logins(["www.bilibili.com"], cookies)[0]
    assert row["status"] == "logged_in"
    assert row["expiring_soon"] is False
    assert row["expires_at"].startswith("2100")


async def test_login_status_needs_an_open_browser(tmp_path):
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    browser = BrowserController(settings, None)
    status = await browser.login_status(["bilibili.com"])
    assert status["ready"] is False
    assert status["logins"] == []


async def test_vault_login_refuses_a_site_without_saved_credentials(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    monkeypatch.setattr(
        vault_module.keyring, "get_password", lambda service, key: None
    )
    browser = BrowserController(settings, None)
    result = await browser.execute(
        "vault_login",
        {"host": "bilibili.com", "username_ref": "e1", "password_ref": "e2", "version": 1},
        "task",
        "operation",
    )
    assert result["status"] == "error"
    assert "没有" in result["error"]


class LoginPage:
    def __init__(self):
        self.url = "https://ai.example/login"
        self.closed = False
        self.filled = []
        self.clicked = []
        self.load_states = []

    def is_closed(self):
        return self.closed

    async def goto(self, url, **kwargs):
        self.url = url

    async def title(self):
        return "登录"

    async def bring_to_front(self):
        pass

    async def wait_for_load_state(self, state=None, **kwargs):
        self.load_states.append(state)

    async def evaluate(self, script):
        return []

    async def screenshot(self, **kwargs):
        return b""

    def locator(self, selector):
        page = self

        class Locator:
            async def fill(self, value):
                page.filled.append((selector, value))

            async def click(self):
                page.clicked.append(selector)

            async def count(self):
                return 1

            async def inner_text(self):
                return "登录页正文" * 30

        return Locator()


class LoginStore:
    def __init__(self):
        self.saved = []

    async def save(self, row):
        self.saved.append(row)

    async def event(self, *args, **kwargs):
        return None


def make_login_browser(tmp_path, monkeypatch):
    page = LoginPage()

    class Context:
        def __init__(self):
            self.pages = [page]
            self.browser = SimpleNamespace(is_connected=lambda: True, on=lambda *args: None)

        def on(self, name, handler):
            pass

        def set_default_timeout(self, timeout):
            pass

        async def new_page(self):
            return page

        async def close(self):
            pass

    class Runtime:
        def __init__(self):
            self.chromium = self

        async def start(self):
            return self

        async def launch_persistent_context(self, profile, **kwargs):
            return Context()

        async def stop(self):
            pass

    monkeypatch.setattr(browser_module, "async_playwright", Runtime)
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    return BrowserController(settings, LoginStore()), page


async def test_login_fills_and_submits_in_one_confirmed_action(tmp_path, monkeypatch):
    browser, page = make_login_browser(tmp_path, monkeypatch)
    stored = json.dumps({"username": "alice", "password": "s3cret"})
    monkeypatch.setattr(vault_module.keyring, "get_password", lambda service, key: stored)
    browser.version = 1
    browser.observed_url = page.url
    browser.refs = {"e1": {}, "e2": {}, "e3": {}}

    result = await browser.login(
        {
            "host": "ai.example",
            "version": 1,
            "username_ref": "e1",
            "password_ref": "e2",
            "submit_ref": "e3",
        },
        "task-1",
    )
    assert result["status"] == "ok"
    assert page.filled == [
        ('[data-orbit-ref="e1"]', "alice"),
        ('[data-orbit-ref="e2"]', "s3cret"),
    ]
    assert page.clicked == ['[data-orbit-ref="e3"]']
    assert "load" in page.load_states


async def test_login_without_submit_ref_only_fills(tmp_path, monkeypatch):
    browser, page = make_login_browser(tmp_path, monkeypatch)
    stored = json.dumps({"username": "alice", "password": "s3cret"})
    monkeypatch.setattr(vault_module.keyring, "get_password", lambda service, key: stored)
    browser.version = 1
    browser.observed_url = page.url
    browser.refs = {"e1": {}, "e2": {}}

    result = await browser.login(
        {"host": "ai.example", "version": 1, "username_ref": "e1", "password_ref": "e2"},
        "task-1",
    )
    assert result["status"] == "ok"
    assert page.clicked == []
    assert "没有提交" in result["message"]


class StubBrowserWithHost:
    def __init__(self, host):
        self.host = host

    def current_host(self):
        return self.host


def make_runner(tmp_path, monkeypatch, *, saved_site: str | None):
    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    if saved_site:
        vault_module.keyring.set_password(
            f"orbit:{settings.data_dir.resolve()}",
            f"site:{saved_site}",
            json.dumps({"username": "alice", "password": "s3cret"}),
        )
    registry = ToolRegistry(settings, None)
    registry.browser = StubBrowserWithHost("target.example")
    return AgentRunner(settings, None, registry, model=None)


def tool_call_message(name: str, args: dict):
    return AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args, "id": f"call-{name}", "type": "tool_call"}],
    )


def takeover_state(already_tried: bool):
    calls = []
    if already_tried:
        calls.append(
            tool_call_message(
                "vault_login",
                {"host": "target.example", "version": 1, "password_ref": "e2"},
            )
        )
    return {"messages": calls, "waiting": {}, "calls": []}


def test_takeover_is_refused_while_saved_credentials_are_untried(tmp_path, monkeypatch):
    secrets: dict[str, str] = {}
    monkeypatch.setattr(
        vault_module.keyring, "set_password", lambda service, key, value: secrets.update({key: value})
    )
    monkeypatch.setattr(vault_module.keyring, "get_password", lambda service, key: secrets.get(key))
    runner = make_runner(tmp_path, monkeypatch, saved_site="target.example")
    blocked = runner.takeover_allowed(
        takeover_state(False), {"reason": "login", "question": "请在浏览器里登录"}
    )
    assert "vault_login" in blocked
    assert "target.example" in blocked


def test_takeover_is_allowed_after_trying_saved_credentials(tmp_path, monkeypatch):
    secrets: dict[str, str] = {}
    monkeypatch.setattr(
        vault_module.keyring, "set_password", lambda service, key, value: secrets.update({key: value})
    )
    monkeypatch.setattr(vault_module.keyring, "get_password", lambda service, key: secrets.get(key))
    runner = make_runner(tmp_path, monkeypatch, saved_site="target.example")
    state = takeover_state(True)
    state["messages"].append(tool_call_message("ask_user", {"reason": "login", "tried": "换过两个入口"}))
    assert runner.takeover_allowed(state, {"reason": "login", "tried": "换过两个入口"}) == ""


def test_takeover_is_allowed_when_the_site_has_no_saved_account(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_module.keyring, "get_password", lambda service, key: None)
    runner = make_runner(tmp_path, monkeypatch, saved_site=None)
    assert (
        runner.takeover_allowed(
            takeover_state(False), {"reason": "verification", "tried": "重新观察了两次"}
        )
        == ""
    )


def test_first_takeover_must_say_what_was_already_tried(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_module.keyring, "get_password", lambda service, key: None)
    runner = make_runner(tmp_path, monkeypatch, saved_site=None)
    blocked = runner.takeover_allowed(takeover_state(False), {"reason": "login"})
    assert "tried" in blocked


def test_takeover_is_not_blocked_twice(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_module.keyring, "get_password", lambda service, key: None)
    runner = make_runner(tmp_path, monkeypatch, saved_site=None)
    state = takeover_state(False)
    state["messages"].append(tool_call_message("ask_user", {"reason": "login"}))
    assert runner.takeover_allowed(state, {"reason": "login"}) == ""


def test_clarification_is_never_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(vault_module.keyring, "get_password", lambda service, key: None)
    runner = make_runner(tmp_path, monkeypatch, saved_site="target.example")
    assert runner.takeover_allowed(takeover_state(False), {"reason": "clarification"}) == ""


def test_credential_login_can_never_be_remembered():
    assert "vault_login" not in GRANTABLE_TOOLS
    tool = next(t for t in BROWSER_TOOLS if t.name == "vault_login")
    assert tool.confirmation is True
    assert "submit_ref" in tool.schema.model_fields
