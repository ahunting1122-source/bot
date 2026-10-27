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
import json
import logging
import os
from collections.abc import Iterable, Mapping, Sequence
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
    "core": "对话骨架：向用户提问（ask_user），任何时候都该在",
    "files": "本地文件读写与检索（local_*）",
    "shell": "在本机执行系统命令（run_command）",
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
    # files
    "local_list_files": "files",
    "local_search_text": "files",
    "local_read_document": "files",
    "local_write_report": "files",
    "local_write_file": "files",
    # shell
    "run_command": "shell",
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


# ── 解析 / 视图 ─────────────────────────────────────────────────────────────


def resolve(
    names: Iterable[str],
    platform: str = DEFAULT_PLATFORM,
    config: Mapping[str, Any] | None = None,
) -> list[str]:
    """算出该平台**实际暴露**的工具名（保持传入顺序）。

    判定顺序（先禁用、再收窄）：
    1. 命中 ``disabled`` → 排除；
    2. ``enabled`` 非空且不命中 → 排除（白名单模式）；
    3. 其余暴露。

    禁用优先于启用：同时写了 ``disable web`` 和 ``enable web_search`` 时，显式禁用赢。
    理由是"关掉"通常是安全决策，"开着"只是便利决策，冲突时该选保守的那个。
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
) -> list[dict[str, Any]]:
    """``orbit tools list`` 的完整视图：工具名 / toolset / 是否暴露 / 为什么被排除。

    ``reason`` 是给人看的诊断信息——"看不到这个工具"必须能当场解释，否则用户只能
    靠猜是没注册、被禁用、还是被白名单挡了。

    ``note`` 是**不改变暴露结果**的提醒（目前只有"未归类"一种）。为什么未归类不直接
    排除：``resolve`` 里若按"没归类就不暴露"处理，新加的工具会静默消失，模型只会报
    "工具不存在"，排查方向完全跑偏。所以运行时放行 + 打标，由测试和 ``tools list``
    把问题顶到明面上。
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
    exposed = resolve(pool, platform, config)
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
