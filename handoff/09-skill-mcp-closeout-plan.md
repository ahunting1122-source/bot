# 方向 9 · Skill 层操作面 + MCP 加固（合并方案）

**先读 `handoff/00-CONTEXT.md`、`05-skill-layer.md`、`07-mcp-hardening.md`、`PROGRESS-WATCH.md`。**

## 为什么把两件事合成一个方向

核查（2026-10-08）发现两层的缺口是同一件事的两半：

| 层 | 缺口 | 本质 |
| --- | --- | --- |
| Skill | 无 API、无前端（`frontend/src` grep `skill` 零命中） | 能力完整但**零操作面**，只能手工编辑 md |
| MCP | `prompts` 未登记为外部技能 | 外部流程知识**进不了技能层** |

两件事共用同一个落点：`SkillProvider` 的**多源化**。Skill 的操作面要做"读某来源的技能"，
MCP prompts 要做"让 MCP 成为第二个来源"。分开做会重复设计两遍来源标识与合并口径，
合并做只设计一次。**MCP 的生命周期加固（心跳/重连）与资源接入（resources）则与之无关，
但同属方向 7，一并做掉避免再开一轮。**

## 前置：两个已确认的坑（不要在实现时重新踩）

1. **`store.lock` 是全局非可重入锁**（`storage.py:279`，`Store.lock = asyncio.Lock()`），
   `_touch_skill`/`_set_skill_status` 用的是 `async with self.store.lock, self.store.sessions()`。
   → **任何新增的 `SkillProvider` 方法若内部调 `sync_to_store`，绝不能自己再持锁**，
   否则自死锁。新代码统一走 `store.sessions()`，锁只在 `Store` 方法内部用。
2. **`confirmation=True` 在 v1 零确认模型下是死配置**（`nodes.gate` 已整体移除确认分支）。
   本次顺手把它降级为**纯审计标签**：保留字段、保留 `GET /api/tools` 展示，
   但**删掉前端"默认每个工具都要确认 / auto_approve 免确认"这两句误导文案**。

---

## 阶段 A · Skill 操作面（API + 前端）

### A1 API（`main.py`，约 5 个端点）

```
GET    /api/skills                → 列表（含治理态与统计）
GET    /api/skills/{name}         → 单个技能正文
PUT    /api/skills/{name}         → 保存/更新正文（原样写文件，version+1）
DELETE /api/skills/{name}         → 删除（见 A3 的三态语义）
POST   /api/skills/{name}/status  → 治理态转移
```

列表返回字段（**文件元数据 + 表统计合并**，正好是 `catalog_with_stats` 已有的口径）：

```json
{
  "name": "web_search", "scope": "site", "version": 1,
  "triggers": ["..."], "status": "active",
  "usage_count": 0, "success_rate": 0.0,
  "consecutive_failures": 0, "last_used_at": "", "updated_at": "..."
}
```

要点：
- `PUT` 只做最基础校验（`_validate_name` + 正文非空 + 三个 slot 的 `scope`），
  **不做治理决策**——与 `save_skill` 的分工一致：API 是"人改文件"，模型沉淀才走四道闸。
- `PUT` **不要**在 `SkillProvider` 上新开 `save_raw` 越过 `sync_to_store`（否则绕开"version 递增
  才复活治理态"规则，前端一保存就把 `stale` 技能偷偷复活）。正确写法：写文件后用
  `load_skill(root, name)` 读回，再走 `sync_to_store`。
  **已核查（2026-10-08）**：`sync_to_store` 自己不持 `store.lock`，它只调 `store.save(row)`，
  而 `store.save` 内部是 `async with self.lock, self.sessions()`（`storage.py:303`）——
  即**锁由 `Store` 逐行持有并释放，不是跨整个 sync 持锁**。所以 A1 的 PUT 路径**无死锁风险**，
  唯一约束是：**新代码不得自己 `async with store.lock`**（非可重入，一碰就自死锁）。
- 响应里带 `notice` 字段说明本次是"新建 / 更新到 vN"。

### A2 前端：新 `Skills.tsx`（不要塞进 `Settings.tsx`）

