"""四个新浏览器工具（``browser_back`` / ``browser_get_images`` / ``browser_console`` /
``browser_dialog``，MATRIX 5.14 的「可做但未做」桶）。

分两层，两层都要说清自己到底测到了什么：

* **纯逻辑层**：图片过滤与相对 URL 转换、console 缓冲裁剪、dialog 状态机、监听器挂载
  时机、工具归类。**不启浏览器、不联网**，必跑；
* **真浏览器层**：真 chromium + 本地 127.0.0.1 站点。起不来就 ``pytest.skip`` 并写明
  原因——拿替身冒充"已验证真浏览器行为"比 skip 糟得多。

为什么真浏览器用例要用本地 HTTP 站点而不是 ``data:`` URL：``page.go_back()`` 只有在
真有 HTTP 响应时才返回 ``Response``（``data:`` 导航返回 ``None``），而"历史栈里有没有
上一页"这件事也只有在真导航下才成立。站点跑在 loopback 上，不碰外网。
"""

import asyncio
import functools
import os
import threading
from contextlib import asynccontextmanager, suppress
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

import app.browser as browser_module
from app import toolsets
from app.browser import (
    CONSOLE_BUFFER,
    CONSOLE_DEFAULT_LIMIT,
    CONSOLE_TEXT_CHARS,
    IMAGES_DEFAULT_LIMIT,
    BrowserController,
    ConsoleLog,
    DialogLog,
    normalize_images,
)
from app.config import Settings
from app.tools import schema_problems

#: 本文件负责的四个工具。以它们为准做登记 / 归类 / schema 三重检查。
NEW_TOOLS = ("browser_back", "browser_get_images", "browser_console", "browser_dialog")


class FakeStore:
    """只实现工具路径真正用到的方法：测试不关心落库，只关心工具回了什么。"""

    def __init__(self):
        self.saved = []

    async def save(self, row):
        self.saved.append(row)
        return row

    async def event(self, *args, **kwargs):
        pass

    async def get(self, model, key):
        return None


class RecordingPage:
    """记录事件订阅的假页面：验证"监听器是在页面创建时挂上的"，不需要真浏览器。"""

    def __init__(self, url: str = "https://example.com/"):
        self.url = url
        self.handlers = {}
        self.attachments = 0

    def on(self, event, handler):
        self.attachments += 1
        self.handlers[event] = handler

    def is_closed(self):
        return False


class FakeDialog:
    """playwright Dialog 的最小替身：只记"被怎么处理了"。"""

    def __init__(self, kind: str, message: str, default_value: str = ""):
        self.type = kind
        self.message = message
        self.default_value = default_value
        self.handled: list = []

    async def accept(self, text=None):
        self.handled.append(("accept", text))

    async def dismiss(self):
        self.handled.append(("dismiss", None))


def make_browser(tmp_path):
    return BrowserController(Settings(data_dir=tmp_path, headless=True), FakeStore())


# ══ 1. 登记 / 归类 / schema（离线）═══════════════════════════════════════════


def test_new_tools_are_registered_with_valid_schemas():
    """四个工具都得进 ``BROWSER_TOOLS``，且 schema 是供应商能吃的形状。"""
    by_name = {tool.name: tool for tool in browser_module.BROWSER_TOOLS}
    for name in NEW_TOOLS:
        assert name in by_name, f"{name} 没有登记进 BROWSER_TOOLS"
        tool = by_name[name]
        assert tool.description.strip()
        assert schema_problems(tool.specification()) == []


def test_toolset_of_classifies_new_tools_as_browser():
    """``browser_`` 前缀应当自动归到 browser toolset（不需要动 toolsets.py 的映射表）。"""
    for name in NEW_TOOLS:
        assert toolsets.toolset_of(name) == "browser"
        assert toolsets.domain_of_tool(name) == "browser"


# ══ 2. console：有界缓冲 / 丢最旧 / 只留三个字段（离线）═════════════════════


