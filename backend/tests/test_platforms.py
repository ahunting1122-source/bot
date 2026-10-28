"""平台层的契约测试：22 个平台声明、基类契约、三个适配器、回调 id。

这一层最容易"看起来做了其实没有"，所以测试盯着三件事：

1. **数字**：22 个平台 / 3 个已实现 / 7 必选 + 5 可选发送 + 5 交互 + 5 边缘 hook。
   数字错了说明契约被改过，而契约被改过通常意味着第三方接入会踩坑。
2. **降级不抛**：交互 UX 不支持时必须**返回** ``NotSupported``，不能抛——抛出去
   等于"带按钮的回复整条发不出去"。
3. **不打真网**：WebhookAdapter 的成功 / 5xx 重试 / 超时三条路径全走
   ``httpx.MockTransport``。本机无网络，任何真实请求都是 bug。
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.platforms import (
    ALL_PLATFORMS,
    APPROVAL_CHOICES,
    CALLBACK_APPROVAL,
    CALLBACK_CLARIFY,
    CALLBACK_SLASH,
    IMPLEMENTED_PLATFORMS,
    PLATFORMS,
    SLASH_CHOICES,
    ConsoleAdapter,
    FileAdapter,
    NotSupported,
    Platform,
    PlatformAdapter,
    PlatformNotImplemented,
    SendResult,
    SendTarget,
    UnimplementedAdapter,
    UxCapability,
    WebhookAdapter,
    build_callback_id,
    default_adapters,
    ntfy_adapter,
    parse_callback_id,
    parse_target,
    resolve_adapter,
)

# ── 平台声明 ────────────────────────────────────────────────────────────────


def test_there_are_exactly_22_platforms():
    """22 是 Hermes ``plugins/platforms/`` 的真实数量，少一个就是漏了一层接入面。"""
    assert len(list(Platform)) == 22
    assert len(ALL_PLATFORMS) == 22
    assert set(PLATFORMS) == set(ALL_PLATFORMS)
    assert {platform.value for platform in Platform} == {
        "a2a",
        "buzz",
        "dingtalk",
        "discord",
        "email",
        "feishu",
        "google_chat",
        "homeassistant",
        "irc",
        "line",
        "matrix",
        "mattermost",
        "ntfy",
        "photon",
        "raft",
        "simplex",
        "slack",
        "sms",
        "teams",
        "telegram",
        "wecom",
        "whatsapp",
    }


def test_every_platform_has_a_description_and_label():
    for platform in ALL_PLATFORMS:
        spec = PLATFORMS[platform]
        assert spec.label.strip(), f"{platform} 缺展示名"
        assert len(spec.description.strip()) >= 8, f"{platform} 的中文说明太短：{spec.description!r}"
        assert spec.platform is platform


def test_implemented_platforms_are_exactly_those_with_adapters():
    """已实现的平台集合 = `IMPLEMENTED_PLATFORMS`，且每个都必须说清**覆盖范围**。

    这条从「只有 3 个」改成「集合自洽」：数量本身不是契约，**声明与实现一致**才是。
    20 个已实现全是「仅出站」（simplex 只做出站文本），`scope` 必须写清没有入站、
    没有媒体、没有按钮——否则会被读成和 Hermes 一样全功能。
    """
    implemented = [platform for platform in ALL_PLATFORMS if PLATFORMS[platform].implemented]
    assert len(implemented) == len(IMPLEMENTED_PLATFORMS) == 20
    assert set(implemented) == set(IMPLEMENTED_PLATFORMS)
    assert {platform.value for platform in implemented} == {
        "ntfy",
        "raft",
        "a2a",
        "slack",
        "discord",
        "telegram",
        "feishu",
        "dingtalk",
        "wecom",
        "google_chat",
        "teams",
        # 6 个 REST 平台（Bearer/Basic 认证 + 路径里的资源 id + form-encoded body）
        "mattermost",
        "line",
        "whatsapp",
        "sms",
        "homeassistant",
        "matrix",
        # 2 个标准库协议平台（SMTP / 裸 socket，连接那一步可注入所以离线可验）
        "email",
        "irc",
        # 1 个 WebSocket 协议平台（本地 simplex-chat daemon 的 CLI 命令通道）
        "simplex",
    }
    for platform in implemented:
        spec = PLATFORMS[platform]
        assert spec.adapter, f"{platform} 已实现却没登记适配器名"
        assert spec.scope.strip(), f"{platform} 已实现却没写覆盖范围（会被误读成全功能）"
        assert not spec.reason, f"{platform} 已实现不该有未实现原因"


def test_unimplemented_platforms_explain_reason_and_credentials():
    """未实现的 2 个必须说清"为什么"与"缺什么凭证"——只说"未实现"等于没说。"""
    pending = [platform for platform in ALL_PLATFORMS if not PLATFORMS[platform].implemented]
    assert len(pending) == 2
    assert {platform.value for platform in pending} == {
        "buzz",
        "photon",
    }
    for platform in pending:
        spec = PLATFORMS[platform]
        assert len(spec.reason.strip()) >= 10, f"{platform} 缺未实现原因"
        assert spec.credentials, f"{platform} 没写需要哪些凭证"
        assert not spec.adapter


# ── 基类契约 ────────────────────────────────────────────────────────────────


def test_base_contract_method_counts():
    """7 必选 / 5 可选发送 / 5 交互 UX / 5 边缘 hook —— 数字就是契约本身。"""
    assert len(PlatformAdapter.REQUIRED_ATTRS) + len(PlatformAdapter.REQUIRED_METHODS) == 7
    assert len(PlatformAdapter.OPTIONAL_SEND_METHODS) == 5
    assert len(PlatformAdapter.INTERACTION_METHODS) == 5
    assert len(PlatformAdapter.EDGE_HOOKS) == 5
    for name in (*PlatformAdapter.REQUIRED_METHODS, *PlatformAdapter.OPTIONAL_SEND_METHODS,
                 *PlatformAdapter.INTERACTION_METHODS, *PlatformAdapter.EDGE_HOOKS):
        assert callable(getattr(PlatformAdapter, name)), f"基类缺方法 {name}"


def test_standalone_sender_fn_is_mandatory():
    """缺 ``standalone_sender_fn`` 会让 cron 投递**静默失败**，所以它是必选。

    这里验证的是"必选"这件事本身：只实现其它方法、不实现它，类建不起来
    （TypeError），而不是等到半夜 cron 悄悄丢消息。
    """

    class _Incomplete(PlatformAdapter):  # noqa: D401 - 测试用桩
        name = "incomplete"

        def start(self) -> None:
            return None

        def stop(self) -> None:
            return None

        def send_text(self, channel: str, text: str) -> SendResult:
            return SendResult(ok=True)

        def send_media(self, channel: str, path: str, *, caption: str = ""):
            return NotSupported("send_media")

        def health(self):
            from app.platforms import AdapterHealth

            return AdapterHealth(ok=True)

    with pytest.raises(TypeError):
        _Incomplete()


def test_all_three_adapters_can_send_text(tmp_path, capsys):
    """Console / File / Webhook 三个适配器都要能真发一条文本。"""
    console = ConsoleAdapter()
    assert console.send_text("#dev", "hello console").ok
    assert "hello console" in capsys.readouterr().out

    file_adapter = FileAdapter(tmp_path)
    assert file_adapter.send_text("#dev", "hello file").ok
    lines = (tmp_path / "outbox.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["text"] == "hello file"

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"id": "m1"})

    webhook = WebhookAdapter("https://example.test/hook", transport=httpx.MockTransport(handler))
    result = webhook.send_text("#dev", "hello webhook")
    assert result.ok and result.message_id == "m1"
    assert json.loads(seen[0].content)["text"] == "hello webhook"


def test_unsupported_ux_returns_not_supported_instead_of_raising():
    """交互 UX 不支持时**返回** ``NotSupported``（优雅降级），绝不抛异常。"""
    adapter = ConsoleAdapter()
    outcomes = [
        adapter.send_typing("#dev"),
        adapter.send_reaction("#dev", "1", "👍"),
        adapter.edit_message("#dev", "1", "x"),
        adapter.delete_message("#dev", "1"),
        adapter.send_buttons("#dev", "x", (("a", "cl:1:0"),)),
        adapter.send_clarify("#dev", "选哪个？", ("a", "b")),
        adapter.send_exec_approval("#dev", "rm -rf /", approval_id="1"),
        adapter.send_slash_confirm("#dev", "确认", "要执行吗", confirm_id="1"),
        adapter.send_model_picker("#dev", ("gpt", "claude")),
        adapter.send_choice_picker("#dev", ("a", "b")),
    ]
    for outcome in outcomes:
        assert isinstance(outcome, NotSupported)
        assert outcome.ok is False
        assert outcome.capability
        assert outcome.hint


def test_ux_capability_probe_defaults_to_nothing():
    adapter = ConsoleAdapter()
    assert adapter.ux_capabilities() == frozenset()
    for capability in UxCapability:
        assert adapter.supports_ux(capability) is False
    assert adapter.supports_ux("不存在的类别") is False


def test_edge_hooks_are_noops_by_default():
    """5 个边缘 hook 默认空实现：网关可以无条件调用，不必到处判空。"""
    adapter = ConsoleAdapter()
    assert adapter.on_connect() is None
    assert adapter.on_disconnect() is None
    assert adapter.on_error(RuntimeError("boom")) is None
    assert adapter.on_rate_limited(1.5) is None
    assert adapter.on_message("#dev", "hi", user_id="u1") is None


def test_adapter_describe_reports_contract():
    described = ConsoleAdapter().describe()
    assert described["name"] == "console"
    assert len(described["required"]) == 6  # 7 必选里 name 是属性，不在方法列表里
    assert len(described["optional_send"]) == 5
    assert len(described["interaction"]) == 5
    assert len(described["edge_hooks"]) == 5


# ── 按钮回调 id 约定 ────────────────────────────────────────────────────────


def test_callback_ids_round_trip_for_all_three_prefixes():
    """三种前缀的构造/解析必须往返一致（顺序不同：``sc`` 是 choice 在前）。"""
    cases = [
        (CALLBACK_CLARIFY, "clarify-1", "2", "cl:clarify-1:2"),
        (CALLBACK_CLARIFY, "clarify-1", "other", "cl:clarify-1:other"),
        (CALLBACK_APPROVAL, "appr-9", "deny", "appr:appr-9:deny"),
        (CALLBACK_SLASH, "confirm-3", "once", "sc:once:confirm-3"),
    ]
    for kind, ident, choice, expected in cases:
        raw = build_callback_id(kind, ident, choice)
        assert raw == expected
        parsed = parse_callback_id(raw)
        assert parsed is not None
        assert (parsed.kind, parsed.ident, parsed.choice) == (kind, ident, choice)
        assert parsed.raw == raw


def test_callback_choices_match_telegram_buttons():
    """选项词表照 telegram 适配器的真实按钮：审批 4 个、斜杠确认 3 个。"""
    assert APPROVAL_CHOICES == ("once", "session", "always", "deny")
    assert SLASH_CHOICES == ("once", "always", "cancel")
    for choice in APPROVAL_CHOICES:
        assert parse_callback_id(build_callback_id(CALLBACK_APPROVAL, "7", choice)).choice == choice
    for choice in SLASH_CHOICES:
        assert parse_callback_id(build_callback_id(CALLBACK_SLASH, "7", choice)).choice == choice


def test_parse_callback_id_rejects_junk_without_raising():
    """回调是外部输入：陌生 id 返回 ``None``，不能让回调处理器崩掉。"""
    for raw in ("", "nope", "cl:", "cl:1", "cl:1:2:3", "zz:1:2", "cl::2", "cl:1:"):
        assert parse_callback_id(raw) is None


def test_build_callback_id_validates_input():
    with pytest.raises(ValueError):
        build_callback_id("zz", "1", "2")
    with pytest.raises(ValueError):
        build_callback_id(CALLBACK_CLARIFY, "1:2", "3")
    with pytest.raises(ValueError):
        build_callback_id(CALLBACK_CLARIFY, "", "3")


def test_legacy_telegram_approval_prefix_is_parsed():
    """0.21.0-cn.18 的 telegram 用 ``ea:<choice>:<id>``；文档写 ``appr:``，两种都认。"""
    parsed = parse_callback_id("ea:session:42")
    assert parsed is not None
    assert (parsed.kind, parsed.ident, parsed.choice) == ("ea", "42", "session")


# ── WebhookAdapter：三种网络结果（全部走 MockTransport，绝不打真网） ────────


def test_webhook_success_uses_injected_transport():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        return httpx.Response(200, json={"ok": True}, headers={"id": "42"})

    adapter = WebhookAdapter("https://example.test/x", transport=httpx.MockTransport(handler))
    result = adapter.send_text("ch", "hi")
    assert result.ok
    assert result.detail["attempts"] == 1
    assert result.detail["status"] == 200


def test_webhook_retries_on_5xx_then_succeeds():
    """5xx 是可重试的：第二次 200 就该成功，且 attempts 记 2。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503 if calls["n"] == 1 else 200, headers={"id": "ok"})

    adapter = WebhookAdapter(
        "https://example.test/x", transport=httpx.MockTransport(handler), attempts=3
    )
    result = adapter.send_text("ch", "hi")
    assert result.ok
    assert result.detail["attempts"] == 2
    assert calls["n"] == 2


