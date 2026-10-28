"""把 5.14 的 57 条缺失按四类机械分类。

## 这个脚本为什么被重写

第一版**docstring 与实现不一致**：它说 `unwired` = 「该名字在 `app/` 的某个
`*_TOOLS` 集合里出现过」，**但代码里根本没有这个检查** → 该桶恒为 0。
而且**没有「刻意不做」桶**，`browser_cdp` 无处可去。

**后果很实际**：`delegate_task`（有 `DelegateRunner`，缺一层 `Tool` 包装，几小时）
与 `browser_vision`（全仓无实现，几天）**落进同一个桶**——
**一个把所有东西都标成"未实现"的桶，撑不起任何工作量估计。**

## 四类

1. **`unimplemented`（全仓无实现）**：`app/` 里既没有 `Tool` 定义，也没有已知机制
   → 真缺能力，要写 schema + 分发 + 测试。
2. **`needs_wrapper`（有机制、缺 `Tool` 包装）**：能力在仓库里（如 `DelegateRunner`），
   只是没包成模型可调的工具 → **几小时量级**。
3. **`deliberate`（刻意不做）**：设计上决定不提供（附理由）。
4. **`needs_env`（需外部条件）**：本脚本**不判**——那要逐个跑 `available()`。

## 判据为什么有一份手写清单

`needs_wrapper` 与 `deliberate` **不可能从源码机械推出来**：
「`DelegateRunner` 存在」不等于「它就是 `delegate_task` 的实现」，
「刻意不做」是人的决定。所以这两类用**手工核实过的清单**，并在输出里标明来源。
**机械判据只负责第 1 类**——而那正是它擅长的部分。

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

#: **有机制、缺 `Tool` 包装**（手工核实：能力在仓库里，只是模型调不到）。
NEEDS_WRAPPER: dict[str, str] = {
    "delegate_task": "app/delegate.py::DelegateRunner（深度/并发/预算三道闸都实现了）",
    "agent_swarm": "app/swarm.py（按 depends_on 拓扑序批量并发）",
}

#: **刻意不做**（手工核实：设计决定，不是没能力）。
DELIBERATE: dict[str, str] = {
    "browser_cdp": "协议逃生口——等于把整条浏览器控制权交给模型，刻意不提供",
}


def _tool_names_defined_in_source() -> set[str]:
    """扫 `app/` 的 AST，找出所有被当作 `Tool(...)` 定义的名字。

    **为什么用 AST 而不是 grep**：grep 会把 `skill_seed.py` 里的种子技能**文档文本**
    也算成"有实现"——那正是第一版判错的来源。
    """
    defined: set[str] = set()
    for path in APP.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                fname = getattr(func, "id", None) or getattr(func, "attr", None)
                if fname == "Tool":
                    if (
                        node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                    ):
                        defined.add(node.args[0].value)
                    for keyword in node.keywords:
                        if (
                            keyword.arg == "name"
                            and isinstance(keyword.value, ast.Constant)
                            and isinstance(keyword.value.value, str)
                        ):
                            defined.add(keyword.value.value)
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
    from app.tool_catalog import orbit_coverage

    missing = list(orbit_coverage().missing)
    defined = _tool_names_defined_in_source()

    buckets: dict[str, list[str]] = {
        "deliberate": [],
        "needs_wrapper": [],
        "unimplemented": [],
    }
    for name in missing:
        if name in DELIBERATE:
            buckets["deliberate"].append(name)
        elif name in NEEDS_WRAPPER:
            buckets["needs_wrapper"].append(name)
        elif name not in defined:
            buckets["unimplemented"].append(name)
        else:
            # 有 Tool 定义但不在池子里 → 缺装配（本轮实测为 0，留着防将来）
            buckets.setdefault("unwired", []).append(name)

    return {
        "total_missing": len(missing),
        "deliberate": sorted(buckets["deliberate"]),
        "needs_wrapper": sorted(buckets["needs_wrapper"]),
        "unimplemented": sorted(buckets["unimplemented"]),
        "unwired": sorted(buckets.get("unwired") or []),
        "sources": {
            "deliberate": "手工核实（设计决定）",
            "needs_wrapper": "手工核实（能力在仓库、缺 Tool 包装）",
            "unimplemented": "AST 扫 app/ 的 Tool(...) 定义",
            "unwired": "AST 扫到 Tool 定义但不在 orbit_pool()",
        },
        "scope": (
            "`needs_env`（需外部条件）本脚本不判——要逐个跑 available()。"
            "`deliberate` / `needs_wrapper` 不可能从源码机械推出，用**手工核实清单**并在 "
            "`sources` 里标明；机械判据只负责 `unimplemented`。"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="5.14 缺失工具的四类计数")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    data = classify()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(f"缺失合计 {data['total_missing']}")
    print(f"  ① 全仓无实现（真缺能力，几天量级）：{len(data['unimplemented'])}")
    for name in data["unimplemented"]:
        print(f"     - {name}")
    print(f"  ② 有机制缺 Tool 包装（几小时量级）：{len(data['needs_wrapper'])}")
    for name in data["needs_wrapper"]:
        print(f"     + {name}  ← {NEEDS_WRAPPER[name]}")
    print(f"  ③ 刻意不做：{len(data['deliberate'])}")
    for name in data["deliberate"]:
        print(f"     × {name}  ← {DELIBERATE[name]}")
    print(f"  ④ 有 Tool 定义但未进池（缺装配）：{len(data['unwired'])}")
    for name in data["unwired"]:
        print(f"     ~ {name}")
    print("  ⑤ 需外部条件：本脚本不判（要逐个跑 available()）")
    print()
    print("口径：", data["scope"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
