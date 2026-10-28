# 对标进度报告

**更新时间**：2026-10-28 15:28（本机实测时钟 `Get-Date`，只读取证）
**本轮轮次**：**N = 12**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑会改状态或真发请求的命令。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `21b1c60`；**轮初工作树干净，轮末 7 项在飞改动**）

---

## 总体完成度

### **160 / 181 项 = 88.4% 严格 · 88.9% 可验证**（✅ 158 + 🟢 2）

| 项 | 说明 |
|---|---|
| 五态 | **✅ 158 · 🟢 2 · ⚫ 1 · 🟡 20 · ⬜ 0**（合计 181 ✓） |
| 严格完成度 | **88.4%** = (✅+🟢) / 181 |
| 可验证完成度 | **88.9%** = (✅+🟢) / (181−1) —— **两个数都打印了** ✅（我上轮指出的 `:209` 条件分支已去掉） |
| 与 `matrix_stats --check` | 逐字一致，exit 0 ✅ |
| 格式合法性 | 181 行状态列全合法；无第二个状态符号；**⚫ 12.9 含 `实现：` 与 `缺：` 两段**；两条 🟢 仍含 `差异：`+`代价` ✅ |

### 硬口径：**全部一致（这次 ruff/mypy 也对上了）**

| 你的口径 | 我的实测 | 判定 |
|---|---|---|
| `REGISTRY` 81 / 420 / 47 = 548；done 354 / partial 28 / todo 166 | **81 / 420 / 47 = 548**；**354 / 28 / 166**（连跑两次一致） | ✅ 完全一致 |
| pytest 1 failed, 3078 passed, 1 skipped | **1 failed / 3078 passed / 1 skipped（284.56s）** | ✅ 一致（你 279.23s） |
| 唯一失败 = 既有 `test_connectors::test_browser_append_and_reload` | 正是它 | ✅ 一致 |
| ruff `All checks passed!` | **All checks passed!**（exit 0） | ✅ **一致** |
| mypy **162** files clean | **162 files clean**（exit 0） | ✅ **完全一致** |
| `matrix_stats --check` exit 0 | exit 0 | ✅ 一致 |

> **上轮那条建议生效了**：我这次**在轮初先跑 ruff/mypy**（不等 pytest 跑完 284s），读数与你的完全对上。上轮的 30 errors 差异确实是"轮中工作树被写"造成的——**顺序一改，问题就消失了**。

---

## 分层完成度

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | 14 / 18 | 77.8% | 🟡4 |
| L2 Agent 核心循环 | 8 / 10 | 80.0% | 🟡2 |
| **L3 上下文装配** | **8 / 8** | **100%** | ✅ |
| L4 模型适配 | 9 / 13 | 69.2% | 🟡4 |
| L5 工具层 | 18 / 19 | 94.7% | 🟡1 |
| **L6 工具集 / 注册** | **5 / 5** | **100%** | ✅ |
| L7 Skill / curator | **17 ✅ + 1 🟢** / 18 | 100% | ✅ |
| L8 MCP | 9 / 10 | 90.0% | 🟡1 |
| **L9 记忆层** | **16 / 16** | **100%** | 🎉 本轮补齐 |
| L10 持久化 / 会话 | **12 ✅ + 1 🟢** / 15 | 86.7% | 🟡2 |
| **L11 cron** | **6 / 6** | **100%** | ✅ |
| L12 Gateway / 多平台 | 8 ✅ + **1 ⚫** / 11 | 72.7% | 🟡2 |
| L13 插件 | 8 / 10 | 80.0% | 🟡2 |
| L14 安全 / 审批 | 8 / 10 | 80.0% | 🟡2 |
| **L15 可观测性** | **6 / 6** | **100%** | ✅ |
| **L16 Web UI / 前端** | **6 / 6** | **100%** | ✅ |
| **合计** | **160 / 181** | **88.4%** | — |

**六层 100%**：L3、L6、L7、L9、L11、L15、L16（L7/L9 计入 🟢）。
**最弱**：L4 模型适配 69.2%（4 条 🟡）、L12 72.7%（2 🟡 + 1 ⚫）。

---

## 本轮抽查证据

**抽样率**：你点名的 6 项**全部复核**；3 处交代复核 3 处；另做了 1 次**过度声称的追查**（13.8）。

### A. 你点名的 6 项

**① 1.7 `orbit chat` 的 flag → ✅✅ 数量与行为都成立**

`orbit chat --help` 实测 **33 个 flag 条目**（含短/长分列；逻辑选项约 30 个，与你说的 30 一致）。

