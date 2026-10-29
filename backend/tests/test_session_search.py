"""`session_search` 与 `orbit sessions` / `orbit session-search` 的契约测试。

**中文用例是重点**：本机实测（``handoff/hermes-parity/research/07-memory-layer.md``
§1.2 / §3.3）SQLite 的 ``unicode61`` 分词把整段中文当一个 token，``MATCH '记忆'``
命中 **0**，只有子串路径能命中。所以这里用「键盘」「报告」把 ``LIKE '%词%'`` 那条
路径钉住——防止将来有人"优化"成分词 / 前缀 / trigram 之后，中文能力静默丢掉。

其余用例锁的是数值与边界：``scan`` 上限、``limit`` 夹取、窗口与书挡不越界、截断
标记、``read`` 的头尾行数、``browse`` 的倒序与计数、``search`` 的模式校验，以及
CLI 侧 ``--yes`` 硬门禁。

全程不发网络请求、不调模型（``test_module_imports_no_model_code`` 直接把这条钉在
源码上）。
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest
from sqlalchemy import select

import app.session_search as session_search_module
from app.cli.commands import sessions as sessions_commands  # noqa: F401 —— 导入即接线
from app.cli.main import main
from app.config import Settings
from app.main import migrate
from app.session_search import SessionSearch, SessionSearchError
from app.storage import Conversation, Message, Store, Task

#: 造数据的时间戳模板。同一会话内按序号递增，保证「按 created_at 排序」的结果确定。
BASE = "2026-01-01T00:00:{:02d}+00:00"


def _settings(tmp_path) -> Settings:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    return settings


async def _open(settings: Settings) -> Store:
    return Store(settings.data_dir / "app.sqlite")


async def _seed(
    store: Store,
    conversation_id: str,
    title: str,
    rows: list[tuple[str, str, str]],
    *,
    offset: int = 0,
    updated_at: str = "",
) -> None:
    """建一场会话。``rows`` 是 ``(消息 id, role, content)``，时间戳按序号递增。

    最后显式回写 ``updated_at``：``Store.save`` 每存一条消息都会把会话的
    ``updated_at`` 刷成"现在"，不覆盖的话 ``browse`` 的倒序就依赖执行顺序而不是数据。
    """
    await store.save(
        Conversation(
            id=conversation_id,
            title=title,
            created_at=BASE.format(offset),
            updated_at=BASE.format(offset),
        )
    )
    for index, (message_id, role, content) in enumerate(rows):
        await store.save(
            Message(
                id=message_id,
                conversation_id=conversation_id,
                role=role,
                content=content,
                created_at=BASE.format(offset + index),
            )
        )
    async with store.sessions() as session:
        conversation = await session.get(Conversation, conversation_id)
        assert conversation is not None
        conversation.updated_at = updated_at or BASE.format(offset + len(rows))
        await session.commit()


def _many(count: int, prefix: str, content: str) -> list[tuple[str, str, str]]:
    return [(f"{prefix}{index}", "user" if index % 2 else "assistant", content) for index in range(1, count + 1)]


# ── discover ────────────────────────────────────────────────────────────────


async def test_discover_matches_chinese_substring(tmp_path):
    """中文子串必须命中——这是 unicode61 分词做不到、本模块存在的头号理由。"""
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _seed(
        store,
        "c1",
        "键盘问题",
        [
            ("m1", "user", "帮我查一下键盘的快捷键"),
            ("m2", "assistant", "键盘快捷键是 Ctrl+K"),
            ("m3", "user", "报告还没写"),
        ],
    )

    anchors = await SessionSearch(store).discover("键盘")
    # created_at 倒序：m2 晚于 m1。
    assert [anchor.message_id for anchor in anchors] == ["m2", "m1"]
    assert all("键盘" in anchor.snippet for anchor in anchors)
    assert {anchor.conversation_id for anchor in anchors} == {"c1"}
    await store.engine.dispose()


async def test_discover_requires_all_terms(tmp_path):
    """多词是 AND 语义（与 Hermes 的 FTS5 隐式 AND 一致），不是 OR。"""
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _seed(
        store,
        "c1",
        "混合",
        [
            ("m1", "user", "键盘和报告都要改"),
            ("m2", "user", "键盘只有这一个词"),
            ("m3", "user", "报告只有这一个词"),
        ],
    )

    anchors = await SessionSearch(store).discover("键盘 报告")
    assert [anchor.message_id for anchor in anchors] == ["m1"]
    await store.engine.dispose()


async def test_discover_escapes_like_wildcards(tmp_path):
    """``%`` / ``_`` 必须当字面量：不转义的话 ``100%`` 会退化成"匹配一切"。"""
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _seed(
        store,
        "c1",
        "转义",
        [
            ("m1", "user", "进度 100% 完成"),
            ("m2", "user", "进度 100 完成"),
            ("m3", "user", "文件 a_b.txt"),
            ("m4", "user", "文件 axb.txt"),
        ],
    )
    searcher = SessionSearch(store)

    assert [anchor.message_id for anchor in await searcher.discover("100%")] == ["m1"]
    assert [anchor.message_id for anchor in await searcher.discover("a_b")] == ["m3"]
    await store.engine.dispose()


async def test_discover_respects_scan_limit(tmp_path):
    """``scan`` 是硬上限：LIKE 用不上索引，无上限扫描会把 CLI 卡死。"""
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _seed(store, "c1", "海量", _many(12, "m", "报告进度更新"))

    searcher = SessionSearch(store)
    assert len(await searcher.discover("报告", limit=10, scan=4)) == 4
    assert len(await searcher.discover("报告", limit=10, scan=300)) == 10
    await store.engine.dispose()


async def test_discover_clamps_limit(tmp_path):
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _seed(store, "c1", "海量", _many(15, "m", "报告进度更新"))
    searcher = SessionSearch(store)

    assert len(await searcher.discover("报告", limit=99)) == 10, "limit 上界是 10"
    assert len(await searcher.discover("报告", limit=0)) == 1, "limit 下界是 1"
    await store.engine.dispose()


async def test_discover_filters_by_conversation(tmp_path):
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _seed(store, "c1", "甲", [("m1", "user", "键盘坏了")])
    await _seed(store, "c2", "乙", [("m2", "user", "键盘也坏了")], offset=10)
    searcher = SessionSearch(store)

    assert [a.message_id for a in await searcher.discover("键盘")] == ["m2", "m1"]
    assert [a.message_id for a in await searcher.discover("键盘", conversation_id="c1")] == ["m1"]
    await store.engine.dispose()


async def test_discover_empty_query_returns_nothing(tmp_path):
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _seed(store, "c1", "甲", [("m1", "user", "随便说点什么")])
    assert await SessionSearch(store).discover("   ") == []
    await store.engine.dispose()


# ── anchored_view（scroll）────────────────────────────────────────────────────


async def _window_fixture(store: Store) -> None:
    """c1：16 条消息（秒 10..25）；c2：3 条消息，时间戳故意落在 c1 的区间里。"""
    await _seed(store, "c1", "长会话", _many(16, "m", "内容"), offset=10)
    await _seed(store, "c2", "干扰会话", _many(3, "x", "内容"), offset=12)


async def test_anchored_view_window_and_bookend(tmp_path):
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _window_fixture(store)

    view = await SessionSearch(store).anchored_view("m10", window=5, bookend=3)
    window_ids = [item["message_id"] for item in view["window"]]

    assert window_ids == ["m5", "m6", "m7", "m8", "m9", "m10", "m11", "m12", "m13", "m14", "m15"]
    assert [item["message_id"] for item in view["before"]] == ["m2", "m3", "m4"]
    assert [item["message_id"] for item in view["after"]] == ["m16"]
    # 锚点必须被标出来，且只标一次。
    assert [item["message_id"] for item in view["window"] if item.get("anchor")] == ["m10"]
    assert view["anchor"]["message_id"] == "m10"
    # 跨会话不许漏：c2 的时间戳落在 c1 区间内，也不能出现在窗口里。
    assert not [item for item in view["window"] if item["message_id"].startswith("x")]
    await store.engine.dispose()


async def test_anchored_view_does_not_cross_conversation_edges(tmp_path):
    """锚点是首条 / 末条时不越界：``before`` / ``after`` 为空，窗口自然变短。"""
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _window_fixture(store)
    searcher = SessionSearch(store)

    first = await searcher.anchored_view("m1", window=5, bookend=3)
    assert first["before"] == []
    assert [item["message_id"] for item in first["window"]] == ["m1", "m2", "m3", "m4", "m5", "m6"]

    last = await searcher.anchored_view("m16", window=5, bookend=3)
    assert last["after"] == []
    assert [item["message_id"] for item in last["window"]] == [
        "m11", "m12", "m13", "m14", "m15", "m16",
    ]
    assert [item["message_id"] for item in last["before"]] == ["m8", "m9", "m10"]
    await store.engine.dispose()


async def test_anchored_view_marks_truncation(tmp_path):
    """窗口按 4000 字符、书挡按 1200 字符截断，且每条都标明 ``truncated``。"""
    settings = _settings(tmp_path)
    store = await _open(settings)
    long_text = "长" * 5_000
    medium_text = "中" * 2_000
    await _seed(
        store,
        "c1",
        "长消息",
        [
            ("m1", "user", "短消息"),
            ("m2", "user", medium_text),
            ("m3", "user", long_text),
            ("m4", "user", "再来一条"),
            ("m5", "user", "还有一条"),
        ],
        offset=10,
    )

    view = await SessionSearch(store).anchored_view("m4", window=1, bookend=2)
    window = {item["message_id"]: item for item in view["window"]}
    assert window["m3"]["truncated"] is True
    assert window["m3"]["chars"] == 5_000
    assert window["m3"]["content"].endswith("…")
    assert window["m4"]["truncated"] is False
    assert window["m4"]["content"] == "再来一条"

    bookend = {item["message_id"]: item for item in view["before"]}
    assert bookend["m2"]["truncated"] is True, "书挡按 1200 字符截断"
    assert bookend["m2"]["chars"] == 2_000
    assert bookend["m1"]["truncated"] is False
    await store.engine.dispose()


async def test_anchored_view_drops_compaction_summaries_from_bookend(tmp_path):
    """压缩交接摘要不许借书挡回流（Hermes #43175 的同一条约束）。"""
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _seed(
        store,
        "c1",
        "压缩",
        [
            ("m1", "user", "[CONTEXT COMPACTION] 这是机器写的摘要载荷"),
            ("m2", "user", "[CONTEXT SUMMARY]: 另一份摘要"),
            ("m3", "user", "真实上下文 A"),
            ("m4", "user", "锚点"),
            ("m5", "user", "后面"),
        ],
        offset=10,
    )

    view = await SessionSearch(store).anchored_view("m4", window=1, bookend=3)
    assert [item["message_id"] for item in view["window"]] == ["m3", "m4", "m5"]
    assert view["before"] == [], "摘要必须被剔除，剩下的是真实上下文"
    await store.engine.dispose()


