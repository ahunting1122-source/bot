# 对标进度报告

**更新时间**：2026-10-27 23:58（本机实测时钟，只读取证）
**本轮轮次**：**N = 3**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑 `config set` / `doctor --fix` / `config edit` / `doctor --live` / `cron tick` 等会改状态或真发请求的命令。全量 pytest 已按授权运行（只读）。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `2485a6b`，工作树干净）

---

## 总体完成度

### **42 / 181 项 = 23.2%**

| 项 | 说明 |
|---|---|
| 清单来源 | `MATRIX.md` 主清单，脚本逐行解析表行：**181 行**（L1–L16） |
| 完成 | **✅ 42 条**（本轮逐条复核，见下） |
| 部分 | 🟡 **13 条**（不计分） |
| 未开始 | ⬜ **126 条** |
| 与表头一致性 | MATRIX 表头写「181 条 / ✅42 / 🟡13 / ⬜126」——**与我的脚本统计逐字一致** ✅（上轮的表头错账已修） |

### 本轮变化：**新增完成 22 条，主动降级 2 条，净 +20**

| 类别 | 条目 |
|---|---|
| **新增 ✅（22）** | 1.4 1.5 1.6（命令面已登记）· 3.1 3.2 3.3（三层 prompt / 项目文件 / SOUL）· 5.1（遮蔽门禁）· 5.4 5.5 5.6（三层结果治理 / 输出上限 / read_file 钉死）· 6.3 6.4（`orbit tools` / 注册暴露分离）· 9.7 9.8 9.9（四形态 / 零 LLM / 参数）· 10.10 10.15（backup·import / checkpoints）· 11.1 11.2 11.4（cron 存储调度 / CLI / 语义）· 15.2 15.3（insights / monitoring） |
| **主动降级 ✅→🟡（2）** | **4.5 fallback 链**、**4.6 credential pool**——理由：「未接线运行时，`agent.get_model()` 仍是唯一出模型的地方」。**我已独立复核：`grep FallbackChain\|CredentialPool\|fallback` 于 `agent.py` / `graph/nodes.py` / `main.py` = 0 命中，降级诚实。** |
| **辅助口径** | `REGISTRY` **75 顶层 / 411 展平**（硬口径 ✓）；状态 `done 82 · partial 3 · todo 326` |

> **本轮一句话**：这一轮的实物质量**明显高于前两轮**——新增的 6 个模块全部有真实接线（不是空壳），且实现方**主动把两条 ✅ 降级**、把 5.15 从「8 域」改成「7 域」并写明「`ask_user` 不是元工具」。上轮我点名的 4 个问题**全部被修**。剩余最大风险不在代码，在 **ROADMAP 的波次状态表已经和实物脱节**。

---

## 分层完成度

按 MATRIX 表行脚本统计：

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI 层 | 7 / 17 | 41.2% | 进行中（🟡1：真实现 82/411） |
| L2 Agent 核心循环 | 1 / 10 | 10.0% | 未开始（仅预算刹车） |
| L3 上下文装配 | 5 / 8 | 62.5% | 进行中 |
| L4 模型适配 | 1 / 13 | 7.7% | **有风险**（🟡4，全部「已实现未接线」） |
| L5 工具层 | 8 / 19 | 42.1% | 进行中（🟡1） |
| L6 工具集 / 注册 | 2 / 5 | 40.0% | 进行中（🟡2） |
| L7 Skill / curator | 0 / 18 | 0% | 未开始 |
| L8 MCP | 0 / 10 | 0% | 未开始 |
| L9 记忆层 | 3 / 16 | 18.8% | 进行中（🟡1） |
| L10 持久化 / 会话 | 2 / 15 | 13.3% | 进行中（🟡1） |
| L11 cron / 自动化 | 3 / 6 | 50.0% | 进行中（🟡2） |
| L12 Gateway / 多平台 | 0 / 11 | 0% | 未开始 |
| L13 插件层 | 0 / 10 | 0% | 未开始 |
| L14 安全 / 审批 | 3 / 10 | 30.0% | 进行中（🟡1） |
| L15 可观测性 | 4 / 7 | 57.1% | 进行中 |
| L16 Web UI / 前端 | 3 / 6 | 50.0% | 进行中 |
| **合计** | **42 / 181** | **23.2%** | — |

**三层仍是零**：L7 Skill（18 条）、L12 Gateway（11 条）、L13 插件（10 条）= 39 条，占全部未完成项的 31%。

