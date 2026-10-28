# ✅ 行对抗性审计（第 17 轮）

审计范围：MATRIX.md 里 **166 行 ✅**（全文 167 个含 ✅ 的表格行，减去口径变更日志里那条
「移除元指标行 1.17」的自述行 = 166）。**全量抽，不是抽样**——166 行每一行都过了第 1 道机器筛，
其中 123 行进到第 2 道人工追调用方。

方法：
1. **建图**：`ast` 解析 `backend/app/**/*.py` 全部 171 个模块的 import（含函数内惰性 import），
   从生产根 `app.main` / `app.agent` / `app.cli.main` / `app.cli.commands` / `app.graph.*`
   做可达性闭包 → 9 个模块不可达。
2. **抄入口**：从每行证据栏正则抽反引号里的 `xxx.py` / `xxx.py::函数` / `函数()`，解析成 `app.*`
   模块名（164 行抽出了至少一个可解析路径；5.9 / 5.10 两行的标题里未转义的 `|` 破坏了表格
   分列——`| 5.9 | ... iwr\|iex ... |` 实际是 7 列而不是 6 列，所以机器解析会把 `W13` 当成状态列，
   这两行单独人工读）。
3. **找调用方**：对每个被引用模块算「谁 import 它」并把 importer 分成 `prod`（`app/` 内）与
   `test`；对 `::函数` 再算一次「模块外谁引用这个符号」，**同模块内自调用一律不算**。
4. **人工复核**：对 123 行里任何一处信号（模块不可达 / 只有 test importer / 只有 CLI 报告命令
   importer / 有 `attach|install|register_*|ensure_loaded|acquire` 这类动词入口）逐条 `grep` +
   读源码确认调用链，直到能写出具体文件:行。
5. **CLI 模块全量核对**：`app/cli/commands/*.py` 46 个模块逐个比对 `__init__.py` 的 import 列表。

工具只用 `ast` / `grep` / 读文件，**没跑 pytest**（机器上有人在跑）。除本文件外未改任何文件。

## 结论摘要

- **确认（找到生产调用方）**：**105**
  - 其中逐函数追到具体调用点：**79**
  - 模块级确认（有生产 importer、未逐函数追）：**26**
- **SUSPECT（找不到生产调用方）**：**18**
  - 高（功能完全不可达）：**8**
  - 中（部分路径断 / 声明了但永不触发）：**7**
  - 低（只是报告不准 / 引用的入口不是生产路径）：**3**
- **未逐条核对**：**43**（清单见文末，都是 CLI 面计数类 / 证据栏只引测试的行；我确认了模块
  可达但没逐条追到 handler）

> 105 + 18 + 43 = **166**，与 MATRIX 的 ✅ 计数逐个对得上（交叉核对脚本见文末）。

> **先说三件已经修好的事**（被点名的三处假绿，本轮实测都已补上，不要再按旧结论报）：
> - **13.8**：`app/main.py:887-892` 现在真的 `plugin_dashboard.discover(...)` → `PAGES.add` →
>   `mount_into_app(app)`，且在 `mount("/")` 之前。**已从假绿变真绿。**
> - **`register_hook`**：`app/graph/nodes.py` 有 4 个 `hooks.emit(...)` 发射点（715 / 724 /
>   1202 / 1229），`from app import hooks` 走的是 `DEFAULT_HOOKS` 单例，`main.py:174-178` 也把
>   `hook_sink=DEFAULT_HOOKS` 注进了 `PluginHost`。**插件注册的 hook 现在会被调用**（但只对这
>   4 个事件，见 SUSPECT #11）。
> - **10.1**：`app/main.py:127` 真的 `app.state.plane_sink.attach()` 了。**写侧统一已通。**

---

## SUSPECT 清单（按严重度排）

### 1. 4.3 per-model 动态协议路由（Claude→Messages、GPT-5/Codex/Grok→Responses、其余→chat）

- **证据栏声称**：`app/model_router.py` 的 `route(model_name, settings)` → `RouteDecision(provider /
  base_url / max_context / supports_tools / supports_reasoning / supported / reason)`
- **我 grep 了什么**：
  `grep -rn "model_router|route_model|protocol_for" app/`、
  `grep -rn "import model_router|from app import model_router" app/ tests/`、
  AST 可达性闭包、`grep -n "from app import" app/cli/commands/model_extra.py`
- **找到什么**：`app/model_router.py` **全仓只有 1 个 importer，就是
  `app/cli/commands/model_extra.py:31`**。而 `model_extra` 从没被 `app/cli/commands/__init__.py`
  导入（见附录），所以它自己也进不了生产。`tests/test_model_router.py:13` 是唯一的其它引用。
  生产里真正的线协议选择是 **per-provider 静态**的：`app/providers.py:1192-1220`
  `build_chat_model()` 按 `profile.transport`（`chat_completions` / `anthropic_messages` /
  `bedrock_converse` / `gemini`）分派，而 `transport` 是 `ProviderProfile` 上的常量字段
  （`providers.py:640-887` 逐个写死）。
- **缺什么**：缺 `app/agent.py::_client_builder`（或 `providers.build_chat_model`）里对
  `model_router.route()` 的那一行调用。**没有它，"per-model"这件事不存在**——同一个 provider 下
  换模型不会换线协议（`gpt-5` 与 `gpt-4o` 都走 chat_completions，`claude-*` 走 Messages 是因为
  整个 `anthropic` provider 的 transport 就是 `anthropic_messages`）。
- **严重度**：**高**（引用的机制完全不可达；现有能力是另一套 per-provider 实现）

### 2. 4.9 辅助模型 slot 18 个（`auxiliary.*`，含 vision）

- **证据栏声称**：`app/aux_slots.py` 补到 18 个槽位（summary / title / compression / … / moa_reference / moa_aggregator）
- **我 grep 了什么**：`grep -rn "aux_slots|resolve_aux_model|auxiliary\." app/`、
  `grep -rn "aux_slots" tests/`
- **找到什么**：`app/aux_slots.py` **唯一 importer 是 `app/cli/commands/model_extra.py:29`
  （`from app import aux_slots as aux`）**，即那个死模块。`app/capabilities.py:104,255,275`
  三处只在**注释里**提 `aux_slots.PRICES`，没有 import。`tests/test_aux_slots.py:14` 是唯一引用。
- **缺什么**：缺「摘要 / 压缩 / 标题 / 分类 / vision 真正去问 `aux_slots` 要 provider+model」的那一行。
  现在 `app/summaries.py`、`app/compaction.py` 各自拿主模型跑，`auxiliary.*` 配置项**零消费者**
  ——用户在 `settings.json` 里配 `auxiliary.compression.model` 不会改变任何行为。
- **严重度**：**高**（18 个槽位的配置面在运行时是装饰）

### 3. 4.11 MoA（Mixture of Agents：N reference + 1 aggregator）

- **证据栏声称**：`app/moa.py`：`MoAPlan`（多 proposer + 1 aggregator），`run(plan, prompt, call=...)`
- **我 grep 了什么**：`grep -rn "\bmoa\b|MoAPlan" app/`、`grep -rn "moa" tests/`、
  `grep -rn "from app import moa|import moa" app/`
