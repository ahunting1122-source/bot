"""Gateway 的契约测试：互斥、四个状态文件、崩溃循环、路由、投递账本、CLI。

这一层最容易假绿：只要不抛异常，看起来"网关起来了"。所以测试盯着**落盘的事实**——
锁文件里到底写了什么、四个状态文件在不在、账本里最后一行是 ok 还是 failed。
另外所有网络路径都走 ``httpx.MockTransport``：本机无网络，任何真请求都是 bug。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from app import gateway as gw
from app.cli.main import main
from app.platforms import Platform, WebhookAdapter

#: 显式导入即接线（生产里由 ``app/cli/commands/__init__.py`` 统一接线）。
import app.cli.commands.gateway as gateway_cli  # noqa: E402,F401  isort: skip


# ── 夹具 ────────────────────────────────────────────────────────────────────


def _dead_pid() -> int:
    """造一个**确定已退出**的 pid：起一个空进程、等它结束、拿它的 pid。

    比随手写 ``999999`` 可靠：那个数字可能真的被系统分配给了别的进程。
    """
    process = subprocess.Popen(  # noqa: S603 - 测试里只跑 sys.executable 的空脚本
        [sys.executable, "-c", "pass"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    process.wait(timeout=30)
    return process.pid


def _ok_transport(calls: dict[str, int] | None = None) -> httpx.MockTransport:
    counter = calls if calls is not None else {}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] = counter.get("n", 0) + 1
        return httpx.Response(200, headers={"id": "msg-1"})

    return httpx.MockTransport(handler)


def _service(tmp_path: Path, **kwargs) -> gw.GatewayService:
    transport = kwargs.pop("transport", None) or _ok_transport()
    adapters = {
        "ntfy": WebhookAdapter(
            "https://ntfy.test/topic", name="ntfy", platform=Platform.NTFY,
            body_shape="text", transport=transport,
        )
    }
    return gw.GatewayService(tmp_path / "data", adapters=adapters, **kwargs)


# ── 进程身份 ────────────────────────────────────────────────────────────────


def test_identity_of_live_and_dead_processes():
    """``pid_alive`` 必须能分清活/死；``process_start_time`` 要能拿到本进程的创建时刻。"""
    me = os.getpid()
    assert gw.pid_alive(me) is True
    assert gw.pid_alive(_dead_pid()) is False
    start = gw.process_start_time(me)
    assert start is not None and start > 0
    pid, host_start_time = gw.identity()
    assert pid == me and host_start_time is not None


# ── 单 dispatcher 互斥 ──────────────────────────────────────────────────────


def test_second_dispatcher_is_rejected(tmp_path):
    """同一数据目录的第二个 dispatcher 必须被拒绝，并说清"为什么不能起两个"。"""
    first = _service(tmp_path)
    assert first.start().ok
    second = _service(tmp_path)
    outcome = second.start()
    assert outcome.ok is False
    assert "已有 dispatcher 在运行" in outcome.reason
    # 锁没被第二个实例抢走
    holder = first.lock.read_holder()
    assert holder["pid"] == os.getpid()


def test_orphan_lock_from_dead_process_is_reclaimed(tmp_path):
    """死进程留下的锁 = 孤儿锁，下次启动要能回收（否则网关再也起不来）。"""
    paths = gw.GatewayPaths(tmp_path / "data")
    paths.ensure()
    dead = _dead_pid()
    gw.write_json(paths.lock, {"pid": dead, "host_start_time": time.time(), "acquired_at": 0})

    service = _service(tmp_path)
    outcome = service.start()
    assert outcome.ok is True
    assert outcome.reclaimed is True
    assert "孤儿锁" in outcome.reason
    assert service.lock.read_holder()["pid"] == os.getpid()


def test_pid_reuse_is_detected_by_start_time(tmp_path):
    """pid 活着但**创建时间对不上** = PID 被复用，也必须当孤儿锁回收。

    这是只凭 pid 判活会放行第二个 dispatcher 的那条路径，必须单独钉住。
    """
    paths = gw.GatewayPaths(tmp_path / "data")
    paths.ensure()
    gw.write_json(
        paths.lock,
        {"pid": os.getpid(), "host_start_time": 1.0, "acquired_at": 0},  # 明显不是本进程
    )
    service = _service(tmp_path)
    outcome = service.start()
    assert outcome.ok is True
    assert outcome.reclaimed is True


def test_force_takes_over_a_live_lock(tmp_path):
    """``--force`` 是逃生门：明确接管，并说明接管了谁的锁。"""
    first = _service(tmp_path)
    assert first.start().ok
    second = _service(tmp_path)
    outcome = second.start(force=True)
    assert outcome.ok is True
    assert outcome.reclaimed is True
    assert "--force" in outcome.reason


def test_stop_releases_only_own_lock(tmp_path):
    """释放锁时只释放自己的：别人的锁不能动，否则两个进程会互相踩。"""
    service = _service(tmp_path)
    service.start()
    paths = gw.GatewayPaths(tmp_path / "data")
    gw.write_json(paths.lock, {"pid": _dead_pid(), "host_start_time": time.time()})
    assert service.stop()["released"] is False
    assert paths.lock.exists()


# ── 四个状态文件 ────────────────────────────────────────────────────────────


def test_start_writes_all_four_state_files(tmp_path):
    """``start()`` 之后四个状态文件都要真的落盘，且内容能被读回来。"""
    service = _service(tmp_path)
    outcome = service.start()
    assert outcome.ok
    paths = service.paths

    # ① gateway-starts.log：纯文本，一行一个 epoch 秒
    stamps = gw.read_start_stamps(paths.starts)
    assert len(stamps) == 1
    assert abs(stamps[0] - time.time()) < 60

    # ② processes.json：(pid, host_start_time) 是身份
    processes = json.loads(paths.processes.read_text(encoding="utf-8"))
    assert isinstance(processes, list) and len(processes) == 1
    assert processes[0]["pid"] == os.getpid()
    assert processes[0]["host_start_time"] is not None
    assert processes[0]["purpose"] == "gateway"

    # ③ spawn-ledger.json：机器级账本（启动期扫过一遍孤儿）
    assert paths.spawn_ledger.exists()
    assert json.loads(paths.spawn_ledger.read_text(encoding="utf-8")) == []

    # ④ channel_directory.json：updated_at + platforms
    directory = json.loads(paths.channel_directory.read_text(encoding="utf-8"))
    assert directory["updated_at"]
    assert isinstance(directory["platforms"], dict)
    assert directory["platforms"] == {}  # 还没有任何会话，空是**正确**的


def test_process_registry_dedupes_by_pid_and_start_time(tmp_path):
    """登记簿以 ``(pid, host_start_time)`` 为键：同一进程重复登记只留一条。"""
    registry = gw.ProcessRegistry(tmp_path / "processes.json")
    registry.register({"pid": 1, "host_start_time": 10.0, "purpose": "a"})
    registry.register({"pid": 1, "host_start_time": 10.0, "purpose": "b"})
    registry.register({"pid": 1, "host_start_time": 20.0, "purpose": "c"})
    records = registry.load()
    assert len(records) == 2
    assert {record["purpose"] for record in records} == {"b", "c"}


def test_spawn_ledger_reaps_dead_orphans(tmp_path):
    """孤儿回收：死进程的条目被清掉，活进程的留着。"""
    ledger = gw.SpawnLedger(tmp_path / "spawn-ledger.json")
    dead = _dead_pid()
    ledger.save(
        [
            {"pid": dead, "create_time": time.time(), "purpose": "dashboard"},
            {"pid": os.getpid(), "create_time": gw.process_start_time(os.getpid()),
             "purpose": "mcp-helper"},
        ]
    )
    reaped = ledger.reap_orphans()
    assert [item["pid"] for item in reaped] == [dead]
    assert [item["purpose"] for item in ledger.load()] == ["mcp-helper"]


def test_broken_state_file_is_treated_as_empty_and_quarantined(tmp_path):
    """手抖写坏一个 JSON 不该让网关起不来：坏文件改名留证，按空处理。"""
    service = _service(tmp_path)
    service.paths.ensure()
    service.paths.processes.write_text("{ not json", encoding="utf-8")
    assert service.processes.load() == []
    assert list(service.paths.root.glob("processes.json.corrupt-*"))


# ── 崩溃循环检测 ────────────────────────────────────────────────────────────


def test_crash_loop_detected_from_dense_start_records(tmp_path):
    """连写 5 条启动记录 → ``health()`` 必须报崩溃循环。"""
    service = _service(tmp_path)
    service.start()  # 先正常启动一次，把四个状态文件写出来
    now = time.time()
    for offset in (50, 40, 30, 20, 10):
        gw._append_line(service.paths.starts, f"{now - offset:.6f}")  # noqa: SLF001

    report = service.health()
    assert report.crash_loop is not None
    assert report.crash_loop.crash_loop is True
    assert report.crash_loop.starts_in_window == 6
    assert report.healthy is False
    assert any("崩溃重启循环" in issue for issue in report.issues)


def test_sparse_starts_are_not_a_crash_loop(tmp_path):
    """稀疏的启动记录（跨天各一次）不能误报——否则提示会被当成噪音忽略。"""
    paths = gw.GatewayPaths(tmp_path / "data")
    paths.ensure()
    now = time.time()
    for offset in (86400 * 3, 86400 * 2, 86400):
        gw._append_line(paths.starts, f"{now - offset:.6f}")
    report = gw.detect_crash_loop(paths.starts, now=now)
    assert report.crash_loop is False
    assert report.total_starts == 3


def test_start_stamps_from_the_future_are_ignored(tmp_path):
    """明显来自未来的戳（时钟回拨/手改）不计入，否则一条坏数据会永久报崩溃循环。"""
    paths = gw.GatewayPaths(tmp_path / "data")
    paths.ensure()
    now = time.time()
    for offset in (10, 20, 30):
        gw._append_line(paths.starts, f"{now + offset:.6f}")
    assert gw.detect_crash_loop(paths.starts, now=now).crash_loop is False


def test_crash_loop_surfaces_in_status(tmp_path):
    service = _service(tmp_path)
    service.start()
    now = time.time()
    for offset in (30, 20, 10):
        gw._append_line(service.paths.starts, f"{now - offset:.6f}")  # noqa: SLF001
    assert service.status()["starts"]["crash_loop"] is True


# ── 路由 ────────────────────────────────────────────────────────────────────


def test_route_derives_and_remembers_session_key(tmp_path):
    """``route`` 给出确定性会话键，并把频道记进目录（会话反推那条数据源）。"""
    service = _service(tmp_path)
    key = service.route("ntfy", "#ops")
    assert key == "ntfy:#ops"
    assert service.route("ntfy", "#ops") == key  # 幂等
    directory = json.loads(service.paths.channel_directory.read_text(encoding="utf-8"))
    assert "#ops" in directory["platforms"]["ntfy"]
    assert directory["sessions"]["ntfy:#ops"] == key
    # 不给频道 = home channel
    assert service.route("ntfy") == "ntfy:home"


def test_channel_directory_rebuild_merges_enumeration_and_sessions(tmp_path):
    """重建目录要**合并**：能枚举的频道 + 从会话反推的频道，谁都不能被覆盖掉。"""

    class _Enumerable(WebhookAdapter):
        def list_channels(self) -> list[str]:
            return ["#ci", "#ops"]

    transport = _ok_transport()
    adapters = {
        "ntfy": _Enumerable("https://ntfy.test/t", name="ntfy", platform=Platform.NTFY,
                            transport=transport),
        "telegram": WebhookAdapter("", name="telegram", transport=transport),
    }
    service = gw.GatewayService(tmp_path / "data", adapters=adapters)
    service.directory.remember("telegram", "-100123")  # 反推来源
    rebuilt = service.directory.rebuild(adapters)
    assert set(rebuilt["platforms"]["ntfy"]) == {"#ci", "#ops"}
    assert rebuilt["platforms"]["telegram"] == ["-100123"]


# ── 投递 ────────────────────────────────────────────────────────────────────


def test_deliver_success_writes_one_ledger_line(tmp_path):
    service = _service(tmp_path)
    service.start()
    result = service.deliver("hello", platform="ntfy", channel="#ops")
    assert result.ok
    assert result.attempts == 1
    assert result.session_key == "ntfy:#ops"
    records = service.deliveries()
    assert len(records) == 1
    assert records[0]["status"] == "ok"
    assert records[0]["platform"] == "ntfy"
    assert records[0]["message_id"] == "msg-1"


def test_deliver_retries_then_records_failed_without_raising(tmp_path):
    """失败重试后仍失败：账本记 ``failed``，**且不抛到调用方**（cron 不该崩）。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500)

    service = _service(tmp_path, transport=httpx.MockTransport(handler))
    service.start()
    result = service.deliver("hello", platform="ntfy", channel="#ops", attempts=2)
    assert result.ok is False
    assert result.attempts == 2
    assert "500" in result.error
    records = service.deliveries()
    assert [record["status"] for record in records] == ["failed", "failed"]
    assert [record["attempt"] for record in records] == [1, 2]


