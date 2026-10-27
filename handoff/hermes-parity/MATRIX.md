# Orbit × Hermes 对标矩阵（主清单）

**完成判据**：本表每一行都要有「实现文件:行 + 验证方式」两项证据，缺一不算完成。
**状态口径**：✅ 已实现并有证据 · 🟡 部分（有实现但缺项）· ⬜ 未开始
**依据**：`handoff/hermes-parity/research/01..09-*.md`（逐层调研，每条结论标【实证】/【推断】）
**进度报告**：`handoff/hermes-parity/PROGRESS.md`（监视子agent 定期核对后重写）
**排期**：`handoff/hermes-parity/ROADMAP.md`

条目合计 **181**。当前：✅ 25 · 🟡 7 · ⬜ 149（完成度 13.8%，截至 W3/W4/W13 收口）
计数**可重算**：`backend\.venv\Scripts\python.exe scripts\matrix_stats.py --check`
（头部数字与表格不符时退出 1——手维护的数字必然漂移，所以给了这个门禁。
但**最终判据仍是 `PROGRESS.md` 的抽查结论**：文档说完成、代码查无实据的，一律不计分）

---

## L1 入口 / CLI 层（Hermes: `hermes_cli/`，74 顶层命令 / 295 二级 / 36 三级 = 408 可调用节点 / 23 全局 flag）

| # | 条目 | Hermes 依据 | 波次 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 1.1 | 中央命令注册表 + 声明式命令面 | `hermes_cli/commands.py` 的 `CommandDef` 中央注册表 | W1 | ✅ | `backend/app/cli/spec.py` 的 `Registry` / `Command`；`app/cli/commands/` 各模块 `REGISTRY.add(...)` |
| 1.2 | argparse 子命令树 + `--help` 版式对齐 | `hermes --help` 即 argparse 版式 | W1 | ✅ | `backend/app/cli/main.py` 的 `build_parser` / `_add_command` |
| 1.3 | 全局 flags（`--version` / `--json` / `--quiet` / `--data-dir` / `--no-color`） | `hermes --help` options 段 | W1 | ✅ | `main.py` 的 `_global_options`（用 `SUPPRESS` 防子命令默认值覆盖父级解析） |
| 1.4 | 顶层命令面**已登记**（74 个独立命令，77 个名字含别名） | `_raw/cli-help.txt`；调研 01 §0 | W2 | ✅ | `REGISTRY.all()` == 74；`tests/test_cli.py::test_top_level_surface_covers_hermes` |
| 1.5 | 二级子命令面**已登记**（298 条，选项说明 100% 覆盖） | `_raw/sub-digest.txt`；调研 01 | W2 | ✅ | `REGISTRY.parity_rows()` == 407；`undocumented_options(SPECS) == 0` |
| 1.6 | 三级子命令面**已登记**（35 条 / 8 个嵌套命令组） | `_raw/sub3-digest.txt`；调研 01 | W2 | ✅ | 同上（`tests/test_cli.py::test_option_help_is_documented`） |
| 1.16 | 上述 407 条里**有真实现**的比例（当前 **46 / 407**：approvals 9 / status…config 25 / model·fallback·auth·moa·login 等 12） | — | W2→W13 | 🟡 | `REGISTRY.parity_rows()` 的 `state` 分布（46 done / 2 partial / 359 todo）；`todo` 命令调用 exit 3 并给出完整命令路径与排期波次 |
| 1.7 | 全局运行 flags 完整（23 个：`-z` / `-m` / `--provider` / `-t` / `--resume` / `--continue` / `--in` / `--yolo` / `--skills` / `--tui` / `--cli` / `--dev` / `--safe-mode` / `--ignore-user-config` / `--ignore-rules` / `--accept-hooks` / `--worktree` / `--pass-session-id` / `--no-restore-cwd` / `--usage-file` / `--reasoning` 等） | 调研 01 §2 | W2 | ⬜ | — |
| 1.7 | oneshot 模式 `-z`（无横幅、无 spinner、只输出最终文本，供脚本管道） | `hermes --help` `-z` 说明 | W11 | ⬜ | — |
| 1.8 | 交互 chat 模式（`orbit chat`） | `cmd-01-chat.txt` | W11 | ⬜ | — |
| 1.9 | shell 补全脚本 | `hermes completion {bash,zsh,fish}` | W1 | ✅ | `core.py` `run_completion`（额外支持 powershell） |
| 1.10 | `serve` headless 后端 | `cmd-73-serve.txt` | W12 | ⬜ | — |
| 1.11 | `dashboard` Web UI 启动器 | `cmd-72-dashboard.txt` | W12 | ⬜ | — |
| 1.12 | `desktop` / `gui` 原生应用 | `cmd-74/75` | W12 | ⬜ | — |
| 1.13 | `acp` ACP 服务端 | `cmd-64-acp.txt` | W11 | ⬜ | — |
| 1.14 | `console` 安全命令台 | `cmd-44-console.txt` | W12 | ⬜ | — |
| 1.15 | TUI（终端 UI） | `tui/dist` | W12 | ⬜ | — |

## L2 Agent 核心循环层（Hermes: `run_agent.py` + `agent/*`，PYZ 内）

