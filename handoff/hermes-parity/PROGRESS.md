# 对标进度报告

**更新时间**：2026-10-28 22:46（本机实测时钟 `Get-Date`，只读取证）
**本轮轮次**：**N = 32**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP / GAPS 文件；未装依赖；未启动后端；未跑会改状态或真发请求的命令（边界动作：跑了 1 个测试文件 + 一次全量；读了 `agent.py`/`nodes.py` 的路由链）。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `cc59e9f`；**轮初 `git status` 0 条目，轮末 0 条目**）

---

## 总体完成度

### **166 / 181 项 = 91.7% 严格 · 93.8% 可验证**（✅ 161 + 🟢 5）

| 项 | 说明 |
|---|---|
| 五态 | **✅ 161 · 🟢 5 · ⚫ 4 · 🟡 11 · ⬜ 0**（合计 181 ✓） |
| 严格 / 可验证 | **91.7%** / **93.8%** —— 从 91.2% / 93.2% 上升（`6.4` 转 ✅） |
| **`matrix_stats --check`** | **exit 0**，零告警 ✅ |
| 顶层命令对齐 | **77/77（Orbit 顶层 84，自有 7）** + 口径说明 ✅ |

### 硬口径：**全部一致**

| 你的口径 | 我的实测 | 判定 |
|---|---|---|
| ruff `All checks passed!` | **All checks passed!**（exit 0） | ✅ 一致 |
| mypy **173** files clean | **173 files clean**（exit 0） | ✅ 完全一致 |
| pytest 1 failed, **3577** passed, 3 skipped | **1 failed / 3577 passed / 3 skipped（390.65s）** | ✅ **完全一致** |
| `matrix_stats --check` exit 0 | **exit 0**，181/161/5/4/11/0 | ✅ 一致 |
| `git status` 0 项 | **轮初轮末均 0 项** | ✅ 一致 |

---

## 分层完成度

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | 15 ✅ + 1 🟢 / 18 | 88.9% | 🟡2 |
| L2 Agent 核心循环 | 9 / 10 | 90.0% | 🟡1 |
| **L3 上下文装配** | **8 / 8** | **100%** | ✅ |
| L4 模型适配 | 9 ✅ + 2 ⚫ / 13 | 84.6% | 🟡2 |
| **L5 工具层** | **19 / 19** | **100%** | 🎉（**6.4 转 ✅**） |
| **L6 工具集 / 注册** | **5 / 5** | **100%** | ✅ |
| L7 Skill / curator | **17 ✅ + 1 🟢** / 18 | 100% | ✅ |
| L8 MCP | 9 ✅ + 1 ⚫ / 10 | 90.0% | — |
| **L9 记忆层** | **16 / 16** | **100%** | ✅ |
| L10 持久化 / 会话 | **14 ✅ + 2 🟢** / 15 | 100% | ✅ |
| **L11 cron** | **6 / 6** | **100%** | ✅ |
| L12 Gateway / 多平台 | 8 ✅ + 1 ⚫ / 11 | 72.7% | 🟡2 |
| L13 插件 | **8 / 10** | 80.0% | 🟡2 |
| L14 安全 / 审批 | 8 ✅ + 1 🟢 / 10 | 90.0% | 🟡1 |
| L15 可观测性 | 5 / 6 | 83.3% | 🟡1 |
| **L16 Web UI / 前端** | **6 / 6** | **100%** | ✅ |
| **合计** | **166 / 181** | **91.7%** | — |

**十层 100%**：L3、**L5（本轮补齐）**、L6、L7、L9、L10、L11、L16。

---

## 本轮抽查证据

**抽样率**：3 项点名**全部复核**（含 1 项**查出真 bug**）。

### A. ★ 最重要：`_enqueue_after` 不传 `platform` → 延后入队的任务**平台被降级成 `cli`**

**你的设计对了一半，但漏了一条路径。**

