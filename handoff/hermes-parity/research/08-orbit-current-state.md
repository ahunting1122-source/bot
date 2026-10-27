# Orbit 现状能力矩阵（对标 Hermes 的基线）

日期：**2026-10-27 23:09（本机实测时钟；读数快照）**
性质：**只读调研**。本文不改任何文件、不装依赖、不跑改数据的命令。
用途：给「对标 Hermes 补齐功能」提供**准确、可核验**的基线。所有结论都带 `文件:行号` 证据，未找到证据的一律标"缺失"，不靠推测。

> ## ⚠️ 读数时点与并发变更（先读这一条）
>
> **本仓库在我调研的 15 分钟里正被并发修改。** 实测证据（按时间顺序）：
>
> | 时刻 | 事实 | 证据 |
> |---|---|---|
> | 起始 | 本机时间是 **2026-10-27**，而 `handoff/PROGRESS-WATCH.md` 最后一次巡检是 **2026-10-13**（差约 2 周） | `Get-Date`；`PROGRESS-WATCH.md:341` |
> | 起始 | 已提交基线 = **`0964821`**（`chore: 清理预算策略的过期文档串与死配置，忽略 .dsh/`） | `git log --oneline -3` |
> | 23:03 | **`backend/app/cli/` 出现**（`spec.py`/`context.py`/`main.py`/`__init__.py`/`__main__.py`），`git status` 显示 **untracked** | `git status --porcelain` → `?? backend/app/cli/` |
> | 23:05 | 又新增 `commands/core.py`(28KB) —— **仍在写入中** | 两次 `Get-ChildItem` 结果不同 |
> | 23:08 | 新增 `backend/tests/test_cli.py`(15 用例) 与 `scripts/parity_report.py`；`pyproject.toml` 被改（**加了 `[project.scripts] orbit = "app.cli.main:main"`**） | `git status` → ` M backend/pyproject.toml`、`?? backend/tests/test_cli.py`、`?? scripts/parity_report.py`；`git diff` |
> | 23:09 | 测试收集数 **279 → 294** | `pytest --collect-only -q`（两次实跑） |
>
> **因此：**
> 1. 本文所有"现状"= **HEAD `0964821` + 2026-10-27 23:09 的工作区**。凡标"缺失"的项，若恰是别人正在施工的，请先跑 `git status` / 看 mtime 复核。
> 2. **受影响的结论只有"CLI 入口层"**——它从"缺失"→"**施工中、未提交、未安装**"（详见 §2 第 1 行、§6 第 1 条）。其余 15 层我逐个用 grep 复核过锚点（`tools.py:315/391/437/615/726`、`policies.py:51-209/356/586/603/607/624/627/631` 等），**未发现漂移**。
> 3. 若需要严格可复现的基线，请在**无并发写入**时按 §附录 的命令重跑一遍。

---

## §1 一句话定位 + 技术栈 + 代码规模

### 1.1 一句话定位

> Orbit = **网页操作特化的本地单用户 Agent**：给一句自然语言目标，它在**真实可见的浏览器**里自己判断去哪个站点、打开进入、站内完成多步操作；本地文件、跨对话记忆、RAG 检索是辅助能力。无域名白名单，任意 `http(s)` 站点可访问。

定位原文见 `README.md:1-4`、`handoff/00-CONTEXT.md:7`、`MVP_PLAN.md:7-17`。

与 Hermes 的形态差异（本文反复用到，先说清）：Hermes 是**五层单体 harness + 多入口（CLI/IDE/消息平台/cron）**；Orbit 历史上是**单体 harness + 单入口（Web 工作台）**。中间的执行/工具/技能层 Orbit 做得反而更深，而**入口层与接入层是短板**——不过截至本文读数时点，**入口层正在被并发施工**（`backend/app/cli/` 刚创建，见文首警告框）。

### 1.2 技术栈

| 面 | 选型 | 证据 |
|---|---|---|
| 语言/运行时 | Python `>=3.12,<3.14` | `backend/pyproject.toml:5` |
| Web 框架 | FastAPI + uvicorn（单 worker，不启用 reload） | `pyproject.toml:7-8`；`README.md`（启动节） |
| 编排 | LangGraph `>=1,<2`（`StateGraph`）+ `langgraph-checkpoint-sqlite` | `pyproject.toml:13-14`；`app/graph/builder.py:7`；`app/agent.py:7,42-45` |
| 模型客户端 | `langchain-openai`（`ChatOpenAI`，OpenAI 兼容 chat-completions） | `pyproject.toml:15`；`app/agent.py:282-288` |
| 存储 | SQLAlchemy async + aiosqlite（SQLite 单文件 + WAL） | `pyproject.toml:10-11`；`app/storage.py:277-294` |
| 迁移 | Alembic | `pyproject.toml:12`；`backend/alembic.ini`；`app/main.py:92-95` |
| 浏览器 | Playwright（可见窗口） | `pyproject.toml:17`；`app/browser.py:6` |
| 检索 | SQLite FTS5（trigram + 字符级两路） + `sqlite-vec` vec0 + `fastembed`/`onnxruntime` 嵌入 + cross-encoder 重排 | `pyproject.toml:20-24`；`app/indexing.py`；`app/vectors.py`；`app/rerank.py` |
| MCP | `mcp>=2.2.0`（stdio + streamable HTTP） | `pyproject.toml:20`；`app/mcp.py:30-32` |
| 密钥 | 系统 keyring（Windows 凭据管理器） | `pyproject.toml:19`；`app/vault.py:6`；`app/mcp.py:29` |
| 文档解析 | `pypdf`（仅 PDF） | `pyproject.toml:18`；`app/tools.py:560-564` |
| 前端 | React `19.2.8` + Vite `^7` + TypeScript `~5.9` + react-markdown/remark-gfm + lucide-react | `frontend/package.json:11-30` |

### 1.3 代码规模（命令统计，排除 `.venv` / `__pycache__` / `pytest-of-*`）

> **口径警告（本仓特有）**：本仓源码是 **UTF-8 无 BOM + LF**，PowerShell 的 `Get-Content` / `Measure-Object -Line` 会误判编码并**吞掉行数**（实测 `app/context.py`：`Get-Content` 报 127 行，实际 177 行）。**下表的行数一律由 `backend\.venv\Scripts\python.exe` 读 `splitlines()` 得出**，是可信口径。

