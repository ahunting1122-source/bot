# Orbit 总体架构设计

日期：2026-09-22。**本文是设计的唯一入口**，吸收并取代 `AGENT_ORCHESTRATION.md`、`HARNESS_DESIGN.md`（两份保留作细节附录）。现状对照见 `AGENT_DESIGN.md`，路线讨论见 `ROADMAP.md`。

## 0. 一句话

**模型只负责决定做什么；harness 负责其余一切。** 三条能力链路（工具 / MCP / RAG）加一条经验链路（Skill）挂在同一个骨架上，彼此不知道对方存在。

---

## 1. 全图

```
┌─ 前端工作台 ────────────────────────────────────────────────┐
│  工作台（聊天/过程/确认） · 对话摘要 · 密钥存储 · 连接与设置     │
└───────────────────────────┬────────────────────────────────┘
                     REST / SSE
┌───────────────────────────▼────────────────────────────────┐
│ API 层（FastAPI）  本地会话 Cookie · loopback · 幂等任务提交   │
└───────────────────────────┬────────────────────────────────┘
┌───────────────────────────▼────────────────────────────────┐
│ 任务执行器   单 worker 队列 · 一次一任务 · 取消 · 重启恢复       │
└───────────────────────────┬────────────────────────────────┘
┌───────────────────────────▼────────────────────────────────┐
│ LangGraph StateGraph（每任务一条 thread）                     │
│  装载 → 思考 → 确认 → 执行 → 治理 → 预算 → 收尾                │
│  checkpoint 每步落盘 · interrupt 挂起 · SSE 流式回放           │
└───────────────────────────┬────────────────────────────────┘
       统一 Tool 契约（名称 / 参数 / 确认策略 / 结果契约）
┌───────────────────────────▼────────────────────────────────┐
│ 能力层（模型侧看不出差别）                                    │
│  工具：内置        MCP 客户端      Skill 加载器   RAG 检索     │
│  browser/local    mcp__*         skill_*        retriever_*  │
└───────────────────────────┬────────────────────────────────┘
┌───────────────────────────▼────────────────────────────────┐
│ 执行层  Playwright 可见浏览器 · MCP 子进程/远程 · 文件系统       │
│        FTS5 索引 · 向量索引（sqlite-vec）                     │
└───────────────────────────┬────────────────────────────────┘
┌───────────────────────────▼────────────────────────────────┐
│ 存储层  app.sqlite · checkpoints.sqlite · chunks · tool_results│
│        skills · artifacts · keyring · vault.json · mcp.json  │
└─────────────────────────────────────────────────────────────┘
横切关注点：权限策略（v1 完全访问 + 全量审计） · 幂等与 operation_id · 三层预算 · 可观测
```

---

## 2. 与 LangChain / LangGraph 的映射

沿用它已有的抽象，只写缺的部分——不自己发明状态机、消息模型和检索接口。

| 我们的概念 | LangChain / LangGraph | 现状 |
|---|---|---|
| 状态与归约 | `StateGraph` + `Annotated[list, add_messages]` | ✅ 已用 |
| 节点与条件边 | `add_node` / `add_conditional_edges` | ✅ 已用 |
| 挂起与恢复 | `interrupt()` + `Command(resume=)` + `AsyncSqliteSaver` | ✅ 已用 |
| 工具契约 | `BaseTool` / `StructuredTool`（内置用 Pydantic，外部用 JSON Schema） | ✅ 已用（自建 `Tool`，形状一致） |
| 工具结果 | `ToolMessage` | ✅ 已用 |
| 流式 | `astream` | ✅ 已用（外加自建 SSE 事件层） |
| 长期记忆 / 技能 | LangGraph `BaseStore` | ⬜ 可选用，先落 SQLite 表 |
| 文档与出处 | `Document(page_content, metadata)` | ⬜ metadata 就是 origin |
| 分块 | `RecursiveCharacterTextSplitter` 一类 | ✅ 已用（自建 `indexing.chunk_text`，结构切分→300–500 合并） |
| 检索器 | `BaseRetriever` 接口 | ✅ 已用（自建 `FtsRetriever`，形状一致） |
| 向量库 | `VectorStore` 接口 → `sqlite-vec` 薄封装 | ✅ 已用（`app/vectors.py::SQLiteVecStore`，vec0 虚表） |
| Embedding | `Embeddings` 接口（可替换实现） | ✅ 已用（`FastEmbedEmbeddings`，bge-small-zh-v1.5） |

**不引入**：LangGraph 的预置 ReAct agent（我们的 gate/digest/budget 是自定义的）、LangServe、多 Agent supervisor。

---

## 3. Loop：节点、边、终止

```
             ┌────────── plan（预留节点）──────────┐
             │                                    │
START → 装载 → 思考 ⇄ 确认 → 执行 → 治理 → 预算 ──继续──→ 自省 → 思考
              │                       │      │
              │                       │      └─超限─→ 收尾
              │                       └─原文入库 / 索引写入
              └─无工具调用─→ 收尾
```

| 节点 | 职责 | 关键点 |
|---|---|---|
| 装载 `prepare` | 装配上下文 | 记忆 + 摘要 + 技能简述 + **RAG 按任务预取**；初始化预算 |
| 思考 `think` | 决定下一步 | 绑定工具、流式输出、**写下当前计划判断**；模型调用有限重试（流式已出正文则不重试） |
| 确认 `gate` | 人工介入判定 | **v1：只处理 `ask_user` 与工具返回的 `needs_user`**（真登录墙）；逐项确认分支已移除 |
| 执行 `execute` | 唯一分发点 | 校验 → 幂等 → 分发 → 结果归一 |
| 治理 `digest` | 结果治理 + 索引写入 | 原文入库、引用登记、大小估算、**切块写索引** |
| 自省 `reflect` | 周期性纠偏 | **已接进图**（预算之后、思考之前）；产出的是轨迹推导的确定性提示，不调模型；每任务最多 2 次 |
| 预算 `budget` | 三类预算判定 | 动作 / token / **时间都硬拦**（半量预警归 `reflect`，满量拦截归这里）；超限走收尾 |
| 收尾 `finalize` | 带进度收尾 | 已完成 / 未完成清单 + 下一步建议 + 对外动作清单 + 引用回填校验 + **成本聚合 `final.cost`** |
| `plan` | 结构化计划 | **只留接口与 state 字段，不进图**（计划由 harness 从工具轨迹派生，零模型调用） |

