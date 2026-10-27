# Hermes Agent（CN 0.21.0-cn.18）工具层完整清单与机制说明

> **调查范围**：`E:\Hermes Agent CN Desktop\data\versions\0.21.0-cn.18\_internal\tools\`（147 个 `.py`，真实源码，只读）
> **辅助证据**：`hermes-agent-cn-runtime-win32-x64.exe tools --help` / `tools list --help` / `tools enable --help`（仅 `--help`）、`data\hermes-home\config.yaml`（只读）
> **不可见部分**：`toolsets`、`model_tools`、`registration_lifecycle`、`toolset_distributions` 等顶层模块**不在** `_internal\` 下（PyInstaller 打包进 exe 的 PYZ），因此凡涉及它们的结论均标注为「推断」，并给出推断依据。
> **安全声明**：本次调查未修改/删除 Hermes 目录或 hermes-home 中任何文件，未安装任何依赖，exe 仅以 `--help` 调用。

---

## 0. 结论摘要（口径先行）

| 指标 | 数值 | 口径 |
|---|---|---|
| 工具模块文件 | **147** 个 `.py` | `_internal\tools\*.py` 直接计数 |
| 内置工具（model-facing schema） | **94** | 见 §1 三口径合并 |
| 功能域（本报告的归类） | **19** | §3 |
| 注册的 toolset 名 | **32** + `mcp-<server>` 动态 | `registry.register(toolset=...)` 字面量去重 |
| 静态 `registry.register()` 调用点 | 99（其中 13 处为 `atexit.register` / TTS provider `@register` 误命中） | AST 扫描 |
| MCP 工具 | 无上限，`mcp__<server>__<tool>` | `mcp_tool.py:6887 MCP_TOOL_NAME_PREFIX = "mcp__"` |

**「94」的三口径说明**（务必区分，避免与官方口径混淆）：

1. **86** = 源码中 `registry.register(name="…")` 字面量去重后的工具名（AST 精确扫描，见 `_scan_tools.py` 复现方法）。
2. **90** = 86 + 4 个「schema/handler 定义在 `tools/*.py` 但注册发生在冻结层」的工具：`send_message`（`send_message_tool.py`）、`open_preview`（`open_preview_tool.py`）、`read_preview`（`read_preview_tool.py`）、`close_preview`（`close_preview_tool.py`）。
3. **94** = 90 + 3 个 tool-search 桥接工具（`tool_search` / `tool_describe` / `tool_call`，常量在 `tool_search.py:64-66`）+ 1 个条件注册的 `message_agent`（`bot_mode_dm.py:59 MESSAGE_AGENT_TOOL_NAME`，仅 bot 模式）。

> 不含 MCP（`mcp__*`，运行时按 server 动态生成）、不含插件工具（`plugins.entries` 加载，本机为 `superpowers/.hermes-plugin`）、不含仅存在于冻结层、`tools/` 无源码的工具（若有，无法从源码证实）。

---

## 1. 枚举方法（可复现）

不要逐字读 147 个文件。用 AST 而非正则，因为 schema 常量多为跨行 dict + 字符串拼接。

```python
# 核心思路（完整脚本见同目录 _scan_tools.py / _scan_desc.py）
import ast
for node in ast.walk(ast.parse(src)):
    if isinstance(node, ast.Call) and getattr(node.func, "attr", None) == "register":
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        name    = resolve(kw["name"], consts)      # 字面量 or 模块级 str 常量
        toolset = resolve(kw["toolset"], consts)
# 再单独扫所有含 "name"+"description" 的 dict 字面量，取参数 properties
```

**为什么不用 `TOOL_NAME` 常量**：本版本几乎不用该模式。全仓库仅 4 处：

- `mcp_tool.py:6887 MCP_TOOL_NAME_PREFIX = "mcp__"`
- `tool_search.py:64-66 TOOL_SEARCH_NAME/TOOL_DESCRIBE_NAME/TOOL_CALL_NAME` + `:68 BRIDGE_TOOL_NAMES`
- `bot_mode_dm.py:59 MESSAGE_AGENT_TOOL_NAME = "message_agent"`
- `tool_result_storage.py:67 _BUDGET_TOOL_NAME = "__budget_enforcement__"`（内部伪工具名，不对模型暴露）

真正的注册契约是 `registry.register(name=, toolset=, schema=, handler=, …)`（`registry.py:1131`）。

**误命中排除**：`atexit.register`（`code_kernel.py:401`、`code_kernel_remote.py:222`、`credential_files.py:342`）与 `@register("elevenlabs")` 之类 TTS provider 注册（`tts_streaming.py:220/264/313/400`）不是工具注册。

---

## 2. 注册机制（`_internal/tools/registry.py`，1763 行）

### 2.1 单一注册契约

`registry.py:1131 ToolRegistry.register(name, toolset, schema, handler, check_fn=None, requires_env=None, is_async=False, description="", emoji="", max_result_size_chars=None, dynamic_schema_overrides=None, override=False, scope=None)`

`ToolEntry`（`registry.py:429`）用 `__slots__` 存 12 个字段（`name/toolset/schema/handler/check_fn/requires_env/is_async/description/emoji/max_result_size_chars/dynamic_schema_overrides/_schema_json`），并**刻意不反向引用 registry**，使 deregister / re-register 靠引用计数即可回收，不形成环（注释里的 GC 不变式，被 `test_no_circular_refs` 钉住）。

模块级单例：`registry.py:1717 registry = ToolRegistry()`。

### 2.2 懒发现：AST 索引 + 按需 import

这是本版本最关键的架构特征——**147 个工具模块不会在启动时全部 import**。

| 函数 / 常量 | 位置 | 作用 |
|---|---|---|
| `enable_lazy_builtins()` | `registry.py:749` | 仅记录 tools 目录 + 置 flag，由 `model_tools` 在 import 时调用 |
| `_SKIP_TOOL_FILES = {"__init__.py","registry.py","mcp_tool.py"}` | `registry.py:194` | 索引时跳过 |
| `_is_registry_register_call()` / `_module_registers_tools()` | `registry.py:77` / `:90` | 只认**模块体**里的 `registry.register(...)`，函数体内的调用不算（避免 helper 模块误判） |
| `build_tool_index()` | `registry.py:243` | 产出 `tool_to_module` / `toolset_to_modules` / `modules` / `opaque_modules` |
| `load_or_build_tool_index()` | `registry.py:354` | 带磁盘缓存（`_TOOL_INDEX_VERSION = 1`，`registry.py:193`；指纹 `_tool_index_fingerprint()` `:305`） |
| `_ensure_tool_loaded(name)` | `registry.py:805` | 查一个工具 → 只 import 提供它的那一个模块 |
| `_ensure_toolset_loaded(ts)` | `registry.py:816` | 查一个 toolset → import 该 toolset 的全部贡献模块 |
| `_ensure_all_loaded()` | `registry.py:826` | 聚合查询（列表/诊断）才全量 import，最多一次 |

> 设计意图：`get_tool_definitions(enabled_toolsets=[...])` 不应把 browser / image_gen / tts 等重模块拖进来。`registry.py:1423-1428` 的注释明确说明这一点。

### 2.3 命名与遮蔽（shadow / override）规则

`register()` 内的判定顺序（`registry.py:1155-1228`）：

1. **确定 owner**：`handler_owner = _plugin_owner_of(handler)`（`:1023`，取 `handler.__globals__["__name__"]`，即**定义处**模块，不随调用点漂移）；`caller_owner = _plugin_namespace_of_module(_caller_module())`；`owner = caller_owner or handler_owner`。`scope` 为空时由 owner 推导 profile scope（`:1158`）。
2. **跨 profile 遮蔽全局**：若 `owner` 有、`scope` 有、本地 slot 为空、且 `name in self._tools`：
   - `override=False` → **直接 return（拒绝），只打 `logger.error`，不抛异常**（`:1177-1185`）
   - `override=True` 但未获操作员 opt-in → **抛 `PermissionError`**（`:1186-1191`）
3. **跨 toolset 冲突**（`existing.toolset != toolset`）：
   - `override=True` + 有 opt-in → 允许替换，`logger.info` 记审计（`:1212`）
   - 否则 → **拒绝**，包括 MCP 对 MCP 的碰撞。注释明确：合法的 MCP 重连/刷新是在**同一个 canonical toolset 内**重注册，因此不受影响（`:1218-1228`）。
4. 写入 `target[name] = ToolEntry(...)`，`self._generation += 1`。

**操作员 opt-in 通道**：`_PluginOverridePolicy`（`registry.py:481`）+ `register_plugin_override_policy()`（`:966`）+ `_plugin_override_allowed()`（`:1013`）。配置入口是 `config.yaml` 的 `plugins.entries.<plugin_id>.allow_tool_override: true`（错误文案里直写，`:1200-1202`）。本机实际值：`plugins.entries.superpowers/.hermes-plugin.allow_tool_override: false`。

**注销同样受门禁**：`deregister()`（`registry.py:1252`）防止「先删掉不归自己管的工具，再普通 register 绕过 override 检查」这条路（`:1259-1266` 注释）。`mcp-*` toolset **豁免**（`entry.toolset.startswith("mcp-")`，`:1292`），因为动态发现本来就要 nuke-and-repave。

**CAS 式回滚**：`restore_registration()`（`registry.py:1341`）用身份比较 `target.get(name) is not current` 才回滚，避免多 profile 进程里误删别的插件的新注册。

**toolset 别名**：`register_toolset_alias(alias, toolset)`（`registry.py:940`），MCP 用它把 server 名映射到 `mcp-<server>`（`mcp_tool.py:7575 registry.register_toolset_alias(name, toolset_name)`）。别名碰撞只 warning 不阻塞。

**桥接工具名保留**：`tool_search` / `tool_describe` / `tool_call` 由「registry 现有的 override 保护逻辑」拒绝任何用户/插件/MCP 同名注册（`tool_search.py:61-63` 注释 + `BRIDGE_TOOL_NAMES`）。

### 2.4 schema 校验 / 规范化

本版本**没有**在 `register()` 里做 JSON-Schema 合法性校验——schema 原样存入 `ToolEntry.schema`。校验/修复发生在两个下游位置：

| 位置 | 函数 | 职责 |
|---|---|---|
| `schema_sanitizer.py` | `_sanitize_single_tool()`（`:138`）、`sanitize_property_key()`（`:55`） | 修 llama.cpp / Anthropic / Fireworks 会 400 的构造：`{"type":"object"}` 无 properties、裸字符串 `"object"`、`type: ["string","null"]` 数组类型、只为允许 null 的 `anyOf/oneOf`、`$ref` 同级带 `default` |
| `schema_sanitizer.py:50` | `_PROP_KEY_RE = ^[a-zA-Z0-9_.-]{1,64}$` | 属性名强制收敛；Cloudflare 扁平 API 的 61 个非法键（`issue_class~neq`、`meta.<field>[<op>]`）会让**整个 tools 数组 400** |
| `registry.get_definitions()` | `registry.py:1412` | 出口处强制补 `name`：`schema_with_name = {**entry.schema, "name": entry.name}`（`:1461`）；再叠加 `dynamic_schema_overrides()`（`:1467`），异常则回退静态 schema 并 warning |

`dynamic_schema_overrides` 的典型用户是 `delegate_task`：它的 description 必须反映**当前** `delegation.max_concurrent_children` / `max_spawn_depth`，所以每次 `get_definitions()` 重建（`delegate_tool.py:5074-5204`）。

### 2.5 按 source 重连 / 刷新（generation 计数）

- `self._generation`（`registry.py:726`）：**每次 register / deregister / register_toolset_alias / MCP refresh 都 +1**（`:950`、`:1250`、`:1338`、`:1404`）。
- 外部调用方（如 `get_tool_definitions`）用 generation 做 memo key：generation 不变则缓存有效，变了就自然失效。
- 线程安全：`self._lock = threading.RLock()`（`:720`），MCP 动态刷新会在别的线程改 registry，所以「写加锁、读快照」（`_snapshot_state()` `:857`）。
- **MCP 刷新路径**（`mcp_tool.py`）：
  - `notifications/tools/list_changed` → `deregister()` + 重新 `register()`（`deregister` docstring 明写用途，`registry.py:1256`）
  - 注册结果由 registry 做**原子归属判定**：`if registry.get_toolset_for_tool(registry_name) != toolset_name: continue`（`mcp_tool.py:7385` / `:7542` / `:7575`）。注释说 pre-check 只是 advisory，真正的 owner gate 是 `ToolRegistry.register()`。
  - 连接失败分级见 §5.1；重连退避 `_CONNECT_RETRY_BASE_BACKOFF_SEC = 30.0`、`_CONNECT_RETRY_MAX_BACKOFF_SEC = 600.0`（`mcp_tool.py:4542-4543`），parked server 自探间隔 `_PARKED_RETRY_INTERVAL = 300`（`mcp_tool.py:590`）。

### 2.6 心跳 / 可用性 `check_fn`（TTL + 容错宽限）

「心跳」在 Hermes 里不是定时器，而是**按需探针 + TTL 缓存 + 瞬态失败宽限**：

| 常量 | 值 | 位置 |
|---|---|---|
| `_CHECK_FN_TTL_SECONDS` | **30.0 s** | `registry.py:514` |
| `_CHECK_FN_FAILURE_GRACE_SECONDS` | **60.0 s** | `registry.py:518` |
| `_CHECK_FN_CACHE_MAX` | **512** | `registry.py:519` |

`_check_fn_cached(fn)`（`registry.py:609`）语义：

1. `fn in _NO_CACHE_CHECK_FNS`（`no_cache_check_fn()` 装饰器，`:527`）→ 直跑，不缓存（本地 config 驱动的检查用这个）。
2. 缓存 key 是 `(fn, scope)`，`scope = hermes_home_key()` → **多 profile 不串味**；profile 身份解析失败时返回 `CHECK_FN_CACHE_BYPASS` 并**绕过两层缓存 fail-closed**（`:580-591`、`:615`）。
3. 命中且 < 30 s → 返回缓存值。
4. 未命中 → 跑 `bool(fn())`；异常视为 `False` 但打 warning（`:600`）。
5. **成功** → 写 `_check_fn_last_good[key] = now` + 缓存 True。
6. **失败**：若距上次成功 < 60 s → **判定为抖动，返回 True 且不缓存失败**（下次重新探）；否则缓存 False 并 warning。

> 为什么需要：`subprocess.run(["docker","version"], timeout=5)` 在负载下超时一次，就会把整个 `terminal`+`file` toolset 从正在构建的 agent 上摘掉——最典型受害者是 `delegate_task` 子 agent，它会报「Tool read_file does not exist」（`registry.py:501-511` 注释，引用 issue #21658 / #5304）。

`invalidate_check_fn_cache()`（`:665`）在 `hermes tools enable/disable` 后清空两层缓存。`get_cached_check_fn_result()`（`:673`）给 dashboard 这类只读面板用，**绝不触发探针**。

toolset 级可用性：`_toolset_has_exposable_tools(toolset, entries)`（`registry.py:874`）——只要该 toolset 里**任一**工具可暴露即算可用，避免混合 toolset（如 `terminal` 里混一个 desktop-only 的 `read_terminal`）被第一个 `check_fn` 误杀。`_toolset_checks`（`:715`）现在只用于 banner 分类「lazy-init vs disabled」，不再 gate toolset（`:1242-1249` 注释）。

### 2.7 dispatch 与结果契约

`dispatch(name, args, **kwargs)`（`registry.py:1516`）：

- `is_async=True` → 经 `model_tools._run_async()` 桥接
- 结果统一过 `_normalize_handler_result()`（`:1485`）：**只允许 `str` 或 `{"_multimodal": True, "content": [...]}` 多模态信封**；其它类型转成 `tool_error(..., error_type="tool_result_contract")`
- 任何异常 → `logger.exception` + `model_tools._sanitize_tool_error()` 清洗（防止异常串里的围栏/CDATA 变成结构噪声）→ `tool_error(...)`

错误体上限：`_MAX_TOOL_ERROR_CHARS = 2048`（`registry.py:36`），截断标记 `"… [truncated]"`（`:37`）；日志保留 `_MAX_LOGGED_ERROR_CHARS = 8192`（`:39`）。`_bound_json_error_result()`（`:54`）在 dispatch 边界再兜一次——防止 handler 自己 `json.dumps({"error": str(exc)})` 绕过上限。

返回辅助：`tool_error(message, **extra)`（`:1736`）、`tool_result(data=None, **kwargs)`（`:1751`），都是 `orjson.dumps(...).decode()`。

---

## 3. 工具集（toolset）分层与 per-platform 模型

### 3.1 从 CLI 证实：per-platform 是真实存在的维度

```
$ hermes-agent-cn-runtime-win32-x64.exe tools --help
usage: hermes tools [-h] [--summary] {list,disable,enable,post-setup} ...
Enable, disable, or list tools for CLI, Telegram, Discord, etc.
Built-in toolsets use plain names (e.g. web, memory).
MCP tools use server:tool notation (e.g. github:create_issue).

$ hermes tools list --help
usage: hermes tools list [-h] [--platform PLATFORM]
  --platform PLATFORM  Platform to show (default: cli)

$ hermes tools enable --help
usage: hermes tools enable [-h] [--platform PLATFORM] NAME [NAME ...]
  NAME  Toolset name or MCP tool in server:tool form
```

结论（可直接引用）：

- 启用/禁用模型是 **`(platform, name)` 二元组**，`platform` 默认 `cli`，其它平台按文案至少包含 `telegram`、`discord`。
- 被启用的单位是 **toolset 名**（`web`、`memory`）或 **`server:tool` 形式的 MCP 工具**——即「built-in 按 toolset 粒度、MCP 按单工具粒度」的不对称模型。
- 另有 `--summary`（打印各平台启用摘要）与 `post-setup`（跑 provider 的 npm/pip/binary 安装钩子）。

### 3.2 三层过滤（还原）

```
层 1  platform 启用集   ← config.yaml 里 per-platform 的 enabled toolsets（CLI/dashboard 管理）
        ↓  enabled_toolsets=[...]
层 2  registry 可用性   ← 每个 ToolEntry.check_fn()（30s TTL + 60s 宽限，§2.6）
        ↓  get_definitions() 逐工具 check_fn 过滤 + dynamic_schema_overrides
层 3  tool_search 披露  ← 只有「可延迟」的才被 bridge 收走；core / GUI surface 永不延迟
```

层 3 的证据非常明确（`tool_search.py` 模块 docstring `:1-41` + 常量）：

| 常量 / 函数 | 值 / 位置 | 语义 |
|---|---|---|
| `_core_tool_names()` | `tool_search.py:219`，`from toolsets import _HERMES_CORE_TOOLS` | **核心工具永不延迟**，硬约束（"Always-load means always-load. No exceptions."） |
| `_DIRECT_SURFACE_TOOLSETS` | `tool_search.py:234` = `{"desktop_ui", "project"}` | 会话内被启用后**永不延迟**；但**不在** core 列表里，所以 CLI/消息平台不付它们的 schema 成本 |
| `is_deferrable_tool_name()` | `tool_search.py:237` | 可延迟 ⟺ `toolset.startswith("mcp-")`，或「既非 core 也非 GUI surface」的插件工具 |
| `CHARS_PER_TOKEN` | `tool_search.py:76` = **4.0** | token 估算（英文+JSON 经验值，刻意略低估） |
| `listing_max_tokens` | `tool_search.py:112` = **4000** | 内嵌目录清单的绝对上限；有效预算 = `min(4000, threshold_pct% × context)` |
| `_MAX_QUERIES_PER_CALL` / `_MAX_DESCRIBE_NAMES_PER_CALL` | `tool_search.py:79` / `:81` = **10** / **10** | 单次桥接调用的工作量上限 |
| 分级披露 Tier 0/1/2 | `tool_search.py:17-29` | Tier0 无 MCP/插件→纯直通；Tier1 清单装得下→桥接 + name+短描述清单（超预算降级为仅名字）；Tier2 连名字都装不下（例：Cloudflare ~3300 工具，名字本身 ~32K token）→ 裸桥接 + 每 server 一行摘要 |
| BM25 检索 | `tool_search.py:506 _bm25_score(k1=1.5, b=0.75)` | 自实现，不引依赖；`snowballstemmer` 做词干 |
| `source` 概念 | `tool_search.py:462 _classify_source()` | 返回 `("mcp", "mcp-<server>")` / `("plugin", "<toolset>")` / `("other", "")`；`CatalogEntry.source` + `source_name` 存进目录（`:494-501`） |

**推断（标注）**：`_HERMES_CORE_TOOLS` 的具体成员无法从源码读出（`toolsets` 模块冻结在 exe 内）。但由 `tool_search.py:10-16` 与 `:232-234` 可确定其**性质**：一个显式白名单，包含「CLI/消息平台也该有的基础工具」，**不含** `desktop_ui`、`project`，也**不含** `delegation` 的编排类工具（`delegate_tool.py:1186 DEFAULT_TOOLSETS = ["terminal","file","web"]` 是子 agent 的默认继承集，可作旁证）。

### 3.3 32 个已注册 toolset（`register(toolset=…)` 字面量去重）

| toolset | 工具数 | toolset | 工具数 |
|---|---|---|---|
| `kanban` | 14 | `feishu_drive` | 4 |
| `desktop_ui` | 12(+3) | `homeassistant` | 4 |
| `browser` | 10 | `file` | 4 |
| `hermes-yuanbao` | 5 | `skills` | 3 |
| `video_gen` | 3 | `browser-cdp` | 2 |
| `terminal` | 2 | `web` | 2 |
| `context_engine` | 2 | `vision`/`video` | 1/1 |
| `browser-use` | 1 | `clarify` | 1 |
| `code_execution` | 1 | `computer_use` | 1 |
| `cronjob` | 1 | `delegation` | 1 |
| `discord` | 1 | `discord_admin` | 1 |
| `feishu_doc` | 1 | `image_gen` | 1 |
| `memory` | 1 | `project` | 1 |
| `session_search` | 1 | `swarm` | 1 |
| `todo` | 1 | `tts` | 1 |
| `x_search` | 1 | `mcp-<server>` | 动态 |

另有别名机制（`registry.py:940`）把 server 名映射到 canonical `mcp-<server>`。

---

## 4. 全量工具清单（19 个功能域）

> 格式：`工具名` — 一行用途；**关键参数**。参数名取自 schema `parameters.properties`。

### 4.1 文件读写与编辑（`file`，4）
证据：`file_tools.py`（`read_file_tool` `:1635`、`write_file_tool` `:2256`、`patch_tool` `:2347`、`search_tool` `:2567`）

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `read_file` | 带行号分页读文本，替代 `cat/head/tail` | `path`, `offset`, `limit`（默认 2000 行） |
| `write_file` | 全量覆盖写文件，替代 `echo`/heredoc | `path`, `content` |
| `patch` | 定向 find-and-replace 编辑，**fuzzy 匹配 9 策略**，替代 `sed/awk` | `path`, `old_string`, `new_string`, `replace_all` |
| `search_files` | 内容搜索 + 按名找文件，ripgrep 后端，替代 `grep/rg/find/ls` | `pattern`, `target`, `path`, `file_glob`, `output_mode`, `context` |

### 4.2 终端与进程（`terminal`，2）
证据：`terminal_tool.py`（`terminal_tool` `:3077`）、`process_registry.py`

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `terminal` | 在 8 种后端执行命令（见 §4.2.1） | `command`, `timeout`, `background`, `interactive`, `process_id`, `workdir`, `wait_for_pattern` |
| `process` | 管理后台进程：list / poll / kill / 等 | `action`, `session_id`, `pattern`, `inactivity_timeout`, `block`, `output_path` |

**4.2.1 执行后端**（`terminal_tool.py:1790 _CONTAINER_BACKENDS = {"docker","singularity","modal","daytona","vercel_sandbox"}`，`:1800` 另加 `local`/`ssh`/`managed_modal`；`tools/environments/` 下有 `local.py`、`docker.py`、`ssh.py`、`modal.py`、`managed_modal.py`、`daytona.py`、`singularity.py`、`vercel_sandbox.py`、`powershell_session.py`、`windows_env.py`、`process_pwsh.py`）。`TERMINAL_ENV` 环境变量选择，默认 `local`。

### 4.3 代码执行（`code_execution`，1）
证据：`code_execution_tool.py`（模块 docstring 描述 UDS RPC 架构）

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `execute_code` | **Programmatic Tool Calling**：模型写 Python 脚本，脚本内通过 RPC 调 Hermes 工具，多步链压缩成一轮推理；只有 stdout 回模型，中间结果不进上下文 | `code`, `reset` |

> 传输层两套：本地 UDS（Unix domain socket，Windows 禁用）+ 远端文件式 RPC（Docker/SSH/Modal/Daytona）。`DEFAULT_EXECUTION_MODE = "project"`、`DEFAULT_KERNEL_MODE = "session"`（`code_execution_tool.py`）。**注意：本机是 Windows，`execute_code` 的本地路径被禁用。**

### 4.4 浏览器（`browser` 10 + `browser-cdp` 2 + `browser-use` 1 = 13）
证据：`browser_tool.py:2793 BROWSER_TOOL_SCHEMAS`、`browser_cdp_tool.py`、`browser_dialog_tool.py`、`browser_use_cli.py`

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `browser_navigate` | 打开 URL 并建立会话（其它 browser 工具的前置） | `url` |
| `browser_snapshot` | 无障碍树文本快照，产出可点的 ref ID（`@e1`） | `full`（默认 false = 紧凑） |
| `browser_click` | 按 ref ID 点击 | `ref` |
| `browser_type` | 按 ref ID 输入（先清空） | `ref`, `text` |
| `browser_press` | 按键 / 快捷键 | `key` |
| `browser_scroll` | 上下滚动 | `direction` |
| `browser_back` | 后退 | — |
| `browser_get_images` | 列出页面图片 URL + alt（喂给 vision） | — |
| `browser_vision` | 截图做视觉检查（CAPTCHA / 视觉验证 / 复杂布局） | `question`, `annotate` |
| `browser_console` | 读 console 输出与 JS 未捕获错误 | `expression`, `clear` |
| `browser_cdp` | 原始 Chrome DevTools Protocol 逃生口 | `method`, `params`, `target_id`, `frame_id`, `timeout` |
| `browser_dialog` | 响应原生 JS 对话框（alert/confirm/prompt/beforeunload） | `action`, `prompt_text`, `dialog_id` |
| `browser_exec` | browser-use CLI 后端执行代码 | `code`, `session`, `timeout_s` |

### 4.5 Web 搜索与抓取（`web`，2）
证据：`web_tools.py:820 web_search_tool`

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `web_search` | 网页搜索，最多 5 条结果；支持 `site:`/`filetype:pdf`/`intitle:`/`-term`/精确短语 | `query`, `limit` |
| `web_extract` | 把 URL 抽成 markdown/纯文本（不走 LLM），支持 PDF；受字符预算约束 | `urls`（≤5）, `char_limit` |

### 4.6 专项搜索（`x_search`、`session_search`，2）

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `x_search` | 用 xAI 内置 X Search 搜 X/Twitter 帖子、主页、线程（只读发现） | `query`, `allowed_x_handles`, `excluded_x_handles`, `from_date`, `to_date`, `enable_image_understanding` |
| `session_search` | 搜历史 Hermes 会话（本地 session DB 的 FTS5），或在某个会话内翻页 | `query`, `limit`, `sort`, `detail`, `session_id`, `window`, `role_filter` |

### 4.7 记忆（`memory`，1）
证据：`memory_tool.py:1101 memory_tool`

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `memory` | 保存跨会话的高信号事实（MEMORY.md / USER.md，每轮注入）；要求一次调用批量提交所有变更 | `action`, `target`, `content`, `old_text`, `new_text`, `operations` |

### 4.8 技能（`skills`，3）
证据：`skills_tool.py`、`skill_manager_tool.py`

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `skills_list` | 列出可用技能（name + description） | `category` |
| `skill_view` | 加载技能全文或链接文件（references/templates/scripts） | `name`, `file_path` |
| `skill_manage` | 创建/更新/删除技能（= 程序性记忆），一次调用多个 operation | `operations` |

### 4.9 MCP（`mcp-<server>`，动态无上限）
证据：`mcp_tool.py`（8725 行）、`mcp_oauth.py`、`mcp_schema_cache.py`、`mcp_stdio_watchdog.py`、`setup_mcp_tool.py`

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `mcp__<server>__<tool>` | MCP server 暴露的任意工具，注册时进 `mcp-<server>` toolset | 由 server 定义 |
| `setup_mcp` | 以**内联同意卡片**提议一个 MCP server（装目录项 / 重启用被禁 server / 跑自定义命令） | `server`, `action`, `reason` |

> MCP 还自动生成 4 类工具处理器：`list_resources` / `read_resource` / `list_prompts` / `get_prompt`（`mcp_tool.py:7542-7548 handler_factories`）。

### 4.10 委派与子 agent（`delegation`、`swarm`，2）
证据：`delegate_tool.py`、`agent_swarm.py`、`async_delegation.py`、`swarm_scheduler.py`、`subagent_runner.py`、`subagent_worktree.py`

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `delegate_task` | 在隔离上下文里派生 1..N 个子 agent；支持批量并行、`background=true` 异步、`orchestrator` 角色再派工 | `tasks`, `action`, `subagent_id`, `message` |
| `agent_swarm` | 用一个 prompt 模板 + `{{item}}` 占位符，从 items 批量起子 agent | `prompt_template`, `items`, `subagent_type`, `description`, `resume_agent_ids` |

### 4.11 媒体生成（`image_gen` 1 + `video_gen` 3 = 4）

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `image_generate` | 文生图；模型的编辑/参考能力在 serving 时动态渲染 | `prompt`, `aspect_ratio`（默认 landscape） |
| `video_generate` | 文生视频（schema 在 `get_definitions()` 时由 `_build_dynamic_video_schema` 重建） | `prompt`, `duration`, `aspect_ratio`, `resolution`, `model` |
| `xai_video_edit` | 用 xAI Imagine 编辑已有视频 | `prompt`, `video_url`, `model` |
| `xai_video_extend` | 用 xAI Imagine 延长已有视频 | `prompt`, `video_url`, `duration`, `model` |

### 4.12 视觉与视频理解（`vision` 1 + `video` 1 = 2）
证据：`vision_tools.py`

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `vision_analyze` | 把图片加载进对话（任意时刻用户提到图片都可调） | `image_url`, `question`, `region` |
| `video_analyze` | 用多模态模型分析 URL 或本地视频 | `video_url`, `question` |

### 4.13 语音与 TTS（`tts` 1；STT 不是工具）
证据：`tts_tool.py:3584 text_to_speech_tool`、`transcription_tools.py`、`voice_mode.py`、`wake_word.py`、`tts_streaming.py`

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `text_to_speech` | 文本转语音，返回 `MEDIA:` 路径由平台原生投递 | `text`, `output_path`, `speed`, `instructions`, `provider` |

> **STT 不暴露为模型工具**：`transcription_tools.py` 只提供库函数 `transcribe_audio()`（`:23` import 示例），被 voice_mode / CLI 调用。provider 默认 `local`（faster-whisper，默认模型 `base`），另有 `openai`（Whisper API，需 `VOICE_TOOLS_OPENAI_KEY`）、`local_command`（本机 config 用这个，模型 `small`）。流式 TTS provider 有 4 个：`elevenlabs`/`openai`/`gemini`/`xai`（`tts_streaming.py:220/264/313/400`，全部 24 kHz mono int16 PCM）。

### 4.14 看板（`kanban`，14）
证据：`kanban_tools.py`

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `kanban_show` | 读任务完整状态（title/body/assignee/父任务 handoff/自己此前的尝试） | `task_id`, `board` |
| `kanban_list` | 列任务摘要，供 orchestrator profile 发现可路由的活 | `assignee`, `status`, `tenant`, `include_archived`, `limit`, `board` |
| `kanban_create` | 建任务，可指定父任务 | `title`, `assignee`, `body`, `parents`, `priority`, `workspace_kind`, `workspace_path` |
| `kanban_comment` | 追加评论（跨 run 的持久备注） | `task_id`, `body`, `board` |
| `kanban_complete` | 标记完成 + 结构化 handoff 给下游 worker/人 | `task_id`, `summary`, `metadata`, `result`, `artifacts`, `board` |
| `kanban_block` | 停下并**按原因**路由：`kind='dependency'` 等 | `task_id`, `reason`, `kind`, `board` |
| `kanban_unblock` | 解除阻塞（父任务全 done 则 ready，否则 todo） | `task_id`, `board` |
| `kanban_request_review` | 交审：实现+自审+验证完成，等人工/另一 agent 审 | `task_id`, `summary`, `reviewer`, `metadata`, `board` |
| `kanban_request_changes` | 审阅结论：退回原实现者并给出具体必改项 | `task_id`, `reason`, `board` |
| `kanban_heartbeat` | 长操作期间「我还活着」信号 | `task_id`, `note`, `board` |
| `kanban_link` | 两个已存在任务之间加父子依赖边 | `parent_id`, `child_id`, `board` |
| `kanban_attach` | 内联 base64 挂文件 | `task_id`, `filename`, `content_base64`, `content_type` |
| `kanban_attach_url` | 按 URL 挂文件（server 侧下载，有大小上限） | `task_id`, `url`, `filename`, `content_type` |
| `kanban_attachments` | 列任务附件（id/filename/size/上传者/磁盘绝对路径） | `task_id`, `board` |

### 4.15 定时（`cronjob`，1）
证据：`cronjob_tools.py`

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `cronjob` | 管理定时任务：`create` 从 prompt 和/或 skills 建任务；`list` 查看 | `action`, `job_id`, `prompt`, `schedule`, `name`, `repeat`, `deliver`, `skills`, `enabled_toolsets`, `workdir` |

### 4.16 桌面与窗口（`desktop_ui` 15 + `project` 1 + `computer_use` 1 = 17）
证据：`*_preview_tool.py`、`tip_tool.py`、`tour_tool.py`、`apply_layout_tool.py`、`focus_pane_tool.py`、`read_window_tool.py`、`read_terminal_tool.py`、`close_terminal_tool.py`、`react_to_message_tool.py`、`project_tools.py`、`computer_use_tool.py`

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `desktop_preview` | 预览面板（open/read/close 复合动作） | `action`, `url`, `label`, `start`, `count` |
| `open_preview` / `read_preview` / `close_preview` | 打开 / 读 / 关闭聊天旁的预览面板（或其中一个标签页） | `url`,`label` / `start`,`count` / `url` |
| `drive_preview` | **驱动**预览面板里的网页：登录、填表、点击 | `action`, `ref`, `selector`, `text`, `submit`, `key`, `amount` |
| `annotate_preview` | 在预览页留**持久**标记（`drive_preview` 的标记会淡出） | `action`, `ref`, `selector`, `label` |
| `read_terminal` | 读聊天旁终端面板（无参 = 可见屏 + 总行数） | `start_line`, `count` |
| `close_terminal` | 关闭某个后台进程的只读终端标签页 | `process_id` |
| `read_window_below` | 识别 Hermes 窗口**正后方**的应用窗口（用户在干什么） | — |
| `focus_pane` | 聚焦某个桌面面板：chat/files/terminal/review(git diff) | `pane` |
| `apply_layout` | 应用保存的布局预设 | `preset` |
| `tip` | 小箭头气泡指一个东西（无变暗、无 tour 外壳） | `text`, `selector`, `title`, `side` |
| `tour` | 引导式导览：变暗屏幕 + 高亮元素 + 带标题的 popover | `action`, `surface`, `selector`, `steps`, `step_index` |
| `react_to_message` | 给消息加一个 emoji 反应（iMessage tapback 式） | `emoji`, `message_row_id`, `messages_back` |
| `desktop_project` | 桌面 Project（命名工作区）：create/switch，可绑 path | `action`, `name`, `path` |
| `computer_use` | 通过 cua-driver 做通用桌面控制（跨 macOS/Win/Linux），**后台执行不抢用户光标** | 由 `COMPUTER_USE_SCHEMA` 定义（`tools/computer_use/schema.py`） |

### 4.17 协作与消息（`discord`/`discord_admin`/`feishu_doc`/`feishu_drive`/`hermes-yuanbao`/`send_message`，13）

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `send_message` | 向已接入的消息平台发消息，或列可用 target | `action`, `target`, `message`, `emoji`, `message_id` |
| `discord` | Discord 核心动作集（`_CORE_ACTIONS`） | 由 `_build_schema` 动态生成；`channel_id`,`message_id`,`query`,`limit` |
| `discord_admin` | Discord 管理动作集（`_ADMIN_ACTIONS`），需 `DISCORD_BOT_TOKEN` | 同上 + `role_id`,`auto_archive_duration` |
| `feishu_doc_read` | 读飞书/Lark 文档全文为纯文本 | `doc_token` |
| `feishu_drive_list_comments` | 列文档评论（`is_whole=true` 只看整篇评论） | `file_token`, `file_type`, `is_whole`, `page_size`, `page_token` |
| `feishu_drive_list_comment_replies` | 列某条评论线程的所有回复 | `file_token`, `comment_id`, `file_type`, `page_size` |
| `feishu_drive_reply_comment` | 回复**局部**（引用文本）评论线程 | `file_token`, `comment_id`, `content`, `file_type` |
| `feishu_drive_add_comment` | 加**整篇**文档评论（或作为局部评论的兜底） | `file_token`, `content`, `file_type` |
| `yb_query_group_info` | 查元宝群（「派」）基本信息：群名/群主/成员数 | `group_code` |
| `yb_query_group_members` | 查群成员（用于 @某人） | `group_code`, `action`, `name`, `mention` |
| `yb_send_dm` | 群里给某人发私信，可带媒体 | `group_code`, `name`, `message`, `user_id`, `media_files` |
| `yb_search_sticker` | 按关键词搜内置贴纸（TIM 表情）目录 | `query`, `limit` |
| `yb_send_sticker` | 发内置贴纸到当前会话 | `sticker`, `chat_id`, `reply_to` |

### 4.18 智能家居（`homeassistant`，4）

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `ha_list_entities` | 列实体，可按 domain/area 过滤 | `domain`, `area` |
| `ha_get_state` | 单个实体的详细状态 + 全部属性（亮度/色温…） | `entity_id` |
| `ha_list_services` | 列可用服务（动作），看每个实体能做什么 | `domain` |
| `ha_call_service` | 调服务控制设备 | `domain`, `service`, `entity_id`, `data` |

### 4.19 元工具（`todo`/`clarify`/`context_engine`/bridge/`message_agent`，8）

| 工具 | 用途 | 关键参数 |
|---|---|---|
| `todo` | 会话任务清单；无参 = 读；复杂任务（3+ 步）必用 | `todos`, `merge`, `auto_fix` |
| `clarify` | 需要决策/澄清/反馈时向用户提一个或多个问题 | `questions` |
| `compact` | 触发上下文压缩（`context_engine` toolset） | 由 `CONTEXT_*_SCHEMA` 定义 |
| `context_usage` | 查当前上下文占用（`context_engine` toolset） | — |
| `tool_search` | 桥接工具：按自然语言/BM25 检索被延迟的工具 | `query`（≤10 queries/次） |
| `tool_describe` | 桥接工具：取被延迟工具的完整 schema | `names`（≤10/次） |
| `tool_call` | 桥接工具：调用被延迟的工具（走 `model_tools.handle_function_call`，护栏/审批/截断全生效） | `name`, `arguments` |
| `message_agent` | bot 模式下给另一个 agent 发消息（`bot_mode_dm.py:59`，条件注册） | 由 `ensure_message_agent_tool()`（`:129`）动态构建 |

> `compact` / `context_usage` 是**注册在中央 registry 里、但执行被 agent 运行时拦截**的：`context_engine_tools.py` 模块 docstring 明写 dispatch 在 `agent/agent_runtime_helpers.py:invoke_tool` 和 `agent/tool_executor.py` 被拦截，路由到 per-agent 的 `context_compressor.handle_tool_call()`；registry handler 只是 stub。

---

## 5. 结果治理：大小上限 / 落盘 / 裁剪（全部实数）

### 5.1 三层防线总览

`tool_result_storage.py` 模块 docstring 明确写了三层：

```
层 1  每工具自带输出上限（tool 作者控制）
层 2  maybe_persist_tool_result()  单条超阈值 → 落盘 + 预览替换
层 3  enforce_turn_budget()        整轮聚合超预算 → 按大小从大到小落盘
```

### 5.2 `tool_output_limits.py`（3991 B）——可配置的输出截断

| 常量 | 值 | 对应旧硬编码位置 |
|---|---|---|
| `DEFAULT_MAX_BYTES` | **50_000** | `terminal_tool.MAX_OUTPUT_CHARS` |
| `DEFAULT_MAX_LINES` | **2000** | `file_operations.MAX_LINES` |
| `DEFAULT_MAX_LINE_LENGTH` | **2000** | `file_operations.MAX_LINE_LENGTH` |

配置段：`config.yaml` → `tool_output: {max_bytes, max_lines, max_line_length}`。读取器 `get_tool_output_limits()`（`:59`）**防御性**：任何异常/非法值回退默认，并做模块级缓存（`_cached_limits`）。访问器 `get_max_bytes()`（`:98`）/ `get_max_lines()`（`:103`）/ `get_max_line_length()`（`:108`）。

**终端输出裁剪是「头尾保留」而非单侧截断**（`terminal_tool.py:4166-4169`）：

```python
MAX_OUTPUT_CHARS = get_max_bytes()            # 默认 50_000
head_chars = int(MAX_OUTPUT_CHARS * 0.4)      # 40% 头（错误信息常在前）
tail_chars = MAX_OUTPUT_CHARS - head_chars    # 60% 尾（最近/最相关）
```

`file_operations.py:1117-1119` 仍是 `MAX_LINES = 2000` / `MAX_LINE_LENGTH = 2000` / `MAX_FILE_SIZE = 50 * 1024`（50 KB）。

### 5.3 `budget_config.py`（7810 B）——预算常量单一真相源

| 常量 | 值 | 位置 |
|---|---|---|
| `DEFAULT_RESULT_SIZE_CHARS` | **100_000** | `:17` |
| `DEFAULT_TURN_BUDGET_CHARS` | **200_000** | `:18` |
| `DEFAULT_PREVIEW_SIZE_CHARS` | **1_500** | `:19` |
| `DEFAULT_MCP_RESULT_SIZE_CHARS` | **50_000** | `:32` |
| `MCP_TOOL_PREFIX` | `"mcp_"` | `:36` |
| `PINNED_THRESHOLDS` | `{"read_file": float("inf")}` | `:11-13` |
| `_CHARS_PER_TOKEN` | **4** | `:123` |
| `_PER_RESULT_WINDOW_FRACTION` | **0.15** | `:130` |
| `_PER_TURN_WINDOW_FRACTION` | **0.30** | `:131` |
| `_MIN_RESULT_SIZE_CHARS` | **8_000** | `:135` |
| `_MIN_TURN_BUDGET_CHARS` | **16_000** | `:136` |

**为什么 `read_file` 被钉成 `inf`**：`budget_config.py:10` 注释 —— 防止「persist → read → persist」死循环。

**按上下文窗口缩放**：`budget_for_context_window(context_length)`（`:139`）

```
window_chars = context_length * 4
per_result  = clamp(window_chars * 0.15, 8_000, 100_000)
per_turn    = clamp(window_chars * 0.30, 16_000, 200_000)
```

即：大模型（200K+ token）**逐字节不变**（比例值被 clamp 到历史默认值作上限）；小模型（如 65K token）按比例缩小，并设下限保证预览仍可用。这是 issue #23767 的修复。

**阈值解析优先级**（`resolve_threshold()`，`:102-112`）：
`PINNED_THRESHOLDS` > `tool_overrides` > MCP 前缀（`min(mcp_result_size, default_result_size)`）> `registry.get_max_result_size()`（再被 `min(…, default_result_size)` 夹住）。

> 最后一层夹取的意义（`:95-100` 注释）：`web`/`terminal`/`x_search` 都注册了 100K 固定值；若不夹，小模型的缩放预算会被 per-tool 大值重新顶回去。

### 5.4 `tool_result_storage.py`（17214 B）——落盘与预览

| 常量 | 值 | 位置 |
|---|---|---|
| `PERSISTED_OUTPUT_TAG` / `..._CLOSING_TAG` | `<persisted-output>` / `</persisted-output>` | `:61-62` |
| `STORAGE_DIR` | `/tmp/hermes-results`（**沙箱侧**兜底目录） | `:63` |
| `SPILLOVER_SUBDIR` | `cache/spillover`（**host 侧规范位置**） | `:64` |
| `SPILLOVER_MAX_AGE_HOURS` | **24** | `:65` |
| `HEREDOC_MARKER` | `HERMES_PERSIST_EOF` | `:66` |
| `_BUDGET_TOOL_NAME` | `__budget_enforcement__` | `:67` |
| `_MAX_RESULT_FILENAME_STEM` | **120** | `:69` |
| `_MAX_RESULT_FILENAME_CHARS`（正则） | `[^A-Za-z0-9_.-]+` 替换 | `:68` |

落盘规范路径：`$HERMES_HOME/cache/spillover/{tool_use_id}.txt`。

`maybe_persist_tool_result()`（`:314`）流程：

1. `effective_threshold = threshold ?? config.resolve_threshold(tool_name)`
2. `threshold == inf` → 原样返回；`len(content) <= threshold` → 原样返回
3. `generate_preview(content, max_chars=config.preview_size)`（默认 1500 字符）
4. **先写 host 侧** `_write_to_spillover()`（`:145`）
5. 本地后端 → 返回 host 路径；远端后端 → `_sandbox_visible_spillover_path()`（`:162`）探可读后返回翻译路径；不可读 → `_write_to_sandbox()`（`:249`）写进沙箱临时目录
6. 全都失败 → **内联截断兜底**：`"[Truncated: tool response was N chars. Full output could not be saved to sandbox.]"`

`enforce_turn_budget()`（`:399`）：累计所有 tool message 的 `len(content)`；超 `config.turn_budget` 则把**未落盘的**结果按大小降序，逐个以 `threshold=0` 强制落盘，直到总量回到预算内。

清理：`cleanup_spillover_cache(max_age_hours=24)`（`:82`）+ `_prune_spillover_once()`（`:106`）——gateway housekeeping 每小时扫，纯 CLI 安装则在首次 spill 时自清一次。

### 5.5 `hook_output_spill.py`（8122 B）——hook 注入上下文的落盘

移植自 openai/codex PR #21069。背景：shell hook 与 `pre_llm_call` 插件返回的 `{"context": "..."}` 会被拼进**当前轮用户消息**，之后每次 API 调用都重发；一个大的 debug dump 会污染整个会话并打破 prompt cache 前缀。

配置（`config.yaml` → `hooks.output_spill`）：

| 键 | 默认值 |
|---|---|
| `enabled` | **true** |
| `max_chars` | **10000** |
| `preview_head` | **500** |
| `preview_tail` | **500** |
| `directory` | `null` → `<HERMES_HOME>/hook_outputs` |

不变式：`enabled: false` 或未超限 → 原样返回；**永不抛异常**（I/O 失败则退化为按字节截断 + 提示）；文件按会话分组（`/new` 不会让同一目录无限增长）。

### 5.6 `spill_safety.py`（4206 B）——符号链接安全的落盘原语

威胁模型（模块 docstring）：spill 文件写在 `~/.hermes` 下**可预测、世界可发现**的目录里。普通 `open(path,"w")` / `Path.write_text` 会**跟随预先植入的符号链接**，把写操作重定向到 `~/.bashrc`、`authorized_keys` 等任意用户文件。

防护（按构造拒绝链接）：

- 新文件一律 `O_CREAT | O_EXCL` —— 对**任何**已存在路径（含悬空符号链接）失败，绝不跟随
- 覆盖写先 `lstat` + `unlink`（删链接只删链接，不删目标），再独占重建
- check-then-create 不可被竞态利用，因为**创建本身是独占的**

两个隐私档：

| 档 | 目录权限 | 文件权限 | 用途 |
|---|---|---|---|
| `private=True`（默认） | `0o700` | `0o600` | 可能含未脱敏原始密钥的 spill（终端输出、hook 上下文） |
| `private=False` | umask 默认 | umask 默认 | 需被 bind-mount 进远端终端后端（Docker/Modal/SSH）的 cache 目录，非 root 容器 UID 必须可读 |

导出：`ensure_spill_dir` / `open_exclusive` / `write_text_exclusive`。

---

## 6. 错误分类与重试

### 6.1 `bot_failure_reasons.py`（172 行）——13 类 reason code + 重试策略

常量（`:26-42`）：

| 类别 | code |
|---|---|
| 传输/运行时 | `runtime_offline`、`queued_expired`、`delivery_timeout`、`agent_blocked`、`cancelled` |
| agent 侧 | `provider_auth_or_access`、`provider_quota_limit`、`provider_rate_limit`、`provider_server_error`、`context_overflow`、`missing_config`、`model_unavailable`、`unknown` |

`ALL_REASONS`（`:44`）13 个；`AUTO_RETRYABLE`（`:63`）= `{runtime_offline, delivery_timeout, provider_rate_limit, provider_server_error}`（**4 个**）。

**重试动作映射** `retry_action(reason)`（`:88`）：

| 输入 | 动作 | 语义 |
|---|---|---|
| `AUTO_RETRYABLE` 中的 4 个 | `"resume"` | 原会话原样重试（不改 session） |
| `context_overflow` | `"compress_then_resume"` | 先对会话做上下文压缩，再重试同一会话 |
| 其余（auth/quota/config/model/unknown） | `"none"` | **不自动重试**：重试修不好，只烧配额 |

维护者裁决（`bot_failure_reasons.py:73-81` 注释）：**重试的 bot turn 永不新建 session**。

**分类规则** `_RULES`（`:109`）：有序 `(regex, code)` 元组，**首个匹配生效**，明确「auth 优先于 quota」：

1. `authentication_error|invalid api key|401/403` → `provider_auth_or_access`
2. `402|out of funds|quota|balance` → `provider_quota_limit`
3. `429|rate.?limit` → `provider_rate_limit`
4. `5xx|server error|overloaded` → `provider_server_error`
5. `context length|context_overflow|maximum context` → `context_overflow`
6. `no llm provider configured|missing config|no access token` → `missing_config`
7. 兜底 → `unknown`

入口 `classify_agent_error(text)`（`:159`）；便捷判定 `is_auto_retryable(reason)`（`:68`）。

### 6.2 `mcp_tool.py:_classify_mcp_failure()`（`:1448`）——permanent / transient

| 判定 | 条件 |
|---|---|
| `"permanent"`（立即 park，不烧重试阶梯） | auth 失败（401/403）；`NonMcpEndpointError`（URL 是网页不是 MCP）；`InvalidMcpUrlError`（配置不可用）；`FileNotFoundError` / `OSError(ENOENT)`（stdio 命令不存在）；`HTTPStatusError` 401/403 |
| `"transient"`（保留正常退避阶梯） | 网络抖动、EOF、`ClosedResourceError`、transport TaskGroup 掉线、超时 |

`_unwrap_exception_group()` 先剥 `ExceptionGroup` 再判定。退避常量：`_CONNECT_RETRY_BASE_BACKOFF_SEC = 30.0`、`_CONNECT_RETRY_MAX_BACKOFF_SEC = 600.0`（`:4542-4543`）。

### 6.3 其他重试常量

| 位置 | 常量 | 值 |
|---|---|---|
| `swarm_scheduler.py:34` | `RATE_LIMIT_RETRY_BASE_MS` | **3000**（指数退避基数，×2^retryCount） |
| `swarm_scheduler.py:37` | `GLOBAL_RETRY_INTERVAL_MS` | **1500** |
| `tools/environments/vercel_sandbox.py:66-69` | `_CREATE_RETRY_ATTEMPTS` / `_WRITE_RETRY_ATTEMPTS` / `_RETRY_BACKOFF_STEP` | **3** / **3** / **100 ms** |
| `read_extract.py:81` | `ANYDOC_RETRY_SECONDS` | **300.0** |
| `delegation_output_schema.py:27` | `MAX_SCHEMA_RETRIES` | **1**（结构化输出只重试 1 次，带原文校验错误，不重贴 schema） |
| `vision_tools.py:458` | `_is_retryable_download_error()` | 下载错误可重试判定 |

**工具结果错误体**：`registry.py:_bound_error_text()` 上限 **2048** 字符（`_MAX_TOOL_ERROR_CHARS`），日志 **8192**（`_MAX_LOGGED_ERROR_CHARS`）。

---

## 7. 审批与安全（8 个模块各管什么 + 具体黑名单）

### 7.1 模块职责表

| 文件 | 大小 | 管什么 |
|---|---|---|
| `approval.py` | **268 KB / 6056 行** | 危险命令系统的**唯一真相源**：模式检测（`DANGEROUS_PATTERNS` / `detect_dangerous_command`）、per-session 审批状态（线程安全，按 `session_key`）、审批提示（CLI 交互 + gateway 异步）、aux-LLM 智能审批、永久 allowlist 持久化 |
| `write_approval.py` | 19 KB / 492 行 | **memory / skills 写入**的审批门 + 待批存储（`<HERMES_HOME>/pending/{memory,skills}/<id>.json`）。配置键 `write_approval`（`CONFIG_KEY`，`:67`），子系统常量 `MEMORY="memory"` / `SKILLS="skills"`（`:58-59`）。`false`（默认）= 自由写；`true` = 不提交，改为内联提示（memory，仅交互式 CLI）或暂存到 pending store 待用户带外批准 |
| `path_security.py` | 1.3 KB / 44 行 | 共享路径校验：`validate_within_dir(path, root)` 用 `Path.resolve()` + `relative_to()` 防 `..` 穿越与符号链接逃逸。原先是 skill_manager_tool / skills_tool / skills_hub / cronjob_tools / credential_files 各自复制的逻辑 |
| `url_safety.py` | 33.6 KB / 874 行 | **SSRF 防护**：阻断指向私有/内网地址的请求（云 metadata、localhost、私网）。全局开关 `security.allow_private_urls`，但 **metadata 端点永远封**。提供 `create_ssrf_safe_client()` / `create_ssrf_safe_async_client()`（`:825`/`:841`），在 TCP connect 前一刻复检并连到已验证 IP（保留 Host/SNI），缓解 DNS rebinding TOCTOU |
| `website_policy.py` | 9.6 KB / 283 行 | **网站黑名单**：从 `config.yaml` 的 `security.website_blocklist` 读用户维护的域名黑名单 + 可选共享列表文件。配置键：`enabled`（默认 false）、`domains`（list）、`shared_files`（list）。短 TTL 内存缓存（避免 50 URL 的 extract 触发 51 次 YAML 解析）。异常类 `WebsitePolicyError`（`:44`） |
| `threat_patterns.py` | 14 KB / 286 行 | **提示注入 / promptware / 外泄**模式库的唯一真相源。**36 条** `(regex, pattern_id, scope)`，供 `agent/prompt_builder.py`、`memory_tool.py`、`agent/tool_dispatch_helpers.py` 共用。入口 `scan_for_threats(content, scope="context")`（`:211`）、`first_threat_message(content, scope="strict")`（`:262`） |
| `tirith_security.py` | 34.5 KB / 901 行 | **tirith 预执行扫描**包装：把 tirith 二进制当子进程跑，扫命令里的内容级威胁（同形字 URL、pipe-to-interpreter、终端注入等）。**退出码是判定的唯一真相**：`0=allow`、`1=block`、`2=warn`；JSON stdout 只丰富 findings，绝不覆盖判定。运维失败（spawn 错、超时、未知退出码）遵守 `tirith_fail_open`；编程错误则向上抛。默认配置（`:72-75`）：`tirith_enabled=True`、`tirith_path="tirith"`、`tirith_timeout=5`、`tirith_fail_open=True`；env 覆盖 `TIRITH_ENABLED`/`TIRITH_BIN`/`TIRITH_TIMEOUT`/`TIRITH_FAIL_OPEN`。缺失时自动从 GitHub Releases 下到 `$HERMES_HOME/bin/tirith`，**始终校验 SHA-256**，有 cosign 时再做 provenance 验证 |
| `osv_check.py` | 7.2 KB / 218 行 | **MCP 扩展包恶意软件检查**：`npx`/`uvx` 启动 MCP server 前查 OSV API，**只阻断 MAL-\* 恶意公告，普通 CVE 忽略**。端点 `https://api.osv.dev/v1/query`（env `OSV_ENDPOINT` 可覆盖），`_TIMEOUT = 10` s，典型延迟 ~300 ms。**Fail-open**：网络错误放行。带结果缓存（`(ecosystem, package, version) → (expiry, result)`），因为 MCP 重连阶梯会对同一包反复 preflight（#75485 事故记录：16 小时内 779K 次 api.osv.dev DNS 查询） |

### 7.2 `approval.py` 的不可逆动作黑名单

**两级**：`HARDLINE_PATTERNS`（**12 条**，`approval.py:551`，**无条件拦截，任何模式/allowlist 都绕不过**）+ `DANGEROUS_PATTERNS`（**99 条**，`approval.py:945`，走审批流程）。

#### 7.2.1 HARDLINE（12 条，硬地板）

| # | 描述 | 模式要点 |
|---|---|---|
| 1 | `recursive delete of root filesystem` | `rm -r*` 目标解析为 `/`（含 `//`、`/.`、`/..`、`/../..`、`/*`、`/ *`） |
| 2 | `recursive delete of system directory` | `_HARDLINE_SYSTEM_DIRS`（`:531-534`）：`/home`、`/root`、`/etc`、`/usr`、`/var`、`/bin`、`/sbin`、`/boot`、`/lib`（及各自 `/*`） |
| 3 | `recursive delete of home directory` | `~`、`$HOME`、`${HOME}`、`~/*` |
| 4 | `format filesystem (mkfs)` | `mkfs` / `mkfs.<fs>` |
| 5 | `dd to raw block device` | `dd … of=/dev/{sd,nvme,hd,mmcblk,vd,xvd}*` |
| 6 | `redirect to raw block device` | `> /dev/{sd,nvme,hd,mmcblk,vd,xvd}*` |
| 7 | `fork bomb` | `:(){ :|:& };:` |
| 8 | `kill all processes` | `kill -… -1` |
| 9 | `system shutdown/reboot` | `shutdown` / `reboot` / `halt` / `poweroff` |
| 10 | `init 0/6 (shutdown/reboot)` | `init 0` / `init 6` |
| 11 | `systemctl poweroff/reboot` | `systemctl poweroff|reboot|halt|kexec` |
| 12 | `telinit 0/6 (shutdown/reboot)` | `telinit 0` / `telinit 6` |

两个工程细节（避免误杀，也避免被绕过）：

- **命令位置锚定** `_CMDPOS`（`approval.py:549 _RM_FLAG_PREFIX = _CMDPOS + r'rm\s+(-[^\s]*\s+)*'`）：只在 `rm` 是**真正的命令词**时触发（行首、`;`/`&&`/`||`/`|` 之后、`$(`/反引号之后、`sudo`/`env`/`exec` 包装之后）。因此 `gh pr create --title "block rm -rf / spellings"` 这种把 `rm -rf /` 当**数据**的命令不会被地板拦住（issue #93392）。
- **引号遮蔽** `_QUOTE_MASKED_HARDLINE_DESCRIPTIONS = {"redirect to raw block device", "fork bomb"}`（`:621`）+ `_mask_quoted_prose()`（`:655`）：这两条触发点没有命令名词可锚定，所以对**引号遮蔽变体**匹配；但 `_SHELL_CARRIER_NAMES = {eval, sh, bash, zsh, ksh, dash, source, .}`（`:639`）出现在命令位置时，**原文不遮蔽也扫一遍**——引号不是绕过手段。

预编译：`HARDLINE_PATTERNS_COMPILED`（`:626`）、`DANGEROUS_PATTERNS_COMPILED`（`:1275`）。注释给出动机：首次 `terminal()` 冷缓存重新 compile 12+47 条约 **2.6 ms**，且 Python 的 512 项 `re._cache` 会被别处的正则挤掉。

#### 7.2.2 DANGEROUS_PATTERNS（99 条）——按类归并

**A. Unix 破坏性删除 / 磁盘（约 12 条）**
`rm … /`（"delete in root path"）、`rm -r*`（"recursive delete"）、`rm --recursive`、`rm` + 长参数形式、`mkfs`、`dd … if=`（"disk copy"）、`> /dev/sd`（"write to block device"）、`xargs … rm`、`find … -exec rm`、`find … -delete`

**B. Windows 破坏性操作（约 14 条）**
`cmd /c|/k … del|erase|rd|rmdir`、`powershell|pwsh … Remove-Item|rmdir|erase|del|rd|ri|rm`、`powershell … -EncodedCommand|-enc|-e`、`Remove-Item -Recurse|-Force`、`del|erase|rd|rmdir /s|/q`、`iwr|Invoke-WebRequest|irm|curl|wget | iex`、`iex(iwr …)`、`taskkill /F`、`Stop-Process -Force`、`Format-Volume`、`Clear-Disk`、`diskpart`、`format.com X:`、`cipher /w`、`icacls … /grant Everyone`（含本地化 `todos`/`jeder`/`tout le monde`/`*S-1-1-0`）、`icacls /reset`、`vssadmin … delete shadows`、`wbadmin … delete`、`bcdedit /set`、`reg delete`、`Remove-ItemProperty -Force`、`Stop-Service -Force`、`sc stop|delete`、`users\<x>\.ssh`（Windows 路径读 SSH 密钥）、`appdata\{local,roaming}\hermes…\.env`（读 Hermes 密钥）

**C. 权限 / 属主（4 条）**
`chmod 777|666|o+*w|a+*w`、`chmod --recursive` + 上述、`chown -R root`、`chown --recursive root`

**D. 数据库（3 条）**
`DROP TABLE|DATABASE`、`DELETE FROM`（无 `WHERE`）、`TRUNCATE TABLE`

**E. 进程 / 服务终止（6 条）**
`systemctl stop|restart|disable|mask`、`kill -9 -1`、`pkill -9`、`killall -KILL`、`killall -s KILL`、`killall -r`

**F. 远程代码执行 / pipe-to-shell（约 8 条）**
`curl|wget | sh|bash`、`bash|sh < <(curl|wget)`、`eval|source|. $(curl|wget)`、`base64 -d | sh`、`xxd -r | sh`、`echo | tr | sh`、`openssl base64|enc -d | sh`、`bash|sh <<`（heredoc）

**G. 敏感文件覆写（约 9 条）**
`tee` / `>>` 写系统配置（`_SYSTEM_CONFIG_PATH`）、`tee` / `>>` 写项目 `.env`/config（`_PROJECT_SENSITIVE_WRITE_TARGET`）、`cp|mv|install` 到系统配置 / 项目 env / 凭证-`.ssh`-shell-rc 路径、`sed -i` / `sed --in-place` / `perl|ruby -i` 改凭证-`.ssh`-rc 路径、`sed -i` 改系统配置、`sed -i` 改 Hermes config/env

**H. Hermes 自身生命周期（约 8 条，自终止保护）**
`hermes gateway stop|restart`、`hermes update`、`docker -H|--host|--context`、`docker context use`、`podman --url|--connection|--identity`、`podman -r|--remote`、`DOCKER_HOST|DOCKER_CONTEXT|CONTAINER_HOST|CONTAINER_CONNECTION=`、`docker compose restart|stop|kill|down`、`docker restart|stop|kill`、`gateway run` 后台化（`&`/`disown`/`setsid`）、`nohup … gateway run`、`pkill|killall … hermes|gateway|cli.py`、`kill $(pgrep|pidof)`、`kill \`pgrep|pidof\``、`launchctl stop|kickstart|bootout|unload|kill|disable|remove` + `hermes`/`ai.hermes`

**I. git 破坏性操作（6 条）**
`git reset --hard`、`git push --force`、`git push -f`、`git clean -f`、`git branch -D`、`git branch -d|--delete` + `-f|--force`（两种参数顺序）

**J. 其他（约 3 条）**
`chmod +x` 后立即 `./` 执行、`sudo -s|--shell|-a|--login`、`sudo -[a-z]*[sa][a-z]*`

#### 7.2.3 审批模式与旁路

`_normalize_approval_mode()`（`approval.py:3490`）：合法值只有 **`manual` / `smart` / `off`**（`:3501`），未知值告警后回落 `manual`。YAML 1.1 把裸 `off` 解析成布尔 → 专门处理成字符串 `"off"`（`:3502-3503`）。

三种旁路源（`is_approval_bypass_active_for_session()`，`:3551`）：

1. 进程级 `--yolo` / `HERMES_YOLO_MODE`（`_YOLO_MODE_FROZEN`，**import 时冻结**，防止运行中被 skill 翻转——这是一条提示注入提权路径）
2. 会话级 gateway `/yolo` 开关
3. `approvals.mode: off`

> **本机实况**：`config.yaml` → `approvals: mode: 'off'`，即**审批提示全部旁路**。硬地板（HARDLINE 12 条）仍然有效。

还有三种上下文策略：`_get_cron_approval_mode()`（`:3619`，默认 `deny`）、`_get_single_query_approval_mode()`（`:3632`，`approvals.single_query_mode`，默认 `deny`）、`_get_unattended_approval_mode()`（`:3645`）。

**人类等待时间豁免**：`_HumanWaitState`（`:2608`）+ `_HUMAN_WAIT_MAX_SESSIONS` —— 按 session 记录「可证实地阻塞在人审批提示上的墙钟时间」，`agent/tool_executor.py` 的并发批次 deadline 会**扣掉这段**，但**只扣这段**。理由（`:2591-2605` 注释）：在源头（人类等待）计量，而不是在授权门里（那是任意代码）计量，否则一个卡死的 `pre_tool_call` 插件或死掉的审批客户端就能把豁免 1:1 涨成墙钟，彻底废掉 deadline（#79719）。

**智能审批**：`_smart_approve(command, description)`（`:3725`），`approval_mode == "smart"` 时经 aux LLM 自动批准低风险命令；`_prepare_smart_approval_observer()`（`:141`）/ `_observe_smart_approval_verdict()`（`:176`）记录判定用于审计。

**子 agent 审批**：`delegate_tool.py:79 _subagent_auto_deny()` / `:93 _subagent_auto_approve()`。因为子 agent 跑在 `ThreadPoolExecutor` worker 里，CLI 的交互审批回调存在 `terminal_tool.py` 的 `threading.local()` 里，worker **继承不到**；没有回调时会从 worker 线程 `input()`，与拥有 stdin 的父 TUI **死锁**。所以用 `ThreadPoolExecutor(initializer=_set_subagent_approval_cb, initargs=(cb,))` 注入非交互回调。配置 `delegation.subagent_auto_approve`（默认 **False** → auto-deny，安全；`true` → 自动批准，cron/batch 的 opt-in YOLO）。两条路径都打 `logger.warning` 供审计。

### 7.3 `threat_patterns.py` 的 36 条模式（按 scope 分组）

AST 精确计数：`_PATTERNS` 共 **36** 条，分布 **11 / 17 / 8**。

| scope | 条数 | pattern_id |
|---|---|---|
| `all`（处处生效） | **11** | `prompt_injection`、`sys_prompt_override`、`disregard_rules`、`bypass_restrictions`、`html_comment_injection`、`hidden_div`、`translate_execute`、`deception_hide`、`exfil_curl`、`exfil_wget`、`read_secrets` |
| `context`（上下文文件 + memory + tool 结果） | **17** | `role_hijack`、`role_pretend`、`leak_system_prompt`、`remove_filters`、`fake_update`、`identity_override`、`c2_node_registration`、`c2_heartbeat`、`c2_task_pull`、`c2_network_connect`、`forced_action`、`anti_forensic_oneliner`、`anti_forensic_disk`、`env_var_unset_agent`、`known_c2_framework`、`c2_explicit`、`c2_explicit_long` |
| `strict`（memory 写入 + 技能安装） | **8** | `send_to_url`、`context_exfil`、`ssh_backdoor`、`ssh_access`、`hermes_env`、`agent_config_mod`、`hermes_config_mod`、`hardcoded_secret` |

**为什么分 scope**（模块 docstring `:19-27`）：tool 结果里是网页、GitHub issue、MCP 响应——**用户没有撰写的内容**，那里要**广谱检测**；但**阻断**只留给用户能介入的路径（memory 写入、技能安装）。

**锚定原则**：新模式锚在 **C2 专有词汇或明确的攻击行为**上，不锚「命令式英语」。"you are obligated to"、"you must" 这类在正常指令文档（AGENTS.md、CLAUDE.md）里太常见，会大量误报。

### 7.4 `url_safety.py` 的 SSRF 封禁清单（具体 IP）

**永远封禁的主机名**（`_BLOCKED_HOSTNAMES`，`:166-169`）：`metadata.google.internal`、`metadata.goog`

**永远封禁的 IP**（`_ALWAYS_BLOCKED_IPS`，`:180-191`）：

| IP | 说明 |
|---|---|
| `169.254.169.254` | AWS/GCP/Azure/DO/Oracle metadata |
| `169.254.170.2` | AWS ECS task metadata（task IAM creds） |
| `169.254.169.253` | Azure IMDS wire server |
| `fd00:ec2::254` | AWS metadata（IPv6） |
| `100.100.100.200` | 阿里云 metadata |
| `::ffff:169.254.169.254` / `::ffff:169.254.170.2` / `::ffff:169.254.169.253` / `::ffff:100.100.100.200` | IPv4-mapped IPv6 变体（Python `ipaddress` 视为不同地址，必须显式列出） |

**永远封禁的网段**（`_ALWAYS_BLOCKED_NETWORKS`，`:192-195`）：`169.254.0.0/16`、`::ffff:169.254.0.0/112`

**额外显式网段**：`_CGNAT_NETWORK = 100.64.0.0/10`（`:210`）——`ipaddress.is_private` 对 CGNAT 返回 False，必须单独封。用于运营商级 NAT、Tailscale/WireGuard、部分云内网。

**窄白名单**：`_TRUSTED_PRIVATE_IP_HOSTS = {"multimedia.nt.qq.com.cn"}`（`:200-202`）——QQ 媒体下载在本地代理/benchmark 基础设施后可能合法解析到 `198.18.0.0/15`。

**连接上限**：`_MAX_SSRF_CONNECT_IPS = 8`（`:204`）。

**已知局限**（docstring 自述）：DNS rebinding（TTL=0 先返回公网 IP 过检、再返回私网 IP 实连）靠 `create_ssrf_safe_client()` 缓解；重定向绕过靠 httpx event hook 对每个跳转目标复检（`vision_tools`、gateway 平台适配器、媒体缓存助手），但 `web` 工具走第三方 SDK（Firecrawl/Exa），重定向在其服务端处理。

---

## 8. 中断与预算

### 8.1 `interrupt.py`（5463 B）——per-thread 中断信号

问题：gateway 里同一进程并发跑多个 agent，**中断一个会话不能杀掉别的会话里的工具**。

机制：

```python
_interrupted_threads: set[int]              # 被打断的 thread ident
_interrupt_reasons: dict[int, str]          # 用户可安全展示的原因（不含来消息原文）
_lock = threading.Lock()
```

API：

| 函数 | 位置 | 语义 |
|---|---|---|
| `set_interrupt(active, thread_id=None, *, reason=None)` | `:41` | `thread_id=None` → 当前线程（CLI/测试向后兼容） |
| `is_interrupted()` | `:64` | 查**当前线程**，无参 |
| `is_thread_interrupted(thread_id)` | `:73` | 查指定线程；`None` 永不为中断。用于把等待搬到 deadline worker 后，`/stop` 仍能杀掉原始 tool-worker 的子进程（#94285） |
| `get_interrupt_reason()` | `:88` | 当前线程的中断原因 |
| `clear_current_thread_interrupt()` | `:94` | 在刚被批准的命令 spawn 子进程前清掉**当前线程**的陈旧中断位，避免批准等待期间落下的 bit 把这次运行 SIGINT 成 exit 130。注释强调**直接调用、绝不通过 `_interrupt_event` 代理**（代理的 `.clear()` 绑到执行它的线程） |
| `_ThreadAwareEventProxy` / `_interrupt_event` | `:118`/`:140` | 向后兼容垫片：`code_execution_tool`、`process_registry`、测试直接 import `_interrupt_event` 并调 `.is_set()/.set()/.clear()` |

调试：`HERMES_DEBUG_INTERRUPT` 环境变量打开 per-call 日志（调用线程 / 目标线程 / 当前集合），并把 `tools` logger 从 CLI 启动时强制降到的 ERROR 拉回 INFO。

工具侧用法（模块 docstring）：

```python
from tools.interrupt import is_interrupted
if is_interrupted():
    return {"output": "[interrupted]", "returncode": 130}
```

### 8.2 `budget_config.py` —— 见 §5.3（预算常量与上下文缩放全部实数在那里）

---

## 9. 子 agent / 委派的分层与预算模型

### 9.1 分层（8 个模块的职责）

| 模块 | 职责 |
|---|---|
| `delegate_tool.py`（235 KB / 5368 行） | **门面 + 编排**：`delegate_task` 工具 schema、角色/深度判定、子 agent 构建、批量并行、进度事件、摘要预算 |
| `subagent_runner.py`（5.7 KB） | **共用执行入口**：`build_and_run_subagent(goal, parent_agent, context=None, max_iterations=50, role="leaf", swarm_index=None, swarm_item=None, abort_signal=None) -> dict`。`delegate_task` 与 `agent_swarm` 的唯一代码路径 |
| `async_delegation.py`（66 KB） | **异步/后台委派注册表**：支撑 `delegate_task(background=true)`。派发后立刻返回 handle；子 agent 完成后把完成事件推给**共享的** `process_registry.completion_queue`（`type="async_delegation"`），CLI `process_loop` 与 gateway watcher 已经在轮询该队列 → 完成事件以**新 turn** 浮现，而不是插在 tool result 与 assistant 消息之间（保证 role 严格交替 + prompt cache 不破） |
| `agent_swarm.py`（14 KB） | **模板化批量**：`agent_swarm` 工具，`prompt_template` + `{{item}}` + `items` |
| `swarm_scheduler.py`（16 KB） | **限流感知调度器** `SwarmBatchScheduler` |
| `subagent_worktree.py`（13 KB） | **opt-in git worktree 隔离**：每个子 agent 从父仓库当前 HEAD 拉独立 worktree |
| `delegation_output_schema.py`（5.8 KB） | 结构化输出契约（`MAX_SCHEMA_RETRIES = 1`） |
| `delegation_live_log.py`（17.8 KB） | 每个子 agent 一份 append-only 可 tail 的日志（`<hermes_home>/cache/delegation/live/<id>/task-<n>.log`，保留 7 天） |

### 9.2 深度与角色

| 常量 / 函数 | 值 / 位置 | 语义 |
|---|---|---|
| `MAX_DEPTH` | **1**（`delegate_tool.py:130`） | 默认扁平：parent(0) → child(1)；孙 agent 被拒，除非抬高 `max_spawn_depth` |
| `_get_max_spawn_depth()` | `:1031` | 读 `delegation.max_spawn_depth`，**下限 1、无上限**；非法值回落默认 2（`:1237 max_spawn_depth: int = 2`） |
| 角色 | `role="leaf"` / `role="orchestrator"` | **只有 `orchestrator` 才能再派工**（`:1303`、`:1396`）；`orchestrator` 会被重新加回 `delegation` toolset（`:1906`）。模型**没有** `toolsets` 参数，无法自己声明 |
| `_COMPOSITE_BLOCKED_TOOLSETS` | `frozenset({"delegation"})`（`:1374`） | 组合/复合模式下屏蔽 `delegation` toolset |
| `_get_orchestrator_enabled()` | 配合 `delegation.orchestrator_enabled` | `orchestration_available = _get_max_spawn_depth() >= 2 and _get_orchestrator_enabled()`（`:5079`） |

### 9.3 子 agent 能力裁剪

`DELEGATE_BLOCKED_TOOLS`（`delegate_tool.py:51-59`）——子 agent **永远拿不到**这 5 个工具：

| 工具 | 理由（源码注释） |
|---|---|
| `delegate_task` | 不许递归委派 |
| `clarify` | 不许和用户交互 |
| `memory` | 不许写共享 MEMORY.md |
| `send_message` | 不许有跨平台副作用 |
| `cronjob` | 不许以父 agent 名义排更多活 |

子 agent 继承父的 toolsets，再剥掉上述黑名单（`:1-20` 模块 docstring）；`DEFAULT_TOOLSETS = ["terminal", "file", "web"]`（`:1186`）。MCP toolset 会被保留（`_is_mcp_toolset_name`，`:1145`）。

### 9.4 预算模型（全部实数）

| 常量 | 值 | 位置 | 语义 |
|---|---|---|---|
| `_DEFAULT_MAX_CONCURRENT_CHILDREN` | **10** | `:123` | 默认并发子 agent 上限；可被 `delegation.max_concurrent_children` 覆盖（`:933-942`） |
| `DEFAULT_MAX_ITERATIONS` | **250** | `:1150` | 每个子 agent 的迭代上限（`subagent_runner.py` 默认参数是 50，delegate 路径用 250） |
| `DEFAULT_MAX_SUMMARY_CHARS` | **24_000** | `:1154` | 单份摘要的硬字符上限（叠在动态预算之上，兜住不听「简洁」指令的模型；`0` 关闭） |
| `_SUMMARY_HEADROOM_FRACTION` | **0.5** | `:1159` | 整批子 agent 摘要可占父 agent **剩余**上下文余量的比例；per-summary 预算 = 这个切片 / 批大小，防 N 个孩子集体撑爆父窗口（#9126 的压缩/429 死亡螺旋） |
| `_MIN_SUMMARY_CHARS` | **2_000** | `:1162` | 单份摘要下限（父 agent 快满时仍给可用切片，否则截成噪声） |
| `DEFAULT_CHILD_TIMEOUT` | **None** | `:1168` | 默认**无**墙钟上限——合法重活（深度评审、研究扇出、慢推理模型）被中途杀掉是真实痛点。用户可 `delegation.child_timeout_seconds` opt-in |
| `_HEARTBEAT_INTERVAL` | **30 s** | `:1169` | 委派期间父 agent 活动心跳间隔 |
| `_HEARTBEAT_STALE_CYCLES_IDLE` | **15** | `:1184` | 15 × 30 s = **450 s** 轮次间无活动 → 判定卡死 |
| `_HEARTBEAT_STALE_CYCLES_IN_TOOL` | **40** | `:1185` | 40 × 30 s = **1200 s** 卡在同一个工具 → 判定卡死 |
| `SUBAGENT_FAILURE_STATUSES` | `{"failed","error","timeout"}` | `:170` | 失败态集合 |
| `MAX_SCHEMA_RETRIES` | **1** | `delegation_output_schema.py:27` | 结构化输出只重试 1 次，带**原文校验错误**，不重贴 schema |

**心跳语义要点**（`:1170-1183` 注释）：在飞的模型等待**不算 idle**——`direct_api_call` 会在请求打开期间刷新 `last_activity_ts`，监控把时间戳推进视为进展（与 streamed chunk / async stall monitor 同一信号）。慢速本地 GGUF / 长 prefill 模型不能因为单次 completion 超过 idle 窗口就被杀。

### 9.5 `swarm_scheduler.py` 调度参数

| 常量 | 值 | 语义 |
|---|---|---|
| `INITIAL_LAUNCH_LIMIT` | **5** | 正常相位立即突发 5 个 |
| `INITIAL_LAUNCH_INTERVAL_MS` | **700** | 之后节流到每 700 ms 一个 |
| `RATE_LIMIT_RETRY_BASE_MS` | **3000** | 429 相位指数退避：3000 × 2^retryCount |
| `RATE_LIMIT_CAPACITY_SHRINK_INTERVAL_MS` | **60000** | 每 60 s 缩一次容量 |
| `CAPACITY_RECOVERY_MS` | **180000** | 静默 3 分钟后容量恢复 |
| `GLOBAL_RETRY_INTERVAL_MS` | **1500** | 限流相位下 launch 的最小间隔 |

规则：429 的任务**重排到队首**（不是队尾）；只剩一个未完成任务时直接判失败（不挂死）；用户取消保留已完成结果、把在飞/未启标为 aborted，非用户取消整批拒绝；单任务超时只失败该任务、不进入限流相位。

### 9.6 `subagent_worktree.py` 隔离契约

配置 `delegation.worktree_isolation: true`（默认 **false**）。契约：

- **opt-in 且仅 git**：非 git 工作区静默忽略，子 agent 共享父目录，行为不变
- **每个子 agent 一个 worktree**，从父仓库当前 `HEAD` 拉出，路径 `<repo>/.worktrees/subagent-<id>`，分支 `hermes-subagent/<id>`
- **父 agent 审/合**：每个结果条目报告 worktree 路径、分支、commit 数、dirty 状态
- **干净的自动清理**：无新 commit 且工作区干净的 worktree 在子 agent 结束后自动删除

---

## 10. 其他值得记录的机制（不在任务清单内但影响 Orbit 对齐）

1. **`lazy_deps.py`（55 KB）**：`ensure()`（`transcription_tools.py:346` 等调用）按需加载可选依赖，`FeatureUnavailable(RuntimeError)`（`:338`）是统一失败态。这解释了「不安装新依赖」在本产品里是**一等公民**设计而非限制。
2. **`plugin_guard.py` / `self_repo_guard.py`**：插件与自仓库写入的守卫层，与 `registry` 的 `_PluginOverridePolicy` 配套。
3. **`managed_tool_gateway.py` / `tool_backend_helpers.py`**：托管 provider 网关（`NOUS_MANAGED_PROVIDER = "nous"`），`resolve_provider_secret()` 是 provider 密钥的唯一解析入口。
4. **`web_result_cache.py`**：web 结果缓存；与 `tool_result_storage` 的 spillover 目录并列在 `_CACHE_DIRS`，被 bind-mount 进远端后端。
5. **`ansi_strip.py` / `terminal_post_process.py` / `terminal_command_rewrite.py` / `shell_heredoc.py`**：终端输出的清洗与命令改写管线（Windows 另有 `pwsh_fix.py` / `powershell_session.py`）。
6. **`fuzzy_match.py`（49 KB）**：`patch` 的 9 策略模糊匹配引擎，是本产品编辑体验的核心。
7. **`binary_extensions.py` / `audio_container.py` / `image_source.py`**：媒体类型识别与 SSRF-安全的图片源解析（`SourceUnsafe` / `SourceTooLarge` / `SourceNotFound` / `NotAnImage`，`image_source.py:52-74`）。

---

## 11. 附：复现脚本

同目录下：

- `_scan_tools.py` —— AST 扫 `registry.register()`，输出 `_tools_raw.json`（99 调用点 / 86 具名 / 64 schema 常量）
- `_scan_desc.py` —— 扫所有含 `name`+`description` 的 dict 字面量，输出 `_tool_desc.json`（87 键，其中 2 个是 `browser_supervisor.py` 的 HTTP 头误命中）

关键校验命令（只读）：

```powershell
$t = "E:\Hermes Agent CN Desktop\data\versions\0.21.0-cn.18\_internal\tools"
(Get-ChildItem $t -Filter *.py).Count                      # 147
$exe = "E:\Hermes Agent CN Desktop\data\versions\0.21.0-cn.18\hermes-agent-cn-runtime-win32-x64.exe"
& $exe tools --help; & $exe tools list --help; & $exe tools enable --help
```

**未能证实的项（明确标注）**：

1. `_HERMES_CORE_TOOLS` 的**具体成员**——`toolsets` 模块无 `.py` 源码（在 exe 的 PYZ 内）。只能证实其**性质**（显式白名单、不含 `desktop_ui`/`project`）与**消费者**（`tool_search.py:226`）。
2. per-platform 启用集的**具体存储位置与默认值**——只能从 CLI help 证实 `(platform, name)` 模型与 `--platform` 参数，以及 `--summary` 存在。
3. `toolset_distributions` 模块的**分发规则**——无源码。
4. `compact` / `context_usage` 的**完整参数表**——schema 在 `context_engine_tools.py` 中以运行时构造方式生成，未落到可静态提取的 `*_SCHEMA` 字面量。
5. `computer_use` 的**完整参数表**——schema 在 `tools/computer_use/schema.py`，由 `COMPUTER_USE_SCHEMA` 引用但未展开为 `"name"`+`"description"` 同层 dict。