def test_deliver_to_unimplemented_platform_is_flagged_not_raised(tmp_path):
    """未实现平台：``not_implemented=True``（不是"网络抖动"），且账本照样记一笔。"""
    service = _service(tmp_path)
    service.start()
    result = service.deliver("hello", platform="telegram", channel="@me")
    assert result.ok is False
    assert result.not_implemented is True
    assert "TELEGRAM_BOT_TOKEN" in result.error
    assert service.deliveries()[-1]["status"] == "failed"


def test_deliver_to_unknown_platform_does_not_raise(tmp_path):
    """连平台名都不认识：同样只记账、不抛（调用方只需要看 ``ok``）。"""
    service = _service(tmp_path)
    result = service.deliver("hello", platform="not-a-platform")
    assert result.ok is False
    assert result.not_implemented is True


def test_deliver_survives_adapter_crash(tmp_path):
    """适配器内部抛异常也要记账并返回失败，不能让异常穿到 cron 里。"""

    class _Exploding(WebhookAdapter):
        def send_text(self, channel: str, text: str):
            raise RuntimeError("适配器炸了")

    service = gw.GatewayService(
        tmp_path / "data",
        adapters={"ntfy": _Exploding("https://x.test", name="ntfy")},
    )
    result = service.deliver("hi", platform="ntfy", attempts=1)
    assert result.ok is False
    assert "适配器炸了" in result.error
    assert service.deliveries()[-1]["status"] == "failed"


