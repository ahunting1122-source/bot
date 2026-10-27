# 对标进度报告

**更新时间**：2026-10-27 23:32（本机实测时钟，只读取证）
**本轮轮次**：**N = 2**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑 `config set` / `doctor --fix` / `config edit` / `doctor --live` 等会改状态或真发请求的命令。全量 pytest 已按授权运行（只读）。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `02dff09`）

---

## 总体完成度

### **22 / 180 项 = 12.2%**

**口径（这次是硬口径，不再是上轮的代理口径）**：

| 项 | 说明 |
|---|---|
| 清单来源 | `MATRIX.md` 已成为**主清单**，本轮按其表行逐条统计：**180 行**（L1–L16） |
| 分母 | 180 条 |
| 完成 | **✅ 22 条**（= 有实现文件 + 有测试/命令输出证据，本轮已逐条复核） |
| 部分 | 🟡 **4 条**（4.1 / 4.2 / 5.15 / 14.1，**不计分**） |
| 未开始 | ⬜ **154 条** |
| 上轮可比性 | **不可直接相减**：上轮分母是 35 项代理清单（research top-N 汇总），本轮换成 MATRIX 180 行。同一批条目在上轮口径下的真实变化见下方「新增完成项」 |

### 新增完成项：**+21**

上轮 35 项代理口径里只认定 1 项完成（`completion`）。本轮 22 条 ✅ 中，**21 条是新增**，且**上轮我点名的 6 个「自评 done 但选项面是子集」的命令，现在全部补齐**——不是改标签，是代码真改了：

| 命令 | 上轮实测 | 本轮实测 | 结论 |
|---|---|---|---|
| `logs` | 仅 `--file/--tail` | `-n/--lines`、`-f/--follow`、`--level`、`--session`、`--since`、`--component` + `log_name` | **6/6 选项对齐**（目标名 3/5，见风险 R4） |
| `config` | 5 子命令 | **9 子命令**：`show,get,set,unset,path,env-path,edit,check,migrate` | 与 Hermes 9/9 对齐 |
| `status` | 无选项 | `--all` + `--deep` | 2/2 对齐 |
| `doctor` | 无选项 | `--fix` + `--live` + `--ack` | 3/3 对齐 |
| `dump` | 无 `--show-keys` | `--show-keys` | 对齐 |
| `prompt-size` | 缺 `--platform` | `--platform PLATFORM` | 对齐 |
| `completion` | 已对齐 | 已对齐（多 powershell） | 保持 |

> **本轮结论一句话**：上轮「完成率虚高 6 倍」的 R1 风险已被**结构性修掉**——`_table.py:56-65` 让声明表的 `Spec.build()` 硬编码 `state="todo"`，真实现必须在功能域模块里单独 `REGISTRY.add`。`state` 已不可能被转录表抬高。同时 12 个「done」命令的选项面**这次真的补全了**。

---

## 分层完成度

按 MATRIX 表行统计（脚本逐行解析，非目测）：

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI 层 | 4 / 16 | 25.0% | 进行中 |
| L2 Agent 核心循环 | 1 / 10 | 10.0% | 未开始（仅预算刹车） |
| L3 上下文装配 | 2 / 8 | 25.0% | 进行中 |
| L4 模型适配 | 3 / 13 | 23.1% | 进行中（🟡2） |
| L5 工具层 | 4 / 19 | 21.1% | 进行中（🟡1） |
| L6 工具集 / 注册 | 0 / 5 | 0% | 未开始 |
| L7 Skill / curator | 0 / 18 | 0% | 未开始 |
| L8 MCP | 0 / 10 | 0% | 未开始 |
| L9 记忆层 | 0 / 16 | 0% | 未开始 |
| L10 持久化 / 会话 | 0 / 15 | 0% | 未开始 |
| L11 cron / 自动化 | 0 / 6 | 0% | 未开始 |
| L12 Gateway / 多平台 | 0 / 11 | 0% | 未开始 |
| L13 插件层 | 0 / 10 | 0% | 未开始 |
| L14 安全 / 审批 | 3 / 10 | 30.0% | 进行中（🟡1） |
| L15 可观测性 | 2 / 7 | 28.6% | 进行中 |
| L16 Web UI / 前端 | 3 / 6 | 50.0% | 进行中 |
| **合计** | **22 / 180** | **12.2%** | — |

