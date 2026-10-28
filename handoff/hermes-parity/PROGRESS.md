# 对标进度报告

**更新时间**：2026-10-28 12:48（本机实测时钟，只读取证）
**本轮轮次**：**N = 9**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑会改状态或真发请求的命令（`orbit setup` / `config edit` / `doctor --fix` / `cron run` / `gateway start` / `backup restore` 全部未运行）。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `a693224`；**轮初工作树干净，轮末出现 2 个在飞改动，见 R1**）

---

## 总体完成度

### **127 / 181 项 = 70.2%**

| 项 | 说明 |
|---|---|
| 权威口径 | 读「状态」列（先替转义竖线 `\|` 再切段） |
| 完成 / 部分 / 未开始 | **✅ 127 · 🟡 46 · ⬜ 8**（127+46+8 = 181 ✓） |
| 与 `matrix_stats --check` 一致性 | 工具输出「181 项 · ✅127 · 🟡46 · ⬜8（70.2%）」exit 0 —— **与我独立解析逐字一致** ✅ |
| 格式合法性 | 181 行状态列**全部合法**，**无任何一行出现第二个状态符号** ✅（门禁持续生效） |
| 剩余 ⬜ 清单 | 我独立列出的 8 条 = `2.1 / 5.7 / 5.14 / 7.16 / 7.18 / 8.7 / 8.10 / 9.14` —— **与你给的清单逐条相同** ✅ |

> 记账提示：我上轮实测是 **112（61.9%）**，你这次写「64.1% → 70.2%」。中间那 4 条（112→116）是我上轮报告之后、本轮波次之前落地的，我这轮没有复核它们——**本轮我只对「116→127」这 11 条负责**。

### 硬口径：**全部对上**

| 你的口径 | 我的实测 | 判定 |
|---|---|---|
| `REGISTRY` 81 顶层 / 386 二级 / 39 三级 = 506 | **81 / 386 / 39 = 506** | ✅ 一致 |
| done 290 / partial 28 / todo 188 | **290 / 28 / 188**（合计 506 ✓） | ✅ 一致 |
| pytest 1 failed, 2303 passed, 1 skipped | **1 failed / 2303 passed / 1 skipped（200.17s）** | ✅ 一致（你 198.86s） |
| 唯一失败 = 既有 `test_connectors::test_browser_append_and_reload` | 正是它 | ✅ 一致 |
| ruff `All checks passed!` | **All checks passed!**（exit 0） | ✅ 一致 |
| mypy **135** files clean | **135 files, Success**（exit 0） | ✅ 一致 |
| `matrix_stats --check` exit 0 | **exit 0** | ✅ 一致 |

**上轮三条建议，我逐条复核了落地情况：**

| 上轮建议 | 本轮实测 | 状态 |
|---|---|---|
| 修 `metrics_schema.py` 的 2 个 mypy 错误 | **135 文件 clean** —— 错误消失，且文件数从 122 涨到 135（那 2 个在飞文件已提交） | ✅ **已修** |
| 10.5 的「177,015」换成可复现数字 | MATRIX 现写「64 会话 → 46 份，2,505,095 → 1,858,947 字节，省 18 份 / 646,148 字节」，**与我上轮的只读实测逐字相同**，并注明「先前照抄子agent 自述，复现不出来，**已作废**」 | ✅ **已修，且改法正确** |
| L2 计数 ✅6/🟡3/⬜1 | 本轮实测 L2 = **✅6 · 🟡3 · ⬜1 = 10** | ✅ **已修** |

---

## 分层完成度

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | 13 / 18 | 72.2% | 🟡5、⬜0 |
| L2 Agent 核心循环 | 6 / 10 | 60.0% | 🟡3、⬜1 |
| **L3 上下文装配** | **8 / 8** | **100%** | **✅ 仍完整（你让我确认的那条）** |
| L4 模型适配 | 8 / 13 | 61.5% | 🟡5、⬜0 |
| L5 工具层 | 13 / 19 | 68.4% | 🟡4、⬜2 |
| L6 工具集 / 注册 | 3 / 5 | 60.0% | 🟡2、⬜0 |
| L7 Skill / curator | 13 / 18 | 72.2% | 🟡3、⬜2 |
| L8 MCP | 6 / 10 | 60.0% | 🟡2、⬜2 |
| L9 记忆层 | 11 / 16 | 68.8% | 🟡4、⬜1 |
| L10 持久化 / 会话 | 10 / 15 | 66.7% | 🟡5、⬜0 |
| **L11 cron** | **6 / 6** | **100%** | **🎉 本轮补齐** |
| L12 Gateway / 多平台 | 8 / 11 | 72.7% | 🟡3、⬜0 |
| L13 插件 | 5 / 10 | 50.0% | 🟡5、⬜0 |
| L14 安全 / 审批 | 6 / 10 | 60.0% | 🟡4、⬜0 |
| **L15 可观测性** | **6 / 6** | **100%** | **🎉 本轮补齐** |
| L16 Web UI / 前端 | 5 / 6 | 83.3% | 🟡1、⬜0 |
| **合计** | **127 / 181** | **70.2%** | — |

