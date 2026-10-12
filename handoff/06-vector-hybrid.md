# 方向 6 · 向量检索：本地 Embedding + 混合检索

**先读 `handoff/00-CONTEXT.md`。依赖：方向 3（chunks/FTS5 已建）、方向 4（检索接口与引用回填已通）。**

## 目标

给混合检索补上向量这一路：**本地 Embedding + sqlite-vec**，与 FTS5 做 RRF 融合。

## 已定决策（用户拍板：向量要上，embedding 走本地）

- **Embedding 实现**：`fastembed` + `BAAI/bge-small-zh-v1.5`（中文专项、ONNX 推理、**不依赖 torch**、~100MB 首次下载后离线）；走 `Embeddings` Protocol，可替换
- **向量存储**：`sqlite-vec` 扩展（虚拟表），与现有 SQLite 同进程单文件；用 `VectorStore` 形状薄封装
- **上下文对齐**：bge 最大输入 512 token——切块长度必须与之对齐（写入侧已在方向 3 定为 300–500 中文字 + overlap，需按中文≈token 折算校验，超限在句子边界再切）
- **异步**：embedding 计算放后台（挂 digest 之后），不阻塞工具返回；查询时单条 embedding 可接受
- **混合检索**：FTS5 BM25 ‖ 向量余弦 → **RRF 融合**（k=60）→ top-K；两路各自归一后融合
- **触发**：与方向 4 相同的三种触发，向量路在混合检索内部生效

## 已知约束（如实告知用户）

1. **DeepSeek 没有 embedding 接口**——这是当初把 embedding 做成可替换接口的原因；本地小模型方案已拍板，不再考虑远端
2. 中文查询在 bge-small-zh 上效果依赖切块质量——**先保证方向 3 的切块规则落地**，否则向量召回无意义
3. CPU 推理：批量索引放后台，单条查询几十 ms 可接受

## 涉及文件

`backend/app/embeddings.py`（新：`Embeddings` Protocol + `FastEmbedEmbeddings` 实现）、`backend/app/vector_store.py`（新：sqlite-vec 封装）、`backend/app/retrieval.py`（混合检索 + RRF）、`backend/pyproject.toml`（加 fastembed、sqlite-vec）、`backend/tests/test_vector.py`（新）

## 验收

1. 索引后用"同义不同词"查询（如索引里是"机械键盘评测"，查"买哪种键盘好"）能召回正确 chunk——这是向量存在的唯一理由
2. 混合检索（FTS5+向量 RRF）优于任一单路的手工对比记录
3. 全量测试与评估集不回归；embedding 下载与推理不阻塞工具调用

## 禁区

不做 rerank（等有真实排序瓶颈）、不引入 Chroma/LanceDB/远端向量库、不上 GPU 依赖、不用会外发内容的远端 embedding。
