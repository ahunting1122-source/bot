"""`--auth-mode oauth` 真的从命令行走到 `ProxyServer`（MATRIX 4.7 的验收判据）。

## 为什么需要这条测试

`ProxyPlan.auth_mode` 与 `oauth_token_provider()` 早就写好了、各自的单测也绿，
但**命令行没有入口**——`proxy serve` 只注入 `headers=auth_headers(plan, api_key)`，
`token_provider` 永远是 `None`。

这是「库层有、运行时没接」的典型：**每一层单独看都对，合起来那条线是断的。**
13.8（dashboard 路由）就是这么假绿过一轮的，所以这里照同样的做法补两道：
一道行为（命令行真的把值传到 `ProxyServer`），一道源码（`main` 里真的传了）。
"""

from __future__ import annotations

from pathlib import Path

import pytest


def test_auth_mode_reaches_the_plan():
    """`--auth-mode oauth` 必须落到 `ProxyPlan.auth_mode` 上。

    只断言 `ProxyPlan` 有 `auth_mode` 字段是不够的（它一直有）——
    要断言**命令行给的值真的进去了**。
    """
    from app import proxy as px
    from app.cli.commands.model_extra2 import _proxy_plan_from_args

    class Args:
        provider = "anthropic"
        host = "127.0.0.1"
        port = 8645
        map = ""
        auth_mode = "oauth"

    plan = _proxy_plan_from_args(Args())
    assert plan.auth_mode == "oauth"
    assert isinstance(plan, px.ProxyPlan)


def test_auth_mode_defaults_to_api_key():
    """不给 `--auth-mode` 时行为**一点没变**（默认 api_key）。"""
    from app.cli.commands.model_extra2 import _proxy_plan_from_args

    class Args:
        provider = "anthropic"
        host = "127.0.0.1"
        port = 8645
        map = ""

    assert _proxy_plan_from_args(Args()).auth_mode == "api_key"


def test_unknown_auth_mode_is_refused():
    """非法值当场拒，并说清可选范围——不要静默退回 api_key。"""
    from app.cli.commands.model_extra2 import _proxy_plan_from_args
    from app.cli.context import CliError

    class Args:
        provider = "anthropic"
        host = "127.0.0.1"
        port = 8645
        map = ""
        auth_mode = "乱写"

    with pytest.raises(CliError, match="api_key / oauth"):
        _proxy_plan_from_args(Args())


def test_serve_passes_token_provider_to_the_server(monkeypatch, capsys):
    """**核心判据**：`run_proxy_serve` 必须把 `token_provider` 传给 `ProxyServer`。

    这条在接线前会失败（`token_provider` 恒为 `None`）——所以它真的能红。
    """
    from app.cli.commands import model_extra2

    captured: dict = {}

    class _FakeServer:
        def __init__(self, plan, **kwargs):
            captured["plan"] = plan
            captured.update(kwargs)

        def describe(self) -> str:
            return "fake"

        def serve(self) -> None:
            return None

    monkeypatch.setattr(model_extra2.px, "ProxyServer", _FakeServer)
    monkeypatch.setattr(model_extra2.px, "port_in_use", lambda host, port: False)
    monkeypatch.setattr(model_extra2.px, "oauth_token_provider", lambda *a, **k: "TOKEN_FN")

    class Args:
        provider = "anthropic"
        host = "127.0.0.1"
        port = 8645
        map = ""
        auth_mode = "oauth"
        foreground = True

    class Ctx:
        settings = None

        def out(self, *_a, **_k):
            return None

        def warn(self, *_a, **_k):
            return None

    # `run_proxy_serve` 会阻塞在 `server.serve()`；`_FakeServer.serve` 立刻返回。
    model_extra2.run_proxy_serve(Ctx(), Args())
    assert captured.get("token_provider") == "TOKEN_FN", (
        "`run_proxy_serve` 没有把 `token_provider` 传给 `ProxyServer`——"
        "`--auth-mode oauth` 只是被接受了，没生效"
    )


def test_source_actually_passes_token_provider():
    """源码层机械证据：行为测试可能因为构造 Args 的细节而失真，这条不会。"""
    from app.cli.commands import model_extra2

    source = Path(model_extra2.__file__).read_text(encoding="utf-8")
    assert "token_provider=token_provider" in source, (
        "`model_extra2.py` 里没有 `token_provider=token_provider`——"
        "OAuth 模式没有接到 `ProxyServer` 上"
    )
    assert "oauth_token_provider" in source
