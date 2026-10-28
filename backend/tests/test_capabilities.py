"""三级能力元数据（MATRIX 4.10）：三层回退、`source` 可见、不知道就说不知道。

这份文件的主角是**来源**和**空值**：

- 同一个 `context_window` 可能来自手工覆盖 / 运行时缓存 / 出厂 snapshot，可信度
  完全不同，所以每条用例都断言 `source` 与逐字段的 `field_sources`；
- 三层都没有时必须 `source="unknown"` 且**七个字段全 None**——一旦有人给未知模型
  填了"保守默认值"，`test_unknown_*` 会立刻红。

全程离线：不写网络、不装依赖，`refresh_cache()` 的用例断言它**明确报错**。
"""

from __future__ import annotations

import json

import pytest

from app import capabilities as cap
from app import providers as pv

# ── snapshot 的覆盖范围 ────────────────────────────────────────────────────


def test_snapshot_covers_all_sixteen_providers():
    """16 家 provider 全部在 snapshot 里有键——**空表也是键**（bedrock / custom）。"""
    assert set(cap.SNAPSHOT) == set(pv.PROVIDERS)
    assert len(cap.SNAPSHOT) == 16


def test_snapshot_covers_every_default_model():
    """有默认模型的 14 家，snapshot 必须给出那条模型的条目。

    `bedrock` / `custom` 的 `default_model` 是空串（`models_known=False`，清单只能由
    SDK 或用户给），所以它们是**唯二**不覆盖默认模型的两家——这不是漏抄。
    """
    missing = [
        f"{profile.id}/{profile.default_model}"
        for profile in pv.PROVIDERS.values()
        if profile.default_model and profile.default_model not in cap.SNAPSHOT.get(profile.id, {})
    ]
    assert not missing, f"snapshot 缺默认模型：{missing}"
    without_default = {
        profile.id for profile in pv.PROVIDERS.values() if not profile.default_model
    }
    assert without_default == {"bedrock", "custom"}
    assert cap.SNAPSHOT["bedrock"] == {} and cap.SNAPSHOT["custom"] == {}


def test_snapshot_does_not_drift_from_the_provider_table():
    """snapshot 与 `providers.py` 的 `models` 表不许漂移：两处数字不一致时，
    用户会看到"能力表说支持 vision，实际发图 400"。"""
    for provider, models in cap.SNAPSHOT.items():
        profile = pv.PROVIDERS[provider]
        for model, values in models.items():
            declared = profile.models.get(model)
            if not declared:
                continue
            for field in ("context_window", "supports_tools", "supports_vision", "supports_reasoning"):
                assert values[field] == declared[field], f"{provider}/{model}/{field} 漂移了"


def test_snapshot_leaves_undocumented_values_empty():
    """不许猜：只有调研里真有出处的 `max_output` 才填，价格表里没有的就留 None。"""
    assert cap.resolve("gpt-5.4-mini", "openai").max_output == 128000  # 调研 §7.2
    assert cap.resolve("gpt-5.4", "openai").max_output is None
    openrouter = cap.resolve("anthropic/claude-sonnet-4.6", "openrouter")
    assert openrouter.input_price is None and openrouter.output_price is None
    assert "input_price" in openrouter.unknown_fields()


# ── 三层回退 ────────────────────────────────────────────────────────────────


def test_snapshot_is_the_third_tier(tmp_path):
    result = cap.resolve("deepseek-v4-flash", "deepseek", data_dir=tmp_path)
    assert result.source == "snapshot"
    assert result.context_window == 131072
    assert result.supports_tools is True
    assert set(result.field_sources.values()) == {"snapshot"}


def test_cache_tier_beats_snapshot_field_by_field(tmp_path):
    """缓存只给了窗口 → 窗口来自 cache，价格仍逐字段回退到 snapshot（混源是常态）。"""
    pv.record_capability(tmp_path, "deepseek", "deepseek-v4-flash", {"context_window": 999})
    result = cap.resolve("deepseek-v4-flash", "deepseek", data_dir=tmp_path)
    assert result.source == "cache"
    assert result.context_window == 999
    assert result.field_sources["context_window"] == "cache"
    assert result.input_price == 0.14
    assert result.field_sources["input_price"] == "snapshot"


