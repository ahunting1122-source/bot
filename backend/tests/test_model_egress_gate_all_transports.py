"""闸门必须在 `build_chat_model()` 的**分叉点之前**（第 53 轮）。

## 为什么需要这条

上一轮我只在 `_build_openai_chat()` 里加了闸——**只堵了 `chat_completions` 一条路**。
`anthropic_messages`（`:1202`）与 `gemini`（`:1215`）各自 new 客户端，**都没有闸**，
而它们的 `base_url` 同样来自用户可改的配置、`api_key` 同样会发出去。

**同一件事在三条路径上各写一遍，必然漏一条——这次就漏了两条。**

## 判据

**共用的安全检查放在分叉点之前，不放在分支里面。**
这条测试用**源码顺序**断言它：闸门调用必须出现在第一个
`if profile.transport ==` **之前**。这样新增第四条 transport 时也自动被覆盖。
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from app import providers as providers_module


def _build_chat_model_body() -> ast.FunctionDef:
    """用 **AST** 取 `build_chat_model` 的函数体。

    ## 为什么不用 `source.find("\ndef ")`（上轮的写法）

    那个写法**遇到嵌套 `def` 就会提前截断**——闭包、内部辅助函数都算。
    截断之后「第一个 `if profile.transport ==`」可能落在外面，
    断言就会以「没找到分支」的方式失败，**或者（更糟）被改成 `>= 0` 之后静默通过**。

    **文本匹配判断代码结构本质上是在猜；AST 不受注释、字符串、缩进影响。**
    """
    source = pathlib.Path(providers_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "build_chat_model":
            return node
    raise AssertionError("在模块顶层找不到 build_chat_model")


def _first_line_of(node: ast.AST) -> int:
    return min(
        (child.lineno for child in ast.walk(node) if hasattr(child, "lineno")),
        default=getattr(node, "lineno", 0),
    )


def test_the_gate_runs_before_the_transport_branches():
    """**闸门在分叉点之前**——新增 transport 时自动被覆盖。

    用 AST 而不是文本匹配：取 `build_chat_model` 的**直接子节点**，
    找闸门调用与第一个 transport 分支的**行号**，断言前者更早。
    """
    body = _build_chat_model_body()

    gate_lines: list[int] = []
    branch_lines: list[int] = []
    for stmt in ast.walk(body):
        # 闸门调用
        if isinstance(stmt, ast.Call) and isinstance(stmt.func, ast.Name):
            if stmt.func.id == "_refuse_cloud_metadata_endpoint":
                gate_lines.append(stmt.lineno)
        # `if profile.transport ==` 这类比较
        if isinstance(stmt, ast.Compare):
            rendered = ast.dump(stmt)
            if "transport" in rendered:
                branch_lines.append(stmt.lineno)

    assert gate_lines, "`build_chat_model()` 里没有调用闸门——所有 transport 全裸奔"
    assert branch_lines, "找不到 transport 分支（测试失效了）"
    assert min(gate_lines) < min(branch_lines), (
        f"闸门在第 {min(gate_lines)} 行，而第一个 transport 分支在第 "
        f"{min(branch_lines)} 行——闸门被放在了分支里面或之后，"
        "那样只堵其中一条路，其余几条仍会把凭证发出去"
    )
@pytest.mark.parametrize(
    "transport",
    # **四条 transport 全测**：监视子agent 指出第 4 条 edrock_converse 我也漏了
    # （闸门在分叉点之前，理论上覆盖它——**但没测过就不算**）。
    ["chat_completions", "anthropic_messages", "gemini", "bedrock_converse"],
)
def test_every_transport_refuses_a_cloud_metadata_base_url(transport):
    """三条 transport **都必须**被拦。

    参数化是刻意的：**只测 `chat_completions` 的话，上一轮那个「只堵一条」的实现也能过。**

    profile 从**真实注册表**里派生（`dataclasses.replace`），而不是手搓一个——
    手搓要逐个填必填字段，填错一个就变成「测的是构造函数」而不是「测的是闸门」。
    """
    import dataclasses

    from app.providers import PROVIDERS, build_chat_model

    base = next(iter(PROVIDERS.values()))
    profile = dataclasses.replace(base, transport=transport)

    class _Settings:
        model_api_key = "sk-probe"
        model_base_url = "http://169.254.169.254/v1"
        model_provider = ""
        data_dir = pathlib.Path(".")

    with pytest.raises(Exception) as excinfo:
        build_chat_model(profile, _Settings(), "gpt-x")  # type: ignore[arg-type]
    message = str(excinfo.value)
    assert "云元数据" in message, f"{transport} 没被闸门拦下：{message}"
