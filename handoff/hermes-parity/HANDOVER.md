
> **⚠️ 数字口径（第 101 轮统一）**：本文件里凡是「N 条 🟡」的说法，**一律以 `MATRIX.md` 为准**。当前是 **5 条**（`1.13b` / `4.12` / `5.14` / `12.5` / `12.10`）。
> 历史文本里出现过的 7 条、6 条是**当时的真实状态**，不是现在的。
> **判据：同一个量在两个文档里各写一遍，必然漂移 —— 要么指过去，要么只在一处写。**

# 接手说明

> 给下一个接这份工作的人。**三节：已封口 / 明确欠着 / 别碰。**
>
> 最后一节最值钱：**它记录的是「看起来像 bug、其实是决定」的东西。**
> 我自己在 59 轮里两次把已声明的设计当成缺陷去改，都被门禁或文档拦住了。

## 〇、定论：本机可达的上限在哪里（第 96 轮）

**先说结论：goal 的完成标志（MATRIX 无 🟡）在本机环境里不可达。
下面的每一条都不是推测，是逐条实测过的阻塞条件。**

### 当前状态

**182 行：✅168 · 🟢5 · ⚫4 · 🟡5 · ⬜0 —— 严格 95.1% / 可验证 97.2%**
工具覆盖 **59/94**；八道门禁全绿；全量 `3770 passed / 3 skipped / 0 failed`。

### 5 条 🟡 各自缺什么（全部实测）

| 行 | 缺什么 | 谁能关掉它 |
|---|---|---|
| `4.12` 语音 18 家 | **带宽**。`edge-tts` 跑 29 分钟、`kittentts` 跑 25 分钟均无进展（~54 kB/s）；`sapi`/`onecore` 实测 `available=False`（「current security setting」，不是代码） | 换快网络 + 装音色 |
| `5.14` 工具 94 个 | 剩 35 条：**元宝 5 条要 Tencent IM SDK**、**desktop 15 条要桌面运行时**、**media 4 / vision 2 要生成模型**、**飞书 5 条要 API 参考**（`research/` 只有参数没有端点）、search 1 要 xAI 凭证 | 外部 SDK / 桌面应用 / 模型 |
| `12.5` 平台 22 个 | `buzz` 签名层**已不缺**（`coincurve` 已装、BIP-340 验签通过），缺 `buzz` CLI + relay；`photon` 缺 Node sidecar + 项目凭证 | 外部 CLI / 凭证 |
| `12.10` enroll | 3 条路径 2 条已做完并有测试，第 3 条要**公网可达的 HTTPS 回调** | 公网地址 |
| `1.13b` Electron 壳 | `electron` 包 ~100MB（~54 kB/s）+ 托盘/快捷键/自启/更新是**一整块新工程** | 带宽 + 排期 |

### 4 条 ⚫ 全部是「实现完了但缺对端才能验」

`4.2` bedrock（`sign_sigv4()` 已实现，要 AWS 凭证）· `4.7` proxy OAuth（全链路已实现，要真 OAuth provider）·
`8.3` OAuth（除一次 POST 外全真，要真 OAuth 服务器）· `12.9` peer（5 个端点已实现，要对端）。

**没有一条是「代码没写」。**

### 一个必须记住的教训（这条比上面所有数字都重要）

**第 66 轮之前，我把「本机无网络、不能装依赖」写进了十几条理由里 —— 而它是错的。**
`npm` 能连 registry、PyPI HTTP 200、venv 里的 python 也能联网、`pip install` 成功过两次。

**一条被证伪的前提留在文档里，会把后面每个人都劝退。**
所以：**「本机做不到」是本项目最贵的一类断言，它必须像「这条测试证明了 X」一样有可复现的实测证据。**

### 如果要把这个 goal 真正推完，需要什么

1. **一条快网络**（`edge-tts` / `electron` / `kittentts` 都能装）；
2. **一份飞书开放平台 API 参考 + 元宝 SDK 文档**（飞书 5 条、元宝 5 条）；
3. **一个公网 HTTPS 端点**（`12.10` 第 3 条路径）；
4. **一批凭证**（AWS / xAI / HA / Discord 真 token，用来把 ⚫ 与 media/vision 验成 ✅）；
5. **一个排期决定**（`4.8` 已做完；`1.13b` 的桌面壳要不要做）。

**这五样没有一样能靠「再多写点代码」变出来。**

## 〇·一 第 100 轮里程碑快照（全部本轮实跑）

