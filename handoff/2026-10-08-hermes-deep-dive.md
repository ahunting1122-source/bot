# Hermes 深度拆解 + 向量方向参考

日期：2026-10-08。对象：`NousResearch/hermes-agent`（MIT）。前作是 `2026-10-08-harness-survey-hermes-vs-dsh.md`（两家对比），本篇**只拆 Hermes，并把它对"向量/检索"的取舍直接对到 Orbit 的向量路实测上**。

---

## 0. 一句话

Hermes 是一个**五层、单体、边界清晰**的 harness：`入口层 → Agent 核心层 → 模型适配层 → 工具层 → 持久化与学习层`。它最反直觉、也最值得 Orbit 抄的一点是——**核心记忆与检索刻意不用向量，只上 SQLite FTS5；向量被放到"可选插件"里**。而且它给出的理由，正好命中 Orbit 这轮实测出来的那个坑。

---

## 1. 代码结构（"承重墙"，读源码从这几个文件开始）

```
hermes-agent/
├── run_agent.py            # AIAgent —— 核心对话对象（大量逻辑已拆到 agent/）
├── model_tools.py          # 工具发现与分发；_discover_tools() 自动 import tools/*.py
├── toolsets.py             # 工具集定义（_HERMES_CORE_TOOLS）
├── cli.py                  # HermesCLI 交互入口
├── hermes_state.py         # SQLite 会话存储 + FTS5
├── agent/
│   ├── conversation_loop.py    # 真实循环（流式/重试/fallback/响应校验/tool call 修复/并发）
│   ├── agent_init.py           # 初始化：provider 解析、OAuth、工具装载
│   ├── prompt_builder.py       # 系统提示装配（三层）
│   ├── context_compressor.py   # 自动上下文压缩
│   ├── prompt_caching.py       # Anthropic 提示缓存
│   ├── memory_manager.py       # 记忆写入 + sync_turn（通知所有 memory provider）
│   ├── transports/ , anthropic_adapter.py , bedrock_adapter.py   # 模型适配
│   └── skill_commands.py
├── hermes_cli/             # commands.py（CommandDef 中央注册表）/ config.py（DEFAULT_CONFIG）/ main.py
├── tools/                  # 40+ 工具，一文件一工具
│   ├── registry.py             # 中央工具注册表（schema/handler/dispatch）
│   ├── approval.py             # 危险命令检测 + per-session approval
│   ├── session_search_tool.py  # ★ 过去会话检索：FTS5 + anchored windows
│   ├── web_tools.py            # web_search / web_extract（Parallel/Firecrawl + Gemini 摘要）
│   ├── vision_tools.py         # 多模态图像分析
│   ├── delegate_tool.py        # 子代理派生 + 并行任务
│   ├── code_execution_tool.py  # 沙箱 Python + RPC 工具访问
│   ├── cronjob_tools.py , skill_tools.py , terminal_tool.py , file_operations.py
│   └── environments/           # base + local/docker/ssh/singularity/modal/daytona
├── gateway/                # run.py（GatewayRunner）/ session.py / platforms/（telegram/discord/slack/whatsapp…）
├── cron/                   # 定时任务调度
├── skills/                 # 内置技能（26 类目）
├── optional-skills/        # ★ 官方可选技能：hub 可发现，默认不激活
└── tests/                  # ~3000 个 pytest
```

**两个"从目录就能读出优先级"的信号**：① `run_agent.py` 排第一、`gateway/` 放最后（与 OpenClaw 正好相反）——**执行与学习是主线，接入是配菜**；② `optional-skills/` 的存在本身就是一种架构主张：能力可以不默认开启。

---

## 2. 六项职责逐条（对齐 Orbit 的 `HARNESS_DESIGN.md`）

