# Hermes 记忆层完整清单与机制（runtime 0.21.0-cn.18 实测）

> 实测对象：`E:\Hermes Agent CN Desktop\data\versions\0.21.0-cn.18`（CN 发行版）
> 实测时间基准：state.db 最新 session = `20261012_134424_c05887`
> 安全约束：全程未修改 Hermes 目录 / hermes-home；sqlite 只用 `mode=ro&immutable=1`；exe 只带 `--help`；未安装任何依赖。

---

## 0. 结论摘要（TL;DR）

| 项 | 实测值 |
|---|---|
| Prompt Memory 上限 | MEMORY.md **2,200 字符**（可配 1–8,000）；USER.md **1,375 字符**（可配） |
| MEMORY.md 现状 | **2,171 字符 / 20 条 = 98.7% 满** |
| USER.md 现状 | **1,393 字符 / 1 条 = 101.3% 超限**（已无法再 add，必须先 replace/remove） |
| 注入方式 | **冻结快照（frozen snapshot）**，仅在 session 启动时读取一次，会话内写入不改 system prompt |
| 三层记忆 | Prompt Memory（MEMORY.md+USER.md） / Episodic Archive（state.db + FTS5） / Skill Memory（skills/） |
| 向量检索 | **核心完全不用向量**：只用 SQLite FTS5 + BM25；向量只存在于外部 provider（mem0-OSS / hindsight / retaindb / supermemory 云侧） |
| Episodic 规模 | state.db **174.4 MB**，messages **14,595** 行，sessions **100**，schema_version **26** |
| FTS5 延迟 | 单词查询净耗时 **1.5–2 ms**（含进程启动 6.7 ms，基线 5 ms） |
| session_search 是否花 LLM | **不花**。源码明写 "No LLM calls anywhere"，summary LLM 路径已被 PR #26419 移除 |
| 外部 provider 插件 | **8 个**目录（byterover/hindsight/holographic/honcho/mem0/openviking/retaindb/supermemory）；`memory --help` 只列 **7 个**（漏 supermemory） |
| system prompt 总量 | **40,163 字符**，其中技能索引占 **22,754 字符 = 56.7%**，记忆两块合计仅 **3,950 字符 = 9.8%** |

---

## 1. 三层记忆结构

### 1.1 Prompt Memory —— memories/MEMORY.md + USER.md

**文件实测（UTF-8，字节精确）**

| 文件 | 磁盘字节 | 字符数 | 条目数 | 上限 | 占用率 | 中文占比 | token 估算 |
|---|---|---|---|---|---|---|---|
| `memories/MEMORY.md` | 3,500 B | 2,171 | 20 | 2,200 | **98.7%** | 884 CJK | **≈900–1,300 tok**（chars/4 下限 543） |
| `memories/USER.md` | 3,190 B | 1,393 | **1**（单条巨型条目） | 1,375 | **101.3%（超限）** | 1,175 CJK | **≈1,000–1,400 tok** |
| `SOUL.md`（同层但不同机制） | 540 B | — | — | 无 | — | — | ≈180 tok |

> 计数口径：`_char_count()` = `len("\n§\n".join(entries))`，与文件字符数一致（实测两者相等）。**按字符不按 token**——源码 docstring 明写理由："Character limits (not tokens) because char counts are model-independent."

**上限常量（`tools/memory_tool.py` L80-82, L192）**

```python
DEFAULT_MEMORY_CHAR_LIMIT = 2200     # MEMORY.md 默认
MIN_MEMORY_CHAR_LIMIT     = 1
MAX_MEMORY_CHAR_LIMIT     = 8000     # 归一化夹取上界
user_char_limit: int = 1375          # USER.md 默认（MemoryStore.__init__）
ENTRY_DELIMITER = "\n§\n"            # U+00A7 分节符
```

配置覆盖键（`load_on_disk_store()` L935-970 读取）：`memory.memory_char_limit` / `memory.user_char_limit`，另 `memory.memory_enabled`、`memory.user_profile_enabled`（默认都 true）。本机 `config.yaml` **没有 `memory:` 段** → 全部走默认值、外部 provider 未启用。

**注入时机：冻结快照（实测确认）**

- `MemoryStore.load_from_disk()` 在 session 启动时读盘 → `_render_block()` 生成 `_system_prompt_snapshot`。
- `format_for_system_prompt(target)` 只返回快照，**会话中永不改变**；源码注释："Mid-session writes update files on disk immediately (durable) but do NOT change the system prompt -- this preserves the prefix cache for the entire session."
- 工具响应返回的是**实时状态**（live state），错误分支才回显 `current_entries`；成功响应刻意不回显（防模型反复"找活干"）。

**实际渲染格式（从 state.db `sessions.system_prompt` 反查，非推测）**

```
══════════════════════════════════════════════
MEMORY (your personal notes) [99% — 2,181/2,200 chars]
══════════════════════════════════════════════
本机装OpenClaw(...)。Miniconda3在E:\Miniconda3(未入PATH，用完整路径)。
§
高考知识库knowledge/gaokao/与E:\报考。
§
...
══════════════════════════════════════════════
USER PROFILE (who the user is) [100% — 1,375/1,375 chars]
══════════════════════════════════════════════
...
```

**system prompt 实测分区（session 20261012_134424_c05887，总 40,163 字符）**

| 位置 | 区块 | 字符数 | 占比 |
|---|---|---|---|
| L1-26 | SOUL.md 人格 + 核心行为准则（Finishing the job / Parallel tool calls / Mid-turn steering / Tool-use enforcement） | 5,534 | 13.8% |
| L27-272 | **`## Skills (mandatory)` 技能索引**（分类→技能名+截断描述，246 行） | **22,754** | **56.7%** |
| L273-491 | Project Context / AGENTS.md（工作区上下文文件） | 7,921 | 19.7% |
| L492-534 | **MEMORY 块** | 2,331 | 5.8% |
| L535-末尾 | **USER PROFILE 块** + `Conversation started / Model / Provider` 尾注 | 1,619 | 4.0% |

> 关键洞察：记忆块在 prompt **末尾**，技能索引吃掉一多半预算。任何"记忆扩容"在 40k 字符的 prompt 里都只是零头；真正的预算大头是技能索引。

