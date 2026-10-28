"""MCP OAuth 授权流程的**纯函数那一半**（矩阵 8.3 收尾）。

## 为什么只做一半，以及这一半为什么值得做

Hermes 的 ``mcp_oauth.py``（84 KB）+ ``mcp_oauth_manager.py`` 做的是一条完整的授权码
流程：读 RFC 9728 保护资源元数据 → 读 RFC 8414 授权服务器元数据 → 动态客户端注册
（RFC 7591）→ 生成 PKCE → **在本机开一个回环端口的回调服务器** → 打开浏览器 →
等重定向 → 拿 code 换 token → 存起来 → 过期了再刷新。

本机**无网络**、也没有可联调的 OAuth 服务。真把整条流程写出来，得到的是"一次都没跑过"
的代码：本地回调服务器起没起来、浏览器有没有被拉起、state 校验有没有生效，全都无法验证。
所以这里严格切一刀：

| 部分 | 状态 | 为什么 |
|---|---|---|
| PKCE（verifier / challenge） | **真实现** | 纯计算，RFC 7636 的形状可以断言 |
| authorize URL 拼装 | **真实现** | 纯字符串，参数集合可逐项核对 |
| token / refresh 请求体 | **真实现** | 纯 dict，字段来自 RFC 6749 与 MCP SDK |
| 令牌响应解析 | **真实现** | 纯函数，坏载荷可测 |
| 过期判定 | **真实现** | 纯函数，边界可测 |
| 令牌存储（keyring） | **真实现** | 不落盘明文，可测 |
| 起回调服务器 / 开浏览器 / 真换 token | **如实抛** :class:`NetworkUnavailable` | 见上 |

:func:`run_flow` 抛出的异常里写清了"缺哪几样东西"，而不是一句"未实现"——用户/下一个人
接着做的时候，需要知道断点在哪。

## PKCE 是真的（RFC 7636）

``code_verifier`` 用 ``secrets.token_urlsafe(64)``（86 个 URL 安全字符，落在 RFC 的
43..128 区间内），``code_challenge`` = ``base64url(sha256(verifier))`` 去掉 ``=`` 填充，
``code_challenge_method`` 恒为 ``S256``。mcp SDK 的 ``PKCEParameters.generate`` 也是这个
形状（它用 128 个 ``[A-Za-z0-9-._~]`` 字符），两者可互操作。

## 令牌落在哪

keyring，服务名 ``orbit:<data_dir>:mcp:<server>``，键 ``oauth-token``。
**不落盘明文**：token 进 ``data/`` 下的任何文件都等于进了备份、进了 git（很多人把
``data/`` 一起提交）、进了报错日志。

``app/mcp.py`` 里已有的 ``OAuthStore``（服务名 ``orbit:<data_dir>``、键 ``oauth:<server>``）
是 ``mcp auth set`` 的写入端，本轮不动它。于是**读**要两边都看（先本模块的键，再退到
``OAuthStore``），**写**要两边都写——否则 ``orbit mcp auth status`` 会对着一个刚刚登录
成功的服务报"未配置"。这个双写是刻意记录下来的技术债，注释在 :meth:`TokenStore.save`。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse

import keyring

log = logging.getLogger("orbit.mcp.oauth")

#: PKCE 的 ``code_verifier`` 长度上限（RFC 7636 §4.1：43..128）。
PKCE_MAX_LENGTH = 128
#: 生成 verifier 时取的随机字节数。``token_urlsafe(64)`` → 86 个字符，在合法区间内。
PKCE_BYTES = 64
#: 默认回调地址。**只用于"打印将要访问的 URL"**：真流程会先占一个空闲回环端口，
#: 再把这个地址换成实际端口（Hermes 的 ``_reserve_callback_port`` 就是干这个的）。
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/callback"
#: keyring 里 token 的键名（服务名里已经带了 server，键不再重复）。
TOKEN_KEY = "oauth-token"
#: 提前多少秒把 token 判成"即将过期"。与 ``app/mcp.py`` 的 ``OAUTH_EXPIRY_SKEW`` 同值：
#: 网络往返与系统时钟偏差都要留余量，否则会在请求刚发出时过期，报出来的却是 401。
DEFAULT_SKEW = 60.0


class NetworkUnavailable(RuntimeError):
    """需要出网/浏览器/本地监听才能继续的那一步。

    单独一个异常类型而不是 ``NotImplementedError``：调用方要能把它和"真 bug"分开——
    前者该原样打给用户（并给出手工通道），后者该冒栈。
    """


@dataclass(frozen=True)
class TokenPayload:
    """一份 OAuth 令牌（RFC 6749 §5.1 的响应 + 我们自己记的两个时刻）。"""

    access_token: str
    token_type: str = "Bearer"
    #: 服务给的相对有效期（秒）。``None`` = 服务没给。
    expires_in: float | None = None
    refresh_token: str = ""
    scope: str = ""
    #: 拿到令牌的时刻（epoch 秒）。
    obtained_at: float = 0.0
    #: 绝对到期时刻（epoch 秒）。``0`` = 未知，与"已过期"是两件事。
    expires_at: float = 0.0

    def as_dict(self) -> dict:
        """落 keyring 的 JSON 载荷。字段名与令牌响应同名，便于直接对账。"""
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "expires_in": self.expires_in,
            "refresh_token": self.refresh_token,
            "scope": self.scope,
            "obtained_at": self.obtained_at,
            "expires_at": self.expires_at,
        }

    @classmethod
    def from_dict(cls, payload: Any) -> TokenPayload | None:
        """从 JSON 载荷还原；坏载荷返回 ``None``（宁可报"未配置"也不要崩）。"""
        if not isinstance(payload, dict):
            return None
        token = str(payload.get("access_token") or "")
        if not token:
            return None
        return cls(
            access_token=token,
            token_type=str(payload.get("token_type") or "Bearer"),
            expires_in=_number(payload.get("expires_in")),
            refresh_token=str(payload.get("refresh_token") or ""),
            scope=str(payload.get("scope") or ""),
            obtained_at=_number(payload.get("obtained_at")) or 0.0,
            expires_at=_number(payload.get("expires_at")) or 0.0,
        )


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _number(value: Any) -> float | None:
    """宽松取数：服务常把 ``expires_in`` 当字符串返回（RFC 只要求是数字，现实不遵守）。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _get(config: Any, key: str, default: Any = "") -> Any:
    """从 dict 或对象里读一个字段（两种都支持，免得调用方为了调用它先转一层）。"""
    if isinstance(config, dict):
        value = config.get(key)
    else:
        value = getattr(config, key, None)
    return default if value is None else value


