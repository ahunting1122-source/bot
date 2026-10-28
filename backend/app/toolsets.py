"""工具集（toolset）过滤层：把"哪些工具能被模型看见"变成一份可审计的声明。

对标 Hermes 的 ``toolsets`` 模块（口径见 ``handoff/hermes-parity/research/02-tool-layer.md``
§1「注册表遮蔽规则」与 §3「19 功能域 / 32 toolset」）。三条设计决定，都是"为什么这么做"：

1. **工具必须显式归到某个 toolset**。Hermes 的每个工具在 ``registry.register(name=,
   toolset=)`` 时就把归属写死了，于是"禁用 web"这类操作才有意义。Orbit 原来的工具表
   只有名字和 schema，没有归属维度——想收窄暴露面只能删代码。这里用一张声明式映射
   补上这一维：**没登记的工具会落进 ``unclassified``**，而不是悄悄默认全开。
   为什么让 ``unclassified`` 是"可见的坏状态"而不是"默认放行"：暴露面失控从来不是
   某一次错误的决定，而是某个新工具没人想起来归类，于是永远躺在模型能看见的地方。

2. **per-platform 模型：默认全开，只记禁用集**。配置文件只写"关掉了什么"，
   没写的等于开着。这样新增工具不需要改任何配置就自动生效（否则每加一个工具，所有
   平台的历史配置都会把它意外关掉）。``enabled`` 列表是另一种模式——"只开这几个"，
   用于给 Telegram/Discord 这类窄场景收窄，语义照 Hermes 的 per-platform 启用集。

3. **本模块不 import ``app.tools`` / ``app.browser`` 等重模块**。它是纯数据 + 纯函数，
   于是 CLI、注册表、测试都能零成本用它，也不会踩到 playwright 之类的可选依赖。
   工具名由调用方传进来（``names`` 参数），归属判定由本模块负责。
"""

from __future__ import annotations

import copy
import importlib.util
import json
import logging
import os
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("orbit.toolsets")

#: 默认平台。``orbit tools`` 的所有子命令不带 ``--platform`` 时都落到这里，
#: 与 Hermes 的 ``--platform PLATFORM  Platform to show (default: cli)`` 一致。
DEFAULT_PLATFORM = "cli"

#: 配置文件相对 ``data_dir`` 的文件名。放在数据目录而不是仓库根，是为了让
#: "换一个数据目录"能连工具启停一起换走（与 settings.json / approvals.json 同规矩）。
CONFIG_FILENAME = "tools.json"

#: 未归类工具落点。**故意不是一个合法 toolset**：测试拿它当门禁，命中即失败。
UNCLASSIFIED = "unclassified"

#: toolset → 一行中文说明。``orbit tools list`` 直接展示它，所以每条都要说清
#: "这组工具能让模型干什么"，而不是重复 toolset 名字。
TOOLSETS: dict[str, str] = {
    "core": "对话骨架：提问（ask_user）、选择题（clarify）、任务清单（todo）、工具检索",
    "files": "本地文件读写与检索（local_*、apply_patch）",
    "shell": "在本机执行系统命令（run_command）",
    # 代码执行（MATRIX 5.17）。**单独立一个 toolset**，不并进 shell：两者风险面不同。
    # `shell` 是"跑一条系统命令"，`code` 是"跑一段模型自己写的代码"——后者能在一个
    # 进程里做任意事，收窄暴露面时往往想单独关掉它（保留 run_command 做构建/查询）。
    "code": "在子进程里执行一段代码（code_execution，非沙箱）",
    "browser": "浏览器观察与操作（browser_*）",
    "vault": "凭据填充与登录（vault_login）",
    "notion": "Notion 页面读写（notion_*）",
    "github": "GitHub 只读检索（github_*）",
    "memory": "跨会话长期记忆（memory_*）",
    "history": "历史任务检索（history_*）",
    "retrieval": "已读内容回看（retriever_search）",
    "skills": "技能库读取与沉淀（skill_*）",
    "web": "联网搜索与正文抓取（web_search / web_extract）",
    "mcp": "MCP 服务动态暴露的工具（mcp__<server>__<tool>）",
}