| Orbit 职责 | Hermes 落点 | 关键文件 |
|---|---|---|
| 上下文装配 | 三层 prompt + 压缩 + 缓存；**只加载优先级最高的那一个项目文件**（AGENTS.md vs HERMES.md，从 cwd 走到 git root）；SOUL.md 独立全局加载 | `agent/prompt_builder.py` |
| 能力声明 | 中央注册表 + toolset 分组（web/terminal/file/browser…，**可按平台开关**）+ `check_fn` | `tools/registry.py`、`model_tools.py`、`toolsets.py` |
| 调用分发 | `registry` 统一 dispatch，handler 一律返回 JSON 字符串 | `tools/registry.py` |
| 权限与确认 | Smart/Manual/Off + 危险命令检测 + per-session approval + 固定黑名单 | `tools/approval.py` |
| 结果治理 | 每个工具声明**结果大小上限**；超期工具输出在压缩前裁剪；`memory_manager.sync_turn` 通知外部记忆 provider | `tools/registry.py`、`agent/memory_manager.py` |
| 生命周期与恢复 | SQLite 会话 + 事件；`iteration_budget`（默认 **90 轮**）；循环结束触发**后台 review** | `hermes_state.py`、`run_agent.py` |

### 2.1 工具注册的写法（可照抄）

```python
registry.register(
    name="example_tool", toolset="example",
    schema={...},                       # JSON Schema
    handler=lambda args, **kw: ...,
    check_fn=check_requirements,        # ★ 不满足条件则"不出现"在模型工具列表里
    requires_env=["EXAMPLE_API_KEY"],
)
```

自动发现：`tools/*.py` 只要有顶层 `registry.register()` 就会被 `_discover_tools()` 导入，**不需要维护清单**。加一个工具 = 3 个文件（新文件 + `model_tools.py` 的 import + `toolsets.py` 的成员）。

**`check_fn` 就是"注册 vs 暴露"分离的第一刀**：工具在注册表里，但没配 API key 就不暴露给模型。

### 2.2 四条硬规矩（写进 CONTRIBUTING，值得 Orbit 借鉴）

1. **绝不破坏提示缓存**——不要在对话中途改 context / tools / 系统提示。
2. **消息角色必须交替**——不允许连续两条 assistant 或两条 user。
3. **瞬态注入（ephemeral injection）**：系统提示与 prefill **在 API 调用时注入，绝不落库/落日志**。
4. 路径一律走 `get_hermes_home()`，不硬编码 `~/.hermes`（profile 安全）。

> ⚠️ 第 3 条和 dsh 的 **"模型可见即已记录"** 正好**相反**。Hermes 用"不落库"换提示缓存稳定 + 隐私；dsh 用"必须落库"换完全可回放。Orbit 现在是**落库的**（`context.PROMPT_VERSION` 随 `message_started` 入库）——属于 dsh 那一派，这是个**有意的取舍**，不是缺陷，但要清楚自己选了哪边。

---

## 3. 记忆系统：三层，各管一件事（本篇对 Orbit 最有用的部分）

| 层 | 载体 | 容量 | 检索/生效方式 |
|---|---|---|---|
| ① Prompt Memory | `~/.hermes/memories/MEMORY.md` + `USER.md` | MEMORY.md **2,200 字符(~800 tok)**、USER.md **1,375 字符(~500 tok)**，合计 **~3,575 字符 / ~1,300 tok** | 会话开始**整体注入**、**冻结快照** |
| ② Episodic Archive | `~/.hermes/state.db`（SQLite + **FTS5**） | 无上限 | `session_search` 工具按需查，**~20ms，不花 LLM** |
| ③ Skill Memory | `~/.hermes/skills/*.md` | 无上限 | 提示里只放**名字+短摘要**，按需加载全文 |

### 3.1 三个必须记住的机制细节

