"""会话维护（MATRIX 10.13）的契约测试。

每一条命令的判据都是同一句话：**dry-run 一个字节都不动、``apply`` 真的改、
改了多少行说得清**。所以用例基本成对出现（先 dry-run 再 apply），并且断言的是
数据库里的真实状态（重新查一遍），不是报告对象自己说的话——报告是被告，不能自证。
"""

from __future__ import annotations

import sqlite3

import pytest

import app.cli.commands.session_extra3 as extra3  # noqa: F401 —— 导入即接线（10 条 + 4 条 plugins）
from app import session_maintenance as sm
from app.cli.main import main
from app.config import Settings
from app.main import migrate
from app.storage import Conversation, Message, Skill, Store, Task, TaskEvent


def _settings(tmp_path) -> Settings:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    return settings


def _store(settings: Settings) -> Store:
    return Store(settings.data_dir / "app.sqlite")


def _argv(settings: Settings, *rest: str) -> list[str]:
    return ["--data-dir", str(settings.data_dir), *rest]


def _count(settings: Settings, sql: str, params: tuple = ()) -> int:
    connection = sqlite3.connect(str(settings.data_dir / "app.sqlite"))
    try:
        return int(connection.execute(sql, params).fetchone()[0])
    finally:
        connection.close()


async def _seed(store: Store) -> None:
    """一场会话 + 一个僵尸任务 + 一个悬空路由任务 + 三条消息。"""
    await store.save(
        Conversation(id="c1", title="维护测试", updated_at="2026-01-01T00:00:00+00:00")
    )
    await store.save(
        Message(id="m1", conversation_id="c1", role="user", content="正常消息")
    )
    await store.save(
        Message(id="m2", conversation_id="c1", role="user", content="[CONTEXT COMPACTION v1] 只有载荷")
    )
    await store.save(
        Message(id="m3", conversation_id="c1", role="user", content="[CONTEXT SUMMARY]:\n真实正文")
    )
    await store.save(
        Task(
            id="t-zombie",
            conversation_id="c1",
            goal="卡住的任务",
            status="RUNNING",
            updated_at="2020-01-01T00:00:00+00:00",
        )
    )
    await store.save(
        Task(id="t-ghost", conversation_id="missing-conversation", goal="悬空路由", status="PENDING")
    )


# ── 1. optimize ─────────────────────────────────────────────────────────────


def test_optimize_reports_before_and_after_for_every_database(tmp_path):
    """三个库都要出现在报告里，且都带上整理前后的字节数（这是它唯一的价值）。"""
    settings = _settings(tmp_path)
    for name in ("checkpoints.sqlite", "fts.sqlite"):
        connection = sqlite3.connect(str(settings.data_dir / name))
        try:
            connection.execute("CREATE TABLE t(x TEXT)")
            connection.executemany("INSERT INTO t VALUES (?)", [("a" * 200,) for _ in range(50)])
            connection.commit()
            connection.execute("DELETE FROM t")
            connection.commit()
        finally:
            connection.close()

    report = sm.optimize(settings.data_dir)

    assert [item.name for item in report.databases] == list(sm.DATABASE_NAMES)
    assert all(item.note == "" for item in report.databases), "三个库都在，不该有跳过项"
    assert all(item.before > 0 and item.after > 0 for item in report.databases)
    assert report.total_before > 0
    # 整理完的库必须还是好的（VACUUM 半途炸掉会把库搞坏，这是最该防的）
    for name in sm.DATABASE_NAMES:
        connection = sqlite3.connect(str(settings.data_dir / name))
        try:
            assert [row[0] for row in connection.execute("PRAGMA integrity_check")] == ["ok"]
        finally:
            connection.close()


def test_optimize_skips_missing_databases_with_a_reason(tmp_path):
    """缺的库如实报"跳过"，不假装整理过。"""
    settings = _settings(tmp_path)
    report = sm.optimize(settings.data_dir)

    missing = [item for item in report.databases if item.note]
    assert [item.name for item in missing] == ["checkpoints.sqlite", "fts.sqlite"]
    assert all("不存在" in item.note for item in missing)
    assert report.databases[0].note == ""


# ── 2. optimize-storage ─────────────────────────────────────────────────────