def _base_url(server_url: str) -> str:
    """取服务 URL 的 origin（抄 mcp SDK ``get_authorization_base_url``：去掉 path）。"""
    parsed = urlparse(server_url)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def resource_url(config: Any) -> str:
    """RFC 8707 的 ``resource`` 参数：服务 URL 的规范化形式（去 fragment、小写主机）。

    抄 mcp SDK 的 ``resource_url_from_server_url``。现代协议（2025-06-18 起）的 token
    与授权请求都要带它——服务靠它判断"这个令牌是发给哪个资源的"。
    """
    raw = str(_get(config, "url") or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return raw
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path}"


def authorization_endpoint(config: Any) -> str:
    """授权端点。配置里没写就从服务 origin 推 ``/authorize``（SDK 的兜底同款）。"""
    explicit = str(_get(config, "authorization_endpoint") or "").strip()
    if explicit:
        return explicit
    base = _base_url(str(_get(config, "url") or ""))
    return f"{base}/authorize" if base else ""


def token_endpoint(config: Any) -> str:
    """令牌端点。同上，兜底 ``/token``。"""
    explicit = str(_get(config, "token_endpoint") or "").strip()
    if explicit:
        return explicit
    base = _base_url(str(_get(config, "url") or ""))
    return f"{base}/token" if base else ""


def redirect_uri(config: Any) -> str:
    """回调地址。没配置时给一个**示例**回环地址（见 :data:`DEFAULT_REDIRECT_URI`）。"""
    explicit = str(_get(config, "redirect_uri") or "").strip()
    return explicit or DEFAULT_REDIRECT_URI