def test_webhook_does_not_retry_on_4xx():
    """4xx 是客户端错，重试没有意义——一次就放弃，省掉无谓的等待。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    adapter = WebhookAdapter(
        "https://example.test/x", transport=httpx.MockTransport(handler), attempts=3
    )
    result = adapter.send_text("ch", "hi")
    assert not result.ok
    assert "404" in result.error
    assert calls["n"] == 1


def test_webhook_gives_up_after_timeouts():
    """超时三次 → 失败，且 attempts 与配置一致（账本要对得上尝试次数）。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectTimeout("timed out")

    adapter = WebhookAdapter(
        "https://example.test/x", transport=httpx.MockTransport(handler), attempts=2
    )
    result = adapter.send_text("ch", "hi")
    assert not result.ok
    assert "超时" in result.error
    assert result.detail["attempts"] == 2
    assert calls["n"] == 2


def test_webhook_without_url_fails_without_network():
    """没配 URL 时**不发请求**，直接给出可行动的报错。"""
    adapter = WebhookAdapter("", transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    result = adapter.send_text("ch", "hi")
    assert not result.ok
    # 文案改成「未配置出站地址 + 具体环境变量名」：写死 `NTFY_URL` 会让人去改
    # 一个跟自己平台无关的配置。
    assert "未配置出站地址" in result.error
    assert "NTFY_URL" in result.error
    health = adapter.health()
    assert health.ok and health.extra["configured"] is False


def test_webhook_body_shapes():
    """三种请求体形状：ntfy 纯文本 / 通用 JSON / a2a JSON-RPC。"""
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200)

    transport = httpx.MockTransport(handler)
    for shape, name in (("text", "ntfy"), ("json", "raft"), ("jsonrpc", "a2a")):
        adapter = WebhookAdapter(f"https://example.test/{shape}", name=name, body_shape=shape,
                                 transport=transport)
        assert adapter.send_text("ch", "body").ok
    assert captured[0].content == b"body"
    assert json.loads(captured[1].content) == {"text": "body", "channel": "ch"}
    rpc = json.loads(captured[2].content)
    assert rpc["method"] == "message/send"
    assert rpc["params"]["message"]["parts"][0]["text"] == "body"