**写满后的合并策略：没有自动合并（模型驱动 + 原子批处理）**

1. `add` 超限 → 返回**非终态错误**，附 `current_entries` 全文 + `usage`，指令："Consolidate now: use 'replace' to merge overlapping entries into shorter ones or 'remove' stale ... then retry this add — all in this turn."
2. `apply_batch(target, operations)` —— 一次调用内 remove/replace/add 混合，**按最终结果校验预算**，全有或全无（"all-or-nothing"）。这是官方推荐的合并路径，避免多轮重发上下文的抖动。
3. 熔断：`_MAX_CONSOLIDATION_FAILURES_PER_TURN = 3`。一轮内第 4 次失败返回终态 `{"success": false, "done": true, ...}`，强制模型停止重试并回答用户（issue #42405：记忆副作用不得阻断回复）。
4. 成功即重置计数器（`_success_response()` 里 `_consolidation_failures = 0`）。
5. 没有后台 LLM 摘要/压缩任务 —— 合并完全由模型在会话内完成。

### 1.2 Episodic Archive —— state.db + FTS5

**库文件**：`hermes-home/state.db` = **182,902,784 B（174.4 MB）**，另有 `-wal` 4,185,952 B、`-shm` 32,768 B。`schema_version` 表 = **26**。

**表结构（`sqlite_master` 原文，只读导出）**

```sql
CREATE TABLE sessions (
  id TEXT PRIMARY KEY, source TEXT NOT NULL, user_id TEXT, session_key TEXT,
  chat_id TEXT, chat_type TEXT, thread_id TEXT, display_name TEXT, origin_json TEXT,
  expiry_finalized INTEGER DEFAULT 0, model TEXT, model_config TEXT, system_prompt TEXT,
  parent_session_id TEXT, started_at REAL NOT NULL, ended_at REAL, end_reason TEXT,
  message_count INTEGER DEFAULT 0, tool_call_count INTEGER DEFAULT 0,
  input_tokens INTEGER DEFAULT 0, output_tokens INTEGER DEFAULT 0,
  cache_read_tokens INTEGER DEFAULT 0, cache_write_tokens INTEGER DEFAULT 0,
  reasoning_tokens INTEGER DEFAULT 0, cwd TEXT, git_branch TEXT, git_repo_root TEXT,
  billing_provider TEXT, billing_base_url TEXT, billing_mode TEXT,
  estimated_cost_usd REAL, actual_cost_usd REAL, cost_status TEXT, cost_source TEXT,
  pricing_version TEXT, title TEXT, api_call_count INTEGER DEFAULT 0,
  handoff_state TEXT, handoff_platform TEXT, handoff_error TEXT,
  compression_failure_cooldown_until REAL, compression_failure_error TEXT,
  compression_fallback_streak INTEGER NOT NULL DEFAULT 0, profile_name TEXT,
  rewind_count INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
  system_prompt_hash TEXT, last_activity_at REAL, last_activity_description TEXT,
  last_activity_provenance TEXT, compression_ineffective_count INTEGER NOT NULL DEFAULT 0,
  pinned INTEGER NOT NULL DEFAULT 0, last_read_at REAL,
  git_metadata_generation INTEGER NOT NULL DEFAULT 0, title_source TEXT,
  hidden INTEGER NOT NULL DEFAULT 0,
  FOREIGN KEY (parent_session_id) REFERENCES sessions(id)
)

CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id),
  role TEXT NOT NULL, content TEXT, tool_call_id TEXT, tool_calls TEXT, tool_name TEXT,
  effect_disposition TEXT, timestamp REAL NOT NULL, token_count INTEGER,
  finish_reason TEXT, reasoning TEXT, reasoning_content TEXT, reasoning_details TEXT,
  codex_reasoning_items TEXT, codex_message_items TEXT, platform_message_id TEXT,
  observed INTEGER DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,      -- 1=在活上下文中
  compacted INTEGER NOT NULL DEFAULT 0,   -- 1=已被压缩归档
  api_content TEXT, display_kind TEXT, display_metadata TEXT,
  _compressed_summary INTEGER NOT NULL DEFAULT 0
)

CREATE VIRTUAL TABLE messages_fts         USING fts5(content);                    -- 默认 unicode61
CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(content, tokenize='trigram'); -- CJK/子串用
```

**索引清单（11 个 + 2 个 FTS5 虚表）**

```
idx_sessions_source (source)                                   idx_sessions_source_id (source,id)
idx_sessions_parent (parent_session_id)                        idx_sessions_started (started_at DESC)
idx_sessions_session_key (session_key, started_at DESC)        idx_sessions_gateway_peer (source,user_id,chat_id,chat_type,thread_id,started_at DESC)
idx_sessions_handoff_state (handoff_state, started_at)         idx_sessions_system_prompt_hash (system_prompt_hash)
idx_sessions_title_unique (title) WHERE title IS NOT NULL      ← 唯一索引，标题可反查 session
idx_messages_session (session_id, timestamp)                   idx_messages_session_id (session_id, id)
idx_messages_session_active (session_id, active, timestamp)    idx_messages_active_null (active) WHERE active IS NULL
idx_messages_platform_msg_id (session_id, platform_message_id) WHERE platform_message_id IS NOT NULL
idx_messages_assistant_calls_by_session (session_id) WHERE role='assistant' AND tool_calls IS NOT NULL
```

**数据量实测**：`messages` 14,595 行 / `messages_fts` 14,595 / `messages_fts_trigram` 14,595 / `sessions` 100。
`state_meta` 仅一条：`fts_optimize_available=1`（配合 `hermes sessions optimize` 做 FTS5 segment merge + VACUUM）。

**检索延迟实测**（sqlite3 CLI，`mode=ro&immutable=1`，含进程启动基线 5.0 ms）

