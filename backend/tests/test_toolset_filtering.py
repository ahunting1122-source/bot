"""工具注册层收尾的三块契约：19 域覆盖 / 分级披露与预算 / 四层过滤（+ 16 家钩子）。

分节对应 MATRIX 条目，失败时能直接看出是哪一块坏了：

1. **19 域覆盖（6.1 / 5.15）**：``TOOLSETS`` 必须能对每个域回答"覆盖了什么、为什么空"，
   且 ``UNCLASSIFIED`` 为空——未归类是"暴露面失控"的入口，必须是红。
2. **分级披露（5.11）**：``ALWAYS_EAGER`` 永不裁剪；预算裁剪可验证（裁剪前超、
   裁剪后在预算内，或如实报出 schema 地板）；tier 分层真的生效。
3. **四层过滤（6.2）**：平台 / 场景 / profile / 委托各拦一类，且 ``explain()`` 说得出
   是哪一层拦的、为什么。
4. **16 家钩子（4.1）**：``fetch_models`` 无网络必须抛 ``NetworkUnavailable``，
   **绝不返回空列表**（空列表会被上层读成"这家没有模型"）。

最后一块（钩子）放在本文件而不是新建第三个测试文件：它和上面三块是同一轮改动，
拆开会让"这一层到底齐没齐"要跑两个文件才能回答。
"""

from __future__ import annotations

import pytest

from app import providers as pv
from app import tool_catalog, toolsets
from app.browser import BROWSER_TOOLS
from app.config import Settings
from app.connectors import GITHUB_TOOLS
from app.tools import LOCAL_TOOLS, ToolRegistry
from app.web import WEB_TOOLS

#: 四张真实工具表里的全部工具名。
REAL_NAMES = [tool.name for tool in (*LOCAL_TOOLS, *BROWSER_TOOLS, *GITHUB_TOOLS, *WEB_TOOLS)]


def _registry(tmp_path) -> ToolRegistry:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    registry = ToolRegistry(settings, None)
    registry.register(BROWSER_TOOLS, lambda *a, **k: None)
    registry.register(GITHUB_TOOLS, lambda *a, **k: None)
    registry.register(WEB_TOOLS, lambda *a, **k: None)
    return registry


def _config(platform: str = "cli", *, disabled=(), enabled=()) -> dict:
    return {"platforms": {platform: {"disabled": list(disabled), "enabled": list(enabled)}}}


def _profile_config(name: str, *, disabled=(), enabled=()) -> dict:
    return {"profiles": {name: {"disabled": list(disabled), "enabled": list(enabled)}}}


# ══ 1. 19 域覆盖（6.1 / 5.15）═══════════════════════════════════════════════


def test_domains_mirror_the_catalog():
    """``toolsets.DOMAINS`` 必须与 ``tool_catalog.DOMAINS`` 逐字相同。

    两份是**故意复制**的（``tool_catalog`` 顶层 import ``toolsets``，反向 import 会成环），
    所以这条用例就是那道"复制不许漂"的门禁。域少一个、说明改一个字都会红。
    """
    assert toolsets.DOMAINS == tool_catalog.DOMAINS
    assert len(toolsets.DOMAINS) == 19


def test_every_domain_has_a_toolset_group():
    """19 个域每一个都要有 toolset 归组（可以是空元组，但不能缺条目）。"""
    missing = [domain for domain in toolsets.DOMAINS if domain not in toolsets.DOMAIN_TOOLSETS]
    assert missing == [], f"这些域没有 toolset 归组：{missing}"


def test_domain_toolsets_are_a_partition_of_declared_toolsets():
    """域 → toolset 必须是**划分**：每个 toolset 恰好属于一个域。

    允许重叠的话"域覆盖"会重复计数，看起来覆盖得比实际好——那正是这类报表最常见的谎。
    """
    seen: dict[str, str] = {}
    for domain, group in toolsets.DOMAIN_TOOLSETS.items():
        for toolset in group:
            assert toolset in toolsets.TOOLSETS, f"{domain} 引用了未声明的 toolset：{toolset}"
            assert toolset not in seen, (
                f"toolset {toolset} 同时属于 {seen[toolset]} 与 {domain}（应当是划分）"
            )
            seen[toolset] = domain
    unmapped = sorted(set(toolsets.TOOLSETS) - set(seen))
    assert unmapped == [], f"这些 toolset 不属于任何域：{unmapped}"


