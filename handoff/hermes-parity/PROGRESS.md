# 对标进度报告

**更新时间**：2026-10-28 11:20（本机实测时钟，只读取证）
**本轮轮次**：**N = 7**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑会改状态或真发请求的命令（`config edit` / `doctor --fix` / `--live` / `cron run` / `gateway start` / `backup restore` 全部未运行）。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `b5457c9`，**工作树干净** ✅）

---

## 总体完成度

### **97 / 181 项 = 53.6%**

> ⚠️ **与表头的 54.1% 差 1 条，原因是计数工具的口径 bug，不是你在虚报。详见风险 R1。**

| 项 | 说明 |
|---|---|
| 权威口径 | **读表格的「状态」列**（第 5 个 `\|` 段），不是整行扫符号 |
| 完成 | **✅ 97 条** |
| 部分 | 🟡 **45 条** |
| 未开始 | ⬜ **39 条** |
| 合计 | 181（编号唯一 181/181） |
| 表头 / `matrix_stats.py` 口径 | ✅98 / 🟡44 / ⬜39 = **54.1%** ← **多了 1 条**，见 R1 |

**差异的唯一来源**：`MATRIX 10.1` 的状态列是 **🟡**，但它的证据栏里含我上轮建议的措辞「→ **变 ✅ 的路径**」。`scripts/matrix_stats.py::count()` 用的是 `if DONE in line`（**整行包含即算完成**），于是把这条 🟡 算成了 ✅。

**修正后：97 / 45 / 39 = 53.6%**；本轮真实新增 ✅ = **14 条**（表头显示 15 条）。

### 硬口径：**全部对上**

| 你的口径 | 我的实测 | 判定 |
|---|---|---|
| `REGISTRY` 80 顶层 / 345 二级 / 39 三级 = 464 | **80 / 345 / 39 = 464** | ✅ 一致 |
| done 219 / partial 18 / todo 227 | **219 / 18 / 227** | ✅ 一致 |
| pytest 1 failed, 1760 passed, 1 skipped | **1 failed / 1760 passed / 1 skipped（153.74s）** | ✅ 一致（你 145.08s） |
| ruff `All checks passed!` | **All checks passed!**（exit 0） | ✅ 一致 |
| mypy 103 source files | **103 source files** | ✅ 一致 |
| 工作树干净 | `git status --porcelain` 空 | ✅ 一致 |
| 唯一失败 = 既有 `test_connectors::test_browser_append_and_reload` | 正是它 | ✅ 一致 |
| MATRIX ✅98 / 🟡44 / ⬜39（54.1%） | **97 / 45 / 39（53.6%）** | ⚠️ **差 1 条（R1）** |

---

## 分层完成度

（状态列口径）

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | 10 / 18 | 55.6% | 进行中（🟡4） |
| L2 Agent 核心循环 | 1 / 10 | 10.0% | **最弱层**（🟡3，⬜6） |
| **L3 上下文装配** | **8 / 8** | **100%** | **🎉 首个整层对齐** |
| L4 模型适配 | 6 / 13 | 46.2% | 本轮 +5（🟡3，⬜4） |
| L5 工具层 | 11 / 19 | 57.9% | 本轮 +3（🟡4，⬜4） |
| L6 工具集 / 注册 | 3 / 5 | 60.0% | 进行中（🟡2） |
| L7 Skill / curator | 9 / 18 | 50.0% | 进行中（🟡5，⬜4） |
| L8 MCP | 6 / 10 | 60.0% | 进行中（🟡2，⬜2） |
| L9 记忆层 | 8 / 16 | 50.0% | 本轮 +3（🟡3，⬜5） |
| L10 持久化 / 会话 | 5 / 15 | 33.3% | 🟡5、⬜5 |
| L11 cron | 3 / 6 | 50.0% | 进行中（🟡2） |
| L12 Gateway / 多平台 | 7 / 11 | 63.6% | 进行中（🟡3） |
| L13 插件 | 5 / 10 | 50.0% | 进行中（🟡4） |
| L14 安全 / 审批 | 6 / 10 | 60.0% | 🟡4 |
| L15 可观测性 | 4 / 6 | 66.7% | ⬜2 |
| L16 Web UI / 前端 | 5 / 6 | 83.3% | 本轮 +1（16.1） |
| **合计** | **97 / 181** | **53.6%** | — |

**L3 上下文装配 8/8 = 100%** —— 这是项目开跑以来**第一个整层对齐的层**，值得单独记一笔。

