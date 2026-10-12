"""RAG 写入侧：切块规则、双路 FTS、去重。

关键回归：trigram 对 2 字中文查询返回 0 条（'键盘' 命中不了），字符级那一路必须补上。
"""

from app.config import Settings
from app.indexing import (
    FtsIndexer,
    chunk_text,
    chunks_for_tool_result,
    match_expression,
    readable_text,
    shingle_similarity,
    split_blocks,
    split_chars,
)
from app.main import migrate
from app.storage import Conversation, Store, Task


def test_split_chars_keeps_latin_words_whole():
    assert split_chars("机械键盘 B站") == "机 械 键 盘 B 站"


def test_blocks_carry_the_heading_path():
    blocks = split_blocks("# 搜索结果\n\n第一段正文。\n\n## 分页\n\n第二段正文。")
    assert [block.title_path for block in blocks] == ["搜索结果", "搜索结果 > 分页"]


def test_heading_only_pages_keep_their_headings():
    """SPA 结果列表页每个条目就是一个 `### 标题`，标题之间根本没有正文。

    只把标题当 title_path 用掉的话，整页一块都落不下来——实测 B 站搜索页
    2238 字只落成 34 字导航残渣，检索必然 0 召回。
    """
    titles = [
        f"### 【2026年9月键盘推荐】第{i}期 千元内性价比键盘自选实测" for i in range(6)
    ]
    chunks = chunk_text("VCT\n\n登录后你可以:\n\n" + "\n\n".join(titles))
    indexed = "\n".join(chunk["text"] for chunk in chunks)
    for index in range(6):
        assert f"第{index}期" in indexed
    # 标题自己成了内容：索引里不再只剩那 34 字导航残渣
    assert sum(len(chunk["text"]) for chunk in chunks) > 150


def test_chunk_text_prefixes_the_heading_path():
    chunks = chunk_text("# 机械键盘\n\n" + "买哪种键盘好，看这篇就够。" * 30)
    assert chunks
    assert chunks[0]["text"].startswith("机械键盘")
    assert 300 <= len(chunks[0]["text"]) <= 600


def test_long_block_is_split_with_overlap():
    source = "。".join(f"第{i}句话说明键盘的选购要点" for i in range(80)) + "。"
    chunks = chunk_text(source)
    assert len(chunks) > 1
    assert all(len(chunk["text"]) <= 600 for chunk in chunks)


def test_large_table_repeats_the_header_on_every_piece():
    header = "| 标题 | 播放量 |\n| --- | --- |"
    rows = "\n".join(f"| 视频标题非常长的第{i}条记录 | {i}00 |" for i in range(40))
    chunks = chunk_text(f"{header}\n{rows}")
    assert len(chunks) > 1
    for chunk in chunks:
        assert "| 标题 | 播放量 |" in chunk["text"]
        assert "| --- | --- |" in chunk["text"]


def test_table_rows_split_individually_with_header():
    # 行级切块：每个数据行 + 表头独立成块，单元格信号不被稀释（A 集门 2 的对症）。
    header = "| 标题 | 播放量 |\n| --- | --- |"
    rows = "\n".join(f"| 视频{i} | {i}00 |" for i in range(5))
    chunks = chunk_text(f"{header}\n{rows}")
    # 5 个数据行 → 5 块（不再整表一块、也不再按 400 字行组合并）。
    assert len(chunks) == 5
    assert all("| 标题 | 播放量 |" in chunk["text"] for chunk in chunks)
    assert all("| --- | --- |" in chunk["text"] for chunk in chunks)
    # 每块只含一个数据行，目标单元格独占一块。
    assert chunks[0]["text"].endswith("| 视频0 | 000 |")
    assert "| 视频1 | 100 |" not in chunks[0]["text"]


def test_small_table_without_data_rows_stays_whole():
    # 只有表头、无数据行的表不逐行切（没有数据行可切）。
    header = "| 标题 | 播放量 |\n| --- | --- |"
    chunks = chunk_text(header)
    assert len(chunks) == 1


def test_duplicate_blocks_are_dropped():
    body = "重复出现的正文内容。" * 10
    chunks = chunk_text(f"{body}\n\n{body}")
    assert len(chunks) == 1