def scope_of(config: Any) -> str:
    """申请的 scope。``scopes``（列表）与 ``scope``（空格串）两种写法都认。"""
    raw = _get(config, "scope")
    if isinstance(raw, (list, tuple)):
        return " ".join(str(item) for item in raw if str(item).strip())
    text = str(raw or "").strip()
    if text:
        return text
    scopes = _get(config, "scopes")
    if isinstance(scopes, (list, tuple)):
        return " ".join(str(item) for item in scopes if str(item).strip())
    return ""


# ── PKCE（RFC 7636）──────────────────────────────────────────────────────────


def code_challenge_for(code_verifier: str) -> str:
    """``code_challenge = base64url(sha256(verifier))``，去掉 ``=`` 填充。

    RFC 7636 §4.2 要求的 S256 变换。``urlsafe_b64encode`` 已经给出 ``-`` / ``_`` 变体，
    但**填充必须去掉**：带 ``=`` 的 challenge 在部分授权服务器上会被当成非法字符。
    """
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def new_code_verifier() -> str:
    """生成一个 ``code_verifier``：86 个 URL 安全字符，落在 RFC 的 43..128 区间内。"""
    return secrets.token_urlsafe(PKCE_BYTES)


def new_state() -> str:
    """生成 ``state``（CSRF 用）。与 mcp SDK 同款：``secrets.token_urlsafe(32)``。"""
    return secrets.token_urlsafe(32)


def pkce_pair() -> tuple[str, str]:
    """一次生成 ``(code_verifier, code_challenge)``。"""
    verifier = new_code_verifier()
    return verifier, code_challenge_for(verifier)


# ── 请求体（纯函数）──────────────────────────────────────────────────────────


def build_authorize_url(config: Any, *, state: str, code_challenge: str) -> str:
    """拼授权 URL（用户浏览器要打开的那一条）。

    参数集合照 mcp SDK 的 ``_perform_authorization_code_grant``：``response_type=code``、
    ``client_id``、``redirect_uri``、``state``、``code_challenge``、
    ``code_challenge_method=S256``，再加 RFC 8707 的 ``resource``、可选的 ``scope``。
    ``scope`` 里含 ``offline_access`` 时补 ``prompt=consent``（OIDC 的硬要求，否则拿不到
    refresh token）。

    ``state`` / ``code_challenge`` 由调用方传进来而不是在这里生成：这两个值必须与
    "等一下校验回调"的那一侧共享，就地生成会让校验永远对不上。
    """
    endpoint = authorization_endpoint(config)
    if not endpoint:
        raise ValueError("MCP 服务没有 url，无法推出授权端点（请在配置里写 authorization_endpoint）")
    if not state:
        raise ValueError("state 不能为空：授权回调要靠它防 CSRF")
    if not code_challenge:
        raise ValueError("code_challenge 不能为空：本流程只支持 PKCE(S256)")
    client_id = str(_get(config, "client_id") or "").strip()
    if not client_id:
        raise ValueError(
            "缺少 client_id：真实流程要先做动态客户端注册（RFC 7591）拿到它，"
            "而注册需要出网。可以先把已知的 client_id 写进配置再调用。"
        )
    params: dict[str, str] = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri(config),
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    resource = resource_url(config)
    # RFC 8707：现代协议默认带；配置里显式关掉才不带（老服务会把它当未知参数报错）。
    if resource and bool(_get(config, "include_resource", True)):
        params["resource"] = resource
    scope = scope_of(config)
    if scope:
        params["scope"] = scope
        if "offline_access" in scope.split():
            params["prompt"] = "consent"
    return f"{endpoint}?{urlencode(params)}"


