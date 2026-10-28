"""工具的**懒发现**层：用 AST 索引回答"这个工具在哪个文件、第几行"，按需 import。

## 为什么需要它（MATRIX 5.2）

Hermes 的工具注册是"用到才 import"的：一份 AST 索引（``tools/_index.py``）记着每个
工具定义在哪个模块，注册表先只装核心工具，剩下的等第一次需要时再 import 并注册。
Orbit 原来是**全量即时注册**——``import app.tools`` 就把 24 个工具全建好，
``app/main.py`` 再把浏览器、web、GitHub 的工具一次性注册进来。

## 诚实边界：Orbit 的收益在哪、不在哪

**先把话说清楚，因为这层最容易被吹成"省了启动时间"**：

* Orbit 的 **24 个内置工具全部在 ``app/tools.py`` 一个文件里**。它们共享同一份
  import 成本（pydantic / sqlalchemy / pypdf 是那个模块的依赖），**懒发现对它们
  一分钱都省不下来**——你不可能"只 import 一个文件的一部分"。
* 真正有收益的是**按模块分居**的那些：``app/browser.py``（10 个工具，拖 playwright）、
  ``app/web.py``（2 个，拖 html 解析与出站策略）、``app/connectors.py``（2 个，
  拖 GitHub token 逻辑）。这些模块**本来就不是启动必需**，懒发现能让你在没有
  playwright / 没有配 token 的机器上也不付它们的 import 成本。
* 收益最大的一档是 **插件与 MCP 工具**：它们天然分散在几十个模块里，而且大部分
  一轮对话里根本用不上。这才是这层的目标场景，也是 Hermes 的原始动机。

所以本模块的价值不是"启动更快"（在内置工具上几乎为零），而是三件别的事：

1. **可查**：``orbit tools discover`` 能列出"每个工具定义在哪个文件第几行"，
   排查"这个工具到底注册了没有"不用再 grep；
2. **可延迟**：插件 / MCP 那类分散工具可以只在需要时 import（:func:`ensure_loaded`）；
3. **失效可判定**：``generation`` 是一份"源码指纹"，文件没变就不重扫——这就是
   Hermes 说的"generation 计数失效"，比"每次启动都重扫一遍"便宜得多。

## 三条设计决定

1. **只用 ``ast``，绝不 import 被测模块**。扫描本身要能扫"装不上依赖的模块"——
   那正是最需要被索引的东西（playwright 没装的机器上，``app/browser.py`` 依然
   得能被索引出来）。
2. **``generation`` 是 mtime+size 的哈希**，不是时间戳、不是计数器。计数器要有人
   负责递增（漏一次就永远不重扫）；时间戳会因为"改完又改回来"而误判。mtime+size
   的文件集合哈希是"内容变没变"的最便宜近似，且**跨进程一致**（进程重启后同一份
   源码算出同一个指纹，索引文件才有意义）。
3. **索引落盘到 ``<data>/tool-index.json``**，但**不做缓存命中判断的唯一依据**：
   读回来只是为了让 ``orbit tools discover --json`` 在别的进程里也能看到上次结果。
   真正决定"要不要重扫"的永远是现算的 ``generation``——落盘文件可以是旧的，
   现算的指纹不会骗人。
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import json
import logging
import os
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("orbit.tool_discovery")

#: 索引文件名。放在 ``data_dir`` 里，与 ``settings.json`` / ``tools.json`` 同规矩：
#: 换一个数据目录就能连索引一起换走。
INDEX_FILENAME = "tool-index.json"

#: 扫描时跳过的目录。``tests`` 也跳过：测试里的 ``Tool(...)`` 是夹具，不是真工具，
#: 把它们算进来会让"索引里的工具数"永远对不上注册表。
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "node_modules",
        "tests",
        "migrations",
    }
)

#: 工具名可能来自的两种 AST 形态（见 :func:`_tool_names_in`）。
TOOL_FACTORY = "Tool"
REGISTER_FUNC = "register"

#: **工具由 ``ToolRegistry`` 自己分发**的模块：它们的工具不需要外部 executor
#: （``ToolRegistry.execute`` 见 ``name not in self.external`` 就走 ``self.local``）。
#: 只有 ``app.tools`` 一家——刻意写成白名单而不是"猜"：猜错的后果是注册了一个
#: 永远调不动的工具。
LOCAL_MODULES: frozenset[str] = frozenset({"app.tools"})


class DiscoveryError(RuntimeError):
    """索引查不到 / 加载不了时抛。**消息必须能行动**：说清下一步做什么。"""


@dataclass(frozen=True)
class ModuleInfo:
    """一个工具在源码里的位置：工具名 + 定义模块 + 文件 + 行号。

    名字叫 ``ModuleInfo`` 是因为它回答的核心问题是"**哪个模块**"——``path`` 与
    ``lineno`` 只是把这个答案钉到可以跳转的粒度（``ensure_loaded`` 要 import 的是
    ``module``，人和编辑器要的是 ``path:lineno``）。
    """

    name: str
    module: str
    path: str
    lineno: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "module": self.module,
            "path": self.path,
            "lineno": self.lineno,
        }


# ── AST 扫描 ────────────────────────────────────────────────────────────────


def _module_name(root: Path, path: Path) -> str:
    """源文件 → 模块名（点分）。**不 import，纯路径推导**。

    规则：``root`` 自己是一个包（有 ``__init__.py``）就用它的目录名当前缀，
    否则用相对路径本身。``__init__.py`` 去掉文件名而不是当成一个叫 ``__init__`` 的
    模块——否则 ``app/tools/__init__.py`` 会变成 ``app.tools.__init__``，import 会失败。
    """
    relative = path.relative_to(root)
    parts = list(relative.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    base = root.name if (root / "__init__.py").exists() else ""
    return ".".join([base, *parts]) if base else ".".join(parts)


def _call_name(node: ast.Call) -> str:
    """``Tool(...)`` / ``registry.register(...)`` 的调用名（``Tool`` / ``register``）。"""
    func = node.func
    return getattr(func, "id", None) or getattr(func, "attr", None) or ""


def _literal_str(node: ast.expr | None) -> str:
    """取一个字符串字面量；不是字面量就返回空串（**不做常量折叠**）。

    为什么不做折叠：``Tool(NAME, ...)`` 这种写法下工具名是变量，静态推不出来。
    与其猜（把变量名当工具名），不如返回空串让这个工具"不被索引"——索引少一条
    的代价是"下次还得 grep"，猜错的代价是 ``ensure_loaded`` import 了一个错模块。
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _tool_names_in(tree: ast.AST) -> list[tuple[str, int]]:
    """从一棵 AST 里认出 ``Tool("name", …)`` 与 ``register(name="name", …)``。

    两种形态都要认，理由：Orbit 的内置工具是 ``Tool(...)`` 字面量（``LOCAL_TOOLS``、
    ``BROWSER_TOOLS``、``WEB_TOOLS``、``GITHUB_TOOLS``），而 Hermes 与插件走
    ``registry.register(name=...)``。只认一种就会漏掉一半工具，而"索引漏了"
    在运行时表现为"这个工具找不到模块"，很难联想到是扫描器的问题。
    """
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        called = _call_name(node)
        if called == TOOL_FACTORY:
            name = _literal_str(node.args[0]) if node.args else ""
            if not name:
                for keyword in node.keywords:
                    if keyword.arg == "name":
                        name = _literal_str(keyword.value)
            if name:
                found.append((name, node.lineno))
        elif called == REGISTER_FUNC:
            name = ""
            for keyword in node.keywords:
                if keyword.arg == "name":
                    name = _literal_str(keyword.value)
            if not name and node.args:
                name = _literal_str(node.args[0])
            if name:
                found.append((name, node.lineno))
    return found


