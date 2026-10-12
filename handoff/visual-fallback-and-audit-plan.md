# 下一个任务计划：视觉兜底（最小闭环）+ 审计时间线

日期：2026-09-30。承接 Skill 层（阶段 5）收口后。这是**讨论稿**，落定后按 GOAL-MODE 方式逐项推进、每步一验。

---

## 0. 前提（已核实，2026-09-30）

**DeepSeek 有多模态，但模型名不同：**

- 当前配置 `ORBIT_MODEL=deepseek-chat` 是**纯文本**，看不了图。
- 支持视觉的是 **`deepseek-flash`**（DeepSeek-V4.1-Flash），它是**通用多模态模型**——同时支持文本对话、Tool Calls、图像理解，1M 上下文，价格比 deepseek-chat 更低（输入 1元/百万、输出 4元/百万，空闲时段）。
- `deepseek-v4-pro` 更强但**不支持图像理解**。

**结论：要让模型真看图，只需把 `ORBIT_MODEL` 从 `deepseek-chat` 改成 `deepseek-flash`**，一个模型承接整个 Agent（文本+工具+视觉），无需双模型，且**反而降本**。

模型接入用 `langchain_openai.ChatOpenAI`，OpenAI 兼容格式，`HumanMessage` 传 list-of-dict（`image_url` 块）即可，base64 内联（截图本地文件直接读 base64）。图片仅允许出现在 `user` 消息中（`system`/`assistant` 带图会 400）。

**方案定为：路线 2（真·多模态视觉兜底）**，用户已拍板接受。

---

## 1. 视觉兜底：最小闭环拆解

### 现状（代码级依据）

- `app/browser.py::locate()`：ref 过期 / URL 变了 / `locator.count() != 1` 时抛 `ValueError("元素已变化，请重新观察")`。
- `app/browser.py::OBSERVE_JS`：只收集 `ref/tag/role/name`，**无坐标**。
- `app/browser.py::observe()`：每次已截图存 artifact（`kind="screenshot"`），返回 `screenshot_id`，但**图没进模型上下文**。
- `browser_click` / `browser_fill` 走 `locate(args)` 按 ref 定位，**无坐标点击**。

### 改动（按依赖排序）

| 步 | 改动 | 文件 | 验收 |
|---|---|---|---|
| V0 | 模型切 `deepseek-flash`（`.env` 的 `ORBIT_MODEL` + `data/settings.json` 的 `model`），确认 `ChatOpenAI` 能传图 | `backend/.env` + `data/settings.json` | ✅ 已完成：文本能力不退化 + 真实截图看图成功 |
| V1 | `OBSERVE_JS` 给每个元素加 `x/y/width/height`（getBoundingClientRect），`observe()` 返回带坐标的 elements | `app/browser.py` | ✅ 已完成：观察结果含坐标；现有测试不回归 |
| V2 | 新增 `browser_click_at(x, y)` 工具（坐标点击，需确认），`execute` 里 `page.mouse.click(x, y)` | `app/browser.py` | ✅ 已完成：工具可注册；坐标点击生效 |
| V3 | `locate()` 失败时**降级**：先用 name/text 在最新 refs 里重新匹配找回新 ref → 返回新 locator；仍失败才进入视觉兜底 | `app/browser.py` | ✅ 已完成：元素轻微变动时点击不再断 |
| V4 | **视觉兜底**：`locate()` 彻底失败时，把截图 base64 塞进 `HumanMessage`（image_url 块）让模型看图报坐标 → 坐标点击；模型定位不到则存 artifact 给人看 | `app/browser.py` + `app/graph/nodes.py` | ✅ 已完成：模型能看图报坐标并成功点击 |

### 关键设计决策

