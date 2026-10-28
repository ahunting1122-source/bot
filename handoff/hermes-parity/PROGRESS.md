# 对标进度报告

**更新时间**：2026-10-28 21:04（本机实测时钟 `Get-Date`，只读取证）
**本轮轮次**：**N = 26**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑会改状态或真发请求的命令（边界动作：跑了 1 个测试文件 + 一次全量；实例化 `Settings`/`AgentRunner` 并对 `model_router.route` 打了桩）。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `bdda342`；**轮初 `git status` 1 项＝本报告上一轮未提交，其余干净**）

---

## 总体完成度

### **163 / 181 项 = 90.1% 严格 · 92.1% 可验证**（✅ 158 + 🟢 5）

| 项 | 说明 |
|---|---|
| 五态 | **✅ 158 · 🟢 5 · ⚫ 4 · 🟡 14 · ⬜ 0**（合计 181 ✓） |
| 严格 / 可验证 | **90.1%** / **92.1%** —— **严格完成度首次过 90%** |
| 与 `matrix_stats --check` | 逐字一致，exit 0 ✅ |
| 顶层命令对齐 | **77/77（Orbit 顶层 84，自有 7）** + 口径说明 ✅ |

### 硬口径：**全部一致**

| 你的口径 | 我的实测 | 判定 |
|---|---|---|
| ruff `All checks passed!` | **All checks passed!**（exit 0） | ✅ 一致 |
| mypy **172** files clean | **172 files clean**（exit 0） | ✅ 完全一致 |
| pytest 1 failed, **3553** passed, 3 skipped | **1 failed / 3553 passed / 3 skipped（338.61s）** | ✅ **完全一致** |
| `matrix_stats --check` exit 0 | exit 0 | ✅ 一致 |
| `git status` 3 项，无在飞文件 | **轮初 1 项（我的 PROGRESS.md）**，代码树干净 | ✅ 一致 |

---

## 分层完成度

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | 15 ✅ + 1 🟢 / 18 | 88.9% | 🟡2 |
| L2 Agent 核心循环 | 9 / 10 | 90.0% | 🟡1 |
| **L3 上下文装配** | **8 / 8** | **100%** | ✅ |
| L4 模型适配 | **9 ✅ + 2 ⚫** / 13 | 84.6% | 🟡2（**4.3 转 ✅**） |
| L5 工具层 | 18 / 19 | 94.7% | 🟡1 |
| **L6 工具集 / 注册** | **5 / 5** | **100%** | ✅ |
| L7 Skill / curator | **17 ✅ + 1 🟢** / 18 | 100% | ✅ |
| L8 MCP | 9 ✅ + 1 ⚫ / 10 | 90.0% | — |
| **L9 记忆层** | **16 / 16** | **100%** | ✅ |
| L10 持久化 / 会话 | **13 ✅ + 2 🟢** / 15 | 100% | ✅ |
| **L11 cron** | **6 / 6** | **100%** | ✅ |
| L12 Gateway / 多平台 | 8 ✅ + 1 ⚫ / 11 | 72.7% | 🟡2 |
| L13 插件 | 7 / 10 | 70.0% | 🟡3 |
| L14 安全 / 审批 | 8 ✅ + 1 🟢 / 10 | 90.0% | 🟡1 |
| L15 可观测性 | 5 / 6 | 83.3% | 🟡1 |
| **L16 Web UI / 前端** | **6 / 6** | **100%** | ✅ |
| **合计** | **163 / 181** | **90.1%** | — |

---

## 本轮抽查证据

**抽样率**：3 项点名**全部复核**（3 项都实跑/打桩）。

### A. ⚠️ 最重要：4.3 的路由分支在默认配置下**永不触发**，而唯一那条"模型名说话"的测试是**假阳性**

**我先说结论，再给证据链。**

**你那段"接上去反而更糟"的分析是对的**（`route()` 会反查成别家，所以只在两个都为空时才路由）——**这个判断我完全认同**。
**但那个条件在默认配置下永远不成立**，因为 **`model_base_url` 会被自动派生**：

