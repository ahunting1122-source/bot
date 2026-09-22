# Agent 编排设计（讨论稿 · 计划模式）

日期：2026-09-22。**只设计，不实现。** 先定框架与链，未完成的能力只留接口。
背景与现状见 `AGENT_DESIGN.md`。

## 0.6 v1 权限模型：完全访问（2026-09-22 用户决定）

用户明确：**权限不要卡着，第一版做完全访问，尽量减少人工操作。** 这改变了一个核心节点的语义。

**新原则**

- **事前零确认**：所有工具（含浏览器动作、MCP 工具、凭证登录、对外写入）直接执行，不再逐项征求同意。
- **人工介入只剩两种**：① 模型主动 `ask_user`（真登录墙、验证码、缺关键输入）② 用户主动点停止。
- **信任模型从事前确认转到事后审计**：正因为不卡权限，每一次外部动作的**落库记录 + 对外动作清单**从加分项变成必需品（digest / finalize 承担）。
- **预算成为唯一自动刹车**：动作数、token、时间三类预算必须先做，默认值收紧。

**节点语义变化**

| 节点 | 旧语义 | v1 语义 |
|---|---|---|
| `gate` | 确认判定 + 接管 + ask_user | **只处理人工介入**：`ask_user`（模型主动求助）与工具返回 `needs_user`（真登录墙）。确认分支整体移除 |
| 站点级免确认 | 按主机名授权 | 代码保留，v1 策略 = 全放（该机制退化为审计分组标签） |
| `takeover_allowed` 门槛 | 保留 | **保留**——它本身就是"减少人工"的手段（逼模型先自己登录） |
| `finalize` | 收尾 | 新增职责：**对外动作清单**（本任务在哪些站点做了什么） |
| MCP `auto_approve` | 按服务显式开启 | v1 策略 = 全部视为开启（配置项保留，向后兼容） |

**风险如实记录**：完全访问意味着模型可以不经确认在站点上提交、发布、产生费用。v1 的缓解手段只有三个——审计清单、预算刹车、停止按钮。**建议 v1 体验期使用低风险账号**，这是使用建议而不是代码限制。

## 0. 本轮目标与非目标

**目标**

1. 把编排从 `think → gate → execute` 扩成**可生长的链**：加能力时插节点，不改主干。
2. 状态里**显式表达进度与预算**，不再只靠消息流。
3. 为四件没做完的事（技能、检索、结果分级、计划与反思）**留出接口**：协议 + 默认空实现，行为与今天完全一致。
4. 建 `tool_results` 表，让 `digest` 把工具结果原文落库并登记引用（**这一轮模型看到的仍是全量**，落库是为了先拿到观测数据）。

**非目标**：本轮不实现技能内容、不上向量、不改 MCP、不动前端、不做并行。

**硬约束**：每一步都必须能单独关掉。新节点默认空转，关掉后系统行为与当前版本逐字节一致——否则无法定位回归。

## 1. 设计原则

| 原则 | 含义 |
|---|---|
| 空转即兼容 | 新节点默认 no-op；接口默认实现 = 当前行为 |
| 接口先于实现 | 未完成的能力只定义 `Protocol`，不写假逻辑、不留 TODO 分支 |
| 进度结构化 | 计划、预算、引用都进 state，可被节点读写、可被 UI 展示 |
| 收尾而非抛错 | 预算耗尽、连续失败、达到上限都要带进度收尾并说明差什么 |
| 可观测 | 每次注入了哪些技能/检索片段、压缩掉了什么，都要留痕（调试用） |

## 2. 新的链

```
                 ┌──────────── plan（预留：生成/更新计划）────────────┐
                 │                                                    │
START → prepare → think ⇄ gate → execute → digest → budget ──继续──→ gate
                    │                        │         │
                    │                        │         └─超限─→ finalize
                    │                        └─原文入库─→（引用进上下文）
                    └─无工具调用─→ finalize
                                     ↑
                          reflect（预留：周期性自省）─→ think
```

| 节点 | 职责 | 本轮 |
|---|---|---|
| `prepare` | 装载会话与任务、注入记忆/摘要；**预留**技能筛选与检索注入 | 新建，先只做现状搬家 |
| `think` | 决定下一步：调工具 / 直接回答 / 更新计划 | 保留，接入预算与引用 |
| `gate` | 确认、接管、拒绝（已有） | 保留 + 接管门槛 |
| `execute` | 执行工具（已有） | 保留 |
| `digest` | 工具结果分级：原文入库 + 登记引用；摘要进上下文 | 新建；**建 `tool_results` 表并落库**，但默认策略仍全量透传，模型看到的不变 |
| `budget` | 三类预算检查，决定继续/收尾 | 新建骨架，默认只查动作数 |
| `reflect` | 周期性自省：是否有进展、是否换策略 | 新建节点，默认不触发 |
| `plan` | 生成/更新结构化计划 | 不新建节点，先让 think 写 `state["plan"]` |
| `finalize` | 收尾：产出回答、标记未完成步骤 | 新建 |

