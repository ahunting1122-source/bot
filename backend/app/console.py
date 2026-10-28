"""安全命令台：只允许白名单里的只读命令，**绝不经过 shell**。

## 为什么需要它（本模块存在的理由，别删）

Hermes 的 `console` 是"只允许白名单命令的交互台"。Orbit 的对应场景非常具体：
**人不在电脑前**，通过消息平台 / 网关 / 远程通道给家里那台机器发一条指令。
这种输入**必须假设不可信**：

- 转发链路上的平台账号、群聊、webhook 都可能被别人碰到；
- 消息正文是纯文本，谁都能往里塞 ``rm -rf /`` 或 ``curl x | sh``；
- 一旦放行任意命令，这台机器上跑着的浏览器 cookie、密钥库、工作目录就全交出去了。

所以这里做三件事，缺一不可：

1. **白名单**（:data:`ALLOWED`）：只放只读 / 无害命令（status / doctor /
   config show / logs / insights / monitoring …）。凡是会写数据、发网络请求、
   起进程的，一条都不放——白名单要"窄到不需要逐个论证为什么安全"。
2. **不经过 shell**：命令以**列表**形式直接交给 CLI 的 ``main()``（等价于
   ``subprocess`` 的列表形式），全程没有 shell 中间层、没有字符串拼接。
   在此之上，``|`` / ``;`` / ``&&`` / ``$()`` / 反引号 这些 **shell 注入面**
   一律先拒：它们在一条"参数列表"里没有任何合法用途，出现即视为注入尝试，
   而不是"用户手滑"。注入检查打在**原文**上（分词之前），因为换行这类分隔符
   一旦分词就消失了——先分词再检查会漏掉 ``status\\nrm -rf /``。
3. **审计**（``<data_dir>/console-audit.jsonl``）：每次执行都记一行
   ``{ts, argv, allowed, reason}``，**拒绝也记**。只记成功的审计等于把
   "有人试过 ``rm -rf``"这件事从历史里抹掉——那正是最该留下的那一条。

白名单可以临时扩（`orbit console --allow "orbit skin list"`），但**只能扩**：
扩展项仍要走同样的注入检查与审计，而且只在本次会话生效（不落盘），
避免"为了图省事永久放开一条写命令"。
"""

from __future__ import annotations

import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: 白名单：只读 / 无害的命令路径（**不含** ``orbit`` 前缀）。
#:
#: 挑选标准只有一条：**它出错也不会改动机器状态**。因此刻意排除：
#: `chat`（会花 token、会调工具）、`setup` / `config set`（写配置）、
#: `serve` / `dashboard`（起进程、开浏览器）、`backup` / `sessions prune`
#: （写盘）、`cron run`（执行脚本）。宁可少一条，也不要一条"看起来只读"的。
ALLOWED: tuple[str, ...] = (
    "status",
    "doctor",
    "config show",
    "config path",
    "config get",
    "logs",
    "insights",
    "monitoring status",
    "monitoring metrics",
    "skin list",
    "skin show",
    "prompt-size",
    "completion",
)

#: 全局帮助 / 版本：不是"命令路径"而是顶层 flag，单独放行。
#: 它们只打印文本，且是"我连上了但不知道能干什么"时最该能跑的两条。
ALLOWED_FLAGS: tuple[str, ...] = ("--help", "-h", "--version", "-V")

#: shell 注入面。命中即拒——一条参数列表里它们不可能有合法用途。
SHELL_META: tuple[str, ...] = (
    "|",
    "||",
    "&",
    "&&",
    ";",
    "$(",
    "${",
    "`",
    ">",
    "<",
    "\n",
    "\r",
    "\\\n",
)

#: 拒绝时的退出码。126 = "命令存在但不可执行"（Unix 语义），便于调用方区分
#: "被策略拒了"和"命令自己跑失败了"。
DENIED_CODE = 126


