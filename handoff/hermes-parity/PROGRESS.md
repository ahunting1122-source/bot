# 对标进度报告

**更新时间**：2026-10-28 18:35（本机实测时钟 `Get-Date`，只读取证）
**本轮轮次**：**N = 18**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑会改状态或真发请求的命令（边界动作：跑了 2 个测试文件、实例化了 Settings/AgentState 做字段自检）。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `53d59c2`；**轮初工作树干净，轮末 4 项在飞**）

---

## 总体完成度

### **156 / 181 项 = 86.2% 严格 · 88.1% 可验证**（✅ 152 + 🟢 4）

| 项 | 说明 |
|---|---|
| 五态 | **✅ 152 · 🟢 4 · ⚫ 4 · 🟡 21 · ⬜ 0**（合计 181 ✓） |
| 严格 / 可验证 | **86.2%** / **88.1%** —— **与上轮完全相同**（你说的"数字没动"成立 ✓） |
| 与 `matrix_stats --check` | 逐字一致，exit 0 ✅ |
| 结构 / 状态列 | 181 行全合法，零异常 ✅ |
| 软检查（本轮新增） | **精确命中 1 条**（10.13），exit 0，"提示（不判错，请人判）" ✅ |

**这轮没有数字可报，但它是这十几轮里含金量最高的一轮** —— 因为修掉的是**我上轮判定为 ✅✅ 的那个功能**。下面 A 节先认我的错。

### 硬口径：**全部一致**

| 你的口径 | 我的实测 | 判定 |
|---|---|---|
| ruff `All checks passed!` | **All checks passed!**（exit 0） | ✅ 一致 |
| mypy **171** files clean | **171 files clean**（exit 0） | ✅ 完全一致 |
| pytest 1 failed, 3501 passed, 3 skipped | **1 failed / 3501 passed / 3 skipped（346.84s）** | ✅ 一致（你 326.64s） |
| `matrix_stats --check` exit 0 | exit 0（软检查不判错） | ✅ 一致 |

---

## 分层完成度

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | 15 ✅ + 1 🟢 / 18 | 88.9% | 🟡2 |
| L2 Agent 核心循环 | 9 / 10 | 90.0% | 🟡1 |
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
| L14 安全 / 审批 | 8 ✅ + 1 🟢 / 10 | 90.0% | 🟡1 |
| L15 可观测性 | 5 / 6 | 83.3% | 🟡1 |
| **L16 Web UI / 前端** | **6 / 6** | **100%** | ✅ |
| **合计** | **156 / 181** | **86.2%** | — |

---

## 本轮抽查证据

**抽样率**：3 项点名**全部复核**（其中 2 项**实跑**）；另**主动核了一条你没让我核但与你问题直接相关的**（`--max-turns` 默认值 vs Hermes，见 B 节）。

### A. 我上轮的错 —— 先说这个

**你判得对，我判错了。** 我上轮写「2.2 的四处接线 → ✅✅ 全链路串通」，**那段链路我核的每一行都没错**：
```
policies.py:415  turns = resolve_turn_budgets(settings)
policies.py:419  "max_turns": int(turns.parent_turns)
policies.py:445-449  max_turns = budget["max_turns"]; used_turns = int(state["turns"]); if >= : return "max_turns"
nodes.py:563  "turns": int(state.get("turns") or 0) + 1
```
**但它当时不生效。** 我漏掉的是**上游两个字面量根本没被接住**。

**我为什么会漏**（这是本轮最该记的一条）：
`turn_budgets.resolve()` 的取值写法是
```python
found = _positive(getattr(settings, "max_turns", None))     # ← 带 fallback
...
if found is None: parent, parent_source = 90, "默认"
```
**`getattr(..., None)` + 回落默认值，会让"字段根本不存在"看起来和"用了默认值"一模一样。** 我读到 `:419 "max_turns": int(turns.parent_turns)` 就认为"值进来了"，**但那个值永远是回落出来的 90**。
**同理**：`AgentState` 是 `TypedDict`，未声明的键**静默丢弃**——`"turns": ... + 1` 写得再对，键不存在就等于没写。

**方法要补一条（比"跑一遍"更可操作）**：
> **凡是 `getattr(obj, name, default)` / `.get(name, default)` 的链路，必须单独验 `name` 真的在容器里**：
> - pydantic → `name in Model.model_fields`
> - TypedDict → `name in X.__annotations__`
> - dataclass → `name in {f.name for f in fields(X)}`
> **因为 fallback 的存在，缺字段的链路在静态阅读下与正常链路完全同形。**

这与我第 15 轮（browser）那条是同族的第三次：**读代码得出的"通"不能替代跑一遍**。区别是这次更隐蔽——**不是我看漏了，是代码本身让"缺"看起来像"有"**。