**辅助口径（命令面，与上表分母不同，勿混算）**：

| 指标 | 实测 | 说明 |
|---|---|---|
| 命令面**登记**率 | **74 / 74 顶层** · **407 / 407 展平节点** | `REGISTRY.all()` = 74，`parity_rows()` = 407 |
| 选项说明覆盖率 | **884 / 884 = 100%**，`undocumented_options(SPECS)` = **0** | 亲自复算：SPECS 67 顶层 → 展平 385 节点 → 884 个选项，无说明 = 0 |
| 命令**实现**率 | **43 / 407 = 10.6%** | registry 状态：`done` 43 · `partial` 2 · `todo` 362 |
| 六组归类填充 | `core` 342 · `ops` 30 · `run` 15 · `ext` 9 · `platform` 6 · `internal` 5 | 上轮「3 组为空」已消除 |

---

## 本轮抽查证据

**抽样率**：MATRIX 的 **22 条 ✅ 全部复核（22/22 = 100%）**，**4 条 🟡 全部复核（4/4）**，远超 30% 要求。另抽了 2 条 ⬜（5.12 / 5.13 元工具）做反证。

### A. 命令与模块实物核对（全部亲自跑，非看文档）

| 检查 | 命令 | 实测结果 |
|---|---|---|
| 注册表规模 | `len(REGISTRY.all()), len(REGISTRY.parity_rows())` | **74 / 407** ✅ 与要求一致 |
| 选项说明补全 | `len(undocumented_options(SPECS))` | **0** ✅；SPECS 67 顶层 / 展平 385 / 选项 **884** |
| provider 数 | `len(PROVIDERS)` | **16** ✅：`anthropic bedrock custom deepseek gemini groq kimi-coding minimax mistral moonshot ollama openai openrouter qwen xai zai` |
| 审批条数 | `len(HARDLINE_PATTERNS), len(DANGEROUS_PATTERNS)` | **18 / 38**，其中 `destructive=True` **35** 条 |
| 全量测试 | `pytest -q` | **480 passed / 1 failed / 481 collected，75.54s** |
| 既有失败 | — | 唯一失败 = `test_connectors.py::test_browser_append_and_reload`（**基线既有**，按指示不计回归）；本轮**未复现** `test_graph` 的负载敏感偶发失败 |
| 新增测试规模 | `pytest --collect-only -q` | `test_approvals` **89** · `test_web` **41** · `test_fallback` **29** · `test_providers` **25** · `test_cli` **18**（MATRIX 写的 89 / 41 是**参数化后的收集数**，与 `def test_` 的 24 / 29 不矛盾，已核） |
| todo 命令行为 | `orbit skin list` / `orbit portal info` | 均 **exit 3** + 明确报错「尚未实现（计划波次 …）」，`_table.py:74-85` → **未实现是诚实的，不是空壳** |

### B. 父 agent 点名的 4 条，逐条对账

**① 5.8「18 条 hardline + 43 条 dangerous」→ 43 这个数字不对，MATRIX 写的 38 才对**

- 运行时实测：`HARDLINE_PATTERNS` = **18**，`DANGEROUS_PATTERNS` = **38**，其中第 4 字段 `destructive=True` 的 = **35**（= 3 条非破坏类）。
- MATRIX 5.8 原文「18 条 hardline + **38** 条 dangerous（其中 **35** 条标破坏类）」——**与代码逐字吻合**。
- 你在消息里说的「43 条」**查无实据**；正确数字是 38。**建议以 MATRIX 为准，不要改。**
- 硬地板 18 条 id 实测：`mkfs, format_drive, dd_block_device, redirect_block_device, rm_root, rd_drive_root, del_drive_root, rm_drive_root, delete_shadows, bcdedit, cipher_w, reg_delete_hklm, diskpart, clear_disk, fork_bomb, kill_all, power_off, init_power`。

**② 4.1「16 家 provider」→ ✅ 实据成立（16/16）**

- `app/providers.py` 运行时 `PROVIDERS` = 16，名单与 MATRIX 列的 16 家**完全一致**。
- 🟡 定级合理：Hermes 48 profile → Orbit 16 = 33%，且 `notes=` 字段逐家写了与 Hermes 的差异（如 `providers.py:248` openai-codex 走 `codex_responses`、`providers.py:423` xai 同理），说明**知道自己缺什么**。

