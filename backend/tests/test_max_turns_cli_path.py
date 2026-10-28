"""`orbit chat --max-turns N` 真的把值送到 budget（MATRIX 2.2 的 CLI 侧判据）。

## 为什么这条必须单独存在

上轮我验的是 `Settings(max_turns=3)` → `budget.start()` 这一段，**那只是后半截**。
真实路径还有前半截：

    `orbit chat --max-turns 3`
      → `chat._apply_flags()` 把值放进 `updates`
      → `context.settings = settings.model_copy(update=updates)`
      → `policies.budget.start(settings)` → `turn_budgets.resolve()`

**任何一环断了，值都到不了 budget，而后面每一环单独看都是对的。**
上轮那个「`Settings` 里根本没有这个字段」的静默丢弃就藏在这条链上——
只验后半截永远发现不了。
"""

from __future__ import annotations

from app.config import Settings


class _Args:
    """模拟 `orbit chat --max-turns 3` 解析出来的 args（其余 flag 全关）。"""

    max_turns = "3"
    run_budget = None
    skills = None
    image = None
    safe_mode = False
    ignore_user_config = False
    ignore_rules = False
    model = None
    provider = None
    reasoning = None
    toolsets = None
    pass_session_id = False
    source = None
    accept_hooks = False
    in_dir = None
    worktree = False
    checkpoints = False
    yolo = False
    tui = False
    dev = False
    quiet = False
    verbose = False


class _Ctx:
    """最小的 Context 替身：只需要 settings 与几个输出方法。"""

    def __init__(self, settings):
        self.settings = settings

    def note(self, *_a, **_k):
        return None

    def warn(self, *_a, **_k):
        return None

    def out(self, *_a, **_k):
        return None


def test_cli_max_turns_reaches_the_budget(tmp_path):
    """`--max-turns 3` 必须一路走到 `budget["max_turns"] == 3`。

    这条在链上任何一环断掉时都会失败——包括「`Settings` 没这个字段」
    那种**静默丢弃**（pydantic 对未声明 kwarg 不报错，只是丢掉）。
    """
    from app.cli.commands import chat as chat_module
    from app.policies import DefaultBudgetPolicy

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    ctx = _Ctx(settings)

    state = chat_module._apply_flags(ctx, _Args())

    # ① settings 上真的有这个值（不是被 pydantic 静默丢了）
    assert getattr(ctx.settings, "max_turns", None) == 3, (
        "`--max-turns` 没进 settings——pydantic 对未声明的 kwarg 是静默丢弃的"
    )
    # ② state 里也有（给诊断用）
    assert state.get("max_turns") == 3
    # ③ 最终 budget 里有，且是 CLI 给的那个值（不是默认 90）
    budget = DefaultBudgetPolicy().start(ctx.settings)
    assert budget["max_turns"] == 3, f"budget 拿到的是 {budget['max_turns']}，不是 3"


def test_default_max_turns_is_the_documented_90(tmp_path):
    """不给 `--max-turns` 时，budget 拿到默认档 90（`turn_budgets` 的父预算）。"""
    from app.policies import DefaultBudgetPolicy

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    budget = DefaultBudgetPolicy().start(settings)
    assert budget["max_turns"] == 90
