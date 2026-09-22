# Orbit 修复与验收记录

日期：2026-09-20。对应 [原始诊断](DIAGNOSIS_AND_REPAIR_PLAN.md)。

## 交付状态

- 工作台保持纯聊天；不挂载任务面板，也不把普通问答包装成任务。
- 聊天正文使用 react-markdown + remark-gfm；标题、段落、列表、表格、代码块、引用和链接均直接渲染。表格、长代码可横向滚动。
- 模型通过 astream 输出正文增量；停止保留部分回复，失败可重试，补充和操作确认在聊天中继续。
- 消息区独立滚动，输入区和停止按钮保持可见；上翻历史时停止自动跟随。
- SSE 断线会显示提示并带游标恢复；正常结束清理心跳，避免误重连。快速切页的动画中断不会产生未处理异常。
- “对话摘要”支持搜索、打开、生成、重生成和失败重试，摘要覆盖多轮记录并更新标题。
- 模型、通用网页、GitHub、Notion 独立保存。模型状态区分未配置、待验证、检查成功与失败；密钥不回显，留空保持。
- 浏览器显式使用可见模式，返回实际目标标题和网址；通用网页与 Notion 分开，关闭后可重新启动。不会静默改成无头模式。

## 真实运行证据

| 验证项 | 结果 |
| --- | --- |
| 数据库迁移 | 当前 app.sqlite 已升到 0002；升级时原有 5 段对话、12 条消息保持。新增的是本次验收会话，原记录未删除。 |
| 旧 Bilibili 设置 | 原 notion_url 已迁至 browser_url=https://www.bilibili.com/；Notion 专用字段为空。 |
| DeepSeek 连接 | 设置页发起真实最小请求，服务有效响应，检查成功。 |
| 真实 Markdown 回复 | 聊天页直接展示标题、表格、粗体列表、Python 代码、引用和外部链接。检查截图后修复了继承空白导致的列表间距及表格样式。 |
| 正文流式 | 一条 289 字回复产生 13 次 message_delta，时间跨度约 0.78 秒，结束后只保存 1 条助手回复。 |
| 多轮摘要 | 同一会话第 2 轮补充界面需求后，摘要同时反映 Markdown 渲染、流式输出及新界面要求，消息数为 4。 |
| 真实停止 | 在真实模型长回复中点击停止，后端状态 CANCELLED，保留 189 字部分回复；之后摘要为 ready、覆盖 10 条消息。 |
| Bilibili 可见窗口 | 设置页打开后获得 Bilibili 实际标题；Win32 检查窗口 1296×1034，未最小化。 |
| 关闭实际窗口再重开 | 对本次打开的 Orbit 专用窗口发送正常关闭请求，再从设置页打开；浏览器 PID 从 11812 变为 19604，仍为 Bilibili 页面和可见窗口。 |
| 更换网址 | 独立临时 Profile 的真实 BrowserController 验证 Bilibili → 关闭重开 → GitHub，页面正文非空。 |
| 服务重启恢复 | 页面不刷新，重启后端后点击摘要刷新成功；旧 Cookie 由 API 客户端刷新后重试。 |

## 完整界面交互

可复运行：[scripts/ui_smoke.py](scripts/ui_smoke.py)。使用临时 SQLite、工作目录、内存凭证存储和随机本地端口。FastAPI、LangGraph、SSE、设置校验、摘要服务、React 均真实运行；模型与外部平台响应使用受控替身。

| 交互组 | 已验证入口与结果 |
| --- | --- |
| 导航与欢迎页 | 品牌、新对话、三个导航、历史列表、重复选择同一对话、管理连接、工作目录、顶部模型、三个建议、GitHub/Notion 页脚入口、返回对话。 |
| 设置 | 所有输入字段；四区保存；空值/URL/域名/仓库格式反馈；模型检查成功、失败、恢复；凭证保存后清空输入并保持存储；页面刷新回读。 |
| 网页按钮 | 通用网页和 Notion 打开、空网址禁用、打开前保存改动、目标参数正确、打开失败后重试清旧错误。 |
| 对话摘要 | 标题/摘要搜索、无匹配、清空、刷新、点击/Enter/Space 打开、打开当前已选对话、生成/重生成、生成失败与请求失败重试、空对话按钮禁用。 |
| 普通聊天 | 按钮与 Ctrl+Enter 发送、空白消息禁用、真实 SSE 增量拼接、Markdown 语义元素与链接、切换历史再返回不串流。 |
| 确认与恢复 | 补充回答继续、允许、拒绝、真实独立过程说明展开、发送失败保留草稿、关闭错误提示、回复失败重试。 |
| 长回复 | 至少 25 段持续增量；回看位置保持；停止按钮在视口内；点击后后端 CANCELLED；部分正文保留；重新回复与停止后新消息成功。 |
| 等待期间停止 | 等待补充时点击停止，状态为 CANCELLED，摘要最终 ready 且摘要消息数等于实际消息数。 |
| 响应式 | 320 / 768 / 1024 / 1440 宽度的聊天、设置、摘要页面无横向溢出；移动导航展开/关闭和发送。 |
| SSE 断线续传 | 结束实际 SSE HTTP 响应（不发送 settled），页面提示断线并从事件 148 继续，7 段增量拼成完整 208 字正文，无重复增量，只有一条最终回复。 |
| 旧会话恢复 | 使用过期 Cookie 点击刷新，实际网络结果为 conversations 401 → session 200 → conversations 200；新 Cookie 生效且无可见错误。 |

最终 12 组通过，页面脚本错误为 0。以 [最终 report.json](test-results/acceptance-2026-09-20/completed/report.json) 为准；同目录有等待动画完成后的三页 × 四种宽度截图，以及断线前后截图。父目录保留早期运行记录，包括已修复问题的失败现场，不能把早期记录当作当前结果。

断线测试采用服务端受控结束真实 HTTP 流。曾使用 Chromium 离线模拟，但它没有可靠切断已建立的 localhost 连接，已替换该故障注入方法。

## 代码检查

- 后端 pytest：23 passed；Ruff、Mypy、Bandit 通过。
- 前端 ESLint、TypeScript 生产构建通过。
- npm audit（官方 registry）：0 vulnerabilities。本机镜像不支持 audit 接口，因此仅为该检查使用官方 registry。
- 真实可见浏览器脚本：[scripts/check_browser_visible.py](scripts/check_browser_visible.py)。
- 服务已重启并运行在 http://127.0.0.1:6185/；最新前端入口加载 index-KTwSt3nW.js，已在真实页面确认聊天、摘要和连接设置入口。

```powershell
cd E:\bbbooottt\backend
uv run pytest -q
uv run ruff check app tests
uv run mypy app
uv run python ..\scripts\ui_smoke.py
uv run python ..\scripts\check_browser_visible.py

cd ..\frontend
npm run lint
npm run build
```

## 范围与保留事项

- 未登录真实 Notion/GitHub 私有账号，没有发送第三方消息、提交写入或自动处理验证码。真实账号权限及第三方 DOM 变化须在使用相应账号时确认；界面入口和受控失败恢复已验证。
- 过程说明使用模型对工具操作的简短说明，独立于最终回答；不展示原始 reasoning_content，也不制造过程文本。
- 项目运行数据、密钥与用户默认浏览器 Profile 保留；测试未输出密钥。
- 自动审批拒绝清理首轮临时迁移审查副本 `C:\Users\Administrator\AppData\Local\Temp\orbit-migration-audit-hegkk31h`（仅返回 blocked by policy），该副本留在临时目录，未影响项目运行数据。
