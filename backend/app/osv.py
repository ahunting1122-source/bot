"""OSV 供应链审计（MATRIX 14.5）：**离线能做的那一半，做到底**。

## 先划清楚边界

Hermes 的 `security audit` 是"把依赖清单发给 OSV.dev，拿回通告"。本机没有网络，
而且**不许发真实网络请求**。于是这个模块只有两条路可走：

1. **离线**：把 Orbit 声明的依赖与**实际装上的版本**列出来，做成 OSV query 形状，
   再把"哪些部分只有联网才能知道"如实标出来。这一半是真能用的——它至少能回答
   "我在用什么版本的什么包"，而这恰恰是供应链审计里最容易出错的一步
   （声明的是范围，跑的是另一个版本）。
2. **本地通告库**：:func:`apply_offline_db` 读一份本地 OSV 导出（JSON），
   之后 :func:`audit_local` 就能**真的比对出漏洞**——不联网、不猜。
   有本地库时这一层不是占位，是可用的。

没有本地库又没有网络时，:func:`audit_local` 会带 `network_required=True` 与
`NetworkUnavailable` 的说明，**绝不输出"未发现漏洞"**。理由与
`app/security_audit.py` 的第一原则完全一样：一次比对都没发生，说"通过"就是假的安全感。
需要硬失败的地方传 `strict=True`，那时直接抛 :class:`NetworkUnavailable`。

## 为什么版本号要取"实际装上的"而不是声明的

`pyproject.toml` 里写的是 `fastapi>=0.115,<1`——**那不是版本，是范围**。
拿 `>=0.115` 去问 OSV 问不出东西，拿它去和通告的 `fixed: 0.115.3` 比更是没有意义。
所以 :func:`audit_local` 的版本来源优先级是：

    精确钉死的版本（`==1.2.3` / `1.2.3`） > 当前环境实际安装的版本 > （都没有 → 无法比对）

第三种情况**不猜**：报告里标 `comparable=False` 并说明"装上之后才能确定"。

## 与 `app/security_audit.py` 的分工

- `security_audit`：venv / 插件目录 / MCP 配置三项审计 + `refresh_cache`（**联网**刷缓存）。
- `osv`（本模块）：**依赖清单 → OSV query 形状**的转换、OSV 响应的纯函数解析、
  以及**离线通告库**比对。它不发起任何网络请求。

两者的 `NetworkUnavailable` 是同一个类（从 `security_audit` 导入），
这样调用方只需要 `except NetworkUnavailable` 一处。
"""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.security_audit import (
    SEVERITY_ORDER,
    Finding,
    NetworkUnavailable,
    installed_distributions,
    normalize_name,
    worst,
)

#: PyPI 的 ecosystem 名（OSV 的写法，大小写敏感）。
ECOSYSTEM_PYPI = "PyPI"

#: OSV 单条查询端点。**只写在这里做记录，本模块不调用它**（本机不许联网）。
OSV_QUERY_URL = "https://api.osv.dev/v1/query"

#: 缺网络时的标准说明。**措辞里不许出现"未发现漏洞"**——那句话一旦出现，
#: 无论前后怎么否定都会被扫读成结论。
NETWORK_NOTE = (
    "离线模式：依赖清单已列出，但**漏洞比对需要联网查询 OSV.dev**"
    "（或先用 `apply_offline_db(<本地 OSV 导出>)` 挂一份本地通告库）。"
    "这不等于「没有漏洞」——本轮一次通告比对都没有发生。"
)

#: 需求行解析。支持 `name[extra]>=1,<2` / `name==1.2.3` / `name` / `name @ url`。
_REQUIREMENT = re.compile(
    r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:\[[^\]]*\])?"
    r"\s*(?P<spec>[<>=!~].*)?$"
)

#: 从版本约束里挑出**精确**版本。`==1.2.3` 与裸 `1.2.3` 算，`>=1.2` 不算。
_EXACT = re.compile(r"^(?:==)?\s*v?([0-9][0-9A-Za-z.+-]*)$")