```
HEAD                      ca29045
git status                干净（0 项）

MATRIX                    182 行：✅ 168 · 🟢 5 · ⚫ 4 · 🟡 5 · ⬜ 0
严格完成度                95.1%   = (✅+🟢)/182
可验证完成度              97.2%   = (✅+🟢)/(182−4)
工具覆盖                  59 / 94（missing 35）

pytest -q                 3770 passed, 3 skipped, 0 failed   (468.53s)
ruff check .              All checks passed!
mypy app                  Success: no issues found in 181 source files

matrix_stats --check      exit 0
audit_matrix_citations    exit 0
audit_matrix_flags        exit 0
audit_tool_mappings       exit 0（59 个 covered 全指向真实工具）
audit_test_claims         exit 0
audit_skipped_tests       exit 0
audit_cited_skips         exit 0
```

### 从第 60 轮到第 100 轮发生了什么

| 项 | 第 60 轮 | 第 100 轮 |
|---|---|---|
| MATRIX | ✅166 · 🟡7 | **✅168 · 🟡5** |
| 严格完成度 | 94.0% | **95.1%** |
| 工具覆盖 | 52/94 | **59/94** |
| 门禁 | 4 道 | **9 道 + 1 个报告工具** |
| 全量测试 | 3729 passed（1 条「基线失败」） | **3770 passed（0 failed）** |

**关掉的 🟡**：`14.11`（出站闸全覆盖）· `4.8`（egress 凭证注入代理）· `1.7`（`--usage-file`）。
**新增工具**：`kanban_attach_url` · `ha_*` ×4 · `discord` / `discord_admin`。

### 这 40 轮里最值钱的四条教训

1. **「本机做不到」是本项目最贵的一类断言** —— 第 66 轮实测推翻了「本机无网络」，
   而它此前支撑了 5 条 🟡 约 65 轮。
2. **批量文本替换必须限定在「目标行」内** —— 第 44 轮的 `reason="同上。"` 替换
   误伤了 20 条（跨 desktop/collab/home 三个域），而那 20 条正是分类依据。
3. **接数据源之前先确认「那个字段真的有人写」** —— `Chunk.tokens` 有列、有类型、
   有默认值，**却没有写入点**；第一版 `--usage-file` 因此永远报 `tokens: 0`。
4. **`os.kill(pid, 0)` 在 Windows 上是「杀进程」不是「探活」** ——
   我写了个「随机杀进程」的函数，靠整段回退才没进仓库。

### 剩下的 5 条 🟡 与 35 条工具，缺的都是本机之外的东西

详见「〇、定论」那一节 —— 五样：快网络 / 飞书+元宝 API 文档 / 公网 HTTPS 端点 /
一批凭证 / 一个排期决定。**没有一样能靠「再多写点代码」变出来。**


## 〇·二 第 111 轮最终状态（取代上面第 100 轮的快照）

```
HEAD                      2af9ef1
git status                干净（0 项）

MATRIX                    182 行：✅ 168 · 🟢 5 · ⚫ 4 · 🟡 5 · ⬜ 0
严格完成度                95.1%   = (✅+🟢)/182
可验证完成度              97.2%   = (✅+🟢)/(182−4)
工具覆盖                  65 / 94（missing 29）
注册进应用的工具          83 个（全部有 toolset 归属）

scripts/check_all.py      13 项全部通过（10 道硬门禁 + 3 项报告式）
pytest -q                 3790 passed, 3 skipped, 0 failed
ruff check . / mypy app   全绿（183 source files）
```

### 第 100 → 111 轮新增的

| 项 | 第 100 轮 | 第 111 轮 |
|---|---|---|
| 工具覆盖 | 59/94 | **65/94** |
| 门禁 | 9 道 + 1 报告工具 | **12 道 + 1 报告工具**（`check_all` 里 13 项） |
| 注册进应用的工具 | **78**（有 5 个漏注册） | **83** |

**新增工具**：`feishu_doc_read` · 预览面板 5 条（`desktop_preview` / `open_preview` /
`read_preview` / `annotate_preview` / `close_preview`）。

### 第 108 轮修的那个真 bug（这一节最值得读）

`scripts/audit_tool_registration.py` 真起一次应用，比对「目录声称 covered」与
「`ToolRegistry` 里实际注册的」——**抓到 5 条**：

```
setup_mcp / text_to_speech / compact / context_usage / tool_call
```

有实现、有测试、目录标了 covered，**但 `main.py` 从没调 `register_growth_tools`** ——
**模型根本看不到这 5 个工具。**

