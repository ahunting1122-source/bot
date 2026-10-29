"""审计：MATRIX 引用的测试文件里，有没有**会跳过**的测试。

## 为什么需要它

第 78 轮我从 `pytest -rs` 的 skip 原因里抓到一处虚报（`4.12` 说 `sapi` 能合成，
而 skip 原因写着「No voice installed」）。

**如果一条 ✅ 行的唯一证据是一个会跳过的测试，那它在本机根本没被验过。**
「测试文件存在」≠「测试跑过」。

## 为什么不跑 pytest

跑一遍要 6 分钟，当门禁太慢。**静态扫 skip 标记就够了** ——
`pytest.skip(...)` / `pytest.mark.skipif` / `pytest.mark.skip` 在源码里是可见的。

## 口径

- 只报**文件级**：某个测试文件里有 skip 标记 → 该文件「部分跳过」。
- 再交叉：这个文件有没有被 MATRIX 引用。**被引用 + 有跳过 → 值得人看一眼。**
- **不判死**：一个文件里可能 20 个测试只跳 1 个。所以只报告，不返回 1。
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
MATRIX = ROOT / "handoff" / "hermes-parity" / "MATRIX.md"

SKIP_PATTERNS = (
    re.compile(r"pytest\.skip\("),
    re.compile(r"pytest\.mark\.skipif"),
    re.compile(r"pytest\.mark\.skip\b"),
    re.compile(r"@pytest\.mark\.skip"),
)


def main() -> int:
    skipped: dict[str, list[int]] = {}
    for path in sorted((BACKEND / "tests").glob("*.py")):
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        hits = [
            n + 1
            for n, line in enumerate(lines)
            if any(p.search(line) for p in SKIP_PATTERNS)
        ]
        if hits:
            skipped[path.name] = hits

    matrix = MATRIX.read_text(encoding="utf-8")
    cited = {
        name
        for name in skipped
        if f"tests/{name}" in matrix or f"tests\\{name}" in matrix
    }

    print(f"含 skip 标记的测试文件：{len(skipped)}")
    for name, hits in sorted(skipped.items()):
        mark = "**被 MATRIX 引用**" if name in cited else ""
        print(f"  {name:34} {len(hits):2} 处 {mark}")

    print()
    if cited:
        print(f"**{len(cited)} 个被引用的测试文件含 skip 标记**：{sorted(cited)}")
        print("**它们不一定是问题**（一个文件可能只跳 1 个测试），但值得人看一眼：")
        print("如果某条 ✅ 的唯一证据落在被跳过的那个测试上，那它在本机没被验过。")
    else:
        print("没有被 MATRIX 引用的测试文件含 skip 标记。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