def test_module_level_route_and_deliver(tmp_path):
    """模块级 ``route`` / ``deliver`` 与实例方法等价（cron 就是这么用的）。"""
    service = _service(tmp_path)
    assert gw.route("ntfy", "#ops", gateway=service) == "ntfy:#ops"
    result = gw.deliver("hi", platform="ntfy", channel="#ops", gateway=service)
    assert result.ok


# ── webhook 订阅 / 配对 / 对等节点（落盘 + 模板） ────────────────────────────


def test_webhook_subscription_renders_dot_notation(tmp_path):
    store = gw.WebhookStore(tmp_path / "webhooks.json")
    subscription = gw.WebhookSubscription(
        name="ci", prompt="build {build.status} on {repo}", events=["push"]
    )
    store.upsert(subscription)
    loaded = store.load()["ci"]
    assert loaded.render({"build": {"status": "green"}, "repo": "orbit"}) == "build green on orbit"
    # 缺字段原样保留，便于排障（不要静默变空字符串）
    assert "{build.status}" in loaded.render({})
    assert store.remove("ci") is True
    assert store.remove("ci") is False


def test_pairing_approve_and_revoke(tmp_path):
    store = gw.PairingStore(tmp_path / "pairing")
    entry = store.add_pending({"platform": "telegram", "user_id": "u1", "user_name": "小明"})
    assert store.pending()[0]["code"] == entry["code"]
    # 批准时可以给配对码（大小写不敏感），也可以给 request id
    assert store.approve("telegram", entry["code"].lower()) is not None
    assert store.pending() == []
    assert store.approved()[0]["user_id"] == "u1"
    assert store.revoke("telegram", "u1") is True
    assert store.revoke("telegram", "u1") is False


