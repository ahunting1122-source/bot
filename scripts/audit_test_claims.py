"""审计：声称「有断言 / 有测试」的 ✅ 行，必须点名测试文件。

## 为什么需要它

`12.1` 的证据曾写着「……死进程的孤儿锁会被回收（**有断言**）」——
**但没说是哪个测试。** 于是这条 ✅ 只能被信任，不能被复核。

**「有断言」是一句声明，不是一个抓手；点名文件才是。**

## 口径

只查 ✅ 行；只查「声称测过」的说法（`有断言` / `有测试` / `测试覆盖` / `已测` /
`被测试` / `断言过` / `测试验过`）；证据里必须出现 `tests/xxx.py`。

**会误报的情况**：一句话里提到「既有测试断言…」是在**解释设计取舍**，不是声称本行有测试。
实测 `4.1` 就是这种（「因为既有测试断言那两个字段默认为 None」）。
**遇到误报，正确的处理是给那一行补上真正的测试名，而不是放宽正则** ——
因为补上名字本身就有价值。

**判据：审计脚本抓到的每一条，要么是真问题，要么是「这句话本来该点名却没说」——
两种都值得改。**
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

CLAIM = re.compile(r"有断言|有测试|测试覆盖|已测|被测试|断言过|测试验过")
NAMED = re.compile(r"tests[/\\][\w/\\]+\.py")


def main() -> int:
    rows: list[tuple[str, str, str]] = []
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        if not re.match(r"^\| [0-9]", line):
            continue
        parts = line.replace("\\|", "\x00").split("|")
        if len(parts) != 8:
            continue
        rows.append((parts[1].strip(), parts[5].strip(), parts[6]))

    offenders = []
    for ident, status, evidence in rows:
        if status != "✅":
            continue
        claim = CLAIM.search(evidence)
        if claim and not NAMED.search(evidence):
            at = evidence.find(claim.group(0))
            context = evidence[max(0, at - 45) : at + 35].replace("\n", " ").strip()
            offenders.append((ident, claim.group(0), context))

    print(f"数据行：{len(rows)}")
    if not offenders:
        print("每一条声称测过的 ✅ 行都点名了测试文件。")
        return 0

    print()
    print(f"**{len(offenders)} 条声称测过、但没点名测试文件：**")
    for ident, word, context in offenders:
        print(f"  {ident:7} 「{word}」 …{context}…")
    return 1


if __name__ == "__main__":
    sys.exit(main())
