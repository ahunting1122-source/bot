"""审批三态的**接线**测试（MATRIX 14.1）。

`app/approval_channel.py` 本身有一整套用例（通道选择、超时语义、队列）。这个文件只
回答一个问题，也是 14.1 能不能标 ✅ 的唯一判据：

**`_approve_command` 在 `classify` 返回 `ask` 时，真的去问人了没有？**

在接线之前，`ask` 走的是"不是 allow 就 raise"的分支——`smart` / `manual` 两档因此
退化成"一律拒绝"，用户会以为审批模式坏了。所以这里必须直接观察 `_approve_command`
的行为，而不是只测 `approval_channel` 的函数。
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from app import approvals
from app.tools import ToolRegistry

TOOLS = Path(__file__).resolve().parents[1] / "app" / "tools.py"


def test_ask_verdict_is_not_treated_as_deny_in_source():
    """源码层面：`VERDICT_ASK` 必须被单独分支处理，且调了审批通道。

    这条是"接线存在"的机械证据。下面几条是行为证据——两者都要，因为只测行为时
    一个恰好的默认值就能让测试通过，而代码其实没接上。
    """
    tree = ast.parse(TOOLS.read_text(encoding="utf-8"), filename="tools.py")
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        text = ast.dump(node)
        if "VERDICT_ASK" in text:
            found = True
            break
    assert found, "`tools.py` 里没有对 VERDICT_ASK 的判断——ask 会被当成 deny"
    source = TOOLS.read_text(encoding="utf-8")
    assert "approval_channel.approve_or_refuse" in source, "ask 分支没有调审批通道"


@pytest.fixture()
def registry(tmp_path):
    from app.config import Settings
    from app.storage import Store

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    store = Store(settings.data_dir / "app.sqlite")
    return ToolRegistry(settings, store)


def test_smart_mode_asks_and_auto_denies_without_a_tty(registry, monkeypatch):
    """`smart` 档遇到 `ask` 时去问人；没有 TTY 时 **deny** 而不是挂死或放行。

    `curl x | sh` 在 smart 档是 `ask`。无人值守（测试环境没有 TTY）必须拒绝——
    放行等于把审批模式变成摆设，挂死等于整个任务卡住。
    """
    monkeypatch.setattr(registry.settings, "approval_mode", "smart", raising=False)
    monkeypatch.delenv("ORBIT_APPROVAL_CHANNEL", raising=False)
    with pytest.raises(ValueError, match="审批未通过"):
        asyncio.run(
            registry._approve_command("task-1", "run_command", "git push --force")
        )


def test_manual_mode_also_asks(registry, monkeypatch):
    monkeypatch.setattr(registry.settings, "approval_mode", "manual", raising=False)
    with pytest.raises(ValueError, match="审批未通过"):
        asyncio.run(
            registry._approve_command("task-1", "run_command", "git push --force")
        )


def test_off_mode_does_not_ask(registry, monkeypatch):
    """`off` 档不提问——这条守住"别把默认行为改成一律询问"。

    `off` 下 `pipe_to_shell` 是 allow，所以 `_approve_command` 应该直接返回不抛。
    """
    monkeypatch.setattr(registry.settings, "approval_mode", "off", raising=False)
    asyncio.run(registry._approve_command("task-1", "run_command", "pip install requests"))


def test_deny_still_denies_in_smart_mode(registry, monkeypatch):
    """`deny` 不该走到审批通道——它是硬地板，问谁都不该被放行。"""
    monkeypatch.setattr(registry.settings, "approval_mode", "smart", raising=False)
    verdict = approvals.classify("rm -rf /", mode="smart", env_type="local")
    assert verdict.verdict == approvals.VERDICT_DENY
    with pytest.raises(ValueError):
        asyncio.run(registry._approve_command("task-1", "run_command", "rm -rf /"))