`Settings.tsx` 已 586 行、还塞着 MCP 区。技能需要一个**独立导航页**：

- 入口：`App.tsx` 导航加一项 `skills`（和 `memories`/`summaries` 同级）。
- 列表：左侧技能名 + scope 徽标 + 状态徽标；状态用色区分
  （`unreviewed` 灰 / `active` 绿 / `stale` 琥珀 / `archived` 灰划掉）。
- 详情：正文 `textarea`（等宽）+ triggers 编辑（每行一条）+ scope 下拉 + 保存按钮。
- 底部一行统计：`usage_count` / `success_rate` / `last_used_at`，并复用 `Memories.tsx` 里
  "从未被用过"的判据（`usage_count=0` 标记）。
- 治理操作：`未确认 → 确认启用`、`归档`、`恢复`（`archived` → `active`）。
- 类型补进 `api.ts`：`export type Skill = { ... }`。
- 复用现成样式类：`settings-section` / `grant-list` / `field` / `hint` / `notice`，
  **不要新造一套 CSS**。

### A3 删除语义（这里有个真问题，必须明确选一个）

`sync_to_store` 目前对"文件删了"的处理是**标 `archived` 但不删行**。所以：

- `DELETE /api/skills/{name}` 的实现 = **删文件 + 标 archived**（与 `sync_to_store` 口径一致），
  不是真删表行。UI 文案写"已移除（保留统计）"，给"恢复"入口。
- 若要真删（连统计一起删），需要显式 `?purge=1`，且必须在 UI 二次确认。
  **建议：第一版不做 `purge`**，`archived` + 可恢复已经够用，真删没有恢复路径。

### A3b `usage_count == 0` 不是"陈旧证据"（Hermes 依据，正确性修正）

`policies.record_outcome` 现在有一段归档判据：

```python
elif row.failures > 0 and row.success_rate < min_rate and int(row.usage_count or 0) >= 3:
    row.status = "archived"
```

`usage_count >= 3` 这个门是对的（没被用过就没有成功率可言）。
但**另有 90 天未用即归档**的分支，它对 `usage_count == 0` 的技能同样生效 ——
Hermes 明确把这两种情况分成不同概念（`curator.apply_automatic_transitions` 注释）：

> `use_count == 0 是"缺乏证据"，不是"陈旧证据"：绝不在技能年轻于 stale_after_days 时归档`

规则落成：**`usage_count == 0` 时，只在技能本身已超过归档期（`created_at` 距今 > 90 天）
才归档；绝不因"从未用过"而提前归档**。锚点取值顺序照 Hermes：
`last_used_at → created_at → now`（防从未活动的技能自我归档）。

理由很实际：一个刚沉淀、还没被触发过的技能被"超期"归档，等于**沉淀白做**。
测试：加一条"新建但从未使用的技能不会被归档"。

### A4 验收

1. 新建技能 → 出现在列表 → 状态 `unreviewed` → 点"确认启用" → `active`
   → 下次同触发词任务 `skills_injected` 事件里出现它。
2. 改正文保存 → `version` +1，且**从 `stale` 恢复为 `active`**（这是 `sync_to_store`
   "只有 version 递增才复活"规则的正向验证，必须有测试）。
3. 删除 → 列表消失、表里 `status='archived'`、统计保留；点"恢复"能回来。
4. 测试：`backend/tests/test_skills_api.py`（新，约 10 条）+ 前端 `npm run lint`/`build`。

---

### A5 写入侧内容守门（Hermes 依据，新增）

**Orbit 现在 `skill_save` 只校验"正文非空"。** 没有任何东西阻止模型把
"这次试了三次都失败了"沉淀成一条像样的技能 —— 而这条会污染后续所有相似任务。

照抄 Hermes `_DO_NOT_CAPTURE_BLOCK` 的五条禁止项，落成 `skills.py` 的一个纯函数
`_refuse_capture(body: str) -> str`（返回拒绝原因，空串放行）：

1. **环境相关故障**（缺二进制、未装包、`command not found`、凭据未配置）
   → 用户可自行修复，不是持久规则。
2. **对工具/功能的负面断言**（"浏览器工具不能用"、"X 工具坏了"）
   → 原文理由：这类内容会**固化成拒绝**，问题修好后仍被引用数月。
