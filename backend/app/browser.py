import asyncio
import hashlib
from contextlib import suppress
from urllib.parse import urlparse

from playwright.async_api import async_playwright
from pydantic import Field

from app.config import http_url, is_notion_host
from app.storage import Artifact, Operation
from app.tools import Args, Empty, Tool
from app.vault import Vault, cookie_host_matches, summarize_logins


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


class ClickAtArgs(Args):
    x: int = Field(ge=0, le=100000, description="视口内点击点的 x 坐标（相对视口左上角）")
    y: int = Field(ge=0, le=100000, description="视口内点击点的 y 坐标（相对视口左上角）")
    version: int = Field(description="当前观察返回的版本号，用于确认点击发生在已观察页面上")


class SummaryArgs(Args):
    content: str = Field(min_length=1, max_length=12000)


class VaultLoginArgs(Args):
    host: str = Field(description="密钥存储里保存过账号密码的站点主机名")
    version: int = Field(description="最新 observe 返回的版本号")
    password_ref: str = Field(description="密码输入框的 ref")
    username_ref: str = Field(default="", description="用户名输入框的 ref，留空表示只用密码")
    submit_ref: str = Field(
        default="", description="登录/提交按钮的 ref；给出后会自动点击完成登录"
    )


BROWSER_TOOLS = [
    Tool("browser_observe", "观察当前浏览器页面，返回文本和带版本的可交互元素引用。", Empty),
    Tool(
        "browser_navigate",
        "打开任意 http(s) 网址，没有域名限制；会返回是否真的进入了页面。",
        NavigateArgs,
    ),
    Tool("browser_click", "点击最新观察中的元素；需要确认。", ElementArgs, True),
    Tool(
        "browser_click_at",
        "在视口指定坐标处点击（视觉兜底：当元素 ref 已失效、无法用 browser_click 定位时，"
        "模型可根据最新截图/观察判断目标位置并给出坐标）。需要确认。",
        ClickAtArgs,
        True,
    ),
    Tool("browser_fill", "填写最新观察中的输入元素；需要确认。", FillArgs, True),
    Tool("browser_press", "按一个键；需要确认。", PressArgs, True),
    Tool("browser_scroll", "滚动页面。", ScrollArgs),
    Tool(
        "vault_login",
        "用密钥存储中保存的账号密码登录站点：填写用户名与密码，并在给出 submit_ref 时点击提交，"
        "一次完成登录。密码不经过模型，且始终需要用户确认。",
        VaultLoginArgs,
        True,
    ),
    Tool("notion_read_page", "读取设置中指定的 Notion 页面。未登录时请求接管。", Empty),
    Tool(
        "notion_append_summary",
        "向配置的 Notion 页面追加摘要和操作标记，并回读验证；需要确认。",
        SummaryArgs,
        True,
    ),
]

OBSERVE_JS = """() => {
  const seen = new Set();
  const pick = (selector, limit) => [...document.querySelectorAll(selector)]
    .filter(e => e.getBoundingClientRect().width && e.getBoundingClientRect().height)
    .filter(e => { if (seen.has(e)) return false; seen.add(e); return true; })
    .slice(0, limit);
  // Controls first: pagination and form actions live at the bottom of the DOM, so a single
  // DOM-ordered cap would drop them on link-heavy pages.
  const nodes = [
    ...pick('button,input,textarea,select,[role="button"],[contenteditable="true"]', 120),
    ...pick('a', 80),
  ];
  return nodes.map((e,i) => {
    const ref = 'e' + i;
    e.setAttribute('data-orbit-ref', ref);
    const r = e.getBoundingClientRect();
    // 中心坐标：坐标点击（视觉兜底）与元素几何定位都依赖它。
    return {ref,tag:e.tagName.toLowerCase(),role:e.getAttribute('role'),name:(e.getAttribute('aria-label')||e.getAttribute('placeholder')||e.innerText||'').slice(0,100),x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),width:Math.round(r.width),height:Math.round(r.height)};
  });
}"""