**终止条件（全部要能优雅收尾）**：模型不再调用工具 / 动作数上限 / token 预算 / 时间预算 / 用户取消 / 连续同类失败。
**恢复**：checkpoint 每步落盘；`operation_id` 保证重放不重复副作用；`uncertain` 结果重启后**先回读核对**。

---

## 4. 工具层

- **统一契约**：`name` / `description` / 参数（内置 Pydantic、外部 JSON Schema）/ `confirmation` / `source`。
- **唯一分发点**：`ToolRegistry.execute()` —— 校验 → 幂等 → 落 pending → 事件 → 分发 → 归一 → 入库 → 事件。
- **四类结果分开处理**：

| status | harness 行为 |
|---|---|
| `ok` | 可用，计入预算 |
| `error` | 回灌模型自行决定；**重启不自动重试** |
| `needs_user` | 挂起等用户；处理后**重新执行**该工具 |
| `uncertain` | **禁止重发**，先回读核对（副作用类操作必须实现） |

- **确认策略（v1 已改）**：**完全访问，事前零确认**。工具直接执行；人工介入只剩 `ask_user`（模型主动求助）与工具返回的 `needs_user`（真登录墙）。
- **信任模型转为事后审计**：每次外部动作落 `tool_results` 并在 `finalize` 输出对外动作清单；三层预算是唯一的自动刹车。
- **站点级免确认机制代码保留**，v1 策略 = 全放（退化为审计分组标签）。`takeover_allowed` 门槛保留——它逼模型先自己登录，本身就是减少人工。

---

## 5. MCP

| 项 | 设计 |
|---|---|
| 传输 | stdio（本地进程）+ streamable HTTP（远程，headers 走 httpx2） |
| 命名 | `mcp__<服务>__<工具>`，ASCII，不与内置冲突 |
| 默认确认 | 是；只有服务配置显式 `auto_approve: true` 才免 |
| 密钥 | 配置写 `keyring:名称`，值存系统凭据管理器 |
| 隔离 | 每服务独立 exit stack + 连接超时；坏服务只标记自己 |
| **重连** | 待补：心跳 + 指数退避重连 |
| **resources** | **接进 RAG，但隔离**：异步抓取入索引队列，失败只记日志，不进主链路 |
| prompts | 作为 Skill 的一种来源（外部技能） |
| 采样 / elicitation | 默认关闭，安全上单独设计 |
| 服务端模式 | 远期：把 `browser_*` 等暴露为 MCP 工具，让别人调 Orbit |

---

## 6. Skill（经验链路）

**状态：二期治理四道闸已落地（2026-09-30 终版）。** 已实现全链路：`app/skills.py` 加载器 + `sync_to_store` 镜像、`skill_load` / `skill_list` / `skill_save`、`prepare` 注入择优、四道闸治理（写入查重 / 矛盾转备选 / 注入择优 / 降级淘汰）、`skills` 表（迁移 `0006` + `0007`，治理字段 `consecutive_failures` / `last_used_at`）。剩余仅「与 MCP 的关系」一项未做（依赖外部 prompts 源，非本地闭环）。方案终版：`handoff/skill-layer-phase2-plan.md`。

**Tool 回答"能做什么"，Skill 回答"该怎么做"。** 站点技能让同类型任务第二次做时更快、更少踩坑。

| 项 | 设计 | 状态 |
|---|---|---|
| 形态 | `data/skills/<name>/SKILL.md`：触发条件 + 步骤 + 坑 + 可选脚本 | ✅ 已做（文件是真值，改完即生效） |
| 加载器 | `app/skills.py` 解析 frontmatter + 正文；每次读磁盘不缓存 | ✅ 已做 |
| 读取工具 | `skill_load(name)`（只读、免确认）、`skill_list()`（列技能） | ✅ 已做 |
| 注入 | `prepare` 按触发词字面匹配召回 + **注入择优**（过滤 unreviewed/archived → scope→success_rate→usage_count 排序 → 同簇只出冠军），上限 5 | ✅ 已做 |
| 表 | `skills(...)` + 治理字段 `consecutive_failures` / `last_used_at` | ✅ 已做（迁移 `0006`+`0007`；文件是真值、表是索引，`sync_to_store` 单向镜像） |
| 沉淀 | `skill_save`：同名更新（version+1 合并差异）、触发词重叠互标备选、无冲突新建 | ✅ 已做 |
| 治理 | 写入查重 / 矛盾转备选 / 注入择优 / 降级淘汰（连续失败标 stale、超期/低成功率归档） | ✅ 已做 |
| 与 MCP 的关系 | MCP prompts 是外部来源，本地 SKILL.md 是内部来源，同一个加载器 | ⬜ 待做（依赖外部 prompts 源） |

### 6.1 技能（3 个已落盘并被加载）

`web_search`、`bili_video_detail`、`form_fill_login`。其中 `web_search` 内容沉淀自前几天的真实实测（SPA 渲染、分页截断、点击后旧页面三个坑），`bili_video_detail` 沉淀了「点击开新标签页」这个坑（对应 `browser.py` 的 grace 宽限期修复）。技能原文见各自 `SKILL.md`，这里不复制（文件是真值，避免两处漂移）。

**验证标准**：用「在某个新站点搜索某关键词」类任务对比——加载技能后，步骤数应少于 16.7 首次摸索时的次数。这正是 Skill 价值的第一份量化证据（一期已具备观测条件：`skills_injected` 事件 + 工具步数可直接对照）。