def test_override_tier_beats_cache_and_snapshot(tmp_path):
    """三级同时有值时，手工覆盖必须赢——用户写下的话不该被缓存盖掉。"""
    pv.record_capability(tmp_path, "deepseek", "deepseek-v4-flash", {"context_window": 999})
    path = cap.write_override(
        tmp_path, "deepseek", "deepseek-v4-flash", {"context_window": 12345}
    )
    assert path.name == "capabilities.json"
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["deepseek"]["deepseek-v4-flash"]["context_window"] == 12345

    result = cap.resolve("deepseek-v4-flash", "deepseek", data_dir=tmp_path)
    assert result.source == "override"
    assert result.context_window == 12345
    assert result.field_sources["context_window"] == "override"


def test_explicit_overrides_argument_is_the_first_tier():
    """单测 / 脚本要能在不碰磁盘的情况下压过一切（`OVERRIDES` 的入参形态）。"""
    result = cap.resolve(
        "deepseek-v4-flash",
        "deepseek",
        overrides={"deepseek": {"deepseek-v4-flash": {"supports_vision": True}}},
    )
    assert result.source == "override"
    assert result.supports_vision is True
    # 只覆盖一个字段不该抹掉另外六个。
    assert result.context_window == 131072
    assert result.field_sources["context_window"] == "snapshot"


def test_unknown_model_returns_none_for_every_field():
    """**三层都没有就是不知道**：不许填 8192 / False 这类"看起来像真的"默认值。"""
    result = cap.resolve("totally-new-model", "openai")
    assert result.source == "unknown"
    assert result.known is False
    for name in cap.CAPABILITY_FIELDS:
        assert result.value(name) is None, f"{name} 不许有默认值"
    assert result.field_sources == {}
    payload = result.to_dict()
    assert payload["context_window"] is None
    assert payload["supports_tools"] is None
    assert payload["unknown_fields"] == list(cap.CAPABILITY_FIELDS)


def test_provider_is_inferred_from_the_model_name():
    result = cap.resolve("claude-sonnet-5")
    assert result.provider == "anthropic"
    assert result.source == "snapshot"


def test_unknown_field_name_is_rejected():
    with pytest.raises(ValueError, match="未知能力字段"):
        cap.resolve("gpt-5.4", "openai").value("supports_telepathy")


# ── 刷新缓存：本机无网络必须明确报错 ────────────────────────────────────────


def test_refresh_cache_reports_network_unavailable():
    """不许"静默成功"：这条命令的语义就是联网拉目录，没网就该说没网。"""
    with pytest.raises(cap.NetworkUnavailable) as excinfo:
        cap.refresh_cache()
    message = str(excinfo.value)
    assert "联网" in message
    assert "snapshot" in message, "必须告诉用户现在用的是 snapshot"


def test_broken_override_file_falls_back_to_snapshot(tmp_path):
    """覆盖表坏了只该退回下一档，不该让查询失败。"""
    (tmp_path / "capabilities.json").write_text("{ 这不是 json", encoding="utf-8")
    assert cap.load_overrides(tmp_path) == {}
    assert cap.resolve("deepseek-v4-flash", "deepseek", data_dir=tmp_path).source == "snapshot"


def test_override_file_drops_junk_keys(tmp_path):
    """`capabilities.json` 是给人看的：只留白名单字段，垃圾键写不进去。"""
    (tmp_path / "capabilities.json").write_text(
        json.dumps({"deepseek": {"deepseek-chat": {"context_window": 1, "junk": 2}}}),
        encoding="utf-8",
    )
    assert cap.load_overrides(tmp_path) == {
        "deepseek": {"deepseek-chat": {"context_window": 1}}
    }


# ── CLI：`orbit model --capabilities …` ────────────────────────────────────


def test_cli_capabilities_prints_values_with_sources(tmp_path, capsys):
    from app.cli.commands import model_extra2  # noqa: F401 —— 导入即接线
    from app.cli.main import main

    code = main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "model",
            "--capabilities",
            "--cap-model",
            "deepseek-v4-flash",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "deepseek-v4-flash" in out
    assert "snapshot" in out, "来源必须打在表里"
    assert "131,072" in out


def test_cli_capabilities_refresh_reports_offline(tmp_path, capsys):
    from app.cli.commands import model_extra2  # noqa: F401
    from app.cli.main import main

    code = main(["--data-dir", str(tmp_path / "data"), "model", "--cap-refresh"])
    assert code == 1
    err = capsys.readouterr().err
    assert "联网" in err


def test_cli_capabilities_warns_when_nothing_is_known(tmp_path, capsys):
    from app.cli.commands import model_extra2  # noqa: F401
    from app.cli.main import main

    code = main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "model",
            "--capabilities",
            "--cap-model",
            "no-such-model",
        ]
    )
    assert code == 0
    captured = capsys.readouterr()
    assert "不给默认值" in captured.err
    assert "未知" in captured.out
