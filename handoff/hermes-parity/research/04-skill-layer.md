# 04 — Hermes Skill 层 + Curator 层完整清单与机制

> 目标机：Windows，Hermes Agent CN 发行版 runtime `0.21.0-cn.18`（kernel 0.21.0，channel stable，
> sourceRepo `Eynzof/Hermes-CN-Core`，sourceCommit `b9f82a21fd9268bd499051e23b614ecb06d5b888`，
> 见 `...\0.21.0-cn.18\manifest.json`）。
> 本文所有结论均带源码文件 / 函数名 / 行号 / 实测数值证据。只读勘察，未修改 Hermes 任何文件，
> exe 仅以 `--help` 调用，未安装任何依赖。

---

## 0. 证据边界（必读，影响第 5 节的数值可信度）

| 结论 | 证据 |
|---|---|
| **技能库类目数 = 21**（不是任务书假设的 26）；另有 `index-cache/` 是远端索引缓存目录，**不是**类目 | `Get-ChildItem _internal\skills -Directory` 共 22 项，其中 `index-cache` 只含 4 个 `.json`（`anthropics_skills_skills_.json` / `claude_marketplace_anthropics_skills.json` / `lobehub_index.json` / `openai_skills_skills_.json`），无 `SKILL.md` |
| **技能数 = 91**（`SKILL.md` 文件数）；其中 87 个位于类目下、4 个位于库根 | `(Get-ChildItem -Recurse -Filter SKILL.md).Count = 91` |
| `bundled-skills\` 与 `_internal\skills\` **规模一致**（21 类目 / 91 技能） | 同上脚本对 `E:\Hermes Agent CN Desktop\bundled-skills` 运行得 21 / 91 |
| 4 个类目目录里**没有**技能，只有 1 个「顶层技能」直接放在类目目录里 | `computer-use\SKILL.md`、`dogfood\SKILL.md`、`hermes-desktop-plugins\SKILL.md`、`yuanbao\SKILL.md` → 这 4 个是「类目名 = 技能名」的根级技能 |
| 用户已装技能库（`hermes-home\skills\`）规模远大于 bundled：**36 个一级目录 + 165 条 prompt 装配条目** | `.skills_prompt_snapshot.json` 的 `skills` 数组长度 = 165；`manifest` 键数 = 183 |

### 0.1 关键限制：curator 主体源码在本机**不可读**

`skills_tool.py:2150` 与多处注释指向 `agent/curator.py`、`agent/skill_utils.py`、`agent/prompt_builder.py`、
`agent/curator_backup.py`，但：

```
Get-ChildItem 'E:\Hermes Agent CN Desktop' -Recurse -Filter curator.py   → 0 命中
Get-ChildItem 'E:\Hermes Agent CN Desktop' -Recurse -Filter skill_utils.py → 0 命中
_internal\agent\  只含 models_dev_snapshot.json / swarm-mode-*.md（纯数据）
```

`_internal\hermes_agent-0.21.0.dist-info\RECORD` 显示这是 **editable 安装**（`__editable__.hermes_agent-0.21.0.pth` +
finder），但这两个文件在打包时被移除；`top_level.txt` 声明 `agent`、`cli`、`tools`、`gateway` 等 33 个顶层包，
其中只有 `tools\` 以 `.py` 形式落盘（147 个文件），其余包被压进 exe 的 PyInstaller PYZ。

对 75 MB 的 `hermes-agent-cn-runtime-win32-x64.exe` 做流式字节扫描：

```
needles: stale_after_days / archive_after_days / min_idle_hours / curator.py / skill_utils.py
结果: 全部 = 0 命中（PYZ 内模块经 zlib 压缩，明文不可检索）
```

**因此**：`curator.stale_after_days` / `curator.archive_after_days` / `interval_hours` / `min_idle_hours`
的**出厂默认数值无法从本机静态取证**。第 5 节给出的是「可证部分 + 取证路径」，凡不可证者均明确标注
`⚠ 不可证`，绝不臆造数字。

---

## 1. 技能清单

### 1.1 类目 → 技能 → description（逐条，共 91 条）

description 取自各 `SKILL.md` frontmatter 原文（超过 150 字符者截断加 `…`）。

#### apple（4）
| 技能目录 | frontmatter name | description |
|---|---|---|
| `apple\apple-notes` | apple-notes | Manage Apple Notes via memo CLI: create, search, edit. |
| `apple\apple-reminders` | apple-reminders | Apple Reminders via remindctl: add, list, complete. |
| `apple\findmy` | findmy | Track Apple devices/AirTags via FindMy.app on macOS. |
| `apple\imessage` | imessage | Send and receive iMessages/SMS via the imsg CLI on macOS. |

#### autonomous-ai-agents（5）
| 技能目录 | name | description |
|---|---|---|
| `claude-code` | claude-code | Delegate coding to Claude Code CLI (features, PRs). |
| `codex` | codex | Delegate coding to OpenAI Codex CLI (features, PRs). |
| `computer-use` | computer-use | Drive the desktop background-first; escalate on signal. |
| `hermes-agent` | hermes-agent | Use, configure, theme, extend, and orchestrate Hermes Agent. |
| `opencode` | opencode | Delegate coding to OpenCode CLI (features, PR review). |

#### creative（16）
| 技能目录 | name | description |
|---|---|---|
| `architecture-diagram` | architecture-diagram | Dark-themed SVG architecture/cloud/infra diagrams as HTML. |
| `ascii-art` | ascii-art | ASCII art: pyfiglet, cowsay, boxes, image-to-ascii. |
| `ascii-video` | ascii-video | ASCII video: convert video/audio to colored ASCII MP4/GIF. |
| `baoyu-infographic` | baoyu-infographic | Infographics: 21 layouts x 21 styles (信息图, 可视化). |
| `claude-design` | claude-design | Design one-off HTML artifacts (landing, deck, prototype). |
| `comfyui` | comfyui | Generate images, video, and audio with ComfyUI — install, launch, manage nodes/models, run workflows with parameter injection. Uses the official comfy… |
| `design-md` | design-md | Author/validate/export Google's DESIGN.md token spec files. |
| `excalidraw` | excalidraw | Hand-drawn Excalidraw JSON diagrams (arch, flow, seq). |
| `humanizer` | humanizer | Humanize text: strip AI-isms and add real voice. |
| `manim-video` | manim-video | Manim CE animations: 3Blue1Brown math/algo videos. |
| `p5js` | p5js | p5.js sketches: gen art, shaders, interactive, 3D. |
| `popular-web-designs` | popular-web-designs | 54 real design systems (Stripe, Linear, Vercel) as HTML/CSS. |
| `pretext` | pretext | Use when building creative browser demos with @chenglou/pretext — DOM-free text layout for ASCII art, typographic flow around obstacles, text-as-geome… |
| `sketch` | sketch | Throwaway HTML mockups: 2-3 design variants to compare. |
| `songwriting-and-ai-music` | songwriting-and-ai-music | Songwriting craft and Suno AI music prompts. |
| `touchdesigner-mcp` | touchdesigner-mcp | Control a running TouchDesigner instance via twozero MCP — create operators, set parameters, wire connections, execute Python, build real-time visuals… |

#### data-science（1）
| `jupyter-live-kernel` | jupyter-live-kernel | Iterative Python via live Jupyter kernel (hamelnb). |

#### devops（1）
| `sdlc-review` | sdlc-review | Review Kanban handoffs and route verified outcomes. |

#### email（2）
| `email-inbox-triage` | email-inbox-triage | Triage an inbox: prioritize threads, draft replies safely. |
| `himalaya` | himalaya | Himalaya CLI: IMAP/SMTP email from terminal. |

#### github（6）
| `codebase-inspection` | codebase-inspection | Inspect codebases w/ pygount: LOC, languages, ratios. |
| `github-auth` | github-auth | GitHub auth setup: HTTPS tokens, SSH keys, gh CLI login. |
| `github-code-review` | github-code-review | Review PRs: diffs, inline comments via gh or REST. |
| `github-issues` | github-issues | Create, triage, label, assign GitHub issues via gh or REST. |
| `github-pr-workflow` | github-pr-workflow | GitHub PR lifecycle: branch, commit, open, CI, merge. |
| `github-repo-management` | github-repo-management | Clone/create/fork repos; manage remotes, releases. |

#### media（4）
| `gif-search` | gif-search | Search/download GIFs from Tenor via curl + jq. |
| `heartmula` | heartmula | HeartMuLa: Suno-like song generation from lyrics + tags. |
| `songsee` | songsee | Audio spectrograms/features (mel, chroma, MFCC) via CLI. |
| `youtube-content` | youtube-content | YouTube transcripts to summaries, threads, blogs. |

#### mlops（7，含 3 个二级子类：`evaluation` / `inference` / `models`）
| 技能目录 | name | description |
|---|---|---|
| `mlops\evaluation\lm-evaluation-harness` | **evaluating-llms-harness** | lm-eval-harness: benchmark LLMs (MMLU, GSM8K, etc.). |
| `mlops\evaluation\weights-and-biases` | weights-and-biases | W&B: log ML experiments, sweeps, model registry, dashboards. |
| `mlops\huggingface-hub` | huggingface-hub | HuggingFace hf CLI: search/download/upload models, datasets. |
| `mlops\inference\llama-cpp` | llama-cpp | llama.cpp local GGUF inference + HF Hub model discovery. |
| `mlops\inference\vllm` | **serving-llms-vllm** | vLLM: high-throughput LLM serving, OpenAI API, quantization. |
| `mlops\models\audiocraft` | **audiocraft-audio-generation** | AudioCraft: MusicGen text-to-music, AudioGen text-to-sound. |
| `mlops\models\segment-anything` | **segment-anything-model** | SAM: zero-shot image segmentation via points, boxes, masks. |

> 注意：`mlops` 有 **3 层目录**（`mlops/evaluation/<skill>`），目录名与 frontmatter name 有 4 处不一致
> （见上表加粗）。这直接影响 `skill_linter` 的 `name-dir-mismatch` 规则（见 §3.5）。

#### note-taking（1）
| `obsidian` | obsidian | Read, search, create, and edit notes in the Obsidian vault. |

#### productivity（17）
| `airtable` | airtable | Airtable REST API via curl. Records CRUD, filters, upserts. |
| `box` | box | Box manages cloud files, sharing, search, and metadata. |
| `document-to-action-items` | document-to-action-items | Extract cited obligations, deadlines, tasks from documents. |
| `docx` | docx | Create, read, edit, template, and review Word .docx files. |
| `google-workspace` | google-workspace | Gmail, Calendar, Drive, Docs, Sheets via gws CLI or Python. |
| `maps` | maps | Geocode, POIs, routes, timezones via OpenStreetMap/OSRM. |
| `meeting-action-items` | meeting-action-items | Turn meeting notes into cited decisions, owners, tickets. |
| `nano-pdf` | nano-pdf | Edit PDF text/typos/titles via nano-pdf CLI (NL prompts). |
| `notion` | notion | Notion API + ntn CLI: pages, databases, markdown, Workers. |
| `ocr-and-documents` | ocr-and-documents | Extract text from PDFs/scans (pymupdf, marker-pdf). |
| `pdf` | pdf | PDF files: create, read, merge, fill, OCR, edit text. |
| `petdex` | petdex | Install and select animated petdex mascots for Hermes. |
| `powerpoint` | powerpoint | Create, read, edit .pptx decks with python-pptx. |
| `product-price-monitor` | product-price-monitor | Watch product, flight, or listing prices; alert on target. |
| `teams-meeting-pipeline` | teams-meeting-pipeline | Teams meeting summaries, job replay, Graph subscriptions. |
| `weekly-review-planning` | weekly-review-planning | Weekly reset: commitments, stalled work, next-week plan. |
| `xlsx` | xlsx | Create, read, edit Excel .xlsx workbooks and CSVs. |

#### research（7）
| `arxiv` | arxiv | Search arXiv papers by keyword, author, category, or ID. |
| `blogwatcher` | blogwatcher | Monitor blogs and RSS/Atom feeds via blogwatcher-cli tool. |
| `competitor-news-monitor` | competitor-news-monitor | Watch named companies for material news; cited digests. |
| `grounded-citations` | grounded-citations | Ground answers and documents in cited, verifiable sources. |
| `llm-wiki` | llm-wiki | Karpathy's LLM Wiki: build/query interlinked markdown KB. |
| `polymarket` | polymarket | Query Polymarket: markets, prices, orderbooks, history. |
| `research-paper-writing` | research-paper-writing | Write ML papers for NeurIPS/ICML/ICLR: design→submit. |

#### smart-home（1）
| `openhue` | openhue | Control Philips Hue lights, scenes, rooms via OpenHue CLI. |

#### social-media（1）
| `xurl` | xurl | X/Twitter via xurl CLI: raw post search, posting, DM, media. |

#### software-development（13）
| `codebase-inspection` | codebase-inspection | Inspect codebases w/ pygount: LOC, languages, ratios. |
| `dogfood` | dogfood | Exploratory QA of web apps: find bugs, evidence, reports. |
| `github` | github | GitHub via gh CLI: PRs, issues, reviews, repos, auth. |
| `hermes-agent-skill-authoring` | hermes-agent-skill-authoring | Author in-repo SKILL.md files: frontmatter and structure. |
| `inspecting-hermes-desktop-dom` | inspecting-hermes-desktop-dom | Read the live Hermes desktop DOM/CSS over CDP. |
| `node-inspect-debugger` | node-inspect-debugger | Debug Node.js via --inspect + Chrome DevTools Protocol CLI. |
| `plan` | plan | Plan mode: write an actionable markdown plan to .hermes/plans/, no execution. Bite-sized tasks, exact paths, complete code. |
| `python-debugpy` | python-debugpy | Debug Python: pdb REPL + debugpy remote (DAP). |
| `requesting-code-review` | requesting-code-review | Pre-commit review: security scan, quality gates, auto-fix. |
| `simplify-code` | simplify-code | Parallel 4-agent cleanup of recent code changes. |
| `spike` | spike | Throwaway experiments to validate an idea before build. |
| `systematic-debugging` | systematic-debugging | 4-phase root cause debugging: understand bugs before fixing. |
| `test-driven-development` | test-driven-development | TDD: enforce RED-GREEN-REFACTOR, tests before code. |

#### web（1）
| `blocked-page-recovery` | blocked-page-recovery | Use when a fetch fails: 403/429, paywall, WAF, bot wall. |

#### 库根级技能（4，不属于任何类目）
| `computer-use\SKILL.md` | computer-use | Drive the user's desktop in the background — clicking, typing, scrolling, dragging — without stealing the cursor, keyboard focus, or switching virtu… |
| `dogfood\SKILL.md` | dogfood | Exploratory QA of web apps: find bugs, evidence, reports. |
| `hermes-desktop-plugins\SKILL.md` | hermes-desktop-plugins | Write desktop app plugins that add UI panes and commands. |
| `yuanbao\SKILL.md` | yuanbao | Yuanbao (元宝) groups: @mention users, query info/members. |

**同名冲突实测**（bundled 库内已存在 4 组重名，是 §2.4 first-wins 规则的真实样本）：

| name | 出现位置 |
|---|---|
| `computer-use` | 根级 `computer-use\` + `autonomous-ai-agents\computer-use\` |
| `dogfood` | 根级 `dogfood\` + `software-development\dogfood\` |
| `codebase-inspection` | `github\codebase-inspection\` + `software-development\codebase-inspection\` |
| `github` | 仅 `software-development\github\`（与 `github\` 类目名同名，非技能重名） |

### 1.2 `DESCRIPTION.md`：类目级描述文件

每个类目目录下有一个 `DESCRIPTION.md`（`mlops` 的 3 个子类也各有一个），用于**类目摘要**，不参与技能发现。
它被装配进 `.skills_prompt_snapshot.json` 的 `category_descriptions`。实测样例（`snapshot.category_descriptions`）：

```json
"creative": "Creative content generation — ASCII art, hand-drawn style diagrams, and visual design tools.",
"mlops/inference": "Model serving, quantization (GGUF/GPTQ), structured output, inference optimization, and model surgery tools for deploying and running LLMs."
```

类目键使用 `/` 连接嵌套层级（`mlops/evaluation`、`productivity/ocr-and-documents`），实测共 **17 条**。

### 1.3 SKILL.md frontmatter 字段全集（16 个顶层键 + 1 个嵌套块）

统计方法：对 91 个 `SKILL.md` 逐文件正则抽取顶层 `^key:`。出现频次即证据。

| # | 字段 | 出现次数 | 类型 | 含义 | 是否必填 |
|---|---|---|---|---|---|
| 1 | `name` | 91/91 | str | 技能唯一标识，也是 `.usage.json` / `.bundled_manifest` 的键。`skill_manager_tool.py:197 MAX_NAME_LENGTH=64`；`skills_tool.py:175` 同值；`VALID_NAME_RE = ^[a-z0-9][a-z0-9._-]*$`（`skill_manager_tool.py:554`） | **必填（硬校验）** `_validate_frontmatter` 缺则报 `Frontmatter must include 'name' field.` |
| 2 | `description` | 91/91 | str | 一句话能力陈述。三档上限：硬校验 `MAX_DESCRIPTION_LENGTH=1024`（`skill_manager_tool.py:198`）；**新建技能额外 60 字符预算**（`SKILL_PROMPT_DESC_LIMIT`，`_validate_frontmatter(new_skill=True)` 在 644–651 行强制）；linter 建议 ≤60 | **必填（硬校验）** |
| 3 | `version` | 91/91 | str | 语义化版本。新建建议 `0.1.0`（`hermes-agent-skill-authoring` skill 规范） | 否（校验器不查），但 linter 报 `missing-metadata` |
| 4 | `platforms` | 91/91 | list[str] | 宿主 OS 门控。合法值 `linux` / `macos` / `windows` / `darwin`（`skill_linter.py:348`）；`_PLATFORM_MAP` 在 `skills_tool.py:180`。**省略 = 全平台加载**；不匹配则在扫描阶段直接 `continue`（`skills_tool.py:794`） | 否，但 91/91 全都有 |
| 5 | `metadata` | 87/91 | map | 任意键值（agentskills.io 兼容）。Hermes 约定子键 `metadata.hermes.tags` / `metadata.hermes.related_skills` | 否；linter 报 `missing-metadata` |
| 6 | `author` | 86/91 | str | 作者署名。规范：**人类在前**，`Ben Barclay (benbarclay), Hermes Agent` | 否；linter 报 `missing-metadata` / `author-caps` |
| 7 | `license` | 85/91 | str | 许可证（多为 `MIT`） | 否；linter 报 `missing-metadata` |
| 8 | `prerequisites` | 16/91 | map | **运行时依赖**。子键 `env_vars: [...]`（旧式环境变量名，加载时被规范化为 `required_environment_variables`）、`commands: [...]`（命令检查，**仅提示不阻断**）。见 `skills_tool.py:37-40` 与 `_collect_prerequisite_values`(309) / `_normalize_setup_metadata`(321) | 否 |
| 9 | `dependencies` | 10/91 | list/map | 包/工具依赖声明（如 `python: [torch]`）。消费方为文档与安装提示，不阻断加载 | 否 |
| 10 | `tags` | 4/91 | list[str] | 顶层标签（旧式）。**推荐位置是 `metadata.hermes.tags`**，故仅 4 个技能用顶层。解析函数 `_parse_tags`（`skills_tool.py:629`）同时兼容 `[a, b]`、`a, b`、已解析列表 | 否 |
| 11 | `triggers` | 2/91 | list[str] | 显式触发词/触发场景（`popular-web-designs`、`songwriting-and-ai-music`）。非标准字段，不参与门控 | 否 |
| 12 | `required_credential_files` | 1/91 | list | 需要的凭证文件路径清单（仅 `productivity\google-workspace`）。配合 `_capture_required_environment_variables`(435) 的凭证采集/落盘流程 | 否 |
| 13 | `title` | 1/91 | str | 展示标题（仅 `research\research-paper-writing`） | 否 |
| 14 | `environments` | 1/91 | map/list | 环境门控（仅 `devops\sdlc-review`）。消费方 `skill_matches_environment`（`skills_tool.py:289`） | 否 |
| 15 | `compatibility` | 1/91 | str | 兼容性说明（agentskills.io 字段，仅 `creative\comfyui`） | 否 |
| 16 | `setup` | 1/91 | map | 结构化安装/配置元数据（仅 `creative\comfyui`），由 `_normalize_setup_metadata`(321) / `_build_setup_note`(563) 消费 | 否 |

**嵌套块 `metadata.hermes`**（linter `_check_metadata_block` 明确要求）：

| 子字段 | 含义 |
|---|---|
| `metadata.hermes.tags` | 技能标签，用于检索与分类展示 |
| `metadata.hermes.related_skills` | 相关技能名列表；`hermes-agent-skill-authoring` 规范要求**每一项必须在同一技能树中真实存在** |

**frontmatter 解析实现**：不是裸 `yaml.safe_load`，而是 `agent.skill_utils.parse_frontmatter`（`skills_tool.py:85` 导入），
自带 BOM 容错（`content.lstrip("\ufeff")`，`skill_manager_tool.py:618`）与平台匹配。扫描时只读前 **4000 字符**
（`skills_tool.py:791` `read_text(...)[:4000]`）以控制开销。

**扫描阶段的兜底**：若 `description` 缺失，取 body 中第一行非标题文本（`skills_tool.py:806-812`）；
超长则截到 `MAX_DESCRIPTION_LENGTH-3 + "..."`（814-815）。

---

## 2. 多根加载与优先级

### 2.1 加载根（4 类，代码位置 `skills_tool.py:725-748`）

```python
from agent.skill_utils import (
    get_external_skills_dirs, get_project_skills_dirs,
    iter_project_skill_files, iter_skill_index_files,
)
project_dirs = list(get_project_skills_dirs())     # ① 项目级（已 trust）
dirs_to_scan: list = list(project_dirs)            #    项目根优先
active_skills_dir = _skills_dir()                  # ② 用户级 ~/.hermes/skills
if active_skills_dir.exists(): dirs_to_scan.append(active_skills_dir)
dirs_to_scan.extend(get_external_skills_dirs())    # ③ 外部目录 skills.external_dirs
```

| tier | 根 | 解析函数 | 说明 |
|---|---|---|---|
| ① **project**（最高） | `<repo>/.hermes/skills`、`<repo>/.agents/skills` | `get_project_skills_dirs()` / `iter_project_skill_files()` | **必须先 trust**：`hermes skills trust [path]`（默认取 cwd 所属 git checkout）。`untrust` 撤销。见 CLI `skills trust` 帮助原文 |
| ② **user / profile** | `~/.hermes/skills`（本机 = `data\hermes-home\skills`） | `_skills_dir()`（`skills_tool.py:160`，**每次调用实时解析** `get_hermes_home()`，支持多 profile） | 单一真相源：bundled 播种、hub 安装、agent 自建全部共存于此 |
| ③ **external** | `skills.external_dirs` 配置项 | `get_external_skills_dirs()` | **只读**：`skill_usage._external_read_only_message()`「external skills are read-only to the curator」；不可被 curator 归档/改写 |
| ④ **plugin**（正交） | 插件包内技能，命名空间形式 `plugin:skill` | `pm.find_plugin_skill()`（`skills_tool.py:1110+`，名字含 `:` 时走此分支） | 命名空间须匹配 `[a-zA-Z0-9_-]+` |
| ⑤ **bundled 源**（非运行时根） | `_internal\skills` / `bundled-skills` | `get_bundled_skills_dir()`（`skills_sync.py:118`） | **不作为加载根**，只在安装/更新时被「播种」进 ② |

### 2.2 tier 优先级与冲突规则

1. **first-wins（先到先得）**，不是合并、不是报错。`skills_tool.py:765, 800-804`：
   ```python
   seen_names: set = set()
   ...
   name = frontmatter.get("name", skill_dir.name)[:MAX_NAME_LENGTH]
   if name in seen_names: continue      # 后来的同名技能被静默丢弃
   if name in disabled:   continue
   seen_names.add(name)
   ```
   因 `dirs_to_scan` 顺序是 project → user → external，**优先级 = project > user > external**。
   注释明确写了：`# Trusted project-local dirs come FIRST: first-wins dedup below gives them precedence over same-named local/external skills.`（741-742 行）
