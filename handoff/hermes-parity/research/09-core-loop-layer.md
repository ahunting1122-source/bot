# Hermes Agent 核心循环层 / 上下文装配层 / 入口·UI 层 / 可观测性层 —— 机制清单

> 目标：为 Orbit 复刻「Hermes 唯一没有 .py 源码的那一层」建立可对齐的机制基线。
> 目标机器：Windows，runtime `0.21.0-cn.18`（CN 发行版），项目根 `E:\bbbooottt`。
> 产出日期：依据本机 2026-08 ~ 2026-10 的运行痕迹交叉还原。

---

## 0. 取证方法与硬约束（先读）

| 项 | 内容 |
|---|---|
| 只读纪律 | Hermes 安装目录与 `hermes-home` 全程只读；sqlite 一律 `file:...?mode=ro&immutable=1` 打开；未修改任何文件。 |
| exe 只跑 `--help` | 仅执行 `hermes --help` 及 `<subcommand> --help`。**没有**运行 `status`/`dump`/`prompt-size --json` 等会读取或改变状态的子命令。 |
| 无源码层 | `agent/*`、`run_agent`、`hermes_state`、`hermes_cli`、`toolsets` 打包在 exe 的 PYZ 内。实测 `_internal\agent\` 下只有 `models_dev_snapshot.json` 与两个 swarm 提醒 md，**无 .py**（【实证】`Get-ChildItem _internal\agent -Recurse -File`）。对 exe 做字节扫描，`pre_tool_call` / `CONTEXT COMPACTION` / `DEFAULT_MAX_ITERATIONS` 命中数均为 0（zlib 压缩的 PYZ），**无法直接取串**（【实证】`_probe\probe_exe.py`）。 |
| 交叉还原手段 | ① `_internal\tools\*.py`（174 个工具模块，含 delegate/swarm/async/clarify/todo/approval/interrupt/context_engine/hook_output_spill/tool_search/session_search）；② exe 的 `--help` 文本；③ `hermes-home\config.yaml`；④ `hermes-home\.skills_prompt_snapshot.json`；⑤ `state.db` 表结构与内容（含 **16 份已装配 system prompt 全文**）；⑥ `logs\agent.log*`（4.7 MB 历史运行日志，含循环/压缩/评审/中断的 INFO 行）；⑦ 随包文档 `_internal\skills\autonomous-ai-agents\hermes-agent\references\*.md`；⑧ 上游 `main` 分支源码（仅作对照，标【推断】）。 |

**标注约定**：【实证】= 本机文件/命令输出/数据库内容可直接验证；【推断】= 由实证片段推理，或来自上游/文档与本机版本可能不同。

---

## 1. 核心循环（parent loop / child loop）

### 1.1 父循环与子循环的结构差异

| 维度 | 父循环（顶层 AIAgent） | 子循环（delegate/子 agent） | 证据 |
|---|---|---|---|
| 入口 | `AIAgent.run_conversation()` → 逐轮 tool-calling | 同一 `run_conversation`，但由 `_run_child_turn(child, goal, abort_signal)` 驱动 | 【实证】`tools/subagent_runner.py:12-99` 调 `_build_child_agent` + `_run_child_turn`；日志 `platform=subagent` 的 turn 行与父轮格式完全一致 |
| system prompt | 完整装配（见 §2，实测 10,196–44,034 字符） | 聚焦短 prompt：`_build_child_system_prompt()`，只含 TASK/CONTEXT/WORKSPACE PATH + 工作区上下文文件（`skip_soul=True`） | 【实证】`delegate_tool.py:1231-1334`；`build_context_files_prompt(cwd=..., skip_soul=True)`（`delegate_tool.py:1273-1288`） |
| 身份 | 载入 `SOUL.md` | **不载 SOUL**（"identity belongs to the parent"） | 【实证】同上注释 1266-1267 |
| 工具面 | 全会话 toolset | 父工具集的窄化副本，`DEFAULT_TOOLSETS = ["terminal","file","web"]`，且剔除 `DELEGATE_BLOCKED_TOOLS` / `delegation` 复合 toolset / `kanban`；父的 MCP toolset 会被补回 | 【实证】`delegate_tool.py:1186`、`_strip_blocked_tools()` 1364-1382、`_preserve_parent_mcp_toolsets()` 1140-1147 |
| 交互能力 | 可 clarify / memory / cronjob | **不能** clarify、memory、cronjob（orchestrator 模式下仍不能）；leaf 不能再 delegate | 【实证】schema 动态描述 `delegate_tool.py:5093-5104` |
| 迭代预算 | `agent.max_turns`（默认 90） | `delegation.max_iterations`（默认 50） | 【实证】日志 `budget=24/90`（desktop）、`budget=16/50`（subagent 到 `max_iterations_reached(50/50)`）；随包文档 `references/configuration.md:11,20` |
| 终端会话 | 独立 | 独立（"own conversation, terminal session, and toolset"） | 【实证】`delegate_tool.py:5107-5108` |
| 返回 | 用户可见回复 | 只回**最终 summary**（有字符预算，见 §4.4） | 【实证】`delegate_tool.py:1150-1162`、`_apply_summary_budget` |

### 1.2 迭代预算：三个数字的真实关系（重点）

| 常量/配置 | 数值 | 作用域 | 证据 |
|---|---|---|---|
| `DEFAULT_MAX_ITERATIONS` | **250** | `delegate_tool` 模块内的**兜底**默认值：仅当 `delegation.max_iterations` 未配置时用于子 agent | 【实证】`delegate_tool.py:1150`；`default_max_iter = cfg.get("max_iterations", DEFAULT_MAX_ITERATIONS)`（`:4010`） |
| `delegation.max_iterations` | **50** | 子 agent（含 orchestrator 的 worker）每 child 的预算 | 【实证】随包文档 `configuration.md:20`；`subagent_runner.py:16,154` 的签名默认 `max_iterations: int = 50`；日志出现 `budget=…/50` 与 `reason=max_iterations_reached(50/50)` |
| `agent.max_turns` | **90** | 父 agent 每轮对话的 API-call 预算 | 【实证】文档 `configuration.md:11`；日志 327 条 `budget=…/90`（desktop 主体） |
| 实测另一档 | **500** | 2026-10-12 起的 desktop 会话 | 【实证】`agent.log` `Turn ended: … budget=12/500`（`session=20261012_140602_3e04d4`, `platform=desktop`）——说明 desktop 入口显式传了更大的 `max_iterations` |
| 实测另一档 | **16** | 2026-08 的部分 desktop 会话 | 【实证】`agent.log.1` `budget=3/16`（122 条）——同一入口在早期版本/不同 surface 用 16 |

**结论（【推断】，高置信）**：`250` 只是「配置缺失时的兜底」，**不是运行值**；本机 `config.yaml` 没有 `delegation:` 段（【实证】全文 134 行，无 delegation 键），因此子 agent 实际跑 `delegation.max_iterations` 的**内置默认 50**（与 `subagent_runner.py` 签名默认一致），父 agent 跑 `agent.max_turns` 默认 90。三者是**三套独立预算**，不叠加换算；父 + 全部子的总量可远超父预算（源码注释明说："total iterations across parent + subagents can exceed the parent's max_iterations"，`delegate_tool.py:1946-1949`）。
**模型无法自减预算**：`delegate_task` 的 `max_iterations` 参数被显式忽略，以 config 为准（`delegate_tool.py:4011-4022`，【实证】）。

### 1.3 心跳 30 s + 双阈值级联中断

**同步路径（父线程内）** 【实证】`delegate_tool.py:1169-1185, 2740-2834`：

```
_HEARTBEAT_INTERVAL            = 30      # s，心跳周期
_HEARTBEAT_STALE_CYCLES_IDLE   = 15      # 15 × 30s = 450s   子 agent 在两轮之间无进展
_HEARTBEAT_STALE_CYCLES_IN_TOOL= 40      # 40 × 30s = 1200s  子 agent 卡在同一个 tool 里
DEFAULT_CHILD_TIMEOUT          = None    # 默认无墙钟上限（刻意取消）
```

- 心跳线程 `_heartbeat_loop()` 每 30 s 调 `parent_agent._touch_activity(desc)`，desc 形如 `delegate_task: subagent running terminal (iteration 7/50)`——**目的是让 gateway 的 inactivity 看门狗不误杀父会话**（注释 2740-2743）。
- 双阈值的判据是三元组 `(api_call_count, current_tool, last_activity_ts)` 是否**全部冻结**；任一推进即清零计数（`:2777-2793`）。
- 阈值按「是否在 tool 内」二选一：`stale_limit = IN_TOOL if child_tool else IDLE`（`:2800-2804`）。
- 超阈值动作 = **`break`，即停止续命**，把「杀不杀」交回 gateway 的 inactivity 超时（`:2805-2813`，注释 "let gateway timeout fire"）——这就是「级联中断」：**心跳级（stale 检测）不杀进程，只撤掉防误杀保护，让下一级（gateway idle timeout）杀**。
- 显式 `delegation.child_timeout_seconds` 可恢复硬墙钟（floor 30 s，0/负数=关闭，`:989-1016`）。

**异步路径（background=true）** 【实证】`tools/async_delegation.py:114-117`：

```
_STALE_CHECK_INTERVAL  = 30.0     # 监控线程扫描周期
_STALE_IDLE_SECONDS    = 450.0    # 无进展且无 current_tool → stalled
_STALE_IN_TOOL_SECONDS = 1200.0   # 无进展但在 tool 内 → stalled
_STALL_GRACE_SECONDS   = 120.0    # 中断后给 runner 收尾的宽限期
```
动作序列：`interrupt` → 等 `120 s` grace 让 runner 走 `finally` 正常 finalize（可交付部分结果）→ 仍未返回则强制落 `stalled` 终态事件（注释 96-108、`_finalize_stalled()` 1349）。**同样不做墙钟杀**，只做「无进展」杀。

**其它心跳常量（旁证同一设计语言）**：cron 任务运行期 `_CRON_RUN_HEARTBEAT_INTERVAL = 10.0`、`_CRON_RUN_HEARTBEAT_CEILING = 6*3600`（【实证】`cronjob_tools.py:28-37`）；`approval.py` 等待审批时也按短切片轮询并发心跳（`:4642, 4732`）。

### 1.4 中断时如何补发 tool_result 占位以保持消息配对合法

这是「无源码层」里最需要证据的一环。本机可确证的链条有四段：

1. **工具侧不抛异常、返回「中断结果」**：`tools/interrupt.py` 的模块 docstring 明确工具用法 `return {"output": "[interrupted]", "returncode": 130}`（【实证】`interrupt.py:11-14`）——即被中断的 tool 仍产出**一条正常的 tool result**，因此 `assistant(tool_calls)` ↔ `tool` 的配对天然成立。
2. **中断是 per-thread 的**，不跨会话误伤：`_interrupted_threads: set[int]` + `set_interrupt(active, thread_id, reason)` / `is_interrupted()`（【实证】`interrupt.py:37-102`）；`_ThreadAwareEventProxy` 为老调用点保留 `.set()/.clear()/.is_set()`（`:128-145`）。
3. **父被中断时，未完成的子任务被合成为「结果条目」而非丢弃**：`_execute_and_aggregate()` 在父 `_interrupt_requested` 时给仍在 pending 的 child 造一条 `{"status": "interrupted", "summary": None, "error": "Parent agent interrupted — child did not finish in time", "api_calls": 0, ...}`（【实证】`delegate_tool.py:4289-4327`）。这条 dict 会被渲染成 `delegate_task` 这一 tool_call 的**返回值**，配对因此闭合。
4. **运行期还有一层消息修补器**：日志出现 `agent.message_sanitization: Unrepairable tool_call arguments for <tool> — replaced with empty object (was: …)`（【实证】`agent.log` 2026-10-13/10-27，`errors.log` 多例）。说明 `agent/message_sanitization`（PYZ 内）会对畸形/被截断的 `tool_calls.arguments` 做修复，修不了就**替换为 `{}`**——同样是「宁可给出空对象，也不留下非法配对」。

**推断补全（【推断】）**：完整实现应在 `agent/message_sanitization.py` + `agent/conversation_loop` 内：遍历 messages，对每个 `tool_calls[i]` 检查是否存在同 `tool_call_id` 的 `tool` 消息，缺失则**补一条占位 tool 消息**（内容形如 `[interrupted]` / `[no result]`），并把「不可修复的 arguments」降级为 `{}`。上游 `hermes_cli/plugins.py` 的硬不变量注释也印证设计意图：*"Message role alternation — never two assistant or two user messages in a row; only `tool` results can repeat"*（【实证·随包文档】`skills/.../hermes-agent/SKILL.md:210`）。
**复刻要点**：中断/超时/预算耗尽三条路径都必须**产出 tool result**，不能靠「跳过」。本机日志的 `reason=interrupted_by_user`(13 次) / `interrupted_during_api_call`(20 次) 与 `max_iterations_reached`(3 次) 是三种不同终态（【实证】`agent.log.1` 统计）。

### 1.5 循环结束后触发的后台动作

| 动作 | 触发点 | 证据 |
|---|---|---|
| **背景评审（memory/自省）** | 独立线程 `bg-review`，轮结束后跑 | 【实证】`agent.log` `agent.background_review: Background review complete: thread=bg-review calls=6 in=30827 out=13718 cache_read=363264 result=memory`（2026-10-12）、`result=none`（多次）；有 `calls=0 in=0 out=0` 的空转样本 → 有「无事可做就不调用模型」的短路 |
| **学习回路（技能库更新）** | **在同一 session 内追加一轮 user turn**：`Review the conversation above and update the skill library. Be ACTIVE — most ses…` | 【实证】`agent.turn_context: conversation turn: session=<同 id> … history=59 msg='Review the conversation above and update the skill library…'`（2026-08-08 16:15:33，紧跟在 16:15:33,415 的 `Turn ended` 之后） |
| **curator（技能生命周期后台维护）** | 定时/事件驱动，独立于会话 | 【实证·随包文档】`references/background-systems.md:47-72`：`hermes curator` 全套动词、`~/.hermes/skills/.usage.json` 遥测（`use_count`/`view_count`/`patch_count`/`last_activity_at`/`state`/`pinned`）、只动 `created_by: agent` 的技能、**永不删除**（最多 archive）、`curator.consolidate` 默认 false 故常规后台零 token |
| **session finalize 钩子** | 轮/会话收尾 | 【实证】hook 名 `on_session_finalize` / `on_session_end` 出现在两个内置可观测性插件的 `plugin.yaml`（`plugins/observability/{langfuse,nemo_relay}/plugin.yaml`） |
| **verify 门（`agent.verify_on_stop`）** | 编辑过代码、准备收尾时 | 【推断】仅见于随包文档 `configuration.md:11`（`agent` 段列 `verify_on_stop`）；上游 `VALID_HOOKS` 有配套 `pre_verify` 事件与 `agent.max_verify_nudges` 上限 |

---

## 2. 上下文装配层（三层 prompt）

### 2.1 三层模型与实测装配顺序

**取证方式（最强证据）**：`state.db` 的 `system_prompts` 表按 sha256 内容寻址保存了 **16 份历史 system prompt 全文**（【实证】`select hash, length(prompt) from system_prompts` → 10,196 / 11,624 / 11,823 / 17,816 / 22,289 / 30,958 / 31,481 / 31,668 / 32,370 / 43,911 / 43,924×2 / 43,980 / 43,981 / 44,012 / 44,034 字符）。取最长一份（44,034 字符 / 615 行）逐段定位，得到**实际字节偏移**：

| # | 偏移 | 块 | 归属层（推断） | 内容 |
|---|---|---|---|---|
| 1 | **0** | `SOUL.md` 原文 | Stable | 本机 540 B（"You are Hermes Agent…Always respond in Chinese."）。**16/16 份 prompt 都以它开头**（`pr.startswith(soul[:120]) == True`，【实证】`_probe\probe_prompt.py`） |
| 2 | 541 | 运行时自述 + 文档指针（`hermes-agent` 技能提示） | Stable | 指向 `https://hermes-agent.nousresearch.com/docs` |
| 3 | ~700 | `# Finishing the job` / `# Parallel tool calls` / 记忆使用规范 / `## Skill Safety Rule` / `## Mid-turn user steering` / `# Tool-use enforcement` / `# Execution discipline`（含 `<tool_persistence>` `<mandatory_tool_use>` `<act_dont_ask>` `<prerequisite_checks>` `<verification>` XML 段） | Stable | 行为硬约束；`[SKILL_PRUNED]` 与 `[OUT-OF-BAND USER MESSAGE]` 两个协议的**权威定义就在这里**（第 16、19-23 行） |
| 4 | ~5,000 | 环境段（shell 是 bash/MSYS、路径翻译规则、PTY 回车规则）→ 编码代理协作段 → 工作区快照（Root/Branch/Status/Project/Verify/Context files）→ Python 工具链 → `Active Hermes profile` → `Managed Desktop scope` → desktop 表面能力（MEDIA: / ::preview{} / `data-hermes-send`） | Context | **随 cwd 与 surface 变化**，故属会话级 |
| 5 | **15,227** | `# Project Context` → `## AGENTS.md` → 文件全文 | Context | 见 §2.2 |
| 6 | **19,527** | `## Skills` → `<available_skills>`（**24,200** 起）→ 分类+技能名+一行描述 | Context | 见 §2.5 |
| 7 | **40,080** | `════ MEMORY (your personal notes) [99% — 2,193/2,200 chars] ════` + `§` 分隔条目 | Context（会话内冻结） | 见 §2.4 |
| 8 | **42,424** | `════ USER PROFILE (who the user is) [100% — 1,375/1,375 chars] ════` | Context（会话内冻结） | 同上 |
| 9 | **43,906** | `Conversation started: Friday, October 16, 2026 (中国标准时间, UTC+08:00)` / `Model:` / `Provider:` / `Platform:` | **Volatile** | 每次装配都会变的尾巴 |

