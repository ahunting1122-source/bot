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
  现在改成读状态列，并加断言（状态列必须是四符号之一、整行不得出现第二个状态符号），
  让"证据栏提到别的符号"当场被门禁抓住。

- **五个状态**：✅ 已实现 / 🟢 **有意差异**（功能有，实现方式与 Hermes 不同，且必须写明
  `差异：…`）/ ⚫ **本机不可验证**（实现做完了，但验证需要网络/硬件/平台凭证）/
  🟡 部分 / ⬜ 未开始。

  **为什么要 🟢**：有些条目 Orbit 是**故意**不照抄 Hermes 的。比如 `journey delete`，
  Hermes 是真删技能，Orbit 只写旁注、绝不回写审计数据——这是更好的工程决定，不是没做。
  把它们和 🟡（没做完）混在一起，会让完成度卡在七成出头，而且读者分不清
  「还差工作」和「已经决定不这么做」。🟢 **计入完成度**。

  **为什么要 ⚫ 而且不计入完成度**：环境限制（本机没网络 / 没 chromium / 没平台凭证）
  是「**未验证**」，不是「已完成」。把它算进完成度，等于用本机条件给目标降标，
  与「Hermes 有的 Orbit 都有」直接冲突。所以 ⚫ 单列，并给出**双口径**：
  - 严格完成度 = (✅ + 🟢) / 总数        ← 离目标多远
  - 可验证完成度 = (✅ + 🟢) / (总数 − ⚫) ← 在本机能验证的范围内做完多少
  并成一个数字一定会误导。

  **两道门禁**（否则这两个状态都会退化成垃圾桶）：
  1. 标 🟢 的行，证据栏**必须**含 `差异：`；
  2. 标 🟢 的行，`差异：` 之后**不得**出现「本机 / 没网络 / 没装 / 无法验证 / 缺凭证」——
     这些是环境限制，该用 ⚫。判据是一个可机械执行的问题：
     **「换一台有网/有 chromium/有平台凭证的机器，这条能不能验证？」**
     能 → 是环境限制（⚫）；不能（能力本身依赖我们不打算做的私有协议）→ 才可能是 🟢。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

MATRIX = Path(__file__).resolve().parents[1] / "handoff" / "hermes-parity" / "MATRIX.md"

DONE, PARTIAL, TODO = "\u2705", "\U0001F7E1", "\u2b1c"
#: 有意差异：功能有，但实现方式与 Hermes 不同（证据栏必须写明 `差异：…`）。
DIVERGENT = "\U0001F7E2"
#: 本机不可验证：实现做完了，但验证要网络/硬件/平台凭证。**不计入完成度**。
UNVERIFIABLE = "\u26ab"
SYMBOLS = (DONE, PARTIAL, TODO, DIVERGENT, UNVERIFIABLE)

#: 出现在 🟢 的 `差异：` 段里就说明它其实是环境限制，该用 ⚫。
#: 这是让 🟢 不退化成垃圾桶的机械手段——只靠"必须写差异"拦不住这一种。
ENV_MARKERS = ("本机", "没网络", "没有网络", "没装", "无法验证", "缺凭证", "无网络")

