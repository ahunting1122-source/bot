"""dashboard 插件契约（MATRIX 13.8）：``manifest.json`` 8 字段 + 模块级 ``router`` + ``/api/plugins/<name>/*``。

**它是什么**：Hermes 的 dashboard 插件是**唯一有跨前后端契约**的扩展点。一个 dashboard
插件目录长这样（``hermes-achievements`` / ``kanban`` 两个真实例子）：

    <group>/dashboard/
    ├── manifest.json      # 声明式契约（344 B / 382 B）
    ├── plugin_api.py      # FastAPI 后端，**必须暴露模块级 router = APIRouter()**
    └── dist/{index.js,style.css}

宿主负责把 ``plugin_api.router`` 挂到 ``/api/plugins/<manifest.name>/``，并把
``manifest.tab`` 变成侧边栏里的一页。字段表见 :data:`DASHBOARD_FIELDS`
（权威来源：``handoff/hermes-parity/research/05-plugin-gateway-layer.md`` §1.7）。

## 本模块做到哪一步（读之前先看这条，别被函数名骗了）

Orbit 的 dashboard 是 **React 前端 + FastAPI**（``app/main.py``），而 ``app/main.py``
曾经是**禁改区**（那是给子agent 的写入约束，不是设计决定）。**现已接线**：`app/main.py` 会 `discover()` + `PAGES.add()` + `mount_into_app(app)`。所以这一层现在做四件事：

1. **发现**（:func:`discover`）：扫 ``<dir>/*/dashboard/manifest.json``，把坏插件记进
   ``errors`` 而不中断（与 ``app/plugins.py::discover`` 同一套纪律）。
2. **校验**（:func:`validate`）：8 字段齐不齐、``tab`` 形状对不对、``api`` 里有没有
   模块级 ``router``、声明的静态资源在不在盘上。**不 import 插件代码**——校验要能在
   没有 dashboard 依赖、也没有网络的环境里跑。
3. **生成路由描述**（:func:`routes_for`）：把 ``router`` 上声明的路由翻译成
   ``/api/plugins/<name>/…`` 形态的 :class:`RouteSpec` 列表，供将来接线与 ``--json`` 消费。
4. **登记页面**（:class:`PageRegistry`，MATRIX 13.4）：``ctx.register_dashboard_page``
   的**真实落点**。上一轮注册完只留在 ``PluginContext`` 里，上下文一死就查不到了；
   登记表让"插件注册了哪些页面、要挂在哪、还缺什么"变成可枚举、可断言的事实
   （``orbit plugins pages`` 就是它的消费者）。

5. **真挂载**（:func:`build_router` / :func:`mount` / :func:`mount_into_app`，MATRIX 13.8
   本轮补上）：把插件的 ``router`` 真正 include 进一个 ``fastapi.APIRouter``。

## "不 import 插件代码"这条限制变了吗？没有，只是划了边界

发现 / 校验 / 路由描述（1–4）**一律不 import 插件代码**：它们是报告路径，必须在"没有
dashboard 依赖、也没有网络"的环境里跑得动，也不能因为一次 ``orbit plugins list`` 就
执行第三方代码。**挂载**（5）则必须 import 才能拿到 ``router``——所以它被关在一个显式
的、可审计的入口里（:func:`mount`），docstring 与日志都写明"这一步会执行第三方代码"。
两条纪律并存的方式是：**默认路径永不 import，挂载只能从 mount() 进**。

## 挂上之后仍然缺什么（都不隐瞒）

- 挂载入口是给宿主用的（``main.py`` 一行 ``plugin_dashboard.mount_into_app(app)``），而
  ``app/main.py`` **已经调了它**（在 `mount("/")` 之前）：:func:`describe` 对
  "没被挂过的插件"仍然报 ``mounted=False`` / ``state=partial``。
- 前端侧边栏（React）还没有插件页注册表，``tab.position`` 目前只被报告、不被渲染。
- ``/api/plugins/*`` 与核心 API 一样没有鉴权（本机单用户 loopback），插件路由不自带例外。
"""

from __future__ import annotations

import functools
import importlib.util
import inspect
import json
import logging
import re
import sys
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("orbit.plugin_dashboard")

#: 插件名正则（与 ``app/plugins.py::_NAME_RE`` 一致）：名字会变成 URL 前缀与目录名，
#: 所以必须挡住分隔符与 ``..``。
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")

#: 侧边栏插入位的语法：``after:<内置页id>`` / ``before:<内置页id>``（照 §1.7 的
#: ``after:analytics`` / ``after:skills``）。
_POSITION_RE = re.compile(r"^(?:after|before):[a-z0-9][a-z0-9_-]*$")

#: 挂载前缀。Hermes 原文：*"Mounted at ``/api/plugins/kanban/`` by the dashboard plugin system"*。
API_PREFIX = "/api/plugins"

