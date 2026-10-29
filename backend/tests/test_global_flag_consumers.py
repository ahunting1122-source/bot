"""`1.7`：全局 flag 里又查出两个「写了没人读」的装饰 flag（已修）。

## 怎么查出来的

`1.7` 的证据栏写着「其余 28 个确实真生效」——**那是一条第 17 轮的人工断言**，
而同一栏里已经承认「至少两个是装饰」。所以本轮用 AST 机械核了一遍
`_apply_flags` 写进 `updates` 的 9 个键，看它们在 `app/`（**排除 `app/cli/`**）里
有没有读取点：

```
ignore_rules         readers=1
max_turns            readers=1
model                readers=11
model_base_url       readers=5
model_provider       readers=6
reasoning_level      readers=1
safe_mode            readers=1
run_budget_seconds   readers=0   ← 装饰
session_source       readers=0   ← 装饰
```

## 两个都是「接上了但没生效」那一族

- **`--run-budget`** 写 `updates["run_budget_seconds"]`，而 `Settings` **没有这个字段**
  → pydantic **静默丢掉**未声明的 kwarg → 产品读的是 `settings.max_seconds`
  （`policies.py` 拿它做时间预算闸门）。**flag 一个字节都没生效。**
- **`--source`** 写 `updates["session_source"]`，全仓**零读取点**。

**这一族最难发现**：flag 能解析、不报错、也没有任何提示。
"""

from __future__ import annotations

import argparse

import pytest


def _context(tmp_path):
    from app.cli.context import Context
    from app.config import Settings

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    return Context(settings=settings, json_output=False, quiet=True)


def _args(**overrides):
    base = {
        "run_budget": None,
        "source": None,
        "pass_session_id": False,
        "accept_hooks": False,
        "max_turns": None,
        "reasoning": None,
        "safe_mode": False,
        "ignore_rules": False,
        "toolsets": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


def test_run_budget_writes_the_field_the_product_actually_reads(tmp_path):
    """**核心判据**：`--run-budget` 必须写进 `Settings.max_seconds`。

    原来写的是 `run_budget_seconds`——`Settings` 里没这个字段，pydantic 静默丢掉，
    **flag 完全没生效**。
    """
    from app.cli.commands.chat import _apply_flags

    context = _context(tmp_path)
    _apply_flags(context, _args(run_budget="120"))

    # **断言在 settings 上**：`_apply_flags` 返回的是 `state`，`updates` 已经
    # 通过 `model_copy` 落进 `context.settings` 了
    # （第一版断言在返回的 dict 上，那是 `state`，所以是错的）。
    assert context.settings.max_seconds == 120, (
        f"`--run-budget 120` 没落到 `max_seconds`（产品真正读的那个）："
        f"{context.settings.max_seconds}"
    )


def test_source_reaches_the_platform_mechanism(tmp_path):
    """**判据**：`--source` 必须真的影响行为，不能只是写一个没人读的键。

    接到 6.4 建的 `enqueue(..., platform=...)` 上——它决定工具暴露面。
    """
    from app.cli.commands.chat import _apply_flags

    context = _context(tmp_path)
    updates = _apply_flags(context, _args(source="slack"))

    assert "session_source" not in updates, (
        "又写回了那个零读取点的键——flag 等于没写"
    )
    assert getattr(context, "session_source", "") == "slack", (
        "`--source` 没挂到 context 上，`_submit()` 就拿不到它"
    )


def test_source_is_not_mistaken_for_the_platform():
    """**接错概念的回归测试**（MATRIX 1.7 第 42 轮）。

    Hermes 的 --source 是「**会话来源标签，用于过滤**」（调研 01:181）；
    而 6.4 的 platform 决定的是**工具暴露面**。**两个概念。**

    上一版把 --source 接到 platform 上：第三方传 --source tool 期望隐藏会话，
    实际只是把 platform 设成 	ool（没配置 → 工具集不变）→
    **净效果仍是 0，而 --help 在说谎。**

    这条钉两件事：① _submit() **不能**把 source 当 platform；
    ② 它**必须**把 source 记进任务事件（真实、可观测的效果）。
    """
    import pathlib

    from app.cli.commands import chat as chat_module

    raw = pathlib.Path(chat_module.__file__).read_text(encoding="utf-8")
    code = "\\n".join(
        line for line in raw.splitlines() if not line.strip().startswith("#")
    )
    assert 'platform=getattr(context, "session_source"' not in code, (
        "又把 --source 当平台了——那是工具暴露面，不是会话来源标签"
    )
    assert 'source=source' in code, (
        "--source 没有记进任务事件——那它又变成零效果的装饰 flag"
    )


def test_no_global_flag_is_written_without_a_reader():
    """**性质断言**：`_apply_flags` 写进 `updates` 的每个键，都必须有非 CLI 读取点。

    这条不是清单断言——它遍历**当前实际写的那批键**，所以新增 flag 时自动生效。
    """
    import ast
    import pathlib

    from app.cli.commands import chat as chat_module

    app = pathlib.Path(chat_module.__file__).resolve().parent.parent
    raw = pathlib.Path(chat_module.__file__).read_text(encoding="utf-8")
    # **先去掉注释**：我的注释里就写着 updates["run_budget_seconds"]（说明历史），
    # 不过滤会把注释当成真写入。这个测试自己踩过。
    source = "\n".join(
        line for line in raw.splitlines() if not line.strip().startswith("#")
    )
    written = set(
        __import__("re").findall(r'updates\["([a-z_]+)"\]', source)
    )
    assert written, "一条都没解析出来——测试失效了"

    wiring = {"cli/context.py", "cli/commands/chat.py"}
    read: set[str] = set()
    for path in app.rglob("*.py"):
        if path.relative_to(app).as_posix() in wiring:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            name = None
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in {"settings", "cfg"}
            ):
                name = node.attr
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                name = node.args[1].value
            elif (
                isinstance(node, ast.Subscript)
                and isinstance(node.slice, ast.Constant)
                and isinstance(node.slice.value, str)
            ):
                name = node.slice.value
            if name:
                read.add(name)

    orphans = sorted(written - read)
    assert not orphans, (
        f"这些 flag 写进去了但 app/（非 CLI）里零读取点——装饰 flag：{orphans}"
    )


@pytest.mark.parametrize("key", ["max_seconds"])
def test_the_repaired_field_exists_on_settings(key):
    """**前置条件**：写进去的字段必须真的在 `Settings` 上。

    pydantic 对未声明的 kwarg **静默丢弃**——这就是 `run_budget_seconds`
    一个字节都没生效的原因。
    """
    from app.config import Settings

    assert key in Settings.model_fields, f"`Settings` 没有 `{key}`，写进去会被静默丢掉"
