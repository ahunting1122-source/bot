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


def test_production_registers_the_four_tools(tmp_path):
    """**生产装配**：`create_app()` 之后这 4 个真的在注册表里。"""
    from starlette.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    settings = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "w")
    settings.prepare()
    app = create_app(settings=settings)
    with TestClient(app):
        names = app.state.kanban_tools
        assert set(names) == {
            "kanban_create",
            "kanban_list",
            "kanban_show",
            "kanban_complete",
        }, names
