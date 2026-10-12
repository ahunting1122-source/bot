# 方向 5 · Skill 层：流程知识的沉淀与复用

**先读 `handoff/00-CONTEXT.md`。**

## 目标

补上"该怎么做"这一层：技能（流程知识）的**存储、注入、沉淀、治理**四件事。首个技能 `web_search` 已落盘（`data/skills/web_search/SKILL.md`，内容来自实测三坑）。

## 已定决策

- **双层存储**：`data/skills/<name>/SKILL.md` 是**正文真值**（人可直接编辑，改完即生效）；`skills` 表是**索引与统计**（name, scope(global/project/site), triggers(JSON), version, usage_count, success_rate, failures, status(unreviewed/active/stale/archived), path）
- **注入**：系统提示只放"有哪些技能 + 一句话描述"（上限 5 个）；模型判断相关时调 `skill_load(name)` 取全文
- **新工具**：`skill_load(name)`（只读，免确认）、`skill_save(name, scope, triggers, body)`（v1 全放模式下直接落库，标 `unreviewed`）
- **沉淀流程**：任务运行时每步已落 `tool_results`——沉淀就是把这些轨迹交给模型提炼四样：触发条件 / 常量步骤 / **可变参数** / 坑与绕法。**不是复制轨迹，是参数化**
- **memories 与 skills 分工**：memories 存事实偏好（键值），skills 存流程做法。用户说"存成记忆"时模型判断归属，都相关就都写

## 冲突治理（四道闸，本方向一并实现）

1. **写入查重**：`skill_save` 落库前先按 scope+触发词检索现有技能，重叠（同 host 或触发词重合）→ 不新建，**更新现有技能**（version+1，合并差异）
2. **矛盾转备选**：同站不同做法不合并，都保留并在 triggers 里标注互为备选
3. **注入择优**：简述上限 5 个；排序 scope(site>project>global) → success_rate → usage_count；同簇只注入冠军
4. **降级淘汰**：按技能执行连续 2 次失败标 `stale` 降权；90 天未用或成功率 <50% 归档（不删）

## 涉及文件

`data/skills/web_search/SKILL.md`（已存在，可作格式范本）、`backend/app/skills.py`（新：加载器 + 统计）、`backend/app/tools.py`（注册 skill_load/skill_save）、`backend/app/graph/nodes.py`（prepare 注入简述）、`backend/migrations/versions/000X_skills.py`、`backend/tests/test_skills.py`（新）

## 验收

1. 加载 `web_search` 技能后，同类型任务（如"在某个新站点搜索 XX"）的**工具调用步数少于 16.7 首次摸索时的次数**（对比数据在 ARCHITECTURE.md 16.7）
2. `skill_save` 沉淀一次新流程，下次相似任务自动注入并被使用
3. 同 scope 触发重叠时走"更新"而非"新建"；连续失败后技能降权
4. 全量测试与评估集不回归

## 禁区

不开放全自动沉淀（先手工 + 用户触发）；不做技能市场/分享；不在技能里写密码；不用技能替代工具（技能引用工具）。