EXTRACT_JS = """() => {
  const clean = (value) => (value || '').replace(/\\s+/g, ' ').trim();
  const skip = 'nav,header,footer,script,style,noscript,aside,svg';
  const out = [];
  let inTable = false;
  for (const node of document.querySelectorAll('h1,h2,h3,h4,p,li,tr,blockquote,pre')) {
    if (node.closest(skip)) continue;
    const tag = node.tagName.toLowerCase();
    if (tag === 'p' && node.closest('li')) continue;
    const text = clean(node.innerText);
    if (!text) continue;
    if (tag === 'tr') {
      const cells = [...node.querySelectorAll('th,td')].map((cell) => clean(cell.innerText));
      if (!cells.length) continue;
      if (!inTable) {
        out.push('| ' + cells.join(' | ') + ' |');
        out.push('| ' + cells.map(() => '---').join(' | ') + ' |');
        inTable = true;
      } else {
        out.push('| ' + cells.join(' | ') + ' |');
      }
      continue;
    }
    inTable = false;
    if (tag === 'li') out.push('- ' + text);
    else if (tag === 'blockquote') out.push('> ' + text);
    else if (tag === 'pre') out.push('```\\n' + (node.innerText || '').trim().slice(0, 2000) + '\\n```');
    else if (tag === 'p') out.push(text);
    else out.push('#'.repeat(Math.min(6, Number(tag.slice(1)) || 1)) + ' ' + text);
  }
  return out.join('\\n\\n').slice(0, 24000);
}"""


class BrowserSessionError(RuntimeError):
    def __init__(self, stage: str, message: str):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.status_code = 503 if stage == "startup" else 502


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_closed_error(exc: BaseException) -> bool:
    """Playwright reports a dropped page as TargetClosedError; the name is stable across versions."""
    return type(exc).__name__ == "TargetClosedError" or "has been closed" in str(exc)