**边**

```
START → prepare → think
think  ──有 calls──→ gate ──→ execute ──→ digest ──→ budget
think  ──无 calls──→ finalize → END
budget ──继续──→ gate（还有积压 calls）| think
budget ──收尾──→ finalize
execute ──needs_user/uncertain──→（gate 挂起，已有语义保留）
```

`plan` / `reflect` 本轮只作为 **state 字段与预留节点签名**存在，不进图；接口就绪后再接线。

## 3. 状态设计

```python
class AgentState(TypedDict):
    # ── 已有，语义不变 ─────────────────────────
    task_id: str
    messages: Annotated[list[AnyMessage], add_messages]
    calls: list[dict]
    actions: int
    answer: dict
    waiting: dict

    # ── 新增：进度与预算 ───────────────────────
    plan: dict          # {"steps": [{"goal","status","evidence"}], "current": int, "revised_at": str}
    budget: dict        # {"max_actions", "used_actions", "token_estimate", "max_tokens",
                        #  "deadline", "stop_reason"}
    refs: dict          # {result_id: {"tool","summary","bytes","tokens"}}   ← 结果分级的引用表

    # ── 新增：可观测与预留 ─────────────────────
    context_meta: dict  # {"skills": [...], "retrieved": [...], "compressed": {...}}
    reflections: list   # 最近的短结论，供 think 参考
```

**兼容性**：新字段全部有默认值；`prepare` 负责初始化。旧 checkpoint 恢复时缺字段按默认值补齐（读取处统一走 `.get()`）。

## 4. 接口清单（未完成功能的占位）

全部放在 `app/policies.py`，以 `Protocol` 定义。默认实现放在同文件，显式命名为 `DefaultXxx`，一眼能看出"这是当前行为"。

| 接口 | 职责 | 默认实现 | 谁来补 |
|---|---|---|---|
| `ContextSource` | 往系统提示追加内容（记忆、摘要、技能、检索片段） | 记忆 + 摘要注入（现状） | 技能、RAG |
| `ToolResultPolicy` | 决定工具结果进上下文的形式 | 全量返回（现状） | A1 结果分级 |
| `BudgetPolicy` | 动作/token/时间三类预算判定 | 只判 `actions >= max_actions`（现状） | A3 三层预算 |
| `ToolExposer` | 这轮暴露哪些工具 | 全部（现状） | 工具数超阈值时 |
| `SkillProvider` | 列出相关技能简述 / 取技能正文 | 空：不提供技能 | C 技能层 |
| `Retriever` | 检索片段（本地文件、历史、工具结果） | 关键词子串（现状三处） | B2 FTS5 / B3 向量 |
| `Planner` | 生成/更新计划 | 不调用；计划由模型在 think 内隐式给出 | A2 计划状态 |
| `Reflector` | 周期性自省 | 空：不触发 | A2 reflect 节点 |
| `SubAgentRunner` | 执行子任务 | 未实现，接口留空 | 远期 |

**接入方式**：`AgentRunner.__init__` 收一组 policy，默认全用 `Default*`。换实现只改注入点，不动节点代码。

## 5. 目录与代码关系

现有 `agent.py` 已经 400 行上下，再加 4 个节点会难维护。建议拆包，但**不搬已有逻辑的语义**：

```
app/graph/
  state.py      AgentState + 默认值 + 旧 checkpoint 兼容读取
  nodes.py      prepare / think / gate / execute / digest / budget / reflect / finalize
  builder.py    组装 StateGraph（今天的 AgentRunner.start() 搬过来）
app/policies.py 接口协议 + 默认实现
app/agent.py    只留 AgentRunner（队列、worker、取消、生命周期）
```

迁移方式：先建包并把 `think/gate/execute` **原样搬过去**（不改一行逻辑），跑通测试；再插新节点。这样出问题能明确归因到"搬家"还是"新增"。

## 6. 分步实施（整体计划 · 已含 v1 权限模型）

**默认值（由我决定，可随时改配置）**：动作上限 30 / token 估算上限 200k / 单任务 10 分钟 / MCP 连接超时 20 秒 / reflect 每任务最多 2 次。审计事件永久保留（本地库）。

