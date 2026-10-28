# 对标进度报告

**更新时间**：2026-10-28 19:50（本机实测时钟 `Get-Date`，只读取证）
**本轮轮次**：**N = 22**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑会改状态或真发请求的命令（边界动作：跑了 3 个测试文件 + 一次全量）。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `f7413b6`；**轮初 `git status` 0 条目，轮末 0 条目**）

---

## 总体完成度

### **160 / 181 项 = 88.4% 严格 · 90.4% 可验证**（✅ 155 + 🟢 5）

| 项 | 说明 |
|---|---|
| 五态 | **✅ 155 · 🟢 5 · ⚫ 4 · 🟡 17 · ⬜ 0**（合计 181 ✓） |
| 严格 / 可验证 | **88.4%** / **90.4%** —— **与上轮完全相同**（13.5 推到 16/17 仍差 1 个，所以还是 🟡） |
| 与 `matrix_stats --check` | 逐字一致，exit 0 ✅ |
| 结构 / 状态列 | 181 行全合法，零异常 ✅ |

**"数字没动"这一轮是好事**：13.5 从 8/17 推到 16/17，**做了 8 个发射点却没有换来一行 ✅** —— 这是**状态机的诚实**：它只在"整条对齐"时翻绿，不因为"推进了一大半"给分。**如果它按进度给分，13.5 早就该绿了。**

### 硬口径：**全部一致，基线干净**

| 你的口径 | 我的实测 | 判定 |
|---|---|---|
| ruff `All checks passed!` | **All checks passed!**（exit 0） | ✅ 一致 |
| mypy **172** files clean | **172 files clean**（exit 0） | ✅ 完全一致 |
| pytest 1 failed, **3532** passed, 3 skipped | **1 failed / 3532 passed / 3 skipped（343.51s）** | ✅ **完全一致** |
| `matrix_stats --check` exit 0 | exit 0 | ✅ 一致 |
| `git status` 8 项，全部本轮改动，无在飞文件 | **我轮初轮末均 0 项** | ✅ 一致（改动已提交成 `f7413b6`） |

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
| L10 持久化 / 会话 | **12 ✅ + 2 🟢** / 15 | 93.3% | 🟡1 |
| **L11 cron** | **6 / 6** | **100%** | ✅ |
| L12 Gateway / 多平台 | 8 ✅ + 1 ⚫ / 11 | 72.7% | 🟡2 |
| L13 插件 | 7 / 10 | 70.0% | 🟡3 |
| L14 安全 / 审批 | 8 ✅ + 1 🟢 / 10 | 90.0% | 🟡1 |
| L15 可观测性 | 5 / 6 | 83.3% | 🟡1 |
| **L16 Web UI / 前端** | **6 / 6** | **100%** | ✅ |
| **合计** | **160 / 181** | **88.4%** | — |

---

## 本轮抽查证据

**抽样率**：3 项点名**全部复核**（3 项都实跑）；另核了「零告警」测试与 `EMITTED_EVENTS` 实际计数。

### A. 你点名的 3 项 —— **3/3 全部撑得住，其中 2 项的守卫设计连续第二轮升级**

**① `test_every_hermes_top_level_command_exists_in_orbit` 能不能红 → ✅✅ 能，而且它加了"防空转"守卫**

```python
effective = (orbit - set(_RENAMES)) | {h for h in _RENAMES.values() if h in hermes}
missing = sorted(hermes - effective)
assert not missing, (
    f"这些 Hermes 顶层命令在 Orbit 里没有：{missing}\n"
    "（如果只是改了名，请加进 `_RENAMES` 并说明对应关系）")
assert len(hermes) >= 70, f"Hermes 名字集合只读到 {len(hermes)} 个，转储文件可能不全"
```
**清空 `_RENAMES`** → `effective = orbit` → 那 5 个只以新名存在的 `internal-*` 进 `missing` → **必红** ✓✓
**`_RENAMES` 我核了，正是那 5 个**（`__compute-host` → `internal-compute-host` 等）✓，与你说的完全一致。

**`assert len(hermes) >= 70` 这条防空转守卫值得单独说**：
> 转储文件缺失时 `hermes` 会变得很小，`hermes - effective` 自然为空 → **测试会永远绿**。

**这与你上轮的 `assert calls`（regex 停止匹配就永远绿）是同一个模式——连续第二轮主动加防空转断言。** 这是本项目质量上最明显的一条上升线。

