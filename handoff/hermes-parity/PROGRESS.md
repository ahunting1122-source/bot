# 对标进度报告

**更新时间**：2026-10-28 10:46（本机实测时钟 `Get-Date`，只读取证）
**本轮轮次**：**N = 6**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑 `config set` / `doctor --fix` / `config edit` / `doctor --live` / `cron run` / `gateway start` / `backup restore` 等会改状态或真发请求的命令。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `39cbe58`，**工作树干净** ✅）

---

## 总体完成度

### **83 / 181 项 = 45.9%**

| 项 | 说明 |
|---|---|
| 清单来源 | `MATRIX.md`，脚本逐行解析：**181 行**（L1–L16），**编号唯一 181/181** |
| 完成 | **✅ 83 条**（本轮新增 10 条，**复核 9 条 = 90%**） |
| 部分 | 🟡 **37 条**（你刻意标的 3 条 🟡 **全部复核，判为恰当**） |
| 未开始 | ⬜ **61 条** |
| 表头一致性 | MATRIX 表头「181 / ✅83 / 🟡37 / ⬜61 / 45.9%」——**与我的脚本统计逐字一致** ✅ |

### 硬口径：**全部对上（一个数字有微差）**

| 你的口径 | 我的实测 | 判定 |
|---|---|---|
| `REGISTRY` 78 顶层 / 332 二级 / 39 三级 = 449 | **78 / 332 / 39 = 449** | ✅ 完全一致 |
| done 193 / partial 16 / todo 240 | **193 / 16 / 240** | ✅ 完全一致 |
| MATRIX 181 项：✅83 / 🟡37 / ⬜61（45.9%） | **181 / 83 / 37 / 61 = 45.9%** | ✅ 完全一致 |
| pytest 1 failed, 1374 passed, 1 skipped | **1 failed, 1374 passed, 1 skipped（127.52s）** | ✅ 完全一致 |
| ruff `All checks passed!` | **All checks passed!**（exit 0） | ✅ 一致 |
| mypy **84** source files | **86** source files | ⚠️ 微差（多 2 个文件，你跑之后又加了文件；不影响结论） |
| 工作树干净 | `git status --porcelain` **空** | ✅ 一致 |
| **唯一失败** = `test_connectors::test_browser_append_and_reload` | 正是它，无其它失败 | ✅ **基线终于干净了** |

> **上轮 P0 已达成**：这是本项目**第一次**拿到"独占 + 干净工作树"的全量基线。耗时 127.52s，对比上轮被并发污染时的 711s——**同一套测试差 5.6 倍**，这就是上轮读数不可采信的直接证据。

---

## 分层完成度

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | 10 / 18 | 55.6% | 进行中（🟡2） |
| L2 Agent 核心循环 | 1 / 10 | 10.0% | 未开始（🟡1） |
| L3 上下文装配 | 7 / 8 | 87.5% | 接近对齐 |
| L4 模型适配 | 1 / 13 | 7.7% | **有风险**（🟡4 全是「已实现未接线」） |
| L5 工具层 | 8 / 19 | 42.1% | 进行中（🟡2） |
| L6 工具集 / 注册 | 3 / 5 | 60.0% | 进行中（🟡2） |
| L7 Skill / curator | 9 / 18 | 50.0% | 进行中（🟡5） |
| **L8 MCP** | **6 / 10** | **60.0%** | **本轮从 0 跃到 60%**（🟡2，⬜2） |
| L9 记忆层 | 5 / 16 | 31.3% | 进行中（🟡2） |
| **L10 持久化 / 会话** | **5 / 15** | **33.3%** | 本轮 +3（🟡5，⬜5） |
| L11 cron | 3 / 6 | 50.0% | 进行中（🟡2） |
| L12 Gateway / 多平台 | 7 / 11 | 63.6% | 进行中（🟡3） |
| L13 插件 | 5 / 10 | 50.0% | 进行中（🟡4） |
| L14 安全 / 审批 | 5 / 10 | 50.0% | 进行中（🟡3） |
| L15 可观测性 | 4 / 6 | 66.7% | 进行中 |
| **L16 Web UI / 前端** | **4 / 6** | **66.7%** | 本轮 +1（16.5 skin） |
| **合计** | **83 / 181** | **45.9%** | — |

**没有任何一层为零了**——L8 从 0% 补到 60%，是这轮结构性变化最大的一层。

---

## 本轮抽查证据

