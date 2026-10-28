"""本地 OAuth 回调服务器（MATRIX 8.3）的契约测试。

这一组用例存在的理由只有一句：**除了"拿 code 换 token"那一次 POST，整条流程都是真的**。
所以这里真的起了一个回环 HTTP 服务器、真的发了一次 HTTP 请求、真的校验了 state、真的
把令牌写进了（内存版）keyring。唯一的假东西是：

* `exchange=`（那一次需要出网的 POST）——注入一个假实现；
* `opener=`（"谁来把浏览器送到授权 URL"）——注入一个"假浏览器"，它对回环地址发请求，
  于是测试不会弹窗、也不出网（127.0.0.1 是回环，不是网络）。

安全面单独钉三条：错 state 被拒、state 一次性、超时必关服务器。
"""

from __future__ import annotations

import http.client
import socket
from urllib.parse import parse_qs, urlparse

import pytest

import app.mcp as mcp_module
import app.mcp_oauth_flow as flow
import app.oauth_callback as callback

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
    """`TokenStore` 会双写（本模块 + `app.mcp.OAuthStore`），两处都要换掉。"""
    fake = FakeKeyring()
    monkeypatch.setattr(flow, "keyring", fake)
    monkeypatch.setattr(mcp_module, "keyring", fake)
    return fake


def _hit(redirect_uri: str, query: str = "") -> tuple[int, str]:
    """**假浏览器的一次请求**：真的 TCP 连到 127.0.0.1 的那个端口，发一个 GET。

    用 `http.client` 而不是 `urllib`：这里要读**状态码**，而 `urlopen` 把 4xx 变成异常。
    """
    parsed = urlparse(redirect_uri)
    conn = http.client.HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port, timeout=5)
    try:
        target = parsed.path + (f"?{query}" if query else "")
        conn.request("GET", target)
        response = conn.getresponse()
        return response.status, response.read().decode("utf-8")
    finally:
        conn.close()


def _drive(server: callback.CallbackServer, query: str) -> tuple[int, str]:
    """对**已经启动**的服务器打一次请求。"""
    return _hit(server.redirect_uri, query)


# ── 回调服务器本体 ──────────────────────────────────────────────────────────


def test_callback_server_receives_code_and_state():
    server = callback.CallbackServer(state="st-1")
    server.start()
    try:
        status, body = _drive(server, "code=abc123&state=st-1")
        assert status == 200
        assert "授权已完成" in body
        result = server.wait(5)
    finally:
        server.close()
    assert result.ok
    assert result.code == "abc123"
    assert result.state == "st-1"
    assert server.rejected == 0


def test_wrong_state_is_rejected_but_the_flow_keeps_waiting():
    """错 state 只回 400，**不终止流程**。

    终止的话，任何本机进程只要往这个端口打一次请求就能取消用户的授权——那等于把
    "取消"按钮发给所有人。
    """
    server = callback.CallbackServer(state="st-right")
    server.start()
    try:
        status, _ = _drive(server, "code=stolen&state=st-wrong")
        assert status == 400
        assert server.rejected == 1
        # 服务器还在服务：正确的那次回调随后照样能通过。
        status, _ = _drive(server, "code=good&state=st-right")
        assert status == 200
        result = server.wait(5)
    finally:
        server.close()
    assert result.code == "good"


def test_state_is_single_use():
    """同一个 state 第二次到达必须被拒（重放 / 浏览器把历史地址又开一次）。"""
    server = callback.CallbackServer(state="st-once")
    server.start()
    try:
        first_status, _ = _drive(server, "code=c1&state=st-once")
        replay_status, _ = _drive(server, "code=c2&state=st-once")
        result = server.wait(5)
    finally:
        server.close()
    assert first_status == 200
    assert replay_status == 400, "state 用过必须失效，否则它可以被重放"
    assert server.rejected == 1
    assert result.code == "c1", "重放不能覆盖已经收到的结果"


def test_missing_code_is_rejected():
    server = callback.CallbackServer(state="st-x")
    server.start()
    try:
        status, _ = _drive(server, "state=st-x")
        assert status == 400
        assert server.rejected == 1
    finally:
        server.close()


def test_timeout_closes_the_server():
    """超时必须**先关服务器再抛**：留一个还在监听的端口是最糟的收尾方式。"""
    server = callback.CallbackServer(state="st-t", timeout=0.2)
    server.start()
    port = server.port
    with pytest.raises(callback.CallbackTimeout):
        server.wait()
    # 端口确实关掉了（连不上 = 没有进程在听）。
    with pytest.raises(OSError):
        probe = socket.create_connection(("127.0.0.1", port), timeout=0.5)
        probe.close()


def test_non_loopback_hosts_are_refused():
    with pytest.raises(callback.CallbackError, match="回环"):
        callback.CallbackServer(host="0.0.0.0")
    with pytest.raises(callback.CallbackError, match="回环"):
        callback.CallbackServer(host="192.168.1.10")


def test_redirect_uri_requires_a_started_server():
    server = callback.CallbackServer()
    with pytest.raises(callback.CallbackError, match="还没启动"):
        _ = server.redirect_uri