| 查询 | 命中行数 | 耗时 |
|---|---|---|
| `'memory'` 单词 | 491 | 6.7 ms → **净 ~1.7 ms** |
| `'memory search'`（隐式 AND） | 197 | — |
| `'memory OR session'` | 1,572 | — |
| `'"session search"'`（短语） | 22 | — |
| `'deploy*'`（前缀） | 199 | — |
| `'memory NOT kanban'` | 461 | — |
| trigram `'记忆'` | 4 | 6.1 ms → **净 ~1.1 ms** |
| **unicode61 `'记忆'`** | **0** ⚠️ | — |

> ⚠️ **CN 发行版关键坑**：默认 `unicode61` 分词器把中文整段当一个 token，`messages_fts MATCH '记忆'` **命中 0**。中文检索必须走 `messages_fts_trigram`。这正是 schema 里第二张 FTS5 表存在的理由。

**anchored windows 机制**

- 由 `hermes_state.SessionDB` 提供三个原语（源码在 exe 的 PYZ 里，`.py` 不在 `_internal`，以下由调用点 + schema + 实测行为反推）：
  - `db.search_messages(query, role_filter, exclude_sources, limit, offset, sort, fields)` → 返回 `id, session_id, role, snippet, source, model, session_started`（`_DISCOVER_SEARCH_FIELDS`）
  - `db.get_anchored_view(session_id, anchor_id, window=5, bookend=3)` → `{window, bookend_start, bookend_end, messages_before, messages_after}`
  - `db.get_messages_around(session_id, msg_id, window=N)` → scroll 用，无 bookend
- 窗口语义：**锚点消息 ±5 条**（含锚点，带 `anchor: true` 标记），外加**首 3 条 / 末 3 条 bookend**；`messages_before/after` 给出真实边界余量。
- 活上下文过滤：`active=1` 才算在活上下文；`compacted=1` 的历史行会被 session_search 放行（因为已不在上下文里）。`_HIDDEN_SESSION_SOURCES = ("kanban","subagent","tool")` 默认不进检索；`_DEMOTED_SESSION_SOURCES = ("cron",)` 只降权不排除（issue #19434：cron 语料会饿死交互会话）。

### 1.3 Skill Memory —— skills/

- 实测：`hermes-home/skills/` 下 **37 个分类目录 + 168 个 SKILL.md**，共 **922 个文件**；`.skills_prompt_snapshot.json`（112,272 B，version=2）缓存 **183 条** manifest（`SKILL.md → [mtime_ns, size]`）+ `skills[]`（name/category/description/platforms/conditions）+ `category_descriptions`。
- 注入：system prompt 里的 `## Skills (mandatory)` 段 = **22,754 字符**（占 prompt 56.7%），只放"分类 + 技能名 + 截断描述"。
- 正文按需加载：`skill_view(name=...)`（`tools/skills_tool.py`，`MAX_NAME_LENGTH=64`、`MAX_DESCRIPTION_LENGTH=1024`，`_SKILLS_CACHE_TTL_SECONDS=30.0`）—— 典型渐进披露。

**三层分工（一句话版）**

| 层 | 写什么 | 何时进上下文 | 容量 | 检索 |
|---|---|---|---|---|
| Prompt Memory | 稳定高信号事实/偏好（**每轮都注入**） | 启动时冻结快照，全轮次 | 2,200 / 1,375 字符硬顶 | 无检索，全量注入 |
| Episodic Archive | 完整对话原文（自动写，不可关） | 不进上下文，按需 `session_search` | 磁盘 174 MB 级，无上限 | FTS5/BM25 + anchored window |
| Skill Memory | 可复用**流程/操作手册** | 仅索引（名字+描述）常驻；正文 `skill_view` 拉取 | 索引 ~22.7k 字符 | 模型读索引自行选择 |

> memory 工具 schema 里明确划线："SKIP: ... Procedures belong in a skill, not memory." 与 "temporary todo state (use session_search)" —— 三层边界是写进工具描述里的。

---

## 2. recall 与 user modeling 的分离

**结论：核心层不分离——同一个工具、同一个 store 类，用 `target` 参数分流；外部 provider 才把 recall 与 user modeling 拆成不同后端与不同工具。**

**写者**：只有模型自己（通过 `memory` 工具）。没有后台抽取、没有自动摘要、没有 heuristics 写入。
**读者**：system prompt 注入器（`format_for_system_prompt`），启动时一次。

**暴露给模型的工具（核心只有 2 个与记忆相关 + 2 个与技能相关）**

```jsonc
// tools/memory_tool.py L1101 注册名 = "memory"，toolset = "memory"
{
  "action": "add" | "replace" | "remove",   // 省略时走 operations 批处理
  "target": "memory" | "user",              // 动态 enum：按 config 开关裁剪
  "content": "…",                            // add/replace 必填
  "old_text": "…",                           // replace/remove 必填（短唯一子串，非全文/非 ID）
  "new_text": "…",                           // content 的别名（兼容 old_text/new_text 直觉）
  "operations": [{ "action": "...", "content": "...", "old_text": "..." }, ...]  // 原子批处理
}
```

| 关注点 | target=`memory` | target=`user` |
|---|---|---|
| 语义 | agent 自己的笔记（环境事实、项目约定、工具怪癖、教训） | 用户是谁（偏好、沟通风格、期望、工作习惯） |
| 文件 | `memories/MEMORY.md` | `memories/USER.md` |
| 上限 | 2,200（1–8,000 可配） | 1,375 可配 |
| prompt 头 | `MEMORY (your personal notes) [x% — n/limit chars]` | `USER PROFILE (who the user is) [x% — n/limit chars]` |
| 开关 | `memory.memory_enabled` | `memory.user_profile_enabled` |

其他相关工具：`session_search`（episodic 层，见 §3）、`skills_list` / `skill_view` / `skill_manage`（skill 层）。

**外部 provider 才做的分离**（同进程内两个方向都走 provider）：
- 自动 recall：`prefetch(query)` → 注入（Hindsight `auto_recall`、Supermemory `auto_recall`、Honcho `recallMode=hybrid` 的 base context + dialectic）。
- user modeling：Honcho 的 user representation / peer card、RetainDB `retaindb_profile`、Supermemory `supermemory-profile`、Hindsight `observation` 层。
- Honcho 明确把注入放在**用户消息**而不是 system prompt，以保 prefix cache（"Only a static mode header goes in the system prompt"）—— 与核心的 frozen snapshot 是同一动机的两种解法。