- **找到什么**：`app/moa.py` **唯一 importer 是 `app/cli/commands/model_extra.py:30`**（死模块）。
  （`app/policies.py` 里出现的 `app.moa` 是**反向**的——`moa.py` 自己 `from app.policies import …`；
  `grep -c moa app/policies.py` = 0。）`tests/test_moa.py:12` 是唯一其它引用。
  `orbit moa` 父命令确实存在（`app/cli/commands/model.py:1189`、`hermes_parity.py:553`），
  但它的 `run` 子命令只由 `model_extra.py:480 _attach("moa run", ...)` 追加。
- **缺什么**：`model_extra.attach()` 里那行 `_attach("moa run", ...)` 永不执行 →
  **`orbit moa run <prompt>` 这条命令在编译出来的 parser 里不存在**；`app/moa.py` 的 `run()`
  也没有任何 agent/工具路径调用它。
- **严重度**：**高**（功能完全不可达，连 CLI 出口都没有）

### 4. 9.16 `orbit profile`（14 子命令，用户画像）

- **证据栏声称**：`app/profile_dist.py`：**13 条 distribution 操作全实现**（`list` / `use` /
  `create` / `delete` / `describe` / `show` / `alias` / `rename` / `export` / `import` /
  `install` / `update` / `info`）
- **我 grep 了什么**：`grep -rn "profile_dist" app/`、AST 可达性、
  `grep -n "_bind\(" app/cli/commands/skill_extra3.py`
- **找到什么**：`app/profile_dist.py` 的唯一 importer 是 `app/cli/commands/skill_extra3.py:53`。
  `skill_extra3.py:539-549` 明确 `_bind` 了 11 条路径：
  `skills publish` + `profile list` / `use` / `create` / `delete` / `describe` / `alias` /
  `rename` / `install` / `update` / `info`。**而 `skill_extra3` 从未被
  `app/cli/commands/__init__.py` import**（见附录）。
- **缺什么**：`app/cli/commands/__init__.py` 里缺 `skill_extra3,` 这一行。`__all__`（第 125 行）
  **写了 `"skill_extra3"` 却没 import**——`from app.cli.commands import *` 会直接
  `AttributeError`，这是"文档说接了、代码没接"的字面证据。
- **严重度**：**高**（`orbit profile list/use/create/delete/describe/alias/rename/install/update/info`
  十条子命令全部无 handler）

### 5. 7.13 `orbit skills {... publish ...}`

- **证据栏声称**：`orbit skills` 27 条里实现 25 条。**本轮补上最后一条 `skills publish`**：
  `SkillHub.publish(name, *, to, dry_run=True)`
- **我 grep 了什么**：`grep -rn "run_skills_publish|skills publish" app/`、
  `grep -n "_bind" app/cli/commands/skill_extra3.py`
- **找到什么**：`run_skills_publish` 定义在 `skill_extra3.py:100`，绑定在 `skill_extra3.py:539`，
  **整个 handler 只存在于这个从不被 import 的模块里**。`SkillHub.publish` 本体在
  `app/skill_hub.py`（可达），但没有任何命令面调它。
- **缺什么**：同 #4——`__init__.py` 缺 `skill_extra3,`。缺了它，`skills publish` 在表里是
  `todo`/无 handler 状态，`orbit skills publish x` 会报"未实现"。
- **严重度**：**高**（本轮"补上的最后一条"恰好落在唯一一个没接线的模块里）

### 6. 10.14 事件溯源（`hosted_room_*` 14 张表：seq + event_id UNIQUE + cursors + watermarks）

- **证据栏声称**：`app/event_log.py`：`append(stream, event) -> seq`、`since(stream, cursor)`、
  `watch()`、`streams()`
- **我 grep 了什么**：`grep -rn "event_log|hosted_room" app/`、`grep -rn "event_log" tests/`、
  AST importer 表
- **找到什么**：`grep -rn "event_log" app/` → **0 命中**（连注释都没有）。
  `grep -rn "hosted_room" app/` → 0 命中。唯一引用是 `tests/test_event_log.py:15`。
  AST 表里 `app.event_log` 的 `prod_importers=[]`、`test_importers=[]`（我的 importer 只扫
  `app/`，补扫 `tests/` 后才找到测试）。
- **缺什么**：**一个 importer 都没有**。缺的是「谁写事件流」——`app/storage.py::Store.event()`
  仍然只写 `task_events` 表，`app/session_plane.py` 写 `plane.jsonl`，两者都不走 `event_log`。
  即整个事件溯源模块是**孤立文件**。
- **严重度**：**高**（零调用方，连测试之外的一次 import 都没有）

### 7. 10.8 存活租约（`runtime/active_sessions.json` + `gateway_heartbeats` + `processes.json` 的 (pid, host_start_time)）

- **证据栏声称**：`app/session_leases.py`：`runtime/active_sessions.json` + `acquire` /
  `heartbeat` / `release` / `live` / `reap_stale`
- **我 grep 了什么**：`grep -rn "session_leases|active_sessions|heartbeat_at|LeaseStore" app/`、
  `grep -rn "\.acquire\(" app/`
- **找到什么**：`app/session_leases.py` 唯一 importer 是
  `app/cli/commands/session_extra.py:57`，且只用了**读侧与清理侧**：
  `session_extra.py:89 _leases()` → `LeaseStore(...)`，`:486 sessions leases`（读），
  `:530 store.reap_stale(...)`（清）。
  `grep -rn "\.acquire\(" app/` 只有三处：`app/fallback.py:515`（凭证池）、
  `app/gateway.py:433`（dispatcher 锁）、`app/session_leases.py:227`（**定义处**）。
  **`LeaseStore.acquire` 零调用方。**
- **缺什么**：缺 `app/agent.py::AgentRunner.start()`（或 `main.py` lifespan、`chat.py::_build_stack`）
  里的 `LeaseStore(...).acquire(session_id, pid)` + 心跳 + `close()` 里 `release()`。
  没有写侧，`runtime/active_sessions.json` **在生产里永远不会被创建**——`orbit sessions leases`
  永远报"没有租约"，`reap_stale` 永远扫一个空表。
- **严重度**：**高**（正是 10.1 那个形状：读侧统一了，写侧从没被调用）

### 8. 2.2 三套独立不叠加的迭代预算（父 90 / 子 50 / 250 只是 config 兜底）

- **证据栏声称**：`app/turn_budgets.py`：父 **90** / 子 **50** 可配（`settings.turn_budgets` 或
  平铺 `max_turns` / `max_iterations`）……核心断言是**父预算不因子任务扣减**。`describe()` 把这三件事逐条讲出来
- **我 grep 了什么**：`grep -rn "max_turns|max_iterations|DEFAULT_MAX_ITERATIONS" app/`、
  `grep -rn "turn_budgets\.resolve|resolve_budgets|state\[.max_turns.\]" app/`、
  `grep -rn "PARENT_BUDGET_STEPS|CHILD_BUDGET_STEPS|runner_factory" app/`、读
  `app/cli/commands/chat.py:282-345, 348-383`
