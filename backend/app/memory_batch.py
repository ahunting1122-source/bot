"""记忆的原子批处理与 consolidation 熔断（MATRIX 9.4 / 9.5）。

对标 Hermes `tools/memory_tool.py` 的两个机制：

1. **`apply_batch`**：一次调用里 add / update / delete 混合执行，**按最终态校验预算**，
   全有或全无。为什么需要它——模型写满 2,200 字符后要腾挪空间，如果没有批处理，
   它只能"先 remove 一次、再 add 一次"地来回调用，每一轮都要重发整段上下文；
   而中间态（删了还没加）一旦超限，第二步就会被拒，模型于是反复抖动。
   批处理把"腾挪"压成一次调用，中间态是否超限无关紧要。

2. **`ConsolidationBreaker`**：一轮内 consolidation 连续失败 3 次就熔断，本轮不再尝试，
   下一轮重置。为什么需要它——记忆写入是**副作用**，副作用不能阻断回复
   （Hermes issue #42405）。没有熔断时，模型会一直重试"整理记忆"，把一轮对话拖死。

## 原子性怎么保证

三段式，且**校验阶段一次 IO 都不写**：

1. **读快照**：`backend.all_entries()` 拿当前全量记忆（预算要按全库算，不能只看改动的 key）。
2. **纯函数推演**：`plan_batch()` 在一份内存副本上按顺序施加所有操作，逐条校验
   （key ≤160、value ≤4000、条数上限、总字符 ≤2,200），最后**只对最终态**校验预算。
   任何一条不合法 → 立即返回，`wrote=False`，磁盘上什么都没发生。
3. **提交与补偿**：全部合法才 `backend.commit(mutations)`。`SqlMemoryBackend.commit`
   把整批写在**一个事务**里，异常时 SQLite 自己回滚；除此之外 `apply_batch` 还会用
   步骤 1 的快照调 `backend.restore()` 做一次**补偿写**——单事务能兜住数据库，
   兜不住"后端只提交了一半"的将来（比如有人把 backend 换成 HTTP provider）。
   补偿是幂等的：把改过的 key 写回旧值、把新建的 key 删掉。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select

from app.memory_prompt import MEMORY_MAX_CHARS, render, render_entry
from app.storage import Memory, now

log = logging.getLogger("orbit.memory_batch")

#: 单条 key 上限（与 `storage.Memory.key` 的 `String(160)` 一致）。
KEY_LIMIT = 160
#: 单条 value 上限。
VALUE_LIMIT = 4000
#: 一次批处理最多多少条操作。上限的意义不是"防大"而是"防抖"：
#: 一个批处理超过几十条，说明模型在拿批处理当脚本用，那不是它该干的事。
MAX_OPERATIONS = 64
#: 合法的操作名。
VALID_OPS = ("add", "update", "delete")

#: 一轮内允许的 consolidation 失败次数。第 3 次失败即熔断，第 4 次直接放弃。
_MAX_CONSOLIDATION_FAILURES_PER_TURN = 3

#: 熔断状态文件名。
BREAKER_NAME = "breaker.json"


# ── 批处理 ───────────────────────────────────────────────────────────────────


@dataclass
class BatchResult:
    """一次批处理的结果。`wrote` 与 `rolled_back` 是两个独立的观测位：

    - `wrote=False`：校验没过，**一个字节都没写**（`rolled_back` 也就无意义）；
    - `wrote=True, rolled_back=True`：写了但失败，已补偿回批处理前的状态；
    - `ok=True`：全部生效。
    """

    ok: bool
    applied: int = 0
    error: str = ""
    #: 出错的具体是第几条（1 基）。0 = 不是某一条的问题（比如总条数超限）。
    failed_op: int = 0
    wrote: bool = False
    rolled_back: bool = False
    #: 最终态（校验失败时是**批处理前**的状态，便于调用方回显给模型）。
    final: dict[str, str] = field(default_factory=dict)
    used: int = 0
    limit: int = MEMORY_MAX_CHARS

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "applied": self.applied,
            "error": self.error,
            "failed_op": self.failed_op,
            "wrote": self.wrote,
            "rolled_back": self.rolled_back,
            "used": self.used,
            "limit": self.limit,
            "keys": sorted(self.final),
        }


class MemoryBackend(Protocol):
    """批处理的落盘后端。三个方法就够：读全量、提交、补偿。"""

    async def all_entries(self) -> dict[str, str]: ...

    async def commit(self, mutations: list[tuple[str, str | None]]) -> None: ...

    async def restore(self, before: dict[str, str], created: list[str]) -> None: ...


class SqlMemoryBackend:
    """`memories` 表的批处理后端：整批写在一个事务里。"""

    def __init__(self, store: Any) -> None:
        self.store = store

    async def all_entries(self) -> dict[str, str]:
        rows = await self.store.rows(select(Memory).order_by(Memory.updated_at))
        return {str(row.key): str(row.value) for row in rows}

    async def commit(self, mutations: list[tuple[str, str | None]]) -> None:
        """`mutations` 是 `[(key, value|None)]`，`None` 表示删除。"""
        async with self.store.lock, self.store.sessions() as session:
            for key, value in mutations:
                row = await session.scalar(select(Memory).where(Memory.key == key))
                if value is None:
                    if row is not None:
                        await session.delete(row)
                    continue
                if row is None:
                    session.add(Memory(key=key, value=value, source="batch"))
                else:
                    row.value = value
                    row.updated_at = now()
            # 一次 commit 落整批：任何一步抛异常，这个事务整体不生效。
            await session.commit()

    async def restore(self, before: dict[str, str], created: list[str]) -> None:
        """补偿写：改过的 key 写回旧值，新建的 key 删掉。

        单事务已经把正常路径兜住了，这里防的是"提交了一半"的将来
        （换 HTTP / 文件后端时，它们没有事务）。补偿必须**幂等**，
        因为它可能在一个已经回滚干净的状态上再跑一次。
        """
        mutations: list[tuple[str, str | None]] = [(key, value) for key, value in before.items()]
        mutations += [(key, None) for key in created]
        if mutations:
            await self.commit(mutations)


def _normalize_operations(operations: Any) -> tuple[list[dict], str]:
    """把入参收敛成 `[{"op","key","value"}]`。非 list 一律判错。"""
    if not isinstance(operations, list):
        return [], "operations 必须是列表"
    normalized: list[dict] = []
    for item in operations:
        if not isinstance(item, dict):
            return [], "operations 里每一项都必须是对象"
        normalized.append(item)
    return normalized, ""


def _op_name(item: dict) -> str:
    """`op` 与 Hermes 的 `action` 两种写法都认（模型两种都可能给）。"""
    return str(item.get("op") or item.get("action") or "").strip().lower()


def plan_batch(
    current: dict[str, str],
    operations: Any,
    *,
    limit: int = MEMORY_MAX_CHARS,
) -> BatchResult:
    """**纯函数**：在一份内存副本上推演整批操作，不碰任何 IO。

    校验口径（逐条 + 最终态）：

    - key 非空、长度 ≤ `KEY_LIMIT`；value 长度 ≤ `VALUE_LIMIT`；
    - 操作条数 ≤ `MAX_OPERATIONS`；
    - `add` 要求 key **不存在**、`update` / `delete` 要求 key **存在**。为什么这么严：
      批处理是模型一次腾挪多条的路径，若 `add` 静默覆盖已存在的 key，就分不清
      "我改了一条"和"我加了一条新的"，回滚与审计都失去依据。报错里直接告诉它该用哪个 op。
    - 预算**只校验最终态**：中间态超限无所谓（先删后加正是批处理的用途）。
      预算用的是 `memory_prompt.render` 的判定——它才是决定"什么真的会被注入"的那段代码，
      这里若另写一套算法，两处迟早对不上。

    已知边界（写在这里免得下一个人当成 bug）：`memory_prompt.render_entry` 会把单条
    value 渲染截到 600 字符。所以一条 1,000 字符的 value 能过预算校验（渲染后只占 600），
    但注入时尾部 400 字符是看不到的。`VALUE_LIMIT`（4000）是**落库**上限，
    渲染上限在 `memory_prompt.VALUE_LIMIT`，两者刻意不同：前者管"能不能存"，
    后者管"注入时给模型看多少"。
    """
    original = dict(current)
    state = dict(current)
    baseline = render(list(original.items()), limit=limit).used

    def _fail(message: str, index: int = 0) -> BatchResult:
        """校验失败：`final` 一律回**批处理前**的状态。

        为什么不是"算到一半的中间态"：调用方（模型 / 用户）看到的必须是"磁盘上现在
        是什么"。回显中间态会让它以为批处理生效了一部分，下一次调用就会基于幻觉改数据。
        """
        return BatchResult(
            ok=False, error=message, failed_op=index, final=original, limit=limit, used=baseline
        )

    ops, error = _normalize_operations(operations)
    if error:
        return _fail(error)
    if not ops:
        return _fail("operations 列表为空")
    if len(ops) > MAX_OPERATIONS:
        return _fail(f"一次最多 {MAX_OPERATIONS} 条操作，收到 {len(ops)} 条")

    for index, item in enumerate(ops, 1):
        op = _op_name(item)
        key = str(item.get("key") or "").strip()
        raw_value = item.get("value")
        value = "" if raw_value is None else str(raw_value)
        where = f"第 {index} 条（{op or '未知操作'}）"

        if op not in VALID_OPS:
            return _fail(f"{where}：未知操作，只支持 {' / '.join(VALID_OPS)}", index)
        if not key:
            return _fail(f"{where}：key 不能为空", index)
        if len(key) > KEY_LIMIT:
            return _fail(f"{where}：key 长度 {len(key)} 超过上限 {KEY_LIMIT}", index)

        if op == "delete":
            if key not in state:
                return _fail(f"{where}：key「{key}」不存在，无法删除", index)
            state.pop(key)
            continue

        if not value.strip():
            return _fail(f"{where}：value 不能为空", index)
        if len(value) > VALUE_LIMIT:
            return _fail(f"{where}：value 长度 {len(value)} 超过上限 {VALUE_LIMIT}", index)
        if op == "add" and key in state:
            return _fail(f"{where}：key「{key}」已存在，改用 update（或先 delete）", index)
        if op == "update" and key not in state:
            return _fail(f"{where}：key「{key}」不存在，改用 add", index)
        state[key] = value

    # 只对**最终态**校验预算。
    fits, used = _fits(list(state.items()), limit)
    if not fits:
        return _fail(
            f"最终状态超出记忆预算：{limit} 字符上限，当前内容会被裁剪"
            "（批处理里先删几条或合并后再 add）"
        )

    return BatchResult(ok=True, applied=len(ops), final=state, used=used, limit=limit)


def _mutations_for(current: dict[str, str], final: dict[str, str], touched: list[str]) -> list[tuple[str, str | None]]:
    """把"前后差异"翻成后端能执行的 mutation 列表。"""
    mutations: list[tuple[str, str | None]] = []
    for key in touched:
        if key in final:
            if current.get(key) != final[key]:
                mutations.append((key, final[key]))
        elif key in current:
            mutations.append((key, None))
    return mutations


async def apply_batch(
    store: Any,
    operations: Any,
    *,
    limit: int = MEMORY_MAX_CHARS,
    backend: MemoryBackend | None = None,
) -> BatchResult:
    """原子批处理：按最终态校验，全有或全无。

    `store` 是 `app.storage.Store`；测试可以塞 `backend=` 换掉落盘实现
    （故意在第 N 次写失败，验证补偿真的把状态退回批处理前）。
    """
    engine: MemoryBackend = backend if backend is not None else SqlMemoryBackend(store)
    current = await engine.all_entries()
    planned = plan_batch(current, operations, limit=limit)
    if not planned.ok:
        return planned

    final = planned.final
    touched = sorted(set(current) | set(final))
    touched = [key for key in touched if current.get(key) != final.get(key)]
    mutations = _mutations_for(current, final, touched)
    if not mutations:
        # 批处理全部是幂等的空转（比如 update 成同样的值）：仍然算成功，但不必写盘。
        return planned

    before = {key: current[key] for key in touched if key in current}
    created = [key for key in touched if key not in current]
    try:
        await engine.commit(mutations)
    except Exception as exc:  # noqa: BLE001 —— 后端可以是任何东西，异常类型不可枚举
        log.warning("批处理写入失败，开始补偿：%s", exc)
        try:
            await engine.restore(before, created)
        except Exception as restore_error:  # 补偿也失败：如实报，不掩盖
            log.error("批处理补偿失败：%s", restore_error)
            return BatchResult(
                ok=False,
                error=f"写入失败（{exc}），补偿也失败（{restore_error}）：记忆可能处于中间状态",
                wrote=True,
                rolled_back=False,
                final=final,
                limit=limit,
                used=planned.used,
            )
        return BatchResult(
            ok=False,
            error=f"写入失败：{exc}（已回滚到批处理前）",
            wrote=True,
            rolled_back=True,
            final=current,
            limit=limit,
        )
    planned.wrote = True
    return planned


# ── consolidation ────────────────────────────────────────────────────────────


@dataclass
class ConsolidationResult:
    """整理结果。`kept` 是**整理后**的记忆（旧→新），三项计数必须自洽：

    `len(kept) + dropped == 去重后的条数`，`merged` 是被合并掉的条数（含在 dropped 里）。
    """

    kept: list[tuple[str, str]] = field(default_factory=list)
    dropped: int = 0
    merged: int = 0
    used: int = 0
    limit: int = MEMORY_MAX_CHARS
    #: 非空表示这次没真正整理（熔断打开 / 底层失败），值是原因。
    skipped: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "kept": [{"key": key, "value": value} for key, value in self.kept],
            "dropped": self.dropped,
            "merged": self.merged,
            "used": self.used,
            "limit": self.limit,
            "skipped": self.skipped,
            "error": self.error,
            "count": len(self.kept),
        }


def _prefix_of(key: str) -> str:
    """key 的层级前缀（第一个分隔符之前）。没有分隔符则返回空串。"""
    for separator in ("/", ":", ".", ">"):
        if separator in key:
            head = key.split(separator, 1)[0].strip()
            if head:
                return head
    return ""


def _fits(items: list[tuple[str, str]], limit: int) -> tuple[bool, int]:
    """最终态是否**完整**放进预算：返回 (放得下吗, 渲染后字符数)。

    判据用 `memory_prompt.render` 的结果，而不是自己再算一遍：
    它才是决定"什么真的会被注入"的那段代码，另写一套算法两处迟早对不上。
    `block.entries != expected` 抓的是"只剩一条也被截断"这种静默丢字。
    """
    expected = [line for line in (render_entry(k, v) for k, v in items) if line]
    block = render(items, limit=limit)
    return (block.dropped == 0 and block.entries == expected), block.used


def _last_segment(key: str) -> str:
    """key 的最后一段（`project/orbit/stack` → `stack`）。"""
    for separator in ("/", ":", ".", ">"):
        if separator in key:
            return key.rsplit(separator, 1)[1].strip() or key
    return key


def _merge_group(
    items: list[tuple[str, str]], prefix: str
) -> tuple[list[tuple[str, str]], int]:
    """把同一前缀的条目合并成一条，返回 (新列表, 被合并掉的条数)。

    key 取组内**最旧**那条（它在列表里靠前，位置稳定）；value 把各条的
    `最后一段: 正文` 用 `；` 连起来——这样"哪句话原来挂在哪个 key 上"没有丢，
    整理不等于抹掉出处。

    合并后的 value 超过 `VALUE_LIMIT` 就**放弃**这次合并：把一条记忆塞爆，
    等于用"一条超大记忆"换"几条小记忆"，预算没省下来还更难读。
    """
    indexes = [index for index, (key, _) in enumerate(items) if _prefix_of(key) == prefix]
    if len(indexes) < 2:
        return items, 0
    fragments: list[str] = []
    for index in indexes:
        key, value = items[index]
        fragment = f"{_last_segment(key)}: {value}" if value else _last_segment(key)
        if fragment not in fragments:
            fragments.append(fragment)
    combined = "；".join(fragments)
    if len(combined) > VALUE_LIMIT:
        return items, 0
    head = indexes[0]
    result = list(items)
    for index in reversed(indexes[1:]):
        result.pop(index)
    result[head] = (items[head][0], combined)
    return result, len(indexes) - 1


def consolidate(
    pairs: list[tuple[str, str]],
    *,
    limit: int = MEMORY_MAX_CHARS,
) -> ConsolidationResult:
    """把超预算的记忆合并/裁剪到预算内（**纯函数**，不写盘）。

    顺序（先合并、后裁剪）：合并保留的信息比裁剪多，所以能合就合、合不动才丢。

    1. **同 key 去重**：后出现的胜出（旧→新顺序下即"新的覆盖旧的"），计入 `merged`；
    2. **同前缀合并**：`project/orbit/stack` 与 `project/orbit/deploy` 这类同前缀条目
       合成一条，计入 `merged`。**只在超预算时才合并**——有空间时不动数据，
       整理不该悄悄改 key；
    3. **丢最旧**：仍然超预算时从列表**头部**（最旧）开始丢，计入 `dropped`。
       理由同 `memory_prompt`：新写入的在尾部，丢最新的会让用户以为"刚记的没生效"。
    """
    working: list[tuple[str, str]] = []
    seen: dict[str, int] = {}
    merged = 0
    for raw_key, raw_value in pairs:
        key = (raw_key or "").strip()
        if not key:
            continue
        value = " ".join((raw_value or "").split())
        if key in seen:
            working[seen[key]] = (key, value)
            merged += 1
            continue
        seen[key] = len(working)
        working.append((key, value))

    # 合并阶段：每轮合并一个（成员最多的）前缀组，直到放得下或合不动。
    while not _fits(working, limit)[0]:
        counts: dict[str, int] = {}
        for key, _ in working:
            prefix = _prefix_of(key)
            if prefix:
                counts[prefix] = counts.get(prefix, 0) + 1
        candidates = sorted(
            (prefix for prefix, count in counts.items() if count >= 2),
            key=lambda prefix: (-counts[prefix], prefix),
        )
        progressed = False
        for prefix in candidates:
            merged_items, removed = _merge_group(working, prefix)
            if removed:
                working, merged, progressed = merged_items, merged + removed, True
                break
        if not progressed:
            break

    # 裁剪阶段：从最旧的开始丢。
    dropped = 0
    while working and not _fits(working, limit)[0]:
        working.pop(0)
        dropped += 1

    used = _fits(working, limit)[1] if working else 0
    return ConsolidationResult(
        kept=list(working), dropped=dropped, merged=merged, used=used, limit=limit
    )


# ── 熔断 ─────────────────────────────────────────────────────────────────────


class ConsolidationBreaker:
    """一轮 3 次 consolidation 失败就熔断；熔断后本轮不再尝试，下一轮重置。

    状态落在 `<data_dir>/memory/breaker.json`（跨进程可见：CLI 一次调用一个进程，
    熔断状态必须留在盘上才有意义）。

    「一轮」由调用方给一个 `turn` 标识（任务 id / 时间戳都行）。判定规则只有两条：

    - **同一轮**内失败累计到 `_MAX_CONSOLIDATION_FAILURES_PER_TURN`（=3）→ `tripped=True`；
    - `turn` 变了 → 计数归零（新一轮），`record_success` 也归零。

    为什么"3 次"而不是"1 次"：一次失败可能只是这次合并策略没合好，模型换个切法
    就能成；但连续 3 次说明**这条路走不通**，继续重试只会把一轮对话拖死。
    Hermes 的 `_MAX_CONSOLIDATION_FAILURES_PER_TURN = 3` 就是这个经验值。
    """

    def __init__(self, data_dir: Any = None, *, path: Any = None) -> None:
        if path is not None:
            self.path: Path | None = Path(path)
        elif data_dir is not None:
            self.path = Path(data_dir) / "memory" / BREAKER_NAME
        else:
            # 没有落点 = 纯内存熔断（测试 / 无 data_dir 的场景）。
            self.path = None
        self._memory: dict = {"turn": "", "failures": 0, "tripped": False, "updated_at": ""}

    # ── 读写状态 ──
    def _read(self) -> dict:
        if self.path is None or not self.path.exists():
            return dict(self._memory)
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("熔断状态读失败，按未熔断处理：%s", self.path)
            return {"turn": "", "failures": 0, "tripped": False, "updated_at": ""}
        if not isinstance(data, dict):
            return {"turn": "", "failures": 0, "tripped": False, "updated_at": ""}
        return {
            "turn": str(data.get("turn") or ""),
            "failures": int(data.get("failures") or 0),
            "tripped": bool(data.get("tripped")),
            "updated_at": str(data.get("updated_at") or ""),
        }

    def _write(self, state: dict) -> dict:
        state = dict(state)
        state["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        self._memory = state
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        return state

    def _for_turn(self, turn: str) -> dict:
        """取这一轮的状态；`turn` 变了就当作新的一轮（计数归零，但不写盘）。"""
        state = self._read()
        if state["turn"] != turn:
            return {"turn": turn, "failures": 0, "tripped": False, "updated_at": state["updated_at"]}
        return state

    # ── 对外 ──
    def state(self, turn: str = "") -> dict:
        """当前轮的状态。给 `turn` 时按那一轮的口径返回（换轮视为已重置）。"""
        state = self._read() if not turn else self._for_turn(turn)
        state = dict(state)
        state["max_failures"] = _MAX_CONSOLIDATION_FAILURES_PER_TURN
        state["path"] = str(self.path) if self.path else ""
        return state

    def should_attempt(self, turn: str) -> bool:
        """本轮还能不能尝试 consolidation。熔断打开即 False。"""
        return not self._for_turn(turn)["tripped"]

    def record_failure(self, turn: str) -> dict:
        state = self._for_turn(turn)
        state["turn"] = turn
        state["failures"] = int(state["failures"]) + 1
        if state["failures"] >= _MAX_CONSOLIDATION_FAILURES_PER_TURN:
            state["tripped"] = True
        return self._write(state)

    def record_success(self, turn: str) -> dict:
        """成功即归零（Hermes 的 `_success_response()` 同一个语义）。"""
        return self._write({"turn": turn, "failures": 0, "tripped": False, "updated_at": ""})

    def reset(self, turn: str = "") -> dict:
        """手动复位。CLI `memory breaker --reset` 用它，也用于测试。"""
        return self._write({"turn": turn, "failures": 0, "tripped": False, "updated_at": ""})


async def consolidate_guarded(
    breaker: ConsolidationBreaker,
    pairs: list[tuple[str, str]],
    *,
    limit: int = MEMORY_MAX_CHARS,
    turn: str = "",
    merge: Any = None,
) -> ConsolidationResult:
    """带熔断的 consolidation：熔断打开时**根本不调用** `merge`。

    "不再尝试"必须是"不调用"，而不是"调用了但丢弃结果"——后者照样烧 CPU、
    照样可能写盘，熔断就形同虚设。`merge` 可注入，测试用它数调用次数。
    """
    if not breaker.should_attempt(turn):
        state = breaker.state(turn)
        return ConsolidationResult(
            limit=limit,
            skipped=f"本轮已失败 {state['failures']} 次，熔断中（下一轮自动重置）",
        )
    worker = merge if merge is not None else consolidate
    try:
        result = worker(pairs, limit=limit)
    except Exception as exc:  # noqa: BLE001 —— 任何整理失败都只记一次，不向上冒
        state = breaker.record_failure(turn)
        log.warning("consolidation 失败（本轮第 %s 次）：%s", state["failures"], exc)
        return ConsolidationResult(limit=limit, error=str(exc), skipped="consolidation 失败")
    breaker.record_success(turn)
    return result
