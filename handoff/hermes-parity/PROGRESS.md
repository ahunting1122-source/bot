# 对标进度报告

**更新时间**：2026-10-28 19:14（本机实测时钟 `Get-Date`，只读取证）
**本轮轮次**：**N = 20**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑会改状态或真发请求的命令（边界动作：跑了 3 个测试文件 + 两次全量）。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `7d42875`；**轮初工作树干净，轮末 3 项在飞**）

---

## 总体完成度

### **159 / 181 项 = 87.8% 严格 · 89.8% 可验证**（✅ 154 + 🟢 5）

| 项 | 说明 |
|---|---|
| 五态 | **✅ 154 · 🟢 5 · ⚫ 4 · 🟡 18 · ⬜ 0**（合计 181 ✓） |
| 严格 / 可验证 | **87.8%** / **89.8%** —— 从 86.7% / 88.7% 上升 |
| 与 `matrix_stats --check` | 逐字一致，exit 0 ✅ |
| 结构 / 状态列 | 181 行全合法，零异常 ✅ |

### 硬口径

| 你的口径 | 我的实测 | 判定 |
|---|---|---|
| ruff `All checks passed!` | **All checks passed!**（exit 0） | ✅ 一致 |
| mypy **172** files（新增 `clarify_delivery.py`） | **172 files clean**（exit 0） | ✅ 完全一致 |
| pytest 1 failed, **3517** passed, 3 skipped | **1 failed / 3521 passed / 3 skipped（347.79s）** | ⚠️ **+4，来源已查明（见下）** |
| `matrix_stats --check` exit 0 | exit 0 | ✅ 一致 |
| REGISTRY 550，done 384 / partial 24 / todo 142 | **550；384 / 24 / 142**（连跑两次一致） | ✅ 完全一致 |

**`+4` 的来源（不是回归，是你的基线取数时点）**：
`tests/test_subagent_hooks.py` —— **你跑基线时它正在被写**。证据链完整：
1. 我第一遍全量 → `ERROR tests/test_subagent_hooks.py` / `Interrupted: 1 error during collection`
2. 单独跑它 → **`4 passed`**
3. 轮末 `git status` → `?? backend/tests/test_subagent_hooks.py`（**未跟踪**，说明是新文件）
4. 重跑全量 → **3521 passed**（= 3517 + 4）

→ **`+4` 就是那 4 条 subagent hook 测试**。**这是本项目第三次被"在飞文件"改读数**（前两次是 mypy 文件数 143→145、ruff 0→30 errors）。**规律已经很清楚：`git status` 非空时，任何"计数类"读数都不可信。**

---

## 分层完成度

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | 15 ✅ + 1 🟢 / 18 | 88.9% | 🟡2 |
| L2 Agent 核心循环 | 9 / 10 | 90.0% | 🟡1 |
| **L3 上下文装配** | **8 / 8** | **100%** | ✅ |
| L4 模型适配 | 7 ✅ + 2 ⚫ / 13 | 69.2% | 🟡4 |
| L5 工具层 | **17 / 19** | 89.5% | 🟡2（**5.13 转 ✅**） |
| **L6 工具集 / 注册** | **5 / 5** | **100%** | ✅ |
| L7 Skill / curator | **17 ✅ + 1 🟢** / 18 | 100% | ✅ |
| L8 MCP | 9 ✅ + 1 ⚫ / 10 | 90.0% | — |
| **L9 记忆层** | **16 / 16** | **100%** | ✅ |
| L10 持久化 / 会话 | **12 ✅ + 2 🟢** / 15 | 93.3% | 🟡1（**10.7 转 ✅**） |
| **L11 cron** | **6 / 6** | **100%** | ✅ |
| L12 Gateway / 多平台 | 8 ✅ + 1 ⚫ / 11 | 72.7% | 🟡2 |
| L13 插件 | 7 / 10 | 70.0% | 🟡3 |
| L14 安全 / 审批 | 8 ✅ + 1 🟢 / 10 | 90.0% | 🟡1 |
| L15 可观测性 | 5 / 6 | 83.3% | 🟡1 |
| **L16 Web UI / 前端** | **6 / 6** | **100%** | ✅ |
| **合计** | **159 / 181** | **87.8%** | — |

