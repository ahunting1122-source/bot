# 对标进度报告

**更新时间**：2026-10-28 18:22（本机实测时钟 `Get-Date`，只读取证）
**本轮轮次**：**N = 17**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑会改状态或真发请求的命令。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `3d50b03`；**轮初工作树干净，轮末 5 项在飞**）

---

## 总体完成度

### **156 / 181 项 = 86.2% 严格 · 88.1% 可验证**（✅ 152 + 🟢 4）

| 项 | 说明 |
|---|---|
| 五态 | **✅ 152 · 🟢 4 · ⚫ 4 · 🟡 21 · ⬜ 0**（合计 181 ✓） |
| 严格 / 可验证 | **86.2%** / **88.1%** —— 从 89.0% / 91.0% 下降 |
| 与 `matrix_stats --check` | 逐字一致，exit 0 ✅ |
| 结构 / 状态列 | 181 行全合法，零异常 ✅ |

**下降的构成是清楚的、也是对的**：−6（新降级）+1（2.2 修好后恢复 ✅）= **净 −5 条 ✅**。
**关键判据**：**2.2 是"修好恢复"，不是"翻案"**——它现在有真接线（我逐段核了，见 A①），与那 6 条"查出虚报"方向相反。**这一进一出放在同一轮里，恰好说明状态机的两个方向都在动**。

### 硬口径：**全部一致**

| 你的口径 | 我的实测 | 判定 |
|---|---|---|
| ruff `All checks passed!` | **All checks passed!**（exit 0） | ✅ 一致 |
| mypy **171** files clean | **171 files clean**（exit 0） | ✅ 完全一致 |
| pytest 1 failed, 3500 passed, 3 skipped | **1 failed / 3500 passed / 3 skipped（343.73s）** | ✅ 一致（你 314.65s） |
| `matrix_stats --check` exit 0 | exit 0 | ✅ 一致 |
| REGISTRY 549，done **383** / partial **24** / todo **142** | **549；383 / 24 / 142**（连跑两次一致） | ✅ 完全一致 |

**命令面 done 379 → 383** 这个 +4，来源我核到了：`profile`（+1）+ `sessions`（+1）+ `cron`（+1）+ 其余 3 个同类父节点里的一部分 —— **正是 `recompute_group_states()` 修好的那 6 个**。

---

## 分层完成度

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | 15 ✅ + 1 🟢 / 18 | 88.9% | 🟡2 |
| L2 Agent 核心循环 | 9 / 10 | 90.0% | 🟡1（**2.2 已恢复**） |
| **L3 上下文装配** | **8 / 8** | **100%** | ✅ |
| L4 模型适配 | 6 ✅ + 2 ⚫ / 13 | 61.5% | 🟡5 |
| L5 工具层 | 16 / 19 | 84.2% | 🟡3 |
| **L6 工具集 / 注册** | **5 / 5** | **100%** | ✅ |
| L7 Skill / curator | **17 ✅ + 1 🟢** / 18 | 100% | ✅ |
| L8 MCP | 9 ✅ + 1 ⚫ / 10 | 90.0% | — |
| **L9 记忆层** | **16 / 16** | **100%** | ✅ |
| L10 持久化 / 会话 | 12 ✅ + 1 🟢 / 15 | 86.7% | 🟡2 |
| **L11 cron** | **6 / 6** | **100%** | ✅ |
| L12 Gateway / 多平台 | 8 ✅ + 1 ⚫ / 11 | 72.7% | 🟡2 |
| L13 插件 | 7 / 10 | 70.0% | 🟡3 |
| **L14 安全 / 审批** | 8 ✅ + 1 🟢 / 10 | 90.0% | 🟡1（14.8 降） |
| **L15 可观测性** | 5 / 6 | 83.3% | 🟡1（15.4 降） |
| **L16 Web UI / 前端** | **6 / 6** | **100%** | ✅ |
| **合计** | **156 / 181** | **86.2%** | — |

**七层 100%**：L3、L6、L7、L9、L11、L16（+ L10/L14 计入 🟢 后为 100%）。
**最弱**：L4 61.5%（5 🟡 + 2 ⚫）、L13 70.0%。