---

## 3. `session_search` 完整机制（`tools/session_search_tool.py`，1,271 行）

### 3.1 四种形态（单一 schema，无 mode 参数，靠入参推断）

| 形态 | 触发条件 | 行为 | 源码函数 |
|---|---|---|---|
| **DISCOVERY** | 传 `query` | FTS5 检索 + 按 lineage 去重 + 锚定窗口水化 | `_discover()` L761 |
| **SCROLL** | `session_id` + `around_message_id` | 锚点 ±window 消息切片，无 FTS5、无 bookend | `_scroll()` L542 |
| **READ** | 只有 `session_id` | 整段 dump；大会话退化 head/tail | `_read_session()` L435 |
| **BROWSE** | 无参 | 最近会话列表（标题/预览/时间） | `_list_recent_sessions()` L485 |

### 3.2 关键数值（全部来自源码常量）

```python
_DISCOVER_SCAN_LIMIT   = 300      # FTS 先扫 300 行，再做 lineage 去重
_HIDDEN_SESSION_SOURCES = ("kanban","subagent","tool")   # 默认排除
_DEMOTED_SESSION_SOURCES = ("cron",)                     # 只降权
_COMPACTION_PREFIXES = ("[CONTEXT COMPACTION", "[CONTEXT SUMMARY]:")  # bookend 里剔除
limit   : default 3, clamp [1, 10]         # _session_search_impl L1046-1052
window  : default 5, clamp [1, 20]         # _scroll L568-576
get_anchored_view(window=5, bookend=3)     # discovery/title 两处都硬编码 5/3
_read_session(head=20, tail=10)            # 大会话只给首 20 + 末 10
_shaping: window 消息 max_content_len=4000 / bookend 消息 1200
```

### 3.3 FTS5 查询语法（实测验证，非文档转述）

工具把 query 原样交给 `db.search_messages()`，即 FTS5 `MATCH` 语法。**实测命中行数**：

| 语法 | 示例 | 实测命中 |
|---|---|---|
| 单词（隐式 AND） | `memory` | 491 |
| 多词隐式 AND | `memory search` | 197 |
| OR | `memory OR session` | 1,572 |
| 短语 | `"session search"` | 22 |
| 前缀 | `deploy*` | 199 |
| NOT | `memory NOT kanban` | 461 |
| 中文（unicode61） | `记忆` | **0（不可用）** |
| 中文（trigram 表） | `记忆` | 4 |

空结果时工具自带提示（源码 `_empty_payload`）："FTS5 ANDs all terms by default — broaden with OR (`alpha OR beta`), exact-match with quoted phrases, exclude with NOT, or prefix-match with `deploy*`."

### 3.4 anchored windows（窗口大小/重叠）

- discovery：`get_anchored_view(hit_sid, msg_id, window=5, bookend=3)` → 命中消息 ±5 + 首3/末3。
- **adaptive 详情（默认）**：`result_detail = "full" if detail=="full" or not results else "compact"` —— **只有第 1 条**结果享受完整窗口 + bookend，第 2 条起只给 `match_message_id` 那一条原文（`window_messages = [m for m in window if m.id == msg_id]`）。
- scroll：重锚推进，**窗口边界消息重复出现作为定位标记**（源码 hint："the boundary message repeats as an orientation marker"）；`messages_before/after < window` 表示到端了。
- lineage 修复：锚点落在子会话时静默 rebind（`rebind_warning`）；当前 lineage 内且仍在活上下文的命中会被拒（"already in your active context"），但 `compacted=1` 归档行 / compression-ended 父会话 / `/new` 重置前驱三种情况放行。

### 3.5 LLM 摘要二次加工 —— **不存在（已移除）**

模块 docstring 原文：

> "No LLM calls anywhere — every shape returns actual messages from the DB."
> "History: PR #20238 (JabberELF) seeded a fast/summary dual-mode split; the toolkit expansion in PR #26419 (yoniebans) added the anchored drill-down, bookends, and sort. **This module merges all of that into a single calling shape with no mode parameter, no summary LLM path**, and explicit scroll support."

工具 schema 描述也写："Results are actual DB messages, **no LLM**."

**成本 = 0 token（除返回内容本身）**。所谓"二次加工"全部是确定性字符串处理：ANSI 转义剥离（`tools/ansi_strip.strip_ansi`）、compaction 摘要剔除、截断标记（`content_truncated` + `original_content_chars`）、cron 降权稳定排序、lineage 去重。

### 3.6 返回结构

**discover**：
```jsonc
{ "success": true, "mode": "discover", "query": "...", "detail": "adaptive",
  "results": [ { "session_id", "link", "when", "source", "model", "title",
                 "matched_role", "match_message_id", "snippet",
                 "bookend_start":[...], "messages":[{"id","role","content","timestamp","anchor":true,...}],
                 "bookend_end":[...], "messages_before", "messages_after",
                 "detail":"full|compact", "parent_session_id"? } ],
  "count", "sessions_searched", "link_hint" }
```
**scroll**：`{success, mode:"scroll", session_id, around_message_id, session_meta{when,source,model,title}, window, messages[], messages_before, messages_after, hint, warning?}`
**read**：`{success, mode:"read", session_id, link, session_meta, message_count, truncated, messages[], message?}`
**browse**：`{success, mode:"browse", results[{session_id,link,title,source,started_at,last_active,message_count,preview}], count, message}`

`link` = `@session:<profile>/<id>`，schema 要求模型**原样内联**写进回复（渲染成带标题的链接）。

---

## 4. 外部 memory provider 插件（8 个逐个）

**装载机制**（`plugins/memory/__init__.py`）：4 个来源 —— ① 内置 `plugins/memory/<name>/` ② `$HERMES_HOME/plugins/<name>/` ③ 项目内 `./.hermes/plugins/<name>/`（需 `HERMES_ENABLE_PROJECT_PLUGINS`）④ pip entry point 组 `hermes_agent.memory_providers`。**优先级：bundled > user > project > entry point**（与通用 PluginManager 相反，故意如此：防止工作树里的同名目录劫持记忆）。**同一时刻只能激活 1 个**，由 `memory.provider` 选；**内置 MEMORY.md/USER.md 永远在线，不可关闭**（`memory --help` 原话："Built-in memory (MEMORY.md/USER.md) is always active."）。

