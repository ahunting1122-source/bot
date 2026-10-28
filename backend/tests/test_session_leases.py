"""存活租约（``app/session_leases.py``）的契约测试。

钉住五件事，每件都对应一个"看起来能用、实际会坏"的失败模式：

1. **acquire / heartbeat / release 往返**：登记簿是"我还在用这个会话"的唯一凭据，
   三个动作少一个，别人就没法判断该不该接管。
2. **心跳过期的被清掉**：进程活着但卡死（心跳停了）是最常见的一种失效，只靠 pid 判活
   会让一个卡住的持有者永久占着会话。
3. **PID 复用不算存活**：只比 pid 会把"别人的进程"当成自己的持有者——这是
   ``app/orphan_reaper.py`` 模块 docstring 里点名要防的事故。
4. **不 kill 任何进程**：判据基于"读进程创建时间"这种尽力而为的探测，在不可靠判据上
   执行不可逆动作是不可接受的。所以这里既扫源码（不许出现 ``os.kill`` / ``taskkill``），
   也在运行时把 ``os.kill`` 换成"一调就炸"，证明整条路径碰不到它。
5. **租约被别的 pid 持有时不许偷偷续/释放**：那等于替别人做决定。
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

import app.cli.commands.session_extra  # noqa: F401 —— 导入即接线（sessions leases/reap）
import app.session_leases as leases_module
from app.cli.main import main
from app.config import Settings
from app.main import migrate
from app.session_leases import (
    KIND_ALIVE,
    KIND_DEAD,
    KIND_REUSED,
    LeaseError,
    LeaseStore,
)


def _store(tmp_path, **kwargs) -> LeaseStore:
    """默认探针换成"可控时钟 + 可注入进程身份"，避免测试依赖真实进程表。"""
    clock = kwargs.pop("clock", None)
    return LeaseStore(tmp_path / "data", clock=clock, **kwargs)


# ── 往返 ────────────────────────────────────────────────────────────────────


def test_acquire_heartbeat_release_roundtrip(tmp_path):
    store = _store(tmp_path)
    lease = store.acquire("c1", platform="cli")
    assert lease.session_id == "c1"
    assert lease.pid == os.getpid()
    assert lease.platform == "cli"
    assert lease.host_start_time is not None, "必须记下创建时刻，否则 PID 复用后无法识别"

    # 落盘的是 {session_id: {...}} 这个形状（见模块 docstring）
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    assert set(raw) == {"c1"}
    assert raw["c1"]["pid"] == os.getpid()

    before = store.get("c1")
    assert before is not None
    again = store.heartbeat("c1")
    assert again.heartbeat_at >= before.heartbeat_at

    assert store.release("c1") is True
    assert store.get("c1") is None
    assert store.load() == {}
    assert store.release("c1") is False, "重复释放是无操作，不报错"


def test_heartbeat_without_a_lease_raises(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(LeaseError):
        store.heartbeat("c1")


def test_heartbeat_by_another_pid_is_refused(tmp_path):
    """租约被别人持有时不许续——偷偷续会让两个进程都以为自己独占。"""
    store = _store(tmp_path)
    store.acquire("c1", pid=4242, start_time=100.0)
    with pytest.raises(LeaseError):
        store.heartbeat("c1")
    with pytest.raises(LeaseError):
        store.release("c1")


def test_acquire_twice_overwrites_the_holder(tmp_path):
    """后写者持有（不排队、不阻塞）：本模块没有"强制夺回"的手段，见模块 docstring。"""
    store = _store(tmp_path)
    store.acquire("c1", pid=4242, start_time=100.0)
    store.acquire("c1", pid=os.getpid(), start_time=200.0)
    lease = store.get("c1")
    assert lease is not None
    assert lease.pid == os.getpid()


def test_empty_session_id_is_rejected(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(LeaseError):
        store.acquire("   ")


# ── 判活 ────────────────────────────────────────────────────────────────────


def test_expired_heartbeat_is_not_live_and_gets_reaped(tmp_path):
    """心跳停了 200 秒（> 默认 90 秒）→ 不算存活，``reap_stale`` 清掉。"""
    now = [1000.0]
    store = _store(tmp_path, clock=lambda: now[0], alive=lambda pid: True, start_time=lambda pid: 42.0)
    store.acquire("c1", pid=4242, start_time=42.0)

    now[0] += 200.0
    verdicts = store.verdicts()
    assert verdicts[0].kind == KIND_ALIVE, "进程还在"
    assert verdicts[0].expired is True, "但心跳过期了"
    assert verdicts[0].live is False
    assert store.live() == []

    report = store.reap_stale()
    assert report.dry_run is True
    assert report.removed == 0
    assert store.load() != {}, "dry-run 不许动登记簿"

    report = store.reap_stale(dry_run=False)
    assert report.removed == 1
    assert store.load() == {}


def test_fresh_heartbeat_survives_the_reap(tmp_path):
    now = [1000.0]
    store = _store(tmp_path, clock=lambda: now[0], alive=lambda pid: True, start_time=lambda pid: 42.0)
    store.acquire("c1", pid=4242, start_time=42.0)
    now[0] += 10.0
    store.heartbeat("c1", pid=4242)
    now[0] += 10.0

    assert [lease.session_id for lease in store.live()] == ["c1"]
    report = store.reap_stale(dry_run=False)
    assert report.removed == 0
    assert "c1" in store.load()


def test_dead_process_is_not_live(tmp_path):
    store = _store(tmp_path, alive=lambda pid: False, start_time=lambda pid: 42.0)
    store.acquire("c1", pid=4242, start_time=42.0)
    verdict = store.verdicts()[0]
    assert verdict.kind == KIND_DEAD
    assert verdict.live is False
    assert store.reap_stale(dry_run=False).removed == 1


def test_pid_reuse_is_not_liveness(tmp_path):
    """pid 活着但**创建时间对不上** → PID 被复用了，不是我们的持有者。"""
    store = _store(
        tmp_path,
        alive=lambda pid: True,
        start_time=lambda pid: 999.0,  # 实际创建时刻与登记时的 42.0 差了 957 秒
    )
    store.acquire("c1", pid=4242, start_time=42.0)

    verdict = store.verdicts()[0]
    assert verdict.kind == KIND_REUSED
    assert verdict.live is False, "PID 复用不算存活"
    assert store.live() == []
    assert store.reap_stale(dry_run=False).removed == 1


def test_unknown_start_time_falls_back_to_pid_liveness(tmp_path):
    """拿不到创建时刻时退回 pid 判活：把"探测不到"当成"已死"会误清活着的登记。"""
    store = _store(tmp_path, alive=lambda pid: True, start_time=lambda pid: None)
    store.acquire("c1", pid=4242, start_time=42.0)
    assert store.verdicts()[0].kind == KIND_ALIVE


# ── 绝不 kill ───────────────────────────────────────────────────────────────


def test_source_contains_no_kill_calls():
    """源码里不许出现任何**杀进程的能力**：既没有杀进程的调用，也没有能杀进程的模块。

    查的是 AST 里的调用与 import，不是裸文本：模块 docstring 必须能写
    "本模块不 ``os.kill``、不 ``taskkill``"这种话——按文本 grep 会让解释性文字把
    测试判成失败，那样后来的人只会把注释删掉，而不是把调用删掉。
    """
    source = Path(leases_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    banned_calls = {"kill", "killpg", "terminate", "send_signal", "system", "popen", "call"}
    calls: list[str] = []
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            calls.append(_dotted(node.func))
        elif isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    offenders = [name for name in calls if name.split(".")[-1] in banned_calls]
    assert offenders == [], f"session_leases.py 里出现了杀进程的调用：{offenders}"
    assert not imports & {"subprocess", "ctypes", "signal"}, (
        f"session_leases.py 不该 import 这些模块（它们能杀进程）：{sorted(imports)}"
    )


def _dotted(node: ast.expr) -> str:
    """把 ``os.kill`` 这类调用目标还原成点号串（``Name`` / ``Attribute`` 两种）。"""
    parts: list[str] = []
    current: ast.expr | None = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def test_operations_never_touch_process_signals(tmp_path, monkeypatch):
    """把 ``os.kill`` 换成"一调就炸"，跑完一整圈仍然不炸 → 证明真的没杀进程。"""
    now = [1000.0]

    def boom(*args, **kwargs):
        raise AssertionError("session_leases 不许向任何进程发信号")

    monkeypatch.setattr(os, "kill", boom)
    store = _store(
        tmp_path, clock=lambda: now[0], alive=lambda pid: True, start_time=lambda pid: 42.0
    )
    store.acquire("c1", pid=4242, start_time=42.0)
    now[0] += 5.0
    store.heartbeat("c1", pid=4242)
    assert [lease.session_id for lease in store.live()] == ["c1"]
    now[0] += 500.0  # 心跳过期
    report = store.reap_stale(dry_run=False)
    assert report.removed == 1
    assert store.load() == {}


# ── 形状与兼容 ──────────────────────────────────────────────────────────────


def test_reads_hermes_style_entries_list(tmp_path):
    """Hermes 的 ``runtime/active_sessions.json`` 是 ``{"entries": [...]}``，也要能读。"""
    store = _store(tmp_path, alive=lambda pid: True, start_time=lambda pid: 42.0)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "lease_id": "354c45c6",
                        "session_id": "20261028_103113_011746",
                        "pid": 6524,
                        "process_start_time": 42.0,
                        "surface": "desktop",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    loaded = store.load()
    assert list(loaded) == ["20261028_103113_011746"]
    assert loaded["20261028_103113_011746"]["pid"] == 6524


def test_corrupt_file_reads_as_empty_instead_of_crashing(tmp_path):
    store = _store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{这不是 JSON", encoding="utf-8")
    assert store.load() == {}
    assert store.verdicts() == []


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_leases_lists_registrations(tmp_path, capsys):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    # 用**本进程**登记：CLI 那边走真实探针，pid 必须是真活着的那一个，
    # 否则这条用例只是在测"一个假 pid 被判成 dead"。
    LeaseStore(settings.data_dir).acquire("c1", platform="desktop")

    assert main(["--data-dir", str(settings.data_dir), "sessions", "leases"]) == 0
    out = capsys.readouterr().out
    assert "存活租约 1 / 登记 1" in out
    assert str(os.getpid()) in out
    assert "desktop" in out


def test_cli_reap_is_dry_run_by_default(tmp_path, capsys):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = LeaseStore(settings.data_dir, alive=lambda pid: False, start_time=lambda pid: 42.0)
    store.acquire("c1", pid=4242, start_time=42.0)

    assert main(["--data-dir", str(settings.data_dir), "sessions", "reap"]) == 0
    out = capsys.readouterr().out
    assert "只报告" in out
    assert "未改动任何文件" in out
    assert "c1" in store.load(), "dry-run 不许动登记簿"

    assert main(["--data-dir", str(settings.data_dir), "sessions", "reap", "--apply"]) == 0
    assert "已清 1 条登记" in capsys.readouterr().out
    assert store.load() == {}


def test_cli_reap_rejects_conflicting_flags(tmp_path, capsys):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    code = main(
        ["--data-dir", str(settings.data_dir), "sessions", "reap", "--dry-run", "--apply"]
    )
    assert code == 1
    assert "互斥" in capsys.readouterr().err
