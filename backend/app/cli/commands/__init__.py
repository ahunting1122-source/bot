"""命令模块集合。导入即注册——``main`` 在编译 parser 之前导入本包。

**导入顺序是有语义的，不要按字母序重排**：

1. 先导入**有真实现**的功能域模块（`core`、`model`、`web` …），它们各自 ``REGISTRY.add``。
2. **最后**导入声明表 ``hermes_parity``：它的 ``register_specs`` 会跳过已登记的名字。

于是"某条命令从声明表迁移到真实现"只需要做一件事——在下面把它排到
``hermes_parity`` **之前**，声明表里那条就自动失效。不需要手改表，
也不会出现"两处都登记 → 重复注册报错"。

``# isort: split`` 是刻意加的：没有它，lint 会按字母序把 ``model`` 排到
``hermes_parity`` 后面，迁移规则当场失效。
"""

from app.cli.commands import (  # isort: split
    acp,
    approvals,
    chat,
    core,
    cron,
    curator,
    delegate,
    gateway,
    hooks,
    mcp,
    mcp_extra,
    memory_extra,
    memory_extra2,
    model,
    # `model_extra` 给 `model` 加开关、给 `moa` 加 `run` 子命令（MATRIX 4.11）。
    # **必须在 `model` 之后导入**（它要往已存在的父节点上挂子命令），
    # 但它**不自 import `hermes_parity`**，所以留在第一组。
    # 少了这一行：`orbit moa` 的 `list`/`configure`/`delete` 都能跑，
    # **唯独 `run` 不存在**——用户能配置 MoA 却永远跑不了它。
    model_extra,
    observability,
    observability_extra,
    ops,
    platform_extra,
    plugins,
    plugins_extra,
    security,
    security_extra,
    session_plane,
    sessions,
    setup,
    skill_extra,
    skills,
    tools,
    tools_extra,
    tools_extra2,
    tools_extra3,
    tools_extra4,
    ui,
    ui_extra,
)

# ── 第二组：**必须在表组装之后**导入的模块 ─────────────────────────────────
#
# 判据：这些模块自己 `import hermes_parity`（要把子命令挂到表里已存在的父节点上，
# 需要表先组装好）。如果把它们放在上面那组里，它们会在 `setup.py` **之前**触发
# `register_specs`，于是表里的顶层 `setup` 先登记、随后 `setup.py` 再登记一次 →
# `命令重复登记：setup`，整个 CLI 起不来。
#
# 所以规则要精确表述：**替换顶层命令的模块必须排在 `hermes_parity` 之前；
# 只往已有父节点挂子命令的模块可以（也必须）排在之后。**
from app.cli.commands import (
    cron_extra,  # noqa: E402,F401
    hermes_parity,  # noqa: F401
    model_extra2,  # noqa: E402,F401
    peer_extra,  # noqa: E402,F401
    session_extra,  # noqa: E402,F401
    session_extra2,  # noqa: E402,F401
    session_extra3,  # noqa: E402,F401
    skill_extra2,  # noqa: E402,F401
    skill_extra3,  # noqa: E402,F401
)

# 表组装完之后要做的状态校正：`tools_extra2` 接管的 iron-proxy 生命周期命令
# （`egress start/stop/...`）只是"如实报告 Orbit 没有这一层"，功能本身没实现。
# 但 `Spec.build()` 见到有 handler 就标 `done`，于是"如实说没有"被算成"有"。
# 这里在表组装完成后按事实改回 `partial`——放在 `hermes_parity` 之后是必须的，
# 因为表在那之前还不存在。
from app.cli.commands.tools_extra2 import mark_proxy_paths_partial  # noqa: E402

mark_proxy_paths_partial()

# 同类的第二处：`memory_extra2` 把 `profile show/export/import` 从 Hermes 的
# **profile distribution** 语义接管成**用户画像**语义，连选项形状一起换
# （`show` 不再要 `PROFILE_NAME`）。但它在 `hermes_parity` 之前导入，那时表还没有，
# 节点更新被跳过 → `orbit profile show` 会报 "the following arguments are required:
# PROFILE_NAME"。所以表组装完之后重放一遍。
from app.cli.commands.memory_extra2 import rebind_profile_options  # noqa: E402

rebind_profile_options()


# ── 表组装完之后的第三处修正：重算**纯分组节点**的状态 ─────────────────────
#
# `_table.Spec.build()` 的聚合逻辑本身是对的（子全 done → 父 done），但它**跑得太早**：
# `skill_extra3` / `session_extra3` / `peer_extra` 这些模块是在表组装**之后**才把
# handler 挂到已有子节点上的（它们自己包了 `register_specs`）。所以：
#
#   组装时子节点是 todo → 父节点算成 todo/partial → 之后子节点变 done，
#   **父节点却停在旧状态上**。
#
# 实测有 6 个父节点中招（`profile` 24 个子全 done、`sessions` 27 个全 done、
# `cron` 16 个全 done，父节点却都报 `partial`）——**完成度被系统性低估**。
# 这与 `Spec.build` 里那段「`skin` 就是这么被低估的」注释是同一个病，只是
# 那次是"一律标 todo"，这次是"算完之后才挂载"。
def recompute_group_states() -> list[str]:
    """按子节点状态重算纯分组节点的状态，返回被改动的节点名。

    **只动"自己没有 handler 但有子命令"的节点**——有 handler 的节点它的状态是
    自己挣来的（`claimed → done`），不能被子节点覆盖。
    """
    from app.cli.spec import REGISTRY as _REG

    changed: list[str] = []

    def walk(command) -> None:
        for sub in command.subcommands:
            walk(sub)
        if not command.subcommands or command.handler is not None:
            return
        states = {sub.state for sub in command.subcommands}
        if states == {"done"}:
            want = "done"
        elif states & {"done", "partial"}:
            want = "partial"
        else:
            want = "todo"
        if want != command.state:
            changed.append(f"{command.name}: {command.state} → {want}")
            command.state = want

    for top in _REG.all():
        walk(top)
    return changed


recompute_group_states()

__all__ = [
    "acp",
    "approvals",
    "chat",
    "core",
    "cron",
    "curator",
    "delegate",
    "gateway",
    "hooks",
    "mcp",
    "mcp_extra",
    "memory_extra",
    "memory_extra2",
    "model",
    "model_extra",
    "observability",
    "observability_extra",
    "observability_extra",
    "ops",
    "platform_extra",
    "plugins",
    "plugins_extra",
    "peer_extra",
    "security",
    "security_extra",
    "session_extra",
    "session_extra2",
    "session_extra3",
    "session_plane",
    "sessions",
    "setup",
    "skill_extra",
    "skill_extra2",
    "skill_extra3",
    "skills",
    "tools",
    "tools_extra",
    "tools_extra2",
    "tools_extra3",
    "tools_extra4",
    "ui",
    "ui_extra",
    "hermes_parity",
    "cron_extra",
    "model_extra2",
]
