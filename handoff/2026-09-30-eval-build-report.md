# 2026-09-30 · 评测集建设 + 链式核查报告

> 本文回答用户这一轮的三个要求：**构建评测集**、**调用 `.env` 跑真实模型**、**链式核查到代码级、避免"未实现却说实现了"**。

## 0. 一句话结论

评测集从 10 个用例扩到 **14 个**并真跑通（**14/14**）。过程中链式核查出 **3 个真实缺陷**（不是文档问题，是代码问题），全部修到代码级并补了回归测试。
同时确认 **4 项能力确实未实现**，已在 `ARCHITECTURE.md` 里逐条标注代码级依据，不再读起来像已完成。

## 1. 环境：`.env` 真正被用起来了

- `backend/.env`：`ORBIT_MODEL=deepseek-chat`、`ORBIT_MODEL_BASE_URL=https://api.deepseek.com`；**API key 不进 `.env`**，只在系统 keyring（service `orbit:<data_dir绝对路径>`，account `model_api_key`）。
- 评测全程走真实模型 + 真实浏览器，不再有"评测脚本从没跑过真模型"的空缺。
- 本机坑（已修）：本地 `127.0.0.1:6185` 会被系统代理劫持成 502 → `scripts/agent_probe.py` 的 `local_opener()` 用 `ProxyHandler({})` 绕过。

## 2. 评测集（`scripts/eval_cases.json`，14 个用例）

| 维度 | 用例 |
|---|---|
| 浏览器主链路 | 进未配置站点 / 站内搜索+改排序 / 翻页 / **进详情页** / 填表单回车 / 登录态就绪 / 一任务串两站 |
| 不该动手就别动 | 常识问答不动浏览器 |
| 本地文件 | 列目录→读 Markdown→写报告 |
| 记忆 | 跨对话记忆（**再开新对话**校验） |
| 护栏 | 护栏不误触发 + 检索次数上限真的拦停 |
| RAG | 召回上一轮页面 / **检索类回答必须回填来源** |

断言只查行为与结构（不比对站点内容，站点每天在变）。护栏断言分两路：`max_tool_calls` 只数**真正执行**的调用，被拦下的由 `min_blocked_calls` / `require_tool_notice` 单独断言。

## 3. 三个真实缺陷（链式定位到代码）

### D1 · 新标签页竞态：点击"不跳转"其实是跳了

- **现象**：`bili_video_detail` 常驻失败——13 次工具调用后 `ask_user:verification`，最终地址仍停在搜索页。
- **代码级定位**：`app/browser.py::wait_for_change` 每 0.25s 轮询，**发现同页签名变化就立刻返回**。
- **实测证据**（探针）：B 站结果标题是 `target="_blank"`，点击会**同时**做两件事——
  ```
  [t=0.5s] pages 1 -> 2                                     ← 新标签页出现
  clicked page url: .../all?vt=62128072&keyword=机械键盘     ← 旧页面 URL 也变了
  ```
  旧页面在 t≈0 就因 `vt=` 跟踪参数变了签名，函数当场返回**旧页面**，新标签页出现时已经晚了一步。模型据此判定"点了不跳转"，反复重试到放弃。
- **修复**：发现同页签名变化后不立刻收工，留 `grace`（4×0.25s=1s）宽限期继续观察新标签页；新标签页一出现就跟随。
- **回归测试**：`tests/test_browser_lifecycle.py::test_click_follows_a_new_tab_that_opens_after_the_url_already_changed`（先断言旧页 `vt=` 确实变了，再断言交回的是视频页）。

### D2 · 索引把"标题型页面"整页丢光 → RAG 必然 0 召回

这是本轮最有价值的发现：**"RAG 召回上一轮页面"看着做完了，实际是空的。**

- **现象**：`recall_previous_page` 召回@5 = 0%；`citation_on_retrieval_answer` 第二轮"检索不到刚才的内容"。
- **线性排查**：
  1. 查 `chunks` 表：B 站搜索页每个 origin **只有 1 个 34 字的块**，正文是 `'VCT\nVCT\n登录后你可以:\n下载APP\n公益\n官方微博\n官方微信'`（纯导航残渣）。
  2. 查 `tool_results`：同一页面 `text` 有 **3315 字**，视频标题全在里面——**模型看得到，索引却没有**。
  3. 用真实函数复跑 `chunks_for_tool_result(stored_payload)` → **能切出 8~9 块**。说明入库前 payload 里 `structured` 被 `project()` 剥掉了（`OFF_CONTEXT_KEYS = ("structured",)`），存储态和入库态不是一回事。
  4. 走应用真实路径（持久化 profile + headed）探针：`structured=2238 字`（内容齐全，每条视频一个 `### 标题`），`readable_text=2238`，但 `chunk_text()` 只出 **1 块 34 字**。