**三层归属（【推断】）**：源码 `agent/prompt_builder.py` 在 PYZ 内，`Stable / Context / Volatile` 这三个名字无法直接取证（对 exe 扫描 `prompt_builder` 仅命中 1 次，无内容）。但**分层的存在与顺序是【实证】**：前缀 1–8 在会话内字节不变（记忆/画像自带「冻结快照」设计，见 §2.4），只有第 9 块每轮变；`prompt-size --help` 也把预算拆成 `system prompt total / skills index / memory / user profile / tool-schema JSON` 五档（【实证】`hermes prompt-size --help`）。**复刻建议**：把 1–3 定为 Stable（可跨会话前缀缓存）、4–8 定为 Context（会话级、只在 `/new` 或技能/记忆变更时重建）、9 定为 Volatile（每轮重算，放最尾以免破坏缓存前缀）。

**工具 schema 不在 system prompt 内**（【实证】）：44,034 字符里没有任何 tool schema；`prompt-size` 把 `tool-schema JSON` 单列；`tools/tool_search.py` 直接操作「model-visible tools array」。工具面是**独立的 tools 参数**，通过 `get_definitions()` 每次装配（`delegate_tool` 用 `dynamic_schema_overrides` 在每次 `get_definitions()` 时把并发/深度写进描述，【实证】`delegate_tool.py:5176-5195`）。

### 2.2 项目文件加载（AGENTS.md / HERMES.md）

