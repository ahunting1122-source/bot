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

import base64
import pathlib

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


def test_unblock_downgrades_instead_of_refusing(board):
    """**父卡没完成时降级、不拒绝**（Hermes：父任务未完成则回 todo）。

    第一版**报错拒绝**是个死胡同：用户除了先做父卡没有别的路，而**父卡可能被别人卡着**
    ——于是这条卡谁也推不动，也没人能表达「我知道它还等着，但我先把它放回池子」。

    **降级既不拒绝（无死胡同），也不 ready（无死循环）**——所以**不加 --force**：
    强制放回可认领状态会制造我原本担心的那个死循环（重新认领→又卡住→再 block→再 force）。
    """
    kanban, data_dir = board
    parent = kanban.create(data_dir, "父卡")
    child = kanban.create(data_dir, "子卡", parent=parent.id)
    kanban.block(data_dir, child.id, reason="等父")

    released = kanban.unblock(data_dir, child.id)
    assert released.status == kanban.STATUS_OPEN, "降级后应放回池子"
    # 但依赖必须留在卡上，否则下一个认领的人看不到它还在等
    assert any("[dependency]" in item["text"] for item in released.comments)
    assert parent.id in " ".join(item["text"] for item in released.comments)


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

# ── 第三轮：link / request_review / request_changes ────────────────────────


def test_link_attaches_a_parent_to_an_existing_card(board):
    kanban, data_dir = board
    parent = kanban.create(data_dir, "父")
    child = kanban.create(data_dir, "子")
    linked = kanban.link(data_dir, child.id, parent.id)
    assert linked.first_parent == parent.id


def test_link_refuses_a_cycle(board):
    """**成环不会报错，会让拓扑序永远算不完**——死锁比报错更难查。"""
    kanban, data_dir = board
    first = kanban.create(data_dir, "A")
    second = kanban.create(data_dir, "B")
    kanban.link(data_dir, second.id, first.id)  # A → B

    with pytest.raises(kanban.KanbanError) as excinfo:
        kanban.link(data_dir, first.id, second.id)  # B → A 会成环
    assert "环" in str(excinfo.value)
    # 数据没被改坏
    assert kanban.show(data_dir, first.id).first_parent == ""


def test_link_refuses_a_card_as_its_own_parent(board):
    kanban, data_dir = board
    card = kanban.create(data_dir, "自环")
    with pytest.raises(kanban.KanbanError):
        kanban.link(data_dir, card.id, card.id)


def test_request_review_moves_the_card_to_in_review(board):
    kanban, data_dir = board
    card = kanban.create(data_dir, "要交审")
    reviewed = kanban.request_review(data_dir, card.id, note="请看一下")
    assert reviewed.status == kanban.STATUS_REVIEW
    assert any("[review]" in item["text"] for item in reviewed.comments)


def test_a_done_card_cannot_be_sent_to_review(board):
    """**done 是终点**——再交审会让 orchestrator 看到「已完成又在等人审」。"""
    kanban, data_dir = board
    card = kanban.create(data_dir, "已完")
    kanban.complete(data_dir, card.id, handoff="ok")
    with pytest.raises(kanban.KanbanError):
        kanban.request_review(data_dir, card.id)


def test_a_blocked_card_cannot_be_sent_to_review(board):
    """blocked 要先把阻塞解掉——否则会看到「又阻塞又在等人审」。"""
    kanban, data_dir = board
    card = kanban.create(data_dir, "卡住的")
    kanban.block(data_dir, card.id, reason="等依赖")
    with pytest.raises(kanban.KanbanError):
        kanban.request_review(data_dir, card.id)


def test_request_changes_sends_it_back_with_the_required_changes(board):
    kanban, data_dir = board
    card = kanban.create(data_dir, "要改")
    kanban.request_review(data_dir, card.id)
    returned = kanban.request_changes(data_dir, card.id, changes="第三节要加数据")
    assert returned.status == kanban.STATUS_OPEN
    assert any("第三节要加数据" in item["text"] for item in returned.comments)