def exchange_payload(config: Any, code: str, code_verifier: str) -> dict:
    """换 token 的请求体（``grant_type=authorization_code``）。

    字段照 RFC 6749 §4.1.3 + PKCE：``code`` / ``redirect_uri`` / ``client_id`` /
    ``code_verifier``（+ RFC 8707 的 ``resource``）。**不**在这里加 ``client_secret``：
    公共客户端（MCP 的常态）没有 secret，加一个空值会被部分服务判成非法凭证。
    """
    if not str(code or "").strip():
        raise ValueError("授权码不能为空")
    if not str(code_verifier or "").strip():
        raise ValueError("code_verifier 不能为空：PKCE 换 token 必须带它")
    client_id = str(_get(config, "client_id") or "").strip()
    if not client_id:
        raise ValueError("缺少 client_id，无法拼换 token 请求")
    payload: dict[str, str] = {
        "grant_type": "authorization_code",
        "code": str(code),
        "redirect_uri": redirect_uri(config),
        "client_id": client_id,
        "code_verifier": str(code_verifier),
    }
    resource = resource_url(config)
    if resource and bool(_get(config, "include_resource", True)):
        payload["resource"] = resource
    return payload


def refresh_payload(config: Any, refresh_token: str) -> dict:
    """刷新令牌的请求体（``grant_type=refresh_token``）。

    照 mcp SDK 的 ``_refresh_token``：``refresh_token`` + ``client_id``（+ ``resource``）。
    刷新响应**可能**换发新的 refresh token，调用方必须用响应里的那个覆盖旧的
    （:func:`parse_token_response` 保留原值只在响应没给时才用）。
    """
    if not str(refresh_token or "").strip():
        raise ValueError("没有 refresh token，无法刷新（需要重新走一次授权）")
    client_id = str(_get(config, "client_id") or "").strip()
    if not client_id:
        raise ValueError("缺少 client_id，无法拼刷新请求")
    payload: dict[str, str] = {
        "grant_type": "refresh_token",
        "refresh_token": str(refresh_token),
        "client_id": client_id,
    }
    resource = resource_url(config)
    if resource and bool(_get(config, "include_resource", True)):
        payload["resource"] = resource
    return payload


def parse_token_response(payload: Any, *, now: float | None = None) -> TokenPayload:
    """解析令牌端点的响应。

    只做**结构**校验：没有 ``access_token`` 就是错（RFC 6749 §5.1 说它是 REQUIRED），
    其余字段宽松处理——``token_type`` 大小写不统一（规范写 ``Bearer``，有的服务回
    ``bearer``），``expires_in`` 常被当字符串返回。

    ``expires_at`` 在这里就算成**绝对时刻**：存相对值的话，进程重启后"还剩多少秒"就
    无从算起（``app/mcp.py`` 的 ``OAuthStore`` 是同一条理由）。
    """
    if not isinstance(payload, dict):
        raise ValueError(f"令牌响应不是对象：{type(payload).__name__}")
    if payload.get("error"):
        # RFC 6749 §5.2 的错误响应：把 error / error_description 原样带出去，
        # 否则用户只会看到一个"没有 access_token"的谜语。
        detail = str(payload.get("error_description") or "").strip()
        raise ValueError(
            f"令牌端点报错：{payload.get('error')}" + (f"（{detail}）" if detail else "")
        )
    access_token = str(payload.get("access_token") or "")
    if not access_token:
        raise ValueError("令牌响应里没有 access_token")
    moment = time.time() if now is None else now
    expires_in = _number(payload.get("expires_in"))
    return TokenPayload(
        access_token=access_token,
        token_type=str(payload.get("token_type") or "Bearer").title(),
        expires_in=expires_in,
        refresh_token=str(payload.get("refresh_token") or ""),
        scope=str(payload.get("scope") or ""),
        obtained_at=moment,
        expires_at=(moment + expires_in) if expires_in and expires_in > 0 else 0.0,
    )


def is_expired(token: Any, *, skew: float = DEFAULT_SKEW, now: float | None = None) -> bool:
    """令牌是否过期（``skew`` 是"即将过期"的提前量）。

    三种输入都认：:class:`TokenPayload`、``app/mcp.py`` 的 ``TokenRecord``、普通 dict。
    读的是 ``expires_at``（绝对时刻）优先，其次是 ``obtained_at + expires_in``。
    **到期时间未知 → False**：把"不知道"当成"已过期"，会让长效令牌每次都被判死、
    每轮都去刷新，而刷新往往还需要网络——等于把一个可用配置变成不可用。
    """
    moment = time.time() if now is None else now
    expires_at = _number(_get(token, "expires_at", None))
    if expires_at is not None and expires_at > 0:
        return (expires_at - moment) <= skew
    expires_in = _number(_get(token, "expires_in", None))
    if expires_in is not None and expires_in > 0:
        obtained = _number(_get(token, "obtained_at", None)) or 0.0
        return ((obtained + expires_in) - moment) <= skew
    return False


