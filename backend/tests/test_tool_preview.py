"""`app/tool_preview.py` 的测试（`5.14` 的 `desktop` 域 4 条）。"""

from __future__ import annotations

import json

import pytest

from app.tool_preview import PREVIEW_TOOLS, PreviewTool


@pytest.fixture()
def tool(tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "note.md").write_text("# 标题\n正文", encoding="utf-8")
    return PreviewTool(data_dir, workspace), data_dir, workspace


def test_five_tools_are_declared():
    """`open_preview` 是 `desktop_preview` 的同义工具 —— **同一个处理器的第二个名字**。"""
    assert [t.name for t in PREVIEW_TOOLS] == [
        "desktop_preview",
        "open_preview",
        "read_preview",
        "annotate_preview",
        "close_preview",
    ]


def test_open_preview_is_an_alias_of_desktop_preview(tool):
    """**两者行为必须完全一致** —— 否则就是两个名字两套逻辑，迟早漂移。"""
    preview, _data_dir, workspace = tool
    opened = preview.execute("open_preview", {"target": str(workspace / "note.md")}, "t", "o")
    assert opened["status"] == "ok"
    read = preview.execute("read_preview", {}, "t", "o")
    assert "# 标题" in read["content"], "open_preview 打开的东西 read_preview 读不到"


def test_open_a_local_file_records_it(tool):
    preview, data_dir, workspace = tool
    result = preview.execute("desktop_preview", {"target": str(workspace / "note.md")}, "t", "o")
    assert result["status"] == "ok"
    assert result["preview"]["title"] == "note.md"
    # **状态落盘** —— 跨调用存活
    assert json.loads((data_dir / "preview" / "state.json").read_text(encoding="utf-8"))


def test_open_a_missing_file_is_an_error(tool):
    preview, _data_dir, workspace = tool
    result = preview.execute("desktop_preview", {"target": str(workspace / "nope.md")}, "t", "o")
    assert result["status"] == "error"
    assert "文件不存在" in result["error"]


def test_open_a_url_is_accepted(tool):
    preview, _data_dir, _ws = tool
    result = preview.execute("desktop_preview", {"target": "https://example.com/x"}, "t", "o")
    assert result["status"] == "ok"
    assert result["preview"]["is_url"] is True


def test_read_returns_the_file_body(tool):
    preview, _data_dir, workspace = tool
    preview.execute("desktop_preview", {"target": str(workspace / "note.md")}, "t", "o")
    result = preview.execute("read_preview", {}, "t", "o")
    assert result["status"] == "ok"
    assert result["kind"] == "file"
    assert "# 标题" in result["content"]
    assert result["truncated"] is False


def test_read_truncates_and_says_so(tool):
    """**截断要说出来** —— 静默截断会让模型以为「就这么多」。"""
    preview, _data_dir, workspace = tool
    (workspace / "big.txt").write_text("x" * 5000, encoding="utf-8")
    preview.execute("desktop_preview", {"target": str(workspace / "big.txt")}, "t", "o")
    result = preview.execute("read_preview", {"limit": 100}, "t", "o")
    assert len(result["content"]) == 100
    assert result["truncated"] is True
    assert result["chars"] == 5000, "chars 应是全长，不是返回长度"


def test_read_a_url_does_not_fetch_it(tool):
    """**后端不抓远端内容** —— 说了就写清楚，别让模型以为拿到了正文。"""
    preview, _data_dir, _ws = tool
    preview.execute("desktop_preview", {"target": "https://example.com/x"}, "t", "o")
    result = preview.execute("read_preview", {}, "t", "o")
    assert result["kind"] == "url"
    assert "不抓取远端内容" in result["note"]


def test_read_without_an_open_preview_is_an_error(tool):
    preview, _data_dir, _ws = tool
    result = preview.execute("read_preview", {}, "t", "o")
    assert result["status"] == "error"
    assert "没有打开的预览" in result["error"]


def test_annotations_persist_across_calls(tool):
    preview, data_dir, workspace = tool
    preview.execute("desktop_preview", {"target": str(workspace / "note.md")}, "t", "o")
    preview.execute("annotate_preview", {"note": "这里要改"}, "t", "o")

    # **换一个实例读** —— 证明是落盘的，不是内存里的
    again = PreviewTool(data_dir, workspace)
    result = again.execute("read_preview", {}, "t", "o")
    assert [a["note"] for a in result["annotations"]] == ["这里要改"]


def test_annotate_without_an_open_preview_is_an_error(tool):
    preview, _data_dir, _ws = tool
    result = preview.execute("annotate_preview", {"note": "x"}, "t", "o")
    assert result["status"] == "error"


def test_close_clears_the_state(tool):
    preview, data_dir, workspace = tool
    preview.execute("desktop_preview", {"target": str(workspace / "note.md")}, "t", "o")
    preview.execute("close_preview", {}, "t", "o")
    assert not (data_dir / "preview" / "state.json").exists()
    assert preview.execute("read_preview", {}, "t", "o")["status"] == "error"