| # | 条目 | Hermes 依据 | 波次 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 2.1 | 单循环 + 传输适配器归一化多 provider | 【推断】`agent/conversation_loop.py` | W3 | ⬜ | — |
| 2.2 | 三套**独立不叠加**的迭代预算：父 `agent.max_turns` 默认 **90**、子 `delegation.max_iterations` 默认 **50**、`DEFAULT_MAX_ITERATIONS=250` 只是 config 缺省兜底（不是运行值）；desktop 入口另有 500 档 | 【实证】`delegate_tool.py:1150`；本机日志 `budget=…/90` 327 条、`max_iterations_reached(50/50)`；调研 09 §1 | W11 | ⬜ | — |
| 2.3 | 心跳 30s + 双阈值判卡死（idle 15 周期=**450s** / in-tool 40 周期=**1200s**），超阈值只 break 续命、由 gateway inactivity 超时执行真正的杀 =「级联」 | 【实证】`delegate_tool.py:1169,1184,1185`；调研 09 §2 | W11 | ⬜ | — |
| 2.4 | 中断配对：工具**返回** `{"output":"[interrupted]","returncode":130}` 而非抛异常；父中断时未完成 child 被合成 `status=interrupted` 结果条目；不可修 arguments 降级为 `{}` | 【实证】`tools/interrupt.py`；`delegate_tool.py:4289-4327` | W11 | ⬜ | — |
| 2.5 | 循环结束后台动作：bg-review 线程 + 同会话追加一轮「Review the conversation above and update the skill library」学习回路 + curator | 【实证】`agent.background_review` 日志实证；调研 09 §4 | W5 | ⬜ | — |
| 2.6 | 错误分类 13 类 + 仅 4 类自动重试 | 【实证】`tools/bot_failure_reasons.py`（`AUTO_RETRYABLE` 4 个） | W4 | ⬜ | — |
| 2.7 | `context_overflow` → 压缩后重试 | 【实证】bot_failure_reasons 映射 | W8 | ⬜ | — |
| 2.8 | per-thread 中断（可打断单条线程） | 【实证】`tools/interrupt.py` | W11 | ⬜ | — |
| 2.9 | 三层预算刹车（动作 / token / 时间） | Orbit 已有 | W1 | ✅ | `app/config.py` 的 `max_actions` / `max_seconds`；`app/policies.py` 的 `DefaultBudgetPolicy`；`app/graph/nodes.py` 的 budget 节点 |
| 2.10 | 上下文窗口自适应结果阈值（per_result=clamp(win×0.15,8k,100k)、per_turn=clamp(win×0.30,16k,200k)） | 【实证】`tools/budget_config.py` 注释 | W4 | ⬜ | — |

## L3 上下文装配层（Hermes: `agent/prompt_builder.py`）

| # | 条目 | Hermes 依据 | 波次 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 3.1 | 三层 prompt 分层，**装配顺序已用 state.db 16 份 system_prompt 全文按字节偏移定死**：`SOUL.md`(0) → 行为/环境/编码/工作区/profile/desktop 段 → `# Project Context` AGENTS.md(15,227) → `## Skills <available_skills>`(24,200) → `MEMORY`(40,080) → `USER PROFILE`(42,424) → Volatile 尾(43,906: Conversation started / Model / Provider / Platform)。16/16 份都以 SOUL.md 开头；**工具 schema 不在 prompt 内**（独立 tools 数组） | 【实证】调研 09 §5 | W3 | ⬜ | — |
| 3.2 | 项目文件单一优先级加载（`AGENTS.md` vs `HERMES.md`，cwd → git root） | 【实证】调研 09 §5 | W3 | ⬜ | — |
| 3.3 | `SOUL.md` 独立全局加载，是 prompt 第一块（不受 memory 预算约束） | 【实证】本机 `hermes-home/SOUL.md` 540B；调研 07 | W3 | ⬜ | — |
| 3.4 | 记忆注入（冻结快照，会话内不变） | 【实证】`load_from_disk()` + `_system_prompt_snapshot`；调研 07 | W6 | ⬜ | — |
| 3.5 | 技能索引注入 + description 60 字符预算 | 【实证】`SKILL_PROMPT_DESC_LIMIT=60`（57+"..."）；调研 04 | W5 | ⬜ | — |
| 3.6 | 工具 schema 注入 + 体积可观测 | 【实证】`hermes prompt-size` | W1 | ✅ | `core.py` `run_prompt_size`（当前：系统提示 **4,043 B** / 工具 schema **16,895 B** / **29** 个工具；数值随提示词与工具变化，**不要写死在这里**——以命令输出为准） |
| 3.7 | 提示词版本号随事件落库 | Orbit 已有（dsh 派，非 Hermes 派） | W1 | ✅ | `backend/app/context.py:12` `PROMPT_VERSION` |
| 3.8 | 注入扫描（不可信内容包裹） | 【实证】调研 09 `untrusted-content wrapper` | W13 | ⬜ | — |

## L4 模型适配层（Hermes: `providers/` + `plugins/model-providers/`，39 目录 / 48 profile）