---

## 本轮抽查证据

**抽样率**：42 条 ✅ 中**直接读代码/跑命令复核 20 条 = 47.6%**（要求 ≥30%）；13 条 🟡 复核 6 条；⬜ 复核 1 条（11.5）。另外上轮已验证的 5 个文件（`approvals.py` / `providers.py` / `web.py` / `fallback.py` / CLI `approvals.py`）**mtime 未变**（23:21–23:25），上轮结论继续有效。

### A. 你点名的 6 条，逐条对账

**① 5.4 三层结果治理 → ✅ 真接线，不是只 import**

```
nodes.py:26   from app.budget import TurnBudget
nodes.py:551  projected = self.policies.tool_results.project(call["name"], result)   ← 工具执行步骤
nodes.py:562  turn = TurnBudget(data_dir=getattr(self.settings, "data_dir", None))   ← 真实创建
nodes.py:623  turn = (getattr(self, "_turn_budgets", None) or {}).get(task_id)
nodes.py:624  meta = await self.policies.tool_results.record(..., turn=turn, ...)     ← 真传进 record
nodes.py:634  if turn is not None and not state.get("calls"): turn.reset()            ← 轮末复位
```
`policies.py:19` `from app.budget import Cut, govern` → `:298 governed = govern(name, body, data_dir=..., turn=turn, config=...)` → `:299 body = governed.text` → `:311-315` 回填 `spill_path` / `cut` / `degraded` 三个观测字段（既有键未动）。

**判定：✅ 成立。** `TurnBudget` 在工具结果处理步骤按 `task_id` 缓存（注释说明为何不塞 state：checkpoint 序列化会破坏恢复），**不是 import 了没用**。
常量也逐条对上：`budget.py` `DEFAULT_RESULT_SIZE=100_000` / `DEFAULT_TURN_BUDGET=200_000` / `DEFAULT_PREVIEW_SIZE=1_500` / `DEFAULT_MCP_RESULT_SIZE=50_000` / `SPILLOVER_MAX_AGE_HOURS=24` / `DEFAULT_MAX_BYTES=50_000` / `DEFAULT_MAX_LINES=2_000` / `DEFAULT_MAX_LINE_LENGTH=2_000` / `TERMINAL_HEAD_RATIO=0.4`。

**② 6.4 注册 vs 暴露分离 → ✅ 接口成立，但「无差异化消费点」**

- `tools.py:814 def specs(self, platform: str = toolsets.DEFAULT_PLATFORM)` → `:821 exposed = set(toolsets.resolve(list(self.tools), platform, self.toolsets_config()))` ← **真的走 `toolsets.resolve`**
- 实证逐名相等：`toolsets.resolve(10 个样例名, 'cli', {})` → 返回 10 个，`sorted(out) == sorted(names)` = **True**，差集空 ✅
- `DEFAULT_PLATFORM = "cli"`（`toolsets.py:37`）；`declared_toolsets()` = 13 个（core/files/shell/browser/vault/notion/github/memory/history/retrieval/skills/web/mcp）
- **但**：`graph/nodes.py:320` 是 `self.registry.specs()` —— **不传 platform，恒用默认 cli**；全仓无任何调用点传非 cli 平台。

**判定：✅ 成立（接口 + 语义 + 回归都对），但需在证据栏补一句「运行时恒为 cli 全集，per-platform 收窄尚无消费方」**——否则下轮容易被读成"已在多平台生效"。这与上轮 `ToolExposer` 空转是同一类 seam 问题，只是这次过滤逻辑是真的。

**③ 5.1 遮蔽门禁 → ✅ 逐条符合 Hermes 语义**

`tools.py:_may_register()`（无异常路径，返回 bool）：
```python
if tool.name.startswith(MCP_TOOL_PREFIX): return True        # mcp__ 豁免
owner = existing.source if existing else self._shadows.get(tool.name, "")
if not owner or owner == tool.source: return True            # 同 source 允许重复注册
if not override:
    log.error("工具 %s 已被 %s 注册，来自 %s 的同名注册被拒绝…")  # 只 log，不抛
    return False
if not self._override_allowed(tool.source):
    log.error("工具 %s 的 override=True 被拒：%s 未获操作员授权…")  # 两道闸分开
    return False
log.info(...); return True
```
- 跨 source 同名**默认拒绝且不抛异常**（`log.error` + `return False`）✅
- `override=True` **且** `data/tools.json` 的 `allow_tool_override` 授权才放行（`tools.py:789`）✅
- `unregister_source` 留墓碑，槽位空了也认主 ✅（`self._shadows`）