**抽样率**：本轮新增 10 条 ✅ **复核 9 条（90%）**；你刻意标的 3 条 🟡 **全部复核**；4 个缺陷修复 **全部复核（4/4）**。

### A. 8.x / 10.x 的 ✅ 撑不撑得住 —— **9 条全撑得住，没有一条虚**

**① 8.1 stdio + Streamable HTTP → ✅✅ 两种 client 都是真的，不是只 import 名字**

```python
mcp.py:31   from mcp.client.stdio import StdioServerParameters
mcp.py:32   from mcp.client.streamable_http import streamable_http_client
mcp.py:289      url = str(entry.get("url") or "").strip()
mcp.py:290      if url:
mcp.py:294          return Client(streamable_http_client(url, http_client=http_client))   ← 分支一
mcp.py:295      command = str(entry.get("command") or "").strip()
mcp.py:296      if not command: raise ValueError("需要 command 或 url")
mcp.py:300          ... StdioServerParameters(command=command, ...)                        ← 分支二
```
**是真分支**（url → Streamable HTTP，command → stdio），不是两个 import 摆着。

**② 8.2 能力声明前置 → ✅✅ 两处都走 `_capability`**

```python
mcp.py:733  def _capability(client, name: str) -> bool:
mcp.py:741      caps = client.server_capabilities           ← 读 initialize_result
mcp.py:743      return bool(dumped(caps).get(name))         ← 拿不到按"不支持"返回 False
mcp.py:801  if _capability(client, "prompts"):              ← prompts
mcp.py:811  if _capability(client, "resources"):            ← resources
```
docstring 把理由写清了：「没声明的服务被调用会抛 `Method not found`，把它当成"空清单"处理会让"服务不支持"和"服务出错了"两件事混成同一个现象」。**prompts / resources 两处确实都走它** ✓

**③ 8.4 命名 `mcp__server__prompt` → ✅**

```python
mcp_prompts.py:64  def prompt_skill_name(server, prompt) -> str:
mcp_prompts.py:71      return f"mcp__{sanitize(server)}__{sanitize(prompt)}"[:MAX_SKILL_NAME]   # 64
mcp_prompts.py:86  def parse_prompt_skill_name(name) -> tuple[str, str] | None:                 # 逆函数
```
命名与 `app/mcp.py` 的 `tool_name()` 同构，且有逆解析 ✓

**④ 8.5 resources 复用现有 RAG（不是自造向量库）→ ✅✅**

实际代码调用（非仅 docstring）：
```
mcp_resources.py:46   from app.indexing import chunks_for_resource
mcp_resources.py:356  chunks = chunks_for_resource(uri, body, server)
mcp_resources.py:360  # 先删旧块：FtsIndexer 按 (origin, hash) 判重，只写不删会把旧版本永久留在索引里
mcp_resources.py:451  async def search_resources(retriever, query, *, limit=5)
mcp_resources.py:458  ``retriever`` 是 ``app.retrieval.FtsRetriever``（生产里由 …）
```
**关键反证：`pyproject.toml` 里 `lancedb|chromadb|faiss|qdrant|milvus|pinecone` 全部零命中** → 没有偷偷引入新向量库，确实复用既有 `FtsIndexer`/`FtsRetriever` ✓

**⑤ 8.6 心跳 + 失败宽限期 → ✅（深度略浅，如实标注）**

`mcp.py:13-15` 模块 docstring 明确写了「each one runs its own heartbeat… a probe that flaps must not…（见 `HEARTBEAT_GRACE_SECONDS`）」，`:519-522` 有 `graceful = await self._supervise(name, client)`。**我核到了机制存在与宽限常量的引用，但没逐个核宽限秒数与退避序列的数值**——这条我只做到"结构成立"，标为**已核 80%**。

**⑥ 8.9 `mcp serve` → ✅**

`mcp_server.py:95 def to_mcp_tools(specs) -> list[McpTool]` 是纯函数（只做 schema 转换，不起进程），`:186 build_server()` 负责真正起 stdio server ✓ 与 MATRIX「测试只测转换，不真起进程」一致。

**⑦ 10.2 id 游标跨重启续号 → ✅✅ 有专门用例**