**你写对的部分**（我核了，设计是对的）：
```python
async def enqueue(self, task_id, resume=None, *, platform: str = "cli"):
    # 记下来源平台（MATRIX 6.4）。**放在最前面**：下面有两条早退分支
    # （任务在跑时延后入队、已排队时忽略），平台信息不该因为走了早退就丢——
    # 延后入队那条尤其重要，它会在原任务收尾后**重新**入队。
    self._task_platform[task_id] = str(platform or "cli")     ← 最前面 ✓
    job = self.current.get(task_id)
    if job is not None and not job.done():
        deferred = asyncio.create_task(self._enqueue_after(job, task_id, resume))
        ...
```
✓ **"放在最前面"这个位置选对了**，注释也把理由写清了 ✓

**但漏的部分在这里**：
```python
async def _enqueue_after(self, job, task_id, resume):
    """等当前 job 收尾后再入队。"""
    await asyncio.gather(job, return_exceptions=True)
    await self.enqueue(task_id, resume)          ← **没传 platform！**
```

**完整时序（三步，两步都在丢信息）**：
| 步 | 发生什么 | `_task_platform[task_id]` |
|---|---|---|
| 1 | `enqueue(..., platform="api")` 最前面记录 | **`"api"`** ✓ |
| 2 | job 跑完 → 它的 `finally` 里 `self._task_platform.pop(task.id, None)`（`agent.py:687`） | **被删掉** |
| 3 | `_enqueue_after` → `enqueue(task_id, resume)` **不传 platform** → 默认 `"cli"` | **`"cli"`** ❌ |

→ **一个从 API 入队的任务，只要走了延后入队，恢复后就变成 `cli` 任务**，**工具集会跟着换掉**。

**为什么这是真问题（不是理论问题）**：
**延后入队正是 `WAITING_USER` resume 的主路径** —— 你自己在 docstring 里写了这个场景：
> 「调用方看到"已等待用户"就立刻 `enqueue(..., resume)`，此时 `task_id in self.current` 仍为真」

→ **用户在 API 上回答澄清 → resume 走延后路径 → 平台变 `cli` → 后续工具集从 api 的换成 cli 的。** **这正好发生在"任务中途"**，而不是任务开始。

**测试没覆盖**：`test_enqueue_records_the_platform`（`:87`）只测**直接入队**路径（`await runner.enqueue(task.id, platform="api")` 然后立刻断言），**没有构造"正在跑 → 延后 → 恢复"的时序** ✓

**修法（推荐：defer 时捕获并传下去）**：
```python
if job is not None and not job.done():
    deferred = asyncio.create_task(
        self._enqueue_after(job, task_id, resume, platform)      # ← 捕获
    )
...
async def _enqueue_after(self, job, task_id, resume, platform):
    await asyncio.gather(job, return_exceptions=True)
    await self.enqueue(task_id, resume, platform=platform)        # ← 传回
```
**不要**改成"在 `_enqueue_after` 里读 `_task_platform`" —— 因为第 2 步已经 pop 了，读不到。**必须在 defer 那一刻捕获。**

**这条的教训值得记**：**"放在最前面"解决了"早退时丢信息"，但没解决"重新入队时用什么值"**。**两个问题看起来是同一个，实际是两步。**

### B. 你点名的 3 项

**① `_task_platform` 的清理时机 → 你的担心成立，但**丢的原因不是 pop，是 `_enqueue_after` 没传参**

你说「我判断不会（重新 enqueue 会再记一次）」—— **"会再记一次"是对的，但记的是默认值 `cli`，不是原来的 `api`**。
所以：**pop 本身没错**（它防的是内存泄漏），**错在 re-enqueue 那一行没有携带原值** ✓ 详见 A 节。

**② `platform_for` 的 `getattr` 兜底算不算「静默降级四兄弟」→ 形式上同族，性质不同，但它确实掩盖一种真问题**

```python
platform = "cli"
platform_of = getattr(runner, "platform_for", None)
if callable(platform_of):
    platform = platform_of(task.id)
```