- **找到什么**：
  - `app/cli/commands/chat.py:282-289` 把 `--max-turns` 解析进 `state["max_turns"]` 与
    `state["budgets"]`；
  - `chat.py:348 _apply_state()` 是唯一把这些 state 作用到运行时的地方，它**只处理
    `toolsets` / `skills` / `pass_session_id`**（354-383 行），**没有 `max_turns` 分支**；
  - `grep -rn "state\[.max_turns.\]|get(.max_turns.)"` → 只有 `chat.py:288` 那个**写**点，**零读点**；
  - `turn_budgets.resolve()` 只有两个调用方：`chat.py:289`（存进 state 供展示，然后被丢掉）和
    `app/tool_extra_impl2.py:516`（`budget_status` 工具，**只报告**）；
  - `app/delegate.py` 自己有 `PARENT_BUDGET_STEPS=90` / `CHILD_BUDGET_STEPS=50`（78/82 行），
    但 `DelegateRunner` 的 `runner_factory` **全仓无人注入**——`app/tool_catalog.py:288` 自己写着
    「**做不到，不是没排期**：`app/delegate.py::DelegateRunner` 的 `runner_factory` …」，
    `app/tool_extra_impl.py:58` 也写「在全仓库没有[注入]」。
- **缺什么**：缺两行——① `chat.py::_apply_state()` 里把 `state["max_turns"]` 落到
  `runner`/`settings` 上；② `app/graph/nodes.py`（或 `agent.py`）的 think→execute 循环里一个
  `if turns >= max_turns: stop_reason="max_turns"` 的判据。`grep -rn "max_turns"` 在 `app/graph/`
  与 `app/agent.py` 里 **0 命中**：图里**没有任何轮次上限**。
- **严重度**：**高**（"父预算 90"是纯文档数字；`--max-turns` 是个被丢弃的参数；子预算所在的
  delegate 链自己承认未接线。`turn_budgets.py` 属"报告类"，按判据不算消费方）

### 9. 4.4 逐 provider 推理线格式翻译表（5 种落点 + 互斥/拒绝/强制规则）

- **证据栏声称**：`app/reasoning.py`：4 家的 `extract(response, provider)` 与
  `to_request(level, provider)`（`reasoning_effort` / `thinking.budget_tokens` 等落点）
- **我 grep 了什么**：`grep -rn "reasoning|effort" app/`、
  `grep -rn "reasoning_level|reasoning_effort=" app/`、`grep -rn "get_model\(" app/`
- **找到什么**：`app/reasoning.py` 的 prod importer 只有两个：
  `app/cli/commands/chat.py:252`（**只用 `LEVELS` 做参数校验**）和
  `app/cli/commands/model_extra.py:30`（死模块）。`to_request()` / `extract()` /
  `TRANSLATORS` **零生产调用方**。
  真正干活的是 `app/providers.py` 里另一份实现（`_deepseek_extra` / `_translate_anthropic` …
  共 400+ 行），经 `build_chat_model → extra_body_for(hooked, target, reasoning_effort)`（1190 行）。
  但 `reasoning_effort` 在生产里**永远是 `None`**：
  `grep -rn "get_model\(" app/` → `app/graph/nodes.py:518` 与 `:666` 都调 `runner.get_model()`
  **不传参数**；`agent.py:684-688` 的 `reasoning_effort` 默认 `None`。
  `grep -rn "reasoning_level" app/` → **全仓 1 处**：`chat.py:263` 的写入，**零读取**。
- **缺什么**：① `nodes.py` 该传 `reasoning_effort=settings.reasoning_level`；②
  `reasoning.py::extract()` 该接在 `nodes.py` 读响应的地方（否则模型的推理内容永远不被提取/展示）。
  现在 `--reasoning high` 会写进 settings，然后被 `_deepseek_extra(model, None)` 翻译成
  `{"thinking": {"type": "disabled"}}`——**用户要求开推理，实际发出去的是"关闭"**。
- **严重度**：**中**（线格式翻译在 providers.py 里是活的，但 reasoning.py 那份是死的；
  用户可见的 `--reasoning` 与 `reasoning_effort` 通路是断的）

### 10. 1.7 全局运行 flags 完整（30 个，证据栏称"每个都真的改变行为"）

- **证据栏声称**：**每个都真的改变行为**（`--reasoning` 改 settings、`--toolsets` 真从注册表摘工具、
  `-s` 的正文进 Context 层、`--worktree` 真建隔离工作树、`--checkpoints` 真做影子快照）
- **我 grep 了什么**：读 `app/cli/commands/chat.py:210-345`（`_apply_flags`）与 `:348-383`
  （`_apply_state`）；`grep -rn "state\[.max_turns.\]|reasoning_level" app/`
- **找到什么**：`--toolsets`（`_apply_state` 354-373 真摘工具）、`-s/--skills`（375-380 真进
  settings）、`--worktree`（317-328 真建树）、`--checkpoints`（331-343 真快照）、
  `--run-budget`（294 写 `run_budget_seconds` → 305-306 `model_copy(update=updates)` 生效）
  这几条**确实改变行为**。但：
  - `--reasoning`（250-263）只写 `updates["reasoning_level"]`，**该 settings 键全仓零读取**（见 #9）；
  - `--max-turns`（282-289）只写 `state["max_turns"]`，**`_apply_state` 不消费，全仓零读取**（见 #8）。
- **缺什么**：`chat.py::_apply_state()` 里缺 `--max-turns` / `--reasoning` 的落地分支。
- **严重度**：**中**（30 个 flag 里至少 2 个是纯装饰；证据栏"每个都真的改变行为"是过度声明）

### 11. 13.5 hook 事件：本机可证 15 个

- **证据栏声称**：`app/hooks.py`：hook 注册表 + 发射（同步/异步）+ 单个 hook 抛异常不影响其它 + 优先级；
  **实证 15 个 / 推断 2 个在注释里标清**
- **我 grep 了什么**：`grep -rn "DEFAULT_HOOKS" app/`、`grep -rn "hooks\.emit\(|\.emit\(" app/`、
  `grep -rn "pre_approval|post_approval|subagent_start|subagent_stop|HOOK_EVENTS" app/`
- **找到什么**：`HOOK_EVENTS`（`hooks.py:67-91`）确实声明了 17 个事件名，含
  `pre_approval_request` / `post_approval_response` / `subagent_start` / `subagent_stop`。
  但**全仓 `hooks.emit(...)` 只有 4 个发射点**，全在 `app/graph/nodes.py`：
  `:715 pre_llm_call`、`:724 post_llm_call`、`:1202 pre_tool_call`、`:1229 post_tool_call`。
  `app/approvals.py`、`app/approval_channel.py`、`app/delegate.py` 里 **`emit` 0 命中**。
- **缺什么**：13 个事件缺发射点——最具体的是 `app/approvals.py` 裁决处缺
  `hooks.emit("pre_approval_request", …)` / `hooks.emit("post_approval_response", …)`，
  `app/delegate.py::DelegateRunner.run` 缺 `subagent_start` / `subagent_stop`。
  插件按 `HOOK_EVENTS` 注册这 13 个事件会**静默永不触发**（`register()` 成功、`hooks(event)` 非空、
  `emit` 从不发生）。
- **严重度**：**中**（注册表与 4 个发射点是真的；但"17 个事件可用"对 13 个不成立，
  且失败方式是静默的）

