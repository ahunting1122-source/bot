"""写侧统一（MATRIX 10.1）、平台投递（11.3）、级联紧急停止（11.6）、权限共享（11.5）。

四条边界：**不真跑 agent**（执行器是桩）、**不出网**（投递只用 ``console`` 旁路）、
**不常驻**（tick 就是一次调用）、**不动别人的存储**（平面与账本都落在 tmp_path）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

# 导入即登记真实现。**必须显式导入**：`commands/__init__.py` 由别的波次统一接线，
# 本模块的用例不能依赖那行 import 已经存在（否则接线前后行为不一致）。
import app.cli.commands.cron  # noqa: F401
import app.cli.commands.cron_extra  # noqa: F401
from app import cron
from app.cli.main import main
from app.delivery_ledger import STATUS_DELIVERED, DeliveryLedger
from app.gateway import GatewayService
from app.platforms import PLATFORMS, Platform
from app.session_plane import (
    KIND_CRON_RUN_FAILED,
    KIND_CRON_RUN_FINISHED,
    KIND_CRON_RUN_STARTED,
    KIND_GATEWAY_DELIVERED,
    SessionPlane,
)

#: 固定"现在"：2026-01-05 是周一，周字段相关断言才读得懂。
NOW = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)


def _due_job(store: cron.CronStore, **kwargs: Any) -> cron.Job:
    """建一条"已经到点"的任务：一小时前创建、间隔 30m。"""
    return cron.create_job(store, schedule="30m", now=NOW - timedelta(hours=1), **kwargs)


async def _ok_runner(job: cron.Job) -> dict[str, Any]:
    return {"ok": True, "output": f"ran {job.id}"}


async def _boom_runner(job: cron.Job) -> dict[str, Any]:
    raise RuntimeError("模型炸了")


def _events(data_dir: Path, session_id: str) -> list[dict[str, Any]]:
    return SessionPlane(data_dir).events(session_id)


# ── 写侧统一：cron 写平面（MATRIX 10.1） ────────────────────────────────────


async def test_successful_run_writes_started_and_finished(tmp_path: Path) -> None:
    """跑一次 → 平面上两条（started / finished），会话键是 ``cron:<job_id>``。"""
    store = cron.CronStore(tmp_path)
    job = _due_job(store, prompt="看门狗")
    results = await cron.CronRunner(tmp_path, _ok_runner).tick(NOW)

    assert results[0].ok is True
    events = _events(tmp_path, f"cron:{job.id}")
    assert [item["kind"] for item in events] == [
        KIND_CRON_RUN_STARTED,
        KIND_CRON_RUN_FINISHED,
    ]
    assert all(item["job_id"] == job.id for item in events)
    assert all(item["run_id"] == results[0].run_id for item in events)
    assert events[1]["ok"] is True


async def test_failed_run_writes_three_events(tmp_path: Path) -> None:
    """失败一次 → **三条**：started / finished / failed（失败是额外那一条）。

    为什么失败要多写一条：审计要能直接按 ``kind`` 捞失败，不必逐条解析 ``finished``
    的布尔字段——而"哪次跑挂了"正是事后最常被问的那句话。
    """
    store = cron.CronStore(tmp_path)
    job = _due_job(store, prompt="会炸")
    results = await cron.CronRunner(tmp_path, _boom_runner).tick(NOW)

    assert results[0].ok is False
    events = _events(tmp_path, f"cron:{job.id}")
    assert [item["kind"] for item in events] == [
        KIND_CRON_RUN_STARTED,
        KIND_CRON_RUN_FINISHED,
        KIND_CRON_RUN_FAILED,
    ]
    assert "模型炸了" in events[2]["error"]


async def test_unavailable_executor_is_not_recorded_as_failure(tmp_path: Path) -> None:
    """执行器没接线**不算失败**：平面里只有 started / finished，没有 ``failed``。

    否则"配置缺项"会在平面里堆成一串假的失败——而"按 kind 捞失败"正是审计最常用的一步。
    """
    store = cron.CronStore(tmp_path)
    job = _due_job(store, prompt="需要 agent 执行器")

    async def unavailable(target: cron.Job) -> dict[str, Any]:
        return {"ok": False, "unavailable": True, "error": "agent 执行器尚未接线"}

    results = await cron.CronRunner(tmp_path, unavailable).tick(NOW)
    assert results[0].unavailable is True
    events = _events(tmp_path, f"cron:{job.id}")
    assert [item["kind"] for item in events] == [
        KIND_CRON_RUN_STARTED,
        KIND_CRON_RUN_FINISHED,
    ]
    assert events[1]["unavailable"] is True


async def test_replaying_the_same_run_does_not_duplicate_events(tmp_path: Path) -> None:
    """同一个 ``run_id`` 跑两次 → 平面**不产生第二条**（幂等靠 message_key，不靠 id）。"""
    store = cron.CronStore(tmp_path)
    job = _due_job(store, prompt="x")
    runner = cron.CronRunner(tmp_path, _ok_runner)

    await runner.run_job(job, NOW, run_id="run-fixed")
    await runner.run_job(job, NOW, run_id="run-fixed")

    events = _events(tmp_path, f"cron:{job.id}")
    assert [item["kind"] for item in events] == [
        KIND_CRON_RUN_STARTED,
        KIND_CRON_RUN_FINISHED,
    ]
    # 幂等键落在事件上，事后能看出"这条是被哪个键写进来的"。
    assert all(item["message_key"] == f"cron:{job.id}:run-fixed:{item['kind'].rsplit('.', 1)[-1]}" for item in events)
    # runs.jsonl 是流水账，两次运行就该有两行（平面去重不代表运行去重）。
    assert len(store.load_runs()) == 2


# ── 写侧统一：gateway 写平面（MATRIX 10.1） ─────────────────────────────────


def test_gateway_delivery_writes_one_plane_event(tmp_path: Path) -> None:
    """投递一次 → 平面上一条 ``gateway.delivered``，会话键 ``gateway:console``。"""
    result = GatewayService(tmp_path).deliver("你好", platform="console", channel="dev")
    assert result.ok is True

    events = _events(tmp_path, "gateway:console")
    assert len(events) == 1
    assert events[0]["kind"] == KIND_GATEWAY_DELIVERED
    assert events[0]["platform"] == "console"
    assert events[0]["channel"] == "dev"
    assert events[0]["message_key"]


def test_gateway_repeated_message_key_is_idempotent(tmp_path: Path) -> None:
    """同 ``message_key`` 重复投递 → 平面上仍只有一条（重复写不产生两条）。"""
    service = GatewayService(tmp_path)
    service.deliver("你好", platform="console", message_key="k-1")
    service.deliver("你好", platform="console", message_key="k-1")
    assert len(_events(tmp_path, "gateway:console")) == 1

    # 结论变了就是另一件事：先失败后成功必须留两条，否则账面上永远只有失败。
    service.pause(reason="测试")
    service.deliver("你好", platform="console", message_key="k-1")
    service.resume()
    events = _events(tmp_path, "gateway:console")
    assert [item["kind"] for item in events] == [
        KIND_GATEWAY_DELIVERED,
        "gateway.delivery_failed",
    ]


def test_gateway_refuses_to_send_while_paused(tmp_path: Path) -> None:
    """紧急停止必须能真的挡住出站：返回**失败**而不是空成功。"""
    service = GatewayService(tmp_path)
    service.pause(reason="演练")
    result = service.deliver("你好", platform="console")
    assert result.ok is False
    assert "紧急停止" in result.error
    assert service.resume()["released"] is True


# ── 平台投递（MATRIX 11.3） ─────────────────────────────────────────────────


async def test_cron_platform_delivery_goes_through_gateway_and_ledger(tmp_path: Path) -> None:
    """已实现平台（``console`` 旁路）能投，结论同时进投递账本与平面。"""
    store = cron.CronStore(tmp_path)
    job = _due_job(store, prompt="汇报", deliver="platform:console:ops")
    results = await cron.CronRunner(tmp_path, _ok_runner).tick(NOW)

    assert results[0].ok is True
    assert results[0].delivered == "console:ops"

    ledger = DeliveryLedger(tmp_path)
    key = f"cron:{job.id}:{results[0].run_id}"
    latest = ledger.latest(key)
    assert latest is not None
    assert latest["status"] == STATUS_DELIVERED
    assert latest["platform"] == "console"
    assert latest["channel"] == "ops"
    # 平台那一侧也留了痕：一条 gateway.delivered。
    assert [item["kind"] for item in _events(tmp_path, "gateway:console")] == [
        KIND_GATEWAY_DELIVERED
    ]


def test_unimplemented_platform_is_rejected_at_create(tmp_path: Path) -> None:
    """未实现平台在 ``create`` 就拒——等到跑的时候才失败，用户已经等了半小时。

    判据来自 ``app/platforms.py`` 的 ``PLATFORMS[...].implemented``（单一事实源），
    所以这里也从同一处取一个"当前未实现"的平台，而不是硬编码某个名字
    （平台层正在逐波补适配器，硬编码会在别人补完时假失败）。
    """
    pending = [item for item in Platform if not PLATFORMS[item].implemented]
    if not pending:  # pragma: no cover - 22 个平台全实现时才走这里
        pytest.skip("22 个平台已全部实现，没有可用的「未实现」样本")
    store = cron.CronStore(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        cron.create_job(store, schedule="30m", prompt="x", deliver=f"platform:{pending[0].value}")
    message = str(excinfo.value)
    assert pending[0].value in message
    assert "尚未实现" in message
    # 校验发生在落盘之前：坏输入不留半个任务。
    assert store.load_jobs() == []
    assert not store.jobs_path.exists()


def test_unknown_platform_and_bare_targets_are_rejected(tmp_path: Path) -> None:
    store = cron.CronStore(tmp_path)
    for bad in ("platform:", "platform:origin", "telegram:1", "discord", "file:"):
        with pytest.raises(ValueError):
            cron.create_job(store, schedule="30m", prompt="x", deliver=bad)
    assert store.load_jobs() == []


def test_deliver_targets_parse_into_three_modes() -> None:
    """三种形状解析成同一组 ``(模式, 目标, 频道)``，投递侧不必各写一套判断。"""
    assert cron.parse_deliver("none") == ("none", "", "")
    assert cron.parse_deliver("file:out.md") == ("file", "out.md", "")
    assert cron.parse_deliver("platform:console") == ("platform", "console", "")
    assert cron.parse_deliver("platform:console:ops") == ("platform", "console", "ops")
    assert cron.validate_deliver("platform:CONSOLE:ops") == "platform:console:ops"


async def test_delivery_failure_fails_the_run(tmp_path: Path) -> None:
    """平台投递失败算任务失败：跑了但没人收到，在运维上等于没跑。"""
    service = GatewayService(tmp_path)
    service.pause(reason="演练")
    store = cron.CronStore(tmp_path)
    _due_job(store, prompt="汇报", deliver="platform:console")
    results = await cron.CronRunner(tmp_path, _ok_runner).tick(NOW)
    assert results[0].ok is False
    assert "紧急停止" in results[0].error


# ── 权限共享（MATRIX 11.5） ─────────────────────────────────────────────────


async def test_cron_reads_the_same_settings_json_as_the_interaction_side(tmp_path: Path) -> None:
    """权限档读的是**同一份** ``<data_dir>/settings.json``，并写进运行记录与平面事件。"""
    (tmp_path / "settings.json").write_text(
        json.dumps({"file_scope": "machine", "approval_mode": "smart"}), encoding="utf-8"
    )
    store = cron.CronStore(tmp_path)
    job = _due_job(store, prompt="需要整机权限的任务")
    results = await cron.CronRunner(tmp_path, _ok_runner).tick(NOW)

    assert results[0].file_scope == "machine"
    assert results[0].approval_mode == "smart"
    events = _events(tmp_path, f"cron:{job.id}")
    assert {item["file_scope"] for item in events} == {"machine"}
    assert {item["approval_mode"] for item in events} == {"smart"}
    # 流水账里也要有：事后追责看的是 runs.jsonl 与平面两边。
    record = store.load_runs()[-1]
    assert (record["file_scope"], record["approval_mode"]) == ("machine", "smart")


def test_permission_defaults_are_the_narrow_ones(tmp_path: Path) -> None:
    """没有 settings.json 时退回更窄的一档（workspace / off），绝不反向放开。"""
    perms = cron.read_permissions(tmp_path)
    assert perms["file_scope"] == "workspace"
    assert perms["approval_mode"] == "off"
    assert "默认值" in perms["source"]


def test_broken_settings_json_degrades_to_narrow_scope(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text("{ 坏掉的 json", encoding="utf-8")
    perms = cron.read_permissions(tmp_path)
    assert perms["file_scope"] == "workspace"
    assert perms["approval_mode"] == "off"


def test_unknown_permission_values_degrade(tmp_path: Path) -> None:
    (tmp_path / "settings.json").write_text(
        json.dumps({"file_scope": "root", "approval_mode": "yolo"}), encoding="utf-8"
    )
    perms = cron.read_permissions(tmp_path)
    assert perms["file_scope"] == "workspace"
    assert perms["approval_mode"] == "off"


# ── 级联紧急停止（MATRIX 11.6） ─────────────────────────────────────────────


async def test_pause_all_cascades_to_cron_gateway_and_running_jobs(tmp_path: Path) -> None:
    """``pause_all`` 同时管 cron 哨兵、gateway 哨兵与正在跑的任务（只标记）。"""
    store = cron.CronStore(tmp_path)
    _due_job(store, prompt="x")
    # 造一条"正在跑"的登记（真实场景里由另一个进程的 tick 写）。
    store.running.register("run-live", "cron-live", started_at=cron.to_iso(NOW))

    status = cron.pause_all(tmp_path, reason="演练")
    assert status.paused is True
    assert set(status.subsystems) == {"cron", "gateway", "running_jobs"}
    assert set(status.paused_names()) == {"cron", "gateway", "running_jobs"}

    assert store.paused() is True
    assert (tmp_path / "gateway" / "paused").exists()
    marked = store.running.load()[0]
    assert marked["cancel_requested"] is True
    assert marked["cancel_reason"] == "演练"

    # cron 真的不跑了。
    assert await cron.CronRunner(tmp_path, _ok_runner).tick(NOW) == []
    # gateway 也真的不发。
    assert GatewayService(tmp_path).deliver("x", platform="console").ok is False


async def test_resume_all_restores_every_subsystem(tmp_path: Path) -> None:
    store = cron.CronStore(tmp_path)
    _due_job(store, prompt="x")
    store.running.register("run-live", "cron-live", started_at=cron.to_iso(NOW))
    cron.pause_all(tmp_path, reason="演练")

    status = cron.resume_all(tmp_path)
    assert status.paused is False
    assert status.paused_names() == []
    assert status.subsystems["running_jobs"]["unmarked"] == 1
    assert store.running.load()[0]["cancel_requested"] is False
    assert not (tmp_path / "gateway" / "paused").exists()

    results = await cron.CronRunner(tmp_path, _ok_runner).tick(NOW)
    assert len(results) == 1


def test_cascade_status_reports_each_subsystem(tmp_path: Path) -> None:
    """``--status`` 的价值是"我到底停了什么"：逐个报，而不是只给一个布尔。"""
    status = cron.cascade_status(tmp_path)
    assert status.paused is False
    assert status.subsystems["cron"]["paused"] is False
    assert status.subsystems["gateway"]["paused"] is False
    assert status.subsystems["running_jobs"]["running"] == 0

    cron.pause_all(tmp_path, reason="演练")
    again = cron.cascade_status(tmp_path)
    assert again.paused is True
    assert again.subsystems["cron"]["path"].endswith("paused")


async def test_cancelled_run_is_marked_but_not_killed(tmp_path: Path) -> None:
    """运行期间收到停止标记 → 只写进备注，结果照实记录（**不强杀**）。"""
    store = cron.CronStore(tmp_path)
    job = _due_job(store, prompt="x")
    runner = cron.CronRunner(tmp_path, _ok_runner)

    async def runner_with_pause(target: cron.Job) -> dict[str, Any]:
        # 模拟"跑到一半有人喊停"：执行体自己把标记打上（跨进程就是这样发生的）。
        store.running.mark_cancelled(reason="演练")
        return {"ok": True, "output": "跑完了"}

    runner.runner = runner_with_pause
    result = await runner.run_job(job, NOW, run_id="run-pause")
    assert result.ok is True  # 结果不变：标记不等于失败
    assert result.cancel_requested is True
    assert "只标记" in result.note
    # 跑完就注销，登记簿里不留残条。
    assert store.running.load() == []


# ── CLI（register_handler 接线的真实入口） ─────────────────────────────────


def test_cli_pause_status_lists_paused_subsystems(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    assert main(["--data-dir", str(data), "pause", "--status"]) == 0
    out = capsys.readouterr().out
    assert "未暂停" in out
    for name in ("cron", "gateway", "running_jobs"):
        assert name in out

    assert main(["--data-dir", str(data), "--json", "pause", "--reason", "演练"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["paused"] is True
    assert set(payload["subsystems"]) == {"cron", "gateway", "running_jobs"}

    assert main(["--data-dir", str(data), "--json", "pause", "--status"]) == 0
    assert json.loads(capsys.readouterr().out)["paused"] is True

    assert main(["--data-dir", str(data), "--json", "resume"]) == 0
    assert json.loads(capsys.readouterr().out)["paused"] is False


def test_cli_cron_pause_all_and_resume_all(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    assert main(["--data-dir", str(data), "cron", "pause-all", "--reason", "演练"]) == 0
    assert "已级联紧急停止" in capsys.readouterr().out
    assert (data / "cron" / "paused").exists()
    assert (data / "gateway" / "paused").exists()

    assert main(["--data-dir", str(data), "cron", "resume-all"]) == 0
    assert "已解除紧急停止" in capsys.readouterr().out
    assert not (data / "cron" / "paused").exists()


def test_cli_cron_events_lists_plane_events(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = tmp_path / "data"
    store = cron.CronStore(data)
    job = _due_job(store, prompt="x")
    assert main(["--data-dir", str(data), "cron", "tick"]) == 0
    capsys.readouterr()

    assert main(["--data-dir", str(data), "--json", "cron", "events", "--job-id", job.id]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session"] == f"cron:{job.id}"
    kinds = [item["kind"] for item in payload["events"]]
    assert KIND_CRON_RUN_STARTED in kinds and KIND_CRON_RUN_FINISHED in kinds


def test_cli_cron_deliver_resends_last_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """补投：把上一次的输出再送一遍，走的是与自动投递同一条路径。

    用 ``platform:file``（outbox.jsonl）而不是 ``console``：``--json`` 模式下
    console 适配器会直接往 stdout 打印，把结构化输出污染成不可解析的东西。
    """
    data = tmp_path / "data"
    store = cron.CronStore(data)
    job = cron.create_job(
        store,
        schedule="30m",
        no_agent=True,
        script="echo hello-cron",
        deliver="platform:file",
        now=NOW - timedelta(hours=1),
    )
    assert main(["--data-dir", str(data), "cron", "tick"]) == 0
    capsys.readouterr()

    assert main(["--data-dir", str(data), "--json", "cron", "deliver", job.id]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["platform"] == "file"
    # 补投沿用**那一轮**的 message_key：账本里仍然只有一条"这一轮的输出"，
    # 但 attempts 涨到 2 —— 重复投递是事实，重复记一条"另一条消息"才是谎话。
    ledger = DeliveryLedger(data)
    key = f"cron:{job.id}:{payload['run_id']}"
    assert ledger.keys() == [key]
    latest = ledger.latest(key)
    assert latest is not None
    assert latest["status"] == STATUS_DELIVERED
    assert latest["attempts"] == 2
    lines = (data / "outbox.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert "hello-cron" in lines[-1]


def test_cli_cron_deliver_without_output_or_text_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    data = tmp_path / "data"
    store = cron.CronStore(data)
    job = _due_job(store, prompt="x", deliver="platform:console")
    assert main(["--data-dir", str(data), "cron", "deliver", job.id]) == 1
    assert "还没有执行记录" in capsys.readouterr().err


def test_cli_paths_are_wired_into_declaration_table() -> None:
    """``register_handler`` 接的路径必须真的落到已构建的 ``Command`` 上。"""
    from app.cli.commands import cron_extra
    from app.cli.commands._table import HANDLERS
    from app.cli.spec import REGISTRY

    expected = {
        "pause": cron_extra.run_pause,
        "resume": cron_extra.run_resume,
        "cron deliver": cron_extra.run_deliver,
        "cron pause-all": cron_extra.run_pause_all,
        "cron resume-all": cron_extra.run_resume_all,
        "cron events": cron_extra.run_events,
    }
    assert expected.keys() <= HANDLERS.keys()
    for path, handler in expected.items():
        assert HANDLERS[path] is handler
    pause = REGISTRY.get("pause")
    assert pause is not None and pause.handler is cron_extra.run_pause
    assert pause.state == "done"
    assert any("--status" in option.flags for option in pause.options)
    cron_command = REGISTRY.get("cron")
    assert cron_command is not None
    for name in ("deliver", "pause-all", "resume-all", "events"):
        child = cron_command.child(name)
        assert child is not None and child.state == "done"


def test_deferred_fixup_restores_the_status_option() -> None:
    """``--status`` 的兜底钩子：表组装完那一刻补上选项（接线顺序变了也不丢）。

    模拟"本模块排在 ``hermes_parity`` 之前导入"的那一刻：把选项摘掉，再走一次
    ``REGISTRY.add``（``register_specs`` 组装完一定会走它），选项应当自动回来。
    """
    from app.cli.commands import cron_extra  # noqa: F401 —— 确保钩子已装
    from app.cli.spec import REGISTRY

    pause = REGISTRY.get("pause")
    assert pause is not None
    pause.options = [option for option in pause.options if "--status" not in option.flags]
    assert not any("--status" in option.flags for option in pause.options)

    REGISTRY.add()  # 组装完成时的写点：钩子在这里补选项
    assert any("--status" in option.flags for option in pause.options)