Provider 契约（`agent/memory_provider.py` ABC，由各插件实现）：`name / is_available / initialize / system_prompt_block / prefetch / sync_turn / get_tool_schemas / handle_tool_call / on_session_end / on_memory_write / shutdown / get_config_schema / save_config`。

| # | provider | 定位 | 后端 / 是否需云 | 凭证 | 工具 | 钩子 |
|---|---|---|---|---|---|---|
| 1 | **holographic** | 本地图记忆（唯一纯本地、零依赖、零云的"结构化"provider） | **本地 SQLite** `$HERMES_HOME/memory_store.db`；FTS5 + **HRR 全息向量**（numpy 可选） | 无 | `fact_store`（9 action：add/search/probe/related/reason/contradict/update/remove/list）、`fact_feedback` | `on_session_end` |
| 2 | **hindsight** | 知识图谱 + 实体解析 + 多策略检索；三种模式 | ① `cloud`（Hindsight Cloud）② `local_embedded`（本地 daemon 内置 PostgreSQL，空闲 **5 min** 自动停）③ `local_external`（自建 HTTP） | `HINDSIGHT_API_KEY` / 本地模式 `HINDSIGHT_LLM_API_KEY` | `hindsight_retain`、`hindsight_recall`、`hindsight_reflect` | `on_session_end` |
| 3 | **mem0** | 服务端 LLM 事实抽取 + 语义检索 + 自动去重；三连接模式 | ① `platform`（api.mem0.ai）② 自建 dashboard（FastAPI+pgvector，`X-API-Key`）③ `oss` 进程内（LLM/embedder/vector 全自选） | `MEM0_API_KEY` | `mem0_search`、`mem0_add`、`mem0_update`、`mem0_delete` | — |
| 4 | **supermemory** | 语义长期记忆 + **profile 召回** + 整会话 ingest（一次/session） | 托管 `api.supermemory.ai` 或自建 `npx supermemory local` | `SUPERMEMORY_API_KEY` | `supermemory_store`、`supermemory_search`、`supermemory_forget`、`supermemory_profile`（另有 kebab 别名 `supermemory-save/-search/-forget/-profile`） | — |
| 5 | **honcho** | **AI-native 跨会话 user modeling**（dialectic 多轮推理 + conclusions），最偏"用户建模" | Honcho Cloud（OAuth/device code/API key）或自建 `baseUrl` | `HONCHO_API_KEY` 或 OAuth 授权（自动刷新） | `honcho_profile`、`honcho_search`、`honcho_reasoning`、`honcho_context`、`honcho_conclude` | `on_session_end` |
| 6 | **byterover** | 层级知识树 + 分级检索（fuzzy text → LLM-driven search）；**local-first** | 本地 `$HERMES_HOME/byterover/`，走外部 `brv` CLI；云同步可选 | `BRV_API_KEY`（**可选**） | `brv_query`、`brv_curate`、`brv_status` | `on_pre_compress` |
| 7 | **openviking** | 火山引擎（字节）context database：文件系统式层级（`viking://` URI）+ 分级上下文 | 自建 `openviking-server`（默认 `http://127.0.0.1:1933`） | `OPENVIKING_API_KEY`（本地可信模式可无）+ account/user | `viking_search`、`viking_read`、`viking_browse`、`viking_remember`、`viking_forget`、`viking_add_resource` | `on_session_end` |
| 8 | **retaindb** | 云记忆 API：hybrid（**Vector + BM25 + Rerank**）+ **7 种记忆类型** | 纯云 `api.retaindb.com`（$20/月） | `RETAINDB_API_KEY`（必需） | `retaindb_profile`、`retaindb_search`、`retaindb_context`、`retaindb_remember`、`retaindb_forget`、`retaindb_upload_file`、`retaindb_list_files`、`retaindb_read_file`、`retaindb_ingest_file`、`retaindb_delete_file` | — |

> 注：`memory --help` 的 provider 列表只写了 7 个（honcho, openviking, mem0, hindsight, holographic, retaindb, byterover），**supermemory 有目录/plugin.yaml/README/实现但未出现在 help 字符串里** —— CN 0.21.0-cn.18 的一处不一致。

### 4.1 holographic 深挖（唯一本地"图记忆"）

`store.py`（26 KB）+ `retrieval.py`（25 KB）+ `holographic.py`（11 KB HRR 代数）+ `__init__.py`（20 KB provider）。

**库表**（`_SCHEMA` 原文要点）：
```sql
facts(fact_id PK, content TEXT NOT NULL UNIQUE, category, tags, trust_score REAL DEFAULT 0.5,
      retrieval_count, helpful_count, created_at, updated_at, hrr_vector BLOB)
entities(entity_id PK, name, entity_type DEFAULT 'unknown', aliases, created_at)
fact_entities(fact_id, entity_id, PRIMARY KEY(fact_id, entity_id))
memory_banks(bank_id PK, bank_name UNIQUE, vector BLOB, dim, fact_count, updated_at)
CREATE VIRTUAL TABLE facts_fts USING fts5(content, tags, content=facts, content_rowid=fact_id);  -- 外部内容表 + 3 触发器
idx_facts_trust(trust_score DESC) / idx_facts_category / idx_entities_name
```