2. **bundled 不覆盖 user**：bundled 只在 `sync_skills()`（`skills_sync.py:712`）里播种，且「用户改过就跳过」——见 §6.1。
3. **disabled 优先于一切**：`_get_disabled_skill_names()` 来自 `skills.disabled` + `skills.platform_disabled.<platform>`，
   平台解析顺序（`_is_skill_disabled`，684-707 行）：显式 `platform` 参数 → `HERMES_PLATFORM` → 会话上下文 `HERMES_SESSION_PLATFORM`。
4. **排除目录**：`EXCLUDED_SKILL_DIRS`（`agent.skill_utils`，`skills_tool.py:86` 导入），实测效果是 `.archive/` 与 `.hub/` 不参与扫描——
   证据：`hermes-home\skills\.archive\` 下有 3 个技能（`cn-university-roster-verification` / `doubao2api-deploy-ops` /
   `feishu-gateway-troubleshooting`），但它们**不出现在** `.skills_prompt_snapshot.json` 的 `skills` 数组里。

### 2.3 目录识别规则

- 单技能目录 = 含 `SKILL.md` 的目录（`iter_skill_index_files(scan_dir, "SKILL.md")`，`skills_tool.py:782`）。
- 类目层级由 `_get_category_from_path()`（`skills_tool.py:592`）推出：相对路径段数 `>= 3` 时取 `parts[0]`。
  因此 `mlops/evaluation/lm-evaluation-harness/SKILL.md` 的 category 是 `mlops`（二级子类不进 category）。
- 支持文件目录白名单 `ALLOWED_SUBDIRS = {"references","templates","scripts","assets"}`（`skill_manager_tool.py:557`）；
  hub 安装侧另有 `_ALLOWED_SUPPORT_DIRS = {"references","templates","scripts","assets","examples"}`（`skills_hub.py:157`，多一个 `examples`）。

### 2.4 扫描缓存

`_SKILLS_CACHE`（`skills_tool.py:102-105`），双键（`with_disabled` / `filtered`），
签名 = `(每目录 max(mtime, 子目录 mtime) + 子目录条目数, disabled frozenset, platform)`，
TTL = **`_SKILLS_CACHE_TTL_SECONDS = 30.0` 秒**。子目录**条目数**进签名是为了兜 NTFS 目录 mtime 惰性更新
（注释 134-137 行明确写了这个 Windows 坑）。

---

## 3. 写侧：`skill_manager_tool.py` 完整校验链

### 3.1 关于「不要捕获清单 / `_DO_NOT_CAPTURE_BLOCK`」——**本版本不存在**

全树检索结果：

```
Select-String -Path _internal\tools\*.py -Pattern '_DO_NOT_CAPTURE|DO_NOT_CAPTURE|do_not_capture'  → 0 命中
递归 _internal 全树（*.py/*.md/*.json/*.txt）同 pattern                                          → 0 命中
```

`skill_manager_tool.py` 内 `capture` 一词仅 6 处，全部指 **ledger 的 before/after 状态快照**
（`capture_before` / `_ledger_before`，行 1946–1967），与「不要捕获」无关。

**替代结论**：本版本用 **4 道分散的「不要写/不要删/不要自动接管」守门**承担该职责。
以下按原文抄出（不改写、不翻译）：

**(A) 背景复盘 fork 的「不得写推断内容」门** — `skill_manager_tool.py:80-88`（`mark_background_review_skill_read` docstring）

> The autonomous review fork is allowed to evolve skills, but it must not
> patch or rewrite content it has only inferred from the transcript.  The
> skill_view tool calls this after returning file content to the model; write
> paths below require the corresponding target path to be present when the
> current origin is ``background_review``.

**(B) 背景复盘 fork 的「外部归属只读」门** — `skill_manager_tool.py:343-349`（`_background_review_write_guard` docstring）

> Refuse autonomous curator writes to externally owned skills.
>
> Foreground agents may still perform user-directed edits to external,
> bundled, or hub-installed skills. The background review fork is different:
> it is autonomous lifecycle maintenance, so its write surface is restricted
> to local curator-owned sediment.

**(C) curator 合并轮的「禁止无去处的裸删」门（fail-closed）** — `skill_manager_tool.py:535-547`（返回给模型的拒绝原文）

> Refusing background curator delete of skill '{name}': the
> consolidation pass may only archive a skill it has absorbed into
> an umbrella. Pass absorbed_into=<umbrella> (the umbrella must
> already exist) to record a verified consolidation. Pruning a
> skill with no forwarding target is not permitted here — the
> deterministic inactivity prune handles staleness archival
> separately. Keeping '{name}' active.

**(D) 前台写入不归 curator 所有的「不得推断归属」原则** — `tools/skill_provenance.py:3-5`

> The curator only consolidates/prunes skills it autonomously created via the
> background self-improvement review fork. Skills a user asks a foreground
> agent to write belong to the user and must never be auto-curated.

**(E) 受保护内建技能永不触碰** — `skill_usage.py:58-66`

> Load-bearing bundled built-ins the curator must NEVER archive or consolidate,
> regardless of ``curator.prune_builtins``, pin state, or LLM judgment. These
> back advertised UX paths; silently archiving one turns its slash command
> into "Unknown command" with no signal to the user.
> …
> (``plan`` used to live here; it is now a first-class built-in command with
> no skill on disk, so the set is currently empty.)

> **实测值**：`PROTECTED_BUILTIN_SKILLS: Set[str] = set()` —— 本版本该集合为**空**。

### 3.2 写入通道与 staging / 审批门

| 项 | 值 | 证据 |
|---|---|---|
| 门控模块 | `tools/write_approval.py`，配置键 `write_approval`（`CONFIG_KEY = "write_approval"`），子系统常量 `MEMORY="memory"` / `SKILLS="skills"` | `write_approval.py:58-67` |
| 默认值 | `false`（默认自由写入）；`true` 时需审批 | `write_approval.py` 模块 docstring |
| staging 落盘 | `<HERMES_HOME>/pending/skills/<id>.json`（`_pending_dir(subsystem)`，110 行） | 同上 docstring |
| 触发的 6 个 action | `{"create","edit","patch","delete","write_file","remove_file"}` | `_apply_skill_write_gate`，`skill_manager_tool.py:1527` |
| 三种决策 | `decision.allow` → 直通；`decision.blocked` → 直接拒绝；否则 **stage** 并返回 `{"success":true,"staged":true,"pending_id":…,"gist":…}` | 1537-1555 行 |
| gist 生成 | `wa.skill_gist(action, name, content, file_path, old_string, new_string)`（`write_approval.py:388`） | 1546-1552 行 |
| 审批回放 | `/skills approve` → `apply_skill_pending(payload)`，用 `_skill_gate_bypass` ContextVar 置 True 重入 `skill_manage`，避免二次 staging | 1558-1578 行 |
| 技能 vs 记忆的差异 | 记忆可 inline 弹窗审批；**技能一律 stage**（100KB SKILL.md 无法 inline 审阅） | `write_approval.py` docstring：「skill writes always stage (too big to eyeball mid-loop)」 |
| 本机现状 | `approvals.mode: 'off'`（`hermes-home\config.yaml`）→ 门控实际不生效 | — |

### 3.3 `skill_manage` 总管线（有序）

`skill_manage()`（`skill_manager_tool.py:1900`）按以下顺序执行，**任一步返回即中止**：

1. `_skill_lookup_path_error(name)` 式的路径校验（`skills_tool.py:206` 同源）
2. `_background_review_preflight(action, name)`（491 行）→ 背景复盘 fork 的预检
3. **审批门** `_apply_skill_write_gate(...)`（1937-1944）→ 可能 stage 或 block
4. **ledger 前置快照** `_ledger.capture_before(_ledger_before_dir, complete_package=(action=="delete"), skill=name)`（1961-1965）
   —— 注释明确「Telemetry, not a gate: failures here must NEVER block the mutation」
5. 按 action 分派（1969-2015）
6. 成功后：`_ledger.record_mutation(...)`（2033）→ `clear_skills_system_prompt_cache(clear_snapshot=True)`（2043-2044）
   → `skill_usage.bump_patch/forget/record_created`（2053+）

批量入口 `_skill_manage_batch`（1585 行）：`_BATCH_OP_ACTIONS={"create","patch","write_file","remove_file"}`，
`_BATCH_MAX_OPS = 20`，**delete 只能作为唯一 op**（1599-1601），任一步失败回滚所有被触及的技能。

### 3.4 create / edit / patch / delete 各自的校验链

**create** `_create_skill()`（981 行）：
```
_validate_name(name)                      # 长度≤64 + VALID_NAME_RE
_validate_category(category)              # 单段目录名，无 / 或 \
_validate_frontmatter(content, new_skill=True)   # 含 60 字符 description 预算
_validate_content_size(content)           # ≤100_000 字符
_find_skill(name)  → 已存在则拒绝「A skill named 'x' already exists at …」
mkdir + atomic_write_text(create_mode=0o644)
_security_scan_skill(skill_dir)           # 失败则 shutil.rmtree 回滚
_add_description_prompt_preview()
_attach_lint_findings()                   # 仅 advisory，永不阻断
```

**edit** `_edit_skill()`（1080 行）：`_validate_frontmatter(content)`（**new_skill=False**，不查 60 预算）→
`_validate_content_size` → `_find_skill` → `_org_mirror_write_guard` → `_background_review_write_guard`

**patch** `_patch_skill()`（1142 行）：目标文件必须已存在；`old_string` 匹配语义与 `patch` 工具一致；
`content` 与 `old_string/new_string` **互斥**（1984-1989 行报错：`Pass EITHER content … OR old_string/new_string …, not both.`）

**delete** `_delete_skill()`（1279 行）——最长的守门链：
```
_find_skill(name)                          → 不存在报 _skill_not_found_error
_org_mirror_write_guard(name, path, "delete")
_background_review_write_guard(name, path, "delete")   # 背景 fork + pinned → 拒绝
_curator_consolidation_delete_guard(name, absorbed_into)  # 背景 fork 无 absorbed_into → fail closed
_pinned_guard(name)                        # ESSENTIAL_SKILLS 或 pinned → 拒绝
[若 absorbed_into 非空] 目标必须存在且 ≠ 自身
_validate_delete_target(skill_dir)         # ① 必须严格位于某 skills root 内
                                           # ② 不能是 skills root 本身
                                           # ③ 不能是 symlink / junction
[is_background_review()] → skill_usage.archive_skill(name)   # curator 轮：可恢复归档，不 rmtree
[否则] shutil.rmtree(skill_dir)            # 前台用户删除：硬删除
[清理空类目目录，但不动 skills root]
```

`_validate_delete_target`（236-294 行）docstring 直接引用了 Kilo Code issue #11227（递归删除抹掉用户工作目录）作为威胁模型。

**pinned 语义不对称**（重要）：
- 前台 `_pinned_guard`（297 行）：pin **只阻止删除**，允许 patch/edit。
- 背景 `_background_review_write_guard`（357-374 行）：pin **阻止一切写入**，注释理由是「there is no user in the loop to consent to an edit here」（issue #25839）。

### 3.5 `_validate_frontmatter` 硬校验（603-657 行，逐条）

| # | 规则 | 失败文案 |
|---|---|---|
| 1 | 内容非空 | `Content cannot be empty.` |
| 2 | 容忍并剥离 UTF-8 BOM | （`content.lstrip("\ufeff")`，618 行） |
| 3 | 必须以 `---` 开头 | `SKILL.md must start with YAML frontmatter (---). See existing skills for format.` |
| 4 | 必须有闭合 `\n---\n` | `SKILL.md frontmatter is not closed. Ensure you have a closing '---' line.` |
| 5 | 必须能 `yaml.safe_load` | `YAML frontmatter parse error: {e}` |
| 6 | 必须是 mapping | `Frontmatter must be a YAML mapping (key: value pairs).` |
| 7 | 必须有 `name` | `Frontmatter must include 'name' field.` |
| 8 | 必须有 `description` | `Frontmatter must include 'description' field.` |
| 9 | `len(description) ≤ 1024` | `Description exceeds 1024 characters.` |
| 10 | **仅 create 路径**：`len(description) ≤ SKILL_PROMPT_DESC_LIMIT(60)` | `Description is N chars — new skills must fit the 60-char system-prompt budget (one sentence, trigger first, ends with a period). The skill index truncates longer descriptions to 57 chars + '...', destroying the routing signal. Move detail into the skill body.` |
| 11 | 闭合 `---` 之后 body 非空 | `SKILL.md must have content after the frontmatter (instructions, procedures, etc.).` |

> 第 10 条反向证明了 **`SKILL_PROMPT_DESC_LIMIT = 60`、索引截断点 = 57 字符 + `"..."`**。

### 3.6 数值上限全集（`skill_manager_tool.py`）

| 常量 | 值 | 行号 | 用途 |
|---|---|---|---|
| `MAX_NAME_LENGTH` | 64 | 197 | 技能名 / 类目名长度 |
| `MAX_DESCRIPTION_LENGTH` | 1024 | 198 | description 硬上限 |
| `MAX_SKILL_CONTENT_CHARS` | **100_000**（注释：`~36k tokens at 2.75 chars/token`） | 550 | 单文件字符上限 |
| `MAX_SKILL_FILE_BYTES` | **1_048_576**（1 MiB） | 551 | 支撑文件字节上限 |
| `ALLOWED_SUBDIRS` | `{references, templates, scripts, assets}` | 557 | 支撑文件目录白名单 |
| `_BATCH_MAX_OPS` | 20 | 1582 | 单次批量操作上限 |
| `_SYNC_PUSH_DEBOUNCE_S` | 5.0 | 1855 | 写后同步推送去抖秒数 |

### 3.7 AST 审计（`skills_ast_audit.py`，5196 B）

**定位**：`hermes skills audit --deep`，**opt-in 诊断，不是安全门**。docstring 原文：
> AST-level deep audit for skill Python files — opt-in diagnostic, not a security gate.
> … Every pattern flagged here has legitimate uses; findings are hints for human review, not verdicts.

| pattern_id | 触发条件 | 说明 |
|---|---|---|
| `dynamic_import` | `importlib.import_module(...)` | loads arbitrary modules at runtime |
| `dynamic_import_computed` | `__import__(<非字面量>)` | `__import__` with non-literal module name |
| `dynamic_getattr` | `getattr(obj, <非字面量>)` | `getattr` with non-literal attribute name |
| `dict_access` | `obj.__dict__[<非字面量>]` | dynamic attribute access |

忽略目录 `_IGNORED_DIRS = {"__pycache__", ".venv", "venv", "node_modules"}`（22 行）。
入口：`ast_scan_path(path)`（84 行）→ `format_ast_report(findings, skill_name)`（118 行）。

### 3.8 linter 规则清单（`skill_linter.py`，12 条 / 2 条 ERROR）

**定位**：advisory，永不硬拒（硬拒在 `_validate_frontmatter`）。入口 `lint_content(content, *, skill_dir)`（374 行）按固定顺序串起 10 个检查函数。

| # | rule id | 级别 | 检查函数 | 内容 | 阈值/取值 |
|---|---|---|---|---|---|
| 1 | `name-format` | **ERROR** | `_check_name_format`(139) | name 必须匹配 `[a-z0-9][a-z0-9_-]*` | 正则 |
| 2 | `name-dir-mismatch` | **ERROR** | `_check_name_matches_dir`(119) | frontmatter name 必须与目录名**完全一致** | 字符串相等 |
| 3 | `description-length` | WARNING | `_check_description`(155) | 超 `SKILL_PROMPT_DESC_LIMIT`(60) 即告警「index truncates past 60 chars + '...', losing routing signal」 | 60 |
| 4 | `description-marketing` | WARNING | 同上 | 禁营销词 | `_MARKETING_WORDS = (powerful, comprehensive, seamless, advanced, cutting-edge, state-of-the-art, revolutionary, robust)`（8 个，60-69 行） |
| 5 | `missing-metadata` | WARNING | `_check_metadata_block`(187) | 缺 `version`/`author`/`license`、缺 `metadata.hermes`、缺 `metadata.hermes.tags` | 4 种触发 |
| 6 | `author-caps` | WARNING | 同上 | author 写成 hermes/agent/hermes agent（小写）时提示应为 `Hermes Agent` | 白名单比较 |
| 7 | `shell-utility-reference` | WARNING | `_check_shell_utilities`(230) | 正文（**剥离 fenced code block 后**）出现 `` `grep` ``/`` `cat` `` 等，应改为原生工具名 | `_SHELL_UTIL_TO_TOOL`：grep/rg→search_files、cat/head/tail→read_file、sed/awk→patch、find→search_files(target='files')、ls→search_files(target='files')（9 个） |
| 8 | `missing-section` | WARNING | `_check_sections`(249) | 必须有 `## When to Use`（大小写两种） | `_EXPECTED_SECTIONS` |
| 9 | `dangling-reference` | WARNING | `_check_reference_links`(262) | body 里 `references|templates|assets/...` 链接在磁盘不存在（`scripts/` 豁免） | 跳过含 `*` 或以 `/` 结尾者 |
| 10 | `platforms-gating` | WARNING | `_check_platforms_gating`(291) | `scripts/` 下 `.py/.sh/.bash` 含 POSIX-only 原语但未声明 `platforms:` | `_POSIX_PRIMITIVES = (fcntl, termios, os.setsid, signal.SIGKILL, osascript, /proc/, apt-get, systemctl)`（8 个） |
| 11 | `platforms-value` | WARNING | `_check_platform_list_valid`(344) | `platforms` 值非法 | 合法集 `{linux, macos, windows, darwin}` |
| 12 | `forbidden-file` | WARNING | `_check_forbidden_files`(327) | 技能目录里不该出现的脚手架文件 | `_FORBIDDEN_FILES = (README.md, CHANGELOG.md, install.sh, .env, .env.example, .gitignore)`（6 个） |

### 3.9 写入后的安全扫描（`_security_scan_skill`，148-172 行）

- 默认**关闭**：`skills.guard_agent_created` 默认 `False`（`_guard_agent_created_enabled`，129-145 行）。
  docstring 原文理由：「Off by default because the agent can already execute the same code paths via terminal() with no gate,
  so the scan adds friction without meaningful security.」
- 开启时：`scan_skill(skill_dir, source="agent-created")` → `should_allow_install(...)`；
  `allowed is False` 或 `allowed is None`（ask 判决）都当作错误返回给模型，并**回滚已创建的目录**。

---

## 4. 读侧与渐进披露

### 4.1 三层模型（`skills_tool.py:9-12` docstring 原文）

> Inspired by Anthropic's Claude Skills system with progressive disclosure architecture:
> - Metadata (name ≤64 chars, description ≤1024 chars) - shown in skills_list
> - Full Instructions - loaded via skill_view when needed
> - Linked Files (references, templates) - loaded on demand

| 层 | 注入内容 | 载体 |
|---|---|---|
| Tier 1 | `name` + `description`（+ `category` / `origin` / 路径） | 系统提示的技能索引 / `skills_list()` |
| Tier 2 | 完整 SKILL.md 正文 | `skill_view(name)` |
| Tier 3 | `references/` `templates/` 等支撑文件 | `skill_view(name, file_path="references/x.md")` |

### 4.2 提示里到底注入什么、多少预算

- **注入字段**：`name`、`description`，以及 `.skills_prompt_snapshot.json` 里可见的 `category`、`platforms`、`conditions`。
  `skills_list()` docstring（855 行）：`Returns only name + description to minimize token usage.`
- **description 预算 = 60 字符**，超长在**装配时**截断为 **57 字符 + `"..."`**。
  两处独立证据：
  1. `_validate_frontmatter` 错误文案（`skill_manager_tool.py:649`）：「The skill index truncates longer descriptions to 57 chars + '...'」
  2. `hermes-agent-skill-authoring` SKILL.md：「The system prompt skill index truncates at 57 chars + "..." — the trigger/capability must be self-contained in that window.」
  3. `SKILL_MANAGE_SCHEMA.description`（`skill_manager_tool.py:2115-2117`）：「Keep the description's first 57 chars a self-contained trigger」
- **没有全局 token 预算常量**：裁剪是按**单条 description 60 字符**做的，而不是按总 token 数排序截断。
  `MAX_DESCRIPTION_LENGTH=1024` 只作用于 `skills_list()` 的返回（`skills_tool.py:814-815`），不作用于系统提示索引。
- **按需加载无分页**：`skill_view` 一次返回全文（单文件上限 100 000 字符）。

### 4.3 重复查看去重（省 token 的真实机制）

`_skill_view_with_bump`（`skills_tool.py:2117`）在调用 `skill_view` **之前**先查 `_check_skill_view_dedup(task_id, name, file_path)`：
- 同一 session 已加载过同一文件且磁盘未变 → 返回短 stub，不再重发全文。
- 动机见注释（2126-2127 行）：「production mining: **~286k tokens** of verbatim repeat skill_view content in one 400k-message window」。
- 上限 `_SKILL_VIEW_DEDUP_CAP = 200`（2017 行）；context 压缩时清空（2130 行注释）。

### 4.4 计数联动

`skill_view` 成功后**同时**调 `bump_view()` 和 `bump_use()`（2146-2155 行）。注释：
> A skill_view tool call is the agent actively loading the skill to act on it — that counts as use, not just a browse/view.
> Curator's stale timer keys off last_used_at (see agent/curator.py).

### 4.5 `.skills_prompt_snapshot.json`（112 272 B，实测）

**作用**：技能索引装配快照 + **变更检测清单**。写入/失效点：
- 写：`agent.prompt_builder`（编译进 exe）；`skill_manager_tool.py:2043-2044` 在**每次成功写入**后调
  `clear_skills_system_prompt_cache(clear_snapshot=True)` → 快照被清空重建。
- 因此它 = 「上一次装配技能提示时的输入清单」，用于判断是否需要重新扫描/重新渲染。

**结构（实测 4 个顶层键，`version = 2`）**：

```jsonc
{
  "version": 2,
  "manifest": {                                  // 183 条：相对路径 → "<mtime_ns> <size>"
    "accounts\\account-signup-provisioning\\SKILL.md": "1790172562939966200 10309",
    "apple\\DESCRIPTION.md": "… …"
  },
  "skills": [ /* 165 条 */ ],
  "category_descriptions": { /* 17 条：类目 → 描述 */ }
}
```

`skills[]` 单条结构（实测原文）：

```jsonc
{
  "skill_name": "account-signup-provisioning",
  "category": "accounts",
  "frontmatter_name": "account-signup-provisioning",
  "description": "替用户合规开通需要身份/手机/扫码验证的第三方账号（Gmail/Google、GitHub、各类平台）：买号与账号...",
  "platforms": [],
  "conditions": {
    "fallback_for_toolsets": [],
    "requires_toolsets": [],
    "fallback_for_tools": [],
    "requires_tools": [],
    "session_platforms": []
  }
}
```

要点：
- `manifest` 用 **mtime_ns + size** 做指纹（不是 hash），所以它是**廉价的增量扫描判据**。
- `manifest`(183) > `skills`(165)：差额 = `DESCRIPTION.md` 文件 + 被 `platforms`/`disabled`/`conditions` 过滤掉的技能。
- `conditions.*` 是**条件装载**机制（5 个维度）：`requires_toolsets` / `fallback_for_toolsets` /
  `requires_tools` / `fallback_for_tools` / `session_platforms`——即技能可声明「仅当某工具集可用时装载」或
  「当某工具集不可用时作为回退装载」。这套条件来自 frontmatter（本机 91 个 bundled 技能里没有用到，
  但用户库里有），是 bundled 库**没有**的机制。

---

## 5. 治理与生命周期（curator 层）

### 5.1 状态机（`skill_usage.py:20-26` docstring 原文）

```
Lifecycle states:
    active    -> default
    stale     -> unused > stale_after_days (config)
    archived  -> unused > archive_after_days (config); moved to .archive/
    pinned    -> opt-out from auto transitions (boolean flag, orthogonal to state)
