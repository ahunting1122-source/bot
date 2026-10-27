# 对标进度报告

**更新时间**：2026-10-27 23:10（本机实测时钟，只读取证）
**本轮轮次**：**N = 1**（首次运行；目录内此前无 PROGRESS.md）
**性质**：只读核对。本轮未修改任何代码、未改任何 research 文件、未安装依赖、未启动后端、未跑全量 pytest（仅 `pytest tests/test_cli.py -q`）。
**基准**：Hermes Agent runtime 0.21.0-cn.18（`E:\Hermes Agent CN Desktop`）· **目标仓**：Orbit `E:\bbbooottt`

---

## 总体完成度

### **1 / 35 项 = 2.9%**

**口径（必须连同数字一起读）**：

| 项 | 说明 |
|---|---|
| 条目来源 | `MATRIX.md` / `ROADMAP.md` **均不存在**，本轮改从 research 各篇的「Orbit 必补 top N」清单汇总，**属代理口径** |
| 分母构成 | CLI 入口层 7 项（已登记命令逐条对标核对）+ 持久化 5（03:562）+ 技能 6（04:1231）+ 插件/网关 6（05:1408）+ 模型 5（06:691）+ 记忆 6（07:521）= **35** |
| **不在分母内** | 工具层（`02-tool-layer.md` **未给** top-N 清单）、核心循环层（`09-core-loop-layer.md` **缺失**）→ 真实完成度**只会更低**，不会更高 |
| 判定标准 | **完成 = 该条目的 Hermes 契约在 Orbit 有实现且有验证证据**；有骨架但缺关键项记「部分」（**不计分**）；查无实据记「未开始」 |
| 计数结果 | 完成 **1** · 部分 **11** · 未开始 **23** · 合计 35 |
| 另一个更硬的口径 | Hermes 顶层命令 **77** 个（`research\_raw\cmd-*.txt` 实测 77 个），Orbit 已注册顶层命令 **7** 个 → 命令面覆盖率 **7/77 = 9.1%**，其中选项面完全对齐的 **1/77** |

> 一句话：**CLI 骨架刚落地、其余各层「必补项」全线未开工**。当前 2.9% 不是「快做完了」，而是「分母刚被定义清楚」。

---

## 分层完成度

成熟度参照 `research\08-orbit-current-state.md` §2 的 16 层划分与「完整/部分/缺失」口径；完成度按上表 35 项主口径计分。

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| CLI 入口层 | 1 / 7 | 14% | 进行中 |
| 工具层 | — / — | — | **有风险**（02 未给必补清单，无法计分；08 §6.3 列出 9 项工具缺口作为代理） |
| 持久化与会话层 | 0 / 5 | 0% | 未开始 |
| 技能层 | 0 / 6 | 0% | 未开始 |
| 插件 / 网关层 | 0 / 6 | 0% | 未开始 |
| 模型适配层 | 0 / 5 | 0% | 未开始 |
| 记忆层 | 0 / 6 | 0% | 未开始 |
| 核心循环层 | — / — | — | **未开始**（09 产物缺失，无清单可计分） |
| cron 自动化 | 0 / 1 | 0% | 未开始（整层缺失，grep 零命中） |
| 安全与审批分级 | 0 / 1 | 0% | 未开始（v1 有意零确认，见 08 §7.1） |
| 可观测性（指标/追踪） | 0 / 1 | 0% | 未开始（只有事件流） |

---

## 本轮抽查证据

**抽样率**：主口径 35 项中逐条取证 **17 项 = 48.6%**（要求 ≥30%）；CLI 入口层 **7/7 = 100%**；已注册 12 个命令节点 **12/12** 核了 handler 与测试。

### A. orbit CLI 只读取证（命令输出摘要）

