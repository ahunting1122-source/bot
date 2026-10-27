# Hermes Agent CLI 命令面完整清单

> **采集对象**：Hermes Agent CN Desktop 发行版，runtime `0.21.0-cn.18`
> **运行时 exe**：`E:\Hermes Agent CN Desktop\data\versions\0.21.0-cn.18\hermes-agent-cn-runtime-win32-x64.exe`
> **`--version` 原文**：`Hermes Agent v0.21.0 (2026.8.31)` / `Install directory: E:\Hermes Agent CN Desktop\data\versions\0.21.0-cn.18\_internal` / `Python: 3.14.7`
> **用途**：为 Orbit 对齐 Hermes 的 CLI 能力面提供权威基线（不是猜测，全部来自 exe 自身的 `--help` 输出）。

---

## 0. 采集方法与统计口径

### 0.1 采集方法（可复现）

| 步骤 | 命令 | 结果 |
|---|---|---|
| 1 | `& $exe --help` | 拿到顶层 74 个命令（含别名共 77 个名字）+ 23 个全局 flag |
| 2 | `& $exe <cmd> --help` × 77 | 77 个顶层命令/别名，全部 exit code 0 |
| 3 | `& $exe <cmd> <sub> --help` × 295 | 全部二级子命令，全部 exit code 0 |
| 4 | `& $exe <cmd> <sub> <sub2> --help` × 36 | 全部三级子命令（8 个嵌套命令组），全部 exit code 0 |
| 5 | `& $exe --version` | 版本 0.21.0 / 2026.8.31 |

**安全约束遵守情况**：全部调用只带 `--help` / `--version`（`--version` 属只读元信息）。未执行任何会改变状态的子命令（`update`/`uninstall`/`pause`/`resume`/`chat`/`serve`/`dashboard`/`setup`/`auth`/`status` 等一律仅加 `--help`）。未修改/删除 Hermes 安装目录与 `hermes-home` 下任何文件。未安装任何依赖。

**原始证据留存**（本仓库内，供复核）：

| 路径 | 内容 |
|---|---|
| `_raw/cmd-01-chat.txt` … `_raw/cmd-77-prompt-size.txt` | 77 个顶层命令 `--help` 原文（含 exit code 行） |
| `_raw/sub/*.txt` | 295 个二级子命令 `--help` 原文 |
| `_raw/sub3/*.txt` | 36 个三级子命令 `--help` 原文 |
| `_raw/sub-digest.txt` / `_raw/sub3-digest.txt` | 全部子命令的 usage 行紧凑汇总 |

### 0.2 数量统计

| 口径 | 数量 |
|---|---|
| 顶层名字（`--help` 的 positional 列表长度） | **77**（74 个独立命令 + 3 个别名） |
| 顶层独立命令 | **74** |
| 二级子命令节点 | **295**（其中 8 个是再分组的命令组） |
| 三级子命令节点 | **36** |
| **可调用的命令节点合计** | **408** |
| 顶层全局 flag | **29** |
| 别名 | `learning` = `memory-graph` = `journey`；`gui` = `desktop` |
| 内部命令（`__` 前缀，SUPPRESS） | **5** |
| exit code | 全部 `--help` 调用返回 **0**（无 1） |

### 0.3 顶层子命令总表（74 行）

「内部命令」列：`是` = `__` 前缀、在 `--help` 列表中被 `==SUPPRESS==` 隐藏；`—` = 正常公开命令。

| # | 命令 | 一句话用途 | 子子命令 | 内部命令 |
|---|---|---|---|---|
| 1 | `chat` | 与 agent 开交互式会话 / 单次问答 | 无 | — |
| 2 | `model` | 交互式选择推理 provider 与默认模型 | 无 | — |
| 3 | `moa` | 配置 `/moa` 用的 Mixture-of-Agents provider/模型槽位 | 3 | — |
| 4 | `fallback` | 管理主模型失败时依次尝试的 fallback provider 链 | 4 | — |
| 5 | `worktree` | 审计与回收累积的 git worktree / 已合并分支 | 2 | — |
| 6 | `browser` | 真实 profile 浏览辅助（关闭占用 profile 的浏览器） | 1 | — |
| 7 | `secrets` | 接入外部密钥源（Bitwarden / 1Password），启动时拉取 API key | 2 | — |
| 8 | `egress` | 管理 iron-proxy 出网凭据注入防火墙 | 9 | — |
| 9 | `migrate` | 迁移已退役模型 / 废弃配置项 | 1 | — |
| 10 | `gateway` | 消息网关管理（Telegram/Discord/WhatsApp/微信等） | 11 | — |
| 11 | `proxy` | 本地 OpenAI 兼容代理，转发到 OAuth provider | 3 | — |
| 12 | `lsp` | 管理 LSP 层（write_file/patch 后的语义诊断） | 6 | — |
| 13 | `setup` | 交互式安装/配置向导 | 0（有 7 个 section 位置参数） | — |
| 14 | `whatsapp` | 配置 WhatsApp 并通过二维码配对（Baileys 桥） | 无 | — |
| 15 | `whatsapp-cloud` | 配置 Meta WhatsApp Business Cloud API 适配器 | 无 | — |
| 16 | `slack` | Slack 集成辅助（app manifest 生成） | 1 | — |
| 17 | `send` | 向已配置的平台发送一条消息（脚本/cron/CI 用） | 无 | — |
| 18 | `login` | **已废弃**；改用 `auth` / `model` / `setup` | 无 | — |
| 19 | `logout` | 清除某推理 provider 的认证 | 无 | — |
| 20 | `auth` | 管理 provider 凭据池（多 key 轮转、Spotify PKCE） | 7 | — |
| 21 | `status` | 显示所有组件状态 | 无 | — |
| 22 | `pause` | 紧急停止：暂停 cron/kanban 派发与新的 gateway 轮次 | 无 | — |
| 23 | `resume` | 解除 `pause` 设置的紧急停止 | 无 | — |
| 24 | `cron` | 定时任务管理 | 13 | — |
| 25 | `sync` | Skill Sync：跨设备/团队同步技能 | 8 | — |
| 26 | `webhook` | 管理事件驱动激活的动态 webhook 订阅 | 4 | — |
| 27 | `peer` | 跨机器 bot-to-bot DM（对等 Hermes 网关） | 7 | — |
| 28 | `portal` | Nous Portal 设置（登录、选模型、Tool Gateway） | 4 | — |
| 29 | `kanban` | 多 profile 协作任务板（任务/依赖/评论） | 46 | — |
| 30 | `project` | 管理项目（命名、多文件夹工作区） | 12 | — |
| 31 | `hooks` | 查看/测试 shell 脚本 hook 与首次使用同意白名单 | 4 | — |
| 32 | `doctor` | 检查配置与依赖 | 无 | — |
| 33 | `verify` | 探测项目 run recipe 并冒烟测试 | 无 | — |
| 34 | `security` | 供应链审计（OSV.dev） | 1 | — |
| 35 | `approvals` | 危险命令审批工具（挖掘历史生成 allowlist 建议） | 2 | — |
| 36 | `dump` | 输出可粘贴的安装摘要（支持/debug 用） | 无 | — |
| 37 | `debug` | 上传日志与系统信息供支持分析 | 2 | — |
| 38 | `backup` | 把 Hermes home 备份成 zip | 无 | — |
| 39 | `checkpoints` | 查看/清理 `~/.hermes/checkpoints/` | 4 | — |
| 40 | `import` | 从 zip 恢复 Hermes 备份 | 无 | — |
| 41 | `import-agent` | 导入 Claude Code / Codex CLI 的配置到 Hermes | 无（有 2 个位置参数取值） | — |
| 42 | `config` | 查看与编辑配置 | 9 | — |
| 43 | `skin` | 列出、切换、微调皮肤 | 3 | — |
| 44 | `console` | 打开受限的 Hermes 命令 REPL | 无 | — |
| 45 | `pairing` | 管理 DM 配对码（用户授权） | 4 | — |
| 46 | `skills` | 搜索/安装/审计/配置/管理技能 | 22 | — |
| 47 | `bundles` | 创建/列出/管理技能包（多技能的别名） | 5 | — |
| 48 | `plugins` | 管理与校验插件 | 11（+`pack` 下 3 个三级） | — |
| 49 | `curator` | 后台技能维护（curator）状态/运行/暂停/固定 | 17 | — |
| 50 | `pets` | 浏览、安装、选择 petdex 动画宠物 | 8 | — |
| 51 | `journey`（别名 `learning`、`memory-graph`） | 已学技能 + 记忆的时间线视图 | 3 | — |
| 52 | `memory` | 配置外部记忆 provider | 4 | — |
| 53 | `tools` | 按平台配置启用哪些工具 | 4 | — |
| 54 | `computer-use` | 管理 Computer Use（cua-driver）后端 | 4（+`permissions` 下 2 个三级） | — |
| 55 | `mcp` | 管理 MCP server，并把 Hermes 当 MCP server 跑 | 12 | — |
| 56 | `sessions` | 管理会话历史（列表/改名/导出/清理/删除） | 19 | — |
| 57 | `insights` | 用量洞察与分析 | 无 | — |
| 58 | `monitoring` | 网关监控（健康与诊断导出） | 1 | — |
| 59 | `claw` | OpenClaw 迁移工具 | 2 | — |
| 60 | `update` | 更新 Hermes Agent | 无 | — |
| 61 | `uninstall` | 卸载 Hermes Agent | 无 | — |
| 62 | `acp` | 以 ACP（Agent Client Protocol）server 运行，供编辑器集成 | 无 | — |
| 63 | `profile` | 管理 profile（多个隔离的 Hermes 实例） | 13 | — |
| 64 | `completion` | 输出 shell 补全脚本（bash/zsh/fish） | 无（有 1 个位置参数取值） | — |
| 65 | `__run-script` | 内部：跑一个脚本文件 | 无 | **是** |
| 66 | `__slash-worker` | 内部：TUI slash-worker 子进程 | 无 | **是** |
| 67 | `__compute-host` | 内部：dashboard compute-host 子进程 | 无 | **是** |
| 68 | `__gateway-restart-watch` | 内部：轮询 PID 后重启 gateway | 无 | **是** |
| 69 | `__update-gateway-helper` | 内部：分离式执行 `hermes update --gateway` | 无 | **是** |
| 70 | `dashboard` | 启动 Web UI dashboard（默认端口 9119） | 1 | — |
| 71 | `serve` | 启动后端 server（headless，供桌面端/远程客户端） | 无 | — |
| 72 | `desktop`（别名 `gui`） | 构建并启动原生桌面应用（Electron） | 无 | — |
| 73 | `logs` | 查看与过滤 Hermes 日志 | 无（有 5 个 log_name 取值） | — |
| 74 | `prompt-size` | 显示 system prompt + tool schema 的字节分解 | 无 | — |

---

## 1. 逐命令详表

> 说明：每个命令给出 **用途 / 用法行 / 全部 options / 子子命令 / 关键行为**。option 的「默认」列取自 `--help` 原文（原文未写默认的记 `—`）。子子命令的 options 见其 usage 行（该行已枚举全部 flag 与取值域），完整帮助原文见 `_raw/sub/`。

---

### 1.1 `chat` — 与 agent 开始交互式会话

**用途**：启动一个 Hermes Agent 交互式对话会话；也可用 `-q` 做单次问答后退出。

**用法**：
```
hermes chat [-h] [-q QUERY | --query-file PATH] [--oneshot] [--image IMAGE]
            [-m MODEL] [-t TOOLSETS] [--reasoning LEVEL] [-s SKILLS]
            [--provider PROVIDER] [-v] [-Q] [--resume SESSION_ID]
            [--no-restore-cwd] [--in DIR] [--continue [SESSION_NAME]]
            [--create-if-missing] [--worktree] [--accept-hooks] [--checkpoints]
            [--max-turns N] [--run-budget SECONDS] [--yolo] [--pass-session-id]
            [--ignore-user-config] [--ignore-rules] [--safe-mode]
            [--source SOURCE] [--tui] [--cli] [--dev]
```

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助并退出 |
| `-q, --query QUERY` | 文本 | — | 要执行的 query。在真实 TTY 上，该 prompt 会作为第一轮喂进交互会话；配合 `--oneshot`/`-Q` 或非 TTY 时，回答后退出 |
| `--query-file PATH` | 路径 / `-`=stdin | — | 从文件读取单条 query。任意文本安全：不做 shell 解释，引号/`$(...)`/反引号原样保留。与 `-q` 互斥 |
| `--oneshot` | flag | 关 | 配合 `-q`/`--query-file`：回答后退出（旧版单次行为），而不是进入交互会话。非 TTY stdio 与 `-Q/--quiet` 会隐式启用 |
| `--image IMAGE` | 本地图片路径 | — | 给单条 query 附加一张图片 |
| `-m, --model MODEL` | 模型名 | 配置默认 | 使用的模型（如 `anthropic/claude-sonnet-4`） |
| `-t, --toolsets TOOLSETS` | 逗号分隔 | 配置默认 | 本次启用的 toolset |
| `--reasoning LEVEL` | `none/minimal/low/medium/high/xhigh/max/ultra` | `agent.reasoning_effort` | 本次运行的推理强度，仅本次生效 |
| `-s, --skills SKILLS` | 技能名，可重复/逗号分隔 | — | 为会话预加载技能 |
| `--provider PROVIDER` | provider 名 | `auto` | 推理 provider。内置或 config.yaml `providers:` 中的自定义名 |
| `-v, --verbose` | flag | 关 | 详细输出 |
| `-Q, --quiet` | flag | 关 | 程序化用静默模式：抑制 banner/spinner/tool 预览，只输出最终回复与会话信息 |
| `--resume, -r SESSION_ID` | 会话 ID 或 `latest` | — | 恢复历史会话 |
| `--no-restore-cwd` | flag | 关 | 不 cd 到被恢复会话记录的 cwd |
| `--in DIR` | 目录 | — | 启动/恢复前先 cd 到 DIR（会把 `--resume latest`/`-c` 的查找限定到 DIR 的工作区） |
| `--continue, -c [SESSION_NAME]` | 名称，可省略 | 最近会话 | 按名字恢复会话；不带名字则取最近 |
| `--create-if-missing` | flag | 关 | 配合 `-c/--continue <name>`：名字不存在时新建该标题会话（而非报 not-found）。供「发到这条命名线程，没有就建」的程序化调用 |
| `--worktree, -w` | flag | 关 | 在隔离 git worktree 中运行（同仓库并行 agent） |
| `--accept-hooks` | flag | 关 | 自动同意 config.yaml 中未见过的新 shell hook，免 TTY 提示（等价 `HERMES_ACCEPT_HOOKS=1` / `hooks_auto_accept: true`） |
| `--checkpoints` | flag | 关 | 破坏性文件操作前启用文件系统 checkpoint（用 `/rollback` 恢复） |
| `--max-turns N` | 整数 | `500`（或 `agent.max_turns`） | 每轮对话的最大工具调用迭代数 |
| `--run-budget SECONDS` | 秒 | 未设置=关 | 每次对话的挂钟预算。到 80% 时给 agent 一次收尾提示，并把隐式 provider stale 超时压到剩余预算。也可配 `agent.run_budget_seconds` |
| `--yolo` | flag | 关 | 绕过所有危险命令审批（风险自负） |
| `--pass-session-id` | flag | 关 | 把会话 ID 放进 system prompt |
| `--ignore-user-config` | flag | 关 | 忽略 `~/.hermes/config.yaml`，回落到内置默认（`.env` 凭据仍加载） |
| `--ignore-rules` | flag | 关 | 跳过 AGENTS.md、SOUL.md、`.cursorrules`、memory 与预加载技能的自动注入 |
| `--safe-mode` | flag | 关 | 排障模式：关闭所有定制（用户配置、AGENTS.md/memory 注入、插件、MCP server），隐含 `--ignore-user-config` + `--ignore-rules` |
| `--source SOURCE` | 文本 | `cli` | 会话来源标签，用于过滤。第三方集成可用 `tool`，使其不出现在用户会话列表 |
| `--tui` | flag | 关 | 启动现代 TUI 而非经典 REPL |
| `--cli` | flag | 关 | 强制经典 prompt_toolkit REPL（覆盖 `display.interface=tui`） |
| `--dev` | flag | 关 | 配合 `--tui`：用 tsx 直接跑 TypeScript 源码（跳过 dist 构建） |

**子子命令**：无。

**关键行为**：`-q` 在 TTY 与在管道里的语义不同（TTY 上会「种入」交互会话）；`--safe-mode` 是排障用的核弹级隔离开关；`--run-budget` 是给评测/一次性调用用的硬上限。

---

### 1.2 `model` — 交互式选择 provider 与默认模型

**用途**：交互式选择推理 provider 和默认模型（写回 config）。