| # | 条目 | Hermes 依据 | 波次 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 4.1 | Provider 注册表 + `ProviderProfile` 钩子（fetch_models / build_extra_body / build_api_kwargs_extras / prepare_messages / resolve_aux_model / default_vision_model / supported_reasoning_efforts） | 【实证】48 个 provider 调用点反推；调研 06 §2 | W3 | 🟡 | `app/providers.py` 16 家（Hermes 48 profile）：`deepseek openai anthropic gemini openrouter zai kimi-coding minimax moonshot qwen groq mistral xai ollama bedrock custom`；注册表与钩子齐全，**覆盖面待扩** |
| 4.2 | 多 transport（`chat_completions` / `anthropic_messages` / `codex_responses` / `bedrock_converse` / `copilot_acp`） | 【实证】调研 06 §3 | W3 | 🟡 | 已实现 3 种（`chat_completions` 走 langchain、`anthropic_messages` / `gemini` 走 httpx）；`bedrock_converse` 声明但**明确抛错**（无 boto3）；`codex_responses` / `copilot_acp` 未做 |
| 4.3 | per-model 动态协议路由（Claude→Messages、GPT-5/Codex/Grok→Responses、其余→chat） | 【实证】调研 06 §3.3 | W3 | ⬜ | — |
| 4.4 | 逐 provider 推理线格式翻译表（5 种落点 + 互斥/拒绝/强制规则） | 【实证】调研 06 §3.5（16 行差异） | W3 | ⬜ | — |
| 4.5 | fallback 链（触发 429/529/503/连接失败；其它错误不切换） | 【实证】`hermes fallback --help`；调研 06 | W3 | 🟡 | `app/fallback.py` `FallbackChain` / `FAILOVER_STATUS`；`tests/test_fallback.py`。**未接线运行时**：`agent.get_model()` 仍是唯一出模型的地方，回退链只在库层 + CLI 管理面生效 |
| 4.6 | credential pool（多 key 轮转 + 耗尽标记 + `auth reset`） | 【实证】`hermes auth --help` 子命令 | W3 | 🟡 | `app/fallback.py` `CredentialPool` / `CredentialLease`；密钥走 keyring。**同样未接线运行时**，理由同上 |
| 4.7 | `proxy` 本地 OpenAI 兼容代理（到 OAuth provider） | 【实证】`cmd-11-proxy.txt` | W3 | ⬜ | — |
| 4.8 | `egress` 出口凭证注入防火墙（iron-proxy） | 【实证】`cmd-08-egress.txt` | W13 | ⬜ | — |
| 4.9 | 辅助模型 slot 18 个（`auxiliary.*`，含 vision） | 【实证】调研 06 §4 | W3 | ⬜ | — |
| 4.10 | 三级能力元数据（手工覆盖 → models_dev cache → 出厂 snapshot） | 【实证】调研 06 §5 | W3 | ⬜ | — |
| 4.11 | MoA（Mixture of Agents：N reference + 1 aggregator） | 【实证】`cmd-03-moa.txt` | W3 | ⬜ | — |
| 4.12 | TTS 11 家 / STT 8 家 + command provider | 【实证】调研 06 §4 | W3 | ⬜ | — |
| 4.13 | 配置树可完整导出（`orbit config show` 给嵌套全树 + `config check` 查缺失/过期） | 【实证】`hermes config show/check/migrate` | W1 | ✅ | `core.py` `_config_tree` / `config_check` / `config_migrate` |

## L5 工具层（Hermes: `_internal/tools/`，147 模块 / 94 内置工具 / 19 功能域）

| # | 条目 | Hermes 依据 | 波次 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 5.1 | 单一注册表 + 跨 source 遮蔽门禁（默认拒绝，`override=True` 需操作员 opt-in，deregister 同门禁） | 【实证】`tools/registry.py`；调研 02 §1 | W4 | ⬜ | — |
| 5.2 | 懒发现（AST 索引按需 import + generation 计数失效） | 【实证】`registry.py` `_generation` / `_SKIP_TOOL_FILES` | W4 | ⬜ | — |
| 5.3 | `check_fn` 可用性探针（30s TTL + 60s 抖动宽限 + profile 维度 key + 512 上限） | 【实证】`registry.py`；调研 02 §1 | W4 | ⬜ | — |
| 5.4 | 三层结果治理（单条 100K / MCP 50K / 预览 1,500 / 整轮 200K / spillover 落盘 24h 清理） | 【实证】`tools/budget_config.py` `DEFAULT_*` | W4 | ⬜ | — |
| 5.5 | `tool_output_limits`（max_bytes 50,000 / max_lines 2,000 / 行宽 2,000；终端 40% 头 + 60% 尾） | 【实证】`tools/tool_output_limits.py` | W4 | ⬜ | — |
| 5.6 | `read_file` 阈值钉死 `inf`（防 persist→read→persist 死循环） | 【实证】`budget_config.py` `PINNED_THRESHOLDS` | W4 | ⬜ | — |
| 5.7 | `hook_output_spill`（10,000 / head 500 / tail 500）+ `spill_safety` 拒符号链接 | 【实证】调研 02 §3 | W4 | ⬜ | — |
| 5.8 | 危险命令两级：**18 条 hardline 硬地板**（无条件拦）+ **38 条 dangerous**（其中 35 条标破坏类，永不自动加白） | 【实证】`app/approvals.py` `HARDLINE_PATTERNS` / `DANGEROUS_PATTERNS` | W13 | ✅ | `app/approvals.py:60,96`；`tests/test_approvals.py` 89 条用例覆盖 |
| 5.9 | Windows 破坏性操作清单（diskpart / Format / Clear-Disk / vssadmin / bcdedit / reg delete / icacls / iwr\|iex / EncodedCommand） | 【实证】`app/approvals.py` | W13 | ✅ | 同上 |
| 5.10 | 自终止保护（`pkill hermes\|orbit` / `hermes gateway stop` / `hermes update`） | 【实证】`app/approvals.py` `self_kill` / `self_modify` | W13 | ✅ | 同上 |
| 5.11 | `tool_search` 分级披露（core/GUI 永不延迟；listing 4,000 token；Tier0/1/2） | 【实证】`tools/tool_search.py` | W4 | ⬜ | — |
| 5.12 | `todo` 元工具 | 【实证】`tools/todo_tool.py` | W4 | ⬜ | — |
| 5.13 | `clarify` 元工具 + gateway | 【实证】`tools/clarify_tool.py` | W4 | ⬜ | — |
| 5.14 | 内置工具数量对齐（94 个；Orbit 现 28 个） | 【实证】调研 02 口径说明 | W4 | ⬜ | — |
| 5.15 | 19 个功能域覆盖（file/terminal/code_execution/browser/web/专项搜索/memory/skills/MCP/委派/媒体生成/视觉/TTS/kanban/cronjob/桌面窗口/协作消息/HomeAssistant/元工具） | 【实证】调研 02 §2 | W4 | 🟡 | 已有 **7 域**：file / terminal / browser / web / memory / skills / MCP。缺 12 域：code_execution、专项搜索、委派、媒体生成、视觉、TTS、kanban、cronjob、桌面窗口、协作消息、HomeAssistant、**元工具**（`ask_user` 是交互门不是元工具；`todo` / `clarify` / `tool_search` 均不存在，见 5.12/5.13） |
| 5.19 | `web_search` + `web_extract`（4 家搜索 provider + URL 安全闸 + 结果缓存） | 【实证】`plugins/web/` 9 provider；`tools/url_safety.py`、`web_result_cache.py` | W4 | ✅ | `app/web.py` + `app/web_providers.py`（searxng / brave / tavily / ddgs，契约照 Hermes）；`tests/test_web.py` 41 条。已接线 `app/main.py` |
| 5.16 | `file_operations` / `patch_parser`（结构化补丁编辑） | 【实证】`tools/patch_parser.py` | W4 | ⬜ | — |
| 5.17 | `code_execution` + `code_kernel`（持久内核） | 【实证】`tools/code_execution_tool.py` | W4 | ⬜ | — |
| 5.18 | `terminal` 后端抽象（本地/远程/容器） | 【实证】`tools/terminal_tool.py` 217KB | W4 | ⬜ | — |

