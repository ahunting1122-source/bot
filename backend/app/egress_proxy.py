"""出口凭证注入代理（`4.8` iron-proxy 的最小实现）。

## 它做什么

把 `EgressPolicy` 应用到真实出站流量上。**它不重新实现任何判断逻辑** ——
四步全部调用 `app/egress.py` 里已经建好、已经有测试的函数：

1. `refuse_cloud_metadata(url)` —— 云元数据硬拦（与其余出站点同一条规则）；
2. `policy.check([url])` —— 不许就 403，`reason` 原样回给调用方；
3. `policy.inject_headers(url, headers)` —— 把占位符换成真凭证；
4. 转发。

## 为什么「注入失败」绝不能降级为「原样发」

`inject_headers` 的语义是「按 host 规则注入请求头」。
如果规则存在但取不到密钥，**原样发出去等于把占位符当凭证发给上游** ——
上游会收到一个无效凭证并回 401，**而调用方看到的是一次「正常的 401」，不是「代理配置坏了」。**
**那是最坏的一种失败：把配置错误伪装成上游错误。**

## 为什么被拒的请求必须在**连接之前**就拒

只断言「返回了 403」不够 —— **403 可能是先转发出去、拿到响应再改成 403。**
那样数据已经离开本机了。所以 `_guard` 在任何 socket 打开之前跑。
"""

from __future__ import annotations

import http.server
import threading
import urllib.parse
from typing import Any

import httpx

from app.egress import refuse_cloud_metadata

#: 进程里只放这个占位符，真凭证留在代理里。
PLACEHOLDER_ENV = "ORBIT_PROXY_TOKEN"


class EgressProxyRefusal(Exception):
    """代理拒绝转发。`status` 是要回给调用方的 HTTP 码。"""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


class EgressProxy:
    """把 `EgressPolicy` 应用到真实出站流量的最小守护进程。

    **`policy` 由外部注入** —— 测试可以给一个真策略配一条规则，
    生产由 `egress start` 从配置文件加载。**不在构造里读盘**，是为了让这个类可单测。
    """

    def __init__(self, policy: Any, *, timeout: float = 30.0) -> None:
        self.policy = policy
        self.timeout = timeout

    def guard(self, target: str, headers: dict[str, str] | None = None) -> dict[str, str]:
        """四步闸门：云元数据 → 策略 → 凭证注入。**返回可直接发出去的头。**

        **任何一步不过就抛 `EgressProxyRefusal`，绝不返回「原样转发」。**
        """
        try:
            refuse_cloud_metadata(target, where="egress 代理转发")
        except ValueError as exc:
            # **必须转成 403 而不是让它掉进 502** ——
            # 云元数据是「策略拒绝」，不是「上游故障」。两者的处置完全不同。
            raise EgressProxyRefusal(403, str(exc)) from exc

        decision = self.policy.check([target])
        if not decision.allowed:
            raise EgressProxyRefusal(403, decision.reason or "出站策略不允许")

        incoming = dict(headers or {})
        injected = self.policy.inject_headers(target, incoming)

        # **注入后仍是占位符 = 规则匹配了但密钥取不到。**
        # 这时候发出去，上游会回 401，而调用方会以为是自己的问题。
        token = injected.get("Authorization", "")
        if token.startswith("Bearer ") and token[len("Bearer ") :].strip() in _placeholder_values():
            raise EgressProxyRefusal(
                500,
                "有注入规则但取不到真凭证：**拒绝原样转发**——"
                "把占位符当凭证发出去，会把配置错误伪装成上游 401。",
            )
        return injected

    def forward(self, target: str, headers: dict[str, str] | None = None) -> httpx.Response:
        """过闸门后转发一次（非流式，测试与短请求用）。"""
        safe = self.guard(target, headers)
        with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
            return client.get(target, headers=safe)


def _placeholder_values() -> set[str]:
    """进程侧占位符的取值集合（环境里那个 + 常见字面量）。"""
    import os

    values = {"<proxy-token>", "PROXY_TOKEN", "placeholder"}
    token = os.environ.get(PLACEHOLDER_ENV, "").strip()
    if token:
        values.add(token)
    return values


class _Handler(http.server.BaseHTTPRequestHandler):
    """把请求交给 `EgressProxy`。`server.proxy` 由 `serve()` 注入。"""

    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 —— http.server 的约定
        """**只处理 `POST /__shutdown`**：代理自己退出，`stop` 不需要知道 pid。

        ## 为什么 `stop` 用这个而不是信号

        Windows 上 `os.kill(pid, 0)` 会真的 `TerminateProcess` ——
        第 88 轮我用它做「探活」，等于写了个随机杀进程的函数。
        **自停端点让 `stop` 完全不需要进程管理能力。**

        ## 两道约束

        - 只接受 `127.0.0.1`（代理本来就只绑回环，再挡一道）；
        - 只接受 `POST`（不给 GET，免得被 `<img>` / 预取意外触发）。
        """
        if self.path != "/__shutdown":
            self.send_error(404, "unknown endpoint")
            return
        if self.client_address[0] not in {"127.0.0.1", "::1"}:
            self.send_error(403, "loopback only")
            return
        body = b"stopping"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        # **在响应写完之后**再停，否则调用方拿到的是连接被断而不是 200。
        import threading

        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def do_GET(self) -> None:  # noqa: N802 —— http.server 的约定
        proxy: EgressProxy = self.server.proxy  # type: ignore[attr-defined]
        headers = {k: v for k, v in self.headers.items()}
        # 代理请求的目标在请求行里是绝对 URL。
        target = self.path
        if not urllib.parse.urlsplit(target).netloc:
            self.send_error(400, "absolute URL required")
            return
        try:
            response = proxy.forward(target, headers)
        except EgressProxyRefusal as refusal:
            body = refusal.reason.encode("utf-8")
            self.send_response(refusal.status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        except Exception as exc:  # noqa: BLE001 —— 上游故障统一 502
            body = f"上游请求失败：{type(exc).__name__}: {exc}".encode()
            self.send_response(502)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        self.send_response(response.status_code)
        self.send_header("Content-Type", response.headers.get("content-type", "text/plain"))
        self.send_header("Content-Length", str(len(response.content)))
        self.end_headers()
        self.wfile.write(response.content)

    def log_message(self, *args: Any) -> None:
        """静默：代理的日志走 `context.note`，不走 stderr。"""
        return


def serve(proxy: EgressProxy, *, host: str = "127.0.0.1", port: int = 0) -> http.server.HTTPServer:
    """起一个后台代理，返回 server（`server.server_port` 是实际端口）。"""
    server = http.server.HTTPServer((host, port), _Handler)
    server.proxy = proxy  # type: ignore[attr-defined]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
