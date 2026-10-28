"""孤儿回收（``app/orphan_reaper.py``）的契约测试。

这一层最容易出**危险**的假绿：判据写错时程序不报错，只是把活进程当孤儿（或反过来）。
所以这里钉四条：

1. **死进程被识别**（``processes.json`` / ``spawn-ledger.json`` / ``dispatcher.lock`` 三处）；
2. **PID 复用不算孤儿**：``(pid, host_start_time)`` 二元组对不上时，那条登记是"身份不符"
   而不是"孤儿"——只看 pid 的实现会把别人的进程算成孤儿，而 Windows 上
   ``os.kill(pid, 0)`` 甚至会直接把对方杀掉；
3. **``dry_run=True`` 不动账本**（默认就该是只报告）；
4. **绝不 kill**：源码里不许出现 taskkill / kill / terminate 这类动作（用 AST 钉住）。

"死 pid"用"起一个空进程、等它退出、拿它的 pid"造出来（比随手写 ``999999`` 可靠：
那个数字可能真的被系统分配给了别的进程）。
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

from app import gateway as gw
from app import orphan_reaper as reaper_module
from app.orphan_reaper import (
    KIND_DEAD,
    KIND_REUSED,
    SOURCE_LOCK,
    SOURCE_PROCESSES,
    SOURCE_SPAWN_LEDGER,
    OrphanReaper,
)


def _dead_pid() -> int:
    """造一个**确定已退出**的 pid。"""
    process = subprocess.Popen(  # noqa: S603 - 测试里只跑 sys.executable 的空脚本
        [sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    process.wait(timeout=30)
    return process.pid


def _reaper(tmp_path) -> OrphanReaper:
    return OrphanReaper(tmp_path / "data")


def _paths(tmp_path) -> gw.GatewayPaths:
    return gw.GatewayPaths(tmp_path / "data")


def _register_process(tmp_path, record: dict) -> None:
    paths = _paths(tmp_path)
    paths.ensure()
    gw.ProcessRegistry(paths.processes).register(record)


def _register_spawn(tmp_path, record: dict) -> None:
    paths = _paths(tmp_path)
    paths.ensure()
    gw.SpawnLedger(paths.spawn_ledger).save([record])


# ── 死进程被识别 ────────────────────────────────────────────────────────────


def test_dead_process_in_processes_json_is_an_orphan(tmp_path):
    pid = _dead_pid()
    _register_process(tmp_path, {"pid": pid, "host_start_time": 1.0, "purpose": "gateway"})

    orphans = _reaper(tmp_path).find_orphans()
    assert [item.pid for item in orphans] == [pid]
    assert orphans[0].kind == KIND_DEAD
    assert orphans[0].source == SOURCE_PROCESSES
    assert "已不在" in orphans[0].reason


def test_dead_process_in_spawn_ledger_is_an_orphan(tmp_path):
    """spawn-ledger 的时间字段叫 ``create_time``（不是 ``host_start_time``）。"""
    pid = _dead_pid()
    _register_spawn(tmp_path, {"pid": pid, "create_time": 1.0, "purpose": "子进程"})

    orphans = _reaper(tmp_path).find_orphans()
    assert [(item.pid, item.source) for item in orphans] == [(pid, SOURCE_SPAWN_LEDGER)]


def test_live_process_is_not_an_orphan(tmp_path):
    """本进程登记了自己：活着且创建时间对得上 → 什么都不报。"""
    pid, start = gw.identity()
    _register_process(tmp_path, {"pid": pid, "host_start_time": start})

    reaper = _reaper(tmp_path)
    assert reaper.find_orphans() == []
    assert reaper.find_stale() == []


def test_missing_files_yield_nothing(tmp_path):
    """三个登记簿都不存在时返回空，不抛异常（第一次启动就是这个状态）。"""
    reaper = _reaper(tmp_path)
    assert reaper.find_orphans() == []
    assert reaper.find_stale() == []
    report = reaper.reap(dry_run=False)
    assert report.removed == 0
    assert report.lock_removed is False


# ── PID 复用 ────────────────────────────────────────────────────────────────


def test_pid_reuse_is_not_an_orphan(tmp_path):
    """pid 活着但创建时间对不上：**不算孤儿**，只算"身份不符"的无效登记。

    探针可注入，所以不必真去等 PID 被系统复用（那既慢又不确定）。
    """
    _register_process(tmp_path, {"pid": 4321, "host_start_time": 1_000.0})
    reaper = OrphanReaper(
        tmp_path / "data",
        alive=lambda pid: True,
        start_time=lambda pid: 9_999.0,  # 同一个 pid，但创建时间完全不同
    )

    assert reaper.find_orphans() == [], "PID 复用不是孤儿"
    stale = reaper.find_stale()
    assert [item.pid for item in stale] == [4321]
    assert stale[0].kind == KIND_REUSED
    assert "复用" in stale[0].reason


def test_start_time_within_tolerance_is_alive(tmp_path):
    """创建时间差 1 秒以内算同一个进程（与 ``ProcessRegistry._alive`` 同容差）。"""
    _register_process(tmp_path, {"pid": 4321, "host_start_time": 1_000.0})
    reaper = OrphanReaper(
        tmp_path / "data", alive=lambda pid: True, start_time=lambda pid: 1_000.9
    )
    assert reaper.find_orphans() == []
    assert reaper.find_stale() == []


def test_unreadable_start_time_does_not_report_an_orphan(tmp_path):
    """探测不到创建时间时退回 pid 判活——**不能**把"探测失败"当成"进程已死"。"""
    _register_process(tmp_path, {"pid": 4321, "host_start_time": None})
    reaper = OrphanReaper(
        tmp_path / "data", alive=lambda pid: True, start_time=lambda pid: None
    )
    assert reaper.find_orphans() == []
    assert reaper.find_stale() == []


def test_bogus_pid_is_an_orphan(tmp_path):
    """连 pid 都不是数字的登记一定是坏的。"""
    _register_process(tmp_path, {"pid": "不是数字", "host_start_time": 1.0})
    orphans = _reaper(tmp_path).find_orphans()
    assert len(orphans) == 1
    assert orphans[0].pid == 0


# ── 孤儿锁 ──────────────────────────────────────────────────────────────────


def test_orphan_dispatcher_lock_is_detected(tmp_path):
    """``dispatcher.lock`` 指向死进程时，它也是一条无效登记。"""
    paths = _paths(tmp_path)
    paths.ensure()
    pid = _dead_pid()
    gw.write_json(paths.lock, {"pid": pid, "host_start_time": 1.0})

    orphans = _reaper(tmp_path).find_orphans()
    assert [(item.source, item.pid) for item in orphans] == [(SOURCE_LOCK, pid)]


def test_corrupt_lock_is_reported_not_ignored(tmp_path):
    """锁文件坏掉时如实报出来——"读不出来"不等于"没问题"。"""
    paths = _paths(tmp_path)
    paths.ensure()
    paths.lock.write_text("{ 这不是 JSON", encoding="utf-8")
    orphans = _reaper(tmp_path).find_orphans()
    assert len(orphans) == 1
    assert orphans[0].source == SOURCE_LOCK
    assert "无法解析" in orphans[0].reason


# ── dry-run 与清理 ──────────────────────────────────────────────────────────


def test_dry_run_does_not_touch_the_ledgers(tmp_path):
    """``dry_run=True`` 只报告：两个登记簿的字节内容一字不变。"""
    pid = _dead_pid()
    _register_process(tmp_path, {"pid": pid, "host_start_time": 1.0})
    _register_spawn(tmp_path, {"pid": pid, "create_time": 1.0, "purpose": "子进程"})
    paths = _paths(tmp_path)
    before = (paths.processes.read_bytes(), paths.spawn_ledger.read_bytes())

    report = _reaper(tmp_path).reap(dry_run=True)

    assert report.dry_run is True
    assert len(report.orphans) == 2, "两个来源各一条"
    assert report.removed == 0
    assert (paths.processes.read_bytes(), paths.spawn_ledger.read_bytes()) == before


def test_reap_removes_only_dead_entries(tmp_path):
    """``dry_run=False`` 清掉死条目，**活的那条必须留着**。"""
    dead = _dead_pid()
    alive_pid, alive_start = gw.identity()
    paths = _paths(tmp_path)
    gw.ProcessRegistry(paths.processes).save(
        [
            {"pid": dead, "host_start_time": 1.0},
            {"pid": alive_pid, "host_start_time": alive_start},
        ]
    )
    gw.SpawnLedger(paths.spawn_ledger).save([{"pid": dead, "create_time": 1.0, "purpose": "x"}])

    report = _reaper(tmp_path).reap(dry_run=False)

    assert report.removed == 2
    assert [item["pid"] for item in gw.ProcessRegistry(paths.processes).load()] == [alive_pid]
    assert gw.SpawnLedger(paths.spawn_ledger).load() == []


def test_reap_deletes_the_orphan_lock(tmp_path):
    """孤儿锁在 ``dry_run=False`` 时被删掉（清登记，不是杀进程）。"""
    paths = _paths(tmp_path)
    paths.ensure()
    pid = _dead_pid()
    gw.write_json(paths.lock, {"pid": pid, "host_start_time": 1.0})

    report = _reaper(tmp_path).reap(dry_run=False)
    assert report.lock_removed is True
    assert not paths.lock.exists()


def test_reap_keeps_a_healthy_lock(tmp_path):
    """活着的 holder 的锁不能动——否则第二个 dispatcher 会被放进来。"""
    paths = _paths(tmp_path)
    paths.ensure()
    pid, start = gw.identity()
    gw.write_json(paths.lock, {"pid": pid, "host_start_time": start})

    report = _reaper(tmp_path).reap(dry_run=False)
    assert report.lock_removed is False
    assert paths.lock.exists()


def test_report_serializes_for_json_output(tmp_path):
    pid = _dead_pid()
    _register_process(tmp_path, {"pid": pid, "host_start_time": 1.0})
    payload = _reaper(tmp_path).reap(dry_run=True).as_dict()
    assert payload["orphan_count"] == 1
    assert payload["stale_count"] == 0
    assert json.dumps(payload)  # 可序列化
    assert payload["orphans"][0]["pid"] == pid


# ── 绝不 kill ───────────────────────────────────────────────────────────────


def test_module_never_kills_processes():
    """源码里不许出现任何杀进程的调用（AST 级门禁，不靠 grep 字面量）。

    理由：孤儿判据基于"读进程创建时间"这种尽力而为的探测，在不可靠判据上执行
    不可逆动作是不可接受的。真要收进程树是 ``app/gateway.py`` 的事。
    """
    source = Path(reaper_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {
        "taskkill",
        "kill",
        "killpg",
        "terminate",
        "TerminateProcess",
        "Popen",
        "system",
        "run",
    }
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name in forbidden:
                offenders.append(str(name))
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            offenders.append(str(node.attr))
        if isinstance(node, ast.Name) and node.id in {"subprocess", "signal"}:
            offenders.append(str(node.id))
    assert not offenders, f"孤儿回收模块不该出现这些动作：{sorted(set(offenders))}"
