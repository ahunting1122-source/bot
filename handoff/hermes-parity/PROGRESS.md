# 对标进度报告

**更新时间**：2026-10-28 20:38（本机实测时钟 `Get-Date`，只读取证）
**本轮轮次**：**N = 24**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑会改状态或真发请求的命令（边界动作：跑了 3 个测试文件 + 一次全量；实例化 `Settings` 做了 `setattr` 实测）。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `21df943`；**轮初 `git status` 0 条目，轮末 0 条目**）

---

## 总体完成度

### **161 / 181 项 = 89.0% 严格 · 91.0% 可验证**（✅ 156 + 🟢 5）

| 项 | 说明 |
|---|---|
| 五态 | **✅ 156 · 🟢 5 · ⚫ 4 · 🟡 16 · ⬜ 0**（合计 181 ✓） |
| 严格 / 可验证 | **89.0%** / **91.0%** —— **与上轮完全相同**（加固轮，无状态翻转） |
| 与 `matrix_stats --check` | 逐字一致，exit 0 ✅ |
| 顶层命令对齐 | **77/77（Orbit 顶层 84，自有 7）** + 口径说明 —— 持续在输出里 ✅ |

### 硬口径：**全部一致，基线干净**

| 你的口径 | 我的实测 | 判定 |
|---|---|---|
| ruff `All checks passed!` | **All checks passed!**（exit 0） | ✅ 一致 |
| mypy **172** files clean | **172 files clean**（exit 0） | ✅ 完全一致 |
| pytest 1 failed, **3541** passed, 3 skipped | **1 failed / 3541 passed / 3 skipped（387.62s）** | ✅ **完全一致** |
| `matrix_stats --check` exit 0 | exit 0 | ✅ 一致 |
| `git status` 7 项，无在飞文件 | **轮初轮末均 0 项** | ✅ 一致（改动已提交成 `21df943`） |

**"数字没动"这一轮同样是好事**：查出的 4 个死写入 + 2 个"靠巧合生效"**都在既有 ✅ 条目内部**，没有一个翻转状态。**加固轮不涨分是对的** —— 如果修 bug 就涨分，那说明之前的分是虚的。

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
| L10 持久化 / 会话 | **13 ✅ + 2 🟢** / 15 | 100% | ✅ |
| **L11 cron** | **6 / 6** | **100%** | ✅ |
| L12 Gateway / 多平台 | 8 ✅ + 1 ⚫ / 11 | 72.7% | 🟡2 |
| L13 插件 | 7 / 10 | 70.0% | 🟡3 |
| L14 安全 / 审批 | 8 ✅ + 1 🟢 / 10 | 90.0% | 🟡1 |
| L15 可观测性 | 5 / 6 | 83.3% | 🟡1 |
| **L16 Web UI / 前端** | **6 / 6** | **100%** | ✅ |
| **合计** | **161 / 181** | **89.0%** | — |

---

## 本轮抽查证据

**抽样率**：3 项点名**全部复核**（3 项都实跑）；另**实测了 `setattr` 盲区，查出一个真 bug**（见 A②）。

### A. 你点名的 3 项

**① `safe_mode` / `ignore_rules` 是否真的还能用 → ✅✅ 三条判据全过**

```
safe_mode      in_model_fields=True  构造后读=True   model_dump有=True
ignore_rules   in_model_fields=True  构造后读=True   model_dump有=True
```
**三条判据是我上轮建议的那三条**（读得到 / 在 `model_fields` / `model_dump` 里有），**两条字段全过** ✓✓
**`model_dump()` 那条是关键**：裸属性会在这里丢，所以它证明这两个现在是**真字段**而不是"恰好进了 `__dict__`"。

**而且你"误报 property"的自我修正我核了，是对的**：
```
machine_wide in model_fields: False
machine_wide 是 property: True
实例读得到: True
```
→ `machine_wide` 是 `Settings` 上的 `@property`，**合法**，`in model_fields` 查不到但它不是 bug ✓
**你的判据修正（"在类上定义过——字段和 property 都算，实例裸属性不算"）是唯一正确的那个**，我在 `test_settings_fields_are_real.py:97-99` 读到了这段说明 ✓

**② AST 扫描是否漏掉 `setattr(settings, key, value)` → ⚠️ 它确实是盲区，而且我实测出一个真 bug**

