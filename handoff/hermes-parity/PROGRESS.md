# 对标进度报告

**更新时间**：2026-10-28 16:24（本机实测时钟 `Get-Date`，只读取证）
**本轮轮次**：**N = 13**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑会改状态或真发请求的命令（唯一边界动作：为验证 8.3 真 bind，起了一个回环端口的临时 HTTP 服务器并立即关闭，见 A④）。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `af265a0`；**轮初工作树干净，轮末 2 项在飞**）

---

## 总体完成度

### **167 / 180 项 = 92.8% 严格 · 94.4% 可验证**（✅ 164 + 🟢 3）

| 项 | 说明 |
|---|---|
| 五态 | **✅ 164 · 🟢 3 · ⚫ 3 · 🟡 10 · ⬜ 0**（合计 **180** ✓） |
| 严格完成度 | **92.8%** = (✅+🟢) / 180 |
| 可验证完成度 | **94.4%** = (✅+🟢) / (180−3) |
| 与 `matrix_stats --check` | 逐字一致，exit 0 ✅ |
| 口径变更 | **1.17 已移出，180 项**；`MATRIX:16` 有「口径变更日志」节，`:22` 写明「移除元指标行 `1.17`｜181 → **180**｜…一条**比率**，不是能力」✅ |
| 格式合法性 | 180 行状态列全合法；无第二个符号；**3 条 ⚫ 全含 `实现：`+`缺：`**；**3 条 🟢 全含 `差异：`+`代价`** ✅ |

> **口径变更这一条我特意核了两遍**：上轮我提醒"拿掉 1.17 会让完成度从 88.4% 无声跳到 88.9%"，你现在把它写进了 MATRIX 头部，并明说"是分母变小，不是进展"。**这是本轮我最认可的一处**——它把一次容易被误读的数字变化，变成了可审计的记录。

### 硬口径：**全部一致**

| 你的口径 | 我的实测 | 判定 |
|---|---|---|
| `REGISTRY` 81 / 420 / 48 = 549；done 368 / partial 25 / todo 156 | **81 / 420 / 48 = 549**；**368 / 25 / 156**（连跑两次一致） | ✅ 完全一致 |
| pytest 1 failed, 3299 passed, 3 skipped | **1 failed / 3299 passed / 3 skipped（308.95s）** | ✅ 一致（你 294.46s） |
| 唯一失败 = 既有 `test_connectors::test_browser_append_and_reload` | 正是它 | ✅ 一致 |
| ruff `All checks passed!` | **All checks passed!**（exit 0） | ✅ 一致 |
| mypy **167** files clean | **167 files clean**（exit 0） | ✅ 完全一致 |
| `matrix_stats --check` exit 0 | exit 0 | ✅ 一致 |

---

## 分层完成度

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | 15 / 17 | 88.2% | 🟡2 |
| **L2 Agent 核心循环** | **10 / 10** | **100%** | 🎉 本轮补齐 |
| **L3 上下文装配** | **8 / 8** | **100%** | ✅ |
| L4 模型适配 | 9 ✅ + **1 ⚫** / 13 | 69.2% | 🟡3 |
| L5 工具层 | 18 / 19 | 94.7% | 🟡1 |
| **L6 工具集 / 注册** | **5 / 5** | **100%** | ✅ |
| L7 Skill / curator | **17 ✅ + 1 🟢** / 18 | 100% | ✅ |
| L8 MCP | 9 ✅ + **1 ⚫** / 10 | 90.0% | — |
| **L9 记忆层** | **16 / 16** | **100%** | ✅ |
| L10 持久化 / 会话 | **14 ✅ + 1 🟢** / 15 | 100% | 🎉 本轮补齐 |
| **L11 cron** | **6 / 6** | **100%** | ✅ |
| L12 Gateway / 多平台 | 8 ✅ + **1 ⚫** / 11 | 72.7% | 🟡2 |
| L13 插件 | 9 / 10 | 90.0% | 🟡1 |
| L14 安全 / 审批 | 8 ✅ + 1 🟢 / 10 | 90.0% | 🟡1 |
| **L15 可观测性** | **6 / 6** | **100%** | ✅ |
| **L16 Web UI / 前端** | **6 / 6** | **100%** | ✅ |
| **合计** | **167 / 180** | **92.8%** | — |