### 6.3 沉淀流程：把某次协作的操作流程存成技能

关键前提：**素材不需要额外记录**。任务运行时每一步都已落在 `tool_results`（工具名、参数、URL、状态、结果原文）和 `task_events`（事件序列）里——沉淀就是把这些结构化轨迹交给模型提炼，而不是重新回忆。

```
任务运行中（每步自动落 tool_results）
        │
        ▼
触发 · 用户说「把刚才的流程存下来」/ 模型任务成功后主动提议
        │
        ▼
提取 · skill_save(name, scope, body)
        模型读本次工具轨迹，提炼四样东西：
        ① 触发条件（什么情况下该用这个流程）
        ② 步骤（含具体站点、URL 模式、元素定位经验）
        ③ 坑（哪一步失败过、怎么绕过）
        ④ 参数化（关键词/数量是变量，流程是常量）
        │
        ▼
落库 · 双层存储
        data/skills/<name>/SKILL.md   ← 正文真值，人可直接编辑
        skills 表                     ← 元数据 + usage_count / success_rate
        │
        ▼
复用 · 下次相似任务
        prepare 按触发条件列出技能简述 → 模型 skill_load 取正文 → 按步骤执行
        │
        ▼
反馈 · usage_count / success_rate 累计
        低使用率或低成功率的技能提示更新/淘汰
```

**沉淀时模型要做的判断**（这是质量关键，不是简单复制轨迹）：

| 提炼项 | 从轨迹里抽什么 | 例 |
|---|---|---|
| 触发条件 | goal 的模式 | "用户要在站内找某类条目" |
| 常量步骤 | 反复出现的成功动作序列 | 构造搜索 URL → 确认进入 → 点排序按钮 |
| 变量参数 | 每次不同的输入 | 关键词、要第几页、抽哪些字段 |
| 坑与绕法 | error / needs_user / 重试发生过的地方 | SPA 要等渲染、分页在底部 |

**和 `memories` 的分工**（两层不要混）：

| | 存什么 | 例 |
|---|---|---|
| `memories` | **事实与偏好**（键值） | "报告用中文 Markdown"、"常用站点是 B 站" |
| `skills` | **流程与做法**（步骤） | "站内搜索应该这么搜" |

用户说"存成记忆"时，模型应判断：是事实偏好 → `memory_save`；是操作流程 → `skill_save`。两者都相关就都写。

**治理**（与 v1 全放权限一致的哲学）：默认直接落库（不卡确认），但标记 `unreviewed`；靠 usage/success 统计淘汰低质量技能，用户随时可在界面编辑或删除 SKILL.md 文件（文件是真值，改完即生效）。

### 6.4 技能存多了会冲突吗——会，五种形态四道闸

**冲突的五种形态**

| 形态 | 例子 | 伤害 |
|---|---|---|
| 触发重叠 | "B 站搜索"与"视频站搜索"同时命中一个任务 | 模型不知道用哪个 |
| 内容矛盾 | 一个技能教 `order=click`，另一个教点页码按钮 | 行为不一致，结果不可复现 |
| 作用域冲突 | global 说"搜索走 API"，site 说"B 站走浏览器" | 优先级不明 |
| 过时失效 | 站点改版，技能还在教旧参数、旧元素 | 误导 + 浪费步骤 |
| 数量膨胀 | 几十个技能的简述把上下文重新撑爆 | 等于白做上下文压缩 |

**四道闸（对应治理）**

1. **写入查重（沉淀时）**：新技能落库前先跑一次"技能检索"——同 scope 下触发条件重叠（同 host 或触发词重合）即**不新建，改为更新现有技能**（version+1，合并差异）。沉淀变更新，重复从源头不产生。
2. **参数矛盾重新定义为"备选路径"**：同 host 同动作的不同做法（比如两种搜索 URL 模板）不合并，而是**都保留、标注互为备选**——运行时一个失败自动换另一个。矛盾不总是坏事，有时是备胎。
3. **注入择优（每次任务）**：技能简述注入上限 5 个；排序规则 = scope（site > project > global）→ success_rate → usage_count；**同簇只注入分最高的一个**，其余作为失败后的备选。site 覆盖 global，像层叠样式。
4. **运行时降级与淘汰**：按技能步骤执行失败 → `failure++` → 连续 2 次失败标记 `stale` 并降权（回退到无技能摸索）；90 天未用或 success_rate < 50% → 归档（移出注入池，不删除）。

**这些机制属于阶段 7 的验收范围，但设计现在锁定**——否则等存了 50 个技能再补治理，迁移成本更高。

### 6.2 搜索引擎 API 怎么适配（两条接入路）

`web_search` 这个技能本身**不需要任何 key**——它走浏览器路径，用的是站点自己的搜索框。要接"带 API key 的搜索引擎"，有两条路：

**路线 A：走 MCP（推荐先试，零代码）**
社区已有现成的搜索类 MCP 服务（Tavily、Brave Search、SearXNG 等）。在 `data/mcp.json` 加一条、keyring 存 key（`"keyring:tavily"` 语法）、保存重连——工具自动注册为 `mcp__tavily__search`，模型立刻可用。**不需要写任何代码**，这正是刚建好的 MCP 通道存在的意义。

**路线 B：内置工具 `web_search_api`（可控性最强）**
自己写一个工具包装搜索 API（Tavily / 博查 / SearXNG），返回结构化的 `[{title, url, snippet}]`。要不要写，取决于路线 A 找不到合适的现成服务。

**Skill 的适配改动很小**——步骤 1 加一个条件分支：

> 若 MCP 或配置里有可用的搜索 API → 直接调用，拿结构化结果（更快、更干净）；
> 否则走浏览器路径（本技能其余步骤）。

**两条路径是互补，不是替代**：