1. **模型切到 `deepseek-flash`**：`ORBIT_MODEL=deepseek-flash`。一个模型承接文本+工具+视觉，价格更低、上下文 1M。这是视觉兜底的前提。
2. **坐标点击仍需确认**（`confirmation=True`）——它绕过了 ref 校验，风险略高，必须和 `browser_click` 一样要用户确认。
3. **视觉兜底的真正形态（多模态下）**：`locate()` 失败时，把**当前截图 base64** 塞进一条 `HumanMessage`（image_url 块），让模型看截图 + 文本描述「元素已变化，请看图指出要点的元素坐标」，模型返回 `(x, y)` → 坐标点击。这才是「截图给模型看 + 模型报坐标」的字面完整实现。
4. **三层兜底**（从轻到重，前面的失败才触发后面的）：
   - 第一层：ref 过期但 name/text 能重新匹配 → 自动找回新 ref（几何，不需看图）
   - 第二层：重新匹配失败 → 喂截图给模型，模型报坐标 → `browser_click_at`
   - 第三层：连模型都定位不到 → 截图存 artifact，配合审计时间线给人看（人接管）
5. **截图 token 成本**：每张图缩放后最多 1024 token。视觉兜底只在「DOM 失败」时触发（低频），不是每次观察都喂图，成本可控。

---

## 2. 审计时间线

### 现状（代码级依据）

- `tool_results` 表已记：`operation_id/task_id/name/status/host/text/bytes/tokens/truncated/hash/created_at`。
- `task_events` 已记事件序列（`tool_started/tool_finished/...`）。
- **缺**：①「前后截图」——ROADMAP 审计硬要求「每个动作前后截图 + 结果」；②对外动作的可核验清单（`finalize` 已输出 `outward_actions`，但**是否落库可回看**待确认）；③动作与截图的关联（哪个截图对应哪个动作的"前/后"）。

### 改动（按依赖排序）

| 步 | 改动 | 文件 | 验收 |
|---|---|---|---|
| A1 | 确认 `finalize` 的 `outward_actions` 是否已落库；没有则落 `task_events`（`outward_action` 事件，带 host/工具/URL/时间） | `app/graph/nodes.py` | 审计事件可回看 |
| A2 | 「动作前后截图」：`execute` 里对外动作（click/fill/navigate/scroll 等写向站点的）前后各截一张，artifact 记 `kind="screenshot"` + 加 `label` 标「动作前/动作后」，并关联 `operation_id` | `app/browser.py` + `storage.py`（Artifact 加 `operation_id` 列，迁移 `0008`） | 每个对外动作有前后截图 |
| A3 | 审计时间线查询/展示：已有 `history_get_task` 可回看产物，补一个「列出某任务所有对外动作 + 前后截图」的接口或复用现有事件 | `app/main.py` 或前端 | 能回看、能追责 |

### 关键设计决策

1. **审计是「事后可回看」，不是「事前拦截」**——与 v1「事前零确认、事后审计」的信任模型一致。
2. **「前后截图」有存储成本**——每个动作 2 张图。要加**上限或抽样**（如每任务最多 N 张、或只给「对外动作」截图而非所有工具）。这个阈值要在实现时定。
3. **迁移编号**：Artifact 加列需要新迁移。当前 `0005` 冻结给向量表、`0006/0007` 已用，审计截图加列用 **`0008`**。

---

## 3. 两个任务的排布建议

视觉兜底（V1-V4）和审计时间线（A1-A3）**可以并行也可以串行**。我的建议是：

**先做视觉兜底 V1-V4（一个完整小闭环），再做审计 A1-A3。**

理由：
- 视觉兜底是「能力」、审计是「信任」，前者做完能立刻在真实任务里看到「DOM 失败不再断」的收益，手感好；
- 审计时间线的 A2（前后截图）会**复用视觉兜底 V1 的截图/坐标基础设施**，先做视觉兜底能少写一遍截图逻辑；
- 两个都碰 `browser.py` 和 `storage.py`，串行能避免互相踩 diff。

但如果你更看重「先把信任底座铺好」，也可以先 A1（确认审计落库）——A1 很轻，半小时的事。

---

## 4. 已拍板决策（2026-09-30）

1. **路线**：视觉兜底走**路线 2（真·多模态，模型切 `deepseek-flash`）** ✅
2. **审计截图成本**：接受——每个对外动作前后各一张（待实现，见下）
3. **排布**：先视觉兜底（V0-V4）、后审计（A1-A3）✅

**进度（2026-09-30 晚）**：视觉兜底 V0-V4 已全部完成并通过验证（141 测试全绿 + ruff/mypy clean + 真实浏览器/视觉链路冒烟通过）。剩余工作 = 审计时间线 A1-A3。
