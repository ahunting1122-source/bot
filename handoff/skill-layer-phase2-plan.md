# Skill 层二期方案（终版）

日期：2026-09-30。承接一期（`handoff/2026-09-30-skill-layer-phase1-closeout.md`），这是二期的最终设计——**治理四道闸 + 治理状态机**，钉在真实代码上，可直接按序执行。

---

## 0. 定调：治理状态存表，正文触发词存文件，单向同步

一期确立了「文件是真值、表是索引」。二期要新增的**降级淘汰**（连续失败标 stale、90 天未用归档）是**纯运行时动态统计**——它天生属于表，因为文件无法可靠承载"连续失败了几次"这种逐次变化的计数。

所以二期的架构定调是：

```
文件 SKILL.md（正文 + triggers + scope + version）   ← 人编辑，程序不反向写
        │  sync_to_store 单向镜像（文件 → 表，幂等 upsert）
        ▼
表 skills（索引 + 统计：usage_count / success_rate / failures /
        consecutive_failures / last_used_at / status 治理态）
        ▲
        │  四道闸只读写表的统计列，绝不反向写文件
```

**铁律：治理只改表的统计列与 status，绝不改文件正文。** 否则「文件是真值、改完即生效」这个给人类的承诺就被程序偷偷破坏了。治理结论（降权/归档）只影响**注入阶段是否选中该技能**，不删除文件、不改正文。

---

## 1. 四道闸的完整定义与落点

| 闸 | 触发点 | 输入 | 动作 | 落点 |
|---|---|---|---|---|
| ① 写入查重 | `skill_save` | 新技能 name/scope/triggers | 同 host/同触发词重叠 → **更新现有技能**（version+1、合并差异），不新建 | `save_skill` 前 |
| ② 矛盾转备选 | `skill_save` 查重命中 | 新旧两版正文 | 同站不同做法不合并，都保留，triggers 里标注互为备选 | `save_skill` 前 |
| ③ 注入择优 | `prepare` → `related_skills` | 全部候选技能 + 统计 | 简述上限 5；排序 scope(site>project>global) → success_rate → usage_count；**同簇只注入冠军** | `related_skills` |
| ④ 降级淘汰 | `finalize`（任务终态）+ `_touch_skill` | 本次用了哪些技能 + 成功与否 | 连续 2 次失败标 `stale` 降权；90 天未用或成功率<50% 归档 | 表统计列 |

四道闸**不在任何节点里写死逻辑**，全部走 `SkillProvider` Protocol 扩展方法，与一期「扩展点走 `policies.skills`」的约定一致。

---

## 1.1 三个已拍板决策（2026-09-30 定稿）

| 决策点 | 结论 |
|---|---|
| 同簇判定 | **A：triggers 里互标 `备选: xxx` 算一簇**，同簇只出排序冠军。复用 ② 矛盾转备选的产物，不引入新判定。簇判定 = 读 triggers 里有没有 `备选:` 前缀，一行正则。 |
| 阈值可配置 | **默认值定死 + 保留调参能力**：`stale 连续失败=2`、`归档未用天数=90`、`归档成功率下限=0.5` 做成 `Settings` 字段（`ORBIT_` 前缀 / `.env` 可覆盖），默认值写死。命名：`skill_stale_failures` / `skill_archive_days` / `skill_archive_min_success_rate`。 |
| 成功口径 | **B：只对「本次被 `skill_load` 且任务最终 completed」的技能记成功**（清连续失败）；被加载但任务失败 → `consecutive_failures+1`；未被加载的技能不记账。连续失败满 2 才 stale，用软降权吸收误伤；`partial`（引用未回填）与技能好坏无关，不作为失败依据。 |

---

## 2. 数据模型变更（迁移 `0007`）

`Skill` 表新增两列，支撑降级淘汰：

| 新列 | 类型 | 用途 |
|---|---|---|
| `consecutive_failures` | Integer, default 0 | 连续失败计数：任务失败 +1、成功归零；满 2 → 标 `stale` |
| `last_used_at` | String, default "" | 上次被 `skill_load` 的 `now()` 时间戳；90 天未用 → 归档 |

**`status` 治理状态机**（在「文件存活态 active/archived」之上扩展）：

```
unreviewed → active → stale → archived
  (新沉淀)   (在用)   (降权)   (归档)
```

- `unreviewed`：`skill_save` 新落盘，待用户确认（**未被确认前不注入**）
- `active`：手写技能（一期默认）+ 已确认技能，正常注入
- `stale`：连续 2 次失败降权，注入排序垫底（仍可被选，不硬剔除）
- `archived`：文件删了 / 90 天未用 / 成功率<50%，不再注入

