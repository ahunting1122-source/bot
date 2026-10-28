# 对标进度报告

**更新时间**：2026-10-28 00:52（本机实测时钟，只读取证）
**本轮轮次**：**N = 5**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑 `config set` / `doctor --fix` / `config edit` / `doctor --live` / `cron run` / `gateway start` / `backup restore` 等会改状态或真发请求的命令。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `10bde7f`；**工作树不干净**：1 项已跟踪修改 + 27 项未跟踪，全部是 4 个在飞子agent 的产物）

---

## 总体完成度

### **73 / 181 项 = 40.3%**

| 项 | 说明 |
|---|---|
| 清单来源 | `MATRIX.md`，脚本逐行解析：**181 行**（L1–L16），**编号唯一 181/181** |
| 完成 | **✅ 73 条**（本轮新增 4 条：3.4 / 3.5 / 6.5 / 14.3——**4 条全部逐条复核**） |
| 部分 | 🟡 **29 条** |
| 未开始 | ⬜ **79 条** |
| 表头一致性 | MATRIX 表头「181 / ✅73 / 🟡29 / ⬜79 / 40.3%」——**与我的脚本统计逐字一致** ✅ |

### 先确认你上轮的更正：**全部为真，我上轮引的是旧快照**

| 你更正的项 | 当前文件实测 | 结论 |
|---|---|---|
| MATRIX 1.5 写 `== 417` | 实测 `parity_rows() == 417`；且 1.5 的「307 条二级」也与实测一致（75 + 307 + 35 = 417） | ✅ **已修** |
| MATRIX 1.17 写 `137 / 417 + 9 partial + 271 todo` | `REGISTRY` 实测 `done 137 · partial 9 · todo 271`，合计 417 | ✅ **已修，逐字一致** |
| 6.4 加「恒为 cli 全集」注 | 证据栏末尾确有「**注意**：`graph/nodes.py` 调 `specs()` 时没传 platform，全仓没有非 `cli` 的调用点」 | ✅ **已加** |
| 11.1 加「无常驻 ticker」注 | 证据栏末尾确有「**注意**：没有常驻 ticker——靠外部调度器调 `orbit cron tick`」 | ✅ **已加** |
| 15.1 补「日志目标 3/5」 | 证据栏确有「**日志目标覆盖 3/5**：agent/errors/gateway 有真实文件，`gui`/`desktop` 缺失（Orbit 没有原生桌面壳）」 | ✅ **已加** |
| 12.5 采纳我的纠错 | 已改为「真实适配器 **3 个**：`a2a`/`ntfy`/`raft`…另有 2 个 Orbit 自有本地适配器 `console`/`file`（不在 22 枚举内）」 | ✅ **已改，与代码一致** |

> 我上轮报的 5 条残留风险，**这一轮全部关闭**。抱歉引了旧值——教训记下：**引用 MATRIX 具体行时必须当轮读取，不能沿用上一轮的文本**。

---

## 分层完成度

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | 10 / 18 | 55.6% | 进行中（🟡2） |
| L2 Agent 核心循环 | 1 / 10 | 10.0% | 未开始（W11 在飞） |
| L3 上下文装配 | **7 / 8** | **87.5%** | 接近对齐（本轮 +2：3.4 / 3.5） |
| L4 模型适配 | 1 / 13 | 7.7% | **有风险**（🟡4 全是「已实现未接线」） |
| L5 工具层 | 8 / 19 | 42.1% | 进行中（🟡1） |
| L6 工具集 / 注册 | **3 / 5** | 60.0% | 进行中（🟡2，本轮 +1：6.5） |
| L7 Skill / curator | 9 / 18 | 50.0% | 进行中（🟡5） |
| L8 MCP | 0 / 10 | 0% | 未开始（W7 在飞） |
| L9 记忆层 | 5 / 16 | 31.3% | 进行中（🟡2） |
| L10 持久化 / 会话 | 2 / 15 | 13.3% | 未开始（W8 在飞） |
| L11 cron | 3 / 6 | 50.0% | 进行中（🟡2） |
| L12 Gateway / 多平台 | 7 / 11 | 63.6% | 进行中（🟡3） |
| L13 插件 | 5 / 10 | 50.0% | 进行中（🟡4） |
| L14 安全 / 审批 | **5 / 10** | 50.0% | 进行中（🟡3，本轮 +1：14.3） |
| L15 可观测性 | 4 / 6 | 66.7% | 进行中 |
| L16 Web UI / 前端 | 3 / 6 | 50.0% | 未开始（W12 在飞） |
| **合计** | **73 / 181** | **40.3%** | — |

