"""代理的 OAuth 上游支持（MATRIX 4.7 的另一半）的契约测试。

Hermes 的 proxy 是"到 OAuth provider"：上游不要 API key，要用户授权拿到的令牌。
本机没有网络、也没有可用的 OAuth 客户端凭据，所以这里**只钉能在本机证明的部分**：

1. **取令牌**：从 keyring 读（`TokenStore`），没有就报可读的错；
2. **判过期**：纯本地纯函数，带 60 秒提前量，边界可测；
3. **刷新**：默认实现需要出网 → 如实抛 `NetworkUnavailable`；注入假刷新就能跑通；
4. **选头**：`auth_mode` 决定发 `x-api-key` / `x-goog-api-key` 还是
   `Authorization: Bearer <令牌>`，并且真的进了上游请求。

**没钉的（也是做不到的）**：OAuth 令牌能不能被 Anthropic / Gemini 上游接受。
那需要一次真实请求，本机发不出去——`describe()` 里如实写着"代码在、没跑通"。
"""

from __future__ import annotations

import pytest

import app.mcp as mcp_module
import app.mcp_oauth_flow as flow
import app.proxy as px

NOW = 1_700_000_000.0

#: OAuth 上游的配置：既要有令牌存储的坐标（server / data_dir），也要有刷新所需的端点。
def _config(tmp_path, **overrides) -> dict:
    base = {
        "server": "demo",
        "data_dir": str(tmp_path / "data"),
        "url": "https://mcp.example.com/mcp",
        "client_id": "client-abc",
        "token_endpoint": "https://mcp.example.com/token",
    }
    base.update(overrides)
    return base


class FakeKeyring:
    """内存版 keyring：生产实现走系统钥匙串，测试绝不能往里写东西。"""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, key: str) -> str | None:
        return self.store.get((service, key))

    def set_password(self, service: str, key: str, value: str) -> None:
        self.store[(service, key)] = value

    def delete_password(self, service: str, key: str) -> None:
        self.store.pop((service, key), None)


@pytest.fixture
def fake_keyring(monkeypatch):
    fake = FakeKeyring()
    monkeypatch.setattr(flow, "keyring", fake)
    monkeypatch.setattr(mcp_module, "keyring", fake)
    return fake


def _store(tmp_path) -> flow.TokenStore:
    return flow.TokenStore(tmp_path / "data", "demo")


def _token(*, access: str = "tok-a", expires_at: float = NOW + 3600, refresh: str = "") -> flow.TokenPayload:
    return flow.TokenPayload(
        access_token=access,
        token_type="Bearer",
        expires_in=None,
        refresh_token=refresh,
        scope="read",
        obtained_at=NOW,
        expires_at=expires_at,
    )


# ── 1. 取令牌（纯本地）──────────────────────────────────────────────────────


def test_provider_reads_the_token_from_keyring(fake_keyring, tmp_path):
    store = _store(tmp_path)
    store.save(_token(access="tok-local"))
    provider = px.oauth_token_provider(_config(tmp_path), store=store, clock=lambda: NOW)
    assert provider() == "tok-local"


def test_missing_token_is_a_readable_error(fake_keyring, tmp_path):
    provider = px.oauth_token_provider(_config(tmp_path), store=_store(tmp_path), clock=lambda: NOW)
    with pytest.raises(px.OAuthTokenError) as excinfo:
        provider()
    message = str(excinfo.value)
    # 报错要说清"去哪儿登录"，而不是一句"令牌无效"。
    assert "mcp login" in message
    assert "keyring" in message


def test_store_coordinates_are_required_and_named(fake_keyring, tmp_path):
    """缺 server / data_dir 时点名缺哪个——猜一个目录会读成"用户没登录"。"""
    with pytest.raises(px.OAuthTokenError) as excinfo:
        px.oauth_token_provider({"data_dir": str(tmp_path / "data")})
    assert "server" in str(excinfo.value)
    with pytest.raises(px.OAuthTokenError) as excinfo:
        px.oauth_token_provider({"server": "demo"})
    assert "data_dir" in str(excinfo.value)


def test_store_is_derived_from_config_when_not_injected(fake_keyring, tmp_path):
    """不注入 store 时，服务名必须是 `orbit:<data_dir>:mcp:<server>`（与写端同源）。"""
    store = _store(tmp_path)
    store.save(_token(access="tok-derived"))
    provider = px.oauth_token_provider(_config(tmp_path), clock=lambda: NOW)
    assert provider() == "tok-derived"
    assert store.service == f"orbit:{(tmp_path / 'data').resolve()}:mcp:demo"


# ── 2. 判过期（纯本地）+ 3. 刷新（需要网络）─────────────────────────────────


def test_expired_token_triggers_a_refresh_and_returns_the_new_one(fake_keyring, tmp_path):
    store = _store(tmp_path)
    store.save(_token(access="old", expires_at=NOW - 1, refresh="refresh-1"))
    calls: list[str] = []

    def refresh(token):
        calls.append(token.access_token)
        return _token(access="new", expires_at=NOW + 3600, refresh="refresh-2")

    provider = px.oauth_token_provider(
        _config(tmp_path), store=store, refresh=refresh, clock=lambda: NOW
    )
    assert provider() == "new"
    assert calls == ["old"], "过期时必须拿旧令牌去换，而不是直接报错"