**我的判断分两层**：

**不是同族的那一面**（所以你不必把它当病改掉）：
| | 2.2 / `reasoning_level` 的 fallback | 这里的 fallback |
|---|---|---|
| 兜底值 | `None` / `90`（**看起来像"字段有这个值"**） | **`"cli"`，是一个合法的真值** |
| 掩盖了什么 | **"字段不存在"被伪装成"字段用了默认"** | 假件/旧 runner 没有这个方法 |
| Hermes 侧 | 无对应 | **`--platform` 默认就是 `cli`**（`tools list --platform PLATFORM`，调研 01:1374）✓ |

→ **判据**：**兜底值是否是一个"合法的真值"**。`"cli"` 是合法默认（Hermes 也默认 cli）→ **不是掩盖缺字段** ✓

**是同族的那一面（你的担心成立）**：
**它也会掩盖「`AgentRunner` 上这个方法消失了」** —— 如果有人重构掉 `platform_for`，**所有任务静默退回 `cli`，6.4 退回"从未生效"，而全量测试仍然全绿**（因为假件本来就没有这个方法，`getattr` 兜底让它照常过）。

**建议（很便宜）**：加一条**对真实类**的反证断言，与假件的宽松并存：
```python
def test_the_real_runner_really_has_platform_for():
    """假件可以没有，但**真类必须有**——否则 getattr 兜底会让它静默退回 cli。"""
    from app.agent import AgentRunner
    assert callable(getattr(AgentRunner, "platform_for", None)), \
        "AgentRunner 丢了 platform_for——所有任务会静默退回 cli，6.4 变成从未生效"
```
→ **这一条同时满足两件事**：假件保持宽松（不用改 9 个假 registry），**真类的方法存在性被钉住** ✓

**③ 6.4 判 ✅ 是否过早 / `api` 这个平台名是否自造 → ✅ 不撤回，但 `api` 要标"Orbit 自造"**

**`cli` 是 Hermes 的真名** ✓：
```
调研 01-cli-surface.md:1374
  | `list` | 显示所有工具及其启用状态 | `hermes tools list [-h] [--platform PLATFORM]`（**`--platform` 默认 `cli`**）|
```
→ **`cli` 与 Hermes 完全一致，且默认值也一致** ✓✓

**`api` 在 Hermes 侧查不到对应**：
- Hermes 的 `--platform` 语义是**网关渠道**（telegram/slack/discord/… + `cli`）
- **Hermes 没有"HTTP API 作为一个 platform"这个概念**（它的 API 不是渠道）
→ **所以 `api` 是 Orbit 自造的** ✓ **你的怀疑对**

**但我不建议因此撤回 6.4 的 ✅**，理由：
1. **机制是真的、测试是真的**（`cli` 与 `api` 暴露的工具集真的不同）—— 这一行承诺的是"工具暴露面按任务来源过滤"，**不是"平台名与 Hermes 逐一对应"**
2. **Orbit 有 Hermes 没有的 HTTP API**，**需要一个名字**，`api` 是最直白的
3. **且它落在 `platforms.<name>.disabled/enabled` 这个 Hermes 命名空间里** —— 这是唯一值得留意的地方（见 R2）

**建议**：**证据栏加一句**：「`cli` 沿用 Hermes（`tools list --platform` 默认 cli）；**`api` 是 Orbit 自造**——Hermes 的 platform 是网关渠道，没有"HTTP API 作为渠道"的概念。Orbit 的 HTTP API 是 Hermes 没有的宿主，故需新名。」

### C. 你那个假件回归的判断我完全同意

> `nodes.py` 开始传 platform 后，`test_compaction.py` 的假 registry `specs()` 不收参数 → 9 条失败。**修的是假件不是产品。**

**判据（第 21 轮定过，这轮再次成立）**：**真实签名是否本来就允许这个参数？** 允许 → 假件过时；不允许 → 产品越界。
`ToolRegistry.specs(platform=...)` 一直支持 platform ✓ → **假件过时，改假件正确** ✓

