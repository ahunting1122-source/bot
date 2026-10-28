"""元工具（`todo` / `clarify`）与可用性探针（`check_fn`）的契约测试。

三个部分各测各的，失败时能直接看出是哪一层坏了：

1. **todo**：增删改查、两条上限（单任务 50 条 / 单条 200 字符）、"每次调用都回读
   完整清单"、**重启后状态还在**（落盘而不是内存）。
2. **clarify**：选项数量校验、渲染文本里含全部选项、`allow_free_text=False` 的
   "只能选"提示，以及最关键的——它返回的 `needs_user` 载荷真的**穿透了
   `ToolRegistry.execute`**（不改 `app/graph/nodes.py` 就能复用 `ask_user` 通路）。
3. **check_fn**：可用 / 不可用、30 秒 TTL 内不重复探测（数调用次数）、60 秒抖动
   宽限内不摘工具。

探针用例全部用显式 `now=` 传假时钟：真等 30 秒的测试没人会跑，而"用 sleep 验证
缓存"最后一定会变成被跳过的那条。
"""

from __future__ import annotations

import json

import pytest

from app import toolsets
from app.config import Settings
from app.main import migrate
from app.storage import Conversation, Operation, Store, Task
from app.tools import (
    TODO_MAX_CHARS,
    TODO_MAX_ITEMS,
    ToolRegistry,
    load_todos,
    todos_path,
)

# ── 夹具 ────────────────────────────────────────────────────────────────────