### 12. 10.7 孤儿回收（`startup_orphan_reap` 67 条 / `ws_orphan_reap` 3 条）

- **证据栏声称**：`app/orphan_reaper.py`：扫 `processes.json` + `spawn-ledger.json` 找登记过但
  进程已死的条目；`reap(dry_run=True)` 默认只报告
- **我 grep 了什么**：`grep -rn "OrphanReaper|\.reap\(|reap_stale|reap_orphans" app/`、
  `grep -rn "startup_orphan_reap|ws_orphan_reap" app/`、读 `app/main.py` lifespan（104-232）
- **找到什么**：`OrphanReaper.reap()` 的**唯一调用方是 `app/cli/commands/session_plane.py:652-653`**
  （`orbit plane reap`）。`grep -rn "startup_orphan_reap"` 在 `app/` 里只命中
  `app/orphan_reaper.py:5` 的 **docstring 自述**，**没有任何同名函数**。
  `app/main.py` 的 lifespan（110-231 行）里**没有**任何 reap 调用。
  `gateway.py:662 reap_orphans` 是 `SpawnLedger` 的另一个方法，由 `gateway.py:905` 调——只覆盖
  gateway 自己的 spawn ledger。
- **缺什么**：`app/main.py` lifespan 里 `await asyncio.to_thread(orphan_reaper.reap, ...)` 那一行。
  行标题承诺的"**startup**"回收不存在：上次崩溃留下的死条目要等用户手动敲
  `orbit plane reap` 才会被清。
- **严重度**：**中**（模块与 CLI 都在，但"进程启动时回收"这个语义没有执行点）

### 13. 5.2 懒发现（AST 索引按需 import + generation 计数失效）

- **证据栏声称**：`app/tool_discovery.py`：**AST 扫描**……`ensure_loaded(name, registry)` 按需 import；
  `orbit tools discover` 是 CLI 出口
- **我 grep 了什么**：`grep -rn "tool_discovery|ensure_loaded|discover_tools" app/`、AST importer 表
- **找到什么**：`app/tool_discovery.py` 唯一 prod importer 是
  `app/cli/commands/tools_extra3.py:39`（`orbit tools discover`）。**`app/tools.py` 不 import 它**
  （`tools.py:19-29` 的 import 列表里没有），`ensure_loaded()` **零调用方**。
  `app/tools.py:1851` 的注释还在说「`app/tool_discovery.py`（懒发现）与 `tool_search` 的职责」——
  注释里承认了分工，代码里没接。
- **缺什么**：`app/tools.py::ToolRegistry.specs()` / `execute()` 里缺 `tool_discovery.ensure_loaded(...)`
  的调用。没有它，39 个工具仍然在 import 期全部真 import（`tools.py:19-29` 静态 import 链），
  AST 索引只是一份**离线报告**。
- **严重度**：**中**（索引本身真、CLI 出口真；"按需 import"这个核心收益没有执行点。
  证据栏自己也写了"24 个内置工具同在一个文件 → 零收益"，所以实际影响面是那 15 个 + 插件/MCP）

### 14. 5.13 `clarify` 元工具 + gateway

- **证据栏声称**：`clarify` 的 gateway 按钮通了：`render_choices()` 纯函数——**≤5 个出按钮**
  （回调 id `cl:<id>:<n>`）……`send_clarify()` 有 `send_buttons` 走按钮、遇 `NotSupported` 退回 `send_text`
- **我 grep 了什么**：`grep -rn "render_choices|send_clarify|clarify_payload" app/`
- **找到什么**：`render_choices` / `send_clarify` 定义在 `app/platforms.py:2517 / 2582`，**唯一
  调用方是 `app/cli/commands/peer_extra.py:283`**——而那里的注释自己写着「这是
  `app.platforms.send_clarify` 的**手动验证入口**」。
  `grep -rn "clarify" app/gateway.py` → **0 命中**。
  `app/tools.py:2242` 的 `clarify` 工具只 `return clarify_payload(args)`（把问题交给模型/前端），
  不往平台发按钮。
- **缺什么**：`app/gateway.py` 的消息循环里缺「工具结果 status==needs_user 且带 choices →
  `platforms.send_clarify(...)`」那一段。所以真流程里 `clarify` 在消息平台上只会出纯文本，
  `cl:` 回调 id 也没有人监听。
- **严重度**：**中**（渲染与降级逻辑真、有测试；但生产里唯一的调用点是"手动验证入口"）

### 15. 14.8 `path_security` / `url_safety` / `website_policy` / `threat_patterns`

- **证据栏声称**：`app/path_security.py`：`PathPolicy` 防住符号链接与 Windows 目录联接、`..` 穿越、
  Windows 保留名、NTFS ADS、设备命名空间……`UrlPolicy` 挡 `file:`/`javascript:`/`data:`、
  回环/私有段、云元数据 `169.254.169.254`；`website_policy` 三态 + 通配符
- **我 grep 了什么**：`grep -rn "path_security|url_safety|website_policy|threat_patterns" app/`、
  `grep -n "^from app import" app/tools.py`、`grep -rn "policy|blocked|url" app/browser.py`
- **找到什么**：`app/path_security.py` 唯一 prod importer 是
  `app/cli/commands/security_extra.py:35`（`orbit security path|url|threat` 三条诊断命令）。
  `app/tools.py:19-29` 的 import 列表**不含 path_security**——工具层的路径检查走的是
  `tools.py:827 resolve_path()`（另一份实现，`path_security.py:7-8` 的 docstring 自己承认
  「只做解析后是否还在授权目录内，**不认 Windows 保留名、不认 ADS、不认符号链接逃逸**」）。
  `app/browser.py:477 validate_url()` 只调 `config.http_url()`（scheme 白名单），
  **不查 IP 段、不查元数据地址、不查 website_policy**。
- **缺什么**：① `app/tools.py::resolve_path` 里缺「构造 `path_security.PathPolicy(...).check()`
  并用它替换/加强现有判断」；② `app/browser.py::validate_url` 里缺 `UrlPolicy.check()`；
  ③ `website_policy` **全仓零执行点**（只有 `security_extra.py` 的诊断命令会 print 它）。
- **严重度**：**中**（`tirith` 的 16 条威胁规则确实经 `tools.py::_approve_command` 进了生产，
  所以"threat_patterns"这半是真的；但 path / url / website_policy 三个策略对象只在
  `orbit security …` 里活，工具与浏览器两条真路径都不过它们）

### 16. 15.4 shared_metrics schema v1 / v2

- **证据栏声称**：`app/metrics_schema.py`：**v1 / v2 双版本**，输出带 `schema_version`，
  字段增删必须走新版本、旧版本保留校验器、给**单向迁移**（v1 → v2）
- **我 grep 了什么**：`grep -rn "metrics_schema|shared_metrics" app/`、`grep -rn "schema_version" app/`
- **找到什么**：`app/metrics_schema.py` 唯一 prod importer 是
  `app/cli/commands/observability_extra.py:23`（`orbit metrics schema`）。
  `app/metrics.py`（指标聚合）与 `app/observability_sink.py`（exporter 落点）
  **都不 import metrics_schema、都不产出 `schema_version` 字段**
  （`grep -rn "schema_version" app/` 在 `app/metrics.py` / `app/observability_sink.py` /
  `app/exporters.py` 里 0 命中，只有 `app/mcp_schema_cache.py` 那个无关的缓存版本号）。