### D. 分类脚本的修复我核了，改得对

```
① 全仓无实现（几天量级）        54   ← AST 扫 app/ 的 Tool(...) 定义
② 有机制缺 Tool 包装（几小时）    2   ← 手工核实（DelegateRunner / swarm）
③ 刻意不做                      1   ← 手工核实（browser_cdp 协议逃生口）
④ 有定义但未进池（缺装配）        0
⑤ 需外部条件                    本脚本不判
```
✓ **四类 + 每类标明判据来源** —— **"②/③ 不可能从源码机械推出，所以用手工核实清单并标明来源；机械判据只负责 ①"** 这个划分**是正确处理**：**让机械判据只做它擅长的事，剩下的显式标注为人判** ✓
**而且两档工作量（几天 / 几小时）真的写进了 GAPS.md** ✓ —— 这正是我上轮说的"撑起工作量估算的粒度"。

**你自己发现 `agent_swarm` 也没有 `Tool` 定义** —— 这条我核了，**对** ✓（我上轮独立扫过，`agent_swarm` → 【无任何 Tool 定义】）

---

## 与目标标志的差距

**⬜ 0**，离目标还差 **11 条 🟡 + 4 条 ⚫**：

```
🟡 11：1.7 1.13b 4.8 4.9 4.12 5.14
       12.5 12.10 13.5 14.8 15.4
⚫ 4 ：4.2 4.7 8.3 12.9
```

**GAPS.md 的「该 ✅」还剩 3 条**：`15.4`（下轮）/ `4.9` / `13.5`（已拆两步，排最后）

---

## 风险与漂移

### R1 🟠 **`_enqueue_after` 丢失 platform（A 节）—— 本轮唯一实质缺陷**

延后入队（`WAITING_USER` resume 的主路径）会让 `api` 任务恢复成 `cli` 任务，**工具集随之换掉**。
**修法**：defer 时捕获 `platform` 并传回 `enqueue`。**测试需补"正在跑 → 延后 → 恢复后平台不变"的时序用例。**

### R2 🟡 **`api` 这个平台名是 Orbit 自造的，落在 Hermes 的 `platforms.<name>` 命名空间里**

`cli` 与 Hermes 一致 ✓；`api` 无对应。**风险**：Hermes 的 `platforms.<name>.disabled/enabled` 是**网关渠道**配置，混入一个"宿主"名字会让两套词汇表重叠。
**建议**：证据栏标明 `api` 为自造 + 理由；**若将来 Hermes 真加了 `api` 渠道，这个名字要重新考虑。**

### R3 🟡 **`getattr(runner, "platform_for", None)` 掩盖"真类丢方法"（B②）**

形式同族、性质不同（兜底值是合法真值），**但它会让 `AgentRunner` 丢掉 `platform_for` 时静默退回 `cli` 而测试全绿**。
**建议**：加一条对**真实类**的 `callable(...)` 反证断言，假件保持宽松。

### R4 🟢 **本轮两处判断都做对了**

1. **分类脚本的"机械判据只做它擅长的 ①，②③ 手工核实并标明来源"** —— 这是把"能机械判的"和"只能人判的"分开，**比强行机械化更诚实**
2. **假件回归改假件** —— 判据（真实签名本来允许）成立

### R5 🟡 **未验证项（如实标注）**

| 项 | 为什么没验 |
|---|---|
| A 节的 bug **实跑复现** | 需要构造"任务在跑 → 延后入队 → 恢复"的时序 → 会改状态，**未做**；**结论来自代码时序读解**（`_enqueue_after` 的 `enqueue` 调用无 platform 参数 + 默认值 `cli`），**证据是静态的** |
| `--event-log` / `orbit doctor` 的端到端 | 未实跑 doctor |
| `15.4` / `4.9` / `13.5` 的实现 | 未做 |
| `12.10` 的 `enroll`、`4.12` 的 19 家逐家 | 要网络/凭证 → 未跑 |
| `orbit chat` 交互、`gateway start`、`cron run`、`backup create/restore`、`config edit`、`doctor --fix/--live` | 会改状态或真发请求 → 未运行 |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用（连续 27 轮如此） |

