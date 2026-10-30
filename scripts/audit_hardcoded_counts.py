"""报告式审计：列出 MATRIX 证据里**写死的计数**，供人复核。

## 为什么要它

第 98–99 轮连抓到两处过期数字，都是同一种病：
- `10.11` 写「已到 **0011**」，而目录里已有 `0012`；
- `10.13` 写「**27/27**」，而 `parity_rows()` 实测 33 条。

**它们过期时不会报错，只会让读者以为项目停在那里。**

## 口径：只报告，不判死

`N/M` 与「已到 NNNN」这类形状，**有些是常量（比如「50,000 字符上限」），永远不会过期**；
有些是**会随项目长大的计数**。脚本分不出来，**所以只列出来让人扫一眼**。

**判据：能自动判的交给门禁，判不了的交给「把它集中起来」——
一份 20 行的清单，比在 182 行里靠记忆找要便宜得多。**
"""

from __future__ import annotations

import pathlib
import re
import sys

MATRIX = (
    pathlib.Path(__file__).resolve().parent.parent
    / "handoff"
    / "hermes-parity"
    / "MATRIX.md"
)

#: 会随项目长大的计数形状（常量阈值不在此列）。
PATTERNS = (
    (re.compile(r"\*\*(\d+)/(\d+)\*\*"), "N/M 完成度"),
    (re.compile(r"已到\s*\*{0,2}(\d{3,4})\*{0,2}"), "已到 NNNN"),
    (re.compile(r"\*\*(\d+)\s*条\*\*"), "**N 条**"),
    (re.compile(r"covered\s+(\d+)\s*→\s*(\d+)"), "covered N→M"),
)


def main() -> int:
    rows: list[tuple[str, str, str, str]] = []
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        if not re.match(r"^\| [0-9]", line):
            continue
        parts = line.replace("\\|", "\x00").split("|")
        if len(parts) != 8:
            continue
        ident = parts[1].strip()
        evidence = parts[6].replace("\x00", "|")
        for pattern, label in PATTERNS:
            for match in pattern.finditer(evidence):
                rows.append((ident, label, match.group(0), evidence[max(0, match.start() - 40) : match.start() + 20]))

    print(f"MATRIX 证据里写死的计数：{len(rows)} 处")
    print()
    by_row: dict[str, list[tuple[str, str]]] = {}
    for ident, label, text, _ctx in rows:
        by_row.setdefault(ident, []).append((label, text))

    for ident, items in by_row.items():
        shown = "、".join(f"{label} `{text}`" for label, text in items[:4])
        more = f" 等 {len(items)} 处" if len(items) > 4 else ""
        print(f"  {ident:7} {shown}{more}")

    print()
    print("**这份清单不是错误列表** —— 有些是常量（如「50,000 字符上限」）永远不会过期。")
    print("**但会随项目长大的计数（完成度 / 迁移版本 / 子命令条数）要定期核一遍。**")
    return 0


if __name__ == "__main__":
    sys.exit(main())