---

## 本轮抽查证据

**抽样率**：3 项点名**全部复核**（3 项都实跑）；另**主动查了 exporter 的订阅全景**（因为那是我上轮读漏的地方）。

### A. 我上轮的错 —— 「全仓零内置订阅方」是错的

**你是对的，我错了。** 订阅方一直存在，是 `observability_sink.attach()` 注册的 exporter：
```python
# app/observability_sink.py
:82   for event in exporter.hooks():          ← 遍历 exporter 声明的事件
:91       registry.register(event, _hook)     ← 注册（registry 默认 DEFAULT_HOOKS）
```
**我为什么会漏**：我上轮 grep 的是 **`register_hook\(|subscribe\(`** —— 而实际调用是 **`registry.register(event, _hook)`**。**搜错了方法名，就得出"零"。**

**这与 2.2 的 fallback 是同一族**：**用间接指标（方法名匹配）推断"有没有"，而那个指标只覆盖一种写法。** 这是我这十几轮里第四次同族错误（第 15 轮 browser、第 17 轮 `TurnBudget` 同名、第 18 轮 fallback、这次方法名）。
**规则要升级**：**"搜不到"只能证明"这个写法没有"，不能证明"没有"。** 要证明"没有"，得用**穷举式**判据（AST 扫全部 `Call` 节点里 `.register`/`.emit` 的属性名，或者**跑一遍看告警**）。

**而 fail-loud 的价值在这里被实证了**：它把「13 个没接」从**散文**变成了**运行时指名告警**，而且**立刻抓出一个我们俩都读漏的事实**。**这是"把知识写成代码"胜过"把知识写成文档"的最好例证。**

### B. 你点名的 3 项 —— **3/3 全部撑得住**

**① `on_session_end` 是否真在 `finally` 里 → ✅✅ 在，而且取的是最终状态**

结构确认（`agent.py` 任务体）：
```python
try:
    ... 任务主体 ...
finally:
    self.partial.pop(task.id, None)
    self._token_usage.pop(task.id, None)
    # `on_session_end` 发射点（MATRIX 13.5）：与 `on_session_start` 配对。
    # 放在 `finally` 里，**失败/取消的任务也要发**——否则观测端的会话数永远少于
    # 实际跑过的任务，而"少记"比"记错"更难发现。
    try:
        _final = await self.store.get(Task, task.id)     ← 取最终状态
        hooks.emit("on_session_end", {
            "task_id": …, "conversation_id": …,
            "status": str(getattr(_final, "status", "") or ""),   ← 真实终态
            "error":  str(getattr(_final, "error", "") or ""),
        })
    except Exception as exc:  # noqa: BLE001 —— 观测钩子不该影响任务收尾
        log.warning("on_session_end 发射失败（不影响任务）：%s", exc)
```
**两处超出要求**：
- 不只是"在 finally 里"，还**回查 `_final.status`** → 失败/取消的任务**上报的是真实终态**，不是笼统的"ended" ✓
- **自身包 try/except** → 钩子失败不会破坏任务收尾 ✓

**你那句「"少记"比"记错"更难发现」我完全同意**，而且它是这条放 `finally` 的**唯一正确理由**——不是"顺便覆盖失败路径"，是**观测端的计数一致性**。

**② `test_degraded_text_actually_carries_the_original_question` 能不能红 → ✅ 能，且断言的正是"真正发出去的文本"**

```python
class _RecordingText(platforms.ConsoleAdapter):
    def send_text(self, channel, text, **_kwargs):
        sent.append(text)          ← 抓住真正发出去的文本
clarify_delivery.configure(_RecordingText(), "chan-r")
# 返回值这里故意不看——**这条只认"真正发出去的文本"**。
# 载荷对不对由上面几条断言管；两者混在一起会让人以为验了投递。
clarify_payload(_args(3))
assert sent, "降级路径没有真的调用 send_text"
text = sent[-1]
assert "选哪个方案" in text                                    ← ① 问题原文
for index in range(1, 4):
    assert f"{index}) 选项{index}" in text                     ← ② 每个选项带序号
assert "回复序号" in text                                      ← ③ 作答说明
```
**把 `"\n".join(lines)` 换成 `question`** → 选项行与作答说明全消失 → ②③ 必红 ✓✓
**还有一处我很认可**：「**返回值这里故意不看**……两者混在一起会让人以为验了投递」——**这条注释本身就是方法**：它把"载荷正确"与"投递正确"两个断言**显式分开**，防止一个测试同时冒充两者。

