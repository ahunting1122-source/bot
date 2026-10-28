"""本地 OAuth 回调服务器 + 整条授权码流程（MATRIX 8.3 的后半段）。

## 为什么这一块可以是真的，而 `mcp_oauth_flow` 那一半只能如实抛

上一轮把 OAuth 切成两半时，`run_flow()` 抛的清单里有四条：出网做元数据发现、动态客户端
注册、**起本地回调服务器**、用户在自己的浏览器里点授权。前两条和最后一条确实要外部条件，
但第三条——"在本机回环端口起一个 HTTP 服务器，等一次带 `?code=&state=` 的重定向"——**完全
是本地行为**：`http.server` 是标准库，回环 socket 不需要网卡，state 校验是纯字符串比较。

所以这一轮把它做掉。做完之后整条流程的真实度是：

| 步骤 | 真不真 | 说明 |
|---|---|---|
| 生成 PKCE（verifier / challenge） | **真** | `mcp_oauth_flow.pkce_pair()`，RFC 7636 |
| 生成 `state` | **真** | `secrets.token_urlsafe(32)` |
| 占一个空闲回环端口并起 HTTP 服务器 | **真** | 真的 `bind()`，真的监听，端口是内核分配的 |
| 拼 authorize URL（含**真实**回调地址） | **真** | 端口只有 bind 之后才知道，所以 URL 是 bind 之后才拼的 |
| 打开浏览器 / 打印 URL | **真**（`webbrowser`）或**可注入** | 测试注入记录器，不真开浏览器 |
| 等回调、校验 `state`、取 `code` | **真** | 真的收了一次 HTTP 请求，错 state 真的被拒 |
| 拿 code 换 token（那一次 POST） | **假不了也真不了** | 需要出网；默认实现抛 :class:`NetworkUnavailable`，注入假 exchange 后整条流程跑通 |
| 令牌写进 keyring | **真** | 走 `mcp_oauth_flow.TokenStore`（双写，见那边的 docstring） |

判据就是这一句：**除了那一次 POST，其它全是真的**。

## 三条安全约束（都落在代码里，不是注释里的愿望）

1. **只绑回环**：`host` 不是回环地址直接抛 :class:`CallbackError`。回调服务器收的是一个
   带授权码的 URL，绑 `0.0.0.0` 等于把授权码开放给整个网段（同网段的人抢先 GET 一次就
   能把 code 换走）。
2. **state 一次性**：校验通过的那一刻就把 `state` 清空。之后同一个 `state` 再来（重放、
   或浏览器把历史里的地址又打开一次）一律 400 —— "用过就失效"必须由服务器记住，而不能
   靠调用方自觉。
3. **超时必关服务器**：`wait()` 超时先 `close()` 再抛 :class:`CallbackTimeout`。漏掉这一步
   就会在机器上留一个还在监听的端口，而用户看到的只是一条"授权超时"。

## 错 state 为什么不直接终止流程

一个陌生请求（端口扫描、别的标签页、被复制的旧链接）打进来时，**不能**让它把用户的授权
流程搞死——那等于给任何本机进程一个"取消授权"的按钮。所以错 state 只回 400 并记一次
`rejected`，服务器继续等正确的那一个；只有超时才收工。
"""

from __future__ import annotations

import logging
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from app.mcp_oauth_flow import (
    NetworkUnavailable,
    TokenPayload,
    TokenStore,
    build_authorize_url,
    exchange_payload,
    new_state,
    parse_token_response,
    pkce_pair,
    token_endpoint,
)

log = logging.getLogger("orbit.mcp.oauth.callback")

#: 回调服务器只允许绑这个地址族。`localhost` 也收（它可能先解析到 `::1`），
#: 但**不收** `0.0.0.0` / 空串 / 任何具体网卡地址——理由见模块 docstring 第 1 条。
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

#: 默认回调路径。授权服务器把 code 送回 `http://127.0.0.1:<port>/callback?code=…`。
CALLBACK_PATH = "/callback"

#: 默认等待时长（秒）。120 秒是"够用户点完授权、又不至于让进程干等半小时"的折中。
DEFAULT_TIMEOUT = 120.0

