# 03 · Hermes 持久化与会话层完整清单

> 目标：为 Orbit 的持久化层提供「Hermes 到底存了什么、怎么存、怎么恢复」的可执行对照表。
> 取证对象：Hermes Agent CN 发行版 `0.21.0-cn.18`，home = `E:\Hermes Agent CN Desktop\data\hermes-home`（**只读**）。
> 采集方式：`python 3.13.12 / sqlite 3.51.1`，URI `file:...?mode=ro&immutable=1`（另做了一次 WAL 感知只读对照，见 §0.2）；CLI 仅执行 `--help`；源码只读 `_internal\tools\*.py`。
> 采集时间：本机数据截至 `sessions.started_at` 最大值 = 2026-10-27 19:27:37（本地时间）。
> 未修改/删除 hermes-home 与 Hermes 目录下任何文件；未 copy 大库、未 VACUUM、未安装依赖。

---

## 0. 证据基线与快照事实

### 0.1 六个 SQLite 库（全部只读打开成功）

| 文件 | 字节 | 表数 | journal | 备注 |
|---|---:|---:|---|---|
| `state.db` | 182,902,784（174.4 MB） | 40 | delete（主库）+ `-wal` 4,185,952 B / `-shm` 32,768 B | 会话/消息/网关/投递/委派/用量/系统提示词 |
| `kanban.db` | 118,784 | 8 | delete | 看板任务内核；本机 0 行 |
| `projects.db` | 45,056 | 4 | delete | 项目/工作区索引；本机 0 行 |
| `verification_evidence.db` | 147,456 | 4 | delete | 验证证据账本；17 条 state + 8 条 event |
| `desktop-ui.sqlite` | 335,872 | 5 | delete | 桌面 UI 侧数据；336 条 turn_stats |
| `cron\executions.db` | 20,480 | 1 | delete | cron 执行账本；0 行 |

**合计 62 张表**（含 FTS5 影子表与 `sqlite_sequence`）。

### 0.2 两种只读姿势的一致性校验

| 打开方式 | 结果 |
|---|---|
| `mode=ro&immutable=1`（忽略 WAL） | 40 表 / `schema_version=86` / 各表行数与下表一致 |
| `mode=ro`（读 WAL，`-shm` 已存在） | 40 表 / `schema_version=86` / **行数完全相同，表集合无差异** |

结论：`state.db-wal` 里没有未被 checkpoint 的额外 schema 或行数差异，下文行数可信。
（若需更严格：WAL 文件存在说明最后写入可能未落主库，行数级一致性已足够本清单用途。）

**快照是移动靶**：采集时 Hermes 桌面端与运行时正在运行（`hermes-agent-cn-desktop` pid 10904、`hermes-agent-cn-runtime-win32-x64` pid 9472），`state.db-wal` 与 `kanban.db-shm` 的 mtime 持续变化（23:05 仍在刷新）——**这是活动进程在写，不是本次取证造成**。所有读取均为 `mode=ro`（`immutable=1` 完全不触碰 `-shm`），未对任何库产生写入。因此上文行数代表 2026-10-27 23:05 前后的瞬时值。

### 0.3 state.db 元信息

| 项 | 值 |
|---|---|
| `PRAGMA journal_mode` | `delete`（主库；运行期由 `SessionDB` 决定，`async_delegation` 明确要求「保留既有 journal 模式」） |
| `page_size` / `page_count` / `freelist_count` | 4096 / 44654 / 170 |
| `encoding` | UTF-8 |
| `user_version` / `application_id` | 0 / 0（迁移版本走自有 `schema_version` 表） |
| `schema_version` 表内容 | `version = 26` |
| `state_meta` 内容 | `fts_optimize_available = 1` |
| 触发器 | 6 个，全部是 `messages` → FTS5 的同步触发器（`messages_fts_insert/delete/update`、`messages_fts_trigram_insert/delete/update`） |

---

## 1. state.db 全表清单（40 张）

### 1.1 会话 / 消息 / 事件族（核心 5 张）

| 表 | 行数 | 用途（中文） |
|---|---:|---|
| `sessions` | **100** | 会话主表：一条 = 一个可续接的会话（CLI/桌面/网关/子代理/cron 全在同一平面），含来源、路由身份、模型与计费、父子谱系、压缩状态、归档/隐藏/置顶、最后活动时间等 56 列 |
| `messages` | **14,595** | 消息与工具轨迹的追加日志：`role ∈ {user, assistant, tool, session_meta}`，含工具调用 JSON、推理内容、token 数、`active/compacted` 归档标志，是断线重连/回放/审计的唯一真相源 |
| `system_prompts` | 16 | 系统提示词内容寻址去重表（`hash` 主键 + 全文），`sessions.system_prompt_hash` 外挂引用，避免每个会话重复存一份大 prompt |
| `session_model_usage` | 115 | 按「会话 × 模型 × 计费渠道 × 任务类型」聚合的用量/成本账（api 调用数、四类 token、估算/实际 USD、成本来源），PK 六元组 |
| `state_meta` | 1 | 库级键值杂项（本机只有 `fts_optimize_available`），供运行时能力探测 |

`sessions` 关键列（全文见 §1.6 DDL）：

- 身份/路由：`id`、`source`、`user_id`、`session_key`、`chat_id`、`chat_type`、`thread_id`、`display_name`、`origin_json`
- 生命周期：`started_at`、`ended_at`、`end_reason`、`expiry_finalized`、`archived`、`hidden`、`pinned`、`last_read_at`
- 谱系：`parent_session_id`（自引用 FK）、`rewind_count`
- 压缩：`compression_failure_cooldown_until`、`compression_failure_error`、`compression_fallback_streak`、`compression_ineffective_count`
- 模型/计费：`model`、`model_config`、`system_prompt`、`system_prompt_hash`、`billing_*`、`estimated_cost_usd`、`actual_cost_usd`、`cost_status`、`cost_source`、`pricing_version`
- 工作区：`cwd`、`git_branch`、`git_repo_root`、`git_metadata_generation`
- 交付/交接：`handoff_state`、`handoff_platform`、`handoff_error`
- UI：`title`、`title_source`、`last_activity_at`、`last_activity_description`、`last_activity_provenance`、`profile_name`
- 计数（反规范化）：`message_count`、`tool_call_count`、`input_tokens`、`output_tokens`、`cache_read_tokens`、`cache_write_tokens`、`reasoning_tokens`、`api_call_count`

`sessions` 索引 10 个（含 1 个唯一）：

```sql
CREATE UNIQUE INDEX idx_sessions_title_unique ON sessions(title) WHERE title IS NOT NULL;  -- 标题全局唯一（部分索引）
CREATE INDEX idx_sessions_started            ON sessions(started_at DESC);
CREATE INDEX idx_sessions_source             ON sessions(source);
CREATE INDEX idx_sessions_source_id          ON sessions(source, id);
CREATE INDEX idx_sessions_parent             ON sessions(parent_session_id);
CREATE INDEX idx_sessions_session_key        ON sessions(session_key, started_at DESC);
CREATE INDEX idx_sessions_gateway_peer       ON sessions(source, user_id, chat_id, chat_type, thread_id, started_at DESC);
CREATE INDEX idx_sessions_handoff_state      ON sessions(handoff_state, started_at);
CREATE INDEX idx_sessions_system_prompt_hash ON sessions(system_prompt_hash);
```