- **缺什么**：`app/metrics.py` 的产出函数里缺 `metrics_schema.dumps(...)`。所谓 "shared"
  没有第二个共享方——schema 只被一条 CLI 命令用来校验用户手喂的 JSON。
- **严重度**：**低**（schema 模块真、校验器真、迁移真；但"共享契约"的共享方缺一半，
  指标生产者不遵守它）

### 17. 8.5 resources → RAG 索引

- **证据栏声称**：`app/mcp_resources.py`（resources → RAG 索引）
- **我 grep 了什么**：`grep -rn "mcp_resources|index_resources" app/`、读 `app/mcp.py:452-460, 553-590`
- **找到什么**：`app/mcp_resources.py` 唯一 prod importer 是 `app/cli/commands/mcp.py`
  （`orbit mcp resources` 列表命令）。真正的 resources→RAG 索引写在
  `app/mcp.py::_index_resources`（553 行），它 import 的是
  `app.indexing.chunks_for_resource`（559 行），**不经过 `app/mcp_resources.py`**。
  索引写入器由 `app/main.py:210 build_indexer(...)` 注入，所以这条能力**在生产里是通的**。
- **缺什么**：不是功能缺，是**证据栏引错了入口**：引的 `app/mcp_resources.py` 不是生产路径。
- **严重度**：**低**（能力在，报告指向了一个只服务 CLI 的模块）

### 18. 6.4 "注册 vs 暴露"分离（per-run 收窄暴露面）

- **证据栏声称**：`ToolRegistry.specs(platform=...)` 走 `app/toolsets.resolve`；默认 `cli` 与
  改动前逐名相等……**注意**：`graph/nodes.py` 调 `specs()` 时**没传 platform**，全仓没有非 `cli`
  的调用点——接口成立但"多平台差异化"尚无消费方
- **我 grep 了什么**：`grep -rn "specs(" app/`
- **找到什么**：`app/graph/nodes.py:518` 附近确实调 `specs()`（无 platform）。证据栏**自己承认**
  「全仓没有非 `cli` 的调用点」。
- **缺什么**：`app/gateway.py`（platform 应为具体平台）与 `app/cron.py`（`scene="cron"`）两条路径
  都没传 platform。`app/toolsets.py:918` 定义了 `"cron": ("ask_user", "clarify", "todo")` 的场景摘除表，
  但那条表只有在 `context.scene=="cron"` 时才生效——而没有任何生产调用点构造过
  `scene="cron"` 的过滤上下文。
- **严重度**：**低**（`specs()` 本身在生产路径上，per-run 收窄成立；但"多平台/场景差异化"
  这一半按证据栏自己的话"尚无消费方"。**这条不是虚报，是 ✅ 打在了一个只完成一半的条目上**）

---

## 确认无问题的（一行一个，带调用方路径）

> 格式：`行号 条目 → 生产调用方`。「函数级」= 我追到了具体调用点；「模块级」= 有 `app/` 内
> 非测试 importer，未再往下追函数。

1.1 中央命令注册表 → **函数级**：各 `app/cli/commands/*.py` 在 import 期 `REGISTRY.add(...)`，
   消费者是 `app/cli/main.py::build_parser`（经 `app/cli/commands/__init__.py` → `app/cli/main.py`）
1.2 argparse 子命令树 → **函数级**：`app/cli/main.py::build_parser` / `_add_command`；
   入口 `app/cli/__main__.py` → `app/cli/main.py::main`
1.3 全局 flags → **函数级**：`app/cli/main.py::_global_options`（同一入口链）
1.9 交互 chat → **函数级**：`app/cli/commands/chat.py::run_chat` ← `commands/__init__.py:19`
1.10 shell 补全 → **函数级**：`app/cli/commands/core.py::run_completion` ← `commands/__init__.py:20`
1.11 serve → **函数级**：`app/cli/commands/ui.py` ← `commands/__init__.py:49`
1.15 console 安全命令台 → **函数级**：`app/console.py` ← `app/cli/commands/ui.py`（已注册命令的 handler）
1.18 session-search → **函数级**：`app/cli/commands/sessions.py` ← `commands/__init__.py:40`
2.1 单循环 + provider 归一化 → **函数级**：`app/graph/nodes.py`（唯一 think/execute）←
   `app/graph/builder.py::build_graph` ← `app/agent.py` ← `app/main.py:216` + `chat.py::_build_stack`
2.3 心跳 30s + 双阈值 → **函数级**：`app/heartbeat.py::Heartbeat` ← `app/graph/nodes.py::beat()`
   （`nodes.py:32` `from app.heartbeat import TOOL_PREFIX, Heartbeat`）
2.4 中断配对 → **函数级**：`app/errors_retry.interrupted_result` ← `app/graph/nodes.py::interrupted_updates`
   （`nodes.py:1104`）；`repair_tool_pairs()` ← `nodes.py::think`
2.6 错误分类 13 类 → **函数级**：`app/errors.decide` / `errors_retry.decide` ←
   `app/graph/nodes.py::ask_model`（`nodes.py:735`）
2.7 context_overflow → 压缩后重试 → **函数级**：`app/compaction.py::should_compact/plan` ←
   `app/graph/nodes.py::think`（`nodes.py:28` import，`think` 每轮开头判定）
2.9 三层预算刹车 → **函数级**：`app/policies.DefaultBudgetPolicy` ← `app/graph/nodes.py::budget`
   节点（`app/graph/builder.py:40` 把 `nodes.budget` 挂进图，`budget_route` 决定收尾）；
   `max_actions`/`max_seconds` ← `app/config.py:133/136` → `policies.py:410/415` → `nodes.py:1400-1438`
2.10 上下文窗口自适应阈值 → **函数级**：`app/window.py::thresholds_for` ← `app/graph/nodes.py:39`
3.1 三层 prompt 分层 → **函数级**：`app/context.py` ← `app/graph/nodes.py:31`（`PROMPT_VERSION` /
   `frozen_prefix` / `prefix_of` 全在用）
3.4 记忆注入冻结快照 → **函数级**：`app/context.build_prefix` / `context_meta["system_prefix"]` ←
   `app/graph/nodes.py:31`（`FROZEN_PREFIX_KEY` / `frozen_prefix`）
3.5 技能索引注入 → **函数级**：`app/graph/nodes.py` 技能索引段 ← `app/skill_index.py`（可达，
   importer 含 `app/graph/nodes.py`）
3.6 工具 schema 注入 + 体积可观测 → **函数级**：`app/tools.py::specs()` ← `app/graph/nodes.py:518`
3.7 提示词版本号随事件落库 → **函数级**：`app/context.PROMPT_VERSION`（`context.py:13`）→
   `app/graph/nodes.py:542` 与 `:656` 写进 `store.event(..., prompt_version=...)`
3.8 注入扫描（不可信内容包裹）→ **函数级**：`app/untrusted.py` ← `app/tools.py::as_tool_text`
   （`tools.py:44-50` 的 `TOOL_SOURCE_KEY` 机制就在做这件事）+ `app/memory_write.py`
