"""本地 OpenAI 兼容代理：把 anthropic / gemini 协议在本地翻成 OpenAI 面（MATRIX 4.7）。

## 为什么需要它

生态里大量客户端（Cursor、Continue、各种 OpenAI SDK 封装、LangChain 的
`ChatOpenAI`）只会说 OpenAI 的 `POST /v1/chat/completions`。而 Orbit 支持的三家里
有两家说的是**另一套协议**：Anthropic 的 `messages`（system 在顶层、工具是
`input_schema` / `tool_use` 内容块）、Gemini 的 `generateContent`（`contents` /
`parts` / `functionDeclarations`）。Hermes 的 `proxy` 就是干这件事：**在本地起一个
OpenAI 面，后端转给非 OpenAI 协议的 provider**（调研 06 §5.5）。

## 本层的边界（先说清楚，免得被当成完整代理）

**做到了**：

- `openai_to_provider()` / `provider_to_openai()`：`chat/completions` 的
  **messages / tools / usage** 三块的双向翻译，纯函数、可单测、不发请求；
- `ProxyPlan`：监听地址 + 上游 + 模型映射 + 鉴权模式，`port_in_use()` 做端口占用检查；
- `ProxyServer.handle()`：整包（非流式）链路，纯函数式（转发函数可注入，离线可测）；
- **SSE 流式透传**（MATRIX 4.7）：`handle_stream()` 把上游的 `text/event-stream`
  逐事件翻成 OpenAI 的 `data: {...}\n\n`，`stream: true` 不再降级成整包；
- **重试**：只对 5xx / 连接错误 / 超时重试（4xx 立刻失败），每次尝试记在返回值里；
- **并发控制**：一个信号量（默认 :data:`DEFAULT_CONCURRENCY` = 8），超了**排队**而不是拒；
- **鉴权**：可选 bearer token（`<data>/proxy.json` 的 `token`）；
- **两种上游鉴权模式**（MATRIX 4.7 的 OAuth 一半）：`auth_mode="api_key"`（默认）走
  `x-api-key` / `x-goog-api-key`；`auth_mode="oauth"` 走 `Authorization: Bearer <OAuth
  令牌>`，令牌由 :func:`oauth_token_provider` 从 keyring 取、过期就刷新；
- `ProxyServer.serve()`：`http.server` 起的最小 HTTP 服务。

**没做（如实列出）**：

- **流式只翻文本增量**：Anthropic 的 `thinking` / `tool_use` 增量、Gemini 的
  `functionCall` 增量都不转（工具调用在流式下会被丢掉，客户端会看到一个"只说了半句
  话"的回答）。非流式那条路是完整的（含 tool_calls）；
- **重试没有 jitter、不重试 429**：429 是限流信号，重试只会加重限流；本层按 4xx
  立刻失败，把决定权交回客户端（它有完整的退避策略）；
- **没有 `responses` / `embeddings` / 图片端点**：只有 `/v1/chat/completions`
  与 `/v1/models`；
- **OAuth 供应商的真实端点本机验不了**：取令牌、判过期、选头这些**本地逻辑**是真的
  且有测试，但"拿 OAuth 令牌去 Anthropic / Gemini 上游真能换回补全"这件事**一次都没
  跑过**（无网络、无可用 OAuth 客户端凭据）。所以 `describe()` 里把它单列成
  "代码在、没跑通"，不许它冒充已验证的能力。

## 一条硬约束：没有 token 就绝不能绑非回环地址

代理会替客户端把**上游凭据**带出去。绑 `0.0.0.0` 且没有 token，等于把"用这台机器的
模型额度"这件事开放给整个网段。所以这条约束**不只是 docstring 里的一句话**，而是
:func:`ensure_bindable` 的运行时检查——`serve()` 在 `bind()` 之前先过它，绑不上就报错
而不是"启动成功然后被人白用"。`plan_for()` 仍允许你填任意 host（计划是纯数据），
拦人的地方在真正启动那一刻。

## 端口占用检查为什么照抄 `ui.py` 而不是 import 它

`app/cli/commands/ui.py` 的 `port_in_use()` 用"能不能 bind 上"判定，且**不设
`SO_REUSEADDR`**（Windows 上那个选项允许两个 socket 绑同一端口，探针会永远报
"空闲"）。这里逐字复刻那段逻辑，但**不 import `ui.py`**：它是 CLI 层模块，会连带
拉进 console / skin / webbrowser 一堆 UI 依赖，让一个纯网络工具模块反向依赖 UI
层。同样的取舍在 `app/` 里已有先例（`egress` / `dashboard` 各留一份探针）。
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import mcp_oauth_flow as oauth_flow
from app import providers as pv

#: 默认监听地址。**只默认回环**：代理会把请求转给持有上游凭据的本机，绑 `0.0.0.0`
#: 等于把凭据借给整个网段用。
DEFAULT_HOST = "127.0.0.1"

#: 默认端口 8645 —— 与声明表里 `--port` 的帮助文本一致（dashboard 9119 / proxy 8645）。
DEFAULT_PORT = 8645

#: 回环地址集合。`ensure_bindable()` 用它判"这个 host 需不需要 token"。
LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1"})

#: 代理自己的配置文件（与 `settings.json` / `tools.json` 同规矩：落在数据目录）。
#: 目前只认一个字段：`{"token": "..."}`。
PROXY_CONFIG_NAME = "proxy.json"

#: 同时在飞的上游请求上限。超了**排队**（信号量），不是拒绝。
#:
#: 为什么是 8：上游都是按分钟计费的远程服务，客户端（Cursor / Continue）会并发几路
#: 补全 + 一路对话，8 路足够；再高只会把 429 从上游换到本机。
DEFAULT_CONCURRENCY = 8

#: 默认最大尝试次数（含第一次）。3 = 一次重试 + 一次兜底，够躲过瞬时 5xx。
DEFAULT_MAX_ATTEMPTS = 3

#: 会触发重试的状态码。**只有 5xx**：4xx（含 429）是"这次请求本身有问题 / 被限流"，
#: 重试只会把同一个错误再要一遍，还会加重限流。
RETRIABLE_STATUS: frozenset[int] = frozenset(range(500, 600))

#: 支持作为代理上游的 provider。**只有这两家**：它们是"协议不是 OpenAI"的那两家；
#: 其余 14 家本来就是 `chat_completions`，套一层代理除了多一次转发没有任何收益。
SUPPORTED_UPSTREAMS: tuple[str, ...] = ("anthropic", "gemini")

#: 上游 provider → 它真正说的线协议（与 `providers.ProviderProfile.transport` 对齐）。
UPSTREAM_TRANSPORTS: dict[str, str] = {
    "anthropic": "anthropic_messages",
    "gemini": "gemini",
}

#: 上游鉴权的两种模式（MATRIX 4.7 的 OAuth 一半）。
#:
#: `api_key` 是原来那条路：静态密钥进 `x-api-key` / `x-goog-api-key`。
#: `oauth` 是 Hermes 的 proxy 形态：**用 OAuth 令牌而不是 API key** 去访问上游——
#: 令牌由 `oauth_token_provider()` 从 keyring 取、过期就刷新，请求头变成
#: `Authorization: Bearer <令牌>`。
API_KEY, OAUTH = "api_key", "oauth"
AUTH_MODES: tuple[str, ...] = (API_KEY, OAUTH)

#: Anthropic 的 `max_tokens` 是**必填**字段（缺了直接 400），而 OpenAI 的
#: `max_tokens` 是可选的。客户端不给时用这个默认值——写在常量里而不是散在代码里，
#: 因为它会直接决定"最长回答能有多长"。
DEFAULT_MAX_TOKENS = 4096


def _refuse_cloud_metadata(url: str) -> None:
    """拒绝把代理请求转发到云元数据端点。

    **名单复用 `path_security`**，与 `providers.py` 里那条同一个来源——
    不是新建一份（两份必然漂移）。

    **为什么代理这条路要单独拦**：`proxy.py` 是本地 OpenAI 兼容代理，
    **它可以被任何客户端打到**（curl、第三方 SDK、别的进程），
    那条路根本不经过 `providers.py` 的模型客户端构造。
    **两条路互不覆盖，所以两处都要有。**

    **边界（如实写）**：只判字面量。DNS 解析到元数据 IP 的域名挡不住——
    那要真查 DNS，而这里在同步的转发路径上。**已知残余风险。**
    """
    from urllib.parse import urlparse

    from app.path_security import BLOCKED_HOSTNAMES, BLOCKED_IPS

    host = (urlparse(str(url or "")).hostname or "").strip().lower()
    if not host:
        return
    blocked = {name.lower() for name in BLOCKED_HOSTNAMES} | {
        str(ip).lower() for ip in BLOCKED_IPS
    }
    if host in blocked:
        raise ProxyError(
            f"拒绝转发到云元数据端点（{host}）："
            "**它的唯一用途是交出实例凭证，代理没有任何正当理由转发到那里**"
        )


class ProxyError(RuntimeError):
    """代理层的可预期失败（消息要能直接给人看）。"""


class UnsupportedUpstream(ProxyError):
    """`provider` 不在 `SUPPORTED_UPSTREAMS` 里。"""


class ProxyUpstreamError(ProxyError):
    """上游回了非 2xx。**带状态码**，因为重试判定全靠它。

    为什么不直接 `raise_for_status()`：`httpx.HTTPStatusError` 的消息里塞着完整 URL
    与响应头，把它原样回给客户端既泄露上游端点、又让"是不是 5xx"变成字符串匹配。
    这里只留 ``(status, 一段人话)``，重试判定读 ``status``。
    """

    def __init__(self, status: int, message: str = "") -> None:
        self.status = int(status)
        super().__init__(message or f"上游返回 HTTP {self.status}")


class ProxyAuthError(ProxyError):
    """请求没有带合法的 bearer token（代理开了鉴权时）。"""


class OAuthTokenError(ProxyError):
    """OAuth 上游拿不到可用令牌（没登录 / 没配 data_dir / 令牌过期且刷不了）。

    单独一个类型而不是一句 `ProxyError`：这三种失败的处置完全不同——没登录要去
    `orbit mcp auth`，过期要出网刷新，没配目录是配置写错了。混成一句话，用户只能
    拿到一句"上游请求失败"。
    """


# ── 重试 / 并发 / 鉴权的策略对象 ─────────────────────────────────────────────


def is_retriable(exc: BaseException) -> bool:
    """这次失败值不值得再试一次。

    只认三类：

    * :class:`ProxyUpstreamError` 且状态码在 :data:`RETRIABLE_STATUS`（5xx）；
    * 连接类错误（`ConnectionError` / `OSError` 一族）；
    * 超时（`TimeoutError`，以及 httpx 的 `ConnectTimeout` / `ReadTimeout`——
      后者按类名判，避免在模块顶层 import httpx）。

    **4xx 一律不重试**：请求本身有问题，重试只是把同一个错误再要一遍。
    """
    if isinstance(exc, ProxyUpstreamError):
        return exc.status in RETRIABLE_STATUS
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    if isinstance(exc, OSError):  # ConnectionError 的超集：网络栈的各种失败
        return True
    return any(
        marker in type(exc).__name__ for marker in ("Connect", "Timeout", "Transport")
    )


@dataclass(frozen=True)
class RetryPolicy:
    """重试策略。**纯数据**，``sleep`` 由调用方注入（测试里换成空函数）。

    退避是指数式的（``base_delay * factor**(n-1)``，封顶 ``max_delay``）：
    瞬时 5xx 通常在几十毫秒到几秒内恢复，立刻重试只会把同一波故障再撞一次。
    """

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_delay: float = 0.25
    factor: float = 2.0
    max_delay: float = 4.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ProxyError("RetryPolicy.max_attempts 至少是 1（否则一次都不发）")

    def delay_for(self, attempt: int) -> float:
        """第 ``attempt`` 次失败之后该等多久（``attempt`` 从 1 开始）。"""
        if attempt < 1:
            return 0.0
        return min(self.max_delay, self.base_delay * (self.factor ** (attempt - 1)))


@dataclass(frozen=True)
class Attempt:
    """一次上游尝试的记录。**每次尝试都要留痕**——"重试过几次、每次为什么失败"
    是排障时唯一有用的信息，只在最终错误里留一句"上游失败"等于把过程扔掉。"""

    index: int
    ok: bool
    status: int = 0
    error: str = ""
    retriable: bool = False
    delay: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "ok": self.ok,
            "status": self.status,
            "error": self.error,
            "retriable": self.retriable,
            "delay": round(self.delay, 3),
        }


def ensure_bindable(host: str, token: str) -> str:
    """检查"这个 host + 这个 token"能不能启动，返回规整后的 host。

    **本模块最重要的一条约束的落地点**：没有 token 时只允许绑回环地址。

    为什么是硬错误而不是警告：绑 `0.0.0.0` 的代理会把上游凭据借给整个网段，而用户
    看到的是"启动成功"——一个只在安全审计时才暴露的洞。这里宁可让启动失败，并给出
    两条明确出路（配 token，或改回 `127.0.0.1`）。
    """
    target = (host or DEFAULT_HOST).strip() or DEFAULT_HOST
    if target in LOOPBACK_HOSTS or target.startswith("127."):
        return target
    if (token or "").strip():
        return target
    raise ProxyError(
        f"拒绝监听 {target}：没有配 token 时代理只能绑回环地址（{' / '.join(sorted(LOOPBACK_HOSTS))}）。"
        f"绑非回环会把上游凭据借给整个网段。两条出路：在 "
        f"<data>/{PROXY_CONFIG_NAME} 里写 {{\"token\": \"...\"}}（客户端用 "
        "`Authorization: Bearer <token>`），或者改回 `--host 127.0.0.1`。"
    )


def config_path(data_dir: str | Path) -> Path:
    """`<data>/proxy.json` 的绝对路径。"""
    return Path(data_dir) / PROXY_CONFIG_NAME


def load_token(data_dir: str | Path) -> str:
    """读代理的 bearer token（`<data>/proxy.json` 的 `token` 字段）。

    坏文件 / 缺字段一律返回空串（= 不鉴权，于是 :func:`ensure_bindable` 只允许回环）：
    读不出来时**收紧**而不是放开，这才是安全的默认方向。
    """
    path = config_path(data_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("token", "") or "").strip()


# ── OAuth 令牌（MATRIX 4.7 的 OAuth 一半）───────────────────────────────────
#
# Hermes 的 proxy 是"到 OAuth provider"：上游不要 API key，要**用户授权拿到的令牌**。
# 本层能真做的部分是**取令牌与判过期**——两件纯本地的事：
#
# * 取：令牌在系统 keyring 里（`mcp_oauth_flow.TokenStore`，服务名带 data_dir 与 server）；
# * 判：`mcp_oauth_flow.is_expired()` 是纯函数，读的是绝对到期时刻。
#
# 真做不到的是**刷新**：`POST <token_endpoint>` 要出网。所以默认的刷新实现如实抛
# `NetworkUnavailable`，而"注入一个假刷新函数"这条路径能整条跑通（测试就是这么做的）。
# 由此得到一句如实的结论：**OAuth 模式的本地逻辑是真的，端到端没跑通**。


def _config_value(config: Any, key: str, default: Any = "") -> Any:
    """从 dict 或对象里读一个字段（两种都收，免得调用方为了用它先转一层）。"""
    if isinstance(config, dict):
        value = config.get(key)
    else:
        value = getattr(config, key, None)
    return default if value is None else value


def oauth_store(
    config: Any, store: oauth_flow.TokenStore | None = None
) -> oauth_flow.TokenStore:
    """OAuth 上游的令牌存储：给了 ``store`` 就用它，否则按配置现建一个。

    为什么需要 ``server`` 与 ``data_dir`` 两项：keyring 的服务名是
    `orbit:<data_dir>:mcp:<server>`，少一个就不知道该去读哪一条——而"读不到"会被误报成
    "用户没登录"，排查方向直接跑偏。所以缺项时**报配置错**，不猜。
    """
    if store is not None:
        return store
    server = str(_config_value(config, "server") or _config_value(config, "name") or "").strip()
    data_dir = str(_config_value(config, "data_dir") or "").strip()
    missing = [
        name for name, value in (("server（或 name）", server), ("data_dir", data_dir)) if not value
    ]
    if missing:
        raise OAuthTokenError(
            f"OAuth 上游缺少配置：{'、'.join(missing)}。令牌存在系统 keyring 里，"
            "服务名是 `orbit:<data_dir>:mcp:<server>`，这两项缺一个就不知道该读哪一条。"
        )
    return oauth_flow.TokenStore(Path(data_dir).resolve(), server)


def oauth_token_provider(
    config: Any,
    *,
    store: oauth_flow.TokenStore | None = None,
    refresh: Callable[[oauth_flow.TokenPayload], oauth_flow.TokenPayload] | None = None,
    clock: Callable[[], float] | None = None,
) -> Callable[[], str]:
    """造一个"随时给我一个能用的 OAuth 令牌"的函数（`() -> str`）。

    每次调用都重新读 keyring 并判过期——**不做进程内缓存**。理由：代理是长驻进程，
    令牌会在进程活着的时候过期；缓存住第一次读到的值，等于让代理在几小时后开始
    稳定收 401，而重启一次就好，这种故障最难查。

    三件事分得很清楚：

    * **取令牌**：纯本地（keyring 读），没有令牌抛 :class:`OAuthTokenError`；
    * **判过期**：纯本地纯函数（`is_expired`，带 60 秒提前量）；
    * **刷新**：需要出网。默认实现走 `mcp_oauth_flow.refresh_token(transport=None)`，
      也就是**如实抛** `NetworkUnavailable`；注入 ``refresh=`` 就能离线跑通整条路。

    ``clock`` 是"现在几点"的注入点：过期判定必须能被测试钉住，否则只能靠 `sleep`。
    """
    target = oauth_store(config, store)
    now = clock or time.time

    def refresh_needs_network(old: oauth_flow.TokenPayload) -> oauth_flow.TokenPayload:
        """默认刷新：`transport=None` 时 `refresh_token` 会如实抛 `NetworkUnavailable`。

        **故意不吞**——把"刷不了"降级成"拿旧令牌继续"，会让上游回 401，排查方向
        跑到令牌有效期上去。
        """
        return oauth_flow.refresh_token(config, old, store=target)

    def provider() -> str:
        token = target.load()
        if token is None:
            raise OAuthTokenError(
                f"OAuth 上游没有令牌（keyring 服务名 {target.service}）。先跑一次授权："
                "`orbit mcp login <server>`，或用 `orbit mcp auth set <server>` 手工写入。"
            )
        if oauth_flow.is_expired(token, now=now()):
            refresher = refresh or refresh_needs_network
            token = refresher(token)
        return token.access_token

    return provider


# ── 计划 ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProxyPlan:
    """一次代理的完整参数。**纯数据**：不碰 socket、不读配置。

    `model_map` 的方向是"客户端请求的模型名 → 上游真实模型名"。客户端通常写的是
    OpenAI 风格的模型名（`gpt-4o-mini`），而上游只认自己的名字
    （`claude-sonnet-5` / `gemini-3.5-flash`）。表里没有的名字**原样透传**——很多
    客户端会直接写上游模型名，此时不该报错。
    """

    listen_host: str = DEFAULT_HOST
    listen_port: int = DEFAULT_PORT
    upstream_provider: str = "anthropic"
    base_url: str = ""
    model_map: dict[str, str] = field(default_factory=dict)
    #: 上游鉴权模式（见 :data:`AUTH_MODES`）。**默认 `api_key`**：引入 OAuth 不能改变
    #: 既有行为——没显式要 OAuth 的人必须还走 `x-api-key` 那条路。
    auth_mode: str = API_KEY

    def __post_init__(self) -> None:
        if self.upstream_provider not in SUPPORTED_UPSTREAMS:
            raise UnsupportedUpstream(
                f"不支持把 `{self.upstream_provider}` 当代理上游：只有 "
                f"{' / '.join(SUPPORTED_UPSTREAMS)} 的协议与 OpenAI 不同，需要翻译。"
                "其余 provider 本身就是 OpenAI 兼容面，客户端直连即可。"
            )
        if self.auth_mode not in AUTH_MODES:
            raise ProxyError(
                f"未知的 auth_mode：`{self.auth_mode}`（可用：{' / '.join(AUTH_MODES)}）。"
                "`api_key` 走静态密钥头，`oauth` 走 `Authorization: Bearer <OAuth 令牌>`。"
            )

    @property
    def transport(self) -> str:
        return UPSTREAM_TRANSPORTS[self.upstream_provider]

    @property
    def url(self) -> str:
        port = self.listen_port if self.listen_port > 0 else "<系统分配>"
        return f"http://{self.listen_host}:{port}/v1"

    def resolve_model(self, name: str) -> str:
        """客户端模型名 → 上游模型名（表里没有就原样透传）。"""
        key = (name or "").strip()
        return self.model_map.get(key, key)

    def to_dict(self) -> dict[str, Any]:
        return {
            "listen_host": self.listen_host,
            "listen_port": self.listen_port,
            "upstream_provider": self.upstream_provider,
            "transport": self.transport,
            "base_url": self.base_url,
            "model_map": dict(self.model_map),
            "auth_mode": self.auth_mode,
            "url": self.url,
        }

    def describe(self) -> str:
        """给人看的一段说明。**端口占用不在这里查**——那是 `port_in_use()` 的事，
        计划对象保持"不碰 socket"的纯粹性。"""
        mapped = "、".join(f"{key}→{value}" for key, value in self.model_map.items())
        auth = (
            "OAuth 令牌（Authorization: Bearer <令牌>，从 keyring 取、过期刷新）"
            if self.auth_mode == OAUTH
            else "API key（x-api-key / x-goog-api-key）"
        )
        lines = [
            f"监听：{self.url}（OpenAI 兼容面，只有 /v1/chat/completions 与 /v1/models）",
            f"上游：{self.upstream_provider}（线协议 {self.transport}）",
            f"上游端点：{self.base_url or '（未填，需在 Settings 里配 base_url）'}",
            f"上游鉴权：{auth}",
            f"模型映射：{mapped or '（空：客户端写的模型名原样透传）'}",
        ]
        if self.listen_host not in {"127.0.0.1", "localhost", "::1"}:
            lines.append(
                f"警告：监听 {self.listen_host} 不是回环地址 —— 上游凭据会暴露给整个网段。"
            )
        return "\n".join(lines)


def plan_for(
    provider: str = "anthropic",
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    model_map: dict[str, str] | None = None,
    auth_mode: str = API_KEY,
) -> ProxyPlan:
    """按 provider 造一份计划：端点取 `providers.PROVIDERS` 的默认值。"""
    key = (provider or "").strip().lower()
    profile = pv.PROVIDERS.get(key)
    if profile is None:
        raise UnsupportedUpstream(f"未知 provider：{key}（`orbit proxy providers` 看可用上游）")
    return ProxyPlan(
        listen_host=(host or DEFAULT_HOST).strip() or DEFAULT_HOST,
        listen_port=int(port),
        upstream_provider=key,
        base_url=profile.default_base_url,
        model_map=dict(model_map or {}),
        auth_mode=(auth_mode or API_KEY).strip().lower(),
    )


def port_in_use(host: str, port: int) -> bool:
    """端口是否已被占用。用"能不能 bind 上"判定，不连、不发包。

    **不设 SO_REUSEADDR**：Windows 上那个选项允许两个 socket 绑同一端口，探针会
    永远报"空闲"——一个永远回答"空闲"的健康检查比没有更糟。逻辑与
    `app/cli/commands/ui.py::port_in_use` 一致（只读它，不 import 它，理由见模块
    docstring）。
    """
    if port <= 0:
        return False
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        probe = socket.socket(family, socket.SOCK_STREAM)
    except OSError:
        return False
    try:
        probe.bind((host, port))
    except OSError:
        return True
    finally:
        probe.close()
    return False


# ── 协议翻译：OpenAI 请求 → 上游请求 ────────────────────────────────────────


def _text_of(content: Any) -> str:
    """把 OpenAI 的 `content`（字符串或 parts 数组）压成纯文本。

    图片 part（`{"type":"image_url"}`）**明确抛错**而不是丢掉：静默丢内容会让模型
    "看不见图"却给出看起来正常的回答，比报错危险得多（与 `providers._text_of`
    同一取舍）。
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {None, "text"}:
                parts.append(str(item.get("text", "")))
            else:
                raise ProxyError(
                    "本代理只翻译纯文本内容块：图片（image_url / inline_data）需要各自的"
                    "线格式，本轮未实现。"
                )
        return "".join(parts)
    return str(content)