def test_request_changes_needs_a_non_empty_reason(board):
    """**退回却不说是改什么，等于把问题原样丢回去。**"""
    kanban, data_dir = board
    card = kanban.create(data_dir, "空退回")
    kanban.request_review(data_dir, card.id)
    with pytest.raises(kanban.KanbanError):
        kanban.request_changes(data_dir, card.id, changes="   ")


def test_only_an_in_review_card_can_be_returned(board):
    """从 done 退回会抹掉已完成的记录。"""
    kanban, data_dir = board
    card = kanban.create(data_dir, "没交审")
    with pytest.raises(kanban.KanbanError) as excinfo:
        kanban.request_changes(data_dir, card.id, changes="改点东西")
    assert "in_review" in str(excinfo.value)


async def test_the_three_new_tools_dispatch(board):
    from app import tool_kanban

    _kanban, _data_dir = board
    a = (await tool_kanban.execute("kanban_create", {"title": "A"}, "t", "o1"))["card"]["id"]
    b = (await tool_kanban.execute("kanban_create", {"title": "B"}, "t", "o2"))["card"]["id"]

    linked = await tool_kanban.execute(
        "kanban_link", {"child_id": b, "parent_id": a}, "t", "o3"
    )
    assert linked["card"]["parent"] == a

    reviewed = await tool_kanban.execute(
        "kanban_request_review", {"id": b, "note": "看一下"}, "t", "o4"
    )
    assert reviewed["card"]["status"] == "in_review"

    returned = await tool_kanban.execute(
        "kanban_request_changes", {"id": b, "changes": "补测试"}, "t", "o5"
    )
    assert returned["card"]["status"] == "open"

# ── 第四轮：attach / attachments ───────────────────────────────────────────


def test_attach_writes_the_file_and_stores_a_relative_path(board):
    """**卡上只存相对路径**——存绝对路径会让整块板在换数据目录后全部失效，
    而那种失效**不会报错，只会让附件「凭空消失」**。"""
    kanban, data_dir = board
    card = kanban.create(data_dir, "要挂附件")
    attached = kanban.attach(data_dir, card.id, "note.txt", b"hello")

    assert len(attached.attachments) == 1
    item = attached.attachments[0]
    assert not pathlib.PurePath(item["path"]).is_absolute(), item["path"]
    assert (pathlib.Path(data_dir) / item["path"]).read_bytes() == b"hello"
    assert item["bytes"] == 5


def test_attachments_returns_absolute_paths(board):
    """列附件时**还原成绝对路径**——这个工具的用途就是「告诉我附件在哪」。"""
    kanban, data_dir = board
    card = kanban.create(data_dir, "查附件")
    kanban.attach(data_dir, card.id, "a.bin", b"xyz")

    items = kanban.attachments(data_dir, card.id)
    assert len(items) == 1
    assert pathlib.Path(items[0]["path"]).is_absolute()
    assert pathlib.Path(items[0]["path"]).read_bytes() == b"xyz"


def test_attach_strips_directories_from_the_name(board):
    """**名字来自模型，不能直接当文件名**：`../` 会写到附件目录外。"""
    kanban, data_dir = board
    card = kanban.create(data_dir, "路径穿越")
    attached = kanban.attach(data_dir, card.id, "../../evil.txt", b"x")
    assert attached.attachments[0]["name"] == "evil.txt"
    # 落在附件目录里，不在数据目录外
    stored = pathlib.Path(data_dir) / attached.attachments[0]["path"]
    assert kanban.ATTACH_DIRNAME in stored.parts
    assert stored.read_bytes() == b"x"


def test_attach_refuses_dot_and_dotdot_names(board):
    kanban, data_dir = board
    card = kanban.create(data_dir, "坏名字")
    for bad in (".", "..", ""):
        with pytest.raises(kanban.KanbanError):
            kanban.attach(data_dir, card.id, bad, b"x")


def test_reattaching_the_same_name_replaces_the_record(board):
    """同名覆盖时**替换那条记录**而不是追加两条——否则列出来有两个同路径条目。"""
    kanban, data_dir = board
    card = kanban.create(data_dir, "覆盖")
    kanban.attach(data_dir, card.id, "same.txt", b"first")
    again = kanban.attach(data_dir, card.id, "same.txt", b"second!")
    assert len(again.attachments) == 1
    assert again.attachments[0]["bytes"] == 7