```python
session_plane.py:137   """JSONL 追加日志：单行一条 JSON，``id`` 单调递增且**跨进程/重启续号**。"""
session_plane.py:247   def append(self, record):
session_plane.py:250       ident = self._last_id_locked() + 1        ← 不用内存计数器
session_plane.py:206   def _last_id_locked(self):
session_plane.py:207       tail = self._last_id_from_tail()          ← 先读文件尾
session_plane.py:211       tail = self._last_id_from_scan()          ← 尾巴不干净则全扫
```
**你问的"新建实例 id 回到 0"用例确实在**：
- `tests/test_session_plane.py:59 test_id_continues_after_restart` ✓
- `tests/test_session_plane.py:77 test_id_continues_past_broken_lines` ✓（尾巴有坏行也能续上）

**⑧ 10.7 孤儿回收 → ✅✅ 判据与"不 kill"都有断言**

`orphan_reaper.py` 模块 docstring 直接就是你要的答案：
- `:10`「**判据：`(pid, host_start_time)` 二元组，不是 pid**」；`:12` 解释 PID 复用会把"别人的进程"当成自己的
- `:36-38`「**绝不 kill 进程** … 本模块**只清登记**，不 `taskkill`、不 `SIGTERM`、不发信号」
- `:24-28` 三态分类：`dead`（孤儿，reap 清登记）/ `reused`（PID 复用，报告"身份不符"，reap 也清登记）/ `alive`（什么都不做）

用例 16 条，其中直接对应你两个问题的：
- **`test_module_never_kills_processes`(:260)** ← 直接断言"从不 kill"
- `test_pid_reuse_is_not_an_orphan`(:112)、`test_start_time_within_tolerance_is_alive`(:131)
- `test_dry_run_does_not_touch_the_ledgers`(:187)、`test_reap_removes_only_dead_entries`(:203)

**⑨ 16.5 skin → ✅✅ 3 皮肤 + NO_COLOR 双通道都有用例**

```
SKINS = 3 → ['default', 'mono', 'solarized']        TOKENS = 7
no_color_requested() 存在（env + flag 两个来源）
```
用例：`test_has_at_least_three_builtin_skins`(:62) · `test_no_color_env_degrades_every_token_to_empty_string`(:99) · `test_no_color_flag_degrades_every_token`(:107) · `test_no_color_requested_sources`(:112) · `test_cli_skin_no_color_flag_degrades`(:242) ✓

> **未复核的 1 条**：10.6（delivery ledger）。上轮它是在飞产物且失败过（`test_written_rows_carry_gateway_field_names` / `test_pending_keeps_only_the_latest_row_per_key`），本轮它出现在 ✅ 且全量绿。**我没有单独复核它的实现**，只确认了测试全绿——如实标为未核。

### B. 你刻意标的 3 条 🟡 —— **三条都标得准，一条不宽也不窄**

**① 8.3 OAuth → 🟡 正确**

- `OAuthStore`(:933) 的 docstring 自己就写：「元数据 → PKCE → 本地回调 HTTP 服务器 → 换 token）**没有做**」
- 四态实现在 :1012-1027：`missing` / `expired` / `expiring` / `valid`（`:1014` 解释 expiring 与 valid 为何分开）
- `orbit mcp auth login --help` → 「走完整 OAuth 授权流程（**本轮未实现，如实报错**）」；带假服务名跑 → exit 1「没有名为 'nosuchserver' 的 MCP 服务。已配置：demo」（先校验再报未实现）

**不抬 ✅ 是对的**：真 OAuth 流程（PKCE/回调/换 token）确实没有，本机也没法验证。

**② 10.1「统一 session 平面」→ 🟡 标得准，既不宽也不窄**（你专门问的那条）

| 维度 | 实测 | 判定 |
|---|---|---|
| 读侧：三源合并 | `session_plane.py:313 _SOURCE_RANK = {plane:0, message:1, task_event:2}`；`:26-28` docstring「读侧仍然读 `app.sqlite`：`messages` 与 `task_events` 是既有真相源，平面不复制它们，只在 `timeline` 里合并成一条时间线」 | ✅ **统一读视图成立** |
| 读侧用例 | `test_timeline_merges_three_sources_in_time_order`(:180) · `test_timeline_limit_keeps_the_newest`(:218) · `test_timeline_does_not_leak_other_sessions`(:228) | ✅ 有测试 |
| **写侧：谁在写 plane** | `delivery_ledger.py` **2 处**、`session_plane.py` 自身 + `plane` CLI；**`cron.py` / `gateway.py` / `agent.py` / `graph/nodes.py` / `main.py` 全部 = 0 命中** | ⚠️ **交互循环、cron、gateway 都不写 plane** |
| 同库同表 | 不是：plane 是 `<data>/sessions/plane.jsonl`，messages/task_events 在 `app.sqlite` | ❌ 不满足 |