**用法**：`hermes model [-h] [--refresh] [--portal-url PORTAL_URL] [--inference-url INFERENCE_URL] [--client-id CLIENT_ID] [--scope SCOPE] [--no-browser] [--timeout TIMEOUT] [--ca-bundle CA_BUNDLE] [--insecure]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--refresh` | flag | 关 | 清空模型选择器磁盘缓存，重新拉取每个 provider 的实时 `/v1/models` |
| `--portal-url PORTAL_URL` | URL | 生产 Portal | Nous 登录用 Portal base URL |
| `--inference-url INFERENCE_URL` | URL | 生产推理 API | Nous 登录用推理 API base URL |
| `--client-id CLIENT_ID` | 文本 | `hermes-cli` | Nous 登录用 OAuth client id |
| `--scope SCOPE` | 文本 | — | Nous 登录请求的 OAuth scope |
| `--no-browser` | flag | 关 | Nous 登录时不自动开浏览器 |
| `--timeout TIMEOUT` | 秒 | `15` | Nous 登录的 HTTP 超时 |
| `--ca-bundle CA_BUNDLE` | PEM 路径 | — | Nous TLS 校验用 CA bundle |
| `--insecure` | flag | 关 | 关闭 Nous 登录 TLS 校验（仅测试） |

**子子命令**：无。**关键行为**：纯交互命令，无子命令；Orbit 若做模型选择器需对齐 `--refresh` 的缓存失效语义。

---

### 1.3 `moa` — 配置 Mixture-of-Agents 槽位

**用途**：配置 `/moa <prompt>` 使用的 provider/模型集合。

**用法**：`hermes moa [-h] {list,ls,configure,config,delete,rm} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子命令（别名） | 用途 | usage |
|---|---|---|
| `list`（`ls`） | 显示当前 MoA 模型槽位 | `hermes moa list [-h]` |
| `configure`（`config`） | 交互式挑选 MoA 模型 | `hermes moa configure [-h] [name]` |
| `delete`（`rm`） | 删除一个 MoA 预设 | `hermes moa delete [-h] name` |

---

### 1.4 `fallback` — fallback provider 链

**用途**：管理 fallback provider 链。主模型遇限流、过载、连接错误时按顺序尝试。

**用法**：`hermes fallback [-h] {list,ls,add,remove,rm,clear} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子命令（别名） | 用途 | usage |
|---|---|---|
| `list`（`ls`） | 显示当前 fallback 链（无子命令时的默认动作） | `hermes fallback list [-h]` |
| `add` | 选 provider+模型（与 `hermes model` 同一选择器）并追加到链尾 | `hermes fallback add [-h]` |
| `remove`（`rm`） | 选一个条目从链中删除 | `hermes fallback remove [-h]` |
| `clear` | 清空所有 fallback 条目 | `hermes fallback clear [-h]` |

---

### 1.5 `worktree` — worktree 回收

**用途**：回收 `hermes -w` 会话累积的 `.worktrees/` 目录（有人值守）。绝不删除未提交的已跟踪改动、独有的未推送提交、或正在使用的 tree；仅有未跟踪文件的草稿会先归档到 `~/.hermes/archive/worktree-prune/` 再删除。

**用法**：`hermes worktree [-h] {list,ls,audit,prune} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `list`（`ls`、`audit`） | 分类每个 tree：年龄、大小、判定、原因（默认动作） | `hermes worktree list [-h] [--repo REPO]` |
| `prune` | 删除安全的 tree，并删除已完全合并的本地分支 | `hermes worktree prune [-h] [--repo REPO] [--dry-run] [--trees-only] [--branches-only]` |

---

### 1.6 `browser` — 真实 profile 浏览辅助

**用途**：配合 `browser.use_real_profile`。`close-profile` 会终止占用默认 profile 的浏览器进程树，以便 Hermes 复制该 profile——**破坏性**（该浏览器未保存的标签页会丢失）。agent 仅在用户批准后才执行。

**用法**：`hermes browser [-h] {close-profile} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `close-profile` | 关闭占用真实 profile 的浏览器（不做询问——仅在用户明确同意后运行；会丢未保存标签） | `hermes browser close-profile [-h] [--browser BROWSER]` |

---

### 1.7 `secrets` — 外部密钥源

**用途**：进程启动时从外部密钥管理器拉取 API key，而不是存进 `~/.hermes/.env`。支持 Bitwarden Secrets Manager 与 1Password。

**用法**：`hermes secrets [-h] {bitwarden,bw,onepassword,op,1password} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**（二级 → 三级）：

| 子命令（别名） | 用途 | usage |
|---|---|---|
| `bitwarden`（`bw`） | Bitwarden Secrets Manager 集成 | `hermes secrets bitwarden [-h] {setup,status,token,sync,disable,install} ...` |
| `onepassword`（`op`、`1password`） | 1Password（`op://` 引用）集成 | `hermes secrets onepassword [-h] {setup,status,token,set,remove,sync,disable} ...` |

**三级子命令**：

| 三级命令 | usage |
|---|---|
| `secrets bitwarden setup` | `hermes secrets bitwarden setup [-h] [--project-id PROJECT_ID] [--access-token ACCESS_TOKEN] [--server-url SERVER_URL]` |
| `secrets bitwarden status` | `hermes secrets bitwarden status [-h]` |
| `secrets bitwarden token` | `hermes secrets bitwarden token [-h] [--access-token ACCESS_TOKEN] [--no-verify]` |
| `secrets bitwarden sync` | `hermes secrets bitwarden sync [-h] [--apply]` |
| `secrets bitwarden disable` | `hermes secrets bitwarden disable [-h]` |
| `secrets bitwarden install` | `hermes secrets bitwarden install [-h] [--force]` |
| `secrets onepassword setup` | `hermes secrets onepassword setup [-h] [--account ACCOUNT] [--token-env TOKEN_ENV] [--token TOKEN] [--binary-path BINARY_PATH]` |
| `secrets onepassword status` | `hermes secrets onepassword status [-h]` |
| `secrets onepassword token` | `hermes secrets onepassword token [-h] [--token TOKEN] [--no-verify]` |
| `secrets onepassword set` | `hermes secrets onepassword set [-h] env_var reference` |
| `secrets onepassword remove` | `hermes secrets onepassword remove [-h] env_var` |
| `secrets onepassword sync` | `hermes secrets onepassword sync [-h] [--apply]` |
| `secrets onepassword disable` | `hermes secrets onepassword disable [-h]` |

---

### 1.8 `egress` — iron-proxy 出网防火墙

**用途**：管理 iron-proxy —— 可选的 TLS 拦截出网防火墙，在请求离开沙箱前把 proxy token 换成真实 API 凭据。默认禁用。

**用法**：`hermes egress [-h] {install,setup,start,stop,restart,reload,status,disable,config} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `install` | 下载 iron-proxy 二进制（v0.39.0） | `hermes egress install [-h] [--force]` |
| `setup` | 交互式向导：安装 + CA + 铸造 token + 写配置 | `hermes egress setup [-h] [--tunnel-port TUNNEL_PORT] [--from-bitwarden] [--no-bitwarden] [--rotate-tokens] [--restart] [--no-restart]` |
| `start` | 启动受管 iron-proxy | `hermes egress start [-h]` |
| `stop` | 停止受管 iron-proxy | `hermes egress stop [-h]` |
| `restart` | 重启（运行中则先停再起） | `hermes egress restart [-h]` |
| `reload` | 从 proxy.yaml 热重载运行中 daemon 的规则集（管理 API，不重启、不断连） | `hermes egress reload [-h]` |
| `status` | 显示代理状态与映射 | `hermes egress status [-h] [--show-tokens]` |
| `disable` | 关闭代理集成 | `hermes egress disable [-h]` |
| `config` | 打印生成的 proxy.yaml 路径 | `hermes egress config [-h]` |

---

### 1.9 `migrate` — 退役模型/废弃配置迁移

**用途**：诊断并（可选）重写当前 config.yaml，替换对已退役模型或废弃配置项的引用。

**用法**：`hermes migrate [-h] {xai} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `xai` | 迁移计划于 2026-05-15 退役的 xAI 模型 | `hermes migrate xai [-h] [--apply] [--no-backup]` |

---

### 1.10 `gateway` — 消息网关管理

**用途**：管理消息网关（Telegram、Discord、WhatsApp、微信等）。

**用法**：`hermes gateway [-h] [--accept-hooks] {run,start,stop,restart,status,install,uninstall,list,setup,migrate-legacy,enroll} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--accept-hooks` | flag | 关 | 免 TTY 提示自动同意未见过的新 shell hook（等价 `HERMES_ACCEPT_HOOKS=1` / `hooks_auto_accept: true`） |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `run` | 前台运行网关（WSL/Docker/Termux 推荐） | `hermes gateway run [-h] [-v] [-q] [--replace] [--force] [--no-supervise] [--external-supervisor] [--accept-hooks]` |
| `start` | 启动已安装的 systemd/launchd 后台服务 | `hermes gateway start [-h] [--system] [--all]` |
| `stop` | 停止网关服务 | `hermes gateway stop [-h] [--system] [--all]` |
| `restart` | 重启网关服务 | `hermes gateway restart [-h] [--system] [--all]` |
| `status` | 显示网关状态 | `hermes gateway status [-h] [--deep] [-l] [--system]` |
| `install` | 把网关安装为 systemd/launchd 后台服务 | `hermes gateway install [-h] [--force] [--system] [--run-as-user RUN_AS_USER] [--start-now] [--no-start-now] [--start-on-login] [--no-start-on-login]` |
| `uninstall` | 卸载网关服务 | `hermes gateway uninstall [-h] [--system]` |
| `list` | 列出所有 profile 及其网关状态 | `hermes gateway list [-h]` |
| `setup` | 配置消息平台 | `hermes gateway setup [-h]` |
| `migrate-legacy` | 移除改名前的旧 `hermes.service` unit | `hermes gateway migrate-legacy [-h] [--dry-run] [-y]` |
| `enroll` | 用 relay connector 注册本网关（把 relay 凭据写入 `.env`） | `hermes gateway enroll [-h] [--token TOKEN] [--connector-url CONNECTOR_URL] [--gateway-id GATEWAY_ID] [--wake-url WAKE_URL]` |

**关键行为**：`run` 的 `-v/--verbose` 是累加式（`-v`=INFO，`-vv`=DEBUG）；`install` 有 `--start-now`/`--start-on-login` 的正负成对开关；`--system` 只对 Linux 系统级服务有意义。

---

### 1.11 `proxy` — 本地 OpenAI 兼容代理

**用途**：跑一个本地 HTTP server，把 OpenAI 兼容请求转发给已 OAuth 认证的 provider（如 Nous Portal）。外部应用可用任意 bearer token 指向该代理，代理会挂上你的真实凭据。

**用法**：`hermes proxy [-h] {start,status,providers} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `start` | 前台运行代理 | `hermes proxy start [-h] [--provider PROVIDER] [--host HOST] [--port PORT]` |
| `status` | 显示哪些代理上游已就绪 | `hermes proxy status [-h]` |
| `providers` | 列出可用的代理上游 provider | `hermes proxy providers [-h]` |

---

### 1.12 `lsp` — LSP 层管理

**用途**：管理为 `write_file`/`patch` 之后提供语义诊断的 LSP 层。

**用法**：`hermes lsp [-h] {status,list,install,install-all,restart,which} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `status` | 显示 LSP 服务状态 | `hermes lsp status [-h] [--json]` |
| `list` | 列出支持的 language server | `hermes lsp list [-h] [--installed-only]` |
| `install` | 安装某个 server 二进制 | `hermes lsp install [-h] server` |
| `install-all` | 安装所有有已知自动安装 recipe 的 server | `hermes lsp install-all [-h] [--include-manual]` |
| `restart` | 拆掉运行中的 LSP client（下次编辑会重新 spawn） | `hermes lsp restart [-h]` |
| `which` | 打印某 server 的二进制路径 | `hermes lsp which [-h] server` |

---

### 1.13 `setup` — 交互式配置向导

**用途**：用交互式向导配置 Hermes Agent。可只跑某个 section：`hermes setup model|tts|terminal|gateway|tools|telemetry|agent`。

**用法**：`hermes setup [-h] [--non-interactive] [--reset] [--reconfigure] [--quick] [--portal] [{model,tts,terminal,gateway,tools,telemetry,agent}]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| （位置参数） | `model`/`tts`/`terminal`/`gateway`/`tools`/`telemetry`/`agent` | — | 只跑指定 section 而非完整向导 |
| `--non-interactive` | flag | 关 | 非交互模式（用默认值/环境变量） |
| `--reset` | flag | 关 | 重置配置为默认 |
| `--reconfigure` | flag | 已有安装默认 | 重跑完整向导，把当前值作为默认展示。为向后兼容保留——裸 `hermes setup` 现在就是这个行为 |
| `--quick` | flag | 关 | 已有安装上：只询问缺失/未设置的项，而不是跑完整重配置向导 |
| `--portal` | flag | 关 | 一次性 Nous Portal 设置：OAuth 登录、选 Nous 模型、把 Nous 设为推理 provider、opt-in Tool Gateway。跳过向导其余部分 |

**子子命令**：无（7 个 section 是位置参数取值，不是子命令）。

---

### 1.14 `whatsapp` — WhatsApp 个人号配置

**用途**：配置 WhatsApp 并通过二维码配对（Baileys 桥）。

**用法**：`hermes whatsapp [-h]`　|　**options**：仅 `-h, --help`。**子子命令**：无。

---

### 1.15 `whatsapp-cloud` — WhatsApp Business Cloud API

**用途**：配置官方 Meta WhatsApp Business Cloud API 适配器（需 Business 账号 + 公网 webhook URL）。与 `hermes whatsapp`（个人号 Baileys 桥）不同。

**用法**：`hermes whatsapp-cloud [-h]`　|　**options**：仅 `-h, --help`。**子子命令**：无。

---

### 1.16 `slack` — Slack 集成辅助

**用途**：Hermes 的 Slack 集成辅助工具。

**用法**：`hermes slack [-h] {manifest} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `manifest` | 打印或写出 Slack app manifest，把每个网关命令注册为原生 slash（`/btw`、`/stop`、`/model`…） | `hermes slack manifest [-h] [--write [PATH]] [--name NAME] [--description DESCRIPTION] [--long-description TEXT \| --long-description-file PATH] [--slashes-only] [--no-assistant \| --agent-view]` |

---

### 1.17 `send` — 向平台发送消息

**用途**：把任意 shell 脚本的文本管道到 Hermes 已配置的任意消息平台。复用网关的平台凭据（`~/.hermes/.env` + `~/.hermes/config.yaml`）——**无 LLM、无 agent loop**，对 Telegram/Discord/Slack/Signal 这类 bot-token 平台无需运行网关。

**用法**：`hermes send [-h] [-t TARGET] [-f PATH] [-s LINE] [-l] [-q] [--json] [message]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| （位置参数）`message` | 文本 | 从 `--file`/stdin 读 | 消息正文 |
| `-t, --to TARGET` | `platform`、`platform:chat_id`、`platform:chat_id:thread_id`、`platform:#channel-name` | 平台 home channel | 投递目标。例：`telegram`、`telegram:-1001234567890:17585`、`discord:#ops`、`slack:C0123ABCD`、`signal:+15551234567` |
| `-f, --file PATH` | 路径 / `-`=stdin | — | 从文件读消息正文（仅文本）。要发图片/文档附件，在正文用 `MEDIA:<path>` |
| `-s, --subject LINE` | 文本 | — | 在正文前加一行主题/头 |
| `-l, --list` | flag | 关 | 列出可用目标。可带位置参数过滤：`hermes send --list telegram` |
| `-q, --quiet` | flag | 关 | 成功时不输出 stdout（只看 exit code） |
| `--json` | flag | 关 | 输出原始 JSON 结果而非人类可读文本 |

**子子命令**：无。**exit code**：`0` 成功、`1` 投递/后端错误、`2` 用法错误。

---

### 1.18 `login` — 已废弃

**用途**：**已废弃**。改用 `hermes auth` 管理凭据、`hermes model` 选 provider、或 `hermes setup` 完整设置。

**用法**：`hermes login [-h] [--provider PROVIDER] [--portal-url PORTAL_URL] [--inference-url INFERENCE_URL] [--client-id CLIENT_ID] [--scope SCOPE] [--no-browser] [--timeout TIMEOUT] [--ca-bundle CA_BUNDLE] [--insecure]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--provider PROVIDER` | — | — | （已废弃）provider 名；被忽略——见 `hermes model` |
| `--portal-url PORTAL_URL` | URL | 生产 Portal | Portal base URL |
| `--inference-url INFERENCE_URL` | URL | 生产推理 API | 推理 API base URL |
| `--client-id CLIENT_ID` | 文本 | `hermes-cli` | OAuth client id |
| `--scope SCOPE` | 文本 | — | 请求的 OAuth scope |
| `--no-browser` | flag | 关 | 不自动开浏览器 |
| `--timeout TIMEOUT` | 秒 | `15` | HTTP 超时 |
| `--ca-bundle CA_BUNDLE` | PEM 路径 | — | TLS 校验用 CA bundle |
| `--insecure` | flag | 关 | 关闭 TLS 校验（仅测试） |

---

### 1.19 `logout` — 清除 provider 认证

**用途**：移除已存凭据并重置 provider 配置。

**用法**：`hermes logout [-h] [--provider {nous,openai-codex,xai-oauth,spotify}]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--provider` | `nous` / `openai-codex` / `xai-oauth` / `spotify` | 当前活动 provider | 要登出的 provider |

---

### 1.20 `auth` — provider 凭据池

**用途**：管理池化 provider 凭据（多 key 轮转/耗尽状态）。