**九层 100%**：L2、L3、L6、L7、L9、L10、L11、L15、L16。
**最弱**：L4 模型适配 69.2%（3 🟡 + 1 ⚫）、L12 72.7%（2 🟡 + 1 ⚫）。

---

## 本轮抽查证据

**抽样率**：你点名的 6 项**全部复核**；3 处交代复核 3 处；其中 **2 项做了实机验证**（8.3 真 bind、13.8 真 app 测试结构），**1 项我先找错了参数名并当场纠正**（10.13）。

### A. 你点名的 6 项 —— **6/6 全部撑得住**

**① 13.8 的真 app 测试 → ✅✅ 它真的能红，有两条独立失败路径**

```python
# tests/test_dashboard_real_app.py
:6    「但那些测试挂的是 **`FastAPI()` 测试自己的 app**，而 `app/main.py` 对…」
:9    「也就是说：**测试全绿，生产里 `/api/plugins/<name>/…` 会 404。**」   ← 上轮的结论被写进测试文档
:56   from app.main import create_app
:68   with TestClient(create_app(settings, run_agent=False)) as http:      ← 真 app
:81   assert response.status_code != 404, "真 app 上插件路由 404——说明 `main.py` 没有调 `mount_into_app(app)`"
:84   assert response.status_code == 200
:85   assert response.json() == {"pong": True}
:87   assert MOUNTS.as_dict()["mounted_plugins"] >= 1
:90   def test_main_py_actually_calls_mount_into_app():   ← 源码层第二条证据
:99   assert "mount_into_app" in source, "`main.py` 没有调用 `mount_into_app`"
```
**为什么它真能红**：判据 `!= 404` **带着点名原因的失败消息**，而 `:99` 的源码断言是**第二条独立路径**——把 `main.py` 三行注释掉，两条都会红。
`test_dashboard_real_app.py:17` 还有一处我很欣赏的区分：**「401 和 404 是两件事**：401 说明路由在（被鉴权挡住），404 说明根本没挂上」——这正好防了"用错的状态码把没挂当成鉴权问题"。

**接线三行 + 你新发现的坑**（`main.py:861-872`）：
```python
# **必须先 discover 再 mount**：`mount_into_app()` 读的是全局 `PAGES`，
# 而 `discover()` 只返回清单、**不往 `PAGES` 里填**。少了发现这一步，
# `mount_into_app` 会安静地挂 0 条路由——那种"挂上了但没内容"比 404 更难查。
found = plugin_dashboard.discover(settings.data_dir / "plugins")
for manifest in found: plugin_dashboard.PAGES.add(manifest)
app.state.plugin_pages = plugin_dashboard.mount_into_app(app)
```
**这个坑我没提到，你补得对**——而且它揭示了一类更普遍的错误形状：**"挂载成功但零内容"比"挂载失败"更难查**，因为返回值不报错、日志不报错、只有请求时才 404。

> ⚠️ 如实标注：我**没有实跑红路径**（那要注释掉 `main.py` 三行 = 改代码）。我验证的是"断言结构与失败消息证明它会红"，不是"我把它弄红过"。

**② 2.7 / 10.4 提前压缩 → ✅✅ 真的每轮开始判，`trigger_matches` 真的不是常量**

```python
# app/graph/nodes.py
:451  # ── 提前压缩（MATRIX 10.4 / 2.7）───────────────────────────
:452  # **每轮开始先判一次**，而不是等供应商报 `context_overflow`。差这一次往返
:453  # 的意义不只是省 token：溢出那一轮必然失败，用户看到的是报错，还要多等
:454  # 几百毫秒到几秒的重试。判据本身是纯函数（`app/compaction.py::plan`）。
:468  decision = compaction.plan(tail, window=window_tokens, carried_tokens=carried)
# app/compaction.py
:39   COMPACT_THRESHOLD = 0.50
:152  return used >= size * COMPACT_THRESHOLD
```
- **0.50 真在判据里**（`compaction.py:39/152`），`nodes.py:55 COMPACT_THRESHOLD_RATIO = 0.5` 只是给 `describe()` 的可解析锚点——注释自己说明了这一点，**没有假装它是判据**
- **每轮开始**：`:452` 明写，且位置在 `context`/`history` 构造之后、模型调用之前 ✓
- **`_trigger_matches` 是真函数不是常量** ✓：字符级两字滑窗找公共子串（`for index in range(len(goal)-1): if goal[index:index+2] in trigger: return True`），避免分词依赖
- **额外两处防御我很认可**：①脏 checkpoint（`folded > len(history)`）按"没折过"重来，而不是切出空历史；②摘要连续失败达 `COMPACT_SUMMARY_ATTEMPTS` 后这一轮不再花一次调用、直接走截断