async def test_anchored_view_unknown_message_raises(tmp_path):
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _seed(store, "c1", "甲", [("m1", "user", "内容")])

    with pytest.raises(SessionSearchError):
        await SessionSearch(store).anchored_view("不存在")
    await store.engine.dispose()


# ── read ────────────────────────────────────────────────────────────────────


async def test_read_short_message_is_verbatim(tmp_path):
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _seed(store, "c1", "甲", [("m1", "user", "第一行\n第二行")])

    payload = await SessionSearch(store).read("m1")
    assert payload["truncated"] is False
    assert payload["omitted_lines"] == 0
    assert payload["content"] == "第一行\n第二行"
    assert payload["total_lines"] == 2
    await store.engine.dispose()


async def test_read_long_message_keeps_head_and_tail(tmp_path):
    settings = _settings(tmp_path)
    store = await _open(settings)
    body = "\n".join(f"第{index}行" for index in range(1, 41))
    await _seed(store, "c1", "甲", [("m1", "tool", body)])

    payload = await SessionSearch(store).read("m1")
    assert payload["truncated"] is True
    assert payload["total_lines"] == 40
    assert payload["omitted_lines"] == 10
    assert payload["head"] == [f"第{index}行" for index in range(1, 21)]
    assert payload["tail"] == [f"第{index}行" for index in range(31, 41)]
    assert "…（省略 10 行）…" in payload["content"]
    await store.engine.dispose()


