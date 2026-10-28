# 对标进度报告

**更新时间**：2026-10-28 17:35（本机实测时钟 `Get-Date`，只读取证）
**本轮轮次**：**N = 15**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑会改状态或真发请求的命令（边界动作：为验证 browser 层可用性，尝试启动了一次浏览器控制器，失败即止；为验证 exporter 可用性，实例化了三个 exporter 对象）。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `a565c6f`；**轮初工作树干净，轮末 3 项在飞**）

---

## 总体完成度

### **170 / 181 项 = 93.9% 严格 · 96.0% 可验证**（✅ 166 + 🟢 4）

| 项 | 说明 |
|---|---|
| 五态 | **✅ 166 · 🟢 4 · ⚫ 4 · 🟡 7 · ⬜ 0**（合计 181 ✓） |
| 严格 / 可验证 | **93.9%** / **96.0%** —— **与上轮完全相同**（你说的"数字没动"成立 ✓） |
| 与 `matrix_stats --check` | 逐字一致，exit 0 ✅ |
| 结构 / 状态列 | 181 行全合法，零异常 ✅ |
| 必备段落 | 4 ⚫ 全含 `实现：`+`缺：`；4 🟢 全含 `差异：`+`代价` ✅ |

**这轮没有数字可报，但实质有三处变化**（一处修好了、两处我查出还差半步）——见 A②、A④。

### 硬口径

| 项 | 我的实测 | 判定 |
|---|---|---|
| ruff `All checks passed!` | **All checks passed!**（exit 0） | ✅ 一致 |
| mypy **171** files clean | **171 files clean**（exit 0） | ✅ 完全一致 |
| pytest 1 failed, 3468 passed, 3 skipped | **1 failed / 3468 passed / 3 skipped（302.93s）** | ✅ 一致（你 319.97s） |
| 唯一失败 = 既有 `test_connectors::test_browser_append_and_reload` | 正是它 | ✅ 一致 |
| `matrix_stats --check` exit 0 | exit 0 | ✅ 一致 |
| （本轮你未报 REGISTRY，我实测供参考） | **81 顶层 / 549 合计；done 379 · partial 25 · todo 145**（上轮 368/25/156 → **done +11**） | 供参考 |

---

## 分层完成度

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | **16 ✅ + 1 🟢** / 18 | 94.4% | 🟡1（1.13b） |
| **L2 Agent 核心循环** | **10 / 10** | **100%** | ✅ |
| **L3 上下文装配** | **8 / 8** | **100%** | ✅ |
| L4 模型适配 | 9 ✅ + **2 ⚫** / 13 | 69.2% | 🟡2 |
| L5 工具层 | 18 / 19 | 94.7% | 🟡1（5.14） |
| **L6 工具集 / 注册** | **5 / 5** | **100%** | ✅ |
| L7 Skill / curator | **17 ✅ + 1 🟢** / 18 | 100% | ✅ |
| L8 MCP | 9 ✅ + **1 ⚫** / 10 | 90.0% | — |
| **L9 记忆层** | **16 / 16** | **100%** | ✅ |
| L10 持久化 / 会话 | **14 ✅ + 1 🟢** / 15 | 100% | ✅ |
| **L11 cron** | **6 / 6** | **100%** | ✅ |
| L12 Gateway / 多平台 | 8 ✅ + **1 ⚫** / 11 | 72.7% | 🟡2 |
| L13 插件 | 9 / 10 | 90.0% | 🟡1（13.4） |
| L14 安全 / 审批 | **9 ✅ + 1 🟢** / 10 | 100% | ✅ |
| **L15 可观测性** | **6 / 6** | **100%** | ✅ |
| **L16 Web UI / 前端** | **6 / 6** | **100%** | ✅ |
| **合计** | **170 / 181** | **93.9%** | — |

**十层 100%**：L2、L3、L6、L7、L9、L10、L11、L14、L15、L16。

---

## 本轮抽查证据

**抽样率**：你点名的 4 项**全部复核**；2 处交代复核 2 处；另**实机试了 3 件事**（三个 exporter 的 `available()`、浏览器能否启动、chromium 二进制是否在）。

