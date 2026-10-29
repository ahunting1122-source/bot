"""审计 MATRIX 里提到的每一个 `--flag` 是否真的存在于 Orbit 的 CLI。

## 为什么需要它

第 63 轮就是这样抓到 `--usage-file` 的：MATRIX 声称 23 个运行 flag「完整」，
而其中一个在 `app/cli/` **全目录零命中**。**「参数出现在文档里」不等于「它存在」。**

## 用法

```
python scripts/audit_matrix_flags.py           # 只报告
python scripts/audit_matrix_flags.py --check   # 有可疑项时 exit 1
```

## 口径（重要，别把它当绝对判定）

- **只查长选项**（`--xxx`）：短选项在散文里歧义太大。
- **权威来源是 parser 的 action 名单**，不是 `--help` 输出 ——
  很多选项是 SUPPRESS 的，`--help` 里看不到但确实能用。
- **会误报**：MATRIX 的散文里会出现**别的程序**的参数（实测两例：
  `msedge --app=http://…`、`pip install … --retries 10`）。
  所以脚本**打印上下文让人判断**，而不是直接判死。
  `--check` 只对「上下文里看不出是别的程序」的项返回 1。

**判据：审计脚本的价值不在「能判死」，在「把需要人看一眼的地方集中起来」。**
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"
MATRIX = pathlib.Path(__file__).resolve().parent.parent / "handoff" / "hermes-parity" / "MATRIX.md"
sys.path.insert(0, str(BACKEND))

#: MATRIX 里出现、但属于**别的程序**的参数（实测确认过的）。
FOREIGN = {
    "--app": "在 `msedge --app=http://…` / `chrome --app=http://…` 里（浏览器参数）",
    "--retries": "在 `pip install … --retries 10` 里（pip 参数）",
}


def collect_flags() -> set[str]:
    """从真实 parser + 注册表里收集所有长选项。"""
    import argparse as ap

    from app.cli import main as cli_main
    from app.cli import spec

    known: set[str] = set()

    def walk(parser: ap.ArgumentParser, depth: int = 0) -> None:
        if depth > 3:
            return
        for action in parser._actions:
            for option in action.option_strings:
                if option.startswith("--"):
                    known.add(option)
            choices = getattr(action, "choices", None)
            if isinstance(choices, dict):
                for sub in choices.values():
                    if isinstance(sub, ap.ArgumentParser):
                        walk(sub, depth + 1)

    walk(cli_main.build_parser())
    walk(cli_main._global_options())
    for command in spec.REGISTRY.all():
        for option in getattr(command, "options", ()):
            for flag in getattr(option, "flags", ()):
                if flag.startswith("--"):
                    known.add(flag)
    return known


def main() -> int:
    parser = argparse.ArgumentParser(description="审计 MATRIX 里的 --flag 断言")
    parser.add_argument("--check", action="store_true", help="有未解释的可疑项时 exit 1")
    args = parser.parse_args()

    known = collect_flags()
    text = MATRIX.read_text(encoding="utf-8")
    mentioned = sorted(set(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]{1,40})", text)))
    unknown = [flag for flag in mentioned if flag not in known]

    print(f"CLI 里已知长选项：{len(known)}")
    print(f"MATRIX 里提到：{len(mentioned)}")
    print()

    unexplained: list[str] = []
    for flag in unknown:
        if flag in FOREIGN:
            print(f"  已知外来参数 {flag} —— {FOREIGN[flag]}")
            continue
        unexplained.append(flag)
        for line in text.splitlines():
            if flag in line and re.match(r"^\| [0-9]", line):
                ident = line.split("|")[1].strip()
                at = line.find(flag)
                print(f"  **{flag}** ← 行 {ident}")
                print(f"      上下文：…{line[max(0, at - 80) : at + 40]}…")
                break
        else:
            print(f"  **{flag}** ← 不在数据行里")

    print()
    if unexplained:
        print(f"**{len(unexplained)} 个无法解释：{unexplained}**")
        return 1 if args.check else 0
    print("MATRIX 提到的每一个长选项都存在，或已登记为外来参数。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
