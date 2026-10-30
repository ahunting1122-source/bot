"""门禁：`covered` 的工具必须**真的注册进应用**。

## 为什么这条比 `audit_tool_mappings` 更进一步

`audit_tool_mappings` 查的是「目录声称的名字，在某个 `*_TOOLS` 列表里存在」。
**它不查那个列表有没有被 `registry.register()` 接进应用。**

于是会出现这种情况：**工具对象写好了、目录也标了 covered，
但装配时忘了注册 —— 模型永远看不到它。**
这正是「接上了但没接完」的最隐蔽形态：**连测试都可能是绿的**（单测直接调 execute）。

## 怎么查

真起一次应用（`create_app()` + `TestClient`），拿 `ToolRegistry` 里实际的工具名，
与目录里所有 `covered` 的映射名比对。
"""

from __future__ import annotations

import pathlib
import sys
import tempfile

BACKEND = pathlib.Path(r"E:\bbbooottt\backend")
sys.path.insert(0, str(BACKEND))

from app.tool_catalog import HERMES_TOOLS  # noqa: E402


def registered_names() -> set[str]:
    from starlette.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    root = pathlib.Path(tempfile.mkdtemp(dir=str(BACKEND), prefix="_reg_"))
    settings = Settings(data_dir=root / "d", workspace=root / "w")
    settings.prepare()
    app = create_app(settings=settings)
    try:
        with TestClient(app):
            registry = app.state.registry
            return set(getattr(registry, "tools", {}) or {})
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    real = registered_names()
    print(f"应用里实际注册的工具：{len(real)} 个")

    claimed: list[tuple[str, str]] = []
    for tool in HERMES_TOOLS:
        if tool.orbit_status != "covered":
            continue
        for name in tool.orbit_tools:
            claimed.append((tool.name, name))

    missing = [(hermes, orbit) for hermes, orbit in claimed if orbit not in real]
    print(f"目录声称 covered 的映射：{len(claimed)} 条")

    if not missing:
        print()
        print("**每一条 covered 映射的工具都真的注册进了应用。**")
        return 0

    print()
    print(f"**{len(missing)} 条声称覆盖、但应用里没有注册：**")
    for hermes, orbit in missing:
        print(f"  {hermes:26} → {orbit}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