**唯一整层为零**：L8 MCP（10 条）——但 W7 子agent 正在做，产物未提交。

---

## 本轮抽查证据

**抽样率**：本轮新增 4 条 ✅ **全部复核（4/4 = 100%）**；另复核 2 个 bug 修复 + 硬口径 3 项 + 在飞产物的接线状态（12 个新模块）。

### A. 4 条新 ✅，逐条判定（结论：**4 条全部撑得住，没有一条虚**）

**① 14.3 密钥脱敏（🟡 → ✅）→ ✅✅ 抬得对，但有一个边界要补**

接线实测：
```
tools.py:1356   from app import redact
tools.py:1358   return json.dumps(redact.redact_obj(result), ensure_ascii=False, default=str)   ← as_tool_text
policies.py:295 from app import redact
policies.py:297 body = redact.redact(body).text                                                 ← record
```
行为实测（非只看调用）：
```
redact('OPENAI_API_KEY=sk-proj-abcdefghijklmnop1234 以及普通文本')
  → 'OPENAI_API_KEY=sk-p…1234 以及普通文本'      # 首 4 末 4，中段打码
  hits → [{'kind': 'openai_key', 'count': 1}]
redact_obj({'a': {'b': [secret, 1, None]}, 'c': 'PATH=/usr/bin'})
  → {'a': {'b': ['sk-p…1234', 1, None]}, 'c': 'PATH=/usr/bin'}   # 递归 + 非密钥不动
PATTERNS = 16 类 ✓（MATRIX 写 16，与代码一致；上轮的 15 已更新）
```
**测试是否只断言"函数被调用"？→ 不是，是真的往返。** `test_redact_wiring.py:84 test_record_persists_redacted_text` 的断言链：
```python
await DefaultToolResultPolicy().record(..., text=f"$ env\nOPENAI_API_KEY={SECRET}\nPATH=/usr/bin")
rows = await store.rows(select(ToolResult).where(ToolResult.operation_id == "op-1"))
stored = rows[0].text
assert SECRET not in stored                     # 落库后读回已脱敏
assert "sk-p" in stored and "6789" in stored     # 首尾保留
assert "PATH=/usr/bin" in stored                 # 非密钥内容没被动
```
→ **从 store 读回验证，是真断言**。另有 `test_as_tool_text_stays_valid_json` 断言嵌套 `[1, True, None]` 结构不被破坏（即"先脱敏 dict 再序列化"这条设计真的生效）。
6 条用例全在：`test_as_tool_text_redacts_a_secret` / `..._stays_valid_json` / `..._redacts_nested_values` / `..._leaves_harmless_text_alone` / `..._survives_non_string_values` / `test_record_persists_redacted_text`。

**判定：✅ 成立，抬到 ✅ 不早。** ⚠️ 但要补一句边界（见 R3）：只接了**两条**出口（模型侧 `as_tool_text` + 落库侧 `record`），**日志 / 事件负载 / `dump` 未覆盖**。MATRIX 现措辞"已接线到两条独立出口"是**如实的**，不是虚报——只是别让人读成"全局脱敏已完成"。

**② 3.5 技能索引 60 字符预算 → ✅✅ 精确**

```
nodes.py:280  def render_skills(self, skills: list[dict]) -> str:
nodes.py:292  from app.skill_index import index_description
nodes.py:300  summary = index_description(skill.get("description") or "")
nodes.py:302  triggers = (skill.get("triggers") or [])[:2]
nodes.py:303  summary = "；".join(triggers)          ← 描述缺失退回触发词
```
截断实测：`INDEX_DESCRIPTION_LIMIT = 60` / `INDEX_DESCRIPTION_KEEP = 57`；120 字符输入 → **60 字符输出，结尾 `'长的技...'`**（57 + `...`）；短描述原样保留；空串 → 空串。
测试：`test_skill_index_injection.py` **7 条**（与 MATRIX 一致），含 `test_description_is_used_and_truncated_to_the_index_budget`(:30)、`test_missing_description_falls_back_to_triggers`(:49)、**`test_body_is_never_included`(:62)**、`test_index_description_helper_is_the_single_source_of_truth`(:77)。

