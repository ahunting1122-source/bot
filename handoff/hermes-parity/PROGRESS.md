# 对标进度报告

**更新时间**：2026-10-28 14:35（本机实测时钟 `Get-Date`，只读取证）
**本轮轮次**：**N = 11**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑会改状态或真发请求的命令。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `028aa8f`；**轮初工作树干净，轮末 10 项在飞改动且被持续写入，见 R1**）

---

## 总体完成度

### **151 / 181 项 = 83.4%**（✅ 149 + 🟢 2）

| 项 | 说明 |
|---|---|
| 五态 | **✅ 149 · 🟢 2 · ⚫ 0 · 🟡 30 · ⬜ 0**（合计 181 ✓） |
| 严格完成度 | **83.4%** = (✅+🟢) / 总数 |
| 可验证完成度 | **⚫ = 0 → 与严格口径相同（83.4%）** |
| 与 `matrix_stats --check` | 「181 项 · ✅149 · 🟢2 · ⚫0 · 🟡30 · ⬜0」exit 0 —— **与我独立解析逐字一致** ✅ |
| 格式合法性 | 181 行状态列全合法；**无第二个状态符号**；**两条 🟢 都含 `差异：` 且都含「代价」** ✅（上轮 7.17 的建议已落地） |

**⚫ = 0 这个数字本身就是结论**：30 条 🟡 里**没有一条**属于"做完了但本机验不了"，都是能力本身还缺。**所以完成度没有被环境限制压低** —— 你上轮担心的"环境限制算不算完成"，在当下这个快照里**还不构成问题**（但见 R2 的判据问题，它会在真有 ⚫ 时立刻变关键）。

### 硬口径

| 你的口径 | 我的实测 | 判定 |
|---|---|---|
| `REGISTRY` 81 / 410 / 47 = 538；done 345 / partial 27 / todo 166 | **81 / 410 / 47 = 538**；**345 / 27 / 166**（连跑两次一致） | ✅ **完全一致** |
| pytest 1 failed, 2859 passed, 1 skipped | **1 failed / 2859 passed / 1 skipped（271.29s）** | ✅ 一致（你 251.99s） |
| 唯一失败 = 既有 `test_connectors::test_browser_append_and_reload` | 正是它 | ✅ 一致 |
| ruff `All checks passed!` | **轮中实测 30 errors**，但**全部落在 3 个轮中正在改的文件** | ⚠️ **见 R1：不可采信，非回归** |
| mypy **155** files clean | **156 files clean** | ⚠️ +1 = 轮末新增未跟踪的 `peer_client.py` |
| `matrix_stats --check` exit 0 | **exit 0** | ✅ 一致 |

**ruff 差异的精确归因**（我查到了底）：30 个错误全部落在 `app/platforms.py` / `app/plugin_dashboard.py` / `app/providers.py` —— **这三个文件都在轮中 `M`（已修改）清单里**，没有一个落在未改动的文件上。
而且我第一眼看到的 `B017 Do not assert blind exception` 在 `tests/test_chat.py:103`，**几分钟后已被改成 `pytest.raises(CliError)`，注释还写着「ruff B017 拦得对」**——文件 mtime `14:33:56`，就在我巡检期间。**所以这不是回归，是工作树正在被写。**

---

## 分层完成度

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | 13 / 18 | 72.2% | 🟡5 |
| L2 Agent 核心循环 | 8 / 10 | 80.0% | 🟡2 |
| **L3 上下文装配** | **8 / 8** | **100%** | ✅ |
| L4 模型适配 | 9 / 13 | 69.2% | 🟡4 |
| L5 工具层 | 16 / 19 | 84.2% | 🟡3 |
| **L6 工具集 / 注册** | **5 / 5** | **100%** | 🎉 本轮补齐 |
| L7 Skill / curator | **16 ✅ + 1 🟢** / 18 | 94.4% | 🟡1 |
| L8 MCP | 8 / 10 | 80.0% | 🟡2 |
| L9 记忆层 | 14 / 16 | 87.5% | 🟡2 |
| L10 持久化 / 会话 | **12 ✅ + 1 🟢** / 15 | 86.7% | 🟡2 |
| **L11 cron** | **6 / 6** | **100%** | ✅ |
| L12 Gateway / 多平台 | 8 / 11 | 72.7% | 🟡3 |
| L13 插件 | 6 / 10 | 60.0% | 🟡4 |
| L14 安全 / 审批 | 8 / 10 | 80.0% | 🟡2 |
| **L15 可观测性** | **6 / 6** | **100%** | ✅ |
| **L16 Web UI / 前端** | **6 / 6** | **100%** | 🎉 本轮补齐 |
| **合计** | **151 / 181** | **83.4%** | — |