**你那句总结我完全同意，并且建议直接进方法论**：
> **正证（链路通）与反证（真跑能停）不能互相替代。**

### B. 三个静默丢弃是否真修了 → ✅✅ 三个都修了，我逐个实测

**① `Settings` 有 `max_turns` 字段** ✅
```
max_turns in Settings.model_fields: True
  默认值: None | 注解: int | None
Settings(max_turns=7).max_turns = 7          ← 不再被静默丢弃
```
**② `AgentState` 有 `turns` 字段** ✅
```
turns in AgentState.__annotations__: True
  类型: int
  全部键: actions, answer, budget, calls, context_meta, final, last_result,
          messages, plan, reflections, refs, task_id, turns, waiting
```
**③ 计数搬到了 `budget` 节点** ✅
```
nodes.py:1381  async def budget(self, state):        ← 所在函数
nodes.py:1438      turns = int(state.get("turns") or 0) + 1
nodes.py:1439      decision = self.policies.budget.check({**state, "budget": budget, "turns": turns})
nodes.py:1452      return {"budget": budget, "turns": turns}
```
**而 `think`（def `:423`）里已经没有 `turns` 了** ✅ —— 搬迁是干净的，不是两处都写。

**你那个"放错节点"的分析我特别认可**：图是 `think → gate → execute → digest → budget → gate …`，一次 `think` 后可多次 `gate→execute→budget` 循环，**放在 `think` 里会漏掉不经过 think 的循环——而那恰恰是最容易失控的形状**。这是**读图结构才能得出的结论**，不是读单点代码能发现的。

### C. `test_max_turns_end_to_end.py` 能不能红 → ✅ 结构上必红，且我实跑它现在绿

**实跑**：`5 passed in 3.47s`（`test_max_turns_end_to_end.py` + `test_max_turns_enforced.py`）✓

**为什么它必红**（断言结构）：
```python
:90   max_actions=100,
:91   max_turns=3,          ← 停下来的原因只可能是轮次
:113  stops = [e for e in events if e.get("event_type") == "budget_stopped"]
:114  assert stops, f"没有任何 budget_stopped 事件…"
:116  assert "max_turns" in reasons, f"停下来的原因不是 max_turns（实际：{reasons}）"
:123  assert turns >= 3
```
**把 `check()` 的 `max_turns` 分支注释掉** → `reasons` 只可能是 `{"max_actions"}`（100 次动作后停）或空 → `:116` 必失败 ✓✓
**`max_actions=100` 这个设置是关键**：它让"停在 max_turns"与"停在 max_actions"**在数值上不可能混淆**（3 ≪ 100）。**这正是我上轮建议的"反证"落地的样子，而且比我说的更严谨**——我只说"反证比正证强"，你把反证的**判据做成了无歧义的数字差**。

> ⚠️ 如实标注：我**没有实跑红路径**（要注释掉代码）。我验证的是"断言结构 + 数值设置证明它会红"，不是"我把它弄红过"。

### D. 软检查会不会误报 → ✅ 只针对 ✅，3 条 🟢 不可能触发

```python
matrix_stats.py:198  if status == DONE and "语义差异" in line:
:192  # 软检查（不判错，只提示）：标 ✅ 却在证据栏里写了「语义差异」
:197  # 写不出来 → 就是没做到。做成提示而不是错误，因为它需要人来判。
```
**`status == DONE` 是硬条件** → `7.17` / `10.11` / `14.5` 的状态是 `DIVERGENT`（🟢），**走不到这个分支** ✓

**实测输出**（我这一轮亲自跑的）：
```
条目 181：✅ 152 · 🟢 4 · ⚫ 4 · 🟡 21 · ⬜ 0
  严格完成度      86.2%  = (✅+🟢) / 总数（离目标多远）
  可验证完成度    88.1%  = (✅+🟢) / (总数−⚫）（本机能验的部分做完多少；⚫ = 4）
提示（不判错，请人判）：
  · 10.13：标了 ✅ 但证据里提到「语义差异」——若代价能写成具体行为差异，考虑标「有意差异」；写不出来就是没做到
```
**恰好 1 条、exit 0、措辞把判据也给了**（"代价能写成具体行为差异 → 🟢"）✓✓

### E. 「实现了」与「对齐了」分家 → ✅ 已落地

`10.13` 的「27/27 全部 done」已改成「27/27 全部**有真实现**」+【语义差异】段；标题的「19 子命令」也去掉了 Orbit 侧数字 ✓ —— 上轮那两条 P0 都到位。

---

## 与目标标志的差距

**⬜ 0**，离目标还差 **21 条 🟡 + 4 条 ⚫**（与上轮相同，无增减）：