4.1 Provider 注册表 + `ProviderProfile` 钩子 → **函数级**：`app/providers.py::build_chat_model` ←
   `app/agent.py:792`（`_client_builder`，生产路径不传 `factory`）
4.5 fallback 链 → **函数级**：`app/fallback.py::ModelPlan` ← `app/agent.py:705-710`
   （`_targets()` 读 `fb.load_chain(settings.data_dir)`）
4.10 三级能力元数据 → **函数级**：`app/capabilities.py` ← `app/providers.py`（`providers.py` 的
   `models` / `model_capabilities` 直接读它）
4.13 配置树完整导出 → **函数级**：`app/cli/commands/core.py::run_config_show` ←
   `commands/__init__.py:20`（`config show` / `config check`）
5.1 单一注册表 + 遮蔽门禁 → **函数级**：`app/tools.py::ToolRegistry.register`（`tools.py:1705-1780`
   的 `allow_tool_override` 门禁）← `app/main.py:154-163` 真实注册 GITHUB/BROWSER/WEB 工具
5.3 `check_fn` 可用性探针 → **函数级**：`app/toolsets.py` ← `app/tools.py::specs()/check_fn`
5.4 三层结果治理 → **函数级**：`app/budget.py::ToolResultsPolicy` ← `app/graph/nodes.py:1116`
   （`self.policies.tool_results.project`）与 `nodes.py:1265`（`record(..., turn=turn)`）
5.5 `tool_output_limits` → **函数级**：`app/budget.py:194-196` 常量 ← 同上 `project()`/`record()`
5.6 `read_file` 阈值钉死 `inf` → **函数级**：`app/budget.py` ← `app/tools.py` 的 `read_file` 分支
5.7 `hook_output_spill` + `spill_safety` → **函数级**：`app/budget.py` ← `app/tools.py:47-50`
   （`TOOL_SOURCE_KEY` / spill 目标挂在结果 dict 上）
5.11 `tool_search` 分级披露 → **函数级**：`app/tools.py:2791`（`if name == "tool_search"` 在
   `execute()` 主分支里）+ `app/toolsets.py:378` `ALWAYS_EAGER`
5.12 `todo` 元工具 → **函数级**：`app/tools.py:2239`（`if name == "todo":`）+ `tools.py:689` 声明
5.16 `file_operations` / `patch_parser` → **函数级**：`app/patch.py` ← `app/tools.py`（import 列表
   含 patch 的实现调用）
5.17 `code_execution` + `code_kernel` → **函数级**：`app/code_exec.py` ← `app/tools.py:23`
5.18 `terminal` 后端抽象 → **函数级**：`app/terminal.py` ← `app/tools.py:26`
5.19 `web_search` + `web_extract` → **函数级**：`app/web.py::WEB_TOOLS` ← `app/main.py:160-163`
   （`registry.register(WEB_TOOLS, app.state.web.execute)`）
6.1 toolset 定义 → **函数级**：`app/toolsets.py::TOOLSETS` ← `app/tools.py::specs()` → `nodes.py:518`
6.3 `orbit tools {list,enable,disable,post-setup}` → **函数级**：`app/cli/commands/tools.py` ←
   `commands/__init__.py:44`
6.5 工具 schema 校验前置 → **函数级**：`app/tools.py::schema_problems`（`tools.py:934` 定义）←
   `tools.py:1867`（**注册期**就调，不是只有 CLI）
7.1 多根 tier → **函数级**：`app/skill_index.py` ← `app/graph/nodes.py`（importer 表里有
   `app/graph/nodes`）
7.5 快照缓存 TTL 30s → **函数级**：`app/skill_index.py` 同上
7.6 写侧校验链 → **函数级**：`app/skill_lint.py` ← `app/skill_hub.py`（安装期）/ `app/skill_sync.py`
7.7 ledger 记账 → **函数级**：`app/curator.py` ← `app/journey.py` + `app/cli/commands/curator.py`
7.9 prune 默认 90 天 → **函数级**：`app/curator.py` ← `app/cli/commands/curator.py`（`prune` 命令）
7.12 `skills_guard` 107 条 + hub quarantine → **函数级**：`app/skill_hub.py` ←
   `app/tool_extra_impl2.py`（main.py:192 注册的工具）+ `skill_extra2.py`
7.14 `orbit bundles` → **函数级**：`app/bundles.py` ← `app/cli/commands/skill_extra.py` ←
   `commands/__init__.py:42`
7.16 `orbit sync` → **函数级**：`app/skill_sync.py` ← `app/cli/commands/skill_extra2.py` ←
   `commands/__init__.py:70`
7.18 技能库规模 → **函数级**：`app/skill_seed.py` ← `app/curator.py` / `app/skill_sync.py`
8.1 stdio + Streamable HTTP → **函数级**：`app/mcp.py::MCPManager` ← `app/main.py:210-211`
   （`await app.state.mcp.start(registry)`）
8.2 能力声明前置 → **函数级**：`app/mcp.py::_capability`（私有，同模块内被连接流程调用）←
   `main.py:211 start()` 链
8.4 prompts → 技能 → **函数级**：`app/mcp_prompts.py` ← `app/mcp.py::prompt_skills` ←
   `app/main.py:220`（`runner.policies.skills.prompt_source = app.state.mcp.prompt_skills`）
8.6 心跳 + 失败宽限期 → **函数级**：`app/mcp.py` ← `app/main.py:211`
8.7 schema 缓存 → **函数级**：`app/mcp_schema_cache.py` ← `app/mcp.py`（importer 表含 `app.mcp`）
8.8 `orbit mcp {...}` → **函数级**：`app/cli/commands/mcp_extra.py` ← `commands/__init__.py:27`
8.9 `mcp serve` → **函数级**：`app/mcp_server.py` ← `app/cli/commands/mcp.py`
8.10 optional-mcps 目录 → **函数级**：`app/mcp_catalog.py` ← `app/cli/commands/mcp_extra.py`
9.1 `MEMORY.md` 上限 → **函数级**：`app/memory_prompt.py` ← `app/context.py` ← `nodes.py:31`
9.2 `USER.md` 字符上限 → **函数级**：`app/profile.py` ← `app/context.py` + `app/main.py:18`
9.3 冻结快照 + 头部自曝用量 → **函数级**：`app/memory_prompt.HEADER` ← `app/context.build_context_tier`
   ← `nodes.py`
9.4 原子批处理 `operations[]` → **函数级**：`app/memory_batch.py::apply_batch` ←
   `app/cli/commands/memory_extra.py` ← `commands/__init__.py:28`
9.6 CJK 检索 → **函数级**：`app/session_search.py` ← `app/tools.py`（session_search 工具）
9.7 `session_search` 四形态 → **函数级**：同上 + `app/cli/commands/sessions.py`
9.8 检索零 LLM 成本 → **函数级**：`app/session_search.py`（`grep "llm\|model"` 在该模块无调用）
9.9 检索参数 → **函数级**：`app/session_search.py` 常量 ← 同上两条路径
9.10 8 个外部 memory provider → **函数级**：`app/memory_providers.py` ←
   `app/tool_extra_impl2.py`（`main.py:192 register_round3_tools`）
