"""技能 linter：12 条 advisory 规则 + Python 代码块 AST 审计 + staging 审批门。

对标 Hermes `tools/skill_linter.py`（12 条规则 / 其中 2 条 ERROR）、
`tools/skills_ast_audit.py`（AST 深度诊断）、`tools/write_approval.py` 的 skills
staging 通道（`<home>/pending/skills/<id>.json`）。规格出处：
`handoff/hermes-parity/research/04-skill-layer.md` §3.2 / §3.7 / §3.8。

## 为什么 linter 不硬拒

硬拒在 `app/skill_index.py::validate_frontmatter`（11 条硬校验，写侧的前置闸）。
linter 是它的**软伴生**：Hermes 的定位原文是 advisory（`_attach_lint_findings()`
「仅 advisory，永不阻断」）。理由很实在——「描述里写了 powerful」不该让一个技能
装不进来，但**不告诉作者**就等于这条约定只存在于 reviewer 的脑子里。所以：
linter 出报告，写侧只把报告附给模型，放不放行由硬校验决定。

## 为什么规则是「数据」而不是一串 if

`RULES` 是 12 个 `(code, severity, 检查函数, 中文说明)` 的元组。做成数据的好处：
① CLI 能一次把「本机跑的是哪 12 条、各自什么级别」打出来，文案与实现不会漂移；
② 测试可以逐条命中（`RULES` 的长度与 code 都被断言），加规则时不会悄悄少一条；
③ 顺序即报告顺序，人读时的稳定感是免费的。

## AST 审计的两个刻意选择

1. **只看正文里的 fenced Python 代码块**，不扫磁盘上的 `scripts/`。技能正文是
   **模型会照着抄**的东西；`scripts/` 里的脚本是给人跑的，扫描它们的收益远小于
   「读盘 + 遍历」的成本（Hermes 的 `ast_scan_path` 属于 `skills audit --deep`
   的 opt-in 诊断，不在安装路径上）。
2. **解析失败报 lint 问题，不抛异常**。语法错误的代码块恰恰是最该被指出来的：
   它说明作者从没跑过这段示例。若在这里抛 `SyntaxError`，一条坏示例会让整份技能
   的 lint 结果全丢——包括那 11 条本来能报出来的问题。

## staging：模型写出来的技能不该直接生效

Hermes 的原文理由（`write_approval.py` docstring）：「skill writes always stage
(too big to eyeball mid-loop)」——记忆可以内联弹窗审批，100 KB 的 SKILL.md 不行。
本实现照抄这个闸：`stage()` 只往 `<data_dir>/skills-staging/<id>/` 写，
`approve()` 才搬进正式目录 `<data_dir>/skills/<name>/`，`reject()` 直接删。

staging id 是**内容寻址**的（`sha256(name\\ntext)[:12]`），与 curator ledger 同一
立场：同样的内容必然得到同一个 id，于是重复 stage 幂等、审批记录可复现，也不会
出现「同一个技能在待审列表里出现三份」。
"""

from __future__ import annotations

import ast
import hashlib
import json
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.skill_index import (
    ALLOWED_FRONTMATTER_FIELDS,
    INDEX_DESCRIPTION_LIMIT,
    MAX_NAME_LENGTH,
    MAX_SKILL_CONTENT_CHARS,
    parse_triggers,
    validate_frontmatter,
)

# ── 级别 ────────────────────────────────────────────────────────────────────

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
#: 排序权重：ERROR 永远在最上面（报告的第一行应该是要动手的那条）。
SEVERITY_RANK: dict[str, int] = {SEVERITY_ERROR: 0, SEVERITY_WARNING: 1}

# ── 阈值（全部落在常量上，CLI 文案与测试都从这里取）────────────────────────

#: 触发词条数上限。超过它说明这个技能想覆盖一切场景——路由信号会被摊薄。
MAX_TRIGGERS = 8
#: 触发词最短长度（按字符）。一个字的触发词几乎必然误触发。
MIN_TRIGGER_LENGTH = 2
#: URL 报告条数上限：一份技能里 20 个外链时，逐条报会把报告淹掉。
MAX_URL_FINDINGS = 5
#: `scope` 的合法取值（与 `app/skills.py::save_skill` 同口径）。
VALID_SCOPES: tuple[str, ...] = ("global", "project", "site")
#: Orbit 自己的两个 frontmatter 字段。它们**不在** Hermes 的 16 字段白名单里，但
#: `app/skills.py::save_skill` 每一份技能都会写 `scope` 与 `status`。若 lint 把它们
#: 报成「未知字段」，等于对本库自己写的每一份技能都刷一条噪音——一条永远为真的
#: 告警会让其余告警一起被无视。所以白名单在这里**按实现扩容**，而不是按调研缩减。
ORBIT_EXTRA_FIELDS: frozenset[str] = frozenset({"scope", "status"})
#: lint 认可的前置字段集合。
LINT_ALLOWED_FIELDS: frozenset[str] = ALLOWED_FRONTMATTER_FIELDS | ORBIT_EXTRA_FIELDS