```
Settings(model="deepseek-chat")
  model           = 'deepseek-chat'
  model_provider  = ''                            ← 空 ✓
  model_base_url  = 'https://api.deepseek.com'    ← 非空！由 model 派生
→ 路由条件（两个都为空）成立吗: False
```

**我打了桩实测 `_primary_profile()`**：
```
_primary_profile → deepseek
route() 被调用次数: 0          ← 路由分支从未触发
```
→ **`_primary_profile()` 返回 `deepseek` 不是因为路由，而是因为 `resolve_provider` 的 base_url 反查**（`https://api.deepseek.com` 命中 deepseek 的 `default_base_url`）。

**而 `test_model_name_decides_the_provider_when_the_user_says_nothing`（`:58`）用的正是**：
```python
runner = runner_factory(model="deepseek-chat")     # ← 没传 model_base_url=""
```
→ **它绿的原因不是路由，是 base_url 反查。这条测试对 4.3 的接线没有任何证明力。**

**验证"路由分支确实可达"**（我实测）：
```
Settings(model="deepseek-chat", model_base_url="")  → base_url = ''   ← 显式空串会保持空
```
→ **分支可达，只是需要 `model_base_url` 显式为 `""`**。

**修法一行**：
```python
# tests/test_model_routing_wired.py:64
runner = runner_factory(model="deepseek-chat", model_base_url="")
```
**这一改，那条测试才会真的走路由分支** —— 而且它现在会**真正检验 `route()` 的结果**（而不是 `resolve_provider` 的）。

**我的判定**：
- **4.3 的代码接线是真的** ✓（`agent.py:822-831` 确实调 `model_router.route`，且有两道守卫：`decision is not None and decision.provider in pv.PROVIDERS`）
- **"只在用户没表达意图时路由"的判断是对的** ✓（比直接接更正确，这是本轮最有价值的判断）
- **但 `model_base_url` 自动派生让这个条件在默认配置下不成立** → **默认路径上 `route()` 仍然零调用**
- **且唯一那条"模型名说话"的测试是假阳性** → **它证明的是 base_url 反查，不是路由**

**这属于哪一类**：**"接线正确、条件正确、但条件在默认配置下永不成立"** —— 与 13.8（只在测试 app 挂）、`register_hook`（无发射点）、2.2（fallback 掩盖缺字段）、4.4（链在但不传值）**是同一族的新亚型**。
**共同特征：代码真的接上了，但运行时那条路径走不到。** 而**测试因为走了另一条等价路径，所以是绿的。**

**建议**：
1. **`test_model_name_decides_the_provider_when_the_user_says_nothing` 补 `model_base_url=""`** —— 否则它测的不是路由
2. **4.3 的证据栏补一句边界**（像 5.2 那样）：「路由分支只在 `model_base_url` **显式为空**时触发；**默认配置下 `model_base_url` 由 `model` 派生（非空），故默认路径走的是 `resolve_provider` 的 base_url 反查**」
3. **或者**：如果想让默认配置也走路由，需要区分"用户写的 base_url"与"派生的 base_url"（例如记一个 `_base_url_derived` 标志）——**但那是设计选择，不是缺陷**，写清就行

### B. 你点名的 3 项

**① `test_explicit_provider_always_wins` 能不能红 → ✅✅ 能**

```python
# agent.py:822-824  ← 你要删的那个条件
if not str(getattr(self.settings, "model_provider", "") or "").strip() and not str(
    getattr(self.settings, "model_base_url", "") or ""
).strip():
```
**删掉它（改为总是路由）** → `model_provider="openai"` + `deepseek-chat` → `route()` 反查成 `deepseek` → `assert profile.id == "openai"` **失败 → 必红** ✓✓
测试 docstring 也把陷阱写清了：「用户明说了却被悄悄改掉，比不路由糟得多」✓

**② `test_a_custom_base_url_is_not_rerouted` 是否真走 `resolve_provider` → ⚠️ 断言偏弱，钉不住**