```
🟡 21：1.7 1.13b 4.3 4.4 4.8 4.9 4.11 4.12 5.2 5.13 5.14 6.4 10.7 10.8 10.14
       12.5 12.10 13.4 13.5 14.8 15.4
⚫ 4 ：4.2 4.7 8.3 12.9
```

### 你问的：「默认值 90 合理吗」—— **同意不验"够不够"，但"抄得对不对"必须验，而且现在查出不对**

**先说结论分三层**：

**① "90 轮内正常任务一定能结束"这个测试不该写** —— 它是个伪命题。
**"正常任务"无法定义**。拿真实任务跑，要么太慢（你说得对，90 轮跑不动），要么就得造一个"90 轮内会结束"的合成任务——**而那种任务只能证明"我的合成任务在 90 轮内结束"，证明不了用户的任务**。**一个只能自证的测试，不如没有。**

**② 但"90 是不是从 Hermes 抄对的"必须验 —— 而我核出它抄错了一半** ⚠️

**Hermes 侧有明确证据**（`_raw/cmd-01-chat.txt:69-70`）：
```
--max-turns N   Maximum tool-calling iterations per conversation turn
                (default: 500, or agent.max_turns in config)
```
→ **Hermes 的 CLI `--max-turns` 默认是 500**，`agent.max_turns`（配置）才是 90。**两个数不是一个东西。**

**Orbit 侧现在是**：
```
chat.py:488   Option(("--max-turns",), "本轮最多几个 think→execute 循环（默认 90）")   ← CLI 说 90
hermes_parity.py:218  "--max-turns N": "每轮对话最多的工具调用迭代数（默认 500）"        ← SPEC 说 500
turn_budgets.py:9     | `agent.max_turns` | **90** |                                ← 配置表说 90
```
→ **Orbit 内部自己就矛盾**：SPEC 写 500、CLI 写 90、实现走 90。
**后果**：**Orbit 的 CLI 默认比 Hermes 严 5.5 倍**。一个在 Hermes 里能跑 500 次迭代的任务，在 Orbit 里 90 次就被停掉——**而且用户看到的是"任务被预算停下来了"，不会知道是默认值设窄了。**

**这就是"不验它是配置"会漏掉的那类东西**：它不是"90 够不够"（用户偏好），是**"90 与 500 差 5.5 倍"（对齐缺陷）**。建议：
- 若 Hermes 真是 CLI 500 / config 90 → **Orbit 的 CLI 默认应改 500**，`agent.max_turns=90` 保留为配置默认（两者分工与 Hermes 一致）
- 无论改不改，**SPEC 与 CLI 的文案必须先统一**（现在一句写 500 一句写 90，无论谁对，读的人都无法判断）

**③ 更该验的是"停了说不说清楚"** —— 这个可验、该验，而且现在**没做** ⚠️

`nodes.py:1444-1452` 的 `budget_stopped` 载荷是：
```python
"budget_stopped", reason=decision, used_actions=…, max_actions=…, max_turns=…, turns=…, elapsed=…
```
**它带了数字，但没有一句话告诉用户"你可以用 `--max-turns` 调大"**。
**为什么这条比"90 够不够"重要**：90 够不够是**用户的事**（有人要 90、有人要 500）；**"停了却不说为什么、怎么办"是产品的事**。前者不可验，后者可验。
**建议**：`reason == "max_turns"` 时在事件/最终回复里带一句「本轮达到轮次上限（N），可用 `--max-turns` 调整」。**这条能写成测试，而且不慢。**

---

## 风险与漂移

### R1 🟠 **`--max-turns` 默认值与 Hermes 不一致（5.5 倍），且 Orbit 内部文案自相矛盾（B②）**

`cmd-01-chat.txt:69-70` 明写 Hermes CLI 默认 **500**；`hermes_parity.py:218`（Orbit 的 SPEC）也写 **500**；但 `chat.py:488` 与实现都是 **90**。
**这是本轮新发现，也是"不验配置"最容易漏的一类**：它不是偏好问题，是对齐问题。

### R2 🟡 **`budget_stopped` 不说"怎么办"（B③）**

带数字、不带指引。用户被停了却不知道能调。**可验、该验、成本低。**

### R3 🟢 **本轮的修复质量高于问题本身**

三个静默丢弃不是"顺手改一下"能发现的——尤其 ①`Settings` 缺字段（pydantic 静默丢 kwarg）和 ③计数放错节点（需要读图结构才知道 `think` 会漏掉 `gate→execute→budget` 循环）。**而且你用"e2e 真跑 + max_actions=100 制造无歧义判据"钉住，这个做法应该成为所有"机制类"条目的标准动作。**

### R4 🟡 **轮末 4 项在飞**

