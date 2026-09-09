import time

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from app.config import Settings
from app.main import create_app


class LocalWorkflowModel:
    def bind_tools(self, *args, **kwargs):
        return self

    async def ainvoke(self, messages):
        completed = [m for m in messages if m.type == "tool"]
        if not completed:
            return AIMessage(
                content="读取笔记。",
                tool_calls=[
                    {
                        "name": "local_read_document",
                        "args": {"path": "notes.md"},
                        "id": "read",
                        "type": "tool_call",
                    }
                ],
            )
        if len(completed) == 1:
            assert "FastAPI" in completed[0].content
            return AIMessage(
                content="保存摘要。",
                tool_calls=[
                    {
                        "name": "local_write_report",
                        "args": {
                            "path": "reports/summary.md",
                            "content": "# 摘要\n项目使用 FastAPI。",
                        },
                        "id": "write",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="报告已保存到 reports/summary.md。")


def test_api_agent_report_and_event_replay(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    (settings.workspace / "notes.md").write_text("项目使用 FastAPI。", encoding="utf-8")
    application = create_app(settings)
    with TestClient(application) as client:
        application.state.runner.model = LocalWorkflowModel()
        client.get("/api/session")
        conversation = client.post("/api/conversations").json()
        task = client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={"content": "读取笔记并生成报告"},
        ).json()
        for _ in range(100):
            current = client.get(f"/api/tasks/{task['id']}").json()
            if current["status"] in {"SUCCEEDED", "FAILED"}:
                break
            time.sleep(0.05)
        assert current["status"] == "SUCCEEDED", current
        artifact = current["artifacts"][0]
        assert "FastAPI" in client.get(f"/api/artifacts/{artifact['id']}").text
        replay = client.get(f"/api/tasks/{task['id']}/events").text
        assert "tool_finished" in replay and "task_completed" in replay
        other = client.post("/api/conversations").json()
        assert client.get(f"/api/conversations/{other['id']}/messages").json() == []