| 范围 | 文件数 | 总行数 | 非空行 | 统计口径 |
|---|---|---|---|---|
| `backend/app/**.py`（**不含**新增 `cli/`） | **23** | **9,029** | 7,938 | `app.rglob('*.py')` 排除 `__pycache__` |
| `backend/app/cli/**.py`（**新增、untracked、施工中**） | **7** | **1,478** | 1,261 | `spec.py`/`context.py`/`main.py`/`commands/core.py`/`__init__.py`/`__main__.py`/`commands/__init__.py` |
| `backend/app` 合计 | **30** | **10,507** | 9,199 | |
| `backend/migrations/versions/*.py` | **10** | 505 | 420 | `0001`–`0007`、`0009`–`0011`（**`0008` 空缺**） |
| `backend/tests/*.py` | **27**（23 个 `test_*.py` + `conftest.py` + 3 个 MCP fixture） | 6,424 | 5,203 | 含新增 `test_cli.py`；`fixtures/echo|rich|stall_mcp_server.py` |
| **后端 Python 合计** | **67** | **17,436** | 14,822 | app(含 cli) + migrations + tests |
| `scripts/*.py` | **18** | 4,210 | 3,654 | 含新增 `parity_report.py`；另有 `eval_cases.json`(223 行) + `start.ps1`(11 行) |
| **Python 总计** | **85** | **21,646** | **18,476** | backend + scripts |
| `frontend/src/**/*.ts(x)` | **13** | **3,749** | 3,636 | |
| `frontend/src/**/*.css` | **4** | **3,421** | 3,404 | |
| 顶层 `.md` 文档 | **15** | — | — | `ARCHITECTURE.md` 43.5KB 最大 |
| `handoff/*.md` | **25** | — | — | `vector-path-plan.md` 50KB、`PROGRESS-WATCH.md` 45KB 最大 |

**最大单文件（后端）**：`app/tools.py` **1,162** > `app/graph/nodes.py` **928** > `app/main.py` **805** > `app/cli/commands/core.py` **755** > `app/mcp.py` **694** > `app/policies.py` **632** > `app/browser.py` **625** > `app/indexing.py` **541**。

**规模画像**：~18.5k 非空行 Python + 3.6k 前端 TS + 3.4k CSS。后端**注释/文档串密度极高**（`tools.py`、`policies.py`、`mcp.py` 里大量段落是设计取舍记录），这意味着**"行数"不能当复杂度指标读**——真实逻辑密度约是行数的 1/2 到 2/3。

---

## §2 分层现状表

层划分按 Hermes 的形态（`handoff/2026-10-08-hermes-deep-dive.md:9` 的五层 + `2026-10-08-harness-survey-hermes-vs-dsh.md:24-34` 的九层补充），逐层给 Orbit 现状。成熟度口径：**完整**＝该层职责在本仓有实现且被主链路消费；**部分**＝有实现但有明确缺口或 seam 空转；**缺失**＝零实现。

