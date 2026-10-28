"""dashboard docs / files / webhooks 三页后端的契约测试（MATRIX 16.1）。

这三条路由都碰**用户可控路径**，所以大部分用例是安全用例而不是功能用例：
路径逃逸、Windows 保留名、ADS、越界读取。功能对了但校验漏了，等于给了一个
任意文件读取口子。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path: Path):
    from app.config import Settings
    from app.main import create_app, migrate

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    project = tmp_path
    (project / "handoff").mkdir(exist_ok=True)
    (project / "handoff" / "note.md").write_text("# 说明\n内容", encoding="utf-8")
    (project / "handoff" / "blob.bin").write_bytes(b"\x00\x01")
    (settings.data_dir / "skills").mkdir(parents=True, exist_ok=True)
    (settings.data_dir / "skills" / "SKILL.md").write_text("# 技能", encoding="utf-8")
    with TestClient(create_app(settings, run_agent=False)) as http:
        # `/api/*` 有本地会话中间件：先拿一次会话 cookie，否则全是 401。
        http.get("/api/session")
        yield http


# ── docs ───────────────────────────────────────────────────────────────────


def test_docs_lists_markdown_only(client):
    payload = client.get("/api/docs").json()
    paths = [item["path"] for item in payload["items"]]
    assert "note.md" in paths
    assert "SKILL.md" in paths
    # 二进制不列出来——列了再报错是更差的体验
    assert "blob.bin" not in paths


def test_docs_reads_markdown(client):
    payload = client.get("/api/docs/read", params={"source": "项目文档", "path": "note.md"}).json()
    assert "说明" in payload["text"]


def test_docs_rejects_path_traversal(client):
    response = client.get("/api/docs/read", params={"source": "项目文档", "path": "../../secret.txt"})
    assert response.status_code == 400
    assert ".." in response.json()["detail"]


def test_docs_rejects_absolute_path(client):
    response = client.get("/api/docs/read", params={"source": "项目文档", "path": "C:/Windows/win.ini"})
    assert response.status_code == 400


def test_docs_rejects_windows_reserved_name(client):
    response = client.get("/api/docs/read", params={"source": "项目文档", "path": "CON.md"})
    assert response.status_code == 400
    assert "保留名" in response.json()["detail"]


def test_docs_rejects_alternate_data_stream(client):
    response = client.get("/api/docs/read", params={"source": "项目文档", "path": "note.md:stream"})
    assert response.status_code == 400


def test_docs_unknown_source_is_404(client):
    assert client.get("/api/docs/read", params={"source": "不存在", "path": "x.md"}).status_code == 404


# ── files ──────────────────────────────────────────────────────────────────


def test_files_lists_workspace(client):
    payload = client.get("/api/files").json()
    names = {item["name"] for item in payload["items"]}
    assert "handoff" in names


def test_files_respects_depth(client):
    shallow = client.get("/api/files", params={"depth": 1}).json()
    deep = client.get("/api/files", params={"depth": 3}).json()
    assert len(deep["items"]) >= len(shallow["items"])


def test_files_rejects_escape(client):
    assert client.get("/api/files", params={"path": "../.."}).status_code == 400


# ── webhooks ───────────────────────────────────────────────────────────────


def test_webhook_register_list_delete_round_trip(client):
    assert client.post("/api/webhooks", json={"id": "hook1", "label": "测试", "prompt": "跑一下"}).json()["ok"]
    items = client.get("/api/webhooks").json()["items"]
    assert [item["id"] for item in items] == ["hook1"]
    assert client.delete("/api/webhooks/hook1").json()["ok"]
    assert client.get("/api/webhooks").json()["items"] == []


def test_webhook_secret_is_never_echoed(client):
    client.post("/api/webhooks", json={"id": "h2", "secret": "s3cr3t"})
    items = client.get("/api/webhooks").json()["items"]
    assert items[0]["has_secret"] is True
    assert "s3cr3t" not in json.dumps(items)


def test_webhook_receive_writes_inbox(client, tmp_path):
    client.post("/api/webhooks", json={"id": "h3"})
    response = client.post("/api/webhooks/h3", json={"hello": "world"})
    assert response.status_code == 200
    assert response.json()["accepted"] is True
    log = tmp_path / "data" / "webhooks" / "h3.jsonl"
    assert log.exists()
    assert "hello" in log.read_text(encoding="utf-8")


def test_webhook_secret_mismatch_is_401(client):
    client.post("/api/webhooks", json={"id": "h4", "secret": "right"})
    assert client.post("/api/webhooks/h4", json={}).status_code == 401
    assert client.post("/api/webhooks/h4", json={}, headers={"x-orbit-secret": "right"}).status_code == 200


def test_webhook_unknown_id_is_404(client):
    assert client.post("/api/webhooks/nope", json={}).status_code == 404


def test_duplicate_webhook_id_is_409(client):
    client.post("/api/webhooks", json={"id": "dup"})
    assert client.post("/api/webhooks", json={"id": "dup"}).status_code == 409
