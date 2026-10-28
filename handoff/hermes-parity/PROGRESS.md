# 对标进度报告

**更新时间**：2026-10-28 20:20（本机实测时钟 `Get-Date`，只读取证）
**本轮轮次**：**N = 23**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑会改状态或真发请求的命令（边界动作：跑了 3 个测试文件 + 一次全量 + 若干 `--help`）。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD **`bf2383e`**；轮初 `git status` 1 条目在飞，**轮末 0 项，仅本报告本身未提交**）

> **本轮含一次轮中更正**：我先查出 `orbit plane leases` 不存在（A④），**你在同一轮内修好并做成了机制**（`bf2383e`）。**下面 A④ 与 R1 已按修复后的状态改写**，并保留"先查出问题"的原始记录——因为**这条的价值不在修得快，而在它被做成了守卫**。

---

## 总体完成度

### **161 / 181 项 = 89.0% 严格 · 91.0% 可验证**（✅ 156 + 🟢 5）

| 项 | 说明 |
|---|---|
| 五态 | **✅ 156 · 🟢 5 · ⚫ 4 · 🟡 16 · ⬜ 0**（合计 181 ✓） |
| 严格 / 可验证 | **89.0%** / **91.0%** —— 从 88.4% / 90.4% 上升（`10.8` 转 ✅） |
| 与 `matrix_stats --check` | 逐字一致，exit 0 ✅ |
| **顶层命令对齐（新）** | **77/77（Orbit 顶层 84，自有 7）** + 口径说明 —— **已进统计出口** ✅ |

### 硬口径

| 你的口径 | 我的实测 | 判定 |
|---|---|---|
| ruff `All checks passed!` | **All checks passed!**（exit 0） | ✅ 一致 |
| mypy **172** files clean | **172 files clean**（exit 0） | ✅ 完全一致 |
| pytest 1 failed, **3534** passed, 3 skipped | **1 failed / 3536 passed / 3 skipped（370.16s）**；修完后基线亦为 **3536** | ✅ **一致**（+2 见下） |
| `matrix_stats --check` exit 0 | exit 0 | ✅ 一致 |
| `git status` 6 项，无在飞文件 | **轮初 1 项在飞（`M app/graph/nodes.py`），轮末 0 项** | ⚠️ 见下 |

**`+2` 的来源**：`tests/test_session_lease_wiring.py` 有 **2 条用例**（`test_active_sessions_file_is_created_and_cleaned` + `test_takeover_makes_the_original_task_yield`），**我跑时它们已在，你的基线里没有**。结合 `nodes.py` mtime `20:07:40`（你报基线之后），**你的 3534 是干净基线，我的 3536 多了这 2 条**；你修完 `bf2383e` 后基线也是 **3536** —— **两个数现在收敛到同一个值。**
→ **这是第四次在飞漂移，但这次只有 +2 且来源精确到文件**。**带 `git status` 的习惯仍然有效**——它让我立刻知道"有 1 项在飞"，从而不去怀疑那 +2 是回归。

### 我上轮的建议：**逐字落地了** ✅

```
顶层命令对齐    77/77（Orbit 顶层 84，自有 7）
  ※ 口径：顶层命令**名**集合差（含 5 个已知改名；**不含子命令级**）；
    **只证明没有整条命令缺失，不证明功能对齐**
```
**三件事都做到了**：① 不占 181 行（不引入新分母）② 放 `matrix_stats` 出口（与测试同源）③ **口径写在数字旁边**。**这是"建议被完整执行"的最好一次。**

---

## 分层完成度

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | 15 ✅ + 1 🟢 / 18 | 88.9% | 🟡2 |
| L2 Agent 核心循环 | 9 / 10 | 90.0% | 🟡1 |
| **L3 上下文装配** | **8 / 8** | **100%** | ✅ |
| L4 模型适配 | 8 ✅ + 2 ⚫ / 13 | 76.9% | 🟡3 |
| L5 工具层 | 17 / 19 | 89.5% | 🟡2 |
| **L6 工具集 / 注册** | **5 / 5** | **100%** | ✅ |
| L7 Skill / curator | **17 ✅ + 1 🟢** / 18 | 100% | ✅ |
| L8 MCP | 9 ✅ + 1 ⚫ / 10 | 90.0% | — |
| **L9 记忆层** | **16 / 16** | **100%** | ✅ |
| L10 持久化 / 会话 | **13 ✅ + 2 🟢** / 15 | 100% | 🎉 本轮补齐（**10.8 转 ✅**） |
| **L11 cron** | **6 / 6** | **100%** | ✅ |
| L12 Gateway / 多平台 | 8 ✅ + 1 ⚫ / 11 | 72.7% | 🟡2 |
| L13 插件 | 7 / 10 | 70.0% | 🟡3 |
| L14 安全 / 审批 | 8 ✅ + 1 🟢 / 10 | 90.0% | 🟡1 |
| L15 可观测性 | 5 / 6 | 83.3% | 🟡1 |
| **L16 Web UI / 前端** | **6 / 6** | **100%** | ✅ |
| **合计** | **161 / 181** | **89.0%** | — |