#: 声明式映射：工具名 → toolset。**新增工具必须在这里加一行**，否则落进
#: ``unclassified``（测试会失败）。之所以全量列出而不是只写前缀规则：
#: 这张表就是"暴露面清单"，review 时能一眼看出模型手里到底有什么。
TOOL_TOOLSET: dict[str, str] = {
    # core
    "ask_user": "core",
    # 元工具：检索可用工具。归 core 而不是新建 toolset——它不该被"禁用 web"这类
    # 操作连带关掉，否则用户关掉一个域就再也找不到别的域有什么工具了。
    "tool_search": "core",
    # 元工具：任务清单（MATRIX 5.12）与结构化澄清（5.13）。同样归 core：它们属于
    # "对话骨架"，关掉任何一个，模型就只能靠记忆干活 / 靠散文提问。
    "todo": "core",
    "clarify": "core",
    # files
    "local_list_files": "files",
    "local_search_text": "files",
    "local_read_document": "files",
    "local_write_report": "files",
    "local_write_file": "files",
    # 结构化补丁编辑（MATRIX 5.16）：改文件的另一种方式，所以归 files。
    "apply_patch": "files",
    # shell
    "run_command": "shell",
    # code（MATRIX 5.17）：显式登记而不是靠前缀——`code_execution` 没有 `code_` 前缀
    # 之外的习惯可循，靠猜前缀归类正是这张表要防的那件事。
    "code_execution": "code",
    # browser
    "browser_observe": "browser",
    "browser_navigate": "browser",
    "browser_click": "browser",
    "browser_click_at": "browser",
    "browser_fill": "browser",
    "browser_press": "browser",
    "browser_scroll": "browser",
    # vault
    "vault_login": "vault",
    # notion
    "notion_read_page": "notion",
    "notion_append_summary": "notion",
    # github
    "github_search_issues": "github",
    "github_get_issue": "github",
    # memory
    "memory_search": "memory",
    "memory_save": "memory",
    "memory_delete": "memory",
    # history
    "history_search_tasks": "history",
    "history_get_task": "history",
    # retrieval
    "retriever_search": "retrieval",
    # skills
    "skill_load": "skills",
    "skill_list": "skills",
    "skill_save": "skills",
    # web
    "web_search": "web",
    "web_extract": "web",
}

#: 前缀 → toolset。这是"新增工具自动归类"的兜底：按仓库既有命名习惯（``local_`` /
#: ``browser_`` / ``mcp__`` …）新加的工具会自然落到正确的组，不必等谁想起来改表。
#: **显式登记优先**：``TOOL_TOOLSET`` 里写过的名字不会被前缀规则改写。
#: 顺序有意义——长前缀必须排在同前缀的短前缀之前（当前无冲突，但别打乱）。
PREFIX_TOOLSETS: tuple[tuple[str, str], ...] = (
    ("mcp__", "mcp"),
    ("local_", "files"),
    ("browser_", "browser"),
    ("memory_", "memory"),
    ("skill_", "skills"),
    ("history_", "history"),
    ("notion_", "notion"),
    ("github_", "github"),
    ("web_", "web"),
)


# ── 归属判定 ────────────────────────────────────────────────────────────────


def toolset_of(name: str) -> str:
    """工具名 → toolset。查不到就返回 :data:`UNCLASSIFIED`（而不是猜一个）。"""
    explicit = TOOL_TOOLSET.get(name)
    if explicit is not None:
        return explicit
    for prefix, toolset in PREFIX_TOOLSETS:
        if name.startswith(prefix):
            return toolset
    return UNCLASSIFIED


