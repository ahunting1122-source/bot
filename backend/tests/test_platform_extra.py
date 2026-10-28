"""6 个新平台适配器 + ``orbit peer`` / ``orbit gateway`` 剩余子命令的契约测试。

两块，各自盯一类最容易假绿的东西：

1. **平台适配器**：每个平台的 URL 与 body 形状逐条钉住（全部走 ``httpx.MockTransport``，
   本机无网络，任何真请求都是 bug），未配置时报错必须点名**该平台自己的**环境变量，
   并且 ``PLATFORMS`` 里已实现的条目必须填齐 ``adapter`` / ``scope``、清空 ``reason``。
2. **CLI**：能跑通的断言真跑通（退出码 0 + 落盘事实），做不到的断言**退出码 3**且
   文案里写清原因——"未实现"被静默当成成功，比没有这条命令更糟。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app import platforms
from app.cli.main import main
from app.cli.spec import REGISTRY

#: 显式导入即接线（生产里由 ``app/cli/commands/__init__.py`` 统一接线）。
import app.cli.commands.platform_extra as platform_extra  # noqa: E402,F401  isort: skip

# ── 夹具 ────────────────────────────────────────────────────────────────────


def _capture() -> tuple[list[httpx.Request], httpx.MockTransport]:
    """造一个记录请求的 MockTransport（每个用例一份，避免跨用例串味）。"""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True}, headers={"id": "msg-1"})

    return seen, httpx.MockTransport(handler)


def _run(tmp_path, *argv: str) -> int:
    return main(["--data-dir", str(tmp_path / "data"), *argv])


# ── 6 个新平台的 URL 与 body ────────────────────────────────────────────────


def test_mattermost_posts_to_v4_posts_with_bearer():
    seen, transport = _capture()
    adapter = platforms.mattermost_adapter(
        url="https://mm.example.com/", token="tok", transport=transport
    )
    result = adapter.send_text("chan-1", "hi 世界")
    assert result.ok
    request = seen[0]
    assert request.method == "POST"
    # 结尾斜杠要被规范化，否则拼出 `//api/v4/posts`
    assert str(request.url) == "https://mm.example.com/api/v4/posts"
    assert request.headers["Authorization"] == "Bearer tok"
    assert json.loads(request.content) == {"channel_id": "chan-1", "message": "hi 世界"}


def test_line_pushes_text_message():
    seen, transport = _capture()
    adapter = platforms.line_adapter(token="tok", transport=transport)
    assert adapter.send_text("U1234", "hi 世界").ok
    request = seen[0]
    assert str(request.url) == "https://api.line.me/v2/bot/message/push"
    assert request.headers["Authorization"] == "Bearer tok"
    assert json.loads(request.content) == {
        "to": "U1234",
        "messages": [{"type": "text", "text": "hi 世界"}],
    }


def test_whatsapp_cloud_uses_graph_messages():
    seen, transport = _capture()
    adapter = platforms.whatsapp_cloud_adapter(
        token="tok", phone_number_id="555", transport=transport
    )
    assert adapter.send_text("8613800000000", "hi 世界").ok
    request = seen[0]
    assert str(request.url) == "https://graph.facebook.com/v20.0/555/messages"
    assert request.headers["Authorization"] == "Bearer tok"
    assert json.loads(request.content) == {
        "messaging_product": "whatsapp",
        "to": "8613800000000",
        "type": "text",
        "text": {"body": "hi 世界"},
    }


def test_sms_uses_form_encoded_body_and_basic_auth():
    """Twilio 两个坑：body 是 **form-encoded**（不是 JSON），认证是 **HTTP Basic**。"""
    seen, transport = _capture()
    adapter = platforms.sms_adapter(
        account_sid="AC1", auth_token="sec", from_number="+1555", transport=transport
    )
    assert adapter.send_text("+8613800000000", "hi")
    request = seen[0]
    assert str(request.url) == "https://api.twilio.com/2010-04-01/Accounts/AC1/Messages.json"
    assert request.headers["Content-Type"] == "application/x-www-form-urlencoded"
    # Basic 的 base64 里就是 SID:AuthToken
    assert request.headers["Authorization"] == "Basic QUMxOnNlYw=="
    body = request.content.decode()
    assert "To=%2B8613800000000" in body
    assert "From=%2B1555" in body
    assert "Body=hi" in body


def test_homeassistant_calls_notify_service():
    seen, transport = _capture()
    adapter = platforms.homeassistant_adapter(
        url="http://ha.local:8123/", token="tok", service="mobile_app_x", transport=transport
    )
    assert adapter.send_text("notify.x", "hi 世界").ok
    request = seen[0]
    assert str(request.url) == "http://ha.local:8123/api/services/notify/mobile_app_x"
    assert request.headers["Authorization"] == "Bearer tok"
    assert json.loads(request.content) == {"message": "hi 世界"}


def test_matrix_uses_put_with_encoded_room_and_stable_txn():
    """Matrix 三处必须照规范：PUT、room id 编码、txn 在重试之间**复用**。"""
    seen, transport = _capture()
    adapter = platforms.matrix_adapter(
        homeserver="https://matrix.example.org", token="tok", transport=transport
    )
    assert adapter.send_text("!abc:example.org", "hi 世界").ok
    request = seen[0]
    assert request.method == "PUT"
    # 用 `str(url)` 而不是 `url.path`：后者是**解码后**的路径，会把编码问题藏起来。
    url = str(request.url)
    assert url.startswith(
        "https://matrix.example.org/_matrix/client/v3/rooms/"
        "%21abc%3Aexample.org/send/m.room.message/"
    )
    assert "!abc:example.org" not in url  # 未编码的 room id 会被服务端当成别的资源
    assert request.headers["Authorization"] == "Bearer tok"
    assert json.loads(request.content) == {"msgtype": "m.text", "body": "hi 世界"}

    # 重试复用同一个 txn：5xx 之后第二次请求的 URL 必须与第一次**完全一致**
    attempts: list[str] = []

    def flaky(request_: httpx.Request) -> httpx.Response:
        attempts.append(str(request_.url))
        return httpx.Response(503 if len(attempts) == 1 else 200)

    retrying = platforms.matrix_adapter(
        homeserver="https://matrix.example.org", token="tok",
        transport=httpx.MockTransport(flaky),
    )
    assert retrying.send_text("!abc:example.org", "hi").ok
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]


# ── 未配置：报错要点名该平台自己的环境变量 ──────────────────────────────────


@pytest.mark.parametrize(
    ("factory", "expected_env"),
    [
        (lambda: platforms.mattermost_adapter(url="", token=""), "MATTERMOST_URL"),
        (lambda: platforms.line_adapter(token=""), "LINE_CHANNEL_ACCESS_TOKEN"),
        (lambda: platforms.whatsapp_cloud_adapter(token="", phone_number_id=""),
         "WHATSAPP_CLOUD_TOKEN"),
        (lambda: platforms.sms_adapter(account_sid="", auth_token="", from_number=""),
         "TWILIO_ACCOUNT_SID"),
        (lambda: platforms.homeassistant_adapter(url="", token=""), "HASS_URL"),
        (lambda: platforms.matrix_adapter(homeserver="", token=""), "MATRIX_HOMESERVER"),
    ],
)
def test_unconfigured_adapter_names_its_own_env_var(factory, expected_env):
    """未配置时 ``ok=False``，且提示的是**这个平台**的变量名，不是别人的。"""
    seen, transport = _capture()
    adapter = factory()
    adapter.transport = transport  # 未配置时不该发请求；给了 transport 也不会被用到
    result = adapter.send_text("ch", "hi")
    assert result.ok is False
    assert expected_env in result.error
    assert "NTFY_URL" not in result.error  # 写死别人的变量名等于让人去改不存在的配置
    assert seen == []  # 关键：**一个请求都没发**
    health = adapter.health()
    assert health.ok is True  # 未配置 = 未启用，不是故障
    assert health.extra["configured"] is False
    assert health.extra["missing_env"]


# ── PLATFORMS 声明与实际一致 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "name", ["mattermost", "line", "whatsapp", "sms", "homeassistant", "matrix"]
)
def test_new_platform_specs_are_honest(name):
    """已实现的条目必须填 adapter、写清 scope、**清空 reason**。"""
    spec = platforms.PLATFORMS[platforms.Platform(name)]
    assert spec.implemented is True
    assert spec.adapter
    assert spec.scope.strip()
    assert spec.reason == ""
    # scope 必须自陈"仅出站"——否则会被读成和 Hermes 一样双向全功能
    assert "仅出站" in spec.scope
    assert spec.credentials


def test_implemented_count_is_nineteen():
    """17 → 19：`email`（SMTP）与 `irc`（裸 socket）本轮补上，两者都能离线验证。"""
    assert len(platforms.IMPLEMENTED_PLATFORMS) == 19
    pending = [
        platform.value
        for platform in platforms.ALL_PLATFORMS
        if not platforms.PLATFORMS[platform].implemented
    ]
    assert pending == ["buzz", "photon", "simplex"]


def test_rest_platform_table_documents_every_new_adapter():
    """`REST_PLATFORMS` 是"可核对的事实表"：6 个新平台都要在里面。"""
    assert len(platforms.REST_PLATFORMS) == 6
    for platform in platforms.REST_PLATFORMS:
        assert platforms.PLATFORMS[platform].implemented
        assert platforms.PLATFORMS[platform].adapter


# ── orbit peer ──────────────────────────────────────────────────────────────


def _add_peer(tmp_path, name: str = "spark", url: str = "http://spark.test:8377") -> None:
    assert _run(tmp_path, "peer", "add", name, "--url", url, "--key", "k1") == 0


def test_cli_peer_dm_sends_and_prints_reply(tmp_path, capsys, monkeypatch):
    """``peer dm`` 真发一次：URL / Bearer / body 三条都对，回复被打印出来。"""
    _add_peer(tmp_path)
    capsys.readouterr()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"reply": "磁盘一切正常"})

    monkeypatch.setattr(platform_extra, "PEER_TRANSPORT", httpx.MockTransport(handler))
    assert _run(tmp_path, "peer", "dm", "spark", "磁盘状态？") == 0
    out = capsys.readouterr().out
    assert "磁盘一切正常" in out
    request = seen[0]
    assert str(request.url) == "http://spark.test:8377/peer/dm"
    assert request.headers["Authorization"] == "Bearer k1"
    assert json.loads(request.content) == {"agent": "", "message": "磁盘状态？"}


def test_cli_peer_dm_supports_named_profile(tmp_path, capsys, monkeypatch):
    """``<peer>/<agent>`` 语法：``/`` 之后是具名 profile，要进 body。"""
    _add_peer(tmp_path)
    capsys.readouterr()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"reply": "ok"})

    monkeypatch.setattr(platform_extra, "PEER_TRANSPORT", httpx.MockTransport(handler))
    assert _run(tmp_path, "peer", "dm", "spark/researcher", "hi") == 0
    assert json.loads(seen[0].content)["agent"] == "researcher"


def test_cli_peer_dm_reports_http_failure(tmp_path, capsys, monkeypatch):
    _add_peer(tmp_path)
    capsys.readouterr()
    monkeypatch.setattr(
        platform_extra,
        "PEER_TRANSPORT",
        httpx.MockTransport(lambda request: httpx.Response(502, text="bad gateway")),
    )
    assert _run(tmp_path, "peer", "dm", "spark", "hi") == 1
    err = capsys.readouterr().err
    assert "502" in err


def test_cli_peer_dm_unknown_peer_is_actionable(tmp_path, capsys):
    assert _run(tmp_path, "peer", "dm", "nope", "hi") == 1
    assert "peer add" in capsys.readouterr().err


def test_cli_peer_run_and_stop_actually_contact_the_peer(tmp_path, capsys):
    """``run`` / ``stop`` 现在**真的去连对端**，连不上就报连接错误并指出查什么。

    这条原来断言的是「退出码 3 + peer run 未实现」——那是真实现出现之前的事实。
    `app/peer_client.py` 落地后，占位已让位，新契约是：
    **有真实现、真的发请求、失败时报可行动的错**。

    为什么这比「未实现」更有用：未实现只说明"没有"；连接错误说明"有，但对端没起"——
    后者直接告诉用户下一步该查什么。
    """
    _add_peer(tmp_path)
    capsys.readouterr()
    # 测试环境连不上 spark.test（DNS 解析失败），所以是普通失败而不是"未实现"
    assert _run(tmp_path, "peer", "run", "spark", "长任务") == 1
    err = capsys.readouterr().err
    assert "peer run" in err
    # 错误里必须给出**下一步查什么**，而不是只说失败
    assert "peer list" in err or "URL" in err

    assert _run(tmp_path, "peer", "stop", "spark", "run_abc") == 1
    err = capsys.readouterr().err
    assert "peer stop" in err


# ── orbit gateway ───────────────────────────────────────────────────────────


def test_cli_gateway_run_foreground_bounded(tmp_path, capsys, monkeypatch):
    """``gateway run`` 前台跑 n 个 tick 就退出，且锁被释放（不留下孤儿锁）。"""
    monkeypatch.setenv(platform_extra.RUN_TICKS_ENV, "1")
    assert _run(tmp_path, "gateway", "run") == 0
    out = capsys.readouterr().out
    assert "前台运行" in out
    # 退出后 dispatcher 必须是"未运行"：锁真的被释放了，而不是留在那里当孤儿锁。
    assert _run(tmp_path, "gateway", "status") == 0
    assert "未运行" in capsys.readouterr().out


def test_cli_gateway_run_refuses_second_dispatcher(tmp_path, capsys, monkeypatch):
    assert _run(tmp_path, "gateway", "start") == 0
    capsys.readouterr()
    monkeypatch.setenv(platform_extra.RUN_TICKS_ENV, "1")
    assert _run(tmp_path, "gateway", "run") == 1
    assert "已有 dispatcher 在运行" in capsys.readouterr().err


def test_cli_gateway_list_shows_single_instance(tmp_path, capsys):
    assert _run(tmp_path, "gateway", "list") == 0
    out = capsys.readouterr().out
    assert "数据目录" in out
    assert "没有 Hermes 的 profile 多实例" in out


def test_cli_gateway_migrate_legacy_never_deletes(tmp_path, capsys):
    """``migrate-legacy`` 只扫描与报告；``--yes`` 也不会真删（写入范围只在 data_dir）。"""
    assert _run(tmp_path, "gateway", "migrate-legacy", "--dry-run", "--yes") == 0
    out = capsys.readouterr().out
    assert "遗留" in out or "没有遗留" in out
    if "遗留单元" in out:
        assert "不会真的删除" in out


@pytest.mark.parametrize(
    ("argv", "needle"),
    [
        (("gateway", "install"), "gateway install 未实现"),
        (("gateway", "uninstall"), "gateway uninstall 未实现"),
        (("gateway", "enroll", "--token", "t"), "gateway enroll 未实现"),
    ],
)
def test_cli_gateway_unimplemented_paths_exit_3(tmp_path, capsys, argv, needle):
    assert _run(tmp_path, *argv) == 3
    err = capsys.readouterr().err
    assert needle in err
    # 必须写清"为什么做不到"，而不是只丢一句"未实现"
    assert "systemd" in err or "connector" in err or "data_dir" in err


# ── 注册表状态：只有真做成的才算 done ───────────────────────────────────────


def test_registry_states_are_honest():
    peer = REGISTRY.get("peer")
    gateway = REGISTRY.get("gateway")
    assert peer is not None and gateway is not None
    assert peer.child("dm").state == "done"
    assert peer.child("run").state == "partial"
    assert peer.child("stop").state == "partial"
    assert gateway.child("run").state == "done"
    assert gateway.child("list").state == "partial"
    assert gateway.child("migrate-legacy").state == "partial"
    for leaf in ("install", "uninstall", "enroll"):
        assert gateway.child(leaf).state == "partial", leaf
        assert gateway.child(leaf).handler is not None


def test_mark_unimplemented_paths_partial_is_idempotent():
    assert platform_extra.mark_unimplemented_paths_partial() == []
