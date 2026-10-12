# 目标模式执行报告（2026-09-30）

执行方式：自主推进，不中途提问；需要拍板处按 `handoff/GOAL-MODE-PLAN.md` §2 的默认值直接决定。
范围：清掉三轮面经评估累积的护栏类未执行项（ALI F1–F8 / 腾讯 R1–R7 / RAG F3–F5 / 09-25 FIX-2~5）。

## 1. 结论

**护栏全部收口，基线从 96 提升到 104 测试，界面 13/13、检索评测 PASS，五项门禁全绿。**
连续两轮被面试题撞上的三个缺口（预算只记录不拦截、Reflector 不接线、检索无限次）本轮清零。
剩余未做项集中在"需要环境或产品范围"的三件事上，已在 §5 写明触发条件。

## 2. 逐目标：改动与验证

| 目标 | 改动文件 | 关键实现 | 验证 | 状态 |
|---|---|---|---|---|
| G1 预算三类硬拦 | `app/policies.py` | `check` 补 token（**满量**）与时间（`elapsed`）判断 | 3 个单测：三条停止原因 + 零阈值不拦死 | ✅ |
| G2 检索调用上限 | `app/tools.py` | `RETRIEVER_CALL_LIMIT=4`；计次走 `Operation` 表（重启后仍有效），超限回"请基于已有结果作答" | 单测：第 4 次前正常、第 5 次只回提示 | ✅ |
| G3 成本聚合 | `app/graph/nodes.py` | `finalize.cost_summary` → `final.cost{refs,tokens,bytes,actions,elapsed,citations}`，同名字段落 `finalize_completed` 事件 | 预算收尾用例断言 actions/tokens/bytes/elapsed | ✅ |
| G4 Reflector 接线 | `app/policies.py`、`app/graph/nodes.py`、`app/graph/builder.py` | 新增 `reflect` 节点（budget→reflect→think）；`DefaultReflector.reflect` 产出三类确定性纠偏（同类连续失败/6 步间隔/token 过半），触发时落 `reflection` 事件并注入一条 harness 提示；不满足条件时零动作（行为与从前一致） | 纯函数单测 + **集成测试**：六步任务在第 6 步落一条 `reflection`，任务照常收尾 | ✅ |
| G5 并发闸 + 版本可追溯 | `app/tools.py`、`app/context.py`、`app/graph/nodes.py`、`app/agent.py` | `TOOL_CONCURRENCY=4` + `ToolRegistry.slots`；`PROMPT_VERSION="2026-09-30.1"` 随 `message_started` 落库；`task_started` 带模型名 | 单测：事件里能读到版本号与模型名 | ✅ |
| G6 模型调用容错 | `app/graph/nodes.py`、`app/agent.py` | `ask_model` 有限重试 3 次（**流式已吐出正文则不重试**，避免重复内容），耗尽抛 `ModelFailureError` → Runner 落成可读 FAILED 文案 | 2 个单测：抖动后成功（2 条 `model_retry`、`message_started` 仍只有 1 条）；持续失败 → FAILED 文案含"模型连续 3 次调用失败" | ✅ |
| G7 切块参数扫描 | `scripts/eval_chunk_sweep.py`（新）、`data/chunk_sweep.json`（存档） | 复用 `eval_rag` 的 DDL/拆词/打分，替换 `indexing.py` 顶部常量重跑；两组语料（合成 / 仓库 6 篇真实中文文档） | 见 §3 数字 | ✅ |
| G8 决策收口 + 留档 | 本报告、`GOAL-MODE-PLAN.md`、`ARCHITECTURE.md`、三份评估报告 | 决策记录 + 过时表述修正（reflect 已进图、预算已硬拦、切块结论补注） | 文档一致性 | ✅ |

## 3. 关键发现

### 3.1 一个设计冲突：半量预警与满量拦截不能同阈值

F1/R1 原写"`token_estimate*2 >= max_tokens` 即停"。真把 reflect 接进图后，两条机制用同一条件会**互相遮蔽**：budget 先停，reflect 的"token 过半该收窄了"永远轮不到执行，等于又造了一个死分支。

裁决：**分层**——`reflect` 管半量预警（提醒模型收窄范围），`budget` 管满量硬停。两个阈值都落在测试里（`test_budget_target_is_reachable_after_the_reflection_warning`），保证"半量时还没被拦停"。

### 3.2 切块参数扫描：真实语料上大块略好，但结论仍是维持 300–500

| 语料 | 配置 | 块数 | trigram | chars | 融合 recall@5 | 融合 MRR@5 |
|---|---|---|---|---|---|---|
| synthetic（3 篇短文） | 300-500（现状） | 4 | 0.500 | 1.000 | 1.000 | 0.917 |
| synthetic | 1000-1200 | 4 | 0.500 | 1.000 | 1.000 | 0.917 |
| **repo_docs（6 篇真实文档）** | 300-500（现状） | 184 | 0.500 | 0.667 | **0.667** | **0.301** |
| repo_docs | 600-800 | 137 | 0.500 | 0.667 | 0.667 | 0.333 |
| repo_docs | 1000-1200 | 115 | 0.583 | 0.667 | **0.750** | **0.361** |
| repo_docs | 300-500 零重叠 | 184 | 0.500 | 0.667 | 0.667 | 0.308 |