#: 授权 URL 的打开方式：`(url) -> None`。测试注入一个记录器/假浏览器，生产用
#: `webbrowser.open`。做成注入点而不是直接调 `webbrowser`，是为了让"整条流程除了那一次
#: POST 之外都能在离线环境里跑一遍"——否则测试一碰这条路径就会真弹浏览器。
Opener = Callable[[str], None]

#: 换 token 的函数：`(config, code, code_verifier) -> TokenPayload`。
Exchange = Callable[[Any, str, str], TokenPayload]


class CallbackError(RuntimeError):
    """回调服务器本身的失败（绑不上端口、地址不是回环、被重复启动……）。"""


class CallbackTimeout(CallbackError):
    """在超时之前没等到回调。**抛之前服务器已经关了**。"""


class CallbackRejected(CallbackError):
    """回调回来了，但里面是错误（如 `error=access_denied`）或没有 code。"""


@dataclass(frozen=True)
class CallbackResult:
    """一次回调的内容（授权服务器重定向回来的那些 query 参数）。"""

    code: str = ""
    state: str = ""
    error: str = ""
    error_description: str = ""

    @property
    def ok(self) -> bool:
        """拿到 code 且没有 error 才算成功。"""
        return bool(self.code) and not self.error


@dataclass
class _Outcome:
    """`_evaluate()` 的结论：回什么状态码 / 回什么话 / 要不要收工。"""

    status: int
    message: str
    result: CallbackResult | None = None


