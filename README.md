# Orbit · 本地优先的个人 Agent

Python + FastAPI + LangGraph 后端，React 工作台。助手可读取授权目录里的资料，通过连接器查询 GitHub、操作 Notion 网页，并保存跨对话偏好。

当前状态：第一版代码可本机启动。已通过本地工作流和浏览器测试页验证；真实模型、GitHub 私有仓库和 Notion 登录账号需要使用者配置，真实 Notion 端到端尚未验收。

## 启动

要求：Windows、Python 3.12、uv、Node.js 20.19+ 或 22.12+。

第一次安装：

```powershell
cd E:\bbbooottt\backend
uv sync --python 3.12
uv run playwright install chromium

cd ..\frontend
npm ci
npm run build
```

启动已构建的工作台：

```powershell
cd E:\bbbooottt
.\scripts\start.ps1
```

打开 <http://127.0.0.1:6185>，API 文档在 <http://127.0.0.1:6185/docs>。启动脚本会保持在当前终端，按 Ctrl+C 停止。Windows 下运行一个 worker，不启用 Uvicorn reload，保证 Playwright 的子进程事件循环可用。

开发前端时，在另一终端进入 `frontend` 执行 `npm run dev`，访问 <http://127.0.0.1:5173>，请求通过 Vite 代理到后端。

## 首次使用

1. 打开“连接与设置”，填写支持工具调用的模型名称、OpenAI 兼容 API 地址和 Key。Key 通过系统 keyring 保存，不回显到前端。
2. 将 TXT、Markdown、文本型 PDF 放入 `data/workspace`。可以把 `examples/project-notes.md` 复制进去作为演示输入。
3. 如需更换授权目录，复制 `backend/.env.example` 为 `backend/.env`，设置 `ORBIT_WORKSPACE` 后重启。也可在此文件配置模型等字段。
4. GitHub 填写 `owner/repo`；公开仓库可不填 Token，私有仓库需要适当权限的凭证。
5. Notion 填写自己有编辑权限的具体页面 URL，保存后点击“打开浏览器登录”，在专用浏览器中登录。
6. 回到工作台发送任务，在右侧查看执行过程、工具结果和下载报告。需要接管时处理浏览器，点击“确认并继续”。

推荐第一个任务：

> 阅读 project-notes.md，生成一份中文 Markdown 摘要，保存到 reports/project-summary.md。

长期记忆演示：

> 记住：我的报告格式是中文 Markdown。

确认保存后新建对话，要求生成报告。新对话注入已保存的偏好，不会自动载入其他对话原文。历史工作可通过 `history_search_tasks` 和 `history_get_task` 检索。记忆管理页面支持直接添加、更新和删除。

## 已实现模块

| 模块 | 实现 |
|---|---|
| Agent | LangGraph think → gate → execute，结构化工具调用，最大动作数，用户中断与继续 |
| 任务 | SQLite 持久化、单槽队列、SSE 事件回放、取消、重启后核对恢复 |
| 本地 | 文本/PDF 读取、文件列举、关键词搜索、Markdown 报告、产物下载 |
| 记忆 | 当前会话历史与最近任务摘要、长期键值偏好、历史任务检索 |
| GitHub | 指定仓库 Issue 搜索与详情、来源链接、有限读重试 |
| 浏览器 | 独立 Profile、DOM 观察和元素引用、点击/输入/按键/滚动、截图 |
| Notion | 已有页面读取、摘要追加、标记核对、刷新验证；真实站点待登录联调 |
| 工作台 | 会话列表、任务时间线、接管、模型/平台设置、记忆管理 |

模型完成一轮执行时 UI 显示“已结束”，不代表每项外部操作都成功；请查看具体工具结果。工具错误会返回模型用于调整或说明原因。未配置模型时任务明确失败，不返回伪造的 AI 内容。

## 实现取舍

- 采用固定单用户身份 `local`，API 绑定本机并使用本地会话 Cookie。当前不面向公网部署或多人共享。
- 工具名称使用下划线以兼容模型 function calling；计划中的点分名称是概念命名。
- 外部写入、浏览器交互、记忆增删在第一版逐项确认；本地新报告在授权目录内直接创建。
- 长期记忆首版注入最近 12 条偏好，历史按关键词检索；按项目隔离、语义召回和向量 RAG 尚未实现。
- 当前会话读取最近消息并限制字符数，同时保存最近任务摘要；尚未实现模型驱动的完整历史压缩。
- 浏览器通常优先使用 DOM；并未实现通用桌面 GUI、任意 Shell 或纯截图坐标 Agent。
- Notion DOM 结构可能变化，当前适配 `.notion-page-content` 下的可编辑内容。登录、验证码、只读页面或结构变化可能需要人工接管。
- Notion 已开始写入但结果不明时不会直接重复提交。通用点击等动作不保证 exactly-once，恢复后需要重新观察。
- 取消只停止后续执行，不能撤销已完成的外部写入。
- 凭证在 keyring/本地环境变量，非敏感配置在 `data/settings.json`；初版尚未单独建立 connector_accounts 数据表。
- 后端、Profile 和文件处理在本机运行；使用云模型时，被选入上下文的资料会发送给所配置的模型。

## 最小验证

后端：

```powershell
cd backend
uv run pytest -q
uv run ruff check .
uv run mypy app
```

前端：

```powershell
cd frontend
npm run build
npm run lint
```

启动后检查工作台：

```powershell
cd backend
uv run python ..\scripts\ui_smoke.py
```

后端测试使用脚本化模型、Mock GitHub 和本地浏览器测试页，不需要真实账号，不证明真实模型回答质量或第三方页面长期稳定性。测试覆盖本地报告闭环、暂停重启恢复、Notion 类编辑页回读、记忆持久化、事件回放和会话隔离。

## 目录

```text
backend/app/       API、Agent、工具、连接器、浏览器、上下文、存储
backend/migrations/  Alembic 数据迁移
backend/tests/     主流程最小测试
frontend/src/      React 工作台
scripts/           本地启动与页面冒烟
examples/          可复制到工作目录的演示资料
data/              运行数据、Profile、文件及截图，不入 Git
MVP_PLAN.md        原始计划与实施状态
```

下一步是填写模型与平台配置，使用真实笔记完成“本地文件 → GitHub → Notion → 本地报告”的演示，并据实记录成功率和耗时。
