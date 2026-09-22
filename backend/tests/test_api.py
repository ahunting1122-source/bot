from fastapi.testclient import TestClient
from sqlalchemy import text

from app.config import Settings
from app.main import create_app
from app.storage import Store


async def test_store_runs_in_wal_mode(tmp_path):
    # 没有 WAL 时，SSE 的读事务会和任务写入抢锁，写操作报 database is locked。
    store = Store(tmp_path / "app.sqlite")
    await store.tune()
    async with store.sessions() as session:
        mode = (await session.execute(text("PRAGMA journal_mode"))).scalar()
    assert str(mode).lower() == "wal"
    assert store.wal_enabled is True
    await store.engine.dispose()


def test_conversation_task_and_memory_persist(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    application = create_app(settings, run_agent=False)
    with TestClient(application) as client:
        client.get("/api/session")
        assert client.get("/api/health").json()["status"] == "ok"
        conversation = client.post("/api/conversations").json()
        response = client.post(
            f"/api/conversations/{conversation['id']}/messages", json={"content": "读取项目笔记"}
        )
        assert response.status_code == 202
        task_id = response.json()["id"]
        assert client.get(f"/api/tasks/{task_id}").json()["goal"] == "读取项目笔记"
        client.post("/api/memories", json={"key": "report_format", "value": "Markdown"})
        client.post("/api/memories", json={"key": "report_format", "value": "中文 Markdown"})
        assert len(client.get("/api/memories").json()) == 1
    with TestClient(create_app(settings, run_agent=False)) as client:
        client.get("/api/session")
        assert client.get("/api/conversations").json()[0]["id"] == conversation["id"]
        assert client.get("/api/memories").json()[0]["value"] == "中文 Markdown"