class CallbackServer:
    """回环地址上的单次回调服务器。

    生命周期是**显式**的：`start()` 绑端口 → `wait()` 等回调 → `close()` 收工。
    没有 `with` 语法糖是因为 `serve()` 已经把三步串好了；需要细粒度控制的调用方
    （想先拿到 `redirect_uri` 去拼 URL）走这三个方法。
    """

    def __init__(
        self,
        *,
        state: str = "",
        host: str = "127.0.0.1",
        path: str = CALLBACK_PATH,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        target = (host or "127.0.0.1").strip()
        if target not in LOOPBACK_HOSTS and not target.startswith("127."):
            raise CallbackError(
                f"拒绝在 {target} 上起回调服务器：只允许回环地址"
                f"（{' / '.join(sorted(LOOPBACK_HOSTS))}）。"
                "回调地址里带着授权码，绑非回环等于把它开放给整个网段。"
            )
        self.host = target
        self.path = path or CALLBACK_PATH
        self.timeout = float(timeout)
        #: CSRF 用的 state。**校验通过后会立刻被清空**（一次性）。
        self.state = state or new_state()
        #: 被拒的回调次数（state 不符 / 缺 code）。只用于诊断与测试断言。
        self.rejected = 0
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._done = threading.Event()
        self._result: CallbackResult | None = None
        self._lock = threading.Lock()

    # -- 生命周期 ---------------------------------------------------------
    @property
    def port(self) -> int:
        """真正绑上的端口（`port=0` 时由内核分配）。没启动时为 0。"""
        httpd = self._httpd
        if httpd is None:
            return 0
        return int(httpd.server_address[1])

    @property
    def redirect_uri(self) -> str:
        """要交给授权服务器的 `redirect_uri`。**必须在 `start()` 之后读**。"""
        if self._httpd is None:
            raise CallbackError("服务器还没启动，读不到回调地址（先调用 start()）")
        return f"http://{self.host}:{self.port}{self.path}"

    def start(self, *, port: int = 0) -> str:
        """绑定并开始服务，返回 `redirect_uri`。

        `port=0`（默认）= 让内核分配一个空闲端口：写死端口会撞上"这个端口被别的程序占了"
        这种和 OAuth 毫无关系的故障，而用户只会看到一句 bind 失败。
        """
        if self._httpd is not None:
            raise CallbackError("回调服务器已经启动了（一个实例只服务一次授权）")
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _respond(self, status: int, message: str) -> None:
                body = (
                    "<!doctype html><meta charset='utf-8'>"
                    "<title>Orbit OAuth</title>"
                    f"<h1>{status}</h1><p>{message}</p>"
                ).encode()
                self.send_response(status)
                self.send_header("content-type", "text/html; charset=utf-8")
                self.send_header("content-length", str(len(body)))
                self.send_header("connection", "close")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802 —— 标准库规定的名字
                parsed = urlparse(self.path)
                if parsed.path != server.path:
                    self._respond(404, f"路径不对：回调地址是 {server.path}")
                    return
                params = parse_qs(parsed.query)

                def first(name: str) -> str:
                    values = params.get(name) or [""]
                    return str(values[0] or "")

                outcome = server._evaluate(
                    code=first("code"),
                    state=first("state"),
                    error=first("error"),
                    error_description=first("error_description"),
                )
                # **先回话、再收工**：反过来的话，`wait()` 一侧可能已经把监听 socket
                # 关掉，客户端会看到一个连接重置而不是我们写的那段说明。
                self._respond(outcome.status, outcome.message)
                if outcome.result is not None:
                    server._finish(outcome.result)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                # 默认实现会把每个请求打到 stderr。回调服务器只收一次请求，
                # 静默它比留一行噪音更合适（要排障时看 `rejected` 与日志）。
                return

        try:
            httpd = ThreadingHTTPServer((self.host, int(port)), Handler)
        except OSError as exc:
            raise CallbackError(
                f"无法在 {self.host}:{port} 上起回调服务器：{exc}。"
                "（端口被占用时换 `port=0` 让内核分配）"
            ) from exc
        self._httpd = httpd
        self._thread = threading.Thread(
            target=httpd.serve_forever, name="orbit-oauth-callback", daemon=True
        )
        self._thread.start()
        log.debug("OAuth 回调服务器已启动：%s", self.redirect_uri)
        return self.redirect_uri

    def wait(self, timeout: float | None = None) -> CallbackResult:
        """阻塞等回调，返回结果。超时**先关服务器再抛** :class:`CallbackTimeout`。"""
        limit = self.timeout if timeout is None else float(timeout)
        try:
            arrived = self._done.wait(max(0.0, limit))
        finally:
            if not self._done.is_set():
                # 超时（或等待被中断）：无论如何都要关掉，否则机器上留一个还在监听的端口。
                self.close()
        if not arrived:
            raise CallbackTimeout(
                f"{limit:.0f} 秒内没有收到 OAuth 回调（回调地址已经关闭）。"
                "常见原因：浏览器里点了拒绝、地址栏被拦、或者授权服务器不接受回环重定向。"
            )
        result = self._result
        if result is None:  # pragma: no cover - _finish 与 _done 是一起设置的
            raise CallbackError("回调事件已触发但结果为空（内部状态不一致）")
        self.close()
        return result

    def serve(
        self,
        *,
        port: int = 0,
        timeout: float = DEFAULT_TIMEOUT,
        opener: Opener | None = None,
        build_url: Callable[[str], str] | None = None,
    ) -> CallbackResult:
        """一次性跑完"绑端口 → 打开授权 URL → 等回调 → 关服务器"。

        ``opener``：`(url) -> None`，生产是 `webbrowser.open`。**测试注入一个假浏览器**
        （收到 URL 后自己对回环地址发一个请求），于是整条路径不碰真浏览器、不出网。

        ``build_url``：`(redirect_uri) -> authorize_url`。授权 URL 里必须带**真实**
        回调地址，而端口只有 bind 之后才知道，所以 URL 不能提前拼好传进来——只能传一个
        构造函数。给了它就用它的结果喂 `opener`；没给就把 `redirect_uri` 本身交给
        `opener`（等价于"直接把浏览器送到回调地址"，只对调试有意义）。
        """
        self.start(port=port)
        try:
            target = self.redirect_uri
            if build_url is not None:
                target = build_url(self.redirect_uri)
            if opener is not None:
                opener(target)
            return self.wait(timeout)
        finally:
            self.close()

    def close(self) -> None:
        """关服务器（幂等）。没启动过、或已经关过都不抛。"""
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception as exc:  # noqa: BLE001 - 没起 serve_forever 时 shutdown 会抛
                log.debug("shutdown 回调服务器失败（可能从未开始服务）：%s", exc)
            try:
                httpd.server_close()
            except OSError as exc:  # pragma: no cover - 关一个已关的 socket
                log.debug("server_close 失败：%s", exc)
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    # -- 回调判定 ---------------------------------------------------------
    def _evaluate(self, *, code: str, state: str, error: str, error_description: str) -> _Outcome:
        """判定一次回调：错误 / 缺 code / state 不符 / 通过。

        顺序是刻意的：**先看 state，再看 code**。反过来会让一个伪造请求（错 state +
        带一个能用的 code）在日志里留下"收到 code"的痕迹，掩盖掉"有人在打这个端口"。
        """
        if error:
            # 授权服务器主动回错误（用户点了拒绝、scope 被改……）：这是终态，
            # 继续等下去只会等到超时，所以带着 error 收工。
            return _Outcome(
                status=200,
                message=f"授权未完成：{error}",
                result=CallbackResult(state=state, error=error, error_description=error_description),
            )
        if not state:
            self.rejected += 1
            return _Outcome(status=400, message="缺少 state 参数：无法确认这是本次授权的回调")
        with self._lock:
            expected = self.state
            if not expected or state != expected:
                self.rejected += 1
                return _Outcome(
                    status=400,
                    message="state 不匹配（可能是一次 CSRF 试探，或这条链接已经用过/过期）",
                )
            # 一次性：**校验通过的同时作废**。放在锁里是为了让两个并发请求只有一个能过。
            self.state = ""
        if not code:
            self.rejected += 1
            return _Outcome(status=400, message="回调里没有 code 参数")
        return _Outcome(
            status=200,
            message="授权已完成，可以关闭这个窗口回到 Orbit。",
            result=CallbackResult(code=code, state=state),
        )

    def _finish(self, result: CallbackResult) -> None:
        """记下结果并唤醒 `wait()`。**只生效一次**（后来的回调不会覆盖已到的结果）。"""
        with self._lock:
            if self._result is None:
                self._result = result
        self._done.set()


# ── 整条授权流程 ────────────────────────────────────────────────────────────


def _get(config: Any, key: str, default: Any = "") -> Any:
    """从 dict 或对象里读一个字段（与 `mcp_oauth_flow._get` 同一取舍：两种都收）。"""
    if isinstance(config, dict):
        value = config.get(key)
    else:
        value = getattr(config, key, None)
    return default if value is None else value


def as_config_dict(config: Any) -> dict[str, Any]:
    """把配置摊成 dict，便于**替换掉 `redirect_uri`** 之后再拼 URL。

    为什么必须替换：`build_authorize_url` 从配置里读回调地址，而真实回调地址只有
    bind 之后才知道。直接改用户的 dict 会污染调用方（它可能拿同一份 dict 去做别的），
    所以这里复制一份。
    """
    if isinstance(config, dict):
        return dict(config)
    return {
        key: getattr(config, key, "")
        for key in (
            "url",
            "client_id",
            "scope",
            "scopes",
            "redirect_uri",
            "authorization_endpoint",
            "token_endpoint",
            "include_resource",
            "name",
        )
        if hasattr(config, key)
    }


def default_exchange(
    config: Any, code: str, code_verifier: str, *, transport: Any = None
) -> TokenPayload:
    """默认的"拿 code 换 token"：真发那一次 POST。**本机无网络 → 抛 `NetworkUnavailable`。**

    ``transport`` 是注入点（签名 `(url, payload) -> dict`）。不注入时如实抛，并说清缺的是
    哪一次请求——这是整条流程里**唯一**真的做不到的一步，报错必须让人一眼看出这一点，
    而不是以为回调服务器或 state 校验出了问题。
    """
    payload = exchange_payload(config, code, code_verifier)
    endpoint = token_endpoint(config)
    if transport is None:
        raise NetworkUnavailable(
            "拿授权码换令牌需要 POST 到 "
            f"{endpoint or '（无法从服务 url 推出令牌端点）'}，本机无网络，这一步做不了。"
            "**这一步是整条授权流程里唯一需要出网的地方**：回调服务器、state 校验、"
            "PKCE、keyring 写入都是真的，只有这一次 POST 缺网络。"
        )
    return parse_token_response(transport(endpoint, payload))


@dataclass(frozen=True)
class AuthorizeFlowResult:
    """一次授权流程的**完整交代**：每一步做到没有，都记下来。"""

    server: str
    redirect_uri: str
    authorize_url: str
    state: str
    code: str
    token: TokenPayload | None = None
    saved: bool = False
    rejected: int = 0
    steps: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """落 JSON 用。**不含 code_verifier**（秘密），code 本身也只在流程内用一次。"""
        return {
            "server": self.server,
            "redirect_uri": self.redirect_uri,
            "authorize_url": self.authorize_url,
            "state": self.state,
            "code_received": bool(self.code),
            "token_obtained": self.token is not None,
            "saved": self.saved,
            "rejected_callbacks": self.rejected,
            "steps": list(self.steps),
        }


def _default_opener(url: str) -> None:
    """生产用：打印 + 试着拉起系统浏览器。

    打印**不是**多余的：无头环境（SSH / CI）里 `webbrowser.open` 返回 False 什么都不做，
    而用户需要那条 URL 去别的机器上打开。所以先打印再尝试打开。
    """
    print(f"请在浏览器里打开下面的地址完成授权：\n{url}\n")
    try:
        opened = webbrowser.open(url)
    except Exception as exc:  # noqa: BLE001 - 没有桌面会话时各种异常都可能
        log.debug("拉起浏览器失败（URL 已打印）：%s", exc)
        return
    if not opened:
        log.debug("系统里没有可用的浏览器（URL 已打印）")


def run_authorize_flow(
    config: Any,
    *,
    server: CallbackServer | None = None,
    exchange: Exchange | None = None,
    opener: Opener | None = None,
    store: TokenStore | None = None,
    timeout: float | None = None,
    port: int = 0,
) -> AuthorizeFlowResult:
    """走一遍授权码流程：起回调 → 打开 URL → 等 code → 换 token → 存 keyring。

    除了 `exchange`（默认实现要出网）之外的每一步都是真的：
    PKCE、state、回环服务器、state 校验、浏览器打开（可注入）、keyring 写入。

    ``exchange`` 注入一个假实现后，整条流程在离线环境里**跑得通**——这就是"这条能不能
    标完成"的判据。默认实现见 :func:`default_exchange`（抛 `NetworkUnavailable`）。
    """
    name = str(_get(config, "name") or _get(config, "server") or "")
    callback = server if server is not None else CallbackServer()
    expected_state = callback.state
    verifier, challenge = pkce_pair()
    #: 回调地址要在 `serve()` 返回**之前**记下来：`serve()` 结束时服务器已经关了，
    #: 那时再读 `callback.redirect_uri` 会抛（"还没启动"）。
    captured: dict[str, str] = {}

    def build_url(redirect_uri: str) -> str:
        # 用**真实**回调地址覆盖配置里的那个（配置里的往往是占位值 / 别的端口）。
        captured["redirect_uri"] = redirect_uri
        overridden = {**as_config_dict(config), "redirect_uri": redirect_uri}
        return build_authorize_url(overridden, state=expected_state, code_challenge=challenge)

    opened: list[str] = []
    # 生产路径（没注入 opener）必须**真的**把 URL 打印出来并尝试拉起浏览器：不这样的话
    # `serve()` 会安静地等到超时，用户只会看到一句"授权超时"，而实际上它连 URL 都没给他。
    driver = opener if opener is not None else _default_opener

    def remember(url: str) -> None:
        opened.append(url)
        driver(url)

    result = callback.serve(
        port=port,
        timeout=callback.timeout if timeout is None else float(timeout),
        opener=remember,
        build_url=build_url,
    )
    authorize_url = opened[0] if opened else ""
    redirect_uri = captured.get("redirect_uri", "")

    steps: list[dict[str, Any]] = [
        {"step": "起本地回调服务器", "done": bool(redirect_uri), "detail": redirect_uri},
        {"step": "打开 / 打印授权 URL", "done": bool(authorize_url), "detail": authorize_url},
        {
            "step": "收到回调并校验 state",
            "done": bool(result.state) and result.state == expected_state,
            "detail": f"被拒 {callback.rejected} 次" if callback.rejected else "一次通过",
        },
    ]
    if result.error:
        steps.append({"step": "换 token", "done": False, "detail": f"授权服务器报错：{result.error}"})
        raise CallbackRejected(
            f"授权未完成：{result.error}"
            + (f"（{result.error_description}）" if result.error_description else "")
        )
    if not result.code:
        steps.append({"step": "换 token", "done": False, "detail": "回调里没有 code"})
        raise CallbackRejected("回调里没有 code，无法继续换 token")
    if result.state != expected_state:
        # 理论上到不了这里（服务器已经校验过），留着是因为"信任下游的校验"是这类流程
        # 最典型的漏洞来源：state 的比较必须由**用它的那一侧**再做一次。
        raise CallbackRejected("回调的 state 与本次流程不符（已拒绝，不做换 token）")

    token: TokenPayload | None = None
    exchange_fn = exchange if exchange is not None else default_exchange
    try:
        token = exchange_fn(config, result.code, verifier)
    except NetworkUnavailable as exc:
        steps.append({"step": "换 token", "done": False, "detail": "需要网络（那一次 POST）"})
        raise NetworkUnavailable(str(exc)) from exc
    steps.append(
        {
            "step": "换 token",
            "done": token is not None,
            "detail": "拿到 access_token" if token is not None else "未返回令牌",
        }
    )
    saved = False
    if token is not None and store is not None:
        store.save(token)
        saved = True
        steps.append(
            {
                "step": "写入 keyring",
                "done": True,
                "detail": f"{store.service}/{store.server}",
            }
        )
    else:
        steps.append(
            {
                "step": "写入 keyring",
                "done": False,
                "detail": "没有传 store，令牌没有落库" if token is not None else "没有令牌可存",
            }
        )
    return AuthorizeFlowResult(
        server=name,
        redirect_uri=redirect_uri,
        authorize_url=authorize_url,
        state=expected_state,
        code=result.code,
        token=token,
        saved=saved,
        rejected=callback.rejected,
        steps=steps,
    )


def describe() -> str:
    """这一层做到什么、没做到什么。CLI 的 `mcp auth` 说明直接打印它。"""
    return "\n".join(
        [
            "MCP OAuth 本地回调（除那一次换 token 的 POST 之外全是真的）：",
            "  1) 回环 HTTP 服务器：真的 bind、真的监听，端口由内核分配（port=0）；",
            "  2) state 防 CSRF：校验通过后立刻作废（一次性），错 state 回 400 但不终止流程；",
            "  3) 超时必关服务器：wait() 超时先 close() 再抛，不留监听端口；",
            "  4) PKCE（S256）、authorize URL、令牌响应解析、keyring 写入都是真的；",
            "  5) 浏览器打开可注入：生产用 webbrowser，测试注入记录器，不弹窗。",
            "做不到的只有一步：拿 code 换 token 的那次 POST 需要出网，"
            "默认实现如实抛 NetworkUnavailable；注入假 exchange 后整条流程可离线跑通。",
            "仍然缺（要真跑需要外部条件）：出网做 RFC 9728 / RFC 8414 元数据发现、"
            "动态客户端注册（RFC 7591）拿 client_id、以及一个能真授权的人。",
        ]
    )


__all__ = [
    "CALLBACK_PATH",
    "DEFAULT_TIMEOUT",
    "LOOPBACK_HOSTS",
    "AuthorizeFlowResult",
    "CallbackError",
    "CallbackRejected",
    "CallbackResult",
    "CallbackServer",
    "CallbackTimeout",
    "Exchange",
    "Opener",
    "as_config_dict",
    "default_exchange",
    "describe",
    "run_authorize_flow",
]