| 阶段 | 内容 | 出口标准 |
|---|---|---|
| 0 | **拆包搬家**：`think/gate/execute` 原样进 `app/graph/`，`agent.py` 只留队列与生命周期 | 61 测试 + 10 用例评估集零回归 |
| 1 | **权限切换**：gate 移除确认分支，只留 `ask_user` / `needs_user` 两条人工路径；站点免确认机制退化为审计标签；MCP 默认免确认 | 跑一个真实多步任务，**全程零人工确认**完成 |
| 2 | **digest + `tool_results` 表**（alembic 0003）：原文落库、checksum 幂等、大小/token 估算、引用登记 | 能回答"哪一步最贵、结果多大"；模型看到的内容不变 |
| 3 | **预算三层 + `finalize`**：动作/token/时间；超限带"已完成/未完成清单 + 下一步建议 + 对外动作清单"收尾 | 人为把上限调低触发一次，收尾可读且不抛错 |
| 4 | **`plan` 派生**：harness 从工具轨迹生成计划；`context_meta` 记录注入与压缩 | 真实任务后能回看"注入了什么、计划走到哪" |
| 5 | **RAG 写入侧**：`chunks` 表 + FTS5(trigram)，挂 `digest` 之后；来源含网页正文与工具结果 | 网页内容与工具结果可被检索，每块带 origin |
| 6 | **RAG 读取侧**：`retriever_search` 工具（范围按任务）+ `prepare` 预取 + **引用回填硬约束** | 检索类任务回答必须带来源，否则判失败 |
| 7 | **Skill 层**：SKILL.md + 加载器 + `skill_load` 工具；先手工写 3 个（含 1 个站点操作技能） | 同一站点做第二遍，步骤数明显少于第一遍 |
| 8 | **向量**：`Embeddings` 接口 + 远端实现 + sqlite-vec + 混合检索（RRF） | 出现"用词不同语义相同"的查询能召回 |
| 9 | **MCP 补齐**：心跳 + 指数退避重连 + `resources` 隔离接入 RAG | 服务崩了自愈；resources 内容可被检索 |
| 10 | **存储收敛**：grants/mcp_servers/settings 入 DB，JSON 只留启动必需 | 配置真值只剩 keyring 与 DB |

**顺序理由**：0 是地基；1 是本轮最大的行为变化，越早做越早暴露自主模式的真实问题；2/3 紧随其后——没有审计与预算，完全访问就是裸奔；5/6 让"做过的事"可检索、可追溯；7/8/9 在链路稳定后叠加。

**每阶段的回归门槛**：上一阶段全部测试 + 评估集通过，才进下一阶段；评估集用例随阶段增加（阶段 1 加"零确认完成多步任务"用例，阶段 6 加"回答必须带来源"用例）。

## 7. 风险与取舍

| 风险 | 处理 |
|---|---|
| 拆包引入回归 | 第 1 步只搬家不改逻辑，用现有 61 项测试 + 10 用例评估集卡住 |
| checkpoint 旧状态缺字段 | 统一走 `.get()` + `prepare` 补默认值；不做数据迁移 |
| 节点变多导致调试困难 | 每个节点进出都写 `task_events`，前端时间线按事件回放（已有 SSE 通道） |
| 过早抽象 | 只抽象**已知要补的 9 个点**，不为想象的需求加层；每个 Protocol 不超过 3 个方法 |

**明确不做**：不引入消息总线、不引入依赖注入框架、不为并行做准备、不做多 Agent 调度。这些等真有需求再说。

## 7.5 节点 I/O 矩阵（谁读谁写哪些 state 字段）

| 节点 | 读 | 写 |
|---|---|---|
| `prepare` | task_id、checkpoint（补默认值） | messages（初始）、plan（初始化）、budget（初始化）、context_meta |
| `think` | messages、plan、budget、refs、reflections、context_meta | calls（新调用）、answer（若直接回答）、plan（可修订）、context_meta（本次注入记录） |
| `gate` | calls[0]、waiting、refs（确认时展示参数） | answer（放行/拒绝/接管反馈）、waiting（挂起载荷） |
| `execute` | calls[0]、answer、refs | messages（ToolMessage）、waiting（needs_user 时） |
| `digest` | 上一次工具结果、refs | refs（登记引用）、context_meta（大小/token）、**tool_results 表（库）** |
| `budget` | budget、actions、时钟 | budget（used/stop_reason）、calls（清空积压则回 think） |
| `reflect`（预留） | plan、messages 尾部、budget | reflections、plan（可修订） |
| `finalize` | plan、budget、refs、messages 尾部 | answer（收尾文本）、任务状态 |