**③ 4.2「3 种 transport + bedrock 明确抛错」→ ✅ 实据成立，表述准确**

- `providers.py:46`：`Transport = Literal["chat_completions", "anthropic_messages", "gemini", "bedrock_converse"]` —— 4 个**声明**值。
- `providers.py:58-61`：`bedrock_converse` 的说明写明「只能走 boto3 SDK（SigV4 签名），没有 REST /v1 面；要接 Bedrock 需先允许 boto3，届时再补 transport」→ **声明但未实现**，且 `UnsupportedTransport`（`:729`）会抛。
- `codex_responses` 全文件仅 4 处、**全部在注释/notes 里**（`:233,248,408,423`），无 transport 实现 → MATRIX「未做」属实。
- `copilot_acp` = **0 处**，完全未提及。
- 故「已实现 3 种（chat_completions / anthropic_messages / gemini）+ bedrock 声明即抛错」**准确**。

**④ 5.19「web 工具已接线 main.py」→ ✅ 这次是真的接线了（上轮的怀疑已消解）**

- `app/main.py:140`：`from app.web import WEB_TOOLS, WebTools`
- `app/main.py:143`：`registry.register(WEB_TOOLS, app.state.web.execute)` ← **真实调用点，不是注释**
- `app/web.py` 实测导出 `WEB_TOOLS = ['web_search', 'web_extract']`；`app/web_providers.py` `PROVIDER_ORDER = ('searxng', 'brave', 'tavily', 'ddgs')` = 4 家 provider。
- `pytest --collect-only tests/test_web.py` = **41** 条，与 MATRIX 一致。
- 旁证：`orbit prompt-size --json` 工具数从 27 → **29**，多出来的 2 个正是这两个工具 → 接线已在运行时生效。

**⑤ 14.1 标 🟡「缺交互式审批通道」→ 这个降级是诚实的，不是虚报**

- `grep 'input\(|prompt_toolkit|click\.prompt|ask_user|interactive'` 于 `app/approvals.py` + `app/cli/commands/approvals.py` = **0 命中** → 确实没有任何"弹窗问一句"的通道。
- `EXIT_CODES = {'allow': 0, 'ask': 2, 'deny': 3}`：`ask` 这个裁决值存在、退出码也定义了，但**没有消费方**（CLI 里没有把它变成一次询问）。
- `APPROVAL_MODES = ('off', 'smart', 'manual')` 三态真实存在，`orbit approvals mode` 可切换。
- 故 MATRIX 的措辞「`smart`/`manual` 的语义是"拒绝并让人自己去做"，与 Hermes 的 ask 不完全等价」**准确**，🟡 定级恰当。**这是本轮最值得肯定的一条——主动降级而不是硬标 ✅。**

### C. 两档审批行为实测（你要求的 exit 3 / exit 0）

```
$ orbit approvals test -- mkfs.ext4 /dev/sda1
裁决：allow | 规则：mkfs | 审批模式：off（完全访问档）        EXIT=0
```
本机默认是**完全访问档**，所以 CLI 直接跑是 exit 0。为不改变任何配置，我改用 `classify()` 纯函数对两档各调一次（只读）：

| 场景 | verdict | exit | 理由 |
|---|---|---|---|
| `machine_wide=False`（workspace 档） | **deny** | **3** | 硬地板规则 mkfs：格式化文件系统（不可恢复，任何模式都不放行） |
| `machine_wide=True`，`hardline_in_machine=False`（本机默认） | **allow** | **0** | 按既有产品决策放行 |
| `machine_wide=True`，`hardline_in_machine=True`（开关打开） | **deny** | **3** | — |

**与你的描述完全一致，且这是刻意设计**：`approvals.py:20,203,230,325-329` 明确把「完全访问档是否也守硬地板」做成显式开关 `approvals.hardline_in_machine`，**默认 False**。不记为 bug。

### D. 22 条 ✅ 的证据链（逐条，含本轮复核发现）