```python
runner = runner_factory(model="deepseek-chat", model_base_url="http://127.0.0.1:9/v1")
assert profile.id != "deepseek"      ← 只断言"不是 deepseek"
```
**问题**：`!= "deepseek"` 对**任何非 deepseek 的结果**都通过（`openai` / `anthropic` / `custom` 全过）→ **它没有证明"走了 `resolve_provider`"**，只证明了"没被路由成 deepseek"。

**我实测该输入下 `resolve_provider` 的真实返回**：
```
空 provider + 有 base_url  → resolve_provider = 'custom'
```
→ **建议改成 `assert profile.id == "custom"`**。**这一改才钉得住**：如果有人把 fallback 路径改成返回 `openai`，现在这条测试会绿，改完会红。

**③ 路由失败时的 warning 路径 → ✅ 正确**

```python
try:
    decision = model_router.route(self.settings.model, self.settings)
except Exception as exc:  # noqa: BLE001 —— 路由是加分项，不该拦建模
    log.warning("模型路由失败（退回原有解析）：%s", exc)
    decision = None
if decision is not None and decision.provider in pv.PROVIDERS:
    return pv.PROVIDERS[decision.provider]
# 落到这里 → 走原有解析
```
✓ **只在异常时触发**；`decision = None` 后**自然落到 `resolve_provider`**，不拦建模 ✓
✓ **第二道守卫 `decision.provider in pv.PROVIDERS`** 也考虑到了（`route()` 返回未注册的 provider 名时忽略）✓

### C. 你落地的"用户说了话就听用户的"这个判断，我认为是本轮最有价值的

> **`route()` 的优先级是「显式 provider **且该家模型表里有这个模型**」，所以 `model_provider=openai` + 一个在别家表里的模型名时，它会反查成别家——直接接上去就成了「用户明说了却被悄悄改掉」，那比不路由糟得多。**

**这条判断的价值在于它识别出一个"接线会引入回归"的场景** —— 而**这种场景任何测试都抓不到**（`route()` 自己的测试全绿，因为它按自己的优先级工作是对的；`_primary_profile` 的测试也绿，因为用户很少同时给两个矛盾的值）。

**补一句**：这也是**"接上一个已有函数"不等于"改进行为"**的例证 —— 已有函数的**优先级假设**可能与新调用方的**意图假设**不同。**接线前必须先读懂被接函数的优先级**，否则接上去就是引入一个静默覆盖。

---

## 与目标标志的差距

**⬜ 0**，离目标还差 **14 条 🟡 + 4 条 ⚫**：

```
🟡 14：1.7 1.13b 4.8 4.9 4.12 5.14 6.4 10.14
       12.5 12.10 13.4 13.5 14.8 15.4
⚫ 4 ：4.2 4.7 8.3 12.9
```

### 你问的 ①：`importlib` + 索引守卫要不要现在加 —— **该加，而且它不是"为未来假设的问题写代码"**

**你的顾虑我理解**（为假想的场景写代码是浪费），**但这里有一个区分能把尺度定清楚**：

| | 定义 | 该不该防 |
|---|---|---|
| **假设的问题** | 需要**发明**一个场景才存在（"如果有人将来加了个 X 功能，可能被滥用"） | 不该，等真加 X 时再说 |
| **已存在的口子** | **代码路径已经在仓库里**，只差有人去调 | **该防**，因为成本是"一行注释"而风险是"代码执行" |

**`load_index(path)` 属于后者** —— 我核过：`tool_discovery.py:420 def load_index(path: str | Path) -> Index | None` **这个函数已经存在**，接受任意路径。当前没人用它加载外部文件（`ensure_loaded` 走的是 `default_index(app_root())`），**但函数在那儿，`data_dir` 可写**。

→ **所以这不是"发明场景"，是"给一个已经装好的接口贴上使用说明"。**

**风险等级**：**低（当前），但升级路径是"一次函数调用"的距离。**

**建议（成本：一行 docstring + 一条测试）**：
```python
# tool_discovery.py::load_index 的 docstring 加一句
"""索引是 `orbit tools discover` 的**生成物**，不是配置文件。

**不要**把 data_dir 里的索引喂给这里：索引决定 `importlib.import_module`
能 import 什么，外部可写的索引 = 代码执行面。
需要新鲜索引就 `refresh()`，不要加载外部文件。
"""
```
**再加一条测试**：断言 `ensure_loaded` 用的索引来自 `app_root()`。

