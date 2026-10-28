"""启动时真的清一次孤儿账本（MATRIX 10.7 的验收判据）。

## 之前缺的是什么

`OrphanReaper` 能扫 `processes.json` + `spawn-ledger.json` 找"登记过但进程已死"的条目，
测试也齐。但 **`reap()` 的唯一调用方是 `orbit plane reap`**——进程崩过之后残留的登记
只有用户自己想起来敲那条命令才会被清。于是 `orbit plane` 一直报着几个早就死掉的进程，
而**"报告说谎"比"不报告"更糟**。

## 判据
1. **真起一次应用**（走 lifespan），残留在 `processes.json` 里的死 PID 条目被清掉；
2. **不误杀**：`reap()` 的契约是「绝不 kill 进程」，只删账本条目——这条也验一下，
   因为如果它其实会 kill，那"启动时自动跑"就是危险行为。
"""

from __future__ import annotations

import json

import pytest


def _dead_pid() -> int:
    """一个几乎肯定不存在的 PID（Windows 的 PID 空间远小于这个数）。"""
    return 999_999


@pytest.fixture()
def app_with_stale_ledger(tmp_path):
    """造一个「上次崩了」的账本：登记了一个已死的 PID。"""
    from app.config import Settings
    from app.main import create_app

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    paths_dir = settings.data_dir / "gateway"
    paths_dir.mkdir(parents=True, exist_ok=True)
    ledger = paths_dir / "processes.json"
    # **落盘是 JSON 数组**（`ProcessRegistry.load()` 明确只接受 list，
    # 其它形状一律当空——写成 `{"entries": [...]}` 会让回收"什么都没找到"，
    # 而看起来一切正常）。
    ledger.write_text(
        json.dumps(
            [
                {
                    "pid": _dead_pid(),
                    "host_start_time": 1.0,
                    "kind": "gateway",
                    "channel": "telegram",
                    "started_at": 1.0,
                }
            ]
        ),
        encoding="utf-8",
    )
    return create_app(settings=settings), ledger


def test_startup_reaps_dead_ledger_entries(app_with_stale_ledger):
    """**真起一次应用**，死条目必须在 lifespan 里被清掉。"""
    from starlette.testclient import TestClient

    app, ledger = app_with_stale_ledger
    before = json.loads(ledger.read_text(encoding="utf-8"))
    assert before, "前置条件不成立：账本里应该有那条死登记"

    # 用 with 触发 lifespan（startup + shutdown）
    with TestClient(app):
        pass

    after = json.loads(ledger.read_text(encoding="utf-8"))
    pids = [entry.get("pid") for entry in after]
    assert _dead_pid() not in pids, (
        f"启动没有回收死条目，账本里还有 {pids}——`orbit plane` 会一直报假进程"
    )


def test_reap_never_kills_processes(tmp_path):
    """`reap()` 的契约是**绝不 kill**——只清账本。

    这条是"启动时自动跑"的前提：如果它会 kill，自动跑就是危险行为。
    用**当前进程自己的 PID** 造一条"登记过"的记录，跑 `dry_run=False` 之后
    本进程必须还活着。
    """
    import os

    from app.config import Settings
    from app.orphan_reaper import OrphanReaper

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    paths_dir = settings.data_dir / "gateway"
    paths_dir.mkdir(parents=True, exist_ok=True)
    (paths_dir / "processes.json").write_text(
        json.dumps(
            [
                {
                    "pid": os.getpid(),
                    "host_start_time": 1.0,  # 故意对不上，让它被判为 reused
                    "kind": "gateway",
                    "channel": "test",
                    "started_at": 1.0,
                }
            ]
        ),
        encoding="utf-8",
    )

    OrphanReaper(settings.data_dir).reap(dry_run=False)

    # 我还活着——`reap` 只动了账本
    assert os.getpid() > 0
    assert os.path.exists(os.path.dirname(os.path.abspath(__file__)))


def test_a_live_registered_process_is_never_reaped(tmp_path):
    """**三态不能退化成两态**：活着且身份相符的登记**必须保留**。

    监视子agent 的提醒：`OrphanReaper` 分 alive / dead / reused 三态。
    如果回收逻辑把它们压成两态（"活着 / 不活着"），就会把**另一个活着的实例**
    当孤儿删掉——那不只是报错，是把正在跑的服务从账本里抹掉。

    这里用**当前进程自己的 PID + 真实 start_time** 造一条 `alive` 记录，
    跑 `dry_run=False` 之后它必须还在。
    """
    import os

    from app.config import Settings
    from app.orphan_reaper import OrphanReaper, process_start_time

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    paths_dir = settings.data_dir / "gateway"
    paths_dir.mkdir(parents=True, exist_ok=True)
    ledger = paths_dir / "processes.json"

    started = process_start_time(os.getpid())
    if started is None:  # 拿不到 start_time 的平台构造不出 alive 记录
        pytest.skip("本平台读不到进程启动时间")

    ledger.write_text(
        json.dumps(
            [
                {
                    "pid": os.getpid(),
                    "host_start_time": started,
                    "kind": "gateway",
                    "channel": "live",
                    "started_at": 1.0,
                }
            ]
        ),
        encoding="utf-8",
    )

    report = OrphanReaper(settings.data_dir).reap(dry_run=False)

    remaining = [entry.get("pid") for entry in json.loads(ledger.read_text(encoding="utf-8"))]
    assert os.getpid() in remaining, (
        "活着的登记被回收删掉了——三态（alive/dead/reused）被退化成了两态"
    )
    assert report.removed == 0, f"不该删任何东西，实际删了 {report.removed} 条"