```
轮初 git status: 0 条目 ✓
轮末 git status: 4 条目
  M app/cli/commands/__init__.py  handoff/hermes-parity/MATRIX.md
  ?? tests/test_max_turns_cli_path.py     ← 已经在补 CLI 路径测试
  ?? tests/test_moa_run_wired.py          ← 4.11 在做
```

### R5 🟡 **未验证项（如实标注）**

| 项 | 为什么没验 |
|---|---|
| `test_max_turns_end_to_end` 的**红路径实跑** | 要注释掉代码 → 未做；只验证了断言结构 + 数值设置证明必红 |
| Hermes 的 `agent.max_turns = 90` 是否真有原文证据 | 我核到的是 **CLI 默认 500**（`cmd-01-chat.txt:69-70`）；90 那个数来自 `turn_budgets.py` 的表格，**其原始出处（§1.1/1.2）我没打开核** |
| 软检查对**未来**新增条目的效果 | 只核了当前 181 行 |
| `12.10` 的 `enroll`、`4.12` 的 19 家逐家 | 要网络/凭证 → 未跑 |
| `orbit chat` 交互、`gateway start`、`cron run`、`backup create/restore`、`config edit`、`doctor --fix/--live` | 会改状态或真发请求 → 未运行 |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用（连续 13 轮如此） |

---

## 下一轮建议

按优先级：

1. **【P0】查清 Hermes 的 `--max-turns` 默认到底是 500 还是 90，然后把 Orbit 的两个数统一**（R1）：CLI 默认 500 / 配置 `agent.max_turns` 90 是 Hermes 的分工，Orbit 现在把两者混成一个 90，**比 Hermes 严 5.5 倍**。**这是对齐缺陷，不是偏好。**
2. **【P0】`budget_stopped` 加一句"可用 `--max-turns` 调整"**（R2）：90 够不够是用户的事，**"停了不说怎么办"是产品的事**。这条能写成测试且不慢——比"90 轮内正常任务能结束"那个伪命题值得做得多。
3. **【P1】把「e2e 真跑 + 无歧义判据」固化成机制类条目的标准动作**（R3）：`max_actions=100` 对 `max_turns=3` 这个设计是模板——**判据要在数值上不可能混淆**，否则测试绿了也不知道测的是哪条。
4. **【P1】4.11 `moa run`（你下轮计划）**：你打算"先写反证测试"，对。**补一条具体建议**：反证要像 2.2 那样**制造无歧义**——比如"配置了 MoA 但没 `run`"必须报出 `moa 未运行`，而不是静默返回空结果。
5. **【P1】把"带 fallback 的取值必须单独验字段存在"写进方法论**（A 节）：这是本轮唯一一条**能预防同类错误**的规则，比"多跑测试"更可操作——因为**它指向了"什么时候必须跑"**。
6. **【P2】`5.13` 的 `clarify` → `send_clarify`**（你之前计划）：接完检查**无按钮平台降级成文本时，用户看到的是不是与原问题等价**——这是"降级"最容易丢语义的地方。
7. **【P2】`10.7` 的启动回收**：接进 startup 时包 try/except + 一句原因，**别在无 DB 时抛错**（TUI 被 `require_db()` 坑过一次）。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 16 轮未核。`nodes.py:254 context_meta.setdefault("compressed", {})` 仍是空占位，而 `nodes.py` 连续五轮在改。

---

*本报告由 monitor 角色只读生成。**先说我的错**：上轮我判 2.2「✅✅ 全链路串通」——那段链路我核的每一行都对，但**功能当时不生效**，因为 `turn_budgets.resolve()` 用 `getattr(settings, "max_turns", None)` **带 fallback**，缺字段与用默认值在静态阅读下完全同形。**新方法规则**：凡 `getattr(x, name, default)` / `.get(name, default)` 的链路，必须单独验 `name in Model.model_fields` / `name in X.__annotations__`——**它指向了"什么时候必须跑"，比"多跑测试"可操作**。三个静默丢弃我逐个实测都修好了（`Settings.model_fields` 有 `max_turns`、`AgentState.__annotations__` 有 `turns`、计数在 `budget` 而非 `think`）；e2e 用 `max_actions=100` 对 `max_turns=3` 制造无歧义判据，结构上必红；软检查 `status == DONE` 是硬条件，3 条 🟢 不可能误报，实测只命中 10.13。**关于"默认值 90"：不验"够不够"是对的（伪命题），但我核出"抄得不对"——Hermes CLI 默认 500（`cmd-01-chat.txt:69-70`），Orbit 的 CLI 却写 90 而 SPEC 写 500，内部自相矛盾且比 Hermes 严 5.5 倍；另 `budget_stopped` 带了数字却没告诉用户可调。** 未验证项单列，不混进完成项。*
