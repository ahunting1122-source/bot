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
- `ProxyPlan`：监听地址 + 上游 + 模型映射，`port_in_use()` 做端口占用检查；
- `ProxyServer.serve()`：`http.server` 起的最小 HTTP 服务，`handle()` 是纯函数
  （测试只测 `handle()`，**不起服务**）。

**没做（如实列出）**：

- **没有 SSE 流式透传**：请求里 `stream: true` 会被降级成非流式（上游回整包，
  代理一次性回整包）。要真流式得逐事件翻译 `content_block_delta` /
  `candidates.parts` 的增量，那是另一层工作；
- **没有重试**：上游 429 / 5xx 原样回给客户端（带原始状态码），不做退避；
- **没有并发控制**：`ThreadingHTTPServer` 来一个请求开一个线程，没有队列、没有
  限流、没有连接池上限；
- **没有鉴权**：只监听回环地址（默认 `127.0.0.1`）。绑非回环地址等于把上游凭据
  暴露给整个网段——`plan_for()` 不拦，但 `describe()` 会警告；
- **没有 `responses` / `embeddings` / 图片端点**：只有 `/v1/chat/completions`
  与 `/v1/models`。

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
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app import providers as pv

#: 默认监听地址。**只默认回环**：代理会把请求转给持有上游凭据的本机，绑 `0.0.0.0`
#: 等于把凭据借给整个网段用。
DEFAULT_HOST = "127.0.0.1"

#: 默认端口 8645 —— 与声明表里 `--port` 的帮助文本一致（dashboard 9119 / proxy 8645）。
DEFAULT_PORT = 8645

#: 支持作为代理上游的 provider。**只有这两家**：它们是"协议不是 OpenAI"的那两家；
#: 其余 14 家本来就是 `chat_completions`，套一层代理除了多一次转发没有任何收益。
SUPPORTED_UPSTREAMS: tuple[str, ...] = ("anthropic", "gemini")

#: 上游 provider → 它真正说的线协议（与 `providers.ProviderProfile.transport` 对齐）。
UPSTREAM_TRANSPORTS: dict[str, str] = {
    "anthropic": "anthropic_messages",
    "gemini": "gemini",
}

#: Anthropic 的 `max_tokens` 是**必填**字段（缺了直接 400），而 OpenAI 的
#: `max_tokens` 是可选的。客户端不给时用这个默认值——写在常量里而不是散在代码里，
#: 因为它会直接决定"最长回答能有多长"。
DEFAULT_MAX_TOKENS = 4096


class ProxyError(RuntimeError):
    """代理层的可预期失败（消息要能直接给人看）。"""


