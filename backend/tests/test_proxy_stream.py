"""MATRIX 4.7 / 4.2：代理的流式 / 重试 / 并发 / 鉴权，以及 SigV4 的官方向量验证。

四段各管一件事，**全部离线**（不发任何真实请求）：

1. **流式**：上游 SSE 逐事件翻成 OpenAI `chat.completion.chunk`，第一帧给 role、
   中间帧给 content、最后一帧给 `finish_reason`，然后 `data: [DONE]`；
2. **重试**：只对 5xx / 连接错误 / 超时重试，**4xx 立刻失败**；每次尝试都记在
   `orbit_attempts` 里（失败路径也要留痕）；
3. **并发**：信号量超了排队而不是拒绝——用两个线程实测"同时最多 1 路"；
4. **鉴权**：没有 token 时**只能绑回环地址**（`ensure_bindable` 是硬错误），
   有 token 时校验 `Authorization: Bearer`。

SigV4 那一段用 **AWS 官方材料的两组向量**逐字段比对（canonical request / string to
sign / signature 三段都断言）：只比最终签名的话，规范化错在哪一步永远查不出来。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime

import httpx
import pytest

from app import providers as pv
from app import proxy as px

# ── 夹具 ────────────────────────────────────────────────────────────────────


def _request(**overrides) -> dict:
    payload = {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "你好"}],
        "stream": True,
    }
    payload.update(overrides)
    return payload


def _anthropic_stream() -> list[str]:
    """一份真实的 Anthropic SSE 事件序列（文本两段 + stop_reason + usage）。"""
    return [
        "event: message_start",
        'data: {"type":"message_start","message":{"id":"msg_1","model":"claude-sonnet-5",'
        '"usage":{"input_tokens":9}}}',
        "",
        "event: content_block_start",
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}',
        "",
        "event: content_block_delta",
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你"}}',
        "",
        "event: content_block_delta",
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"好"}}',
        "",
        "event: message_delta",
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":2}}',
        "",
        "event: message_stop",
        'data: {"type":"message_stop"}',
    ]


def _gemini_stream() -> list[str]:
    return [
        'data: {"candidates":[{"content":{"parts":[{"text":"你"}]}}]}',
        'data: {"candidates":[{"content":{"parts":[{"text":"好"}]},'
        '"finishReason":"STOP"}],"usageMetadata":{"promptTokenCount":4,'
        '"candidatesTokenCount":2}}',
    ]


def _frames(chunks: list[bytes]) -> list[dict]:
    """把 SSE 帧解回 JSON（`[DONE]` 跳过）。顺带断言每一帧都是 `data: ...\\n\\n` 形状。"""
    out: list[dict] = []
    for frame in chunks:
        text = frame.decode("utf-8")
        assert text.startswith("data: ") and text.endswith("\n\n"), f"帧形状不对：{text!r}"
        body = text[len("data: ") : -2]
        if body == "[DONE]":
            continue
        out.append(json.loads(body))
    return out


# ── 1. 流式 ─────────────────────────────────────────────────────────────────


def test_stream_translates_anthropic_events_into_openai_chunks():
    """逐事件翻译：role → 两段文本 → finish_reason(带 usage) → [DONE]。"""
    server = px.ProxyServer(px.plan_for("anthropic"), stream_forward=lambda *a: _anthropic_stream())
    raw = list(server.handle_stream(_request()))

    assert raw[-1] == px.SSE_DONE
    chunks = _frames(raw)
    assert chunks[0]["object"] == "chat.completion.chunk"
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert "".join(
        chunk["choices"][0]["delta"].get("content", "") for chunk in chunks
    ) == "你好"
    assert all(chunk["choices"][0]["finish_reason"] is None for chunk in chunks[:-1])
    final = chunks[-1]
    assert final["choices"][0]["finish_reason"] == "stop"
    assert final["choices"][0]["delta"] == {}
    # usage 三个字段都要在：少了 total_tokens，客户端永远显示 0。
    assert final["usage"] == {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}


def test_stream_translates_gemini_increments():
    server = px.ProxyServer(px.plan_for("gemini"), stream_forward=lambda *a: _gemini_stream())
    chunks = _frames(list(server.handle_stream(_request())))
    assert chunks[0]["choices"][0]["delta"]["role"] == "assistant"
    assert "".join(
        chunk["choices"][0]["delta"].get("content", "") for chunk in chunks
    ) == "你好"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"


def test_stream_request_asks_the_upstream_for_a_stream():
    """`stream: true` 不再降级：Anthropic 收到 stream=True，Gemini 换到 SSE 方法名。"""
    seen: dict[str, object] = {}

    def anthropic(url, headers, body):
        seen["url"] = url
        seen["body"] = body
        return iter(_anthropic_stream())

    list(px.ProxyServer(px.plan_for("anthropic"), stream_forward=anthropic).handle_stream(_request()))
    assert seen["body"]["stream"] is True
    assert str(seen["url"]).endswith("/v1/messages")

    seen.clear()

    def gemini(url, headers, body):
        seen["url"] = url
        return iter(_gemini_stream())

    list(
        px.ProxyServer(
            px.plan_for("gemini", model_map={"gpt-4o-mini": "gemini-3.5-flash"}),
            stream_forward=gemini,
        ).handle_stream(_request())
    )
    assert str(seen["url"]).endswith("/models/gemini-3.5-flash:streamGenerateContent?alt=sse")


def test_stream_without_a_stream_forward_says_so_instead_of_faking():
    """没接流式转发时**不伪造空流**：客户端会一直等一个永远不来的 chunk。"""
    server = px.ProxyServer(px.plan_for("anthropic"))
    frames = list(server.handle_stream(_request()))
    body = _frames(frames)
    assert body[0]["error"]["code"] == "no_stream_upstream"
    assert frames[-1] == px.SSE_DONE


def test_stream_upstream_error_becomes_an_error_frame():
    """上游在流里报错 → 一帧 error（此时响应头早发出去了，改不了状态码）。"""

    def boom(url, headers, body):
        yield 'data: {"type":"error","error":{"message":"overloaded"}}'

    frames = list(px.ProxyServer(px.plan_for("anthropic"), stream_forward=boom).handle_stream(_request()))
    body = _frames(frames)
    assert body[0]["error"]["code"] == "proxy_error"
    assert "overloaded" in body[0]["error"]["message"]


def test_stream_retries_only_before_the_first_byte():
    """**已经吐过内容就不重试**：重试会让客户端收到两段拼接的文本（前一段没终止帧）。"""
    attempts: list[int] = []

    def flaky(url, headers, body):
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("connection reset")  # 还没吐任何帧 → 可重试
        yield 'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"你"}}'
        raise ConnectionError("断了")  # 已经吐过内容 → 不再重试

    server = px.ProxyServer(
        px.plan_for("anthropic"), stream_forward=flaky, sleep=lambda _d: None
    )
    body = _frames(list(server.handle_stream(_request())))
    assert len(attempts) == 2, "第一次连接失败应当重试一次"
    # 第一帧是 role（`delta.content` 为空串），文本在后面的帧里；最后一帧是 error。
    text = "".join(
        chunk["choices"][0]["delta"].get("content", "")
        for chunk in body
        if "choices" in chunk
    )
    assert text == "你"
    assert body[-1]["error"]["code"] == "upstream_error"
    assert "已开始输出" in body[-1]["error"]["message"]


# ── 2. 重试 ─────────────────────────────────────────────────────────────────


def _ok_response() -> dict:
    return {
        "id": "msg_1",
        "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": "好"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def test_retry_retries_5xx_and_records_every_attempt():
    calls: list[int] = []

    def flaky(url, headers, body):
        calls.append(1)
        if len(calls) < 3:
            raise px.ProxyUpstreamError(503, "upstream overloaded")
        return _ok_response()

    server = px.ProxyServer(px.plan_for("anthropic"), forward=flaky, sleep=lambda _d: None)
    result = server.handle(_request())

    assert result["choices"][0]["message"]["content"] == "好"
    assert len(calls) == 3
    attempts = result["orbit_attempts"]
    assert [item["ok"] for item in attempts] == [False, False, True]
    assert [item["status"] for item in attempts] == [503, 503, 200]
    assert attempts[0]["retriable"] is True
    assert attempts[0]["delay"] > 0  # 指数退避真的算过


def test_retry_never_retries_4xx():
    """4xx 立刻失败：请求本身有问题，再发一次只会把同一个错误要回来。"""
    calls: list[int] = []

    def bad(url, headers, body):
        calls.append(1)
        raise px.ProxyUpstreamError(400, "bad request")

    server = px.ProxyServer(px.plan_for("anthropic"), forward=bad, sleep=lambda _d: None)
    result = server.handle(_request())

    assert len(calls) == 1
    assert result["error"]["code"] == "upstream_error"
    assert "400" in result["error"]["message"]
    assert len(result["orbit_attempts"]) == 1
    assert result["orbit_attempts"][0]["retriable"] is False


@pytest.mark.parametrize("exc", [ConnectionError("reset"), TimeoutError("slow"), OSError("down")])
def test_retry_retries_connection_and_timeout_errors(exc):
    calls: list[int] = []

    def flaky(url, headers, body):
        calls.append(1)
        if len(calls) == 1:
            raise exc
        return _ok_response()

    server = px.ProxyServer(px.plan_for("anthropic"), forward=flaky, sleep=lambda _d: None)
    assert server.handle(_request())["choices"][0]["message"]["content"] == "好"
    assert len(calls) == 2


def test_retry_gives_up_after_max_attempts_and_still_reports_attempts():
    calls: list[int] = []

    def always_down(url, headers, body):
        calls.append(1)
        raise ConnectionError("no route")

    policy = px.RetryPolicy(max_attempts=2)
    server = px.ProxyServer(
        px.plan_for("anthropic"), forward=always_down, retry=policy, sleep=lambda _d: None
    )
    result = server.handle(_request())
    assert len(calls) == 2
    assert len(result["orbit_attempts"]) == 2
    assert result["error"]["code"] == "upstream_error"


def test_unexpected_exceptions_are_not_retried():
    """看不懂的异常不重试：`RuntimeError` 不是网络问题，重试只是拖慢报错。"""
    calls: list[int] = []

    def boom(url, headers, body):
        calls.append(1)
        raise RuntimeError("connection refused")

    server = px.ProxyServer(px.plan_for("anthropic"), forward=boom, sleep=lambda _d: None)
    result = server.handle(_request())
    assert len(calls) == 1
    assert "connection refused" in result["error"]["message"]


# ── 3. 并发 ─────────────────────────────────────────────────────────────────


def test_concurrency_semaphore_queues_instead_of_rejecting():
    """concurrency=1 时两个并发请求**排队**，不是第二个被拒；两个都要拿到结果。"""
    state = {"now": 0, "peak": 0}
    lock = threading.Lock()
    release = threading.Event()

    def slow(url, headers, body):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        release.wait(2.0)
        with lock:
            state["now"] -= 1
        return _ok_response()

    server = px.ProxyServer(px.plan_for("anthropic"), forward=slow, concurrency=1)
    results: list[dict] = []

    def run():
        results.append(server.handle(_request()))

    first = threading.Thread(target=run)
    first.start()
    for _ in range(100):  # 等第一个真的进到上游调用里
        if state["now"] == 1:
            break
        time.sleep(0.01)
    second = threading.Thread(target=run)
    second.start()
    time.sleep(0.15)
    release.set()
    first.join(5)
    second.join(5)

    assert len(results) == 2
    assert all("error" not in result for result in results), "排队不能变成拒绝"
    assert state["peak"] == 1, "信号量没生效：同时进了 2 路"


# ── 4. 鉴权 ─────────────────────────────────────────────────────────────────


def test_no_token_means_loopback_only():
    """**本模块最硬的约束**：没有 token 时绑非回环地址是硬错误，不是警告。"""
    with pytest.raises(px.ProxyError) as excinfo:
        px.ensure_bindable("0.0.0.0", "")
    message = str(excinfo.value)
    assert "回环" in message and "token" in message

    # 有 token 才放行非回环；回环无论如何都放行。
    assert px.ensure_bindable("0.0.0.0", "s3cret") == "0.0.0.0"
    assert px.ensure_bindable("127.0.0.1", "") == "127.0.0.1"
    assert px.ensure_bindable("", "") == px.DEFAULT_HOST


def test_token_is_read_from_the_data_dir_and_authorization_is_enforced(tmp_path):
    """token 从 `<data>/proxy.json` 读；配了就必须带 `Authorization: Bearer`。"""
    assert px.load_token(tmp_path) == "", "没有配置文件时不得凭空造 token"
    path = px.config_path(tmp_path)
    path.write_text(json.dumps({"token": "abc123"}), encoding="utf-8")
    assert px.load_token(tmp_path) == "abc123"

    server = px.ProxyServer(px.plan_for("anthropic"), token="abc123")
    assert server.authorize("Bearer abc123") is True
    assert server.authorize("bearer abc123") is True  # 大小写不敏感
    assert server.authorize("Bearer nope") is False
    assert server.authorize("abc123") is False, "缺 Bearer 前缀不算"
    # 没配 token 的代理一律放行（它只可能绑在回环上，见上一条用例）。
    assert px.ProxyServer(px.plan_for("anthropic")).authorize("") is True


def test_broken_proxy_config_falls_back_to_no_token(tmp_path):
    """坏配置文件读成空 token → 于是只能绑回环（收紧而不是放开）。"""
    px.config_path(tmp_path).write_text("{not json", encoding="utf-8")
    assert px.load_token(tmp_path) == ""


def test_stream_forward_is_wired_automatically_when_a_real_forward_is_injected():
    """`orbit proxy serve` 只注入 `forward=`：流式转发必须自动补上。

    不补的话 `stream: true` 会从"降级成整包"（旧行为，能用）变成"回一帧
    `no_stream_upstream` 错误"（退步）。这里读私有属性是刻意的：这是**唯一**能证明
    接线而不真的发一次网络请求的办法——本机无网络，真发只会得到一个连接错误。
    """
    server = px.ProxyServer(px.plan_for("anthropic"), forward=lambda *a: {})
    assert server._stream_forward is not None, "注入了 forward 却没接流式转发"
    # 完全没有转发函数时仍然是"明确说没接"，不伪造流。
    assert px.ProxyServer(px.plan_for("anthropic"))._stream_forward is None


def test_describe_reports_what_is_now_implemented():
    text = px.describe()
    for word in ("流式", "重试", "并发", "鉴权", "最小转发层"):
        assert word in text
    # 做到的部分也要说清边界，否则会被读成生产网关。
    assert "429" in text


# ── 5. SigV4：AWS 官方向量逐字段比对（MATRIX 4.2）──────────────────────────

#: 官方向量用的凭据（AWS 文档与 `aws-sig-v4-test-suite` 里公开的示例值，不是真凭据）。
AKID = "AKIDEXAMPLE"
SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
AMZ_NOW = datetime(2015, 8, 30, 12, 36, 0, tzinfo=UTC)


def test_sigv4_matches_the_aws_documented_iam_example():
    """AWS 文档「Signature Version 4 signing process」的 IAM ListUsers 示例。

    三段分别断言：canonical request 的**文本**、string to sign、最终签名。
    只比签名的话，"规范化错在哪一步"永远查不出来。
    """
    result = pv.sigv4_headers(
        "GET",
        "https://iam.amazonaws.com/?Action=ListUsers&Version=2010-05-08",
        {
            "content-type": "application/x-www-form-urlencoded; charset=utf-8",
            "host": "iam.amazonaws.com",
        },
        b"",
        access_key=AKID,
        secret_key=SECRET,
        region="us-east-1",
        service="iam",
        now=AMZ_NOW,
    )
    assert result.canonical_request == (
        "GET\n"
        "/\n"
        "Action=ListUsers&Version=2010-05-08\n"
        "content-type:application/x-www-form-urlencoded; charset=utf-8\n"
        "host:iam.amazonaws.com\n"
        "x-amz-date:20150830T123600Z\n"
        "\n"
        "content-type;host;x-amz-date\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert result.string_to_sign == (
        "AWS4-HMAC-SHA256\n"
        "20150830T123600Z\n"
        "20150830/us-east-1/iam/aws4_request\n"
        "f536975d06c0309214f805bb90ccff089219ecd68b2577efef23edd43b7e1a59"
    )
    assert result.signature == (
        "5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    )
    assert result.headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/20150830/us-east-1/iam/aws4_request, "
        "SignedHeaders=content-type;host;x-amz-date, "
        "Signature=5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7"
    )
    # host 必须参与签名，也必须出现在要发的头里。
    assert result.headers["host"] == "iam.amazonaws.com"


def test_sigv4_matches_the_aws_sig_v4_test_suite_get_vanilla():
    """`aws/aws-sig-v4-test-suite` 的 `get-vanilla` 用例（region/service 都是占位值）。"""
    result = pv.sigv4_headers(
        "GET",
        "https://example.amazonaws.com/",
        None,
        b"",
        access_key=AKID,
        secret_key=SECRET,
        region="us-east-1",
        service="service",
        now=AMZ_NOW,
    )
    assert result.signed_headers == "host;x-amz-date"
    assert result.canonical_request == (
        "GET\n"
        "/\n"
        "\n"
        "host:example.amazonaws.com\n"
        "x-amz-date:20150830T123600Z\n"
        "\n"
        "host;x-amz-date\n"
        "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )
    assert result.signature == (
        "5fa00fa31553b73ebf1942676e86291e8372ff2a2260956d9b8aae1d763fbf31"
    )


def test_sign_sigv4_is_a_pure_function_of_its_inputs():
    """同样的入参（含 `now`）永远同样的头——这正是它能被官方向量验证的原因。"""
    kwargs = dict(
        access_key=AKID,
        secret_key=SECRET,
        region="us-east-1",
        service="service",
        now=AMZ_NOW,
    )
    first = pv.sign_sigv4("GET", "https://example.amazonaws.com/", None, b"", **kwargs)
    second = pv.sign_sigv4("GET", "https://example.amazonaws.com/", None, b"", **kwargs)
    assert first == second
    # 换个时刻签名就必须变（时间戳进了 string to sign）。
    later = pv.sign_sigv4(
        "GET",
        "https://example.amazonaws.com/",
        None,
        b"",
        access_key=AKID,
        secret_key=SECRET,
        region="us-east-1",
        service="service",
        now=datetime(2015, 8, 30, 12, 36, 1, tzinfo=UTC),
    )
    assert later["authorization"] != first["authorization"]


def test_sigv4_requires_credentials_and_signs_the_session_token():
    with pytest.raises(pv.MissingCredential):
        pv.sign_sigv4(
            "GET",
            "https://example.amazonaws.com/",
            None,
            b"",
            access_key="",
            secret_key="",
            region="us-east-1",
            service="service",
        )
    signed = pv.sigv4_headers(
        "GET",
        "https://example.amazonaws.com/",
        None,
        b"",
        access_key=AKID,
        secret_key=SECRET,
        region="us-east-1",
        service="service",
        now=AMZ_NOW,
        session_token="tok-123",
    )
    # 临时凭据的 token 必须进签名，也必须发出去——否则 AWS 认不出是哪一次会话。
    assert "x-amz-security-token" in signed.signed_headers
    assert signed.headers["x-amz-security-token"] == "tok-123"


def test_uri_encode_and_query_canonicalisation_follow_rfc3986():
    assert pv.uri_encode("a b/c~d") == "a%20b%2Fc~d"
    assert pv.uri_encode("中") == "%E4%B8%AD"  # 按 UTF-8 字节、大写十六进制
    assert pv.uri_encode("/a/b", encode_slash=False) == "/a/b"
    # query 排序 + 重新转义（先解码再编码，避免 %2520）
    assert pv.canonical_query_string("b=2&a=1") == "a=1&b=2"
    assert pv.canonical_query_string("q=a%20b") == "q=a%20b"


# ── 6. Bedrock Converse：能签、能发、边界如实 ───────────────────────────────


def test_bedrock_available_checks_the_three_env_vars():
    ok, reason = pv.bedrock_available({})
    assert ok is False and "AWS_ACCESS_KEY_ID" in reason
    assert "凭据链" in reason, "必须说清不做凭据链发现，否则用户会以为 SSO 也能用"
    ok, reason = pv.bedrock_available(
        {"AWS_ACCESS_KEY_ID": "a", "AWS_SECRET_ACCESS_KEY": "b", "AWS_REGION": "us-east-1"}
    )
    assert ok is True and reason == ""
    # AWS_DEFAULT_REGION 是官方别名，必须认。
    ok, _ = pv.bedrock_available(
        {"AWS_ACCESS_KEY_ID": "a", "AWS_SECRET_ACCESS_KEY": "b", "AWS_DEFAULT_REGION": "eu-west-1"}
    )
    assert ok is True


@pytest.mark.asyncio
async def test_bedrock_converse_client_signs_the_request_and_parses_the_reply():
    """客户端用 `sign_sigv4` 现签一次，再发 httpx（MockTransport，一个字节都不上网）。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "output": {"message": {"content": [{"text": "你好"}]}},
                "stopReason": "end_turn",
                "usage": {"inputTokens": 3, "outputTokens": 2},
            },
        )

    client = pv.bedrock_client(
        model="anthropic.claude-v2",
        transport=httpx.MockTransport(handler),
        env={
            "AWS_ACCESS_KEY_ID": AKID,
            "AWS_SECRET_ACCESS_KEY": SECRET,
            "AWS_REGION": "us-east-1",
        },
        now=lambda: AMZ_NOW,
    )
    result = await client.ainvoke(
        [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hi"}]
    )

    assert result.content == "你好"
    assert str(seen["url"]).endswith("/model/anthropic.claude-v2/converse")
    headers = seen["headers"]
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "x-amz-date" in headers
    # Converse 的 system 是顶层列表，不是 messages 里的一条。
    assert seen["body"]["system"] == [{"text": "be brief"}]
    assert seen["body"]["messages"] == [{"role": "user", "content": [{"text": "hi"}]}]


@pytest.mark.asyncio
async def test_bedrock_streaming_and_tools_are_explicitly_not_implemented():
    """做不到就抛 `NotImplementedError` 并写清原因——假装支持会让调用静默失效。"""
    client = pv.BedrockConverseClient(
        base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        model="m",
        region="us-east-1",
        access_key="a",
        secret_key="b",
    )
    with pytest.raises(NotImplementedError) as excinfo:
        async for _chunk in client.astream([{"role": "user", "content": "hi"}]):
            pass
    assert "eventstream" in str(excinfo.value)
    with pytest.raises(NotImplementedError):
        client.bind_tools([{"name": "read"}])


def test_bedrock_transport_is_now_released_and_names_the_missing_credential(monkeypatch):
    """**基线变更（本文件唯一一处）**：这条原来钉的是"bedrock 仍报 UnsupportedTransport"。

    MATRIX 4.2 之后 `bedrock_converse` 已放行（签名 + 客户端 + 请求形状都有，且按 AWS
    官方向量验证过），所以旧断言不再成立。新的契约分两半，都要钉住：

    * 凭证齐 → `build_chat_model` 返回**真客户端**；
    * 凭证不齐 → `MissingCredential`，消息里点名缺哪个环境变量（不再是"协议不存在"）。
    """
    profile = pv.PROVIDERS["bedrock"]
    assert profile.supported is True

    for name in pv.BEDROCK_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    with pytest.raises(pv.MissingCredential) as excinfo:
        pv.build_chat_model(profile, SimpleSettings(model="anthropic.claude-v2"))
    message = str(excinfo.value)
    assert "AWS_ACCESS_KEY_ID" in message and "AWS_SECRET_ACCESS_KEY" in message
    assert "AWS_REGION" in message
    # 仍然没做凭据链发现：这句话不许丢，否则 EC2/SSO 上的人会以为能自动取到。
    assert "SSO" in message or "凭据链" in message

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    client = pv.build_chat_model(profile, SimpleSettings(model="anthropic.claude-v2"))
    assert isinstance(client, pv.BedrockConverseClient)
    # 端点由 region 推；`SimpleSettings(base_url=...)` 里的 OpenAI 端点不该被塞给 Bedrock。
    assert client.endpoint().startswith("https://bedrock-runtime.us-east-1.amazonaws.com/")


class SimpleSettings:
    """`ModelSettings` 的最小替身（只有 `build_chat_model` 会读的那几个字段）。"""

    def __init__(self, model: str = "", base_url: str = "") -> None:
        self.model = model
        self.model_base_url = base_url
        self.model_provider = "bedrock"
        self.model_api_key = ""