| 场景 | 用哪条 |
|---|---|
| 公开网页信息、要快、要干净 | **API** |
| 需要登录态才能看的（你的 B 站账号、内部系统） | **浏览器**——唯一能用的，API 看不到 |
| 需要精确操作（点击/翻页/筛选取证） | **浏览器** |

API 结果同样入 RAG 索引（`origin` = 结果 URL），所以两条路径最终汇进同一个知识层。

**密钥存放**：keyring（`search:<provider>`），和站点账号、MCP 密钥同一机制，绝不进模型上下文。

---

## 7. RAG

**先服务网页内容与工具结果**（已定），本地文档继续用现成的文件工具。

### 7.1 写入侧

```
browser.py 结构化正文（structured）／工具结果原文
                        ─→ 结构切块（标题、段落、表格按行）
                        ─→ 归一化去重（hash ＋ 相邻相似度 >0.9 合并）
                        ─→ 估算 tokens（目标 300–500 中文字，带 overlap）
                        ─→ 写 FTS5(trigram) ＋ FTS5(chars) ＋ 向量（占位）
                        ─→ chunks 表
```

`chunks(row_id INTEGER PK AUTOINCREMENT, id, source_type, source_id, conversation_id, task_id, host, ordinal, text, char_text, tokens, hash, origin, title_path, created_at)`

**`origin` 是硬要求**：只认 URL（`web_page`）与文件路径（`local_file`）。没有真实出处的工具结果不进索引——编出来的 origin 无法被引用，写进去只会让 finalize 的引用核对失败。

**三个容易踩的点**

- **标题本身就是内容**：`split_blocks` 若把 `###` 只当 title_path 用掉，**标题之间没有正文**的页面会整页落不下来——SPA 结果列表页每个条目就是一个 `### 标题`（B 站搜索页实测 2238 字只索引成 34 字导航残渣，检索必然 0 召回）。所以没等到正文的标题必须自己成为一块。回归测试见 `tests/test_indexing.py::test_heading_only_pages_keep_their_headings`。
- **`row_id` 用自增整数而非 TEXT 主键**：TEXT 主键在 REPLACE 语义下 rowid 会变，会让 FTS 行变孤儿。
- **`conversation_id` / `task_id` 必须建在表里**：读取侧「默认按会话收窄」靠这两列；表建好再补列要写迁移并回填（`source_id` 反查会话要 join 两跳）。

### 7.1.1 为什么是两张 FTS 表，不是一个索引

FTS5 的 tokenizer 是**表级**选项，没法在同一张表上同时用 trigram 和 unicode61，所以建两张：

| 表 | 索引列 | tokenizer | 作用 |
|---|---|---|---|
| `chunks_fts_trigram` | `text` | `trigram` | 主路径，但**只匹配 ≥3 字符**——中文最常见的 2 字词（键盘／显卡／B站）全部 0 召回 |
| `chunks_fts_chars` | `char_text` | `unicode61` | 补齐短查询：CJK 逐字空格切分（`机械键盘` → `机 械 键 盘`） |

两张表都挂 `AI`/`AD`/`AU` 三个同步触发器。**external content 表的行标识列恒为 `rowid`**，`content_rowid='row_id'` 只是把它映射到 `chunks.row_id`；触发器里写成 `row_id` 会直接报 `no column named row_id`。漏触发器则索引与主表**静默失同步**（查询慢慢变错却不报任何错）。

**查询侧必须按窗口拆词**：整句当短语查询必然 0 召回（预取用的就是整句任务目标）。因此 trigram 路按 3 字滑窗、字符级路按相邻两词滑窗，各候选短语用 `OR` 连接；短查询（≤12 字）额外保留整串以维持精确短语语义。

### 7.2.1 切块规则：结构优先、定长兜底（不是纯定长，也不是让模型判断语义）

**为什么不是纯定长**：网页正文是半结构化的（标题 / 段落 / 列表 / 表格），定长切会把表格行、句子拦腰截断——检索命中的片段残缺，模型据此只能给出残缺回答。

**为什么不是"用模型判断语义边界"**：每切一刀跑一次 LLM，成本与延迟不可接受，而且边界判断本身不稳定。我们要的是"足够好"，不是"完美"。

**规则（三层）**

1. **结构切**：按块级元素切——markdown 标题、HTML `p`/`li`/`tr`、空行分段。结构边界就是天然的语义边界。
2. **定长合并/再切**：相邻小块合并到目标 **300–500 中文字**；超过 **512 token**（embedding 窗口上限）时退回句子边界再切。
3. **overlap 50–100 字**：跨边界句子在相邻两块各出现一次，防止答案正好落在边界上。

**三条特殊规则**

- **表格**：**一条数据行 + 表头独立成一块**（表头必带，否则行数据没有列名，检索命中也无意义）；只有表头、没有数据行的表整表一块。原「整表 ≤500 字一块 / 大表按 400 字行组切」**已废除**（2026-10-09）——实测目标单元格信号被同表其他行稀释到 3–8%，是 A 集门 2 的缺池根因。四档粒度实测（`scripts/diag_table_split.py`）：整表 A 0.625 / 行组400 0.625 / 行组250 0.562 / **行级 0.688**，B 集行级最高 1.000——行级双优，故定稿。
- **块首带路径**：把所属标题路径（`H1 > H2`）拼进块首。检索命中的是片段，带上路径片段才自明。
- **去重**：hash 相同不入库；相邻块相似度 > 0.9 合并。

**为什么目标定 300–500 中文字**：太小（<100）缺上下文，命中了也读不出含义；太大（>512 token）超出 bge 窗口被截断，尾部语义丢失；注入预算也划算——top-K≤5 × 500 字 ≈ 2500 字。

