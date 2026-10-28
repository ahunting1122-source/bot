"""供应链审计：已安装分发包（OSV 通告）、插件目录（危险模式）、MCP 配置（明文密钥）。

## 这个模块的第一原则：**没有数据时不许说"安全"**

Hermes 的 `security audit` 是"查 OSV.dev"。本机没有网络，也**不许**发真实网络请求，
于是最诱人的错误实现是：拉不到通告 → `findings = []` → 打印"未发现漏洞"。
那是**假的安全感**：用户会据此认为审计过了，而实际上一次比对都没发生。

所以 :func:`audit_venv` 的行为被钉死成两条：

- 本地通告缓存（`<data_dir>/osv-cache.json`）不存在 → `cache_missing=True`，
  并且 `note` 里写"没有本地通告数据，需要联网刷新才能审计"。**`findings` 为空
  但报告不是"通过"**（`AuditReport.passed()` 会返回 `False`）。
- 缓存存在 → 逐条比对，命中的写进 `findings`；**没命中也只代表"对这份缓存没命中"**，
  报告里带 `cache_updated_at` 让人自己判断缓存有多旧。

缓存由 :func:`refresh_cache` 刷新。它**必须可注入 transport**（`httpx.MockTransport`），
否则测试就只能靠真网络——那在本机是不可能也不允许的。

## 与 Hermes 的差异（刻意的）

- Hermes 的 `--fail-on` 默认 `critical`，低于该级别退出码仍是 0。Orbit 更严：
  **任何命中都退出 1**。理由：供应链命中不是"告警级别问题"，静默的 0 退出码会被
  CI 直接吞掉。`--fail-on` 只能把门槛**降低**，不能放宽到 0（见 CLI 模块的说明）。
- Hermes 扫 `~/.hermes/plugins/` 的 requirements 文件；Orbit 的插件是**目录**，
  所以按 `plugin_guard` 的思路扫源码形态（见 :data:`PLUGIN_PATTERNS`）。
"""

from __future__ import annotations

import json
import re
import sys
import sysconfig
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.redact import is_secret_name, redact

#: 本地通告缓存的文件名（相对 `data_dir`）。
CACHE_NAME = "osv-cache.json"

#: 缓存缺失时的标准提示语。**CLI 直接打印它**，避免"未发现漏洞"被误读。
#:
#: 措辞刻意避开"未发现漏洞"这五个字：那句话一旦出现在输出里，无论前后怎么否定，
#: 都会被扫读成结论。这里说「没有漏洞」并紧跟"一次比对都没有发生"。
CACHE_MISSING_NOTE = (
    "没有本地通告数据，需要联网刷新才能审计。"
    "这不等于「没有漏洞」——本轮一次通告比对都没有发生。"
)

#: OSV 的批量查询端点。用 querybatch 而不是 query：一个 venv 有上百个包，
#: 逐个查会变成上百次请求。
OSV_BATCH_URL = "https://api.osv.dev/v1/querybatch"

#: 严重度排序。`unknown` 排最低但**仍然算命中**——不知道严重度不等于不严重。
SEVERITY_ORDER: dict[str, int] = {
    "unknown": 0,
    "low": 1,
    "moderate": 2,
    "high": 3,
    "critical": 4,
}

#: 插件扫描跳过的目录（VCS 内部、缓存、vendored 环境）——照 `plugin_guard.EXCLUDED_DIRS`。
EXCLUDED_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
    }
)

#: 需要按"代码"看的后缀：危险模式只在这些文件里找。
CODE_SUFFIXES: frozenset[str] = frozenset(
    {".py", ".js", ".mjs", ".ts", ".tsx", ".jsx", ".sh", ".bash", ".ps1", ".rb", ".php"}
)

#: 需要按"文本"看的后缀：密钥扫描在这些文件里找（文档里的密钥同样是密钥）。
TEXT_SUFFIXES: frozenset[str] = CODE_SUFFIXES | {
    ".yaml",
    ".yml",
    ".json",
    ".toml",
    ".ini",
    ".cfg",
    ".md",
    ".txt",
    ".env",
}