#: GHSA / OSV 的 `database_specific.severity` 取值 → 本项目的五级。
_SEVERITY_ALIASES: dict[str, str] = {
    "critical": "critical",
    "high": "high",
    "moderate": "moderate",
    "medium": "moderate",
    "low": "low",
    "unknown": "unknown",
}

#: 生效中的离线通告库。`apply_offline_db` 写它，`audit_local` 读它。
_ACTIVE_DB: OsvDatabase | None = None


# ── 依赖清单 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Dependency:
    """一条依赖声明。

    `version` 是**确切版本**（能确定时才有值）；`spec` 是原始约束串，原样保留，
    因为"声明的是 `>=0.115,<1`"和"装的是 0.115.6"是两条不同的信息。
    """

    name: str
    version: str = ""
    spec: str = ""
    ecosystem: str = ECOSYSTEM_PYPI
    source: str = ""

    @property
    def pinned(self) -> bool:
        return bool(self.version)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "spec": self.spec,
            "ecosystem": self.ecosystem,
            "source": self.source,
            "pinned": self.pinned,
        }

    def query(self) -> dict[str, Any]:
        """OSV query 形状。没有确切版本时**不带 `version` 键**——
        OSV 会按"这个包的全部通告"返回，这比带一个假版本诚实。"""
        payload: dict[str, Any] = {"package": {"name": self.name, "ecosystem": self.ecosystem}}
        if self.version:
            payload["version"] = self.version
        return payload


def parse_requirement(line: str, *, source: str = "") -> Dependency | None:
    """解析一条依赖声明。看不懂返回 `None`（**不猜**）。"""
    text = (line or "").strip()
    if not text or text.startswith("#") or text.startswith("-"):
        return None
    # 环境标记（`; python_version < "3.12"`）与 URL 依赖都不影响包名解析。
    text = text.split(";", 1)[0].strip()
    if not text:
        return None
    match = _REQUIREMENT.match(text)
    if match is None:
        return None
    name = match.group("name").strip()
    if not name:
        return None
    spec = (match.group("spec") or "").strip()
    exact = _EXACT.match(spec) if spec else None
    return Dependency(
        name=name,
        version=exact.group(1) if exact else "",
        spec=spec,
        source=source,
    )


def _pyproject_dependencies(path: Path) -> list[Dependency]:
    """读 `pyproject.toml` 的 `project.dependencies` / `optional-dependencies` / dev 组。"""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    project = data.get("project")
    if not isinstance(project, Mapping):
        project = {}
    out: list[Dependency] = []
    for item in _as_str_list(project.get("dependencies")):
        dependency = parse_requirement(item, source="pyproject.toml")
        if dependency is not None:
            out.append(dependency)
    optional = project.get("optional-dependencies")
    if isinstance(optional, Mapping):
        for group, items in optional.items():
            for item in _as_str_list(items):
                dependency = parse_requirement(item, source=f"pyproject.toml:{group}")
                if dependency is not None:
                    out.append(dependency)
    groups = data.get("dependency-groups")
    if isinstance(groups, Mapping):
        for group, items in groups.items():
            for item in _as_str_list(items):
                dependency = parse_requirement(item, source=f"pyproject.toml:group:{group}")
                if dependency is not None:
                    out.append(dependency)
    return out