**两个关键 flag 我逐个查了行为，都不是"接受但没用"**：

| flag | 声明 | 实现证据 |
|---|---|---|
| `-t/--toolsets` | 「只开这些工具集」 | `chat.py:266-274` 校验 → `state["toolsets"] = resolve(wanted, platform="cli")`；`:351-370` 注释明写「`--toolsets` 走的是'**从注册表里摘掉**不属于这些工具集的工具'，而不是'只给模型看…'」，并按 `toolset_of(name)` 过滤后打「只保留 N 个工具」 |
| `-s/--skills` | 「预载技能名（可重复），**正文注入 Context 层**」 | 声明本身就是你要的答案：**Context 层，不是 Stable 层** ✓ |

**② 13.3 插件工具/钩子进宿主 → ✅✅ 两处 sink 都真接**

```python
# app/main.py
:174  from app.hooks import DEFAULT_HOOKS
:177  app.state.plugin_host = PluginHost.from_settings(
:178      settings, tool_sink=registry, hook_sink=DEFAULT_HOOKS
:180  app.state.plugin_host.attach_to_app(app)
:186  app.state.extra_tools = register_extra_tools(registry, data_dir=settings.data_dir)
```
`tool_sink=registry`（→ `ToolRegistry`）、`hook_sink=DEFAULT_HOOKS`（→ 进程级钩子单例）**都是真的** ✓；你交代的第 2 条（补 `register_extra_tools`）也在 `:186` ✓。
**`describe()` 的诚实设计值得记**：`mounted` 不是常量，而是查 `MOUNTS` 现算——「没挂过的插件报 `state=partial` / `mounted=False`，并把'要挂上去还缺什么'逐条列出来——一个'挂上了但 404'的 dashboard 插件比一个'明确说没挂'的更难查」✓

**③ 13.8 → ⚠️ 本轮唯一一条过度声称（详见 R1）**

**机制与测试都真**：`build_router()` 先 `validate()` → `_import_api` → 逐条 `functools.wraps` 包 try/except（**保签名、放行 HTTPException**）→ 重建 `APIRouter` → `include_router(prefix="/api/plugins/<name>")`；`tests/test_dashboard_mount.py` **11 条**（我 collect 确认 11），其中 `test_build_router_makes_plugin_routes_reachable` 确实让 `/api/plugins/<name>/…` 返回 **200**。

**但「真的挂上了」这句在生产里不成立**：
```
$ grep plugin_dashboard|mount_into_app|build_router  app/main.py
  （零命中）
```
而且**模块自己说了五遍**它没被调用：
```
plugin_dashboard.py:46   「``app/main.py`` 是禁改区，所以**仓库里目前没有任何地方调用它**」
plugin_dashboard.py:645  「app/main.py 是禁改区，没有地方 include 这个 router，所以本页面当前没有…」
plugin_dashboard.py:739  「（禁改区），所以除 mount() 被显式调用过的插件外，**一条路由都没挂上**」
plugin_dashboard.py:1019 「``main.py`` 是禁改区，所以本函数**目前没有生产调用方**」
```
测试挂的是**测试自己的 `FastAPI()`**（`test_dashboard_mount.py:109 app = FastAPI()`），不是真 app。

→ **判定：13.8 应降 🟡**（机制✅ + 测试✅，但生产未接线，真 app 里 `/api/plugins/…` 会 **404 而不是 200**）。**更讽刺的是**：`main.py` 这轮**已经被改过**（13.3 的 `PluginHost` 就加在里面），所以"禁改区"这个理由**已经不成立**了——现在完全可以顺手加一行 `plugin_dashboard.mount_into_app(app)`。

**④ 12.9 的 ⚫ → ✅✅ 首个合法 ⚫，两段都成立（这是本轮我最认可的一条）**

| 要求 | 实测 |
|---|---|
| **`实现：`** | ✅ `app/peer_client.py::PeerClient`：`PROTOCOL` 表（`:66`）+ 5 个端点常量（`/peer/health`、`/peer/dm`、`/peer/run`、`/peer/stop`、`/peer/status`，`:46-54`）+ `import httpx`（`:41`）+ 可注入 `transport`（`:153`，注释说「本机无网络，注入 `httpx.MockTransport` 才能验证」）+ 4 个错误类（`PeerUnsupported` / `PeerUnavailable` / `PeerResponseError` / `PeerError`）；`peer run`/`stop` **已从占位换成真实现** |
| **`缺：`** | ✅ **`app/main.py` 对 `peer` 零命中** → Orbit **没有 `/peer/*` 服务端**。客户端自己也在 `RUN_CONTRACT` 里写着「对端需要提供 `<base>/peer/run` 与 `<base>/peer/stop`」 |

