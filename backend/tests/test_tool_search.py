"""`tool_search` 元工具的契约测试（Hermes 的 `tool_search`，MATRIX 5.11）。

为什么需要它：工具一多，把全部 schema 塞进每次请求既贵又稀释注意力。有了检索，
模型可以「先找再调」。Orbit 现在 28 个工具还塞得下，所以这一步是**为规模准备**的
——接口先立起来，以后涨到几十个时不用改模型的行为约定。

三条硬要求：
1. 只搜**已暴露**的工具。搜到却调不了，比搜不到更糟。
2. 名字命中要排在描述命中前面。模型搜 `browser` 时想要的是 `browser_*`，
   不是某个说明里碰巧提到 browser 的工具。
3. 空查询返回空列表，不是全量。空查询返回全量等于把「分级披露」又变回「全塞」。
"""

from __future__ import annotations

import pytest

import app.tools as tools_module
from app.browser import BROWSER_TOOLS
from app.config import Settings
from app.main import migrate
from app.storage import Store
from app.tools import LOCAL_TOOLS, ToolRegistry
from app.web import WEB_TOOLS


@pytest.fixture()
async def registry(tmp_path, monkeypatch):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    instance = ToolRegistry(settings, store)

    async def noop(*args, **kwargs):
        return {}

    instance.register(BROWSER_TOOLS, noop)
    instance.register(WEB_TOOLS, noop)
    yield instance
    await store.engine.dispose()


def test_searches_by_tool_name(registry):
    names = [hit["name"] for hit in registry.search_tools("browser")]
    assert "browser_navigate" in names
    # 名字命中优先：第一条必须是 browser_* 而不是描述里提到 browser 的别的工具。
    assert names[0].startswith("browser_")


def test_searches_by_chinese_description(registry):
    """Orbit 的工具说明是中文的，按中文关键词要能搜到。"""
    names = [hit["name"] for hit in registry.search_tools("文件")]
    assert names, "中文描述搜索没有命中"


def test_searches_by_toolset_name(registry):
    names = [hit["name"] for hit in registry.search_tools("web")]
    assert "web_search" in names


def test_name_hits_rank_above_description_hits(registry):
    hits = registry.search_tools("search")
    names = [hit["name"] for hit in hits]
    # `local_search_text` / `history_search_tasks` / `retriever_search` 名字里都有 search
    assert "local_search_text" in names


def test_limit_is_respected(registry):
    assert len(registry.search_tools("a", limit=2)) <= 2
    assert len(registry.search_tools("e", limit=1)) == 1


def test_empty_query_returns_nothing(registry):
    """空查询返回全量 = 把「分级披露」又变回「全塞」，所以必须是空列表。"""
    assert registry.search_tools("") == []
    assert registry.search_tools("   ") == []


def test_unknown_keyword_returns_nothing(registry):
    assert registry.search_tools("这个词一定不存在zzz") == []


def test_only_exposed_tools_are_searchable(registry, monkeypatch):
    """被 toolset 关掉的工具不该出现在检索结果里——搜到却调不了最误导人。"""
    real_resolve = tools_module.toolsets.resolve

    def hide_browser(names, platform, config):
        return [name for name in real_resolve(names, platform, config)
                if not name.startswith("browser_")]

    monkeypatch.setattr(tools_module.toolsets, "resolve", hide_browser)
    names = [hit["name"] for hit in registry.search_tools("browser")]
    assert names == []


def test_hits_carry_actionable_fields(registry):
    hit = registry.search_tools("web_search", limit=1)[0]
    assert hit["name"] == "web_search"
    assert hit["description"]
    assert hit["toolset"] == "web"
    assert isinstance(hit["parameters"], list)


def test_tool_search_is_itself_a_tool(registry):
    """元工具也要能被搜到，否则「不知道该用哪个工具」的模型永远找不到它。"""
    assert "tool_search" in [hit["name"] for hit in registry.search_tools("tool_search")]


async def test_execute_returns_ok_and_hits(registry):
    result = await registry.execute(
        "task-1", "op-1", "tool_search", {"query": "browser", "limit": 3}
    )
    assert result["status"] == "ok"
    assert len(result["data"]) == 3
    assert all(hit["name"].startswith("browser_") for hit in result["data"])


def test_tool_search_is_classified_into_a_toolset():
    """未归类的工具会让 `tests/test_toolsets.py` 的「全部归类」断言失败。"""
    from app.toolsets import UNCLASSIFIED, toolset_of

    assert toolset_of("tool_search") != UNCLASSIFIED


def test_local_tools_still_all_have_valid_schemas():
    from app.tools import schema_problems

    broken = {
        tool.specification()["function"]["name"]: schema_problems(tool.specification())
        for tool in LOCAL_TOOLS
    }
    assert {name: issue for name, issue in broken.items() if issue} == {}