另加 `test_degraded_text_marks_a_forced_choice`：`allow_free_text=False` 时文本必须明说「只能选」（`:178` 断言"强制选择时文本没写清只能选几项"）✓

**③ `test_a_live_registered_process_is_never_reaped` 是否真构造 `alive` → ✅✅ 真构造了，skip 分支没走到**

```python
:137  started = process_start_time(os.getpid())          ← 真实 start_time
:138  if started is None:  # 拿不到 start_time 的平台构造不出 alive 记录
:139      pytest.skip("本平台读不到进程启动时间")
:145  "pid": os.getpid(),
:146  "host_start_time": started,                        ← 用它造 alive 记录
:159  assert os.getpid() in remaining, (
:160      "活着的登记被回收删掉了——三态（alive/dead/reused）被退化成了两态"
:162  assert report.removed == 0, f"不该删任何东西，实际删了 {report.removed} 条"
```
**实跑 `3 passed, 0 skipped`** → **`:139` 的 skip 没走到**，说明 `process_start_time(os.getpid())` 返回了真值、**`alive` 记录真的构造出来了** ✓✓
**这是回答你问题最硬的证据**：不是"代码里有 skip 分支所以可能没测"，而是**运行结果里 skip 计数是 0**。

**另**：`test_reap_never_kills_processes`（`:78`）用 `os.getpid()` + **故意对不上的 `host_start_time: 1.0`** 造 `reused` → 覆盖了第二态 ✓。三态里 alive 与 reused 都有用例，`dead` 由 `test_startup_reaps_dead_ledger_entries` 覆盖 → **三态全覆盖** ✓

### C. 你踩的 JSON 数组坑 —— 与 2.2 同族，值得升级成一条规则

> 账本落盘是 **JSON 数组**，我第一版写成 `{"entries": [...]}`，`ProcessRegistry.load()` 直接当空 → 回收「什么都没找到」而看起来一切正常。

**你自己已经点出它和 2.2 同族**，我补一条更可操作的判据：
> **凡是 `load()` 类函数，都要有一条"喂错形状必须报错/告警"的用例。**

因为 `load()` 的典型实现是 `json.load(...)` + 取默认值，**形状错 → 取到空 → 返回空集合**，与"文件里真的没有记录"**完全同形**。这与 `getattr(...,default)`、`parity_rows()` 的回落、`process_start_time` 的 skip 分支是**同一族：静默降级**。

**建议把这类归纳成一个名字**（方便以后互相提醒）：**"静默降级四兄弟"** —— ①`getattr`/`.get` fallback ②`load()` 形状不匹配返回空 ③`if x is None: skip/pass` ④"搜不到方法名就断言没有"。**它们的共同特征：失败与"确实没有"同形。**

---

## 与目标标志的差距

**⬜ 0**，离目标还差 **18 条 🟡 + 4 条 ⚫**：

```
🟡 18：1.7 1.13b 4.3 4.4 4.8 4.9 4.12 5.2 5.14 6.4 10.8 10.14
       12.5 12.10 13.4 13.5 14.8 15.4
⚫ 4 ：4.2 4.7 8.3 12.9
```

### 你问的：`5.13` 接的是 ConsoleAdapter，算 ✅ 还是 🟡 —— **我的判断是 ✅，但证据栏必须写清"验证边界"**

**理由三条**：