### A. 你点名的 4 项

**① 四个发射点是否真在热路径 → ✅✅ 全部在，而且"每次尝试 / 每次调用"都对**

| 位置 | 事件 | 所在函数 | 判定 |
|---|---|---|---|
| `nodes.py:715` | `pre_llm_call` | `ask_model`（def `:681`） | **热路径** ✓ 载荷含 `{"task_id", "attempt": attempts, "model"}` —— **`attempt` 字段本身就是"每次尝试"的证据** |
| `nodes.py:724` | `post_llm_call` | `ask_model` | **热路径** ✓ 紧跟 `result = await self.stream_model(...)` 之后 |
| `nodes.py:1202` | `pre_tool_call` | `run_tool`（def `:1173`） | **热路径** ✓ 在 `job = asyncio.ensure_future(...)` **之前**，载荷 `{"task_id","tool","args"}` |
| `nodes.py:1229` | `post_tool_call` | `run_tool` | **热路径** ✓ 紧跟 `result = job.result()`，载荷含 `result_chars` |

**2 个在 `ask_model`（每次尝试）+ 2 个在 `run_tool`（每次调用）** —— 与你的说法逐条对上，**没有一个是冷分支**。
**另外四处都包了 `try/except` + `log.warning`**，注释写明「钩子是旁路，旁路出问题不该让主循环死——但也不能静默」✓ —— 这个取舍是对的：钩子出错不能拖垮对话，但必须留痕。

**② `observability_sink.attach()` 的 available() 过滤 → ⚠️ `available()` 是真的，但「跳过理由」是空壳**

**先说好的部分（`available()` 真调、真返回）**：
```
jsonl       available=True
langfuse    available=False
nemo_relay  available=False
```
`observability_sink.py:52 usable = bool(exporter.available())` —— **真调，不是常量** ✓
`:53-54` 连 `available()` 自己抛异常都兜住（"available() 抛错：…"）✓
`:24` 「**不静默丢弃**：`describe()` 里列进 `skipped` 并给原因」——意图正确 ✓

**但接线错位了**：
```python
:59  why = exporter.describe().get("reason") or "available() 返回 False"
```
而 exporter 的 `describe()` 实际返回的键是：
```
['name', 'kind', 'available', 'endpoint', 'missing_env', 'spool', 'note']   ← 没有 reason
```
→ **`get("reason")` 永远是 `None`，所以永远走 fallback `"available() 返回 False"`**。

**结果**：用户看到 `skipped={"langfuse": "available() 返回 False"}`，而**真实原因被丢掉了**——它其实写在另外两个键里：
```
missing_env: ['HERMES_LANGFUSE_PUBLIC_KEY', 'HERMES_LANGFUSE_SECRET_KEY']
note:        '本机无网络，远端上报不可用。事件已 spool 到本地，联网后可用 `orbit observability flush` 补发。'
```
**所以回答你的问题**：`available()` 返回 False **是真的**（缺 env 是硬证据），**但"跳过理由"这段接线是坏的**——它找的键名与 exporter 提供的对不上。**这与 13.8 / 13.4 同族：机制真、数据真、展示那一层断线。**
**修法一行**：`why = describe().get("reason") or describe().get("note") or (f"缺 {', '.join(missing_env)}" if missing_env else "available() 返回 False")`。

**③ `test_jsonl_exporter_lands_a_real_line` → ✅ 与你自述完全一致（确实没断言 schema）**

```python
attach({"jsonl": exporter})
DEFAULT_HOOKS.emit("pre_llm_call", {"task_id": "t3", "attempt": 1})
exporter.flush()
assert files                                  # 有文件
assert lines                                  # 非空
first = json.loads(lines[0])                  # 能解析成 JSON
assert "pre_llm_call" in json.dumps(first)     # 含事件名
assert "t3" in json.dumps(first)               # 含 task_id
```
**它证明了"钩子 → exporter → 落盘"这条链路端到端通**，而且 `json.loads` 顺带证明了行是合法 JSON ✓
**没断言的是**：字段结构（`event`/`payload` 的 schema）、时间戳、exporter 名。**这个边界是合理的**——这条测试的职责是"链路通不通"，schema 该由 exporter 自己的单测管。**你的自述准确，不需要补。**