**最讽刺的地方**：`main.py` 第 221 行的注释原文就是
「覆盖率报告会说『覆盖了』，但模型看不见——**那种不一致比没做更糟**」。
**这句警告自己没被遵守。**

**为什么之前的门禁查不出来**：`audit_tool_mappings` 只查「名字在某个 `*_TOOLS` 列表里存在」，
**不查那个列表有没有被注册进应用**。**只有「真起一次应用、看注册表」才查得出来。**

### 如果只剩一件事要做

**先读「〇、定论」那一节** —— 它列了 5 条 🟡 与 29 条工具各自缺什么，
以及要把这个 goal 推完需要的五样东西（快网络 / 飞书+元宝 API 文档 / 公网端点 /
一批凭证 / 一个排期决定）。**那五样没有一样能靠「再多写点代码」变出来。**


---

## 一、已封口（做完且有验证证据）

**核验方式**（三条命令，缺一条都不算）：

```powershell
cd E:\bbbooottt\backend
.\.venv\Scripts\python.exe -m ruff check .          # 应 All checks passed
.\.venv\Scripts\python.exe -m mypy app              # 应 Success, 177 files
.\.venv\Scripts\python.exe -m pytest -q             # 应 1 failed（基线既有）/ 3718 passed
cd E:\bbbooottt
.\.venv\Scripts\python.exe scripts\matrix_stats.py --check       # 应 exit 0
.\.venv\Scripts\python.exe scripts\audit_matrix_citations.py     # 应 exit 0
```

**那个 1 failed 是基线既有的**：`tests/test_connectors.py::test_browser_append_and_reload`
——它不是回归，**从第 1 轮起就在**。

**182 行 MATRIX：✅167 · 🟢5 · ⚫4 · 🟡6 · ⬜0（严格 94.5% / 可验证 96.6%）**

**出站云元数据硬拦已全覆盖**（第 61 轮完成）：`egress.refuse_cloud_metadata()` 是全仓唯一实现，
接在**全部 5 个用户可配 URL 的出站点**上——模型客户端构造 / 代理转发 / 模型连通性检查 /
peer 请求 / 平台发送。**判据是「URL 是否来自用户可配配置」，不是「有几个 `httpx.Client(`」。**


---

## 二、明确欠着（5 条 🟡，每条都有解锁条件）

| 条目 | 欠什么 | 解锁条件 | 量级 |
|---|---|---|---|
| **4.8** iron-proxy | `install` / `setup` / `start`（**不需二进制的部分已全部做完**） | 下载 iron-proxy 二进制 + 网络 | 约 1 周 |
| **12.5** `buzz` | 平台适配器 | `pip install coincurve`（**不要手写 secp256k1**） | 半天 |
| **12.5** `photon` | 平台适配器 | `pip install grpcio` | 半天 |
| **12.10** enroll | 注册流程 | 公网可达 HTTPS 回调 | 1 天 |
| **4.12** 语音 15 家 | 见 MATRIX 五类（1 可用 / 1 平台不适用 / 4 要装 / 8 云 / 3 模型 / 1 命令） | 各自不同 | 每家 1–2 小时 |
| **5.14** 工具 42 条 | 见 GAPS 终表三类 | 大部分要外部条件 | —— |
| **1.13b** 原生外壳 | Electron 外壳 | `npm install electron` + 打包器 | 3–5 天 |

**`4.12` 的 `say` 是「平台不适用」**（macOS 命令），**不是「缺外部条件」**——
后者暗示「配一下就行」，前者是「这个平台永远没有」。

---

## 三、别碰（看起来像 bug，其实是决定）

> **这一节是为了防止下一个人重复我犯过的错。每一条都写清「为什么」和「改了会怎样」。**
>
> **⚠️ 3.5 与 3.8 严格说是「事实」不是「决定」** —— 3.5 是「没做」（不是决定不做），
> 3.8 是「字段名搞错了、已修」（不是设计选择）。**放在这里是因为它们同样"看起来像 bug"**，
> 但**不要把「事实」读成「我们选择了这样」** —— 前者可以变，后者变了要付代价。

### 3.1 `EgressPolicy` 的 `default=allow` 不做 IP 校验

`_guard_resolved_ip` 里有四条跳过条件，其中一条是「走 `default=allow`（没命中任何规则）不查」。

**我曾把它当「第二个安全洞」去打补丁——那是误判。** 它是有名字、有位置、有理由的设计：
**没配策略时不该每次出站都真查 DNS。**

**判据：把「我没读到的设计」当成「洞」，与把「我没读到的实现」当成「不存在」是同一类错误。**