ITEM = re.compile(r"^\| (\d+)\.(\d+[a-z]?) \|")
#: 文件头里那句"条目合计 **180**。当前：✅ 22 · 🟢 3 · ⚫ 1 · 🟡 4 · ⬜ 154"
#: 数字前面带状态表情，所以用 `[^\d]*` 吃掉分隔符，而不是硬编码具体表情——
#: 换表情不该让门禁失效。🟢 / ⚫ 段可选（老格式没有它们）。
HEADER = re.compile(
    r"条目合计 \*\*(?P<total>\d+)\*\*。当前：[^\d]*(?P<done>\d+) · "
    r"(?:[^\d]*(?P<divergent>\d+) · )?(?:[^\d]*(?P<unverifiable>\d+) · )?"
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
    total = done = partial = todo = divergent = unverifiable = 0
    for line in text.splitlines():
        if not ITEM.match(line):
            continue
        total += 1
        status = status_of(line)
        if status == DONE:
            done += 1
        elif status == DIVERGENT:
            divergent += 1
        elif status == UNVERIFIABLE:
            unverifiable += 1
        elif status == PARTIAL:
            partial += 1
        elif status == TODO:
            todo += 1
    return {
        "total": total,
        "done": done,
        "divergent": divergent,
        "unverifiable": unverifiable,
        "partial": partial,
        "todo": todo,
    }


def completion(actual: dict[str, int]) -> float:
    """**严格**完成度 = （已实现 + 有意差异）/ 总数。

    🟢 计入完成：它满足目标的字面要求（Hermes 有的能力 Orbit 有），只是做法不同。
    ⚫ **不计入**：环境限制是「未验证」，不是「已完成」——算进去等于用本机条件给目标降标。
    """
    total = actual["total"] or 1
    return (actual["done"] + actual.get("divergent", 0)) / total * 100


def verifiable_completion(actual: dict[str, int]) -> float:
    """**可验证范围**内的完成度 = (✅ + 🟢) / (总数 − ⚫)。

    两个口径必须并列：严格口径回答「离目标多远」，这个回答「本机能验的部分做完没」。
    并成一个数字一定会误导——要么把环境限制算成成果，要么让已经做完的活看起来没做。
    """
    verifiable = actual["total"] - actual.get("unverifiable", 0)
    if verifiable <= 0:
        return 0.0
    return (actual["done"] + actual.get("divergent", 0)) / verifiable * 100


def status_column_problems(text: str) -> list[str]:
    """状态列的三种病：不是四符号之一、整行出现第二个状态符号、🟢 没写差异。

    第二种正是虚报的成因——证据栏写「变 ✅ 的路径」时，状态列是 🟡，整行却含 ✅。
    第三种是 🟢 特有的：标了「有意差异」却不写差异在哪，那 🟢 就成了逃避 ✅ 的垃圾桶。
    把这些变成门禁错误，比事后靠人眼发现可靠。
    """
    problems: list[str] = []
    for line in text.splitlines():
        match = ITEM.match(line)
        if not match:
            continue
        ident = f"{match.group(1)}.{match.group(2)}"
        # 结构门禁：一行必须是 8 段（首尾空 + 编号/描述/来源/波次/状态/证据）。
        # 证据里的裸 `|` 会把行切成 9~11 段——Markdown 表格会画歪，
        # 而**取值检查抓不到这个**（状态列照样读得对，因为它读的是第 6 列，
        # 多余的管道符都在它后面）。监视子agent 那句话值得照抄：
        # **凡是「人写、机器读」的格式都该有结构检查兜底。**
        if len(line.replace("\\|", "\x00").split("|")) != 8:
            problems.append(
                f"{ident}：这一行不是 8 段（证据里有未转义的 `|`）——"
                "表格会画歪，且编辑脚本会静默跳过它。管道符要写成 `\\|`"
            )
        status = status_of(line)
        if status not in SYMBOLS:
            problems.append(f"{ident}：状态列不是四符号之一（{status!r}）")
            continue
        found = [symbol for symbol in SYMBOLS if symbol in line]
        if len(found) > 1:
            others = "、".join(symbol for symbol in found if symbol != status)
            problems.append(f"{ident}：状态列是 {status}，但行内还出现了 {others}（会被误算）")
            continue
        if status == DIVERGENT:
            if "差异：" not in line:
                problems.append(f"{ident}：标了 🟢（有意差异）却没写「差异：…」——🟢 不是逃避 ✅ 的垃圾桶")
                continue
            # 第二道：`差异：` 之后出现环境限制字样 → 它其实是 ⚫ 不是 🟢。
            # 判据是可机械执行的一问：「换一台有网/有 chromium/有凭证的机器，能不能验证？」
            tail = line.split("差异：", 1)[1]
            hit = [word for word in ENV_MARKERS if word in tail]
            if hit:
                problems.append(
                    f"{ident}：标了 🟢 但「差异：」段里出现 {('、'.join(hit))}——"
                    "这是环境限制，该用 ⚫（本机不可验证），不是有意差异"
                )
        if status == UNVERIFIABLE:
            # ⚫ 的硬门禁：**必须证明实现真的存在**，否则它就是第二个垃圾桶——
            # 这次收的是「压根没写」。判据是两段都得到：
            #   `实现：<文件:行>` —— 代码在哪
            #   `缺：<网络/凭证/硬件>` —— 差哪个外部条件才能验
            #
            # 这条门禁的理由：监视子agent 指出「能不能验证」与「实现在不在」是两件事。
            # 没写的能力换什么机器都验不了，那必须留 🟡——⚫ 只能收「写好了但没法验」。
            if "实现：" not in line:
                problems.append(
                    f"{ident}：标了 ⚫ 但没写「实现：<文件:行>」——没实现的能力换什么机器都验不了，"
                    "该回 🟡 而不是 ⚫"
                )
            if "缺：" not in line:
                problems.append(f"{ident}：标了 ⚫ 但没写「缺：<网络/凭证/硬件>」——不说缺什么等于没说")
    return problems


def duplicate_ids(text: str) -> list[str]:
    """重复的条目编号。

    这是必须查的：编号重复会让"条目总数"虚高，而总数是完成度的分母——
    分母错了，百分比就没有意义。实际踩过一次（1.7 被两条不同的行占用）。
    """
    ids = [match.group(0) for match in re.finditer(r"^\| (\d+\.\d+[a-z]?) \|", text, re.MULTILINE)]
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
        f"条目 {actual['total']}：✅ {actual['done']} · 🟢 {actual['divergent']} · "
        f"⚫ {actual['unverifiable']} · 🟡 {actual['partial']} · ⬜ {actual['todo']}"
    )
    print(f"  严格完成度      {completion(actual):.1f}%  = (✅+🟢) / 总数（离目标多远）")
    # **两个口径永远都打印**，哪怕 ⚫ = 0。只在 ⚫ > 0 时才打第二个数，读者根本不知道
    # 有第二个口径——双口径的意义就在于"总能看到两个数"。
    print(
        f"  可验证完成度    {verifiable_completion(actual):.1f}%  = (✅+🟢) / (总数−⚫)"
        f"（本机能验的部分做完多少；⚫ = {actual['unverifiable']}）"
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
    declared = {key: int(value) for key, value in match.groupdict().items() if value is not None}
    # 老格式没有 🟢 段：缺这一项时按 0 比，不强行要求头部写出来。
    declared.setdefault("divergent", 0)
    if declared != actual:
        print(f"头部声明 {declared} 与实际 {actual} 不一致", file=sys.stderr)
        return 1 if args.check else 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