## L6 工具集 / 注册层（Hermes: `toolsets.py` / `toolset_distributions.py`）

| # | 条目 | Hermes 依据 | 波次 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 6.1 | toolset 定义（32 个已注册 toolset） | 【实证】调研 02；【推断】具体成员（PYZ 内） | W4 | ⬜ | — |
| 6.2 | 四层过滤（平台 / 场景 / profile / 委托） | 【实证】前作报告 + `hermes tools --platform` | W4 | ⬜ | — |
| 6.3 | `orbit tools {list,enable,disable,post-setup}` | 【实证】`_raw/sub/tools__*.txt` | W4 | ⬜ | — |
| 6.4 | "注册 vs 暴露"分离（per-run 收窄暴露面） | 【实证】前作 §4 建议；Hermes `tool_search` 即其实现 | W4 | ⬜ | — |
| 6.5 | 工具 schema 校验前置（dict / parameters 必须 object） | 【实证】`registry.register` raise 检查 | W4 | ⬜ | — |

## L7 Skill / curator 层（Hermes: `skills_*.py` + `bundled-skills`，21 类目 / 91 技能）

| # | 条目 | Hermes 依据 | 波次 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 7.1 | 多根 tier（project > user > external，first-wins 去重） | 【实证】调研 04 §2 | W5 | ⬜ | — |
| 7.2 | frontmatter 16 字段 + 11 条硬校验（强制 name + description） | 【实证】调研 04 §1.2 | W5 | ⬜ | — |
| 7.3 | 名字 64 / 描述 1,024 / 索引 60 字符 / 正文 100,000 / 单文件 1 MiB 上限 | 【实证】调研 04 §3 数值 | W5 | ⬜ | — |
| 7.4 | 渐进披露三层（索引只放名字+短摘要 → 按需 `skill_load` 全文） | 【实证】调研 04 §4 | W5 | ⬜ | — |
| 7.5 | 快照缓存 TTL 30s + view 去重 | 【实证】`.skills_prompt_snapshot.json` v2（manifest 183 条 / skills 165 条） | W5 | ⬜ | — |
| 7.6 | 写侧校验链（frontmatter + AST 审计 + linter 12 规则）+ staging 审批门 | 【实证】`skill_manager_tool.py` / `skills_ast_audit.py` / `skill_linter.py` | W5 | ⬜ | — |
| 7.7 | ledger 记账（8 字段：id/ts/actor/action/skill/evidence/before/after，sha256 内容寻址） | 【实证】`tools/skill_ledger.py` | W5 | ⬜ | — |
| 7.8 | curator 状态机 + 两级锚点（max(last_used, last_viewed, last_patched) → created_at，**不含 mtime**） | 【实证】调研 04 §5 | W5 | ⬜ | — |
| 7.9 | prune 默认 90 天 / prune_builtins True / consolidate False | 【实证】`hermes curator prune --help` | W5 | ⬜ | — |
| 7.10 | 可恢复归档（archive → restore / rollback） | 【实证】`curator archive/restore/rollback` | W5 | ⬜ | — |
| 7.11 | `.usage.json` 13 字段（use_count / view_count / patch_count / reuse_after_patch） | 【实证】调研 04 §6 | W5 | ⬜ | — |
| 7.12 | `skills_guard` 107 条威胁模式 + hub quarantine | 【实证】`tools/skills_guard.py` | W5 | ⬜ | — |
| 7.13 | `orbit skills {list,show,search,install,update,uninstall,browse,inspect,audit,check,diff,opt-in,opt-out,publish,snapshot,tap,trust,untrust,reset,repair-official,config,list-modified}` | 【实证】`_raw/sub/skills__*.txt`（22 条） | W5 | ⬜ | — |
| 7.14 | `orbit bundles {create,list,show,delete,reload}`（技能别名集合） | 【实证】`_raw/sub/bundles__*.txt` | W5 | ⬜ | — |
| 7.15 | `orbit curator {status,run,pause,resume,pin,unpin,prune,purge,archive,restore,adopt,ledger,usage,backup,rollback,list-archived,list-unmanaged}` | 【实证】`_raw/sub/curator__*.txt`（17 条） | W5 | ⬜ | — |
| 7.16 | `orbit sync`（跨设备技能同步 + 三方合并） | 【实证】`cmd-25-sync.txt` | W5 | ⬜ | — |
| 7.17 | `orbit journey {list,edit,delete}`（学习时间线；`learning` / `memory-graph` 是别名） | 【实证】`cmd-51/52/53` 输出完全相同 | W5 | ⬜ | — |
| 7.18 | 技能库规模（21 类目 / 91 技能；Orbit 现 3 条） | 【实证】调研 04 §1 | W5 | ⬜ | — |

