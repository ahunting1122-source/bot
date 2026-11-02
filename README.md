# Orbit · 网页操作特化的本地 Agent

Python + FastAPI + LangGraph 后端，React 工作台。核心能力是**在真实可见浏览器里操作各类网站**：给出自然语言目标，它自己判断该去哪个站点、打开进入，并在站内完成多步操作。本地文件与跨对话记忆是辅助能力。

**没有域名白名单**——任意 http(s) 站点都可访问，新增站点不需要写适配器。

当前状态：站点识别、进入、站内搜索/排序/翻页/进详情、表单填写提交均已用真实模型与真实站点实测；密钥存储支持查看密钥、保存站点账号与自动登录（密码不经过模型）；站点级免确认可减少重复询问。固定任务集 10 个用例首轮 10 / 10，见 `test-results/eval-<日期>/`。人工接管目前只验证到"模型主动请求"这一步，完整的人工处理流程尚未演练。

在 MVP 主链之上，后续已补齐五个模块并均有测试覆盖：**知识层（RAG 三档检索 + 引用回填）**、**技能层（加载 / 沉淀 / 四道闸治理）**、**MCP 补齐（生命周期、prompts→技能、resources→RAG）**、**记忆卫生与用户画像**（迁移 `0009`–`0011`）、**权限级别与系统命令执行**（对话页可切「仅工作文件夹 / 完全访问权限」，后者可读写整机任意文件并执行任意命令）。当前回归基线（第 122 轮实测）：后端 `pytest` **3852 passed, 3 skipped, 0 failed**、`ruff` / `mypy` clean；功能对齐有 **13 项门禁**一键复核（`backend\.venv\Scripts\python.exe scripts\check_all.py`）。剩下的方向与已知问题全部列在「下一步」。

## 启动

要求：Windows、Python 3.12、uv、Node.js 20.19+ 或 22.12+。

第一次安装：

```powershell
cd E:\bbbooottt\backend
uv sync --python 3.12
uv run playwright install chromium

cd ..\frontend
npm ci
npm run build
```

启动已构建的工作台（二选一）：

```powershell
cd E:\bbbooottt
.\scripts\start.ps1            # uvicorn 直起，端口 6185
# 或
cd backend
.\venv\Scripts\python.exe -m app.cli.main serve --skip-build   # 端口 9119
```

`start.ps1` 打开 <http://127.0.0.1:6185>；`orbit serve` 打开 <http://127.0.0.1:9119>，API 文档在 <http://127.0.0.1:6185/docs>。启动脚本会保持在当前终端，按 Ctrl+C 停止。Windows 下运行一个 worker，不启用 Uvicorn reload，保证 Playwright 的子进程事件循环可用。

开发前端时，在另一终端进入 `frontend` 执行 `npm run dev`，访问 <http://127.0.0.1:5173>，请求通过 Vite 代理到后端。

## 首次使用

1. 打开“连接与设置”，填写支持工具调用的模型名称、OpenAI 兼容 API 地址和 Key，保存后点击“检查模型连接”。Key 通过系统 keyring 保存，不回显到前端，留空保存会保留已有值。
2. “通用网页”里填一个常用起始网址，点“打开浏览器登录”，在专用浏览器窗口里登录你常用的站点。登录态保存在本机 Profile 里，下次直接是已登录状态。
3. 需要账号密码由助手代为填写时，去“密钥存储”保存该站点的用户名与密码。**密码存在系统凭据管理器，不进入模型上下文**：助手调用 `vault_login` 时只传站点名与页面元素引用，后端取值后直接填入并提交，每次都会单独向你确认。
4. 回到工作台直接聊天。需要补充信息时在输入框回答，需要确认时选择允许或拒绝；反复确认同类站内操作时可以点“允许，这个站点以后不再问”，之后可在设置页的“已记住的站点操作”里撤销。
5. 将 TXT、Markdown、文本型 PDF 放入 `data/workspace` 可让助手阅读；如需更换授权目录，复制 `backend/.env.example` 为 `backend/.env`，设置 `ORBIT_WORKSPACE` 后重启。
6. 想接外部工具时，在「连接与设置 → MCP 服务」里填服务列表（本地进程写 `command` + `args`，远程写 `url`），保存后助手就能调用它们提供的工具。默认每个工具都要确认；确认无误的服务可以加 `"auto_approve": true`。配置里的密钥写成 `"keyring:名称"`，实际值用 `POST /api/mcp/secret` 单独保存。

推荐的第一条消息（不需要任何配置）：

> 打开 IT之家，告诉我今天的头条标题是什么。

登录站点示例：

> 打开 https://example.com/login，用密钥存储里的账号登录进去。

长期记忆演示：

> 记住：我的报告格式是中文 Markdown。