| 检查 | 命令 | 结果 |
|---|---|---|
| 命令面 | `python -m app.cli --help` | 退出码 **0**；7 个顶层命令 `status doctor dump logs prompt-size completion config`；6 个归类中 `run` / `platform` / `ext` 三组**空** |
| 注册表规模 | `from app.cli import REGISTRY` | 顶层 **7**，`parity_rows()` 展平 **12** |
| 状态分布 | 同上 | `done` **12 / 12**，`P0` 12，分组 `core` 10 + `ops` 2 —— **自评 100% done**（见风险 R1） |
| status | `python -m app.cli status --json` | 退出码 **0**；真实数据：conversations 21 / messages 95 / tasks 49 / skills 3 / memories 1 / chunks 134 |
| doctor | `python -m app.cli doctor --json` | 退出码 **0**；8 项检查全「正常」，`failures: []`，`warnings: []` |
| 测试 | `pytest tests/test_cli.py -q` | **15 passed in 0.74s**，退出码 0 |
| handler 落地 | `grep 'handler=' app\cli\commands\core.py` | 11 处显式 handler（`:685,693,701,709,721,729,747,756,765,777,786`），第 12 条 `config` 复用 `config_list`；`state="done"` 12 处（`:687-788`）→ **handler 真实存在，非空壳** |
| 入口可执行性 | `Get-Command orbit` | **未找到** → `[project.scripts] orbit = "app.cli.main:main"`（`pyproject.toml:30-31`）已声明但**未安装**，`orbit` 命令装不出来 |

### B. 逐条对标抽查（Hermes help 实测 vs Orbit help 实测）

| # | 条目 | 证据 | 判定 |
|---|---|---|---|
| 1 | **CLI `completion`** | Hermes `{bash,zsh,fish}`（`_raw\cmd-66-completion.txt`）vs Orbit `{bash,zsh,fish,powershell}`（实测）→ 超集 | ✅ **完成（唯一）** |
| 2 | CLI `logs` | Hermes 6 选项 `-n/-f/--level/--session/--since/--component` + 5 个日志目标；Orbit 仅 `--file/--tail`（`core.py:709` `run_logs`） | 部分：**4/6 选项缺**，无 follow/level/session/since/component |
| 3 | CLI `config` | Hermes 9 子命令 `show,edit,get,set,unset,path,env-path,check,migrate`（`_raw\cmd-42-config.txt`）；Orbit 5：`list,get,set,unset,path`（`core.py:747-786`） | 部分：缺 `edit/env-path/check/migrate`（`list`≈`show`） |
| 4 | CLI `status` | Hermes `--all/--deep`（`_raw\cmd-21-status.txt`）；Orbit 无选项（`core.py:685`） | 部分：**2/2 选项缺** |
| 5 | CLI `doctor` | Hermes `--fix/--live/--ack`（`_raw\cmd-32-doctor.txt`）；Orbit 无选项（`core.py:693`），8 项静态检查 | 部分：**3/3 选项缺**，无 `--live` 真调用探针 |
| 6 | CLI `dump` | Hermes `--show-keys` + 纯文本可粘贴；Orbit 无该选项、输出为 JSON 快照（`core.py:701`） | 部分：缺 `--show-keys`，形态不同 |
| 7 | CLI `prompt-size` | Hermes `--platform` + `--json`；Orbit 仅 `--json`（`core.py:721`） | 部分：缺 `--platform` |
| 8 | 持久化 #2 压缩谱系 | `grep parent_session_id\|end_reason\|compaction`（app + migrations）= **0 命中** | 未开始 |
| 9 | 技能 #1 多根 + tier | `app\skills.py:89` `load_skill(root, name)` **单根**；`grep external_dirs\|tier` = 0 | 未开始 |
| 10 | 技能 #5 curator 生命周期 | `grep curator` = 0 命中 | 未开始 |
| 11 | 模型 #4 fallback/credential_pool/429 | `grep fallback_model\|credential_pool\|FailoverReason` = **0** | 未开始 |
| 12 | 记忆 #3 CJK 双索引 | `indexing.py:72,103,452` 有 trigram，但仅服务 **chunks（RAG 文档）**；messages/memories 无 trigram FTS | 部分：用途不匹配 |
| 13 | 记忆 #4 零 LLM `session_search` | `grep session_search` = **0** | 未开始 |
| 14 | 插件 G1–G6 | `grep plugin\|plugin_loader\|hook_registry` 于 `app\*.py` `app\graph\*.py` `app\cli\**` `migrations\**` = **0** | 未开始（整层） |
| 15 | 网关 G4 进程模型 + 4 状态文件 | `grep \bgateway\b` = **0** | 未开始（整层） |
| 16 | cron 自动化 | `grep \b(cron\|scheduler\|apscheduler)\b` = **0** | 未开始（整层） |
| 17 | 核心循环 #5 子 Agent | `policies.py:209,586,603` 有 Protocol + `available=False`；`grep \.subagents` 于 app 全量 = **0 消费点** | 未开始（**与 08 §7#1 一致，文档可信**） |