## L8 MCP 层（Hermes: `tools/mcp_*.py` 约 25 模块）

| # | 条目 | Hermes 依据 | 波次 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 8.1 | stdio + Streamable HTTP（新旧两种 client） | 【实证】调研 02 §2；`mcp_tool.py` 389KB | W7 | ⬜ | — |
| 8.2 | 能力声明前置（先读 `initialize_result` 再调用） | 【实证】`tools/mcp_tool.py` | W7 | ⬜ | — |
| 8.3 | OAuth（`mcp_oauth.py` 84KB / `mcp_oauth_manager.py` / `mcp_dashboard_oauth.py`） | 【实证】调研 05 §2 | W7 | ⬜ | — |
| 8.4 | prompts → 技能 | 【实证】`hermes mcp --help`；Orbit 已有雏形 | W7 | ⬜ | — |
| 8.5 | resources → RAG 索引 | 【实证】同上 | W7 | ⬜ | — |
| 8.6 | 心跳 + 失败宽限期（不因单次失败摘工具） | 【实证】`mcp_stdio_watchdog.py`；`_CHECK_FN_FAILURE_GRACE_SECONDS` | W7 | ⬜ | — |
| 8.7 | schema 缓存（`mcp_schema_cache.py`） | 【实证】调研 05 | W7 | ⬜ | — |
| 8.8 | `orbit mcp {add,list,remove,test,login,reauth,configure,catalog,install,picker,serve}` | 【实证】`_raw/sub/mcp__*.txt`（11 条） | W7 | ⬜ | — |
| 8.9 | 把 Orbit 自己暴露成 MCP 服务（`mcp serve`） | 【实证】`_raw/sub/mcp__serve.txt` | W7 | ⬜ | — |
| 8.10 | optional-mcps 预置目录（63 个服务模板） | 【实证】`_internal/optional-mcps/` | W7 | ⬜ | — |

## L9 记忆层（Hermes: `memory_tool.py` + `session_search_tool.py` + `plugins/memory/`）

| # | 条目 | Hermes 依据 | 波次 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 9.1 | `MEMORY.md` 上限 2,200 字符（可配 1–8,000），按字符不按 token | 【实证】调研 07 §1（实测 2,171/98.7%） | W6 | ⬜ | — |
| 9.2 | `USER.md` 上限 1,375 字符（实测 1,393/101.3% 已超顶） | 【实证】调研 07 §1 | W6 | ⬜ | — |
| 9.3 | 冻结快照注入 + 头部自曝用量（`MEMORY (your personal notes) [99% — 2,181/2,200 chars]`） | 【实证】state.db `sessions.system_prompt` 反查 | W6 | ⬜ | — |
| 9.4 | 原子批处理 `operations[]`（按最终态校验，全有或全无） | 【实证】`memory_tool.py` `apply_batch` | W6 | ⬜ | — |
| 9.5 | 一轮 3 次 consolidation 失败熔断（`_MAX_CONSOLIDATION_FAILURES_PER_TURN=3`） | 【实证】调研 07 §2 | W6 | ⬜ | — |
| 9.6 | CJK 双 FTS 索引（unicode61 + trigram；中文必须走 trigram，否则命中 0） | 【实证】`messages_fts` + `messages_fts_trigram`；`MATCH '记忆'` = 0 | W6 | ⬜ | — |
| 9.7 | `session_search` 四形态（discover/scroll/read/browse）+ 锚点 ±5 + bookend 3/3 + 300 行 lineage 去重 | 【实证】`session_search_tool.py` 1,271 行 | W6 | ⬜ | — |
| 9.8 | 检索**零 LLM 成本**（无 summary LLM 路径） | 【实证】docstring "No LLM calls anywhere"；PR #26419 | W6 | ⬜ | — |
| 9.9 | 检索参数（limit 默认 3 夹 [1,10]、window 默认 5 夹 [1,20]、read head20/tail10、截断 4000/1200） | 【实证】调研 07 §3 | W6 | ⬜ | — |
| 9.10 | 8 个外部 memory provider 插件接口（byterover/hindsight/holographic/honcho/mem0/openviking/retaindb/supermemory） | 【实证】调研 07 §4 | W6 | ⬜ | — |
| 9.11 | holographic 本地图记忆（HRR dim=1024、权重 fts0.4/jaccard0.3/hrr0.3、trust ±0.05/−0.10） | 【实证】`plugins/memory/holographic/` | W6 | ⬜ | — |
| 9.12 | 写入门禁（威胁扫描 → 审批三态 → 快照期二次扫描） | 【实证】调研 07 §5 | W6 | ⬜ | — |
| 9.13 | 漂移防护（round-trip + 单条超全库上限 → `.bak.<ts>` + 拒写） | 【实证】调研 07 §5 | W6 | ⬜ | — |
| 9.14 | 核心零向量（向量只在 provider 里） | 【实证】`_internal` 无 lancedb/chromadb/faiss/sentence_transformers | W6 | ⬜ | — |
| 9.15 | `orbit memory {setup,status,off,reset}` | 【实证】`_raw/sub/memory__*.txt` | W6 | ⬜ | — |
| 9.16 | `orbit profile`（14 子命令，用户画像） | 【实证】`_raw/sub/profile__*.txt` | W6 | ⬜ | — |

