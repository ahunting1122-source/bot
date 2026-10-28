"""cron 子系统的契约测试。

三条边界：**不真跑 agent**（执行器是桩）、**不起常驻线程**（tick 就是一次调用）、
**不发网络请求**。测的是"调度算得对、tick 不炸、账记得住"这三件事——定时任务出问题
时最难查的正是这三样，而 agent 本身的行为由它自己的测试覆盖。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

# 导入即登记真实现。这里**必须**显式导入：`commands/__init__.py` 由别的波次统一接线，
# 本模块的测试不能依赖那行 import 已经存在（否则这条用例在接线前后行为不一致）。
import app.cli.commands.cron  # noqa: F401
from app import cron

#: 固定"现在"：2026-01-05 是周一，周字段相关断言才读得懂。
NOW = datetime(2026, 1, 5, 8, 0, tzinfo=UTC)


def _due_job(store: cron.CronStore, **kwargs: Any) -> cron.Job:
    """建一条"已经到点"的任务：一小时前创建、间隔 30m。"""
    return cron.create_job(store, schedule="30m", now=NOW - timedelta(hours=1), **kwargs)


async def _ok_runner(job: cron.Job) -> dict[str, Any]:
    return {"ok": True, "output": f"ran {job.id}"}


async def _boom_runner(job: cron.Job) -> dict[str, Any]:
    raise RuntimeError("模型炸了")


# ── 调度表达式 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("text", ["30m", "2h", "90s", "1d", "every 2h", "every hour", "every 30 minutes"])
def test_interval_forms_parse(text: str) -> None:
    assert isinstance(cron.parse_schedule(text), cron.Interval)


@pytest.mark.parametrize(
    "text",
    ["*/15 * * * *", "0 9 * * 1-5", "0 9 1 * *", "30 8 * * 0", "0 0 * * 7", "5,35 * * * *", "0 9-18/2 * * *"],
)
def test_cron_forms_parse(text: str) -> None:
    assert isinstance(cron.parse_schedule(text), cron.CronSpec)


def test_weekday_seven_is_sunday() -> None:
    """周字段 0 与 7 都指周日（vixie cron 的兼容写法，用户手写时常混用）。"""
    spec = cron.parse_schedule("0 9 * * 7")
    assert isinstance(spec, cron.CronSpec)
    assert spec.weekdays == frozenset({0})


@pytest.mark.parametrize(
    "text",
    [
        "",
        "bogus",
        "0 9 * *",  # 只有 4 个字段
        "99 * * * *",  # 分钟超范围
        "0 24 * * *",  # 小时超范围
        "0 9 * * 8",  # 周字段超范围
        "*/0 * * * *",  # 步长为 0
        "10-5 * * * *",  # 范围倒序
        "0 9 * * abc",  # 非数字
        "in 30m",  # 一次性写法：明确不支持
    ],
)
def test_bad_schedules_raise(text: str) -> None:
    with pytest.raises(ValueError):
        cron.parse_schedule(text)


def test_next_run_interval_and_cron() -> None:
    assert cron.next_run("30m", NOW) == NOW + timedelta(minutes=30)
    assert cron.next_run("every 2h", NOW) == NOW + timedelta(hours=2)
    assert cron.next_run("0 9 * * *", NOW) == datetime(2026, 1, 5, 9, 0, tzinfo=UTC)
    assert cron.next_run("*/15 * * * *", NOW) == datetime(2026, 1, 5, 8, 15, tzinfo=UTC)


def test_next_run_weekday_range_skips_weekend() -> None:
    """`0 9 * * 1-5` 从周五 10:00 出发，下一次是下周一 9:00（不是周六）。"""
    friday = datetime(2026, 1, 9, 10, 0, tzinfo=UTC)
    assert cron.next_run("0 9 * * 1-5", friday) == datetime(2026, 1, 12, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize("schedule", ["*/15 * * * *", "30m", "0 9 * * 1-5"])
def test_next_run_is_strictly_monotonic(schedule: str) -> None:
    """单调推进是硬契约：不单调就会在同一分钟里被反复触发。"""
    moment = NOW
    for _ in range(40):
        following = cron.next_run(schedule, moment)
        assert following > moment
        moment = following


def test_is_due_compares_next_run_at(tmp_path: Path) -> None:
    store = cron.CronStore(tmp_path)
    job = cron.create_job(store, schedule="30m", prompt="汇报", now=NOW)
    assert not cron.is_due(job, NOW + timedelta(minutes=1))
    assert cron.is_due(job, NOW + timedelta(minutes=31))
    job.enabled = False
    assert not cron.is_due(job, NOW + timedelta(minutes=31))


def test_is_due_respects_repeat_limit(tmp_path: Path) -> None:
    store = cron.CronStore(tmp_path)
    job = cron.create_job(store, schedule="30m", prompt="x", repeat=1, now=NOW)
    job.run_count = 1
    assert not cron.is_due(job, NOW + timedelta(hours=1))


# ── 存储与 CRUD ─────────────────────────────────────────────────────────────


def test_crud_round_trip_and_idempotent_write(tmp_path: Path) -> None:
    store = cron.CronStore(tmp_path)
    job = cron.create_job(store, schedule="30m", prompt="汇报", now=NOW)
    assert [item.id for item in store.load_jobs()] == [job.id]

    # 同样的内容写两遍，落盘字节一致（原子写 + 稳定序列化 = 可 diff）。
    first = store.jobs_path.read_text(encoding="utf-8")
    store.save_jobs(store.load_jobs())
    assert store.jobs_path.read_text(encoding="utf-8") == first

    updated = cron.update_job(store, job.id, schedule="2h", name="日报", now=NOW)
    assert updated is not None
    assert updated.schedule == "2h"
    assert updated.next_run_at == cron.to_iso(NOW + timedelta(hours=2))
    assert cron.update_job(store, "cron-nope", schedule="1h") is None

    assert cron.remove_job(store, job.id) is True
    assert cron.remove_job(store, job.id) is False
    assert store.load_jobs() == []


def test_create_validates_before_writing(tmp_path: Path) -> None:
    store = cron.CronStore(tmp_path)
    with pytest.raises(ValueError):
        cron.create_job(store, schedule="bogus", prompt="x")
    with pytest.raises(ValueError):
        cron.create_job(store, schedule="30m", prompt="x", deliver="telegram:1")
    with pytest.raises(ValueError):
        cron.create_job(store, schedule="30m", no_agent=True)  # 脚本模式缺脚本
    with pytest.raises(ValueError):
        cron.create_job(store, schedule="30m")  # 既无 prompt 也无 skill
    assert store.load_jobs() == []
    assert not store.jobs_path.exists()


def test_corrupt_jobs_file_is_treated_as_empty(tmp_path: Path) -> None:
    """写坏的 JSON 当空表，但**原始文件留证**——不能悄悄覆盖用户手改的内容。"""
    store = cron.CronStore(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    store.jobs_path.write_text("{ 这不是 json", encoding="utf-8")
    assert store.load_jobs() == []
    assert any(item.name.startswith("jobs.json.corrupt-") for item in store.root.iterdir())
    job = cron.create_job(store, schedule="30m", prompt="x", now=NOW)
    assert [item.id for item in store.load_jobs()] == [job.id]


def test_job_tolerates_hand_edited_json(tmp_path: Path) -> None:
    store = cron.CronStore(tmp_path)
    store.root.mkdir(parents=True, exist_ok=True)
    store.jobs_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "cron-hand",
                        "schedule": "30m",
                        "prompt": "手写的",
                        "repeat": "3",
                        "enabled": "false",
                        "skills": "a, b",
                        "no_agent": "true",
                        "unknown_field": 1,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    job = store.load_jobs()[0]
    assert job.repeat == 3
    assert job.enabled is False
    assert job.skills == ["a", "b"]
    assert job.no_agent is True


def test_deliver_validation() -> None:
    assert cron.validate_deliver("") == "none"
    assert cron.validate_deliver("file:out.md") == "file:out.md"
    for bad in ("telegram:1", "discord", "origin", "file:"):
        with pytest.raises(ValueError):
            cron.validate_deliver(bad)


# ── tick ────────────────────────────────────────────────────────────────────


async def test_tick_runs_only_due_jobs(tmp_path: Path) -> None:
    store = cron.CronStore(tmp_path)
    due = _due_job(store, prompt="到点了")
    later = cron.create_job(store, schedule="2h", prompt="还早", now=NOW)
    results = await cron.CronRunner(tmp_path, _ok_runner).tick(NOW)

    assert [item.job_id for item in results] == [due.id]
    jobs = {item.id: item for item in store.load_jobs()}
    assert jobs[due.id].run_count == 1
    assert jobs[due.id].last_run_at == cron.to_iso(NOW)
    following = cron.from_iso(jobs[due.id].next_run_at)
    assert following is not None and following > NOW
    assert jobs[later.id].run_count == 0

    records = store.load_runs()
    assert len(records) == 1
    assert records[0]["job_id"] == due.id
    assert records[0]["ok"] is True


async def test_tick_records_failure_without_breaking_the_round(tmp_path: Path) -> None:
    """一条任务炸了，同一轮里的另一条必须照跑——外部调度器只看退出码，整轮崩了就全停。"""
    store = cron.CronStore(tmp_path)
    bad = _due_job(store, prompt="会炸")
    good = _due_job(store, prompt="不会炸")

    async def runner(job: cron.Job) -> dict[str, Any]:
        if job.id == bad.id:
            raise RuntimeError("模型炸了")
        return {"ok": True, "output": "fine"}

    results = await cron.CronRunner(tmp_path, runner).tick(NOW)
    assert len(results) == 2
    by_id = {item.job_id: item for item in results}
    assert by_id[bad.id].ok is False
    assert "模型炸了" in by_id[bad.id].error
    assert by_id[good.id].ok is True

    jobs = {item.id: item for item in store.load_jobs()}
    assert jobs[bad.id].failures == 1
    assert jobs[bad.id].enabled is True
    assert jobs[good.id].failures == 0
    assert len(store.load_runs()) == 2


async def test_three_consecutive_failures_disable_the_job(tmp_path: Path) -> None:
    store = cron.CronStore(tmp_path)
    job = _due_job(store, prompt="坏任务")
    moment = NOW
    result = None
    for _ in range(cron.MAX_CONSECUTIVE_FAILURES):
        results = await cron.CronRunner(tmp_path, _boom_runner).tick(moment)
        assert [item.job_id for item in results] == [job.id]
        result = results[0]
        moment += timedelta(minutes=31)  # 失败后 next_run_at 推进了，得走到下一个槽位

    assert result is not None
    assert result.disabled is True
    assert "自动停用" in result.note
    saved = store.load_jobs()[0]
    assert saved.enabled is False
    assert saved.failures == cron.MAX_CONSECUTIVE_FAILURES
    assert len(store.load_runs()) == cron.MAX_CONSECUTIVE_FAILURES


async def test_success_resets_consecutive_failures(tmp_path: Path) -> None:
    store = cron.CronStore(tmp_path)
    job = _due_job(store, prompt="偶尔失败")
    state = {"calls": 0}

    async def flaky(_job: cron.Job) -> dict[str, Any]:
        state["calls"] += 1
        if state["calls"] == 1:
            raise RuntimeError("第一次失败")
        return {"ok": True, "output": "第二次成功"}

    await cron.CronRunner(tmp_path, flaky).tick(NOW)
    assert store.load_jobs()[0].failures == 1
    await cron.CronRunner(tmp_path, flaky).tick(NOW + timedelta(minutes=31))
    assert store.load_jobs()[0].failures == 0
    assert store.load_jobs()[0].enabled is True
    assert job.id == store.load_jobs()[0].id


async def test_unavailable_executor_does_not_count_as_failure(tmp_path: Path) -> None:
    """执行器没接线不是任务的错：照实记流水，但不计连续失败（否则任务会被自动停用）。"""
    store = cron.CronStore(tmp_path)
    _due_job(store, prompt="agent 模式")

    async def missing(_job: cron.Job) -> dict[str, Any]:
        return {"ok": False, "unavailable": True, "error": "执行器未接线"}

    moment = NOW
    for _ in range(cron.MAX_CONSECUTIVE_FAILURES):
        await cron.CronRunner(tmp_path, missing).tick(moment)
        moment += timedelta(minutes=31)

    saved = store.load_jobs()[0]
    assert saved.enabled is True
    assert saved.failures == 0
    assert saved.run_count == cron.MAX_CONSECUTIVE_FAILURES


async def test_tick_stops_after_repeat_limit(tmp_path: Path) -> None:
    store = cron.CronStore(tmp_path)
    cron.create_job(store, schedule="30m", prompt="只跑一次", repeat=1, now=NOW - timedelta(hours=1))
    results = await cron.CronRunner(tmp_path, _ok_runner).tick(NOW)
    assert results[0].disabled is True
    saved = store.load_jobs()[0]
    assert saved.enabled is False
    assert saved.next_run_at == ""
    assert await cron.CronRunner(tmp_path, _ok_runner).tick(NOW + timedelta(hours=5)) == []


async def test_file_delivery_writes_output(tmp_path: Path) -> None:
    store = cron.CronStore(tmp_path)
    target = tmp_path / "out" / "report.md"
    job = _due_job(store, prompt="写文件", deliver=f"file:{target}")
    results = await cron.CronRunner(tmp_path, _ok_runner).tick(NOW)
    assert results[0].delivered == str(target)
    assert target.read_text(encoding="utf-8").strip() == f"ran {job.id}"


async def test_bad_deliver_target_fails_the_run(tmp_path: Path) -> None:
    """手改进 jobs.json 的非法投递目标：记成失败，而不是假装投递成功。"""
    store = cron.CronStore(tmp_path)
    _due_job(store, prompt="x")
    jobs = store.load_jobs()
    jobs[0].deliver = "telegram:1"
    store.save_jobs(jobs)
    results = await cron.CronRunner(tmp_path, _ok_runner).tick(NOW)
    assert results[0].ok is False
    assert "平台投递未实现" in results[0].error


# ── 暂停 / 恢复 ─────────────────────────────────────────────────────────────


async def test_paused_job_is_not_run(tmp_path: Path) -> None:
    store = cron.CronStore(tmp_path)
    job = _due_job(store, prompt="x")
    runner = cron.CronRunner(tmp_path, _ok_runner)
    assert runner.pause(job.id) is True
    assert await runner.tick(NOW) == []
    assert runner.pause("cron-nope") is False

    assert runner.resume(job.id, run_now=True, now=NOW) is not None
    assert len(await runner.tick(NOW)) == 1


async def test_pause_all_sentinel_blocks_everything(tmp_path: Path) -> None:
    store = cron.CronStore(tmp_path)
    _due_job(store, prompt="x")
    runner = cron.CronRunner(tmp_path, _ok_runner)

    runner.pause_all(reason="紧急停止")
    assert store.paused() is True
    assert store.paused_path.exists()
    assert await runner.tick(NOW) == []

    runner.resume_all()
    assert store.paused() is False
    assert len(await runner.tick(NOW)) == 1


def test_resume_clears_failure_counter(tmp_path: Path) -> None:
    """恢复时清零连续失败：不清零的话，刚恢复的任务会被下一次失败立刻再停用。"""
    store = cron.CronStore(tmp_path)
    job = _due_job(store, prompt="x")
    jobs = store.load_jobs()
    jobs[0].failures = cron.MAX_CONSECUTIVE_FAILURES
    jobs[0].enabled = False
    store.save_jobs(jobs)

    resumed = cron.CronRunner(tmp_path, _ok_runner).resume(job.id, now=NOW)
    assert resumed is not None
    assert resumed.enabled is True
    assert resumed.failures == 0
    assert resumed.next_run_at == cron.to_iso(NOW + timedelta(minutes=30))


# ── incidents ───────────────────────────────────────────────────────────────


async def test_incidents_go_detected_alerted_closed(tmp_path: Path) -> None:
    store = cron.CronStore(tmp_path)
    _due_job(store, prompt="会坏")
    await cron.CronRunner(tmp_path, _boom_runner).tick(NOW)

    incidents = cron.sync_incidents(store)
    assert len(incidents) == 1
    assert incidents[0].state == "detected"
    assert incidents[0].failures == 1
    assert "模型炸了" in incidents[0].last_error

    acked = cron.acknowledge_incident(store, incidents[0].id)
    assert acked is not None
    assert acked.state == "alerted"
    assert cron.acknowledge_incident(store, "incident-nope") is None

    # 任务后来跑成功了 → 事故自动结案（但记录保留）。
    await cron.CronRunner(tmp_path, _ok_runner).tick(NOW + timedelta(minutes=31))
    closed = cron.sync_incidents(store)
    assert len(closed) == 1
    assert closed[0].state == "closed"
    assert closed[0].closed_at


async def test_incidents_ignore_unavailable_runs(tmp_path: Path) -> None:
    store = cron.CronStore(tmp_path)
    _due_job(store, prompt="x")

    async def missing(_job: cron.Job) -> dict[str, Any]:
        return {"ok": False, "unavailable": True, "error": "执行器未接线"}

    await cron.CronRunner(tmp_path, missing).tick(NOW)
    assert cron.sync_incidents(store) == []


# ── CLI ─────────────────────────────────────────────────────────────────────


def _cli(tmp_path: Path, *argv: str) -> int:
    from app.cli.main import main

    return main(["--data-dir", str(tmp_path / "data"), *argv])


def test_cli_create_then_list(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = _cli(
        tmp_path, "cron", "create", "30m", "--no-agent", "--script", "echo hi", "--name", "看门狗"
    )
    assert code == 0
    capsys.readouterr()

    assert _cli(tmp_path, "cron", "list") == 0
    out = capsys.readouterr().out
    assert "看门狗" in out
    assert "30m" in out


def test_cli_create_reports_next_run_in_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(tmp_path, "--json", "cron", "create", "every 2h", "汇报一下") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["job"]["schedule"] == "every 2h"
    assert payload["job"]["next_run_at"]


def test_cli_no_agent_without_script_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(tmp_path, "cron", "create", "30m", "--no-agent") == 1
    assert "--script" in capsys.readouterr().err


def test_cli_rejects_unimplemented_delivery(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """平台投递必须**明确报错**：静默接受会让用户以为输出已经发出去了。"""
    assert _cli(tmp_path, "cron", "create", "30m", "汇报", "--deliver", "telegram:1") == 1
    err = capsys.readouterr().err
    assert "平台投递未实现" in err
    assert "W9" in err


def test_cli_rejects_unimplemented_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(tmp_path, "cron", "create", "30m", "汇报", "--continuity") == 1
    assert "--continuity" in capsys.readouterr().err


def test_cli_bad_schedule_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli(tmp_path, "cron", "create", "not-a-schedule", "汇报") == 1
    assert "无法解析调度表达式" in capsys.readouterr().err


def test_cli_tick_runs_due_script_job(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """tick 是外部调度器的入口：执行到点任务、打印摘要、**返回 0**。"""
    assert _cli(tmp_path, "cron", "create", "30m", "--no-agent", "--script", "echo tick-ok") == 0
    store = cron.CronStore(tmp_path / "data")
    jobs = store.load_jobs()
    jobs[0].next_run_at = cron.to_iso(datetime.now(UTC) - timedelta(minutes=5))
    store.save_jobs(jobs)
    capsys.readouterr()

    assert _cli(tmp_path, "cron", "tick") == 0
    out = capsys.readouterr().out
    assert "执行 1 条" in out
    records = store.load_runs()
    assert records and records[-1]["ok"] is True


def test_cli_tick_with_nothing_due_returns_zero(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(tmp_path, "cron", "tick") == 0
    assert "没有到点的任务" in capsys.readouterr().out


def test_cli_run_pause_resume_remove_cycle(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(tmp_path, "cron", "create", "30m", "汇报") == 0
    store = cron.CronStore(tmp_path / "data")
    job_id = store.load_jobs()[0].id
    capsys.readouterr()

    assert _cli(tmp_path, "cron", "run", job_id) == 0
    assert _cli(tmp_path, "cron", "pause", job_id) == 0
    assert store.load_jobs()[0].enabled is False
    assert _cli(tmp_path, "cron", "run", job_id) == 1  # 停用的任务不许触发
    capsys.readouterr()

    assert _cli(tmp_path, "cron", "resume", job_id, "--run-now") == 0
    assert store.load_jobs()[0].enabled is True
    assert _cli(tmp_path, "cron", "remove", job_id) == 0
    assert store.load_jobs() == []
    assert _cli(tmp_path, "cron", "remove", job_id) == 1


def test_cli_pause_all_and_resume_all(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli(tmp_path, "cron", "pause", "all") == 0
    store = cron.CronStore(tmp_path / "data")
    assert store.paused() is True
    assert _cli(tmp_path, "cron", "tick") == 0
    assert "全局暂停" in capsys.readouterr().out
    assert _cli(tmp_path, "cron", "resume", "all") == 0
    assert store.paused() is False


def test_cli_status_and_doctor(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli(tmp_path, "cron", "create", "0 9 * * 1-5", "工作日汇报") == 0
    capsys.readouterr()

    assert _cli(tmp_path, "cron", "status") == 0
    assert "任务：1 条" in capsys.readouterr().out

    assert _cli(tmp_path, "cron", "doctor") == 0
    out = capsys.readouterr().out
    assert "任务表" in out
    assert "调度表达式" in out


def test_cli_doctor_reports_corrupt_jobs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = cron.CronStore(tmp_path / "data")
    store.root.mkdir(parents=True, exist_ok=True)
    store.jobs_path.write_text("{ 坏掉的任务表", encoding="utf-8")
    assert _cli(tmp_path, "cron", "doctor") == 1
    assert "无法解析" in capsys.readouterr().out


def test_cli_incidents_list_and_ack(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli(tmp_path, "cron", "create", "30m", "--no-agent", "--script", "exit /b 9") == 0
    store = cron.CronStore(tmp_path / "data")
    jobs = store.load_jobs()
    jobs[0].next_run_at = cron.to_iso(datetime.now(UTC) - timedelta(minutes=5))
    store.save_jobs(jobs)
    capsys.readouterr()
    assert _cli(tmp_path, "cron", "tick") == 0
    capsys.readouterr()

    assert _cli(tmp_path, "cron", "incidents") == 0
    out = capsys.readouterr().out
    assert "detected" in out

    incident_id = cron.sync_incidents(store)[0].id
    assert _cli(tmp_path, "cron", "incidents", "ack", incident_id) == 0
    assert cron.sync_incidents(store)[0].state == "alerted"
    assert _cli(tmp_path, "cron", "incidents", "ack", "incident-nope") == 1


def test_cli_runs_limit_is_clamped(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli(tmp_path, "cron", "create", "30m", "汇报") == 0
    capsys.readouterr()
    assert _cli(tmp_path, "cron", "runs", "--limit", "9999") == 0
    assert "没有执行记录" in capsys.readouterr().out


def test_cron_command_is_registered_with_all_subcommands() -> None:
    """命令面必须齐全：少一个子命令就等于少一块功能，`--help` 里也看不见。"""
    from app.cli.spec import REGISTRY

    command = REGISTRY.get("cron")
    assert command is not None
    names = {sub.name for sub in command.subcommands}
    base = {
        "create",
        "edit",
        "list",
        "remove",
        "run",
        "runs",
        "tick",
        "status",
        "pause",
        "resume",
        "doctor",
        "incidents",
    }
    # 本轮新增（`app/cli/commands/cron_extra.py`）：平台补投、级联停止、平面事件查询。
    # 判据写成"原有的一条都不能少 + 只允许这四条新增"，而不是"恰好等于"：
    # 新增子命令是功能增长，不该让这份清单假失败；而漏掉原有子命令仍然会失败。
    added_this_round = {"deliver", "pause-all", "resume-all", "events"}
    assert base <= names
    assert names <= base | added_this_round
    for sub in command.subcommands:
        assert sub.help.strip(), f"cron {sub.name} 缺 help"
        assert sub.handler is not None, f"cron {sub.name} 没有真实现"