**判定：✅ 精确，与你的描述逐条一致。**

**④ 9.6 CJK → 🟡 降级诚实**

`app/session_search.py:21-29` 模块 docstring 明写：
> **中文检索为什么不用 FTS5**：`unicode61` 分词把整段中文当一个 token，`MATCH '记忆'` 命中 **0**；trigram 那一路能命中，但 Orbit 的 `messages` 表**没有 FTS5 索引**，而迁移文件是禁改区（本轮不许加表）。所以走**子串匹配** `lower(content) LIKE '%词%'`。

- 实测 `grep 'fts|FTS|MATCH|trigram'` 于 `session_search.py` → 只有**注释**提到 FTS，无任何 FTS 表/查询 ✅
- 转义与限流都在：`_escape_like` / `_like_pattern`（`%`/`_` 转义）、`DEFAULT_SCAN=300` / `MAX_SCAN=1_000`
- **判定：🟡 降级诚实且理由充分。** 唯一可补的是「库到十万条量级会慢」已写在 MATRIX 里 ✅

**⑤ 11.5「与交互共享 session 平面」标 ⬜ → 诚实**

`cron.py:5-6` 明写：「**落盘用 JSON，不建数据库表**。任务定义进 `<data_dir>/cron/jobs.json`，每次执行一行进 `<data_dir>/cron/runs.jsonl`。迁移文件是禁改区…」；`cron.py:467-477` `CronStore.root = Path(data_dir) / "cron"`。
→ **cron 确实跑在独立存储上，不碰 `app.sqlite`**，11.5 标 ⬜ 正确。

**⑥ 3.1 三层 prompt → ✅ 真被调用，顺序正确**

```
context.py:69  def build_stable(settings)          # SOUL.md + agent.md
context.py:78  def build_context_tier(settings, *, memory_text, profile_block)
context.py:90  def build_volatile_tier(settings, *, file_access, summary)
context.py:170 async def build_context(...)
context.py:223     build_stable(settings),
context.py:224     build_context_tier(settings, memory_text=..., profile_block=...),
context.py:225     build_volatile_tier(settings, file_access=..., summary=...),
```
**顺序 Stable → Context → Volatile，且三层都被 `build_context` 调用** ✅。`tests/test_context_tiers.py` 收集 **10 条**（与 MATRIX 一致）。
- 3.3：`load_soul` 先数据目录后仓库根（`:36`），并拼在 Stable 最前（`:75 f"{soul}\n{SYSTEM}"`）✅
- 3.2：`PROJECT_FILES = ("AGENTS.md", "ORBIT.md")`，走到 `.git` 止步（`:64`）✅ —— 注意 Orbit 用 `ORBIT.md` 替代 Hermes 的 `HERMES.md`，**是有意改名，MATRIX 已如实写**

### B. 其余复核项（代码级）