| 文件 | 发现方式 | 优先级 |
|---|---|---|
| `.hermes.md` / `HERMES.md` | 从 cwd 向上走到 **git root** 停止 | 1（最高） |
| `AGENTS.md` / `agents.md` | **仅 cwd**（子目录与父目录副本被忽略） | 2 |
| `CLAUDE.md` / `claude.md` | 仅 cwd | 3 |
| `.cursorrules` / `.cursor/rules/*.mdc` | 仅 cwd | 4 |

- **first match wins，一次会话只装一个**（【实证·随包文档】`references/project-context-files.md:3-11`）。
- 每文件**上限 20,000 字符**，超长走 head+tail 截断并插 `[...truncated...]`（同上 `:20-22`）。
- 内容先过 **threat-pattern 扫描**，命中注入模式替换为 `[BLOCKED: ...]`（同上 `:24-26`；本机 `tools/threat_patterns.py:5` 注明与 `agent/prompt_builder.py`、`tools/memory_tool.py` 共用模式集）。
- 子 agent 复用**同一套发现/优先级/截断逻辑**（`build_context_files_prompt`），但 `skip_soul=True`（【实证】`delegate_tool.py:1261-1288`）。
- 本机实测：workspace `C:\Users\Administrator\.openclaw\workspace` 命中的是 `AGENTS.md`，被原样注入为 `# Project Context → ## AGENTS.md`（【实证】prompt 第 124-340 行；工作区快照行 `Context files: AGENTS.md`）。
- 关闭方式：`--ignore-rules`（跳过全部项目文件 **+ SOUL.md** + memory + 预载技能）、`--safe-mode`（再叠加忽略 user config / plugins / MCP）（【实证】`hermes --help`）。

### 2.3 SOUL.md

- 位置 `$HERMES_HOME/SOUL.md`，本机 **540 B**（【实证】`Get-Item SOUL.md` → Length 540）。
- **独立于项目文件、始终加载**，定位是「身份」而非「项目规则」（【实证·随包文档】`project-context-files.md:12`）。
- **是 system prompt 的第一块**（【实证】16/16 份 prompt 以它开头）。
- 本机内容尾部有一句用户定制 `Always respond in Chinese.`（【实证】），说明 SOUL.md 是用户可编辑的**身份注入点**，且直接决定语言行为——复刻时必须把它做成第一段、可写、参与前缀缓存。

### 2.4 记忆注入

- 两个独立块：`MEMORY (your personal notes)` 与 `USER PROFILE (who the user is)`，各有**硬字符预算**并显示占用百分比：本机 `2,193/2,200`（99%）与 `1,375/1,375`（100%）（【实证】prompt 第 546-610 行）。
- 条目用 `§` 分隔，单行陈述式（"User prefers concise responses"，禁止祈使句——prompt 第 13 行明确解释原因：祈使句会被后续会话重读成指令并压过用户当前请求）。
- **冻结快照模式**：`tools/memory_tool.py:23` "Frozen snapshot pattern: system prompt is stable, tool responses show live state"；`:178` "Never mutated mid-session. Keeps prefix cache stable."（【实证】）。即：**写入记忆不立即改 prompt**，靠 tool response 回显实时状态，避免破坏前缀缓存。
- 记忆块的 stable header 前缀由 `memory_tool.py:69` 的常量集合定义（【实证】）。
- 预算满时的策略：同一批里 replace/consolidate 旧条目，而不是跳过保存（【实证】prompt 第 13 行）。
- 关闭：`--ignore-rules` / `--safe-mode`；cron 会话默认 `skip_memory=True`（【实证·随包文档】`background-systems.md:41`）。

### 2.5 技能摘要注入

- 形态：`## Skills` → `<available_skills>` … `</available_skills>` → 一句 "Only proceed without loading a skill if genuinely none are relevant to the task."（【实证】prompt 第 343-544 行）。
- 内容 = **分类标题 + `分类描述` + 每个技能一行 `name: description`**，只给索引不给正文（渐进披露，正文靠 `skill_view`）。
- 本机规模：`<available_skills>` 块从偏移 24,200 到记忆块前的 40,080 → 约 **15.9 KB**；索引源 `.skills_prompt_snapshot.json` 含 **165 条技能**、17 条分类描述、文件本身 112,272 B（【实证】）。任务书给的 22,754 字符是 `prompt-size` 口径下的 skills index 预算（该口径含分类描述与缩进），本样本的渲染长度 ~15.9 KB 属同一量级、随技能集变化（【推断】差异因会话/技能集不同）。
- 缓存：`skill_manager_tool.py:2043` 调 `agent.prompt_builder.clear_skills_system_prompt_cache()`——技能变更时**显式失效 prompt 缓存**（【实证】），与「会话内不改 prompt」的硬不变量配套。
- 预载：`--skills/-s`（可重复或逗号分隔）把技能正文直接塞进上下文（【实证】`hermes --help`）。

### 2.6 `.skills_prompt_snapshot.json` 结构（python 只读解析）

```
version = 2
manifest : dict, 183 项，键=相对路径（`github\github-auth\SKILL.md`、`apple\DESCRIPTION.md`），值=2 元素列表（mtime/size 指纹，实测 len=2）
skills   : list, 165 项，每项 6 键
           { skill_name, category, frontmatter_name, description, platforms[], conditions{} }
           conditions = { fallback_for_toolsets[], requires_toolsets[],
                          fallback_for_tools[], requires_tools[], session_platforms[] }
category_descriptions : dict, 17 项（`mlops/inference` 这类二级分类也在内）
```
（【实证】`_probe\probe_snapshot.py` 输出）
**复刻要点**：`manifest` 是**增量/失效判定表**（键是文件相对路径，值是短指纹），`skills` 是**渲染数据**，`conditions` 是**条件可见性**（按 toolset/平台/会话类型过滤技能，所以同一台机器不同 surface 的索引不同）。

### 2.7 工具 schema 注入（与 §2.1 呼应）

- 独立 `tools` 数组，每次 `get_definitions()` 重建；支持 `dynamic_schema_overrides`（按 config 实时改写描述文本，如 `delegate_task` 的并发上限、深度）。
- 渐进披露：当存在 MCP/插件工具时，把它们换成 3 个桥接工具 `tool_search` / `tool_describe` / `tool_call`，核心工具**永不延迟**（`tools/tool_search.py:1-40`，【实证】）。
- 三级披露（Tier 0/1/2）：Tier1 用「名字+短描述」清单，预算 = `min(5% 上下文, listing_max_tokens)`，超预算降级为「仅名字」，再超则降级为「每 server 一行（server 名 + 工具数）」；`CHARS_PER_TOKEN = 4.0`，单次 `tool_search` 最多 10 条查询、`tool_describe` 最多 10 个名字（【实证】`tool_search.py:64-81, 234`）。
- 工具变更**只在 `/reset` 生效**，绝不中途改（【实证·随包文档】`configuration.md:59`）。

---

## 3. 压缩与谱系

### 3.1 触发阈值与产物

| 项 | 值/形态 | 证据 |
|---|---|---|
| 开关 | `compression.enabled` | 【实证·文档】`configuration.md:13` |
| 触发阈值 | `compression.threshold` **0.50**（占上下文窗口比例） | 【实证·文档】`configuration.md:13` |
| 目标比例 | `compression.target_ratio` **0.20** | 同上 |
| 实测行为 | `context compression started: messages=900 tokens=~529,109` → `done: messages=900->755 rough_tokens=~198,917`；另一例 `1193 msgs / ~503,392` → `907 msgs / ~190,571` | 【实证】`agent.log.1` 2026-08-29 两例；压缩后 ≈ 原 token 的 **37.6% / 37.9%** |
| 压缩用模型 | 走**辅助模型**（`agent.auxiliary_client`），可配 `auxiliary.compression`；失败重试 1/2、2/2 后换 provider（`auto (gpt-5.6-terra)` → fallback `deepseek-chat`） | 【实证】`agent.log.1` 同一时段的 `Auxiliary compression:` 行 |
| 产物前缀 | `[CONTEXT COMPACTION` / `[CONTEXT SUMMARY]:` | 【实证】`tools/session_search_tool.py:81-83` `_COMPACTION_PREFIXES`；`_is_compaction_summary()` 124-129 |
| 数据库标记 | `messages.compacted`、`messages.active`、`messages._compressed_summary`、`messages.api_content` | 【实证】`messages` 表 DDL |
| 会话级压缩健康度 | `sessions.compression_failure_cooldown_until` / `compression_failure_error` / `compression_fallback_streak` / `compression_ineffective_count` | 【实证】`sessions` 表 DDL |
| 并发互斥 | `compression_locks(session_id PK, holder, acquired_at, expires_at)` | 【实证】DDL（本机 0 行） |
| 技能正文被压缩掉时的占位 | `[SKILL_PRUNED]`，须用 `skill_view` 重载；重载后忽略同技能旧标记 | 【实证】prompt 第 15-16 行 `## Skill Safety Rule` |
| 可手动触发 | `compact` / `context_usage` 两个工具（toolset `context_engine`） | 【实证】`tools/context_engine_tools.py` 全文；`tool_executor` 识别 `function_name == "compact"` 后内联调 `_compress_context()`（`:50-57`） |

**压缩是「唯一允许改历史」的例外**（【实证·随包文档】`SKILL.md:209`："Never break prompt caching … The only exception is context compression"）。

### 3.2 父子 session 谱系

