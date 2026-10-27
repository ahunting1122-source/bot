"""多根技能索引：tier 优先级 + frontmatter 硬校验 + 渐进披露 + 威胁扫描。

对标 Hermes `tools/skills_tool.py`（读侧扫描/缓存/去重）+ `tools/skill_manager_tool.py`
的 `_validate_frontmatter`（写侧硬校验）+ `tools/skills_guard.py` 的威胁模式库。
规格出处：`handoff/hermes-parity/research/04-skill-layer.md`。

## 为什么单独一个模块，而不是塞进 `app/skills.py`

`app/skills.py` 是**一期的最小读侧**：单根（`<data_dir>/skills`）、手写简化 YAML 解析、
不做校验、不缓存。它的调用方（`skill_load` / `skill_list` 工具、`sync_to_store`）依赖
「每次都读磁盘、坏文件返回空技能」这套语义。把多根 + 校验 + 缓存塞进去会同时改掉
这些语义，风险远大于收益。所以：**旧模块一行不动**，新模块独立承载 parity 语义，
由 CLI（`orbit skills …`）与 curator 消费。

## 三个设计决定（都是被坑出来的，不是偏好）

1. **同名 first-wins + 显式 `shadowed` 列表**。Hermes 用 `seen_names` 集合**静默丢弃**
   后来的同名技能（`skills_tool.py:800-804`）。静默覆盖是最难排查的一类问题：
   用户改的是 project 副本，加载的却是 user 副本，现象是「改了没生效」。
   本实现保留 first-wins（行为对齐），但把被压住的来源与路径记进 `shadowed`，
   `orbit skills browse` 直接把它打出来。

2. **硬校验失败 → 该技能不加载并进 `errors`，不抛异常**。一个坏 SKILL.md 不能
   让整棵技能树读不出来——否则用户装了一个坏技能，全部技能一起消失，而报错信息
   指向的是「技能列表为空」，与真正的原因相距十万八千里。

3. **索引只装 `{name, description(≤60), triggers, tier, path}`，正文靠 `load()` 按需读**。
   这是 Hermes 的「渐进披露」三层模型的 Tier 1（`skills_tool.py:9-12`）：
   每轮装配都注入 100+ 条全文 = 几十万 token 的首 token 税。索引里**绝不缓存正文**。

## 缓存签名为什么是「目录 mtime + 条目数」而不是「文件 hash」

NTFS 的目录 mtime 是**惰性更新**的（Hermes `skills_tool.py:134-137` 明确记了这个坑）：
新建文件后目录 mtime 可能不变，所以签名里必须再掺**条目数**兜底。用文件 hash 更准，
但那是每次装配都要读全部文件——正是缓存要避免的开销。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# ── 数值上限（逐条对齐 Hermes 常量，见调研 04 §3.6）────────────────────────────

#: 技能名上限（`skill_manager_tool.py:197 MAX_NAME_LENGTH`）。
MAX_NAME_LENGTH = 64
#: description 硬上限（同文件 198 行 `MAX_DESCRIPTION_LENGTH`）。
MAX_DESCRIPTION_LENGTH = 1024
#: 索引注入预算（`SKILL_PROMPT_DESC_LIMIT`）：超过它在**装配时**截断。
INDEX_DESCRIPTION_LIMIT = 60
#: 截断保留长度：57 + "..." = 60（Hermes 错误文案原文「truncates longer descriptions
#: to 57 chars + '...'」，两处独立证据，见调研 04 §4.2）。
INDEX_DESCRIPTION_KEEP = 57
#: 单个 SKILL.md 正文上限（`MAX_SKILL_CONTENT_CHARS`，注释：约 36k tokens）。
MAX_SKILL_CONTENT_CHARS = 100_000
#: 单文件字节上限（`MAX_SKILL_FILE_BYTES` = 1 MiB）。
MAX_SKILL_FILE_BYTES = 1_048_576
#: 索引缓存 TTL（`_SKILLS_CACHE_TTL_SECONDS = 30.0`）。
INDEX_CACHE_TTL_SECONDS = 30.0
#: 扫描时只读前 4000 字符做 frontmatter 判定（`skills_tool.py:791`）。
FRONTMATTER_SCAN_CHARS = 4000

# ── tier（加载根优先级）──────────────────────────────────────────────────────

TIER_PROJECT = "project"
TIER_USER = "user"
TIER_EXTERNAL = "external"
#: 顺序即优先级：project > user > external（first-wins 靠这个顺序成立）。
TIERS: tuple[str, ...] = (TIER_PROJECT, TIER_USER, TIER_EXTERNAL)
TIER_LABELS = {
    TIER_PROJECT: "项目级（<cwd>/.orbit/skills，需 trust）",
    TIER_USER: "用户级（<data_dir>/skills）",
    TIER_EXTERNAL: "外部级（<data_dir>/skills-external，只读）",
}

#: 不参与扫描的目录。`.archive` 是归档区（Hermes `EXCLUDED_SKILL_DIRS` 的实测效果：
#: 归档技能不出现在提示快照里），`.hub` 是远端安装区。
EXCLUDED_DIR_NAMES = frozenset(
    {".archive", ".hub", ".git", ".venv", "venv", "__pycache__", "node_modules", "index-cache"}
)

# ── frontmatter 白名单（调研 04 §1.3 的 16 个顶层字段，91 个 SKILL.md 实测）────

#: 允许的 16 个顶层键。**未知键不硬拒**（Hermes 也不拒，只是解析器不消费），
#: 但会进 `warnings`——「写了但没人读」的字段是最常见的自欺。
ALLOWED_FRONTMATTER_FIELDS = frozenset(
    {
        "name",  # 91/91 必填
        "description",  # 91/91 必填
        "version",  # 91/91
        "platforms",  # 91/91：linux/macos/windows/darwin，省略=全平台
        "metadata",  # 87/91：agentskills.io 兼容块，约定子键 metadata.hermes.*
        "author",  # 86/91
        "license",  # 85/91
        "prerequisites",  # 16/91：env_vars / commands
        "dependencies",  # 10/91
        "tags",  # 4/91（推荐位置是 metadata.hermes.tags）
        "triggers",  # 2/91：显式触发词
        "required_credential_files",  # 1/91
        "title",  # 1/91
        "environments",  # 1/91：环境门控
        "compatibility",  # 1/91
        "setup",  # 1/91：结构化安装元数据
    }
)

#: `---\n…\n---\n` 的闭合匹配。`(?:\r?\n|$)` 让「只有 frontmatter、没有正文」也能匹配上，
#: 从而由第 11 条校验给出「正文为空」这个**准确**原因，而不是笼统的「没闭合」。
_FRONTMATTER = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)
_BOM = "\ufeff"


class SkillIndexError(RuntimeError):
    """索引层的可预期失败（技能不存在等）。CLI 侧翻成 ``CliError``。"""


# ── frontmatter 校验 ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FrontmatterReport:
    """一次 frontmatter 校验的结果。``ok=False`` 时只有 ``error`` 有意义。"""

    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    error: str = ""
    warnings: tuple[str, ...] = ()


def index_description(text: str, limit: int = INDEX_DESCRIPTION_LIMIT) -> str:
    """把 description 压进索引预算：超长截到 57 字符 + ``...``。

    截断**只发生在索引里**：`load()` 返回的是全文。理由同 Hermes——索引是路由信号，
    每条最多 60 字符；而正文里的细节在 `skill_view` 时才需要。
    """
    flat = " ".join(str(text or "").split())
    if len(flat) <= limit:
        return flat
    keep = max(0, limit - 3)
    return flat[:keep].rstrip() + "..."


def validate_frontmatter(text: str, *, new_skill: bool = False) -> FrontmatterReport:
    """按 Hermes `_validate_frontmatter`（`skill_manager_tool.py:603-657`）的 11 条硬校验。

    逐条顺序与失败原因对齐 Hermes（中文文案 + 英文原文见各分支注释）：

    1. 内容非空
    2. 容忍并剥离 UTF-8 BOM（``content.lstrip("\\ufeff")``，618 行）
    3. 必须以 ``---`` 开头
    4. 必须有闭合的 ``---``
    5. 必须能 ``yaml.safe_load``
    6. 必须是 mapping
    7. 必须有 ``name``（本实现额外判 ≤64，见下）
    8. 必须有 ``description``
    9. ``len(description) ≤ 1024``
    10. **仅新建路径**：``len(description) ≤ 60``
    11. 闭合 ``---`` 之后 body 非空

    ``name`` 长度这一条是**刻意偏离** Hermes 的：Hermes 在扫描侧把超长 name 静默
    截到 64 字符（`skills_tool.py:801`），结果是 `.usage.json` 的键、目录名、
    提示里的名字三者对不上，比直接报错难查一个数量级。所以这里判硬错。

    ``new_skill=True`` 对应 Hermes 的 ``_validate_frontmatter(content, new_skill=True)``：
    新建技能额外受 60 字符预算约束（644-651 行），因为超出的部分在索引里一定会被砍掉。
    """
    raw = text or ""
    # ① 空内容
    if not raw.strip():
        return FrontmatterReport(ok=False, error="技能内容为空（Content cannot be empty）")

    # ② BOM：Windows 上记事本/部分编辑器会写 BOM，不剥就整段 frontmatter 都匹配不上
    if raw.startswith(_BOM):
        raw = raw.lstrip(_BOM)

    # ③ 必须以 --- 开头
    if not raw.startswith("---"):
        return FrontmatterReport(
            ok=False,
            error="SKILL.md 必须以 YAML frontmatter（---）开头；可参考任意现有技能的格式",
        )

    # ④ 闭合的 ---
    match = _FRONTMATTER.match(raw)
    if match is None:
        return FrontmatterReport(
            ok=False,
            error="SKILL.md 的 frontmatter 没有闭合：缺少结束的 '---' 行",
        )

    # ⑤ YAML 可解析
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        return FrontmatterReport(ok=False, error=f"frontmatter YAML 解析失败：{exc}")

    # ⑥ 必须是 mapping
    if not isinstance(data, dict):
        return FrontmatterReport(
            ok=False, error="frontmatter 必须是 YAML 映射（key: value 键值对）"
        )

    meta: dict[str, Any] = {str(key): value for key, value in data.items()}

    # ⑦ name：必填 + 长度
    name = meta.get("name")
    if not isinstance(name, str) or not name.strip():
        return FrontmatterReport(ok=False, error="frontmatter 必须包含 'name' 字段")
    if len(name.strip()) > MAX_NAME_LENGTH:
        return FrontmatterReport(
            ok=False,
            error=f"name 超过 {MAX_NAME_LENGTH} 字符上限（当前 {len(name.strip())}）",
        )

    # ⑧ description：必填
    description = meta.get("description")
    if not isinstance(description, str) or not description.strip():
        return FrontmatterReport(
            ok=False, error="frontmatter 必须包含 'description' 字段"
        )

    # ⑨ description 硬上限
    if len(description) > MAX_DESCRIPTION_LENGTH:
        return FrontmatterReport(
            ok=False,
            error=f"description 超过 {MAX_DESCRIPTION_LENGTH} 字符上限（当前 {len(description)}）",
        )

    # ⑩ 新建路径的 60 字符预算
    if new_skill and len(description) > INDEX_DESCRIPTION_LIMIT:
        return FrontmatterReport(
            ok=False,
            error=(
                f"description 有 {len(description)} 字符——新建技能必须压进 "
                f"{INDEX_DESCRIPTION_LIMIT} 字符的系统提示预算（一句话、触发点在前、以句号结尾）。"
                f"技能索引会把更长的描述截到 {INDEX_DESCRIPTION_KEEP} 字符 + '...'，"
                "路由信号会被截断。细节请写进正文。"
            ),
        )

    # ⑪ body 非空
    body = raw[match.end() :]
    if not body.strip():
        return FrontmatterReport(
            ok=False, error="frontmatter 之后必须有正文（步骤、说明等），不能只有元数据"
        )

    warnings = _frontmatter_warnings(meta)
    return FrontmatterReport(ok=True, data=meta, body=body, warnings=warnings)


def _frontmatter_warnings(meta: dict[str, Any]) -> tuple[str, ...]:
    """白名单外的键 → 告警（不拒）。顺带检查 ``platforms`` 取值。"""
    warnings = [
        f"unknown-field:{key}" for key in sorted(meta) if key not in ALLOWED_FRONTMATTER_FIELDS
    ]
    platforms = meta.get("platforms")
    if platforms is not None:
        if not isinstance(platforms, list):
            warnings.append("platforms-not-a-list")
        else:
            bad = [str(item) for item in platforms if str(item) not in _VALID_PLATFORMS]
            if bad:
                warnings.append("platforms-invalid:" + ",".join(bad))
    return tuple(warnings)


_VALID_PLATFORMS = frozenset({"linux", "macos", "windows", "darwin"})


def check_content_size(text: str) -> str:
    """正文/文件级上限检查。返回错误文案，空串 = 通过。

    与 frontmatter 校验分开：Hermes 里也是两条独立闸（``_validate_frontmatter`` 与
    ``_validate_content_size``）。分开的好处是错误文案能直接指向"文件太大"，
    而不是笼统的"格式不对"。
    """
    if len(text) > MAX_SKILL_CONTENT_CHARS:
        return (
            f"技能正文 {len(text)} 字符，超过 {MAX_SKILL_CONTENT_CHARS} 字符上限"
            f"（约 36k tokens）；请把细节拆到 references/ 里按需加载"
        )
    return ""


# ── 索引条目 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SkillRecord:
    """一条索引条目。**只有装配需要的元数据，没有正文**（渐进披露的 Tier 1）。"""

    name: str
    description: str
    triggers: tuple[str, ...]
    tier: str
    path: Path
    category: str = ""

    def as_index(self) -> dict[str, Any]:
        """``index()`` 对外暴露的形状：5 个键，一个不多。

        多给一个字段就是多一份首 token 税，而且会让「索引里有正文」这类退化
        无声地发生（字段越多，越没人检查里面装了什么）。
        """
        return {
            "name": self.name,
            "description": self.description,
            "triggers": list(self.triggers),
            "tier": self.tier,
            "path": str(self.path),
        }


@dataclass
class IndexResult:
    """一次扫描的完整结果：技能 + 坏文件 + 被压住的同名技能 + 未信任的项目根。"""

    records: list[SkillRecord] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    shadowed: list[dict[str, Any]] = field(default_factory=list)
    untrusted: list[str] = field(default_factory=list)

    @property
    def skills(self) -> list[dict[str, Any]]:
        """索引视图（``{name, description, triggers, tier, path}`` 列表）。"""
        return [record.as_index() for record in self.records]

    def find(self, name: str) -> SkillRecord | None:
        clean = (name or "").strip().removesuffix(".md")
        for record in self.records:
            if record.name == clean:
                return record
        return None

    def by_tier(self) -> dict[str, list[SkillRecord]]:
        """按 tier 分组（顺序照优先级）。`browse` 与 `status` 都用它。"""
        grouped: dict[str, list[SkillRecord]] = {tier: [] for tier in TIERS}
        for record in self.records:
            grouped.setdefault(record.tier, []).append(record)
        return grouped


# ── 威胁模式（skills_guard 的高价值子集）──────────────────────────────────────

#: **这是子集，不是全部。** Hermes `skills_guard.py` 的 `THREAT_PATTERNS`（184-666 行）
#: 实测 107 条 / 9 类别 / 4 severity。本实现覆盖 **30 条**（= `THREAT_PATTERN_COUNT`，
#: 有测试盯着这个数），选取标准是
#: 「一条命中就足以让人停下来看一眼」的高危形态：
#: 提示注入、凭证外传、破坏性命令、反弹 shell、混淆执行、持久化、挖矿。
#: 未覆盖的主要是细粒度变体（同语义的第 2~5 种写法）与低危 `*_ref` 提示。
THREAT_PATTERNS: tuple[tuple[str, str, str, str], ...] = (
    # (pattern_id, severity, category, regex)
    # —— 提示注入（injection）——
    ("prompt_injection_ignore", "critical", "injection",
     r"(?i)ignore\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instruction|prompt|rule|direction)s?"),
    ("prompt_injection_disregard", "critical", "injection",
     r"(?i)disregard\s+(all\s+)?(the\s+)?(previous|prior|above|system)"),
    ("sys_prompt_override", "critical", "injection",
     r"(?i)(reveal|print|show|dump|repeat)\s+(your\s+|the\s+)?(system\s+prompt|initial\s+instruction|hidden\s+rule)"),
    ("role_hijack", "high", "injection",
     r"(?i)\byou\s+are\s+now\s+(a|an|the)\b"),
    ("jailbreak_dan", "critical", "injection",
     r"(?i)\b(dan\s+mode|developer\s+mode\s+enabled|jailbreak(ed)?\s+mode)\b"),
    ("translate_execute", "high", "injection",
     r"(?i)translate\s+the\s+following[\s\S]{0,120}?\b(then\s+)?(run|execute|exec)\b"),
    ("hidden_instruction", "high", "injection",
     r"(?i)<!--\s*(ignore|instructions?\s*:|system\s*:|you\s+are\s+now)"),
    # —— 凭证外传（exfiltration）——
    ("env_exfil_curl", "critical", "exfiltration",
     r"(?i)curl\s+[^\n|;]*(\$\{?[A-Z0-9_]*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*\}?)"),
    ("env_exfil_http_client", "critical", "exfiltration",
     r"(?i)(requests|httpx|urllib)\.[a-z]+\([^\n)]{0,200}(os\.environ|getenv|environ\[)"),
    ("read_secrets_file", "critical", "exfiltration",
     r"(?i)(cat|open|read_text|read_bytes)\s*[^\n]{0,80}(\.ssh/|id_rsa|id_ed25519|\.aws/credentials|/\.env\b)"),
    ("dns_exfil", "critical", "exfiltration",
     r"(?i)(nslookup|dig)\s+[^\n]{0,80}(\$\(|\$\{?[A-Z_]*(KEY|TOKEN|SECRET))"),
    ("md_image_exfil", "critical", "exfiltration",
     r"!\[[^\]]*\]\(\s*https?://[^)\s]*[?&][^)\s]*(key|token|secret|password)="),
    ("md_link_exfil", "high", "exfiltration",
     r"(?i)\[[^\]]*\]\(\s*https?://[^)\s]*[?&](data|payload|leak)="),
    # —— 破坏性（destructive）——
    ("destructive_root_rm", "critical", "destructive",
     r"(?i)\brm\s+-[a-z]*[rf][a-z]*\s+/(\s|$|\*)"),
    ("destructive_home_rm", "critical", "destructive",
     r"(?i)\brm\s+-[a-z]*[rf][a-z]*\s+(~|\$HOME|\$\{HOME\})/?(\s|$)"),
    ("format_filesystem", "critical", "destructive",
     r"(?i)\b(mkfs(\.\w+)?|format\s+[a-z]:)\b"),
    ("overwrite_disk", "critical", "destructive",
     r"(?i)\bdd\s+[^\n]{0,40}of=/dev/(sd|nvme|hd)"),
    # —— 持久化（persistence）——
    ("persistence_cron", "high", "persistence",
     r"(?i)(crontab\s+-[a-z]|/etc/cron\.|/etc/cron\s*$)"),
    ("ssh_backdoor", "critical", "persistence",
     r"(?i)>>?\s*[~$\w/.\-]*\.ssh/authorized_keys"),
    ("sudoers_mod", "critical", "persistence",
     r"(?i)(/etc/sudoers|visudo)"),
    # —— 执行 / 反弹 shell（execution）——
    ("reverse_shell", "critical", "execution",
     r"(?i)(bash\s+-i\s+>&|/dev/tcp/|nc\s+-e\s+/bin/(ba)?sh|ncat\s+-e)"),
    ("curl_pipe_shell", "critical", "execution",
     r"(?i)(curl|wget)\s+[^\n|;]{1,200}\|\s*(sudo\s+)?(sh|bash|zsh|python3?)\b"),
    ("python_os_system", "high", "execution",
     r"(?i)\bos\.system\s*\("),
    ("python_subprocess_shell", "high", "execution",
     r"(?i)subprocess\.[a-z]+\([^\n)]{0,200}shell\s*=\s*True"),
    # —— 混淆（obfuscation）——
    ("base64_decode_pipe", "critical", "obfuscation",
     r"(?i)base64\s+(-d|--decode|-D)\s*\|\s*(sh|bash|zsh|python3?)"),
    ("echo_pipe_exec", "high", "obfuscation",
     r"(?i)echo\s+[^\n|;]{1,200}\|\s*(sudo\s+)?(sh|bash|zsh)\b"),
    ("zero_width_payload", "medium", "obfuscation",
     r"[\u200b\u200c\u200d\u2060\ufeff]{6,}"),
    # —— 其他高危目标 ──
    ("system_passwd_access", "high", "traversal",
     r"(?i)(/etc/passwd|/etc/shadow)\b"),
    ("path_traversal_deep", "medium", "traversal",
     r"(\.\./){4,}|(\.\.\\){4,}"),
    ("crypto_mining", "high", "mining",
     r"(?i)(xmrig|stratum\+tcp://|minerd|cpuminer)"),
)

#: 供 docstring / CLI 显示的条数（写成常量，免得文案与实现漂移）。
THREAT_PATTERN_COUNT = len(THREAT_PATTERNS)

_COMPILED_THREATS: tuple[tuple[str, str, str, re.Pattern[str]], ...] = tuple(
    (pattern_id, severity, category, re.compile(regex, re.DOTALL))
    for pattern_id, severity, category, regex in THREAT_PATTERNS
)

#: severity → 排序权重。`skills check` 按它排，让 critical 永远在最上面。
SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}


@dataclass(frozen=True)
class ThreatFinding:
    """一条威胁命中。带行号与摘录——只说"有风险"而不指出位置的报告没人会修。"""

    pattern_id: str
    severity: str
    category: str
    where: str
    line: int
    excerpt: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "pattern_id": self.pattern_id,
            "severity": self.severity,
            "category": self.category,
            "where": self.where,
            "line": self.line,
            "excerpt": self.excerpt,
        }


def scan_text(text: str, *, where: str = "body") -> list[ThreatFinding]:
    """扫描一段文本，返回命中的威胁（按 severity 排序）。

    ``where`` 只用于报告定位（``body`` / ``description``）。扫描对象是**技能正文 +
    frontmatter description**——description 会被注入系统提示，所以它比正文更危险：
    正文要模型主动 load 才看得到，description 每轮都在。
    """
    findings: list[ThreatFinding] = []
    source = text or ""
    for pattern_id, severity, category, pattern in _COMPILED_THREATS:
        match = pattern.search(source)
        if match is None:
            continue
        start = match.start()
        line = source.count("\n", 0, start) + 1
        excerpt = " ".join(match.group(0).split())[:120]
        findings.append(
            ThreatFinding(
                pattern_id=pattern_id,
                severity=severity,
                category=category,
                where=where,
                line=line,
                excerpt=excerpt,
            )
        )
    findings.sort(key=lambda item: (SEVERITY_RANK.get(item.severity, 9), item.pattern_id))
    return findings


def scan_skill(text: str) -> list[ThreatFinding]:
    """扫描一个完整 SKILL.md：description 与正文分开扫，报告里能区分位置。"""
    report = validate_frontmatter(text)
    findings: list[ThreatFinding] = []
    if report.ok:
        findings.extend(scan_text(str(report.data.get("description") or ""), where="description"))
        findings.extend(scan_text(report.body, where="body"))
    else:
        # 格式坏的文件也扫：坏文件照样会被装进目录，不能因为解析失败就跳过安全扫描
        findings.extend(scan_text(text, where="raw"))
    findings.sort(key=lambda item: (SEVERITY_RANK.get(item.severity, 9), item.pattern_id))
    return findings


# ── 索引本体 ────────────────────────────────────────────────────────────────


class SkillIndex:
    """多根技能索引。一个实例 = 一份 (data_dir, cwd) 的视图。

    三个根（优先级从高到低）：

    - ``project``  ``<cwd>/.orbit/skills``，**必须先 trust**（``orbit skills trust``）
    - ``user``     ``<data_dir>/skills``
    - ``external`` ``<data_dir>/skills-external``，只读（curator 永不归档它）

    为什么 project 要 trust：项目目录里的 SKILL.md 是**别人仓库里的内容**，克隆下来就
    自动注入系统提示 = 一条免审批的提示注入通道。Hermes 因此要求显式 trust
    （``hermes skills trust``），本实现照抄这个闸。未信任时根**不扫描**，并在
    ``IndexResult.untrusted`` 里留痕，CLI 会提示怎么开。
    """

    def __init__(
        self,
        data_dir: Path | str,
        *,
        cwd: Path | str | None = None,
        project_root: Path | str | None = None,
        external_root: Path | str | None = None,
        require_project_trust: bool = True,
        ttl: float = INDEX_CACHE_TTL_SECONDS,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.cwd = Path(cwd) if cwd is not None else Path.cwd()
        self.project_root = (
            Path(project_root) if project_root is not None else self.cwd / ".orbit" / "skills"
        )
        self.user_root = self.data_dir / "skills"
        self.external_root = (
            Path(external_root) if external_root is not None else self.data_dir / "skills-external"
        )
        self.require_project_trust = require_project_trust
        self.ttl = ttl
        #: 签名 → (monotonic 时间戳, 结果)。只留最新一份：无界增长比不缓存更糟。
        self._cache: dict[str, tuple[float, IndexResult]] = {}

    # —— 信任 ——

    @property
    def trust_path(self) -> Path:
        return self.data_dir / "skills-trusted.json"

    def trusted_projects(self) -> list[str]:
        """读信任清单。坏文件当空清单——信任清单读不出来时**宁可少加载**。"""
        try:
            payload = json.loads(self.trust_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if isinstance(payload, dict):
            items = payload.get("trusted") or []
        elif isinstance(payload, list):
            items = payload
        else:
            return []
        return [str(item) for item in items if str(item).strip()]

    def _write_trusted(self, items: list[str]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.trust_path.write_text(
            json.dumps({"version": 1, "trusted": items}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    def trust(self, path: Path | str | None = None) -> str:
        """信任一个项目根（默认 cwd），返回规范化后的路径。幂等。"""
        target = _normalize(path if path is not None else self.cwd)
        items = self.trusted_projects()
        if target not in items:
            items.append(target)
            items.sort()
            self._write_trusted(items)
        self.invalidate()
        return target

    def untrust(self, path: Path | str | None = None) -> bool:
        """撤销信任。返回是否真的删掉了一条。"""
        target = _normalize(path if path is not None else self.cwd)
        items = self.trusted_projects()
        remaining = [item for item in items if item != target]
        changed = len(remaining) != len(items)
        if changed:
            self._write_trusted(remaining)
            self.invalidate()
        return changed

    def project_trusted(self) -> bool:
        """项目根是否已信任：信任清单里的任一条是本项目根的祖先即可。"""
        if not self.require_project_trust:
            return True
        root = Path(_normalize(self.project_root))
        for item in self.trusted_projects():
            trusted = Path(_normalize(item))
            if root == trusted:
                return True
            try:
                root.relative_to(trusted)
            except ValueError:
                continue
            return True
        return False

    # —— 扫描 ——

    def active_roots(self) -> list[tuple[str, Path]]:
        """当前生效的 (tier, 根) 列表，顺序即优先级。不存在的根直接跳过。"""
        roots: list[tuple[str, Path]] = []
        if self.project_root.is_dir() and self.project_trusted():
            roots.append((TIER_PROJECT, self.project_root))
        if self.user_root.is_dir():
            roots.append((TIER_USER, self.user_root))
        if self.external_root.is_dir():
            roots.append((TIER_EXTERNAL, self.external_root))
        return roots

    def _untrusted_roots(self) -> list[str]:
        """有技能但未信任的项目根（给 CLI 提示用）。"""
        if self.require_project_trust and self.project_root.is_dir() and not self.project_trusted():
            return [str(self.project_root)]
        return []

    def _iter_skill_files(self, root: Path) -> list[Path]:
        """枚举某根下的全部 ``SKILL.md``（类目可嵌套，最多不设限，但剪掉排除目录）。"""
        found: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in EXCLUDED_DIR_NAMES and not name.startswith(".")
            )
            if "SKILL.md" in filenames:
                found.append(Path(dirpath) / "SKILL.md")
        return found

    def _read_entry(
        self, path: Path, root: Path, tier: str
    ) -> tuple[SkillRecord | None, dict[str, Any] | None]:
        """读一个 SKILL.md 并做硬校验。返回 ``(记录, 错误)``，两者必有一为 None。"""
        try:
            size = path.stat().st_size
        except OSError as exc:
            return None, {"tier": tier, "path": str(path), "name": path.parent.name,
                          "error": f"无法读取文件：{exc}"}
        if size > MAX_SKILL_FILE_BYTES:
            return None, {"tier": tier, "path": str(path), "name": path.parent.name,
                          "error": f"文件 {size} 字节，超过 {MAX_SKILL_FILE_BYTES} 字节上限"}
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            return None, {"tier": tier, "path": str(path), "name": path.parent.name,
                          "error": f"无法按 UTF-8 读取：{exc}"}

        report = validate_frontmatter(text)
        if not report.ok:
            return None, {"tier": tier, "path": str(path), "name": path.parent.name,
                          "error": report.error}
        size_error = check_content_size(text)
        if size_error:
            return None, {"tier": tier, "path": str(path), "name": path.parent.name,
                          "error": size_error}

        name = str(report.data.get("name")).strip()[:MAX_NAME_LENGTH]
        description = index_description(str(report.data.get("description") or ""))
        return (
            SkillRecord(
                name=name,
                description=description,
                triggers=_parse_triggers(report.data),
                tier=tier,
                path=path,
                category=_category_of(path, root),
            ),
            None,
        )

    def _scan_all(self) -> IndexResult:
        """按 tier 顺序扫全部根，同名 first-wins，被压住的进 ``shadowed``。"""
        result = IndexResult(untrusted=self._untrusted_roots())
        seen: dict[str, SkillRecord] = {}
        for tier, root in self.active_roots():
            for path in self._iter_skill_files(root):
                record, error = self._read_entry(path, root, tier)
                if error is not None:
                    result.errors.append(error)
                    continue
                assert record is not None  # _read_entry 的契约：两者必有一为 None
                winner = seen.get(record.name)
                if winner is not None:
                    # 静默覆盖是最难排查的一类问题，所以这里必须留痕
                    result.shadowed.append(
                        {
                            "name": record.name,
                            "tier": tier,
                            "path": str(record.path),
                            "shadowed_by_tier": winner.tier,
                            "shadowed_by_path": str(winner.path),
                        }
                    )
                    continue
                seen[record.name] = record
                result.records.append(record)
        return result

    def _signature(self) -> str:
        """目录元数据摘要：每个目录的 mtime_ns + 条目数 + 路径。

        不读文件内容（那是缓存要省掉的开销）；掺条目数是为了兜 NTFS 目录 mtime
        惰性更新——只看 mtime 会漏掉"新建/删除技能后目录 mtime 没变"。
        """
        digest = hashlib.sha1()
        for tier, root in self.active_roots():
            digest.update(f"{tier}:{root}".encode())
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = sorted(
                    name
                    for name in dirnames
                    if name not in EXCLUDED_DIR_NAMES and not name.startswith(".")
                )
                try:
                    stat = os.stat(dirpath)
                except OSError:
                    continue
                digest.update(
                    f"{dirpath}|{stat.st_mtime_ns}|{len(filenames)}|{len(dirnames)}".encode()
                )
        return digest.hexdigest()

    # —— 对外 ——

    def index(self) -> IndexResult:
        """取索引（带 TTL 缓存）。TTL 内同一签名直接复用，不重扫。"""
        signature = self._signature()
        now = time.monotonic()
        cached = self._cache.get(signature)
        if cached is not None and (now - cached[0]) < self.ttl:
            return cached[1]
        result = self._scan_all()
        self._cache = {signature: (now, result)}
        return result

    def invalidate(self) -> None:
        """手动失效缓存。写完技能/改完 trust 之后必须调——否则要等 30 秒。"""
        self._cache.clear()

    def load(self, name: str) -> dict[str, Any]:
        """按需读全文（渐进披露 Tier 2）。索引里没有正文，正文只在这里出现。"""
        record = self.index().find(name)
        if record is None:
            raise SkillIndexError(f"技能不存在：{name}")
        try:
            text = record.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SkillIndexError(f"技能文件读不出来：{record.path}（{exc}）") from exc
        report = validate_frontmatter(text)
        return {
            "name": record.name,
            "tier": record.tier,
            "path": str(record.path),
            "description": str(report.data.get("description") or ""),
            "triggers": list(record.triggers),
            "frontmatter": report.data,
            "body": report.body if report.ok else text,
            "ok": report.ok,
            "error": report.error,
            "warnings": list(report.warnings),
        }

    def check(self, name: str | None = None) -> list[dict[str, Any]]:
        """跑 frontmatter 校验 + 威胁扫描，返回逐技能报告（``skills check`` 用）。

        与 `index()` 的区别：这里**要读全文**（安全扫描必须看正文），所以它天然
        不受 30 秒缓存保护，是明确的"体检"动作而不是每轮装配路径。
        """
        result = self.index()
        records = result.records
        if name:
            record = result.find(name)
            if record is None:
                raise SkillIndexError(f"技能不存在：{name}")
            records = [record]
        reports: list[dict[str, Any]] = []
        for record in records:
            try:
                text = record.path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                reports.append(
                    {
                        "name": record.name,
                        "tier": record.tier,
                        "path": str(record.path),
                        "ok": False,
                        "error": f"无法读取：{exc}",
                        "warnings": [],
                        "findings": [],
                    }
                )
                continue
            report = validate_frontmatter(text)
            findings = scan_skill(text)
            reports.append(
                {
                    "name": record.name,
                    "tier": record.tier,
                    "path": str(record.path),
                    "ok": report.ok,
                    "error": report.error,
                    "warnings": list(report.warnings),
                    "findings": [finding.as_dict() for finding in findings],
                }
            )
        return reports

    # —— 快照（对齐 `.skills_prompt_snapshot.json` v2）——

    def snapshot(self) -> dict[str, Any]:
        """导出索引快照：``version / manifest / skills / category_descriptions``。

        manifest 用 ``mtime_ns + size`` 做指纹（不是 hash）——它是**廉价的增量判据**：
        比对两次快照就能知道哪些文件变了，而不必读内容。
        """
        result = self.index()
        manifest: dict[str, str] = {}
        for tier, root in self.active_roots():
            for path in self._iter_skill_files(root):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                try:
                    key = str(path.relative_to(root))
                except ValueError:
                    key = str(path)
                manifest[f"{tier}:{key}"] = f"{stat.st_mtime_ns} {stat.st_size}"
        categories: dict[str, str] = {}
        for record in result.records:
            if record.category:
                categories.setdefault(record.category, "")
        return {
            "version": 2,
            "manifest": manifest,
            "skills": result.skills,
            "category_descriptions": categories,
            "errors": result.errors,
            "shadowed": result.shadowed,
        }

    def diff_snapshot(self, payload: dict[str, Any]) -> dict[str, list[str]]:
        """把一份快照与本机现状对账：新增 / 缺失 / 变化。

        ``skills snapshot import`` 用它。本库没有 hub 安装通道（无网络、无注册表），
        所以 import 只能**对账**而不能"照快照把技能装回来"——这一点必须说清楚，
        假装能装回去比明说不能更糟。
        """
        incoming = {
            str(item.get("name")): item
            for item in payload.get("skills") or []
            if isinstance(item, dict) and item.get("name")
        }
        current = {record.name: record for record in self.index().records}
        added = sorted(set(incoming) - set(current))
        removed = sorted(set(current) - set(incoming))
        changed = sorted(
            name
            for name in set(incoming) & set(current)
            if str(incoming[name].get("description") or "")
            != current[name].description
        )
        return {"added": added, "removed": removed, "changed": changed}


def _parse_triggers(meta: dict[str, Any]) -> tuple[str, ...]:
    """触发词可能写在三个位置：``triggers`` / 顶层 ``tags`` / ``metadata.hermes.tags``。

    Hermes 里这三处并存（`_parse_tags` 兼容 ``[a, b]`` / ``a, b`` / 已解析列表），
    推荐位置是 ``metadata.hermes.tags``。索引要给出触发词就得三处都认。
    """
    for candidate in (
        meta.get("triggers"),
        meta.get("tags"),
        (meta.get("metadata") or {}).get("hermes", {}).get("tags")
        if isinstance(meta.get("metadata"), dict)
        else None,
    ):
        items = _coerce_list(candidate)
        if items:
            return items
    return ()


def _coerce_list(value: Any) -> tuple[str, ...]:
    """把 ``[a, b]`` / ``a, b`` / 单个字符串统一成字符串元组。"""
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        return tuple(part.strip() for part in text.split(",") if part.strip())
    return ()


def _category_of(path: Path, root: Path) -> str:
    """类目 = 相对根路径的第一段（``mlops/evaluation/x/SKILL.md`` → ``mlops``）。

    与 Hermes `_get_category_from_path` 同口径：二级子类不进 category。
    直接放在根下的技能没有类目，返回空串。
    """
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return ""
    return parts[0] if len(parts) >= 3 else ""


def _normalize(path: Path | str) -> str:
    """路径规范化：绝对 + 大小写不敏感（Windows）。信任清单靠它比对。"""
    return os.path.normcase(os.path.abspath(str(path)))


def build_index(data_dir: Path | str, *, cwd: Path | str | None = None) -> SkillIndex:
    """CLI 用的便捷构造：数据目录 + 当前工作目录。"""
    return SkillIndex(data_dir, cwd=cwd)


__all__ = [
    "ALLOWED_FRONTMATTER_FIELDS",
    "EXCLUDED_DIR_NAMES",
    "FRONTMATTER_SCAN_CHARS",
    "INDEX_CACHE_TTL_SECONDS",
    "INDEX_DESCRIPTION_KEEP",
    "INDEX_DESCRIPTION_LIMIT",
    "MAX_DESCRIPTION_LENGTH",
    "MAX_NAME_LENGTH",
    "MAX_SKILL_CONTENT_CHARS",
    "MAX_SKILL_FILE_BYTES",
    "SEVERITY_RANK",
    "TIERS",
    "TIER_EXTERNAL",
    "TIER_LABELS",
    "TIER_PROJECT",
    "TIER_USER",
    "THREAT_PATTERNS",
    "THREAT_PATTERN_COUNT",
    "FrontmatterReport",
    "IndexResult",
    "SkillIndex",
    "SkillIndexError",
    "SkillRecord",
    "ThreatFinding",
    "build_index",
    "check_content_size",
    "index_description",
    "scan_skill",
    "scan_text",
    "validate_frontmatter",
]