def test_every_tool_is_classified():
    """``UNCLASSIFIED == ()``：四张真实工具表里的每个工具都必须有 toolset 与域。"""
    unclassified = tuple(
        name for name in REAL_NAMES if toolsets.toolset_of(name) == toolsets.UNCLASSIFIED
    )
    assert unclassified == (), f"这些工具没归类，暴露面失控：{unclassified}"
    without_domain = tuple(name for name in REAL_NAMES if not toolsets.domain_of_tool(name))
    assert without_domain == (), f"这些工具归不到任何功能域：{without_domain}"


def test_local_tools_are_all_classified():
    """``LOCAL_TOOLS`` 单独再查一遍（任务书点名的判据就是这个集合）。"""
    assert tuple(n for n in (t.name for t in LOCAL_TOOLS) if toolsets.toolset_of(n) == toolsets.UNCLASSIFIED) == ()


def test_describe_by_domain_returns_nineteen_rows():
    """``describe(by_domain=True)`` 必须按 19 个域输出，且顺序与 ``DOMAINS`` 一致。"""
    rows = toolsets.describe(by_domain=True)
    assert [row["domain"] for row in rows] == list(toolsets.DOMAINS)
    for row in rows:
        assert row["label"] == toolsets.DOMAINS[row["domain"]]
        assert isinstance(row["tools"], list)
        assert row["count"] == len(row["tools"])


def test_describe_by_domain_reports_tools_of_the_real_registry():
    """按域视图里的工具名必须是真实工具名，不能是声明的影子。"""
    rows = {row["domain"]: row for row in toolsets.describe_domains(REAL_NAMES)}
    assert "local_read_file" in rows["file"]["tools"]
    assert "run_command" in rows["terminal"]["tools"]
    assert "web_search" in rows["web"]["tools"]
    assert "session_search" in rows["search"]["tools"]
    assert "tool_search" in rows["meta"]["tools"]


def test_empty_domains_must_explain_themselves():
    """空域**必须**给原因。``empty=True`` 而没有原因，下一个人只会以为是笔误。"""
    rows = toolsets.describe_domains(REAL_NAMES)
    empty = [row for row in rows if row["empty"]]
    assert empty, "19 域里应当至少有一个空域（delegation / media / …）"
    for row in empty:
        assert row["reason"].strip(), f"{row['domain']} 是空域却没写原因"
    # 8 个已知空域必须都被标出来；`mcp` 单独一类（运行时动态，见下一条用例）。
    assert {row["domain"] for row in empty if not row["dynamic"]} == set(toolsets.EMPTY_TOOLSETS)


def test_mcp_domain_is_dynamic_not_empty_capability():
    """``mcp`` 的 ``tools`` 为空是**运行时生成**，不能跟真空域混成一类。"""
    rows = {row["domain"]: row for row in toolsets.describe_domains(REAL_NAMES)}
    assert rows["mcp"]["empty"] is True
    assert rows["mcp"]["dynamic"] is True
    assert "运行时" in rows["mcp"]["reason"]


def test_toolset_descriptions_are_documented():
    for toolset, description in toolsets.TOOLSETS.items():
        assert description.strip(), f"toolset {toolset} 缺中文说明"


# ══ 2. 分级披露与预算（5.11）═══════════════════════════════════════════════


def test_estimate_tokens_is_chars_over_four():
    """``estimate_tokens`` 是字符数 / 4（向上取整），**不引 tokenizer**。"""
    assert toolsets.estimate_tokens("") == 0
    assert toolsets.estimate_tokens("abcd") == 1
    assert toolsets.estimate_tokens("abcde") == 2
    assert toolsets.estimate_tokens("a" * 4000) == 1000
    # 非字符串入参按 JSON 序列化后再量。
    assert toolsets.estimate_tokens({"a": 1}) == toolsets.estimate_tokens('{"a": 1}')


