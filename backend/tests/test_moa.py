"""MoA 运行时（MATRIX 4.11）：并发、失败容忍、预算上限。

三个判据各自对应一条硬要求，全部用注入的假 `call` 验证——零网络。
"""

from __future__ import annotations

import asyncio

import pytest

from app import moa


def plan(*providers: str, aggregator: str = "agg", **kwargs) -> moa.MoAPlan:
    return moa.MoAPlan(
        proposers=tuple(moa.Proposer(provider=name, model=f"{name}-model") for name in providers),
        aggregator=moa.Proposer(provider=aggregator, model="agg-model"),
        **kwargs,
    )


# ── 并发 ────────────────────────────────────────────────────────────────────


async def test_three_proposers_run_concurrently():
    """串行跑就失去了"多个便宜模型同时干活"的意义——延迟会随模型数线性增长。"""
    in_flight = 0
    peak = 0
    seen: list[str] = []

    async def call(proposer, prompt):
        nonlocal in_flight, peak
        if proposer.provider == "agg":
            return "汇总答案"
        seen.append(proposer.provider)
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02)
        in_flight -= 1
        return f"回答来自 {proposer.provider}"

    result = await moa.run(plan("a", "b", "c"), "问题", call=call)
    assert peak == 3, f"峰值并发应当为 3，实际 {peak}"
    assert sorted(seen) == ["a", "b", "c"]
    assert result.text == "汇总答案"
    assert len(result.proposals) == 3
    assert all(item.ok for item in result.proposals)


async def test_aggregator_receives_every_proposal():
    captured: dict[str, str] = {}

    async def call(proposer, prompt):
        if proposer.provider == "agg":
            captured["prompt"] = prompt
            return "汇总"
        return f"{proposer.provider} 的观点"

    await moa.run(plan("a", "b"), "问题", call=call)
    prompt = captured["prompt"]
    assert "a 的观点" in prompt and "b 的观点" in prompt
    assert "## 用户问题\n问题" in prompt
    assert moa.AGGREGATOR_SYSTEM in prompt


# ── 失败容忍 ────────────────────────────────────────────────────────────────


async def test_one_proposer_failing_still_produces_a_result():
    """少一份参考回答只是质量降一点，比"整轮什么都没有"好得多。"""
    captured: dict[str, str] = {}

    async def call(proposer, prompt):
        if proposer.provider == "agg":
            captured["prompt"] = prompt
            return "汇总答案"
        if proposer.provider == "b":
            raise RuntimeError("boom")
        return f"{proposer.provider} 的观点"

    result = await moa.run(plan("a", "b", "c"), "问题", call=call)
    assert result.text == "汇总答案"
    assert result.missing == ["b/b-model"]
    assert [item.ok for item in result.proposals] == [True, False, True]
    # 聚合提示词里必须写明"缺了谁"，否则汇总模型会把 2 份参考当成 2 个全部角度
    assert "未能作答" in captured["prompt"]
    assert "b/b-model" in captured["prompt"]


async def test_empty_answer_counts_as_missing_not_as_a_blank_proposal():
    """空文本是"没答上来"，不能当成"答了但内容为空"混进参考回答里。"""

    async def call(proposer, prompt):
        if proposer.provider == "agg":
            return "汇总"
        return "   " if proposer.provider == "a" else "b 的观点"

    result = await moa.run(plan("a", "b"), "问题", call=call)
    assert result.missing == ["a/a-model"]
    assert result.text == "汇总"


async def test_all_proposers_failing_raises_instead_of_calling_the_aggregator():
    """全挂时不该去问 aggregator：它只会拿到一堆错误信息，白花一次钱。"""
    called: list[str] = []

    async def call(proposer, prompt):
        called.append(proposer.provider)
        raise RuntimeError("down")

    with pytest.raises(moa.MoAFailed) as excinfo:
        await moa.run(plan("a", "b"), "问题", call=call)
    assert "agg" not in called
    assert "a/a-model" in str(excinfo.value) and "b/b-model" in str(excinfo.value)


# ── 预算 ────────────────────────────────────────────────────────────────────


