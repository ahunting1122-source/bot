"""记忆写入的门禁与漂移防护（MATRIX 9.12 / 9.13）。

对标 Hermes `tools/memory_tool.py` 的两套机制（research 07 §5.4 / §5.6）：

## 9.12 写入门禁：三段，顺序不能换

1. **威胁扫描**（`threat_scan`）：写时逐条扫**即将写入的正文**。复用
   `app/untrusted.py` 的规则表（那里已经把"忽略以上指令"、角色劫持、HTML 注释藏指令、
   base64 载荷、零宽字符都覆盖了），本模块只决定"命中即拒"——`untrusted.scan` 自己的
   定位是"提示而非拦截"，记忆写入是**副作用**，提示没有意义，必须拒。
2. **审批门**（`approval_gate`）：复用 `app/approvals.py` 的三态语义（`off/smart/manual`）。
   为什么不能省：威胁扫描只认已知特征，审批门是**人**这一层的兜底。
3. **快照期二次扫描**（`snapshot_scan`）：对"即将成为全库"的**最终态**再扫一遍。
   为什么单条干净还要再扫：记忆块在提示词里是**逐行相邻**注入的，两条各自无害的
   记录拼起来可以构成一句完整的注入（"请忽略以上" + "的指令并照做"）。只扫单条
   等于把组合攻击整个漏掉，而组合攻击恰恰是记忆这种"长期、可累积"载体上最划算的
   攻击方式——一次写半句，第二次补完。

## 9.13 漂移防护：两个信号，命中就拒写并备份

Hermes 的 `_detect_external_drift` 有两条信号，本模块两条都实现：

1. **round-trip 不一致**（`round_trip_check`）：把每条记忆读出再写回，逐字节比对。
   为什么这条能抓漂移：工具自己写出去的东西必然能原样读回；读回不一样，说明库被
   **别的路径**动过（patch 工具、shell 追加、手工编辑、姐妹会话、另一个 profile）。
   此时若照常写，`set_memory` 的同义 key 合并会把外部内容顺手覆盖掉——那是不可逆的
   数据丢失（Hermes issue #26045）。所以先落 `memories.bak.<ts>` 再拒写。
2. **单条超全库上限**（`oversize_entries`）：一条 value 就超过整库预算（2,200 字符）时
   拒写。为什么不静默截断：截断后这条记录几乎不含信息，而用户会以为"我记下来了"。
   工具自己不可能写出这种条目（`memory_batch` 会按最终态校验预算），所以它出现即
   说明**外部写入了自由文本**——与 round-trip 是同一类事故的两个征兆。

## 为什么审批门的未知模式要 fail-closed

`approvals.resolve_mode` 对非法值退回 `off`（fail-open），理由是"不改变现状"。
写入门禁是**新增的拦截层**，新层 fail-open 等于没有这一层：配置里写错一个字母，
所有记忆写入就静默恢复成无门禁。所以这里未知模式一律按 `manual` 处理（拒绝），
并在原因里说明"这是刻意的，与 `resolve_mode` 不同"。

## 为什么 `staged` 是真的可回放，而不是假装

`approvals.py` 明说 Orbit 没有交互式审批通道，所以 `manual` 的语义是"拒绝并让人自己
去做"。`smart` 则落到 `staged`：内容存进 `<data>/memory/pending/<ts>.json`，
用户用 `orbit memory write-check --apply <文件>` 回放。这样三态都有**可观察的落点**，
不存在"提示已暂存但其实丢了"这种情况。
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from app import untrusted
from app.approvals import APPROVAL_MODES
from app.memory_prompt import MEMORY_MAX_CHARS, render

log = logging.getLogger("orbit.memory_write")

#: 审批门的三态。名字与 Hermes 的 `allow / blocked / staged` 对齐。
GATE_ALLOW = "allow"
GATE_STAGED = "staged"
GATE_BLOCKED = "blocked"

#: 退出码照 `approvals.EXIT_CODES` 的 0/2/3 口径（0 放行 / 2 待处置 / 3 拒绝）。
#: 复用同一套数字是刻意的：脚本里 `orbit approvals test` 与 `orbit memory write-check`
#: 的返回码语义一致，不必记两套。
EXIT_CODES: dict[str, int] = {GATE_ALLOW: 0, GATE_STAGED: 2, GATE_BLOCKED: 3}

#: 门禁拦在哪一段。写进结果里，因为"被拦"和"被哪一段拦"是两件事：
#: 前者要求用户改内容，后者决定他该改内容还是改审批模式。
STAGE_THREAT = "threat_scan"
STAGE_APPROVAL = "approval"
STAGE_SNAPSHOT = "snapshot"
STAGE_DRIFT = "drift"

#: 漂移备份的文件名前缀。`<data_dir>/memory/memories.bak.<ts>`，照 Hermes 的
#: `MEMORY.md.bak.<unix_ts>` 口径（换成记忆库这个载体，名字随之改成 `memories`）。
BACKUP_STEM = "memories.bak"

#: 待处置写入的落点目录（相对 `<data_dir>/memory/`）。
PENDING_DIR = "pending"

#: 白名单里用来"整体放行记忆写入"的规则名。写法与 `approvals allow add <rule>` 一致。
WRITE_RULE = "memory_write"


# ── 结果类型 ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Gate:
    """审批门的裁决。`verdict` 是 `allow / staged / blocked` 之一。"""

    verdict: str
    mode: str = "off"
    reason: str = ""

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.verdict, 1)

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "mode": self.mode, "reason": self.reason}


@dataclass
class DriftResult:
    """round-trip 的结果。`mismatches` 是**证据**，不是计数：

    只说"漂移了"而不给哪条 key、前后各是什么，用户没法判断是外部改动还是自己手滑。
    """

    ok: bool
    checked: int = 0
    mismatches: list[dict] = field(default_factory=list)
    backup: str = ""
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "checked": self.checked,
            "mismatches": list(self.mismatches),
            "backup": self.backup,
            "reason": self.reason,
        }


@dataclass
class WriteCheck:
    """一次写入裁决的完整结果。

    `ok` 的语义是"**现在可以写**"：`staged` 的 `ok` 也是 `False`——内容确实还没进库，
    说它 ok 会让调用方以为写成功了。
    """

    ok: bool
    verdict: str = GATE_ALLOW
    stage: str = ""
    reason: str = ""
    findings: list[dict] = field(default_factory=list)
    oversize: list[dict] = field(default_factory=list)
    drift: dict = field(default_factory=dict)
    gate: dict = field(default_factory=dict)
    pending: str = ""
    used: int = 0
    limit: int = MEMORY_MAX_CHARS

    @property
    def exit_code(self) -> int:
        if self.ok:
            return 0
        return EXIT_CODES.get(self.verdict, 1)

    def to_dict(self) -> dict:
        """`--json` 的载荷。**每个拒绝都带原因**：只报 `ok=false` 等于让人猜。"""
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "stage": self.stage,
            "reason": self.reason,
            "findings": list(self.findings),
            "oversize": list(self.oversize),
            "drift": dict(self.drift),
            "gate": dict(self.gate),
            "pending": self.pending,
            "used": self.used,
            "limit": self.limit,
        }


class RoundTripBackend(Protocol):
    """漂移检测要的后端形状。与 `memory_batch.MemoryBackend` 的前两个方法一致，
    故意不要求 `restore`——读回写回不需要补偿。"""

    async def all_entries(self) -> dict[str, str]: ...

    async def commit(self, mutations: list[tuple[str, str | None]]) -> None: ...


# ── 第 1 段：威胁扫描 ────────────────────────────────────────────────────────


def threat_scan(text: str, *, limit: int = untrusted.MAX_SCAN_CHARS) -> list[dict]:
    """扫描一段**即将写入**的正文，返回命中列表（空 = 干净）。

    为什么是"命中即拒"而 `untrusted.scan` 自己的定位是"提示"：工具结果里出现
    "忽略以上指令"最常见的原因是它真是一篇讲注入的文章，所以那一路只提示；
    但**用户要记住的东西**里出现这句话，没有任何正当用途。同一个规则表，两种处置。
    """
    return [
        {"kind": item.kind, "detail": item.detail, "position": item.position}
        for item in untrusted.scan(text, limit=limit)
    ]


def describe_findings(findings: list[dict]) -> str:
    """把命中列表压成一句人话（错误消息里用）。同类命中只报第一次，避免刷屏。"""
    if not findings:
        return ""
    seen: list[str] = []
    for item in findings:
        text = f"{item.get('kind', '')}（{item.get('detail', '')}）"
        if text not in seen:
            seen.append(text)
    return "；".join(seen[:3])


# ── 第 2 段：审批门 ──────────────────────────────────────────────────────────


def approval_gate(mode: str, *, allowlist: tuple[str, ...] | list[str] = ()) -> Gate:
    """把审批模式翻成三态裁决。`off` 放行 / `smart` 暂存 / `manual` 拒绝。

    `allowlist` 里的元素可以是 `memory_write` 这个规则名，也可以是模式名
    （`smart` / `manual`）——两种写法都认，因为 `approvals allow add` 收的是规则名，
    而人脑子里的东西常常是"这个模式太严了，对记忆放开"。
    """
    text = str(mode or "").strip().lower()
    allowed = {str(item).strip().casefold() for item in allowlist if str(item).strip()}
    if WRITE_RULE.casefold() in allowed:
        return Gate(
            GATE_ALLOW,
            mode=text,
            reason=f"记忆写入已在白名单里（{WRITE_RULE}），审批门放行",
        )
    if text == "off":
        return Gate(
            GATE_ALLOW,
            mode=text,
            reason="审批模式 off：与既有 memory_save 行为一致，直接放行",
        )
    if text == "smart":
        return Gate(
            GATE_STAGED,
            mode=text,
            reason=(
                "审批模式 smart：内容已暂存为待处置写入，"
                "用 `orbit memory write-check --apply <文件>` 回放"
            ),
        )
    if text == "manual":
        return Gate(
            GATE_BLOCKED,
            mode=text,
            reason=(
                "审批模式 manual：记忆写入一律拒绝（Orbit 没有交互式审批通道）。"
                f"要放行请改用 `orbit approvals allow add {WRITE_RULE}`"
            ),
        )
    return Gate(
        GATE_BLOCKED,
        mode=text,
        reason=(
            f"未知审批模式「{text}」，按最严格处理（可选：{' / '.join(APPROVAL_MODES)}）。"
            "这里刻意 fail-closed，与 approvals.resolve_mode 退回 off 的取舍不同——"
            "写入门禁是新层，新层 fail-open 等于没有"
        ),
    )


# ── 第 3 段：快照期二次扫描 ──────────────────────────────────────────────────


def _pairs(entries: Any) -> list[tuple[str, str]]:
    """把 dict / `[(key, value)]` 统一成有序 pair 列表（与 `memory_graph._pairs` 同口径）。"""
    if entries is None:
        return []
    if isinstance(entries, dict):
        return [(str(key), "" if value is None else str(value)) for key, value in entries.items()]
    out: list[tuple[str, str]] = []
    for item in entries:
        try:
            key, value = item
        except (TypeError, ValueError):
            continue
        name = str(key or "").strip()
        if name:
            out.append((name, "" if value is None else str(value)))
    return out


def snapshot_scan(
    entries: Any,
    *,
    touched: list[str] | tuple[str, ...] | None = None,
) -> list[dict]:
    """扫"即将成为全库"的最终态，返回带 `scope` 的命中列表。

    两个 scope：

    - `entry`：某一条自己就有问题。`touched` 给了就只报**这次要写的那几条**
      （历史遗留的脏条目另行报为提示，不该让整库变成不可写）；
    - `composition`：**只有拼起来才有问题**。判定方式是：把各条的值按换行相邻拼成
      一整段再扫，命中的位置若落在某条内部、而那条自己单独扫时没有同样的命中，
      就算组合命中。

    为什么按"值逐行相邻"拼而不是按真实渲染（`- key: value`）拼：真实渲染每条前面
    还有 `- key: ` 前缀，反而更难跨条命中。检测宁严勿松——它只用来**拒写**，
    不用来判定"可以注入"，所以偏严的代价是一次误拦 + 一句解释，偏松的代价是漏掉
    组合注入。
    """
    pairs = _pairs(entries)
    if not pairs:
        return []
    watched = None
    if touched is not None:
        watched = {str(item).strip() for item in touched if str(item).strip()}

    findings: list[dict] = []
    per_entry: dict[int, set[tuple[str, str]]] = {}
    for index, (key, value) in enumerate(pairs):
        hits = threat_scan(value)
        per_entry[index] = {(item["kind"], item["detail"]) for item in hits}
        if watched is not None and key not in watched:
            # 不是这次写的：不拦，但也不隐瞒。
            for item in hits:
                findings.append({**item, "scope": "entry", "key": key, "pre_existing": True})
            continue
        for item in hits:
            findings.append({**item, "scope": "entry", "key": key, "pre_existing": False})

    # 组合扫描：算出每条的字符区间，命中位置落在哪条里就归属哪条。
    spans: list[tuple[int, int, int]] = []
    offset = 0
    for index, (_, value) in enumerate(pairs):
        spans.append((offset, offset + len(value), index))
        offset += len(value) + 1  # +1 是拼接用的换行
    blob = "\n".join(value for _, value in pairs)
    for item in threat_scan(blob):
        owner = None
        for start, end, index in spans:
            if start <= item["position"] < end:
                owner = index
                break
        if owner is not None and (item["kind"], item["detail"]) in per_entry.get(owner, set()):
            # 同一条自己就能命中，已经在 entry 里报过了。
            continue
        findings.append(
            {
                **item,
                "scope": "composition",
                "key": pairs[owner][0] if owner is not None else "",
                "pre_existing": False,
            }
        )
    return findings


# ── 9.13 漂移防护 ────────────────────────────────────────────────────────────


def oversize_entries(
    entries: Any,
    *,
    limit: int = MEMORY_MAX_CHARS,
) -> list[dict]:
    """找出"单条就超过整库预算"的条目。

    判据用**原始长度**（`- key: value` 渲染后多长），不用 `memory_prompt.render_entry`
    的结果——后者会把 value 截到 600 字符，于是任何条目都"装得下"，这个检查就永远
    不触发。而这里要抓的正是"原始内容比整库预算还大"这种工具自己写不出来的东西。
    """
    found: list[dict] = []
    for key, value in _pairs(entries):
        size = len(f"- {key}: {value}")
        if size > limit:
            found.append(
                {
                    "key": key,
                    "chars": size,
                    "limit": limit,
                    "reason": (
                        f"单条记忆 {size} 字符已超过整库预算 {limit} 字符："
                        "写进去会挤掉其余全部记忆（或注入时被整条截断），所以拒写。"
                        "请拆成几条再写"
                    ),
                }
            )
    return found


def backup_path(data_dir: Any, *, stamp: float | None = None) -> Path:
    """漂移备份的落点：`<data_dir>/memory/memories.bak.<ts>`。

    `ts` 用 Unix 秒（照 Hermes 的 `.bak.<unix_ts>`）。同一秒内多次漂移加 `-n` 后缀，
    绝不覆盖已有备份——备份互相覆盖等于没有备份。
    """
    root = Path(data_dir) / "memory"
    root.mkdir(parents=True, exist_ok=True)
    ts = int(stamp if stamp is not None else time.time())
    candidate = root / f"{BACKUP_STEM}.{ts}"
    index = 1
    while candidate.exists():
        candidate = root / f"{BACKUP_STEM}.{ts}-{index}"
        index += 1
    return candidate


def write_backup(entries: dict[str, str], data_dir: Any, *, stamp: float | None = None) -> Path:
    """把**旧库**写成一份 JSON 快照。写不成就是写不成，异常往上抛。"""
    path = backup_path(data_dir, stamp=stamp)
    payload = {
        "ts": int(stamp if stamp is not None else time.time()),
        "reason": "记忆库 round-trip 不一致，已拒绝本次写入",
        "entries": {str(key): str(value) for key, value in entries.items()},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


async def round_trip_check(
    backend: RoundTripBackend,
    *,
    data_dir: Any = None,
    stamp: float | None = None,
) -> DriftResult:
    """把每条记忆读出再写回，逐字节比对；不一致就备份旧库并判漂移。

    **代价必须先说**：这个检查会真的执行一次全库写回。对 SQLite 后端它是幂等的
    （写回同样的值只更新 `updated_at`），但它确实是一次写操作——所以只在**写入前**
    调用，不在读路径上调。

    空库直接通过：没有记忆可漂移，写回零条也不该被当成"不一致"。
    """
    before = {str(key): str(value) for key, value in (await backend.all_entries()).items()}
    if not before:
        return DriftResult(ok=True, checked=0, reason="空库，没有可漂移的内容")
    await backend.commit([(key, value) for key, value in before.items()])
    after = {str(key): str(value) for key, value in (await backend.all_entries()).items()}

    mismatches: list[dict] = []
    for key, value in before.items():
        if key not in after:
            mismatches.append(
                {"key": key, "kind": "lost", "before": value, "after": "", "note": "写回后消失了"}
            )
        elif after[key] != value:
            mismatches.append(
                {
                    "key": key,
                    "kind": "changed",
                    "before": value,
                    "after": after[key],
                    "note": "写回后内容变了（多半被同义 key 合并或外部改动）",
                }
            )
    for key, value in after.items():
        if key not in before:
            mismatches.append(
                {"key": key, "kind": "appeared", "before": "", "after": value, "note": "写回后多出来了"}
            )
    if not mismatches:
        return DriftResult(ok=True, checked=len(before), reason=f"{len(before)} 条记忆逐字节一致")

    backup = ""
    if data_dir is None:
        reason = (
            f"round-trip 不一致（{len(mismatches)} 条），但未给 data_dir，"
            "无法落备份——按拒写处理"
        )
    else:
        path = write_backup(before, data_dir, stamp=stamp)
        backup = str(path)
        reason = (
            f"round-trip 不一致（{len(mismatches)} 条）：库被工具之外的路径改动过，"
            f"已把旧库备份到 {path.name} 并拒绝本次写入"
        )
    log.warning("记忆漂移：%s", reason)
    return DriftResult(
        ok=False, checked=len(before), mismatches=mismatches, backup=backup, reason=reason
    )


# ── staged 落点 ──────────────────────────────────────────────────────────────


def pending_path(data_dir: Any, *, stamp: float | None = None) -> Path:
    """待处置写入的落点：`<data_dir>/memory/pending/<ts>.json`。"""
    root = Path(data_dir) / "memory" / PENDING_DIR
    root.mkdir(parents=True, exist_ok=True)
    ts = int(stamp if stamp is not None else time.time())
    candidate = root / f"{ts}.json"
    index = 1
    while candidate.exists():
        candidate = root / f"{ts}-{index}.json"
        index += 1
    return candidate


def stage_pending(
    entries: Any,
    data_dir: Any,
    *,
    mode: str = "",
    stamp: float | None = None,
) -> Path:
    """把这次想写的内容存成待处置文件（`staged` 的落点）。"""
    path = pending_path(data_dir, stamp=stamp)
    payload = {
        "ts": int(stamp if stamp is not None else time.time()),
        "mode": mode,
        "entries": {key: value for key, value in _pairs(entries)},
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def read_pending(path: Any) -> dict[str, str]:
    """读回待处置文件里的 `entries`。坏文件给可操作的报错，不给 JSON 栈。"""
    raw = Path(path).read_text(encoding="utf-8")
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise ValueError(f"待处置写入不是合法 JSON：{path}（{exc}）") from exc
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        raise ValueError(f"待处置写入缺 entries 字段：{path}")
    return {str(key): str(value) for key, value in data["entries"].items()}


async def apply_pending(path: Any, backend: RoundTripBackend) -> dict:
    """回放一份待处置写入（绕过审批门，但仍**过威胁扫描**）。

    为什么绕过审批门而不过威胁扫描：审批门问的是"这个人是否同意这次写"，
    人按下 `--apply` 就是回答了；威胁扫描问的是"这段内容本身能不能进库"，
    答案不因为有人点了按钮就变。
    """
    entries = read_pending(path)
    dirty = {key: threat_scan(value) for key, value in entries.items()}
    dirty = {key: hits for key, hits in dirty.items() if hits}
    if dirty:
        return {
            "ok": False,
            "applied": 0,
            "reason": "待处置写入里仍有威胁特征，拒绝回放：" + "、".join(sorted(dirty)),
            "findings": dirty,
        }
    await backend.commit([(key, value) for key, value in entries.items()])
    return {"ok": True, "applied": len(entries), "keys": sorted(entries)}


# ── 编排 ─────────────────────────────────────────────────────────────────────


async def check_write(
    entries: Any,
    *,
    mode: str = "off",
    backend: RoundTripBackend | None = None,
    data_dir: Any = None,
    limit: int = MEMORY_MAX_CHARS,
    allowlist: tuple[str, ...] | list[str] = (),
    touched: list[str] | tuple[str, ...] | None = None,
    stamp: float | None = None,
) -> WriteCheck:
    """跑完整的三段门禁 + 漂移防护，返回一次写入的裁决。

    顺序（也是短路的顺序）：威胁扫描 → 审批门 → 快照二次扫描 → 单条超限 →
    round-trip。为什么 round-trip 放最后：它**会写盘**（写回），前面的纯检查能拦住的
    情况就不该先动一次库。
    """
    pairs = _pairs(entries)
    if not pairs:
        return WriteCheck(ok=False, verdict=GATE_BLOCKED, stage=STAGE_THREAT, reason="没有要写入的内容")

    # 预算用量用 `memory_prompt.render` 算，不自己再数一遍：它才是决定"什么真的会被
    # 注入"的那段代码（`memory_batch._fits` 也是这个理由）。
    used = render(pairs, limit=limit).used
    watched = None if touched is None else {str(item).strip() for item in touched}
    # 1) 威胁扫描：只看这次要写的正文。
    scanned = [
        {"key": key, "value": value}
        for key, value in pairs
        if watched is None or key in watched
    ]
    findings: list[dict] = []
    for item in scanned:
        for hit in threat_scan(item["value"]):
            findings.append({**hit, "scope": "entry", "key": item["key"], "pre_existing": False})
    if findings:
        return WriteCheck(
            ok=False,
            verdict=GATE_BLOCKED,
            stage=STAGE_THREAT,
            reason=f"威胁扫描拒绝：{describe_findings(findings)}",
            findings=findings,
            used=used,
            limit=limit,
        )

    # 2) 审批门。
    gate = approval_gate(mode, allowlist=allowlist)

    # 3) 快照期二次扫描：最终态里出现组合命中就拒。
    snapshot = snapshot_scan(pairs, touched=touched)
    composition = [item for item in snapshot if item.get("scope") == "composition"]
    fresh_entry = [item for item in snapshot if item.get("scope") == "entry" and not item.get("pre_existing")]
    if composition or fresh_entry:
        blocking = composition + fresh_entry
        where = "组合后" if composition else "逐条"
        return WriteCheck(
            ok=False,
            verdict=GATE_BLOCKED,
            stage=STAGE_SNAPSHOT,
            reason=f"快照期二次扫描拒绝（{where}命中）：{describe_findings(blocking)}",
            findings=blocking,
            gate=gate.to_dict(),
            used=used,
            limit=limit,
        )

    # 4) 单条超全库上限：静默截断是最坏的选项，直接拒。
    oversize = oversize_entries(pairs, limit=limit)
    if oversize:
        return WriteCheck(
            ok=False,
            verdict=GATE_BLOCKED,
            stage=STAGE_DRIFT,
            reason=oversize[0]["reason"],
            oversize=oversize,
            gate=gate.to_dict(),
            used=used,
            limit=limit,
        )

    # 5) 审批门真的拦住了就不再动库（round-trip 会写盘）。
    if gate.verdict == GATE_BLOCKED:
        return WriteCheck(
            ok=False,
            verdict=GATE_BLOCKED,
            stage=STAGE_APPROVAL,
            reason=gate.reason,
            findings=snapshot,
            gate=gate.to_dict(),
            used=used,
            limit=limit,
        )

    # 6) 漂移防护：只有在真的要写的时候才做（它会写回一次全库）。
    drift = DriftResult(ok=True, reason="未给后端，跳过 round-trip")
    if backend is not None:
        drift = await round_trip_check(backend, data_dir=data_dir, stamp=stamp)
        if not drift.ok:
            return WriteCheck(
                ok=False,
                verdict=GATE_BLOCKED,
                stage=STAGE_DRIFT,
                reason=drift.reason,
                findings=snapshot,
                drift=drift.to_dict(),
                gate=gate.to_dict(),
                limit=limit,
            )

    if gate.verdict == GATE_STAGED:
        if data_dir is None:
            return WriteCheck(
                ok=False,
                verdict=GATE_BLOCKED,
                stage=STAGE_APPROVAL,
                reason="审批模式 smart 需要把内容暂存，但未给 data_dir",
                gate=gate.to_dict(),
                limit=limit,
            )
        path = stage_pending(pairs, data_dir, mode=gate.mode, stamp=stamp)
        return WriteCheck(
            ok=False,
            verdict=GATE_STAGED,
            stage=STAGE_APPROVAL,
            reason=f"{gate.reason}：{path}",
            findings=snapshot,
            drift=drift.to_dict(),
            gate=gate.to_dict(),
            pending=str(path),
            used=used,
            limit=limit,
        )

    return WriteCheck(
        ok=True,
        verdict=GATE_ALLOW,
        findings=snapshot,
        drift=drift.to_dict(),
        gate=gate.to_dict(),
        used=used,
        limit=limit,
    )