**原则**：每个节点只写自己负责的字段；`plan` 只有 prepare/think/reflect 能写；`budget` 只有 prepare/budget 能写。这样出了问题能直接归因到节点。

## 7.6 编排待定细节（本轮要讨论完的五个）

**O1. `budget` 判"继续"之后回到哪？**
现状推导：`parallel_tool_calls=False`，一次 think 只产出一个调用，所以 execute 之后 calls 必空。
→ **结论：回 `think`**（重新决策），而不是回 `gate`。gate 只在"有积压调用"时才有意义。
待确认：是否保持禁用并行工具调用？我推荐**保持禁用**——并行工具调用的结果交错会让 digest 和确认变复杂，收益存疑。

**O2. 计划由谁写？**
三个候选：(a) harness 从工具轨迹派生（零模型负担，但只能反映"做过什么"，不含"打算做什么"）；(b) 独立 `plan` 节点单独一次 LLM 调用；(c) 让 think 显式调用 `update_plan` 工具。
→ **结论：先 (a)（harness 派生），`plan` 节点（b）留作预留接口**，等发现 (a) 的计划质量不够再上 (b)。理由：(c) 受"单次只产出一个调用"限制，会和真工具抢位置。

**O3. 挂起恢复后从哪个节点继续？**
现状：`interrupt()` 发生在 `gate`，`Command(resume=...)` 恢复时**重进 gate**，state 从 checkpoint 原样带回来。
→ **结论：保持现状**。恢复语义是"用户对同一个待确认调用给了答复"，而不是"重新决策"——重进 gate 是对的。风险点：恢复后 `execute` 里的幂等检查必须命中（operation_id 相同），已有测试覆盖。

**O4. `reflect` 什么时候触发？**
三个候选：固定每 N 步 / 连续失败触发 / 预算过半触发。
→ **结论：三个条件取或**——`连续 2 次同类工具 error`、`每 6 步`、`token 预算消耗过半`。且 reflect 每任务最多跑 2 次（防循环）。数值先拍脑袋，跑真实任务后校准。

**O5. 两条"等用户"路径要不要合并？**
现状有两条：① gate 里 `interrupt()`（工具需确认 / ask_user）② 工具返回 `needs_user` → waiting → 下一轮 gate 再 interrupt。两条路径最终都走 WAITING_USER，但入口不同、恢复语义相同。
→ **结论：统一成一条**。工具返回 `needs_user` 时不再绕道 waiting 字段，直接由 execute 把它转成 gate 的 interrupt 载荷（带 reason/tool/args）。少一个中间状态，UI 侧只有一种"等用户"形态。**这条是五条里唯一需要改代码语义的**，收益是状态机变简单。

## 7.7 挂起与恢复的完整语义（现状梳理）

```
任务运行中 ──gate interrupt──→ WAITING_USER（checkpoint 已含挂起点）
                                   │
                    ┌──────────────┼──────────────┐
                    ▼              ▼              ▼
              用户答复/批准    用户点停止      进程重启
                    │              │              │
                    ▼              ▼              ▼
            resume→重进 gate   CANCELLED      recovery_check
            （state 原样带回）  （保留已有结果） （RUNNING 任务→WAITING_USER）
                    │
                    ▼
            execute 重放（operation_id 幂等命中）
```

**重启恢复的特例**：浏览器是不可序列化的，恢复后必然是"重新观察页面"而不是"回到当时的 DOM"。这是 by design，前端要如实告诉用户"已恢复，但页面状态需要重新确认"。

| 决定 | 结论 |
|---|---|
| 代码结构 | **拆成 `app/graph/` 包**：`state.py` / `nodes.py` / `builder.py`，`agent.py` 只留 `AgentRunner`（队列、worker、取消、生命周期）。第 1 步只搬家不改逻辑 |
| `digest` 落库 | **现在就建 `tool_results` 表**（alembic 0003），原文入库 + 引用登记一步到位 |
| `finalize` 产出 | **已完成 / 未完成清单 + 下一步建议**。因此 `plan` 字段必须最早可用——它从第 2 步起就要被写入 |
| MCP / 技能 / 检索 / 向量 | 本轮不动，只留接口 |

**实施注意事项**（来自这三条决定）

- `plan` 的优先级被 `finalize` 依赖**提前**：第 2 步就要让 `think` 每次写下当前对计划的判断，第 6 步只是把它写得更结构化。
- `tool_results` 建表后，`digest` 必须做**幂等**：同一个 `operation_id` 重复执行（恢复、重试）只落一条，用 checksum 判重。
- 拆包后的第一件事是**跑通现有测试**，确认"搬家"零回归，再动新节点。
