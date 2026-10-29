# 对标进度报告

**更新时间**：2026-10-29 08:53（本机实测时钟 `Get-Date`，只读取证）
**本轮轮次**：**N = 38**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP / GAPS 文件；未装依赖；未启动后端；未跑会改状态或真发请求的命令（边界动作：跑了 1 个测试文件 + 一次全量；实测 `DelegateRunner.cancel` 是否可 await）。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `086e63c`；**轮初 `git status` 0 条目，轮末 0 条目**）

---

## 总体完成度

### **169 / 181 项 = 93.4% 严格 · 95.5% 可验证**（✅ 164 + 🟢 5）

| 项 | 说明 |
|---|---|
| 五态 | **✅ 164 · 🟢 5 · ⚫ 4 · 🟡 8 · ⬜ 0**（合计 181 ✓） |
| 严格 / 可验证 | **93.4%** / **95.5%** —— 未变（**修洞轮，正确**） |
| `matrix_stats --check` | **exit 0**，零告警 ✅ |
| 顶层命令对齐 | **77/77（Orbit 顶层 84，自有 7）** + 口径说明 ✅ |

### 硬口径：**全部一致**

| 你的口径 | 我的实测 | 判定 |
|---|---|---|
| ruff `All checks passed!` | **All checks passed!**（exit 0） | ✅ 一致 |
| mypy **175** files clean | **175 files clean**（exit 0） | ✅ 完全一致 |
| pytest 1 failed, **3615** passed, 3 skipped | **1 failed / 3615 passed / 3 skipped（429.29s）** | ✅ **完全一致** |
| `matrix_stats --check` exit 0 | **exit 0**，181/164/5/4/8/0 | ✅ 一致 |
| 已提交 `086e63c`，工作区 0 项 | **轮初轮末均 0 项** | ✅ 一致 |

---

## 分层完成度

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | 15 ✅ + 1 🟢 / 18 | 88.9% | 🟡2 |
| L2 Agent 核心循环 | 9 / 10 | 90.0% | 🟡1 |
| **L3 上下文装配** | **8 / 8** | **100%** | ✅ |
| **L4 模型适配** | **10 ✅ + 2 ⚫** / 13 | 92.3% | 🟡1 |
| **L5 工具层** | **19 / 19** | **100%** | ✅ |
| **L6 工具集 / 注册** | **5 / 5** | **100%** | ✅ |
| L7 Skill / curator | **17 ✅ + 1 🟢** / 18 | 100% | ✅ |
| L8 MCP | 9 ✅ + 1 ⚫ / 10 | 90.0% | — |
| **L9 记忆层** | **16 / 16** | **100%** | ✅ |
| L10 持久化 / 会话 | **14 ✅ + 2 🟢** / 15 | 100% | ✅ |
| **L11 cron** | **6 / 6** | **100%** | ✅ |
| L12 Gateway / 多平台 | 8 ✅ + 1 ⚫ / 11 | 72.7% | 🟡2 |
| L13 插件 | **9 / 10** | 90.0% | 🟡1 |
| L14 安全 / 审批 | 8 ✅ + 1 🟢 / 10 | 90.0% | 🟡1 |
| **L15 可观测性** | **6 / 6** | **100%** | ✅ |
| **L16 Web UI / 前端** | **6 / 6** | **100%** | ✅ |
| **合计** | **169 / 181** | **93.4%** | — |

---

## 本轮抽查证据

**抽样率**：3 项点名**全部复核**（3 项都实测）。

### A. ★ 最重要：④ 的修复里有一个 `await` 错误 —— **`DelegateRunner.cancel` 是同步方法，`await` 它抛 TypeError 被吞掉**

**我实测**：
```
delegate.py:360   def cancel(self, child_task_id: str) -> bool:      ← **同步，不是 async**
asyncio.iscoroutinefunction(r.cancel)  →  False
await r.cancel("nope")                 →  TypeError: object bool can't be used in 'await' expression
```