def iter_sources(root: str | Path) -> list[Path]:
    """``root`` 下所有要参与索引的 ``.py`` 文件（排序后，保证 generation 稳定）。"""
    base = Path(root)
    if not base.exists():
        return []
    files: list[Path] = []
    for current, dirs, names in os.walk(base):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for filename in sorted(names):
            if not filename.endswith(".py"):
                continue
            files.append(Path(current) / filename)
    return files


def scan_modules(root: str | Path) -> dict[str, ModuleInfo]:
    """扫 ``root`` 下的源码，返回 ``{工具名: ModuleInfo}``。**不 import 任何被测模块**。

    同名工具（跨模块重复定义）保留**第一次**扫到的那个，并记一条 warning：
    注册表的遮蔽门禁才是裁决者，索引只负责"指路"，不该在这里替它做决定。
    """
    base = Path(root)
    index: dict[str, ModuleInfo] = {}
    for path in iter_sources(base):
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            # 语法错的模块跳过而不是整次扫描失败：索引要能在"仓库正被改到一半"时
            # 照样跑起来，否则一次手滑就让 `tools discover` 完全不可用。
            log.warning("跳过无法解析的文件 %s：%s", path, exc)
            continue
        module = _module_name(base, path)
        for name, lineno in _tool_names_in(tree):
            existing = index.get(name)
            if existing is not None:
                log.warning(
                    "工具 %s 在 %s:%d 与 %s:%d 都有定义，索引保留前者（谁生效由注册表决定）",
                    name,
                    existing.path,
                    existing.lineno,
                    path,
                    lineno,
                )
                continue
            index[name] = ModuleInfo(name=name, module=module, path=str(path), lineno=lineno)
    return index