**先确认你的扫描确实用 AST**（不是纯正则）：
```
:106  def _keys_written_to_settings()
:115      import ast
:129      tree = ast.parse(...)
:132      for node in ast.walk(tree):
:135          if isinstance(node, ast.Assign):
:140              if isinstance(target, ast.Attribute) and _is_settings(target.value):
```
✓ **真 AST，只认 `Attribute` 目标** —— 所以注释与字符串（你踩的第二次）确实被排除了。
（小提示：`_model_copy_literals` 仍是正则，但那是 `model_copy(update={...})` 这种极窄形状，且有 4 条测试兜着，**不算问题**。）

**盲区本身：`hasattr` 对 property 也返回 `True` → `setattr` 崩** —— 我实测：
```
machine_wide   hasattr=True   setattr 抛错: AttributeError: property 'machine_wide' of 'Settings' object has no setter
safe_mode      hasattr=True   setattr 成功  读回=True
no_such_key    hasattr=False  setattr 成功  读回='<缺>'      ← 被守卫挡住，符合你的判断
```
**`Settings` 上有 2 个只读 property**：`machine_wide` / `file_scope_label`。
→ **如果 `settings.json` 里出现 `"machine_wide": ...`（前端"连接与设置"页写的，或用户手改），`app/cli/context.py:110-111` 的 `hasattr` 守卫会放行，然后 `setattr` 抛 `AttributeError` —— 未被捕获，CLI 启动崩。**

**这算不算风险？我的判断：算，但性质不是"越权"而是"健壮性"**（详见 B 节）。
**一句话修法**：`if hasattr(self.settings, key)` → **`if key in type(self.settings).model_fields`**。这一改同时做三件事：
- 挡住裸属性（现在已挡）
- **挡住 property（现在没挡）** ← 修 bug
- 语义正好是"**只有声明过的字段可写**"

**③ `test_stop_reasons_are_actually_produced_by_the_policy` 能不能红 → ✅✅ 能，而且两个产出方都有反证**

```python
# 三种方式真驱动 check() 产出三个原因
produced.add(policy.check({"actions": 10**6, "turns": 0, "budget": budget}))       # max_actions
produced.add(policy.check({"actions": 0, "turns": 10**6, "budget": budget}))       # max_turns
full["token_estimate"] = int(full["max_tokens"]) + 1
produced.add(policy.check({"actions": 0, "turns": 0, "budget": full}))             # max_tokens
produced.discard("")
missing = set(DefaultBudgetPolicy.STOP_REASONS) - produced
assert not missing, ("这些原因声明了但 `check()` 产不出来：{missing}——"
                     "声明与实现漂移，提示会为一种不存在的情况写文案")
```
**往 `STOP_REASONS` 加 `"max_seconds"` → `produced` 里没有它 → `missing` 非空 → 必红** ✓✓

**`session_takeover` 的反证在哪**：它不由 `check()` 产出，而由 `_lease_lost()` 返回（`nodes.py:1518 return LEASE_STOP_REASON`）—— **它的反证在 `test_takeover_makes_the_original_task_yield`**（断言 `"session_takeover" in reasons`）。
→ **两个产出方各有一条反证，只是在不同文件里** ✓ **覆盖是完整的，不是遗漏。**

### B. 上轮那两条建议的落地质量

**① 清单断言 → 性质断言：✅ 完整落地，而且比我建议的多做了一步**

```python
# test_hint_commands_exist.py:86
declared = tuple(DefaultBudgetPolicy.STOP_REASONS) + (nodes_module.LEASE_STOP_REASON,)
```
✓ **遍历并集**，不再硬编码 4 个 ✓
**而且 `policies.py:394` 的 docstring 直接抄了我那句**：
> 「把清单写在测试里会过期，写在产出方不会。」

**多做的这一步是关键**：我建议"策略声明 + 并集"，**你额外加了反证测试**（声明的每个原因必须真能产出）。**这一步把"声明"和"实现"也钉在了一起** —— 我上轮只说了"新增原因会自动要求写提示"，**没说"声明了却产不出来"这种情况**。**你的补法更完整。**

**② `Settings` 裸属性全扫：✅ 做了，而且可复用函数抽出来了**

```python
# tests/test_settings_fields_are_real.py:45
def assert_real_settings_field(settings, name: str) -> None:
    assert name in Settings.model_fields, f"`{name}` 不在 `Settings.model_fields` 里…"
    assert hasattr(settings, name), f"`{name}` 是字段但实例上没有"
    assert name in settings.model_dump(), …          ← 三条判据齐全
```
✓✓ **正是我上轮建议的可复用断言，三条判据一条不少**（含 `model_dump()` 那条决定性的）。