1. **5.13 的边界是"契约实现"，不是"某个真实平台的按钮"。** 真实平台按钮属于 **12.5（平台适配器）**。**用 12.5 的缺口去压 5.13，是跨条目追责**——而跨条目追责会让每条都永远做不完。
2. **按钮路径不是"没验证"，是"在契约层验证了"。** 假适配器是 `PlatformAdapter.send_clarify` 的**契约实例**，它证明了 `render_choices` 真的产出按钮、callback 格式是 `cl:<n>` ——**那正是真实适配器要消费的东西**。契约正确 + 实现正确 = 本条完成。
3. **生产路径真跑过。** `chat.py:66 clarify_delivery.configure(platforms.ConsoleAdapter(), "cli")` 是**唯一**生产配置点；`orbit chat` 是当前唯一宿主，它没按钮 → **降级是唯一正确的行为** ✓

**但必须加一条硬要求 —— 证据栏写明验证边界**，例如：
> 「按钮路径在**契约层**验证（假适配器实例化 `PlatformAdapter.send_clarify`，断言 `used == "buttons"` 与 callback 格式 `cl:<n>`）；**真实平台的按钮从未被按过**——取决于 12.5 的平台适配器，不在本条范围。」

**为什么这条不能省**：不写，下一个人看到 ✅ + 「≤5 个选项**真的出按钮**」会以为"Telegram 上试过了"。**这正是我们反复防的"✅ 被读成比它承诺的更多"。**

**另外送你一个跨条目观察（本轮新发现，建议记进 12.5）**：
```
clarify_delivery.configure() 的生产调用点：只有 chat.py:66（ConsoleAdapter, "cli"）
```
→ **即使将来 12.5 把 Telegram 适配器写完，如果宿主不在驱动任务前调 `configure(adapter, channel)`，按钮依然不会出。** 这是**横跨 5.13 与 12.5 的接线缺口**。建议在 12.5 的证据栏记一句：「按钮路径还需宿主在驱动任务前调 `clarify_delivery.configure(adapter, channel)`，目前只有 `orbit chat` 调了」。

### 顺带：13.5 对 exporter 的真实缺口是 6 个，不只是"subagent 2 条"

我查了 exporter 的订阅全景（这是我上轮该做没做的）：

| exporter 订阅的 11 个事件 | 状态 |
|---|---|
| `pre_llm_call` / `post_llm_call` / `pre_tool_call` / `post_tool_call` / `on_session_end` | ✅ 已接通（5） |
| **`pre_api_request` / `post_api_request` / `api_request_error`** | ⚠️ **仍会告警** |
| **`on_session_finalize`** | ⚠️ 仍会告警 |
| **`subagent_start` / `subagent_stop`** | ⚠️ 仍会告警（你下轮计划） |

→ **仍缺 6 个**，其中 **`pre_api_request` / `post_api_request` / `api_request_error` 是 API 请求级事件——那正是 Langfuse 做 tracing 最需要的三件**。**13.5 的真实缺口不只是"subagent 两条"。** 建议下轮补 subagent 时，把这三个 API 级事件一起排进计划（它们在 `agent.get_model()`/`stream_model` 那一带，位置集中）。

---

## 风险与漂移

### R1 🟠 **我的"搜不到方法名就断言没有"（A 节）—— 需要升级规则**

`register_hook\(|subscribe\(` 搜不到 ≠ 没有订阅方，因为实际写法是 `registry.register(...)`。
**规则**：**"搜不到"只证明"这个写法没有"。要证明"没有"，用穷举判据（AST 扫全部调用点）或跑一遍看告警。**

### R2 🟡 **基线被在飞文件改动（pytest +4）**

本轮第三次。**规律**：`git status` 非空时，任何计数类读数（测试数 / mypy 文件数 / ruff 错误数）都不可信。**建议**：报基线时**同时报 `git status` 条目数**，非 0 就标注"基线含在飞代码"。

### R3 🟡 **13.5 的缺口比"4/17"这个说法更大**（见上表）

对 exporter 而言是 **6 个仍未接通**，其中 3 个是 API 级事件（tracing 的核心）。**建议 13.5 的证据栏按"订阅方视角"补一张表**，而不只是写"4/17 → 6/17"。

### R4 🟡 **轮末 3 项在飞**

```
轮初 git status: 0 条目 ✓
轮末 git status: 3 条目
  M app/delegate.py  app/hooks.py
  ?? tests/test_subagent_hooks.py     ← 4 passed（你下轮计划的 subagent 已在做）
```