**现在有三层 100%**：**L3 上下文装配（8/8）**、**L11 cron（6/6）**、**L15 可观测性（6/6）**。
**8 个整层零 ⬜ 的层**：L1 / L4 / L6 / L10 / L11 / L12 / L13 / L14 / L15 / L16（只剩 🟡）。

---

## 本轮抽查证据

**抽样率**：本轮 11 条转 ✅ 中**复核 9 条（82%）**；你点名的 7 条**全部复核**；两处主动交代**全部复核**；另按你的要求抽查了「同名不同语义」这一类。

### A. 你点名的 7 条，逐条判定 —— **7/7 全部撑得住**

**① 9.12 写入门禁「已接线」→ ✅✅ 接线与拦截都验证了（端到端 CLI 差最后一步）**

**接线**（`tools.py`）：
```
:17    from app import approvals, code_exec, egress, memory_write, terminal, tirith, toolsets
:1735  allowed, refusal = memory_write_allowed(latest_user_text(messages))
:1746  check = await memory_write.check_write(...)          ← memory_save 路径真调
```
**CLI 路径**（`cli/commands/memory_extra.py:280`）——**在写之前、按最终态**：
```python
check = context.run(memory_write.check_write(
    planned.final, mode=approvals.resolve_mode(context.settings),
    backend=engine, data_dir=context.data_dir, limit=limit))
if not check.ok:
    context.warn(f"记忆写入被拒（{check.stage}）：{check.reason}")
    return check.exit_code or 1
result = context.run(apply_batch(store, operations, limit=limit))   ← 被拒时根本走不到这
```
**门禁真的会拦**（我直接调 `check_write`，纯内存不落盘）：
```
注入文本 → ok=False verdict=blocked stage=threat_scan
           reason=威胁扫描拒绝：prompt_injection_zh（中文经典注入：要求忽略以上内容）；role_h…
干净文本 → ok=False verdict=blocked stage=approval（因为我没给 data_dir，smart 模式要暂存——预期行为）
```
**退出码链路**：`memory_write.EXIT_CODES = {allow: 0, staged: 2, **blocked: 3**}` → `return check.exit_code or 1` = **3** ✓

> ⚠️ **端到端 CLI 实跑未完成**（如实标注）：`orbit memory batch --file <注入>` 在**全新 `--data-dir`** 下会先撞上「数据库不存在：…app.sqlite（先运行 `orbit setup`）」，而 `orbit setup` 属改状态命令 → 我没有跑。**已核到的是：门禁逻辑（in-process）+ 调用点（写之前）+ 退出码映射（blocked→3）三段完整**，缺的只是"在真库上跑一遍"这一步。
> 附带发现：`memory batch` 对带 BOM 的 JSON 会**明确报错**（"operations 不是合法 JSON：Unexpected UTF-8 BOM"）而不是静默当空——这是好的错误处理，不是缺陷。

**② 10.14 事件溯源 → ✅✅ 三处都真**

```
event_log.py:11  seq   <data_dir>/events/<stream>.jsonl 里**每流独立**的自增整数
event_log.py:17-21  ## 游标必须是 seq，不能是时间戳
                    同毫秒时间戳完全相同，用 ts > cursor 增量读会把它们**一起漏掉或一起重复**
def since(stream, cursor=0):   fresh = [i for i in read_all(stream) if _as_int(i["seq"]) > floor]
```
- **每流独立单调** ✓（`:11` + `since` 按 `seq > cursor` 过滤）
- **游标是 seq 不是时间戳** ✓（`:17-21` 明确写了理由；`since` 实现就是 `seq > floor`）
- **`compact` 记下被丢 seq 区间** ✓：
```python
def compact(stream, keep):   # 返回 {stream, dropped, dropped_from, dropped_to, kept, compaction_seq}
    ...
    marker_payload = {"kind": KIND_COMPACTED, "stream":..., "dropped":..., "dropped_from": first_seq, ...}
```
且「没裁过就**不写压缩事件**——没裁过就不该在审计里留下'这里被裁过'的假痕迹」✓ 这条很硬。