**结论：🟡 恰当，不是标窄了（不该是 ✅），也不是标宽了（不该是 ⬜）。**
- 不该 ✅：Hermes 的 10.1 原文是「**同库同表**」，Orbit 是"跨库统一读视图"；且写侧只有运维动作（delivery/prune）写 plane，**交互、cron、gateway 三个入口都不写**——"统一平面"目前是"统一读取"而非"统一写入"。
- 不该 ⬜：读侧合并是真的、有排序（`_SOURCE_RANK`）、有游标（10.2）、有测试，这是实打实的价值。
- **建议补一句证据**：「写侧目前只有 delivery_ledger 与 plane CLI 写入；cron/gateway/agent 均未写入」——这是 🟡 定级的真正理由，比"不是同库同表"更硬。

**③ 10.4 压缩谱系 → 🟡 正确**

- `SessionPlane.compress()`(:353) 存在，写 `parent_session_id` + `compressed_at` + `reason`
- `lineage()` 有循环检测 + `MAX_LINEAGE_DEPTH` 兜底
- **阈值确实没接**：全仓 grep `0.50` / `0.20` / `target_ratio` 在 `session_plane.py` 只命中 `def compress` 一行；`graph/*.py` 与 `agent.py` 里**只有** `nodes.py:139 context_meta.setdefault("compressed", {})`（空占位）

**「谱系有、阈值未接主循环」属实 → 🟡 正确。**

### C. 4 个缺陷修复 —— **4/4 全部真实修好**

| # | 修复 | 代码证据 | 判定 |
|---|---|---|---|
| 1 | `_table.Spec.build` 中间节点 | `claimed = HANDLERS.get(path)` → 有则 `handler, state = claimed, "done"`（支持"裸命令+子命令并存"）；`elif children:` → `states == {"done"}` 则 done、`states & {"done","partial"}` 则 partial、否则 todo；注释点名了 `dashboard` 与 `skin` 两个受害案例 | ✅✅ 真修，且两处 bug 都在注释里留了档 |
| 2 | `lineage` 的 `current = parent` | 现在是 `up: str \| None = parents.get(current)`，尾部注释：「必须走 `up`。这里早先写的是 `current = parent`——`parent` 是上面建 `parents` 映射时留下的循环变量…结果每条谱系都在第二步就自我循环，被误判成"脏数据"直接报错」 | ✅✅ 真修，且**把坑写进了注释** |
| 3 | `listing_signature` 取第一个就返回 | 现在 `parts` 收集 `meta`+`annotations` 的 5 个键（etag/ETag/lastModified/last_modified/updated_at）+ `size`，`"\|".join(parts)`；docstring 写明旧 bug（「etag 没变但 size 变了完全看不出来」）与代价（「升级后第一次全量重建，一次性成本」） | ✅✅ 真修，代价如实披露 |
| 4 | `worktree` 的 `list[str]` 被遮蔽 | `worktree.py:47 Names = list[str]`（模块级别名）；`:41-44` 注释：「在类体里写 `list[str]` 会被解析成那个方法而不是内置类型，mypy 直接报 valid-type」；`:314` / `:323` 已改用 `Names` | ✅✅ 真修 |

### D. 一处措辞要改（8.8 举错例子）

`orbit mcp` 实测 **17 个子命令**，`state=done` 的 **14 个**（serve/add/remove/list/test/configure/login/reauth/enable/disable/reload/resources/prompts/auth）——**数字 14/17 与 MATRIX 一致** ✓。

但 MATRIX 8.8 的举例是「`login` / `reauth` 如实报未实现」，**这两个实际是 `state=done` 且有真 handler（`run_login` / `run_reauth`）**；真正 `state=todo` 的是 **`picker` / `catalog` / `install`**（实测 `orbit mcp catalog` → exit 3「尚未实现」）。
→ 建议改成：「17 条里实现 14 条；`picker`/`catalog`/`install` 未实现（exit 3）；`auth login`/`reauth` 命令已实现但 **OAuth 流程本身**如实报未实现」。**count 没错，举例错了。**

---

## 与目标标志的差距

### Top 10 未完成项

