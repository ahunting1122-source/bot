"""Curator 层：技能生命周期治理（状态机 / 锚点 / 可恢复归档 / 逐变更 ledger）。

对标 Hermes `tools/skill_usage.py`（遥测 + 状态 + 归档/恢复 + 资格判定）、
`tools/skill_ledger.py`（逐变更审计账本 + 单条回滚）。规格出处：
`handoff/hermes-parity/research/04-skill-layer.md` §5。

## 状态机

```
unreviewed ──(首次使用)──▶ active ──(连续失败 ≥ 2)──▶ stale
                              ▲                        │
                              └──(连续失败清零)────────┘
active/stale ──(闲置 > 90 天 或 成功率 < 0.5 且有失败)──▶ archived
```

`pinned` **不是状态**，是正交布尔标志（Hermes `skill_usage.py:20-26` 原文：
「pinned -> opt-out from auto transitions (boolean flag, orthogonal to state)」）。
pin 住之后任何自动迁移都不碰它——用户说"这个技能我要一直留着"，就该一直留着。

## 三个必须写进注释的坑

1. **锚点不含文件 mtime**。Hermes 的锚点是
   ``max(last_used_at, last_viewed_at, last_patched_at)`` → 回退 ``created_at``
   （`skill_usage.py:145-162`）。``mtime`` 被**编辑器、同步盘、git checkout、解压工具**
   随手改；拿它判"陈旧"会把刚打开过一次的老技能判成"刚用过"（永不归档），
   也会把同步工具重写过的技能判成"刚创建"——两个方向都会误判。

2. **`use_count == 0` 永不因"未使用"归档**。"从未用过" ≠ "陈旧"。一个刚装进来
   还没被触发的技能，闲置天数和 90 天老技能长得一模一样；只按时间判会把它连同
   "还没轮到它"一起埋掉。所以闲置归档的前提是 ``use_count > 0``。

3. **`.usage.json` 只装遥测，治理状态另存 `<data_dir>/skills-state.json`**。
   任务书把 `.usage.json` 定为 13 个使用统计字段；把 ``state`` 混进去有两个坏处：
   ① 删掉 `.usage.json`（"重置统计"这种常规操作）会顺手抹掉治理态，已归档技能静默复活；
   ② 归档要把技能目录整体搬走，状态若跟着目录走，搬完就得再写一次目标位置的文件，
   回滚时还要搬回来——多一条能出错的路。放在 data_dir 的独立文件里，两件事互不牵连。

## 为什么归档是"移动目录"而不是"打标记"

Hermes 的强承诺（CLI `curator --help` 原文）：「Archives are recoverable;
**auto-deletion never happens**」。本实现照抄：归档 = 把技能目录搬进
``<data_dir>/skills-archive/<name>/``，恢复 = 搬回来，每一步都进 ledger。
`prune` 只归档不删除——"自动删除"这条路根本不存在，所以也不需要"防止误删"的讨论。
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.skill_index import SkillIndex, SkillRecord

# ── 状态 ────────────────────────────────────────────────────────────────────

STATE_UNREVIEWED = "unreviewed"
STATE_ACTIVE = "active"
STATE_STALE = "stale"
STATE_ARCHIVED = "archived"
#: 迁移图上的四个状态。顺序即"生命周期顺序"，用于展示。
VALID_STATES: tuple[str, ...] = (STATE_UNREVIEWED, STATE_ACTIVE, STATE_STALE, STATE_ARCHIVED)

# ── 阈值（默认值全部落在常量上，测试与 CLI 都从这里取，避免文案与实现漂移）──────

#: 连续失败多少次判 stale。Hermes 的 `curator.stale_after_days` 数值不可证
#: （`agent/curator.py` 被压进 exe PYZ，见调研 04 §0.1/§5.2），所以这里用
#: 「连续失败次数」这个**可观测、可解释**的判据代替纯时间判据：失败是可归因的，
#: 而"多久没用"对从来没被触发过的技能毫无意义。
STALE_FAILURES = 2
#: 闲置多少天判 archived。**唯一可证的 Hermes 天数阈值**：
#: `hermes curator prune --help` 原文「idle for at least N days (default: 90)」。
ARCHIVE_DAYS = 90
#: 成功率下限：低于它且有失败 → 归档。
ARCHIVE_MIN_SUCCESS_RATE = 0.5
#: 成功率判据的**最小样本量**。这是对任务书"成功率 < 0.5 且有失败"的必要补充：
#: 没有它，一个刚连续失败 2 次（成功率 0.0）的技能会**同时**满足 stale 与 archived
#: 两条规则，而两条规则的结论相反（stale 可自愈、archived 要手动恢复）。要求至少
#: 3 个成败样本，等于把两条规则分工定清：连续失败 = 短期信号（stale），
#: 成功率低 = 长期信号（archived）。
ARCHIVE_MIN_SAMPLES = 3

# ── 文件布局 ────────────────────────────────────────────────────────────────

#: 每技能一份使用遥测（sidecar，与 SKILL.md 解耦）。
USAGE_FILENAME = ".usage.json"
#: 逐变更审计账本（append-only JSONL）。
LEDGER_FILENAME = "skills-ledger.jsonl"
#: 生命周期状态（独立于 .usage.json，理由见模块 docstring 第 3 条）。
STATE_FILENAME = "skills-state.json"
#: 归档区。
ARCHIVE_DIRNAME = "skills-archive"

# ── `.usage.json` 的字段全集 ─────────────────────────────────────────────────

#: 任务书要求的 13 个使用统计字段（顺序即落盘顺序，人读时稳定）。
USAGE_FIELDS: tuple[str, ...] = (
    "use_count",  # 被主动使用次数
    "view_count",  # 被查看次数
    "patch_count",  # 被修改次数
    "reuse_after_patch",  # 改完之后是否又被复用（质量代理指标，不是成败）
    "last_used_at",
    "last_viewed_at",
    "last_patched_at",
    "first_used_at",
    "consecutive_failures",  # 连续失败数，成功即清零
    "successes",
    "failures",
    "created_by",  # 接管策略标志："agent" = curator 可管，不是"agent 写的"的证据
    "pinned",  # 正交于 state 的豁免标志
)

#: 第 14 个键：锚点的第二级回退（调研 04 §5.3 明确要求「回退 created_at」）。
#: 单列出来是因为它不属于"使用统计"，而是"技能存在的时间基准"。
USAGE_ANCHOR_FIELDS: tuple[str, ...] = ("created_at",)

# ── ledger ─────────────────────────────────────────────────────────────────

#: 账本一条记录的 8 个字段（Hermes 实测 14 条取并集）。
LEDGER_FIELDS: tuple[str, ...] = ("id", "ts", "actor", "action", "skill", "evidence", "before", "after")
ACTOR_CURATOR = "curator"
ACTOR_AGENT = "agent"
ACTOR_USER = "user"
VALID_ACTORS: tuple[str, ...] = (ACTOR_CURATOR, ACTOR_AGENT, ACTOR_USER)

#: 可被 `rollback` 反向执行的 action。其它 action（pin/unpin 之类）改的是标志位，
#: 反向执行没有意义——与其假装能回滚，不如明确拒绝。
ROLLBACK_ACTIONS: frozenset[str] = frozenset({"archive", "restore"})


class CuratorError(RuntimeError):
    """curator 的可预期失败（技能不存在 / 外部技能只读 / pin 住不能归档…）。"""


def _iso(moment: datetime) -> str:
    """统一 ISO8601（带 UTC 偏移），与 Hermes 的 ``ts`` 形状一致。"""
    return moment.isoformat()


def parse_iso(raw: Any) -> datetime | None:
    """解析 ISO 时间戳。失败返回 None；naive 时间戳按 UTC 处理（Hermes 同口径）。"""
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment


@dataclass
class UsageRecord:
    """一个技能的使用遥测（= 一份 `.usage.json`）。"""

    use_count: int = 0
    view_count: int = 0
    patch_count: int = 0
    reuse_after_patch: bool = False
    last_used_at: str | None = None
    last_viewed_at: str | None = None
    last_patched_at: str | None = None
    first_used_at: str | None = None
    consecutive_failures: int = 0
    successes: int = 0
    failures: int = 0
    created_by: str | None = None
    pinned: bool = False
    created_at: str | None = None

    # —— 派生量 ——

    @property
    def activity_count(self) -> int:
        """活动总量 = 使用 + 查看 + 修改（Hermes `activity_count`）。"""
        return self.use_count + self.view_count + self.patch_count

    @property
    def outcome_total(self) -> int:
        return self.successes + self.failures

    @property
    def success_rate(self) -> float:
        """成功率。**没有样本时返回 1.0**：0 样本不能被当作"全失败"。

        这条容易写错：`successes / (successes + failures)` 在分母为 0 时要么抛异常
        要么被兜成 0.0，而 0.0 < 0.5 会让"从没跑过的技能"直接被归档。返回 1.0
        表示"暂无证据表明它不好"，与 ``use_count == 0 永不因未使用归档`` 同一立场。
        """
        total = self.outcome_total
        if total == 0:
            return 1.0
        return self.successes / total

    def latest_activity_at(self) -> datetime | None:
        """第 1 级锚点：三个活动时间戳取**最大值**（不是按顺序短路）。"""
        latest: datetime | None = None
        for raw in (self.last_used_at, self.last_viewed_at, self.last_patched_at):
            moment = parse_iso(raw)
            if moment is None:
                continue
            if latest is None or moment > latest:
                latest = moment
        return latest

    def anchor(self) -> datetime | None:
        """第 2 级锚点：活动时间戳全空 → 回退 ``created_at``。

        **刻意不含文件 mtime**（理由见模块 docstring 第 1 条）。
        """
        return self.latest_activity_at() or parse_iso(self.created_at)

    def idle_days(self, now: datetime) -> float | None:
        """距锚点多少天。没有锚点 → None（"不知道"不等于"很久"）。"""
        anchor = self.anchor()
        if anchor is None:
            return None
        return (now - anchor).total_seconds() / 86400.0

    # —— 事件 ——

    def record_use(self, now: datetime) -> None:
        """主动使用一次。首次使用同时写 ``first_used_at``。"""
        stamp = _iso(now)
        self.use_count += 1
        self.last_used_at = stamp
        if self.first_used_at is None:
            self.first_used_at = stamp

    def record_view(self, now: datetime) -> None:
        self.view_count += 1
        self.last_viewed_at = _iso(now)

    def record_patch(self, now: datetime) -> None:
        self.patch_count += 1
        self.last_patched_at = _iso(now)

    def record_outcome(self, *, success: bool, now: datetime) -> None:
        """记一次执行结果。成功**清零**连续失败——这是 stale 能回 active 的唯一途径。"""
        if success:
            self.successes += 1
            self.consecutive_failures = 0
        else:
            self.failures += 1
            self.consecutive_failures += 1
        self.last_used_at = _iso(now)

    # —— 序列化 ——

    def to_dict(self) -> dict[str, Any]:
        """按 ``USAGE_FIELDS`` + 锚点字段的顺序落盘（人读 diff 时稳定）。"""
        payload: dict[str, Any] = {}
        for name in USAGE_FIELDS:
            payload[name] = getattr(self, name)
        for name in USAGE_ANCHOR_FIELDS:
            payload[name] = getattr(self, name)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> UsageRecord:
        """从磁盘读回。未知键忽略、类型不符退默认——遥测文件坏掉不该阻断治理。"""
        data = payload or {}
        record = cls()
        for name in USAGE_FIELDS:
            if name not in data:
                continue
            value = data[name]
            if name in ("use_count", "view_count", "patch_count", "consecutive_failures",
                        "successes", "failures"):
                try:
                    setattr(record, name, int(value))
                except (TypeError, ValueError):
                    continue
            elif name in ("pinned", "reuse_after_patch"):
                setattr(record, name, bool(value))
            else:
                setattr(record, name, None if value is None else str(value))
        for name in USAGE_ANCHOR_FIELDS:
            if name in data:
                setattr(record, name, None if data[name] is None else str(data[name]))
        return record


@dataclass(frozen=True)
class LedgerEntry:
    """账本一条。``before`` / ``after`` 是 ``[{path, sha256}]`` 清单。"""

    id: str
    ts: str
    actor: str
    action: str
    skill: str
    evidence: dict[str, Any] = field(default_factory=dict)
    before: tuple[dict[str, str], ...] = ()
    after: tuple[dict[str, str], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": self.ts,
            "actor": self.actor,
            "action": self.action,
            "skill": self.skill,
            "evidence": dict(self.evidence),
            "before": [dict(item) for item in self.before],
            "after": [dict(item) for item in self.after],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LedgerEntry:
        return cls(
            id=str(payload.get("id") or ""),
            ts=str(payload.get("ts") or ""),
            actor=str(payload.get("actor") or ""),
            action=str(payload.get("action") or ""),
            skill=str(payload.get("skill") or ""),
            evidence=dict(payload.get("evidence") or {}),
            before=tuple(dict(item) for item in payload.get("before") or []),
            after=tuple(dict(item) for item in payload.get("after") or []),
        )


def make_ledger_id(
    action: str, skill: str, ts: str, before: Any, after: Any
) -> str:
    """内容寻址的账本 id：``sha256(action|skill|ts|before|after)[:16]``。

    为什么内容寻址而不是 uuid：同样的 (动作, 技能, 时间, 前后状态) 必然得到同一个 id，
    于是"同一次变更被记两遍"在账本里**自动去重**，重放/重试也不会产生两条互相矛盾的
    审计记录。这不是为了省 uuid 的随机数，而是让 id 本身携带"这是什么变更"的信息。
    """
    payload = "|".join(
        [
            action,
            skill,
            ts,
            json.dumps(before, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            json.dumps(after, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _file_manifest(root: Path) -> list[dict[str, str]]:
    """列出目录下全部文件的 ``{path, sha256}`` 清单（递归，按路径排序）。

    为什么记整包而不是只记 SKILL.md：Hermes 的 issue #96962 就是"只捕磁盘拿到空壳"
    ——归档前先把支撑文件迁走的流程里，只记一个文件会让回滚恢复出半个技能。
    """
    if not root.is_dir():
        return []
    manifest: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        manifest.append({"path": str(path), "sha256": digest})
    return manifest


class Curator:
    """技能生命周期治理器。一个实例 = 一份 ``data_dir`` 的治理视图。"""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        index: SkillIndex | None = None,
        cwd: Path | str | None = None,
        stale_failures: int = STALE_FAILURES,
        archive_days: int = ARCHIVE_DAYS,
        archive_min_success_rate: float = ARCHIVE_MIN_SUCCESS_RATE,
        now: Any = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.index = index if index is not None else SkillIndex(self.data_dir, cwd=cwd)
        self.stale_failures = stale_failures
        self.archive_days = archive_days
        self.archive_min_success_rate = archive_min_success_rate
        self._now = now

    # —— 基础设施 ——

    def now(self) -> datetime:
        """当前时间。测试注入 ``now`` 才能验证"闲置 100 天"这类判定。"""
        if self._now is not None:
            return self._now()
        return datetime.now(UTC)

    @property
    def ledger_path(self) -> Path:
        return self.data_dir / LEDGER_FILENAME

    @property
    def state_path(self) -> Path:
        return self.data_dir / STATE_FILENAME

    @property
    def archive_root(self) -> Path:
        return self.data_dir / ARCHIVE_DIRNAME

    def usage_path(self, skill_dir: Path) -> Path:
        return skill_dir / USAGE_FILENAME

    def read_usage(self, skill_dir: Path) -> UsageRecord:
        """读一个技能的 `.usage.json`。缺失/损坏 → 空记录（不是错误）。"""
        path = self.usage_path(skill_dir)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return UsageRecord()
        if not isinstance(payload, dict):
            return UsageRecord()
        return UsageRecord.from_dict(payload)

    def write_usage(self, skill_dir: Path, record: UsageRecord) -> None:
        """写回 `.usage.json`（临时文件 + replace，避免半截文件被读到）。

        幂等：同一份记录写两次，文件内容逐字节相同（``to_dict`` 顺序固定、无时间戳注入）。
        """
        skill_dir.mkdir(parents=True, exist_ok=True)
        path = self.usage_path(skill_dir)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(record.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        temporary.replace(path)

    def _load_states(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        skills = payload.get("skills")
        return dict(skills) if isinstance(skills, dict) else {}

    def _save_states(self, states: dict[str, dict[str, Any]]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps({"version": 1, "skills": states}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def state_of(self, name: str) -> str:
        """当前状态。没有记录 = ``unreviewed``（新沉淀、还没被确认过）。"""
        return self._state_from(self._load_states(), name)

    def _state_from(self, states: dict[str, dict[str, Any]], name: str) -> str:
        """从**已加载**的状态表里取状态。

        单条查询走 `state_of` 会重读一次 JSON；批量场景（run / status / usage /
        prune）必须先 `_load_states()` 一次再走这里——100 个技能 × 100 次文件解析
        是那种"能跑但没人愿意等"的慢。
        """
        entry = states.get(name) or {}
        state = str(entry.get("state") or "")
        return state if state in VALID_STATES else STATE_UNREVIEWED

    def state_map(self) -> dict[str, str]:
        """一次读出全部技能状态（批量场景用，避免 N 次文件解析）。"""
        states = self._load_states()
        return {name: self._state_from(states, name) for name in states}

    def state_entries(self) -> dict[str, dict[str, Any]]:
        """原始状态表（``{名字: {state, reason, updated_at, archived_at?}}``）。

        `state_map` 只给状态字符串；`app/journey.py` 的时间线还需要
        ``updated_at``（事件发生在哪天）与 ``reason``（为什么迁移）——从字符串反推
        这两样是不可能的，所以把已解析的表原样交出去，而不是让调用方去读私有
        ``_load_states``（那样一改内部结构就会静默崩在另一个模块里）。
        """
        return {name: dict(entry) for name, entry in self._load_states().items()}


    def set_state(self, name: str, state: str, reason: str = "") -> None:
        if state not in VALID_STATES:
            raise CuratorError(f"未知状态：{state}")
        states = self._load_states()
        self._apply_state(states, name, state, reason)
        self._save_states(states)

    def _apply_state(
        self, states: dict[str, dict[str, Any]], name: str, state: str, reason: str
    ) -> None:
        """改内存里的状态表（调用方负责落盘）。`run()` 一次性落盘，避免 N 次写文件。"""
        entry = states.setdefault(name, {})
        entry["state"] = state
        entry["reason"] = reason
        entry["updated_at"] = _iso(self.now())
        if state == STATE_ARCHIVED:
            entry.setdefault("archived_at", _iso(self.now()))
        elif "archived_at" in entry:
            entry.pop("archived_at", None)

    # —— ledger ——

    def record_ledger(
        self,
        *,
        action: str,
        skill: str,
        actor: str = ACTOR_CURATOR,
        evidence: dict[str, Any] | None = None,
        before: list[dict[str, str]] | None = None,
        after: list[dict[str, str]] | None = None,
    ) -> str:
        """追加一条账本记录，返回它的内容寻址 id。

        账本是**遥测不是门**（Hermes 原文：「a ledger failure must never block the
        mutation it describes」）——但本实现仍然让它抛异常，因为 Orbit 的写入路径
        没有并发压力，而"静默吞掉审计失败"意味着账本会悄悄缺条目，比写入失败更难查。
        """
        if actor not in VALID_ACTORS:
            raise CuratorError(f"未知 actor：{actor}")
        ts = _iso(self.now())
        before_list = [dict(item) for item in (before or [])]
        after_list = [dict(item) for item in (after or [])]
        entry = LedgerEntry(
            id=make_ledger_id(action, skill, ts, before_list, after_list),
            ts=ts,
            actor=actor,
            action=action,
            skill=skill,
            evidence=dict(evidence or {}),
            before=tuple(before_list),
            after=tuple(after_list),
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry.to_dict(), ensure_ascii=False) + "\n")
        return entry.id

    def ledger_entries(self, *, skill: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        """读账本（最新在前）。坏行跳过——一行坏了不能整本读不出来。"""
        entries: list[dict[str, Any]] = []
        try:
            lines = self.ledger_path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            return []
        for line in lines:
            text = line.strip()
            if not text:
                continue
            try:
                payload = json.loads(text)
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            if skill and str(payload.get("skill") or "") != skill:
                continue
            entries.append(payload)
        entries.reverse()
        return entries[: max(0, limit)]

    def find_ledger_entry(self, entry_id: str) -> dict[str, Any] | None:
        for entry in self.ledger_entries(limit=10_000):
            if entry.get("id") == entry_id:
                return entry
        return None

    # —— 归档 / 恢复 / 回滚 ——

    def _archive_dir(self, name: str) -> Path:
        """归档目标目录。重名冲突追加时间戳后缀（Hermes 同规则）。

        为什么允许重名冲突：同一个技能可能被归档、恢复、再归档。若直接覆盖，
        第二次归档会把第一次的备份抹掉——"可恢复"就打了折扣。
        """
        target = self.archive_root / name
        if not target.exists():
            return target
        return self.archive_root / f"{name}-{self.now().strftime('%Y%m%d%H%M%S')}"

    def archive(self, name: str, *, actor: str = ACTOR_USER, reason: str = "手动归档") -> str:
        """手动归档：把技能目录搬进归档区，记 ledger，状态改 archived。返回 ledger id。"""
        result = self.index.index()
        record = result.find(name)
        if record is None:
            raise CuratorError(f"技能不存在：{name}")
        return self._archive_record(record, actor=actor, reason=reason)

    def _archive_record(
        self,
        record: SkillRecord,
        *,
        actor: str,
        reason: str,
        states: dict[str, dict[str, Any]] | None = None,
    ) -> str:
        """归档一个技能（搬目录 + 记账 + 改状态）。

        ``states`` 由 `run()` 传进来：它已经持有状态表快照，若这里再走
        ``set_state``（内部 load→改→save）就会在 `run()` 最后那次落盘时被旧快照覆盖，
        归档状态会静默丢失。这是一个**踩到过**的 bug，不是防御性编程。
        """
        if record.tier == "external":
            # Hermes: "external skills are read-only to the curator"
            raise CuratorError(f"外部技能只读，curator 不归档：{record.name}")
        skill_dir = record.path.parent
        usage = self.read_usage(skill_dir)
        if usage.pinned:
            raise CuratorError(f"技能 {record.name} 已 pin，先 `orbit curator unpin` 再归档")
        destination = self._archive_dir(record.name)
        before = _file_manifest(skill_dir)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(skill_dir), str(destination))
        after = _file_manifest(destination)
        entry_id = self.record_ledger(
            action="archive",
            skill=record.name,
            actor=actor,
            evidence={"reason": reason, "tier": record.tier, "to": str(destination)},
            before=before,
            after=after,
        )
        if states is not None:
            self._apply_state(states, record.name, STATE_ARCHIVED, reason)
        else:
            self.set_state(record.name, STATE_ARCHIVED, reason)
        self.index.invalidate()
        return entry_id

    def _find_archived_dir(self, name: str) -> Path | None:
        """在归档区里找技能目录：先精确名，再找带时间戳后缀的。"""
        exact = self.archive_root / name
        if exact.is_dir():
            return exact
        if not self.archive_root.is_dir():
            return None
        candidates = sorted(
            path for path in self.archive_root.iterdir() if path.is_dir() and path.name.startswith(f"{name}-")
        )
        return candidates[-1] if candidates else None

    def restore(self, name: str, *, actor: str = ACTOR_USER) -> str:
        """从归档区恢复：搬回用户级根目录的**扁平布局**，记 ledger，状态改 active。

        为什么恢复成扁平布局（不重建原类目层级）：Hermes 的 ``restore_skill`` 就是这么
        做的，理由是类目层级只是展示分组，重建它需要额外记录原路径——而账本里的
        ``before`` 清单只保证文件内容可回滚，不保证目录结构。扁平化让"恢复"永远成功。
        """
        source = self._find_archived_dir(name)
        if source is None:
            raise CuratorError(f"归档区里没有技能：{name}")
        destination = self.index.user_root / name
        if destination.exists():
            raise CuratorError(f"用户级技能目录里已有同名技能，恢复会覆盖：{destination}")
        before = _file_manifest(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        after = _file_manifest(destination)
        entry_id = self.record_ledger(
            action="restore",
            skill=name,
            actor=actor,
            evidence={"from": str(source), "to": str(destination)},
            before=before,
            after=after,
        )
        self.set_state(name, STATE_ACTIVE, "从归档恢复")
        self.index.invalidate()
        return entry_id

    def rollback(self, entry_id: str, *, actor: str = ACTOR_USER) -> dict[str, Any]:
        """按 ledger 回滚一次操作（目前支持 archive / restore 的逆操作）。

        逆操作是**从账本推出来的**，不是硬编码的分支：``before`` 里的路径就是变更前的
        位置，``after`` 里的路径就是变更后的位置，把目录从后者搬回前者即可。
        """
        entry = self.find_ledger_entry(entry_id)
        if entry is None:
            raise CuratorError(f"账本里没有这条记录：{entry_id}")
        action = str(entry.get("action") or "")
        if action not in ROLLBACK_ACTIONS:
            raise CuratorError(
                f"action={action} 不可回滚（只有 {'/'.join(sorted(ROLLBACK_ACTIONS))} 可逆）"
            )
        before = entry.get("before") or []
        after = entry.get("after") or []
        if not before or not after:
            raise CuratorError(f"账本记录缺少 before/after 清单，无法回滚：{entry_id}")
        current = Path(str(after[0].get("path") or "")).parent
        original = Path(str(before[0].get("path") or "")).parent
        if not current.is_dir():
            raise CuratorError(f"当前路径已不存在，无法回滚：{current}")
        if original.exists():
            raise CuratorError(f"回滚目标已存在，拒绝覆盖：{original}")
        original.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(current), str(original))
        skill = str(entry.get("skill") or "")
        new_id = self.record_ledger(
            action="rollback",
            skill=skill,
            actor=actor,
            evidence={"entry_id": entry_id, "reverted_action": action},
            before=_file_manifest(current),
            after=_file_manifest(original),
        )
        self.set_state(
            skill, STATE_ACTIVE if action == "archive" else STATE_ARCHIVED, f"回滚 {entry_id}"
        )
        self.index.invalidate()
        return {
            "entry_id": entry_id,
            "rollback_id": new_id,
            "skill": skill,
            "action": action,
            "from": str(current),
            "to": str(original),
        }

    # —— pin ——

    def _set_pinned(self, name: str, pinned: bool, *, actor: str) -> str:
        result = self.index.index()
        record = result.find(name)
        if record is None:
            raise CuratorError(f"技能不存在：{name}")
        skill_dir = record.path.parent
        usage = self.read_usage(skill_dir)
        if usage.pinned == pinned:
            # 幂等：重复 pin 不写账本，否则账本会被无意义的重复条目灌满
            return ""
        usage.pinned = pinned
        self.write_usage(skill_dir, usage)
        return self.record_ledger(
            action="pin" if pinned else "unpin",
            skill=name,
            actor=actor,
            evidence={"tier": record.tier},
            before=[],
            after=[],
        )

    def pin(self, name: str, *, actor: str = ACTOR_USER) -> str:
        """pin 住：豁免**每一次**自动迁移（Hermes：exempt from every auto-transition）。"""
        return self._set_pinned(name, True, actor=actor)

    def unpin(self, name: str, *, actor: str = ACTOR_USER) -> str:
        return self._set_pinned(name, False, actor=actor)

    # —— 状态迁移 ——

    def decide(
        self, name: str, current: str, usage: UsageRecord, now: datetime
    ) -> tuple[str | None, str]:
        """算出目标状态与理由。返回 ``(None, 理由)`` 表示不迁移。

        判定顺序是**有讲究的**，不是随手排的：

        1. pinned → 不动（豁免一切）
        2. 已归档 → 不动（恢复是显式动作，自动"复活"会让归档失去意义）
        3. unreviewed → active：只要有过真实活动
        4. **stale 且连续失败已清零 → active**：这一步必须排在归档判定之前。
           理由是"最新的证据是它刚成功"——若先跑成功率归档，一个 2 失败 1 成功的
           技能（成功率 0.33）会在刚刚成功之后立刻被归档，观感与逻辑都说不过去。
        5. 归档 ①：成功率 < 阈值且有失败样本（长期质量差）
        6. 归档 ②：闲置 > ``archive_days`` 且 ``use_count > 0``（"从未用过" ≠ "陈旧"）
        7. stale：连续失败 ≥ ``stale_failures``
        8. 其余不动
        """
        if usage.pinned:
            return None, "已 pin，豁免自动迁移"
        if current == STATE_ARCHIVED:
            return None, "已归档，恢复需显式 `curator restore`"
        if current == STATE_UNREVIEWED:
            if usage.activity_count > 0 or usage.successes > 0:
                return STATE_ACTIVE, "首次使用：已产生真实活动"
            return None, "尚未使用过（unreviewed 保持）"
        if current == STATE_STALE and usage.consecutive_failures == 0:
            return STATE_ACTIVE, "连续失败已清零，恢复为 active"
        # 归档判定 ①：成功率过低且有失败样本（**且样本量够**，否则与 stale 打架）
        if (
            usage.failures > 0
            and usage.outcome_total >= ARCHIVE_MIN_SAMPLES
            and usage.success_rate < self.archive_min_success_rate
        ):
            return (
                STATE_ARCHIVED,
                f"成功率 {usage.success_rate:.2f} < {self.archive_min_success_rate:.2f}"
                f"（失败 {usage.failures} 次 / 成功 {usage.successes} 次，"
                f"共 {usage.outcome_total} 个样本）",
            )
        # 归档判定 ②：闲置过久。**use_count == 0 永不因未使用归档**——"从未用过"≠"陈旧"
        idle_days = usage.idle_days(now)
        if idle_days is not None and idle_days > self.archive_days and usage.use_count > 0:
            return (
                STATE_ARCHIVED,
                f"闲置 {idle_days:.0f} 天 > {self.archive_days} 天（锚点不含文件 mtime）",
            )
        if usage.consecutive_failures >= self.stale_failures:
            return (
                STATE_STALE,
                f"连续失败 {usage.consecutive_failures} 次 ≥ {self.stale_failures}",
            )
        return None, "无需迁移"

    def run(self, *, dry_run: bool = True) -> list[dict[str, Any]]:
        """扫全部技能 → 算状态迁移 → ``dry_run=False`` 才落盘。

        返回 ``[{name, from, to, reason}]``。``dry_run`` 默认 True 是刻意的：
        生命周期治理是会**搬目录**的操作，默认值必须是"只看不动"。
        """
        now = self.now()
        states = self._load_states()
        transitions: list[dict[str, Any]] = []
        result = self.index.index()
        for record in result.records:
            skill_dir = record.path.parent
            usage = self.read_usage(skill_dir)
            current = self._state_from(states, record.name)
            target, reason = self.decide(record.name, current, usage, now)
            if target is None or target == current:
                continue
            transitions.append(
                {"name": record.name, "from": current, "to": target, "reason": reason}
            )
            if dry_run:
                continue
            if target == STATE_ARCHIVED:
                try:
                    self._archive_record(
                        record, actor=ACTOR_CURATOR, reason=reason, states=states
                    )
                except CuratorError as exc:
                    # 归档失败（pin 住/外部只读）不该中断整轮：其它技能的迁移照常进行
                    transitions[-1]["reason"] = f"{reason}（归档失败：{exc}）"
                continue
            self._apply_state(states, record.name, target, reason)
        if not dry_run:
            self._save_states(states)
        return transitions

    def prune(self, *, days: int | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
        """批量归档闲置 ≥ N 天的技能（``--days`` 默认 90，Hermes CLI 同值）。

        与 `run()` 的区别：`prune` **只看闲置**，不看成功率、不看 stale；
        它是用户显式发起的"清一清"，判据单一才解释得清。
        """
        threshold = self.archive_days if days is None else int(days)
        now = self.now()
        states = self._load_states()
        result = self.index.index()
        archived: list[dict[str, Any]] = []
        for record in result.records:
            skill_dir = record.path.parent
            usage = self.read_usage(skill_dir)
            current = self._state_from(states, record.name)
            if usage.pinned or current == STATE_ARCHIVED:
                continue
            idle_days = usage.idle_days(now)
            # 从未用过 ≠ 陈旧：没有使用记录就没有"闲置"可言
            if idle_days is None or usage.use_count == 0:
                continue
            if idle_days < threshold:
                continue
            reason = f"闲置 {idle_days:.0f} 天 ≥ {threshold} 天"
            archived.append(
                {"name": record.name, "from": current, "to": STATE_ARCHIVED, "reason": reason}
            )
            if not dry_run:
                try:
                    self._archive_record(record, actor=ACTOR_CURATOR, reason=reason)
                except CuratorError as exc:
                    archived[-1]["reason"] = f"{reason}（归档失败：{exc}）"
        return archived

    # —— 报表 ——

    def status(self) -> dict[str, Any]:
        """治理概览：状态分布、pin 数、阈值、归档区、账本条数。"""
        result = self.index.index()
        states = self._load_states()
        counts = {state: 0 for state in VALID_STATES}
        pinned = 0
        for record in result.records:
            state = self._state_from(states, record.name)
            counts[state] = counts.get(state, 0) + 1
            if self.read_usage(record.path.parent).pinned:
                pinned += 1
        archived_on_disk = 0
        if self.archive_root.is_dir():
            archived_on_disk = sum(1 for path in self.archive_root.iterdir() if path.is_dir())
        return {
            "skills": len(result.records),
            "states": counts,
            "pinned": pinned,
            "archived_on_disk": archived_on_disk,
            "errors": len(result.errors),
            "shadowed": len(result.shadowed),
            "untrusted": list(result.untrusted),
            "state_records": len(states),
            "ledger_entries": len(self.ledger_entries(limit=10_000)),
            "thresholds": {
                "stale_failures": self.stale_failures,
                "archive_days": self.archive_days,
                "archive_min_success_rate": self.archive_min_success_rate,
                "archive_min_samples": ARCHIVE_MIN_SAMPLES,
            },
        }

    def usage_report(
        self, *, sort: str = "activity", provenance: str | None = None
    ) -> list[dict[str, Any]]:
        """全技能使用遥测报表。

        ``provenance`` 的映射必须说明白（Orbit 没有 hub 通道、也没有 bundled 播种）：

        - ``agent``   → ``created_by == "agent"``（curator 可接管的那批）
        - ``bundled`` → 无归属标记（``created_by`` 为空）的技能，近似 Hermes 的
          "不是 curator 造的那批"
        - ``hub``     → 恒为空集合。这是**事实**，不是 bug：本库没有远端安装通道。

        假装 `--provenance hub` 有内容，比让它空着更糟。
        """
        now = self.now()
        states = self._load_states()
        rows: list[dict[str, Any]] = []
        for record in self.index.index().records:
            usage = self.read_usage(record.path.parent)
            anchor = usage.anchor()
            rows.append(
                {
                    "name": record.name,
                    "tier": record.tier,
                    "provenance": usage.created_by or "",
                    "state": self._state_from(states, record.name),
                    "use_count": usage.use_count,
                    "view_count": usage.view_count,
                    "patch_count": usage.patch_count,
                    "successes": usage.successes,
                    "failures": usage.failures,
                    "success_rate": round(usage.success_rate, 2),
                    "activity": usage.activity_count,
                    "pinned": usage.pinned,
                    "idle_days": None
                    if anchor is None
                    else round((now - anchor).total_seconds() / 86400.0, 1),
                    "last_active_at": anchor.isoformat() if anchor else "",
                }
            )
        if provenance == "agent":
            rows = [row for row in rows if row["provenance"] == "agent"]
        elif provenance == "bundled":
            rows = [row for row in rows if not row["provenance"]]
        elif provenance == "hub":
            rows = []
        if sort == "recent":
            rows.sort(key=lambda row: (row["last_active_at"], row["name"]), reverse=True)
        elif sort == "name":
            rows.sort(key=lambda row: row["name"])
        else:
            rows.sort(key=lambda row: (-int(row["activity"]), row["name"]))
        return rows


__all__ = [
    "ACTOR_AGENT",
    "ACTOR_CURATOR",
    "ACTOR_USER",
    "ARCHIVE_DAYS",
    "ARCHIVE_DIRNAME",
    "ARCHIVE_MIN_SAMPLES",
    "ARCHIVE_MIN_SUCCESS_RATE",
    "Curator",
    "CuratorError",
    "LEDGER_FIELDS",
    "LEDGER_FILENAME",
    "ROLLBACK_ACTIONS",
    "STALE_FAILURES",
    "STATE_ACTIVE",
    "STATE_ARCHIVED",
    "STATE_FILENAME",
    "STATE_STALE",
    "STATE_UNREVIEWED",
    "USAGE_ANCHOR_FIELDS",
    "USAGE_FIELDS",
    "USAGE_FILENAME",
    "VALID_ACTORS",
    "VALID_STATES",
    "LedgerEntry",
    "UsageRecord",
    "make_ledger_id",
    "parse_iso",
]