def declared_tool_names() -> list[str]:
    """声明表里登记过的全部工具名（排序后，便于 diff 与输出稳定）。"""
    return sorted(TOOL_TOOLSET)


def declared_toolsets() -> list[str]:
    """全部 toolset 名（声明顺序即输出顺序：先具体功能域，最后 mcp）。"""
    return list(TOOLSETS)


def members(toolset: str, names: Iterable[str] | None = None) -> list[str]:
    """某个 toolset 下的工具名。``names`` 为空时只看声明表。"""
    pool = list(names) if names is not None else declared_tool_names()
    return [name for name in pool if toolset_of(name) == toolset]


# ── 配置文件读写 ────────────────────────────────────────────────────────────


def config_path(data_dir: str | Path) -> Path:
    """``data/tools.json`` 的绝对路径。"""
    return Path(data_dir) / CONFIG_FILENAME


def empty_config() -> dict[str, Any]:
    """空配置 = 全开。所有平台都缺省，因此没有任何工具被排除。"""
    return {"platforms": {}}


#: 读盘缓存：``path -> (mtime, config)``。
#:
#: 为什么需要：``ToolRegistry.specs()`` 每一轮都会走一次过滤，而它落在请求路径上。
#: 每次 turn 都读一次 JSON 文件既浪费又会在多进程下放大磁盘抖动。用 mtime 当失效
#: 指纹就够了——``orbit tools enable/disable`` 写完盘 mtime 必变，下一次读自然刷新。
_CONFIG_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def load_config(path: str | Path) -> dict[str, Any]:
    """读配置文件。**任何异常都退回"全开"**，绝不让坏配置把工具面打成空。

    理由：配置读不出来时，模型手里一个工具都没有，等于整个 agent 失效。相比之下
    "多暴露了几个工具"是更轻的故障。坏文件会打 warning 留证据。
    """
    target = Path(path)
    try:
        stamp = target.stat().st_mtime
    except OSError:
        return empty_config()
    cached = _CONFIG_CACHE.get(str(target))
    if cached is not None and cached[0] == stamp:
        return copy.deepcopy(cached[1])
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log.warning("工具集配置 %s 读取失败，按全开处理：%s", target, exc)
        return empty_config()
    config = raw if isinstance(raw, dict) else empty_config()
    if not isinstance(config.get("platforms"), dict):
        config = {**config, "platforms": {}}
    _CONFIG_CACHE[str(target)] = (stamp, copy.deepcopy(config))
    return config


def save_config(path: str | Path, config: Mapping[str, Any]) -> Path:
    """写配置文件（先写临时文件再替换，避免半截 JSON）。"""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(config), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, target)
    _CONFIG_CACHE.pop(str(target), None)
    return target


def _str_list(value: Any) -> list[str]:
    """把配置里的列表字段读成去重保序的字符串列表；坏值当空。"""
    if not isinstance(value, (list, tuple)):
        return []
    seen: dict[str, None] = {}
    for item in value:
        text = str(item).strip()
        if text:
            seen.setdefault(text, None)
    return list(seen)


def platform_state(
    config: Mapping[str, Any] | None, platform: str = DEFAULT_PLATFORM
) -> tuple[list[str], list[str]]:
    """返回该平台的 ``(disabled, enabled)``。

    ``enabled`` 为空 = 不收窄（默认全开模式）；非空 = 只暴露 ``enabled`` 里的。
    这正是 Hermes 的不对称语义：禁用是"减法"，默认不写就是开；收窄是"白名单"，
    只在用户显式列了东西时生效。
    """
    platforms = (config or {}).get("platforms")
    if not isinstance(platforms, Mapping):
        return [], []
    entry = platforms.get(platform)
    if not isinstance(entry, Mapping):
        return [], []
    return _str_list(entry.get("disabled")), _str_list(entry.get("enabled"))