| 条目 | 证据（本轮实测） | 判定 |
|---|---|---|
| 1.1 | `spec.py:124` `class Registry`；`spec.py:192` `REGISTRY = Registry()`（MATRIX 写 `:190`，漂移 2 行） | ✅ 结论成立，行号过期 |
| 1.2 | `main.py:92` `def build_parser()`（MATRIX 写 `:96`，漂移 4 行） | ✅ 结论成立，行号过期 |
| 1.3 | `main.py:34` `def _global_options()` + `:44-60` 用 `SUPPRESS`（MATRIX 写 `:47`，漂移 13 行） | ✅ 结论成立，行号过期 |
| 1.9 | `orbit completion {bash,zsh,fish,powershell}` ⊇ Hermes 3 种 | ✅ 精确 |
| 2.9 | `config.py:119 max_actions=40` / `:122 max_seconds=360`（MATRIX 写 `:113,116`，漂移 6 行）；预算节点 `graph/nodes.py` | ✅ 结论成立，行号过期 |
| 3.6 | `orbit prompt-size --json` → **system 4,043B / tool 16,895B / 29 工具**；MATRIX 写「3,417B / 15,177B / 27 工具」 | ⚠️ **结论成立，三个数字全部过期**（web 两工具接入后未回填） |
| 3.7 | `context.py:12 PROMPT_VERSION = "2026-10-27.1"` | ✅ 精确 |
| 4.5 | `fallback.py:40 FAILOVER_STATUS = frozenset({429,529,503})`；`:189 FallbackChain`；`:167 FallbackExhausted`；`test_fallback.py` 29 条 | ✅ 精确 |
| 4.6 | `fallback.py:395 CredentialPool` / `:381 CredentialLease` / `:335 CredentialPoolExhausted` / `:313 KEY_PREFIX="pool:"`；`orbit auth {add,list,remove,reset,status,logout,spotify}` | ✅ 精确 |
| 4.13 | `core.py:615 config_show` / `:733 config_check` / `:775 config_migrate`；`orbit config --help` 实测 9 子命令 | ✅ 精确 |
| 5.8 | 18 / 38 / 35（见 B①） | ✅ 精确 |
| 5.9 | Windows 全清单逐条命中：`approvals.py:43 format_drive`、`:62 delete_shadows(vssadmin)`、`:63 bcdedit`、`:69 diskpart`、`:94 icacls_reset`、`:105 iwr_pipe_iex`、`:107 invoke_expression`、`:108 encoded_command` | ✅ 精确 |
| 5.10 | `approvals.py:121 self_kill`（`pkill hermes|orbit`）、`:122 self_modify`（`hermes|orbit stop/update/uninstall`） | ✅ 精确 |
| 5.19 | `main.py:140,143` 真实 register；`web.py` 2 工具；`web_providers.py` 4 家；41 条测试 | ✅ 精确 |
| 14.2 | 18 条硬地板 + `hardline_in_machine` 默认 False（`:203`），边界已在证据栏诚实标注 | ✅ 精确 |
| 14.7 | `connectors.py:41 self.service = f"orbit:{...}"`（MATRIX 写 `:60`，漂移 19 行） | ✅ 结论成立，行号过期 |
| 14.9 | `orbit approvals {test,suggest,allow,list-rules,mode}` 实测全部存在；`test_approvals.py` 89 条 | ✅ 精确 |
| 15.1 | `orbit logs --help` 实测 6 选项全在（见新增完成项表） | ✅ 成立；目标名 3/5（见 R4） |
| 15.6 | `core.py:445 run_dump`；`--show-keys` 已加 | ✅ 精确 |
| 16.2 | `frontend/src/App.tsx`（21,740B）+ `main.py` SSE | ✅ 精确 |
| 16.3 | `frontend/src/Skills.tsx` / `Memories.tsx` / `Summaries.tsx` 均存在 | ✅ 精确 |
| 16.4 | `frontend/src/Settings.tsx`（18,835B） | ✅ 精确 |

---

## 与目标标志的差距

目标标志 = 「Hermes 有的 Orbit 都有，**逐项有验证证据**」。本轮 12.2% 里，剩下的 154 条 ⬜ **高度集中在 7 个整层为零的层**——这是与目标的最大距离。

### Top 10 未完成项（按「整层为零 × 阻塞下游」排序）