**③ 3.4 记忆冻结快照 → ✅✅ 用例真的在断言"逐字节不变"**

```python
async def test_system_prefix_is_frozen_across_steps_of_one_task(tmp_path):   # tests/test_graph.py
    ...
    assert len(model.prefixes) >= 3, "两步工具调用 + 一次收尾，至少 3 次模型调用"
    assert len(set(model.prefixes)) == 1, "同一任务内系统提示必须完全一致"
```
`len(set(...)) == 1` = **逐字节相同**，且 `>= 3` 次模型调用保证它真的跨了多步（不是只调一次就"相等"）。实现侧：`context.py:244 FROZEN_PREFIX_KEY = "system_prefix"`、`:247 build_prefix()`、`:274 frozen_prefix()` ✓。
MATRIX 还主动标了差异「Orbit 每个任务冻一次，Hermes 每个会话冻一次」——**如实**。

**④ 6.5 工具 schema 校验 → ✅✅ 你踩过的那个坑确实修了**

```python
tools.py:501  def schema_problems(spec: dict) -> list[str]:
tools.py:510      （``{"type": "function", "function": {...}}``），裸形状（直接 ``{name, ...}``）
tools.py:517      candidate = spec.get("function")        ← 先拆 OpenAI 外壳
tools.py:878      problems = schema_problems(spec)
                  if problems:
                      log.error("工具 %s 的 schema 不合法，已从暴露面移除：%s", name, "；".join(problems))
                      continue                                    ← 排除并点名
```
两种形状实测（我直接调函数）：
```
裸形状 {'name','description','parameters':{'type':'object',...}}      → []
OpenAI 壳 {'type':'function','function':{...}}                        → []
缺 name                                                               → ['缺 name']
parameters.type = 'array'                                             → ["parameters.type 必须是 'object'，实际是 'array'"]
```
测试：`test_tool_schema.py` **10 条**，含 **`test_wrapped_and_bare_shapes_are_both_accepted`(:48)**（正是你说的那个坑）与 `test_specs_drops_the_broken_tool_and_keeps_the_rest`(:109)（坏工具被排除、好工具一个不少）、`test_every_builtin_tool_has_a_valid_schema`(:30)（15 个内置工具全合法）。

### B. 两个 bug 修复，都成立

**① `_attach_globals` 冲突跳过（不是致命）→ ✅**

```python
taken = {flag for option in command.options for flag in option.flags}
for action in global_parser._actions:
    if isinstance(action, argparse._HelpAction): continue
    if taken & set(action.option_strings): continue      ← 冲突就跳过
    parser._add_action(action)
```
docstring 把两头都写清了：直接 `parents=[global_parser]` 会 `argparse.ArgumentError: conflicting option string: --json` 让**整个命令树编译不出来**；不挂则 `orbit security audit --json` 报 `unrecognized arguments`。

实测：
```
$ orbit approvals allow list --json     → {"ok": true, "allow": []}    EXIT=0  ✓
$ build_parser()                        → OK（help 6,180 字符）        ✓ 没炸
```

**② `orbit skills check` exit 0 → ✅**

```
$ orbit skills check
体检 3 个技能（威胁模式库 30 条，Hermes 107 条的子集）
bili_video_detail  user  通过  警告 unknown-field:scope；警告 unknown-field:source
form_fill_login    user  通过  警告 unknown-field:scope；警告 unknown-field:source
web_search         user  通过  警告 unknown-field:scope；警告 unknown-field:source
另有 6 条 frontmatter 提示（不影响加载）。
EXIT=0   ✓
```
之前是 3 处硬问题 + 3 个技能"未加载"，现在 **3/3 通过、exit 0** ✓。
⚠️ 但仍有 `unknown-field:scope` / `unknown-field:source` 两条 warning：**Orbit 自家技能用了 16 字段白名单之外的两个键**。不阻断加载，但说明"自家数据 vs 自家校验规则"还有一道缝（见 R2）。

