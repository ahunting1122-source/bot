# 对标进度报告

**更新时间**：2026-10-28 00:22（本机实测时钟，只读取证）
**本轮轮次**：**N = 4**
**性质**：只读核对。未修改任何代码 / research / MATRIX / ROADMAP 文件；未装依赖；未启动后端；未跑 `config set` / `doctor --fix` / `config edit` / `doctor --live` / `cron run` / `gateway start` / `backup restore` 等会改状态或真发请求的命令。全量 pytest、ruff、mypy 已按授权运行（均只读）。
**基准**：Hermes Agent runtime 0.21.0-cn.18 · **目标仓**：Orbit `E:\bbbooottt`（HEAD `d68dfb4`，工作树干净）

---

## 总体完成度

### **69 / 181 项 = 38.1%**

| 项 | 说明 |
|---|---|
| 清单来源 | `MATRIX.md` 主清单，脚本逐行解析：**181 行**（L1–L16），**编号无重复**（已核 181/181 唯一） |
| 完成 | **✅ 69 条**（本轮复核 21 条 = 30.4%，含你点名的 10 条全部） |
| 部分 | 🟡 **30 条**（不计分） |
| 未开始 | ⬜ **82 条** |
| 表头一致性 | MATRIX 表头「181 / ✅69 / 🟡30 / ⬜82 / 38.1%」——**与我的脚本统计逐字一致** ✅ |

### 本轮变化：**✅ 43 → 69（+26）· 🟡 13 → 30（+17）· ⬜ 125 → 82（−43）**

| 层 | 上轮 | 本轮 | 变化 |
|---|---|---|---|
| L7 Skill / curator | 0/18 | **9/18** | +9（skill_index + curator 整层） |
| L12 Gateway / 多平台 | 0/11 | **7/11** | +7（platforms + gateway） |
| L13 插件 | 0/10 | **5/10** | +5（plugins + hooks） |
| L14 安全 | 3/10 | **4/10** | +1（secrets/redact/audit，其中 14.3 降为 🟡） |
| L9 记忆 | 3/16 | **5/16** | +2（memory_prompt 9.1 / 9.3） |
| L1 CLI | 7/17 | **10/18** | +3（1.8 `-z` / 1.9 `chat` / 1.18 行号顺延） |
| L15 可观测 | 4/7 | **4/6** | 持平（15.1 加了结构化日志+轮转） |
| 其余 | — | — | 条目数微调（L15 7→6） |

> **本轮一句话**：这是迄今**证据质量最高**的一轮。你点名的 10 条里 **9 条完全撑得住**（其中 7.2 / 12.1 / 12.3 / 13.6 / 13.7 是"逐条对齐 + 每条一个专用用例"的硬证据），**只有 12.5 一条指代错了平台名**（数字对、名字错）。剩余错账不在代码，在 **MATRIX 的 1.5 / 1.17 两行计数**。

---

## 分层完成度

| 层 | 完成/总数 | 百分比 | 状态 |
|---|---|---|---|
| L1 入口 / CLI | 10 / 18 | 55.6% | 进行中（🟡2） |
| L2 Agent 核心循环 | 1 / 10 | 10.0% | 未开始 |
| L3 上下文装配 | 5 / 8 | 62.5% | 进行中 |
| L4 模型适配 | 1 / 13 | 7.7% | **有风险**（🟡4 全是「已实现未接线」） |
| L5 工具层 | 8 / 19 | 42.1% | 进行中（🟡1） |
| L6 工具集 / 注册 | 2 / 5 | 40.0% | 进行中（🟡2） |
| L7 Skill / curator | 9 / 18 | 50.0% | 进行中（🟡5） |
| L8 MCP | 0 / 10 | 0% | 未开始 |
| L9 记忆层 | 5 / 16 | 31.3% | 进行中（🟡2） |
| L10 持久化 / 会话 | 2 / 15 | 13.3% | 进行中（🟡1） |
| L11 cron | 3 / 6 | 50.0% | 进行中（🟡2） |
| L12 Gateway / 多平台 | 7 / 11 | 63.6% | 进行中（🟡3） |
| L13 插件 | 5 / 10 | 50.0% | 进行中（🟡4） |
| L14 安全 / 审批 | 4 / 10 | 40.0% | 进行中（🟡4） |
| L15 可观测性 | 4 / 6 | 66.7% | 进行中 |
| L16 Web UI / 前端 | 3 / 6 | 50.0% | 进行中 |
| **合计** | **69 / 181** | **38.1%** | — |