def test_pairing_approve_requires_matching_platform(tmp_path):
    store = gw.PairingStore(tmp_path / "pairing")
    entry = store.add_pending({"platform": "telegram", "user_id": "u1"})
    assert store.approve("discord", entry["code"]) is None


def test_peer_registry_masks_keys(tmp_path):
    registry = gw.PeerRegistry(tmp_path / "peers.json")
    registry.add("spark", "http://spark.lan:8377", key="secret-key-1234", note="homelab")
    record = registry.load()["spark"]
    assert record["url"] == "http://spark.lan:8377"
    assert gw.PeerRegistry.mask(record["key"]) == "secr***********"
    assert gw.PeerRegistry.mask("") == "（未设置）"
    assert registry.remove("spark") is True


# ── CLI ─────────────────────────────────────────────────────────────────────


def _run(tmp_path: Path, *argv: str) -> int:
    return main(["--data-dir", str(tmp_path / "data"), *argv])


def test_cli_gateway_status_and_doctor(tmp_path, capsys):
    assert _run(tmp_path, "gateway", "status") == 0
    out = capsys.readouterr().out
    assert "dispatcher" in out
    assert "22 个已登记" in out
    assert "3 个已实现" in out

    assert _run(tmp_path, "gateway", "start") == 0
    capsys.readouterr()
    assert _run(tmp_path, "gateway", "status", "--deep") == 0
    assert "状态文件" in capsys.readouterr().out

    assert _run(tmp_path, "gateway", "doctor") == 0
    assert "网关自检全部通过" in capsys.readouterr().out
    assert _run(tmp_path, "gateway", "stop") == 0