async def test_optimize_storage_dry_run_reports_rows_but_changes_nothing(tmp_path):
    """默认 dry-run：报"将删多少行"和体积预估，库里一行都不能少。"""
    settings = _settings(tmp_path)
    store = _store(settings)
    await store.save(Task(id="t1", conversation_id="c1", goal="g"))
    await store.save(Conversation(id="c1", title="c1"))
    await store.save(
        TaskEvent(id=1, task_id="t1", event_type="old", payload={},
                  created_at="2020-01-01T00:00:00+00:00")
    )
    await store.save(
        TaskEvent(id=2, task_id="t1", event_type="old", payload={},
                  created_at="2020-02-01T00:00:00+00:00")
    )
    await store.save(TaskEvent(id=3, task_id="t1", event_type="new", payload={}))
    await store.engine.dispose()

    report = sm.optimize_storage(settings.data_dir, keep_days=30)

    assert report.apply is False
    assert report.events_total == 3
    assert report.pruned == 2, "两条 2020 年的事件超过 30 天"
    assert report.kept == 1
    assert _count(settings, "SELECT count(*) FROM task_events") == 3, "dry-run 不许删行"
    assert not (settings.data_dir / "backups").exists(), "dry-run 不该产生备份"
    assert not list(settings.data_dir.glob("*.preview*")), "副本必须清理干净"


async def test_optimize_storage_apply_deletes_exactly_the_old_rows(tmp_path):
    """``--apply`` 真的删，且删的行数与报告一致（rowcount 不是估算）。"""
    settings = _settings(tmp_path)
    store = _store(settings)
    await store.save(Task(id="t1", conversation_id="c1", goal="g"))
    await store.save(Conversation(id="c1", title="c1"))
    await store.save(
        TaskEvent(id=1, task_id="t1", event_type="old", payload={},
                  created_at="2020-01-01T00:00:00+00:00")
    )
    await store.save(TaskEvent(id=2, task_id="t1", event_type="new", payload={}))
    await store.engine.dispose()

    report = sm.optimize_storage(settings.data_dir, apply=True, keep_days=30)

    assert report.apply is True
    assert report.pruned == 1
    assert report.backup and (settings.data_dir / "backups").is_dir(), "改动前必须有备份"
    assert _count(settings, "SELECT count(*) FROM task_events") == 1
    assert _count(settings, "SELECT count(*) FROM task_events WHERE event_type='new'") == 1


# ── 3. repair ───────────────────────────────────────────────────────────────


def test_repair_reports_foreign_key_problem_without_touching_data(tmp_path):
    """``foreign_key_check`` 抓到悬空外键就如实报；默认不改任何东西。"""
    settings = _settings(tmp_path)
    connection = sqlite3.connect(str(settings.data_dir / "app.sqlite"))
    try:
        connection.execute(
            "INSERT INTO tasks(id, conversation_id, goal, status, waiting, error, created_at,"
            " updated_at) VALUES('t1','ghost','g','PENDING','{}','','x','x')"
        )
        connection.commit()
    finally:
        connection.close()

    report = sm.repair(settings.data_dir)

    app_check = next(item for item in report.checks if item.database == "app.sqlite")
    assert app_check.integrity == ("ok",)
    assert len(app_check.foreign_keys) == 1, "悬空外键必须被抓到"
    assert report.ok is False
    assert report.apply is False
    # 只报告不自动改：那一行还在
    assert _count(settings, "SELECT count(*) FROM tasks") == 1


def test_repair_apply_only_reindexes(tmp_path):
    """``--apply`` 只做 REINDEX（纯派生数据），不改任何业务行。"""
    settings = _settings(tmp_path)
    before = _count(settings, "SELECT count(*) FROM sqlite_master WHERE type='index'")

    report = sm.repair(settings.data_dir, apply=True)

    assert report.apply is True
    assert report.reindexed >= before > 0
    assert _count(settings, "SELECT count(*) FROM sqlite_master WHERE type='index'") == before


# ── 4. repair-routing ───────────────────────────────────────────────────────


async def test_repair_routing_dry_run_lists_rows_and_changes_nothing(tmp_path):
    settings = _settings(tmp_path)
    store = _store(settings)
    await _seed(store)
    await store.engine.dispose()

    report = await sm.repair_routing(_store(settings))

    assert [item.task_id for item in report.orphans] == ["t-ghost"]
    assert report.orphans[0].conversation_id == "missing-conversation"
    assert report.orphans[0].new_conversation_id == "c1", "目标必须是最近会话"
    assert report.changed == 0
    assert (
        _count(settings, "SELECT count(*) FROM tasks WHERE conversation_id='missing-conversation'")
        == 1
    ), "dry-run 不许改路由"