| # | 未完成项 | 现状 | 量级 |
|---|---|---|---|
| 1 | **L4 模型适配（8 ⬜ + 4 🟡）** | **4 条 🟡 全是「库层有、运行时没接」**：fallback 链 / credential pool / provider 钩子 / transport。已连续 3 轮排第一类风险 | 12 条待补 |
| 2 | **L9 记忆层（9 ⬜ + 2 🟡）** | 缺 USER.md 用量自曝头、原子批处理 `operations[]`、熔断、8 个 memory provider、写入门禁、漂移防护、CJK FTS | 11 条待补 |
| 3 | **L5 工具层（9 ⬜ + 2 🟡）** | 29 工具 vs Hermes 94；缺 code_execution、媒体生成、视觉、TTS、kanban、cronjob、桌面窗口、协作消息、HomeAssistant、元工具 | 差 65 个工具 |
| 4 | **L2 核心循环（8 ⬜ + 1 🟡）** | 子 Agent 已落地但零消费；缺 13 类错误分类、心跳判卡死、中断配对、压缩重试、后台学习回路 | 9 条待补 |
| 5 | **L1 入口 / CLI（6 ⬜ + 2 🟡）** | `serve` / `dashboard` / `desktop` / `gui` / `acp` / TUI / 全局 23 flag | 8 条待补 |
| 6 | **L7 Skill 剩余（4 ⬜ + 5 🟡）** | 缺 hub 分发、bundles、sync 三方合并、`skills_guard` 107 条威胁模式、curator 剩 8 子命令 | 9 条待补 |
| 7 | **L10 持久化剩余（5 ⬜ + 5 🟡）** | 缺 `active=0 AND compacted=1` 归档、`system_prompts` 内容寻址、存活租约、双 FTS、`hosted_room_*` 事件溯源；🟡 里 10.1 写侧未统一 | 10 条待补 |
| 8 | **L13 插件剩余（1 ⬜ + 4 🟡）** | 缺 dashboard 插件契约、懒发现、`all`/`pack` CLI | 5 条待补 |
| 9 | **L12 剩余（1 ⬜ + 3 🟡）** | 19 个平台适配器全是占位；缺 `gateway` 11 子命令全量、peer、monitoring 导出 | 4 条待补 |
| 10 | **L14 剩余（2 ⬜ + 3 🟡）** | 缺 tirith、egress 代理、`orbit hooks` CLI、`path_security`/`url_safety` 四件套 | 5 条待补 |

---

## 风险与漂移

### R1 🟡 **8.8 的举例错位（count 对、例子错）**

见 D 节：`login`/`reauth` 是 done，未实现的是 `picker`/`catalog`/`install`。建议改措辞，避免下轮有人去找"login 未实现的证据"而困惑。

### R2 🟡 **10.1 的 🟡 理由可以更硬**

现在理由写的是「不是同库同表」。**更硬的理由是写侧**：`cron` / `gateway` / `agent` / `graph/nodes.py` / `main.py` 对 plane 的引用数**全是 0**，只有 `delivery_ledger` 与 `plane` CLI 在写。建议补进证据栏——这决定了 10.1 短期不可能变 ✅（要变 ✅ 得让三个入口都写 plane）。

### R3 🟡 **L4 的 4 条 🟡 已连续 3 轮无人动**

fallback / credential pool / provider 钩子 / transport 四条 🟡 从第 4 轮就是这个状态（"库层有、运行时没接"），本轮 W7/W8/W11/W12 全部绕过了 L4。**这是全表最稳定的一块欠账**，且性价比最高（`agent.get_model()` 里接一次 `FallbackChain` 能把 4.5/4.6 同时翻 ✅）。

### R4 🟢 **上轮的两个教训已记入本报告（按你要求）**

**教训 1：本机不能并发跑重活。**
- 你的观测：`import app.mcp` 看似挂死（faulthandler 停在 `get_data`），实际是 pytest + mypy 后台任务压满 CPU，独占重跑 2.1 秒。
- **我本轮提供了旁证**：同一套全量测试，上轮（4 子agent 在跑）**711s**，本轮（独占）**127.52s** —— **5.6 倍差**。所以"跑得慢/看似挂起"的第一诊断永远是"先看有没有并发重活"，不是"代码有问题"。
- 我在第 5 轮也踩过同一类：想跑"只含已跟踪测试文件"的基线，>15 分钟未结束只好中止；本轮独占后 127s 就跑完了。