#: 结构限制——插件是真实代码库，比技能大得多，但仍然要有上限，否则一次审计
#: 可能扫掉几个 GB（`plugin_guard` 的同类常量）。
MAX_FILES = 400
MAX_FILE_KB = 1024
MAX_TOTAL_KB = 10 * 1024

#: 插件源码里的危险模式。`(正则, 规则名, 严重度, 中文说明)`。
#:
#: 这里**刻意不把 `subprocess` / `os.environ` 当成恶意**：插件本来就要启动子进程、
#: 读自己的 API key（`plugin_guard` 的注释把这件事讲得很清楚）。所以这里的判据是
#: "形态可疑"，不是"有这个调用就是坏的"——结论要人看，不要机器定罪。
PLUGIN_PATTERNS: tuple[tuple[re.Pattern[str], str, str, str], ...] = (
    (re.compile(r"\bos\.system\s*\("), "os_system", "high", "通过 shell 执行命令（os.system）"),
    (re.compile(r"\bos\.popen\s*\("), "os_popen", "high", "通过管道执行命令（os.popen）"),
    (
        re.compile(r"\bsubprocess\.(?:run|Popen|call|check_output|check_call)\b"),
        "subprocess",
        "medium",
        "启动外部进程（subprocess）",
    ),
    (re.compile(r"(?<![\w.])eval\s*\("), "eval", "high", "动态求值字符串（eval）"),
    (re.compile(r"(?<![\w.])exec\s*\("), "exec", "high", "动态执行代码（exec）"),
    (re.compile(r"\b__import__\s*\("), "dynamic_import", "medium", "动态导入（__import__）"),
    (re.compile(r"\bcompile\s*\([^)]*['\"]exec['\"]"), "compile_exec", "high", "compile(..., 'exec')"),
    (re.compile(r"\bshutil\.rmtree\s*\("), "rmtree", "medium", "递归删除目录（shutil.rmtree）"),
    (
        re.compile(
            r"(?:[A-Za-z]:\\Windows|/etc/|/usr/(?:local/)?(?:bin|sbin)|/bin/|/sbin/"
            r"|%WINDIR%|%SystemRoot%|ProgramData|Program\s+Files)",
            re.IGNORECASE,
        ),
        "system_path",
        "medium",
        "引用系统目录路径（可能写入系统位置）",
    ),
    (
        re.compile(r"base64\s*\.\s*b64decode\s*\([^)]*\)\s*\)?\s*(?:\.decode|exec|eval)"),
        "obfuscated_exec",
        "high",
        "解码后执行（混淆载荷）",
    ),
)

#: MCP `command` 里"值得看一眼"的可执行文件。
#:
#: `sh -c` / `cmd /c` / `powershell -c` 是最常见的**隐藏真实命令**的手法；
#: `curl` / `wget` / `nc` 一类是"从网络取内容再执行"的前置。
SUSPICIOUS_COMMANDS: dict[str, str] = {
    "sh": "用 shell 包装启动（真实命令藏在 -c 后面）",
    "bash": "用 shell 包装启动（真实命令藏在 -c 后面）",
    "zsh": "用 shell 包装启动（真实命令藏在 -c 后面）",
    "cmd": "用 shell 包装启动（真实命令藏在 /c 后面）",
    "powershell": "用 PowerShell 包装启动（真实命令藏在 -c 后面）",
    "pwsh": "用 PowerShell 包装启动（真实命令藏在 -c 后面）",
    "curl": "从网络取内容（常与管道执行连用）",
    "wget": "从网络取内容（常与管道执行连用）",
    "nc": "网络工具（可能是反弹 shell）",
    "ncat": "网络工具（可能是反弹 shell）",
    "socat": "网络工具（可能是反弹 shell）",
}

#: 临时目录：从临时目录启动的可执行文件通常是"先落盘再运行"的载荷。
TEMP_MARKERS: tuple[str, ...] = ("/tmp/", "/var/tmp/", "\\temp\\", "\\tmp\\", "%temp%")


class NetworkUnavailable(RuntimeError):
    """没有网络（或没有可用的 transport）。**必须显式抛出**，不许静默降级成"没漏洞"。"""