**查出的 6 个我认同分类**：
| 类型 | 键 | 判断 |
|---|---|---|
| **只写不读的死写入** | `pass_session_id` `quiet` `skills_preload` `verbose` | **删掉正确** —— 写了没人读，比裸属性更无害但也更无用；`verbose` 改成如实说明也对（**没有实现就不要假装有开关**） |
| **靠巧合生效** | `safe_mode` `ignore_rules` | **声明成真字段正确** —— 它们"能跑"只因裸属性恰好进了 `__dict__`，**这正是 `reasoning_level` 那一族** |

---

## 与目标标志的差距

**⬜ 0**，离目标还差 **16 条 🟡 + 4 条 ⚫**（与上轮相同）：

```
🟡 16：1.7 1.13b 4.3 4.8 4.9 4.12 5.2 5.14 6.4 10.14
       12.5 12.10 13.4 13.5 14.8 15.4
⚫ 4 ：4.2 4.7 8.3 12.9
```

### 你问的：`settings.json` 是配置面还是越权面 —— **是配置面，你的倾向对；但那个 `hasattr` 守卫有个真 bug**

**① 是配置面，不是越权面** ✓
`settings.json` 就是配置文件，本地单用户；**能改它的人本来就能改环境变量、密钥文件、`~/.config`** —— 把它当越权面等于把威胁模型搞错。**真正的越权面是"远端能改它"**，而这里不是。

**② 但你的推理里有一个我没预料到的缺口**（A② 实测）：
你说「`hasattr` 那个守卫让**裸属性写不进去**（因为裸属性此刻还不存在），所以它反而**不会**制造新的裸属性」——**这句是对的**（我实测 `no_such_key` → `hasattr=False` → 不写）✓
**但 `hasattr` 对 property 也返回 True** → `setattr` 抛 `AttributeError` → **CLI 启动崩**。
→ **所以它不是"制造裸属性"的风险，是"配置里出现某个合法属性名就崩"的健壮性风险。** 这两件事不同，但都在同一行代码上。

**③ 修法（一句话，同时解决三件事）**：
```python
# 现在
if hasattr(self.settings, key):
    setattr(self.settings, key, value)
# 建议
if key in type(self.settings).model_fields:      # 只写声明字段
    setattr(self.settings, key, value)
```
- 挡住裸属性（已挡）
- **挡住 property（修 bug）**
- **语义正好是"只有声明过的字段可写"** —— 而这正是你想要的语义

**④ 顺带一个未来风险（现在不构成问题，但值得先写一句）**：
如果将来 `Settings` 加了**敏感字段**（`allow_*`、`skip_approvals`、`hardline_in_machine` 之类），**`settings.json` 就从"配置面"变成"提权面"** —— 因为任何能写这个文件的东西都能关掉审批。
**建议现在就在 `context.py` 那处加一句注释**：「这里只写 `model_fields` 里的字段；**新增敏感字段时必须显式决定它是否允许从 `settings.json` 覆盖**」。**一行注释，挡的是未来的一次误判。**

---

## 风险与漂移

### R1 🟠 **`app/cli/context.py:110-111` 的 `hasattr` 放行 property → `setattr` 崩（A②，本轮新发现）**

`Settings` 有 2 个只读 property（`machine_wide` / `file_scope_label`）。`settings.json` 里出现任一键名 → CLI 启动 `AttributeError`。
**危害**：配置里写错/写多一个键 → **CLI 起不来，且报错是 `AttributeError` 而不是"这个键不能改"**。
**修法**：`hasattr` → `key in type(settings).model_fields`。

### R2 🟢 **本轮加固质量高，而且两条建议都"超额落地"**

- 清单断言 → 性质断言：**遍历并集**（我建议的）+ **反证测试**（我没提的）→ **"声明"与"实现"也被钉住了**
- 裸属性全扫：**`assert_real_settings_field` 抽成可复用函数，三条判据齐全**
- **`_is_declared` 的判据修正**（property 算、裸属性不算）写进了 docstring，`machine_wide` 那个误报有据可查

### R3 🟡 **`_model_copy_literals` 仍用正则（小提示，非缺陷）**

AST 只覆盖 `settings.X = ...`；`model_copy(update={...})` 走正则。**形状极窄 + 4 条测试兜着，不构成问题** —— 但如果以后 `model_copy` 的写法变体增多（换行、变量代替字面量），这条会静默漏。**建议加一条防空转断言**（"至少扫到 N 处 model_copy"），与你已有的习惯一致。

### R4 🟡 **轮末 `git status` 0 条目**