def _tool_calls_of(message: dict[str, Any]) -> list[dict[str, Any]]:
    calls = message.get("tool_calls")
    return [call for call in calls if isinstance(call, dict)] if isinstance(calls, list) else []


def _arguments_of(call: dict[str, Any]) -> dict[str, Any]:
    """OpenAI 的 `arguments` 是 **JSON 字符串**，上游要的是对象。

    解析失败 → 抛错（不静默当空对象）：把坏参数变成 `{}` 会让工具收到一份"看起来
    合法"的空入参，模型和用户都看不出哪里错了。
    """
    raw = call.get("function", {}).get("arguments", "")
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise ProxyError(f"工具调用参数不是合法 JSON：{str(raw)[:120]}") from exc
    if not isinstance(parsed, dict):
        raise ProxyError(f"工具调用参数必须是 JSON 对象，收到：{str(raw)[:120]}")
    return parsed


def _openai_tools_to_anthropic(tools: Any) -> list[dict[str, Any]]:
    """`[{type:function, function:{...}}]` → `[{name, description, input_schema}]`。"""
    out: list[dict[str, Any]] = []
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        item: dict[str, Any] = {"name": str(function.get("name", ""))}
        if function.get("description"):
            item["description"] = str(function["description"])
        item["input_schema"] = function.get("parameters") or {"type": "object", "properties": {}}
        out.append(item)
    return out