**数值**：
- HRR 维度 `hrr_dim = 1024`（默认），相位向量编码（`encode_atom` SHA-256 计数器块 → 相位），`phases_to_bytes/bytes_to_phases` 落 BLOB。
- 打分权重：**fts 0.4 / jaccard 0.3 / hrr 0.3**（无 numpy 时自动重分配为 0.6 / 0.4 / 0）。
- 检索管线（`FactRetriever.search`）：FTS5 取 **limit×3** 候选 → Jaccard 重排 → `final = relevance × trust_score` → 可选时间衰减 `0.5^(age_days/half_life)`（half_life 默认 **0 = 关闭**）。
- 信任分：`default_trust=0.5`，helpful **+0.05** / unhelpful **−0.10**，夹取 [0,1]（`_clamp_trust`）。
- 去重：`content UNIQUE` → `sqlite3.IntegrityError` 时返回既有 `fact_id`（**内容级精确去重，非 hash**）。
- 实体抽取正则：`_RE_CAPITALIZED`（连续首字母大写词组）/ 双引号 / 单引号 / `X aka Y` / `X also known as Y`。
- `probe()` 是**真·组合检索**：`role_entity = encode_atom("__hrr_role_entity__")`，`probe_key = bind(entity_vec, role_entity)`，再从 `memory_banks`（按 category 打包的 bundle 向量）**unbind** 出关联内容 —— 不是关键词搜索。
- 其余 action：`related`（图邻居）、`reason`（多跳组合）、`contradict`（冲突检测）。
- 连接策略：进程级共享连接注册表 + 单 RLock + `isolation_level=None`（autocommit），避免 delegate 子 agent 多写者 "database is locked"。

### 4.2 与核心的差异速查

| 维度 | 核心 built-in | holographic | hindsight | mem0 | supermemory | honcho | byterover | openviking | retaindb |
|---|---|---|---|---|---|---|---|---|---|
| 后端位置 | 本地文件 | 本地 SQLite | 云/本地 PG | 云/自建/进程内 | 云/自建 | 云/自建 | 本地 CLI+树 | 自建服务 | 云 |
| 向量 | ❌ | ✅ HRR(1024) | ✅ 服务端 | ✅ qdrant/pgvector | ✅ 服务端 | ✅ 服务端 | ❌（fuzzy+LLM 检索） | ✅（服务端 embedding） | ✅ |
| 自动抽取 | ❌ | 可选 `auto_extract` | ✅ `auto_retain` | ✅ 服务端 LLM | ✅ `auto_capture` | ✅ observation | ❌（模型显式 curate） | ✅ session 提交抽取 | ✅ |
| 用户建模 | USER.md 手工 | ❌ | observation 层 | ❌ | profile 端点 | ✅ 核心卖点 | ❌ | ❌ | ✅ `retaindb_profile` |
| 需云/凭证 | 无 | 无 | 云需 key | 需 key | 需 key | key/OAuth | 可选 | 可选 | **必需** |

---

## 5. 记忆治理

### 5.1 去重

- **加载时**：`load_from_disk()` 与 `_reload_target()` 都做 `list(dict.fromkeys(entries))` —— 保序、留首次出现，**精确字符串去重**。
- **写入时**：`add()` 里 `if content in entries: return "Entry already exists (no duplicate added)."`。
- **无 content hash、无 embedding 近似去重**（核心）。语义去重交给外部 provider（mem0 "automatic deduplication"；holographic 是 `UNIQUE(content)` 精确匹配）。
- **多写者**：每次变更在 `.lock` 文件锁（Windows 走 `msvcrt.locking`）下**重新读盘**再改，避免姐妹会话互相覆盖。

### 5.2 遗忘 / 过期

- 核心**没有 TTL、没有时间衰减、没有自动过期**。删除只有三条路：
  1. 模型调 `memory(action="remove", old_text=...)`
  2. `hermes memory reset --target {all|memory|user} [--yes]`（清空 MEMORY.md / USER.md）
  3. `hermes journey delete <node>`（node = `skill name` 或 `memory:<source>:<index>`）
- 时间衰减只存在于 holographic（`temporal_decay_half_life`，默认关闭）；会话层有 `hermes sessions prune/archive` 与 `pinned` 豁免。
- 值得注意：**没有"记忆过期"就没有记忆腐化治理** —— 本机 MEMORY.md 里"KKRICH 中转 key 在 cc-switch.db"这类条目永远不会自己退场。

### 5.3 容量封顶

- 双 store 各自硬顶（2,200 / 1,375），**整个 store 共享一个字符预算**，不是每条上限。
- 超顶 → `add`/`replace` 拒绝 + 回显 `current_entries` + 合并指令；`apply_batch` 按**最终结果**校验（中间态超限无所谓），全有或全无。
- 熔断 3 次/轮（`_MAX_CONSOLIDATION_FAILURES_PER_TURN`），之后强制终态返回，防"记忆写入把一轮对话拖死"。
- 单条 > 全库上限 → 判为外部漂移（见 5.6）。

### 5.4 写入守门（三层）

1. **注入/外泄扫描**：`_scan_memory_content()` → `tools/threat_patterns.first_threat_message(scope="strict")`。写时扫 `content`；`apply_batch` 里**任一 op 命中即整批拒绝**。
2. **审批门**：`_apply_write_gate()` → `tools/write_approval.evaluate_gate(wa.MEMORY, ...)`，三态 `allow / blocked / staged`；staged 写入落 `pending_id`，由 `/memory approve` 走 `apply_memory_pending()` 回放（绕过门）。**本机 `approvals.mode: 'off'` → 直接放行**。批量操作作为**单一单元**过门（`_apply_batch_write_gate`）。
3. **快照二次扫描**：`_sanitize_entries_for_snapshot()` 在构建 prompt 时逐条再扫；命中的条目在 prompt 里被替换成 `[BLOCKED: MEMORY.md entry contained threat pattern(s): ...]`，但**实时状态保留原文**，让用户能看见并自己删（"silently dropping them would hide the attack from the user"）。

### 5.5 profile（USER.md）的合并与版本

- **合并**：无自动合并。USER.md 就是一个 store，20 条也好 1 条也好，合并只能由模型用 `replace` 把多条揉短 / `apply_batch` 一次腾挪。**本机实测 USER.md 只有 1 条 1,393 字符的巨型条目且已超 1,375 上限 → 当前状态下任何 `add` 都必然失败**，只能 replace/remove 重写。
- **版本**：**没有版本号、没有历史栈、没有 diff**。只有：
  - 原子写：`atomic_write_text()`（mkstemp + fsync + `os.replace`），读者永远看到旧完整文件或新完整文件
  - 锁：`MEMORY.md.lock` / `USER.md.lock`（本机存在，0 字节）
  - 漂移备份：`MEMORY.md.bak.<unix_ts>` / `USER.md.bak.<unix_ts>`
