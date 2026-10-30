"""交叉审计：✅ 行引用的测试文件里，有没有**含 skip 标记**的。

## 为什么要把两个门禁交叉

- `audit_test_claims` 只问「有没有点名测试文件」；
- `audit_skipped_tests` 只问「哪些文件含 skip 标记」。

**两者都没问的是**：一条 ✅ 行的证据落在**一个含 skip 的文件**上时，
**那个被跳过的测试，是不是这条行唯一的依据？**

## 口径（宽，只抓需要人看一眼的）

对每条 ✅ 行：抽出它引用的所有 `tests/*.py`；若其中**至少一个含 skip 标记**，就列出来，
并把「那行引用了哪些文件、其中哪些含 skip」摆在一起，**由人判断**。

**不判死** —— 一个文件 20 个测试只跳 1 个，是很常见的。
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
MATRIX = ROOT / "handoff" / "hermes-parity" / "MATRIX.md"

SKIP = (
    re.compile(r"pytest\.skip\("),
    re.compile(r"pytest\.mark\.skipif"),
    re.compile(r"@pytest\.mark\.skip"),
)
CITED = re.compile(r"tests[/\\]([\w]+\.py)")


def main() -> int:
    skipped: set[str] = set()
    for path in (BACKEND / "tests").glob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(p.search(text) for p in SKIP):
            skipped.add(path.name)

    rows: list[tuple[str, str, str]] = []
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        if not re.match(r"^\| [0-9]", line):
            continue
        parts = line.replace("\\|", "\x00").split("|")
        if len(parts) != 8:
            continue
        rows.append((parts[1].strip(), parts[5].strip(), parts[6]))

    hits = []
    for ident, status, evidence in rows:
        if status != "✅":
            continue
        cited = set(CITED.findall(evidence.replace("\x00", "|")))
        bad = cited & skipped
        if bad:
            hits.append((ident, sorted(cited), sorted(bad)))

    print(f"含 skip 标记的测试文件：{len(skipped)}")
    print(f"✅ 行里引用了含 skip 文件的行：{len(hits)}")
    if not hits:
        print("没有一条 ✅ 行引用含 skip 的测试文件。")
        return 0

    print()
    for ident, cited, bad in hits:
        print(f"  {ident:7} 引用 {len(cited)} 个文件，其中含 skip 的：{bad}")
    print()
    print("**这些不一定有问题**（一个文件可能只跳 1 个测试），但值得人看一眼：")
    print("如果某条 ✅ 的唯一依据落在被跳过的那个测试上，那它在本机没被验过。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
