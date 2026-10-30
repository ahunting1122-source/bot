"""一条命令跑完所有门禁。

## 为什么需要它

到第 104 轮，门禁有 **11 道** + `ruff` + `mypy`，`HANDOVER.md` 3.14 里是一串命令。
**任何人接手都要复制粘贴十几行、还要记得 `PYTHONIOENCODING` 和 `cd backend`。**

**这个脚本把那串命令收成一个入口，并给出一张「哪道绿哪道红」的表。**

## 用法

```powershell
cd E:\\bbbooottt
.\\backend\\.venv\\Scripts\\python.exe scripts\\check_all.py
```

**exit 0 = 全绿；非 0 = 至少一道红。**

## 口径

- **`audit_skipped_tests` / `audit_cited_skips` / `audit_hardcoded_counts` 是报告式的**，
  恒返回 0 —— 它们列出来给人看，不替你判死。这里照样跑，但标注「报告式」。
- **`mypy` 与 `ruff` 要在 `backend/` 里跑**（配置在 `backend/pyproject.toml`）。
"""

from __future__ import annotations

import os
import subprocess
import sys

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
PY = str(ROOT / "backend" / ".venv" / "Scripts" / "python.exe")
SCRIPTS = ROOT / "scripts"
BACKEND = ROOT / "backend"

#: (标签, argv, cwd, 是否报告式)
CHECKS: list[tuple[str, list[str], object, bool]] = [
    ("matrix_stats --check", [PY, str(SCRIPTS / "matrix_stats.py"), "--check"], ROOT, False),
    ("audit_matrix_citations", [PY, str(SCRIPTS / "audit_matrix_citations.py")], ROOT, False),
    ("audit_matrix_flags", [PY, str(SCRIPTS / "audit_matrix_flags.py"), "--check"], ROOT, False),
    ("audit_tool_mappings", [PY, str(SCRIPTS / "audit_tool_mappings.py")], ROOT, False),
    ("audit_test_claims", [PY, str(SCRIPTS / "audit_test_claims.py")], ROOT, False),
    ("audit_skipped_tests", [PY, str(SCRIPTS / "audit_skipped_tests.py")], ROOT, True),
    ("audit_cited_skips", [PY, str(SCRIPTS / "audit_cited_skips.py")], ROOT, True),
    ("audit_hardcoded_counts", [PY, str(SCRIPTS / "audit_hardcoded_counts.py")], ROOT, True),
    ("audit_doc_consistency", [PY, str(SCRIPTS / "audit_doc_consistency.py")], ROOT, False),
    ("audit_research_lines", [PY, str(SCRIPTS / "audit_research_lines.py")], ROOT, False),
    ("ruff check .", [PY, "-m", "ruff", "check", "."], BACKEND, False),
    ("mypy app", [PY, "-m", "mypy", "app"], BACKEND, False),
]


def main() -> int:
    env = dict(os.environ)
    # **不设这个，matrix_stats 在 GBK 控制台会 UnicodeEncodeError**
    env["PYTHONIOENCODING"] = "utf-8"

    results: list[tuple[str, int, bool]] = []
    for label, argv, cwd, report_only in CHECKS:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        results.append((label, completed.returncode, report_only))
        mark = "OK " if completed.returncode == 0 else "RED"
        suffix = "（报告式）" if report_only else ""
        print(f"  {mark} {label}{suffix}")
        if completed.returncode != 0:
            tail = (completed.stdout or completed.stderr or "").strip().splitlines()[-3:]
            for line in tail:
                print(f"        {line[:110]}")

    hard_failures = [label for label, code, report_only in results if code != 0 and not report_only]
    print()
    if hard_failures:
        print(f"**{len(hard_failures)} 道红：**{hard_failures}")
        return 1
    print(f"全部 {len(results)} 项通过（其中 {sum(1 for _, _, r in results if r)} 项是报告式，恒返回 0）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