**补注（2026-09-30，实测）**：`scripts/eval_chunk_sweep.py` 用仓库 6 篇真实中文文档（184 块）跑过 300-500 / 600-800 / 1000-1200 / 零重叠四档，关键词路（当前唯一在跑的路）**大块反而略好**：融合 recall@5 0.667 → 0.667 → 0.750，MRR 0.301 → 0.333 → 0.361；零重叠比现状低（MRR 0.308 vs 0.301 基本持平，说明 overlap 在当前语料上不构成增益）。这不构成"改成大块"的理由：① 12 条查询样本太小，标记是长片段，天然偏爱大块；② 真正的约束是**向量路的 bge 512 token 窗口**——1000–1200 中文字已明显越窗，向量上线后必然吃亏。**结论：维持 300–500，等向量路上线（阶段 6）后用同一脚本复测再定**；参数都在 `indexing.py` 顶部，改一行即可复现。

### 7.2.2 一个 chunk 到底长什么样（真实示例）

以 09-22 那次 B 站任务的真实观察结果为例，切块后落库的是这样的行：

```json
{
  "id": "ch_01H8XK2P…",
  "source_type": "tool_result",
  "source_id": "op_12b1ece9…e5",
  "origin": "https://search.bilibili.com/all?keyword=机械键盘&order=click",
  "host": "search.bilibili.com",
  "title_path": "搜索结果 > 最多播放 > 第 1 页",
  "ordinal": 2,
  "text": "…向妈妈炫耀自己的新键盘（816.6万 / 00:22 / 魔性小柴）…《未成年键盘的声音》（682万 / 02:55 / leileiguai）…",
  "tokens": 380,
  "hash": "b3a1f…",
  "created_at": "2026-09-22T02:20:31+08:00"
}
```

**存**：网页观察正文（去导航后切块）、超长工具结果原文、本地文件文本、技能正文、（后期）会话消息。
**不存**：截图（只在 `artifacts` 里存路径）、keyring 里的任何值、原始 HTML 标签（存渲染后正文）、被拒绝或未执行的操作。

**数量级**：一次网页观察 14000 字 ≈ 28 块；一天 10 个任务 ≈ 600 块，一年约 20 万块——FTS5 和 sqlite-vec 对这个量级毫无压力，本地单文件完全够。

### 7.2 读取侧

```
查询构造（窗口拆词）→ 查询路由（lexical / short / semantic，决定哪几路参与、要不要过词面覆盖门）
  → 三路候选：FTS5 trigram ‖ FTS5 字符级 ‖ 向量（sqlite-vec vec0 KNN）
  → RRF 融合（k=60；只用排名，避免两路分数不可比）
  → cross-encoder 重排（第三档，`rerank_enabled` 默认**开**；池 = top-20）
  → top-K（K ≤ 5，单次注入 ≤2500 字）
  → 注入 prepare：片段 + origin
  → 记 context_meta.retrieved ＋ 落 retrieval_injected 事件（便于回放与评估）
```

**检索范围按任务**（已定）：默认在当前任务上下文（会话、站点、来源类型）内检索，需要全局时显式指定——避免检索噪声把上下文重新撑爆。

**预取保守 gating**：命中分数不过阈值（`1/(k+5)`，即至少进过某一路的 top5）就不注入。这样 `1+1` 这类无检索任务零注入，不会被误标「未引用来源」。

**查询路由 + 词面覆盖门（2026-10-08 第三轮）**：`route_query()` 把查询分三类——**lexical**（含引号 / ASCII 标识符 / 路径扩展名）→ 三路全开 + 要求候选覆盖查询的 ≥50% 词窗；**short**（≤2 字中文）→ 关掉 trigram（trigram 只匹配 ≥3 字符，2 字必零召回）、只留字符级 + 向量；**semantic**（其余）→ 三路全开、不设覆盖门。覆盖门只对词面路生效，因为自然语言的正确块**经常只覆盖查询的一部分词**，对语义路加门会误杀。这是对「两路 FTS 用 OR 词组几乎总能凑满噪声候选」的直接对症。

**第三档重排（2026-10-08，默认开）**：`app/rerank.py` 的 `FastEmbedReranker`（cross-encoder，默认 `BAAI/bge-reranker-base`）在融合后对候选池（默认 top-20）逐对打分重排。**动机有实测背书**：换 gemma 后 B 集向量单路 0.917 被三路融合压回 0.750，而**调权重是 wash**（见下），所以正解是「RRF 只召回、排序交重排」。`rerank_hits()` 是模块级纯函数（评测离线复用）；`NullReranker` 在模型缺失时空转、如实降级。**效果**：B 集 0.750 → **0.917（过门）**，A 集 0.562 → 0.625。代价：模型 1.04 GB、常驻 1.42 GB、~20 ms/对（`pool=20` 即 +0.4 s/查询）。**`rerank_enabled` 默认 `True`（2026-10-08 收口）**：证据是 B 过门，回退只需 `ORBIT_RERANK_ENABLED=0`。详见 `handoff/vector-path-plan.md` 第三轮。

**融合与排序的实测结论（2026-10-08 汇总）**：
1. **B 集的门槛曾被 RRF 挡死**——向量单路 0.917（高于 0.85）被三路等权融合压回 0.750。重排上线后恢复到 0.917。
2. **给向量路加权已被实测证伪**（(1,1,1)/(1,1,2)/(1,1,3) 三档差异不超过 1 条查询，只是把损失在 A/B 之间搬家），`rrf()` 保留 `weights` 参数但默认仍等权。
3. **A 集门槛（≥0.70）的阻塞点是「表格块召回」，不是排序**——A 集 16 条里 3 条目标块压根不在候选池；追到根因是**表格被整表切块、目标单元格信号被稀释**（目标短语仅占块 3–8%，词面两路全空、向量路排 22/58/12 位）。量化：A 集目标块 62.5% 是表格块（语料里只占 32%），表格块命中率 50% vs 正文 83%。对症手段是**表格行级切块**（属切块策略变更，需单独一轮，见 §10）。**→ 已于 2026-10-09 实施**（`split_table` 改行级）：A 集 0.625 → **0.688**、B 集 0.917 → **1.000**；但门 2（≥0.70）仍差 1 条。行级切块抬的是**可达池内的排序命中**，没抬**候选池天花板**（四档粒度实测天花板恒为 0.812）——余下 3 条缺池是**嵌入模型对极短功能性短语的判别力上限**，不是切块粒度问题（详见 `handoff/vector-path-plan.md` 第四轮）。
4. 候选池天花板（`recall@rerank_pool`）：A **0.812** / B **1.000**；深扫 `LIST_DEPTH` 10/20/30 对 A 重排无影响——**A 的残差是召回上限，不是池深**。

