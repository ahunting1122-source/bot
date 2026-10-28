"""统一 session 平面（``app/session_plane.py``）的契约测试。

钉住四件事，每件都对应一个"看起来能用、实际会坏"的失败模式：

1. **id 续号**：新建一个 :class:`SessionPlane` 实例（模拟进程重启）接着写，id 不能回到 0。
   靠内存计数器的实现能过所有单进程用例，却会在重启后让 ``since(cursor)`` 永远返回空
   ——那正是这条用例存在的理由。
2. **``since`` 按 id 游标**：同一毫秒内写入的多条事件，用时间戳做游标会一起漏掉或一起
   重复；用 id 游标只返回真正的新事件。
3. **统一时间线**：``messages`` / ``task_events`` / 平面事件三个来源合并后**按时间排序
   且不丢**（少一个来源就等于"cron 的会话看不全"，而这正是本层要解决的问题）。
4. **压缩谱系**：父子链上溯；**循环引用必须报错**而不是把命令挂死。

全程不建表、不迁移数据库以外的东西：平面是 JSONL，时间线读的是既有 ``app.sqlite``。
"""

from __future__ import annotations

import json

import pytest

import app.cli.commands.session_plane as plane_cli  # noqa: F401 —— 导入即接线
from app.cli.main import main
from app.cli.spec import REGISTRY
from app.config import Settings
from app.main import migrate
from app.session_plane import (
    KIND_COMPRESSED,
    AppendLog,
    SessionPlane,
    SessionPlaneError,
    to_epoch,
)
from app.storage import Conversation, Message, Store, Task, TaskEvent


def _plane(tmp_path) -> SessionPlane:
    return SessionPlane(tmp_path / "data")


# ── 追加日志 ────────────────────────────────────────────────────────────────


def test_ids_are_monotonic(tmp_path):
    """id 从 1 开始严格递增，每条事件都带 kind / session_id / ts。"""
    plane = _plane(tmp_path)
    first = plane.append({"kind": "session.created", "session_id": "s1"})
    second = plane.append({"kind": "session.resumed", "session_id": "s1"})

    assert first["id"] == 1
    assert second["id"] == 2
    assert second["id"] > first["id"]
    assert first["ts"], "ts 不能为空——时间线要靠它排序"
    assert first["kind"] == "session.created"
    assert first["session_id"] == "s1"


def test_id_continues_after_restart(tmp_path):
    """**新建一个 SessionPlane 实例**（等价于进程重启）接着写，id 不回到 0。

    这条用例专门挡"内存计数器"的实现：那种实现在单进程里全绿，重启后新事件会
    与旧事件撞 id，``since`` 当场失效。
    """
    first_run = _plane(tmp_path)
    first_run.append({"kind": "session.created", "session_id": "s1"})
    first_run.append({"kind": "session.created", "session_id": "s2"})

    # 模拟进程重启：全新对象、全新锁、全新缓存，只有文件还在。
    second_run = _plane(tmp_path)
    resumed = second_run.append({"kind": "session.created", "session_id": "s3"})

    assert resumed["id"] == 3, "重启后 id 必须续上文件里的最大值"
    assert [item["id"] for item in second_run.since(0)] == [1, 2, 3]


def test_id_continues_past_broken_lines(tmp_path):
    """坏行（半行 JSON）不能让续号退回 1，也不能让读日志失败。

    如实标注一个边界：**写坏的那一行自己的 id 是不可知的**（它连 JSON 都不成立，
    解析不出 id），所以"不复用"只对**可解析的**记录成立。这里断言的是可解析记录
    的最大值被尊重、坏行被跳过、后续事件继续递增。
    """
    plane = _plane(tmp_path)
    plane.append({"kind": "session.created", "session_id": "s1"})
    with plane.path.open("a", encoding="utf-8") as handle:
        handle.write('{"id": 99, "kind": "broken"\n')  # 故意少一个右括号
    plane.append({"kind": "session.created", "session_id": "s2"})

    fresh = _plane(tmp_path)
    event = fresh.append({"kind": "session.created", "session_id": "s3"})
    assert event["id"] == 3, "可解析记录的 id 是 1 和 2，新事件必须是 3"
    assert [item["id"] for item in fresh.since(0)] == [1, 2, 3]


