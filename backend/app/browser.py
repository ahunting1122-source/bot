import asyncio
import hashlib
import time
from collections import deque
from contextlib import suppress
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright
from pydantic import Field

from app.config import http_url, is_notion_host
from app.storage import Artifact, Operation
from app.tools import Args, Empty, Tool
from app.vault import Vault, cookie_host_matches, summarize_logins

# ── 四个新浏览器工具（MATRIX 5.14 的「可做但未做」桶）─────────────────────────
#
# browser_back / browser_get_images / browser_console / browser_dialog 都只用已有的
# BrowserController，不引入任何新依赖。下面这组常量是**所有上限的唯一出处**：
# 缓冲条数、返回条数、单条字符数都必须有界——否则一次长会话、一条 minified 报错、
# 一张 base64 内联图，任一个都能把内存或上下文预算吃光。

#: console / pageerror 的有界缓冲条数。
#:
#: **必须有限**：页面每秒可能吐几十条日志，无限缓冲最终会把长会话的内存吃光，
#: 而模型永远只看最后几十条。超限丢最旧（`deque(maxlen=)` 的语义），
#: 但丢了多少要单独记一笔——"你看到的是不是全部"必须能回答。
CONSOLE_BUFFER = 200
#: 单条 console 文本上限。minified 库的报错动辄几万字符，一条就能吃光整个预算。
CONSOLE_TEXT_CHARS = 2000
#: `browser_console` 默认回多少条（最近 N 条）。
CONSOLE_DEFAULT_LIMIT = 50
#: `browser_get_images` 默认回多少张。
IMAGES_DEFAULT_LIMIT = 30
#: 追踪像素阈值：宽高都 ≤ 这个值的图对模型没有任何信息量（典型是 1×1 统计像素）。
TRACKING_PIXEL_MAX = 2
#: 单条图片地址的字符上限。base64 内联图（`data:image/...`）动辄几十万字符，
#: 原样交给模型等于当场自爆预算。
IMAGE_SRC_CHARS = 2000
#: dialog 历史条数上限。模型只需要知道"刚才弹过什么"，不需要全部历史。
DIALOG_BUFFER = 20
#: 待处理 dialog 的自动 dismiss 宽限（秒）。为什么必须有它，见 `_on_dialog` 的注释。
DIALOG_GRACE_SECONDS = 2.0
#: 保留事件状态的页面数上限（每个页面一份 console / dialog 缓冲）。
#: 有界是为了让"一直开新标签页"的长会话不会把旧页面的缓冲永远攒在内存里。
PAGE_EVENTS_MAX = 8


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


class ImagesArgs(Args):
    """`browser_get_images` 的参数。"""

    limit: int = Field(
        default=IMAGES_DEFAULT_LIMIT,
        ge=1,
        le=200,
        description="最多返回几张图片（默认 30）。列表页常有几十上百张，先少取再看。",
    )


class ConsoleArgs(Args):
    """`browser_console` 的参数。"""

    limit: int = Field(
        default=CONSOLE_DEFAULT_LIMIT,
        ge=1,
        le=200,
        description="返回最近多少条日志（默认 50，含 pageerror 未捕获错误）",
    )


class DialogArgs(Args):
    """`browser_dialog` 的参数。"""

    action: str = Field(description="accept=确认（点「确定」）；dismiss=取消（点「取消」/关掉）")
    text: str = Field(
        default="",
        max_length=2000,
        description="prompt 输入框里要填的文本；只有 prompt 类型用得上，其余类型忽略",
    )


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
        "browser_back",
        "后退到上一页，返回新的 URL 与标题。**没有上一页时如实返回 went_back=false**，"
        "不会假装成功；后退后元素引用失效，需要重新观察。",
        Empty,
    ),
    Tool(
        "browser_get_images",
        "列出当前页面的图片：**绝对 URL** + alt 文本 + 宽高。相对地址已转成绝对地址，"
        "1×1 之类的追踪像素已过滤。要看页面上有什么图、或把图片地址交给别的工具时用它。",
        ImagesArgs,
    ),
    Tool(
        "browser_console",
        "读当前页面最近的 console 日志与 JS 未捕获错误（pageerror），默认最近 50 条。"
        "日志从页面创建时就开始收集，所以能看到打开页面期间发生的报错。",
        ConsoleArgs,
    ),
    Tool(
        "browser_dialog",
        "处理页面弹出的原生对话框（alert / confirm / prompt / beforeunload）："
        "accept=确认，dismiss=取消；prompt 可用 text 填输入框。"
        "**没有待处理对话框时如实返回 handled=false**。",
        DialogArgs,
    ),
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