@dataclass(frozen=True)
class Finding:
    """一条审计命中。"""

    #: 命中的对象：`包名==版本` / `插件相对路径:行号` / `MCP 服务名`。
    target: str
    #: 人读的说明。
    detail: str
    #: `low` / `moderate` / `high` / `critical` / `unknown`。
    severity: str = "unknown"
    #: 通告 ID（有缓存时才有）。
    advisory: str = ""
    #: `venv` / `plugins` / `mcp`。
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "detail": self.detail,
            "severity": self.severity,
            "advisory": self.advisory,
            "source": self.source,
        }

    @property
    def rank(self) -> int:
        return SEVERITY_ORDER.get(self.severity, 0)


@dataclass
class AuditReport:
    """一次审计的结果。

    `cache_missing` 与 `findings` 是**正交**的两个字段：缓存缺失时 `findings` 必为空，
    但报告**没有通过**——:meth:`passed` 把这两种"空"区分开了。
    """

    kind: str
    scanned: int = 0
    findings: list[Finding] = field(default_factory=list)
    #: 通告缓存不存在（或无法解析）。**这会让 `passed()` 返回 False。**
    cache_missing: bool = False
    #: 真的扫了东西没有。目标目录/文件不存在、配置无法解析时为 False。
    #: 没有 `audited` 这一位，"没东西可扫"和"扫了没命中"会长得一模一样。
    audited: bool = True
    #: 给人读的补充说明（缓存缺失 / 目录不存在 / 解析失败）。
    note: str = ""
    cache_path: str = ""
    cache_updated_at: str = ""

    def passed(self) -> bool:
        """真的"审过了且没命中"才返回 True。缓存缺失 / 没扫成 → False。"""
        return self.audited and not self.findings and not self.cache_missing

    def headline(self) -> str:
        """一句话结论。**缓存缺失时绝不出现"未发现漏洞"**。"""
        # 三种「未审计」必须分开说，否则用户会去查错方向：
        #   scanned == 0  → 路径配错了，该去查路径
        #   scanned > 0   → 路径没问题，缺的是通告库/网络，该去挂库或联网
        # 原来两者共用「目标不存在或无法解析」，于是扫了 24 项也报「目标不存在」。
        if not self.audited:
            if self.scanned:
                return f"未审计（已列出 {self.scanned} 项，缺本地通告库未比对）"
            return "未审计（目标不存在或无法解析）"
        if self.cache_missing:
            return "未审计（本地通告缓存缺失）"
        if self.findings:
            return f"命中 {len(self.findings)} 条"
        return "未命中（对本份本地通告缓存）"

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "scanned": self.scanned,
            "findings": [finding.as_dict() for finding in self.findings],
            "cache_missing": self.cache_missing,
            "audited": self.audited,
            "note": self.note,
            "cache_path": self.cache_path,
            "cache_updated_at": self.cache_updated_at,
            "headline": self.headline(),
            "passed": self.passed(),
        }


# ── 已安装分发包 ────────────────────────────────────────────────────────────


def default_site_packages() -> Path:
    """当前解释器的 `site-packages`。跨平台靠 `sysconfig`，不硬编码 `Lib/site-packages`。"""
    purelib = sysconfig.get_paths().get("purelib")
    if purelib:
        return Path(purelib)
    return Path(sys.prefix) / "Lib" / "site-packages"


def installed_distributions(site_packages: Path) -> dict[str, str]:
    """扫 `*.dist-info/METADATA` 抽出 `name → version`。

    刻意不用 `importlib.metadata.distributions()`：那个只认**当前解释器**的路径，
    而 `--venv PATH` 要能指向另一个 venv。读 METADATA 头部（RFC 822，到第一个空行为止）
    是唯一与路径无关的做法。
    """
    found: dict[str, str] = {}
    root = Path(site_packages)
    if not root.is_dir():
        return found
    for info in sorted(root.glob("*.dist-info")):
        metadata = info / "METADATA"
        if not metadata.is_file():
            continue
        name, version = _metadata_name_version(metadata)
        if name:
            found[name] = version
    return found