**所以在 `cancel_children` 里实际发生的是**：
```python
await runner.cancel(child_id)
#     ^^^^^^^^^^^^^^^^^^^^^^ ① 先求值：cancel() 真的执行了（inner.cancel() 生效）→ 返回 True
# ^^^^ ② 再 await True → **TypeError**
except Exception as exc:      # ③ 被这里吞掉
    log.warning("停子任务 %s 失败：%s", child_id, exc)
# stopped 永远不增加
```

**三个后果，按严重度**：

| # | 后果 | 说明 |
|---|---|---|
| **①** | **`stopped` 永远是 0** | `cancel_children` 的返回值恒为 0 —— **函数自述"返回停掉了几个"是假的** |
| **②** | **每次都留一条 warning** | 内容形如「停子任务 X 失败：object bool can't be used in 'await' expression」—— **看起来像失败，实际子任务停了**。这会让真失败淹没在假失败里 |
| **③** | **没有任何东西 await 子任务的真正退出** ⚠️ | **这是你 Q1 担心的那条，而且担心成立** |

**③ 为什么是原洞没有真正关掉**：
- `inner.cancel()` 只是**发出取消请求**，子任务的协程在**下一个 await 点**才收到 `CancelledError`，然后还要跑它自己的 `finally`（写状态、清理）
- 而父任务这边：`cancel()` → `cancel_children()` **立即返回**（因为 await 抛了）→ `job.cancel(); await gather(job)` 只等**父**的 job
- **子任务（`inner`）没人 await** → 父的 `finally` → **`release(conversation_id)` 可能发生在子任务还在收尾时**
- → **窗口变小了，但没有消失。** 而 10.8 的全部意义是"没有窗口"。

**你的测试为什么没抓到**：它断言的是**「子任务停了」**（`inner.cancel()` 确实让它停）**，不是「`cancel_children` 等到了它停」**。这两件事在测试里长得一样，在运行时差一个 await。

**修法（两处都要改）**：
```python
# ① cancel_children 里：不要 await 同步方法
runner.cancel(child_id)          # 去掉 await

# ② 但要 await 子任务的真正退出（这才是"先停子再停父"的实质）
inner = getattr(runner, "_inner", {}).get(child_id)
if inner is not None:
    await asyncio.gather(inner, return_exceptions=True)
```
**或者更干净**：**把 `DelegateRunner.cancel` 改成 `async def`**，内部 `inner.cancel()` 之后 `await asyncio.gather(inner, return_exceptions=True)` —— **这样调用方 `await runner.cancel(...)` 就是对的，而且语义完整。**
**注意**：`delegate.py` 的 docstring 说"只取消内层的 runner 任务，不取消自己那个 `_execute` 任务"—— 那个约束与"await 内层退出"**不冲突**（内层是 `_inner[child_task_id]`，`_execute` 是另一条）。

### B. 你点名的另 2 项

**② `_RUNNERS` 模块级 dict 会不会泄漏 → ✅ 会，而且正常路径就泄漏**

```
tool_delegate.py:132   _RUNNERS[task_id] = runner        ← 派子任务时写入
tool_delegate.py:145   runner = _RUNNERS.pop(task_id, None)   ← **只在 cancel_children 里 pop**
tool_delegate.py:116   _RUNNERS.clear()                  ← 只在 reset() 里，**生产不调**
```

**所以**：
- 父任务**被 cancel** → `cancel_children` → `pop` → **不泄漏** ✓
- 父任务**正常跑完**（绝大多数情况）→ **没有任何路径 pop** → **记录永远留着** ❌

**→ 每个派过子任务的父任务，泄漏一个 `DelegateRunner`（含 `_children` / `_inner` 两个 dict）。** 长跑的 gateway 上会**无界增长** ✓ **你问的"算不算泄漏"：算，而且是主要路径上的泄漏。**

**建议**：**在父任务结束处 pop** —— 最自然是 `agent.py` 那个 `finally`（`release` 旁边），**与租约一起清**：
```python
finally:
    self.leases.release(task.conversation_id)
    tool_delegate.forget(task.id)      # ← 或直接 _RUNNERS.pop(task.id, None)
```
**顺带一个好处**：把"父任务结束"和"清理子任务相关记录"放在同一处，**下次有人加子任务资源时会看到这里**。