def test_cli_gateway_start_refuses_second_dispatcher(tmp_path, capsys):
    assert _run(tmp_path, "gateway", "start") == 0
    capsys.readouterr()
    assert _run(tmp_path, "gateway", "start") == 1
    assert "已有 dispatcher 在运行" in capsys.readouterr().err


def test_cli_send_to_console_and_file(tmp_path, capsys):
    assert _run(tmp_path, "send", "--to", "console", "hello") == 0
    assert "hello" in capsys.readouterr().out

    assert _run(tmp_path, "send", "--to", "file", "to outbox") == 0
    capsys.readouterr()
    outbox = (tmp_path / "data" / "outbox.jsonl").read_text(encoding="utf-8")
    assert "to outbox" in outbox
    # 投递账本也要有（send 走的是同一条 deliver 路径）
    ledger = (tmp_path / "data" / "gateway" / "deliveries.jsonl").read_text(encoding="utf-8")
    assert json.loads(ledger.strip().splitlines()[-1])["status"] == "ok"


def test_cli_send_to_unimplemented_platform_reports_reason(tmp_path, capsys):
    """未实现平台发消息：退出码 1 + 说清原因与所需凭证。"""
    code = _run(tmp_path, "send", "--to", "telegram:-100123", "hi")
    assert code == 1
    err = capsys.readouterr().err
    assert "未实现" in err
    assert "TELEGRAM_BOT_TOKEN" in err