---

## 本轮抽查证据

**抽样率**：4 项 P0 落地**全部复核**；6 条新降级**逐条复核（100%）**；另**独立扫了全部 549 条命令的状态一致性**。

### A. 你点名的 4 项 —— **4/4 全部撑得住，其中两条比我预期的更稳**

**① 2.2 的四处接线 → ✅✅ 全链路串通，而且 `turns` 真的持久化（你担心的那个陷阱不存在）**

**完整链路我逐段核了**：
```python
# 1) 起点：settings → IterationBudgets
policies.py:415  turns = resolve_turn_budgets(settings)
policies.py:419  "max_turns": int(turns.parent_turns),        ← 进 budget

# 2) 判定：读 state 里的 turns
policies.py:445  max_turns = int(budget.get("max_turns") or 0)
policies.py:447      used_turns = int(state.get("turns") or 0)     ← 从 state 读
policies.py:449          return "max_turns"                        ← 真的返回路由名

# 3) 计数：也从 state 读
nodes.py:563   "turns": int(state.get("turns") or 0) + 1

# 4) 调用点
nodes.py:247   budget = dict(state.get("budget") or {}) or self.policies.budget.start(self.settings)
nodes.py:1434  decision = self.policies.budget.check({**state, "budget": budget})
```
**你担心"每轮都被重置、上限永远到不了"——不会发生**，原因是关键的那一行：
`"turns": int(state.get("turns") or 0) + 1` —— **从 `state` 读旧值再 +1**，而 `state` 过 checkpoint。
**反例长什么样**：如果写成 `self._turns += 1`（实例属性），才会每轮重置。**这里是 state，不是实例属性** ✓✓

**而且那段注释自陈得很干净**（`policies.py:409-413`）：
> 「**这条之前是虚的**——`turn_budgets.resolve()` 能算出父 90 / 子 50，`--max-turns` 也把值写进 state，但图里没有任何地方读它，所以一个不收敛的工具循环会一直跑下去。现在把它放进 budget，由 `check()` 硬停。」

**你那条 `actions` vs `turns` 的区分是本轮最有价值的一句话**：
> 「一个『只思考不调工具』的空转循环 `actions` 恒为 0，只靠 `max_actions` 拦不住」

这条**比我上轮给的判据更具体**——我只说了"迭代上限零强制"，你把它落成了"为什么必须单独一个计数器"。而且用**反证测试**钉住（拿掉 turns 时 `max_actions` 确实拦不住），这比正向测试强。

**② `recompute_group_states()` 会不会覆盖本该 partial 的父节点 → ✅✅ 不会，而且我全表扫过零不一致**

```python
def walk(command):
    for sub in command.subcommands: walk(sub)      # 先递归（自底向上）
    if not command.subcommands or command.handler is not None:
        return                                      # ← 有 handler 的节点直接跳过
    states = {sub.state for sub in command.subcommands}
    if states == {"done"}: want = "done"
    elif states & {"done", "partial"}: want = "partial"
    else: want = "todo"
```
**两道保险都对**：
- `handler is not None` → **有 handler 的节点状态是自己挣的，不被子节点覆盖** ✓
- 聚合规则与 `Spec.build()` 一致（`{"done"}` → done，含 partial → partial，否则 todo）✓
- `walk(sub)` 在计算自己**之前**递归 → 自底向上，子节点先定稿 ✓

**我独立扫了全部 549 条**：
```
无 handler 却与聚合不符的节点: 无（零不一致 ✓）
有 handler 且有子命令（被跳过、保留自己状态）: 3 个
  fallback (done, 4 子) · tools (done, 13 子) · dashboard (done, 3 子)
```
→ **零不一致成立**，且那 3 个"既有 handler 又有子命令"的节点确实被跳过 ✓✓

**③ `EMITTED_EVENTS` 的 warning 会不会刷屏 → ✅ 不会**

