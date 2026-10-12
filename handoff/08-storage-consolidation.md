# 方向 8 · 存储收敛：配置入 DB 与表结构补全

**先读 `handoff/00-CONTEXT.md`。依赖：方向 1–5 的表都已建（tool_results/chunks/skills/grants/mcp_servers），本方向做收口。**

## 目标

解决"同一类东西散落四处"的问题：**真值只在 keyring 与 DB**，JSON 文件只留启动必需。

## 现状盘点

| 现在的位置 | 内容 | 目标 |
|---|---|---|
| `data/settings.json` | 模型/网页/granted_actions | 迁入 DB `settings` 表（键值或宽表），保留导出能力 |
| `data/vault.json` | 站点账号名与更新时间 | 并入 `skills/credentials` 迁移 |
| `data/mcp.json` | MCP 服务声明 | 迁入 DB `mcp_servers` 表 |
| `backend/.env` | 模型地址等启动必需 | **保留**（真正的启动必需项） |
| keyring | 所有密钥真值 | **保留** |
| `data/*.sqlite`、Profile | 业务数据与登录态 | **保留**（备份已覆盖） |

## 表结构补全（一次性迁移 0003+，与方向 1/2 协调编号）

| 表 | 补什么 |
|---|---|
| `messages` | `tool_call_id`（关联 tool_calls）、`kind`（user/assistant/tool/system）、`tokens`（估算） |
| `memories` | `scope`（global/project/site）、`source_message_id`、`active`（软删除） |
| `artifacts` | `checksum`、`bytes`、`expires_at` |
| 新表 | `settings(key, value, updated_at)`、`grants(host, tool, created_at)`、`mcp_servers(...)`、`tool_results`、`chunks`、`skills`（若前面阶段已建则跳过） |

## 迁移原则

1. **只加不删**：旧 JSON 文件保留为只读备份，代码不再读写；下一个大版本再清理
2. **首次启动自动迁移**：lifespan 里检测旧文件存在 → 导入 DB → 重命名文件为 `*.migrated`
3. 迁移要幂等：重复启动不重复导入
4. `ConnectorConfig` 的读写路径改为 DB，对外 API 响应**保持不变**（前端零改动）

## 附带清理（本方向一并做）

- `messages` 写入路径补 `tool_call_id`（工具结果消息关联 operation_id）
- `task_events` 加保留策略（默认全留，提供 `days` 配置）
- `granted_actions` 从 settings.json 迁到 `grants` 表后，`app/policies.py` 与 `app/config.py` 的读写改 DB

## 涉及文件

`backend/app/storage.py`（新模型）、`backend/app/config.py`、`backend/app/connectors.py`、`backend/app/main.py`、`backend/migrations/versions/000X_*.py`、`backend/tests/test_storage_migration.py`（新）

## 验收

1. 全新环境：启动后不产生任何 *.json 配置文件，功能等价
2. 旧环境升级：首次启动自动把 settings.json/vault.json/mcp.json 导入 DB 并重命名，功能等价、配置不丢
3. keyring 值原样可用（模型连接检查通过、vault 页面账号可见）
4. 全量测试（含新增迁移测试）与评估集不回归

## 禁区

不动 keyring 的 service/key 命名（已是既定契约）；不删除旧 JSON（只改名）；不改前端 API 形状（内部实现换，接口不变）。