| 条目 | 证据 | 判定 |
|---|---|---|
| 5.5 | `budget.py` `DEFAULT_MAX_BYTES=50_000` / `DEFAULT_MAX_LINES=2_000` / `DEFAULT_MAX_LINE_LENGTH=2_000` / `TERMINAL_HEAD_RATIO=0.4` / `DEFAULT_HEAD_RATIO=0.7` | ✅ 精确 |
| 5.6 | `budget.py:62 PINNED_THRESHOLDS = {"read_file": math.inf}`；`:157` 优先级 pinned > overrides > mcp_ > default；`:548 if not math.isinf(threshold)` | ✅ 精确 |
| 6.3 | `orbit tools {list,enable,disable,post-setup}` 实测存在 | ✅ 精确 |
| 9.7/9.9 | `DEFAULT_LIMIT=3 / MAX_LIMIT=10 / DEFAULT_WINDOW=5 / MAX_WINDOW=20 / WINDOW_MAX_CHARS=4_000 / BOOKEND_MAX_CHARS=1_200 / DEFAULT_SCAN=300 / DEFAULT_BOOKEND=3 / READ_HEAD=20 / READ_TAIL=10` | ✅ 与 MATRIX 逐字一致 |
| 9.8 | 模块不 import 任何模型/连接器；`_COMPACTION_PREFIXES` 剔除压缩载荷（对齐 Hermes #43175） | ✅ 零 LLM 成立 |
| 10.10 | `backup.py:121 src.backup(dst)`（SQLite 在线备份 API）；`:99 _sha256`；`:9` docstring 明写防 zip slip；`MANIFEST_VERSION=1`；`dry_run` | ✅ 精确；`test_backup.py` 19 条 ✓ |
| 11.1 | `CronStore`（jobs.json + runs.jsonl，原子写 `_atomic_write`）/ `CronSpec`（`30m`/`every 2h`/5 字段，自实现）/ `CronRunner.tick` / `next_run` / `is_due` | ✅ 成立（**形态差异见 R4**） |
| 11.4 | `Job` 有 `skills` / `script` / `no_agent`（= Hermes `--no-agent`）字段 | ✅ 字段级成立 |
| 15.2/15.3 | `orbit insights --days --source`、`orbit monitoring status` 实测；`test_observability.py` 4 条 | ✅ 精确；MATRIX 已标注 `--source` 口径分歧（Hermes 平台维度 vs Orbit 站点维度）——**主动披露，加分** |
| 4.5/4.6 降级 | `grep FallbackChain\|CredentialPool\|fallback` 于 `agent.py`/`nodes.py`/`main.py` = **0 命中**；出模型处仍只有 `agent.py:282 ChatOpenAI` + `summaries.py:165` + `providers.py:801` | ✅ **降级诚实** |
| 5.15 降级 | 「已有 7 域…缺 12 域…**元工具**（`ask_user` 是交互门不是元工具；`todo`/`clarify`/`tool_search` 均不存在，见 5.12/5.13）」 | ✅ **正是上轮 R3 的修法**，已改对 |

### C. 测试基线

```
$ pytest -q
1 failed, 681 passed, 1 skipped, 1 warning in 96.34s (0:01:36)
```
- 唯一失败 = `tests/test_connectors.py::test_browser_append_and_reload`（`assert 'error' == 'ok'`）→ **基线既有，不计回归** ✓
- **本轮未观察到 `test_graph.py` 的时间类偶发失败**（隔离与全量均未见），记为「未复现」，不下「负载敏感」的结论
- 新测试收集数：`test_cron` **65** · `test_toolsets` **35** · `test_session_search` **34** · `test_budget` **26** · `test_backup` **19** · `test_context_tiers` **10** · `test_setup` **9** · `test_observability` **4**
- `--help` 退出码逐个复核（`monitoring`/`tools`/`insights`/`cron`/`sessions`/`backup`/`checkpoints`/`session-search`）= **全部 0**（PowerShell 里看到的 `[exit code: 1]` 是管道假象，已排除）

---

## 与目标标志的差距

目标标志 = 「Hermes 有的 Orbit 都有，**逐项有验证证据**」。23.2% 之下，剩下 126 条 ⬜ 里 **39 条（31%）集中在三个整层为零的层**。

### Top 10 未完成项

| # | 未完成项 | 现状证据 | 量级 |
|---|---|---|---|
| 1 | **L7 Skill / curator（18 条全 ⬜，条目数最多的一层）** | 有技能底座（3 条手写）；缺多根 tier、16 字段 frontmatter、渐进披露、写侧校验链、ledger、curator 状态机、hub/bundles/sync | 18 条 |
| 2 | **L12 Gateway / 多平台（11 条全 ⬜）** | `grep \bgateway\b` 仍 0；Hermes 22 适配器 + 4 状态文件 | 整层；阻塞 cron 投递（11.3 🟡）与 send/peer/webhook |
| 3 | **L13 插件（10 条全 ⬜）** | `grep plugin` = 0；Hermes 18 组 / 103 provider / 19 种 `register_*` | 整层 |
| 4 | **L9 记忆层剩 12 ⬜** | 已有 `session_search` 四形态；缺 MEMORY/USER 字符预算、冻结快照、原子批处理、8 个 provider 接口、写入门禁、漂移防护 | 12 条 |
| 5 | **L10 持久化剩 12 ⬜** | 已有 backup/import + checkpoints；缺统一 session 平面、`id` 游标、压缩谱系、幂等投递账本、孤儿回收、`orbit sessions` 19 子命令 | 12 条 |
| 6 | **L8 MCP（10 条全 ⬜）** | stdio/HTTP + 生命周期加固已有；缺 OAuth、prompts→技能、resources→RAG、`orbit mcp` 11 子命令 | 10 条 |
| 7 | **L5 工具层剩 10 ⬜** | 29 工具 vs Hermes 94；缺 code_execution、媒体生成、视觉、TTS、kanban、cronjob、桌面窗口、协作消息、HomeAssistant、元工具 | 差 65 个工具 |
| 8 | **L4 模型层 8 ⬜ + 4 🟡** | **最需要注意的是 4 🟡 全是「已实现未接线」**：fallback 链、credential pool、provider 钩子、transport——库层有、运行时没用 | 8 + 4 |
| 9 | **L2 核心循环 9 ⬜** | 子 Agent 仍零消费（`policies.subagents` 引用 0）；缺 13 类错误分类、心跳判卡死、中断配对、压缩重试、后台学习回路 | 9 条 |
| 10 | **L1 剩 9 ⬜** | `chat` / `serve` / `dashboard` / `desktop` / `gui` / `acp` / `console` / TUI / oneshot `-z` 全无 | 9 条 |