def test_similar_neighbours_collapse():
    # 近似重复：正文相同、只多一句结尾。字符 bigram 的 Jaccard 需 >0.9 才会合并。
    body = (
        "机械键盘的选购先要明确用途：办公打字看重手感与静音，游戏竞技更在意触发速度与回报率。"
        "轴体分为线性、段落与类青三类，线性直上直下适合快速连击，段落轴则提供清晰的确认感。"
        "键帽材质常见ABS与PBT，ABS手感细腻但容易打油，PBT耐磨且久用不易起油光。"
        "配列从全尺寸到六十百分比各有取舍，是否需要数字小键盘与方向键取决于桌面空间大小。"
        "连接方式分为有线、蓝牙与无线接收器三种，多设备切换要看是否支持一键换台与低延迟。"
        "灯效与热插拔属于加分项，前者影响观感，后者决定后期换轴的便利程度与可玩性。"
        "预算区间同样关键，入门与高阶的手感差距主要体现在结构填充与出厂调校上。"
    )
    near = body + "最后记得确认保修条款。"
    assert shingle_similarity(body, near) > 0.9
    # 两段各自成块（合并阶段已被 500 字上限强制切开），再靠相似度合并回一块。
    assert len(chunk_text(f"{body}\n\n{near}")) == 1


def test_readable_text_ignores_json_structures():
    assert readable_text({"elements": [{"ref": "e1"}], "text": "正文"}) == "正文"
    assert readable_text([{"excerpt": "命中片段", "path": "a.md"}]) == "命中片段"
    assert readable_text({"elements": [{"ref": "e1"}]}) == ""


def test_match_expression_keeps_short_queries_exact():
    assert match_expression("机械键盘", "trigram").startswith('"机械键盘"')
    assert match_expression("键盘", "chars") == '"键 盘"'


def test_match_expression_splits_long_goals_into_windows():
    # 长目标若整句当一个短语 → 必然 0 召回；必须拆成窗口后按 OR 连接。
    goal = "把刚才那个页面里的信息再整理一遍给我，别重新打开浏览器"
    expression = match_expression(goal, "trigram")
    assert " OR " in expression
    assert goal not in expression
    assert '"把刚才"' in expression


async def make_store(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="搜键盘"))
    return store, task


async def test_two_path_index_finds_short_chinese_queries(tmp_path):
    store, task = await make_store(tmp_path)
    indexer = FtsIndexer(store)
    result = {
        "status": "ok",
        "data": {
            "url": "https://search.bilibili.com/all?keyword=keyboard",
            "title": "搜索结果",
            "structured": "# 机械键盘\n\n买哪种键盘好，看这篇键盘选购指南。",
        },
    }
    chunks = chunks_for_tool_result(task, "browser_observe", result)
    assert chunks and chunks[0]["source_type"] == "web_page"
    assert chunks[0]["origin"].startswith("https://search.bilibili.com")
    assert await indexer.write(chunks) >= 1

    trigram = await indexer.search("机械键盘", mode="trigram")
    assert trigram and trigram[0]["origin"] == result["data"]["url"]
    # trigram 只匹配 ≥3 字符：2 字查询必须由字符级那一路兜住
    assert await indexer.search("键盘", mode="trigram") == []
    chars = await indexer.search("键盘", mode="chars")
    assert chars and "键盘" in chars[0]["text"]
    assert await indexer.search("B站", mode="trigram") == []

    # 同一页面重复观察不产生重复块
    assert await indexer.write(chunks) == 0
    await store.engine.dispose()


async def test_index_requires_an_origin_and_readable_text(tmp_path):
    store, task = await make_store(tmp_path)
    assert chunks_for_tool_result(task, "browser_observe", {"data": {"text": "正文"}}) == []
    assert chunks_for_tool_result(task, "browser_observe", {"data": {"elements": [{"ref": "e1"}]}}) == []
    chunks = chunks_for_tool_result(
        task, "local_read_document", {"data": {"path": "notes.md", "text": "笔记正文" * 60}}
    )
    assert chunks[0]["source_type"] == "local_file" and chunks[0]["origin"] == "notes.md"
    await store.engine.dispose()
