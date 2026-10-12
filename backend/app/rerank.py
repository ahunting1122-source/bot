"""RAG 融合的第三档：cross-encoder 重排。

`HARNESS_DESIGN.md §4.3` 把检索后端分成三阶段：① FTS5 → ② 向量 → ③ 重排。
这里补的是第三阶段。动机来自两条实测/调研证据（见
`handoff/2026-10-08-hermes-deep-dive.md §6` 与 `handoff/vector-path-plan.md`）：

1. Orbit 第二轮实测：给向量路**加权是 wash**（三档权重差异 ≤1 条查询），而 B 集
   "向量单路 0.917 → 三路融合 0.750"——**融合本身在丢分**。换句话说，两路 FTS 用 OR
   词组查询几乎总能凑满候选（即使全是噪声），其 rank-1 噪声拿 `1/61` 就能与向量路的
   正确命中同量级，**靠调权重搬不动**。
2. Hermes（NousResearch）的 hybrid 检索把融合并列成三档：`rrf`（等权）/ `linear`（加权）
   / `cross-encoder`（重排），其中**真正解决"融合失真"的是重排**——先出候选池，再用
   cross-encoder 对 `(query, doc)` 逐对打分重排。

所以这里做的不是"再调一个权重"，而是：**RRF 只负责召回候选池，排序交给重排**。

`rerank` 与 `Embeddings.embed` 一样是**同步 CPU 调用**（ONNX 推理），调用方必须放
`asyncio.to_thread` 里，绝不直接在事件循环里调。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("orbit.rerank")

#: 默认重排模型。fastembed 0.8.1 的 cross-encoder 列表里，只有两个支持中文：
#: `BAAI/bge-reranker-base`（1.04GB）与 `jinaai/jina-reranker-v2-base-multilingual`
#: （1.11GB）；其余（ms-marco-MiniLM 系列、jina-reranker-v1-tiny/turbo-en）都是英文。
#: bge-reranker-base 与既有 bge 系同源、显存/内存开销更可控，故为默认。
#: 注意它是 **1GB 级**模型，本机必须 `enable_cpu_mem_arena=False`（见 vectors.py 的实测）。
DEFAULT_RERANK_MODEL = "BAAI/bge-reranker-base"
#: 候选池大小的默认值（配置里可覆盖）。**重排只能重排池内的候选**——
#: 所以 `recall@pool` 是重排效果的硬天花板，池子太小就直接丢召回。
DEFAULT_RERANK_POOL = 20


class NullReranker:
    """默认空转：不改变顺序，行为与接入重排之前逐字一致。

    没有模型、未开启、或加载失败时都用它——**如实降级**，不假装重排过。
    """

    enabled = False
    model = ""

    def rerank(self, query: str, documents: list[str], *, top_k: int) -> list[int]:
        return list(range(min(max(0, top_k), len(documents))))


class FastEmbedReranker:
    """fastembed + 本地 ONNX cross-encoder：内容不出本机，不依赖 torch。

    首次运行按 `rerank_model` 下载模型（约 1GB，落到 `resolve_cache_dir`，不进系统临时目录）。
    模型**懒加载**：构造时不联网、不占内存，第一次 `rerank` 才加载。
    """

    enabled = True

    def __init__(
        self,
        model_name: str = DEFAULT_RERANK_MODEL,
        *,
        cache_dir: str | None = None,
        cpu_mem_arena: bool = False,
        threads: int = 0,
    ):
        self.model = model_name
        self.cache_dir = cache_dir
        self.cpu_mem_arena = cpu_mem_arena
        self.threads = threads
        self._encoder: Any = None

    def _ensure_model(self):
        if self._encoder is None:
            from fastembed.rerank.cross_encoder import TextCrossEncoder

            kwargs: dict[str, Any] = {
                "model_name": self.model,
                "enable_cpu_mem_arena": self.cpu_mem_arena,
            }
            if self.cache_dir:
                kwargs["cache_dir"] = self.cache_dir
            if self.threads:
                kwargs["threads"] = self.threads
            try:
                self._encoder = TextCrossEncoder(**kwargs)
            except Exception as exc:  # 首次要联网下载，代理/墙会拦
                raise RuntimeError(
                    f"加载重排模型 {self.model} 失败：{exc}。"
                    "首次运行需联网下载模型（缓存到 embedding_cache_dir，之后离线）；"
                    "若被网络或代理拦截，可设 HF_ENDPOINT=https://hf-mirror.com 后重试"
                    "（注意：镜像对 Xet 托管的仓库会返回 401，此时改用 HF_HUB_DISABLE_XET=1）。"
                ) from exc
        return self._encoder

    def score(self, query: str, documents: list[str]) -> list[float]:
        """逐对打分（与 `rerank` 同源，单独暴露便于单测与调试）。"""
        bodies = [text for text in (documents or []) if text is not None]
        if not bodies:
            return []
        return [float(value) for value in self._ensure_model().rerank(query, bodies)]

    def rerank(self, query: str, documents: list[str], *, top_k: int) -> list[int]:
        documents = list(documents or [])
        if not documents:
            return []
        scores = self.score(query, documents)
        if len(scores) != len(documents):
            raise RuntimeError(f"重排打分条数 {len(scores)} != 文档数 {len(documents)}")
        # 同分保持稳定（按原顺序），避免 RRF 的顺序被无谓打乱
        order = sorted(range(len(scores)), key=lambda index: (-scores[index], index))
        return order[: max(0, top_k)]


def build_reranker(settings) -> Any:
    """按 `Settings` 构造重排器；未开启或依赖缺失时**如实退回空转**。"""
    if not getattr(settings, "rerank_enabled", False):
        return NullReranker()
    try:
        from fastembed.rerank.cross_encoder import TextCrossEncoder  # noqa: F401
    except Exception as exc:  # fastembed 版本过旧 / 未装
        log.warning("重排已开启但 fastembed cross-encoder 不可用，退化为不重排：%s", exc)
        return NullReranker()
    from app.vectors import resolve_cache_dir

    return FastEmbedReranker(
        model_name=str(getattr(settings, "rerank_model", DEFAULT_RERANK_MODEL) or DEFAULT_RERANK_MODEL),
        cache_dir=resolve_cache_dir(settings),
        cpu_mem_arena=bool(getattr(settings, "embedding_cpu_mem_arena", False)),
        threads=int(getattr(settings, "embedding_threads", 0) or 0),
    )