## L10 持久化 / 会话层（Hermes: `hermes_state*.py`，state.db 26 业务表）

| # | 条目 | Hermes 依据 | 波次 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 10.1 | 单一 session 平面（CLI / TUI / gateway / cron 同库同表） | 【实证】`sessions.json` README；`sessions.source` 实测 4 种来源 | W8 | ⬜ | — |
| 10.2 | 消息追加日志 + `id` AUTOINCREMENT 作游标（不复用） | 【实证】调研 03 §5（max id 14,597 > 行数 14,595） | W8 | ⬜ | — |
| 10.3 | 归档不删（`active=0 AND compacted=1`） | 【实证】调研 03 §3（2,101 条） | W8 | ⬜ | — |
| 10.4 | 压缩谱系（`parent_session_id` + `end_reason='compression'`）；压缩阈值 **0.50** / 目标比 **0.20**；产物前缀 `[CONTEXT COMPACTION` / `[CONTEXT SUMMARY]:`；实测 529k→199k、503k→191k（≈37.6%） | 【实证】调研 09 §6、03 §3 | W8 | ⬜ | — |
| 10.5 | `system_prompts` 内容寻址（hash）+ `compression_locks` | 【实证】调研 03 §1 | W8 | ⬜ | — |
| 10.6 | 幂等键 + 至少一次投递账本（`delivery_obligations` / `async_delegations` / `tasks.idempotency_key`） | 【实证】调研 03 §1（123 行） | W8 | ⬜ | — |
| 10.7 | 孤儿回收（`startup_orphan_reap` 67 条 / `ws_orphan_reap` 3 条） | 【实证】调研 03 §3 | W8 | ⬜ | — |
| 10.8 | 存活租约（`runtime/active_sessions.json` + `gateway_heartbeats` + `processes.json` 的 (pid, host_start_time)） | 【实证】调研 03 §3 | W8 | ⬜ | — |
| 10.9 | checkpoint（影子 git 库 + prune 保留策略） | 【实证】`checkpoint_manager.py` 88KB；`hermes checkpoints --help` | W8 | ⬜ | — |
| 10.10 | backup / import（zip 往返） | 【实证】`cmd-38/40` | W2 | ⬜ | — |
| 10.11 | `schema_version` 表 + 迁移器 | 【实证】调研 03 §1（v26） | W8 | ⬜ | — |
| 10.12 | 双 FTS 索引（分词 + trigram） | 【实证】调研 03 §1 | W8 | ⬜ | — |
| 10.13 | `orbit sessions`（19 子命令：list/browse/show/rename/export/import/prune/archive/pin/unpin/pinned/stats/recover/repair/repair-routing/optimize/optimize-storage/clean-markers/retitle-skills/delete） | 【实证】`_raw/sub/sessions__*.txt` | W8 | ⬜ | — |
| 10.14 | 事件溯源（`hosted_room_*` 14 张表：seq + event_id UNIQUE + cursors + watermarks） | 【实证】调研 03 §5 | W8 | ⬜ | — |
| 10.15 | `orbit checkpoints {status,prune,clear,clear-legacy}` | 【实证】`_raw/sub/checkpoints__*.txt` | W8 | ⬜ | — |

## L11 cron / 自动化层（Hermes: `tools/cronjob_tools.py` 99KB + `cron/`）

| # | 条目 | Hermes 依据 | 波次 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 11.1 | 定时任务存储 + 调度器 | 【实证】`cron/executions.db`；`tools/cronjob_tools.py` | W9 | ⬜ | — |
| 11.2 | `orbit cron {create,edit,list,remove,run,runs,tick,status,pause,resume,doctor,incidents,notepad}` | 【实证】`_raw/sub/cron__*.txt`（13 条） | W9 | ⬜ | — |
| 11.3 | 投递（`--deliver` 到平台） | 【实证】`cron create --deliver` | W9 | ⬜ | — |
| 11.4 | `--skill` / `--script` / `--no-agent` / `--monitor-script` / `--continuity` 语义 | 【实证】`cron create --help` | W9 | ⬜ | — |
| 11.5 | 与交互共享 session 平面与权限机制 | 【实证】前作 §4；调研 03 §2 | W9 | ⬜ | — |
| 11.6 | 紧急停止（`pause` / `resume` 级联 cron/kanban/gateway） | 【实证】`cmd-22/23` | W9 | ⬜ | — |

## L12 Gateway / 多平台层（Hermes: `_internal/gateway/` + `plugins/platforms/` 22 适配器）