**五层 100%**：L3（8/8）、L6（5/5）、L11（6/6）、L15（6/6）、L16（6/6）。
**最弱三层**：L13 插件（60%，4 条 🟡）、L4 模型（69.2%，4 条 🟡）、L1 入口（72.2%，5 条 🟡）。

---

## 本轮抽查证据

**抽样率**：本轮 14 条转 ✅ 中**复核 7 条（你点名的全部）**；3 处交代复核 2 处；另做了 2 次**实机攻击验证**（真造 junction、真跑 token 预算）。

### A. 你点名的 7 条

**① 10.1 写侧统一 → ✅✅ `attach()` 是真实例属性遮蔽，`nodes.py` 一处未改**

```python
# app/session_plane.py
:1270  original = store.event
:1275  async def event(task_id, kind, **payload):     # 包装：先写会话库，再写平面
:1291  store.event = event                            ← 实例属性遮蔽
:1294  store.event = original                         ← 返回的卸载函数
# 防重复挂载
if getattr(original, "_orbit_plane_sink", False): raise SessionPlaneError("这个 store 已经挂过 PlaneSink")
```
**调用点真的接了**（`app/main.py` lifespan）：
```python
:124  from app.session_plane import PlaneSink, SessionPlane
:126  app.state.plane_sink = PlaneSink(SessionPlane(settings.data_dir), app.state.store)
:127  app.state.plane_detach = app.state.plane_sink.attach()
```
**`nodes.py` / `agent.py` 里一个字都没改**：`nodes.py` **26 处** `await self.store.event(...)` + `agent.py` **8 处**，全部走 `self.store.event` → 自动进平面 ✓✓
（旁注：`attach()` 的 docstring 里还留着「本轮只能把它做成'一行就能接上'的钩子，**不能声称已经接上**」——**这句已过期**，建议更新，否则下一个人会以为没接。）

**② 14.1 审批接线 → ✅✅ `VERDICT_ASK` 独立分支，无 TTY 不挂死**

```python
# app/tools.py
:2024  async def _approve_command(...)
:2053      if verdict.verdict == approvals.VERDICT_ASK:      ← 单独分支
:2060          # 通道选择与超时语义都在 `approval_channel` 里：无 TTY / 无人值守一律…
:2063          decision = approval_channel.approve_or_refuse(
```
**「无 TTY 是 deny 而不是挂死」这条我逐行核了 `approval_channel.py`**：
- `ConsoleChannel`：`_is_tty()` 为假时**立刻** `return Decision.timed_out_at(...)`（`:436`），注释写明「`orbit gateway` / cron / CI 里…**或者干脆永久阻塞（看起来像"agent 挂了"）**。两者都是错的，所以先判 TTY」
- `AutoDenyChannel`：**一律 deny**（`:581`），理由「无人值守时"没人回答"和"有人同意"在结果上必须不同」
- `resolve_channel()`：stdin 非 TTY → auto-deny；**未知配置值也落到 auto-deny（fail closed）**
- `Decision.outcome` 是**三态**（`allow`/`deny`/`timeout`），超时与拒绝的 `reason` 措辞不同且带等了多久 ✓

**③ 16.1 → ✅✅ covered 7 / missing 0 / unbacked {}，且真的扫两处**

实跑 `dashboard.report()`：`total: 18 | covered: 7 | partial: 11 | **missing: 0** | **unbacked: {}**` ✓✓
`main_routes()` 确实**同时扫两处**，而且 docstring 把理由写透了：
> 「两处来源：`main.py` 的 `@app.<method>` 与 `dashboard_api.py` 的 `@router.<method>`。少了第二处，`APIRouter` 上挂的路由对这个核对器就是隐形的——页面会被误报成 missing，而路由其实已经在跑。**核对器自己也得跟上实现，否则它只是一份过时清单。**」