```

代码常量（`skill_usage.py:53-56`）：

```python
STATE_ACTIVE   = "active"
STATE_STALE    = "stale"
STATE_ARCHIVED = "archived"
_VALID_STATES  = {STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED}
```

**`pinned` 不是 state，是正交布尔标志**（存在 `.usage.json` 记录的 `pinned` 字段里），
对**每一次自动迁移**和**每一次 LLM 复盘**都豁免（`background-systems.md:59-60`：
「Pinned skills are exempt from every auto-transition and every LLM review pass.」）。

### 5.2 阈值：可证数值 vs 不可证数值

| 配置键 | 可证内容 | 默认值 |
|---|---|---|
| `curator.prune_builtins` | **默认 `True`**（`skill_usage.py:250-266` 读 `curator.prune_builtins`，`default=True`） | ✅ 可证 |
| `curator.consolidate` | **默认 `False`**（LLM 合并轮 opt-in；`configuration.md:22` 写 `consolidate` (false, opt-in)；CLI `curator run --consolidate` 说明「overriding the config default (off)」） | ✅ 可证 |
| `curator.archive_ttl_days` | 用于 `hermes curator purge`：只删**比该值更旧**的已归档技能 | ⚠ 数值不可证 |
| `curator.stale_after_days` | 「unused > stale_after_days → stale」 | ⚠ **数值不可证** |
| `curator.archive_after_days` | 「unused > archive_after_days → archived」 | ⚠ **数值不可证** |
| `curator.interval_hours` | 后台复盘周期 | ⚠ 数值不可证 |
| `curator.min_idle_hours` | 触发复盘的最小空闲时长 | ⚠ 数值不可证 |
| `curator.enabled` | 总开关 | ⚠ 不可证 |
| `curator.backup.*` | 备份策略 | ⚠ 不可证 |

**唯一可证的天数阈值 = 90 天**，来自 CLI 帮助原文（`hermes curator prune --help`）：
> `--days DAYS  Archive skills idle for at least N days (default: 90)`

以及 `hermes curator prune` 子命令摘要：「Bulk-archive curator-managed skills idle for >= N days (**default 90**)」。

**不可证的根因**：`agent/curator.py` 被压进 exe PYZ（见 §0.1）。已用流式字节扫描验证
`stale_after_days` / `archive_after_days` / `min_idle_hours` 明文 0 命中。

**取证建议（给 Orbit parity 实现时对齐用）**：可执行 `hermes curator status` / `hermes curator run --dry-run`
观察实际阈值，或用 `hermes config get curator`（本次任务限制只允许 `--help`，故未执行）。

### 5.3 锚点取值顺序（**这是本节最关键的可证事实**）

「多久没用」的参照时间戳，取值顺序为**两级**：

**第 1 级（活动锚点）** —— `latest_activity_at(record)`，`skill_usage.py:145-162`：

```python
for key in ("last_used_at", "last_viewed_at", "last_patched_at"):
    raw = record.get(key)
    dt = _parse_iso_timestamp(raw)
    if dt is None: continue
    if latest_dt is None or dt > latest_dt:
        latest_dt = dt; latest_raw = str(raw)