def test_always_eager_tools_are_never_delayed():
    """``ALWAYS_EAGER`` 里的核心动作必须存在、且 tier 恒为 0。"""
    required = {
        "run_command",
        "local_read_file",
        "local_write_file",
        "apply_patch",
        "todo",
        "clarify",
        "tool_search",
    }
    assert required <= toolsets.ALWAYS_EAGER
    for name in toolsets.ALWAYS_EAGER:
        assert toolsets.tier_of(name) == 0, f"{name} 在 ALWAYS_EAGER 里却不是 Tier 0"


def test_tier_layering_is_effective():
    """tier 按 toolset 声明生效：core=0 / files=1 / browser=2，未知一律按 2。"""
    assert toolsets.tier_of("tool_search") == 0
    assert toolsets.tier_of("clarify") == 0
    assert toolsets.tier_of("local_read_file") == 0  # 显式列进 ALWAYS_EAGER
    assert toolsets.tier_of("local_list_files") == 1
    assert toolsets.tier_of("run_command") == 0  # ALWAYS_EAGER 覆盖了 shell 的 1
    assert toolsets.tier_of("browser_navigate") == 2
    assert toolsets.tier_of("some_unknown_tool") == 2


def test_every_declared_toolset_has_a_tier():
    missing = [name for name in toolsets.TOOLSETS if name not in toolsets.TOOLSET_TIERS]
    assert missing == [], f"这些 toolset 没声明 tier：{missing}"


def test_budget_trimming_brings_a_large_listing_within_budget(tmp_path):
    """裁剪前超预算、裁剪后落在预算内——这是 5.11 的可验证判据。

    用合成工具集而不是真实工具集：真实工具集的**参数 schema 地板**本身就超预算
    （见下一条用例），拿它当"裁剪后达标"的证据会变成假账。
    """
    from app.tools import _trim_specs

    specs = [
        {
            "type": "function",
            "function": {
                "name": f"fake_tool_{index}",
                "description": "这是一段很长的说明。" * 60,
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            },
        }
        for index in range(40)
    ]
    before = toolsets.estimate_tokens(specs)
    assert before > toolsets.LISTING_BUDGET_TOKENS

    trimmed, over = _trim_specs(specs, toolsets.LISTING_BUDGET_TOKENS)
    after = toolsets.estimate_tokens(trimmed)
    assert over is False
    assert after <= toolsets.LISTING_BUDGET_TOKENS
    assert after < before
    # 裁剪的是描述，**不是工具**：名字与参数 schema 一个不少。
    assert [spec["function"]["name"] for spec in trimmed] == [
        spec["function"]["name"] for spec in specs
    ]
    assert all(spec["function"]["parameters"] for spec in trimmed)


def test_builtin_listing_reports_its_schema_floor(tmp_path):
    """真实 38 个内置工具：裁剪能压描述，但**压不到 4,000**，而且必须如实报出来。

    这是本层最重要的诚实点。参数 schema 是调用契约——删了它模型不是"晚点知道"，
    而是永远调不动这个工具。所以 ``_trim_specs`` 返回 ``over_budget=True``，
    由调用方报出来；真正的出路是延迟 Tier 2 工具（``app/tool_discovery.py``）。
    """
    from app.tools import _trim_specs

    registry = _registry(tmp_path)
    full = registry.specs(budget=None)
    trimmed = registry.specs()

    # 一个工具都没少，参数 schema 一个都没动。
    assert [spec["function"]["name"] for spec in full] == [
        spec["function"]["name"] for spec in trimmed
    ]
    assert [spec["function"]["parameters"] for spec in full] == [
        spec["function"]["parameters"] for spec in trimmed
    ]
    # Tier 0 的描述逐字不变（永不裁剪）。
    eager = {
        spec["function"]["name"]: spec["function"]["description"]
        for spec in full
        if spec["function"]["name"] in toolsets.ALWAYS_EAGER
    }
    trimmed_map = {spec["function"]["name"]: spec for spec in trimmed}
    for name, description in eager.items():
        assert trimmed_map[name]["function"]["description"] == description

    # 地板：把描述全删掉之后仍然超预算 —— 这才是"压不下去"的原因。
    floor = [
        {**spec, "function": {**spec["function"], "description": ""}} for spec in full
    ]
    floor_tokens = toolsets.estimate_tokens(floor)
    assert floor_tokens > toolsets.LISTING_BUDGET_TOKENS, (
        "schema 地板已经降到预算以下了：请更新 ToolRegistry.specs 的 docstring 与这条用例"
    )
    _trimmed_again, over = _trim_specs(full, toolsets.LISTING_BUDGET_TOKENS)
    assert over is True
    assert toolsets.estimate_tokens(trimmed) < toolsets.estimate_tokens(full)


