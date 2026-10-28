"""``app/peer_client.py`` 的契约测试（MATRIX 12.9）。

本机**无网络**，所以这里一个真实请求都不发：全部走 ``httpx.MockTransport``。
要守住的四件事：

1. **四个方法各自的形状**（``health`` / ``dm`` / ``run`` / ``stop``）：URL、Bearer、
   body 三个字段逐个钉住——形状写错了对端就收不到，而"发出去但字段名不对"是最难
   从日志里看出来的故障。
2. **``run`` / ``stop`` 在对端不支持时抛 ``PeerUnsupported``**，并且消息里必须点名
   「对端需要提供 ``<base>/peer/run`` 与 ``<base>/peer/stop``」——只说"未实现"
   等于让用户去猜该去对端加什么。
3. **重试语义**：5xx 重试、4xx 立刻失败。这条写错的代价是"对方回 400 之后还刷 3 次"，
   或者"对端重启期间的 503 直接判死"。
4. **离线可验**：``transport`` 可注入本身就是被测试的事实。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.peer_client import (
    PEER_DM_PATH,
    PEER_HEALTH_PATH,
    PEER_RUN_PATH,
    PEER_STATUS_PATH,
    PEER_STOP_PATH,
    PROTOCOL,
    RUN_CONTRACT,
    PeerClient,
    PeerResponseError,
    PeerUnavailable,
    PeerUnsupported,
)

BASE = "http://spark.test:8377"


def _client(handler, **kwargs) -> tuple[list[httpx.Request], PeerClient]:
    """造一个记录请求的客户端（每个用例一份，避免跨用例串味）。"""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(record)
    return seen, PeerClient(BASE, token="k1", transport=transport, **kwargs)


# ── 协议表 ──────────────────────────────────────────────────────────────────


def test_protocol_table_covers_five_endpoints():
    """协议表就是"对端要实现什么"的单一事实源：5 个端点，一个不能少。"""
    assert set(PROTOCOL) == {
        PEER_HEALTH_PATH,
        PEER_DM_PATH,
        PEER_RUN_PATH,
        PEER_STOP_PATH,
        PEER_STATUS_PATH,
    }
    for path, contract in PROTOCOL.items():
        assert path.startswith("/peer/")
        assert contract.strip(), path


# ── health ──────────────────────────────────────────────────────────────────


def test_health_gets_peer_health_and_reports_capabilities():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "capabilities": ["dm", "run"]})

    seen, client = _client(handler)
    payload = client.health()
    assert payload["ok"] is True
    assert payload["capabilities"] == ["dm", "run"]
    assert payload["peer"] == BASE
    assert payload["protocol"] == PROTOCOL
    request = seen[0]
    assert request.method == "GET"
    assert str(request.url) == f"{BASE}{PEER_HEALTH_PATH}"
    assert request.headers["Authorization"] == "Bearer k1"


def test_health_does_not_invent_capabilities():
    """对端没声明能力时**不能**替它猜：能力列表必须是空。"""
    _seen, client = _client(lambda request: httpx.Response(200, json={"ok": True}))
    assert client.health()["capabilities"] == []


def test_health_accepts_plain_text_response():
    """极简对端回 ``pong`` 纯文本也要能用（不因"不是 JSON"就判坏）。"""
    _seen, client = _client(lambda request: httpx.Response(200, text="pong"))
    assert client.health()["text"] == "pong"


# ── dm ──────────────────────────────────────────────────────────────────────


def test_dm_posts_agent_and_message_and_returns_reply():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"reply": "磁盘一切正常"})

    seen, client = _client(handler)
    result = client.dm("researcher", "磁盘状态？")
    assert result["reply"] == "磁盘一切正常"
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == f"{BASE}{PEER_DM_PATH}"
    assert request.headers["Authorization"] == "Bearer k1"
    assert json.loads(request.content) == {"agent": "researcher", "message": "磁盘状态？"}


def test_dm_falls_back_to_default_agent():
    """``peer dm spark "hi"``（不带 ``/agent``）要把空 agent 原样发出去，不能编一个。"""
    seen, client = _client(
        lambda request: httpx.Response(200, json={"reply": "ok"}), agent=""
    )
    client.dm("", "hi")
    assert json.loads(seen[0].content)["agent"] == ""


# ── run ─────────────────────────────────────────────────────────────────────


def test_run_posts_and_returns_run_id():
    seen, client = _client(lambda request: httpx.Response(202, json={"run_id": "run_42"}))
    result = client.run("researcher", "长任务", idempotency_key="k-1")
    assert result["run_id"] == "run_42"
    request = seen[0]
    assert str(request.url) == f"{BASE}{PEER_RUN_PATH}"
    assert json.loads(request.content) == {
        "agent": "researcher",
        "message": "长任务",
        "idempotency_key": "k-1",
    }


def test_run_requires_run_id_in_response():
    """对端回 200 却没有 run_id → 明确报错（不能返回一个空 run id 让用户去查）。"""
    _seen, client = _client(lambda request: httpx.Response(200, json={"ok": True}))
    with pytest.raises(PeerResponseError) as excinfo:
        client.run("", "长任务")
    assert "run_id" in str(excinfo.value)


# ── stop ────────────────────────────────────────────────────────────────────


def test_stop_posts_run_id():
    seen, client = _client(lambda request: httpx.Response(200, json={"stopped": True}))
    result = client.stop("run_42")
    assert result["stopped"] is True
    assert result["run_id"] == "run_42"
    assert str(seen[0].url) == f"{BASE}{PEER_STOP_PATH}"
    assert json.loads(seen[0].content) == {"run_id": "run_42"}


def test_stop_rejects_empty_run_id():
    _seen, client = _client(lambda request: httpx.Response(200))
    with pytest.raises(ValueError):
        client.stop("  ")


# ── status ──────────────────────────────────────────────────────────────────


def test_status_reads_run_ledger():
    seen, client = _client(lambda request: httpx.Response(200, json={"runs": [{"run_id": "r1"}]}))
    result = client.status("r1")
    assert result["runs"] == {"runs": [{"run_id": "r1"}]}
    assert str(seen[0].url) == f"{BASE}{PEER_STATUS_PATH}?run_id=r1"


# ── 对端不支持 run / stop ───────────────────────────────────────────────────


@pytest.mark.parametrize("status_code", [404, 405, 501])
def test_run_raises_peer_unsupported_when_peer_lacks_endpoint(status_code):
    """404/405/501 = 对端没有这个端点 → ``PeerUnsupported``，并点名缺什么。"""
    _seen, client = _client(lambda request: httpx.Response(status_code))
    with pytest.raises(PeerUnsupported) as excinfo:
        client.run("", "长任务")
    message = str(excinfo.value)
    assert f"<base>{PEER_RUN_PATH}" in message
    assert f"<base>{PEER_STOP_PATH}" in message
    assert RUN_CONTRACT in message
    # 说清"为什么 Orbit 这边也没有"：不然读者会以为是自己的配置问题
    assert "api_server" in message


def test_stop_raises_peer_unsupported_and_names_missing_endpoints():
    _seen, client = _client(lambda request: httpx.Response(404))
    with pytest.raises(PeerUnsupported) as excinfo:
        client.stop("run_42")
    assert "<base>/peer/stop" in str(excinfo.value)
    assert excinfo.value.endpoint == PEER_STOP_PATH


def test_unsupported_is_not_retried():
    """能力缺失不是网络抖动：**只发一次**，别把 404 刷三遍。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404)

    _seen, client = _client(handler, attempts=3)
    with pytest.raises(PeerUnsupported):
        client.run("", "x")
    assert len(calls) == 1