**还加了第二条我没想到的**：
```python
def test_the_orbit_only_list_stays_small_and_documented():
    """Orbit 自有顶层命令应当少而有序……
    这条不是判错，是**防漂移**：如果哪天自有命令突然涨到二十几个，
    说明有人在用「加新命令」绕开「对齐 Hermes」。"""
```
→ **它守的是"用加新命令把指标做好看"** —— 这是**指标可以被 gaming 的那条缝**，你主动堵上了 ✓✓

**实跑**：`2 passed in 1.53s` ✓

**② `api_request_error` 是否真的不吞取消 → ✅✅ 真的不吞，且顺序正确**

```python
# nodes.py:978-995
try:
    result = await self._stream_model_once(model, messages, task_id, identity)
except asyncio.CancelledError:
    # 取消不是错误：不发 `api_request_error`（否则观测端会把用户主动中断
    # 算成 provider 故障，那会污染错误率）。
    raise                                    ← 取消分支在前，直接抛
except Exception as exc:
    try:
        hooks.emit("api_request_error", {"task_id":…, "model":…, "error": f"{type(exc).__name__}: {exc}"})
    except Exception as hook_exc:
        log.warning("api_request_error 钩子出错（已忽略）：%s", hook_exc)
    raise                                    ← 发完照常抛
```
✓✓ **`except asyncio.CancelledError: raise` 在 emit 之前**，且 `except Exception` 分支**发完照常 raise**（钩子是旁路，不改变控制流）。

**补一个技术事实（不影响你的结论，但值得知道）**：Python 3.8+ 的 `CancelledError` 继承 `BaseException` 而非 `Exception`，所以**即使不写这个显式分支，它也不会被 `except Exception` 捕获**。**但那个显式分支的价值不在"拦得住"，而在"把意图写下来"** —— 没有它，下一个人可能把 `except Exception` 改成 `except BaseException`，那时取消就会被算成错误。**所以这条留着是对的，而且注释写清了理由。**

**③ `stream_model` 包装层是否真的被调用 → ✅✅ `_stream_model_once` 零外部调用点**

```
nodes.py:749   result = await self.stream_model(...)              ← 外部唯一入口（在 ask_model 里）
nodes.py:954   async def stream_model(...)                        ← 包装层
nodes.py:979       result = await self._stream_model_once(...)    ← 唯一调用点（包装层内部）
nodes.py:1006  async def _stream_model_once(...)                  ← 被包装者
```
→ **`_stream_model_once` 没有任何外部调用点** ✓✓ —— 所以**所有模型流式请求都经过包装层**，per-request 事件真的每次发。

**而且你拆这两层的理由是我这轮最认可的一处设计判断**：
> 「`pre_llm_call` 包的是**整次模型调用**（含重试与压缩重来），而这一层包的是**一次真正的 HTTP 请求**——`ask_model` 的 while 循环每转一圈就是一次请求。观测端做 tracing 要的是后者：**重试三次就有三个 span，用 `llm_call` 只能看到一个**。」

**这是"事件粒度必须与观测语义匹配"的问题**，而且**只有真想过 tracing 才会发现** —— 如果按 `llm_call` 粒度发 API 事件，重试的 3 次会被压成 1 个 span，**观测端看到的延迟与错误率全是错的**。

### B. 额外核到的：「零告警」测试是本轮最好的设计

```python
def test_starting_the_app_emits_zero_missing_emitter_warnings(tmp_path):
    """**起一次应用，不能有任何「没有发射点」的告警。**"""
    ...
    warns = [r for r in records if "没有发射点" in r]
    assert warns == [], (
        "内置 exporter 订阅了没有发射点的事件（这些订阅永远不会触发）：\n  " ...)
```
**为什么它比"逐个数事件名"强**：它断言的是**一个性质**（"零告警"），而不是**一份枚举**（"这 17 个都有发射点"）。
→ **将来任何 exporter 加一条订阅、而那个事件没有发射点 → 这条自动红** ✓✓ **不需要人去同步清单。**

**这是本项目第一次用"性质断言"替代"清单断言"** —— 而清单断言的典型问题正是我们踩过的：**清单会过期，性质不会。**

另加 `test_api_request_events_fire_per_request`（`:155` `assert names.count("pre_api_request") == names.count("post_api_request")` —— **配对断言**，防"只发前不发后"）+ `:153 assert "api_request_error" not in names`（成功路径不该有错误事件）✓ 实跑 `5 passed` ✓

---

## 与目标标志的差距

**⬜ 0**，离目标还差 **17 条 🟡 + 4 条 ⚫**：

```
🟡 17：1.7 1.13b 4.3 4.8 4.9 4.12 5.2 5.14 6.4 10.8 10.14
       12.5 12.10 13.4 13.5 14.8 15.4
⚫ 4 ：4.2 4.7 8.3 12.9
```