| # | 条目 | Hermes 依据 | 波次 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 12.1 | 单 dispatcher 互斥 | 【实证】调研 05 §4 | W9 | ⬜ | — |
| 12.2 | 四个状态文件（`gateway-starts.log` / `processes.json` / `spawn-ledger.json` / `channel_directory.json`） | 【实证】本机 hermes-home 实测 | W9 | ⬜ | — |
| 12.3 | 平台适配器基类契约（7 必选 + 5 可选发送 + 5 交互 UX 降级 + 按钮回调 id 约定 `cl:`/`appr:`/`sc:`） | 【实证】调研 05 §3 | W9 | ⬜ | — |
| 12.4 | `standalone_sender_fn`（缺它会让 cron 投递静默失败） | 【实证】调研 05 §3 | W9 | ⬜ | — |
| 12.5 | 22 个平台适配器（a2a/buzz/dingtalk/discord/email/feishu/google_chat/homeassistant/irc/line/matrix/mattermost/ntfy/photon/raft/simplex/slack/sms/teams/telegram/wecom/whatsapp） | 【实证】调研 05 §2（22 目录 22 yaml；wecom 一目录两 Platform） | W9 | ⬜ | — |
| 12.6 | `orbit send`（每平台原生 ID 正则 12 条 + `MEDIA:` 附件 + 退出码 0/1/2） | 【实证】`cmd-17-send.txt` | W9 | ⬜ | — |
| 12.7 | `orbit webhook {subscribe,list,remove,test}`（HMAC + `{dot.notation}` + `--deliver-only` + `--script`） | 【实证】`_raw/sub/webhook__*.txt` | W9 | ⬜ | — |
| 12.8 | `orbit pairing {list,approve,revoke,clear-pending}`（DM 配对授权） | 【实证】`_raw/sub/pairing__*.txt` | W9 | ⬜ | — |
| 12.9 | `orbit peer {add,list,remove,dm,run,status,stop}`（跨机器 bot 间 DM） | 【实证】`_raw/sub/peer__*.txt` | W9 | ⬜ | — |
| 12.10 | `orbit gateway {setup,start,stop,restart,status,run,list,enroll,install,uninstall,migrate-legacy}` | 【实证】`_raw/sub/gateway__*.txt`（11 条） | W9 | ⬜ | — |
| 12.11 | `orbit monitoring status`（health & diagnostics 导出） | 【实证】`_raw/sub/monitoring__status.txt` | W12 | ⬜ | — |

## L13 插件层（Hermes: `plugins/` 18 组 / 103 provider）

| # | 条目 | Hermes 依据 | 波次 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 13.1 | `plugin.yaml` 16 字段 manifest（kind 决定加载时机） | 【实证】调研 05 §1 | W10 | ⬜ | — |
| 13.2 | `requires_env` / `optional_env` 富字典 6 子字段（自动注入 setup 向导） | 【实证】168 条声明实测 | W10 | ⬜ | — |
| 13.3 | `provides_tools` 影响发现期 import 时机 | 【实证】调研 05 §1 | W10 | ⬜ | — |
| 13.4 | 19 种 `ctx.register_*` 能力注入面 | 【实证】调研 05 §1（13 真实调用 + 3 平台扩展点 + 3 文档化） | W10 | ⬜ | — |
| 13.5 | hook 事件：**本机可证 15 个**（两个 observability 插件 manifest 并集 + `approval.py` 的 pre/post_approval + `delegate_tool` 的 subagent_start/stop）；「17 个」的另 2 个（最可能 `post_compaction` / `pre_verify`）属【推断】——`VALID_HOOKS` 在 PYZ 内、exe 字节扫描 0 命中 | 【实证】调研 09 §9 | W10 | ⬜ | — |
| 13.6 | `plugin_guard` 三级裁决（safe/caution/dangerous，`--force` 不得覆盖 dangerous） | 【实证】`tools/plugin_guard.py` | W10 | ⬜ | — |
| 13.7 | 插件结构限额（400 文件 / 10 MB / 单文件 1 MB / 拒 symlink 逃逸） | 【实证】调研 05 §6 | W10 | ⬜ | — |
| 13.8 | dashboard 插件契约（manifest.json 8 字段 + 模块级 `router` + `/api/plugins/<name>/`） | 【实证】调研 05 §6 | W10 | ⬜ | — |
| 13.9 | `orbit plugins {list,show,enable,disable,install,remove,update,search,doctor,capabilities,pack}` | 【实证】`_raw/sub/plugins__*.txt`（11 条） | W10 | ⬜ | — |
| 13.10 | `plugins.enabled/disabled/entries` 配置 + `allow_tool_override` | 【实证】本机 config.yaml | W10 | ⬜ | — |

## L14 安全 / 审批层