def test_since_returns_only_new_events(tmp_path):
    """``since(cursor)`` 只返回 id 更大的事件（按 id 而不是按时间戳）。"""
    plane = _plane(tmp_path)
    for index in range(3):
        plane.append({"kind": "session.created", "session_id": f"s{index}", "ts": "2026-01-01T00:00:00+00:00"})

    # 三条事件时间戳完全相同：按 ts 做游标必然出错，按 id 才正确。
    assert [item["id"] for item in plane.since(0)] == [1, 2, 3]
    assert [item["id"] for item in plane.since(1)] == [2, 3]
    assert plane.since(3) == []
    assert [item["id"] for item in plane.since(0, limit=2)] == [1, 2]
    assert plane.cursor() == 3


def test_tail_returns_the_last_events_of_one_session(tmp_path):
    plane = _plane(tmp_path)
    for index in range(5):
        plane.append({"kind": "message", "session_id": "s1" if index % 2 else "s2"})

    tail = plane.tail("s1", 2)
    assert len(tail) == 2
    assert all(item["session_id"] == "s1" for item in tail)
    assert [item["id"] for item in tail] == [2, 4], "tail 必须正序，且只含该会话"


def test_append_requires_kind(tmp_path):
    with pytest.raises(SessionPlaneError):
        _plane(tmp_path).append({"session_id": "s1"})


def test_append_log_skips_broken_lines(tmp_path):
    """坏行跳过而不是抛异常：一个手抖写坏的行不该让整个平面读不出来。"""
    log = AppendLog(tmp_path / "plane.jsonl")
    log.append({"kind": "a"})
    with log.path.open("a", encoding="utf-8") as handle:
        handle.write("这不是 JSON\n")
    log.append({"kind": "b"})
    assert [item["kind"] for item in log.read_all()] == ["a", "b"]


# ── 统一时间线 ──────────────────────────────────────────────────────────────


def _settings(tmp_path) -> Settings:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    return settings


async def _seed(store: Store) -> None:
    """造一场会话：两条消息 + 一个任务 + 两个任务事件。"""
    await store.save(
        Conversation(id="c1", title="统一平面", created_at="2026-01-01T00:00:00+00:00",
                     updated_at="2026-01-01T00:00:09+00:00")
    )
    await store.save(
        Message(id="m1", conversation_id="c1", role="user", content="帮我整理周报",
                created_at="2026-01-01T00:00:01+00:00")
    )
    await store.save(
        Message(id="m2", conversation_id="c1", role="assistant", content="好的，先读文件",
                created_at="2026-01-01T00:00:05+00:00")
    )
    await store.save(
        Task(id="t1", conversation_id="c1", goal="整理周报", created_at="2026-01-01T00:00:02+00:00")
    )
    await store.save(
        TaskEvent(id=1, task_id="t1", event_type="started", payload={"step": 1},
                  created_at="2026-01-01T00:00:03+00:00")
    )
    await store.save(
        TaskEvent(id=2, task_id="t1", event_type="finished", payload={"ok": True},
                  created_at="2026-01-01T00:00:08+00:00")
    )
    # 最后显式回写 updated_at：`Store.save` 每存一条消息都会把会话的 updated_at 刷成
    # "现在"，不回写的话 prune 的时间过滤会永远匹配不到（"看起来没命中"最难排查）。
    async with store.sessions() as session:
        conversation = await session.get(Conversation, "c1")
        assert conversation is not None
        conversation.updated_at = "2026-01-01T00:00:09+00:00"
        await session.commit()


async def test_timeline_merges_three_sources_in_time_order(tmp_path):
    """三个来源合并后按时间排序，且一条都不丢。"""
    settings = _settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    await _seed(store)

    plane = SessionPlane(settings.data_dir)
    plane.append({"kind": "session.created", "session_id": "c1", "ts": "2026-01-01T00:00:00+00:00"})
    plane.append({"kind": "session.resumed", "session_id": "c1", "ts": "2026-01-01T00:00:07+00:00"})

    entries = await plane.timeline("c1", store=store)
    await store.engine.dispose()

    assert len(entries) == 6, f"2 条平面 + 2 条消息 + 2 条任务事件，实际 {len(entries)}"
    assert {item.source for item in entries} == {"plane", "message", "task_event"}
    stamps = [to_epoch(item.ts) for item in entries]
    assert stamps == sorted(stamps), "时间线必须按时间排序"
    assert [item.kind for item in entries] == [
        "session.created",
        "user",
        "started",
        "assistant",
        "session.resumed",
        "finished",
    ]
    # 正文与回跳 id 都在：时间线是"看全"，不是"看个类型名"。
    assert any("整理周报" in item.text for item in entries)
    assert any(item.ref == "m2" for item in entries)