async def test_the_two_attachment_tools_dispatch(board):
    from app import tool_kanban

    _kanban, _data_dir = board
    created = await tool_kanban.execute("kanban_create", {"title": "挂"}, "t", "o1")
    card_id = created["card"]["id"]

    payload = base64.b64encode(b"hi there").decode()
    attached = await tool_kanban.execute(
        "kanban_attach",
        {"id": card_id, "name": "hi.txt", "content_base64": payload},
        "t",
        "o2",
    )
    assert attached["card"]["attachments"][0]["name"] == "hi.txt"

    listed = await tool_kanban.execute("kanban_attachments", {"id": card_id}, "t", "o3")
    assert listed["count"] == 1
    assert pathlib.Path(listed["attachments"][0]["path"]).is_absolute()


async def test_a_broken_base64_is_refused_with_a_clear_reason(board):
    from app import tool_kanban

    _kanban, _data_dir = board
    created = await tool_kanban.execute("kanban_create", {"title": "坏编码"}, "t", "o1")
    card_id = created["card"]["id"]
    with pytest.raises(ValueError) as excinfo:
        await tool_kanban.execute(
            "kanban_attach",
            {"id": card_id, "name": "x.bin", "content_base64": "!!!not-base64!!!"},
            "t",
            "o2",
        )
    assert "base64" in str(excinfo.value)


def test_production_registers_every_declared_kanban_tool(tmp_path):
    """**生产装配**：`create_app()` 之后**声明的每一个**看板工具都在注册表里。

    **不写死数字**：从 `KANBAN_TOOLS` 实际长度派生。
    写死数字的话，每加一个工具就要改一次测试 —— 而这个家族已经加过 4 次。
    """
    from starlette.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    settings = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "w")
    settings.prepare()
    app = create_app(settings=settings)
    with TestClient(app):
        from app.tool_kanban import KANBAN_TOOLS

        names = set(app.state.kanban_tools)
        declared = {tool.name for tool in KANBAN_TOOLS}
        assert names == declared, f"声明了 {sorted(declared)}，注册了 {sorted(names)}"
        assert "kanban_attach" in names
        assert "kanban_attachments" in names


# ── 认领闸门：死循环那一半（第 46 轮）─────────────────────────────────────


def test_claim_refuses_when_the_parent_is_not_done(board):
    """**闸门在「认领」这一步，不在「状态值」**（MATRIX 5.14 第 46 轮）。

    上一版把 unblock 改成「父卡没完成也放回 open」——**关掉了死胡同**，
    **但没关掉死循环**：open 是可认领的，那张卡仍会被认领、又卡在同一依赖上。
    而 [dependency] 那条评论**只是记录，不是闸门**。

    **判据：闸门要放在危害真正发生的那一步。** 危害发生在「认领」。
    """
    kanban, data_dir = board
    parent = kanban.create(data_dir, "父卡")
    child = kanban.create(data_dir, "子卡", parent=parent.id)
    kanban.block(data_dir, child.id, reason="等父")
    kanban.unblock(data_dir, child.id)  # 放回 open（父卡仍未完成）

    with pytest.raises(kanban.KanbanError) as excinfo:
        kanban.claim(data_dir, child.id)
    assert parent.id in str(excinfo.value)

    # 父卡做完之后就能认领了
    kanban.complete(data_dir, parent.id, handoff="父做完")
    assert kanban.claim(data_dir, child.id, assignee="alice").status == kanban.STATUS_DOING


def test_list_reports_ready_so_discovery_does_not_mislead(board):
    """kanban_list 是 orchestrator **发现可路由的活**的那一步。

    只报 status 的话，一张「open 但父卡没 done」的卡会被当成可接的——
    **把判据提前到发现那一步，错误就不会发生**（而不是发生后再报错）。
    """
    kanban, data_dir = board
    parent = kanban.create(data_dir, "父")
    child = kanban.create(data_dir, "子", parent=parent.id)

    rows = {row["id"]: row for row in kanban.summarize(kanban.list_cards(data_dir))}
    assert rows[parent.id]["ready"] is True
    assert rows[child.id]["ready"] is False
    assert rows[child.id]["blocked_by"] == [parent.id]


