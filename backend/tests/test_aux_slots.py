"""辅助模型槽位（MATRIX 4.9）：专用配置优先、未配置时 `inherited=True`、成本估算。

`inherited` 是这份文件的主角：把"其实没配、跑的是主模型"这件事做成可见的返回值，
否则用户会一直以为自己在用便宜的小模型，直到账单出来。
"""

from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from app import aux_slots as aux
from app.config import Settings


def make_settings(tmp_path, *, auxiliary: dict | None = None) -> Settings:
    settings = Settings(
        model="gpt-5.4",
        model_provider="openai",
        model_base_url="https://api.openai.com/v1",
        model_api_key=SecretStr("sk-test-123456"),
        data_dir=tmp_path / "data",
        workspace=tmp_path / "files",
    )
    settings.prepare()
    if auxiliary is not None:
        (settings.data_dir / "settings.json").write_text(
            json.dumps({"auxiliary": auxiliary}), encoding="utf-8"
        )
    return settings


# ── 继承语义 ────────────────────────────────────────────────────────────────


def test_unconfigured_slot_inherits_the_main_model_and_says_so(tmp_path):
    """没配就退回主模型——但**必须**标 `inherited=True`，让"其实没配"可见。"""
    settings = make_settings(tmp_path)
    slot = aux.resolve_slot("compression", settings)
    assert slot.inherited is True
    assert slot.source == "main"
    assert slot.provider == "openai"
    assert slot.model == "gpt-5.4"
    assert slot.base_url == "https://api.openai.com/v1"


def test_dedicated_slot_wins_over_the_main_model(tmp_path):
    settings = make_settings(
        tmp_path, auxiliary={"title": {"provider": "deepseek", "model": "deepseek-v4-flash"}}
    )
    slot = aux.resolve_slot("title", settings)
    assert slot.inherited is False
    assert slot.source == "slot"
    assert slot.provider == "deepseek"
    assert slot.model == "deepseek-v4-flash"
    assert slot.base_url == "https://api.deepseek.com/v1", "专用槽位用自己的默认端点"


def test_provider_only_config_uses_that_providers_default_model(tmp_path):
    """只填 provider 不填 model：取该家默认模型，而不是拼一个不存在的组合。"""
    settings = make_settings(tmp_path, auxiliary={"summary": {"provider": "deepseek"}})
    slot = aux.resolve_slot("summary", settings)
    assert slot.inherited is False
    assert slot.model == "deepseek-v4-flash"


def test_inheritance_is_per_slot(tmp_path):
    """配了 title 不等于配了 compression——槽位之间不许互相串。"""
    settings = make_settings(
        tmp_path, auxiliary={"title": {"provider": "deepseek", "model": "deepseek-chat"}}
    )
    assert aux.resolve_slot("title", settings).inherited is False
    assert aux.resolve_slot("compression", settings).inherited is True


def test_timeout_defaults_follow_hermes(tmp_path):
    """压缩要读整段历史 → 120s；标题/分类只读一小段 → 30s。"""
    settings = make_settings(tmp_path)
    assert aux.resolve_slot("compression", settings).timeout == 120
    assert aux.resolve_slot("title", settings).timeout == 30
    assert aux.DEFAULT_TIMEOUT["summary"] == 120


def test_timeout_can_be_overridden(tmp_path):
    settings = make_settings(
        tmp_path, auxiliary={"classifier": {"provider": "deepseek", "timeout": 8}}
    )
    assert aux.resolve_slot("classifier", settings).timeout == 8


def test_broken_config_falls_back_to_inheritance(tmp_path):
    """配置文件坏了只该退回"全部继承主模型"，不该让摘要直接崩。"""
    settings = make_settings(tmp_path)
    (settings.data_dir / "settings.json").write_text("{ 这不是 json", encoding="utf-8")
    assert aux.resolve_slot("summary", settings).inherited is True


def test_unknown_slot_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="未知槽位"):
        aux.resolve_slot("vision", make_settings(tmp_path))


def test_all_four_slots_resolve(tmp_path):
    slots = aux.resolve_all(make_settings(tmp_path))
    assert [item.slot for item in slots] == list(aux.SLOTS)
    assert set(aux.SLOTS) == {"summary", "title", "compression", "classifier"}


# ── 成本估算 ────────────────────────────────────────────────────────────────


