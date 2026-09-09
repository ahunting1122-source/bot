import hashlib
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from pydantic import Field

from app.storage import Artifact, Operation
from app.tools import Args, Empty, Tool


class NavigateArgs(Args):
    url: str


class ElementArgs(Args):
    ref: str
    version: int


class FillArgs(ElementArgs):
    text: str = Field(max_length=12000)


class PressArgs(Args):
    key: str = Field(description="Enter、Tab、Escape 或 ArrowDown")


class ScrollArgs(Args):
    pixels: int = Field(default=600, ge=-2000, le=2000)


class SummaryArgs(Args):
    content: str = Field(min_length=1, max_length=12000)


BROWSER_TOOLS = [
    Tool("browser_observe", "观察当前浏览器页面，返回文本和带版本的可交互元素引用。", Empty),
    Tool(
        "browser_navigate",
        "打开配置的 Notion 页面或 GitHub 域名；其他域名需用户配置。",
        NavigateArgs,
    ),
    Tool("browser_click", "点击最新观察中的元素；需要确认。", ElementArgs, True),
    Tool("browser_fill", "填写最新观察中的输入元素；需要确认。", FillArgs, True),
    Tool("browser_press", "按一个键；需要确认。", PressArgs, True),
    Tool("browser_scroll", "滚动页面。", ScrollArgs),
    Tool("notion_read_page", "读取设置中指定的 Notion 页面。未登录时请求接管。", Empty),
    Tool(
        "notion_append_summary",
        "向配置的 Notion 页面追加摘要和操作标记，并回读验证；需要确认。",
        SummaryArgs,
        True,
    ),
]

OBSERVE_JS = """() => {
  const nodes = [...document.querySelectorAll('button,a,input,textarea,select,[role="button"],[contenteditable="true"]')]
    .filter(e => e.getBoundingClientRect().width && e.getBoundingClientRect().height).slice(0,100);
  return nodes.map((e,i) => {
    const ref = 'e' + i;
    e.setAttribute('data-orbit-ref', ref);
    return {ref,tag:e.tagName.toLowerCase(),role:e.getAttribute('role'),name:(e.getAttribute('aria-label')||e.getAttribute('placeholder')||e.innerText||'').slice(0,100)};
  });
}"""


