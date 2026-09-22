# Agent 架构现状与优化设计

日期：2026-09-22。讨论稿。第一部分是**事实**（代码里是什么样），第二部分是**问题**，第三部分是**优化方案与取舍**。

---

# 第一部分：现状

## 1. Agent 框架

单进程、单 worker、单层状态图。没有子 Agent，没有规划节点，没有反思节点。

```
FastAPI
  └─ AgentRunner（进程内单例）
       ├─ queue: asyncio.Queue[task_id]     一次只跑一个任务
       ├─ work(): 常驻协程，串行消费队列
       └─ run(task): 每个任务一次 graph.ainvoke
            └─ StateGraph（thread_id = task.id）
                 think ──有工具调用──→ gate ──→ execute ──还有调用──→ gate
                   ↑                              │
                   └──────────没有更多调用────────┘
```

| 节点 | 职责 | 关键实现 |
|---|---|---|
| `think` | 组装上下文、绑定工具、流式生成 | 查动作上限 → `build_context()` → `bind_tools(registry.specs())` → `astream` 逐字推 SSE |
| `gate` | 决定是否需要用户介入 | 工具需确认 / `ask_user` / 上一步返回 `needs_user` → `interrupt()` 挂起 |
| `execute` | 执行工具、回灌结果 | `operation_id = f"{task_id}:{call_id}"`，结果作 `ToolMessage` 回灌 |

状态字段只有五个：`task_id`、`messages`、`calls`、`actions`、`answer`、`waiting`。

**恢复**：图状态存 `checkpoints.sqlite`（LangGraph SqliteSaver），进程重启后按 thread 恢复。
**取消**：`asyncio.Task.cancel()` + 状态置 `CANCELLED`。
**并发**：单 worker，没有任务级并行，也没有工具级并行（`parallel_tool_calls=False`）。

## 2. 数据库

三个 SQLite 文件 + 四个配置文件，配置散落。

| 存储 | 内容 | 说明 |
|---|---|---|
| `data/app.sqlite` | 业务数据，7 张表 | 见下表 |
| `data/checkpoints.sqlite` | LangGraph 图状态 | 与业务库分离，`store.lock` 管不到它 |
| `data/settings.json` | 模型、网页、已记住的站点操作 | 由 `ConnectorConfig` 独占读写 |
| `data/vault.json` | 站点账号**名称**与更新时间 | 值在 keyring |
| `data/mcp.json` | MCP 服务声明 | 刚加 |
| 系统 keyring | 模型 Key、GitHub Token、站点密码、MCP 密钥 | 唯一放真值的地方 |
| `data/workspace/` + `data/artifacts/` | 文件与截图 | 路径存 `artifacts` 表 |

业务表：

| 表 | 关键字段 | 现状问题 |
|---|---|---|
| `conversations` | id, title, summary, summary_status, message_revision | 摘要与标题共用一个字段区，状态机靠字符串 |
| `messages` | id, conversation_id, role, content, created_at | **没有 tool_call_id、没有 token 数、没有 kind**；工具消息只活在 checkpoint 里 |
| `tasks` | id, goal, status, waiting(JSON), error | `waiting` 是自由 JSON |
| `task_events` | id(自增), task_id, event_type, payload(JSON) | SSE 回放靠它，无过期策略 |
| `memories` | id, key(唯一), value, source | **没有作用域**（计划里的 project_key 没做）、没有来源消息、没有失效标记 |
| `tool_calls` | id, task_id, name, status, args(JSON), result(JSON) | 结果直接存 JSON，大结果会撑大库 |
| `artifacts` | id, task_id, kind, location, label | 没有 checksum、没有大小、没有引用计数 |

已开 WAL。写操作用 `Store.lock` 串行化；读不加锁。

## 3. 向量 / RAG 库

**不存在。** 没有任何 embedding、向量索引或召回代码。

当前"检索"只有三条：

