"""`orbit_only` 统计：**用名字集合差，不动 `Command.hermes` 字段**。

## 为什么不用 `Command.hermes`

监视子agent 查过：`Command.hermes` 大部分为空（`chat` / `config` / `cron` / `plane` /
`moa` / `approvals` 全 `''`），而 `parity_rows()` 在它为空时**回落到命令名**——
于是它永远非空，也就永远判不出「这条是不是 Hermes 有的」。
**这与 `getattr(settings, "max_turns", None)` 是同一类病：fallback 让「没填」看起来像「填了」。**

## 为什么名字集合差够用

判「是不是 Hermes 有」**只需要一份名字集合**，而它已经在仓库里：
`research/_raw/cmd-*.txt`（77 个顶层 help 转储）与 `research/_raw/sub/*.txt`
（295 个二级 help 转储）。**文件名就是命令路径**：
`cmd-01-chat.txt` → `chat`；`approvals__suggest.txt` → `approvals suggest`。

所以不用填 550 行、不用改字段、不用去回落——**只加一个只读统计**。

## 口径（必须先说清，否则数字会被误读）

**只判顶层命令名。** 子命令级的「多出来」（比如 `orbit sessions` 27 条 vs Hermes 19 条）
**判不了**——`sub/*.txt` 里没有的也可能是 Hermes 的子命令没被 dump 到。
那部分继续留在 MATRIX 对应行的「代价：」里，**不在这里假装能算**。

用法：
    python scripts/orbit_only.py            # 人读
    python scripts/orbit_only.py --json     # 机读
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
RAW = ROOT / "handoff" / "hermes-parity" / "research" / "_raw"

#: `cmd-01-chat.txt` → `chat`。前导 `NN-` 是导出顺序号，不是名字的一部分。
_TOP = re.compile(r"^cmd-\d+-(.+)\.txt$")
#: `approvals__suggest.txt` → `("approvals", "suggest")`。
_SUB = re.compile(r"^(.+?)__(.+)\.txt$")


def hermes_top_names() -> set[str]:
    """Hermes 的顶层命令名集合（来自 `--help` 转储的文件名）。"""
    names: set[str] = set()
    for path in RAW.glob("cmd-*.txt"):
        match = _TOP.match(path.name)
        if match:
            names.add(match.group(1))
    return names


def hermes_sub_paths() -> set[str]:
    """Hermes 的二级命令路径集合（`"approvals suggest"` 这种形状）。"""
    paths: set[str] = set()
    for path in (RAW / "sub").glob("*.txt"):
        match = _SUB.match(path.name)
        if match:
            paths.add(f"{match.group(1)} {match.group(2)}")
    return paths


def orbit_top_names() -> set[str]:
    """Orbit 的顶层命令名（含别名）。"""
    sys.path.insert(0, str(ROOT / "backend"))
    from app.cli import REGISTRY

    names: set[str] = set()
    for command in REGISTRY.all():
        names.add(command.name)
        for alias in getattr(command, "aliases", ()) or ():
            names.add(str(alias))
    return names


#: 已知的**改名对应**：Orbit 的 `__xxx` 就是 Hermes 的 `internal-xxx`。
#:
#: **为什么必须单独列**：名字差只能判「名字在不在」，判不了「能力在不在」。
#: 第一版直接做差，把这 5 个算成了「Orbit 自有」——**它们其实是对齐的，只是改了名**。
#: 不算这一步，「自有 12 个」这个数字会虚高将近一倍。
_RENAMES = {
    "__compute-host": "internal-compute-host",
    "__gateway-restart-watch": "internal-gateway-restart-watch",
    "__run-script": "internal-run-script",
    "__slash-worker": "internal-slash-worker",
    "__update-gateway-helper": "internal-update-gateway-helper",
}


def report() -> dict:
    hermes = hermes_top_names()
    orbit = orbit_top_names()

    # 先把改名对上：改名后命中 Hermes 的，算「对齐（改名）」，不算自有
    renamed = {o: h for o, h in _RENAMES.items() if o in orbit and h in hermes}
    orbit_effective = (orbit - set(renamed)) | {h for h in renamed.values()}

    orbit_only = sorted(orbit_effective - hermes)
    hermes_only = sorted(hermes - orbit_effective)
    return {
        "hermes_top": len(hermes),
        "orbit_top": len(orbit),
        "aligned": len(orbit_effective & hermes),
        "renamed": renamed,
        "orbit_only": orbit_only,
        "hermes_only": hermes_only,
        "hermes_sub_paths": len(hermes_sub_paths()),
        # 口径声明：这几个数字回答的问题不同，别混着比
        "scope": (
            "只判顶层命令名，且已扣除已知改名（`__xxx` = `internal-xxx`）。"
            "**名字差 ≠ 能力差**：剩下的「自有」里仍可能有别名或改名未登记，"
            "需要人判。子命令级的「多出来」（如 orbit sessions 27 条 vs Hermes 19 条）"
            "**判不了**——sub/*.txt 里没有的也可能是 Hermes 的子命令没被 dump 到，"
            "那部分留在 MATRIX 对应行的「代价：」里。"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="orbit_only 顶层命令统计（名字集合差）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    data = report()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    print(
        f"Hermes 顶层 {data['hermes_top']} · Orbit 顶层 {data['orbit_top']} · "
        f"名字重合 {data['aligned']}"
    )
    print(f"Orbit 自有（Hermes 名字集合里没有）：{len(data['orbit_only'])}")
    for name in data["orbit_only"]:
        print(f"  + {name}")
    print(f"Hermes 有而 Orbit 没有：{len(data['hermes_only'])}")
    for name in data["hermes_only"]:
        print(f"  - {name}")
    print()
    print("口径：", data["scope"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