**③ 11.3 cron 投递 → ✅✅ 真在 `create` 时拒**

```python
def validate_deliver(deliver):
    """…**未实现的平台在 ``create`` / ``edit`` 就被拒**（调用方都是先校验再落盘），
       而不是等到任务跑起来才失败：那时用户已经等了半小时。"""
    ...
    if value.startswith(DELIVER_PLATFORM_PREFIX):
        reason = _platform_rejection(platform_name)
        if reason:
            raise ValueError(reason)          ← create 阶段直接抛
```
✓✓ 而且理由写得很好：「静默接受一个没实现的平台会让用户以为"输出已经发到群里了"，实际什么都没发——定时任务的失败模式里，"以为发了"比"明确没发"贵得多」。

**④ 11.6 级联停止 → ✅✅ 真管 3 个子系统 + 真只标记**

```python
def pause_all(data_dir, *, reason=""):
    """紧急停止级联：cron 哨兵 + gateway 哨兵 + 正在跑的任务（只标记）。
    三个子系统，为什么都得管：
    - cron：挡"下一轮 tick 不开始"；
    - gateway：挡"新网关轮次与出站投递"——只停 cron 等于停了半台机器；
    - 正在跑的任务：已经开始的那一轮在**别的进程**里，只能打标记让它自己退。
      跨进程强杀（taskkill / 信号）会引入"杀错 PID / 杀掉外部调度器"的风险，
      那比"多跑完这一轮"贵得多。"""
```
代码：`store.set_paused(True)` + `running.mark_cancelled(reason=...)` + `_gateway_pause_paths` / `write_json` ✓
另：`CascadeStatus.paused` 是「**任一**子系统仍停着就算停着」而不是"全部停着"——理由「漏停一个子系统（比如 gateway 还在发消息）比多报一次'还停着'危险得多」✓ 这个方向选对了。

**⑤ 15.4 / 15.5 → ✅✅ 两处都真**

**15.4 `available=false` 必须写 source/help**：
```python
# metrics_schema.py
# 不可用时**必须**有 source 或 help 说明为什么——空着等于没说。
if not item["available"]:
    _check(bool(item.get("source") or item.get("help")),
           f"v2 的指标 {name} 标了不可用，却没写 source/help 说明原因")
```
✓ 而且 `available=true` 时反向要求有数字 `value` ✓

**15.5 `flush()` 真抛错 + 钩子 11 条**：
- `exporters.py:33`「Hermes `plugins/observability/langfuse/plugin.yaml` 里的 hooks 清单（**11 条**）」
- 实测 `LANGFUSE_HOOKS` = **11**：`pre_api_request, post_api_request, api_request_error, pre_llm_call, post_llm_call, pre_tool_call, post_tool_call, on_session_finalize, on_session_end, subagent_start, subagent_stop` ✓
- `flush()` 在 `:214-221` **`raise NetworkUnavailable(...)`** —— 不是返回假成功 ✓✓

**⑥ 4.10 三级能力元数据 → ✅✅ 七字段真的全 None**

`app/capabilities.py::resolve(model, provider, ...)`（不是我一开始猜的 `model_capabilities`）。用例：
```python
def test_unknown_model_returns_none_for_every_field():        # test_capabilities.py:124
    assert result.value(name) is None, f"{name} 不许有默认值"   # ← 逐字段断言，不是抽查一个
    assert payload["context_window"] is None
    assert payload["supports_tools"] is None
```
✓✓ **「不许有默认值」被写进了断言消息**，这正是你要的那个形状。

**⑦ 9.11 HRR → ✅✅ 权重与 0.78 串扰都有用例钉住**

`app/holographic.py`（不是 `memory_graph.py`——后者 docstring `:12` 明说"HRR 那套**再换实现**"，是另一件事）：
- `DIM = 1024`（实测 `H.DIM = 1024`）✓
- 权重 **fts 0.4 / jaccard 0.3 / hrr 0.3**（`:18-20,63`）✓
- `encode` 用 `hashlib` 派生种子做确定性伪随机相位（**不用 `random`**，要可复现）、`bind`/`unbind` 用 FFT ✓
- **用例**：`test_weights_are_forty_thirty_thirty`(:100) · `test_each_signal_contributes_its_own_weight`(:106) · `test_score_is_exactly_the_weighted_sum_of_its_three_signals`(:113) ✓
- **0.78 串扰**：`test_multi_token_bind_unbind_is_approximate_not_exact`(:73)，docstring「多词叠加后 bind→unbind 有串扰（实测 ≈0.78）：这是 HRR 的统计性质，不是 bug」✓✓