三点结论：
1. **合成语料（4 块）根本不可分辨**——切块参数实验必须用长文语料，这也是把仓库设计文档作第二组语料的原因；
2. 关键词路上大块确实略好，但**12 条查询 + 长标记片段天然偏爱大块**，不足以推翻设计值；
3. 真正的硬约束是**向量路 bge 的 512 token 窗口**——1000–1200 中文字已明显越窗。**维持 300–500**，阶段 6 上线向量后用同一脚本复测。
4. 副作用收获：真实语料上 **融合 0.750 > chars 0.667 > trigram 0.500**，第一次在非合成语料上量化证明了双路 RRF 的增益（RAG-F5 一直缺的就是这个数）。

⚠️ **口径提醒**：`repo_docs` 语料读的是仓库当前文档，**文档一改块数就变**。首轮 184 块（融合 MRR 0.301），写完本报告后再跑是 187 块（0.318）——排名不分先后但绝对值会漂。要做趋势对比，先固定一份语料快照（把文档复制进 `data/` 再指向它）。

### 3.3 一次真实回归与处理：自动重试"吃掉"了用户可见的失败路径

加完模型重试后，界面冒烟出现 1 项失败：`chat stop, subsequent send and failed reply retry`。
根因不是产品 bug，而是 **fixture 语义过期**：`ui_fixture.py` 的 `UI_RETRY` 只失败一次（模拟抖动），以前这一次失败直接把任务打穿、用户看到"重试这次回复"按钮；现在被 think 的重试自己吃掉了，用户看不到失败态，用例等不到那个按钮。

处理（两个方向都保住）：
- 保留自动重试（抖动自愈对用户是纯收益）；
- 把 fixture 改成**连续失败满 `MODEL_ATTEMPTS` 次**（直接 import 该常量，避免漂移硬编码），于是"失败 + 重试这次回复"这条用户可见路径继续被测到。

重跑：**13/13 PASS**。这类"测试夹具与产品语义脱节"的坑值得记住：改行为时先问"哪个用例在验证旧语义"。

## 4. 验证证据

| 门禁 | 命令 | 结果 |
|---|---|---|
| 后端单测 | `cd backend && uv run pytest -q` | **104 passed**（96 → 104，新增 8） |
| 静态检查 | `uv run ruff check app tests ../scripts` | All checks passed |
| 类型 | `uv run mypy app` | no issues found (19 files) |
| 前端 | `npm run lint` / `npm run build` | 通过 / built in 3.22s |
| 界面冒烟 | `uv run python ../scripts/ui_smoke.py` | **13/13 PASS**（含 SSE 重连、停止后重试、失败重试） |
| 离线检索 | `uv run python ../scripts/eval_rag.py` | PASS（融合 recall@5 = 1.000，门槛 0.8） |
| 切块扫描 | `uv run python ../scripts/eval_chunk_sweep.py --json ../data/chunk_sweep.json` | 见 §3.2 |

## 5. 决策与未完成项（含触发条件）

| 项 | 本轮决定 | 触发条件 / 下一步 |
|---|---|---|
| 多模态（OCR / 图像 / ASR） | **不做** | 出现"必须看图才能完成"的真实任务；最小实现 = 浏览器截图作为 vision content 进 think（截图已在 `artifacts`）。需同时评估 token 成本 |
| 向量路（阶段 6 / 0005 迁移） | **维持推迟** | 出现"用词不同语义相同"的真实失败查询；checklist 见 `handoff/06-vector-hybrid.md`，届时先用 `eval_chunk_sweep` 复测切块 |
| agent 级评测基线（recall@5 / 引用覆盖率实数） | **先出数不设阈** | 需要真实模型 + 浏览器环境跑 `eval_run.py`（11 用例）；本机保留/断点续跑 |
| RAG-F3 真实语料评测集 | **仍待办**（本轮只是用仓库文档做了代理语料） | 真实任务跑起来后用真库 chunks + 失败查询回填 |
| 单 worker 抢占（F5） | **仍为设计稿** | 长任务中新任务只能排队，前端无"排队中"可见状态；浏览器独占是硬约束 |
| `max_actions` 默认值 | **保持 20** | 与 STOP_LABELS、收尾文案、评估集行为一致 |

## 6. 一句话交接

护栏已从"半成品"变成"真在拦"：预算三类硬停、自省真跑起来、检索有上限、失败有明确文案、成本可查。下一件值得做的事不是加功能，而是**把 agent 级评测基线跑出来**（需要真实模型环境），否则"更稳了"仍然只有单测和界面证据、没有任务级证据。
