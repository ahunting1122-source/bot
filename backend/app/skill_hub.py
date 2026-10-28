"""技能 hub（本地目录版）+ quarantine 隔离区。

对标 Hermes `tools/skills_hub.py`（`quarantine → scan → move` 安装流程 +
`lock.json` + `audit.log`）与 `tools/skills_guard.py` 的判决门槛。规格出处：
`handoff/hermes-parity/research/04-skill-layer.md` §6.3 / §5.9。

## 为什么「远端」是一个本地目录

Hermes 的 hub 有 9 类 Source（HermesIndex / GitHub / well-known / ClawHub …），
全部依赖网络。**本机没有网络**，把 HTTP 通道写出来只会得到一个永远超时的壳，
而且「看起来能用」比「明确说不能」更危险——用户会以为装上了。

所以本实现把 `<data_dir>/skills-hub/` 当作「远端」的替身：

```
<data_dir>/skills-hub/            ← 手工放入的技能包（模拟远端索引）
├── mlops/eval-harness/SKILL.md   ← 允许类目嵌套（identifier = "mlops/eval-harness"）
└── quick-notes/SKILL.md
```

远端通道（URL / `owner/repo/name`）走 `install_remote()`，**明确抛未实现并给出
本地做法**。这是本模块最重要的一条诚实性约束。

## 为什么安装必须先落 quarantine

从外部拿来的 SKILL.md 是**要注入系统提示的内容**，也就是一条免审批的指令通道。
Hermes 的流程是 `quarantine_bundle → scan_skill → install_from_quarantine`：
先落在隔离区、扫完再搬。照抄它的理由不是「对齐」，而是：

1. **扫描对象必须与最终落地的文件是同一份**。若先搬进 `skills/` 再扫，扫描失败的
   那一瞬间技能已经在装配路径上了（下一轮提示就可能带上它）。
2. 隔离区里躺着的东西**可以被看到**（`list_quarantine()`），于是「为什么没装上」
   有答案；直接拒绝只会得到「装了没反应」。

放行条件写死为**零 critical**（`GUARD_CRITICAL = "critical"`）。high/medium 只记录
不阻断——Hermes 的 `should_allow_install` 对 `community` 来源也是这么分的：
dangerous 才 block，caution 只提示。`release()` **不能绕过 critical**（重扫一次，
仍 critical 就仍然拒绝），与 Hermes「`--force` 不能覆盖 dangerous 判决」同一立场。

## `publish()`：把技能**打包进**本地 hub（矩阵 7.13 的最后一条）

`hermes skills publish --to github|clawhub` 是"发到社区注册表"，本机没有网络、
也没有注册表账号，所以 :meth:`SkillHub.publish` 只做**另一半**：把技能打包成一份
自洽的发布物（`SKILL.md` + `manifest.json`），落在 `<data_dir>/skills-hub/<名字>/`。

三条硬规矩：

1. **`to` 只接受本地路径**。远程目标（`github` / `clawhub` / `https://…`）如实抛
   「未实现 + 需要网络 + 社区索引」，不写一个永远超时的 HTTP 壳——那会让用户以为
   已经发出去了。
2. **默认 dry_run**。先打印"会发布什么、内容 hash 是多少、写到哪"，`--apply` 才落盘。
   发布是**对外可见**的动作，默认就该是先看后做。
3. **发布前必过 linter 零 ERROR**（复用 `app/skill_lint.py`，只读它）。安装侧的
   `scan_dir` 对 lint 只记 `caution` 不阻断（那是"装到自己机器上"），而发布是把
   内容推给别人，门槛必须更严：别人没有机会在你的隔离区里复查。

## lock / audit 为什么不在 hub 目录里

Hermes 把 `lock.json` / `audit.log` 放在 `~/.hermes/skills/.hub/` 下。本实现把它们
放在 `<data_dir>/` 下（`skills-hub-lock.json` / `skills-hub-audit.jsonl`），理由：
`skills-hub/` 在这里扮演「远端」，用户会整体替换它来模拟换源；把本机安装记录写进
「远端」，换源时会被一起换掉——安装记录丢了，`uninstall` 就不敢删任何东西了。
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.skill_index import (
    SEVERITY_RANK,
    SkillIndex,
    ThreatFinding,
    index_description,
    scan_skill,
    scan_text,
    validate_frontmatter,
)
from app.skill_lint import Finding, has_errors, lint

# ── 目录与文件布局 ──────────────────────────────────────────────────────────

#: 「远端」目录（本地目录形式的 hub）。
HUB_DIRNAME = "skills-hub"
#: 隔离区。装进来的技能先落这里，扫完才搬。
QUARANTINE_DIRNAME = "skills-quarantine"
#: 已安装记录（本机侧，与 hub 目录分离，理由见模块 docstring）。
LOCK_FILENAME = "skills-hub-lock.json"
#: 安装审计（append-only JSONL）。
AUDIT_FILENAME = "skills-hub-audit.jsonl"
#: 正式安装根（用户级技能根）。
INSTALL_DIRNAME = "skills"
#: 隔离区里每个技能的扫描报告（`list_quarantine()` 用它解释「为什么被拦」）。
QUARANTINE_META_FILENAME = ".quarantine.json"
#: 技能正文文件名。
SKILL_FILENAME = "SKILL.md"
#: 发布清单文件名（`publish()` 的产物）。**只放在包顶层**，见 `dir_hash`。
MANIFEST_FILENAME = "manifest.json"
#: 计算内容指纹时忽略的顶层文件。为什么忽略 `manifest.json`：它是**发布元数据**
#: （谁在什么时候、以什么 hash 发的），不是技能内容。算进去会让
#: `dir_hash(hub 包) != dir_hash(已安装副本)`（隔离报告那条老坑的同一形状），
#: 于是 `skills update` 对每个从 hub 装的技能都永久显示「有更新」。
HASH_SKIP: frozenset[str] = frozenset({MANIFEST_FILENAME})
#: 远程发布目标的写法（`--to github` / `--to clawhub` / URL）。命中即如实报未实现。
REMOTE_TARGETS: frozenset[str] = frozenset(
    {"github", "gitlab", "clawhub", "clawhub.ai", "registry", "hub", "remote"}
)
#: 只有这个级别的威胁会拦住安装。
GUARD_CRITICAL = "critical"
#: 扫描时顺带看的支撑文件后缀（二进制/图片不扫）。
SCANNABLE_SUFFIXES: frozenset[str] = frozenset({".md", ".txt", ".py", ".sh", ".bash", ".ps1", ".json", ".yaml", ".yml"})
#: 单目录文件数上限（Hermes `MAX_FILE_COUNT = 50`）：一个技能包塞进 200 个文件
#: 说明它其实是仓库，不是技能。
MAX_FILES = 50


class SkillHubError(RuntimeError):
    """hub 的可预期失败（包不存在、目标已存在、仍有 critical…）。"""


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    """读 JSON 对象。坏文件当空 dict——锁文件坏了不该让整个 hub 不可用。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def dir_hash(root: Path, *, skip: frozenset[str] = HASH_SKIP) -> str:
    """目录内容指纹：``sha256(排序后的 相对路径 + 文件 sha256)``。

    用整包而不是只算 SKILL.md：一个技能的 ``references/`` 被换掉，正文却一字未改，
    只比正文会让 `skills update` 认为「没变化」。排序是为了让指纹与文件系统遍历
    顺序无关（Windows 上 `rglob` 的顺序不保证稳定）。

    ``skip`` 是**顶层**要忽略的文件名（默认只有 ``manifest.json``，理由见常量注释）。
    只匹配顶层：技能正文里出现一个 ``references/manifest.json`` 是内容，不是元数据。
    """
    if not root.is_dir():
        return ""
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        relative = path.relative_to(root).as_posix()
        if relative in skip:
            continue
        try:
            body = path.read_bytes()
        except OSError:
            continue
        digest.update(relative.encode("utf-8"))
        digest.update(hashlib.sha256(body).digest())
    return digest.hexdigest()


