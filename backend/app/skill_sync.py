"""跨设备技能同步 + 三方合并（MATRIX 7.16）。

对标 Hermes `skills_sync_client.py`（真正的跨设备同步：content-addressed 对象模型 +
CAS ref + 逐技能三方合并）。规格出处：`handoff/hermes-parity/research/04-skill-layer.md`
§6.2。

## 为什么「远端」是一个本地目录

Hermes 的同步平面是 `https://gateway-gateway.nousresearch.com/v1/sync/*`，需要 Bearer
token、`tool_gateway_admin` 声明和登录账号。**本机没有网络**，把 HTTP 通道写出来只会
得到一个永远超时的壳，而「看起来能同步」比「明确说不能」危险得多：用户会以为工作已经
备份/合并了，直到换机器才发现什么都没有。

所以本实现把「远端」换成**一个可传输的目录（或 zip）**：

```
<data_dir>/sync/20261008T101500Z/     ← push 产出的「远端快照」
├── manifest.json                     ← 名字 / 内容 hash / mtime / 来源设备
├── apple-notes/SKILL.md
└── weekly-review/SKILL.md
```

把它拷到另一台机器（U 盘、网盘、git 仓库都行）再 `orbit sync pull <那个目录>`，
就是一次真实的跨设备同步。`push_remote()` / `pull_remote()` 明确抛未实现——这是本模块
最重要的一条诚实性约束，与 `skill_hub.install_remote()` 同一立场。

## 三方合并的四种情形（逐条对齐 Hermes `_merge_skill`，1451-1468 行）

| base | ours | theirs | 判定 | 说明 |
|---|---|---|---|---|
| — | = | = | `either` / `none` | 两边同内容（或两边都没有）→ 不算冲突 |
| X | Y≠X | X | `ours` | 只有本地改过 → 取本地 |
| X | X | Z≠X | `theirs` | 只有远端改过 → 取远端 |
| X | Y≠X | Z≠X, Y≠Z | **`conflict`** | 两边都改且不同 → **不自动选边** |

「一边新增」「一边删除」也落在上面这张表里：`base=None, ours=Y, theirs=None` 是
`ours`（本地新增）；`base=X, ours=None, theirs=X` 也是 `ours`（本地删除，远端的删除
同理归 `theirs`）。**没有时间戳比较**——"谁更新"在跨设备场景下不可靠（时钟漂移、
单向同步、补写历史），Hermes 也是整技能 tree 哈希对等比较，不引入 mtime 裁决。

## 为什么冲突默认 `--skip` 而不是 `--ours`

`--ours` 看起来"更安全"（本地的东西不动），但它会让**另一台设备上的工作静默消失**：
用户看到 "sync 成功"，实际上远端那条改动被丢掉了，而且没有任何痕迹——等到发现时，
那一版内容已经不在任何一台机器的磁盘上（bundle 被下一次 push 覆盖）。

`--skip` 的代价是"同步没做完"，这是**可见**的代价：`conflicts` 列表里有名字、两边
hash 都在，用户可以 `--ours` / `--theirs` 逐条决定，或者自己打开两份文件对比。
可见的不完整 > 不可见的完整。

## 导入必须过 lint + quarantine

从 bundle 拿到的 SKILL.md 是**别人机器上的内容**，而且它会被注入系统提示——等于一条
免审批的指令通道。所以导入路径是 `quarantine → 扫描 → 放行 → 正式目录`，与
`skill_hub.install()` 完全一致（复用 `SkillHub.scan_dir` 的零 critical 放行）：

1. 先落 `<data>/skills-quarantine/<name>/`（**扫描对象与最终落地的是同一份字节**）；
2. 扫威胁模式（critical 阻断）+ 跑 `skill_lint`；
3. **额外要求 lint 零 ERROR**（比 hub 的"零 critical"更严）：ERROR 级问题意味着这个
   技能在索引里根本读不出来（名字不合规 / frontmatter 坏 / scope 与目录不符），
   导进来等于"同步成功了但技能不存在"；
4. 过了才搬进 `<data>/skills/<name>/`，并在审计里留一条。

## 状态文件

`<data_dir>/skills-sync-state.json`：`device`（本机标签）、`enabled`（opt-in 名单）、
`base`（上次同步的逐技能 hash，**三方合并的基准**）、`last_push` / `last_pull`。

`base` 必须持久化：它一丢，下一次 pull 就只能退化成"两边比较"，而两边比较无法区分
"远端没动"和"远端删了"——这正是三方合并要解决的问题。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.skill_hub import (
    LOCK_FILENAME,
    QUARANTINE_DIRNAME,
    SKILL_FILENAME,
    SkillHub,
)
from app.skill_index import SkillIndex
from app.skill_lint import has_errors, lint

# ── 常量 ────────────────────────────────────────────────────────────────────

#: 同步格式版本。写进 manifest，未来改协议时可以拒读旧包而不是猜。
WIRE_VERSION = 1
#: 「远端」快照的落点（``<data_dir>/sync/<ts>/``）。
SYNC_DIRNAME = "sync"
#: 每个 bundle 的内容清单。
MANIFEST_FILENAME = "manifest.json"
#: 本机同步状态（device / enabled / base / last_*）。
STATE_FILENAME = "skills-sync-state.json"
#: 技能正文文件名（与 `skill_hub` 同值，这里再导出一次免得调用方到处 import）。
BUNDLE_SKILL_FILENAME = SKILL_FILENAME
#: 没有设置设备标签时的默认值。
DEFAULT_DEVICE = "local"

# ── 合并裁决 ────────────────────────────────────────────────────────────────

#: 只有本地改过（或本地新增）→ 取本地。
DECISION_OURS = "ours"
#: 只有远端改过（或远端新增）→ 取远端。
DECISION_THEIRS = "theirs"
#: 两边内容相同 → 取其一（**不算冲突**）。
DECISION_EITHER = "either"
#: 两边都没有 → 什么都不做。
DECISION_NONE = "none"
#: 两边都改且不同 → 冲突，**不自动选边**。
DECISION_CONFLICT = "conflict"

#: 冲突解决策略。``skip`` 是默认值，理由见模块 docstring。
POLICY_SKIP = "skip"
POLICY_OURS = "ours"
POLICY_THEIRS = "theirs"
POLICIES: tuple[str, ...] = (POLICY_SKIP, POLICY_OURS, POLICY_THEIRS)


class SkillSyncError(RuntimeError):
    """同步层的可预期失败（bundle 不存在、格式不对、远端未实现…）。"""


def _digest(text: str) -> str:
    """内容指纹（sha256，按**文本**算）。

    按文本而不是字节：Windows 上 ``write_text`` 会把 ``\\n`` 写成 ``\\r\\n``，按字节
    算会让"刚 push 完就显示有变化"。两边都在文本层比对才自洽。
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _stamp(moment: datetime) -> str:
    """目录名用的紧凑时间戳（``20261008T101500Z``）：冒号在 Windows 路径里非法。"""
    return moment.strftime("%Y%m%dT%H%M%SZ")