---

## 本轮抽查证据

**抽样率**：本轮新增 14 条 ✅ 中**复核 12 条（86%）**；你点名的 4 条 🟡 **全部复核**；`errors.py` 处置 **独立复核**。

### A. 你点名的 7 条，逐条判定 —— **7/7 全部撑得住**

**① 4.5 / 4.6「已接线」→ ✅✅ 真接线，且测试确实走 `get_model` + 假 factory**

`app/agent.py` 里裸名 `FallbackChain` 命中 1（在 docstring），`CredentialPool` 命中 1；但**真正的接线走 `fb.` 前缀**（`from app import fallback as fb`）：
```
agent.py:476   def get_model(...)                       ← 返回 RoutedModel，不是裸客户端
agent.py:503       route = _route_of(candidate, len(plan.attempts) - 1)
agent.py:504       self.last_route = route
agent.py:512       return RoutedModel(plan, client, route, on_switch=self._on_route_switch)
agent.py:514   def _on_route_switch(self, route):      ← 切换时写 last_route + log.warning
agent.py:519   def _targets(self) -> list[fb.FallbackTarget]:   ← 主模型 + fallback_chain
agent.py:526       primary = fb.FallbackTarget(...)
agent.py:552       pool = fb.CredentialPool(fb.KeyringStore(f"orbit:{...}"))
agent.py:166       raise fb.FallbackExhausted(self._plan.attempts) from exc
```
`Route`（`agent.py:28`）字段与你说的完全一致：`provider / model / base_url / key_label / attempt / reason`，`to_dict()` 额外给 `switched = attempt > 0` ✓

**测试确实调 `get_model` 走假 factory**（不是只测 `FallbackChain`）：
```python
# tests/test_fallback_wiring.py
@pytest.fixture
def runner(settings):   # 真 AgentRunner（注释：get_model 是它的方法，不能绕开它测）
def test_401_switches_key_within_same_provider(settings, runner):
    use_pool(runner, pool_with("sk-key-A", "sk-key-B"))
    recorder = Recorder(lambda candidate, n: FakeStatusError(401,...) if n == 1 else FakeClient(...))
    model = runner.get_model(factory=recorder)
    assert recorder.providers == ["openai", "openai"]      # 401 不换 provider
    assert recorder.key_labels == ["pool#0", "pool#1"]     # 换同一家的下一把 key
    assert model.route.key_label == "pool#1"
    assert model.route.attempt == 1
```
14 条用例覆盖：401/429 换 key、400/503 换 provider、未知错误**不切换**、全链失败报整条链、切换有日志、invoke 期失败切换、`bind_tools` 跨切换存活、注入模型原样返回、`route` 对调用方可见 ✓✓
**上轮我点名的「库层有、运行时没接」已彻底解决。**

**② 9.4 原子批处理「全有或全无」→ ✅✅ 补偿回滚真被验证**

```python
# app/memory_batch.py
:269  async def apply_batch(...)     """原子批处理：按最终态校验，全有或全无。"""
:279      （故意在第 N 次写失败，验证补偿真的把状态退回批处理前）
:300      log.warning("批处理写入失败，开始补偿：%s", exc)
:302      await engine.restore(before, created)
:139  async def restore(self, before, created):   """补偿写：改过的 key 写回旧值，新建的 key 删掉。…补偿必须**幂等**"""
```
**你要的那个「先真写一半再抛」的后端在**：`test_apply_batch_rolls_back_when_write_fails`(:184) 用 `_FlakyBackend(store)`，断言链：
```python
assert result.ok is False
assert result.wrote is True          # 真的写进去了（写了一半）
assert result.rolled_back is True    # 然后补偿了
assert "已回滚" in result.error
assert await _entries(store) == {"k1": "v1"}   # 旧值回来了、新建的 k9 没留下
```
**这是真补偿验证，不是"函数被调用过"。** ✓✓

**③ 9.5 熔断 → ✅✅ 确实在数调用次数**

```python
async def test_guarded_consolidation_stops_calling_when_tripped(tmp_path):
    """熔断期间"不再尝试"必须是**不调用**，所以数调用次数。"""
    ...
    for _ in range(3): ...        assert calls["n"] == 3
    # 第 4、5 次：熔断已打开，worker 一次都不该被调用
    for _ in range(2): ...        assert calls["n"] == 3      ← 仍是 3
    await consolidate_guarded(..., turn="t2", ...)            assert calls["n"] == 4   ← 下一轮恢复
```
**正是你要的「3 次失败后第 4、5 次仍为 3」** ✓；常量 `_MAX_CONSOLIDATION_FAILURES_PER_TURN = 3`（`memory_batch.py:58`）。