确认保存后新建对话，要求生成报告。新对话注入已保存的偏好，不会自动载入其他对话原文。历史工作可通过 `history_search_tasks` 和 `history_get_task` 检索；界面的“对话摘要”用于回顾多轮内容。

## 已实现模块

| 模块 | 实现 |
|---|---|
| Agent | LangGraph think → gate → execute，结构化工具调用，最大动作数（`max_actions=40`），用户中断与继续 |
| 权限级别 | 对话页可切「仅工作文件夹 / 完全访问权限」，约束**文件读写范围与命令执行**；完全访问档不设限 |
| 预算与长线任务 | 动作数 / token 硬停 + **时间软闸**：到 `max_seconds`（默认 360s）不硬停，转入后台继续并定期发进度报告（`background_report`）；对话页显示 token/s 速率与输入/输出 token 消耗 |
| 回复 | SQLite 持久化（WAL）、单槽队列、正文 SSE 增量、取消后部分保留、重试、重启恢复 |
| 站点操作 | 任意 http(s) 网址直连；导航后等页面稳定再观察；带版本号的元素引用；点击/输入/按键/滚动/截图；进入判定标注 `entry_weak` |
| 元素采集 | 控件（button/input/select 等）优先于链接，避免底部翻页控件被截断 |
| 密钥存储 | 密钥查看与更新；站点账号保存；登录状态按 Cookie 推测（标"疑似"）；`vault_login` 自动登录且密码不经过模型 |
| 授权 | v1 **事前零确认**：工具直接执行，人工介入只剩 `ask_user` 与 `needs_user` 两种。站点级 grant 退化为审计分组标签（按主机名隔离、可撤销） |
| 接管 | 接管是最后手段：站点有保存账号时先要求自己登录；首次请求必须说明已尝试过什么 |
| 本地（辅助） | 文本/PDF 读取、文件列举、关键词搜索、Markdown 报告、产物下载 |
| 命令执行 | `run_command` 走系统 shell（Windows 为 cmd）执行命令并回收 stdout/stderr 与退出码；超时会终止整棵进程树，输出过长保留头尾 |
| 记忆（辅助） | 当前会话历史、模型生成的多轮会话摘要、长期键值偏好、历史检索 |
| 知识（RAG） | 结构感知切块（标题路径 + **表格按行切块**）+ 双路 FTS5（trigram / 字符级）+ `sqlite-vec` 向量路（`embeddinggemma-300m`）+ cross-encoder 重排，三档融合；`retriever_search` 工具 + 引用回填硬约束；向量与重排默认开、按需可关、失败自动降级 |
| 技能 | `SKILL.md` 加载与注入、沉淀落盘 + 四道闸治理（写入查重 / 矛盾转备选 / 注入择优 / 降级淘汰）；工具 `skill_load` / `skill_list` / `skill_save` + 操作面 API 与前端「技能」页 |
| MCP | 连接外部 MCP 服务（本地 `command` 或远程 `url`），把它们的工具暴露给模型；默认逐个确认，可显式免确认；密钥走 keyring 不落配置文件。**生命周期加固**（订阅流失即摘工具 + 指数退避重连）、**prompts → 技能**、**resources → RAG 索引** |
| 工作台 | Markdown 聊天、增量回复、补充/确认/停止/重试、对话摘要、密钥存储、连接与设置（含 MCP 服务） |
| 回归 | 10 个固定任务的评估跑批 + 单任务探针 + 14 组界面交互检查 + 权限/命令真实接口 e2e |

普通问答直接显示回答，不展示任务面板。工具错误会返回模型用于调整或说明原因。折叠的“过程说明”仅在模型提供独立的工具说明时出现，不复制最终回答或展示原始推理。未配置模型时明确提示连接设置，不返回伪造内容。

GitHub 与 Notion 连接器代码仍然保留，但已不是主线：它们现在只是"站点"的一种，不再参与完成线验收。

## 实现取舍