- `sessions.parent_session_id` 有 `FOREIGN KEY … REFERENCES sessions(id)`（【实证】DDL）。
- 本机 6 条父子关系，`end_reason='agent_close'`：3 条是 desktop 父会话派生的子会话（`api_call_count` 16/39/50），3 条是 subagent（`source='subagent'`，50/34/50）（【实证】`_probe\probe_state2.py`）。
- `end_reason` 分布：`startup_orphan_reap` 67、`NULL` 24、`agent_close` 6、`ws_orphan_reap` 3（【实证】）——说明**进程退出时会有孤儿回收**，谱系不保证闭合。
- 谱系语义分三类（【实证】`session_search_tool.py` 注释）：
  1. **压缩续接**（`in_place` 或从 `old_session_id` 轮转，摘要被带入新会话）；
  2. **`/new`·`/reset`·idle 过期**（`_FRESH_RESET_END_REASONS = _RESET_END_REASONS | {"new_session"}`）——共享 `parent_session_id` 但**前序内容不在活上下文里**；
  3. **活体委派子会话**（父仍在跑）。
  该工具据此决定「回溯会话历史时能不能看到父内容」。

### 3.3 system_prompts 内容寻址

```
CREATE TABLE system_prompts ( hash TEXT PRIMARY KEY, prompt TEXT NOT NULL )
```
- `hash` = prompt 全文的 sha256（本机 16 行，长度 10,196–44,034 字符）（【实证】）。
- `sessions.system_prompt` 仍保存一份内联副本，另有 `sessions.system_prompt_hash` 指向 `system_prompts`（【实证】DDL）——即**去重存储 + 可追溯**：同一份 prompt 被多个会话共享时只存一次，且能统计「哪版 prompt 在跑」。

---

## 4. 委派与并发

### 4.1 `delegate_task` 完整工具面

**Schema**（【实证】`delegate_tool.py:5198-5302`）：

| 参数 | 类型 | 说明 |
|---|---|---|
| `tasks[]` | array(minItems 1) | 唯一对外形态；每项 `{goal(必填), context, output_schema}` |
| `tasks[].output_schema` | object | 子 agent 最终答复须校验的 JSON Schema；父做**一次有界纠正重试**；结果带 `schema_valid`，失败带 `schema_errors` |
| `action` | enum `spawn\|list\|steer\|stop` | 默认 `spawn`；`list` 列 id/goal/status/transcript；`steer` 把纠偏文本**追加到子 agent 下一个 tool result 后**（不中断）；`stop` 提前结束并仍返回部分结果 |
| `subagent_id` / `message` | string | 配合 `steer`/`stop` |
| 隐藏（不对外） | — | 旧的单任务 `goal`/`context`/`output_schema`、per-task `role`、顶层 `background`、`acp_command`/`acp_args` 仍被 handler 接受（旧 transcript 兼容），**刻意不写进 schema** |

**子 agent 类型**：`leaf`（默认，不能再委派）vs `orchestrator`（可再派生，受 `max_spawn_depth` 约束）。**能力由深度推导，不由调用方声明**——`role` 参数已被废弃（【实证】`_build_role_param_description()` 5155-5173、`MAX_DEPTH = 1` 默认「扁平」）。

**并行度**：`delegation.max_concurrent_children`，代码常量 `_DEFAULT_MAX_CONCURRENT_CHILDREN = 10`（【实证】`delegate_tool.py:123`），env 回退 `DELEGATION_MAX_CONCURRENT_CHILDREN`，**下限 1、无上限**；>10 时打一次成本告警。⚠️ **版本冲突**：随包文档写默认 **3**（`configuration.md:20`、`background-systems.md:13`），源码注释也写 "default 3"（`:5260`），但常量是 10 → 以本机代码为准 **10**（【实证】），文档为旧值（【推断】）。
三道真实限流（【实证·随包文档】`delegate-task-concurrency-diagnosis.md`）：① 单次调用 `len(tasks) > max_children` 直接 `tool_error`；② `run_agent._cap_delegate_task_calls` 把同一 assistant 轮里多个 `delegate_task` 调用截断到上限；③ >10 的成本告警（**不截断**）。模型自称「被运行时限制」多数是自我收敛。

**后台化**：顶层 `delegate_task` **总是** background（模型无选择权，`_model_background_value()` 5310-5323），dispatch 立即返回并给 live transcript 路径，结果作为**一条合并消息**自己回到会话；例外是 orchestrator 子 agent（depth>0）需在自己轮内拿结果。`background` 参数已废弃。

**live log**（【实证】`tools/delegation_live_log.py:1-30`）：每个 child 一个 append-only 人类可读文件
`<hermes_home>/cache/delegation/live/<delegation_id>/task-<n>.log`，dispatch 时预建带表头（可立即 `tail -f`），逐事件一行（assistant 文本/thinking/tool 调用/tool 结果/生命周期标记），另有 `manifest.json`；截断常量 `_ASSISTANT_MAX=600`、`_THINKING_MAX=300`、`_ARGS_MAX=220`、`_RESULT_MAX=400`、`_KICKOFF_MAX=500`；保留 **7 天**（`LIVE_RETENTION_DAYS`），落在 `cache/` 是因为该目录被**只读挂载**进 Docker/Modal/SSH 后端。

**worktree 隔离**：`delegation.worktree_isolation`（bool，默认 **false**）；开启后每个 child 从父当前 commit 拉独立 git worktree；非 git 仓库或非本地终端后端时**静默忽略**（【实证】`_get_worktree_isolation()` 945-956）。

**摘要预算**（防「N 个子 agent 挤爆父窗口」）：`DEFAULT_MAX_SUMMARY_CHARS = 24000` 硬顶、`_SUMMARY_HEADROOM_FRACTION = 0.5`（整批摘要最多吃父剩余 headroom 的一半）、`_MIN_SUMMARY_CHARS = 2000` 地板（【实证】`:1151-1162`）。

**结果结构**：`status ∈ {completed, interrupted, failed}`、`exit_reason ∈ {completed, max_iterations, interrupted, error}`，且 `truncated ≡ (exit_reason == "max_iterations")`（【实证】`:2696-2712, 3384-3401`）。

### 4.2 `agent_swarm` / `swarm_scheduler` 调度模型

`agent_swarm` 工具（【实证】`tools/agent_swarm.py:13-64`）：模板化批量派生子 agent。
- 参数：`description`、`subagent_type`、`prompt_template`（**必须含 `{{item}}`**）、`items[]`（**maxItems 128**）、`resume_agent_ids`（`{agent_id: prompt}` 续跑失败任务）；`anyOf` 要求「items+prompt_template」或「resume_agent_ids」。
- 复用同一 runner：`subagent_runner.build_and_run_subagent()`（【实证】`subagent_runner.py:12-99`），因此与 `delegate_task` 共享凭据解析（`delegation.provider/base_url/model`）。

`swarm_scheduler` 调度模型（【实证】`tools/swarm_scheduler.py:1-37`）：

| 常量 | 值 | 语义 |
|---|---|---|
| `INITIAL_LAUNCH_LIMIT` | **5** | 突发阶段一次性最多同时起 5 个 |
| `INITIAL_LAUNCH_INTERVAL_MS` | **700** | 突发之后，正常阶段每 700 ms 起 1 个 |
| `RATE_LIMIT_RETRY_BASE_MS` | **3000** | 429 退避基数：`3s × 2^(retry-1)` → 3/6/12/24… |
| `RATE_LIMIT_CAPACITY_SHRINK_INTERVAL_MS` | **60000** | 容量（并发上限）**最多每 60 s 缩 1** |
| `CAPACITY_RECOVERY_MS` | **180000** | 静默 3 min 后容量 +1；静默 2×3 min 且已恢复满则彻底复位 |
| `GLOBAL_RETRY_INTERVAL_MS` | **1500** | 限流阶段全局最小启动间隔 |

识别 429 的方式：错误串含 `429` 或 `rate_limit`（`:315-316`）。

### 4.3 异步委派生命周期（`async_delegations` 表）

DDL（【实证】`async_delegation.py:155-192` + 本机 `state.db`）：

```
async_delegations(
  delegation_id PK, origin_session, origin_ui_session_id, parent_session_id,
  state, dispatched_at, completed_at, updated_at,
  event_json, result_json,
  delivery_state DEFAULT 'pending', delivery_attempts, delivered_at,
  owner_pid, owner_started_at, task_json,
  delivery_claim, delivery_claimed_at, origin_session_id )
```
生命周期常量（【实证】`:76-121`）：`_MAX_RETAINED_COMPLETED=50`、`_DURABLE_RETENTION_SECONDS=7d`、`_MAX_DURABLE_PENDING=1000`、`_MAX_DELIVERY_ATTEMPTS=8`（超限收敛为终态 `dropped`，不会无限重放）、`_MAX_COMPLETION_REPLAY_AGE_S=48h`（重启后超过 48 h 的 pending 完成事件**直接丢弃**，避免老会话重放）。

关键函数链（【实证】）：`dispatch_async_delegation` / `dispatch_async_delegation_batch` → `_persist_dispatch` → `_finalize` / `_begin_finalization` / `_finish_finalization` → `_push_completion_event`；恢复侧 `recover_abandoned_delegations()`、`restore_undelivered_completions()`；投递侧带 **claim/lease** 语义（`claim_completion_delivery` / `complete_completion_delivery` / `release_*` / `drop_*`），防多消费者重复投递。`_stale_monitor_loop()` 是唯一 watchdog（§1.3）。
**容量**：`_get_max_async_children()` 已**并入** `max_concurrent_children`（`max_async_children` 为废弃键，读到会告警）；满容量时**新异步派发直接拒绝**（不排队），调用方回退同步执行（【实证】`delegate_tool.py:962-986`）。

---

## 5. 人机协同

### 5.1 clarify（提问与等待）