`messages` 索引 6 个（含 3 个部分索引）：

```sql
CREATE INDEX idx_messages_session            ON messages(session_id, timestamp);
CREATE INDEX idx_messages_session_id         ON messages(session_id, id);            -- 增量游标
CREATE INDEX idx_messages_session_active     ON messages(session_id, active, timestamp);
CREATE INDEX idx_messages_active_null        ON messages(active) WHERE active IS NULL;  -- 历史 NULL 修复探测
CREATE INDEX idx_messages_platform_msg_id    ON messages(session_id, platform_message_id) WHERE platform_message_id IS NOT NULL;
CREATE INDEX idx_messages_assistant_calls_by_session ON messages(session_id) WHERE role='assistant' AND tool_calls IS NOT NULL;
```

### 1.2 全文检索族（12 张 FTS5 影子表）

| 表 | 行数 | 用途（中文） |
|---|---:|---|
| `messages_fts` | 14,595 | FTS5 虚表（分词索引），`session_search` 的召回入口 |
| `messages_fts_config` / `_content` / `_data` / `_docsize` / `_idx` | 2 / 14,595 / 2,332 / 14,595 / 1,925 | FTS5 内部影子表：分词配置、原文副本、倒排段数据、文档尺寸、段索引 |
| `messages_fts_trigram` | 14,595 | 第二套 FTS5 **trigram** 索引（子串/中文友好匹配） |
| `messages_fts_trigram_config` / `_content` / `_data` / `_docsize` / `_idx` | 2 / 14,595 / 15,248 / 14,595 / 10,117 | trigram 索引的影子表 |

同步机制（6 个触发器，`AFTER INSERT/DELETE/UPDATE OF content, tool_name, tool_calls ON messages`）意味着：**消息表是唯一写入点，检索索引由库内触发器自动派生**，不会出现「写了消息忘了更新索引」的不一致。`hermes sessions optimize` 会合并 FTS 段 + VACUUM（见 §4.6）。

### 1.3 网关 / 会话平面 / 投递族（7 张）

| 表 | 行数 | 用途（中文） |
|---|---:|---|
| `gateway_routing` | **5** | 网关路由索引：`session_key`（`agent:main:<platform>:<chat_type>:<chat_id>[:thread]`）→ 会话记录 JSON；PK `(scope, session_key)`，`scope` 实测为 sessions 目录绝对路径，支持多 home/多 profile 隔离 |
| `gateway_heartbeats` | 5 | 网关后端存活心跳：`backend_id`=`<profile>@<host>:<pid>:<8hex>`、`pid`、`started_at`、`last_heartbeat`，用于发现僵死后端 |
| `gateway_hygiene_state` | 0 | 每 `session_key` 的连续失败计数（`failure_streak`），断线重连退避/熔断用 |
| `delivery_obligations` | **123** | 出站投递义务账本（至少一次语义）：`obligation_id` 主键、目标（platform/chat_id/thread_id）、`content`、`state`、`attempts`、`owner_pid`、`last_error`；本机 123 条全部 `feishu / delivered` |
| `async_delegations` | **1** | 异步委派持久账本：`delegation_id` 主键、`origin_session(_id)`、`parent_session_id`、`state`、`event_json`/`result_json`/`task_json`、`delivery_state`/`delivery_attempts`/`delivery_claim`、`owner_pid`/`owner_started_at` |
| `session_turn_leases` | 0 | 会话轮次租约（`conversation_id` PK、`holder`、`acquired_at`、`expires_at`）：同一会话同时只允许一个 turn 持有者 |
| `compression_locks` | 0 | 压缩互斥锁（`session_id` PK、`holder`、`expires_at` + `idx_compression_locks_expires`）：防止多进程对同一会话并发压缩 |

### 1.4 Hosted Room（多人/多机协作事件流，14 张，本机全空）

| 表 | 行数 | 用途（中文） |
|---|---:|---|
| `hosted_rooms` | 0 | 房间主表：成员 JSON、权威网关 id、`authority_epoch`、`next_seq`、`event_bytes`、`revision` |
| `hosted_room_events` | 0 | **带序号的追加事件流**：`PRIMARY KEY(room_id, seq)`、`UNIQUE(room_id, event_id)`、`kind`、`actor_json`、`payload_json` |
| `hosted_room_links` | 0 | 房间成员链接：`target_url`/`target_profile`/`grant`/`catalog_json`/`transport_security`/`status` |
| `hosted_room_peer_reservations` | 0 | 对端保留席位：`authority_gateway_id` + `authority_epoch` + `expires_at`/`revoked_at`（租约式成员权威） |
| `hosted_room_remote_runs` | 0 | 远端执行登记：`run_id`/`session_id`/`execution_generation`/`target_install_id`（幂等重放关键） |
| `hosted_room_policy_cursors` | 0 | 策略事件消费游标：`through_seq`、`stopped_through_seq` |
| `hosted_room_policy_events` | 0 | 策略讨论事件：`discussion_event_id`、`seq`、`event_json` |
| `hosted_room_policy_publications` | 0 | 策略发布幂等键：`(room_id, task_id, kind, execution_generation)` |
| `hosted_room_policy_threads` | 0 | 策略讨论线程状态：`latest_user_seq`、`completed` |
| `hosted_room_policy_transcript` | 0 | 线程内有序转录：`(room_id, thread_id, seq)` + `settled_seq` |
| `hosted_room_policy_transcript_state` | 0 | 转录 schema 版本位（`schema_version`） |
| `hosted_room_policy_watermarks` | 0 | 每成员已读水位：`seen_through_seq` |
| `hosted_room_retired_ids` | 0 | 已退役房间 id 墓碑（`retired_at`） |
| `hosted_room_revoked_grants` | 0 | 授权撤销窗口：`revoked_before`、`expires_at` |

> 这一族是 Hermes 里**唯一真正「事件溯源」形态**的存储：序号自增 + 唯一事件 id + 消费游标 + 水位 + 墓碑。Orbit 若要做可回放事件流，这份 schema 是最直接参照。

### 1.5 元表（2 张）

| 表 | 行数 | 用途（中文） |
|---|---:|---|
| `schema_version` | 1（`26`） | 单行迁移版本号（Hermes 自己的迁移器读取，与 SQLite `user_version` 无关） |
| `sqlite_sequence` | 1 | AUTOINCREMENT 分配器状态（`messages` 用；`max(id)=14597` 而行数 14,595 → 有 2 个 id 空洞，说明删过行但 id 不复用，**可安全用作回放游标**） |

### 1.6 关键表完整 DDL（原样取自 `sqlite_master`）

