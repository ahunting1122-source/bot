# Skill 层一期收口 + 二期过渡骨架

日期：2026-09-30。承接 `2026-09-30-skill-layer-read-report.md`（读取侧落地）之后，完成「一期可以简单交付 + 二期过渡准备」。

## 目标拆解

1. **一期收口（简单交付）**：技能对模型/用户可见可管理——`skill_load`（已有）+ 新增 `skill_list`（列技能）+ 新增 `skill_save`（沉淀落盘）。
2. **建 `skills` 表（二期过渡骨架）**：这是二期的落点，现在建好表 + 定义模型 + 冻结迁移编号，二期不用再动 schema。
3. **预留接口不实现**：`skill_save` 只做最小落盘（写文件标 `unreviewed`），四道闸治理逻辑留二期。

## 改动清单

| 文件 | 改动 |
|---|---|
| `app/storage.py` | 新增 `Skill` 模型（name/scope/triggers(JSON)/version/body/path/usage_count/success_rate/failures/status/created_at/updated_at） |
| `migrations/versions/0006_skills.py`（新） | `down_revision=0004`，upgrade/downgrade 均验证通过 |
| `app/skills.py` | 新增 `save_skill`（写 SKILL.md + 名字防穿越校验）与 `sync_to_store`（文件→表幂等镜像） |
| `app/policies.py` | `SkillProvider` Protocol 加 `save()`；`DefaultSkillProvider` 实现 |
| `app/tools.py` | 注册 `skill_list` + `skill_save`；`_touch_skill` 使 `skill_load` 累加 `usage_count` |
| `tests/test_skills.py` | +5 测试 |
| `ARCHITECTURE.md` / `PROGRESS-WATCH.md` | 状态表同步 |

## 关键设计决策

1. **文件是真值，表是索引**。`skills` 表由 `sync_to_store` 从文件镜像（幂等 upsert）；文件删了 → 标 `archived`（不删行，保留统计）。
2. **`status` 字段语义收敛为「文件存活态」**（active/archived），避免与二期治理审核态（unreviewed/approved/rejected）在同一个字段上打架——后者二期再单独加字段。
3. **`skill_save` 一期只落盘不治理**：做名字校验（`_SAFE_NAME` 正则防目录穿越）+ 空正文/非法名拒绝 + 写文件标 `unreviewed`；四道闸（写入查重 / 矛盾转备选 / 注入择优 / 降级淘汰）留二期。
4. **迁移用 `0006`**（`0005` 已冻结给向量表，不抢占）。

## 门禁

- pytest **119 passed**（+5）
- ruff clean · mypy clean（20 files）
- 前端 lint + build 通过
- 迁移 upgrade → `0006`、downgrade → `0004` 均通过（skills 表 13 列齐全）
- eval_run **14/14 无回归**

## 二期（四道闸治理）触发条件

出现「模型反复踩同类坑」或「你手动改过 SKILL.md 想沉淀新流程」时开工。届时用「同站第二遍步数 < 16.7 首遍」这条量化标准证明技能真实有效。