# ── 规则 code（常量而不是字面量：RULES 与检查函数共用，避免两处漂移）────────

CODE_NAME_FORMAT = "name-format"
CODE_SCOPE_DIR = "scope-dir-mismatch"
CODE_DESCRIPTION = "description-length"
CODE_TRIGGERS = "triggers-count"
CODE_BODY_LENGTH = "body-too-long"
CODE_DANGEROUS = "dangerous-command"
CODE_URL = "untrusted-url"
CODE_PLACEHOLDER = "placeholder"
CODE_UNKNOWN_FIELD = "unknown-field"
CODE_HEADING = "missing-heading"
CODE_DUPLICATE_TRIGGERS = "duplicate-triggers"
CODE_ENCODING = "encoding"
#: 下面三个 code **不属于那 12 条规则**：它们是前置判定与 AST 审计的产物。
#: 单列出来是因为「frontmatter 读不出来」与「代码块语法错」在报告里的位置不同
#: （前者替代整份规则集，后者是额外加的一节）。
CODE_FRONTMATTER = "frontmatter-invalid"
CODE_AST_CALL = "ast-forbidden-call"
CODE_AST_SYNTAX = "ast-syntax-error"


# ── 数据结构 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Finding:
    """一条 lint 结果。

    ``line`` 是**原文**行号（1 起，0 表示「全文级，没有具体位置」）。只报「有风险」
    而不指出位置，等于把定位工作退回给读者——这是最容易让报告被无视的写法。
    """

    code: str
    severity: str
    message: str
    line: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "line": self.line,
        }

    def format(self) -> str:
        badge = "✗" if self.severity == SEVERITY_ERROR else "⚠"
        where = f" (第 {self.line} 行)" if self.line else ""
        return f"{badge} [{self.code}] {self.message}{where}"


@dataclass(frozen=True)
class LintInput:
    """一条规则能看到的东西。**纯数据，不含磁盘句柄**——规则因此可以单测。

    ``path`` 是可选的实际落点（``SKILL.md`` 的路径）：只有「scope 与目录层级不符」
    这条需要它。没有 path 时该规则静默跳过，而不是猜一个目录——猜错的方向是把
    正确的技能报成错的，那种误报比漏报更伤信任。
    """

    name: str
    text: str
    frontmatter: dict[str, Any]
    body: str
    path: Path | None = None


CheckFn = Callable[[LintInput], "list[Finding]"]


@dataclass(frozen=True)
class Rule:
    """一条规则：``(code, severity, 检查函数, 中文说明)``。

    说明写成一句「要做什么」而不是「哪里错了」：报告的读者是模型的下一轮，
    它需要的是动作，不是判决。
    """

    code: str
    severity: str
    check: CheckFn
    summary: str


# ── 小工具 ──────────────────────────────────────────────────────────────────

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]*")
_URL_RE = re.compile(r"https?://[^\s)\]\"'<>`]+", re.IGNORECASE)
_HEADING_RE = re.compile(r"^#{1,6}[ \t]+\S", re.MULTILINE)
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\u2060\ufeff]{2,}")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_REPLACEMENT_RE = re.compile("\ufffd")

#: 可信任的外链主机（含子域）。**白名单短是有意的**：这份清单的作用不是「判断网站
#: 好坏」，而是「这条链接会不会让模型去拉一份没人审过的指令」——只有官方文档站与
#: 代码托管能算「作者与读者都能核对的公开出处」，其余一律让人看一眼。
TRUSTED_HOSTS: frozenset[str] = frozenset(
    {
        "github.com",
        "raw.githubusercontent.com",
        "gist.github.com",
        "docs.python.org",
        "python.org",
        "pypi.org",
        "developer.mozilla.org",
        "learn.microsoft.com",
        "modelcontextprotocol.io",
        "agentskills.io",
        "localhost",
        "127.0.0.1",
    }
)