def generation(root: str | Path) -> str:
    """源码指纹：所有相关文件的 ``(相对路径, mtime_ns, size)`` 的 sha256。

    **为什么是这三样**：路径变了 = 模块名变了（必须重扫）；mtime 变了 = 文件被写过
    （即使内容改回原样，重扫一次也不贵）；size 兜住"mtime 精度不足"的平台
    （某些文件系统的 mtime 只有秒级，同一秒内两次写入 mtime 相同）。

    结果**跨进程稳定**：同一份源码在任何进程里算出同一个指纹，索引文件才有意义。
    """
    base = Path(root)
    digest = hashlib.sha256()
    for path in iter_sources(base):
        try:
            stat = path.stat()
        except OSError:
            continue
        relative = path.relative_to(base).as_posix()
        digest.update(f"{relative}\0{stat.st_mtime_ns}\0{stat.st_size}\n".encode())
    return digest.hexdigest()[:32]


# ── 索引 ────────────────────────────────────────────────────────────────────


@dataclass
class Index:
    """一次扫描的结果 + 一个 generation 门禁。

    ``refresh()`` 只在指纹变了时重扫——**这就是"generation 计数失效"的落地形态**：
    没人需要维护计数器，指纹自己会说话。
    """

    root: Path
    entries: dict[str, ModuleInfo] = field(default_factory=dict)
    stamp: str = ""
    scanned_at: float = 0.0
    #: 累计重扫次数（给诊断用：为 1 说明缓存一直在命中）。
    scans: int = 0

    def refresh(self, *, force: bool = False) -> bool:
        """指纹变了才重扫。返回 True = 这次真的重扫了。"""
        current = generation(self.root)
        if not force and current == self.stamp and self.entries:
            return False
        self.entries = scan_modules(self.root)
        self.stamp = current
        self.scanned_at = time.time()
        self.scans += 1
        return True

    def find(self, name: str) -> ModuleInfo | None:
        return self.entries.get(name)

    def modules(self) -> dict[str, list[str]]:
        """``{模块名: [工具名…]}``（排序后，便于 diff 与展示）。"""
        grouped: dict[str, list[str]] = {}
        for info in self.entries.values():
            grouped.setdefault(info.module, []).append(info.name)
        return {module: sorted(names) for module, names in sorted(grouped.items())}

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "generation": self.stamp,
            "scanned_at": self.scanned_at,
            "scans": self.scans,
            "tools": {name: info.as_dict() for name, info in sorted(self.entries.items())},
            "modules": self.modules(),
        }

    def ensure_loaded(
        self,
        name: str,
        registry: Any,
        *,
        executor: Any = None,
        override: bool = False,
    ) -> bool:
        """第一次需要 ``name`` 时 import 它的模块并注册。返回 True = 这次真的加载了。

        幂等：工具已在注册表里就直接返回 False（**不重复注册**——重复注册会踩
        ``ToolRegistry`` 的遮蔽门禁，而且 MCP 重连场景下本来就该走注册表自己的路径）。

        ``executor`` 留给调用方传：**本模块不可能凭空造出执行器**。``app/browser.py``
        的工具由 ``app.state.browser.execute`` 执行，那个对象只有装配层（``app/main.py``）
        知道。所以：
        * ``LOCAL_MODULES`` 里的模块（``app.tools``）直接进 ``registry.tools``，
          由 ``ToolRegistry.local`` 分发；
        * 其余模块**必须**传 ``executor``，否则如实抛错——编一个假执行器只会换来
          "工具调了但什么也没发生"，比报错难查得多。
        """
        info = self.find(name)
        if info is None:
            raise DiscoveryError(
                f"索引里没有工具 {name}。先跑 `orbit tools discover` 重建索引；"
                "若它确实存在，检查它的名字是不是字符串字面量（变量名静态推不出来）。"
            )
        if name in registry.tools:
            return False
        try:
            module = importlib.import_module(info.module)
        except Exception as exc:  # noqa: BLE001 - 导入失败要如实说是哪个模块
            raise DiscoveryError(
                f"加载工具 {name} 失败：import {info.module} 出错（{type(exc).__name__}: {exc}）。"
                f"它定义在 {info.path}:{info.lineno}。"
            ) from exc
        tool = find_tool_object(module, name)
        if tool is None:
            raise DiscoveryError(
                f"{info.module} 里找不到名为 {name} 的工具对象（索引说在 "
                f"{info.path}:{info.lineno}）。可能工具名是运行时拼出来的，"
                "或者注册发生在别的模块。"
            )
        if executor is None:
            if info.module not in LOCAL_MODULES:
                raise DiscoveryError(
                    f"工具 {name} 在 {info.module}，它的执行器只有装配层知道。"
                    "请传 executor=（例如 app/main.py 里的 app.state.<x>.execute）。"
                )
            registry.tools[name] = tool
            return True
        registry.register([tool], executor, override=override)
        return True


