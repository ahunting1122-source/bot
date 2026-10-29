"""机械核对：MATRIX 证据栏里引用的测试文件是否真的存在。

## 为什么做这个

MATRIX 有 181 行，其中大量证据栏写着「`tests/test_xxx.py` N 条」。
**如果那个文件不存在，那条 ✅ 就是凭空的** —— 而这正是本项目反复打的
「文档说完成、代码查无实据」。

**判据：「让 181 条都可复核」比「多做一条」更值 —— 多做一条只增加 1/181，
而让整份文档可复核，增加的是可信度本身。**

## 这个检查只做一件窄事

**引用存在性。** 它不证明测试断言了那件事（那要人读），但它能挡住
「引用一个不存在的文件」这种最硬的假。
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(r"E:\bbbooottt")
MATRIX = ROOT / "handoff" / "hermes-parity" / "MATRIX.md"
TESTS = ROOT / "backend" / "tests"

text = MATRIX.read_text(encoding="utf-8")

# 抓证据栏里的 `tests/xxx.py` 与 `test_xxx.py`
cited = set(re.findall(r"`(tests/[A-Za-z0-9_]+\.py)`", text))
cited |= set(re.findall(r"`(test_[A-Za-z0-9_]+\.py)`", text))

missing: list[str] = []
present: list[str] = []
for name in sorted(cited):
    path = TESTS / pathlib.PurePath(name).name
    (present if path.exists() else missing).append(name)

print(f"MATRIX 引用的测试文件：{len(cited)} 个")
print(f"  存在：{len(present)}")
print(f"  不存在：{len(missing)}")
for name in missing:
    print(f"    - {name}")

# 反向：有多少测试文件从没在 MATRIX 里出现过（只报数，不判错）
all_tests = {p.name for p in TESTS.glob("test_*.py")}
cited_names = {pathlib.PurePath(name).name for name in cited}
uncited = sorted(all_tests - cited_names)
print()
print(f"tests/ 里共 {len(all_tests)} 个测试文件，其中 {len(uncited)} 个没在 MATRIX 里被引用")
print("（没被引用不是错——很多是内部回归测试，不对应 MATRIX 行）")

sys.exit(1 if missing else 0)