**但云元数据是「例外的例外」**：它没有合理场景，所以 `egress` 里对它是**硬拦**（不受 allow/deny 影响）。

### 3.2 `kanban_unblock` 父卡没完成时「降级」而不是「拒绝」

Hermes 实证（`01-cli-surface.md:795`）：**父任务未完成则回 todo**。

**不要加 `--force`** —— 强制放回可认领状态会制造死循环（重新认领 → 又卡住 → 再 block → 再 force）。
**真正的闸门在 `claim()`**：父卡没 done 就拒绝认领。

**判据：闸门要放在危害真正发生的那一步。**

### 3.3 `kanban_heartbeat` 拒绝已 done 的卡

Hermes 实证（`01-cli-surface.md:811`）：「为**运行中**任务发心跳」。
done 不是运行中，**打了会让它看起来还在跑**。

### 3.4 `--source` **真的筛会话列表**，但只在「创建会话时」能设

`--source` 在 Hermes 是「会话来源标签，用于过滤」（`01-cli-surface.md:181`）。
Orbit 的 `conversations.source` 列**已加**（`0012` 迁移），`sessions list --source` **真的筛**。

**但 `--source` 只在「创建会话时」能设** —— **Hermes 自己也没有 set-source / retag 命令**，
所以「只在创建时能设」不是缺陷。

> **这节标题曾经是错的**：它一度写作「`--source` 记进任务事件，**不做会话列表筛选**」——
> 那是第 41 轮的状态（当时没有 `conversations.source` 列）。第 50 轮加了列、改了正文，
> **但没改标题**。
> **判据：标题是「承诺」，正文是「证据」；两者矛盾时读者会信标题 ——
> 所以改正文必须同步改标题，标题的滞后比正文的滞后更危险。**

### 3.5 `kanban_attach_url` 未做

**要联网下载**。它的失败模式（下载超时 / 重定向 / 大文件）与本地落盘的 `attach` 完全不同，
**不能拿 `attach` 顶替**。

### 3.6 `test_graph::test_time_budget_backgrounds` 与 `test_mcp` 的偶发失败

**两条都是负载敏感的**（第 1 条已有 3 次独立观察，第 2 条 2 次）。单跑必过。

`wait_status` 的窗口已从 `stall=15s / ceiling=60s` 放宽到 **45s / 150s**，
理由写在函数 docstring 里：**`stall` 的假设是「没进展 = 逻辑坏了」，
但 CPU 被饿住时「没进展」也是正常的**。

**不要把这两条标成 flaky** —— 那等于把红灯信号调成静音。

### 3.7 `egress disable` / `egress reload` 返回 exit 3

**那不是失败，是正确答案**：没有守护进程时，拒绝比假装成功诚实。
**这是「静默降级」的反面。**

### 3.8 `--run-budget` 写的是 `max_seconds` 不是 `run_budget_seconds`

`Settings` 里**没有** `run_budget_seconds` 这个字段，pydantic 会**静默丢掉**未声明的 kwarg
——原来那版**一个字节都没生效**。产品读的是 `settings.max_seconds`。

**判据：`updates` 里出现的每个键都必须在 `Settings.model_fields` 里。**
`tests/test_global_flag_consumers.py` 用**性质断言**守着这条（遍历当前实际写的键，不是清单）。

### 3.9 复核方式必须跑在「被测的那个环境」里

**这一条是元级的：它管的是「怎么复核」，不是「复核什么」。**

**实例**：`GAPS.md` 的复核方式一度写成 `pip show coincurve` ——
但**这个 venv 里没有 `pip` 模块**（`No module named pip`），
而 **PATH 上的 `pip` 是 Miniconda 的**（`E:\Miniconda3\Scripts\pip.exe`）。
→ **它查的是另一个环境**：如果 Miniconda 里恰好装了，它会报「有」而 venv 里没有。

**正确写法**：
```powershell
cd E:\bbbooottt\backend
.\.venv\Scripts\python.exe -c "import importlib.util as u; print(u.find_spec('coincurve') is not None)"
# → False
```

**判据：复核方式必须跑在「被测的那个环境」里；全局 `pip` 不等于 venv ——
而「查错环境」比「不查」更糟**（前者给出确定的错答案，后者至少让人知道没查）。

### 3.10 第五道门禁：`scripts/audit_matrix_flags.py`

**它查的是「MATRIX 提到的每一个 `--flag` 在 CLI 里真的存在吗」。**