def _openai_tools_to_gemini(tools: Any) -> list[dict[str, Any]]:
    """`[{type:function, function:{...}}]` → `[{functionDeclarations:[...]}]`。"""
    declarations: list[dict[str, Any]] = []
    for tool in tools if isinstance(tools, list) else []:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        item: dict[str, Any] = {"name": str(function.get("name", ""))}
        if function.get("description"):
            item["description"] = str(function["description"])
        parameters = function.get("parameters")
        if isinstance(parameters, dict) and parameters:
            item["parameters"] = parameters
        declarations.append(item)
    return [{"functionDeclarations": declarations}] if declarations else []


def _anthropic_tool_choice(choice: Any) -> dict[str, Any] | None:
    """OpenAI 的 `tool_choice` → Anthropic 的 `{type: auto|any|tool}`。"""
    if choice is None:
        return None
    if isinstance(choice, str):
        return {"auto": {"type": "auto"}, "required": {"type": "any"}, "none": None}.get(choice)
    if isinstance(choice, dict):
        function = choice.get("function")
        if isinstance(function, dict) and function.get("name"):
            return {"type": "tool", "name": str(function["name"])}
    return None


def _to_anthropic(payload: dict[str, Any], *, stream: bool = False) -> dict[str, Any]:
    """OpenAI `chat/completions` 请求 → Anthropic `messages` 请求。"""
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "user"))
        if role in {"system", "developer"}:
            system_parts.append(_text_of(message.get("content")))
            continue
        if role == "tool":
            # OpenAI 的工具结果是独立的一条消息；Anthropic 要的是**user 消息里的
            # tool_result 内容块**。拼错这块，模型会以为工具输出是用户说的话。
            messages.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": str(message.get("tool_call_id", "")),
                            "content": _text_of(message.get("content")),
                        }
                    ],
                }
            )
            continue
        content: Any = _text_of(message.get("content"))
        calls = _tool_calls_of(message)
        if calls and role == "assistant":
            blocks: list[dict[str, Any]] = []
            if content:
                blocks.append({"type": "text", "text": content})
            for call in calls:
                function = call.get("function", {})
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": str(call.get("id", "")),
                        "name": str(function.get("name", "")),
                        "input": _arguments_of(call),
                    }
                )
            content = blocks
        messages.append({"role": "assistant" if role == "assistant" else "user", "content": content})

    body: dict[str, Any] = {
        "model": str(payload.get("model", "")),
        "max_tokens": int(payload.get("max_tokens") or payload.get("max_completion_tokens") or DEFAULT_MAX_TOKENS),
        "messages": messages,
    }
    system = "\n\n".join(part for part in system_parts if part)
    if system:
        body["system"] = system
    tools = _openai_tools_to_anthropic(payload.get("tools"))
    if tools:
        body["tools"] = tools
        choice = _anthropic_tool_choice(payload.get("tool_choice"))
        if choice is not None:
            body["tool_choice"] = choice
    for source_key, target_key in (("temperature", "temperature"), ("top_p", "top_p")):
        if payload.get(source_key) is not None:
            body[target_key] = payload[source_key]
    stops = payload.get("stop")
    if isinstance(stops, str):
        body["stop_sequences"] = [stops]
    elif isinstance(stops, list) and stops:
        body["stop_sequences"] = [str(item) for item in stops]
    # `stream` **只在真的走流式那条路时为 True**：默认 False 是刻意的——`handle()` 按
    # 整包解析，把 `stream: true` 透上去会拿到一串事件流然后解析失败。流式由
    # `handle_stream()` 显式传 `stream=True`，两条路各自自洽。
    body["stream"] = bool(stream)
    return body


