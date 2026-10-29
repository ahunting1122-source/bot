"""看板工具（MATRIX 5.14 的 `kanban_*` 一组，本轮 4 个）。

## 为什么只做 4 个

Hermes 有 14 个 `kanban_*`，后 10 个各自带一套状态机与并发语义
（交审→退回的循环 / 阻塞的依赖判定 / heartbeat 超时窗口 / 附件的 base64 与落点）。
**一次铺开会留下「注册了但没验过」的工具——接上了但有洞比没接上更危险。**

## 与 `todo` 的分工

`todo` 是**单个任务自己的**清单（「我这一步要做啥」）；
看板是**跨 run、跨 agent** 的任务板（「谁在做什么、谁等谁」）。
"""

from __future__ import annotations

import pytest


@pytest.fixture()
def board(tmp_path):
    from app import kanban, tool_kanban

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    tool_kanban.configure(data_dir)
    yield kanban, data_dir
    tool_kanban.reset()


def test_create_and_show_roundtrip(board):
    kanban, data_dir = board
    card = kanban.create(data_dir, "写第三集大纲", body="柠檬主角", assignee="alice")
    assert card.id == "k-0001"
    assert kanban.show(data_dir, card.id).body == "柠檬主角"


def test_ids_are_unique_and_increment(board):
    kanban, data_dir = board
    first = kanban.create(data_dir, "一")
    second = kanban.create(data_dir, "二")
    assert first.id != second.id


def test_a_child_card_requires_an_existing_parent(board):
    """**依赖必须真的存在**——指向一张不存在的父卡，会在读的时候变成悬空引用。"""
    kanban, data_dir = board
    with pytest.raises(kanban.KanbanError):
        kanban.create(data_dir, "子卡", parent="k-9999")


def test_list_filters_by_status_and_assignee(board):
    kanban, data_dir = board
    kanban.create(data_dir, "甲", assignee="alice")
    done = kanban.create(data_dir, "乙", assignee="bob")
    kanban.complete(data_dir, done.id, handoff="做完了")

    assert len(kanban.list_cards(data_dir)) == 2
    assert len(kanban.list_cards(data_dir, status=kanban.STATUS_DONE)) == 1
    assert len(kanban.list_cards(data_dir, assignee="alice")) == 1


def test_complete_records_the_handoff(board):
    kanban, data_dir = board
    card = kanban.create(data_dir, "交出去")
    done = kanban.complete(data_dir, card.id, handoff="产出在 out/a.md；还差评审")
    assert done.status == kanban.STATUS_DONE
    assert "还差评审" in kanban.show(data_dir, card.id).handoff


def test_completing_twice_is_refused(board):
    """**重复完成不是幂等**——它通常意味着两个 agent 在推同一张卡。

    静默吞掉会掩盖「有人在重复劳动」，而那件事应该被看见。
    """
    kanban, data_dir = board
    card = kanban.create(data_dir, "只该做一次")
    kanban.complete(data_dir, card.id, handoff="第一次")
    with pytest.raises(kanban.KanbanError) as excinfo:
        kanban.complete(data_dir, card.id, handoff="第二次")
    assert "两个 agent" in str(excinfo.value)


def test_a_broken_board_file_reads_as_empty(board):
    """**坏文件当空板**：看板坏了不该让工具直接崩。"""
    kanban, data_dir = board
    (data_dir / kanban.BOARD_DIRNAME).mkdir(parents=True, exist_ok=True)
    (data_dir / kanban.BOARD_DIRNAME / kanban.BOARD_FILENAME).write_text(
        "{ not json", encoding="utf-8"
    )
    assert kanban.load_board(data_dir) == []


async def test_the_tools_dispatch_end_to_end(board):
    from app import tool_kanban

    _kanban, _data_dir = board
    created = await tool_kanban.execute(
        "kanban_create", {"title": "从工具建卡"}, "task-x", "op-1"
    )
    assert created["ok"] is True
    card_id = created["card"]["id"]

    listed = await tool_kanban.execute("kanban_list", {}, "task-x", "op-2")
    assert listed["count"] == 1
    # 摘要不带 body（那是 `show` 的事）
    assert "body" not in listed["cards"][0]

    shown = await tool_kanban.execute("kanban_show", {"id": card_id}, "task-x", "op-3")
    assert shown["card"]["title"] == "从工具建卡"

    done = await tool_kanban.execute(
        "kanban_complete", {"id": card_id, "handoff": "ok"}, "task-x", "op-4"
    )
    assert done["card"]["status"] == "done"


def test_without_wiring_nothing_is_registered():
    """**没接线就一个都不注册**——注册一个必然抛错的工具比不注册更糟。"""
    from app import tool_kanban

    class _Registry:
        def __init__(self):
            self.tools: dict = {}

        def register(self, tools, executor, source=None):
            for tool in tools:
                self.tools[tool.name] = executor

    tool_kanban.reset()
    registry = _Registry()
    names = tool_kanban.register_kanban_tools(registry)
    assert names == []
    assert registry.tools == {}

# ── 第二轮：comment / block / unblock / heartbeat ──────────────────────────


