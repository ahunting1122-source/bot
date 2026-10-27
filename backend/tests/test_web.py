"""`web_search` / `web_extract` 的测试（W4）。

**全程不发真实网络请求**：provider 的响应解析用 `httpx.MockTransport` 把请求截在
内存里，`web_extract` 的 DNS 解析通过注入假 resolver 跳过。这是仓库既有约定——
测试不该因为本机没网就红，也不该因为本机能上网就真去抓别人的服务器。

夹具里的 JSON / HTML 都用**真实形状**（字段名、嵌套层级照 provider 实际返回抄），
否则测过的解析在线上第一次就会塌。
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.web import (
    WEB_TOOLS,
    ExtractCache,
    WebTools,
    check_url,
)
from app.web import (
    execute as web_execute,
)
from app.web_providers import (
    DEFAULT_COUNT,
    BraveProvider,
    DuckDuckGoProvider,
    SearchResult,
    SearxngProvider,
    TavilyProvider,
    WebProviderError,
    available_providers,
    select_provider,
)

#: 一个"公网"IP（example.com 的地址之一）：假 resolver 用它冒充成功解析。
PUBLIC_IP = "93.184.216.34"


@pytest.fixture(autouse=True)
def _clean_web_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """清掉宿主机可能带进来的搜索配置。

    不清的话"全都没配时只有 ddgs 可用"这条会随机器状态漂——本机开发时正好设了
    BRAVE_API_KEY，测试就会莫名其妙地红。
    """
    for name in (
        "SEARXNG_URL",
        "BRAVE_API_KEY",
        "BRAVE_SEARCH_API_KEY",
        "TAVILY_API_KEY",
        "TAVILY_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def client_for(handler) -> httpx.AsyncClient:
    """把请求截在内存里的 httpx 客户端。"""
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)


def settings_for(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path)


def public_resolver(_host: str) -> list[str]:
    return [PUBLIC_IP]


# ── provider 响应解析 ────────────────────────────────────────────────────────

SEARXNG_BODY = {
    "query": "机械键盘 轴体",
    "number_of_results": 3,
    "results": [
        {
            "url": "https://example.com/a",
            "title": "A 篇文章",
            "content": "A 的摘要",
            "engine": "google",
            "score": 1.5,
            "publishedDate": "2024-05-01T00:00:00",
        },
        {
            "url": "https://example.com/b",
            "title": "B 篇文章",
            "content": "B 的摘要",
            "engine": "bing",
            "score": 4.2,
            "publishedDate": None,
        },
        {
            "url": "https://example.com/c",
            "title": "C 篇文章",
            "content": "C 的摘要",
            "engine": "duckduckgo",
            "score": "2.0",
        },
    ],
    "infoboxes": [],
}


async def test_searxng_parses_sorts_and_caps(monkeypatch, tmp_path):
    monkeypatch.setenv("SEARXNG_URL", "http://searx.local:8080/")
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(200, json=SEARXNG_BODY)

    provider = SearxngProvider(settings_for(tmp_path), client_for(handler))
    results = await provider.search("机械键盘 轴体", count=2, freshness="week")

    assert seen["url"].startswith("http://searx.local:8080/search?")
    assert "format=json" in seen["url"]
    assert "time_range=week" in seen["url"]
    # score 降序：B(4.2) → C(2.0) → A(1.5)，截到 2 条。
    assert [item.url for item in results] == ["https://example.com/b", "https://example.com/c"]
    assert results[0].snippet == "B 的摘要"
    assert results[0].published == ""


async def test_searxng_requires_configured_url(tmp_path):
    provider = SearxngProvider(settings_for(tmp_path))
    assert provider.available() is False
    with pytest.raises(WebProviderError, match="SEARXNG_URL"):
        await provider.search("任意查询")


BRAVE_BODY = {
    "type": "search",
    "web": {
        "type": "search",
        "results": [
            {
                "title": "Brave 结果一",
                "url": "https://example.org/one",
                "description": "第一条摘要",
                "page_age": "2024-05-02T08:00:00",
                "age": "2 days ago",
            },
            {
                "title": "Brave 结果二",
                "url": "https://example.org/two",
                "description": "第二条摘要",
            },
        ],
    },
}


async def test_brave_parses_results_and_sends_key(monkeypatch, tmp_path):
    monkeypatch.setenv("BRAVE_API_KEY", "brave-test-key")
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["token"] = request.headers.get("X-Subscription-Token")
        return httpx.Response(200, json=BRAVE_BODY)

    provider = BraveProvider(settings_for(tmp_path), client_for(handler))
    results = await provider.search("orbit agent", count=5, freshness="month")

    assert seen["token"] == "brave-test-key"
    assert seen["url"] is not None
    assert "count=5" in seen["url"]
    assert "freshness=pm" in seen["url"]  # 月度在 Brave 侧是 pm
    assert [item.title for item in results] == ["Brave 结果一", "Brave 结果二"]
    assert results[0].published == "2024-05-02T08:00:00"
    assert results[1].published == ""


TAVILY_BODY = {
    "query": "orbit",
    "results": [
        {
            "title": "Tavily 结果",
            "url": "https://example.net/t",
            "content": "Tavily 摘要",
            "score": 0.93,
            "published_date": "2024-05-03",
        }
    ],
    "response_time": 0.42,
}


async def test_tavily_posts_payload_with_key(monkeypatch, tmp_path):
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test")
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=TAVILY_BODY)

    provider = TavilyProvider(settings_for(tmp_path), client_for(handler))
    results = await provider.search("orbit", count=3, freshness="year")

    body = seen["body"]
    assert seen["url"] == "https://api.tavily.com/search"
    assert isinstance(body, dict)
    # Tavily 的 key 在请求体里，不是 Authorization 头（照 Hermes 的契约）。
    assert body["api_key"] == "tvly-test"
    assert body["query"] == "orbit"
    assert body["max_results"] == 3
    assert body["time_range"] == "year"
    assert results[0].snippet == "Tavily 摘要"
    assert results[0].published == "2024-05-03"


DDG_HTML = """<!DOCTYPE html>
<html><body>
<div class="serp__results">
  <div class="result results_links results_links_deep web-result">
    <div class="links_main links_deep result__body">
      <h2 class="result__title">
        <a rel="nofollow" class="result__a"
           href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage%3Fx%3D1&amp;rut=9f1">示例<b>标题</b>一</a>
      </h2>
      <a class="result__snippet"
         href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage%3Fx%3D1">摘要<b>文本</b>一</a>
      <span class="result__timestamp">2024-04-01</span>
    </div>
  </div>
  <div class="result results_links web-result result--ad">
    <div class="links_main result__body">
      <h2 class="result__title">
        <a class="result__a" href="https://duckduckgo.com/y.js?ad_domain=ads.example">广告位</a>
      </h2>
    </div>
  </div>
  <div class="result results_links web-result">
    <div class="links_main result__body">
      <h2 class="result__title"><a class="result__a" href="https://example.com/second">第二条</a></h2>
      <a class="result__snippet">第二条摘要</a>
    </div>
  </div>