### R5 🟡 **未验证项（如实标注）**

| 项 | 为什么没验 |
|---|---|
| `test_degraded_text` / `test_a_live_registered_process` 的**红路径实跑** | 要改代码 → 未做；验证的是"断言抓的是真正发出去的文本 / skip 计数为 0" |
| `on_session_end` 在**真失败任务**上的实际发射 | 要造一个失败任务跑完整图 → 未跑；只核了它在 `finally` 内 + 取 `_final.status` |
| `10.7` 启动回收的**真实启动路径** | 只核了测试与 `reap()` 调用点，未真起一次应用看回收发生 |
| `12.10` 的 `enroll`、`4.12` 的 19 家逐家 | 要网络/凭证 → 未跑 |
| `orbit chat` 交互、`gateway start`、`cron run`、`backup create/restore`、`config edit`、`doctor --fix/--live` | 会改状态或真发请求 → 未运行 |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用（连续 15 轮如此） |

---

## 下一轮建议

按优先级：

1. **【P0】修 `Command.hermes` 字段 + 去掉 `parity_rows()` 回落（你已列为 P0）**：仍是 `orbit_only` 口径的前置。**注意它和本轮那三兄弟同族**（fallback 让"没填"看起来像"填了"）。
2. **【P0】补 subagent 时把 `pre_api_request` / `post_api_request` / `api_request_error` 一起排进计划**（R3）：它们才是 Langfuse tracing 的核心，且位置集中在 `get_model`/`stream_model` 一带。**只补 subagent 两条，exporter 仍会有 4 个事件收不到。**
3. **【P1】5.13 的证据栏补"验证边界"段**（见上节）：一句话的成本，防的是"✅ 被读成比它承诺的更多"。
4. **【P1】12.5 记一句 `configure()` 的接线缺口**：即使平台适配器写完，宿主不调 `configure` 按钮也不出。
5. **【P1】把"静默降级四兄弟"写成方法论**（C 节）：①`getattr`/`.get` fallback ②`load()` 形状不匹配返回空 ③`if x is None: skip/pass` ④搜不到方法名就断言没有。**共同特征：失败与"确实没有"同形。** 这四条现在各踩过一次，写成一条规则能一次防住。
6. **【P2】报基线时同时报 `git status` 条目数**（R2）：非 0 就标"含在飞代码"。这轮 +4、上轮 mypy +2、再上轮 ruff 30 errors，全是同一原因。
7. **【P2】`4.12` 照 `4.2` 的 `BEDROCK_LIMITATIONS` 逐家写**：现在"19 家 4 家可用"读者不知道另外 15 家是"没做"还是"缺条件"。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 18 轮未核。`nodes.py:254 context_meta.setdefault("compressed", {})` 仍是空占位。

---

*本报告由 monitor 角色只读生成。**先认我上轮的错**：「全仓零内置订阅方」是错的——订阅方是 `observability_sink.attach()` 的 `for event in exporter.hooks(): registry.register(event, _hook)`，而我 grep 的是 `register_hook\(|subscribe\(`，**搜错方法名就得出"零"**。这是同族第四次错误，规则升级为：**"搜不到"只证明"这个写法没有"，不证明"没有"**。**fail-loud 因此被实证有效**：它把散文变成了运行时指名告警，并抓出我们俩都读漏的事实。三项点名全部撑得住：`on_session_end` **在 finally 内且回查 `_final.status`**（失败任务上报真实终态）、`test_degraded_text` **抓的是真正发出去的文本**（且注释显式分开"载荷正确"与"投递正确"）、`test_a_live_registered_process` **实跑 0 skipped 说明 alive 真构造出来了**。**另查出一个比你说的更大的缺口**：exporter 订阅 11 个事件，现在仍有 **6 个**会告警，其中 `pre_api_request`/`post_api_request`/`api_request_error` 是 tracing 的核心。`5.13` 我判 **✅**，但证据栏必须写明"按钮路径在契约层验证、真实平台从未按过"。未验证项单列，不混进完成项。*