def find_tool_object(module: Any, name: str) -> Any:
    """在**已导入**的模块里找 ``name`` 对应的 Tool 对象。

    认三种摆放方式（Orbit 里三种都有）：模块级变量是 Tool、是 list/tuple 的元素、
    或挂在某个类的属性上（``app/browser.py`` 是 list）。用 ``__dict__`` 遍历而不是
    硬编码属性名——新增一个 ``XXX_TOOLS`` 列表不该要求改这个函数。
    """
    for value in vars(module).values():
        if getattr(value, "name", None) == name and hasattr(value, "specification"):
            return value
        if isinstance(value, (list, tuple)):
            for item in value:
                if getattr(item, "name", None) == name and hasattr(item, "specification"):
                    return item
    return None


#: 进程内默认索引（按 root 分开缓存）。键是 ``root`` 的绝对路径。
_INDEXES: dict[str, Index] = {}


def default_index(root: str | Path) -> Index:
    """取（必要时创建）某个 root 的进程内索引。**不自动扫描**：由调用方 refresh。"""
    key = str(Path(root).resolve())
    index = _INDEXES.get(key)
    if index is None:
        index = Index(root=Path(root))
        _INDEXES[key] = index
    return index


def app_root() -> Path:
    """Orbit 的 ``app`` 包目录（索引的默认 root）。"""
    import app

    return Path(app.__file__).parent


def index_path(data_dir: str | Path) -> Path:
    """``<data>/tool-index.json`` 的绝对路径。"""
    return Path(data_dir) / INDEX_FILENAME


