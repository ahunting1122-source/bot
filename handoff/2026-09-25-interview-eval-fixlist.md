# 用牛客面经反向评估 Orbit Agent 设计（附修复指令）

日期：2026-09-25。面经来源：osillow《牛客》AI Agent 实习面试实录。
方法：把每道面试题当成对 Orbit 的"设计拷问"，以本仓库实际代码与 `ARCHITECTURE.md` 为依据逐题作答；答不上或答不圆的地方，就是缺陷，转为修复指令（见 §3）。

---

## 1. 总评

面经里的项目题可以归成四组：**流式链路 / RAG / 身份与幂等 / 编排与可观测**。

- **流式链路**：Orbit 的答案比面经里的"Emitter 推送"更强——事件溯源式 SSE，重连零丢失（§2.1）。
- **RAG**：双路 FTS5 + RRF + 引用回填硬约束是能讲出彩的部分；向量路还是空壳（§2.2，FIX-3）。
- **身份与幂等**：四层 ID 维度清晰，`operation_id = task_id:call_id` 是亮点（§2.3）。
- **编排**：单图 + policy 扩展点，与面经"多模块而非多 agent"的结论一致且更有结构（§2.4）。
- **明确缺失**：多模态（OCR/图像/ASR）完全没有（FIX-2）；评测只有口径没有基线数（FIX-4）。

---

## 2. 逐题回答（以 Orbit 口径）

### 2.1 「这个 agent 项目怎么开发的？前端 SSE 流对话到后端执行完成的核心链路」

核心链路一句话：**前端 EventSource 订阅任务事件流 → 后端 LangGraph 单图执行，每一步落 `task_events` → SSE 端点按游标增量回放 → 任务终态发 `settled` 关流**。

具体实现（与面经答法对照）：

1. **提交**：`POST /api/conversations/{id}/messages` 返回 202 + task_id（幂等任务提交），前端不阻塞等待。
2. **订阅**：`GET /api/tasks/{task_id}/events?after=<cursor>`，`media_type=text/event-stream`。前端 `new EventSource(url, { withCredentials: true })`。
3. **事件源是数据库不是内存 Emitter**：`task_events` 表是唯一事件源，每条事件有自增 `id`；SSE 端点 0.1s 轮询 `id > cursor` 的增量，逐条以 `id: <n>\ndata: <json>` 帧写出。
4. **断线重连不丢事件**：浏览器 EventSource 原生带 `Last-Event-ID` 重连，后端取 `max(after, last-event-id)` 作游标——重连后从断点回放，**不存在 Emitter 内存队列那种"断线期间事件丢失"问题**。
5. **判活**：后端空闲时发 `heartbeat` 事件；前端有 3.5s 看门狗（`useWorkspace.ts`），超时主动断开重连（指数退避）。
6. **终态**：任务进入 SUCCEEDED/FAILED/CANCELLED/WAITING_USER 且增量发完 → 发 `event: settled`，前端关流并刷新。
7. **流式分片**：`think` 节点流式产出 `message_started` / `message_delta` / `message_finished` 事件，前端拼 `liveReply` 实时渲染，`message_finished` 后并入时间线。

比面经答法强的点：面试者说"后端 Emitter 单向长连接推送"，追问"断线了怎么办"就容易卡住；Orbit 的"落库 + 游标回放"天然回答了重连、回放（`ui_smoke` 的 SSE 重连用例依赖这一点）和事件只增不改的演进约束。

### 2.2 「项目 RAG 是怎么存储和召回的？」

**存储（写入侧，挂在 digest 节点）**：
- 来源：浏览器观察的结构化正文 + 工具结果原文。**只收有真实出处的片段**（origin = URL 或文件路径），没有出处的正文不进索引。
- 切块：结构优先（标题/段落/表格按行）+ 定长兜底（300–500 中文字），带 50–100 字 overlap；大表按行组切并**重复表头**；块首拼标题路径。
- 索引：**两张 FTS5 虚表**——`chunks_fts_trigram`（trigram 分词，主路径）+ `chunks_fts_chars`（unicode61 + CJK 逐字，补齐 2 字中文词）。FTS5 的 tokenizer 是表级选项，只能建两张表，各挂 AI/AD/AU 同步触发器。

