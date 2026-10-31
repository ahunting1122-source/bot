"""`app/tool_ui.py` 的测试（`5.14` 的 `desktop` 域 4 条：UI 状态）。"""

from __future__ import annotations

import json

import pytest

from app.tool_ui import LAYOUT_PRESETS, PANES, UI_TOOLS, UiStateTool, load_ui_state


@pytest.fixture()
def ui(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return UiStateTool(data_dir), data_dir


def test_four_tools_are_declared():
    assert [t.name for t in UI_TOOLS] == ["focus_pane", "apply_layout", "tip", "tour"]


async def test_focus_sets_the_pane_and_persists(ui):
    tool, data_dir = ui
    result = await tool.execute("focus_pane", {"pane": "preview"}, "t", "o1")
    assert result["status"] == "ok"
    assert result["ui"]["focused_pane"] == "preview"
    # **换一个实例读** —— 证明是落盘的
    assert load_ui_state(data_dir)["focused_pane"] == "preview"


async def test_focus_rejects_an_unknown_pane(ui):
    """**拼错的面板名会让前端静默什么都不高亮** —— 所以这里要拒绝并列出可选值。"""
    tool, _data_dir = ui
    result = await tool.execute("focus_pane", {"pane": "preveiw"}, "t", "o")
    assert result["status"] == "error"
    assert "preview" in result["error"], "错误里要列出可选面板"


async def test_apply_layout_preset_expands_to_real_panes(ui):
    tool, _data_dir = ui
    result = await tool.execute("apply_layout", {"preset": "split"}, "t", "o")
    assert result["status"] == "ok"
    panes = result["ui"]["layout"]["panes"]
    assert [p["id"] for p in panes] == ["chat", "preview"]
    assert sum(p["size"] for p in panes) == 100
    assert result["ui"]["layout"]["preset"] == "split"


async def test_apply_layout_accepts_custom_panes(ui):
    tool, _data_dir = ui
    result = await tool.execute(
        "apply_layout",
        {"panes": [{"id": "chat", "size": 70}, {"id": "files", "size": 30}]},
        "t",
        "o",
    )
    assert result["status"] == "ok"
    assert result["ui"]["layout"]["preset"] == "custom"


async def test_apply_layout_rejects_a_bad_preset(ui):
    tool, _data_dir = ui
    result = await tool.execute("apply_layout", {"preset": "nope"}, "t", "o")
    assert result["status"] == "error"
    assert "single" in result["error"]


async def test_apply_layout_rejects_unknown_pane_in_custom(ui):
    tool, _data_dir = ui
    result = await tool.execute("apply_layout", {"panes": [{"id": "nope", "size": 10}]}, "t", "o")
    assert result["status"] == "error"


async def test_apply_layout_needs_preset_or_panes(ui):
    tool, _data_dir = ui
    result = await tool.execute("apply_layout", {}, "t", "o")
    assert result["status"] == "error"
    assert "preset" in result["error"]


async def test_tip_is_anchored_and_has_an_expiry(ui):
    tool, _data_dir = ui
    result = await tool.execute(
        "tip", {"target": "composer", "text": "在这里输入", "placement": "top"}, "t", "o"
    )
    assert result["status"] == "ok"
    tips = result["ui"]["tips"]
    assert len(tips) == 1
    assert tips[0]["target"] == "composer"
    assert tips[0]["expires_at"] > 0, "气泡必须有过期时间"


async def test_a_second_tip_on_the_same_target_replaces_the_first(ui):
    """**同一点叠两个气泡会互相挡住** —— 新气泡撤掉旧的。"""
    tool, _data_dir = ui
    await tool.execute("tip", {"target": "composer", "text": "旧"}, "t", "o1")
    result = await tool.execute("tip", {"target": "composer", "text": "新"}, "t", "o2")
    tips = result["ui"]["tips"]
    assert len(tips) == 1
    assert tips[0]["text"] == "新"


async def test_tip_rejects_a_bad_placement(ui):
    tool, _data_dir = ui
    result = await tool.execute("tip", {"target": "x", "text": "y", "placement": "middle"}, "t", "o")
    assert result["status"] == "error"


async def test_expired_tips_are_filtered_on_read(ui):
    """**过期气泡不该继续显示** —— 读侧过滤，不需要定时器。"""
    tool, data_dir = ui
    await tool.execute("tip", {"target": "a", "text": "短", "ttl_seconds": 1}, "t", "o")
    state_file = data_dir / "ui" / "state.json"
    state = json.loads(state_file.read_text(encoding="utf-8"))
    state["tips"][0]["expires_at"] = 0  # 手动让它过期
    state_file.write_text(json.dumps(state), encoding="utf-8")
    assert load_ui_state(data_dir)["tips"] == []


async def test_tour_starts_at_step_zero(ui):
    tool, _data_dir = ui
    steps = [
        {"target": "composer", "title": "第一步", "body": "在这里说话"},
        {"target": "sidebar", "title": "第二步", "body": "这里是历史"},
    ]
    result = await tool.execute("tour", {"action": "start", "steps": steps}, "t", "o")
    assert result["status"] == "ok"
    assert result["ui"]["tour"]["index"] == 0
    assert result["ui"]["tour"]["active"] is True


async def test_tour_next_advances_and_finishing_clears_it(ui):
    """**走完最后一步要自动结束** —— 停在最后一步会让人以为还欠一步。"""
    tool, _data_dir = ui
    steps = [{"target": "a", "title": "1"}, {"target": "b", "title": "2"}]
    await tool.execute("tour", {"action": "start", "steps": steps}, "t", "o0")

    after_next = await tool.execute("tour", {"action": "next"}, "t", "o1")
    assert after_next["ui"]["tour"]["index"] == 1

    finished = await tool.execute("tour", {"action": "next"}, "t", "o2")
    assert "tour" not in finished["ui"], "走到最后一步之后应该结束"


async def test_tour_prev_clamps_at_zero(ui):
    tool, _data_dir = ui
    await tool.execute("tour", {"action": "start", "steps": [{"target": "a"}, {"target": "b"}]}, "t", "o0")
    result = await tool.execute("tour", {"action": "prev"}, "t", "o1")
    assert result["ui"]["tour"]["index"] == 0


async def test_tour_stop_clears_it(ui):
    tool, _data_dir = ui
    await tool.execute("tour", {"action": "start", "steps": [{"target": "a"}]}, "t", "o0")
    result = await tool.execute("tour", {"action": "stop"}, "t", "o1")
    assert "tour" not in result["ui"]


async def test_tour_start_needs_steps(ui):
    tool, _data_dir = ui
    result = await tool.execute("tour", {"action": "start"}, "t", "o")
    assert result["status"] == "error"
    assert "steps" in result["error"]


async def test_tour_next_without_a_tour_is_an_error(ui):
    tool, _data_dir = ui
    result = await tool.execute("tour", {"action": "next"}, "t", "o")
    assert result["status"] == "error"
    assert "导览" in result["error"]


def test_pane_and_preset_tables_are_non_empty():
    """前端 `PANE_LABELS` 与这里的 `PANES` 要能对上 —— 空表会让 UI 条渲染不出来。"""
    assert len(PANES) >= 5
    assert set(LAYOUT_PRESETS) == {"single", "split", "triple", "review"}
    for panes in LAYOUT_PRESETS.values():
        assert all(pid in PANES for pid, _size in panes), "预设里出现了不在 PANES 的面板"
