"""审计：	ool_catalog 每条 covered 声称映射到的工具，是否真的存在。

## 为什么需要它

**映射到不存在的名字比「没实现」更危险：没实现会被发现，映射到不存在的名字不会。**
这是「接上了但有洞，比没接上更危险」的目录版本。

## 基准（这是本脚本的关键）

**所有模块里 *_TOOLS / TOOLS 且元素全是 Tool 实例的列表的并集。**

**为什么不能用别的**：第 73 轮我试过四个入口，四个都不是工具名清单，
于是误报 send_message / 	odo / clarify 不存在 —— **错的是取数方式，不是目录**。

**脚本带自检**：如果基准里找不到 send_message 等已知工具，直接返回 2（基准不可信，审计无意义）。

**判据：先证明基准能列出「已知一定存在的名字」，再用它判别人缺什么。**

## 上轮为什么没结论

我试了四个入口（`app.state.*tools`、`toolsets.TOOLS`、`LOCAL_TOOLS`、`GRANTABLE_TOOLS`），
**四个都不是「工具名清单」**，于是误报 `send_message` / `todo` / `clarify` 不存在。

## 这次用的基准

**所有模块里 `*_TOOLS` / `TOOLS` 且元素全是 `Tool` 实例的列表的并集。**
实测 11 个列表 → **70 个唯一工具名**，`send_message` / `todo` / `clarify` / `process` 全在。

**判据：先证明基准能列出「已知一定存在的名字」，再用它判别人缺什么。**
"""

from __future__ import annotations

import importlib
import pathlib
import pkgutil
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
sys.path.insert(0, str(BACKEND))

from app.tool_catalog import HERMES_TOOLS, orbit_coverage
from app.tools import Tool


def real_tool_names() -> tuple[set[str], int]:
    from app import __path__ as app_path

    names: set[str] = set()
    lists = 0
    for mod in pkgutil.iter_modules(app_path):
        try:
            module = importlib.import_module("app." + mod.name)
        except Exception:  # noqa: BLE001, S112 —— 某个模块 import 失败不该让审计整体失败
            continue
        for attr in dir(module):
            if not (attr.endswith("_TOOLS") or attr == "TOOLS"):
                continue
            value = getattr(module, attr)
            if (
                isinstance(value, (list, tuple))
                and value
                and all(isinstance(item, Tool) for item in value)
            ):
                lists += 1
                names.update(tool.name for tool in value)
    return names, lists


def main() -> int:
    real, lists = real_tool_names()
    print(f"基准：{lists} 个 *_TOOLS 列表，{len(real)} 个唯一工具名")

    # **自检**：这些名字必须都在基准里，否则基准本身不可信。
    sanity = ["send_message", "todo", "clarify", "process", "browser_navigate"]
    bad = [n for n in sanity if n not in real]
    if bad:
        print(f"**基准自检失败：{bad} 不在基准里 —— 基准不可信，审计无意义**")
        return 2
    print(f"基准自检通过：{len(sanity)} 个已知工具全在")

    coverage = orbit_coverage()
    claimed: list[tuple[str, str]] = []
    for tool in HERMES_TOOLS:
        if tool.orbit_status != "covered":
            continue
        for name in tool.orbit_tools:
            claimed.append((tool.name, name))
    print(f"目录声称的映射：{len(claimed)} 条")

    missing = [(h, o) for h, o in claimed if o not in real]
    print()
    if not missing:
        print(f"**每一条 covered 映射都指向真实存在的工具（{coverage.covered_count} 个）。**")
        return 0
    print(f"**{len(missing)} 条映射指向不存在的工具：**")
    for hermes, orbit in missing:
        print(f"  {hermes:26} → {orbit}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