**④ 5.12 `todo` 落盘 + 重启 → ✅✅ 用例在**

`test_meta_tools.py:76 async def test_todo_state_survives_a_restart(env)` ✓ —— 就是"重启后还在"。
另有 `test_todo_add_list_and_reads_back_the_full_list`(:61)（回读）、上限 50 条(:110)、单条 200 字符(:123)。常量 `TODO_MAX_ITEMS = 50` / `TODO_MAX_CHARS = 200` / `TODO_STATUSES = (pending, in_progress, done)`（`tools.py:305-308`）✓

**⑤ 5.16 apply_patch 原子性 → ✅✅ 用例正是那个形状**

`test_patch.py:189 def test_context_mismatch_writes_nothing_at_all(tmp_path)` ✓ —— 「第 2 个 hunk 不匹配 → 第 1 个 hunk 也没写」。
配套还有 `test_mismatch_names_the_hunk_and_the_line`(:210)、`test_apply_is_idempotent_on_a_second_run`(:233)、`test_path_escape_is_rejected`(:247)、`test_the_same_file_twice_in_one_patch_is_rejected`(:286)，共 22 条。

**⑥ 3.8 注入扫描只包高风险来源 → ✅✅ `memory_search` 确实不被包**

```python
# app/tools.py（as_tool_text）
:1851  from app import redact, untrusted
:1859  if untrusted.is_high_risk(source):
:1860      findings = untrusted.scan(text)
:1871      return untrusted.wrap(source, text)

# app/untrusted.py
:50  HIGH_RISK_TOOLS = frozenset({"run_command", "local_read_document", "web_search", "web_extract"})
:53  HIGH_RISK_PREFIXES = ("browser_",)
:48  # 反过来，``memory_search`` / ``skill_load`` / ``history_*`` **不在**名单里：它们读的…
```
用例：`test_high_risk_sources_are_wrapped`(:117) + `test_low_risk_sources_are_not_wrapped`(:127) + `test_redaction_still_happens_inside_the_wrapper`(:147) ✓✓

**⑦ 16.1 dashboard 18 页 → ✅✅ 测试现场读 `main.py` 抽路由，不是硬编码**

实跑 `dashboard.report()`：
```
total: 18 | covered: 4 | partial: 11 | missing: 3 | unbacked: {}
```
**与 MATRIX 的 4 / 11 / 3 完全一致，且 `unbacked` 为空** ✓
测试确实是现场读文件：
```python
test_dashboard.py:45  MAIN_PY = Path(__file__).resolve().parents[1] / "app" / "main.py"
test_dashboard.py:6-7 「…`app/main.py` 一改，测试还绿着，面板就开始撒谎。所以这里凡是涉及路由的断言，
                       都现场 read_text 读 app/main.py 再比对。」
:121  test_main_py_route_extraction_actually_works   ← 断言 len(routes) >= 25，防正则写坏
:146  test_no_page_claims_a_route_that_main_py_does_not_have  ← report()["unbacked"] 必须空
```
**这是本轮质量最高的一条证据设计**：连"正则本身写坏了"都有自检。

### B. 你刻意标的 4 条 🟡 —— **四条都标得准**

| 条目 | 实测 | 判定 |
|---|---|---|
| **4.9** 辅助槽位 | `aux_slots.SLOTS = ('summary','title','compression','classifier')` = **4** 个（Hermes 18 个） | ✅ 🟡 正确 |
| **5.13** clarify 的 gateway 按钮 | `grep send_buttons` 于 `tools.py` / `graph/nodes.py` / `gateway.py` = **0 命中** → clarify 确实没走按钮渲染，只渲染文本 | ✅ 🟡 正确 |
| **9.10** 外部 provider | `PROVIDER_NAMES` = **8**（local/file/jsonl + mem0/zep/letta/chroma/pgvector）；真实现 3 个；其余 `available()` **恒 False**（`:99,103,231,234,349`）；读写抛 `ProviderUnavailable` | ✅ 🟡 正确（**但见 R3 的命名问题**） |
| **16.1** 3 页无数据源 | `missing: 3` 实测 = 3，`partial: 11` | ✅ 🟡 正确 |

### C. `errors.py` 撤回的处置 —— **我判断：撤回是对的**

先说复核结果（你交代的三点全部属实）：

