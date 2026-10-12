# Hermes 源码实证：Skill 层与 MCP 层（2026-10-08 二次核查）

**触发**：核查 Orbit 的 Skill/MCP 实现情况后，回头验证 Hermes 这两层到底怎么做的。
**方法**：直接读 `NousResearch/hermes-agent`（main 分支）源码，不再依赖前作报告转述。
**结论一句话**：**前作报告有一处实质错误需要纠正**（"5 次工具调用触发技能生成"不存在），
且 Hermes 这两层的真实形态与 Orbit 的差异比原报告描述的**大得多**。

---

## 0. 纠正前作报告的一处错误

`handoff/2026-10-08-hermes-deep-dive.md` §4 写：

> **触发阈值**：一次任务满足任一条件才生成 skill —— **≥5 次工具调用**、**发生了一次错误恢复**、**用户做了一次纠正**。

**源码实证：不成立。** 逐文件核过 `curator.py`(68KB)、`background_review.py`(78KB)、
`review_engine.py`、`learning_mutations.py`：

- **没有任何"工具调用次数"阈值**。全仓无 `MIN_TOOL_CALLS` 类常量。
- **"错误恢复"不是触发条件，而是反面约束**——`_DO_NOT_CAPTURE_BLOCK` 明确要求
  "会话内已解决的瞬时错误**不要**捕获；重试成功了，该记的是重试模式，不是原始失败"。
- **"用户纠正"确实存在，但性质不同**：它不是"触发条件"，而是 review prompt 里的
  **信号清单第一项**（原文 "FIRST-CLASS skill signal"）。判据是**定性的**，不是计数。

正确的机制是：**没有阈值，靠"高频 review + 强信号清单"驱动**。原文：

> Be ACTIVE — most sessions produce at least one skill update, even if small.
> A pass that does nothing is a missed learning opportunity, not a neutral outcome.

→ 这条纠正对 Orbit **很重要**：前作报告若被当作事实采纳，会误导我们去实现一个
**Hermes 根本没做**的计数阈值。**Orbit 现在也没做阈值，这一点上是对的。**

---

## 1. Skill 层：Hermes 是"四个子系统"，Orbit 是"一个模块 + 四道闸"

### 1.1 读侧（`tools/skills_tool.py`）

工具**只有两个，且都是只读**：`skills_list`（仅 name+description）、`skill_view`（全文 + 链接文件）。

Frontmatter 字段比 Orbit 丰富得多：`name` / `description` / `tags` / `related_skills` /
`compatibility` / `metadata.hermes.*` / `deps` / `required_credential_files` /
`required_environment_variables`。

**多根 + tier 优先级**（Orbit 是单目录 `data/skills/`）：
`project > local > create_dir > external_dirs`。跨 tier 冲突按优先级裁决并记日志；
**同 tier 不同技能同名 → 拒绝猜测**（`AMBIGUOUS_SKILL_PREFIX`）。

**去重三层**：① 会话内 repeat-view stub（同文件未变更返回短 stub，上下文压缩时清缓存）
② 候选按解析后路径去重 ③ 目录扫描缓存 TTL 30s（按 mtime 签名）。

### 1.2 写侧（`tools/skill_manager_tool.py`）—— 与 Orbit 差异最大的一处

工具名 `skill_manage`，**唯一对外形态是一个 `operations` 数组**（memory-tool 模式）：

| action | 说明 |
| --- | --- |
| `create` | 完整 SKILL.md，必须在该 skill 其他 op 之前 |
| `patch` | `old_string`/`new_string` **（首选）**；或 `content` 全量重写（最后手段） |
| `write_file` / `remove_file` | 支持文件，限 `references/` `templates/` `scripts/` `assets/` |
| `delete` | 需 `absorbed_into=<umbrella>` 声明吸收目标 |

**关键差异（Orbit 完全没有的）**：

1. **审批门（staging）**：skill 写入"太大无法内联审查"，所以**无论来源一律暂存**，
   返回 `{"staged": true, "pending_id": ...}`，审批后回放。
   → Orbit 是 v1 零确认模型，`skill_save` 直接落盘标 `unreviewed`。
   **Hermes 对"写技能"是例外地保留了人工门**，这值得注意：它把技能当成比工具调用更需要审查的东西。
2. **名称/规模硬约束**：`MAX_NAME_LENGTH=64`、`MAX_DESCRIPTION_LENGTH=1024`、
   `MAX_SKILL_CONTENT_CHARS=100_000`、`MAX_SKILL_FILE_BYTES=1MiB`；
   新建 skill 的 description 还受 **系统提示预算**（`SKILL_PROMPT_DESC_LIMIT`）约束。
