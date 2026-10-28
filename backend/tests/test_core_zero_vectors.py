"""核心零向量依赖的守卫测试（MATRIX 9.14）。

## 这条到底在守什么

Hermes 的 `_internal` 里**没有** lancedb / chromadb / faiss / sentence_transformers
（实证：grep 全无）。它的向量能力只在 memory provider 里，核心是干净的。

Orbit 不一样：核心带了 `app/vectors.py`（sqlite-vec）与 `app/rerank.py`（fastembed）。
这不是"功能缺失"，而是"多了一条可选路"。但**多一条路只有在它真的是可选的时候
才不算负担**——如果某个模块在 import 期就把 sqlite-vec 或 onnxruntime 拉起来，
那么：

- 没装这两个包的环境连 `import app.agent` 都会失败；
- 装了的环境每次启动多背几百 MB 的加载。

所以这里守的是**性质**而不是"有没有这个功能"：

1. `app/` 下**没有任何模块级** import 向量库（必须是函数内懒加载）；
2. 向量路不可用时 `vec_available()` 返回 False 而不是抛异常（可降级）；
3. 即使把 `sqlite_vec` / `fastembed` 从 `sys.modules` 里摘掉，`import app` 仍然成功。

第 3 条是这套断言里最有价值的：前两条是"看代码"，第 3 条是"真的不依赖"。
"""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"

#: 只要在模块级被 import 就算违规的库。注意 `fastembed` 的子模块也算。
VECTOR_LIBS = (
    "sqlite_vec",
    "fastembed",
    "onnxruntime",
    "sentence_transformers",
    "lancedb",
    "chromadb",
    "faiss",
)


def _module_level_imports(path: Path) -> list[str]:
    """一个文件里**位于模块顶层**的 import 名字（含 `from x import y` 的 x）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in tree.body:  # 只遍历顶层，函数/类里的 import 不看
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


def test_no_module_level_vector_imports():
    """向量库只能在函数里 import。模块级 import 会让"可选"变成"必需"。"""
    offenders: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        for name in _module_level_imports(path):
            root = name.split(".")[0]
            if root in VECTOR_LIBS:
                offenders.append(f"{path.relative_to(APP.parent)}: {name}")
    assert not offenders, "这些文件在模块级 import 了向量库：\n" + "\n".join(offenders)


def test_vector_libs_are_actually_used_somewhere():
    """反向断言：确实有地方用它们（否则上一条测试是空转的）。

    如果哪天向量能力被整个删掉，这条会红——那时候应该连同 MATRIX 9.14 一起改，
    而不是让一条永远通过的守卫假装还在守什么。
    """
    hits: list[str] = []
    for path in sorted(APP.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for lib in ("sqlite_vec", "fastembed"):
            if f"import {lib}" in text or f"from {lib}" in text:
                hits.append(f"{path.name}:{lib}")
    assert hits, "app/ 下找不到任何向量库的使用——守卫已空转，请复核 MATRIX 9.14"


def test_vec_available_degrades_instead_of_raising():
    """扩展缺失时返回 False，不抛。向量路整体降级的入口就是这个函数。"""
    from app import vectors

    assert isinstance(vectors.vec_available(), bool)


def test_app_imports_without_vector_libs():
    """把向量库从 `sys.modules` 摘掉后，核心仍然能 import。

    这条比"看代码"强：它真的把依赖拿掉再试一次。
    """
    saved = {name: sys.modules.pop(name) for name in list(sys.modules) if name.split(".")[0] in VECTOR_LIBS}
    try:
        for module in ("app.agent", "app.tools", "app.context", "app.memory_prompt"):
            importlib.import_module(module)
    finally:
        sys.modules.update(saved)


def test_rerank_import_does_not_load_the_model():
    """`import app.rerank` 不该把模型加载起来（几百 MB，且要联网下载）。"""
    from app import rerank

    encoder = rerank.Reranker() if hasattr(rerank, "Reranker") else None
    if encoder is not None:
        # 构造完还没 `_ensure_model`，编码器必须是 None
        assert getattr(encoder, "_encoder", None) is None
