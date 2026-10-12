# 方向 7 · MCP 加固：重连、心跳、resources 隔离接入

**先读 `handoff/00-CONTEXT.md`。**

## 现状（`backend/app/mcp.py`，mcp 2.2.0 客户端）

已实现：stdio + streamable HTTP 两种传输、启动时 `list_tools` 注册工具（`mcp__<服务>__<工具>`）、默认逐个确认（v1 全放权限下配置 `auto_approve`）、keyring 密钥（`"keyring:名称"`）、每服务独立 exit stack + 20 秒连接超时、坏服务只标记自己、`GET/PUT /api/mcp` + `POST /api/mcp/reload`、设置页「MCP 服务」区。

**缺**：断线自动重连、心跳、resources、prompts、采样回调（默认关，安全上单独设计）、服务端模式（远期）。

## 本方向三件事

### 1. 生命周期加固

- **心跳**：每 N 秒 `send_ping()`，连续失败判定服务失联 → 标记 status 并摘除其工具（`registry.unregister_source`）
- **指数退避重连**：失联后 2s/4s/8s…（上限 60s）自动重连，成功后恢复工具注册；重连期间工具对模型返回明确的 `needs_user`/error（"服务 X 暂不可用，正在重连"）
- 全部异步后台任务，不阻塞主循环；`close()` 时取消

### 2. resources 接入 RAG（隔离模式，已拍板）

- 服务连接后 `list_resources()` + `read_resource()`，内容走**异步抓取队列**进方向 3 的切块索引（`source_type="mcp_resource"`，origin=资源 URI）
- **隔离原则**：抓取失败只记日志，绝不阻塞主链路；资源抓取限速；不索引超大资源（上限与工具结果一致）
- 配置里加 `"index_resources": true`（默认 false，按服务显式开启）

### 3. prompts 作为技能来源

- `list_prompts()` 拉到的提示模板登记为**外部技能**（来源标记 `mcp:<服务>`），与本地 SKILL.md 走同一个加载器（`app/policies.py` 的 `SkillProvider` 有两路实现）
- 治理同本地技能（注入择优、失败降级）

## 已知坑（前次实测，务必沿用）

- **mcp 2.2.0**：`FastMCP`→`MCPServer`；字段 snake_case（`input_schema`/`is_error`/`structured_content`），**访问不存在的属性会挂死进程**——读 SDK 对象一律 `model_dump()` 转字典
- 一个启动即退出的服务会让握手无限等——已有 `asyncio.timeout` + 独立 exit stack，**重连逻辑必须沿用同一模式**
- 测试用真实 stdio 子进程（`tests/fixtures/echo_mcp_server.py`，用 `mcp.server.mcpserver.MCPServer`）

## 涉及文件

`backend/app/mcp.py`（心跳/重连/resources/prompts）、`backend/app/retrieval.py`（resources 入索引）、`backend/tests/test_mcp.py`（新增用例：失联重连、resources 入索引、prompts 注册）

## 验收

1. 杀掉一个 MCP 子进程 → 心跳检测到 → 工具摘除 → 子进程重启后自动恢复注册（全程无人工）
2. 配置 `index_resources: true` 的服务，其 resources 出现在 chunks 表且 origin 可追溯
3. 全量测试与评估集不回归

## 禁区

不接采样/elicitation（安全未设计）；不做服务端模式（远期）；不给 MCP 工具加站点级免确认（不在 GRANTABLE 内）。
