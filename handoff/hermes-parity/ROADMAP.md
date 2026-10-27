# Orbit × Hermes 功能对标 · 路线图

**目标（用户设定）**：照着 Hermes 完成 Orbit，**包括 CLI 工具与 Hermes 的所有层**。
**完成标志**：Hermes 具备的功能，Orbit 全部具备，且每一项都有可复核的验证证据。

- 基准：本机 `NousResearch/hermes-agent` CN 发行版，runtime **0.21.0-cn.18**（kernel 0.21.0）
  - 安装位置 `E:\Hermes Agent CN Desktop\data\versions\0.21.0-cn.18`
  - 运行时 exe `hermes-agent-cn-runtime-win32-x64.exe`（核心层编译在 PYZ 内，无 .py 源码）
  - 可读源码：`_internal\tools\`（147 个工具模块）、`_internal\plugins\`（18 组 / 103 个 provider）、`_internal\skills\`（21 类目 / 91 个技能）
  - 用户 home `E:\Hermes Agent CN Desktop\data\hermes-home`（state.db 174MB / 26 业务表 / 14,595 条消息）
- 被改对象：`E:\bbbooottt`（Orbit）——Python 3.12 + FastAPI + LangGraph 后端，React 前端

调研产物（逐层，带 file:line 证据）：`handoff/hermes-parity/research/01..09-*.md`

---

## 0. 方法论

1. **先调研、后行动**，调研与行动在同一个目标任务里推进，不另起炉灶。
2. **一切结论要证据**。研究文件里每条结论标【实证】或【推断】；实现完成后由监视子agent抽查代码，
   查不到证据的条目在 `PROGRESS.md` 里记「无实据」，不计完成。
3. **命令面即功能面**。`orbit --help` 的完整性是"对齐 Hermes"的第一道验收线；
   `backend/app/cli/spec.py` 的注册表是唯一真相源，`scripts/parity_report.py` 一键重算矩阵。
4. **分波次、每波可验证**。每波结束必须有：测试通过 + CLI 可跑 + 矩阵状态更新。
5. **不做假对齐**。Hermes 有而 Orbit 不需要的运维复杂度（如 20+ 平台全量适配、pets 动画）
   按 P1/P2 排后，但**不允许登记成 done**。

---

## 1. 波次总表

| 波次 | 主题 | 对应 Hermes 层 | 状态 |
| --- | --- | --- | --- |
| W0 | 调研：9 份逐层清单 + Orbit 现状矩阵 | 全部 | ✅ 完成 |
| W1 | CLI 地基 + 核心命令（status/doctor/config/dump/logs/prompt-size/completion） | 入口/CLI 层 | ✅ 完成 |
| W2 | CLI 命令面补齐：75 顶层 / 411 展平条目全部登记，选项说明 100% 覆盖 | 入口/CLI 层、运维 | 🟡 命令面已完成，实现 **82/411**（另有 3 partial、326 todo） |
| W3 | 配置与模型适配层：16 家 provider、3 种 transport、fallback 链、凭证池、三层 prompt | 模型适配层 + 上下文装配层 | 🟡 provider/fallback/auth/三层 prompt 已落地；per-model 路由、推理线翻译表、auxiliary slots、MoA 运行时待做；**fallback 与凭证池未接线运行时** |
| W4 | 工具层：注册表遮蔽门禁、toolset 过滤、三层结果治理、危险命令两级、元工具 | 工具层 + 注册/toolset 层 | 🟡 遮蔽门禁 / toolset 过滤 / 三层结果治理 / 危险命令两级 / web 工具**全已落地**；缺元工具（todo / clarify / tool_search）与 12 个功能域 |
| W5 | Skill 层：多根 tier、16 字段 frontmatter、渐进披露、写侧校验链、curator 状态机、hub/bundles/sync | Skill/curator 层 | 🟡 本轮施工中 |
| W6 | 记忆层：MEMORY/USER 字符预算 + 冻结快照、CJK 双 FTS、session_search 四形态、provider 接口 | 记忆层 | 🟡 四形态召回（零 LLM）已落地；字符预算、冻结快照自曝用量、8 个 provider、FTS 索引待做 |
| W7 | MCP 层：OAuth、prompts→技能、resources→RAG、`orbit mcp`、把 Orbit 自己暴露成 MCP 服务 | MCP 层 | ⬜ 待做 |
| W8 | 持久化与会话层：统一 session 平面、追加日志 + id 游标、压缩谱系、幂等/投递账本、孤儿回收、checkpoint | 持久化层 | 🟡 backup / import / checkpoints 已落地；统一 session 平面、压缩谱系、投递账本、孤儿回收待做 |
| W9 | 自动化与网关：cron、gateway dispatcher + 四状态文件、平台适配器基类、webhook/pairing/peer/send | cron 层 + gateway/平台层 | 🟡 cron 整层已落地（无常驻 ticker，靠外部调度器）；gateway / 平台层本轮施工中 |
| W10 | 插件系统：16 字段 plugin.yaml、19 种 register_*、17 个 hook 事件、plugin_guard 三级裁决 | 插件层 | 🟡 本轮施工中 |
| W11 | 委派与并发：subagent runner、深度/并发/预算、swarm 调度、worktree 隔离、中断补位 | Agent 核心循环层 | ⬜ 待做 |
| W12 | 可观测性与 UI：logs/insights/monitoring、metrics schema、dashboard 页面、skin/console/TUI | 可观测性层 + Web UI 层 | 🟡 结构化日志 + 轮转、insights、monitoring 已落地；metrics schema、dashboard 18 页、skin/console/TUI 待做 |
| W13 | 安全：secrets/egress/OSV 供应链、密钥脱敏、审批三态、hardline 硬地板 | 安全与审批层 | 🟡 审批三态 + 18 条硬地板已落地；secrets/egress/OSV/脱敏本轮施工中 |
| W14 | 验收：逐项对照矩阵出证据、补测试与文档 | 全部 | ⬜ 待做 |

---

## 2. 各波次验收判据

### W0 调研（✅）

- [x] 01 CLI 命令面：Hermes 全部子命令 + 每个 option 的取值与默认值
- [x] 02 工具层：94 个内置工具 / 19 功能域 / 32 toolset / 三层结果治理数值
- [x] 03 持久化层：6 库 62 表 / 目录即数据 / 会话生命周期与恢复
- [x] 04 Skill 层：21 类目 91 技能 / frontmatter 16 字段 / curator 与 ledger
- [x] 05 插件与网关层：18 组 103 provider / 22 平台适配器 / 19 种 register_*
- [x] 06 模型适配层：provider 表 + config 键表 + 容错链
- [x] 07 记忆层：三层记忆容量与数值 / session_search 四形态 / 8 个 provider
- [x] 08 Orbit 现状矩阵：分层成熟度 + 28 个工具 + 24 张表 + 缺失清单
- [x] 09 核心循环层：三层 prompt / 预算与心跳 / 压缩谱系 / UI 入口

### W1 CLI 地基（✅）

- [x] `backend/app/cli/` 四层结构（spec / context / main / commands）
- [x] 中央命令注册表 + 归类 + 对标优先级 + 对标状态元数据
- [x] `[project.scripts] orbit = "app.cli.main:main"`，`python -m app.cli` 等价入口
- [x] 命令：`status` `doctor` `config{list,get,set,unset,path}` `dump` `logs` `prompt-size` `completion`
- [x] 配置水合：运行时 settings.json + 系统 keyring（否则密钥永远显示"未配置"）
- [x] `--json` 结构化输出、`--data-dir` 隔离、密钥不上命令行
- [x] `scripts/parity_report.py` 从注册表一键生成对标矩阵
- [x] `backend/tests/test_cli.py` 15 条契约测试；ruff/mypy clean

### W2 CLI 命令面补齐

判据：`orbit <cmd> --help` 覆盖 Hermes 全部非内部命令；每个命令有真实实现（非空壳），
运维类（backup/import/checkpoints/sessions/prune/migrate/update/pause/resume/verify/security/debug）
全部可跑并有测试。

### W3 配置与模型适配层

判据：provider 抽象（openai_chat / anthropic / gemini / bedrock 至少 4 种 transport）；
`orbit model` / `moa` / `fallback` / `proxy` / `auth` / `login` / `logout` 可用；
auxiliary.vision 与 stt 可配置；provider 凭证走 keyring，不落配置文件。

### W4 工具层

判据：注册表跨 source 遮蔽默认拒绝（`override=True` 需显式放行）；toolset 四层过滤 +
`orbit tools` per-platform 启停；三层结果治理（50K 输出 / 100K 单条 / 200K 整轮 + spillover 落盘）；
危险命令两级（hardline 硬地板 + 审批模式）；`todo` / `clarify` / `tool_search` 元工具落地。

### W5 Skill 层

判据：多根 tier（project > user > external，first-wins）；frontmatter 16 字段 + 11 条硬校验 +
description 60 字符预算；渐进披露（索引只放名字+短摘要，按需 `skill_load` 全文）；
写侧校验链 + staging 审批门 + ledger（sha256 内容寻址）；curator 状态机 + 两级锚点 + 可恢复归档；
`orbit skills` / `bundles` / `curator` / `sync` / `journey` 可用。

### W6 记忆层

判据：`MEMORY.md` 2,200 字符 / `USER.md` 1,375 字符上限 + 冻结快照 + 头部自曝用量
（`[99% — 2,181/2,200 chars]`）；CJK 双 FTS 索引（unicode61 + trigram，中文必须走 trigram）；
`session_search` 四形态（discover/scroll/read/browse）零 LLM 成本；记忆 provider 插件接口 + 至少 1 个实现。

### W7 MCP 层

判据：stdio + Streamable HTTP + OAuth；prompts→技能、resources→RAG；`orbit mcp` 管理面；
`orbit serve --mcp` 把 Orbit 工具暴露成 MCP 服务。

### W8 持久化与会话层

判据：单一 session 平面（CLI/gateway/cron 同库同表）；messages 追加 + `id` 游标回放；
压缩产生父子谱系 + `system_prompts` 内容寻址；幂等键 + 投递账本；孤儿回收 + 存活租约；
checkpoint 快照/回滚；`orbit sessions` / `checkpoints` / `backup` / `import` 可用。

### W9 自动化与网关

判据：cron 调度（含投递）；gateway 单 dispatcher 互斥 + 四个状态文件；平台适配器基类契约
（7 必选 + 5 可选发送 + 交互降级 + `standalone_sender_fn`）；至少 4 个平台落地（CLI/Webhook/飞书/Slack）；
`orbit cron` / `gateway` / `webhook` / `pairing` / `peer` / `send` 可用。

### W10 插件系统

判据：plugin.yaml 16 字段 manifest（kind 决定加载时机、env 富字典进 setup 向导、provides_tools 影响发现期）；
19 种 `register_*` 能力 + 17 个 hook 事件；plugin_guard 三级裁决（`--force` 不得覆盖 dangerous）；
dashboard 插件契约（manifest.json 8 字段 + 模块级 `router`）。

### W11 委派与并发

判据：`subagent` 作为一等控制平面对象（run id / 外部操控 / 父完成后存活）；MAX_DEPTH 与并发上限；
子 agent 独立预算（父剩余 50% 按批均分，下限 2,000）；心跳 30s + 双阈值判卡死；中断补发 tool_result 占位。

### W12 可观测性与 UI

判据：结构化日志 + 轮转；`orbit insights`（用量口径）；`orbit monitoring`（health/diagnostics）；
metrics schema（shared_metrics v1/v2）；dashboard 页面覆盖 Hermes 的 18 个页面语义（可合并但不可缺功能）；
`orbit skin` / `console` / `logs` 可用。

### W13 安全

判据：`orbit secrets`（外部密钥源）/ `egress`（出口凭证注入）/ `security`（OSV 供应链审计）；
输出与日志密钥脱敏；审批三态 manual/smart/off + 12 条 hardline 硬地板无条件拦截。

### W14 验收

判据：`MATRIX.md` 每一项都有：实现文件:行 + 测试用例 + CLI 取证命令；监视子agent抽查通过率 100%；
`pytest` 全绿；`ruff` / `mypy` clean。

---

## 3. 已知的、必须如实记录的边界

1. **Hermes 核心层无源码**。`run_agent` / `hermes_state` / `hermes_cli` / `toolsets` / `agent/curator.py`
   都编译在 exe 的 PYZ 内。相关结论靠"调用点源码 + 库结构 + CLI --help + 实测"三重交叉印证，
   在 research 文件里标了【推断】。若某条推断后来被证伪，必须回改实现而不是保留好看的说法。
2. **数量口径以实测为准，不采信前作转述**。本轮已修正三处：
   - 技能类目 **21** 不是 26（`index-cache` 是远端索引缓存，不是类目）
   - 平台适配器 **22** 不是 20（`wecom` 一个目录注册两个 Platform）
   - `_DO_NOT_CAPTURE_BLOCK` 在 0.21.0 **不存在**（全树 grep 0 命中），改抄 5 段等价守门原文
3. **本机 gateway 从未真正接入消息平台**（`channel_directory.json` 的 `platforms` 为空、`pairing\` 为空）。
   平台层按"契约实现 + 可验证的本地回环"验收，不假装接过真实第三方。
4. **`hermes platforms` 不是有效子命令**（实测 invalid choice）。平台操作分散在
   `gateway setup` / `whatsapp` / `whatsapp-cloud` / `slack` / `send` / `pairing` / `status`。
5. **预存债务**：`backend/tests/test_connectors.py::test_browser_append_and_reload` 在本轮开始前
   就是失败的（278 passed / 1 failed），属环境性既有失败，不计入本轮引入的回归。
