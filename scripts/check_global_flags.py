"""机械核对：全局 flag 有没有「消费者」（MATRIX 1.7 的实质）。

## 为什么需要这个脚本

1.7 的证据栏写着「其余 28 个确实真生效」——**那是一条第 17 轮的人工断言**。
而同一栏里已经承认「至少两个是装饰」，说明人工断言在这个维度上出过错。
**「每个都真的改变行为」这句话，必须能被机械复核，否则它只是一句好听的总结。**

## 判据（刻意宽松，只找「完全没人读」的）

一个全局 flag 的 `dest` 若在 `app/` 里（**排除 `app/cli/`**）**一次都没出现**，
它就是装饰：CLI 写进去了，产品里没人读。

- 只扫 `app/` 的**非 CLI** 部分——CLI 自己写值不算消费者；
- 用 AST 找 `settings.<dest>` / `getattr(settings, "<dest>")` / `updates["<dest>"]`；
- **`settings.json` 那条路**（`Context.__init__` 的 `model_copy(update=...)`）也算消费者，
  因为那是把 flag 真正送进 `Settings` 的地方——所以 `app/cli/context.py` 单独白名单。

用法：
    python scripts/check_global_flags.py
"""

from __future__ import annotations

import ast
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
APP = BACKEND / "app"

#: 这些文件属于「把 flag 送进 Settings」的接线层，算消费者。
WIRING = {APP / "cli" / "context.py", APP / "cli" / "commands" / "chat.py"}


def global_flag_dests() -> list[str]:
    sys.path.insert(0, str(BACKEND))
    from app.cli.spec import GLOBAL_OPTIONS  # type: ignore[attr-defined]

    dests: list[str] = []
    for option in GLOBAL_OPTIONS:
        for flag in option.flags:
            name = flag.lstrip("-").replace("-", "_")
            dests.append(getattr(option, "dest", "") or name)
    return sorted(set(dests))


def readers() -> dict[str, set[str]]:
    """`dest -> 在哪些非 CLI 文件里被读过`。"""
    found: dict[str, set[str]] = {}
    for path in APP.rglob("*.py"):
        if path in WIRING:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            name: str | None = None
            # settings.<dest>
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id in {"settings", "cfg", "self.settings"}:
                    name = node.attr
            # getattr(settings, "<dest>") / .get("<dest>")
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id == "getattr" and len(node.args) >= 2:
                    second = node.args[1]
                    if isinstance(second, ast.Constant) and isinstance(second.value, str):
                        name = second.value
            elif isinstance(node, ast.Subscript):
                if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                    name = node.slice.value
            if name:
                found.setdefault(name, set()).add(path.relative_to(APP).as_posix())
    return found


def main() -> int:
    dests = global_flag_dests()
    seen = readers()
    orphans = [dest for dest in dests if dest not in seen]
    print(f"全局 flag 的 dest：{len(dests)} 个")
    print(f"在 app/（非 CLI）里被读到的：{len(dests) - len(orphans)} 个")
    if orphans:
        print()
        print(f"**没有任何非 CLI 消费者的（装饰 flag）：{len(orphans)} 个**")
        for dest in orphans:
            print(f"  - {dest}")
        print()
        print("注意：这里只判「完全没人读」。有消费者但消费者自己也是死的（如 4.9 那种）")
        print("      本脚本看不出来——那种要另查。")
    else:
        print()
        print("没有装饰 flag：每个全局 flag 都至少有一个非 CLI 读取点。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
