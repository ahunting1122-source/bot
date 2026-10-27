"""联网工具：`web_search` 与 `web_extract`（W4，对标 Hermes 的 web 工具面）。

**接入点（下一波接线）**：`app/main.py` 里

    from app.web import WEB_TOOLS, WebTools
    app.state.web = WebTools(settings)
    registry.register(WEB_TOOLS, app.state.web.execute)

本轮不接 `main.py`（它不在可改范围内），只保证 `WEB_TOOLS` 与 `execute` 存在、
可测，且不依赖浏览器进程。

两条工具的分工（照 Hermes）：

- `web_search` 只负责"找到候选 URL"，每条结果必须带 `url`——Orbit 的引用回填只认
  url / path 两种出处（`app/indexing.py::origin_of`），没有 url 的结果等于不可引用。
- `web_extract` 只负责"把已知 URL 读成正文"，且必须过 URL 安全闸：只允许
  http/https，本机 / 内网 / 云元数据地址、`file:` / `data:` / `javascript:` 全拒。

为什么这两条工具是刚需：Orbit 是"网页操作特化"的 agent，但此前上网只能靠浏览器
一页页点——找资料这种"先搜索再精读"的活，用浏览器做等于把搜索引擎当成手工翻页。
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import socket
import time
from collections.abc import Callable
from contextlib import suppress
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import Field

from app.indexing import split_blocks
from app.tools import Args, Tool
from app.web_providers import (
    DEFAULT_COUNT,
    MAX_COUNT,
    USER_AGENT,
    SearchProvider,
    WebProviderError,
    build_providers,
    normalize_freshness,
)

log = logging.getLogger("orbit.web")

#: 正文默认返回上限（字符）。8000 是"够模型据此作答、又不至于挤掉上下文"的量级。
DEFAULT_MAX_CHARS = 8000
#: 正文硬上限。与 `browser_observe` 的 24000 对齐：单次工具结果别超过这个量级。
MAX_MAX_CHARS = 24000
#: 页面字节上限。超过 3 MB 的基本不是文章页（是下载站、图集或压缩包索引），
#: 整页读进内存只会浪费预算。
MAX_HTML_BYTES = 3 * 1024 * 1024
#: 抽取结果缓存 TTL（秒）。20 分钟对齐 Hermes `web.cache_ttl_minutes` 默认值：
#: 同一轮任务里"抽一遍、引用核对时再抽一遍"是常态，跨任务的新鲜度要求也没那么紧。
CACHE_TTL_SECONDS = 1200.0


class WebSearchArgs(Args):
    query: str = Field(
        min_length=1,
        max_length=200,
        description="搜索词。可以带引号短语、site:、filetype: 这类限定符，provider 会原样透传。",
    )
    count: int = Field(
        default=DEFAULT_COUNT,
        ge=1,
        le=MAX_COUNT,
        description=f"返回条数上限（最多 {MAX_COUNT} 条）。默认 {DEFAULT_COUNT}。",
    )
    freshness: str = Field(
        default="",
        description="时间范围：day / week / month / year，留空表示不限时间。",
    )


class WebExtractArgs(Args):
    url: str = Field(
        min_length=1,
        max_length=2000,
        description="要抽取正文的 http(s) 网址。只能是公网地址：本机与内网会被拒绝。",
    )
    max_chars: int = Field(
        default=DEFAULT_MAX_CHARS,
        ge=200,
        le=MAX_MAX_CHARS,
        description=f"返回正文的字符上限（最多 {MAX_MAX_CHARS}）。默认 {DEFAULT_MAX_CHARS}。",
    )


WEB_TOOLS = [
    Tool(
        "web_search",
        "在公网上搜索网页，返回标题、来源 URL 与摘要。需要先确认资料在哪时用它，"
        "不要用浏览器一页页点；拿到 URL 后再用 web_extract 读正文。"
        f"每次最多 {MAX_COUNT} 条。",
        WebSearchArgs,
    ),
    Tool(
        "web_extract",
        "读取一个 http(s) 网页的正文纯文本（不走模型、不做摘要），返回文本、最终 URL "
        "与是否被截断。只能访问公网地址：本机、内网、云元数据地址会被拒绝。",
        WebExtractArgs,
    ),
]


# ── URL 安全闸 ────────────────────────────────────────────────────────────────

#: 永远拒绝的主机名：云元数据端点的 DNS 别名。**没有开关**——照 Hermes
#: `url_safety.py` 的"security floor"：这些地址的唯一用途就是读本机/云厂商凭证，
#: 任何"允许私有地址"的选项都不该把它们放出来。
BLOCKED_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "metadata",
    }
)

#: 单看 IP 就该拒的固定地址（各家云厂商的元数据端点）。
BLOCKED_IPS = frozenset(
    ipaddress.ip_address(value)
    for value in (
        "169.254.169.254",  # AWS / GCP / Azure / DO / Oracle 通用元数据
        "169.254.170.2",  # AWS ECS task metadata（含 task IAM 凭证）
        "169.254.169.253",  # Azure IMDS wire server
        "100.100.100.200",  # 阿里云元数据
        "fd00:ec2::254",  # AWS 元数据（IPv6）
    )
)

#: 整段拒绝的网段：链路本地（元数据就在里面，且没有合法的公网用途）。
BLOCKED_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fd00:ec2::/64"),
)

#: 内网后缀。这些短名在本机解析出来的都是内网地址，连解析都不必做。
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa")


def blocked_ip_reason(value: Any) -> str:
    """IP 落在不该被访问的段里就返回中文理由，否则返回空串。"""
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return "无法识别的地址"
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        # `::ffff:169.254.169.254` 这类映射形式必须按内层 IPv4 判，
        # 否则绕过一次就能直连元数据端点。
        reason = blocked_ip_reason(mapped)
        if reason:
            return reason
    if address in BLOCKED_IPS or any(address in network for network in BLOCKED_NETWORKS):
        return "这是云厂商元数据 / 链路本地地址（能读到实例凭证）"
    if address.is_loopback:
        return "这是本机回环地址"
    if address.is_link_local:
        return "这是链路本地地址"
    if address.is_private:
        return "这是私有网段地址"
    if address.is_reserved or address.is_unspecified or address.is_multicast:
        return "这是保留 / 未指定 / 组播地址"
    return ""


def resolve_host(host: str) -> list[str]:
    """把主机名解析成 IP 列表。**解析失败按拒绝处理**（见 `check_url`）。"""
    try:
        info = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError as exc:
        raise ValueError(f"域名解析失败，已按安全策略拒绝：{host}") from exc
    return [str(item[4][0]).split("%", 1)[0] for item in info]


def check_url(url: str, *, resolve: Callable[[str], list[str]] | None = None) -> str:
    """URL 安全闸：只放行指向公网的 http/https 地址，不合格抛 `ValueError`（中文理由）。

    为什么要有这道闸（"反正只是只读"不成立）：`web_extract` 会把任意 URL 的响应
    正文读进模型上下文。放行 `file:` 就是本地任意文件读取；放行 `127.0.0.1` / 私有
    网段就是拿这台机器当跳板打内网；放行 `169.254.169.254` 就是读云厂商实例凭证。
    这三条是 SSRF 的经典路径，成本极低、后果极重，所以**默认全拒**，不提供
    "允许私有地址"的开关。

    `resolve` 可注入（测试用）。默认实现会做 DNS 解析再判一次 IP：只判主机名字面
    会被 `http://evil.example`（A 记录指向 127.0.0.1）绕过。解析失败一律拒绝
    （fail closed）——宁可读不到页面，也不能在"解析不出来"的模糊状态下放行。
    """
    text = (url or "").strip()
    if not text:
        raise ValueError("网址不能为空")
    parsed = urlparse(text)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        raise ValueError(
            f"只允许 http/https 网址（收到 {scheme or '空协议'}）。"
            "file: / data: / javascript: 等协议一律拒绝：它们能读本地文件或执行脚本，不是网页。"
        )
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError("网址缺少主机名")
    if parsed.username or parsed.password:
        raise ValueError("网址里不能带用户名或密码（会被日志与索引记录下来）")
    if host in BLOCKED_HOSTNAMES or host == "localhost" or host.endswith(BLOCKED_SUFFIXES):
        raise ValueError(f"拒绝访问本机 / 内网主机：{host}")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        reason = blocked_ip_reason(literal)
        if reason:
            raise ValueError(f"拒绝访问 {host}：{reason}")
        return text
    addresses = (resolve or resolve_host)(host)
    if not addresses:
        raise ValueError(f"域名没有解析出任何地址，已按安全策略拒绝：{host}")
    for address in addresses:
        reason = blocked_ip_reason(address)
        if reason:
            raise ValueError(f"拒绝访问 {host}（解析到 {address}）：{reason}")
    return text


# ── HTML → 正文 ──────────────────────────────────────────────────────────────

#: 只带导航/脚本/装饰的标签：它们的内容不是正文，整段跳过。
SKIP_TAGS = frozenset(
    {
        "script",
        "style",
        "noscript",
        "svg",
        "iframe",
        "template",
        "nav",
        "header",
        "footer",
        "aside",
        "form",
        "button",
        "select",
        "option",
    }
)
HEADING_TAGS = {f"h{level}": level for level in range(1, 7)}
BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "section",
        "article",
        "main",
        "br",
        "li",
        "blockquote",
        "pre",
        "tr",
        "td",
        "th",
        "dd",
        "dt",
        "figcaption",
        "table",
        "ul",
        "ol",
        "dl",
        *HEADING_TAGS,
    }
)


def _squeeze(text: str) -> str:
    """把连续空白压成一个空格：HTML 里的换行与缩进都是排版噪声。"""
    return re.sub(r"\s+", " ", text or "").strip()


class _PageTextParser(HTMLParser):
    """HTML → Markdown 风格正文。

    约定与 `app/browser.py::EXTRACT_JS` 一致（`#` 标题、`- ` 列表、`>` 引用、
    `|` 表格行）：浏览器读到的和 `web_extract` 读到的必须是**同一种文本**，否则
    同一页在两条路径下的片段对不上，检索命中也没法互相印证。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.blocks: list[str] = []
        self._skip = 0
        self._pre = 0
        self._in_title = False
        self._kind = "p"
        self._buffer: list[str] = []
        self._row: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "title":
            self._in_title = True
            return
        if tag in SKIP_TAGS:
            self._skip += 1
            return
        if self._skip:
            return
        if tag == "pre":
            self._flush()
            self._pre = 1
            self._kind = "pre"
            return
        if tag == "tr":
            self._flush()
            self._row = []
            return
        if tag in BLOCK_TAGS:
            self._flush()
            if tag in HEADING_TAGS:
                self._kind = f"h{HEADING_TAGS[tag]}"
            elif tag == "li":
                self._kind = "li"
            elif tag == "blockquote":
                self._kind = "quote"
            elif tag not in {"td", "th"}:
                self._kind = "p"

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self._in_title = False
            return
        if tag in SKIP_TAGS:
            if self._skip:
                self._skip -= 1
            return
        if self._skip:
            return
        if tag == "pre":
            self._flush()
            self._pre = 0
            self._kind = "p"
            return
        if tag == "tr":
            self._flush()
            if self._row:
                self.blocks.append("| " + " | ".join(self._row) + " |")
            self._row = None
            return
        if tag in BLOCK_TAGS:
            self._flush()
            self._kind = "p"

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title += data
            return
        if self._skip:
            return
        self._buffer.append(data)

    def _flush(self) -> None:
        """收束当前块。表格单元格先攒进行，行尾才成一块。"""
        raw = "".join(self._buffer)
        self._buffer = []
        if self._pre:
            body = raw.strip("\n").rstrip()
            if body.strip():
                self.blocks.append(f"```\n{body}\n```")
            return
        text = _squeeze(raw)
        if not text:
            return
        if self._row is not None:
            self._row.append(text)
            return
        if self._kind.startswith("h"):
            self.blocks.append("#" * min(int(self._kind[1:]), 6) + " " + text)
        elif self._kind == "li":
            self.blocks.append("- " + text)
        elif self._kind == "quote":
            self.blocks.append("> " + text)
        else:
            self.blocks.append(text)


