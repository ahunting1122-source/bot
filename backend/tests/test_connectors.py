import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest

from app.browser import BROWSER_TOOLS, BrowserController
from app.config import Settings
from app.connectors import GitHubConnector
from app.main import migrate
from app.storage import Conversation, Store, Task
from app.tools import ToolRegistry

HTML = b"""<!doctype html><title>Test notes</title>
<div class="notion-page-content"><div contenteditable="true" aria-label="Notes">Initial note</div></div>
<script>const editor=document.querySelector('[contenteditable]');
editor.innerHTML=localStorage.getItem('notes')||'Initial note';
document.addEventListener('input',()=>localStorage.setItem('notes',editor.innerHTML));</script>"""


@pytest.fixture
def web_page():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}/"
    server.shutdown()
    server.server_close()
    worker.join()


async def test_github_read():
    def response(request):
        assert "repo:example/project" in request.url.params["q"]
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "number": 1,
                        "title": "Improve docs",
                        "state": "open",
                        "html_url": "https://github.com/example/project/issues/1",
                        "body": "Details",
                    }
                ]
            },
        )

    connector = GitHubConnector(
        Settings(github_repo="example/project"),
        httpx.AsyncClient(transport=httpx.MockTransport(response)),
    )
    result = await connector.execute("github_search_issues", {"query": "docs"}, "task", "op")
    assert result["data"][0]["number"] == 1
    await connector.close()


async def test_browser_append_and_reload(tmp_path, web_page):
    settings = Settings(
        data_dir=tmp_path / "data", workspace=tmp_path / "files", notion_url=web_page, headless=True
    )
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    browser = BrowserController(settings, store)
    registry = ToolRegistry(settings, store)
    registry.register(BROWSER_TOOLS, browser.execute)
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="追加摘要"))
    try:
        result = await registry.execute(task.id, "read", "notion_read_page", {})
        assert result["status"] == "ok"
        assert result["data"]["elements"]
        result = await registry.execute(
            task.id, "append", "notion_append_summary", {"content": "Verified summary"}
        )
        assert result["status"] == "ok", result
        await registry.execute(
            task.id, "append", "notion_append_summary", {"content": "Verified summary"}
        )
        assert (await browser.page.locator("body").inner_text()).count("Verified summary") == 1
    finally:
        await browser.close()
        await store.engine.dispose()