#: 危险命令示例。扫**整份正文（含代码块）**——这条规则的对象正是「示例」，
#: 剥掉代码块就等于把要查的东西筛掉了。
_DANGEROUS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        # 结尾用 `(?:\s|$|\*)` 而不是 `\b`：`rm -rf /` 里 `/` 是非单词字符，
        # `\b` 在它后面永远不成立——这条正则会静默一条都不命中（踩过）。
        re.compile(r"\brm\s+-[a-z]*[rf][a-z]*\s+(?:/|~|\$HOME)(?:\s|$|\*)"),
        "rm -rf 删除根目录或家目录",
    ),
    (re.compile(r"(?i)\bdel\s+/[fsq]\b[^\n]{0,40}/[sq]\b"), "Windows del /f /s /q 递归删除"),
    (
        re.compile(r"(?i)\bRemove-Item\b[^\n]{0,80}-Recurse[^\n]{0,40}-Force"),
        "PowerShell 递归强制删除",
    ),
    (re.compile(r"(?i)\b(mkfs(\.\w+)?|format\s+[a-z]:)"), "格式化磁盘"),
    (re.compile(r"(?i)\bdd\s+[^\n]{0,40}of=/dev/"), "dd 直写块设备"),
    (
        re.compile(r"(?i)(curl|wget)\s+[^\n|;]{1,200}\|\s*(sudo\s+)?(sh|bash|zsh)\b"),
        "把下载内容直接交给 shell 执行",
    ),
    (re.compile(r"(?i)\bchmod\s+-R\s+777\b"), "chmod -R 777 放开全部权限"),
    (re.compile(r"(?i)\b(DROP|TRUNCATE)\s+(TABLE|DATABASE)\b"), "无备份的破坏性 SQL"),
    (re.compile(r"(?i)\bgit\s+push\s+--force\b"), "git push --force 覆盖远端历史"),
)

#: 占位符。尖括号那一档只认**明确的占位词**：`<data_dir>` / `<name>` 这类是正常的
#: 文档写法，把它们报成占位符会让这条规则变成噪音源（一次误报就够毁掉一条规则）。
_PLACEHOLDER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bTODO\b"), "TODO"),
    (re.compile(r"\bFIXME\b"), "FIXME"),
    (re.compile(r"\bTBD\b"), "TBD"),
    (re.compile(r"待补充|待填写|此处省略|占位符"), "中文占位符"),
    (
        re.compile(r"(?i)<(placeholder|insert[^>]*|your[-_ ][^>]*|todo|tbd)>"),
        "尖括号占位符",
    ),
)


def _line_of(text: str, index: int) -> int:
    """字符下标 → 1 起行号。报告里的行号是给人跳转用的，必须能对上原文。"""
    return text.count("\n", 0, max(0, index)) + 1


def _clean_name(raw: str) -> str:
    """技能名去掉 ``.md`` 后缀与首尾空白（``skills inspect foo.md`` 同口径）。"""
    return (raw or "").strip().removesuffix(".md")


def _expected_scope(path: Path | None) -> str:
    """按落点推断「这个技能应该声明哪个 scope」。推不出来返回空串（= 不判）。

    约定来自 `app/skill_index.py` 的三根：项目根是 ``<cwd>/.orbit/skills``（还有
    Hermes 的 ``.agents/skills``），用户根是 ``<data_dir>/skills``。
    """
    if path is None:
        return ""
    parts = [part.lower() for part in Path(path).parts]
    if ".orbit" in parts or ".agents" in parts:
        return "project"
    if "skills" in parts:
        return "global"
    return ""


# ── 12 条规则 ───────────────────────────────────────────────────────────────


def _check_name_format(context: LintInput) -> list[Finding]:
    """① 名字不合规（ERROR）。"""
    name = _clean_name(str(context.frontmatter.get("name") or context.name or ""))
    if not name:
        # 缺失由 frontmatter-invalid 报；lint 不重复喊同一件事
        return []
    if len(name) > MAX_NAME_LENGTH:
        return [
            Finding(
                CODE_NAME_FORMAT,
                SEVERITY_ERROR,
                f"技能名 {len(name)} 字符，超过 {MAX_NAME_LENGTH} 上限",
                1,
            )
        ]
    if not _NAME_RE.fullmatch(name):
        return [
            Finding(
                CODE_NAME_FORMAT,
                SEVERITY_ERROR,
                f"技能名 {name!r} 不合规：只允许小写字母、数字、连字符、下划线，"
                "且不能以符号开头（大写会让 .usage.json 的键、目录名、提示里的名字"
                "三者对不上）",
                1,
            )
        ]
    return []


def _check_scope_matches_dir(context: LintInput) -> list[Finding]:
    """② scope 与目录层级不符（ERROR）。"""
    raw = context.frontmatter.get("scope")
    if raw is None:
        return []
    scope = str(raw).strip()
    if scope not in VALID_SCOPES:
        return [
            Finding(
                CODE_SCOPE_DIR,
                SEVERITY_ERROR,
                f"scope 取值 {scope!r} 不在 {list(VALID_SCOPES)} 里",
                1,
            )
        ]
    if scope == "site":
        # 没有与 site 对应的目录约定，硬判只会误报
        return []
    expected = _expected_scope(context.path)
    if not expected or scope == expected:
        return []
    return [
        Finding(
            CODE_SCOPE_DIR,
            SEVERITY_ERROR,
            f"技能落在 {context.path} 却声明 scope: {scope}（该位置约定是 "
            f"{expected}）；scope 决定装配时的 tier，写错会让它出现在错误的优先级上",
            1,
        )
    ]