```python
hooks.py:270  if name in HOOK_EVENTS and name not in EMITTED_EVENTS:
hooks.py:271      log.warning(…)
```
**两个条件**决定了它不会刷屏：
- **只对"已知事件"（在 `HOOK_EVENTS` 里）且"无发射点"的告警** → 拼错的事件名不会触发这条（走另一条路径）
- **全仓零 `register_hook(` / `subscribe(` 调用方**（除 `hooks.py` 自己）→ **内置插件根本不订阅任何事件** → 这条 warning 只在第三方插件订那 13 个时才出现

**你选 warning 而不是抛错，理由我同意**：「钩子是旁路，抛错会让整个插件装不上；而且将来接上第 5 个发射点时不该改所有订阅方」。**补一条**：`EMITTED_EVENTS` 是 frozenset、可与 `HOOK_EVENTS` 做集合差，所以将来接上第 5 个发射点只需改这个集合一处，订阅方零改动 ✓ —— **这个设计让"接发射点"变成 O(1) 的改动**。

**④ `IterationBudgets` 改名有没有漏改 → ✅ 零残留**

```
旧名 TurnBudgets 残留（app/ · graph/ · cli/commands/ · tests/ 全扫）：空
新名 IterationBudgets：turn_budgets.py:29（docstring）/ :49（class）/ :94（resolve）/
                        :122（构造）/ :148（__all__）
```
**全在 `turn_budgets.py` 内部** ✓ 与你说的"4 处"一致（我数到 5，含一处 docstring 提及，都同文件）。

**你附的那句话我要单独肯定**：
> **一个会让读者得出相反结论的命名，本身就是缺陷。**

这句话值得进 `handoff/` 的方法论段。它把"命名"从风格问题提升成**正确性问题**——因为这个名字**实际造成了一次误判**（审计员一次、我一次），有实证。

### B. 6 条新降级 —— **6/6 全部成立**

| 行 | 我的复核 | 判定 |
|---|---|---|
| **5.13** | `send_clarify` 调用方只有 `peer_extra.py:283`，且那里写着「这是 `app.platforms.send_clarify` 的**手动验证入口**」→ `clarify` **工具**本身不走它 ✓ | ✅ 成立 |
| **6.4** | `FilterContext(` 在 `app/` 与 `graph/` 里**零构造点** → 四层过滤的 `context` 在 CLI 外从不传入 ✓ | ✅ 成立 |
| **10.7** | `OrphanReaper.reap()` 调用方只有 `session_plane.py:653`（= `orbit plane reap`）；`session_leases.py:168` 只构造 reaper 用于 `classify`（只读）→ **启动时无回收** ✓ | ✅ 成立 |
| **13.5** | 上轮已核（17 声明 / 4 有发射点） ✓ | ✅ 成立 |
| **14.8** | **模块 docstring 自己写着**（`path_security.py:11`）：「`website_policy` 与 `threat_patterns` **完全没有独立实现**」 ✓✓ | ✅ 成立（自陈即证据） |
| **15.4** | `metrics_schema` 唯一 importer 是 `observability_extra.py`（报告命令）→ **无生产生产者** ✓ | ✅ 成立 |

**14.8 与 6.4 属于同一类**：**证据栏/模块文档自己就承认了缺口**，却标着 ✅。这类最该早发现——**因为承认的那句话和错误的那个状态就在同一行/同一文件里**。

### C. `8.5` 不降级 —— **判断正确，且这条原则要立住**

> **证据错 ≠ 能力缺**——修正证据，状态保持 ✅。

我完全同意，并补一句为什么它重要：**如果把"证据引错模块"也降级，那 🟡 就会混进两种完全不同的东西**（"能力缺"与"文档写错"），而这两者的修复成本差一个量级。**状态机只该反映能力，文档错误该在文档层修。**

---

## 与目标标志的差距

**⬜ 0**，离目标还差 **21 条 🟡 + 4 条 ⚫**：

```
🟡 21：1.7 1.13b 4.3 4.4 4.8 4.9 4.11 4.12 5.2 5.13 5.14 6.4 10.7 10.8 10.14
       12.5 12.10 13.4 13.5 14.8 15.4
⚫ 4 ：4.2 4.7 8.3 12.9
```