#: **8 个必填字段**（调研 §1.7 的字段表）。值 = 中文含义 + "宿主拿它干什么"。
#:
#: 关于"8"这个数字：调研正文写"两例并集，共 8 个字段"，但同一节贴出的示例 JSON 与
#: 字段表**列了 9 行**（多了 ``css``）。这里按"必需 8 + 可选 1"落地，并在
#: :data:`OPTIONAL_DASHBOARD_FIELDS` 里显式登记 ``css``——两边都不隐瞒，免得
#: "到底 8 还是 9"变成每次都要重新考古的问题。
DASHBOARD_FIELDS: dict[str, str] = {
    "name": "插件 id（**同时决定 API 挂载前缀** /api/plugins/<name>/，也是目录名与前端路由的键）",
    "label": "侧边栏标签文字（Achievements / Kanban）",
    "description": "插件页描述（dashboard 插件列表里展示）",
    "icon": "图标名（Star / Package），由 dashboard 的图标集解析；不认的名字会退化成默认图标",
    "version": "前端资源版本，用于缓存失效（改了 dist/ 就必须改它，否则浏览器拿旧包）",
    "tab": "路由声明：{path: 前端路由, position: 'after:<内置页id>'}",
    "entry": "前端 JS 入口（dist/index.js），相对插件目录",
    "api": "后端模块（plugin_api.py），**必须暴露模块级 router**；宿主负责挂到 /api/plugins/<name>/",
}

#: 1 个可选字段（调研示例里有、字段表里也列了，但缺了不影响挂载）。
OPTIONAL_DASHBOARD_FIELDS: dict[str, str] = {
    "css": "样式表（dist/style.css），相对插件目录；缺了就没有自定义样式",
}

#: 发现时跳过的目录（与 ``app/plugins.py::EXCLUDED_DIRS`` 同源）。
EXCLUDED_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        "dist",
    }
)

#: ``plugin_api.py`` 里"模块级 router"的判定：``router = APIRouter()`` / ``router=...``。
_ROUTER_DECL = re.compile(r"^(?:router|ROUTER)\s*(?::[^=]+)?=", re.MULTILINE)

#: 路由装饰器：``@router.get("/items")`` / ``@router.post('/x/{id}')``。
_ROUTE_DECORATOR = re.compile(
    r"@\s*(?:router|ROUTER)\s*\.\s*(get|post|put|patch|delete|head|options|websocket)\s*\(\s*"
    r"(?:path\s*=\s*)?[\"']([^\"']*)[\"']",
    re.IGNORECASE,
)

#: ``router.add_api_route("/x", handler, methods=["POST"])`` 这种写法（kanban 里没有，
#: 但真实生态里常见，漏掉会让 routes_for 少报）。
_ADD_API_ROUTE = re.compile(
    r"\.\s*add_api_route\s*\(\s*[\"']([^\"']*)[\"']", re.IGNORECASE
)


class DashboardError(RuntimeError):
    """dashboard 插件的可预期失败（manifest 读不了 / 字段非法）。"""


# ── 数据结构 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DashboardTab:
    """侧边栏的一页：``path`` 是前端路由，``position`` 是插入位。"""

    path: str
    position: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"path": self.path, "position": self.position}


@dataclass(frozen=True)
class DashboardManifest:
    """一份解析好的 ``manifest.json``。frozen：声明是"编译期"结构。

    ``root`` 是**manifest 所在目录**（Hermes 布局是 ``<group>/dashboard/``）；它只用于
    "资源在不在盘上"的校验（``entry`` / ``css`` / ``api`` 都相对它解析），
    不参与 manifest 字段比较。
    """

    name: str
    label: str = ""
    description: str = ""
    icon: str = ""
    version: str = ""
    tab: DashboardTab = field(default_factory=lambda: DashboardTab(path=""))
    entry: str = ""
    api: str = ""
    css: str = ""
    # ── 元数据（不是 manifest 字段）──
    root: Path | None = None
    unknown_fields: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def route_prefix(self) -> str:
        """API 挂载前缀（**带结尾斜杠**，照 Hermes 的 ``/api/plugins/kanban/``）。"""
        return f"{API_PREFIX}/{self.name}/"

    @property
    def frontend_path(self) -> str:
        """前端路由（``tab.path``）。"""
        return self.tab.path

    @property
    def directory(self) -> Path:
        if self.root is None:
            raise DashboardError(f"dashboard 插件 {self.name} 没有绑定目录")
        return self.root

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "fields": {
                "label": self.label,
                "description": self.description,
                "icon": self.icon,
                "version": self.version,
                "tab": self.tab.as_dict(),
                "entry": self.entry,
                "api": self.api,
            },
            "optional": {"css": self.css},
            "root": str(self.root) if self.root else "",
            "route_prefix": self.route_prefix,
            "unknown_fields": list(self.unknown_fields),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class DashboardIssue:
    """一条校验发现。``level`` ∈ ``error`` / ``warn``。"""

    field: str
    level: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"field": self.field, "level": self.level, "detail": self.detail}


@dataclass(frozen=True)
class DashboardValidation:
    """校验结果。``ok`` = 没有 ``error`` 级发现（``warn`` 不拦）。"""

    ok: bool
    issues: tuple[DashboardIssue, ...] = ()
    manifest: DashboardManifest | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "issues": [item.as_dict() for item in self.issues],
            "manifest": self.manifest.as_dict() if self.manifest else None,
        }