**④ 5.14 的三桶分类 → ⚠️ 分类判断对，但有两处要修正**

**先说对的部分**：把 61 条分成「需外部条件 52 / 刻意不做 1 / **可做但未做 4**」，并且**拒绝把 `kanban_*` 算进"刻意不做"**（理由：Hermes 的看板带 `request_review`/`block`/`heartbeat`/`link` 的多人协作协议，`todo` 不等价）——**这个拒绝是对的**。把不等价的东西算进"刻意不做"，正是我们防的"🟢/⚫ 变垃圾桶"。

**修正一：`BrowserController` 现有方法里没有 `go_back` / console / dialog**
```
现有方法：close, current_host, execute, last_screenshot_data_url, locate, login,
         login_status, new_page, notion, observe, open, open_session,
         page_signature, settle, start, validate_url, verify_entry, wait_for_change
```
→ **`browser_back` / `browser_console` / `browser_dialog` 不是"用现有 controller 就能做"**，都要给 controller 加方法（playwright 支持，但代码不存在）。`browser_get_images` 部分可行：`last_screenshot_data_url()`（`:428`）能拿到截图 data URL，但"取页面上的图片"要 DOM 查询，不是截图。
**这不改变"可做但未做"的归类**（确实是缺口、确实该做），只是**工作量比"就能做"大**。

**修正二（更要紧）：本机 chromium 起不来，所以这 4 个做完也只能标 ⚫，不能标 ✅**
我实机试了：
```
BrowserController(settings, store).start()
→ BrowserSessionError: 浏览器启动失败，请确认当前 Windows 桌面可用，并重试打开；未切换到无头模式
（强制 settings.headless=True 再试：同样失败）
```
**而二进制是装了的**：`~/AppData/Local/ms-playwright/` 下有 `chromium-1228`、`chromium-1234`、`chromium_headless_shell-1228/1234` ✓
→ 所以不是"缺 chromium"，是**本会话没有可用桌面 / 启动被环境挡住**。

**这条推论很重要**：这 4 个工具写完后，**"实现有、本机验不了"→ 按我们的两问判据应标 ⚫，不是 ✅**。
**🟡 →（写完）→ ⚫，不会到 ✅** —— 除非换一台有桌面的机器跑一次。**如果写完直接标 ✅，那就是下一处假绿。**

### B. 你交代的两件事

**① 脚本把自己绊倒（先替换文本、又拿被替换掉的原文当锚点）→ 记法对**

你总结的两条我都同意，补一个更通用的形式：**"改 A 又拿 A 当锚点"本质是「同一轮里对象被换掉了」**。同类还有：
- 先 `flush` 再 `read` 同一个缓冲
- 先 `rm` 再用路径 `stat`
- 先重命名再用旧名找

**共同判据**：**操作序列里，后续步骤引用的标识符必须在后续时刻仍然指向同一个对象。** 拿不准就"先全定位、再统一改"（你给的第一条）。

**② 门禁第三次抓到写门禁的人 → 这条门禁的价值已经超额兑现**

你在 4.8 / 5.14 证据栏里写 `🟢` 这个字符指代「有意差异」，整行出现第二个状态符号 → 被拦。
**这三次（第 12 轮 10.4 的「从 🟡 变 ✅」、第 14 轮的 `🟢` 指代、本轮同类）全是同一个原因：状态符号是"值"，但也被当"词"用。**
**建议把这条写成注释固定在 `matrix_stats.py` 的状态符号定义旁**：「这些字符在本文件里是**取值标记**，不得在证据栏里当普通词使用；要提及时写中文名（"已实现"/"有意差异"/"部分"/"未开始"）。」——这样下一个人写之前就能看到，而不是被拦了才知道。

---

## 与目标标志的差距

**⬜ 0**，离目标只差 **7 条 🟡 + 4 条 ⚫**：