**③ 5.14 判 🟡 是否还站得住 → ✅ 站得住，而且比上轮更站得住**

**你问「那 2 条现在是完整的了吗」→ 不完整**：
| 缺口 | 状态 |
|---|---|
| 限流相位 | **未做**（GAPS.md 已记） |
| 预算更紧的算法（`min(50, 父剩余/子数)`） | **未做** |
| **④ 中断传播** | **上轮是"未做"，这轮"做了但有一个 await 错误 + 没有 await 子任务退出"** |

→ **所以 5.14 保持 🟡 正确** ✓ **而且如果按"④ 完整了"去写证据栏，就会高估。**

### C. 你修 ② 的方式我认可，而且理由比"记账"更深

> 「**不按工具名判断**（按名字判断会在改名时静默失效）——只认 `steps` 键，其余工具恒为 0。」

**这条判断对** ✓ —— **按工具名匹配会在改名/别名/包装时静默失效，而"结果里有没有 `steps` 键"是结构判据，不依赖命名。** 这与我们之前立的「用可核对的事实而不是可编造的字段做判据」（`load_index` 用 `path` 不用 `module`）是同一族 ✓

**上限写进证据栏也做对了** ✓：「`DEFAULT_MAX_CONCURRENT = 4` 但**那是并发不是总数**——顺序 spawn 10 个就是 90 + 500 步」。**"放大 3–5 倍"与"烧穿"读者现在能分清。**

---

## 与目标标志的差距

**⬜ 0**，离目标还差 **8 条 🟡 + 4 条 ⚫**：

```
🟡 8：1.7 1.13b 4.8 4.12 5.14 12.5 12.10 14.8
⚫ 4 ：4.2 4.7 8.3 12.9
```

---

## 风险与漂移

### R1 🟠 **`await runner.cancel(...)` 对同步方法 → TypeError 被吞（A 节）**

三个后果：`stopped` 恒为 0 / 每次留假失败 warning / **没有 await 子任务退出 → release 仍可能早于子任务收尾**。
**修法**：去掉 `await` **并且** await 内层任务的退出（或把 `DelegateRunner.cancel` 改成 `async def`）。

### R2 🟠 **`_RUNNERS` 在正常路径上泄漏（B②）**

只在 `cancel_children` 里 `pop`，`reset()` 生产不调 → **父任务正常跑完就永久留一条**。
**修法**：在 `agent.py` 的 `finally`（`release` 旁边）pop。

### R3 🟡 **测试断言了「子任务停了」但没断言「等到了它停」（A 节）**

两件事在测试里长得一样，**在运行时差一个 await**。
**建议**：测试断言 **`cancel_children()` 的返回值 == 子任务数**（现在恒为 0，会立刻红）—— **一条断言就能钉住这个 bug。**

### R4 🟢 **本轮两处判断质量高**

1. **「先停子再停父」的顺序判断** —— 理由（"反过来父的 job 一 cancel，这条协程可能被打断在 `await` 上，子就漏掉了"）**是对的**，只是实现上漏了"等子退出"那一步
2. **② 不按工具名判断** —— 用结构判据（有没有 `steps` 键）而不是命名判据 ✓

### R5 🟡 **未验证项（如实标注）**

| 项 | 为什么没验 |
|---|---|
| A 节 TypeError 的**端到端复现** | 我实测了 `await r.cancel(...)` 抛 TypeError（确凿），**但没有构造完整"父跑→spawn 子→cancel 父"的时序看 warning 真的出现** |
| `_RUNNERS` 泄漏的**实测** | 只读了写入/pop/clear 三处，**没有跑到内存增长** |
| `GraphChildRunner` 的 `thread_id` 隔离正确性 | 只读了 `run()` 主体，**未审图复用的隔离** |
| `12.10` 的 `enroll`、`4.12` 的 19 家逐家 | 要网络/凭证 → 未跑 |
| `orbit chat` 交互、`gateway start`、`cron run`、`backup create/restore`、`config edit`、`doctor --fix/--live` | 会改状态或真发请求 → 未运行 |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用（连续 32 轮如此） |

