# 方向 1 · Agent 编排拆包与链骨架

**先读 `handoff/00-CONTEXT.md`。**

## 目标

把编排从 `agent.py` 单文件拆成 `app/graph/` 包，并把链扩为：
`START → prepare → think ⇄ gate → execute → digest → budget →（继续→gate / 收尾→finalize → END）`。
`plan` / `reflect` 本轮**只留 state 字段与节点签名，不进图**。

## 已定决策

- 拆包：`app/graph/state.py`（AgentState+默认值+旧 checkpoint 兼容）、`nodes.py`（8 个节点）、`builder.py`（组装 StateGraph）；`agent.py` 只留 AgentRunner（队列/worker/取消/生命周期）
- 9 个 Protocol 放 `app/policies.py`：ContextSource / ToolResultPolicy / BudgetPolicy / ToolExposer / SkillProvider / Retriever / Planner / Reflector / SubAgentRunner，每个配 `Default*` 实现＝当前行为
- `budget` 判"继续"后回 `think`（并行工具调用保持禁用）
- 挂起恢复保持现状：interrupt 在 gate，恢复重进 gate，execute 靠 operation_id 幂等
- `reflect` 触发三条件取或：连续 2 次同类工具 error / 每 6 步 / token 过半；每任务最多 2 次
- digest 本阶段建 `tool_results` 表（alembic 0003）并落库（checksum 幂等），但**模型看到的工具结果保持全量**（引用式回灌是后续方向）

## 节点 I/O 约束

`plan` 只有 prepare/think/reflect 能写；`budget` 只有 prepare/budget 能写；每个节点只写自己负责的字段。新 state 字段（plan/budget/refs/context_meta/reflections）全部带默认值，旧 checkpoint 缺字段按默认补（读取走 `.get()`）。

## 验收

1. 第一步只搬家不改逻辑：61 测试 + 10 用例评估集全过，行为逐字节一致
2. 之后每插一个节点跑全量测试 + `scripts/agent_probe.py` 真实任务对比
3. budget 超限时不再抛"已达到最大工具动作数"通用错误，而是 finalize 给"已完成/未完成清单 + 下一步建议"

## 禁区

不做并行工具调用、不做子 Agent、不引入预置 ReAct / 重框架、不动 MCP 与 RAG（其他方向负责）。

## 涉及文件

`backend/app/agent.py`、`backend/app/graph/*`（新）、`backend/app/policies.py`（新）、`backend/app/tools.py`（Registry 已支持外部工具与 unregister_source）、`backend/migrations/versions/0003_*`（新）、`backend/tests/test_agent.py`、`scripts/agent_probe.py`