（旁注：同一函数的 docstring 首段还写着「只解析 `@app.<method>` 装饰器——这是 Orbit 里唯一的声明形状」，与下面的实现**自相矛盾**，建议一并更新。）

**④ 5.2 AST 扫出 39 个工具 → ✅ 与真实工具表对得上**

```
toolsets.declared_tool_names() = 39
tool_catalog.orbit_pool()      = 39
```
两处独立来源都是 **39** ✓

**⑤ 6.2 四层过滤 → ✅✅ cron 真的摘掉 ask_user / clarify / todo（我第一次测错了）**

**先说我的错**：我第一次调 `resolve(names, "cron", {})`，结果 7 个工具一个没少，差点报"过滤没生效"。读了源码才发现 `resolve()` 的 docstring 明写：
> 「``context`` 留空 = **只有平台层生效**，与引入四层之前的签名与行为逐字一致。这是刻意的：`ToolRegistry.specs()` 每轮都走这条路，把它一次性改成四层会让所有既有调用点（以及几十条测试）一起漂」

**带 `context` 重测后，三层场景全部精确**：
```
scene=None      -> [ask_user, clarify, delegate_task, run_command, todo, vault_login, web_search]   （只平台层）
scene=cron      -> [delegate_task, run_command, vault_login, web_search]        ← 摘掉 ask_user/clarify/todo ✓
scene=subagent  -> [ask_user, clarify, run_command, todo, vault_login, web_search]  ← 摘掉 delegate_task ✓
scene=acp       -> [ask_user, clarify, delegate_task, run_command, todo, web_search] ← 摘掉 vault_login ✓
```
✓✓ 与 MATRIX 6.2 描述逐条一致。

**⑥ 14.8 `mklink /J` 目录联接 → ✅✅ 我真造了一个 junction，确认被拒**

```
$ cmd /c mklink /J <inner>\j <outside>     → rc=0（成功创建）
j.is_symlink()   = False        ← 正是 MATRIX 说的那个前提
j.is_junction()  = True
PathPolicy(allowed_roots=(inner,), base=inner).check("j/x.txt")
  → allowed=False, kind='symlink'
    reason='路径经过链接「…\inner\j」（符号链接或目录联接）：链接可以指向根目录之外，
            而前缀比较看不出这一点。要允许链接请显式设置 follow_symlinks=True。'
对照组 check("normal.txt") → allowed=True  ✓（无假阳性）
```
代码侧 `path_security.py:359-371` 的 `_is_link()` 同时判 `is_symlink()` **与** `is_junction()`（3.12 才有，用 `getattr` 兜底），并写明「只判符号链接，等于在 Windows 上把最容易做出来的那条逃逸路径漏掉」✓✓
**这是本轮最硬的一条证据：不是读代码推断，是把攻击真做出来看它拦不拦。**

**⑦ 5.11 的「4,935 token 地板」→ ⚠️ 四个数字里三个复现了，就这个不行（而且代码库里它有三个版本）**

我按 main.py 的方式登记工具组后实测：

| 数字 | MATRIX 5.11 声称 | 我实测 | 判定 |
|---|---|---|---|
| 工具数 | 38 | **38** | ✅ 精确 |
| 全量 token | 5,649 | **5,646** | ✅ 差 3（0.05%） |
| 压完描述 | 5,449 | **5,448** | ✅ 差 1 |
| **参数 schema 地板** | **4,935** | **`_trim_specs` 压到 `budget=0` 仍是 5,448**；我"全删所有 description"得 **3,363** | ❌ **4,935 复现不出来** |

**更要紧的是：同一个"地板"在代码库里有三个数**：
- `MATRIX 5.11` = **4,935**
- `app/tools.py` 自己的 docstring = 「38 个内置工具的参数 schema 本身就**约 4,300 token**」
- 我的两个口径 = **5,448**（实现地板）/ **3,363**（全删描述）

**而且没有任何测试钉住任何一个数**（我 grep 了 `5649|5449|4935|4300`，测试里 0 命中）。