def platforms_in(config: Mapping[str, Any] | None) -> list[str]:
    """配置里出现过的平台名（供 ``tools --summary`` 枚举）。"""
    platforms = (config or {}).get("platforms")
    if not isinstance(platforms, Mapping):
        return []
    return [str(name) for name in platforms]


def _hits(entry: str, name: str, toolset: str) -> bool:
    """一条配置项是否命中某个工具。

    四种粒度都要认：单个工具名（``web_search``）、toolset 名（``web``）、
    Hermes 命令面的 ``server:tool``（``github:create_issue``）、server 级
    ``mcp:<server>``。为什么不能只比工具名：Hermes 的启用单位是"内置按 toolset、
    MCP 按单工具"，两套粒度必须在同一份配置里共存。
    """
    text = entry.strip()
    if not text:
        return False
    if text.startswith("mcp__"):
        # mcp__<server>__<tool> 精确匹配；mcp__<server> 按 server 前缀匹配
        if len(text.split("__")) >= 3:
            return name == text
        return name.startswith(text + "__")
    if text.startswith("mcp:"):
        return name.startswith(f"mcp__{text[4:]}__")
    if ":" in text:
        server, _, tool = text.partition(":")
        return name == f"mcp__{server}__{tool}"
    return text == name or text == toolset


def _matching_entry(entries: Sequence[str], name: str, toolset: str) -> str:
    for entry in entries:
        if _hits(entry, name, toolset):
            return entry
    return ""


# ── 可用性探针（Hermes 的 `check_fn`，MATRIX 5.3）────────────────────────────
#
# 三层过滤的第二层：**工具在不在** 与 **工具能不能用** 是两件事。`github_*` 没配
# token 时仍然"在"，但调它必然报错；`browser_*` 没装 chromium 时同理。把这类工具
# 照常暴露给模型，只会换来一串"我试了但失败了"的回合。
#
# 但"探针失败就摘工具"是个陷阱：一次超时（负载下的 `playwright` 启动、网络抖一下）
# 就会把整个 toolset 从正在干活的 agent 上摘掉，子 agent 随即报"工具不存在"。
# Hermes 为此加了 **30s TTL + 60s 抖动宽限**（registry.py:514-519），这里照抄同一
# 组数字，理由一致。

#: 探针结果的有效期。期内重复询问直接返回缓存，不再真的探测。
PROBE_TTL_SECONDS = 30.0
#: 抖动宽限：距上次"探测成功"不到这么久时，本次失败按抖动处理——**不摘工具**，
#: 也不缓存这次失败（下次重新探）。
PROBE_GRACE_SECONDS = 60.0

#: 工具 → 它需要什么。键可以是**精确工具名**，也可以是以 ``_`` 结尾的**前缀**。
#:
#: 只登记"不满足就必然调不通"的需求。刻意**不**登记 `local_*` / `memory_*` 这类
#: 无外部依赖的工具：给它们编需求只会制造误摘的机会。
TOOL_REQUIREMENTS: dict[str, str] = {
    "browser_": "playwright",  # 浏览器全族要 playwright + chromium 在
    "notion_": "playwright",  # notion_* 走浏览器会话，同一份依赖
    "github_": "github_token",  # GitHub API 没 token 会被限流/拒绝
    "web_": "search_provider",  # 搜索要至少一个可用 provider
    "run_command": "shell",  # 要有可执行的 shell
}


def requirement_of(name: str) -> str:
    """工具名 → 需求 id（空串 = 没有声明的需求 = 无条件可用）。"""
    exact = TOOL_REQUIREMENTS.get(name)
    if exact:
        return exact
    for key, requirement in TOOL_REQUIREMENTS.items():
        if key.endswith("_") and name.startswith(key):
            return requirement
    return ""