**唯一整层为零**：**L8 MCP（10 条）**。次弱：L2（9 ⬜）、L10（12 ⬜）、L4（8 ⬜ + 4 🟡）。

---

## 本轮抽查证据

**抽样率**：69 条 ✅ 中**复核 21 条 = 30.4%**（要求 ≥30%），其中你点名的 10 条**全部复核**。⬜ 复核 1 条（11.5 已于上轮）、🟡 复核 6 条。

### A. 硬口径（你给的四个数字，两个对、两个不对）

| 你的口径 | 实测 | 判定 |
|---|---|---|
| `REGISTRY` 75 顶层 | `len(REGISTRY.all())` = **75** | ✅ 对 |
| `REGISTRY` **411** 展平 | `len(REGISTRY.parity_rows())` = **417** | ❌ **不对，是 417** |
| `done 137 / partial 3 / todo 271` | **done 137 ✅ · partial 9 ❌ · todo 271 ✅** | 部分对（partial 是 9，不是 3；137+9+271 = 417） |
| 测试 **1067 passed / 1 failed / 1 skipped** | `1 failed, 1067 passed, 1 skipped in 106.15s` | ✅ **完全一致** |
| ruff clean | `All checks passed!`（exit 0） | ✅ 对 |
| mypy 68 文件 clean | `Success: no issues found in 68 source files`（exit 0） | ✅ 对 |
| 并行负载假失败 | 本轮**未出现**（全量 1067 passed，无 `NameError: Spec` 之类） | ✅ 结果稳定 |

> 411 / partial 3 这两个数字来自 **MATRIX 1.5 与 1.17 两行的旧值**（见风险 R1），不是你记错——ROADMAP 这轮已经改对了（写 417 / 137 / 9 / 271，与我实测逐字一致）。

### B. 你点名的 10 条，逐条判定

**① 7.2「16 字段 + 11 条硬校验」→ ✅✅ 完全撑得住**

- `ALLOWED_FRONTMATTER_FIELDS` **len = 16** ✓
- `validate_frontmatter()`（`skill_index.py:154`）docstring 逐条编号 **①–⑪ = 11 条硬校验**，且每条对应一个 `return FrontmatterReport(ok=False, …)` 分支：
  ① 空内容 · ② 剥 BOM · ③ 必须 `---` 开头 · ④ 必须闭合 · ⑤ YAML 可解析 · ⑥ 必须是 mapping · ⑦ `name` 必填（本实现额外判 ≤64）· ⑧ `description` 必填 · ⑨ `description ≤ 1024` · ⑩ **仅新建** `≤ 60` · ⑪ body 非空
- **每条硬校验一个用例**（实证）：`test_check_1_empty_content`(:141) → `test_check_2_bom_is_stripped`(:146) → `test_check_3…`(:152) → `:157` → `:162` → `:167` → `:172` → `:177` → `:182` → `test_check_10_new_skill_description_budget`(:190) → `test_check_11_body_required`(:200)。**11 个专用用例，一条不少** ✓
- 额外加分：`test_whitelist_matches_spec_16_fields`(:87) 直接把 16 字段白名单钉进测试。

**② 7.8「锚点不含 mtime」→ ✅✅ 有专门用例**