### 你问的 ①：`on_session_reset` 判定为"功能缺口不是钩子缺口" —— **完全认同，而且这是本轮最有价值的一个判断**

**三条理由**：

1. **"编一个发射点会把 `delete` 谎报成 `reset`"** —— 这是**语义错配**，比"没有事件"更糟。订阅方收到 `on_session_reset` 会做重置相关的事（清 trace、重置计数器），而实际发生的是删除 → **观测端状态与真实状态不一致，而且不会报错**。**这正是"静默失效"最坏的一种：它看起来在工作。**

2. **这是同一条原则的第三次应用**：
   - 13.5 早期：`register_hook` 静默接受无发射点事件 → 要求 fail loud
   - 上轮：`test_rejected_delegation_emits_neither` → 不为凑事件数而虚报
   - 本轮：**不为凑 17/17 而编发射点** ✓
   **三次守的是同一件事：数字不能凭空产生。**

3. **它把一个"钩子缺口"正确地降级成"功能缺口"** —— 这是**更准的归类**，而且**决定了修法**：
   | 归类 | 根因 | 修法 |
   |---|---|---|
   | 钩子缺口 | 忘了发事件 | 加一行 `emit` |
   | **功能缺口（正确）** | **Orbit 没有"会话重置"这个动作** | **先加功能（`reset` 语义），再顺带 emit** |

**建议一条**：13.5 的证据栏把 `on_session_reset` **单独标成「不是钩子缺口，是功能缺口：Orbit 无"会话重置"动作（`sessions` 有 `delete`/`archive`，但没有 `/new`/`/reset` 语义）」**。
**为什么**：不这么写，下一个人看到"16/17，差 1 个"会以为**只差一行 emit**，于是去加一行假的 —— **那正是你刚刚避免的事，会被人补回来。**

### 你问的 ②：77/77 该不该在 MATRIX 里单独占一行 —— **同意不加行，但理由要更准，且有个前提**

**同意不加行**，理由比"它是聚合事实"更硬一条：
> **它会引入一个新分母（77 或 84），与 181 并列时会让人搞不清哪个是主口径。** 这与 **1.17 那个元指标行被移出**是同一类问题 —— 我们上上轮刚为"分母混用"付过代价（88.4% vs 88.9% 那次）。

**但"只放 README/ROADMAP"不够，我建议放 `matrix_stats` 的输出**：
- 因为它**现在有测试守着**（`test_every_hermes_top_level_command_exists_in_orbit`）
- **一个有测试守的判据，必须有固定的展示位置** —— 否则测试红了，没人知道该去更新哪个数字
- 放 `matrix_stats` 输出里（像"可验证完成度"那样多打一行「顶层命令对齐 77/77」）**既不占 181 行、又和测试同一个出口**

**一个必须满足的前提**：**口径要写在数字旁边**，一句就够：
> 「**顶层命令名集合差**（含 5 个已知改名；**不含子命令级**）。**这只证明没有整条命令缺失，不证明功能对齐。**」

**这句话你已经在测试 docstring 里写了** ✓ —— **把它搬到数字旁边就行。** 因为**不写口径的 77/77 一定会被读成"功能全对齐了"**，而那是这份报告里最容易被误读的数字。

---

## 风险与漂移

### R1 🟢 **本轮最有价值的三件事，都是"守住了不该虚报的地方"**

1. **16/17 仍然 🟡** —— 做了 8 个发射点却没换来一行 ✅，**状态机没有按进度给分**
2. **`on_session_reset` 拒绝编发射点** —— 宁可留 1 个 🟡，也不让 `delete` 冒充 `reset`
3. **`_RENAMES` 扣除后从 12 降到 7** —— 你第一版虚高了一倍，自己查出来并写进 docstring：「**名字差 ≠ 能力差**」

**这三件事的共同点：每一次都是"本来可以更好看，但选择了更准"。**

### R2 🟡 **`on_session_reset` 的归类必须写进证据栏（见上）**

否则下一个人会把"功能缺口"当"钩子缺口"补掉 —— **而补法就是加一行假 emit。**

### R3 🟡 **77/77 需要一个固定的展示位置 + 口径**

建议 `matrix_stats` 输出加一行，并在数字旁写"不含子命令级、不证明功能对齐"。

### R4 🟡 **轮末 `git status` 0 条目（两头都干净）**

```
轮初 git status: 0 条目 ✓
轮末 git status: 0 条目 ✓
```
连续第二轮两头干净 → 所有计数类读数可信（pytest 3532 一次对上）。

