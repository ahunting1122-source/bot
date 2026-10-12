# 开源 Harness 调研：Hermes vs DeepSeek Harness

日期：2026-10-08。目的：**看别人怎么造 harness**，为 Orbit 的设计决策取证。对照物是本仓 `HARNESS_DESIGN.md`（Harness = 模型的"主板"+ 六项职责）。

两个对象均已核实为真实、活跃的开源项目：

| | Hermes | DeepSeek Harness |
|---|---|---|
| 出品 | NousResearch（`NousResearch/hermes-agent`） | DeepSeek AI（`deepseek-ai/deepseek-harness`） |
| 命令 | `hermes` | `dsh`（npm `@deepseek-ai/dsh`） |
| 许可 | MIT | MIT |
| 状态 | 持续高频迭代（每日数十 PR） | Developer Preview（0.1.7-rc.2 → 0.2 桌面预览） |
| 定位 | 长时运行的**个人 Agent**（终端/IDE/消息平台/定时） | 通用 Agent **运行时底座**（桌面/Web/headless/SDK） |
| 一句话 | **边界清晰的单体 harness**，把 session 当基础设施 | **一切皆插件**，连 agent loop 本身都可替换 |

---

## 一、Hermes（NousResearch）

参考：Arize 的九层 harness 分析（Aparna Dhinakaran, 2026-06）+ 仓库源码展开 + 官方文档。

### 1.1 九层模型里 Hermes 的实现

| 层 | Hermes 的做法 |
|---|---|
| 外层迭代循环 | 同步 `AIAgent` 循环（`agent/conversation_loop.py`）；**最强的是 Provider 抽象**——同一运行时驱动 chat-completions / Anthropic Messages / Codex Responses / 进程外 Codex app-server / Bedrock，差异由传输适配器归一化 |
| 上下文管理与压缩 | 完整压缩链路（非截断）；三步：**裁剪超期工具输出 → 保护头尾 → 辅助模型摘要中段** |
| 技能与工具管理 | **注册（import 时进中央注册表）与暴露（toolset 层按运行决定模型看到什么）分离** |
| 子 Agent 管理 | `tools/delegate_tool.py`：子运行有自己的 task ID / 终端上下文 / 迭代预算，向父返回结构化摘要 |
| 内置预制技能 | 数十个领域技能；首次跑通的复杂流程可沉淀为 Skill |
| 会话持久化与恢复 | **SQLite + FTS5 + WAL**（不支持 WAL 的文件系统有降级路径）；session 是运行时基础设施 |
| 系统提示装配 | **显式三层**：Stable（身份 SOUL.md / 工具指南 / 技能索引 / 环境提示）· Context（cwd 的 AGENTS.md/CLAUDE.md/.cursorrules，**加载前做提示注入扫描**）· Volatile（记忆快照 / 用户画像 / 时间戳） |
| 生命周期钩子 | **两个钩子面**：进程内的 plugin 钩子（pre/post tool call、gateway pre-dispatch、审批请求/响应，可阻塞/改写/透传）+ 文件系统驱动的 gateway 钩子（shell/python 脚本挂在 gateway 启动 / agent step / 命令触发） |
| 权限与安全层 | 分级审批（**Smart / Manual / Off**）+ **不可逆动作的固定黑名单（YOLO 模式下也拦）** |

### 1.2 四个最有辨识度的设计

1. **压缩 = session 生命周期事件（谱系化）**。压缩时：关闭当前 session 行 → 用摘要种一个**子 session** → **轮换 session ID** → 记录父子谱系；并通知 plugin context engine 与 memory provider"边界移动了"。多次压缩得到**谱系链**（session1→2→3），而不是一条被反复重写的对话记录。摘要预算约压缩内容的 **20%，下限 2k / 上限 12k token**。
2. **注册 vs 暴露分离**。工具库可以很大，但单次运行暴露给模型的表面被四层过滤收窄：平台过滤 → 场景过滤 → profile 过滤 → 委托过滤。
3. **Session 是服务，不是聊天记录**。CLI / 消息网关 / cron 挂到**同一个 session 平面**；消息在推理前就能路由到正确 session；cron 在无活跃终端时也能写入 session。还对外暴露 `session_search` 工具，把"回忆"交回模型循环。
4. **超出九层的三个子系统**：消息网关（20+ 平台）/ Profile（隔离的 agent 根）/ **cron 是一等子系统**（持久化作业、受同一权限机制管控、每 profile 隔离）。

### 1.3 执行控制的硬边界（来自源码级分析）

