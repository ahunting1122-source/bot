"""把 observability exporter 接到**真的会触发的钩子**上（MATRIX 13.4 / 15.x）。

## 缺的是什么

`app/exporters.py` 定义了 `Exporter` 协议（`hooks()` / `emit()` / `flush()`），
`JsonlExporter` 只落盘、本机可端到端验证。但它的 `hooks()` 返回的是**生命周期事件名**
（`pre_llm_call` / `post_tool_call` / …），而在此之前：

- `nodes.py` **不发钩子**（本轮刚补上）；
- 也没有任何地方把 exporter 注册进 `DEFAULT_HOOKS`。

所以 exporter 一直是"代码在、没人调"。

## 这一层做的事

`attach(exporters)` 把每个 exporter 的 `hooks()` 逐个注册到 `DEFAULT_HOOKS`，
钩子触发时调 `exporter.emit(event, payload)`。

**两条硬约束**：

1. **exporter 抛异常不能拖垮任务**——观测是旁路。记 `failures` 并继续，
   `describe()` 里能查到"哪个 exporter 失败了几次"。
2. **只注册 `available()` 为真的**——不可用的 exporter（要网络/缺凭证）注册进去
   只会每轮抛异常刷日志。但**不静默丢弃**：`describe()` 里列进 `skipped` 并给原因。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("orbit.observability")

#: 记录每个 exporter 被调了多少次、失败了多少次。进程级，`describe()` 读它。
STATS: dict[str, dict[str, int]] = {}


def attach(exporters: dict[str, Any], *, registry: Any = None) -> dict[str, Any]:
    """把 exporter 池接到钩子上，返回一份报告。

    ``registry`` 留空用 ``app.hooks.DEFAULT_HOOKS``——**必须用那个单例**：
    插件注册到 A、节点发射到 B 的话，钩子会静默不生效。
    """
    if registry is None:
        from app.hooks import DEFAULT_HOOKS

        registry = DEFAULT_HOOKS

    report: dict[str, Any] = {"attached": [], "skipped": {}, "hooks": 0}
    for name, exporter in exporters.items():
        try:
            usable = bool(exporter.available())
        except Exception as exc:  # noqa: BLE001 —— available() 自己炸了也要能继续
            report["skipped"][name] = f"available() 抛错：{exc}"
            continue
        if not usable:
            # 不可用就跳过，但**必须记下原因**——静默跳过会让"没数据"变成谜。
            #
            # **三级回退，别只找一个键**：exporter 的 `describe()` 返回的是
            # `name / kind / available / endpoint / missing_env / spool / note`——
            # **没有 `reason`**。原来只找 `reason`，于是永远走 fallback，
            # 用户看到"available() 返回 False"这种废话，而真实原因（缺哪两个 env、
            # 事件已 spool 到本地、联网后可 flush 补发）现成地躺在 `missing_env` 与
            # `note` 里却没人读。**机制真、数据真、展示层断线**——与 13.8 同族。
            why = ""
            try:
                info = exporter.describe()
                note = str(info.get("note") or "").strip()
                missing = [str(x) for x in (info.get("missing_env") or [])]
                if missing:
                    why = f"缺环境变量 {', '.join(missing)}"
                if note:
                    why = f"{why}；{note}" if why else note
                if not why:
                    why = str(info.get("reason") or "")
            except Exception:  # noqa: BLE001
                why = ""
            report["skipped"][name] = why or "available() 返回 False（exporter 没给原因）"
            continue

        stats = STATS.setdefault(name, {"calls": 0, "failures": 0})
        for event in exporter.hooks():
            def _hook(payload, _exporter=exporter, _event=event, _stats=stats, _name=name):
                _stats["calls"] += 1
                try:
                    _exporter.emit(_event, dict(payload or {}))
                except Exception as exc:  # noqa: BLE001 —— 观测是旁路
                    _stats["failures"] += 1
                    log.warning("exporter %s 处理 %s 出错（已忽略）：%s", _name, _event, exc)

            registry.register(event, _hook)
            report["hooks"] += 1
        report["attached"].append(name)
    return report


def describe(exporters: dict[str, Any] | None = None) -> dict[str, Any]:
    """自曝：接了谁、跳过了谁、各自调了多少次、失败了几次。"""
    out: dict[str, Any] = {"stats": {k: dict(v) for k, v in STATS.items()}}
    if exporters is not None:
        out["registered"] = sorted(exporters)
    return out


def reset() -> None:
    """清空计数（测试用）。"""
    STATS.clear()