def _check_description(context: LintInput) -> list[Finding]:
    """③ 描述缺失或过长（WARNING）。"""
    description = str(context.frontmatter.get("description") or "").strip()
    if not description:
        return [
            Finding(
                CODE_DESCRIPTION,
                SEVERITY_WARNING,
                "缺 description：索引只装名字 + 一句话描述，没有它这个技能不会被路由到",
                1,
            )
        ]
    if len(description) <= INDEX_DESCRIPTION_LIMIT:
        return []
    return [
        Finding(
            CODE_DESCRIPTION,
            SEVERITY_WARNING,
            f"description {len(description)} 字符，超过索引预算 "
            f"{INDEX_DESCRIPTION_LIMIT}：更长的部分在装配时被截到 57 字符 + '...'，"
            "路由信号被砍掉；细节请写进正文",
            1,
        )
    ]


def _check_triggers(context: LintInput) -> list[Finding]:
    """④ 触发词过多或过短（WARNING）。"""
    triggers = parse_triggers(context.frontmatter)
    findings: list[Finding] = []
    if len(triggers) > MAX_TRIGGERS:
        findings.append(
            Finding(
                CODE_TRIGGERS,
                SEVERITY_WARNING,
                f"触发词 {len(triggers)} 条，超过 {MAX_TRIGGERS}：覆盖太宽会让它在"
                "无关任务上被拉起来；留最具体的几条",
                1,
            )
        )
    short = [item for item in triggers if len(item.strip()) < MIN_TRIGGER_LENGTH]
    if short:
        findings.append(
            Finding(
                CODE_TRIGGERS,
                SEVERITY_WARNING,
                f"触发词过短（< {MIN_TRIGGER_LENGTH} 字符）：{short}——一个字几乎"
                "必然误触发",
                1,
            )
        )
    return findings


def _check_body_length(context: LintInput) -> list[Finding]:
    """⑤ 正文过长（WARNING）。"""
    size = len(context.text)
    if size <= MAX_SKILL_CONTENT_CHARS:
        return []
    return [
        Finding(
            CODE_BODY_LENGTH,
            SEVERITY_WARNING,
            f"正文 {size} 字符，超过 {MAX_SKILL_CONTENT_CHARS} 上限（约 36k tokens）："
            "把细节拆到 references/ 里按需加载",
            0,
        )
    ]


def _check_dangerous_commands(context: LintInput) -> list[Finding]:
    """⑥ 正文含危险命令示例（WARNING）。"""
    findings: list[Finding] = []
    seen: set[str] = set()
    for pattern, label in _DANGEROUS_PATTERNS:
        match = pattern.search(context.text)
        if match is None or label in seen:
            continue
        seen.add(label)
        findings.append(
            Finding(
                CODE_DANGEROUS,
                SEVERITY_WARNING,
                f"正文出现危险命令示例（{label}）：模型会照着抄，改成参数化写法或"
                "明确标注「仅演示，勿直接执行」",
                _line_of(context.text, match.start()),
            )
        )
    return findings


def _check_untrusted_urls(context: LintInput) -> list[Finding]:
    """⑦ 含不可信 URL（WARNING）。"""
    hosts: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in _URL_RE.finditer(context.text):
        url = match.group(0).rstrip(".,;)")
        host = _host_of(url)
        if not host or host in seen:
            continue
        if host in TRUSTED_HOSTS or any(
            host.endswith("." + trusted) for trusted in TRUSTED_HOSTS
        ):
            continue
        seen.add(host)
        hosts.append((host, url))
        if len(hosts) >= MAX_URL_FINDINGS:
            break
    if not hosts:
        return []
    listed = "、".join(host for host, _url in hosts)
    return [
        Finding(
            CODE_URL,
            SEVERITY_WARNING,
            f"正文引用了 {len(hosts)} 个不在白名单里的外部地址（{listed}）：技能正文"
            "里的链接会被模型当作指令来源，非官方站点的内容没人审过",
            0,
        )
    ]


def _host_of(url: str) -> str:
    """从 URL 取主机名（小写）。解析失败返回空串——坏 URL 不是这条规则的活。"""
    without_scheme = url.split("://", 1)[-1]
    host = without_scheme.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.rsplit("@", 1)[-1]  # 去掉 user:pass@
    if ":" in host:
        host = host.split(":", 1)[0]
    return host.lower()


def _check_placeholders(context: LintInput) -> list[Finding]:
    """⑧ 含 TODO / 占位符（WARNING）。"""
    findings: list[Finding] = []
    for pattern, label in _PLACEHOLDER_PATTERNS:
        match = pattern.search(context.text)
        if match is None:
            continue
        findings.append(
            Finding(
                CODE_PLACEHOLDER,
                SEVERITY_WARNING,
                f"正文残留{label}：没写完的步骤会被当成完整流程执行，"
                "删掉或补成可执行的具体步骤",
                _line_of(context.text, match.start()),
            )
        )
    return findings