class BrowserController:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.runtime = self.context = self.page = None
        self.version = 0
        self.observed_url = ""
        self.refs: dict = {}

    async def start(self):
        if self.context is None:
            self.runtime = await async_playwright().start()
            try:
                self.context = await self.runtime.chromium.launch_persistent_context(
                    str(self.settings.data_dir / "browser_profiles" / "default"),
                    headless=self.settings.headless,
                    viewport={"width": 1280, "height": 900},
                )
            except Exception:
                await self.runtime.stop()
                self.runtime = None
                raise
            self.context.set_default_timeout(10000)
            self.page = (
                self.context.pages[0] if self.context.pages else await self.context.new_page()
            )
            self.context.on("page", self.new_page)
        if self.page is None or self.page.is_closed():
            self.page = await self.context.new_page()
        return self.page

    def new_page(self, page):
        self.page = page
        self.refs = {}

    async def close(self):
        if self.context:
            await self.context.close()
        if self.runtime:
            await self.runtime.stop()
        self.context = self.page = self.runtime = None

    def allowed(self, url: str):
        parsed = urlparse(url)
        host = parsed.hostname
        target = urlparse(self.settings.notion_url).hostname
        if (
            parsed.scheme not in {"http", "https"}
            or host
            not in {
                "github.com",
                "www.notion.so",
                "notion.so",
                "www.notion.com",
                "notion.com",
                target,
            }
            or not host
        ):
            raise ValueError("该网址不在已配置平台范围内")

    async def observe(self, task_id: str):
        page = await self.start()
        self.allowed(page.url)
        self.version += 1
        elements = await page.evaluate(OBSERVE_JS)
        self.refs = {item["ref"]: item for item in elements}
        self.observed_url = page.url
        text = (await page.locator("body").inner_text())[:14000]
        artifact_id = hashlib.sha256(f"{task_id}:screen:{self.version}".encode()).hexdigest()[:32]
        path = self.settings.data_dir / "artifacts" / f"{artifact_id}.png"
        await page.screenshot(path=str(path))
        await self.store.save(
            Artifact(
                id=artifact_id,
                task_id=task_id,
                kind="screenshot",
                location=str(path),
                label="网页观察",
            )
        )
        return {
            "url": page.url,
            "title": await page.title(),
            "text": text,
            "version": self.version,
            "elements": elements,
            "screenshot_id": artifact_id,
        }

    async def locate(self, args):
        page = await self.start()
        self.allowed(page.url)
        if (
            args["version"] != self.version
            or page.url != self.observed_url
            or args["ref"] not in self.refs
        ):
            raise ValueError("页面引用过期，请重新观察")
        locator = page.locator(f'[data-orbit-ref="{args["ref"]}"]')
        if await locator.count() != 1:
            raise ValueError("元素已变化，请重新观察")
        return locator

    async def execute(self, name, args, task_id, operation_id):
        page = await self.start()
        if name.startswith("notion_"):
            return await self.notion(name, args, task_id, operation_id)
        if name == "browser_navigate":
            self.allowed(args["url"])
            self.refs = {}
            await page.goto(args["url"], wait_until="domcontentloaded", timeout=30000)
        elif name == "browser_click":
            await (await self.locate(args)).click()
        elif name == "browser_fill":
            await (await self.locate(args)).fill(args["text"])
        elif name == "browser_press":
            self.allowed(page.url)
            if args["key"] not in {"Enter", "Tab", "Escape", "ArrowDown", "ArrowUp"}:
                raise ValueError("不支持该按键")
            await page.keyboard.press(args["key"])
        elif name == "browser_scroll":
            self.allowed(page.url)
            await page.mouse.wheel(0, args["pixels"])
        return {"status": "ok", "data": await self.observe(task_id)}

    async def notion(self, name, args, task_id, operation_id):
        if not self.settings.notion_url:
            return {"status": "error", "error": "请先在设置中指定 Notion 页面 URL"}
        self.allowed(self.settings.notion_url)
        page = await self.start()
        if page.url.split("?")[0] != self.settings.notion_url.split("?")[0]:
            await page.goto(self.settings.notion_url, wait_until="domcontentloaded", timeout=30000)
        body = await page.locator("body").inner_text()
        marker = f"[Orbit {hashlib.sha256(operation_id.encode()).hexdigest()[:16]}]"
        if name == "notion_append_summary" and marker in body:
            verified = " ".join(args["content"].split()) in " ".join(body.split())
            return {
                "status": "ok" if verified else "error",
                "data": {"url": page.url, "verified": verified, "marker": marker},
                "message": "已找到操作标记，未重复写入",
            }
        editors = page.locator('.notion-page-content [contenteditable="true"]')
        if await editors.count() == 0:
            return {
                "status": "needs_user",
                "reason": "login",
                "message": "请在专用浏览器中登录 Notion，打开已配置的可编辑页面，再点击继续。",
            }
        if name == "notion_read_page":
            return {"status": "ok", "data": await self.observe(task_id)}
        operation = await self.store.get(Operation, operation_id)
        if operation.result.get("write_started"):
            return {
                "status": "error",
                "error": "上次追加可能部分完成但未找到完整标记，请人工核对页面；本次没有重复提交。",
            }
        operation.result = {"write_started": True}
        await self.store.save(operation)
        try:
            await editors.last.click()
            await page.keyboard.press("Control+End")
            await page.keyboard.press("End")
            await page.keyboard.press("Enter")
            await page.keyboard.insert_text(args["content"] + "\n\n" + marker)
            await page.keyboard.press("Escape")
            await page.wait_for_function(
                "marker => document.body.innerText.includes(marker)", arg=marker, timeout=10000
            )
            body = await page.locator("body").inner_text()
            if " ".join(args["content"].split()) not in " ".join(body.split()):
                raise ValueError("回读内容与摘要不一致")
            # Reload to confirm the site persisted the change rather than only editing the DOM.
            await page.reload(wait_until="domcontentloaded")
            await page.wait_for_function(
                "marker => document.body.innerText.includes(marker)", arg=marker, timeout=10000
            )
            observation = await self.observe(task_id)
            return {
                "status": "ok",
                "write_started": True,
                "data": {
                    "url": page.url,
                    "verified": True,
                    "marker": marker,
                    "screenshot_id": observation["screenshot_id"],
                },
            }
        except Exception:
            return {
                "status": "uncertain",
                "write_started": True,
                "message": "网页写入后未能确认保存。请检查 Notion 页面，继续时会先核对，不直接重复提交。",
            }