```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL, user_id TEXT, session_key TEXT, chat_id TEXT, chat_type TEXT,
    thread_id TEXT, display_name TEXT, origin_json TEXT, expiry_finalized INTEGER DEFAULT 0,
    model TEXT, model_config TEXT, system_prompt TEXT, parent_session_id TEXT,
    started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
    message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
    reasoning_tokens INTEGER DEFAULT 0,
    cwd TEXT, git_branch TEXT, git_repo_root TEXT,
    billing_provider TEXT, billing_base_url TEXT, billing_mode TEXT,
    estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT, cost_source TEXT,
    pricing_version TEXT, title TEXT, api_call_count INTEGER DEFAULT 0,
    handoff_state TEXT, handoff_platform TEXT, handoff_error TEXT,
    compression_failure_cooldown_until REAL, compression_failure_error TEXT,
    compression_fallback_streak INTEGER NOT NULL DEFAULT 0,
    profile_name TEXT, rewind_count INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
    "system_prompt_hash" TEXT, "last_activity_at" REAL, "last_activity_description" TEXT,
    "last_activity_provenance" TEXT, "compression_ineffective_count" INTEGER NOT NULL DEFAULT 0,
    "pinned" INTEGER NOT NULL DEFAULT 0, "last_read_at" REAL,
    "git_metadata_generation" INTEGER NOT NULL DEFAULT 0, "title_source" TEXT,
    "hidden" INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
);

CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role TEXT NOT NULL, content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
    effect_disposition TEXT, timestamp REAL NOT NULL, token_count INTEGER, finish_reason TEXT,
    reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT,
    codex_reasoning_items TEXT, codex_message_items TEXT,
    platform_message_id TEXT, observed INTEGER DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1, compacted INTEGER NOT NULL DEFAULT 0, api_content TEXT,
    "display_kind" TEXT, "display_metadata" TEXT, "_compressed_summary" INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE gateway_routing (
    scope TEXT NOT NULL DEFAULT '', session_key TEXT NOT NULL, entry_json TEXT NOT NULL,
    updated_at REAL NOT NULL, PRIMARY KEY (scope, session_key)
);

CREATE TABLE delivery_obligations (
    obligation_id TEXT PRIMARY KEY, session_key TEXT NOT NULL, platform TEXT NOT NULL,
    chat_id TEXT NOT NULL, thread_id TEXT, content TEXT NOT NULL, state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, updated_at REAL NOT NULL,
    owner_pid INTEGER, owner_started_at INTEGER, last_error TEXT
);

CREATE TABLE session_model_usage (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    model TEXT NOT NULL, billing_provider TEXT NOT NULL DEFAULT '',
    billing_base_url TEXT NOT NULL DEFAULT '', billing_mode TEXT NOT NULL DEFAULT '',
    task TEXT NOT NULL DEFAULT '', api_call_count INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0, output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0, cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0, estimated_cost_usd REAL NOT NULL DEFAULT 0,
    actual_cost_usd REAL NOT NULL DEFAULT 0, cost_status TEXT, cost_source TEXT,
    first_seen REAL, last_seen REAL,
    PRIMARY KEY (session_id, model, billing_provider, billing_base_url, billing_mode, task)
);
```

### 1.7 state.db 里的实测分布（可用于校验 Orbit 的对照数据）

| 维度 | 实测值 |
|---|---|
| `sessions.source` | `desktop` 75 / `openclaw` 17 / `feishu` 5 / `subagent` 3 |
| `sessions.end_reason` | `startup_orphan_reap` **67** / `NULL` 24 / `agent_close` 6 / `ws_orphan_reap` 3 |
| `sessions.parent_session_id` 非空 | 6（全部是 `subagent`/`desktop` 的委派子会话，非压缩谱系） |
| `sessions.archived / hidden / pinned / expiry_finalized / rewind_count` | 全 0 |
| `sessions.title_source` | `NULL` 90 / `user` 10；`distinct system_prompt_hash` = 16（= `system_prompts` 行数） |
| `messages.role` | `tool` 7,267 / `assistant` 6,708 / `user` 615 / `session_meta` 5 |
| `messages.finish_reason` | `NULL` 8,339 / `tool_calls` 5,743 / `stop` 434 / **`verification_required` 79** |
| `messages.active/compacted` | `(1,0)` 12,494 / **`(0,1)` 2,101**（压缩归档，内容保留但移出活动上下文） |
| `messages.observed` | `0` 13,657 / `1` 938 |
| `messages.display_kind` / `effect_disposition` | 全 `NULL`（列已预留，本机未启用） |
| `session_model_usage` 汇总 | 5,577 次 API 调用 / 输入 15,198,049 tok / 输出 3,517,611 tok / 估算 $4.069 |
| `session_model_usage.task` | `''` 82 / **`approval` 18** / `vision` 5 / `title_generation` 5 / `background_review` 3 |

---

## 2. 维度归类：哪些东西**不在** state.db 里（关键结论）

任务要求按「会话/消息/事件、工具调用、技能、记忆、cron、审批、usage/insights、checkpoint、pairing、webhook」分类。实测结论：