def _check_playwright(settings: Any = None) -> tuple[bool, str]:
    """playwright 能不能用：包在 + chromium 在。

    **为什么不去真的启动一次**：``sync_playwright().start()`` 会 spawn 一个 node 驱动
    进程，几百毫秒起步、在负载下还可能挂住。探针落在每轮请求的暴露面计算路径上，
    这个代价付不起（Hermes 的 ``subprocess.run(["docker","version"])`` 教训就是这个）。
    真正"启动失败"的信号更准、更便宜——第一次调用工具时的报错。所以这里的判据是
    "依赖齐不齐"，不是"能不能跑起来"。
    """
    if importlib.util.find_spec("playwright") is None:
        return False, "playwright 未安装"
    candidates: list[Path] = []
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if override:
        candidates.append(Path(override))
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if local:
        candidates.append(Path(local) / "ms-playwright")
    candidates.append(Path.home() / ".cache" / "ms-playwright")
    for base in candidates:
        try:
            if not base.is_dir():
                continue
            if any(item.name.startswith("chromium") for item in base.iterdir()):
                return True, ""
        except OSError:
            continue
    return False, "playwright 已安装但找不到 chromium（先执行 playwright install chromium）"


def _check_github_token(settings: Any = None) -> tuple[bool, str]:
    """GitHub 工具要 token：没有它就必然 403 / 限流。"""
    raw = getattr(settings, "github_token", None) if settings is not None else None
    getter = getattr(raw, "get_secret_value", None)
    token = str(getter() or "") if callable(getter) else str(raw or "")
    token = token.strip() or os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        return False, "未配置 github_token（设置里填，或设环境变量 GITHUB_TOKEN）"
    return True, ""


def _check_search_provider(settings: Any = None) -> tuple[bool, str]:
    """web_* 要至少一个可用 provider。

    `settings` 为 None 时**按可用处理**（fail-open）：没有配置对象就无从判断，
    这时摘工具等于凭空少一个功能。
    """
    if settings is None:
        return True, ""
    try:
        from app.web_providers import available_providers

        providers = available_providers(settings)
    except Exception as exc:  # noqa: BLE001 - 探针本身不该把命令面打崩
        return True, f"provider 探测失败（按可用处理）：{exc}"
    if any(item.get("available") for item in providers):
        return True, ""
    return False, "没有可用的搜索 provider（配置 searxng_url / brave_search_api_key 等）"


def _check_shell(settings: Any = None) -> tuple[bool, str]:
    """run_command 要有一个能执行的 shell。"""
    if os.name == "nt":
        return True, ""  # cmd.exe 是系统组件，不存在的话整个平台都不成立
    return (True, "") if Path("/bin/sh").exists() else (False, "找不到 /bin/sh")


#: 需求 id → 检查函数。返回 ``(可用?, 不可用原因)``。
REQUIREMENT_CHECKS = {
    "playwright": _check_playwright,
    "github_token": _check_github_token,
    "search_provider": _check_search_provider,
    "shell": _check_shell,
}


@dataclass
class ProbeState:
    """一次探针的结论。"""

    available: bool
    reason: str
    checked_at: float
    #: 上次**判定可用**的时刻。``None`` = 从未成功过（区别于"成功时刻恰好是 0"，
    #: 所以这里不用 0 当哨兵值——假时钟测试会踩到）。
    last_good_at: float | None = None


#: 探针缓存（进程内）。键是工具名：同一个 requirement 被多个工具共享时各记一份，
#: 这样"某个工具名被摘掉"不会牵连同组的其它工具（Hermes 的 key 是 ``(fn, scope)``）。
_PROBE_CACHE: dict[str, ProbeState] = {}


