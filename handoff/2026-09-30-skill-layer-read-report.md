# 2026-09-30 · Skill 层一期（读取侧）落地记录

## 结论

Skill 层分两期，本轮落地**一期（读取侧）**：技能文件加载 + `skill_load` 工具 + prepare 注入简述。沉淀（`skill_save`）与四道闸留二期。端到端验证通过：真实任务里模型主动 `skill_load` 并按技能步骤执行。

## 改动清单

| 文件 | 改动 |
|---|---|
| `backend/app/skills.py`（新） | frontmatter 解析（name/scope/version/triggers）+ 正文拆分；`catalog()` / `load_skill()` / `skill_summary()`。每次读磁盘不缓存（文件是真值）。坏文件/缺失文件返回空，不抛异常 |
| `backend/app/policies.py` | `DefaultSkillProvider` 从空实现改为委托 `app/skills.py`；`load() -> str`（正文）；`Policies.default` 注入 `DefaultSkillProvider(settings)` |
| `backend/app/tools.py` | 新增 `SkillLoadArgs` + `skill_load` 工具（只读、免确认）；local 分支读 `settings.data_dir / "skills"` |
| `backend/app/graph/nodes.py` | `prepare` 调用 `related_skills()` 注入相关技能简述（上限 5），落 `context_meta["skills"]` + `skills_block`，发 `skills_injected` 事件；`think` 把 `skills_block` 与 `retrieved_block` 一起拼进系统提示 |
| `backend/app/context.py` | SYSTEM 增加技能说明句；`PROMPT_VERSION` → `2026-09-30.2` |
| `data/skills/bili_video_detail/SKILL.md`（新） | 沉淀「点击开新标签页」坑 + 抽标题/UP主/播放量 |
| `data/skills/form_fill_login/SKILL.md`（新） | 表单填写与登录（vault_login 优先、ref 过期、SPA 登录判断） |
| `backend/tests/test_skills.py`（新） | 8 个测试：解析、加载、catalog、summary、工具、注入匹配、provider |

## 关键设计决策

1. **一期只做「读」，不建表**。`catalog()` 直接 `glob` 目录 + 解析 frontmatter，`skills` 表（存 usage_count/success_rate/version 合并）留到二期——它只为沉淀统计服务。因此**不碰迁移编号**（`0005` 仍留给向量表）。
2. **相关性判断 = 字面匹配，不复用 FTS5**。方案 A 本意是复用检索，但技能库只有 3 篇、上限 5，且**不该混进 `chunks` 索引**（会污染引用回填）。所以用「trigger 与任务目标有 ≥2 字公共子串」的轻量匹配，scope 优先 site>project>global，按目录名稳定排序。
3. **注入走 `context_meta["skills_block"]`**，与 `retrieved_block` 并列，`think` 统一拼接——复用现有注入通道，不新增机制。
4. **职责分层**：工具层 `skill_load` 直接读文件（`ToolRegistry` 持有 settings）；节点层注入走 `policies.skills`（`Nodes` 持有 policies）。两者不互相依赖。

## 验证

| 门禁 | 结果 |
|---|---|
| `uv run pytest -q` | **114 passed**（106 → 114，+8 技能测试） |
| `ruff check app tests ../scripts` | clean |
| `mypy app` | clean（20 files） |
| 前端 `lint` + `build` | 通过 |
| `eval_run.py` 全量 | **14/14**，无回归 |

**端到端实测**（真实模型 + 浏览器）：任务「在 B 站搜索机械键盘，按最多播放排序，告诉我前三的标题」→ `skills_injected` 注入了 `['bili_video_detail', 'web_search']`，模型**主动调用 `skill_load`**，最终按最多播放排序给出 Top3、附来源 URL、并主动剔除了无关条目（正是 `web_search` 技能第 6 条输出要求）。

## 剩余债（二期）

- `skill_save` 沉淀 + 四道闸（写入查重 / 矛盾转备选 / 注入择优 / 降级淘汰）
- `skills` 表（存统计，供 version 合并与降级淘汰）
- MCP prompts 作技能来源（复用加载器）
- 技能价值量化：用「同站第二遍步数 < 16.7 首遍」这条标准，跑几周出数据

## 触发条件（何时开工二期）

出现「模型反复在同一类任务里踩同样的坑、或用户手动改过 SKILL.md 想沉淀新流程」时，说明技能库需要自动沉淀——届时再上 `skill_save`。