---

## 风险与漂移

### R1 🟠 **ROADMAP 波次总表已与实物脱节（本轮最大漂移，6 处状态过期）**

| 波次 | ROADMAP 写 | 实测 | 偏差 |
|---|---|---|---|
| W2 | 「74 顶层 / 407 展平」「实现 **54/407**」 | **75 / 411**；`done` **82** | 低估 28 条；数字全过期 |
| W4 | 「遮蔽门禁、toolset 过滤、结果治理、元工具**待做**」 | 遮蔽门禁(5.1) / toolset 过滤(6.4) / 结果治理(5.4/5.5/5.6) **全已 ✅** | **只差元工具**，严重低估 |
| W6 | 「⬜ 待做」 | `session_search` 四形态(9.7/9.8/9.9) **已 ✅** | 应为 🟡/进行中 |
| W8 | 「⬜ 待做」 | backup/import(10.10) + checkpoints(10.15) **已 ✅** | 应为 🟡 |
| W9 | 「⬜ 待做」 | cron 整层(11.1/11.2/11.4) **已 ✅** | 应为 🟡 |
| W12 | 「⬜ 待做」 | insights(15.2) / monitoring(15.3) / logs(15.1) **已 ✅** | 应为 🟡 |
| W3 | 「provider/fallback/auth **已落地**」 | 本轮 fallback/credential pool **已主动降级 🟡（未接线运行时）** | **高估**——波次表没跟上降级 |

**最刺眼的一条：同一个指标三个数字**——`MATRIX 1.16` 写「**46 / 407**」、`ROADMAP W2` 写「**54 / 407**」、`REGISTRY.parity_rows()` 实测「**82 / 411**」。三处都自称是「已实现比例」。

> 上轮我报的是 MATRIX 表头错账，这一轮 MATRIX 修好了，**ROADMAP 成了新的错账源**。建议把波次状态也交给脚本生成（`scripts/parity_report.py` 已有基础）。

### R2 🟠 **MATRIX 1.4 / 1.5 / 1.16 的计数落后于 REGISTRY**

| 条目 | MATRIX 写 | 实测 |
|---|---|---|
| 1.4 | 顶层 **74** 个独立命令 | **75**（新增 `session-search`） |
| 1.5 | 二级子命令 **298** 条 | **301** |
| 1.16 | **46 / 407** done | **82 / 411** |
| 1.6 | 三级 **35** 条 | **35** ✅ 这个对 |

（`75 + 301 + 35 = 411` 与 `parity_rows()` 完全吻合，说明只是没回填，不是统计口径差异。）

### R3 🟡 **6.4 的 ✅ 成立，但运行时恒为 cli 全集**

`graph/nodes.py:320` 调 `self.registry.specs()` **不传 platform**，全仓无非 cli 调用点。过滤层真实存在、cli 语义逐名相等（已实证），但**per-platform 收窄目前没有消费方**。建议证据栏补一句，别让下轮读成"已在多平台生效"。

### R4 🟡 **11.1 的 ✅ 成立，但与 Hermes 形态不同（未在证据栏披露）**

`cron.py:14` 明写：「**不常驻**。`CronRunner.tick` 是一次性的，由外部调度器（Windows 计划任务 / …）驱动」。
Hermes 侧有常驻 ticker 心跳（`cron/` 下 `.jobs.lock` + 心跳）。**这是有意的架构取舍（无常驻进程更省资源），但属于形态差异，应在 11.1 证据栏写明**——否则「调度器」一词会让人以为有 daemon。

### R5 🟡 **新增顶层命令 `session-search` 在 MATRIX 里没有独立行**

