"""MCP resources → RAG：文本入库、二进制跳过、`(uri, etag)` 增量、消失即失效。

三条硬承诺，每条都有对应用例：

1. **文本资源真的进了既有 RAG**（chunks 表 + 两路 FTS），能按 `source_type="mcp_resource"`
   检索到——不是"写了个假索引器就算完"。
2. **二进制资源跳过并记原因**（图片/PDF/blob），报告里必须能读出"为什么没索引"。
3. **增量按 `(uri, etag/mtime/size)` 判定**：没变不重建，变了先删旧块再写新块，
   从服务列表里消失的 URI 连索引一起失效。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.indexing import build_indexer as build_fts
from app.main import migrate
from app.mcp_resources import (
    ResourceIndexer,
    binary_reason,
    content_signature,
    is_textual,
    listing_signature,
    search_resources,
)
from app.retrieval import FtsRetriever
from app.storage import Chunk, Store


class RecordingIndexer:
    """只记写入了什么的假索引器（没有 `.store`，所以删旧块会返回 0）。

    用它而不是真实 `FtsIndexer`，是为了把"写了几次"变成一个可断言的数——
    增量逻辑的 bug 全部表现为"多写了一次"或"该写没写"。
    """

    def __init__(self) -> None:
        self.writes: list[list[dict]] = []

    async def write(self, chunks):
        self.writes.append(list(chunks))
        return len(chunks)


class FakeResourceClient:
    """最小 MCP 资源客户端替身。返回值用 dict（本模块的 `_shape` 认这种形状）。"""

    def __init__(self, resources, contents, templates=None) -> None:
        self._resources = list(resources)
        self._contents = dict(contents)
        self._templates = list(templates or [])
        self.calls: list[tuple] = []

    async def list_resources(self, cursor=None):
        self.calls.append(("list_resources", cursor))
        return {"resources": list(self._resources), "next_cursor": None}

    async def read_resource(self, uri, **_kwargs):
        self.calls.append(("read_resource", uri))
        return {"contents": list(self._contents.get(uri, []))}

    async def list_resource_templates(self):
        self.calls.append(("list_resource_templates",))
        return {"resource_templates": list(self._templates)}

    def method_calls(self, method: str) -> list[tuple]:
        return [call for call in self.calls if call[0] == method]


def text_resource(uri: str = "note://a", etag: str | None = "v1") -> dict:
    shape = {"uri": uri, "name": uri.rsplit("/", 1)[-1], "mime_type": "text/plain"}
    if etag is not None:
        shape["meta"] = {"etag": etag}
    return shape


def text_content(uri: str, body: str) -> list[dict]:
    return [{"uri": uri, "mime_type": "text/plain", "text": body}]


def migrated_settings(tmp_path: Path) -> Settings:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    return settings


# ── MIME 与签名（纯函数）────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("mime", "expected"),
    [
        ("text/plain", True),
        ("text/markdown; charset=utf-8", True),
        ("application/json", True),
        ("application/ld+json", True),
        ("application/vnd.api+json", True),
        ("", True),  # 未声明 → 按可读处理，读回来还有 blob 兜底
        ("image/png", False),
        ("application/pdf", False),
        ("application/octet-stream", False),
        ("audio/mpeg", False),
    ],
)
def test_is_textual(mime, expected):
    assert is_textual(mime) is expected


def test_listing_signature_combines_every_available_signal():
    """签名要把**所有**可用信号拼起来，而不是取第一个就返回。

    早先的实现是「找到 etag 就返回」，于是「etag 没变但 size 变了」这种服务端 bug
    完全看不出来——而 etag 写错恰恰是自建 MCP 服务最常见的问题。
    """
    assert listing_signature({"meta": {"etag": "abc"}}) == "etag:abc"
    assert listing_signature({"annotations": {"last_modified": "2026-01-01"}}) == (
        "last_modified:2026-01-01"
    )
    assert listing_signature({"size": 42}) == "size:42"
    # 多信号同时存在 → 全部拼进去，任何一个变了签名就变。
    combined = listing_signature({"meta": {"etag": "abc"}, "size": 42})
    assert "etag:abc" in combined and "size:42" in combined
    assert combined != listing_signature({"meta": {"etag": "abc"}, "size": 43})
    # 没给任何信号 → 只能读正文比 hash。
    assert listing_signature({}) == ""


def test_content_signature_is_stable_and_content_sensitive():
    assert content_signature("abc") == content_signature("abc")
    assert content_signature("abc") != content_signature("abd")
    assert content_signature("").startswith("sha256:")


def test_binary_reason_names_the_mime():
    assert "image/png" in binary_reason("image/png")
    assert "未声明 MIME" in binary_reason("")


# ── 文本入库（真实 chunks 表 + 真实检索）────────────────────────────────────


async def test_text_resource_lands_in_the_existing_rag(tmp_path):
    """走真实的 `FtsIndexer` + `FtsRetriever`：入库后必须能被检索到。"""
    settings = migrated_settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    indexer = ResourceIndexer(build_fts(settings, store), tmp_path / "state.json", gap=0)
    body = "欢迎使用 rich fixture。这里有一段足够长的中文正文，用来证明资源真的被切块并写进了索引表。"
    client = FakeResourceClient(
        resources=[text_resource()],
        contents={"note://a": text_content("note://a", body)},
    )
    try:
        report = await indexer.index_server("rich", client)
        assert report.indexed == ["note://a"]
        assert report.chunks >= 1

        rows = await store.rows(select(Chunk).where(Chunk.source_type == "mcp_resource"))
        assert [row.origin for row in rows] == ["note://a"]
        assert rows[0].host == "mcp:rich"
        # 资源没有任务上下文：只能靠 scope="global" 召回（语义正确，见 chunks_for_resource）
        assert rows[0].task_id == ""
        assert rows[0].conversation_id == ""
        assert "欢迎使用" in rows[0].text

        retriever = FtsRetriever(store)  # 只走 FTS 两路，不加载嵌入/重排模型
        hits = await search_resources(retriever, "欢迎使用", limit=5)
        assert "note://a" in {hit.get("origin") for hit in hits}
    finally:
        await store.engine.dispose()


async def test_binary_resource_is_skipped_with_a_reason_and_never_read(tmp_path):
    """声明是二进制的资源连读都不读（省一次往返），并且原因要能被 CLI 打出来。"""
    indexer = ResourceIndexer(RecordingIndexer(), None, gap=0)
    client = FakeResourceClient(
        resources=[{"uri": "file:///logo.png", "mime_type": "image/png"}],
        contents={"file:///logo.png": [{"uri": "file:///logo.png", "blob": "iVBORw0KGgo="}]},
    )
    report = await indexer.index_server("srv", client)

    assert report.indexed == []
    assert report.skipped and "image/png" in report.skipped[0]["reason"]
    assert report.skipped[0]["uri"] == "file:///logo.png"
    assert indexer.indexed_uris("srv") == []
    assert client.method_calls("read_resource") == []  # 没去读二进制资源


async def test_blob_payload_is_skipped_even_when_the_mime_looks_textual(tmp_path):
    """MIME 说是 text/plain、正文却是 blob：按二进制跳过，不假装索引了。"""
    indexer = ResourceIndexer(RecordingIndexer(), None, gap=0)
    client = FakeResourceClient(
        resources=[{"uri": "file:///scan", "mime_type": "text/plain"}],
        contents={"file:///scan": [{"uri": "file:///scan", "mime_type": "application/pdf", "blob": "JVBERi0="}]},
    )
    report = await indexer.index_server("srv", client)

    assert report.indexed == []
    assert report.skipped and "application/pdf" in report.skipped[0]["reason"]


async def test_empty_body_is_skipped_with_its_own_reason(tmp_path):
    """正文为空与二进制是两种不同的故障，原因必须分开写。"""
    indexer = ResourceIndexer(RecordingIndexer(), None, gap=0)
    client = FakeResourceClient(
        resources=[{"uri": "note://empty", "mime_type": "text/plain"}],
        contents={"note://empty": [{"uri": "note://empty", "mime_type": "text/plain", "text": "   "}]},
    )
    report = await indexer.index_server("srv", client)
    assert report.indexed == []
    assert report.skipped and "为空" in report.skipped[0]["reason"]


# ── 增量：没变不重建，变了重建 ──────────────────────────────────────────────


async def test_unchanged_resource_is_not_reindexed(tmp_path):
    recorder = RecordingIndexer()
    indexer = ResourceIndexer(recorder, None, gap=0)
    client = FakeResourceClient(
        resources=[text_resource(etag="v1")],
        contents={"note://a": text_content("note://a", "第一版正文，长度足够触发切块与写入。")},
    )
    first = await indexer.index_server("s", client)
    assert first.indexed == ["note://a"]
    assert len(recorder.writes) == 1
    reads_after_first = len(client.method_calls("read_resource"))

    second = await indexer.index_server("s", client)
    assert second.unchanged == ["note://a"]
    assert second.indexed == []
    assert len(recorder.writes) == 1  # 没有第二次写入
    # 列表信号命中 → 连正文都没再读（这才是增量真正的收益）
    assert len(client.method_calls("read_resource")) == reads_after_first


async def test_changed_etag_triggers_a_rebuild(tmp_path):
    recorder = RecordingIndexer()
    indexer = ResourceIndexer(recorder, None, gap=0)
    await indexer.index_server(
        "s",
        FakeResourceClient(
            resources=[text_resource(etag="v1")],
            contents={"note://a": text_content("note://a", "第一版正文，长度足够触发切块与写入。")},
        ),
    )
    report = await indexer.index_server(
        "s",
        FakeResourceClient(
            resources=[text_resource(etag="v2")],
            contents={"note://a": text_content("note://a", "第二版正文，内容换了，必须重建索引。")},
        ),
    )
    assert report.indexed == ["note://a"]
    assert len(recorder.writes) == 2


async def test_content_change_wins_over_a_lying_etag(tmp_path):
    """服务端把 etag 写错（内容变了但 etag 没变）时，正文签名兜住这种情况。"""
    recorder = RecordingIndexer()
    indexer = ResourceIndexer(recorder, None, gap=0)
    await indexer.index_server(
        "s",
        FakeResourceClient(
            resources=[text_resource(etag="same")],
            contents={"note://a": text_content("note://a", "旧内容，长度足够触发切块与写入。")},
        ),
    )
    # 同 etag，但列表里多给一个 size 信号让快路径不成立 → 会去读正文，读到新内容
    changed = text_resource(etag="same")
    changed["size"] = 999
    report = await indexer.index_server(
        "s",
        FakeResourceClient(
            resources=[changed],
            contents={"note://a": text_content("note://a", "新内容，长度足够触发切块与写入。")},
        ),
    )
    assert report.indexed == ["note://a"]
    assert len(recorder.writes) == 2


async def test_state_file_makes_incremental_checks_survive_a_restart(tmp_path):
    """增量状态落盘：进程重启后不该把所有资源重抓一遍。"""
    state_path = tmp_path / "state.json"
    client = FakeResourceClient(
        resources=[text_resource(etag="v1")],
        contents={"note://a": text_content("note://a", "正文，长度足够触发切块与写入。")},
    )
    first = ResourceIndexer(RecordingIndexer(), state_path, gap=0)
    await first.index_server("s", client)
    assert json.loads(state_path.read_text(encoding="utf-8"))["version"] == 1

    second_recorder = RecordingIndexer()
    second = ResourceIndexer(second_recorder, state_path, gap=0)
    report = await second.index_server("s", client)
    assert report.unchanged == ["note://a"]
    assert second_recorder.writes == []


async def test_reset_forces_a_full_reindex(tmp_path):
    state_path = tmp_path / "state.json"
    client = FakeResourceClient(
        resources=[text_resource(etag="v1")],
        contents={"note://a": text_content("note://a", "正文，长度足够触发切块与写入。")},
    )
    recorder = RecordingIndexer()
    indexer = ResourceIndexer(recorder, state_path, gap=0)
    await indexer.index_server("s", client)
    indexer.reset()
    assert indexer.indexed_uris("s") == []
    report = await indexer.index_server("s", client)
    assert report.indexed == ["note://a"]
    assert len(recorder.writes) == 2


# ── 失效：消失的资源连索引一起删 ────────────────────────────────────────────


async def test_a_vanished_resource_is_dropped_from_both_state_and_index(tmp_path):
    """服务不再列出某个 URI → 状态与 chunks 都必须消失（否则检索会命中一个不存在的出处）。"""
    settings = migrated_settings(tmp_path)
    store = Store(settings.data_dir / "app.sqlite")
    indexer = ResourceIndexer(build_fts(settings, store), tmp_path / "state.json", gap=0)
    both = FakeResourceClient(
        resources=[text_resource("note://a", "v1"), text_resource("note://b", "v1")],
        contents={
            "note://a": text_content("note://a", "第一条正文，长度足够触发切块与写入。"),
            "note://b": text_content("note://b", "第二条正文，长度足够触发切块与写入。"),
        },
    )
    try:
        await indexer.index_server("s", both)
        rows = await store.rows(select(Chunk).where(Chunk.source_type == "mcp_resource"))
        assert sorted(row.origin for row in rows) == ["note://a", "note://b"]

        only_a = FakeResourceClient(
            resources=[text_resource("note://a", "v1")],
            contents={"note://a": text_content("note://a", "第一条正文，长度足够触发切块与写入。")},
        )
        report = await indexer.index_server("s", only_a)
        assert report.dropped == ["note://b"]
        assert indexer.indexed_uris("s") == ["note://a"]

        rows = await store.rows(select(Chunk).where(Chunk.source_type == "mcp_resource"))
        assert {row.origin for row in rows} == {"note://a"}
    finally:
        await store.engine.dispose()


# ── templates 与检索口径 ────────────────────────────────────────────────────


async def test_templates_are_reported_but_not_indexed(tmp_path):
    """模板是带参数的 URI，没有实参读不出来——报出来，但不假装索引了。"""
    indexer = ResourceIndexer(RecordingIndexer(), None, gap=0)
    client = FakeResourceClient(
        resources=[],
        contents={},
        templates=[{"uri_template": "file:///{path}", "name": "files"}],
    )
    report = await indexer.index_server("s", client)
    assert report.indexed == []
    assert report.templates == [{"uri_template": "file:///{path}", "name": "files"}]


async def test_search_resources_pins_scope_and_source_type():
    """检索口径只在一处定义：`scope="global"` + `source_type="mcp_resource"`。"""

    class FakeRetriever:
        def __init__(self) -> None:
            self.seen: dict = {}

        async def search(self, query, **kwargs):
            self.seen = {"query": query, **kwargs}
            return []

    retriever = FakeRetriever()
    assert await search_resources(retriever, "键盘", limit=3) == []
    assert retriever.seen["query"] == "键盘"
    assert retriever.seen["scope"] == "global"
    assert retriever.seen["source_type"] == "mcp_resource"
    assert retriever.seen["top_k"] == 3


async def test_a_broken_uri_does_not_stop_the_batch(tmp_path):
    """一条坏 URI 只进 errors，同批其它资源照常入库。"""
    recorder = RecordingIndexer()
    indexer = ResourceIndexer(recorder, None, gap=0)

    class Exploding(FakeResourceClient):
        async def read_resource(self, uri, **kwargs):
            if uri == "note://bad":
                raise RuntimeError("读不出来")
            return await super().read_resource(uri, **kwargs)

    client = Exploding(
        resources=[text_resource("note://bad", "v1"), text_resource("note://ok", "v1")],
        contents={"note://ok": text_content("note://ok", "好的一条正文，长度足够触发切块与写入。")},
    )
    report = await indexer.index_server("s", client)
    assert report.indexed == ["note://ok"]
    assert report.errors and report.errors[0]["uri"] == "note://bad"