1. `local_search_text`：遍历工作目录，逐文件 `casefold().find()` 关键词，最多 30 条命中，单文件截 16000 字。
2. `memory_search`：对 `memories.value` 做关键词匹配。
3. `history_search_tasks`：只在历史任务的 `goal` 里匹配关键词。

上下文注入是**固定预算**：最近 30 条消息、24000 字符、最近 12 条偏好、整段会话摘要。全部塞进每一次 `think`。

## 4. MCP

只做**客户端**，这一版刚落地。

| 能力 | 状态 |
|---|---|
| 本地进程（stdio） | ✅ |
| 远程（streamable HTTP + headers） | ✅ 代码就绪，未实测 |
| 工具发现与注册 | ✅ 启动时 `list_tools`，名字规整为 `mcp__<服务>__<工具>` |
| 工具调用 | ✅ 结果转成 `{content, structured, is_error}` |
| 确认策略 | ✅ 默认逐个确认，可按服务 `auto_approve` |
| 密钥 | ✅ 配置文件写 `keyring:名称`，值在 keyring |
| 资源（resources） | ❌ 未接 |
| 提示模板（prompts） | ❌ 未接 |
| 采样 / elicitation 回调 | ❌ 未接（模型反向调用由 MCP 服务发起的能力） |
| 服务端模式（自己作为 MCP 服务被别人用） | ❌ 未做 |
| 断线重连 | ⚠️ 只有手动 `reload`，没有自动重试与心跳检测 |

## 5. Skill

**不存在。** 没有 skill 概念、没有技能目录、没有触发机制。

现在的"能力"只有一层：工具（`tool`）。工具回答的是"**能做什么**"（点这个元素、读这个文件），但没有任何东西承载"**该怎么做**"——比如"在 B 站搜索并翻页抽字段"的正确步骤、某个站点的坑、一类任务的通用流程。

结果就是：**每次任务都从零开始试**。前面实测里模型能自己摸索出来，靠的是模型能力，不是系统积累。会话之间唯一沉淀下来的是 `memories` 里的键值偏好。

## 6. Loop 设计

单层循环，终止条件只有"动作数上限"和"模型不再调用工具"。

```
每次 think 都：
  build_context()   ← 重新查库组装（最近 30 条 + 12 条记忆 + 摘要）
  bind_tools(全部工具)  ← 现在 23 个，接更多 MCP 后会继续涨
  流式生成
每次 execute 都：
  全量工具结果 → ToolMessage → 进 messages → 下一轮继续进上下文
```

| 维度 | 现状 |
|---|---|
| 循环层级 | 1 层（任务内含多步） |
| 终止条件 | `actions >= 20` 抛错；模型自然停止 |
| 预算管理 | 只有动作数。**没有 token 预算、没有时间预算** |
| 上下文压缩 | 无。靠"最近 30 条"这个粗粒度窗口 |
| 工具结果处理 | 全量进上下文。一次网页观察可能带 100+ 元素 + 14000 字正文 |
| 错误处理 | 结果回给模型自我调整；只有 `TargetClosedError` 在代码里自动重试一次 |
| 计划表示 | **无**。计划只存在于模型脑子里和消息流里 |
| 反思 | 无 |
| 并行 | 无 |
| 子任务 | 无。长任务在同一个上下文窗口里跑到底 |

---

# 第二部分：问题（按痛感排序）