def _check_unknown_fields(context: LintInput) -> list[Finding]:
    """⑨ frontmatter 有未知字段（WARNING）。"""
    unknown = sorted(key for key in context.frontmatter if key not in LINT_ALLOWED_FIELDS)
    if not unknown:
        return []
    return [
        Finding(
            CODE_UNKNOWN_FIELD,
            SEVERITY_WARNING,
            f"frontmatter 有 {len(unknown)} 个不在白名单里的键：{unknown}"
            "（写了但没人读的字段最容易让人误以为生效了）",
            1,
        )
    ]


def _check_missing_heading(context: LintInput) -> list[Finding]:
    """⑩ 正文缺标题（WARNING）。"""
    if _HEADING_RE.search(context.body):
        return []
    return [
        Finding(
            CODE_HEADING,
            SEVERITY_WARNING,
            "正文没有任何 Markdown 标题：分节是渐进披露的锚点，"
            "整段平铺的正文在装配时无法按需定位",
            0,
        )
    ]


def _check_duplicate_triggers(context: LintInput) -> list[Finding]:
    """⑪ 重复触发词（WARNING）。"""
    triggers = parse_triggers(context.frontmatter)
    seen: set[str] = set()
    duplicates: list[str] = []
    for item in triggers:
        key = item.strip().lower()
        if not key:
            continue
        if key in seen and key not in duplicates:
            duplicates.append(key)
        seen.add(key)
    if not duplicates:
        return []
    return [
        Finding(
            CODE_DUPLICATE_TRIGGERS,
            SEVERITY_WARNING,
            f"触发词重复：{duplicates}——重复不会加权，只会让「有几条触发词」这个"
            "判断失真",
            1,
        )
    ]


def _check_encoding(context: LintInput) -> list[Finding]:
    """⑫ 编码问题（WARNING）。"""
    findings: list[Finding] = []
    if context.text.startswith("\ufeff"):
        findings.append(
            Finding(
                CODE_ENCODING,
                SEVERITY_WARNING,
                "文件以 UTF-8 BOM 开头：部分解析器会把 BOM 当成 frontmatter 的一部分，"
                "整段元数据都读不出来",
                1,
            )
        )
    for pattern, label in (
        (_ZERO_WIDTH_RE, "零宽字符（可用来藏不可见指令）"),
        (_CONTROL_RE, "控制字符"),
        (_REPLACEMENT_RE, "U+FFFD 替换字符（说明某处不是合法 UTF-8）"),
    ):
        match = pattern.search(context.text)
        if match is None:
            continue
        findings.append(
            Finding(
                CODE_ENCODING,
                SEVERITY_WARNING,
                f"正文含{label}：保存为纯 UTF-8（无 BOM）后重写这一段",
                _line_of(context.text, match.start()),
            )
        )
    return findings


#: **12 条规则，顺序即报告顺序。** 2 条 ERROR（名字 / scope 与目录），
#: 10 条 WARNING——与 Hermes 的「12 条 / 2 条 ERROR」同构。
RULES: tuple[Rule, ...] = (
    Rule(CODE_NAME_FORMAT, SEVERITY_ERROR, _check_name_format, "技能名必须合规且 ≤64 字符"),
    Rule(CODE_SCOPE_DIR, SEVERITY_ERROR, _check_scope_matches_dir, "scope 必须与目录层级一致"),
    Rule(CODE_DESCRIPTION, SEVERITY_WARNING, _check_description, "description 必须有且 ≤60 字符"),
    Rule(CODE_TRIGGERS, SEVERITY_WARNING, _check_triggers, f"触发词 ≤{MAX_TRIGGERS} 条且不短于 2 字符"),
    Rule(CODE_BODY_LENGTH, SEVERITY_WARNING, _check_body_length, "正文 ≤100,000 字符"),
    Rule(CODE_DANGEROUS, SEVERITY_WARNING, _check_dangerous_commands, "正文不得含危险命令示例"),
    Rule(CODE_URL, SEVERITY_WARNING, _check_untrusted_urls, "外链必须来自可信任主机"),
    Rule(CODE_PLACEHOLDER, SEVERITY_WARNING, _check_placeholders, "正文不得残留 TODO/占位符"),
    Rule(CODE_UNKNOWN_FIELD, SEVERITY_WARNING, _check_unknown_fields, "frontmatter 不得有未知字段"),
    Rule(CODE_HEADING, SEVERITY_WARNING, _check_missing_heading, "正文必须有 Markdown 标题"),
    Rule(CODE_DUPLICATE_TRIGGERS, SEVERITY_WARNING, _check_duplicate_triggers, "触发词不得重复"),
    Rule(CODE_ENCODING, SEVERITY_WARNING, _check_encoding, "文件必须是干净的 UTF-8（无 BOM/零宽/控制符）"),
)

#: 规则条数（写成常量：测试与 CLI 文案都断言它，防止加规则时漏改文档）。
RULE_COUNT = len(RULES)


# ── AST 审计 ────────────────────────────────────────────────────────────────