def test_unknown_path_is_404():
    server = callback.CallbackServer(state="st-p", path="/callback")
    server.start()
    try:
        status, _ = _hit(f"http://127.0.0.1:{server.port}/other", "code=c&state=st-p")
        assert status == 404
    finally:
        server.close()


# ── 整条流程：除了那一次 POST，其它都是真的 ────────────────────────────────


def _fake_browser(url: str, *, code: str = "code-from-provider") -> None:
    """假浏览器：把 authorize URL 里的 state 取出来，直接对回调地址发一次请求。"""
    params = parse_qs(urlparse(url).query)
    redirect = params["redirect_uri"][0]
    state = params["state"][0]
    status, _ = _hit(redirect, f"code={code}&state={state}")
    assert status == 200


def test_authorize_flow_completes_with_an_injected_exchange(fake_keyring, tmp_path):
    """**这条就是"能不能标完成"的判据**：注入假 exchange 后整条流程跑通。"""
    seen: dict[str, object] = {}

    def exchange(config, code, code_verifier):
        seen["config"] = config
        seen["code"] = code
        seen["verifier"] = code_verifier
        return flow.TokenPayload(
            access_token="access-123",
            refresh_token="refresh-123",
            expires_in=3600.0,
            obtained_at=1_700_000_000.0,
            expires_at=1_700_003_600.0,
        )

    opened: list[str] = []
    store = flow.TokenStore(tmp_path / "data", "demo")
    result = callback.run_authorize_flow(
        CONFIG,
        opener=lambda url: (opened.append(url), _fake_browser(url))[1],
        exchange=exchange,
        store=store,
        timeout=10,
    )

    # 1) 授权 URL 是真的，而且带着**真实端口**的回调地址（不是占位值）。
    assert len(opened) == 1
    parsed = urlparse(opened[0])
    params = parse_qs(parsed.query)
    assert parsed.netloc == "mcp.example.com"
    assert params["code_challenge_method"] == ["S256"]
    assert params["state"] == [result.state]
    redirect = params["redirect_uri"][0]
    assert redirect.startswith("http://127.0.0.1:")
    assert redirect.endswith("/callback")
    assert params["resource"] == ["https://mcp.example.com/mcp"]

    # 2) 回调真的收到了 code，PKCE 的 verifier 真的交回给 exchange 了。
    assert seen["code"] == "code-from-provider"
    assert isinstance(seen["verifier"], str) and len(seen["verifier"]) >= 43

    # 3) 令牌真的写进了 keyring（fake_keyring 是内存版）。
    assert result.token is not None and result.token.access_token == "access-123"
    assert result.saved is True
    assert fake_keyring.get_password(store.service, flow.TOKEN_KEY)

    # 4) 每一步都留下痕迹，没有一步是"假装做过"。
    steps = {item["step"]: item["done"] for item in result.steps}
    assert steps["起本地回调服务器"] is True
    assert steps["打开 / 打印授权 URL"] is True
    assert steps["收到回调并校验 state"] is True
    assert steps["换 token"] is True
    assert steps["写入 keyring"] is True


def test_default_exchange_raises_network_unavailable():
    """默认 exchange 就是那一次 POST：本机无网络，如实抛。"""
    with pytest.raises(flow.NetworkUnavailable) as excinfo:
        callback.default_exchange(CONFIG, "code", "verifier" * 8)
    message = str(excinfo.value)
    assert "POST" in message
    assert "唯一需要出网" in message


def test_flow_with_default_exchange_gets_all_the_way_to_the_post(tmp_path):
    """用默认 exchange 时，流程必须**走到换 token 那一步**才抛。

    这条比"抛了 NetworkUnavailable"强得多：它证明服务器起了、回调收了、state 校验过了，
    卡点**恰好**是那一次 POST——也就是"除了那一次 POST，其它全是真的"。
    """
    with pytest.raises(flow.NetworkUnavailable):
        callback.run_authorize_flow(
            CONFIG,
            opener=_fake_browser,
            timeout=10,
        )


def test_provider_error_callback_raises_rejected():
    def opener(url: str) -> None:
        params = parse_qs(urlparse(url).query)
        _hit(params["redirect_uri"][0], f"error=access_denied&state={params['state'][0]}")

    with pytest.raises(callback.CallbackRejected) as excinfo:
        callback.run_authorize_flow(CONFIG, opener=opener, timeout=10)
    assert "access_denied" in str(excinfo.value)


def test_flow_reports_which_steps_were_not_done(tmp_path):
    """没传 store 时如实说"令牌没落库"，而不是默默丢掉。"""
    def exchange(config, code, verifier):
        return flow.TokenPayload(access_token="a")

    result = callback.run_authorize_flow(
        CONFIG,
        opener=_fake_browser,
        exchange=exchange,
        timeout=10,
    )
    assert result.saved is False
    steps = {item["step"]: item for item in result.steps}
    assert steps["写入 keyring"]["done"] is False
    assert "没有传 store" in steps["写入 keyring"]["detail"]


def test_describe_states_the_single_missing_piece():
    text = callback.describe()
    assert "回环" in text and "state" in text
    # 唯一做不到的那一步必须点名，否则读者会以为整条链路都跑通了。
    assert "POST" in text and "NetworkUnavailable" in text