### 你问的：`sessions` 父节点 done 会不会让人误以为完全对齐 —— **分两层看，我的答案与你的倾向一半相同**

**先说结论**：
- **registry 的父节点 done：保留，你的倾向对** ✓
- **但 MATRIX 10.13 那一行要改** —— 因为问题不在状态，在**那句话的位置**

**判据（这是核心）**：**"实现了"和"对齐了"是两个不同的断言。**
| 层 | 它回答什么 | `sessions` 的答案 |
|---|---|---|
| **registry（549 条命令面）** | 子命令**实现了**吗 | 27/27 都实现了 → **done 是事实** ✓ |
| **MATRIX（181 行对齐表）** | 与 Hermes **对齐**了吗 | 27 条里 `recover`/`retitle-skills` 语义不同 → **不是纯对齐** |

→ **所以父节点 done 本身不误导，它只承诺前者。** 你的倾向（保留 done + 语义差异写进子行证据栏）**方向是对的**。

**但有一个真实风险，而且它现在就存在**：MATRIX 10.13 的证据栏里写着「**`orbit sessions` 27/27 全部 done**」。**这句话出现在对齐表里**，读者会把它读成"27/27 都对齐了"。**同一个词在两个层面含义不同，而它们现在写在同一份文件里。**

**我的具体建议（三条）**：
1. **把那句话改一个字**：「27/27 全部 done」→「**27/27 全部有真实现**」。**"实现"归 registry，"对齐"归 MATRIX**，两个词不要互相借。
2. **加一段固定小标题 `【语义差异】`**，写清 `recover` / `retitle-skills` 的 Hermes 原语义 vs Orbit 的语义、以及**代价**（用户会看到什么不同的行为）。
3. **如果差异大到用户能感知** → 那一行应该标 **🟢** 而不是 ✅。判据用我们已有的：**"代价"能写成具体行为差异 → 🟢；写不出来 → 是 ✅**。

**另外给一个机械化建议**（因为这类会反复出现）：
这类"实现了但语义不同"现在已有 **4 例**（7.17、10.11、14.5 已 🟢，sessions 的 recover/retitle-skills 待定）。建议：
- MATRIX 统一用 `【语义差异】` 这个固定小标题
- `matrix_stats` 加一条提示（不是报错）：**标 ✅ 且含「语义差异」的行 → 提示"考虑 🟢"**
这样口径就统一了，也不会有第五例再靠人肉发现。

**顺带一个小漂移**：10.13 标题写「**19 子命令**」，正文写「**27/27**」——与 5.14 的「28 vs 33」是同一类（**标题滞后于正文**）。建议标题不写具体数字。

---

## 风险与漂移

### R1 🟢 **本轮方向正确，且 2.2 的修复质量高于预期**

我原本担心的两个陷阱（`turns` 被重置、`recompute` 覆盖合法 partial）**都不存在**：
- `turns` 从 `state` 读写 → 过 checkpoint 保留 ✓
- `recompute` 跳过有 handler 的节点 + 自底向上 ✓，全表零不一致 ✓

**而且你主动做了两件超出要求的事**：①`actions` vs `turns` 的区分写进注释并用**反证测试**钉住；②`IterationBudgets` 改名时把"会误导的命名本身就是缺陷"写进 docstring。

### R2 🟡 **「done」一词在两个层面含义不同（本轮唯一的实质风险）**

见上节。**registry 的 done = 实现；MATRIX 的 ✅ = 对齐**。现在 10.13 里两个词混用。
**影响面**：所有"子命令全实现了但语义有差异"的条目。已知 4 例。

### R3 🟡 **两处标题数字滞后**

`10.13` 标题「19 子命令」vs 正文「27/27」；`5.14` 标题「Orbit 现 28 个」vs 正文/实测 33（上轮已提）。
**建议**：标题只写 Hermes 侧数字（那是稳定的），Orbit 侧交给正文与实测。

### R4 🟡 **轮末 5 项在飞**