| # | 层 | Orbit 现状 | 证据（文件:行） | 成熟度 |
|---|---|---|---|---|
| 1 | **CLI 入口层** | **施工中（本次调研期间落地，尚未提交/安装）**。`backend/app/cli/` 已成包（**7 文件 / 1,478 行**）：`spec.py` 声明式命令注册表（`Command`/`Option`/`Registry` + 归类 `core/run/ops/platform/ext/internal` + 对标优先级 `P0/P1/P2` + 状态 `done/partial/todo`），`main.py` 把注册表编译成 argparse 嵌套 subparser 并统一分发/统一错误出口，`context.py` 提供运行时上下文（配置/存储/异步桥/输出格式），`commands/core.py`(755 行) 是业务实现。**已实现 7 个顶层命令**：`status`、`doctor`（8 项自检）、`dump`、`logs`、`prompt-size`、`completion`、`config`（`list/get/set/unset/path` 5 子命令），全部 `state="done"`。入口声明已加：`[project.scripts] orbit = "app.cli.main:main"`（**但这是未提交的工作区改动**，`uv sync` 前 `orbit` 装不出来；等价替代 `python -m app.cli`）。**仍是半成品**：`git status` 显示整个 `backend/app/cli/` **untracked**；`commands/` 下只有 `core.py`，而 `run`（对话/服务/网关/定时/委派）、`platform`、`ext` 三组**只有归类声明、零实现**。历史入口仍是 ASGI 对象 + `scripts/start.ps1`。 | `backend/app/cli/spec.py`（注册表/归类/优先级）、`main.py`（argparse 编译）、`context.py`、`commands/core.py:657-782`（命令声明）、`commands/core.py:76,246,354,363,373,396,420,436,490,551,647`（handler 实现）；入口声明 `backend/pyproject.toml`（`[project.scripts]`，`git diff` 可见，未提交）；状态 `git status` → `?? backend/app/cli/`；旧入口 `backend/app/main.py:805` + `scripts/start.ps1` | **部分**（骨架+7 命令+入口声明已落地；未提交、未安装、4 组中 3 组空） |
| 2 | **Agent 核心循环** | LangGraph `StateGraph`，8 节点：`prepare → think ⇄ gate → execute → digest → budget →（reflect → think / finalize）`。Runner 负责队列/单 worker/取消/重启恢复。模型调用带 3 次重试（仅在未吐出正文时重试），流式按 60ms/80 字符节流发事件。三类预算：动作 40 硬拦、token 200k 硬拦、时间 360s **软闸**（到点转后台 + 定期进度报告）。 | 图：`app/graph/builder.py:31-52`（节点 `:34-41`，边 `:43-51`）；节点实现 `app/graph/nodes.py:111/294/457/504/596/694/720/782`；Runner `app/agent.py:19-146`；`recursion_limit=200` `app/agent.py:149`；重试 `app/graph/nodes.py:39,350-374`；流式 `:376-454`；预算 `app/policies.py:304-353` + `app/graph/nodes.py:720-779` | **完整** |
| 3 | **上下文装配** | 单条 `SystemMessage` 拼装：`agent.md` 正文 + 权限级别说明 + 配置网址 + 画像块 + 12 条记忆 + 会话摘要 + 最近 30 条消息（24000 字符预算）。**冻结前缀**机制：任务内系统提示算一次、之后每步复用同一字符串（保提示缓存 + 任务内自洽）。技能简述与检索片段作为 `extra` 追加在系统提示尾部。 | `app/context.py:86-141`（装配）、`:12`（`PROMPT_VERSION`）、`:61`（`SYSTEM` 从 `app/agent.md` 加载，缺失时回退 `:22-43`）；冻结：`app/graph/nodes.py:139-157`、`:294-310`，`app/context.py:145-177`；技能注入 `app/graph/nodes.py:178-190,212-291`；检索注入 `:159-177` | **部分**（无三层分层、无项目文件加载与注入扫描、无压缩谱系） |
| 4 | **模型适配（provider）** | **只有一条路径**：`ChatOpenAI` + `model_base_url`（OpenAI 兼容端点，可指向任意兼容服务）。无 Anthropic Messages / Bedrock / Codex / 多 provider 归一化；无 fallback 链、无多 key 轮转。错误处理只有 5 条异常名 → 中文文案的映射。 | `app/agent.py:277-288`（`get_model`）；`app/config.py:105-107`；`app/summaries.py:8-9`（摘要另起一次 `ChatOpenAI`）；错误映射 `app/agent.py:204-215` | **部分**（单一 provider，够用但无适配层） |
| 5 | **工具层** | 本仓最强的层之一。`Tool` 数据类（name / description / Pydantic schema **或** 外部 JSON Schema / confirmation / source）；`ToolRegistry` 是**唯一分发点**：参数校验 → 幂等短路（`operation_id` 已 `DONE` 直接复用）→ 落库 → `tool_started` 事件 → 并发闸 → 本地或外部执行 → `refused/error` 分类 → 状态落 `DONE`/`WAITING` → `tool_finished` 事件。结果治理：16000 截断 + sha256 checksum 幂等 + token 估算 + `structured` 字段剥离出上下文。 | `app/tools.py:282-312`（`Tool`）、`:615-765`（`ToolRegistry`，`register:681` / `unregister_source:704` / `specs:710` / `validate:713` / `execute:726`）；结果治理 `app/policies.py:235-301`；投影剥离 `:240-248`；`TOOL_CONCURRENCY=4` `app/tools.py:26` | **完整** |
| 6 | **工具注册与 toolset** | **注册有、暴露无**。注册支持"同 source 重注册允许 / 跨 source 遮蔽拒绝并记日志"（照抄 Hermes 的 registry 规则）；但**暴露层是直通**：`DefaultToolExposer.expose()` 原样返回，`think` 每步把**全部**工具塞给模型。无 toolset 分组、无 `check_fn`/`requires_env`、无按平台/场景/profile/委托的四层过滤。 | `app/tools.py:681-708`（注册/卸载）；`app/policies.py:91-94`（Protocol）+ `:356-358`（直通实现）；`app/graph/nodes.py:319`（`expose(registry.specs())`）；`app/tools.py:710-711`（`specs()` 全量） | **部分**（seam 存在但空转） |
| 7 | **Skill 层** | 本仓最成熟的一层，**比 Hermes 的证据口径更严**。文件是真值（`data/skills/<name>/SKILL.md`），表是索引。四道闸全部落地：① 写入侧内容守门（拒绝环境故障/负面断言/瞬时错误/一次性叙述/未解决失败）；② 同名更新 + 同 scope 触发词重叠→互标"备选"；③ 注入择优（过滤 unreviewed/archived → scope>成功率>用量>名字 → 同簇只留冠军，上限 5）；④ 终态回写降级淘汰（连续失败标 stale、超期/低成功率归档）。三工具 + 完整 CRUD API 面。 | 解析/落盘/目录 `app/skills.py:69-184`；镜像 `:323-387`；写入守门 `:241-319`；查重 `:209-233`；择优 `app/graph/nodes.py:212-265`；淘汰 `app/policies.py:438-493`；工具 `app/tools.py:371-387,1015-1157`；API `app/main.py:542-735`；迁移 `0006/0007/0011` | **完整** |
| 8 | **MCP 层** | 完整且做了生命周期加固。stdio + streamable HTTP 双传输；密钥走 keyring（`keyring:<name>` 占位解析，明文不落 `mcp.json`）；5 态状态机（idle/disabled/ready/reconnecting/error）；**事件式存活检测**（新协议 `client.listen()` 抛 `SubscriptionLost`，老协议退回 `send_ping` 轮询 + 抖动宽限）；指数退避重连（2→60s）+ 失联即摘工具避免模型调死工具；prompts→技能第二来源；resources→RAG 后台索引（限速 + 翻页上限）。 | `app/mcp.py:287-304`（连接）、`:233-264`（密钥）、`:75-79`（状态）、`:497-577`（看门狗）、`:73,579-602`（退避）、`:379-443`（prompts）、`:445-488`（resources）、`:654-694`（执行/归一）；装配 `app/main.py:142-152` | **完整** |
| 9 | **记忆层** | 三层，与 Hermes 同构：① `memories` 表 key-value（12 条注入、同义 key 自动合并、`hit_count`/`last_used_at` 观测"死数据"）；② 画像 `data/profile/USER.md`（1600 字符上限、`§` 分隔、整段注入、文件真值表为索引）；③ 技能文件（见 #7）。写入门槛比 Hermes **更保守**：harness 侧确定性拦截，只认用户最近一条消息里的显式"记住"指令，并有否定/疑问 veto 表。 | `app/storage.py:131-149`（Memory）、`:40-66`（同义合并）、`:337-361`（`set_memory`）；观测 `app/context.py:64-83`；画像 `app/profile.py`（`PROFILE_MAX_CHARS=1600`、`SEPARATOR="§"`）；注入 `app/context.py:116-119`；写入门槛 `app/tools.py:45-139,914-931`；API `app/main.py:394-443`；迁移 `0009/0010` | **完整** |
| 10 | **持久化与会话** | SQLite 单文件 + WAL + 长超时；LangGraph checkpoint 独立库；事件流表 + SSE 断点续传（游标 `after` / `Last-Event-ID`、1s 心跳、终态 `settled` 收尾）；任务状态机 6 态 + 重启恢复（PENDING 重入队、RUNNING 转 WAITING_USER 并要求重新观察）；`Operation` 幂等；取消保留部分正文。 | `app/storage.py:275-361`（`Store`、WAL `:284-294`）；checkpoint `app/agent.py:42-45`；事件 `app/storage.py:122-128,324-325`；SSE `app/main.py:286-332`；恢复 `app/agent.py:47-61`；幂等 `app/tools.py:732-737`；部分正文 `app/agent.py:265-275` | **完整**（缺 `messages.tool_call_id`、`artifacts.checksum`；session 不跨入口） |
| 11 | **cron 自动化** | **不存在**。`backend/app` 全量 grep `cron|schedul` 只命中 `SummaryService.schedule()` —— 那是**进程内 asyncio 后台摘要任务**，不是定时调度。无调度器、无持久化作业、无 trigger、无"无人值守安全边界"。 | grep 结果：`app/agent.py:225`、`app/main.py:342`、`app/summaries.py:115,136,150`（全部为摘要调度）；`ROADMAP.md:35`（"定时与触发式任务"仍是待办） | **缺失** |
| 12 | **gateway 多平台** | **不存在**。grep `gateway` 在 `backend/app` 零命中。只有 loopback HTTP + React 前端；本地会话中间件硬性限定 `localhost/127.0.0.1/testserver` + Origin 白名单 + `orbit_session` cookie。无 Telegram/Discord/Slack/WhatsApp/webhook 任何接入。 | `app/main.py:175-187`（本地会话中间件）；`app/config.py:188-193`（`allowed_origins`）；`handoff/2026-10-08-harness-survey-hermes-vs-dsh.md:175`（"20+ 消息平台网关：超出 Orbit 定位"——这是**有意不做**） | **缺失**（有意） |
| 13 | **插件系统** | **不存在**。grep `plugin` 在 `backend/app` 零命中。唯一扩展点是 `policies.py` 的 **13 个 Protocol** 在启动期由 `Policies.default()` 注入——这是 dsh 意义上"seam 的 Service Definition 部分"，但**无热插拔、无卸载、无生命周期钩子（pre/post tool call）、无副作用撤销**。 | `app/policies.py:51-212`（13 个 Protocol）、`:593-632`（`Policies` dataclass + `default()`）；注入点 `app/agent.py:26`、`app/main.py:148-152` | **缺失** |
| 14 | **安全与审批** | **部分，且是有意偏离 Hermes**。v1 定为"完全访问、事前零确认"，`gate` 节点里确认分支已整体移除。现存防线：① 文件/命令**两档权限**（仅工作文件夹 / 完全访问），`resolve_path` 是唯一落地点；② **不可逆命令固定黑名单 14 条**（格式化/关机/删卷影/bcdedit/裸写块设备/递归删根…）——**只在窄档生效**；③ 密码不经模型（keyring + `vault_login` 一次完成填+提交）；④ 工作台自身 DB/配置文件禁止工具改写；⑤ 站点级免确认 grants 写入；⑥ MCP 工具默认 `confirmation=True`；⑦ 本地会话 token + Origin/host 双校验。**缺口**：无 Smart/Manual/Off 分级、无 per-session approval、无审批请求/响应钩子、machine 档下 `run_command` 零拦截（无沙箱）。 | `app/graph/nodes.py:457-461`（零确认注释）；`app/config.py:37-55,199-205`（权限档）；`app/tools.py:391-407`（`resolve_path`）、`:437-464`（黑名单）、`:866-878`（DB 防写）、`:892-906`（命令两档）；`app/vault.py`；`app/browser.py:74-80`（`vault_login`）；`app/mcp.py:362`；`app/main.py:175-187` | **部分** |
| 15 | **可观测性** | **部分：有事件流，无指标/追踪**。`task_events` 是唯一观测面，约 24 类事件（`task_created/task_started/node_entered/message_started/message_delta/message_finished/activity_summary/tool_started/tool_finished/tool_result_stored/chunks_indexed/vectors_indexed/retrieval_injected/skills_injected/reflection/budget_stopped/backgrounded/background_report/citation_checked/finalize_completed/waiting_user/task_completed/task_failed/task_cancelled/summary_cost`），前端 SSE 消费。token 是**估算**（CJK 按字、其他 4 字符≈1 token），任务收尾汇总进 `task_completed`；成本汇总单列摘要开销。**无** OpenTelemetry / Prometheus / LangSmith / 结构化日志落盘（7 个模块只 `logging.getLogger`，无 handler 配置）。 | 事件写入 `app/storage.py:324-325`；估算 `app/policies.py:30-33`；计量 `app/graph/nodes.py:430-451`；成本 `:822-842`；日志器 `app/agent.py:16` 等 7 处 | **部分** |
| 16 | **Web UI (dashboard)** | **完整，但形态是"工作台"不是"仪表盘"**。React 19 + Vite，6 个视图：工作台（对话）/ 对话摘要 / 长期记忆 / 技能 / 密钥存储 / 连接与设置。13 个 ts/tsx（3,749 行）+ 4 个 css（3,421 行）。SSE 消费 + 断线按游标补读 + token 计量条 + 后台任务提示。**无**指标仪表盘、无 cron 管理页、无审计时间线页、无工具开关页。 | 视图枚举与导航 `frontend/src/App.tsx:34,50-57`；路由分发 `:321-349`；SSE 客户端 `frontend/src/useWorkspace.ts`(256 行)；`frontend/package.json:6-10` | **完整** |

