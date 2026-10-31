"""`app/tool_desktop_extra.py` 的测试（`5.14` 桌面域最后 4 条）。"""

from __future__ import annotations

import json

from app import providers as pv
from app.tool_desktop_extra import (
    DESKTOP_EXTRA_TOOLS,
    DesktopExtraTool,
    load_terminal_views,
    register_terminal_view,
)


class _Secret:
    def __init__(self, value: str = "") -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


class FakeSettings:
    def __init__(self, model: str = "gpt-4o", key: str = "sk-test") -> None:
        self.model = model
        self.model_base_url = "https://example.invalid/v1"
        self.model_provider = "openai"
        self.model_api_key = _Secret(key)


class FakeReply:
    def __init__(self, content) -> None:
        self.content = content


class FakeClient:
    def __init__(self, content=None, *, boom: Exception | None = None) -> None:
        self.content = content if content is not None else "一张网页截图"
        self.boom = boom
        self.calls: list = []

    async def ainvoke(self, messages):
        self.calls.append(messages)
        if self.boom:
            raise self.boom
        return FakeReply(self.content)


class FakeBrowser:
    def __init__(self, *, shot: str | None = "data:image/png;base64,AAA", boom: bool = False) -> None:
        self.shot = shot
        self.boom = boom
        self.observed = 0
        self.executed: list[tuple[str, dict]] = []

    def last_screenshot_data_url(self):
        return self.shot

    async def observe(self, task_id):
        self.observed += 1
        self.shot = self.shot or "data:image/png;base64,BBB"

    async def execute(self, name, args, task_id, operation_id):
        if self.boom:
            raise RuntimeError("浏览器拒绝")
        self.executed.append((name, dict(args)))
        return {"ok": True}


def test_four_tools_are_declared():
    assert [t.name for t in DESKTOP_EXTRA_TOOLS] == [
        "close_terminal",
        "drive_preview",
        "browser_vision",
        "vision_analyze",
    ]


# ---------- close_terminal ----------


async def test_close_terminal_without_views_is_a_noop(tmp_path):
    tool = DesktopExtraTool(tmp_path)
    result = await tool.execute("close_terminal", {}, "t", "o")
    assert result["status"] == "ok"
    assert result["closed"] == []


async def test_close_terminal_removes_one_view_and_says_the_process_lives(tmp_path):
    """**关视图 ≠ 杀进程** —— 返回里要明说进程还在跑，否则模型会以为它停了。"""
    register_terminal_view(tmp_path, {"id": "v1", "pid": 111})
    register_terminal_view(tmp_path, {"id": "v2", "pid": 222})
    tool = DesktopExtraTool(tmp_path)

    result = await tool.execute("close_terminal", {"view_id": "v1"}, "t", "o")

    assert result["status"] == "ok"
    assert result["closed"] == ["v1"]
    assert result["process_still_running"] is True
    assert [v["id"] for v in load_terminal_views(tmp_path)] == ["v2"]


async def test_close_terminal_all_clears_everything(tmp_path):
    register_terminal_view(tmp_path, {"id": "v1"})
    register_terminal_view(tmp_path, {"id": "v2"})
    tool = DesktopExtraTool(tmp_path)
    result = await tool.execute("close_terminal", {"all": True}, "t", "o")
    assert sorted(result["closed"]) == ["v1", "v2"]
    assert load_terminal_views(tmp_path) == []


async def test_close_terminal_rejects_an_unknown_id_and_lists_the_real_ones(tmp_path):
    register_terminal_view(tmp_path, {"id": "v1"})
    tool = DesktopExtraTool(tmp_path)
    result = await tool.execute("close_terminal", {"view_id": "nope"}, "t", "o")
    assert result["status"] == "error"
    assert "v1" in result["error"], "错误里要列出当前存在的视图"


async def test_close_terminal_needs_an_id_or_all(tmp_path):
    register_terminal_view(tmp_path, {"id": "v1"})
    tool = DesktopExtraTool(tmp_path)
    result = await tool.execute("close_terminal", {}, "t", "o")
    assert result["status"] == "error"
    assert "all" in result["error"]


def test_register_terminal_view_is_idempotent(tmp_path):
    register_terminal_view(tmp_path, {"id": "v1", "pid": 1})
    register_terminal_view(tmp_path, {"id": "v1", "pid": 2})
    views = load_terminal_views(tmp_path)
    assert len(views) == 1 and views[0]["pid"] == 2


# ---------- drive_preview ----------


async def test_drive_preview_forwards_the_action_to_the_browser(tmp_path):
    browser = FakeBrowser()
    tool = DesktopExtraTool(tmp_path, browser=browser)
    result = await tool.execute(
        "drive_preview", {"action": "click", "target": "#submit"}, "t", "o"
    )
    assert result["status"] == "ok"
    assert browser.executed == [("click", {"target": "#submit"})]


async def test_drive_preview_without_a_browser_is_an_error(tmp_path):
    tool = DesktopExtraTool(tmp_path)
    result = await tool.execute("drive_preview", {"action": "click"}, "t", "o")
    assert result["status"] == "error"
    assert "浏览器层" in result["error"]


async def test_drive_preview_surfaces_a_browser_refusal(tmp_path):
    tool = DesktopExtraTool(tmp_path, browser=FakeBrowser(boom=True))
    result = await tool.execute("drive_preview", {"action": "click"}, "t", "o")
    assert result["status"] == "error"
    assert "拒绝" in result["error"]


# ---------- browser_vision ----------