### C. 本轮复核为「文档说法成立」的点（正面证据，避免只报坏消息）

- `08 §7#1`「`policies.subagents` 零消费」→ 本轮独立 grep 复核 **0 消费点**，一致。
- `08 §2`「工具层/技能层/MCP/记忆/持久化 = 完整」→ handler 与迁移文件确实存在，`status --json` 能读到真实数据（21 会话 / 134 chunks），非空壳。
- `08` 称 `[project.scripts]` 已声明但未提交/未安装 → `pyproject.toml:30-31` 存在、`Get-Command orbit` 未找到，**两点都成立**。

---

## 与目标标志的差距

目标标志 =「Hermes 有的功能 Orbit 都有，**逐项有验证证据**」。当前距标志的核心差距是**层不在，而不是层不够好**。

### Top 10 未完成项（按「不补就无法宣称对齐」排序）

| # | 未完成项 | 现状证据 | 缺口量级 |
|---|---|---|---|
| 1 | **gateway 多平台接入 + 4 个状态文件**（`gateway-starts.log` / `processes.json` / `spawn-ledger.json` / `channel_directory.json`） | `grep gateway` = 0（整层） | Hermes 22 个平台适配器；Orbit 0 |
| 2 | **cron 定时子系统**（调度器 + 持久化作业 + trigger + 无人值守边界） | `grep cron\|scheduler` = 0；Hermes `cron` 有 12 子命令 | 整层 |
| 3 | **插件系统**（`plugin.yaml` 16 字段 manifest + `ctx.register_*` 19 种 + 17 hook 事件） | `grep plugin` = 0；Hermes 103 插件 / 18 组 | 整层 |
| 4 | **模型 provider 适配层**（`ProviderProfile` 注册表 + 五态 `api_mode` + per-model 路由 + 推理线格式翻译表） | `grep ProviderProfile\|api_mode` = 0；Orbit 唯一 `ChatOpenAI`（`agent.py:277-288`） | Hermes 39 目录 / 48 profile；Orbit 1 条 |
| 5 | **失败容错三件套**（fallback 链 + credential_pool + 429 熔断） | `grep fallback_model\|credential_pool` = 0 | 整块 |
| 6 | **子 Agent / 委派**（深度/角色/预算/工作树隔离） | Protocol 有、消费点 0 | 整块 |
| 7 | **持久化：压缩谱系 + 幂等投递账本**（`parent_session_id` / `delivery_obligations` / `idempotency_key`） | grep 0 命中；Orbit 仅 `Operation` 幂等 | Hermes 40 表 vs Orbit 15 表 |
| 8 | **技能：多根 tier + curator 生命周期 + hub 分发** | `skills.py` 单根；`grep curator` = 0 | Hermes 91 bundled + 36 用户；Orbit 3 手写 |
| 9 | **记忆：零 LLM `session_search` + 写入门禁/漂移防护** | `grep session_search` = 0 | Hermes 4 形态 / ~1.7ms；Orbit 无 |
| 10 | **CLI 剩余命令面** | Hermes 77 顶层 + 295 子命令 + 36 三级；Orbit 7 顶层 + 5 子命令 | **差 70 个顶层命令**，其中 `run/platform/ext` 三组零实现 |