`tools/clarify_tool.py`（【实证】）：
- `MAX_CHOICES = 4`（UI 恒追加第 5 项 "Other (type your answer)"）、`MAX_QUESTIONS = 5`（一次批量提问上限）。
- 单选（radio）/多选（checkbox）双模：`multi_select`。
- `TIMEOUT_RESPONSE` 是固定哨兵文案："The user did not provide a response within the time limit. Use your best judgement to make the choice and proceed."——**超时不是错误，而是授权自主决策**；批量模式下收到该哨兵会中止剩余问题，不逐个骚扰。
- `RECOMMENDED_LABEL = "(Recommended)"` 加在第一个选项上，四端统一渲染。
- 结构：`qid` 稳定线格式（`q0..qN`，按索引序）。

`tools/clarify_gateway.py`（【实证】）：gateway 侧阻塞原语。
- 模块级状态 + `threading.RLock`，`wait_for_response(clarify_id, timeout)` 用 **1 秒切片轮询** `Event.wait(slice)`，**每个切片都发 agent inactivity 心跳**，避免看门狗误杀活着的等待（`:110-116`）。
- `timeout <= 0` = 无限等待（用户还在想时绝不自动跳过）。
- 超时解析单一来源 `resolve_clarify_timeout(config)`：`clarify.timeout`（旧）→ `agent.clarify_timeout` → **3600 s**；旧默认 600 s 会把条目在想事情的中途逐出，导致晚点按钮点到死条目、agent 卡在 `running: clarify`（#32762）（`:531-574`）。
- 两条投递路径：按钮 UI（如 Telegram `InlineKeyboardMarkup`）/ 文本回退（编号列表 + 网关 `_handle_message` 拦截）。

### 5.2 审批门（approvals）

【实证】`tools/approval.py`：
- `_VALID_MODES = ("manual", "smart", "off")`，默认 **manual**；`mode: false` 归一为 `off`（`:3501-3517, 3536-3547`）。
- `_get_approval_timeout()` 默认 **300 s**，上限受 `_get_approval_timeout` 平台安全封顶（`:3579-3596, 2638-2643`）。
- `smart` 模式先用**辅助 LLM** 判（`:5159-5163, 5666`），判定结果可持久化为 allowlist；`approvals` CLI 子命令把历史挖成 allowlist 提案。
- 审批等待同样按短切片轮询并心跳（`:4642, 4732`）；并行 tool 批次会合并等待（`:4655`）；用户信号打断时明确落 "Approval wait interrupted by user signal"（`:4778`）。
- 子 agent 侧走**独立回调**：`delegation.subagent_auto_approve`（默认 **false** → `_subagent_auto_deny`；true → 自动 `once`），两者都打 `logger.warning` 审计（`delegate_tool.py:73-96`）。
- 本机 `config.yaml`：`approvals.mode: 'off'`（【实证】第 41-42 行）。

### 5.3 todo

【实证】`tools/todo_tool.py`：
- 单工具双语义：给 `todos` 就写，不给就读；**每次调用返回完整列表**。
- 状态集 `{pending, in_progress, completed, cancelled}`；单调递增 revision，UI 可拒绝过期更新。
- 状态挂在 `AIAgent` 实例（每会话一份），**上下文压缩后会被重新注入**（`format_for_injection`）——这是「压缩后不丢计划」的关键机制。
- 边界：`MAX_TODO_CONTENT_CHARS=4000`、`MAX_TODO_ITEMS=256`、`MAX_TODO_RESULT_CHARS=512_000`（防止伪造的超大 tool result 在历史重建时膨胀注入块）。
- 设计红线：**不改 system prompt、不改 tool response 结构**，行为指引全部写在 schema description 里。

### 5.4 tool_search（分级披露）

见 §2.7。补充：桥接工具与直调走**同一条** `model_tools.handle_function_call`，因此 guardrail、插件 pre/post hook、审批流、结果截断**行为完全一致**；显示层与 trajectory 存储做了 unwrap，用户看到的始终是真实工具而非 bridge（【实证】`tool_search.py:1-40`）。目录**无状态**、每次从当前 tool-defs 重建（避免会话键目录漂移导致工具静默消失）。

---

## 6. 入口与 UI 层

### 6.1 入口清单（10 个入口 + 2 个隐藏运行时入口）

| # | 入口 | 定位 | `--help` 关键选项 | 证据 |
|---|---|---|---|---|
| 1 | **TUI**（Ink） | 现代终端 UI，`display.interface: tui` 或 `--tui`；产物 `tui\dist\entry.js`（**3,728,190 B** 单文件 bundle） | `--tui` / `--cli`（强制经典 REPL）/ `--dev`（tsx 跑源码） | 【实证】`hermes --help`；`Get-ChildItem ui\dist` |
| 2 | **Web dashboard** | 管理面板 + 内嵌 chat，`web_dist` 单页应用 | `--port`(9119) / `--host`(127.0.0.1) / `--skip-build` / `--isolated` / `--stop` / `--status` / `--no-open` / 子命令 `register` | 【实证】`hermes dashboard --help` |
| 3 | **Desktop**（Electron，别名 `gui`） | 原生桌面 app；默认「装 node 依赖 → 构建本平台 unpacked → 启动」 | `--source` / `--build-only` / `--skip-build` / `--force-build` / `--hermes-root` / `--cwd` / `--fake-boot` / `--ignore-existing` / `--setup-tcc-identity`(mac) | 【实证】`hermes desktop --help` |
| 4 | **ACP server** | 给编辑器（VS Code/Zed/JetBrains）的 Agent Client Protocol 服务 | `--accept-hooks` / `--check` / `--setup` / `--setup-browser` / `--yes` / `--version` | 【实证】`hermes acp --help` |
| 5 | **`serve`（headless 后端）** | JSON-RPC/WebSocket 后端，desktop 与远程客户端连它；**永不打开浏览器** | `--port`(9119, 0=OS 分配) / `--host` / `--skip-build` / `--isolated` / `--stop` / `--status` / `--ssh-session-token-file` / `--ssh-owner-nonce`；`--insecure` 已废弃（公开 bind 强制鉴权） | 【实证】`hermes serve --help` |
| 6 | **CLI 交互模式** | 经典 prompt_toolkit REPL（默认）；`chat` 子命令 | `chat -q "..."` 单次；`-c/--continue`、`--resume <id\|latest>`、`--in DIR`、`--no-restore-cwd`、`--worktree` | 【实证】`hermes --help` |
| 7 | **oneshot `-z`** | 脚本/管道用：只把最终文本打到 stdout，**无 banner/无 spinner/无 tool 预览/无 session_id 行**；工具/记忆/rules/AGENTS.md 照常加载，审批自动绕过 | `-z/--oneshot PROMPT`、`--usage-file PATH`（跑完写 JSON 用量报告：估算成本/token/模型/api_calls，失败也写） | 【实证】`hermes --help` |
| 8 | **skin / 主题** | 皮肤管理：`list` / `use` / `set <key> <hex>`（原地改当前皮肤的一个颜色） | 无额外 flag | 【实证】`hermes skin --help`；`templates/skin.yaml` |
| 9 | **completion** | 打印 shell 补全脚本 | 位置参数 `bash\|zsh\|fish`（默认 bash） | 【实证】`hermes completion --help` |
| 10 | **console（安全命令台）** | **不是**裸 shell，也不暴露完整 CLI；只给一份策展过的命令 REPL | 无选项 | 【实证】`hermes console --help` |
| — | `__run-script` / `__slash-worker` / `__compute-host` / `__gateway-restart-watch` / `__update-gateway-helper` | 内部入口，`==SUPPRESS==`（不出现在常规帮助里） | — | 【实证】`hermes --help` 的 `==SUPPRESS==` 标注 |

其它 surface（同一核心）：**messaging gateway**（Telegram/Discord/Slack/WhatsApp/iMessage/Signal/Matrix/Teams/Email…）、**OpenAI 兼容 `proxy`**、**MCP server 模式**（`hermes mcp`）、**cron**、**kanban worker**（【实证·随包文档】`SKILL.md:23-27` + `--help` 子命令表）。

### 6.2 Web dashboard 页面清单（`_internal\hermes_cli\web_dist\assets`）

路由字面量（【实证】正则抓 `index-D5YdCED1.js` 的 `path:`）共 **19** 条：`*`、`/analytics`、`/channels`、`/chat`、`/config`、`/cron`、`/docs`、`/env`、`/files`、`/logs`、`/mcp`、`/models`、`/pairing`、`/plugins`、`/profiles`、`/sessions`、`/skills`、`/system`、`/webhooks`，另有两个非页面路径 `/api/ws`。

页面 chunk（每个路由一个懒加载 chunk，【实证】文件清单）：`AnalyticsPage`、`ChannelsPage`、`ChatPage`、`ConfigPage`、`CronPage`、`EnvPage`、`FilesPage`、`LogsPage`、`McpPage`、`ModelsPage`、`PairingPage`、`PluginsPage`、`ProfilesPage`、`SessionsPage`、`SkillsPage`、`SystemPage`、`WebhooksPage` + `DocsPage`（19 个页面 chunk）+ `ProfileBuilderPage`（**不作为独立路由**，由 Profiles 流程懒加载，见 `index-BMYA59-X.js` 的 chunk 预载清单）。

> 任务书列的 18 个页面 = 上述 19 减去 `/docs`（DocsPage 是内嵌文档页，非管理功能页）。**结论：管理页面 18 个 + 1 个文档页 + 1 个非路由的 ProfileBuilder 向导。**

技术栈旁证（【实证】资产名）：React + `react-vendor`、`xterm`（Web 终端，`xterm-CXxU4Y2B.js` 474 KB）、`i18n-XZPHmDjw.js`（477 KB，含中英双语，与文档站的 zh-Hans 对应）、`themes-5RlbmZz2.js`（皮肤）、`useProfileScope`（多 profile 作用域）。

### 6.3 入口与核心层的耦合点（复刻必读）