- 跨 profile：`get_memory_dir()` = `get_hermes_home()/memories`，随 `HERMES_HOME` / profile 切换（源码专门日志 `memory: loading from %s` 以诊断"记忆看起来丢了"= profile 漂移，GitHub #335）。

### 5.6 外部漂移防护（`_detect_external_drift`）

两个信号任一命中即判漂移：
1. **round-trip 不一致**：`raw.strip() != ENTRY_DELIMITER.join(parsed)`
2. **单条超全库上限**：`max_entry_len > char_limit`（工具自己不可能写出这种条目 → 一定是 patch 工具/shell 追加/手工编辑/姐妹会话塞进来的自由文本）

命中 → 先写 `.bak.<ts>` 快照，**拒绝本次变更**（否则 flush 会把外部追加内容截断丢弃，issue #26045）。另设 `_READ_FAILED` 哨兵：文件存在但读失败（含非法 UTF-8）时**中止**而非当成空库 —— 否则 `add` 会把整库重写成一条（#26045 同类）。

### 5.7 SOUL.md 的作用与加载规则

- **内容**（540 B，实测原文）：Nous 人格声明 + "You are helpful, knowledgeable, and direct..." + `Always respond in Chinese.`
- **位置**：`hermes-home/SOUL.md`（HERMES_HOME 根，非 memories/）。
- **加载**：system prompt **第一块**（实测 `instr(system_prompt,'Always respond in Chinese') = 515`，即前 515 字符），随后才是 Hermes 文档指引与 `# Finishing the job` 等核心准则（合计 5,534 字符）。
- **规则**：静态、只读注入；**不受 memory 字符预算约束、不被 memory 工具读写**、不参与冻结快照机制（每次启动按文件当前内容构建）。同族文件（由 `plugins/memory/honcho/cli.py` L1614 的迁移清单佐证）：`SOUL.md, IDENTITY.md, AGENTS.md, TOOLS.md, BOOTSTRAP.md`；其中 `AGENTS.md` 走另一条通道注入为 `# Project Context`（本机实测 7,921 字符）。
- 语义分工（源码注释）："SOUL.md describes the agent, not the user" —— 人格归 SOUL，用户事实归 USER.md。

---

## 6. CLI 面（`hermes-agent-cn-runtime-win32-x64.exe`，全部 `--help` 实测）

### 6.1 `memory --help`

```
usage: hermes memory [-h] {setup,status,off,reset} ...
Set up and manage external memory provider plugins. Available providers:
honcho, openviking, mem0, hindsight, holographic, retaindb, byterover. Only
one external provider can be active at a time. Built-in memory
(MEMORY.md/USER.md) is always active.
```
| 子命令 | 选项 |
|---|---|
| `memory setup [provider]` | 位置参数 `provider`（如 `honcho`）跳过选择器；无其他选项 |
| `memory status` | 无 |
| `memory off` | 无（禁用外部 provider，仅留内置） |
| `memory reset` | `--yes/-y`、`--target {all,memory,user}`（默认 all） |

### 6.2 `memory-graph --help` / `journey --help` / `learning --help`

三个命令名**输出完全相同的 usage**（`memory-graph` 与 `learning` 是 `journey` 的别名）：

```
usage: hermes journey [-h] [--reveal 0..1] [--play] [--fps FPS]
                      [--width WIDTH] [--height HEIGHT] [--no-color] [--json]
                      {list,delete,edit} ...
A terminal rendition of the desktop Star Map / Memory Graph: a timeline bar
chart of learned skills and memories over time (oldest at top, newest at
bottom) plus a playable constellation scrubber.
```
| 子命令 | 选项 |
|---|---|
| `journey list` | `--no-color` |
| `journey delete <node>` | `-y/--yes`；`node` = 技能名 或 `memory:<source>:<index>` |
| `journey edit <node>` | `$EDITOR` 内编辑技能或记忆 |
| 全局 | `--reveal 0..1`、`--play`、`--fps FPS`（默认 12）、`--width`、`--height`、`--no-color`、`--json`（打印原始 graph payload） |

> 这是**记忆的可视化/治理入口**：星图 = 技能 + 记忆的时间轴，可逐节点删除/编辑。

### 6.3 `insights --help`
```
usage: hermes insights [-h] [--days DAYS] [--source SOURCE]
Analyze session history to show token usage, costs, tool patterns, and activity trends
```
`--days`（默认 30）、`--source`（cli/telegram/discord…）

### 6.4 `profile --help`（14 个子命令）
`list, use, create, delete, describe, show, alias, rename, export, import, install, update, info`
（`describe` 供 kanban orchestrator 用；`rename` 对 default 只改显示名；`install/update` 走 profile distribution git URL）

### 6.5 `sessions --help`（19 个子命令）
`list, export, delete, prune, archive, optimize, clean-markers, optimize-storage, repair, repair-routing, recover, stats, rename, pin, unpin, pinned, retitle-skills, browse, import`

其中与记忆层强相关的：
- `optimize` —— "Reclaim disk space: merge FTS5 segments + VACUUM (no data change)"
- `optimize-storage` —— "Migrate the search index to the compact v23 layout"
- `pin/unpin/pinned` —— 持久 keep 标记，**豁免自动归档**
- `prune` —— 按时间窗/source/title 删除；`archive` —— 批量软隐藏不删
- `import` —— 导入 Claude Code / Codex CLI 会话
- `repair` —— 修 state.db schema 让隐藏会话重现

---

## 7. 检索选型证据：核心用不用向量？

**结论：核心（built-in memory + session_search）零向量、零 embedding、零 ANN。唯一向量在外部 provider，且 holographic 用的是自研 HRR 而非 embedding 模型。**

证据链：