def _metadata_name_version(metadata: Path) -> tuple[str, str]:
    """从 METADATA 头部取 Name / Version。读不到就返回空串（不抛：一个坏包不该炸整次审计）。"""
    name = ""
    version = ""
    try:
        text = metadata.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", ""
    for line in text.splitlines():
        if not line.strip():
            break  # 头部结束，后面是长描述，可能又出现 "Name:" 字样
        lowered = line.lower()
        if lowered.startswith("name:") and not name:
            name = line.split(":", 1)[1].strip()
        elif lowered.startswith("version:") and not version:
            version = line.split(":", 1)[1].strip()
        if name and version:
            break
    return name, version


def normalize_name(name: str) -> str:
    """PEP 503 归一化：`Foo_Bar.baz` 与 `foo-bar-baz` 是同一个包。"""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


# ── 通告缓存 ────────────────────────────────────────────────────────────────


def cache_path(data_dir: Path | None) -> Path:
    """通告缓存路径。`data_dir` 为空时退回"当前目录"，便于纯函数式调用。"""
    base = Path(data_dir) if data_dir is not None else Path.cwd()
    return base / CACHE_NAME


def load_cache(path: Path) -> tuple[dict[str, Any] | None, str]:
    """读缓存，返回 `(packages, updated_at)`。文件不存在或坏掉 → `(None, "")`。

    坏掉的缓存**当作缺失**处理：解析失败时假装"没有通告"会让审计悄悄变成空转。
    """
    if not path.is_file():
        return None, ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, ""
    if not isinstance(data, dict):
        return None, ""
    updated = str(data.get("_updated_at", ""))
    packages = {
        str(key): value
        for key, value in data.items()
        if not str(key).startswith("_") and isinstance(value, dict)
    }
    return packages, updated


def _advisories_for(
    cache: Mapping[str, Any], name: str, version: str
) -> list[dict[str, Any]]:
    """在缓存里查一个包的一个版本。`"*"` 版本键表示"所有版本都受影响"。"""
    bucket = cache.get(name)
    if bucket is None:
        # 缓存里可能用别的写法记同一个包（下划线 / 大小写），再试一次归一化名。
        normalized = normalize_name(name)
        for key, value in cache.items():
            if normalize_name(key) == normalized:
                bucket = value
                break
    if not isinstance(bucket, Mapping):
        return []
    out: list[dict[str, Any]] = []
    for version_key in (version, "*"):
        entries = bucket.get(version_key)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            out.append(_as_advisory(entry))
    return out


def _as_advisory(entry: Any) -> dict[str, Any]:
    """把缓存里的一条通告归一成 `{id, summary, severity}`。字符串也接受（手写的缓存）。"""
    if isinstance(entry, str):
        return {"id": entry, "summary": "", "severity": "unknown"}
    if isinstance(entry, Mapping):
        severity = str(entry.get("severity", "")).lower()
        if severity not in SEVERITY_ORDER:
            severity = "unknown"
        return {
            "id": str(entry.get("id", "")),
            "summary": str(entry.get("summary", "")),
            "severity": severity,
        }
    return {"id": "", "summary": "", "severity": "unknown"}


# ── 审计一：venv ────────────────────────────────────────────────────────────


def audit_venv(site_packages: Path, *, data_dir: Path | None = None) -> AuditReport:
    """比对已安装分发包与本地通告缓存。

    **缓存缺失时 `findings` 为空但报告不通过**——见模块 docstring 的第一原则。
    """
    packages = installed_distributions(site_packages)
    report = AuditReport(kind="venv", scanned=len(packages))
    path = cache_path(data_dir)
    report.cache_path = str(path)
    if not Path(site_packages).is_dir():
        report.audited = False
        report.note = f"目录不存在：{site_packages}（没有可扫描的分发包）"
        return report
    cache, updated = load_cache(path)
    if cache is None:
        report.cache_missing = True
        report.note = CACHE_MISSING_NOTE
        return report
    report.cache_updated_at = updated
    for name in sorted(packages):
        version = packages[name]
        for advisory in _advisories_for(cache, name, version):
            identifier = advisory["id"] or "（缓存里没有 ID）"
            summary = advisory["summary"]
            detail = f"{identifier}" + (f"：{summary}" if summary else "")
            report.findings.append(
                Finding(
                    target=f"{name}=={version}",
                    detail=detail,
                    severity=advisory["severity"],
                    advisory=identifier,
                    source="venv",
                )
            )
    return report


