# 方向 4 · RAG 读取侧：检索工具与引用回填硬约束

**先读 `handoff/00-CONTEXT.md`。依赖：方向 3（chunks 表与 FTS5 已建）。**

## 目标

让检索真正被用起来：三种触发 + 一个检索工具 + **引用回填硬约束**。

## 已定决策

- 检索范围**默认按任务收窄**（当前会话/站点/来源类型），全局检索需显式指定——防止噪声撑爆上下文
- 混合检索：FTS5 BM25 ‖ 向量余弦 → RRF 融合（本阶段向量路为空实现，接口先留；向量在方向 6 接）
- 新工具 `retriever_search(query, scope?, source_type?, top_k≤5)`：只读，默认免确认；结果 `[{text, origin, score}]`，注入量 ≤2500 字
- 三种触发：① `prepare` 按目标预取（≤5 片段）② 模型显式调 `retriever_search` ③ digest 发现结果过薄时补取（可选）
- **引用回填 = 硬约束**：检索类任务的最终回答必须包含所用片段的 `origin`；实现方式——SYSTEM 提示写死规则 + `finalize` 校验（回答里没有任何已注入片段的 origin → 回答附"未引用来源"提示并标记任务部分完成）
- 注入的每个片段记进 `context_meta.retrieved`，便于回放与评估
- 检索器实现走 `BaseRetriever` 形状（LangChain 接口对齐），`Retriever` Protocol 已在 `app/policies.py` 预留

## 评估补齐（本方向一并做）

跑批报告（`scripts/eval_run.py`）新增两个检索指标：**recall@5**（预期 origin 是否在注入片段里）与 **引用覆盖率**（回答中的 origin 占注入片段比例）。为此在 `eval_cases.json` 的检索类用例里加 `expect_origins` 字段。

## 涉及文件

`backend/app/retrieval.py`（新：查询构造、混合检索、RRF）、`backend/app/graph/nodes.py`（prepare 预取）、`backend/app/tools.py`（注册 retriever_search）、`backend/app/context.py`（SYSTEM 补引用规则）、`scripts/eval_cases.json`、`scripts/eval_run.py`、`backend/tests/test_retrieval.py`（新）

## 验收

1. 检索类任务（如"上次 B 站搜到的键盘信息再整理一遍"）能命中历史 chunks 并在回答里给出 origin
2. 无来源的回答被 finalize 标记
3. 评估报告出现 recall@5 与引用覆盖率两列
4. 全量测试与评估集不回归

## 禁区

不上 rerank（等召回质量有真实瓶颈）、不做查询改写与 multi-hop（预留接口）、不在无 chunks 数据时假装检索成功（空结果如实返回）。