→ **「写好了但没法验」成立**：客户端真实现（可 MockTransport 单测），但端到端要两台 Orbit 互发，本机没有服务端，**一次也没跑通**。**这是我上轮要求的那种形状，没有掺水。**

**⑤ 9.16 `use()` 报代价 / `install` 拒绝覆盖 → ✅✅ 两条都写死在模块契约里**

`app/profile_dist.py`：
```
:46  「2. **`install` 绝不覆盖正在生效的那套**。生效中的 profile 被就地覆盖 = 用户当前这次…」
:49  「3. **`use` 必须报代价**。切换不是改一个字符串：配置项、画像、技能清单会同时变。」
:172 错误类 docstring 含「删生效中的那套」作为可预期失败之一
```
另外它的 `active` 设计也诚实：`active` 的真值只有 `profiles/active` 一行，**不存两份**（「写两个地方必然会在某次崩溃后分叉」）✓

**⑥ 5.11 的 `test_tool_budget_floor.py` → ✅✅ 真的是关系，不是变相写死**

```python
# tests/test_tool_budget_floor.py
:14  1. 全量工具的 token 数 > 4,000（预算确实装不下）
:15  2. 压完描述之后**仍然** > 4,000（所以 over_budget 是对的，不是懒得压）
:18  以后工具变多/变少，数字会变，但这三条关系不会变。**关系才是契约，数字不是。**
:88  assert 0 < floor <= full
:90  assert floor > BUDGET * 0.5        ← 第三条：地板占全量 > 50%
:122 assert abs(claimed - floor) <= floor * 0.35   ← 额外守卫：docstring 里的「约 N」与实测地板差太远就报错
```
全部从 `registry` fixture **现算**（`_tokens(json.loads(json.dumps(spec)))`），**没有任何硬编码数字** ✓✓
**而且比我建议的多了一条**：`test_docstring_does_not_claim_a_different_floor` —— 直接把"文档里的数字会不会漂"也钉住了。**这正好治了 4,935 那个病的根因。**

### B. 你主动交代的三件事

**① 摘掉 `peer run`/`stop` 的占位 → ✅ 正确且必要**

你的理由我完全同意，而且它比"补一个功能"更重要：**占位在真实现出现时让位，否则它从"诚实"变成"挡住真话"**。
`register_handler` 的重复登记门禁会让先登记的赢——所以占位不撤，真实现永远接管不了，用户看到的永远是「未实现」。**这是"诚实"变成"永久错误"的机制。**

**② 补 `register_extra_tools(registry)` → ✅ 已核实（`main.py:186`）**

理由也对：「不补的话覆盖率报告会说'覆盖了'、模型却看不见。**那种不一致比没做更糟。**」——这与 13.8 是同一类问题（**报告说通了、实际没通**），只是这次你在自查时抓到了。

**③ PowerShell 吃掉反引号 → ✅ 第 3 次了，建议升级成硬规则**

你这次的 ` ``app.hooks`` ` → `pp.hooks`，ruff 不报（注释不参与解析），只能靠 grep 看出来。
**我这边同族的问题也复现过**：`-Encoding utf8NoBOM` 在这个 shell 里不存在（报"无法匹配枚举名"），导致我的探针脚本静默没写出来；最后改用 `[System.IO.File]::WriteAllText` + `UTF8Encoding($false)` 才行。
**建议把这条写进 `handoff/` 的方法论段，不只是聊天记录**：「Windows 上写含反引号/中文/特殊编码的 Markdown 或注释，一律走脚本文件（`WriteAllText` + `UTF8Encoding($false)`），不要走 `python -c` / `Set-Content`。」

---

## 与目标标志的差距

**⬜ 0 · ⚫ 1**，离目标只差 **20 条 🟡**：

```
1.13 1.14 1.16 1.17    ← L1（desktop / gui / TUI / 【元指标行】）
2.7  2.8               ← L2（压缩重试 / per-thread 中断）
4.2  4.7  4.8  4.12    ← L4（bedrock / proxy-OAuth / 代理 / voice）
5.14                   ← L5（94 工具覆盖 26/94）
8.3                    ← L8（真 OAuth 流程）
10.4 10.13             ← L10（压缩阈值 / sessions 19 子命令）
12.5 12.10             ← L12（剩 5 平台 / gateway 剩余）
13.4 13.9              ← L13（懒发现 / 其余）
14.4 14.5              ← L14（egress 代理 / 脱敏出口）
```

