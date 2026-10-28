"""dashboard 插件路由**真挂载**（MATRIX 13.8）的契约测试。

上一轮 ``routes_for()`` 只能把源码里的 ``@router.get(...)`` 翻译成 ``/api/plugins/<name>/…``
的**描述**，``mounted`` 恒为 ``False``——因为没有任何挂载点。本轮补上 ``build_router()`` /
``mount()`` / ``mount_into_app()``，于是"插件的路由到底通不通"第一次可以被真的请求验证。

这份测试钉住五件事：

1. **真的通**：``build_router`` 挂上之后 ``/api/plugins/<name>/…`` 真的 200（含路径参数）；
2. **隔离**：插件路由抛异常只影响它自己那一条（500 + 插件名），同插件的健康路由与其它插件
   都照常 200——一个第三方插件炸了不该拖垮整个 app；
3. **准入**：manifest 不完整 / ``api`` 里没有模块级 ``router`` / import 就炸 —— **一律不挂**，
   并且**原因进报告**（``MOUNTS.skipped``），不许静默 404；
4. **描述跟着事实走**：挂过之后 ``describe()`` 报 ``mounted=True`` + 挂了几条路由，
   ``PageRegistry.as_dict()`` 的 ``mounted`` 数得出来；
5. **"不 import" 这条限制没被破坏**：报告路径（``routes_for``）依然只读源码。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import plugin_dashboard as dash

#: 一份照 research §1.7 的真实形状写的 manifest（8 必填 + css）。
MANIFEST = {
    "name": "kanban",
    "label": "Kanban",
    "description": "Multi-agent collaboration board",
    "icon": "Package",
    "version": "1.0.0",
    "tab": {"path": "/kanban", "position": "after:skills"},
    "entry": "dist/index.js",
    "css": "dist/style.css",
    "api": "plugin_api.py",
}

#: 真实 ``plugin_api.py`` 的硬契约：模块级 router + 不依赖 dashboard 也能 import。
API_SOURCE = '''
"""Kanban dashboard plugin — backend API routes."""
from fastapi import APIRouter, HTTPException

router = APIRouter()


@router.get("/items")
def items():
    return [{"id": "a"}, {"id": "b"}]


@router.post("/items/{item_id}/move")
def move(item_id: str):
    return {"moved": item_id}


@router.get("/boom")
def boom():
    raise RuntimeError("插件内部炸了")


@router.get("/missing")
def missing():
    raise HTTPException(404, "没有这条")
'''


@pytest.fixture(autouse=True)
def _isolate_mounts():
    """每个用例都从"一条都没挂"开始（MOUNTS 是进程级的）。"""
    dash.MOUNTS.clear()
    yield
    dash.MOUNTS.clear()


def write_dashboard(
    root: Path,
    name: str = "kanban",
    *,
    manifest: dict | None = None,
    api: str = API_SOURCE,
) -> dash.DashboardManifest:
    """按 Hermes 布局落一个 dashboard 插件，并返回解析好的 manifest。"""
    directory = root / name / "dashboard"
    (directory / "dist").mkdir(parents=True, exist_ok=True)
    (directory / "dist" / "index.js").write_text("export default {};\n", encoding="utf-8")
    (directory / "dist" / "style.css").write_text("/* x */\n", encoding="utf-8")
    (directory / "plugin_api.py").write_text(api, encoding="utf-8")
    payload = dict(manifest if manifest is not None else MANIFEST)
    payload.setdefault("name", name)
    (directory / "manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    return dash.load_manifest(directory / "manifest.json")


def registry_with(*manifests: dash.DashboardManifest) -> dash.PageRegistry:
    registry = dash.PageRegistry()
    for manifest in manifests:
        registry.add(manifest)
    return registry


def client_for(router) -> TestClient:
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ── 1. 真的通 ───────────────────────────────────────────────────────────────


def test_build_router_makes_plugin_routes_reachable(tmp_path):
    """挂上之后 ``/api/plugins/<name>/…`` 真的 200——这是"真挂载"的唯一证据。"""
    manifest = write_dashboard(tmp_path)
    router = dash.build_router(registry_with(manifest))
    client = client_for(router)

    listing = client.get("/api/plugins/kanban/items")
    assert listing.status_code == 200
    assert listing.json() == [{"id": "a"}, {"id": "b"}]
    # 路径参数也要通：包装函数必须保住原始签名，否则 FastAPI 解析不到 item_id
    moved = client.post("/api/plugins/kanban/items/x1/move")
    assert moved.status_code == 200
    assert moved.json() == {"moved": "x1"}
    # 没挂的插件前缀仍然 404（没有"整片 /api/plugins/* 被吃掉"这种事）
    assert client.get("/api/plugins/other/items").status_code == 404


def test_mount_into_app_includes_the_router(tmp_path):
    """``mount_into_app(app)`` 就是 main.py 要粘的那一行。"""
    manifest = write_dashboard(tmp_path)
    app = FastAPI()
    report = dash.mount_into_app(app, registry_with(manifest))

    assert report["included"] is True
    assert report["mounted_plugins"] == 1
    assert TestClient(app).get("/api/plugins/kanban/items").status_code == 200


# ── 2. 隔离：一个插件炸了不拖垮别的 ─────────────────────────────────────────


def test_plugin_exception_only_breaks_its_own_route(tmp_path):
    """插件路由抛异常 → 500 + 插件名；同插件的健康路由与其它插件照常 200。"""
    broken = write_dashboard(tmp_path / "a", "kanban")
    healthy = write_dashboard(
        tmp_path / "b",
        "achievements",
        manifest={
            **MANIFEST,
            "name": "achievements",
            "label": "Achievements",
            "icon": "Star",
            "tab": {"path": "/achievements", "position": "after:analytics"},
        },
    )
    client = client_for(dash.build_router(registry_with(broken, healthy)))

    boom = client.get("/api/plugins/kanban/boom")
    assert boom.status_code == 500
    assert "kanban" in boom.text, "500 必须点名是哪个插件"
    assert "插件内部炸了" in boom.text
    # 同一插件的健康路由不受影响
    assert client.get("/api/plugins/kanban/items").status_code == 200
    # 别的插件也不受影响
    assert client.get("/api/plugins/achievements/items").status_code == 200


def test_plugin_http_exception_is_passed_through(tmp_path):
    """插件主动抛的 404 是正常语义，不许被改写成 500。"""
    manifest = write_dashboard(tmp_path)
    client = client_for(dash.build_router(registry_with(manifest)))
    response = client.get("/api/plugins/kanban/missing")
    assert response.status_code == 404
    assert "没有这条" in response.text


def test_import_failure_does_not_stop_other_plugins(tmp_path):
    """一个插件的 ``plugin_api.py`` import 就炸：它不挂，别的插件照挂。"""
    broken = write_dashboard(tmp_path / "a", "kanban", api="raise RuntimeError('import 阶段就炸')\n")
    healthy = write_dashboard(
        tmp_path / "b",
        "achievements",
        manifest={**MANIFEST, "name": "achievements", "icon": "Star"},
    )
    router = dash.build_router(registry_with(broken, healthy))
    client = client_for(router)

    assert client.get("/api/plugins/kanban/items").status_code == 404
    assert client.get("/api/plugins/achievements/items").status_code == 200
    assert "kanban" in dash.MOUNTS.skipped()
    assert "import" in dash.MOUNTS.skipped()["kanban"]


# ── 3. 准入：不过校验就不挂，且原因进报告 ──────────────────────────────────


def test_incomplete_manifest_is_not_mounted(tmp_path):
    """manifest 缺字段 → **不挂**（不是挂上一个半成品），原因写进报告。"""
    manifest = write_dashboard(
        tmp_path, manifest={key: value for key, value in MANIFEST.items() if key != "icon"}
    )
    router = dash.build_router(registry_with(manifest))

    assert client_for(router).get("/api/plugins/kanban/items").status_code == 404
    assert manifest.name in dash.MOUNTS.skipped()
    reason = dash.MOUNTS.skipped()[manifest.name]
    assert "校验未通过" in reason
    assert "icon" in reason, "被拒原因必须点名缺哪个字段"
    assert dash.MOUNTS.get(manifest.name) is None


def test_api_without_module_level_router_is_not_mounted(tmp_path):
    manifest = write_dashboard(tmp_path, api="def items():\n    return []\n")
    router = dash.build_router(registry_with(manifest))

    assert client_for(router).get("/api/plugins/kanban/items").status_code == 404
    assert "router" in dash.MOUNTS.skipped()[manifest.name]


def test_mount_requires_validate_and_is_auditable(tmp_path, caplog):
    """``mount()`` 是显式入口：日志里点名"即将 import 谁"，报告里给出挂了什么。"""
    manifest = write_dashboard(tmp_path)
    with caplog.at_level("WARNING", logger="orbit.plugin_dashboard"):
        report = dash.mount(registry_with(manifest))

    assert report["mounted_plugins"] == 1
    assert report["mounted_routes"] == 4, report
    assert report["mounted"][0]["plugin"] == "kanban"
    assert "plugin_api.py" in report["mounted"][0]["api"]
    audit = " ".join(record.getMessage() for record in caplog.records)
    assert "kanban" in audit and "第三方代码" in audit, "挂载必须留下可审计的日志"


# ── 4. 描述跟着事实走 ───────────────────────────────────────────────────────


def test_describe_reports_mounted_after_mount(tmp_path):
    manifest = write_dashboard(tmp_path)
    registry = registry_with(manifest)

    before = dash.describe(manifest)
    assert before["mounted"] is False and before["state"] == "partial"

    dash.build_router(registry)
    after = dash.describe(manifest)
    assert after["mounted"] is True
    assert after["state"] == "mounted"
    assert after["mounted_routes"] == 4
    assert all(path.startswith("/api/plugins/kanban/") for path in after["mounted_paths"])
    # 登记表的计数必须跟着变（上一轮这里恒为 0）
    snapshot = registry.as_dict()
    assert snapshot["mounted"] == 1
    assert snapshot["pending_mount"] == []
    assert snapshot["mounted_pages"] == ["kanban"]


def test_unmounted_page_still_reports_partial(tmp_path):
    """没挂过的页面仍然报 partial，并写清还缺什么（不谎称挂上了）。"""
    manifest = write_dashboard(tmp_path)
    described = dash.describe(manifest)
    assert described["mounted"] is False
    assert "没有" in described["reason"]
    assert any("include_router" in item for item in described["needed"])


# ── 5. "不 import 插件代码"这条限制没被破坏 ────────────────────────────────


def test_routes_for_still_does_not_import_the_plugin(tmp_path):
    """报告路径只读源码：``routes_for`` 不 import，``build_router`` 才 import。

    用"import 时写标记文件"来证明，而不是查 ``sys.modules``：后者会被别的用例污染
    （模块名是 ``orbit_dashboard_plugins.<name>``，跨用例复用时集合不会变）。
    """
    marker = tmp_path / "imported.marker"
    api = (
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported', encoding='utf-8')\n"
        "from fastapi import APIRouter\n"
        "router = APIRouter()\n"
        "\n"
        "@router.get('/items')\n"
        "def items():\n"
        "    return []\n"
    )
    manifest = write_dashboard(tmp_path, api=api)

    routes = dash.routes_for(manifest)
    assert [item.path for item in routes] == ["/api/plugins/kanban/items"]
    assert not marker.exists(), "routes_for 只该读源码，不该 import 插件"

    # 挂载路径则必须 import（这就是为什么它被关在 mount() 里）
    dash.build_router(registry_with(manifest))
    assert marker.exists(), "build_router 必须真的 import 才能拿到 router"