@dataclass(frozen=True)
class ConsolePolicy:
    """白名单策略。``extra`` 是本次会话临时加的白名单项（`--allow`）。"""

    allowed: frozenset[str] = field(default_factory=lambda: frozenset(ALLOWED))
    extra: tuple[str, ...] = ()

    @classmethod
    def default(cls) -> ConsolePolicy:
        return cls()

    def with_extra(self, *entries: str) -> ConsolePolicy:
        """临时扩白名单。只接受"命令路径"形状，含 shell 元字符的直接丢弃。

        丢弃而不是抛错：`--allow` 的输入同样不可信，它不该让整个控制台起不来，
        但**也绝不能**把一条 ``status; rm -rf`` 塞进白名单——那种条目永远匹配不上
        任何合法 argv，留着只是噪声。条目里的 ``orbit`` 前缀会被去掉（用户按
        `--help` 里写的形状抄进来时一定带前缀）。
        """
        cleaned: list[str] = []
        for entry in entries:
            text = str(entry).strip()
            if not text or _shell_meta_in(text):
                continue
            path = " ".join(_normalize(text))
            if path:
                cleaned.append(path)
        return ConsolePolicy(allowed=self.allowed, extra=self.extra + tuple(cleaned))

    def entries(self) -> tuple[str, ...]:
        """全部生效的白名单项（内置 + 临时），按内置顺序展示、去掉重复。

        `allowed` 是 frozenset（集合语义：查得快、不会被意外修改），但**展示**要有
        稳定顺序——否则每次打印出来的白名单顺序都不一样，看着像配置在变。
        """
        ordered = sorted(
            self.allowed,
            key=lambda entry: (ALLOWED.index(entry) if entry in ALLOWED else len(ALLOWED), entry),
        )
        merged: list[str] = list(ordered)
        for entry in self.extra:
            if entry not in merged:
                merged.append(entry)
        return tuple(merged)

    def check(self, argv: Any) -> tuple[bool, str]:
        """判定一条命令是否放行。返回 ``(是否允许, 理由)``。``argv`` 可以是字符串
        （远程消息的原始形态）或列表（已分词的调用形态）。

        顺序刻意如此：**先查注入面，再查白名单**。反过来的话，一条
        ``status; rm -rf /`` 会先匹配上 `status` 前缀，理由写成"允许"——
        注入尝试就被伪装成了一次合法调用。而且注入检查打在**分词前的原文**上：
        ``status\\nrm -rf /`` 分词后换行没了，只查 token 会漏。
        """
        raw = argv if isinstance(argv, str) else " ".join(str(token) for token in (argv or []))
        hit = _shell_meta_in(raw)
        if hit:
            return False, (
                f"检测到 shell 注入面 {hit!r}：控制台不经过 shell，"
                "参数列表里出现管道/分隔符/命令替换一律拒绝"
            )
        tokens = _normalize(argv)
        if not tokens:
            return False, "空命令"
        path = _path_tokens(tokens)
        if not path:
            if tokens[0] in ALLOWED_FLAGS:
                return True, f"命中白名单：orbit {tokens[0]}"
            return False, "缺少命令名（第一条不是选项）"
        for entry in self.entries():
            entry_tokens = entry.split()
            if path[: len(entry_tokens)] == entry_tokens:
                return True, f"命中白名单：orbit {entry}"
        return False, (
            f"不在白名单：orbit {' '.join(path)}"
            f"（只允许只读命令：{'、'.join('orbit ' + e for e in self.entries())}）"
        )


@dataclass(frozen=True)
class ConsoleResult:
    """一次控制台执行的结果。``output`` / ``error`` 是捕获到的子命令输出。"""

    argv: tuple[str, ...]
    allowed: bool
    reason: str = ""
    code: int = 0
    output: str = ""
    error: str = ""


def audit_path(data_dir: str | Path) -> Path:
    return Path(data_dir) / "console-audit.jsonl"


def _shell_meta_in(text: str) -> str:
    """返回命中的第一个注入面标记，没有则空串。"""
    for marker in SHELL_META:
        if marker in text:
            return marker
    return ""


