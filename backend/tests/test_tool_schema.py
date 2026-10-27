"""工具 schema 前置校验的契约测试（`app/tools.py::schema_problems`）。

为什么值得单独一个文件：畸形 schema 的代价不对称——本地一秒能查出来的问题，
放到线上就是**整次请求 400**，而供应商的错误信息通常只说"第 N 个函数的参数非法"，
定位成本极高。所以校验要在**暴露之前**做，而且要有用例锁住"它真的会拦"。

另一个容易被忽略的点：Orbit 发出去的是 OpenAI 函数调用形状
（`{"type": "function", "function": {...}}`），校验必须能拆开那层壳，
否则每个工具都会被判成"缺 name"——这个坑本轮真踩过一次。
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.main import migrate
from app.storage import Store
from app.tools import LOCAL_TOOLS, ToolRegistry, schema_problems


def _wrapped(**body) -> dict:
    """OpenAI 函数调用形状。"""
    return {"type": "function", "function": body}


# ── 内置工具必须全部合法 ───────────────────────────────────────────────────


def test_every_builtin_tool_has_a_valid_schema():
    """回归：任何一个内置工具 schema 变畸形，这里先炸，而不是等供应商 400。"""
    problems = {
        tool.specification()["function"]["name"]: schema_problems(tool.specification())
        for tool in LOCAL_TOOLS
    }
    broken = {name: issue for name, issue in problems.items() if issue}
    assert broken == {}


def test_builtin_tool_names_are_unique():
    names = [tool.specification()["function"]["name"] for tool in LOCAL_TOOLS]
    assert len(names) == len(set(names))


# ── 校验本身 ───────────────────────────────────────────────────────────────


def test_wrapped_and_bare_shapes_are_both_accepted():
    body = {
        "name": "x",
        "description": "说明",
        "parameters": {"type": "object", "properties": {"a": {"type": "string"}}},
    }
    assert schema_problems(_wrapped(**body)) == []
    assert schema_problems(body) == []


def test_parameters_type_must_be_object():
    problems = schema_problems(_wrapped(name="x", description="d", parameters={"type": "array"}))
    assert any("object" in issue for issue in problems)


def test_required_must_reference_existing_properties():
    problems = schema_problems(
        _wrapped(
            name="x",
            description="d",
            parameters={"type": "object", "properties": {"a": {}}, "required": ["b"]},
        )
    )
    assert any("required" in issue and "b" in issue for issue in problems)


def test_missing_name_and_description_are_reported():
    problems = schema_problems(_wrapped(parameters={"type": "object", "properties": {}}))
    assert any("name" in issue for issue in problems)
    assert any("description" in issue for issue in problems)


def test_properties_must_be_a_dict():
    problems = schema_problems(
        _wrapped(name="x", description="d", parameters={"type": "object", "properties": []})
    )
    assert any("properties" in issue for issue in problems)


def test_tool_without_parameters_is_allowed():
    """无参工具可以省略 parameters——调用方按空对象处理。"""
    assert schema_problems(_wrapped(name="x", description="d")) == []


def test_non_dict_spec_is_reported():
    assert schema_problems("nope")  # type: ignore[arg-type]


# ── 暴露面：坏工具被排除，好工具留下 ───────────────────────────────────────


@pytest.fixture()
async def registry(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    yield ToolRegistry(settings, store)
    await store.engine.dispose()


async def test_specs_drops_the_broken_tool_and_keeps_the_rest(registry):
    class BrokenTool:
        def specification(self):
            return _wrapped(
                name="broken_tool",
                description="故意的畸形工具",
                parameters={"type": "array", "properties": {}},
            )

    registry.tools["broken_tool"] = BrokenTool()  # type: ignore[assignment]
    names = [spec["function"]["name"] for spec in registry.specs()]
    assert "broken_tool" not in names
    # 其它工具一个都不能少：排除一个坏工具是为了救整轮请求，不是顺手删一堆。
    assert "run_command" in names
    assert len(names) == len(LOCAL_TOOLS)