_PYTHON_FENCE = re.compile(
    r"^```(?P<lang>[A-Za-z0-9_+.-]*)[ \t]*\r?\n(?P<code>.*?)^```[ \t]*$",
    re.DOTALL | re.MULTILINE,
)
#: 只认这些 fence 语言标记。**没有标记的代码块跳过**：把一段 shell/JSON 当 Python
#: 解析必然得到假语法错误，而假错误会让整条 AST 审计失去可信度。
PYTHON_LANGS: frozenset[str] = frozenset({"python", "python3", "py"})
#: 直接执行的入口。
FORBIDDEN_NAMES: frozenset[str] = frozenset({"eval", "exec"})
#: `subprocess` 的常用入口（任何一个都足以执行任意命令）。
SUBPROCESS_FUNCS: frozenset[str] = frozenset(
    {"run", "call", "Popen", "check_call", "check_output"}
)


def _forbidden_call(node: ast.AST) -> str | None:
    """命中返回调用名（如 ``os.system``），否则 None。"""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if isinstance(func, ast.Name) and func.id in FORBIDDEN_NAMES:
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        owner = func.value.id
        if owner == "os" and func.attr == "system":
            return "os.system"
        if owner == "subprocess" and func.attr in SUBPROCESS_FUNCS:
            return f"subprocess.{func.attr}"
    return None


def audit_python_blocks(body: str) -> list[Finding]:
    """扫正文里的 Python 代码块：禁用的调用 + 语法错误。

    语法错误**报成 lint 问题而不是抛异常**：一条写坏的示例恰恰是最该被指出来的
    东西，而且抛异常会让其余 12 条规则的结果一起消失。
    """
    findings: list[Finding] = []
    source = body or ""
    for match in _PYTHON_FENCE.finditer(source):
        if (match.group("lang") or "").lower() not in PYTHON_LANGS:
            continue
        code = match.group("code")
        start = _line_of(source, match.start("code"))
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            offset = max(0, (exc.lineno or 1) - 1)
            findings.append(
                Finding(
                    CODE_AST_SYNTAX,
                    SEVERITY_WARNING,
                    f"Python 代码块有语法错误，AST 审计已跳过这段：{exc.msg}",
                    start + offset,
                )
            )
            continue
        for node in ast.walk(tree):
            label = _forbidden_call(node)
            if label is None:
                continue
            findings.append(
                Finding(
                    CODE_AST_CALL,
                    SEVERITY_WARNING,
                    f"Python 代码块里调用了 {label}：示例会被模型照抄执行，"
                    "改成显式参数化调用，或标注「仅演示」",
                    start + max(0, getattr(node, "lineno", 1) - 1),
                )
            )
    return findings


# ── 入口 ────────────────────────────────────────────────────────────────────


def lint(name: str, text: str, *, path: Path | str | None = None) -> list[Finding]:
    """对一份 SKILL.md 全文跑 12 条规则 + AST 审计。**纯函数，不碰磁盘。**

    ``path`` 只用于「scope 与目录层级不符」这一条；不给就跳过它。

    frontmatter 读不出来时先报一条 ``frontmatter-invalid``（ERROR），再对**原文**
    跑剩下的规则——坏文件也需要体检报告，尤其是它为什么坏。
    """
    source = text or ""
    findings: list[Finding] = []
    report = validate_frontmatter(source)
    location = Path(path) if path is not None else None
    if report.ok:
        context = LintInput(
            name=name,
            text=source,
            frontmatter=dict(report.data),
            body=report.body,
            path=location,
        )
    else:
        findings.append(
            Finding(CODE_FRONTMATTER, SEVERITY_ERROR, report.error or "frontmatter 不合法", 1)
        )
        context = LintInput(name=name, text=source, frontmatter={}, body=source, path=location)
    for rule in RULES:
        findings.extend(rule.check(context))
    findings.extend(audit_python_blocks(context.body))
    return findings


def lint_file(path: Path | str, *, name: str | None = None) -> list[Finding]:
    """lint 一个磁盘上的 SKILL.md。

    读文件用 ``errors="replace"``：编码坏了也要出报告（第 12 条规则就是为它准备的）。
    用严格 UTF-8 读会在 `UnicodeDecodeError` 上直接中断，于是「这个文件是坏编码」
    这条最有用的结论反而报不出来。
    """
    target = Path(path)
    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise SkillLintError(f"读不出技能文件：{target}（{exc}）") from exc
    text = raw.decode("utf-8", errors="replace")
    return lint(name or target.parent.name, text, path=target)


def has_errors(findings: list[Finding]) -> bool:
    """是否有 ERROR 级结果（CLI 用它决定退出码）。"""
    return any(finding.severity == SEVERITY_ERROR for finding in findings)


def sort_findings(findings: list[Finding]) -> list[Finding]:
    """按严重程度稳定排序（同级别保持规则顺序）。"""
    return sorted(findings, key=lambda item: SEVERITY_RANK.get(item.severity, 9))