def test_claim_refuses_a_card_that_is_not_open(board):
    kanban, data_dir = board
    card = kanban.create(data_dir, "已做")
    kanban.complete(data_dir, card.id, handoff="ok")
    with pytest.raises(kanban.KanbanError):
        kanban.claim(data_dir, card.id)


# ── 多父依赖：link 是「加一条边」不是「覆盖」（第 47 轮）──────────────────


def test_link_adds_an_edge_instead_of_overwriting(board):
    """**link 的语义是「加一条依赖边」**（Hermes kanban_link），边可以有多条。

    原来的 parent 是单个字符串，link() 就是**覆盖**：link(A,B) 之后
    link(A,C) 会把 B 那条边**悄悄丢掉**（实测确认过）。**那不是加边，是数据丢失。**
    而数据丢失在这里**不会报错**——A 只是少了一个前驱，看起来一切正常。
    """
    kanban, data_dir = board
    a = kanban.create(data_dir, "A")
    b = kanban.create(data_dir, "B")
    c = kanban.create(data_dir, "C")
    kanban.link(data_dir, a.id, b.id)
    linked = kanban.link(data_dir, a.id, c.id)

    assert set(linked.parents) == {b.id, c.id}, linked.parents
    # 派生 parent 仍是第一个（兼容旧读法）
    assert linked.first_parent == b.id


def test_both_parents_block_the_card(board):
    """**依赖判定走全集**：只看第一个前驱会漏掉另一条。"""
    kanban, data_dir = board
    a = kanban.create(data_dir, "A")
    b = kanban.create(data_dir, "B")
    c = kanban.create(data_dir, "C")
    kanban.link(data_dir, a.id, b.id)
    kanban.link(data_dir, a.id, c.id)

    # 只做完 B，C 还没做 → 仍然不能认领
    kanban.complete(data_dir, b.id, handoff="B 好了")
    with pytest.raises(kanban.KanbanError) as excinfo:
        kanban.claim(data_dir, a.id)
    assert c.id in str(excinfo.value)

    # C 也做完 → 才能认领
    kanban.complete(data_dir, c.id, handoff="C 好了")
    assert kanban.claim(data_dir, a.id).status == kanban.STATUS_DOING


def test_linking_the_same_edge_twice_is_idempotent(board):
    """重复加同一条边**不是错误**，只是重放——但也不能产生重复项。"""
    kanban, data_dir = board
    a = kanban.create(data_dir, "A")
    b = kanban.create(data_dir, "B")
    kanban.link(data_dir, a.id, b.id)
    twice = kanban.link(data_dir, a.id, b.id)
    assert twice.parents == [b.id]


