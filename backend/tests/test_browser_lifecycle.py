import asyncio
from types import SimpleNamespace

import pytest

import app.browser as browser_module
from app.browser import BrowserController, BrowserSessionError
from app.config import Settings


class FakePage:
    def __init__(self):
        self.url = "about:blank"
        self.closed = False
        self.visits = []
        self.load_states = []

    def is_closed(self):
        return self.closed

    async def goto(self, url, **kwargs):
        self.visits.append(url)
        self.url = url

    async def title(self):
        return "Browser fixture"

    async def bring_to_front(self):
        pass

    async def wait_for_load_state(self, state=None, **kwargs):
        self.load_states.append(state)

    async def evaluate(self, script):
        return []

    async def screenshot(self, **kwargs):
        return b""

    def locator(self, selector):
        return SimpleNamespace(inner_text=self._body_text)

    async def _body_text(self):
        return "fixture body text " * 40


class FakeStore:
    def __init__(self):
        self.saved = []
        self.events = []

    async def save(self, row):
        self.saved.append(row)
        return row

    async def event(self, *args, **kwargs):
        self.events.append((args, kwargs))

    async def get(self, model, key):
        return None


class FakeContext:
    def __init__(self):
        self.pages = [FakePage()]
        self.handlers = {}
        self.connected = True
        self.browser = SimpleNamespace(is_connected=lambda: self.connected, on=self.on)

    def on(self, name, handler):
        self.handlers[name] = handler

    def set_default_timeout(self, timeout):
        pass

    async def new_page(self):
        if not self.connected:
            raise RuntimeError("closed context")
        page = FakePage()
        self.pages.append(page)
        return page

    async def close(self):
        self.connected = False
        for page in self.pages:
            page.closed = True
        if "close" in self.handlers:
            self.handlers["close"](self)


class FakeRuntime:
    def __init__(self, launches, *, failure=False):
        self.launches = launches
        self.failure = failure
        self.stopped = False
        self.chromium = self

    async def start(self):
        return self

    async def launch_persistent_context(self, profile, **kwargs):
        self.launches.append(kwargs["headless"])
        await asyncio.sleep(0)
        if self.failure:
            raise RuntimeError("launch failed with private token=secret")
        return FakeContext()

    async def stop(self):
        self.stopped = True


def make_browser(tmp_path, monkeypatch, *, failure=False, headless=False):
    launches, runtimes = [], []

    def create_runtime():
        runtime = FakeRuntime(launches, failure=failure)
        runtimes.append(runtime)
        return runtime

    monkeypatch.setattr(browser_module, "async_playwright", create_runtime)
    settings = Settings(
        data_dir=tmp_path,
        browser_url="https://www.bilibili.com/",
        headless=headless,
    )
    return BrowserController(settings, None), launches, runtimes


async def test_visible_failure_never_falls_back_and_stops_runtime(tmp_path, monkeypatch):
    browser, launches, runtimes = make_browser(tmp_path, monkeypatch, failure=True)
    with pytest.raises(RuntimeError) as failure:
        await browser.open_session()
    assert launches == [False]
    assert runtimes[0].stopped
    assert browser.runtime is browser.context is browser.page is None
    assert "secret" not in str(failure.value)


async def test_closed_window_reopens_and_concurrent_start_shares_profile(tmp_path, monkeypatch):
    browser, launches, runtimes = make_browser(tmp_path, monkeypatch)
    first, second = await asyncio.gather(browser.start(), browser.start())
    assert first is second
    assert launches == [False]
    browser.refs = {"e1": {}}
    await browser.context.close()
    reopened = await browser.start()
    assert reopened is not first
    assert browser.refs == {}
    assert runtimes[0].stopped
    assert launches == [False, False]
    await browser.close()


async def test_close_event_does_not_interrupt_the_pending_close_response(tmp_path, monkeypatch):
    browser, _, runtimes = make_browser(tmp_path, monkeypatch)
    await browser.start()
    context = browser.context
    # Playwright emits the close event before the close RPC has fully returned.
    context.handlers["close"](context)
    await asyncio.sleep(0)
    assert not runtimes[0].stopped
    assert browser.page is None
    await browser.start()
    assert runtimes[0].stopped
    await browser.close()


async def test_open_session_is_visible_and_always_navigates_requested_page(tmp_path, monkeypatch):
    browser, launches, _ = make_browser(tmp_path, monkeypatch, headless=True)
    await browser.start()
    result = await browser.open_session("https://space.bilibili.com/123")
    assert launches == [True, False]
    assert result == {
        "url": "https://space.bilibili.com/123",
        "title": "Browser fixture",
        "mode": "headed",
        "visible": True,
    }
    await browser.open_session("https://www.bilibili.com/")
    assert browser.page.visits == [
        "https://space.bilibili.com/123",
        "https://www.bilibili.com/",
    ]
    await browser.start()
    assert launches == [True, False]
    # Any HTTP(S) site is reachable; only the address format is checked.
    await browser.open_session("https://unconfigured.example/")
    assert browser.page.visits[-1] == "https://unconfigured.example/"
    with pytest.raises(ValueError):
        await browser.open_session("javascript:alert(1)")
    await browser.close()