def format_findings(findings: list[Finding]) -> str:
    """人读的多行报告（CLI 与测试共用一种排版）。"""
    return "\n".join(finding.format() for finding in sort_findings(findings))


# ── staging 审批门 ──────────────────────────────────────────────────────────

#: 待审区目录名（与 `<data_dir>/skills` 平级，因此不会被索引扫到）。
STAGING_DIRNAME = "skills-staging"
#: 每个待审技能自己的元数据文件名。
STAGING_META_FILENAME = "staged.json"
#: 待审技能正文的文件名（与正式目录同构，审批时可直接对比）。
STAGING_SKILL_FILENAME = "SKILL.md"


class SkillLintError(RuntimeError):
    """lint / staging 的可预期失败（内容过不了硬校验、staging id 不存在等）。"""


@dataclass(frozen=True)
class StagedSkill:
    """一条待审记录。"""

    staging_id: str
    name: str
    path: Path
    staged_at: str
    note: str = ""
    frontmatter_ok: bool = True
    frontmatter_error: str = ""
    findings: tuple[Finding, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "staging_id": self.staging_id,
            "name": self.name,
            "path": str(self.path),
            "staged_at": self.staged_at,
            "note": self.note,
            "frontmatter_ok": self.frontmatter_ok,
            "frontmatter_error": self.frontmatter_error,
            "findings": [finding.as_dict() for finding in self.findings],
        }