async def test_repair_routing_apply_reattaches_to_the_latest_session(tmp_path):
    settings = _settings(tmp_path)
    store = _store(settings)
    await _seed(store)
    await store.engine.dispose()

    report = await sm.repair_routing(_store(settings), apply=True)

    assert report.changed == 1
    assert (
        _count(settings, "SELECT count(*) FROM tasks WHERE id='t-ghost' AND conversation_id='c1'")
        == 1
    )


async def test_repair_routing_refuses_when_there_is_no_session_to_attach_to(tmp_path):
    """一个会话都没有时无从重挂：如实报，不猜、不置空。"""
    settings = _settings(tmp_path)
    store = _store(settings)
    await store.save(Task(id="t1", conversation_id="ghost", goal="g"))
    await store.engine.dispose()

    report = await sm.repair_routing(_store(settings), apply=True)

    assert report.changed == 0
    assert "一个会话都没有" in report.note
    assert _count(settings, "SELECT count(*) FROM tasks WHERE conversation_id='ghost'") == 1


# ── 5. recover ──────────────────────────────────────────────────────────────


async def test_recover_dry_run_reports_zombies_without_changing_status(tmp_path):
    settings = _settings(tmp_path)
    store = _store(settings)
    await _seed(store)
    await store.engine.dispose()

    report = await sm.recover(_store(settings), stale_minutes=30)

    assert [item.task_id for item in report.zombies] == ["t-zombie"]
    assert report.changed == 0
    assert _count(settings, "SELECT count(*) FROM tasks WHERE status='RUNNING'") == 1


async def test_recover_apply_marks_failed_and_writes_end_reason(tmp_path):
    """``--apply`` 真的改，并且 ``end_reason=recovered`` 落在三处可查的地方。"""
    settings = _settings(tmp_path)
    store = _store(settings)
    await _seed(store)
    await store.engine.dispose()

    report = await sm.recover(_store(settings), apply=True, stale_minutes=30)

    assert report.changed == 1
    assert _count(settings, "SELECT count(*) FROM tasks WHERE status='FAILED'") == 1
    assert (
        _count(settings, "SELECT count(*) FROM tasks WHERE id='t-zombie' AND error='recovered'")
        == 1
    )
    # tasks 表没有 end_reason 列，所以它落在 waiting JSON 里 + 一条事件里
    connection = sqlite3.connect(str(settings.data_dir / "app.sqlite"))
    try:
        waiting = connection.execute(
            "SELECT waiting FROM tasks WHERE id='t-zombie'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert "recovered" in str(waiting)
    assert (
        _count(settings, "SELECT count(*) FROM task_events WHERE event_type='recovered'") == 1
    )


async def test_recover_ignores_a_fresh_running_task(tmp_path):
    settings = _settings(tmp_path)
    store = _store(settings)
    await store.save(Conversation(id="c1", title="c1"))
    await store.save(Task(id="t-fresh", conversation_id="c1", goal="刚跑", status="RUNNING"))
    await store.engine.dispose()

    report = await sm.recover(_store(settings), apply=True, stale_minutes=30)

    assert report.zombies == ()
    assert report.changed == 0
    assert _count(settings, "SELECT count(*) FROM tasks WHERE status='RUNNING'") == 1


# ── 6. clean-markers ────────────────────────────────────────────────────────


async def test_clean_markers_dry_run_touches_nothing(tmp_path):
    settings = _settings(tmp_path)
    store = _store(settings)
    await _seed(store)
    await store.engine.dispose()

    report = await sm.clean_markers(_store(settings))

    assert report.apply is False
    assert {item.message_id for item in report.hits} == {"m2", "m3"}
    assert report.stripped == 0 and report.deleted == 0
    assert _count(settings, "SELECT count(*) FROM messages") == 3


async def test_clean_markers_apply_deletes_pure_payload_and_strips_prefix(tmp_path):
    """只有标记行的整条删；标记行 + 正文的只剥标记（正文一个字都不能丢）。"""
    settings = _settings(tmp_path)
    store = _store(settings)
    await _seed(store)
    await store.engine.dispose()

    report = await sm.clean_markers(_store(settings), apply=True)

    assert report.deleted == 1 and report.stripped == 1
    assert _count(settings, "SELECT count(*) FROM messages WHERE id='m2'") == 0
    connection = sqlite3.connect(str(settings.data_dir / "app.sqlite"))
    try:
        content = connection.execute("SELECT content FROM messages WHERE id='m3'").fetchone()[0]
        untouched = connection.execute("SELECT content FROM messages WHERE id='m1'").fetchone()[0]
    finally:
        connection.close()
    assert content == "真实正文", "正文必须保留，只剥掉标记行"
    assert untouched == "正常消息", "没命中的消息不许动"


# ── 7. retitle-skills ───────────────────────────────────────────────────────


async def _seed_skill(settings: Settings, store: Store) -> None:
    directory = settings.data_dir / "skills" / "dir-name"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        "---\nname: fm-name\ntriggers: [x]\n---\n正文\n", encoding="utf-8"
    )
    await store.save(Skill(name="dir-name", source="local"))