**为什么需要**：第 63 轮就是这样抓到 `--usage-file` 的 —— MATRIX 声称 23 个运行 flag「完整」，而其中一个在 `app/cli/` **全目录零命中**。
**「参数出现在文档里」不等于「它存在」。**

**口径（别当绝对判定）**：
- 只查长选项；短选项在散文里歧义太大；
- 权威来源是 **parser 的 action 名单**，不是 `--help` —— 很多选项是 SUPPRESS 的，`--help` 看不到但确实能用；
- **会误报**：散文里会出现**别的程序**的参数（实测两例：`msedge --app=http://…`、`pip install … --retries 10`），已登记在 `FOREIGN` 表里。

**当前结果**：CLI 已知长选项 **441** 个，MATRIX 提到 **65** 个，**全部存在或已登记为外来参数**。

**判据：审计脚本的价值不在「能判死」，在「把需要人看一眼的地方集中起来」。**

### 3.11 第六道门禁：`scripts/audit_tool_mappings.py`

**它查的是「`tool_catalog` 每条 covered 声称映射到的工具，真的存在吗」。**

**为什么需要**：**映射到不存在的名字比「没实现」更危险 —— 没实现会被发现，映射到不存在的名字不会。**这是「接上了但有洞，比没接上更危险」的目录版本。

**基准是本脚本的关键**：所有模块里 `*_TOOLS` / `TOOLS` 且元素全是 `Tool` 实例的列表的并集。
**为什么不能用别的**：第 73 轮我试过四个入口（`app.state.*tools`、`toolsets.TOOLS`、`LOCAL_TOOLS`、`GRANTABLE_TOOLS`），**四个都不是工具名清单**，于是误报 `send_message` / `todo` / `clarify` 不存在 —— **错的是取数方式，不是目录**。

**脚本带自检**：基准里找不到 `send_message` 等已知工具就直接返回 2。

**当前结果**：11 个 `*_TOOLS` 列表 → **70 个唯一工具名**；目录声称 **59 条**映射，**全部指向真实工具**（53 个 covered）。

**判据：先证明基准能列出「已知一定存在的名字」，再用它判别人缺什么。**

### 3.12 第七道门禁：`scripts/audit_test_claims.py`

**它查的是「声称『有断言 / 有测试』的 ✅ 行，有没有点名测试文件」。**

**为什么需要**：`12.1` 的证据曾写着「……死进程的孤儿锁会被回收（**有断言**）」——**但没说是哪个测试。** 于是这条 ✅ 只能被信任，不能被复核。
**「有断言」是一句声明，不是一个抓手；点名文件才是。**

**首轮抓到 6 条**（`4.1` / `7.4` / `7.7` / `7.11` / `10.4` / `12.1`），全部已补：4 条补到**精确函数名**（已读源码确认），2 条只到**文件级**（如实标注，不假装精确）。

**会误报的情况**：一句话里提到「既有测试断言…」是在**解释设计取舍**，不是声称本行有测试（`4.1` 就是这种）。**遇到误报，正确处理是给那一行补上真正的测试名，而不是放宽正则** —— 因为补上名字本身就有价值。

**判据：审计脚本抓到的每一条，要么是真问题，要么是「这句话本来该点名却没说」——两种都值得改。**

> **本节是第 92 轮补写的**：第 75 轮我以为写进去了，实际上没有 —— 门禁一直在跑，但没有任何文档告诉别人它存在。
> **一个没人知道的检查等于没有：门禁的价值一半在「能跑」，一半在「有人知道它在跑」。**

### 3.13 第八道门禁：`scripts/audit_skipped_tests.py`

**它查的是「MATRIX 引用的测试文件里，有没有会跳过的测试」。**

**为什么需要**：第 78 轮我从 `pytest -rs` 的 skip 原因里抓到一处虚报 —— `4.12` 说 `sapi` 能合成，而 skip 原因写着「No voice installed on the system」。
**「测试文件存在」≠「测试跑过」。** 如果一条 ✅ 行的唯一证据是一个会跳过的测试，**那它在本机根本没被验过。**

**为什么不跑 pytest**：跑一遍要 6 分钟，当门禁太慢。**静态扫 skip 标记就够了**（`pytest.skip(` / `pytest.mark.skipif` / `@pytest.mark.skip`）。

**口径**：只报**文件级**、只报告不判死（一个文件里可能 20 个测试只跳 1 个）。

**当前结果**：12 个测试文件含 skip 标记，其中 **5 个被 MATRIX 引用**；而运行时**实际只跳过 3 个测试**（`test_budget.py:206`、`test_voice_local.py:119/144`）。