### B. 你的两处主动交代

**① 9.16 的研究错误 → ✅ 更正准确，我独立复核了 13 这个数**

MATRIX 现在写：「Hermes 的 `profile` **不是用户画像**，而是 **profile distribution（可切换的发行版配置）管理器**，且是 **13** 条子命令不是 14」。
**我从 `research/_raw/sub/profile__*.txt` 独立数**：共 **13** 个文件 —— `alias, create, delete, describe, export, import, info, install, list, rename, show, update, use`，**与你列的 13 条逐条相同** ✓✓
状态也诚实：🟡 + 「distribution 那 10 条仍 todo——把它们做成画像增删改就是抄错语义」✓

**这是本轮最值得记的一条**：一个从**研究阶段**就存在的语义错误，被子agent 在实现时顶回来。**错误在被发现前已经存在了 8 轮**，而它不会表现为任何测试失败或计数异常——**只能靠人/agent 对着上游 help 逐字读出来**。

**② 「如实说没有 ≠ 有」这条规矩 → 已落地，且当场抓到 4 条**

你抓的 4 条我抽核了 2 条，都成立：
- **记忆写入门禁**：现在 `check_write` 真的在 `memory_save`（`tools.py:1746`）与 `memory batch`（`memory_extra.py:280`）两处，且注入文本实测 `blocked / threat_scan / prompt_injection_zh` ✓
- **`egress` 6 条**：上轮我核过 = `install/start/stop/restart/reload/disable` 为 `partial` ✓
- 另外两条（`profile show` 的 `_BOUND` 重绑、你自己写错 `Context` API）我未单独复核，如实标注。

### C. 按你的要求抽查「同名不同语义」这一类 —— **发现 1 条：7.17**

你点名要核 `journey` / `plane` / `session` / `memory-graph`，结果如下：

| 名字 | Hermes 侧 | Orbit 侧 | 判定 |
|---|---|---|---|
| `journey` / `learning` / `memory-graph` | **三个是同一命令的别名**（我核过：去掉 `### CMD` 头行后三份 help **完全相同**） | 注册为 `journey` + `aliases=('learning','memory-graph')` ✓；`hermes='journey'` | 别名关系**对** |
| `plane` | Hermes **无**此命令 | `hermes=''`（空）→ 明确标为 Orbit 自有 | ✓ 不占位 |
| `session-search` | Hermes **无**此命令 | `hermes=''` ✓ | ✓ 不占位 |
| `sessions` | 19 子命令 | 26 子命令、`state=partial` | ✓ 如实标 partial |

**但 `journey` 的语义有一处差异，MATRIX 没标出来** ⚠️：
- **Hermes**：`delete` = 「Delete a learned skill (archived) or **memory** by node id」；`edit` = 「Edit a learned skill or memory by node id **in $EDITOR**」——**真的删、真的编辑**
- **Orbit**：`journey.py` 的 `edit(id, note)` / `delete(id)` **只写 `journey-notes.json`**，usage 与 ledger 字节不变（第 7 轮我核过，`journey.py:29,36` 明写「绝不回写 `.usage.json` 或 ledger」「一个字节都不动」）

→ **语义是"标注"而不是"删除/编辑"**。这个取舍本身**是对的**（审计不可篡改，理由写在模块 docstring 里），但 MATRIX 7.17 标的是 **✅**，证据栏只写了 Orbit 做了什么，**没有把它标为与 Hermes 的差异**。
**建议**：7.17 在证据栏加一句「**差异**：Hermes 的 `delete`/`edit` 真删/真编辑；Orbit 只写旁注 + 墓碑标记（审计不可篡改的取舍），语义是"标注"而非"删除"」——或者降为 🟡。**这正是你担心的那一类，只是它是"行为差异未标注"而非"数字错"。**

### D. 其余复核