def test_budget_none_means_no_trimming(tmp_path):
    """``budget=None`` = 旧行为，供对照与"我要完整描述"的场景。"""
    registry = _registry(tmp_path)
    full = registry.specs(budget=None)
    assert any(len(spec["function"]["description"]) > 100 for spec in full)


def test_specs_names_are_stable_under_trimming(tmp_path):
    """裁剪不许改变工具集合——这是既有回归闸（``test_specs_cli_matches_the_pre_filter_surface``）
    的补充保护：改名字集合等于让模型调不动某些工具。"""
    registry = _registry(tmp_path)
    expected = [tool.name for tool in (*LOCAL_TOOLS, *BROWSER_TOOLS, *GITHUB_TOOLS, *WEB_TOOLS)]
    assert [spec["function"]["name"] for spec in registry.specs()] == expected
    assert [spec["function"]["name"] for spec in registry.specs(budget=None)] == expected


# ══ 3. 四层过滤（6.2）═══════════════════════════════════════════════════════


def test_scene_layer_cron_drops_interactive_tools():
    """cron 不给交互类工具：定时任务没人能回答，问了只会空转到超时。"""
    context = toolsets.FilterContext(scene="cron")
    exposed = toolsets.resolve(
        ["ask_user", "clarify", "todo", "run_command"], context=context
    )
    assert exposed == ["run_command"]


def test_scene_layer_subagent_drops_delegate_task():
    """subagent 不给 delegate_task：子 agent 再起子 agent 会无限递归。"""
    context = toolsets.FilterContext(scene="subagent")
    exposed = toolsets.resolve(["delegate_task", "run_command", "todo"], context=context)
    assert exposed == ["run_command", "todo"]


def test_scene_layer_acp_drops_browser_login():
    """acp（编辑器内嵌）不给 vault_login：那里没人能接管浏览器登录。"""
    context = toolsets.FilterContext(scene="acp")
    assert toolsets.resolve(["vault_login", "web_search"], context=context) == ["web_search"]


def test_scene_layer_chat_excludes_nothing():
    context = toolsets.FilterContext(scene="chat")
    names = ["ask_user", "clarify", "todo", "run_command", "web_search"]
    assert toolsets.resolve(names, context=context) == names


def test_platform_layer_still_works_through_context():
    """四层里的第一层就是原来的平台层：``context.platform`` 决定看哪一段配置。"""
    config = _config("cli", disabled=["web"])
    context = toolsets.FilterContext(platform="cli")
    assert toolsets.resolve(["web_search", "run_command"], config=config, context=context) == [
        "run_command"
    ]
    other = toolsets.FilterContext(platform="telegram")
    assert toolsets.resolve(
        ["web_search", "run_command"], config=config, context=other
    ) == ["web_search", "run_command"]


def test_profile_layer_reads_per_profile_config():
    """profile 层从 ``data/tools.json`` 的 ``profiles.<name>`` 读。"""
    config = _profile_config("review-only", disabled=["run_command", "code"])
    context = toolsets.FilterContext(profile="review-only")
    exposed = toolsets.resolve(
        ["run_command", "code_execution", "local_read_file"], config=config, context=context
    )
    assert exposed == ["local_read_file"]
    # 没有这个 profile 时不收窄。
    plain = toolsets.FilterContext(profile="")
    assert toolsets.resolve(
        ["run_command", "code_execution"], config=config, context=plain
    ) == ["run_command", "code_execution"]


