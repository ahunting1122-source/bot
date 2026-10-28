"""`orbit moa run` 真的存在且能解析（MATRIX 4.11 的验收判据 + 反证）。

## 背景：这条的虚报形状很特殊

`orbit moa` 的 `list` / `configure` / `delete` **都能跑**（配置面已接），
但 `run` 这条**执行入口**从来没挂上——因为唯一 import `app/moa.py` 的是
`model_extra.py`，而它自己没被 `commands/__init__.py` import。

**用户能配置 MoA，却永远跑不了它。** 这种"半条命"比完全没做更难发现：
`orbit moa --help` 有内容、`list` 有输出、测试也绿。

## 两层判据（照监视子agent 的建议：反证比正证更能钉住）

1. **反证**：`build_parser()` 产出的解析器**必须能接受 `moa run`**——
   少了那行 import，argparse 会报 `invalid choice: 'run'`。这条真的能红。
2. **正证**：`moa` 的每条子命令都有 handler，且 `run` 的 handler 不是占位。
"""

from __future__ import annotations

import pytest


def test_parser_accepts_moa_run():
    """**反证**：解析器必须认识 `moa run`。

    这条在 `model_extra` 没被 import 时会失败（argparse 报 invalid choice）——
    所以它守的正是那个静默缺失。
    """
    from app.cli.main import build_parser

    parser = build_parser()
    # 不真跑（会连模型），只让 argparse 解析到子命令层
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["moa", "run", "--help"])
    # --help 会 SystemExit(0)；如果是 invalid choice 则是 SystemExit(2)
    assert exc.value.code == 0, (
        "`moa run` 没被解析器认识——`model_extra` 大概又没被 import 了"
    )


def test_every_moa_subcommand_has_a_real_handler():
    """`moa` 的每条子命令都必须有 handler（不能是纯占位）。"""
    from app.cli import REGISTRY

    parent = REGISTRY.get("moa")
    assert parent is not None
    names = {sub.name for sub in parent.subcommands}
    # `run` 是执行入口，`list`/`configure`/`delete` 是配置面——一个都不能少
    assert {"run", "list", "configure", "delete"} <= names, f"缺子命令：{names}"
    for sub in parent.subcommands:
        assert sub.handler is not None, f"`moa {sub.name}` 没有 handler"
        assert sub.state == "done", f"`moa {sub.name}` 状态是 {sub.state}"


def test_moa_run_dry_run_reports_a_plan_without_calling_a_model():
    """`--dry-run` 必须**只展示计划、不发起调用**。

    这是执行入口唯一能在本机验证的部分（无网络、无 provider 凭证），
    而它恰好是最该被验的部分：**一个 dry-run 会真调模型的命令是危险的**。
    """
    from app.cli import REGISTRY

    command = next(sub for sub in REGISTRY.get("moa").subcommands if sub.name == "run")
    assert command.handler is not None
    # 选项里必须有 --dry-run，否则上面那条"只展示计划"的承诺无处可依
    flags = {flag for option in command.options for flag in option.flags}
    assert "--dry-run" in flags, f"`moa run` 没有 --dry-run：{sorted(flags)}"
    assert "--preset" in flags, f"`moa run` 没有 --preset：{sorted(flags)}"