# ── 令牌存储（keyring，不落盘明文）──────────────────────────────────────────


class TokenStore:
    """一个 MCP 服务的 OAuth 令牌存储。

    服务名 ``orbit:<data_dir>:mcp:<server>``：把"哪个数据目录 + 哪个服务"编进服务名，
    于是同一台机器上两份 Orbit 数据目录、十几个 MCP 服务的令牌互不覆盖。
    """

    def __init__(self, data_dir: Path | str, server: str) -> None:
        self.data_dir = Path(data_dir)
        self.server = str(server)

    @classmethod
    def for_settings(cls, settings, server: str) -> TokenStore:
        """按 ``Settings`` 构造（服务名里的路径与 ``MCPManager.service`` 同源、已 resolve）。"""
        return cls(Path(settings.data_dir).resolve(), server)

    @property
    def service(self) -> str:
        return f"orbit:{self.data_dir}:mcp:{self.server}"

    def save(self, token: TokenPayload) -> TokenPayload:
        """写令牌。

        **双写**（见模块 docstring）：本模块的键是主存储，另外镜像一份进
        ``app/mcp.py`` 的 ``OAuthStore``，否则 ``orbit mcp auth status`` 会对着一个刚登录
        成功的服务报"未配置"——同一件事两个读端看到不同答案，是最难查的一类不一致。
        镜像失败只记 debug：主存储写成功就算成功。
        """
        keyring.set_password(self.service, TOKEN_KEY, json.dumps(token.as_dict()))
        try:
            from app.mcp import OAuthStore

            OAuthStore(f"orbit:{self.data_dir}").set(
                self.server,
                token.access_token,
                refresh_token=token.refresh_token,
                expires_in=token.expires_in or 0.0,
                token_type=token.token_type,
                scope=token.scope,
                now=token.obtained_at or None,
            )
        except Exception as exc:  # noqa: BLE001 - 镜像失败不该让登录结果丢失
            log.debug("镜像 OAuth token 到 OAuthStore 失败（%s）：%s", self.server, exc)
        return token

    def load(self) -> TokenPayload | None:
        """读令牌：先读本模块的键，再退到 ``OAuthStore``（``mcp auth set`` 写的那份）。"""
        try:
            raw = keyring.get_password(self.service, TOKEN_KEY)
        except Exception as exc:  # noqa: BLE001 - 后端不可用（无 vault）不该让命令崩
            log.debug("读 keyring 失败（%s）：%s", self.service, exc)
            raw = None
        if raw:
            try:
                parsed = TokenPayload.from_dict(json.loads(raw))
            except ValueError:
                parsed = None
            if parsed is not None:
                return parsed
        return self._load_legacy()

    def _load_legacy(self) -> TokenPayload | None:
        """读 ``app/mcp.py`` ``OAuthStore`` 里的旧键（本轮之前 ``mcp auth set`` 写的）。"""
        try:
            from app.mcp import OAuthStore

            record = OAuthStore(f"orbit:{self.data_dir}").get(self.server)
        except Exception as exc:  # noqa: BLE001
            log.debug("读旧 OAuth 键失败（%s）：%s", self.server, exc)
            return None
        if record is None:
            return None
        return TokenPayload(
            access_token=record.access_token,
            token_type=record.token_type,
            expires_in=None,
            refresh_token=record.refresh_token,
            scope=record.scope,
            obtained_at=record.obtained_at,
            expires_at=record.expires_at,
        )

    def clear(self) -> bool:
        """删令牌（两边都删）。返回是否真的删掉了至少一条。"""
        removed = False
        try:
            if keyring.get_password(self.service, TOKEN_KEY):
                keyring.delete_password(self.service, TOKEN_KEY)
                removed = True
        except Exception as exc:  # noqa: BLE001
            log.debug("删 keyring 键失败（%s）：%s", self.service, exc)
        try:
            from app.mcp import OAuthStore

            removed = OAuthStore(f"orbit:{self.data_dir}").clear(self.server) or removed
        except Exception as exc:  # noqa: BLE001
            log.debug("删旧 OAuth 键失败（%s）：%s", self.server, exc)
        return removed

    def status(self, *, skew: float = DEFAULT_SKEW, now: float | None = None) -> dict:
        """令牌状态：``missing`` / ``expired`` / ``expiring`` / ``valid``。"""
        token = self.load()
        if token is None:
            return {"server": self.server, "state": "missing", "expires_at": 0.0, "seconds_left": None}
        moment = time.time() if now is None else now
        left = None if token.expires_at <= 0 else token.expires_at - moment
        if left is None:
            state = "valid"
        elif left <= 0:
            state = "expired"
        elif left <= skew:
            state = "expiring"
        else:
            state = "valid"
        return {
            "server": self.server,
            "state": state,
            "expires_at": token.expires_at,
            "seconds_left": None if left is None else round(left, 1),
        }