def test_per_slot_cap_reserves_a_share_for_the_aggregator():
    """预算按 (proposer 数 + 1) 均分：不给汇总那一步留份额，它必然超预算。"""
    budgeted = plan("a", "b", "c", budget_tokens=400, max_tokens=4096)
    assert moa.per_slot_cap(budgeted) == 100  # 400 / (3 + 1)
    unlimited = plan("a", "b", "c", max_tokens=512)
    assert moa.per_slot_cap(unlimited) == 512


def test_truncate_to_tokens_is_idempotent_under_the_limit():
    assert moa.truncate_to_tokens("短", 100) == ("短", False)
    text, cut = moa.truncate_to_tokens("啊" * 500, 50)
    assert cut is True
    assert len(text) <= 60


async def test_budget_caps_every_proposal():
    """预算是**硬上限**：宁可截断，也不能因为一次 /moa 把当月配额烧完。"""
    limits: list[int] = []

    async def call(proposer, prompt):
        if proposer.provider == "agg":
            return "汇总"
        return "长" * 5000

    result = await moa.run(plan("a", "b", "c", budget_tokens=400, max_tokens=4096), "问题",
                           call=call)
    assert result.budget_limited is True
    assert all(item.truncated for item in result.proposals)
    # 每份参考都被压到 100 token 以内（400 / 4 个槽位）
    assert all(item.tokens <= 100 for item in result.proposals)
    del limits


async def test_no_budget_means_no_truncation():
    async def call(proposer, prompt):
        if proposer.provider == "agg":
            return "汇总"
        return "答" * 100

    result = await moa.run(plan("a", "b"), "问题", call=call)
    assert result.budget_limited is False
    assert not any(item.truncated for item in result.proposals)


# ── 计划与预设 ──────────────────────────────────────────────────────────────


def test_plan_rejects_empty_proposer_list():
    with pytest.raises(ValueError, match="至少需要一个 proposer"):
        moa.MoAPlan(proposers=(), aggregator=moa.Proposer(provider="agg"))


def test_run_without_call_or_settings_refuses_to_invent_an_answer():
    async def scenario() -> None:
        await moa.run(plan("a"), "问题")

    with pytest.raises(ValueError, match="需要 `call`"):
        asyncio.run(scenario())


def test_plan_from_preset_matches_the_stored_shape():
    """`orbit moa configure` 存的就是这个形状（Hermes 字段名），要能直接跑。"""
    preset = {
        "reference_models": [
            {"provider": "deepseek", "model": "deepseek-v4-flash"},
            {"provider": "openai", "model": "gpt-5.4"},
        ],
        "aggregator": {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"},
        "max_tokens": 2048,
        "enabled": True,
    }
    built = moa.plan_from_preset(preset, "default")
    assert [item.provider for item in built.proposers] == ["deepseek", "openai"]
    assert built.aggregator.provider == "openrouter"
    assert built.max_tokens == 2048
    assert built.name == "default"


def test_plan_from_preset_rejects_a_missing_aggregator():
    with pytest.raises(ValueError, match="缺 aggregator"):
        moa.plan_from_preset({"reference_models": [{"provider": "deepseek"}]}, "x")


def test_plan_serializes_for_json_output():
    payload = plan("a", "b").to_dict()
    assert payload["name"] == "default"
    assert len(payload["proposers"]) == 2
    assert payload["aggregator"]["provider"] == "agg"


# ── CLI：`orbit moa run` ────────────────────────────────────────────────────


def test_cli_moa_run_is_attached_as_a_subcommand(tmp_path, capsys):
    """`moa` 没有位置参数，所以 `moa run` 能正常作为子命令挂上去。"""
    from app.cli.commands import model_extra  # noqa: F401 —— 导入即接线
    from app.cli.main import main
    from app.cli.spec import REGISTRY

    assert REGISTRY.get("moa").child("run") is not None
    base = ["--data-dir", str(tmp_path / "data")]
    assert (
        main(
            [
                *base,
                "moa",
                "configure",
                "default",
                "--reference",
                "deepseek/deepseek-v4-flash",
                "--aggregator",
                "openai/gpt-5.4",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main([*base, "moa", "run", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert "deepseek/deepseek-v4-flash" in out
    assert "openai/gpt-5.4" in out
