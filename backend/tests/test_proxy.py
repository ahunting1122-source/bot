"""本地 OpenAI 兼容代理（MATRIX 4.7）：协议翻译 + 端口占用 + 如实说明边界。

重点三块：

1. `openai_to_provider()` / `provider_to_openai()` 对 **anthropic 与 gemini** 各走
   一个往返，且 `tools` 与 `usage` **一个字段都不许丢**（丢了 usage，客户端永远
   显示 0 token；丢了 tools，工具调用静默失效）；
2. `ProxyServer.handle()` 是纯函数：注入一个假转发函数就能离线跑完"请求翻译 →
   上游 → 响应翻译"，**不真起服务、不发真实请求**；
3. `port_in_use()` 真的能检出占用（用一个 listening socket 占住再测）。

`describe()` 的用例是"如实说明"的门禁：无流式 / 无重试 / 无并发控制这几句话必须
在输出里，否则用户会把最小转发层当成生产网关。
"""

from __future__ import annotations

import json
import socket

import pytest

from app import proxy as px


def _request() -> dict:
    """一份典型的 OpenAI `chat/completions` 请求（含 system / 工具调用 / 工具结果）。"""
    return {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "上海天气?"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "weather", "arguments": '{"city": "SH"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "晴 26°C"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "weather",
                    "description": "查天气",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                },
            }
        ],
        "tool_choice": "auto",
        "max_tokens": 128,
        "temperature": 0.2,
        "stream": True,
    }


def _anthropic_response() -> dict:
    return {
        "id": "msg_01",
        "model": "claude-sonnet-5",
        "content": [
            {"type": "text", "text": "我查一下"},
            {"type": "tool_use", "id": "toolu_1", "name": "weather", "input": {"city": "SH"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 11, "output_tokens": 7},
    }


def _gemini_response() -> dict:
    return {
        "modelVersion": "gemini-3.5-flash",
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "我查一下"},
                        {"functionCall": {"name": "weather", "args": {"city": "SH"}}},
                    ]
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 11, "candidatesTokenCount": 7},
    }


# ── 请求翻译：OpenAI → 上游 ─────────────────────────────────────────────────


def test_openai_to_anthropic_covers_messages_tools_and_limits():
    body = px.openai_to_provider(_request(), "anthropic")
    # system 必须上提到顶层（留在 messages 里 Anthropic 会 400）。
    assert body["system"] == "be brief"
    assert all(message["role"] != "system" for message in body["messages"])
    # 工具结果变成 user 消息里的 tool_result 块，而不是一条普通 user 文本。
    tail = body["messages"][-1]
    assert tail["role"] == "user"
    assert tail["content"][0] == {
        "type": "tool_result",
        "tool_use_id": "call_1",
        "content": "晴 26°C",
    }
    # assistant 的 tool_calls 变成 tool_use 内容块，arguments 字符串解成对象。
    assistant = body["messages"][-2]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == {
        "type": "tool_use",
        "id": "call_1",
        "name": "weather",
        "input": {"city": "SH"},
    }
    # tools：function 结构 → input_schema。
    assert body["tools"][0]["name"] == "weather"
    assert body["tools"][0]["input_schema"]["properties"]["city"]["type"] == "string"
    assert body["tool_choice"] == {"type": "auto"}
    assert body["max_tokens"] == 128
    assert body["temperature"] == 0.2
    # stream 不透传：本层没有 SSE 翻译（见 proxy 模块 docstring）。
    assert body["stream"] is False


def test_openai_to_anthropic_defaults_max_tokens_because_it_is_required():
    payload = _request()
    payload.pop("max_tokens")
    body = px.openai_to_provider(payload, "anthropic")
    assert body["max_tokens"] == px.DEFAULT_MAX_TOKENS


def test_openai_to_gemini_covers_contents_tools_and_generation_config():
    body = px.openai_to_provider(_request(), "gemini")
    assert body["systemInstruction"]["parts"][0]["text"] == "be brief"
    assert body["contents"][0] == {"role": "user", "parts": [{"text": "上海天气?"}]}
    # assistant → model，工具调用 → functionCall part。
    model_turn = body["contents"][1]
    assert model_turn["role"] == "model"
    assert model_turn["parts"][0] == {"functionCall": {"name": "weather", "args": {"city": "SH"}}}
    # 工具结果 → functionResponse part。
    assert body["contents"][2]["parts"][0]["functionResponse"]["response"] == {"content": "晴 26°C"}
    # tools：function → functionDeclarations（OpenAPI Schema 子集）。
    declaration = body["tools"][0]["functionDeclarations"][0]
    assert declaration["name"] == "weather"
    assert declaration["parameters"]["required"] == ["city"]
    assert body["generationConfig"]["maxOutputTokens"] == 128
    assert body["generationConfig"]["temperature"] == 0.2