**判据：门禁要区分「静态可能跳过」和「本机确实跳过」—— 前者是提醒，后者是账。**

### 3.14 门禁总入口：`scripts/check_all.py`（**一条命令跑完**）

```powershell
cd E:\bbbooottt
.\backend\.venv\Scripts\python.exe scripts\check_all.py
```

**exit 0 = 全绿；非 0 = 至少一道红。** 它会打出一张「哪道绿哪道红」的表。

**脚本自己设 `PYTHONIOENCODING=utf-8`** —— 不设的话 `matrix_stats` 在 GBK 控制台会
`UnicodeEncodeError`（这个坑踩过）。

**12 项里 3 项是「报告式」，恒返回 0**（`audit_skipped_tests` / `audit_cited_skips` /
`audit_hardcoded_counts`）—— 它们把需要人看一眼的地方集中起来，**不替你判死**。

---

下面是各项的说明（想单独跑某一道时用）：


**全部从仓库根跑，全部 exit 0 才算绿。**

```powershell
cd E:\bbbooottt
$env:PYTHONIOENCODING='utf-8'          # 不加会在 GBK 下报 UnicodeEncodeError
.\backend\.venv\Scripts\python.exe scripts\matrix_stats.py --check        # 1 行数/状态列/完成度
.\backend\.venv\Scripts\python.exe scripts\audit_matrix_citations.py      # 2 引用测试文件是否存在
.\backend\.venv\Scripts\python.exe scripts\audit_matrix_flags.py --check  # 3 提到的 --flag 是否存在
.\backend\.venv\Scripts\python.exe scripts\audit_tool_mappings.py         # 4 covered 映射是否指向真工具
.\backend\.venv\Scripts\python.exe scripts\audit_test_claims.py           # 5 「有断言」是否点名测试
# 6 被引用的测试是否含 skip
.\backend\.venv\Scripts\python.exe scripts\audit_cited_skips.py          # 9 ✅ 行是否引用了含 skip 的文件
cd backend
.\.venv\Scripts\python.exe -m ruff check .                                 # 7 风格
.\.venv\Scripts\python.exe -m mypy app                                     # 8 类型
```

**注意两条口径**：

- `matrix_stats` 报「Orbit 顶层 84」而 MATRIX `1.4` 说 **81** —— **两个数都对**：
  81 是 `REGISTRY.all()` 的**命令名**数，84 是**含别名**。见 `1.4` 的行内说明。
- 门禁 5 / 6 是**报告式**的（`audit_skipped_tests` 恒返回 0）——
  **它们把需要人看一眼的地方集中起来，不替你判死。**

**判据：门禁要有一份权威清单 —— 否则「八道」只是传说，
而第 75 轮我就是这样让第七道门禁整整 17 轮没人知道。**


### 3.15 第九道门禁：`scripts/audit_cited_skips.py`

**它把前两道门禁交叉起来**：`audit_test_claims` 只问「有没有点名测试文件」，
`audit_skipped_tests` 只问「哪些文件含 skip 标记」。
**两者都没问的是**：一条 ✅ 行的证据落在**一个含 skip 的文件**上时，
**那个被跳过的测试，是不是这条行唯一的依据？**

**口径（宽，只抓需要人看一眼的）**：列出「✅ 行引用含 skip 文件」的全部组合，
**不判死** —— 一个文件 20 个测试只跳 1 个是很常见的。

**首轮结果**：12 个测试文件含 skip 标记；**7 条 ✅ 行引用了它们**
（`5.4` / `5.5` / `7.18` / `10.7` / `11.3` / `11.6` / `15.4`）。

**逐条看过之后：0 条真有问题。**
- 运行时**实际只跳过 3 个测试**（`test_budget.py:206`、`test_voice_local.py:119/144`），
  其余文件里的 skip 是 `skipif` 条件在本机求值为假；
- 所以只有引用 `test_budget.py` 的 `5.4` / `5.5` 值得查 ——
  **而它们都没提 symlink**，被跳过的那个 `test_symlink_spill_escape_is_refused` 不是它们的依据。

**判据：交叉审计的价值不在「找出问题」，在「把两个门禁都没覆盖的缝补上」——
而这条缝的第一轮结果是干净的，那也是一个结果。**


### 3.16 报告式工具：`scripts/audit_hardcoded_counts.py`（**不是门禁**）

**它列出 MATRIX 证据里写死的计数**（`N/M`、「已到 NNNN」、`**N 条**`、`covered N→M`），
供人扫一眼。**恒返回 0，不进「九道门禁」清单。**