### 关于你问的「1.17 元指标行该不该拿掉」——我的判断

**该拿掉，但理由要更准，而且必须同时处理一个副作用。**

**先确认你的观察**：1.17 现在确实在 20 条 🟡 里，内容是「548 条里有真实现的比例（354/548）」。

**为什么该拿掉（三条，比"分子分母混在一起"更具体）**：

1. **它永远不可能变 ✅**：除非 `done == total`（那意味着 548 条命令全部实现，包括 19 个占位平台和 internal 命令）。它会在 🟡 里**永久占一格**，把"20 条真实工作"稀释成"19 条真工作 + 1 条永远做不完"。
2. **它自己是分母的一部分，又随分子漂**：181 里含它一行，而它的值又是"548 里 354"——**同一张表里出现了两个不同分母**（181 与 548），读者极易把它们混起来。
3. **它根本不需要人工登记**：它是 `REGISTRY.parity_rows()` 的**派生量**，`matrix_stats` 现算就行。人工维护一个"自动算得出来的数"是纯风险（且它这轮已经从 83/411 漂到 354/548 好几次）。

**⚠️ 但拿掉有一个必须一起处理的副作用**：

```
现在：160 / 181 = 88.4%
拿掉后：160 / 180 = 88.9%
```
**完成度会因为"少了一条不可能完成的条目"而自动涨 0.5 个百分点**——这不是进展，是分母变了。如果不写明，**下一个人会把它读成"进度涨了"**。

**所以我的建议是三件一起做**：
1. **把 1.17 移出 181 计数**，放进 MATRIX 的「元指标」附段（或干脆只由 `matrix_stats` 输出「命令面实现率 354/548」）。
2. **在同一轮里记一行口径变更日志**：`2026-10-28：条目 181 → 180（移除元指标行 1.17，它不是可完成的工作项）`。**没有这行，就是一次无声的完成度上抬。**
3. **顺手检查还有没有同类**：我扫了一遍 181 条，**只有 1.17 是"比率行"**；但 `1.16`（"上述 411 条里…"）如果也是实现率口径，要一并确认它不是第二条元指标（我看到的 1.16 是「TUI（终端 UI）」，属实体条目，没问题）。

---

## 风险与漂移

### R1 🟠 **13.8 过度声称：MATRIX 说"路由真的挂上了"，生产里一条没挂（本轮最重要）**

| 维度 | 实情 |
|---|---|
| 机制 | ✅ `build_router()` / `mount()` / `mount_into_app()` 全部实现且设计严谨（validate → import → wraps 包 try/except 保签名 → include_router） |
| 测试 | ✅ `tests/test_dashboard_mount.py` **11 条**，`/api/plugins/<name>/…` 在**测试 app** 上真返回 200 |
| **生产接线** | ❌ **`app/main.py` 零命中** `plugin_dashboard` / `mount_into_app` / `build_router` |
| 模块自述 | ❌ 五处写「`main.py` 是禁改区…没有任何地方调用它」「一条路由都没挂上」 |

**判定：13.8 应为 🟡**（"机制+测试完成，生产未接线"），当前 ✅ 会让读者以为 `orbit dashboard` 能跑插件页面——实际会 404。

**而且"禁改区"这个理由已经过期**：这轮 `main.py` 明明被改了（13.3 的 `PluginHost` 就加在里面）。**既然能加 13.3，就能加 13.8**——建议一并补上那一行，然后把 docstring 里五处"禁改区"改掉。

### R2 🟡 **三处过期 docstring（同一类，且其中两处会误导判断）**

| 位置 | 过期内容 | 现状 |
|---|---|---|
| `plugin_dashboard.py` ×5 | 「`app/main.py` 是禁改区…没有生产调用方」 | `main.py` 本轮已被改（13.3 接线） |
| `session_plane.PlaneSink.attach()` | 「不能声称已经接上」（上轮已提，**仍未改**） | 已真接（`main.py:127`） |
| `dashboard.main_routes()` | 「只解析 `@app.<method>` 装饰器」 | 实现同时扫 `@app.` 与 `@router.` |

**这三处的共同危害**：下一个人读代码会得出**与事实相反**的结论（"没接"→ 其实接了；"只扫一处"→ 其实扫两处）。**成本各只有一行。**

### R3 🟡 **轮末 7 项在飞改动（本轮基线取自轮初，不受影响）**