def probe(
    name: str,
    settings: Any = None,
    *,
    now: float | None = None,
    trigger: bool = True,
) -> tuple[bool, str]:
    """问一句"这个工具现在可用吗"。返回 ``(可用?, 不可用原因)``。

    ``trigger=False`` 是**只读缓存**：过期或没探测过一律按"可用"返回，绝不触发真实
    探测。暴露面计算（:func:`resolve`）必须走这条路——它每轮都会跑，而探针会读盘、
    读密钥、glob 浏览器目录。

    ``trigger=True`` 的语义照 Hermes：

    1. 缓存新鲜（< :data:`PROBE_TTL_SECONDS`）→ 直接返回，**不重复探测**；
    2. 探测成功 → 记缓存，并更新 `last_good_at`；
    3. 探测失败但距上次成功 < :data:`PROBE_GRACE_SECONDS` → 判为抖动，返回可用且
       **不缓存失败**（下次重新探）；
    4. 否则缓存失败并返回不可用。
    """
    requirement = requirement_of(name)
    if not requirement:
        return True, ""
    check = REQUIREMENT_CHECKS.get(requirement)
    if check is None:  # 声明了需求却没有检查器：按可用处理，别让笔误变成摘工具
        return True, ""
    stamp = time.monotonic() if now is None else now
    state = _PROBE_CACHE.get(name)
    if state is not None and stamp - state.checked_at < PROBE_TTL_SECONDS:
        return state.available, state.reason
    if not trigger:
        return True, ""
    try:
        ok, reason = check(settings)
    except Exception as exc:  # noqa: BLE001 - 探针炸了算失败，但不该把调用方打崩
        ok, reason = False, f"探针异常：{type(exc).__name__}: {exc}"
    if ok:
        _PROBE_CACHE[name] = ProbeState(True, "", stamp, stamp)
        return True, ""
    last_good = state.last_good_at if state is not None else None
    if last_good is not None and stamp - last_good < PROBE_GRACE_SECONDS:
        log.info("工具 %s 的探针失败（%s），但距上次成功不足宽限期，按抖动处理", name, reason)
        return True, ""
    _PROBE_CACHE[name] = ProbeState(False, reason, stamp, last_good)
    # 用 info 而不是 warning：CLI（`orbit tools probe`）会把不可用原因直接打印给人，
    # 再往 stderr 打一份 warning 就是重复噪声。长期运行的 agent 侧需要这条记录，
    # info 级别在默认日志配置里照样落盘。
    log.info("工具 %s 不可用：%s", name, reason)
    return False, reason


def cached_probe(name: str, *, now: float | None = None) -> tuple[bool, str]:
    """:func:`probe` 的只读形式（不触发探测）。"""
    return probe(name, trigger=False, now=now)


def probe_all(
    names: Iterable[str], settings: Any = None, *, now: float | None = None
) -> list[dict[str, Any]]:
    """逐个探测（会触发真实检查），返回可直接展示的行。"""
    rows: list[dict[str, Any]] = []
    for name in names:
        requirement = requirement_of(name)
        available, reason = probe(name, settings, now=now) if requirement else (True, "")
        rows.append(
            {
                "name": name,
                "toolset": toolset_of(name),
                "requirement": requirement,
                "available": available,
                "reason": reason,
            }
        )
    return rows


def invalidate_probe_cache() -> None:
    """清空探针缓存（``orbit tools enable/disable`` 之后应当调用）。"""
    _PROBE_CACHE.clear()


# ── 解析 / 视图 ─────────────────────────────────────────────────────────────


def resolve(
    names: Iterable[str],
    platform: str = DEFAULT_PLATFORM,
    config: Mapping[str, Any] | None = None,
    *,
    now: float | None = None,
) -> list[str]:
    """算出该平台**实际暴露**的工具名（保持传入顺序）。

    判定顺序（先禁用、再收窄、最后看可用性）：
    1. 命中 ``disabled`` → 排除；
    2. ``enabled`` 非空且不命中 → 排除（白名单模式）；
    3. 探针缓存里**明确判定不可用** → 排除；
    4. 其余暴露。

    禁用优先于启用：同时写了 ``disable web`` 和 ``enable web_search`` 时，显式禁用赢。
    理由是"关掉"通常是安全决策，"开着"只是便利决策，冲突时该选保守的那个。

    第 3 步**只读缓存**（``probe(trigger=False)``），绝不触发真实探测：``resolve``
    落在每轮请求的暴露面计算上，而探针会读盘、读密钥、glob 浏览器目录。没探测过
    或结果过期时按"可用"处理——**宁多暴露一个调不通的工具，也不因为没探测就把工具
    悄悄摘掉**。真正的探测由 ``orbit tools probe`` / ``probe_all`` 触发。
    """
    disabled, enabled = platform_state(config, platform)
    exposed: list[str] = []
    for name in _config_exposure(names, platform, config):
        if not cached_probe(name, now=now)[0]:
            continue
        exposed.append(name)
    return exposed