3. **会话内已解决的瞬时错误** → 若重试成功，该记的是**重试模式**，不是原始失败。
4. **一次性任务叙述**（"总结今天的行情"）→ 不构成一类可复用工作。
5. **未解决的失败** → 不得把未验证的失败序列写成"可靠工作流"。

实现口径：**关键词 + 结构启发式，不调模型**（harness 拦截、不靠提示词，与 M2 记忆写入门槛同纪律）。
被拦时 **`raise`**（不能返回 `status` —— `execute` 会包成 `{"status":"ok"}` 覆盖，M2 已踩过）。
配套：`tool_results` 里保留"重试模式"的正面引导（允许、但要求写清成功路径）。

测试：`test_skills.py` 加 5 条（每类禁止项 1 条 + 1 条放行正例）。

---

## 阶段 B · MCP prompts → Skill 多源化

### B1 设计：来源标识进 `SkillProvider`

`SkillProvider` 目前全是单目录实现（`self.root = <data_dir>/skills`）。改成显式带来源：

- 技能记录加 `source` 字段：`local`（默认）或 `mcp:<服务>`。
- 文件式与 MCP 式共用一个**内存态目录**：`catalog_with_stats` 合并两路结果；
  `load(name)` 路由到正确的来源；`save()` 只对 local 生效（MCP prompts 只读，不能改）。
- **`skills` 表加 `source` 列**（加列，不是新表）→ 迁移 **`0011_skill_source`**。
  `name` 的唯一约束要改成 `(name, source)` 复合唯一，否则同名的本地技能与外部技能撞车。

### B2 MCP 侧

- `MCPManager.start()` 里在 `list_tools()` 之后补 `list_prompts()`（**同样要放在
  已有的 `asyncio.timeout` + 独立 `inner` exit stack 里**，坏服务仍然只标自己）。
- **先读能力声明再调用**（Hermes 依据）：不要假设服务支持 prompts/resources，
  从握手返回的 `InitializeResult.capabilities` 判定；不支持就跳过，**不是**每次抛错。
  → Orbit 的 `MCPManager.start()` 现在只拿 `listing.tools`，需要额外保留 initialize 结果。
- prompt 的 `name` / `description` / `arguments` 映射成技能的
  `name`（前缀 `mcp__<服务>__` 保持与工具一致的命名纪律）/ `triggers`（用 arguments 描述兜底）/ `body`（模板正文）。
- 远程 prompt 取正文用 `get_prompt()`；**这一步是网络调用，失败只记日志**，
  prompt 仍以"仅描述"占位出现（`body` 为空 → 不进 `catalog`，与文件式"body 为空就跳过"一致）。
- 生命周期：MCP 服务重连后 prompt 目录跟着刷新（见阶段 C 的重连成功回调）。
- 分页：如果 SDK 的 `list_prompts` 有 cursor，**要设翻页上限**（Hermes 用 50 页）
  防 forever-cursor 死循环。

### B3 验收

1. `echo_mcp_server.py` 加一个 prompt 夹具 → 连接后该 prompt 出现在技能列表，`source="mcp:echo"`。
2. 注入择优对两路一视同仁（`related_skills` 不改代码，靠 `catalog_with_stats` 合并）。
3. 服务断开 → 其 prompts 从目录消失（与工具摘除同一条路径）。
4. 测试：`test_mcp.py` 加 2–3 条；`test_skills.py` 加多源合并与 `(name, source)` 唯一性。

---

## 阶段 C · MCP 生命周期加固（心跳 + 重连）

### C1 现状与目标

现在杀掉 MCP 子进程 = 永久失联，只能手工点"重新连接"。
目标：**全程无人工** —— 失联被检测到 → 工具摘除 → 自动重连 → 恢复注册。

### C2 实现（`mcp.py`）

- 每服务一个后台看门狗任务，**用 `client.listen()` 开一条长驻订阅流**当存活信号：
  服务非优雅断开时它立刻抛 `SubscriptionLost`，是**事件驱动**而非轮询。