# ── 需要网络/浏览器的那几步（如实抛）───────────────────────────────────────

#: ``run_flow`` 报错时逐条列出的缺项。写在这里而不是拼在异常字符串里，是为了让测试
#: 能断言"这四件事都说了"，也让下一个人接着做时直接照这张清单补。
FLOW_REQUIREMENTS: tuple[str, ...] = (
    "出网访问授权服务器：先读 RFC 9728 保护资源元数据、再读 RFC 8414 授权服务器元数据"
    "（本机无网络，这两次发现请求发不出去）",
    "动态客户端注册（RFC 7591）拿 client_id（同样要出网）",
    "在本机回环端口起一个回调 HTTP 服务器等浏览器重定向（需要占端口 + 等一个外部事件）",
    "用户在自己的浏览器里完成授权（需要一个真人 + 一个能开浏览器的桌面会话）",
)


def run_flow(config: Any, *, server: str = "", store: TokenStore | None = None) -> TokenPayload:
    """走完整授权码流程。**本机做不到，如实抛** :class:`NetworkUnavailable`。

    参数留着是为了将来接线时签名不用改；现在它们只用于把"卡在哪一步"说清楚。
    """
    endpoint = authorization_endpoint(config) or "（无法从服务 url 推出授权端点）"
    missing = "\n".join(f"  {index}. {item}" for index, item in enumerate(FLOW_REQUIREMENTS, 1))
    raise NetworkUnavailable(
        f"MCP OAuth 授权流程需要下列条件，本机缺其中的关键几项：\n{missing}\n"
        f"（授权端点会是 {endpoint}；{server or '本服务'}）\n"
        "纯函数部分已经实现并且有测试：PKCE、authorize URL、换 token / 刷新请求体、"
        "令牌响应解析、过期判定、keyring 存储。可以用 `orbit mcp auth set <name>` "
        "手工写入 token，再用 `orbit mcp auth status <name>` 查过期时间。"
    )


def refresh_token(
    config: Any,
    token: TokenPayload,
    *,
    transport=None,
    store: TokenStore | None = None,
) -> TokenPayload:
    """用 refresh token 换一个新的 access token。

    ``transport`` 是"POST 表单"的注入点（签名 ``(url, payload) -> dict``）。生产环境不
    注入——因为本机无网络，注入一个真 HTTP 客户端也只会超时；**不注入时如实抛**
    :class:`NetworkUnavailable`，并说清缺什么。测试注入一个假的 transport 就能验证
    "拼请求体 → 解析响应 → 存回 keyring"这条链是通的。
    """
    payload = refresh_payload(config, token.refresh_token)
    endpoint = token_endpoint(config)
    if transport is None:
        raise NetworkUnavailable(
            "刷新令牌需要 POST 到 "
            f"{endpoint or '（无法从服务 url 推出令牌端点）'}，本机无网络，这一步做不了。"
            "缺的是：出网访问令牌端点 + 一个可用的 refresh token。"
        )
    response = transport(endpoint, payload)
    fresh = parse_token_response(response)
    # RFC 6749 §6：刷新响应可以不返回新的 refresh_token，那就继续用旧的。
    if not fresh.refresh_token:
        fresh = TokenPayload(
            access_token=fresh.access_token,
            token_type=fresh.token_type,
            expires_in=fresh.expires_in,
            refresh_token=token.refresh_token,
            scope=fresh.scope or token.scope,
            obtained_at=fresh.obtained_at,
            expires_at=fresh.expires_at,
        )
    if store is not None:
        store.save(fresh)
    return fresh