**用法**：`hermes auth [-h] {add,list,remove,reset,status,logout,spotify} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `add` | 添加一个池化凭据 | `hermes auth add [-h] [--type {oauth,api-key,api_key}] [--label LABEL] [--api-key API_KEY] [--portal-url PORTAL_URL] [--inference-url INFERENCE_URL] [--client-id CLIENT_ID] [--scope SCOPE] [--no-browser] [--timeout TIMEOUT] [--insecure] [--ca-bundle CA_BUNDLE] provider` |
| `list` | 列出池化凭据 | `hermes auth list [-h] [provider]` |
| `remove` | 按 index、id 或 label 移除池化凭据 | `hermes auth remove [-h] provider target` |
| `reset` | 清除某 provider 所有凭据的耗尽状态 | `hermes auth reset [-h] provider` |
| `status` | 显示某 provider 的认证状态 | `hermes auth status [-h] provider` |
| `logout` | 登出某 provider 并清除已存认证状态 | `hermes auth logout [-h] provider` |
| `spotify` | 用 PKCE 为 Hermes 认证 Spotify | `hermes auth spotify [-h] [--client-id CLIENT_ID] [--redirect-uri REDIRECT_URI] [--scope SCOPE] [--no-browser] [--timeout TIMEOUT] [{login,status,logout}]` |

**三级子命令**（`auth spotify` 的取值等价子命令，`--help` 输出相同）：

| 三级命令 | usage |
|---|---|
| `auth spotify login` | `hermes auth spotify [-h] [--client-id ...] [--redirect-uri ...] [--scope ...] [--no-browser] [--timeout ...] [{login,status,logout}]` |
| `auth spotify status` | 同上 |
| `auth spotify logout` | 同上 |

---

### 1.21 `status` — 组件状态

**用途**：显示 Hermes Agent 各组件状态。

**用法**：`hermes status [-h] [--all] [--deep]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--all` | flag | 关 | 显示全部细节（已脱敏，可用于分享） |
| `--deep` | flag | 关 | 跑深度检查（可能更慢） |

**子子命令**：无。

---

### 1.22 `pause` — 紧急停止

**用途**：启用全局紧急停止。只停**新**工作——cron 派发、kanban 派发、新的 gateway 轮次——直到 `hermes resume`。**绝不杀掉进行中的工作**。

**用法**：`hermes pause [-h] [--reason REASON]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--reason REASON` | 文本 | — | 可选原因，存进 sentinel 并展示给用户 |

---

### 1.23 `resume` — 解除紧急停止

**用途**：移除 ESTOP sentinel；下一个 tick 恢复派发。

**用法**：`hermes resume [-h]`　|　**options**：仅 `-h, --help`。**子子命令**：无。

> 注意：`resume` 与 `chat --resume` / `cron resume` 是三件不同的事，Orbit 命名时勿混淆。

---

### 1.24 `cron` — 定时任务

**用途**：管理定时任务。

**用法**：`hermes cron [-h] [--accept-hooks] {list,create,add,edit,pause,resume,run,remove,rm,delete,status,runs,history,incidents,notepad,doctor,tick} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--accept-hooks` | flag | 关 | 免 TTY 自动同意未见过的新 shell hook |

**子子命令**：

| 子子命令（别名） | 用途 | usage |
|---|---|---|
| `list` | 列出定时任务 | `hermes cron list [-h] [--all]` |
| `create`（`add`） | 创建定时任务 | `hermes cron create [-h] [--name NAME] [--deliver DELIVER] [--repeat REPEAT] [--skill SKILLS] [--script SCRIPT] [--no-agent] [--monitor-script MONITOR_SCRIPT] [--monitor-url MONITOR_URL] [--workdir WORKDIR] [--model MODEL] [--provider MODEL_PROVIDER] [--reasoning-effort REASONING_EFFORT] [--continuity] schedule [prompt]` |
| `edit` | 编辑已有任务 | `hermes cron edit [-h] [--schedule SCHEDULE] [--prompt PROMPT] [--name NAME] [--deliver DELIVER] [--repeat REPEAT] [--skill SKILLS] [--add-skill ADD_SKILLS] [--remove-skill REMOVE_SKILLS] [--clear-skills] [--script SCRIPT] [--no-agent] [--agent] [--continuity] [--no-continuity] [--monitor-script ...] [--monitor-url ...] [--workdir WORKDIR] [--model MODEL] [--provider MODEL_PROVIDER] [--reasoning-effort ...] job_id` |
| `pause` | 暂停任务 | `hermes cron pause [-h] job_id` |
| `resume` | 恢复暂停的任务 | `hermes cron resume [-h] [--at RUN_AT] [--run-now] job_id` |
| `run` | 下一个调度 tick 运行该任务 | `hermes cron run [-h] [--accept-hooks] job_id` |
| `remove`（`rm`、`delete`） | 删除任务 | `hermes cron remove [-h] job_id` |
| `status` | 检查 cron 调度器是否在跑 | `hermes cron status [-h]` |
| `runs`（`history`） | 显示持久化执行尝试记录 | `hermes cron runs [-h] [--limit LIMIT] [job_id]` |
| `incidents` | 列出/确认持久化 cron 失败事件 | `hermes cron incidents [-h] [--state {detected,alerted,closed}] [{list,ack}] [incident_id]` |
| `notepad` | 读写任务的持久化 notepad（跨运行的 KV） | `hermes cron notepad [-h] job_id [{get,set,delete,list}] [key] [value]` |
| `doctor` | 检查定时任务的常见健康问题 | `hermes cron doctor [-h]` |
| `tick` | 运行一次到期任务后退出 | `hermes cron tick [-h] [--accept-hooks]` |

**关键行为**：`create` 的核心语义是「`--no-agent` 则完全跳过 LLM，只按时跑 `--script`」；`--continuity` 让每次运行带上该任务上一次的输出（跨运行记忆）；`--monitor-script`/`--monitor-url` 提供外部信号触发；`schedule` 是位置参数（cron 表达式/自然语言），`prompt` 可选。

---

### 1.25 `sync` — Skill Sync

**用途**：Skill Sync 让你的技能跟着你走。个人同步在你自己的设备间移动技能；若你属于某个组织，还会拿到组织的共享技能，并可把自己的技能提议给团队。

**用法**：`hermes sync [-h] {status,pull,push,now,enable,disable,device,propose} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `status` | 显示同步了什么、来自哪里 | `hermes sync status [-h]` |
| `pull` | 拉取你（及你组织）的已同步技能 | `hermes sync pull [-h]` |
| `push` | 推送你已 opt-in 的技能 | `hermes sync push [-h]` |
| `now` | 立即对账：先 pull 再 push | `hermes sync now [-h]` |
| `enable` | 把某技能纳入同步 | `hermes sync enable [-h] skill` |
| `disable` | 把某技能排除出同步 | `hermes sync disable [-h] skill` |
| `device` | 显示或设置本设备标签（同步控制台里显示） | `hermes sync device [-h] [--name DEVICE_NAME]` |
| `propose` | 把一个技能分享给组织 | `hermes sync propose [-h] [-m MESSAGE] name` |

---

### 1.26 `webhook` — 动态 webhook 订阅

**用途**：创建、列出、删除用于事件驱动 agent 激活的 webhook 订阅。

**用法**：`hermes webhook [-h] {subscribe,add,list,ls,remove,rm,test} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令（别名） | 用途 | usage |
|---|---|---|
| `subscribe`（`add`） | 创建 webhook 订阅 | `hermes webhook subscribe [-h] [--prompt PROMPT] [--events EVENTS] [--description DESCRIPTION] [--skills SKILLS] [--deliver DELIVER] [--deliver-chat-id DELIVER_CHAT_ID] [--secret SECRET] [--deliver-only] [--script SCRIPT] name` |
| `list`（`ls`） | 列出所有动态订阅 | `hermes webhook list [-h]` |
| `remove`（`rm`） | 删除订阅 | `hermes webhook remove [-h] name` |
| `test` | 向 webhook 路由发一次测试 POST | `hermes webhook test [-h] [--payload PAYLOAD] name` |

**关键行为**：`--prompt` 支持 `{dot.notation}` 载荷引用；`--secret` 是 HMAC 密钥（省略则自动生成）；`--deliver-only` 跳过 agent，直接投递渲染后的 prompt；`--script` 是 `~/.hermes/scripts/` 下的过滤/转换脚本。

---

### 1.27 `peer` — 跨机器 bot-to-bot DM

**用途**：把其它 Hermes 网关注册为 peer 并给它们的 agent 发消息。`hermes peer dm <peer>[/<agent>] "..."` 通过 peer 的 API server 投递到远端 agent 的规范 Bot Chat 并打印回复——是 `hermes -p <bot> chat` 的跨机器孪生。peer 必须运行 `api_server` 平台；其 `API_SERVER_KEY` 以凭据形式存进本地 `~/.hermes/.env`。

**用法**：`hermes peer [-h] {add,set,list,ls,remove,rm,dm,run,status,stop} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令（别名） | 用途 | usage |
|---|---|---|
| `add`（`set`） | 注册（或更新）一个 peer 网关 | `hermes peer add [-h] --url URL [--key KEY] [--note NOTE] name` |
| `list`（`ls`） | 列出已注册 peer | `hermes peer list [-h]` |
| `remove`（`rm`） | 移除 peer | `hermes peer remove [-h] name` |
| `dm` | 给 peer 上的 agent 发消息并打印回复 | `hermes peer dm [-h] [--json] target [message]` |
| `run` | 异步启动一个长 peer 轮次并返回 run ID | `hermes peer run [-h] [--idempotency-key IDEMPOTENCY_KEY] [--json] target [message]` |
| `status` | 读取异步 peer run 的状态与最终输出 | `hermes peer status [-h] [--json] target run_id` |
| `stop` | 停掉一个异步 peer run（不影响其它轮次） | `hermes peer stop [-h] [--json] target run_id` |

**exit code**：`0` 成功、`1` 投递/peer 错误、`2` 用法错误。

---

### 1.28 `portal` — Nous Portal

**用途**：裸跑 `hermes portal` 即登录 Nous Portal 并完成设置——选模型、把 Nous 设为 provider、提供 Tool Gateway（`hermes auth add nous --type oauth` 的人类可读别名，等价 `hermes setup --portal`）。

**用法**：`hermes portal [-h] {login,info,status,open,tools} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `login` | 登录 Nous Portal + 完成设置（默认动作；一次性 onboarding） | `hermes portal login [-h]` |
| `info` | 显示 Portal 认证 + Tool Gateway 路由摘要 | `hermes portal info [-h]` |
| `open` | 用默认浏览器打开 Portal 订阅页 | `hermes portal open [-h]` |
| `tools` | 列出 Tool Gateway 工具及哪些经 Nous 路由 | `hermes portal tools [-h]` |

> 文档与 usage 里出现 `status` 但子命令表未列（`{login,info,status,open,tools}` 中有 `status`，描述段只提 login/info/open/tools）——属 Hermes 自身文档不一致，Orbit 对齐时以子命令集合为准。

---

### 1.29 `kanban` — 多 profile 协作任务板

**用途**：SQLite 持久化的任务板，跨 Hermes profile 共享。任务被原子认领、可依赖其它任务、由指定 profile 在隔离工作区执行。

**用法**：`hermes kanban [-h] [--board <slug>] {init,boards,create,swarm,list,ls,show,assign,set-model,reclaim,reassign,diagnostics,diag,link,unlink,claim,comment,attach,attachments,attach-rm,complete,edit,block,schedule,unblock,request-review,request-changes,reopen-review,promote,archive,tail,dispatch,daemon,watch,stats,notify-subscribe,notify-list,notify-unsubscribe,log,runs,heartbeat,assignees,context,specify,decompose,gc,repair} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--board <slug>` | board slug | 当前 board（由 `hermes kanban boards switch <slug>` 或 `HERMES_KANBAN_BOARD` 决定） | 要操作的 board |

**子子命令（46 个）**：

| 子子命令（别名） | 用途 | usage |
|---|---|---|
| `init` | 缺则创建 `kanban.db`（幂等） | `hermes kanban init [-h]` |
| `boards` | 管理 board（每项目/工作流一个） | `hermes kanban boards [-h] {list,ls,create,new,rm,remove,delete,switch,use,show,current,rename,set-default-workdir,export,import} ...` |
| `create` | 创建任务 | `hermes kanban create [-h] [--body BODY] [--assignee ASSIGNEE] [--parent PARENT] [--workspace WORKSPACE] [--branch BRANCH] [--project PROJECT] [--tenant TENANT] [--priority PRIORITY] [--triage] [--idempotency-key IDEMPOTENCY_KEY] [--max-runtime MAX_RUNTIME] [--created-by CREATED_BY] [--skill SKILLS] [--max-retries N] [--model MODEL_OVERRIDE] [--provider PROVIDER_OVERRIDE] [--goal] [--goal-max-turns N] [--initial-status {blocked,running}] [--json] title` |
| `swarm` | 创建 Kanban Swarm v1 图（并行 worker → verifier → synthesizer） | `hermes kanban swarm [-h] [--worker PROFILE:TITLE[:SKILL,SKILL]] --verifier VERIFIER --synthesizer SYNTHESIZER [--tenant TENANT] [--priority PRIORITY] [--created-by CREATED_BY] [--idempotency-key ...] [--json] goal` |
| `list`（`ls`） | 列出任务 | `hermes kanban list [-h] [--mine] [--assignee ASSIGNEE] [--status {archived,blocked,done,ready,review,running,scheduled,todo,triage}] [--tenant TENANT] [--session SESSION] [--archived] [--json] [--sort {assignee,created,created-desc,priority,priority-desc,status,title,updated}] [--workflow-template-id ID] [--step-key KEY]` |
| `show` | 显示任务及其评论+事件 | `hermes kanban show [-h] [--json] [--state-type {status,outcome}] [--state-name VALUE] task_id` |
| `assign` | 指派/改派任务 | `hermes kanban assign [-h] task_id profile` |
| `set-model` | 设置/清除任务的模型/provider 覆盖（下次派发生效） | `hermes kanban set-model [-h] [--provider PROVIDER] task_id [model]` |
| `reclaim` | 释放运行中任务的 worker 认领 | `hermes kanban reclaim [-h] [--reason REASON] task_id` |
| `reassign` | 改派到另一 profile，可选先 reclaim | `hermes kanban reassign [-h] [--reclaim] [--reason REASON] task_id profile` |
| `diagnostics`（`diag`） | 列出当前 board 的活动诊断 | `hermes kanban diagnostics [-h] [--severity {warning,error,critical}] [--task TASK] [--json]` |
| `link` | 添加 parent→child 依赖 | `hermes kanban link [-h] parent_id child_id` |
| `unlink` | 移除 parent→child 依赖 | `hermes kanban unlink [-h] parent_id child_id` |
| `claim` | 原子认领一个 ready 任务（打印解析出的工作区路径） | `hermes kanban claim [-h] [--ttl TTL] task_id` |
| `comment` | 追加评论 | `hermes kanban comment [-h] [--author AUTHOR] [--max-len MAX_LEN] task_id text [text ...]` |
| `attach` | 把本地文件挂到任务 | `hermes kanban attach [-h] [--content-type CONTENT_TYPE] [--name NAME] [--author AUTHOR] task_id path` |
| `attachments` | 列出任务附件 | `hermes kanban attachments [-h] [--json] task_id` |
| `attach-rm` | 按 id 删除附件 | `hermes kanban attach-rm [-h] attachment_id` |
| `complete` | 把一个或多个任务标为 done | `hermes kanban complete [-h] [--result RESULT] [--summary SUMMARY] [--metadata METADATA] task_ids [task_ids ...]` |
| `edit` | 编辑已完成任务的恢复字段 | `hermes kanban edit [-h] --result RESULT [--summary SUMMARY] [--metadata METADATA] task_id` |
| `block` | 把一个或多个任务标为 blocked | `hermes kanban block [-h] [--ids IDS [IDS ...]] [--kind {capability,dependency,needs_input,transient}] task_id [reason ...]` |
| `schedule` | 把任务停在 Scheduled（等时间，非等人） | `hermes kanban schedule [-h] [--ids IDS [IDS ...]] task_id [reason ...]` |
| `unblock` | 把 blocked/scheduled 任务放回 ready（父任务未完成则回 todo） | `hermes kanban unblock [-h] [--reason REASON] task_ids [task_ids ...]` |
| `request-review` | 把任务移到 `review`（实现完成、待评审）——不是 block | `hermes kanban request-review [-h] [--summary SUMMARY] [--reviewer REVIEWER] [--metadata METADATA] [--force] task_id` |
| `request-changes` | 评审结论：把活动 review run 退回实现者 | `hermes kanban request-changes [-h] task_id reason [reason ...]` |
| `reopen-review` | 把一个或多个 review 任务退回改（review → ready/todo） | `hermes kanban reopen-review [-h] [--reason REASON] task_ids [task_ids ...]` |
| `promote` | 手动把 todo/blocked 任务移到 ready（恢复路径） | `hermes kanban promote [-h] [--ids IDS [IDS ...]] [--force] [--dry-run] [--json] task_id [reason ...]` |
| `archive` | 归档一个或多个任务 | `hermes kanban archive [-h] [--rm PURGE_IDS [PURGE_IDS ...]] [task_ids ...]` |
| `tail` | 跟随任务的事件流 | `hermes kanban tail [-h] [--interval INTERVAL] task_id` |
| `dispatch` | 一趟 dispatcher：回收 stale、提升 ready、spawn worker | `hermes kanban dispatch [-h] [--dry-run] [--max MAX] [--failure-limit FAILURE_LIMIT] [--json]` |
| `daemon` | **已废弃** —— dispatcher 现在跑在 gateway 里，改用 `hermes gateway start` | `hermes kanban daemon [-h] [--interval INTERVAL] [--max MAX] [--failure-limit FAILURE_LIMIT] [--pidfile PIDFILE] [--verbose]` |
| `watch` | 把 task_events 实时流到终端（Ctrl+C 退出） | `hermes kanban watch [-h] [--assignee ASSIGNEE] [--tenant TENANT] [--kinds KINDS] [--interval INTERVAL]` |
| `stats` | 各状态/各 assignee 计数 + 最老 ready 年龄 | `hermes kanban stats [-h] [--json]` |
| `notify-subscribe` | 把网关来源订阅到任务的终态事件（供 `/kanban subscribe`） | `hermes kanban notify-subscribe [-h] --platform PLATFORM --chat-id CHAT_ID [--thread-id THREAD_ID] [--user-id USER_ID] [--user-id-alt USER_ID_ALT] [--chat-type {dm,group,channel,thread}] [--notifier-profile NOTIFIER_PROFILE] [--delivery-mode {notify,notify+wake,wake}] task_id` |
| `notify-list` | 列出通知订阅（可按单任务） | `hermes kanban notify-list [-h] [--json] [task_id]` |
| `notify-unsubscribe` | 移除任务上的网关订阅 | `hermes kanban notify-unsubscribe [-h] --platform PLATFORM --chat-id CHAT_ID [--thread-id THREAD_ID] task_id` |
| `log` | 打印任务的 worker 日志（`<kanban-root>/kanban/logs/`） | `hermes kanban log [-h] [--tail TAIL] task_id` |
| `runs` | 显示任务的尝试历史（每次运行一行：profile、结果、耗时、摘要） | `hermes kanban runs [-h] [--json] [--state-type {status,outcome}] [--state-name VALUE] task_id` |
| `heartbeat` | 为运行中任务发心跳事件（worker 存活信号） | `hermes kanban heartbeat [-h] [--note NOTE] task_id` |
| `assignees` | 列出已知 profile + 各 profile 任务数 | `hermes kanban assignees [-h] [--json]` |
| `context` | 打印 worker 看到的完整上下文（标题+正文+父任务结果+评论） | `hermes kanban context [-h] task_id` |
| `specify` | 把 triage 列任务补成具体 spec 并提升到 todo（用 `auxiliary.triage_specifier`） | `hermes kanban specify [-h] [--all] [--tenant TENANT] [--author AUTHOR] [--json] [task_id]` |
| `decompose` | 把 triage 任务拆成子任务图并按描述路由给专家 profile（用 `auxiliary.kanban_decomposer`） | `hermes kanban decompose [-h] [--all] [--tenant TENANT] [--author AUTHOR] [--json] [task_id]` |
| `gc` | 垃圾回收归档任务的 workspace、旧事件、旧日志 | `hermes kanban gc [-h] [--event-retention-days EVENT_RETENTION_DAYS] [--log-retention-days LOG_RETENTION_DAYS]` |
| `repair` | 检查 `kanban.db` 完整性并自动修复仅索引损坏 | `hermes kanban repair [-h] [--json]` |

**三级子命令**（`kanban boards`）：

| 三级命令 | usage |
|---|---|
| `kanban boards list` | `hermes kanban boards list [-h] [--json] [--all]` |
| `kanban boards create` | `hermes kanban boards create [-h] [--name NAME] [--description DESCRIPTION] [--icon ICON] [--color COLOR] [--switch] [--default-workdir DEFAULT_WORKDIR] slug` |
| `kanban boards rm` | `hermes kanban boards rm [-h] [--delete] slug` |
| `kanban boards switch` | `hermes kanban boards switch [-h] slug` |
| `kanban boards show` / `current` | `hermes kanban boards show [-h]`（`current` 是 `show` 的别名） |
| `kanban boards rename` | `hermes kanban boards rename [-h] slug name` |
| `kanban boards set-default-workdir` | `hermes kanban boards set-default-workdir [-h] slug [path]` |
| `kanban boards export` | `hermes kanban boards export [-h] [-o OUTPUT] [--no-attachments] [--include-logs] [--json] [slug]` |
| `kanban boards import` | `hermes kanban boards import [-h] [--as AS_SLUG] [--switch] [--json] archive` |

**关键行为**：`create` 的状态机入口有 `--triage`（进 triage 等 specifier）与 `--initial-status {blocked,running}`；`--goal` 让 worker 跑 goal loop（`--goal-max-turns` 默认 **20**）；`daemon` 已废弃，dispatcher 归 gateway。

---

### 1.30 `project` — 项目管理

**用途**：项目是人工命名的、可跨多个文件夹/仓库的工作区。它锚定桌面端的会话分组；绑定 kanban board 后为任务提供确定的 worktree + 分支约定。状态按 profile 隔离。

**用法**：`hermes project [-h] {create,list,ls,show,add-folder,remove-folder,rename,set-primary,use,archive,restore,bind-board} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令（别名） | 用途 | usage |
|---|---|---|
| `create` | 新建项目 | `hermes project create [-h] [--slug SLUG] [--primary PATH] [--description DESCRIPTION] [--icon ICON] [--color COLOR] [--board SLUG] [--use] name [folders ...]` |
| `list`（`ls`） | 列出项目 | `hermes project list [-h] [--all]` |
| `show` | 显示项目详情 | `hermes project show [-h] project` |
| `add-folder` | 给项目加文件夹 | `hermes project add-folder [-h] [--label LABEL] [--primary] project path` |
| `remove-folder` | 从项目移除文件夹 | `hermes project remove-folder [-h] project path` |
| `rename` | 重命名项目 | `hermes project rename [-h] project name` |
| `set-primary` | 设置主文件夹 | `hermes project set-primary [-h] project path` |
| `use` | 设为活动项目 | `hermes project use [-h] [project]` |
| `archive` | 归档项目 | `hermes project archive [-h] project` |
| `restore` | 恢复归档项目 | `hermes project restore [-h] project` |
| `bind-board` | 把 kanban board 绑到项目 | `hermes project bind-board [-h] project [board]` |

