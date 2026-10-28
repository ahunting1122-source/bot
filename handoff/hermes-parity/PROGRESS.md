# 对标进度报告

**更新时间**：2026-10-28 19:34（本机实测时钟 `Get-Date`，只读取证）
**本轮轮次**：**N = 21**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑会改状态或真发请求的命令（边界动作：跑了 2 个测试文件 + 一次全量；实例化 `Settings` 做字段自检）。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `6c8c2e7`；**轮初 `git status` 0 条目，轮末 0 条目**）

---

## 总体完成度

### **160 / 181 项 = 88.4% 严格 · 90.4% 可验证**（✅ 155 + 🟢 5）

| 项 | 说明 |
|---|---|
| 五态 | **✅ 155 · 🟢 5 · ⚫ 4 · 🟡 17 · ⬜ 0**（合计 181 ✓） |
| 严格 / 可验证 | **88.4%** / **90.4%** —— 从 87.8% / 89.8% 上升 |
| 与 `matrix_stats --check` | 逐字一致，exit 0 ✅ |
| 结构 / 状态列 | 181 行全合法，零异常 ✅ |

### 硬口径：**全部一致，而且这次基线是干净的**

| 你的口径 | 我的实测 | 判定 |
|---|---|---|
| ruff `All checks passed!` | **All checks passed!**（exit 0） | ✅ 一致 |
| mypy **172** files clean | **172 files clean**（exit 0） | ✅ 完全一致 |
| pytest 1 failed, **3525** passed, 3 skipped | **1 failed / 3525 passed / 3 skipped（337.92s）** | ✅ **完全一致** |
| `matrix_stats --check` exit 0 | exit 0 | ✅ 一致 |
| `git status` 9 项（你报），全部本轮改动 | **我轮初 0 项、轮末 0 项** | ✅ **更干净** |

> **带 `git status` 条目数这个习惯立刻见效了**：你说"9 项全部是本轮改动、无在飞文件"，我这边读到时已经是 **0 项**（改动已提交成 `6c8c2e7`）。**两边的数字差不是矛盾，是时点差——而正因为你标了"9 项全部本轮改动"，我能立刻判断"没有在飞文件"，测试数 3525 一次对上。** 这比上轮那个 `+4` 强太多了。

---

## 分层完成度

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | 15 ✅ + 1 🟢 / 18 | 88.9% | 🟡2 |
| L2 Agent 核心循环 | 9 / 10 | 90.0% | 🟡1 |
| **L3 上下文装配** | **8 / 8** | **100%** | ✅ |
| L4 模型适配 | **8 ✅ + 2 ⚫** / 13 | 76.9% | 🟡3（**4.4 转 ✅**） |
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

**抽样率**：3 项点名**全部复核**（3 项都实跑）；另**主动核了"仍未接通的 9 个事件"**（你下轮只计划 4 个，见下）。

### A. 你点名的 3 项 —— **3/3 全部撑得住，其中 2 项的设计比我预期的更严谨**

**① `test_no_get_model_call_site_forgets_the_argument` 能不能红 → ✅✅ 能红，而且它防住了"空转绿"**

```python
source = pathlib.Path(nodes_module.__file__).read_text(encoding="utf-8")
# **先剔掉注释行**：解释性注释里也会出现 `get_model()` 这种字面量，
# 第一版没剔，结果把一句「之前所有调用点都不传」的注释当成了真调用点。
code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
calls = re.findall(r"(?<![\"'])get_model\(([^)]*)\)", code)
assert calls, "没找到任何 get_model 调用点——测试失效了，先确认代码结构"      ← 防空转
bare = [args for args in calls if "reasoning_effort" not in args]
assert not bare, f"这些 get_model 调用没传 reasoning_effort：{bare}"
```
**把 `nodes.py` 任一调用点的 `reasoning_effort=` 删掉** → 该调用实参里不含这个字符串 → `bare` 非空 → **必红** ✓✓

**两处超出预期**：
- **`assert calls` 这条防空转守卫** —— 它防的是"regex 哪天停止匹配了，测试从此永远绿"。**这正是我们反复说的"守卫空转"，你主动加了。**
- **"先剔注释行"那段自陈** —— 你第一版把一句**注释里**的 `get_model()` 当成了真调用点。**这个坑很典型：源码扫描类测试的最大假阳性源就是注释和字符串。** 你不但修了，还把踩坑过程写进注释。