### R5 🟡 **未验证项（如实标注）**

| 项 | 为什么没验 |
|---|---|
| `test_every_hermes_top_level_command_exists_in_orbit` 的**红路径实跑** | 要清空 `_RENAMES` → 未做；验证的是"集合差公式 + 5 个映射"结构必红 |
| `on_session_finalize` 与 `on_session_end` **分开**的实际观测差异 | 只核了发射点位置（`finalize` 节点 vs `agent.py` finally），**没跑一次真实任务看两个事件的实际时序** |
| 审批前后事件的**三态**（allow/deny/timeout） | 只核了 MATRIX 描述与代码位置，**未实跑一次审批看 timeout 真的原样上报** |
| `12.10` 的 `enroll`、`4.12` 的 19 家逐家 | 要网络/凭证 → 未跑 |
| `orbit chat` 交互、`gateway start`、`cron run`、`backup create/restore`、`config edit`、`doctor --fix/--live` | 会改状态或真发请求 → 未运行 |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用（连续 17 轮如此） |

---

## 下一轮建议

按优先级：

1. **【P0】把 `on_session_reset` 的归类写进 13.5 证据栏**（R2）：**一句话，防的是"下一个人把功能缺口当钩子缺口补一行假 emit"。**
2. **【P0】`matrix_stats` 输出加一行「顶层命令对齐 77/77」+ 口径说明**（R3）：有测试守的判据必须有固定出口，**且口径必须写在数字旁**。
3. **【P1】`reasoning_level` 三判据抽成 `assert_real_settings_field`（你已排下轮）**：建议顺带**扫一遍 `Settings` 有没有别的"裸属性"** —— `model_copy(update=...)` 那个口子还在，**可能还有第二个 `reasoning_level`**。这比只修当前字段更有价值。
4. **【P1】5.2（懒加载）优先于 10.8**：`ensure_loaded` 零调用是"机制在但没人用"，而它直接影响**每轮请求的工具 schema 体积**（5.11 的 4,000 token 预算也挂在这条上）。**10.8 的 `LeaseStore.acquire` 影响面小得多。**
5. **【P1】「零告警」这种"性质断言"值得推广**（B 节）：它是本项目第一次用性质替代清单。**可推广的候选**：①"所有 ✅ 行都有 `实现：`/`差异：`"（已有门禁）②"所有 🟡 行的证据栏都写了缺什么" ③"所有 CLI 子命令的 `--help` 都能跑通"。**清单会过期，性质不会。**
6. **【P2】把"防空转守卫"立成规范**：你已经连续两轮加了（`assert calls` / `assert len(hermes) >= 70`）。**建议写成一句规则**：**任何"扫描源码/文件集合"的测试，都必须有一条"扫到的东西不为空/不少于 N"的断言。**
7. **【P2】`4.12` 照 `4.2` 的 `BEDROCK_LIMITATIONS` 逐家写**：现在"19 家 4 家可用"读者不知道另外 15 家是"没做"还是"缺条件"。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 20 轮未核。`nodes.py:254 context_meta.setdefault("compressed", {})` 仍是空占位。

---

*本报告由 monitor 角色只读生成。**基线连续第二轮完全对上**（pytest **3532** 一致，轮初轮末 `git status` 均 0 项）。三项点名全部撑得住：`test_every_hermes_top_level_command_exists_in_orbit` **能红且加了 `assert len(hermes) >= 70` 防空转守卫**（转储缺失时测试会永远绿——这与你上轮 `assert calls` 是同一个模式，**连续第二轮主动加**），另加 `test_the_orbit_only_list_stays_small_and_documented` 守住"用加新命令把指标做好看"这条缝；`api_request_error` **`except CancelledError: raise` 确实在 emit 之前**（补一个技术事实：3.8+ 的 `CancelledError` 继承 `BaseException`，所以那个显式分支**价值不在"拦得住"而在"把意图写下来"**）；`_stream_model_once` **零外部调用点**，所有流式请求都过包装层。你那句"重试三次就有三个 span，用 `llm_call` 只能看到一个"是**只有真想过 tracing 才会发现**的粒度问题。**两个判断我都认同**：`on_session_reset` 是**功能缺口不是钩子缺口**（编发射点会让 `delete` 冒充 `reset`，是"静默失效"最坏的一种），但**请把归类写进证据栏**，否则下一个人会去补一行假 emit；`77/77` **不占 181 行**（会引入新分母，与 1.17 同一类问题），但**要放 `matrix_stats` 输出并写明口径**。未验证项单列，不混进完成项。*