- 失联动作：`registry.unregister_source(f"mcp:{name}")` + `status[name] = {"state": "reconnecting"}`，
  并**从 `_routes` 移除该服务所有工具**（避免模型调到"已不可用"的工具才报错）。
- 重连：指数退避 `2/4/8/16/32/60s` 封顶；成功 → 重新 `list_tools()` + `list_prompts()`
  + `registry.register(...)` + `status = ready`；失败继续退避。
- 全程 `asyncio.create_task`，**`close()` 必须取消所有看门狗任务**（与 `AgentRunner.close` 同纪律）。
- 沿用已有模式：**每次重连尝试自己一个 `AsyncExitStack`**，半连接不能泄漏子进程。

### C2a ⚠️ `send_ping` 在现代协议上不可用（实测推翻原设计，必须知道）

**原方案写"周期 `send_ping()`"，实测证明这条路是死的。** 记在这里，防止以后有人改回去：

- 本机协商到的协议版本是 **`2026-07-28`**（现代线）。SDK 的 `Client.send_ping()` 带
  `@deprecated("ping is removed as of 2026-07-28; the method only works under mode='legacy'")`。
- 实测：**服务活得好好的，`send_ping()` 依然抛 `MCPError: Method not found`** —— 也就是说它
  **恒失败**。拿它当心跳，宽限期一过就会把所有服务判死、无限重连。
- 同理 `list_tools()` 打在响应缓存上：服务停摆时它照样返回旧清单（实测确认），**也证明不了存活**。
- **正解**：`client.listen(tools_list_changed=True)`。实测杀掉子进程后它立刻抛
  `SubscriptionLost: subscription 'listen-1' ended without the server's graceful close`。
- 老协议（`< 2026-07-28`）不支持 listen，才退回 `send_ping` 轮询 —— 那时它还在，
  宽限期就是给这条轮询路用的。判定函数 `_supports_listen()`。
- 回归闸：`test_modern_wire_does_not_rely_on_ping` 断言"活着但 ping 抛错"，
  谁把心跳改回 ping 谁红。

### C2b 失败宽限期（Hermes 依据，必做）

**单次心跳失败绝不可立刻摘工具。** Hermes `registry.check_fn` 的设计说明写得很实：
一个抖动的探针会**静默摘掉整个 toolset**，表现为子 agent 报"工具不存在"。
规则：结果缓存 30s；**若 60s 内成功过而当前失败 → 判为 flake，沿用 last-good 且不缓存**；
失败**持续超过宽限期**才采信。→ 落成常量 `HEARTBEAT_GRACE_SECONDS=60`、`HEARTBEAT_CACHE_TTL=30`。

**parked 自探测**：被摘除工具的服务器"没别的东西能救活它"，必须自己周期重探
（Hermes 用 300s）。→ 我们重连退避本来就有，但**要保证不会因为"没有流量"而永不重试**。

### C3 顺带修的确定问题

- `public()` 的 `state` 增加 `reconnecting` 一档 → 前端 `mcpLabels` 补文案。
- `execute()` 现在对失联服务返回 `{"status":"error"}`。保持在 `error` 是对的
  （工具已被摘除，正常路径模型看不到它），但补一句可读文案：
  `"服务 X 暂不可用，正在重连"`。
- **`registry.register()` 加跨 source 遮蔽检查**：现在是无条件覆盖，MCP 服务与内置工具
  同名会静默遮蔽。规则照 Hermes：**同 source 重复注册允许**（重连刷新需要），
  **跨 source 遮蔽拒绝并记日志**。
- **注册时前置校验 schema 形状**（`parameters` 必须是 dict），避免运行时 400。

### C4 验收（对应 `07-mcp-hardening.md` 验收 1）

1. 杀掉 MCP 子进程 → 心跳检出 → 工具摘除（`registry.tools` 里 `mcp__` 前缀清空）
   → 子进程重启后**自动**恢复注册，全程无人工。
2. **宽限期生效**：单次心跳失败（模拟一次抖动）**不**摘工具；连续失败超宽限期才摘。
3. `close()` 后无残留 asyncio task（用 `asyncio.all_tasks()` 断言）。
4. 测试：`test_mcp.py` 加"失联重连"与"抖动不摘工具"两条用例（用可控子进程，真实 stdio）。

