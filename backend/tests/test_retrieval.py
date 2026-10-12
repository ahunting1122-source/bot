"""RAG 读取侧：RRF 融合、范围收窄、注入体积控制、预取门槛。"""

from app.config import Settings
from app.indexing import FtsIndexer, chunks_for_tool_result
from app.main import migrate
from app.retrieval import (
    INJECT_CHARS,
    PREFETCH_MIN_SCORE,
    best_rank_score,
    build_retriever,
    format_hits,
    rrf,
)
from app.storage import Conversation, Store, Task


def test_rrf_prefers_documents_found_by_both_paths():
    lexical = [{"row_id": 1, "text": "甲"}, {"row_id": 2, "text": "乙"}]
    chars = [{"row_id": 2, "text": "乙"}, {"row_id": 3, "text": "丙"}]
    fused = rrf([lexical, chars])
    assert fused[0]["row_id"] == 2
    assert set(fused[0]["paths"]) == {"trigram", "chars"}
    assert [hit["row_id"] for hit in fused] == [2, 1, 3]


def test_rrf_ignores_empty_paths():
    """向量路关掉时它返空列表：空的一路不贡献候选，也不影响排名。"""
    assert rrf([[], []]) == []


def test_prefetch_threshold_matches_a_top_five_hit():
    assert best_rank_score(5) >= PREFETCH_MIN_SCORE
    assert best_rank_score(6) < PREFETCH_MIN_SCORE


def test_format_hits_returns_an_honest_empty_result():
    payload = format_hits("机械键盘", "task", [])
    assert payload["hits"] == [] and payload["count"] == 0


def test_format_hits_caps_the_injected_size():
    hits = [
        {
            "text": "字" * 800,
            "origin": f"https://example.com/{index}",
            "source_type": "web_page",
            "score": 0.03,
        }
        for index in range(10)
    ]
    payload = format_hits("机械键盘", "task", hits)
    assert 0 < payload["count"] < len(hits)
    assert sum(len(hit["text"]) for hit in payload["hits"]) <= INJECT_CHARS


async def seed_chunks(store, indexer, conversation_id, task_id, origin, body):
    task = Task(id=task_id, conversation_id=conversation_id, goal="搜键盘")
    chunks = chunks_for_tool_result(
        task,
        "browser_observe",
        {"status": "ok", "data": {"url": origin, "title": "搜索", "structured": body}},
    )
    assert await indexer.write(chunks) >= 1


async def test_search_narrows_to_the_current_conversation_by_default(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    indexer = FtsIndexer(store)
    first = await store.save(Conversation())
    second = await store.save(Conversation())
    await seed_chunks(store, indexer, first.id, "t1", "https://a.example.com/1", "# 机械键盘\n甲")
    await seed_chunks(store, indexer, second.id, "t2", "https://b.example.com/2", "# 机械键盘\n乙")

    retriever = build_retriever(settings, store)
    scoped = await retriever.search("机械键盘", conversation_id=first.id, top_k=5)
    assert {hit["origin"] for hit in scoped} == {"https://a.example.com/1"}

    everything = await retriever.search(
        "机械键盘", scope="global", conversation_id=first.id, top_k=5
    )
    assert {hit["origin"] for hit in everything} == {
        "https://a.example.com/1",
        "https://b.example.com/2",
    }

    narrow = await retriever.search(
        "机械键盘", conversation_id=first.id, source_type="web_page", top_k=5
    )
    assert narrow

    assert await retriever.search("机械键盘", conversation_id=first.id, source_type="local_file") == []
    await store.engine.dispose()


async def test_short_query_is_reachable_through_the_char_path(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    indexer = FtsIndexer(store)
    conversation = await store.save(Conversation())
    await seed_chunks(
        store,
        indexer,
        conversation.id,
        "t1",
        "https://a.example.com/1",
        "# 机械键盘\n买哪种键盘好，看这篇键盘选购指南。",
    )
    retriever = build_retriever(settings, store)
    hits = await retriever.search("键盘", conversation_id=conversation.id, top_k=5)
    assert hits and "键盘" in hits[0]["text"]
    assert hits[0]["score"] >= PREFETCH_MIN_SCORE
    await store.engine.dispose()


async def test_long_natural_goal_still_retrieves_earlier_chunks(tmp_path):
    """预取用的是整句任务目标：不能整句当一个 FTS 短语，否则永远 0 召回。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    indexer = FtsIndexer(store)
    conversation = await store.save(Conversation())
    await seed_chunks(
        store,
        indexer,
        conversation.id,
        "t1",
        "https://a.example.com/1",
        "# 机械键盘\n今天看到一条消息，机械键盘涨价了。",
    )
    retriever = build_retriever(settings, store)
    hits = await retriever.search(
        "把刚才看到的机械键盘那条消息再整理一遍给我",
        scope="task",
        conversation_id=conversation.id,
        top_k=5,
    )
    assert hits, "整句目标拆窗口后应当能召回到先前的块"
    assert hits[0]["origin"] == "https://a.example.com/1"
    await store.engine.dispose()