**八层 100%**：L3、L6、L7、L9、**L10（本轮补齐）**、L11、L16。

---

## 本轮抽查证据

**抽样率**：3 项点名**全部复核**（3 项都实跑）+ **你点名要我核的那条命令**（见 A④）。

### A. 你点名的 3 项 + 1 条命令

**① `test_takeover_makes_the_original_task_yield` 能不能红 → ✅✅ 能，而且它断言的是**具体原因**不是"停了"**

```python
# tests/test_session_lease_wiring.py
monkeypatch.setattr(nodes_module, "LEASE_HEARTBEAT_SECONDS", 0.0)
#   ↑ 注释：验的是接管语义，不是节流间隔
... 轮询等账本出现，超时则 AssertionError("任务跑起来了却没登记会话租约——acquire 没接上")
runner.leases.acquire(conversation.id, platform="other", pid=999_998, start_time=1.0)   # 模拟另一进程接管
...
reasons = {str(json.loads(p).get("reason")) for (p,) in rows if p}
assert "session_takeover" in reasons, (
    f"被接管后没有停下（budget_stopped 的原因：{reasons}）——"
    "两边会继续往同一份 checkpoint 交错写")
```
**去掉 `nodes.py:1590` 的 `lost = self._lease_lost(state["task_id"])`** → `decision` 只可能来自 `budget.check(...)` → `reasons` 里只会有 `max_actions`/`max_turns`/`max_tokens` → **`session_takeover` 缺席 → 必红** ✓✓

**三处设计我很认可**：
- **断言的是 `reason == "session_takeover"`，不是"任务停了"** —— 否则 `max_actions` 撞上也会绿，**测的就不是接管了**
- **失败消息点名后果**（"两边会继续往同一份 checkpoint 交错写"）—— 下一个人看到红立刻知道代价
- **`monkeypatch` 关掉节流 + 注释说明"验的是接管语义，不是节流间隔"** —— 这是你自述的第二个坑，处理方式正确：**测试要验什么就只验什么，不要让一个无关的优化参数成为被测对象**

**还带一条防空转断言**（"任务跑起来了却没登记会话租约——**acquire 没接上**"）—— **连续第三轮出现**，已经成了你的习惯 ✓

**② 节流比例是否合理 → ✅✅ 10s vs 90s = 9 倍余量，合理；而且你的 docstring 给了比"10 秒足够"更硬的论证**

```
nodes.py:47            LEASE_HEARTBEAT_SECONDS = 10.0
session_leases.py:62   DEFAULT_MAX_AGE = 90.0
```
→ **检测延迟 ≤ 10s，租约 90s 才过期 → 9 倍安全余量** ✓ 合理。

**但真正值得记的是 `nodes.py:1492-1496` 的理由**：
> 「租约的 `max_age` 是 90 秒，所以每 10 秒查一次完全够（**比"每轮查"安全**：每轮查会在快循环里把盘写爆，慢循环里反而查得太稀）。」

**这个论证比"10 秒足够"强一个量级**：它指出**"每轮"这个粒度在两个方向上都是错的** —— 快循环写爆盘、慢循环查太稀。
**根因**：**被检查的东西（租约年龄）以时间计量，不以轮次计量** → **时间型节流是唯一正确的粒度**。
**这条可以抽象成一句规则**：**节流的粒度必须与被检查量的计量单位一致**（时间型的量用时间型节流，轮次型的量用轮次型节流）。

**③ `release` 是否真在 `finally` 里 → ✅✅ 在，且理由写清了**

```python
# agent.py:659-671
finally:
    self.partial.pop(task.id, None)
    self._token_usage.pop(task.id, None)
    # 释放会话租约（MATRIX 10.8）。**放在 `finally` 里**：失败/取消也要放，
    # 否则一个崩掉的任务会让那个会话永远显示"有人持有"，下次启动的
    # `reap_stale` 才能清掉——而那时用户已经困惑过了。
    try:
        self.leases.release(task.conversation_id)
    except Exception as exc:
        log.warning("会话租约释放失败（不影响任务结果）：%s", exc)
```
✓✓ 在 `finally` 内、包 try/except、理由完整（"否则…永远显示'有人持有'…而那时用户已经困惑过了"）。
**并且 `acquire` 也真接上了**（`agent.py:574 self.leases.acquire(task.conversation_id, platform="cli")`）—— 从"零生产调用方"变成了真调用 ✓

