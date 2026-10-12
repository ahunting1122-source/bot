# 方向 2 · 工具结果治理与 tool_results 表

**先读 `handoff/00-CONTEXT.md`。**

## 目标

工具结果分级治理的第一步：**原文落库 + 引用登记 + 大小/token 观测**。本阶段模型看到的工具结果**保持全量**（引用式回灌是后续方向的开关）。

## 已定决策

- 新表 `tool_results(id, operation_id, task_id, name, text, bytes, tokens, hash, created_at)`，alembic 迁移 0003
- `digest` 逻辑放在 ToolRegistry.execute 末尾（事件之后）：原文入库、估算 token、登记 `refs`；幂等靠 checksum 判重（同 operation_id 重复执行只落一条）
- 结果大小上限：工具结果文本截 16000 字（现状）；超过的先截断再入库，`truncated` 标记
- `finalize`（本阶段一起做）新增职责：输出**对外动作清单**——本任务在哪些站点做了什么（从 tool_results 过滤 name 以 browser_/vault_/notion_ 开头且 status=ok 的记录）

## 涉及文件

`backend/app/tools.py`（execute 末尾挂 digest）、`backend/app/storage.py`（新模型）、`backend/migrations/versions/0003_tool_results.py`（新）、`backend/app/graph/nodes.py`（若方向 1 已拆包则 digest 是独立节点，否则挂在 execute 后）、`backend/tests/test_tool_results.py`（新）

## 验收

1. 真实跑一个 B 站任务后，`tool_results` 里有每一步的原文、bytes、tokens，且同一 operation_id 重跑不产生重复行
2. `agent_probe` 的轨迹里能看到每步的大小
3. 全量测试与评估集不回归

## 禁区

不要在本阶段实现"引用式回灌"（摘要+fetch_result），那是下一个方向；不要把截图（artifacts）也切块入库。

## 依赖

方向 1（graph 包）先行——digest 作为独立节点需要节点骨架；若方向 1 未完成，可先建表与落库函数，digest 节点后补。