async def test_retitle_skills_dry_run_reports_the_rename_only(tmp_path):
    settings = _settings(tmp_path)
    store = _store(settings)
    await _seed_skill(settings, store)
    await store.engine.dispose()

    report = await sm.retitle_skills(_store(settings), settings.data_dir / "skills")

    assert [(item.old_name, item.new_name) for item in report.renames] == [
        ("dir-name", "fm-name")
    ]
    assert report.changed == 0
    assert _count(settings, "SELECT count(*) FROM skills WHERE name='dir-name'") == 1


async def test_retitle_skills_apply_rewrites_the_index_name(tmp_path):
    settings = _settings(tmp_path)
    store = _store(settings)
    await _seed_skill(settings, store)
    await store.engine.dispose()

    report = await sm.retitle_skills(
        _store(settings), settings.data_dir / "skills", apply=True
    )

    assert report.changed == 1
    assert _count(settings, "SELECT count(*) FROM skills WHERE name='fm-name'") == 1
    assert _count(settings, "SELECT count(*) FROM skills WHERE name='dir-name'") == 0


# ── 8. pin / unpin / pinned ─────────────────────────────────────────────────


async def test_pin_table_is_created_idempotently(tmp_path):
    """自建表：``CREATE TABLE IF NOT EXISTS``，跑两次不报错、列就那三列。"""
    settings = _settings(tmp_path)
    store = _store(settings)
    await sm.ensure_pin_table(store)
    await sm.ensure_pin_table(store)
    connection = sqlite3.connect(str(settings.data_dir / "app.sqlite"))
    try:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(session_pins)")]
    finally:
        connection.close()
    await store.engine.dispose()
    assert columns == ["session_id", "pinned_at", "note"]


async def test_pin_unpin_round_trip_and_dangling_detection(tmp_path):
    settings = _settings(tmp_path)
    store = _store(settings)
    await _seed(store)
    await store.engine.dispose()

    store = _store(settings)
    outcome = await sm.pin(store, ["c1"], note="别清掉")
    assert outcome.pinned == ("c1",)
    entries = await sm.pinned(store)
    assert [(item.session_id, item.note, item.dangling) for item in entries] == [
        ("c1", "别清掉", False)
    ]

    # 悬空 pin：会话被删掉后 pin 还在，必须被检出来
    async with store.sessions() as session:
        conversation = await session.get(Conversation, "c1")
        assert conversation is not None
        await session.delete(conversation)
        await session.commit()
    entries = await sm.pinned(store)
    assert [item.dangling for item in entries] == [True]

    # 会话没了，unpin 用原 id 直接删 pin（前缀解析会报"没有这个会话"，所以用 id 也得能删）
    removed = await sm.unpin(store, ["c1"])
    assert removed.pinned == ("c1",)
    assert await sm.pinned(store) == ()
    await store.engine.dispose()


async def test_pin_accepts_unique_prefix_and_rejects_ambiguity(tmp_path):
    settings = _settings(tmp_path)
    store = _store(settings)
    await store.save(Conversation(id="aaa111", title="a"))
    await store.save(Conversation(id="aab222", title="b"))

    ambiguous = await sm.pin(store, ["aa"])
    assert ambiguous.pinned == ()
    assert "歧义" in ambiguous.skipped[0][1]

    unique = await sm.pin(store, ["aaa"])
    assert unique.pinned == ("aaa111",)
    missing = await sm.pin(store, ["nope"])
    assert missing.skipped[0][1] == "没有这个会话"
    await store.engine.dispose()