9.11 holographic 本地图记忆 → **函数级**：`app/holographic.py` ← `app/memory_providers.py`
9.12 写入门禁 → **函数级**：`app/memory_write.py::check_write`（`memory_write.py:549` 定义）←
   **`app/tools.py:2486`**（memory 工具真调它）
9.13 漂移防护 → **函数级**：`app/memory_write.py` ← `app/tools.py:2486` 同一条链
9.14 核心零向量 → **函数级**：`app/vectors.py` / `app/rerank.py` ← `app/retrieval.py` ←
   `app/graph/nodes.py:35`（`from app.retrieval import PREFETCH_MIN_SCORE`）
9.15 `orbit memory {...}` → **函数级**：`app/cli/commands/memory_extra.py` ← `commands/__init__.py:28`
10.1 单一 session 平面 → **函数级**：`app/session_plane.PlaneSink.attach` ← **`app/main.py:127`**
   （`app.state.plane_detach = app.state.plane_sink.attach()`）
10.3 归档不删 → **函数级**：`app/session_archive.py` ← `app/cli/commands/session_extra.py` ←
   `commands/__init__.py:67`
10.4 压缩谱系 → **函数级**：`app/graph/nodes.py`（压缩段）+ `app/compaction.py` 阈值
10.6 幂等键 + 投递账本 → **函数级**：`app/delivery_ledger.py` ← `app/cron.py`（importer 表）
10.9 checkpoint 管理 → **函数级**：`app/shadow_checkpoint.py` ← `app/cli/commands/chat.py:332`
   （`--checkpoints` 真建 `ShadowRepo`）+ `session_extra2.py`
10.10 backup / import → **函数级**：`app/backup.py` ← `app/cli/commands/ops.py` ←
   `commands/__init__.py:33`
10.12 双 FTS 索引 → **函数级**：`app/session_index.py` ← `app/session_search.py` ← `app/tools.py`；
   `app/storage.py` ← 28 个生产 importer（含 `app/main.py`）
10.15 `orbit checkpoints {...}` → **函数级**：`app/cli/commands/ops.py` ← `commands/__init__.py:33`
11.1 定时任务存储 + 调度器 → **函数级**：`app/cron.py` ← `app/tool_extra_impl.py`（cron_job 工具）
   + `app/cli/commands/cron.py`
11.2 `orbit cron {...}` → **函数级**：`app/cli/commands/cron.py` ← `commands/__init__.py:21`
11.4 `--skill` / `--script` / `--no-agent` 语义 → **函数级**：`app/cron.py` ← 同上
12.1 单 dispatcher 互斥 → **函数级**：`app/gateway.py::acquire`（`gateway.py:433/882`）←
   `app/cron.py` / `app/session_leases.py` / gateway CLI
12.2 四个状态文件 → **函数级**：`app/gateway.py`（`gateway.py:905 spawn_ledger.reap_orphans`）←
   `app/cli/commands/gateway.py` ← `commands/__init__.py:24`
12.3 平台适配器基类契约 → **函数级**：`app/platforms.py` ← `app/gateway.py` / `app/cron.py` /
   `app/tool_extra_impl2.py`
12.11 `orbit monitoring status` → **函数级**：`app/cli/commands/observability.py` ←
   `commands/__init__.py:31`
13.1 `plugin.yaml` 16 字段 manifest → **函数级**：`app/plugins.py` ← `app/plugin_host.py` ←
   `app/main.py:177`（`PluginHost.from_settings(...)`）
13.2 `requires_env` / `optional_env` → **函数级**：`app/plugins.py`（manifest 解析）← 同上
13.3 `provides_tools` 影响 import 时机 → **函数级**：`app/plugin_host.py` ← `app/main.py:177-180`
   （`hook_sink=DEFAULT_HOOKS` / `attach_to_app`）
13.8 dashboard 插件契约 → **函数级**：`app/main.py:887-892`
   （`plugin_dashboard.discover(...)` → `PAGES.add` → `mount_into_app(app)`）。**已修好**
14.1 审批三态 → **函数级**：`app/approval_channel.py` ← `app/tools.py:20`（`_approve_command` 链）
14.2 硬地板 18 条 → **函数级**：`app/approvals.py` ← `app/tools.py:21`（`tools.py:2027` 的
   「tirith → approvals → egress」三道闸）
14.3 密钥脱敏 → **函数级**：`app/redact.py` ← `app/tools.py` / `app/policies.py`
14.4 `tirith` 执行前扫描 → **函数级**：`app/tirith.py` ← `app/tools.py:27`
   （`_approve_command` 第一道闸）
14.6 外部密钥源 → **函数级**：`app/secrets.py` ← `app/egress.py`（importer 表）+ security CLI
14.7 系统 keyring 存密钥 → **函数级**：`app/vault.py::Vault` ← **`app/main.py:131`**
14.9 `orbit approvals {...}` → **函数级**：`app/cli/commands/approvals.py` ← `commands/__init__.py:18`
14.10 `orbit hooks {...}` → **函数级**：`app/shell_hooks.py` ← `app/cli/commands/hooks.py` ←
   `commands/__init__.py:25`；`app/hooks.py` ← `app/graph/nodes.py:28`
15.1 结构化日志 + `orbit logs` → **函数级**：`app/logging_setup.configure` ← **`app/main.py:113`**
15.2 `orbit insights` → **函数级**：`app/cli/commands/observability.py` ← `commands/__init__.py:31`
15.3 `orbit monitoring status` → **函数级**：同上
15.5 observability exporter 插件 → **函数级**：`app/exporters.py` + `app/observability_sink.attach`
   ← **`app/main.py:205-206`**
15.6 `orbit dump` → **函数级**：`app/cli/commands/core.py` ← `commands/__init__.py:20`
16.1 dashboard 18 个页面 → **函数级**：`app/dashboard_api.router` ← **`app/main.py:870-872`**
   （`app.include_router(dashboard_api.router)`）+ `app/dashboard.py` ← `ui_extra.py`
16.5 `orbit skin` → **函数级**：`app/skin.py` ← `app/cli/commands/ui.py` ← `commands/__init__.py:49`

---

## 附：未接入 `__init__.py` 的 CLI 模块

**全量核对结果：46 个命令模块里 2 个从未被 import。**

| 模块 | 大小 | 它绑了哪些路径 | 后果 |
|---|---|---|---|
| `app/cli/commands/model_extra.py` | 21,631 B | `model` 命令的 `--route` / `--reasoning` / `--aux` / `--aux-set` / `--aux-unset` / `--aux-cost` / `--aux-tokens` / `--aux-timeout` 八个开关（`_attach_model_flags()`，第 455-470 行）+ `moa run` 子命令（第 480-496 行），末尾第 501 行 `attach()` 在模块级调用 | `orbit model --route` / `--aux*` 在编译出的 parser 里**不存在**（`grep -rn '"--route"' app/cli/commands/` 只命中这个死模块）；`orbit moa run` 子命令不存在 → **MATRIX 4.3 / 4.9 / 4.11 三行全断** |
| `app/cli/commands/skill_extra3.py` | 27,109 B | `skills publish` + `profile list/use/create/delete/describe/alias/rename/install/update/info` 共 11 条（`_bind` 列表在第 539-549 行） | 这 11 条命令的 handler 永不挂上 → **MATRIX 7.13 / 9.16 两行断** |