**④ 你点名要我核的：`orbit plane leases` 存在吗 → ❌ 不存在，但你在同一轮内修好并做成了机制**

**原始问题（我核出来的）**：
```
plane 子命令: ['timeline','tail','lineage','show','resume','reap','deliveries','backfill','compact-check']
leases 在其中: False
$ orbit plane leases --help  → argparse 报错，可选值里没有 leases
```
→ hint 当时指向一个**不存在的命令**。**这比"没有提示"更糟**：用户照着敲会得到 argparse 报错，**然后以为是自己敲错了**。

**修复后（我逐项核过 `bf2383e`）**：
```
nodes.py:1805  "要继续：等那边结束，或看 `runtime/active_sessions.json` 里谁持有"
nodes.py:1806  "（`orbit plane show <会话>` 能看到该会话的平面视图）。"
```
| 我核的 | 结果 |
|---|---|
| 旧串 `orbit plane leases` 是否清除 | **全仓零残留** ✓ |
| `orbit plane show` 是否真存在 | **存在**，`usage: orbit plane show [-h] …` 实跑通过 ✓ |
| 指向的文件是否真实 | `runtime/active_sessions.json` 是租约账本本身（`session_leases` 写它）✓ |

**而且它被做成了机制** —— `tests/test_hint_commands_exist.py`，我读了源码，**它真能红**：
```python
_ORBIT_CMD = re.compile(r"`orbit ([a-z][a-z0-9-]*(?: [a-z][a-z0-9-]*){0,2})")
def _resolve(path):
    ... 逐级走注册表，跳过 --option，任一级不存在就返回 None
def test_every_command_named_in_stop_hints_exists():
    start = source.find("def _stop_hint")
    assert start != -1, "找不到 `_stop_hint`——测试失效了"              ← 防空转 ①
    mentioned = sorted({m.group(1) for m in _ORBIT_CMD.finditer(body)})
    assert mentioned, "没解析出任何 `orbit <cmd>`——正则或文案结构变了"   ← 防空转 ②
    missing = [path for path in mentioned if _resolve(path) is None]
    assert not missing, "提示里指向了不存在的命令：…用户照着敲会报错"
```
**把 `orbit plane leases` 写回去 → `_resolve("plane leases")` 返回 `None` → 必红** ✓✓
**关键设计：子命令级也查**（`{0,2}` 允许三段路径）—— **只查顶层正好会漏掉这一类**。**实跑 2 passed** ✓

**这条我给的评价最高，理由是它修的不是"一句话"而是"一类错"**：
- `_stop_hint` 是**唯一一类"代码正确、类型自洽、lint 干净，但用户照做会失败"的东西** —— 因为它只是字符串
- **静态检查原理上看不见它**（mypy/ruff 不解析文案语义）
- 所以需要**语义级守卫**：把文案里的命令解析出来，对注册表核 —— **你做的正是这个**

**⚠️ 但第二条测试有一个我要指出的弱点**：
```python
reasons = ("max_actions", "max_tokens", "max_turns", "session_takeover")
for reason in reasons:
    assert _stop_hint(reason, budget, 3).strip(), f"`{reason}` 没有提示"