def test_cli_send_unknown_platform_is_usage_error(tmp_path, capsys):
    assert _run(tmp_path, "send", "--to", "nope:x", "hi") == 2
    assert "未知平台" in capsys.readouterr().err


def test_cli_send_list(tmp_path, capsys):
    assert _run(tmp_path, "send", "--list") == 0
    out = capsys.readouterr().out
    for name in ("ntfy", "raft", "a2a", "console", "file"):
        assert name in out
    assert "仅声明" in out


def test_cli_send_reads_file(tmp_path, capsys):
    body = tmp_path / "body.txt"
    body.write_text("from a file", encoding="utf-8")
    assert _run(tmp_path, "send", "--to", "console", "--file", str(body), "--subject", "[CI]") == 0
    out = capsys.readouterr().out
    assert "[CI]" in out and "from a file" in out


def test_cli_webhook_round_trip(tmp_path, capsys):
    assert _run(tmp_path, "webhook", "list") == 0
    assert "没有任何 webhook 订阅" in capsys.readouterr().out

    assert (
        _run(
            tmp_path,
            "webhook",
            "subscribe",
            "ci",
            "--prompt",
            "build {build.status}",
            "--events",
            "push,tag",
            "--deliver",
            "console",
        )
        == 0
    )
    capsys.readouterr()
    assert _run(tmp_path, "webhook", "list") == 0
    out = capsys.readouterr().out
    assert "ci" in out and "/webhooks/ci" in out and "push、tag" in out

    assert _run(tmp_path, "webhook", "test", "ci", "--payload", '{"build": {"status": "ok"}}') == 0
    out = capsys.readouterr().out
    assert "build ok" in out
    assert "X-Orbit-Signature: sha256=" in out

    assert _run(tmp_path, "webhook", "remove", "ci") == 0
    assert _run(tmp_path, "webhook", "remove", "ci") == 1


def test_cli_webhook_deliver_only_requires_real_target(tmp_path, capsys):
    code = _run(tmp_path, "webhook", "subscribe", "bad", "--deliver-only")
    assert code == 1
    assert "--deliver-only" in capsys.readouterr().err


def test_cli_pairing_list_approve_revoke(tmp_path, capsys):
    assert _run(tmp_path, "pairing", "list") == 0
    assert "待批准：0 条" in capsys.readouterr().out

    # 入站侧（将来的适配器）才会写 pending；这里直接种一条，模拟真实链路
    store = gw.PairingStore(tmp_path / "data" / "pairing")
    entry = store.add_pending({"platform": "telegram", "user_id": "u9", "user_name": "阿九"})

    assert _run(tmp_path, "pairing", "list") == 0
    assert "阿九" in capsys.readouterr().out

    assert _run(tmp_path, "pairing", "approve", "telegram", entry["code"]) == 0
    capsys.readouterr()
    assert _run(tmp_path, "pairing", "revoke", "telegram", "u9") == 0
    capsys.readouterr()
    assert _run(tmp_path, "pairing", "revoke", "telegram", "u9") == 1
    assert _run(tmp_path, "pairing", "clear-pending") == 0


def test_cli_peer_add_list_remove(tmp_path, capsys):
    assert _run(tmp_path, "peer", "list") == 0
    assert "没有已注册的对等节点" in capsys.readouterr().out

    assert _run(tmp_path, "peer", "add", "spark", "--url", "http://spark.lan:8377",
                "--key", "abcdef123456") == 0
    out = capsys.readouterr().out
    assert "abcd" in out and "abcdef123456" not in out  # 密钥必须脱敏

    assert _run(tmp_path, "peer", "list") == 0
    assert "spark" in capsys.readouterr().out

    assert _run(tmp_path, "peer", "remove", "spark") == 0
    assert _run(tmp_path, "peer", "remove", "spark") == 1