def _restore_headings(markdown: str) -> str:
    """用索引侧的 `split_blocks` 归一结构，再把标题按层级还原成 `#` 行。

    为什么绕这一圈：`split_blocks` 已经把"标题 / 表格行 / 列表 / 空行分段"的判定
    写好了（`app/indexing.py`），再在 web.py 里写一套就会有两种切法——同一页
    "抽出来读到的"和"入库可检索的"边界不一致，引用核对时对不上。它把标题存成
    `title_path`（`A > B`），所以这里按路径的共同前缀还原层级。
    """
    blocks = split_blocks(markdown)
    if not blocks:
        return markdown.strip()
    lines: list[str] = []
    path: list[str] = []
    for block in blocks:
        current = [part for part in block.title_path.split(" > ") if part]
        shared = 0
        while shared < min(len(path), len(current)) and path[shared] == current[shared]:
            shared += 1
        for depth in range(shared, len(current)):
            lines.append("#" * min(depth + 1, 6) + " " + current[depth])
        path = current
        lines.append(block.text)
    return "\n\n".join(lines).strip()


def extract_main_text(html: str) -> tuple[str, str]:
    """HTML → (标题, 正文)。正文是 Markdown 风格纯文本（纯 CPU，调用方放线程里跑）。"""
    parser = _PageTextParser()
    parser.feed(html)
    parser.close()
    markdown = "\n\n".join(parser.blocks)
    return _squeeze(parser.title), _restore_headings(markdown)