```
**`reasons` 是硬编码的元组**，而 docstring 写的是「策略与租约机制**可能产出的全部**原因」。
→ **将来 `policies.check()` 新增一个 stop reason（比如 `max_seconds`），这条测试不会知道** —— 它只遍历那 4 个，**照样绿**。
**这正是我上轮说的"清单会过期，性质不会"**：第一条测试是**性质断言**（扫源码、解析命令、查注册表），第二条是**清单断言**（硬编码 4 个）。
**建议改法**：让策略自己声明原因集合（`DefaultBudgetPolicy.STOP_REASONS = ("max_actions","max_tokens","max_turns")`），租约机制声明 `"session_takeover"`，测试遍历**它们的并集** → **新增原因会自动要求写提示**。

### B. 你选 10.8 而不是 5.2 的理由 —— **认同，而且理由可以更硬两条**

你说「效率问题用户能感觉到慢，数据损坏用户只会觉得'这 AI 今天怪怪的'」——对。我加两条：

1. **数据损坏不可逆，效率问题可逆**。交错写进同一份 checkpoint 之后，**事后无法分离**哪条消息属于哪次驱动；而慢只要下次跑快点就行。**判据：能不能事后修复。**
2. **数据损坏没有信号**。慢有信号（用户知道在等），损坏没信号（用户会归因到"模型今天不行"或"我自己说错了"）—— **无信号的缺陷比有信号的缺陷危险一个量级。**

**所以判据是两层**：**「有没有信号」+「能不能事后修复」** —— 数据损坏两条都输，效率问题两条都赢。**你的判断对，而且比"用户能感觉到"更可判。**

### C. 你自述的两个测试坑 —— 都对，第二个尤其值得记

1. **单跑绿、全量红**（状态转终态在 `try` 里、`release` 在 `finally` 里，中间有窗口；查一次就断言 = 断言了还没到的状态）
   → **这是异步测试的通病**，你的修法（轮询等释放）对。**判据**：**凡是"某个副作用发生在另一个块里"的测试，都不能只查一次。**
2. **节流让接管测试要等 10 秒 → monkeypatch 设 0**
   → **这条更值得记**：它的价值不在"让测试快"，而在**把被测对象与无关参数解耦**。**注释"验的是接管语义，不是节流间隔"就是这份解耦的声明。**

---

## 与目标标志的差距

**⬜ 0**，离目标还差 **16 条 🟡 + 4 条 ⚫**：

```
🟡 16：1.7 1.13b 4.3 4.8 4.9 4.12 5.2 5.14 6.4 10.14
       12.5 12.10 13.4 13.5 14.8 15.4