---

## 阶段 D · MCP resources → RAG（隔离模式）

### D1 实现

- 配置 `mcp.json` 每服务加 `"index_resources": true`（**默认 false**）。
- 连接后 `list_resources()` + `read_resource()`；内容走**异步抓取队列**
  （复用 `digest` 的切块入口，`source_type="mcp_resource"`，`origin=资源 URI`）。
- **隔离原则（硬约束）**：抓取失败只记日志、绝不阻塞主链路；抓取限速；
  超大资源按工具结果同一上限截断，不入库。

### D2 前置检查（先做这个，别直接写代码）

`chunks_for_tool_result` 目前只认 `url→web_page` / `path→local_file` 两类出处。
加 `mcp_resource` 之前必须确认 **`origin` 的来源字段怎么填、`source_type` 白名单是否要放行**，
以及 **`retrieval` 的查询路径是否会把它当成可召回来源**。
预计要动 `app/indexing.py` 的出处判定 + `RetrievalArgs.source_type` 的描述。
→ 这是本阶段最容易偷工减料的地方，**A/B/C 三个阶段做完后再评估是否并入本轮**。

### D2 前置检查结果（2026-10-08 已做，结论：D 单独一轮，不与 A/B/C 同批）

核查 `indexing.py` / `storage.py` / `retrieval.py` / `vectors.py` / `graph/nodes.py`，四条结论：

1. **`source_type` 没有白名单**：`Chunk.source_type` 是 `String(32)`、无 CHECK/枚举；检索与向量
   两路都是通用 `c.source_type = ?` 过滤。→ `mcp_resource` 一旦写入即可召回，**无需迁移、无需改 schema**。
2. **摄入路径不能复用 `chunks_for_tool_result`**：它只在 `Nodes.index()`（工具执行 digest 时）被调，
   而 MCP resources 是**连接时由 `MCPManager` 拉取**、不是工具结果。→ 需要一条**新的摄入路径**
   （MCPManager → `chunk_text` 切块 → `indexer.write`），这意味着要把 `Indexer` 穿进 `MCPManager`。
3. **任务上下文缺口（真正的设计问题）**：chunk 带 `task_id/source_id/conversation_id`，MCP resources
   拉取时**还没有任何任务**。→ 只能以空 task_id 写入、只靠 `scope="global"` 召回（语义上合理，但要显式拍板）；
   且 `host_of_origin` 对 `note://welcome` 这类 URI 无 host 可抽，需兜底。
4. **还需要**：每服务 `index_resources` 开关（默认 false）、`list_resources` 翻页上限、`read_resource`
   限速、抓取失败绝不阻塞主链路。

**决定：D 推迟为独立一轮。** 不是偷工——是它自成一个有独立设计问题的批（新摄入路径 + Indexer
穿进 MCP + 任务上下文语义），混进本轮只会变成"半成品"，正是方案原判的坑。A/B/C 已完整交付，
下一轮从上面的 4 条结论直接开工。

### D4 落地结果（2026-10-08 已实现，端到端可召回）

按 D2 四条结论实现，无需迁移：

- **`indexing.py`** 新增 `chunks_for_resource(uri, body, server)`：`source_type="mcp_resource"`、
  `origin=uri`、`host=mcp:<服务>`、`task_id/source_id/conversation_id` 全空（无任务上下文），
  正文截断 `RESOURCE_CHAR_LIMIT=16000`。复用 `chunk_text` 切块。
- **`mcp.py`**：`MCPManager(settings, indexer=...)` 加可选 indexer 注入；`save_servers` 保留
  `index_resources` 字段（默认 false）；`_connect_locked` 成功后对 `index_resources` 服务
  `asyncio.create_task(self._index_resources(...))`（后台隔离，连接本身不阻塞）；`_index_resources`
  顺序抓取 + `RESOURCE_FETCH_GAP=0.1` 限速 + `RESOURCE_READ_TIMEOUT=10` + `RESOURCE_MAX_PAGES=50`
  翻页上限 + 坏 URI 只跳过该条；`_stop_locked` 取消全部资源任务。