def _config_exposure(
    names: Iterable[str],
    platform: str = DEFAULT_PLATFORM,
    config: Mapping[str, Any] | None = None,
) -> list[str]:
    """**只看配置**的暴露面（不看探针）。

    与 :func:`resolve` 分开是因为两者语义不同：``resolve`` 回答"此刻能暴露什么"，
    这个函数回答"配置允许暴露什么"。``post_setup_snapshot`` 必须用后者——把
    "此刻恰好不可用"（没装 chromium、没配 token）写进配置快照，等于把一次瞬时状态
    固化成永久禁用，之后用户装好了依赖，工具还是不会回来。
    """
    disabled, enabled = platform_state(config, platform)
    exposed: list[str] = []
    for name in names:
        toolset = toolset_of(name)
        if disabled and _matching_entry(disabled, name, toolset):
            continue
        if enabled and not _matching_entry(enabled, name, toolset):
            continue
        exposed.append(name)
    return exposed


def describe(
    platform: str = DEFAULT_PLATFORM,
    config: Mapping[str, Any] | None = None,
    names: Iterable[str] | None = None,
    *,
    settings: Any = None,
    probe_fresh: bool = True,
) -> list[dict[str, Any]]:
    """``orbit tools list`` 的完整视图：工具名 / toolset / 是否暴露 / 为什么被排除。

    ``reason`` 是给人看的诊断信息——"看不到这个工具"必须能当场解释，否则用户只能
    靠猜是没注册、被禁用、还是被白名单挡了。

    ``note`` 是**不改变暴露结果**的提醒（目前只有"未归类"一种）。为什么未归类不直接
    排除：``resolve`` 里若按"没归类就不暴露"处理，新加的工具会静默消失，模型只会报
    "工具不存在"，排查方向完全跑偏。所以运行时放行 + 打标，由测试和 ``tools list``
    把问题顶到明面上。

    ``probe_fresh``（默认开）：``tools list`` 是**人主动发起的一次性诊断命令**，
    在这里跑一次探针是合理的——不然"为什么这个工具用不了"永远只能靠猜。反过来
    ``resolve``（每轮请求都跑）只读缓存，两者刻意不对称。
    """
    pool = list(names) if names is not None else declared_tool_names()
    disabled, enabled = platform_state(config, platform)
    rows: list[dict[str, Any]] = []
    for name in pool:
        toolset = toolset_of(name)
        reason = ""
        if disabled and (entry := _matching_entry(disabled, name, toolset)):
            reason = f"禁用集命中「{entry}」"
        elif enabled and not _matching_entry(enabled, name, toolset):
            reason = "不在 enabled 白名单里"
        else:
            available, unavailable_reason = (
                probe(name, settings) if probe_fresh else cached_probe(name)
            )
            if not available:
                reason = f"不可用：{unavailable_reason}"
        note = ""
        if toolset == UNCLASSIFIED:
            note = "未归类：请在 app/toolsets.py 的 TOOL_TOOLSET 里登记"
        rows.append(
            {
                "name": name,
                "toolset": toolset,
                "exposed": not reason,
                "reason": reason,
                "note": note,
                "requirement": requirement_of(name),
            }
        )
    return rows