| 你的说法 | 实测 |
|---|---|
| 只保留分类层 | `errors.py`：`ADVICE` 13 类 / `RETRYABLE` 4 类 / `is_context_overflow()` 存在 ✓ |
| 21 条测试 | `pytest tests/test_errors.py --collect-only` = **21 tests** ✓ |
| 未接线 | `grep errors` 于 `app/graph/nodes.py` = **0**，`app/agent.py` = **0** ✓ |
| 撤回后 test_graph 恢复绿 | `pytest tests/test_graph.py -q` = **24 passed in 10.73s** ✓ |

**为什么"撤回"比"留着红树等对方"更对（4 条理由）**：

1. **绿树是多方共享的公共资产。** 这是并行多 agent 施工；一棵红树会让**所有人**都失去"这次失败是不是我弄的"这个判据——你这轮就正好撞上了这个困境（分不清是自己接线还是别人重写 `agent.py`）。留着红树等于把不确定性扩散给每一个并行的 agent。
2. **归因必须先隔离成单变量。** 正确顺序是「等 `agent.py` 稳定 → 单独接线 → 跑 test_graph」。撤回把接线变成一个**可重放的干净实验**；留着红树则永远无法归因。
3. **MATRIX 的标注方式正是诚实优先要的做法。** 2.6/2.7 标 🟡 并写明「**未接线**：`Nodes.ask_model` 仍对所有异常重试 `MODEL_ATTEMPTS` 次——那一轮 `app/agent.py` 正被另一个子agent 重写，同时改会互相覆盖」。**把不确定性写进文档而不是藏起来**，这比"接上了但不知道为什么红"好一个数量级。
4. **沉没成本没浪费。** 分类层 + 21 条测试留在库里，接线只需把 `ask_model` 的 except 分支改成 `errors.classify(exc)` → 只有 `RETRYABLE` 才重试。成本已经付了，只差一行调用。

**唯一建议补充**：在 2.6/2.7 的证据栏加一句**接线点**（`Nodes.ask_model` 的 except 分支 → `errors.classify` → 仅 `RETRYABLE` 重试），否则下一个人接手时要重新推导一遍。

**反过来说，"留着红树"只在两个条件同时成立时才更优**：能即时协调 + 红树存活以分钟计。本轮明显不成立。

### D. 其余新增 ✅ 抽查（3 条）

| 条目 | 证据 | 判定 |
|---|---|---|
| **4.3** per-model 路由 | `app/model_router.py::route()` 实跑：`gpt-5.4`→openai ✓、`deepseek-chat`→deepseek ✓、`totally-unknown-model`→`supported=False` + 「不在任何 provider 的模型表里」**不猜** ✓；`test_model_router.py` 28 条 | ✅ |
| **4.4** 推理翻译表 | `reasoning.py`：`Translator` 类 + 逐家请求构造 `_deepseek_request` / `_zai_request` / `_kimi_request` + `_ANTHROPIC_BUDGET` + `NotSupported` | ✅ |
| **4.11** MoA | `moa.py`：`Proposer` / `MoAPlan` / `Proposal` / `MoAResult` / `per_slot_cap` / `build_aggregator_prompt`（N proposer + 1 aggregator） | ✅ |
| **5.3** check_fn 探针 | `toolsets.py:313 PROBE_TTL_SECONDS = 30.0`、`:316 PROBE_GRACE_SECONDS = 60.0`、`_PROBE_CACHE`、三步逻辑（新鲜即返回 / 失败在宽限内判抖动 / 超宽限才摘） | ✅ |

> **未复核的 2 条**：9.15（`orbit memory`）、14.6 / 14.10 其中之一。如实标注。

---

## 与目标标志的差距

### Top 10 未完成项