# ── 抽取结果缓存 ─────────────────────────────────────────────────────────────


class ExtractCache:
    """抽取结果缓存：内存字典 + `<data_dir>/cache/web/` 落盘。

    为什么两层：内存挡同一轮任务里的重复抽取（最常见——模型抽一遍、引用核对时又
    抽一遍），落盘挡跨进程 / 重启后的重复抓取。

    **只缓存成功结果**：把失败也缓存住，会让一次网络抖动在整个 TTL 内看起来像
    "这页永远打不开"，模型只能换个说法再试一次。
    """

    def __init__(self, root: Path, ttl: float = CACHE_TTL_SECONDS) -> None:
        self.root = Path(root)
        self.ttl = ttl
        self._memory: dict[str, dict[str, Any]] = {}

    def _path(self, url: str) -> Path:
        host = (urlparse(url).hostname or "page").replace(":", "_")
        slug = re.sub(r"[^A-Za-z0-9._-]", "-", host)[:60].strip("-") or "page"
        digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        return self.root / f"{slug}-{digest}.json"

    def get(self, url: str) -> dict[str, Any] | None:
        now = time.time()
        entry = self._memory.get(url)
        if entry is not None:
            if now - float(entry.get("fetched_at") or 0) < self.ttl:
                return entry
            self._memory.pop(url, None)
        path = self._path(url)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or now - float(entry.get("fetched_at") or 0) >= self.ttl:
            # 过期或损坏的条目直接删掉：留在盘上只会让下次再读一遍坏数据。
            with suppress(OSError):
                path.unlink()
            return None
        self._memory[url] = entry
        return entry

    def put(self, url: str, payload: dict[str, Any]) -> None:
        # 缓存键是**请求**的 URL，`payload["url"]` 保留**最终** URL（重定向后）。
        # 两个都存：键要保证下次同参数能命中，返回值要能告诉模型"正文其实来自哪一页"。
        entry = {**payload, "requested": url, "fetched_at": time.time()}
        self._memory[url] = entry
        path = self._path(url)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:  # 缓存落盘失败不影响本次结果
            log.debug("抽取缓存落盘失败（忽略）：%s", exc)