---

## 下一轮建议

按优先级：

1. **【P0】修 `_enqueue_after` 丢 platform**（R1）：defer 时捕获并传回。**并补一条时序测试**：「任务在跑 → `enqueue(platform="api")` → 原任务结束 → 恢复后 `platform_for` 仍是 `api`」。**这条测试现在不存在，而它正好覆盖 `WAITING_USER` resume 的主路径。**
2. **【P0】加「真实类必须有 `platform_for`」的反证断言**（R3）：一行，防"方法消失后静默退回 cli"。
3. **【P1】证据栏标 `api` 为 Orbit 自造 + 理由**（R2）：`cli` 与 Hermes 一致，`api` 无对应。
4. **【P1】按顺序做 `15.4`**（你的计划）：metrics schema 成为真契约。**注意它与 6.4 同型**（机制存在、无生产产出方）——**接线后必查"产出真的经过 validate"**，而不只是"validate 能校验"。
5. **【P1】把「defer 时捕获 vs re-enqueue 时读取」这条教训记进方法论**（A 节）：**"把信息放在最前面"解决"早退时丢信息"，但不解决"重新入队时用什么值"** —— **两个问题看起来是一个，实际是两步。**
6. **【P2】`4.9`（aux slots）接一个消费者**：与 6.4/15.4 同型（零消费者），**建议连着做**，三次同型的修法会强化同一个模式识别。
7. **【P2】`4.12` 照 `4.2` 的 `BEDROCK_LIMITATIONS` 逐家写**：GAPS.md 已给出分组，逐家写清 `install`/`requires`/`reason` 即完成压实。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 29 轮未核。`nodes.py:254 context_meta.setdefault("compressed", {})` 仍是空占位。

---

*本报告由 monitor 角色只读生成。**本轮查出一个真 bug：`_enqueue_after` 里 `await self.enqueue(task_id, resume)` 没传 `platform` → 默认 `"cli"` → 延后入队的任务平台被降级。** 完整时序三步：① `enqueue(platform="api")` 最前面记 `"api"` ✓ ② job 收尾的 `finally` 里 `pop` 删掉（`agent.py:687`）③ `_enqueue_after` 重新入队**不传参** → 写成 `"cli"` ❌。**而延后入队正是 `WAITING_USER` resume 的主路径**（你 docstring 自己写的场景）→ **用户在 API 上回答澄清 → 恢复后平台变 cli → 工具集跟着换掉**。`test_enqueue_records_the_platform` 只测直接路径，**没覆盖这个时序**。**修法：defer 时捕获 platform 并传回**（不能改成"在 `_enqueue_after` 里读 `_task_platform`"，因为第 ② 步已经 pop 了）。**教训：「把信息放在最前面」解决"早退时丢信息"，但不解决"重新入队时用什么值"——两个问题看起来是一个，实际是两步。** 另两项：`platform_for` 的 `getattr` 兜底**形式同族、性质不同**（兜底值 `"cli"` 是合法真值，Hermes 的 `--platform` 也默认 cli），**但它确实掩盖"`AgentRunner` 丢了这个方法"** → 建议加一条对真实类的 `callable(...)` 反证断言；**6.4 的 ✅ 我不撤回**，但 **`api` 是 Orbit 自造**（Hermes 的 platform 是网关渠道，无"HTTP API 作为渠道"概念），`cli` 则与 Hermes 完全一致（调研 01:1374 `tools list --platform` 默认 cli）✓。分类脚本的四类改法我核了，**"机械判据只做 ①、②③ 手工核实并标明来源"是正确处理**。未验证项单列，不混进完成项。*