| # | 未完成项 | 现状证据 | 量级 |
|---|---|---|---|
| 1 | **L12 Gateway / 多平台层（11 条全 ⬜）** | `grep \bgateway\b` 于 `app\*.py` 仍 = 0；Hermes 22 平台适配器 + 4 状态文件 | 整层；阻塞 cron 投递、send/peer/webhook |
| 2 | **L11 cron / 自动化层（6 条全 ⬜）** | 无调度器、无持久化作业、无 trigger | 整层；Hermes `cronjob_tools.py` 99KB |
| 3 | **L13 插件层（10 条全 ⬜）** | `grep plugin` = 0；Hermes 18 组 / 103 provider / 19 种 `register_*` | 整层 |
| 4 | **L7 Skill / curator（18 条全 ⬜）** | Orbit 有技能底座（3 条手写），但多根 tier / 16 字段 frontmatter / curator 状态机 / hub 全无 | 18 条，是条目数最多的一层 |
| 5 | **L9 记忆层（16 条全 ⬜）** | 有 memories 表 + USER.md 底座；缺字符预算、冻结快照、CJK 双 FTS、`session_search` 四形态 | 16 条 |
| 6 | **L10 持久化 / 会话（15 条全 ⬜）** | 有 15 张表 + WAL + checkpoint；缺压缩谱系、幂等投递账本、孤儿回收、`orbit sessions` 19 子命令 | 15 条 |
| 7 | **L8 MCP 进阶（10 条全 ⬜）** | stdio/HTTP + 生命周期加固**已有**，但 OAuth、prompts→技能、resources→RAG、`orbit mcp` 11 子命令全无 | 10 条 |
| 8 | **L5 工具层剩余（14 ⬜ + 1 🟡）** | 29 工具 vs Hermes 94；缺 code_execution、媒体生成、视觉、TTS、kanban、cronjob、桌面窗口、协作消息、HomeAssistant | 差 65 个工具 |
| 9 | **L4 模型层剩余（8 ⬜ + 2 🟡）** | 缺 per-model 路由、推理线翻译表、auxiliary 18 slot、三级能力元数据、MoA 运行时、proxy、egress | 8 条 |
| 10 | **L2/L6 核心循环 + 注册暴露（14 ⬜）** | 子 Agent 仍零消费（`policies.subagents` 引用 0）；toolset 四层过滤、`tool_search` 分级披露、结果治理三层全无 | 14 条 |

> 附：L1 还差 12 条（`chat`/`serve`/`dashboard`/`desktop`/`gui`/`acp`/`console`/TUI/oneshot `-z`/全局 23 flag/二级 295 实现/三级 36 实现）；L16 差 3 条（18 页面语义、`skin`、browse 页）。

---

## 风险与漂移

### R1 🟠 **MATRIX 表头自计数严重过期（本轮最大的文档内部矛盾）**

`MATRIX.md:9-10` 写：「条目合计 **133**。当前：✅ **14** · 🟡 **0** · ⬜ **119**」。
**脚本逐行实测：180 行 / ✅ 22 / 🟡 4 / ⬜ 154。** 三个数字全错，且 `README.md` 也照抄了「133 条」。

- 危害：任何按表头读进度的人会低估约 8 个百分点、并误以为「没有任何部分完成项（🟡=0）」，而实际有 4 条 🟡 正是最需要人看的诚实降级。
- 附带：MATRIX 表头自己写了「计数由人工维护，以 PROGRESS.md 为准」——**那就应该由脚本重算，别人工维护**（`scripts/parity_report.py` 已存在，可扩展）。
- 另外 `ROADMAP.md:75` 写「`test_cli.py` **15 条**契约测试」，实测 **18** 条。

### R2 🟠 **6/22 条 ✅ 的证据引用已漂移（结论对，引用跳不过去）**

| 条目 | MATRIX 引用 | 实际位置 | 漂移 |
|---|---|---|---|
| 1.1 | `spec.py:190` | `spec.py:192`（`Registry` 类在 `:124`） | 2 行 |
| 1.2 | `main.py:96` | `main.py:92` | 4 行 |
| 1.3 | `main.py:47` | `main.py:34`（`:47` 是其中一个 option） | 13 行 |
| 2.9 | `config.py:113,116` | `config.py:119,122` | 6 行 |
| 14.7 | `connectors.py:60` | `connectors.py:41` | 19 行 |
| 3.6 | 3,417B / 15,177B / 27 工具 | **4,043B / 16,895B / 29 工具** | 数值全变 |