L9 只有 9.6–9.16 覆盖**机制**（四形态/参数/零 LLM），`orbit sessions` 在 10.13；**没有任何一行登记 `orbit session-search` 这条新命令本身**。命令面覆盖仍有洞（上轮 R6 同类问题的残留）。

### R6 🟢 **上轮我报的 4 个问题，全部已修（记录以免下轮重复报警）**

| 上轮风险 | 本轮实测 | 状态 |
|---|---|---|
| R1 MATRIX 表头「133/✅14/🟡0/⬜119」错账 | 表头 **181 / ✅42 / 🟡13 / ⬜126**，与我的脚本统计**逐字一致** | **已修** |
| R2 3.6 三个数值过期 | 已回填 **4,043 B / 16,895 B / 29 工具**，并加注「不要写死在这里，以命令输出为准」 | **已修** |
| R3 5.15「8 域」里的元工具无实据 | 改为 **7 域** + 明写「`ask_user` 是交互门不是元工具；`todo`/`clarify`/`tool_search` 均不存在」 | **已修** |
| R4 15.1「六选项全对齐」但目标 3/5 | 本轮未见补充说明（**仍未修**，见下） | ⬜ 残留 |

### R7 🟡 **未验证项（如实标注，不许当通过）**

| 项 | 为什么没验 |
|---|---|
| `orbit config edit` / `doctor --fix` / `doctor --live` | 会拉起编辑器 / 写盘 / 真发网络请求 → **未运行** |
| `orbit cron tick` / `cron run` | 会真的执行任务并写 `runs.jsonl` → **未运行**（只读了 `CronRunner.tick` 源码与 `--help`） |
| `orbit backup create` / `restore` | 会写 zip、改数据目录 → **未运行**（只读了 `backup.py` 实现与 19 条测试） |
| 15.1 的日志目标数 | 上轮报的「目标 3/5（缺 gui/desktop）」本轮**未再核**，仍按未修处理 |
| `test_graph.py` 负载敏感失败 | 本轮未复现 → 「未观察到」，不下结论 |

---

## 下一轮建议

按优先级（**先让账本追上实物，再补层**）：

1. **【P0】把 ROADMAP 波次状态表交给脚本生成**：现在 6 处状态过期、W3 还高估（fallback 已降级 🟡 但波次表仍写"已落地"）。同一指标出现 **46 / 54 / 82** 三个数字，必须统一到 `REGISTRY.parity_rows()` 的 `state` 分布。
2. **【P0】回填 MATRIX 1.4 / 1.5 / 1.16 的计数**：74→**75**、298→**301**、46/407→**82/411**。（1.6 的 35 是对的，不用动。）
3. **【P1】给 6.4 与 11.1 各补一句形态说明**：6.4「运行时恒为 cli 全集，per-platform 收窄无消费方」；11.1「无常驻 ticker，靠外部计划任务驱动」——两条都是**结论成立但容易被误读**的类型。
4. **【P1】补 `orbit session-search` 的矩阵行**（并顺手补上轮遗留的 15.1 日志目标 3/5）。
5. **【P1】下一波建议做 W5（Skill / curator，18 条）**：它是剩余最大单层，且 Orbit 有技能底座（文件真值 + 表索引 + 四道闸），是**最容易把 ⬜ 批量变 ✅ 且测试好写**的一层。W12 剩余（dashboard 页面/skin/TUI）体量小，可顺手清掉。
6. **【P1】把「已实现未接线」当成一类独立风险来处理**：L4 的 4 条 🟡 全是这个病（fallback / credential pool / provider 钩子 / transport 库层有、运行时没用）。建议在 `agent.get_model()` 里真正接一次 `FallbackChain`，这一条能把 4.5/4.6 同时翻回 ✅，性价比最高。
7. **【P2】每条 ✅ 加一列「验证命令」**：本轮 42 条里多数证据是「文件:函数名」，只有少数可一键复跑。目标标志要求「逐项有验证证据」，建议加 `orbit approvals list-rules | grep -c mkfs` 这类可执行断言。
8. **【P2】下轮复核 `08` 的「15 条已声明未消费」**：本轮仍未核其余 14 条（`Tool.confirmation` / `granted()` / `budget["summary_mark"]` / `context_meta["compressed"]` 等）是否随本批施工接线。

---

*本报告由 monitor 角色只读生成。42 条 ✅ 复核 20 条（47.6%）、13 条 🟡 复核 6 条，未为好看加分；未验证项单列，不混进完成项。所有数字可按上文命令复现。*