- **预算**：父 Agent **90 轮**上限，子 Agent **50 轮**上限。
- **级联中断**：父 Agent 心跳超时 **30s** → 子 Agent 连锁停止。
- **"中断伪造"**：打断时补发一条 tool result 占位，保持消息结构合法（解决 API 对 tool_call/tool_result 配对的强制性）。
- **错误分类驱动恢复**：14 类 `FailoverReason`（如 402 额度耗尽→立刻换 Key；429 限流→退避重试）。
- **风险分级按动作而非工具名**：`/tmp` 写入放行、`.env` 修改需审批、删除/部署强制确认——同一个 file 工具，不同动作风险不同。

### 1.4 已知短板（作者与社区共同的判断）

- **子运行没有一等控制平面**：绝大多数子任务活在父调用路径下，父结束子即结束；缺 run ID / 显式生命周期 / 外部操控 / 父完成后仍存活的清理语义。这是与 OpenClaw 编排层的主要差距。
- **提示词快照冻结**换稳定性（典型成本 vs 实时性取舍）。
- 曾内置 `tirith` 命令扫描器，因**误报过高**（14/16 攻击命中，但 12/24 日常命令被误伤）已于 10-06 从 core 移除 → 提示"事后扫描式安全"的局限。

---

## 二、DeepSeek Harness（dsh）

参考：官方 `docs/architecture.md` + Cordis 论文《A Programming Paradigm for Spatiotemporal Composability》+ 源码级剖析。

### 2.1 底座：Cordis，"一切皆插件，没有特权内核"

dsh 是一个 **Cordis 插件树**。Cordis（来自 Koishi 生态，已 vendored 固定版本）管理插件的挂载/卸载/依赖。五个核心概念：

1. **插件实现 Service**（框架管生命周期）
2. **Context 是服务容器**：每个服务在稳定的 `ctx.*` 上，**按 key 查找而非 import**
3. **依赖靠 `inject` 声明**，加载顺序由依赖图推出
4. **事件是类型化的**，五种派发模式：`emit`（观察）/ `waterfall`（级联，可短路）/ `parallel`（扇出）/ `serial`（顺序）/ `bail`
5. **注册是副作用**：所有贡献走 `ctx.effect()`，插件卸载时**自动撤销**（可逆系统）

关键句：**"不存在需要打补丁的特权内核——扩展 dsh 的方式是把插件挂到其他插件旁边。"** 连 **agent loop 本身都是插件**（`ctx.agentLoop`），可以在配置里整体替换，不用改源码。

### 2.2 核心包（`docs/architecture.md` 原表）

| 包 | 拥有 | `ctx` key |
|---|---|---|
| `core/session` | append-only 的 `SessionEvent` 日志 + 内存存储 | `ctx.sessions` |
| `core/system-prompt` | prompt 段落 + 工具 schema 装配 | `ctx.systemPrompt` |
| `core/tools` | **scoped 工具注册表** + 受控执行管线 | `ctx.tools` |
| `core/agent` | `Agent` 接口、live 注册表、`agent/*` 事件 | `ctx.agents` |
| `core/agent-loop` | 默认驱动（实现上述接口） | `ctx.agentLoop` |
| `core/scope` | per-agent 作用域注册原语 | 库，无 key |
| `llm/llm` | Message/stream 词汇 + 适配器 seam | `ctx.llm` |

### 2.3 Turn flow（原文）

```
turn/start
  claim next-step input plus one queued message
  assemble prompt sections + tool schemas
  -> agent/pre-step                   reject | enter(messages)
     step/start
     append entered messages as user/message
     derive model history from the log
     agent/request -> llm/stream -> assistant/chunk* -> assistant/message
     tool/call* -> tools/pre-execute -> tools/execute -> tools/post-execute -> tool/result*
     step/end
     tools owe another request, or next-step input arrived -> claim -> next step
  -> agent/turn-stopping
turn/end
```

- **step = 一次模型请求 + 它调用的工具**；**turn = 0..n 个 step**。
- 事件分三个域：**Session 事件**（持久事实，重载后仍在）/ **Agent 事件**（`agent/*`，观察或拦截在途工作）/ **Capability 事件**（把策略和适配器挂到 seam 上：`fs/*`、`tools/*`、`telemetry/*`）。
- `agent/pre-step`、`agent/request`、`llm/stream`、三个 `tools/*` 是 **waterfall**（监听器必须调 `next()` 才继续）；`agent/turn-stopping` 是 **serial**（无 next）。

### 2.4 三条不变量 / 关键机制