**为什么需要**：第 98–99 轮连抓到两处过期数字，都是同一种病 ——
`10.11` 写「已到 0011」（实际 0012）、`10.13` 写「27/27」（实测 33）。
**它们过期时不会报错，只会让读者以为项目停在那里。**

**它有一个已知假阳性，必须知道**：**它分不清「活的断言」和「我引用的历史值」。**
首轮报 10 处，其中多数是我自己写的「原写 27/27」「原写已到 0011」这类**纠错记录**。
**所以这份清单不能当错误列表读，只能当「该扫一眼的地方」读。**

**判据：能自动判的交给门禁，判不了的交给「把它集中起来」——
一份 10 行的清单，比在 182 行里靠记忆找要便宜得多。
但报告式工具必须说清它会误报什么，否则下一个人会把它当真。**


### 3.17 第十道门禁：scripts/audit_doc_consistency.py

**它查的是「GAPS / HANDOVER / README 里的『N 条 🟡』是否与 MATRIX 实际一致」。**

**为什么需要**：第 101 轮发现 MATRIX 是 **5 条 🟡**，而 GAPS.md 里写着
「7 条」1 处、「6 条」6 处、「5 条」1 处 —— **读者读到哪个数字，取决于他看哪一行。**

**判据：同一个量在两个文档里各写一遍，必然漂移 —— 要么指过去，要么只在一处写。**

**PROGRESS.md 故意不在范围内**：它是监视子agent 每轮重写的快照，天然滞后于 MATRIX。
**门禁只管长期文档 —— 快照滞后不是缺陷，长期文档矛盾才是。**

**遇到误报怎么办**：历史叙述里提到旧数字会触发。
**正确做法是把那句改写成不重复数字的说法，而不是放宽正则。**


### 3.18 第十一道门禁：`scripts/audit_research_lines.py`

**它查的是「MATRIX 引用的调研文档能不能解析到真实文件」。**

**为什么需要**：`audit_matrix_citations` 只查 `tests/*.py` 在不在，**它不管调研文档**。
而 MATRIX 的证据大量依赖调研结论 —— **引用解析不到时，读者会以为「有实证」，其实那份实证找不到**。

**两种引用形状都要查**（MATRIX 里实际出现的）：

| 形状 | 实测数量 | 例子 |
|---|---|---|
| 编号式 | **8 个**（主流） | `【实证】调研 03 §1` |
| 路径式（带行号） | **1 处** | `research/01-cli-surface.md:1834` |

**只查第一种会得到一个「覆盖 1 处」的门禁 —— 那种门禁给人的是假安心。**
所以两种都查：编号式验 `research/NN-*.md` 存在；路径式验文件存在**且行号在范围内**
（行号一过期，「可复核」就变成不可能，而且不会报错）。

**当前结果**：8 个编号 + 1 处路径，**全部能解析**。

**判据：门禁的覆盖面要配得上它占的那一行 —— 只覆盖 1 处的检查不该叫门禁。**

### 3.19 第十二道门禁：`scripts/audit_tool_registration.py`

**它查的是「目录里标 covered 的工具，真的注册进应用了吗」。**

**为什么 `audit_tool_mappings` 不够**：它只查「那个名字在某个 `*_TOOLS` 列表里存在」。
**它不查那个列表有没有被 `registry.register()` 接进应用。**
于是会出现：**工具写好了、测试绿了、目录标了 covered —— 但模型永远看不到它。**

**它真起一次应用**（`create_app()` + `TestClient`），拿 `ToolRegistry` 里实际的工具名来比对。

**首轮抓到 5 条**：`setup_mcp` / `text_to_speech` / `compact` / `context_usage` / `tool_call` ——
全在 `GROWTH_TOOLS` 里，有实现有测试，**但 `main.py` 只调了 `register_extra_tools` 与
`register_round3_tools`，没调 `register_growth_tools`**。已接上：注册工具 78 → **83**。

**最讽刺的地方**：`main.py` 第 221 行的注释原文就是
「覆盖率报告会说『覆盖了』，但模型看不见——**那种不一致比没做更糟**」。
**这句话警告的正是这件事，而它自己漏了。**

**判据：写在注释里的警告不会自己执行 —— 它需要一个门禁。**

**第 109 轮：把这个 bug 直接泛化扫了一遍。**

`app/` 里一共 **10 个** `register_*` 函数。逐个查「有没有被调用」：