return latest_raw
```

即：**`max(last_used_at, last_viewed_at, last_patched_at)`** —— 三者取最新，不是按顺序短路。

**第 2 级（回退锚点）** —— docstring 明确（147-151 行）：

> "Activity" means a skill was used, viewed, or patched. **Creation time is
> intentionally excluded** so callers can still distinguish never-active skills;
> lifecycle code can fall back to ``created_at`` as its own anchor.

即：**若三个活动时间戳全为空 → 回退 `created_at`**。

**特别说明**：`skills_tool.py:2150` 注释写「Curator's stale timer keys off **last_used_at**」——
这是对「主要锚点」的简述，完整实现是上面三级取 max + created_at 回退。
**`mtime` 不参与**（`skill_usage.py` 全文无 `st_mtime` 用于锚点；`st_mtime` 只出现在 `skills_tool.py` 的扫描缓存签名里）。

时间戳解析容错：`_parse_iso_timestamp`（132-142 行）——`datetime.fromisoformat` 失败返回 `None`；
naive 时间戳强制按 UTC（`parsed.replace(tzinfo=timezone.utc)`）。

### 5.4 `.usage.json` 统计口径

**文件**：`~/.hermes/skills/.usage.json`（sidecar，与 SKILL.md 解耦；本机 38 397 B / **92 条记录**）。

**单条记录 schema** —— `_empty_record()`，`skill_usage.py:643-658`：

| 字段 | 初值 | 含义 | 写入者 |
|---|---|---|---|
| `created_by` | `None` | **curator 接管策略标志**（`"agent"` / `"installed"` / `None`），**不是**作者归属的事实记录 | `record_created` / `record_installed` / `mark_agent_created` |
| `use_count` | 0 | 被**主动使用**次数 | `bump_use` |
| `view_count` | 0 | 被查看次数 | `bump_view` |
| `last_used_at` | `None` | 最近一次使用（ISO8601 UTC） | `bump_use` |
| `last_viewed_at` | `None` | 最近一次查看 | `bump_view` |
| `patch_count` | 0 | 被 patch/edit 次数 | `bump_patch` |
| `patch_generation` | 0 | patch 代数（每次 patch +1） | `bump_patch` |
| `last_reused_patch_generation` | 0 | 最后一次「patch 后被复用」的代数 | `bump_use` |
| `last_patched_at` | `None` | 最近一次 patch | `bump_patch` |
| `created_at` | now | 记录创建时间（第 2 级回退锚点） | `_empty_record` |
| `state` | `"active"` | active/stale/archived | `set_state` |
| `pinned` | `False` | pin 标志（正交于 state） | `set_pinned` |
| `archived_at` | `None` | 归档时间 | `set_state` |

可选字段（按需出现）：`sync`（bool，同步 opt-in，`set_sync` 写入）、`agent_created`（bool 兼容旧记录，
`_is_curator_managed_record` 兼容读取）。

**成功/失败口径 —— 关键结论：`use_count` 里没有成功/失败维度。**

- `bump_use(skill_name, *, task_id=None, session_id=None)`（863 行）：**只做 `use_count += 1` + `last_used_at = now`**，
  无 outcome 参数、无 success/failure 计数、无失败率。
- 唯一带语义的派生量是 **`reuse_after_patch`**（881-888 行）：`reused = use_count > 0`，
  `reuse_after_patch = reused and patch_generation > last_reused_patch_generation`。
  即「技能被 patch 之后是否又被复用」——这是**质量代理指标**，不是成功/失败。
- 派生汇总：`activity_count(record)` = `use_count + view_count + patch_count`（165-173 行）。
- 观测口径 ≠ 治理口径：`bump_view` docstring 原文「Tracks every skill regardless of provenance — built-ins and hub skills included.
  Usage telemetry is observability, not a curation signal.」

**写入时机与并发**：
- 时机：`skill_view` 成功（bump_view + bump_use）；`skill_manage` 成功（bump_patch / record_created / forget）。
- 原子性：`save_usage()`（681 行）= `tempfile.mkstemp` + `f.flush()` + `os.fsync()` + `os.replace`，
  `orjson.dumps(..., OPT_INDENT_2 | OPT_SORT_KEYS)`。
- 锁：`_usage_file_lock()`（89-121 行），Unix 用 `fcntl.flock(LOCK_EX)`，Windows 用 `msvcrt.locking(LK_LOCK, 1)`；
  锁文件 `~/.hermes/skills/.usage.json.lock`（本机实测存在，1 字节）。
- 容错：所有 bump 都是 best-effort，异常只 `logger.debug`，**永不破坏工具调用**。

**本机实测分布**（`.usage.json` 92 条）：
- `state`: `active=89`、`archived=3`、`stale=0`
- `created_by`: `agent=74`、空(`None`)=18 → 即 74 个归 curator 管，18 个「无归属标记」（对应 `hermes curator list-unmanaged`）
- `pinned`: 0

### 5.5 curator 的资格判定（谁可以被自动治理）

`is_curation_eligible(skill_name, skill_path=None)`，`skill_usage.py:454-481`，判定顺序：

```
1. skill_path 在 external_dirs        → False（永不可治理）
2. name ∈ PROTECTED_BUILTIN_SKILLS    → False（永不可治理；本版本该集合为空）
3. is_hub_installed(name)             → False（hub 安装永不可治理）
4. is_bundled(name)                   → 返回 _prune_builtins_enabled()（默认 True → 可治理）
5. 本地目录存在                        → 返回 not is_external_skill_path(dir)
6. 外部目录存在                        → False
7. 其余（无标记的自建技能）             → True
```

**`created_by` 的真实语义**（`_is_curator_managed_record` docstring，484-501 行，issue #67140）：
> NAMING (issue #67140): the on-disk field is ``created_by``, which reads
> like provenance but is consumed as a **curator-management opt-in policy
> flag**. …
> ``created_by: "agent"`` therefore means "curator-managed", NOT "proof the
> agent wrote it".

即：**`created_by: "agent"` = 「curator 可接管」，不是「agent 写的」的证据**。
前台 `skill_manage(create)` **故意不打这个标记**（`skill_manager_tool.py:2056-2059`：`record_created(name, agent_created=is_background_review())`），
因为用户要求创建的技能属于用户。用户可用 `hermes curator adopt` 显式交出。

`adopt_skill(name)`（595 行）是唯一的接管入口，且「Provenance is a DECLARATION, never an inference」——
`list_unmanaged_skill_names()` 只报告，**不允许自动 adopt**（docstring 537-540 行原文）。

### 5.6 归档与恢复（可恢复，永不自动删除）

`archive_skill(skill_name)`，`skill_usage.py:1074-1155`：
1. `_find_skill_dir` 找不到且存在于 external → 拒绝（`external skills are read-only to the curator`）
2. `is_curation_eligible` 不过 → 分别给出 protected / hub / bundled 三种拒绝文案
3. 目标 `~/.hermes/skills/.archive/<skill_dir.name>`；**类目嵌套被压平**；重名冲突追加
   `-YYYYMMDDHHMMSS`（1113-1115 行，`_ARCHIVE_TS_SUFFIX_RE = ^(.+)-\d{14}$`）
4. ledger 前置快照（`complete_package=True`，防「空壳回滚」，issue #96962）
5. `skill_dir.rename(dest)`，跨设备失败则 `shutil.move`
6. 若为 bundled：`add_suppressed_name()` 写入 `.curator_suppressed`，让下次更新重新播种时**保持归档**
7. `set_state(name, STATE_ARCHIVED)` → 写 `archived_at`
8. `_ledger.record_mutation("archive", ...)`

`restore_skill`（1158 行）：移回 `~/.hermes/skills/` **顶层扁平布局，不重建原类目层级**（docstring 明确）；
若与 hub 安装同名则拒绝（避免遮蔽上游版本）。

**「永不自动删除」的强证据**：CLI `curator --help` 原文——
> Archives are recoverable; **auto-deletion never happens**.
> `purge` — Delete archived skills older than `curator.archive_ttl_days` (**explicit only — never automatic**; recorded in the ledger)

### 5.7 `.curator_state` 与运行时机

本机 `.curator_state`（348 B）实测全文：

```json
{
  "last_report_path": null,
  "last_run_at": "2026-08-09T12:51:57.427550+00:00",
  "last_run_duration_seconds": null,
  "last_run_summary": "deferred first run — curator seeded, will run after one interval; use `hermes curator run --dry-run` to preview now",
  "last_run_summary_shown_at": null,
  "paused": false,
  "run_count": 0
}
```

可读出：**首次运行是延迟的**（seed-on-first-sight 之后要等一个 `interval_hours` 周期）；
本机 `run_count = 0`（从未真正跑过）、`paused = false`、`last_report_path` 为空。

运行时机（`skills_sync_client.py:11-13` 注释）：periodic pull hook 挂在「**the curator tick sites**」——
即 curator 的定时 tick 同时驱动技能同步拉取。手动触发：`hermes curator run [--sync|--background|--dry-run|--consolidate]`。

**成本**：`background-systems.md:61-65` 原文——
> the deterministic inactivity/prune sweep runs **for free**. The aux-model "consolidate overlapping skills into umbrellas" pass is
> **off by default** … Routine background curation costs **zero tokens**.

### 5.8 `skill_ledger.py` —— 逐变更审计账本

- 路径：`~/.hermes/skills/.curator_ledger.jsonl`（append-only JSONL；本机 34 784 B / **14 条**）
- blob 库：`~/.hermes/.curator_backups/blobs/`（**内容寻址 sha256 去重**，不是每次打 tarball）
- 角色常量：`ACTOR_CURATOR="curator"` / `ACTOR_AGENT="agent"` / `ACTOR_USER="user"`（45-47 行，`_VALID_ACTORS`）
- **不是门**：「The ledger is TELEMETRY, NOT A GATE: a ledger failure must never block the mutation it describes.」
  唯一例外：`rollback_entry` 在自己回滚前的安全捕获失败时 **FAIL CLOSED**
- 记录字段全集（实测 14 条取并集）：`id`（12 位 hex）、`ts`、`actor`、`action`、`skill`、
  `evidence`（含 `session_id`、`file_path`、`absorbed_into`、`archived`）、
  `before[]`、`after[]`（每项 `{path, sha256}`）

实测一条（简化）：

```json
{"id":"e3c5f030224b","ts":"2026-10-23T13:32:13.552348+00:00","actor":"curator","action":"patch",
 "skill":"local-fullstack-verification",
 "evidence":{"session_id":"20261023_210700_e96bc0"},
 "before":[{"path":"…\\devops\\local-fullstack-verification\\SKILL.md","sha256":"da1cbdd2…"}],
 "after": [{"path":"…\\devops\\local-fullstack-verification\\SKILL.md","sha256":"48daa0cb…"}]}