- 所有入口共用同一个 `AIAgent` + `run_conversation`，差异只在 ①system prompt 的 surface 段（desktop 段、coding 段、messaging 段不同）②`max_iterations` 预算（实测 desktop 90/500、subagent 50、cron 16/20）③审批通道（CLI stdin vs gateway per-session 队列 vs MCP elicitation）（【实证】prompt 第 122 行 desktop 段 + 日志 budget 分布 + `approval.py` 注释）。
- `-z` 与 `console` 是**安全闸门不同的两个极端**：`-z` 自动绕过审批（为脚本），`console` 只暴露策展命令（为人类防误操作）。

---

## 7. 可观测性层

### 7.1 logs（文件/格式/轮转）

【实证】`hermes-home\logs\` 实测：

| 文件 | 大小 | 用途 |
|---|---|---|
| `agent.log` | 2,381,815 B | 主日志（agent/tools/cli/cron/gui 混写） |
| `agent.log.1` | 5,243,008 B | **轮转归档**（存在 `.1` 即证明按大小轮转，单文件约 5 MB 上限） |
| `errors.log` | 984,686 B | WARNING 及以上副本 |
| `gateway.log` | 228,926 B | gateway |
| `gui.log` | 51,268 B | TUI/desktop 前端 |
| `desktop-gateway-restart.log` / `gateway-exit-diag.log` / `gateway-restart.log` | 62,159 / 13,302 / 3,452 B | 生命周期诊断 |
| `.__agent.lock` 等 4 个 0 字节文件 | — | 写入互斥锁 |

行格式（【实证】）：
```
2026-09-06 09:13:53,295 INFO [20260906_084509_01c508] agent.conversation_loop: API call #148: model=… provider=… in=173828 out=87 total=173915 latency=1.4s cache=173696/173828 (100%)
```
= `时间 级别 [session_id] 模块名: 消息 key=value…`。**模块名即组件名**：`agent.conversation_loop`、`agent.tool_executor`、`agent.turn_context`、`agent.conversation_compression`、`agent.background_review`、`agent.message_sanitization`、`agent.auxiliary_client`、`agent.stream_diag`、`tools.registry`、`tools.terminal_tool`、`cron.scheduler`、`tui_gateway.server`、`run_agent`、`hermes_cli.model_catalog`。

`hermes logs` 的查询面（【实证】`logs --help`）：`log_name ∈ {agent, errors, gateway, gui, desktop, list}`；`-n/--lines`(50)、`-f/--follow`、`--level`、`--session <substr>`、`--since 1h|30m|2d`、`--component {gateway,agent,tools,cli,cron,gui}`。

### 7.2 insights（用量分析口径）

【实证】`hermes insights --help`：*"Analyze session history to show **token usage, costs, tool patterns, and activity trends**"*；`--days`（默认 **30**）、`--source`（按平台过滤 cli/telegram/discord…）。
数据源即 `state.db`（【实证】DDL）：`sessions` 上 `input_tokens/output_tokens/cache_read_tokens/cache_write_tokens/reasoning_tokens/api_call_count/tool_call_count/estimated_cost_usd/actual_cost_usd/cost_status/cost_source/pricing_version` + 独立表 `session_model_usage`（本机 **115 行**）。口径要点：**成本分 estimated vs actual 两条腿**，并记录计价版本，便于回溯定价变更。

### 7.3 monitoring（health / diagnostics）

【实证】`hermes monitoring --help`：子命令只有 `status`；定位是 *"Gateway monitoring: service health metrics plus redacted diagnostics, exported over **OTLP** to an operator-configured endpoint. **Content-free by construction** — no prompts, messages, tool args/results, or usage analytics."* 配置在 `monitoring.*`。即：**与 insights 严格分工**——monitoring 只出健康指标 + 脱敏诊断，绝不带内容/用量。
旁证表：`gateway_heartbeats`、`gateway_hygiene_state`、`gateway_routing`、`delivery_obligations`、`session_turn_leases`（【实证】`state.db` 表清单）。

### 7.4 observability 插件 exporter

【实证】`_internal\plugins\observability\`：

| 插件 | 版本 | 依赖 env | 订阅的 hook（manifest 声明） |
|---|---|---|---|
| `langfuse` | 1.0.0 | `HERMES_LANGFUSE_PUBLIC_KEY`、`HERMES_LANGFUSE_SECRET_KEY` | `pre_api_request`、`post_api_request`、`api_request_error`、`pre_llm_call`、`post_llm_call`、`pre_tool_call`、`post_tool_call`、`on_session_finalize`、`on_session_end`、`subagent_start`、`subagent_stop`（11） |
| `nemo_relay` | 0.1.0 | `HERMES_NEMO_RELAY_*` | 上述 11 + `on_session_start`、`on_session_reset`、`pre_approval_request`、`post_approval_response`（15） |

两者都需 `hermes plugins enable observability/<name>` 显式开启（opt-in，默认不加载）。`nemo_relay` 同时是随包 pip 依赖（`_internal\nemo_relay\`、`nemo_relay-0.7.2.dist-info`）。

### 7.5 shared_metrics schema v1 vs v2

【实证】`_internal\hermes_cli\observability\schemas\hermes.shared_metrics.v{1,2}.schema.json`（7,676 B / 16,104 B）：

共同顶层（两者一致，`required` 全 8 项）：
```
schema_version, package_id, install_id, period_start, period_end, generated_at, resource, metrics
```
| | v1 | v2 |
|---|---|---|
| `$id` | `urn:hermes-agent:schema:shared-metrics:v1` | `…:v2` |
| `resource.properties` | 仅 `hermes_version` | `architecture`、`hermes_version`、`install_method`、`os_family` |
| `$defs` 指标族 | `uuid`、`model_call_counter`、`task_started_counter`、`task_finished_counter`、`execution_surface`、`task_entrypoint`、`duration_bucket`、`count_bucket`（8） | 上述 8 + `client_active_counter`、`model_route_counter`、`tool_call_counter`、`tool_approval_counter`、`tool_latency_bucket`、`tool_retry_bucket`、`skill_lifecycle_counter`、`skill_load_counter`、`skill_provenance`（17） |
| 计数器形状 | 每个 `*_counter` 固定 `{name, type, dimensions, value}` 四键全 required | 同左 |

**v2 的增量语义**：①环境维度补齐（架构/安装方式/OS 族，便于分群）；②从「任务级」下探到「工具级」（调用/审批/延迟桶/重试桶）；③新增技能生命周期与来源追踪（`skill_lifecycle_counter`/`skill_load_counter`/`skill_provenance`）；④新增 `client_active_counter`（客户端活跃）与 `model_route_counter`（路由到哪个 provider/model）。`install_id` 明示 *"Random persistent identifier scoped to one HERMES_HOME; local-only"*。

### 7.6 `hermes status` / `hermes dump` 输出什么

**只跑了 `--help`**（未执行），故为【推断】+ 帮助文本实证：
- `status`：*"Display status of Hermes Agent components"*；`--all` 显示全部细节（已脱敏，可用于分享）、`--deep` 跑较慢的深检查。dashboard 的 `/api/status`、`/api/system-stats`、`/api/memory`、`/api/credential-pool`、`/api/checkpoints`、`/api/hooks`、`/api/curator`、`/api/portal` 与它同源（【实证】`SystemPage` chunk 的 `Promise.allSettled([...])` 调用列表）→ **status 覆盖面 = 组件健康 + 系统指标 + 记忆 + 凭证池 + checkpoint + hooks + curator + portal + 版本更新检查**。
- `dump`：*"compact, plain-text summary of your Hermes setup … copy-pasted into Discord/GitHub"*；`--show-keys` 只显示 key 的**首/尾 4 字符**（默认只报 set/not set）。

### 7.7 核心循环可观测性：日志里能直接量到的东西

复刻时这些字段必须打（全部来自本机实证行）：
- `agent.turn_context: conversation turn: session=… model=… provider=… platform=… history=<N> msg='<截断>'` → 每轮起手式（`platform` 取值实测 `desktop` / `feishu` / `subagent`）。
- `agent.conversation_loop: API call #<n>: model=… provider=… in=… out=… total=… latency=…s cache=<hit>/<total> (<pct>%)` → 每次模型调用。
- `agent.conversation_loop: Turn ended: reason=<text_response|interrupted_by_user|interrupted_during_api_call|max_iterations_reached> model=… api_calls=<n>/<budget> budget=<n>/<budget> tool_turns=<n> last_msg_role=… response_len=… session=…` → **轮终态 + 预算消耗**（最重要的一个信号）。
- `agent.tool_executor: tool <name> completed (<s>, <chars> chars)` / `returned error (<s>): {…}` → 工具级。
- `agent.conversation_compression: context compression started/done: messages=A->B tokens/rough_tokens=…` → 压缩。
- `agent.background_review: Background review complete: thread=bg-review calls=… in=… out=… cache_read=… result=<memory|none>` → 后台评审。
- `agent.message_sanitization: Unrepairable tool_call arguments for <tool> — replaced with empty object` → 配对/参数修补。
- `tools.registry: check_fn <fn> returned False; dependent tools will be unavailable this turn` → 工具可用性动态门。

---

## 8. hooks 层

### 8.1 四套并存的 hook 系统（先分清）

| 系统 | 注册方式 | 运行范围 | 证据 |
|---|---|---|---|
| **Gateway hooks** | `$HERMES_HOME/hooks/<name>/{HOOK.yaml,handler.py}` | 仅 gateway；**trusted-by-placement**（放进去就是 opt-in，无 enable 列表，`HERMES_SAFE_MODE` 也不跳过） | 【实证·官方文档】`/docs/user-guide/features/hooks` |
| **Plugin hooks** | 插件内 `ctx.register_hook()` | CLI + gateway + desktop/TUI | 【实证】`plugins/observability/*/plugin.yaml` 的 `hooks:` 列表 |
| **Shell hooks** | `config.yaml` 的 `hooks:` 块 → shell 脚本 | CLI + gateway + Desktop/TUI/dashboard chat | 【实证】`hermes hooks --help` + 上游 `agent/shell_hooks.py` |
| **Outbound webhooks** | `config.yaml` 的 `hooks.outbound:` | CLI + gateway；签名后推外部 HTTP | 【实证】上游 `shell_hooks.py` 的 `_forget_home_registrations` 注释 + 文档 |