def test_profile_layer_supports_whitelist():
    config = _profile_config("narrow", enabled=["files"])
    context = toolsets.FilterContext(profile="narrow")
    assert toolsets.resolve(
        ["local_read_file", "web_search"], config=config, context=context
    ) == ["local_read_file"]


def test_delegated_layer_blocks_recursion_and_user_questions():
    """委托层：防无限递归 + 不绕过委派者直接问用户。"""
    context = toolsets.FilterContext(delegated=True)
    exposed = toolsets.resolve(
        ["delegate_task", "ask_user", "clarify", "run_command"], context=context
    )
    assert exposed == ["run_command"]
    # 不是被委派的一轮 → 这一层不生效。
    normal = toolsets.FilterContext(delegated=False)
    assert toolsets.resolve(["ask_user", "clarify"], context=normal) == ["ask_user", "clarify"]


def test_layers_compose_in_order():
    """四层同时生效时取交集，且平台层先拦。"""
    config = {
        **_config("cli", disabled=["web"]),
        **_profile_config("strict", disabled=["code"]),
    }
    context = toolsets.FilterContext(platform="cli", scene="cron", profile="strict", delegated=True)
    exposed = toolsets.resolve(
        ["web_search", "code_execution", "ask_user", "run_command"], config=config, context=context
    )
    assert exposed == ["run_command"]


def test_unknown_scene_is_rejected_loudly():
    with pytest.raises(ValueError):
        toolsets.FilterContext(scene="not-a-scene")


def test_explain_names_the_layer_that_blocked_it():
    """``explain()`` 必须说清是**哪一层**拦的、为什么——一句"不可见"等于没解释。"""
    text = toolsets.explain("clarify", toolsets.FilterContext(scene="cron"))
    assert "场景层" in text
    assert "排除" in text
    assert "cron" in text
    assert "不可见" in text
    # 另外三层也要各有一行（排查时最常问的正是"它们为什么不拦"）。
    for layer in ("平台层", "profile 层", "委托层"):
        assert layer in text


def test_explain_says_visible_when_nothing_blocks():
    text = toolsets.explain("run_command", toolsets.FilterContext())
    assert "可见" in text
    assert "结论：可见" in text


def test_explain_explains_the_delegated_layer():
    text = toolsets.explain("ask_user", toolsets.FilterContext(delegated=True))
    assert "委托层" in text
    assert "递归" in text or "绕过" in text


def test_explain_explains_the_profile_layer():
    config = _profile_config("review-only", disabled=["run_command"])
    text = toolsets.explain(
        "run_command", toolsets.FilterContext(profile="review-only"), config
    )
    assert "profile 层" in text
    assert "review-only" in text


def test_specs_honours_the_context(tmp_path):
    """``ToolRegistry.specs(context=...)`` 真的按四层收窄（不只是 ``resolve`` 会）。"""
    registry = _registry(tmp_path)
    names = [spec["function"]["name"] for spec in registry.specs()]
    assert "clarify" in names
    cron_names = [
        spec["function"]["name"]
        for spec in registry.specs(context=toolsets.FilterContext(scene="cron"))
    ]
    assert "clarify" not in cron_names
    assert "run_command" in cron_names


# ══ 4. 16 家钩子（4.1）══════════════════════════════════════════════════════


def test_all_sixteen_providers_have_a_hooks_entry():
    assert len(pv.PROVIDERS) == 16
    assert set(pv.PROVIDER_HOOKS) == set(pv.PROVIDERS)


def test_factory_profiles_keep_their_hook_fields_empty():
    """出厂 profile 的两个钩子字段仍是 None——这是既有回归闸，不能被覆盖表悄悄改掉。"""
    for key, profile in pv.PROVIDERS.items():
        assert profile.fetch_models is None, key
        assert profile.build_extra_body is None, key