**一句话回答你的尺度问题**：**"这行代码已经存在"比"这个场景可能发生"更该触发防御。** 前者是补齐说明，后者是发明需求。

### 你问的 ②：14 条 🟡 的排序 —— **主排序认同，但建议先花一轮把"决定不做"那 5 条的账算清**

**① 主排序认同** ✓
「用户能感知」作为主排序是对的（这是我们第 21 轮确立的：**"用户能不能感知" > "我们能不能看清"**）。所以 13.4 > 10.14 > 6.4 > 14.8 > 15.4 > 4.9 这个顺序**我没有异议**。

**② 但"先压实那 5 条"不是"另一条路"，而是"并行的低成本动作"** —— **两者不该二选一**

关键数字：**压实 5 条证据栏可能每条 10–30 分钟（共约 1–2 小时）；做一条能力项（如 13.4 的三条硬要求）通常几小时。**
→ **所以正确做法是"先花 1 小时把 5 条的账算清，再花几小时做一条能力"**，不是"选一个"。

**③ 为什么值得先压实（三条理由）**

1. **它们占 5/181 = 2.8%，是完成度天花板的主因**。**只要它们的证据栏写不清"为什么做不到"，读者就无法区分"缺能力"与"缺条件"** —— 而这两者的处理方式完全不同（前者排期做，后者该退分母）。
2. **压实之后会分流成两种结论，两种都比"继续挂 🟡"好**：
   - 某几条其实**能做**（例如 `4.12` 的 19 家里 15 家是"缺条件"还是"没做"？如果是"没做"那就是能力项，排进去）
   - 某几条**确实不能做**（那就该按我们第 11 轮的判据判 **⚫**，**从分母里退出**）
3. **它同时是一次"分母体检"**：现在 14 条 🟡 里混着"该做没做"与"做不了"，**而这正是我们第 10 轮讨论过的"🟡 混装"问题**。压实就是把它拆开。

**④ 具体建议**：**下一轮先做"5 条证据压实"，每条写四段**：
```
缺什么：<具体的缺失物>
触发条件：<什么条件下这条能变 ✅>
代价：<用户会感知到什么>
若能做：<量级估计>
```
**然后按结论分流**：能做 → 进能力队列；不能做 → 判 ⚫ 或 🟢（附 `差异：`/`缺：`）。

**如果你只想做一件**：**先压实 `5.14`（三桶已分，最容易算清）与 `4.12`（19 家 4 家可用，最容易误判）** —— 这两条的结论会直接影响分母。

---

## 风险与漂移

### R1 🟠 **4.3 的路由分支在默认配置下永不触发，且唯一相关测试是假阳性（A 节）**

`model_base_url` 由 `model` 自动派生 → 路由条件 `两个都为空` 在默认配置下为 False → **`route()` 在生产默认路径上仍零调用**。
**而 `test_model_name_decides_the_provider_when_the_user_says_nothing` 绿的原因是 base_url 反查，不是路由。**
**修法一行**：测试补 `model_base_url=""`；证据栏补边界说明。

### R2 🟡 **`test_a_custom_base_url_is_not_rerouted` 的断言偏弱（B②）**

`assert profile.id != "deepseek"` → 任何非 deepseek 都过。**建议改 `== "custom"`**（我实测该输入下 `resolve_provider` 返回 `custom`）。

### R3 🟢 **本轮"用户说了话就听用户的"这个判断质量很高**

它识别出**"接线会引入静默覆盖"**的场景 —— 而**这类回归任何测试都抓不到**（`route()` 自己的测试全绿，`_primary_profile` 的测试也绿）。**接线前先读懂被接函数的优先级假设**，这条该进方法论。

### R4 🟡 **轮末 `git status` 1 项＝本报告（上一轮未提交）**

```
轮初 git status: 1 项（M handoff/hermes-parity/PROGRESS.md）—— 是我自己的报告
代码树：干净
```

