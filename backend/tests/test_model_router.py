"""per-model 路由（MATRIX 4.3）：给定模型名，决定 provider / 端点 / 能力。

三条硬要求都在这份文件里锁死：已知模型路由正确、未知模型**不猜**、
用户显式选过的 provider 不被反查覆盖。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app import model_router as mr
from app import providers as pv


def settings_for(model: str = "", provider: str = "", base_url: str = "") -> SimpleNamespace:
    """`route()` 真正需要的字段就这几个（`ModelSettings` 协议的最小面）。"""
    return SimpleNamespace(model=model, model_provider=provider, model_base_url=base_url)


# ── 已知模型 ────────────────────────────────────────────────────────────────


def test_known_model_resolves_to_its_provider_and_capabilities():
    decision = mr.route("gpt-5.4", settings_for(base_url="https://api.openai.com/v1"))
    assert decision.supported is True
    assert decision.provider == "openai"
    assert decision.transport == "chat_completions"
    assert decision.context_window == 400000
    assert decision.supports_tools is True
    assert decision.supports_reasoning is True
    assert decision.source == mr.SOURCE_MODEL


def test_capabilities_are_per_model_not_per_provider():
    """同一家的不同模型能力不同：`deepseek-chat` 不支持 reasoning，`deepseek-v4-pro` 支持。"""
    pro = mr.route("deepseek/deepseek-v4-pro")
    chat = mr.route("deepseek/deepseek-chat")
    assert pro.supports_reasoning is True
    assert chat.supports_reasoning is False
    assert pro.provider == chat.provider == "deepseek"


def test_capability_sources_are_reported():
    """每个数字都要能回答"这是查到的还是兜底值"。"""
    decision = mr.route("gpt-5.4")
    assert set(decision.capability_sources) == set(pv.CAPABILITY_FIELDS)
    assert decision.capability_sources["context_window"] == "manual"


def test_transport_follows_the_provider_profile():
    assert mr.route("claude-sonnet-5").transport == "anthropic_messages"
    assert mr.route("gemini-3.5-flash").transport == "gemini"
    assert mr.route("glm-5.2").transport == "chat_completions"


# ── 未知模型：不猜 ──────────────────────────────────────────────────────────


def test_unknown_model_is_explicitly_unsupported():
    decision = mr.route("totally-made-up-model")
    assert decision.supported is False
    assert decision.provider == ""
    assert "不在任何 provider 的模型表里" in decision.reason
    assert decision.source == mr.SOURCE_UNKNOWN
    # 不支持时不许填"看起来合理"的猜测值
    assert decision.context_window == 0
    assert decision.transport == ""


def test_empty_model_name_is_rejected():
    assert mr.route("").supported is False
    assert mr.route("   ").supported is False


def test_unknown_provider_prefix_falls_back_to_model_lookup():
    """`notaprovider/some-model` 不是已知 provider 前缀，所以整串当模型名——不猜、也不炸。"""
    decision = mr.route("notaprovider/some-model")
    assert decision.supported is False
    assert decision.source == mr.SOURCE_UNKNOWN
    assert "provider/model" in decision.reason


def test_explicit_provider_without_the_model_falls_back_to_lookup():
    """显式 provider 只用来消歧：那家根本没有这个模型时，退回模型表反查。"""
    decision = mr.route("claude-sonnet-5", settings_for(provider="openai"))
    assert decision.supported is True
    assert decision.provider == "anthropic"


# ── 歧义：不猜，让人来定 ────────────────────────────────────────────────────


def test_ambiguous_model_is_not_guessed():
    """`kimi-k2.5` 同时登记在 kimi-coding 与 moonshot 下——反查没有唯一答案。"""
    decision = mr.route("kimi-k2.5")
    assert decision.supported is False
    assert decision.source == mr.SOURCE_AMBIGUOUS
    assert "kimi-coding" in decision.reason and "moonshot" in decision.reason
    assert "不猜" in decision.reason


def test_explicit_provider_wins_over_reverse_lookup():
    """用户显式选过 moonshot，就不该被反查"猜"成 kimi-coding。"""
    decision = mr.route("kimi-k2.5", settings_for(provider="moonshot"))
    assert decision.supported is True
    assert decision.provider == "moonshot"
    assert decision.source == mr.SOURCE_EXPLICIT


def test_provider_prefix_wins_over_settings():
    decision = mr.route("kimi-coding/kimi-k2.5", settings_for(provider="moonshot"))
    assert decision.provider == "kimi-coding"


def test_explicit_provider_with_unknown_model_says_so():
    decision = mr.route("deepseek/not-a-model", settings_for())
    assert decision.supported is False
    assert decision.provider == "deepseek", "报错要指回用户写的那家"
    assert "模型表里没有" in decision.reason


# ── 前缀解析：别把带斜杠的模型名拆碎 ───────────────────────────────────────


def test_openrouter_model_name_keeps_its_inner_slash():
    provider, model = mr.split_model("openrouter/anthropic/claude-sonnet-4.6")
    assert provider == "openrouter"
    assert model == "anthropic/claude-sonnet-4.6"


def test_qwen_style_name_is_not_mistaken_for_a_prefix():
    """`qwen/qwen3-plus` 里的 qwen 是已知 provider，但它其实是 OpenRouter 目录里的模型名。

    第 1 步按前缀切成 (qwen, qwen3-plus) 会落空，所以整串要再当模型名试一次——
    这不是"猜"：唯一的精确命中在 openrouter 的模型表里。
    """
    decision = mr.route("qwen/qwen3-plus")
    assert decision.supported is True
    assert decision.provider == "openrouter"
    assert decision.model == "qwen/qwen3-plus"


# ── 端点 ────────────────────────────────────────────────────────────────────


def test_base_url_uses_settings_only_for_the_current_provider():
    """自建网关不该被顺带套到别家的模型上——那会得到 404 而不是"你没配这家"。"""
    gateway = settings_for(provider="openai", base_url="http://127.0.0.1:8000/v1")
    # 同一家（当前配置指的就是 openai）→ 用用户填的端点
    assert mr.route("gpt-5.4", gateway).base_url == "http://127.0.0.1:8000/v1"
    # 别家的模型 → 用它自己的默认端点，绝不跟着网关走
    assert mr.route("claude-sonnet-5", gateway).base_url == "https://api.anthropic.com"


# ── 线协议放行状态与路由的关系 ──────────────────────────────────────────────
#
# **基线变更（本文件唯一一处）**：这条原来叫 `test_unsupported_transport_is_explained_not_hidden`，
# 钉的是"bedrock 的线协议未实现，`route()` 说 supported=False 且原因里有 boto3"。
# MATRIX 4.2 放行 `bedrock_converse` 之后，`UNSUPPORTED_TRANSPORTS` 变成空表，这个前提没了，
# 所以改成钉新契约：**路由说 supported 只表示线协议实现了，不等于本机配置齐了**——
# 凭证不齐由 `providers.build_chat_model` 抛 `MissingCredential` 并点名环境变量。


def test_released_transport_routes_as_supported():
    decision = mr.route("bedrock/anthropic.claude-v2")
    assert decision.supported is True
    assert decision.provider == "bedrock"


def test_no_transport_is_left_unsupported():
    assert pv.UNSUPPORTED_TRANSPORTS == {}
    assert "bedrock_converse" in pv.SUPPORTED_TRANSPORTS


# ── 清单未知的 provider 不该被"表里没有"判死 ───────────────────────────────


def test_provider_with_unknown_model_list_is_allowed():
    """`custom` / `bedrock` 的模型清单只能实时拉取，表里没有不等于不支持。"""
    decision = mr.route("custom/my-local-model")
    assert decision.supported is True
    assert decision.provider == "custom"


def test_decision_serializes_for_json_output():
    payload = mr.route("gpt-5.4").to_dict()
    assert payload["supported"] is True
    assert payload["provider"] == "openai"
    assert isinstance(payload["capability_sources"], dict)


@pytest.mark.parametrize("model", ["gpt-5.4", "claude-sonnet-5", "gemini-3.5-flash", "glm-5.2"])
def test_every_supported_decision_has_a_base_url(model):
    assert mr.route(model).base_url


# ── CLI 接线：`orbit model --route` / `--reasoning` / `--aux` ───────────────


def test_cli_flags_are_attached_to_the_existing_model_command():
    """`model_extra` 一导入就该把开关挂到已有的 `model` 上（不另立顶层命令）。

    为什么是开关不是子命令：`model` 已有位置参数 `selection`，argparse 会把第一个
    词先喂给它，`orbit model routing` 会被吃成 `selection="routing"`。
    """
    from app.cli.commands import model_extra
    from app.cli.spec import REGISTRY

    parent = REGISTRY.get("model")
    assert parent is not None
    flags = {option.flags[0] for option in parent.options}
    for flag in ("--route", "--reasoning", "--aux", "--aux-set"):
        assert flag in flags, f"`model {flag}` 没挂上"
    assert model_extra.WIRING["model flags"] in {"flags", "already"}


def test_cli_routing_prints_the_decision(tmp_path, capsys):
    from app.cli.commands import model_extra  # noqa: F401 —— 导入即接线
    from app.cli.main import main

    code = main(["--data-dir", str(tmp_path / "data"), "model", "--route", "gpt-5.4"])
    assert code == 0
    out = capsys.readouterr().out
    assert "openai" in out
    assert "400000" in out.replace(",", "")


def test_cli_reasoning_lists_the_translators(tmp_path, capsys):
    from app.cli.commands import model_extra  # noqa: F401
    from app.cli.main import main

    assert main(["--data-dir", str(tmp_path / "data"), "model", "--reasoning"]) == 0
    out = capsys.readouterr().out
    for provider in ("deepseek", "anthropic", "gemini", "openai"):
        assert provider in out


def test_cli_reasoning_shows_request_shape(tmp_path, capsys):
    from app.cli.commands import model_extra  # noqa: F401
    from app.cli.main import main

    code = main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "model",
            "--reasoning-provider",
            "deepseek",
            "--reasoning-level",
            "high",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "reasoning_effort" in out
    assert "thinking" in out


def test_cli_reasoning_rejects_an_unsupported_provider(tmp_path, capsys):
    """不支持的 provider 必须给出可行动的报错（`main` 把 `CliError` 折成非零退出码）。"""
    from app.cli.commands import model_extra  # noqa: F401
    from app.cli.main import main

    code = main(["--data-dir", str(tmp_path / "data"), "model", "--reasoning-provider", "nope"])
    assert code != 0
    assert "没有推理线格式翻译器" in capsys.readouterr().err


def test_cli_existing_model_behaviour_is_untouched(tmp_path, capsys):
    """只前置拦截、不替换语义：`orbit model --list` 还是原来那个实现。"""
    from app.cli.commands import model_extra  # noqa: F401
    from app.cli.main import main

    assert main(["--data-dir", str(tmp_path / "data"), "model", "--list"]) == 0
    assert "deepseek" in capsys.readouterr().out