def test_cli_peer_add_requires_http_url(tmp_path, capsys):
    assert _run(tmp_path, "peer", "add", "spark") == 1
    assert "--url" in capsys.readouterr().err
    assert _run(tmp_path, "peer", "add", "spark", "--url", "spark.lan:8377") == 1
    assert "http(s)" in capsys.readouterr().err


def test_cli_gateway_setup_lists_platforms(tmp_path, capsys):
    assert _run(tmp_path, "gateway", "setup") == 0
    out = capsys.readouterr().out
    assert "22 个" in out
    assert "ntfy" in out and "telegram" in out
    payload = json.loads((tmp_path / "data" / "gateway" / "platforms.json").read_text("utf-8"))
    assert payload["enabled"] == ["a2a", "ntfy", "raft"]
    assert len(payload["platforms"]) == 22


def test_cli_gateway_doctor_goes_red_on_crash_loop(tmp_path, capsys):
    """崩溃循环要能被 ``gateway doctor`` 抓出来（退出码 1），而不是只写进状态文件。"""
    assert _run(tmp_path, "gateway", "start") == 0
    capsys.readouterr()
    starts = tmp_path / "data" / "gateway" / "gateway-starts.log"
    now = time.time()
    for offset in (40, 30, 20, 10):
        gw._append_line(starts, f"{now - offset:.6f}")  # noqa: SLF001
    assert _run(tmp_path, "gateway", "doctor") == 1
    out = capsys.readouterr()
    assert "崩溃循环" in out.out
    assert "未通过" in out.err


def test_cli_json_output_is_machine_readable(tmp_path, capsys):
    assert main(["--data-dir", str(tmp_path / "data"), "--json", "gateway", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["platforms"]["total"] == 22


def test_cli_gateway_doctor_subcommand_exists():
    """``gateway doctor`` 是表里没有的子命令，必须由本模块挂上去。"""
    from app.cli.spec import REGISTRY

    parent = REGISTRY.get("gateway")
    assert parent is not None
    child = parent.child("doctor")
    assert child is not None
    assert child.state == "done"
    assert child.handler is gateway_cli.run_doctor


def test_cli_paths_are_wired_into_declaration_table():
    """用 ``register_handler`` 接的路径必须真的落到已构建的 ``Command`` 上。"""
    from app.cli.commands._table import HANDLERS
    from app.cli.spec import REGISTRY

    expected = {
        "gateway status": gateway_cli.run_status,
        "gateway setup": gateway_cli.run_setup,
        "gateway start": gateway_cli.run_start,
        "gateway stop": gateway_cli.run_stop,
        "gateway restart": gateway_cli.run_restart,
        "send": gateway_cli.run_send,
        "webhook subscribe": gateway_cli.run_webhook,
        "webhook list": gateway_cli.run_webhook,
        "webhook remove": gateway_cli.run_webhook,
        "webhook test": gateway_cli.run_webhook,
        "pairing list": gateway_cli.run_pairing,
        "pairing approve": gateway_cli.run_pairing,
        "pairing revoke": gateway_cli.run_pairing,
        "pairing clear-pending": gateway_cli.run_pairing,
        "peer add": gateway_cli.run_peer,
        "peer list": gateway_cli.run_peer,
        "peer remove": gateway_cli.run_peer,
        "peer status": gateway_cli.run_peer,
    }
    assert expected.keys() <= HANDLERS.keys()
    for path, handler in expected.items():
        assert HANDLERS[path] is handler
        head, _, leaf = path.partition(" ")
        parent = REGISTRY.get(head)
        assert parent is not None, head
        target = parent.child(leaf) if leaf else parent
        assert target is not None, path
        assert target.handler is handler, path
        assert target.state == "done", path
