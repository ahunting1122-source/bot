"""peer 协议客户端（MATRIX 12.9）：把「一次 HTTP POST」升级成**有契约的对等协议**。

## 为什么需要这一层

``orbit peer dm`` 早先是"在命令处理函数里直接 POST 一次"（见
``app/cli/commands/platform_extra.py``）：URL、Bearer、body 形状、超时、重试全部散在
CLI 里，于是 ① 别的地方（cron 投递、网关、将来的入站服务端）想给对端发消息只能复制
粘贴一遍；② 没有任何地方**声明**这个协议长什么样，对端要实现时只能读 Orbit 的源码。
本模块把协议钉成一张表（:data:`PROTOCOL`）再加一个薄客户端，两件事一起解决。

## 协议的诚实边界（先定协议，再实现客户端）

Hermes 的对等节点靠 ``api_server`` 平台提供**异步 run 账本**与**取消端点**。
Orbit 这边两头都没有：平台适配器只做出站文本、网关不监听端口。所以本轮的取舍是：

- ``health`` / ``dm``：**客户端真实现**，形状自洽，可离线用 ``httpx.MockTransport``
  全链路验证（本机无网络，任何真请求都是 bug）；
- ``run`` / ``stop`` / ``status``：**协议先定、客户端先写**，但**服务端未实现时如实抛**
  :class:`PeerUnsupported`，消息里点名「对端需要提供 ``<base>/peer/run`` 与
  ``<base>/peer/stop``」。为什么不假装成功：一个发得出去、查不回来的 run id 比不发
  更坏——用户会拿着一个永远查不到的任务号等结果。

## 重试语义（和 ``app/platforms.py`` 的 WebhookAdapter 同一条规则）

- 5xx / 连接错误 / 超时 → 重试（对端可能正在重启，重试有意义）；
- 4xx → **立刻失败**（请求本身有问题，重试一万次也一样）；
- ``run`` / ``stop`` / ``status`` 遇到 404 / 405 / 501 → 直接 :class:`PeerUnsupported`
  （这是"对端没有这个端点"，不是"对端暂时不健康"，重试只会把 404 刷进日志）。

``transport`` 必须可注入：本机无网络，注入 ``httpx.MockTransport`` 才能验证成功 /
5xx / 4xx / 超时四条路径。可注入不是为了"方便测试"，而是为了让这一层在离线环境里
**被真的跑过**。
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import httpx

# ── 端点路径 ────────────────────────────────────────────────────────────────

#: 对等节点的健康探测。返回 200 + 任意 JSON（``capabilities`` 可选）。
PEER_HEALTH_PATH = "/peer/health"
#: 同步私信：发一条、拿到一条回复。
PEER_DM_PATH = "/peer/dm"
#: 异步长任务：起一个 run 并立刻返回 ``run_id``（对端未实现时 404）。
PEER_RUN_PATH = "/peer/run"
#: 取消单个 run（对端未实现时 404）。
PEER_STOP_PATH = "/peer/stop"
#: 读 run 的状态与最终输出（对端未实现时 404）。
PEER_STATUS_PATH = "/peer/status"

#: 触发 :class:`PeerUnsupported` 的状态码：404 没有这个路由、405 方法不对、
#: 501 对端明确说"没实现"。三者都是**能力缺失**，与"网络抖动"是两件事。
UNSUPPORTED_STATUS = frozenset({404, 405, 501})

#: ``run`` / ``stop`` 的契约原文。报错与 ``peer health`` 都直接引用这一句，
#: 免得同一个事实在三个地方写成三种说法。
RUN_CONTRACT = "对端需要提供 <base>/peer/run 与 <base>/peer/stop"

#: 协议表：端点 → 一句话契约（请求体 / 响应体 / 失败语义）。
#: 这张表是本模块的**单一事实源**：对端实现者照着它写，Orbit 照着它测。
PROTOCOL: dict[str, str] = {
    PEER_HEALTH_PATH: "GET · 200 + JSON（可选 capabilities: ['dm','run','stop','status']）",
    PEER_DM_PATH: "POST {agent, message} · 200 + {reply}（同步，等对端跑完这一轮）",
    PEER_RUN_PATH: "POST {agent, message, idempotency_key} · 202/200 + {run_id}（异步，立刻返回）",
    PEER_STOP_PATH: "POST {run_id} · 200 + {stopped: true}（只停这一个 run）",
    PEER_STATUS_PATH: "GET ?run_id=… · 200 + {run_id, state, output}（state ∈ running/done/failed）",
}


# ── 异常 ────────────────────────────────────────────────────────────────────


class PeerError(RuntimeError):
    """peer 客户端的一切失败的基类。``peer`` / ``endpoint`` 供调用方拼可行动的报错。"""

    def __init__(self, message: str, *, peer: str = "", endpoint: str = "") -> None:
        self.peer = peer
        self.endpoint = endpoint
        super().__init__(message)


class PeerUnsupported(PeerError):
    """对端**没有**这个端点（或明确说没实现）。

    为什么单独一个异常类型：调用方看到它应该"如实告诉用户对端缺什么"，
    而不是"稍后再试"。用 ``ok=False`` 的返回值做不到这一点——调用方会当成网络抖动。
    """

    def __init__(self, peer: str, endpoint: str, *, missing: tuple[str, ...] = ()) -> None:
        self.missing = tuple(missing) or (PEER_RUN_PATH, PEER_STOP_PATH)
        needs = "、".join(f"<base>{path}" for path in self.missing)
        super().__init__(
            f"对端 {peer or '(未命名)'} 不支持 {endpoint}：{RUN_CONTRACT}"
            f"（本次缺的是 {needs}）。"
            "Hermes 的对等节点由 api_server 平台的 run 账本 + 取消端点提供；"
            "Orbit 本轮两端都没有，所以这条路径只定协议、不发请求。",
            peer=peer,
            endpoint=endpoint,
        )


class PeerUnavailable(PeerError):
    """连不上（DNS / 拒绝连接 / 超时，且重试预算已用完）。"""


class PeerResponseError(PeerError):
    """对端回了一个 HTTP 错误状态（4xx/5xx）或回了看不懂的载荷。"""

    def __init__(
        self,
        message: str,
        *,
        peer: str = "",
        endpoint: str = "",
        status: int = 0,
        body: str = "",
    ) -> None:
        self.status = int(status)
        self.body = str(body or "")
        super().__init__(message, peer=peer, endpoint=endpoint)


# ── 客户端 ──────────────────────────────────────────────────────────────────


class PeerClient:
    """一个对等节点的薄客户端：5 个方法对应 :data:`PROTOCOL` 的 5 个端点。

    用法（离线可验）::

        client = PeerClient("http://spark.lan:8377", token="k1",
                            transport=httpx.MockTransport(handler))
        client.health()          # GET  /peer/health
        client.dm("researcher", "磁盘状态？")
        client.run("researcher", "长任务")   # 对端没端点 → PeerUnsupported

    ``base_url`` 结尾斜杠会被规范化（``http://h:p/`` 与 ``http://h:p`` 等价）：
    拼出 ``//peer/dm`` 这种 URL 时，很多反向代理会回 404，而 404 恰好是本模块
    用来判断"对端没有这个端点"的信号——一个多余的斜杠会把"连得上"误判成"不支持"。
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str = "",
        agent: str = "",
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
        attempts: int = 3,
        retry_delay: float = 0.0,
        user_agent: str = "orbit-peer/1",
    ) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.token = str(token or "")
        #: 默认 agent（``<peer>/<agent>`` 语法里的具名 profile）；单次调用可覆盖。
        self.agent = str(agent or "")
        self.transport = transport
        #: 比 webhook 的 5s 长：对端可能要真跑一轮 agent 才回。
        self.timeout = float(timeout)
        self.attempts = max(1, int(attempts))
        self.retry_delay = max(0.0, float(retry_delay))
        self.user_agent = user_agent

    # ── 五个端点 ──
    def health(self) -> dict[str, Any]:
        """``GET <base>/peer/health``：对端活着吗、声明了哪些能力。

        返回原样载荷 + 三个便于调用方直接用的键（``ok`` / ``peer`` / ``capabilities``）。
        **不把 200 当成"全功能"**：能力以对端自报的 ``capabilities`` 为准，没报就是空。
        """
        payload, status = self._request("GET", PEER_HEALTH_PATH)
        capabilities = payload.get("capabilities")
        return {
            **payload,
            "ok": True,
            "peer": self.base_url,
            "status": status,
            "capabilities": [str(item) for item in capabilities] if isinstance(capabilities, list) else [],
            "protocol": dict(PROTOCOL),
        }

    def dm(self, agent: str, message: str) -> dict[str, Any]:
        """``POST <base>/peer/dm``：同步私信，返回对端的回复正文。

        ``agent`` 为空时退回构造时的默认 agent——``peer dm spark "hi"`` 与
        ``peer dm spark/researcher "hi"`` 都要能用。
        """
        body = {"agent": self._agent(agent), "message": str(message)}
        payload, status = self._request("POST", PEER_DM_PATH, json_body=body)
        reply = payload.get("reply") or payload.get("text") or ""
        return {
            "ok": True,
            "peer": self.base_url,
            "agent": body["agent"],
            "status": status,
            "reply": str(reply),
            "raw": payload,
        }

    def run(self, agent: str, command: str, *, idempotency_key: str = "") -> dict[str, Any]:
        """``POST <base>/peer/run``：起一个异步 run，返回 ``run_id``。

        对端没有这个端点（404/405/501）时抛 :class:`PeerUnsupported`——
        **不返回假 run_id**。``idempotency_key`` 由调用方给一个跨重试稳定的值，
        对端据此去重（同一句话发两次是两件事，重试一次不是）。
        """
        body: dict[str, Any] = {"agent": self._agent(agent), "message": str(command)}
        if idempotency_key:
            body["idempotency_key"] = str(idempotency_key)
        payload, status = self._request(
            "POST", PEER_RUN_PATH, json_body=body, unsupported_paths=(PEER_RUN_PATH, PEER_STOP_PATH)
        )
        run_id = str(payload.get("run_id") or payload.get("id") or "")
        if not run_id:
            raise PeerResponseError(
                f"对端 {self.base_url} 回了 {status}，但载荷里没有 run_id："
                f"{self._snippet(payload)}。没有 run id 就无法查询或停止这个任务。",
                peer=self.base_url,
                endpoint=PEER_RUN_PATH,
                status=status,
            )
        return {"ok": True, "peer": self.base_url, "run_id": run_id, "status": status, "raw": payload}

    def stop(self, run_id: str) -> dict[str, Any]:
        """``POST <base>/peer/stop``：只停这一个 run，不影响对端其它 turn。"""
        target = str(run_id or "").strip()
        if not target:
            raise ValueError("peer stop 需要 run_id（空串无法定位一个 run）")
        payload, status = self._request(
            "POST",
            PEER_STOP_PATH,
            json_body={"run_id": target},
            unsupported_paths=(PEER_RUN_PATH, PEER_STOP_PATH),
        )
        return {
            "ok": True,
            "peer": self.base_url,
            "run_id": target,
            "stopped": bool(payload.get("stopped", True)),
            "status": status,
            "raw": payload,
        }

    def status(self, run_id: str = "") -> dict[str, Any]:
        """``GET <base>/peer/status[?run_id=…]``：读 run 账本（不给 id 就是整个账本）。"""
        query = f"?{urlencode({'run_id': run_id})}" if run_id else ""
        payload, code = self._request(
            "GET",
            PEER_STATUS_PATH,
            query=query,
            unsupported_paths=(PEER_RUN_PATH, PEER_STOP_PATH),
        )
        return {"ok": True, "peer": self.base_url, "status": code, "runs": payload}

    # ── 内部 ──
    def _agent(self, agent: str) -> str:
        return str(agent or self.agent or "").strip()

    def _url(self, path: str, query: str = "") -> str:
        return f"{self.base_url}{path}{query}"

    def _headers(self) -> dict[str, str]:
        headers = {"User-Agent": self.user_agent, "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _snippet(payload: Any) -> str:
        text = str(payload)
        return text[:200] + ("…" if len(text) > 200 else "")

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        query: str = "",
        unsupported_paths: tuple[str, ...] = (),
    ) -> tuple[dict[str, Any], int]:
        """发一次请求（含重试），返回 ``(载荷, 状态码)``。

        ``unsupported_paths`` 非空时，404/405/501 会被翻译成
        :class:`PeerUnsupported`（并把这些路径原样写进报错里，告诉用户缺什么）。
        """
        if not self.base_url:
            raise PeerError("peer 客户端没有 base_url（先 `orbit peer add <name> --url …`）")
        url = self._url(path, query)
        last_error = ""
        with httpx.Client(transport=self.transport, timeout=self.timeout) as client:
            for attempt in range(1, self.attempts + 1):
                try:
                    response = client.request(
                        method, url, headers=self._headers(), json=json_body
                    )
                except httpx.TimeoutException as exc:
                    last_error = f"超时（第 {attempt} 次）：{exc!r}"
                except httpx.HTTPError as exc:
                    last_error = f"连接错误（第 {attempt} 次）：{exc!r}"
                else:
                    if response.status_code in UNSUPPORTED_STATUS and unsupported_paths:
                        raise PeerUnsupported(
                            self.base_url, path, missing=tuple(unsupported_paths)
                        )
                    if response.status_code < 400:
                        return self._decode(response), response.status_code
                    body = response.text[:200]
                    if response.status_code < 500:
                        # 4xx：请求本身有问题（缺 token / body 不对 / 路由不存在），
                        # 重试只会把同一条错误刷 N 遍。
                        raise PeerResponseError(
                            f"对端 {self.base_url} 返回 HTTP {response.status_code}"
                            f"（{path}）：{body or '(空响应)'}",
                            peer=self.base_url,
                            endpoint=path,
                            status=response.status_code,
                            body=body,
                        )
                    last_error = f"HTTP {response.status_code}（第 {attempt} 次）：{body}"
                if attempt < self.attempts and self.retry_delay:
                    time.sleep(self.retry_delay)
        raise PeerUnavailable(
            f"连不上对等节点 {self.base_url}{path}（试了 {self.attempts} 次）：{last_error}。"
            "检查 `orbit peer list` 里的 URL，以及对端是否在跑带 peer 端点的服务",
            peer=self.base_url,
            endpoint=path,
        )

    @staticmethod
    def _decode(response: httpx.Response) -> dict[str, Any]:
        """响应体解析：JSON 对象原样返回；非 JSON 包成 ``{"text": ...}``。

        为什么不要求必须 JSON：对端可能是一个极简脚本（回 ``pong`` 纯文本），
        这种响应完全可用，为它抛异常等于把"能用"判成"坏了"。
        """
        try:
            parsed = response.json()
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
        if parsed is not None:
            return {"data": parsed}
        return {"text": response.text}


__all__ = [
    "PEER_DM_PATH",
    "PEER_HEALTH_PATH",
    "PEER_RUN_PATH",
    "PEER_STATUS_PATH",
    "PEER_STOP_PATH",
    "PROTOCOL",
    "RUN_CONTRACT",
    "UNSUPPORTED_STATUS",
    "PeerClient",
    "PeerError",
    "PeerResponseError",
    "PeerUnavailable",
    "PeerUnsupported",
]