async def test_timeline_works_without_a_store(tmp_path):
    """没有数据库时只返回平面事件——平面不该比它合并的东西更脆弱。"""
    plane = _plane(tmp_path)
    plane.append({"kind": "session.created", "session_id": "c1"})
    entries = await plane.timeline("c1")
    assert [item.source for item in entries] == ["plane"]


async def test_timeline_limit_keeps_the_newest(tmp_path):
    settings = _settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    await _seed(store)
    plane = SessionPlane(settings.data_dir)
    entries = await plane.timeline("c1", store=store, limit=2)
    await store.engine.dispose()
    assert [item.kind for item in entries] == ["assistant", "finished"]


async def test_timeline_does_not_leak_other_sessions(tmp_path):
    """别的会话的消息/任务不能混进来（join 写错就会漏）。"""
    settings = _settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    await _seed(store)
    await store.save(Conversation(id="c2", title="另一个会话"))
    await store.save(
        Message(id="m9", conversation_id="c2", role="user", content="无关内容",
                created_at="2026-01-01T00:00:04+00:00")
    )
    plane = SessionPlane(settings.data_dir)
    entries = await plane.timeline("c1", store=store)
    await store.engine.dispose()
    assert all(item.session_id == "c1" for item in entries)
    assert all(item.ref != "m9" for item in entries)


# ── 压缩谱系 ────────────────────────────────────────────────────────────────


def test_lineage_walks_up_to_the_root(tmp_path):
    """c3 ← c2 ← c1：上溯到根，深度与摘要字符数都带上。"""
    plane = _plane(tmp_path)
    plane.compress("c2", "c1", summary_chars=1200, ts="2026-01-01T00:00:00+00:00")
    plane.compress("c3", "c2", summary_chars=800, ts="2026-01-01T00:00:01+00:00")

    chain = plane.lineage("c3")
    assert [item["session_id"] for item in chain] == ["c3", "c2", "c1"]
    assert [item["depth"] for item in chain] == [0, 1, 2]
    assert chain[0]["parent"] == "c2"
    assert chain[1]["summary_chars"] == 1200
    assert chain[-1]["parent"] is None, "根没有父会话"
    assert plane.parent_of("c3") == "c2"


def test_lineage_of_a_root_is_a_single_entry(tmp_path):
    plane = _plane(tmp_path)
    plane.append({"kind": "session.created", "session_id": "c1"})
    chain = plane.lineage("c1")
    assert len(chain) == 1
    assert chain[0]["parent"] is None


def test_lineage_detects_a_cycle(tmp_path):
    """a → b → a：必须**报错**，不能把命令挂死在死循环里。"""
    plane = _plane(tmp_path)
    plane.compress("a", "b")
    plane.compress("b", "a")

    with pytest.raises(SessionPlaneError) as excinfo:
        plane.lineage("a")
    assert "循环引用" in str(excinfo.value)


def test_lineage_detects_self_reference(tmp_path):
    """自己指向自己也是环（最小的一种，最容易漏判）。"""
    plane = _plane(tmp_path)
    plane.compress("a", "a")
    with pytest.raises(SessionPlaneError):
        plane.lineage("a")


def test_lineage_last_compression_wins(tmp_path):
    """同一会话被压缩两次时，谱系取**最后一次**（否则上溯会走进历史分支）。"""
    plane = _plane(tmp_path)
    plane.compress("c2", "c1", summary_chars=100, ts="2026-01-01T00:00:00+00:00")
    plane.compress("c2", "c9", summary_chars=200, ts="2026-01-01T00:00:01+00:00")
    chain = plane.lineage("c2")
    assert chain[0]["parent"] == "c9"
    assert chain[0]["summary_chars"] == 200


def test_compressed_event_is_readable_as_a_plane_event(tmp_path):
    """谱系事件本身就是普通平面事件（kind=session.compressed），时间线上能看见。"""
    plane = _plane(tmp_path)
    event = plane.compress("c2", "c1", summary="[CONTEXT COMPACTION] 摘要…", summary_chars=9)
    assert event["kind"] == KIND_COMPRESSED
    assert event["parent_session_id"] == "c1"
    assert event["compressed_at"]
    stored = plane.events("c2")
    assert stored and stored[0]["summary"].startswith("[CONTEXT COMPACTION")


