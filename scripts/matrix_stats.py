"""统计 `handoff/hermes-parity/MATRIX.md` 的条目数与状态分布。

为什么要有这个脚本：主清单是手写的 Markdown 表格，条目数会随波次变化。
手维护一个"合计 133 项"的数字必然漂移——而**完成度百分比是给人和监视子agent
看的判据**，一个漂移的分母会让进度看起来忽高忽低。所以让它可重算。

用法（仓库根目录）：

    backend\\.venv\\Scripts\\python.exe scripts\\matrix_stats.py
    backend\\.venv\\Scripts\\python.exe scripts\\matrix_stats.py --check   # 与文件头部声明不符则退出 1

口径：
- 只统计形如 `| 12.3 | …` 的条目行（分层清单），附录与说明表不计入。
- 状态取**状态列**（第 5 个 `|` 段），不是"整行里出现了哪个符号"。

  **这一条踩过坑**：早先的实现是 `if DONE in line`，只要证据栏里出现一个 ✅ 就算完成。
  监视子agent 的一条建议里写了「→ 变 ✅ 的路径」，于是 10.1（状态列是 🟡）被算成完成，
  完成度虚报 1 条。整行匹配的错法很隐蔽：状态列明明写着 🟡，数字却对不上。
  现在改成读状态列，并加两条断言（状态列必须是三符号之一、整行不得出现第二个状态符号），
  让"证据栏提到别的符号"当场被门禁抓住。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MATRIX = Path(__file__).resolve().parents[1] / "handoff" / "hermes-parity" / "MATRIX.md"

DONE, PARTIAL, TODO = "\u2705", "\U0001F7E1", "\u2b1c"
SYMBOLS = (DONE, PARTIAL, TODO)
ITEM = re.compile(r"^\| (\d+)\.(\d+[a-z]?) \|")
#: 文件头里那句"条目合计 **180**。当前：✅ 22 · 🟡 4 · ⬜ 154"
#: 数字前面带状态表情，所以用 `[^\d]*` 吃掉分隔符，而不是硬编码具体表情——
#: 换表情不该让门禁失效。
HEADER = re.compile(
    r"条目合计 \*\*(?P<total>\d+)\*\*。当前：[^\d]*(?P<done>\d+) · "
    r"[^\d]*(?P<partial>\d+) · [^\d]*(?P<todo>\d+)"
)


def status_of(line: str) -> str:
    """取状态列（第 5 个 `|` 段）。取不到返回空串。

    **必须先处理转义竖线**：条目的"依据"栏里常有 ``iwr\\|iex`` 这种转义写法，
    直接 ``line.split("|")`` 会把它切成两段，状态列整体右移一格，于是
    「状态列不是三符号之一（'W13'）」——5.9 / 5.10 就是这么被静默漏掉的
    （两个状态都算不进任何一桶，总数却照数，导致 95+45+39 ≠ 181）。
    """
    # 用一个不会出现在 Markdown 里的占位符替掉转义竖线，再按真竖线切。
    safe = line.replace("\\|", "\x00")
    parts = safe.split("|")
    if len(parts) < 7:
        return ""
    return parts[5].strip()


def count(text: str) -> dict[str, int]:
    total = done = partial = todo = 0
    for line in text.splitlines():
        if not ITEM.match(line):
            continue
        total += 1
        status = status_of(line)
        if status == DONE:
            done += 1
        elif status == PARTIAL:
            partial += 1
        elif status == TODO:
            todo += 1
    return {"total": total, "done": done, "partial": partial, "todo": todo}


def status_column_problems(text: str) -> list[str]:
    """状态列的两种病：不是三符号之一，或整行出现第二个状态符号。

    第二种正是虚报的成因——证据栏写「变 ✅ 的路径」时，状态列是 🟡，整行却含 ✅。
    把它变成门禁错误，比事后靠人眼发现可靠。
    """
    problems: list[str] = []
    for line in text.splitlines():
        match = ITEM.match(line)
        if not match:
            continue
        ident = f"{match.group(1)}.{match.group(2)}"
        status = status_of(line)
        if status not in SYMBOLS:
            problems.append(f"{ident}：状态列不是三符号之一（{status!r}）")
            continue
        found = [symbol for symbol in SYMBOLS if symbol in line]
        if len(found) > 1:
            others = "、".join(symbol for symbol in found if symbol != status)
            problems.append(f"{ident}：状态列是 {status}，但行内还出现了 {others}（会被误算）")
    return problems


def duplicate_ids(text: str) -> list[str]:
    """重复的条目编号。

    这是必须查的：编号重复会让"条目总数"虚高，而总数是完成度的分母——
    分母错了，百分比就没有意义。实际踩过一次（1.7 被两条不同的行占用）。
    """
    ids = [match.group(0) for match in re.finditer(r"^\| (\d+\.\d+[a-z]?) \|", text, re.M)]
    seen: set[str] = set()
    dupes: list[str] = []
    for identifier in ids:
        key = identifier.strip("| ")
        if key in seen and key not in dupes:
            dupes.append(key)
        seen.add(key)
    return dupes


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

    dupes = duplicate_ids(text)
    if dupes:
        print(f"条目编号重复：{'、'.join(dupes)}", file=sys.stderr)
        return 1

    problems = status_column_problems(text)
    if problems:
        print("状态列有问题：", file=sys.stderr)
        for problem in problems:
            print(f"  · {problem}", file=sys.stderr)
        return 1

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
