"""门禁：MATRIX 引用的调研文档必须能解析到真实文件。

## 为什么需要它

`audit_matrix_citations` 只查「`tests/*.py` 在不在」，**它不管调研文档**。
而 MATRIX 的证据大量依赖调研结论 —— 如果引用解析不到，
**读者会以为「有实证」，其实那份实证找不到**。

## 两种引用形状（MATRIX 里实际出现的）

1. **路径式**：`research/01-cli-surface.md:1834`（实测只有 **1 处**）；
2. **编号式**：`【实证】调研 03 §1`（实测 **8 个编号**，是主流形状）。

**两种都要查** —— 只查第一种会得到一个「覆盖 1 处」的门禁，
**那种门禁给人的是假安心。**

## 口径

- 编号式：`research/` 下必须有 `NN-*.md`；
- 路径式：文件必须存在，且**行号在文件范围内**（行号一过期，「可复核」就变成不可能）；
- **不检查内容是否对得上** —— 那需要语义判断，机器做不了。
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
HANDOFF = ROOT / "handoff" / "hermes-parity"
RESEARCH = HANDOFF / "research"
MATRIX = HANDOFF / "MATRIX.md"

PATH_CITE = re.compile(r"research/([\w.-]+\.md):(\d+)(?:-(\d+))?")
NUM_CITE = re.compile(r"调研\s*(\d{2})")


def main() -> int:
    text = MATRIX.read_text(encoding="utf-8")
    have = sorted(p.name for p in RESEARCH.glob("*.md"))
    problems: list[str] = []

    # ① 编号式
    numbers = sorted(set(NUM_CITE.findall(text)))
    for number in numbers:
        if not any(name.startswith(number) for name in have):
            problems.append(f"调研 {number} → research/ 下没有 {number}-*.md")
    print(f"编号式引用：{len(numbers)} 个（{', '.join(numbers) or '无'}）")

    # ② 路径式（含行号范围）
    lengths: dict[str, int] = {}
    path_count = 0
    for match in PATH_CITE.finditer(text):
        name = match.group(1)
        path_count += 1
        path = RESEARCH / name
        if not path.is_file():
            problems.append(f"{match.group(0)} → 文件不存在")
            continue
        if name not in lengths:
            lengths[name] = len(path.read_text(encoding="utf-8", errors="ignore").splitlines())
        last = int(match.group(3) or match.group(2))
        if last > lengths[name]:
            problems.append(f"{match.group(0)} → 超出范围（共 {lengths[name]} 行）")
    print(f"路径式引用：{path_count} 处")

    if not problems:
        print()
        print("所有调研引用都能解析到真实文件与有效行号。")
        return 0

    print()
    print(f"**{len(problems)} 处解析不到：**")
    for problem in problems:
        print(f"  {problem}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