def _read_json(path: Path) -> dict[str, Any]:
    """读 JSON 对象。坏文件当空 dict——待审区的元数据坏了不该让审批列表读不出来。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


class SkillStaging:
    """staging 审批门。一个实例 = 一份 ``data_dir`` 的待审区视图。"""

    def __init__(
        self,
        data_dir: Path | str,
        *,
        install_root: Path | str | None = None,
        now: Any = None,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.install_root = (
            Path(install_root) if install_root is not None else self.data_dir / "skills"
        )
        self._now = now

    def now(self) -> datetime:
        """当前时间。测试注入 ``now`` 才能断言 ``staged_at``（不靠真实时钟）。"""
        return self._now() if self._now is not None else datetime.now(UTC)

    @property
    def root(self) -> Path:
        return self.data_dir / STAGING_DIRNAME

    def stage(self, name: str, text: str, *, note: str = "") -> str:
        """把一份新技能写进待审区，返回 ``staging_id``。**不碰正式目录。**"""
        clean = _clean_name(name)
        if not clean:
            raise SkillLintError("staging 需要一个技能名（会用作正式目录名）")
        source = text or ""
        if not source.strip():
            raise SkillLintError("staging 的内容为空")
        report = validate_frontmatter(source)
        findings = lint(clean, source)
        staging_id = hashlib.sha256(f"{clean}\n{source}".encode()).hexdigest()[:12]
        target = self.root / staging_id
        target.mkdir(parents=True, exist_ok=True)
        (target / STAGING_SKILL_FILENAME).write_text(source, encoding="utf-8")
        meta_path = target / STAGING_META_FILENAME
        staged_at = self.now().isoformat()
        previous = _read_json(meta_path)
        if previous.get("staged_at"):
            # 幂等：同内容重复 stage 保留首次时间戳，否则待审列表的排序会随
            # 「又跑了一次 stage」而无意义地跳动
            staged_at = str(previous["staged_at"])
        meta = {
            "staging_id": staging_id,
            "name": clean,
            "staged_at": staged_at,
            "note": note,
            "frontmatter_ok": report.ok,
            "frontmatter_error": report.error,
            "findings": [finding.as_dict() for finding in findings],
        }
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return staging_id

    def list(self) -> list[StagedSkill]:
        """列出待审技能（按 ``staged_at`` 升序）。"""
        if not self.root.is_dir():
            return []
        staged: list[StagedSkill] = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir():
                continue
            meta = _read_json(entry / STAGING_META_FILENAME)
            if not meta:
                continue
            staged.append(_staged_from(entry, meta))
        staged.sort(key=lambda item: (item.staged_at, item.staging_id))
        return staged

    def get(self, staging_id: str) -> StagedSkill:
        """按 id 取一条待审记录（不存在则抛 ``SkillLintError``）。"""
        clean = (staging_id or "").strip()
        for staged in self.list():
            if staged.staging_id == clean:
                return staged
        raise SkillLintError(f"待审区里没有这个 id：{clean or '（空）'}（先 `orbit skills stage` 看列表）")

    def approve(self, staging_id: str) -> dict[str, Any]:
        """审批通过：搬进正式目录。

        放行条件（两条都过才搬）：
        ① frontmatter 11 条硬校验通过——否则搬进去也不会被加载；
        ② lint 零 ERROR——WARNING 只是提示，不拦（与 Hermes 的 advisory 定位一致）。
        """
        staged = self.get(staging_id)
        text = (staged.path / STAGING_SKILL_FILENAME).read_text(encoding="utf-8")
        report = validate_frontmatter(text)
        if not report.ok:
            raise SkillLintError(f"审批被拒（{staged.staging_id}）：{report.error}")
        name = _clean_name(str(report.data.get("name") or staged.name))
        findings = lint(name, text)
        errors = [finding for finding in findings if finding.severity == SEVERITY_ERROR]
        if errors:
            detail = "；".join(f"[{item.code}] {item.message}" for item in errors)
            raise SkillLintError(
                f"审批被拒（{staged.staging_id}）：lint 有 {len(errors)} 条 ERROR — {detail}"
            )
        target = self.install_root / name
        if target.exists():
            raise SkillLintError(
                f"正式目录里已有同名技能：{target}（先 `orbit curator archive {name}` "
                "或手工处理，覆盖不是审批门的默认动作）"
            )
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(staged.path / STAGING_SKILL_FILENAME, target / STAGING_SKILL_FILENAME)
        shutil.rmtree(staged.path, ignore_errors=True)
        return {
            "staging_id": staged.staging_id,
            "name": name,
            "path": str(target / STAGING_SKILL_FILENAME),
            "findings": [finding.as_dict() for finding in findings],
        }

    def reject(self, staging_id: str) -> bool:
        """驳回：删掉待审记录。返回是否真的删掉了一条。"""
        staged = self.get(staging_id)
        shutil.rmtree(staged.path, ignore_errors=True)
        return not staged.path.exists()


def _staged_from(entry: Path, meta: dict[str, Any]) -> StagedSkill:
    """把一份 ``staged.json`` 还原成 :class:`StagedSkill`（坏字段退默认）。"""
    raw_findings = meta.get("findings") or []
    findings: list[Finding] = []
    for item in raw_findings:
        if not isinstance(item, dict):
            continue
        findings.append(
            Finding(
                code=str(item.get("code") or ""),
                severity=str(item.get("severity") or SEVERITY_WARNING),
                message=str(item.get("message") or ""),
                line=int(item.get("line") or 0),
            )
        )
    return StagedSkill(
        staging_id=str(meta.get("staging_id") or entry.name),
        name=str(meta.get("name") or entry.name),
        path=entry,
        staged_at=str(meta.get("staged_at") or ""),
        note=str(meta.get("note") or ""),
        frontmatter_ok=bool(meta.get("frontmatter_ok", True)),
        frontmatter_error=str(meta.get("frontmatter_error") or ""),
        findings=tuple(findings),
    )


#: 模块级便捷函数（CLI 与测试都只需要「给我 data_dir，其余照默认」）。
def stage(data_dir: Path | str, name: str, text: str, *, note: str = "") -> str:
    """``SkillStaging(data_dir).stage(name, text)`` 的简写。"""
    return SkillStaging(data_dir).stage(name, text, note=note)


def approve(data_dir: Path | str, staging_id: str) -> dict[str, Any]:
    """``SkillStaging(data_dir).approve(staging_id)`` 的简写。"""
    return SkillStaging(data_dir).approve(staging_id)


def reject(data_dir: Path | str, staging_id: str) -> bool:
    """``SkillStaging(data_dir).reject(staging_id)`` 的简写。"""
    return SkillStaging(data_dir).reject(staging_id)


__all__ = [
    "CODE_AST_CALL",
    "CODE_AST_SYNTAX",
    "CODE_BODY_LENGTH",
    "CODE_DANGEROUS",
    "CODE_DESCRIPTION",
    "CODE_DUPLICATE_TRIGGERS",
    "CODE_ENCODING",
    "CODE_FRONTMATTER",
    "CODE_HEADING",
    "CODE_NAME_FORMAT",
    "CODE_PLACEHOLDER",
    "CODE_SCOPE_DIR",
    "CODE_TRIGGERS",
    "CODE_UNKNOWN_FIELD",
    "CODE_URL",
    "FORBIDDEN_NAMES",
    "LINT_ALLOWED_FIELDS",
    "MAX_TRIGGERS",
    "MAX_URL_FINDINGS",
    "MIN_TRIGGER_LENGTH",
    "ORBIT_EXTRA_FIELDS",
    "PYTHON_LANGS",
    "RULES",
    "RULE_COUNT",
    "SEVERITY_ERROR",
    "SEVERITY_RANK",
    "SEVERITY_WARNING",
    "STAGING_DIRNAME",
    "STAGING_META_FILENAME",
    "STAGING_SKILL_FILENAME",
    "TRUSTED_HOSTS",
    "VALID_SCOPES",
    "Finding",
    "LintInput",
    "Rule",
    "SkillLintError",
    "SkillStaging",
    "StagedSkill",
    "approve",
    "audit_python_blocks",
    "format_findings",
    "has_errors",
    "lint",
    "lint_file",
    "reject",
    "sort_findings",
    "stage",
]