def _to_gemini(payload: dict[str, Any]) -> dict[str, Any]:
    """OpenAI `chat/completions` 请求 → Gemini `generateContent` 请求。"""
    system_parts: list[str] = []
    contents: list[dict[str, Any]] = []
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role", "user"))
        if role in {"system", "developer"}:
            system_parts.append(_text_of(message.get("content")))
            continue
        if role == "tool":
            contents.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": str(message.get("name", "")),
                                "response": {"content": _text_of(message.get("content"))},
                            }
                        }
                    ],
                }
            )
            continue
        parts: list[dict[str, Any]] = []
        text = _text_of(message.get("content"))
        if text:
            parts.append({"text": text})
        if role == "assistant":
            for call in _tool_calls_of(message):
                function = call.get("function", {})
                parts.append(
                    {
                        "functionCall": {
                            "name": str(function.get("name", "")),
                            "args": _arguments_of(call),
                        }
                    }
                )
        if parts:
            # Gemini 的角色只有 user / model 两种，assistant → model。
            contents.append({"role": "model" if role == "assistant" else "user", "parts": parts})

    body: dict[str, Any] = {"contents": contents}
    system = "\n\n".join(part for part in system_parts if part)
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    tools = _openai_tools_to_gemini(payload.get("tools"))
    if tools:
        body["tools"] = tools
    generation: dict[str, Any] = {}
    if payload.get("temperature") is not None:
        generation["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        generation["topP"] = payload["top_p"]
    limit = payload.get("max_tokens") or payload.get("max_completion_tokens")
    if limit is not None:
        generation["maxOutputTokens"] = int(limit)
    stops = payload.get("stop")
    if isinstance(stops, str):
        generation["stopSequences"] = [stops]
    elif isinstance(stops, list) and stops:
        generation["stopSequences"] = [str(item) for item in stops]
    if generation:
        body["generationConfig"] = generation
    return body


def openai_to_provider(
    payload: dict[str, Any], provider: str, *, stream: bool = False
) -> dict[str, Any]:
    """**纯函数**：OpenAI 请求体 → 上游请求体。

    覆盖三块：`messages`（含 system 上提 / 工具结果与 tool_use 块）、`tools`
    （`function` 结构 → `input_schema` / `functionDeclarations`）、以及生成参数
    （`max_tokens` / `temperature` / `stop`）。`usage` 是响应侧的字段，见
    `provider_to_openai()`。

    ``stream`` 默认 **False**：本函数的默认行为是"整包"，只有 :meth:`ProxyServer.
    handle_stream` 才传 ``True``。把它做成显式参数而不是读 ``payload["stream"]``，
    是为了让"我要流式"成为调用方的决定，而不是客户端随手写的一个字段——两条路的
    响应形状完全不同（整包 vs `data:` 帧），搞混就是解析失败。

    不认识的上游 → `UnsupportedUpstream`；不认识的结构 → `ProxyError`。
    **不做"尽力而为"的近似**：翻译错的请求会静默变成另一种语义，比直接报错难查。
    """
    key = (provider or "").strip().lower()
    if key == "anthropic":
        return _to_anthropic(payload, stream=stream)
    if key == "gemini":
        return _to_gemini(payload)
    raise UnsupportedUpstream(
        f"没有 `{provider}` 的请求翻译实现（可用：{' / '.join(SUPPORTED_UPSTREAMS)}）"
    )


# ── 协议翻译：上游响应 → OpenAI 响应 ────────────────────────────────────────

#: Anthropic / Gemini 的结束原因 → OpenAI 的 `finish_reason`。
_ANTHROPIC_FINISH: dict[str, str] = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}
_GEMINI_FINISH: dict[str, str] = {
    "STOP": "stop",
    "MAX_TOKENS": "length",
    "SAFETY": "content_filter",
    "RECITATION": "content_filter",
    "BLOCKLIST": "content_filter",
    "PROHIBITED_CONTENT": "content_filter",
}


def _openai_usage(prompt_tokens: int, completion_tokens: int) -> dict[str, int]:
    """`usage` 三个字段一起给：OpenAI 客户端普遍会读 `total_tokens`，缺了就是 0。"""
    return {
        "prompt_tokens": int(prompt_tokens),
        "completion_tokens": int(completion_tokens),
        "total_tokens": int(prompt_tokens) + int(completion_tokens),
    }


def _completion(
    *,
    response_id: str,
    model: str,
    created: int,
    content: str,
    tool_calls: list[dict[str, Any]],
    finish_reason: str,
    usage: dict[str, int],
) -> dict[str, Any]:
    """拼一个 OpenAI `chat.completion`。两条协议最终都落到这里。"""
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": response_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage,
    }


def _from_anthropic(response: dict[str, Any], model: str, created: int) -> dict[str, Any]:
    blocks = response.get("content")
    if not isinstance(blocks, list):
        raise ProxyError(f"Anthropic 响应里没有 content 块：{str(response)[:200]}")
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(str(block.get("text", "")))
        elif block.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": str(block.get("id", "")),
                    "type": "function",
                    "function": {
                        "name": str(block.get("name", "")),
                        "arguments": json.dumps(block.get("input", {}), ensure_ascii=False),
                    },
                }
            )
    raw_usage = response.get("usage") or {}
    usage = _openai_usage(
        int(raw_usage.get("input_tokens", 0) or 0),
        int(raw_usage.get("output_tokens", 0) or 0),
    )
    return _completion(
        response_id=str(response.get("id", "")),
        model=str(response.get("model", model)),
        created=created,
        content="".join(texts),
        tool_calls=tool_calls,
        finish_reason=_ANTHROPIC_FINISH.get(str(response.get("stop_reason", "")), "stop"),
        usage=usage,
    )


def _from_gemini(response: dict[str, Any], model: str, created: int) -> dict[str, Any]:
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates:
        blocked = response.get("promptFeedback") or {}
        raise ProxyError(f"Gemini 没有返回候选：{str(blocked or response)[:200]}")
    first = candidates[0] if isinstance(candidates[0], dict) else {}
    parts = (first.get("content") or {}).get("parts") or []
    texts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        if not isinstance(part, dict):
            continue
        if "text" in part:
            texts.append(str(part.get("text", "")))
        elif isinstance(part.get("functionCall"), dict):
            call = part["functionCall"]
            tool_calls.append(
                {
                    # Gemini 不给调用 id：合成一个稳定的（名字 + 序号），
                    # 否则客户端回传 tool 结果时对不上是哪一次调用。
                    "id": f"call_{call.get('name', 'tool')}_{index}",
                    "type": "function",
                    "function": {
                        "name": str(call.get("name", "")),
                        "arguments": json.dumps(call.get("args", {}), ensure_ascii=False),
                    },
                }
            )
    raw_usage = response.get("usageMetadata") or {}
    usage = _openai_usage(
        int(raw_usage.get("promptTokenCount", 0) or 0),
        int(raw_usage.get("candidatesTokenCount", 0) or 0),
    )
    return _completion(
        response_id=str(response.get("responseId", "") or f"chatcmpl-gemini-{created}"),
        model=str(response.get("modelVersion", model)),
        created=created,
        content="".join(texts),
        tool_calls=tool_calls,
        finish_reason=_GEMINI_FINISH.get(str(first.get("finishReason", "STOP")), "stop"),
        usage=usage,
    )