async def test_read_unknown_message_raises(tmp_path):
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _seed(store, "c1", "甲", [("m1", "user", "内容")])

    with pytest.raises(SessionSearchError):
        await SessionSearch(store).read("不存在")
    await store.engine.dispose()


# ── browse ──────────────────────────────────────────────────────────────────


async def test_browse_is_reverse_chronological_with_counts(tmp_path):
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _seed(
        store,
        "c1",
        "旧会话",
        [("m1", "assistant", "开场白"), ("m2", "user", "第一条用户消息"), ("m3", "user", "第二条")],
        offset=10,
        updated_at=BASE.format(20),
    )
    await _seed(
        store,
        "c2",
        "新会话",
        [("n1", "user", "新会话的第一句")],
        offset=30,
        updated_at=BASE.format(40),
    )

    sessions = await SessionSearch(store).browse(limit=10)
    assert [item["id"] for item in sessions] == ["c2", "c1"], "按 updated_at 倒序"
    assert [item["message_count"] for item in sessions] == [1, 3]
    assert sessions[0]["first_user_snippet"] == "新会话的第一句"
    # 首条 user 消息而不是首条消息（c1 的第一条是 assistant）。
    assert sessions[1]["first_user_snippet"] == "第一条用户消息"
    assert sessions[1]["title"] == "旧会话"
    await store.engine.dispose()