**召回（读取侧，prepare 节点预取）**：
- 查询按窗口拆词（trigram 3 字滑窗 + 字符级相邻两词滑窗，短语 OR 连接）——整句当短语查询必然 0 召回。
- 三路候选（trigram ‖ 字符级 ‖ 向量）→ **RRF 融合**（k=60，只用排名不比分数）→ top-K≤5，单次注入 ≤2500 字。
- **保守 gating**：命中分不过阈值不注入；`1+1` 这类无检索任务零注入，不会被误标"未引用来源"。
- **引用回填硬约束**：检索任务回答必须标注来源 origin，finalize 校验（归一化匹配 host+path），不匹配判未完成。

**如实回答的部分**：向量路当前是**空实现**（如实返回空列表，不污染排名）——sqlite-vec + fastembed 在阶段 6，未开始（→ FIX-3）。评估口径已落（`retrieval_injected` / `citation_checked` 事件，eval_run.py 出 recall@5 与引用覆盖率两列），但**还没跑出真实基线数**（→ FIX-4）。

### 2.3 「项目 OCR 是怎么实现的 / 多模态输入？」

**如实回答：Orbit 目前没有 OCR、没有 ASR、没有图像输入**。面经答法（多模态走大模型 API + ASR 接入）在 Orbit 里只对了一半：模型调用层（connectors）是通用 chat completions，但从未传过 image content。截图目前只落 `artifacts` 存路径，不进模型上下文。

这是一个**范围未拍板**的缺口而非 bug——本地网页 agent 最有价值的形态是"截图 + DOM 结构一起给 vision 模型看"，但会增加 token 成本（→ FIX-2，需拍板）。

### 2.4 「UUID 的实现和维度是什么？」

四层 ID，各管一件事（`storage.py` / `nodes.py` 实际代码）：

| ID | 生成 | 维度/作用 |
|---|---|---|
| `conversation_id` | `uuid4()` | **会话维度**——多轮上下文、摘要、RAG 默认收窄范围 |
| `task_id` | `uuid4()` | **执行维度**——一次任务、一条 LangGraph thread、一条 SSE 事件流 |
| `operation_id` = `task_id:call_id` | 组合 | **幂等维度**——工具副作用去重，重启重放不重复执行；`tool_results` 按它登记 |
| `event.id` | 自增整数 | **顺序/续传维度**——SSE 游标（`after` / `Last-Event-ID`）、事件只增不改的前提 |

另有 `response_id`（`uuid4().hex`）标记一次模型流式响应，前端用它把 `message_delta` 分片归组。

与面经答法的差异：面试者答"sessionId 一个维度"，Orbit 把**幂等**和**续传**从会话维度里拆了出来——这是能被追问"重启后工具会不会重复执行"时立得住的答案。没有 user 维度：本地单用户（loopback + Cookie 会话），是刻意的 v1 范围。

### 2.5 「多模块 agent，为什么不用多 agent 形式？」

面经答法（协商开销大、耗时不接受）成立，Orbit 的结构把这个论点工程化了：
- **单 StateGraph**：装载 → think ⇄ gate → execute → digest → budget → finalize，全部节点共享一个 `AgentState`，没有跨 agent 消息协商。
- **9 个 Protocol 扩展点**（`app/policies.py`）：要加能力 = 加一个 policy 实现，不开新 agent。
- **单 worker 队列，一次一任务**：本地浏览器操作是强串行资源，多 agent 并发操作同一个浏览器只会互相踩。

什么时候才值得上多 agent：任务可天然并行（多个标签页/多个站点）、且单上下文装不下时。当前规模（96 测试、11 评估用例）远没到。

### 2.6 「为什么不用 Spring AI 之类的框架？agent 之间怎么传数据？」

- 框架选择：**用 LangGraph 但不用它的预置 ReAct agent**——沿用它的状态机/消息模型/checkpoint/interrupt（自研这些没有价值），自建 gate/digest/budget/budget 这些 harness 节点和 9 个 Protocol。与面经"自研向量机"相比，Orbit 的立场是**自研的点必须有独有价值**：gate 的人工介入判定、digest 的索引写入、budget 三层刹车，这些是框架没有的；状态机和消息模型不是。
- 数据传递：不需要"agent 之间传数据"（没有多 agent）——节点间靠 `AgentState` + reducer；跨重启靠 `AsyncSqliteSaver` checkpoint 每步落盘；跨会话靠 SQLite（messages / tool_results / chunks）；上下文装配集中在 `prepare`（记忆 + 摘要 + 技能简述 + RAG 预取），**下游节点永远不从原始消息列表里自己捞上下文**。

### 2.7 「项目中都是交给 AI 完成流程吗？哪些是自己实现的？」