**还加了第二条我没想到的**：
```python
def test_cli_flag_writes_the_field_that_nodes_reads():
    """`--reasoning` 写的键名必须与 `Nodes` 读的键名一致。
    **两处写字符串常量是最容易漂的地方**：一个改成 `reasoning`、另一个还是
    `reasoning_level`，两边各自都对，合起来永远不生效。"""
```
→ **这条守的是"两端各自正确、合起来永不生效"**，而 4.4 的病根正是这个形状。**两条测试分别守住了"没人传值"和"键名不一致"两个变体**，比只写一条强得多。

**② `test_rejected_delegation_emits_neither` 是否真走 `_preflight` → ✅✅ 是，我核到了拒绝分支**

```python
# 测试侧
result = await runner.run(DelegateSpec(goal="太深了", depth=3))
assert result.ok is False
assert seen == [], f"被拒的委派不该发子 agent 事件，实际发了 {seen}"

# 实现侧（我核的）
delegate.py:70   DEFAULT_MAX_DEPTH = 2
delegate.py:65   # 父(0) → 子(1) → 孙(2)。第 3 层（depth=3）直接拒绝。
delegate.py:406  def _preflight(self, child_id, spec) -> DelegateResult | None:
delegate.py:416      depth = max(1, int(spec.depth or 1))
delegate.py:417      if depth > self.max_depth:      ← 拒绝分支
```
→ **`depth=3 > max_depth=2` → 走 `:417` 的拒绝分支** ✓✓ 而 `assert result.ok is False` **证明失败确实来自委派路径**（不是别的原因炸了）。

**这条守的东西我特别认可**：
> 「**别为了凑事件数而虚报**：拒绝发生在 `runner_factory` 之前，没有 span 可开，也就不该有 start。」

**这是"事件数"与"真实发生"分离的判据** —— 而你刚好在**推进事件数（6/17 → 8/17）的那一轮**加这条，**顺序是对的**：先立"不许虚报"的守卫，再往上加数字。

另加 `test_both_events_are_declared_as_emitted`（断言两个都在 `EMITTED_EVENTS`）✓

**③ `reasoning_level` 是否真进 `Settings.model_fields` → ✅✅ 真进了，我用了"序列化"作决定性判据**

```
reasoning_level in model_fields: True
  默认值: ''  | 注解: <class 'str'>
Settings(reasoning_level='high').reasoning_level = 'high'
model_dump() 里有没有: True          ← 决定性：裸属性会在这里丢
```
**并且我造了对照，验证这个判据确实能抓出那个病**：
```
s2 = Settings().model_copy(update={"not_a_field_xyz": 1})
  not_a_field_xyz 能用吗: 1                    ← 能读
  它在 model_fields 里吗: False                ← 但不在字段表里 → 序列化会丢
```
→ **「能用但 `model_fields` 查不到、`model_dump()` 会丢」正是你描述的那个病**，而 `reasoning_level` 现在三条全过 ✓✓

**所以判据要补一句（比"查 model_fields"更硬）**：**"读得到" + "在 `model_fields` 里" + "`model_dump()` 里有" 三条都要过。** 只查第一条会被裸属性骗过——**而这正是你第二次踩它的原因。**

### B. 你那个"11 个假 runner 回归"的判断，我完全同意，并想加一句

> 修的是假件不是产品——真实签名本来就有这个可选参数，假件过时会让「产品真的传了参数」变成测试报错。

**同意，而且这条判据可以独立成立**：**当"产品改对了"导致测试红，先问"红的是产品还是假件"**。
- 假件过时 → 改假件（本例）
- 产品真错 → 改产品

**判据一句话**：**真实签名是否本来就允许这个参数？** 允许 → 假件过时；不允许 → 产品越界。

---

## 与目标标志的差距

**⬜ 0**，离目标还差 **17 条 🟡 + 4 条 ⚫**：

```
🟡 17：1.7 1.13b 4.3 4.8 4.9 4.12 5.2 5.14 6.4 10.8 10.14
       12.5 12.10 13.4 13.5 14.8 15.4
⚫ 4 ：4.2 4.7 8.3 12.9
```

### 顺带：13.5 仍未接通的是 **9 个**，不是 4 个

我实测 `EMITTED_EVENTS` = **8/17** ✓（与你说的一致），但**差集是 9 个**：

| 你下轮计划的 4 个 | 我另查出的 5 个 |
|---|---|
| `pre_api_request` / `post_api_request` / `api_request_error` / `on_session_finalize` | `on_pre_compress` · `on_session_reset` · `pre_approval_request` · `post_approval_response` · `transform_tool_result` |