@pytest.fixture()
async def env(tmp_path):
    """隔离数据目录上的完整运行环境（settings + store + registry）。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="多步任务"))
    registry = ToolRegistry(settings, store)
    try:
        yield settings, store, registry, task.id
    finally:
        await store.engine.dispose()


async def _todo(registry: ToolRegistry, task_id: str, op: str, **args) -> dict:
    return await registry.execute(task_id, op, "todo", args)


# ── 1. todo ─────────────────────────────────────────────────────────────────


async def test_todo_add_list_and_reads_back_the_full_list(env):
    """增 + 读：每次调用都返回完整清单（清单不会被注入上下文，只能靠回读）。"""
    settings, _store, registry, task_id = env
    added = await _todo(registry, task_id, "add1", action="add", items=["读规格书", "写 patch 模块"])
    assert added["status"] == "ok"
    data = added["data"]
    assert data["count"] == 2 and data["pending"] == 2
    assert [item["text"] for item in data["items"]] == ["读规格书", "写 patch 模块"]
    assert data["items"][0]["index"] == 1
    # 渲染文本带序号与状态，模型可以直接照着念给用户
    assert "1. [ ] (待办) 读规格书" in data["rendered"]
    # 落盘位置就在约定的 <data_dir>/todos/<task_id>.json
    assert todos_path(settings.data_dir, task_id).is_file()


async def test_todo_state_survives_a_restart(env):
    """重启后状态还在：清单是磁盘上的一等文件，不是内存里的字典。"""
    settings, store, registry, task_id = env
    await _todo(registry, task_id, "add1", action="add", items=["第一步", "第二步"])
    # 模拟重启：换一个全新的 registry 实例（同一个数据目录 / 同一个库）
    restarted = ToolRegistry(settings, store)
    again = await _todo(restarted, task_id, "list1", action="list")
    assert [item["text"] for item in again["data"]["items"]] == ["第一步", "第二步"]
    assert load_todos(settings.data_dir, task_id)[0]["text"] == "第一步"


async def test_todo_update_complete_remove_and_clear(env):
    """改状态 / 勾掉 / 删掉 / 清空，四条都回读完整清单。"""
    _settings, _store, registry, task_id = env
    await _todo(registry, task_id, "add1", action="add", items=["甲", "乙", "丙"])

    updated = await _todo(registry, task_id, "upd1", action="update", index=2, status="in_progress")
    assert updated["data"]["items"][1]["status"] == "in_progress"
    assert "进行中" in updated["data"]["rendered"]

    done = await _todo(registry, task_id, "done1", action="complete", index=1)
    assert done["data"]["items"][0]["status"] == "done"
    assert done["data"]["count"] == 3 and done["data"]["pending"] == 2
    assert done["data"]["rendered"].startswith("1. [x] (已完成) 甲")

    removed = await _todo(registry, task_id, "rm1", action="remove", index=3)
    assert removed["data"]["count"] == 2
    assert [item["text"] for item in removed["data"]["items"]] == ["甲", "乙"]

    cleared = await _todo(registry, task_id, "clr1", action="clear")
    assert cleared["data"]["count"] == 0
    assert cleared["data"]["rendered"] == "（清单为空）"


async def test_todo_rejects_more_than_fifty_items(env):
    """单任务上限 50 条：超限**拒绝并说明**，不做静默截断。"""
    _settings, _store, registry, task_id = env
    full = [f"步骤 {index}" for index in range(TODO_MAX_ITEMS)]
    ok = await _todo(registry, task_id, "add1", action="add", items=full)
    assert ok["data"]["count"] == TODO_MAX_ITEMS

    over = await _todo(registry, task_id, "add2", action="add", items=["第 51 条"])
    assert over["status"] == "error"
    assert str(TODO_MAX_ITEMS) in over["error"]
    assert "complete" in over["error"] or "remove" in over["error"]  # 给出下一步怎么做


async def test_todo_rejects_an_item_over_200_chars(env):
    """单条 200 字符上限：超了拒绝，并建议拆小。"""
    _settings, _store, registry, task_id = env
    too_long = "很长的一步" * 60
    assert len(too_long) > TODO_MAX_CHARS
    result = await _todo(registry, task_id, "add1", action="add", items=[too_long])
    assert result["status"] == "error"
    assert str(TODO_MAX_CHARS) in result["error"]


async def test_todo_rejects_bad_action_and_bad_index(env):
    """错误参数要给出可执行的下一步，而不是笼统的"失败"。"""
    _settings, _store, registry, task_id = env
    await _todo(registry, task_id, "add1", action="add", items=["只有一条"])
    bad_action = await _todo(registry, task_id, "bad1", action="teleport")
    assert bad_action["status"] == "error" and "action" in bad_action["error"]
    bad_index = await _todo(registry, task_id, "bad2", action="complete", index=9)
    assert bad_index["status"] == "error" and "1-1" in bad_index["error"]


# ── 2. clarify ──────────────────────────────────────────────────────────────


OPTIONS = [
    {"label": "Markdown", "description": "便于版本管理"},
    {"label": "Word", "description": "便于对外交付"},
]


async def test_clarify_returns_needs_user_with_rendered_options(env):
    """`clarify` 的返回值必须是 `needs_user` 载荷，且选项已经渲染成文本。

    渲染这一步不是"顺手美化"：`nodes.py` 只把 `reason` / `message` / `tool` 搬进
    `waiting`，结构化 `options` 会被丢掉。所以任何前端（CLI / Web / gateway）能用到
    的那份就是 `message` 里的 `1) … 2) …`。
    """
    _settings, store, registry, task_id = env
    result = await registry.execute(
        task_id, "clarify1", "clarify", {"question": "报告用哪种格式？", "options": OPTIONS}
    )
    assert result["status"] == "needs_user"
    assert result["reason"] == "clarification"
    message = result["message"]
    assert "报告用哪种格式？" in message
    assert "1) Markdown —— 便于版本管理" in message
    assert "2) Word —— 便于对外交付" in message
    assert "回复序号即可" in message
    # 结构化字段仍然保留，富前端可以渲染成按钮
    assert [item["label"] for item in result["options"]] == ["Markdown", "Word"]
    # 落库状态是非终态（WAITING）：等用户回答，不能当"已完成"缓存住
    stored = await store.get(Operation, "clarify1")
    assert stored.status == "WAITING"


@pytest.mark.parametrize("count", [1, 7])
async def test_clarify_rejects_out_of_range_option_counts(env, count):
    """选项数量必须 2-6：1 个不是选择题，7 个该先合并。"""
    _settings, _store, registry, task_id = env
    result = await registry.execute(
        task_id,
        f"clarify-{count}",
        "clarify",
        {
            "question": "选一个",
            "options": [{"label": f"选项{index}"} for index in range(count)],
        },
    )
    assert result["status"] == "error"
    assert "2-6" in result["error"]


async def test_clarify_without_free_text_says_pick_one(env):
    """`allow_free_text=False` 时必须明说"只能选"，否则用户以为还能自由作答。"""
    _settings, _store, registry, task_id = env
    result = await registry.execute(
        task_id,
        "clarify-strict",
        "clarify",
        {"question": "走哪条路？", "options": OPTIONS, "allow_free_text": False},
    )
    assert result["status"] == "needs_user"
    assert "只能从上面选一个" in result["message"]
    assert "用自己的话" not in result["message"]
    assert result["allow_free_text"] is False


async def test_clarify_is_not_mistaken_for_a_normal_tool_result(env):
    """回归闸：`execute` 不能把 `needs_user` 包成 `{"status": "ok"}`。

    一旦包成 ok，`nodes.py` 就看不到 `needs_user`，用户永远不会被问到问题——
    模型会拿着一段"选项文本"自己选一个继续跑。这是这条通路最坏的失败方式：
    静默、且看起来一切正常。
    """
    _settings, _store, registry, task_id = env
    result = await registry.execute(
        task_id, "clarify-shape", "clarify", {"question": "确认吗？", "options": OPTIONS}
    )
    assert "data" not in result
    assert result["status"] == "needs_user"


# ── 3. check_fn（可用性探针）────────────────────────────────────────────────


def _fake_check(monkeypatch, outcome: dict, calls: dict):
    """把某个需求的检查函数换成可计数的假实现。"""

    def fake(settings=None):
        calls["n"] = calls.get("n", 0) + 1
        if outcome.get("ok", True):
            return True, ""
        return False, outcome.get("reason", "不可用")

    monkeypatch.setitem(toolsets.REQUIREMENT_CHECKS, "github_token", fake)
    return fake


@pytest.fixture(autouse=True)
def _clean_probe_cache():
    """每条用例前后都清缓存：探针缓存是进程级全局状态，串味会让用例互相影响。"""
    toolsets.invalidate_probe_cache()
    yield
    toolsets.invalidate_probe_cache()


def test_probe_marks_an_available_tool(monkeypatch):
    calls: dict = {}
    _fake_check(monkeypatch, {"ok": True}, calls)
    assert toolsets.probe("github_search_issues", None, now=0.0) == (True, "")
    assert calls["n"] == 1


def test_probe_reports_the_reason_when_unavailable(monkeypatch):
    calls: dict = {}
    _fake_check(monkeypatch, {"ok": False, "reason": "未配置 github_token"}, calls)
    available, reason = toolsets.probe("github_search_issues", None, now=0.0)
    assert available is False and "github_token" in reason


def test_probe_does_not_reprobe_within_the_30s_ttl(monkeypatch):
    """30 秒内重复询问只探一次——探针会读密钥、glob 目录，不能每轮都跑。"""
    calls: dict = {}
    _fake_check(monkeypatch, {"ok": True}, calls)
    toolsets.probe("github_search_issues", None, now=0.0)
    toolsets.probe("github_search_issues", None, now=10.0)
    toolsets.probe("github_search_issues", None, now=29.9)
    assert calls["n"] == 1, "TTL 内不该重复探测"
    toolsets.probe("github_search_issues", None, now=30.1)
    assert calls["n"] == 2, "TTL 过期后应重新探测"


def test_probe_grace_period_does_not_drop_the_tool(monkeypatch):
    """60 秒抖动宽限：上次成功不到 60s 时，这次失败按抖动处理，不摘工具。"""
    calls: dict = {}
    outcome: dict = {"ok": True}
    _fake_check(monkeypatch, outcome, calls)
    assert toolsets.probe("github_search_issues", None, now=0.0) == (True, "")
    outcome["ok"] = False
    outcome["reason"] = "token 掉了"
    # 40s：TTL 已过 → 重新探测 → 失败；但距上次成功 40s < 60s → 仍判可用
    assert toolsets.probe("github_search_issues", None, now=40.0) == (True, "")
    # 110s：距上次成功 110s > 60s → 这次失败是真的，缓存并报不可用
    available, reason = toolsets.probe("github_search_issues", None, now=110.0)
    assert available is False and reason == "token 掉了"


def test_cached_probe_never_triggers_a_real_check(monkeypatch):
    """`resolve()` 走的是只读缓存：没探测过就按可用，绝不因为"没探"而摘工具。"""
    calls: dict = {}
    _fake_check(monkeypatch, {"ok": False}, calls)
    assert toolsets.cached_probe("github_search_issues") == (True, "")
    assert calls.get("n", 0) == 0


def test_resolve_excludes_a_tool_only_after_a_negative_probe(monkeypatch):
    calls: dict = {}
    _fake_check(monkeypatch, {"ok": False, "reason": "未配置 github_token"}, calls)
    names = ["github_search_issues", "run_command"]
    # 还没探过 → 全暴露（宁可多暴露一个调不通的工具，也不静默摘掉）
    assert toolsets.resolve(names, "cli", None) == names
    toolsets.probe("github_search_issues", None, now=0.0)
    # 假时钟：让只读缓存看到"这条结论还很新鲜"
    assert toolsets.resolve(names, "cli", None, now=1.0) == ["run_command"]
    # 结论过期（>30s）后回到"未知 = 可用"，不会拿旧结论永久压制工具
    assert toolsets.resolve(names, "cli", None, now=99.0) == names


def test_describe_shows_the_unavailable_reason(monkeypatch):
    """`orbit tools list` 的"暴露"列要能说清为什么不可用。"""
    calls: dict = {}
    _fake_check(monkeypatch, {"ok": False, "reason": "未配置 github_token"}, calls)
    rows = {
        row["name"]: row
        for row in toolsets.describe("cli", None, ["github_search_issues"], probe_fresh=True)
    }
    row = rows["github_search_issues"]
    assert row["exposed"] is False
    assert "不可用" in row["reason"] and "github_token" in row["reason"]
    assert row["requirement"] == "github_token"


def test_requirement_lookup_handles_prefixes_and_exact_names():
    assert toolsets.requirement_of("browser_click") == "playwright"
    assert toolsets.requirement_of("notion_read_page") == "playwright"
    assert toolsets.requirement_of("github_get_issue") == "github_token"
    assert toolsets.requirement_of("run_command") == "shell"
    # 没有外部依赖的工具：空串 = 无条件可用，探针不该给它编需求
    assert toolsets.requirement_of("memory_search") == ""
    assert toolsets.requirement_of("todo") == ""


# ── 4. 命令面：`tools list` 的原因列 + `tools probe` ────────────────────────

import app.cli.commands.tools_extra  # noqa: E402,F401 —— 导入即把 probe 子命令挂到 `tools` 上
from app.cli.main import main  # noqa: E402


def _no_github_token(monkeypatch):
    monkeypatch.setitem(
        toolsets.REQUIREMENT_CHECKS, "github_token", lambda settings=None: (False, "未配置 github_token")
    )


def test_cli_list_shows_the_unavailable_reason(tmp_path, capsys, monkeypatch):
    """`orbit tools list` 的"暴露"列必须能说清"为什么不可用"（MATRIX 5.3 的交付面）。"""
    _no_github_token(monkeypatch)
    assert main(["--data-dir", str(tmp_path / "data"), "tools", "list"]) == 0
    out = capsys.readouterr().out
    row = next(line for line in out.splitlines() if line.startswith("github_search_issues"))
    assert "否" in row and "不可用" in row and "github_token" in row


def test_cli_tools_probe_reports_requirements(tmp_path, capsys, monkeypatch):
    """`orbit tools probe`：跑真探针，按工具列出需求与可用性。"""
    _no_github_token(monkeypatch)
    code = main(["--data-dir", str(tmp_path / "data"), "--json", "tools", "probe"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["probed"] >= 1
    rows = {row["name"]: row for row in payload["tools"]}
    assert rows["github_search_issues"]["requirement"] == "github_token"
    assert rows["github_search_issues"]["available"] is False
    assert "github_search_issues" in payload["unavailable"]