def test_console_buffer_drops_the_oldest_when_full():
    log = ConsoleLog(capacity=3)
    for index in range(5):
        log.add("log", f"第{index}条")
    snapshot = log.snapshot(limit=10)
    assert [item["text"] for item in snapshot["entries"]] == ["第2条", "第3条", "第4条"]
    assert snapshot["dropped"] == 2
    assert snapshot["total"] == 3


def test_console_default_buffer_is_bounded():
    """默认容量必须有界：无限缓冲会把长会话的内存吃光。"""
    log = ConsoleLog()
    assert log.capacity == CONSOLE_BUFFER
    for index in range(CONSOLE_BUFFER + 5):
        log.add("log", str(index))
    assert len(log.entries) == CONSOLE_BUFFER
    assert log.dropped == 5
    assert log.tail(2)[0]["text"] == str(CONSOLE_BUFFER + 5 - 2)


def test_console_entries_keep_only_level_text_and_time():
    """一条记录只保留 level / text / 时间——不把 ConsoleMessage 或 JSHandle 交出去。"""
    log = ConsoleLog()
    log.add("warning", "x" * (CONSOLE_TEXT_CHARS + 500))
    entry = log.tail(1)[0]
    assert set(entry) == {"level", "text", "at"}
    assert entry["level"] == "warning"
    assert len(entry["text"]) == CONSOLE_TEXT_CHARS


def test_console_snapshot_honours_the_limit():
    log = ConsoleLog()
    for index in range(10):
        log.add("log", str(index))
    snapshot = log.snapshot(limit=CONSOLE_DEFAULT_LIMIT)
    assert snapshot["count"] == 10
    assert [item["text"] for item in log.snapshot(limit=3)["entries"]] == ["7", "8", "9"]


# ══ 3. 图片：相对转绝对 / 追踪像素 / limit / 去重（离线）════════════════════


def test_images_turn_relative_urls_into_absolute():
    """相对地址必须转成绝对：模型拿到 ``/img/a.png`` 不知道该拼哪个站点。"""
    result = normalize_images(
        [
            {"src": "img/a.png", "alt": "图", "width": 640, "height": 480},
            {"src": "/b.png", "alt": "", "width": 10, "height": 10},
            {"src": "https://cdn.example.com/c.png", "alt": "", "width": 10, "height": 10},
        ],
        "https://example.com/news/1.html",
    )
    assert [item["src"] for item in result["images"]] == [
        "https://example.com/news/img/a.png",
        "https://example.com/b.png",
        "https://cdn.example.com/c.png",
    ]


def test_images_skip_tracking_pixels():
    """宽高都 ≤2 的图（1×1 统计像素、0×0 没加载出来的图）一律过滤。"""
    result = normalize_images(
        [
            {"src": "px.gif", "alt": "", "width": 1, "height": 1},
            {"src": "zero.png", "alt": "", "width": 0, "height": 0},
            {"src": "big.png", "alt": "", "width": 3, "height": 2},
        ],
        "https://example.com/",
    )
    assert [item["src"] for item in result["images"]] == ["https://example.com/big.png"]
    assert result["skipped"]["tracking_pixel"] == 2


def test_images_respect_limit_and_dedupe():
    raw = [{"src": f"{index}.png", "alt": "", "width": 8, "height": 8} for index in range(5)]
    raw.append({"src": "0.png", "alt": "重复", "width": 8, "height": 8})
    result = normalize_images(raw, "https://example.com/", limit=2)
    assert [item["src"] for item in result["images"]] == [
        "https://example.com/0.png",
        "https://example.com/1.png",
    ]
    assert result["count"] == 2
    assert result["skipped"]["duplicate"] == 1
    assert result["skipped"]["over_limit"] == 3


def test_images_drop_absurdly_long_sources():
    """base64 内联图原样进上下文等于当场自爆预算，必须挡在工具层。"""
    result = normalize_images(
        [{"src": "data:image/png;base64," + "A" * 5000, "alt": "", "width": 4, "height": 4}],
        "https://example.com/",
    )
    assert result["images"] == []
    assert result["skipped"]["too_long"] == 1


def test_images_default_limit_matches_the_tool_default():
    assert browser_module.ImagesArgs().limit == IMAGES_DEFAULT_LIMIT == 30