→ 补完那 4 个是 **12/17**，还剩 5。**其中 `pre_approval_request` / `post_approval_response` 值得单独看**：审批是 L14 的核心能力，**订阅方（插件）能挂审批前后钩子、但永远收不到**——这与"exporter 订阅收不到"是同一类问题，只是换了个订阅方。

### 你问的：4.4 vs `Command.hermes` 字段，这个取舍认不认同 —— **认同，而且我要给你一个更便宜的方案，让这个取舍不必存在**

**① 认同取舍，理由可以更硬一条**

你说的"4.4 是用户能感知的能力缺口，`hermes` 字段只是统计口径"——对。我再加一条：
> **4.4 这类是"沉默失效"**：用户传了 `--reasoning high`、**没报错、行为不变**。他甚至**无法报告这个 bug**——因为他不知道该怎么期待。而统计口径不准，**至少我们自己看得见**。

**判据一句话**：**"用户能不能感知" > "我们能不能看清"**。前者是产品缺陷，后者是仪表缺陷。

**② 但我要补一个前提（我核过了）**

`hermes` 字段缺失的**风险方向**是：`parity_rows()` 回落到命令名 → 所有条目看起来都"有 Hermes 对应" → **把 Orbit 自有的命令算进"对齐成果"**。
→ 也就是**完成度虚高**，**不是"漏掉缺口"**。
**虚高不会让你少做工作，只会让报告好看** —— 所以它确实可以排在能力项之后。**这个判断我支持。**

**③ 更便宜的方案：不必改 550 行字段，也不必排到"能力项清完之后"** ⭐

你的三步修里第 1 步是"每个 `Command` 要么写路径、要么显式 `ORBIT_ONLY` 哨兵"——**那是 550 行填充，工作量大，所以你觉得它是个独立项目。**
**但其实不需要动那个字段。** 因为 `Command.hermes` 本来就不是可用信号（`chat`/`config`/`cron` 全空），而**判"是不是 Hermes 有"只需要一份 Hermes 的命令名集合**——**这份集合已经在仓库里**：
```
research/_raw/cmd-*.txt      77 个（顶层 help）
research/_raw/sub/*.txt     295 个（二级 help）
```
→ **直接拿名字做集合差**：`name in hermes_names` → 对齐；否则 → Orbit 自有。**不改字段、不改回落、不动 550 行，只加一个只读统计。**

**成本可能是半小时，而不是一个独立项目。** 这样它就变成"能力项之间顺手做掉"，**你的取舍问题自动消失**。

**⚠️ 一个必须提前定的口径**：名字差只能判"**顶层命令名**是否在 Hermes 里"。子命令级（如 `sessions` 的 27 vs 19）**判不了**——那需要按父节点分组比。建议：**先只做顶层**（81 个顶层里有几个是 Orbit 自有？这个数立刻有用），子命令级的"多出来"继续留在 🟢 的 `代价：` 里。

---

## 风险与漂移

### R1 🟢 **本轮三条修复都有"防同类复发"的守卫，这是最重要的变化**

- `test_no_get_model_call_site_forgets_the_argument` → 守"没人传值"（4.4 的病）
- `test_cli_flag_writes_the_field_that_nodes_reads` → 守"两端键名不一致"
- `test_rejected_delegation_emits_neither` → 守"为凑事件数而虚报"
- 加上上轮的 `EMITTED_EVENTS` 告警 → 守"订阅了收不到"

**四条守卫分别覆盖了四种不同的失效形状**，而且都是**机制**（不是文档）。**这是这十几轮里质量最高的一轮。**

### R2 🟡 **13.5 的缺口是 9 个不是 4 个（见上表）**

下轮补 4 个 → 12/17，还剩 5。其中 `pre_approval_request` / `post_approval_response` 与审批层相关，**建议和 14.x 一起看**。

### R3 🟡 **`reasoning_level` 的判据要三条**（A③）

"读得到"不够，还要"在 `model_fields` 里" + "`model_dump()` 里有"。**这是你第二次踩裸属性**——**判据升级能防第三次。**

### R4 🟡 **轮末 `git status` 0 条目（干净）**

```
轮初 git status: 0 条目 ✓
轮末 git status: 0 条目 ✓
```
**本轮是罕见的"两头都干净"** —— 所以所有硬口径读数都可信（pytest 3525 一次对上就是证明）。

### R5 🟡 **未验证项（如实标注）**