### 2.1 一句话汇总

**强在中间三层**：工具层 / Skill 层 / MCP 层（外加记忆层与检索链路）——这三层的实现深度已经超过 Hermes 的对应部分（尤其 Skill 治理与 MCP 生命周期加固）。
**弱在两端 + 横向**：接入层（gateway / cron）、插件与钩子层、provider 适配层——**整段缺失**；入口层（CLI）**刚开工、未提交、4 组命令中 3 组空**。
**半成品**：上下文装配（缺分层与压缩谱系）、工具暴露（seam 空转）、安全审批（零确认是有意取舍，但缺分级与沙箱）、可观测性（有事件无指标）。

> 口径提醒：本节把"有骨架但未接线"记作 **部分** 而不是 **缺失**——因为补它的成本是"接线"而非"从零建"。§6 则只列**真正的零实现**，两节互补不重复。

---

## §3 工具清单（模型当前可见的全部工具）

来源三个注册点 + 一个动态源。**静态内置 28 个**，另有 `mcp__<服务>__<工具>` 动态注册。

### 3.1 本地 / 记忆 / 历史 / 检索 / 技能（`app/tools.py:315-388`，15 个）

| # | 工具名 | 用途 | 注册行 |
|---|---|---|---|
| 1 | `local_list_files` | 列出目录内容（窄档只看授权目录，完全访问档可从盘符开始） | `tools.py:316` |
| 2 | `local_search_text` | 在文本文件里搜关键词，返回路径 + 摘录 | `tools.py:322` |
| 3 | `local_read_document` | 读文档（≤16000 字符；TXT/MD/文本 PDF，完全访问档不限扩展名） | `tools.py:328` |
| 4 | `local_write_report` | 在授权目录创建 Markdown 报告（不覆盖不同内容）并登记 artifact | `tools.py:334` |
| 5 | `local_write_file` | 写入/改写任意文本文件（窄档需 `overwrite=true` 且不碰工作台自身 DB） | `tools.py:335` |
| 6 | `run_command` | 在本机执行 shell 命令，返回 stdout/stderr/退出码（窄档限 cwd + 黑名单） | `tools.py:341` |
| 7 | `memory_search` | 按关键词搜跨对话长期记忆 | `tools.py:349` |
| 8 | `memory_save` | 保存/更新长期记忆（**harness 强制只认用户显式"记住"**；`confirmation=True`） | `tools.py:350` |
| 9 | `memory_delete` | 删除长期记忆（键名部分匹配；`confirmation=True`） | `tools.py:357` |
| 10 | `history_search_tasks` | 搜其他对话的历史任务目标 | `tools.py:363` |
| 11 | `history_get_task` | 读指定历史任务的目标/状态/产物 | `tools.py:364` |
| 12 | `retriever_search` | 检索此前读过的网页正文、工具结果、本地文件片段（带来源地址；每任务上限 4 次） | `tools.py:365` |
| 13 | `skill_load` | 读一个已登记技能的完整正文（也路由 `mcp__*` 到远程 prompt） | `tools.py:371` |
| 14 | `skill_list` | 列出全部技能名与触发条件 | `tools.py:377` |
| 15 | `skill_save` | 把新流程沉淀成 SKILL.md（走写入守门 + 查重 + 矛盾转备选） | `tools.py:382` |

### 3.2 编排内建（`app/graph/nodes.py:922-925`，1 个）

| # | 工具名 | 用途 | 注册行 |
|---|---|---|---|
| 16 | `ask_user` | 请求澄清或浏览器人工接管（`interrupt()` 挂起，回答后继续原任务）；有"先自己试过"的门槛 | `nodes.py:922-925` |