| 条目 | 证据 | 判定 |
|---|---|---|
| 7.14 bundles | `orbit bundles` 实测 5 子命令 `list/show/create/delete/reload`，`state=done`；`bundles.py` 有 `Bundle`/`Bundles`/`ReloadReport` | ✅ |
| L3 未被破坏 | 8/8 = 100%（独立解析） | ✅ **确认** |
| 8 条剩余 ⬜ | 独立列出与你给的清单**逐条相同** | ✅ |
| `memory batch` 的 BOM 处理 | 带 BOM 的 JSON → 明确报错而非静默当空 | ✅ 好行为 |

---

## 与目标标志的差距

只剩 **8 条 ⬜**，逐条列（这是离「Hermes 有的 Orbit 都有」的最后硬缺口）：

| # | 条目 | 内容 | 备注 |
|---|---|---|---|
| 1 | **2.1** | 单循环 + 传输适配器归一化多 provider | 【推断】层（Hermes 源码在 PYZ 内） |
| 2 | **5.7** | `hook_output_spill`（10,000 / head 500 / tail 500）+ `spill_safety` 拒符号链接 | **轮末看到 `budget.py` 正在被改，可能就在做这条** |
| 3 | **5.14** | 内置工具数量对齐（94 个；Orbit 现 29+） | 差 65 个工具 |
| 4 | **7.16** | `orbit sync`（跨设备技能同步 + 三方合并） | 体量最大的一条 |
| 5 | **7.18** | 技能库规模（Hermes 21 类目 / 91 技能） | 规模类，非机制 |
| 6 | **8.7** | MCP schema 缓存 | **轮末看到 `mcp_schema_cache.py` 新建，正在做** |
| 7 | **8.10** | optional-mcps 预置目录（63 个服务模板） | 纯数据活 |
| 8 | **9.14** | 核心零向量（向量只在 provider 里） | 这是一条"**证明没有**"的条目 |

> 注意 **9.14 是反向条目**（要求"核心**不含**向量"）。它不能靠写代码完成，只能靠"扫依赖 + 扫 import 并给出证据"来完成——建议把它做成一条**可复跑的门禁**（`grep -r 'lancedb|chromadb|faiss' app/` 必须为空），否则它会永远挂在 ⬜ 上。

### 46 条 🟡 的分布（这才是真正的剩余工作）

| 层 | 🟡 | 主要内容 |
|---|---|---|
| L4 | 5 | 4.1 只 16/48 profile、4.2 只 3/5 transport、4.9 只 4/18 槽位 等 |
| L10 | 5 | 10.1 写侧未统一、10.4 压缩阈值未接、10.9 非影子 git、10.11 无 schema_version 表、10.13 19 子命令实现 8 |
| L13 | 5 | dashboard 插件契约、懒发现、`all`/`pack` CLI 等 |
| L1 | 5 | 全局 23 flag、二级/三级实现率、`serve`/`desktop`/`gui` 等 |
| L5 | 4 | 媒体生成、视觉、TTS、kanban、cronjob、桌面窗口、协作消息、HomeAssistant |
| L9 | 4 | USER.md 用量头、8 个外部 provider（`available()` 恒 False）、写入门禁余项、漂移防护 |
| L14 | 4 | tirith 已 ✅；egress 代理、`path_security`/`url_safety` 四件套、脱敏出口未全覆盖 |
| L2 | 3 | 心跳判卡死、swarm 调度、后台学习回路 |
| L7 | 3 | hub 远端索引、`skills_guard` 107 条威胁模式、7.17 语义差异 |
| L12 | 3 | 19 个平台适配器全是占位、peer、monitoring 导出 |
| L6 | 2 | toolset 覆盖 13/32、四层过滤只做平台层 |
| L8 | 2 | OAuth 真流程、`orbit mcp` 剩余子命令 |
| L16 | 1 | dashboard 剩余页面语义 |

---

## 风险与漂移

### R1 🟡 **轮末出现 2 个在飞改动（本轮基线取自轮初，不受影响）**

```
轮初 git status: 0 条目  ✓（与你说的一致）
轮末 git status: 2 条目
   M backend/app/budget.py                 ← 可能在收 5.7 hook_output_spill
   ?? backend/app/mcp_schema_cache.py      ← 明确是 8.7 MCP schema cache
```
→ **本轮所有硬口径均取自轮初的 `HEAD a693224`**；这两个改动落地后 `REGISTRY` 与测试数都会再变。**上轮的教训（轮初轮末各查一次 git）本轮已执行，也确实又抓到了在飞施工。**

### R2 🟠 **7.17 的行为差异未标注（本轮唯一的新发现，见 C 节）**