> 附：`08 §6` 的 30 条「明确缺失」与本表交叉，其中**未在本表出现的**还有：多 profile/多用户隔离、工具暴露四层过滤、AGENTS.md/HERMES.md 项目文件加载、Anthropic prompt caching、OCR/ASR、docx/xlsx/pptx 解析、computer use、TOTP/OAuth 二次验证、审计时间线、指标追踪、CI/CD、backup/import。

---

## 风险与漂移

### R1 🔴 **文档/注册表声称完成、但经不起 Hermes 命令面对照的条目（本轮最大风险）**

`backend/app/cli/spec.py` 的 `state` 字段把 **12/12 个节点全部标为 `done`**（`core.py:687,695,703,711,723,731,741,749,758,767,779,788`），`parity_rows()` 于是产出「12 done / 0 partial / 0 todo」= 表面 100%。
**但按 Hermes 同名命令的选项/子命令面逐条核对，7 个顶层命令中只有 1 个（`completion`）完全对齐，6 个是子集**：

| 命令 | 自评 | 实测缺项 |
|---|---|---|
| `logs` | done | 缺 `-f/--level/--session/--since/--component` 与 5 个日志目标 |
| `config` | done | 缺 `edit/env-path/check/migrate` 4 个子命令 |
| `status` | done | 缺 `--all/--deep`（2/2） |
| `doctor` | done | 缺 `--fix/--live/--ack`（3/3） |
| `dump` | done | 缺 `--show-keys`；输出形态（JSON）与 Hermes（可粘贴纯文本）不同 |
| `prompt-size` | done | 缺 `--platform` |

**危害**：任何从 `REGISTRY.parity_rows()` 自动生成的完成率都会虚高 ~6 倍（12/12 vs 实际 1 个真对齐）。这是本项目的**口径污染源**，必须在生成 MATRIX.md 前先修掉。
**建议判定**：这 6 条应降为 `partial` 并附缺项清单；`state` 的语义要写死为「与 Hermes 同名命令的**选项面 + 子命令面 + 输出契约**全部覆盖」。

### R2 🟠 **完成率口径陷阱：注册表规模 ≠ 对标规模**

`GROUPS` 声明 6 个归类（`spec.py:25-32`），实际只有 `core`(10) 与 `ops`(2) 有命令，`run`/`platform`/`ext` 三组**空**。若以「已登记命令的 done 率」计分，永远是 100%；只有以 **Hermes 77 个顶层命令**为分母才有意义（现为 7/77 = 9.1%）。**分母口径必须在 MATRIX.md 里钉死。**

### R3 🟠 **research 产物缺口 → 两层无法计分**

- `01-cli-surface.md`：**缺失**。CLI 全量面（Hermes 77 顶层 + 295 二级 + 36 三级 = 411 份 `--help` 实录已采到 `_raw\`）尚无汇总文档 → CLI 分母只能近似用 77。
- `09-core-loop-layer.md`：**缺失**。核心循环层无清单 → 该层完成度无法计分。
- `02-tool-layer.md`：**存在但体例不一致**——它是全 7 篇里**唯一没有「Orbit 必补 top N」章节**的（03:562 / 04:1231 / 05:1408 / 06:691 / 07:521 都有），只有 §10「其他值得记录的机制」。工具层因此**无清单可计分**。

### R4 🟠 **全部进度处于 untracked 状态，无版本证据**

`git status --porcelain`（`E:\bbbooottt`，HEAD `0964821`）：

```
 M backend/pyproject.toml