- `curator.py:202` `latest_activity_at()` 遍历 `(last_used_at, last_viewed_at, last_patched_at)`；`:210-215` `anchor()` = `latest_activity_at() or parse_iso(created_at)`，docstring 明写「**刻意不含文件 mtime**」，模块 docstring `:22-24` 还给了理由（mtime 会被编辑器/同步盘/git checkout/解压改）。
- **专门用例在**：`tests/test_curator.py:203 test_anchor_ignores_file_mtime` ✓ + `:221 test_anchor_falls_back_to_created_at` ✓
- 判定：**"改新 mtime 仍应归档"这条断言真实存在**。

**③ 12.1「孤儿锁回收」→ ✅✅ 真的回收，不是"锁在就拒"**

- `gateway.py:344`：「锁在、holder 已死**或** PID 被复用（启动时间对不上）→ **回收孤儿锁**并接管」
- `acquire()`(:377) → `:391` 「死进程 / PID 复用 → 孤儿锁，回收后接管」→ `:395 reclaimed=True`，`:397` reason 写「回收孤儿锁（pid=… 已不在或已被复用）」
- 身份校验不是只看 pid：`_windows_start_time()`（`OpenProcess` + 内核时间，`:165-198`）、`_posix_start_time()`（`/proc/<pid>/stat` 第 22 字段，`:200-222`）；`:358` 明确「启动时间对不上 = PID 被复用，视为已死」
- 判定：**比"锁存在就拒绝"强一档，是 (pid, host_start_time) 三元组级别的互斥**。

**④ 12.3「7 必选 + 5 可选 + 5 交互 UX」→ ✅✅ 数字逐个对上**

```
PlatformAdapter.__abstractmethods__ = {start, stop, send_text, send_media, health, standalone_sender_fn} = 6
REQUIRED_ATTRS = ('name',)                      → 6 方法 + name 属性 = 7 必选 ✓
OPTIONAL_SEND_METHODS = 5  (typing/reaction/edit/delete/buttons) ✓
INTERACTION_METHODS  = 5  (clarify/审批/斜杠/模型选择/选择器)   ✓
EDGE_HOOKS           = 5  (connect/disconnect/error/rate_limited/message) ✓
```
- **不支持的 UX 是返回 `NotSupported`，不是抛异常** ✓（5 个方法体全是 `return self._not_supported(...)`；类 docstring 明写「默认返回 NotSupported（降级为纯文本，不抛）」）
- 按钮回调前缀：实测 `cl` / `appr` / `sc` / **`ea`**（比 Hermes 的 3 个多一个 `ea`=exec approval，属超集）
- `standalone_sender_fn` 被**设成抽象方法**（而非 Hermes 的可选 hook），理由写在 docstring：漏实现就 `TypeError`，而不是 cron 半夜静默丢消息——**这是主动加严，不是缩水**。

**⑤ 12.5「22 枚举 / 只实现 3 个」→ ⚠️ 数字对，但**名字错**

| 检查 | 结果 |
|---|---|
| 平台枚举数 | **22** ✓ |
| `implemented=True` 恰好 3 个 | ✅ 恰好 3 |
| 其余 19 个发消息抛 `PlatformNotImplemented` | ✅ `UnimplementedAdapter` 每个发送方法都 `raise self._refuse()`，docstring 明写「不给空成功」 |
| **哪 3 个？** | ❌ **MATRIX 写 `console` / `file` / `webhook`；实际是 `a2a` / `ntfy` / `raft`** |

`console` / `file` / `webhook` 是 Orbit **自有的本地适配器类**（`ConsoleAdapter` / `FileAdapter` / `WebhookAdapter`），**根本不在那 22 个平台枚举里**。数字 3 巧合相同，指代完全不同。🟡 不计分，**但证据文字是错的，必须改**。

**⑥ 13.6「--force 不覆盖 dangerous」→ ✅✅ 断言真的在**

```python
def test_force_never_overrides_dangerous(tmp_path):          # test_plugins.py:561
    """硬规矩：`--force` 不覆盖 dangerous（这是安全底线，不是可配置策略）。"""
    allowed, reason = plugins.should_allow_plugin_install(result, force=True)
    assert allowed is False
    assert "--force" in reason and "dangerous" in reason
```
另有 `test_install_local_refuses_dangerous_even_with_force`(:569)：断言 `PluginGuardError` 被抛 **且** `not (data_dir/"plugins"/"p-evil").exists()`（被拒时不留目录）✓