3. **per-skill 文件锁**（跨整个 read-modify-write，`.locks/` 下按 SHA256 命名）。
4. **原子写 + 安全扫描**：`atomic_write_text`；扫描判 `"ask"` 则**回滚**原内容。
5. **目录接管守卫**：`mkdir(exist_ok=False)`，EEXIST 时只允许复用**空真实目录**。
6. **kill-switch 式防护**：`_validate_delete_target` 注释直接引用了 Kilo Code #11227
   （内置技能哨兵解析到 server cwd，递归删除**抹掉整个工作目录**）——防的就是 rm -rf 类事故。

### 1.3 学侧（`agent/background_review.py`）—— 真正的"学习回路"

**每轮对话后** fork 一个 AIAgent **守护线程**重放快照，问"该存/改什么技能或记忆？"

- **分叉要保证提示缓存一致**：byte-identical system prompt / tools[] / reasoning config，
  共享 session_id —— 官方注释称带来 **~26% 成本削减**。
- **持久化完全隔离**：`_persist_disabled = True`、`_session_db = None`、`skip_memory=True`。
- **前台优先**：新实时轮次到来即取消 review，等待上限 **2.0s**，超时告警后继续实时轮次
  （原文："self-improvement work must never block a user-facing turn"）。
- **预算**：`_REVIEW_MAX_ITERATIONS=16`；输入 token 预算 = 上下文窗口 **75%**，
  硬上限 **600k**，未知窗口回退 **120k**。
- **工具白名单只在调度侧**（保持 `tools[]` 字节一致）：只读 `read_file`/`search_files`；
  **写工具禁用**，自主维护必须走 `skill_manage` 校验。

**信号清单（定性，四选一，无阈值）**：用户纠正风格/语气/格式/冗长度（一等信号）·
用户纠正工作流 · 出现非平凡技术/修复/绕法 · 本次加载的技能被证明错了。

**明确"不要捕获"五项**：环境相关故障 · 对工具的**负面断言**（"X 工具坏了"会固化成拒绝）·
会话内已解决的瞬时错误 · 一次性任务叙述 · **未解决的失败**
（不得把未验证的失败序列写成"可靠工作流"）。

> **这一项对 Orbit 直接可用**：Orbit 的 `skill_save` 目前只校验正文非空。
> 没有任何东西阻止模型把"这次失败了三次"沉淀成技能。

### 1.4 治理侧（`agent/curator.py` + `skill_manager_guards.py`）

**两段式，这是 Hermes 最清晰的设计**：

| 段 | 性质 | 触发 | 动作 |
| --- | --- | --- | --- |
| 确定性剪枝 | **纯时间戳，无 LLM** | 间隔 ≥168h **且** 空闲 ≥2h | `active↔stale↔archived` |
| LLM 整合 | fork，**默认关闭**（`DEFAULT_CONSOLIDATE=False`） | 仅候选存在时 | 合并进 umbrella / 降级为 references |

阈值：`stale_after_days=14`、`archive_after_days=30`、跨进程锁 `_CLAIM_STALE_SECONDS=3600`。

**三条 Orbit 该抄的不变量**：

1. **`use_count == 0` 是"缺乏证据"，不是"陈旧证据"**——技能年轻于 `stale_after_days` 时
   **绝不归档**从未用过的技能。原文注释直接把这两件事区分为不同概念。
2. **`absorbed_into` 强制**：LLM 整合 pass 里删技能**必须**声明吸收目标，
   否则拒绝（"那是确定性陈旧度 pass 的职责"）。背景注释引 #29912：
   无转发目标的删除曾**归档整簇活跃技能**，留下指向不存在名字的活跃自动化。
3. **只归档不删除**，且所有变更走 ledgered `skill_manage`（可回滚）；
   分叉 `enabled_toolsets=["skills"]`，**刻意不给终端**——因为 `mv/cp/rm` 会绕过账本、破坏回滚。

**守卫清单**（`skill_manager_guards.py`）：pin/essential 不删（patches 仍允许）·
background review 只能删 `created_by: "agent"` 的 curator 技能 ·
external_dirs 只读 · protected builtin / hub-installed / bundled 全拒 ·
**read-before-write 强制**（review fork 必须先 `skill_view` 再改，且 transcript 里引用过不算）。

---

## 2. MCP 层：Hermes 是"重工业"，Orbit 是"能用"

### 2.1 规模对比

Hermes 的 MCP 不是一个文件，是 **`tools/mcp_*.py` 约 25 个模块**：