1. **冻结快照（frozen snapshot）**：记忆在会话开始注入一次，**会话中写文件会立即落盘，但当前 system prompt 不变**，下次会话才生效。目的就是**保住 Anthropic 的提示缓存**。→ Orbit 的 `build_context()` 每轮重装，与这条相反；若 Orbit 要长期跑，值得考虑"会话内冻结 + 下轮刷新"。
2. **`§` 分隔 + 字符上限 + 主动合并**：条目用 section sign 分隔；接近上限时**先合并相近事实**再加新条目。写入策略很明确——*该存*：偏好、环境事实、项目约定、纠正、工具怪癖；*不该存*：会话结果、完成记录、临时任务态（这些交给 `session_search`）；*流程类*：交给 skill。
3. **`memory` 工具三动作**：`add` / `replace` / `remove`。另有**周期性 nudge**：不是等用户说"记住"，而是 agent 自己判断什么值得持久化。

### 3.2 "两个问题要分开"（这条是整篇的题眼）

Hermes 明确把 **recall（过去发生了什么 = 检索问题）** 和 **user modeling（这个人是谁、怎么工作 = 摘要/推断问题）** 拆开：
- recall → **FTS5 全文本搜索**（本地、快、可解释）
- user modeling → 3,575 字符的画像文件；想要更深的建模就交给外部（Honcho 的 dialectic reasoning）

原文的判词很准：**"把这两个问题混为一谈，是很多 agent 设计出错的根源。"**

> Orbit 的对应关系已经是对的：RAG（检索）与 `MEMORY.md`（画像）本就是两套东西。这条可以作为 Orbit 拒绝"用一套向量把记忆和检索全都做了"的**外部依据**。

---

## 4. 学习回路（skill 怎么产生、怎么更新）

- ~~**触发阈值**：一次任务满足任一条件才生成 skill —— **≥5 次工具调用**、**发生了一次错误恢复**、**用户做了一次纠正**。~~
  ⚠️ **2026-10-08 二次核查已推翻此条**：源码（`agent/background_review.py`、`agent/curator.py`）**不存在任何工具调用次数阈值**；
  "错误恢复"不是触发条件而是**反面约束**（`_DO_NOT_CAPTURE_BLOCK` 要求"已解决的瞬时错误不要捕获"）；
  "用户纠正"是 review prompt 的**信号清单首项**，判据定性、无计数。
  真实机制 = **每轮对话后都 fork review + 强信号清单**（原文 "most sessions produce at least one skill update"）。
  详见 `2026-10-08-hermes-skill-mcp-source-read.md` §0。
- **落点**：主循环 `iteration_budget`（90 轮）结束后，触发**后台 review 流程** —— 这是 skill 与记忆自动沉淀的**唯一入口**。
- **就地精炼**：执行中发现比 skill 文档更好的做法，**无需提示即更新文档**。
- **渐进披露（progressive disclosure）**：提示里只放 skill 名 + 摘要，全文按需加载。→ **200 个技能与 40 个技能的上下文成本接近**。
- **代价**：独立分析给出自我反思开销约 **+15%~25% token**。
- **形态**：纯 Markdown，人类可读可改可删——**没有不可检视的向量库**（原话）。

> 对照 Orbit：Orbit 的 skill 层已有"注入择优/成功回写/治理三态"，比 Hermes 的**证据口径更严**（Orbit 用 `SUCCEEDED` 终态回写、连续失败降级、超期归档）。**Hermes 更值得抄的是"渐进披露"与写入侧的「不要捕获」清单**（后者 Orbit 完全没有）。

---

## 5. 压缩与谱系（与 Orbit 的 digest/摘要最相关）

- 触发：每轮 API 调用后评估 token 用量，超过阈值（默认约**模型窗口的 50%**）触发压缩器。
- 三步：**① 裁剪超期工具输出（占位符替换）→ ② Token 预算保护头尾 → ③ 中段交辅助模型结构化摘要**，最后拼成"头 + 摘要 + 尾"，并**修复因压缩产生的孤儿 tool_call/tool_result 配对**。
- 摘要预算 ≈ 压缩内容的 **20%**，下限 **2k** / 上限 **12k** token。
- **压缩 = session 生命周期事件**：关当前行 → 子 session 由摘要种子 → **轮换 session ID** → 记父子谱系；多次压缩得到**谱系链**。→ Orbit 的摘要目前是**原地改写**，可以照这个改。

