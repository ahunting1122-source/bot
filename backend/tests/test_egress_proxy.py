"""`app/egress_proxy.py` 的测试（`4.8` 的最小实现）。

## 核心断言是「上游零连接」

只断言「返回了 403」不够 —— **403 可能是先转发出去、拿到响应再改成 403。**
那样数据已经离开本机了。所以每条拒绝用例都同时断言 **echo server 的连接计数为 0**。

## 第二个核心断言是「占位符没有到达上游」

`inject_headers` 有规则但取不到密钥时，**原样转发等于把占位符当凭证发出去** ——
上游回 401，而调用方会以为是自己配置错了。**这是最坏的一种失败。**
"""

from __future__ import annotations

import http.server
import threading

import pytest

from app.egress_proxy import EgressProxy, EgressProxyRefusal, serve

REAL_KEY = "sk-real-0123456789"
PLACEHOLDER = "orbit-placeholder-token"


class FakeDecision:
    def __init__(self, allowed: bool, reason: str = "") -> None:
        self.allowed = allowed
        self.reason = reason


class FakePolicy:
    """够用的策略替身：`check` 只看 host 在不在 `denied` 里；
    `inject_headers` 按 `inject` 表决定注入什么。

    **用替身是为了单独测「代理自己那部分逻辑」**（闸门顺序、不降级、零连接）。
    真实策略的集成是另一组测试的事 —— 混在一起会分不清是代理错了还是策略错了。
    """

    def __init__(self, *, denied: tuple[str, ...] = (), inject: dict[str, str] | None = None,
                 missing_secret: bool = False) -> None:
        self.denied = denied
        self.inject = inject or {}
        self.missing_secret = missing_secret

    def check(self, urls: list[str]) -> FakeDecision:
        for url in urls:
            if any(host in url for host in self.denied):
                return FakeDecision(False, f"host 在拒绝名单里：{url}")
        return FakeDecision(True)

    def inject_headers(self, url: str, headers: dict[str, str] | None = None) -> dict[str, str]:
        out = dict(headers or {})
        for host, header_value in self.inject.items():
            if host in url:
                if self.missing_secret:
                    # 规则匹配但密钥缺失：**保留占位符**（模拟真实策略的保守行为）
                    continue
                out["Authorization"] = header_value
        return out


class Echo:
    """本地 echo server：记下收到的头与连接次数。"""

    def __init__(self) -> None:
        self.headers_seen: list[dict[str, str]] = []
        self.connections = 0
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.connections += 1
                outer.headers_seen.append({k: v for k, v in self.headers.items()})
                body = b"ok"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                return

        self.server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def echo():
    server = Echo()
    try:
        yield server
    finally:
        server.close()


@pytest.fixture()
def proxy_server():
    servers = []

    def start(proxy: EgressProxy):
        server = serve(proxy)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


def test_the_real_key_reaches_the_upstream_not_the_placeholder(echo, proxy_server):
    """**真 key 到达上游** —— 而且不是占位符。这是整个功能的意义所在。"""
    policy = FakePolicy(inject={"127.0.0.1": f"Bearer {REAL_KEY}"})
    server = proxy_server(EgressProxy(policy))

    import httpx

    response = httpx.get(
        f"{echo.base}/x", proxy=f"http://127.0.0.1:{server.server_port}",
        headers={"Authorization": f"Bearer {PLACEHOLDER}"},
        timeout=10,
    )
    assert response.status_code == 200
    assert echo.connections == 1
    seen = echo.headers_seen[0].get("Authorization")
    assert seen == f"Bearer {REAL_KEY}", f"上游收到的是 {seen!r}"
    assert PLACEHOLDER not in (seen or "")


def test_a_denied_host_is_refused_with_zero_upstream_connections(echo, proxy_server):
    """**被拒的请求必须在打开 socket 之前就拒** —— 所以断言连接数为 0。"""
    policy = FakePolicy(denied=("127.0.0.1",))
    server = proxy_server(EgressProxy(policy))

    import httpx

    response = httpx.get(
        f"{echo.base}/x", proxy=f"http://127.0.0.1:{server.server_port}", timeout=10
    )
    assert response.status_code == 403
    assert "拒绝名单" in response.text
    assert echo.connections == 0, "**被拒的请求仍然连上了上游**"


def test_cloud_metadata_is_refused_with_zero_upstream_connections(proxy_server):
    """云元数据端点在任何字节发出之前被拒。"""
    policy = FakePolicy()
    server = proxy_server(EgressProxy(policy))

    import httpx

    response = httpx.get(
        "http://169.254.169.254/latest/meta-data", proxy=f"http://127.0.0.1:{server.server_port}",
        timeout=10,
    )
    assert response.status_code == 403
    assert "云元数据" in response.text


def test_a_missing_secret_refuses_instead_of_sending_the_placeholder(
    echo, proxy_server, monkeypatch
):
    """**规则匹配但密钥缺失时，绝不许把占位符发出去。**

    原样转发会让上游回 401，而调用方会以为是自己配错了 ——
    **把配置错误伪装成上游错误，是最坏的一种失败。**
    """
    # 进程侧占位符由环境变量给出 —— 代理靠它认出「这还是占位符」。
    monkeypatch.setenv("ORBIT_PROXY_TOKEN", PLACEHOLDER)
    policy = FakePolicy(inject={"127.0.0.1": f"Bearer {REAL_KEY}"}, missing_secret=True)
    server = proxy_server(EgressProxy(policy))

    import httpx

    response = httpx.get(
        f"{echo.base}/x", proxy=f"http://127.0.0.1:{server.server_port}",
        headers={"Authorization": f"Bearer {PLACEHOLDER}"},
        timeout=10,
    )
    assert response.status_code == 500
    assert "拒绝原样转发" in response.text
    assert echo.connections == 0, "**占位符被发出去了**"


def test_no_matching_rule_forwards_headers_unchanged(echo, proxy_server):
    """没有规则就没有要注入的 —— 头原样转发是允许的（成对断言：防「总是拦」）。"""
    policy = FakePolicy()
    server = proxy_server(EgressProxy(policy))

    import httpx

    response = httpx.get(
        f"{echo.base}/x", proxy=f"http://127.0.0.1:{server.server_port}",
        headers={"X-Custom": "keep-me"},
        timeout=10,
    )
    assert response.status_code == 200
    assert echo.headers_seen[0].get("X-Custom") == "keep-me"


def test_guard_raises_before_any_network_call():
    """`guard()` 是纯函数式的闸门：不过就抛，**不碰网络**。"""
    proxy = EgressProxy(FakePolicy(denied=("blocked.example",)))
    with pytest.raises(EgressProxyRefusal) as excinfo:
        proxy.guard("https://blocked.example/x")
    assert excinfo.value.status == 403