**⑦ 13.7「四种限额」→ ✅✅ 四种各一个拒绝用例**

`test_structure_limit_too_many_files`(:581) · `test_structure_limit_total_size`(:591) · `test_structure_limit_single_file`(:603) · `test_structure_limit_symlink_escape`(:636)。
常量实测：`MAX_PLUGIN_FILE_COUNT = 400`、`MAX_PLUGIN_TOTAL_SIZE_KB = 10 * 1024`、单文件 1 MB；逃逸判定用 `resolve()` 而非 `is_symlink()`（`plugins.py:1215-1217` 解释了为什么只认符号链接会漏），另有 `broken_symlink` / `symlink_escape` 两个 pattern id ✓

**⑧ 14.3「标 🟡 说未接线」→ ✅ 诚实**

| 文件 | `redact` 命中数 |
|---|---|
| `app/tools.py` | **0** ✓ |
| `app/policies.py` | **0** ✓ |
| 全 `app/` 目录 | 只有 `redact.py` / `secrets.py` / `security_audit.py` / `hermes_parity.py` |

→ **「未接线到 `tools.as_tool_text` / `policies.record`」属实**，🟡 定级正确。

**⑨ 1.9「`orbit chat`」→ ✅ 成立**

- `chat.py` 里 `mcp` 命中 **3 处，全部是文字**：`:9-10` docstring 明写「**不接 MCP**：`MCPManager.start()` 会拉起外部进程，在一次性 CLI 会话里留下孤儿进程的风险高于收益」，`:282` help 文本。**无任何 MCPManager 代码调用** ✓
- `-z` 是**全局** flag（`main.py:66-67` `-z/--oneshot`），`:162-163` 把 `orbit -z "…"` 路由成 `orbit chat -z "…"`（对齐 `hermes -z`）✓
- 用例：`test_oneshot_prints_only_the_final_answer`(:95) · `test_global_oneshot_flag_is_equivalent`(:103) · `test_oneshot_json_payload`(:112) ✓

**⑩ 15.1「结构化日志」→ ✅✅ lifespan 真调了**

```python
main.py:108  @asynccontextmanager
main.py:109  async def lifespan(app: FastAPI):
main.py:113      logging_setup.configure(settings.data_dir, level=settings.log_level)
main.py:182  app = FastAPI(title="Orbit · 个人助手", lifespan=lifespan)
```
常量实测：`MAX_BYTES = 5_242_880`（**5MB**）× `BACKUP_COUNT = 5` ✓ 正好是「5MB×5 轮转」；`errors.log` 独立 2MB×3 只收 WARNING+ ✓；`task_context` 用 contextvar ✓

### C. 顺手核的 matrix_stats 门禁

**`duplicate_ids()` 逻辑正确，且比"只查重复"更严**：

```python
def duplicate_ids(text): ...   # matrix_stats.py:52
def main() -> int:
    dupes = duplicate_ids(text)
    if dupes:
        print(f"条目编号重复：{'、'.join(dupes)}", file=sys.stderr)
        return 1                      # ← :84 无条件 exit 1，不受 --check 影响
```
- 直接调用实测：喂 4 行含重复的文本 → `['1.1', '1.2']`；干净输入 → `[]` ✓
- **门禁触发路径已在源码确认（dupes 非空即 return 1）**。
- ⚠️ **端到端未实跑**：`matrix_stats.py` 只读固定的 `MATRIX.md`（无路径参数），要实跑 exit 1 必须改 MATRIX.md —— 属禁改区，**故只做了函数级 + 源码级验证，不宣称端到端通过**。
- 附带验证：真 MATRIX 跑 `matrix_stats.py` → 输出「条目 181：✅ 69 · 🟡 30 · ⬜ 82（38.1%）」exit 0 ✓，与表头一致。

### D. 其余抽样复核（代码级，全部撑得住）