---

### 1.31 `hooks` — shell 脚本 hook

**用途**：查看 `~/.hermes/config.yaml` 中声明的 shell 脚本 hook，用合成载荷测试它们，并管理首次使用同意白名单 `~/.hermes/shell-hooks-allowlist.json`。

**用法**：`hermes hooks [-h] {list,ls,test,revoke,remove,rm,doctor} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令（别名） | 用途 | usage |
|---|---|---|
| `list`（`ls`） | 列出已配置 hook 及 matcher、超时、同意状态 | `hermes hooks list [-h]` |
| `test` | 用合成载荷触发所有匹配 `<event>` 的 hook | `hermes hooks test [-h] [--for-tool FOR_TOOL] [--payload-file PAYLOAD_FILE] event` |
| `revoke`（`remove`、`rm`） | 移除某命令的白名单条目（下次重启生效） | `hermes hooks revoke [-h] command` |
| `doctor` | 逐个检查：exec 位、白名单、mtime 漂移、JSON 合法性、合成运行耗时 | `hermes hooks doctor [-h]` |

---

### 1.32 `doctor` — 诊断

**用途**：诊断 Hermes Agent 设置的问题。

**用法**：`hermes doctor [-h] [--fix] [--live] [--ack ADVISORY_ID]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--fix` | flag | 关 | 尝试自动修复问题 |
| `--live` | flag | 关 | opt-in：静态检查后，对每个已配置工具后端（Firecrawl/FAL/browser/MCP/TTS/STT）跑一次有界、只读的真实调用健康探针。会发真实网络请求 |
| `--ack ADVISORY_ID` | 公告 ID | — | 按 ID 确认某安全公告并退出；确认后该公告不再触发启动横幅 |

**子子命令**：无。

---

### 1.33 `verify` — 项目 run recipe 探测与冒烟测试

**用途**：探测当前项目如何 bootstrap/build/test/start（或读取 `.hermes/environment.json` 里保存的 manifest），然后跑一遍验证：bootstrap → build → test → 后台启动 → 轮询就绪 → teardown。

**用法**：`hermes verify [-h] [--detect-only] [--save] [--skip-start] [--phase {bootstrap,build,test,start}] [--port PORT] [--timeout TIMEOUT] [--ready-timeout READY_TIMEOUT] [--json] [path]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| （位置参数）`path` | 目录 | 当前目录 | 要验证的项目根 |
| `--detect-only` | flag | 关 | 只探测并以 JSON 打印 recipe，不执行任何东西 |
| `--save` | flag | 关 | 把 recipe 存为项目内 `.hermes/environment.json` |
| `--skip-start` | flag | 关 | 跑命令阶段但跳过启动应用/就绪轮询 |
| `--phase` | `bootstrap`/`build`/`test`/`start` | 全部 | 只跑给定阶段（可重复） |
| `--port PORT` | 整数 | — | 覆盖就绪轮询用的端口 |
| `--timeout TIMEOUT` | 秒 | `600` | 每阶段超时 |
| `--ready-timeout READY_TIMEOUT` | 秒 | `60` | 就绪轮询超时 |
| `--json` | flag | 关 | 输出机器可读 JSON 结果 |

**子子命令**：无。

---

### 1.34 `security` — 供应链审计

**用途**：对 OSV.dev 的按需漏洞扫描。覆盖 Hermes venv（已装 PyPI dist）、`~/.hermes/plugins/` 下插件声明的 Python 依赖、以及 config.yaml 中固定版本的 npx/uvx MCP server。**不**扫全局安装的包或编辑器/浏览器扩展。

**用法**：`hermes security [-h] <subcommand> ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `audit` | 跑一次供应链审计 | `hermes security audit [-h] [--json] [--fail-on {low,moderate,high,critical}] [--skip-venv] [--skip-plugins] [--skip-mcp]` |

---

### 1.35 `approvals` — 危险命令审批工具

**用途**：危险命令审批系统的工具。`hermes approvals suggest` 从会话数据库挖掘历史审批决策，提议 `command_allowlist` 条目，让反复被批准的命令不再提示。

**用法**：`hermes approvals [-h] <subcommand> ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `suggest` | 从历史审批提议 `command_allowlist` 条目 | `hermes approvals suggest [-h] [--apply N[,M...]] [--json] [--days DAYS] [--min-count MIN_COUNT] [--limit LIMIT] [--db DB]` |
| `test` | 干跑某命令的审批判定（**绝不执行**） | `hermes approvals test [-h] [--env-type ENV_TYPE] [--json] ...` |

---

### 1.36 `dump` — 安装摘要

**用途**：输出紧凑的纯文本安装摘要，可复制粘贴到 Discord/GitHub 作为支持上下文。

**用法**：`hermes dump [-h] [--show-keys]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--show-keys` | flag | 关 | 显示脱敏的 API key 前后各 4 位，而不是只显示 set/not set |

---

### 1.37 `debug` — 上传调试信息

**用途**：调试工具。`hermes debug share` 把调试报告（系统信息 + 近期日志）上传到 paste 服务并给出可分享 URL。

**用法**：`hermes debug [-h] {share,delete} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `share` | 上传调试报告到 paste 服务并打印可分享 URL | `hermes debug share [-h] [--lines LINES] [--expire EXPIRE] [--local] [-y] [--no-redact] [--nous]` |
| `delete` | 删除 `hermes debug share` 上传的 paste | `hermes debug delete [-h] [urls ...]` |

---

### 1.38 `backup` — 备份

**用途**：把整个 Hermes 配置、技能、会话与数据打成 zip（不含 hermes-agent 代码库）。用 `--quick` 做仅关键状态文件的快速快照。

**用法**：`hermes backup [-h] [-o OUTPUT] [-q] [-l LABEL]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `-o, --output OUTPUT` | 路径 | `~/hermes-backup-<timestamp>.zip` | 输出 zip 路径 |
| `-q, --quick` | flag | 关 | 快速快照：仅关键状态文件（config、state.db、.env、auth、cron） |
| `-l, --label LABEL` | 文本 | — | 快照标签（仅配合 `--quick`） |

---

### 1.39 `checkpoints` — 文件 checkpoint 存储

**用途**：管理文件系统 checkpoint 存储——hermes 在 `write_file`/`patch`/`terminal` 前用来给工作目录打快照的影子 git 仓库。可查看占用空间、强制 prune、或清空基座。

**用法**：`hermes checkpoints [-h] COMMAND ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `status` | 显示总大小、项目数、逐项目明细 | `hermes checkpoints status [-h] [--limit LIMIT]` |
| `list` | `status` 的别名 | 同 `status` |
| `prune` | 删除孤儿/陈旧 checkpoint 并 GC 存储 | `hermes checkpoints prune [-h] [--retention-days RETENTION_DAYS] [--max-size-mb MAX_SIZE_MB] [--keep-orphans] [-f]` |
| `clear` | 删除整个 checkpoint 基座（所有 `/rollback` 历史） | `hermes checkpoints clear [-h] [-f]` |
| `clear-legacy` | 只删除 v1 迁移遗留的 `legacy-<ts>/` 归档 | `hermes checkpoints clear-legacy [-h] [-f]` |

---

### 1.40 `import` — 从备份恢复

**用途**：把之前创建的 Hermes 备份解压到 Hermes home，恢复配置、技能、会话与数据。

**用法**：`hermes import [-h] [--force] zipfile`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| （位置参数）`zipfile` | 路径 | — | 备份 zip 路径 |
| `--force, -f` | flag | 关 | 不确认直接覆盖已存在文件 |

---

### 1.41 `import-agent` — 从 Claude Code / Codex CLI 导入

**用途**：一条命令把另一个编码 agent 的设置导入 Hermes。把 `CLAUDE.md`/`AGENTS.md` 指令、权限 allowlist、MCP server、技能与记忆映射到 Hermes 对应物。**总是先展示预览**再改动。API key 与凭据**永不导入**——用 `hermes setup`。

**用法**：`hermes import-agent [-h] [--source SOURCE] [--dry-run] [--overwrite] [--yes] [{claude-code,codex}]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| （位置参数） | `claude-code` / `codex` | 自动探测 `~/.claude` 或 `~/.codex` | 从哪个 agent 导入 |
| `--source SOURCE` | 路径 | `~/.claude` 或 `~/.codex` | 该 agent 的配置目录 |
| `--dry-run` | flag | 关 | 只预览——展示完要导入什么就停 |
| `--overwrite` | flag | 关（冲突时跳过） | 同名冲突时覆盖已存在的 Hermes 条目 |
| `--yes, -y` | flag | 关 | 跳过确认提示 |

---

### 1.42 `config` — 配置管理

**用途**：管理 Hermes Agent 配置。

**用法**：`hermes config [-h] {show,edit,get,set,unset,path,env-path,check,migrate} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `show` | 显示当前配置 | `hermes config show [-h]` |
| `edit` | 用编辑器打开配置文件 | `hermes config edit [-h]` |
| `get` | 打印某个已解析的配置值 | `hermes config get [-h] [--json] [key]` |
| `set` | 设置配置值 | `hermes config set [-h] [--force] [key] [value]` |
| `unset` | 删除配置值 | `hermes config unset [-h] [key]` |
| `path` | 打印配置文件路径 | `hermes config path [-h]` |
| `env-path` | 打印 `.env` 文件路径 | `hermes config env-path [-h]` |
| `check` | 检查缺失/过期的配置 | `hermes config check [-h]` |
| `migrate` | 用新选项更新配置 | `hermes config migrate [-h]` |

**关键行为**：`set` 的 `--force` 用于「写入一个 Hermes 不认识的 key 后，跳过 unknown-key 提示」。

---

### 1.43 `skin` — 皮肤

**用途**：管理 Hermes 皮肤。`set` 就地微调活动皮肤的某个颜色。

**用法**：`hermes skin [-h] {list,use,set} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `list` | 列出可用皮肤 | `hermes skin list [-h]` |
| `use` | 切换活动皮肤 | `hermes skin use [-h] name` |
| `set` | 设置活动皮肤的某个颜色（如 `skin set ui_tool '#00FFFF'`） | `hermes skin set [-h] [--skin SKIN] key value` |

---

### 1.44 `console` — 受限命令 REPL

**用途**：打开一个精选的 Hermes 命令 REPL。**不是**裸 shell，也不暴露完整 Hermes CLI。

**用法**：`hermes console [-h]`　|　**options**：仅 `-h, --help`。**子子命令**：无。

---

### 1.45 `pairing` — DM 配对码

**用途**：通过配对码批准或撤销用户访问。