**教训 2：PowerShell 会吃掉反引号。**
- 你的观测：用 `python -c` 改含反引号的 Markdown 行会**静默改不动**（踩了两次），要改用脚本文件。
- **我这边同族的坑**：PowerShell 对 native command 的 stderr 处理会伪造退出码——我在前几轮多次看到 `--help` 后面跟着 `[exit code: 1]`，逐个单独复核后**全是 0**；本轮 `orbit mcp auth login --help` 也显示 exit 2 但那是"缺 NAME 参数"的真实 argparse 错误。**结论：凡"退出码异常"，先在干净的单命令里复核一次，再下结论。**

### R5 🟡 **未验证项（如实标注，不许当通过）**

| 项 | 为什么没验 |
|---|---|
| 10.6 delivery ledger 实现 | 只确认测试全绿，**未单独读实现**（上轮它是在飞产物且失败过） |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用，**未逐个核数值** |
| `orbit chat` 交互式会话 | 会起 REPL 并可能调模型 → 未运行 |
| `gateway start` / dispatcher 实际互斥 | 会起服务、写 `dispatcher.lock` → 未运行 |
| `cron run` / 投递 | 会真执行任务、写 `runs.jsonl` → 未运行 |
| `backup create` / `restore` | 会写 zip、改数据目录 → 未运行 |
| `config edit` / `doctor --fix` / `doctor --live` | 拉编辑器 / 写盘 / 真发请求 → 未运行 |
| `matrix_stats.py` 端到端 exit 1 | 只读固定 MATRIX.md、无路径参数；实跑需改禁改区（第 4 轮起一直是这个状态） |

---

## 下一轮建议

按优先级：

1. **【P0】保住"干净基线"这个新资产**：本轮 1374 passed / 1 failed / 1 skipped（127.52s）是**第一次可信基线**。建议把它写进 `MATRIX.md` 附注或 `ROADMAP.md`，并规定"每轮收口必须独占跑一次"。上轮 711s vs 本轮 127s 的对比就是理由。
2. **【P0】L4 的 4 条 🟡 该动手了**：连续 3 轮无人碰，且是"库层有、运行时没接"这一类。在 `agent.get_model()` 里接一次 `FallbackChain`，**一条改动能同时把 4.5/4.6 翻回 ✅**，还能顺手把 `credential_pool` 的 429 熔断接上。这是全表 ROI 最高的一处。
3. **【P1】修 8.8 的举例**（`login`/`reauth` → `picker`/`catalog`/`install`），并补一句「`auth login` 命令已实现但 OAuth 流程如实报未实现」。
4. **【P1】给 10.1 补"写侧"理由**：把「`cron`/`gateway`/`agent`/`nodes`/`main` 对 plane 引用数 = 0，只有 `delivery_ledger` 与 `plane` CLI 在写」写进证据栏。这既解释了为什么是 🟡，也给出了变 ✅ 的路径。
5. **【P1】L9 记忆层是下一个大块**：9 ⬜ + 2 🟡，且缺的都是**可离线验证、测试好写**的东西（USER.md 用量头、`operations[]` 原子批处理、3 次失败熔断、写入门禁、漂移防护）。Orbit 已有 memories 表 + USER.md 底座，性价比高于 L5（要新增 65 个工具）。
6. **【P2】把 `picker`/`catalog`/`install` 三条 todo 收掉**：`catalog` 可以做成"读 optional-mcps 目录"的只读命令（顺带解掉 8.10），`picker` 可做成交互选择。三条加起来能把 8.8 从 🟡 推到 ✅。
7. **【P2】给每条 ✅ 补「验证命令」列**：本轮 9 条复核里有 6 条是"读源码 + 对常量 + 看用例名"，只有 8.1/8.5/8.8 有可一键复跑的命令。补上后下轮监视不必重读源码。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 4 轮未核（`Tool.confirmation` / `granted()` / `budget["summary_mark"]` / `context_meta["compressed"]` 等）。注意 `nodes.py:139` 的 `context_meta.setdefault("compressed", {})` 我这轮顺带看到**仍是空占位**——正是那 15 条里的一条。

---

*本报告由 monitor 角色只读生成。本轮新增 10 条 ✅ 复核 9 条（90%），你点名的 3 条 🟡 全部判为**标得恰当**（10.1 尤其：不宽不窄，但理由该换成"写侧未统一"）；4 个缺陷修复 **4/4 全部真修**。未验证项单列，不混进完成项。所有数字可按上文命令复现。*
