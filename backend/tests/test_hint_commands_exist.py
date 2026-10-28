"""提示文案里提到的命令必须真的存在（`_stop_hint` 的守卫）。

## 为什么需要这条

`budget_stopped` 的 `hint` 会告诉用户「要放宽就用 `orbit chat --max-turns N`」这种话。
**如果指向的命令不存在，用户会去敲一个 404** —— 那比不给提示更糟：
提示的价值全在「照着做能解决」，指错了就等于把人引到沟里。

我在写 10.8 的提示时写了「`orbit plane leases` 看看谁持有」——**那条子命令不存在**。
自己核出来才发现。所以把它做成机制，而不是靠记性。

判据：把 `_stop_hint` 里出现的每个 `orbit <cmd>` 都解析出来，逐个查注册表。
**子命令级也要查**（`orbit plane leases` 这种），只查顶层会漏掉这一类错。
"""

from __future__ import annotations

import pathlib
import re

#: `orbit chat` / `orbit plane show` / `orbit mcp resources` …
_ORBIT_CMD = re.compile(r"`orbit ([a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*){0,2})")


def _resolve(path: str):
    """按空格逐级解析命令路径；任一级不存在就返回 None。"""
    from app.cli import REGISTRY

    parts = path.split()
    command = REGISTRY.get(parts[0])
    if command is None:
        return None
    for part in parts[1:]:
        # 选项（`--max-turns`）不是子命令，跳过
        if part.startswith("-"):
            continue
        found = next((sub for sub in command.subcommands if sub.name == part), None)
        if found is None:
            return None
        command = found
    return command


def test_every_command_named_in_stop_hints_exists():
    """`_stop_hint` 里提到的每个命令路径都必须存在。

    这条红了 = 提示在指引用户去敲一个不存在的命令。
    """
    from app.graph import nodes as nodes_module

    source = pathlib.Path(nodes_module.__file__).read_text(encoding="utf-8")
    start = source.find("def _stop_hint")
    assert start != -1, "找不到 `_stop_hint`——测试失效了，先确认代码结构"
    body = source[start : start + 2400]

    mentioned = sorted({m.group(1) for m in _ORBIT_CMD.finditer(body)})
    assert mentioned, "`_stop_hint` 里没解析出任何 `orbit <cmd>`——正则或文案结构变了"

    missing = [path for path in mentioned if _resolve(path) is None]
    assert not missing, (
        f"提示里指向了不存在的命令：{missing}——用户照着敲会报错，"
        "提示的价值全在「照着做能解决」"
    )


def test_hint_covers_every_stop_reason_the_producers_declare():
    """**性质断言**：产出方声明的每个 `stop_reason`，`_stop_hint` 都要有话说。

    **为什么不用硬编码元组**：`reasons = ("max_actions", ...)` 是**清单断言**——
    将来 `policies.check()` 新增一种原因，那条测试照样绿（它只遍历写死的 4 个）。
    **清单会过期，性质不会。**

    所以原因由**产出方自己声明**：
    - `DefaultBudgetPolicy.STOP_REASONS`（策略能产出的）
    - `nodes.LEASE_STOP_REASON`（租约机制能产出的）

    测试遍历两者的**并集**——新增原因自动要求写提示。
    """
    from app.config import Settings
    from app.graph import nodes as nodes_module
    from app.policies import DefaultBudgetPolicy

    policy = DefaultBudgetPolicy()
    budget = policy.start(Settings(data_dir=pathlib.Path("."), workspace=pathlib.Path(".")))

    declared = tuple(DefaultBudgetPolicy.STOP_REASONS) + (nodes_module.LEASE_STOP_REASON,)
    assert len(declared) >= 4, f"产出方声明的原因太少（{declared}）——是不是漏声明了？"

    for reason in declared:
        hint = nodes_module._stop_hint(reason, budget, 3)
        assert hint.strip(), f"`{reason}` 没有提示——用户不知道该怎么调"


def test_stop_reasons_are_actually_produced_by_the_policy():
    """反证：声明的每个原因，`check()` 都要**真的能产出**它。

    防的是「声明了一份，代码产出另一份」——两边各自都对、合起来永远对不上。
    """
    from app.config import Settings
    from app.policies import DefaultBudgetPolicy

    policy = DefaultBudgetPolicy()
    budget = policy.start(Settings(data_dir=pathlib.Path("."), workspace=pathlib.Path(".")))
    produced: set[str] = set()

    # max_actions：动作数打满
    produced.add(policy.check({"actions": 10**6, "turns": 0, "budget": budget}))
    # max_turns：轮次打满
    produced.add(policy.check({"actions": 0, "turns": 10**6, "budget": budget}))
    # max_tokens：token 估算打满
    full = dict(budget)
    full["token_estimate"] = int(full["max_tokens"]) + 1
    produced.add(policy.check({"actions": 0, "turns": 0, "budget": full}))

    produced.discard("")
    missing = set(DefaultBudgetPolicy.STOP_REASONS) - produced
    assert not missing, (
        f"这些原因声明了但 `check()` 产不出来：{missing}——"
        "声明与实现漂移，提示会为一种不存在的情况写文案"
    )