# ── 重试语义 ────────────────────────────────────────────────────────────────


def test_retries_on_5xx_then_succeeds():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503) if len(calls) < 3 else httpx.Response(200, json={"reply": "ok"})

    _seen, client = _client(handler, attempts=3)
    assert client.dm("", "hi")["reply"] == "ok"
    assert len(calls) == 3


def test_does_not_retry_on_4xx():
    """4xx 是请求本身有问题（缺 token / body 不对），重试一万次也一样。"""
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="bad request")

    _seen, client = _client(handler, attempts=5)
    with pytest.raises(PeerResponseError) as excinfo:
        client.dm("", "hi")
    assert len(calls) == 1
    assert excinfo.value.status == 400
    assert "400" in str(excinfo.value)


def test_connection_errors_become_peer_unavailable_after_retries():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ConnectError("refused", request=request)

    _seen, client = _client(handler, attempts=2)
    with pytest.raises(PeerUnavailable) as excinfo:
        client.dm("", "hi")
    assert len(calls) == 2
    assert "试了 2 次" in str(excinfo.value)


def test_timeouts_are_retried():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("slow", request=request)

    _seen, client = _client(handler, attempts=2)
    with pytest.raises(PeerUnavailable):
        client.dm("", "hi")
    assert len(calls) == 2


# ── URL 规范化 ──────────────────────────────────────────────────────────────


def test_trailing_slash_is_normalized():
    """结尾斜杠必须去掉：拼出 ``//peer/dm`` 会被反代判 404，而 404 在本模块里
    恰好是"对端没有这个端点"的信号——一个斜杠能把"连得上"误判成"不支持"。"""
    seen: list[httpx.Request] = []
    transport = httpx.MockTransport(
        lambda request: (seen.append(request), httpx.Response(200, json={"reply": "ok"}))[1]
    )
    client = PeerClient(f"{BASE}/", transport=transport)
    client.dm("", "hi")
    assert str(seen[0].url) == f"{BASE}{PEER_DM_PATH}"


def test_missing_base_url_fails_fast():
    client = PeerClient("", transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    with pytest.raises(Exception) as excinfo:
        client.health()
    assert "base_url" in str(excinfo.value)
