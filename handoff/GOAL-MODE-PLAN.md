# 目标模式执行计划（GOAL MODE）

日期：2026-09-30。执行方式：**自主推进、逐项验证、减少人工介入**。
依据：三轮面经评估累积的未执行修复指令（`ALI_INTERVIEW_EVALUATION.md` F1–F8、
`AGENT_INTERVIEW_EVALUATION.md` R1–R7、`RAG_INTERVIEW_EVALUATION.md` F3–F5、
`handoff/2026-09-25-interview-eval-fixlist.md` FIX-2~5）。

## 0. 目标模式约定

1. **不中途提问**：需要拍板的地方按本文件 §2 的默认值直接决定并记录理由，留待事后复审；
2. **每步一验**：每个目标改完立刻跑对应验证（单测 / 门禁 / 评测脚本），通过才进入下一项；
3. **口径不降级**：验证基线沿用项目既有标准——`pytest`、`ruff check`、`mypy app`、前端 `lint`+`build`、`eval_rag` 门槛 0.8；
4. **可回滚**：改动集中在既有文件的小函数，不新建抽象层、不动迁移编号（0001–0004 冻结，0005 留给向量表）；
5. **留档**：过程与结果写进本文件 §3 执行记录 + `handoff/2026-09-30-goal-mode-report.md`。

## 1. 目标清单（按依赖与优先级排序）

| 目标 | 内容 | 来源 | 验收标准 |
|---|---|---|---|
| G1 | 预算三层真正生效：`DefaultBudgetPolicy.check` 补 token / 时间判断 | F1=R1（两轮未执行） | 单测锁定三条停止原因；`budget_stopped` 事件带对应 reason |
| G2 | `retriever_search` 每任务调用上限（4 次） | F2=R2 | 第 5 次返回明确提示而非继续检索；有单测 |
| G3 | 成本聚合：`finalize` 输出 `final.cost`（tokens/bytes/actions/elapsed/refs） | F4=R4 | 收尾结果含 cost；事件带同名指标 |
| G4 | Reflector 接线：新增 `reflect` 节点，产出确定性自省提示 | F3=R3（两轮未执行） | 图里存在 reflect 节点；触发条件命中时事件 + 提示注入；单测 |
| G5 | 工具并发闸 + 提示词/模型版本可追溯 | F6、R5 | 并发闸常量存在；`message_started` 带 prompt_version，`task_started` 带 model |
| G6 | 模型调用容错：流式前失败重试 3 次，仍失败给明确文案 | F7=R6 | 单测：连续失败 → 任务失败且文案明确；未产生重复正文 |
| G7 | chunk size 参数扫描（400 / 800 / 1200）出数字 | RAG-F4 | 扫描脚本落地 + 报告写入文档 |
| G8 | 收口决策与记录（多模态、向量、max_actions、KPI 阈值） | FIX-2/3/4、§9 待拍板 | 决策记录进报告；未做项写明触发条件 |

## 2. 默认决策（无人审查模式下的替代拍板）

| 待拍板项 | 默认决策 | 理由 |
|---|---|---|
| 多模态（OCR / 图像 / ASR） | **本轮不做，写进 ROADMAP 待触发** | 需产品范围与 token 成本权衡；当前 11 用例评估集无多模态用例，做了也无验证手段。触发条件：出现"必须看图才能完成"的真实任务 |
| 向量路（阶段 6 / FIX-3） | **维持推迟**，保留 `vector_candidates` 空实现 | 三轮评估一致结论：先 FTS5 后向量；无真实 badcase 时上向量是负收益。上线 checklist 见 `handoff/06-vector-hybrid.md` |
| `max_actions` 默认值（20 vs 30） | **保持 20** | 与现有 `STOP_LABELS`、收尾文案、评估集行为一致；提高上限会让预算收尾更晚触发，收益不明 |
| token 预算是否拦截 | **拦截**（G1 实现，阈值沿用 `default_max_tokens=200000`，按估算值 ×2 判半量） | 只记录不拦截是连续两轮被撞的缺口；`token_estimate` 是粗估，故留 ×2 余量 |
| KPI 阈值（recall@5 / 引用覆盖率） | **离线 recall@5 门槛 0.8 保持**；agent 级两列**先出数不设阈** | 无真实模型环境，设阈无依据 |
| `structured` 是否含 iframe / 懒加载 | 维持现状，不在本轮范围 | 属观察层改动，与本次护栏收口无关 |

## 3. 执行记录

（逐项完成后追加：目标 / 改动文件 / 验证命令 / 结果 / 偏差）

| 目标 | 状态 | 改动 | 验证 |
|---|---|---|---|
| G1 | ✅ | `app/policies.py`：check 补 token（满量）与时间 | 3 单测通过 |
| G2 | ✅ | `app/tools.py`：`RETRIEVER_CALL_LIMIT=4`，计次走 Operation 表 | 单测：第 5 次只回提示 |
| G3 | ✅ | `app/graph/nodes.py`：`cost_summary` → `final.cost` + 事件 | 收尾事件断言 |
| G4 | ✅ | `policies.py`/`nodes.py`/`builder.py`：`reflect` 节点接进图 + 确定性提示 | 纯函数单测 + 六步集成测试 |
| G5 | ✅ | `tools.py` 并发闸；`context.PROMPT_VERSION` 随事件；`task_started` 带模型名 | 事件断言单测 |
| G6 | ✅ | `nodes.ask_model` 有限重试 + `ModelFailureError`；`agent.py` 文案 | 2 单测（成功/失败两路） |
| G7 | ✅ | `scripts/eval_chunk_sweep.py`（新）+ `data/chunk_sweep.json` | 真实语料 184 块四档对比，数字见报告 §3.2 |
| G8 | ✅ | 报告 + 三份评估报告状态回填 + `ARCHITECTURE.md` 纠偏 | 全量门禁：104 passed / ruff / mypy / lint+build / ui 13/13 / eval_rag PASS |

**偏差记录**：F1/R1 原方案"token 半量即停"与 reflect 的半量触发同阈值互相遮蔽 → 改为
**reflect 管半量预警、budget 管满量硬拦**（报告 §3.1）。界面冒烟出现 1 次回归（自动重试吃掉
用户可见失败路径）→ 改 fixture 而非改产品（报告 §3.3），重跑 13/13。

详细过程与证据：`handoff/2026-09-30-goal-mode-report.md`。