| 条目 | 证据 | 判定 |
|---|---|---|
| 9.1 | `MEMORY_MAX_CHARS = 2200` ✓；`HEADER = "长期记忆（MEMORY）[{pct}% — {used}/{limit} 字符]"` ✓；`render` / `render_entry` / `render_block` | ✅ |
| 13.1 | `MANIFEST_FIELDS = 16` ✓、`PLUGIN_KINDS = 4` ✓、`KIND_TIMING = 4` ✓ | ✅ |
| 13.2 | `ENV_FIELDS = 6` ✓（富字典 6 子字段） | ✅ |
| 13.4 | `PLUGIN_CAPABILITIES = 19` ✓（另有 `LANDED_CAPABILITIES=6` / `UNSUPPORTED_CAPABILITIES=13`——**主动区分"已落地/未落地"，很诚实**） | ✅ |
| 13.5 | `HOOK_EVENTS = 17` = 15 实证 + `INFERRED_EVENTS = 2` ✓；`EVENT_EVIDENCE` / `EVENT_NOTES` 逐事件标注 | ✅ |
| 12.2 | 四个状态文件全在：`gateway-starts.log` / `processes.json` / `spawn-ledger.json` / `channel_directory.json`（+ `dispatcher.lock` / `deliveries.jsonl` / `peers.json` / `webhooks.json`） | ✅ |
| 12.4 | `standalone_sender_fn` 抽象方法 + 注释说明 cron 静默失败坑 | ✅ |
| 12.6 / 12.7 / 12.8 | `orbit send` / `webhook 4/4` / `pairing 4/4` | ✅ |
| 7.15 | `orbit curator` 注册 **17 子命令**，MATRIX **自我限定"17 条里实现 9 条"**（status/run/pin/unpin/archive/restore/ledger/usage/prune）——**主动标注完成度，不是整条打 ✅** | ✅ 诚实 |
| 7.3 | name 64 / desc 1024 / 索引 60（57+…）/ 正文 100,000 / 单文件 1 MiB | ✅ |
| 7.11 | `.usage.json` 13 字段 + `test_usage_json_has_all_13_fields`(:411) | ✅ |
| 14.6 | `secrets.py` 五源：keyring / env / file（`describe()` 明文警示）/ Bitwarden / 1Password（未装则 `available()=False` + 安装提示） | ✅ |
| 15.1 轮转 | 5MB × 5 ✓；errors.log 2MB × 3 只收 WARNING+ | ✅ |

---

## 与目标标志的差距

### Top 10 未完成项

| # | 未完成项 | 现状证据 | 量级 |
|---|---|---|---|
| 1 | **L8 MCP（10 条全 ⬜，唯一整层为零）** | stdio/HTTP + 生命周期加固早已有；缺 OAuth、prompts→技能、resources→RAG、`orbit mcp` 11 子命令、把自己暴露成 MCP 服务 | 10 条 |
| 2 | **L10 持久化 / 会话（12 ⬜）** | 有 backup/import + checkpoints；缺统一 session 平面、`id` 游标、压缩谱系、幂等投递账本、孤儿回收、`orbit sessions` 19 子命令 | 12 条 |
| 3 | **L4 模型适配（8 ⬜ + 4 🟡）** | **4 条 🟡 全是「库层有、运行时没接」**：fallback 链 / credential pool / provider 钩子 / transport | 12 条待补 |
| 4 | **L2 核心循环（9 ⬜）** | 子 Agent 仍零消费（`policies.subagents` 引用 0）；缺 13 类错误分类、心跳判卡死、中断配对、压缩重试、后台学习回路 | 9 条 |
| 5 | **L9 记忆层（9 ⬜ + 2 🟡）** | 已有 MEMORY 预算 + 四形态召回；缺 USER.md 用量自曝头、原子批处理、熔断、8 个 provider、写入门禁、漂移防护、CJK FTS | 11 条待补 |
| 6 | **L5 工具层（10 ⬜）** | 29 工具 vs Hermes 94；缺 code_execution、媒体生成、视觉、TTS、kanban、cronjob、桌面窗口、协作消息、HomeAssistant、元工具 | 差 65 个工具 |
| 7 | **L7 剩余（4 ⬜ + 5 🟡）** | 缺 hub 分发、bundles、sync 三方合并、`skills_guard` 107 条威胁模式、curator 剩余 8 子命令 | 9 条待补 |
| 8 | **L13 剩余（1 ⬜ + 4 🟡）** | 缺 dashboard 插件契约、懒发现、`all`/`pack` CLI 等 | 5 条待补 |
| 9 | **L12 剩余（1 ⬜ + 3 🟡）** | 19 个平台适配器全是占位；缺 `gateway` 11 子命令全量、peer、monitoring 导出 | 4 条待补 |
| 10 | **L1 剩余（6 ⬜ + 2 🟡）** | `serve` / `dashboard` / `desktop` / `gui` / `acp` / `console` / TUI / 全局 23 flag | 8 条 |