def save_index(index: Index, path: str | Path) -> Path:
    """索引落盘（先写临时文件再替换，避免半截 JSON）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(index.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    return target


def load_index(path: str | Path) -> Index | None:
    """读回落盘的索引。坏文件/不存在都返回 ``None``（**绝不抛**）。

    为什么容忍坏文件：索引是**加速与诊断用的旁路数据**，不是配置。它坏了应该退回
    "重扫一遍"，而不是让 ``orbit tools discover`` 直接失败。
    """
    target = Path(path)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    root = raw.get("root")
    tools = raw.get("tools")
    if not isinstance(root, str) or not isinstance(tools, dict):
        return None
    entries: dict[str, ModuleInfo] = {}
    for name, item in tools.items():
        if not isinstance(item, dict):
            continue
        entries[str(name)] = ModuleInfo(
            name=str(item.get("name") or name),
            module=str(item.get("module") or ""),
            path=str(item.get("path") or ""),
            lineno=int(item.get("lineno") or 0),
        )
    return Index(
        root=Path(root),
        entries=entries,
        stamp=str(raw.get("generation") or ""),
        scanned_at=float(raw.get("scanned_at") or 0.0),
        scans=int(raw.get("scans") or 0),
    )


def discover(
    root: str | Path | None = None,
    *,
    data_dir: str | Path | None = None,
    force: bool = False,
) -> Index:
    """跑一次发现：刷新索引，并把结果落到 ``<data>/tool-index.json``（给了 data_dir 时）。"""
    target = Path(root) if root is not None else app_root()
    index = default_index(target)
    index.refresh(force=force)
    if data_dir is not None:
        try:
            save_index(index, index_path(data_dir))
        except OSError as exc:  # 落盘失败不该让发现本身失败
            log.warning("工具索引落盘失败（忽略）：%s", exc)
    return index


def ensure_loaded(
    name: str,
    registry: Any,
    *,
    executor: Any = None,
    override: bool = False,
    root: str | Path | None = None,
) -> bool:
    """模块级入口：用**默认索引**（``app`` 包）按需加载一个工具。返回 True = 这次真的加载了。

    与 ``Index.ensure_loaded`` 同一套语义，只是省掉"先拿到索引"这一步——调用方
    （插件装配、MCP 桥）通常只知道自己要哪个工具名。
    """
    index = default_index(root if root is not None else app_root())
    if not index.entries:
        index.refresh()
    return index.ensure_loaded(name, registry, executor=executor, override=override)


# ── 自述 ────────────────────────────────────────────────────────────────────


def describe(root: str | Path | None = None) -> str:
    """一段**如实的**自述：这层到底省了什么、没省什么。

    存在的理由：懒发现最容易被写成"启动快了几百毫秒"。Orbit 的内置工具全在一个
    文件里，那个数字根本不存在。与其留一段漂亮的话让人误判，不如让函数自己算出
    真实分布并写出来——``orbit tools discover`` 直接打印它。
    """
    index = default_index(root if root is not None else app_root())
    if not index.entries:
        index.refresh()
    grouped = index.modules()
    total = len(index.entries)
    builtin = len(grouped.get("app.tools", []))
    lines = [
        "懒发现（MATRIX 5.2）：AST 索引 + 按需 import",
        f"  索引 root：{index.root}",
        f"  generation：{index.stamp}（文件 mtime+size 的哈希；变了才重扫）",
        f"  工具总数：{total}，分布在 {len(grouped)} 个模块",
    ]
    for module, names in grouped.items():
        lines.append(f"    {module}：{len(names)} 个（{'、'.join(names[:4])}{'…' if len(names) > 4 else ''}）")
    lines.append(
        f"  真实收益：{builtin} 个内置工具全部在 app/tools.py **同一个文件**里，"
        "懒发现对它们省不下任何 import 成本（同一个模块不可能只 import 一部分）。"
    )
    other = total - builtin
    lines.append(
        f"  有收益的部分：另外 {other} 个工具分居在 {len(grouped) - (1 if builtin else 0)} 个模块"
        "（browser / web / connectors），它们拖的是 playwright、html 解析、GitHub 逻辑——"
        "没装依赖或没配 token 的机器上本来就不该付这份成本。"
    )
    lines.append(
        "  收益最大的一档在**插件与 MCP 工具**上：天然分散、多数轮次用不上，"
        "这才是这层的目标场景。别把这里读成「启动快了很多」。"
    )
    return "\n".join(lines)


def covered_names(index: Index, names: Iterable[str]) -> tuple[list[str], list[str]]:
    """对账：``(索引里有的, 索引里没有的)``。给测试与 ``tools discover`` 用。

    为什么需要它：索引"看起来在工作"与"真的覆盖了注册表"是两件事。这个函数把
    差额摆到明面上——``missing`` 非空就是索引有洞，而洞在运行时表现为
    "这个工具找不到模块"，最难联想回扫描器。
    """
    known: list[str] = []
    missing: list[str] = []
    for name in names:
        (known if index.find(name) is not None else missing).append(name)
    return known, missing


__all__ = [
    "INDEX_FILENAME",
    "LOCAL_MODULES",
    "DiscoveryError",
    "Index",
    "ModuleInfo",
    "app_root",
    "covered_names",
    "default_index",
    "describe",
    "discover",
    "ensure_loaded",
    "find_tool_object",
    "generation",
    "index_path",
    "iter_sources",
    "load_index",
    "save_index",
    "scan_modules",
]
