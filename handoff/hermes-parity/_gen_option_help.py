"""一次性脚本：把 Hermes ``--help`` 的 options / positional arguments 段抽成 JSON。

输入（只读）：

- ``research/_raw/cmd-NN-*.txt``            每个顶层命令的 ``--help`` 全文
- ``research/_raw/sub/<cmd>__<sub>.txt``    每个二级子命令的 ``--help`` 全文
- ``research/_raw/sub3/<cmd>__<sub>.txt``   每个三级子命令的 ``--help`` 全文

输出：``handoff/hermes-parity/_option_help.json``

    {"options": {"<命令路径>": {"--flag VALUE": "<英文 help 原文>"}},
     "positionals": {"<命令路径>": {"name": "<英文 help 原文>"}}}

argparse 的 help 会折行，续行比选项行缩进更深；这里把续行拼回一行。``-h/--help`` 跳过。
注意：``_raw/sub`` 与 ``_raw/sub3`` 下的文件带 UTF-8 BOM，必须用 ``utf-8-sig`` 读。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RAW = ROOT / "research" / "_raw"

#: ``  --flag, -f VALUE   说明`` 里的 flag 部分
OPTION_HEAD = re.compile(r"^  ((?:-[A-Za-z0-9], )?-{1,2}[A-Za-z0-9][^\s]*.*?)(?: {2,}(.*))?$")


def command_path(lines: list[str]) -> str:
    """从 usage 行取命令路径（``hermes a b c`` → ``a b c``），最多三级。"""
    for line in lines:
        if line.startswith("usage:"):
            tokens = line.split()[2:]
            path: list[str] = []
            for token in tokens:
                if token.startswith(("-", "[", "{")):
                    break
                path.append(token)
                if len(path) == 3:
                    break
            return " ".join(path)
    return ""


def sections(lines: list[str]) -> tuple[dict[str, str], dict[str, str]]:
    """切出 options / positional arguments 两段，续行拼回一行。"""
    options: dict[str, str] = {}
    positionals: dict[str, str] = {}
    mode = ""
    last: tuple[dict[str, str], str] | None = None
    for line in lines:
        stripped = line.strip()
        if stripped in {"options:", "positional arguments:", "optional arguments:"}:
            mode = "positional" if stripped == "positional arguments:" else "options"
            last = None
            continue
        if not mode:
            continue
        if not stripped:
            last = None
            continue
        if mode == "options":
            match = OPTION_HEAD.match(line)
            if match and line.startswith("  -"):
                head = match.group(1).strip()
                text = (match.group(2) or "").strip()
                if head in {"-h, --help", "--help"}:
                    last = None
                    continue
                options[head] = text
                last = (options, head)
                continue
        elif line.startswith("  ") and not line.startswith("   ") and not stripped.startswith("-"):
            name, _, text = stripped.partition("  ")
            name = name.strip()
            positionals[name] = text.strip()
            last = (positionals, name)
            continue
        # 续行：并回上一条
        if last is not None:
            target, key = last
            target[key] = f"{target[key]} {stripped}".strip()
    return options, positionals


def main() -> None:
    data: dict[str, dict[str, dict[str, str]]] = {"options": {}, "positionals": {}}
    files = (
        sorted(RAW.glob("cmd-*.txt"))
        + sorted(RAW.glob("sub/*.txt"))
        + sorted(RAW.glob("sub3/*.txt"))
    )
    for file in files:
        lines = file.read_text(encoding="utf-8-sig").splitlines()
        path = command_path(lines)
        if not path:
            continue
        options, positionals = sections(lines)
        data["options"].setdefault(path, {}).update(options)
        data["positionals"].setdefault(path, {}).update(positionals)
    out = ROOT / "_option_help.json"
    out.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
    print(f"files={len(files)} paths={len(data['options'])} -> {out}")


if __name__ == "__main__":
    main()