# ── 审计二：插件目录 ────────────────────────────────────────────────────────


def audit_plugins(plugins_dir: Path) -> AuditReport:
    """按 `plugin_guard` 的思路扫插件目录：结构限制 + 危险模式 + 硬编码密钥。

    分两档看待同一个文件：**代码文件**看危险模式（`subprocess` / `eval` …），
    **文本文件**看硬编码密钥（README 里贴的 key 一样是泄漏）。这与
    `plugin_guard` 的"code 文件豁免 env 读取类模式、文档文件全量扫"是同一个思路。
    """
    report = AuditReport(kind="plugins")
    root = Path(plugins_dir)
    if not root.is_dir():
        report.audited = False
        report.note = f"插件目录不存在：{root}（没有可扫描的插件）"
        return report

    files = 0
    total_bytes = 0
    for path in sorted(root.rglob("*")):
        if files >= MAX_FILES:
            report.note = f"达到文件上限 {MAX_FILES}，剩下的没扫（结构限制，见 plugin_guard）"
            break
        rel_parts = path.relative_to(root).parts
        if any(part in EXCLUDED_DIRS for part in rel_parts):
            continue
        rel = "/".join(rel_parts)
        if path.is_symlink():
            files += 1
            report.findings.append(
                Finding(
                    target=rel,
                    detail="符号链接（可能指向插件目录之外，需人工确认）",
                    severity="medium",
                    source="plugins",
                )
            )
            continue
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in TEXT_SUFFIXES:
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        if size > MAX_FILE_KB * 1024:
            files += 1
            report.findings.append(
                Finding(
                    target=rel,
                    detail=f"文件 {size // 1024}KB 超过单文件上限 {MAX_FILE_KB}KB，未扫描",
                    severity="low",
                    source="plugins",
                )
            )
            continue
        if total_bytes + size > MAX_TOTAL_KB * 1024:
            report.note = f"达到总大小上限 {MAX_TOTAL_KB}KB，剩下的没扫（结构限制）"
            break
        total_bytes += size
        files += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        report.findings.extend(_scan_plugin_text(rel, text, is_code=suffix in CODE_SUFFIXES))

    report.scanned = files
    return report


def _scan_plugin_text(rel: str, text: str, *, is_code: bool) -> list[Finding]:
    """扫一个文件的内容。返回命中列表（行号写进 `target`，方便直接跳过去）。"""
    findings: list[Finding] = []
    lines = text.splitlines()
    if is_code:
        for pattern, kind, severity, label in PLUGIN_PATTERNS:
            for index, line in enumerate(lines, start=1):
                if pattern.search(line):
                    findings.append(
                        Finding(
                            target=f"{rel}:{index}",
                            detail=f"{label}（规则 {kind}）",
                            severity=severity,
                            source="plugins",
                        )
                    )
    for index, line in enumerate(lines, start=1):
        result = redact(line)
        if not result:
            continue
        kinds = "、".join(result.kinds)
        findings.append(
            Finding(
                target=f"{rel}:{index}",
                detail=f"疑似硬编码密钥（{kinds}）",
                severity="critical",
                source="plugins",
            )
        )
    return findings


# ── 审计三：MCP 配置 ────────────────────────────────────────────────────────


def audit_mcp(config_path: Path) -> AuditReport:
    """检查 MCP 服务配置：明文密钥 + 可疑的 `command`。

    Orbit 的约定是**密钥不进 `mcp.json`**：值写成 `keyring:<名字>`，运行时由
    `MCPManager._resolve()` 从系统 keyring 取（见 `app/mcp.py` 的模块 docstring）。
    所以"值不是 `keyring:` 前缀但看着像密钥"就是一条真命中，不是风格建议。
    """
    report = AuditReport(kind="mcp")
    path = Path(config_path)
    if not path.is_file():
        report.audited = False
        report.note = f"配置文件不存在：{path}（没有 MCP 服务可审计）"
        return report
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        report.audited = False
        report.note = f"配置文件无法解析：{path}（{exc}）——未审计，不是「未发现漏洞」"
        return report

    servers = _mcp_servers(data)
    report.scanned = len(servers)
    for server in servers:
        name = str(server.get("name") or server.get("_name") or "（未命名）")
        report.findings.extend(_scan_mcp_secrets(name, server))
        report.findings.extend(_scan_mcp_command(name, server))
        report.findings.extend(_scan_mcp_url(name, server))
    return report