def test_a_comment_is_persisted_on_the_card(board):
    """评论是**留在卡上的记录**，不是"再发一次消息"。"""
    kanban, data_dir = board
    card = kanban.create(data_dir, "要留话的卡")
    kanban.comment(data_dir, card.id, "我先看了，卡在等 API key", author="alice")

    stored = kanban.show(data_dir, card.id)
    assert len(stored.comments) == 1
    assert "等 API key" in stored.comments[0]["text"]
    assert stored.comments[0]["author"] == "alice"


def test_an_empty_comment_is_refused(board):
    kanban, data_dir = board
    card = kanban.create(data_dir, "空评论")
    with pytest.raises(kanban.KanbanError):
        kanban.comment(data_dir, card.id, "   ")


def test_block_records_the_reason_as_a_comment(board):
    kanban, data_dir = board
    card = kanban.create(data_dir, "会被卡住")
    blocked = kanban.block(data_dir, card.id, reason="等父卡")

    assert blocked.status == kanban.STATUS_BLOCKED
    assert any("[blocked]" in item["text"] for item in blocked.comments)


def test_a_done_card_cannot_be_blocked(board):
    """**完成了就是完成了**——把它退回 blocked 会让下游看到「已完成又阻塞」的卡。"""
    kanban, data_dir = board
    card = kanban.create(data_dir, "已做完")
    kanban.complete(data_dir, card.id, handoff="ok")
    with pytest.raises(kanban.KanbanError) as excinfo:
        kanban.block(data_dir, card.id)
    assert "已完成又阻塞" in str(excinfo.value)


def test_unblock_needs_the_parent_done(board):
    """**父卡没完成就解阻塞 = 死循环**：会被重新认领，然后又卡在同一个依赖上。"""
    kanban, data_dir = board
    parent = kanban.create(data_dir, "父卡")
    child = kanban.create(data_dir, "子卡", parent=parent.id)
    kanban.block(data_dir, child.id, reason="等父")

    with pytest.raises(kanban.KanbanError) as excinfo:
        kanban.unblock(data_dir, child.id)
    assert parent.id in str(excinfo.value)
    # 仍然 blocked，且留下了一条说明是哪张父卡挡着
    still = kanban.show(data_dir, child.id)
    assert still.status == kanban.STATUS_BLOCKED
    assert any("[still-blocked]" in item["text"] for item in still.comments)

    # 父卡做完之后就能解了
    kanban.complete(data_dir, parent.id, handoff="父做完")
    assert kanban.unblock(data_dir, child.id).status == kanban.STATUS_OPEN


def test_unblock_refuses_a_card_that_is_not_blocked(board):
    kanban, data_dir = board
    card = kanban.create(data_dir, "没被卡过")
    with pytest.raises(kanban.KanbanError):
        kanban.unblock(data_dir, card.id)


def test_heartbeat_only_touches_updated_at(board):
    """heartbeat 让 orchestrator 分辨「在跑」与「跑挂了」——**状态不能变**。"""
    kanban, data_dir = board
    card = kanban.create(data_dir, "在跑")
    before = kanban.show(data_dir, card.id)
    after = kanban.heartbeat(data_dir, card.id)
    assert after.status == before.status
    assert after.updated_at >= before.updated_at


def test_heartbeat_refuses_a_done_card(board):
    """已 done 的卡打 heartbeat 会让它看起来还在跑。"""
    kanban, data_dir = board
    card = kanban.create(data_dir, "已完")
    kanban.complete(data_dir, card.id, handoff="ok")
    with pytest.raises(kanban.KanbanError):
        kanban.heartbeat(data_dir, card.id)


async def test_the_four_new_tools_dispatch(board):
    from app import tool_kanban

    _kanban, _data_dir = board
    created = await tool_kanban.execute("kanban_create", {"title": "走一遍"}, "t", "o1")
    card_id = created["card"]["id"]

    commented = await tool_kanban.execute(
        "kanban_comment", {"id": card_id, "text": "留句话"}, "t", "o2"
    )
    assert commented["card"]["comments"]

    blocked = await tool_kanban.execute(
        "kanban_block", {"id": card_id, "reason": "等依赖"}, "t", "o3"
    )
    assert blocked["card"]["status"] == "blocked"

    unblocked = await tool_kanban.execute("kanban_unblock", {"id": card_id}, "t", "o4")
    assert unblocked["card"]["status"] == "open"

    beat = await tool_kanban.execute("kanban_heartbeat", {"id": card_id}, "t", "o5")
    assert beat["card"]["status"] == "open"


def test_production_registers_all_eight(tmp_path):
    """**生产装配**：`create_app()` 之后 8 个看板工具真的都在注册表里。"""
    from starlette.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    settings = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "w")
    settings.prepare()
    app = create_app(settings=settings)
    with TestClient(app):
        names = set(app.state.kanban_tools)
        expected = {
            "kanban_create",
            "kanban_list",
            "kanban_show",
            "kanban_complete",
            "kanban_comment",
            "kanban_block",
            "kanban_unblock",
            "kanban_heartbeat",
        }
        assert names == expected, names