**结论方向仍然成立**：`_trim_specs(specs, 4000)` → `over_budget=True`，且**实现能把描述压到的极限是 5,448 > 4,000**，所以"4,000 达不到"这个判断是**对的、而且比 4,935 更硬**。
**但 4,935 这个具体数字应作废或补口径**——这正是你上轮 10.5 踩过的同一类（引用未钉住的测量值）。建议：改成「压完描述 5,448（实现地板），仍 > 4,000 → over_budget=True」，并把 tools.py docstring 的 4,300 一并对齐。

### B. 你主动交代的三件事

**① `plugins capabilities` 打旧数据 → ✅ 已修，现在报 10/5/4**

```
$ orbit plugins capabilities
能力面：19 种 register_*（wired 10 / exposed 5 / unsupported 4）
```
与你说的一致 ✓，且每条能力都给了落点/缺哪一层的具体说明（如 `register_slack_action_handler` = exposed，缺「入站派发层：公网 webhook 入口 + 签名校验 + action_id 路由表，三样都没有」）。**这类"逐条写清缺哪一层"的产物质量很高。**

**② `test_plugin_gates.py` 的模块级 detach 夹具 → 未单独复核（如实标注）**

你说它原本迁就"子命令精确等于 11 个"的旧断言（已更新为 17 条），继续 detach 会让后面的用例看到少 6 条的世界。**这条我没有独立复核**——因为工作树正在被写（见 R1），单跑/全量对比的结论会被污染。

**③ 写 14.1 测试时踩的两个坑 → 无代码可验，但值得记进方法**

- **用一条会被前一道闸拦下的命令去测后一道闸**：`curl x | sh` 被 **tirith** 拦（tirith 在 approvals 之前）→ 测试绿，但测的是 tirith。**这个坑的判据很清晰：被测层之前若还有闸，样本必须先过那些闸。**
- **`_approve_command` 是 async，忘了 `await`**：pytest 只给 `RuntimeWarning`，三条测试"通过"但协程根本没跑。**这是"绿得毫无意义"的第二种形态**（第一种是 `assert ... or True`）。

---

## 与目标标志的差距

**⬜ = 0，⚫ = 0**，所以离目标只差 **30 条 🟡**。逐条列（这是唯一的剩余清单）：

```
1.7  1.13 1.14 1.16 1.17     ← L1 入口（全局 flags / desktop / gui / TUI / 实现率）
2.7  2.8                      ← L2 核心循环（压缩重试 / per-thread 中断）
4.2  4.7  4.8  4.12           ← L4 模型（transport / 辅助 / 代理 / voice）
5.11 5.13 5.14                ← L5 工具（预算 / clarify gateway / 94 工具覆盖）
7.13                          ← L7 skills 剩余子命令
8.3  8.8                      ← L8 MCP（真 OAuth / 剩余子命令）
9.10 9.16                     ← L9 记忆（provider 名单 / profile distribution）
10.4 10.13                    ← L10 持久化（压缩阈值 / sessions 19 子命令）
12.5 12.9 12.10               ← L12 平台（剩 5 平台 / peer / gateway 剩余）
13.3 13.4 13.8 13.9           ← L13 插件（dashboard 契约 / 懒发现 / 真挂载 / 其余）
14.4 14.5                     ← L14 安全（egress 代理 / 脱敏出口）
```

### 关于你问的「B 类到底该标 ⚫ 还是 🟡」——我的答案

**你的自测方向对，但判据少了一个必要条件，会漏掉"没做"被包装成"验不了"的情况。我的判据是两问，不是一问：**

| 问 1：实现存在吗？ | 问 2：本机能验证吗？ | 状态 |
|---|---|---|
| 有（代码写完） | 能 | ✅（有证据） |
| 有（代码写完） | 不能（缺网络/凭证/硬件） | **⚫** |
| **没有** | 换台机器**能**验 | **🟡（不是 ⚫）** |
| 没有 | 换台机器也不能 | **🟡**（能力缺，可能是 🟢 的候选） |

**关键差别**：你只问了「换一台有网机器能不能验证」，但**"能不能验证"和"实现在不在"是两件事**。一个能力**压根没写**，换什么机器都验不了——它必须留在 🟡，不能因为"理论上换机器能验"就升 ⚫。

**按这个判据回看你列的五条**：