原因很清楚：实现文件在本轮持续增长（`core.py`、`providers.py`、`main.py` 都在改），行号与实测数字自然失效。**结论没有一条被推翻**，但「可复核证据」的可复核性下降——这恰恰是本项目完成判据的核心。建议每次收口用脚本重算，别手写。

### R3 🟠 **5.15 的「8 域」有一域无实据，且与 5.12/5.13 自相矛盾**

MATRIX 5.15（🟡）称「已有 file/terminal/browser/**web**/memory/skills/MCP/**元工具** 8 域」。

- 实测 29 个工具全名（`prompt-size --json`）：`run_command, vault_login, skill_save, skill_load, skill_list, retriever_search, web_search, web_extract, memory_save, memory_delete, memory_search, local_write_file, local_read_document, local_list_files, local_write_report, local_search_text, history_search_tasks, history_get_task, browser_observe, browser_navigate, browser_click, browser_click_at, browser_fill, browser_press, browser_scroll, notion_read_page, notion_append_summary, github_search_issues, github_get_issue` + 编排内建 `ask_user`（`nodes.py:923`）。
- **`todo` 与 `clarify` 两个元工具都不存在**（`grep name="todo"|name="clarify"` = 0 命中）——而 MATRIX 5.12（`todo` 元工具）与 5.13（`clarify` 元工具）**自己就标着 ⬜**。
- 同一域既算「已有」又算「未做」= 口径打架。**实际是 7 域**（若把 `ask_user` 算作 clarify 的部分实现，可写 7.5 域，但要说明）。

### R4 🟡 **15.1「六选项全对齐」成立，但日志目标只有 3/5**

- 选项面：`-n/--lines`、`-f/--follow`、`--level`、`--session`、`--since`、`--component` = **6/6 ✅ 真对齐**（上轮的缺项全补上了）。
- 位置参数：Hermes 有 `agent / errors / gateway / gui / desktop / list` 六种；Orbit 只有 `agent / errors / gateway / list` → **缺 `gui` / `desktop`**（Orbit 无 Electron 应用，可解释，但判据里该写出来）。
- 建议：证据栏补一句「目标 3/5（gui/desktop 无对应进程，见 L1 的 `desktop`/`gui` 条目 ⬜）」，否则下轮又会被当成"全对齐"。

### R5 🟡 **安全观察：默认档下 `rm -rf /` 会被放行（是决策，但必须一直可见）**

```
$ orbit approvals test -- "rm -rf /"   →  EXIT=0（allow）
```
- 原因：本机 `approvals.hardline_in_machine` 默认 False + 完全访问档 → 硬地板不生效（`approvals.py:230`）。
- MATRIX 14.2 的证据栏**已经诚实写明**这个边界，`approvals.py:20` 的模块 docstring 也写了取舍。**不算虚报。**
- 但这是全项目最需要长期盯的一条产品决策：一旦有人把 `hardline_in_machine` 的默认值当成"安全网"，`rm -rf /` 在默认配置下真的不会被拦。建议同步写进 `README.md` 的「诚实边界」段（目前只在 MATRIX 14.2 和源码 docstring 里）。

### R6 🟡 **L1 的 1.4/1.5/1.6 仍标 ⬜，但 74/407 已全量登记 → 矩阵低估本轮最大工作量**

- 1.4「顶层命令面完整（74 个）」⬜、1.5「二级子命令面完整（295 条）」⬜、1.6「三级（36 条）」⬜。
- 实测：`REGISTRY` 已有 **74 顶层 / 407 展平**，**884 个选项说明 100% 补全**，`undocumented_options` = 0。
- 也就是说：本轮体量最大的一项成果（`hermes_parity.py` 118KB 的声明表）**在矩阵里没有任何一行体现**。判据把「登记完整」和「实现完整」混在一条里，导致「做完了」也显示 ⬜。
- 建议：1.4/1.5/1.6 判据拆成「① 命令面已登记（现状 100%）」与「② 已实现（现状 43/407 = 10.6%）」两条，各自打状态。ROADMAP W2 已经这么写了（「命令面已完成，实现 43/407」），**矩阵没跟上**。

### R7 🟢 **上轮风险已消解（记录以免下轮重复报警）**

| 上轮风险 | 本轮实测 | 状态 |
|---|---|---|
| R1 自评 `state="done"` 虚高 | `_table.py:56-65` 硬编码 `state="todo"`；真实现独立登记；6 个命令选项面真补齐 | **已修（结构性）** |
| R2 三组归类为空 | `run` 15 / `platform` 6 / `ext` 9 均已填充 | **已修** |
| R3 research 缺 01 / 09 | `01-cli-surface.md`（1,735 行）、`09-core-loop-layer.md`（455 行）均已产出 | **已修** |
| R4 全部进度 untracked | `git ls-files`：`backend/app/cli` 12 文件、`handoff/hermes-parity` 510 文件、`approvals.py` / `providers.py` / `web.py` / `test_cli.py` 全部已入库（HEAD `02dff09`） | **已修** |

### R8 🟡 **未验证项（如实标注，不许当通过）**

| 项 | 为什么没验 |
|---|---|
| `orbit config edit` | 会拉起系统编辑器，属改状态操作 → **未运行** |
| `orbit doctor --fix` | 会尝试修复/写盘 → **未运行** |
| `orbit doctor --live` | 会真发网络请求探活模型端点 → **未运行** |
| `orbit model` / `moa` 运行时 | 会改默认模型/写配置 → 只看了 `--help` |
| `test_graph.py` 负载敏感偶发失败 | 本轮全量跑**未复现**（480 passed），无法确认是否存在 → 记为「未观察到」，不下结论 |
| Hermes 侧 48 profile 的逐家差异 | 依赖 06 调研的【推断】部分，本轮未重新取证 |

---

## 下一轮建议

按优先级（**先修计数与引用，再补层**——计数不准时，补层的成果无法验收）：

1. **【P0】用脚本重算 MATRIX 表头 + README 的「133 条」**：现在真实是 180 行 / ✅22 / 🟡4 / ⬜154。别再人工维护计数（表头自己都写了「以 PROGRESS.md 为准」，那就让脚本算）。顺手修 `ROADMAP.md:75` 的「15 条测试」→ 18。
2. **【P0】一次性修掉 R2 的 6 处漂移引用**（1.1/1.2/1.3/2.9/14.7 的行号 + 3.6 的三个数值）。建议写个 `scripts/matrix_evidence_check.py`：给定 `文件:行`，校验该行是否含预期符号，不匹配就报出来——**这是让「可复核证据」真的可复核的唯一办法**。
3. **【P0】拆 L1 的 1.4/1.5/1.6 判据**：把「命令面已登记（100%）」与「已实现（43/407）」分成两条各自打状态，让 `hermes_parity.py` 这项本轮最大成果在矩阵里可见（ROADMAP W2 已经这么写，矩阵跟上即可）。
4. **【P1】修 5.15 的「8 域」→ 7 域**（去掉「元工具」，或写明「仅 `ask_user`，`todo`/`clarify` 见 5.12/5.13 ⬜」），消除与 5.12/5.13 的自相矛盾。
5. **【P1】15.1 证据栏补「日志目标 3/5，缺 gui/desktop」**；同时把 `hardline_in_machine` 的默认值决策写进 `README.md` 的「诚实边界」段（现在只在 MATRIX 14.2 与源码 docstring）。
6. **【P1】下一波优先做 W5 / W6 / W8（Skill / 记忆 / 持久化）**：这三层 Orbit 本来就有可用底座（技能文件、memories 表 + USER.md、15 张表 + WAL + checkpoint），是**最快能把 ⬜ 变 ✅ 且测试好写**的三层；反过来 W9（cron/gateway）整层为零、依赖面最广，放后面。
7. **【P2】给「每条 ✅」补一条可执行验证命令**：现在 22 条里 16 条证据是「文件:行」，只有少数带命令输出。目标标志要求「逐项有验证证据」，建议每条 ✅ 加一列「验证命令」（如 `orbit approvals list-rules | grep -c mkfs`），让下轮监视可以**一键复跑**而不是重新读代码。
8. **【P2】下轮复核 `08-orbit-current-state.md` 的「15 条已声明未消费」**：本轮只复核了 `policies.subagents`（引用数仍为 0），其余 14 条（`Tool.confirmation` / `granted()` / `budget["summary_mark"]` / `context_meta["compressed"]` 等）是否随本批施工接线，尚未核。

---

*本报告由 monitor 角色只读生成。22 条 ✅ 与 4 条 🟡 全部亲自复核（100% 抽样），未为好看加分；未验证项已单列，不混进完成项。所有数字均可按上文命令复现。*