def test_an_old_board_with_a_single_parent_still_reads(board):
    """**旧板只有 parent 字符串**：读的时候升级成列表，不迁移文件。"""
    import json

    kanban, data_dir = board
    path = data_dir / kanban.BOARD_DIRNAME / kanban.BOARD_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "cards": [
                    {"id": "k-0001", "title": "旧卡", "parent": "k-0002"},
                    {"id": "k-0002", "title": "旧父"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    old = kanban.show(data_dir, "k-0001")
    assert old.parents == ["k-0002"]
    assert old.first_parent == "k-0002"

def test_list_separates_waiting_on_parent_from_an_explicit_block(board):
    """**两种「不可开工」的原因必须分开**（第 47 轮）。

    只报 `ready=False` 会把「等前驱做完」和「有人显式叫停」混成一件——
    而处理方式完全不同：前者再等等就好，后者要人/agent 去解。
    """
    kanban, data_dir = board
    parent = kanban.create(data_dir, "父")
    waiting = kanban.create(data_dir, "等父卡", parent=parent.id)
    stopped = kanban.create(data_dir, "被叫停")
    kanban.block(data_dir, stopped.id, reason="等评审")

    rows = {row["id"]: row for row in kanban.summarize(kanban.list_cards(data_dir))}
    assert rows[waiting.id]["blocked_reason"] == "waiting_on_parent"
    assert rows[stopped.id]["blocked_reason"] == "blocked"
    # 父卡自己是可以开工的
    assert rows[parent.id]["blocked_reason"] == ""
    assert rows[parent.id]["ready"] is True

def test_cycle_detection_finds_a_cycle_on_the_second_parent(board):
    """**环只在「第二个前驱」那条路上可达**——这条才证明是 DFS（第 48 轮）。

    ## 第一版这条测试是**摆设**，实测发现的

    第一版构造的是 `C.parents=[A]`、`B.parents=[C]`，再 `link(C→B)`。
    但环检测从 `B` 出发**第一跳就撞上 `C`** —— **DFS 一次都没被用到**。
    我把实现降级成「只看 `parents[0]`」再跑，**它照样过**。→ 摆设。

    ## 能区分的构造

    ```
    M.parents = [A, X]   ← M 有两个前驱，X 在**第二个**
    Y.parents = [M]      ← Y → M
    link(X → Y)          ← 检查：从 Y 上溯能否到 X
    ```

    - **DFS**：Y → M → 展开 M 的全部前驱 {A, X} → **撞到 X** → 报环 ✓
    - **只看第一个前驱**：Y → M → `parents[0]` = A → A 无父 → 停 → **漏检** ✗

    建边过程本身没有环（X 无父、A 无父），所以这个环**完全由 `link(X→Y)` 造成**。
    """
    kanban, data_dir = board
    a = kanban.create(data_dir, "A")
    x = kanban.create(data_dir, "X")
    m = kanban.create(data_dir, "M")
    y = kanban.create(data_dir, "Y")

    # M 先依赖 A（成为 parents[0]），再依赖 X（成为 parents[1]）
    kanban.link(data_dir, m.id, a.id)
    kanban.link(data_dir, m.id, x.id)
    assert kanban.show(data_dir, m.id).parents == [a.id, x.id], "X 必须落在第二个前驱"

    # Y 依赖 M
    kanban.link(data_dir, y.id, m.id)

    # 现在让 X 依赖 Y → X → Y → M → X 成环，**而这条环只在 M 的第二个前驱上可达**
    with pytest.raises(kanban.KanbanError) as excinfo:
        kanban.link(data_dir, x.id, y.id)
    assert "环" in str(excinfo.value)

    # 数据没被改坏
    assert kanban.show(data_dir, x.id).parents == []

def test_cycle_detection_walks_the_whole_chain_not_just_two_levels(board):
    """**环只在第 5 层可达**——这条防的是「BFS 但只走两层」那种假 DFS（第 49 轮）。

    上一条测试（环在第二个前驱）能挡住「只看 `parents[0]`」的实现，
    **但挡不住「走了前驱、但只走两层」的实现**——那种在上一条的构造里也能撞上 X。

    这里把环藏到第 5 层：
    ```
    N1.parents = [N2]
    N2.parents = [N3]
    N3.parents = [N4]
    N4.parents = [N5]
    N5.parents = [N1]      ← 已经成环？不行，那是建边时就该被拒
    ```
    所以换个方向：先建一条**长链**，再让**链头**依赖**链尾**——
    检查从链尾上溯必须走满 4 跳才撞到链头。
    """
    kanban, data_dir = board
    nodes = [kanban.create(data_dir, f"N{i}") for i in range(1, 6)]

    # N1 → N2 → N3 → N4 → N5（N1 是链头，N5 是链尾）
    for child, parent in zip(nodes[1:], nodes[:4], strict=False):
        kanban.link(data_dir, child.id, parent.id)

    # 让链头依赖链尾 → 成环，**从 N1 上溯要跨 4 条边才回到 N5→N1**
    with pytest.raises(kanban.KanbanError) as excinfo:
        kanban.link(data_dir, nodes[0].id, nodes[4].id)
    assert "环" in str(excinfo.value)
    assert kanban.show(data_dir, nodes[0].id).parents == []