#: 抽图片用。**只抽原始数据，不做判断**：过滤与相对 URL 转换全在 Python 侧
#: （:func:`normalize_images`），因为那部分能离线单测，而 JS 里写的逻辑只能靠真浏览器验。
#:
#: 上限 200 张：图库里几百张图时没必要全搬回 Python 再截断，跨进程传一趟就是白花时间。
IMAGES_JS = """() => {
  const out = [];
  for (const img of document.querySelectorAll('img')) {
    if (out.length >= 200) break;
    const rect = img.getBoundingClientRect();
    out.push({
      src: img.currentSrc || img.getAttribute('src') || '',
      alt: img.getAttribute('alt') || '',
      // naturalWidth 是图片的真实像素尺寸（判断追踪像素要靠它，不能靠 CSS 布局尺寸）。
      width: Math.round(img.naturalWidth || rect.width || 0),
      height: Math.round(img.naturalHeight || rect.height || 0),
    });
  }
  return out;
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


# ── 四个新工具的纯逻辑（**不启浏览器也能测**）──────────────────────────────────
#
# 这一段刻意不碰 playwright：图片过滤与相对 URL 转换、console 条目裁剪、dialog 状态机
# 都是纯对象/纯函数。于是没有 chromium、没有网络的机器上也能把这些逻辑测透；
# 真浏览器那一层只负责"把事件喂进来"，判定规则不写第二遍。


def clock_text(at: str | None = None) -> str:
    """日志/对话框的时间戳（本地时间，精确到秒）。

    只保留到秒：模型要的是"这几条谁先谁后"，毫秒级时间戳只是多占字符。
    """
    return at or time.strftime("%H:%M:%S")


def console_entry(level, text, at: str | None = None) -> dict:
    """一条 console 记录的**唯一**形状：只有 level / text / 时间。

    为什么不把整条 `ConsoleMessage`（或它的 JSHandle）交给模型：那些对象的
    可序列化内容要么为空、要么把一整棵远程对象描述塞进上下文，模型既看不懂也用不上。
    """
    return {
        "level": str(level or "log")[:40],
        "text": str(text or "")[:CONSOLE_TEXT_CHARS],
        "at": clock_text(at),
    }


class ConsoleLog:
    """一个页面的 console / pageerror 有界缓冲。

    为什么要自己数 ``dropped``：只看最后 50 条时，"这个页面到底吐了 5 条还是 500 条"
    是判断"页面是不是在报错刷屏"的关键差别。丢了多少必须能被说出来，
    否则"只看到最后几条"会被误读成"只有这几条"。
    """

    def __init__(self, capacity: int = CONSOLE_BUFFER):
        self.capacity = max(1, int(capacity))
        self.entries: deque[dict] = deque(maxlen=self.capacity)
        self.dropped = 0

    def add(self, level, text, at: str | None = None) -> dict:
        """追加一条。缓冲满了就丢最旧的那条，并把丢弃计数加一。"""
        if len(self.entries) == self.capacity:
            self.dropped += 1
        entry = console_entry(level, text, at)
        self.entries.append(entry)
        return entry

    def tail(self, limit: int = CONSOLE_DEFAULT_LIMIT) -> list[dict]:
        """最近 ``limit`` 条（顺序照发生先后，最早的在前面）。"""
        return list(self.entries)[-max(1, int(limit)) :]

    def snapshot(self, limit: int = CONSOLE_DEFAULT_LIMIT) -> dict:
        """给模型的载荷：最近 N 条 + "总共收了多少、丢了多少"。"""
        entries = self.tail(limit)
        return {
            "entries": entries,
            "count": len(entries),
            "total": len(self.entries),
            "dropped": self.dropped,
            "limit": max(1, int(limit)),
        }

    def clear(self) -> None:
        self.entries.clear()
        self.dropped = 0


def _pixels(value) -> int:
    """把 JS 侧给的尺寸转成整数像素（坏值按 0 处理，不抛错——一张图不该让整次调用失败）。"""
    try:
        return max(0, int(float(value)))
    except (TypeError, ValueError):
        return 0


def normalize_images(
    raw, base_url: str, limit: int = IMAGES_DEFAULT_LIMIT
) -> dict:
    """把页面里抽出的 img 清单整成模型能直接用的形状。

    三件事都在这里做，因为它们都是纯逻辑、都能离线测：

    * **相对 URL 转绝对**（用页面 URL 做 base）。模型拿到 ``/img/a.png`` 没法用——
      它不知道该拼哪个站点；
    * **过滤追踪像素**（宽高都 ≤ :data:`TRACKING_PIXEL_MAX`）。1×1 统计像素对模型
      零信息量，只占位置。注意 0×0（懒加载还没加载出来的图）同样被滤掉，理由相同：
      尺寸都不知道的图，模型也没法用；
    * **去重与截断**。同一张图在页面上出现多次（logo、占位图）只留一条。

    返回里带 ``skipped`` 计数：少了几张必须能解释，静默丢弃会让模型以为页面只有这么点图。
    """
    count = max(1, int(limit))
    images: list[dict] = []
    seen: set[str] = set()
    skipped = {"empty": 0, "too_long": 0, "tracking_pixel": 0, "duplicate": 0, "over_limit": 0}
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        src = str(item.get("src") or "").strip()
        if not src:
            skipped["empty"] += 1
            continue
        # urljoin 同时处理绝对地址（原样返回）、站内绝对路径（补 host）与相对路径。
        absolute = urljoin(base_url or "", src)
        if len(absolute) > IMAGE_SRC_CHARS:
            skipped["too_long"] += 1
            continue
        width, height = _pixels(item.get("width")), _pixels(item.get("height"))
        if width <= TRACKING_PIXEL_MAX and height <= TRACKING_PIXEL_MAX:
            skipped["tracking_pixel"] += 1
            continue
        if absolute in seen:
            skipped["duplicate"] += 1
            continue
        if len(images) >= count:
            skipped["over_limit"] += 1
            continue
        seen.add(absolute)
        images.append(
            {
                "src": absolute,
                "alt": str(item.get("alt") or "")[:200],
                "width": width,
                "height": height,
            }
        )
    return {"images": images, "count": len(images), "base": base_url, "skipped": skipped}


class DialogLog:
    """页面原生对话框（alert / confirm / prompt / beforeunload）的状态机。

    为什么单独抽出来：Playwright 有一条硬约定——**有** dialog 监听器就必须由监听器
    accept/dismiss，否则页面冻结、导航不发生。于是这里必须同时做三件事，少一件就出事：

    1. **记录**：模型要看得见"刚才弹过什么、什么类型、默认值是什么"；
    2. **暂存**：把活着的 dialog 对象留住，等 ``browser_dialog`` 按模型的意愿处理；
    3. **兜底**：没人及时处理就自动 dismiss（见 ``BrowserController._on_dialog``），
       绝不把整次工具调用挂死。

    这三件事的顺序也是死的：先记录再暂存，否则"待处理"永远查不到历史。
    """

    def __init__(self, capacity: int = DIALOG_BUFFER):
        self.capacity = max(1, int(capacity))
        self.entries: deque[dict] = deque(maxlen=self.capacity)
        #: 活着的 playwright Dialog 对象（只在 pending 期间非 None）。
        self.dialog = None
        #: 待处理的那条记录（和 ``entries`` 里是同一条 dict，就地改 ``action``）。
        self.pending: dict | None = None

    def record(self, kind: str, message: str, default_value: str = "", at=None) -> dict:
        entry = {
            "type": str(kind or "dialog")[:40],
            "message": str(message or "")[:CONSOLE_TEXT_CHARS],
            "default_value": str(default_value or "")[:200],
            "at": clock_text(at),
            "action": "",
        }
        self.entries.append(entry)
        self.pending = entry
        return entry

    def bind(self, dialog) -> None:
        """把活着的 Dialog 对象挂到当前待处理记录上。"""
        self.dialog = dialog

    def is_pending(self, entry) -> bool:
        """这条记录是不是仍然是待处理的那一条（兜底任务用它判断"还要不要动手"）。"""
        return self.pending is entry

    def take(self):
        """取出待处理的那个 ``(dialog, entry)``；没有就返回 None。

        **只取不处理**：真正的 accept/dismiss 由调用方做，因为那一步要 await 且可能失败。
        """
        if self.pending is None or self.dialog is None:
            return None
        return self.dialog, self.pending

    def resolve(self, action: str) -> dict | None:
        """把待处理记录标成已处理（``action`` 写进历史，模型据此知道它被谁处理了）。"""
        entry = self.pending
        if entry is None:
            return None
        entry["action"] = action
        self.pending = None
        self.dialog = None
        return entry

    def snapshot(self, limit: int = 5) -> dict:
        """给模型的载荷：有没有待处理的、它是哪一条、最近几条历史。"""
        recent = [dict(item) for item in list(self.entries)[-max(1, int(limit)) :]]
        return {
            "pending": self.pending is not None,
            "dialog": dict(self.pending) if self.pending is not None else None,
            "recent": recent,
            "total": len(self.entries),
        }

    def clear(self) -> None:
        self.entries.clear()
        self.pending = None
        self.dialog = None


class PageEvents:
    """一个页面一份的事件状态：console 缓冲 + dialog 状态机 + 是否已挂监听器。

    为什么按页面存而不是整个控制器一份：新标签页是**另一份** console/dialog 流。
    混在一起，模型会把 A 页的报错当成 B 页的——排查方向当场跑偏。
    """

    __slots__ = ("console", "dialogs", "attached")

    def __init__(self):
        self.console = ConsoleLog()
        self.dialogs = DialogLog()
        #: 监听器是否已挂上。重复挂会让同一条日志收两遍。
        self.attached = False

    def clear(self) -> None:
        self.console.clear()
        self.dialogs.clear()


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
        #: 页面对象 → 该页面的事件状态（有界，见 :data:`PAGE_EVENTS_MAX`）。
        self._page_events: dict = {}

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
                # 持久化上下文可能自带一个已经存在的页面：它不走 new_page()，所以这里
                # 也得挂一次监听器，否则"打开就有页面"的情况下 console 永远收不到东西。
                if self.page is not None:
                    self._attach_listeners(self.page)
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
        # 页面没了，挂在它们上面的缓冲也就没意义了（留着只会让下一次会话读到旧日志）。
        self._clear_page_events()
        # The close event can arrive before the pending context.close() RPC returns.
        # Stopping the driver here cuts that response off with TargetClosedError.
        # The next start()/close() cleans up the driver under the lifecycle lock.

    def _clear_observation(self):
        self.refs = {}
        self.observed_url = ""
        self.version += 1

    def _clear_page_events(self):
        for state in self._page_events.values():
            state.clear()
        self._page_events = {}

    def new_page(self, page):
        self.page = page
        self._clear_observation()
        # 监听器必须**在页面创建时**挂上（这里就是页面进入控制器的唯一入口）。
        # 等模型想起调 browser_console 才挂，那些日志早就随页面过去了，工具永远是空的。
        self._attach_listeners(page)

    def page_events(self, page=None) -> PageEvents:
        """取（必要时新建）某个页面的事件状态。``page`` 留空 = 当前页面。

        没有页面时返回一份**空**状态而不是抛错：``browser_console`` 在浏览器还没开时
        回"一条日志都没有"比回一个异常更有用（模型据此知道"页面还没打开"）。
        """
        target = self.page if page is None else page
        if target is None:
            return PageEvents()
        state = self._page_events.get(target)
        if state is None:
            state = PageEvents()
            self._page_events[target] = state
            # 有界：一直开新标签页的长会话不能让旧页面的缓冲永远攒着。
            while len(self._page_events) > PAGE_EVENTS_MAX:
                self._page_events.pop(next(iter(self._page_events)))
        return state

    def _attach_listeners(self, page) -> None:
        """给一个页面挂 console / pageerror / dialog 监听器（每个页面只挂一次）。"""
        state = self.page_events(page)
        if state.attached:
            return
        register = getattr(page, "on", None)
        if not callable(register):
            # 页面对象不支持事件（测试替身 / 精简实现）：挂不上只意味着收不到日志，
            # 不该让"打开页面"这件事本身失败。
            return
        state.attached = True
        register("console", lambda message: state.console.add(message.type, message.text))
        register("pageerror", lambda error: state.console.add("pageerror", str(error)))
        register("dialog", lambda dialog: self._on_dialog(state, dialog))

    def _on_dialog(self, state: PageEvents, dialog) -> None:
        """dialog 事件的落地：记录 + 暂存 + 起一个兜底任务。

        **为什么必须有兜底**：Playwright 的约定是"有监听器就得由监听器 accept/dismiss，
        否则页面冻结"。而模型要先看到 dialog 才能决定 accept 还是 dismiss——中间这段
        时间页面本来就卡着。所以这里给它一个宽限期（:data:`DIALOG_GRACE_SECONDS`）：
        模型来得及就由 ``browser_dialog`` 按它的意愿处理，来不及就自动 dismiss。
        宁可让模型看到"这条被兜底处理了"，也不能让整次工具调用挂死到超时。
        """
        entry = state.dialogs.record(
            dialog.type, dialog.message, getattr(dialog, "default_value", "") or ""
        )
        state.dialogs.bind(dialog)
        with suppress(RuntimeError):
            asyncio.get_running_loop().create_task(self._auto_dismiss(state, dialog, entry))

    async def _auto_dismiss(self, state: PageEvents, dialog, entry) -> None:
        await asyncio.sleep(DIALOG_GRACE_SECONDS)
        if not state.dialogs.is_pending(entry):
            return
        with suppress(Exception):
            await dialog.dismiss()
        state.dialogs.resolve("dismiss（兜底自动处理）")

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
        # console / dialog 缓冲跟着会话走：关掉浏览器之后还留着旧日志，下一轮会话的模型
        # 会把上一轮的报错当成现在的。
        self._clear_page_events()
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

    async def go_back(self, page):
        """后退到上一页，并把"到底退没退"如实说清。

        `page.go_back()` 的返回值有两种"没有 HTTP 响应"的情形，必须分开：
        真正没有上一页，与"上一页是 data:/about: 这类无响应导航（或同页跳转）"。
        两者的返回值都是 None，只靠返回值判断会把"确实退回去了"报成"没退"
        （反之，若一律当成后退成功，模型就会拿旧 refs 去点新页面）。
        所以判据是 **URL 有没有变**，返回值只用来补状态码。
        """
        before = page.url
        response = await page.go_back()
        moved = page.url != before
        if not moved:
            return {
                "status": "ok",
                "data": {
                    "url": page.url,
                    "title": await page.title(),
                    "went_back": False,
                    "message": "没有上一页可后退：浏览器历史里只有当前这一页。",
                },
            }
        await self.settle(page, full=True)
        # 回到的是另一页，旧 refs 必须作废——否则模型会拿上一页的引用去点这一页。
        self._clear_observation()
        data = {
            "url": page.url,
            "title": await page.title(),
            "went_back": True,
            "version": self.version,
            "message": "已后退一页；元素引用已失效，需要重新观察。",
        }
        if response is not None:
            data["status_code"] = response.status
        else:
            data["note"] = "这次后退没有 HTTP 响应（同页跳转或 data:/about: 页面），所以没有状态码。"
        return {"status": "ok", "data": data}

    async def images(self, limit: int = IMAGES_DEFAULT_LIMIT) -> dict:
        """抽当前页面的图片。判定规则全在 :func:`normalize_images` 里（可离线测）。"""
        page = await self.start()
        raw = await page.evaluate(IMAGES_JS)
        return normalize_images(raw, page.url, limit)

    async def handle_dialog(self, args):
        """处理待处理的对话框：accept / dismiss。

        **没有待处理 dialog 时如实返回 handled=false**。静默返回"成功"是最坏的一种谎：
        模型会以为弹窗已经处理、页面可以继续，于是在"页面其实还卡着"的状态下继续点。
        """
        action = str(args.get("action") or "").strip().lower()
        if action not in {"accept", "dismiss"}:
            raise ValueError("action 只能是 accept 或 dismiss")
        page = self.page
        state = self.page_events(page)
        taken = state.dialogs.take()
        if taken is None:
            data = state.dialogs.snapshot()
            data["handled"] = False
            data["message"] = (
                "当前没有待处理的对话框。"
                if not data["recent"]
                else "当前没有待处理的对话框；最近弹过的几条见 recent（action 说明它们被谁处理了）。"
            )
            return {"status": "ok", "data": data}
        dialog, entry = taken
        text = str(args.get("text") or "")
        try:
            if action == "accept":
                # prompt 才有输入框；其余类型传文本会被 playwright 拒绝，所以按类型分开调。
                if entry.get("type") == "prompt" and text:
                    await dialog.accept(text)
                else:
                    await dialog.accept()
            else:
                await dialog.dismiss()
        except Exception as exc:  # 对话框可能已被页面自己关掉，或页面已经没了
            state.dialogs.resolve(f"{action} 失败")
            return {"status": "error", "error": f"处理对话框失败：{str(exc)[:400]}"}
        state.dialogs.resolve(action)
        if page is not None:
            # accept 之后可能触发导航（确认删除→跳走），等它稳定再交回结果。
            await self.settle(page)
        data = state.dialogs.snapshot()
        data["handled"] = True
        data["action"] = action
        data["dialog"] = dict(entry)
        data["url"] = page.url if page is not None else ""
        return {"status": "ok", "data": data}

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
        # ── 四个只读/历史类工具：不经过 wait_for_change ────────────────────────
        # 它们都不"操作页面"，所以既不该付等待变更的代价，也不该刷新观察版本。
        if name == "browser_back":
            return await self.go_back(page)
        if name == "browser_get_images":
            return {
                "status": "ok",
                "data": await self.images(args.get("limit", IMAGES_DEFAULT_LIMIT)),
            }
        if name == "browser_console":
            return {
                "status": "ok",
                "data": self.page_events().console.snapshot(
                    args.get("limit", CONSOLE_DEFAULT_LIMIT)
                ),
            }
        if name == "browser_dialog":
            return await self.handle_dialog(args)
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