# ── 给 CLI / app.mcp 的收尾报告 ─────────────────────────────────────────────


def login_report(settings, entry: dict, *, now: float | None = None) -> dict:
    """``mcp login`` / ``mcp reauth`` 能真做的事：查过期、判断能否刷新、给出授权 URL。

    这三件都是**本地计算**，不需要网络。返回值里的 ``steps`` 是给用户看的执行计划
    （逐条带 ``done`` 标记），``authorize_url`` 是"真流程会打开的那一条"——打印它有意义：
    用户可以在有浏览器的机器上手工打开，或者拿去和厂商文档对账。
    """
    name = str(entry.get("name") or "")
    store = TokenStore.for_settings(settings, name)
    token = store.load()
    status = store.status(now=now)
    # verifier 用完就丢：它只在与"等一下校验回调"的那一侧成对出现时才有意义，而本机
    # 走不到那一步。**故意不放进返回值**——返回值会被 `--json` 打到 stdout，也就可能被
    # 重定向进日志文件；PKCE 的另一半不该出现在那里。
    _verifier, challenge = pkce_pair()
    state = new_state()
    config = {
        "url": entry.get("url") or "",
        "client_id": entry.get("client_id") or "",
        "scope": entry.get("scope") or "",
        "redirect_uri": entry.get("redirect_uri") or "",
        "authorization_endpoint": entry.get("authorization_endpoint") or "",
        "token_endpoint": entry.get("token_endpoint") or "",
    }
    url = ""
    url_error = ""
    try:
        url = build_authorize_url(config, state=state, code_challenge=challenge)
    except ValueError as exc:
        # 缺 client_id 是最常见的一种（动态注册要出网），把它当成"报错"会让 login 命令
        # 在最该给信息的时候什么都不给——所以降级成一句说明。
        url_error = str(exc)
    expired = bool(token is not None and is_expired(token, now=now))
    can_refresh = bool(token is not None and token.refresh_token)
    steps = [
        {"step": "读取已有令牌", "done": token is not None, "detail": status["state"]},
        {
            "step": "判断是否过期",
            "done": token is not None,
            "detail": "无令牌" if token is None else ("已过期" if expired else "未过期"),
        },
        {"step": "刷新令牌", "done": False, "detail": "需要网络" if can_refresh else "没有 refresh token"},
        {"step": "起本地回调服务器 + 打开浏览器", "done": False, "detail": "本机做不到，见 missing"},
        {"step": "换 token 并写入 keyring", "done": False, "detail": "依赖上一步"},
    ]
    return {
        "server": name,
        "state": status["state"],
        "expires_at": status["expires_at"],
        "seconds_left": status["seconds_left"],
        "has_refresh_token": can_refresh,
        "authorize_url": url,
        "authorize_url_error": url_error,
        "code_challenge_method": "S256",
        # 只给 challenge（公开值），不给 verifier（秘密，见上）。
        "code_challenge": challenge,
        "state_parameter": state,
        "steps": steps,
        "missing": list(FLOW_REQUIREMENTS),
    }


__all__ = [
    "DEFAULT_REDIRECT_URI",
    "DEFAULT_SKEW",
    "FLOW_REQUIREMENTS",
    "PKCE_BYTES",
    "PKCE_MAX_LENGTH",
    "TOKEN_KEY",
    "NetworkUnavailable",
    "TokenPayload",
    "TokenStore",
    "authorization_endpoint",
    "build_authorize_url",
    "code_challenge_for",
    "exchange_payload",
    "is_expired",
    "login_report",
    "new_code_verifier",
    "new_state",
    "parse_token_response",
    "pkce_pair",
    "redirect_uri",
    "refresh_payload",
    "refresh_token",
    "resource_url",
    "run_flow",
    "scope_of",
    "token_endpoint",
]