### 3.3 浏览器 / Notion（`app/browser.py:56-88`，10 个）

| # | 工具名 | 用途 | 注册行 |
|---|---|---|---|
| 17 | `browser_observe` | 观察当前页面：正文 + 带版本号的可交互元素 ref（含中心坐标） | `browser.py:57` |
| 18 | `browser_navigate` | 打开任意 `http(s)` 网址，回报是否真进入（`entry_weak` 标记） | `browser.py:58` |
| 19 | `browser_click` | 点击最新观察中的元素（`confirmation=True`） | `browser.py:63` |
| 20 | `browser_click_at` | 视口坐标点击（视觉兜底：ref 失效时按截图报坐标；`confirmation=True`） | `browser.py:64` |
| 21 | `browser_fill` | 填写最新观察中的输入元素（`confirmation=True`） | `browser.py:71` |
| 22 | `browser_press` | 按键 Enter/Tab/Escape/ArrowDown（`confirmation=True`） | `browser.py:72` |
| 23 | `browser_scroll` | 滚动页面（±2000px） | `browser.py:73` |
| 24 | `vault_login` | 用 keyring 里的站点账号完成填+提交登录，密码不经过模型（`confirmation=True`） | `browser.py:74` |
| 25 | `notion_read_page` | 读取设置中指定的 Notion 页面 | `browser.py:81` |
| 26 | `notion_append_summary` | 向配置的 Notion 页面追加摘要并回读验证（`confirmation=True`） | `browser.py:82` |

### 3.4 GitHub 连接器（`app/connectors.py:27-34`，2 个）

| # | 工具名 | 用途 | 注册行 |
|---|---|---|---|
| 27 | `github_search_issues` | 在配置的仓库搜 Issue（≤10 条 + 来源链接） | `connectors.py:28` |
| 28 | `github_get_issue` | 读指定仓库的 Issue 详情 | `connectors.py:33` |

### 3.5 动态源：MCP（数量不定）

`mcp__<sanitize(server)>__<sanitize(remote)>`，名字截断 64 字符，与内置工具永不同名。每个工具带服务器给的 JSON Schema 原样透传（不假装校验）。`auto_approve=false`（默认）时 `confirmation=True`。

证据：`app/mcp.py:108-110`（命名）、`:355-370`（注册）；`/api/tools` 可实时查看（`app/main.py:516-522`）。

> ⚠️ 注意：**没有 `web_search` / `web_extract` 工具**。全仓 `backend/app` grep `web_search|web_extract` 只命中一处 —— `tools.py:268` 里作为**示例技能名**出现在 `SkillLoadArgs.name` 的描述字符串中。`data/skills/web_search/SKILL.md` 是一个"站点内搜索怎么做"的**流程技能**，不是联网搜索工具。Orbit 的上网能力 100% 依赖浏览器。

---

## §4 数据模型

### 4.1 迁移链（`backend/migrations/versions/`，10 个版本文件）

```
0001_initial → 0002_conversation_summaries → 0003_tool_results → 0004_chunks
→ 0005_vector_table → 0006_skills → 0007_skill_governance
→ [0008 空缺，预留给审计时间线] → 0009_memory_hygiene → 0010_user_profile → 0011_skill_source
```

`0008` 的空缺是**有意预留**，`0005_vector_table.py` 的 docstring 明写"`0008` 因此不被本方向占用（留给审计时间线）"；`handoff/PROGRESS-WATCH.md:360` 记"已空缺 5 个编号周期，编号空洞有堆积风险"。下一个可用编号 = `0012`。

### 4.2 表清单

**A. 11 张 ORM 表（`app/storage.py`）**

| # | 表名 | ORM 类 | 主键 | 关键列 / 用途 | 证据 |
|---|---|---|---|---|---|
| 1 | `conversations` | `Conversation` | `id` | `title`、`summary`、`message_revision`、`summary_status/error/updated_at/message_id/message_count`、`summary_tokens`、`summary_cost_seconds` | `storage.py:81-98` |
| 2 | `messages` | `Message` | `id` | `conversation_id`、`role`、`content`、`created_at`（**无 `tool_call_id`**） | `storage.py:101-107` |
| 3 | `tasks` | `Task` | `id` | `conversation_id`、`goal`、`status`（6 态）、`waiting`(JSON)、`error` | `storage.py:110-119` |
| 4 | `task_events` | `TaskEvent` | `id`(自增) | `task_id`、`event_type`、`payload`(JSON) —— SSE 的唯一数据源 | `storage.py:122-128` |
| 5 | `memories` | `Memory` | `id` | `key`(唯一,160)、`value`、`source`、`hit_count`、`last_used_at` | `storage.py:131-149`；`hit_count/last_used_at` 由 `0009` 加 |
| 6 | `profiles` | `Profile` | `id` | `user_id`、`body`、`entries`、`chars`、`max_chars`(1600) —— 画像索引，正文真值在 `data/profile/USER.md` | `storage.py:152-171`；`0010` 建表 |
| 7 | `skills` | `Skill` | `id` | `name`+`source` 复合唯一、`scope`、`triggers`(JSON)、`version`、`body`、`path`、`usage_count`、`success_rate`、`failures`、`consecutive_failures`、`last_used_at`、`status`(4 态) | `storage.py:174-203`；`0006/0007/0011` |
| 8 | `tool_calls` | `Operation` | `id`(`task_id:call_id`) | `name`、`status`(`RUNNING`/`DONE`/`WAITING`)、`args`(JSON)、`result`(JSON) —— 幂等与恢复的落点 | `storage.py:206-213` |
| 9 | `artifacts` | `Artifact` | `id` | `task_id`、`kind`、`location`、`label`（**无 `checksum`**） | `storage.py:216-222` |
| 10 | `tool_results` | `ToolResult` | `id`(自增) | `operation_id`(唯一)、`name`、`status`、`host`、`text`、`bytes`、`tokens`、`truncated`、`hash` | `storage.py:225-243`；`0003` |
| 11 | `chunks` | `Chunk` | `row_id`(自增，兼 FTS5 rowid) | `id`(唯一64)、`source_type`、`source_id`、`conversation_id`、`task_id`、`host`、`ordinal`、`text`、`char_text`、`tokens`、`hash`、`origin`、`title_path` | `storage.py:246-268`；`0004` |

**B. 2 张 FTS5 虚表 + 触发器（`0004_chunks.py`）**

| 表 | 类型 | 用途 |
|---|---|---|
| `chunks_fts_trigram` | `fts5(text, content='chunks', content_rowid='row_id', tokenize='trigram')` | 长词/英文/数字检索 |
| `chunks_fts_chars` | `fts5(char_text, content='chunks', content_rowid='row_id', tokenize='unicode61')` | 中文 2 字查询（trigram 对 2 字中文返回 0 条） |
| `chunks_ai` / `chunks_ad` / `chunks_au` | AFTER INSERT/DELETE/UPDATE 触发器 | 保持两张 FTS 表与 `chunks` 同步 |

