"""密钥脱敏**接线**的契约测试。

`app/redact.py` 本身有测试，但"有实现"和"生效"是两件事——上一轮 MATRIX 里 14.3
就因此停在 🟡：15 类模式齐全、用例齐全，可是 `redact` 在 `tools.py` / `policies.py`
里 **0 命中**，模型和数据库拿到的仍然是明文。

这个文件只锁两件事：
1. `as_tool_text`（模型侧唯一漏斗）真的脱敏，且输出仍是合法 JSON
2. `DefaultToolResultPolicy.record`（落库侧）真的脱敏

两条出口都要锁，因为它们是独立的：只堵一条，另一条照样漏。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app.config import Settings
from app.main import migrate
from app.policies import DefaultToolResultPolicy
from app.storage import Conversation, Store, Task, ToolResult
from app.tools import as_tool_text

#: 一个形状正确的假 OpenAI key（不是真凭证，但能命中模式）。
SECRET = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"


# ── 模型侧：as_tool_text ───────────────────────────────────────────────────


def test_as_tool_text_redacts_a_secret():
    text = as_tool_text({"stdout": f"OPENAI_API_KEY={SECRET}", "ok": True})
    assert SECRET not in text
    # 保留首 4 末 4：够确认"是哪把钥匙"，不够复原。
    assert "sk-p" in text and "6789" in text


def test_as_tool_text_stays_valid_json():
    """先脱敏 dict 再序列化，所以结构不会被替换动作改坏。"""
    payload = {"stdout": f"TOKEN={SECRET}", "nested": {"list": [SECRET, 1, True, None]}}
    text = as_tool_text(payload)
    parsed = json.loads(text)
    assert parsed["nested"]["list"][1:] == [1, True, None]


def test_as_tool_text_redacts_nested_values():
    text = as_tool_text({"files": [{"content": f"key={SECRET}"}]})
    assert SECRET not in text


def test_as_tool_text_leaves_harmless_text_alone():
    """假阳性回归：普通内容不该被动。"""
    payload = {
        "uuid": "550e8400-e29b-41d4-a716-446655440000",
        "time": "2026-10-28T00:12:33+00:00",
        "url": "https://example.com/path?a=1&b=2",
        "text": "这是一段正常的中文说明。",
    }
    assert json.loads(as_tool_text(payload)) == payload


def test_as_tool_text_survives_non_string_values():
    """工具结果里可能有 int / bool / None / 嵌套 list，别在类型上炸。"""
    payload = {"code": 0, "ok": True, "ratio": 0.5, "nothing": None, "items": [[1, 2]]}
    assert json.loads(as_tool_text(payload)) == payload


# ── 落库侧：record ─────────────────────────────────────────────────────────


@pytest.fixture()
async def store(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    instance = Store(settings.data_dir / "app.sqlite")
    yield instance
    await instance.engine.dispose()


async def test_record_persists_redacted_text(store):
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="跑个命令"))
    await DefaultToolResultPolicy().record(
        store,
        task_id=task.id,
        operation_id="op-1",
        name="run_command",
        status="ok",
        host="local",
        text=f"$ env\nOPENAI_API_KEY={SECRET}\nPATH=/usr/bin",
    )
    rows = await store.rows(select(ToolResult).where(ToolResult.operation_id == "op-1"))
    assert len(rows) == 1
    stored = rows[0].text
    assert SECRET not in stored
    assert "sk-p" in stored and "6789" in stored
    # 非密钥内容不该被动。
    assert "PATH=/usr/bin" in stored