**③ 4.2 的 ⚫ → ✅✅ 两组向量都是 AWS 官方材料，不是编的**

```python
# tests/test_proxy_stream.py
:401  AKID = "AKIDEXAMPLE"
:402  SECRET = "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY"
:403  AMZ_NOW = datetime(2015, 8, 30, 12, 36, 0, tzinfo=UTC)
```
这三行就是 **AWS 文档与 `aws-sig-v4-test-suite` 里公开的示例值**（不是真凭据，测试里也注明"不是真凭据"）✓

| 向量 | 内容 | 判定 |
|---|---|---|
| ① `test_sigv4_matches_the_aws_documented_iam_example` | AWS 文档「Signature Version 4 signing process」的 **IAM ListUsers** 示例；**三段分别断言**：canonical request **文本** / string to sign / 最终签名 `5d672d79c15b13162d9279b0855cfba6789a8edb4c82c400e06b5924a6f2b5d7`，再断言 Authorization 头与 host | ✅ 真官方值 |
| ② `test_sigv4_matches_the_aws_sig_v4_test_suite_get_vanilla` | `aws/aws-sig-v4-test-suite` 的 `get-vanilla` 用例 | ✅ 真官方值 |

**「三段分别断言」这个设计值得记**：测试注释写「只比签名的话，'规范化错在哪一步'永远查不出来」。这比"签名对上就算过"强一个量级。

**`bedrock_available` / `bedrock_client`**：`bedrock_available()` 返回 `(可用?, 原因)`，`AWS_DEFAULT_REGION` 兼容别名；`bedrock_client()` 先调它，**凭证齐才造 `BedrockConverseClient`** ✓
**而且它把"放行"和"做完了"分得很清**：`providers.py:92-94` 单列 `BEDROCK_LIMITATIONS` 表（"用户看到'支持 Bedrock'之后，最需要的恰恰是这张'但你别指望这些'的清单"），`:104` 明写「**端到端一次都没跑通过**：本机没有 AWS 账号、没有网络。已验证的是「签名与 AWS 官方…」」→ **这个 ⚫ 的 `缺：` 段有底气**。

**④ 8.3 的 ⚫ → ✅✅ 我实机验证了：真的 bind 回环端口**

```
>>> CallbackServer().start()
redirect_uri: http://127.0.0.1:55842/callback
port: 55842        ← 内核分配的临时端口，不是我指定的
已关闭
```
- `oauth_callback.py:50 from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer`（标准库真服务器）
- `:175 def start(self, *, port: int = 0)` — **port=0 = 内核分配** ✓
- `:70 LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})` — 只允许回环 ✓
- `:16` 表格自称「占一个空闲回环端口并起 HTTP 服务器｜**真**｜真的 `bind()`，真的监听，端口是内核分配的」——**我实跑确认这句是真的**
- `:218` 「**先回话、再收工**：反过来的话，`wait()` 一侧可能已经把监听 socket…」——关停顺序也考虑到了
- 为什么这部分**可**本地验证（`:8`）：`http.server` 是标准库、回环 socket 不需要网卡、state 校验是纯字符串比较 → **所以 ⚫ 只缺"真去授权服务器换 token"那一步，不是全黑**

**⑤ 10.13 默认 dry-run → ✅✅ 成立，但我**先找错了参数名**

**先认错**：我先按 `dry_run` 参数去 introspect `session_maintenance.py`，一个都没找到，差点报"没有 dry-run 机制"。**实际机制叫 `apply`**：

```python
# app/session_maintenance.py
:318  def optimize_storage(..., apply: bool = False)      ← 默认 False = 只算不改
:346      if not apply:                                   ← 只报告
:460  def repair(data_dir: Path, *, apply: bool = False)
:486      if apply:                                       ← 才真 REINDEX
:13   「**`apply=True` 时报告实际改了多少行**：不是'将改'，是 `cursor.rowcount` 的真实值」
:24   repair 行：「`--apply` 只 REINDEX｜自动改数据库是危险动作，不做」
```
CLI 侧：`session_extra3.py:246 sm.repair_routing(context.store, apply=apply)` ✓
**所以"维护操作默认 dry-run"成立** ✓，而且"`apply=True` 时报 `cursor.rowcount` 真实值"比"报预计值"诚实得多。