### C. 测试结果：**你给的 195 完全对上；全量数字本轮不可作为稳定基线**

| 跑法 | 结果 |
|---|---|
| **你动的 10 个测试文件** | **195 passed in 30.01s** ✅ **与你说的 195 完全一致** |
| 全量（第 1 次，00:30 左右） | **5 failed / 1190 passed / 1 skipped / 121.43s** |
| 全量（第 2 次，含新落地文件） | **6 failed / 1246 passed / 1 skipped / 711.24s** |
| `test_graph::test_ask_user_still_interrupts_and_resumes` 隔离跑 | **1 passed in 5.70s** ✅ |

**两次全量的测试总数从 1196 涨到 1253、耗时从 121s 涨到 711s**——这本身就是"跑的过程中有文件落地"的直接证据。所以**本轮全量数字不能当基线**。

失败逐条归类（**没有一条是已提交代码的回归**）：

| 失败 | 归属 | 依据 |
|---|---|---|
| `test_connectors::test_browser_append_and_reload` | **基线既有** | 连续 5 轮都失败，与你给的基线一致 |
| `test_console` ×2 | **在飞 W12 子agent** | `test_console.py` 是 `??` 未跟踪；`app/console.py` 未跟踪 |
| `test_delivery_ledger` ×2 | **在飞 W8 子agent** | `test_delivery_ledger.py` 是 `??`；`app/delivery_ledger.py` 未跟踪 |
| `test_session_plane` ×1 | **在飞 W8 子agent** | `test_session_plane.py` 是 `??` |
| `test_worktree` ×2 | **在飞 W11 子agent** | `test_worktree.py` 是 `??`；`app/worktree.py` 未跟踪 |
| `test_mcp::test_resource_index_failure_does_not_block_connect` | **在飞 W7 子agent** | `app/mcp.py` 是唯一 ` M`（已跟踪修改，正在改） |
| `test_graph::test_ask_user_still_interrupts_and_resumes` | **负载敏感，非回归** | **隔离跑 1 passed in 5.70s** ✓ |

**在飞产物确实"未接线"（我逐个查了 12 个新模块在 `main.py` / `commands/__init__.py` / `agent.py` / `graph/nodes.py` 的引用数）**：

| 模块 | 引用数 | 模块 | 引用数 |
|---|---|---|---|
| `session_plane` | **0** | `metrics` | **0** |
| `delegate` | **0** | `orphan_reaper` | **0** |
| `delivery_ledger` | **0** | `skin` | **0** |
| `mcp_prompts` | **0** | `swarm` | **0** |
| `mcp_resources` | **0** | `worktree` | **0** |
| `mcp_server` | **0** | `console` | 1（仅在 `ui.py` 里，`ui.py` 自身未注册） |

它们只互相 import（`swarm ↔ delegate ↔ worktree`、`ui → console/skin/metrics`），**没有任何一条进主链路**。`REGISTRY` 里也没有 `session-plane` / `ui` / `delegate` / `swarm` ✓ —— **与你的说法完全一致**。

> ⚠️ **未能完成的一项**：我想跑一次"只含 HEAD 已跟踪测试文件（55 个）"的干净基线，机器被 4 个子agent 压满，>15 分钟未结束，我中止了。**该项如实标为未验证**，不猜数字。

---

## 与目标标志的差距

### Top 10 未完成项