def test_split_ids_accepts_spaces_and_commas():
    assert sm.split_ids(["a,b", "c"]) == ["a", "b", "c"]
    assert sm.split_ids("a b") == ["a", "b"]
    assert sm.split_ids(None) == []


def test_marker_of_uses_the_session_search_prefixes():
    """标记判定必须来自 ``app/session_search.py``（唯一权威来源）。"""
    assert sm.marker_of("[CONTEXT COMPACTION v2] x") == "[CONTEXT COMPACTION"
    assert sm.marker_of("[CONTEXT SUMMARY]:\n正文") == "[CONTEXT SUMMARY]:"
    assert sm.marker_of("中段提到 [CONTEXT COMPACTION 但不在开头") == ""


# ── 9. 接线：10 条路径都接上了，且 CLI 真能跑通 ─────────────────────────────


def test_all_ten_session_paths_are_wired_and_done():
    from app.cli.spec import REGISTRY

    parent = REGISTRY.get("sessions")
    assert parent is not None
    names = [
        "optimize",
        "clean-markers",
        "optimize-storage",
        "repair",
        "repair-routing",
        "recover",
        "pin",
        "unpin",
        "pinned",
        "retitle-skills",
    ]
    for name in names:
        node = parent.child(name)
        assert node is not None, name
        assert node.state == "done", f"{name} 还是 {node.state}"
        assert node.handler is not None, name
        assert node.help.strip(), name
        assert f"sessions {name}" in extra3.WIRED


def test_cli_sessions_optimize_prints_sizes(tmp_path, capsys):
    settings = _settings(tmp_path)
    assert main(_argv(settings, "sessions", "optimize")) == 0
    out = capsys.readouterr().out
    assert "app.sqlite" in out
    assert "合计" in out


def test_cli_sessions_pin_pinned_unpin(tmp_path, capsys):
    settings = _settings(tmp_path)
    store = _store(settings)
    import asyncio

    asyncio.run(store.save(Conversation(id="c1", title="会话一")))
    asyncio.run(store.engine.dispose())

    assert main(_argv(settings, "sessions", "pin", "c1", "--note", "留着")) == 0
    capsys.readouterr()
    assert main(_argv(settings, "--json", "sessions", "pinned")) == 0
    payload = capsys.readouterr().out
    assert '"session_id": "c1"' in payload and '"dangling": 0' in payload
    assert main(_argv(settings, "sessions", "unpin", "c1")) == 0
    capsys.readouterr()
    assert main(_argv(settings, "sessions", "pinned")) == 0
    assert "还没有钉住任何会话" in capsys.readouterr().out


def test_cli_sessions_clean_markers_is_dry_run_by_default(tmp_path, capsys):
    """默认 dry-run 是**命令层**的承诺，不只是模块层的。"""
    settings = _settings(tmp_path)
    store = _store(settings)
    import asyncio

    asyncio.run(store.save(Conversation(id="c1", title="c1")))
    asyncio.run(
        store.save(
            Message(id="m1", conversation_id="c1", role="user", content="[CONTEXT COMPACTION v1]")
        )
    )
    asyncio.run(store.engine.dispose())

    assert main(_argv(settings, "sessions", "clean-markers")) == 0
    out = capsys.readouterr().out
    assert "dry-run" in out
    assert _count(settings, "SELECT count(*) FROM messages") == 1

    assert main(_argv(settings, "sessions", "clean-markers", "--apply")) == 0
    assert _count(settings, "SELECT count(*) FROM messages") == 0


@pytest.mark.parametrize(
    "argv",
    [
        ("sessions", "repair"),
        ("sessions", "optimize-storage"),
        ("sessions", "repair-routing"),
        ("sessions", "recover"),
        ("sessions", "retitle-skills"),
    ],
)
def test_cli_maintenance_commands_run_on_an_empty_but_valid_database(tmp_path, capsys, argv):
    """空库上每条命令都得给出结论而不是栈（"没有可改的"是正常结论）。"""
    settings = _settings(tmp_path)
    assert main(_argv(settings, *argv)) == 0
    assert capsys.readouterr().out.strip()