Orbit 是面经里说的后者——**agent 模式**（模型在 think 节点决定调用什么工具），不是预编排工作树。但"交给 AI"只覆盖**决策**；harness 全部是确定性代码：
- 自己实现：工具契约与唯一分发点（校验→幂等→分发→归一→入库）、四类结果状态机（ok/error/needs_user/uncertain）、digest 治理、三层预算刹车、引用回填校验、事件层与 SSE 回放、RAG 双路检索。
- 交给模型：每一步做什么、参数怎么填、失败后怎么调整、finalize 怎么总结。
- 保留给人的：`ask_user` 与登录墙 `needs_user`。

### 2.8 「回复是作为一个整体回复前端吗？」

不是，两层分离：
1. **过程流式**：`message_started/delta/finished` 事件实时推，前端拼 liveReply。
2. **正文真值**：落库的是 **finalize 的产出**（`state["final"]["text"]`），不是 `messages[-1]`——流式分片丢了也不影响持久化正文，刷新页面从 DB 读回完整结果。

### 2.9 「用 AI 开发时怎么排查错误？」

- 运行时可观测：`task_events` 全量事件（每个节点动作、每次工具调用都有事件）+ `tool_results` 存原文/耗时/大小——管理端时间线可以回放任意一次任务的完整过程，等价于自带埋点。
- 开发期质量门：pytest 96 用例、ruff + mypy、前端 lint + build、`ui_smoke.py` Playwright 13/13 交互用例——AI 改坏了立刻红。
- 效果评估：`eval_run.py` 11 用例，按 `retrieval_injected` / `citation_checked` 事件出 recall@5 与引用覆盖率。

---

## 3. 修复指令清单

| # | 发现 | 指令 | 状态 |
|---|---|---|---|
| **FIX-1** | SSE 空闲心跳每 100ms 一帧（10 次/秒），每个连接白耗带宽与 CPU；且与前端 3.5s 看门狗耦合过紧的频率没人写注释 | **心跳限频 1s**：保留 0.1s 事件轮询（低延迟），空闲心跳改为距上一帧 ≥1s 才发。验证：pytest / ruff / mypy 全绿，ui_smoke 的 SSE 重连用例不回归 | ✅ **已修复**（`backend/app/main.py`，96 passed / ruff / mypy 全绿；ui_smoke 未在本轮跑，下次有浏览器环境补跑） |
| **FIX-2** | 多模态（OCR/图像输入/ASR）完全缺失 | 拍板范围：最小实现 = 浏览器截图（已有 artifacts）作为 vision content 传入 think 节点；OCR/ASR 明确写入 ROADMAP 为不做或后期。**动手前需用户拍板，勿自行实现** | ⬜ 待拍板 |
| **FIX-3** | RAG 向量召回路是空实现，"语义相同用词不同"查不回 | 按 ARCHITECTURE §7.3 实施阶段 6：fastembed + BAAI/bge-small-zh-v1.5 + sqlite-vec，切块长度对齐 512 token，索引写入挂 digest 异步 | ⬜ 阶段 6 未开始（已有既定计划，非新债） |
| **FIX-4** | 评测只有口径没有数：recall@5 / 引用覆盖率从未在真实模型环境跑出基线 | 在有真实模型 + 浏览器的环境跑一轮 `eval_run.py`，把两列基线数写回 ROADMAP；KPI 阈值等有数再定 | ⬜ 待环境 |
| **FIX-5** | SSE 靠 0.1s 轮询 DB 取增量，多连接时 DB 压力线性涨 | 可选优化：进程内事件通知（asyncio.Event / 内存 bus）替代固定间隔轮询，落库回放机制保留。本地单用户下低优先级，**不急** | ⬜ 低优先级 |

---

## 4. 面试个人题（与 agent 设计无关，略答）

实习时长/学校平衡、AI coding 比例、会哪些语言、Vue 响应式（defineProperty→Proxy，ref/reactive 区别）、二叉树遍历/爬楼梯——这些是个人面问题，与本仓库设计评估无关，不展开。

## 5. 结论

按这份面经拷问一遍，Orbit 在**流式链路（断线重连）、幂等设计、harness 边界**三处能给出比"Emitter 推送 / sessionId / 全交给 AI"更深一层的答案；真正的短板集中在：**多模态缺失（需拍板）、向量召回未落地（计划内）、评测无基线数（需环境）**。本轮顺手修掉了 SSE 心跳频率问题，基线保持全绿。