1. **上下文会爆**。工具结果全量进窗口，网页观察尤其重。跑十几步就到模型窗口上限，而且每一步都重新塞一次系统提示 + 30 条消息，token 成本随步数线性上升。这是当前最硬的瓶颈。
2. **没有计划状态**。任务做到第 7 步，模型"忘了"目标或重复做同一件事，系统没有任何机制发现——因为进度没有被结构化表达，也没有地方检查。
3. **能力不积累**。同一个站点、同一类任务，做第二遍和第一遍一样贵。Skill 缺失导致"经验"无法沉淀。
4. **工具列表线性增长**。23 个工具已经不少；接几个 MCP 服务后可能上百个。每轮都把全部工具定义塞给模型，既贵又容易选错。
5. **配置散落四处**。同一类东西（站点授权、MCP 服务、凭证）一半在 DB 一半在 JSON 文件一半在 keyring，迁移、备份、审计都难受。
6. **messages 表信息不足**。没有 tool_call_id / token 数 / kind，工具轨迹只在 checkpoint 里，出了 checkpoint 就查不到"这一轮到底调了什么"。
7. **单 worker 串行**。长任务期间不能开新任务（前端会直接拒绝），体验上像"单线程助手"。
8. **恢复粒度粗**。只有 checkpoint 级恢复；`tool_calls` 表虽然记了 operation_id，但没有和消息、artifact 串成一条可读的因果链。

---

# 第三部分：优化方案

## A. Loop 重构（最高优先）

**A1. 引用式工具结果（解决 1、8）**

工具结果分两级：**摘要 + 引用**进上下文，**原文**留在库里。

```
工具执行完 →
  原文写入 tool_results（新表，带 checksum/大小/token 估算）
  返回给模型的是：状态 + 结构化摘要 + result_id
  模型需要细节时调用 fetch_result(result_id, 范围) 按需取回片段
```

网页观察天然适用：`observe` 只回"页面标题 + 正文摘要 + 元素按类型分组的前 N 个"，完整元素表和截图引用化。

**A2. 显式计划状态（解决 2）**

`AgentState` 增加 `plan`：步骤清单 + 当前步 + 每步状态 + 结果引用。

```
think 时可以更新计划（加步/改步/标记完成）
每 N 步或每次工具失败后插入 reflect 节点：
  「目标是什么 / 已完成什么 / 卡在哪 / 是否换策略」
超过动作上限时不是抛错，而是用当前进度收尾并说明还差什么
```

**A3. 三层预算（解决 1、7）**

| 预算 | 触发时行为 |
|---|---|
| 动作数（已有） | 收尾并报告剩余步骤 |
| Token 估算 | 先压缩旧工具结果为引用，再不行才收尾 |
| 墙钟时间 | 收尾并说明耗时去向 |

**A4. 工具按需暴露（解决 4）**

工具超过阈值（比如 30 个）后分两级：核心工具常驻，其余按当前上下文动态筛选（站点相关 / 会话相关 / MCP 服务分组）。模型也可以显式 `list_tools(关键词)` 找工具。

## B. 记忆与 RAG（次高）

**B1. 先补结构化记忆，再谈向量（解决 3、5）**

`memories` 加 `scope`（global / project / site）、`source_message_id`、`active`。把"站点偏好"和"通用偏好"分开——现在混在一张表里，注入时只能按时间取最近 12 条。

**B2. 全文检索先上 FTS5，别急着上向量**

SQLite 自带 FTS5，`trigram` 分词对中文可用，零新依赖、离线、快。**先用它把 `local_search_text` / `memory_search` / `history_search_tasks` 三个关键词匹配换掉**，同时新增"检索历史工具结果"。

价值对比：现在关键词匹配是"字面子串"，FTS5 至少是真正的索引检索 + 排序 + 高亮。**投入小、见效快，而且它才是后面混合检索的一半。**

**B3. 向量检索放在有真实语料之后（解决 1 的长期版）**

只有在 B2 跑起来、且确实存在"语义相近但用词不同"的查询失败案例后再做。方案：

| 选择 | 取舍 |
|---|---|
| `sqlite-vec` | 单文件、和现有库同进程、零运维。**推荐** |
| LanceDB | 列存 + 向量，检索快，多一个依赖 |
| Chroma | 功能全，但会引入服务/进程模型 |
| pgvector | 需要 PostgreSQL，违背"本机单文件"定位 |

