"""`mount_into_app` 在**真 app** 上真的挂上（MATRIX 13.8 的验收判据）。

## 为什么这条测试必须存在

监视子agent 第 12 轮抓到一个过度声称：`build_router()` 机制是真的、11 条测试也过，
但那些测试挂的是 **`FastAPI()` 测试自己的 app**，而 `app/main.py` 对
`plugin_dashboard` / `mount_into_app` **零命中**。

也就是说：**测试全绿，生产里 `/api/plugins/<name>/…` 会 404。**

这是最坏的一类假绿——机制对、测试对、结论错。所以这条测试不用测试自己的 app，
而是从 `create_app()` 拿**真 app**，装一个真插件，然后请求它。

判据只有一句：**真 app 上那条路由返回 200。**

`/api/*` 有本地会话中间件，所以先 `GET /api/session` 拿 cookie——否则拿到的是 401，
**401 和 404 是两件事**：401 说明路由在（被鉴权挡住），404 说明根本没挂上。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

MANIFEST = {
    "name": "p-real",
    "label": "Real",
    "description": "真 app 挂载验证用",
    "icon": "Package",
    "version": "1.0.0",
    "tab": {"path": "/real", "position": "after:skills"},
    "entry": "dist/index.js",
    "css": "dist/style.css",
    "api": "plugin_api.py",
}

API_SOURCE = '''
from fastapi import APIRouter

router = APIRouter()


@router.get("/ping")
def ping():
    return {"pong": True}
'''


@pytest.fixture()
def client(tmp_path):
    """真 `create_app()` + 已装好的一个 dashboard 插件 + 已认证的客户端。"""
    from app.config import Settings
    from app.main import create_app

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()

    root = settings.data_dir / "plugins" / "p-real"
    (root / "dist").mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(json.dumps(MANIFEST, ensure_ascii=False), encoding="utf-8")
    (root / "dist" / "index.js").write_text("export default {};", encoding="utf-8")
    (root / "dist" / "style.css").write_text(".x{}", encoding="utf-8")
    (root / "plugin_api.py").write_text(API_SOURCE, encoding="utf-8")

    with TestClient(create_app(settings, run_agent=False)) as http:
        http.get("/api/session")  # 拿本地会话 cookie，否则 /api/* 全是 401
        yield http


def test_plugin_route_is_200_on_the_real_app(client):
    """**核心判据**：真 app 上 `/api/plugins/p-real/ping` 返回 200 且内容对。

    这条测试在 `mount_into_app` 没接进 `main.py` 时会 404——所以它真的能红。
    """
    from app.plugin_dashboard import MOUNTS

    response = client.get("/api/plugins/p-real/ping")
    assert response.status_code != 404, (
        "真 app 上插件路由 404——说明 `main.py` 没有调 `mount_into_app(app)`"
    )
    assert response.status_code == 200, f"期望 200，得到 {response.status_code}"
    assert response.json() == {"pong": True}
    # 报告要跟着事实走
    assert MOUNTS.as_dict()["mounted_plugins"] >= 1


def test_main_py_actually_calls_mount_into_app():
    """源码层机械证据：`main.py` 里必须出现 `mount_into_app`。

    行为测试可能因为环境差异跳过（比如插件目录读不到），而这条不会——
    它直接回答「那一行在不在」。两者都要，缺一条就会有一类错误漏网。
    """
    from app import main as main_module

    source = Path(main_module.__file__).read_text(encoding="utf-8")
    assert "mount_into_app" in source, "`main.py` 没有调用 `mount_into_app`"
    assert "plugin_dashboard" in source, "`main.py` 没有引用 `plugin_dashboard`"