---

## 6. 向量方向参考（本篇重点）

### 6.1 Hermes 的立场：**核心不做向量，向量是可选插件**

这是外部证据里**最该拿回 Orbit 的一条**。Hermes 的核心记忆/会话检索**只有 FTS5，没有 embedding、没有向量索引**，而且是**刻意**的。它自己列的理由：

1. **不需要独立向量库** → 整个 agent 栈 **<500MB**，能在 **$5/月 VPS** 上跑生产。
2. **每次查询都要付 embedding 延迟**；FTS5 在 SQLite 层，`session_search` 实测 **~20ms、不花 LLM 调用**。
3. **向量在精确匹配上会退化**：原文例子——skill 里写着"用 Apollo API，不要用 Clearbit"，若按向量相似度检索，**可能召回一个语义相邻、但用的是 Clearbit 的 skill**。
4. **可解释**：FTS5 能说清"为什么命中"，向量只能"数学上近"。

而向量能力被放在**可选插件**里，两种形态：

| 插件/技能 | 形态 | 检索与融合 | 备注 |
|---|---|---|---|
| **lancedb 插件** | 记忆 provider（`hermes memory setup` 激活） | 模式 = **vector（默认）/ hybrid / fts**；hybrid 融合 = **`rrf` / `linear`（加权，`reranker.weight` 是向量权重）/ `cross-encoder`（过采样池 + sentence-transformers 重排）** | 默认后端是 **OpenAI `text-embedding-3-small`（云端 API）**；**hybrid 失败自动回退纯 vector**；工具 `lancedb_recall/remember/read/forget`，按 content hash 去重 |
| **qmd 技能**（Query Markup Documents） | 本地文档 RAG 技能 | **sqlite-vec + FTS5**：BM25 腿 + 向量腿（本地 GGUF 嵌入）+ **LLM 重排（RRF + query expansion）**；查询用 **HyDE** 提召回 | 索引 `~/.cache/qmd/index.sqlite`；**切块 ~900 token / 15% overlap，按标题·代码块·空行切，代码块保持完整**；三种模式 `search`(~0.2s) / `vsearch`(~3s) / rerank(~2-3s warm, ~19s cold)；约 **2GB** 模型 |

> 另注：Hermes 的 **GitHub issue #844（workspace RAG）计划用 `fastembed`** 做 PDF/DOCX 抽取——**和 Orbit 现在用的是同一个库**。

### 6.2 把它对到 Orbit 的向量路实测

Orbit 第二轮的实测结论是：**A 集融合 0.562（门槛 0.70）/ B 集融合 0.750（门槛 0.85），但 B 集"向量单路" 0.917 已过门槛；加权是 wash；瓶颈在 RRF 融合本身。**

