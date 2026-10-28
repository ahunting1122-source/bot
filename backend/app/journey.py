"""学习时间线（journey）：把「技能什么时候被学会、用过、改过、提升、归档」拼成一条线。

对标 Hermes `hermes journey`（帮助原文：「A terminal rendition of the desktop
Star Map / Memory Graph: a timeline bar chart of learned skills and memories over
time … Mirrors the TUI `/journey` overlay and the desktop panel」）。规格出处：
`handoff/hermes-parity/research/04-skill-layer.md` §7.5。

## 命令别名（**写在这里是因为它会让人找不到实现**）

`orbit learning` 与 `orbit memory-graph` 都是 **`orbit journey` 的别名**
（声明表里 `journey` 的 `aliases=("learning", "memory-graph")`，MATRIX 7.17 已记录）。
三条路径是**同一个 handler**：Hermes 的三个帮助输出逐字节相同（调研 04 §7.5 的实测
结论），所以这里不复制三份实现——复制之后必然有一份先腐烂。

## 时间线的数据来源（只有两处真值）

| 来源 | 读什么 | 得到哪些 kind |
|---|---|---|
| 每个技能的 `.usage.json` | `created_at` / `last_used_at` / `last_patched_at` | `created` / `used` / `patched` |
| `<data_dir>/skills-state.json` | `state` + `updated_at` + `reason` | `promoted`（→ active）/ `archived` |
| `<data_dir>/skills-ledger.jsonl` | `ts` / `action` / `evidence.reason` | `archived`（archive）/ `promoted`（restore、rollback）/ `patched`（其余动作） |

这三处都是**审计数据**：`.usage.json` 是遥测、`skills-state.json` 是治理态、ledger
是逐变更账本。时间线只是它们的**视图**，自己**不存任何事实**。

## 为什么 edit / delete 只改旁注文件

`edit(entry_id, note)` 与 `delete(entry_id)` **只写** ``<data_dir>/journey-notes.json``
（旁注 + 墓碑标记），绝不回写 `.usage.json` 或 ledger。理由是这一层最核心的承诺：

> **审计数据不可篡改。** ledger 的价值来自「它记录了真实发生过什么」；一旦允许
> 从时间线界面「删掉一条」，账本就变成了「用户希望发生过什么」。归档/恢复/回滚
> 的取证能力当场归零，而且是不可察觉地归零——文件还在，只是少了几行。

所以「删除」= 在这条时间线上打一个墓碑（`deleted: true`），列表不再显示它，但
`.usage.json` 与 ledger 一个字节都不动。要真的消除影响，请用 curator 的动作
（archive / restore），那是有账可查的路径。

`entry_id` 是**内容寻址**的（`sha256(ts|kind|skill|detail)[:16]`）：旁注因此能稳定
地挂在「同一个事件」上，即使时间线被重新推导一遍（换了天、补了遥测）也不会错位。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.curator import (
    STATE_ACTIVE,
    STATE_ARCHIVED,
    Curator,
    parse_iso,
)
from app.skill_index import SkillIndex

#: 旁注文件（时间线自己的唯一落盘物）。
NOTES_FILENAME = "journey-notes.json"
#: 五种事件类型（顺序即 CLI 图例顺序）。
KIND_CREATED = "created"
KIND_USED = "used"
KIND_PATCHED = "patched"
KIND_PROMOTED = "promoted"
KIND_ARCHIVED = "archived"
KINDS: tuple[str, ...] = (KIND_CREATED, KIND_USED, KIND_PATCHED, KIND_PROMOTED, KIND_ARCHIVED)
#: 默认回看窗口（天）。
DEFAULT_DAYS = 30
#: 读 ledger 的上限。账本是 append-only JSONL，一次全读会把大库拖慢；10k 条足够
#: 覆盖「学习时间线」这个用途（它看的是趋势，不是逐条取证）。
LEDGER_SCAN_LIMIT = 10_000
#: ledger action → 时间线 kind。未列出的动作归 ``patched``：时间线只有 5 类，
#: 而「治理标志变更」（pin/unpin）比正文修改更不该被隐藏——归到 patched 并写清
#: 动作名，比让它从时间线上消失诚实。
LEDGER_KIND: dict[str, str] = {
    "create": KIND_CREATED,
    "patch": KIND_PATCHED,
    "archive": KIND_ARCHIVED,
    "restore": KIND_PROMOTED,
    "rollback": KIND_PROMOTED,
    "pin": KIND_PATCHED,
    "unpin": KIND_PATCHED,
}


class JourneyError(RuntimeError):
    """时间线的可预期失败（节点 id 不存在等）。"""


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def make_entry_id(ts: str, kind: str, skill: str, detail: str) -> str:
    """内容寻址的条目 id：同样的事件必然得到同一个 id（旁注因此稳定）。"""
    payload = "|".join([ts, kind, skill, detail])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class TimelineEntry:
    """时间线上的一条事件。``note`` 来自旁注文件（不是审计数据）。"""

    entry_id: str
    ts: str
    kind: str
    skill: str
    detail: str = ""
    note: str = ""
    #: 解析后的时间戳，只用于排序与「多少天前」的显示，不进 JSON。
    moment: datetime = field(default=datetime(1970, 1, 1, tzinfo=UTC), compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "ts": self.ts,
            "kind": self.kind,
            "skill": self.skill,
            "detail": self.detail,
            "note": self.note,
        }


class Journey:
    """学习时间线。一个实例 = 一份 ``data_dir`` 的视图（自身不存事实）。"""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        index: SkillIndex | None = None,
        curator: Curator | None = None,
        now: Any = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.index = index if index is not None else SkillIndex(self.data_dir)
        self.curator = (
            curator if curator is not None else Curator(self.data_dir, index=self.index)
        )
        self._now = now

    def now(self) -> datetime:
        return self._now() if self._now is not None else datetime.now(UTC)

    @property
    def notes_path(self) -> Path:
        return self.data_dir / NOTES_FILENAME

    # —— 旁注文件（唯一的落盘物）——

    def _read_notes(self) -> dict[str, dict[str, Any]]:
        try:
            payload = json.loads(self.notes_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        raw = payload.get("notes") if isinstance(payload, dict) else None
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): dict(value) for key, value in raw.items() if isinstance(value, dict)
        }

    def _write_notes(self, notes: dict[str, dict[str, Any]]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "notes": notes}
        self.notes_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    # —— 事件收集 ——

    def _collect(self) -> list[TimelineEntry]:
        """从 usage / state / ledger 拼出全部事件（去重、按时间升序）。"""
        raw: list[tuple[str, str, str, str]] = []

        # ① 每个技能的 .usage.json：created / used / patched
        for record in self.index.index().records:
            usage = self.curator.read_usage(record.path.parent)
            if usage.created_at:
                raw.append(
                    (
                        usage.created_at,
                        KIND_CREATED,
                        record.name,
                        f"首次沉淀（created_by={usage.created_by or '未标记'}）",
                    )
                )
            if usage.last_used_at:
                raw.append(
                    (
                        usage.last_used_at,
                        KIND_USED,
                        record.name,
                        f"累计使用 {usage.use_count} 次"
                        f"（成功 {usage.successes} / 失败 {usage.failures}）",
                    )
                )
            if usage.last_patched_at:
                raw.append(
                    (
                        usage.last_patched_at,
                        KIND_PATCHED,
                        record.name,
                        f"改过 {usage.patch_count} 次",
                    )
                )

        # ② 治理态：active → promoted，archived → archived
        for name, entry in self.curator.state_entries().items():
            state = str(entry.get("state") or "")
            ts = str(entry.get("updated_at") or "")
            reason = str(entry.get("reason") or "")
            if state == STATE_ACTIVE:
                raw.append((ts, KIND_PROMOTED, name, f"提升为 active：{reason or '（无理由）'}"))
            elif state == STATE_ARCHIVED:
                raw.append((ts, KIND_ARCHIVED, name, f"归档：{reason or '（无理由）'}"))

        # ③ ledger：逐变更账本（archive/restore/rollback/pin/…）
        for item in self.curator.ledger_entries(limit=LEDGER_SCAN_LIMIT):
            action = str(item.get("action") or "")
            if not action:
                continue
            kind = LEDGER_KIND.get(action, KIND_PATCHED)
            evidence = item.get("evidence") or {}
            reason = str(evidence.get("reason") or "") if isinstance(evidence, dict) else ""
            detail = f"ledger {action}" + (f"：{reason}" if reason else "")
            raw.append((str(item.get("ts") or ""), kind, str(item.get("skill") or ""), detail))

        entries: list[TimelineEntry] = []
        seen: set[str] = set()
        for ts, kind, skill, detail in raw:
            moment = parse_iso(ts)
            # 没有时间戳的事件进不了时间线：一条不知道何时发生的事，放在哪一天都是错的
            if moment is None or not skill.strip():
                continue
            entry_id = make_entry_id(ts, kind, skill, detail)
            if entry_id in seen:
                continue
            seen.add(entry_id)
            entries.append(
                TimelineEntry(
                    entry_id=entry_id,
                    ts=ts,
                    kind=kind,
                    skill=skill,
                    detail=detail,
                    moment=moment,
                )
            )
        entries.sort(key=lambda item: (item.moment, item.entry_id))
        return entries

    def entries(self) -> list[TimelineEntry]:
        """全部事件（不带旁注、不过滤墓碑）。给诊断用。"""
        return self._collect()

    def find(self, entry_id: str) -> TimelineEntry:
        """按 id 取一条事件（不存在则抛 ``JourneyError``）。"""
        clean = (entry_id or "").strip()
        for entry in self._collect():
            if entry.entry_id == clean:
                return entry
        raise JourneyError(
            f"时间线里没有这个节点 id：{clean or '（空）'}（`orbit journey list` 可查）"
        )

    # —— 对外 ——

    def list(self, days: int = DEFAULT_DAYS) -> list[TimelineEntry]:
        """最近 ``days`` 天的事件（最旧在上、最新在下），带旁注、不含墓碑。"""
        window = max(1, int(days))
        cutoff = self.now() - timedelta(days=window)
        notes = self._read_notes()
        rows: list[TimelineEntry] = []
        for entry in self._collect():
            note = notes.get(entry.entry_id) or {}
            if note.get("deleted"):
                continue
            if entry.moment < cutoff:
                continue
            rows.append(replace(entry, note=str(note.get("note") or "")))
        return rows

    def edit(self, entry_id: str, note: str) -> TimelineEntry:
        """给一条事件写旁注（**只写 journey-notes.json**，不动审计数据）。"""
        entry = self.find(entry_id)
        notes = self._read_notes()
        record = notes.setdefault(entry.entry_id, {})
        record["note"] = (note or "").strip()
        record["updated_at"] = _iso(self.now())
        # 写旁注顺带撤销墓碑：用户对一条已删除的条目写旁注，意图显然是「我要它回来」
        record.pop("deleted", None)
        record.pop("deleted_at", None)
        self._write_notes(notes)
        return replace(entry, note=record["note"])

    def delete(self, entry_id: str) -> bool:
        """从时间线上隐藏一条事件（**墓碑，不是删除**）。

        返回是否新打了一个墓碑（重复删除返回 False，幂等）。
        """
        entry = self.find(entry_id)
        notes = self._read_notes()
        record = notes.setdefault(entry.entry_id, {})
        already = bool(record.get("deleted"))
        record["deleted"] = True
        record["deleted_at"] = _iso(self.now())
        self._write_notes(notes)
        return not already


__all__ = [
    "DEFAULT_DAYS",
    "KINDS",
    "KIND_ARCHIVED",
    "KIND_CREATED",
    "KIND_PATCHED",
    "KIND_PROMOTED",
    "KIND_USED",
    "LEDGER_KIND",
    "LEDGER_SCAN_LIMIT",
    "NOTES_FILENAME",
    "Journey",
    "JourneyError",
    "TimelineEntry",
    "make_entry_id",
]