def test_plane_file_is_plain_jsonl(tmp_path):
    """落盘是**一行一条 JSON**：能用 jq / tail 直接看，不需要解析器。"""
    plane = _plane(tmp_path)
    plane.append({"kind": "session.created", "session_id": "s1"})
    lines = plane.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["session_id"] == "s1"


# ── CLI：orbit plane … 与 sessions import/prune ──────────────────────────────


def _argv(settings: Settings, *rest: str) -> list[str]:
    return ["--data-dir", str(settings.data_dir), *rest]


def test_plane_is_registered_with_its_subcommands():
    """`plane` 是 Orbit 自有顶层命令；`sessions import/prune` 已接管为 done。"""
    command = REGISTRY.get("plane")
    assert command is not None
    names = [sub.name for sub in command.subcommands]
    assert names == ["timeline", "tail", "lineage", "show", "resume", "reap", "deliveries"]
    assert all(sub.handler is not None for sub in command.subcommands)

    sessions = REGISTRY.get("sessions")
    assert sessions is not None
    for leaf in ("import", "prune"):
        child = sessions.child(leaf)
        assert child is not None
        assert child.state == "done", f"sessions {leaf} 应该已被本模块接管"
    # 已被 sessions.py 占用的六条不能被重复接管（重复登记会抛 ValueError）。
    assert plane_cli.__name__.endswith("session_plane")