</div>
</body></html>"""


async def test_ddgs_parses_html_results(tmp_path):
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        return httpx.Response(
            200, text=DDG_HTML, headers={"content-type": "text/html; charset=utf-8"}
        )

    provider = DuckDuckGoProvider(settings_for(tmp_path), client_for(handler))
    results = await provider.search("示例查询", count=5, freshness="day")

    assert "html.duckduckgo.com" in seen["url"]
    assert "df=d" in seen["url"]
    # 跳转壳被解开；广告位（y.js）被丢掉。
    assert [item.url for item in results] == [
        "https://example.com/page?x=1",
        "https://example.com/second",
    ]
    assert results[0].title == "示例标题一"
    assert results[0].snippet == "摘要文本一"
    assert results[0].published == "2024-04-01"
    assert results[1].published == ""


async def test_ddgs_caps_result_count(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=DDG_HTML)

    provider = DuckDuckGoProvider(settings_for(tmp_path), client_for(handler))
    results = await provider.search("示例查询", count=1)
    assert len(results) == 1


# ── provider 选择 ────────────────────────────────────────────────────────────


def test_select_provider_falls_back_to_ddgs(tmp_path):
    settings = settings_for(tmp_path)
    assert select_provider(settings).name == "ddgs"
    status = {item["name"]: item for item in available_providers(settings)}
    assert set(status) == {"searxng", "brave", "tavily", "ddgs"}
    assert status["ddgs"]["available"] is True
    assert status["ddgs"]["requires_key"] is False
    assert status["brave"]["available"] is False
    assert status["brave"]["requires_key"] is True
    assert status["tavily"]["available"] is False
    assert status["searxng"]["available"] is False


def test_select_provider_prefers_configured(monkeypatch, tmp_path):
    settings = settings_for(tmp_path)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-x")
    assert select_provider(settings).name == "tavily"
    monkeypatch.setenv("BRAVE_API_KEY", "brave-x")
    assert select_provider(settings).name == "brave"
    monkeypatch.setenv("SEARXNG_URL", "http://searx.local:8080")
    assert select_provider(settings).name == "searxng"


# ── web_search 参数校验 ──────────────────────────────────────────────────────


def tool_schema(name: str):
    return next(item for item in WEB_TOOLS if item.name == name).schema


def test_web_tools_registered():
    assert [item.name for item in WEB_TOOLS] == ["web_search", "web_extract"]
    assert all(item.schema is not None for item in WEB_TOOLS)


def test_web_search_rejects_empty_query():
    schema = tool_schema("web_search")
    assert schema is not None
    with pytest.raises(ValidationError):
        schema.model_validate({"query": ""})


def test_web_search_caps_count():
    schema = tool_schema("web_search")
    assert schema is not None
    with pytest.raises(ValidationError):
        schema.model_validate({"query": "机械键盘", "count": 11})
    with pytest.raises(ValidationError):
        schema.model_validate({"query": "机械键盘", "count": 0})
    assert schema.model_validate({"query": "机械键盘"}).model_dump()["count"] == DEFAULT_COUNT
    assert schema.model_validate({"query": "机械键盘", "count": 10}).model_dump()["count"] == 10


async def test_web_search_rejects_unknown_freshness(tmp_path):
    tools = WebTools(settings_for(tmp_path))
    with pytest.raises(ValueError, match="freshness"):
        await tools.execute("web_search", {"query": "机械键盘", "freshness": "decade"})


# ── web_extract 的 URL 安全闸 ────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:text/html,<h1>hi</h1>",
        "javascript:alert(document.cookie)",
        "http://127.0.0.1/",
        "http://127.0.0.1:6185/api/tasks",
        "http://169.254.169.254/latest/meta-data/",
        "http://localhost:8000/",
        "http://10.0.0.5/internal",
        "http://192.168.1.1/",
        "http://[::1]/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://example.com@127.0.0.1/",
        "ftp://example.com/file.txt",
    ],
)
def test_check_url_rejects(url):
    with pytest.raises(ValueError):
        check_url(url, resolve=public_resolver)


def test_check_url_rejects_hostname_resolving_to_private_ip():
    with pytest.raises(ValueError, match="私有网段"):
        check_url("http://internal.example/", resolve=lambda _host: ["10.1.2.3"])


def test_check_url_rejects_when_dns_fails():
    def broken(_host: str) -> list[str]:
        raise ValueError("域名解析失败，已按安全策略拒绝：broken.example")

    with pytest.raises(ValueError):
        check_url("http://broken.example/", resolve=broken)


def test_check_url_allows_public_page():
    assert check_url("https://example.com/a?b=1", resolve=public_resolver) == (
        "https://example.com/a?b=1"
    )


async def test_web_extract_execute_rejects_unsafe_url(tmp_path):
    tools = WebTools(settings_for(tmp_path), resolve=public_resolver)
    with pytest.raises(ValueError):
        await tools.execute("web_extract", {"url": "file:///etc/passwd"})
    with pytest.raises(ValueError):
        await tools.execute("web_extract", {"url": "http://127.0.0.1/admin"})


# ── web_extract 正文抽取 / 截断 / 缓存 ──────────────────────────────────────

PAGE_HTML = """<!DOCTYPE html>
<html><head><title>示例文章标题</title>
<style>.x { color: red }</style>
<script>window.__DATA__ = {"secret": 1};</script>
</head><body>
<nav><a href="/">首页</a></nav>
<article>
  <h1>机械键盘轴体怎么选</h1>
  <p>段落轴适合长时间打字，段落感明确，长时间输入不容易累。</p>
  <p>线性轴触发快，适合游戏；声音上比段落轴更闷一些。</p>
  <p>青轴段落感最强，声音也最大，合租或者开放工位要留意同事的感受。</p>
  <p>红轴是线性轴里最常见的默认选择，手感偏轻，长时间使用不容易疲劳。</p>
  <p>预算有限时先买一把热插拔套件，轴体可以之后按手感慢慢换。</p>
  <h2>结论</h2>
  <ul><li>办公打字优先段落轴</li><li>游戏优先线性轴</li></ul>
  <table>
    <tr><th>轴体</th><th>手感</th></tr>
    <tr><td>段落轴</td><td>有段落感</td></tr>
  </table>
