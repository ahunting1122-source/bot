"""`kanban_attach_url`（`5.14` 看板家族最后一个工具，第 70 轮）。

## 为什么它现在能做

第 66 轮证明网络是通的；而 `httpx` 本来就装在本仓 —— **这个工具不需要任何新依赖**。

## 三件必须验的事

1. **能下载并落盘**（用**本地 HTTP server**，不依赖外网）；
2. **云元数据 URL 必须被拒** —— 「按 URL 下载」是最经典的 SSRF 入口；
3. **大小上限要在下载过程中生效** —— 先下完再判等于让人把内存打满。
"""

from __future__ import annotations

import http.server
import threading

import pytest


@pytest.fixture()
def board(tmp_path):
    from app import kanban, tool_kanban

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    tool_kanban.configure(data_dir)
    yield kanban, data_dir
    tool_kanban.reset()


@pytest.fixture()
def local_server():
    """起一个只服务内存里那几个路径的本地 HTTP server。"""
    payloads: dict[str, bytes] = {"/note.txt": b"downloaded!", "/big.bin": b"x" * 200_000}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 —— http.server 的约定
            body = payloads.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # 静默，别污染测试输出
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", payloads
    finally:
        server.shutdown()
        server.server_close()


def test_it_downloads_and_attaches(board, local_server):
    """**真的下载并落盘**——不是只记一条「有附件」。"""
    kanban, data_dir = board
    base, payloads = local_server
    card = kanban.create(data_dir, "按 URL 挂")

    attached = kanban.attach_url(data_dir, card.id, f"{base}/note.txt")

    assert len(attached.attachments) == 1
    item = attached.attachments[0]
    assert item["name"] == "note.txt", "名字该从 URL 路径取"
    assert item["bytes"] == len(payloads["/note.txt"])
    from pathlib import Path

    assert (Path(data_dir) / item["path"]).read_bytes() == payloads["/note.txt"]


def test_an_explicit_name_wins(board, local_server):
    kanban, data_dir = board
    base, _ = local_server
    card = kanban.create(data_dir, "改名")
    attached = kanban.attach_url(data_dir, card.id, f"{base}/note.txt", name="mine.txt")
    assert attached.attachments[0]["name"] == "mine.txt"


def test_cloud_metadata_urls_are_refused_before_any_download(board):
    """**「按 URL 下载」是最经典的 SSRF 入口** —— 必须在下任何字节之前拒。

    否则攻击者让 URL 指向 `169.254.169.254`，就能把云实例凭证下成「附件」再读出来。
    """
    kanban, data_dir = board
    card = kanban.create(data_dir, "SSRF")
    with pytest.raises(ValueError) as excinfo:
        kanban.attach_url(data_dir, card.id, "http://169.254.169.254/latest/meta-data")
    assert "云元数据" in str(excinfo.value)
    # **卡片上不该留下任何记录**
    assert kanban.show(data_dir, card.id).attachments == []


def test_the_size_cap_stops_during_download_not_after(board, local_server, monkeypatch):
    """**上限要在下载过程中生效** —— 先下完再判等于让人把内存打满。

    这里把上限压到 1000 字节，让 200KB 的响应**必然**触发「下载中停」。
    """
    from app import kanban as kanban_module

    kanban, data_dir = board
    base, _ = local_server
    monkeypatch.setattr(kanban_module, "MAX_ATTACHMENT_BYTES", 1000)
    card = kanban.create(data_dir, "太大")

    with pytest.raises(kanban.KanbanError) as excinfo:
        kanban.attach_url(data_dir, card.id, f"{base}/big.bin")
    assert "下载过程中就停" in str(excinfo.value), str(excinfo.value)
    assert kanban.show(data_dir, card.id).attachments == []


async def test_the_tool_dispatches(board, local_server):
    from app import tool_kanban

    _kanban, _data_dir = board
    base, _ = local_server
    created = await tool_kanban.execute("kanban_create", {"title": "走工具"}, "t", "o1")
    card_id = created["card"]["id"]

    result = await tool_kanban.execute(
        "kanban_attach_url", {"id": card_id, "url": f"{base}/note.txt"}, "t", "o2"
    )
    assert result["ok"] is True
    assert result["card"]["attachments"][0]["name"] == "note.txt"


def test_production_registers_fourteen_kanban_tools(tmp_path):
    """**看板家族 14/14 全部注册**（生产装配后实测）。"""
    from starlette.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    settings = Settings(data_dir=tmp_path / "d", workspace=tmp_path / "w")
    settings.prepare()
    app = create_app(settings=settings)
    with TestClient(app):
        names = set(app.state.kanban_tools)
        assert len(names) == 14, sorted(names)
        assert "kanban_attach_url" in names