# ══ 4. dialog 状态机（离线，不用真浏览器）═══════════════════════════════════


def test_dialog_log_tracks_pending_until_resolved():
    log = DialogLog()
    log.record("confirm", "确定删除？")
    log.bind(object())
    assert log.snapshot()["pending"] is True
    assert log.take() is not None
    log.resolve("accept")
    snapshot = log.snapshot()
    assert snapshot["pending"] is False
    assert snapshot["dialog"] is None
    assert snapshot["recent"][-1]["action"] == "accept"
    assert snapshot["recent"][-1]["message"] == "确定删除？"


def test_dialog_log_history_is_bounded():
    log = DialogLog(capacity=2)
    for index in range(3):
        log.record("alert", str(index))
    assert len(log.entries) == 2
    assert log.snapshot()["total"] == 2


async def test_dialog_tool_rejects_an_unknown_action(tmp_path):
    browser = make_browser(tmp_path)
    with pytest.raises(ValueError, match="accept"):
        await browser.handle_dialog({"action": "click"})


async def test_dialog_tool_reports_honestly_when_nothing_is_pending(tmp_path):
    """没有 dialog 时必须如实报 handled=false，不能静默成功。"""
    browser = make_browser(tmp_path)
    result = await browser.handle_dialog({"action": "accept"})
    assert result["status"] == "ok"
    assert result["data"]["handled"] is False
    assert result["data"]["pending"] is False
    assert "没有待处理" in result["data"]["message"]


async def test_dialog_tool_accepts_a_pending_dialog_with_prompt_text(tmp_path, monkeypatch):
    """prompt 的 text 要真的传下去（其余类型不带文本调用）。"""
    monkeypatch.setattr(browser_module, "DIALOG_GRACE_SECONDS", 0.05)
    browser = make_browser(tmp_path)
    page = RecordingPage()
    browser.new_page(page)
    dialog = FakeDialog("prompt", "名字？", "默认")
    page.handlers["dialog"](dialog)
    assert browser.page_events().dialogs.snapshot()["pending"] is True

    result = await browser.handle_dialog({"action": "accept", "text": "张三"})
    assert result["data"]["handled"] is True
    assert result["data"]["dialog"]["type"] == "prompt"
    assert dialog.handled == [("accept", "张三")]
    # 已经被模型处理掉，兜底任务不该再动它
    await asyncio.sleep(0.15)
    assert dialog.handled == [("accept", "张三")]


async def test_dialog_falls_back_to_dismiss_when_nobody_handles_it(tmp_path, monkeypatch):
    """没人处理时自动 dismiss：Playwright 的约定是"不处理就冻结页面"，绝不挂死。"""
    monkeypatch.setattr(browser_module, "DIALOG_GRACE_SECONDS", 0.05)
    browser = make_browser(tmp_path)
    page = RecordingPage()
    browser.new_page(page)
    dialog = FakeDialog("alert", "没人管我")
    page.handlers["dialog"](dialog)
    await asyncio.sleep(0.2)
    assert dialog.handled == [("dismiss", None)]
    snapshot = browser.page_events().dialogs.snapshot()
    assert snapshot["pending"] is False
    assert "兜底" in snapshot["recent"][-1]["action"]
    assert snapshot["recent"][-1]["message"] == "没人管我"


# ══ 5. 监听器生命周期（离线）════════════════════════════════════════════════


async def test_listeners_are_attached_when_the_page_is_created(tmp_path):
    """**在页面创建时就挂**监听器，而不是等模型调工具时才挂。

    等到 ``browser_console`` 被调用才开始收，页面加载期间的日志早就过去了——
    这就是"工具永远是空的"最常见的原因。
    """
    browser = make_browser(tmp_path)
    page = RecordingPage()
    browser.new_page(page)
    assert set(page.handlers) == {"console", "pageerror", "dialog"}
    assert page.attachments == 3

    page.handlers["console"](SimpleNamespace(type="warning", text="注意"))
    page.handlers["pageerror"](RuntimeError("炸了"))
    snapshot = browser.page_events().console.snapshot(10)
    assert [item["text"] for item in snapshot["entries"]] == ["注意", "炸了"]
    assert snapshot["entries"][1]["level"] == "pageerror"

    # 同一个页面只挂一次：重复挂会让每条日志收两遍。
    browser.new_page(page)
    assert page.attachments == 3