- **`main.py`** 装配层注入 `build_indexer(settings, store)`。
- **`tools.py`** `RetrievalArgs.source_type` 描述补 `mcp_resource`。
- 检索路径无需改动：`scope="global"` 时 `narrowed=""` 不过滤 conversation_id，空 task 的资源
  chunk 自然可召回（`scope="task"` 会按具体会话收窄，资源不进——语义正确）。

**验收（D3）全过**：`test_resources_indexed_when_enabled`（资源进 chunks、origin=note://welcome、
global scope 可召回）、`test_resources_not_indexed_by_default`（默认不抓取）、
`test_resource_index_failure_does_not_block_connect`（BoomIndexer 抛异常连接仍 ready）。


### D3 验收

配置 `index_resources: true` 的服务，其 resources 出现在 `chunks` 表且 `origin` 可追溯；
抓取失败不影响主链路（用坏 URI 验证）。

---

## 涉及文件

| 阶段 | 文件 |
| --- | --- |
| A | `backend/app/main.py`（+5 端点）、`backend/tests/test_skills_api.py`（新）、`frontend/src/Skills.tsx`（新）、`frontend/src/App.tsx`（导航）、`frontend/src/api.ts`（`Skill` 类型） |
| B | `backend/app/policies.py`（SkillProvider 多源）、`backend/app/mcp.py`（`list_prompts`）、`backend/app/storage.py`（`Skill.source`）、`backend/migrations/versions/0011_skill_source.py`（新）、`backend/tests/test_mcp.py`、`backend/tests/fixtures/echo_mcp_server.py` |
| C | `backend/app/mcp.py`（`listen()` 存活检测 / 重连）、`backend/app/tools.py`（`register` 阴影拒绝 + schema 校验）、`frontend/src/Settings.tsx`（`reconnecting` 文案 + 删误导文案）、`frontend/src/api.ts`（state 加 `reconnecting`）、`backend/tests/test_mcp.py`、`backend/tests/fixtures/stale_mcp_server.py`（新） |
| D | `backend/app/indexing.py`（`chunks_for_resource`）、`backend/app/mcp.py`（`_index_resources` + `indexer` 注入）、`backend/app/main.py`（装配）、`backend/app/tools.py`（`RetrievalArgs` 描述）、`backend/tests/test_mcp.py` |

## 迁移编号

`0011_skill_source`（加 `skills.source` 列 + `(name, source)` 复合唯一）。
本方案**不抢 `0008`**（审计时间线预定），编号链无缺口。

## 执行顺序与理由

1. **C（心跳 + 重连）先做** —— 它是 B 的前提：prompts 目录要在重连后刷新，
   没有重连机制的话 B 的"断开→消失→恢复"验收跑不了。
2. **A（操作面）** —— 独立可交付，做完立刻能手工验证 B 的结果（在 UI 里看到 `mcp:` 来源的技能）。
3. **B（prompts 多源）** —— 依赖 C 的重连回调与 A 的展示面。
4. **D（resources）** —— 先做 D2 前置检查再决定是否并入；不与 A/B/C 同批提交。

## 禁区（沿用原方案）

- 不接采样 / elicitation（安全未设计）；不做 MCP 服务端模式。
- 不给 MCP 工具加站点级免确认（不在 `GRANTABLE_TOOLS` 内）。
- 不开放全自动沉淀；技能里不写密码；不用技能替代工具。
- **不做 `DELETE ?purge=1`**（真删不可恢复，统计价值高于清理价值）。

## 验证基线（每阶段结束后必跑）

```
cd backend && uv run pytest -q                      # 当前 218 passed
uv run ruff check app tests ../scripts && uv run mypy app
cd frontend && npm run lint && npm run build
cd backend && uv run python ../scripts/ui_smoke.py  # 当前 13/13
uv run python ../scripts/eval_run.py                # 当前 14/14，需先起 uvicorn 6185 并重启后端
```

`frontend/src/App.tsx:GRANTABLE_ACTIONS` 与后端 `GRANTABLE_TOOLS` 必须保持同步
（本方案不新增可记住动作，只是别在改导航时碰坏它）。