| # | 未完成项 | 现状 | 量级 |
|---|---|---|---|
| 1 | **L2 核心循环（6 ⬜ + 3 🟡）** | 唯一"几乎没动"的层：13 类错误分类**未接线**、`context_overflow` 压缩重试**未接线**、心跳判卡死、中断配对、后台学习回路 | 9 条待补 |
| 2 | **L10 持久化（5 ⬜ + 5 🟡）** | 归档不删、`system_prompts` 内容寻址、存活租约、双 FTS、`hosted_room_*` 事件溯源；🟡 里 10.1 写侧未统一、10.4 压缩阈值未接 | 10 条待补 |
| 3 | **L7 Skill 剩余（4 ⬜ + 5 🟡）** | hub 分发、bundles、sync 三方合并、`skills_guard` 107 条威胁模式、curator 剩 8 子命令 | 9 条待补 |
| 4 | **L1 入口（4 ⬜ + 4 🟡）** | `serve` / `dashboard` / `desktop` / `gui` / `acp` / TUI / 全局 23 flag | 8 条待补 |
| 5 | **L9 记忆剩余（5 ⬜ + 3 🟡）** | USER.md 用量自曝头、8 个外部 provider（`available()` 恒 False）、holographic、写入门禁、漂移防护、CJK FTS | 8 条待补 |
| 6 | **L4 模型剩余（4 ⬜ + 3 🟡）** | `proxy` / `egress` / 三级能力元数据 / TTS-STT；🟡 里 4.1 只 16/48、4.2 只 3 transport、4.9 只 4/18 槽位 | 7 条待补 |
| 7 | **L5 工具剩余（4 ⬜ + 4 🟡）** | 29 工具 vs 94；缺 code_execution、媒体生成、视觉、TTS、kanban、cronjob、桌面窗口、协作消息、HomeAssistant | 差 65 个工具 |
| 8 | **L13 插件剩余（1 ⬜ + 4 🟡）** | dashboard 插件契约、懒发现、`all`/`pack` CLI | 5 条待补 |
| 9 | **L14 安全剩余（0 ⬜ + 4 🟡）** | tirith、egress、`path_security`/`url_safety` 四件套、脱敏出口未全覆盖 | 4 条待补 |
| 10 | **L12 剩余（1 ⬜ + 3 🟡）** | 19 个平台适配器全是占位；`gateway` 11 子命令全量、peer、monitoring 导出 | 4 条待补 |

---

## 风险与漂移

### R1 🔴 **计数工具的口径 bug：`matrix_stats.py` 把 1 条 🟡 算成了 ✅（本轮最重要）**

```python
# scripts/matrix_stats.py:37  count()
if DONE in line:          # ← 整行包含 ✅ 就算完成
    done += 1
```
而它的**自己的 docstring（:14）写的是**「状态取该行里出现的**第一个**标记」——**实现与文档不符**。

后果：`MATRIX 10.1` 状态列是 **🟡**，但证据栏含我上轮建议的措辞「→ **变 ✅ 的路径**」→ 被算成 done。

| 口径 | ✅ | 🟡 | ⬜ | 完成度 |
|---|---|---|---|---|
| `matrix_stats.py`（整行包含） | **98** | 44 | 39 | 54.1% |
| **状态列（权威）** | **97** | **45** | 39 | **53.6%** |

**全表只有 1 行**是「状态列符号 ≠ 行内唯一符号」：`10.1`（行内同时出现 ✅ 和 🟡）。所以修法很小：
1. `count()` 改成**读状态列**（按未转义的 `|` 切段取第 5 段）；
2. 加一条断言：**每行状态列必须是且仅是 ✅/🟡/⬜ 之一**（现在 181 行都满足，除了符号唯一性）；
3. 加一条断言：**行内不得出现与状态列不同的状态符号**（当前唯一违反者 10.1）。

> 讽刺的是：这个工具当初就是为"分母不能虚高"造的（`duplicate_ids` 门禁），结果**分子**被同一个 bug 污染了 1 条。而且污染源是我上轮写的建议措辞——**以后建议里不要写字面 ✅ 符号**，改写成"可转 done"。

### R2 🟡 **我自己的统计方法也有同一缺陷（已修正，需披露）**

第 2–6 轮我用的是 PowerShell 整行 `-match '✅'`，与本轮 `matrix_stats.py` 同一个口径。本轮改用**状态列解析**后才发现差异。
- 第 6 轮报的 83 条是否受影响：**无法回溯确认**（当时 10.1 的 🟡 证据栏还没有 ✅ 字样，大概率准确），但**从本轮起统一用状态列口径**，并在报告里注明。
- 教训：**"扫描符号"永远不可靠**——报告文本里会出现被引用的符号。必须定位到结构化字段。

### R3 🟡 **9.10 的 8 个 provider 名与 Hermes 的 8 个只有 `mem0` 重合**

- Hermes 侧（MATRIX 条目原文）：`byterover / hindsight / holographic / honcho / mem0 / openviking / retaindb / supermemory`
- Orbit 侧实测：`local / file / jsonl`（真实现 3 个）+ `mem0 / zep / letta / chroma / pgvector`（接口 5 个）
→ **接口数量 8 对上了，但名字只有 `mem0` 重合**。这不算虚报（🟡 且写明了 `available()` 恒 False），但建议在证据栏注明「**provider 名单按可用生态选，不是照抄 Hermes 的 8 家**」，否则下轮容易误以为"8 个 provider 已对齐"。

