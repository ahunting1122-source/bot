"""`orbit insights` 与 `orbit monitoring status` 的契约测试。

这两条命令都是**只读聚合**，所以测试重点是"口径对不对"而不是"功能有没有"：
一个算错的成功率或 token 归因，比命令不存在更糟——它会被人当真。

已知的真实口径（本机实测，写进测试以免回退）：
- `tool_calls.status` 取值是 `DONE` / `RUNNING`，**只记控制流不记成败**
- `tool_results.status` 取值是 `ok` / `error`，**成败看这张表**
第一版把两者搞混，把 97 次调用全算成失败（失败率 100%）。这里锁住正确口径。
"""

from __future__ import annotations

import json

from app.cli.main import main
from app.config import Settings
from app.main import migrate
from app.storage import Conversation, Message, Operation, Store, Task, ToolResult


async def _seed(tmp_path):
    """造一个最小但真实形状的库：1 个成功任务 + 1 个失败任务 + 2 次工具调用。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")

    conversation = await store.save(Conversation(title="测试会话"))
    await store.save(Message(conversation_id=conversation.id, role="user", content="查一下"))
    ok_task = await store.save(Task(conversation_id=conversation.id, goal="成功的任务"))
    bad_task = await store.save(Task(conversation_id=conversation.id, goal="失败的任务"))

    await store.transition(ok_task.id, "SUCCEEDED")
    await store.transition(bad_task.id, "FAILED", error="模型超时")

    await store.save(
        Operation(id="op-1", task_id=ok_task.id, name="run_command", status="DONE", args={})
    )
    await store.save(
        Operation(id="op-2", task_id=ok_task.id, name="web_search", status="DONE", args={})
    )
    await store.save(
        ToolResult(
            operation_id="op-1",
            task_id=ok_task.id,
            name="run_command",
            status="ok",
            host="",
            text="hello",
            bytes=5,
            tokens=2,
            hash="a",
        )
    )
    await store.save(
        ToolResult(
            operation_id="op-2",
            task_id=ok_task.id,
            name="web_search",
            status="error",
            host="example.com",
            text="boom",
            bytes=4,
            tokens=1,
            hash="b",
        )
    )
    await store.event(ok_task.id, "task_completed", token_usage={"prompt_tokens": 10, "completion_tokens": 5})
    await store.engine.dispose()
    return settings


async def test_insights_counts_success_and_failures_correctly(tmp_path, capsys):
    await _seed(tmp_path)
    assert main(["--data-dir", str(tmp_path / "data"), "--json", "insights"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"] == 2
    assert payload["status_counts"] == {"SUCCEEDED": 1, "FAILED": 1}
    assert payload["success_rate"] == 50.0
    assert payload["tool_calls"] == 2
    # 关键口径：失败数来自 tool_results.status，不是 tool_calls.status。
    assert payload["tool_errors"] == 1
    assert payload["tool_tokens"] == 3
    assert payload["model_tokens"] == 15


async def test_insights_source_filter_uses_host(tmp_path, capsys):
    await _seed(tmp_path)
    assert main(["--data-dir", str(tmp_path / "data"), "--json", "insights", "--source", "example"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tasks"] == 1
    assert payload["hosts"] == [{"host": "example.com", "count": 1}]

    capsys.readouterr()
    assert main(["--data-dir", str(tmp_path / "data"), "--json", "insights", "--source", "nope"]) == 0
    assert json.loads(capsys.readouterr().out)["tasks"] == 0


async def test_monitoring_status_is_content_free(tmp_path, capsys):
    await _seed(tmp_path)
    assert main(["--data-dir", str(tmp_path / "data"), "--json", "monitoring", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["export"]["configured"] is False
    assert payload["export"]["content_free"] is True
    assert payload["health"]["完整性"] == "ok"
    # 不许把内容带进导出物：整个 payload 里不该出现消息正文或工具结果正文。
    blob = json.dumps(payload, ensure_ascii=False)
    for secret in ("查一下", "hello", "boom"):
        assert secret not in blob


def test_insights_requires_a_database(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "empty"), "insights"])
    assert code == 1
    assert "数据库不存在" in capsys.readouterr().err
