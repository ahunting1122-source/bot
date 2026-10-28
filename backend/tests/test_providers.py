"""`app/providers.py` 的契约测试。

三块重点：

1. **表完整性**——provider 身份（base_url / key 环境变量）漏一个字段，接这家就会
   在运行时静默走错端点；
2. **反查**——`model_base_url` 的等价写法必须能认出来（`https://api.deepseek.com`
   与 `https://api.deepseek.com/v1` 是同一个端点）；
3. **三级能力查询的优先级**，以及**不支持的线协议必须抛带原因的清晰异常**。

全部离线：httpx 请求走 `httpx.MockTransport`，没有任何真实网络调用。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app import providers as pv

#: 06 调研里点名、且本层必须覆盖的 provider。少一个就是漏抄规格书。
REQUIRED_PROVIDERS = (
    "deepseek",
    "openai",
    "anthropic",
    "gemini",
    "openrouter",
    "zai",
    "kimi-coding",
    "minimax",
    "moonshot",
    "qwen",
    "groq",
    "mistral",
    "xai",
    "bedrock",
    "ollama",
    "custom",
)


def _settings(**overrides):
    """最小 Settings 替身。`ModelSettings` 是 Protocol，只要有这四个属性就够。"""
    base = {
        "model": "",
        "model_base_url": "https://api.openai.com/v1",
        "model_provider": "",
        "model_api_key": SecretStr(""),
    }
    base.update(overrides)
    return SimpleNamespace(**base)


# ── 表完整性 ────────────────────────────────────────────────────────────────


def test_required_providers_are_registered():
    missing = [name for name in REQUIRED_PROVIDERS if name not in pv.PROVIDERS]
    assert not missing, f"缺 provider：{missing}"


def test_profile_fields_are_consistent():
    """每个 profile 的身份字段必须自洽。

    规则按"这家到底是什么"分档，而不是一刀切要求所有字段非空：
    - `auth == "api_key"` 必须有 `key_env`（否则用户不知道该设哪个环境变量）；
    - `custom` 的端点由用户填，允许为空；
    - `models_known == False`（只能实时拉清单）允许没有出厂模型表；
    - 其余每家至少一个模型、一个端点、一种线协议。
    """
    for key, profile in pv.PROVIDERS.items():
        assert key == profile.id, f"{key}: 字典键与 profile.id 不一致"
        assert profile.name.strip(), f"{key}: 缺显示名"
        assert profile.transport in {
            *pv.SUPPORTED_TRANSPORTS,
            *pv.UNSUPPORTED_TRANSPORTS,
        }, f"{key}: 未知线协议 {profile.transport}"
        if profile.auth == "api_key":
            assert profile.key_env, f"{key}: api_key 类 provider 必须有 key_env"
        if key != "custom":
            assert profile.default_base_url.startswith("http"), f"{key}: 端点必须是 http(s)"
        if profile.models_known:
            assert profile.models, f"{key}: 声明了模型清单却一个模型都没有"
        for model, caps in profile.models.items():
            assert set(caps) == set(pv.CAPABILITY_FIELDS), f"{key}/{model}: 能力字段不全"
            for flag in ("supports_tools", "supports_reasoning", "supports_vision"):
                assert isinstance(caps[flag], bool), f"{key}/{model}: {flag} 必须是布尔"
            assert isinstance(caps["context_window"], int), f"{key}/{model}: 窗口必须是整数"
            assert caps["context_window"] > 0, f"{key}/{model}: 窗口必须为正"


def test_key_env_names_put_primary_first():
    """别名表要主名在前：读密钥时按顺序取，顺序错了会读到过期的那把。"""
    zai = pv.PROVIDERS["zai"]
    assert zai.key_env_names[0] == zai.key_env == "ZAI_API_KEY"
    assert set(zai.key_env_names) == {"ZAI_API_KEY", "GLM_API_KEY", "Z_AI_API_KEY"}
    # ollama / custom 没有 key，别名表也必须是空的（不能凭空造一个变量名）。
    assert pv.PROVIDERS["ollama"].key_env_names == ()
    assert pv.PROVIDERS["custom"].key_env_names == ()


# ── 反查 ────────────────────────────────────────────────────────────────────


def test_resolve_provider_by_base_url():
    assert pv.resolve_provider(_settings()).id == "openai"
    # 调研 §4.2 里用户把 deepseek 写成不带 /v1 的形式，必须仍认得出。
    assert pv.resolve_provider(
        _settings(model_base_url="https://api.deepseek.com")
    ).id == "deepseek"
    assert pv.resolve_provider(
        _settings(model_base_url="https://api.deepseek.com/v1/")
    ).id == "deepseek"
    assert pv.resolve_provider(
        _settings(model_base_url="https://api.minimax.io/anthropic")
    ).id == "minimax"


def test_resolve_provider_prefers_explicit_override():
    """显式 provider 优先于反查：反查天生有歧义，用户明确选过的不该被猜覆盖。"""
    settings = _settings(model_base_url="https://api.deepseek.com/v1", model_provider="openrouter")
    assert pv.resolve_provider(settings).id == "openrouter"


def test_resolve_provider_unknown_override_raises():
    with pytest.raises(pv.UnknownProvider) as excinfo:
        pv.resolve_provider(_settings(model_provider="not-a-provider"))
    assert "not-a-provider" in str(excinfo.value)


def test_resolve_provider_falls_back_to_custom():
    """自建网关（vLLM / LM Studio）不该报错，落到 custom 才是对的。"""
    settings = _settings(model_base_url="http://127.0.0.1:8000/v1")
    assert pv.resolve_provider(settings).id == "custom"


# ── 三级能力查询 ────────────────────────────────────────────────────────────


def test_capability_prefers_manual_over_cache_and_factory(tmp_path):
    """优先级：手工覆盖 → 运行时缓存 → 出厂兜底。"""
    profile = pv.PROVIDERS["deepseek"]
    # 三级同时有值时，手工覆盖必须赢。
    pv.record_capability(tmp_path, "deepseek", "deepseek-v4-pro", {"supports_tools": False})
    value, source = pv.capability_with_source(
        profile, "deepseek-v4-pro", "supports_tools", data_dir=tmp_path
    )
    assert (value, source) == (True, "manual")

    # 手工表里没有的模型 → 命中缓存。
    pv.record_capability(tmp_path, "deepseek", "brand-new-model", {"context_window": 999999})
    value, source = pv.capability_with_source(
        profile, "brand-new-model", "context_window", data_dir=tmp_path
    )
    assert (value, source) == (999999, "cache")

    # 两级都没有 → 出厂兜底（保守：不支持、小窗口）。
    value, source = pv.capability_with_source(profile, "unknown-model", "supports_tools")
    assert (value, source) == (False, "factory")
    assert pv.capability(profile, "unknown-model", "context_window") == pv.FACTORY_CAPABILITY[
        "context_window"
    ]


def test_capability_ignores_broken_cache_file(tmp_path):
    """缓存坏了只该退回下一档，不该让查询炸掉。"""
    path = pv.cache_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert pv.load_cache(tmp_path) == {}
    value, source = pv.capability_with_source(
        pv.PROVIDERS["deepseek"], "deepseek-chat", "supports_tools", data_dir=tmp_path
    )
    assert (value, source) == (True, "manual")


def test_capability_rejects_unknown_field():
    with pytest.raises(ValueError) as excinfo:
        pv.capability(pv.PROVIDERS["deepseek"], "deepseek-chat", "supports_telepathy")
    assert "未知能力字段" in str(excinfo.value)


def test_record_capability_writes_documented_shape(tmp_path):
    path = pv.record_capability(
        tmp_path, "deepseek", "deepseek-v4-pro", {"supports_tools": False, "junk": 1}
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["deepseek"]["models"]["deepseek-v4-pro"] == {"supports_tools": False}
    assert "at" in data["deepseek"]


# ── bedrock：已放行，但凭证不齐时要点名缺哪个环境变量（MATRIX 4.2）──────────
#
# **基线变更（本文件第 1 处）**：这条原来叫 `test_unsupported_transport_raises_with_reason`，
# 钉的是"bedrock 抛 UnsupportedTransport 且消息里有 boto3"。翻转的理由：SigV4 签名与
# Converse 客户端都已实现、并按 AWS 官方向量逐字段验证过，**静态凭证就够发请求**；
# 把"没配凭证"说成"协议不支持"，会让用户永远等一个不会来的版本更新。
# 所以现在钉的是新契约：凭证不齐 → `MissingCredential`，且消息里点名缺哪个变量。


def test_bedrock_without_credentials_names_the_missing_env_vars(monkeypatch):
    profile = pv.PROVIDERS["bedrock"]
    assert profile.supported is True, "bedrock_converse 已放行（MATRIX 4.2）"
    for name in pv.BEDROCK_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    with pytest.raises(pv.MissingCredential) as excinfo:
        pv.build_chat_model(profile, _settings(model="anthropic.claude-v2"))
    message = str(excinfo.value)
    # 三个变量必须逐个点名，否则用户只知道"缺凭证"却不知道缺哪把。
    for name in pv.BEDROCK_ENV_KEYS:
        assert name in message, name
    # 仍然没做凭据链发现——这句话必须留着，否则 EC2/SSO 上的人会以为能自动取到。
    assert "SSO" in message or "凭据链" in message


def test_bedrock_with_credentials_returns_a_real_client(monkeypatch):
    """凭证齐 → **真客户端**（不是抛错、也不是 None）。"""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIDEXAMPLE")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "secret")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    client = pv.build_chat_model(
        pv.PROVIDERS["bedrock"], _settings(model="anthropic.claude-v2")
    )
    assert isinstance(client, pv.BedrockConverseClient)
    assert client.region == "us-east-1"
    # 端点由 region 推，**不**跟着 `model_base_url`（那个默认值是 OpenAI 的端点）。
    assert client.endpoint().startswith("https://bedrock-runtime.us-east-1.amazonaws.com/")
    assert client.endpoint().endswith("/model/anthropic.claude-v2/converse")
    assert "secret" not in repr(client), "凭据不许进 repr"


def test_supported_transports_are_exactly_the_four():
    """**基线变更（本文件第 2 处）**：原来是"exactly the three"且断言
    `UNSUPPORTED_TRANSPORTS == {"bedrock_converse"}`；bedrock 放行后两者都要跟着改。"""
    assert set(pv.SUPPORTED_TRANSPORTS) == {
        "chat_completions",
        "anthropic_messages",
        "gemini",
        "bedrock_converse",
    }
    # 未实现的线协议现在是**空表**（不是删掉这个概念：机制留着，将来再有不支持的就往里写）。
    assert set(pv.UNSUPPORTED_TRANSPORTS) == set()
    # 但"bedrock 仍然没做到什么"必须还有一张清单——放行不等于全都做到了。
    assert len(pv.BEDROCK_LIMITATIONS) >= 3


# ── chat_completions：保持现有行为 ──────────────────────────────────────────


def test_chat_completions_keeps_existing_timeout_and_retries():
    """`agent.get_model()` 一直是 `timeout=60, max_retries=1`，本层不许偷偷改。

    `ChatOpenAI` 把 `timeout` 存成 `request_timeout`（pydantic 别名），所以断言读的
    是后者——读 `timeout` 会拿到 AttributeError，而不是"没设置"。
    """
    model = pv.build_chat_model(
        pv.PROVIDERS["openai"],
        _settings(model="gpt-5.4", model_api_key=SecretStr("sk-test-123456")),
    )
    assert model.request_timeout == 60
    assert model.max_retries == 1
    assert model.model_name == "gpt-5.4"
    assert str(model.openai_api_base).rstrip("/") == "https://api.openai.com/v1"


def test_chat_completions_requires_key_for_api_key_provider(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(pv.MissingCredential) as excinfo:
        pv.build_chat_model(pv.PROVIDERS["openai"], _settings(model="gpt-5.4"))
    assert "orbit login" in str(excinfo.value)


def test_keyless_local_endpoint_uses_placeholder(monkeypatch):
    """Ollama 不需要 key，但 openai SDK 在 api_key 为空时直接抛——所以给占位值。"""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    model = pv.build_chat_model(
        pv.PROVIDERS["ollama"],
        _settings(model="llama3.2", model_base_url="http://127.0.0.1:11434/v1"),
    )
    assert model.model_name == "llama3.2"


def test_extra_body_hooks_follow_provider_wire_formats():
    """推理参数落点逐家不同（调研 §3.5）：只断言有互斥/拒绝规则的那三家。"""
    deepseek = pv.PROVIDERS["deepseek"]
    assert pv.extra_body_for(deepseek, "deepseek-v4-pro", None) == {
        "thinking": {"type": "disabled"}
    }
    assert pv.extra_body_for(deepseek, "deepseek-v4-pro", "high") == {
        "thinking": {"type": "enabled"}
    }
    # v3 系列不认 thinking，发一个不起作用的字段只会让日志骗人。
    assert pv.extra_body_for(deepseek, "deepseek-chat", "high") == {}

    # Kimi：thinking 与顶层 reasoning_effort 互斥，有 effort 时这里必须让位。
    kimi = pv.PROVIDERS["kimi-coding"]
    assert pv.extra_body_for(kimi, "kimi-k3", "high") == {}
    assert pv.extra_body_for(kimi, "kimi-k3", None) == {"thinking": {"type": "disabled"}}

    # Z.AI：GLM-4.5+ 默认开思考，不发 disabled 等于没关。
    zai = pv.PROVIDERS["zai"]
    assert pv.extra_body_for(zai, "glm-5.2", None) == {"thinking": {"type": "disabled"}}
    assert pv.extra_body_for(zai, "glm-4-9b", None) == {}


# ── 轻量客户端（anthropic_messages / gemini）：全程 MockTransport ───────────


def _mock_transport(handler):
    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_anthropic_client_ainvoke_uses_x_api_key():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "claude-sonnet-5",
                "content": [{"type": "text", "text": "你好"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 3, "output_tokens": 2},
            },
        )

    client = pv.AnthropicMessagesClient(
        base_url="https://api.anthropic.com",
        api_key="sk-ant-secret",
        model="claude-sonnet-5",
        transport=_mock_transport(handler),
    )
    result = await client.ainvoke([{"role": "user", "content": "hi"}])

    assert result.content == "你好"
    assert str(seen["url"]).endswith("/v1/messages")
    headers = seen["headers"]
    assert headers["x-api-key"] == "sk-ant-secret"
    assert headers["anthropic-version"] == "2023-06-01"
    assert "authorization" not in headers, "Anthropic 原生面不是 Bearer 鉴权"
    body = seen["body"]
    assert body["stream"] is False
    assert body["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_anthropic_client_moves_system_message_to_top_level():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": "ok"}]})

    client = pv.AnthropicMessagesClient(
        base_url="https://api.anthropic.com",
        api_key="k",
        model="m",
        transport=_mock_transport(handler),
    )
    await client.ainvoke(
        [{"role": "system", "content": "你是助手"}, {"role": "user", "content": "hi"}]
    )
    body = seen["body"]
    assert body["system"] == "你是助手"
    assert all(item["role"] != "system" for item in body["messages"])


@pytest.mark.asyncio
async def test_anthropic_client_streams_text_deltas():
    events = [
        'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"你"}}\n\n',
        'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"type":"text_delta","text":"好"}}\n\n',
        'event: message_stop\ndata: {"type":"message_stop"}\n\n',
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="".join(events))

    client = pv.AnthropicMessagesClient(
        base_url="https://api.anthropic.com",
        api_key="k",
        model="m",
        transport=_mock_transport(handler),
    )
    chunks = [chunk.content async for chunk in client.astream([{"role": "user", "content": "hi"}])]
    assert "".join(chunks) == "你好"


@pytest.mark.asyncio
async def test_gemini_client_uses_native_endpoint_and_key_header():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "candidates": [{"content": {"parts": [{"text": "hi there"}]}}],
                "usageMetadata": {"totalTokenCount": 5},
            },
        )

    client = pv.GeminiClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="goog-key",
        model="gemini-3.5-flash",
        transport=_mock_transport(handler),
    )
    result = await client.ainvoke([{"role": "user", "content": "hi"}])

    assert result.content == "hi there"
    url = str(seen["url"])
    assert url.endswith("/models/gemini-3.5-flash:generateContent")
    assert "key=" not in url, "key 不能进 URL：它会出现在异常消息与访问日志里"
    assert seen["headers"]["x-goog-api-key"] == "goog-key"
    assert seen["body"]["contents"] == [{"role": "user", "parts": [{"text": "hi"}]}]


@pytest.mark.asyncio
async def test_clients_propagate_http_status_errors():
    """429 必须原样冒出去（带 response），否则回退链认不出限流。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    client = pv.AnthropicMessagesClient(
        base_url="https://api.anthropic.com",
        api_key="k",
        model="m",
        transport=_mock_transport(handler),
    )
    with pytest.raises(httpx.HTTPStatusError) as excinfo:
        await client.ainvoke([{"role": "user", "content": "hi"}])
    assert excinfo.value.response.status_code == 429


