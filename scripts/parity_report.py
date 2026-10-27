"""生成 CLI 对标矩阵（Orbit 命令 ↔ Hermes 命令）。

为什么要有这个脚本：``app/cli/spec.py`` 的注册表是命令面的**唯一真相源**，
``handoff/hermes-parity/MATRIX.md`` 是给人看的对照表。两者必须能一键重算，
否则文档必然漂移成"历史记录"。

用法（在 backend 目录下）：

    .\\.venv\\Scripts\\python.exe ..\\scripts\\parity_report.py
    .\\.venv\\Scripts\\python.exe ..\\scripts\\parity_report.py --out ..\\handoff\\hermes-parity\\cli-matrix.md
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))

from app.cli import REGISTRY  # noqa: E402  （需要先插好 sys.path）

HEADER = """# Orbit CLI 对标矩阵（自动生成）

> 由 `scripts/parity_report.py` 从 `backend/app/cli/spec.py` 的注册表生成，**不要手改**。
> 重算：`cd backend; .\\.venv\\Scripts\\python.exe ..\\scripts\\parity_report.py`

"""


def render() -> str:
    rows = REGISTRY.parity_rows()
    counts = Counter(row["state"] for row in rows)
    total = len(rows)
    done = counts.get("done", 0)
    lines = [HEADER]
    lines.append(f"命令条目合计 **{total}**：已实现 {done}、部分 {counts.get('partial', 0)}、"
                 f"待实现 {counts.get('todo', 0)}\n")
    lines.append("| 命令 | 对标 Hermes | 归类 | 优先级 | 状态 | 说明 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for row in rows:
        state = {"done": "✅ 已实现", "partial": "🟡 部分", "todo": "⬜ 待实现"}[row["state"]]
        lines.append(
            f"| `{row['command']}` | `{row['hermes']}` | {row['group']} | "
            f"{row['parity']} | {state} | {row['help']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 Orbit CLI 对标矩阵")
    parser.add_argument("--out", metavar="PATH", help="写文件；不给则打印到 stdout")
    args = parser.parse_args()
    text = render()
    if args.out:
        target = Path(args.out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"已写入 {target}（{len(text)} 字符）")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