- 采用固定单用户身份 `local`，API 绑定本机并使用本地会话 Cookie。当前不面向公网部署或多人共享。
- 工具名称使用下划线以兼容模型 function calling；计划中的点分名称是概念命名。
- **不设域名白名单**：只校验地址格式（http/https、无用户名密码）。真实站点内容不做安全过滤，页面里的文字不能改变工具权限。
- v1 明确选**事前零确认**：不再有"允许/拒绝"弹窗，站点级 grant 只作为审计分组标签保留。信任靠**事后审计 + 三层预算刹车**（动作 / token / 时间）与**权限级别**来兜，而不是靠事前打断。
- 站点账号与密钥存在系统 keyring；`data/vault.json` 只记录站点名与更新时间，不存值。密码经专用工具填入页面，不进入模型上下文、日志或 checkpoint。
- 登录状态按 Cookie 名启发式判断，只作提示，不代表站点一定可用；会话过期由站点侧决定。
- 长期记忆注入近期偏好、历史按关键词检索；**知识层已接入 RAG**（结构感知切块 + 双路 FTS5 + `sqlite-vec` 向量 + cross-encoder 重排三档，默认开启，失败自动降级；关掉 `ORBIT_VECTOR_ENABLED=0` / `ORBIT_RERANK_ENABLED=0` 即退回纯 FTS5 双路）。评测量化：词面查询集 recall@5 1.000（过门）、改写查询集 0.688（门槛 0.70，差 1 条，详见「下一步」）。
- 当前会话读取最近消息并限制字符数，同时注入多轮会话摘要；摘要后台遍历完整历史并按长度分块综合，生成时新增消息会触发重算。
- **权限级别是本地 Agent 的核心安全面**：`workspace`（默认）把文件与命令都夹在授权目录内，并拦下格式化/关机/递归删根目录这类不可逆命令；`machine` 才是真正不设限——用户选择它即把本机交给它。两档都在对话页可见可切，切换落 `data/settings.json`。命令执行的超时与输出截断是运行保护，两档都在。
- 浏览器通常优先使用 DOM；并未实现通用桌面 GUI、纯截图坐标 Agent，也不提供"按应用分发"的桌面自动化（命令执行走 `run_command`，是 shell 而不是 GUI 驱动）。
- Notion DOM 结构可能变化，当前适配 `.notion-page-content` 下的可编辑内容。登录、验证码、只读页面或结构变化可能需要人工接管。
- Notion 已开始写入但结果不明时不会直接重复提交。通用点击等动作不保证 exactly-once，恢复后需要重新观察。
- 取消只停止后续执行，不能撤销已完成的外部写入。
- 凭证在 keyring/本地环境变量，非敏感配置在 `data/settings.json`；初版尚未单独建立 connector_accounts 数据表。
- 后端、Profile 和文件处理在本机运行；使用云模型时，被选入上下文的资料会发送给所配置的模型。

## 最小验证

后端（一键门禁，exit 0 = 全绿）：

```powershell
cd E:\bbbooottt
backend\.venv\Scripts\python.exe scripts\check_all.py   # 13 项（含 pytest 之外的 10 道审计门禁）
cd backend
.\venv\Scripts\python.exe -m pytest -q        # 3852 passed / 0 failed
.\venv\Scripts\python.exe -m ruff check .
.\venv\Scripts\python.exe -m mypy app
```

前端：

```powershell
cd frontend
npm run build
npm run lint
```

用隔离数据检查工作台（脚本自行启动临时服务，不修改现有凭证和对话）：

```powershell
cd backend
uv run python ..\scripts\ui_smoke.py
```

固定任务集跑批（需要后端已在 6185 运行；每步确认会自动批准，报告写到 `test-results/eval-<日期>/`）：

```powershell
cd backend
uv run python ..\scripts\eval_run.py                # 全部用例
uv run python ..\scripts\eval_run.py bili_page_two  # 单个用例
```

单任务探针（想知道某个自然语言目标实际调了哪些工具时用它）：

```powershell
cd backend
uv run python ..\scripts\agent_probe.py "在 B 站搜索机械键盘并翻到第 2 页"
```

查看当前接入了哪些工具（内置的加 MCP 带来的）：

```powershell
curl -s -b cookies.txt http://127.0.0.1:6185/api/tools
```

真实可见浏览器最小验证（临时 Profile，打开 Bilibili 与 GitHub，不登录）：

```powershell
cd backend
uv run python ..\scripts\check_browser_visible.py
```

后端单元与集成测试使用脚本化模型、Mock 传输和本地替身页面，不需要真实账号，**不证明真实模型回答质量或第三方页面长期稳定性**——那部分由 `eval_run.py` 的真实跑批覆盖。测试覆盖：路径边界、授权与站点免确认、凭证白名单与登录判定、浏览器生命周期与导航等待、暂停重启恢复、记忆持久化、事件回放、会话隔离、SSE 取消不破坏数据库。

## 下一步

按优先级排列。每项标注**现状**与**落点**，可直接据此开工。

### 1. 子 Agent（唯一"协议已声明、零消费方"的扩展点）
- **现状**：`backend/app/policies.py` 声明了 `subagents: SubAgentRunner` 并注入 `DefaultSubAgentRunner()`，但全仓**只有这两处**，没有任何调用路径（实现为 `available=False` + `NotImplementedError`）。
- **已定方案**：定位"并行 / 隔离 / 专家"三合一；编排方式 = 作为 `execute` 的一个工具 `subagent_run`，复用现有 loop 与护栏，**不动 8 节点图骨架**。
- **设计要点**（参考 Codex / dsh 等 harness）：
  1. 子 Agent 各持一份更紧的 `DefaultBudgetPolicy`，**不共享父预算池**（并发扇出会把父额度吃光）；
  2. 并发上限，对齐既有 `TOOL_CONCURRENCY=4`；
  3. **级联取消**：父取消 → 广播停子，每个子再带独立 timeout 兜底；LangGraph 取消需补 `tool_result` 占位以保持消息配对；
  4. 结果回流用**结构化摘要 + 引用**（`{goal, status, summary, references[], artifacts[]}`），**不回流完整历史**；子工具结果照常走 digest 落 `tool_results`，父可用 `retriever_search` 召回。