async def test_cli_plane_timeline(tmp_path, capsys):
    """`plane timeline` 把三个来源合并后打到 stdout / JSON。"""
    settings = _settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    await _seed(store)
    await store.engine.dispose()
    SessionPlane(settings.data_dir).append(
        {"kind": "session.created", "session_id": "c1", "ts": "2026-01-01T00:00:00+00:00"}
    )

    assert main(_argv(settings, "--json", "plane", "timeline", "c1")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 5
    assert {item["source"] for item in payload["entries"]} == {
        "plane",
        "message",
        "task_event",
    }


async def test_cli_plane_lineage_reports_a_cycle_as_an_error(tmp_path, capsys):
    """谱系有环时返回码 1 + 一句人话（不挂死、不打栈）。"""
    settings = _settings(tmp_path)
    plane = SessionPlane(settings.data_dir)
    plane.compress("a", "b")
    plane.compress("b", "a")

    assert main(_argv(settings, "plane", "lineage", "a")) == 1
    assert "循环引用" in capsys.readouterr().err


async def test_cli_plane_lineage_prints_the_chain(tmp_path, capsys):
    settings = _settings(tmp_path)
    SessionPlane(settings.data_dir).compress("c2", "c1", summary_chars=42)
    assert main(_argv(settings, "--json", "plane", "lineage", "c2")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["depth"] == 2
    assert payload["lineage"][0]["summary_chars"] == 42


async def test_cli_plane_deliveries_reads_the_ledger(tmp_path, capsys):
    """`plane deliveries` 在空账本上也要能跑（返回 0 + 0 个 key）。"""
    settings = _settings(tmp_path)
    assert main(_argv(settings, "--json", "plane", "deliveries")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stats"]["keys"] == 0

    from app.delivery_ledger import DeliveryLedger

    ledger = DeliveryLedger(settings.data_dir)
    ledger.claim("k1", platform="ntfy", channel="c1")
    ledger.mark("k1", "delivered", attempts=1)

    assert main(_argv(settings, "--json", "plane", "deliveries")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stats"]["delivered"] == 1
    assert payload["records"][-1]["message_key"] == "k1"


async def test_cli_plane_reap_dry_run(tmp_path, capsys):
    """`plane reap --dry-run` 只报告，不动账本。"""
    settings = _settings(tmp_path)
    assert main(_argv(settings, "--json", "plane", "reap", "--dry-run")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["orphan_count"] == 0


async def test_cli_plane_reap_rejects_conflicting_flags(tmp_path, capsys):
    settings = _settings(tmp_path)
    assert main(_argv(settings, "plane", "reap", "--dry-run", "--apply")) == 1
    assert "互斥" in capsys.readouterr().err


async def test_cli_plane_tail(tmp_path, capsys):
    settings = _settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    await _seed(store)
    await store.engine.dispose()
    assert main(_argv(settings, "--json", "plane", "tail", "c1", "-n", "2")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 2


async def test_cli_plane_show(tmp_path, capsys):
    settings = _settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    await _seed(store)
    await store.engine.dispose()
    assert main(_argv(settings, "--json", "plane", "show", "c1")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["messages"] == 2
    assert payload["tasks"] == 1


async def test_cli_sessions_list_still_works(tmp_path, capsys):
    """接管了新路径之后，`sessions list`（sessions.py 的实现）不能被碰坏。"""
    settings = _settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    await _seed(store)
    await store.engine.dispose()

    assert main(_argv(settings, "--json", "sessions", "list")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["sessions"][0]["id"] == "c1"


async def test_cli_sessions_prune_dry_run(tmp_path, capsys):
    settings = _settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    await _seed(store)
    await store.engine.dispose()

    # 会话更新于 2026-01-01，用"1 秒内"的窗口筛不到它；用 0 秒窗口 + 未来时间点不现实，
    # 所以直接断言"老会话会被 --older-than 命中"，用足够大的天数。
    assert main(_argv(settings, "--json", "sessions", "prune", "--older-than", "1", "--dry-run")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["dry_run"] is True
    assert payload["count"] == 1, "2026-01-01 的会话相对今天一定老于 1 天"

    # dry-run 不删：会话还在。
    store = Store(settings.data_dir / "app.sqlite")
    assert await store.get(Conversation, "c1") is not None
    await store.engine.dispose()


async def test_cli_sessions_prune_requires_yes(tmp_path, capsys):
    """真删必须显式 ``--yes``：会话是用户唯一的原始记录，误删不可恢复。"""
    settings = _settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    await _seed(store)
    await store.engine.dispose()

    assert main(_argv(settings, "sessions", "prune", "--older-than", "1")) == 1
    assert "--yes" in capsys.readouterr().err

    store = Store(settings.data_dir / "app.sqlite")
    assert await store.get(Conversation, "c1") is not None, "被拒绝时不能删任何东西"
    await store.engine.dispose()


async def test_cli_sessions_prune_deletes_with_yes_and_logs_a_plane_event(tmp_path, capsys):
    settings = _settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    await _seed(store)
    await store.engine.dispose()

    assert main(_argv(settings, "--json", "sessions", "prune", "--older-than", "1", "--yes")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["removed"] == 1

    store = Store(settings.data_dir / "app.sqlite")
    assert await store.get(Conversation, "c1") is None
    await store.engine.dispose()

    kinds = [item["kind"] for item in SessionPlane(settings.data_dir).events("c1")]
    assert "session.pruned" in kinds, "删除也要在平面上留痕（审计要能回答'谁删了它'）"


async def test_cli_sessions_prune_rejects_unimplemented_filters(tmp_path, capsys):
    """表里有、Orbit 库没有对应列的过滤条件必须**报未实现**，不能静默忽略。"""
    settings = _settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    await _seed(store)
    await store.engine.dispose()

    assert main(_argv(settings, "sessions", "prune", "--model", "gpt-5", "--yes")) == 1
    captured = capsys.readouterr().err
    assert "未实现" in captured
    assert "--model" in captured


async def test_cli_sessions_import_creates_a_conversation(tmp_path, capsys):
    """`sessions import` 吃 Orbit 自己导出的 JSONL，并在平面上留痕。"""
    settings = _settings(tmp_path)
    source = tmp_path / "session.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps({"session_id": "x", "title": "导入的会话"}, ensure_ascii=False),
                json.dumps(
                    {"id": "1", "role": "user", "created_at": "2026-01-02T00:00:00+00:00",
                     "content": "第一句"},
                    ensure_ascii=False,
                ),
                json.dumps(
                    {"id": "2", "role": "assistant", "created_at": "2026-01-02T00:00:01+00:00",
                     "content": "第二句"},
                    ensure_ascii=False,
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert main(_argv(settings, "--json", "sessions", "import", str(source))) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["messages"] == 2
    assert payload["title"] == "导入的会话"

    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.get(Conversation, payload["id"])
    assert conversation is not None and conversation.title == "导入的会话"
    await store.engine.dispose()

    kinds = [item["kind"] for item in SessionPlane(settings.data_dir).events(payload["id"])]
    assert "session.imported" in kinds


async def test_cli_sessions_import_without_a_path_explains_the_usage(tmp_path, capsys):
    settings = _settings(tmp_path)
    assert main(_argv(settings, "sessions", "import")) == 1
    assert "用法" in capsys.readouterr().err


async def test_cli_sessions_import_of_an_empty_file_fails(tmp_path, capsys):
    settings = _settings(tmp_path)
    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert main(_argv(settings, "sessions", "import", str(empty))) == 1
    assert "没有可导入的消息" in capsys.readouterr().err