---

## 风险与漂移

### R1 🟠 **MATRIX 1.5 / 1.17 两行计数与 REGISTRY 脱节（本轮唯一实质错账）**

| 位置 | 写的是 | 实测 | 偏差 |
|---|---|---|---|
| `MATRIX 1.5` | `REGISTRY.parity_rows() == **411**` | **417** | 少 6 |
| `MATRIX 1.17` | 「**83 / 411**，另有 **3** 条 partial、**325** 条 todo」 | **137 / 417**，**9** partial、**271** todo | **done 少算 54 条** |
| `ROADMAP W2` | 「75 顶层 / **417** 展平」「实现 **137/417**（另有 9 partial、271 todo）」 | ✅ **逐字一致** | 无 |

**反过来了**：上轮是 ROADMAP 落后、MATRIX 正确；这轮 **ROADMAP 改对了，MATRIX 落后**。你消息里的「411 / partial 3」正是从 MATRIX 1.17 读来的。
**危害**：1.17 是本项目"命令面实现率"的唯一登记处，现在低报 54 条（83 → 137），比总数错 6 更严重。

### R2 🟡 **12.5 的平台指代错误（数字对、名字错）**

见 B⑤：`implemented=True` 的 3 个是 **`a2a` / `ntfy` / `raft`**，MATRIX 写成 `console` / `file` / `webhook`——后者是 Orbit 自有本地适配器类，不在 22 枚举内。🟡 不计分，但**证据文字错误会误导下一轮**（有人会去找 console 适配器的实现）。

### R3 🟡 **15.1「六选项全对齐」仍缺目标数说明（上轮 R4 未修）**

`orbit logs --help` 实测 `log_name` 仍是 **`agent` / `errors` / `gateway` / `list`** = 3 个目标（Hermes 有 `agent/errors/gateway/gui/desktop/list` = 5 个 + list）。选项面 6/6 确实对齐，但**目标数 3/5 这半边一直没写进证据栏**。第三轮提过，仍未补。

### R4 🟡 **未验证项（如实标注，不许当通过）**

| 项 | 为什么没验 |
|---|---|
| `matrix_stats.py` 端到端 exit 1 | 它只读固定 `MATRIX.md`、无路径参数；要实跑必须改 MATRIX（禁改区）。**只做了函数级 + 源码级验证** |
| `orbit chat` 交互式会话 | 会起 REPL 并可能调模型 → 未运行（只读 `chat.py` 与 9 条测试） |
| `gateway start` / `dispatcher` 实际互斥 | 会起服务、写 `dispatcher.lock` → 未运行（只读 `acquire()` 实现） |
| `cron run` / `gateway` 投递 | 会真执行任务、写 `runs.jsonl` → 未运行 |
| `backup create` / `restore` | 会写 zip、改数据目录 → 未运行 |
| `orbit hooks` 实际触发 | 会执行 shell 脚本 → 未运行（只读注册表与事件表） |
| `config edit` / `doctor --fix` / `doctor --live` | 拉编辑器 / 写盘 / 真发请求 → 未运行 |
| 19 个占位平台真的抛 `PlatformNotImplemented` | 只读 `UnimplementedAdapter` 源码（每个发送方法 `raise self._refuse()`），**未逐个实例化调用** |