| Hermes 的做法 | Orbit 的实测/现状 | 结论 |
|---|---|---|
| 核心**不上向量**，FTS5 够用 | Orbit 把向量做进**主依赖**（D2 决策） | Hermes 的论据（成本/延迟/精确匹配退化）**恰好命中 Orbit 的两处痛点** |
| 向量放**可选插件**，默认不激活 | Orbit 无插件层，向量是 core | Orbit 缺"可选开关"这层——`vector_enabled` 默认 `False` 只是半个开关 |
| **精确匹配会退化**（Apollo vs Clearbit） | B 集是"词面混合"集，FTS 该赢的部分被融合压掉；A 集（语义改写）向量 0.562 仍不及 **0.70** | **Orbit 的 B 集问题，本质就是 Hermes 说的那个退化**——不是模型不行（向量单路 0.917），是"该让 FTS 赢的查询被向量拉平了" |
| 融合给三种：**rrf / linear(加权) / cross-encoder** | Orbit 只测了 RRF **权重**，结论 wash；计划 §4.3 预留"阶段 3 = 重排" | **Orbit 还没试的是 `cross-encoder` 重排**——这正是 Hermes 用来解决"融合失真"的第三档 |
| **hybrid 失败 → 回退纯 vector** | Orbit 三路**固定融合**，无回退 | 可借鉴：**融合置信度低时直接回退单路**，而不是硬融 |
| chunk ~**900 token / 15% overlap** | Orbit **300–500 字符** | Hermes 块大得多（嵌入窗口大）。Orbit 换 gemma-768 后窗口更宽，**切块上限可重估**（与既有待办"阶段 6 复测"一致） |
| recall 与 user modeling **分开** | Orbit: RAG 与 MEMORY.md 本就分开 | **一致，可引用为外部依据** |
| 检索**零 LLM 成本**（FTS5 本地 ~20ms） | Orbit 向量：gemma **173ms/块**、单次观察 **4.84s**、**5.6× 慢** | 直接为 Orbit 的"5.6× 延迟"待办**提供了对照基线** |
| `session_search` = FTS5 + **anchored windows** + LLM 摘要 | Orbit RAG = chunk + 引用 | 一致思路；`anchored windows` 可作 Orbit 引用窗口的参考 |

### 6.3 三条可直接采用的结论

1. **别再往"给向量加权"上加码**。Hermes 的 hybrid 也把 `rrf`（等权）列为默认，加权是**可选项而非解法**；它真正的第三档是 **cross-encoder 重排**。→ **Orbit 的下一步应该是"重排"，而不是继续调权重**；这也和 `HARNESS_DESIGN.md §4.3` 早已规划的第 3 阶段吻合。
2. **区分"精确匹配查询"与"语义查询"，分别走 FTS 与向量**——也就是 Hermes 说的"别指望一个工具干两件事"。→ Orbit 可以考虑**按查询类型路由**（含专名/型号/路径的走 FTS 优先；自然语言问句走向量优先），而不是永远三路等权融合。这比调权重更能解 B 集。
3. **向量要么做成可关（现已有 `vector_enabled`），要么做成可选插件**；并且**默认不因它变慢**（Hermes 靠"不默认启用"绕开了 5.6× 延迟问题）。→ Orbit 的 gemma 5.6× 延迟若长期无解，**"默认关、按需开"就是 Hermes 给的现成答案**。

### 6.4 一个反方观点（也要如实记）

Hermes 的 FTS5-only 在**"用词不同但语义相同"**的检索上就是弱——这正是 Orbit 建向量路的初衷（A 集）。所以正确读法不是"Hermes 说向量没用"，而是：
**"FTS 覆盖大部分检索；向量的价值集中在语义改写类查询，代价（延迟/复杂度/精确匹配退化）必须靠'可选 + 重排'来控制，而不是塞进核心主干。"**

---

## 7. 来源

- 仓库结构与开发规范：`github.com/NousResearch/hermes-agent`（AGENTS.md / CONTRIBUTING.md / 源码目录）。
- 记忆与检索：官方文档 `hermes-agent.nousresearch.com` → `user-guide/features/memory`；`fast.io` 记忆配置指南（8 个外部 provider 对比）；`dev.to`《Your AI Agent Probably Doesn't Need a Vector Database》。
- 向量插件：官方文档 `hermes-agent.nousresearch.com/docs/plugins/lancedb`；`qmd` 技能指南（`fast.io`）。
- 架构与九层：Arize《How Hermes implements an open source agent harness architecture》（2026-06）；`innfactory.ai/en/ai-harness/hermes-agent`；`espressio.ai` 完整指南（FTS5 vs 向量的选型论证）。
- 学习回路：`revolutioninai.com` 两篇（skill 阈值、三层记忆、+15–25% 开销）。
- 早前《Project Structure》与《架构深度解读》（CSDN）用于交叉核对文件路径。