**引用回填是硬约束**（已定）：检索类任务的回答必须标注用了哪几个 chunk（`origin` 可见），否则不算完成。这既是质量门槛，也是评估检索效果的唯一依据。两条守卫防止误伤：

1. **只在本次注入过片段时才校验**（`context_meta.retrieved` 非空），否则无检索任务会被误标「部分完成」。
2. **origin 用归一化匹配**：去 scheme / `www.` / query，比对 host+path 前缀——模型常把 `search.bilibili.com/all?…` 引用成 `bilibili.com`，严格全等会把正常引用判成未引用。

**评估口径**：服务端落 `retrieval_injected`（注入了哪些 origins ＋ 分数）与 `citation_checked`（matched / missing）两个事件，`eval_run.py` 据此在报告里出 **recall@5** 与**引用覆盖率**两列。按计划先出数、不定 KPI（有真实数据再设阈值）。

### 7.3 向量选型（已实现：sqlite-vec vec0 + embeddinggemma-300m）

| 项 | 选择 | 理由 |
|---|---|---|
| 向量存储 | `sqlite-vec`（`chunk_vectors`，vec0，当前 768 维，迁移 `0005`） | 单文件、与现有 SQLite 同进程、零运维；用 `VectorStore` 形状封装，以后可换 |
| 备选 | LanceDB / Chroma | 功能更全，但多一个依赖或进程 |
| Embedding | **本地模型（已实现）**：`fastembed` + `google/embeddinggemma-300m` | 中文/多语言、ONNX 推理**不依赖 torch**、内容不出本机；实现走 `Embeddings` 接口，可替换 |
| 重排 | **本地 cross-encoder（已实现，默认开）**：`fastembed` + `BAAI/bge-reranker-base` | 融合后排序的第三档；`Reranker` 是第 12 个 policy，可替换 |

**实现要点**

1. 模型 1.24 GB，首次运行下载到 `<data_dir>/fastembed_cache`（`Settings.embedding_cache_dir`，并把 `FASTEMBED_CACHE_PATH` 一并导出），之后离线。**不要用 fastembed 的默认缓存**——那是 `%TEMP%/fastembed_cache`，落系统盘且可能被清理。
2. 输入窗口 2048 token，现有 300–500 字切块远在窗口内，不构成约束（`bge` 系列才是 512 的紧约束）。
3. 嵌入与切块放在**同一个 `to_thread`** 里（`graph/nodes.py::digest.index`）。**实测 173 ms/块、一次网页观察（28 块）4.84 s**——比旧模型 bge-small-zh（31 ms / 0.86 s）慢 5.6 倍，已超过方案 D4 定的「digest > 1.5 s 就上后台队列」触发线，队列化列为后续项。
4. **两侧前缀都要配**（这是最容易配错的一处，三种模型的组合都不一样）：gemma 要 `task: search result | query: ` / `title: none | text: `；bge v1.5 两侧都空（v1.0 才需要前缀，加上反而掉分）；Qwen3-Embedding 只在查询侧加 `Instruct: {任务}\nQuery:`。`Settings.vector_query_prefix` / `vector_doc_prefix` 分开承载。
5. **扩展加载必须自持连接**：aiosqlite 把真实 `sqlite3.Connection` 放在独立 worker 线程，无法用 SQLAlchemy 的 `connect` 事件加载 sqlite-vec（同线程守卫报错）。`SQLiteVecStore` 每次公开方法开一条短 aiosqlite 连接，`enable_load_extension` → `load_extension` → 立刻关掉。迁移侧是同步引擎，直接用 `op.get_bind().connection.dbapi_connection`，加载失败硬失败（不做静默降级）。
6. **onnxruntime 的 CPU memory arena 必须关**（`Settings.embedding_cpu_mem_arena` 默认 `False`）：实测开着时 1.24 GB 的模型把进程工作集顶到 5.29 GB，整机只剩 0.42 GB 可用而卡死；关掉后加载 2.4 s、加载后 891 MB。**重排模型也吃这条**（`build_reranker` 把 `embedding_cpu_mem_arena` 一并传进 `TextCrossEncoder`）。
7. **向量表的维度跟随配置，不跟随迁移**：vec0 的维度建表时钉死在 `float[N]`，而 `embedding_dim` 是可配置的，所以校准放在运行时——`SQLiteVecStore.ensure_schema()` 比对 `vector_meta` 与当前配置，维度或模型不一致就 DROP 重建并留可读 warning，`AgentRunner.start()` 会调一次。换模型后必须重跑 `scripts/backfill_vectors.py`。
8. **本环境的 bulk-delete 守卫会打断多文件 HF 下载**（`bge-reranker-base` 两次 exit 1）。单文件大 blob 能下完，所以遇到时**手工收尾 HF 缓存**（`.incomplete` blob 改 commit-hash 名 + 建 snapshot + 硬链接 + size 断言），不必重下。

**选型实测（195 块真实仓库语料 + A/B 双集 recall@5，详见 `handoff/vector-path-plan.md` 第二轮）**