| # | 未完成项 | 现状 | 备注 |
|---|---|---|---|
| 1 | **L8 MCP（10 条全 ⬜）** | 唯一整层为零 | W7 在飞（`mcp_prompts`/`mcp_resources`/`mcp_server` 已写但 0 引用） |
| 2 | **L10 持久化 / 会话（12 ⬜ + 1 🟡）** | 缺统一 session 平面、`id` 游标、压缩谱系、幂等投递账本、孤儿回收、`orbit sessions` 19 子命令 | W8 在飞（`session_plane`/`delivery_ledger`/`orphan_reaper` 已写但 0 引用） |
| 3 | **L2 核心循环（9 ⬜）** | 子 Agent 仍零消费；缺 13 类错误分类、心跳判卡死、中断配对、压缩重试 | W11 在飞（`delegate`/`swarm`/`worktree` 已写但 0 引用） |
| 4 | **L4 模型适配（8 ⬜ + 4 🟡）** | **4 条 🟡 全是「库层有、运行时没接」**：fallback 链 / credential pool / provider 钩子 / transport | 全表性价比最高的一处 |
| 5 | **L5 工具层（10 ⬜ + 1 🟡）** | 29 工具 vs Hermes 94；缺 code_execution、媒体生成、视觉、TTS、kanban、cronjob、桌面窗口、协作消息、HomeAssistant、元工具 | 差 65 个工具 |
| 6 | **L9 记忆层（9 ⬜ + 2 🟡）** | 缺 USER.md 用量自曝头、原子批处理、熔断、8 个 provider、写入门禁、漂移防护、CJK FTS | — |
| 7 | **L7 剩余（4 ⬜ + 5 🟡）** | 缺 hub 分发、bundles、sync 三方合并、`skills_guard` 107 条威胁模式、curator 剩 8 子命令 | — |
| 8 | **L13 剩余（1 ⬜ + 4 🟡）** | 缺 dashboard 插件契约、懒发现、`all`/`pack` CLI | — |
| 9 | **L12 剩余（1 ⬜ + 3 🟡）** | 19 个平台适配器全是占位；缺 `gateway` 11 子命令全量、peer、monitoring 导出 | — |
| 10 | **L16 剩余（3 ⬜）** | dashboard 18 页面语义、`skin`、browse 页 | W12 在飞 |

---

## 风险与漂移

### R1 🟠 **工作树有 28 项在飞改动，本轮"测试基线"不可采信**

`git status`：**1 项已跟踪修改（`app/mcp.py`）+ 27 项未跟踪**。全量测试两次跑出不同总数（1196 → 1253）、不同耗时（121s → 711s），证明**跑的过程中确实有文件落地**。
- 影响：本轮任何"全量 N passed"的数字都**不能当基线**；下一轮必须以"子agent 全停 + 工作树干净"为前提重跑一次。
- 建议：把"跑基线前先 `git status` 确认干净"写进流程；或给 `matrix_stats.py` / 测试脚本加一个"工作树脏则警告"的前置检查。

### R2 🟡 **自家技能用了自家白名单外的字段（`scope` / `source`）**

`orbit skills check` 现在 exit 0 ✓，但 3 个技能各报 2 条 warning：`unknown-field:scope`、`unknown-field:source`。
16 字段白名单（`ALLOWED_FRONTMATTER_FIELDS`）里没有这两个键，而 Orbit 自己的技能数据在用。**不阻断加载**，但这是"校验规则 vs 实际数据"的口径缝隙——建议要么把这两个键纳入白名单（它们确实是 Orbit 的多源化设计），要么从数据里去掉。

### R3 🟡 **14.3 的 ✅ 需要补一句出口边界**

脱敏只接了**两条**出口：`tools.as_tool_text`（模型侧）+ `policies.record`（落库侧）。
**未覆盖**：日志（`logging_setup` 写盘的内容）、`task_events` 事件负载、`orbit dump` 的快照。
MATRIX 现在写"已接线到两条独立出口"是**如实**的（没说"全局"），但建议再加半句"其余出口（日志/事件/dump）未覆盖"，避免下轮被读成脱敏已闭环。

### R4 🟡 **未验证项（如实标注，不许当通过）**

| 项 | 为什么没验 |
|---|---|
| "只含 HEAD 已跟踪测试文件"的干净基线 | 机器被 4 子agent 压满，>15 min 未结束，**已中止** |
| `orbit chat` 交互式会话 | 会起 REPL 并可能调模型 → 未运行 |
| `gateway start` / dispatcher 实际互斥 | 会起服务、写 `dispatcher.lock` → 未运行 |
| `cron run` / 投递 | 会真执行任务、写 `runs.jsonl` → 未运行 |
| `backup create` / `restore` | 会写 zip、改数据目录 → 未运行 |
| `config edit` / `doctor --fix` / `doctor --live` | 拉编辑器 / 写盘 / 真发请求 → 未运行 |
| `matrix_stats.py` 端到端 exit 1 | 只读固定 MATRIX.md、无路径参数；实跑需改禁改区（上轮已说明，仍未变） |