### R5 🟢 **上轮 4 条风险，本轮全部已修（记录以免重复报警）**

| 上轮风险 | 本轮实测 | 状态 |
|---|---|---|
| R1 ROADMAP 6 处波次状态过期 | W2–W13 全部更新，且 W2 数字与 REGISTRY 逐字一致；W3 也补上了 fallback 降级 | **已修** |
| R2 MATRIX 1.4 计数 74→75 | 1.4 现写「**75** 个：Hermes 的 74 + Orbit 自有 `session-search`」✓ | **已修** |
| R3 6.4 / 11.1 缺形态说明 | 6.4 仍未见「恒为 cli 全集」的说明（**未修**）；11.1 仍未提「无常驻 ticker」（**未修**） | ⬜ 残留 2 条 |
| R5 `orbit session-search` 无矩阵行 | 新增 **1.18** 行登记 `orbit session-search {discover,scroll,read,browse}` ✓ | **已修** |

---

## 下一轮建议

按优先级（**这轮代码质量已经很好，瓶颈全在账本**）：

1. **【P0】修 MATRIX 1.5 / 1.17 两行**：411 → **417**；「83/411 + 3 partial + 325 todo」→「**137/417 + 9 partial + 271 todo**」。这两行是全项目"命令面实现率"的唯一登记处，现在低报 54 条。**建议直接把这两行的数字改成从 `matrix_stats.py` 生成，别再手写**。
2. **【P0】修 12.5 的平台名**：`console`/`file`/`webhook` → **`a2a`/`ntfy`/`raft`**，并说明 console/file/webhook 是 Orbit 自有本地适配器（不在 22 枚举内）。
3. **【P1】补 15.1 的「目标 3/5，缺 gui/desktop」**（第三轮提过两次了）；同时把 6.4「运行时恒为 cli 全集」、11.1「无常驻 ticker」两句补上——这三条都是**结论成立但会被误读**的类型。
4. **【P1】下一波做 L8 MCP（唯一整层为零）**：Orbit 已有 stdio/HTTP 双传输 + 五态状态机 + 事件式存活检测（08 调研评为"完整且加固"），补 `orbit mcp` 11 子命令 + OAuth 的性价比最高，**能一次把 10 条 ⬜ 变成 ✅/🟡**。
5. **【P1】把 L4 的 4 条 🟡 当一类问题解决**：fallback / credential pool / provider 钩子 / transport 全是"库层有、运行时没接"。建议在 `agent.get_model()` 里真接一次 `FallbackChain`——**一条改动能把 4.5/4.6 同时翻回 ✅**，是全表性价比最高的一处。
6. **【P2】把 19 个占位平台做成"契约测试"而非"逐平台实现"**：12.5 已经诚实标了 🟡，但可以再加一条参数化用例（遍历 `PLATFORMS`，断言 `implemented=False` 的适配器每个发送方法都抛 `PlatformNotImplemented`），把"未实现"从文字承诺变成可执行断言。
7. **【P2】每条 ✅ 加一列「验证命令」**：本轮 69 条 ✅ 里多数证据是「文件 + 函数名 + 用例名」，质量已不错；再加一列可一键复跑的断言（如 `orbit curator --help | grep -c '{status,usage,…}'`），下轮监视就不必重新读源码。
8. **【P2】复核 `08` 的「15 条已声明未消费」剩余 13 条**：本轮仍未核（`Tool.confirmation` / `granted()` / `budget["summary_mark"]` / `context_meta["compressed"]` 等）。

---

*本报告由 monitor 角色只读生成。69 条 ✅ 复核 21 条（30.4%），你点名的 10 条全部复核：**9 条完全撑得住，1 条（12.5）数字对名字错**。未验证项单列，不混进完成项。所有数字可按上文命令复现。*