```
mcp_tool.py / mcp_tool_lifecycle.py / mcp_tool_transport.py / mcp_tool_server_run.py
mcp_tool_discovery.py / mcp_tool_registration.py / mcp_tool_health.py / mcp_tool_errors.py
mcp_tool_scope.py / mcp_tool_sampling.py / mcp_tool_schema.py / mcp_schema_cache.py
mcp_tool_agent.py / mcp_tool_common.py / mcp_tool_config.py / mcp_tool_content.py
mcp_tool_handlers.py / mcp_tool_loop.py / mcp_tool_node_abi.py
mcp_oauth*.py (4 个) / mcp_liveness.py / mcp_death_supervisor.py / mcp_dashboard_oauth.py
```

Orbit 是**一个 299 行的 `mcp.py`**。

### 2.2 传输

Hermes：stdio + Streamable HTTP（新 `streamable_http_client` 与旧 `streamablehttp_client`
**双 API 兼容**）+ **SSE**，且 **HTTP → SSE 自动回退**（`_sse_fallback` 锁存）。

Orbit：stdio + Streamable HTTP，无 SSE，无回退。

### 2.3 生命周期（Orbit 的缺口正在这里）

Hermes 的机制密度远超我们方案里的"心跳 + 指数退避"：

| 机制 | 参数 | 解决的问题 |
| --- | --- | --- |
| 后台专用事件循环 | 守护线程 + `MCPServerTask` 长生命周期 Task | anyio cancel scope 必须在同一 Task 进出 |
| 保活（keepalive） | 默认 180s，下限 5s | 远端**必须**比服务端 idle-session TTL 更快 ping |
| **"未证明会话"快速掉线预算** | `_session_proven` | 握手后立即抖动的服务器仍会被 park |
| 熔断器 | 阈值 **3** 次连续失败 / 冷却 **60s** | 原文明确：防止模型陷入 **90 轮空转烧预算**（#10447） |
| 连接退避冷却 | **30 → 600s** | 防"重启风暴"：单个 stdio 服务反复重生成（#50394） |
| **parked 自探测** | **300s** | 被摘除工具的服务器"没别的东西能救活它"，只能自己探 |
| stdio 子进程死亡 | 15s 有界等待 + PID 快速失败 | 网关重启杀掉所有 MCP 子进程（#81995） |
| **父进程死亡监管** | 单 supervisor 进程 | `kill -9`/OOM 后 stdio 子进程变孤儿、永久存活 |
| 跨进程发现锁 | advisory file lock | gateway + CLI + TUI 不要各自重复发现（#62771） |
| 分页上限 | `_MCP_LIST_MAX_PAGES=50` | 防 forever-cursor 死循环 |

**区分两类失败**（`_server_errors_all_application`）：传输不可达 vs 服务可达但调用被拒
——决定熔断器打开的措辞（#11113）。

### 2.4 resources / prompts

**都用**，且不是硬编码假设——`initialize_result` 捕获握手返回的**真实声明能力**
（`.capabilities.resources` / `.capabilities.prompts`）再决定是否调用。
监听 `ToolListChangedNotification` / `PromptListChangedNotification` /
`ResourceListChangedNotification` 做**动态刷新**；通用 `_paginate_full_list(items_attr=...)` 三合一。

→ Orbit 方案阶段 B（prompts 作技能源）与阶段 D（resources 入 RAG）的方向正确，
但**前置条件**是像 Hermes 那样先读 `initialize_result` 判定能力，
而不是假设服务一定支持（否则对不支持的服务器会反复抛错）。

### 2.5 安全（Orbit 完全没有的部分）

1. **trust 分级**（`trust: full | untrusted`）：untrusted 服务器上**每个可写调用都要审批**；
   `readOnlyHint` 不是恰好 `True` 就算可写；**拼写错误 → 落到 untrusted**（fail-closed）。
   原文关键词：说谎的 `readOnlyHint` 只能"跳过操作员已被警告过的调用审批"，**永不扩大访问**。
2. **OSV 恶意软件预检**：stdio 启动前对 npx/uvx/pipx 包查 OSV，12s 墙钟上限、超时 fail-open。
   实现细节值得抄：预检必须看到**真实 argv**——任何把 argv 改写成 wrapper 的步骤都得在它之后，
   否则检查的是 wrapper，静默变成 no-op。
3. **OAuth 全家桶**（4 个模块）+ 凭据刷新锁。
4. **多 profile 按连接键隔离**：同名服务器不同凭据 = 两个连接，失败不互相遮蔽。

