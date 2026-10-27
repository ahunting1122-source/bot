"""统计 `handoff/hermes-parity/MATRIX.md` 的条目数与状态分布。

为什么要有这个脚本：主清单是手写的 Markdown 表格，条目数会随波次变化。
手维护一个"合计 133 项"的数字必然漂移——而**完成度百分比是给人和监视子agent
看的判据**，一个漂移的分母会让进度看起来忽高忽低。所以让它可重算。

用法（仓库根目录）：

    backend\\.venv\\Scripts\\python.exe scripts\\matrix_stats.py
    backend\\.venv\\Scripts\\python.exe scripts\\matrix_stats.py --check   # 与文件头部声明不符则退出 1

口径：
- 只统计形如 `| 12.3 | …` 的条目行（分层清单），附录与说明表不计入。
- 状态取该行里出现的第一个标记：✅ 已实现 / 🟡 部分 / ⬜ 未开始。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MATRIX = Path(__file__).resolve().parents[1] / "handoff" / "hermes-parity" / "MATRIX.md"

DONE, PARTIAL, TODO = "\u2705", "\U0001F7E1", "\u2b1c"
ITEM = re.compile(r"^\| (\d+)\.(\d+[a-z]?) \|")
#: 文件头里那句"条目合计 **180**。当前：✅ 22 · 🟡 4 · ⬜ 154"
#: 数字前面带状态表情，所以用 `[^\d]*` 吃掉分隔符，而不是硬编码具体表情——
#: 换表情不该让门禁失效。
HEADER = re.compile(
    r"条目合计 \*\*(?P<total>\d+)\*\*。当前：[^\d]*(?P<done>\d+) · "
    r"[^\d]*(?P<partial>\d+) · [^\d]*(?P<todo>\d+)"
)


def count(text: str) -> dict[str, int]:
    total = done = partial = todo = 0
    for line in text.splitlines():
        if not ITEM.match(line):
            continue
        total += 1
        if DONE in line:
            done += 1
        elif PARTIAL in line:
            partial += 1
        elif TODO in line:
            todo += 1
    return {"total": total, "done": done, "partial": partial, "todo": todo}


def main() -> int:
    parser = argparse.ArgumentParser(description="统计对标主清单")
    parser.add_argument("--check", action="store_true", help="与文件头部声明比对，不符则退出 1")
    args = parser.parse_args()

    text = MATRIX.read_text(encoding="utf-8")
    actual = count(text)
    print(
        f"条目 {actual['total']}：✅ {actual['done']} · 🟡 {actual['partial']} · ⬜ {actual['todo']}"
        f"（完成度 {actual['done'] / actual['total'] * 100:.1f}%）"
    )

    match = HEADER.search(text)
    if match is None:
        print("找不到文件头部的计数声明（格式已变？）", file=sys.stderr)
        return 1
    declared = {key: int(value) for key, value in match.groupdict().items()}
    if declared != actual:
        print(f"头部声明 {declared} 与实际 {actual} 不一致", file=sys.stderr)
        return 1 if args.check else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