def safe_page_url(url: str) -> str:
    """Display the reached page without login tokens, fragments, or URL credentials."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return "about:blank" if url == "about:blank" else ""
    host = parsed.hostname or ""
    if ":" in host:
        host = f"[{host}]"
    if parsed.port:
        host += f":{parsed.port}"
    return parsed._replace(netloc=host, query="", fragment="", params="").geturl()


class BrowserController:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.runtime = self.context = self.page = None
        self.version = 0
        self.observed_url = ""
        self.refs: dict = {}
        self._lifecycle_lock = asyncio.Lock()
        self._headless: bool | None = None
        self._context_closed = False
        self._last_screenshot: str | None = None

    async def start(self):
        async with self._lifecycle_lock:
            return await self._start_locked()

    async def _start_locked(self, headless: bool | None = None):
        try:
            if self.context is not None:
                browser = self.context.browser
                disconnected = browser is not None and not browser.is_connected()
                mode_changed = headless is not None and headless != self._headless
                if self._context_closed or disconnected or mode_changed:
                    await self._close_locked()
            if self.context is None:
                # A visible session stays visible when the agent next uses a browser tool.
                selected_mode = self.settings.headless if headless is None else headless
                self.runtime = await async_playwright().start()
                profile = str(self.settings.data_dir / "browser_profiles" / "default")
                self.context = await self.runtime.chromium.launch_persistent_context(
                    profile, headless=selected_mode, viewport={"width": 1280, "height": 900}
                )
                self._headless = selected_mode
                self._context_closed = False
                context = self.context
                context.on("close", lambda *_: self._disconnected(context))
                if context.browser is not None:
                    context.browser.on("disconnected", lambda *_: self._disconnected(context))
                context.set_default_timeout(10000)
                context.on(
                    "page", lambda page: self.new_page(page) if self.context is context else None
                )
                self.page = next((page for page in context.pages if not page.is_closed()), None)
            if self.page is None or self.page.is_closed():
                self.new_page(await self.context.new_page())
            return self.page
        except BaseException as exc:
            await self._close_locked()
            if isinstance(exc, asyncio.CancelledError):
                raise
            message = "浏览器启动失败，请确认当前 Windows 桌面可用，并重试打开；未切换到无头模式"
            details = str(exc).lower()
            if "executable doesn't exist" in details:
                message = (
                    "缺少 Chromium，请在 backend 目录运行 uv run playwright install chromium 后重试"
                )
            elif any(
                part in details for part in ("processsingleton", "profile in use", "already in use")
            ):
                message = "专用浏览器用户目录正在使用，请关闭其他 Orbit 浏览器进程后重试"
            raise BrowserSessionError("startup", message) from None

    def _disconnected(self, context):
        if self.context is not context:
            return
        self._context_closed = True
        self.page = None
        self._clear_observation()
        # The close event can arrive before the pending context.close() RPC returns.
        # Stopping the driver here cuts that response off with TargetClosedError.
        # The next start()/close() cleans up the driver under the lifecycle lock.

    def _clear_observation(self):
        self.refs = {}
        self.observed_url = ""
        self.version += 1

    def new_page(self, page):
        self.page = page
        self._clear_observation()

    async def close(self):
        async with self._lifecycle_lock:
            await self._close_locked()

    async def _close_locked(self):
        context, runtime = self.context, self.runtime
        context_already_closed = self._context_closed
        # Clear first: callbacks from old instances must never invalidate a new session.
        self.context = self.page = self.runtime = None
        self._headless = None
        self._context_closed = False
        self._clear_observation()
        try:
            if context is not None and not context_already_closed:
                with suppress(Exception):
                    await context.close()
        finally:
            if runtime is not None:
                with suppress(Exception):
                    await runtime.stop()

    async def open_session(self, url: str | None = None):
        target = url if url is not None else (self.settings.browser_url or self.settings.notion_url)
        if target:
            self.validate_url(target)
        async with self._lifecycle_lock:
            page = await self._start_locked(headless=False)
            if target:
                try:
                    self._clear_observation()
                    response = await page.goto(target, wait_until="domcontentloaded", timeout=30000)
                    self.validate_url(page.url)
                    if response is not None and response.status >= 400:
                        raise BrowserSessionError(
                            "navigation",
                            f"目标网站返回 HTTP {response.status}，请检查网址或稍后重试",
                        )
                except BaseException as exc:
                    await self._close_locked()
                    if isinstance(exc, asyncio.CancelledError | BrowserSessionError):
                        raise
                    message = "浏览器已启动，但目标网页未能打开，请检查网址、网络和允许域名后重试"
                    raise BrowserSessionError("navigation", message) from None
            try:
                await page.bring_to_front()
                return {
                    "url": safe_page_url(page.url),
                    "title": await page.title(),
                    "mode": "headed",
                    "visible": True,
                }
            except BaseException as exc:
                await self._close_locked()
                if isinstance(exc, asyncio.CancelledError):
                    raise
                raise BrowserSessionError(
                    "focus", "无法唤起浏览器窗口，请重新点击打开浏览器"
                ) from None

    def current_host(self) -> str:
        if self.page is None:
            return ""
        return host_of(self.page.url)

    async def login_status(self, hosts: list[str]) -> dict:
        """Read the profile's cookies through Playwright; the cookie file itself stays locked."""
        if self.context is None:
            return {"ready": False, "reason": "浏览器还没打开", "logins": []}
        cookies = await self.context.cookies()
        return {"ready": True, "reason": "", "logins": summarize_logins(hosts, cookies)}

    async def login(self, args, task_id):
        """Fill saved credentials and, when a submit button is named, finish the sign-in."""
        credentials = Vault(self.settings).site_credentials(args["host"])
        if not credentials:
            return {
                "status": "error",
                "error": f"密钥存储里没有 {args['host']} 的账号密码，请先在密钥存储中保存",
            }
        if credentials["username"] and not args["username_ref"]:
            return {"status": "error", "error": "该站点保存了用户名，请同时给出用户名输入框的 ref"}
        if not credentials["password"]:
            return {"status": "error", "error": "该站点没有保存密码"}
        page = await self.start()
        # Fail closed when the open page is not the site these credentials belong to.
        if not cookie_host_matches(host_of(page.url), args["host"]):
            return {
                "status": "error",
                "error": f"当前页面不是 {args['host']}，请先在该站点打开登录页",
            }

        def element(ref: str):
            return self.locate({"ref": ref, "version": args["version"]})

        if credentials["username"]:
            await (await element(args["username_ref"])).fill(credentials["username"])
        await (await element(args["password_ref"])).fill(credentials["password"])
        if not args.get("submit_ref", ""):
            return {
                "status": "ok",
                "data": await self.observe(task_id),
                "message": "已填写账号密码，但没有给出 submit_ref，因此没有提交。",
            }
        await (await element(args["submit_ref"])).click()
        await self.settle(page, full=True)
        return {"status": "ok", "data": await self.observe(task_id)}

    def validate_url(self, url: str) -> str:
        """Any HTTP(S) site is reachable; only the address format is checked."""
        if url.startswith("about:"):
            return url
        return http_url(url, "网址")

    async def observe(self, task_id: str):
        page = await self.start()
        self.validate_url(page.url)
        self.version += 1
        elements = await page.evaluate(OBSERVE_JS)
        self.refs = {item["ref"]: item for item in elements}
        self.observed_url = page.url
        text = (await page.locator("body").inner_text())[:14000]
        # 结构化正文只给索引器用：进模型上下文会被 ToolResultPolicy.project() 剥离。
        structured = ""
        with suppress(Exception):
            structured = str(await page.evaluate(EXTRACT_JS))[:24000]
        artifact_id = hashlib.sha256(f"{task_id}:screen:{self.version}".encode()).hexdigest()[:32]
        path = self.settings.data_dir / "artifacts" / f"{artifact_id}.png"
        await page.screenshot(path=str(path))
        self._last_screenshot = str(path)
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
            "structured": structured,
            "version": self.version,
            "elements": elements,
            "screenshot_id": artifact_id,
        }

    async def locate(self, args):
        page = await self.start()
        self.validate_url(page.url)
        if args["ref"] not in self.refs:
            raise ValueError("页面引用过期，请重新观察")
        locator = page.locator(f'[data-orbit-ref="{args["ref"]}"]')
        if await locator.count() == 1:
            return locator
        # 降级：ref 过期或 DOM 轻微变动，但元素其实还在——用 name/text 在最新 refs 里
        # 重新匹配找回新 ref，让「ref 过期」从断任务变成自动恢复。
        target = self.refs.get(args["ref"]) or {}
        target_name = (target.get("name") or "").strip()
        if target_name:
            refreshed = await page.evaluate(OBSERVE_JS)
            new_refs = {item["ref"]: item for item in refreshed}
            for item in refreshed:
                if (item.get("name") or "").strip() == target_name:
                    self.refs = new_refs
                    self.version += 1
                    self.observed_url = page.url
                    return page.locator(f'[data-orbit-ref="{item["ref"]}"]')
        raise ValueError("元素已变化，请重新观察")

    def last_screenshot_data_url(self) -> str | None:
        """最新观察截图的 base64 data URL，供视觉兜底喂给多模态模型。"""
        if not self._last_screenshot:
            return None
        try:
            import base64

            with open(self._last_screenshot, "rb") as fh:
                encoded = base64.b64encode(fh.read()).decode("ascii")
            return f"data:image/png;base64,{encoded}"
        except OSError:
            return None

    async def execute(self, name, args, task_id, operation_id):
        if name.startswith("notion_"):
            return await self.notion(name, args, task_id, operation_id)
        if name == "vault_login":
            return await self.login(args, task_id)
        page = await self.start()
        if name == "browser_navigate":
            self.validate_url(args["url"])
            try:
                return await self.open(args["url"], task_id, page)
            except Exception as exc:
                if not is_closed_error(exc):
                    raise
                # A cold browser can drop the first page. Reopen it once instead of
                # surfacing a target-closed error that the model can only retry blindly.
                await self.close()
                return await self.open(args["url"], task_id, await self.start())
        if name == "browser_observe":
            return {"status": "ok", "data": await self.observe(task_id)}
        before = await self.page_signature(page)
        if name == "browser_click":
            await (await self.locate(args)).click()
        elif name == "browser_click_at":
            self.validate_url(page.url)
            if args["version"] != self.version or page.url != self.observed_url:
                raise ValueError("页面引用过期，请重新观察后再点击")
            await page.mouse.click(args["x"], args["y"])
        elif name == "browser_fill":
            await (await self.locate(args)).fill(args["text"])
        elif name == "browser_press":
            self.validate_url(page.url)
            if args["key"] not in {"Enter", "Tab", "Escape", "ArrowDown", "ArrowUp"}:
                raise ValueError("不支持该按键")
            await page.keyboard.press(args["key"])
        elif name == "browser_scroll":
            self.validate_url(page.url)
            await page.mouse.wheel(0, args["pixels"])
        await self.wait_for_change(page, before)
        return {"status": "ok", "data": await self.observe(task_id)}

    async def settle(self, page, *, full: bool = False):
        """Single-page apps render after domcontentloaded. Look at them only once they settle.

        Both waits are best-effort: pages with polling requests may never reach networkidle,
        and a missing load state must never fail the action itself.
        """
        with suppress(Exception):
            await page.wait_for_load_state("load", timeout=8000 if full else 3000)
        with suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=3000 if full else 1500)

    async def page_signature(self, page) -> str:
        """Cheap signal that the page moved on: URL plus visible text length."""
        with suppress(Exception):
            text = (await page.locator("body").inner_text()).strip()
            return f"{page.url}|{len(text)}"
        return getattr(page, "url", "")

    async def wait_for_change(
        self, page, before: str, *, tries: int = 12, delay: float = 0.25, grace: int = 4
    ):
        """A click or Enter may navigate, or open a new tab.

        Without this the observation handed back describes the page as it was before the
        action, and the model concludes "nothing happened" and retries blindly.

        同页签名变化后**不立刻收工**：有些站点（B 站搜索结果就是）点击时先在当前页
        挂一个跟踪参数，真正的内容在新标签页里稍后才出现。没有宽限期就会在新标签页
        出现之前把旧页面当成结果交回去，模型据此判定"点了不跳转"而反复重试。
        宽限期内一旦发现新标签页，就跟随它（new_page 已经把 self.page 切过去了）。
        """
        changed_at: int | None = None
        for step in range(tries):
            current = await self.start()
            if current is not page:
                await self.settle(current, full=True)
                return
            if changed_at is None:
                if await self.page_signature(current) != before:
                    changed_at = step
            elif step - changed_at >= grace:
                await self.settle(current, full=True)
                return
            await asyncio.sleep(delay)
        await self.settle(page)

    async def open(self, url: str, task_id: str, page):
        self.refs = {}
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await self.settle(page, full=True)
        if response is not None and response.status >= 400:
            return {
                "status": "error",
                "error": f"目标网站返回 HTTP {response.status}，请检查网址或稍后重试",
            }
        return await self.verify_entry(page, task_id)

    async def verify_entry(self, page, task_id):
        """Report whether the page really loaded. A weak page is still returned for inspection."""
        title = (await page.title()).strip()
        text = (await page.locator("body").inner_text()).strip()
        data = await self.observe(task_id)
        if not title or len(text) < 200:
            data["entry_weak"] = True
            data["entry_note"] = (
                f"页面已打开，但正文只有 {len(text)} 字、标题为「{title or '无'}」，"
                "可能未真正进入目标页面或需要登录"
            )
        return {"status": "ok", "data": data}

    async def notion(self, name, args, task_id, operation_id):
        if not self.settings.notion_url:
            return {"status": "error", "error": "请先在设置中指定 Notion 页面 URL"}
        host = urlparse(self.settings.notion_url).hostname
        local_fixture = self.settings.headless and host in {"localhost", "127.0.0.1", "::1"}
        if not is_notion_host(host) and not local_fixture:
            return {
                "status": "error",
                "error": "该地址不是 Notion 页面，请在通用网页连接中使用浏览器工具",
            }
        self.validate_url(self.settings.notion_url)
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
