"""`app/dashboard.py`（18 页语义登记 + 会话回放）与 `dashboard` 新子命令的契约测试。

## 为什么这些断言必须"读文件"而不是硬编码

页面覆盖状态的唯一意义是**诚实**。硬编码一份"chat 有 /api/xxx"的期望值，等于把
"我核过"换成"我记得"——`app/main.py` 一改，测试还绿着，面板就开始撒谎。
所以这里凡是涉及路由的断言，都现场 ``read_text`` 读 ``app/main.py`` 再比对。

## 三条口径（改实现前先读）

1. ``PAGES`` 恰好 18 个，名字与 Hermes 的 18 条顶层路由一一对应（子页
   ``/profiles/:id/build`` 不算独立页）。
2. ``report()`` 的三类计数必须自洽：三类之和 = 总数 = ``len(PAGES)``。
3. ``covered`` / ``partial`` 必须给 ``orbit_route``；``missing`` 必须写清 ``gap``；
   且 ``orbit_route`` 里出现的每条 ``/api/...`` 都必须在 ``app/main.py`` 里真实存在。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from app.cli.main import main
from app.config import Settings
from app.dashboard import (
    PAGES,
    STATUSES,
    main_routes,
    page_by_name,
    report,
    session_replay,
    unbacked_routes,
)
from app.main import migrate
from app.storage import Conversation, Message, Store, Task, TaskEvent

# 导入即接线（与 `test_skin.py` 同一个手法：`commands/__init__.py` 由主线统一接，
# 这里显式导入一次，测试就不依赖那条接线是否已合并）。
import app.cli.commands.ui_extra  # noqa: F401  # isort: skip

#: `app/main.py` 的绝对路径。**只读**，用来核对路由是否真的存在。
MAIN_PY = Path(__file__).resolve().parents[1] / "app" / "main.py"

#: Hermes dashboard 的 18 条顶层路由（`hermes_cli/web_dist/assets/index-*.js` 里的
#: `path:` 定义）。写成常量是为了锁住"18"这个数字的来源，而不是靠记忆。
HERMES_PAGES = frozenset(
    {
        "analytics",
        "channels",
        "chat",
        "config",
        "cron",
        "docs",
        "env",
        "files",
        "logs",
        "mcp",
        "models",
        "pairing",
        "plugins",
        "profiles",
        "sessions",
        "skills",
        "system",
        "webhooks",
    }
)

#: 从 ``orbit_route`` 里抽 ``/api/...`` 的字面量。
ROUTE_TOKEN = re.compile(r"/api/[A-Za-z0-9_/{}\-]+")


# ── 18 个页面 ───────────────────────────────────────────────────────────────


def test_pages_are_exactly_the_hermes_eighteen():
    """恰好 18 页，名字与 Hermes 的顶层路由一致，且不重复。"""
    names = [page.name for page in PAGES]
    assert len(PAGES) == 18, f"页面数应为 18，实际 {len(PAGES)}"
    assert len(names) == len(set(names)), f"页面名重复：{names}"
    assert set(names) == set(HERMES_PAGES), f"与 Hermes 路由对不上：{set(names) ^ set(HERMES_PAGES)}"


def test_every_page_has_purpose_and_data_sources():
    """每页都要说清"解决什么问题"和"需要哪些数据"，否则覆盖判定无从谈起。"""
    for page in PAGES:
        assert page.title.strip(), page.name
        assert page.purpose.strip(), f"{page.name} 缺 purpose"
        assert page.data_sources, f"{page.name} 缺 data_sources"
        assert all(source.strip() for source in page.data_sources), page.name
        assert page.orbit_status in STATUSES, page.name


def test_missing_pages_explain_what_is_missing():
    """`missing` 必须写 gap（要可行动），`covered`/`partial` 必须给 orbit_route。"""
    for page in PAGES:
        if page.orbit_status == "missing":
            assert page.gap.strip(), f"{page.name} 标了 missing 却没写缺什么"
            assert not page.orbit_route, f"{page.name} 标了 missing 却给了 orbit_route"
        else:
            assert page.orbit_route.strip(), f"{page.name} 标了 {page.orbit_status} 却没给 orbit_route"


def test_report_counts_are_self_consistent():
    """三类计数之和 = total = len(PAGES)，且与逐页状态一致。"""
    summary = report()
    assert summary["total"] == len(PAGES) == 18
    assert summary["covered"] + summary["partial"] + summary["missing"] == summary["total"]
    for status in STATUSES:
        expected = sum(1 for page in PAGES if page.orbit_status == status)
        assert summary[status] == expected, f"{status} 计数与逐页状态不一致"
    assert len(summary["pages"]) == 18


# ── 覆盖状态的诚实性：读 app/main.py 现场核对 ────────────────────────────────


def test_main_py_route_extraction_actually_works():
    """先证明"抽取路由"这件事本身没坏，后面的断言才有意义。

    正则写错会让 :func:`unbacked_routes` 把一切都判成对不上（或者反过来全放过），
    所以这里正向断言几条**一定存在**的路由。
    """
    routes = main_routes()
    assert len(routes) >= 25, f"只抽到 {len(routes)} 条路由，正则大概率坏了"
    for path in ("/api/health", "/api/skills", "/api/mcp", "/api/conversations"):
        assert path in routes, f"{path} 没被抽出来"


@pytest.mark.parametrize("name", ["chat", "sessions", "skills"])
def test_three_covered_pages_point_at_routes_that_really_exist(name: str):
    """抽查 3 个 `covered` 页：`orbit_route` 里的路由必须在 `app/main.py` 里真实存在。"""
    page = page_by_name(name)
    assert page is not None, f"没有登记页面 {name}"
    assert page.orbit_status == "covered", f"{name} 不是 covered：{page.orbit_status}"
    claimed = ROUTE_TOKEN.findall(page.orbit_route)
    assert claimed, f"{name} 的 orbit_route 没写具体路由：{page.orbit_route}"
    text = MAIN_PY.read_text(encoding="utf-8")
    for route in claimed:
        assert route in text, f"{name} 声称的 {route} 在 app/main.py 里找不到"


def test_no_page_claims_a_route_that_main_py_does_not_have():
    """全量核对：任何页面声明的 `/api/...` 都必须在 `app/main.py` 里存在。

    这是"不许把没数据源的页面标 covered"的机械保证——`report()["unbacked"]` 必须是空。
    """
    routes = main_routes()
    offenders = {
        page.name: unbacked_routes(page, routes)
        for page in PAGES
        if unbacked_routes(page, routes)
    }
    assert offenders == {}, f"这些页面声明了不存在的路由：{offenders}"
    assert report()["unbacked"] == {}


def test_every_covered_page_is_backed_by_a_real_route():
    """**每条 covered 都必须有真实路由兜底**——这才是该守的东西。

    原来这条断言的是「`missing` 至少 1 个」。那是当时的事实，不是契约：docs / files /
    webhooks 补了后端之后 `missing` 归零，旧断言就成了"不许把活干完"。
    数量会变，"声明与路由一致"不会——所以改成断言后者，并保留一个上界防止
    一夜之间全部标 covered 却不改任何路由。
    """
    summary = report()
    assert summary["covered"] <= 10, f"covered 有 {summary['covered']} 个，口径多半放宽了"
    # 关键断言：unbacked 必须为空，且 covered 的页都得声明了路由
    assert summary["unbacked"] == {}, f"这些声明的路由不存在：{summary['unbacked']}"
    for page in summary["pages"]:
        if page["orbit_status"] == "covered":
            assert page["orbit_route"].strip(), f"{page['name']} 标了 covered 却没声明路由"


def test_page_by_name_is_case_insensitive():
    assert page_by_name("CHAT") is not None
    assert page_by_name("nope") is None


# ── 会话回放：合并口径 ──────────────────────────────────────────────────────


async def _store(tmp_path: Path) -> tuple[Settings, Store]:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    return settings, Store(settings.data_dir / "app.sqlite")


async def _seeded(tmp_path: Path) -> tuple[Settings, Store, str]:
    """造一个会话：3 条消息 + 2 个任务事件，时间刻意交错。"""
    settings, store = await _store(tmp_path)
    conversation = await store.save(Conversation(title="回放测试会话"))
    await store.save(
        Message(
            conversation_id=conversation.id,
            role="user",
            content="第一句：查一下天气",
            created_at="2026-10-01T10:00:00+00:00",
        )
    )
    task = await store.save(Task(conversation_id=conversation.id, goal="查天气"))
    await store.save(
        TaskEvent(
            task_id=task.id,
            event_type="tool_start",
            payload={"tool": "web_search", "query": "天气"},
            created_at="2026-10-01T10:00:02+00:00",
        )
    )
    await store.save(
        TaskEvent(
            task_id=task.id,
            event_type="tool_done",
            payload={"tool": "web_search", "status": "DONE"},
            created_at="2026-10-01T10:00:05+00:00",
        )
    )
    await store.save(
        Message(
            conversation_id=conversation.id,
            role="assistant",
            content="第二句：晴，26 度",
            created_at="2026-10-01T10:00:03+00:00",
        )
    )
    await store.save(
        Message(
            conversation_id=conversation.id,
            role="assistant",
            content="第三句：需要我设个提醒吗",
            created_at="2026-10-01T10:00:09+00:00",
        )
    )
    return settings, store, conversation.id


async def test_session_replay_merges_messages_and_events_by_time(tmp_path):
    """消息与事件按 created_at 交错合并，而不是"先全部消息再全部事件"。"""
    _, store, conversation_id = await _seeded(tmp_path)
    result = await session_replay(store, conversation_id)
    await store.engine.dispose()

    assert result["found"] is True
    assert result["title"] == "回放测试会话"
    assert result["total_frames"] == 5
    labels = [(frame["kind"], frame["label"]) for frame in result["frames"]]
    assert labels == [
        ("message", "user"),
        ("event", "tool_start"),
        ("message", "assistant"),
        ("event", "tool_done"),
        ("message", "assistant"),
    ], "合并顺序不对：应当按时间交错"
    offsets = [frame["offset_ms"] for frame in result["frames"]]
    assert offsets == [0, 2000, 3000, 5000, 9000]
    assert result["duration_ms"] == 9000
    assert result["frames"][0]["text"] == "第一句：查一下天气"
    # 事件 payload 被压成一行人能读的字，不是原样 JSON。
    assert "tool=web_search" in result["frames"][1]["text"]


async def test_session_replay_limit_keeps_the_most_recent_frames(tmp_path):
    """`limit` 取**尾部最近 N 帧**，并且 offset 从保留下来的首帧重新起算。"""
    _, store, conversation_id = await _seeded(tmp_path)
    result = await session_replay(store, conversation_id, limit=2)
    await store.engine.dispose()

    assert result["total_frames"] == 5
    assert result["returned"] == 2
    assert result["dropped"] == 3
    assert result["truncated"] is True
    assert [frame["seq"] for frame in result["frames"]] == [1, 2], "seq 从 1 重排"
    # 首帧是 10:00:05 那条事件 → 它自己的偏移归零，下一帧 4 秒后。
    assert result["frames"][0]["offset_ms"] == 0
    assert result["frames"][1]["offset_ms"] == 4000
    assert result["frames"][0]["label"] == "tool_done"


async def test_session_replay_speed_only_scales_the_delay(tmp_path):
    """倍速只改 `delay_ms`，不改顺序、不改 `offset_ms`。"""
    _, store, conversation_id = await _seeded(tmp_path)
    normal = await session_replay(store, conversation_id)
    fast = await session_replay(store, conversation_id, speed=2.0)
    await store.engine.dispose()

    assert [frame["offset_ms"] for frame in fast["frames"]] == [
        frame["offset_ms"] for frame in normal["frames"]
    ]
    assert [frame["delay_ms"] for frame in fast["frames"]] == [
        frame["offset_ms"] // 2 for frame in normal["frames"]
    ]
    assert [frame["kind"] for frame in fast["frames"]] == [
        frame["kind"] for frame in normal["frames"]
    ]


async def test_session_replay_empty_conversation_does_not_crash(tmp_path):
    """空会话：返回空帧列表，不是异常。"""
    _, store = await _store(tmp_path)
    conversation = await store.save(Conversation(title="空的"))
    result = await session_replay(store, conversation.id)
    await store.engine.dispose()

    assert result["found"] is True
    assert result["frames"] == []
    assert result["total_frames"] == 0
    assert result["duration_ms"] == 0


async def test_session_replay_unknown_session_reports_not_found(tmp_path):
    """写错的会话 id：`found=False`，而不是抛栈。"""
    _, store = await _store(tmp_path)
    result = await session_replay(store, "does-not-exist")
    await store.engine.dispose()

    assert result["found"] is False
    assert result["frames"] == []
    assert result["total_frames"] == 0


async def test_session_replay_rejects_bad_speed_and_limit(tmp_path):
    """`speed <= 0` / `limit < 1` 直接报错，不静默兜底成原速或全部。"""
    _, store, conversation_id = await _seeded(tmp_path)
    with pytest.raises(ValueError, match="speed"):
        await session_replay(store, conversation_id, speed=0)
    with pytest.raises(ValueError, match="limit"):
        await session_replay(store, conversation_id, limit=0)
    await store.engine.dispose()


async def test_session_replay_ignores_other_conversations(tmp_path):
    """别的会话的消息/事件不能混进来。"""
    _, store, conversation_id = await _seeded(tmp_path)
    other = await store.save(Conversation(title="另一个会话"))
    await store.save(
        Message(
            conversation_id=other.id,
            role="user",
            content="不相干的一句",
            created_at="2026-10-01T10:00:01+00:00",
        )
    )
    other_task = await store.save(Task(conversation_id=other.id, goal="别的任务"))
    await store.save(
        TaskEvent(
            task_id=other_task.id,
            event_type="noise",
            payload={"x": 1},
            created_at="2026-10-01T10:00:04+00:00",
        )
    )
    result = await session_replay(store, conversation_id)
    await store.engine.dispose()

    texts = " ".join(frame["text"] for frame in result["frames"])
    assert "不相干" not in texts
    assert all(frame["label"] != "noise" for frame in result["frames"])
    assert result["total_frames"] == 5


# ── CLI：dashboard pages / dashboard replay / desktop ────────────────────────


def test_cli_dashboard_pages_lists_all_eighteen(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "dashboard", "pages"])
    out = capsys.readouterr().out
    assert code == 0
    assert "18 个页面" in out
    for name in HERMES_PAGES:
        assert name in out, f"{name} 没出现在 `dashboard pages` 输出里"


def test_cli_dashboard_pages_json_is_machine_readable(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "--json", "dashboard", "pages"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["total"] == 18
    assert payload["covered"] + payload["partial"] + payload["missing"] == 18
    assert payload["unbacked"] == {}
    assert {page["name"] for page in payload["pages"]} == HERMES_PAGES


def test_cli_dashboard_pages_status_filter(tmp_path, capsys):
    # docs / files / webhooks 已有后端路由，所以它们现在在 `covered` 里而不是 `missing`。
    code = main(
        ["--data-dir", str(tmp_path / "data"), "dashboard", "pages", "--status", "covered"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "docs" in out and "webhooks" in out
    # 用页面**标题**而不是 id 判断：cron 这个词也出现在 webhooks 的说明里，
    # 拿 id 做子串判断会误伤。
    assert "定时任务" not in out, "过滤没生效：partial 的页面还在表里"


def test_cli_dashboard_pages_rejects_unknown_status(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "dashboard", "pages", "--status", "nope"])
    assert code == 2
    assert "未知状态" in capsys.readouterr().err


async def test_cli_dashboard_replay_prints_frames(tmp_path, capsys):
    settings, store, conversation_id = await _seeded(tmp_path)
    await store.engine.dispose()
    code = main(
        [
            "--data-dir",
            str(settings.data_dir),
            "dashboard",
            "replay",
            conversation_id,
            "--speed",
            "2",
            "--limit",
            "2",
        ]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "会话回放" in out
    assert "回放 2 帧" in out
    assert "--limit 截掉了最早的 3 帧" in out


async def test_cli_dashboard_replay_unknown_session_exits_nonzero(tmp_path, capsys):
    settings, store = await _store(tmp_path)
    await store.engine.dispose()
    code = main(["--data-dir", str(settings.data_dir), "dashboard", "replay", "no-such-id"])
    assert code == 1
    assert "找不到会话" in capsys.readouterr().err


def test_cli_dashboard_replay_requires_session_id(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "dashboard", "replay"])
    assert code == 2
    assert "用法" in capsys.readouterr().err


def test_cli_dashboard_replay_rejects_bad_speed(tmp_path, capsys):
    code = main(
        ["--data-dir", str(tmp_path / "data"), "dashboard", "replay", "x", "--speed", "0"]
    )
    assert code == 2
    assert "--speed" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["desktop", "gui"])
def test_cli_desktop_reports_not_implemented_with_alternative(tmp_path, capsys, command):
    """`desktop` / `gui` 必须如实说没实现，并给出可照做的等价做法。"""
    code = main(["--data-dir", str(tmp_path / "data"), command])
    err = capsys.readouterr().err
    assert code == 3, "未实现应当是明确的非 0 退出码（3），不是 0"
    assert "未实现" in err
    assert "没有原生桌面壳" in err
    assert "orbit dashboard" in err, "没给等价做法"
    assert "--source" in err, "Hermes 的 flag 语义没交代"