| 维度 | 存放位置 | 证据 |
|---|---|---|
| 会话 / 消息 / 事件 | `state.db: sessions, messages`（+ FTS 影子表） | §1 |
| 工具调用 | `state.db: messages.tool_calls / tool_call_id / tool_name / finish_reason='tool_calls'`（**无独立表**）；进程型工具另落 `processes.json` | `messages` DDL；`tools\process_registry.py:69 CHECKPOINT_PATH = get_hermes_home()/"processes.json"` |
| 技能 | **文件系统**：`skills/<category>/<name>/SKILL.md` + 4 个 sidecar（`.usage.json` / `.curator_ledger.jsonl` / `.curator_state` / `.bundled_manifest`）；state.db 无技能表 | `tools\skill_usage.py:3,85`（`~/.hermes/skills/.usage.json`） |
| 记忆 | **文件系统**：`memories/MEMORY.md`、`memories/USER.md`（+ `.lock`）；state.db 无记忆表 | `tools\memory_tool.py:6-8,66-67,265-266` |
| cron | `cron\` 目录：`executions.db`（执行账本，1 表）+ `output/` + `ticker_heartbeat`/`ticker_last_success` + `.jobs.lock`/`.tick.lock`；任务定义走 `cron/jobs.json`（本机不存在 → 0 个任务） | 目录实测 + `executions` DDL（`status CHECK IN ('claimed','running','completed','failed','unknown')`） |
| 审批 | state.db 无审批表；审批是**运行时状态 + 用量记账**（`session_model_usage.task='approval'` 18 行）；策略开关在 `config.yaml: approvals.mode`（本机 `off`） | §1.7；`config.yaml` |
| usage / insights | `state.db: session_model_usage`（后端权威）+ `desktop-ui.sqlite: turn_stats`（336 行，逐轮 TTFT/耗时/token/成本） | §1、§3.4 |
| checkpoint | **不在 SQLite**：影子 git 仓库 `<HERMES_HOME>/checkpoints/store/`（本机该目录**不存在** → 0 个 checkpoint） | `tools\checkpoint_manager.py:16-25,75,78` |
| pairing | `pairing\` 与 `platforms\pairing\` 目录（本机均为空）；state.db 无 pairing 表 | 目录实测 |
| webhook | state.db 无 webhook 表；能力在桌面/网关运行时（`hermes_cli\web_dist` 有 `WebhooksPage`），本机未落库 | `_internal\hermes_cli\web_dist\assets\WebhooksPage-C68NN31t.js` |

> 给 Orbit 的直接教训：Hermes 把「**会话/消息**」放进关系库（强一致 + 可查询 + 可回放），把「**技能/记忆/checkpoint**」放进文件系统（人类可编辑 + git 可版本化），把「**执行账本**」拆成独立小库（cron/kanban/verification 各自一把锁，互不阻塞主库写）。这是一个刻意的分层，不是遗漏。

---

## 3. 其他四个库 + cron 账本

### 3.1 `kanban.db`（8 表，本机全 0 行）

| 表 | 用途（中文） |
|---|---|
| `tasks` | 卡片主表（35 列）：状态机、`assignee`、`priority`、工作区（`workspace_kind`/`workspace_path`/`branch_name`/`project_id`）、**`idempotency_key`**、`claim_lock`/`claim_expires`、`consecutive_failures` 熔断计数、`block_kind`/`block_recurrences` 防循环、`goal_mode`/`goal_max_turns`、`session_id` 溯源、`skills`/`model_override` 覆盖 |
| `task_runs` | 每次执行尝试（`status: running/done/blocked/crashed/timed_out/failed/released`；`outcome`、`claim_lock`、`worker_pid`、`last_heartbeat_at`、`max_runtime_seconds`、`error`） |
| `task_events` | 卡片事件流（`task_id`、`run_id`、`kind`、`payload`、`created_at`），`idx_events_task`/`idx_events_run` |
| `task_comments` | 人类/代理评论 |
| `task_attachments` | 附件登记（`filename`/`stored_path`/`content_type`/`size`/`uploaded_by`） |
| `task_links` | 父子依赖（`parent_id, child_id` PK） |
| `kanban_notify_subs` | 通知订阅（`task_id`×`platform`×`chat_id`×`thread_id` + `last_event_id` 游标） |
| `sqlite_sequence` | 自增状态 |

索引：`idx_tasks_idempotency`、`idx_tasks_status`、`idx_tasks_assignee_status`、`idx_tasks_tenant`、`idx_tasks_session_id`、`idx_runs_status`、`idx_runs_task`、`idx_events_task`、`idx_events_run`、`idx_links_parent`、`idx_links_child`、`idx_comments_task`、`idx_attachments_task`、`idx_notify_task`。

### 3.2 `projects.db`（4 表，本机全 0 行）

| 表 | 用途（中文） |
|---|---|
| `projects` | 项目（= 命名工作区）：`slug` 唯一、`name`/`description`/`icon`/`color`、`board_slug`（关联看板）、`primary_path`、`archived` |
| `project_folders` | 项目包含的目录（`(project_id, path)` PK、`is_primary`、`label`、`added_at`），FK ON DELETE CASCADE |
| `discovered_repos` | 自动发现的 git 仓库（`root` PK、`label`、`last_seen`） |
| `project_meta` | 库级 KV |

### 3.3 `verification_evidence.db`（4 表，8 event + 17 state）

| 表 | 行数 | 用途（中文） |
|---|---:|---|
| `verification_events` | 8 | 「验证证据」追加账本：`session_id`、`cwd`、`root`、`command`、**`canonical_command`**、`kind`（本机全 `test`）、`scope`（`full`）、`status`（`passed`）、`exit_code`、`output_summary` |
| `verification_state` | 17 | 每 `(session_id, root)` 的验证水位：`last_event_id`、**`last_edit_at`**、`changed_paths_json`（改过哪些文件还没验） |
| `meta` | 1 | `schema_version = 1` |
| `sqlite_sequence` | 1 | 自增状态 |

索引：`idx_verification_events_session_root ON verification_events(session_id, root, id DESC)`。
用途实证：本机 8 条记录都是 `npm run test` 且 `exit_code=0 / passed`，即「**声称测试通过必须先有一条可查询的证据行**」的落库形式。`changed_paths_json` 让运行时能判断「自上次验证后是否又改过文件」。

### 3.4 `desktop-ui.sqlite`（5 表）

| 表 | 行数 | 用途（中文） |
|---|---:|---|
| `turn_stats` | **336** | 逐轮 UI 指标：`turn_index`、`client_message_id`、`backend_message_id`、`content_hash`、`ttft_ms`、`duration_ms`、`tokens_*`、`cache_read/write`、`context_used/max`、`api_calls`、`cost_usd`、`finish_reason`、`status` |
| `ui_kv` | 17 | 桌面端 KV（`hermes:gateway-session-map`、`hermes.active-session-id`、`hermes-cn-ui.workspacePath`、`hermes:model-usage-log`、遥测设备 id 等） |
| `session_ui_state` | 0 | 会话的 UI 覆盖层（`title_override`、`archived`、`pinned`、`tags_json`、`workspace_path`）——**与后端 `sessions` 分离，避免 UI 偏好污染会话真相** |
| `ui_events` | 0 | 前端遥测事件流（`ts`、`event_name`、`source`、`props_json`、`app_version`） |
| `ui_schema_migrations` | 1 | UI 库迁移版本（`version=1`） |

索引：`idx_turn_stats_session`、`idx_turn_stats_hash`、`idx_ui_events_ts`、`idx_ui_events_name`。

### 3.5 `cron\executions.db`（1 表，0 行）

```sql
CREATE TABLE executions (
  id TEXT PRIMARY KEY, job_id TEXT NOT NULL, source TEXT NOT NULL,
  process_id TEXT NOT NULL, pid INTEGER NOT NULL, process_started_at INTEGER,
  status TEXT NOT NULL CHECK(status IN ('claimed','running','completed','failed','unknown')),
  claimed_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, error TEXT
);
-- idx_executions_status_claimed(status, claimed_at, id) / idx_executions_job_claimed(job_id, claimed_at, id)
```
用途：cron 单次执行的**抢占式账本**（`claimed` → `running` → 终态），配合 `pid` + `process_started_at` 识别僵尸执行。

---

## 4. 目录即数据：hermes-home 逐项清单

> 标记说明：`[实测]` = 本次直接列出文件/读取内容；`[推断]` = 依据源码或文件名推断，未逐字节验证。

### 4.1 根目录文件

| 路径 | 大小 | 存什么 |
|---|---:|---|
| `state.db` (+`-wal` 4.0MB/`-shm`) | 174.4 MB | §1 的会话/消息/网关/投递/委派/用量主库 |
| `kanban.db` (+`-wal` 0B/`-shm`) | 118 KB | §3.1 |
| `projects.db` | 45 KB | §3.2 |
| `verification_evidence.db` | 147 KB | §3.3 |
| `desktop-ui.sqlite` | 335 KB | §3.4 |
| `config.yaml` | 4.1 KB | 主配置：`model`/`providers`（含明文 api_key，**勿外传**）、`auxiliary.vision`、`stt`、`approvals.mode='off'`、`onboarding`、`desktop.models.provider_order`、`plugins.enabled`、`_config_version: 33` |
| `config.yaml.bak_*` / `.corrupt.*.bak` | 1.5/1.8 KB | 配置历史备份与损坏快照 |
| `SOUL.md` | 540 B | 人格/行为基底提示词（人类可编辑） |
| `auth.json` | 678 B | 凭证：`version`/`providers`/`credential_pool`/`updated_at`（OAuth/token 池） |
| `auth.json.corrupt` / `auth.lock` | 1.4 KB / 1 B | 损坏快照 + 互斥锁 |
| `.env` / `.env.bak-feishu*` | 723 B | 环境变量（渠道密钥等） |
| `channel_directory.json` | 70 B | 已连接渠道目录；本机 `platforms: {}`（空） |
| `feishu_seen_message_ids.json` | 19 B | 飞书入站去重集合；本机 `{"message_ids": {}}` |
| `processes.json` | 2.7 KB | **后台进程登记表**（3 条）：`session_id`(`proc_<12hex>`)、`command`、`pid`、`pid_scope`、`host_start_time`、`cwd`、`task_id`、`owner_task_id`、`session_key`、watcher 信息、`parent_session_id` |
| `spawn-ledger.json` | 454 B | 运行时 spawn 台账：`pid`、`create_time`、`purpose`（如 `dashboard`）、`install`、`spawner_pid`、`argv`、`host`、`port` |
| `install_id` / `.install_id.lock` | 34 B / 1 B | 安装实例 id（多实例/许可识别） |
| `.update_check` | 62 B | 更新检查缓存：`{"ts":..., "behind":0, "rev":null, "ver":"0.19.0"}` |
| `models_dev_cache.json` / `.etag` | 5.9 MB | models.dev 模型目录缓存 |
| `provider_models_cache.json` / `ollama_cloud_models_cache.json` | 512/449 B | 各 provider 模型清单缓存 |
| `.skills_prompt_snapshot.json` | 112 KB | 技能提示词快照：`{version, manifest, skills, category_descriptions}`（决定本轮注入哪些技能描述） |
| `gateway-starts.log` | 716 B | 网关启动流水（根目录级别的轻量日志） |

### 4.2 `sessions/`（195 个子目录 + 5 个文件）

| 内容 | 格式 | 存什么 |
|---|---|---|
| `sessions.json` | JSON | **遗留镜像**：文件自带 README 明确「这不是会话列表；所有会话都在 state.db」。映射 `session_key → {session_id, created_at, updated_at, display_name, platform, chat_type, tokens..., suspended, resume_pending}`。可用 `gateway.write_sessions_json: false` 关闭 |
| `request_dump_<ts>_<sess>_<ts>_<hex>.json` | JSON | 请求体快照（4 个，最大 2.1 MB）：键为 `timestamp`/`session_id`/`reason`/`request`/`error`，用于复现 API 报错 |
| `<12-hex>/terminal_output_original.txt` | 纯文本 | **终端输出 spill**：过滤 ANSI 后、去重/截断**之前**的原始输出（195 个目录，样本 `09570aa5129b` 内容为测试输出行）。命名来源：`tools\terminal_post_process.py:106-125`，`hermes_home/"sessions"/uuid4().hex[:12]/terminal_output_original.txt` |

> 注意：这些目录名是**每次终端调用的随机 12-hex**，不是会话 id —— 不要拿它当会话主键。

### 4.3 `cron/`

| 路径 | 存什么 |
|---|---|
| `executions.db` | §3.5 执行账本 |
| `output/` | 定时任务输出归档（`tools\cronjob_tools.py:1146` 提到 `~/.hermes/cron/output/`）；本机空 |
| `ticker_heartbeat` / `ticker_last_success` | 调度 ticker 心跳与最后成功时间（epoch 浮点，如 `1793113217.51`）`[实测]` |
| `.jobs.lock` / `.tick.lock` | 任务定义写入锁 / tick 互斥锁（0 字节）`[实测]` |
| `jobs.json` | 任务定义文件 `[推断]`：本机**不存在**（与 `executions` 0 行、`output/` 空一致 → 当前无 cron 任务）；`tools\cronjob_tools.py` 从 `cron.scheduler` 导入解析器，未解包到该模块源码 |

### 4.4 `memories/`

| 文件 | 大小 | 存什么 |
|---|---:|---|
| `MEMORY.md` | 3.5 KB | 代理自身笔记（环境事实、项目约定）——人类可编辑的纯文本，注入系统提示 |
| `USER.md` | 3.2 KB | 关于用户的长期事实（偏好、沟通风格） |
| `MEMORY.md.lock` / `USER.md.lock` | 0 B | 写互斥锁（多进程安全） |

证据：`tools\memory_tool.py:6-8`（两文件职责）、`:66-67`（`get_hermes_home()/"memories"`）、`:265-266`（读入）、`:361-362`（写回路径）。

### 4.5 `skills/`（36 个分类目录 + `.archive`/`.hub` + 5 个 sidecar 文件）

| 内容 | 存什么 |
|---|---|
| `<category>/<skill>/SKILL.md` | 技能正文（样本：`devops\local-fullstack-verification\SKILL.md` 10,327 B） |
| `.usage.json` | 92 条技能使用元数据：`use_count`/`view_count`/`patch_count`/`created_by`(agent/user)/`state`/`pinned`/`archived_at`/`last_used_at`/`last_patched_at` |
| `.curator_ledger.jsonl` | 技能变更审计（append-only JSONL）：`{id, ts, actor, action, skill, evidence:{session_id}, before:[{path,sha256}], after:[...]}` |
| `.curator_state` | 策展器运行状态：`last_run_at`、`run_count`、`paused`、`last_run_summary` |
| `.bundled_manifest` | 内置技能清单 `<name>:<md5>`，用于识别「用户改过的内置技能」 |
| `.usage.json.lock` | 1 B 写锁 |
| `.archive/` | 归档技能（样本：`feishu-gateway-troubleshooting` 等） |
| `.hub/` | 技能市场：`lock.json`、`taps.json`、`audit.log`、`index-cache/`、`quarantine/`（隔离区） |

### 4.6 其余目录

| 目录 | 内容（实测样本） | 存什么 |
|---|---|---|
| `logs/` | `agent.log` 2.4 MB、`agent.log.1` 5.2 MB、`errors.log` 985 KB、`gateway.log` 229 KB、`gui.log` 51 KB、`desktop-gateway-restart.log` 62 KB、`gateway-restart.log`、`gateway-exit-diag.log`、`curator/`；每类日志配 `.__<name>.lock` | 运行日志（轮转 + 进程级锁） |
| `backups/` | `state.db.pre-openclaw-migrate.20260808_162011`（2.1 MB）、`MEMORY.md.pre-openclaw-migrate.20260808_162011` | 迁移前的一次性备份（**注意：这是裸 db 拷贝，非 `hermes backup` 的 zip**） |
| `cache/` | 文件：`openrouter_model_metadata.json` 378 KB、`tool_index.json` 11 KB、`model_catalog.json` 5 KB、`schema_columns.json` 4.7 KB；子目录：`terminal/`（`hermes-snap-<12hex>.sh` shell 快照）、`terminal-output/`、`delegation/`（`subagent-summary-1-<ts>.txt` + `live/`）、`screenshots/`（`browser_screenshot_<32hex>.png`）、`images/`、`documents/`、`web/`、`vision/`、`tiktoken/`、`huggingface/`、`ms-playwright/`、`browser-use/`、`blocked-scripts/` | 可重建的缓存（删了不丢数据） |
| `attachments/` | `零件5（一实体）.SLDPRT`（6.2 MB） | 用户上传附件原文件 |
| `images/` | `upload_20260908_024806_1.png`（53 KB） | 上传图片原文件 |
| `audio_cache/` / `image_cache/` | 空 | TTS/图像生成缓存 |
| `pending_messages/` | 空 | 待投递消息暂存（配合 `delivery_obligations`）`[推断]` |
| `pairing/` + `platforms/pairing/` | 均空 | 设备/渠道配对状态 `[推断]` |
| `hooks/` | 空 | 钩子脚本目录（`hook_output_spill` 的 `hooks.output_spill` 配置读取处）`[推断]` |
| `lsp/` | `package.json`、`package-lock.json`、`bin/pyright-langserver`、`bin/typescript-language-server`、`node_modules/` | LSP 语言服务器安装位（符号级代码智能） |
| `runtime/` | `active_sessions.json`（581 B）+ `active_sessions.lock` | **活跃会话租约表**：每条含 `lease_id`(32hex)、`session_id`、`pid`、`process_start_time`、`surface`(如 `desktop`)、`track_liveness`、`metadata.live_session_id`(8hex)、`started_at`、`updated_at` |
| `state/` | `gateway.heartbeat`（`{pid, updated_at, monotonic, start_time}`）、`gateway.lifecycle.json`（`{phase:"running", pid, start_time, started_at}`） | 网关进程级存活/生命周期标记 |
| `gateway/` | `restart_loop.json` → `{"boots":[1786410179.95]}` | 网关重启循环检测（防崩溃重启风暴） |
| `desktop/` | 空 | 桌面端落盘位（本机 UI 状态实际在 `desktop-ui.sqlite`） |
| `kanban/` | `.dispatcher.lock`（2 B） | 看板调度器单实例锁 |
| `platforms/` | `pairing/`（空） | 平台侧状态（配对等） |
| `migration/` | `openclaw/20260808T162040/{MIGRATION_NOTES.md, report.json(21 KB), summary.md, backups/, overflow/}` | 从 openclaw 迁入的一次性迁移报告与溢出文件 |
| `scripts/` | 空 | 用户脚本（`cronjob_tools.py:743` 的 `get_hermes_home()/"scripts"`，cron 脚本必须在此目录内） |
| `.curator_backups/blobs/` | 内容寻址 blob（sha256 文件名，如 `48daa0cb...` 10 KB） | 技能策展器的 before/after 内容备份（与 `.curator_ledger.jsonl` 的 sha256 对应） |
| `.env.backups/` | `im-onboarding-<ts>.bak`（10 个） | `.env` 历史快照 |
| `.port-locks/` | `9120.lock`、`8645.lock`、`8644.lock`（0 B） | 端口占用锁（多实例防抢端口） |
| `lark-override/` | `lark_oapi/`、`lark_oapi-1.7.2.dist-info/` | 飞书 SDK 版本覆盖（旁路加载） |
| `plugins/` | `superpowers/`（含 `.agents`、`.claude-plugin`、`.codex-plugin`、`.cursor-plugin`、`.devin-plugin`、`.git` 等跨工具插件描述） | 插件根；`config.yaml: plugins.enabled: [superpowers/.hermes-plugin]` |
| `checkpoints/` | **不存在** | 影子 git checkpoint 库（源码定义位置见 §5.4）；本机 0 个 checkpoint |

---

## 5. 会话生命周期与恢复机制（CLI + 源码反推）

### 5.1 会话是一个「服务」，不是一次进程

- **统一平面**：`sessions.json` 文件内嵌 README 原文——"ALL sessions (CLI, TUI, and gateway) live in `~/.hermes/state.db` and are shown by `hermes sessions list` and `/sessions`. This is NOT the session list."
- **同表异源**：`sessions.source ∈ {desktop, openclaw, feishu, subagent}`（本机实测四种并存），`hermes sessions list` 一次列出全部。
- **路由身份与内容分离**：`gateway_routing(scope, session_key) → entry_json{session_id,...}`，`scope` = sessions 目录绝对路径 → 同一份 state.db 可被不同 home/profile 复用而不串号。
- **单写者保证**：`session_turn_leases(conversation_id PK, holder, expires_at)` 让同一会话的并发 turn 只有一个持有者；`compression_locks(session_id PK)` 单独保护压缩。
- **活跃租约**：`runtime/active_sessions.json` 记录 `lease_id / session_id / pid / process_start_time / surface / track_liveness` —— 桌面端、网关、子代理各自的存活凭据。

CLI 证据（`hermes sessions --help`）：
```
list / export / delete / prune / archive / optimize / clean-markers / optimize-storage /
repair / repair-routing / recover / stats / rename / pin / unpin / pinned /
retitle-skills / browse / import
```
`hermes sessions --help` 原文：*"View and manage the SQLite session store"*。

### 5.2 幂等（idempotency）——四道防线

| 层 | 机制 | 证据 |
|---|---|---|
| 会话消息 | `messages` 用 AUTOINCREMENT id + 部分索引 `idx_messages_platform_msg_id`（按 `session_id, platform_message_id`）做入站去重；id 空洞（max=14597 vs 14,595 行）证明 id 不复用 | §1.1、§1.5 |
| 委派 | `async_delegations.delegation_id` 主键 + `INSERT OR REPLACE`；`delivery_claim`/`delivery_claimed_at` 抢占式投递；`origin_session_id` 保存原始 API 会话 id，**使重启后恢复的完成事件仍可路由** | `tools\async_delegation.py:264-267`（INSERT OR REPLACE）、`:184-188`（注释："Without persisting it, completions recovered after a process restart are unroutable"） |
| 出站投递 | `delivery_obligations` 义务账本：`state` + `attempts` + `last_error` + `owner_pid`，至少一次语义 | §1.3（本机 123 条全 `delivered`） |
| 看板任务 | `tasks.idempotency_key` + `idx_tasks_idempotency`；`claim_lock`/`claim_expires` 抢占；`consecutive_failures` 熔断；`block_recurrences` 防 unblock 死循环 | §3.1 DDL 注释 |

### 5.3 级联中断与孤儿回收

- **进程树级联 kill**：`tools\process_registry.py:963-999` — Windows 用 `taskkill /PID <pid> /T /F`（`/T` = 连带子进程树），POSIX 走进程组 SIGTERM 级联；`:972` 明确"enumeration is best-effort and misses orphaned descendants"。
- **进程登记可恢复**：`processes.json`（`CHECKPOINT_PATH = get_hermes_home()/"processes.json"`，`process_registry.py:69`）存 `pid` + `host_start_time`（防 PID 复用误杀）+ `task_id`/`owner_task_id`/`session_key`/`parent_session_id`，重启后可对账。
- **会话侧孤儿收割**：实测 `sessions.end_reason` 分布 —— `startup_orphan_reap` **67**、`ws_orphan_reap` 3、`agent_close` 6、`NULL` 24。即：进程启动时把上次崩溃遗留的「未正常结束」会话批量标记为 `startup_orphan_reap`；WebSocket 断连遗留标记为 `ws_orphan_reap`。
- **网关僵尸探测**：`gateway_heartbeats.last_heartbeat`（5 条历史后端）+ `state/gateway.heartbeat` + `state/gateway.lifecycle.json{phase}`；`gateway_hygiene_state.failure_streak` 做退避；`gateway/restart_loop.json{boots:[...]}` 记录启动次数防重启风暴。
- **租约过期**：`session_turn_leases.expires_at` / `compression_locks.expires_at` / `async_delegations.owner_pid + owner_started_at`（配合 `_STALE_IDLE_SECONDS=450`、`_STALE_IN_TOOL_SECONDS=1200`、`_STALL_GRACE_SECONDS=120`，见 `async_delegation.py:114-117`）。

### 5.4 压缩产生谱系（父/子 session）

- **谱系载体**：`sessions.parent_session_id`（自引用 FK）+ `idx_sessions_parent`；压缩轮换时父会话 `end_reason='compression'`，子会话 `parent_session_id` 指向父。
- **源码证据**：`tools\session_search_tool.py:133-164` 的 `_resolve_to_parent` —— 沿 `parent_session_id` 上溯到谱系根，返回 `(root_id, has_compression_hop)`，其中 `has_compression_hop` 为真当且仅当链上某祖先 `end_reason == 'compression'`；注释原文区分「压缩分裂谱系」与「委派谱系」两种语义。
- **归档而非删除**：`messages.active=0 AND compacted=1` 表示该消息已被压缩摘要替代（本机 2,101 条）；`session_search_tool.py:242-246` 注释原文："Compaction archives are `active=0, compacted=1` — the content was ..."。
- **压缩失败也有状态**：`sessions.compression_failure_cooldown_until` / `compression_failure_error` / `compression_fallback_streak` / `compression_ineffective_count` + `compression_locks` 表。
- **本机实况（如实标注）**：`SELECT ... WHERE end_reason='compression'` 返回 **0 行**；6 条带 `parent_session_id` 的会话都是 `subagent`/`desktop` 的委派子会话。即：**谱系机制存在但本机从未触发压缩**。Orbit 若以本机数据做对照，需要自己造压缩场景。

### 5.5 Checkpoint：粒度与保留策略

源码（`tools\checkpoint_manager.py`）给出的完整定义：

| 项 | 值 | 出处 |
|---|---|---|
| 存储根 | `<HERMES_HOME>/checkpoints/` | `:16`、`:75 CHECKPOINT_BASE = get_hermes_home()/"checkpoints"` |
| 布局 | `store/`（单一共享 bare-ish git 仓库）、`store/HEAD|config|objects/`、`store/refs/hermes/<hash16>`（每项目一个分支尖）、`store/indexes/<hash16>`（每项目 index）、`store/projects/<hash16>.json`（`{workdir, created_at, last_touch}`）、`store/ledgers/`、`store/info/exclude`、`.last_prune`、`legacy-<ts>/`（v1 迁移归档） | `:16-25`、`:78-83` |
| 触发粒度 | **每对话轮次最多一次**，在 `write_file` / `patch` / 带破坏性参数的 `terminal` 之前 | `:5-8` |
| 单位 | 每个工作目录一个 `hash16` 项目，共享对象库（跨 worktree 去重） | `:27-39` |
| 保留 | `retention_days`（按 `last_touch` 过期）+ `max_total_size_mb`（超限从最旧开始丢）+ 孤儿清理（`workdir` 不存在）+ `git gc --prune=now` | `:44-48`、`:773-776` |
| 写入台账 | `_LEDGER_MAX_ENTRIES = 2000`（每项目保留最新 2000 条 agent-write 记录） | `:86` |
| 规模上限 | `_MAX_FILES = 50_000`（超大目录跳过快照） | `:155` |
| 排除项 | `node_modules/ dist/ build/ target/ .venv/ .git/ *.dll *.exe *.zip *.log .env*` 等 | `:88-149` |
| 对 LLM 透明 | 「This is NOT a tool — the LLM never sees it」，由 `checkpoints` 配置或 `--checkpoints` CLI 开关控制 | `:10-11` |
| CLI | `hermes checkpoints {status|list|prune|clear|clear-legacy}`；`status` = "Show total size, project count, and per-project breakdown" | `checkpoints --help` |
| 本机实况（如实标注） | `hermes-home\checkpoints\` **目录不存在**，`E:\Hermes Agent CN Desktop\data` 下也搜不到 `checkpoints` 目录 → **本机 0 个 checkpoint，无法提供实机样本**；机制描述全部来自源码与 CLI help | 目录实测 |

### 5.6 备份 / 导入 / 导出格式

| 命令 | 格式 | 内容 |
|---|---|---|
| `hermes backup` | zip（默认 `~/hermes-backup-<timestamp>.zip`） | 全量：config、skills、sessions、data；**排除 hermes-agent 代码库**；`-o` 指定输出、`-l` 打标签 |
| `hermes backup --quick` | zip（快照式） | 只含关键状态文件："config, state.db, .env, auth, cron"（help 原文） |
| `hermes import <zipfile>` | 从 zip 还原到 Hermes home | 恢复 config/skills/sessions/data；`--force/-f` 免确认覆盖 |
| `hermes sessions export` | **JSONL / Markdown / QMD** 三种 | 按会话导出可移植文本 |
| `hermes sessions import` | — | 导入 **Claude Code / Codex CLI** 的会话 |
| `hermes sessions recover` | 新库 | "Rebuild canonical session data into a separate clean database"（灾难恢复：从损坏库重建） |
| `hermes sessions repair` / `repair-routing` | 原地修 | 修 schema 畸形导致会话隐藏；重打丢失的网关路由身份 |
| `hermes sessions optimize` / `optimize-storage` | 原地 | FTS5 段合并 + VACUUM（不删数据）；迁移到紧凑 v23 索引布局 |
| `hermes dump [--show-keys]` | 纯文本 | 可贴到 Discord/GitHub 的配置摘要（`--show-keys` 显示密钥首尾 4 字符） |
| `hermes status [--all|--deep]` | 文本 | 组件状态；`--deep` 跑深检 |

---

## 6. 事件模型：消息表如何支撑「断线重连 / 回放 / 审计」

Hermes **没有**一张叫 `events` 的会话事件表。它用 `messages` 的追加日志 + 一组状态列 + 一族旁路账本来实现三种能力。以下全部给出列名证据。

### 6.1 断线重连（tail / resume）

| 需要的能力 | 提供它的列/索引 |
|---|---|
| 单调递增、不复用的偏移量 | `messages.id INTEGER PRIMARY KEY AUTOINCREMENT`；`sqlite_sequence`（max id 14,597 > 行数 14,595 → 有空洞，可安全当游标） |
| 按会话增量拉取 | `idx_messages_session_id ON messages(session_id, id)` |
| 按时间窗重放 | `idx_messages_session ON messages(session_id, timestamp)`；`timestamp REAL NOT NULL` |
| 只拉「当前活动上下文」 | `active INTEGER NOT NULL DEFAULT 1` + `idx_messages_session_active ON messages(session_id, active, timestamp)` |
| 入站消息去重（同一平台消息重复投递） | `platform_message_id TEXT` + `idx_messages_platform_msg_id ... WHERE platform_message_id IS NOT NULL` |
| 客户端/前端回执位 | `observed INTEGER DEFAULT 0`（本机 938 条=1）`[推断：是否已被上层消费]` |
| 会话级进度快照 | `sessions.message_count` / `tool_call_count` / `*_tokens` / `api_call_count`（反规范化，实测与 `COUNT(*)` 一致） |
| 断线时的「最后活动」 | `sessions.last_activity_at` / `last_activity_description` / `last_activity_provenance` / `last_read_at` |

### 6.2 回放（replay / 上下文重建）

| 需要的能力 | 提供它的列 |
|---|---|
| 完整对话骨架 | `role` + `content` + `timestamp` |
| 工具调用配对 | `tool_calls`（JSON，实测含 `id`/`call_id`/`response_item_id`/`type:"function"`/`function.name`）+ `tool_call_id` + `tool_name` |
| 结束原因（区分「说完」与「要调工具」） | `finish_reason ∈ {tool_calls, stop, verification_required, NULL}` |
| 思考轨迹（审计/复现必需） | `reasoning`、`reasoning_content`、`reasoning_details`、`codex_reasoning_items`、`codex_message_items` |
| 「模型实际看到的」vs「存档的」 | `api_content`（发给 API 的规范化内容）与 `content`（原始）分列 |
| 压缩后的可回放性 | `active=0, compacted=1` 归档 + `_compressed_summary` 标记 + `messages._compressed_summary INTEGER` |
| 系统提示词溯源 | `sessions.system_prompt_hash` → `system_prompts(hash, prompt)`（16 个不同 prompt 被 100 个会话共享） |
| UI 呈现扩展 | `display_kind` / `display_metadata`（本机全 NULL，已预留） |
| 压缩谱系回放 | `sessions.parent_session_id` + `end_reason='compression'` |

### 6.3 审计（谁在什么时候做了什么）

| 审计问题 | 证据列/表 |
|---|---|
| 这条消息属于哪个会话、哪个来源 | `messages.session_id` → `sessions.id`；`sessions.source/user_id/chat_id/thread_id/session_key` |
| 什么时候 | `messages.timestamp REAL`、`sessions.started_at/ended_at` |
| 花了多少 | `messages.token_count`；`sessions.*_tokens/*_cost`；`session_model_usage`（会话×模型×渠道×task 六元组 PK）；`desktop-ui.sqlite:turn_stats`（逐轮 TTFT/耗时/成本） |
| 工具副作用是否被处置 | `messages.effect_disposition`（本机全 NULL，已预留） |
| 有没有验证过 | `verification_evidence.db: verification_events(canonical_command, status, exit_code, output_summary)` + `verification_state(last_event_id, last_edit_at, changed_paths_json)`；`messages.finish_reason='verification_required'`（79 条） |
| 委派链 | `async_delegations(origin_session, parent_session_id, state, event_json, result_json, task_json, delivery_*)` |
| 出站是否送达 | `delivery_obligations(state, attempts, last_error, delivered_at)` |
| 看板任务全过程 | `kanban.db: task_events(kind, payload, run_id)` + `task_runs(status, outcome, error)` |
| 技能被谁改了什么 | `skills/.curator_ledger.jsonl`（before/after sha256）+ `.curator_backups/blobs/<sha256>` |
| 桌面遥测 | `desktop-ui.sqlite: ui_events(event_name, ts, props_json, app_version)` |
| 房间级事件流（可精确回放） | `hosted_room_events(room_id, seq CHECK>=1, event_id UNIQUE, kind, actor_json, payload_json)` + `hosted_room_policy_cursors(through_seq)` + `hosted_room_policy_watermarks(seen_through_seq)` |

### 6.4 事件模型的两种范式并存（Orbit 需二选一或分层）

| 范式 | Hermes 实现 | 特点 |
|---|---|---|
| **状态快照 + 追加消息**（主路径） | `sessions` + `messages`（无 seq 列，靠 AUTOINCREMENT id 当隐式 seq；无「事件类型」列，靠 `role`/`finish_reason`/`display_kind` 编码语义） | 写少、查快、易做 UI；回放需按 id 游标 + 标志位重建 |
| **纯事件溯源**（旁路，仅协作房间用） | `hosted_room_events` + `cursors` + `watermarks` + `retired_ids` 墓碑 + `execution_generation` 幂等 | 可精确重放到任意 seq；需要投影/游标维护成本 |

---

## 7. 给 Orbit 的结论（Top 5 必须补齐）

1. **单一会话平面 + 追加日志**：`sessions`(56 列) + `messages`(24 列) 同库，`source` 区分入口、`id` 作回放游标、`active/compacted` 做归档不删、`parent_session_id` 做谱系。缺这一层，重连/回放/审计全都无处落地。
2. **压缩谱系与系统提示词去重**：`parent_session_id` + `end_reason='compression'` + `compression_locks` + `compression_failure_*` 状态位 + `system_prompts(hash)` 内容寻址。长会话可持续的前提，不是「截断」，是「谱系 + 归档」。
3. **幂等与至少一次投递账本**：`delivery_obligations`（state/attempts/last_error/owner_pid）、`async_delegations`（delegation_id PK + delivery_claim + origin_session_id 恢复路由）、`tasks.idempotency_key` + `claim_lock`。崩溃重启后「不重复、不丢」全靠这三张表。
4. **孤儿/级联回收与存活租约**：`end_reason ∈ {startup_orphan_reap, ws_orphan_reap}` + `runtime/active_sessions.json` 租约 + `gateway_heartbeats` + `processes.json(pid+host_start_time)` + `taskkill /T`。多端并发不互相踩的前提。
5. **验证证据 + 迁移版本 + 备份通道**：`verification_events/state`（声称通过必须有证据行）、`schema_version` 表 + `sessions repair/recover/optimize`、`backup/import`（zip + `--quick` 关键集）、双 FTS 索引（分词 + trigram）作为会话检索底座。

---

## 8. 未取证 / 需注意的事项（如实标注）

| 项 | 状态 |
|---|---|
| `state.db` 业务代码（`SessionDB`、迁移器、压缩器） | **未取证**：这些模块编译进 exe（PyInstaller 压缩 PYZ，字符串搜不到），`_internal` 下只有 `tools\*.py` 可读 |
| `cron/jobs.json` 的确切路径与 schema | **推断**：本机无该文件，`cron` 包源码未解包；只能确认 `cron\` 下有 `.jobs.lock`、`executions.db`、`output/`、ticker 心跳 |
| `checkpoints/` 实机样本 | **不存在**：本机 `checkpoints` 目录缺失，机制全部来自 `checkpoint_manager.py` 源码与 `checkpoints --help` |
| `messages.observed` / `effect_disposition` / `display_kind` 语义 | **部分推断**：列存在但本机几乎全空/NULL，未找到可读源码定义 |
| 压缩（`end_reason='compression'`）实机样本 | **0 行**：本机从未触发压缩，谱系只能靠源码语义 + 6 条委派子会话旁证 |
| `hosted_room_*` 14 张表 | 结构已完整导出，但**全 0 行**，无实机数据可对照 |
| `gateway_heartbeats` 最后一条 `last_heartbeat` 值异常（`179311-1288.39898`，`typeof=real`） | 疑为写入中断留下的异常值；两次只读读取一致，不影响本清单结论，但说明**该列缺少合法性校验** |
| `config.yaml` / `auth.json` / `.env` 内含明文密钥 | 本报告**不复制任何密钥值**，只描述结构；外发前请脱敏 |

---

### 附：本次只读取证的可复现命令

```powershell
# 1) schema + 行数（只读，immutable）
python dump_schema.py "E:\Hermes Agent CN Desktop\data\hermes-home\state.db" --out state_db.json

# 2) WAL 感知只读对照（仍为 mode=ro）
python dump_schema.py "...\state.db" --rw-shm --out state_db_wal.json

# 3) CLI 仅 --help
& $exe sessions --help; & $exe checkpoints --help; & $exe backup --help
& $exe import --help;   & $exe dump --help;        & $exe status --help
```
脚本与原始 JSON 落盘于 `handoff\hermes-parity\research\_tools\`（`state_db.json`、`state_db_wal.json`、`kanban.json`、`projects.json`、`verification_evidence.json`、`desktop-ui.json`、`cron_executions.json`、`help\*.txt`），可复核每个数字。