def provider_to_openai(
    response: dict[str, Any], provider: str, *, model: str = "", created: int | None = None
) -> dict[str, Any]:
    """**纯函数**：上游响应体 → OpenAI `chat.completion`。

    三块都要保住：
    - `content`：文本块拼起来；
    - `tool_calls`：`tool_use` / `functionCall` → `[{id,type,function:{name,arguments}}]`，
      `arguments` 是 **JSON 字符串**（OpenAI 的形状）；
    - `usage`：`input_tokens` / `output_tokens`、`promptTokenCount` /
      `candidatesTokenCount` → `prompt_tokens` / `completion_tokens` / `total_tokens`。
      丢了 usage，客户端就永远显示 0 token。
    """
    key = (provider or "").strip().lower()
    stamp = int(time.time()) if created is None else int(created)
    if key == "anthropic":
        return _from_anthropic(response, model, stamp)
    if key == "gemini":
        return _from_gemini(response, model, stamp)
    raise UnsupportedUpstream(
        f"没有 `{provider}` 的响应翻译实现（可用：{' / '.join(SUPPORTED_UPSTREAMS)}）"
    )


# ── SSE 流式翻译（MATRIX 4.7）────────────────────────────────────────────────
#
# 流式这一层最容易做错的地方是"以为事件流是统一形状"。它不是：
#
# * Anthropic：`message_start` → 若干 `content_block_start` / `content_block_delta`
#   → `message_delta`（带 stop_reason 与 output_tokens）→ `message_stop`，每个事件
#   一行 `event:` + 一行 `data:`；
# * Gemini（`?alt=sse`）：每个 `data:` 就是一份完整的 `GenerateContentResponse`
#   增量，文本在 `candidates[0].content.parts[].text`，结束在 `finishReason`。
#
# 所以翻译是**逐事件**做的，而不是"把上游的 chunk 直接转 JSON"。OpenAI 客户端要的是
# `chat.completion.chunk`：第一帧给 `delta.role`，中间帧给 `delta.content`，最后一帧
# 给 `finish_reason`，然后 `data: [DONE]`。


def sse_frame(payload: Mapping[str, Any] | str) -> bytes:
    """一帧 OpenAI SSE：``data: <json>\\n\\n``。

    ``\\n\\n`` 是分隔符不是排版：少一个换行，客户端会把两帧连成一个坏 JSON。
    """
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return f"data: {text}\n\n".encode()


#: 流结束标记。OpenAI 的客户端（含各家 SDK）都等这一行才肯收尾。
SSE_DONE: bytes = sse_frame("[DONE]")