def test_bind_tools_is_not_faked():
    """做不到就抛 `NotImplementedError` 并写清原因——假装支持会让工具调用静默失效。"""
    anthropic_client = pv.AnthropicMessagesClient(
        base_url="https://api.anthropic.com", api_key="k", model="m"
    )
    gemini_client = pv.GeminiClient(
        base_url="https://generativelanguage.googleapis.com/v1beta", api_key="k", model="m"
    )
    for client in (anthropic_client, gemini_client):
        with pytest.raises(NotImplementedError) as excinfo:
            client.bind_tools([{"name": "read"}])
        assert "未实现" in str(excinfo.value)


def test_multimodal_content_is_rejected_not_dropped():
    """图片/工具内容块没有实现，就必须报错：静默丢内容会让模型"看不见图"还答得很自信。"""
    with pytest.raises(NotImplementedError):
        pv._wire_messages(
            [{"role": "user", "content": [{"type": "image", "source": {"data": "..."}}]}]
        )


def test_client_repr_never_leaks_key():
    client = pv.AnthropicMessagesClient(
        base_url="https://api.anthropic.com", api_key="sk-ant-very-secret", model="m"
    )
    assert "sk-ant-very-secret" not in repr(client)


# ── 新增的 profile 钩子（MATRIX 4.1）：纯新增、默认 None、行为不变 ──────────