class UnsupportedUpstream(ProxyError):
    """`provider` 不在 `SUPPORTED_UPSTREAMS` 里。"""


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

    def __post_init__(self) -> None:
        if self.upstream_provider not in SUPPORTED_UPSTREAMS:
            raise UnsupportedUpstream(
                f"不支持把 `{self.upstream_provider}` 当代理上游：只有 "
                f"{' / '.join(SUPPORTED_UPSTREAMS)} 的协议与 OpenAI 不同，需要翻译。"
                "其余 provider 本身就是 OpenAI 兼容面，客户端直连即可。"
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
            "url": self.url,
        }

    def describe(self) -> str:
        """给人看的一段说明。**端口占用不在这里查**——那是 `port_in_use()` 的事，
        计划对象保持"不碰 socket"的纯粹性。"""
        mapped = "、".join(f"{key}→{value}" for key, value in self.model_map.items())
        lines = [
            f"监听：{self.url}（OpenAI 兼容面，只有 /v1/chat/completions 与 /v1/models）",
            f"上游：{self.upstream_provider}（线协议 {self.transport}）",
            f"上游端点：{self.base_url or '（未填，需在 Settings 里配 base_url）'}",
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


def _to_anthropic(payload: dict[str, Any]) -> dict[str, Any]:
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
    # `stream` 不透传：本层没有 SSE 翻译（见模块 docstring），发 true 上去只会拿到
    # 一串事件流却按整包解析 —— 那会直接坏。显式关掉并在 describe() 里说明。
    body["stream"] = False
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


def openai_to_provider(payload: dict[str, Any], provider: str) -> dict[str, Any]:
    """**纯函数**：OpenAI 请求体 → 上游请求体。

    覆盖三块：`messages`（含 system 上提 / 工具结果与 tool_use 块）、`tools`
    （`function` 结构 → `input_schema` / `functionDeclarations`）、以及生成参数
    （`max_tokens` / `temperature` / `stop`）。`usage` 是响应侧的字段，见
    `provider_to_openai()`。

    不认识的上游 → `UnsupportedUpstream`；不认识的结构 → `ProxyError`。
    **不做"尽力而为"的近似**：翻译错的请求会静默变成另一种语义，比直接报错难查。
    """
    key = (provider or "").strip().lower()
    if key == "anthropic":
        return _to_anthropic(payload)
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


# ── 服务 ────────────────────────────────────────────────────────────────────

#: 上游转发函数：`(url, headers, body) -> 解析后的 JSON`。
#:
#: 做成可注入的回调，是为了让 `ProxyServer.handle()` **能离线测**：测试塞一个假
#: 转发函数，就得到"请求翻译 → 上游 → 响应翻译"的完整链路，一个字节都不上网。
Forward = Callable[[str, dict[str, str], dict[str, Any]], dict[str, Any]]


def auth_headers(plan: ProxyPlan, api_key: str) -> dict[str, str]:
    """上游鉴权头。**两家不是同一套**：Anthropic 用 `x-api-key` + `anthropic-version`，
    Gemini 用 `x-goog-api-key`（与 `providers.py` 的两个客户端逐字一致）。

    key 只在请求头里用一次，不进 `ProxyPlan`、不进日志：计划对象会被 `to_dict()`
    序列化，凭据一旦进去就会被打进日志和 `--json` 输出。
    """
    if not api_key:
        return {}
    if plan.upstream_provider == "anthropic":
        return {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    return {"x-goog-api-key": api_key}


def http_forward(plan: ProxyPlan, *, api_key: str = "", timeout: float = 60.0) -> Forward:
    """造一个真的走 httpx 的转发函数（**无重试、无流式**，见模块 docstring）。

    这里没有做"离线降级"：没有网络时 httpx 会抛 `ConnectError`，代理把它转成
    OpenAI 形状的错误体回给客户端。假装成功比报错危险得多。
    """

    def forward(url: str, headers: dict[str, str], body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        merged = {**auth_headers(plan, api_key), "content-type": "application/json", **headers}
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, headers=merged, json=body)
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            raise ProxyError(f"上游返回的不是 JSON 对象：{str(data)[:200]}")
        return data

    return forward


class ProxyServer:
    """最小代理：翻译 + 转发 + 回翻。**测试只调 `handle()`，不起服务**。

    `handle(payload)` 是纯函数（除了注入的 `forward` 会碰网络）：同样的输入 +
    同样的 `forward` 一定得到同样的输出，所以协议翻译的正确性可以在离线环境里
    被完整断言。
    """

    def __init__(
        self,
        plan: ProxyPlan,
        *,
        forward: Forward | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.plan = plan
        self._forward = forward
        # 鉴权头由启动方（`orbit proxy serve`）注入，本类不自己读 keyring：
        # 代理对象会被 CLI 打印/序列化，凭据不该躺在它的属性里。
        self._extra_headers = dict(headers or {})

    # -- 翻译 -------------------------------------------------------------
    def translate_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        """OpenAI 请求 → 上游请求（顺手把模型名按 `model_map` 换掉）。"""
        translated = openai_to_provider(payload, self.plan.upstream_provider)
        if "model" in translated:
            translated["model"] = self.plan.resolve_model(str(payload.get("model", "")))
        return translated

    def translate_response(
        self, response: dict[str, Any], *, model: str = ""
    ) -> dict[str, Any]:
        return provider_to_openai(response, self.plan.upstream_provider, model=model)

    # -- 请求处理（纯函数）------------------------------------------------
    def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        """处理一次 `/v1/chat/completions`。**不抛异常给调用方**：错误也是 JSON。

        没有注入 `forward` 时返回一个 OpenAI 形状的错误体并说明原因——**不伪造一个
        空回答**。代理层最常见的坑就是"上游没通但客户端看到 200 + 空字符串"。
        """
        if not isinstance(payload, dict):
            return _error("请求体必须是 JSON 对象", code="bad_request")
        if self._forward is None:
            return _error(
                "代理没有配置上游转发函数（`forward=`）：本层只做协议翻译，真实转发需要"
                "注入 forward。用 `orbit proxy serve` 启动时会自动接 httpx 转发。",
                code="no_upstream",
            )
        try:
            body = self.translate_request(payload)
            data = self._forward(
                self._endpoint(str(payload.get("model", ""))), self._headers(), body
            )
            return self.translate_response(data, model=str(payload.get("model", "")))
        except UnsupportedUpstream as exc:
            return _error(str(exc), code="unsupported_upstream")
        except ProxyError as exc:
            return _error(str(exc), code="proxy_error")
        except Exception as exc:  # noqa: BLE001 —— 上游异常统一转成可读错误体
            return _error(f"上游请求失败：{exc}", code="upstream_error")

    def _endpoint(self, model: str) -> str:
        """上游端点。Anthropic 的模型在**请求体**里，Gemini 的模型在**URL** 里。"""
        base = self.plan.base_url.rstrip("/")
        if self.plan.upstream_provider == "anthropic":
            return f"{base}/v1/messages"
        target = self.plan.resolve_model(model)
        if not target:
            raise ProxyError(
                "Gemini 上游需要模型名：客户端请求里没有 `model`，`model_map` 也没配。"
            )
        return f"{base}/models/{target}:generateContent"

    def _headers(self) -> dict[str, str]:
        return {"content-type": "application/json", **self._extra_headers}

    def models(self) -> list[dict[str, Any]]:
        """`GET /v1/models` 的返回。**只有映射表里的名字**——不列出上游的全部模型，
        因为本层没有联网拉清单的能力（`capabilities.refresh_cache()` 会明确报错）。"""
        return [
            {"id": name, "object": "model", "owned_by": self.plan.upstream_provider}
            for name in sorted(self.plan.model_map)
        ]

    def describe(self) -> str:
        """计划 + **本层没做到的事**。用户拿到的必须是完整交代，不是一句"已就绪"。"""
        return "\n".join(
            [
                self.plan.describe(),
                "边界（如实说明）：",
                "  · 无 SSE 流式透传：`stream: true` 会被降级为非流式整包返回；",
                "  · 无重试：上游 429 / 5xx 原样回给客户端；",
                "  · 无并发控制：每个请求一个线程，没有队列与限流；",
                "  · 无鉴权：默认只监听回环地址；绑非回环等于把上游凭据借给整个网段；",
                "  · 只有 /v1/chat/completions 与 /v1/models，没有 responses / embeddings。",
            ]
        )

    # -- 真起服务（测试不调）----------------------------------------------
    def serve(self) -> None:  # pragma: no cover - 阻塞式服务，单测不启动
        """用 `http.server` 起最小 HTTP 服务，**阻塞**直到 Ctrl-C。

        为什么不是 aiohttp / uvicorn：那两条路要么没装、要么会把整个 FastAPI 应用
        拉起来。本层只需要"收一个 JSON、回一个 JSON"，标准库够用，也少一层依赖。
        """
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

            def do_GET(self) -> None:  # noqa: N802 —— 标准库规定的名字
                if self.path.rstrip("/").endswith("/v1/models"):
                    self._write(200, {"object": "list", "data": server.models()})
                    return
                self._write(404, _error(f"未知路径：{self.path}", code="not_found"))

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    self._write(400, _error("请求体不是合法 JSON", code="bad_request"))
                    return
                result = server.handle(payload)
                status = 200 if "error" not in result else 502
                self._write(status, result)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                # 静默默认日志（它会打印每个请求到 stderr）。要排障时由 CLI 打开。
                return

        httpd = ThreadingHTTPServer((self.plan.listen_host, self.plan.listen_port), Handler)
        try:
            httpd.serve_forever()
        finally:
            httpd.server_close()


def _error(message: str, *, code: str) -> dict[str, Any]:
    """OpenAI 形状的错误体。客户端（尤其 SDK）认这个结构。"""
    return {"error": {"message": message, "type": "proxy_error", "code": code}}


def describe() -> str:
    """这一层到底做了什么、没做什么。CLI 的 `proxy status` 直接打印它。"""
    return "\n".join(
        [
            "本地 OpenAI 兼容代理（把 anthropic / gemini 协议翻成 OpenAI 面）",
            f"可用上游：{' / '.join(SUPPORTED_UPSTREAMS)}",
            "翻译范围：chat/completions 的 messages、tools、usage 三块",
            "没做到：无 SSE 流式透传、无重试、无并发控制、无鉴权、无 responses/embeddings",
            "这是**最小转发层**：够让只认 OpenAI 协议的客户端跑起来，不是生产级网关。",
        ]
    )


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_PORT",
    "SUPPORTED_UPSTREAMS",
    "UPSTREAM_TRANSPORTS",
    "Forward",
    "ProxyError",
    "ProxyPlan",
    "ProxyServer",
    "UnsupportedUpstream",
    "describe",
    "http_forward",
    "openai_to_provider",
    "plan_for",
    "port_in_use",
    "provider_to_openai",
]