| # | 条目 | Hermes 依据 | 波次 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 14.1 | 审批三态 `off` / `smart` / `manual` | 【实证】`hermes approvals test` 三态退出码 0/2/3 | W13 | 🟡 | `app/approvals.py` `APPROVAL_MODES`；`orbit approvals mode` 可切换。**缺交互式审批通道**：Orbit 没有"弹窗问一句"的 UI，`smart`/`manual` 的语义是"拒绝并让人自己去做"，与 Hermes 的 ask 不完全等价 |
| 14.2 | 硬地板无条件拦（18 条：`rm -rf /`、mkfs、`dd of=/dev/sd*`、fork bomb、`kill -1`、shutdown/reboot、diskpart、vssadmin、bcdedit…） | 【实证】`app/approvals.py` `HARDLINE_PATTERNS` | W13 | ✅ | `tests/test_approvals.py::test_hardline_*`（11 条样本 × 3 种模式）。**边界**：完全访问档默认不守硬地板（`approvals.hardline_in_machine`，默认关）——尊重 Orbit 既有产品决策，见 `README` 与 `test_file_scope.py` |
| 14.3 | 密钥脱敏（`security.redact_secrets` 默认开） | 【实证】config.yaml 注释 | W13 | ⬜ | — |
| 14.4 | `tirith` 执行前扫描（`tirith_*` 配置项） | 【实证】config.yaml 注释；前作"已被上游移除"需复核 | W13 | ⬜ | — |
| 14.5 | OSV 供应链审计（`security audit`） | 【实证】`tools/osv_check.py`；`_raw/sub/security__audit.txt` | W13 | ⬜ | — |
| 14.6 | 外部密钥源（Bitwarden / 1Password） | 【实证】`_raw/sub3/secrets-*__*.txt` | W13 | ⬜ | — |
| 14.7 | 系统 keyring 存密钥（不落配置文件） | Orbit 已有 | W1 | ✅ | `backend/app/connectors.py` 的 `ConnectorConfig.service`；CLI 侧 `app/cli/context.py` 的 `Context.hydrate` |
| 14.8 | `path_security` / `url_safety` / `website_policy` / `threat_patterns` | 【实证】调研 02 §4 | W13 | ⬜ | — |
| 14.9 | `orbit approvals {test,suggest,allow,list-rules,mode}`（`test` 三态退出码、`suggest` 从历史挖可加白模式且破坏类永不提议） | 【实证】`_raw/sub/approvals__*.txt` | W13 | ✅ | `app/cli/commands/approvals.py`；`tests/test_approvals.py::test_cli_*` |
| 14.10 | `orbit hooks {list,test,revoke,doctor}`（shell 脚本 hook） | 【实证】`_raw/sub/hooks__*.txt` | W13 | ⬜ | — |

## L15 可观测性层

| # | 条目 | Hermes 依据 | 波次 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 15.1 | 结构化日志 + `orbit logs` 过滤（`-n` / `-f` / `--level` / `--session` / `--since` / `--component` / `list`） | 【实证】`cmd-76-logs.txt` 六选项全对齐 | W1 | ✅ | `core.py` `run_logs` / `_filter_lines` / `_follow`（轮转仍未做，见 15.7） |
| 15.7 | 日志轮转（按大小/时间滚动） | 【推断】Hermes `hermes_logging` 模块（PYZ 内） | W12 | ⬜ | — |
| 15.2 | `orbit insights`（用量分析：token 三路归因 / 工具排行 / 站点分布 / 活动趋势） | 【实证】`cmd-59-insights.txt` | W12 | ✅ | `app/cli/commands/observability.py` `run_insights`；`tests/test_observability.py`。**口径分歧已标注**：`--source` 在 Hermes 是平台维度，Orbit 单用户本地部署改为"任务主要操作过哪个站点" |
| 15.3 | `orbit monitoring status`（health & diagnostics，**content-free**） | 【实证】`cmd-60-monitoring.txt` | W12 | ✅ | `observability.py` `run_monitoring_status`；`tests/test_observability.py::test_monitoring_status_is_content_free` 断言导出物里不含消息/结果正文 |
| 15.4 | shared_metrics schema v1 / v2 | 【实证】`hermes_cli/observability/schemas/*.json` | W12 | ⬜ | — |
| 15.5 | observability exporter 插件（langfuse / nemo_relay） | 【实证】`plugins/observability/` | W12 | ⬜ | — |
| 15.6 | `orbit dump`（排障快照） | 【实证】`cmd-36-dump.txt` | W1 | ✅ | `core.py` `run_dump`（密钥打码） |

## L16 Web UI / 前端层

| # | 条目 | Hermes 依据 | 波次 | 状态 | 证据 |
| --- | --- | --- | --- | --- | --- |
| 16.1 | dashboard 18 个页面语义（Analytics/Channels/Chat/Config/Cron/Env/Files/Logs/Mcp/Models/Pairing/Plugins/ProfileBuilder/Profiles/Sessions/Skills/System/Webhooks） | 【实证】`web_dist/assets/*Page-*.js` | W12 | ⬜ | — |
| 16.2 | 聊天页 + 增量流式 + 断线回放 | Orbit 已有 | W1 | ✅ | `frontend/src/App.tsx`；`backend/app/main.py` SSE |
| 16.3 | 技能页 / 记忆页 / 会话摘要页 | Orbit 已有 | W1 | ✅ | `frontend/src/Skills.tsx` / `Memories.tsx` / `Summaries.tsx` |
| 16.4 | 设置页（模型 / MCP / 权限级别） | Orbit 已有 | W1 | ✅ | `frontend/src/Settings.tsx` |
| 16.5 | `orbit skin`（主题切换） | 【实证】`_raw/sub/skin__*.txt` | W12 | ⬜ | — |
| 16.6 | 会话回放 / browse 页 | 【实证】`sessions browse` | W12 | ⬜ | — |

---

## 附：诚实边界（不计入完成度，但必须记住）

1. Hermes 核心层（`run_agent` / `hermes_state` / `hermes_cli` / `toolsets` / `agent/curator.py`）**无源码**，
   相关条目依据标【推断】，若被证伪必须回改实现。
2. `_DO_NOT_CAPTURE_BLOCK` 在 0.21.0 **不存在**（全树 grep 0 命中）。本表不登记它。
3. `hermes platforms` **不是有效子命令**（实测 invalid choice）。平台操作走 `gateway`/`send`/`pairing` 等。
4. 本机 gateway **从未接入真实消息平台**（`channel_directory.json` 的 `platforms` 为空）。平台层按
   "契约实现 + 本地回环验证"验收，不假装接过第三方。
5. 既有失败：`backend/tests/test_connectors.py::test_browser_append_and_reload` 在本轮开始前即失败
   （278 passed / 1 failed），属环境性既有问题。