| 类别 | 条目 |
|---|---|
| **🟡 7 条** | `1.13b`(原生壳) · `4.8`(iron-proxy，已写"本阶段决定不做") · `4.12`(19 家 4 家) · `5.14`(33/94，三桶已分) · `12.5`(20/22) · `12.10`(enroll 要网络) · `13.4`(wired 14 / exposed 1 / unsupported 4) |
| **⚫ 4 条** | `4.2`(bedrock) · `4.7`(proxy OAuth) · `8.3`(真 OAuth) · `12.9`(peer) |

### 你问的：`register_memory_provider` 两套要不要合并 —— **同意不合并；但"算不算 wired"取决于读写两条都通**

**先回答"要不要合并"：同意不合并，而且理由比你给的更强一条。**

你说"合并会把已经验证过的三段闸搅进去"——对，但可以更硬地说：**`memory_write.py` 的三段闸（威胁扫描 → 审批三态 → 快照二次扫描）是目前唯一被逐条验证过的安全边界；把 `MemoryRouter` 这个未验证的抽象合并进去，等于在一个已验证的边界上开一个未验证的入口。** 这违反一条更基本的原则：**不要在已验证的边界上引入未验证的组件**——哪怕它"看起来是同类的"（这与你这轮 `security_audit` vs `osv` 的教训是同一条：形状相同 ≠ 同一件事）。

**再回答"算不算真 wired"：给三条硬要求，缺一条就不是 ✅。**

| # | 要求 | 为什么 |
|---|---|---|
| 1 | **写路径**：`memory_save` 过完三段闸、写进 MEMORY.md **之后**，真的把同一条内容写进插件 provider | 只注册进表 = 13.4 那个假绿的翻版 |
| 2 | **读路径**：`MemoryRouter` 的检索/注入**真的会问插件 provider** | **只写不读 = 半条**。用户写了记忆但模型永远检索不到，比没写更糟（"我明明记过"） |
| 3 | **失败隔离**：插件 provider 抛异常不能让 `memory_save` 失败，但**必须记账**（事件 + 计数） | 旁路的定义；但静默失败等于第二条假绿 |

**所以我的判据是**：
- **写读都接 + 失败隔离** → **✅**（"不合并"是设计决定，可在证据栏注明，不必单列 🟢——因为它没有"代价"，只是实现选择）
- **只接写、读不问插件** → **🟡**，证据栏写「写侧已接、读侧未接：`MemoryRouter` 检索路径尚未查询插件 provider」

**最后点一个你方案里没提的风险**：不合并 = **两套记忆、两个真相源**。如果 MEMORY.md 写成功、插件 provider 写失败（或反过来），记忆就分叉了。建议把**写顺序与取舍**写进证据栏，否则下一个人会以为是双向同步：

> **MEMORY.md 是权威，插件 provider 是镜像。顺序：先 MEMORY.md（过三段闸）→ 成功后镜像写入 → 镜像失败只记账、不回滚。**
> **为什么不回滚**：回滚 MEMORY.md 会让用户丢掉刚写进去的记忆，比"镜像不一致"代价大得多；而不一致可以在下次 `memory sync` 时收敛。

---

## 风险与漂移

### R1 🟠 **`observability_sink` 的跳过理由是空壳（A②）——本轮新发现**

`describe().get("reason")` 与实际键名（`missing_env` / `note`）对不上 → 永远走 fallback。**用户看到的"原因"等于没有原因**，而真实原因（缺哪两个 env、本机无网络、事件已 spool、联网后可 flush 补发）全都现成地躺在 `describe()` 里。**一行可修。**

### R2 🟠 **5.14 的 browser 桶：做完只能到 ⚫，不能到 ✅（A④）**

chromium 二进制在，但**本会话启动失败**（headful 与 headless 都试过）。所以这 4 个工具写完后是"实现有、本机验不了" → **⚫**。
**这条要提前说清**，否则下一轮很容易把"写完了"记成 ✅ —— 那正是这轮你亲手抓到的那种假绿。

### R3 🟡 **两处"就能做"的说法偏乐观（不改变归类，只影响排期）**

