"""MCP prompts → 技能：形状转换、命名、渲染、以及"缓存 + 显式刷新"的桥接策略。

这一组用例盯的是 `app/mcp_prompts.py` 的三件硬承诺：

1. `list_prompts` 的结果能变成 Orbit 的技能形状（`name` / `description` / `body`）；
2. 名字是 ``mcp__<server>__<prompt>``（与 `app/mcp.py` 的工具命名同构）；
3. **同步读路径一次网络都不打**——桥接靠缓存，不靠在同步代码里跑事件循环。

第 3 条是这一层唯一真正危险的地方（在同步路径里 `asyncio.run()` 会炸掉后端与 CLI 的
常驻循环），所以它有两条用例：一条数调用次数，一条**在正在运行的循环里**调同步读。
"""

from __future__ import annotations

import asyncio

import pytest

from app.mcp_prompts import (
    PromptRegistry,
    PromptSkill,
    parse_prompt_skill_name,
    prompt_skill_name,
    prompt_text,
    render_prompt,
    skills_from_listing,
    summarize,
)


class FakeClient:
    """最小可用的 MCP 客户端替身：只实现本模块真正会调的两个方法。

    返回值刻意用**普通 dict**而不是伪造 pydantic 模型：本模块的 ``_shape`` 同时认
    两种形状，测试走 dict 这一支就能顺带证明那层兼容是有效的。
    """

    def __init__(self, prompts, bodies=None, *, fail_bodies: bool = False) -> None:
        self._prompts = list(prompts)
        self._bodies = dict(bodies or {})
        self._fail = fail_bodies
        self.calls: list[tuple] = []

    async def list_prompts(self, cursor=None):
        self.calls.append(("list_prompts", cursor))
        return {"prompts": list(self._prompts), "next_cursor": None}

    async def get_prompt(self, name, arguments=None):
        self.calls.append(("get_prompt", name, arguments))
        if self._fail:
            raise RuntimeError("prompt 服务炸了")
        text = self._bodies.get(name, f"正文：{name}")
        if arguments:
            text = f"{text} " + " ".join(f"{key}={value}" for key, value in arguments.items())
        return {"messages": [{"role": "user", "content": {"type": "text", "text": text}}]}

    def method_calls(self, method: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == method]


def listing() -> list[dict]:
    """三个 prompt：正常的、只有 title 的、名字为空的（要被跳过）。"""
    return [
        {
            "name": "summarize",
            "description": "把文档压成三条要点",
            "arguments": [{"name": "topic", "required": True, "description": "主题"}],
        },
        {"name": "review", "title": "代码评审", "arguments": []},
        {"name": "", "description": "空名字的条目必须被跳过"},
    ]


# ── 形状与命名 ──────────────────────────────────────────────────────────────


def test_listing_becomes_orbit_skill_shape():
    """`list_prompts` → 技能形状；列表阶段**不取正文**（正文由 refresh 预取）。"""
    skills = skills_from_listing("rich", listing())
    assert [skill.name for skill in skills] == ["mcp__rich__summarize", "mcp__rich__review"]
    assert skills[0].description == "把文档压成三条要点"
    assert skills[0].body == ""
    assert skills[0].arguments == (
        {"name": "topic", "description": "主题", "required": True},
    )
    assert skills[0].as_skill() == {
        "name": "mcp__rich__summarize",
        "description": "把文档压成三条要点",
        "body": "",
    }


def test_description_falls_back_to_title_and_then_to_the_prompt_name():
    """三处都没有 description 时用 prompt 名兜底，**不拿正文首行去凑**。"""
    skills = skills_from_listing("s", [{"name": "review", "title": "代码评审"}, {"name": "bare"}])
    assert skills[0].description == "代码评审"
    assert skills[1].description == "bare"


def test_names_are_mcp_prefixed_ascii_and_bounded():
    assert prompt_skill_name("filesystem", "read_file") == "mcp__filesystem__read_file"
    exotic = prompt_skill_name("我的 服务/v2", "读 文件")
    assert exotic.startswith("mcp__")
    assert exotic.isascii()
    assert len(prompt_skill_name("x" * 80, "y" * 80)) <= 64


def test_skill_name_round_trips_back_to_server_and_prompt():
    assert parse_prompt_skill_name("mcp__rich__summarize") == ("rich", "summarize")
    # prompt 名里本来就可能带下划线：只切前两个分隔符，别从右边切。
    assert parse_prompt_skill_name("mcp__srv__a__b") == ("srv", "a__b")
    assert parse_prompt_skill_name("local_skill") is None
    assert parse_prompt_skill_name("mcp__broken") is None


def test_prompt_text_joins_text_blocks_and_ignores_non_text():
    result = {
        "messages": [
            {"content": {"type": "text", "text": "第一段"}},
            {"content": "裸字符串内容"},
            {"content": {"type": "image", "data": "…"}},
        ]
    }
    assert prompt_text(result) == "第一段\n裸字符串内容"


# ── 刷新 / 渲染 ─────────────────────────────────────────────────────────────