**C. 2 张向量表（`0005_vector_table.py`）**

| 表 | 类型 | 用途 |
|---|---|---|
| `chunk_vectors` | `vec0` 虚表（`float[768]`，维度建表时钉死） | 向量近邻检索 |
| `vector_meta` | 普通表 | 记录 `(model, dim, created_at)`，启动时 `ensure_schema()` 比对，不一致就 DROP 重建 |

**物理表合计 15 张**（11 ORM + 2 FTS5 + 2 向量）。

**D. 非 SQLite 的持久化面**

| 载体 | 内容 | 证据 |
|---|---|---|
| `data/app.sqlite` | 主库（上面 15 张表） | `app/config.py:95`；`app/main.py:106` |
| `data/checkpoints.sqlite` | LangGraph `AsyncSqliteSaver` checkpoint | `app/agent.py:42-45` |
| `data/settings.json` | 连接器配置（模型名/base_url/仓库/Notion/浏览器 URL/grants/file_scope） | `app/connectors.py:40,57-` |
| `data/mcp.json` | MCP 服务声明（`name/enabled/auto_approve/index_resources/url|command/args/env/headers`） | `app/mcp.py:167,190-231` |
| `data/skills/<name>/SKILL.md` | 技能正文真值 | `app/skills.py:113-160` |
| `data/profile/USER.md` | 用户画像正文真值 | `app/profile.py`（`FILE_NAME="USER.md"`） |
| 系统 keyring（service=`orbit:<data_dir>`） | `model_api_key`、`github_token`、`site:<host>`、`mcp:<server>:<name>` | `app/vault.py:11-16`；`app/mcp.py:168,239,260` |

---

## §5 测试与质量

### 5.1 测试规模

| 指标 | 数值 | 命令/证据 |
|---|---|---|
| 测试文件 | **23 个 `test_*.py`**（+ `conftest.py` + 3 个 MCP fixture 服务器 = **27 文件**） | `tests.rglob('*.py')`（Python 计数，排除 `__pycache__`） |
| `def test_` 函数数 | **294** | 逐文件计数求和 |
| pytest 收集数 | **294 tests collected in 2.67s** | `backend\.venv\Scripts\python.exe -m pytest --collect-only -q`（本次实跑两次，exit 0） |
| 上一次全量通过基线 | **279 passed / 0 failed**（2026-10-13） | `handoff/PROGRESS-WATCH.md:354,362` |
| ⚠️ 增量来源 | 279 → 294 的 **+15** 全部来自 `test_cli.py`（23:08 才出现，见文首警告框） | `git status` → `?? backend/tests/test_cli.py` |

**逐文件用例数**（合计 294）

| 文件 | 用例 | 文件 | 用例 |
|---|---|---|---|
| `test_file_scope.py` | 29 | `test_skills.py` | 27 |
| `test_graph.py` | 24 | `test_mcp.py` | 23 |
| `test_rerank_routing.py` | 20 | `test_browser_lifecycle.py` | 19 |
| `test_vectors.py` | 18 | `test_profile.py` | 17 |
| `test_vault.py` | 17 | **`test_cli.py`（新增）** | **15** |
| `test_indexing.py` | 15 | `test_memory_hygiene.py` | 12 |
| `test_summaries.py` | 10 | `test_skills_api.py` | 9 |
| `test_retrieval.py` | 8 | `test_grants.py` | 7 |
| `test_tools.py` | 6 | `test_tool_results.py` | 6 |
| `test_agent.py` | 4 | `test_connector_settings.py` | 3 |
| `test_api.py` | 2 | `test_connectors.py` | 2 |
| `test_workflow.py` | 1 | | |

### 5.2 静态检查配置

全部集中在 `backend/pyproject.toml`（**无独立 `ruff.toml` / `mypy.ini` / `.bandit`**）：

| 工具 | 配置 | 证据（行号 = 加了 `[project.scripts]` 之后的当前工作区） |
|---|---|---|
| **ruff** | `target-version="py312"`、`line-length=100`、`select=["E","F","I","UP","B","ASYNC"]`、`ignore=["E501","ASYNC230","ASYNC240"]` | `pyproject.toml:41-47` |
| **mypy** | `python_version="3.12"`、`check_untyped_defs=true`、`ignore_missing_imports=true`、`files=["app"]`（**只查 app，不查 tests/scripts**） | `pyproject.toml:49-53` |
| **bandit** | 只在 dev 依赖里声明，**无配置节、无 CI 调用点** | `pyproject.toml:34` |
| **pytest** | `asyncio_mode="auto"`、`testpaths=["tests"]`、`pythonpath=["."]` | `pyproject.toml:36-39` |
| **入口声明** | `[project.scripts] orbit = "app.cli.main:main"`（**未提交的工作区改动**） | `pyproject.toml:30-31` |
| **前端** | `eslint src`、`tsc -b && vite build` | `frontend/package.json:6-10` |
| **覆盖率** | 依赖装了 `pytest-cov`（`pyproject.toml:28`），但**无 coverage 配置、无阈值** | — |

### 5.3 质量侧已知问题（来自 PROGRESS-WATCH，非本次新发现）

- 计时类 flake：`test_graph.py::test_ask_user_still_interrupts_and_resumes` 的 15s stall 断言在全包串行下偶发超时（10-12 出现、10-13 消失）——`PROGRESS-WATCH.md:337,362`。
- mypy 只覆盖 `app/`，`tests/` 与 `scripts/` 不在类型检查面内。
- 无 CI 配置文件（无 `.github/workflows`），验证全靠手动命令清单（`handoff/00-CONTEXT.md:42-52`）。

---

## §6 明确缺失清单（Orbit 完全没有的东西）

按"零实现"标准穷举。每条给出"为什么算缺失"的核验方式。

### 6.1 架构级缺失（整层没有）

| # | 缺失项 | 核验 |
|---|---|---|
| 1 | **CLI 入口**：`orbit` 命令**当前装不出来**（`[project.scripts]` 只存在于未提交的工作区改动里，`uv sync` 后才有）；CLI 包本身仍是 untracked；且 4 个归类里 `run`（对话/服务/网关/定时/委派）、`platform`、`ext` 三组零实现 | `git status` → `?? backend/app/cli/`；`git diff backend/pyproject.toml`；`commands/` 只有 `core.py`（见 §2 第 1 行） |
| 2 | **cron / 定时任务子系统**：无调度器、无持久化作业、无 trigger、无无人值守安全边界 | grep `cron|schedul` 只命中摘要后台任务 |
| 3 | **gateway 多平台接入**：Telegram / Discord / Slack / WhatsApp / webhook / email 全无 | grep `gateway` 零命中 |
| 4 | **插件系统**：无加载器、无 manifest、无 pre/post tool call 钩子、无热插拔、无副作用撤销 | grep `plugin` 零命中；只有启动期 Protocol 注入 |
| 5 | **子 Agent / delegate**：Protocol 声明 + `available=False` + `NotImplementedError`，无任何调用路径 | `policies.py:209-212,586-590`；`policies.subagents` 引用数 = 0 |
| 6 | **多 profile / 多用户隔离**：硬编码 `user_id="local"` 单用户 | `storage.py:84,141,165`；`00-CONTEXT.md:13` |
| 7 | **可选技能市场 / optional-skills hub**：无技能发现、无分发、无版本仓库 | `data/skills/` 只有 3 个手写目录 |