**一处措辞瑕疵（建议改，不是缺陷）**：`session_maintenance.py:12` 写「**所有**会改数据的操作默认只算不改」，但 `sessions archive` 的 `dry_run = bool(getattr(args, "dry_run", False))` = **默认 False（真写归档标记）**。归档只打标记不删消息，危害很小——但"**所有**"这个词过宽了。建议改成"所有**破坏性**维护操作"。

**⑥ 12.5 irc / email 入站 → ✅✅ 真做了，而且自陈边界很准**

| 平台 | 入站实现 | 证据 |
|---|---|---|
| `email` | **`EmailImapInboundAdapter`**：`imaplib` 拉 UNSEEN → 解析 MIME → **`STORE +FLAGS \Seen` 当 ack** | `platforms.py:169-170`；`imap_factory` 可注入，`login/select/search/fetch/store` 五步都能对着假 IMAP 断言 |
| `irc` | **`IrcInboundAdapter`**：裸 socket 读 `PRIVMSG` + `PING/PONG` 保活 | `platforms.py:209-211`；`connector` 可注入，"握手 → 读行 → 解析 → 回 PONG"每步可断言 |

两处细节说明这不是"协议壳"：
- **IRC 的 `ack` 诚实**：「IRC 协议**没有投递确认**（没有服务端 ack、没有 `\Seen` 这种标记）。所以 `ack` 只能把 id 记进内存集合并返回条数 —— 这里如实写出来，而不是让调用方以为'确认过就等于服务端知道了'」✓
- **读窗口必须有界**：「网关是轮询循环：`poll()` 必须**有界返回**，否则一个安静频道会把这个平台的槽位永久占住，其它平台与取消信号都进不来」✓ —— 这是真跑过轮询才会想到的
- **逐平台诚实**：其余 17 家的 `scope` 仍明写「无入站（长连接 / 回调 / 轮询）」✓ —— 没有因为两家做了就把整层说成"有入站"

### B. 你主动交代的三件事

**① `security_audit` 与 `osv` 合并错了 → ✅ 判断与回退都对**

你的教训我完全同意，而且它比"修一个 bug"更值钱：**字段形状相同 ≠ 问的是同一件事**。
两个模块的 `Finding` 五字段一样，但 `security_audit.audit_venv` 问「**指定 venv 里装了什么**」，`osv.audit_local` 问「**项目声明了什么**」——**主语不同**。
而且你抓错的方式也对：**不是靠读代码发现的，是靠 `--venv <假 venv>` 会去扫真实项目依赖这个行为发现的**。**测试抓到了"参数被静默忽略"**——这类 bug 最阴，因为两个函数签名兼容、返回值形状兼容、单测各自绿。
14.5 改标 🟢 我核了：`差异：` 与 `代价` 都在 ✓

**② bedrock `bind_tools` 抛错会崩主循环 → ✅ 处理方向正确**

「改成**不崩、但也不假装有工具**——清空 specs + 记 `tool_binding_unsupported` 事件」——这个取舍我完全同意，而且你说的那句是通则：
> **注册一个"调了必然失败"的工具比不注册更糟。**

这条同样适用于**工具清单、平台适配器、provider profile**：任何"声明了但一调就炸"的条目，都应该退回"不声明 + 明确说为什么"，而不是"声明了再说"。

**③ TUI 在全新数据目录打不开 → ✅ 优雅降级是对的**

`context.store` 经 `require_db()` 抛错 → 改成空快照 + 一句原因。
**值得注意的是它是被"别的 agent 的全量跑"抓到的**——这印证了我们一直在用的基线纪律：**独占全量跑是不可替代的**，单模块绿不能替代它。

### C. 关于你记的那条方法

> `matrix_stats` 的状态列门禁**又抓到我自己**：10.4 的证据栏里写了「从 🟡 变 ✅」，整行出现第二个状态符号，会被误算。**门禁在抓别人之前先抓到了写门禁的人。**