async def test_refresh_prefetches_bodies_into_the_skill():
    """刷新会顺手把每条 prompt 的正文取回来——技能目录是同步读的，正文必须先在异步侧备好。"""
    client = FakeClient(listing(), {"summarize": "请总结「主题」"})
    registry = PromptRegistry()
    skills = await registry.refresh("rich", client)

    assert [skill.name for skill in skills] == ["mcp__rich__summarize", "mcp__rich__review"]
    assert "请总结" in skills[0].body
    assert registry.body("mcp__rich__summarize") == skills[0].body
    assert len(client.method_calls("get_prompt")) == 2  # 两条 prompt 各取一次


async def test_render_prompt_passes_arguments_through():
    client = FakeClient(listing(), {"summarize": "总结「主题」"})
    text = await render_prompt(client, "rich", "summarize", {"topic": "机械键盘"})
    assert "topic=机械键盘" in text
    assert client.calls[-1] == ("get_prompt", "summarize", {"topic": "机械键盘"})


async def test_registry_render_accepts_the_skill_name_and_reuses_the_cached_client():
    """`skill_load` 手里只有技能名（`mcp__…`），没有客户端——registry 必须自己认得。"""
    client = FakeClient(listing(), {"summarize": "总结「主题」"})
    registry = PromptRegistry()
    await registry.refresh("rich", client)
    text = await registry.render_prompt("rich", "mcp__rich__summarize", {"topic": "x"})
    assert "topic=x" in text
    assert client.calls[-1] == ("get_prompt", "summarize", {"topic": "x"})


async def test_render_without_a_connection_raises_instead_of_returning_empty():
    """没连上就渲染不出来——报错，不要返回空正文（空正文会被当成"模板是空的"）。"""
    registry = PromptRegistry()
    with pytest.raises(LookupError):
        await registry.render_prompt("nope", "anything")


async def test_a_broken_prompt_keeps_its_description_only_placeholder():
    """取正文失败只让那一条缺正文；技能仍以"仅描述"出现，不静默消失。"""
    client = FakeClient(listing(), fail_bodies=True)
    registry = PromptRegistry()
    skills = await registry.refresh("rich", client)
    assert len(skills) == 2
    assert all(skill.body == "" for skill in skills)
    assert all(skill.description for skill in skills)


# ── 桥接策略：缓存命中不重复请求 ─────────────────────────────────────────────


async def test_sync_reads_never_hit_the_network():
    client = FakeClient(listing())
    registry = PromptRegistry()
    await registry.refresh("rich", client)
    before = len(client.calls)

    for _ in range(5):
        assert len(registry.skills()) == 2
        assert len(registry.records()) == 2
        assert registry.get("mcp__rich__summarize") is not None
        assert registry.for_server("rich")

    assert len(client.calls) == before, "同步读路径不该产生任何请求"
    # 显式刷新才会再打一次——这是唯一的刷新入口。
    await registry.refresh("rich", client)
    assert len(client.calls) == before + 1 + 2  # 1 次 list_prompts + 2 次 get_prompt


async def test_sync_read_is_safe_inside_a_running_event_loop():
    """这条用例是桥接策略的**硬证明**。

    如果 `skills()` 偷偷用 `asyncio.run()` 去拉正文，在"已经有一个循环在跑"的线程里
    它会直接抛 ``RuntimeError``——本用例就是那个探针。
    """
    client = FakeClient(listing())
    registry = PromptRegistry()
    await registry.refresh("rich", client)
    assert asyncio.get_running_loop()  # 确认当前确实有循环在跑
    skills = registry.skills()
    assert [skill["name"] for skill in skills] == ["mcp__rich__summarize", "mcp__rich__review"]


# ── 失效 ────────────────────────────────────────────────────────────────────


async def test_invalidate_drops_only_the_named_server():
    """服务掉线时它的 prompt 必须跟着消失，别人的不许受牵连。"""
    registry = PromptRegistry()
    await registry.refresh("a", FakeClient(listing()))
    await registry.refresh("b", FakeClient(listing()))
    assert registry.servers() == ["a", "b"]

    registry.invalidate("a")
    assert registry.servers() == ["b"]
    assert registry.get("mcp__a__summarize") is None
    assert registry.get("mcp__b__summarize") is not None

    registry.invalidate()
    assert registry.skills() == []


async def test_is_stale_only_when_a_ttl_is_given():
    registry = PromptRegistry()
    assert registry.is_stale("rich", 0) is False  # ttl<=0 = 只靠显式刷新
    assert registry.is_stale("rich", 60) is True  # 从没刷新过 = 陈旧
    await registry.refresh("rich", FakeClient(listing()))
    assert registry.is_stale("rich", 60) is False


# ── 汇总 ────────────────────────────────────────────────────────────────────


async def test_summarize_counts_skills_with_and_without_body():
    registry = PromptRegistry()
    await registry.refresh("rich", FakeClient(listing()))
    stats = summarize(registry)
    assert stats.servers == ["rich"]
    assert stats.skills == 2
    assert stats.with_body == 2
    assert stats.without_body == 0
    assert stats.as_dict()["skills"] == 2


def test_prompt_skill_is_hashable_and_immutable():
    """`frozen=True` 不是装饰：缓存里的条目被就地改掉会让"刷新过没有"变得不可判定。"""
    skill = PromptSkill(
        name="mcp__s__p", description="d", body="b", server="s", prompt="p"
    )
    assert skill.as_skill()["body"] == "b"
    assert {skill}  # 可放进集合（frozen dataclass 的用处之一）