### 6.2 模型与编排缺失

| # | 缺失项 | 核验 |
|---|---|---|
| 8 | **provider 适配层**：只有 OpenAI 兼容 chat-completions 一条；无 Anthropic Messages / Bedrock / Codex Responses / 进程外 app-server 适配器 | `agent.py:277-288` 唯一 `ChatOpenAI` |
| 9 | **fallback / 多 key 轮转 / 错误分类恢复**：无 402→换 key、429→退避这类 `FailoverReason` 机制；只有 5 条异常名→文案映射 | `agent.py:204-215` |
| 10 | **注册 vs 暴露分离**：无 toolset 分组、无 `check_fn`/`requires_env`、无平台/场景/profile/委托四层过滤；exposer 直通全量 | `policies.py:356-358`；`nodes.py:319` |
| 11 | **压缩谱系（session lineage）**：摘要原地改写，无"关旧 session → 子 session 种摘要 → 轮换 session ID → 记父子链" | `summaries.py`；`storage.py:81-98` 无父子字段 |
| 12 | **上下文三层（Stable/Context/Volatile）显式分层** | `context.py:135-141` 单条字符串拼接 |
| 13 | **cwd 项目文件加载（AGENTS.md / HERMES.md / .cursorrules）+ 提示注入扫描** | 全仓零命中 |
| 14 | **Anthropic 显式 prompt caching（cache_control 块）** | 只有"冻结前缀"这一近似（`context.py:145-177`） |
| 15 | **消息角色交替校验 / 中断伪造（tool_call 配对修复）** | 全仓零命中 |
| 16 | **级联中断（父心跳超时→子停止）/ 父子预算（90/50 轮）** | 无子 Agent，故无 |

### 6.3 工具与能力缺失

| # | 缺失项 | 核验 |
|---|---|---|
| 17 | **`web_search` / `web_extract` 工具**：无任何联网检索/正文抽取工具，上网 100% 靠浏览器 | grep 零命中（只有 `tools.py:268` 的描述字符串示例） |
| 18 | **代码执行沙箱**：无 docker/ssh/singularity/modal/daytona 环境；`run_command` 在 machine 档**零拦截** | grep 零命中；`tools.py:892-894` |
| 19 | **审批分级（Smart/Manual/Off）+ per-session approval + 审批请求/响应钩子** | `nodes.py:457-461` 明写"确认分支整体移除" |
| 20 | **工具级结果大小上限**：只有一个全局 16000，工具无法各自声明 | `policies.py:25,238` |
| 21 | **OCR / ASR / 语音**：`ocr|tesseract|whisper|asr|transcrib` 零命中 | `PROGRESS-WATCH.md:346` |
| 22 | **文档格式解析**：只有 TXT/MD/文本 PDF；无 docx / xlsx / pptx / html 结构化抽取 | `tools.py:560-586` |
| 23 | **桌面 / 电脑操作（computer use）**：只有浏览器 + 截图坐标点击 | `browser.py` 全在 Playwright 内 |
| 24 | **多模态理解（除视觉兜底外）**：仅 `browser_click/fill` 失败时把截图喂多模态模型报坐标 | `nodes.py:556-579` |
| 25 | **TOTP / OAuth 回调 / 二维码协同**（二次验证协同） | `vault.py` 只有静态账号密码 |

### 6.4 数据与治理缺失

| # | 缺失项 | 核验 |
|---|---|---|
| 26 | **审计时间线**：迁移 `0008` 空缺 5 个周期；`messages` 无 `tool_call_id`、`artifacts` 无 `checksum`，无法把"某次对外动作"与消息/产物精确对齐 | `PROGRESS-WATCH.md:347,360` |
| 27 | **指标与分布式追踪**：无 OTel / Prometheus / LangSmith；日志无 handler 配置、无落盘 | grep 零命中 |
| 28 | **可视化仪表盘**：无指标页、无 cron 页、无审计页、无工具开关页 | `App.tsx:34` 只有 6 视图 |
| 29 | **CI/CD**：无 `.github/workflows`、无流水线 | 仓库根目录无 `.github` |
| 30 | **备份/导出/导入**：无 `backup`/`export`/`import` 命令或端点 | 42 个 API 端点无此类（`main.py:195-788` 全部端点清单） |

---

## §7 已声明的未消费能力（声明了但没实现 / 没接线）

这一节是"补齐功能"时**性价比最高**的部分：能力边界已经写在类型里，缺的只是消费方或最后一段接线。