| 条目 | 我的判断 | 依据 |
|---|---|---|
| **4.8** 令牌代换代理 | **看代码**：proxy 真写了（只是没凭证）→ ⚫；压根没写 → 🟡 | 需要看实现文件在不在 |
| **8.3** 真 OAuth 流程 | **🟡** | 上轮 MATRIX 8.3 的证据栏自己写的是「真 OAuth 流程（发现元数据 → PKCE → 本地回调 → 换 token）**未实现**」→ **没写**，所以不是 ⚫ |
| **12.5** 剩 5 平台 | **🟡** | 适配器是 `implemented=False` 的**占位**（`UnimplementedAdapter` 每个发送方法都抛）→ **没写** |
| **4.12** voice 18 家 | **🟡** ✓ | 与你的判断一致（能力本身缺） |
| **13.8** dashboard 真挂载 | **🟡** ✓ | 与你的判断一致 |

**所以：B 类不能整体归 ⚫，要逐条按"实现是否存在"判。** 你自测的五条里，**8.3 与 12.5 我判为 🟡**（证据栏自己写着"未实现"/"占位"）。

**再加一条防滥用门禁（和 🟢 的 `差异：` 同理）**：⚫ 的证据栏**必须同时含两段**——
- `实现：<文件:行>`（证明代码写完了）
- `缺：<网络 / 凭证 / 硬件 / 外部服务>`（说明差什么才能验）

**没有「实现：」的 ⚫ 一律打回 🟡。** 这条可以像 🟢 的第二道门禁那样做成机械检查（`⚫` 行缺 `实现：` 或 `缺：` 就报错）。**否则 ⚫ 会变成第二个垃圾桶——只是这次收的是"没做"。**

**另外一个小口径问题**：你说「`matrix_stats` 现在报两个数」，但我实测**只打印了一个**——因为 `:209 if actual["unverifiable"]:` 让第二个数**只在 ⚫ > 0 时**才打印。⚫=0 时两个数相同，但**读者看不到"双口径已实现"**。建议 ⚫=0 时也打印一行「可验证完成度 83.4%（⚫=0，与严格口径相同）」。

---

## 风险与漂移

### R1 🟠 **轮末工作树被持续写入，我的 ruff/mypy 读数不可采信（但已归因到具体文件）**

```
轮初 git status: 0 条目 ✓
轮末 git status: 10 条目（且仍在变）
  M app/cli/commands/chat.py  app/context.py  app/hooks.py  app/memory_providers.py
  M app/platforms.py  app/plugin_dashboard.py  app/providers.py  app/skill_hub.py
  M tests/test_chat.py
  ?? app/peer_client.py
```
**ruff 我实测 30 errors，但 30 个全部落在 `app/platforms.py` / `app/plugin_dashboard.py` / `app/providers.py` 三个 `M` 文件里**，无一落在未改动文件上 → **已提交基线（HEAD `028aa8f`）与你的 "All checks passed" 不矛盾**。
**佐证**：我第一眼看到的 `B017`（`tests/test_chat.py:103` 的 `pytest.raises(Exception)`）**在我巡检期间就被改成 `pytest.raises(CliError)`**，注释写着「ruff B017 拦得对」——文件 mtime `14:33:56`。
**mypy 156 vs 155**：+1 = 新增未跟踪的 `peer_client.py`。

**结论：本轮 ruff/mypy 只做了归因，没有实测到已提交基线。** 建议下轮在轮初**立刻**跑 ruff/mypy（而不是等 pytest 跑完 271s 之后再跑），否则永远拿到在飞状态的读数。

### R2 🟠 **5.11 的「4,935」不可复现，且代码库内有三套数字（见 A⑦）**

- 复现得住的：38 工具 / 5,646（≈5,649）/ 5,448（≈5,449）
- **复现不出的：4,935**；`tools.py` docstring 写「约 4,300」；我的实现地板是 **5,448**
- **无任何测试钉住这些数**
- **结论方向仍成立**（over_budget=True，实现地板 5,448 > 4,000），建议把 4,935 换成 5,448 并统一 docstring

### R3 🟡 **两处过期 docstring（会误导下一个人）**

