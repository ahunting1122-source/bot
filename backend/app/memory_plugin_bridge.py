"""插件记忆 provider 的**读 / 写 / 记账**桥（MATRIX 13.4）。

## 之前缺的是什么

`app/plugin_capabilities.py` 把 `register_memory_provider` 列为 **exposed**（不是 wired）：
插件能注册一个 provider 对象，但**没有任何生产代码去读它或写它**。

更要紧的是：`app/memory_providers.py::build_router()` **零生产调用方**——
那套「15 个 provider 名字 + 路由器」的抽象从来没被装配过。生产里的记忆就是
一个 `MEMORY.md` 文件（`memory_prompt.render_block` 读、`memory_write` 三段闸写）。

## 监视子agent 给的**三条硬要求**（缺一条就只能算「一半」，而一半比没有更糟）

1. **写路径**要真的写进插件 provider（在三段闸**之后**）；
2. **读路径**要真的查它——**只写不读是半个功能，比没有更糟**（用户以为记住了，模型永远看不到）；
3. **失败要隔离，但要记账**——静默吞掉失败会让「记忆坏了」变成不可诊断。

## 顺序与权威（这条不能搞反）

**`MEMORY.md` 是权威，provider 是镜像。**
顺序 = 先写 `MEMORY.md`（过三段闸）→ 成功之后才镜像 → **镜像失败只记账，不回滚**。

为什么：`MEMORY.md` 是用户能直接看、能 `git diff`、能手工修的那份。把它降级成
「和 provider 平级」会让"记忆到底存在哪"变得不可回答；而镜像失败就回滚，
等于让一个可选插件能阻止核心记忆写入——**方向错了**。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("orbit.memory")

#: 装配层装好的路由器（`memory_providers.build_router`）。None = 没配，一切照旧。
_ROUTER: Any = None

#: **哪些 provider 算「插件来源」**。由装配层显式给——它是唯一知道
#: `plugin_host.memory_providers()` 从哪来的那一层。
#:
#: 早先想用 `getattr(provider, "plugin", False)` 自动识别，但插件 provider 没有这个
#: 属性——**那个过滤条件会让所有插件 provider 都被排除**，功能静默失效。
#: 现在显式传，并且给一个 `name` 兜底：装配层忘了传，就一个都不算（宁可少读，
#: 也不要读错——把内置 provider 当插件 provider 镜像会写重复）。
_PLUGIN_PROVIDERS: list[Any] = []

#: 记账：读写各记成功/失败次数与最近一次错误。**失败必须可见**。
STATS: dict[str, Any] = {
    "write_ok": 0,
    "write_failed": 0,
    "read_ok": 0,
    "read_failed": 0,
    "last_write_error": "",
    "last_read_error": "",
}


def configure(router: Any, *, plugin_providers: list[Any] | None = None) -> None:
    """装配层调一次：把带插件 provider 的路由器挂上来。

    `plugin_providers` 必须是**装配层从 `plugin_host` 拿到的那些**——
    本模块无法自己判断谁是插件 provider（试过用属性自动识别，结果全被排除）。
    """
    global _ROUTER, _PLUGIN_PROVIDERS
    _ROUTER = router
    _PLUGIN_PROVIDERS = list(plugin_providers or [])


def reset() -> None:
    global _ROUTER, _PLUGIN_PROVIDERS
    _ROUTER = None
    _PLUGIN_PROVIDERS = []
    STATS.update(
        {
            "write_ok": 0,
            "write_failed": 0,
            "read_ok": 0,
            "read_failed": 0,
            "last_write_error": "",
            "last_read_error": "",
        }
    )


def active() -> Any:
    return _ROUTER


def _plugin_providers() -> list[Any]:
    """**装配层显式指定的**插件 provider（不是靠属性猜的，见 `_PLUGIN_PROVIDERS`）。"""
    return list(_PLUGIN_PROVIDERS)


def mirror(key: str, value: str) -> None:
    """把一条记忆**镜像**到所有插件 provider。

    **在 `MEMORY.md` 写成功之后调**（见模块 docstring 的顺序）。
    单个 provider 失败**不影响其它**，也不回滚——但**必须记账**。
    """
    for provider in _plugin_providers():
        name = str(getattr(provider, "name", "?"))
        try:
            provider.save(key, value)
        except Exception as exc:  # noqa: BLE001 —— 隔离：一个插件坏了不牵连别的
            STATS["write_failed"] += 1
            STATS["last_write_error"] = f"{name}: {type(exc).__name__}: {exc}"
            log.warning("插件记忆写入失败（%s，已记账不回滚）：%s", name, exc)
        else:
            STATS["write_ok"] += 1


def recall(query: str, *, limit: int = 5) -> str:
    """从插件 provider 里查相关记忆，渲染成一段可注入的文本。

    **返回空串表示「没有额外记忆」**——调用方据此决定是否拼进提示。
    与 `mirror` 一样：单个 provider 失败被隔离并记账。
    """
    chunks: list[str] = []
    for provider in _plugin_providers():
        name = str(getattr(provider, "name", "?"))
        try:
            hits = list(provider.search(query, limit=limit) or [])
        except Exception as exc:  # noqa: BLE001
            STATS["read_failed"] += 1
            STATS["last_read_error"] = f"{name}: {type(exc).__name__}: {exc}"
            log.warning("插件记忆检索失败（%s，已记账）：%s", name, exc)
            continue
        STATS["read_ok"] += 1
        for hit in hits:
            text = str(getattr(hit, "value", "") or hit or "").strip()
            if text:
                chunks.append(f"- [{name}] {text}")
    if not chunks:
        return ""
    return "# 插件记忆（外部 provider 检索结果）\n" + "\n".join(chunks)


def describe() -> dict[str, Any]:
    """自曝：装没装路由器、有几个插件 provider、读写各成功失败多少次，
    以及**内置 provider 里几个真能用**。

    **为什么要报内置那一半**：`PROVIDER_NAMES` 有 15 个名字，很容易被读成
    「15 个 provider 都能用」。本机实测只有 3 个可用（`file` / `jsonl` /
    `holographic`），另外 12 个要网络或凭证。**不报这个数字，读者会高估能力。**
    """
    providers = _plugin_providers()
    # `total` 来自 `PROVIDER_NAMES` 这个常量，**与装没装路由器无关**——
    # 没装路由器时也该如实报「15 个名字、0 个已装」，而不是报 0 个名字。
    builtin: dict[str, Any] = {"total": 0, "available": 0, "names": []}
    try:
        from app.memory_providers import PROVIDER_NAMES

        builtin["total"] = len(PROVIDER_NAMES)
    except Exception as exc:  # noqa: BLE001
        builtin["error"] = f"{type(exc).__name__}: {exc}"
    if _ROUTER is not None:
        try:
            usable: list[str] = []
            for name in PROVIDER_NAMES:
                provider = None
                try:
                    provider = next(
                        (p for p in getattr(_ROUTER, "providers", ()) if p.name == name), None
                    )
                except Exception:  # noqa: BLE001 —— 自省失败不该让 describe 崩
                    provider = None
                if provider is None:
                    continue
                try:
                    if provider.available():
                        usable.append(name)
                except Exception:  # noqa: BLE001
                    continue
            builtin["available"] = len(usable)
            builtin["names"] = usable
        except Exception as exc:  # noqa: BLE001
            builtin["error"] = f"{type(exc).__name__}: {exc}"
    return {
        "configured": _ROUTER is not None,
        "plugin_providers": [str(getattr(p, "name", "?")) for p in providers],
        "builtin": builtin,
        "stats": dict(STATS),
    }