Hermes 的 `journey delete/edit` **真删/真编辑**，Orbit **只写旁注 + 墓碑**。取舍正确，但 MATRIX 标 ✅ 且未标差异。**这类"行为差异未标注"比数字错更难发现**——它不会让任何测试红，也不会让计数异常。

### R3 🟡 **「同名不同语义」这个类别建议做成常设检查**

本轮按你的要求抽查了 4 个名字，抓到 1 条（7.17）。建议把它固化成一张小表（Hermes 命令名 → Hermes 语义 → Orbit 语义 → 是否一致），**在 MATRIX 里加一列或单独一张附页**。目前 9.16 的教训（`profile` = distribution ≠ 用户画像）已经说明这类错误**能潜伏 8 轮不被发现**。

### R4 🟡 **未验证项（如实标注）**

| 项 | 为什么没验 |
|---|---|
| 9.12 的**端到端** CLI exit 3 | 全新 `--data-dir` 缺 `app.sqlite`，补库要跑 `orbit setup`（改状态）→ 未运行。**门禁逻辑 + 调用点 + 退出码映射三段已核** |
| `profile show` 的 `_BOUND` 重绑修复 | 未单独复核（你交代的第 2 条） |
| 8.6 心跳宽限秒数与退避序列 | 只核到机制与常量引用（连续 4 轮如此） |
| 本轮 11 条转 ✅ 中未抽查的 2 条 | 9 条已核 |
| `orbit chat` 交互、`gateway start`、`cron run`、`backup create/restore`、`config edit`、`doctor --fix/--live` | 会改状态或真发请求 → 未运行 |

---

## 下一轮建议

按优先级：

1. **【P0】给 7.17 补「行为差异」标注**（R2）：Hermes 真删/真编辑 vs Orbit 只写旁注 + 墓碑。**这是本轮唯一一条"✅ 撑得住但语义未对齐"的条目**，而它恰好落在你担心的那一类上。补一句就行，不必降级——取舍是对的，只要说出来。
2. **【P0】把「同名不同语义」做成常设检查**（R3）：建议在 MATRIX 加一列 `语义一致性`（一致 / 有意差异 / 待核），并先过一遍全部 181 条里的**命令类条目**（L1/L7/L8/L10/L11/L12/L13）。9.16 那条潜伏了 8 轮，说明这个风险是真实的。
3. **【P0】9.14 是"证明没有"型条目**，不能靠写代码完成：建议做成可复跑门禁（`grep -r 'lancedb|chromadb|faiss|sentence_transformers' app/` 必须空 + `pyproject.toml` 无相关依赖），跑通即 ✅。否则它会永远挂 ⬜。
4. **【P1】5.7 / 8.7 已在收尾**（轮末看到 `budget.py` 改动与 `mcp_schema_cache.py` 新建）：这两条收掉后 ⬜ 只剩 6 条，其中 4 条是**规模/数据类**（5.14 工具数、7.18 技能数、8.10 63 模板、7.16 sync）。
5. **【P1】46 条 🟡 才是真正的大头**：其中 **L4 的 5 条**（4.1 只 16/48 profile、4.2 只 3/5 transport、4.9 只 4/18 槽位）**基本都是"照 06 调研的表抄数据"**，纯数据活、无新逻辑，是性价比最高的一块。
6. **【P1】L10 的 5 条 🟡 里有 3 条是"取舍类"**（10.1 非同库同表、10.9 非影子 git、10.11 无 schema_version 表）——这些**可能永远不该变 ✅**。建议给这类加一个状态 **🟢「有意差异」**，否则完成度永远卡在 70% 出头，且读者分不清"没做"和"故意不做"。
7. **【P2】给每条 ✅ 补「验证命令」列**：本轮 9.12（in-process 调 `check_write`）、9.11（`H.DIM` + 三个权重用例）、4.10（`resolve()` 逐字段 None）都是可一键复跑的；把这种断言补到其余条目，下轮监视不必重读源码。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：连续 7 轮未核。本轮顺带看到 `nodes.py:139 context_meta.setdefault("compressed", {})` 仍是空占位——正是那 15 条之一。

---

*本报告由 monitor 角色只读生成。本轮 11 条转 ✅ 复核 9 条（82%），你点名的 7 条**全部撑得住**；两处交代**全部成立**（9.16 的 13 条子命令我从 `_raw` 独立数过，逐条相同）；按你的要求抽查「同名不同语义」抓到 **1 条（7.17 行为差异未标注）**。9.12 的端到端 CLI 因需先建库而未跑，已如实标注。未验证项单列，不混进完成项。*