def test_webhook_media_is_not_supported_not_raised():
    adapter = WebhookAdapter("https://example.test/x")
    outcome = adapter.send_media("ch", "/tmp/a.png")
    assert isinstance(outcome, NotSupported)


# ── 未实现平台 ──────────────────────────────────────────────────────────────


def test_unimplemented_adapter_raises_with_reason_and_credentials():
    # 用 `buzz` 而不是 telegram：telegram 现在有出站适配器了，拿它当"未实现"样本
    # 会掩盖真正的回归（未实现的平台被悄悄标成实现了）。
    adapter = UnimplementedAdapter(Platform.BUZZ)
    assert adapter.name == "buzz"
    with pytest.raises(PlatformNotImplemented) as excinfo:
        adapter.send_text("ch", "hi")
    message = str(excinfo.value)
    assert "BUZZ_PRIVATE_KEY" in message
    assert "未实现" in message
    # 另外几条发送路径也必须拒绝，不能只拦 send_text
    for call in (
        lambda: adapter.send_typing("ch"),
        lambda: adapter.send_buttons("ch", "x", ()),
        lambda: adapter.standalone_sender_fn(),
        lambda: adapter.start(),
    ):
        with pytest.raises(PlatformNotImplemented):
            call()


def test_resolve_adapter_rejects_unknown_platform_name():
    """未知平台名（不是 22 个之一、也不是 console/file）当场抛，并说清"不是 22 个之一"。"""
    with pytest.raises(PlatformNotImplemented) as excinfo:
        resolve_adapter("wechat-not-a-platform", {})
    assert "不是 Orbit 支持的 22 个平台" in str(excinfo.value)
    # 已知但未实现 → 返回占位适配器（展示用），发送时才抛。
    placeholder = resolve_adapter("wecom", {})
    assert isinstance(placeholder, UnimplementedAdapter)
    with pytest.raises(PlatformNotImplemented):
        placeholder.send_text("ch", "hi")