**关键区分**：一期的「文件存活态」和二期「治理态」在 `status` 里是**同一列、同一状态机**——`archived` 就是两者的共同终态（文件删了=归档，治理淘汰=也归档）。这样不复用新列，语义自洽：`sync_to_store` 只把「文件没了」映射为 `archived`，治理把「不该用了」也映射为 `archived`，二者不冲突。

`unreviewed` 单独存表列，**不写进 frontmatter**——因为 frontmatter 的 `status` 是给人看的（文件在盘就是可用），而 `unreviewed` 是「模型刚吐的草稿还没人拍板」这个运行时态。`skill_save` 落盘时 frontmatter 仍写 `status: unreviewed`（一期已这么做），但**注入端读表判断，`status == unreviewed` 的技能不注入**，直到用户在文件里把 `status` 改成 `active`（或删掉这行）——`sync_to_store` 下次镜像时同步成 `active`。

---

## 3. 四道闸逐一设计

### ① 写入查重

**触发**：`skill_save(name, scope, triggers, body)` 落盘前。

**逻辑**：
1. 拿新技能的 `scope` + 归一化后的触发词，去现有技能里找重叠。
2. 重叠判定：**同 host 或触发词有 ≥2 字公共子串**（复用 `Nodes._trigger_matches` 的成熟匹配，不发明新机制）。
3. 命中且「同 host」→ **更新现有技能**：`version+1`，正文合并差异（追加新步骤/新坑，保留旧内容），写回文件。
4. 命中但「不同 host、仅触发词撞车」→ 走 ② 矛盾转备选。

**为什么一期不做**：写错会静默污染技能库。二期做是因为此时已有「技能被复用」的证据，值得投入。

### ② 矛盾转备选

**触发**：① 查重命中但新旧做法不同（不同 host、或同 host 但步骤矛盾）。

**逻辑**：
- **不合并**。两个技能都保留为独立文件（`name` 不同即可）。
- 在两者 triggers 里各加一条 `备选：<另一个技能名>`，让注入阶段知道"这两个是同一件事的不同做法"。
- 注入时同簇只出冠军（③ 处理），失败时模型可 `skill_load` 另一条备选。

**落点**：写文件的 frontmatter `triggers` 加 `备选: xxx` 条目（这是文件层面的元数据，允许 `sync_to_store` 镜像进表的 triggers JSON）。

### ③ 注入择优

**触发**：`prepare → related_skills`（现在只按 scope+目录名排序，二期换成全排序）。

**逻辑**：
1. 召回：与一期相同（trigger 与目标 ≥2 字公共子串）。
2. **过滤**：剔除 `status == unreviewed` 和 `status == archived`。
3. **排序**：`scope(site>project>global)` → `success_rate desc` → `usage_count desc` → name 稳定。
4. **同簇去重**：triggers 里互标「备选」的技能算一簇，只保留排序最高的冠军。
5. 截断 `SKILL_INJECT_LIMIT`（5）。

**数据来源**：`catalog()` 需扩展返回 `usage_count / success_rate / status / triggers`，即 `sync_to_store` 后从表读（或 catalog 直接 join 表）。为不破坏一期「catalog 读文件」的纯函数性，二期加一个 `catalog_with_stats(store, root)`（异步，读文件 + join 表），`related_skills` 改用它。

### ④ 降级淘汰

**触发**：`finalize`（任务终态，能判定成功/失败）+ `_touch_skill`（记录 `last_used_at`）。

**逻辑**：
1. **连续失败**：任务 FAILED 时，本次 `skill_load` 过的技能 `consecutive_failures+1`；任务 completed 则归零。满 `skill_stale_failures`（默认 2）→ `status=stale`。
2. **长时间未用**：`last_used_at` 距今 >`skill_archive_days`（默认 90）→ `archived`。
3. **低成功率**：`failures > 0` 且 `success_rate < skill_archive_min_success_rate`（默认 0.5）→ `archived`。
4. 归档**不删文件、不改正文**，只改表 `status`——文件还在，用户手动改回还能复活（`sync_to_store` 下次镜像时，若文件在且表 `status=archived` 且非"文件删了"原因，则……**这里需谨慎**：见 §6 风险 2）。

**落点**：`finalize` 里调 `policies.skills.record_outcome(task_status, used_skill_names)`（新增协议方法），`_touch_skill` 里加 `last_used_at=now()`。