- **落点**：先出设计文档（`handoff/`），再改 `policies.py` / `tools.py` / `graph/nodes.py`。

### 2. RAG 门 2（改写查询集 recall@5 ≥ 0.70）
- **现状**：**0.688**（差 1 条）；词面查询集 1.000（已过门）。
- **已排除**：加深候选（`LIST_DEPTH` 10–50）、FTS 覆盖率门控、路径权重、切块粒度四档——**全部实测无效**（候选池天花板恒为 0.812）。残差已定性为**嵌入模型对极短功能性短语的判别力上限**，**不要再调这些参数**。
- **剩余修法**（阶段 6.5 第二半）：① 查询集扩容到 ≥50 条后重估难度分布；② 多向量 / HyDE；③ 换更强的嵌入或重排模型。证据见 `handoff/vector-path-plan.md` 第四轮。

### 3. 生产库重切（RAG 改动生效的前提）
- **现状**：`data/app.sqlite` 的 **524 块仍是旧切块**（整表切块），表格行级切块只对**新入库内容**生效。
- **落点**：写重切脚本（可复用 `scripts/backfill_vectors.py` 的模式），重切后重建向量。

### 4. 存储收敛 + 迁移 `0008`
- **现状**：`messages` 缺 `tool_call_id`、`artifacts` 缺 `checksum`；**`0008` 已空缺 4 个编号周期**（`0009`/`0010`/`0011` 已越过它），预留给审计时间线，编号空洞有堆积风险。
- **落点**：一次迁移补齐存储收敛（`ARCHITECTURE.md` 阶段 8），顺手消掉 `0008`。

### 5. 多模态（OCR / ASR）
- **现状**：只有**图像侧视觉兜底**（DOM 定位失败时把截图喂给模型报坐标，有 4 个单测）；OCR / ASR **零代码**。
- **前置**：确认部署侧模型是多模态的（`deepseek-flash`）；若 `ORBIT_MODEL` 仍是纯文本模型，图像块会被忽略。

### 6. 已知测试问题
- `tests/test_graph.py::test_ask_user_still_interrupts_and_resumes` 的 15s stall 断言在**全包串行**下偶发超时（CPU 争用导致的计时 flake，单独跑 3.66s 通过）。建议收紧等待判据或隔离该用例。

### 7. 版本管理习惯
- 本次把积压的工作区改动一次性落成版本（标签 `orbit-2026-10-12`）。后续请**小步提交**——此前 3 个 commit 之上积压了 87 个文件改动，是纯风险敞口。
- **本机环境注意（已踩过一次）**：在本机的沙箱 shell 里，git 的**嵌套引用**（形如 `refs/heads/feature/xxx`）更新会被回滚——`git commit` 会打印成功哈希、对象也写进了 `.git/objects`，但**分支引用不落盘**，于是 `git log` 报「当前分支没有任何提交」。因此**分支名不能带斜杠**，本仓库已从 `feature/local-web-agent` 改名为 `local-web-agent`。
- 万一分支引用丢失（`git log` 报 unborn），提交对象不会丢，直接用标签恢复引用即可：`git rev-parse <tag> > .git/refs/heads/<branch>`（必要时先 `mkdir -p .git/refs/heads`）。

## 目录

```text
backend/app/       API、Agent 编排（graph/）、工具、检索与向量（RAG）、技能、MCP、连接器、浏览器、密钥存储、上下文、存储
backend/migrations/  Alembic 数据迁移（0001–0011，其中 0008 预留给审计时间线）
backend/tests/     单元与集成测试
frontend/src/      React 工作台（工作台 / 对话摘要 / 记忆 / 技能 / 密钥存储 / 连接与设置）
scripts/           启动、界面冒烟、固定任务集跑批、单任务探针、离线检索评测与诊断
handoff/           设计决策与实施交接记录（按主题归档，含 RAG / 技能 / MCP / 编排等）
examples/          可复制到工作目录的演示资料
data/              运行数据、浏览器 Profile、文件及截图，不入 Git
test-results/      界面与跑批报告
MVP_PLAN.md        当前计划、实测记录与变更沿革
```

真实第三方登录、站点验证和 Notion 写入需要使用者在专用浏览器中接管；测试替身的通过不等于真实账号权限已验证。