def test_default_adapters_cover_every_implemented_platform_plus_dev_sinks(tmp_path):
    """默认适配器池 = 2 个开发旁路 + **全部 19 个已实现平台**，不多不少。

    唯一的例外是 `whatsapp_cloud`：WhatsApp 的平台枚举名是 `whatsapp`，而适配器自己的
    注册名是 `whatsapp_cloud`（Meta Cloud API）。两个键指向**同一个适配器对象**，
    这样 `--to whatsapp:…` 与 `--to whatsapp_cloud:…` 都能投递。
    """
    adapters = default_adapters(tmp_path)
    expected = (
        {"console", "file", "whatsapp_cloud"}
        | {platform.value for platform in IMPLEMENTED_PLATFORMS}
    )
    assert set(adapters) == expected
    for platform in IMPLEMENTED_PLATFORMS:
        assert adapters[platform.value].platform is platform
    assert adapters["whatsapp_cloud"] is adapters["whatsapp"]


def test_ntfy_adapter_is_plain_text_publish():
    adapter = ntfy_adapter(url="https://ntfy.test/topic")
    assert adapter.body_shape == "text"
    assert adapter.platform is Platform.NTFY


# ── 目标语法 ────────────────────────────────────────────────────────────────


def test_parse_target_variants():
    """``platform`` / ``platform:chat_id`` / ``platform:chat_id:thread_id``。"""
    assert parse_target("telegram") == SendTarget(platform="telegram")
    assert parse_target("telegram:-100123") == SendTarget(platform="telegram", channel="-100123")
    assert parse_target("telegram:-100123:17585") == SendTarget(
        platform="telegram", channel="-100123", thread="17585"
    )
    assert parse_target("discord:#ops").raw == "discord:#ops"
    assert parse_target("  NTFY  ").platform == "ntfy"


def test_parse_target_rejects_bad_shapes():
    for raw in ("", "  ", ":chat", "a:b:c:d"):
        with pytest.raises(ValueError):
            parse_target(raw)