```

- `_PACKAGE_RESTORE_ACTIONS = frozenset({"delete","archive","purge"})`（59 行）：这三个 action 必须恢复**完整包**
  （因为 consolidation 会先把支撑文件迁出树，只捕磁盘会拿到空壳，issue #96962）
- 单条回滚：`rollback_entry(entry_id)` → `hermes curator rollback <entry-id>`；
  整树回滚：`hermes curator rollback [--id BACKUP_ID]`（读 `~/.hermes/.curator_backups/` 的 tar.gz，
  `_BACKUP_ID_RE = ^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z(-\d{2})?$`）
- 备份时机：**每次真实 run 之前自动**打 tar.gz（CLI 原文：「curator also does this automatically before every real run」）

### 5.9 `skills_guard.py` —— 安装前守门

**定位**：外部来源技能的**安装前静态扫描**。docstring 原文：「Skills Guard is in-process heuristics
("useful — not boundaries")」（引 SECURITY.md §2.4）。

**信任等级 → 安装策略**（`INSTALL_POLICY`，68-78 行）：

| trust_level | safe | caution | dangerous |
|---|---|---|---|
| `builtin` | allow | allow | allow |
| `trusted` | allow | allow | **block** |
| `community` | allow | **block** | **block** |
| `agent-created` | allow | allow | **ask** |

`VERDICT_INDEX = {"safe":0, "caution":1, "dangerous":2}`（80 行）。
`should_allow_install(result, force=False)`（971-1012 行）返回 `(True|False|None, reason)`，
`None` = 「需用户确认」。**`--force` 不能覆盖 community/trusted 的 dangerous 判决**（989、1002-1008 行）。

**受信仓库**（`TRUSTED_REPOS`，57-67 行，4 个）：
`openai/skills`、`anthropics/skills`、`huggingface/skills`、`NVIDIA/skills`
（NVIDIA 额外要求每项带签名 `skill.oms.sig` + 治理卡 `skill-card.md`）。

**威胁模式库**：`THREAT_PATTERNS`（184-666 行），实测 **107 条**：

| severity | 条数 | | category | 条数 |
|---|---|---|---|---|
| critical | 36 | | exfiltration | 25 |
| high | 43 | | injection | 19 |
| medium | 24 | | persistence | 19 |
| low | 4 | | obfuscation | 14 |
| | | | network | 9 |
| | | | destructive | 8 |
| | | | execution | 6 |
| | | | traversal | 5 |
| | | | mining | 2 |

代表性 pattern_id：`env_exfil_curl`、`env_exfil_wget`、`env_exfil_fetch`、`env_exfil_httpx`、`env_exfil_requests`、
`read_secrets_file`、`dns_exfil`、`md_image_exfil`、`md_link_exfil`、`prompt_injection_ignore`、`sys_prompt_override`、
`role_hijack`、`translate_execute`、`jailbreak_dan`、`destructive_root_rm`、`destructive_home_rm`、`format_filesystem`、
`persistence_cron`、`ssh_backdoor`、`sudoers_mod`、`agent_config_mod`、`hermes_config_mod`、`other_agent_config_mod`、
`reverse_shell`、`bash_reverse_shell`、`tunnel_service`、`base64_decode_pipe`、`echo_pipe_exec`、`python_subprocess`、
`python_os_system`、`path_traversal_deep`、`system_passwd_access`、`crypto_mining`。

结构常量：`MAX_FILE_COUNT = 50`、`MAX_TOTAL_SIZE_KB = 5120`（5 MB，仅告警不阻断）、
`MAX_SINGLE_FILE_KB = 256`、`SCANNER_VERSION = "skills-guard-v2"`（48、673-675 行）。
忽略文件：`.skillignore` / `.clawhubignore`（`_SKILL_IGNORE_FILENAMES`），但 `_NEVER_IGNORABLE = {"SKILL.md"}`。

**已知盲区（docstring 自陈）**：程序化写入（Python `open(...,'w')`、`pathlib.write_text()`、`os.replace()`、
`shutil.copy*`、Node `fs.writeFileSync()`）针对 agent 配置文件只产生低危 `*_ref` finding，**不打分**，
因为「Static regexes cannot reliably tie such a call to the config-file destination」。

### 5.10 `skillevaluator_scan.py`（9 622 B）

docstring 定位：扫描技能内容的评估器；入口与 `skills_ast_audit` 同类的辅助诊断（非门控）。
本文件仅 9.6 KB，未含独立阈值常量，作用是为 `hermes skills audit` 提供 evaluator 侧的扫描输出。

---

## 6. 分发与同步

### 6.1 `skills_sync.py` —— **bundled 播种**（不是跨设备同步）

文件头 docstring 原文即协议说明：

> Skills Sync -- Manifest-based seeding and updating of bundled skills.
> Manifest format (v2): each line is `"skill_name:origin_hash"` where origin_hash is the **MD5** of the bundled skill
> at the time it was last synced to the user dir. Old v1 manifests (plain names without hashes) are auto-migrated.
>
> Update logic:
> - **NEW** skills (not in manifest): copied to user dir, origin hash recorded.
> - **EXISTING** skills (in manifest, present in user dir):
>   * If bundled still matches origin hash: no update → skip without reading the user copy.
>   * If bundled changed and user copy matches origin hash: safe to update.
>   * If bundled changed and user copy differs: user customized it → **SKIP**.
> - **DELETED by user** (in manifest, absent from user dir): respected, not re-added.
> - **REMOVED from bundled** (in manifest, gone from repo): cleaned from manifest.

- manifest 文件：`~/.hermes/skills/.bundled_manifest`（本机 4 158 B），实测格式 `name:32位hex`（= MD5），如
  `airtable:3b1f4e4c0e6aac15f2fd7f55e151bda9`
- 关键函数：`sync_skills(quiet=False)`(712)、`_content_hash`(325)、`_dir_hash`(291)、
  `_is_tracked_user_modification(origin_hash, user_hash)`(1162)、
  `list_user_modified_bundled_skills()`(1174)、`diff_bundled_skill(name)`(1230)、
  `reset_bundled_skill(name, restore=False)`(1063)
- 退出开关：`NO_BUNDLED_SKILLS_MARKER = ".no-bundled-skills"`（106 行）→ `set_bundled_skills_opt_out` / `is_bundled_skills_opt_out`
- 受保护名单：`_essential_names()`（109 行）→ `agent.skill_utils.ESSENTIAL_SKILLS`（如 `hermes-agent`）
- 冲突解决 = **「用户改过就不覆盖」**（不是三方合并、不是时间戳），且用户删除被尊重（不复活）

### 6.2 `skills_sync_client.py` —— 真正的跨设备同步（Skill Sync）

**协议**：content-addressed 对象模型 + CAS ref，语义近似 git。

| 常量 | 值 | 行号 |
|---|---|---|
| `WIRE_VERSION` | `"1"`（Milestone 1 冻结） | 75 |
| `DEFAULT_MAX_OBJECT_BYTES` | **26 214 400**（25 MiB） | 76 |
| `KIND_BLOB` / `KIND_TREE` / `KIND_COMMIT` | `"blob"` / `"tree"` / `"commit"` | 79-81 |
| `MODE_FILE` / `MODE_EXEC` / `MODE_DIR` | `"file"` / `"exec"` / `"dir"` | 84-86 |
| `SYNC_MANIFEST_ENTRY_NAME` / `TYPE` | `"sync-manifest"` | 113-114 |
| `SYNC_MANIFEST_VERSION` | `1` | 115 |
| `DEFAULT_SYNC_BASE_URL` | `https://gateway-gateway.nousresearch.com` | 305 |
| `_ORG_CAS_MAX_ATTEMPTS` | **5** | 1779 |
| `SyncClient.timeout` 默认 | **30.0** 秒 | 771 |
| 同步推送去抖 | **5.0** 秒（`_SYNC_PUSH_DEBOUNCE_S`，`skill_manager_tool.py:1855`） | — |