def test_new_hook_fields_default_to_none():
    """`fetch_models` / `build_extra_body` 是**纯新增**字段：默认 None，
    既有 16 家一个都没填，所以"没钩子"这条路必须照旧工作。"""
    for key, profile in pv.PROVIDERS.items():
        assert profile.fetch_models is None, f"{key} 不该有 fetch_models 钩子"
        assert profile.build_extra_body is None, f"{key} 不该有 build_extra_body 钩子"


def test_hook_helpers_degrade_gracefully():
    """没有钩子时的退化行为：清单退回出厂 `models` 表，请求体原样返回（且不原地改）。"""
    deepseek = pv.PROVIDERS["deepseek"]
    assert pv.fetch_models_for(deepseek) == sorted(deepseek.models)
    # `models_known=False` 的两家没有出厂表 → 返回空表，**不编造模型名**。
    assert pv.fetch_models_for(pv.PROVIDERS["bedrock"]) == []
    assert pv.fetch_models_for(pv.PROVIDERS["custom"]) == []

    payload = {"model": "x", "messages": []}
    out = pv.apply_build_extra_body(deepseek, "deepseek-chat", payload)
    assert out == payload
    assert out is not payload, "必须返回新 dict：原地改会污染调用方的日志"


def test_hook_fields_are_actually_used_when_present():
    """钩子填了就要生效——否则"加了钩子"只是字段长得好看。"""
    import dataclasses

    profile = dataclasses.replace(
        pv.PROVIDERS["deepseek"],
        fetch_models=lambda: ["z-model", "a-model"],
        build_extra_body=lambda model, payload: {**payload, "patched": model},
    )
    assert pv.fetch_models_for(profile) == ["a-model", "z-model"]
    assert pv.apply_build_extra_body(profile, "deepseek-chat", {"model": "m"}) == {
        "model": "m",
        "patched": "deepseek-chat",
    }


def test_every_supported_transport_names_a_real_client():
    """`SUPPORTED_TRANSPORTS` 里的每一种都要指得出实现它的客户端类名。

    矩阵里写"4 种已实现"不算证据；指得出类名才算。这条用例防止有人往
    `SUPPORTED_TRANSPORTS` 里加一个没有实现的字符串。

    **基线变更（本文件第 3 处）**：末尾原来是 `PROVIDERS["bedrock"].supported is False`，
    现在 bedrock 放行了，改成 True。
    """
    assert set(pv.TRANSPORT_CLIENTS) == set(pv.SUPPORTED_TRANSPORTS)
    for transport, target in pv.TRANSPORT_CLIENTS.items():
        module_name, _, attr = target.rpartition(".")
        module = __import__(module_name, fromlist=[attr])
        assert hasattr(module, attr), f"{transport} 指向的 {target} 不存在"
    assert pv.PROVIDERS["bedrock"].supported is True