# ── search（统一入口）───────────────────────────────────────────────────────


async def test_search_dispatches_all_four_modes(tmp_path):
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _seed(store, "c1", "甲", [("m1", "user", "键盘坏了"), ("m2", "user", "换个键盘")], offset=10)
    searcher = SessionSearch(store)

    discovered = await searcher.search("discover", query="键盘")
    assert discovered["mode"] == "discover"
    assert discovered["count"] == 2
    assert discovered["results"][0]["message_id"] == "m2"

    scrolled = await searcher.search("scroll", message_id="m1", window=1)
    assert scrolled["mode"] == "scroll"
    assert [item["message_id"] for item in scrolled["window"]] == ["m1", "m2"]

    read = await searcher.search("read", message_id="m2")
    assert read["mode"] == "read"
    assert read["content"] == "换个键盘"

    browsed = await searcher.search("browse", limit=5)
    assert browsed["mode"] == "browse"
    assert browsed["count"] == 1
    await store.engine.dispose()


async def test_search_rejects_unknown_mode(tmp_path):
    settings = _settings(tmp_path)
    store = await _open(settings)
    with pytest.raises(SessionSearchError) as excinfo:
        await SessionSearch(store).search("grep", query="键盘")
    assert "discover" in str(excinfo.value), "报错要列出可选模式"
    await store.engine.dispose()


async def test_search_requires_arguments_per_mode(tmp_path):
    settings = _settings(tmp_path)
    store = await _open(settings)
    searcher = SessionSearch(store)

    with pytest.raises(SessionSearchError):
        await searcher.search("discover", query="  ")
    with pytest.raises(SessionSearchError):
        await searcher.search("scroll")
    with pytest.raises(SessionSearchError):
        await searcher.search("read")
    await store.engine.dispose()


# ── 零 LLM 约束 ─────────────────────────────────────────────────────────────