**端点**：`SyncClient._url(path)` → `{base}/v1/sync/{path}`（780-781 行），含
`GET /v1/sync/capabilities`（免认证）、`GET /v1/sync/refs?prefix=`、`put_objects`、`cas_ref`、`get_refs`。

**ref 命名空间**：`refs/user/<owner>/HEAD`（`user_head_ref`）、
`refs/user/<owner>/conflict/<n>`（`user_conflict_ref`）、`refs/org/<org_id>/HEAD`（`org_head_ref`）。

**认证与门控**：
- Bearer token 来自 `resolve_nous_runtime_credentials()`。
- **准入闸**：客户端同步在 pre-launch 阶段是 **INERT**（完全空转），除非登录用户带
  `NOUS_ADMIN_CLAIM = "tool_gateway_admin"` 声明（216 行）。JWT payload **不做签名校验**（服务端复核）。
- `sync_feature_enabled()`(391)、`sync_org_auto_propose()`(403)、`sync_default_opt_in()`(422) 三个开关。

**同步单元与 opt-in**：
- 单元 = **单个技能的 tree**（`_skill_trees_of_root`，1187 行），根 tree 由各类目 tree 组装。
- **opt-in 默认关闭**：本地意图存 `.usage.json` 的 `sync` 字段（`set_sync`，`skill_usage.py:1036`）；
  **权威状态**是同步平面上的 `sync-manifest` blob（`refs/user/<owner>/HEAD` 根级），
  push 写、pull 反向reconcile → 「一台设备 opt-in，其他设备自动 opt-in」。
- 资格：`is_sync_eligible(skill_name)`(452) —— 只允许 `~/.hermes/skills/` 下的 **agent-created + user-authored**；
  bundled 与 hub-installed **被排除**。

