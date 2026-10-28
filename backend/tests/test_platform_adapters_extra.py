"""MATRIX 12.5 的第二个增量：`simplex` 接上之后 19/22 → 20/22。

这份文件守两件事：

1. **新接上的那一个（`simplex`）四步都真跑**：拼命令 → 收发帧 → 解析响应 → 判错。
   daemon 用一个**假的连接器**（注入 `connector`）替代，于是整条链路在无网络、
   无 daemon 的环境里被完整走一遍 —— 这是"连接那一步可注入"的全部意义。
2. **没接上的那两个（`buzz` / `photon`）逐个断言 `reason` 非空**，并且原因里要写出
   **实测结论**（本机 `coincurve`/`ecdsa`/`secp256k1`/`grpcio` 一个都没有），
   而不是一句"未实现"。

为什么要单独一份文件而不是塞进 `test_platforms.py`：那一份守的是**22 个平台的契约
与账**（枚举齐不齐、scope 有没有写），这一份守的是**新加的那个适配器的行为**。
混在一起会让"改一个适配器"和"改契约"看起来是同一件事。
"""

from __future__ import annotations

import json

import pytest

from app import platforms


class FakeDaemon:
    """一个假的 simplex-chat daemon：记录收到的帧，按脚本吐回帧。

    只实现适配器真正用到的那三个方法（`send` / `recv` / `close`），
    多实现一个都会让"适配器到底依赖什么"变模糊。
    """

    def __init__(self, frames: list[object] | None = None) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._frames: list[object] = list(frames or [])

    def send(self, payload: str) -> None:
        self.sent.append(payload)

    def recv(self, timeout: float | None = None) -> object:
        if not self._frames:
            raise TimeoutError("假 daemon 没有更多帧")
        return self._frames.pop(0)

    def close(self) -> None:
        self.closed = True


def _adapter(frames: list[object] | None = None, *, url: str = "ws://127.0.0.1:5225"):
    """造一个连到假 daemon 的适配器（`connector` 注入，不碰网络）。"""
    daemon = FakeDaemon(frames)
    adapter = platforms.SimplexAdapter(url, connector=lambda _url, _timeout: daemon)
    return adapter, daemon


def _reply(corr_id: str, resp: dict[str, object]) -> str:
    return json.dumps({"corrId": corr_id, "resp": resp}, ensure_ascii=False)


# ── 1. 纯函数：命令拼装 ────────────────────────────────────────────────────


def test_simplex_target_adds_the_default_direct_prefix():
    """联系人 id 是 daemon 生成的不透明数字；漏前缀时补 `@`（单聊），**不猜 id**。"""
    assert platforms.simplex_target("42") == "@42"
    assert platforms.simplex_target("@42") == "@42"
    assert platforms.simplex_target("#7") == "#7"
    assert platforms.simplex_target("  ") == ""


def test_simplex_command_keeps_the_body_readable():
    """正文是行尾剩下的全部内容，所以只把换行压成空格 —— 逐字符转义会改掉用户看到的字。"""
    assert platforms.simplex_command("@42", "你好 世界") == "/_send @42 你好 世界"
    assert platforms.simplex_command("42", "第一行\n第二行") == "/_send @42 第一行 第二行"


# ── 2. 发送成功：帧真的发出去了，响应真的被解析了 ──────────────────────────


def test_send_text_puts_a_corr_id_frame_on_the_wire():
    adapter, daemon = _adapter()
    # 先造好响应帧：corrId 由适配器现场生成，所以用"回显"的方式对不上 ——
    # 这里用 daemon 的 send 钩子把 corrId 记下来再回填。
    original_send = daemon.send

    def send_and_reply(payload: str) -> None:
        original_send(payload)
        corr_id = json.loads(payload)["corrId"]
        daemon._frames.append(
            _reply(
                corr_id,
                {
                    "type": "newChatItems",
                    "chatItems": [{"chatItem": {"meta": {"itemId": 99}}}],
                },
            )
        )

    daemon.send = send_and_reply  # type: ignore[method-assign]
    result = adapter.send_text("42", "你好，世界")
    assert result.ok is True
    assert result.platform == "simplex"
    assert result.channel == "@42"
    assert result.message_id == "99", "消息 id 要取 daemon 给的，不要自己造"
    frame = json.loads(daemon.sent[0])
    assert frame["cmd"] == "/_send @42 你好，世界"
    assert frame["corrId"], "每条命令必须带 corrId，否则无法把响应配对回来"


def test_send_text_skips_unrelated_pushed_frames():
    """daemon 会主动推订阅事件 —— corrId 对不上的帧必须跳过，而不是当成响应。"""
    adapter, daemon = _adapter()
    adapter.start()

    def send_and_reply(payload: str) -> None:
        daemon.sent.append(payload)
        corr_id = json.loads(payload)["corrId"]
        daemon._frames.extend(
            [
                "daemon: starting up",  # 非 JSON 的启动横幅
                json.dumps({"corrId": "someone-else", "resp": {"type": "newChatItems"}}),
                _reply(corr_id, {"type": "newChatItems", "chatItems": []}),
            ]
        )

    daemon.send = send_and_reply  # type: ignore[method-assign]
    result = adapter.send_text("@42", "hi")
    assert result.ok is True
    assert result.detail["resp_type"] == "newChatItems"


# ── 3. 失败分支：每一条都要说清"为什么" ────────────────────────────────────


