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

import pathlib

import pytest

from app import providers as providers_module


def _build_chat_model_source() -> str:
    source = pathlib.Path(providers_module.__file__).read_text(encoding="utf-8")
    start = source.find("def build_chat_model(")
    assert start >= 0, "找不到 build_chat_model"
    # 到下一个顶层 def 为止
    end = source.find("\ndef ", start + 10)
    return source[start : end if end > 0 else len(source)]


def test_the_gate_runs_before_the_transport_branches():
    """**闸门在分叉点之前**——新增 transport 时自动被覆盖。"""
    body = _build_chat_model_source()

    gate = body.find("_refuse_cloud_metadata_endpoint(")
    assert gate >= 0, "`build_chat_model()` 里没有调用闸门——三条 transport 全裸奔"

    first_branch = body.find("if profile.transport ==")
    assert first_branch >= 0, "找不到 transport 分叉（测试失效了）"
    assert gate < first_branch, (
        "闸门被放在了 transport 分支里面或之后——"
        "那样只堵其中一条路，其余几条仍然会把凭证发出去"
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