def test_every_provider_fetch_models_raises_network_unavailable():
    """16 家的 ``fetch_models`` 一律抛 ``NetworkUnavailable``，**绝不返回空列表**。

    返回空列表会被上层读成"这家没有模型"，而真实原因是"本机没网"——静默错误。
    """
    from app.capabilities import NetworkUnavailable

    for key in pv.PROVIDERS:
        hook = pv.hooks_for(key).fetch_models
        assert hook is not None, f"{key} 缺 fetch_models 钩子"
        with pytest.raises(NetworkUnavailable) as excinfo:
            hook()
        message = str(excinfo.value)
        assert "没有网络" in message
        assert "空列表" in message  # 消息里必须点明"别把这事读成没有模型"


def test_every_missing_body_hook_has_a_reason():
    """``build_extra_body`` 留空的家必须写原因，否则"没做"会被读成"不需要"。"""
    for key in pv.PROVIDERS:
        hooks = pv.hooks_for(key)
        if hooks.build_extra_body is None:
            assert hooks.body_reason.strip(), f"{key} 的 build_extra_body 留空却没写原因"
        else:
            assert hooks.body_reason.strip(), f"{key} 填了钩子也要写清它改了什么"


def test_body_hooks_are_pure_local_rewrites():
    """钩子必须是纯本地改写：给定同一份 payload 得到同一份结果，且不改入参。"""
    payload = {"model": "m", "messages": [], "reasoning_effort": "high"}
    for key in pv.PROVIDERS:
        hook = pv.hooks_for(key).build_extra_body
        if hook is None:
            continue
        first = hook("some-model", dict(payload))
        second = hook("some-model", dict(payload))
        assert first == second, key
    assert payload["reasoning_effort"] == "high", "钩子不许改调用方传进来的 dict"


def test_anthropic_body_hook_translates_thinking_budget():
    hook = pv.hooks_for("anthropic").build_extra_body
    assert hook is not None
    body = hook("claude-sonnet-5", {"max_tokens": 8192, "reasoning_effort": "high"})
    assert body["thinking"]["type"] == "enabled"
    assert body["thinking"]["budget_tokens"] <= 4096  # 夹到 max_tokens 的一半
    assert "reasoning_effort" not in body


def test_gemini_body_hook_uses_thinking_config():
    hook = pv.hooks_for("gemini").build_extra_body
    assert hook is not None
    body = hook("gemini-3.5-flash", {"reasoning_effort": "low"})
    assert body["generationConfig"]["thinkingConfig"]["thinkingBudget"] == 1024


def test_openrouter_body_hook_uses_reasoning_object_and_transforms():
    hook = pv.hooks_for("openrouter").build_extra_body
    assert hook is not None
    body = hook("anthropic/claude-sonnet-4.6", {"reasoning_effort": "medium"})
    assert body["reasoning"] == {"effort": "medium"}
    assert body["transforms"] == ["middle-out"]


def test_providers_that_reject_reasoning_effort_have_it_stripped():
    """不认 ``reasoning_effort`` 的家必须删掉它（发了就是 422 / 400）。"""
    for key in ("mistral", "moonshot", "ollama"):
        hook = pv.hooks_for(key).build_extra_body
        assert hook is not None
        assert "reasoning_effort" not in hook("m", {"reasoning_effort": "high"})


def test_with_hooks_overlays_without_mutating_the_factory_table():
    """覆盖表是叠加：``PROVIDERS`` 保持出厂身份不变（两个进程不会看到两套表）。"""
    profile = pv.PROVIDERS["anthropic"]
    hooked = pv.with_hooks(profile)
    assert hooked.fetch_models is not None
    assert hooked.build_extra_body is not None
    assert profile.fetch_models is None
    assert profile.build_extra_body is None
    assert pv.PROVIDERS["anthropic"] is profile


def test_hook_rows_covers_all_providers():
    rows = pv.hook_rows()
    assert len(rows) == 16
    assert {row["provider"] for row in rows} == set(pv.PROVIDERS)
    for row in rows:
        assert row["fetch_models"] is True