def _read_json(path: Path) -> dict[str, Any]:
    """读 JSON 对象。坏文件当空 dict——状态文件坏了不该让整个同步不可用。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


# ── 三方合并 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class MergeConflict:
    """一条冲突：三方 hash 都留着，让人能自己判断该取哪边。

    只报"有冲突"而不给 hash，等于把定位工作退回给用户——他连"到底哪份是哪份"
    都要靠猜。三个 hash 前缀足以对上 `orbit sync status` 里的清单。
    """

    name: str
    base: str
    ours: str
    theirs: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "base": self.base,
            "ours": self.ours,
            "theirs": self.theirs,
        }

    def describe(self) -> str:
        """人读的一行。空 hash 显示成 ``-``（= 该侧没有这个技能）。"""
        return (
            f"{self.name}：base={self.base[:8] or '-'} "
            f"ours={self.ours[:8] or '-'} theirs={self.theirs[:8] or '-'}"
        )


@dataclass
class MergeResult:
    """一次三方合并的结论。``decisions`` 覆盖**全部**技能名（含 ``none``）。"""

    decisions: dict[str, str] = field(default_factory=dict)
    conflicts: tuple[MergeConflict, ...] = ()

    @property
    def conflict_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.conflicts)

    def take_ours(self) -> tuple[str, ...]:
        return tuple(
            name for name, decision in self.decisions.items() if decision == DECISION_OURS
        )

    def take_theirs(self) -> tuple[str, ...]:
        """需要从 bundle 导入的技能（``theirs`` + 内容相同的 ``either``）。

        ``either`` 也列进来：两边内容一致时"取其一"最省事的做法就是不去动本地，
        但**新增**技能（base 与 ours 都没有、两边都有）必须真的写盘，否则同步完了
        本地还是缺它。所以这里按"本地有没有"再筛一次，见 :meth:`needs_import`。
        """
        return tuple(
            name
            for name, decision in self.decisions.items()
            if decision in (DECISION_THEIRS, DECISION_EITHER)
        )

    def needs_import(self, local: Mapping[str, str]) -> tuple[str, ...]:
        """哪些技能需要真的从 bundle 写进本地目录。

        ``either`` 只有在本地没有这个技能时才需要导入（新增场景）。
        """
        needed: list[str] = []
        for name in self.take_theirs():
            if name not in local:
                needed.append(name)
        return tuple(needed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "decisions": dict(sorted(self.decisions.items())),
            "conflicts": [item.as_dict() for item in self.conflicts],
            "counts": {
                DECISION_OURS: sum(1 for d in self.decisions.values() if d == DECISION_OURS),
                DECISION_THEIRS: sum(1 for d in self.decisions.values() if d == DECISION_THEIRS),
                DECISION_EITHER: sum(1 for d in self.decisions.values() if d == DECISION_EITHER),
                DECISION_NONE: sum(1 for d in self.decisions.values() if d == DECISION_NONE),
                DECISION_CONFLICT: len(self.conflicts),
            },
        }


def merge(
    base: Mapping[str, str],
    ours: Mapping[str, str],
    theirs: Mapping[str, str],
) -> MergeResult:
    """三方合并：``base`` 是上次同步的快照，``ours`` 本地，``theirs`` 远端。

    三个参数都是 ``{技能名: 内容 hash}``，**缺键 = 该侧没有这个技能**（不是空文件）。
    逐条判定的顺序与 Hermes `_merge_skill` 一致（先判"两边相同"，再判单边改动），
    顺序不能换：若先判单边改动，`ours == theirs != base` 会被误判成 `ours`（结论
    碰巧一样），而 `ours == theirs == None`（两边都删了）会被误判成"ours 改了"。

    返回的 ``decisions`` 覆盖三边的名字并集，所以调用方能逐条追问"这个技能为什么
    没同步"，而不是只拿到一个"改了 N 条"的数字。
    """
    decisions: dict[str, str] = {}
    conflicts: list[MergeConflict] = []
    for name in sorted(set(base) | set(ours) | set(theirs)):
        baseline = base.get(name)
        local = ours.get(name)
        remote = theirs.get(name)
        if local == remote:
            decisions[name] = DECISION_EITHER if local is not None else DECISION_NONE
            continue
        ours_changed = local != baseline
        theirs_changed = remote != baseline
        if ours_changed and not theirs_changed:
            decisions[name] = DECISION_OURS
            continue
        if theirs_changed and not ours_changed:
            decisions[name] = DECISION_THEIRS
            continue
        decisions[name] = DECISION_CONFLICT
        conflicts.append(
            MergeConflict(
                name=name,
                base=baseline or "",
                ours=local or "",
                theirs=remote or "",
            )
        )
    return MergeResult(decisions=decisions, conflicts=tuple(conflicts))


def resolve(result: MergeResult, policy: str = POLICY_SKIP) -> dict[str, str]:
    """按策略把冲突折叠成最终裁决：``{技能名: ours/theirs}``（只含需要动作的）。

    ``skip`` 下冲突**不出现在返回值里**——它既不是 ours 也不是 theirs，调用方据此
    知道"这条我没决定"。这就是默认 skip 的全部实现。
    """
    if policy not in POLICIES:
        raise SkillSyncError(f"未知冲突策略 {policy!r}（可选：{', '.join(POLICIES)}）")
    final: dict[str, str] = {}
    for name, decision in result.decisions.items():
        if decision != DECISION_CONFLICT:
            final[name] = decision
            continue
        if policy == POLICY_OURS:
            final[name] = DECISION_OURS
        elif policy == POLICY_THEIRS:
            final[name] = DECISION_THEIRS
        # policy == skip：什么都不写，冲突原样留着让人决定
    return final


# ── bundle ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SyncBundle:
    """一个可传输的技能快照（目录或 zip）。

    ``manifest`` 的形状（``version / device / created_at / skills``）：
    ``skills`` 是 ``{名字: {name, hash, mtime, device}}``。带 mtime 只用于**展示**
    （"这份是昨天从笔记本推的"），**不参与裁决**——见模块 docstring 的"没有时间戳
    比较"一节。
    """

    root: Path
    manifest: dict[str, Any] = field(default_factory=dict)
    #: 从 zip 读出来时的正文缓存（``{名字: 文本}``）；目录模式下为空，按需读盘。
    _bodies: dict[str, str] = field(default_factory=dict)

    @property
    def device(self) -> str:
        return str(self.manifest.get("device") or "")

    @property
    def created_at(self) -> str:
        return str(self.manifest.get("created_at") or "")

    @property
    def version(self) -> int:
        try:
            return int(self.manifest.get("version") or 0)
        except (TypeError, ValueError):
            return 0

    def entries(self) -> dict[str, dict[str, Any]]:
        """``{名字: {name, hash, mtime, device}}``（坏条目跳过）。"""
        raw = self.manifest.get("skills")
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): dict(value)
            for key, value in raw.items()
            if isinstance(value, dict)
        }

    def digests(self) -> dict[str, str]:
        """``{名字: 内容 hash}``，直接喂给 :func:`merge` 的 ``theirs``。"""
        return {
            name: str(entry.get("hash") or "")
            for name, entry in self.entries().items()
            if str(entry.get("hash") or "")
        }

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self.entries()))

    def read(self, name: str) -> str:
        """读某个技能的 SKILL.md 全文。不存在抛 ``SkillSyncError``（不返回空串）。"""
        clean = (name or "").strip()
        if clean in self._bodies:
            return self._bodies[clean]
        path = self.root / clean / BUNDLE_SKILL_FILENAME
        if not path.is_file():
            raise SkillSyncError(f"bundle 里没有技能 {clean!r}（{path} 不存在）")
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SkillSyncError(f"bundle 里的 {clean} 读不出来：{exc}") from exc

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "version": self.version,
            "device": self.device,
            "created_at": self.created_at,
            "skills": len(self.entries()),
        }


def load_bundle(path: Path | str) -> SyncBundle:
    """读一个 bundle（目录或 ``.zip``）。**格式不对就报错，不猜**。"""
    target = Path(path)
    if target.is_file() and target.suffix.lower() == ".zip":
        return _load_zip(target)
    if not target.is_dir():
        raise SkillSyncError(
            f"bundle 不存在：{target}。"
            "先 `orbit sync push` 产出一份，或把另一台机器的 sync 目录拷过来"
        )
    manifest = _read_json(target / MANIFEST_FILENAME)
    if not manifest:
        raise SkillSyncError(
            f"{target} 不像一个同步包：缺 {MANIFEST_FILENAME}（`orbit sync push` 会生成它）"
        )
    bundle = SyncBundle(root=target, manifest=manifest)
    if bundle.version > WIRE_VERSION:
        raise SkillSyncError(
            f"同步包格式版本 {bundle.version} 高于本机支持的 {WIRE_VERSION}："
            "升级 Orbit 再拉，别让旧版本猜新格式"
        )
    return bundle


def _load_zip(path: Path) -> SyncBundle:
    """从 zip 里读 bundle：manifest 解出来，正文留在内存里（不落临时目录）。

    为什么不先解压到临时目录：解压出来的是一堆**未经扫描的 SKILL.md**，任何"顺手
    先解开看看"的代码路径都会绕过 quarantine。正文留在内存，导入时逐条过扫描。
    """
    try:
        with zipfile.ZipFile(path) as archive:
            raw = archive.read(MANIFEST_FILENAME)
            manifest = json.loads(raw.decode("utf-8"))
            bodies: dict[str, str] = {}
            for name in (manifest.get("skills") or {}):
                member = f"{name}/{BUNDLE_SKILL_FILENAME}"
                try:
                    bodies[str(name)] = archive.read(member).decode("utf-8")
                except (KeyError, UnicodeError):
                    continue
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        raise SkillSyncError(f"读不了同步包 {path}：{exc}") from exc
    if not isinstance(manifest, dict):
        raise SkillSyncError(f"{path} 里的 {MANIFEST_FILENAME} 不是 JSON 对象")
    bundle = SyncBundle(root=path, manifest=manifest, _bodies=bodies)
    if bundle.version > WIRE_VERSION:
        raise SkillSyncError(
            f"同步包格式版本 {bundle.version} 高于本机支持的 {WIRE_VERSION}"
        )
    return bundle


# ── 同步主体 ────────────────────────────────────────────────────────────────


class SkillSync:
    """一台设备上的技能同步视图。一个实例 = 一份 ``data_dir`` 的同步状态。"""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        index: SkillIndex | None = None,
        hub: SkillHub | None = None,
        now: Any = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.index = index if index is not None else SkillIndex(self.data_dir)
        self.hub = hub if hub is not None else SkillHub(self.data_dir, index=self.index)
        self._now = now

    def now(self) -> datetime:
        return self._now() if self._now is not None else datetime.now(UTC)

    # —— 路径 ——

    @property
    def skills_root(self) -> Path:
        """用户级技能根（与 `SkillIndex` 的 user tier 同址）。"""
        return self.data_dir / "skills"

    @property
    def sync_root(self) -> Path:
        return self.data_dir / SYNC_DIRNAME

    @property
    def state_path(self) -> Path:
        return self.data_dir / STATE_FILENAME

    @property
    def quarantine_root(self) -> Path:
        return self.data_dir / QUARANTINE_DIRNAME

    @property
    def audit_path(self) -> Path:
        return self.data_dir / "skills-sync-audit.jsonl"

    # —— 状态 ——

    def read_state(self) -> dict[str, Any]:
        """读本机同步状态。坏文件当空状态（只丢 base，不会误删技能）。"""
        state = _read_json(self.state_path)
        if not state:
            return {
                "version": WIRE_VERSION,
                "device": "",
                "enabled": [],
                "base": {},
                "last_push": "",
                "last_pull": "",
            }
        return state

    def _write_state(self, state: dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": WIRE_VERSION,
            "device": str(state.get("device") or ""),
            "enabled": sorted(str(item) for item in (state.get("enabled") or [])),
            "base": {
                str(key): str(value)
                for key, value in (state.get("base") or {}).items()
            },
            "last_push": str(state.get("last_push") or ""),
            "last_pull": str(state.get("last_pull") or ""),
            "updated_at": _iso(self.now()),
        }
        self.state_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def device(self) -> str:
        """本机标签（写进 manifest 的 ``device`` 字段，用于"这份是谁推的"）。"""
        return str(self.read_state().get("device") or "") or DEFAULT_DEVICE

    def set_device(self, name: str) -> str:
        """设置本机标签。空串 = 清除（回到 ``local``）。"""
        clean = " ".join(str(name or "").split())[:64]
        state = self.read_state()
        state["device"] = clean
        self._write_state(state)
        return clean or DEFAULT_DEVICE

    def enabled(self) -> tuple[str, ...]:
        """已纳入同步的技能名（Hermes 的 opt-in 默认关闭，这里同口径）。"""
        return tuple(sorted(str(item) for item in (self.read_state().get("enabled") or [])))

    def enable(self, name: str) -> tuple[str, ...]:
        """把技能纳入同步。

        两条硬要求，缺一条都拒绝（而不是"先记下，push 时才失败"）：
        技能必须真的在磁盘上，且必须**在可同步范围内**——内置技能与 hub 安装的技能
        由种子目录 / `skills update` 管，同步它们会用一份旧副本覆盖另一台机器上更新的
        内容。错误在 ``enable`` 时报出来，用户当场就能换个技能，而不是等到 push。
        """
        clean = self._require_skill(name)
        if clean not in self.eligible():
            raise SkillSyncError(
                f"{clean} 不在可同步范围内（内置技能或 hub 安装的技能不走同步）。"
                "`orbit sync status` 里能看到分界"
            )
        state = self.read_state()
        names = {str(item) for item in (state.get("enabled") or [])}
        names.add(clean)
        state["enabled"] = sorted(names)
        self._write_state(state)
        return self.enabled()

    def disable(self, name: str) -> tuple[str, ...]:
        """把技能移出同步（不动磁盘上的文件，只改名单）。"""
        clean = (name or "").strip().removesuffix(".md")
        state = self.read_state()
        names = {str(item) for item in (state.get("enabled") or [])}
        names.discard(clean)
        state["enabled"] = sorted(names)
        self._write_state(state)
        return self.enabled()

    def _require_skill(self, name: str) -> str:
        clean = (name or "").strip().removesuffix(".md")
        if not clean:
            raise SkillSyncError("需要一个技能名（`orbit skills list` 里第一列）")
        if not (self.skills_root / clean / BUNDLE_SKILL_FILENAME).is_file():
            raise SkillSyncError(
                f"本地没有技能 {clean!r}（{self.skills_root}）；先创建或安装它再纳入同步"
            )
        return clean

    # —— 可同步范围 ——

    def builtin_names(self) -> frozenset[str]:
        """内置（播种来的）技能名。**内置与 hub 安装的技能不参与同步**（Hermes 同口径：
        只允许 agent-created + user-authored）。理由：内置技能在每台机器上都由种子目录
        提供，同步它们等于用一份旧副本覆盖另一台机器上更新的种子。
        """
        from app import skill_seed

        return skill_seed.builtin_names(self.data_dir)

    def hub_names(self) -> frozenset[str]:
        """hub 安装的技能名（读 lock）。这些由 `skills update` 管，不该走同步。"""
        payload = _read_json(self.data_dir / LOCK_FILENAME)
        installed = payload.get("installed")
        if not isinstance(installed, dict):
            return frozenset()
        return frozenset(str(key) for key in installed)

    def eligible(self) -> tuple[str, ...]:
        """可同步的技能名（用户级根下、且不是内置 / hub 安装的）。"""
        excluded = self.builtin_names() | self.hub_names()
        names: list[str] = []
        if not self.skills_root.is_dir():
            return ()
        for entry in sorted(self.skills_root.iterdir()):
            if not entry.is_dir() or entry.name in excluded:
                continue
            if (entry / BUNDLE_SKILL_FILENAME).is_file():
                names.append(entry.name)
        return tuple(names)

    def snapshot(self) -> dict[str, str]:
        """当前本地的 ``{技能名: 内容 hash}``（可同步范围内的全部技能）。

        给 :func:`merge` 当 ``ours``。读不出来的文件**跳过而不是记空 hash**：
        空 hash 会被当成"本地把内容清空了"，从而触发一次覆盖写入。
        """
        digests: dict[str, str] = {}
        for name in self.eligible():
            path = self.skills_root / name / BUNDLE_SKILL_FILENAME
            try:
                digests[name] = _digest(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue
        return digests

    # —— status ——

    def status(self) -> dict[str, Any]:
        """同步概览：设备标签、opt-in 名单、基准条数、已有 bundle、远端状态。"""
        state = self.read_state()
        bundles: list[dict[str, Any]] = []
        if self.sync_root.is_dir():
            for entry in sorted(self.sync_root.iterdir()):
                if not entry.is_dir():
                    continue
                manifest = _read_json(entry / MANIFEST_FILENAME)
                bundles.append(
                    {
                        "path": str(entry),
                        "device": str(manifest.get("device") or ""),
                        "created_at": str(manifest.get("created_at") or ""),
                        "skills": len(manifest.get("skills") or {}),
                    }
                )
        eligible = self.eligible()
        return {
            "device": self.device(),
            "enabled": list(self.enabled()),
            "eligible": list(eligible),
            "not_eligible": {
                "builtin": sorted(self.builtin_names()),
                "hub": sorted(self.hub_names()),
            },
            "base": len(state.get("base") or {}),
            "bundles": bundles,
            "last_push": str(state.get("last_push") or ""),
            "last_pull": str(state.get("last_pull") or ""),
            "remote": "未实现：本机无网络（远端通道见 push_remote / pull_remote）",
            "sync_root": str(self.sync_root),
        }

    # —— push ——

    def export(self, dest: Path | str | None = None, *, names: tuple[str, ...] | None = None) -> SyncBundle:
        """把技能打成一个可传输目录：``<data>/sync/<ts>/`` 或 ``dest``。

        ``names`` 省略时取 opt-in 名单（``enabled``）；名单为空时**什么都不写**并
        报错——"推了一个空包"会让另一台机器以为本地没有技能，从而把远端的技能
        当成"新增"全量导入，方向正好相反。
        """
        wanted = names if names is not None else self.enabled()
        if not wanted:
            raise SkillSyncError(
                "没有技能被纳入同步：先 `orbit sync enable <技能名>`"
                f"（可同步的：{', '.join(self.eligible()) or '（空）'}）"
            )
        missing = [name for name in wanted if name not in self.eligible()]
        if missing:
            raise SkillSyncError(
                f"这些技能不在可同步范围内：{', '.join(sorted(missing))}"
                "（内置技能与 hub 安装的技能不走同步，见 `orbit sync status`）"
            )
        moment = self.now()
        root = Path(dest) if dest is not None else self.sync_root / _stamp(moment)
        root.mkdir(parents=True, exist_ok=True)
        device = self.device()
        entries: dict[str, dict[str, Any]] = {}
        for name in sorted(wanted):
            source = self.skills_root / name / BUNDLE_SKILL_FILENAME
            text = source.read_text(encoding="utf-8")
            target = root / name
            target.mkdir(parents=True, exist_ok=True)
            (target / BUNDLE_SKILL_FILENAME).write_text(text, encoding="utf-8")
            stat = source.stat()
            entries[name] = {
                "name": name,
                "hash": _digest(text),
                "mtime": _iso(datetime.fromtimestamp(stat.st_mtime, UTC)),
                "device": device,
            }
        manifest = {
            "version": WIRE_VERSION,
            "device": device,
            "created_at": _iso(moment),
            "skills": entries,
        }
        (root / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return SyncBundle(root=root, manifest=manifest)

    def push(self, bundle_path: Path | str | None = None) -> dict[str, Any]:
        """推送到**本地目录**形式的远端，并把基准（base）推进到推送时的内容。

        ``bundle_path`` 给一个**已存在的目录**时直接写进去（模拟"推到 U 盘"）；
        否则当成新 bundle 的落点。
        """
        dest = Path(bundle_path) if bundle_path is not None else None
        bundle = self.export(dest)
        state = self.read_state()
        base = {str(key): str(value) for key, value in (state.get("base") or {}).items()}
        base.update(bundle.digests())
        state["base"] = base
        state["last_push"] = _iso(self.now())
        self._write_state(state)
        self._append_audit("PUSH", bundle=str(bundle.root), skills=len(bundle.entries()))
        return {
            "ok": True,
            "bundle": str(bundle.root),
            "device": bundle.device,
            "created_at": bundle.created_at,
            "skills": bundle.names(),
            "manifest": str(bundle.root / MANIFEST_FILENAME),
            "base": len(base),
        }

    # —— pull ——

    def pull(self, bundle_path: Path | str, *, policy: str = POLICY_SKIP) -> dict[str, Any]:
        """从 bundle 拉取并三方合并。**默认 ``policy="skip"``**（冲突留给人决定）。

        导入路径：``quarantine → 扫描（critical + lint ERROR）→ 正式目录``。
        被拦下的技能留在隔离区并记进 ``blocked``，不会"部分写入"。
        """
        if policy not in POLICIES:
            raise SkillSyncError(f"未知冲突策略 {policy!r}（可选：{', '.join(POLICIES)}）")
        bundle = load_bundle(bundle_path)
        theirs = bundle.digests()
        ours = self.snapshot()
        state = self.read_state()
        base = {str(key): str(value) for key, value in (state.get("base") or {}).items()}
        result = merge(base, ours, theirs)
        final = resolve(result, policy)

        imported: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for name, decision in sorted(final.items()):
            if decision != DECISION_THEIRS:
                continue
            if name in ours and ours[name] == theirs.get(name):
                # 内容已经一致（either），不需要重写磁盘
                continue
            outcome = self._import(bundle, name)
            if outcome["ok"]:
                imported.append(outcome)
                base[name] = str(theirs.get(name) or outcome.get("hash") or "")
            else:
                blocked.append(outcome)
                skipped.append(
                    {
                        "name": name,
                        "reason": "导入被拦下（见 blocked）",
                        "decision": decision,
                    }
                )
        for conflict in result.conflicts:
            if conflict.name in final:
                # 冲突被策略折叠成 ours 时，base 推进到**远端那一版**（不是本地那版）：
                # 记的是"我看过远端这版并拒绝了它"。若把 base 记成本地版，下一次 pull
                # 会看到 theirs != base 且 ours == base → 判成 theirs 并**覆盖本地**，
                # 于是 `--ours` 的效果在下一次同步里被静默推翻。
                if final[conflict.name] == DECISION_OURS:
                    base[conflict.name] = conflict.theirs
                continue
            skipped.append(
                {
                    "name": conflict.name,
                    "reason": "两边都改过且内容不同，默认 skip（用 --ours / --theirs 决定）",
                    "decision": DECISION_CONFLICT,
                }
            )
        # 只有真正落定（ours/theirs/either/none）的技能才推进 base。
        # 冲突**不在这里推进**：它们已经在上面按策略处理过（ours 记远端那一版），
        # 若再按"ours 就记本地那一版"覆盖一次，`--ours` 的效果会在下一次 pull 被推翻。
        conflict_names = {item.name for item in result.conflicts}
        for name, decision in final.items():
            if name in conflict_names:
                continue
            if decision == DECISION_OURS and name in ours:
                base[name] = ours[name]
            elif decision == DECISION_NONE:
                base.pop(name, None)

        state["base"] = base
        state["last_pull"] = _iso(self.now())
        self._write_state(state)
        self._append_audit(
            "PULL",
            bundle=str(bundle.root),
            imported=len(imported),
            blocked=len(blocked),
            conflicts=len(result.conflicts),
        )
        if imported:
            self.index.invalidate()
        return {
            "ok": not blocked,
            "bundle": str(bundle.root),
            "policy": policy,
            "merge": result.as_dict(),
            "imported": imported,
            "blocked": blocked,
            "skipped": skipped,
            "conflicts": [item.as_dict() for item in result.conflicts],
        }

    def now_sync(self, *, policy: str = POLICY_SKIP) -> dict[str, Any]:
        """`sync now`：先拉最近的 bundle，再推一次。

        "先拉后推"的顺序与 Hermes 的 ``hermes sync now`` 一致：先吸收远端改动，
        否则本地的旧内容会以"新版本"的身份推上去，把远端刚做的修改盖掉。
        """
        pulled: dict[str, Any] | None = None
        candidates = self.bundles()
        if candidates:
            pulled = self.pull(candidates[-1]["path"], policy=policy)
        pushed = self.push() if self.enabled() else None
        return {"ok": True, "pulled": pulled, "pushed": pushed}

    def bundles(self) -> list[dict[str, Any]]:
        """本机 ``sync/`` 下的 bundle 列表（按名字排序，最后一个是最近的）。"""
        if not self.sync_root.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for entry in sorted(self.sync_root.iterdir()):
            if not entry.is_dir():
                continue
            if not (entry / MANIFEST_FILENAME).is_file():
                continue
            manifest = _read_json(entry / MANIFEST_FILENAME)
            rows.append(
                {
                    "path": str(entry),
                    "device": str(manifest.get("device") or ""),
                    "created_at": str(manifest.get("created_at") or ""),
                    "skills": len(manifest.get("skills") or {}),
                }
            )
        return rows

    # —— 导入（quarantine → 扫描 → 正式目录）——

    def _import(self, bundle: SyncBundle, name: str) -> dict[str, Any]:
        """把一个技能从 bundle 导进正式目录。**必须过 quarantine 与扫描**。

        顺序不能改：先落隔离区 → 扫描**隔离区里的那份副本** → 过了才搬。若先搬再扫，
        扫描失败的那一瞬间技能已经在装配路径上了（下一轮提示就可能带上它）。
        """
        clean = (name or "").strip().removesuffix(".md")
        try:
            text = bundle.read(clean)
        except SkillSyncError as exc:
            return {"ok": False, "name": clean, "reason": str(exc)}
        staged = self.quarantine_root / clean
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        staged.mkdir(parents=True, exist_ok=True)
        (staged / BUNDLE_SKILL_FILENAME).write_text(text, encoding="utf-8")
        report = self.hub.scan_dir(staged, clean)
        # 与 `SkillHub.list_quarantine` 读的是同一个文件名/形状
        self.hub._write_quarantine_meta(staged, report, identifier=f"sync:{clean}")
        lint_errors = [item.format() for item in report.lint_findings if item.severity == "error"]
        if not report.allowed or lint_errors:
            reason = (
                f"critical 威胁 {len(report.critical)} 条"
                if not report.allowed
                else f"lint ERROR {len(lint_errors)} 条"
            )
            self._append_audit("QUARANTINE", skill=clean, reason=reason)
            return {
                "ok": False,
                "name": clean,
                "quarantined": True,
                "reason": reason,
                "path": str(staged),
                "critical": [item.as_dict() for item in report.critical],
                "lint_errors": lint_errors,
            }
        target = self.skills_root / clean
        meta = staged / ".quarantine.json"
        if meta.is_file():
            # 隔离报告是隔离区自己的记账，不是技能内容：留在包里会让"内容 hash"
            # 每次都不一样（刚导入就显示有更新）
            meta.unlink()
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(target))
        # 额外做一次「落地后再体检」：确认搬进去的字节真的零 ERROR。
        # 上面的 lint 扫的是隔离区副本，这一步扫的是**正式目录里的那份**。
        findings = lint(clean, (target / BUNDLE_SKILL_FILENAME).read_text(encoding="utf-8"))
        self._append_audit(
            "IMPORT", skill=clean, verdict=report.verdict, errors=len(lint_errors)
        )
        return {
            "ok": not has_errors(findings),
            "name": clean,
            "path": str(target),
            "hash": _digest((target / BUNDLE_SKILL_FILENAME).read_text(encoding="utf-8")),
            "verdict": report.verdict,
        }

    # —— 远端（明确未实现）——

    def push_remote(self, *args: Any, **kwargs: Any) -> None:
        """远端推送：**未实现**（本机无网络）。

        抛异常而不是返回空结果：`orbit sync push --remote` 必须让人知道"什么都没发生"
        以及"该怎么做"。假装成功比拒绝更糟——用户会以为工作已经备份到别处了。
        """
        raise SkillSyncError(
            "远端同步未实现：本机无网络，无法连接 Hermes 的 /v1/sync 平面。"
            f"本地做法：`orbit sync push` 会写一份 bundle 到 {self.sync_root}\\<时间戳>\\，"
            "把它拷到另一台机器，在那边 `orbit sync pull <那个目录>`"
        )

    def pull_remote(self, *args: Any, **kwargs: Any) -> None:
        """远端拉取：**未实现**（同上）。"""
        raise SkillSyncError(
            "远端同步未实现：本机无网络。"
            "本地做法：把另一台机器的 sync 目录（或 zip）拷过来，"
            "`orbit sync pull <路径>`"
        )

    # —— 审计 ——

    def _append_audit(self, action: str, **extra: Any) -> None:
        """append-only 审计。写失败不阻断——它是记录，不是门。"""
        record = {"ts": _iso(self.now()), "action": action, **extra}
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


__all__ = [
    "BUNDLE_SKILL_FILENAME",
    "DECISION_CONFLICT",
    "DECISION_EITHER",
    "DECISION_NONE",
    "DECISION_OURS",
    "DECISION_THEIRS",
    "DEFAULT_DEVICE",
    "MANIFEST_FILENAME",
    "POLICIES",
    "POLICY_OURS",
    "POLICY_SKIP",
    "POLICY_THEIRS",
    "STATE_FILENAME",
    "SYNC_DIRNAME",
    "WIRE_VERSION",
    "MergeConflict",
    "MergeResult",
    "SkillSync",
    "SkillSyncError",
    "SyncBundle",
    "load_bundle",
    "merge",
    "resolve",
]