async def test_failed_navigation_cleans_up_and_redacts_url(tmp_path, monkeypatch):
    browser, _, runtimes = make_browser(tmp_path, monkeypatch)
    page = await browser.start()

    async def failed_goto(url, **kwargs):
        raise RuntimeError(f"Failed to navigate to {url}")

    monkeypatch.setattr(page, "goto", failed_goto)
    with pytest.raises(BrowserSessionError) as failure:
        await browser.open_session("https://www.bilibili.com/?token=private-value")
    assert failure.value.stage == "navigation"
    assert "private-value" not in str(failure.value)
    assert runtimes[0].stopped
    assert browser.runtime is browser.context is browser.page is None


async def test_navigation_settles_before_observing(tmp_path, monkeypatch):
    # 单页应用在 domcontentloaded 之后才渲染登录表单，观察前必须等它稳定。
    browser, _launches, _ = make_browser(tmp_path, monkeypatch)
    browser.store = FakeStore()
    result = await browser.execute(
        "browser_navigate", {"url": "https://ai.example/login"}, "task-1", "op-1"
    )
    assert result["status"] == "ok"
    assert browser.page.load_states == ["load", "networkidle"]
    await browser.close()


async def test_click_reports_the_page_it_navigated_to(tmp_path, monkeypatch):
    # 点击后如果返回的还是旧页面，模型会误判"没跳转"并盲目重试。
    browser, _launches, _ = make_browser(tmp_path, monkeypatch)
    browser.store = FakeStore()
    page = await browser.start()
    browser.version = 1
    browser.observed_url = page.url
    browser.refs = {"e1": {}}

    class Locator:
        async def click(self):
            page.url = "https://example.com/video/42"

            async def longer():
                return "详情页正文" * 60

            page._body_text = longer

        async def count(self):
            return 1

        async def inner_text(self):
            return await page._body_text()

    monkeypatch.setattr(page, "locator", lambda selector: Locator(), raising=False)
    result = await browser.execute(
        "browser_click", {"ref": "e1", "version": 1}, "task-1", "op-1"
    )
    assert result["status"] == "ok"
    assert result["data"]["url"] == "https://example.com/video/42"
    await browser.close()


async def test_observe_does_not_wait_for_a_change(tmp_path, monkeypatch):
    browser, _launches, _ = make_browser(tmp_path, monkeypatch)
    browser.store = FakeStore()
    page = await browser.start()
    result = await browser.execute("browser_observe", {}, "task-1", "op-1")
    assert result["status"] == "ok"
    assert page.load_states == []
    await browser.close()


async def test_navigate_reopens_browser_when_the_first_page_is_dropped(
    tmp_path, monkeypatch
):
    dropped = [True]

    class DroppingPage(FakePage):
        async def goto(self, url, **kwargs):
            if dropped:
                dropped.pop()
                raise RuntimeError("Target page, context or browser has been closed")
            await super().goto(url, **kwargs)

    class DroppingContext(FakeContext):
        def __init__(self):
            super().__init__()
            self.pages = [DroppingPage()]

        async def new_page(self):
            page = DroppingPage()
            self.pages.append(page)
            return page

    async def launch(self, profile, **kwargs):
        self.launches.append(kwargs["headless"])
        await asyncio.sleep(0)
        return DroppingContext()

    monkeypatch.setattr(FakeRuntime, "launch_persistent_context", launch)
    browser, launches, _ = make_browser(tmp_path, monkeypatch)
    browser.store = FakeStore()
    result = await browser.execute(
        "browser_navigate", {"url": "https://www.ithome.com/"}, "task-1", "op-1"
    )
    assert result["status"] == "ok"
    assert result["data"]["url"] == "https://www.ithome.com/"
    assert result["data"].get("entry_weak") is None
    # The browser was reopened exactly once for the retry.
    assert launches == [False, False]
    await browser.close()


async def test_navigate_does_not_retry_unrelated_errors(tmp_path, monkeypatch):
    browser, launches, _ = make_browser(tmp_path, monkeypatch)
    browser.store = FakeStore()
    page = await browser.start()

    async def failed_goto(url, **kwargs):
        raise RuntimeError("net::ERR_CONNECTION_TIMED_OUT")

    monkeypatch.setattr(page, "goto", failed_goto)
    with pytest.raises(RuntimeError, match="CONNECTION_TIMED_OUT"):
        await browser.execute(
            "browser_navigate", {"url": "https://www.example.com/"}, "task-1", "op-1"
        )
    assert launches == [False]
    await browser.close()


async def test_bilibili_cannot_use_notion_specific_tools(tmp_path, monkeypatch):
    browser, launches, _ = make_browser(tmp_path, monkeypatch)
    browser.settings.notion_url = "https://www.bilibili.com/"
    result = await browser.execute("notion_read_page", {}, "task", "operation")
    assert result["status"] == "error"
    assert launches == []
