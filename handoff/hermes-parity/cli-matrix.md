# Orbit CLI 对标矩阵（自动生成）

> 由 `scripts/parity_report.py` 从 `backend/app/cli/spec.py` 的注册表生成，**不要手改**。
> 重算：`cd backend; .\.venv\Scripts\python.exe ..\scripts\parity_report.py`


命令条目合计 **411**：已实现 82、部分 3、待实现 326

| 命令 | 对标 Hermes | 归类 | 优先级 | 状态 | 说明 |
| --- | --- | --- | --- | --- | --- |
| `approvals` | `approvals` | ops | P0 | ✅ 已实现 | 命令审批：离线裁决预演、白名单、模式管理 |
| `approvals test` | `approvals test` | ops | P0 | ✅ 已实现 | 离线裁决一条命令（不执行、不提示、不落盘） |
| `approvals suggest` | `approvals suggest` | ops | P0 | ✅ 已实现 | 从历史命令里挖可加白的危险模式（破坏类永不提议） |
| `approvals allow` | `approvals allow` | ops | P0 | ✅ 已实现 | 管理命令白名单 |
| `approvals allow list` | `approvals allow list` | ops | P0 | ✅ 已实现 | 列出白名单 |
| `approvals allow add` | `approvals allow add` | ops | P0 | ✅ 已实现 | 加白一条危险规则 |
| `approvals allow remove` | `approvals allow remove` | ops | P0 | ✅ 已实现 | 从白名单移除一条规则 |
| `approvals list-rules` | `approvals list-rules` | ops | P0 | ✅ 已实现 | 列出全部硬地板与危险模式规则 |
| `approvals mode` | `approvals mode` | ops | P0 | ✅ 已实现 | 查看或切换审批模式（off / smart / manual） |
| `status` | `status` | core | P0 | ✅ 已实现 | 显示各组件状态（数据、模型、知识层、技能、记忆、MCP） |
| `doctor` | `doctor` | core | P0 | ✅ 已实现 | 检查配置与依赖（Python、数据库、模型、向量、浏览器内核、密钥库、磁盘） |
| `dump` | `dump` | ops | P0 | ✅ 已实现 | 导出一份排障用的完整设置快照（密钥打码） |
| `logs` | `logs` | ops | P0 | ✅ 已实现 | 查看、跟随与筛选日志文件 |
| `prompt-size` | `prompt-size` | core | P0 | ✅ 已实现 | 显示系统提示与工具 schema 的字节分解（离线，无 API 调用） |
| `completion` | `completion` | core | P0 | ✅ 已实现 | 打印 shell 补全脚本（bash / zsh / fish / powershell） |
| `config` | `config` | core | P0 | ✅ 已实现 | 查看与编辑配置 |
| `config show` | `config show` | core | P0 | ✅ 已实现 | 显示当前完整配置 |
| `config get` | `config get` | core | P0 | ✅ 已实现 | 打印一个已解析的配置值（不给键则打印整棵树） |
| `config set` | `config set` | core | P0 | ✅ 已实现 | 写入一个配置值（运行时键进 settings.json，其余进 backend/.env） |
| `config unset` | `config unset` | core | P0 | ✅ 已实现 | 移除一个配置值 |
| `config path` | `config path` | core | P0 | ✅ 已实现 | 打印配置文件路径 |
| `config env-path` | `config env-path` | core | P0 | ✅ 已实现 | 打印 .env 文件路径 |
| `config edit` | `config edit` | core | P0 | ✅ 已实现 | 用系统编辑器打开配置文件 |
| `config check` | `config check` | core | P0 | ✅ 已实现 | 检查缺失/过期的配置项 |
| `config migrate` | `config migrate` | core | P0 | ✅ 已实现 | 把配置结构升级到当前版本 |
| `cron` | `cron` | run | P0 | 🟡 部分 | 定时任务管理（不常驻，由外部调度器调用 `cron tick`） |
| `cron create` | `cron create` | run | P0 | ✅ 已实现 | 创建定时任务 |
| `cron edit` | `cron edit` | run | P0 | ✅ 已实现 | 编辑已有定时任务 |
| `cron list` | `cron list` | run | P0 | ✅ 已实现 | 列出定时任务 |
| `cron remove` | `cron remove` | run | P0 | ✅ 已实现 | 删除定时任务 |
| `cron run` | `cron run` | run | P0 | ✅ 已实现 | 把任务排到下一次 tick 执行 |
| `cron runs` | `cron runs` | run | P0 | ✅ 已实现 | 查看持久化的执行记录 |
| `cron tick` | `cron tick` | run | P0 | ✅ 已实现 | 执行到点任务后退出（给外部调度器调用） |
| `cron status` | `cron status` | run | P0 | ✅ 已实现 | 查看调度器状态（暂停、到点数、流水行数） |
| `cron pause` | `cron pause` | run | P0 | ✅ 已实现 | 暂停一个任务（job_id 给 all 则全局暂停） |
| `cron resume` | `cron resume` | run | P0 | ✅ 已实现 | 恢复任务（job_id 给 all 则解除全局暂停） |
| `cron doctor` | `cron doctor` | run | P0 | ✅ 已实现 | 体检定时任务（任务表 / 调度 / 流水 / 失败任务） |
| `cron incidents` | `cron incidents` | run | P0 | ✅ 已实现 | 列出或确认 cron 失败事故 |
| `model` | `model` | core | P0 | ✅ 已实现 | 选择默认模型与供应商（含 `--list` 列出全部 provider 与模型能力） |
| `fallback` | `fallback` | core | P0 | ✅ 已实现 | 管理主模型失败时的回退链（触发条件：429 / 529 / 503 / 连接失败） |
| `fallback list` | `fallback list` | core | P0 | ✅ 已实现 | 查看当前回退链 |
| `fallback add` | `fallback add` | core | P0 | ✅ 已实现 | 追加一个回退目标（provider 或 provider/模型） |
| `fallback remove` | `fallback remove` | core | P0 | ✅ 已实现 | 从回退链删除一项（给序号） |
| `fallback clear` | `fallback clear` | core | P0 | ✅ 已实现 | 清空全部回退项 |
| `auth` | `auth` | core | P0 | ✅ 已实现 | 管理池化的供应商凭证（密钥只进系统 keyring） |
| `auth add` | `auth add` | core | P0 | ✅ 已实现 | 添加池化凭证（密钥进系统 keyring） |
| `auth list` | `auth list` | core | P0 | ✅ 已实现 | 列出池化凭证（只显示指纹末 4 位） |
| `auth remove` | `auth remove` | core | P0 | ✅ 已实现 | 按序号 / 标签删除凭证 |
| `auth reset` | `auth reset` | core | P0 | ✅ 已实现 | 清除该 provider 全部凭证的耗尽状态（429 熔断后的恢复手段） |
| `auth status` | `auth status` | core | P0 | ✅ 已实现 | 查看 provider 的鉴权状态（主凭证 + 池） |
| `auth logout` | `auth logout` | core | P0 | ✅ 已实现 | 登出 provider 并清除存储的鉴权状态 |
| `auth spotify` | `auth spotify` | core | P2 | 🟡 部分 | 用 PKCE 完成 Spotify 鉴权（未实现，见报错原因） |
| `login` | `login` | core | P1 | ✅ 已实现 | 登录 api_key 类供应商（把密钥写进系统 keyring） |
| `logout` | `logout` | core | P1 | ✅ 已实现 | 清除供应商的鉴权状态（主凭证 + 该 provider 的凭证池） |
| `moa` | `moa` | core | P1 | 🟡 部分 | 配置 Mixture of Agents 模型槽位（只做配置读写，运行时聚合未实现） |
| `moa list` | `moa list` | core | P0 | ✅ 已实现 | 查看当前 MoA 模型槽位 |
| `moa configure` | `moa configure` | core | P0 | ✅ 已实现 | 创建或更新一个 MoA 预设 |
| `moa delete` | `moa delete` | core | P0 | ✅ 已实现 | 删除一个 MoA 预设 |
| `insights` | `insights` | ops | P0 | ✅ 已实现 | 分析会话历史：token 用量、工具使用模式、站点分布、活动趋势 |
| `monitoring` | `monitoring` | ops | P0 | ✅ 已实现 | 监控状态：健康指标 + 导出配置 + 脱敏姿态（不采集任何内容） |
| `monitoring status` | `monitoring status` | ops | P0 | ✅ 已实现 | 显示监控设置、导出状态与脱敏姿态 |
| `backup` | `backup` | ops | P0 | ✅ 已实现 | 把数据目录打包成带清单的 zip（SQLite 走在线快照） |
| `import` | `import` | ops | P0 | ✅ 已实现 | 从备份 zip 恢复数据目录（默认不覆盖，需 --force） |
| `checkpoints` | `checkpoints` | ops | P0 | ✅ 已实现 | 管理会话检查点存储（LangGraph 的 checkpoints.sqlite） |
| `checkpoints status` | `checkpoints status` | ops | P0 | ✅ 已实现 | 显示占用、线程数、检查点数与按线程的 Top 占用 |
| `checkpoints prune` | `checkpoints prune` | ops | P0 | ✅ 已实现 | 按保留期与体积上限清理检查点（需 -f） |
| `checkpoints clear` | `checkpoints clear` | ops | P0 | ✅ 已实现 | 清空整个检查点库（需 -f） |
| `checkpoints clear-legacy` | `checkpoints clear-legacy` | ops | P0 | ✅ 已实现 | 删除 v1 legacy 归档（Orbit 没有这类归档，无操作） |
| `session-search` | `session-search` | core | P0 | ✅ 已实现 | 会话召回：discover / scroll / read / browse 四形态（零模型调用） |
| `session-search discover` | `session-search discover` | core | P0 | ✅ 已实现 | 按关键词找候选消息，返回锚点（子串匹配，多词 AND） |
| `session-search scroll` | `session-search scroll` | core | P0 | ✅ 已实现 | 围绕锚点展开 ±window 条，并补首尾书挡 |
| `session-search read` | `session-search read` | core | P0 | ✅ 已实现 | 读单条消息的头 20 行 + 尾 10 行（中间标省略行数） |
| `session-search browse` | `session-search browse` | core | P0 | ✅ 已实现 | 按时间倒序列出最近会话 |
| `setup` | `setup` | core | P0 | ✅ 已实现 | 交互式配置向导（模型 / 权限级别 / 审批模式） |
| `tools` | `tools` | ext | P0 | ✅ 已实现 | 按平台配置工具启停 |
| `tools list` | `tools list` | ext | P0 | ✅ 已实现 | 列出全部工具及启停状态 |
| `tools enable` | `tools enable` | ext | P0 | ✅ 已实现 | 启用 toolset 或 MCP 工具 |
| `tools disable` | `tools disable` | ext | P0 | ✅ 已实现 | 禁用 toolset 或 MCP 工具 |
| `tools post-setup` | `tools post-setup` | ext | P0 | ✅ 已实现 | 输出当前平台的启停快照（可直接贴进 data/tools.json） |
| `portal` | `portal` | core | P1 | ⬜ 待实现 | Nous Portal 登录 / 选模型 / 工具网关 |
| `portal login` | `portal login` | core | P1 | ⬜ 待实现 | 登录 Nous Portal 并完成设置 |
| `portal info` | `portal info` | core | P1 | ⬜ 待实现 | 查看 Portal 鉴权与工具网关路由 |
| `portal open` | `portal open` | core | P1 | ⬜ 待实现 | 在默认浏览器打开订阅页 |
| `portal tools` | `portal tools` | core | P1 | ⬜ 待实现 | 列出工具网关工具及路由方式 |
| `secrets` | `secrets` | core | P1 | ⬜ 待实现 | 管理外部密钥源（Bitwarden / 1Password） |
| `secrets bitwarden` | `secrets bitwarden` | core | P1 | ⬜ 待实现 | Bitwarden Secrets Manager 集成 |
| `secrets bitwarden setup` | `secrets bitwarden setup` | core | P1 | ⬜ 待实现 | 向导：安装 bws、存令牌、选项目 |
| `secrets bitwarden status` | `secrets bitwarden status` | core | P1 | ⬜ 待实现 | 查看配置、二进制与令牌状态 |
| `secrets bitwarden token` | `secrets bitwarden token` | core | P1 | ⬜ 待实现 | 轮换访问令牌并写入 .env |
| `secrets bitwarden sync` | `secrets bitwarden sync` | core | P1 | ⬜ 待实现 | 立即拉取密钥并报告变化 |
| `secrets bitwarden disable` | `secrets bitwarden disable` | core | P1 | ⬜ 待实现 | 关闭 Bitwarden 集成 |
| `secrets bitwarden install` | `secrets bitwarden install` | core | P1 | ⬜ 待实现 | 下载并校验 bws 二进制 |
| `secrets onepassword` | `secrets onepassword` | core | P1 | ⬜ 待实现 | 1Password CLI 集成 |
| `secrets onepassword setup` | `secrets onepassword setup` | core | P1 | ⬜ 待实现 | 校验 op CLI、设账号与令牌、启用 |
| `secrets onepassword status` | `secrets onepassword status` | core | P1 | ⬜ 待实现 | 查看配置、op 二进制与引用 |
| `secrets onepassword token` | `secrets onepassword token` | core | P1 | ⬜ 待实现 | 轮换服务账号令牌并写入 .env |
| `secrets onepassword set` | `secrets onepassword set` | core | P1 | ⬜ 待实现 | 把环境变量映射到 op:// 引用 |
| `secrets onepassword remove` | `secrets onepassword remove` | core | P1 | ⬜ 待实现 | 移除环境变量映射 |
| `secrets onepassword sync` | `secrets onepassword sync` | core | P1 | ⬜ 待实现 | 立即解析引用并报告变化 |
| `secrets onepassword disable` | `secrets onepassword disable` | core | P1 | ⬜ 待实现 | 关闭 1Password 集成 |
| `skin` | `skin` | core | P2 | ⬜ 待实现 | 列出 / 切换 / 微调皮肤 |
| `skin list` | `skin list` | core | P1 | ⬜ 待实现 | 列出可用皮肤 |
| `skin use` | `skin use` | core | P1 | ⬜ 待实现 | 切换当前皮肤 |
| `skin set` | `skin set` | core | P1 | ⬜ 待实现 | 设置当前皮肤的某一颜色 |
| `profile` | `profile` | core | P1 | ⬜ 待实现 | 管理 profile（多套隔离实例） |
| `profile list` | `profile list` | core | P1 | ⬜ 待实现 | 列出全部 profile |
| `profile use` | `profile use` | core | P1 | ⬜ 待实现 | 设置粘性默认 profile |
| `profile create` | `profile create` | core | P1 | ⬜ 待实现 | 创建新 profile |
| `profile delete` | `profile delete` | core | P1 | ⬜ 待实现 | 删除 profile |
| `profile describe` | `profile describe` | core | P1 | ⬜ 待实现 | 读写 profile 描述 |
| `profile show` | `profile show` | core | P1 | ⬜ 待实现 | 查看 profile 详情 |
| `profile alias` | `profile alias` | core | P1 | ⬜ 待实现 | 管理包装脚本别名 |
| `profile rename` | `profile rename` | core | P1 | ⬜ 待实现 | 重命名 profile |
| `profile export` | `profile export` | core | P1 | ⬜ 待实现 | 导出 profile 为归档 |
| `profile import` | `profile import` | core | P1 | ⬜ 待实现 | 从归档导入 profile |
| `profile install` | `profile install` | core | P1 | ⬜ 待实现 | 安装 profile 发行包 |
| `profile update` | `profile update` | core | P1 | ⬜ 待实现 | 重新拉取并更新发行包 |
| `profile info` | `profile info` | core | P1 | ⬜ 待实现 | 查看发行包清单 |
| `project` | `project` | core | P1 | ⬜ 待实现 | 管理项目（命名多目录工作区） |
| `project create` | `project create` | core | P1 | ⬜ 待实现 | 创建项目 |
| `project list` | `project list` | core | P1 | ⬜ 待实现 | 列出项目 |
| `project show` | `project show` | core | P1 | ⬜ 待实现 | 查看项目详情 |
| `project add-folder` | `project add-folder` | core | P1 | ⬜ 待实现 | 给项目添加目录 |
| `project remove-folder` | `project remove-folder` | core | P1 | ⬜ 待实现 | 移除项目目录 |
| `project rename` | `project rename` | core | P1 | ⬜ 待实现 | 重命名项目 |
| `project set-primary` | `project set-primary` | core | P1 | ⬜ 待实现 | 设置主目录 |
| `project use` | `project use` | core | P1 | ⬜ 待实现 | 设置当前项目 |
| `project archive` | `project archive` | core | P1 | ⬜ 待实现 | 归档项目 |
| `project restore` | `project restore` | core | P1 | ⬜ 待实现 | 恢复已归档项目 |
| `project bind-board` | `project bind-board` | core | P1 | ⬜ 待实现 | 绑定看板到项目 |
| `chat` | `chat` | run | P0 | ⬜ 待实现 | 与 Agent 交互对话 |
| `gateway` | `gateway` | run | P0 | ⬜ 待实现 | 消息网关管理 |
| `gateway run` | `gateway run` | core | P1 | ⬜ 待实现 | 前台运行网关 |
| `gateway start` | `gateway start` | core | P1 | ⬜ 待实现 | 启动已安装的后台服务 |
| `gateway stop` | `gateway stop` | core | P1 | ⬜ 待实现 | 停止网关服务 |
| `gateway restart` | `gateway restart` | core | P1 | ⬜ 待实现 | 重启网关服务 |
| `gateway status` | `gateway status` | core | P1 | ⬜ 待实现 | 查看网关状态 |
| `gateway install` | `gateway install` | core | P1 | ⬜ 待实现 | 安装网关为后台服务 |
| `gateway uninstall` | `gateway uninstall` | core | P1 | ⬜ 待实现 | 卸载网关服务 |
| `gateway list` | `gateway list` | core | P1 | ⬜ 待实现 | 列出各 profile 的网关状态 |
| `gateway setup` | `gateway setup` | core | P1 | ⬜ 待实现 | 配置消息平台 |
| `gateway migrate-legacy` | `gateway migrate-legacy` | core | P1 | ⬜ 待实现 | 清理旧版 hermes.service 单元 |
| `gateway enroll` | `gateway enroll` | core | P1 | ⬜ 待实现 | 向中继连接器注册本网关 |
| `serve` | `serve` | run | P0 | ⬜ 待实现 | 启动后端服务（headless） |
| `send` | `send` | run | P1 | ⬜ 待实现 | 向已配置平台发送消息 |
| `pause` | `pause` | run | P1 | ⬜ 待实现 | 紧急停止：暂停 cron / 看板调度与新网关轮次 |
| `resume` | `resume` | run | P1 | ⬜ 待实现 | 解除 `pause` 设置的紧急停止 |
| `acp` | `acp` | run | P1 | ⬜ 待实现 | 以 ACP 服务方式运行 Agent |
| `console` | `console` | run | P1 | ⬜ 待实现 | 打开安全命令控制台 |
| `dashboard` | `dashboard` | run | P1 | ⬜ 待实现 | 启动 Web UI 面板 |
| `dashboard register` | `dashboard register` | core | P1 | ⬜ 待实现 | 把自托管面板注册到 Portal |
| `desktop` | `desktop` | run | P1 | ⬜ 待实现 | 构建并启动原生桌面应用 |
| `computer-use` | `computer-use` | run | P1 | ⬜ 待实现 | 管理 Computer Use（cua-driver）后端 |
| `computer-use install` | `computer-use install` | core | P1 | ⬜ 待实现 | 安装或修复 cua-driver |
| `computer-use status` | `computer-use status` | core | P1 | ⬜ 待实现 | 查看 cua-driver 是否可用 |
| `computer-use doctor` | `computer-use doctor` | core | P1 | ⬜ 待实现 | 运行 cua-driver 健康检查 |
| `computer-use permissions` | `computer-use permissions` | core | P1 | ⬜ 待实现 | 检查 / 授予系统权限 |
| `computer-use permissions status` | `computer-use permissions status` | core | P1 | ⬜ 待实现 | 查看权限授予状态 |
| `computer-use permissions grant` | `computer-use permissions grant` | core | P1 | ⬜ 待实现 | 申请系统权限（弹出授权框） |
| `kanban` | `kanban` | run | P1 | ⬜ 待实现 | 多 profile 协作看板 |
| `kanban init` | `kanban init` | core | P1 | ⬜ 待实现 | 按需创建 kanban.db（幂等） |
| `kanban boards` | `kanban boards` | core | P1 | ⬜ 待实现 | 管理看板（每个项目一块） |
| `kanban boards list` | `kanban boards list` | core | P1 | ⬜ 待实现 | 列出看板及任务数 |
| `kanban boards create` | `kanban boards create` | core | P1 | ⬜ 待实现 | 创建看板 |
| `kanban boards rm` | `kanban boards rm` | core | P1 | ⬜ 待实现 | 归档或删除看板 |
| `kanban boards switch` | `kanban boards switch` | core | P1 | ⬜ 待实现 | 切换当前看板 |
| `kanban boards show` | `kanban boards show` | core | P1 | ⬜ 待实现 | 显示当前看板 slug |
| `kanban boards rename` | `kanban boards rename` | core | P1 | ⬜ 待实现 | 改看板显示名 |
| `kanban boards set-default-workdir` | `kanban boards set-default-workdir` | core | P1 | ⬜ 待实现 | 设置看板默认工作目录 |
| `kanban boards export` | `kanban boards export` | core | P1 | ⬜ 待实现 | 导出看板为 tar.gz |
| `kanban boards import` | `kanban boards import` | core | P1 | ⬜ 待实现 | 导入看板归档 |
| `kanban create` | `kanban create` | core | P1 | ⬜ 待实现 | 创建任务 |
| `kanban swarm` | `kanban swarm` | core | P1 | ⬜ 待实现 | 创建 swarm 任务图 |
| `kanban list` | `kanban list` | core | P1 | ⬜ 待实现 | 列出任务 |
| `kanban show` | `kanban show` | core | P1 | ⬜ 待实现 | 查看任务详情与评论 |
| `kanban assign` | `kanban assign` | core | P1 | ⬜ 待实现 | 指派 / 改派任务 |
| `kanban set-model` | `kanban set-model` | core | P1 | ⬜ 待实现 | 设置或清除任务的模型覆盖 |
| `kanban reclaim` | `kanban reclaim` | core | P1 | ⬜ 待实现 | 释放运行中任务的占用 |
| `kanban reassign` | `kanban reassign` | core | P1 | ⬜ 待实现 | 改派到其它 profile |
| `kanban diagnostics` | `kanban diagnostics` | core | P1 | ⬜ 待实现 | 列出当前看板的诊断 |
| `kanban link` | `kanban link` | core | P1 | ⬜ 待实现 | 添加父子依赖 |
| `kanban unlink` | `kanban unlink` | core | P1 | ⬜ 待实现 | 移除父子依赖 |
| `kanban claim` | `kanban claim` | core | P1 | ⬜ 待实现 | 原子认领就绪任务 |
| `kanban comment` | `kanban comment` | core | P1 | ⬜ 待实现 | 追加评论 |
| `kanban attach` | `kanban attach` | core | P1 | ⬜ 待实现 | 给任务附加本地文件 |
| `kanban attachments` | `kanban attachments` | core | P1 | ⬜ 待实现 | 列出任务附件 |
| `kanban attach-rm` | `kanban attach-rm` | core | P1 | ⬜ 待实现 | 按 id 删除附件 |
| `kanban complete` | `kanban complete` | core | P1 | ⬜ 待实现 | 标记任务完成 |
| `kanban edit` | `kanban edit` | core | P1 | ⬜ 待实现 | 编辑已完成任务的恢复字段 |
| `kanban block` | `kanban block` | core | P1 | ⬜ 待实现 | 标记任务阻塞 |
| `kanban schedule` | `kanban schedule` | core | P1 | ⬜ 待实现 | 把任务挂起等待时间 / 依赖 |
| `kanban unblock` | `kanban unblock` | core | P1 | ⬜ 待实现 | 解除阻塞回到就绪 |
| `kanban request-review` | `kanban request-review` | core | P1 | ⬜ 待实现 | 提交评审 |
| `kanban request-changes` | `kanban request-changes` | core | P1 | ⬜ 待实现 | 评审要求修改 |
| `kanban reopen-review` | `kanban reopen-review` | core | P1 | ⬜ 待实现 | 评审任务打回重做 |
| `kanban promote` | `kanban promote` | core | P1 | ⬜ 待实现 | 手动提升为就绪 |
| `kanban archive` | `kanban archive` | core | P1 | ⬜ 待实现 | 归档任务 |
| `kanban tail` | `kanban tail` | core | P1 | ⬜ 待实现 | 跟踪任务事件流 |
| `kanban dispatch` | `kanban dispatch` | core | P1 | ⬜ 待实现 | 执行一轮调度 |
| `kanban daemon` | `kanban daemon` | core | P1 | ⬜ 待实现 | 后台调度器（已弃用，调度归 gateway） |
| `kanban watch` | `kanban watch` | core | P1 | ⬜ 待实现 | 实时流式查看任务事件 |
| `kanban stats` | `kanban stats` | core | P1 | ⬜ 待实现 | 按状态与负责人统计 |
| `kanban notify-subscribe` | `kanban notify-subscribe` | core | P1 | ⬜ 待实现 | 订阅任务的终态通知 |
| `kanban notify-list` | `kanban notify-list` | core | P1 | ⬜ 待实现 | 列出通知订阅 |
| `kanban notify-unsubscribe` | `kanban notify-unsubscribe` | core | P1 | ⬜ 待实现 | 取消任务的通知订阅 |
| `kanban log` | `kanban log` | core | P1 | ⬜ 待实现 | 打印任务 worker 日志 |
| `kanban runs` | `kanban runs` | core | P1 | ⬜ 待实现 | 查看任务运行历史 |
| `kanban heartbeat` | `kanban heartbeat` | core | P1 | ⬜ 待实现 | 为运行中任务发心跳 |
| `kanban assignees` | `kanban assignees` | core | P1 | ⬜ 待实现 | 列出可指派的 profile |
| `kanban context` | `kanban context` | core | P1 | ⬜ 待实现 | 打印 worker 看到的任务上下文 |
| `kanban specify` | `kanban specify` | core | P1 | ⬜ 待实现 | 把 triage 任务补成具体规格 |
| `kanban decompose` | `kanban decompose` | core | P1 | ⬜ 待实现 | 把 triage 任务拆成子任务图 |
| `kanban gc` | `kanban gc` | core | P1 | ⬜ 待实现 | 回收工作区 / 事件 / 日志 |
| `kanban repair` | `kanban repair` | core | P1 | ⬜ 待实现 | 检查并自动修复 kanban.db |
| `worktree` | `worktree` | run | P1 | ⬜ 待实现 | 审计并回收 git worktree 与已合并分支 |
| `worktree list` | `worktree list` | core | P1 | ⬜ 待实现 | 逐棵分类：树龄 / 大小 / 结论 |
| `worktree prune` | `worktree prune` | core | P1 | ⬜ 待实现 | 删除安全 worktree 与已合并分支 |
| `verify` | `verify` | run | P1 | ⬜ 待实现 | 探测项目运行配方并做冒烟测试 |
| `sessions` | `sessions` | ops | P0 | ⬜ 待实现 | 会话历史管理（列表 / 改名 / 导出 / 清理） |
| `sessions list` | `sessions list` | core | P1 | ✅ 已实现 | 列出近期会话 |
| `sessions export` | `sessions export` | core | P1 | ✅ 已实现 | 导出会话为 JSONL / Markdown / QMD |
| `sessions delete` | `sessions delete` | core | P1 | ✅ 已实现 | 删除指定会话 |
| `sessions prune` | `sessions prune` | core | P1 | ⬜ 待实现 | 按过滤条件删除旧会话 |
| `sessions archive` | `sessions archive` | core | P1 | ⬜ 待实现 | 按过滤条件批量归档会话 |
| `sessions optimize` | `sessions optimize` | core | P1 | ⬜ 待实现 | 合并 FTS 段并 VACUUM 回收空间 |
| `sessions clean-markers` | `sessions clean-markers` | core | P1 | ⬜ 待实现 | 清理残留的工具调用标记 |
| `sessions optimize-storage` | `sessions optimize-storage` | core | P1 | ⬜ 待实现 | 迁移搜索索引到紧凑布局 |
| `sessions repair` | `sessions repair` | core | P1 | ⬜ 待实现 | 修复 state.db 结构 |
| `sessions repair-routing` | `sessions repair-routing` | core | P1 | ⬜ 待实现 | 重写丢失路由的网关会话 |
| `sessions recover` | `sessions recover` | core | P1 | ⬜ 待实现 | 把会话数据重建到干净目录 |
| `sessions stats` | `sessions stats` | core | P1 | ✅ 已实现 | 查看会话库统计 |
| `sessions rename` | `sessions rename` | core | P1 | ✅ 已实现 | 设置会话标题 |
| `sessions pin` | `sessions pin` | core | P1 | ⬜ 待实现 | 固定会话（免自动清理） |
| `sessions unpin` | `sessions unpin` | core | P1 | ⬜ 待实现 | 取消固定 |
| `sessions pinned` | `sessions pinned` | core | P1 | ⬜ 待实现 | 列出已固定会话 |
| `sessions retitle-skills` | `sessions retitle-skills` | core | P1 | ⬜ 待实现 | 按技能重命名自动标题会话 |
| `sessions browse` | `sessions browse` | core | P1 | ✅ 已实现 | 交互式浏览 / 搜索会话 |
| `sessions import` | `sessions import` | core | P1 | ⬜ 待实现 | 导入 Claude Code / Codex 会话 |
| `import-agent` | `import-agent` | ops | P1 | ⬜ 待实现 | 导入 Claude Code / Codex CLI 配置 |
| `migrate` | `migrate` | ops | P1 | ⬜ 待实现 | 迁移已退役模型 / 弃用配置 |
| `migrate xai` | `migrate xai` | core | P1 | ⬜ 待实现 | 迁移 2026-05-15 退役的 xAI 模型 |
| `update` | `update` | ops | P1 | ⬜ 待实现 | 更新到最新版本 |
| `uninstall` | `uninstall` | ops | P1 | ⬜ 待实现 | 卸载 Hermes |
| `debug` | `debug` | ops | P1 | ⬜ 待实现 | 上传日志与系统信息以求助 |
| `debug share` | `debug share` | core | P1 | ⬜ 待实现 | 上传调试报告并给出分享链接 |
| `debug delete` | `debug delete` | core | P1 | ⬜ 待实现 | 删除已上传的 paste |
| `security` | `security` | ops | P1 | ⬜ 待实现 | 供应链审计（OSV.dev） |
| `security audit` | `security audit` | core | P1 | ⬜ 待实现 | 跑一次供应链审计 |
| `hooks` | `hooks` | ops | P0 | ⬜ 待实现 | 查看与管理 shell 脚本钩子 |
| `hooks list` | `hooks list` | core | P1 | ⬜ 待实现 | 列出钩子及其匹配器 / 超时 |
| `hooks test` | `hooks test` | core | P1 | ⬜ 待实现 | 对匹配事件触发一次合成负载 |
| `hooks revoke` | `hooks revoke` | core | P1 | ⬜ 待实现 | 吊销某个钩子命令 |
| `hooks doctor` | `hooks doctor` | core | P1 | ⬜ 待实现 | 检查钩子的权限 / 白名单 / mtime |
| `claw` | `claw` | ops | P1 | ⬜ 待实现 | OpenClaw 迁移工具 |
| `claw migrate` | `claw migrate` | core | P1 | ⬜ 待实现 | 从 OpenClaw 迁移到 Hermes |
| `claw cleanup` | `claw cleanup` | core | P1 | ⬜ 待实现 | 归档迁移后残留的 OpenClaw 目录 |
| `lsp` | `lsp` | ops | P1 | ⬜ 待实现 | 语言服务器（LSP）管理 |
| `lsp status` | `lsp status` | core | P1 | ⬜ 待实现 | 查看 LSP 服务状态 |
| `lsp list` | `lsp list` | core | P1 | ⬜ 待实现 | 列出支持的语言服务器 |
| `lsp install` | `lsp install` | core | P1 | ⬜ 待实现 | 安装某个服务器二进制 |
| `lsp install-all` | `lsp install-all` | core | P1 | ⬜ 待实现 | 安装所有可自动安装的服务器 |
| `lsp restart` | `lsp restart` | core | P1 | ⬜ 待实现 | 拆掉运行中的 LSP 客户端 |
| `lsp which` | `lsp which` | core | P1 | ⬜ 待实现 | 打印某服务器的二进制路径 |
| `egress` | `egress` | ops | P1 | ⬜ 待实现 | 出口凭证注入防火墙（iron-proxy） |
| `egress install` | `egress install` | core | P1 | ⬜ 待实现 | 下载 iron-proxy 二进制 |
| `egress setup` | `egress setup` | core | P1 | ⬜ 待实现 | 向导：安装 + CA + 铸令牌 + 写配置 |
| `egress start` | `egress start` | core | P1 | ⬜ 待实现 | 启动受管 iron-proxy |
| `egress stop` | `egress stop` | core | P1 | ⬜ 待实现 | 停止受管 iron-proxy |
| `egress restart` | `egress restart` | core | P1 | ⬜ 待实现 | 重启受管 iron-proxy |
| `egress reload` | `egress reload` | core | P1 | ⬜ 待实现 | 热重载运行中守护进程的规则 |
| `egress status` | `egress status` | core | P1 | ⬜ 待实现 | 查看代理状态与映射 |
| `egress disable` | `egress disable` | core | P1 | ⬜ 待实现 | 关闭代理集成 |
| `egress config` | `egress config` | core | P1 | ⬜ 待实现 | 打印生成的 proxy.yaml 路径 |
| `proxy` | `proxy` | ops | P1 | ⬜ 待实现 | 本地 OpenAI 兼容代理（OAuth 供应商） |
| `proxy start` | `proxy start` | core | P1 | ⬜ 待实现 | 前台运行代理 |
| `proxy status` | `proxy status` | core | P1 | ⬜ 待实现 | 查看哪些上游已就绪 |
| `proxy providers` | `proxy providers` | core | P1 | ⬜ 待实现 | 列出可用的代理上游供应商 |
| `sync` | `sync` | ops | P1 | ⬜ 待实现 | Skill 同步（跨设备 / 团队） |
| `sync status` | `sync status` | core | P1 | ⬜ 待实现 | 查看同步范围与来源 |
| `sync pull` | `sync pull` | core | P1 | ⬜ 待实现 | 拉取已同步技能 |
| `sync push` | `sync push` | core | P1 | ⬜ 待实现 | 推送已选择加入的技能 |
| `sync now` | `sync now` | core | P1 | ⬜ 待实现 | 立即对账：先拉后推 |
| `sync enable` | `sync enable` | core | P1 | ⬜ 待实现 | 把技能纳入同步 |
| `sync disable` | `sync disable` | core | P1 | ⬜ 待实现 | 把技能移出同步 |
| `sync device` | `sync device` | core | P1 | ⬜ 待实现 | 查看或设置本机标签 |
| `sync propose` | `sync propose` | core | P1 | ⬜ 待实现 | 把技能提交到组织共享集 |
| `browser` | `browser` | ops | P1 | ⬜ 待实现 | 真实 profile 浏览辅助（关闭占用进程） |
| `browser close-profile` | `browser close-profile` | core | P1 | ⬜ 待实现 | 关闭占用真实 profile 的浏览器 |
| `whatsapp` | `whatsapp` | platform | P1 | ⬜ 待实现 | 配置 WhatsApp 集成 |
| `whatsapp-cloud` | `whatsapp-cloud` | platform | P1 | ⬜ 待实现 | 配置 WhatsApp Business Cloud API 集成 |
| `slack` | `slack` | platform | P1 | ⬜ 待实现 | Slack 集成辅助（manifest 生成等） |
| `slack manifest` | `slack manifest` | core | P1 | ⬜ 待实现 | 打印或写出 Slack app manifest |
| `webhook` | `webhook` | platform | P1 | ⬜ 待实现 | 动态 webhook 订阅管理 |
| `webhook subscribe` | `webhook subscribe` | core | P1 | ⬜ 待实现 | 创建 webhook 订阅 |
| `webhook list` | `webhook list` | core | P1 | ⬜ 待实现 | 列出全部动态订阅 |
| `webhook remove` | `webhook remove` | core | P1 | ⬜ 待实现 | 删除订阅 |
| `webhook test` | `webhook test` | core | P1 | ⬜ 待实现 | 向路由发一次测试 POST |
| `pairing` | `pairing` | platform | P1 | ⬜ 待实现 | DM 配对码与用户授权 |
| `pairing list` | `pairing list` | core | P1 | ⬜ 待实现 | 查看待批准与已批准用户 |
| `pairing approve` | `pairing approve` | core | P1 | ⬜ 待实现 | 批准配对请求 |
| `pairing revoke` | `pairing revoke` | core | P1 | ⬜ 待实现 | 吊销用户访问 |
| `pairing clear-pending` | `pairing clear-pending` | core | P1 | ⬜ 待实现 | 清空全部待批准码 |
| `peer` | `peer` | platform | P1 | ⬜ 待实现 | 跨机器 bot 间私信（对等网关） |
| `peer add` | `peer add` | core | P1 | ⬜ 待实现 | 注册（或更新）对等网关 |
| `peer list` | `peer list` | core | P1 | ⬜ 待实现 | 列出已注册对等节点 |
| `peer remove` | `peer remove` | core | P1 | ⬜ 待实现 | 移除对等节点 |
| `peer dm` | `peer dm` | core | P1 | ⬜ 待实现 | 发消息给对等节点并打印回复 |
| `peer run` | `peer run` | core | P1 | ⬜ 待实现 | 异步发起一次长任务 |
| `peer status` | `peer status` | core | P1 | ⬜ 待实现 | 读取异步任务状态与输出 |
| `peer stop` | `peer stop` | core | P1 | ⬜ 待实现 | 停止一个异步对等任务 |
| `skills` | `skills` | ext | P0 | ⬜ 待实现 | 技能搜索 / 安装 / 配置 / 管理 |
| `skills trust` | `skills trust` | core | P1 | ⬜ 待实现 | 信任项目以加载其仓库内技能 |
| `skills untrust` | `skills untrust` | core | P1 | ⬜ 待实现 | 撤销项目技能信任 |
| `skills browse` | `skills browse` | core | P1 | ⬜ 待实现 | 分页浏览全部可用技能 |
| `skills search` | `skills search` | core | P1 | ⬜ 待实现 | 搜索技能仓库 |
| `skills install` | `skills install` | core | P1 | ⬜ 待实现 | 安装技能 |
| `skills inspect` | `skills inspect` | core | P1 | ⬜ 待实现 | 安装前预览技能 |
| `skills list` | `skills list` | core | P1 | ⬜ 待实现 | 列出已安装技能 |
| `skills check` | `skills check` | core | P1 | ⬜ 待实现 | 检查 hub 技能是否有更新 |
| `skills update` | `skills update` | core | P1 | ⬜ 待实现 | 更新已安装的 hub 技能 |
| `skills audit` | `skills audit` | core | P1 | ⬜ 待实现 | 重扫已安装的 hub 技能 |
| `skills uninstall` | `skills uninstall` | core | P1 | ⬜ 待实现 | 卸载 hub 安装的技能 |
| `skills reset` | `skills reset` | core | P1 | ⬜ 待实现 | 清除内置技能的 user-modified 标记 |
| `skills list-modified` | `skills list-modified` | core | P1 | ⬜ 待实现 | 列出被本地改过的内置技能 |
| `skills diff` | `skills diff` | core | P1 | ⬜ 待实现 | 对比本地与内置版本差异 |
| `skills opt-out` | `skills opt-out` | core | P1 | ⬜ 待实现 | 停止向 profile 播种内置技能 |
| `skills opt-in` | `skills opt-in` | core | P1 | ⬜ 待实现 | 恢复内置技能播种 |
| `skills repair-official` | `skills repair-official` | core | P1 | ⬜ 待实现 | 回填或恢复官方可选技能 |
| `skills publish` | `skills publish` | core | P1 | ⬜ 待实现 | 把技能发布到注册表 |
| `skills snapshot` | `skills snapshot` | core | P1 | ⬜ 待实现 | 导出 / 导入技能配置 |
| `skills snapshot export` | `skills snapshot export` | core | P1 | ⬜ 待实现 | 把已安装技能导出到文件 |
| `skills snapshot import` | `skills snapshot import` | core | P1 | ⬜ 待实现 | 从文件导入并安装技能 |
| `skills tap` | `skills tap` | core | P1 | ⬜ 待实现 | 管理技能来源（taps） |
| `skills tap list` | `skills tap list` | core | P1 | ⬜ 待实现 | 列出已配置的 tap |
| `skills tap add` | `skills tap add` | core | P1 | ⬜ 待实现 | 把 GitHub 仓库加为技能源 |
| `skills tap remove` | `skills tap remove` | core | P1 | ⬜ 待实现 | 移除一个 tap |
| `skills config` | `skills config` | core | P1 | ⬜ 待实现 | 交互式启停技能 |
| `bundles` | `bundles` | ext | P1 | ⬜ 待实现 | 技能包（多技能别名）管理 |
| `bundles list` | `bundles list` | core | P1 | ⬜ 待实现 | 列出已安装技能包 |
| `bundles show` | `bundles show` | core | P1 | ⬜ 待实现 | 查看单个技能包内容 |
| `bundles create` | `bundles create` | core | P1 | ⬜ 待实现 | 创建技能包 |
| `bundles delete` | `bundles delete` | core | P1 | ⬜ 待实现 | 删除技能包 |
| `bundles reload` | `bundles reload` | core | P1 | ⬜ 待实现 | 重扫技能包目录并报告变化 |
| `plugins` | `plugins` | ext | P0 | ⬜ 待实现 | 插件管理与校验 |
| `plugins install` | `plugins install` | core | P1 | ⬜ 待实现 | 从 Git URL / owner/repo / 索引安装插件 |
| `plugins search` | `plugins search` | core | P1 | ⬜ 待实现 | 搜索社区插件索引 |
| `plugins update` | `plugins update` | core | P1 | ⬜ 待实现 | 拉取已安装插件的最新代码 |
| `plugins remove` | `plugins remove` | core | P1 | ⬜ 待实现 | 移除插件 |
| `plugins list` | `plugins list` | core | P1 | ⬜ 待实现 | 列出已安装插件 |
| `plugins enable` | `plugins enable` | core | P1 | ⬜ 待实现 | 启用插件 |
| `plugins disable` | `plugins disable` | core | P1 | ⬜ 待实现 | 禁用插件（不卸载） |
| `plugins capabilities` | `plugins capabilities` | core | P1 | ⬜ 待实现 | 查看声明与授予的能力 |
| `plugins doctor` | `plugins doctor` | core | P1 | ⬜ 待实现 | 按真实运行时契约校验插件 |
| `plugins pack` | `plugins pack` | core | P1 | ⬜ 待实现 | 可分享的插件集（hermes-pack.yaml） |
| `plugins pack install` | `plugins pack install` | core | P1 | ⬜ 待实现 | 审阅并安装一个 pack |
| `plugins pack export` | `plugins pack export` | core | P1 | ⬜ 待实现 | 把当前安装导出成 pack YAML |
| `plugins pack show` | `plugins pack show` | core | P1 | ⬜ 待实现 | 干跑：解析并展示 pack |
| `plugins show` | `plugins show` | core | P1 | ⬜ 待实现 | 查看单个插件详情 |
| `curator` | `curator` | ext | P1 | ⬜ 待实现 | 后台技能维护（curator） |
| `curator status` | `curator status` | core | P1 | ⬜ 待实现 | 查看 curator 状态与技能统计 |
| `curator usage` | `curator usage` | core | P1 | ⬜ 待实现 | 查看全部技能的使用遥测 |
| `curator run` | `curator run` | core | P1 | ⬜ 待实现 | 立即触发一次 curator 复查 |
| `curator pause` | `curator pause` | core | P1 | ⬜ 待实现 | 暂停 curator |
| `curator resume` | `curator resume` | core | P1 | ⬜ 待实现 | 恢复 curator |
| `curator pin` | `curator pin` | core | P1 | ⬜ 待实现 | 固定技能（不再自动迁移） |
| `curator unpin` | `curator unpin` | core | P1 | ⬜ 待实现 | 取消固定 |
| `curator list-unmanaged` | `curator list-unmanaged` | core | P1 | ⬜ 待实现 | 列出无 provenance 的可治理技能 |
| `curator adopt` | `curator adopt` | core | P1 | ⬜ 待实现 | 把无主技能交给 curator 治理 |
| `curator restore` | `curator restore` | core | P1 | ⬜ 待实现 | 恢复已归档技能 |
| `curator list-archived` | `curator list-archived` | core | P1 | ⬜ 待实现 | 列出已归档技能 |
| `curator archive` | `curator archive` | core | P1 | ⬜ 待实现 | 手动归档技能 |
| `curator prune` | `curator prune` | core | P1 | ⬜ 待实现 | 批量归档闲置技能 |
| `curator backup` | `curator backup` | core | P1 | ⬜ 待实现 | 对技能目录做 tar.gz 快照 |
| `curator rollback` | `curator rollback` | core | P1 | ⬜ 待实现 | 从快照恢复技能目录 / 单条变更 |
| `curator ledger` | `curator ledger` | core | P1 | ⬜ 待实现 | 列出逐次变更审计账本 |
| `curator purge` | `curator purge` | core | P1 | ⬜ 待实现 | 删除超过 TTL 的归档技能 |
| `memory` | `memory` | ext | P0 | ⬜ 待实现 | 配置外部记忆供应商 |
| `memory setup` | `memory setup` | core | P1 | ⬜ 待实现 | 交互选择并配置供应商 |
| `memory status` | `memory status` | core | P1 | ⬜ 待实现 | 查看当前记忆供应商配置 |
| `memory off` | `memory off` | core | P1 | ⬜ 待实现 | 关闭外部供应商（仅用内置） |
| `memory reset` | `memory reset` | core | P1 | ⬜ 待实现 | 清空内置记忆（MEMORY.md / USER.md） |
| `journey` | `journey` | ext | P1 | ⬜ 待实现 | 学习到的技能与记忆的时间线 |
| `journey list` | `journey list` | core | P1 | ⬜ 待实现 | 列出节点 id |
| `journey delete` | `journey delete` | core | P1 | ⬜ 待实现 | 按节点 id 删除技能或记忆 |
| `journey edit` | `journey edit` | core | P1 | ⬜ 待实现 | 用编辑器修改节点 |
| `pets` | `pets` | ext | P2 | ⬜ 待实现 | 浏览 / 安装 / 选择 petdex 宠物 |
| `pets list` | `pets list` | core | P1 | ⬜ 待实现 | 浏览 petdex 画廊 |
| `pets install` | `pets install` | core | P1 | ⬜ 待实现 | 从画廊安装宠物 |
| `pets select` | `pets select` | core | P1 | ⬜ 待实现 | 设置当前宠物 |
| `pets show` | `pets show` | core | P1 | ⬜ 待实现 | 在终端播放当前宠物 |
| `pets off` | `pets off` | core | P1 | ⬜ 待实现 | 关闭宠物显示 |
| `pets scale` | `pets scale` | core | P1 | ⬜ 待实现 | 调整宠物尺寸 |
| `pets remove` | `pets remove` | core | P1 | ⬜ 待实现 | 删除已安装宠物 |
| `pets doctor` | `pets doctor` | core | P1 | ⬜ 待实现 | 检查宠物配置与终端图形支持 |
| `mcp` | `mcp` | ext | P0 | ⬜ 待实现 | MCP 服务器管理（也可把 Agent 暴露为 MCP） |
| `mcp serve` | `mcp serve` | core | P1 | ⬜ 待实现 | 以 MCP 服务运行 |
| `mcp add` | `mcp add` | core | P1 | ⬜ 待实现 | 添加 MCP 服务器（先发现后安装） |
| `mcp remove` | `mcp remove` | core | P1 | ⬜ 待实现 | 移除 MCP 服务器 |
| `mcp list` | `mcp list` | core | P1 | ⬜ 待实现 | 列出已配置 MCP 服务器 |
| `mcp test` | `mcp test` | core | P1 | ⬜ 待实现 | 测试 MCP 服务器连接 |
| `mcp configure` | `mcp configure` | core | P1 | ⬜ 待实现 | 勾选工具子集 |
| `mcp login` | `mcp login` | core | P1 | ⬜ 待实现 | 强制重新鉴权 OAuth MCP |
| `mcp reauth` | `mcp reauth` | core | P1 | ⬜ 待实现 | 重新鉴权一个或全部 OAuth MCP |
| `mcp picker` | `mcp picker` | core | P1 | ⬜ 待实现 | 交互式目录选择器 |
| `mcp catalog` | `mcp catalog` | core | P1 | ⬜ 待实现 | 列出可一键安装的官方 MCP |
| `mcp install` | `mcp install` | core | P1 | ⬜ 待实现 | 按名称安装目录中的 MCP |
| `__run-script` | `__run-script` | internal | P2 | ⬜ 待实现 | 内部：执行脚本 |
| `__slash-worker` | `__slash-worker` | internal | P2 | ⬜ 待实现 | 内部：斜杠命令 worker |
| `__compute-host` | `__compute-host` | internal | P2 | ⬜ 待实现 | 内部：计算宿主 |
| `__gateway-restart-watch` | `__gateway-restart-watch` | internal | P2 | ⬜ 待实现 | 内部：网关重启看护 |
| `__update-gateway-helper` | `__update-gateway-helper` | internal | P2 | ⬜ 待实现 | 内部：更新网关辅助进程 |