def is_remote_target(to: str | Path | None) -> bool:
    """``--to`` 是不是一个**远程**发布目标（`github` / `clawhub` / URL / `注册表/仓库`）。

    抽成模块级函数是因为两处要用同一套判定：`SkillHub._publish_root`（拒绝）与 CLI
    （把拒绝翻成"未实现"的退出码 3）。判定规则写两遍，迟早在某一边漏掉一种写法。

    只看形状，不做出网探测——本机没有网络，探测本身就会超时。
    """
    if to is None:
        return False
    text = str(to).strip()
    if not text:
        return False
    lowered = text.lower().rstrip("/")
    # 三种形状：带协议头（`https://…` / `git@…`）、名字就是某个注册表（`github`）、
    # 或 `注册表/仓库` 形式（`github/me/skill`）。相对路径 `mlops/eval-harness` 因此
    # 不会被误判——它的第一段不是注册表名。
    head = lowered.replace("\\", "/").split("/")[0]
    return bool(
        "://" in text
        or lowered.startswith(("git@", "ssh:"))
        or lowered in REMOTE_TARGETS
        or (head in REMOTE_TARGETS and "\\" not in text and not Path(text).is_absolute())
    )


@dataclass(frozen=True)
class ScanReport:
    """一次安装前扫描的结果：guard 威胁 + linter 结果 + 判决。"""

    name: str
    threats: tuple[ThreatFinding, ...] = ()
    lint_findings: tuple[Finding, ...] = ()
    files: int = 0

    @property
    def critical(self) -> tuple[ThreatFinding, ...]:
        return tuple(item for item in self.threats if item.severity == GUARD_CRITICAL)

    @property
    def verdict(self) -> str:
        """``blocked``（有 critical）/ ``caution``（只有 high/medium 或 lint ERROR）/
        ``safe``。三档而不是两档：把「能装但值得看一眼」和「干净」混成一档，
        等于让 caution 永远不被人看见。"""
        if self.critical:
            return "blocked"
        if self.threats or has_errors(list(self.lint_findings)):
            return "caution"
        return "safe"

    @property
    def allowed(self) -> bool:
        """放行条件：**零 critical**。lint 与 high/medium 只记录不阻断。"""
        return not self.critical

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "verdict": self.verdict,
            "allowed": self.allowed,
            "files": self.files,
            "critical": [item.as_dict() for item in self.critical],
            "threats": [item.as_dict() for item in self.threats],
            "lint": [item.as_dict() for item in self.lint_findings],
        }


