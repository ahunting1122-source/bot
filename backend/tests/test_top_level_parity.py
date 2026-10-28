"""顶层命令层「Hermes 有而 Orbit 没有 = 0」（目标判据的机械化版本）。

## 为什么这条测试重要

目标原话是「Hermes 有的功能 Orbit 都有」。在**命令面**上，这句话可以机械化验证：
Hermes 的 77 个顶层命令名（来自 `research/_raw/cmd-*.txt` 的文件名，那是逐条
`--help` 转储，**实证而非推断**）**每一个都在 Orbit 的注册表里**。

**它不证明功能对齐**——名字对上不等于语义对上。但它证明「没有整条命令缺失」，
而那是最粗也最致命的一类缺口。

## 为什么不用 `Command.hermes` 字段

`Command.hermes` 大部分为空，而 `parity_rows()` 在它为空时**回落到命令名**——
于是永远非空、永远判不出「是不是 Hermes 有」。**这与
`getattr(settings, "max_turns", None)` 是同一类病：fallback 让「没填」看起来像「填了」。**

这里改用**名字集合差**：Hermes 的名字集合在仓库里就有，不需要填 550 行。
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
RAW = REPO / "handoff" / "hermes-parity" / "research" / "_raw"

_TOP = re.compile(r"^cmd-\d+-(.+)\.txt$")

#: 已知改名：Orbit 的 `__xxx` 就是 Hermes 的 `internal-xxx`。
#: **不算这一步，「自有」会虚高近一倍**（第一版就踩了）。
_RENAMES = {
    "__compute-host": "internal-compute-host",
    "__gateway-restart-watch": "internal-gateway-restart-watch",
    "__run-script": "internal-run-script",
    "__slash-worker": "internal-slash-worker",
    "__update-gateway-helper": "internal-update-gateway-helper",
}


def _hermes_top_names() -> set[str]:
    if not RAW.is_dir():
        pytest.skip(f"找不到 Hermes help 转储目录：{RAW}")
    names: set[str] = set()
    for path in RAW.glob("cmd-*.txt"):
        match = _TOP.match(path.name)
        if match:
            names.add(match.group(1))
    return names


def _orbit_top_names() -> set[str]:
    from app.cli import REGISTRY

    names: set[str] = set()
    for command in REGISTRY.all():
        names.add(command.name)
        for alias in getattr(command, "aliases", ()) or ():
            names.add(str(alias))
    return names


def test_every_hermes_top_level_command_exists_in_orbit():
    """**目标判据**：Hermes 的每个顶层命令名，Orbit 都有。

    这条红了就意味着「有一整条命令缺失」——比任何单点功能缺口都严重。
    """
    hermes = _hermes_top_names()
    orbit = _orbit_top_names()
    # 把已知改名算成对齐
    effective = (orbit - set(_RENAMES)) | {h for h in _RENAMES.values() if h in hermes}

    missing = sorted(hermes - effective)
    assert not missing, (
        f"这些 Hermes 顶层命令在 Orbit 里没有：{missing}\n"
        "（如果只是改了名，请加进 `_RENAMES` 并说明对应关系）"
    )
    assert len(hermes) >= 70, f"Hermes 名字集合只读到 {len(hermes)} 个，转储文件可能不全"


def test_the_orbit_only_list_stays_small_and_documented():
    """Orbit 自有顶层命令应当**少而有序**，且每条都在 help 里说明自己是什么。

    这条不是判错，是**防漂移**：如果哪天自有命令突然涨到二十几个，
    说明有人在用「加新命令」绕开「对齐 Hermes」。
    """
    from app.cli import REGISTRY

    hermes = _hermes_top_names()
    orbit = _orbit_top_names()
    effective = (orbit - set(_RENAMES)) | {h for h in _RENAMES.values() if h in hermes}
    orbit_only = sorted(effective - hermes)

    assert len(orbit_only) <= 12, f"Orbit 自有顶层命令涨到 {len(orbit_only)} 个：{orbit_only}"
    for name in orbit_only:
        command = REGISTRY.get(name)
        assert command is not None, name
        assert (command.help or "").strip(), (
            f"自有命令 `{name}` 没有 help 说明——自有的东西更该说清自己是什么"
        )