1. **"模型可见即已记录"（Model-visible means logged）**：任何到达模型请求的东西都必须能从日志重建，且**有运行时断言**。所以新增一种"模型可见输入"必须先加一个 session 事件。
2. **Session log 是上下文唯一真源**：`deriveMessages()` 从日志投影模型历史；fork / resume / transcript / telemetry / 持久化全从这条流派生。
3. **Capability seam（能力缝）三件套**：**Service Definition（接口，拥有 ctx key）+ Service Provider（实现）+ Consumer（用者，通常是模型可见工具）**。三者齐备才算一个 seam。好处：**换一个 provider 就换掉整个产品能力**——文件系统与子进程 provider 共享同一个"执行世界"，把它指向远程沙箱，Bash / PTY / LSP 一起搬走，consumer 代码不变。**Subagent 也是一个 seam**：provider 可以从"新建子 agent"到"把整轮委托给另一个产品"任意变化。

### 2.5 组合与装配

- **bundle**：Cordis 配置行 + 其挂载代码的分发格式，插入的东西始终可被上层 patch。
- **profile**：Harness home 里的具名组装，列自己叠的 bundle、树外插件、用户自己的 `cordis.patch.yml`。发行版自带 `web` / `headless` / `sdk` / `sdk-minimal` / `acp`。
- 分层顺序：各 bundle → profile patch → home patch → `--patch` 覆盖。`dsh --profile web --dump-config` 打印真实启动树，**任何一行都能被自己的 patch 替换**。
- `dsh-base` 是每个 profile 的第一层：模型适配器、工具、持久化、**沙箱与审批策略**、设置、凭据、遥测。

### 2.6 产品面与已知短板

- 模式：标准 / PTC（批量调工具）/ 极简 / 创造（对话造插件）。
- 内置插件：智能体团队、语音输入、终端、Agent loop、Subagent、网页搜索；插件管理页支持 npm 包名安装。
- v0.1.7-rc.2 亮点：**动态工具 + KV Cache**（模型声明支持时，中途加工具不使 KV cache 失效）、定时任务、Desktop 后台执行、auto review 后的人工接管。
- **短板**：资源占用（长会话 + 持久化下 **14.7GB 常驻内存**，idle ~500MB）、pre-1.0 迭代快、**插件无沙箱隔离**（可执行任意 TS 代码，不可信第三方插件有安全风险）、`ctx.effect()` 契约靠自觉（绕过则卸载时资源泄漏）。

---

## 三、两者对比（按 Orbit 的六项职责对齐）

Orbit 的 harness 六职责来自 `HARNESS_DESIGN.md`：上下文装配 / 能力声明 / 调用分发 / 权限与确认 / 结果治理 / 生命周期与恢复。

| Orbit 职责 | Hermes | dsh | Orbit 现状 | 差距判断 |
|---|---|---|---|---|
| 上下文装配 | 三层 prompt（Stable/Context/Volatile）+ 注入扫描；压缩产出**谱系** | prompt 段落由 `ctx.systemPrompt` 装配，插件注册段落 | `build_context()`：系统提示+记忆+摘要+最近消息+技能+检索 | 中；Orbit 无"三层"显式分层，压缩无谱系 |
| 能力声明 | 中央注册表 **+ toolset 四层过滤**（平台/场景/profile/委托） | `ctx.tools` scoped 注册表，schema 自动进 prompt 装配 | `ToolRegistry.specs()` 一次全暴露 | **小→大**：Orbit 缺"注册 vs 暴露"分离 |
| 调用分发 | 单循环 + 传输适配器归一化多 provider | `tools/*` 三事件 waterfall + seam 三件套 | `registry.execute()` 唯一分发点 + `policies` 9 Protocol | **Orbit 已接近**：policies 就是 seam 雏形 |
| 权限与确认 | Smart/Manual/Off 分级 + **不可逆动作用固定黑名单** | 沙箱 + 审批策略（`dsh-base` 内置）+ auto review | 事前零确认（v1 有意为之）+ 事后审计 + 预算刹车 | 有意偏离；**但"固定黑名单"值得立刻补** |
| 结果治理 | 工具结果大小上限 + 超期裁剪 + 14 类错误分类 | **"模型可见即已记录"运行时断言** | `digest` 是 tool_results 唯一写入口 + 引用登记 | **Orbit 已对齐**，可再加"可重建"断言 |
| 生命周期与恢复 | **session 作服务**（CLI/网关/cron 同平面）+ 幂等 + 级联中断 | append-only **SessionEvent 日志** + fork/resume/telemetry 全派生 | 队列/worker/取消 + `Operation` 幂等 + checkpoints | 中；Orbit 的 session 平面未跨"交互/cron" |

### 两条"设计哲学"对照

