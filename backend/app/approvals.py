"""命令执行的两级危险裁决：**硬地板**（无条件拦）+ **危险模式**（按审批模式处置）。

为什么要单独一个模块，而不是继续堆在 `tools.py` 的 `IRREVERSIBLE_COMMANDS` 里：

1. **它需要被三处共用**：`run_command` 的执行前裁决、`orbit approvals test` 的离线
   预演、`orbit approvals suggest` 的历史挖掘。写在 `tools.py` 里就只能被工具层用。
2. **两级语义不同**。硬地板是"机器会坏且不可恢复"，任何权限档、任何审批模式都不放行；
   危险模式是"可能不可逆但常见"，取决于审批模式与白名单。混在一个列表里，就没法
   回答"这条为什么被拦"。
3. **需要给裁决结果留痕**（命中的规则名 + 归一化命令），否则模型换个写法就能绕，
   而人看不出绕的是哪一条。

## 与 Hermes 的关系（照抄的与刻意不同的）

- **照抄**：两级分层、`hardline` 无条件拦、危险模式命中走审批、`approvals test` 的
  三态退出码（0 allow / 2 ask / 3 deny）、`approvals suggest` 只提议"非破坏类"。
- **刻意不同**：Hermes 在 YOLO（`approvals.mode=off`）下仍保留硬地板；Orbit 的
  「完全访问权限」档在产品上是**明确不设限**的（`README` 与
  `tests/test_file_scope.py::test_machine_scope_ignores_the_guard_entirely` 都固定了这一点）。
  本模块把"完全访问档是否也守硬地板"做成显式开关 `approvals.hardline_in_machine`，
  **默认 `False` 以尊重既有决策**，不擅自反转产品承诺。要对齐 Hermes 就把这个开关打开。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

#: 审批模式。
#:   off    —— 只拦硬地板，危险模式放行（Orbit 既有行为；Hermes 的 yolo 同义）
#:   smart  —— 硬地板拦；危险模式只有在**白名单**里才放行，否则拒绝并说明怎么加白
#:   manual —— 硬地板拦；危险模式一律拒绝（Orbit 当前没有交互式审批通道，
#:             所以 manual 的语义是"拒绝并让人自己去做"，不是"弹窗问一句"）
APPROVAL_MODES: tuple[str, ...] = ("off", "smart", "manual")

#: 硬地板：命中即拒绝，**不看权限档、不看白名单、不看审批模式**。
#: 判据是"不可恢复"——格式化、裸写块设备、删盘根、擦卷影、改启动项、fork 炸弹。
#: 每一项都是 `(正则, 规则名, 中文说明)`。
HARDLINE_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"\bmkfs(\.\w+)?\b", "mkfs", "格式化文件系统"),
    (r"\bformat\s+[a-z]:", "format_drive", "格式化磁盘分区"),
    (r"\bdd\b[^\n]*\bof=\s*/dev/(sd|hd|nvme|disk|mmcblk)", "dd_block_device", "裸写块设备"),
    (r">\s*/dev/(sd|hd|nvme|disk|mmcblk)", "redirect_block_device", "重定向覆盖块设备"),
    (
        r"\brm\s+-[a-z]*[rf][a-z]*\s+(?:/\*?|~\*?|\$home\*?|\$\{home\}\*?)(?:\s|$)",
        "rm_root",
        "递归删除根目录 / 主目录",
    ),
    (r"\b(rd|rmdir)\s+/s\b[^\n]*[a-z]:\\\s*($|[&|])", "rd_drive_root", "递归删除盘根目录"),
    (
        r"\b(del|erase)\s+/[a-z]*[fsq][a-z]*\s+[a-z]:\\\s*($|[&|])",
        "del_drive_root",
        "批量删除盘根目录",
    ),
    (
        r"remove-item[^\n]*-recurse[^\n]*-force[^\n]*[a-z]:\\\s*($|[;|])",
        "rm_drive_root",
        "递归强删盘根目录",
    ),
    (r"\bvssadmin\s+delete\s+shadows\b", "delete_shadows", "删除卷影副本（系统还原点）"),
    (r"\bbcdedit\b", "bcdedit", "改写启动配置"),
    (r"\bcipher\s+/w\b", "cipher_w", "擦除磁盘空闲空间"),
    (r"\breg\s+delete\s+hklm\b", "reg_delete_hklm", "删除系统注册表项"),
    # `diskpart` 整条进硬地板（沿用 Orbit 原有的判据）：它最常见的用法就是
    # `clean` / `create partition`，而"只是想列一下磁盘"有更安全的替代命令。
    # 宁可让人换条命令，也不要为了少一次误拦把清盘的口子留着。
    (r"\bdiskpart\b", "diskpart", "磁盘分区操作（可能清空磁盘）"),
    (r"\bclear-disk\b", "clear_disk", "Clear-Disk 清空磁盘"),
    (r":\s*\(\s*\)\s*\{", "fork_bomb", "fork 炸弹"),
    (r"\bkill\s+-1\b|\bkillall5\b", "kill_all", "杀死全部进程"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "power_off", "关机 / 重启"),
    (r"\binit\s+[06]\b|\btelinit\s+[06]\b|\bsystemctl\s+poweroff\b", "init_power", "切换运行级别到关机"),
)

#: 危险模式：可能不可逆但常见，处置取决于审批模式与白名单。
#: 第三列是**破坏类**标记——`approvals suggest` 永远不会提议给破坏类加白名单。
DANGEROUS_PATTERNS: tuple[tuple[str, str, str, bool], ...] = (
    # ── 递归删除 / 覆盖（非根路径）────────────────────────────────────────
    (r"\brm\s+-[a-z]*[rf]", "rm_recursive", "递归删除", True),
    (r"\b(del|erase)\s+/[a-z]*[fsq]", "del_forced", "强制删除", True),
    (r"\b(rd|rmdir)\s+/s\b", "rd_recursive", "递归删目录", True),
    (r"remove-item[^\n]*-recurse", "ps_remove_recursive", "递归删目录（PowerShell）", True),
    (r"\bgit\s+clean\s+-[a-z]*[fdx]", "git_clean", "丢弃未跟踪文件", True),
    (r"\bgit\s+reset\s+--hard\b", "git_reset_hard", "丢弃工作区改动", True),
    (r"\bgit\s+push\b[^\n]*--force", "git_push_force", "强推远端分支", True),
    (r"\bgit\s+checkout\s+--\s+\.", "git_checkout_discard", "丢弃工作区改动", True),
    # ── 数据库 ────────────────────────────────────────────────────────────
    (r"\bdrop\s+(database|schema|table)\b", "drop_database", "删库 / 删表", True),
    (r"\btruncate\s+table\b", "truncate_table", "清空表", True),
    (r"\bdelete\s+from\b[^\n]*\bwithout\s+where\b", "delete_all_rows", "无条件删全表", True),
    # ── 权限 / 账号 / 计划任务 ────────────────────────────────────────────
    (r"\bicacls\b[^\n]*/reset\b", "icacls_reset", "重置文件 ACL", True),
    (r"\btakeown\s+/f\b", "takeown", "夺取文件所有权", True),
    (r"\bnet\s+user\b[^\n]*/add\b", "net_user_add", "创建本地账号", True),
    (r"\bnet\s+localgroup\b[^\n]*/add\b", "net_group_add", "提权到管理员组", True),
    (r"\bschtasks\s+/create\b", "schtasks_create", "创建计划任务", True),
    (r"\breg\s+add\b", "reg_add", "写注册表", True),
    (r"\bsc\s+delete\b", "sc_delete", "删除系统服务", True),
    (r"\bsystemctl\s+(stop|disable|mask)\b", "systemctl_stop", "停用系统服务", True),
    (r"\bcrontab\s+-r\b", "crontab_remove", "清空定时任务", True),
    # ── 下载即执行 / 编码绕过 ─────────────────────────────────────────────
    (r"\b(curl|wget)\b[^\n]*\|\s*(ba|z|da)?sh\b", "pipe_to_shell", "下载内容直接喂给 shell", True),
    (r"\b(iwr|invoke-webrequest|irm|invoke-restmethod)\b[^\n]*\|\s*(iex|invoke-expression)\b",
     "iwr_pipe_iex", "下载内容直接执行（PowerShell）", True),
    (r"\binvoke-expression\b|\biex\b", "invoke_expression", "执行动态字符串", True),
    (r"-encodedcommand\b|-enc\s+[a-z0-9+/=]{16,}", "encoded_command", "Base64 编码命令（常见免杀手法）", True),
    # ── 存储 / 容器 / 云 ──────────────────────────────────────────────────
    (r"\bdocker\s+system\s+prune\b", "docker_prune", "清理全部未使用镜像/卷", True),
    (r"\bdocker\s+(rm|rmi)\b[^\n]*-f", "docker_force_remove", "强制删除容器/镜像", True),
    (r"\bkubectl\s+delete\b", "kubectl_delete", "删除集群资源", True),
    (r"\bterraform\s+destroy\b", "terraform_destroy", "销毁基础设施", True),
    (r"\baws\s+s3\s+rm\b[^\n]*--recursive", "s3_recursive_delete", "递归删 S3 对象", True),
    (r"\bshred\b|\bsdelete\b", "secure_erase", "安全擦除文件", True),
    # ── 进程 / 提权 ───────────────────────────────────────────────────────
    (r"\btaskkill\b[^\n]*/f\b", "taskkill_force", "强制结束进程", False),
    (r"\bpkill\b|\bkillall\b", "pkill", "按名杀进程", False),
    (r"^\s*sudo\b|\bsudo\s+", "sudo", "提权执行", True),
    # ── 自终止保护（Hermes 也保留这一条）─────────────────────────────────
    (r"\bpkill\b[^\n]*\b(hermes|orbit)\b", "self_kill", "杀掉 agent 自身进程", True),
    (r"\b(hermes|orbit)\s+(gateway\s+)?(stop|update|uninstall)\b", "self_modify", "停掉或改写 agent 自身", True),
    # ── 其它常见不可逆 ────────────────────────────────────────────────────
    (r"\bchmod\s+-r\s+777\b|\bchown\s+-r\b", "chmod_recursive", "递归改权限/属主", True),
    (r"\bwmic\b[^\n]*\bdelete\b", "wmic_delete", "WMIC 删除对象", True),
    (r"\battrib\b[^\n]*\s-[rsh]\b", "attrib_strip", "去掉只读/系统/隐藏属性", False),
)

#: 裁决三态。退出码照 Hermes：0 allow / 2 ask / 3 deny。
VERDICT_ALLOW, VERDICT_ASK, VERDICT_DENY = "allow", "ask", "deny"
EXIT_CODES: dict[str, int] = {VERDICT_ALLOW: 0, VERDICT_ASK: 2, VERDICT_DENY: 3}

#: 归一化时用来识别"可替换参数"的模式。目的是让 `rm -rf /tmp/a` 与 `rm -rf /tmp/b`
#: 归到同一个模式上——`approvals suggest` 靠它做计数。
_PATHISH = re.compile(r"^(?:[a-z]:[\\/]|/|\./|\.\./|~|[a-z]:\\\\)", re.IGNORECASE)
_NUMBERISH = re.compile(r"^-?\d+(\.\d+)?$")
_QUOTED = re.compile(r"""^(['"]).*\1$""")


@dataclass(frozen=True)
class Verdict:
    """一条命令的裁决结果。`rule` 是命中的规则名，没命中时为空串。"""

    verdict: str
    rule: str = ""
    label: str = ""
    normalized: str = ""
    destructive: bool = False
    reason: str = ""

    @property
    def exit_code(self) -> int:
        return EXIT_CODES.get(self.verdict, 1)

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "rule": self.rule,
            "label": self.label,
            "normalized": self.normalized,
            "destructive": self.destructive,
            "reason": self.reason,
        }


def normalize(command: str) -> str:
    """把命令归一化成"模式"：路径、数字、带引号的字符串都换成占位符。

    只做**保守**替换：认不出来的一律原样保留。宁可把两条本该合并的命令分开计数，
    也不要把两条不同的命令错并成一条——那会让白名单比用户以为的宽。
    """
    tokens = (command or "").split()
    out: list[str] = []
    for token in tokens:
        if _QUOTED.match(token):
            out.append("<str>")
        elif _NUMBERISH.match(token):
            out.append("<n>")
        elif _PATHISH.match(token) or ("\\" in token and len(token) > 2):
            out.append("<path>")
        else:
            out.append(token.casefold())
    return " ".join(out)


def _first_match(
    patterns: tuple, text: str
) -> tuple[str, str, bool] | None:
    for entry in patterns:
        pattern, rule, label = entry[0], entry[1], entry[2]
        destructive = entry[3] if len(entry) > 3 else True
        if re.search(pattern, text):
            return rule, label, destructive
    return None


def classify(
    command: str,
    *,
    mode: str = "off",
    allowlist: tuple[str, ...] | tuple[tuple[str, str], ...] = (),
    env_type: str = "local",
    hardline_in_machine: bool = False,
    machine_wide: bool = False,
) -> Verdict:
    """离线裁决一条命令。**不执行、不提示、不落盘。**

    参数说明（这几个开关是刻意显式传的，不读全局配置——`approvals test` 要能在
    "假如我换成另一个模式"的前提下预演）：

    - `mode`：`off` / `smart` / `manual`
    - `allowlist`：白名单。元素可以是规则名，也可以是归一化命令串。
    - `env_type`：非 `local` 的后端（容器/沙箱）**跳过全部守卫**——容器里格式化
      的是容器自己的盘，拦它没有意义，只会挡住正常构建。
    - `hardline_in_machine`：完全访问档是否也守硬地板（见模块 docstring 的取舍说明）。
    """
    normalized = normalize(command)
    text = (command or "").casefold()

    if env_type != "local":
        return Verdict(
            VERDICT_ALLOW,
            normalized=normalized,
            reason=f"后端类型 {env_type} 不是本机，跳过全部守卫",
        )

    hardline = _first_match(HARDLINE_PATTERNS, text)
    if hardline is not None:
        rule, label, _ = hardline
        if machine_wide and not hardline_in_machine:
            # 完全访问档默认不设限（Orbit 既有产品决策）。仍然如实报出来，
            # 让人知道"这条在窄档会被拦"——静默放行比拒绝更难排查。
            return Verdict(
                VERDICT_ALLOW,
                rule=rule,
                label=label,
                normalized=normalized,
                destructive=True,
                reason=(
                    f"命中硬地板「{label}」，但当前是完全访问权限档且未开启 "
                    "approvals.hardline_in_machine，按既有决策放行"
                ),
            )
        return Verdict(
            VERDICT_DENY,
            rule=rule,
            label=label,
            normalized=normalized,
            destructive=True,
            reason=f"硬地板规则 {rule}：{label}（不可恢复，任何模式都不放行）",
        )

    dangerous = _first_match(DANGEROUS_PATTERNS, text)
    if dangerous is None:
        return Verdict(VERDICT_ALLOW, normalized=normalized, reason="未命中任何危险模式")

    rule, label, destructive = dangerous
    normalized_allow = {item.casefold() for item in allowlist}
    if rule.casefold() in normalized_allow or normalized.casefold() in normalized_allow:
        return Verdict(
            VERDICT_ALLOW,
            rule=rule,
            label=label,
            normalized=normalized,
            destructive=destructive,
            reason=f"命中危险模式 {rule}，但已在白名单里",
        )

    if mode == "off":
        return Verdict(
            VERDICT_ALLOW,
            rule=rule,
            label=label,
            normalized=normalized,
            destructive=destructive,
            reason=f"命中危险模式 {rule}（{label}），审批模式 off 放行（已记录）",
        )
    return Verdict(
        VERDICT_ASK,
        rule=rule,
        label=label,
        normalized=normalized,
        destructive=destructive,
        reason=(
            f"命中危险模式 {rule}：{label}。"
            f"审批模式 {mode} 下需要人工处置（Orbit 没有交互式审批通道，"
            f"请自行执行，或用 `orbit approvals allow add {rule}` 加白）"
        ),
    )


# ── 策略解析 ────────────────────────────────────────────────────────────────


def _runtime_settings(settings) -> dict:
    """读 `<data_dir>/settings.json`（前端"连接与设置"与 `orbit config set` 都写这里）。

    为什么要这条兜底：`approval_mode` 是后加的键，而 `Settings` 是 pydantic 的
    启动期模型——把开关只放在 `Settings` 上，就意味着"改完必须重启后端"。
    审批模式是要能当场改的东西，所以运行时文件优先于环境变量。
    """
    path = Path(getattr(settings, "data_dir", ".")) / "settings.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def resolve_mode(settings) -> str:
    """当前审批模式。非法值一律退回 `off`（**fail-closed 的反面**）。

    这里刻意不 fail-closed：`off` 是 Orbit 的既有行为，配置损坏时退回它
    才是"不改变现状"。真正 fail-closed 的是硬地板——那一层与配置无关。
    """
    value = _runtime_settings(settings).get("approval_mode")
    if value is None:
        value = getattr(settings, "approval_mode", "off")
    text = str(value or "off").strip().lower()
    return text if text in APPROVAL_MODES else "off"


def resolve_hardline_in_machine(settings) -> bool:
    """完全访问档是否也守硬地板。默认 `False`（尊重 Orbit 既有产品决策）。"""
    value = _runtime_settings(settings).get("hardline_in_machine")
    if value is None:
        value = getattr(settings, "hardline_in_machine", False)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# ── 白名单存储 ──────────────────────────────────────────────────────────────


@dataclass
class AllowList:
    """命令白名单。落在 `<data_dir>/approvals.json`，**只增不猜**。

    刻意不做"自动学习"：白名单是安全边界，静默扩张比没有更危险。
    `approvals suggest` 只产出建议，写盘必须显式 `--apply`。
    """

    path: Path
    entries: list[dict[str, str]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> AllowList:
        instance = cls(path=path)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return instance
            if isinstance(data, dict):
                raw = data.get("allow", [])
                if isinstance(raw, list):
                    instance.entries = [item for item in raw if isinstance(item, dict)]
        return instance

    @property
    def rules(self) -> tuple[str, ...]:
        return tuple(str(item.get("rule", "")) for item in self.entries if item.get("rule"))

    def add(self, rule: str, note: str = "") -> bool:
        """加一条。已存在返回 False（幂等，不重复写）。"""
        if any(item.get("rule") == rule for item in self.entries):
            return False
        self.entries.append({"rule": rule, "note": note})
        self._save()
        return True

    def remove(self, rule: str) -> bool:
        before = len(self.entries)
        self.entries = [item for item in self.entries if item.get("rule") != rule]
        if len(self.entries) != before:
            self._save()
            return True
        return False

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"allow": self.entries}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