Embedding 来源是个真问题：**DeepSeek 没有 embedding 接口**。三条路——(a) 走一个 OpenAI 兼容的 embedding 服务（多一个外部依赖和密钥）；(b) 本地小模型（`sentence-transformers`，首次下载几百 MB，CPU 推理慢但离线）；(c) 先不上向量。我倾向 **(c) → 需要时选 (a)**，并且 embedding 必须做成可替换的 provider。

## C. Skill 层（缺失最明显，优先级高）

**Skill = 流程知识，Tool = 执行能力。** 现在只有后者。

设计：

```
data/skills/<name>/SKILL.md      ← 触发条件 + 步骤 + 坑 + 可选脚本
表 skills：name, scope, triggers, body, version, usage_count, success_rate
```

- **注入方式**：不是全量塞。系统提示里只放"有哪些技能 + 一句话描述"，模型判断相关时调 `skill_load(name)` 取正文。
- **沉淀方式**：任务成功后可以让模型提议"把这次的做法存成技能"，用户确认后落库。**这就是"能力积累"的入口。**
- **和站点 playbook 是同一件事**：站点操作技能就是 scope=site 的 skill。
- **和 MCP 的关系**：MCP 提供"能调什么"，skill 提供"该怎么用"。互补，不冲突。

我认为这是**投入产出比最高的一块**：不需要新的基础设施，却直接改变"每次从零开始"的性质。

## D. MCP 补齐（中优先）

1. 断线自动重连 + 心跳（现在一个服务挂了只有手动 reload）
2. 接 `resources`（把 MCP 资源当只读数据源，纳入检索）
3. 接 `prompts`（把 MCP 提示模板变成 skill 的一种来源）
4. 采样式回调（服务反向请求模型）——**安全上要非常小心，默认关闭**
5. 服务端模式：把 Orbit 自己的工具暴露成 MCP 服务，让别人（其他 Agent）来调

## E. 数据库整理（低优先，但早做省事）

一次性迁移（alembic 0003）：

- 新增：`tool_results`、`skills`、`grants`、`mcp_servers`（把三个 JSON 收进来）
- `messages` 加 `tool_call_id`、`kind`、`tokens`
- `memories` 加 `scope`、`source_message_id`、`active`
- `artifacts` 加 `checksum`、`bytes`、`expires_at`
- 统一"外部写入前先落一条 pending 记录"的模式（现在只有 `tool_calls` 有）

**配置统一原则**：真值只在 keyring，其余全在 DB。JSON 文件只留启动必需（模型地址、端口）。

---

# 建议的推进顺序

| 顺序 | 内容 | 为什么这个顺序 |
|---|---|---|
| 1 | **A1 引用式工具结果 + A3 预算** | 上下文爆炸是所有后续能力的天花板 |
| 2 | **C Skill 层** | 投入产出比最高，直接改变"经验不积累" |
| 3 | **A2 计划状态 + reflect** | 长任务可控性的关键，依赖 A1 腾出上下文空间 |
| 4 | **B1 记忆作用域 + B2 FTS5** | 检索真能用起来的前置 |
| 5 | **D MCP 补齐**（重连、resources） | 稳定性与数据源扩展 |
| 6 | **E 数据库整理** | 前面几项都会加表，一次迁移比四次省事 |
| 7 | **B3 向量** | 等有真实失败案例再上，避免为技术而技术 |

# 需要你拍板的分歧点

1. **先攻上下文还是先攻 Skill？** 我推荐先上下文（A1），因为它是天花板；但如果你更想立刻看到"越用越聪明"，可以先做 Skill。
2. **Skill 从哪来？** 全自动沉淀（任务成功后自动提议，你只点确认）还是先纯手工写？
3. **向量到底要不要？** 我的立场是"现在不要"，先用 FTS5。你的直觉呢？
4. **多 worker / 并行**：现在一次只跑一个任务。要不要放开并行（代价是浏览器是独占资源，得排队）？
5. **MCP 服务端模式**做不做？——让别人也能把 Orbit 当工具用。这是"从工具到平台"的一步。