| 维度 | Hermes | dsh |
|---|---|---|
| 架构形状 | 单体 harness，边界清楚、务实 | 组合式运行时，一切可换、可热重载 |
| 扩展方式 | 写 plugin / 挂 lifecycle hook；改核心要改源码 | **不改源码**——挂一个插件在旁边，或直接换掉 agent loop |
| 稳定 vs 灵活 | 稳定优先（提示词快照冻结换缓存友好） | 灵活优先（接受启动拓扑开销与内存代价） |
| 上下文真源 | SQLite session 行（可压缩、有谱系） | append-only 事件日志（不可变，投影出上下文） |
| 安全 | 分级审批 + 固定黑名单（Tirith 已弃，改回最小/固定拦截） | 沙箱 + 审批策略；插件无隔离是已知债 |

---

## 四、对 Orbit 的启示（可借鉴 / 已有 / 不建议）

### ✅ 建议借鉴（有具体落点）

1. **"注册 vs 暴露"分离**（Hermes）。Orbit 现在 `specs()` 一次性全暴露；`retriever_search` 的"每任务 4 次"只是它的粗糙版。可加一个 per-run 暴露层（按任务/站点收窄），直接省 token 并降低误用。
2. **压缩 = 生命周期事件 + 谱系**（Hermes）。Orbit 有摘要/`digest`，但压缩是原地改写；改成"关旧 session → 子 session 种摘要 → 记父子"能保住可回溯性，和 Orbit 的"事件只增不改"哲学一致。
3. **"模型可见即已记录"做成运行时断言**（dsh）。Orbit 已有 `context_meta` 与"finalize 产出落库"的习惯；把它升级为**断言**（任何进模型请求的东西必须能从库重建），就能真正支撑回放与评估。
4. **不可逆动作固定黑名单**（Hermes）。Orbit v1 是"事前零确认"，但 Hermes 在 YOLO 下也保留一份**不可逆动作黑名单**。这是低成本、高价值的安全兜底，不违背"零确认"的信任模型。
5. **父子预算 + 级联中断 + "中断伪造"**（Hermes）。父 90 / 子 50 轮、心跳 30s 级联停、打断时补发 tool_result 占位保持消息配对合法——这三条对 Orbit 未来的 subagent 与"用户中途打断"都有直接价值。
6. **cron 与交互共享 session 平面与权限机制**（Hermes）。Orbit 的 automation 现在是独立层；让定时任务挂到同一 session + 同一 gate，可避免"无人值守路径绕过治理"。

### 🟡 已有，可对照打磨

- **seam 三件套**：Orbit 的 `policies.py` 9 个 Protocol + 在 `AgentRunner.__init__` 注入，**已经是 seam 的 Service Definition 部分**；dsh 的差异在"运行时可换 + 可热重载 + 撤销副作用"。Orbit 是启动期注入，够用，不必现在上热重载。
- **唯一分发点 / 唯一结果契约 / 唯一确认入口**：Orbit 三条都已明确，与两家一致，是正确方向。

### ❌ 不建议现在做

- **"一切皆插件"/循环可替换**（dsh）：Orbit 是单人本地项目，插件树 + 依赖图 + 可逆副作用会带来启动拓扑开销与调试复杂度（dsh 自己就背了 14.7GB 常驻内存和"插件无沙箱"的债）。Orbit 走 Hermes 式**边界清晰的单体 harness**更合适。
- **20+ 消息平台网关**：超出 Orbit 的浏览器 Agent 定位。
- **事后命令扫描器**（Hermes 的 Tirith 教训）：误报率高、只覆盖终端字符串、绕过面大，最终被上游移除——不要重走。

### ⚠️ 两家共同的"未解前沿"（Orbit 可关注）

- **持久化子运行控制平面**：Hermes 明说这是它 vs OpenClaw 的差距（缺 run ID / 外部操控 / 父完成后存活）；dsh 的 Agent Teams 是实验性 opt-in（durable roster + task board + mailbox）。Orbit 的 `subagents` 协议已声明未消费——**落地时按"一等控制平面对象"设计，而不是父调用路径的附庸**。

---

## 五、参考来源

- Hermes：`github.com/NousResearch/hermes-agent`；Arize《How Hermes implements an open source agent harness architecture》(2026-06)；`innfactory.ai/en/ai-harness/hermes-agent`；源码级剖析（`agent/conversation_loop.py`、`tools/delegate_tool.py`）。
- DeepSeek Harness：`github.com/deepseek-ai/deepseek-harness` + `docs/architecture.md`；Cordis 论文《A Programming Paradigm for Spatiotemporal Composability》；`deepseek.com/harness/` 官方页；v0.1.7-rc.2 发布说明。
