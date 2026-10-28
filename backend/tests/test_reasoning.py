"""推理线格式翻译表（MATRIX 4.4）：四类响应落点 + 请求参数逐家翻译。

每家至少一条 extract + 一条 to_request；不支持的 provider 必须抛 `NotSupported`
而不是返回空 dict——假装支持会让"关掉思考"静默失效。
"""

from __future__ import annotations

import pytest

from app import reasoning as rs

# ── 响应侧：四类落点 ────────────────────────────────────────────────────────


def test_deepseek_reads_reasoning_content_from_raw_payload():
    response = {
        "choices": [
            {"message": {"content": "答案", "reasoning_content": "先想一下：2+2=4"}}
        ]
    }
    part = rs.extract(response, "deepseek")
    assert part is not None
    assert part.text == "先想一下：2+2=4"
    assert part.field_name == "reasoning_content"
    assert part.provider == "deepseek"


def test_deepseek_reads_reasoning_content_from_langchain_message():
    """LangChain 把推理内容塞在 `additional_kwargs` 里，形状与原始 JSON 完全不同。"""
    response = {"additional_kwargs": {"reasoning_content": "先想一下"}}
    part = rs.extract(response, "deepseek")
    assert part is not None and part.text == "先想一下"


def test_anthropic_reads_thinking_blocks_and_keeps_signature():
    """思考块带 signature：多轮回放必须原样带回，否则下一轮 400。"""
    response = {
        "content": [
            {"type": "text", "text": "答案"},
            {"type": "thinking", "thinking": "分两步", "signature": "sig-abc"},
        ]
    }
    part = rs.extract(response, "anthropic")
    assert part is not None
    assert part.text == "分两步"
    assert part.signature == "sig-abc"
    assert part.field_name == "content[].thinking"


def test_gemini_reads_only_the_thought_parts():
    response = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "先算一下", "thought": True},
                        {"text": "答案是 4"},
                    ]
                }
            }
        ]
    }
    part = rs.extract(response, "gemini")
    assert part is not None
    assert part.text == "先算一下", "只有 thought=true 的 part 才是推理"
    assert part.field_name == "parts[].thought"


def test_openai_o_reads_responses_api_reasoning_summary():
    response = {
        "output": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "推理摘要"}]},
            {"type": "message", "content": [{"type": "output_text", "text": "答案"}]},
        ]
    }
    part = rs.extract(response, "openai")
    assert part is not None
    assert part.text == "推理摘要"
    assert part.field_name == "output[].reasoning.summary"


def test_ollama_reads_thinking_tags_from_content():
    response = {"content": "<thinking>内心独白</thinking>最终答案"}
    part = rs.extract(response, "ollama")
    assert part is not None
    assert part.text == "内心独白"


def test_tagged_thinking_can_be_stripped_from_the_visible_answer():
    """抽出来还不够：不剥离标签，内心独白会直接显示给用户。"""
    assert rs.strip_tags("<thinking>想</thinking>答案") == "答案"
    assert rs.strip_tags("没有标签") == "没有标签"


def test_missing_reasoning_returns_none_not_empty_string():
    """`None` 与 `""` 含义不同：前者是"这家没给推理"，后者是"给了但内容为空"。"""
    assert rs.extract({"choices": [{"message": {"content": "答案"}}]}, "deepseek") is None
    assert rs.extract({"candidates": []}, "gemini") is None


# ── 请求侧：五类落点 + 互斥/拒绝/强制规则 ──────────────────────────────────


def test_deepseek_sends_thinking_in_extra_body_and_effort_on_top_level():
    request = rs.to_request("high", "deepseek")
    assert request["extra_body"] == {"thinking": {"type": "enabled"}}
    assert request["top_level"] == {"reasoning_effort": "high"}


def test_deepseek_off_disables_thinking_and_drops_effort():
    request = rs.to_request(rs.OFF, "deepseek")
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert request["top_level"] == {}, "关掉时不该再发档位"