**冲突解决（三方合并，逐技能）** —— `_merge_skill(base, ours, theirs)`，1451-1468 行：

```python
if ours == theirs:            return "either" if ours is not None else "none"
ours_changed   = ours   != base
theirs_changed = theirs != base
if ours_changed and not theirs_changed: return "ours"
if theirs_changed and not ours_changed: return "theirs"
return "overlap"          # 两边都改且不同
```

**判定粒度 = 整技能 tree 哈希**（不是文件级、不是行级）。
`_resolve_push_conflict`（1356 行）流程：
1. push 遇 409（`SyncConflict`，携带 `actual` head；`None` 表示 ref 不存在需按 create 重试）
2. 取 `theirs_root` / `base_root`，逐技能三方判定
3. **有 overlap** → 生成新 conflict ref `refs/user/<owner>/conflict/<n>`（`_next_conflict_index` 取 max+1），
   **本地保留 ours**，theirs 留服务端，返回 `{"ok": false, "conflict": true, "overlapping_skills": [...]}`，
   提示「Resolve out-of-band (hermes sync / NAS UI)」
4. **无 overlap** → 构造 merge commit（parents = `[actual_head, our_commit]`）再 CAS 一次；
   再失败则返回 `merge CAS lost again … retry sync`

**tie-break 规则总结**：无「时间戳新旧」比较；同内容 → `either`；单边改 → 该边；双边改且不同 → **人工介入**（overlap）。

**组织共享**（`_org` 目录）：
- `ORG_DIR_NAME = "_org"`（1741）；`pull_org_skills`(1805)、`propose_skill`(2015)、
  `list_locally_modified_org_skills`(1972)、`org_skill_is_locally_modified`(1958)
- 本地基线 `_org_baseline_path(org_id)`，用 `_skill_dir_fingerprint` 判本地是否被改
- 权限：admin 直接入库；非 admin 变成待审 proposal（CLI `sync propose` 原文）

**状态文件**：`_sync_state_path()`(946) / `_legacy_sync_state_path()`(950)，含 `head` / `root`。

### 6.3 `skills_hub.py` —— 远端索引 / 搜索 / 安装

**目录布局**（`_hub_dir()` 等，69-101 行）：
```
~/.hermes/skills/.hub/
├── lock.json          # HubLockFile：已安装记录 {"version":1,"installed":{…}}
├── quarantine/        # 下载隔离区，扫描通过后才 move 进 skills/
├── audit.log          # append-only 安装审计
├── taps.json          # 自定义 GitHub tap 列表
└── index-cache/       # 远端索引缓存（TTL 1h）
```
本机实测：`lock.json = {"version": 1, "installed": {}}`、`taps.json = {"taps": []}`、
`audit.log` 0 字节、`index-cache\hermes-index.json` 0 字节 → **从未安装过 hub 技能**。

**远端源（9 类 Source 实现）**：
| 类 | 行号 | 远端 |
|---|---|---|
| `HermesIndexSource` | 4597 | `HERMES_INDEX_URL = https://hermes-agent.nousresearch.com/docs/api/skills-index.json`，`HERMES_INDEX_TTL = 6*3600`（6 小时） |
| `GitHubSource` | 670 | `https://api.github.com/repos/{repo}/contents/…`、`/git/trees/{branch}` |
| `WellKnownSkillSource` | 1378 | well-known agent skill 端点 |
| `UrlSource` | 1605 | 任意直链 `SKILL.md` |
| `SkillsShSource` | 1799 | `https://skills.sh`、`https://www.skills.sh/sitemap.xml` |
| `ClawHubSource` | 2375 | `https://clawhub.ai/api/v1` |
| `LobeHubSource` | 3173 | `https://chat-agents.lobehub.com/index.json` |
| `BrowseShSource` | 3333 | `https://browse.sh/api/skills` |
| `OptionalSkillSource` | 3508 | 官方 `optional-skills/` |

`INDEX_CACHE_TTL = 3600`（56 行，1 小时）。

**GitHub tap 提供方标签**（`GITHUB_TAP_PROVIDERS`，626-635 行，7 个）：
`openai/skills→OpenAI`、`anthropics/skills→Anthropic`、`huggingface/skills→HuggingFace`、
`nvidia/skills→NVIDIA`、`voltagent/awesome-agent-skills→VoltAgent`、`garrytan/gstack→gstack`、`minimax-ai/cli→MiniMax`。
（运行时索引把所有 GitHub tap 折叠成 `source="github"`，靠 `extra.provider` 保留身份。）

**搜索/合并算法**（`unified_search`，4926 行）：
1. `parallel_search_sources(sources, query, source_filter, overall_timeout=30)` —— **整体超时 30 秒**
2. provider 过滤（`--source nvidia` 之类作用在合并集上）
3. **按 `identifier` 去重**（不是 name——同名不同站的 browse-sh 技能会被错误折叠），保留 `trust_level` 更高者
   （`_TRUST_RANK = {"builtin":2, "trusted":1, "community":0}`）
4. **按 trust rank 稳定排序后再截断**，理由原文：「the limit cut must not drop a builtin/official catalog entry
   because a high-volume community source (skills.sh mirrors every repo) happened to finish first」
5. 返回 `deduped[:limit]`；`browse` 默认 `--size 20`

**安装流程**（`quarantine_bundle`(4170) → `scan_skill` → `install_from_quarantine`(4221)）：

```
1. _validate_skill_name(skill_name) / _validate_install_parent_path(category)
2. quarantine 路径必须 is_relative_to(_quarantine_dir())
3. _resolve_lock_install_path(install_rel_path, name)  ← 拦 symlink 重定向
4. 拒绝「把技能嵌进已有技能目录」（沿祖先链找 SKILL.md，issue #75983 的兄弟 case）
5. 目标已存在：
   - 是文件不是目录        → 拒绝
   - 是类目桶（无 SKILL.md 且含子技能）→ 拒绝（issue #75983：会 rmtree 抹掉同桶兄弟）
   - 是已有技能            → shutil.rmtree 后覆盖
6. SKILL.md > 100_000 字节 → 仅 warning
7. quarantine 内任何 symlink → ValueError 拒绝（防把树外内容搬进 skills/）
8. shutil.move 进 install_dir
9. HubLockFile.record_install(name, source, identifier, trust_level, scan_verdict,
                              skill_hash=content_hash(dir), install_path, files, metadata, scan_provenance)
10. append_audit_log("INSTALL", …)
11. skill_usage.record_installed(name)   → created_by="installed", state=active
```

**安全设施**：`_ssrf_safe_http_get`(405) / `_guarded_http_get`(413)（SSRF 防护）、
`_REDIRECT_STATUS_CODES = {301,302,303,307,308}`、`_MAX_SKILL_FETCH_REDIRECTS = 5`（124-125 行）、
`_validate_bundle_rel_path`(452)（路径穿越）、`_is_path_redirect`(366)。

**更新/卸载**：`check_for_skill_updates`(4429)、`bundle_content_hash`(4392)、`uninstall_skill`(4362)（拒绝删除 builtin）。

### 6.4 bundles（技能别名集合 / slash 命令）

**两个不同的 bundle 概念，不要混淆：**

| 概念 | 位置 | 含义 |
|---|---|---|
| `SkillBundle`（dataclass） | `skills_hub.py:147-155` | **下载得到的技能文件包**（`name`, `files{相对路径→内容}`, `source`, `identifier`, `trust_level`, `metadata`），是 hub 的传输单元 |
| **CLI `hermes bundles`** | `hermes_cli`（编译进 exe） | **技能别名集合**：一个 `/<bundle>` slash 命令一次装载多个技能 |

CLI 帮助原文（`hermes bundles --help`）：
> Skill bundles let you load several skills under one **slash command**. `/<bundle>` from the CLI or gateway loads every
> referenced skill at once.

- 子命令 5 个：`list` / `show <name>` / `create <name>` / `delete <name>` / `reload`
- `create` 选项：`--skill/-s SKILL`（可重复）、`--description/-d`、`--instruction/-i`（**额外指导，前置拼接**到装载的技能内容）、`--force/-f`
- `reload`：「Re-scan the **bundles directory** and report changes」→ bundle 是磁盘上的目录/文件集合，可热重载
- 本机实测：`hermes-home` 下**不存在** bundles 目录（`Test-Path …\bundles` = False），即**未创建任何 bundle**；
  `skills_hub` 里也没有 bundle 的定义常量 → bundle 定义在 `hermes_cli`（不可读）

---

## 7. CLI 面（全部 `--help` 实测，exe 仅以 `--help` 调用）

### 7.1 `hermes skills --help`（21 个子命令）

```
usage: hermes skills [-h]
                     {trust,untrust,browse,search,install,inspect,list,check,update,audit,uninstall,
                      reset,list-modified,diff,opt-out,opt-in,repair-official,publish,snapshot,tap,config} ...
Search, install, inspect, audit, configure, and manage skills from skills.sh, well-known agent skill
endpoints, GitHub, ClawHub, and other registries.
```

| 子命令 | 摘要（帮助原文） | 位置参数 / 选项 |
|---|---|---|
| `trust [path]` | Trust a project so its repo-local skills (./.hermes/skills, ./.agents/skills) load | `path` 默认 = cwd 所属 git checkout |
| `untrust [path]` | Revoke project-skill trust for a repo | `path` 同上 |
| `browse` | Browse all available skills (paginated) | `--page`(默认 1)、`--size`(默认 20)、`--source {all,official,skills-sh,well-known,github,clawhub,lobehub,browse-sh,nvidia,openai,anthropic,huggingface,voltagent,gstack,minimax}` |
| `search query` | Search skill registries | `--source {…同上 15 值}`、`--limit`、`--json` |
| `install identifier` | Install a skill（identifier 如 `openai/skills/skill-creator` 或直链 SKILL.md URL） | `--category`、`--name`、`--force`（Install despite blocked scan verdict）、`--yes/-y` |
| `inspect identifier` | Preview a skill without installing | — |
| `list` | List installed skills | `--source {all,hub,builtin,local}`、`--enabled-only` |
| `check [name]` | Check installed hub skills for updates | — |
| `update [name]` | Update installed hub skills | `--force`（Overwrite skills you have edited locally — they are skipped by default） |
| `audit [name]` | Re-scan installed hub skills | `--deep`（Run AST-level analysis on Python files (opt-in diagnostic)） |
| `uninstall name` | Remove a hub-installed skill | `--yes/-y` |
| `reset name` | Reset a bundled skill — clears 'user-modified' tracking so updates work again（清 `.bundled_manifest` 条目） | `--restore`（还删除当前副本并重新拷贝 bundled 版）、`--yes/-y` |
| `list-modified` | List bundled skills you've edited (which `hermes update` keeps) | `--json` |
| `diff name` | Show how your copy of a bundled skill differs from the stock version | — |
| `opt-out` | Stop bundled skills from being seeded into this profile（写 `.no-bundled-skills` marker） | `--remove`（同时删除未修改的 bundled 技能）、`--yes/-y` |
| `opt-in` | Re-enable bundled-skill seeding (undo opt-out) | `--sync`（立即重新播种） |
| `repair-official name` | Backfill or restore official optional skills from repo source（`name` 可为 `all`） | `--restore`、`--yes/-y` |
| `publish skill_path` | Publish a skill to a registry | `--to {github,clawhub}`、`--repo` |
| `snapshot {export,import}` | Export/import skill configurations | 见下 |
| `tap {list,add,remove}` | Manage skill sources | 见下 |
| `config` | Interactive skill configuration — enable/disable individual skills | — |

**三级子命令**：
- `skills snapshot export output` — `output`：Output JSON file path（`-` 表示 stdout）
- `skills snapshot import [--force] input` — `--force`：Force install despite caution verdict
- `skills tap list` — 无选项
- `skills tap add repo` — `repo`：GitHub repo（如 `owner/repo`）
- `skills tap remove name` — `name`：Tap name to remove

### 7.2 `hermes bundles --help`（5 个子命令）