| 模型 | 维度 | 体积 | 每段 | A 向量 | B 向量 |
|---|---|---|---|---|---|
| `BAAI/bge-small-zh-v1.5` | 512 | 0.09 GB | 31 ms | 0.438 | 0.583 |
| `jinaai/jina-embeddings-v2-base-zh` | 768 | 0.64 GB | 164 ms | 0.562 | 0.500 |
| **`google/embeddinggemma-300m`（现默认）** | 768 | 1.24 GB | 173 ms | 0.562 | **0.917** |

（`bge-base-zh-v1.5` / `bge-large-zh-v1.5` / `bge-m3` **不在 fastembed 支持列表里**，用不了。`Qwen3-Embedding-0.6B` 质量更高但是 0.6B 解码器，纯 CPU 代价过大，未采纳。）

**管道端到端（含重排，2026-10-08 第三轮）**

| 查询集 | 三路融合 | 路由门控 | **重排** | 门槛 |
|---|---|---|---|---|
| A · 改写（16 条） | 0.562 | 0.562 | **0.625** | 0.70 ❌ |
| B · 词面（12 条） | 0.750 | 0.750 | **0.917** | 0.85 ✅ |

**切块侧（2026-10-09 已实施）**：A 集的差距定性为**表格块召回**——表格被整表切块（`0004` 切块规则 §7.2.1 的「整表 ≤500 字作为一块」），目标单元格信号被同表其他行稀释。已落地**表格行级切块**（一条表行 = 一个语义单元，仍重复表头，`split_table`）：A 集 0.625 → 0.688、B 集 0.917 → 1.000，`pytest` 248 passed。**未完成**：① 门 2（≥0.70）仍差 1 条，残差 = 候选池天花板 0.812（三路召回上限，与切块粒度、`LIST_DEPTH`、FTS 门控、路径权重全部无关，均已实测否掉）；② 扩大查询集（≥50 条）重估难度分布；③ 生产库 524 块仍是旧切块，需重切才吃得上本次改动（新入库内容自动生效）。

---

## 8. 存储

| 存储 | 内容 |
|---|---|
| `app.sqlite` | conversations / messages / tasks / task_events / memories / tool_calls / artifacts **+ 新增**：chunks / tool_results / skills / grants / mcp_servers |
| `checkpoints.sqlite` | LangGraph 图状态 |
| 索引 | FTS5 双虚表（`chunks_fts_trigram` / `chunks_fts_chars`）＋ sqlite-vec 虚表 `chunk_vectors`（迁移 `0005`，维度跟随 `Settings.embedding_dim`，当前 768）＋ 元信息表 `vector_meta` |
| keyring | 唯一放真值的地方 |
| `vault.json` / `mcp.json` / `settings.json` | 逐步收回 DB，只留启动必需 |

**表设计要点**：`messages` 补 `tool_call_id` / `kind` / `tokens`；`memories` 补 `scope` / `source_message_id` / `active`；`artifacts` 补 `checksum` / `bytes`。一次迁移（0003），避免四次。

---

## 9. 已定事项（本轮讨论）

| # | 决定 |
|---|---|
| 1 | RAG 先服务**网页内容 + 工具结果**，不做本地文档库 |
| 2 | **引用回填做成硬约束**：无来源不算完成检索任务 |
| 3 | 检索范围**默认按任务收窄**，全局需显式指定 |
| 4 | MCP `resources` **接入 RAG 但隔离**（异步、失败只记日志） |
| 5 | **向量要上**：`sqlite-vec` + 可替换的 Embedding 接口 |
| 6 | 编排拆成 `app/graph/` 包；先只搬家不改逻辑（前次决定） |
| 7 | 现在建 `tool_results` 表；digest 落库要幂等（前次决定） |
| 8 | 收尾给"已完成/未完成 + 下一步建议"（前次决定） |
| 9 | **v1 权限模型 = 完全访问**：事前零确认；人工只剩 `ask_user` 与 `needs_user`；信任模型转为事后审计（对外动作清单）+ 三层预算刹车（用户决定） |
| 10 | **Embedding 走本地**：`fastembed` + `BAAI/bge-small-zh-v1.5`（ONNX，不依赖 torch），实现走 `Embeddings` 接口可替换 |
| 11 | **首个 Skill = `web_search`**（站内搜索流程，草稿见 6.1），内容沉淀自真实实测的三个坑 |

---

## 10. 实施顺序

| 阶段 | 内容 | 出口标准 |
|---|---|---|
| 1 | 拆包搬家（prepare/finalize/budget/digest 骨架 + 9 个 Protocol） | 功能零变化；61 测试 + 10 用例评估集全过 |
| 2 | `tool_results` 表 + digest 落库 + 引用登记 | 能回答"哪一步最贵、结果多大" |
| 3 | `chunks` 表 + FTS5 索引 + 写入侧（挂 digest） | 网页内容与工具结果可被检索 |
| 4 | 读取侧 + `retriever_search` 工具 + 引用回填硬约束 | 检索类任务必须给出来源 |
| 5 | Skill 层（SKILL.md + 加载器 + 沉淀流程）；**首个技能 `web_search`（草稿见 6.1）** | 加载技能后，同类任务步骤数少于首次摸索 |
| 6 | 向量：本地 Embedding（fastembed + bge-small-zh-v1.5）+ sqlite-vec + 混合检索 | "用词不同语义相同"的查询能召回 |
| 7 | MCP 补齐：重连、心跳、resources 隔离接入 | 服务崩了能自愈 |
| 8 | 存储收敛（配置入 DB、表结构调整） | 配置只剩一份真值 |
| 6.5 | **检索质量专项**（~~表格行级切块~~ 已完成 + 查询集扩容 ≥50 条 待做） | A 集 ≥0.70（切块已落地 → 0.688，仍差 1 条；残差 = 候选池天花板 0.812） |

前 4 步做完，"网页内容可检索 + 来源可追溯"这条链就通了——这是 RAG 真正开始产生价值的地方。