def test_token_expiring_within_the_skew_is_treated_as_expired(fake_keyring, tmp_path):
    """60 秒提前量：还剩 30 秒的令牌必须在**这次**请求就刷新，而不是等它 401。"""
    store = _store(tmp_path)
    store.save(_token(access="soon", expires_at=NOW + 30, refresh="r"))
    refreshed: list[float] = []

    def refresh(token):
        refreshed.append(NOW)
        return _token(access="fresh", expires_at=NOW + 3600)

    provider = px.oauth_token_provider(
        _config(tmp_path), store=store, refresh=refresh, clock=lambda: NOW
    )
    assert provider() == "fresh"
    assert refreshed, "还剩 30 秒 < 60 秒提前量，必须刷新"


def test_valid_token_is_not_refreshed(fake_keyring, tmp_path):
    store = _store(tmp_path)
    store.save(_token(access="fine", expires_at=NOW + 3600))

    def refresh(token):  # pragma: no cover - 不该被调用
        raise AssertionError("没过期的令牌不该触发刷新")

    provider = px.oauth_token_provider(
        _config(tmp_path), store=store, refresh=refresh, clock=lambda: NOW
    )
    assert provider() == "fine"


def test_expired_token_without_network_raises_network_unavailable(fake_keyring, tmp_path):
    """默认刷新实现 = 那一次 POST：本机无网络，如实抛，不静默用旧令牌。"""
    store = _store(tmp_path)
    store.save(_token(access="old", expires_at=NOW - 1, refresh="refresh-1"))
    provider = px.oauth_token_provider(_config(tmp_path), store=store, clock=lambda: NOW)
    with pytest.raises(flow.NetworkUnavailable) as excinfo:
        provider()
    assert "token" in str(excinfo.value).lower()


def test_refresh_without_a_refresh_token_says_so(fake_keyring, tmp_path):
    store = _store(tmp_path)
    store.save(_token(access="old", expires_at=NOW - 1, refresh=""))
    provider = px.oauth_token_provider(_config(tmp_path), store=store, clock=lambda: NOW)
    with pytest.raises(ValueError, match="refresh token"):
        provider()


# ── 4. auth_mode 选头 ──────────────────────────────────────────────────────


def test_auth_mode_picks_the_right_header():
    api_plan = px.plan_for("anthropic")
    assert px.auth_headers(api_plan, "sk-1") == {
        "x-api-key": "sk-1",
        "anthropic-version": "2023-06-01",
    }
    assert px.auth_headers(px.plan_for("gemini"), "sk-2") == {"x-goog-api-key": "sk-2"}

    oauth_anthropic = px.plan_for("anthropic", auth_mode=px.OAUTH)
    assert px.auth_headers(oauth_anthropic, "sk-1", token="tok") == {
        "authorization": "Bearer tok",
        "anthropic-version": "2023-06-01",
    }
    oauth_gemini = px.plan_for("gemini", auth_mode=px.OAUTH)
    assert px.auth_headers(oauth_gemini, "sk-1", token="tok") == {"authorization": "Bearer tok"}


def test_unknown_auth_mode_is_refused():
    with pytest.raises(px.ProxyError, match="auth_mode"):
        px.ProxyPlan(upstream_provider="anthropic", auth_mode="magic")


def test_plan_carries_auth_mode_into_dict_and_text():
    plan = px.plan_for("anthropic", auth_mode=px.OAUTH)
    assert plan.to_dict()["auth_mode"] == px.OAUTH
    assert "OAuth" in plan.describe()
    assert "API key" in px.plan_for("anthropic").describe()


# ── 5. 真的进了请求 ────────────────────────────────────────────────────────


def _request() -> dict:
    return {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 16,
    }


def _anthropic_reply() -> dict:
    return {
        "id": "msg_1",
        "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def test_handle_injects_the_oauth_header_instead_of_an_api_key():
    seen: dict[str, dict] = {}

    def forward(url, headers, body):
        seen["headers"] = dict(headers)
        return _anthropic_reply()

    server = px.ProxyServer(
        px.plan_for("anthropic", auth_mode=px.OAUTH),
        forward=forward,
        token_provider=lambda: "tok-live",
    )
    result = server.handle(_request())
    assert result["choices"][0]["message"]["content"] == "ok"
    assert seen["headers"]["authorization"] == "Bearer tok-live"
    assert "x-api-key" not in seen["headers"], "OAuth 模式下不该同时带 API key 头"


def test_oauth_mode_without_a_token_provider_says_what_is_missing():
    server = px.ProxyServer(px.plan_for("anthropic", auth_mode=px.OAUTH), forward=lambda *a: {})
    result = server.handle(_request())
    assert result["error"]["code"] == "proxy_error"
    assert "token_provider" in result["error"]["message"]


def test_token_failure_becomes_a_readable_error_body():
    """令牌取不到/刷不了不能变成 500 栈，也不能被当成"上游挂了"去退避重试。"""

    def provider():
        raise flow.NetworkUnavailable("刷新令牌需要 POST 到 https://…/token，本机无网络")

    server = px.ProxyServer(
        px.plan_for("anthropic", auth_mode=px.OAUTH),
        forward=lambda *a: _anthropic_reply(),
        token_provider=provider,
    )
    result = server.handle(_request())
    assert result["error"]["code"] == "oauth_needs_network"
    assert "无网络" in result["error"]["message"]


def test_describe_marks_oauth_as_not_end_to_end_verified():
    text = px.ProxyServer(
        px.plan_for("anthropic", auth_mode=px.OAUTH), token_provider=lambda: "t"
    ).describe()
    # 必须出现"没跑通"这类字样，否则读者会把它当成已验证能力。
    assert "没跑通" in text
    assert "keyring" in text
    assert px.OAUTH in text


def test_module_describe_mentions_both_auth_modes():
    text = px.describe()
    assert px.API_KEY in text and px.OAUTH in text
    assert "Bearer" in text