**本机现状**：`hermes-home\hooks\` **为空目录**（【实证】`Get-ChildItem hooks -Recurse` 无输出）；`config.yaml` 无 `hooks:` 段；`shell-hooks-allowlist.json` **不存在** → 当前零 hook 生效。

### 8.2 `hermes hooks` CLI 面

【实证】`hermes hooks --help`：
```
Inspect shell-script hooks declared in ~/.hermes/config.yaml, test them against
synthetic payloads, and manage the first-use consent allowlist at
~/.hermes/shell-hooks-allowlist.json.
  list (ls)    List configured hooks with matcher, timeout, and consent status
  test         Fire every hook matching <event> against a synthetic payload
  revoke (remove, rm)  Remove a command's allowlist entries (takes effect on next restart)
  doctor       Check each configured hook: exec bit, allowlist, mtime drift,
               JSON validity, and synthetic run timing
```

### 8.3 hook 事件清单

**本机可证的事件（15 个）**——由内置插件 manifest 与源码调用点并集得出（【实证】）：

| # | 事件 | 语义 | 本机证据 |
|---|---|---|---|
| 1 | `pre_tool_call` | 工具执行前；**唯一可 block 的 shell hook 事件** | 两个 observability `plugin.yaml` |
| 2 | `post_tool_call` | 工具执行后 | 同上 |
| 3 | `pre_llm_call` | 模型调用前（可返回 `{"context": …}` 注入） | 同上 + `hook_output_spill.py` 文档 |
| 4 | `post_llm_call` | 模型调用后 | 同上 |
| 5 | `pre_api_request` | 每次 HTTP 请求前 | 同上 |
| 6 | `post_api_request` | 每次 HTTP 请求后 | 同上 |
| 7 | `api_request_error` | 请求失败 | 同上 |
| 8 | `on_session_start` | 会话开始 | `nemo_relay/plugin.yaml` |
| 9 | `on_session_end` | 会话结束 | 两个 manifest |
| 10 | `on_session_finalize` | 会话收尾（终局化） | 两个 manifest |
| 11 | `on_session_reset` | `/new`、`/reset` | `nemo_relay/plugin.yaml` |
| 12 | `pre_approval_request` | 审批弹窗发出前 | `approval.py:172, 4100, 4466, 4563, 4704, 5443` |
| 13 | `post_approval_response` | 审批有结论后 | `approval.py:181, 4111, 4497, 4616, 4720, 4803` |
| 14 | `subagent_start` | 子 agent 起 | `delegate_tool.py:2283-2294` |
| 15 | `subagent_stop` | 子 agent 止（携带 `tool_call_history`） | `delegate_tool.py:3778-3792` |

**关于「17 个」**：`VALID_HOOKS` 定义在 PYZ 内的 `hermes_cli/plugins.py`，**本机无法读取**（exe 字节扫描 0 命中）。上游 `main` 分支的 `VALID_HOOKS` 已有约 60 个事件（【实证·上游对照】`gh-proxy` 取 `hermes_cli/plugins.py`），因此 CN 0.21.0-cn.18 的集合介于两者之间。**最可能补齐到 17 的两个候选**（【推断】）：`post_compaction`（每次提交压缩后，观察者）与 `pre_verify`（编辑代码后、准备收尾时，可返回 `{"action":"continue","message"}` 续跑，受 `agent.max_verify_nudges` 限制）——两者都在上游注释里与 `agent.max_turns`/压缩同段描述，属于核心循环事件，且本机 `sessions.compression_*` 字段与 `verify_on_stop` 配置证明这两个子系统存在。
**结论**：报告「本机可证 15 个 + 上游候选 2 个 = 17」为**推断**；如需硬证，唯一合法途径是在有源码的环境读 `hermes_cli/plugins.py::VALID_HOOKS`。

### 8.4 shell 脚本 hook 契约（可直接照抄实现）

【实证·上游源码】`agent/shell_hooks.py`（与本机 `hook_output_spill.py` 注释互相印证）：

- **stdin（JSON，wire 顺序即字段顺序）**：
  ```json
  {"hook_event_name": "<event>", "tool_name": "...", "tool_input": {...},
   "session_id": "...", "cwd": "...", "profile": "<active profile>", "extra": {...}}
  ```
  `tool_name/args/session_id/parent_session_id` 会被提升为顶层键，其余全部落进 `extra`。
- **stdout（可选 JSON）**：
  - `pre_tool_call`：`{"action": "block", "message": "…"}` / `{"action": "modify", "args": {...}}` / `{"action": "approve", "message": "...", "rule_key": "..."}`（Hermes 专有：把该次调用**升级到人工审批门**）；兼容 Claude-Code 方言 `{"decision": "block"|"modify", "reason", "tool_input"}`。
  - `pre_verify`：`{"action": "continue", "message": "…"}`（或 `{"decision":"block","reason":…}`）。
  - 其它事件：`{"context": "..."}` → 文本被拼进**当轮 user 消息**，并在后续每次 API 调用重复出现（因此有 spill 机制，见下）。
- **退出码语义**：`BLOCK_EXIT_CODE = 2` → **即使不输出 JSON 也 block**（Claude-Code/Cursor 兼容）；其它非 0 → 告警后仍尝试解析 stdout。
- **失败语义**：spawn 错误/超时 **fail-open**（放行）除非 `fail_closed: true`；`fail_closed` 仅对 `pre_tool_call` 生效；`fail_closed` 且 stdout 是不可解析对象 → **block**（不静默放行）。
- **配置项**：`{event: [{command, matcher?, timeout?, fail_closed?}]}`；`matcher` 是正则、**仅对 `pre_tool_call`/`post_tool_call` 生效**（对其它事件配了会告警并忽略）；`timeout` 默认 **60 s**、上限 **300 s**；`hooks:` 下有两个保留子段 `output_spill` 与 `outbound`（不是事件）。
- **Windows 特殊处理**：`.sh/.bash → bash`、`.py → python` 自动加解释器前缀（CreateProcess 无 shebang，否则 WinError 193）。
- **输出溢出（output_spill）**：`hooks.output_spill.{enabled,max_chars,preview_head,preview_tail,directory}`，默认 `enabled: true`、`max_chars: 10000`、head/tail 各 `500`、目录 `<HERMES_HOME>/hook_outputs`；超限则落盘 + 只留 head/tail 预览 + 路径，防「一次注入把前缀缓存顶爆」（【实证】`tools/hook_output_spill.py:1-55`）。
- **`SHELL_UNSUPPORTED_HOOKS = {"transform_api_error_classification"}`**：shell 侧无返回通道，注册时**显式拒绝并告警**，而不是静默忽略（【实证·上游】）。

### 8.5 `--accept-hooks` 的含义

【实证】`hermes --help` 原文：*"Auto-approve any unseen shell hooks declared in config.yaml without a TTY prompt. Equivalent to `HERMES_ACCEPT_HOOKS=1` or `hooks_auto_accept: true` in config.yaml. Use on CI / headless runs that can't prompt."*
机制（【实证】上游 `shell_hooks.py`）：
- shell hook 有**首次使用同意（first-use consent）**：每个 `(event, command)` 对必须在 `$HERMES_HOME/shell-hooks-allowlist.json` 的 `approvals[]` 里，否则**跳过并告警**（不是执行）。
- 同意三条通道：① 前台 CLI 的 TTY 交互确认（`_prompt_and_record`）；② `--accept-hooks` / `HERMES_ACCEPT_HOOKS=1` / `hooks_auto_accept: true` 一次性全量接受并**写入 allowlist**；③ 若写盘失败，该次启动的同意**只留在内存**（`_launch_consent`）。
- allowlist 条目带 `approved_at` 与 `script_mtime_at_approval` → **脚本 mtime 漂移会被 `hermes hooks doctor` 检出**（改了脚本要重新同意）。
- gateway 侧**绝不弹 TTY 提示**（`isatty()` 分不清隐藏控制台与真人，弹窗会阻塞事件循环直到看门狗杀网关，见 #120356/#127822），只能靠 allowlist/opt-in；`sync_from_config` 约每 30 s 对齐一次，**撤销（revoke）在网关下一次同步时生效，其它活进程要重启**。
- `HERMES_SAFE_MODE` / safe-worker 下**零 shell hook 注册**（"hooks are user customizations too"）。

---

## 9. Orbit 核心层必须补齐的 top 6（结论）

| 优先级 | 必须补齐的机制 | 为什么它是「核心层」的命门 | 本机证据锚点 |
|---|---|---|---|
| **1** | **消息配对合法化 + 中断占位**（tool_result 补发、arguments 降级为 `{}`、role 交替不变量） | 所有中断/超时/预算耗尽路径的唯一安全出口；缺了它，长任务一中断就 400 | `interrupt.py:11-14`；`message_sanitization` 日志；`delegate_tool.py:4289-4327` |
| **2** | **三档独立预算 + 终态枚举**（`agent.max_turns` / `delegation.max_iterations` / `child_timeout`，`reason` 与 `exit_reason` 双枚举，`truncated ≡ max_iterations`） | 决定「什么时候停、停了算成功还是截断」，是编排正确性的根 | `delegate_tool.py:1150, 4010-4022, 2696-2712`；日志 `Turn ended: reason=…` |
| **3** | **心跳 + 双阈值级联中断**（30 s 心跳、450 s idle / 1200 s in-tool、stale 只撤保护不杀、120 s grace） | 让「慢但活着」不被误杀、「卡死」能被上层杀；子 agent 长任务的可用性全靠它 | `delegate_tool.py:1169-1185, 2755-2833`；`async_delegation.py:114-117` |
| **4** | **三层 prompt 装配 + 前缀缓存不变量**（SOUL 首块、项目文件 first-match、记忆冻结快照、技能索引可失效重建、Volatile 尾巴置末） | 直接决定 token 成本与「会话中途不许改 prompt」这条硬不变量能否落地 | 16 份 `system_prompts` 实测偏移；`memory_tool.py:23,178`；`skill_manager_tool.py:2043` |
| **5** | **压缩 + 谱系 + 内容寻址**（0.50 触发 / 0.20 目标、`[CONTEXT COMPACTION` 前缀、`parent_session_id` 三态语义、`system_prompts.hash`、`compression_locks`、todo 压缩后重注入） | 长会话唯一的「可继续」机制；谱系语义错判会让 `session_search` 把「重置」当「续接」 | `session_search_tool.py:81-129`；`configuration.md:13`；`state.db` DDL |
| **6** | **委派生命周期 + 交付账本**（`async_delegations` 的 state/delivery 双状态机、claim/lease、8 次投递上限、48 h 重放上限、live transcript、摘要 headroom 预算） | 「后台跑完自己回到会话」是 Hermes 最像产品的能力，也是最容易漏消息/重复投递的地方 | `async_delegation.py:76-192, 343-600`；`delegation_live_log.py`；`delegate_tool.py:1151-1162` |

**次优先（第 7–10）**：人机协同三件套（clarify 3600 s 无限等待 + 超时即授权、审批 manual/smart/off 与 300 s、todo 压缩后重注入）；工具渐进披露（Tier0/1/2 + 5% 预算）；hooks 首次同意 + 双通道 stdin/stdout + spill；可观测性三件套（`Turn ended` 终态行 / `API call #n` 用量行 / `Background review` 行）。

---

## 附录 A：证据索引（文件 → 关键符号）

| 文件（相对 `_internal\`） | 关键符号/行 |
|---|---|
| `tools/delegate_tool.py` | `DEFAULT_MAX_ITERATIONS=250`(1150)、`DEFAULT_MAX_SUMMARY_CHARS=24000`(1154)、`_SUMMARY_HEADROOM_FRACTION=0.5`(1159)、`DEFAULT_CHILD_TIMEOUT=None`(1168)、`_HEARTBEAT_INTERVAL=30`(1169)、`_HEARTBEAT_STALE_CYCLES_IDLE=15`(1184)、`_HEARTBEAT_STALE_CYCLES_IN_TOOL=40`(1185)、`DEFAULT_TOOLSETS`(1186)、`_DEFAULT_MAX_CONCURRENT_CHILDREN=10`(123)、`MAX_DEPTH=1`(130)、`_build_child_system_prompt`(1231)、`_strip_blocked_tools`(1364)、`_get_max_concurrent_children`(904)、`_get_worktree_isolation`(945)、`_get_max_spawn_depth`(1031)、`_heartbeat_loop`(2755)、`_execute_and_aggregate`(4229)、`DELEGATE_TASK_SCHEMA`(5198)、`_build_top_level_description`(5069) |
| `tools/async_delegation.py` | `_DEFAULT_MAX_ASYNC_CHILDREN=3`(76)、`_MAX_RETAINED_COMPLETED=50`(78)、`_DURABLE_RETENTION_SECONDS`(79)、`_MAX_DELIVERY_ATTEMPTS=8`(85)、`_MAX_COMPLETION_REPLAY_AGE_S=48h`(90)、`_STALE_CHECK_INTERVAL=30`(114)、`_STALE_IDLE_SECONDS=450`(115)、`_STALE_IN_TOOL_SECONDS=1200`(116)、`_STALL_GRACE_SECONDS=120`(117)、`async_delegations` DDL(155) |
| `tools/swarm_scheduler.py` | `INITIAL_LAUNCH_LIMIT=5`、`INITIAL_LAUNCH_INTERVAL_MS=700`、`RATE_LIMIT_RETRY_BASE_MS=3000`、`RATE_LIMIT_CAPACITY_SHRINK_INTERVAL_MS=60000`、`CAPACITY_RECOVERY_MS=180000`、`GLOBAL_RETRY_INTERVAL_MS=1500`（32-37） |
| `tools/agent_swarm.py` | `AGENT_SWARM_SCHEMA`(13-64，`maxItems:128`)、`create_agent_swarm_specs`(83)、`agent_swarm_handler`(251) |
| `tools/subagent_runner.py` | `build_and_run_subagent(max_iterations=50)`(12-16)、`resume_subagent`(102) |
| `tools/delegation_live_log.py` | `LIVE_RETENTION_DAYS=7`(44)、截断常量(49-58)、`LiveTranscriptWriter`(112) |
| `tools/interrupt.py` | 全文 145 行；`set_interrupt`(42)、`is_interrupted`(76)、`_ThreadAwareEventProxy`(128) |
| `tools/clarify_tool.py` | `MAX_CHOICES=4`、`MAX_QUESTIONS=5`、`TIMEOUT_RESPONSE`、`RECOMMENDED_LABEL`（20-38） |
| `tools/clarify_gateway.py` | `wait_for_response`(107)、`resolve_clarify_timeout`(531，默认 3600) |
| `tools/approval.py` | `_VALID_MODES`(3501)、`_get_approval_config`(3536)、`_get_approval_timeout`(3579，默认 300)、`_fire_approval_hook`(115-133) |
| `tools/todo_tool.py` | `VALID_STATUSES`、`MAX_TODO_CONTENT_CHARS=4000`、`MAX_TODO_ITEMS=256`、`MAX_TODO_RESULT_CHARS=512000`（26-42） |
| `tools/tool_search.py` | `TOOL_SEARCH_NAME` 等(64-68)、`CHARS_PER_TOKEN=4.0`(76)、`_MAX_QUERIES_PER_CALL=10`(79)、`_DIRECT_SURFACE_TOOLSETS`(234) |
| `tools/session_search_tool.py` | `_COMPACTION_PREFIXES`(81)、`_is_compaction_summary`(124)、`_FRESH_RESET_END_REASONS` |
| `tools/hook_output_spill.py` | `DEFAULT_MAX_CHARS=10_000`、`DEFAULT_PREVIEW_HEAD/TAIL=500`、`DEFAULT_ENABLED=True`（49-52） |
| `tools/memory_tool.py` | "Frozen snapshot pattern"(23)、stable header 前缀(69)、"Never mutated mid-session"(178) |
| `tools/context_engine_tools.py` | `context_usage`/`compact` 注册(92-108) |
| `plugins/observability/{langfuse,nemo_relay}/plugin.yaml` | 事件订阅清单 |
| `skills/autonomous-ai-agents/hermes-agent/references/*.md` | `configuration.md`(阈值/预算默认值)、`project-context-files.md`(项目文件优先级/20k 截断/威胁扫描)、`background-systems.md`(委派/cron/curator)、`delegate-task-concurrency-diagnosis.md`(三道限流) |
| `hermes_cli/observability/schemas/hermes.shared_metrics.v{1,2}.schema.json` | 顶层 8 键 + `$defs` 指标族 |
| `hermes-home/state.db` | `sessions`、`messages`、`system_prompts`、`compression_locks`、`async_delegations`、`session_model_usage`、`gateway_heartbeats`… 共 40 个对象 |
| `hermes-home/logs/agent.log(.1)` | `Turn ended: reason=…`、`API call #n`、`context compression started/done`、`Background review complete`、`message_sanitization` |

## 附录 B：本次使用的探针脚本（可复跑，均只读）

`E:\bbbooottt\handoff\hermes-parity\research\_probe\`
- `probe_state.py` / `probe_state2.py` — `state.db` 表清单、DDL、行数、`system_prompts` 样本、谱系与 `end_reason` 分布
- `probe_snapshot.py` — `.skills_prompt_snapshot.json` 顶层键与条目结构
- `probe_prompt.py` — 抽取最长 system prompt → `system_prompt_sample.txt`，并校验 16/16 以 SOUL.md 开头
- `probe_outline.py` — 生成 `outline.txt`（段落标记行号 + 关键块字符偏移）
- `probe_schemas.py` — v1/v2 指标 schema 结构对比 → `schemas.txt`
- `probe_exe.py` — exe 字节扫描（证明 PYZ 压缩、无法直接取串）

## 附录 C：本次未能取证的点（诚实清单）

| 点 | 原因 | 合法补证方式 |
|---|---|---|
| `VALID_HOOKS` 精确 17 项 | 在 PYZ 内；exe 扫描 0 命中；`hermes hooks list` 属非 `--help` 子命令，按安全规则未执行 | 在有源码的环境读 `hermes_cli/plugins.py::VALID_HOOKS`，或 `hermes hooks list`（只读，但本任务禁止） |
| `agent/prompt_builder.py` 的 `Stable/Context/Volatile` 三段函数名与切分点 | 同上 | 同上；本报告用**实测字节偏移**替代（已足够复刻顺序） |
| `status` / `dump` / `prompt-size --json` 的真实输出 | 只允许 `--help` | 允许执行只读子命令时再取；dashboard `/api/status` 同源可替代 |
| `max_concurrent_children` 默认 3 vs 10 | 随包文档与源码常量冲突（本机代码=10） | 以源码常量 10 为准；文档属旧版本 |
| 压缩阈值的**窗口基数**（0.50 × 多少 token） | `model.context_length` 未在本机 config 显式设置 | 观察 `compression started` 的 `tokens=` 与模型窗口对照 |