# ── 工具执行体 ───────────────────────────────────────────────────────────────


class WebTools:
    """`web_search` / `web_extract` 的执行体（形状照 `BrowserController.execute`）。

    客户端、provider 与缓存都挂在实例上长期复用：每次工具调用都新建 httpx 客户端
    会白扔连接池，也会让缓存形同虚设。
    """

    def __init__(
        self,
        settings: Any,
        client: httpx.AsyncClient | None = None,
        providers: list[SearchProvider] | None = None,
        cache: ExtractCache | None = None,
        resolve: Callable[[str], list[str]] | None = None,
    ) -> None:
        self.settings = settings
        self._client = client
        self._providers = providers
        self._cache = cache or ExtractCache(Path(settings.data_dir) / "cache" / "web")
        #: 注入点：测试给假解析，避免真实 DNS（仓库规则：测试不发网络请求）。
        self._resolve = resolve

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=20.0, follow_redirects=True, headers={"User-Agent": USER_AGENT}
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def providers(self) -> list[SearchProvider]:
        if self._providers is None:
            self._providers = build_providers(self.settings, self.client())
        return self._providers

    async def execute(
        self, name: str, args: dict, task_id: str = "", operation_id: str = ""
    ) -> dict:
        """唯一执行入口。失败一律抛异常，由 `ToolRegistry.execute` 落成 error 状态。"""
        if name == "web_search":
            return {"status": "ok", "data": await self.search(args)}
        if name == "web_extract":
            return {"status": "ok", "data": await self.extract(args)}
        raise ValueError(f"未知工具：{name}")

    async def search(self, args: dict) -> dict:
        """搜索并按 provider 优先级回退。

        回退是照 Hermes 的做法：单家失败（限流 / key 过期 / 5xx）不该让整次搜索失败，
        换下一家往往就成。**只在 provider 层失败时回退**——参数错（比如 freshness
        写错）直接报错，别让用户以为"换了后端就好了"。
        """
        query = str(args.get("query") or "").strip()
        if not query:
            raise ValueError("query 不能为空")
        count = max(1, min(int(args.get("count") or DEFAULT_COUNT), MAX_COUNT))
        freshness = normalize_freshness(args.get("freshness"))
        errors: list[str] = []
        for provider in self.providers():
            if not provider.available(self.settings):
                continue
            try:
                results = await provider.search(query, count=count, freshness=freshness or None)
            except WebProviderError as exc:
                errors.append(f"{provider.name}: {exc}")
                log.info("搜索 provider %s 失败，换下一家：%s", provider.name, exc)
                continue
            return {
                "query": query,
                "provider": provider.name,
                "count": len(results),
                "results": [
                    {
                        "title": item.title,
                        "url": item.url,
                        "snippet": item.snippet,
                        "published": item.published,
                    }
                    for item in results
                ],
            }
        if errors:
            raise WebProviderError("所有搜索 provider 都失败了：" + "；".join(errors))
        raise WebProviderError("没有可用的搜索 provider")

    async def extract(self, args: dict) -> dict:
        """取页面 → 抽正文 → 截断。同 URL 在 TTL 内直接命中缓存。"""
        url = str(args.get("url") or "").strip()
        max_chars = int(args.get("max_chars") or DEFAULT_MAX_CHARS)
        max_chars = max(1, min(max_chars, MAX_MAX_CHARS))
        # 安全闸放线程里跑：默认实现要做 DNS 解析（阻塞调用），不能压在事件循环上。
        safe = await asyncio.to_thread(check_url, url, resolve=self._resolve)
        cached = self._cache.get(safe)
        if cached is not None:
            return self._payload(cached, max_chars, cached=True)
        final_url, html = await self._fetch(safe)
        # 重定向后的地址也要过闸：允许 302 跳到内网是同一类漏洞，只查入口等于没查。
        checked = await asyncio.to_thread(check_url, final_url, resolve=self._resolve)
        title, text = await asyncio.to_thread(extract_main_text, html)
        payload = {"url": checked, "title": title, "text": text}
        self._cache.put(safe, payload)
        return self._payload(payload, max_chars, cached=False)

    async def _fetch(self, url: str) -> tuple[str, str]:
        """取页面 HTML，返回 (最终 URL, HTML)。"""
        try:
            response = await self.client().get(
                url, headers={"Accept": "text/html,application/xhtml+xml"}
            )
        except httpx.HTTPError as exc:
            raise WebProviderError(f"取页面失败（{type(exc).__name__}）：{url}") from exc
        if response.status_code != 200:
            raise WebProviderError(f"取页面失败：HTTP {response.status_code}")
        content_type = (response.headers.get("content-type") or "").lower()
        if content_type and not any(
            kind in content_type for kind in ("html", "text", "xml")
        ):
            raise ValueError(f"这不是网页正文（Content-Type: {content_type}）；PDF 等二进制暂不支持")
        if len(response.content) > MAX_HTML_BYTES:
            raise ValueError(
                f"页面超过 {MAX_HTML_BYTES // (1024 * 1024)} MB，拒绝整页载入（多半不是文章页）"
            )
        return str(response.url), response.text

    @staticmethod
    def _payload(entry: dict[str, Any], max_chars: int, *, cached: bool) -> dict:
        """把缓存条目 / 新抽取结果统一成工具返回值（`url` 是引用回填的落点）。"""
        text = str(entry.get("text") or "")
        clipped = text[:max_chars]
        return {
            "url": str(entry.get("url") or ""),
            "title": str(entry.get("title") or ""),
            "text": clipped,
            "chars": len(clipped),
            "truncated": len(text) > len(clipped),
            "cached": cached,
        }


_DEFAULT_TOOLS: WebTools | None = None


async def execute(
    settings: Any, name: str, args: dict, task_id: str = "", operation_id: str = ""
) -> dict:
    """模块级执行入口：同一份 `settings` 复用同一个 `WebTools`（连接池与缓存不重建）。

    接线时更推荐直接注册实例方法（`registry.register(WEB_TOOLS, web.execute)`），
    这个包装是给"手上只有 settings"的调用方与测试用的。
    """
    global _DEFAULT_TOOLS
    if _DEFAULT_TOOLS is None or _DEFAULT_TOOLS.settings is not settings:
        _DEFAULT_TOOLS = WebTools(settings)
    return await _DEFAULT_TOOLS.execute(name, args, task_id, operation_id)