```
轮初 git status: 0 条目 ✓
轮末 git status: 0 条目 ✓
```
连续第三轮两头干净 → 所有计数类读数可信（pytest 3541 一次对上）。

### R5 🟡 **未验证项（如实标注）**

| 项 | 为什么没验 |
|---|---|
| `test_stop_reasons_are_actually_produced_by_the_policy` 的**红路径实跑** | 要往 `STOP_REASONS` 加一项 → 未做；验证的是"三种驱动 + 集合差"结构必红 |
| `settings.json` 真含 `machine_wide` 时 CLI 的实际行为 | 我实测了 `hasattr`+`setattr` 的行为（抛错），**但没有真写一个 `settings.json` 起 CLI 看崩溃**（那会改状态） |
| 4 个死写入删除后**是否真没有别处读它们** | 只核了 `Settings` 层；未全仓扫 `pass_session_id` / `quiet` / `skills_preload` 的读点 |
| `12.10` 的 `enroll`、`4.12` 的 19 家逐家 | 要网络/凭证 → 未跑 |
| `orbit chat` 交互、`gateway start`、`cron run`、`backup create/restore`、`config edit`、`doctor --fix/--live` | 会改状态或真发请求 → 未运行 |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用（连续 19 轮如此） |

---

## 下一轮建议

按优先级：

1. **【P0】修 `app/cli/context.py` 的 `hasattr` → `key in model_fields`**（R1）：一句话，修的是"配置里多一个键 → CLI 起不来"。**同时挡住裸属性与 property，语义正好是"只有声明字段可写"。**
2. **【P0】在 `context.py` 那处加一句"敏感字段需显式决定是否允许从 `settings.json` 覆盖"的注释**（B④）：**一行注释挡未来的一次误判** —— 现在不是问题，但 `Settings` 一旦加 `allow_*` 类字段就立刻是。
3. **【P1】5.2 懒加载（你下轮计划）**：`ensure_loaded` 零调用 → 直接影响每轮 schema 体积，**5.11 的 4,000 token 预算挂在这条上**。
4. **【P1】给 `_model_copy_literals` 加防空转断言**（R3）：与你已有的 `assert calls` / `assert len(hermes) >= 70` 同一习惯——**扫到 0 处就该红**。
5. **【P1】把"死写入"这一族也做一条守卫**：你这轮手工查出 4 个（写了没人读）。**它可机械化**：AST 扫出 `settings.X = ...` 的写入点，再扫 `getattr(settings, "X")` / `settings.X` 的读取点，**写入无读取 → 报出来**。**与裸属性守卫同一套基础设施，成本很低。**
6. **【P2】`4.12` 照 `4.2` 的 `BEDROCK_LIMITATIONS` 逐家写**：现在"19 家 4 家可用"读者不知道另外 15 家是"没做"还是"缺条件"。
7. **【P2】把"源码扫描类测试的假阳性源"立成清单**：你已经踩过两次（`get_model()` 的注释、`model_copy` 的 docstring 示例与事件 payload 字符串）。**清单：注释 / docstring / 字符串字面量 / 事件 payload 的键名 / 测试里的样例数据** —— **五个来源，前三个用 AST 能挡，后两个要靠"只认语法节点"**。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 22 轮未核。`nodes.py:254 context_meta.setdefault("compressed", {})` 仍是空占位。

---

*本报告由 monitor 角色只读生成。**基线一次对上**（pytest **3541**，轮初轮末 `git status` 均 0 项）。三项点名：`safe_mode`/`ignore_rules` **三条判据全过**（含 `model_dump()` 那条决定性的），`machine_wide` 是 property 故不该被误报——**你的判据修正正确**；`test_stop_reasons_are_actually_produced_by_the_policy` **能红**，且 `session_takeover` 的反证在 `test_takeover_makes_the_original_task_yield` 里（**两个产出方各有一条反证，覆盖完整**）。**AST 盲区那条我实测出一个真 bug**：`app/cli/context.py:110` 的 `hasattr` 对 **property 也返回 True**，`settings.json` 里出现 `machine_wide` 会让 `setattr` 抛 `AttributeError`、**CLI 启动崩**（`Settings` 有 2 个只读 property）。关于你的问题：**`settings.json` 是配置面不是越权面，你的倾向对**（真正的越权面是"远端能改它"）；但 `hasattr` 换 `key in model_fields` 能一行修掉上述 bug，**语义正好是"只有声明字段可写"**。两条建议你都**超额落地**：清单断言改并集之外还加了反证（把"声明与实现"也钉住），`assert_real_settings_field` 三条判据齐全。未验证项单列，不混进完成项。*