def test_anthropic_sends_budget_tokens():
    low = rs.to_request("low", "anthropic")
    high = rs.to_request("high", "anthropic")
    assert low["top_level"]["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    assert high["top_level"]["thinking"]["budget_tokens"] > 1024
    assert low["extra_body"] == {}


def test_anthropic_off_omits_the_field_entirely():
    """Anthropic 没有 `type=disabled` 这种写法，关掉的唯一方式是省略字段。"""
    request = rs.to_request(rs.OFF, "anthropic")
    assert request == {"extra_body": {}, "top_level": {}}


def test_gemini_sends_thinking_budget_and_can_turn_it_off():
    request = rs.to_request("medium", "gemini")
    config = request["top_level"]["generationConfig"]["thinkingConfig"]
    assert config["thinkingBudget"] > 0
    assert config["includeThoughts"] is True
    off = rs.to_request(rs.OFF, "gemini")["top_level"]["generationConfig"]["thinkingConfig"]
    assert off == {"thinkingBudget": 0, "includeThoughts": False}


def test_openai_o_uses_top_level_reasoning_effort():
    assert rs.to_request("medium", "openai")["top_level"] == {"reasoning_effort": "medium"}
    # 关掉用 minimal（none 会被拒）
    assert rs.to_request(rs.OFF, "openai")["top_level"] == {"reasoning_effort": "minimal"}


def test_kimi_never_sends_thinking_and_effort_together():
    """同时发两者 → 400 "cannot specify both"。这是互斥规则，不是风格问题。"""
    request = rs.to_request("high", "kimi-coding")
    assert request["extra_body"] == {}, "有档位时不能再发 thinking"
    assert request["top_level"] == {"reasoning_effort": "high"}
    off = rs.to_request(rs.OFF, "kimi-coding")
    assert off["top_level"] == {} and off["extra_body"] == {"thinking": {"type": "disabled"}}


def test_kimi_clamps_unsupported_medium_upwards():
    """Kimi 的词表是 low/high/max，没有 medium——向上 clamp，不是原样丢过去。"""
    assert rs.to_request("medium", "kimi-coding")["top_level"]["reasoning_effort"] == "high"


def test_zai_clamps_low_and_medium_to_high():
    """GLM-5.2 最低 high，发 low 会 400。"""
    assert rs.to_request("low", "zai")["top_level"]["reasoning_effort"] == "high"
    assert rs.to_request("high", "zai")["top_level"]["reasoning_effort"] == "high"
    # 但关掉必须真的发 disabled：GLM-4.5+ 默认思考是开的
    assert rs.to_request(rs.OFF, "zai")["extra_body"] == {"thinking": {"type": "disabled"}}


def test_ollama_sends_think_boolean():
    assert rs.to_request("low", "ollama")["extra_body"] == {"think": True}
    assert rs.to_request(rs.OFF, "ollama")["extra_body"] == {"think": False}


# ── 不支持的 provider：明确说不支持 ────────────────────────────────────────


def test_unsupported_provider_raises_with_an_actionable_message():
    with pytest.raises(rs.NotSupported) as excinfo:
        rs.extract({"anything": 1}, "some-random-provider")
    message = str(excinfo.value)
    assert "没有推理线格式翻译器" in message
    assert "deepseek" in message, "报错要列出已支持的那些"


def test_unsupported_provider_also_blocks_request_translation():
    """绝不能返回空 dict：那会让"我关掉思考了"变成一句谎话。"""
    with pytest.raises(rs.NotSupported):
        rs.to_request("high", "some-random-provider")


def test_illegal_level_is_rejected_with_the_supported_ladder():
    with pytest.raises(ValueError) as excinfo:
        rs.to_request("turbo", "deepseek")
    assert "low" in str(excinfo.value) and "high" in str(excinfo.value)


# ── 表本身 ──────────────────────────────────────────────────────────────────


def test_at_least_four_translators_cover_the_named_wire_formats():
    """任务要求至少 4 家：deepseek / anthropic / gemini / openai-o 系。"""
    for name in ("deepseek", "anthropic", "gemini", "openai"):
        assert name in rs.TRANSLATORS
    assert len(rs.TRANSLATORS) >= 4


def test_every_translator_declares_levels_field_and_note():
    for name, translator in rs.TRANSLATORS.items():
        assert translator.provider == name
        assert translator.field_name, f"{name} 没写响应落点"
        assert translator.note, f"{name} 没写坑（note 是给人看的）"
        assert translator.levels, f"{name} 没有可用档位"
        assert rs.OFF in translator.levels, f"{name} 没声明怎么关"


def test_describe_is_cli_ready():
    info = rs.describe("gemini")
    assert info["provider"] == "gemini"
    assert info["field"] == "parts[].thought"
    assert "high" in info["levels"]
    assert isinstance(info["note"], str)
