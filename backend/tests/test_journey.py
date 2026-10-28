"""journey（学习时间线）的测试。

三条必测：
1. **时间线只从审计数据推导**：`.usage.json` + `skills-state.json` + ledger 三处拼出来，
   五类 kind（created/used/patched/promoted/archived）都要能出现；
2. **edit / delete 只改旁注文件**：`.usage.json` 与 ledger 的字节必须一个都不变
   ——「审计数据不可篡改」是这一层唯一的硬承诺；
3. **别名**：`learning` / `memory-graph` 与 `journey` 是同一个 handler。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.cli.commands import skill_extra  # noqa: F401  （导入即接线）
from app.cli.main import main
from app.curator import ACTOR_USER, STATE_ACTIVE, STATE_ARCHIVED, Curator, UsageRecord
from app.journey import (
    DEFAULT_DAYS,
    KIND_ARCHIVED,
    KIND_CREATED,
    KIND_PATCHED,
    KIND_PROMOTED,
    KIND_USED,
    KINDS,
    NOTES_FILENAME,
    Journey,
    JourneyError,
    make_entry_id,
)
from app.skill_index import SkillIndex

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

SKILL_TEXT = (
    "---\n"
    "name: {name}\n"
    "description: {name} 的用途。\n"
    "version: 1\n"
    "---\n\n"
    "# 用法\n\n1. 做{name}这件事\n"
)


class Clock:
    """可拨动的时钟（与 `tests/test_curator.py` 同一套做法）。"""

    def __init__(self, moment: datetime = NOW) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment

    def advance(self, days: float) -> None:
        self.moment = self.moment + timedelta(days=days)


def write_skill(data: Path, name: str = "demo") -> Path:
    directory = data / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(SKILL_TEXT.format(name=name), encoding="utf-8")
    return directory


def make_journey(tmp_path, clock: Clock | None = None) -> Journey:
    """构造一份 index + curator + journey 共享同一时钟的视图。"""
    tick = clock or Clock()
    data = tmp_path / "data"
    index = SkillIndex(data)
    curator = Curator(data, index=index, now=tick)
    return Journey(data, index=index, curator=curator, now=tick)


def seed_usage(journey: Journey, skill_dir: Path, *, moment: datetime = NOW) -> UsageRecord:
    record = UsageRecord(created_at=moment.isoformat())
    record.record_use(moment)
    record.record_patch(moment)
    journey.curator.write_usage(skill_dir, record)
    return record


# ── 时间线来源 ──────────────────────────────────────────────────────────────


def test_timeline_is_built_from_usage_file(tmp_path):
    journey = make_journey(tmp_path)
    skill_dir = write_skill(tmp_path / "data")
    seed_usage(journey, skill_dir)

    entries = journey.list()
    kinds = {entry.kind for entry in entries}
    assert kinds == {KIND_CREATED, KIND_USED, KIND_PATCHED}
    assert all(entry.skill == "demo" for entry in entries)
    assert entries == sorted(entries, key=lambda item: item.moment), "最旧在上、最新在下"


def test_timeline_is_built_from_state_file(tmp_path):
    journey = make_journey(tmp_path)
    write_skill(tmp_path / "data")
    journey.curator.set_state("demo", STATE_ACTIVE, "首次使用：已产生真实活动")

    promoted = [entry for entry in journey.list() if entry.kind == KIND_PROMOTED]
    assert len(promoted) == 1
    assert "首次使用" in promoted[0].detail


def test_timeline_is_built_from_ledger(tmp_path):
    journey = make_journey(tmp_path)
    write_skill(tmp_path / "data")
    journey.curator.record_ledger(
        action="archive", skill="demo", actor=ACTOR_USER, evidence={"reason": "手动归档"}
    )
    archived = [entry for entry in journey.list() if entry.kind == KIND_ARCHIVED]
    assert len(archived) == 1
    assert "手动归档" in archived[0].detail


def test_restore_and_rollback_show_up_as_promoted(tmp_path):
    journey = make_journey(tmp_path)
    write_skill(tmp_path / "data")
    journey.curator.record_ledger(action="restore", skill="demo")
    journey.curator.record_ledger(action="rollback", skill="demo")
    promoted = [entry for entry in journey.list() if entry.kind == KIND_PROMOTED]
    assert len(promoted) == 2
    assert {entry.detail.split("：")[0] for entry in promoted} == {"ledger restore", "ledger rollback"}


def test_unknown_ledger_action_falls_back_to_patched(tmp_path):
    """时间线只有 5 类；未知治理动作归 patched 并写清动作名（而不是消失）。"""
    journey = make_journey(tmp_path)
    write_skill(tmp_path / "data")
    journey.curator.record_ledger(action="pin", skill="demo")
    entries = [entry for entry in journey.list() if entry.detail.startswith("ledger pin")]
    assert entries and entries[0].kind == KIND_PATCHED


def test_all_five_kinds_are_reachable(tmp_path):
    """五类 kind 都出现在同一条时间线上（矩阵里写的就是这五类）。

    注意 `skills-state.json` 每个技能只保留**当前**状态（一条记录），所以
    ``promoted`` 在这里靠 ledger 的 ``restore`` 造，而不是靠「先 active 再 archived」
    ——后者会把 active 那条覆盖掉。这不是缺陷，是状态表的定义（当前态，不是历史）。
    """
    journey = make_journey(tmp_path)
    skill_dir = write_skill(tmp_path / "data")
    seed_usage(journey, skill_dir)
    journey.curator.record_ledger(action="restore", skill="demo")
    journey.curator.set_state("demo", STATE_ARCHIVED, "闲置")

    kinds = {entry.kind for entry in journey.list()}
    assert kinds == set(KINDS)


def test_entries_without_timestamp_are_skipped(tmp_path):
    journey = make_journey(tmp_path)
    skill_dir = write_skill(tmp_path / "data")
    record = UsageRecord(created_at="")
    record.use_count = 3  # 有计数但没有时间戳：无法放到任何一天
    journey.curator.write_usage(skill_dir, record)
    assert journey.list() == []


def test_days_window_filters_old_entries(tmp_path):
    clock = Clock()
    journey = make_journey(tmp_path, clock)
    skill_dir = write_skill(tmp_path / "data")
    seed_usage(journey, skill_dir, moment=NOW - timedelta(days=60))

    assert journey.list(days=DEFAULT_DAYS) == [], "60 天前的事件不在 30 天窗口里"
    assert len(journey.list(days=90)) == 3


def test_empty_skill_tree_gives_empty_timeline(tmp_path):
    assert make_journey(tmp_path).list() == []


# ── edit / delete：只动旁注文件 ─────────────────────────────────────────────


def test_edit_writes_note_without_touching_audit_data(tmp_path):
    journey = make_journey(tmp_path)
    skill_dir = write_skill(tmp_path / "data")
    seed_usage(journey, skill_dir)
    journey.curator.record_ledger(action="archive", skill="demo", evidence={"reason": "闲置"})

    usage_path = skill_dir / ".usage.json"
    ledger_path = journey.curator.ledger_path
    usage_before = usage_path.read_bytes()
    ledger_before = ledger_path.read_bytes()

    entry = journey.list()[0]
    edited = journey.edit(entry.entry_id, "这条是第一次学会导出")

    assert edited.note == "这条是第一次学会导出"
    assert usage_path.read_bytes() == usage_before, ".usage.json 一个字节都不能变"
    assert ledger_path.read_bytes() == ledger_before, "ledger 一个字节都不能变"
    assert (tmp_path / "data" / NOTES_FILENAME).is_file()
    # 重新推导一遍时间线，旁注仍然挂得住（entry_id 是内容寻址）
    assert [item.note for item in journey.list() if item.entry_id == entry.entry_id] == [
        "这条是第一次学会导出"
    ]


def test_delete_hides_entry_but_keeps_audit_data(tmp_path):
    journey = make_journey(tmp_path)
    skill_dir = write_skill(tmp_path / "data")
    seed_usage(journey, skill_dir)
    usage_before = (skill_dir / ".usage.json").read_bytes()

    entry = journey.list()[0]
    assert journey.delete(entry.entry_id) is True
    assert journey.delete(entry.entry_id) is False, "重复删除幂等"

    remaining = [item.entry_id for item in journey.list()]
    assert entry.entry_id not in remaining
    assert len(remaining) == 2
    assert (skill_dir / ".usage.json").read_bytes() == usage_before

    notes = json.loads((tmp_path / "data" / NOTES_FILENAME).read_text(encoding="utf-8"))
    assert notes["notes"][entry.entry_id]["deleted"] is True


def test_edit_after_delete_brings_the_entry_back(tmp_path):
    journey = make_journey(tmp_path)
    skill_dir = write_skill(tmp_path / "data")
    seed_usage(journey, skill_dir)
    entry = journey.list()[0]
    journey.delete(entry.entry_id)
    journey.edit(entry.entry_id, "其实有用")
    assert entry.entry_id in [item.entry_id for item in journey.list()]


def test_unknown_node_id_raises(tmp_path):
    journey = make_journey(tmp_path)
    with pytest.raises(JourneyError, match="时间线里没有"):
        journey.edit("deadbeef", "x")
    with pytest.raises(JourneyError, match="时间线里没有"):
        journey.delete("deadbeef")


def test_entry_id_is_content_addressed():
    assert make_entry_id("t", "used", "demo", "d") == make_entry_id("t", "used", "demo", "d")
    assert make_entry_id("t", "used", "demo", "d") != make_entry_id("t", "used", "demo", "e")


# ── CLI ─────────────────────────────────────────────────────────────────────


def seed_cli(tmp_path) -> tuple[Path, str]:
    """在真实时钟下造一条可用的时间线，返回 (data_dir, 节点 id)。"""
    data = tmp_path / "data"
    skill_dir = data / "skills" / "demo"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_TEXT.format(name="demo"), encoding="utf-8")
    moment = datetime.now(UTC)
    record = UsageRecord(created_at=moment.isoformat())
    record.record_use(moment)
    (skill_dir / ".usage.json").write_text(
        json.dumps(record.to_dict(), ensure_ascii=False), encoding="utf-8"
    )
    journey = Journey(data)
    return data, journey.list()[0].entry_id


def test_cli_journey_list(tmp_path, capsys):
    data, entry_id = seed_cli(tmp_path)
    assert main(["--data-dir", str(data), "journey", "list"]) == 0
    out = capsys.readouterr().out
    assert "条事件" in out
    assert "created" in out or "used" in out
    assert entry_id in out, "节点 id 必须打出来，否则没法 edit/delete"


def test_cli_journey_aliases_are_the_same_handler(tmp_path, capsys):
    data, _entry_id = seed_cli(tmp_path)
    for alias in ("learning", "memory-graph"):
        assert main(["--data-dir", str(data), alias, "list"]) == 0
        assert "条事件" in capsys.readouterr().out


def test_cli_journey_edit(tmp_path, capsys):
    data, entry_id = seed_cli(tmp_path)
    assert main(
        ["--data-dir", str(data), "journey", "edit", entry_id, "--note", "第一次学会"]
    ) == 0
    out = capsys.readouterr().out
    assert "已记旁注" in out
    assert "第一次学会" in out
    assert (data / NOTES_FILENAME).is_file()

    assert main(["--data-dir", str(data), "journey", "list"]) == 0
    assert "第一次学会" in capsys.readouterr().out


def test_cli_journey_edit_without_note_errors(tmp_path, monkeypatch, capsys):
    data, entry_id = seed_cli(tmp_path)

    class _Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", _Tty())
    code = main(["--data-dir", str(data), "journey", "edit", entry_id])
    assert code == 1
    assert "--note" in capsys.readouterr().err


def test_cli_journey_delete_requires_yes(tmp_path, capsys):
    data, entry_id = seed_cli(tmp_path)
    code = main(["--data-dir", str(data), "journey", "delete", entry_id])
    assert code == 1
    assert "拒绝执行" in capsys.readouterr().err

    assert main(["--data-dir", str(data), "journey", "delete", entry_id, "-y"]) == 0
    assert "已隐藏" in capsys.readouterr().out


def test_cli_journey_unknown_node(tmp_path, capsys):
    data, _entry_id = seed_cli(tmp_path)
    code = main(["--data-dir", str(data), "journey", "edit", "deadbeef", "--note", "x"])
    assert code == 1
    assert "时间线里没有" in capsys.readouterr().err


def test_cli_journey_json_payload(tmp_path, capsys):
    data, _entry_id = seed_cli(tmp_path)
    assert main(["--data-dir", str(data), "--json", "journey", "list", "--days", "7"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["days"] == 7
    assert payload["count"] >= 1
    assert payload["entries"][0]["kind"] in KINDS


def test_cli_journey_warns_about_unimplemented_animation(tmp_path, capsys):
    """`--play` / `--reveal` 这些动画选项在 `journey` 父命令上（argparse 的层级），
    接受但明确报未实现——假装跑了一次动画比拒绝更浪费时间。"""
    data, _entry_id = seed_cli(tmp_path)
    assert main(["--data-dir", str(data), "journey", "--play", "list"]) == 0
    assert "未实现" in capsys.readouterr().err
