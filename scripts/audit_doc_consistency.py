"""门禁：GAPS / HANDOVER 里「N 条 🟡」必须与 MATRIX 实际一致。

## 为什么需要它

第 101 轮发现：MATRIX 是 **5 条 🟡**，而 `GAPS.md` 里写着
「7 条」1 处、「6 条」6 处、「5 条」1 处 ——
**读者读到哪个数字，取决于他看哪一行。**

**同一个量在两个文档里各写一遍，必然漂移。**
这个门禁把那句话变成机械检查。

## 口径

只查 `N 条 🟡` 这一种形状（最容易被读者当结论的那种）。
**历史叙述里提到旧数字（如「从 7 条降到 5 条」）会误报** ——
所以遇到误报，正确做法是把那句改写成不重复数字的说法，
而不是放宽正则。
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
HANDOFF = ROOT / "handoff" / "hermes-parity"


def matrix_yellow() -> list[str]:
    ids: list[str] = []
    for line in (HANDOFF / "MATRIX.md").read_text(encoding="utf-8").splitlines():
        if not re.match(r"^\| [0-9]", line):
            continue
        parts = line.replace("\\|", "\x00").split("|")
        if len(parts) == 8 and parts[5].strip() == "🟡":
            ids.append(parts[1].strip())
    return ids


def main() -> int:
    ids = matrix_yellow()
    expected = len(ids)
    print(f"MATRIX 实际 🟡：{expected} 条 —— {ids}")

    bad: list[tuple[str, str, int]] = []
    # **PROGRESS.md 故意不在范围内**：它是监视子agent每轮重写的快照，
    # 天然滞后于 MATRIX（它自己也说「以文件为准」）。
    # 门禁只管**长期文档** —— 快照滞后不是缺陷，长期文档矛盾才是。
    for name in ("GAPS.md", "HANDOVER.md", "README.md"):
        path = HANDOFF / name
        if not path.is_file():
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for match in re.finditer(r"(\d+) 条 🟡", line):
                found = int(match.group(1))
                if found != expected:
                    bad.append((name, line.strip()[:90], line_no))

    if not bad:
        print("GAPS / HANDOVER / README 里的「N 条 🟡」都与 MATRIX 一致。")
        return 0

    print()
    print(f"**{len(bad)} 处与 MATRIX 不一致：**")
    for name, text, line_no in bad:
        print(f"  {name}:{line_no}  {text}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