def test_chat_cmd_error_becomes_a_failed_result():
    """daemon 的 `chatCmdError` 要翻成 `ok=False` 并带上它给的 errorType。"""
    adapter, daemon = _adapter()
    original_send = daemon.send

    def send_and_reply(payload: str) -> None:
        original_send(payload)
        corr_id = json.loads(payload)["corrId"]
        daemon._frames.append(
            _reply(
                corr_id,
                {
                    "type": "chatCmdError",
                    "chatError": {"errorType": {"type": "noContact", "contactId": 42}},
                },
            )
        )

    daemon.send = send_and_reply  # type: ignore[method-assign]
    result = adapter.send_text("42", "hi")
    assert result.ok is False
    assert "noContact" in result.error, "原因要原样带出来，不能只说'发送失败'"
    assert result.detail["resp_type"] == "chatCmdError"


def test_missing_response_within_max_frames_fails_and_drops_the_connection():
    """daemon 一直不回应 corrId：有限帧之后放弃，并把连接丢掉（复用只会继续错）。"""
    adapter, daemon = _adapter([json.dumps({"corrId": "other"}) for _ in range(5)])
    adapter.start()
    result = adapter.send_text("42", "hi")
    assert result.ok is False
    assert "通信失败" in result.error
    assert daemon.closed is True, "出错之后必须断开，否则下一次还会读到错位的帧"


def test_unconfigured_adapter_explains_which_env_var():
    adapter = platforms.SimplexAdapter("")
    assert adapter.available() is False
    result = adapter.send_text("42", "hi")
    assert result.ok is False
    assert "SIMPLEX_WS_URL" in result.error
    health = adapter.health()
    assert health.ok is False and "SIMPLEX_WS_URL" in health.detail


def test_empty_channel_or_text_is_rejected_with_a_reason():
    adapter, _daemon = _adapter()
    assert "联系人 id" in adapter.send_text("", "hi").error
    assert "正文是空的" in adapter.send_text("42", "   ").error


# ── 4. 生命周期与能力边界 ──────────────────────────────────────────────────


def test_start_is_idempotent_and_stop_is_repeatable():
    adapter, daemon = _adapter()
    adapter.start()
    adapter.start()  # 第二次不该再开一条连接
    assert adapter.health().detail.count("连接已建立") == 1
    adapter.stop()
    assert daemon.closed is True
    adapter.stop()  # 契约要求：重复 stop 不报错
    assert adapter.health().ok is True


def test_send_media_is_not_supported_not_an_exception():
    """不支持的能力**返回** `NotSupported`（Hermes 的约定：降级，不抛）。"""
    adapter, _daemon = _adapter()
    outcome = adapter.send_media("@42", "x.png", caption="图")
    assert isinstance(outcome, platforms.NotSupported)
    assert outcome.capability == "send_media"


def test_default_adapters_include_simplex():
    pool = platforms.default_adapters(None)
    assert isinstance(pool["simplex"], platforms.SimplexAdapter)
    assert pool["simplex"].platform is platforms.Platform.SIMPLEX
    resolved = platforms.resolve_adapter("simplex", pool)
    assert isinstance(resolved, platforms.SimplexAdapter)


# ── 5. 账：20/22，剩两个逐个说明原因 ───────────────────────────────────────


def test_simplex_spec_says_outbound_only():
    spec = platforms.PLATFORMS[platforms.Platform.SIMPLEX]
    assert spec.implemented is True
    assert spec.adapter == "simplex"
    assert spec.reason == ""
    assert "仅出站" in spec.scope
    # 边界要写清：入站 / 附件 / 群组都没做，且联系人 id 是 daemon 生成的不透明值
    assert "无入站" in spec.scope
    assert "不透明" in spec.scope


def test_implemented_platforms_are_twenty_of_twenty_two():
    assert len(platforms.IMPLEMENTED_PLATFORMS) == 20
    pending = [
        platform.value
        for platform in platforms.ALL_PLATFORMS
        if not platforms.PLATFORMS[platform].implemented
    ]
    assert pending == ["buzz", "photon"]


@pytest.mark.parametrize("name", ["buzz", "photon"])
def test_unimplemented_platforms_name_the_missing_dependency(name):
    """未实现的每个都要有**非空且具体**的 reason（含实测结论），不能只说"未实现"。"""
    spec = platforms.PLATFORMS[platforms.Platform(name)]
    assert spec.implemented is False
    assert spec.adapter == ""
    assert len(spec.reason.strip()) >= 10
    assert spec.credentials, "没写需要哪些凭证，用户下一步只能去翻源码"
    adapter = platforms.UnimplementedAdapter(name)
    with pytest.raises(platforms.PlatformNotImplemented) as excinfo:
        adapter.send_text("room-1", "hi")
    assert spec.reason[:20] in str(excinfo.value)


def test_buzz_reason_records_the_missing_crypto_libraries():
    """buzz 不做是因为**本机没有 secp256k1 的实现**，不是"懒得写"。"""
    reason = platforms.PLATFORMS[platforms.Platform.BUZZ].reason
    for package in ("coincurve", "ecdsa", "secp256k1"):
        assert package in reason, f"reason 里要点名缺哪个包：{package}"


def test_photon_reason_records_the_missing_grpc():
    reason = platforms.PLATFORMS[platforms.Platform.PHOTON].reason
    assert "grpcio" in reason
    assert "sidecar" in reason


def test_simplex_inbound_is_still_listed_as_missing():
    """出站有了，入站**仍然没有** —— 这张表必须继续如实说。"""
    assert platforms.Platform.SIMPLEX in platforms.INBOUND_MISSING
    assert "订阅" in platforms.INBOUND_MISSING[platforms.Platform.SIMPLEX]