?? backend/app/cli/
?? backend/tests/test_cli.py
?? handoff/hermes-parity/
?? scripts/parity_report.py
```

CLI 实现、测试、**整份调研产物（含本报告）**、`parity_report.py` 全部未入库。任何一次误删/回滚都会让「已完成」归零，且无法用 diff 证明进度。**这是当前最容易一夜清零的风险。**

### R5 🟡 **并发写入导致「缺失」结论会过期**

`backend/app/cli/` 文件 mtime 23:03→23:07、`test_cli.py` 23:07、`parity_report.py` 23:08、research 文档 23:05→23:08 **交错**——调研与实现在同一时间窗并行施工。本轮已实测到一次**反向漂移**：`08` 早前称「无 `[project.scripts]`」，实际 `pyproject.toml:30-31` 已有。
**规则**：凡「某能力缺失」的结论，必须与 `git status` / mtime 同轮复核，否则只能标「截至 23:1x 未发现」。

### R6 🟡 **文档声称「未消费」，本轮复核确认（正例，记录以免被误当风险）**

`08 §7` 的 15 条「已声明未消费」里本轮抽查了 1 条：`policies.subagents` 全仓消费点 = **0**，与文档一致。**这说明 08 的证据质量可信，风险不在 08，而在 `spec.py` 的自评状态字段（R1）。**

### R7 🟡 **口径不一致项汇总**

| 口径 | 冲突点 |
|---|---|
| 「完成」定义 | `spec.py` 用「有 handler + 有测试」→ 12 done；本报告用「Hermes 契约全覆盖」→ 1 done。**两套标准必须二选一或分层标注** |
| 工具数 | Hermes 三口径 86/90/94（02:21-27）；Orbit 28 静态 + MCP 动态（08 §3）——不同口径不可直接相除 |
| 表数 | Hermes `state.db` 40 表 vs Orbit 15 表（08 §4）；但 Orbit 另有 2 个 FTS + 2 向量表，需按「逻辑表」还是「物理表」统一 |
| 测试数 | `08` 记 279 tests（2026-10-13 基线）+ 新增 `test_cli.py` 15 条 = 294 未复核；本轮**只验证了 `test_cli.py` 15 passed**，全量未跑（按规则不跑） |

---

## 下一轮建议

按优先级（**先修口径，再补层**——口径不清时补层无法验收）：

1. **【P0】修 `spec.py` 的 `state` 语义并降级 6 条**：把 `logs/config/status/doctor/dump/prompt-size` 从 `done` 改为 `partial`，`description` 里写死缺项（本轮 R1 表格可直接抄）。这是让所有后续完成率可信的前提。
2. **【P0】入库**：提交 `backend/app/cli/`、`backend/tests/test_cli.py`、`handoff/hermes-parity/`、`scripts/parity_report.py`，并跑一次 `uv sync` 让 `orbit` 真正可执行（现在 `Get-Command orbit` 找不到）。
3. **【P0】补 research 缺口**：`01-cli-surface.md`（411 份 `--help` 实录的汇总，钉死 CLI 分母）与 `09-core-loop-layer.md`（核心循环层清单）。
4. **【P1】补 `02-tool-layer.md` 的「Orbit 必补 top N」章节**，与 03–07 体例对齐，否则工具层永远无法计分。
5. **【P1】用 `REGISTRY.parity_rows()` 生成 `MATRIX.md` 作为唯一清单来源**：先只读核对 `scripts/parity_report.py`（2,456 B，23:08 新增）实际输出什么、是否已含 Hermes 侧分母；避免「文档 MATRIX + 代码 REGISTRY」两套口径并存。
6. **【P1】下一批命令按 P0 缺口排序**：`run` 组（`chat`/`serve`/`gateway`/`cron`/`delegate`）是最大空洞；建议先做 `sessions`（Hermes 19 子命令，Orbit 已有会话表可直接消费）或 `cron`（可先做「只读 `list`/`status`」最小可用面）。
7. **【P2】建立「每条 done 必须挂一条测试 + 一条命令输出」的证据表**（照 03 §7#5 的 `verification_events` 思路），把「声称通过」变成「有证据行」。
8. **【P2】下一轮复核 `08 §7` 剩余 14 条未消费项**是否被接线（本轮只抽了 1 条），并复核 `08 §6` 30 条里哪些已被本批施工消解。

---

*本报告由 monitor 角色只读生成。所有数字均可按上文命令复现；凡未亲自验证的一律标注「未验证/代理口径」，未为好看加分。*