def test_module_imports_no_model_code():
    """整个模块不许 import 任何模型/连接器：召回的成本必须恒等于返回内容本身。

    用 AST 只看 import 语句（而不是 grep 全文）：docstring 里本来就要提到"不许 import
    langchain"这件事，字符串匹配会把说明文字当成违规。
    """
    tree = ast.parse(Path(session_search_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden = ("langchain", "openai", "httpx", "app.providers", "app.connectors", "app.tools")
    for name in imported:
        assert not any(name == bad or name.startswith(f"{bad}.") for bad in forbidden), name
    # 白名单而不是黑名单：将来新增一个 import 必须有人主动确认它不碰模型。
    #
    # 9.6 收尾时新增了两个，逐个确认过：
    # - ``pathlib``：只为从 ``Store.engine.url`` 反推 ``data_dir``（找 fts.sqlite）；
    # - ``app.session_index``：**函数内 import**（模块级仍是上面那几个），它是纯
    #   ``sqlite3`` + 标准库的检索层，既不碰模型也不碰连接器。允许它进来是 9.6 的
    #   前提——否则"优先走 FTS"这条根本没法实现。
    assert imported <= {
        "__future__",
        "dataclasses",
        "pathlib",
        "typing",
        "sqlalchemy",
        "app.session_index",
        "app.storage",
    }, imported


# ── CLI：sessions ───────────────────────────────────────────────────────────


async def _cli_seed(tmp_path) -> Settings:
    """给 CLI 用例造库：2 场会话 + 1 个带路径的任务，然后释放连接。"""
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _seed(
        store,
        "c1",
        "周报",
        [
            ("m1", "user", "帮我整理 E:\\reports 里的表格"),
            ("m2", "assistant", "已整理完毕"),
        ],
        offset=10,
        updated_at=BASE.format(20),
    )
    await _seed(store, "c2", "reports 归档", [("n1", "user", "归档一下")], offset=30, updated_at=BASE.format(40))
    await store.save(Task(conversation_id="c1", goal="整理 E:\\reports 里的表格"))
    await store.engine.dispose()
    return settings


def _argv(settings: Settings, *rest: str) -> list[str]:
    return ["--data-dir", str(settings.data_dir), *rest]


async def test_sessions_list_cli(tmp_path, capsys):
    settings = await _cli_seed(tmp_path)
    assert main(_argv(settings, "--json", "sessions", "list")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 2
    assert [item["id"] for item in payload["sessions"]] == ["c2", "c1"]


async def test_sessions_list_really_filters_by_source(tmp_path, capsys):
    """**`--source` 现在真的筛选**（第 50 轮，MATRIX 10.x）。

    这条测试原来叫 `test_sessions_list_ignores_source_with_a_reason`，断言的是
    「参数被忽略 + 给了理由」。**那在当时是诚实的**（`conversations` 没有来源列），
    但它编码的是**缺陷**——**「有理由地忽略一个参数」仍然比「让参数生效」差一档**：
    第三方集成传 `--source tool` 想隐藏会话，实际什么都不会发生。

    现在 `conversations.source` 列存在了，这条改成断言**真筛选**：
    只传 `--source cli` 时拿到 cli 会话；传一个没人用过的来源时拿到空列表。
    """
    settings = await _cli_seed(tmp_path)

    assert main(_argv(settings, "--json", "sessions", "list", "--source", "cli")) == 0
    payload = json.loads(capsys.readouterr().out)
    # 种子会话的 source 默认就是 cli → 全都在
    assert payload["count"] == 2
    assert payload["source"] == "cli"

    # 换一个没人用过的来源 → **真的筛掉了**（旧行为会原样返回全部）
    assert main(_argv(settings, "--json", "sessions", "list", "--source", "tool")) == 0
    other = json.loads(capsys.readouterr().out)
    assert other["count"] == 0, "`--source` 没生效——它又变回一个被忽略的参数了"


async def test_sessions_list_workspace_matches_goal_and_title(tmp_path, capsys):
    settings = await _cli_seed(tmp_path)

    assert main(_argv(settings, "--json", "sessions", "list", "--workspace", "reports")) == 0
    by_path = json.loads(capsys.readouterr().out)
    assert {item["id"] for item in by_path["sessions"]} == {"c1", "c2"}, "c1 命中任务目标，c2 命中标题"

    capsys.readouterr()
    assert main(_argv(settings, "--json", "sessions", "list", "--workspace", "周报")) == 0
    by_title = json.loads(capsys.readouterr().out)
    assert [item["id"] for item in by_title["sessions"]] == ["c1"]


async def test_sessions_browse_cli(tmp_path, capsys):
    settings = await _cli_seed(tmp_path)
    assert main(_argv(settings, "--json", "sessions", "browse")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 2
    assert payload["sessions"][0]["first_user_snippet"] == "归档一下"


async def test_sessions_stats_cli(tmp_path, capsys):
    settings = await _cli_seed(tmp_path)
    assert main(_argv(settings, "--json", "sessions", "stats")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["conversations"] == 2
    assert payload["messages"] == 3
    assert payload["average_messages_per_conversation"] == 1.5
    assert payload["oldest_message_at"] == BASE.format(10)
    assert payload["newest_message_at"] == BASE.format(30)


async def test_sessions_rename_cli_writes_the_title(tmp_path, capsys):
    settings = await _cli_seed(tmp_path)
    assert main(_argv(settings, "sessions", "rename", "c1", "新的标题")) == 0
    capsys.readouterr()

    store = await _open(settings)
    conversation = await store.get(Conversation, "c1")
    assert conversation is not None and conversation.title == "新的标题"
    await store.engine.dispose()


async def test_sessions_rename_unknown_id_fails_cleanly(tmp_path, capsys):
    settings = await _cli_seed(tmp_path)
    assert main(_argv(settings, "sessions", "rename", "nope", "标题")) == 1
    assert "会话不存在" in capsys.readouterr().err


async def test_sessions_export_jsonl_and_markdown(tmp_path, capsys):
    settings = await _cli_seed(tmp_path)

    assert main(_argv(settings, "sessions", "export", "c1", "--format", "jsonl")) == 0
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 3, "1 行会话头 + 2 条消息"
    assert json.loads(lines[0])["title"] == "周报"
    assert json.loads(lines[1])["role"] == "user"

    assert main(_argv(settings, "sessions", "export", "c1", "--format", "md")) == 0
    markdown = capsys.readouterr().out
    assert "# 周报" in markdown
    assert "## user" in markdown


async def test_sessions_export_unsupported_format_is_explicit(tmp_path, capsys):
    settings = await _cli_seed(tmp_path)
    assert main(_argv(settings, "sessions", "export", "c1", "--format", "qmd")) == 1
    assert "未实现" in capsys.readouterr().err


async def test_sessions_delete_requires_yes(tmp_path, capsys):
    settings = await _cli_seed(tmp_path)
    assert main(_argv(settings, "sessions", "delete", "c1")) == 1
    assert "--yes" in capsys.readouterr().err

    store = await _open(settings)
    remaining = await store.rows(select(Message).where(Message.conversation_id == "c1"))
    assert len(remaining) == 2, "缺 --yes 时不许动数据"
    await store.engine.dispose()


async def test_sessions_delete_with_yes_removes_messages(tmp_path, capsys):
    settings = await _cli_seed(tmp_path)
    assert main(_argv(settings, "sessions", "delete", "c1", "--yes")) == 0
    capsys.readouterr()

    store = await _open(settings)
    assert await store.rows(select(Message).where(Message.conversation_id == "c1")) == []
    assert await store.get(Conversation, "c1") is None
    assert await store.rows(select(Task).where(Task.conversation_id == "c1")) == []
    await store.engine.dispose()


# ── CLI：session-search（Orbit 自有入口）─────────────────────────────────────


async def test_session_search_discover_cli_finds_chinese(tmp_path, capsys):
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _seed(store, "c1", "键盘", [("m1", "user", "键盘的快捷键是什么")])
    await store.engine.dispose()

    assert main(_argv(settings, "session-search", "discover", "键盘")) == 0
    out = capsys.readouterr().out
    assert "命中 1 条" in out
    assert "m1" in out

    assert main(_argv(settings, "--json", "session-search", "discover", "键盘")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["results"][0]["message_id"] == "m1"


async def test_session_search_scroll_read_browse_cli(tmp_path, capsys):
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _seed(store, "c1", "甲", [("m1", "user", "第一句"), ("m2", "user", "第二句")], offset=10)
    await store.engine.dispose()

    assert main(_argv(settings, "--json", "session-search", "scroll", "m1", "--window", "1")) == 0
    view = json.loads(capsys.readouterr().out)
    assert [item["message_id"] for item in view["window"]] == ["m1", "m2"]

    assert main(_argv(settings, "--json", "session-search", "read", "m2")) == 0
    assert json.loads(capsys.readouterr().out)["content"] == "第二句"

    assert main(_argv(settings, "--json", "session-search", "browse", "--limit", "5")) == 0
    assert json.loads(capsys.readouterr().out)["count"] == 1


async def test_session_search_discover_accepts_multiple_terms(tmp_path, capsys):
    settings = _settings(tmp_path)
    store = await _open(settings)
    await _seed(
        store,
        "c1",
        "甲",
        [("m1", "user", "键盘和报告都要改"), ("m2", "user", "只有键盘")],
    )
    await store.engine.dispose()

    assert main(_argv(settings, "--json", "session-search", "discover", "键盘", "报告")) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [item["message_id"] for item in payload["results"]] == ["m1"]
