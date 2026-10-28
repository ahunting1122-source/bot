"""MCP OAuth 纯函数层（矩阵 8.3）的契约测试。

这里断言的是**形状**（PKCE 的 RFC 7636 形状、authorize URL 的参数集合、请求体字段、
令牌响应解析、过期边界）与**边界**（哪些步骤真做了、哪些如实抛）。

不联网、不碰真实系统 keyring：所有 keyring 交互都打到一个内存后端上。
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest

import app.mcp as mcp_module
import app.mcp_oauth_flow as flow
from app.config import Settings

NOW = 1_700_000_000.0

CONFIG = {
    "name": "demo",
    "url": "https://mcp.example.com/mcp",
    "client_id": "client-abc",
    "scope": "read write",
}


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
    """把两处 keyring 都换成内存后端（``TokenStore`` 双写，两边都会碰）。"""
    fake = FakeKeyring()
    monkeypatch.setattr(flow, "keyring", fake)
    monkeypatch.setattr(mcp_module, "keyring", fake)
    return fake


def _settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


# ── PKCE（RFC 7636）─────────────────────────────────────────────────────────


def test_pkce_verifier_shape():
    verifier = flow.new_code_verifier()
    assert 43 <= len(verifier) <= flow.PKCE_MAX_LENGTH
    # RFC 7636 §4.1 的字符集：unreserved = ALPHA / DIGIT / "-" / "." / "_" / "~"
    assert all(char.isalnum() or char in "-._~" for char in verifier)
    assert verifier != flow.new_code_verifier(), "verifier 必须是随机的"


def test_code_challenge_is_base64url_sha256_without_padding():
    verifier = flow.new_code_verifier()
    challenge = flow.code_challenge_for(verifier)
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    assert challenge == expected.decode("ascii").rstrip("=")
    assert "=" not in challenge
    assert "+" not in challenge and "/" not in challenge
    # S256 的 challenge 恒为 43 个字符（32 字节 → 43 个 base64 字符）。
    assert len(challenge) == 43


def test_pkce_pair_is_consistent():
    verifier, challenge = flow.pkce_pair()
    assert flow.code_challenge_for(verifier) == challenge


def test_state_is_random_and_url_safe():
    state = flow.new_state()
    assert len(state) >= 32
    assert "=" not in state


# ── authorize URL ───────────────────────────────────────────────────────────


def test_build_authorize_url_carries_every_required_parameter():
    verifier, challenge = flow.pkce_pair()
    url = flow.build_authorize_url(CONFIG, state="st-1", code_challenge=challenge)
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    # 端点从服务 origin 推出来（mcp SDK 的兜底规则：去掉 path 拼 /authorize）。
    assert parsed.netloc == "mcp.example.com"
    assert parsed.path == "/authorize"
    assert params["response_type"] == ["code"]
    assert params["client_id"] == ["client-abc"]
    assert params["state"] == ["st-1"]
    assert params["code_challenge"] == [challenge]
    assert params["code_challenge_method"] == ["S256"]
    assert params["redirect_uri"] == [flow.DEFAULT_REDIRECT_URI]
    # RFC 8707 的 resource：服务 URL 的规范化形式。
    assert params["resource"] == ["https://mcp.example.com/mcp"]
    assert params["scope"] == ["read write"]
    assert "prompt" not in params


def test_offline_access_adds_prompt_consent():
    config = {**CONFIG, "scope": "read offline_access"}
    url = flow.build_authorize_url(config, state="s", code_challenge="c" * 43)
    assert parse_qs(urlparse(url).query)["prompt"] == ["consent"]


def test_explicit_endpoints_win_over_derived_ones():
    config = {**CONFIG, "authorization_endpoint": "https://auth.example.com/oauth2/authorize"}
    url = flow.build_authorize_url(config, state="s", code_challenge="c" * 43)
    assert url.startswith("https://auth.example.com/oauth2/authorize?")


def test_resource_param_can_be_disabled():
    config = {**CONFIG, "include_resource": False}
    url = flow.build_authorize_url(config, state="s", code_challenge="c" * 43)
    assert "resource=" not in url


def test_build_authorize_url_requires_client_id():
    with pytest.raises(ValueError, match="client_id"):
        flow.build_authorize_url({"url": CONFIG["url"]}, state="s", code_challenge="c" * 43)


def test_build_authorize_url_requires_state_and_challenge():
    with pytest.raises(ValueError, match="state"):
        flow.build_authorize_url(CONFIG, state="", code_challenge="c" * 43)
    with pytest.raises(ValueError, match="code_challenge"):
        flow.build_authorize_url(CONFIG, state="s", code_challenge="")


# ── 请求体 ──────────────────────────────────────────────────────────────────


def test_exchange_payload_matches_rfc6749_and_pkce():
    payload = flow.exchange_payload(CONFIG, "the-code", "the-verifier")
    assert payload == {
        "grant_type": "authorization_code",
        "code": "the-code",
        "redirect_uri": flow.DEFAULT_REDIRECT_URI,
        "client_id": "client-abc",
        "code_verifier": "the-verifier",
        "resource": "https://mcp.example.com/mcp",
    }
    # 公共客户端不该带空 secret：那会被部分服务判成非法凭证。
    assert "client_secret" not in payload


def test_exchange_payload_rejects_empty_inputs():
    with pytest.raises(ValueError, match="授权码"):
        flow.exchange_payload(CONFIG, "", "v")
    with pytest.raises(ValueError, match="code_verifier"):
        flow.exchange_payload(CONFIG, "c", "")


def test_refresh_payload_matches_rfc6749():
    payload = flow.refresh_payload(CONFIG, "refresh-1")
    assert payload == {
        "grant_type": "refresh_token",
        "refresh_token": "refresh-1",
        "client_id": "client-abc",
        "resource": "https://mcp.example.com/mcp",
    }


def test_refresh_payload_requires_refresh_token():
    with pytest.raises(ValueError, match="refresh token"):
        flow.refresh_payload(CONFIG, "")


# ── 令牌响应解析 ────────────────────────────────────────────────────────────


def test_parse_token_response_computes_absolute_expiry():
    token = flow.parse_token_response(
        {"access_token": "at", "token_type": "bearer", "expires_in": 3600, "refresh_token": "rt"},
        now=NOW,
    )
    assert token.access_token == "at"
    assert token.token_type == "Bearer"
    assert token.expires_in == 3600
    assert token.obtained_at == NOW
    assert token.expires_at == NOW + 3600


def test_parse_token_response_tolerates_string_expires_in():
    """RFC 说 expires_in 是数字，现实里常被当字符串返回。"""
    token = flow.parse_token_response({"access_token": "at", "expires_in": "60"}, now=NOW)
    assert token.expires_in == 60
    assert token.expires_at == NOW + 60


def test_parse_token_response_without_expiry_is_unknown_not_zero():
    token = flow.parse_token_response({"access_token": "at"}, now=NOW)
    assert token.expires_at == 0.0
    assert flow.is_expired(token, now=NOW + 10**9) is False


def test_parse_token_response_surfaces_oauth_errors():
    with pytest.raises(ValueError, match="invalid_grant"):
        flow.parse_token_response({"error": "invalid_grant", "error_description": "code expired"})


def test_parse_token_response_requires_access_token():
    with pytest.raises(ValueError, match="access_token"):
        flow.parse_token_response({"token_type": "Bearer"})
    with pytest.raises(ValueError, match="不是对象"):
        flow.parse_token_response(["nope"])


def test_token_payload_round_trips_through_json():
    token = flow.parse_token_response({"access_token": "at", "expires_in": 60}, now=NOW)
    assert flow.TokenPayload.from_dict(json.loads(json.dumps(token.as_dict()))) == token
    assert flow.TokenPayload.from_dict({"token_type": "Bearer"}) is None
    assert flow.TokenPayload.from_dict("nope") is None


# ── 过期判定边界 ────────────────────────────────────────────────────────────


def test_is_expired_boundaries():
    token = flow.TokenPayload(access_token="at", expires_at=NOW + 100)
    assert flow.is_expired(token, skew=0, now=NOW) is False
    assert flow.is_expired(token, skew=0, now=NOW + 100) is True  # 正好到期算过期
    assert flow.is_expired(token, skew=60, now=NOW + 40) is True  # 提前量内算即将过期
    assert flow.is_expired(token, skew=60, now=NOW + 39) is False


def test_is_expired_uses_obtained_at_plus_expires_in():
    token = flow.TokenPayload(access_token="at", expires_in=30, obtained_at=NOW)
    assert flow.is_expired(token, skew=0, now=NOW + 10) is False
    assert flow.is_expired(token, skew=0, now=NOW + 30) is True


def test_is_expired_accepts_dicts_and_unknown_expiry():
    assert flow.is_expired({"expires_at": NOW + 1000}, now=NOW) is False
    assert flow.is_expired({"expires_at": NOW - 10}, now=NOW) is True
    # 到期时间未知 → 不判过期（把"不知道"当"已过期"会让长效令牌每轮都被刷新）。
    assert flow.is_expired({}, now=NOW) is False


def test_is_expired_accepts_token_record_from_app_mcp():
    record = mcp_module.TokenRecord("demo", "at", expires_at=NOW - 1)
    assert flow.is_expired(record, now=NOW) is True


# ── 令牌进 keyring，不进文件 ────────────────────────────────────────────────


def test_token_is_stored_in_keyring_not_on_disk(tmp_path, fake_keyring):
    settings = _settings(tmp_path)
    store = flow.TokenStore.for_settings(settings, "demo")
    token = flow.parse_token_response(
        {"access_token": "super-secret-at", "expires_in": 3600, "refresh_token": "super-secret-rt"},
        now=NOW,
    )
    store.save(token)
    assert store.service == f"orbit:{store.data_dir}:mcp:demo"
    assert fake_keyring.get_password(store.service, flow.TOKEN_KEY) is not None
    assert store.load() is not None
    # data_dir 下不许有任何文件包含明文令牌。
    leaked = []
    for path in settings.data_dir.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="ignore")
            if "super-secret-at" in text or "super-secret-rt" in text:
                leaked.append(path)
    assert leaked == []


def test_store_status_reports_expiry(tmp_path, fake_keyring):
    store = flow.TokenStore.for_settings(_settings(tmp_path), "demo")
    store.save(flow.TokenPayload(access_token="at", expires_at=NOW + 3600, obtained_at=NOW))
    assert store.status(now=NOW)["state"] == "valid"
    assert store.status(now=NOW + 3590)["state"] == "expiring"
    assert store.status(now=NOW + 3601)["state"] == "expired"


def test_store_clear_removes_both_keys(tmp_path, fake_keyring):
    store = flow.TokenStore.for_settings(_settings(tmp_path), "demo")
    store.save(flow.TokenPayload(access_token="at", expires_in=60, obtained_at=NOW))
    assert store.clear() is True
    assert store.load() is None
    assert store.status()["state"] == "missing"
    assert store.clear() is False


def test_store_reads_legacy_oauth_key(tmp_path, fake_keyring):
    """``mcp auth set`` 写的是 ``OAuthStore`` 的键：新读端必须能看见它。"""
    legacy = mcp_module.OAuthStore(f"orbit:{flow.TokenStore.for_settings(_settings(tmp_path), 'demo').data_dir}")
    legacy.set("demo", "legacy-at", expires_in=600, now=NOW)
    store = flow.TokenStore.for_settings(_settings(tmp_path), "demo")
    token = store.load()
    assert token is not None and token.access_token == "legacy-at"
    assert store.status(now=NOW + 10)["state"] == "valid"


# ── 如实抛：需要网络/浏览器的那几步 ────────────────────────────────────────


def test_run_flow_raises_network_unavailable_and_lists_what_is_missing():
    with pytest.raises(flow.NetworkUnavailable) as excinfo:
        flow.run_flow(CONFIG, server="demo")
    message = str(excinfo.value)
    for requirement in flow.FLOW_REQUIREMENTS:
        assert requirement.split("（")[0][:12] in message
    assert "RFC 7636" not in message  # 不是"什么都没做"的报错
    assert "https://mcp.example.com" in message


def test_refresh_token_without_transport_raises_network_unavailable():
    token = flow.TokenPayload(access_token="at", refresh_token="rt", expires_at=NOW - 1)
    with pytest.raises(flow.NetworkUnavailable, match="本机无网络"):
        flow.refresh_token(CONFIG, token)


def test_refresh_token_with_injected_transport_round_trips(tmp_path, fake_keyring):
    """注入一个假 transport 就能验证"拼请求体 → 解析响应 → 存回 keyring"这条链。"""
    store = flow.TokenStore.for_settings(_settings(tmp_path), "demo")
    old = flow.TokenPayload(
        access_token="old-at", refresh_token="old-rt", expires_at=NOW - 10, obtained_at=NOW - 100
    )
    store.save(old)
    calls = []

    def transport(url, payload):
        calls.append((url, payload))
        # 刷新响应**不带**新的 refresh_token：必须继续用旧的（RFC 6749 §6）。
        return {"access_token": "new-at", "expires_in": 3600}

    fresh = flow.refresh_token(CONFIG, old, transport=transport, store=store)
    assert fresh.access_token == "new-at"
    assert fresh.refresh_token == "old-rt"
    assert calls[0][0] == "https://mcp.example.com/token"
    assert calls[0][1]["grant_type"] == "refresh_token"
    assert store.load().access_token == "new-at"


# ── login_report：不需要网络的那三件事 ────────────────────────────────────


def test_login_report_gives_authorize_url_without_network(tmp_path, fake_keyring):
    entry = {"name": "demo", "url": CONFIG["url"], "client_id": "client-abc"}
    report = flow.login_report(_settings(tmp_path), entry, now=NOW)
    assert report["state"] == "missing"
    assert report["authorize_url"].startswith("https://mcp.example.com/authorize?")
    assert report["code_challenge_method"] == "S256"
    assert len(report["code_challenge"]) == 43
    # verifier 是秘密：绝不能出现在报告里（报告会进 --json 输出）。
    assert "code_verifier" not in report
    assert [step["step"] for step in report["steps"]][0] == "读取已有令牌"
    assert report["missing"] == list(flow.FLOW_REQUIREMENTS)


def test_login_report_explains_missing_client_id(tmp_path, fake_keyring):
    report = flow.login_report(_settings(tmp_path), {"name": "demo", "url": CONFIG["url"]}, now=NOW)
    assert report["authorize_url"] == ""
    assert "client_id" in report["authorize_url_error"]


def test_login_report_sees_an_expired_token(tmp_path, fake_keyring):
    settings = _settings(tmp_path)
    store = flow.TokenStore.for_settings(settings, "demo")
    store.save(flow.TokenPayload(access_token="at", refresh_token="rt", expires_at=NOW - 5))
    report = flow.login_report(
        settings, {"name": "demo", "url": CONFIG["url"], "client_id": "c"}, now=NOW
    )
    assert report["state"] == "expired"
    assert report["has_refresh_token"] is True


# ── app/mcp.py 的接线 ───────────────────────────────────────────────────────


def test_app_mcp_oauth_refresh_reports_valid_token(tmp_path, fake_keyring):
    settings = _settings(tmp_path)
    store = flow.TokenStore.for_settings(settings, "demo")
    store.save(flow.TokenPayload(access_token="at", expires_at=NOW + 3600, obtained_at=NOW))
    result = mcp_module.oauth_refresh(
        settings, {"name": "demo", "url": CONFIG["url"]}, now=NOW
    )
    assert result == {
        "ok": True,
        "server": "demo",
        "reason": "valid",
        "refreshed": False,
        "expires_at": NOW + 3600,
    }


def test_app_mcp_oauth_refresh_reports_missing_and_no_refresh_token(tmp_path, fake_keyring):
    settings = _settings(tmp_path)
    entry = {"name": "demo", "url": CONFIG["url"]}
    assert mcp_module.oauth_refresh(settings, entry, now=NOW)["reason"] == "missing"
    flow.TokenStore.for_settings(settings, "demo").save(
        flow.TokenPayload(access_token="at", expires_at=NOW - 1)
    )
    assert mcp_module.oauth_refresh(settings, entry, now=NOW)["reason"] == "no_refresh_token"


# ── CLI：mcp login / mcp reauth ────────────────────────────────────────────


def _run_cli(tmp_path, capsys, *argv):
    """跑一次真 CLI（先导入 mcp_extra：生产接线里由 commands/__init__.py 负责）。"""
    import app.cli.commands.mcp_extra  # noqa: F401 - 导入即接线
    from app.cli.main import main

    code = main(["--data-dir", str(tmp_path / "data"), *argv])
    return code, capsys.readouterr()


def _write_server(tmp_path, **extra) -> None:
    """写一条服务配置。走 ``write_servers``：``auth`` 在 ``save_servers`` 白名单之外。"""
    from app.mcp import MCPManager, write_servers

    manager = MCPManager(_settings(tmp_path))
    write_servers(manager, [{"name": "demo", "url": "https://mcp.example.com/mcp", **extra}])


def test_cli_login_prints_status_and_authorize_url_then_exits_3(tmp_path, capsys, fake_keyring):
    _write_server(tmp_path)
    code, captured = _run_cli(tmp_path, capsys, "mcp", "login", "demo")
    assert code == 3
    assert "OAuth token" in captured.out
    assert "读取已有令牌" in captured.out
    # 真流程做不到的那一步，必须把缺什么打出来，而不是一句"未实现"。
    assert "回调服务器" in captured.err


def test_cli_login_unknown_server_exits_1(tmp_path, capsys, fake_keyring):
    code, captured = _run_cli(tmp_path, capsys, "mcp", "login", "nope")
    assert code == 1
    assert "没有名为" in captured.err


def test_cli_reauth_without_oauth_servers_exits_1(tmp_path, capsys, fake_keyring):
    _write_server(tmp_path)
    code, captured = _run_cli(tmp_path, capsys, "mcp", "reauth", "--all")
    assert code == 1
    assert "auth=oauth" in captured.err


def test_cli_reauth_reports_valid_token(tmp_path, capsys, fake_keyring):
    _write_server(tmp_path, auth="oauth")
    store = flow.TokenStore.for_settings(_settings(tmp_path), "demo")
    # CLI 用的是真实时钟（`reauth` 没有 `--now`），所以这里给一个远期到期时刻。
    store.save(flow.TokenPayload(access_token="at", expires_at=time.time() + 10**6))
    code, captured = _run_cli(tmp_path, capsys, "mcp", "reauth")
    assert code == 0
    assert "仍然有效" in captured.out