```
轮初 git status: 0 条目 ✓
轮末 git status: 7 条目
  M app/cli/commands/security.py  app/graph/nodes.py  app/proxy.py  app/security_audit.py
  M handoff/hermes-parity/MATRIX.md
  ?? app/compaction.py  app/oauth_callback.py
```
→ 与你说的"下轮先做 14.5 / 4.7 / 2.7"完全对得上（`security.py` / `proxy.py` / `nodes.py` / `compaction.py` / `oauth_callback.py`）。**所有硬口径取自 `HEAD 21b1c60`。**

### R4 🟡 **未验证项（如实标注）**

| 项 | 为什么没验 |
|---|---|
| 1.7 的 33 个 flag **逐个**验证行为 | 只深核了 `-t/--toolsets` 与 `-s/--skills` 两个（你点名的）；其余 31 个只核了"存在" |
| 13.8 的 try/except 隔离是否真只 500 自己那条 | 只读了 `build_router` 的包装逻辑与测试名，**没有实跑一个会抛异常的插件** |
| 9.16 的 `use()` 代价文案与 `install` 拒绝的**实际输出** | 只核了模块 docstring 与错误类（契约层），**未跑 CLI** |
| `orbit chat` 交互、`gateway start`、`cron run`、`backup create/restore`、`config edit`、`doctor --fix/--live` | 会改状态或真发请求 → 未运行 |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用（连续 7 轮如此） |

---

## 下一轮建议

按优先级：

1. **【P0】13.8 降 🟡，或把 `plugin_dashboard.mount_into_app(app)` 真加进 `main.py`**（R1）：机制和 11 条测试都是真的，缺的只是那一行——而"禁改区"这个理由这轮已经被你自己否掉了。**如果选择接线，请顺手删掉模块里那五处"禁改区"注释**，否则文档会继续与事实相反。
2. **【P0】拿掉 1.17 时，同轮记一行口径变更日志**：`181 → 180，移除元指标行`。**否则完成度会从 88.4% 无声跳到 88.9%**，被读成进展。（理由见上节三条 + 副作用。）
3. **【P1】修三处过期 docstring**（R2）：`plugin_dashboard` ×5、`PlaneSink.attach()`（上轮提过仍未改）、`main_routes()`。各一行成本，但都会让下一个读代码的人判断反了。
4. **【P1】剩下 20 条 🟡 里，L4（4 条）是最弱一层**：4.2 bedrock / 4.7 proxy→OAuth / 4.8 / 4.12。其中 **4.7 与 4.8 你下轮已在做**（`proxy.py` / `oauth_callback.py` 已新建），建议一次把 L4 收口到 100%，那样六层 100% 会变成七层。
5. **【P1】12.5 的 5 个平台值得重新判一次 🟡 vs ⚫**：如果适配器是"HTTP 出站写了但没凭证"→ 该是 ⚫；如果是"占位"→ 🟡。**按你上轮采纳的两问判据，这一条要逐平台看**（`implemented=False` 的是占位 = 🟡）。
6. **【P2】把 5.11 的 `test_tool_budget_floor.py` 当模板推广**：它做了三件事——**断言关系不断言数字**、**从 fixture 现算**、**守卫 docstring 里的数字不漂**。这三条正好治"引用未钉住的测量值"这个反复出现的病（10.5 / 5.11 / 早前的 3.6）。建议任何"证据栏里出现具体数字"的条目都配一条这样的测试。
7. **【P2】把 PowerShell 反引号坑写进 `handoff/` 方法论**（你第 3 次踩）：规则一句话——Windows 上写含反引号/中文/特殊编码的 Markdown 或注释，走脚本文件 + `UTF8Encoding($false)`，不走 `python -c` / `Set-Content`。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 10 轮未核。本轮顺带看到 `nodes.py:139 context_meta.setdefault("compressed", {})` 仍是空占位——正是那 15 条之一，而下轮你要做 2.7（压缩重试），**正好一并解决**。

---

*本报告由 monitor 角色只读生成。你点名的 6 项里 **5 项完全撑得住**（1.7 / 13.3 / 12.9⚫ / 9.16 / 5.11）、**1 项（13.8）过度声称**——机制与 11 条测试都真，但 `main.py` 零引用，真 app 里会 404。**12.9 是我上轮要求的那种合法 ⚫：`实现：` 与 `缺：` 两段都经得起查**（客户端真写、`/peer/*` 服务端确实不存在）。**ruff/mypy 这次因为轮初先跑而对上了**——顺序一改，上轮那 30 errors 的差异就消失了。关于 1.17，我的判断是**该拿掉，但必须同轮记口径变更日志，否则完成度会无声上抬 0.5 个百分点**。未验证项单列，不混进完成项。*