def openai_chunk(
    *,
    response_id: str,
    model: str,
    created: int,
    delta: dict[str, Any],
    finish_reason: str | None = None,
    usage: dict[str, int] | None = None,
) -> dict[str, Any]:
    """一个 `chat.completion.chunk`。字段形状照 OpenAI 的流式响应。"""
    chunk: dict[str, Any] = {
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if usage is not None:
        chunk["usage"] = usage
    return chunk


class StreamTranslator:
    """把上游 SSE 逐行翻成 OpenAI chunk 的**状态机**（每个请求一个实例）。

    为什么要有状态：`finish_reason` 与 `usage` 在上游是**另外的事件**（Anthropic 的
    `message_delta`、Gemini 的 `usageMetadata`），只有攒到流的末尾才能给出终止帧；
    而 `role` 只在第一帧给一次。

    **只翻文本增量**：Anthropic 的 `thinking_delta` / `input_json_delta` 与 Gemini 的
    `functionCall` 增量都不转（模块 docstring 的"没做"里写着）。宁可少给，也不要把
    工具调用的半截 JSON 当正文吐给客户端——那会让客户端把参数拼进回答里。
    """

    def __init__(
        self,
        provider: str,
        *,
        model: str,
        response_id: str = "",
        created: int | None = None,
    ) -> None:
        self.provider = (provider or "").strip().lower()
        self.model = model
        self.created = int(time.time()) if created is None else int(created)
        self.response_id = response_id or f"chatcmpl-proxy-{self.created}"
        self.started = False
        self.finished = False
        self.stop_reason = ""
        self._usage: dict[str, int] | None = None
        self._input_tokens = 0
        self._output_tokens = 0

    # -- 内部 -------------------------------------------------------------
    def _role_chunk(self) -> dict[str, Any]:
        self.started = True
        return openai_chunk(
            response_id=self.response_id,
            model=self.model,
            created=self.created,
            delta={"role": "assistant", "content": ""},
        )

    def _text_chunk(self, text: str) -> list[dict[str, Any]]:
        """一段文本增量。**第一段之前补一帧 role**：上游没给 `message_start` 也可能
        直接给增量（Gemini 就是这种），而 OpenAI 的客户端普遍要求流的开头是
        `delta.role`，否则会把第一段文本当成"元数据"丢掉。"""
        chunks: list[dict[str, Any]] = []
        if not self.started:
            chunks.append(self._role_chunk())
        chunks.append(
            openai_chunk(
                response_id=self.response_id,
                model=self.model,
                created=self.created,
                delta={"content": text},
            )
        )
        return chunks

    def _usage_dict(self) -> dict[str, int] | None:
        if self._usage is not None:
            return self._usage
        if not (self._input_tokens or self._output_tokens):
            return None
        return _openai_usage(self._input_tokens, self._output_tokens)

    # -- 对外 -------------------------------------------------------------
    def feed(self, line: str) -> list[dict[str, Any]]:
        """吃一行上游 SSE，吐 0..N 个 OpenAI chunk。"""
        text = (line or "").strip()
        if not text or text.startswith("event:") or text.startswith(":"):
            return []
        if not text.startswith("data:"):
            return []
        raw = text[len("data:") :].strip()
        if not raw or raw == "[DONE]":
            return []
        try:
            event = json.loads(raw)
        except ValueError:
            # 上游偶尔会塞注释行或半截 JSON（代理链断开时）。跳过而不是抛错：
            # 抛错会让已经吐出去的半段内容白费。
            return []
        if not isinstance(event, dict):
            return []
        if self.provider == "anthropic":
            return self._feed_anthropic(event)
        if self.provider == "gemini":
            return self._feed_gemini(event)
        raise UnsupportedUpstream(f"没有 `{self.provider}` 的流式翻译实现")

    def _feed_anthropic(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        kind = str(event.get("type", ""))
        if kind == "error":
            raise ProxyError(f"Anthropic 流式返回错误：{event.get('error', event)}")
        if kind == "message_start":
            message = event.get("message")
            if isinstance(message, dict):
                self.response_id = str(message.get("id") or self.response_id)
                self.model = str(message.get("model") or self.model)
                usage = message.get("usage")
                if isinstance(usage, dict):
                    self._input_tokens = int(usage.get("input_tokens", 0) or 0)
            return [] if self.started else [self._role_chunk()]
        if kind == "content_block_delta":
            delta = event.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                return self._text_chunk(str(delta.get("text", "")))
            return []
        if kind == "message_delta":
            delta = event.get("delta")
            if isinstance(delta, dict) and delta.get("stop_reason"):
                self.stop_reason = str(delta["stop_reason"])
            usage = event.get("usage")
            if isinstance(usage, dict):
                self._output_tokens = int(usage.get("output_tokens", 0) or 0)
            return []
        if kind == "message_stop":
            self.finished = True
        return []

    def _feed_gemini(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        usage = event.get("usageMetadata")
        if isinstance(usage, dict):
            self._input_tokens = int(usage.get("promptTokenCount", 0) or 0)
            self._output_tokens = int(usage.get("candidatesTokenCount", 0) or 0)
        candidates = event.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            return chunks
        first = candidates[0] if isinstance(candidates[0], dict) else {}
        parts = (first.get("content") or {}).get("parts") or []
        for part in parts:
            if isinstance(part, dict) and "text" in part:
                chunks.extend(self._text_chunk(str(part.get("text", ""))))
        reason = str(first.get("finishReason", "") or "")
        if reason:
            self.stop_reason = reason
            self.finished = True
        return chunks

    def close(self) -> list[dict[str, Any]]:
        """收尾：补 role（若上游一个字都没给）+ 终止帧（带 finish_reason 与 usage）。"""
        chunks: list[dict[str, Any]] = []
        if not self.started:
            chunks.append(self._role_chunk())
        finish = (
            _ANTHROPIC_FINISH.get(self.stop_reason, "stop")
            if self.provider == "anthropic"
            else _GEMINI_FINISH.get(self.stop_reason, "stop")
        )
        chunks.append(
            openai_chunk(
                response_id=self.response_id,
                model=self.model,
                created=self.created,
                delta={},
                finish_reason=finish,
                usage=self._usage_dict(),
            )
        )
        self.finished = True
        return chunks


# ── 服务 ────────────────────────────────────────────────────────────────────

#: 上游转发函数：`(url, headers, body) -> 解析后的 JSON`。
#:
#: 做成可注入的回调，是为了让 `ProxyServer.handle()` **能离线测**：测试塞一个假
#: 转发函数，就得到"请求翻译 → 上游 → 响应翻译"的完整链路，一个字节都不上网。
Forward = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]

#: 流式转发函数：`(url, headers, body) -> 上游 SSE 的逐行迭代器`（每项是一行文本，
#: 不含换行符）。同样是注入点：测试给一个假的事件序列，就得到完整的流式翻译链路。
#:
#: 注意它是**生成器函数**：真正建连接发生在第一次迭代时，所以"连接失败"表现为
#: 迭代时抛异常——`handle_stream` 的重试正是围绕这一点设计的。
StreamForward = Callable[[str, dict[str, str], dict[str, Any]], Iterable[bytes | str]]


def auth_headers(plan: ProxyPlan, api_key: str = "", *, token: str = "") -> dict[str, str]:
    """上游鉴权头，**按 `plan.auth_mode` 选**（MATRIX 4.7）。

    `api_key` 模式（默认）：两家不是同一套——Anthropic 用 `x-api-key` +
    `anthropic-version`，Gemini 用 `x-goog-api-key`（与 `providers.py` 的两个客户端逐字
    一致）。

    `oauth` 模式：上游要的是**用户授权拿到的令牌**，不是厂商 API key，所以两家都用
    `Authorization: Bearer <令牌>`（Anthropic 那一侧还要 `anthropic-version`，否则请求
    形状不完整）。注意这里**只是把令牌放进头**——"OAuth 令牌真能被这两家接受"这件事
    本机验不了（无网络、无可用 OAuth 客户端），`describe()` 里如实标成"代码在、没跑通"。

    凭据只在请求头里用一次，不进 `ProxyPlan`、不进日志：计划对象会被 `to_dict()`
    序列化，凭据一旦进去就会被打进日志和 `--json` 输出。
    """
    if plan.auth_mode == OAUTH:
        if not token:
            return {}
        headers = {"authorization": f"Bearer {token}"}
        if plan.upstream_provider == "anthropic":
            headers["anthropic-version"] = "2023-06-01"
        return headers
    if not api_key:
        return {}
    if plan.upstream_provider == "anthropic":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    return {"x-goog-api-key": api_key}


def http_forward(plan: ProxyPlan, *, api_key: str = "", timeout: float = 60.0) -> Forward:
    """造一个真的走 httpx 的转发函数（整包，非流式）。

    非 2xx **抛 :class:`ProxyUpstreamError`（带状态码）**而不是 `raise_for_status()`：
    重试判定要读状态码，而 `HTTPStatusError` 的消息里带着完整 URL 与响应头——那既不
    适合回给客户端，也不适合做判定。

    这里没有做"离线降级"：没有网络时 httpx 会抛 `ConnectError`，代理把它转成
    OpenAI 形状的错误体回给客户端。假装成功比报错危险得多。
    """

    def forward(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        # **闸门放在真正发请求的那一步**（第 56 轮）：不管 `url` 最终来自
        # `ProxyPlan.upstream`（配置面）还是别处，这里都是执行面——
        # **配置面可能被绕过（直接构造 plan），执行面不会。**
        _refuse_cloud_metadata(url)

        merged = {**auth_headers(plan, api_key), "content-type": "application/json", **headers}
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=merged, json=body)
            if response.status_code >= 400:
                raise ProxyUpstreamError(
                    response.status_code,
                    f"上游 {plan.upstream_provider} 返回 HTTP {response.status_code}："
                    f"{response.text[:400]}",
                )
            data = response.json()
        if not isinstance(data, dict):
            raise ProxyError(f"上游返回的不是 JSON 对象：{str(data)[:200]}")
        return data

    return forward


def http_forward_stream(
    plan: ProxyPlan, *, api_key: str = "", timeout: float = 60.0
) -> StreamForward:
    """造一个走 httpx 的**流式**转发函数（`text/event-stream` 逐行）。

    为什么用生成器：`client.stream()` 必须在**迭代期间**保持打开，否则连接会被立刻
    关掉、只剩已经读到的几行。所以这里 `yield from response.iter_lines()`，把生命周期
    交给迭代器本身（调用方 `handle_stream` 用 `finally` 保证关得掉）。
    """

    def forward(url: str, headers: dict[str, str], body: dict[str, Any]) -> Iterator[str]:
        import httpx

        # **闸门放在真正发请求的那一步**（第 56 轮）：不管 `url` 最终来自
        # `ProxyPlan.upstream`（配置面）还是别处，这里都是执行面——
        # **配置面可能被绕过（直接构造 plan），执行面不会。**
        _refuse_cloud_metadata(url)

        merged = {
            **auth_headers(plan, api_key),
            "content-type": "application/json",
            "accept": "text/event-stream",
            **headers,
        }
        client = httpx.Client(timeout=timeout)
        try:
            with client.stream("POST", url, headers=merged, json=body) as response:
                if response.status_code >= 400:
                    # 必须先把 body 读掉，否则连接没法复用/关闭。
                    response.read()
                    raise ProxyUpstreamError(
                        response.status_code,
                        f"上游 {plan.upstream_provider} 返回 HTTP {response.status_code}",
                    )
                yield from response.iter_lines()
        finally:
            client.close()

    return forward


class ProxyServer:
    """最小代理：翻译 + 转发 + 回翻。**测试只调 `handle()` / `handle_stream()`，不起服务**。

    `handle(payload)` 是纯函数（除了注入的 `forward` 会碰网络）：同样的输入 +
    同样的 `forward` 一定得到同样的输出，所以协议翻译的正确性可以在离线环境里
    被完整断言。

    三件横切能力都在这一层，且都能注入：

    * **重试**：:class:`RetryPolicy` + ``sleep=``（测试换成空函数，不必真等）；
    * **并发**：一个信号量（``concurrency=``），超了排队；`ThreadingHTTPServer`
      每请求一线程，没有这道闸时 20 个客户端就能把上游打到 429；
    * **鉴权**：``token=``，配合 :func:`ensure_bindable` 的"无 token 只能绑回环"。

    `auth_mode="oauth"` 时另外注入 ``token_provider=``（见 :func:`oauth_token_provider`）：
    每次请求现取一次令牌，取/刷的失败在 `handle()` 里变成可读的 OpenAI 错误体。
    """

    def __init__(
        self,
        plan: ProxyPlan,
        *,
        forward: Forward | None = None,
        stream_forward: StreamForward | None = None,
        headers: dict[str, str] | None = None,
        retry: RetryPolicy | None = None,
        concurrency: int = DEFAULT_CONCURRENCY,
        token: str = "",
        token_provider: Callable[[], str] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.plan = plan
        self._forward = forward
        self._stream_forward = stream_forward
        if self._stream_forward is None and forward is not None:
            # **真实转发在场时自动补一个流式转发**：`orbit proxy serve` 只注入了
            # `forward=`，不补这一步的话 `stream: true` 会从"降级成整包"变成"回一帧
            # no_stream_upstream 错误"——那是退步。鉴权头由 `_headers()` 带过去
            # （里面已经有启动方注入的 x-api-key / x-goog-api-key）。
            self._stream_forward = http_forward_stream(plan)
        # 鉴权头由启动方（`orbit proxy serve`）注入，本类不自己读 keyring：
        # 代理对象会被 CLI 打印/序列化，凭据不该躺在它的属性里。
        self._extra_headers = dict(headers or {})
        #: OAuth 模式下"给我一个能用的令牌"的函数。**注入的是函数而不是令牌**：
        #: 长驻进程里令牌会过期，把值存成属性等于让它一路用下去直到 401。
        self._token_provider = token_provider
        self.retry = retry or RetryPolicy()
        self.concurrency = max(1, int(concurrency))
        #: 客户端要带的 bearer token（空 = 不鉴权，于是只能绑回环）。
        self.token = (token or "").strip()
        self._sleep = sleep or time.sleep
        self._slots = threading.Semaphore(self.concurrency)
        #: 最近一次 `handle()` / `handle_stream()` 的尝试记录（供诊断与断言）。
        #: **不进响应体的那部分**在 `handle()` 里另有一份（`orbit_attempts`）。
        self.last_attempts: list[Attempt] = []

    # -- 翻译 -------------------------------------------------------------
    def translate_request(
        self, payload: dict[str, Any], *, stream: bool = False
    ) -> dict[str, Any]:
        """OpenAI 请求 → 上游请求（顺手把模型名按 `model_map` 换掉）。"""
        translated = openai_to_provider(payload, self.plan.upstream_provider, stream=stream)
        if "model" in translated:
            translated["model"] = self.plan.resolve_model(str(payload.get("model", "")))
        return translated

    def translate_response(
        self, response: dict[str, Any], *, model: str = ""
    ) -> dict[str, Any]:
        return provider_to_openai(response, self.plan.upstream_provider, model=model)

    # -- 重试 -------------------------------------------------------------
    def _run_with_retry(self, call: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], list[Attempt]]:
        """跑一次上游调用，按 :class:`RetryPolicy` 重试。返回 ``(结果或抛出的异常, 尝试记录)``。

        **只重试 5xx / 连接错误 / 超时**（判定见 :func:`is_retriable`）。4xx 立刻失败：
        请求本身有问题，再发一次只会把同一个错误要回来，还可能触发上游的滥用检测。
        """
        attempts: list[Attempt] = []
        last: BaseException | None = None
        for index in range(1, self.retry.max_attempts + 1):
            try:
                data = call()
            except Exception as exc:  # noqa: BLE001 —— 分类留给 is_retriable
                status = getattr(exc, "status", 0)
                retriable = is_retriable(exc)
                delay = self.retry.delay_for(index) if retriable else 0.0
                attempts.append(
                    Attempt(
                        index=index,
                        ok=False,
                        status=int(status or 0),
                        error=f"{type(exc).__name__}: {exc}",
                        retriable=retriable,
                        delay=delay,
                    )
                )
                # 每次尝试都**立刻**记到实例上：失败路径也要能在响应里留下痕迹，
                # 否则"重试了几次"只在成功时可见——恰好是最不需要它的场景。
                self.last_attempts = attempts
                last = exc
                if not retriable or index >= self.retry.max_attempts:
                    break
                self._sleep(delay)
                continue
            attempts.append(Attempt(index=index, ok=True, status=200))
            self.last_attempts = attempts
            return data, attempts
        assert last is not None
        raise last

    # -- 请求处理（纯函数）------------------------------------------------
    def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        """处理一次 `/v1/chat/completions`（**非流式**）。不抛异常给调用方：错误也是 JSON。

        没有注入 `forward` 时返回一个 OpenAI 形状的错误体并说明原因——**不伪造一个
        空回答**。代理层最常见的坑就是"上游没通但客户端看到 200 + 空字符串"。

        每次上游尝试都记在返回值的 ``orbit_attempts`` 里（OpenAI 的客户端会忽略未知
        顶层字段）。为什么不塞进 `choices`：那个形状被 SDK 严格解析，塞进去等于给
        每个客户端埋一个反序列化错误。
        """
        if not isinstance(payload, dict):
            return _error("请求体必须是 JSON 对象", code="bad_request")
        if self._forward is None:
            return _error(
                "代理没有配置上游转发函数（`forward=`）：本层只做协议翻译，真实转发需要"
                "注入 forward。用 `orbit proxy serve` 启动时会自动接 httpx 转发。",
                code="no_upstream",
            )
        self.last_attempts = []
        try:
            body = self.translate_request(payload)
            url = self._endpoint(str(payload.get("model", "")))
            headers = self._headers()
            with self._slots:  # 超了并发就排队（不拒绝）
                data, attempts = self._run_with_retry(
                    lambda: self._forward_call(url, headers, body)
                )
            self.last_attempts = attempts
            result = self.translate_response(data, model=str(payload.get("model", "")))
            result["orbit_attempts"] = [item.as_dict() for item in attempts]
            return result
        except UnsupportedUpstream as exc:
            return self._with_attempts(_error(str(exc), code="unsupported_upstream"))
        except ProxyUpstreamError as exc:
            # 上游明确回了 4xx/5xx：这是"上游的问题"，不是"翻译的问题"，所以 code 是
            # `upstream_error`（客户端据此决定要不要退避/换模型）。
            return self._with_attempts(
                _error(f"上游返回 HTTP {exc.status}：{exc}", code="upstream_error")
            )
        except ProxyError as exc:
            return self._with_attempts(_error(str(exc), code="proxy_error"))
        except oauth_flow.NetworkUnavailable as exc:
            # OAuth 令牌过期且刷新要出网。单独一类 code：客户端看到 `oauth_needs_network`
            # 就知道"这不是上游挂了，是本机没网/要重新授权"，而不是去退避重试。
            return self._with_attempts(_error(str(exc), code="oauth_needs_network"))
        except Exception as exc:  # noqa: BLE001 —— 上游异常统一转成可读错误体
            return self._with_attempts(_error(f"上游请求失败：{exc}", code="upstream_error"))

    def _forward_call(
        self, url: str, headers: dict[str, str], body: dict[str, Any]
    ) -> dict[str, Any]:
        """包一层，把"没注入 forward"变成可预期的 :class:`ProxyError`（而不是 TypeError）。"""
        forward = self._forward
        if forward is None:  # pragma: no cover - handle() 已挡在前面
            raise ProxyError("没有配置上游转发函数")
        return forward(url, headers, body)

    def _with_attempts(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.last_attempts:
            payload["orbit_attempts"] = [item.as_dict() for item in self.last_attempts]
        return payload

    def handle_stream(self, payload: dict[str, Any]) -> Iterator[bytes]:
        """处理一次**流式** `/v1/chat/completions`，产出 OpenAI 形状的 SSE 帧。

        与 `handle()` 的三点不同：

        1. 请求体翻译时 ``stream=True``（Anthropic 要显式要流、Gemini 走
           `streamGenerateContent?alt=sse`）；
        2. **重试只在"还没吐过一个字节"时发生**——一旦已经 `yield` 了内容，重试会让
           客户端收到两段拼接的文本（前一段没有终止帧），那比直接报错更难查；
        3. 出错时也是一帧 `data: {"error": ...}`，而不是 HTTP 状态码：此时响应头早就
           发出去了，改状态码已经来不及。
        """
        if not isinstance(payload, dict):
            yield sse_frame(_error("请求体必须是 JSON 对象", code="bad_request"))
            yield SSE_DONE
            return
        if self._stream_forward is None:
            yield sse_frame(
                _error(
                    "代理没有配置流式转发函数（`stream_forward=`）：流式请求不能降级成整包，"
                    "否则客户端会一直等一个永远不来的 chunk。",
                    code="no_stream_upstream",
                )
            )
            yield SSE_DONE
            return

        model = str(payload.get("model", ""))
        self.last_attempts = []
        attempts: list[Attempt] = []
        try:
            body = self.translate_request(payload, stream=True)
            url = self._stream_endpoint(model)
        except ProxyError as exc:
            yield sse_frame(_error(str(exc), code="proxy_error"))
            yield SSE_DONE
            return

        with self._slots:  # 流式请求同样排队；信号量持有到流结束
            for index in range(1, self.retry.max_attempts + 1):
                translator = StreamTranslator(
                    self.plan.upstream_provider,
                    model=self.plan.resolve_model(model) or model,
                )
                emitted = False
                try:
                    stream = self._stream_forward(url, self._headers(), body)
                    for line in stream:
                        text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
                        for chunk in translator.feed(text):
                            emitted = True
                            yield sse_frame(chunk)
                    for chunk in translator.close():
                        yield sse_frame(chunk)
                    yield SSE_DONE
                    attempts.append(Attempt(index=index, ok=True, status=200))
                    self.last_attempts = attempts
                    return
                except Exception as exc:  # noqa: BLE001 —— 分类留给 is_retriable
                    retriable = is_retriable(exc) and not emitted
                    delay = self.retry.delay_for(index) if retriable else 0.0
                    attempts.append(
                        Attempt(
                            index=index,
                            ok=False,
                            status=int(getattr(exc, "status", 0) or 0),
                            error=f"{type(exc).__name__}: {exc}",
                            retriable=retriable,
                            delay=delay,
                        )
                    )
                    self.last_attempts = attempts
                    if retriable and index < self.retry.max_attempts:
                        self._sleep(delay)
                        continue
                    note = "（已开始输出，不再重试）" if emitted else ""
                    # 协议层面的错（上游在流里回了 `type: error`）算 `proxy_error`，
                    # 传输层面的错算 `upstream_error`：客户端对这两类的处置不同
                    # （前者多半要换模型，后者退避重试就行）。
                    code = "proxy_error" if isinstance(exc, ProxyError) else "upstream_error"
                    yield sse_frame(
                        _error(f"上游流式失败：{exc}{note}", code=code)
                    )
                    yield SSE_DONE
                    return

    def _endpoint(self, model: str) -> str:
        """上游端点（整包）。Anthropic 的模型在**请求体**里，Gemini 的模型在**URL** 里。"""
        base = self.plan.base_url.rstrip("/")
        if self.plan.upstream_provider == "anthropic":
            return f"{base}/v1/messages"
        target = self.plan.resolve_model(model)
        if not target:
            raise ProxyError(
                "Gemini 上游需要模型名：客户端请求里没有 `model`，`model_map` 也没配。"
            )
        return f"{base}/models/{target}:generateContent"

    def _stream_endpoint(self, model: str) -> str:
        """上游端点（流式）。Gemini 的流式是**另一个方法名 + `alt=sse`**。"""
        base = self.plan.base_url.rstrip("/")
        if self.plan.upstream_provider == "anthropic":
            return f"{base}/v1/messages"
        target = self.plan.resolve_model(model)
        if not target:
            raise ProxyError(
                "Gemini 上游需要模型名：客户端请求里没有 `model`，`model_map` 也没配。"
            )
        return f"{base}/models/{target}:streamGenerateContent?alt=sse"

    def _headers(self) -> dict[str, str]:
        """这次请求要带的上游头。

        `oauth` 模式下**每次请求现取一次令牌**（`token_provider()`）：取不到就抛，让
        `handle()` 把它变成可读错误体——静默退回 `api_key` 模式会让请求带着错误的鉴权
        头发出去，而上游只会回一句 401。
        """
        headers = {"content-type": "application/json", **self._extra_headers}
        if self.plan.auth_mode == OAUTH:
            if self._token_provider is None:
                raise OAuthTokenError(
                    "auth_mode=oauth 但没有注入 token_provider：本层不自己去 keyring 取令牌，"
                    "由启动方用 `oauth_token_provider(config)` 造一个传进来。"
                )
            headers.update(auth_headers(self.plan, token=self._token_provider()))
        return headers

    def authorize(self, header_value: str) -> bool:
        """校验客户端的 `Authorization` 头。没配 token 时**一律放行**（只绑回环）。"""
        if not self.token:
            return True
        value = (header_value or "").strip()
        if not value.lower().startswith("bearer "):
            return False
        return value[len("bearer ") :].strip() == self.token

    def models(self) -> list[dict[str, Any]]:
        """`GET /v1/models` 的返回。**只有映射表里的名字**——不列出上游的全部模型，
        因为本层没有联网拉清单的能力（`capabilities.refresh_cache()` 会明确报错）。"""
        return [
            {"id": name, "object": "model", "owned_by": self.plan.upstream_provider}
            for name in sorted(self.plan.model_map)
        ]

    def describe(self) -> str:
        """计划 + **本层没做到的事**。用户拿到的必须是完整交代，不是一句"已就绪"。"""
        auth = (
            f"bearer token（客户端带 `Authorization: Bearer <{PROXY_CONFIG_NAME} 里的 token>`）"
            if self.token
            else "无鉴权（因此只允许绑回环地址）"
        )
        upstream_auth = (
            "OAuth 令牌（`Authorization: Bearer <令牌>`，令牌从 keyring 取、过期刷新）"
            if self.plan.auth_mode == OAUTH
            else "API key（`x-api-key` / `x-goog-api-key`）"
        )
        lines = [
            self.plan.describe(),
            "能力（已做到）：",
            "  · 流式：SSE 逐事件翻成 OpenAI `data:` 帧，`stream: true` 不再降级；",
            f"  · 重试：仅 5xx / 连接错误 / 超时，最多 {self.retry.max_attempts} 次，"
            "4xx 立刻失败；每次尝试记在响应的 `orbit_attempts` 里；",
            f"  · 并发：信号量 {self.concurrency} 路，超了排队而不是拒绝；",
            f"  · 客户端鉴权：{auth}；",
            f"  · 上游鉴权：{upstream_auth}。",
            "边界（如实说明）：",
            "  · 流式只翻**文本增量**：thinking / tool_use 增量不转（整包那条路是完整的）；",
            "  · 重试不覆盖 429（限流重试只会更糟），也不做 jitter；",
            "  · 只有 /v1/chat/completions 与 /v1/models，没有 responses / embeddings。",
        ]
        if self.plan.auth_mode == OAUTH:
            lines.extend(
                [
                    "OAuth 模式的分项交代（哪些真、哪些只是「代码在」）：",
                    "  · 真：从 keyring 取令牌、判过期（纯本地）、按 auth_mode 选头；",
                    "  · 真：刷新逻辑的拼装与回写（`mcp_oauth_flow.refresh_token`）；",
                    "  · **没跑通**：刷新那次 POST 要出网，本机无网络，默认实现如实抛"
                    " `NetworkUnavailable`；",
                    "  · **没跑通**：OAuth 令牌能否被 Anthropic / Gemini 上游接受，"
                    "一次都没验证过（没有可用 OAuth 客户端凭据、没有网络）。",
                ]
            )
        return "\n".join(lines)

    # -- 真起服务（测试不调）----------------------------------------------
    def serve(self) -> None:  # pragma: no cover - 阻塞式服务，单测不启动
        """用 `http.server` 起最小 HTTP 服务，**阻塞**直到 Ctrl-C。

        为什么不是 aiohttp / uvicorn：那两条路要么没装、要么会把整个 FastAPI 应用
        拉起来。本层只需要"收一个 JSON、回一个 JSON"，标准库够用，也少一层依赖。

        **启动前先过 :func:`ensure_bindable`**：没有 token 时绑非回环地址会直接抛
        `ProxyError`，而不是"启动成功然后被网段里任何人白用"。
        """
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        host = ensure_bindable(self.plan.listen_host, self.token)
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _write(self, status: int, payload: dict[str, Any]) -> None:
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authorized(self) -> bool:
                if server.authorize(self.headers.get("authorization", "")):
                    return True
                self._write(
                    401,
                    _error(
                        "缺少或错误的 bearer token：客户端要带 "
                        f"`Authorization: Bearer <{PROXY_CONFIG_NAME} 里的 token>`",
                        code="unauthorized",
                    ),
                )
                return False

            def _stream(self, payload: dict[str, Any]) -> None:
                """SSE 响应：**不写 content-length**（长度只有吐完才知道），改用 chunked。"""
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.send_header("cache-control", "no-cache")
                self.send_header("connection", "keep-alive")
                self.send_header("transfer-encoding", "chunked")
                self.end_headers()
                try:
                    for frame in server.handle_stream(payload):
                        self.wfile.write(b"%X\r\n%s\r\n" % (len(frame), frame))
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return  # 客户端先走了：不是错误，别再往断掉的 socket 写

            def do_GET(self) -> None:  # noqa: N802 —— 标准库规定的名字
                if not self._authorized():
                    return
                if self.path.rstrip("/").endswith("/v1/models"):
                    self._write(200, {"object": "list", "data": server.models()})
                    return
                self._write(404, _error(f"未知路径：{self.path}", code="not_found"))

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                length = int(self.headers.get("content-length", "0") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    self._write(400, _error("请求体不是合法 JSON", code="bad_request"))
                    return
                if isinstance(payload, dict) and payload.get("stream"):
                    self._stream(payload)
                    return
                result = server.handle(payload)
                status = 200 if "error" not in result else 502
                self._write(status, result)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                # 静默默认日志（它会打印每个请求到 stderr）。要排障时由 CLI 打开。
                return

        httpd = ThreadingHTTPServer((host, self.plan.listen_port), Handler)
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()


def _error(message: str, *, code: str) -> dict[str, Any]:
    """OpenAI 形状的错误体。客户端（尤其 SDK）认这个结构。"""
    return {"error": {"message": message, "type": "proxy_error", "code": code}}


def describe() -> str:
    """这一层到底做了什么、没做什么。CLI 的 `proxy status` 直接打印它。

    每一条都同时给"做到"和"没做到"：只说做到会让人把它当生产网关（于是拿它挡公网），
    只说没做到会让人以为它没用（于是自己再写一个更糙的）。
    """
    return "\n".join(
        [
            "本地 OpenAI 兼容代理（把 anthropic / gemini 协议翻成 OpenAI 面）",
            f"可用上游：{' / '.join(SUPPORTED_UPSTREAMS)}",
            "翻译范围：chat/completions 的 messages、tools、usage 三块",
            "流式：SSE 逐事件翻成 OpenAI `data:` 帧（只翻文本增量，工具增量不转）",
            f"重试：仅 5xx / 连接错误 / 超时，最多 {DEFAULT_MAX_ATTEMPTS} 次；4xx 立刻失败",
            f"并发：信号量 {DEFAULT_CONCURRENCY} 路，超了排队而不是拒绝",
            f"客户端鉴权：可选 bearer token（<data>/{PROXY_CONFIG_NAME} 的 token）；"
            "**没有 token 时只允许绑回环地址**",
            f"上游鉴权：{' / '.join(AUTH_MODES)} 两种模式——`api_key` 走静态密钥头，"
            "`oauth` 走 `Authorization: Bearer <OAuth 令牌>`（令牌从 keyring 取、过期刷新）",
            "OAuth 模式的边界（如实）：取令牌 / 判过期 / 选头都是真的且有测试；"
            "**刷新要出网、本机没跑通**，令牌能否被上游接受也没验证过",
            "没做到：429 重试、流式工具调用增量、responses / embeddings 端点",
            "这是**最小转发层**：够让只认 OpenAI 协议的客户端跑起来，不是生产级网关。",
        ]
    )


__all__ = [
    "API_KEY",
    "AUTH_MODES",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_HOST",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_PORT",
    "LOOPBACK_HOSTS",
    "OAUTH",
    "PROXY_CONFIG_NAME",
    "RETRIABLE_STATUS",
    "SSE_DONE",
    "SUPPORTED_UPSTREAMS",
    "UPSTREAM_TRANSPORTS",
    "Attempt",
    "Forward",
    "OAuthTokenError",
    "ProxyAuthError",
    "ProxyError",
    "ProxyPlan",
    "ProxyServer",
    "ProxyUpstreamError",
    "RetryPolicy",
    "StreamForward",
    "StreamTranslator",
    "UnsupportedUpstream",
    "auth_headers",
    "config_path",
    "describe",
    "ensure_bindable",
    "http_forward",
    "http_forward_stream",
    "is_retriable",
    "load_token",
    "oauth_store",
    "oauth_token_provider",
    "openai_chunk",
    "openai_to_provider",
    "plan_for",
    "port_in_use",
    "provider_to_openai",
    "sse_frame",
]