def _requirements_dependencies(path: Path) -> list[Dependency]:
    """读 `requirements.txt`（每行一条）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[Dependency] = []
    for line in text.splitlines():
        dependency = parse_requirement(line, source=path.name)
        if dependency is not None:
            out.append(dependency)
    return out


def _as_str_list(value: Any) -> list[str]:
    """把 TOML 里读到的列表读成字符串列表；坏值当空。"""
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value if isinstance(item, str)]


def default_root() -> Path:
    """仓库根（`app/` 的上一级）。装成包之后这里可能没有 `pyproject.toml`，
    那时 :func:`dependency_specs` 会返回空列表——如实，而不是报错。"""
    return Path(__file__).resolve().parent.parent


def dependency_specs(root: str | Path | None = None) -> list[Dependency]:
    """列出依赖声明。`pyproject.toml` 与 `requirements.txt` 都读（去重保序）。"""
    base = Path(root) if root is not None else default_root()
    found: list[Dependency] = []
    pyproject = base / "pyproject.toml"
    if pyproject.is_file():
        found.extend(_pyproject_dependencies(pyproject))
    requirements = base / "requirements.txt"
    if requirements.is_file():
        found.extend(_requirements_dependencies(requirements))
    # 去重：同一个包可能既在 pyproject 又在 requirements 里。保留第一次出现的
    # 那条（声明顺序即优先级），但把已知的确切版本补上去。
    merged: dict[str, Dependency] = {}
    for dependency in found:
        key = normalize_name(dependency.name)
        existing = merged.get(key)
        if existing is None:
            merged[key] = dependency
        elif not existing.version and dependency.version:
            merged[key] = Dependency(
                name=existing.name,
                version=dependency.version,
                spec=existing.spec,
                ecosystem=existing.ecosystem,
                source=existing.source,
            )
    return list(merged.values())


def osv_queries(dependencies: Sequence[Dependency] | None = None) -> list[dict[str, Any]]:
    """把依赖转成 OSV query 列表。`dependencies=None` 时读仓库里的声明。"""
    items = list(dependencies) if dependencies is not None else dependency_specs()
    return [dependency.query() for dependency in items]


#: 仓库依赖的 OSV query 形状（导入时算一次）。**这是一个快照**：改了
#: `pyproject.toml` 之后要重新导入才更新。CLI 用 :func:`osv_queries` 现算，
#: 这个常量是给"想直接看形状"的人与测试用的。
OSV_QUERIES: tuple[dict[str, Any], ...] = tuple(osv_queries())


# ── 通告解析（纯函数）────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AffectedRange:
    """OSV `affected[]` 里的一条影响范围。

    `ranges[]` 给出的是区间（`introduced` → `fixed`），`versions[]` 给出的是
    **显式列举**。两种都记下来：只保留区间会让"只列举版本"的通告被误判成
    "不影响任何版本"。
    """

    package: str = ""
    ecosystem: str = ""
    introduced: str = ""
    fixed: str = ""
    last_affected: str = ""
    versions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "ecosystem": self.ecosystem,
            "introduced": self.introduced,
            "fixed": self.fixed,
            "last_affected": self.last_affected,
            "versions": list(self.versions),
        }


@dataclass(frozen=True)
class Vuln:
    """一条 OSV 通告。字段只留"能行动"的那几个：是什么、多严重、修到哪个版本。"""

    id: str
    summary: str = ""
    severity: str = "unknown"
    aliases: tuple[str, ...] = ()
    affected: tuple[AffectedRange, ...] = ()
    fixed: tuple[str, ...] = ()
    details: str = ""

    @property
    def rank(self) -> int:
        return SEVERITY_ORDER.get(self.severity, 0)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "summary": self.summary,
            "severity": self.severity,
            "aliases": list(self.aliases),
            "affected": [item.as_dict() for item in self.affected],
            "fixed": list(self.fixed),
            "details": self.details,
        }


def parse_response(payload: Any) -> list[Vuln]:
    """解析 OSV 的响应。**纯函数**：不联网、不落盘、不读全局状态。

    三种输入形状都吃（都是 OSV 真实会返回的）：

    - `{"vulns": [...]}` —— `POST /v1/query` 的响应
    - `{"results": [{"vulns": [...]}, ...]}` —— `POST /v1/querybatch` 的响应
    - `[...]` —— OSV 的"导出/下载"文件就是一个数组

    看不懂的条目**跳过而不是猜**：一条坏记录不该让整次审计变成空。
    """
    records: list[Any] = []
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, Mapping):
        raw = payload.get("vulns")
        if isinstance(raw, list):
            records = raw
        else:
            results = payload.get("results")
            if isinstance(results, list):
                for item in results:
                    if isinstance(item, Mapping) and isinstance(item.get("vulns"), list):
                        records.extend(item["vulns"])
            elif payload.get("id"):
                records = [payload]
    out: list[Vuln] = []
    for record in records:
        parsed = _parse_vuln(record)
        if parsed is not None:
            out.append(parsed)
    return out


def _parse_vuln(record: Any) -> Vuln | None:
    """一条 `vulns[]` → :class:`Vuln`。没有 `id` 的直接丢（无法引用）。"""
    if not isinstance(record, Mapping):
        return None
    identifier = str(record.get("id") or "").strip()
    if not identifier:
        return None
    summary = str(record.get("summary") or "").strip()
    details = str(record.get("details") or "").strip()
    aliases = tuple(str(item) for item in record.get("aliases") or [] if isinstance(item, str))
    ranges: list[AffectedRange] = []
    fixed: list[str] = []
    for affected in record.get("affected") or []:
        if not isinstance(affected, Mapping):
            continue
        package = affected.get("package")
        name = ""
        ecosystem = ""
        if isinstance(package, Mapping):
            name = str(package.get("name") or "")
            ecosystem = str(package.get("ecosystem") or "")
        versions = tuple(
            str(item) for item in affected.get("versions") or [] if isinstance(item, str)
        )
        listed = False
        for entry in affected.get("ranges") or []:
            if not isinstance(entry, Mapping):
                continue
            introduced = ""
            fixed_version = ""
            last_affected = ""
            for event in entry.get("events") or []:
                if not isinstance(event, Mapping):
                    continue
                if "introduced" in event and not introduced:
                    introduced = str(event.get("introduced") or "")
                if "fixed" in event and not fixed_version:
                    fixed_version = str(event.get("fixed") or "")
                if "last_affected" in event and not last_affected:
                    last_affected = str(event.get("last_affected") or "")
            ranges.append(
                AffectedRange(
                    package=name,
                    ecosystem=ecosystem,
                    introduced=introduced,
                    fixed=fixed_version,
                    last_affected=last_affected,
                    versions=versions,
                )
            )
            listed = True
            if fixed_version and fixed_version not in fixed:
                fixed.append(fixed_version)
        if not listed:
            # 只给了 `versions[]`（GHSA 常见）：也要记成一条范围，否则会被判成
            # "不影响任何版本"，也就是漏报。
            ranges.append(
                AffectedRange(package=name, ecosystem=ecosystem, versions=versions)
            )
    return Vuln(
        id=identifier,
        summary=summary or details[:200],
        severity=_severity_of(record),
        aliases=aliases,
        affected=tuple(ranges),
        fixed=tuple(fixed),
        details=details,
    )


def _severity_of(record: Mapping[str, Any]) -> str:
    """取严重度。

    OSV 的 `severity[]` 是 CVSS 向量（`CVSS:3.1/AV:N/...`），算分需要额外逻辑；
    本模块**不算分、不猜级**。优先用 `database_specific.severity`（GHSA 会填），
    没有就记 `unknown`——不知道严重度不等于不严重（`SEVERITY_ORDER` 里它排 0 但仍算命中）。
    """
    database_specific = record.get("database_specific")
    if isinstance(database_specific, Mapping):
        raw = str(database_specific.get("severity") or "").strip().casefold()
        if raw in _SEVERITY_ALIASES:
            return _SEVERITY_ALIASES[raw]
    return "unknown"


# ── 版本比较（有 `packaging` 就用，没有就退回保守实现）───────────────────────


def compare_versions(left: str, right: str) -> int | None:
    """比较两个版本。返回 -1 / 0 / 1；**无法比较返回 `None`**（调用方按"未知"处理）。

    为什么不自己写一套 PEP 440：`packaging` 已经装在本机（是很多包的依赖），
    自己实现只会漏掉 `1.0.0rc1 < 1.0.0` 这类规则。但也不**硬依赖**它——没有时
    退回分段比较，宁可判"未知"也不要判错方向。
    """
    a = str(left or "").strip()
    b = str(right or "").strip()
    if not a or not b:
        return None
    try:
        from packaging.version import InvalidVersion, Version

        try:
            first, second = Version(a), Version(b)
        except InvalidVersion:
            return _compare_segments(a, b)
        if first < second:
            return -1
        return 0 if first == second else 1
    except ImportError:
        return _compare_segments(a, b)


def _compare_segments(left: str, right: str) -> int | None:
    """保守的退路：按数字段比，段数不同就补齐 0；出现非数字段则返回 `None`。"""
    left_parts = _numeric_parts(left)
    right_parts = _numeric_parts(right)
    if left_parts is None or right_parts is None:
        return None
    length = max(len(left_parts), len(right_parts))
    padded_left = left_parts + [0] * (length - len(left_parts))
    padded_right = right_parts + [0] * (length - len(right_parts))
    if padded_left < padded_right:
        return -1
    return 0 if padded_left == padded_right else 1


def _numeric_parts(text: str) -> list[int] | None:
    """`1.2.3` → `[1, 2, 3]`。含非数字段（`1.0rc1`）返回 `None`。"""
    parts: list[int] = []
    for chunk in re.split(r"[._-]", text.strip().lstrip("vV")):
        if not chunk:
            continue
        if not chunk.isdigit():
            return None
        parts.append(int(chunk))
    return parts or None


def _in_range(
    version: str, *, introduced: str, fixed: str, last_affected: str
) -> bool | None:
    """版本是否落在一条 OSV 范围内。`None` = 判断不了（**不猜成命中或未命中**）。"""
    if introduced and introduced != "0":
        order = compare_versions(version, introduced)
        if order is None:
            return None
        if order < 0:
            return False
    if fixed:
        order = compare_versions(version, fixed)
        if order is None:
            return None
        if order >= 0:
            return False
    if last_affected:
        order = compare_versions(version, last_affected)
        if order is None:
            return None
        if order > 0:
            return False
    return True


def _affected_hit(version: str, affected: AffectedRange) -> bool | None:
    """一个版本是否落在一条 `affected[]` 里。`None` = 判断不了。

    先看 `versions[]`（显式列举，最确定），再看区间。区间判断不了时返回 `None`，
    由调用方按"命中"处理——通告本身声称这个包受影响，宁可多报一条也不要漏。
    """
    if affected.versions and version in affected.versions:
        return True
    return _in_range(
        version,
        introduced=affected.introduced,
        fixed=affected.fixed,
        last_affected=affected.last_affected,
    )


# ── 离线通告库 ──────────────────────────────────────────────────────────────


@dataclass
class OsvDatabase:
    """一份本地 OSV 导出。`apply_offline_db` 造它，`audit_local` 用它比对。"""

    path: Path | None = None
    vulns: tuple[Vuln, ...] = ()
    loaded_at: str = ""

    @property
    def advisories(self) -> int:
        return len(self.vulns)

    def advisories_for(
        self, name: str, version: str = "", *, ecosystem: str = ECOSYSTEM_PYPI
    ) -> list[Vuln]:
        """查一个包（+版本）命中的通告。

        匹配规则（**保守**：范围判断不了时算命中，因为通告本身声称受影响）：

        - 包名按 PEP 503 归一化比较；
        - 版本为空时**不做版本过滤**（"装了哪个版本不知道" → 列出全部通告）；
        - 有 `introduced` / `fixed` / `last_affected` 就比版本；
        - 都没有时按"全部版本受影响"处理。
        """
        target = normalize_name(name)
        out: list[Vuln] = []
        for vuln in self.vulns:
            for affected in vuln.affected:
                if not affected.package:
                    continue
                if affected.ecosystem and affected.ecosystem != ecosystem:
                    continue
                if normalize_name(affected.package) != target:
                    continue
                if not version:
                    out.append(vuln)
                    break
                verdict = _affected_hit(version, affected)
                if verdict is None or verdict:
                    out.append(vuln)
                    break
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": str(self.path) if self.path else "",
            "advisories": self.advisories,
            "loaded_at": self.loaded_at,
        }


def load_offline_db(path: str | Path) -> OsvDatabase:
    """读一份本地 OSV 导出。**不改全局状态**（纯加载，测试友好）。

    支持三种形状：JSON 数组、`{"vulns": [...]}`、以及 OSV 官方导出的
    NDJSON（每行一个 JSON 对象）。文件不存在或完全解析不出内容时抛
    :class:`NetworkUnavailable` 的姊妹错误 `ValueError`——因为"库是空的"和
    "没有库"必须区分开，否则离线审计会静默变成空转。
    """
    target = Path(path)
    if not target.is_file():
        raise ValueError(f"本地 OSV 导出不存在：{target}")
    text = target.read_text(encoding="utf-8", errors="replace")
    vulns: list[Vuln] = []
    parsed_any = False
    try:
        payload = json.loads(text)
        parsed_any = True
        vulns.extend(parse_response(payload))
    except ValueError:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            parsed_any = True
            vulns.extend(parse_response([record]))
    if not parsed_any:
        raise ValueError(f"本地 OSV 导出无法解析（既不是 JSON 也不是 NDJSON）：{target}")
    return OsvDatabase(
        path=target,
        vulns=tuple(vulns),
        loaded_at=datetime.now(UTC).isoformat(),
    )


def apply_offline_db(path: str | Path) -> OsvDatabase:
    """加载一份本地 OSV 导出并**设为生效的离线库**。

    这是"有本地库时是真的能用"的那个开关：调用之后 :func:`audit_local` 不再报
    `NetworkUnavailable`，而是真的比对并给出命中。
    """
    global _ACTIVE_DB
    database = load_offline_db(path)
    _ACTIVE_DB = database
    return database


def active_db() -> OsvDatabase | None:
    """当前生效的离线库（没有就 `None`）。"""
    return _ACTIVE_DB


def clear_offline_db() -> None:
    """卸掉离线库。测试与"换一份库"时用。"""
    global _ACTIVE_DB
    _ACTIVE_DB = None


# ── 审计报告 ────────────────────────────────────────────────────────────────


@dataclass
class AuditReport:
    """一次 OSV 审计的结果。

    `network_required` 与 `findings` 是**正交**的：需要联网时 `findings` 必为空，
    但报告**没有通过**——:meth:`passed` 把这两种"空"区分开了（与
    `app/security_audit.py::AuditReport` 同一条原则）。
    """

    kind: str = "osv"
    dependencies: list[Dependency] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    #: 没法定版本的依赖（只有范围约束）。它们**被列出来了但没比对**。
    incomparable: list[str] = field(default_factory=list)
    #: 需要联网才能比对。**这会让 `passed()` 返回 False。**
    network_required: bool = True
    note: str = ""
    database: str = ""
    database_advisories: int = 0

    @property
    def scanned(self) -> int:
        return len(self.dependencies)

    def passed(self) -> bool:
        """真的比过了且没命中才返回 True。需要联网 / 没有可比版本 → False。"""
        return not self.network_required and not self.findings and bool(self.dependencies)

    def headline(self) -> str:
        """一句话结论。**需要联网时绝不出现"未发现漏洞"**。"""
        if not self.dependencies:
            return "未审计（没有读到任何依赖声明）"
        if self.network_required:
            return "未审计（需要联网或本地通告库）"
        if self.findings:
            return f"命中 {len(self.findings)} 条"
        return f"未命中（对本地通告库 {self.database_advisories} 条通告）"

    def worst(self) -> str:
        return worst(self.findings)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "scanned": self.scanned,
            "dependencies": [dependency.as_dict() for dependency in self.dependencies],
            "findings": [finding.as_dict() for finding in self.findings],
            "incomparable": list(self.incomparable),
            "network_required": self.network_required,
            "note": self.note,
            "database": self.database,
            "database_advisories": self.database_advisories,
            "headline": self.headline(),
            "passed": self.passed(),
            "worst": self.worst(),
        }


def audit_local(
    *,
    root: str | Path | None = None,
    site_packages: str | Path | None = None,
    database: OsvDatabase | None = None,
    strict: bool = False,
) -> AuditReport:
    """离线审计：列出依赖 + （有本地库时）真的比对。

    参数：

    - `root`：读 `pyproject.toml` / `requirements.txt` 的目录，默认仓库根。
    - `site_packages`：确定"实际装的是哪个版本"用。默认当前解释器的 site-packages；
      传 `None` 且环境里读不到时，只有钉死版本的依赖可比对。
    - `database`：本地通告库。不传就用 :func:`apply_offline_db` 挂上的那份。
    - `strict`：没有可比对的通告库时**抛** :class:`NetworkUnavailable` 而不是返回
      "需要联网"的报告。CI 里应当传 `True`——退出码比一段文字更难被忽略。
    """
    dependencies = _with_installed_versions(
        dependency_specs(root),
        Path(site_packages) if site_packages is not None else _default_site_packages(),
    )
    report = AuditReport(dependencies=dependencies)

    if not dependencies:
        report.note = (
            "没有读到任何依赖声明（既没有 pyproject.toml 也没有 requirements.txt）。"
            "这不是「没有漏洞」，是「没有东西可比」。"
        )
        report.network_required = False
        return report

    active = database if database is not None else _ACTIVE_DB
    if active is None:
        report.network_required = True
        report.note = NETWORK_NOTE
        if strict:
            raise NetworkUnavailable(NETWORK_NOTE)
        return report

    report.network_required = False
    report.database = str(active.path) if active.path else "（内存中的通告库）"
    report.database_advisories = active.advisories
    for dependency in dependencies:
        if not dependency.version:
            report.incomparable.append(dependency.name)
            continue
        for vuln in active.advisories_for(
            dependency.name, dependency.version, ecosystem=dependency.ecosystem
        ):
            fixed = "、".join(vuln.fixed) if vuln.fixed else "（通告里没给修复版本）"
            report.findings.append(
                Finding(
                    target=f"{dependency.name}=={dependency.version}",
                    detail=f"{vuln.id}：{vuln.summary}（修复版本：{fixed}）",
                    severity=vuln.severity,
                    advisory=vuln.id,
                    source="osv",
                )
            )
    if report.incomparable:
        report.note = (
            f"有 {len(report.incomparable)} 个依赖没有确切版本（只有范围约束，"
            f"且本机环境里查不到安装版本），未参与比对：{'、'.join(report.incomparable)}。"
            "装好依赖之后重跑才能覆盖它们。"
        )
    return report


def _default_site_packages() -> Path:
    """当前解释器的 site-packages（复用 `security_audit` 的跨平台实现）。"""
    from app.security_audit import default_site_packages

    return default_site_packages()


def _with_installed_versions(
    dependencies: Sequence[Dependency], site_packages: Path
) -> list[Dependency]:
    """把"声明"补成"声明 + 实际版本"。

    优先级：声明里钉死的版本 > 环境里实际安装的版本 > 留空（无法比对）。
    为什么钉死的优先：`==1.2.3` 是**作者明确要求**的版本，环境里装成别的说明
    环境有问题，报出来的应该是作者声明的那个，而不是把问题藏起来。
    """
    installed: dict[str, str] = {}
    try:
        installed = {
            normalize_name(name): version
            for name, version in installed_distributions(site_packages).items()
        }
    except OSError:
        installed = {}
    out: list[Dependency] = []
    for dependency in dependencies:
        if dependency.version:
            out.append(dependency)
            continue
        version = installed.get(normalize_name(dependency.name), "")
        out.append(
            Dependency(
                name=dependency.name,
                version=version,
                spec=dependency.spec,
                ecosystem=dependency.ecosystem,
                source=dependency.source,
            )
        )
    return out


def describe() -> str:
    """如实说明这一层能做到哪、做不到哪。CLI 与文档共用同一份文字。"""
    active = _ACTIVE_DB
    where = (
        f"已挂载本地通告库 {active.path}（{active.advisories} 条通告）"
        if active is not None
        else "未挂载本地通告库"
    )
    return (
        "OSV 供应链审计（MATRIX 14.5）分两半：\n"
        "  1) 离线能做的那半（**现在就能用**）：从 pyproject.toml / requirements.txt 读出依赖，"
        "补上环境里实际安装的版本，转成 OSV query 形状并打印出来；\n"
        "  2) 漏洞比对那半：需要 OSV.dev 的通告数据。本机无网络，所以只有两种可用路径——"
        "联网刷新（`app/security_audit.py::refresh_cache`，本机做不到），"
        "或者挂一份本地 OSV 导出（`apply_offline_db`，**有库时是真的能用**）。\n"
        f"当前状态：{where}。\n"
        "没做的：不算 CVSS 分（只读 `database_specific.severity`，没有就记 unknown）、"
        "不查传递依赖（只看声明与已安装清单）、不自动升级。"
    )


def iter_findings(report: AuditReport) -> Iterable[Finding]:
    """遍历报告里的命中（给 CLI 与测试用的窄接口）。"""
    return iter(report.findings)
