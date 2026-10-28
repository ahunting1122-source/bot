"""Shell hook 的契约测试（MATRIX 14.10）。

两条最要紧的规矩，各自都值得一组用例：

1. **未授权不执行**。一条 shell hook 就是任意代码执行，而且会长期存在、每次事件都跑。
   授权必须是一次显式动作（`orbit shellhooks trust`），不能靠"加进来就跑"。
2. **执行失败绝不影响主流程**。hook 是旁路观测，超时/崩溃/非零退出都只记日志。
   让用户写的脚本把 agent 卡死，是把旁路变成了关键路径。
"""

from __future__ import annotations

import sys

import pytest

from app.hooks import HOOK_EVENTS
from app.shell_hooks import (
    DEFAULT_TIMEOUT,
    MAX_TIMEOUT,
    ShellHookError,
    ShellHookStore,
    doctor,
    run,
    run_event,
)


@pytest.fixture()
def store(tmp_path):
    return ShellHookStore(tmp_path / "data")


# ── 声明管理 ───────────────────────────────────────────────────────────────


def test_add_and_list_round_trip(store):
    hook = store.add("on_session_end", "echo done", note="归档")
    assert hook.event == "on_session_end"
    assert hook.timeout == DEFAULT_TIMEOUT
    assert [item.id for item in store.list()] == [hook.id]


def test_new_hook_is_never_trusted(store):
    """默认不授权——这是整个模块的安全基石。"""
    hook = store.add("on_session_end", "echo done")
    assert hook.trusted is False


def test_unknown_event_is_rejected(store):
    with pytest.raises(ShellHookError, match="未知事件"):
        store.add("on_nothing_happens", "echo x")


def test_empty_command_is_rejected(store):
    with pytest.raises(ShellHookError, match="不能为空"):
        store.add("on_session_end", "   ")


def test_timeout_is_clamped_to_the_cap(store):
    hook = store.add("on_session_end", "echo x", timeout=9999)
    assert hook.timeout == MAX_TIMEOUT


def test_trust_enable_disable_and_remove(store):
    hook = store.add("on_session_end", "echo x")
    assert store.trust(hook.id) is True
    assert store.get(hook.id).trusted is True
    assert store.set_enabled(hook.id, False) is True
    assert store.get(hook.id).enabled is False
    assert store.remove(hook.id) is True
    assert store.list() == []


def test_trust_unknown_id_is_false(store):
    assert store.trust("nope") is False
    assert store.remove("nope") is False


def test_revoke_by_command_clears_every_match(store):
    """按命令原文撤销，且**全部**撤销——只撤一条会让人以为撤干净了。"""
    store.add("on_session_end", "echo same")
    store.add("on_session_end", "echo same")
    for hook in store.list():
        store.trust(hook.id)
    assert store.revoke_by_command("echo same") == 2
    assert all(not hook.trusted for hook in store.list())
    # 声明还在，只是不再执行。
    assert len(store.list()) == 2


def test_revoke_by_command_is_a_noop_when_nothing_matches(store):
    store.add("on_session_end", "echo x")
    assert store.revoke_by_command("echo other") == 0


def test_store_survives_a_corrupt_file(tmp_path):
    path = tmp_path / "data" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text("{ this is not json", encoding="utf-8")
    assert ShellHookStore(tmp_path / "data").list() == []


# ── 执行 ───────────────────────────────────────────────────────────────────


def test_untrusted_hook_does_not_run(store):
    hook = store.add("on_session_end", "echo SHOULD_NOT_RUN")
    outcome = run(hook, {"a": 1})
    assert outcome.ran is False
    assert outcome.ok is False
    assert "未授权" in outcome.reason
    assert outcome.stdout == ""


def test_disabled_hook_does_not_run(store):
    hook = store.add("on_session_end", "echo x")
    store.trust(hook.id)
    store.set_enabled(hook.id, False)
    outcome = run(store.get(hook.id))
    assert outcome.ran is False
    assert "停用" in outcome.reason


def test_trusted_hook_runs_and_captures_output(store):
    hook = store.add("on_session_end", "echo hello-hook")
    store.trust(hook.id)
    outcome = run(store.get(hook.id), {"k": "v"})
    assert outcome.ran is True
    assert outcome.ok is True
    assert outcome.exit_code == 0
    assert "hello-hook" in outcome.stdout


def test_payload_is_passed_via_environment(store):
    """载荷走环境变量而不是命令行参数：命令行会被 ps 看到。"""
    command = f'"{sys.executable}" -c "import os;print(os.environ.get(\'ORBIT_HOOK_PAYLOAD\',\'\'))"'
    hook = store.add("on_session_end", command)
    store.trust(hook.id)
    outcome = run(store.get(hook.id), {"secret": "s3cr3t"})
    assert "s3cr3t" in outcome.stdout


def test_failure_does_not_raise(store):
    hook = store.add("on_session_end", f'"{sys.executable}" -c "raise SystemExit(7)"')
    store.trust(hook.id)
    outcome = run(store.get(hook.id))
    assert outcome.ran is True
    assert outcome.ok is False
    assert outcome.exit_code == 7
    assert "退出码 7" in outcome.reason


def test_timeout_is_enforced_without_raising(store):
    command = f'"{sys.executable}" -c "import time;time.sleep(10)"'
    hook = store.add("on_session_end", command, timeout=1)
    store.trust(hook.id)
    outcome = run(store.get(hook.id))
    assert outcome.ran is True
    assert outcome.ok is False
    assert "超时" in outcome.reason


def test_run_event_only_fires_matching_hooks(store):
    first = store.add("on_session_end", "echo first")
    second = store.add("pre_tool_call", "echo second")
    for hook in (first, second):
        store.trust(hook.id)
    outcomes = run_event(store, "on_session_end", {})
    assert [item.hook_id for item in outcomes] == [first.id]


def test_one_failing_hook_does_not_stop_the_others(store):
    bad = store.add("on_session_end", f'"{sys.executable}" -c "raise SystemExit(3)"')
    good = store.add("on_session_end", "echo fine")
    for hook in (bad, good):
        store.trust(hook.id)
    outcomes = run_event(store, "on_session_end", {})
    assert len(outcomes) == 2
    assert outcomes[0].ok is False
    assert outcomes[1].ok is True


# ── 体检 ───────────────────────────────────────────────────────────────────


def test_doctor_flags_an_untrusted_hook_as_a_warning(store):
    store.add("on_session_end", "echo x")
    reports = doctor(store)
    assert len(reports) == 1
    assert reports[0].ok is True  # 未授权是 warning，不是硬问题
    assert any("未授权" in item for item in reports[0].warnings)


def test_doctor_flags_a_missing_interpreter_as_a_warning(store):
    store.add("on_session_end", "definitely-not-a-real-binary-xyz --flag")
    report = doctor(store)[0]
    assert report.ok is True
    assert any("找不到可执行文件" in item for item in report.warnings)


def test_doctor_on_an_empty_store(store):
    assert doctor(store) == []


def test_events_catalogue_is_non_empty():
    """`orbit shellhooks events` 要有东西可列——没人记得住 17 个事件名。"""
    assert len(HOOK_EVENTS) >= 15
    assert "on_session_end" in HOOK_EVENTS
    assert "pre_tool_call" in HOOK_EVENTS