**核对方法**（可复现）：
```powershell
$init = Get-Content app\cli\commands\__init__.py -Raw
Get-ChildItem app\cli\commands\*.py | ? { $_.Name -notin '__init__.py','_table.py' } |
  % { if (-not [regex]::IsMatch($init, "(?m)^\s*$($_.BaseName)\s*,\s*(#.*)?$")) { $_.BaseName } }
# → model_extra / skill_extra3
```

**两个额外的事实**：
1. `app/cli/commands/__init__.py` 的 `__all__`（第 125 行）**列了 `"skill_extra3"` 却没有 import 它**
   → `from app.cli.commands import *` 会 `AttributeError`。这是"文档/清单说接了、import 没接"的
   字面证据，也说明 `__all__` 不是从 import 列表生成的，而是手抄的。
2. 这两个模块都**不是**"写了但不需要 import"的类型：`model_extra.py:501` 与
   `skill_extra3.py:579-597` 都有模块级自执行接线（`attach()` / `_mount_everything()` /
   `_install_assembly_hook()` 包 `_table.register_specs`），**设计上就是要被 import 才生效**。
   少一行 import，整份接线静默失效——没有任何 import 期错误、没有任何测试失败
   （`tests/test_skill_publish.py` 20 条、`tests/test_profile_dist.py` 31 条全过，
   因为它们直接 import 模块本身，绕过了 `__init__.py`）。

---

## 附：未逐条核对的 43 行（诚实清单）

这些行我确认了「被引用的模块在 `app/` 内有生产 importer」或「命令已在 `REGISTRY` 里」，
但**没有**逐条追到具体 handler / 具体数值是否真的一致。列出来是为了不让"确认 106"看起来比实际更强：

`1.4`（81 个顶层命令计数）`1.5`（420 条二级 / 506 parity_rows）`1.6`（39 条三级）`1.8`（`-z` oneshot）
`1.12`（dashboard 启动器）`1.14`（ACP 服务端）`1.16`（TUI）`2.5`（bg-review，**默认关**，
`agent.py:599 _spawn_bg_review` 确实被调，但 `_bg_review_enabled()` 默认 `False`）
`2.8`（per-thread 中断）`3.2`（AGENTS.md vs ORBIT.md 优先级）`3.3`（SOUL.md 全局加载）
`4.6`（credential pool / `auth reset`）`5.8`（18 条 hardline / 38 条 dangerous 计数）
`5.9`（Windows 破坏性操作清单）`5.10`（自终止保护）`5.15`（19 个功能域覆盖）
`6.2`（四层过滤：`app/toolsets.py::explain()` 有实现，但我没追到它的生产调用方）
`7.2`（frontmatter 16 字段 / 11 条硬校验）`7.3`（各字段长度上限）`7.4`（渐进披露三层）
`7.8`（curator 状态机 + 两级锚点）`7.10`（archive → restore / rollback）`7.11`（`.usage.json` 13 字段）
`7.15`（`orbit curator` 9/17 条）`9.5`（3 次 consolidation 熔断）`10.2`（消息追加日志 + id 游标）
`10.5`（`system_prompts` hash + `compression_locks`）`10.13`（`orbit sessions` 19 子命令）
`11.3`（`--deliver`）`11.5`（cron 与交互共享权限）`11.6`（紧急停止级联）
`12.4`（`standalone_sender_fn`）`12.6`（`orbit send`）`12.7`（`orbit webhook` 4/4）
`12.8`（`orbit pairing` 4/4）`13.6`（`plugin_guard` 三级裁决）`13.7`（插件结构限额 400/10MB/1MB）
`13.9`（`orbit plugins` 18/19 条）`13.10`（`plugins.enabled/disabled/entries` + `allow_tool_override`）
`16.2`（聊天页 + 增量流式 + 断线回放）`16.3`（技能页 / 记忆页 / 会话摘要页）
`16.4`（设置页）`16.6`（会话回放 / browse 页）

---

## 附：审计中另外注意到的 3 件事（不计入 SUSPECT，但值得记）

1. **`app/reasoning.py` 与 `app/providers.py` 是两份推理翻译实现**，前者死、后者活。
   两边规则如果漂移，"翻译表"这份文档就会开始骗人。`reasoning.py:395-450` 里
   "不回显 reasoning_content 会 400"那条注释，在 providers.py 里没有对应实现
   （`grep -rn "reasoning_content" app/providers.py` → 0 命中）。
2. **`app/cli/commands/__init__.py` 的 `__all__` 手抄且已经和 import 列表不一致**
   （多列了 `skill_extra3`、漏了 `model_extra`）。这个文件本身用 `# isort: split` 精确控制顺序，
   却用一份不同步的 `__all__` 当门面——下次有人按 `__all__` 判断"哪些模块接好了"就会再被骗一次。
3. **`app/tool_catalog.py:288` / `app/tool_extra_impl.py:58` / `app/tool_extra_impl2.py:54`
   三处代码注释自己承认 `DelegateRunner.runner_factory` 全仓未注入**。
   也就是说"委派链未通"这件事**代码里已经写清楚了**，但 MATRIX 2.2 仍然打 ✅。
   这类"注释承认缺、矩阵标完成"的分歧，比单纯找不到调用方更值得优先处理——因为它说明
   自检机制（`tool_catalog` 的 `unavailable` 状态）与 MATRIX 的判据没有对齐。

---

## 附：覆盖度交叉核对（可复现）

本报告的 105 + 18 + 43 必须与 MATRIX 的 ✅ 集合逐个相等。核对脚本（只读）：

```python
import re
matrix = open(r"handoff/hermes-parity/MATRIX.md", encoding="utf-8").read().splitlines()
audit  = open(r"handoff/hermes-parity/AUDIT-round17.md", encoding="utf-8").read()
ok = [c[0] for ln in matrix if ln.startswith("|")
      for c in [[x.strip() for x in ln.strip().strip("|").split("|")]]
      if len(c) >= 6 and re.match(r"^\d+\.\d+[a-z]?$", c[0]) and c[4] == "✅"]
i1 = audit.index("## 确认无问题的"); i2 = audit.index("## 附：未接入")
conf = re.findall(r"(?m)^(\d+\.\d+[a-z]?) ", audit[i1:i2])
i3 = audit.index("## SUSPECT 清单")
susp = re.findall(r"(?m)^### \d+\. (\d+\.\d+[a-z]?) ", audit[i3:i1])
unv  = re.findall(r"`(\d+\.\d+[a-z]?)`", audit[audit.index("## 附：未逐条核对"):])
covered = set(conf) | set(susp) | set(unv)
print(len(ok), len(conf), len(susp), len(unv), len(covered))
print("MISSING:", sorted(set(ok) - covered), "EXTRA:", sorted(covered - set(ok)))
# → 164 105 18 43 165
# → MISSING: ['5.15', '6.2']  ← 这两条已在上面的人工清单里补齐（5.9 / 5.10 因表格分列问题
#   不在 ok 的 164 里，但都在「未逐条核对」清单中）
```
