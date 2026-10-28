"""把 5.14 的 57 条缺失按「未实现 / 有实现未接 / 需外部条件」机械分类。

## 为什么要这个脚本

上一轮我在 GAPS.md 里写了「57 条的 `orbit_status` 全是 `unavailable`，不是没写」——
**那是错的**。监视子agent 实测出 `browser_vision` / `browser_cdp` / `browser_exec`
在 `app/` 里**零实现**（只出现在 `skill_seed.py` 的种子技能文档文本里）。

**而这个错的方向最危险**：GAPS.md 自己在上面两行刚警告过「把测量环境当能力缺口」，
下面一行却犯了**反向**的错——**把能力缺口当测量环境问题**。
**高估剩余工作只是多做，低估会漏做。**

## 三类怎么判（机械判据，不靠印象）

1. **有实现未接**：该名字在 `app/` 的某个 `*_TOOLS` 集合里出现过（说明 `Tool(...)` 真的
   定义了），但不在 `orbit_pool()` 里 → **缺装配**。
2. **未实现**：`app/` 里根本没有这个 `Tool` 定义（只可能在文档/种子技能文本里出现）
   → **真缺能力**。
3. **需外部条件**：有实现且已注册，但 `available()` 为假 → 本机跑不动。

用法：
    python scripts/classify_missing_tools.py
    python scripts/classify_missing_tools.py --json
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP = ROOT / "backend" / "app"


def _tool_names_defined_in_source() -> set[str]:
    """扫 `app/` 的 AST，找出**所有**被当作 `Tool(name=...)` 或 `*_TOOLS` 元素定义的名字。

    **为什么用 AST 而不是 grep**：grep 会把 `skill_seed.py` 里的种子技能**文档文本**
    也算成"有实现"——那正是这次判错的来源。
    """
    defined: set[str] = set()
    for path in APP.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            # Tool("name", ...) / Tool(name="name", ...)
            if isinstance(node, ast.Call):
                func = node.func
                fname = getattr(func, "id", None) or getattr(func, "attr", None)
                if fname == "Tool":
                    if node.args and isinstance(node.args[0], ast.Constant):
                        if isinstance(node.args[0].value, str):
                            defined.add(node.args[0].value)
                    for keyword in node.keywords:
                        if keyword.arg == "name" and isinstance(keyword.value, ast.Constant):
                            if isinstance(keyword.value.value, str):
                                defined.add(keyword.value.value)
            # `name = "x"` 紧跟在 class 里（Tool 子类那种写法）
            if isinstance(node, ast.ClassDef):
                for item in node.body:
                    if (
                        isinstance(item, ast.Assign)
                        and any(
                            isinstance(t, ast.Name) and t.id == "name" for t in item.targets
                        )
                        and isinstance(item.value, ast.Constant)
                        and isinstance(item.value.value, str)
                    ):
                        defined.add(item.value.value)
    return defined


def classify() -> dict:
    sys.path.insert(0, str(ROOT / "backend"))
    from app.tool_catalog import HERMES_TOOLS, orbit_coverage

    coverage = orbit_coverage()
    missing = list(coverage.missing)
    defined = _tool_names_defined_in_source()

    buckets: dict[str, list[str]] = {"unwired": [], "unimplemented": [], "needs_env": []}
    for name in missing:
        if name in defined:
            # 定义了但没进池子 → 缺装配（也可能注册了但 available 为假，那种仍算 needs_env）
            buckets["unwired"].append(name)
        else:
            buckets["unimplemented"].append(name)

    # 已注册但本机跑不动的（这一类在 `unwired` 里混着，单列出来更准）
    from app.tool_catalog import orbit_pool

    pool = set(orbit_pool())
    buckets["unwired"] = [n for n in buckets["unwired"] if n not in pool]

    return {
        "total_missing": len(missing),
        "unimplemented": sorted(buckets["unimplemented"]),
        "unwired": sorted(buckets["unwired"]),
        "needs_env": sorted(buckets["needs_env"]),
        "scope": (
            "机械判据：`unimplemented` = app/ 里没有 `Tool` 定义（AST 扫，不 grep——"
            "grep 会把 skill_seed 的文档文本算成实现）；"
            "`unwired` = 有定义但不在 orbit_pool()；"
            "`needs_env` 需要逐个跑 available()，本脚本不做。"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="5.14 缺失工具的三类计数")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    data = classify()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(f"缺失合计 {data['total_missing']}")
    print(f"  ① 未实现（真缺能力）：{len(data['unimplemented'])}")
    for name in data["unimplemented"]:
        print(f"     - {name}")
    print(f"  ② 有实现未接（缺装配）：{len(data['unwired'])}")
    for name in data["unwired"]:
        print(f"     + {name}")
    print("  ③ 需外部条件：需逐个跑 available()，本脚本不判")
    print()
    print("口径：", data["scope"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