1. **依赖目录实证**（`_internal/` 全量扫描）：**不存在** `lancedb` / `chromadb` / `faiss` / `sentence_transformers` / `pgvector` / `pinecone` / `weaviate` / `milvus` / `voyageai` / `cohere` 任何一个包目录或 dist-info。
2. **确实存在的向量相关依赖，全部挂在 provider 上**：
   - `qdrant_client-1.18.0.dist-info` → mem0 OSS 的 `vector_store.provider: qdrant`
   - `mem0ai-2.0.10.dist-info`、`hindsight_client-0.6.1.dist-info`、`supermemory-3.50.0.dist-info` → 三个云/服务端 provider SDK
   - `numpy-2.4.3` → holographic HRR 代数（不是 embedding）
   - `lazy_deps.py:312` 注释：本地 Hindsight embedding 需要 `sentence-transformers`（**未安装**，说明本地向量模式未启用）
3. **源码自述**：`session_search_tool.py` docstring "Search past Hermes sessions (FTS5 over the local session DB)"、"All four modes operate on the SQLite session DB via the FTS5 index ... No LLM calls anywhere"；`memory_tool.py` 无任何 embedding 调用。
4. **DB 实证**：state.db 里只有 `messages_fts`（unicode61）与 `messages_fts_trigram` 两张 FTS5 虚表，**没有任何 vector/embedding 表**；holographic 的 `memory_store.db` 才有 `hrr_vector BLOB` + `memory_banks.vector BLOB`（且 `memory_store.db` 在本机不存在 → holographic 未启用）。
5. **config 实证**：`config.yaml` 无 `memory:` 段、无 `memory.provider`、无 embedding 相关键 → 默认纯内置 FTS5 路径。
6. **能力边界**：中文查询在 unicode61 表命中 0、在 trigram 表命中 4 —— 说明 CN 版的"语义召回"其实靠 trigram 子串，而非向量语义。**同义改写（"多少钱" vs "价格"）在核心层完全召回不到。**

> 对 Orbit 的含义：Hermes 的记忆检索是 **BM25 + 结构窗口**的工程解，把"语义"外包给外部 provider。若要"零云 + 语义"，只能自建 embedding + 本地 ANN（Hermes 本身没这条路）。

---

## 8. 对 Orbit 记忆层的启示（Top 6 必补）

1. **冻结快照 + 前缀缓存不变式**：启动时定格记忆块、会话中只写盘不改 prompt，用 `[99% — 2,181/2,200 chars]` 这种自曝用量的头部让模型自我管理预算。这是 Hermes 最值得抄的一条。
2. **字符预算 + 原子批处理 + 熔断**：单库字符硬顶（不是 token，模型无关）、`operations[]` 一次调用内腾挪+新增（按最终态校验、全有或全无）、一轮 3 次失败即终态收手。三者缺一就会出现"记忆写入把一轮对话拖死"或"模型反复重试抖动"。
3. **CJK 分词必须双索引**：`unicode61` 对中文命中 0，`trigram` 才可用。任何中文记忆/会话检索，索引层就要为 CJK 单独建表，否则"看起来有检索、实际召不回"。
4. **零 LLM 的会话召回**：`session_search` 四种形态 + 锚点窗口(±5) + bookend(3/3) + adaptive 水化（只富化 top1）+ 300 行扫描后按 lineage 去重 + cron 降权。全程 0 token，延迟 ~1.7 ms。LLM 摘要路径已被上游明确删除，别再走回头路。
5. **写入门禁与漂移防护**：写入前 threat scan（strict）→ 审批三态（allow/block/stage）→ 快照期二次扫描（prompt 用占位符、磁盘留原文供用户处置）；外部漂移 round-trip + 单条超限双信号 → `.bak.<ts>` + 拒写；读失败用 `_READ_FAILED` 哨兵中止而不是当空库。**防的是"整库被重写成一条"这类不可逆事故。**
6. **记忆要有过期与可治理面**：Hermes 核心**没有 TTL/衰减**，只能靠 `journey`（星图 list/delete/edit）+ `memory reset --target` + `sessions prune/pin` 人工治理；本机 USER.md 已 101.3% 超顶、MEMORY.md 98.7%，且存在"永远不该留的 key 位置"类条目 —— 这说明缺 TTL/置信度衰减是真实痛点。Orbit 应补：条目级 TTL/衰减 + 置信度（借鉴 holographic 的 trust ±0.05/−0.10 与半衰期）+ 可视化治理台。

---

## 附录 A：证据与复现命令

```powershell
# 1) 记忆文件字节/字符
$p="E:\Hermes Agent CN Desktop\data\hermes-home\memories\MEMORY.md"
$b=[System.IO.File]::ReadAllBytes($p); $s=[System.Text.Encoding]::UTF8.GetString($b)
"bytes=$($b.Length) chars=$($s.Length)"

# 2) state.db 结构与规模（只读）
$sq="E:\Miniconda3\Library\bin\sqlite3.exe"
$db="file:E:/Hermes Agent CN Desktop/data/hermes-home/state.db?mode=ro&immutable=1"
& $sq -readonly $db "select count(*) from messages; select count(*) from sessions;"
& $sq -readonly $db "select sql from sqlite_master where name in ('messages','sessions','messages_fts','messages_fts_trigram');"

# 3) 实际注入的 system prompt（反查而非推测）
& $sq -readonly $db "select substr(system_prompt,36220,520) from sessions where id='20261012_134424_c05887';"

# 4) CLI 面
& "E:\...\hermes-agent-cn-runtime-win32-x64.exe" memory --help
```

## 附录 B：方法与局限

- `tools/memory_tool.py`、`tools/session_search_tool.py`、`plugins/memory/**` 均为**可读源码**，本报告的常量/函数名/行号全部直接来自源码。
- **`hermes_state.py`、`agent/agent_init.py`、`agent/memory_provider.py`、CLI 主体不在 `_internal` 里**（PyInstaller PYZ 冻结在 exe 内，`_internal` 下无对应 `.py`）。因此 `SessionDB.search_messages / get_anchored_view / get_messages_around / list_sessions_rich` 的**内部 SQL 与 trigram 选择逻辑未能直接读到**，本报告用「调用点 + sqlite_master 结构 + 实测命中数/延迟」三重交叉印证。若要拿到确切 SQL，需要解包 PYZ（本次受"不安装依赖"约束未做）。
- 所有延迟数字含 sqlite3 CLI 进程启动基线 5 ms，已标注净耗时。
- 未回显任何 API key 明文。