```
usage: hermes bundles [-h] {list,show,create,delete,reload} ...
Skill bundles let you load several skills under one slash command. `/<bundle>` from the CLI or gateway
loads every referenced skill at once.
```

| 子命令 | 摘要 | 参数/选项 |
|---|---|---|
| `list` | List installed skill bundles | — |
| `show name` | Show one bundle's contents | `name` |
| `create name` | Create a new skill bundle（技能可用 `--skill` 重复给，或省略后交互输入） | `name`（成为 /slash 命令）、`--skill/-s SKILL`、`--description/-d`、`--instruction/-i`（Extra guidance prepended to the loaded skill content）、`--force/-f` |
| `delete name` | Delete a skill bundle | `name` |
| `reload` | Re-scan the bundles directory and report changes | — |

### 7.3 `hermes curator --help`（17 个子命令）

```
usage: hermes curator [-h]
  {status,usage,run,pause,resume,pin,unpin,list-unmanaged,adopt,restore,list-archived,archive,
   prune,backup,rollback,ledger,purge} ...
The curator is an auxiliary-model background task that periodically reviews agent-created skills, prunes
stale ones, consolidates overlaps, and archives obsolete skills. Bundled and hub-installed skills are
never touched. Archives are recoverable; auto-deletion never happens.
```

| 子命令 | 摘要 | 参数/选项 |
|---|---|---|
| `status` | Show curator status and skill stats | — |
| `usage` | Show usage telemetry for ALL skills (built-in, hub, agent) with provenance | `--sort {activity,recent,name}`（activity=最常用优先/默认；recent=最近活跃优先；name=字母序）、`--provenance {agent,bundled,hub}`、`--json` |
| `run` | Trigger a curator review now | `--sync/--synchronous`（等 LLM 复盘完成，手动运行默认）、`--background`、`--dry-run`（Report only — no state changes, no archives, no consolidation）、`--consolidate`（强制开启 LLM 伞合并轮，覆盖配置默认 off） |
| `pause` | Pause the curator until resumed | — |
| `resume` | Resume a paused curator | — |
| `pin skill` | Pin a skill so the curator never auto-transitions it | `skill` |
| `unpin skill` | Unpin a skill | `skill` |
| `list-unmanaged` | List curation-eligible skills with no provenance marker | — |
| `adopt [skill ...]` | Hand unmanaged skills to the curator（provenance 是**用户声明**） | `--all-unmanaged`、`--dry-run`、`--yes` |
| `restore skill` | Restore an archived skill | `skill` |
| `list-archived` | List archived skills | — |
| `archive skill` | Manually archive a skill (move to `.archive/`, excluded from prompt) | `skill` |
| `prune` | Bulk-archive curator-managed skills idle for >= N days (default 90) | `--days DAYS`（**默认 90**）、`-y/--yes`、`--dry-run` |
| `backup` | Take a manual tar.gz snapshot of `~/.hermes/skills/`（curator 每次真实 run 前也自动做） | `--reason REASON`（存进 `manifest.json`，默认 `'manual'`） |
| `rollback [entry_id]` | 整树快照回滚，或按 ledger entry id 单条回滚 | `entry_id`（来自 `hermes curator ledger`；省略=整树）、`--list`、`--id BACKUP_ID`（默认 newest）、`-y/--yes` |
| `ledger` | List the per-mutation skill audit ledger (all actors: curator/agent/user) | `--skill SKILL`、`--limit LIMIT`（**默认 20**） |
| `purge` | Delete archived skills older than `curator.archive_ttl_days`（**explicit only — never automatic**；记入 ledger） | `--days DAYS`（覆盖本次的 archive_ttl_days）、`--dry-run`、`-y/--yes` |

### 7.4 `hermes sync --help`（8 个子命令）

```
usage: hermes sync [-h] {status,pull,push,now,enable,disable,device,propose} ...
Skill Sync keeps your skills with you. Personal sync moves your own skills between your devices; if you
belong to an organisation, you also get its shared skills and can propose your own back to the team.
```

| 子命令 | 摘要 | 参数/选项 |
|---|---|---|
| `status` | Show what is synced, and from where | — |
| `pull` | Pull your synced skills (and your organisation's) | — |
| `push` | Push your opted-in skills | — |
| `now` | Reconcile now: pull then push | — |
| `enable skill` | Include a skill in your sync | `skill`（frontmatter name / 目录名） |
| `disable skill` | Exclude a skill from your sync | `skill` |
| `device` | Show or set this device's label（shown in the sync console） | `--name DEVICE_NAME`（省略则打印当前 label） |
| `propose name` | Share a skill with your organisation（admin 直接加入；否则成为待审 proposal） | `name`、`-m/--message MESSAGE` |

内置示例（帮助尾部原文）：
```
hermes sync status            what is synced, and from where
hermes sync enable my-skill   include a skill in your sync
hermes sync now               pull, then push
hermes sync propose my-skill  share a skill with your team
```

### 7.5 `hermes journey --help`（3 个子命令 + 7 个全局选项）

```
usage: hermes journey [-h] [--reveal 0..1] [--play] [--fps FPS] [--width WIDTH] [--height HEIGHT]
                      [--no-color] [--json] {list,delete,edit} ...
A terminal rendition of the desktop Star Map / Memory Graph: a timeline bar chart of learned skills and
memories over time (oldest at top, newest at bottom) plus a playable constellation scrubber. Mirrors the
TUI `/journey` overlay and the desktop panel.
```

| 子命令 | 摘要 | 参数/选项 |
|---|---|---|
| `list` | List node ids (for delete/edit) | `--no-color` |
| `delete node` | Delete a learned skill (archived) or memory by node id | `node`（技能名 或 `memory:<source>:<index>`）、`-y/--yes` |
| `edit node` | Edit a learned skill or memory by node id in `$EDITOR` | `node` |

全局选项：`--reveal 0..1`（Render the timeline built up to this point，0=oldest,1=now）、
`--play`（Animate the build-up over time，Ctrl-C 停止）、`--fps FPS`（默认 **12**）、
`--width WIDTH`、`--height HEIGHT`、`--no-color`、`--json`（Print the raw graph payload as JSON and exit）。

---

## 8. 与 Orbit 的对齐结论：技能层必须补齐的 top 6

按「缺了就不能算 parity」的负载重要性排序：

1. **多根扫描 + tier 优先级 + 同名 first-wins 去重**
   `project(.hermes/skills, .agents/skills，需 trust) > user(~/.hermes/skills) > external(skills.external_dirs，只读)`，
   `seen_names` 集合静默丢弃后者（`skills_tool.py:765,800-804`）。Orbit 若做成「合并/报错/覆盖」都是行为不兼容。

2. **frontmatter 硬校验 + 60 字符 description 预算**
   11 条硬校验（`_validate_frontmatter`），其中 `new_skill=True` 时额外强制 `len(description) ≤ 60`，
   索引装配按 **57 字符 + "..."** 截断。这是「技能索引能否被正确路由」的唯一保证。

3. **渐进披露三层 + 快照缓存失效**
   Tier1 `name+description` → Tier2 `skill_view` 全文 → Tier3 `references/` 按需；
   单文件 ≤100 000 字符；`.skills_prompt_snapshot.json`（`version:2` / `manifest{路径→mtime_ns+size}` /
   `skills[]` / `category_descriptions`）+ 写入后 `clear_skills_system_prompt_cache(clear_snapshot=True)`。
   外加 30 秒 TTL 扫描缓存与 `_SKILL_VIEW_DEDUP_CAP=200` 的重复查看去重（实测省 ~286k tokens/窗口）。

4. **写侧校验链 + staging 审批门 + 逐变更 ledger**
   `create/edit/patch/delete/write_file/remove_file` 六通道的**有序**守门（含 `_validate_delete_target` 三重路径安全、
   pinned 前台/后台不对称语义、`_curator_consolidation_delete_guard` fail-closed）；
   `write_approval` staging 到 `pending/skills/<id>.json` + `/skills approve` 回放；
   `.curator_ledger.jsonl` 的 `before[]/after[]` sha256 清单 + 内容寻址 blob（`id/ts/actor/action/skill/evidence`）。

5. **curator 生命周期：state 机 + 两级锚点 + 资格判定 + 可恢复归档**
   `active/stale/archived` + 正交 `pinned`；锚点 = `max(last_used_at, last_viewed_at, last_patched_at)` → 回退 `created_at`
   （**不含 mtime**）；`created_by: "agent"` 是**接管策略标志**而非作者归属；`.usage.json` sidecar 13 字段 +
   `msvcrt`/`fcntl` 双平台文件锁；归档到 `.archive/`（类目压平 + 时间戳后缀），**永不自动删除**，`prune` 默认 90 天。

6. **安装守门 + 分发：skills_guard 信任策略 + hub quarantine + 三方合并同步**
   107 条威胁模式 / 6 类别 / 4 severity；`INSTALL_POLICY` 四档信任 × 三档判决（`--force` 不能翻 dangerous）；
   hub 的 `quarantine → scan → move` + `lock.json` + `audit.log` + SSRF/symlink/类目桶三重路径防护；
   Skill Sync 的 content-addressed blob/tree/commit + CAS + **按整技能 tree 哈希的三方合并**
   （overlap → conflict ref，不自动合并）。

---

## 附录 A：本机关键文件清单与实测大小

| 路径 | 大小 | 备注 |
|---|---|---|
| `_internal\skills\` | 21 类目 / 91 SKILL.md | 类目目录另含 `DESCRIPTION.md` |
| `bundled-skills\` | 21 类目 / 91 SKILL.md | 与上者同规模 |
| `hermes-home\skills\` | 36 个一级目录 | 用户已装（只读） |
| `hermes-home\skills\.usage.json` | 38 397 B | 92 条记录 |
| `hermes-home\skills\.usage.json.lock` | 1 B | 锁文件（Windows `msvcrt`） |
| `hermes-home\skills\.curator_state` | 348 B | run_count=0 |
| `hermes-home\skills\.curator_ledger.jsonl` | 34 784 B | 14 条 |
| `hermes-home\skills\.bundled_manifest` | 4 158 B | `name:md5` |
| `hermes-home\skills\.hub\` | lock.json / taps.json / audit.log(0B) / index-cache | 从未安装 hub 技能 |
| `hermes-home\skills\.archive\` | 3 个已归档技能 | `cn-university-roster-verification`、`doubao2api-deploy-ops`、`feishu-gateway-troubleshooting` |
| `hermes-home\.skills_prompt_snapshot.json` | 112 272 B | version 2 / 165 skills / 183 manifest / 17 category_descriptions |

## 附录 B：技能层源码文件与体量

| 文件 | 字节 | 角色 |
|---|---|---|
| `skills_hub.py` | 198 101 | 远端索引 / 搜索 / 安装 / quarantine / lock / taps |
| `skill_manager_tool.py` | 90 045 | 写侧（create/edit/patch/delete/write_file/remove_file）+ 审批门 + ledger 联动 |
| `skills_sync_client.py` | 88 981 | 跨设备 Skill Sync 客户端（blob/tree/commit + CAS + 三方合并 + org） |
| `skills_tool.py` | 86 435 | 读侧（skills_list / skill_view / 扫描 / 缓存 / 去重） |
| `skills_sync.py` | 61 492 | bundled 播种与 manifest 更新（**不是**跨设备同步） |
| `skills_guard.py` | 59 443 | 安装前静态安全扫描（107 模式 + 信任策略） |
| `skill_usage.py` | 54 007 | 使用遥测 + provenance + 归档/恢复 + 资格判定 |
| `skill_ledger.py` | 24 402 | 逐变更审计账本 + 单条回滚 + blob 存储 |
| `skill_linter.py` | 16 440 | 12 条 advisory 规则 |
| `skillevaluator_scan.py` | 9 622 | evaluator 侧扫描 |
| `skills_ast_audit.py` | 5 196 | AST 深度诊断（4 个 pattern_id） |
| `skill_provenance.py` | 2 602 | 写来源 ContextVar（`foreground` / `background_review`） |