async def test_browser_vision_sends_the_screenshot_to_the_model(tmp_path, monkeypatch):
    client = FakeClient("页面上有个登录框")
    monkeypatch.setattr(pv, "resolve_provider", lambda s: object())
    monkeypatch.setattr(pv, "build_chat_model", lambda *a, **k: client)

    tool = DesktopExtraTool(tmp_path, settings=FakeSettings(), browser=FakeBrowser())
    result = await tool.execute("browser_vision", {"prompt": "看到什么"}, "t", "o")

    assert result["status"] == "ok"
    assert result["text"] == "页面上有个登录框"
    # **截图真的进了消息里**，而且是以 image_url 块的形式
    payload = client.calls[0][0][1]
    assert payload[0]["type"] == "text"
    assert payload[1]["image_url"]["url"].startswith("data:image/png")


async def test_browser_vision_takes_a_screenshot_when_there_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(pv, "resolve_provider", lambda s: object())
    monkeypatch.setattr(pv, "build_chat_model", lambda *a, **k: FakeClient("ok"))
    browser = FakeBrowser(shot=None)
    tool = DesktopExtraTool(tmp_path, settings=FakeSettings(), browser=browser)

    result = await tool.execute("browser_vision", {}, "t", "o")

    assert result["status"] == "ok"
    assert browser.observed == 1, "没有截图时应先 observe 一张"


async def test_browser_vision_without_a_model_config_is_actionable(tmp_path):
    tool = DesktopExtraTool(tmp_path, settings=FakeSettings(model=""), browser=FakeBrowser())
    result = await tool.execute("browser_vision", {}, "t", "o")
    assert result["status"] == "error"
    assert "settings.model" in result["error"]


async def test_browser_vision_without_a_browser_is_an_error(tmp_path):
    tool = DesktopExtraTool(tmp_path, settings=FakeSettings())
    result = await tool.execute("browser_vision", {}, "t", "o")
    assert result["status"] == "error"
    assert "浏览器层" in result["error"]


async def test_browser_vision_flattens_a_content_block_reply(tmp_path, monkeypatch):
    """**有些 provider 回的是内容块列表** —— 要拼成一段文本，别把结构丢给模型。"""
    monkeypatch.setattr(pv, "resolve_provider", lambda s: object())
    monkeypatch.setattr(
        pv, "build_chat_model", lambda *a, **k: FakeClient([{"text": "第一段"}, {"text": "第二段"}])
    )
    tool = DesktopExtraTool(tmp_path, settings=FakeSettings(), browser=FakeBrowser())
    result = await tool.execute("browser_vision", {}, "t", "o")
    assert result["text"] == "第一段第二段"


async def test_browser_vision_surfaces_a_model_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(pv, "resolve_provider", lambda s: object())
    monkeypatch.setattr(
        pv, "build_chat_model", lambda *a, **k: FakeClient(boom=RuntimeError("401"))
    )
    tool = DesktopExtraTool(tmp_path, settings=FakeSettings(), browser=FakeBrowser())
    result = await tool.execute("browser_vision", {}, "t", "o")
    assert result["status"] == "error"
    assert "模型调用失败" in result["error"]


# ---------- vision_analyze ----------


async def test_vision_analyze_reads_a_local_file_as_a_data_url(tmp_path, monkeypatch):
    client = FakeClient("一只猫")
    monkeypatch.setattr(pv, "resolve_provider", lambda s: object())
    monkeypatch.setattr(pv, "build_chat_model", lambda *a, **k: client)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "cat.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    tool = DesktopExtraTool(tmp_path, settings=FakeSettings(), workspace=workspace)

    result = await tool.execute("vision_analyze", {"image": "cat.png"}, "t", "o")

    assert result["status"] == "ok"
    url = client.calls[0][0][1][1]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")


async def test_vision_analyze_passes_a_url_straight_through(tmp_path, monkeypatch):
    client = FakeClient("远程图")
    monkeypatch.setattr(pv, "resolve_provider", lambda s: object())
    monkeypatch.setattr(pv, "build_chat_model", lambda *a, **k: client)
    tool = DesktopExtraTool(tmp_path, settings=FakeSettings(), workspace=tmp_path)

    result = await tool.execute(
        "vision_analyze", {"image": "https://example.com/a.jpg"}, "t", "o"
    )

    assert result["status"] == "ok"
    assert client.calls[0][0][1][1]["image_url"]["url"] == "https://example.com/a.jpg"


async def test_vision_analyze_rejects_a_missing_file(tmp_path):
    tool = DesktopExtraTool(tmp_path, settings=FakeSettings(), workspace=tmp_path)
    result = await tool.execute("vision_analyze", {"image": "nope.png"}, "t", "o")
    assert result["status"] == "error"
    assert "不存在" in result["error"]


async def test_vision_analyze_maps_jpg_to_jpeg_mime(tmp_path, monkeypatch):
    client = FakeClient("x")
    monkeypatch.setattr(pv, "resolve_provider", lambda s: object())
    monkeypatch.setattr(pv, "build_chat_model", lambda *a, **k: client)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.jpg").write_bytes(b"\xff\xd8\xff")
    tool = DesktopExtraTool(tmp_path, settings=FakeSettings(), workspace=workspace)

    await tool.execute("vision_analyze", {"image": "a.jpg"}, "t", "o")

    url = client.calls[0][0][1][1]["image_url"]["url"]
    assert url.startswith("data:image/jpeg;base64,"), "jpg 要映射成 jpeg 的 MIME"


def test_terminal_views_file_is_json(tmp_path):
    register_terminal_view(tmp_path, {"id": "v1"})
    raw = (tmp_path / "terminal" / "views.json").read_text(encoding="utf-8")
    assert json.loads(raw)[0]["id"] == "v1"