| 函数 | 定义于 | 结论 |
|---|---|---|
| `register_action_route` | `plugin_host.py` | 同文件内调用 ✓ |
| `register_ask_user` | `nodes.py` | `agent.py` / `nodes.py` 调用 ✓ |
| `register_handler` | `_table.py` | **31 个模块**调用 ✓ |
| `register_provider` | `web_providers.py` | `plugin_capabilities.py` 调用 ✓ |
| `register_specs` | `_table.py` | **15 个模块**调用 ✓ |
| `register_extra_tools` / `register_round3_tools` | `tool_extra_impl*.py` | `main.py` 调用 ✓ |
| **`register_growth_tools`** | `tool_extra_impl.py` | **没人调用 ✗（第 108 轮已修）** |

**结论：`register_growth_tools` 是唯一真漏的。** 另外 5 个看似「main.py 里没有」的，
其实是被几十个模块共用的 helper —— **扫一遍才知道，凭印象会漏。**

**判据：发现一个 bug 之后，把它所属的那一类扫一遍 —— 
否则你只修了那一处，而不知道还有没有第二处。**

---

**第 110 轮：同一类扫到 CLI 层。**

问的是同一个问题：**标 `done` 的命令，真的接上了执行体吗？**

`spec.REGISTRY.parity_rows()` 里有 **551 条**，其中 `state == "done"` 的 **385 条**。
逐条解析到真实 `Command` 对象再查 `handler`：

| 查什么 | 结果 |
|---|---|
| `done` 但 `handler is None` | **33 条** —— **全是有子命令的父命令**（`approvals` 有 `approvals allow` 等），**正常模式，不是缺陷** |
| **`done` 且既无 `handler` 又无子命令** | **0 条** |

**结论：CLI 层没有「声明了没接线」。**

**两次测量错误的教训**（都在这一轮里）：
① 第一次我用 `r.get('handler')` —— **`parity_rows()` 的 dict 里根本没有 `handler` 字段**，
于是 385 条全被报成「handler 为空」，**我在量空气**；
② 第二次没排除「有子命令的父命令」，33 条正常模式被当成缺陷。

**判据：跑出「全部异常」时，先怀疑自己的取数 —— 全量异常几乎总是测量错，不是代码错。**

## 四、方法论（66 轮里用血换的，按价值排序）

1. **「本机做不到」是本项目最贵的一类断言 —— 它一旦写错，会静默地豁免掉所有相关工作。**
   **第 66 轮的实测把这条前提推翻了**：`npm view electron` → 45.1.0、PyPI → HTTP 200、
   venv 的 python 也能联网、`pip install coincurve` 成功、`coincurve` 的 BIP-340 Schnorr 验签通过。
   **「本机无网络、不能装依赖」是错的，而它支撑了至少 5 条 🟡 约 65 轮。**
   **所以「做不到」必须像「这条测试证明了 X」一样有可复现的实测证据 ——
   而 `pip install` 能不能跑，只需要一条命令就能测。65 轮里没人跑过。**
2. **`git status` 空只覆盖「工作区 vs HEAD」，覆盖不了「坏代码是否进过提交」。**
   回退要验两处：工作区、历史。
2. **「把代码改坏之后测试仍然过」时，第一个该怀疑的是「我改坏了没有」，不是「测试没用」。**
   我两次自证都错在「降级」这一环。
3. **要证明「没有上限」，测更大的数只能排除「小上限」；真正的证明是「代码里没有上限变量」。**
4. **判断「某个定义在不在」必须匹配定义签名（`\ndef name(`），不能匹配裸名字**
   ——裸名字会把调用点也算进去。同一个错我犯过两次。
5. **同一份安全检查抄到第 3 处就该抽出来**——不是为了少打字，是为了不让它们漂移。
6. **`✅` 的行不能有「已知未完成」；有欠账的行不能标 `✅`。**
   而「写进 `✅` 的行」比「写进散文」更糟——**它看起来进了账本，但状态说"做完了"**。
7. **「命令出现在 `--help` 里」只证明它被注册，不证明它做了那件事。唯一证据是跑它、看副作用。**
8. **同一个错误犯两次之后，就该把它从「注意」升级成「机械检查」。**
   `scripts/matrix_stats.py` 与 `scripts/audit_matrix_citations.py` 就是这么来的。
9. **「不需外部条件」≠「该做」——看的是「做出来的东西能不能被验证」。**
   所以不要手写 secp256k1 去绕过 `coincurve`。
10. **带中文 / 特殊字符 / 三引号的改动一律走脚本文件，不走 PowerShell heredoc。**
    这个坑踩了 9 次以上（`—` U+2014 被吃坏、`\r` 被转义、缩进被破坏）。