</article>
<footer>版权所有</footer>
</body></html>"""


def page_client(calls: list[str]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(
            200, text=PAGE_HTML, headers={"content-type": "text/html; charset=utf-8"}
        )

    return client_for(handler)


async def test_web_extract_extracts_body_and_drops_noise(tmp_path):
    calls: list[str] = []
    tools = WebTools(
        settings_for(tmp_path), client=page_client(calls), resolve=public_resolver
    )
    result = await tools.execute("web_extract", {"url": "https://example.com/post"})

    assert result["status"] == "ok"
    data = result["data"]
    assert data["url"] == "https://example.com/post"
    assert data["title"] == "示例文章标题"
    assert data["cached"] is False
    assert data["truncated"] is False
    # 正文按 Markdown 风格归一：标题、段落、列表、表格行都在。
    assert "机械键盘轴体怎么选" in data["text"]
    assert "段落轴适合长时间打字" in data["text"]
    assert "- 办公打字优先段落轴" in data["text"]
    assert "| 轴体 | 手感 |" in data["text"]
    # 噪声被跳过：脚本、样式、页脚、导航都不该进正文。
    assert "secret" not in data["text"]
    assert "color: red" not in data["text"]
    assert "版权所有" not in data["text"]
    assert "首页" not in data["text"]
    assert calls == ["https://example.com/post"]


async def test_web_extract_truncates_to_max_chars(tmp_path):
    tools = WebTools(
        settings_for(tmp_path), client=page_client([]), resolve=public_resolver
    )
    data = (await tools.execute("web_extract", {"url": "https://example.com/post", "max_chars": 200}))[
        "data"
    ]
    assert data["truncated"] is True
    assert data["chars"] == 200
    assert len(data["text"]) == 200


async def test_web_extract_cache_hit_skips_second_fetch(tmp_path):
    calls: list[str] = []
    tools = WebTools(
        settings_for(tmp_path), client=page_client(calls), resolve=public_resolver
    )
    first = await tools.execute("web_extract", {"url": "https://example.com/post"})
    second = await tools.execute("web_extract", {"url": "https://example.com/post"})

    assert first["data"]["cached"] is False
    assert second["data"]["cached"] is True
    assert second["data"]["text"] == first["data"]["text"]
    assert len(calls) == 1  # 只真的取了一次页面


async def test_web_extract_cache_survives_new_instance(tmp_path):
    """落盘层：新实例（模拟重启）仍能命中，不再发请求。"""
    settings = settings_for(tmp_path)
    await WebTools(settings, client=page_client([]), resolve=public_resolver).execute(
        "web_extract", {"url": "https://example.com/post"}
    )
    calls: list[str] = []
    data = (
        await WebTools(settings, client=page_client(calls), resolve=public_resolver).execute(
            "web_extract", {"url": "https://example.com/post"}
        )
    )["data"]
    assert data["cached"] is True
    assert calls == []


def test_extract_cache_expires(tmp_path):
    cache = ExtractCache(tmp_path / "cache" / "web", ttl=0.0)
    cache.put("https://example.com/post", {"url": "https://example.com/post", "text": "正文"})
    assert cache.get("https://example.com/post") is None


async def test_web_extract_rejects_redirect_to_internal(tmp_path):
    """302 跳到内网与直接访问内网是同一类漏洞：重定向后的地址也要过闸。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/admin"})
        return httpx.Response(
            200, text=PAGE_HTML, headers={"content-type": "text/html; charset=utf-8"}
        )

    tools = WebTools(
        settings_for(tmp_path), client=client_for(handler), resolve=public_resolver
    )
    with pytest.raises(ValueError):
        await tools.execute("web_extract", {"url": "https://public.example/post"})