---

## 下一轮建议

按优先级：

1. **【P0】修 `await runner.cancel(...)`**（R1）：去掉 `await` **并且** await 内层任务退出 —— 或直接把 `DelegateRunner.cancel` 改 `async def`（更干净，且调用方语义就对了）。
2. **【P0】给测试加一条断言：`cancel_children()` 返回值 == 子任务数**（R3）：**现在恒为 0，加上立刻红** —— 一条断言钉住整个 bug。
3. **【P0】`_RUNNERS` 在父任务 `finally` 里 pop**（R2）：与 `release` 放同一处，**下次有人加子任务资源会看到这里**。
4. **【P1】修完 ④ 再更新 MATRIX 5.14 / 10.8 的证据栏**：**现在写的是"已修"，但实际有 await 错误** → 不改就等于把上一轮的问题原样留进文档。
5. **【P1】你的 4.12 计划我认同** ✓ —— 它是剩余 🟡 里最小的一条（把 4 家语音在宿主进程验一遍并如实报），**而且不需要设计决策**。**但先修完 R1/R2/R3 再换手** —— 理由是你自己上轮立的规矩：「**刚改完就换手，最容易留下没验过的活**」。
6. **【P1】把「`await` 一个同步方法不会报错在调用点，只会在 await 处抛 TypeError」写进方法论**：**这类错误 ruff/mypy 都看不见**（mypy 通常能看出来，但这里 `runner` 是 `Any` 类型 → 类型检查被绕过）。**判据：`Any` 类型上的 `await` 是不可检查的，必须靠测试断言返回值。**
7. **【P2】`4.12` 照 `4.2` 的 `BEDROCK_LIMITATIONS` 逐家写**。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 34 轮未核。`nodes.py:254 context_meta.setdefault("compressed", {})` 仍是空占位。

---

*本报告由 monitor 角色只读生成。**本轮最重要发现：④ 的修复里 `await runner.cancel(child_id)` 有一个 `await` 错误** —— `delegate.py:360` 的 `cancel` 是**同步方法**（`asyncio.iscoroutinefunction` 实测 False），`await` 它抛 `TypeError: object bool can't be used in 'await' expression`，被 `except Exception` 吞掉。**三个后果**：① `stopped` 恒为 0（函数自述"返回停掉了几个"是假的）② 每次都留一条**假失败** warning ③ **没有任何东西 await 子任务的真正退出** —— 而**③ 正是你 Q1 担心的那条，担心成立**：`inner.cancel()` 只发出请求，子任务还要在下一个 await 点收到 `CancelledError` 再跑自己的 `finally`；而父的 `finally → release()` 在 `cancel_children` 立即返回后就会执行 → **窗口变小了但没消失，而 10.8 的全部意义是"没有窗口"**。**你的测试没抓到是因为它断言"子任务停了"（`inner.cancel()` 确实让它停），不是"`cancel_children` 等到了它停"—— 两件事在测试里长得一样，在运行时差一个 await。** 建议：**去掉 `await` + await 内层任务退出**（或把 `cancel` 改 `async def`），**并加一条 `assert cancel_children(...) == 子任务数`（现在恒为 0，加上立刻红）**。另两项：**`_RUNNERS` 确实泄漏，而且是主要路径上的泄漏** —— 只在 `cancel_children` 里 pop、`reset()` 生产不调，**父任务正常跑完就永久留一条**（每个派过子任务的父任务泄漏一个 `DelegateRunner`），建议在 `agent.py` 的 `finally`（`release` 旁边）pop；**5.14 保持 🟡 正确，而且比上轮更站得住**（限流相位未做、预算更紧算法未做、④ 实际未完成）。**你修 ② 的方式我认可** —— 不按工具名判断、只认 `steps` 键，是结构判据而不是命名判据，与 `load_index` 用 `path` 不用 `module` 同一族。未验证项单列，不混进完成项。*