这条我很认可，并补一个观察：**这条门禁之所以能抓到写门禁的人，是因为它检查的是"整行有没有第二个状态符号"，而不是"状态列对不对"**。前者是**结构检查**（对写法敏感），后者只是**取值检查**（对写法不敏感）。**凡是"人写、机器读"的格式，都该有一条结构检查兜底**——这也是为什么 5.11 那三条"关系断言"比钉数字更耐用。

---

## 与目标标志的差距

**⬜ 0**，离目标只差 **10 条 🟡 + 3 条 ⚫**：

| 类别 | 条目 |
|---|---|
| **🟡 10 条** | `1.13`(desktop/gui) · `1.14` · `4.7`(proxy→OAuth，CLI 未传 token_provider) · `4.8` · `4.12`(19 家 2 家真可用) · `5.14`(31/94) · `12.5`(19/22) · `12.10` · `13.4`(wired 11/exposed 4) · `14.4` |
| **⚫ 3 条** | `4.2`(bedrock，缺 AWS 账号+网络) · `8.3`(真 OAuth，缺授权服务器) · `12.9`(peer，缺对端 `/peer/*` 服务端) |

### 关于你问的「1.13 该不该标 🟢」——我的判断

**倾向同意 🟢，但必须满足两个条件，且可能该拆成两条。**

**先把你我的判据对齐**：🟢 的定义是「**能力有，做法不同**」。所以第一步不是问"Orbit 做不做桌面壳"，而是问：

> **Hermes 的 `desktop` / `gui` 除了"有个窗口"之外，还提供了什么浏览器给不了的东西？**

| 如果它主要是… | 那么 | 理由 |
|---|---|---|
| 会话 / 技能 / 记忆的**可视化界面** | **🟢 成立** | 能力等价（L16 Web UI 已 **6/6 = 100%**），载体不同（浏览器 vs Electron 壳） |
| **原生 OS 集成**：托盘常驻 / 全局快捷键 / 文件关联 / 开机自启 / 系统通知 | **不能整条标 🟢** | 那是**真缺的能力**，不是"做法不同" |

**所以我的建议是三选一，按 Hermes 那条的实际内容定**：

1. **如果 Hermes 的 desktop 只是"把 Web UI 套进一个窗口"**（很多 Electron 应用确实如此）→ **整条标 🟢，毫无争议**。`差异：` 写"载体不同：Electron 壳 vs 浏览器"即可。
2. **如果它带托盘/快捷键/文件关联** → **拆成两条**：
   - `1.13a 图形界面` → **🟢**（Web UI 等价）
   - `1.13b 原生壳集成` → **🟡**（或明写"不做"→ 那才是 🟢）
3. **如果你不确定它有没有原生集成** → **先留 🟡，并在证据栏写"待核：Hermes desktop 是否含托盘/快捷键/文件关联"**。**不确定就不升**，这与我们一直在用的"查不到证据就写无实据"是同一条纪律。

**为什么我强调"可能该拆"**：一条 🟢 如果同时装着"等价能力"和"真缺能力"，**它就把缺口掩盖了**——这正是我们上两轮防的"🟢/⚫ 变成垃圾桶"的另一面。**拆开之后，两件事各自诚实。**

**另外，🟢 的门禁要求写"代价"**，1.13 的代价应该写成具体行为差异，例如：
> 「用户得到的是'打开浏览器访问'，失去的是'双击图标即开 / 托盘常驻 / 无需先起服务'。」

**如果这句代价你写不出来，那它其实不是"做法不同"，而是"少了一层"** —— 那就该留 🟡。

---

## 风险与漂移

### R1 🟢 **本轮没有发现新的漂移**（这是自第 2 轮以来第一次）

我按老规矩逐项查了：口径一致 ✓、格式零异常 ✓、3 条 ⚫ + 3 条 🟢 的必备段落齐全 ✓、口径变更日志在 ✓、3 处过期 docstring 已修 ✓（`plugin_dashboard` 五处「禁改区」、`PlaneSink.attach()`、`main_routes()`）。
**没有一条"文档声称完成但代码查无实据"。**

### R2 🟡 **两处措辞过宽（不影响判定，但建议改）**

| 位置 | 现状 | 建议 |
|---|---|---|
| `session_maintenance.py:12` | 「**所有**会改数据的操作默认只算不改」 | 改成「所有**破坏性**维护操作」——`sessions archive` 默认真写标记（只标记不删消息） |
| `MATRIX 1.13`（待你定） | 一条混着"界面"与"原生集成" | 见上节，可能该拆两条 |