**用法**：`hermes pairing [-h] {list,approve,revoke,clear-pending} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `list` | 显示待批准 + 已批准用户 | `hermes pairing list [-h]` |
| `approve` | 批准一个配对请求 | `hermes pairing approve [-h] platform request-id\|code` |
| `revoke` | 撤销用户访问 | `hermes pairing revoke [-h] platform user_id` |
| `clear-pending` | 清空所有待批准码 | `hermes pairing clear-pending [-h]` |

---

### 1.46 `skills` — 技能管理

**用途**：从 skills.sh、well-known agent skill 端点、GitHub、ClawHub 等注册源搜索、安装、检视、审计、配置与管理技能。

**用法**：`hermes skills [-h] {trust,untrust,browse,search,install,inspect,list,check,update,audit,uninstall,reset,list-modified,diff,opt-out,opt-in,repair-official,publish,snapshot,tap,config} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令（22 个）**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `trust` | 信任某项目，使其仓库内技能（`./.hermes/skills`、`./.agents/skills`）可加载 | `hermes skills trust [-h] [path]` |
| `untrust` | 撤销某仓库的项目技能信任 | `hermes skills untrust [-h] [path]` |
| `browse` | 分页浏览所有可用技能 | `hermes skills browse [-h] [--page PAGE] [--size SIZE] [--source {all,official,skills-sh,well-known,github,clawhub,lobehub,browse-sh,nvidia,openai,anthropic,huggingface,voltagent,gstack,minimax}]` |
| `search` | 搜索技能注册源 | `hermes skills search [-h] [--source {all,official,skills-sh,well-known,github,clawhub,lobehub,browse-sh,nvidia,openai,anthropic,huggingface,voltagent,gstack,minimax}] [--limit LIMIT] [--json] query` |
| `install` | 安装技能 | `hermes skills install [-h] [--category CATEGORY] [--name NAME] [--force] [--yes] identifier` |
| `inspect` | 不安装，预览技能 | `hermes skills inspect [-h] identifier` |
| `list` | 列出已安装技能 | `hermes skills list [-h] [--source {all,hub,builtin,local}] [--enabled-only]` |
| `check` | 检查已安装 hub 技能的更新 | `hermes skills check [-h] [name]` |
| `update` | 更新已安装 hub 技能 | `hermes skills update [-h] [--force] [name]` |
| `audit` | 重新扫描已安装 hub 技能 | `hermes skills audit [-h] [--deep] [name]` |
| `uninstall` | 移除 hub 安装的技能 | `hermes skills uninstall [-h] [--yes] name` |
| `reset` | 重置内置技能——清除 `user-modified` 跟踪，使更新重新可用 | `hermes skills reset [-h] [--restore] [--yes] name` |
| `list-modified` | 列出你改过的内置技能（`hermes update` 会保留） | `hermes skills list-modified [-h] [--json]` |
| `diff` | 显示你的内置技能副本与出厂版的差异 | `hermes skills diff [-h] name` |
| `opt-out` | 停止把内置技能播种到本 profile | `hermes skills opt-out [-h] [--remove] [--yes]` |
| `opt-in` | 重新启用内置技能播种（撤销 opt-out） | `hermes skills opt-in [-h] [--sync]` |
| `repair-official` | 从仓库源回填或恢复官方可选技能 | `hermes skills repair-official [-h] [--restore] [--yes] name` |
| `publish` | 把技能发布到注册源 | `hermes skills publish [-h] [--to {github,clawhub}] [--repo REPO] skill_path` |
| `snapshot` | 导出/导入技能配置 | `hermes skills snapshot [-h] {export,import} ...` |
| `tap` | 管理技能来源 | `hermes skills tap [-h] {list,add,remove} ...` |
| `config` | 交互式技能配置——逐个启用/禁用技能 | `hermes skills config [-h]` |

**三级子命令**：

| 三级命令 | usage |
|---|---|
| `skills tap list` | `hermes skills tap list [-h]` |
| `skills tap add` | `hermes skills tap add [-h] repo` |
| `skills tap remove` | `hermes skills tap remove [-h] name` |
| `skills snapshot export` | `hermes skills snapshot export [-h] output` |
| `skills snapshot import` | `hermes skills snapshot import [-h] [--force] input` |

**关键行为**：`--source` 的取值域就是 Hermes 支持的**全部注册源**（15 个）：`all/official/skills-sh/well-known/github/clawhub/lobehub/browse-sh/nvidia/openai/anthropic/huggingface/voltagent/gstack/minimax`。`install --force` 是「即使扫描判定为 blocked 也安装」。

---

### 1.47 `bundles` — 技能包

**用途**：技能包让你用一个 slash 命令加载多个技能。CLI 或网关里的 `/<bundle>` 会一次加载引用的全部技能。

**用法**：`hermes bundles [-h] {list,show,create,delete,reload} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `list` | 列出已安装技能包 | `hermes bundles list [-h]` |
| `show` | 显示某技能包内容 | `hermes bundles show [-h] name` |
| `create` | 新建技能包 | `hermes bundles create [-h] [--skill SKILL] [--description DESCRIPTION] [--instruction INSTRUCTION] [--force] name` |
| `delete` | 删除技能包 | `hermes bundles delete [-h] name` |
| `reload` | 重新扫描 bundles 目录并报告变化 | `hermes bundles reload [-h]` |

---

### 1.48 `plugins` — 插件

**用途**：安装、更新、移除、列出或校验原生 Hermes 插件与可移植的 Agent Plugins v1 包。**可移植包安装后处于 disabled 状态**。

**用法**：`hermes plugins [-h] {install,search,update,remove,rm,uninstall,list,ls,enable,disable,capabilities,doctor,pack,show,info} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令（别名） | 用途 | usage |
|---|---|---|
| `install` | 从 Git URL、`owner/repo` 或索引名安装插件 | `hermes plugins install [-h] [--force] [--ref COMMIT_SHA] [--enable \| --no-enable] identifier` |
| `search` | 搜索社区插件索引 | `hermes plugins search [-h] [--json] [--capability CAP] [--refresh] [term]` |
| `update` | 拉取已装插件的最新变更 | `hermes plugins update [-h] name` |
| `remove`（`rm`、`uninstall`） | 移除已装插件 | `hermes plugins remove [-h] name` |
| `list`（`ls`） | 列出已装插件 | `hermes plugins list [-h] [--enabled] [--user] [--no-bundled] [--plain] [--json]` |
| `enable` | 启用被禁用的插件 | `hermes plugins enable [-h] [--allow-tool-override \| --no-allow-tool-override] name` |
| `disable` | 禁用插件但不移除 | `hermes plugins disable [-h] name` |
| `capabilities` | 显示每个插件声明 vs 被授予的能力 | `hermes plugins capabilities [-h] [name]` |
| `doctor` | 用真实 runtime 契约校验插件 | `hermes plugins doctor [-h] [--ci] [target]` |
| `pack` | 声明式、可分享的插件集合（`hermes-pack.yaml`） | `hermes plugins pack [-h] {install,export,show} ...` |
| `show`（`info`） | 显示单个插件详情（含 emits/listens） | `hermes plugins show [-h] name` |

**三级子命令**：

| 三级命令 | usage |
|---|---|
| `plugins pack install` | `hermes plugins pack install [-h] [--force] source` |
| `plugins pack export` | `hermes plugins pack export [-h] [--enabled-only] [--name NAME]` |
| `plugins pack show` | `hermes plugins pack show [-h] source` |

**关键行为**：`install --ref` 必须是 40 位不可变 Git commit SHA；`install` 默认会询问是否启用，`--enable`/`--no-enable` 跳过询问。

---

### 1.49 `curator` — 后台技能维护

**用途**：curator 是一个用辅助模型的背景任务，定期审查 agent 自建技能、清理陈旧项、合并重叠项、归档废弃技能。**内置与 hub 安装的技能永不被触碰**。归档可恢复；**永不自动删除**。

**用法**：`hermes curator [-h] {status,usage,run,pause,resume,pin,unpin,list-unmanaged,adopt,restore,list-archived,archive,prune,backup,rollback,ledger,purge} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `status` | 显示 curator 状态与技能统计 | `hermes curator status [-h]` |
| `usage` | 显示**所有**技能（built-in/hub/agent）的使用遥测与来源 | `hermes curator usage [-h] [--sort {activity,recent,name}] [--provenance {agent,bundled,hub}] [--json]` |
| `run` | 立即触发一次 curator 审查 | `hermes curator run [-h] [--sync] [--background] [--dry-run] [--consolidate]` |
| `pause` | 暂停 curator 直到恢复 | `hermes curator pause [-h]` |
| `resume` | 恢复被暂停的 curator | `hermes curator resume [-h]` |
| `pin` | 固定技能，curator 不再自动迁移它 | `hermes curator pin [-h] skill` |
| `unpin` | 取消固定 | `hermes curator unpin [-h] skill` |
| `list-unmanaged` | 列出可被维护但没有来源标记的技能 | `hermes curator list-unmanaged [-h]` |
| `adopt` | 把未托管技能交给 curator（来源标记即用户声明） | `hermes curator adopt [-h] [--all-unmanaged] [--dry-run] [--yes] [skill ...]` |
| `restore` | 恢复已归档技能 | `hermes curator restore [-h] skill` |
| `list-archived` | 列出已归档技能 | `hermes curator list-archived [-h]` |
| `archive` | 手动归档技能（移到 `.archive/`，从 prompt 中排除） | `hermes curator archive [-h] skill` |
| `prune` | 批量归档 curator 管理且闲置 ≥ N 天的技能（默认 **90**） | `hermes curator prune [-h] [--days DAYS] [-y] [--dry-run]` |
| `backup` | 手动给 `~/.hermes/skills/` 打 tar.gz 快照（curator 每次真实运行前也会自动做） | `hermes curator backup [-h] [--reason REASON]` |
| `rollback` | 从 curator 快照恢复 `~/.hermes/skills/`，或按 ledger 条目 id 回滚单次变更 | `hermes curator rollback [-h] [--list] [--id BACKUP_ID] [-y] [entry_id]` |
| `ledger` | 列出逐变更的技能审计账本（curator/agent/user 全部角色） | `hermes curator ledger [-h] [--skill SKILL] [--limit LIMIT]` |
| `purge` | 删除超过 `curator.archive_ttl_days` 的已归档技能（**仅显式执行，永不自动**；记入 ledger） | `hermes curator purge [-h] [--days DAYS] [--dry-run] [-y]` |

---

### 1.50 `pets` — petdex 动画宠物

**用途**：Petdex（github.com/crafter-station/petdex）是编码 agent 的动画精灵宠物公共画廊。装一个后 Hermes 会在 CLI、TUI 与桌面端显示它对 agent 活动的反应。

**用法**：`hermes pets [-h] {list,install,select,show,off,scale,remove,doctor} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `list` | 浏览 petdex 画廊 | `hermes pets list [-h] [--installed] [--limit LIMIT] [query]` |
| `install` | 从画廊安装宠物 | `hermes pets install [-h] [--force] [--select] slug` |
| `select` | 设置活动宠物（写 `display.pet.*`） | `hermes pets select [-h] [slug]` |
| `show` | 在终端里动起来 | `hermes pets show [-h] [--state STATE] [--cycle] [--once] [--mode MODE] [--scale SCALE] [slug]` |
| `off` | 关闭宠物显示 | `hermes pets off [-h]` |
| `scale` | 全局调整宠物大小（`display.pet.scale`） | `hermes pets scale [-h] factor` |
| `remove` | 删除已安装宠物 | `hermes pets remove [-h] slug` |
| `doctor` | 检查宠物设置 + 终端图形支持 | `hermes pets doctor [-h]` |

---

### 1.51 `journey`（别名 `learning`、`memory-graph`）— 学习与记忆时间线

**用途**：桌面 Star Map / Memory Graph 的终端版本：已学技能与记忆随时间的时间线柱状图（最旧在上、最新在下），外加可播放的星座刷选器。镜像 TUI 的 `/journey` 覆盖层与桌面面板。

**用法**：`hermes journey [-h] [--reveal 0..1] [--play] [--fps FPS] [--width WIDTH] [--height HEIGHT] [--no-color] [--json] {list,delete,edit} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--reveal` | `0..1` | — | 渲染到该时点为止的时间线（0=最旧，1=现在） |
| `--play` | flag | 关 | 随时间动画构建（Ctrl-C 停止） |
| `--fps FPS` | 整数 | `12` | `--play` 的动画帧率 |
| `--width WIDTH` | 整数 | — | 覆盖渲染宽度（列） |
| `--height HEIGHT` | 整数 | — | 覆盖渲染高度（行） |
| `--no-color` | flag | 关 | 关闭颜色输出 |
| `--json` | flag | 关 | 打印原始图数据 JSON 并退出 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `list` | 列出节点 id（供 delete/edit 用） | `hermes journey list [-h] [--no-color]` |
| `delete` | 按节点 id 删除已学技能（归档）或记忆 | `hermes journey delete [-h] [-y] node` |
| `edit` | 按节点 id 在 `$EDITOR` 中编辑已学技能或记忆 | `hermes journey edit [-h] node` |

> 三个名字（`journey`/`learning`/`memory-graph`）的 `--help` 输出**完全一致**，是同一命令的别名。

---

### 1.52 `memory` — 外部记忆 provider

**用途**：设置与管理外部记忆 provider 插件。可用 provider：`honcho`、`openviking`、`mem0`、`hindsight`、`holographic`、`retaindb`、`byterover`。**同时只能启用一个**外部 provider。内置记忆（`MEMORY.md`/`USER.md`）始终启用。

**用法**：`hermes memory [-h] {setup,status,off,reset} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `setup` | 交互式选择与配置 provider | `hermes memory setup [-h] [provider]` |
| `status` | 显示当前记忆 provider 配置 | `hermes memory status [-h]` |
| `off` | 关闭外部 provider（仅内置） | `hermes memory off [-h]` |
| `reset` | 抹除所有内置记忆（`MEMORY.md` 与 `USER.md`） | `hermes memory reset [-h] [--yes] [--target {all,memory,user}]` |

---

### 1.53 `tools` — 工具开关

**用途**：为 CLI、Telegram、Discord 等平台启用/禁用/列出工具。内置 toolset 用裸名（如 `web`、`memory`）；MCP 工具用 `server:tool` 记法（如 `github:create_issue`）。**裸跑 `hermes tools` 进入交互式配置 UI**。

**用法**：`hermes tools [-h] [--summary] {list,disable,enable,post-setup} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--summary` | flag | 关 | 打印各平台已启用工具摘要并退出 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `list` | 显示所有工具及其启用状态 | `hermes tools list [-h] [--platform PLATFORM]`（`--platform` 默认 `cli`） |
| `disable` | 禁用 toolset 或 MCP 工具 | `hermes tools disable [-h] [--platform PLATFORM] NAME [NAME ...]` |
| `enable` | 启用 toolset 或 MCP 工具 | `hermes tools enable [-h] [--platform PLATFORM] NAME [NAME ...]` |
| `post-setup` | 跑某 provider 的 post-setup 安装钩子（npm/pip/binary） | `hermes tools post-setup [-h] KEY` |

---

### 1.54 `computer-use` — Computer Use 后端

**用途**：安装或检查 `computer_use` toolset 用的 `cua-driver` 二进制。支持 macOS / Windows / Linux。`install` 会拉取并运行上游 cua-driver 安装器，等价于首次启用 Computer Use toolset 时 `hermes tools` 跑的 post-setup 钩子，也是安装没触发时的稳定重跑目标。`doctor` 会跑 cua-driver 的 `health_report` MCP 工具并展示其检查矩阵（TCC、bundle 身份、版本、平台支持等）。

**用法**：`hermes computer-use [-h] {install,status,doctor,permissions} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `install` | 安装或修复 cua-driver 二进制（macOS/Windows/Linux） | `hermes computer-use install [-h] [--upgrade]` |
| `status` | 打印 cua-driver 是否已安装及是否在 PATH 上 | `hermes computer-use status [-h]` |
| `doctor` | 跑 cua-driver `health_report` 并展示检查矩阵 | `hermes computer-use doctor [-h] [--include CHECK] [--skip CHECK] [--json]` |
| `permissions` | 检查或授予 macOS 辅助功能 + 屏幕录制权限 | `hermes computer-use permissions [-h] {status,grant} ...` |

**三级子命令**：

| 三级命令 | usage |
|---|---|
| `computer-use permissions status` | `hermes computer-use permissions status [-h] [--json]` |
| `computer-use permissions grant` | `hermes computer-use permissions grant [-h]` |

---

### 1.55 `mcp` — MCP server 管理

**用途**：管理 MCP server 连接，并把 Hermes 当作 MCP server 运行。MCP server 通过 Model Context Protocol 提供额外工具。用 `hermes mcp add` 连接新 server，用 `hermes mcp serve` 把 Hermes 会话通过 MCP 暴露出去。

**用法**：`hermes mcp [-h] [--accept-hooks] {serve,add,remove,rm,list,ls,test,configure,config,login,reauth,picker,catalog,install} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--accept-hooks` | flag | 关 | 免 TTY 自动同意未见过的新 shell hook |

**子子命令**：

