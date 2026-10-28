"""metrics 载荷的 schema 版本化：v1 / v2 + 校验 + 迁移。

## 为什么需要"schema 版本"而不是随便输出 JSON

`orbit monitoring metrics --json` 的输出会被**外部系统**消费（Prometheus 抓取、
自建看板、日志管道）。一旦字段改名或删掉，那些消费者会**静默拿到空值**——
不会报错，只是图上少了一条线，而且往往过几天才被发现。

所以：输出带 `schema_version`，字段增删必须走新版本，旧版本保留校验器，
并给出**单向迁移**（v1 → v2）。这条规矩的代价是"改字段要改两处"，
换回来的是"改字段不会静默打断别人的看板"。

## v2 相对 v1 改了什么

- `metrics` 从 `dict[str, number]` 变成 `dict[str, MetricValue]`——每个指标带
  `available` / `source` / `help`。**这是 v2 的核心**：v1 里"没有数据"和"值是 0"
  长得一模一样（都是 `0`），而这两件事完全不同——前者该报警，后者是正常。
- 新增 `collected_at`（ISO8601）与 `degraded`（哪些指标因为数据源缺失而不可用）。
"""

from __future__ import annotations

import json
from typing import Any

#: 当前输出的版本。加字段也升版本——消费者按版本判断能不能读，不靠猜。
CURRENT_VERSION = 2

#: 支持的版本。旧版本保留校验器，不删。
SUPPORTED_VERSIONS = (1, 2)


class SchemaError(ValueError):
    """载荷不符合该版本的 schema。**报错要指出是哪一层哪一项**，不是笼统"不合法"。"""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise SchemaError(message)


def validate_v1(payload: Any) -> None:
    """v1：`{schema_version: 1, metrics: {name: number}}`。"""
    _check(isinstance(payload, dict), "v1 载荷必须是 dict")
    _check(payload.get("schema_version") == 1, "v1 的 schema_version 必须是 1")
    metrics = payload.get("metrics")
    _check(isinstance(metrics, dict), "v1 的 metrics 必须是 dict")
    for name, value in metrics.items():
        _check(isinstance(name, str), f"v1 的指标名必须是字符串：{name!r}")
        _check(bool(name), f"v1 的指标名不能为空：{name!r}")
        _check(
            isinstance(value, (int, float)) and not isinstance(value, bool),
            f"v1 的指标 {name} 必须是数字，实际是 {type(value).__name__}",
        )


def validate_v2(payload: Any) -> None:
    """v2：每个指标是 `{value, available, source, help}`。"""
    _check(isinstance(payload, dict), "v2 载荷必须是 dict")
    _check(payload.get("schema_version") == 2, "v2 的 schema_version 必须是 2")
    metrics = payload.get("metrics")
    _check(isinstance(metrics, dict), "v2 的 metrics 必须是 dict")
    for name, item in metrics.items():
        _check(isinstance(name, str), f"v2 的指标名必须是字符串：{name!r}")
        _check(bool(name), f"v2 的指标名不能为空：{name!r}")
        _check(isinstance(item, dict), f"v2 的指标 {name} 必须是 dict")
        _check("available" in item, f"v2 的指标 {name} 缺 available")
        _check(isinstance(item["available"], bool), f"v2 的指标 {name} 的 available 必须是 bool")
        if item["available"]:
            value = item.get("value")
            _check(
                isinstance(value, (int, float)) and not isinstance(value, bool),
                f"v2 的指标 {name} 标了 available 却没有数字 value",
            )
        # 不可用时**必须**有 source 或 help 说明为什么——空着等于没说。
        if not item["available"]:
            _check(
                bool(item.get("source") or item.get("help")),
                f"v2 的指标 {name} 标了不可用，却没写 source/help 说明原因",
            )
    degraded = payload.get("degraded", [])
    _check(isinstance(degraded, list), "v2 的 degraded 必须是 list")


VALIDATORS = {1: validate_v1, 2: validate_v2}


def validate(payload: Any) -> int:
    """按 `schema_version` 选校验器，返回版本号。未知版本明确报错。"""
    _check(isinstance(payload, dict), "载荷必须是 dict")
    version = payload.get("schema_version")
    _check(
        version in SUPPORTED_VERSIONS,
        f"未知的 schema_version：{version!r}（支持 {SUPPORTED_VERSIONS}）",
    )
    VALIDATORS[version](payload)
    return int(version)


def migrate_v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    """把 v1 载荷升到 v2。

    **单向、无损**：v1 的数字没有"可用性"信息，所以一律标 `available=True`。
    这是刻意的——把 v1 的 0 当成"有数据且为 0"，而不是猜成"缺数据"。
    猜错方向的代价是让人去查一个不存在的数据源故障。
    """
    validate_v1(payload)
    metrics = {
        name: {
            "value": value,
            "available": True,
            "source": "v1（无来源信息）",
            "help": "",
        }
        for name, value in payload["metrics"].items()
    }
    return {
        "schema_version": 2,
        "collected_at": payload.get("collected_at", ""),
        "metrics": metrics,
        "degraded": [],
    }


def dumps(payload: dict[str, Any]) -> str:
    """校验后序列化。**先校验再输出**——不合规的载荷不该离开这个进程。"""
    validate(payload)
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