async def test_console_and_dialog_state_is_per_page(tmp_path):
    """每个页面一份缓冲：A 页的报错不能被当成 B 页的。"""
    browser = make_browser(tmp_path)
    first, second = RecordingPage("https://a.example/"), RecordingPage("https://b.example/")
    browser.new_page(first)
    first.handlers["console"](SimpleNamespace(type="log", text="A 页日志"))
    browser.new_page(second)
    second.handlers["console"](SimpleNamespace(type="log", text="B 页日志"))
    assert [item["text"] for item in browser.page_events().console.tail(5)] == ["B 页日志"]
    assert [item["text"] for item in browser.page_events(first).console.tail(5)] == ["A 页日志"]


async def test_closing_the_browser_clears_the_buffers(tmp_path):
    browser = make_browser(tmp_path)
    page = RecordingPage()
    browser.new_page(page)
    page.handlers["console"](SimpleNamespace(type="log", text="关之前"))
    assert browser.page_events().console.snapshot()["total"] == 1
    await browser.close()
    assert browser._page_events == {}
    assert browser.page_events().console.snapshot()["total"] == 0


async def test_page_without_event_support_does_not_break_page_creation(tmp_path):
    """页面对象不支持事件时（测试替身 / 精简实现）只意味着收不到日志，不该让开页面失败。"""

    class BarePage:
        url = "about:blank"

        def is_closed(self):
            return False

    browser = make_browser(tmp_path)
    browser.new_page(BarePage())
    assert browser.page_events().console.snapshot()["entries"] == []


# ══ 6. 真浏览器（起不来就 skip，并写明原因）═════════════════════════════════


#: 1×1 透明 GIF（追踪像素的标准形态）。
GIF_1X1 = bytes.fromhex(
    "47494638396101000100800000ffffff00000021f90401000000002c00000000010001000002024401003b"
)


class _QuietHandler(SimpleHTTPRequestHandler):
    """不出声的静态文件服务：默认实现会把每条请求打到 stderr，把测试输出淹掉。"""

    def log_message(self, *args, **kwargs):
        pass


@pytest.fixture(scope="module", autouse=True)
def writable_temp(tmp_path_factory):
    """把 ``TMP`` / ``TEMP`` 指到可写目录。

    本机的 ``%TEMP%``（``C:\\Users\\...\\AppData\\Local\\Temp``）对子进程来说不可写：
    playwright 启动 chromium 的第一步就是在那儿 ``mkdtemp``，于是**任何**真浏览器启动都
    直接失败（``EPERM: operation not permitted, mkdtemp '...playwright-artifacts-XXXXXX'``）。
    这不是 Orbit 的缺陷，但会挡住真浏览器验证，所以这里给子进程换一个可写临时目录。
    """
    directory = tmp_path_factory.mktemp("playwright-tmp")
    saved = {key: os.environ.get(key) for key in ("TMP", "TEMP")}
    for key in saved:
        os.environ[key] = str(directory)
    try:
        yield directory
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _chromium_problem() -> str:
    """真 chromium 能不能起来。返回空串 = 能，否则返回原因（给 skip 用）。"""

    async def probe():
        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.launch(headless=True)
            await browser.close()
            return ""
        except Exception as exc:  # noqa: BLE001 - 任何失败都只意味着"这组用例没法真跑"
            return f"{type(exc).__name__}: {exc}"
        finally:
            with suppress(Exception):
                await playwright.stop()

    return asyncio.run(probe())


@pytest.fixture(scope="module")
def real_browser_supported():
    problem = _chromium_problem()
    if problem:
        pytest.skip(f"真 chromium 起不来，真浏览器用例整组 skip（原因：{problem}）")