| 子子命令（别名） | 用途 | usage |
|---|---|---|
| `serve` | 把 Hermes 当 MCP server 跑（把会话暴露给其它 agent） | `hermes mcp serve [-h] [-v] [--accept-hooks]` |
| `add` | 添加 MCP server（discovery-first 安装） | `hermes mcp add [-h] [--url URL] [--command MCP_COMMAND] [--args ...] [--auth {oauth,header}] [--preset PRESET] [--connect-timeout CONNECT_TIMEOUT] [--env [ENV ...]] name` |
| `remove`（`rm`） | 移除 MCP server | `hermes mcp remove [-h] name` |
| `list`（`ls`） | 列出已配置 MCP server | `hermes mcp list [-h]` |
| `test` | 测试 MCP server 连接 | `hermes mcp test [-h] name` |
| `configure`（`config`） | 切换工具选择 | `hermes mcp configure [-h] name` |
| `login` | 对基于 OAuth 的 MCP server 强制重新认证 | `hermes mcp login [-h] name` |
| `reauth` | 重新认证一个或全部（`--all`）OAuth MCP server | `hermes mcp reauth [-h] [--all] [name]` |
| `picker` | 交互式目录选择器（也是裸 `hermes mcp` 的默认动作） | `hermes mcp picker [-h]` |
| `catalog` | 列出 Nous 认证、可一键安装的 MCP | `hermes mcp catalog [-h]` |
| `install` | 按名字安装目录中的 MCP（如 `hermes mcp install n8n`） | `hermes mcp install [-h] identifier` |

**关键行为**：`add` 的 `--args ...` **必须是最后一个选项**；`--env` 是 stdio server 的环境变量（`KEY=VALUE`）。

---

### 1.56 `sessions` — 会话存储管理

**用途**：查看与管理 SQLite 会话存储。

**用法**：`hermes sessions [-h] {list,export,delete,prune,archive,optimize,clean-markers,optimize-storage,repair,repair-routing,recover,stats,rename,pin,unpin,pinned,retitle-skills,browse,import} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令（19 个）**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `list` | 列出近期会话 | `hermes sessions list [-h] [--source SOURCE] [--limit LIMIT] [--workspace NEEDLE]` |
| `export` | 导出会话为 JSONL / Markdown / QMD | `hermes sessions export [-h] [--format {jsonl,md,qmd,html,trace}] [--upload] [--public] [--no-redact] [--only {user-prompts}] [--session-id SESSION_ID] [--older-than AGE] … [--redact] [--lineage {single,logical}] [--delete-after-verified] [--force] [output]` |
| `delete` | 删除指定会话 | `hermes sessions delete [-h] [--yes] session_id` |
| `prune` | 删除旧会话（可按时间窗、来源、标题等过滤） | `hermes sessions prune [-h] [--older-than AGE] [--newer-than AGE] [--before TIME] [--after TIME] [--source SOURCE] [--title TITLE] [--end-reason END_REASON] [--cwd CWD] … [--dry-run] [--yes] [--include-archived] [--include-pinned] [--never-active]` |
| `archive` | 批量归档（软隐藏）匹配的会话——不删除 | `hermes sessions archive [-h] …（与 prune 同一套过滤 flag） [--dry-run] [--yes]` |
| `optimize` | 回收磁盘：合并 FTS5 段 + VACUUM（不改数据） | `hermes sessions optimize [-h]` |
| `clean-markers` | 永久清除 #78148 之前会话遗留的 tool-call marker 内容 | `hermes sessions clean-markers [-h] [--dry-run] [--no-backup]` |
| `optimize-storage` | 把搜索索引迁移到紧凑 v23 布局（大库省盘） | `hermes sessions optimize-storage [-h] [--no-vacuum] [--yes]` |
| `repair` | 修复畸形 `state.db` schema，让隐藏会话重新出现 | `hermes sessions repair [-h] [--check-only] [--no-backup]` |
| `repair-routing` | 重新打上丢失路由身份的 gateway 会话 | `hermes sessions repair-routing [-h] [--apply] [--max-gap-seconds MAX_GAP_SECONDS]` |
| `recover` | 把规范会话数据重建到一个独立的干净数据库 | `hermes sessions recover [-h] --source SOURCE [--output OUTPUT] [--inspect-only] [--work-dir WORK_DIR] [--chunk-size CHUNK_SIZE] [--allow-partial] [--report REPORT]` |
| `stats` | 显示会话存储统计 | `hermes sessions stats [-h]` |
| `rename` | 设置/修改会话标题 | `hermes sessions rename [-h] session_id title [title ...]` |
| `pin` | 固定会话——持久 keep 标记，免于自动归档 | `hermes sessions pin [-h] session_ids [session_ids ...]` |
| `unpin` | 移除固定标记 | `hermes sessions unpin [-h] session_ids [session_ids ...]` |
| `pinned` | 列出已固定会话 | `hermes sessions pinned [-h] [--json]` |
| `retitle-skills` | 重新给「标题来自 /skill 自身文本」的会话起名 | `hermes sessions retitle-skills [-h] [--apply] [--limit LIMIT]` |
| `browse` | 交互式会话选择器——浏览、搜索、恢复 | `hermes sessions browse [-h] [--source SOURCE] [--limit LIMIT]` |
| `import` | 把 Claude Code / Codex CLI 会话导入 Hermes | `hermes sessions import [-h] [--from {claude,codex}] [path]` |

**关键行为**：`prune`/`archive`/`export` 共享一整套 ~28 个过滤 flag（时间窗、来源、标题、cwd、消息数、模型、provider、user、chat-id、分支、token 数、成本、工具调用数）。`prune` 默认**排除**已归档与已固定会话，需显式 `--include-archived` / `--include-pinned`；`--never-active` 是另一类目标（删除有 key 但从未活动的 gateway 行）。`export --format` 支持 `jsonl/md/qmd/html/trace` 五种，且可 `--upload --public`。

---

### 1.57 `insights` — 用量洞察

**用途**：分析会话历史，显示 token 用量、成本、工具模式与活动趋势。

**用法**：`hermes insights [-h] [--days DAYS] [--source SOURCE]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--days DAYS` | 整数 | `30` | 分析天数 |
| `--source SOURCE` | `cli`/`telegram`/`discord` 等 | — | 按平台过滤 |

**子子命令**：无。

---

### 1.58 `monitoring` — 网关监控

**用途**：网关监控：服务健康指标 + 脱敏诊断，通过 OTLP 导出到运维配置的端点。**构造上不含内容**——无 prompt、消息、工具参数/结果或用量分析。在 config.yaml `monitoring.*` 下配置。

**用法**：`hermes monitoring [-h] {status} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `status` | 显示监控设置、导出状态与脱敏姿态 | `hermes monitoring status [-h]` |

---

### 1.59 `claw` — OpenClaw 迁移

**用途**：把设置、记忆、技能与 API key 从 OpenClaw 迁移到 Hermes。

**用法**：`hermes claw [-h] {migrate,cleanup,clean} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令（别名） | 用途 | usage |
|---|---|---|
| `migrate` | 从 OpenClaw 迁移到 Hermes | `hermes claw migrate [-h] [--source SOURCE] [--dry-run] [--preset {user-data,full}] [--overwrite] [--migrate-secrets] [--no-backup] [--workspace-target WORKSPACE_TARGET] [--skill-conflict {skip,overwrite,rename}] [--yes]` |
| `cleanup`（`clean`） | 迁移后归档遗留的 OpenClaw 目录 | `hermes claw cleanup [-h] [--source SOURCE] [--dry-run] [--yes]` |

---

### 1.60 `update` — 更新

**用途**：从 git 拉取最新变更并重装依赖。

**用法**：`hermes update [-h] [--gateway] [--check] [--plan] [--no-backup] [--backup] [--yes] [--keep-stash] [--branch NAME] [--switch-branch] [--force] [--force-venv]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--gateway` | flag | 关 | 网关模式：用基于文件的 IPC 代替 stdin 做提示（`/update` 内部使用） |
| `--check` | flag | 关 | 只检查是否有更新，不安装任何东西 |
| `--plan` | flag | 关 | 显示更新计划后退出，不改动任何东西：安装类型（git/docker/nix）、跨所有 profile 的每个运行中 Hermes 服务及其 supervisor 与运行代码版本、以及各自将如何重启。**只读，可在活跃 fleet 上安全运行** |
| `--no-backup` | flag | — | 本次运行跳过**所有**更新前备份（quick 状态快照与完整 zip 都不做；覆盖 `updates.pre_update_backup`） |
| `--backup` | flag | — | 本次强制做完整更新前备份（quick 快照 + HERMES_HOME zip），无视 `updates.pre_update_backup` |
| `--yes, -y` | flag | 关 | 不阻塞在提示上：接受配置迁移与 stash 恢复提示、跳过 fork-upstream 提示且不添加 remote。API key 输入被跳过；请单独跑 `hermes config migrate` |
| `--keep-stash` | flag | 关 | 更新后**不**重新应用本地改动。未提交改动仍会被 stash 以便更新进行，但会留在 git stash 里，而不是恢复到新代码上。桌面端 updater 用它避免本地源码编辑悄悄跟随更新 |
| `--branch NAME` | 分支名 | `main` | 针对该分支更新。若本地 checkout 在别的分支，hermes 会先切到目标分支（自动 stash 未提交改动） |
| `--switch-branch` | flag | 关 | 在 `updates.parked_branch_strategy: update_in_place` 配置下，本次覆盖它：切到更新目标并在**那里**更新，而不是把目标合并进当前分支。分支历史保持原样——不写入 merge commit。仍拒绝碰脏树 |
| `--force` | flag | 关 | Windows：即使检测到另一个 `hermes.exe` 也继续更新（并发进程很可能导致 WinError 32 警告）。**不**绕过 venv 进程守卫 |
| `--force-venv` | flag | 关 | Windows：即使有其它进程正从该解释器运行（桌面后端、网关、终端）也变更 venv。那些进程会锁住原生 `.pyd`，依赖同步很可能中途失败并把安装留在半更新状态。仅在确认检测到的持有者是误报时使用 |

**子子命令**：无。**关键行为**：`--plan` 是唯一对生产 fleet 安全的只读模式；`--no-backup` 与 `--backup` 是互斥语义的成对强制开关。

---

### 1.61 `uninstall` — 卸载

**用途**：从系统移除 Hermes Agent。可保留配置/数据以便重装。

**用法**：`hermes uninstall [-h] [--full] [--gui] [--gui-summary] [--yes] [--dry-run]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--full` | flag | 关 | 完整卸载——连配置与数据一起删 |
| `--gui` | flag | 关 | 只卸载桌面 Chat GUI，保留 agent |
| `--gui-summary` | flag | 关 | 打印已安装 GUI/agent 产物的 JSON 摘要后退出（桌面 app 用它决定卸载选项） |
| `--yes, -y` | flag | 关 | 跳过确认提示 |
| `--dry-run` | flag | 关 | 打印将要删除什么而不做任何改动 |

**子子命令**：无。

---

### 1.62 `acp` — ACP server 模式

**用途**：以 ACP 模式启动 Hermes Agent，用于编辑器集成（VS Code、Zed、JetBrains）。

**用法**：`hermes acp [-h] [--accept-hooks] [--version] [--check] [--setup] [--setup-browser] [--yes]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--accept-hooks` | flag | 关 | 免 TTY 自动同意未见过的新 shell hook |
| `--version` | flag | 关 | 打印 Hermes ACP 版本并退出 |
| `--check` | flag | 关 | 校验 ACP 依赖与 adapter 导入后退出 |
| `--setup` | flag | 关 | 为 ACP 终端认证跑交互式 Hermes provider/模型设置 |
| `--setup-browser` | flag | 关 | 把 agent-browser + Playwright Chromium 装进 `~/.hermes/node/`（幂等） |
| `--yes, -y` | flag | 关 | 接受所有提示（供 `--setup-browser` 跳过约 400 MB Chromium 下载确认） |

**子子命令**：无。

---

### 1.63 `profile` — profile 管理

**用途**：管理 profile——多个隔离的 Hermes 实例。

**用法**：`hermes profile [-h] {list,use,create,delete,describe,show,alias,rename,export,import,install,update,info} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `list` | 列出所有 profile | `hermes profile list [-h]` |
| `use` | 设置粘性默认 profile | `hermes profile use [-h] profile_name` |
| `create` | 新建 profile | `hermes profile create [-h] [--clone] [--clone-all] [--clone-from SOURCE] [--no-alias] [--no-skills] [--description DESCRIPTION] profile_name` |
| `delete` | 删除 profile | `hermes profile delete [-h] [-y] profile_name` |
| `describe` | 读取或设置 profile 描述（kanban orchestrator 用） | `hermes profile describe [-h] [--text TEXT] [--auto] [--overwrite] [--all] [profile_name]` |
| `show` | 显示 profile 详情 | `hermes profile show [-h] profile_name` |
| `alias` | 管理 wrapper 脚本 | `hermes profile alias [-h] [--remove] [--name NAME] profile_name` |
| `rename` | 重命名 profile（`default`：只设显示名，id 不变） | `hermes profile rename [-h] old_name new_name` |
| `export` | 把 profile 导出为归档 | `hermes profile export [-h] [-o OUTPUT] profile_name` |
| `import` | 从归档导入 profile | `hermes profile import [-h] [--name NAME] archive` |
| `install` | 从 git URL 或本地目录安装 profile 发行版 | `hermes profile install [-h] [--name NAME] [--alias] [--force] [-y] source` |
| `update` | 重新拉取发行版并应用更新（保留用户数据） | `hermes profile update [-h] [--force-config] [-y] profile_name` |
| `info` | 显示 profile 的发行清单（版本、依赖、来源） | `hermes profile info [-h] profile_name` |

**关键行为**：`create --clone` 复制 config.yaml/.env/SOUL.md/技能；`--clone-all` 全量复制（除 per-profile 部分）；`--no-skills` 建一个不含内置技能的空 profile。

---

### 1.64 `completion` — shell 补全

**用途**：打印 shell 补全脚本。

**用法**：`hermes completion [-h] [{bash,zsh,fish}]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| （位置参数） | `bash` / `zsh` / `fish` | `bash` | shell 类型 |

**子子命令**：无。**关键行为**：这是典型的「补全脚本」命令——输出到 stdout，用户 `source` 之。Orbit 若提供补全，应保持 `completion [shell]` 这一形状。

---

### 1.65 `__run-script` — 内部命令

**用途**：`--help` 原文仅有一行 usage，无描述文本；在顶层列表中被 `==SUPPRESS==` 隐藏。功能面：执行一个脚本文件。

**用法**：`hermes __run-script [-h] script`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| （位置参数）`script` | 脚本路径 | — | 要运行的脚本（无 help 文本） |

**子子命令**：无。**内部命令：是**（SUPPRESS）。

---

### 1.66 `__slash-worker` — 内部命令

**用途**：`Internal: run the TUI slash-worker process (hidden).` —— TUI 的 slash 命令工作进程。

**用法**：`hermes __slash-worker [-h] --session-key SESSION_KEY [--model MODEL]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--session-key SESSION_KEY` | 文本（**必填**） | — | 会话 key（无 help 文本） |
| `--model MODEL` | 模型名 | — | 模型（无 help 文本） |

**子子命令**：无。**内部命令：是**（SUPPRESS）。

---

### 1.67 `__compute-host` — 内部命令

**用途**：`Internal: run the dashboard compute-host process (hidden).` —— dashboard 的计算宿主进程。

**用法**：`hermes __compute-host [-h]`　|　**options**：仅 `-h, --help`。**子子命令**：无。**内部命令：是**（SUPPRESS）。

---

### 1.68 `__gateway-restart-watch` — 内部命令

**用途**：`Internal: poll a PID then respawn the gateway (hidden).` —— 轮询某 PID 消失后重启网关。

**用法**：`hermes __gateway-restart-watch [-h] [--deadline DEADLINE] pid ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| （位置参数）`pid` | 进程 ID | — | 要轮询的 PID |
| （位置参数）`cmd` | 剩余参数 | — | 重启命令（`...` 表示可接收多个） |
| `--deadline DEADLINE` | — | — | 截止时间（无 help 文本） |

**子子命令**：无。**内部命令：是**（SUPPRESS）。

---

### 1.69 `__update-gateway-helper` — 内部命令

**用途**：`Internal: run hermes update --gateway detached (hidden).` —— 分离式执行 `hermes update --gateway`。

**用法**：`hermes __update-gateway-helper [-h] output_path exit_code_path ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| （位置参数）`output_path` | 路径 | — | 输出文件路径 |
| （位置参数）`exit_code_path` | 路径 | — | exit code 文件路径 |
| （位置参数）`cmd` | 剩余参数 | — | 更新命令（`...`） |

**子子命令**：无。**内部命令：是**（SUPPRESS）。

---

### 1.70 `dashboard` — Web UI dashboard

**用途**：启动 Hermes Agent Web dashboard，用于管理配置、API key 与会话。

**用法**：`hermes dashboard [-h] [--port PORT] [--host HOST] [--insecure] [--skip-build] [--isolated] [--stop] [--status] [--no-open] {register} ...`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--port PORT` | 整数 | `9119`（`0`=由 OS 自动分配） | 端口 |
| `--host HOST` | IP | `127.0.0.1` | 绑定主机 |
| `--insecure` | flag | 关 | **已废弃 / 无操作**。原用于在非 loopback 绑定上绕过认证。自 2026-06 加固后不再关闭认证——公网绑定始终需要认证 provider（密码或 OAuth）。保持 127.0.0.1 + 隧道以留在本地 |
| `--skip-build` | flag | 关 | 跳过 web UI 构建，直接服务已有 dist。适用于非交互环境（Windows 计划任务、CI）没有 npm 时。预构建：`cd web && npm run build` |
| `--isolated` | flag | 关 | 从命名 profile 启动时，跑一个仅限该 profile 的专用 server，而不是路由到机器级 server。默认行为是统一的：profile 启动会附加到（或启动）**一个**机器级 server 并预选该 profile |
| `--stop` | flag | 关 | 停掉所有运行中的 Hermes web server 进程并退出 |
| `--status` | flag | 关 | 列出运行中的 Hermes web server 进程并退出 |
| `--no-open` | flag | 关 | 不自动打开浏览器 |