### R4 🟡 **未验证项（如实标注）**

| 项 | 为什么没验 |
|---|---|
| 9.15（`orbit memory {setup,status,off,reset}`）、14.6/14.10 之一 | 本轮新增 14 条里未逐条复核的 2 条 |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用，未逐个核数值（连续 2 轮如此） |
| `orbit chat` 交互式、`gateway start`、`cron run`、`backup create/restore`、`config edit`、`doctor --fix/--live` | 全部会改状态或真发请求 → 未运行 |
| `matrix_stats.py` 端到端 exit 1（重复编号门禁） | 只读固定 MATRIX.md、无路径参数；实跑需改禁改区（第 4 轮起一直如此） |

### R5 🟢 **上轮两条建议都已落地（记录以免重复提）**

| 上轮建议 | 本轮实测 | 状态 |
|---|---|---|
| 保住干净基线并写进文档口径 | 本轮仍是独占跑（1760 passed / 153.74s），且我复核到 ruff/mypy 全绿 | **已落地** |
| L4 的 fallback 接线（全表 ROI 最高） | `get_model()` 走 `FallbackChain` + `CredentialPool`，返回 `Route`；4.5/4.6 转 ✅，14 条用例 | **已落地** |
| （上轮 R2）10.1 补「写侧未统一」的理由 | MATRIX 10.1 证据栏**逐字采纳**了我的措辞（含「引用数全是 0」「变 ✅ 的路径」） | **已落地**（但引发了 R1） |

---

## 下一轮建议

按优先级：

1. **【P0】修 `matrix_stats.py::count()` 的口径**（R1）：改成读状态列，并加两条断言（状态列必须是三符号之一；行内不得出现与状态列不同的符号）。改完后表头应显示 **97 / 45 / 39 = 53.6%**。这一条会同时消除"分母门禁"与"分子虚高"两类风险。
2. **【P0】给 2.6 / 2.7 补「接线点」**：`Nodes.ask_model` 的 except 分支 → `errors.classify(exc)` → 仅 `RETRYABLE` 才重试。你撤回的处置是对的，但**接线点没写下来**，下一个人要重新推导。
3. **【P1】L2 是现在最弱的一层（6 ⬜ + 3 🟡，10 条里只 ✅1）**：它恰好是"接线类"欠账（错误分类、压缩重试两条已经写好但没接）。**把 2.6/2.7 接上，L2 立刻从 10% 涨到 30%**，比新开一层便宜得多。
4. **【P1】9.10 补一句 provider 名单口径**（R3），并考虑把 `local/file/jsonl` 之外的 5 个接口**要么标"仅为契约占位"**、要么换成 Hermes 的真实名单（byterover/honcho/…），否则"8 个 provider"这个数字会被误读。
5. **【P1】L10 的 10.1 该决定方向了**：要么把 cron/gateway/agent 的写入过 plane（变 ✅），要么把条目判据改成"统一读视图"并把"同库同表"降级为差异说明。现在这条 🟡 已经挂了 2 轮，且它的证据栏含 ✅ 字样正在污染计数。
6. **【P2】把"状态列唯一性"做成测试**：`tests/test_matrix_format.py` 里断言 181 行的状态列 ∈ {✅,🟡,⬜} 且行内无第二个状态符号。这样 R1 这类问题会在 CI 里当场红，而不是靠监视者肉眼发现。
7. **【P2】每条 ✅ 补「验证命令」列**：本轮 16.1 / 9.4 / 9.5 三条的证据质量极高（现场读文件、数调用次数、断言补偿后状态）；把这种"可一键复跑"的断言补到其余条目。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 5 轮未核。本轮顺带看到 `nodes.py:139 context_meta.setdefault("compressed", {})` 仍是空占位——正是那 15 条之一。

---

*本报告由 monitor 角色只读生成。本轮新增 14 条 ✅ 复核 12 条（86%），你点名的 7 条**全部撑得住**、4 条 🟡 **全部标得准**；`errors.py` 撤回的处置我判为**正确**。最重要的发现是计数工具把 1 条 🟡 算成了 ✅（真实 53.6%，不是 54.1%），且污染源是我上轮建议里的 ✅ 字面符号。未验证项单列，不混进完成项。*