@dataclass(frozen=True)
class RouteSpec:
    """一条被"翻译"过的路由：``path`` 已经是 ``/api/plugins/<name>/…`` 形态。"""

    method: str
    path: str
    endpoint: str = ""
    source: str = ""

    def as_dict(self) -> dict[str, str]:
        return {
            "method": self.method,
            "path": self.path,
            "endpoint": self.endpoint,
            "source": self.source,
        }


@dataclass
class DashboardDiscovery:
    """发现结果。像列表一样可迭代，同时保留错误——坏插件不该让好插件消失。"""

    manifests: list[DashboardManifest] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)
    root: Path | None = None

    def __iter__(self) -> Iterator[DashboardManifest]:
        return iter(self.manifests)

    def __len__(self) -> int:
        return len(self.manifests)

    def __getitem__(self, index: int) -> DashboardManifest:
        return self.manifests[index]

    def by_name(self, name: str) -> DashboardManifest | None:
        for manifest in self.manifests:
            if manifest.name == name:
                return manifest
        return None


# ── 解析 ────────────────────────────────────────────────────────────────────


def _as_str(value: Any) -> str:
    if value is None or isinstance(value, (list, tuple, dict)):
        return ""
    return str(value).strip()


def parse_manifest(data: Mapping[str, Any], *, root: Path | None = None) -> DashboardManifest:
    """从已解析的 JSON 字典构造 manifest。缺必填字段 / 名字非法就抛 :class:`DashboardError`。"""
    if not isinstance(data, Mapping):
        raise DashboardError(
            f"manifest.json 顶层必须是对象，实际是 {type(data).__name__}"
        )
    name = _as_str(data.get("name"))
    if not name:
        raise DashboardError("manifest.json 缺少 name（它同时决定 /api/plugins/<name>/ 前缀）")
    if not _NAME_RE.fullmatch(name) or ".." in name:
        raise DashboardError(
            f"dashboard 插件名非法：{name!r}（字母数字开头，含 . _ -，长度 ≤ 64；"
            f"名字会直接变成 URL 前缀与目录名）"
        )

    tab_raw = data.get("tab")
    tab = DashboardTab(path="")
    if isinstance(tab_raw, Mapping):
        tab = DashboardTab(
            path=_as_str(tab_raw.get("path")), position=_as_str(tab_raw.get("position"))
        )
    elif tab_raw is not None:
        raise DashboardError(f"tab 必须是对象 {{path, position}}，实际是 {type(tab_raw).__name__}")

    warnings: list[str] = []
    unknown = tuple(
        sorted(
            str(key)
            for key in data
            if key not in DASHBOARD_FIELDS and key not in OPTIONAL_DASHBOARD_FIELDS
        )
    )
    if unknown:
        warnings.append(f"未知字段：{'、'.join(unknown)}")

    return DashboardManifest(
        name=name,
        label=_as_str(data.get("label")),
        description=_as_str(data.get("description")),
        icon=_as_str(data.get("icon")),
        version=_as_str(data.get("version")),
        tab=tab,
        entry=_as_str(data.get("entry")),
        api=_as_str(data.get("api")),
        css=_as_str(data.get("css")),
        root=root,
        unknown_fields=unknown,
        warnings=tuple(warnings),
        raw=dict(data),
    )