def _mcp_servers(data: Any) -> list[dict[str, Any]]:
    """兼容两种布局：Orbit 的 `{"servers": [...]}` 与 Claude/Hermes 的 `{"mcpServers": {...}}`。"""
    if not isinstance(data, Mapping):
        return []
    raw = data.get("servers")
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    raw_map = data.get("mcpServers")
    if isinstance(raw_map, Mapping):
        out: list[dict[str, Any]] = []
        for name, item in raw_map.items():
            if isinstance(item, dict):
                merged = dict(item)
                merged.setdefault("name", str(name))
                out.append(merged)
        return out
    return []


def _scan_mcp_secrets(name: str, server: Mapping[str, Any]) -> list[Finding]:
    """`env` / `headers` 里的明文密钥。`keyring:` 前缀是**唯一**的合规写法。"""
    findings: list[Finding] = []
    for field_name in ("env", "headers"):
        values = server.get(field_name)
        if not isinstance(values, Mapping):
            continue
        for key, raw in values.items():
            text = str(raw)
            if text.startswith("keyring:"):
                continue  # 合规：值是指针，不是密钥
            looks_secret = is_secret_name(str(key)) or bool(redact(text))
            if not looks_secret:
                continue
            findings.append(
                Finding(
                    target=f"{name}.{field_name}.{key}",
                    detail=(
                        "明文密钥写在 mcp.json 里（应改成 `keyring:<名字>`，"
                        "由系统 keyring 在运行时解析）"
                    ),
                    severity="critical",
                    source="mcp",
                )
            )
    return findings


def _scan_mcp_command(name: str, server: Mapping[str, Any]) -> list[Finding]:
    """`command` 的可疑可执行路径。"""
    findings: list[Finding] = []
    command = str(server.get("command") or "").strip()
    if not command:
        return findings
    basename = re.split(r"[\\/]", command)[-1].lower()
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename
    reason = SUSPICIOUS_COMMANDS.get(stem) or SUSPICIOUS_COMMANDS.get(basename)
    if reason:
        findings.append(
            Finding(
                target=f"{name}.command",
                detail=f"可疑的可执行文件：{command} —— {reason}",
                severity="high",
                source="mcp",
            )
        )
    lowered = command.lower()
    if any(marker in lowered for marker in TEMP_MARKERS):
        findings.append(
            Finding(
                target=f"{name}.command",
                detail=f"从临时目录启动可执行文件：{command}（典型载荷落盘位置）",
                severity="high",
                source="mcp",
            )
        )
    if ".." in command.replace("\\", "/").split("/"):
        findings.append(
            Finding(
                target=f"{name}.command",
                detail=f"可执行路径含上跳（..）：{command}",
                severity="medium",
                source="mcp",
            )
        )
    return findings


def _scan_mcp_url(name: str, server: Mapping[str, Any]) -> list[Finding]:
    """远程服务 URL 里内嵌的凭据。"""
    findings: list[Finding] = []
    url = str(server.get("url") or "").strip()
    if url and redact(url):
        findings.append(
            Finding(
                target=f"{name}.url",
                detail="URL 里内嵌了凭据（user:password@），应改用 keyring 引用",
                severity="critical",
                source="mcp",
            )
        )
    return findings


# ── 刷新通告缓存 ────────────────────────────────────────────────────────────


