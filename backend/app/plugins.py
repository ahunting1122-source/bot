"""插件层：`plugin.yaml` 声明式 manifest、发现/加载、能力注入面、安装期安全闸。

**为什么 Orbit 需要这一整层**（对标 Hermes 的 ``plugins/`` + ``tools/plugin_guard.py``）：

Orbit 现在只有"技能"（`app/skills.py`，流程知识，纯文档）和"工具"（`app/tools.py`，
写死在代码里）。缺的是中间那一档：**第三方用代码扩展宿主，但宿主在加载前能审查它**。
插件与技能的差别不是"能不能跑代码"，而是"跑代码时有哪些落点"——插件可以在
agent 的生命周期里插 hook、注册工具、注册模型 provider，因此它同时需要：

1. **声明**（`plugin.yaml`）：宿主在**不 import 任何插件代码**的前提下就知道这个插件
   要什么环境变量、会贡献什么工具、属于哪一类（`kind` 决定加载时机）。
2. **注入面**（``ctx.register_*``）：插件只能通过上下文对象拿到落点，而不是
   ``import app.tools`` 到处改全局——这样宿主能审计"它用了哪些能力"。
3. **安装期安全闸**（plugin guard）：插件与 agent 同进程、同权限，**没有 Python 级
   沙箱**（Hermes 原文：*"they run Python in-process with the agent"*）。所以唯一
   能拦的时机是安装/更新时的静态扫描 + 结构限额。

## 本模块的边界（读之前先知道哪些没做）

- **不做**：真正的进程隔离/沙箱。Hermes 也没有，它的"能力"是同意与审计层，
  不是沙箱（原文：*"Capabilities are a consent and audit layer over host API
  surfaces — NOT a sandbox."*）。本模块沿用这个定位，不假装更安全。
- **不做**：`app/main.py` / `app/graph/*` / `app/agent.py` 的接线（本轮禁改区）。
  :meth:`PluginContext` 提供落点、:mod:`app.hooks` 提供事件表，宿主启动时把它们接上即可。
- **不做**：远程安装（`git clone` / 社区索引）。本机无网络，`orbit plugins install`
  只接受本地目录，远程源明确报"未实现 + 原因"。

## manifest 的 16 个顶层字段

字段表 :data:`MANIFEST_FIELDS` **照调研 05 §1.2 的 16 个字段并集**（对 103 个真实
`plugin.yaml` 做 key 并集统计得出），逐个写明含义。任务书里另列了几个 Orbit 侧
扩展名（``entry`` / ``min_orbit_version`` / ``enabled_by_default`` / ``settings`` /
``license`` / ``provides_platform`` / ``provides_provider``）——它们**不在**调研的
16 个之内，但 Orbit 确实要用（尤其 ``entry`` 决定 import 哪个模块），所以单独登记在
:data:`ORBIT_EXTENSION_FIELDS` 里，**同样不算 unknown_fields**。两边都不隐瞒：
``manifest.as_dict()`` 会把"16 字段"与"扩展字段"分开报。
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import shutil
import sys
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]  # 本机无 types-PyYAML（无网络，不能装）

from app.config import ROOT
from app.hooks import HookRegistry, normalize_event

log = logging.getLogger("orbit.plugins")

# ── manifest 字段表 ─────────────────────────────────────────────────────────

#: 调研 05 §1.2 的 16 个顶层字段（**权威集合**，出现次数取自 103 个真实 plugin.yaml）。
#: 值 = 中文含义（含"为什么宿主需要它"）。
MANIFEST_FIELDS: dict[str, str] = {
    "name": "插件唯一标识（103/103）。用于 enable/disable/show、数据目录名与日志归属；"
    "校验正则 `^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$`，**与目录名不必相同**",
    "version": "插件自身语义版本（103/103）。仅展示与审计，不参与依赖求解；"
    "YAML 里 `1.0.0` 与 `\"1.0.0\"` 两种写法并存，都按字符串收",
    "description": "人类可读用途说明（103/103）。`plugins list`、dashboard 插件页、"
    "setup 向导直接展示；多为 `>` 折叠块",
    "author": "作者/维护方（95/103）。内置插件为 NousResearch / Nous Research 两种写法",
    "kind": "插件类别（90/103，缺省 13 个）。**决定加载时机与可注册能力**，见 KIND_TIMING",
    "requires_env": "必填凭证/环境变量（39/103）。元素可裸字符串或富字典，见 ENV_FIELDS",
    "label": "展示名（22/103）。**全部 22 个都是 kind: platform**——只有平台适配器需要",
    "optional_env": "可选环境变量（21/103）。形状同 requires_env（允许名单、home channel 等）",
    "hooks": "本插件订阅的 hook 事件名（10/103）。**声明即注册意图**，运行期由 "
    "ctx.register_hook() 真正绑定",
    "provides_web_providers": "向 web 搜索注册表提供的 provider id（9/103，9 个 web/* 各 1）",
    "pip_dependencies": "需要的 PyPI 包（6/103，PEP 508 约束串）。**Orbit 不装依赖**，"
    "只做展示与 doctor 缺项提示",
    "provides_browser_providers": "提供的浏览器后端 id（3/103：firecrawl/browserbase/browser-use）",
    "provides_tools": "声明贡献的 agent 工具名（3/103）。**关键作用：要求发现期就 import**"
    "（否则 CLI/TUI 进程里工具永远不注册）",
    "platforms": "宿主操作系统约束（2/103，linux/macos/windows）。google_meet 不含 windows",
    "external_dependencies": "非 PyPI 的外部二进制依赖（1/103）。子字段 name/install/check",
    "homepage": "插件主页 URL（1/103，第三方插件 model-providers/meta-ai）",
}

#: Orbit 侧扩展字段（**不在**调研 16 个之内，但 Orbit 需要）。与 16 字段一样**不算**
#: unknown_fields——否则真实 Hermes 插件里的 `entry` 会被误报成"未知字段"。
ORBIT_EXTENSION_FIELDS: dict[str, str] = {
    "entry": "register(ctx) 所在模块（相对插件目录的 .py 或包目录）。缺省 `__init__.py`。"
    "Hermes 的约定是包内 `__init__.py`，Orbit 显式化是为了能加载单文件插件",
    "min_orbit_version": "要求的最低 Orbit 版本。doctor 用它判兼容性；不满足只报错不阻断加载",
    "enabled_by_default": "安装后是否默认启用（默认 true）。Hermes 的 portable package 默认"
    "**装成禁用**，Orbit 用这个字段表达同样语义",
    "settings": "插件自己的默认配置（自由字典）。宿主原样存进 plugins.json，不解释",
    "license": "许可证标识（MIT / Apache-2.0 …）。仅展示与审计",
    "provides_platform": "提供的消息平台 id（Orbit 侧声明；Hermes 用目录分组表达同一件事）",
    "provides_provider": "提供的模型 provider id（Orbit 侧声明；对应 kind: provider）",
}

#: `requires_env` / `optional_env` 富字典的 6 个子字段（05 §1.2.1，168 条声明统计）。
ENV_FIELDS: dict[str, str] = {
    "name": "环境变量名（168/168，大写蛇形）",
    "description": "给用户看的说明（167/168），出现在 config UI / setup 向导 / doctor 缺项提示",
    "prompt": "setup 向导的交互式提问文案（167/168）",
    "password": "true = 输入掩码、落 .env 按密钥处理（167/168）",
    "url": "去哪申请这个凭证（16/168，如 https://api.slack.com/apps）",
    "category": "分类提示（1/168，目前仅 raft 用 `setting`，把设置项与凭证区分开）",
}

#: 归一后的 Orbit kind。**只有这 4 个合法**，其它一律拒绝加载。
PLUGIN_KINDS: tuple[str, ...] = ("tool", "platform", "provider", "dashboard")

#: kind → 加载时机（照 Hermes 的 kind 语义收敛，见 KIND_ALIASES 的对照）。
KIND_TIMING: dict[str, str] = {
    "tool": "启动即加载：agent 起来就把 register(ctx) 跑一遍（贡献工具/hook/CLI 命令）",
    "platform": "网关启动时加载：消息平台适配器只在 gateway 进程里注册",
    "provider": "模型层按需：选到该 provider 时才 import，避免拖慢启动",
    "dashboard": "仅 Web UI：没有 dashboard 时整层跳过",
}

#: Hermes 的真实 kind 取值 → Orbit 的 4 个 kind。
#:
#: 为什么要兼容：Hermes 的 103 个真实 `plugin.yaml` 用的是
#: `backend`(27) / `model-provider`(39) / `platform`(22) / `standalone`(2) / 缺省(13)，
#: 如果 Orbit 只认自己的 4 个名字，**所有真实插件都会被判"kind 非法"**，等于这套
#: manifest 解析器对真实生态零兼容。别名表让两种写法都能加载，同时对外只暴露 4 个语义。
KIND_ALIASES: dict[str, str] = {
    "": "tool",              # 缺省（13 个 memory/* 等）：普通插件
    "backend": "tool",       # 27 个 web/browser/image_gen/video_gen/dashboard_auth
    "standalone": "tool",    # 2 个（google_meet / teams_pipeline）：显式启用
    "model-provider": "provider",  # 39 个推理 provider
}

#: 插件名正则（照 `plugin_storage.py` 的 `_NAME_RE`）。
#: 别的地方用它挡目录穿越——名字会直接变成 `<data_dir>/plugins/<name>`。
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$")

#: 技能名正则：与 `app/skills.py::_SAFE_NAME` 一致（技能名不允许点号）。
_SKILL_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}$")

#: 扫描/发现时永远跳过的目录（VCS 内部、缓存、venv）。
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
    }
)


# ── 异常 ────────────────────────────────────────────────────────────────────


class PluginError(RuntimeError):
    """插件层的可预期失败（manifest 坏、目录不存在、重名安装…）。"""


class ManifestError(PluginError):
    """`plugin.yaml` 读不了 / 字段非法。**kind 非法也走这里**（拒绝加载）。"""


class UnsupportedCapability(PluginError):
    """插件调用了宿主没有落点的 ``ctx.register_*``。

    故意抛而不是静默吞掉：静默吞掉等于"插件以为自己注册成功、宿主永远不会调用它"，
    这是最难查的一类故障（没有报错、没有日志、功能就是不生效）。
    """

    def __init__(self, capability: str, reason: str) -> None:
        self.capability = capability
        self.reason = reason
        super().__init__(f"{capability} 在 Orbit 里没有落点：{reason}")


class PluginGuardError(PluginError):
    """安装闸拒绝（dangerous 裁决 / 需要确认 / 目标已存在）。"""


# ── 环境变量声明 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EnvVar:
    """一条 `requires_env` / `optional_env` 声明。

    两种写法（裸字符串 / 富字典）在这里**收敛成同一个形状**：裸字符串只填 name，
    其余留空。于是下游（setup 向导、doctor、enable 时的缺项提示）只面对一种类型，
    不需要到处写 ``isinstance(item, str)``。
    """

    name: str
    description: str = ""
    prompt: str = ""
    password: bool = False
    url: str = ""
    category: str = ""
    #: True = 来自 requires_env（缺失要拦），False = optional_env（缺失只是提示）。
    required: bool = False
    #: 富字典里出现的未知子字段（不报错，但要能查出来）。
    unknown_fields: tuple[str, ...] = ()

    def as_prompt(self) -> dict[str, Any]:
        """给向导/UI 消费的形状。**不含值**——凭证只从环境变量/密钥环读。"""
        return {
            "name": self.name,
            "description": self.description,
            "prompt": self.prompt or self.description or self.name,
            "password": self.password,
            "url": self.url,
            "category": self.category,
            "required": self.required,
        }


def parse_env_var(item: Any, *, required: bool) -> EnvVar:
    """把一条 env 声明（裸字符串或富字典）解析成 :class:`EnvVar`。"""
    if isinstance(item, str):
        name = item.strip()
        if not name:
            raise ManifestError("env 声明里有空的变量名")
        return EnvVar(name=name, required=required)
    if isinstance(item, dict):
        raw_name = str(item.get("name") or "").strip()
        if not raw_name:
            raise ManifestError(f"env 富字典缺少 name：{item!r}")
        unknown = tuple(sorted(str(key) for key in item if key not in ENV_FIELDS))
        return EnvVar(
            name=raw_name,
            description=str(item.get("description") or ""),
            prompt=str(item.get("prompt") or ""),
            password=_as_bool(item.get("password"), default=False),
            url=str(item.get("url") or ""),
            category=str(item.get("category") or ""),
            required=required,
            unknown_fields=unknown,
        )
    raise ManifestError(f"env 声明只能是字符串或字典：{item!r}")


def env_prompts(manifest: PluginManifest) -> list[dict[str, Any]]:
    """把 manifest 里的 env 声明摊平成"向导可直接渲染"的字段列表。

    **为什么要有这个函数**：`requires_env` 的富字典就是为 setup 向导而生的
    （Hermes 原文：auto-populate ``OPTIONAL_ENV_VARS`` so the setup wizard surfaces
    proper descriptions, prompts, password flags, and URLs）。Orbit 的 `orbit setup`
    要消费同一份信息，就必须有一个与 manifest 结构解耦的出口——否则向导得知道
    "裸字符串"和"富字典"两种写法，还要知道哪条来自 required、哪条来自 optional。
    """
    return [item.as_prompt() for item in (*manifest.requires_env, *manifest.optional_env)]


# ── manifest ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PluginManifest:
    """一份解析好的 `plugin.yaml`。

    frozen：manifest 是"编译期"结构，运行期要变的只有"启用/禁用/授权位"，
    那些落在 :class:`PluginState`（plugins.json）里，不该顺手改 manifest——
    否则两个进程会看到两套声明。
    """

    # ── 调研 05 §1.2 的 16 个字段 ──
    name: str
    version: str = ""
    description: str = ""
    author: str = ""
    kind: str = "tool"
    requires_env: tuple[EnvVar, ...] = ()
    label: str = ""
    optional_env: tuple[EnvVar, ...] = ()
    hooks: tuple[str, ...] = ()
    provides_web_providers: tuple[str, ...] = ()
    pip_dependencies: tuple[str, ...] = ()
    provides_browser_providers: tuple[str, ...] = ()
    provides_tools: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ()
    external_dependencies: tuple[dict[str, Any], ...] = ()
    homepage: str = ""
    # ── Orbit 扩展字段 ──
    entry: str = "__init__.py"
    min_orbit_version: str = ""
    enabled_by_default: bool = True
    settings: dict[str, Any] = field(default_factory=dict)
    license: str = ""
    provides_platform: tuple[str, ...] = ()
    provides_provider: tuple[str, ...] = ()
    # ── 元数据（不是 manifest 字段）──
    path: Path | None = None
    root: Path | None = None
    unknown_fields: tuple[str, ...] = ()
    unknown_hooks: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    raw: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    # ── 派生属性 ──
    @property
    def directory(self) -> Path:
        """插件目录（= plugin.yaml 的父目录）。"""
        if self.path is None:
            raise PluginError(f"插件 {self.name} 没有绑定路径")
        return self.path.parent

    @property
    def eager(self) -> bool:
        """发现期是否就必须 import 这个插件。

        照 Hermes 的规则（`platforms/a2a/plugin.yaml` 注释）：插件默认是**延迟**的，
        只有 ``kind: backend`` 类、或显式声明 ``provides_tools``（要求发现期就 import
        tools.py）的插件才会在 CLI/TUI 进程里立刻注册。Orbit 里 ``tool``/``provider``
        两类都按"启动即加载/按需加载"处理，``platform``/``dashboard`` 一律延迟。
        """
        return self.kind in {"tool", "provider"} or bool(self.provides_tools)

    def missing_env(self, environ: dict[str, str] | None = None) -> tuple[str, ...]:
        """缺哪些**必填**环境变量（只看名字，不读值）。"""
        source = environ if environ is not None else _environ()
        return tuple(item.name for item in self.requires_env if not source.get(item.name))

    def as_dict(self) -> dict[str, Any]:
        """结构化输出。**16 字段与扩展字段分开报**，不混成一张表。"""
        return {
            "name": self.name,
            "fields": {
                "version": self.version,
                "description": self.description,
                "author": self.author,
                "kind": self.kind,
                "label": self.label,
                "homepage": self.homepage,
                "platforms": list(self.platforms),
                "pip_dependencies": list(self.pip_dependencies),
                "external_dependencies": [dict(item) for item in self.external_dependencies],
                "requires_env": [item.as_prompt() for item in self.requires_env],
                "optional_env": [item.as_prompt() for item in self.optional_env],
                "hooks": list(self.hooks),
                "provides_tools": list(self.provides_tools),
                "provides_web_providers": list(self.provides_web_providers),
                "provides_browser_providers": list(self.provides_browser_providers),
            },
            "orbit_fields": {
                "entry": self.entry,
                "min_orbit_version": self.min_orbit_version,
                "enabled_by_default": self.enabled_by_default,
                "settings": dict(self.settings),
                "license": self.license,
                "provides_platform": list(self.provides_platform),
                "provides_provider": list(self.provides_provider),
            },
            "path": str(self.path) if self.path else "",
            "root": str(self.root) if self.root else "",
            "unknown_fields": list(self.unknown_fields),
            "unknown_hooks": list(self.unknown_hooks),
            "warnings": list(self.warnings),
        }


def parse_manifest(
    data: dict[str, Any],
    *,
    path: Path | None = None,
    root: Path | None = None,
) -> PluginManifest:
    """从已解析的 YAML 字典构造 manifest。非法就抛 :class:`ManifestError`。"""
    if not isinstance(data, dict):
        raise ManifestError(f"plugin.yaml 顶层必须是映射，实际是 {type(data).__name__}")

    name = str(data.get("name") or "").strip()
    if not name:
        raise ManifestError("plugin.yaml 缺少 name")
    if not _NAME_RE.fullmatch(name) or ".." in name:
        raise ManifestError(
            f"插件名非法：{name!r}（只允许字母数字开头，含 . _ -，长度 ≤ 64；"
            f"名字会直接变成目录名，所以必须挡住分隔符与 ..）"
        )

    raw_kind = str(data.get("kind") or "").strip()
    kind = KIND_ALIASES.get(raw_kind, raw_kind)
    if kind not in PLUGIN_KINDS:
        raise ManifestError(
            f"插件 {name} 的 kind 非法：{raw_kind!r}。"
            f"合法取值：{'/'.join(PLUGIN_KINDS)}（Hermes 别名 "
            f"{'/'.join(sorted(key for key in KIND_ALIASES if key))} 也会被归一）"
        )

    warnings: list[str] = []
    hooks_declared = _as_str_tuple(data.get("hooks"))
    unknown_hooks: list[str] = []
    for event in hooks_declared:
        try:
            normalize_event(event)
        except Exception:  # 未知事件只记，不拒——manifest 声明比运行期注册更宽松
            unknown_hooks.append(event)
    if unknown_hooks:
        warnings.append(f"hooks 里有未知事件：{'、'.join(unknown_hooks)}")

    known = set(MANIFEST_FIELDS) | set(ORBIT_EXTENSION_FIELDS)
    unknown_fields = tuple(sorted(str(key) for key in data if key not in known))

    return PluginManifest(
        name=name,
        version=_as_str(data.get("version")),
        description=_as_str(data.get("description")),
        author=_as_str(data.get("author")),
        kind=kind,
        requires_env=_env_tuple(data.get("requires_env"), required=True),
        label=_as_str(data.get("label")),
        optional_env=_env_tuple(data.get("optional_env"), required=False),
        hooks=hooks_declared,
        provides_web_providers=_as_str_tuple(data.get("provides_web_providers")),
        pip_dependencies=_as_str_tuple(data.get("pip_dependencies")),
        provides_browser_providers=_as_str_tuple(data.get("provides_browser_providers")),
        provides_tools=_as_str_tuple(data.get("provides_tools")),
        platforms=_as_str_tuple(data.get("platforms")),
        external_dependencies=_as_dict_tuple(data.get("external_dependencies")),
        homepage=_as_str(data.get("homepage")),
        entry=_as_str(data.get("entry")) or "__init__.py",
        min_orbit_version=_as_str(data.get("min_orbit_version")),
        enabled_by_default=_as_bool(data.get("enabled_by_default"), default=True),
        settings=_as_dict(data.get("settings")),
        license=_as_str(data.get("license")),
        provides_platform=_as_str_tuple(data.get("provides_platform")),
        provides_provider=_as_str_tuple(data.get("provides_provider")),
        path=path,
        root=root,
        unknown_fields=unknown_fields,
        unknown_hooks=tuple(unknown_hooks),
        warnings=tuple(warnings),
        raw=dict(data),
    )


def load_manifest(path: Path, *, root: Path | None = None) -> PluginManifest:
    """读并解析一个 `plugin.yaml`。文件坏 / YAML 坏 / 字段坏都抛 :class:`ManifestError`。"""
    file = Path(path)
    if not file.is_file():
        raise ManifestError(f"找不到 plugin.yaml：{file}")
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ManifestError(f"读不了 {file}：{exc}") from exc
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ManifestError(f"{file} 不是合法 YAML：{exc}") from exc
    return parse_manifest(data or {}, path=file, root=root or file.parent.parent)


# ── 发现 ────────────────────────────────────────────────────────────────────


def plugin_roots(settings: Any = None) -> list[Path]:
    """两类插件根（照 Hermes 的 bundled / user 二分）。

    顺序有语义：**用户插件在前**——同名时用户装的那份优先，因为"我装的东西被
    随包的同名插件顶掉"是更坏的结果。
    """
    data_dir = getattr(settings, "data_dir", None) if settings is not None else None
    base = Path(data_dir) if data_dir else ROOT / "data"
    return [base / "plugins", ROOT / "plugins"]


@dataclass(frozen=True)
class DiscoveryError:
    """发现期的一个坏插件（读不了 manifest）。**只记录，不中断发现**。"""

    path: Path
    message: str


@dataclass
class DiscoveryResult:
    """发现结果。像列表一样可迭代，同时保留错误——坏插件不该让好插件消失。"""

    manifests: list[PluginManifest] = field(default_factory=list)
    errors: list[DiscoveryError] = field(default_factory=list)
    roots: list[Path] = field(default_factory=list)

    def __iter__(self) -> Iterator[PluginManifest]:
        return iter(self.manifests)

    def __len__(self) -> int:
        return len(self.manifests)

    def __getitem__(self, index: int) -> PluginManifest:
        return self.manifests[index]

    def by_name(self, name: str) -> PluginManifest | None:
        for manifest in self.manifests:
            if manifest.name == name:
                return manifest
        return None


def discover(roots: Sequence[Path] | None = None, *, settings: Any = None) -> DiscoveryResult:
    """扫描插件根，返回全部可解析的 manifest。

    ``roots`` 留空 = :func:`plugin_roots`（`<data_dir>/plugins/` + `<repo>/plugins/`）。
    扫描深度限 3 层：Hermes 的布局是 `<group>/<provider>/plugin.yaml`（2 层），
    放宽到 3 是给"再包一层命名空间"留余量；不设限会让 `rglob` 把 venv 里几百个
    同名文件都扫进来。
    """
    search_roots = list(roots) if roots is not None else plugin_roots(settings)
    result = DiscoveryResult(roots=search_roots)
    seen: dict[str, Path] = {}
    for root in search_roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("plugin.yaml")):
            try:
                relative = path.relative_to(base)
            except ValueError:  # 符号链接跑到根外
                continue
            if len(relative.parts) > 4 or any(part in EXCLUDED_DIRS for part in relative.parts):
                continue
            try:
                manifest = load_manifest(path, root=base)
            except ManifestError as exc:
                result.errors.append(DiscoveryError(path=path, message=str(exc)))
                continue
            previous = seen.get(manifest.name)
            if previous is not None:
                result.errors.append(
                    DiscoveryError(
                        path=path,
                        message=f"插件名重复：{manifest.name}（已由 {previous} 提供，本份被忽略）",
                    )
                )
                continue
            seen[manifest.name] = path
            result.manifests.append(manifest)
    return result


# ── 能力面 ──────────────────────────────────────────────────────────────────

#: 19 种 ``ctx.register_*``（05 §1.5.1 的完整能力面）。顺序照调研的分组：
#: 工具/钩子 → 平台 → provider → dashboard/观测 → 技能/任务/上下文 → 命令。
PLUGIN_CAPABILITIES: tuple[str, ...] = (
    "register_tool",
    "register_tools",
    "register_hook",
    "register_platform",
    "register_platform_handler",
    "register_slack_action_handler",
    "register_telegram_handler",
    "register_model_provider",
    "register_memory_provider",
    "register_web_provider",
    "register_cron_provider",
    "register_dashboard_page",
    "register_observability_exporter",
    "register_dashboard_auth",
    "register_skill",
    "register_auxiliary_task",
    "register_context_engine",
    "register_command",
    "register_slash_command",
)

#: **有真实落点**的 6 种：值 = 落到哪个真实结构上。
LANDED_CAPABILITIES: dict[str, str] = {
    "register_tool": "记录进 PluginContext.tools，并在注入 tool_sink 时直接调 "
    "app/tools.py::ToolRegistry.register(tools, executor, source=...)",
    "register_tools": "同上（批量形态）",
    "register_hook": "写进 app/hooks.py::HookRegistry（17 个事件的白名单 + 优先级 + 异常隔离）",
    "register_model_provider": "写进 app/providers.py::PROVIDERS（模块级可变表，"
    "`orbit model` 与 resolve_provider 都读它）",
    "register_skill": "复制到 `<data_dir>/skills/<name>/`，app/skills.py 的 "
    "load_skill/catalog 直接读得到",
    "register_command": "构造 app/cli/spec.py::Command 并登记进 REGISTRY，"
    "`orbit <name>` 立刻可用",
}

#: **没有落点**的 13 种：值 = 具体原因（必须写清"缺哪一层"，不许含糊）。
UNSUPPORTED_CAPABILITIES: dict[str, str] = {
    "register_platform": "Orbit 没有消息平台/网关层：没有 Platform 枚举、没有适配器"
    "生命周期、没有 channel_directory 路由表，注册进来的适配器没有任何调用方",
    "register_platform_handler": "平台级原生事件处理器依赖适配器内部的派发点；"
    "Orbit 没有适配器（见 register_platform），所以没有派发点",
    "register_slack_action_handler": "Orbit 没有 Slack 适配器，也没有接收 Block Kit "
    "动作回调的 webhook 入口",
    "register_telegram_handler": "Orbit 没有 Telegram 适配器；它是 "
    "register_platform_handler 的别名，宿主侧同样缺派发点",
    "register_memory_provider": "Orbit 的记忆是本地 SQLite + FTS/向量"
    "（app/retrieval.py），没有可替换的 memory provider 注册表",
    "register_web_provider": "app/web_providers.py 的工厂表在 build_providers() 函数体"
    "里硬编码（searxng/brave/tavily/ddgs 四个类字面量），往 PROVIDER_ORDER 加名字会"
    "直接 KeyError；宿主没有可注入的 provider 注册表",
    "register_cron_provider": "app/cron.py 是内置 interval/cron 解析 + CronStore，"
    "没有『调度后端 provider』抽象",
    "register_dashboard_page": "Orbit 没有 dashboard 插件系统：没有 manifest.json + "
    "dist/ 契约，也没有 /api/plugins/<name>/ 挂载点",
    "register_observability_exporter": "app/cli/commands/observability.py 只读本地"
    "日志/事件做统计，没有 exporter 注册表与推送端",
    "register_dashboard_auth": "Orbit 的 Web 服务没有认证中间件与 session token 机制"
    "（本机单用户 loopback），dashboard auth provider 没有落点",
    "register_auxiliary_task": "Orbit 没有后台辅助任务队列（cron 是面向用户的调度器，"
    "不是插件维护任务槽）",
    "register_context_engine": "app/context.py 的三层上下文（stable/tier/volatile）是"
    "静态函数拼接，没有可替换的 engine 抽象与收集器",
    "register_slash_command": "Orbit 没有斜杠命令分发层（只有 `orbit` 子命令与 "
    "__slash-worker 占位），注册后没有解析入口",
}


@dataclass(frozen=True)
class PluginTool:
    """插件注册的一个工具。

    刻意**不**直接构造 `app/tools.py::Tool`：那会把 `app.tools` 的 import 拖进
    "只要解析 manifest 就要加载 pydantic/httpx"的境地。需要真落点时再
    :meth:`as_tool` 转换。
    """

    name: str
    description: str = ""
    parameters: dict[str, Any] | None = None
    handler: Callable[..., Any] | None = None
    override: bool = False
    source: str = ""

    def as_tool(self) -> Any:
        """转成 `app.tools.Tool`（延迟 import，避免 manifest 解析期拖重依赖）。"""
        from app.tools import Tool

        return Tool(
            name=self.name,
            description=self.description,
            parameters=self.parameters,
            source=self.source or "plugin",
        )

    def as_executor(self) -> Callable[..., Any]:
        """转成 ToolRegistry 认的执行器：``await executor(name, args, task_id, op_id)``。

        插件的 handler 允许同步或异步；两种都包成协程，让 ToolRegistry 只有一条路径。
        """
        handler = self.handler
        name = self.name

        async def executor(
            _name: str,
            args: dict[str, Any],
            _task_id: str = "",
            _operation_id: str = "",
        ) -> Any:
            if handler is None:
                raise PluginError(f"插件工具 {name} 没有绑定 handler")
            outcome = handler(args)
            if _isawaitable(outcome):
                outcome = await outcome
            return outcome

        return executor


class PluginContext:
    """插件拿到的唯一句柄（``register(ctx)`` 的入参）。

    **为什么插件不能直接 import 宿主模块**：一旦允许，宿主就再也无法回答"这个插件
    用了哪些能力"——而这正是 ``orbit plugins capabilities`` 要审计的东西
    （Hermes 原文：capabilities 是同意与审计层）。所有落点都必须经过这里。
    """

    def __init__(
        self,
        manifest: PluginManifest,
        *,
        settings: Any = None,
        hooks: HookRegistry | None = None,
        tool_sink: Any = None,
        environ: dict[str, str] | None = None,
    ) -> None:
        self.manifest = manifest
        self.settings = settings
        #: 事件注册表。留空 = 本上下文自己新建一个（测试/无宿主场景），
        #: 生产环境必须把宿主的同一个实例传进来，否则"注册到 A、发射到 B"。
        self.hooks = hooks if hooks is not None else HookRegistry()
        #: 工具落点：任何带 `register(tools, executor, source=..., override=...)` 的对象
        #: （`app/tools.py::ToolRegistry` 就是）。留空 = 只记录，不注入。
        self.tool_sink = tool_sink
        self.environ = environ if environ is not None else _environ()
        #: 各类登记表（宿主启动时读它们做注入，测试直接断言它们）。
        self.tools: dict[str, PluginTool] = {}
        self.commands: list[str] = []
        self.skills: list[str] = []
        self.model_providers: list[str] = []
        #: 实际调用过的能力（审计用）。
        self.used: list[str] = []
        #: 注册过程中被隔离掉的异常（不中断插件加载）。
        self.errors: list[str] = []

    # ── 元信息 ──
    @property
    def name(self) -> str:
        return self.manifest.name

    @property
    def data_dir(self) -> Path:
        """插件自己的持久化目录（`<data_dir>/plugin-data/<name>/`）。

        照 Hermes 的 `plugin_storage.py`：**不要**把状态写进插件安装目录——
        `plugins remove` 会删它、`plugins update` 会覆盖它，用户数据会跟着代码一起死。
        """
        return plugin_data_dir(self.name, self.settings)

    def capabilities(self) -> dict[str, Any]:
        """审计快照：用了哪些、哪些有落点、哪些注定不支持。"""
        return {
            "plugin": self.name,
            "used": list(self.used),
            "landed": sorted(set(self.used) & set(LANDED_CAPABILITIES)),
            "unsupported": sorted(set(self.used) & set(UNSUPPORTED_CAPABILITIES)),
            "tools": sorted(self.tools),
            "commands": list(self.commands),
            "skills": list(self.skills),
            "model_providers": list(self.model_providers),
            "hooks": self.hooks.events(),
            "errors": list(self.errors),
        }

    def _note(self, capability: str) -> None:
        if capability not in self.used:
            self.used.append(capability)

    def _unsupported(self, capability: str) -> UnsupportedCapability:
        self._note(capability)
        return UnsupportedCapability(capability, UNSUPPORTED_CAPABILITIES[capability])

    # ── 有落点：工具 ──
    def register_tool(
        self,
        name: str,
        description: str = "",
        parameters: dict[str, Any] | None = None,
        handler: Callable[..., Any] | None = None,
        *,
        override: bool = False,
    ) -> PluginTool:
        """注册一个 agent 工具。

        ``override=True`` 只是**意图声明**；是否真的允许覆盖内置工具由宿主的授权位
        决定（照 Hermes：`plugins.entries.<key>.allow_tool_override`，默认 false）。
        """
        self._note("register_tool")
        tool_name = str(name or "").strip()
        if not tool_name:
            raise PluginError("register_tool 需要工具名")
        source = f"plugin:{self.manifest.name}"
        tool = PluginTool(
            name=tool_name,
            description=description,
            parameters=parameters,
            handler=handler,
            override=override,
            source=source,
        )
        self.tools[tool_name] = tool
        if self.tool_sink is not None:
            self.tool_sink.register(
                [tool.as_tool()],
                tool.as_executor(),
                source=source,
                override=override,
            )
        return tool

    def register_tools(self, tools: Iterable[Any]) -> list[PluginTool]:
        """批量注册。元素可以是 :class:`PluginTool` 或"带 name/description 的字典"。"""
        self._note("register_tools")
        out: list[PluginTool] = []
        for item in tools:
            if isinstance(item, PluginTool):
                self._note("register_tool")
                self.tools[item.name] = item
                if self.tool_sink is not None:
                    self.tool_sink.register(
                        [item.as_tool()],
                        item.as_executor(),
                        source=item.source or f"plugin:{self.manifest.name}",
                        override=item.override,
                    )
                out.append(item)
                continue
            if isinstance(item, dict):
                out.append(
                    self.register_tool(
                        str(item.get("name") or ""),
                        str(item.get("description") or ""),
                        item.get("parameters"),
                        item.get("handler"),
                        override=bool(item.get("override", False)),
                    )
                )
                continue
            raise PluginError(f"register_tools 不认识这个元素：{item!r}")
        return out

    # ── 有落点：hook ──
    def register_hook(
        self, event: str, fn: Callable[[dict[str, Any]], Any], priority: int = 0
    ) -> str:
        """订阅一个生命周期事件。未知事件抛 :class:`app.hooks.UnknownHookEvent`。"""
        self._note("register_hook")
        return self.hooks.register(event, fn, priority=priority)

    # ── 有落点：模型 provider ──
    def register_model_provider(self, provider: Any, provider_id: str = "") -> str:
        """注册一个推理 provider（写进 `app/providers.py::PROVIDERS`）。

        接受 `ProviderProfile` 实例或等价字典；字典会按 dataclass 字段构造，字段名
        写错会**当场报错**——静默塞进表里只会让 `orbit model` 在别处炸。
        """
        self._note("register_model_provider")
        from app.providers import PROVIDERS, ProviderProfile

        if isinstance(provider, dict):
            try:
                profile = ProviderProfile(**provider)
            except TypeError as exc:
                raise PluginError(f"provider 定义字段不合法：{exc}") from exc
        else:
            profile = provider
        key = str(provider_id or getattr(profile, "id", "") or "").strip()
        if not key:
            raise PluginError("register_model_provider 需要 provider id")
        if not hasattr(profile, "transport"):
            raise PluginError(f"provider {key} 缺少 transport 字段（无法判断线格式）")
        PROVIDERS[key] = profile
        if key not in self.model_providers:
            self.model_providers.append(key)
        return key

    # ── 有落点：技能 ──
    def register_skill(self, name: str, path: str | Path) -> Path:
        """把插件携带的技能装进 Orbit 的技能根（`<data_dir>/skills/<name>/`）。

        **为什么是复制而不是链接**：技能根是"文件即真值"的目录，`skills.catalog()`
        每次直接读盘；链接会让"卸载插件后技能消失"变成不可预期（用户可能正在用它），
        复制则让技能在插件被删后仍然可用——这与 Hermes 的 register_skill 语义一致
        （切换 provider 时由宿主回滚，而不是靠文件系统联动）。
        """
        self._note("register_skill")
        clean = str(name or "").strip().removesuffix(".md")
        # 判据与 `app/skills.py::_validate_name` 保持一致（**不允许点号**）：
        # 技能名是 `skill_save` 的输入之一，这里放过一个 `a.b` 就会落出一个
        # 后续无法被 save_skill 更新、也无法在 /skill 里正确引用的目录。
        if not _SKILL_NAME_RE.fullmatch(clean):
            raise PluginError(f"技能名非法：{name!r}（只允许字母数字开头，含 _ -，长度 ≤ 80）")
        source = Path(path)
        if not source.exists():
            raise PluginError(f"技能路径不存在：{source}")
        target = skills_root(self.settings) / clean
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
        else:
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target / "SKILL.md")
        if clean not in self.skills:
            self.skills.append(clean)
        return target

    # ── 有落点：CLI 命令 ──
    def register_command(
        self,
        name: str,
        handler: Callable[[Any, Any], int],
        description: str = "",
    ) -> str:
        """注册一条 `orbit <name>` 子命令。

        与内置命令**重名直接报错**（照 Hermes 的冲突策略：插件不得静默覆盖内置命令）。
        """
        self._note("register_command")
        from app.cli.spec import REGISTRY, Command

        clean = str(name or "").strip().lower().lstrip("/").replace(" ", "-")
        if not clean:
            raise PluginError("register_command 需要命令名")
        if REGISTRY.get(clean) is not None:
            raise PluginError(f"命令名与已有命令冲突：{clean}（插件不得覆盖内置命令）")
        REGISTRY.add(
            Command(
                name=clean,
                help=description or f"插件 {self.manifest.name} 提供的命令",
                handler=handler,
                group="ext",
                state="done",
                needs_settings=False,
            )
        )
        self.commands.append(clean)
        return clean

    # ── 没有落点：一律显式抛，绝不静默吞 ──
    def register_platform(self, adapter: Any = None) -> None:
        raise self._unsupported("register_platform")

    def register_platform_handler(self, platform: Any = None, handler: Any = None) -> None:
        raise self._unsupported("register_platform_handler")

    def register_slack_action_handler(self, action_id: str = "", callback: Any = None) -> None:
        raise self._unsupported("register_slack_action_handler")

    def register_telegram_handler(self, *args: Any, **kwargs: Any) -> None:
        raise self._unsupported("register_telegram_handler")

    def register_memory_provider(self, provider: Any = None) -> None:
        raise self._unsupported("register_memory_provider")

    def register_web_provider(self, provider: Any = None) -> None:
        raise self._unsupported("register_web_provider")

    def register_cron_provider(self, provider: Any = None) -> None:
        raise self._unsupported("register_cron_provider")

    def register_dashboard_page(self, page: Any = None) -> None:
        raise self._unsupported("register_dashboard_page")

    def register_observability_exporter(self, exporter: Any = None) -> None:
        raise self._unsupported("register_observability_exporter")

    def register_dashboard_auth(self, provider: Any = None) -> None:
        raise self._unsupported("register_dashboard_auth")

    def register_auxiliary_task(self, task: Any = None) -> None:
        raise self._unsupported("register_auxiliary_task")

    def register_context_engine(self, engine: Any = None) -> None:
        raise self._unsupported("register_context_engine")

    def register_slash_command(self, name: str = "", handler: Any = None) -> None:
        raise self._unsupported("register_slash_command")


# ── 加载 ────────────────────────────────────────────────────────────────────


@dataclass
class LoadedPlugin:
    """一次加载的结果。``error`` 非空 = 这个插件没起来，但**进程继续跑**。"""

    manifest: PluginManifest
    context: PluginContext | None = None
    module: Any = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error


def _module_name(plugin: str) -> str:
    """插件模块的 Python 名字。照 Hermes 的 ``hermes_plugins.<name>`` 惯例。"""
    return "orbit_plugins." + re.sub(r"[^a-zA-Z0-9_]", "_", plugin)


def resolve_entry(manifest: PluginManifest) -> Path:
    """把 ``entry`` 解析成真实文件，并**拒绝逃出插件目录**（符号链接/``..``）。"""
    directory = manifest.directory
    candidate = (directory / manifest.entry).resolve()
    base = directory.resolve()
    if not candidate.is_relative_to(base):
        raise PluginError(
            f"插件 {manifest.name} 的 entry 指向插件目录之外：{manifest.entry} → {candidate}"
        )
    if candidate.is_dir():
        candidate = candidate / "__init__.py"
    if not candidate.is_file():
        raise PluginError(f"插件 {manifest.name} 的 entry 不存在：{candidate}")
    return candidate


def load(
    manifest: PluginManifest,
    *,
    hooks: HookRegistry | None = None,
    tool_sink: Any = None,
    settings: Any = None,
    environ: dict[str, str] | None = None,
) -> LoadedPlugin:
    """import 插件模块并调用它的 ``register(ctx)``。

    **失败只记错误，绝不抛给调用方**：一个坏插件不该让整个 agent 起不来
    （Hermes 的注册路径同样只 ``log.error`` 并跳过）。这是本模块最重要的一条纪律，
    所以异常在这里被就地收口成 :attr:`LoadedPlugin.error`。
    """
    context = PluginContext(
        manifest, settings=settings, hooks=hooks, tool_sink=tool_sink, environ=environ
    )
    try:
        entry = resolve_entry(manifest)
        module_name = _module_name(manifest.name)
        spec = importlib.util.spec_from_file_location(module_name, entry)
        if spec is None or spec.loader is None:
            raise PluginError(f"无法为 {entry} 构造 import 规格")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        register = getattr(module, "register", None)
        if not callable(register):
            raise PluginError(f"插件 {manifest.name} 的 {manifest.entry} 没有定义 register(ctx)")
        register(context)
    except Exception as exc:  # noqa: BLE001 —— 加载失败一律收口，不炸进程
        message = f"{type(exc).__name__}: {exc}"
        log.warning("插件 %s 加载失败（已隔离）：%s", manifest.name, message)
        context.errors.append(message)
        return LoadedPlugin(manifest=manifest, context=context, error=message)
    log.info("插件 %s 已加载（kind=%s）", manifest.name, manifest.kind)
    return LoadedPlugin(manifest=manifest, context=context, module=module)


def load_all(
    manifests: Iterable[PluginManifest],
    *,
    hooks: HookRegistry | None = None,
    tool_sink: Any = None,
    settings: Any = None,
    environ: dict[str, str] | None = None,
) -> list[LoadedPlugin]:
    """批量加载。**逐个隔离**：前一个失败不影响后一个。"""
    return [
        load(
            manifest,
            hooks=hooks,
            tool_sink=tool_sink,
            settings=settings,
            environ=environ,
        )
        for manifest in manifests
    ]


# ── plugin_guard：安装期静态扫描闸 ───────────────────────────────────────────

#: 扫描器版本（写进结果的 provenance，便于审计"这条裁决是谁做的"）。
SCANNER_VERSION = "orbit-plugin-guard-v1"

#: 结构限额（照 `plugin_guard.py`）。
#:
#: Orbit 的取舍：**越界一律按 high 严重级处理**（→ dangerous，`--force` 也不放行）。
#: Hermes 把这几项定为 medium（caution），理由是它的插件仓库里偶尔会合法地 vendor
#: 一个编译产物；Orbit 的插件是纯 Python 包、本机也不装依赖，所以 400 文件 / 10MB
#: 这种体量越界只可能是打包事故或投毒，没有"合法大包"这一档。
MAX_PLUGIN_FILE_COUNT = 400
MAX_PLUGIN_TOTAL_SIZE_KB = 10 * 1024  # 10MB
MAX_PLUGIN_SINGLE_FILE_KB = 1024  # 1MB

#: 文本类扩展名（要逐行扫内容）。
_CODE_EXTENSIONS = frozenset({".py", ".js", ".ts", ".sh", ".bash", ".rb", ".pl", ".php"})
_DOC_EXTENSIONS = frozenset({".md", ".txt", ".yaml", ".yml", ".json", ".toml", ".cfg", ".ini"})

#: dangerous 判据：**执行任意代码 / 出网 / 写系统目录**。
#: 判据来源 `plugin_guard.py` 的定位——插件"被期望"读自己的 API key 并调 provider
#: HTTP API，所以"读 env 里的 key"不能算危险；但 `subprocess` / `eval` / `os.system`
#: / 裸 socket / 写系统目录是另一个量级。
DANGEROUS_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "subprocess",
        re.compile(r"\bsubprocess\b|os\.popen\s*\(|os\.spawn[a-z]*\s*\(|\bshell\s*=\s*True\b"),
        "起子进程（插件进程内运行，子进程继承 agent 的全部权限）",
    ),
    (
        "dynamic_exec",
        re.compile(r"(?<![\w.])(?:eval|exec)\s*\(|\bexec\s*\(\s*(?:__import__|base64|bytes\.fromhex)"),
        "动态执行字符串/字节（可绕开所有静态审查）",
    ),
    (
        "os_system",
        re.compile(r"\bos\.system\s*\("),
        "os.system 直接跑 shell 命令",
    ),
    (
        "network_call",
        re.compile(
            r"\b(?:urllib\.request|urlopen|requests\.(?:get|post|put|delete|patch)|"
            r"httpx\.|aiohttp|socket\.socket|http\.client)\b"
        ),
        "直接发起网络请求（应为每个出网行为留下可审计的 provider 调用）",
    ),
    (
        "system_dir_write",
        re.compile(
            r"(?:[A-Za-z]:[\\/]{1,2}Windows|/etc/|/usr/bin|/usr/local/bin|/bin/|System32|winreg)",
            re.IGNORECASE,
        ),
        "触碰系统目录/注册表",
    ),
    (
        "reverse_shell",
        re.compile(r"/dev/tcp/|\bnc\s+-e\b|\bbash\s+-i\b|\bsocat\b.*\bexec\b"),
        "反弹 shell",
    ),
    (
        "curl_pipe_shell",
        re.compile(r"(?:curl|wget)[^\n|]*\|\s*(?:ba)?sh\b"),
        "curl/wget 管道到 shell",
    ),
)

#: caution 判据：**读凭证 / 写 data_dir 之外 / 破坏性删除**。
CAUTION_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "env_secret_read",
        re.compile(
            r"(?:os\.environ(?:\.get)?|getenv)\s*[\(\[]\s*['\"][A-Za-z0-9_]*"
            r"(?:KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Za-z0-9_]*['\"]"
        ),
        "读取凭证类环境变量（合法做法，但要让人看见）",
    ),
    (
        "credential_store",
        re.compile(r"\.ssh\b|\.aws\b|\.netrc\b|\.hermes[/\\]\.env|keyring\."),
        "访问外部凭证存储",
    ),
    (
        "destructive_delete",
        re.compile(r"\b(?:shutil\.rmtree|os\.remove|os\.unlink)\s*\(|\.unlink\s*\("),
        "删除文件/目录",
    ),
)

#: 写绝对路径的判据（单独处理：要排除落在 data_dir 里的写入）。
_ABSOLUTE_WRITE = re.compile(
    r"(?:open|write_text|write_bytes|Path)\s*\(\s*[rf]?['\"](?P<path>(?:[A-Za-z]:[\\/]|/(?!/))[^'\"]*)['\"]"
)


@dataclass(frozen=True)
class Finding:
    """一条扫描发现。字段照 `plugin_guard.py`，便于两边报告对齐。"""

    pattern_id: str
    severity: str
    category: str
    file: str
    line: int
    match: str
    description: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "severity": self.severity,
            "category": self.category,
            "file": self.file,
            "line": self.line,
            "match": self.match,
            "description": self.description,
        }


@dataclass
class ScanResult:
    """扫描结果。``verdict`` ∈ ``safe`` / ``caution`` / ``dangerous``。"""

    plugin: str
    source: str
    trust_level: str
    verdict: str
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""
    scanned_at: str = ""
    provenance: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin": self.plugin,
            "source": self.source,
            "trust_level": self.trust_level,
            "verdict": self.verdict,
            "findings": [item.as_dict() for item in self.findings],
            "summary": self.summary,
            "scanned_at": self.scanned_at,
            "provenance": dict(self.provenance),
        }


#: 严重级 → 裁决。critical/high 直接 dangerous；medium/low 是 caution。
_SEVERITY_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}


def _verdict_of(findings: Sequence[Finding]) -> str:
    if not findings:
        return "safe"
    worst = max(_SEVERITY_RANK.get(item.severity, 0) for item in findings)
    return "dangerous" if worst >= _SEVERITY_RANK["high"] else "caution"


def _is_excluded(relative: Path) -> bool:
    return any(part in EXCLUDED_DIRS for part in relative.parts)


#: 单次扫描最多看多少个目录条目。存在的理由有两个：① junction 环会让 ``rglob``
#: 无限递归；② `doctor` 被指到一个巨型目录时不该把 CLI 拖死。超限按 high 记一条，
#: 于是裁决会落到 dangerous（"结构说不清"不该被当成安全）。
_MAX_SCAN_ENTRIES = 5000


def _walk(base: Path) -> tuple[list[tuple[str, Path]], list[Finding]]:
    """遍历插件目录，返回（留在目录内的条目, 结构发现）。

    **关键点：用 ``resolve()`` 判逃逸，而不是 ``is_symlink()``。**
    Windows 的目录 junction（``mklink /J``，创建**不需要**管理员权限）在 Python 里
    ``is_symlink()`` 返回 False，但它同样能把路径指到插件目录之外。只认符号链接会
    整类漏掉这种逃逸——本机实测就是这种情况（``WinError 1314`` 建不了符号链接，
    junction 却能建），所以判据必须落在"解析后的真实路径"上。
    """
    resolved_base = base.resolve()
    safe: list[tuple[str, Path]] = []
    findings: list[Finding] = []
    seen = 0
    for item in sorted(base.rglob("*")):
        seen += 1
        if seen > _MAX_SCAN_ENTRIES:
            findings.append(
                Finding(
                    pattern_id="too_many_entries",
                    severity="high",
                    category="structural",
                    file="(directory)",
                    line=0,
                    match=f">{_MAX_SCAN_ENTRIES}",
                    description=(
                        f"目录条目数超过扫描上限 {_MAX_SCAN_ENTRIES}"
                        f"（可能是 junction 环或巨型目录）"
                    ),
                )
            )
            break
        try:
            relative = item.relative_to(base)
        except ValueError:  # 已经跑到根之外（junction 递归）
            continue
        if _is_excluded(relative):
            continue
        rel = "/".join(relative.parts)
        try:
            target = item.resolve()
        except OSError:
            findings.append(
                Finding(
                    pattern_id="broken_symlink",
                    severity="medium",
                    category="traversal",
                    file=rel,
                    line=0,
                    match="broken link",
                    description="断链或循环链接",
                )
            )
            continue
        if not target.is_relative_to(resolved_base):
            findings.append(
                Finding(
                    pattern_id="symlink_escape",
                    severity="critical",
                    category="traversal",
                    file=rel,
                    line=0,
                    match=f"link -> {target}",
                    description="符号链接/junction 指向插件目录之外（可借它读/写任意路径）",
                )
            )
            continue
        safe.append((rel, item))
    return safe, findings


def check_structure(plugin_dir: Path) -> list[Finding]:
    """结构限额与符号链接逃逸检查（照 `plugin_guard.py::_check_plugin_structure`）。

    四种情况必须被拒：文件数 > 400、总量 > 10MB、单文件 > 1MB、链接逃逸。
    """
    base = Path(plugin_dir)
    entries, findings = _walk(base)
    return _structure_findings(entries, findings)


def _structure_findings(
    entries: Sequence[tuple[str, Path]], findings: list[Finding]
) -> list[Finding]:
    """在已遍历出的安全条目上算结构限额（与遍历发现合并返回）。"""
    file_count = 0
    total_size = 0
    for rel, item in entries:
        if item.is_dir():
            continue
        file_count += 1
        try:
            size = item.stat().st_size
        except OSError:
            continue
        total_size += size
        if size > MAX_PLUGIN_SINGLE_FILE_KB * 1024:
            findings.append(
                Finding(
                    pattern_id="oversized_file",
                    severity="high",
                    category="structural",
                    file=rel,
                    line=0,
                    match=f"{size // 1024}KB",
                    description=f"单文件 {size // 1024}KB，超过 {MAX_PLUGIN_SINGLE_FILE_KB}KB 限额",
                )
            )
    if file_count > MAX_PLUGIN_FILE_COUNT:
        findings.append(
            Finding(
                pattern_id="too_many_files",
                severity="high",
                category="structural",
                file="(directory)",
                line=0,
                match=f"{file_count} files",
                description=f"插件有 {file_count} 个文件，超过 {MAX_PLUGIN_FILE_COUNT} 限额",
            )
        )
    if total_size > MAX_PLUGIN_TOTAL_SIZE_KB * 1024:
        findings.append(
            Finding(
                pattern_id="oversized_bundle",
                severity="high",
                category="structural",
                file="(directory)",
                line=0,
                match=f"{total_size // 1024}KB",
                description=(
                    f"插件总量 {total_size // 1024}KB，超过 "
                    f"{MAX_PLUGIN_TOTAL_SIZE_KB}KB（10MB）限额"
                ),
            )
        )
    return findings


def _scan_text(rel: str, text: str, *, data_dir: Path | None) -> list[Finding]:
    """扫一个文件的内容：危险判据优先，其次谨慎判据。"""
    findings: list[Finding] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pattern_id, pattern, description in DANGEROUS_PATTERNS:
            if pattern.search(line):
                findings.append(
                    Finding(
                        pattern_id=pattern_id,
                        severity="critical",
                        category="execution",
                        file=rel,
                        line=line_no,
                        match=line.strip()[:200],
                        description=description,
                    )
                )
        for pattern_id, pattern, description in CAUTION_PATTERNS:
            if pattern.search(line):
                findings.append(
                    Finding(
                        pattern_id=pattern_id,
                        severity="medium",
                        category="credential",
                        file=rel,
                        line=line_no,
                        match=line.strip()[:200],
                        description=description,
                    )
                )
        # 写绝对路径：落在 data_dir 之内不算（那是插件该干的事），之外算 caution。
        for match in _ABSOLUTE_WRITE.finditer(line):
            target = match.group("path")
            if data_dir is not None and _under(target, Path(data_dir)):
                continue
            findings.append(
                Finding(
                    pattern_id="write_outside_data_dir",
                    severity="medium",
                    category="filesystem",
                    file=rel,
                    line=line_no,
                    match=target[:200],
                    description="写入 data_dir 之外的绝对路径",
                )
            )
    return findings


def scan_plugin(
    plugin_dir: Path, source: str = "", *, data_dir: Path | None = None
) -> ScanResult:
    """扫描一个插件目录，给出 ``safe`` / ``caution`` / ``dangerous`` 裁决。

    ``data_dir`` 传进来的作用：让"写 `<data_dir>/...`"这类合法写入不被算成
    "写系统目录之外"。留空则任何绝对路径写入都记 caution（更保守）。
    """
    base = Path(plugin_dir)
    if not base.is_dir():
        raise PluginError(f"不是目录：{base}")
    # 结构检查与内容扫描共用同一次安全遍历：逃出目录的链接既不计数也不读内容
    # （读它等于替攻击者把外面的文件读进来做"扫描"）。
    entries, findings = _walk(base)
    findings = _structure_findings(entries, findings)
    for rel, item in entries:
        if item.is_dir() or item.suffix.lower() not in _CODE_EXTENSIONS | _DOC_EXTENSIONS:
            continue
        try:
            if item.stat().st_size > MAX_PLUGIN_SINGLE_FILE_KB * 1024:
                continue  # 已由结构检查记过，不再整份读进内存
            text = item.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        findings.extend(_scan_text(rel, text, data_dir=data_dir))

    verdict = _verdict_of(findings)
    from datetime import UTC, datetime

    scanned_at = datetime.now(UTC).isoformat()
    categories = sorted({item.category for item in findings})
    summary = (
        f"{base.name}: {verdict} — {len(findings)} 项发现（{', '.join(categories)}）"
        if findings
        else f"{base.name}: 干净扫描，未发现威胁"
    )
    return ScanResult(
        plugin=base.name,
        source=source or base.name,
        trust_level="community",
        verdict=verdict,
        findings=findings,
        summary=summary,
        scanned_at=scanned_at,
        provenance={
            "scanner_version": SCANNER_VERSION,
            "verdict": verdict,
            "source": source or base.name,
        },
    )


def should_allow_plugin_install(
    result: ScanResult, force: bool = False
) -> tuple[bool | None, str]:
    """把裁决映射成安装决定，返回 ``(allowed, reason)``。

    - ``(True, …)``：放行。
    - ``(None, …)``：需要显式确认（caution）。CLI 没有交互提示，所以调用方把
      ``None`` 当"要用户加 --force"处理。
    - ``(False, …)``：阻断。

    **硬规矩：``--force`` 不覆盖 dangerous**。这不是可配置的策略，是安全底线——
    ``--force`` 的语义是"我知道有警告，还是要装"，不是"我知道这是木马，还是要装"。
    """
    if result.verdict == "safe":
        return True, "允许（扫描干净）"
    if result.verdict == "caution":
        if force:
            return True, f"已用 --force 越过 caution 裁决（{len(result.findings)} 项发现）"
        return None, (
            f"需要确认（caution 裁决，{len(result.findings)} 项发现）。"
            f"确认无误后加 --force 重跑"
        )
    return False, (
        f"阻断（dangerous 裁决，{len(result.findings)} 项发现）。"
        f"--force 不覆盖 dangerous 裁决"
    )


def format_scan_report(result: ScanResult) -> str:
    """人读的扫描报告（CLI 与 doctor 共用）。"""
    lines = [
        f"插件：{result.plugin}",
        f"来源：{result.source}（trust={result.trust_level}）",
        f"裁决：{result.verdict}",
        f"扫描器：{result.provenance.get('scanner_version', SCANNER_VERSION)}",
    ]
    if not result.findings:
        lines.append("发现：无")
        return "\n".join(lines)
    lines.append(f"发现（{len(result.findings)}）：")
    for item in result.findings:
        where = f"{item.file}:{item.line}" if item.line else item.file
        lines.append(f"  - [{item.severity}] {item.pattern_id} @ {where} — {item.description}")
    return "\n".join(lines)


# ── 持久化：启用状态与插件数据目录 ──────────────────────────────────────────


def plugin_data_dir(name: str, settings: Any = None) -> Path:
    """插件自己的持久化目录：`<data_dir>/plugin-data/<name>/`。

    照 Hermes 的 `plugin_storage.py`：**不放在插件安装目录里**——那个目录会被
    `plugins remove` 删掉、被 `plugins update` 覆盖，用户数据会跟着代码一起死。
    密钥不在这套约定里（照 Hermes：credential reads 走环境变量/密钥环）。
    """
    if not _NAME_RE.fullmatch(name) or ".." in name:
        raise PluginError(f"插件名非法，不能用作数据目录：{name!r}")
    data_dir = getattr(settings, "data_dir", None) if settings is not None else None
    base = Path(data_dir) if data_dir else ROOT / "data"
    target = base / "plugin-data" / name
    target.mkdir(parents=True, exist_ok=True)
    return target


def skills_root(settings: Any = None) -> Path:
    """Orbit 的技能根（`<data_dir>/skills/`），与 `app/skills.py` 的约定一致。"""
    data_dir = getattr(settings, "data_dir", None) if settings is not None else None
    base = Path(data_dir) if data_dir else ROOT / "data"
    return base / "skills"


def plugins_root(settings: Any = None) -> Path:
    """用户插件安装根（`<data_dir>/plugins/`）。"""
    data_dir = getattr(settings, "data_dir", None) if settings is not None else None
    base = Path(data_dir) if data_dir else ROOT / "data"
    return base / "plugins"


@dataclass
class PluginState:
    """启用/禁用/授权位（落 `<data_dir>/plugins.json`）。

    为什么单独一个文件而不是塞进 `settings.json`：这一段是**插件治理状态**，
    会随 install/remove 频繁变化，和用户配置的语义不同；分开存也让"备份配置"
    不会把"某个第三方插件被授权覆盖内置工具"这件事一起带走。

    形状照 Hermes `config.yaml` 的 `plugins` 段：``enabled`` / ``disabled`` /
    ``entries.<name>.allow_tool_override``。``disabled`` 优先级高于 ``enabled``。
    """

    path: Path
    enabled: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    entries: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> PluginState:
        file = Path(path)
        data: dict[str, Any] = {}
        if file.exists():
            try:
                parsed = json.loads(file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                parsed = {}
            if isinstance(parsed, dict):
                data = parsed
        return cls(
            path=file,
            enabled=[str(item) for item in data.get("enabled") or []],
            disabled=[str(item) for item in data.get("disabled") or []],
            entries={
                str(key): dict(value)
                for key, value in (data.get("entries") or {}).items()
                if isinstance(value, dict)
            },
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "enabled": self.enabled,
            "disabled": self.disabled,
            "entries": self.entries,
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def is_enabled(self, name: str, default: bool = True) -> bool:
        if name in self.disabled:
            return False
        if name in self.enabled:
            return True
        return default

    def enable(self, name: str, *, allow_tool_override: bool | None = None) -> None:
        if name in self.disabled:
            self.disabled.remove(name)
        if name not in self.enabled:
            self.enabled.append(name)
        if allow_tool_override is not None:
            self.entries.setdefault(name, {})["allow_tool_override"] = bool(
                allow_tool_override
            )
        self.save()

    def disable(self, name: str) -> None:
        if name in self.enabled:
            self.enabled.remove(name)
        if name not in self.disabled:
            self.disabled.append(name)
        self.save()

    def allow_tool_override(self, name: str) -> bool:
        """该插件是否被授权覆盖内置工具（默认 false）。"""
        return bool(self.entries.get(name, {}).get("allow_tool_override", False))

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": list(self.enabled),
            "disabled": list(self.disabled),
            "entries": {key: dict(value) for key, value in self.entries.items()},
        }


def state_path(settings: Any = None) -> Path:
    data_dir = getattr(settings, "data_dir", None) if settings is not None else None
    base = Path(data_dir) if data_dir else ROOT / "data"
    return base / "plugins.json"


def load_state(settings: Any = None) -> PluginState:
    return PluginState.load(state_path(settings))


# ── 安装 / 移除 ─────────────────────────────────────────────────────────────


@dataclass
class InstallResult:
    """一次本地安装的结果。"""

    name: str
    target: Path
    verdict: str
    findings: int
    enabled: bool
    reason: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "target": str(self.target),
            "verdict": self.verdict,
            "findings": self.findings,
            "enabled": self.enabled,
            "reason": self.reason,
        }


def install_local(
    source: Path,
    settings: Any = None,
    *,
    force: bool = False,
    enable: bool | None = None,
) -> InstallResult:
    """从**本地目录**安装一个插件。

    流程照 `hermes plugins install` 的顺序（先扫后装，装完再决定启用）：

    1. 解析 manifest（坏 manifest 直接拒，连扫都不用扫）。
    2. 结构限额 + 静态扫描 → 三级裁决 → :func:`should_allow_plugin_install`。
    3. 复制到 `<data_dir>/plugins/<name>/`（**先扫后拷**，绝不先拷再扫）。
    4. 按 ``enable`` 参数或 ``enabled_by_default`` 落启用状态。

    远程源（git/URL/owner-repo）不在本函数职责内：本机无网络，CLI 层直接报未实现。
    """
    src = Path(source).resolve()
    if not src.is_dir():
        raise PluginError(f"安装源不是目录：{src}")
    manifest_path = src / "plugin.yaml"
    manifest = load_manifest(manifest_path, root=src)

    scan = scan_plugin(src, source=str(src), data_dir=Path(settings.data_dir) if settings else None)
    allowed, reason = should_allow_plugin_install(scan, force=force)
    if allowed is False:
        raise PluginGuardError(f"{manifest.name} 安装被拒：{reason}")
    if allowed is None:
        raise PluginGuardError(f"{manifest.name} 需要确认：{reason}")

    target = plugins_root(settings) / manifest.name
    if target.exists():
        if not force:
            raise PluginGuardError(
                f"已存在同名插件目录：{target}（要覆盖请加 --force，它会先删除旧目录）"
            )
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, target)

    state = load_state(settings)
    want_enabled = manifest.enabled_by_default if enable is None else bool(enable)
    if want_enabled:
        state.enable(manifest.name)
    else:
        state.disable(manifest.name)
    return InstallResult(
        name=manifest.name,
        target=target,
        verdict=scan.verdict,
        findings=len(scan.findings),
        enabled=want_enabled,
        reason=reason,
    )


def remove_local(name: str, settings: Any = None) -> Path:
    """删除一个**用户安装**的插件目录。

    只删 `<data_dir>/plugins/<name>/`：随包插件（`<repo>/plugins/`）不在删除范围，
    删它等于改仓库代码。返回被删的路径。
    """
    if not _NAME_RE.fullmatch(name) or ".." in name:
        raise PluginError(f"插件名非法：{name!r}")
    base = plugins_root(settings)
    target = (base / name).resolve()
    if not target.is_relative_to(base.resolve()):
        raise PluginError(f"拒绝删除插件目录之外的路径：{target}")
    if not target.is_dir():
        raise PluginError(f"没有这个用户插件：{name}（{target}）")
    shutil.rmtree(target)
    state = load_state(settings)
    state.disabled = [item for item in state.disabled if item != name]
    state.enabled = [item for item in state.enabled if item != name]
    state.entries.pop(name, None)
    state.save()
    return target


# ── doctor ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Check:
    """一条体检结论。``level`` ∈ ``ok`` / ``warn`` / ``error``。"""

    level: str
    name: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"level": self.level, "name": self.name, "detail": self.detail}


def _version_tuple(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for chunk in re.split(r"[._-]", str(value or "")):
        digits = re.match(r"^\d+", chunk)
        if digits:
            parts.append(int(digits.group(0)))
    return tuple(parts)


def orbit_version() -> str:
    """当前 Orbit 版本（读包元数据，源码直跑时退回常量）。"""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as package_version

    try:
        return package_version("orbit-personal-agent")
    except PackageNotFoundError:
        return "0.1.0"


def doctor(
    manifest: PluginManifest,
    *,
    current_version: str | None = None,
    data_dir: Path | None = None,
) -> list[Check]:
    """按**真实运行期契约**校验一个插件（不是"字段填了没有"那种形式检查）。

    检查项与理由：

    - ``kind`` / ``name``：非法就是加载不了，必须是 error。
    - ``entry``：解析不到文件 → 加载必失败 → error。
    - ``register(ctx)``：存在性要真的 import 才知道；import 失败也报 error。
    - ``hooks``：声明了未知事件 → 运行期注册必失败 → error。
    - ``min_orbit_version``：不满足 → error（插件自己声明的门槛）。
    - 必填 env 缺失 → warn（没配不代表插件坏，只代表现在起不来）。
    - 结构限额/静态扫描 → dangerous 报 error，caution 报 warn。
    - 未知字段 → warn（Hermes 的并集之外，可能是打错字）。
    """
    checks: list[Check] = []
    checks.append(Check("ok", "name", manifest.name))
    checks.append(
        Check("ok", "kind", f"{manifest.kind}（{KIND_TIMING.get(manifest.kind, '')}）")
    )
    if not manifest.version:
        checks.append(Check("warn", "version", "缺 version（审计时无法区分版本）"))
    else:
        checks.append(Check("ok", "version", manifest.version))

    try:
        entry = resolve_entry(manifest)
        checks.append(Check("ok", "entry", str(entry)))
    except PluginError as exc:
        checks.append(Check("error", "entry", str(exc)))
        entry = None

    if entry is not None:
        try:
            module_name = _module_name(manifest.name)
            spec = importlib.util.spec_from_file_location(module_name, entry)
            if spec is None or spec.loader is None:
                raise PluginError("无法构造 import 规格")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            register = getattr(module, "register", None)
            if callable(register):
                checks.append(Check("ok", "register", "可调用"))
            else:
                checks.append(Check("error", "register", "模块里没有 register(ctx)"))
        except Exception as exc:  # noqa: BLE001 —— 体检要把 import 失败报成一条结论
            checks.append(Check("error", "register", f"import 失败：{type(exc).__name__}: {exc}"))

    if manifest.unknown_hooks:
        checks.append(
            Check("error", "hooks", f"未知事件：{'、'.join(manifest.unknown_hooks)}")
        )
    elif manifest.hooks:
        checks.append(Check("ok", "hooks", f"{len(manifest.hooks)} 个事件"))

    if manifest.min_orbit_version:
        current = current_version or orbit_version()
        if _version_tuple(current) < _version_tuple(manifest.min_orbit_version):
            checks.append(
                Check(
                    "error",
                    "min_orbit_version",
                    f"要求 ≥ {manifest.min_orbit_version}，当前 {current}",
                )
            )
        else:
            checks.append(Check("ok", "min_orbit_version", manifest.min_orbit_version))

    missing = manifest.missing_env()
    if missing:
        checks.append(Check("warn", "requires_env", f"缺：{'、'.join(missing)}"))
    elif manifest.requires_env:
        checks.append(Check("ok", "requires_env", f"{len(manifest.requires_env)} 个已齐备"))

    if manifest.unknown_fields:
        checks.append(
            Check("warn", "unknown_fields", "、".join(manifest.unknown_fields))
        )

    try:
        scan = scan_plugin(manifest.directory, source=str(manifest.path), data_dir=data_dir)
    except PluginError as exc:
        checks.append(Check("error", "scan", str(exc)))
    else:
        level = {"safe": "ok", "caution": "warn", "dangerous": "error"}[scan.verdict]
        checks.append(
            Check(level, "scan", f"{scan.verdict}（{len(scan.findings)} 项发现）")
        )
    return checks


# ── 小工具 ──────────────────────────────────────────────────────────────────


def _environ() -> dict[str, str]:
    import os

    return dict(os.environ)


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, dict)):
        return ""
    return str(value).strip()


def _as_bool(value: Any, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _as_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_str_tuple(value: Any) -> tuple[str, ...]:
    """字符串 / 列表都收，统一成去空去重的元组（保持声明顺序）。"""
    if value is None:
        return ()
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value]
    else:
        return ()
    out: list[str] = []
    for item in items:
        text = item.strip()
        if text and text not in out:
            out.append(text)
    return tuple(out)


def _as_dict_tuple(value: Any) -> tuple[dict[str, Any], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(dict(item) for item in value if isinstance(item, dict))


def _env_tuple(value: Any, *, required: bool) -> tuple[EnvVar, ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ManifestError(
            f"{'requires_env' if required else 'optional_env'} 必须是列表，实际是 "
            f"{type(value).__name__}"
        )
    return tuple(parse_env_var(item, required=required) for item in value)


def _under(candidate: str, root: Path) -> bool:
    """``candidate``（可能是字面路径）是否落在 ``root`` 之内。解析失败按 False。"""
    try:
        path = Path(candidate).resolve()
        return path.is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def _isawaitable(value: Any) -> bool:
    import inspect

    return inspect.isawaitable(value)


__all__ = [
    "CAUTION_PATTERNS",
    "DANGEROUS_PATTERNS",
    "ENV_FIELDS",
    "EXCLUDED_DIRS",
    "KIND_ALIASES",
    "KIND_TIMING",
    "LANDED_CAPABILITIES",
    "MANIFEST_FIELDS",
    "MAX_PLUGIN_FILE_COUNT",
    "MAX_PLUGIN_SINGLE_FILE_KB",
    "MAX_PLUGIN_TOTAL_SIZE_KB",
    "ORBIT_EXTENSION_FIELDS",
    "PLUGIN_CAPABILITIES",
    "PLUGIN_KINDS",
    "SCANNER_VERSION",
    "UNSUPPORTED_CAPABILITIES",
    "Check",
    "DiscoveryError",
    "DiscoveryResult",
    "EnvVar",
    "Finding",
    "InstallResult",
    "LoadedPlugin",
    "ManifestError",
    "PluginContext",
    "PluginError",
    "PluginGuardError",
    "PluginManifest",
    "PluginState",
    "PluginTool",
    "ScanResult",
    "UnsupportedCapability",
    "check_structure",
    "discover",
    "doctor",
    "env_prompts",
    "format_scan_report",
    "install_local",
    "load",
    "load_all",
    "load_manifest",
    "load_state",
    "orbit_version",
    "parse_env_var",
    "parse_manifest",
    "plugin_data_dir",
    "plugin_roots",
    "plugins_root",
    "remove_local",
    "resolve_entry",
    "scan_plugin",
    "should_allow_plugin_install",
    "skills_root",
    "state_path",
]