**实施状态（2026-09-30）**：阶段 1–4 已落地，迁移为 `0003_tool_results` / `0004_chunks`（`0005` 后由阶段 6 占用，见下条）；阶段 5（Skill 层）已建 `0006_skills` + `0007_skill_governance`，二期四道闸治理已落地。阶段 1 的出口标准按**新基线**执行：后端 pytest **129 passed**、ruff / mypy / 前端 lint＋build 全绿、界面交互 **13/13** 通过；评估集 14 用例 + 离线检索评测 `eval_rag`（门槛 0.8，PASS）＋切块参数扫描 `eval_chunk_sweep`（结论见 §7.2.1 补注）。阶段 5 已完整落地（§6）。

**护栏收口（2026-09-30，目标模式执行）**：三类预算此前只有动作数真正拦截，token / 时间只记录不拦截；`Reflector` 有实现没接线。本轮补齐——预算三类都硬拦（半量预警归 reflect、满量归 budget）、`reflect` 节点接进图（budget 之后、think 之前，产出确定性纠偏提示）、`retriever_search` 每任务上限 4 次、工具执行加全局并发闸、`finalize` 输出成本聚合、模型调用有限重试（流式已出正文不重试）。过程与证据：`handoff/2026-09-30-goal-mode-report.md`。

**Skill 层读取侧（2026-09-30）**：阶段 5（Skill 层）的「读」这一半已落地——`app/skills.py` 加载器 + `skill_load` 工具 + `prepare` 按触发词注入简述（上限 5）。沉淀（`skill_save`）与四道闸留待二期。详见 §6。

**阶段 6 向量路（2026-10-08，已实现未达标）**：`0005_vector_table` 建 `chunk_vectors`（vec0）+ `vector_meta`；`app/vectors.py`（`SQLiteVecStore` / `FastEmbedEmbeddings` / 两个 `Null*` 降级 / `vec_available()` 探测）；`policies.py` 加第 10、11 个 Protocol（`Embeddings` / `VectorStore`）并在 `Policies.default` 注入；`retrieval.py::vector_candidates` 真实现进 RRF 第 3 路；`graph/nodes.py::digest.index` 把切块与嵌入合并进同一个 `to_thread`。**门禁**：pytest **158 passed**、ruff / mypy clean、`eval_chunk_sweep` / `eval_run` 仍可跑。**验收门槛：门 1（结构）过；门 2 / 门 3（数字）未过**——A 改写查询集融合 recall@5 **0.562**（门槛 0.70）、B 词面查询集 **0.750**（门槛 0.85）。

**第二轮换模型（2026-10-08 同日）**：默认嵌入模型 `BAAI/bge-small-zh-v1.5`(512) → **`google/embeddinggemma-300m`(768)**（选型实测见 §7.3）。融合数字 **A 0.375 → 0.562（+18.7）、B 0.667 → 0.750（+8.3）**，向量单路 B 达 **0.917**。同轮落地：两侧前缀分离（`vector_doc_prefix`）、模型缓存移出 `%TEMP%`（`embedding_cache_dir`）、关闭 onnxruntime CPU arena（`embedding_cpu_mem_arena`）、`ensure_schema()` 维度/模型感知 + 启动校准（`AgentRunner._calibrate_vectors`）。代价：嵌入慢 5.6×（31 ms → 173 ms/块），**已破方案 D4 的 1.5 s 触发线**，后台嵌入队列成为后续必要项。**门槛未过的原因已定位为融合公式而非模型**——详见 `handoff/vector-path-plan.md` 第二轮 · 发现 1 / 2。

**第三轮重排 + 路由（2026-10-08 同日）**：新增 `app/rerank.py`（`NullReranker` / `FastEmbedReranker`）作为融合后的**第三档**，`retrieval.py` 加 `route_query` / `gate_candidates` / `rerank_hits`，`policies.py` 加第 12 个 Protocol `Reranker`。**门 3 首次通过**：B 集 0.750 → **0.917**；A 集 0.562 → **0.625**（仍差 0.075）。**门禁**：pytest **177 passed**（+19）、ruff / mypy clean。代价：重排模型 1.04 GB、常驻 1.42 GB、+0.4 s/查询。**A 集残差已定性为「表格块召回」**（目标块 62.5% 是表格块、命中率 50% vs 正文 83%）→ 立 **阶段 6.5「检索质量专项」**。详见 `handoff/vector-path-plan.md` 第三轮。

**阶段 6 收口（2026-10-08 · 话题终结）**：`vector_enabled` 与 `rerank_enabled` **默认双双翻为 `True`**——依据是 Hermes 经验（向量按需 + cross-encoder 是唯一真见效的第三档）与本项目的 B 集证据（0.750 → 0.917 过门）；回退一句话 = `ORBIT_VECTOR_ENABLED=0` / `ORBIT_RERANK_ENABLED=0`，全关即纯 FTS5 双路。新增 `tests/conftest.py` 在测试期关掉重模型加载（**179 passed**、mypy 23 文件 clean）。**门 2（A ≥0.70）维持未过、不再追**——根因是表格切块粒度，移交阶段 6.5。**RAG 方向至此收口。**

**阶段 7–8 仍未开始**：MCP 补齐（重连 / 心跳 / resources 接入）、存储收敛（配置入 DB、`Artifact` 加 `checksum`、`Message` 加 `tool_call_id`，依赖迁移 `0008`——`0008` 未被向量路占用）。

---

## 11. 已全部定案（2026-09-23）

1. **Embedding 走本地**（用户已定）：`fastembed` + `BAAI/bge-small-zh-v1.5`，ONNX 推理、不依赖 torch、内容不出本机；实现走 `Embeddings` 接口，后续可替换。
2. **首个 Skill = `web_search`**（用户已定）：站内搜索流程，草稿见 6.1，内容沉淀自 16.7 实测的三个坑。

至此本设计稿的决策全部闭环，无待定项。**阶段 1–4 已实施完毕**（状态见 §10）；下一批从阶段 5（Skill 层）开始。