**子子命令**：

| 子子命令 | 用途 | usage |
|---|---|---|
| `register` | 把自托管 dashboard 注册到 Nous Portal（把 OAuth client ID 写入 `.env`） | `hermes dashboard register [-h] [--name NAME] [--redirect-uri REDIRECT_URI] [--portal-url PORTAL_URL]` |

---

### 1.71 `serve` — 后端 server

**用途**：运行 Hermes 后端 server —— 桌面 app 与远程客户端连接的 JSON-RPC/WebSocket 网关。**Headless：从不打开浏览器 UI**。

**用法**：`hermes serve [-h] [--port PORT] [--host HOST] [--insecure] [--skip-build] [--isolated] [--stop] [--status] [--ssh-session-token-file PATH] [--ssh-owner-nonce NONCE]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--port PORT` | 整数 | `9119`（`0`=自动） | 端口 |
| `--host HOST` | IP | `127.0.0.1` | 绑定主机 |
| `--insecure` | flag | 关 | **已废弃 / 无操作**（同 dashboard 说明） |
| `--skip-build` | flag | 关 | 跳过 web UI 构建，直接服务已有 dist |
| `--isolated` | flag | 关 | 同 dashboard：profile 专用 server |
| `--stop` | flag | 关 | 停掉所有运行中的 Hermes web server 进程并退出 |
| `--status` | flag | 关 | 列出运行中的 Hermes web server 进程并退出 |
| `--ssh-session-token-file PATH` | 路径 | — | 从 PATH 读取一次性 Desktop SSH 会话 token |
| `--ssh-owner-nonce NONCE` | 文本 | — | 标识一个 Desktop 拥有的 SSH 后端进程 |

**子子命令**：无。

---

### 1.72 `desktop`（别名 `gui`）— 桌面应用

**用途**：启动 Hermes Electron 桌面 app。默认流程：安装 workspace Node 依赖 → 构建当前 OS 的 unpacked Electron app → 启动该打包产物。

**用法**：`hermes desktop [-h] [--source] [--build-only] [--fake-boot] [--ignore-existing] [--hermes-root HERMES_ROOT] [--cwd CWD] [--skip-build] [--force-build] [--setup-tcc-identity] [--identity IDENTITY]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--source` | flag | 关 | 用 `electron .` 对 `apps/desktop/dist` 启动，而不是打包产物 |
| `--build-only` | flag | 关 | 只构建不启动（安装器 `--update` 流程用） |
| `--fake-boot` | flag | 关 | 启用确定性的桌面启动延迟，用于验证启动 UI |
| `--ignore-existing` | flag | 关 | 后端解析时强制忽略 PATH 上已有的 hermes CLI |
| `--hermes-root HERMES_ROOT` | 路径 | — | 覆盖 Desktop 使用的 Hermes 源码根（设 `HERMES_DESKTOP_HERMES_ROOT`） |
| `--cwd CWD` | 路径 | — | Desktop 聊天会话的初始项目目录（设 `HERMES_DESKTOP_CWD`） |
| `--skip-build` | flag | 关 | 跳过 npm install/package，直接启动 `apps/desktop/release` 里已有的 unpacked app |
| `--force-build` | flag | 关 | 即使 content stamp 匹配也强制全量重建 |
| `--setup-tcc-identity` | flag | 关 | **仅 macOS**：在 login keychain 创建/导入自签名代码签名证书，把 `desktop.macos_signing_identity` 指向它，再重签打包 app。让 macOS TCC 授权（完全磁盘访问、辅助功能、文件与文件夹、麦克风）在重建后仍存活。幂等，更新后可安全重跑 |
| `--identity IDENTITY` | 文本 | `Hermes Local Signing` | `--setup-tcc-identity` 要创建/使用的证书名 |

**子子命令**：无。`gui` 是 `desktop` 的别名，`--help` 输出完全一致。

---

### 1.73 `logs` — 日志

**用途**：查看、tail、过滤 `agent.log` / `errors.log` / `gateway.log` / `gui.log` / `desktop.log`。

**用法**：`hermes logs [-h] [-n LINES] [-f] [--level LEVEL] [--session ID] [--since TIME] [--component NAME] [log_name]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| （位置参数）`log_name` | `agent` / `errors` / `gateway` / `gui` / `desktop` / `list` | `agent` | 要看哪个日志；`list` 显示可用文件 |
| `-n, --lines LINES` | 整数 | `50` | 显示行数 |
| `-f, --follow` | flag | 关 | 实时跟随（类似 `tail -f`） |
| `--level LEVEL` | `DEBUG`/`INFO`/`WARNING`/`ERROR` | — | 最低日志级别 |
| `--session ID` | 文本 | — | 过滤含该 session ID 子串的行 |
| `--since TIME` | `1h`/`30m`/`2d` 等 | — | 显示该时间以来的行 |
| `--component NAME` | `gateway`/`agent`/`tools`/`cli`/`cron`/`gui` | — | 按组件过滤 |

**子子命令**：无（`log_name` 是位置参数取值，不是子命令）。

---

### 1.74 `prompt-size` — prompt 预算分解

**用途**：报告一个新会话的固定 prompt 预算：system prompt 总量、技能索引、记忆、用户画像、工具 schema JSON。**离线运行（不发 API 调用）**。

**用法**：`hermes prompt-size [-h] [--platform PLATFORM] [--json]`

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助 |
| `--platform PLATFORM` | `cli`/`telegram`/`discord`/… | `cli` | 要模拟的平台 |
| `--json` | flag | 关 | 以 JSON 输出分解结果 |

**子子命令**：无。

---

## 2. 全局 flags（顶层 `hermes --help` 的 options）

共 **23** 个，对裸 `hermes`（以及 `chat` 等继承它们的子命令）生效。

| 选项 | 取值 | 默认 | 说明 |
|---|---|---|---|
| `-h, --help` | — | — | 显示帮助并退出 |
| `--version, -V` | — | — | 显示版本并退出（实测输出 `Hermes Agent v0.21.0 (2026.8.31)`） |
| `-z, --oneshot PROMPT` | 文本 | — | 一次性模式：发单条 prompt，**只**把最终回复文本打到 stdout。无 banner、无 spinner、无 tool 预览、无 session_id 行。工具、记忆、rules、CWD 的 AGENTS.md 照常加载；审批自动绕过。面向脚本/管道 |
| `--usage-file PATH` | 路径 | — | **仅 one-shot 模式**：运行后把 JSON 用量报告（估算成本、token 数、模型、`api_calls`）写到 PATH。运行失败也会写，便于管道永远能对账。非 `-z/--oneshot` 下无效果 |
| `-m, --model MODEL` | 模型名 | 配置默认 | 本次调用的模型覆盖（如 `anthropic/claude-sonnet-4.6`）。作用于 `-z/--oneshot` 与 `--tui`。也可用 `HERMES_INFERENCE_MODEL` 环境变量 |
| `--provider PROVIDER` | provider 名 | 配置默认 | 本次调用的 provider 覆盖（如 `openrouter`、`anthropic`）。作用于 `-z/--oneshot` 与 `--tui`。持久 provider 在 config.yaml 的 `model.provider` |
| `--reasoning LEVEL` | `none`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max`/`ultra` | `agent.reasoning_effort` | 本次调用的推理强度，仅本次生效；持久值在 config.yaml（或 `agent.reasoning_overrides` 按模型） |
| `-t, --toolsets TOOLSETS` | 逗号分隔 | 配置默认 | 本次调用启用的 toolset。作用于 `-z/--oneshot` 与 `--tui` |
| `--resume, -r SESSION` | 会话 ID / 标题 / `latest` | — | 按 ID 或标题恢复历史会话；`latest` 取最近（工作区维度，等同不带名的 `-c`） |
| `--no-restore-cwd` | flag | 关 | 不 cd 进被恢复会话记录的 cwd |
| `--in DIR` | 目录 | — | 启动/恢复前 cd 到 DIR。配合 `--resume latest`/`-c` 时，取 DIR 工作区最近的会话，并让会话留在 DIR（跳过记录的 cwd 恢复） |
| `--continue, -c [SESSION_NAME]` | 名称，可省略 | 最近会话 | 按名字恢复会话；不带名字则取最近 |
| `--worktree, -w` | flag | 关 | 在隔离 git worktree 中运行（并行 agent 用） |
| `--accept-hooks` | flag | 关 | 免 TTY 提示自动同意 config.yaml 中未见过的 shell hook。等价 `HERMES_ACCEPT_HOOKS=1` 或 `hooks_auto_accept: true`。用于无法交互的 CI/headless 运行 |
| `--skills, -s SKILLS` | 技能名（可重复或逗号分隔） | — | 为会话预加载技能 |
| `--yolo` | flag | 关 | 绕过所有危险命令审批提示（风险自负） |
| `--pass-session-id` | flag | 关 | 把会话 ID 放进 agent 的 system prompt |
| `--ignore-user-config` | flag | 关 | 忽略 `~/.hermes/config.yaml`，回落到内置默认（`.env` 里的凭据仍加载） |
| `--ignore-rules` | flag | 关 | 跳过 AGENTS.md、SOUL.md、`.cursorrules`、记忆与预加载技能的自动注入 |
| `--safe-mode` | flag | 关 | 排障模式：关闭**所有**定制——用户配置、AGENTS.md/记忆注入、插件、MCP server（隐含 `--ignore-user-config` 与 `--ignore-rules`） |
| `--tui` | flag | 关 | 启动现代 TUI 而非经典 REPL |
| `--cli` | flag | 关 | 强制经典 prompt_toolkit REPL（覆盖 `display.interface=tui`） |
| `--dev` | flag | 关 | 配合 `--tui`：用 tsx 直接跑 TypeScript 源码（跳过 dist 构建） |

**继承关系**：`chat` 子命令重复声明了其中绝大部分（`-m/-t/--provider/--reasoning/-s/-v/-Q/--resume/--in/-c/--worktree/--accept-hooks/--yolo/--pass-session-id/--ignore-user-config/--ignore-rules/--safe-mode/--tui/--cli/--dev`），并额外增加 `-q/--query-file/--oneshot/--image/--checkpoints/--max-turns/--run-budget/--source/--create-if-missing`。`gateway`/`cron`/`mcp`/`acp` 各自声明了 `--accept-hooks`。

---

## 3. Orbit 要对齐的命令清单

**归类定义**
- **核心**：agent 主循环、会话、配置、模型、工具/技能/MCP、上下文与自动化（缺了它 Hermes 就不像 Hermes）
- **运维**：安装、诊断、日志、备份、更新、停止、审计
- **平台**：Web UI / 桌面 / 后端 server / profile / 外观 / 补全
- **集成**：外部平台与外部服务对接
- **内部**：`__` 前缀的 SUPPRESS 命令

**优先级定义**
- **P0 必须**：没有该功能，Orbit 不能称为「对齐 Hermes」
- **P1 应做**：对齐体验完整性明显依赖它
- **P2 可选**：锦上添花 / 生态绑定 / 内部实现细节

| ✔ | 命令 | 用途 | 归类 | 优先级 |
|---|---|---|---|---|
| [ ] | `chat` | 交互式会话 / 单次问答，是 CLI 入口本体 | 核心 | **P0 必须** |
| [ ] | `model` | 选择 provider 与默认模型 | 核心 | **P0 必须** |
| [ ] | `moa` | Mixture-of-Agents 多模型槽位 | 核心 | P2 可选 |
| [ ] | `fallback` | provider 失败回退链 | 核心 | P1 应做 |
| [ ] | `worktree` | git worktree 审计与回收 | 核心 | P2 可选 |
| [ ] | `browser` | 关闭占用真实 profile 的浏览器 | 平台 | P2 可选 |
| [ ] | `secrets` | Bitwarden / 1Password 外部密钥源 | 集成 | P2 可选 |
| [ ] | `egress` | iron-proxy 出网凭据注入防火墙 | 平台 | P2 可选 |
| [ ] | `migrate` | 退役模型 / 废弃配置迁移 | 运维 | P2 可选 |
| [ ] | `gateway` | 消息网关（Telegram/Discord/…）生命周期 | 集成 | **P0 必须** |
| [ ] | `proxy` | 本地 OpenAI 兼容代理 | 集成 | P2 可选 |
| [ ] | `lsp` | write 后语义诊断的 LSP 层 | 核心 | P1 应做 |
| [ ] | `setup` | 交互式配置向导 | 核心 | **P0 必须** |
| [ ] | `whatsapp` | WhatsApp 个人号（Baileys 桥）配置 | 集成 | P2 可选 |
| [ ] | `whatsapp-cloud` | WhatsApp Business Cloud API 配置 | 集成 | P2 可选 |
| [ ] | `slack` | Slack app manifest 生成 | 集成 | P2 可选 |
| [ ] | `send` | 向已配置平台发一条消息（无 LLM） | 集成 | P1 应做 |
| [ ] | `login` | **已废弃**，保留兼容 | 集成 | P2 可选 |
| [ ] | `logout` | 清除 provider 认证 | 集成 | P1 应做 |
| [ ] | `auth` | provider 凭据池管理 | 集成 | **P0 必须** |
| [ ] | `status` | 全组件状态一览 | 运维 | **P0 必须** |
| [ ] | `pause` | 全局紧急停止（只停新工作） | 运维 | P1 应做 |
| [ ] | `resume` | 解除紧急停止 | 运维 | P1 应做 |
| [ ] | `cron` | 定时任务（含 notepad/incidents/durable runs） | 核心 | **P0 必须** |
| [ ] | `sync` | 技能跨设备/组织同步 | 集成 | P2 可选 |
| [ ] | `webhook` | 事件驱动激活的 webhook 订阅 | 集成 | P1 应做 |
| [ ] | `peer` | 跨机器 bot-to-bot DM | 集成 | P2 可选 |
| [ ] | `portal` | Nous Portal 一键 onboarding | 集成 | P2 可选 |
| [ ] | `kanban` | 多 profile 任务板（46 个子命令） | 核心 | P1 应做 |
| [ ] | `project` | 多文件夹命名工作区 | 核心 | P1 应做 |
| [ ] | `hooks` | shell 脚本 hook 检视/测试/白名单 | 核心 | P1 应做 |
| [ ] | `doctor` | 配置与依赖诊断（含 `--fix`/`--live`） | 运维 | **P0 必须** |
| [ ] | `verify` | 项目 run recipe 探测 + 冒烟验证 | 核心 | P1 应做 |
| [ ] | `security` | OSV.dev 供应链审计 | 运维 | P2 可选 |
| [ ] | `approvals` | 从审批历史挖掘 allowlist | 核心 | P1 应做 |
| [ ] | `dump` | 可粘贴的安装摘要 | 运维 | P2 可选 |
| [ ] | `debug` | 调试报告上传 | 运维 | P2 可选 |
| [ ] | `backup` | home 备份为 zip | 运维 | P1 应做 |
| [ ] | `checkpoints` | 文件 checkpoint 存储管理 | 核心 | P1 应做 |
| [ ] | `import` | 从 zip 恢复备份 | 运维 | P1 应做 |
| [ ] | `import-agent` | 从 Claude Code / Codex CLI 导入 | 集成 | P2 可选 |
| [ ] | `config` | 配置 show/get/set/unset/check/migrate | 核心 | **P0 必须** |
| [ ] | `skin` | 皮肤切换与调色 | 平台 | P2 可选 |
| [ ] | `console` | 受限命令 REPL | 平台 | P2 可选 |
| [ ] | `pairing` | DM 配对码授权 | 集成 | P2 可选 |
| [ ] | `skills` | 技能搜索/安装/审计/发布（22 个子命令） | 核心 | **P0 必须** |
| [ ] | `bundles` | 技能包（多技能单 slash） | 核心 | P1 应做 |
| [ ] | `plugins` | 插件安装/启停/能力校验 | 集成 | P1 应做 |
| [ ] | `curator` | 后台技能维护与归档 | 核心 | P2 可选 |
| [ ] | `pets` | petdex 动画宠物 | 平台 | P2 可选 |
| [ ] | `journey`（`learning`/`memory-graph`） | 学习与记忆时间线 | 核心 | P2 可选 |
| [ ] | `memory` | 外部记忆 provider | 核心 | P1 应做 |
| [ ] | `tools` | 按平台启用/禁用工具 | 核心 | **P0 必须** |
| [ ] | `computer-use` | cua-driver 桌面操作后端 | 核心 | P2 可选 |
| [ ] | `mcp` | MCP server 管理与 MCP server 模式 | 核心 | **P0 必须** |
| [ ] | `sessions` | 会话存储管理（19 个子命令） | 核心 | **P0 必须** |
| [ ] | `insights` | token/成本/工具模式分析 | 核心 | P1 应做 |
| [ ] | `monitoring` | 网关 OTLP 监控 | 运维 | P2 可选 |
| [ ] | `claw` | OpenClaw 迁移 | 集成 | P2 可选 |
| [ ] | `update` | 自更新（含 `--plan` 只读计划） | 运维 | P2 可选 |
| [ ] | `uninstall` | 卸载 | 运维 | P2 可选 |
| [ ] | `acp` | ACP server（编辑器集成） | 平台 | P2 可选 |
| [ ] | `profile` | 多隔离实例管理 | 平台 | P1 应做 |
| [ ] | `completion` | shell 补全脚本 | 平台 | P2 可选 |
| [ ] | `__run-script` | 内部：执行脚本 | 内部 | P2 可选 |
| [ ] | `__slash-worker` | 内部：TUI slash-worker 进程 | 内部 | P2 可选 |
| [ ] | `__compute-host` | 内部：dashboard compute-host | 内部 | P2 可选 |
| [ ] | `__gateway-restart-watch` | 内部：PID 轮询后重启网关 | 内部 | P2 可选 |
| [ ] | `__update-gateway-helper` | 内部：分离式 `update --gateway` | 内部 | P2 可选 |
| [ ] | `dashboard` | Web UI dashboard | 平台 | P1 应做 |
| [ ] | `serve` | 后端 JSON-RPC/WebSocket server | 平台 | P1 应做 |
| [ ] | `desktop`（`gui`） | Electron 桌面 app | 平台 | P2 可选 |
| [ ] | `logs` | 日志查看/tail/过滤 | 运维 | **P0 必须** |
| [ ] | `prompt-size` | prompt 与 tool schema 字节预算 | 核心 | P1 应做 |

