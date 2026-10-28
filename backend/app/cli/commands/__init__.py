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
    approvals,
    chat,
    core,
    cron,
    curator,
    delegate,
    gateway,
    mcp,
    model,
    observability,
    ops,
    plugins,
    security,
    session_plane,
    sessions,
    setup,
    skills,
    tools,
    ui,
)

from app.cli.commands import hermes_parity  # noqa: F401

__all__ = [
    "approvals",
    "chat",
    "core",
    "cron",
    "curator",
    "delegate",
    "gateway",
    "mcp",
    "model",
    "observability",
    "ops",
    "plugins",
    "security",
    "session_plane",
    "sessions",
    "setup",
    "skills",
    "tools",
    "ui",
    "hermes_parity",
]