def test_cost_estimate_uses_the_price_table(tmp_path):
    settings = make_settings(
        tmp_path, auxiliary={"title": {"provider": "deepseek", "model": "deepseek-v4-flash"}}
    )
    estimate = aux.estimate_cost("title", {"input": 1_000_000, "output": 1_000_000}, settings)
    # deepseek-v4-flash：输入 $0.14 / 输出 $0.28（每 1M token）
    assert estimate.usd == pytest.approx(0.42)
    assert estimate.priced is True
    assert estimate.inherited is False


def test_cost_estimate_marks_the_inherited_slot(tmp_path):
    """继承主模型的槽位按**主模型**的单价算——这正是"没配辅助模型"的真实代价。"""
    estimate = aux.estimate_cost("compression", {"input": 1_000_000, "output": 1_000_000},
                                 make_settings(tmp_path))
    assert estimate.inherited is True
    assert estimate.model == "gpt-5.4"
    assert estimate.usd == pytest.approx(2.5 + 15.0)


def test_cost_estimate_splits_a_bare_token_count(tmp_path):
    """只给总数时按 8:2 拆输入/输出（摘要类任务的典型比例）。"""
    estimate = aux.estimate_cost("summary", 1000, make_settings(tmp_path))
    assert estimate.input_tokens == 800
    assert estimate.output_tokens == 200


def test_unknown_model_is_flagged_as_unpriced_not_free(tmp_path):
    """查不到单价时按中档兜底并标 `priced=False`：取 0 会让"未知"看起来免费。"""
    settings = make_settings(
        tmp_path, auxiliary={"summary": {"provider": "openai", "model": "mystery-model"}}
    )
    estimate = aux.estimate_cost("summary", 1_000_000, settings)
    assert estimate.priced is False
    assert estimate.usd > 0
    assert "兜底" in estimate.note


def test_local_endpoints_are_genuinely_free(tmp_path):
    """本地端点的 0 是**正确**的值，不是"估不出来"。"""
    settings = make_settings(
        tmp_path, auxiliary={"classifier": {"provider": "ollama", "model": "llama3.2"}}
    )
    estimate = aux.estimate_cost("classifier", 500_000, settings)
    assert estimate.priced is True
    assert estimate.usd == 0.0


def test_estimate_requires_a_model_somehow():
    with pytest.raises(ValueError, match="没有可用模型"):
        aux.estimate_cost("title", 100)


def test_price_of_reports_whether_it_was_found():
    (price, found) = aux.price_of("deepseek", "deepseek-v4-pro")
    assert found is True and price[0] > 0
    (fallback, found) = aux.price_of("deepseek", "not-a-real-model")
    assert found is False and fallback == aux.FALLBACK_PRICE


def test_price_table_documents_its_source():
    """单价是"公开标价，会变"——这句话必须写在源码里，不能只存在脑子里。"""
    import inspect

    source = inspect.getsource(aux)
    assert "公开标价" in source
    assert "models.dev" in source


# ── CLI：`orbit model --aux …` ─────────────────────────────────────────────


def test_cli_aux_lists_slots_and_marks_inheritance(tmp_path, capsys):
    from app.cli.commands import model_extra  # noqa: F401 —— 导入即接线
    from app.cli.main import main

    base = ["--data-dir", str(tmp_path / "data")]
    assert main([*base, "model", "--aux"]) == 0
    out = capsys.readouterr().out
    for slot in aux.SLOTS:
        assert slot in out
    assert "继承（未配置）" in out, "没配就得显示成继承，不能装成配了"


def test_cli_aux_set_then_unset_round_trip(tmp_path, capsys):
    from app.cli.commands import model_extra  # noqa: F401
    from app.cli.main import main

    base = ["--data-dir", str(tmp_path / "data")]
    assert main([*base, "model", "--aux-set", "title=moonshot/kimi-k2.5"]) == 0
    capsys.readouterr()
    assert main([*base, "model", "--aux"]) == 0
    assert "moonshot/kimi-k2.5" in capsys.readouterr().out
    assert main([*base, "model", "--aux-unset", "title"]) == 0
    capsys.readouterr()
    assert main([*base, "model", "--aux"]) == 0
    assert "moonshot/kimi-k2.5" not in capsys.readouterr().out


def test_cli_aux_cost_prints_an_estimate(tmp_path, capsys):
    from app.cli.commands import model_extra  # noqa: F401
    from app.cli.main import main

    base = ["--data-dir", str(tmp_path / "data")]
    assert main([*base, "model", "--aux-cost", "compression", "--aux-tokens", "1000000"]) == 0
    out = capsys.readouterr().out
    assert "估算" in out
    assert "inherited=True" in out, "继承主模型的槽位要把代价如实说出来"
