"""定时任务命令：`orbit cron {create,edit,list,remove,run,runs,tick,status,pause,resume,doctor,incidents}`。

对齐 Hermes 的 `hermes cron` 命令面（13 个子命令，见
`handoff/hermes-parity/research/_raw/sub/cron__*.txt`）。三条设计取向值得先说清：

1. **命令面抄全，实现分档**。选项一律登记（`--help` 里看得见），但本轮做不到的
   （监控模式 / 轮次连续性 / 供应商与推理档位绑定）**显式报错**而不是静默接受——
   "以为开了监控，其实什么都没发生"是定时任务里最贵的失败模式。
2. **不常驻**。`cron tick` 是给外部调度器（Windows 计划任务 / systemd timer）调用的
   一次性入口：执行到点任务、打印摘要、退出。常驻线程会让"后端没开"变成"任务不跑"。
3. **执行器只有脚本模式真接线**。`--no-agent --script` 走本机 shell，真能跑；
   agent 模式需要一个完整的模型轮次（数据库 + 模型密钥 + 图执行），属后端职责，
   这里如实标成"执行器不可用"，并且**不计入连续失败**——否则一次配置缺项会在三分钟里
   把用户的任务自动停用掉。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime
from typing import Any

from app import cron
from app.cli.context import CliError, Context
from app.cli.spec import REGISTRY, Command, Option

#: 命令面已抄全、但本轮**没有实现**的选项 → 显式报错。
#: 静默接受的后果是"用户以为开了监控/连续性，实际什么都没发生"，比报错糟得多。
_UNSUPPORTED_FLAGS: dict[str, str] = {
    "monitor_script": "--monitor-script（监控模式）",
    "monitor_url": "--monitor-url（监控模式）",
    "continuity": "--continuity（轮次连续性）",
    "no_continuity": "--no-continuity（轮次连续性）",
    "provider": "--provider（供应商绑定）",
    "reasoning_effort": "--reasoning-effort（推理档位）",
}


def _reject_unsupported(args) -> None:
    used = [flag for name, flag in _UNSUPPORTED_FLAGS.items() if getattr(args, name, None)]
    if used:
        raise CliError(
            f"以下选项本轮未实现：{'、'.join(used)}。"
            "命令面已按 Hermes 抄全，实现在 handoff/hermes-parity/ROADMAP.md 里排期；"
            "这里显式报错，是为了不让它看起来已经生效。"
        )


def _store(context: Context) -> cron.CronStore:
    return cron.CronStore(context.data_dir)


def _text(args, name: str) -> str:
    return str(getattr(args, name, "") or "")


def _optional(args, name: str) -> str | None:
    """``None`` = 用户没给这个选项（不改），空串 = 用户显式清空。"""
    value = getattr(args, name, None)
    return None if value is None else str(value)


def _repeat(args) -> int:
    raw = getattr(args, "repeat", None)
    text = "" if raw is None else str(raw).strip()
    if not text:
        return 0
    if not text.isdigit():
        raise CliError(f"--repeat 只接受非负整数：{text!r}（0 = 不限次）")
    return int(text)


# ── 执行器 ──────────────────────────────────────────────────────────────────


def _run_script(job: cron.Job, timeout: int) -> dict[str, Any]:
    """跑 ``--script`` 那条 shell 命令（脚本模式：不走模型）。

    用 ``shell=True`` 是刻意的：``--script`` 收的是一条**命令行**（`df -h | tail -1`
    这类管道也要能用），而不是一个可执行文件路径。cwd 取 ``workdir``，让脚本里的
    相对路径有确定含义。``errors="replace"`` 是 Windows 现实：很多命令输出 GBK，
    严格解码会抛 UnicodeDecodeError，把"任务成功但输出读不出来"变成"任务失败"。
    """
    command = job.script.strip()
    if not command:
        return {"ok": False, "error": "任务没有配置 --script"}
    cwd = job.workdir.strip() or None
    try:
        completed = subprocess.run(
            command,
            shell=True,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"脚本执行超时（{timeout}s）"}
    except OSError as exc:
        return {"ok": False, "error": f"脚本无法启动：{exc}"}
    output = (completed.stdout or "").strip()
    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        return {"ok": False, "output": output, "error": stderr or f"退出码 {completed.returncode}"}
    return {"ok": True, "output": output}


def _default_runner(context: Context) -> cron.Runner:
    """CLI 侧的默认执行器：脚本模式真跑，agent 模式如实报"不可用"。"""

    async def run(job: cron.Job) -> dict[str, Any]:
        if job.no_agent:
            # 子进程是阻塞调用，挪到线程里：这一层是 async 的，不该把事件循环钉住。
            return await asyncio.to_thread(_run_script, job, context.settings.command_timeout)
        return {
            "ok": False,
            "unavailable": True,
            "error": (
                "agent 执行器尚未接线：agent 模式需要完整的模型轮次（数据库 + 模型密钥 + 图执行），"
                "属后端职责。当前 cron 只接了脚本模式（--no-agent --script）。"
            ),
        }

    return run


def _runner(context: Context) -> cron.CronRunner:
    return cron.CronRunner(context.data_dir, _default_runner(context))


# ── create ──────────────────────────────────────────────────────────────────


def run_create(context: Context, args) -> int:
    _reject_unsupported(args)
    store = _store(context)
    try:
        job = cron.create_job(
            store,
            schedule=_text(args, "schedule"),
            prompt=_text(args, "prompt"),
            name=_text(args, "name"),
            deliver=_text(args, "deliver"),
            repeat=_repeat(args),
            skills=list(getattr(args, "skill", None) or []),
            script=_text(args, "script"),
            no_agent=bool(getattr(args, "no_agent", False)),
            workdir=_text(args, "workdir"),
            model=_text(args, "model"),
        )
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    mode = "脚本（不走模型）" if job.no_agent else "agent 提示词"
    context.out(f"已创建 {job.id}：{job.display_name}")
    context.out(f"  调度：{job.schedule}（下次触发 {job.next_run_at}）")
    context.out(f"  模式：{mode}")
    context.out(f"  投递：{job.deliver}")
    if job.repeat:
        context.out(f"  次数上限：{job.repeat}")
    context.out()
    context.out("下一轮执行：orbit cron tick（外部调度器按分钟调用）")
    context.emit({"ok": True, "job": job.to_dict()})
    return 0


# ── edit ────────────────────────────────────────────────────────────────────


def run_edit(context: Context, args) -> int:
    _reject_unsupported(args)
    if getattr(args, "no_agent", False) and getattr(args, "agent", False):
        raise CliError("--no-agent 与 --agent 互斥：一个要脚本模式，一个要回到 agent 模式。")
    no_agent: bool | None = None
    if getattr(args, "no_agent", False):
        no_agent = True
    if getattr(args, "agent", False):
        no_agent = False
    store = _store(context)
    try:
        job = cron.update_job(
            store,
            _text(args, "job_id"),
            schedule=_optional(args, "schedule"),
            prompt=_optional(args, "prompt"),
            name=_optional(args, "name"),
            deliver=_optional(args, "deliver"),
            repeat=_repeat(args) if getattr(args, "repeat", None) is not None else None,
            skills=list(getattr(args, "skill", None) or []) or None,
            add_skills=list(getattr(args, "add_skill", None) or []),
            remove_skills=list(getattr(args, "remove_skill", None) or []),
            clear_skills=bool(getattr(args, "clear_skills", False)),
            script=_optional(args, "script"),
            no_agent=no_agent,
            workdir=_optional(args, "workdir"),
            model=_optional(args, "model"),
        )
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    if job is None:
        raise CliError(
            f"没有这个任务：{_text(args, 'job_id')}（`orbit cron list --all` 看全部）"
        )
    context.out(f"已更新 {job.id}：{job.display_name}")
    context.out(f"  调度：{job.schedule}（下次触发 {job.next_run_at or '未排期'}）")
    context.out(f"  技能：{'、'.join(job.skills) if job.skills else '（无）'}")
    context.emit({"ok": True, "job": job.to_dict()})
    return 0


# ── list ────────────────────────────────────────────────────────────────────


def _state_text(job: cron.Job, *, paused: bool) -> str:
    if job.exhausted:
        return "已用完"
    if not job.enabled:
        return "已停用"
    return "全局暂停" if paused else "启用"


def _repeat_text(job: cron.Job) -> str:
    if not job.repeat:
        return "不限"
    return f"{job.run_count}/{job.repeat}"


def run_list(context: Context, args) -> int:
    store = _store(context)
    paused = store.paused()
    jobs = store.load_jobs()
    show_all = bool(getattr(args, "all", False))
    shown = [job for job in jobs if job.enabled or show_all]
    if shown:
        context.table(
            ("ID", "名称", "调度", "状态", "次数", "下次触发", "上次执行", "连续失败"),
            [
                (
                    job.id,
                    job.display_name,
                    job.schedule,
                    _state_text(job, paused=paused),
                    _repeat_text(job),
                    job.next_run_at or "-",
                    job.last_run_at or "-",
                    str(job.failures) if job.failures else "",
                )
                for job in shown
            ],
        )
    else:
        hint = "没有定时任务。" if show_all else "没有启用的定时任务（`--all` 可看到已停用的）。"
        context.out(hint)
    if paused:
        context.out()
        context.out("注意：全局暂停哨兵存在，tick 不会执行任何任务（`orbit cron resume all` 解除）。")
    context.emit({"ok": True, "paused": paused, "jobs": [job.to_dict() for job in shown]})
    return 0


# ── remove / run ────────────────────────────────────────────────────────────


def run_remove(context: Context, args) -> int:
    job_id = _text(args, "job_id")
    if not cron.remove_job(_store(context), job_id):
        raise CliError(f"没有这个任务：{job_id}（`orbit cron list --all` 看全部）")
    context.out(f"已删除 {job_id}")
    context.emit({"ok": True, "removed": job_id})
    return 0


def run_run(context: Context, args) -> int:
    """把任务的 ``next_run_at`` 拨到"现在"，交给下一轮 tick 执行。

    照 Hermes `cron run` 的语义（"Run a job on the next scheduler tick"）：不在命令里
    直接跑，是因为命令进程退出后任务的长输出没人收；交给 tick 才能走同一套记账与投递。
    """
    job_id = _text(args, "job_id")
    store = _store(context)
    jobs = store.load_jobs()
    job = next((item for item in jobs if item.id == job_id), None)
    if job is None:
        raise CliError(f"没有这个任务：{job_id}（`orbit cron list --all` 看全部）")
    if not job.enabled:
        raise CliError(f"{job_id} 已停用，先 `orbit cron resume {job_id}` 再触发。")
    job.next_run_at = cron.to_iso(datetime.now(UTC))
    store.save_jobs(jobs)
    context.out(f"{job_id} 已排到下一次 tick（{job.next_run_at}）。")
    context.emit({"ok": True, "job_id": job_id, "next_run_at": job.next_run_at})
    return 0


# ── runs ────────────────────────────────────────────────────────────────────


def run_runs(context: Context, args) -> int:
    job_id = _text(args, "job_id")
    raw_limit = getattr(args, "limit", None)
    try:
        limit = int(raw_limit) if raw_limit is not None else 20
    except (TypeError, ValueError) as exc:
        raise CliError(f"--limit 只接受整数：{raw_limit!r}") from exc
    # 上限 500 是刻意的：runs.jsonl 是流水账，一次把几千行刷到终端没人读得下去。
    limit = max(1, min(500, limit))
    store = _store(context)
    records = store.load_runs()
    if job_id:
        records = [item for item in records if str(item.get("job_id") or "") == job_id]
    records = records[-limit:]
    if not records:
        context.out("没有执行记录。" + ("" if not job_id else f"（任务 {job_id} 尚未跑过）"))
        context.emit({"ok": True, "runs": []})
        return 0
    context.table(
        ("时间", "任务", "结果", "耗时(ms)", "投递", "备注"),
        [
            (
                str(item.get("ts") or ""),
                str(item.get("name") or item.get("job_id") or ""),
                "成功" if item.get("ok") else ("执行器不可用" if item.get("unavailable") else "失败"),
                str(item.get("duration_ms") or 0),
                str(item.get("delivered") or "-"),
                str(item.get("error") or item.get("note") or "")[:60],
            )
            for item in records
        ],
    )
    context.emit({"ok": True, "runs": records})
    return 0


# ── tick ────────────────────────────────────────────────────────────────────


def run_tick(context: Context, args) -> int:
    """执行到点任务后打印摘要，**不常驻**。

    退出码恒为 0（除非 tick 自身崩了）：外部调度器只看退出码，把"某个任务失败"翻译成
    非零会让计划任务面板上挂一堆红叉，而任务失败本身已经记进 runs.jsonl 了。
    """
    runner = _runner(context)
    if runner.store.paused():
        context.out("全局暂停中，本轮不执行任何任务（`orbit cron resume all` 解除）。")
        context.emit({"ok": True, "paused": True, "ran": []})
        return 0
    results: list[cron.RunResult] = context.run(runner.tick())
    if not results:
        context.out("没有到点的任务。")
        context.emit({"ok": True, "paused": False, "ran": []})
        return 0
    context.table(
        ("任务", "结果", "耗时(ms)", "投递", "备注"),
        [
            (
                item.name,
                "成功" if item.ok else ("执行器不可用" if item.unavailable else "失败"),
                str(item.duration_ms),
                item.delivered or "-",
                (item.note or item.error or "")[:60],
            )
            for item in results
        ],
    )
    failures = [item for item in results if not item.ok and not item.unavailable]
    unavailable = [item for item in results if item.unavailable]
    disabled = [item for item in results if item.disabled]
    context.out()
    context.out(f"执行 {len(results)} 条，失败 {len(failures)} 条。")
    if unavailable:
        context.out(
            f"{len(unavailable)} 条因执行器未接线而未执行（agent 模式需要后端执行器，"
            "不计入连续失败）。"
        )
    if disabled:
        context.out(f"已自动停用：{'、'.join(item.job_id for item in disabled)}")
    context.emit(
        {
            "ok": True,
            "paused": False,
            "ran": [item.as_record() for item in results],
            "failures": len(failures),
            "disabled": [item.job_id for item in disabled],
        }
    )
    return 0


# ── status / doctor ─────────────────────────────────────────────────────────


def run_status(context: Context, args) -> int:
    store = _store(context)
    paused = store.paused()
    jobs = store.load_jobs()
    moment = datetime.now(UTC)
    enabled = [job for job in jobs if job.enabled]
    due = [job for job in enabled if cron.is_due(job, moment)]
    upcoming = sorted(
        ((cron.from_iso(job.next_run_at), job) for job in enabled if cron.from_iso(job.next_run_at)),
        key=lambda item: item[0] or moment,
    )
    runs = store.load_runs()
    context.out(f"调度状态：{'全局暂停（哨兵文件存在）' if paused else '就绪'}")
    context.out(f"任务：{len(jobs)} 条（启用 {len(enabled)}）")
    context.out(f"到点待跑：{len(due)} 条")
    if upcoming and upcoming[0][0] is not None:
        context.out(f"下次触发：{cron.to_iso(upcoming[0][0])}（{upcoming[0][1].display_name}）")
    context.out(f"执行流水：{len(runs)} 条")
    if runs:
        last = runs[-1]
        context.out(
            f"最近一次：{last.get('ts')} · {last.get('name') or last.get('job_id')} · "
            f"{'成功' if last.get('ok') else '失败'}"
        )
    context.out()
    context.out(
        "本实现不常驻：请让外部调度器按分钟调用 `orbit cron tick`"
        "（Windows 计划任务 / systemd timer）。"
    )
    context.emit(
        {
            "ok": True,
            "paused": paused,
            "jobs": len(jobs),
            "enabled": len(enabled),
            "due": len(due),
            "runs": len(runs),
            "next_run_at": cron.to_iso(upcoming[0][0]) if upcoming and upcoming[0][0] else "",
        }
    )
    return 0


PASS = "通过"
WARN = "注意"
FAIL = "失败"


def run_doctor(context: Context, args) -> int:
    """体检：任务表能不能解析、每条调度能不能算出下一次、流水多少行、几条在失败。"""
    store = _store(context)
    checks: list[tuple[str, str, str]] = []

    # 1) 任务表可解析。用**原始文本**判，因为 load_jobs 故意把坏文件当空表。
    if not store.jobs_path.exists():
        checks.append(("任务表", WARN, f"还没有 {store.jobs_path}（用 `orbit cron create` 建第一条）"))
    else:
        try:
            payload = json.loads(store.jobs_path.read_text(encoding="utf-8"))
            count = len(payload.get("jobs") or []) if isinstance(payload, dict) else len(payload)
            checks.append(("任务表", PASS, f"{store.jobs_path} 可解析，{count} 条任务"))
        except (OSError, ValueError) as exc:
            checks.append(("任务表", FAIL, f"{store.jobs_path} 无法解析：{exc}"))

    jobs = store.load_jobs()

    # 2) 每条任务的调度表达式都能算出下一次触发。
    broken: list[str] = []
    for job in jobs:
        try:
            cron.next_run(job.schedule, datetime.now(UTC))
        except ValueError as exc:
            broken.append(f"{job.id}（{job.schedule}）：{exc}")
    if broken:
        checks.append(("调度表达式", FAIL, "；".join(broken)))
    else:
        checks.append(("调度表达式", PASS, f"{len(jobs)} 条都能算出下一次触发"))

    # 3) 执行流水行数。
    runs = store.load_runs()
    checks.append(("执行流水", PASS if runs else WARN, f"{store.runs_path} 共 {len(runs)} 行"))

    # 4) 有连续失败的任务。
    failing = [job for job in jobs if job.failures]
    if failing:
        detail = "、".join(f"{job.id}（连续 {job.failures} 次）" for job in failing)
        checks.append(("失败任务", WARN, f"{len(failing)} 条在失败：{detail}"))
    else:
        checks.append(("失败任务", PASS, "没有连续失败的任务"))

    # 5) 全局暂停哨兵。
    if store.paused():
        checks.append(("全局暂停", WARN, f"{store.paused_path} 存在，tick 不会执行任何任务"))
    else:
        checks.append(("全局暂停", PASS, "未暂停"))

    # 6) agent 模式任务需要后端执行器（本层没有）。
    agent_jobs = [job for job in jobs if not job.no_agent]
    if agent_jobs:
        checks.append(
            ("执行器", WARN, f"{len(agent_jobs)} 条 agent 模式任务需要后端执行器（尚未接线）")
        )
    else:
        checks.append(("执行器", PASS, "全部是脚本模式，CLI 可直接执行"))

    context.out("cron 自检")
    context.out()
    context.table(("检查项", "结果", "详情"), checks)
    failures = [name for name, status, _ in checks if status == FAIL]
    warnings = [name for name, status, _ in checks if status == WARN]
    context.out()
    if failures:
        context.out(f"失败 {len(failures)} 项：{'、'.join(failures)}")
    elif warnings:
        context.out(f"全部通过，{len(warnings)} 项需注意：{'、'.join(warnings)}")
    else:
        context.out("全部通过。")
    context.emit(
        {
            "ok": not failures,
            "checks": [{"name": n, "status": s, "detail": d} for n, s, d in checks],
            "failures": failures,
            "warnings": warnings,
            "runs": len(runs),
            "failing_jobs": [job.id for job in failing],
        }
    )
    return 1 if failures else 0


# ── pause / resume ──────────────────────────────────────────────────────────


def _is_all(job_id: str) -> bool:
    return job_id.strip().lower() in {"all", "*"}


def run_pause(context: Context, args) -> int:
    job_id = _text(args, "job_id")
    runner = _runner(context)
    if _is_all(job_id):
        runner.pause_all(reason="orbit cron pause all")
        context.out(f"已全局暂停：{runner.store.paused_path}（tick 将不执行任何任务）")
        context.emit({"ok": True, "paused_all": True})
        return 0
    if not runner.pause(job_id):
        raise CliError(f"没有这个任务：{job_id}（`orbit cron list --all` 看全部）")
    context.out(f"已暂停 {job_id}")
    context.emit({"ok": True, "paused": job_id})
    return 0


def run_resume(context: Context, args) -> int:
    job_id = _text(args, "job_id")
    runner = _runner(context)
    if _is_all(job_id):
        runner.resume_all()
        context.out("已解除全局暂停。")
        context.emit({"ok": True, "resumed_all": True})
        return 0
    at_raw = _text(args, "at")
    at: datetime | None = None
    if at_raw:
        at = cron.from_iso(at_raw)
        if at is None:
            raise CliError(f"--at 要 ISO-8601 时间：{at_raw!r}（例：2026-01-01T09:00:00+08:00）")
    job = runner.resume(job_id, at=at, run_now=bool(getattr(args, "run_now", False)))
    if job is None:
        raise CliError(f"没有这个任务：{job_id}（`orbit cron list --all` 看全部）")
    context.out(f"已恢复 {job.id}（下次触发 {job.next_run_at or '未排期'}）")
    context.emit({"ok": True, "job": job.to_dict()})
    return 0


# ── incidents ───────────────────────────────────────────────────────────────


def run_incidents(context: Context, args) -> int:
    store = _store(context)
    action = str(getattr(args, "action", None) or "list")
    if action == "ack":
        incident_id = _text(args, "incident_id")
        if not incident_id:
            raise CliError("用法：orbit cron incidents ack <事故 ID>")
        incident = cron.acknowledge_incident(store, incident_id)
        if incident is None:
            raise CliError(f"没有这条事故：{incident_id}（`orbit cron incidents` 看全部）")
        context.out(f"已确认 {incident.id}（状态：{incident.state}）")
        context.emit({"ok": True, "incident": incident.as_dict()})
        return 0

    state = getattr(args, "state", None)
    incidents = cron.sync_incidents(store)
    if state:
        incidents = [item for item in incidents if item.state == state]
    if not incidents:
        context.out("没有事故记录。" + ("" if not state else f"（状态 {state} 下没有）"))
        context.emit({"ok": True, "incidents": []})
        return 0
    context.table(
        ("事故 ID", "任务", "状态", "失败次数", "最近失败", "错误"),
        [
            (
                item.id,
                item.job_name,
                item.state,
                str(item.failures),
                item.last_failure_at,
                item.last_error[:50],
            )
            for item in incidents
        ],
    )
    context.out()
    context.out("确认某条：orbit cron incidents ack <事故 ID>（状态 detected → alerted）")
    context.emit({"ok": True, "incidents": [item.as_dict() for item in incidents]})
    return 0


# ── 注册 ────────────────────────────────────────────────────────────────────

_SKILL_OPTION = Option(("--skill",), "附加一个技能（可重复）", action="append", metavar="SKILLS")


def _cron_command() -> Command:
    return Command(
        name="cron",
        help="定时任务管理（不常驻，由外部调度器调用 `cron tick`）",
        group="run",
        parity="P0",
        state="partial",
        needs_settings=False,
        subcommands=[
            Command(
                name="create",
                help="创建定时任务",
                handler=run_create,
                group="run",
                state="done",
                aliases=("add",),
                needs_settings=True,
                options=[
                    Option(("schedule",), "调度：'30m'、'every 2h'、'0 9 * * *'", nargs="?"),
                    Option(("prompt",), "自包含的任务提示词（脚本模式可省）", nargs="?"),
                    Option(("--name",), "任务名（便于人读）", metavar="NAME"),
                    Option(
                        ("--deliver",),
                        "投递目标：none（默认）或 file:<路径>；平台投递尚未实现",
                        metavar="DELIVER",
                    ),
                    Option(("--repeat",), "次数上限（0 = 不限次）", metavar="REPEAT"),
                    _SKILL_OPTION,
                    Option(
                        ("--script",),
                        "脚本模式要执行的 shell 命令；--no-agent 时必填",
                        metavar="SCRIPT",
                    ),
                    Option(
                        ("--no-agent",),
                        "跳过模型：脚本就是任务，输出直接投递（看门狗模式）",
                        action="store_true",
                    ),
                    Option(("--monitor-script",), "监控模式源脚本（本轮未实现）", metavar="MONITOR_SCRIPT"),
                    Option(("--monitor-url",), "监控模式源 URL（本轮未实现）", metavar="MONITOR_URL"),
                    Option(("--workdir",), "任务运行的绝对工作目录", metavar="WORKDIR"),
                    Option(("--model",), "把这个任务钉到指定模型", metavar="MODEL"),
                    Option(("--provider",), "与 --model 搭配的供应商（本轮未实现）", metavar="MODEL_PROVIDER"),
                    Option(
                        ("--reasoning-effort",),
                        "推理档位（本轮未实现）",
                        metavar="REASONING_EFFORT",
                    ),
                    Option(
                        ("--continuity",),
                        "每轮带上自己上一次的输出（本轮未实现）",
                        action="store_true",
                    ),
                ],
            ),
            Command(
                name="edit",
                help="编辑已有定时任务",
                handler=run_edit,
                group="run",
                state="done",
                needs_settings=True,
                options=[
                    Option(("job_id",), "要编辑的任务 ID", nargs="?"),
                    Option(("--schedule",), "新调度表达式", metavar="SCHEDULE"),
                    Option(("--prompt",), "新提示词", metavar="PROMPT"),
                    Option(("--name",), "新任务名", metavar="NAME"),
                    Option(("--deliver",), "新投递目标（none / file:<路径>）", metavar="DELIVER"),
                    Option(("--repeat",), "新次数上限（0 = 不限次）", metavar="REPEAT"),
                    Option(("--skill",), "用这组技能替换原有技能（可重复）", action="append", metavar="SKILLS"),
                    Option(("--add-skill",), "追加一个技能（可重复）", action="append", metavar="ADD_SKILLS"),
                    Option(("--remove-skill",), "移除一个技能（可重复）", action="append", metavar="REMOVE_SKILLS"),
                    Option(("--clear-skills",), "清空全部技能", action="store_true"),
                    Option(("--script",), "新脚本命令；传空串清空", metavar="SCRIPT"),
                    Option(("--no-agent",), "启用脚本模式（需要脚本）", action="store_true"),
                    Option(("--agent",), "关闭脚本模式，回到 agent 驱动", action="store_true"),
                    Option(("--continuity",), "打开轮次连续性（本轮未实现）", action="store_true"),
                    Option(("--no-continuity",), "关闭轮次连续性（本轮未实现）", action="store_true"),
                    Option(("--monitor-script",), "设置监控源脚本（本轮未实现）", metavar="MONITOR_SCRIPT"),
                    Option(("--monitor-url",), "设置监控源 URL（本轮未实现）", metavar="MONITOR_URL"),
                    Option(("--workdir",), "新工作目录；传空串清空", metavar="WORKDIR"),
                    Option(("--model",), "新模型绑定；传空串清空", metavar="MODEL"),
                    Option(("--provider",), "新供应商（本轮未实现）", metavar="MODEL_PROVIDER"),
                    Option(
                        ("--reasoning-effort",),
                        "新推理档位（本轮未实现）",
                        metavar="REASONING_EFFORT",
                    ),
                ],
            ),
            Command(
                name="list",
                help="列出定时任务",
                handler=run_list,
                group="run",
                state="done",
                needs_settings=False,
                options=[Option(("--all",), "包含已停用的任务", action="store_true")],
            ),
            Command(
                name="remove",
                help="删除定时任务",
                handler=run_remove,
                group="run",
                state="done",
                aliases=("rm", "delete"),
                needs_settings=True,
                options=[Option(("job_id",), "要删除的任务 ID", nargs="?")],
            ),
            Command(
                name="run",
                help="把任务排到下一次 tick 执行",
                handler=run_run,
                group="run",
                state="done",
                needs_settings=True,
                options=[
                    Option(("job_id",), "要触发的任务 ID", nargs="?"),
                    Option(
                        ("--accept-hooks",),
                        "自动批准未见过的手工 hook（Orbit 暂无 hook 体系，此开关为空操作）",
                        action="store_true",
                    ),
                ],
            ),
            Command(
                name="runs",
                help="查看持久化的执行记录",
                handler=run_runs,
                group="run",
                state="done",
                aliases=("history",),
                needs_settings=False,
                options=[
                    Option(("job_id",), "只看某个任务", nargs="?"),
                    Option(("--limit",), "显示行数（1-500，默认 20）", default=20, metavar="LIMIT"),
                ],
            ),
            Command(
                name="tick",
                help="执行到点任务后退出（给外部调度器调用）",
                handler=run_tick,
                group="run",
                state="done",
                needs_settings=True,
                options=[
                    Option(
                        ("--accept-hooks",),
                        "自动批准未见过的手工 hook（Orbit 暂无 hook 体系，此开关为空操作）",
                        action="store_true",
                    ),
                ],
            ),
            Command(
                name="status",
                help="查看调度器状态（暂停、到点数、流水行数）",
                handler=run_status,
                group="run",
                state="done",
                needs_settings=False,
            ),
            Command(
                name="pause",
                help="暂停一个任务（job_id 给 all 则全局暂停）",
                handler=run_pause,
                group="run",
                state="done",
                needs_settings=True,
                options=[Option(("job_id",), "任务 ID，或 all = 全局紧急停止", nargs="?")],
            ),
            Command(
                name="resume",
                help="恢复任务（job_id 给 all 则解除全局暂停）",
                handler=run_resume,
                group="run",
                state="done",
                needs_settings=True,
                options=[
                    Option(("job_id",), "任务 ID，或 all = 解除全局暂停", nargs="?"),
                    Option(("--at",), "按 ISO-8601 时间重新排期", metavar="RUN_AT"),
                    Option(("--run-now",), "排到立即执行（下一次 tick）", action="store_true"),
                ],
            ),
            Command(
                name="doctor",
                help="体检定时任务（任务表 / 调度 / 流水 / 失败任务）",
                handler=run_doctor,
                group="run",
                state="done",
                needs_settings=False,
            ),
            Command(
                name="incidents",
                help="列出或确认 cron 失败事故",
                handler=run_incidents,
                group="run",
                state="done",
                needs_settings=True,
                options=[
                    Option(("action",), "动作：list（默认）/ ack", choices=("list", "ack"), nargs="?"),
                    Option(("incident_id",), "要确认的事故 ID（ack 用）", nargs="?"),
                    Option(
                        ("--state",),
                        "按生命周期状态过滤",
                        choices=cron.INCIDENT_STATES,
                        metavar="STATE",
                    ),
                ],
            ),
        ],
    )


def _install(command: Command) -> None:
    """登记本模块的命令；若声明表已占了同名占位条目，**就地接管**。

    为什么需要接管：`hermes_parity.py` 里已经有一条 `cron` 的占位声明（13 个子命令、
    全部 handler 是"尚未实现"）。按仓库约定，真实现要在 `hermes_parity` **之前**导入，
    `register_specs` 就会跳过同名条目。但导入顺序由 `commands/__init__.py` 决定，而
    测试/其它入口可能先导入了声明表——那时 `REGISTRY.add` 会因重名直接抛错，整个命令面
    都编译不出来。就地覆盖字段既能兼容两种导入顺序，也保证"只有一条 cron"。
    """
    existing = REGISTRY.get(command.name)
    if existing is None:
        REGISTRY.add(command)
        return
    existing.help = command.help
    existing.handler = command.handler
    existing.options = command.options
    existing.subcommands = command.subcommands
    existing.group = command.group
    existing.hermes = command.hermes or existing.hermes
    existing.parity = command.parity
    existing.state = command.state
    existing.needs_settings = command.needs_settings
    existing.needs_store = command.needs_store


_install(_cron_command())