async def test_web_extract_returns_final_url_after_redirect(tmp_path):
    """重定向后必须回填**最终** URL：引用指向跳转页就等于没有出处。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "public.example":
            return httpx.Response(302, headers={"location": "https://final.example/post"})
        return httpx.Response(
            200, text=PAGE_HTML, headers={"content-type": "text/html; charset=utf-8"}
        )

    tools = WebTools(
        settings_for(tmp_path), client=client_for(handler), resolve=public_resolver
    )
    data = (await tools.execute("web_extract", {"url": "https://public.example/post"}))["data"]
    assert data["url"] == "https://final.example/post"
    assert "机械键盘轴体怎么选" in data["text"]


# ── web_search 归一与回退 ────────────────────────────────────────────────────


class FakeProvider:
    """最小 `SearchProvider` 实现：只验证 `WebTools.search` 的归一与回退。"""

    requires_key = False

    def __init__(self, name: str, results=None, error: str = "") -> None:
        self.name = name
        self.results = results or []
        self.error = error
        self.calls: list[dict] = []

    def available(self, settings=None) -> bool:
        return True

    async def search(self, query: str, *, count: int = DEFAULT_COUNT, freshness=None):
        self.calls.append({"query": query, "count": count, "freshness": freshness})
        if self.error:
            raise WebProviderError(self.error)
        return self.results[:count]


async def test_web_search_returns_urls_for_citation(tmp_path):
    provider = FakeProvider(
        "fake",
        [
            SearchResult(
                title="结果一", url="https://example.com/1", snippet="摘要一", published="2024-05-01"
            ),
            SearchResult(title="结果二", url="https://example.com/2", snippet="摘要二"),
        ],
    )
    tools = WebTools(settings_for(tmp_path), providers=[provider])
    data = (await tools.execute("web_search", {"query": "机械键盘", "count": 2}))["data"]

    assert data["provider"] == "fake"
    assert data["count"] == 2
    # 引用回填是硬要求：每条结果都必须带 url。
    assert [item["url"] for item in data["results"]] == [
        "https://example.com/1",
        "https://example.com/2",
    ]
    assert data["results"][0]["snippet"] == "摘要一"
    assert data["results"][0]["published"] == "2024-05-01"
    assert provider.calls[0]["freshness"] is None


async def test_web_search_passes_freshness(tmp_path):
    provider = FakeProvider("fake", [SearchResult(title="t", url="https://example.com/")])
    tools = WebTools(settings_for(tmp_path), providers=[provider])
    await tools.execute("web_search", {"query": "机械键盘", "freshness": "week"})
    assert provider.calls[0]["freshness"] == "week"


async def test_web_search_falls_back_to_next_provider(tmp_path):
    broken = FakeProvider("broken", error="限流")
    healthy = FakeProvider("healthy", [SearchResult(title="t", url="https://example.com/ok")])
    tools = WebTools(settings_for(tmp_path), providers=[broken, healthy])
    data = (await tools.execute("web_search", {"query": "机械键盘"}))["data"]

    assert data["provider"] == "healthy"
    assert data["results"][0]["url"] == "https://example.com/ok"
    assert len(broken.calls) == 1


async def test_web_search_reports_all_providers_failed(tmp_path):
    tools = WebTools(
        settings_for(tmp_path),
        providers=[FakeProvider("a", error="超时"), FakeProvider("b", error="HTTP 429")],
    )
    with pytest.raises(WebProviderError, match="所有搜索 provider 都失败了"):
        await tools.execute("web_search", {"query": "机械键盘"})


async def test_module_level_execute_wrapper(tmp_path):
    """模块级 `execute` 是给「手上只有 settings」的调用方用的：未知工具名要明确报错。"""
    with pytest.raises(ValueError, match="未知工具"):
        await web_execute(settings_for(tmp_path), "web_fetch", {})