def test_broken_tool_arguments_are_rejected_not_swallowed():
    """参数不是合法 JSON 时必须报错：当成 `{}` 会让工具收到"看起来合法"的空入参。"""
    payload = _request()
    payload["messages"][2]["tool_calls"][0]["function"]["arguments"] = "{oops"
    with pytest.raises(px.ProxyError, match="JSON"):
        px.openai_to_provider(payload, "anthropic")


def test_image_parts_are_rejected_instead_of_dropped():
    payload = _request()
    payload["messages"][1]["content"] = [{"type": "image_url", "image_url": {"url": "http://x/1.png"}}]
    with pytest.raises(px.ProxyError, match="纯文本"):
        px.openai_to_provider(payload, "anthropic")


# ── 响应翻译：上游 → OpenAI ─────────────────────────────────────────────────


def test_anthropic_response_round_trip_keeps_tool_calls_and_usage():
    out = px.provider_to_openai(_anthropic_response(), "anthropic", created=1)
    assert out["object"] == "chat.completion"
    assert out["created"] == 1
    choice = out["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == "我查一下"
    assert choice["finish_reason"] == "tool_calls"
    call = choice["message"]["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "SH"}
    # usage 三个字段都要在：缺 total_tokens 客户端会显示 0。
    assert out["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}


def test_gemini_response_round_trip_keeps_tool_calls_and_usage():
    out = px.provider_to_openai(_gemini_response(), "gemini", created=2)
    choice = out["choices"][0]
    assert choice["message"]["content"] == "我查一下"
    call = choice["message"]["tool_calls"][0]
    assert call["function"]["name"] == "weather"
    assert json.loads(call["function"]["arguments"]) == {"city": "SH"}
    # Gemini 不给调用 id → 合成一个稳定的，否则客户端回传结果时对不上。
    assert call["id"].startswith("call_weather_")
    assert out["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert out["model"] == "gemini-3.5-flash"


def test_unknown_upstream_is_rejected():
    with pytest.raises(px.UnsupportedUpstream):
        px.openai_to_provider(_request(), "deepseek")
    with pytest.raises(px.UnsupportedUpstream):
        px.provider_to_openai({}, "openai")
    with pytest.raises(px.UnsupportedUpstream):
        px.plan_for("openai")


# ── ProxyServer.handle：纯函数链路 ──────────────────────────────────────────


def test_handle_translates_both_ways_with_an_injected_forward():
    seen: dict[str, object] = {}

    def forward(url: str, headers: dict[str, str], body: dict) -> dict:
        seen["url"] = url
        seen["body"] = body
        seen["headers"] = headers
        return _anthropic_response()

    plan = px.plan_for("anthropic", model_map={"gpt-4o-mini": "claude-sonnet-5"})
    server = px.ProxyServer(plan, forward=forward, headers={"x-api-key": "sk-test"})
    result = server.handle(_request())

    assert str(seen["url"]).endswith("/v1/messages")
    assert seen["body"]["model"] == "claude-sonnet-5", "model_map 必须生效"
    assert seen["headers"]["x-api-key"] == "sk-test"
    assert result["choices"][0]["message"]["content"] == "我查一下"
    assert result["usage"]["total_tokens"] == 18


def test_handle_without_forward_says_so_instead_of_faking_a_reply():
    """没有上游时**不伪造空回答**：代理最常见的坑就是"没通但客户端看到 200 + 空串"。"""
    result = px.ProxyServer(px.plan_for("anthropic")).handle(_request())
    assert result["error"]["code"] == "no_upstream"
    assert "forward" in result["error"]["message"]


def test_handle_turns_upstream_failures_into_readable_errors():
    def boom(url: str, headers: dict[str, str], body: dict) -> dict:
        raise RuntimeError("connection refused")

    result = px.ProxyServer(px.plan_for("gemini"), forward=boom).handle(_request())
    assert result["error"]["code"] == "upstream_error"
    assert "connection refused" in result["error"]["message"]


def test_handle_rejects_a_non_object_payload():
    result = px.ProxyServer(px.plan_for("anthropic")).handle(["not", "a", "dict"])  # type: ignore[arg-type]
    assert result["error"]["code"] == "bad_request"


def test_gemini_endpoint_carries_the_model_in_the_url():
    seen: dict[str, str] = {}

    def forward(url: str, headers: dict[str, str], body: dict) -> dict:
        seen["url"] = url
        return _gemini_response()

    plan = px.plan_for("gemini", model_map={"gpt-4o-mini": "gemini-3.5-flash"})
    px.ProxyServer(plan, forward=forward).handle(_request())
    assert seen["url"].endswith("/models/gemini-3.5-flash:generateContent")


# ── 端口占用 ────────────────────────────────────────────────────────────────


def test_port_in_use_detects_a_listening_socket():
    """真的占一个端口再测：探针必须报"占用"，放开后必须报"空闲"。"""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        assert px.port_in_use("127.0.0.1", port) is True
    finally:
        listener.close()
    assert px.port_in_use("127.0.0.1", port) is False
    # 端口 0 = 系统分配，不是"占用"。
    assert px.port_in_use("127.0.0.1", 0) is False


# ── 如实说明 ────────────────────────────────────────────────────────────────


def test_describe_lists_what_this_layer_does_not_do():
    text = px.describe()
    for missing in ("流式", "重试", "并发", "鉴权"):
        assert missing in text, f"describe() 必须交代没有 {missing}"
    assert "最小转发层" in text


def test_plan_describe_warns_about_non_loopback_binding():
    plan = px.ProxyPlan(listen_host="0.0.0.0", listen_port=8645)
    assert "警告" in plan.describe()
    assert "8645" in plan.url
    assert px.ProxyPlan().describe().count("警告") == 0


def test_model_map_passes_unknown_names_through():
    plan = px.plan_for("anthropic", model_map={"gpt-4o-mini": "claude-sonnet-5"})
    assert plan.resolve_model("gpt-4o-mini") == "claude-sonnet-5"
    assert plan.resolve_model("claude-opus-5") == "claude-opus-5"


# ── CLI：`orbit proxy …` ───────────────────────────────────────────────────


def test_cli_proxy_plan_prints_the_plan_without_binding(tmp_path, capsys):
    from app.cli.commands import model_extra2  # noqa: F401 —— 导入即接线
    from app.cli.main import main

    code = main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "proxy",
            "plan",
            "--provider",
            "gemini",
            "--map",
            "gpt-4o-mini=gemini-3.5-flash",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "gemini" in out
    assert "gpt-4o-mini→gemini-3.5-flash" in out


def test_cli_proxy_status_reports_the_limits(tmp_path, capsys):
    from app.cli.commands import model_extra2  # noqa: F401
    from app.cli.main import main

    assert main(["--data-dir", str(tmp_path / "data"), "proxy", "status"]) == 0
    out = capsys.readouterr().out
    assert "最小转发层" in out
    assert "anthropic" in out and "gemini" in out


def test_cli_proxy_providers_lists_only_the_two_non_openai_upstreams(tmp_path, capsys):
    from app.cli.commands import model_extra2  # noqa: F401
    from app.cli.main import main

    assert main(["--data-dir", str(tmp_path / "data"), "proxy", "providers"]) == 0
    out = capsys.readouterr().out
    assert "anthropic" in out and "gemini" in out
    assert "openai" not in out, "OpenAI 兼容面不需要代理，列出来只会误导"


def test_proxy_paths_are_wired_into_the_registry():
    """接线证据：表里已有的三条被接管，两条新子命令被追加。"""
    from app.cli.commands import model_extra2
    from app.cli.spec import REGISTRY

    wiring = model_extra2.WIRING
    for path in ("proxy status", "proxy providers", "proxy start"):
        assert wiring[path] in {"claimed", "appended", "deferred"}
    for path in ("proxy plan", "proxy serve"):
        assert wiring[path] in {"appended", "deferred"}

    parent = REGISTRY.get("proxy")
    assert parent is not None
    names = {sub.name for sub in parent.subcommands}
    assert {"start", "status", "providers", "plan", "serve"} <= names
    for sub in parent.subcommands:
        if sub.name in {"start", "status", "providers", "plan", "serve"}:
            assert sub.handler is not None, f"proxy {sub.name} 还没接上实现"
            assert sub.state == "done"