| 项 | 为什么没验 |
|---|---|
| `test_no_get_model_call_site_forgets_the_argument` 的**红路径实跑** | 要删代码 → 未做；验证的是"regex 提取全部实参 + `assert not bare`"结构必红 |
| `--reasoning high` **端到端**真影响模型行为 | 要真调模型 → 未跑；只核了字段链路（CLI → Settings → Nodes → `get_model`） |
| `on_session_finalize` 与 4 个 API 级事件的**发射点位置** | 未核（下轮才做） |
| `12.10` 的 `enroll`、`4.12` 的 19 家逐家 | 要网络/凭证 → 未跑 |
| `orbit chat` 交互、`gateway start`、`cron run`、`backup create/restore`、`config edit`、`doctor --fix/--live` | 会改状态或真发请求 → 未运行 |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用（连续 16 轮如此） |

---

## 下一轮建议

按优先级：

1. **【P0】4 个 API 级事件（你的计划）+ 把 `pre_approval_request`/`post_approval_response` 排进去**：它们与审批层（L14）绑定，**订阅方挂上了却永远收不到**，与 exporter 那个缺口同族。一次排进去比下次再想起来省事。
2. **【P0】`orbit_only` 用"名字集合差"实现，不必改 `hermes` 字段**（见 B③）：`research/_raw/cmd-*.txt`（77 个）+ `sub/*.txt`（295 个）就是现成的名字集合。**先只做顶层**，成本约半小时，**你的取舍问题自动消失**。
3. **【P1】把 `reasoning_level` 的三条判据写进测试工具**（R3）：`读得到` + `in model_fields` + `in model_dump()`。**第二次踩了，值得做成一个可复用的断言函数**（`assert_real_settings_field(Settings, "x")`），以后加字段直接调。
4. **【P1】`test_rejected_delegation_emits_neither` 的模式值得推广**：**"事件数上涨的那一轮，先立'不许虚报'的守卫"**。13.5 还有 9 个事件要接，**每接一批都该有一条"这条路径不该发事件"的反证**。
5. **【P1】5.2 与 10.8 之间，建议先 5.2（懒加载）**：`ensure_loaded` 零调用是"机制在但没人用"，而懒加载直接影响**每轮请求的工具 schema 体积**（5.11 的 4,000 token 预算问题也挂在这条上）。**10.8 的 `LeaseStore.acquire` 影响面小得多。**
6. **【P2】`4.12` 照 `4.2` 的 `BEDROCK_LIMITATIONS` 逐家写**：现在"19 家 4 家可用"读者不知道另外 15 家是"没做"还是"缺条件"。
7. **【P2】把"静默降级四兄弟"落成一张检查表**：你已经把 ④ 做成机制（源码扫描测试），**①②③ 也各能做成断言**：①`getattr` 链路必须验字段存在 ②`load()` 必须有一条"喂错形状"的用例 ③`if None: skip` 的 skip 分支必须有"跳过计数为 0"的断言（`test_a_live_registered_process` 就是这么做的）。**四条都能机械化，不必停在文档。**
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 19 轮未核。`nodes.py:254 context_meta.setdefault("compressed", {})` 仍是空占位。

---

*本报告由 monitor 角色只读生成。**基线这次一次对上**：pytest **3525** 与你的完全相同——因为我轮初轮末 `git status` 都是 **0 条目**，而你也标了"9 项全部本轮改动、无在飞文件"。**带 `git status` 条目数这个习惯立刻见效。** 三项点名全部撑得住：`test_no_get_model_call_site_forgets_the_argument` **能红且加了 `assert calls` 防空转**（还自陈了"第一版把注释当调用点"的假阳性）；`test_rejected_delegation_emits_neither` **真走 `_preflight`**（`depth=3 > DEFAULT_MAX_DEPTH=2`，我核到 `:417` 拒绝分支）；`reasoning_level` **真进 `model_fields` 且 `model_dump()` 里有**（我还造了 `model_copy(update=...)` 裸属性的对照，证明这个判据抓得住）。关于取舍：**认同 4.4 优先**（"用户能不能感知" > "我们能不能看清"，且 4.4 是用户无法报告的沉默失效），但我给了一个**更便宜的 `orbit_only` 方案**——用 `research/_raw` 里现成的 77+295 个 help 文件名做集合差，**不改 550 行字段、成本约半小时**，你的取舍问题自动消失。另提醒：**13.5 差集是 9 个不是 4 个**，其中审批前后两个事件与 L14 绑定。未验证项单列，不混进完成项。*