| # | 声明物 | 声明位置 | 消费状况（核验） | 性质 |
|---|---|---|---|---|
| 1 | `SubAgentRunner` Protocol + `DefaultSubAgentRunner` | `policies.py:209-212`（Protocol）、`:586-590`（`available=False` + `NotImplementedError`）、`:603`（`Policies.subagents` 字段）、`:627`（注入） | `policies.subagents` 全仓**引用数 0** | **完全未实现**（PROGRESS-WATCH 反复确认为"全项目唯一零消费方协议"，`:345,357`） |
| 2 | `Policies.reranker` 字段 | `policies.py:607`（字段）、`:631`（注入） | 全仓**无任何读取**。重排实际经 `build_retriever(settings, store, reranker=reranker)`（`:624`）注入 `retrieval.py` 内部使用 | **死字段**（重排功能在，字段没接线） |
| 3 | `Tool.confirmation` | `tools.py:294`（字段）；各工具构造时传 `True`（如 `tools.py:355,361`；`browser.py:63,69,71,72,79,86`；`mcp.py:362`） | `gate` 节点（`nodes.py:457-477`）**完全不读**该字段；唯一消费点是 `main.py:520` 把它透出给 `/api/tools` 展示 | **声明了"需要确认"但从不触发确认**（v1 零确认的直接后果） |
| 4 | `ToolRegistry.granted()` | `tools.py:672-679`（"用户在当前站点永久批准过该动作"） | **零调用方**。站点级 grants 仍有写入路径（`main.py:360-370` 的 `resume.remember`、`connectors.py:95-113`、`main.py:784-786` 撤销），但**读取侧已断** | **半断线**：写入能力存在、生效判据无人调用 |
| 5 | `SkillProvider` Protocol 的 `catalog()` / `load()` / `save()` | `policies.py:100-104`（协议声明）、`:381-398`（`DefaultSkillProvider` 实现） | 节点与工具一律**直连 `app.skills.*` 函数**（`nodes.py:1043`；`tools.py:1016,1043,1055-1062`），协议方法零消费。只有 `catalog_with_stats` / `record_outcome` / `find_conflict` 三个方法被真消费 | **协议冗余**（seam 只用了 3/6 方法） |
| 6 | `ToolExposer.expose` | `policies.py:91-94`（Protocol）、`:356-358`（`DefaultToolExposer` 直通） | 唯一调用点 `nodes.py:319`，实现是 `return specs` | **seam 空转**：有接口无策略，等于"每轮全量暴露" |
| 7 | `budget["summary_mark"]` | 读取于 `nodes.py:730`（`summaries.cost_since(int(budget.get("summary_mark") or 0))`） | `DefaultBudgetPolicy.start()`（`policies.py:321-333`）**从不写入该键** → 恒为 `0` → `budget` 节点的"按任务窗口切分摘要开销"实际退化为"读全部历史开销" | **接线缺口**（M5 后半段未生效） |
| 8 | `context_meta["compressed"]` | `nodes.py:138`（`context_meta.setdefault("compressed", {})`） | 之后**无任何读写** | **占位字段**（压缩元数据预留） |
| 9 | `with_defaults()` / `defaults()` | `graph/state.py:35-55`；导出 `graph/__init__.py:4-6` | 生产路径**零调用**（`builder.py:10` 只 import `AgentState`）；仅被 `test_graph.py:13,20,29,185,209,224` 消费 | **只在测试里活着的代码**（"旧 checkpoint 兼容"的意图未接进运行时） |
| 10 | 前端 `waiting.reason === "approval"` 分支 | `frontend/src/App.tsx:83-84`（`waitingApproval`）、`:179`（禁用输入）、`:471`（渲染审批 UI） | 后端 `gate`（`nodes.py:457-477`）在 v1 已删确认分支，只会产出 `clarification` / `login` / `verification` 或工具自定义 reason | **前端死代码**（审批 UI 永远不会被点亮） |
| 11 | `ToolResultPolicy.limit` | `policies.py:25`（`TOOL_RESULT_LIMIT=16000`）、`:238`（`limit = TOOL_RESULT_LIMIT`） | 是**类级单值**，`Tool` 数据类里没有 size 上限字段 → 无法按工具声明不同上限 | **声明了"上限"但只有一档** |
| 12 | `TOOL_CONCURRENCY = 4` 并发闸 | `tools.py:26`（`asyncio.Semaphore`，`execute` 里 `:742` 使用） | 注释自述"单 worker 串行下现在天然不超"（`tools.py:24-25`）；`agent.py:128-146` 确实是单 worker 串行 | **接了线但当前恒不生效**（为 MCP 并发预留） |
| 13 | `RRF_WEIGHTS` 加权参数 | `retrieval.py:44`（`(1.0, 1.0, 1.0)`）、`rrf(..., weights=)` `:62-80` | 参数开放但默认等权，实测加权是 wash（注释 `:29-43` 记了 A/B 数据） | **开放但否掉的实验位** |
| 14 | `Settings.github_token` / `github_repo` 与两个 GitHub 工具 | `config.py:108-109`；`connectors.py:27-34`（无条件注册）、`:267-272`（执行时才读 repo/token） | 未配置仓库时**工具仍在注册表里暴露给模型**，调用即失败（无 `check_fn` 式的"不满足条件就不出现"） | **注册与可用性未分离** |
| 15 | `state.py` 的"可观测与预留"注释 | `graph/state.py:26-28`（`context_meta` / `reflections`） | `reflections` 被 `reflect` 节点真消费（`nodes.py:694-717`）；`context_meta` 里 `compressed` 是空占位（见 #8） | **部分预留已兑现、部分未** |

### 7.1 与 Hermes 对照时最该注意的两条

1. **`Tool.confirmation`（#3）+ `granted()`（#4）+ 前端审批 UI（#10）三者构成一条完整的"确认链路"，但三段全部断开**。这不是三个独立 bug，而是"v1 完全访问、事前零确认"这一个决策的连带后果（`handoff/00-CONTEXT.md:11` 明写"不要往设计里加确认弹窗"）。**对标 Hermes 补"分级审批"时，这条链路是现成骨架，不是从零开始。**
2. **`ToolExposer`（#6）是"注册 vs 暴露分离"唯一的落点，而它是直通的**。Hermes 用四层过滤收窄单次运行的工具表面（平台→场景→profile→委托）；Orbit 已把 seam 建好（`policies.py:91-94` + `nodes.py:319`），补的只是 `DefaultToolExposer` 里的过滤逻辑。这是**改动面最小、收益最直接**的一条。

---

## 附：本文的证据生成方式（可复现）

```powershell
# 代码规模（必须用 Python 数，PowerShell 会因 UTF-8/LF 误判吞行）
cd E:\bbbooottt\backend
.\.venv\Scripts\python.exe -c "import pathlib;
app=[p for p in pathlib.Path('app').rglob('*.py') if '__pycache__' not in str(p)];
print(sum(len(p.read_text(encoding='utf-8').splitlines()) for p in app))"

# 测试收集
cd E:\bbbooottt\backend ; .\.venv\Scripts\python.exe -m pytest --collect-only -q

# 缺失项核验（示例）
Select-String -Path backend\app\*.py,backend\app\graph\*.py -Pattern 'cron|gateway|plugin'
Select-String -Path backend\pyproject.toml -Pattern '\[project\.scripts\]'
Get-ChildItem backend\app -Recurse -Filter *.py -File      # 注意：cli 是目录包，别用 Test-Path app\cli.py

# 未消费项核验（示例）
Select-String -Path backend\app\*.py,backend\app\graph\*.py -Pattern 'policies\.subagents'
Select-String -Path backend\app\*.py,backend\app\graph\*.py -Pattern 'summary_mark|confirmation|\.granted\('

# 并发变更核验（本次调研踩到的坑，务必先跑）
git status --porcelain ; git log --oneline -3 ; Get-Date
Get-ChildItem backend\app -Recurse -Filter *.py -File | Sort-Object LastWriteTime -Descending | Select-Object -First 10
```

**未执行**（遵守只读约束）：未运行 `pytest`（仅 `--collect-only`）、未运行 `ruff`/`mypy`、未启动服务、未执行任何 alembic 迁移、未修改任何文件。

> **两个写给下一位的教训**：
> 1. 我第一次用 `Test-Path backend\app\cli.py` 判定"无 CLI"，返回 `False` 就下了结论——但真实形态是**目录包 `app/cli/`**。**判定"某能力缺失"时不要只测单个文件名**，要 `Get-ChildItem -Recurse` 看整棵目录树。
> 2. 我用 `Get-Content | Measure-Object -Line` 统计行数，得到 `app` = 7,458 行；同一批文件用 Python 数却是 **9,029 行**——差 1,571 行，原因是 PowerShell 把这个 UTF-8/LF 仓当成别的编码读，**静默吞行**。凡涉及"规模/密度"的数字，一律用 Python 复核。
