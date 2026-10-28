"""外观 / 主题（皮肤）：一组颜色 token，可切换、可微调、可降级为无色。

为什么需要它：Hermes 有 `hermes skin list/use/set`，而 Orbit 的终端输出此前把颜色
硬编码在各命令里（实际上 `--no-color` 一直是"本实现默认无色"的空壳）。把颜色收进
一张**固定的 token 表**，才能让"换肤"和"无色降级"变成同一件事的两种取值，而不是
散落在十几个 `print` 里的特例。

三条设计约束：

1. **token 是固定集合，皮肤只是取值。** 命令/前端只认
   ``fg`` / ``bg`` / ``accent`` / ``muted`` / ``error`` / ``success`` / ``warn``
   七个名字；新增皮肤不需要改任何调用点，缺的 token 由默认皮肤兜底。
2. **只写 ``<data_dir>/skin.json``。** 不碰 `app/config.py`（Settings 是"运行时可改
   设置"的唯一入口，皮肤是纯展示态，塞进去会让配置 schema 多出七个没人读的字段）。
   读的时候文件缺失/损坏一律退回 ``default``——**坏数据降级，不是报错**。
3. **无色降级是必须的，不是可选项。** `NO_COLOR` 环境变量存在（按 no-color.org 的
   约定：只看"存在"不看值）或显式 `--no-color` 时，所有 token 返回**空串**：
   于是"拼上颜色前缀"的代码天然变成无前缀，管道重定向、日志采集、屏幕阅读器、
   色盲用户都拿到干净的文本。空串而不是去掉字段，是为了让调用点不需要写
   ``if color:`` 分支——少一个分支就少一处漏判。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings

#: 颜色 token 的固定集合。顺序即 `orbit skin show` 的展示顺序。
TOKENS: tuple[str, ...] = ("fg", "bg", "accent", "muted", "error", "success", "warn")

#: 只接受 ``#rrggbb``。Hermes 的 `skin set` 也是这个形状——宽进会让
#: ``red`` / ``#fff`` / ``rgb(...)`` 混进来，终端渲染端各自解释，最后颜色不一致。
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")

#: 皮肤文件名（落在数据目录下，与 `settings.json` 平级）。
SKIN_FILENAME = "skin.json"


@dataclass(frozen=True)
class Skin:
    """一个内置皮肤。``colors`` 必须覆盖全部 :data:`TOKENS`。"""

    name: str
    description: str
    colors: dict[str, str]

    def color(self, token: str) -> str:
        """取某个 token 的颜色。未知 token 抛 ``KeyError``（调用点是写死的字面量）。"""
        return self.colors[token]


#: 内置皮肤。三套够用：默认（16 色终端安全）、mono（无彩色终端 / 纸面打印）、
#: solarized（低对比度偏好者常用的一套公认配色）。
SKINS: dict[str, Skin] = {
    "default": Skin(
        name="default",
        description="默认配色：16 色终端安全，深底浅字",
        colors={
            "fg": "#e6e6e6",
            "bg": "#1b1b1b",
            "accent": "#4ea1ff",
            "muted": "#8a8a8a",
            "error": "#ff5f5f",
            "success": "#3fbf6f",
            "warn": "#ffb000",
        },
    ),
    "mono": Skin(
        name="mono",
        description="单色：不依赖彩色终端，靠亮度区分层次（打印 / 截图友好）",
        colors={
            "fg": "#ffffff",
            "bg": "#000000",
            "accent": "#ffffff",
            "muted": "#a0a0a0",
            "error": "#ffffff",
            "success": "#ffffff",
            "warn": "#d0d0d0",
        },
    ),
    "solarized": Skin(
        name="solarized",
        description="Solarized Dark：低对比度、长时间阅读不刺眼",
        colors={
            "fg": "#839496",
            "bg": "#002b36",
            "accent": "#268bd2",
            "muted": "#586e75",
            "error": "#dc322f",
            "success": "#859900",
            "warn": "#b58900",
        },
    ),
}

#: 默认皮肤名。任何"不知道用哪个"的地方都退回它。
DEFAULT_SKIN = "default"


# ── 查询 ────────────────────────────────────────────────────────────────────


def list_skins() -> tuple[str, ...]:
    """内置皮肤名（稳定顺序，`default` 在最前）。"""
    return tuple(SKINS)


def resolve(name: str) -> Skin:
    """按名字取皮肤。未知名字抛 ``ValueError`` 并列出可用项——静默退回默认会让人
    以为"换肤生效了"，其实什么都没变。"""
    key = (name or "").strip().lower()
    skin = SKINS.get(key)
    if skin is None:
        raise ValueError(f"未知皮肤：{name!r}（可用：{'、'.join(list_skins())}）")
    return skin


def no_color_requested(*, flag: bool = False, environ: dict[str, str] | None = None) -> bool:
    """是否需要无色输出。

    两条来源：显式 ``--no-color``，或 ``NO_COLOR`` 环境变量**存在**。
    按 no-color.org 的约定只看"存在"——``NO_COLOR=0`` 也算要求无色，
    这样用户不必猜"什么值才算真"。
    """
    if flag:
        return True
    env = os.environ if environ is None else environ
    return "NO_COLOR" in env


def tokens(name: str, *, flag: bool = False, environ: dict[str, str] | None = None) -> dict[str, str]:
    """取某个皮肤的实际 token 值（已应用无色降级）。

    无色时**全部返回空串**而不是返回 `None` 或删掉键：调用点因此不需要
    ``if color:`` 分支（见模块 docstring 第 3 条）。
    """
    skin = resolve(name)
    if no_color_requested(flag=flag, environ=environ):
        return dict.fromkeys(TOKENS, "")
    return {token: skin.colors.get(token, SKINS[DEFAULT_SKIN].colors[token]) for token in TOKENS}


# ── 落盘 ────────────────────────────────────────────────────────────────────


def skin_path(settings: Settings) -> Path:
    return Path(settings.data_dir) / SKIN_FILENAME


def _read(settings: Settings) -> dict[str, Any]:
    """读 skin.json。缺失或损坏都当空表——**坏数据降级，不报错**。"""
    path = skin_path(settings)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def current(settings: Settings) -> str:
    """当前激活的皮肤名。文件里写了未知名字也退回默认。"""
    name = str(_read(settings).get("skin") or DEFAULT_SKIN).strip().lower()
    return name if name in SKINS else DEFAULT_SKIN


def _all_overrides(settings: Settings) -> dict[str, dict[str, str]]:
    """读"每个皮肤的逐色覆盖"：``{皮肤名: {token: 颜色}}``。坏值逐条丢弃。

    为什么按皮肤分开存：`skin set --skin mono accent ...` 的语义是"改 mono 的那个
    颜色"，**不是**"切到 mono 再改"。只存一份 `colors` 的话，这条命令会顺手把
    当前皮肤也切走——那是个会让人以为"我改错东西了"的副作用。
    """
    raw = _read(settings).get("overrides")
    if not isinstance(raw, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for name, colors in raw.items():
        if name not in SKINS or not isinstance(colors, dict):
            continue
        clean = {
            str(token): str(value)
            for token, value in colors.items()
            if token in TOKENS and isinstance(value, str) and HEX_COLOR.match(value)
        }
        if clean:
            result[str(name)] = clean
    return result


def overrides(settings: Settings, name: str = "") -> dict[str, str]:
    """某个皮肤的逐色覆盖（默认当前皮肤）。"""
    target = resolve(name).name if name else current(settings)
    return dict(_all_overrides(settings).get(target, {}))


def active_tokens(settings: Settings, *, flag: bool = False) -> dict[str, str]:
    """当前皮肤 + 覆盖后的实际 token（已应用无色降级）。"""
    resolved = tokens(current(settings), flag=flag)
    if no_color_requested(flag=flag):
        return resolved
    resolved.update(overrides(settings))
    return resolved


def _write(settings: Settings, name: str, colors: dict[str, dict[str, str]]) -> Path:
    """原子写：先写临时文件再 replace，避免半截文件让下次读取降级成默认。"""
    path = skin_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "skin": name,
        "overrides": {
            skin_name: dict(sorted(tokens.items()))
            for skin_name, tokens in sorted(colors.items())
        },
        "updated_at": datetime.now(UTC).isoformat(),
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def apply(name: str, settings: Settings) -> Path:
    """切换皮肤：只写 ``<data_dir>/skin.json``，返回写入路径。

    换皮肤会**丢掉上一套皮肤的逐色覆盖**——这是刻意的：覆盖是"在这套配色上微调"，
    带到另一套皮肤上多半是错的（在 mono 上把 fg 调成橙色毫无意义）。其它皮肤的
    覆盖保留（它们本来就没在生效）。
    """
    skin = resolve(name)
    previous = current(settings)
    kept = {
        skin_name: colors
        for skin_name, colors in _all_overrides(settings).items()
        if skin_name != previous
    }
    return _write(settings, skin.name, kept)


def set_color(settings: Settings, key: str, value: str, *, skin: str = "") -> Path:
    """微调某个皮肤的一个颜色（Hermes `skin set <key> <value> [--skin X]`）。

    目标皮肤**不会被切换**：`--skin mono` 只改 mono 的配色，当前皮肤仍是当前皮肤。
    """
    token = (key or "").strip().lower()
    if token not in TOKENS:
        raise ValueError(f"未知颜色 token：{key!r}（可用：{'、'.join(TOKENS)}）")
    color = (value or "").strip()
    if not HEX_COLOR.match(color):
        raise ValueError(f"颜色须为 #rrggbb 形式，收到 {value!r}")
    target = resolve(skin).name if skin else current(settings)
    merged = _all_overrides(settings)
    merged[target] = {**merged.get(target, {}), token: color.lower()}
    return _write(settings, current(settings), merged)


def reset(settings: Settings) -> bool:
    """清掉 skin.json，回到内置默认皮肤。返回是否真的删掉了文件。"""
    path = skin_path(settings)
    if not path.exists():
        return False
    path.unlink()
    return True


def describe(settings: Settings, name: str = "", *, flag: bool = False) -> list[tuple[str, str]]:
    """`orbit skin show` 的数据：``(token, 值)``，无色降级时值全是空串。"""
    target = resolve(name or current(settings))
    if no_color_requested(flag=flag):
        return [(token, "") for token in TOKENS]
    merged = dict(target.colors)
    merged.update(overrides(settings, target.name))
    return [(token, merged.get(token, "")) for token in TOKENS]