```
轮初 git status: 0 条目 ✓
轮末 git status: 5 条目
  M app/config.py  app/graph/nodes.py  app/graph/state.py  tests/test_max_turns_enforced.py
  ?? tests/test_max_turns_end_to_end.py     ← 2.2 的端到端测试在做
```

### R5 🟡 **未验证项（如实标注）**

| 项 | 为什么没验 |
|---|---|
| `2.2` 的**端到端**行为（真跑到 90 轮停下） | 要真跑一轮长对话；只核了接线链路与计数器持久化 |
| `recompute` 对**未来新增**父节点的效果 | 只核了当前 549 条 |
| 审计员报的其余中/低 SUSPECT（本轮未动的） | 未复核 |
| `12.10` 的 `enroll`、`4.12` 的 19 家逐家 | 要网络/凭证 → 未跑 |
| `orbit chat` 交互、`gateway start`、`cron run`、`backup create/restore`、`config edit`、`doctor --fix/--live` | 会改状态或真发请求 → 未运行 |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用（连续 12 轮如此） |

---

## 下一轮建议

按优先级：

1. **【P0】把「实现」与「对齐」两个词分开**（R2）：10.13 的「27/27 全部 done」→「27/27 全部**有真实现**」+ 加 `【语义差异】` 段。**这是本轮唯一会持续误导的口径问题**，而且已有 4 例。
2. **【P0】给 `matrix_stats` 加"✅ 且含语义差异 → 提示考虑 🟢"的软检查**：不报错、只提示。**否则第五例还会靠人肉发现。**
3. **【P1】`sessions` 的 `recover` / `retitle-skills` 单独判一次 🟢 vs ✅**：判据就一句——**代价能不能写成具体行为差异**。能 → 🟢；不能 → ✅ 并在证据栏说明。
4. **【P1】两处标题数字去 Orbit 侧**（R3）：`10.13` 的「19 子命令」、`5.14` 的「28 个」。标题写 Hermes 侧（稳定），Orbit 侧交给正文与实测。
5. **【P1】4.11 的 `moa run`（你下轮计划）**：注意 `orbit moa` 的 `list/configure/delete` 已能跑，所以这是**接线**。**补完请顺手加一条"配置了但没 run 过"的反证测试**——像 2.2 那样，反证比正证更能钉住。
6. **【P1】5.13 接 `clarify` → `send_clarify`**（你下轮计划）：`platforms.send_clarify` 已有真实现（含按钮降级为文本），缺的只是 `clarify` 工具调它。**接完检查一件事**：无按钮平台降级成文本时，**用户看到的是不是与原问题等价**——这是"降级"最容易丢语义的地方。
7. **【P2】10.7 的启动回收**（你下轮计划）：`reap()` 已在 CLI 通，接进 startup 时**注意别在无 DB 时抛错**（上轮 TUI 就是被 `require_db()` 坑过）→ 建议包 try/except + 一句原因。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 15 轮未核。`nodes.py:254 context_meta.setdefault("compressed", {})` 仍是空占位，而 `nodes.py` 连续四轮在改。

---

*本报告由 monitor 角色只读生成。**4 项 P0 全部撑得住，其中两条比我预期更稳**：2.2 的 `turns` 是**从 `state` 读写**（不是实例属性），所以过 checkpoint 会保留——**你担心的"每轮重置"不会发生**；`recompute_group_states()` 用 `handler is not None` 跳过自挣状态的节点、自底向上递归，**我独立扫了全部 549 条，零不一致**，且 3 个"有 handler 又有子命令"的节点（`fallback`/`tools`/`dashboard`）确实被跳过。`EMITTED_EVENTS` 的 warning 不会刷屏（全仓零内置订阅方），`IterationBudgets` 改名零残留。**6 条新降级逐条复核，6/6 成立**（14.8 与 6.4 是"文档自己就承认了缺口却标 ✅"）。**8.5 不降级判断正确**——证据错 ≠ 能力缺。关于 `sessions`：**registry 的 done 保留（你的倾向对），但 MATRIX 里「27/27 全部 done」要改成「全部有真实现」**——"实现"归 registry、"对齐"归 MATRIX，两个词不该互相借。未验证项单列，不混进完成项。*