| 位置 | 过期内容 | 现状 |
|---|---|---|
| `session_plane.PlaneSink.attach()` | 「本轮只能把它做成'一行就能接上'的钩子，**不能声称已经接上**」 | 已在 `main.py:127` 真接上 |
| `dashboard.main_routes()` | 「**只解析** `@app.<method>` 装饰器——这是 Orbit 里唯一的声明形状」 | 实现同时扫 `@app.` 与 `@router.` |

### R4 🟡 **未验证项（如实标注）**

| 项 | 为什么没验 |
|---|---|
| `test_plugin_gates.py` 的 detach 夹具（你交代的第 2 条） | 工作树正在被写，单跑/全量对比结论会被污染 |
| 4.8 / 4.12 / 13.8 的实现是否存在（⚫ vs 🟡 判定） | 只读了 MATRIX 描述，**没有逐条打开实现文件确认**；8.3 / 12.5 我按证据栏文字判为 🟡 |
| 本轮 14 条转 ✅ 中未抽查的 7 条 | 7 条已核 |
| `orbit chat` 交互、`gateway start`、`cron run`、`backup create/restore`、`config edit`、`doctor --fix/--live` | 会改状态或真发请求 → 未运行 |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用（连续 6 轮如此） |

---

## 下一轮建议

按优先级：

1. **【P0】⚫ 加第二道机械门禁：必须含 `实现：<文件:行>` 与 `缺：<条件>`**（R2 的延伸，也是你那个问题的答案）。现在 ⚫ 只有"不计入完成度"这一条规则，**没有"必须证明实现存在"的约束**——那它迟早会收进"没做但换机器也验不了"的条目。判据写成两问：**实现存在吗？本机能验吗？** 只有"存在 + 不能验"才是 ⚫。
2. **【P0】8.3 与 12.5 应该回到 🟡**：证据栏自己写着「真 OAuth 流程…**未实现**」和「19 个占位适配器」，那是"没写"，不是"验不了"。
3. **【P0】修 5.11 的 4,935**（R2）：换成可复现的 **5,448**（实现地板），并同步 `tools.py` docstring 的 4,300。顺手把这三个数写进一条测试钉住，否则下次还会漂。
4. **【P1】修两处过期 docstring**（R3）：`PlaneSink.attach()` 的"不能声称已经接上"、`main_routes()` 的"只解析 `@app.`"。**这两处都会让下一个读代码的人做出错误判断**，成本却只有两行。
5. **【P1】`matrix_stats` 在 ⚫=0 时也打印第二口径**（见 A 末尾）：写「可验证完成度 83.4%（⚫=0，与严格口径相同）」。否则"双口径"只存在于代码里，读者看不见。
6. **【P1】剩下 30 条 🟡 里，L13 插件（4 条）与 L1 入口（5 条）是最弱两块**：L13 缺 dashboard 契约/懒发现/真挂载（其中 13.8 是 🟡 但依赖 `plugin_dashboard.py`，**轮中正在改**）；L1 缺全局 flags、desktop/gui、TUI。
7. **【P2】把你这轮踩的两个测试坑写成两条断言级规则**（比文档更有效）：
   - 「被测层之前若还有闸，样本必须先过那些闸」——可以在 `test_approvals` 里加一条自检：样本命令在 `tirith.scan()` 上必须为 pass，否则 skip 并提示。
   - 「async 函数忘了 `await`」——加 `filterwarnings = error::RuntimeWarning` 到 pytest 配置，让"协程没跑"直接变红。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 9 轮未核。本轮顺带看到 `nodes.py:139 context_meta.setdefault("compressed", {})` 仍是空占位——正是那 15 条之一。

---

*本报告由 monitor 角色只读生成。本轮你点名的 7 条里 **6 条完全撑得住**（10.1 / 14.1 / 16.1 / 5.2 / 6.2 / 14.8，其中 14.8 与 6.2 我做了实机验证）、**1 条（5.11）的 4,935 不可复现但结论方向成立**。**我本轮自己错了一次**：6.2 我一开始没传 `context`，差点报"过滤没生效"——`resolve()` 的 docstring 明写留空只有平台层生效。关于 B 类该标 ⚫ 还是 🟡，我的答案是**按"实现是否存在"逐条判，8.3 与 12.5 应回 🟡**，并给 ⚫ 加一道"必须含 `实现：`"的机械门禁。未验证项单列，不混进完成项。*