class SkillHub:
    """本地目录形式的技能 hub。一个实例 = 一份 ``data_dir`` 的 hub 视图。"""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        index: SkillIndex | None = None,
        now: Any = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.index = index if index is not None else SkillIndex(self.data_dir)
        self._now = now

    def now(self) -> datetime:
        return self._now() if self._now is not None else datetime.now(UTC)

    # —— 路径 ——

    @property
    def hub_root(self) -> Path:
        return self.data_dir / HUB_DIRNAME

    @property
    def quarantine_root(self) -> Path:
        return self.data_dir / QUARANTINE_DIRNAME

    @property
    def install_root(self) -> Path:
        return self.data_dir / INSTALL_DIRNAME

    @property
    def lock_path(self) -> Path:
        return self.data_dir / LOCK_FILENAME

    @property
    def audit_path(self) -> Path:
        return self.data_dir / AUDIT_FILENAME

    # —— catalog ——

    def catalog(self) -> list[dict[str, Any]]:
        """列出「远端」里可安装的技能。类目可嵌套，``identifier`` 是相对路径。

        没有 SKILL.md 的目录直接跳过（那是类目桶，不是技能）。
        """
        entries: list[dict[str, Any]] = []
        if not self.hub_root.is_dir():
            return entries
        for path in sorted(self.hub_root.rglob(SKILL_FILENAME)):
            if not path.is_file():
                continue
            directory = path.parent
            identifier = directory.relative_to(self.hub_root).as_posix()
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            report = validate_frontmatter(text)
            name = str(report.data.get("name") or directory.name) if report.ok else directory.name
            description = (
                index_description(str(report.data.get("description") or "")) if report.ok else ""
            )
            entries.append(
                {
                    "name": name,
                    "identifier": identifier,
                    "description": description,
                    "path": str(path),
                    "installed": self._installed_identifier(name) == identifier,
                    "frontmatter_ok": report.ok,
                }
            )
        entries.sort(key=lambda item: item["identifier"])
        return entries

    def search(self, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """在本地 hub 目录里按名字/描述/identifier 过滤（大小写不敏感）。

        这是 `orbit skills search` 的落地：**没有远端可搜**，所以搜的是本机那份
        模拟远端的目录。空 query 返回全部（= `catalog` 的别名）。
        """
        needle = (query or "").strip().lower()
        entries = self.catalog()
        if needle:
            entries = [
                item
                for item in entries
                if needle in str(item["name"]).lower()
                or needle in str(item["identifier"]).lower()
                or needle in str(item["description"]).lower()
            ]
        return entries[: max(1, int(limit))]

    def resolve(self, identifier: str) -> tuple[Path, str]:
        """把 identifier（相对路径或技能名）解析成 ``(目录, 技能名)``。"""
        clean = (identifier or "").strip().strip("/\\")
        if not clean:
            raise SkillHubError("需要一个技能标识（本地 hub 里的名字或类目/名字）")
        direct = self.hub_root / clean
        if (direct / SKILL_FILENAME).is_file():
            return direct, self._name_of(direct)
        for entry in self.catalog():
            if clean in (entry["name"], entry["identifier"]):
                return self.hub_root / str(entry["identifier"]), str(entry["name"])
        raise SkillHubError(
            f"本地 hub 里没有 {clean!r}（hub 目录：{self.hub_root}）。"
            "把技能包放到那里再装，或 `orbit skills search` 看看有什么"
        )

    def _name_of(self, directory: Path) -> str:
        """目录对应的技能名：优先 frontmatter 的 name，坏文件退回目录名。"""
        try:
            text = (directory / SKILL_FILENAME).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return directory.name
        report = validate_frontmatter(text)
        if report.ok and str(report.data.get("name") or "").strip():
            return str(report.data["name"]).strip()
        return directory.name

    # —— 扫描 ——

    def scan_dir(self, directory: Path, name: str = "") -> ScanReport:
        """扫一个技能目录：guard 威胁模式（全部可扫文件）+ linter（SKILL.md）。

        扫描对象是**隔离区里的副本**，与最终搬进正式目录的是同一份字节——这是
        「扫的和装的是同一个东西」的唯一保证。
        """
        skill_name = name or directory.name
        threats: list[ThreatFinding] = []
        lint_findings: list[Finding] = []
        files = 0
        skill_md = directory / SKILL_FILENAME
        if skill_md.is_file():
            files += 1
            try:
                text = skill_md.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            threats.extend(scan_skill(text))
            lint_findings.extend(lint(skill_name, text, path=skill_md))
        for path in sorted(item for item in directory.rglob("*") if item.is_file()):
            if path == skill_md:
                continue
            files += 1
            if path.suffix.lower() not in SCANNABLE_SUFFIXES:
                continue
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            where = path.relative_to(directory).as_posix()
            threats.extend(scan_text(body, where=where))
        if files > MAX_FILES:
            # 不阻断（Hermes 也只是告警）：但要让「这其实是仓库」这件事可见
            threats.append(
                ThreatFinding(
                    pattern_id="too_many_files",
                    severity="low",
                    category="structure",
                    where=".",
                    line=0,
                    excerpt=f"{files} 个文件 > {MAX_FILES}",
                )
            )
        threats.sort(key=lambda item: (SEVERITY_RANK.get(item.severity, 9), item.pattern_id))
        return ScanReport(
            name=skill_name,
            threats=tuple(threats),
            lint_findings=tuple(lint_findings),
            files=files,
        )

    def scan(self, identifier: str) -> ScanReport:
        """扫 hub 里的某个包（安装前预览，`skills inspect` 也能用）。"""
        directory, name = self.resolve(identifier)
        return self.scan_dir(directory, name)

    # —— 安装 / 隔离 ——

    def install(
        self, identifier: str, *, force: bool = False, name: str | None = None
    ) -> dict[str, Any]:
        """安装：``hub → quarantine → scan → (零 critical) → skills/``。

        返回报告（含 ``quarantined`` / ``verdict`` / ``critical``），**不抛异常表示
        被拦**——「装上了」与「被隔离」都是正常结果，调用方按字段分支即可。
        目标已存在且没给 ``force`` 才抛 ``SkillHubError``（那是参数错误，不是扫描结论）。

        ``name`` 是 Hermes ``skills install --name`` 的落地：覆盖目标目录名/锁键
        （用于「frontmatter 里没有 name」或「与本地已有技能重名」的场合）。
        覆盖后的名字同时用于扫描报告与 lock，三者不会各说各话。
        """
        source, resolved = self.resolve(identifier)
        name = (name or "").strip() or resolved
        target = self.install_root / name
        if target.exists() and not force:
            raise SkillHubError(
                f"已安装同名技能：{target}（要覆盖请加 --force；本地改动会被覆盖）"
            )
        staged = self.quarantine_root / name
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        staged.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, staged)
        report = self.scan_dir(staged, name)
        self._write_quarantine_meta(staged, report, identifier=str(identifier))
        if not report.allowed:
            self._append_audit("QUARANTINE", name, identifier=str(identifier),
                               verdict=report.verdict,
                               critical=len(report.critical))
            return {
                "ok": False,
                "name": name,
                "identifier": str(identifier),
                "quarantined": True,
                "verdict": report.verdict,
                "scan": report.as_dict(),
                "path": str(staged),
            }
        return self._promote(staged, name, identifier=str(identifier), report=report,
                             replace=force)

    def _promote(
        self,
        staged: Path,
        name: str,
        *,
        identifier: str,
        report: ScanReport,
        replace: bool,
    ) -> dict[str, Any]:
        """把隔离区里的技能搬进正式目录，并记 lock + audit。"""
        # 隔离报告是**隔离区自己的记账**，不是技能内容。留在包里会让
        # `dir_hash(已安装) != dir_hash(hub 包)`，于是刚装完就显示「有更新」——
        # 一个永远为真的「有新版本」提示，比没有提示更糟。
        meta = staged / QUARANTINE_META_FILENAME
        if meta.is_file():
            meta.unlink()
        target = self.install_root / name
        if target.exists():
            if not replace:
                raise SkillHubError(f"正式目录里已有同名技能：{target}")
            shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(staged), str(target))
        skill_hash = dir_hash(target)
        lock = self._read_lock()
        lock[name] = {
            "name": name,
            "identifier": identifier,
            "installed_at": _iso(self.now()),
            "skill_hash": skill_hash,
            "scan_verdict": report.verdict,
            "path": str(target),
        }
        self._write_lock(lock)
        self._append_audit("INSTALL", name, identifier=identifier, verdict=report.verdict)
        self.index.invalidate()
        return {
            "ok": True,
            "name": name,
            "identifier": identifier,
            "quarantined": False,
            "verdict": report.verdict,
            "scan": report.as_dict(),
            "path": str(target),
            "skill_hash": skill_hash,
        }

    def list_quarantine(self) -> list[dict[str, Any]]:
        """列出隔离区里的技能（含「为什么被拦」的报告）。"""
        if not self.quarantine_root.is_dir():
            return []
        rows: list[dict[str, Any]] = []
        for entry in sorted(self.quarantine_root.iterdir()):
            if not entry.is_dir() or not (entry / SKILL_FILENAME).is_file():
                continue
            meta = _read_json(entry / QUARANTINE_META_FILENAME)
            rows.append(
                {
                    "name": str(meta.get("name") or entry.name),
                    "path": str(entry),
                    "identifier": str(meta.get("identifier") or ""),
                    "verdict": str(meta.get("verdict") or "unknown"),
                    "critical": list(meta.get("critical") or []),
                    "scanned_at": str(meta.get("scanned_at") or ""),
                }
            )
        return rows

    def release(self, name: str) -> dict[str, Any]:
        """手动放行一个隔离技能：**重扫一遍**，零 critical 才搬进正式目录。

        为什么不给 `--force` 直通：Hermes 的 `--force` 明确**不能**覆盖 dangerous
        判决（`should_allow_install` 989/1002-1008 行）。一个「反正能强装」的开关会
        让隔离区彻底失去意义——它的全部价值就在于「这条结论不能被随手绕开」。
        """
        clean = (name or "").strip()
        staged = self.quarantine_root / clean
        if not (staged / SKILL_FILENAME).is_file():
            raise SkillHubError(f"隔离区里没有技能：{clean}（先 `orbit skills quarantine` 看列表）")
        report = self.scan_dir(staged, clean)
        self._write_quarantine_meta(staged, report, identifier=clean)
        if not report.allowed:
            detail = "；".join(
                f"{item.pattern_id}({item.where}:{item.line})" for item in report.critical
            )
            raise SkillHubError(
                f"拒绝放行 {clean}：仍有 {len(report.critical)} 条 critical — {detail}。"
                "隔离区的结论不能被绕过；确认安全请先修内容，再重扫"
            )
        result = self._promote(staged, clean, identifier=clean, report=report, replace=True)
        self._append_audit("RELEASE", clean, verdict=report.verdict)
        return result

    def purge(self, name: str) -> bool:
        """彻底删掉隔离区里的一个技能（不可恢复）。返回是否真的删掉了。"""
        staged = self.quarantine_root / (name or "").strip()
        if not staged.is_dir():
            return False
        shutil.rmtree(staged, ignore_errors=True)
        self._append_audit("PURGE", (name or "").strip())
        return not staged.exists()

    # —— 已安装视图 ——

    def list_installed(self) -> list[dict[str, Any]]:
        """列出 hub 安装的技能（来自 lock，不是「磁盘上有哪些技能」）。"""
        rows: list[dict[str, Any]] = []
        for name, entry in sorted(self._read_lock().items()):
            rows.append(
                {
                    "name": name,
                    "identifier": str(entry.get("identifier") or ""),
                    "installed_at": str(entry.get("installed_at") or ""),
                    "scan_verdict": str(entry.get("scan_verdict") or ""),
                    "skill_hash": str(entry.get("skill_hash") or ""),
                    "path": str(entry.get("path") or self.install_root / name),
                    "present": (self.install_root / name).is_dir(),
                }
            )
        return rows

    def updates(self) -> list[dict[str, Any]]:
        """比对 hub 包与已安装副本，返回有更新的条目。

        「本地被改过」与「hub 更新过」是两件事：前者要 `--force` 才覆盖（否则用户
        的修改会静默消失），后者才是常规更新。所以这里同时给出 ``local_modified``。
        """
        rows: list[dict[str, Any]] = []
        for name, entry in sorted(self._read_lock().items()):
            installed = self.install_root / name
            if not installed.is_dir():
                continue
            identifier = str(entry.get("identifier") or name)
            try:
                source, _ = self.resolve(identifier)
            except SkillHubError:
                continue
            current = dir_hash(installed)
            recorded = str(entry.get("skill_hash") or "")
            incoming = dir_hash(source)
            rows.append(
                {
                    "name": name,
                    "identifier": identifier,
                    "local_modified": bool(recorded) and current != recorded,
                    "outdated": bool(incoming) and current != incoming,
                    "installed_hash": current,
                    "hub_hash": incoming,
                }
            )
        return rows

    def uninstall(self, name: str) -> dict[str, Any]:
        """卸载一个 **hub 安装的** 技能。

        只删 lock 里记录过的：用户手写/归档恢复的技能不在 lock 里，删它们属于
        `curator archive` 的职责。把「删目录」这种不可逆动作限制在「我们知道它是
        谁装进来的」范围内，是这条命令唯一的安全依据。
        """
        clean = (name or "").strip()
        lock = self._read_lock()
        if clean not in lock:
            raise SkillHubError(
                f"{clean!r} 不是 hub 安装的技能（lock 里没有记录）。"
                "本地技能请用 `orbit curator archive`，它可恢复"
            )
        target = self.install_root / clean
        removed = False
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
            removed = not target.exists()
        lock.pop(clean, None)
        self._write_lock(lock)
        self._append_audit("UNINSTALL", clean, removed=removed)
        self.index.invalidate()
        return {"ok": True, "name": clean, "removed": removed, "path": str(target)}

    # —— 发布（打包进本地 hub）——

    def publish(
        self,
        name: str,
        *,
        to: str | Path | None = None,
        dry_run: bool = True,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """把技能打包成发布物，落到本地 hub 目录（默认 ``<data>/skills-hub/<名字>/``）。

        参数：

        - ``name``：目录路径、已安装技能名，或 hub 里的 identifier（三种都认，见
          :meth:`_publish_source`）。
        - ``to``：**只接受本地路径**。``None`` = 本地 hub 根。``github`` / ``clawhub`` /
          URL 一律抛「未实现」，理由见模块 docstring 与 :meth:`_publish_root`。
        - ``dry_run``：默认 ``True``（只报告，不写盘）。``False`` 才真的复制 + 写
          ``manifest.json``。

        **门禁**：``SKILL.md`` 必须先过 `skill_lint` 零 ERROR。这是发布与安装的
        关键差异——安装时 lint 只记 `caution`（装到自己机器上，隔离区还能复查），
        发布是把内容推给别人，别人没有这个机会。

        返回报告（``ok`` / ``dry_run`` / ``wrote`` / ``target`` / ``content_hash`` /
        ``manifest`` / ``lint``）。失败一律抛 :class:`SkillHubError`：发布是显式动作，
        静默返回 ``ok=False`` 会被脚本当成成功。
        """
        source, _ = self._publish_source(name)
        # 发布物的**身份**是 frontmatter 里的 name，不是源目录名：用户可能把技能放在
        # 任意名字的目录里（`my-work/demo/SKILL.md`）。用目录名会在 hub 里造出一个
        # `my-work` 包，而 `install demo` 要的是 `demo`——同一个技能两个名字。
        skill_name = self._publish_name(source)
        destination = self._publish_root(to)
        target = destination / skill_name
        skill_md = source / SKILL_FILENAME
        try:
            text = skill_md.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise SkillHubError(f"读不出技能正文：{skill_md}（{exc}）") from exc
        findings = lint(skill_name, text, path=skill_md)
        if has_errors(findings):
            detail = "；".join(
                f"{item.code}(第 {item.line} 行)" if item.line else item.code
                for item in findings
                if item.severity == "error"
            )
            raise SkillHubError(
                f"拒绝发布 {skill_name}：lint 有 ERROR（{detail}）。"
                "发布物会被别人直接安装，所以门槛是零 ERROR；先 `orbit skills lint` 修干净"
            )
        content_hash = dir_hash(source)
        if not content_hash:
            raise SkillHubError(f"技能目录是空的或读不出文件：{source}")
        report = validate_frontmatter(text)
        meta = dict(report.data) if report.ok else {}
        manifest = self._manifest(
            skill_name, source=source, content_hash=content_hash, meta=meta, now=now
        )
        existed = (target / SKILL_FILENAME).is_file()
        previous = dir_hash(target) if existed else ""
        payload: dict[str, Any] = {
            "ok": True,
            "name": skill_name,
            "dry_run": bool(dry_run),
            "wrote": False,
            "source": str(source),
            "target": str(target),
            "content_hash": content_hash,
            "overwrites": existed,
            "changed": existed and previous != content_hash,
            "manifest": manifest,
            "lint": {
                "errors": sum(1 for item in findings if item.severity == "error"),
                "warnings": sum(1 for item in findings if item.severity != "error"),
                "findings": [item.as_dict() for item in findings],
            },
        }
        if dry_run:
            return payload
        self._write_package(source, target)
        (target / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        payload["wrote"] = True
        payload["files"] = len([item for item in target.rglob("*") if item.is_file()])
        self._append_audit(
            "PUBLISH", skill_name, target=str(target), content_hash=content_hash
        )
        return payload

    def _publish_name(self, directory: Path) -> str:
        """发布物在 hub 里的目录名：优先 frontmatter 的 ``name``，非法则退回目录名。

        **必须校验**：frontmatter 是技能内容的一部分，`name: ../../evil` 会让
        ``destination / name`` 落到 hub 目录之外——发布是写盘动作，路径穿越在这里
        比在任何地方都危险。校验口径与 `app/skills.py::_validate_name` 一致（只允许
        字母数字与 `_-`），不合法就退回目录名（那已经是一个真实存在的目录名）。
        """
        candidate = self._name_of(directory)
        if re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$", candidate or ""):
            return candidate
        return directory.name

    def _publish_source(self, name: str) -> tuple[Path, str]:
        """把 ``publish`` 的入参解析成 ``(源目录, 技能名)``。

        三种写法都认，顺序是**从最具体到最笼统**：

        1. 一个真的目录路径（含 ``SKILL.md``）——发布自己手写的技能；
        2. 已安装的技能名（``<data>/skills/<名字>``）；
        3. hub 里的 identifier（类目/名字）——把 hub 里现有的包重新打包（换目标目录时用）。

        第 1 种必须排在最前：用户明确给了一个路径，就该以那个路径为准，而不是猜他
        指的是同名的已安装技能。
        """
        raw = str(name or "").strip()
        if not raw:
            raise SkillHubError("需要一个技能：目录路径，或已安装的技能名")
        candidate = Path(raw).expanduser()
        if candidate.is_dir() and (candidate / SKILL_FILENAME).is_file():
            return candidate, candidate.name
        installed = self.install_root / raw
        if (installed / SKILL_FILENAME).is_file():
            return installed, raw
        directory, resolved = self.resolve(raw)  # 找不到会抛带 hub 路径的 SkillHubError
        return directory, resolved

    def _publish_root(self, to: str | Path | None) -> Path:
        """发布目标目录。**远程目标在这里被明确拒绝**。

        为什么不做"远程发布"：`hermes skills publish --to github` 要做的是
        （1）出网（2）用账号 token 往别人的社区索引里写一条。本机两条都不具备。
        写一个 HTTP 壳只会得到"永远超时"，而超时看起来像网络故障、不像"没实现"——
        用户会反复重试。所以这里直接说清楚，并给出本机能做的替代。
        """
        if to is None:
            return self.hub_root
        text = str(to).strip()
        if not text:
            return self.hub_root
        if is_remote_target(text):
            raise SkillHubError(
                f"远程发布未实现：{text!r}。发布到社区索引需要网络 + 注册表账号 + 社区索引协议，"
                "本机都没有。本地做法：`orbit skills publish <技能> --to <本地目录> --apply`，"
                f"产物是一份 SKILL.md + manifest.json（默认落到 {self.hub_root}）"
            )
        return Path(text).expanduser()

    def _manifest(
        self,
        name: str,
        *,
        source: Path,
        content_hash: str,
        meta: dict[str, Any],
        now: datetime | None,
    ) -> dict[str, Any]:
        """生成 `manifest.json` 的内容。

        字段只放**能从技能本身读到的东西**，读不到就留空/留默认，不编：

        - ``name`` / ``version`` / ``description``：frontmatter（没有 version 记 1）；
        - ``content_hash``：整包指纹（= `dir_hash`，不含本文件）；
        - ``dependencies`` / ``author``：frontmatter 里的同名字段（Hermes 的技能包会在
          这里声明"要先装什么"和"谁写的"）。Orbit 的 `save_skill` 不写这两个字段，
          所以手写技能通常拿到 ``[]`` 与 ``""``——这是事实，不是缺漏。
        """
        moment = now if now is not None else self.now()
        dependencies = meta.get("dependencies") or []
        if isinstance(dependencies, str):
            dependencies = [item.strip() for item in dependencies.split(",") if item.strip()]
        version = meta.get("version")
        try:
            version_number = int(str(version).strip())
        except (TypeError, ValueError):
            version_number = 1
        return {
            "name": str(meta.get("name") or name),
            "version": version_number,
            "content_hash": content_hash,
            "dependencies": [str(item) for item in dependencies],
            "author": str(meta.get("author") or ""),
            "description": str(meta.get("description") or ""),
            "scope": str(meta.get("scope") or "global"),
            "triggers": [str(item) for item in meta.get("triggers") or []],
            "files": len([item for item in source.rglob("*") if item.is_file()]),
            "published_at": _iso(moment),
            "publisher": "orbit",
            "format": 1,
        }

    def _write_package(self, source: Path, target: Path) -> None:
        """把技能目录整份复制到发布目标（覆盖旧内容，但不留旧残骸）。

        为什么先删再复制：`copytree(dirs_exist_ok=True)` 会把**上一版有、这一版没有**
        的文件留在目标里，于是发布物的内容 hash 与源目录对不上——而 hash 正是
        "发出去的是哪一份"的唯一凭据。
        """
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, target)

    # —— 远端（明确未实现）——

    def install_remote(self, identifier: str) -> None:
        """远端安装：**未实现**（本机无网络）。

        抛异常而不是返回空结果：`orbit skills install openai/skills/x` 必须让人
        知道「什么都没发生」以及「该怎么做」。假装成功比拒绝更糟。
        """
        raise SkillHubError(
            f"远端 hub 未实现：本机无网络，无法拉取 {identifier!r}。"
            f"本地做法：把技能包放到 {self.hub_root}\\<名字>\\SKILL.md，"
            f"再 `orbit skills install <名字>`（走 quarantine 扫描后才会生效）"
        )

    # —— lock / audit ——

    def _read_lock(self) -> dict[str, dict[str, Any]]:
        payload = _read_json(self.lock_path)
        installed = payload.get("installed")
        if isinstance(installed, dict):
            return {str(key): dict(value) for key, value in installed.items()
                    if isinstance(value, dict)}
        return {}

    def _write_lock(self, installed: dict[str, dict[str, Any]]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "installed": installed}
        self.lock_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    def _installed_identifier(self, name: str) -> str:
        return str((self._read_lock().get(name) or {}).get("identifier") or "")

    def _write_quarantine_meta(self, staged: Path, report: ScanReport, *, identifier: str) -> None:
        payload = {
            "name": report.name,
            "identifier": identifier,
            "verdict": report.verdict,
            "critical": [
                {"pattern_id": item.pattern_id, "where": item.where, "line": item.line}
                for item in report.critical
            ],
            "scanned_at": _iso(self.now()),
            "files": report.files,
        }
        try:
            (staged / QUARANTINE_META_FILENAME).write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError:
            # 报告写不进去不该让安装流程失败：隔离本身已经生效（文件在隔离区）
            pass

    def _append_audit(self, action: str, name: str, **extra: Any) -> None:
        """append-only 审计。写失败不阻断——它是记录，不是门。"""
        record = {"ts": _iso(self.now()), "action": action, "skill": name, **extra}
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass


__all__ = [
    "AUDIT_FILENAME",
    "GUARD_CRITICAL",
    "HASH_SKIP",
    "HUB_DIRNAME",
    "INSTALL_DIRNAME",
    "LOCK_FILENAME",
    "MANIFEST_FILENAME",
    "MAX_FILES",
    "QUARANTINE_DIRNAME",
    "QUARANTINE_META_FILENAME",
    "REMOTE_TARGETS",
    "SKILL_FILENAME",
    "ScanReport",
    "SkillHub",
    "SkillHubError",
    "dir_hash",
    "is_remote_target",
]