### 3.1 统计

| 归类 | 数量 | 其中 P0 |
|---|---|---|
| 核心 | 26 | 9（`chat` `model` `setup` `config` `skills` `tools` `mcp` `sessions` `cron`） |
| 运维 | 14 | 3（`status` `doctor` `logs`） |
| 平台 | 11 | 0 |
| 集成 | 18 | 2（`gateway` `auth`） |
| 内部 | 5 | 0 |
| **合计** | **74** | **14** |

**P0 共 14 个**：`chat`、`model`、`setup`、`config`、`skills`、`tools`、`mcp`、`sessions`、`cron`（核心 9 个）、`gateway`、`auth`（集成 2 个）、`status`、`doctor`、`logs`（运维 3 个）。

### 3.2 若只能做 10 个（P0 收敛建议）

`chat` → `sessions` → `config` → `tools` → `skills` → `mcp` → `model` → `cron` → `gateway` → `auth`

理由：前 7 个构成「能对话 + 能记住 + 能配置 + 能扩展能力」的最小闭环；`cron` 与 `gateway` 是把 agent 从「交互工具」变成「常驻服务」的分界线；`auth` 是任何真实模型接入的前置。`status`/`doctor`/`logs` 虽为 P0，但可在上述 10 个之后再补。

---

## 4. 证据：关键命令输出原文摘录

> 全部摘自 `& $exe <cmd> --help 2>&1` 的实际输出（原样，未改写）。完整原文见 `_raw/`。

### 4.1 `--version`
```
Hermes Agent v0.21.0 (2026.8.31)
Install directory: E:\Hermes Agent CN Desktop\data\versions\0.21.0-cn.18\_internal
Install method: unknown
Python: 3.14.7
OpenAI SDK: Not installed
```

### 4.2 顶层 `--help`（usage 行，证明 77 个名字）
```
usage: hermes [-h] [--version] [-z PROMPT] [--usage-file PATH] [-m MODEL]
              [--provider PROVIDER] [--reasoning LEVEL] [-t TOOLSETS]
              [--resume SESSION] [--no-restore-cwd] [--in DIR]
              [--continue [SESSION_NAME]] [--worktree] [--accept-hooks]
              [--skills SKILLS] [--yolo] [--pass-session-id]
              [--ignore-user-config] [--ignore-rules] [--safe-mode] [--tui]
              [--cli] [--dev]
              {chat,model,moa,...,__update-gateway-helper,dashboard,serve,desktop,gui,logs,prompt-size} ...

Hermes Agent - AI assistant with tool-calling capabilities
```

### 4.3 `chat --help`
```
usage: hermes chat [-h] [-q QUERY | --query-file PATH] [--oneshot]
                   [--image IMAGE] [-m MODEL] [-t TOOLSETS]
                   [--reasoning LEVEL] [-s SKILLS] [--provider PROVIDER] [-v]
                   [-Q] [--resume SESSION_ID] [--no-restore-cwd] [--in DIR]
                   [--continue [SESSION_NAME]] [--create-if-missing]
                   [--worktree] [--accept-hooks] [--checkpoints]
                   [--max-turns N] [--run-budget SECONDS] [--yolo]
  --query-file PATH     Read the single query from a file instead of the
                        command line ('-' reads stdin). Safe for arbitrary
                        text: nothing is shell-interpreted, so quotes, $(...),
                        and backticks are preserved verbatim. Mutually
                        exclusive with -q.
  --max-turns N         Maximum tool-calling iterations per conversation turn
                        (default: 500, or agent.max_turns in config)
```

### 4.4 `kanban --help`（46 个子命令）
```
usage: hermes kanban [-h] [--board <slug>]
                     {init,boards,create,swarm,list,ls,show,assign,set-model,reclaim,reassign,diagnostics,diag,link,unlink,claim,comment,attach,attachments,attach-rm,complete,edit,block,schedule,unblock,request-review,request-changes,reopen-review,promote,archive,tail,dispatch,daemon,watch,stats,notify-subscribe,notify-list,notify-unsubscribe,log,runs,heartbeat,assignees,context,specify,decompose,gc,repair} ...

Durable SQLite-backed task board shared across Hermes profiles. Tasks are
claimed atomically, can depend on other tasks, and are executed by a named
profile in an isolated workspace.
  --board <slug>        Board slug to operate on. Defaults to the current
                        board (set via `hermes kanban boards switch <slug>` or
                        the HERMES_KANBAN_BOARD env var).
```

### 4.5 `kanban create --help`
```
usage: hermes kanban create [-h] [--body BODY] [--assignee ASSIGNEE]
                            [--parent PARENT] [--workspace WORKSPACE]
                            [--branch BRANCH] [--project PROJECT]
                            [--tenant TENANT] [--priority PRIORITY]
                            [--triage] [--idempotency-key IDEMPOTENCY_KEY]
                            ...
  --goal                Run the worker in a goal loop: after each turn a judge
  --goal-max-turns N    Turn budget for --goal workers (default 20). Ignored
  --initial-status {blocked,running}
```

### 4.6 `sessions --help`
```
usage: hermes sessions [-h]
                       {list,export,delete,prune,archive,optimize,clean-markers,optimize-storage,repair,repair-routing,recover,stats,rename,pin,unpin,pinned,retitle-skills,browse,import} ...

View and manage the SQLite session store
    optimize            Reclaim disk space: merge FTS5 segments + VACUUM (no
                        data change)
    pin                 Pin session(s) — durable keep flag, exempt from auto-
                        archive
```

### 4.7 `sessions prune --help`
```
usage: hermes sessions prune [-h] [--older-than AGE] [--newer-than AGE]
                             [--before TIME] [--after TIME] [--source SOURCE]
                             [--title TITLE] [--end-reason END_REASON]
                             [--cwd CWD] [--min-messages MIN_MESSAGES]
                             ...
  --include-archived    Also delete archived sessions (excluded by default)
  --include-pinned      Also delete pinned sessions (excluded by default — pin
  --never-active        Instead of ended sessions, delete keyed gateway rows
```

### 4.8 `skills --help`（22 个子命令）
```
usage: hermes skills [-h]
                     {trust,untrust,browse,search,install,inspect,list,check,update,audit,uninstall,reset,list-modified,diff,opt-out,opt-in,repair-official,publish,snapshot,tap,config} ...

Search, install, inspect, audit, configure, and manage skills from skills.sh,
well-known agent skill endpoints, GitHub, ClawHub, and other registries.
```

### 4.9 `skills search --help`（15 个注册源取值）
```
usage: hermes skills search [-h]
                            [--source {all,official,skills-sh,well-known,github,clawhub,lobehub,browse-sh,nvidia,openai,anthropic,huggingface,voltagent,gstack,minimax}]
                            [--limit LIMIT] [--json] query
  --json                Output JSON instead of a table (full identifiers,
```

### 4.10 `tools --help`
```
usage: hermes tools [-h] [--summary] {list,disable,enable,post-setup} ...

Enable, disable, or list tools for CLI, Telegram, Discord, etc. Built-in
toolsets use plain names (e.g. web, memory). MCP tools use server:tool
notation (e.g. github:create_issue). Run 'hermes tools' with no subcommand for
the interactive configuration UI.
  --summary             Print a summary of enabled tools per platform and exit
```

### 4.11 `mcp --help`
```
usage: hermes mcp [-h] [--accept-hooks]
                  {serve,add,remove,rm,list,ls,test,configure,config,login,reauth,picker,catalog,install} ...

Manage MCP server connections and run Hermes as an MCP server. MCP servers
provide additional tools via the Model Context Protocol. Use 'hermes mcp add'
to connect to a new server, or 'hermes mcp serve' to expose Hermes
conversations over MCP.
```

### 4.12 `mcp add --help`
```
usage: hermes mcp add [-h] [--url URL] [--command MCP_COMMAND] [--args ...]
                      [--auth {oauth,header}] [--preset PRESET]
                      [--connect-timeout CONNECT_TIMEOUT] [--env [ENV ...]]
                      name
  --args ...            Arguments for stdio command; must be the last option
  --env [ENV ...]       Environment variables for stdio servers (KEY=VALUE)
```

### 4.13 `config --help`
```
usage: hermes config [-h]
                     {show,edit,get,set,unset,path,env-path,check,migrate} ...

Manage Hermes Agent configuration
    get                 Print a resolved configuration value
    set                 Set a configuration value
    unset               Remove a configuration value
    migrate             Update config with new options
```

### 4.14 `cron --help`
```
usage: hermes cron [-h] [--accept-hooks]
                   {list,create,add,edit,pause,resume,run,remove,rm,delete,status,runs,history,incidents,notepad,doctor,tick} ...

Manage scheduled tasks
    notepad             Read/write a job's durable notepad (persistent KV
                        across runs)
    tick                Run due jobs once and exit
```

### 4.15 `cron create --help`
```
usage: hermes cron create [-h] [--name NAME] [--deliver DELIVER]
                          [--repeat REPEAT] [--skill SKILLS] [--script SCRIPT]
                          [--no-agent] [--monitor-script MONITOR_SCRIPT]
                          [--monitor-url MONITOR_URL] [--workdir WORKDIR]
                          [--model MODEL] [--provider MODEL_PROVIDER]
                          [--reasoning-effort REASONING_EFFORT]
                          [--continuity] schedule [prompt]
  --no-agent            Skip the LLM entirely — run --script on schedule and
  --continuity          Each run wakes up with the job's own previous output
```

### 4.16 `gateway --help`
```
usage: hermes gateway [-h] [--accept-hooks]
                      {run,start,stop,restart,status,install,uninstall,list,setup,migrate-legacy,enroll} ...

Manage the messaging gateway (Telegram, Discord, WhatsApp, Weixin, and more)
    enroll              Enroll this gateway with a relay connector (writes
                        relay auth creds to .env)
  --accept-hooks        Auto-approve unseen shell hooks without a TTY prompt
```

### 4.17 `auth --help`
```
usage: hermes auth [-h] {add,list,remove,reset,status,logout,spotify} ...

    add                 Add a pooled credential
    list                List pooled credentials
    remove              Remove a pooled credential by index, id, or label
    reset               Clear exhaustion status for all credentials for a
                        provider
```

### 4.18 `status --help`
```
usage: hermes status [-h] [--all] [--deep]

Display status of Hermes Agent components

options:
  -h, --help  show this help message and exit
  --all       Show all details (redacted for sharing)
  --deep      Run deep checks (may take longer)
```

### 4.19 `doctor --help`
```
usage: hermes doctor [-h] [--fix] [--live] [--ack ADVISORY_ID]
  --live             Opt-in: run one bounded, read-only real-call health probe
                     per configured tool backend
                     (Firecrawl/FAL/browser/MCP/TTS/STT) after the static
                     checks. Makes real network calls.
```

### 4.20 `logs --help`
```
usage: hermes logs [-h] [-n LINES] [-f] [--level LEVEL] [--session ID]
                   [--since TIME] [--component NAME]
                   [log_name]

View, tail, and filter agent.log / errors.log / gateway.log / gui.log / desktop.log
  -n, --lines LINES  Number of lines to show (default: 50)
  -f, --follow       Follow the log in real time (like tail -f)
  --component NAME   Filter by component: gateway, agent, tools, cli, cron,
                     gui
```

### 4.21 `completion --help`
```
usage: hermes completion [-h] [{bash,zsh,fish}]

positional arguments:
  {bash,zsh,fish}  Shell type (default: bash)

options:
  -h, --help       show this help message and exit
```

### 4.22 `prompt-size --help`
```
usage: hermes prompt-size [-h] [--platform PLATFORM] [--json]

Report the fixed prompt budget for a fresh session: system prompt total,
skills index, memory, user profile, and tool-schema JSON. Runs offline (no API
call).
  --platform PLATFORM  Platform to simulate (cli, telegram, discord, ...).
                       Default: cli
```

### 4.23 `update --help`
```
usage: hermes update [-h] [--gateway] [--check] [--plan] [--no-backup]
                     [--backup] [--yes] [--keep-stash] [--branch NAME]
                     [--switch-branch] [--force] [--force-venv]
  --plan           Show the update plan and exit without changing anything:
                   install kind (git/docker/nix), every running Hermes service
                   across all profiles with its supervisor and running code
                   version, and how each will be restarted. Read-only; safe on
                   a live fleet.
```

### 4.24 `dashboard --help`
```
usage: hermes dashboard [-h] [--port PORT] [--host HOST] [--insecure]
                        [--skip-build] [--isolated] [--stop] [--status]
                        [--no-open]
                        {register} ...
  --port PORT   Port (default 9119, 0 for auto-assign by OS)
  --host HOST   Host (default 127.0.0.1)
  --insecure    DEPRECATED / NO-OP. Formerly bypassed auth on a non-loopback
                bind. As of the June 2026 hardening it no longer disables
```

### 4.25 `serve --help`
```
usage: hermes serve [-h] [--port PORT] [--host HOST] [--insecure]
                    [--skip-build] [--isolated] [--stop] [--status]
                    [--ssh-session-token-file PATH] [--ssh-owner-nonce NONCE]

Run the Hermes backend server — the JSON-RPC/WebSocket gateway the desktop app
and remote clients connect to. Headless: it never opens a browser UI.
  --ssh-session-token-file PATH
                        Read a one-shot Desktop SSH session token from PATH
```

### 4.26 内部命令（5 个，全部 SUPPRESS）
```
usage: hermes __run-script [-h] script
---
usage: hermes __slash-worker [-h] --session-key SESSION_KEY [--model MODEL]
Internal: run the TUI slash-worker process (hidden).
---
usage: hermes __compute-host [-h]
Internal: run the dashboard compute-host process (hidden).
---
usage: hermes __gateway-restart-watch [-h] [--deadline DEADLINE] pid ...
Internal: poll a PID then respawn the gateway (hidden).
---
usage: hermes __update-gateway-helper [-h] output_path exit_code_path ...
Internal: run hermes update --gateway detached (hidden).
```
顶层列表中的呈现方式：
```
    __run-script        ==SUPPRESS==
    __slash-worker      ==SUPPRESS==
    __compute-host      ==SUPPRESS==
    __gateway-restart-watch
                        ==SUPPRESS==
    __update-gateway-helper
                        ==SUPPRESS==
```

### 4.27 别名等价性证据（`journey` = `learning` = `memory-graph`；`desktop` = `gui`）
```
# hermes learning --help / hermes memory-graph --help 输出与 journey 完全一致：
usage: hermes journey [-h] [--reveal 0..1] [--play] [--fps FPS]
                      [--width WIDTH] [--height HEIGHT] [--no-color] [--json]
                      {list,delete,edit} ...
# hermes gui --help 输出与 desktop 完全一致：
usage: hermes desktop [-h] [--source] [--build-only] [--fake-boot]
                      [--ignore-existing] [--hermes-root HERMES_ROOT]
```

---

## 附：观察到的 Hermes 自身文档不一致点（Orbit 对齐时注意）

| 现象 | 位置 | 建议 |
|---|---|---|
| 子命令集合里有 `status`，但描述段只提 `login/info/open/tools` | `hermes portal --help` | 以 `{login,info,status,open,tools}` 为准 |
| `kanban daemon` 仍存在但已标注 DEPRECATED（dispatcher 归 gateway） | `hermes kanban --help` | Orbit 不应新做 daemon 形态 |
| `login` 整体已废弃但仍在命令表内 | `hermes login --help` | Orbit 可直接不提供，或提供为 alias |
| `--insecure` 在 `dashboard`/`serve` 上是 NO-OP 但保留 | 二者 `--help` | 不要照抄其「关认证」语义 |
| `--help` 全部返回 exit code 0（任务预期可能为 1） | 408 次调用 | 无 exit 1 情况 |