⚫ 4 ：4.2 4.7 8.3 12.9
```

### 一处要判：`orbit plane leases` 不存在 —— **已修，而且修法比我建议的更好**

见 A④。**我建议的是"改成 `orbit sessions leases`"，你改成了 `runtime/active_sessions.json` + `orbit plane show <会话>`** ——
**比我建议的更好**，因为：`orbit sessions leases` 只是"另一个命令"，而**指向租约账本文件本身**让用户能直接看到"谁持有、什么时候心跳"；`orbit plane show <会话>` 则给了**该会话的平面视图**（能看到它跑到哪一步）。**这是"给数据"而不是"给命令"，更接近用户真正想知道的。**
**并且做成了机制**（`tests/test_hint_commands_exist.py`，子命令级也查）—— **我核过源码，把旧串写回去必红**。
**唯一的改进建议**：第二条测试的 `reasons` 元组是硬编码的，建议改成从策略与租约机制**各自声明**再取并集（详见 A④ 末）。

---

## 风险与漂移

### R1 🟢 **hint 指错命令：已修，且修成了守卫（本轮最有价值的一件事）**

`orbit plane leases` 不存在（我核出来的）→ **同一轮内**改成 `runtime/active_sessions.json` + `orbit plane show <会话>`（两者我都实跑确认存在）+ `test_hint_commands_exist.py` 两条守卫（**子命令级也查**，旧串写回必红）。
**这条的价值不在"修得快"，而在它被做成了一类错的守卫**：`_stop_hint` 是唯一一类"代码正确、类型自洽、lint 干净，但用户照做会失败"的东西 —— **静态检查原理上看不见它**。**你把它变成了语义级可验证。**
**剩下一个小口子**：第二条测试的 `reasons` 是硬编码清单（不是从策略派生），新增 stop reason 时它不会红。**建议改成策略声明 + 并集遍历。**

### R2 🟡 **第四次在飞漂移，但这次 +2 且来源精确**

`test_session_lease_wiring.py` 的 2 条用例在你基线之后落地；`nodes.py` mtime 20:07:40。
**带 `git status` 的习惯继续有效**——它让我知道有 1 项在飞，从而不去怀疑 +2 是回归。

### R3 🟢 **`10.8` 的修复质量高，且"节流粒度"那条论证值得进方法论**

「每轮查会在快循环里把盘写爆，慢循环里反而查得太稀」→ **节流粒度必须与被检查量的计量单位一致**（时间型量用时间型节流）。

### R4 🟡 **未验证项（如实标注）**

| 项 | 为什么没验 |
|---|---|
| `test_takeover` 的**红路径实跑** | 要删 `nodes.py:1590` 的 `lost = ...` → 未做；验证的是"断言的是 `session_takeover` 具体原因"结构必红 |
| 真两进程并发的接管 | 测试用 `acquire(pid=999_998, start_time=1.0)` 模拟，**没有真起第二个进程** |
| `Settings` 裸属性全扫（你上轮 P0 ③） | 你本轮未做，我也未做 —— **仍是待办** |
| `12.10` 的 `enroll`、`4.12` 的 19 家逐家 | 要网络/凭证 → 未跑 |
| `orbit chat` 交互、`gateway start`、`cron run`、`backup create/restore`、`config edit`、`doctor --fix/--live` | 会改状态或真发请求 → 未运行 |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用（连续 18 轮如此） |

---

## 下一轮建议

按优先级：

1. **【P0】把 `test_hint_covers_every_stop_reason` 的硬编码 `reasons` 换成"策略声明 + 并集"**（A④ 末）：让 `DefaultBudgetPolicy` 声明 `STOP_REASONS`、租约机制声明 `session_takeover`，测试遍历并集 → **新增 stop reason 会自动要求写提示**。**现在这条是清单断言，会过期；第一条（扫源码查命令）是性质断言，不会。** 把第二条也升级成性质。
2. **【P1】`Settings` 裸属性全扫（你连排两轮的 P0 ③）**：`model_copy(update=...)` 那个口子还在，**可能还有第二个 `reasoning_level`**。**建议做法**：遍历 `Settings.__dict__` 的实例属性，凡是**不在 `model_fields` 里**的都报出来。**这是唯一能一次扫干净的办法。**
3. **【P1】5.2 懒加载（你下轮计划）**：`ensure_loaded` 零调用 → 直接影响每轮 schema 体积，**5.11 的 4,000 token 预算挂在这条上**。
4. **【P1】把"节流粒度"那条论证写进方法论**（R3）：**节流粒度必须与被检查量的计量单位一致**。这条能直接指导 8.6（心跳）、5.3（探针 TTL）、11.x（cron tick）等一批地方。
5. **【P1】把"提示文案是唯一一类静态检查看不见的缺陷"写进方法论**：`_stop_hint` 这类东西 lint/mypy/单测全看不见，**只有"解析文案 → 对注册表核"这种语义级守卫能抓**。**可推广的候选**：①`--help` 里提到的命令/文件路径 ②错误消息里的 `orbit xxx` ③文档字符串里的示例命令。
6. **【P2】`reasoning_level` 三判据抽成 `assert_real_settings_field`**：与第 3 条一起做，**扫出来的每个裸属性都顺手加一条断言**。
7. **【P2】`4.12` 照 `4.2` 的 `BEDROCK_LIMITATIONS` 逐家写**：现在"19 家 4 家可用"读者不知道另外 15 家是"没做"还是"缺条件"。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 21 轮未核。`nodes.py:254 context_meta.setdefault("compressed", {})` 仍是空占位。

---

*本报告由 monitor 角色只读生成。**你点名要我核的那条：`orbit plane leases` 不存在** —— `plane` 的 9 个子命令里没有 `leases`，实跑 `--help` 直接 argparse 报错。**你在同一轮内修好了，而且修法比我建议的更好**：我建议换成 `orbit sessions leases`，你改成指向 `runtime/active_sessions.json`（租约账本本身）+ `orbit plane show <会话>`（该会话的平面视图）—— **"给数据"比"给命令"更接近用户真正想知道的**，两者我都实跑确认存在，旧串全仓零残留。**并且做成了机制**：`test_hint_commands_exist.py` 把 `_stop_hint` 里的每个 `orbit <cmd>` 解析出来逐级查注册表（**子命令级也查**，只查顶层正好会漏掉这一类），我读了源码——**把旧串写回去必红**，还带两条防空转守卫。**这条的价值不在"修得快"，而在它被做成了一类错的守卫**：`_stop_hint` 是唯一一类"代码正确、类型自洽、lint 干净，但用户照做会失败"的东西，**静态检查原理上看不见它**。三项点名全部撑得住：`test_takeover` 断言的是 `reason == "session_takeover"` 而不是"任务停了"（否则 `max_actions` 撞上也会绿）；节流 10s vs 90s = 9 倍余量合理，且你 docstring 里「每轮查会在快循环里把盘写爆、慢循环里反而查得太稀」比"10 秒足够"硬一个量级——**节流粒度必须与被检查量的计量单位一致**；`release` 真在 `finally` 内且 `acquire` 也接上了。你选 10.8 而非 5.2 我认同，并补两条更可判的理由：**数据损坏不可逆、且没有信号**。我上轮那三条建议你逐字落地了（顶层对齐 77/77 进统计出口 + 口径写在数字旁）。**唯一剩下的口子**：`test_hint_covers_every_stop_reason` 的 `reasons` 是硬编码清单，新增 stop reason 时不会红——建议改成策略声明 + 并集。未验证项单列，不混进完成项。*