---

## 4. Protocol 扩展（`SkillProvider`）

一期已有 `catalog() / load() / save()`。二期新增：

```python
class SkillProvider(Protocol):
    def catalog(self) -> list[dict]: ...
    def load(self, name: str) -> str: ...
    def save(self, name, scope, triggers, body) -> dict: ...

    # 二期新增
    async def catalog_with_stats(self, store, root) -> list[dict]:
        """目录 + 表统计（usage_count/success_rate/status），供注入择优。"""

    async def record_outcome(self, store, task_status: str, used_skill_names: list[str]) -> None:
        """任务终态回写：成功清零、失败累加连续失败、满 2 标 stale。"""

    def find_conflict(self, scope, triggers) -> list[dict]:
        """查重：返回与新技能重叠的现有技能列表（② 的输入）。"""
```

`DefaultSkillProvider` 对应实现；`NullSkillProvider`（若有）保持空转兼容。

---

## 5. 迁移与实施顺序

```
迁移 0007：skills 加 consecutive_failures / last_used_at 两列
   ↓
skills.py：save_skill 前接查重（①）、find_conflict、矛盾转备选（②）
   ↓
policies.py：SkillProvider 加三个方法；DefaultSkillProvider 实现
   ↓
nodes.py：related_skills 换 catalog_with_stats（③ 注入择优）
           finalize 调 record_outcome（④ 降级淘汰）
tools.py：_touch_skill 加 last_used_at；skill_save 返回"更新 or 新建"结果
   ↓
tests/test_skills.py 扩展 + 全量门禁
```

每步独立可验收，不一次性堆大 diff。

---

## 6. 风险与必须写进实现的护栏

> **实施状态（2026-09-30）**：本方案已完整落地。四条护栏全部在实现中兑现，其中风险 2（归档复活 bug）已在 `sync_to_store` 中修复，并加了两个回归测试（`test_sync_to_store_does_not_resurrect_stale` / `test_sync_to_store_resurrects_on_explicit_active`）。风险 4 有一处实现细节调整：`record_outcome` 的降级回写接在 `agent.py` 的任务终态（`SUCCEEDED`/`FAILED`）之后，而非 `finalize` 节点——因为任务成败是 runner 在整张图跑完后才判定的，`finalize` 节点拿不到确定终态。

1. **治理不能反向污染文件**：四道闸只改表，唯一写文件的场景是 ①② 的「更新/标注备选」——那也是**用户触发 `skill_save` 时的显式动作**，不是后台自动。降级淘汰（④）**绝不碰文件**。

2. **`sync_to_store` 的归档复活 bug（一期已埋，已修复）**：现在 `sync_to_store` 里「文件在盘 → 表 status 改回 active」这行，会把治理标成 `stale/archived` 的技能**错误复活**。二期必须修：`sync_to_store` 只负责「文件删了 → archived」这一件事，**不再把 archived 改回 active**；`active` 的恢复只由「用户手动把 frontmatter status 改回 active」触发（读文件时发现 `status: active` 才覆盖表的 stale/archived）。这是二期第一个要修的隐患。

3. **`unreviewed` 不注入**：新沉淀的技能必须人工确认（把 frontmatter 的 `unreviewed` 改成 `active` 或删掉）才进注入池，否则模型自己吐的草稿立刻回灌自己，形成反馈回环。

4. **降级是软降权，不是硬删除**：`stale` 技能仍可被 `skill_load`（模型显式点名时），只是注入排序垫底、不主动推荐。归档才彻底不注入。

---

## 7. 验收标准

1. 同站第二遍任务步数 < 首遍 16.7（一期已有观测基线）。
2. `skill_save` 沉淀同 host 技能 → 走「更新 version+1」而非新建两行。
3. 同簇冲突技能 → 双双保留、互标备选，注入只出冠军。
4. 连续 2 次失败 → `stale`，注入排序垫底；90 天未用 → `archived` 不注入。
5. `unreviewed` 技能不注入，直到人工确认。
6. 全量 pytest / ruff / mypy / 前端 / eval_run 不回归。

---

## 8. 二期不做的（明确划界）

- 技能市场 / 分享（`05-skill-layer.md` 禁区）。
- 全自动沉淀（仍「手工 + 用户触发」，模型只提议）。
- 多模态技能、跨项目技能同步。
- 在技能里写密码 / 密钥。
- 治理自动化定时任务（`PROGRESS-WATCH` 是进度跟踪，不是治理）。