def load_manifest(path: Path, *, root: Path | None = None) -> DashboardManifest:
    """读并解析一个 ``manifest.json``。文件坏 / JSON 坏 / 字段坏都抛 :class:`DashboardError`。"""
    file = Path(path)
    if not file.is_file():
        raise DashboardError(f"找不到 dashboard manifest：{file}")
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DashboardError(f"读不了 {file}：{exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise DashboardError(f"{file} 不是合法 JSON：{exc}") from exc
    # ``entry`` / ``css`` / ``api`` 都是**相对 manifest 所在目录**的路径（Hermes 的
    # ``dist/`` 就在 ``dashboard/`` 里），所以 root 默认取 manifest 的父目录。
    return parse_manifest(data or {}, root=root or file.parent)


# ── 校验 ────────────────────────────────────────────────────────────────────


def _router_source(plugin: DashboardManifest) -> str:
    """读 ``api`` 模块的源码（**不 import**：校验必须能在无 dashboard 依赖下跑）。"""
    if not plugin.api or plugin.root is None:
        return ""
    candidate = (plugin.root / plugin.api).resolve()
    if not candidate.is_relative_to(plugin.root.resolve()) or not candidate.is_file():
        return ""
    try:
        return candidate.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def validate(raw: Mapping[str, Any] | DashboardManifest) -> DashboardValidation:
    """校验 8 个必填字段 + ``tab`` 形状 + ``api`` 的模块级 ``router`` + 静态资源存在性。

    入参可以是原始字典（**缺字段也能报出"缺哪个"**，而不是只抛一句"manifest 坏"），
    也可以是已解析的 :class:`DashboardManifest`。

    为什么"缺字段"要逐条报而不是直接抛：dashboard 插件的 manifest 是**手写 JSON**，
    最常见的错误就是漏一个 ``icon`` 或把 ``tab`` 写成字符串；逐条列出字段名，
    作者才知道该补什么。
    """
    issues: list[DashboardIssue] = []
    if isinstance(raw, DashboardManifest):
        plugin: DashboardManifest | None = raw
        data: Mapping[str, Any] = raw.raw
        if not data:  # 手工构造的 manifest（测试/宿主拼装）没有 raw，按字段反推
            data = {
                "name": raw.name,
                "label": raw.label,
                "description": raw.description,
                "icon": raw.icon,
                "version": raw.version,
                "tab": raw.tab.as_dict() if raw.tab.path else None,
                "entry": raw.entry,
                "api": raw.api,
                "css": raw.css,
            }
    else:
        plugin = None
        data = raw

    for name in DASHBOARD_FIELDS:
        if name not in data or data.get(name) in (None, "", {}):
            issues.append(
                DashboardIssue(
                    field=name,
                    level="error",
                    detail=f"缺少必填字段 {name}（{DASHBOARD_FIELDS[name]}）",
                )
            )

    tab_raw = data.get("tab")
    if isinstance(tab_raw, Mapping):
        path = _as_str(tab_raw.get("path"))
        position = _as_str(tab_raw.get("position"))
        if not path.startswith("/"):
            issues.append(
                DashboardIssue(
                    field="tab.path",
                    level="error",
                    detail=f"前端路由必须以 / 开头（现在是 {path!r}）",
                )
            )
        if position and not _POSITION_RE.match(position):
            issues.append(
                DashboardIssue(
                    field="tab.position",
                    level="error",
                    detail=(
                        f"插入位必须是 after:<内置页id> 或 before:<内置页id>"
                        f"（现在是 {position!r}）"
                    ),
                )
            )
    elif tab_raw is not None:
        issues.append(
            DashboardIssue(
                field="tab",
                level="error",
                detail=f"tab 必须是对象 {{path, position}}，实际是 {type(tab_raw).__name__}",
            )
        )

    if plugin is None:
        try:
            plugin = parse_manifest(data)
        except DashboardError as exc:
            issues.append(DashboardIssue(field="name", level="error", detail=str(exc)))
            plugin = None

    if plugin is not None:
        issues.extend(_resource_issues(plugin))

    ok = not any(item.level == "error" for item in issues)
    return DashboardValidation(ok=ok, issues=tuple(issues), manifest=plugin)


def _resource_issues(plugin: DashboardManifest) -> list[DashboardIssue]:
    """静态资源与 ``router`` 的存在性检查（只在有目录可查时做）。"""
    issues: list[DashboardIssue] = []
    source = _router_source(plugin)
    if plugin.api and plugin.root is not None:
        if not source:
            issues.append(
                DashboardIssue(
                    field="api",
                    level="error",
                    detail=f"api 模块不存在或读不了：{plugin.api}（相对插件目录）",
                )
            )
        elif not _ROUTER_DECL.search(source):
            issues.append(
                DashboardIssue(
                    field="api",
                    level="error",
                    detail=(
                        "api 模块没有模块级 router（Hermes 硬契约："
                        "`from fastapi import APIRouter; router = APIRouter()`），"
                        "宿主没有东西可挂"
                    ),
                )
            )
    if plugin.entry and plugin.root is not None:
        entry = plugin.root / plugin.entry
        if not entry.is_file():
            issues.append(
                DashboardIssue(
                    field="entry",
                    level="error",
                    detail=f"前端入口不存在：{plugin.entry}（先构建 dist/ 再声明它）",
                )
            )
    if plugin.css and plugin.root is not None and not (plugin.root / plugin.css).is_file():
        issues.append(
            DashboardIssue(
                field="css", level="warn", detail=f"样式表不存在：{plugin.css}（会退化成默认样式）"
            )
        )
    return issues


# ── 发现 ────────────────────────────────────────────────────────────────────


def discover(plugins_dir: str | Path) -> DashboardDiscovery:
    """扫一个插件根下的 dashboard 插件。

    三种布局都认：① ``<root>/<group>/dashboard/manifest.json``（Hermes 真实布局）；
    ② ``<root>/<name>/manifest.json``；③ ``plugins_dir`` 本身就是一个 dashboard 目录。
    深度限 4 层，与 ``app/plugins.py::discover`` 的理由相同：不设限会把 venv 里
    几百个同名 ``manifest.json`` 都扫进来。
    """
    base = Path(plugins_dir)
    result = DashboardDiscovery(root=base)
    if not base.is_dir():
        result.errors.append((base, f"插件根不是目录：{base}"))
        return result

    candidates: list[Path] = []
    direct = base / "manifest.json"
    if direct.is_file():
        candidates.append(direct)
    for path in sorted(base.rglob("manifest.json")):
        try:
            relative = path.relative_to(base)
        except ValueError:
            continue
        if len(relative.parts) > 4 or any(part in EXCLUDED_DIRS for part in relative.parts):
            continue
        candidates.append(path)

    seen: dict[str, Path] = {}
    for path in candidates:
        try:
            manifest = load_manifest(path)
        except DashboardError as exc:
            result.errors.append((path, str(exc)))
            continue
        previous = seen.get(manifest.name)
        if previous is not None:
            result.errors.append(
                (path, f"dashboard 插件名重复：{manifest.name}（已由 {previous} 提供，本份被忽略）")
            )
            continue
        seen[manifest.name] = path
        result.manifests.append(manifest)
    return result


# ── 路由描述 ────────────────────────────────────────────────────────────────


def _join(prefix: str, route: str) -> str:
    """把挂载前缀和路由拼成一条完整路径（前缀已带结尾斜杠）。"""
    tail = str(route or "").strip()
    if not tail or tail == "/":
        return prefix
    return prefix + tail.lstrip("/")


def routes_for(plugin: DashboardManifest, *, source: str | None = None) -> tuple[RouteSpec, ...]:
    """把 ``api`` 模块里声明的路由翻译成 ``/api/plugins/<name>/…``。

    **读源码而不是 import**：① 校验路径必须能在"没有 dashboard 依赖"的环境里跑
    （Hermes 的 ``plugin_api.py`` 自己就带了 ``APIRouter`` 的 ImportError 兜底，
    说明这条约束是双方共识）；② import 一个第三方插件等于执行它的代码，而这一步
    只该读声明。

    认两种写法：``@router.get("/x")`` 装饰器，以及 ``router.add_api_route("/x", …)``。
    """
    text = source if source is not None else _router_source(plugin)
    prefix = plugin.route_prefix
    routes: list[RouteSpec] = []
    for match in _ROUTE_DECORATOR.finditer(text):
        method, route = match.group(1).upper(), match.group(2)
        routes.append(
            RouteSpec(
                method=method,
                path=_join(prefix, route),
                endpoint=plugin.api,
                source=match.group(0).strip(),
            )
        )
    for match in _ADD_API_ROUTE.finditer(text):
        routes.append(
            RouteSpec(
                method="*",
                path=_join(prefix, match.group(1)),
                endpoint=plugin.api,
                source=match.group(0).strip(),
            )
        )
    return tuple(routes)


def describe(plugin: DashboardManifest) -> dict[str, Any]:
    """给 ``orbit dashboard`` / ``plugins show`` 用的一份**诚实**描述。

    ``mounted`` 不是常量，而是**查出来的**：:data:`MOUNTS` 记着哪些插件真的被
    :func:`mount` 挂过、挂了几条路由。没挂过的插件报 ``state=partial`` /
    ``mounted=False``，并把"要挂上去还缺什么"逐条列出来——一个"挂上了但 404"的
    dashboard 插件比一个"明确说没挂"的更难查，所以宁可报未挂。
    """
    routes = routes_for(plugin)
    record = MOUNTS.get(plugin.name)
    base: dict[str, Any] = {
        "plugin": plugin.name,
        "route_prefix": plugin.route_prefix,
        "frontend_path": plugin.frontend_path,
        "tab": plugin.tab.as_dict(),
        "assets": {"entry": plugin.entry, "css": plugin.css, "api": plugin.api},
        "routes": [item.as_dict() for item in routes],
    }
    if record is not None:
        base.update(
            {
                "state": "mounted",
                "mounted": True,
                "mounted_routes": len(record.routes),
                "mounted_paths": [item.path for item in record.routes],
                "reason": (
                    f"已由 app/plugin_dashboard.py::mount() 挂载：{len(record.routes)} 条路由进 "
                    f"{record.prefix}（这一步 import 了 {record.api}，即执行了插件代码）"
                ),
                "needed": [
                    "前端侧边栏读 tab.position 生成入口（React 侧目前没有插件页注册表）",
                    "静态资源服务：把 dist/ 挂到 /plugins/<name>/ 并带 version 缓存键",
                    "鉴权：/api/plugins/* 必须与核心 API 走同一套 session token（不得自带例外）",
                ],
            }
        )
        return base
    base.update(
        {
            "state": "partial",
            "mounted": False,
            "mounted_routes": 0,
            "reason": (
                "挂载入口已经有了（app/plugin_dashboard.py::mount() / build_router()），但 "
                "本页面没有对应的 API 路由：插件没有声明 api 字段，或 build_router 校验未通过"
                "被挂上；前端侧边栏也还没有插件页注册表"
            ),
            "needed": [
                "app/main.py 增加一行：plugin_dashboard.mount_into_app(app)"
                "（内部就是 include_router；必须在 mount('/', StaticFiles) 之前，"
                "否则 /api/* 被静态站吃掉）",
                "前端侧边栏读 tab.position 生成入口（React 侧目前没有插件页注册表）",
                "静态资源服务：把 dist/ 挂到 /plugins/<name>/ 并带 version 缓存键",
                "鉴权：/api/plugins/* 必须与核心 API 走同一套 session token（不得自带例外）",
            ],
        }
    )
    return base



# ── 页面登记表：`ctx.register_dashboard_page` 的真实落点（MATRIX 13.4）───────


@dataclass
class PageRegistry:
    """插件 dashboard 页面的登记表（宿主侧结构）。

    **为什么需要它**：上一轮 ``register_dashboard_page`` 只把描述写进
    ``PluginContext.dashboard_pages``——上下文随 ``load()`` 一起死掉，于是"插件注册了
    一个页面"这件事**没有任何人查得到**，等于没注册。登记表补上这一层：
    注册 → 登记表 → ``orbit plugins pages`` 消费。

    它**仍然不挂载路由**（``mounted`` 恒为 False，理由见 :func:`describe`），
    但"注册了哪些页面、各自要挂在哪、还缺什么"从此可枚举、可断言。
    """

    #: 页面名 → {"manifest": DashboardManifest, "described": dict}
    _pages: dict[str, dict[str, Any]] = field(default_factory=dict)

    def add(
        self, manifest: DashboardManifest, described: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """登记一个页面。同名覆盖（插件重载时后一份为准，与工具注册同一条纪律）。"""
        entry: dict[str, Any] = {
            "manifest": manifest,
            "described": described if described is not None else describe(manifest),
        }
        self._pages[manifest.name] = entry
        result: dict[str, Any] = entry["described"]
        return result

    def remove(self, name: str) -> bool:
        """摘掉一个页面（插件被禁用/移除时）。返回是否真的摘掉了。"""
        return self._pages.pop(name, None) is not None

    def clear(self) -> None:
        """清空（测试隔离与热重载用）。"""
        self._pages.clear()

    def names(self) -> list[str]:
        """已登记的页面名（保持登记顺序）。"""
        return list(self._pages)

    def by_name(self, name: str) -> dict[str, Any] | None:
        entry = self._pages.get(name)
        return entry["described"] if entry else None

    def manifests(self) -> list[DashboardManifest]:
        return [entry["manifest"] for entry in self._pages.values()]

    def routes(self) -> list[RouteSpec]:
        """全部页面声明的路由（已翻译成 ``/api/plugins/<name>/…``）。"""
        out: list[RouteSpec] = []
        for entry in self._pages.values():
            out.extend(routes_for(entry["manifest"]))
        return out

    def as_dict(self) -> dict[str, Any]:
        """结构化快照。``mounted`` 是**数出来的**（查 :data:`MOUNTS`），不是常量。

        为什么现算而不是用 :meth:`add` 时缓存的那份描述：登记发生在装配期，挂载发生在
        装配之后，缓存的 ``mounted=False`` 会一直骗人。现算的代价是每次多读一遍源码
        （``routes_for`` 走文件），而 ``orbit plugins pages`` 是低频命令。
        """
        pages = [describe(manifest) for manifest in self.manifests()]
        mounted = [item["plugin"] for item in pages if item["mounted"]]
        pending = [item["plugin"] for item in pages if not item["mounted"]]
        return {
            "pages": pages,
            "routes": [item.as_dict() for item in self.routes()],
            "registered": len(self._pages),
            "mounted": len(mounted),
            "mounted_pages": mounted,
            "pending_mount": pending,
            "reason": (
                "登记表是真实落点（注册后 `orbit plugins pages` 立刻查得到），挂载入口是 "
                "app/plugin_dashboard.py::mount()；但 app/main.py 没有 include 这个 router"
                "（main.py 已接线），挂上的条数见 MOUNTS；0 条通常说明没有插件声明 dashboard 页面"
            ),
        }


#: 进程级默认登记表。宿主（``PluginRuntime`` / ``plugins.load``）把它当 ``page_sink``
#: 注入，插件的 ``register_dashboard_page`` 就直接落到这里。
#: **为什么是对象而不是模块级字典**：测试与热重载要能换一个干净的实例（``clear()``），
#: 共享一个裸 dict 会让"上一个用例注册的页面"漏到下一个用例里。
PAGES = PageRegistry()


# ── 真挂载：build_router / mount / mount_into_app（MATRIX 13.8）─────────────


@dataclass(frozen=True)
class MountRecord:
    """一次真实挂载的结果。``routes`` 是**真的**进了 APIRouter 的那几条。"""

    plugin: str
    prefix: str
    api: str
    routes: tuple[RouteSpec, ...] = ()
    #: 被跳过的路由（例如 websocket：``try/except`` 包不住一条长连接）。
    skipped: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin": self.plugin,
            "prefix": self.prefix,
            "api": self.api,
            "mounted_routes": len(self.routes),
            "paths": [item.path for item in self.routes],
            "skipped": list(self.skipped),
        }


class MountRegistry:
    """进程级挂载登记表：谁真的被挂上了、挂了几条、谁被跳过了以及为什么。

    **为什么要单独一张表**：:class:`PageRegistry` 记的是"插件声明了页面"（装配期的事），
    这里记的是"路由真的进了 app"（挂载期的事）。两件事混在一个字段里，就会出现
    "登记了 3 个页面所以显示已挂载 3 个"这种谎报——那正是本轮要修掉的那类问题。
    """

    def __init__(self) -> None:
        self._mounted: dict[str, MountRecord] = {}
        self._skipped: dict[str, str] = {}

    def record(self, record: MountRecord) -> MountRecord:
        self._mounted[record.plugin] = record
        self._skipped.pop(record.plugin, None)
        return record

    def record_skip(self, plugin: str, reason: str) -> None:
        """记一条"这个插件没挂上，因为……"。**不许静默跳过**：跳过也是事实。"""
        self._mounted.pop(plugin, None)
        self._skipped[plugin] = reason

    def get(self, plugin: str) -> MountRecord | None:
        return self._mounted.get(plugin)

    def mounted_names(self) -> list[str]:
        return list(self._mounted)

    def skipped(self) -> dict[str, str]:
        return dict(self._skipped)

    def route_count(self) -> int:
        return sum(len(record.routes) for record in self._mounted.values())

    def clear(self) -> None:
        """清空（测试隔离与热重载用）。"""
        self._mounted.clear()
        self._skipped.clear()

    def as_dict(self) -> dict[str, Any]:
        return {
            "mounted": [record.as_dict() for record in self._mounted.values()],
            "skipped": self.skipped(),
            "mounted_plugins": len(self._mounted),
            "mounted_routes": self.route_count(),
        }


#: 进程级挂载登记表。:func:`describe` 与 :class:`PageRegistry.as_dict` 都读它，
#: 所以"挂没挂上"永远只有一个答案。
MOUNTS = MountRegistry()


def _import_api(plugin: DashboardManifest) -> tuple[Any, str]:
    """import 插件的 ``api`` 模块。返回 ``(module, 错误)``，**错误不抛给调用方**。

    **这一步会执行第三方代码**，这是本模块唯一这样做的地方。三条纪律：

    1. 只从 :func:`mount` / :func:`build_router` 进来（报告路径永不 import）；
    2. 路径必须落在插件目录内（``resolve()`` 后比前缀，挡住 ``..`` 与符号链接）；
    3. import 失败就地收口成一条字符串——一个坏插件不该让整个 dashboard 起不来。
    """
    if plugin.root is None or not plugin.api:
        return None, "manifest 没有绑定插件目录或没声明 api"
    root = plugin.root.resolve()
    candidate = (root / plugin.api).resolve()
    if not candidate.is_relative_to(root):
        return None, f"api 指向插件目录之外：{plugin.api}"
    if not candidate.is_file():
        return None, f"api 模块不存在：{plugin.api}"
    module_name = "orbit_dashboard_plugins." + re.sub(r"[^a-zA-Z0-9_]", "_", plugin.name)
    try:
        spec = importlib.util.spec_from_file_location(module_name, candidate)
        if spec is None or spec.loader is None:
            return None, f"无法为 {candidate} 构造 import 规格"
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 —— 第三方代码，任何异常都只能隔离
        sys.modules.pop(module_name, None)
        return None, f"{type(exc).__name__}: {exc}"
    return module, ""


def _isolate(endpoint: Any, plugin: str, route: str) -> Any:
    """给一条插件路由的 endpoint 包一层 ``try/except``（隔离：插件炸不能拖垮 app）。

    两个细节决定这个包装能不能用：

    - ``functools.wraps`` 让 ``inspect.signature`` 透过 ``__wrapped__`` 看到**原始签名**，
      所以 FastAPI 的参数/依赖解析与没包过时完全一致（否则插件路由的参数会全变成
      ``*args, **kwargs``，请求体解析当场失效）；
    - ``HTTPException`` 原样放行：插件主动抛 404/400 是正常语义，不该被改写成 500。
    """

    @functools.wraps(endpoint)
    async def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            outcome = endpoint(*args, **kwargs)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            return outcome
        except Exception as exc:
            from fastapi import HTTPException

            if isinstance(exc, HTTPException):
                raise
            message = f"插件 {plugin} 的路由 {route} 抛异常：{type(exc).__name__}: {exc}"
            log.warning("%s", message)
            raise HTTPException(500, message) from exc

    return guarded


def _mount_one(router: Any, plugin: DashboardManifest, plugin_router: Any) -> MountRecord:
    """把一个插件的 ``router`` 里的每条路由搬到宿主 router 上，并逐条包隔离。

    为什么重建路由而不是 ``include_router`` 一把梭：``include_router`` 没有"逐条包一层"
    的钩子，而"插件抛异常只影响它自己那条"正是这一层要保证的事。重建的代价是丢掉了
    ``response_model`` 之外的少数 APIRoute 参数（下面尽量原样带过去）。
    """
    from fastapi import APIRouter
    from fastapi.routing import APIRoute

    prefix = plugin.route_prefix.rstrip("/")
    sub = APIRouter()
    mounted: list[RouteSpec] = []
    skipped: list[str] = []
    for route in plugin_router.routes:
        path = str(getattr(route, "path", "") or "")
        if not isinstance(route, APIRoute):
            # websocket / mount 之类的路由：try/except 包不住一条长连接，如实跳过。
            skipped.append(f"{type(route).__name__} {path}（非 HTTP 路由，本层不包隔离）")
            continue
        methods = sorted(method for method in (route.methods or set()) if method != "HEAD")
        if not methods:
            skipped.append(f"{path}（没有 HTTP 方法声明）")
            continue
        guarded = _isolate(route.endpoint, plugin.name, path)
        sub.add_api_route(
            path=path,
            endpoint=guarded,
            methods=methods,
            response_model=getattr(route, "response_model", None),
            status_code=getattr(route, "status_code", None),
            tags=list(getattr(route, "tags", None) or []),
            dependencies=list(getattr(route, "dependencies", None) or []),
            summary=getattr(route, "summary", None),
            description=getattr(route, "description", None),
            include_in_schema=bool(getattr(route, "include_in_schema", True)),
        )
        for method in methods:
            mounted.append(
                RouteSpec(
                    method=method,
                    path=_join(plugin.route_prefix, path),
                    endpoint=plugin.api,
                    source=f"@router.{method.lower()}('{path}')",
                )
            )
    router.include_router(sub, prefix=prefix)
    return MountRecord(
        plugin=plugin.name,
        prefix=prefix,
        api=plugin.api,
        routes=tuple(mounted),
        skipped=tuple(skipped),
    )


def build_router(pages: PageRegistry | None = None) -> Any:
    """把登记表里每个 dashboard 插件的 ``router`` **真正**挂进一个新 ``APIRouter``。

    **这一步会 import 并执行第三方插件代码**（见 :func:`_import_api`），所以它只该从
    :func:`mount` 这个显式入口被调用；:func:`routes_for` 那条"只读源码"的路径保持不变。

    挂载前**必须**过 :func:`validate`：manifest 不完整、``api`` 里没有模块级 ``router``、
    声明的静态资源不在盘上——任意一条不过就**不挂**（宁可 404，也不要挂上一个半成品：
    一个"挂上了但每次 500"的插件比"明确说没挂"难查得多）。每条被拒的原因都进
    :data:`MOUNTS` 的 ``skipped``，报告里看得见。
    """
    from fastapi import APIRouter

    registry = PAGES if pages is None else pages
    router = APIRouter()
    for manifest in registry.manifests():
        validation = validate(manifest)
        if not validation.ok:
            reasons = "；".join(
                f"{item.field}: {item.detail}" for item in validation.issues if item.level == "error"
            )
            MOUNTS.record_skip(manifest.name, f"校验未通过，未挂载：{reasons}")
            log.warning("dashboard 插件 %s 未通过校验，跳过挂载：%s", manifest.name, reasons)
            continue
        module, error = _import_api(manifest)
        if error or module is None:
            MOUNTS.record_skip(manifest.name, f"import 失败，未挂载：{error}")
            log.warning("dashboard 插件 %s 的 api 模块 import 失败，跳过挂载：%s", manifest.name, error)
            continue
        plugin_router = getattr(module, "router", None)
        if not isinstance(plugin_router, APIRouter):
            MOUNTS.record_skip(
                manifest.name,
                f"api 模块里没有模块级 router（拿到的是 {type(plugin_router).__name__}），未挂载",
            )
            continue
        try:
            record = _mount_one(router, manifest, plugin_router)
        except Exception as exc:  # noqa: BLE001 —— 一个插件的路由形状再怪也不该拖垮别的
            MOUNTS.record_skip(manifest.name, f"挂载时抛异常，未挂载：{type(exc).__name__}: {exc}")
            log.warning("dashboard 插件 %s 挂载失败：%s", manifest.name, exc)
            continue
        MOUNTS.record(record)
        log.info(
            "dashboard 插件 %s 已挂载 %d 条路由到 %s", manifest.name, len(record.routes), record.prefix
        )
    return router


def mount(pages: PageRegistry | None = None, *, audit: bool = True) -> dict[str, Any]:
    """**显式的、可审计的挂载入口**：``build_router`` + 一份报告。

    **调用它等于允许执行插件代码**——所以它单独存在，并且默认在日志里逐条点名
    "即将 import 谁、从哪个文件"。审计需要的是"谁在什么时候放行了什么"，不是一句
    "挂载完成"。
    """
    registry = PAGES if pages is None else pages
    if audit:
        for manifest in registry.manifests():
            log.warning(
                "挂载 dashboard 插件 %s：即将 import %s（这一步会执行第三方代码）",
                manifest.name,
                manifest.api or "(未声明 api)",
            )
    router = build_router(registry)
    report = MOUNTS.as_dict()
    report["router"] = router
    return report


def mount_into_app(app: Any, pages: PageRegistry | None = None) -> dict[str, Any]:
    """把插件路由挂到一个 FastAPI app 上——``app/main.py`` 需要的**那一行**。

    ``main.py`` **已经调用本函数**（在 ``mount("/")`` 之前）；那里的写法是::

        from app import plugin_dashboard
        plugin_dashboard.mount_into_app(app)   # 必须在 app.mount("/", StaticFiles) 之前

    "必须在静态站之前"不是风格问题：``app.mount("/")`` 会把 ``/api/*`` 一起吃掉，
    之后 include 的路由永远收不到请求。
    """
    report = mount(pages)
    router = report.pop("router", None)
    include = getattr(app, "include_router", None)
    if router is not None and callable(include):
        include(router)
        report["included"] = True
    else:
        report["included"] = False
        report["note"] = "目标对象没有 include_router，router 只被构造、没有挂到 app 上"
    return report


# ── 小工具 ──────────────────────────────────────────────────────────────────


def manifest_names(manifests: Iterable[DashboardManifest]) -> Sequence[str]:
    """名字列表（保持发现顺序，便于 CLI 表格逐行对照）。"""
    return [item.name for item in manifests]


__all__ = [
    "API_PREFIX",
    "DASHBOARD_FIELDS",
    "EXCLUDED_DIRS",
    "MOUNTS",
    "OPTIONAL_DASHBOARD_FIELDS",
    "PAGES",
    "DashboardDiscovery",
    "DashboardError",
    "DashboardIssue",
    "DashboardManifest",
    "DashboardTab",
    "DashboardValidation",
    "MountRecord",
    "MountRegistry",
    "PageRegistry",
    "RouteSpec",
    "build_router",
    "describe",
    "discover",
    "load_manifest",
    "manifest_names",
    "mount",
    "mount_into_app",
    "parse_manifest",
    "routes_for",
    "validate",
]