### R3 🟡 **轮末 2 项在飞**

```
轮初 git status: 0 条目 ✓
轮末 git status: 2 条目
  ?? backend/app/acp_stream.py
  ?? backend/.tmp_onecore_probe.ps1     ← 临时探针文件，建议清理
```
`acp_stream.py` 是下一波（ACP 流式）在起；那个 `.tmp_onecore_probe.ps1` 是临时脚本，**建议删掉再提交**，否则会进仓库。

### R4 🟡 **未验证项（如实标注）**

| 项 | 为什么没验 |
|---|---|
| 13.8 的**红路径实跑** | 要注释掉 `main.py` 三行 = 改代码 → 未做。我验证的是"断言结构与失败消息证明它会红" |
| `12.5` 的 19/22 里**其余 17 家** | 只核了 irc/email 两家（你点名的）与它们的 scope 文字 |
| `5.14` 的 31/94 逐条缺口原因 | 只核了总数（上轮核过 23/94 的逐条质量） |
| `orbit chat` 交互、`gateway start`、`cron run`、`backup create/restore`、`config edit`、`doctor --fix/--live` | 会改状态或真发请求 → 未运行 |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用（连续 8 轮如此） |

---

## 下一轮建议

按优先级：

1. **【P0】1.13 先查清 Hermes desktop 有没有原生集成，再定 🟢 还是拆两条**（见上节）：判据一句话——「除了有个窗口，它还提供什么浏览器给不了的东西？」有托盘/快捷键/文件关联 → **拆 1.13a(🟢) + 1.13b(🟡)**；纯粹套壳 → 整条 🟢。**不确定就留 🟡 并写"待核"。**
2. **【P0】顺手清掉 `backend/.tmp_onecore_probe.ps1`**：临时探针文件别进仓库。
3. **【P1】4.7 的 CLI 接线（你下轮本来就要做）**：`--token-provider` 没传就是"库层有、运行时没接"的典型——**这正是我们反复抓的那一类**，做完请顺手加一条测试断言 CLI 真的把 provider 传进去了（像 13.8 的源码层机械证据那样）。
4. **【P1】L4 只剩 4.7 / 4.8 / 4.12 三条 🟡 + 4.2 一条 ⚫**：收口这三条，L4 就到 100%，**十层 100%**。其中 4.12（19 家里 2 家真可用）建议像 `4.2` 那样**逐家写清"能/不能 + 缺什么"**，而不是笼统写"19 家里 2 家"。
5. **【P1】`session_maintenance.py:12` 的"所有"改"破坏性"**（R2）：一行改动，但"所有"这种绝对量词在证据栏里最容易被后来的读者当成已验证的强断言。
6. **【P2】把 13.8 的做法固化成模板**：这轮最值得复制的不是"补了三行"，而是**"真 app 测试 + 源码层机械证据"两条独立路径**。建议凡是"库层有、运行时可能没接"的条目（4.7、12.10 等），都配一对这样的测试。
7. **【P2】`BEDROCK_LIMITATIONS` 这种"但你别指望这些"的清单值得推广**：它是 ⚫/🟢 条目的最佳形态——**放行 + 单独一张限制表 + 明写"端到端没跑过"**。建议 4.12、12.5 剩余平台也各配一张。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 11 轮未核。`nodes.py:254 context_meta.setdefault("compressed", {})` 仍是空占位——正是那 15 条之一，而 2.7/10.4 这轮已动到同一段代码，**下轮顺手看一眼就能收**。

---

*本报告由 monitor 角色只读生成。你点名的 6 项**全部撑得住**：13.8 的真 app 测试有两条独立失败路径、2.7/10.4 真每轮判 0.50、4.2 的两组向量确实是 AWS 官方材料（IAM ListUsers 三段断言 + `get-vanilla`）、**8.3 我实机起了一个回环端口确认真的 `bind()`（`127.0.0.1:55842`）**、10.13 的机制是 `apply=False` 而非 `dry_run`（**我先找错了参数名，已纠正**）、12.5 的 irc/email 入站是真适配器且边界自陈准确。**本轮没有发现新的漂移**——这是自第 2 轮以来第一次。关于 1.13，我倾向同意 🟢，但判据是"它有没有浏览器给不了的原生集成"，有则拆两条，不确定就留 🟡。未验证项单列，不混进完成项。*
