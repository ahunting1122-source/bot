"""三级能力元数据：手工覆盖 → 运行时缓存 → 出厂 snapshot（MATRIX 4.10）。

## 为什么是"三层"，而不是一张表

同一个模型的能力（窗口多大、支不支持 tools / vision / 推理、多少钱）在三个不同
时间尺度上变化：

1. **手工覆盖**（`<data>/capabilities.json`）：用户自己知道答案，或者要强制压过
   厂商的标称值（例如网关给自家模型偷偷降了窗口）。这一层必须最先读——用户写下
   来的话不该被任何缓存盖掉。
2. **运行时缓存**（`provider_models_cache.json`，与 `app/providers.py` 的
   `record_capability()` 共用同一个文件）：联网后从 models.dev 目录刷下来的
   "当前真相"。本机无网络，所以这一层**通常是空的**——空就是空，不编。
3. **出厂 snapshot**（本文件的 `SNAPSHOT`）：随代码发布的保守下限。没有网络时
   唯一能用的那一层。

Hermes 的对应实现是 `agent/model_metadata.py`（调研 06 §7.2）：snapshot 3.27 MB
随发行版打包，cache 5.96 MB 做热更新。Orbit **不引入那份二进制资源**，所以
snapshot 只能是"我们真正确认过的那些模型"——少而准，而不是多而糊。

## 为什么 `source` 必须可见

`context_window=131072` 这个数字，可能是用户手填的、可能是昨天从目录刷的、也可能
是出厂时按"保守取小"填的。**三种来源的可信度完全不同**，用户需要知道手里这个数
能不能信，否则压缩阈值一调就 400。所以 `resolve()` 的返回值带 `source`
（`override` / `cache` / `snapshot` / `unknown`），并且**逐字段**记
`field_sources`——混源是常态（覆盖只填了窗口，价格还得看 snapshot）。

## 为什么"不知道"是合法答案

三层都没有这个模型时，返回 `source="unknown"` 且**七个字段全是 `None`**。
不填默认值的理由：一个看起来像真的 `context_window=8192` 会让调用方以为"我知道
这模型只有 8k"，于是过早压缩、或者按错误窗口算钱。`None` 逼调用方显式决定
"没数据时怎么办"，这才是诚实的接口。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from app import providers as pv

#: 能力字段白名单。做成白名单而不是自由 dict：能力字段一放开，缓存文件就会变成
#: 谁都读不懂的垃圾桶，而且拼错的字段名会静默变成"没有数据"。
CapabilityField = Literal[
    "context_window",
    "max_output",
    "supports_tools",
    "supports_vision",
    "supports_reasoning",
    "input_price",
    "output_price",
]

#: 字段顺序即 CLI 表格的展示顺序：先看窗口，再看能力，最后看钱。
CAPABILITY_FIELDS: tuple[str, ...] = (
    "context_window",
    "max_output",
    "supports_tools",
    "supports_vision",
    "supports_reasoning",
    "input_price",
    "output_price",
)

#: 三级来源，**顺序即优先级**。`unknown` 不在里面：它是"三层都没有"的结论，不是一层。
SOURCES: tuple[str, ...] = ("override", "cache", "snapshot")

#: 来源 → 人话解释。CLI 与报错都用它，避免每个调用点各写一遍。
SOURCE_NOTES: dict[str, str] = {
    "override": "手工覆盖（<data>/capabilities.json，用户说了算）",
    "cache": "运行时缓存（联网刷下来的目录，本机通常是空的）",
    "snapshot": "出厂 snapshot（随代码发布的保守下限）",
    "unknown": "三层都没有 —— 不知道，不给默认值",
}


class NetworkUnavailable(RuntimeError):
    """需要联网但本机没有网络。

    单独一个异常类型而不是 `RuntimeError` 直接抛：调用方（CLI / doctor）要能
    **区分"这台机器没网"和"代码有 bug"**，前者该提示"用 snapshot 就行"，
    后者该打栈。
    """


# ── 出厂 snapshot ──────────────────────────────────────────────────────────

#: 出厂能力表：`provider → model → 字段`。
#:
#: **覆盖范围**：`app/providers.py` 的 16 家全部登记为键；其中 14 家有默认模型，
#: 各给一条该默认模型的完整条目；`bedrock` / `custom` 的 `default_model` 是空串
#: （`models_known=False`，模型清单只能由用户或 SDK 给），所以它们的子表是空的
#: ——**空表就是"这里没有出厂答案"**，不是漏抄。
#:
#: **数值来源**（每条都能追）：
#: - 窗口 / tools / vision / reasoning：与 `providers.py` 各家 `models` 表逐字一致
#:   （A 类来自 06 调研 §3.1 的目录覆盖比例，B 类来自 §7.2 的 models.dev 样例，
#:   C 类按"保守取小"填）。测试会断言两处不许漂移。
#: - 价格：`aux_slots.PRICES`（models.dev 快照的 `cost.input` / `cost.output`，
#:   单位 USD / 1M token）。**表里没有的模型价格留 `None`**（例如
#:   `openrouter/anthropic/claude-sonnet-4.6`、`xai/grok-4.7`）——公开标价是会变的
#:   第三方数据，猜一个数比留空更危险。
#: - `max_output`：整份 06 调研里**只有一例**（§7.2 的 `gpt-5.4-mini`：
#:   `limit.output=128000`）。其余一律 `None`。这正是"不许猜"的样子。
SNAPSHOT: dict[str, dict[str, dict[str, Any]]] = {
    "deepseek": {
        "deepseek-v4-flash": {
            "context_window": 131072,
            "max_output": None,
            "supports_tools": True,
            "supports_vision": False,
            "supports_reasoning": True,
            "input_price": 0.14,
            "output_price": 0.28,
        },
    },
    "openai": {
        "gpt-5.4": {
            "context_window": 400000,
            "max_output": None,
            "supports_tools": True,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_price": 2.5,
            "output_price": 15.0,
        },
        # 06 调研 §7.2 的唯一一条 models.dev 单模型样例（含 limit.output）。
        "gpt-5.4-mini": {
            "context_window": 400000,
            "max_output": 128000,
            "supports_tools": True,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_price": 0.75,
            "output_price": 4.5,
        },
    },
    "anthropic": {
        "claude-sonnet-5": {
            "context_window": 200000,
            "max_output": None,
            "supports_tools": True,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_price": 2.0,
            "output_price": 10.0,
        },
    },
    "gemini": {
        "gemini-3.5-flash": {
            "context_window": 1048576,
            "max_output": None,
            "supports_tools": True,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_price": 1.5,
            "output_price": 9.0,
        },
    },
    "openrouter": {
        # 单价表里没有这个模型 → 价格留空（公开标价会变，不猜）。
        "anthropic/claude-sonnet-4.6": {
            "context_window": 200000,
            "max_output": None,
            "supports_tools": True,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_price": None,
            "output_price": None,
        },
    },
    "zai": {
        "glm-5.2": {
            "context_window": 131072,
            "max_output": None,
            "supports_tools": True,
            "supports_vision": False,
            "supports_reasoning": True,
            "input_price": 1.4,
            "output_price": 4.4,
        },
    },
    "kimi-coding": {
        "kimi-k3": {
            "context_window": 262144,
            "max_output": None,
            "supports_tools": True,
            "supports_vision": False,
            "supports_reasoning": True,
            "input_price": 3.0,
            "output_price": 15.0,
        },
    },
    "minimax": {
        "MiniMax-M2.7": {
            "context_window": 204800,
            "max_output": None,
            "supports_tools": True,
            "supports_vision": False,
            "supports_reasoning": True,
            "input_price": 0.3,
            "output_price": 1.2,
        },
    },
    "moonshot": {
        "moonshot-v1-128k": {
            "context_window": 131072,
            "max_output": None,
            "supports_tools": True,
            "supports_vision": False,
            "supports_reasoning": False,
            "input_price": 0.6,
            "output_price": 3.0,
        },
    },
    "qwen": {
        "qwen3-max": {
            "context_window": 131072,
            "max_output": None,
            "supports_tools": True,
            "supports_vision": False,
            "supports_reasoning": True,
            "input_price": 1.2,
            "output_price": 6.0,
        },
    },
    "groq": {
        "llama-3.3-70b-versatile": {
            "context_window": 131072,
            "max_output": None,
            "supports_tools": True,
            "supports_vision": False,
            "supports_reasoning": False,
            "input_price": 0.59,
            "output_price": 0.79,
        },
    },
    "mistral": {
        "mistral-large-latest": {
            "context_window": 131072,
            "max_output": None,
            "supports_tools": True,
            "supports_vision": True,
            "supports_reasoning": False,
            "input_price": 0.5,
            "output_price": 1.5,
        },
    },
    "xai": {
        # grok-4.7 的单价没进 `aux_slots.PRICES`（表里只有 grok-4.5）→ 留空。
        "grok-4.7": {
            "context_window": 256000,
            "max_output": None,
            "supports_tools": True,
            "supports_vision": True,
            "supports_reasoning": True,
            "input_price": None,
            "output_price": None,
        },
    },
    # default_model 为空 + models_known=False：出厂没有可登记的模型。
    "bedrock": {},
    "ollama": {
        "llama3.2": {
            "context_window": 131072,
            "max_output": None,
            "supports_tools": True,
            "supports_vision": False,
            "supports_reasoning": False,
            # 本地推理没有账单，0 是**正确**的值（与 aux_slots 的 wildcard 一致）。
            "input_price": 0.0,
            "output_price": 0.0,
        },
    },
    # 同上：端点与模型由用户填，出厂无从登记。
    "custom": {},
}


# ── 手工覆盖（第一级）──────────────────────────────────────────────────────

#: 进程内手工覆盖表，结构同 `SNAPSHOT`。
#:
#: 为什么还要一个模块级表：`<data>/capabilities.json` 是**持久化**的那份，但
#: 单测、脚本、`doctor` 的临时校验需要一个"不改磁盘也能压过一切"的入口。
#: 默认空——空就代表"没有手工结论"，而不是"全部不支持"。
OVERRIDES: dict[str, dict[str, dict[str, Any]]] = {}


def override_path(data_dir: str | Path) -> Path:
    """手工覆盖的落点。**文件名是承诺**：用户可以直接编辑它。"""
    return Path(data_dir) / "capabilities.json"


def load_overrides(data_dir: str | Path) -> dict[str, dict[str, dict[str, Any]]]:
    """读手工覆盖表。坏文件当空——覆盖表坏了只该退回下一档，不该让查询失败。"""
    path = override_path(data_dir)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return _clean_table(data)


def save_overrides(
    data_dir: str | Path, table: Mapping[str, Any]
) -> Path:
    """写手工覆盖表（只保留白名单字段，顺手丢掉垃圾键）。"""
    path = override_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(_clean_table(table), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def write_override(
    data_dir: str | Path, provider: str, model: str, values: Mapping[str, Any]
) -> Path:
    """把一条手工覆盖**合并**进 `<data>/capabilities.json` 并落盘。

    合并而不是整表替换：用户可能只改 `context_window`，其余字段仍想沿用上次的值。
    只写白名单字段——`capabilities.json` 是给人看的，混进任意键就没人看得懂了。
    """
    table = load_overrides(data_dir)
    bucket = dict(table.get(provider, {}))
    entry = dict(bucket.get(model, {}))
    entry.update({key: values[key] for key in values if key in CAPABILITY_FIELDS})
    bucket[model] = entry
    table[provider] = bucket
    return save_overrides(data_dir, table)


def _clean_table(table: Any) -> dict[str, dict[str, dict[str, Any]]]:
    """把任意读进来的结构洗成 `{provider: {model: {白名单字段: 值}}}`。"""
    out: dict[str, dict[str, dict[str, Any]]] = {}
    if not isinstance(table, dict):
        return out
    for provider, models in table.items():
        if not isinstance(provider, str) or not isinstance(models, dict):
            continue
        bucket: dict[str, dict[str, Any]] = {}
        for model, values in models.items():
            if not isinstance(model, str) or not isinstance(values, dict):
                continue
            bucket[model] = {
                key: values[key] for key in CAPABILITY_FIELDS if key in values
            }
        if bucket:
            out[provider] = bucket
    return out


def _merge(
    base: dict[str, dict[str, dict[str, Any]]],
    extra: dict[str, dict[str, dict[str, Any]]],
) -> dict[str, dict[str, dict[str, Any]]]:
    """逐层合并两张覆盖表，`extra` 优先。返回新 dict（不改入参）。"""
    merged: dict[str, dict[str, dict[str, Any]]] = {
        provider: {model: dict(values) for model, values in models.items()}
        for provider, models in base.items()
    }
    for provider, models in extra.items():
        bucket = merged.setdefault(provider, {})
        for model, values in models.items():
            bucket.setdefault(model, {}).update(values)
    return merged


# ── 查询结果 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Capability:
    """一个模型的能力快照 + **它从哪来**。

    字段默认 `None` 而不是"保守默认值"：`None` = 不知道。见模块 docstring 末段。
    """

    provider: str
    model: str
    context_window: int | None = None
    max_output: int | None = None
    supports_tools: bool | None = None
    supports_vision: bool | None = None
    supports_reasoning: bool | None = None
    input_price: float | None = None
    output_price: float | None = None
    #: `override` / `cache` / `snapshot` / `unknown`（取**优先级最高**的那一层）。
    source: str = "unknown"
    #: 逐字段来源。混源是常态，所以这张表比 `source` 更有信息量。
    field_sources: dict[str, str] = field(default_factory=dict)

    @property
    def known(self) -> bool:
        """三层里至少有一层给出过值。`False` 时所有字段都是 `None`。"""
        return self.source != "unknown"

    def value(self, name: str) -> Any:
        """按字段名取值（`name` 必须在白名单里）。"""
        if name not in CAPABILITY_FIELDS:
            raise ValueError(
                f"未知能力字段：{name}（可用：{', '.join(CAPABILITY_FIELDS)}）"
            )
        return getattr(self, name)

    def unknown_fields(self) -> list[str]:
        """哪些字段没有数据。CLI 用它把"不知道"直接摆出来。"""
        return [name for name in CAPABILITY_FIELDS if self.value(name) is None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            **{name: self.value(name) for name in CAPABILITY_FIELDS},
            "source": self.source,
            "known": self.known,
            "field_sources": dict(self.field_sources),
            "unknown_fields": self.unknown_fields(),
        }


# ── 三级查询 ────────────────────────────────────────────────────────────────


def _cache_table(data_dir: str | Path | None) -> dict[str, dict[str, dict[str, Any]]]:
    """把运行时缓存读成与 snapshot 同构的形状。

    直接复用 `providers.load_cache()`：缓存文件的路径与形状必须与
    `providers.record_capability()` 写的那份**完全一致**，否则"写进去查不到"
    这种 bug 会非常难发现。它只认 `CAPABILITY_FIELDS` 里那 4 个字段，多出来的
    （价格 / `max_output`）同样会被读出来——白名单在 `_clean_table` 里统一过滤。
    """
    if data_dir is None:
        return {}
    raw = pv.load_cache(data_dir)
    table: dict[str, dict[str, dict[str, Any]]] = {}
    for provider, bucket in raw.items():
        if not isinstance(provider, str) or not isinstance(bucket, dict):
            continue
        models = bucket.get("models")
        if isinstance(models, dict):
            table[provider] = _clean_table({provider: models}).get(provider, {})
    return table


def _override_table(
    data_dir: str | Path | None,
    overrides: Mapping[str, Any] | None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """第一级：显式入参 → 文件 → 进程内表（后者只补前者没有的字段）。"""
    if overrides is not None:
        return _clean_table(overrides)
    table = _clean_table(OVERRIDES)
    if data_dir is not None:
        table = _merge(table, load_overrides(data_dir))
    return table


def _provider_of(
    model: str, tables: Mapping[str, dict[str, dict[str, dict[str, Any]]]]
) -> str:
    """反查模型属于哪家：按 `providers.PROVIDERS` 的顺序找第一家认识它的。

    刻意按注册表顺序而不是字典遍历顺序：同一份数据在不同进程里必须给同一个答案。
    找不到就返回空串（`source` 会是 `unknown`），**不猜**。
    """
    for provider in pv.PROVIDERS:
        for tier in SOURCES:
            if model in tables.get(tier, {}).get(provider, {}):
                return provider
    return ""


def resolve(
    model: str,
    provider: str = "",
    *,
    data_dir: str | Path | None = None,
    overrides: Mapping[str, Any] | None = None,
) -> Capability:
    """按 **override → cache → snapshot** 取能力，逐字段回退，并标来源。

    - `provider` 留空时按模型名反查（`_provider_of`）；查不到就是 `unknown`。
    - 逐字段回退而不是整条回退：手工覆盖常常只填 `context_window`，价格仍该从
      snapshot 拿。整条回退会让"只覆盖一个字段"顺带抹掉另外六个。
    - 三层都没有 → 七个字段全 `None` + `source="unknown"`。**不填默认值**。
    """
    name = (model or "").strip()
    provider_id = (provider or "").strip().lower()
    tables: dict[str, dict[str, dict[str, dict[str, Any]]]] = {
        "override": _override_table(data_dir, overrides),
        "cache": _cache_table(data_dir),
        "snapshot": SNAPSHOT,
    }
    if not provider_id:
        provider_id = _provider_of(name, tables)

    entries: dict[str, dict[str, Any]] = {}
    for tier in SOURCES:
        entry = tables[tier].get(provider_id, {}).get(name)
        if isinstance(entry, dict):
            entries[tier] = entry

    values: dict[str, Any] = {}
    field_sources: dict[str, str] = {}
    for name_field in CAPABILITY_FIELDS:
        for tier in SOURCES:
            entry = entries.get(tier)
            if entry is not None and entry.get(name_field) is not None:
                values[name_field] = entry[name_field]
                field_sources[name_field] = tier
                break

    if not field_sources:
        # 没有任何一层给出值：**不填默认值**，如实说"不知道"。
        return Capability(provider=provider_id, model=name, source="unknown")

    source = SOURCES[min(SOURCES.index(tier) for tier in set(field_sources.values()))]
    return Capability(
        provider=provider_id,
        model=name,
        **{name_field: values.get(name_field) for name_field in CAPABILITY_FIELDS},
        source=source,
        field_sources=field_sources,
    )


def refresh_cache(*, data_dir: str | Path | None = None) -> dict[str, Any]:
    """刷新运行时缓存（第二级）。**本机无网络 → 一定抛 `NetworkUnavailable`。**

    为什么不"静默成功"或者"假装刷了"：这条命令的语义是"去 models.dev 拿最新目录"，
    没有网络时它不可能成功。悄悄返回旧数据会让用户以为缓存是新的，于是一个已经
    过期的窗口值会被当成今天的真相——那正是三级设计要防的事。

    真实现需要的三步（等有网络时再做，本轮不做）：
    1. `GET https://models.dev/api.json`（带 ETag 做增量）；
    2. 按 `provider → model → 字段` 写进 `providers.cache_path(data_dir)`；
    3. 写回 `at` 时间戳，`describe_source("cache")` 才能说清"什么时候刷的"。
    """
    raise NetworkUnavailable(
        "缓存刷新需要联网：要拉 models.dev 目录才能更新第二级缓存。"
        "本机没有网络，所以这一步做不到 —— 现在用的是出厂 snapshot"
        f"（`app/capabilities.py` 的 SNAPSHOT，覆盖 {len(SNAPSHOT)} 家 provider），"
        "它的数字是保守下限，不是最新标称值。要手工补某个模型的真实值，"
        "改 `<data>/capabilities.json`（第一级覆盖），不需要网络。"
    )


def describe_source(source: str) -> str:
    """来源 → 人话。未知来源原样返回，不硬编成一句话骗人。"""
    return SOURCE_NOTES.get(source, source)


__all__ = [
    "CAPABILITY_FIELDS",
    "OVERRIDES",
    "SNAPSHOT",
    "SOURCE_NOTES",
    "SOURCES",
    "Capability",
    "CapabilityField",
    "NetworkUnavailable",
    "describe_source",
    "load_overrides",
    "override_path",
    "refresh_cache",
    "resolve",
    "save_overrides",
    "write_override",
]