`browser_back` / `browser_console` / `browser_dialog` 需要给 `BrowserController` **加方法**，不是"用现有就能做"。**归类不变（真缺口），但人日估计要上调。**

### R4 🟡 **轮末 3 项在飞**

```
轮初 git status: 0 条目 ✓
轮末 git status: 3 条目
  M backend/app/browser.py        ← 正是 5.14 browser 桶在动
  M backend/debug.log             ← 日志文件被提交跟踪了？建议加 .gitignore
  ?? backend/.tmp/                ← 临时目录，建议清理
```
**`backend/debug.log` 被跟踪**这件事值得处理：日志文件进版本库会持续产生噪声 diff（本轮它就出现在改动里）。

### R5 🟡 **未验证项（如实标注）**

| 项 | 为什么没验 |
|---|---|
| `12.10` 的 `enroll` 真流程 | 要网络 + 真凭证 → 未跑 |
| `13.4` 的 wired 14 / exposed 1 逐条 | 只核了 `register_hook` 那条（你本轮修的主线），其余未逐条复核 |
| `4.12` 的 19 家里 4 家可用 | 只核了数字来源，未逐家验 |
| `orbit chat` 交互、`gateway start`、`cron run`、`backup create/restore`、`config edit`、`doctor --fix/--live` | 会改状态或真发请求 → 未运行 |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用（连续 10 轮如此） |

---

## 下一轮建议

按优先级：

1. **【P0】修 `observability_sink.py:59` 的键名**（R1）：改成 `reason` → `note` → `missing_env` 三级回退。**一行，但它决定"跳过原因"是有用信息还是废话。**
2. **【P0】给 5.14 的 browser 桶预设结论**（R2）：写完后标 **⚫**（`实现：` + `缺：` 有桌面的机器跑一次），**不要标 ✅**。**提前写清，比事后纠正好。**
3. **【P1】`register_memory_provider` 按三条硬要求接**（见上）：写路径 + **读路径** + 失败记账。**只接写侧就标 🟡 并写明"读侧未接"。** 同时把"MEMORY.md 权威 / provider 镜像 / 不回滚"的取舍写进证据栏。
4. **【P1】把状态符号的"取值标记"性质写进 `matrix_stats.py` 注释**（B②）：它已经三次抓到写门禁的人，说明**光有门禁不够，还要在写之前就知道规则**。
5. **【P1】`backend/debug.log` 加进 `.gitignore`**（R4）：日志进版本库会持续产生噪声 diff，还会让"轮末有改动"这件事失去信号价值。
6. **【P2】13.4 剩下的 `register_memory_provider` 之外，还有 exposed 1 条**：收掉之后 L13 到 100%，**十一层 100%**。
7. **【P2】4.12 建议逐家写「能/不能 + 缺什么」**（上轮提过）：现在"19 家 4 家"读者不知道另外 15 家是"没做"还是"缺条件"。照 `4.2` 的 `BEDROCK_LIMITATIONS` 格式。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 13 轮未核。`nodes.py:254 context_meta.setdefault("compressed", {})` 仍是空占位——而这轮 `nodes.py` 刚好在改（发射点），**顺手看一眼就能收**。

---

*本报告由 monitor 角色只读生成。数字与上轮完全相同（93.9% / 96.0%），但实质有三处：**四个钩子发射点全部真在热路径**（2 个在 `ask_model` 每次尝试、2 个在 `run_tool` 每次调用）；**`observability_sink` 的跳过理由是空壳**——`get("reason")` 与实际键名 `missing_env`/`note` 对不上，永远走 fallback，真实原因（缺哪两个 env、已 spool、可 flush 补发）全丢；**5.14 的 browser 桶分类对，但 chromium 在本会话启动失败**（二进制在，headful 与 headless 都试过），所以那 4 个写完只能标 **⚫ 不是 ✅**。`test_jsonl` 与你自述一致（未断言 schema，合理）。关于 `register_memory_provider`：**同意不合并**（理由加强为"不在已验证的边界上引入未验证组件"），但**算不算 wired 要看读路径**——只接写侧就是 🟡。未验证项单列，不混进完成项。*