### 2.6 与 Orbit 注册表的耦合点（两条可直接抄的规则）

`tools/registry.py`：

- **同 toolset 重复注册允许**（MCP 重连/刷新）；**跨 toolset 遮蔽拒绝**（含 MCP→MCP），
  除非显式 `override=True`。→ Orbit 现在 `register()` 是**无条件覆盖**，
  MCP 服务与内置工具同名时会静默遮蔽。
- **`check_fn` TTL 缓存 + 失败宽限期**：结果缓存 30s；**60s 内成功过而当前失败 → 判为 flake，
  返回 last-good `True` 且不缓存**。原因写得很实：一个抖动的探针会**静默摘掉整个 toolset**，
  表现为子 agent 报"工具 read_file 不存在"。→ 对应 Orbit 方案里的心跳：
  **单次失败不该立刻摘工具**，必须有宽限期。

---

## 3. 对 Orbit 方案的修正建议

对比 `handoff/09-skill-mcp-closeout-plan.md`，按"该抄/不该抄"分类：

### 3.1 建议加进方案的（小而明确）

| # | 改动 | 依据 |
| --- | --- | --- |
| 1 | **`skill_save` 加"不要捕获"拦截**：拒绝以负面断言（"X 工具不可用"）/ 未解决失败写得像"可靠流程"的内容 | `_DO_NOT_CAPTURE_BLOCK` 原文；Orbit 现在只校验正文非空 |
| 2 | **`use_count==0` 永不归档**（Orbit 当前归档条件是 `usage_count>=3` 才启用低成功率归档，方向对，但应显式写"0 使用 = 缺乏证据"） | `curator.apply_automatic_transitions` |
| 3 | **心跳失败加宽限期**：不要单次失败就摘工具（Orbit 方案写的是"连续 2 次"——保留，但补上"上次成功在 N 秒内则判 flake"） | `_CHECK_FN_FAILURE_GRACE_SECONDS` 的设计说明 |
| 4 | **`register()` 拒跨 source 遮蔽**（Orbit 现在无条件覆盖） | `registry.register` 的 shadow 规则 |
| 5 | **测试期 schema 校验前置**：Hermes 在注册时就校验 schema 是 dict/parameters 是 object，防运行时 400 | `registry.register` 的 raise 检查 |

### 3.2 明确**不抄**的（不匹配 Orbit 现状）

| 项 | 为什么不抄 |
| --- | --- |
| staging 审批门 | Orbit v1 是零确认模型，且 `skill_save` 标 `unreviewed` 本身就不注入——等价效果已由状态机达成 |
| 后台 fork 学习回路 | Orbit 是单 worker 串行 + 无子 agent（`subagents` 零消费）。implement 它会顺带引入 fork/预算/缓存一致性一整套，**不是本轮范围** |
| curator LLM 整合 pass | 同上，且它默认就是关的 |
| MCP 重工业（25 模块 / 熔断 / 死亡监管 / OSV / OAuth） | 与 Orbit 单机单用户定位不匹配。**其中"熔断 + parked 自探测 + 宽限期"三样**是低成本高价值的，已并入 3.1 第 3 条 |
| 多根 tier（project/local/external） | Orbit 单 data_dir，无 profile 概念 |

### 3.3 结论

**Orbit 的 Skill 层在"治理严谨度"上不落后**（三态状态机 + 成功终态回写 + 超期/低成功率归档，
比 Hermes 更严）；**落后的两处是**：① 写入侧没有任何内容层面的守门（Hermes 有"不要捕获"清单）
② 没有操作面（Hermes 有 26 类目技能库 + hub + 完整 CLI/curator 命令）。

**MCP 层则确实是数量级差距**，但**大部分差距来自 Orbit 不需要的运维复杂度**
（多 profile、gateway 集群、跨进程锁）。真正该补的仍然只是方案里的三项 +
3.1 的宽限期与遮蔽规则。

---

## 4. 来源

- `tools/skills_tool.py`、`tools/skills_tool_dedup.py`、`tools/skill_manager_tool.py`、
  `tools/skill_manager_guards.py`、`tools/skill_provenance.py`、`tools/skill_ledger.py`
- `agent/background_review.py`（review prompt 全文）、`agent/curator.py`、
  `agent/review_engine.py`、`agent/skill_commands.py`、`agent/learning_mutations.py`
- `tools/registry.py`、`tools/mcp_tool.py` + `tools/mcp_*.py`（25 个模块）
- 仓库：`https://github.com/NousResearch/hermes-agent`（main，MIT），核对于 2026-10-08
