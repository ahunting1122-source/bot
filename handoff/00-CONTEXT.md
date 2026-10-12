# Orbit · 分方向加强计划 · 主上下文（必读）

**用途**：你是一个新开的对话，负责加强 Orbit 项目的某一个方向。**先读完本文件，再读该方向的 `0X-*.md`**，然后与用户讨论并细化该方向的实施计划。用户习惯：先计划、后动手；细节由你决定，重大取舍才问用户。

## 项目定位

Orbit = **网页操作特化的本地 Agent**（Python + FastAPI + LangGraph 后端，React 工作台）。核心能力：在真实可见浏览器里操作任意站点；本地文件与跨对话记忆是辅助。目录 `E:\bbbooottt`。

## 硬约束（所有方向必须遵守）

1. **v1 权限 = 完全访问**：事前零确认。人工介入只剩两种——模型主动 `ask_user`（真登录墙/验证码/缺输入）、用户主动停止。**不要往设计里加确认弹窗**。
2. **密码不经过模型**：站点账号/MCP 密钥存 keyring，经专用工具或解析填充，明文不进上下文/日志/checkpoint。
3. **本地优先单用户**：SQLite 单文件、固定单用户 `local`、仅 loopback；不引入 PostgreSQL/服务化。
4. **不引入**：预置 ReAct agent、多 Agent 框架（AutoGen/CrewAI）、LangServe、消息总线、依赖注入框架。
5. **无域名白名单**：任意 http(s) 站点可访问，只校验地址格式。
6. 语言：与用户交流用**简体中文**；先给计划讨论，用户说"开始/继续"才写代码。

## 当前状态（2026-09-23 基线）

- 后端测试 **61 passed**；Ruff / Mypy 通过；前端 lint+build 通过；界面检查 **13 组**通过；固定任务集 **10 用例 10/10**（热态）
- 真实站点实测过：B 站、IT之家、V2EX（网络不通但未被拒）、ai.zhaoshujun.vip（SPA + 自动登录成功）
- 服务跑在 `http://127.0.0.1:6185`；模型 `deepseek-chat`（已配置，key 在 keyring）

## 关键文件

| 文件 | 内容 |
|---|---|
| `backend/app/agent.py` | AgentRunner（队列/worker）+ 现有 think/gate/execute 节点（待拆包） |
| `backend/app/tools.py` | `Tool`（支持 Pydantic 或 JSON Schema）、`ToolRegistry`（唯一分发点，幂等） |
| `backend/app/browser.py` | 可见浏览器、observe/click/fill/press/scroll、`vault_login`、`settle()`、`verify_entry()` |
| `backend/app/mcp.py` | MCPManager：stdio/HTTP 客户端、工具注册、keyring 密钥、状态 |
| `backend/app/vault.py` | 密钥/站点账号（keyring）、登录状态汇总 |
| `backend/app/storage.py` | SQLAlchemy 模型 7 表 + `Store`（WAL） |
| `backend/app/context.py` | SYSTEM 提示 + `build_context`（固定预算：最近 30 条 + 24000 字符 + 12 条记忆） |
| `backend/app/main.py` | 全部 API 路由（含 /api/mcp、/api/secrets、/api/tools） |
| `scripts/agent_probe.py` | 单任务探针：一句话驱动真实任务，打印工具轨迹，自动批准 |
| `scripts/eval_run.py` + `eval_cases.json` | 固定任务集 10 用例跑批 + 报告（写 `test-results/eval-<日期>/`） |
| `scripts/ui_smoke.py` | 13 组界面交互检查（隔离后端） |
| `data/skills/web_search/SKILL.md` | 首个技能（已落盘） |
| 设计文档 | `ARCHITECTURE.md`（唯一入口）、`AGENT_ORCHESTRATION.md`（链与协议）、`HARNESS_DESIGN.md`（三链路）、`AGENT_DESIGN.md`（现状问题） |

## 验证命令（每个方向完成后必须全绿）

```powershell
cd backend
uv run pytest -q                                  # 61 passed 起步，只增不减
uv run ruff check app tests ../scripts
uv run mypy app
cd ..\frontend ; npm run lint ; npm run build
uv run python ..\scripts\ui_smoke.py              # 13 组
# 服务在跑时：uv run python ..\scripts\eval_run.py  # 10/10
```

## 本环境的坑（都踩过）

- Bash **没有** ls/tail/cat/grep/head/timeout/sleep；遍历/统计用 `uv run python` 或 PowerShell
- `npx` 会被沙箱拦（wsl.exe）；npm 命令改用 **PowerShell** 跑
- Windows 版 Python 读不到 `/tmp`（bash 与 Python 路径不互通）；要处理文件就写到项目目录
- 带括号/反引号的 Python 单行代码会被 bash 拆坏 → 写临时 `.py` 文件再跑，用完删
- git **引用文件可能丢失**（HEAD 变 unborn）：查 `.git/logs/HEAD` 取哈希，用 Python 直写 `.git/refs/heads/...` 修复
- **mcp 2.2.0**：字段是 snake_case（`input_schema`），访问不存在的属性会**挂死进程**——读 SDK 对象一律走 `model_dump()`
- 后端代码改动后要**重启 uvicorn** 才生效（无 reload）

## 全局决策记录（11 条，勿翻案）

① RAG 先服务网页内容+工具结果 ② 引用回填=硬约束（检索类回答无来源不算完成）③ 检索范围默认按任务收窄 ④ MCP resources 接入但隔离（异步、失败只记日志）⑤ 向量要上（sqlite-vec）⑥ 拆 `app/graph/` 包 ⑦ 建 `tool_results` 表（幂等 checksum）⑧ 收尾给"已完成/未完成+下一步建议" ⑨ v1 完全访问权限 ⑩ Embedding 走本地（fastembed + BAAI/bge-small-zh-v1.5）⑪ 首个 Skill=web_search（已落盘）

## 工作方式

1. 读本文件 + 你的方向文件 → 给出细化实施计划（分小步，每步有验收）→ 与用户确认 → 实现 → 跑验证命令 → 汇报。
2. 新增表走 alembic 迁移；测试只增不减；文档（ARCHITECTURE.md 对应小节）随手更新。
3. 与其他方向有交叠时：引用对方方向的接口占位（`app/policies.py` 的 Protocol），不要越界实现。