### R5 🟢 **上轮我报的 5 条风险，本轮全部关闭**

| 上轮风险 | 本轮实测 | 状态 |
|---|---|---|
| MATRIX 1.5 写 411 | 现写 `== 417`，与实测一致 | **已修** |
| MATRIX 1.17 写 83/411 + 3 partial | 现写 `137/417 + 9 partial + 271 todo`，逐字一致 | **已修** |
| 6.4 缺「恒为 cli 全集」 | 证据栏末尾「**注意**」段已加 | **已修** |
| 11.1 缺「无常驻 ticker」 | 证据栏末尾「**注意**」段已加 | **已修** |
| 15.1 缺「目标 3/5」 | 已写「**日志目标覆盖 3/5**：…`gui`/`desktop` 缺失」 | **已修** |
| 12.5 平台名错（console/file/webhook） | 已改为 `a2a`/`ntfy`/`raft` + 说明 console/file 是自有适配器 | **已修** |

---

## 下一轮建议

按优先级：

1. **【P0】子agent 全停后，先跑一次干净基线再动任何数字**：现在工作树 28 项在飞、全量测试总数在跑动中从 1196 涨到 1253。建议先 `git status` 确认干净 → 跑全量 → 记下 `N passed / 1 failed / 1 skipped` 作为**本轮唯一基线**。否则下轮的完成度又会被"文件在飞"污染。
2. **【P0】4 个子agent 接线时，把"接线证据"写进矩阵行**：`session_plane`/`delegate`/`mcp_server` 等 12 个新模块当前在 `main.py`/`commands/__init__.py`/`agent.py`/`nodes.py` 的引用数**全是 0**。接线完成后，每个 ✅ 的证据栏必须写清"哪个文件第几行真正调用它"——**这是本轮唯一被证伪过的风险类型**（上轮 14.3 就是"库层有、没接线"）。
3. **【P1】给 14.3 补出口边界**（R3）：加半句"日志 / 事件负载 / dump 未覆盖"。这是**如实性**问题，不是能力问题。
4. **【P1】修 `scope`/`source` 的口径缝隙**（R2）：把这两个键纳入 `ALLOWED_FRONTMATTER_FIELDS`（它们是 Orbit 多源化设计的真实字段），或从 3 个技能数据里移除——否则 `orbit skills check` 永远带 6 条 warning。
5. **【P1】L4 的 4 条 🟡 一次性解决**：fallback / credential pool / provider 钩子 / transport 全是"库层有、运行时没接"。在 `agent.get_model()` 里真接一次 `FallbackChain`，**一条改动能把 4.5/4.6 同时翻回 ✅**，是全表性价比最高的一处。
6. **【P2】给"在飞产物"加一个隔离开关**：本轮 4 个失败文件（test_console / test_delivery_ledger / test_session_plane / test_worktree）都是未跟踪文件。建议 pytest 配置里加 `--ignore` 白名单或 `collect_ignore` 机制，让"在飞代码的失败"不污染基线读数。
7. **【P2】每条 ✅ 加一列「验证命令」**：本轮 73 条 ✅ 里，14.3 / 3.5 / 6.5 三条的证据质量很好（有往返断言、有形状实测）；建议把这种"可一键复跑"的断言补到其余条目，下轮监视就不必重读源码。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 3 轮未核（`Tool.confirmation` / `granted()` / `budget["summary_mark"]` / `context_meta["compressed"]` 等）。

---

*本报告由 monitor 角色只读生成。本轮新增 4 条 ✅ **全部复核（4/4）**，结论：**4 条全撑得住，没有一条虚**；14.3 从 🟡 抬到 ✅ **抬得不早**，但建议补一句出口边界。未验证项（含被中止的干净基线跑）单列，不混进完成项。所有数字可按上文命令复现。*
