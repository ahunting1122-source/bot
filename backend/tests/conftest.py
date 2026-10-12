"""全局测试夹具。

**为什么要有这个文件**：向量路（gemma 1.24GB）与重排（bge-reranker-base 1.04GB）
在 2026-10-08 收口时改成了**默认开启**（线上默认配置）。但测试套件不应该因此去加载
2.3GB 模型——那会让每个碰 `Settings` 的用例都慢几分钟，且把"检索正确性"和"模型能否加载"
两件事混在一起（后者已有 `test_vectors.py` / `test_rerank_routing.py` 用桩覆盖）。

所以这里用一个 **autouse 夹具**把两个开关按环境变量关掉：`Settings` 通过
`ORBIT_*` 环境变量读取，构造时会拿到关闭态。需要真模型/真重排的用例可自行显式
传 `vector_enabled=True` / `rerank_enabled=True` 覆盖（`Settings(**overrides)` 优先于环境）。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _disable_heavy_retrieval_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """默认关掉向量/重排模型，避免测试套件加载 2.3GB ONNX 权重。

    单个用例若要验证真实默认开的行为，显式传参即可（见 `test_vectors.py` 的
    `test_vector_path_enabled_by_default`、`test_rerank_routing.py` 的
    `test_policies_default_wires_a_reranker`——它们直接断言 `Settings` 的类默认值，
    不受本夹具影响，因为类默认值在 `Settings` 定义处，环境变量只影响实例化）。
    """
    monkeypatch.setenv("ORBIT_VECTOR_ENABLED", "0")
    monkeypatch.setenv("ORBIT_RERANK_ENABLED", "0")
