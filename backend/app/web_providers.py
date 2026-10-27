"""网页搜索 provider 抽象层（对标 Hermes `plugins/web/`）。

为什么要单独一层：搜索后端会换（自建 SearXNG / Brave / Tavily / 无 key 的
DuckDuckGo），但 `web_search` 的**结果契约**不能跟着变——上层只认
`SearchResult(title, url, snippet, published)`，其中 `url` 是 Orbit 引用回填的硬
要求（`app/indexing.py::origin_of` 只认 url / path 两种出处，没有 url 的结果等于
不可引用）。

四个 provider 的请求与响应归一化**照抄 Hermes 的 `plugins/web/*/provider.py`**：
同一个端点、同一组参数名、同一种 `{title, url, description, position}` 中间形状。
对齐契约不是为了好看，而是出问题时能直接拿 Hermes 的源码当基准比对，也能把
两边的搜索结果放在一起看差在哪。

依赖边界：只用 `httpx`（已在依赖里）+ 标准库。**不 import `ddgs` 包**——本机没有
网络，装不了它；DuckDuckGo 走静态 HTML 端点自己解析。
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qs, unquote, urlparse

import httpx

log = logging.getLogger("orbit.web")

#: 单次搜索的默认与上限条数。上限 10 与 `web_search` 工具的 schema 一致：
#: 再多也不会让模型答得更准，只会挤掉上下文。
DEFAULT_COUNT = 5
MAX_COUNT = 10

#: 时间范围的统一取值。各家 provider 的参数名与编码都不同（SearXNG/Brave 用
#: day|week|month|year 与 pd|pw|pm|py，DuckDuckGo 用 d|w|m|y，Tavily 直接收单词），
#: 所以入口只认这 4 个词，转换在各自的 provider 里做。
FRESHNESS_TOKENS = ("day", "week", "month", "year")

#: 各 provider 的 User-Agent。DuckDuckGo 的 HTML 端点会拒绝 httpx 默认 UA（直接回
#: 人机校验页），所以统一带一个普通浏览器 UA。
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class WebProviderError(RuntimeError):
    """provider 层面的失败：网络不通、HTTP 非 200、响应不可解析。

    单独一类而不是 `ValueError`，是因为两者对模型意味着不同的下一步：
    `ValueError` = 参数或输入不合格（改参数重试），`WebProviderError` = 外部服务
    没给到结果（换 provider 或稍后重试，改参数没用）。
    """


def _config(settings: Any, attr: str, env: str) -> str:
    """读一项联网配置：先看 `Settings` 字段，再退回同名环境变量。

    为什么两段读：本轮 `app/config.py` 不在可改范围内，`Settings` 上还没有
    `searxng_url` / `brave_search_api_key` / `tavily_api_key` 这些字段；而 Hermes 的
    契约本来就是**环境变量名**。等下一波把字段补进 `Settings`（`ORBIT_` 前缀）时，
    `getattr` 那一段会自动生效，provider 与测试都不用改。
    """
    if settings is not None:
        value = getattr(settings, attr, "")
        if hasattr(value, "get_secret_value"):  # SecretStr
            value = value.get_secret_value()
        if isinstance(value, str) and value.strip():
            return value.strip()
    return (os.environ.get(env) or "").strip()


def normalize_freshness(value: str | None) -> str:
    """把 `freshness` 收敛到 4 个合法词之一；空值表示不限时间，非法值报错。

    为什么非法值要报错而不是静默忽略：静默忽略会让模型以为"限了最近一周"，
    实际拿到的是全时段结果，然后据此下"最近没有相关消息"的错误结论。
    """
    token = (value or "").strip().lower()
    if not token:
        return ""
    if token not in FRESHNESS_TOKENS:
        raise ValueError(
            f"freshness 只能是 {' / '.join(FRESHNESS_TOKENS)} 之一，或留空表示不限时间（收到 {value}）"
        )
    return token


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class SearchResult:
    """一条搜索结果。

    字段与 Hermes 的 `{title, url, description, position}` 一一对应，只是把
    `description` 叫 `snippet`（Orbit 侧一直这么叫），并补了 `published`。
    """

    title: str
    url: str
    snippet: str = ""
    published: str = ""


@runtime_checkable
class SearchProvider(Protocol):
    """一个搜索后端的契约。

    `search` 是协程：httpx 走异步，别在事件循环里同步阻塞。
    `available` 只判配置是否齐，**不发网络请求**——它会在工具注册与设置页上被反复
    调用，一次探活就能把启动拖成秒级。
    """

    name: str
    requires_key: bool

    def available(self, settings: Any = None) -> bool: ...

    async def search(
        self, query: str, *, count: int = DEFAULT_COUNT, freshness: str | None = None
    ) -> list[SearchResult]: ...


class HttpSearchProvider:
    """provider 公共件：可注入的 httpx 客户端 + 统一失败语义。

    客户端可注入是为了让测试用 `httpx.MockTransport` 把请求截在内存里（仓库规则：
    测试不发真实网络请求），生产路径则是每个 `WebTools` 实例共享一个客户端，
    避免每次工具调用都重建连接池。
    """

    name = ""
    requires_key = True

    def __init__(
        self,
        settings: Any = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.settings = settings
        self._client = client
        self._timeout = timeout

    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def available(self, settings: Any = None) -> bool:
        raise NotImplementedError

    async def search(
        self, query: str, *, count: int = DEFAULT_COUNT, freshness: str | None = None
    ) -> list[SearchResult]:
        # 子类必须实现；基类留桩是为了让 mypy 能确认它满足 `SearchProvider` 协议。
        raise NotImplementedError

    async def _get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """GET 一个 JSON 接口，失败统一抛 `WebProviderError`。"""
        try:
            response = await self.client().get(
                url, params=params, headers=headers or {"Accept": "application/json"}
            )
        except httpx.HTTPError as exc:
            raise WebProviderError(f"{self.name} 连接失败（{type(exc).__name__}）") from exc
        if response.status_code != 200:
            raise WebProviderError(f"{self.name} 返回 HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise WebProviderError(f"{self.name} 返回的不是 JSON") from exc
        if not isinstance(payload, dict):
            raise WebProviderError(f"{self.name} 返回的 JSON 不是对象")
        return payload


class SearxngProvider(HttpSearchProvider):
    """自建 SearXNG 实例（`SEARXNG_URL`，`GET /search?q=&format=json`）。

    不需要 API key，所以 `requires_key = False`；但需要配置实例地址，否则不可用。
    """

    name = "searxng"
    requires_key = False

    def base_url(self, settings: Any = None) -> str:
        return _config(settings if settings is not None else self.settings, "searxng_url", "SEARXNG_URL").rstrip("/")

    def available(self, settings: Any = None) -> bool:
        return bool(self.base_url(settings))

    async def search(
        self, query: str, *, count: int = DEFAULT_COUNT, freshness: str | None = None
    ) -> list[SearchResult]:
        base = self.base_url()
        if not base:
            raise WebProviderError("SearXNG 未配置：请把 SEARXNG_URL 指向自建实例")
        params: dict[str, Any] = {"q": query, "format": "json", "pageno": 1}
        token = normalize_freshness(freshness)
        if token:
            params["time_range"] = token
        payload = await self._get_json(f"{base}/search", params=params)
        raw = payload.get("results") or []
        if not isinstance(raw, list):
            raise WebProviderError("SearXNG 返回的 results 不是数组")
        # SearXNG 会把上游引擎的相关性分数原样透出：先按 score 降序再截断。
        # 不排序取到的是"引擎返回顺序"，聚合多引擎的意义就没了。
        ordered = sorted(raw, key=lambda item: _as_float(item.get("score")), reverse=True)[:count]
        return [
            SearchResult(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                snippet=str(item.get("content") or "").strip(),
                published=str(item.get("publishedDate") or "").strip(),
            )
            for item in ordered
            if isinstance(item, dict) and item.get("url")
        ]


class BraveProvider(HttpSearchProvider):
    """Brave Search（免费档 2000 次/月，`GET api.search.brave.com`）。

    契约照 Hermes `brave_free/provider.py`：`X-Subscription-Token` 头 + `count`
    参数（Brave 自己上限 20），响应取 `web.results`。
    """

    name = "brave"
    requires_key = True
    ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

    def api_key(self, settings: Any = None) -> str:
        # 两个环境变量都认：Hermes 用 BRAVE_SEARCH_API_KEY，Orbit 侧的习惯写法是
        # BRAVE_API_KEY。少一次"明明配了却不生效"的排查。
        target = settings if settings is not None else self.settings
        return _config(target, "brave_search_api_key", "BRAVE_SEARCH_API_KEY") or _config(
            target, "brave_api_key", "BRAVE_API_KEY"
        )

    def available(self, settings: Any = None) -> bool:
        return bool(self.api_key(settings))

    async def search(
        self, query: str, *, count: int = DEFAULT_COUNT, freshness: str | None = None
    ) -> list[SearchResult]:
        key = self.api_key()
        if not key:
            raise WebProviderError("Brave 未配置：请设置 BRAVE_API_KEY（或 BRAVE_SEARCH_API_KEY）")
        params: dict[str, Any] = {"q": query, "count": max(1, min(count, 20))}
        token = normalize_freshness(freshness)
        if token:
            params["freshness"] = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}[token]
        payload = await self._get_json(
            self.ENDPOINT,
            params=params,
            headers={"X-Subscription-Token": key, "Accept": "application/json"},
        )
        web = payload.get("web") or {}
        raw = web.get("results") if isinstance(web, dict) else None
        if not isinstance(raw, list):
            return []
        return [
            SearchResult(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                snippet=str(item.get("description") or "").strip(),
                published=str(item.get("page_age") or item.get("age") or "").strip(),
            )
            for item in raw[:count]
            if isinstance(item, dict) and item.get("url")
        ]


class TavilyProvider(HttpSearchProvider):
    """Tavily（`POST api.tavily.com/search`，api_key 放在 body 里）。

    契约照 Hermes `tavily/provider.py::_tavily_request`：Tavily 的 key 走请求体而
    不是 Authorization 头，写成 Bearer 会被判 401。
    """

    name = "tavily"
    requires_key = True
    ENDPOINT = "https://api.tavily.com"

    def api_key(self, settings: Any = None) -> str:
        return _config(settings if settings is not None else self.settings, "tavily_api_key", "TAVILY_API_KEY")

    def base_url(self, settings: Any = None) -> str:
        target = settings if settings is not None else self.settings
        return (_config(target, "tavily_base_url", "TAVILY_BASE_URL") or self.ENDPOINT).rstrip("/")

    def available(self, settings: Any = None) -> bool:
        return bool(self.api_key(settings))

    async def search(
        self, query: str, *, count: int = DEFAULT_COUNT, freshness: str | None = None
    ) -> list[SearchResult]:
        key = self.api_key()
        if not key:
            raise WebProviderError("Tavily 未配置：请设置 TAVILY_API_KEY")
        payload: dict[str, Any] = {
            "query": query,
            "max_results": max(1, min(count, 20)),
            "include_raw_content": False,
            "include_images": False,
            "api_key": key,
        }
        token = normalize_freshness(freshness)
        if token:
            payload["time_range"] = token
        try:
            response = await self.client().post(f"{self.base_url()}/search", json=payload)
        except httpx.HTTPError as exc:
            raise WebProviderError(f"tavily 连接失败（{type(exc).__name__}）") from exc
        if response.status_code != 200:
            raise WebProviderError(f"tavily 返回 HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise WebProviderError("tavily 返回的不是 JSON") from exc
        raw = body.get("results") if isinstance(body, dict) else None
        if not isinstance(raw, list):
            return []
        return [
            SearchResult(
                title=str(item.get("title") or "").strip(),
                url=str(item.get("url") or "").strip(),
                snippet=str(item.get("content") or "").strip(),
                published=str(item.get("published_date") or "").strip(),
            )
            for item in raw[:count]
            if isinstance(item, dict) and item.get("url")
        ]


def _unwrap_redirect(href: str) -> str:
    """把 DuckDuckGo 的跳转壳解成真实地址。

    HTML 端点给的 href 形如 `/l/?uddg=<percent-encoded>&rut=...`。不解开就会把
    `duckduckgo.com/l/?uddg=...` 当成来源 URL 回填进引用，用户点开看到的是跳转页
    而不是原文——引用回填是硬要求，这里必须给真地址。
    """
    href = (href or "").strip()
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.path.startswith("/l/"):
        targets = parse_qs(parsed.query).get("uddg") or []
        if targets:
            return unquote(targets[0]).strip()
    return href


class _DuckDuckGoParser(HTMLParser):
    """DuckDuckGo HTML 结果页 → 结果列表。

    页面结构（`html.duckduckgo.com` 当前版本）：

        <a class="result__a" href="//duckduckgo.com/l/?uddg=...">标题</a>
        <a class="result__snippet" href="...">摘要</a>
        <span class="result__timestamp">2024-04-01</span>

    摘要与时间戳排在标题**之后**，所以一条结果要等下一个 `result__a`（或收尾）才
    落定——见 `finish()`。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[dict[str, str]] = []
        self._capture = ""
        self._depth = 0
        self._buffer: list[str] = []
        self._current: dict[str, str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._capture:
            # 采集中的元素内部还有标签（`<b>` 高亮等）：只记层级，不重新开采集。
            self._depth += 1
            return
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            self._finish()
            self._current = {
                "title": "",
                "url": _unwrap_redirect(attributes.get("href") or ""),
                "snippet": "",
                "published": "",
            }
            self._start("title")
            return
        if "result__snippet" in classes:
            self._start("snippet")
            return
        if "result__timestamp" in classes:
            self._start("timestamp")

    def handle_endtag(self, tag: str) -> None:
        if not self._capture:
            return
        if self._depth > 0:
            self._depth -= 1
            return
        text = re.sub(r"\s+", " ", "".join(self._buffer)).strip()
        if self._current is not None:
            key = {"title": "title", "snippet": "snippet", "timestamp": "published"}[self._capture]
            self._current[key] = text
        self._capture = ""
        self._buffer = []

    def handle_data(self, data: str) -> None:
        if self._capture:
            self._buffer.append(data)

    def finish(self) -> list[dict[str, str]]:
        """收尾：把最后一条未落定的结果补进来。"""
        self._finish()
        return self.results

    def _start(self, capture: str) -> None:
        self._capture = capture
        self._depth = 0
        self._buffer = []

    def _finish(self) -> None:
        current, self._current = self._current, None
        if current is None:
            return
        url = current["url"]
        # 没有可回填的出处就不收：广告位（y.js 跳转）与站内链接都过不了这道。
        if not url.startswith(("http://", "https://")) or not current["title"]:
            return
        if "duckduckgo.com/y.js" in url or "duckduckgo.com/l/" in url:
            return
        self.results.append(
            {
                "title": current["title"],
                "url": url,
                "snippet": current["snippet"],
                "published": current["published"],
            }
        )


class DuckDuckGoProvider(HttpSearchProvider):
    """无 key 的 DuckDuckGo（抓 `html.duckduckgo.com` 的静态结果页）。

    为什么不照抄 Hermes 的实现：Hermes 的 `ddgs` provider 依赖 `ddgs` 包（多引擎 +
    primp），本机无网络装不了。这里只取 DuckDuckGo 自己的 HTML 端点，用标准库
    `html.parser` 解析，但**结果字段照 Hermes 归一**，上层不用区分来源。
    """

    name = "ddgs"
    requires_key = False
    ENDPOINT = "https://html.duckduckgo.com/html/"

    def available(self, settings: Any = None) -> bool:
        # 无需 key、无需配置：它是"全都没配"时的兜底，所以恒为可用。
        # 真正的失败（限流、断网）发生在 search()，错误信息会说明是哪一步。
        return True

    async def search(
        self, query: str, *, count: int = DEFAULT_COUNT, freshness: str | None = None
    ) -> list[SearchResult]:
        params: dict[str, Any] = {"q": query}
        token = normalize_freshness(freshness)
        if token:
            params["df"] = {"day": "d", "week": "w", "month": "m", "year": "y"}[token]
        try:
            response = await self.client().get(
                self.ENDPOINT, params=params, headers={"Accept": "text/html"}
            )
        except httpx.HTTPError as exc:
            raise WebProviderError(f"ddgs 连接失败（{type(exc).__name__}）") from exc
        if response.status_code != 200:
            raise WebProviderError(f"ddgs 返回 HTTP {response.status_code}")
        parser = _DuckDuckGoParser()
        parser.feed(response.text)
        parser.close()
        results = parser.finish()[:count]
        if not results and "anomaly" in response.text.casefold():
            raise WebProviderError(
                "DuckDuckGo 判定为异常流量（人机校验），请稍后重试或配置其它搜索 provider"
            )
        return [
            SearchResult(
                title=item["title"],
                url=item["url"],
                snippet=item["snippet"],
                published=item["published"],
            )
            for item in results
        ]


#: provider 的固定优先级。顺序的理由：
#: 1) searxng —— 自建实例，没有配额上限、也不把查询词交给第三方，配了就该先用；
#: 2) brave / tavily —— 有 key 的商业 API，按免费配额从小到大排；
#: 3) ddgs —— 无 key 兜底，最容易被 DuckDuckGo 限流，所以永远排最后。
PROVIDER_ORDER = ("searxng", "brave", "tavily", "ddgs")


def build_providers(
    settings: Any, client: httpx.AsyncClient | None = None
) -> list[SearchProvider]:
    """按 `PROVIDER_ORDER` 构造全部 provider（共享同一个 httpx 客户端）。"""
    factories: dict[str, type[HttpSearchProvider]] = {
        "searxng": SearxngProvider,
        "brave": BraveProvider,
        "tavily": TavilyProvider,
        "ddgs": DuckDuckGoProvider,
    }
    providers: list[SearchProvider] = []
    for name in PROVIDER_ORDER:
        providers.append(factories[name](settings, client))
    return providers


def available_providers(
    settings: Any, client: httpx.AsyncClient | None = None
) -> list[dict[str, Any]]:
    """列出全部 provider 与可用状态（设置页/CLI 用它回答"现在会走谁"）。"""
    return [
        {
            "name": provider.name,
            "requires_key": provider.requires_key,
            "available": provider.available(settings),
        }
        for provider in build_providers(settings, client)
    ]


def select_provider(settings: Any, client: httpx.AsyncClient | None = None) -> SearchProvider:
    """选默认 provider：已配置的优先，全都没配就是 ddgs。

    `DuckDuckGoProvider.available()` 恒为 True，所以这里不会"选不出来"；真的没网
    时失败发生在 `search()`，错误信息会指出是哪一家、哪一步。
    """
    for provider in build_providers(settings, client):
        if provider.available(settings):
            return provider
    raise WebProviderError("没有任何可用的搜索 provider")