@pytest.fixture
def site(tmp_path):
    """本地 127.0.0.1 站点：真 http:// 地址才有真实历史栈与 HTTP 响应。"""
    root = tmp_path / "site"
    root.mkdir()
    # 每个页面都显式声明 charset：SimpleHTTPRequestHandler 发的 Content-Type 不带 charset，
    # 不声明的话中文标题会被浏览器按本地编码猜错，断言就会栽在乱码上而不是逻辑上。
    (root / "one.html").write_text(
        "<!doctype html><meta charset=\"utf-8\"><title>第一页</title><h1>one</h1>"
        "<script>console.log('one-loaded')</script>",
        encoding="utf-8",
    )
    (root / "two.html").write_text(
        "<!doctype html><meta charset=\"utf-8\"><title>第二页</title><h1>two</h1>"
        "<script>console.log('two-loaded');"
        "setTimeout(() => { throw new Error('boom'); }, 0)</script>",
        encoding="utf-8",
    )
    for name, size in (("pic.svg", (40, 20)), ("pic2.svg", (60, 30))):
        (root / name).write_text(
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{size[0]}" height="{size[1]}"></svg>',
            encoding="utf-8",
        )
    (root / "px.gif").write_bytes(GIF_1X1)
    (root / "gallery.html").write_text(
        "<!doctype html><title>图库</title>"
        '<img src="pic.svg" alt="大图"><img src="/px.gif" alt="追踪像素">'
        '<img src="pic2.svg" alt="第二张"><img src="pic.svg" alt="重复的大图">',
        encoding="utf-8",
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(_QuietHandler, directory=str(root))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@asynccontextmanager
async def open_browser(tmp_path):
    browser = make_browser(tmp_path)
    try:
        yield browser
    finally:
        with suppress(Exception):
            await browser.close()


async def test_real_back_returns_to_the_previous_page(tmp_path, site, real_browser_supported):
    async with open_browser(tmp_path) as browser:
        await browser.execute("browser_navigate", {"url": f"{site}/one.html"}, "t", "op-1")
        second = await browser.execute(
            "browser_navigate", {"url": f"{site}/two.html"}, "t", "op-2"
        )
        assert second["data"]["url"] == f"{site}/two.html"

        result = await browser.execute("browser_back", {}, "t", "op-3")
        assert result["status"] == "ok"
        assert result["data"]["went_back"] is True
        assert result["data"]["url"] == f"{site}/one.html"
        assert result["data"]["title"] == "第一页"
        assert result["data"]["status_code"] == 200
        # 后退后旧 refs 必须作废
        assert browser.refs == {}


async def test_real_back_reports_honestly_when_history_is_exhausted(
    tmp_path, site, real_browser_supported
):
    """退到历史尽头时必须如实说"没有上一页"，不能假装成功。"""
    async with open_browser(tmp_path) as browser:
        await browser.execute("browser_navigate", {"url": f"{site}/one.html"}, "t", "op-1")
        await browser.execute("browser_navigate", {"url": f"{site}/two.html"}, "t", "op-2")
        outcomes = []
        result = {}
        for index in range(5):
            result = await browser.execute("browser_back", {}, "t", f"op-back-{index}")
            outcomes.append(result["data"]["went_back"])
            if not result["data"]["went_back"]:
                break
        assert outcomes[0] is True, f"第一次后退应当真的退了一页：{outcomes}"
        assert outcomes[-1] is False, f"退到尽头时必须报 went_back=false：{outcomes}"
        assert "没有上一页" in result["data"]["message"]


async def test_real_get_images_absolutizes_and_filters_pixels(
    tmp_path, site, real_browser_supported
):
    async with open_browser(tmp_path) as browser:
        await browser.execute("browser_navigate", {"url": f"{site}/gallery.html"}, "t", "op-1")
        result = await browser.execute("browser_get_images", {"limit": 10}, "t", "op-2")
        data = result["data"]
        sources = [item["src"] for item in data["images"]]
        assert f"{site}/pic.svg" in sources  # 相对地址已转绝对
        assert f"{site}/pic2.svg" in sources
        assert f"{site}/px.gif" not in sources  # 1×1 追踪像素被过滤
        assert len(sources) == len(set(sources))  # 重复的图只留一条
        assert data["skipped"]["tracking_pixel"] == 1
        assert data["images"][0]["width"] == 40

        limited = await browser.execute("browser_get_images", {"limit": 1}, "t", "op-3")
        assert limited["data"]["count"] == 1
        assert limited["data"]["skipped"]["over_limit"] >= 1


async def test_real_console_collects_logs_and_page_errors(tmp_path, site, real_browser_supported):
    """日志是**页面加载期间**打的：只有监听器在页面创建时就挂上才收得到。"""
    async with open_browser(tmp_path) as browser:
        await browser.execute("browser_navigate", {"url": f"{site}/one.html"}, "t", "op-1")
        await browser.execute("browser_navigate", {"url": f"{site}/two.html"}, "t", "op-2")
        await asyncio.sleep(0.3)  # pageerror 来自 setTimeout，要等一拍
        result = await browser.execute("browser_console", {"limit": 20}, "t", "op-3")
        entries = result["data"]["entries"]
        texts = [entry["text"] for entry in entries]
        assert "two-loaded" in texts
        assert any(
            entry["level"] == "pageerror" and "boom" in entry["text"] for entry in entries
        ), entries
        assert set(entries[0]) == {"level", "text", "at"}


async def test_real_dialog_accept_types_the_prompt_text(tmp_path, site, real_browser_supported):
    async with open_browser(tmp_path) as browser:
        await browser.execute("browser_navigate", {"url": f"{site}/one.html"}, "t", "op-1")
        page = browser.page
        await page.evaluate(
            "() => { window.__answer = null;"
            " setTimeout(() => { window.__answer = window.prompt('名字？', '默认'); }, 50); }"
        )
        await asyncio.sleep(0.3)
        result = await browser.execute(
            "browser_dialog", {"action": "accept", "text": "张三"}, "t", "op-2"
        )
        assert result["status"] == "ok"
        assert result["data"]["handled"] is True
        assert result["data"]["dialog"]["type"] == "prompt"
        assert result["data"]["dialog"]["message"] == "名字？"
        assert await page.evaluate("() => window.__answer") == "张三"


async def test_real_dialog_dismiss_then_reports_nothing_pending(
    tmp_path, site, real_browser_supported
):
    async with open_browser(tmp_path) as browser:
        await browser.execute("browser_navigate", {"url": f"{site}/one.html"}, "t", "op-1")
        page = browser.page
        await page.evaluate(
            "() => { setTimeout(() => { window.confirm('继续吗？'); }, 50); }"
        )
        await asyncio.sleep(0.3)
        result = await browser.execute("browser_dialog", {"action": "dismiss"}, "t", "op-2")
        assert result["data"]["handled"] is True
        assert result["data"]["dialog"]["type"] == "confirm"
        assert result["data"]["recent"][-1]["action"] == "dismiss"

        again = await browser.execute("browser_dialog", {"action": "accept"}, "t", "op-3")
        assert again["data"]["handled"] is False
        assert again["data"]["pending"] is False
        assert "没有待处理" in again["data"]["message"]
        assert await page.evaluate("() => 1 + 1") == 2


async def test_real_dialog_fallback_keeps_the_page_usable(
    tmp_path, site, real_browser_supported, monkeypatch
):
    """没人处理时自动 dismiss：页面不会永久卡死（否则下一次求值会一直挂着）。"""
    monkeypatch.setattr(browser_module, "DIALOG_GRACE_SECONDS", 0.1)
    async with open_browser(tmp_path) as browser:
        await browser.execute("browser_navigate", {"url": f"{site}/one.html"}, "t", "op-1")
        page = browser.page
        await page.evaluate("() => { setTimeout(() => { window.alert('兜底'); }, 50); }")
        await asyncio.sleep(0.6)
        assert await page.evaluate("() => 2 + 2") == 4
        result = await browser.execute("browser_dialog", {"action": "accept"}, "t", "op-2")
        assert result["data"]["handled"] is False
        assert result["data"]["recent"][-1]["message"] == "兜底"
        assert "兜底" in result["data"]["recent"][-1]["action"]