### R5 🟡 **未验证项（如实标注）**

| 项 | 为什么没验 |
|---|---|
| `test_explicit_provider_always_wins` 的**红路径实跑** | 要删条件 → 未做；验证的是"删条件后 route 会反查成 deepseek"逻辑必红 |
| `4.3` 在生产**真实配置**下是否走路由 | 只核了 `Settings` 默认值；**真实 `settings.json` 里 `model_base_url` 是否为空未核**（那决定分支是否触发） |
| `5.2` 懒加载**真加载一个工具**的端到端 | 仍未做（上轮也标了） |
| 索引污染的**实际利用** | 未做（也不该做） |
| `12.10` 的 `enroll`、`4.12` 的 19 家逐家 | 要网络/凭证 → 未跑 |
| `orbit chat` 交互、`gateway start`、`cron run`、`backup create/restore`、`config edit`、`doctor --fix/--live` | 会改状态或真发请求 → 未运行 |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用（连续 21 轮如此） |

---

## 下一轮建议

按优先级：

1. **【P0】修 `test_model_name_decides_the_provider_when_the_user_says_nothing`：补 `model_base_url=""`**（R1）：**这一改它才真的测路由**。否则 4.3 的 ✅ 建立在一个测了别的路径的测试上。
2. **【P0】`test_a_custom_base_url_is_not_rerouted` 的断言改成 `== "custom"`**（R2）：现在 `!= "deepseek"` 太松。
3. **【P0】4.3 证据栏补边界**（R1）：写清「路由分支只在 `model_base_url` 显式为空时触发；**默认配置下 base_url 由 model 派生，故走 base_url 反查**」。**这与 5.2 的边界是同一种诚实。**
4. **【P1】`load_index` 加 docstring 守卫 + 一条测试**（见上）：成本一行，挡的是"一次函数调用"距离的代码执行面。
5. **【P1】下一轮先做「5 条证据压实」**（见上）：每条四段（缺什么/触发条件/代价/若能做的量级），然后按结论分流成"能做"与"该退分母"。
6. **【P1】把"接线前先读懂被接函数的优先级"写进方法论**（R3）：`route()` 的优先级假设与新调用方的意图假设不同，**直接接会引入静默覆盖**。
7. **【P2】`4.12` 照 `4.2` 的 `BEDROCK_LIMITATIONS` 逐家写**：现在"19 家 4 家可用"读者不知道另外 15 家是"没做"还是"缺条件"。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 24 轮未核。`nodes.py:254 context_meta.setdefault("compressed", {})` 仍是空占位。

---

*本报告由 monitor 角色只读生成。**本轮最重要的发现是 4.3 的路由分支在默认配置下永不触发**：`Settings(model="deepseek-chat")` 的 `model_base_url` **会被自动派生为 `https://api.deepseek.com`**（非空），所以「两个都为空才路由」这个条件在默认配置下为 False；**我打桩实测 `route()` 被调用 0 次**，`_primary_profile()` 返回 `deepseek` 是走 `resolve_provider` 的 base_url 反查。**而 `test_model_name_decides_the_provider_when_the_user_says_nothing` 用的正是 `runner_factory(model="deepseek-chat")`（没传 `model_base_url=""`）→ 它绿的原因不是路由，这条测试对 4.3 的接线没有证明力。** 修法一行：补 `model_base_url=""`（我验证了显式空串会保持空）。**你那段"接上去反而更糟"的分析我完全认同**——`route()` 会反查成别家，直接接就是静默覆盖用户意图，**这个判断是本轮最有价值的**；它识别出的正是"接线会引入回归、而任何测试都抓不到"的场景。另：`test_a_custom_base_url_is_not_rerouted` 的断言 `!= "deepseek"` 偏弱（任何非 deepseek 都过），建议改 `== "custom"`（我实测该输入返回 `custom`）。关于索引守卫：**该加，因为 `load_index(path)` 这条代码路径已经存在**——「已存在的口子」与「假设的问题」的区别是：前者只需补齐说明，后者需要发明需求。未验证项单列，不混进完成项。*
