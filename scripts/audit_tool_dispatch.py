"""把每个注册工具都通过注册表调一次（第 114 轮）。

## 为什么这是最有价值的一扫

第 113 轮靠「通过注册表调一次」抓到 `PreviewTool.execute` 是同步的 ——
**单测直调发现不了，因为它不经注册表。**

**那就把所有工具都过一遍。**

## 怎么判断「有问题」

- `status=error` + 一句可读的话（如「请配置 X」「需要 channel_id」）→ **正常**；
- `TypeError` / `AttributeError` / `SignatureError` → **结构性错误**（同步执行器、参数不匹配…）；
- 其它异常 → 记下来，可能是真 bug，也可能是「没配置就走不到」。

**用空参数调是故意的**：空参数会让每个工具走到它自己的「参数不足」分支，
**而结构性错误会在那之前就炸。**
"""

from __future__ import annotations

import asyncio
import pathlib
import shutil
import sys
import tempfile

BACKEND = pathlib.Path(r"E:\bbbooottt\backend")
sys.path.insert(0, str(BACKEND))

#: 这些异常类型说明「接线方式错了」，不是「参数不够」。
STRUCTURAL = (TypeError, AttributeError, NotImplementedError)


async def main() -> int:
    from starlette.testclient import TestClient

    from app.config import Settings
    from app.main import create_app

    root = pathlib.Path(tempfile.mkdtemp(dir=str(BACKEND), prefix="_sweep_"))
    settings = Settings(data_dir=root / "d", workspace=root / "w")
    settings.prepare()
    app = create_app(settings=settings)
    structural: list[tuple[str, str]] = []
    other: list[tuple[str, str]] = []
    ok = 0
    try:
        with TestClient(app):
            registry = app.state.registry
            names = sorted(getattr(registry, "tools", {}) or {})
            print(f"注册工具：{len(names)} 个，逐个通过注册表调用（空参数）")
            for i, name in enumerate(names):
                try:
                    await registry.execute("sweep", f"sweep-{i}", name, {})
                    ok += 1
                except STRUCTURAL as exc:
                    structural.append((name, f"{type(exc).__name__}: {exc}"))
                except Exception as exc:  # noqa: BLE001
                    other.append((name, f"{type(exc).__name__}: {str(exc)[:80]}"))
                else:
                    pass
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print(f"正常返回：{ok}")
    print(f"结构性错误（接线错）：{len(structural)}")
    for name, why in structural:
        print(f"  **{name:24} {why[:90]}**")
    print(f"其它异常：{len(other)}")
    for name, why in other[:10]:
        print(f"  {name:24} {why}")

    return 1 if structural else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
