"""Curator 层测试：状态机 / 锚点 / 可恢复归档 / ledger / `.usage.json` / CLI。

时间全部注入（``Clock``），不依赖 ``sleep`` 也不依赖真实时钟——"闲置 100 天"这种
判定只能靠可控制的 now 来验证，靠真实时间跑就变成"要么测不到、要么跑两天"。
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime, timedelta

import pytest

from app.cli.commands import curator as curator_cli  # noqa: F401  （导入即接线）
from app.cli.main import main
from app.curator import (
    ACTOR_CURATOR,
    ARCHIVE_DAYS,
    ARCHIVE_MIN_SAMPLES,
    ARCHIVE_MIN_SUCCESS_RATE,
    LEDGER_FIELDS,
    STALE_FAILURES,
    STATE_ACTIVE,
    STATE_ARCHIVED,
    STATE_STALE,
    STATE_UNREVIEWED,
    USAGE_ANCHOR_FIELDS,
    USAGE_FIELDS,
    Curator,
    CuratorError,
    UsageRecord,
    make_ledger_id,
)

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class Clock:
    """可拨动的时钟。``curator.now`` 依赖注入，测试里想走多快就走多快。"""

    def __init__(self, moment: datetime = NOW) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, days: float) -> None:
        self.moment = self.moment + timedelta(days=days)


def write_skill(root, name: str = "demo", *, body: str = "正文\n"):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 演示技能。\n---\n\n{body}", encoding="utf-8"
    )
    return directory


def make_curator(tmp_path, *, clock: Clock | None = None, **kwargs) -> Curator:
    return Curator(
        tmp_path / "data",
        cwd=tmp_path / "proj",
        now=clock or Clock(),
        **kwargs,
    )


def seed_usage(curator: Curator, skill_dir, record: UsageRecord) -> None:
    curator.write_usage(skill_dir, record)


def used_once(moment: datetime) -> UsageRecord:
    record = UsageRecord(created_at=moment.isoformat())
    record.record_use(moment)
    return record


# ── 1. 状态机四种迁移 ──────────────────────────────────────────────────────


def test_unreviewed_becomes_active_on_first_use(tmp_path):
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    seed_usage(curator, skill_dir, used_once(NOW))

    transitions = curator.run(dry_run=True)
    assert len(transitions) == 1
    assert transitions[0]["name"] == "demo"
    assert transitions[0]["from"] == STATE_UNREVIEWED
    assert transitions[0]["to"] == STATE_ACTIVE


def test_unreviewed_without_use_stays_put(tmp_path):
    curator = make_curator(tmp_path)
    write_skill(curator.index.user_root)
    assert curator.run(dry_run=True) == []
    assert curator.state_of("demo") == STATE_UNREVIEWED


def test_active_becomes_stale_after_consecutive_failures(tmp_path):
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    record = used_once(NOW)
    for _ in range(STALE_FAILURES):
        record.record_outcome(success=False, now=NOW)
    seed_usage(curator, skill_dir, record)
    curator.set_state("demo", STATE_ACTIVE)

    transitions = curator.run(dry_run=True)
    assert [(item["from"], item["to"]) for item in transitions] == [
        (STATE_ACTIVE, STATE_STALE)
    ]
    assert str(STALE_FAILURES) in transitions[0]["reason"]


def test_stale_returns_to_active_when_failures_cleared(tmp_path):
    """一次成功就把 ``consecutive_failures`` 清零——这是 stale 唯一的回退路径。"""
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    record = used_once(NOW)
    record.record_outcome(success=False, now=NOW)
    record.record_outcome(success=False, now=NOW)
    record.record_outcome(success=True, now=NOW)
    assert record.consecutive_failures == 0
    seed_usage(curator, skill_dir, record)
    curator.set_state("demo", STATE_STALE)

    transitions = curator.run(dry_run=True)
    assert [(item["from"], item["to"]) for item in transitions] == [
        (STATE_STALE, STATE_ACTIVE)
    ]


def test_active_archived_after_idle_beyond_archive_days(tmp_path):
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    seed_usage(curator, skill_dir, used_once(NOW - timedelta(days=ARCHIVE_DAYS + 10)))
    curator.set_state("demo", STATE_ACTIVE)

    transitions = curator.run(dry_run=True)
    assert [(item["from"], item["to"]) for item in transitions] == [
        (STATE_ACTIVE, STATE_ARCHIVED)
    ]
    assert "闲置" in transitions[0]["reason"]


def test_small_sample_failures_are_stale_not_archived(tmp_path):
    """2 次连续失败 = 短期信号（stale），不能被成功率判据抢先归档。

    这条是 `ARCHIVE_MIN_SAMPLES` 的存在理由：没有它，成功率 0.0 会让
    "刚失败两次"直接归档，而归档是不可自愈的。
    """
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    record = used_once(NOW)
    for _ in range(STALE_FAILURES):
        record.record_outcome(success=False, now=NOW)
    assert record.outcome_total < ARCHIVE_MIN_SAMPLES
    seed_usage(curator, skill_dir, record)
    curator.set_state("demo", STATE_ACTIVE)

    transitions = curator.run(dry_run=True)
    assert transitions[0]["to"] == STATE_STALE


def test_low_success_rate_archives_active_skill(tmp_path):
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    record = used_once(NOW)
    for _ in range(3):
        record.record_outcome(success=False, now=NOW)
    record.record_outcome(success=True, now=NOW)  # 1/4 = 0.25 < 0.5
    assert record.success_rate < ARCHIVE_MIN_SUCCESS_RATE
    seed_usage(curator, skill_dir, record)
    curator.set_state("demo", STATE_ACTIVE)

    transitions = curator.run(dry_run=True)
    assert transitions[0]["to"] == STATE_ARCHIVED
    assert "成功率" in transitions[0]["reason"]


# ── 2. 两条硬规则：use_count == 0 不归档 / 锚点不含 mtime ─────────────────


def test_use_count_zero_is_never_archived_for_idleness(tmp_path):
    """「从未用过」≠「陈旧」：没被触发过的技能，闲置天数与老技能一模一样。"""
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    record = UsageRecord(created_at=(NOW - timedelta(days=500)).isoformat())
    record.record_view(NOW - timedelta(days=400))
    record.record_view(NOW - timedelta(days=300))
    assert record.use_count == 0
    seed_usage(curator, skill_dir, record)
    curator.set_state("demo", STATE_ACTIVE)

    assert curator.run(dry_run=True) == []
    assert curator.state_of("demo") == STATE_ACTIVE


def test_anchor_ignores_file_mtime(tmp_path):
    """把 SKILL.md 的 mtime 改成"刚刚"，但 last_used 很旧 → 仍然归档。"""
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    seed_usage(curator, skill_dir, used_once(NOW - timedelta(days=ARCHIVE_DAYS + 30)))
    curator.set_state("demo", STATE_ACTIVE)

    # 编辑器/同步工具会随手改 mtime，它绝不能影响"多久没用"的判定
    stamp = time.time()
    os.utime(skill_dir / "SKILL.md", (stamp, stamp))

    record = curator.read_usage(skill_dir)
    assert record.idle_days(NOW) > ARCHIVE_DAYS

    transitions = curator.run(dry_run=True)
    assert transitions and transitions[0]["to"] == STATE_ARCHIVED


def test_anchor_falls_back_to_created_at(tmp_path):
    record = UsageRecord(created_at=(NOW - timedelta(days=120)).isoformat())
    assert record.latest_activity_at() is None
    assert record.anchor() is not None
    assert record.idle_days(NOW) > 100


def test_pinned_skill_is_exempt_from_every_transition(tmp_path):
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    record = used_once(NOW - timedelta(days=400))
    record.pinned = True
    seed_usage(curator, skill_dir, record)
    curator.set_state("demo", STATE_ACTIVE)

    assert curator.run(dry_run=True) == []


# ── 3. dry_run 不落盘 / 真跑才落盘 ────────────────────────────────────────


def test_dry_run_writes_nothing(tmp_path):
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    seed_usage(curator, skill_dir, used_once(NOW - timedelta(days=200)))
    curator.set_state("demo", STATE_ACTIVE)

    transitions = curator.run(dry_run=True)
    assert transitions
    assert (skill_dir / "SKILL.md").is_file()  # 技能还在原位
    assert not (curator.archive_root / "demo").exists()
    assert curator.state_of("demo") == STATE_ACTIVE
    assert curator.ledger_entries() == []


def test_real_run_moves_the_directory_and_persists_state(tmp_path):
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    seed_usage(curator, skill_dir, used_once(NOW - timedelta(days=200)))
    curator.set_state("demo", STATE_ACTIVE)

    transitions = curator.run(dry_run=False)
    assert transitions[0]["to"] == STATE_ARCHIVED
    assert not skill_dir.exists()
    assert (curator.archive_root / "demo" / "SKILL.md").is_file()
    assert curator.state_of("demo") == STATE_ARCHIVED
    assert curator.state_path.is_file()
    assert len(curator.ledger_entries()) == 1


def test_real_run_records_non_archiving_transitions(tmp_path):
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    seed_usage(curator, skill_dir, used_once(NOW))

    curator.run(dry_run=False)
    assert curator.state_of("demo") == STATE_ACTIVE
    # 状态迁移不搬目录，所以账本为空（账本只记"动过磁盘"的变更）
    assert curator.ledger_entries() == []


# ── 4. ledger：8 字段 / 内容寻址 id / 回滚 ─────────────────────────────────


def test_ledger_entry_has_exactly_eight_fields(tmp_path):
    curator = make_curator(tmp_path)
    write_skill(curator.index.user_root)
    curator.archive("demo")

    lines = curator.ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert set(entry) == set(LEDGER_FIELDS)
    assert len(LEDGER_FIELDS) == 8
    assert entry["actor"] == "user"
    assert entry["action"] == "archive"
    assert entry["skill"] == "demo"
    assert entry["evidence"]["reason"] == "手动归档"
    # before/after 是 {path, sha256} 清单
    assert entry["before"][0]["path"].endswith("SKILL.md")
    assert len(entry["before"][0]["sha256"]) == 64
    assert entry["after"][0]["path"].endswith("SKILL.md")


def test_ledger_id_is_content_addressed():
    """同样的 (动作, 技能, 时间, 前后状态) → 同一个 id；任一不同 → 不同 id。"""
    before = [{"path": "a/SKILL.md", "sha256": "1" * 64}]
    after = [{"path": "b/SKILL.md", "sha256": "1" * 64}]
    first = make_ledger_id("archive", "demo", "2026-01-01T00:00:00+00:00", before, after)
    second = make_ledger_id("archive", "demo", "2026-01-01T00:00:00+00:00", before, after)
    assert first == second
    assert len(first) == 16
    other = make_ledger_id("archive", "demo", "2026-01-02T00:00:00+00:00", before, after)
    assert other != first


def test_rollback_undoes_an_archive(tmp_path):
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    entry_id = curator.archive("demo")
    assert not skill_dir.exists()

    result = curator.rollback(entry_id)
    assert result["action"] == "archive"
    assert (skill_dir / "SKILL.md").is_file()
    assert not (curator.archive_root / "demo").exists()
    assert curator.state_of("demo") == STATE_ACTIVE
    # 回滚本身也进账本（append-only，不修改历史条目）
    assert len(curator.ledger_entries()) == 2


def test_rollback_rejects_non_reversible_action(tmp_path):
    curator = make_curator(tmp_path)
    write_skill(curator.index.user_root)
    entry_id = curator.pin("demo")
    with pytest.raises(CuratorError):
        curator.rollback(entry_id)


def test_rollback_unknown_entry(tmp_path):
    curator = make_curator(tmp_path)
    with pytest.raises(CuratorError):
        curator.rollback("deadbeefdeadbeef")


# ── 5. 归档 / 恢复 ────────────────────────────────────────────────────────


def test_archive_then_restore_round_trip(tmp_path):
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    curator.archive("demo")
    assert (curator.archive_root / "demo" / "SKILL.md").is_file()
    assert curator.state_of("demo") == STATE_ARCHIVED

    curator.restore("demo")
    assert (skill_dir / "SKILL.md").is_file()
    assert not (curator.archive_root / "demo").exists()
    assert curator.state_of("demo") == STATE_ACTIVE
    actions = [entry["action"] for entry in curator.ledger_entries()]
    assert actions == ["restore", "archive"]


def test_archive_refuses_pinned_skill(tmp_path):
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    curator.write_usage(skill_dir, UsageRecord(pinned=True))
    with pytest.raises(CuratorError):
        curator.archive("demo")


def test_archive_refuses_external_tier(tmp_path):
    curator = make_curator(tmp_path)
    write_skill(curator.index.external_root)
    with pytest.raises(CuratorError):
        curator.archive("demo")


def test_second_archive_of_same_name_uses_timestamp_suffix(tmp_path):
    curator = make_curator(tmp_path)
    write_skill(curator.index.user_root)
    curator.archive("demo")
    write_skill(curator.index.user_root)  # 又造一个同名技能
    curator.archive("demo")
    archived = sorted(path.name for path in curator.archive_root.iterdir() if path.is_dir())
    assert len(archived) == 2
    assert "demo" in archived
    assert any(name.startswith("demo-") for name in archived)


def test_restore_missing_skill_raises(tmp_path):
    curator = make_curator(tmp_path)
    with pytest.raises(CuratorError):
        curator.restore("ghost")


def test_pin_and_unpin_are_idempotent(tmp_path):
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    first = curator.pin("demo")
    assert first
    assert curator.read_usage(skill_dir).pinned is True
    assert curator.pin("demo") == ""  # 重复 pin 不写账本
    assert curator.unpin("demo")
    assert curator.read_usage(skill_dir).pinned is False


# ── 6. `.usage.json`：13 字段 / 读写幂等 ───────────────────────────────────


def test_usage_json_has_all_13_fields(tmp_path):
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    curator.write_usage(skill_dir, used_once(NOW))

    payload = json.loads((skill_dir / ".usage.json").read_text(encoding="utf-8"))
    assert len(USAGE_FIELDS) == 13
    assert set(USAGE_FIELDS) <= set(payload)
    # 第 14 个键是锚点的第二级回退，单列出来是为了让"13 个统计字段"这件事可核对
    assert set(payload) == set(USAGE_FIELDS) | set(USAGE_ANCHOR_FIELDS)
    assert payload["use_count"] == 1
    assert payload["first_used_at"] == NOW.isoformat()


def test_usage_write_is_idempotent(tmp_path):
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    record = used_once(NOW)
    curator.write_usage(skill_dir, record)
    first = (skill_dir / ".usage.json").read_text(encoding="utf-8")
    curator.write_usage(skill_dir, record)
    second = (skill_dir / ".usage.json").read_text(encoding="utf-8")
    assert first == second
    assert curator.read_usage(skill_dir).to_dict() == record.to_dict()


def test_usage_record_round_trip_keeps_all_fields():
    record = UsageRecord(
        use_count=3,
        view_count=5,
        patch_count=1,
        reuse_after_patch=True,
        last_used_at=NOW.isoformat(),
        last_viewed_at=NOW.isoformat(),
        last_patched_at=NOW.isoformat(),
        first_used_at=NOW.isoformat(),
        consecutive_failures=1,
        successes=4,
        failures=2,
        created_by="agent",
        pinned=True,
        created_at=NOW.isoformat(),
    )
    assert UsageRecord.from_dict(record.to_dict()).to_dict() == record.to_dict()


def test_corrupt_usage_file_degrades_to_empty_record(tmp_path):
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    (skill_dir / ".usage.json").write_text("{不是 json", encoding="utf-8")
    assert curator.read_usage(skill_dir).use_count == 0


def test_success_rate_without_samples_is_one():
    """0 样本不能被当成"全失败"——否则从没跑过的技能会被直接归档。"""
    assert UsageRecord().success_rate == 1.0
    assert UsageRecord(successes=1, failures=1).success_rate == 0.5


# ── 7. prune ──────────────────────────────────────────────────────────────


def test_prune_defaults_to_ninety_days(tmp_path):
    assert ARCHIVE_DAYS == 90
    curator = make_curator(tmp_path)
    old_dir = write_skill(curator.index.user_root, "old")
    new_dir = write_skill(curator.index.user_root, "new")
    seed_usage(curator, old_dir, used_once(NOW - timedelta(days=100)))
    seed_usage(curator, new_dir, used_once(NOW - timedelta(days=10)))

    pruned = curator.prune(dry_run=True)
    assert [item["name"] for item in pruned] == ["old"]
    assert pruned[0]["to"] == STATE_ARCHIVED


def test_prune_skips_never_used_and_pinned(tmp_path):
    curator = make_curator(tmp_path)
    never_dir = write_skill(curator.index.user_root, "never")
    pinned_dir = write_skill(curator.index.user_root, "pinned")
    seed_usage(curator, never_dir, UsageRecord(created_at=(NOW - timedelta(days=400)).isoformat()))
    record = used_once(NOW - timedelta(days=400))
    record.pinned = True
    seed_usage(curator, pinned_dir, record)

    assert curator.prune(dry_run=True) == []


def test_prune_dry_run_does_not_move(tmp_path):
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    seed_usage(curator, skill_dir, used_once(NOW - timedelta(days=200)))
    curator.prune(dry_run=True)
    assert skill_dir.is_dir()
    assert not curator.archive_root.exists()


def test_prune_real_run_archives(tmp_path):
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    seed_usage(curator, skill_dir, used_once(NOW - timedelta(days=200)))
    pruned = curator.prune(dry_run=False)
    assert [item["name"] for item in pruned] == ["demo"]
    assert not skill_dir.exists()
    assert (curator.archive_root / "demo" / "SKILL.md").is_file()


# ── 8. status / usage_report ──────────────────────────────────────────────


def test_status_counts_states_and_thresholds(tmp_path):
    curator = make_curator(tmp_path)
    write_skill(curator.index.user_root, "one")
    write_skill(curator.index.user_root, "two")
    curator.set_state("two", STATE_STALE)

    report = curator.status()
    assert report["skills"] == 2
    assert report["states"][STATE_UNREVIEWED] == 1
    assert report["states"][STATE_STALE] == 1
    assert report["thresholds"] == {
        "stale_failures": STALE_FAILURES,
        "archive_days": ARCHIVE_DAYS,
        "archive_min_success_rate": ARCHIVE_MIN_SUCCESS_RATE,
        "archive_min_samples": ARCHIVE_MIN_SAMPLES,
    }


def test_usage_report_sort_and_provenance(tmp_path):
    curator = make_curator(tmp_path)
    busy_dir = write_skill(curator.index.user_root, "busy")
    idle_dir = write_skill(curator.index.user_root, "idle")
    busy = used_once(NOW)
    busy.view_count = 9
    busy.created_by = "agent"
    curator.write_usage(busy_dir, busy)
    curator.write_usage(idle_dir, used_once(NOW))

    rows = curator.usage_report(sort="activity")
    assert [row["name"] for row in rows] == ["busy", "idle"]
    assert rows[0]["activity"] == 10

    assert [row["name"] for row in curator.usage_report(sort="name")] == ["busy", "idle"]
    agent_rows = curator.usage_report(provenance="agent")
    assert [row["name"] for row in agent_rows] == ["busy"]
    # Orbit 没有 hub 通道：这个枚举恒为空，是事实而不是 bug
    assert curator.usage_report(provenance="hub") == []


# ── 9. CLI（register_handler 接线的真实入口）──────────────────────────────


def test_cli_curator_status(tmp_path, capsys):
    data = tmp_path / "data"
    write_skill(data / "skills")
    code = main(["--data-dir", str(data), "curator", "status"])
    out = capsys.readouterr().out
    assert code == 0
    assert "迁移阈值" in out
    assert str(ARCHIVE_DAYS) in out


def test_cli_curator_run_dry_run(tmp_path, capsys):
    data = tmp_path / "data"
    skill_dir = write_skill(data / "skills")
    record = UsageRecord(created_at="2020-01-01T00:00:00+00:00")
    record.record_use(datetime(2020, 1, 1, tzinfo=UTC))
    (skill_dir / ".usage.json").write_text(json.dumps(record.to_dict()), encoding="utf-8")

    code = main(["--data-dir", str(data), "curator", "run", "--dry-run"])
    out = capsys.readouterr().out
    assert code == 0
    assert "dry-run" in out
    assert "demo" in out
    # dry-run 之后技能仍在原位
    assert (skill_dir / "SKILL.md").is_file()
    assert not (data / "skills-archive").exists()


def test_cli_curator_run_json(tmp_path, capsys):
    data = tmp_path / "data"
    write_skill(data / "skills")
    code = main(["--data-dir", str(data), "--json", "curator", "run", "--dry-run"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["dry_run"] is True


def test_cli_curator_pin_unpin(tmp_path, capsys):
    data = tmp_path / "data"
    write_skill(data / "skills")
    assert main(["--data-dir", str(data), "curator", "pin", "demo"]) == 0
    assert "已 pin" in capsys.readouterr().out
    assert main(["--data-dir", str(data), "curator", "unpin", "demo"]) == 0
    assert "已 unpin" in capsys.readouterr().out


def test_cli_curator_archive_restore_and_ledger(tmp_path, capsys):
    data = tmp_path / "data"
    write_skill(data / "skills")
    assert main(["--data-dir", str(data), "curator", "archive", "demo"]) == 0
    assert "已归档" in capsys.readouterr().out
    assert (data / "skills-archive" / "demo" / "SKILL.md").is_file()

    assert main(["--data-dir", str(data), "curator", "ledger"]) == 0
    assert "archive" in capsys.readouterr().out

    assert main(["--data-dir", str(data), "curator", "restore", "demo"]) == 0
    assert "已恢复" in capsys.readouterr().out
    assert (data / "skills" / "demo" / "SKILL.md").is_file()


def test_cli_curator_usage(tmp_path, capsys):
    data = tmp_path / "data"
    write_skill(data / "skills")
    assert main(["--data-dir", str(data), "curator", "usage", "--sort", "name"]) == 0
    assert "demo" in capsys.readouterr().out


def test_cli_curator_prune_requires_yes(tmp_path, capsys):
    data = tmp_path / "data"
    write_skill(data / "skills")
    code = main(["--data-dir", str(data), "curator", "prune"])
    assert code == 1
    assert "拒绝执行" in capsys.readouterr().err
    capsys.readouterr()
    # --dry-run 不需要 -y
    assert main(["--data-dir", str(data), "curator", "prune", "--dry-run"]) == 0


def test_cli_curator_run_consolidate_reports_unimplemented(tmp_path, capsys):
    data = tmp_path / "data"
    write_skill(data / "skills")
    code = main(["--data-dir", str(data), "curator", "run", "--consolidate"])
    assert code == 1
    assert "未实现" in capsys.readouterr().err


def test_cli_curator_archive_unknown_skill(tmp_path, capsys):
    data = tmp_path / "data"
    code = main(["--data-dir", str(data), "curator", "archive", "ghost"])
    assert code == 1
    assert "技能不存在" in capsys.readouterr().err


def test_actor_constants_are_used_in_ledger(tmp_path):
    """curator 自动归档与手动归档的 actor 必须可区分（账本按 actor 过滤是核心用途）。"""
    curator = make_curator(tmp_path)
    skill_dir = write_skill(curator.index.user_root)
    seed_usage(curator, skill_dir, used_once(NOW - timedelta(days=200)))
    curator.set_state("demo", STATE_ACTIVE)
    curator.run(dry_run=False)
    entry = curator.ledger_entries()[0]
    assert entry["actor"] == ACTOR_CURATOR