# ── 配置变更（给 `orbit tools enable/disable` 用）────────────────────────────


def toggle(
    config: Mapping[str, Any] | None,
    platform: str,
    entries: Sequence[str],
    *,
    enable: bool,
) -> dict[str, Any]:
    """在配置里启用/禁用若干条目，返回**新**配置（不改原对象）。

    语义细节（照 Hermes 的启用/禁用模型）：
    - 禁用：加进 ``disabled``，并从 ``enabled`` 里摘掉（否则两条规则打架）；
    - 启用：从 ``disabled`` 里摘掉；**若当前处于白名单模式**（``enabled`` 非空），
      再把它加进 ``enabled``——否则"启用"在收窄模式里毫无效果，用户会以为命令坏了。
    """
    updated = copy.deepcopy(dict(config or empty_config()))
    platforms = updated.setdefault("platforms", {})
    if not isinstance(platforms, dict):  # 坏配置就地修好，而不是整份丢掉
        platforms = {}
        updated["platforms"] = platforms
    state = platforms.setdefault(platform, {})
    if not isinstance(state, dict):
        state = {}
        platforms[platform] = state
    disabled = _str_list(state.get("disabled"))
    enabled = _str_list(state.get("enabled"))
    for entry in entries:
        text = str(entry).strip()
        if not text:
            continue
        if enable:
            disabled = [item for item in disabled if item != text]
            if enabled and text not in enabled:
                enabled.append(text)
        else:
            if text not in disabled:
                disabled.append(text)
            enabled = [item for item in enabled if item != text]
    state["disabled"] = disabled
    state["enabled"] = enabled
    return updated


def is_known(entry: str) -> bool:
    """这个条目是不是一个**认得的**工具名 / toolset 名 / MCP 写法。

    存在的意义是让拼错的名字当场报错。静默接受 ``websearch`` 然后什么都不发生，
    是这类命令最坏的失败方式——用户以为关掉了，其实全开着。
    """
    text = entry.strip()
    if not text:
        return False
    if text in TOOLSETS or text in TOOL_TOOLSET:
        return True
    if text.startswith("mcp:") or text.startswith("mcp__"):
        return True  # 动态前缀：连接前无法枚举，只能放行
    if ":" in text:  # Hermes 的 server:tool 写法
        return True
    for prefix, _toolset in PREFIX_TOOLSETS:
        if text.startswith(prefix):
            return True
    return False


def suggest(entry: str, limit: int = 3) -> list[str]:
    """给拼错的条目找几个相近的候选（``difflib`` 是标准库，不引依赖）。"""
    from difflib import get_close_matches

    pool = list(TOOLSETS) + declared_tool_names()
    return get_close_matches(entry.strip(), pool, n=limit, cutoff=0.6)


def post_setup_snapshot(
    platform: str = DEFAULT_PLATFORM,
    config: Mapping[str, Any] | None = None,
    names: Iterable[str] | None = None,
) -> dict[str, Any]:
    """``orbit tools post-setup`` 的输出：一段可直接贴进 ``data/tools.json`` 的快照。

    照 Hermes ``tools post-setup`` 的定位——"给 dashboard / 脚本用的稳定、非交互产物"。
    Hermes 那个跑的是供应商安装钩子（本机无网络，装不了任何东西），所以 Orbit 这边
    落成**当前平台的启停快照**：把"此刻模型能看见什么"固化成一段 JSON，贴回配置即可
    复现同一份暴露面。这比打印一句"完成"有用得多。
    """
    pool = list(names) if names is not None else declared_tool_names()
    disabled, enabled = platform_state(config, platform)
    exposed = _config_exposure(pool, platform, config)
    exposed_set = set(exposed)
    return {
        "platforms": {
            platform: {
                "disabled": sorted(
                    {*disabled, *(name for name in pool if name not in exposed_set)}
                ),
                "enabled": enabled,
            }
        }
    }