- **根因（代码级）**：`app/indexing.py::split_blocks` 把 `### 标题` **只当 `title_path` 用掉**，标题自身的文字从不落到任何块里。SPA 结果列表页恰恰是"每个条目就是一个 `###`、标题之间没有正文"，于是整页只剩开头的导航残渣。
- **修复**：标题若迟迟等不到正文（下一个标题/表格/EOF），**标题自己成为一块**；被正文/表格正常消费的标题仍是路径，不重复。
- **效果**：同一页面 `chunk_text` 由 1 块 34 字 → **5 块（406/561/585/582/402 字）**；iQOO 页 4 块。
- **回归测试**：`tests/test_indexing.py::test_heading_only_pages_keep_their_headings`。
- **评测侧证据**：`citation_on_retrieval_answer` 召回@5 **0% → 100%**、引用覆盖 100%；`recall_previous_page` 引用覆盖 **→ 100%**。

### D3 · 两个用例本身不洁净（不是 agent 的错）

- **`memory_across_conversations`**：失败原因"没有调用 memory_save"。查库发现模型答的是"**这条偏好已经记录过了**"——`memories` 表里确实有上一轮留下的记录。**长期记忆是全局的**（`scope=local`），写死的用例第二次运行必然失效；模型不重复保存是**正确行为**。
  → 给评测器加 `{run_id}` 占位符（每轮随机串），用例改成"记住校验码 `{run_id}`"，第二轮在新对话里要求复述。这才真正测出**跨对话召回**。
- **`citation_on_retrieval_answer`**：曾用 `require_tools: ["retriever_search"]` 卡住，但模型这轮是**直接吃 prepare 预取的片段**作答的（召回@5 100%、引用覆盖 100%）——这恰恰是 RAG 该有的样子。断言耦合了实现。
  → 改为 `require_events: ["retrieval"]`（只要有检索注入即可）+ 保留引用覆盖率硬门禁。

## 4. 未实现清单（严格核对，避免"说做了"）

| 能力 | 代码级依据 | 结论 |
|---|---|---|
| **Skill 层** | 无 `backend/app/skills.py`；`tools.py` 未注册 `skill_load`/`skill_save`；`Policies.skills`（`DefaultSkillProvider`）返回空目录且**全仓无任何消费方** | **未实现**。`data/skills/web_search/SKILL.md` 只是格式范本，对行为零影响 |
| **向量路** | `retrieval.py::vector_candidates` 是占位、如实返回 `[]`；迁移只到 `0004_chunks` | **未实现**（已如实标注"空实现，不假装检索成功"） |
| **子 Agent** | `DefaultSubAgentRunner.available = False`；`policies.subagents` 无消费方 | **未实现**（接口留空） |
| **多模态 / OCR** | 无相关代码 | **未实现**（`MVP_COMPLETION_AUDIT.md` 已列为后续范围） |

已实现的 8 个扩展点全部有真实消费方：`context / tool_results / retriever / reflector / planner / indexer / exposer / budget`（均在 `app/graph/nodes.py`）。
**`skills` 与 `subagents` 是仅有的两个"声明了但没人用"的协议。**

文档订正（`ARCHITECTURE.md`）：§1 框图加图例（目标态 vs 已落地）；§6 标题下补"未实现 + 代码依据"；§6.1 改"仅文件落盘，未被加载"；§7 新增"标题本身就是内容"这条易踩点。

## 5. 门禁结果

| 门禁 | 结果 |
|---|---|
| `uv run pytest -q` | **106 passed**（104 → 105 → 106，新增 2 个回归测试） |
| `ruff check app tests ../scripts` | clean |
| `mypy app` | clean（19 files） |
| 前端 `lint` + `build` | 通过（1842 modules） |
| `ui_smoke.py` | **13/13**，无失败项 |
| `eval_rag.py` | **PASS**（rrf recall@5 = 1.000，chars 1.000，trigram 0.417） |
| `eval_run.py`（真实模型+浏览器） | **14/14**，平均 6.7s，平均工具 2.1 次，对外动作 17，平均 recall@5 50%，引用覆盖率 100% |

## 6. 剩余债（触发条件已写明）

1. **prepare 预取那一路的召回 KPI 未定**：`recall_previous_page` 的 recall@5 仍是 0%（引用覆盖率 100% 说明内容最终拿到了，只是不走预取）。按 `GOAL-MODE-PLAN.md` §9.3 的既定决策"先出数、不定 KPI"，暂不加门禁。
2. **索引里的历史残渣**：修复前写入的 34 字导航块仍留在 `chunks` 表（哈希不同、不会被覆盖，也不会匹配键盘类查询，无害）。需要绝对干净时可重建索引。
3. **Skill 层 / 向量路 / 子 Agent / 多模态**：仍是设计态，触发条件见 `MVP_COMPLETION_AUDIT.md` 与 `ROADMAP.md`。
4. **D2 的同类风险**：其它"只有标题、没有正文"的页面结构（如纯目录页）现在能索引了，但 `EXTRACT_JS` 本身对 SPA 列表页的提炼质量仍依赖站点 DOM；若出现新的 0 召回站点，先看 `chunks` 里那个 origin 的 `length(text)`。