def refresh_cache(
    *,
    transport: Any | None = None,
    data_dir: Path | None = None,
    packages: Mapping[str, Sequence[str]] | None = None,
    timeout: float = 10.0,
) -> int:
    """拉 OSV 通告并写 `<data_dir>/osv-cache.json`，返回写入的通告条数。

    - `transport` 是**测试注入点**（`httpx.MockTransport`）。生产不传，走真实网络。
    - 没有 transport 且网络不通 → 抛 :class:`NetworkUnavailable`，
      **绝不**返回 0 假装成功（返回 0 会被上层读成"没有通告"）。
    - `packages` 默认取当前解释器的已安装分发包；测试里传小样本即可。

    为什么用 `querybatch`：一个 venv 上百个包，逐个 `query` 就是上百次请求；
    `querybatch` 一次最多 1000 条，这里按 100 条一批（留余量给 URL/载荷上限）。
    """
    import httpx

    payload_packages = (
        dict(packages)
        if packages is not None
        else _installed_for_query(default_site_packages())
    )
    if not payload_packages:
        raise ValueError("没有可查询的包（packages 为空，且当前环境没有已安装分发包）")

    cache: dict[str, Any] = {}
    written = 0
    items = list(payload_packages.items())
    try:
        with httpx.Client(transport=transport, timeout=timeout) as client:
            for start in range(0, len(items), 100):
                chunk = items[start : start + 100]
                queries = []
                for name, versions in chunk:
                    for version in list(versions) or [""]:
                        query: dict[str, Any] = {
                            "package": {"name": name, "ecosystem": "PyPI"}
                        }
                        if version:
                            query["version"] = version
                        queries.append(query)
                if not queries:
                    continue
                response = client.post(OSV_BATCH_URL, json={"queries": queries})
                response.raise_for_status()
                results = response.json().get("results", [])
                for query, result in zip(queries, results, strict=False):
                    name = str(query["package"]["name"])
                    # 没给版本时用 `*` 记缓存：`_advisories_for` 认这个通配键。
                    version = str(query.get("version", "")) or "*"
                    advisories = [
                        _advisory_from_osv(item)
                        for item in result.get("vulns", []) or []
                        if isinstance(item, Mapping)
                    ]
                    if not advisories:
                        continue
                    cache.setdefault(name, {}).setdefault(version, []).extend(advisories)
                    written += len(advisories)
    except Exception as exc:
        # httpx.HTTPError / OSError / 解析失败都在这里：**统一成"网络不可用"**。
        # 静默返回 0 会让审计层把"没拉到"读成"没漏洞"——那是本模块最不能犯的错。
        raise NetworkUnavailable(
            f"无法从 OSV 拉取通告（{type(exc).__name__}: {exc}）。"
            f"本机没有网络或没有可用的 HTTP transport；"
            f"审计需要先刷新本地通告缓存才能进行。"
        ) from exc

    from datetime import UTC, datetime

    path = cache_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {"_updated_at": datetime.now(UTC).isoformat()}
    payload.update(cache)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return written


def _installed_for_query(site_packages: Path) -> dict[str, list[str]]:
    """把已安装分发包转成 `{name: [version]}`，供 `querybatch` 用。"""
    return {
        name: [version] if version else []
        for name, version in installed_distributions(site_packages).items()
    }


def _advisory_from_osv(vuln: Mapping[str, Any]) -> dict[str, Any]:
    """把 OSV 的一条 `vulns[]` 归一成缓存里的一条通告。"""
    severity = ""
    database_specific = vuln.get("database_specific")
    if isinstance(database_specific, Mapping):
        severity = str(database_specific.get("severity", "")).lower()
    if severity not in SEVERITY_ORDER:
        # OSV 的 `severity[]` 是 CVSS 向量，算分需要额外逻辑；本模块只做"有没有"，
        # 所以没有明确等级时如实记 `unknown`，而不是猜一个。
        severity = "unknown"
    return {
        "id": str(vuln.get("id", "")),
        "summary": str(vuln.get("summary", "") or vuln.get("details", ""))[:200],
        "severity": severity,
    }


def worst(findings: Iterable[Finding]) -> str:
    """一组命中里最严重的那一级（没有命中返回空串）。"""
    best = ""
    best_rank = -1
    for finding in findings:
        if finding.rank > best_rank:
            best, best_rank = finding.severity, finding.rank
    return best