def _normalize(argv: Any) -> list[str]:
    """把 argv 收敛成"不带 orbit 前缀、无空串"的 token 列表。

    接受字符串（远程消息的原始形态）或列表（已分词的调用形态）。字符串用
    ``shlex`` **只做分词**，不做任何替换/展开——分词结果仍然要过白名单。
    """
    tokens = _split(argv)
    if tokens and tokens[0].strip().lower() in {"orbit", "orbit.exe"}:
        tokens = tokens[1:]
    return tokens


def _split(argv: Any) -> list[str]:
    if isinstance(argv, str):
        import shlex

        try:
            return [token for token in shlex.split(argv, posix=False) if token.strip()]
        except ValueError:  # 引号没闭合：整条当单 token，后续照样会被白名单挡住
            return [argv] if argv.strip() else []
    return [str(token) for token in (argv or []) if str(token).strip()]


def _path_tokens(tokens: list[str]) -> list[str]:
    """取命令路径：从开头连续的非选项 token（遇到第一个 ``-`` 开头的就停）。"""
    path: list[str] = []
    for token in tokens:
        if token.startswith("-"):
            break
        path.append(token)
    return path


def execute(
    argv: Any,
    *,
    policy: ConsolePolicy | None = None,
    data_dir: str | Path | None = None,
    settings: Any = None,
) -> ConsoleResult:
    """执行一条白名单命令。**不经过 shell**：argv 列表直接交给 CLI 的 ``main()``。

    ``data_dir`` 会以全局 ``--data-dir`` 形式传给子命令，让控制台操作的是
    **同一个数据目录**（审计也落在那里）——否则远程操作会读到另一个库，
    输出的数字看着像真的、其实是别人的。
    """
    policy = policy or ConsolePolicy.default()
    tokens = _normalize(argv)
    allowed, reason = policy.check(tokens)
    target = data_dir if data_dir is not None else getattr(settings, "data_dir", None)
    _audit(target, tokens, allowed, reason)
    if not allowed:
        return ConsoleResult(argv=tuple(tokens), allowed=False, reason=reason, code=DENIED_CODE)

    # 懒导入：`app.cli.main` 会拉起整棵命令树，放模块顶部会和控制台自身成环。
    from app.cli.main import main as cli_main

    call = list(tokens)
    if target is not None:
        call = ["--data-dir", str(target), *call]
    out, err = io.StringIO(), io.StringIO()
    code = 0
    try:
        with redirect_stdout(out), redirect_stderr(err):
            code = int(cli_main(call) or 0)
    except SystemExit as exc:  # `--help` / `--version` 走 argparse 的正常出口
        code = int(exc.code or 0)
    except Exception as exc:  # noqa: BLE001 —— 控制台不能因为子命令崩了就断线
        return ConsoleResult(
            argv=tuple(tokens),
            allowed=True,
            reason=reason,
            code=1,
            output=out.getvalue(),
            error=f"{type(exc).__name__}: {exc}",
        )
    return ConsoleResult(
        argv=tuple(tokens),
        allowed=True,
        reason=reason,
        code=code,
        output=out.getvalue(),
        error=err.getvalue(),
    )


def _audit(data_dir: str | Path | None, argv: list[str], allowed: bool, reason: str) -> None:
    """追加一行审计。写不进去也不抛——但绝不假装写过（返回值/异常都由调用方看日志）。"""
    if data_dir is None:
        return
    path = audit_path(data_dir)
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "argv": list(argv),
        "allowed": allowed,
        "reason": reason,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        # 审计写失败是环境问题（只读盘 / 权限），不该让一条只读命令跟着失败。
        pass


def read_audit(data_dir: str | Path, *, limit: int = 50) -> list[dict[str, Any]]:
    """读审计尾部（时间正序）。坏行跳过——一行坏了不该让整份历史读不出来。"""
    path = audit_path(data_dir)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            records.append(item)
    return records[-limit:] if limit > 0 else records
