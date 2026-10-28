"""Shell 脚本 hook：让外部命令挂在 agent 生命周期事件上。

## 与 `app/hooks.py` 的分工

`app/hooks.py` 是**进程内**的 hook 注册表——插件用 `register(event, fn)` 挂一个
Python 可调用对象，跑在同一个进程里。这一层是**进程外**的：用户在
`<data_dir>/hooks.json` 里声明一条 shell 命令，事件发生时以子进程执行。

为什么要两套：进程内 hook 需要写 Python 插件；而"每次会话结束把日志推到我的
归档脚本"这种需求，用户只想挂一行命令，不想写插件。

## 为什么默认 `trusted=False`

一条 shell hook 就是**任意代码执行**，而且它会在每次会话/每次工具调用时自动跑。
Hermes 用 `--accept-hooks` / `hooks_auto_accept` 显式授权，Orbit 照抄这个原则：

- 新加的 hook 一律 `trusted=False`，**不执行**，只在 `orbit hooks list` 里显示
  "待授权"，并告诉你 `orbit hooks trust <id>`。
- 为什么不做成"提示一下就跑"：这个 hook 会长期存在，一次手滑等于长期后门。
  授权必须是**一次显式动作**，而不是一次默认同意。

## 为什么 hook 失败绝不影响主流程

hook 是旁路观测，不是主链路。它超时、崩溃、返回非零，都只记一条日志。
让一个用户写的脚本把 agent 卡死或打断，是把旁路变成了关键路径。
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.hooks import HOOK_EVENTS

log = logging.getLogger("orbit.shellhooks")

#: hook 声明文件。
HOOKS_FILENAME = "hooks.json"

#: 单个 hook 的默认超时（秒）。超过就杀掉——挂住的 hook 会拖慢每一轮。
DEFAULT_TIMEOUT = 10

#: 超时上限。允许设很大等于允许把自己卡死。
MAX_TIMEOUT = 120


class ShellHookError(RuntimeError):
    """声明层面的错误（事件名不认识、命令为空）。执行期的错误不走这里。"""


@dataclass
class ShellHook:
    id: str
    event: str
    command: str
    timeout: int = DEFAULT_TIMEOUT
    enabled: bool = True
    #: 未授权的 hook **不执行**。见模块 docstring。
    trusted: bool = False
    note: str = ""
    created_at: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class HookOutcome:
    """一次执行的结果。`ran=False` 时 `reason` 说明为什么没跑。"""

    hook_id: str
    event: str
    ran: bool
    ok: bool = False
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DoctorReport:
    hook_id: str
    event: str
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def _slug(text: str) -> str:
    clean = "".join(ch if (ch.isalnum() or ch in "-_") else "-" for ch in (text or "").strip())
    return clean.strip("-")[:40] or "hook"


class ShellHookStore:
    """`<data_dir>/hooks.json` 的读写。原子写：半个文件比没有文件更糟。"""

    def __init__(self, data_dir: Path | str) -> None:
        self.path = Path(data_dir) / HOOKS_FILENAME

    # —— 持久化 ——

    def _load(self) -> list[ShellHook]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        rows = payload.get("hooks") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return []
        hooks: list[ShellHook] = []
        for row in rows:
            if not isinstance(row, dict) or not row.get("event"):
                continue
            hooks.append(
                ShellHook(
                    id=str(row.get("id") or _slug(str(row.get("event")))),
                    event=str(row.get("event")),
                    command=str(row.get("command") or ""),
                    timeout=int(row.get("timeout") or DEFAULT_TIMEOUT),
                    enabled=bool(row.get("enabled", True)),
                    trusted=bool(row.get("trusted", False)),
                    note=str(row.get("note") or ""),
                    created_at=str(row.get("created_at") or ""),
                )
            )
        return hooks

    def _save(self, hooks: list[ShellHook]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "hooks": [hook.as_dict() for hook in hooks]}
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    # —— 操作 ——

    def list(self) -> list[ShellHook]:
        return self._load()

    def get(self, hook_id: str) -> ShellHook | None:
        return next((hook for hook in self._load() if hook.id == hook_id), None)

    def add(
        self,
        event: str,
        command: str,
        *,
        timeout: int = DEFAULT_TIMEOUT,
        note: str = "",
    ) -> ShellHook:
        name = (event or "").strip()
        if name not in HOOK_EVENTS:
            raise ShellHookError(
                f"未知事件：{event!r}（可用事件见 `orbit hooks events`，共 {len(HOOK_EVENTS)} 个）"
            )
        body = (command or "").strip()
        if not body:
            raise ShellHookError("hook 的命令不能为空")
        bounded = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
        hooks = self._load()
        hook = ShellHook(
            id=_slug(f"{name}-{len(hooks) + 1}-{body[:20]}"),
            event=name,
            command=body,
            timeout=bounded,
            enabled=True,
            # 新 hook 一律未授权：见模块 docstring。
            trusted=False,
            note=note.strip(),
            created_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        )
        hooks.append(hook)
        self._save(hooks)
        return hook

    def remove(self, hook_id: str) -> bool:
        hooks = self._load()
        kept = [hook for hook in hooks if hook.id != hook_id]
        if len(kept) == len(hooks):
            return False
        self._save(kept)
        return True

    def set_enabled(self, hook_id: str, enabled: bool) -> bool:
        hooks = self._load()
        for hook in hooks:
            if hook.id == hook_id:
                hook.enabled = enabled
                self._save(hooks)
                return True
        return False

    def trust(self, hook_id: str) -> bool:
        """显式授权。这是唯一能让未授权 hook 开始执行的入口。"""
        hooks = self._load()
        for hook in hooks:
            if hook.id == hook_id:
                hook.trusted = True
                self._save(hooks)
                return True
        return False

    def revoke(self, hook_id: str) -> bool:
        """撤销授权（hook 保留但不再执行）。与 `remove` 的区别：保留声明便于重新授权。"""
        hooks = self._load()
        for hook in hooks:
            if hook.id == hook_id:
                hook.trusted = False
                self._save(hooks)
                return True
        return False

    def revoke_by_command(self, command: str) -> int:
        """按**命令原文**撤销授权，返回撤销了几条。

        为什么按命令原文而不是 id：Hermes 的 `hooks revoke <command>` 就是这么定义的
        （用户在配置里看到的是命令本身，不是我们生成的 id）。同一段命令可能被声明
        多次，所以全部撤销并返回条数——只撤一条会让人以为撤干净了。
        """
        target = (command or "").strip()
        if not target:
            return 0
        hooks = self._load()
        changed = 0
        for hook in hooks:
            if hook.command.strip() == target and hook.trusted:
                hook.trusted = False
                changed += 1
        if changed:
            self._save(hooks)
        return changed


def _interpreter(command: str) -> str:
    """取命令的第一个词，判断它能不能被找到。"""
    try:
        parts = shlex.split(command, posix=False)
    except ValueError:
        parts = command.split()
    return parts[0] if parts else ""


def doctor(store: ShellHookStore) -> list[DoctorReport]:
    """静态体检：不执行任何 hook，只看声明有没有问题。"""
    reports: list[DoctorReport] = []
    for hook in store.list():
        report = DoctorReport(hook_id=hook.id, event=hook.event)
        if hook.event not in HOOK_EVENTS:
            report.problems.append(f"未知事件 {hook.event!r}")
        if not hook.command.strip():
            report.problems.append("命令为空")
        interpreter = _interpreter(hook.command)
        if interpreter and not (
            shutil.which(interpreter) or Path(interpreter).is_file()
        ):
            # 只记 warning：可能是 shell 内建（如 `echo` 在 cmd 里）或依赖 PATH 里的别名。
            report.warnings.append(f"找不到可执行文件 {interpreter!r}")
        if hook.timeout > MAX_TIMEOUT:
            report.problems.append(f"超时 {hook.timeout}s 超过上限 {MAX_TIMEOUT}s")
        if not hook.trusted:
            report.warnings.append("未授权，不会执行（`orbit hooks trust` 授权）")
        if not hook.enabled:
            report.warnings.append("已停用")
        reports.append(report)
    return reports


def run(
    hook: ShellHook,
    payload: dict[str, Any] | None = None,
    *,
    timeout: int | None = None,
) -> HookOutcome:
    """执行一条 hook。**任何失败都只返回结果，绝不抛异常**（见模块 docstring）。"""
    outcome = HookOutcome(hook_id=hook.id, event=hook.event, ran=False)
    if not hook.trusted:
        outcome.reason = "未授权（`orbit hooks trust <id>` 后才会执行）"
        return outcome
    if not hook.enabled:
        outcome.reason = "已停用"
        return outcome

    limit = max(1, min(int(timeout or hook.timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    env = dict(os.environ)
    # 载荷走环境变量而不是命令行参数：命令行会被 ps 看到，hook 载荷里可能有敏感内容。
    env["ORBIT_HOOK_EVENT"] = hook.event
    env["ORBIT_HOOK_ID"] = hook.id
    env["ORBIT_HOOK_PAYLOAD"] = json.dumps(payload or {}, ensure_ascii=False, default=str)

    started = time.monotonic()
    outcome.ran = True
    try:
        completed = subprocess.run(  # noqa: S602 - 这是本模块的用途：跑用户声明的命令
            hook.command,
            shell=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=limit,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        outcome.ok = False
        outcome.reason = f"超时 {limit}s"
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        log.warning("hook %s（%s）超时 %ss", hook.id, hook.event, limit)
        return outcome
    except OSError as exc:
        outcome.ok = False
        outcome.reason = f"无法启动：{exc}"
        outcome.duration_ms = int((time.monotonic() - started) * 1000)
        return outcome

    outcome.duration_ms = int((time.monotonic() - started) * 1000)
    outcome.exit_code = completed.returncode
    outcome.ok = completed.returncode == 0
    outcome.stdout = (completed.stdout or "")[:4000]
    outcome.stderr = (completed.stderr or "")[:4000]
    if not outcome.ok:
        outcome.reason = f"退出码 {completed.returncode}"
        log.warning("hook %s（%s）失败：%s", hook.id, hook.event, outcome.reason)
    return outcome


def run_event(store: ShellHookStore, event: str, payload: dict[str, Any] | None = None) -> list[HookOutcome]:
    """跑某个事件下的全部 hook。单条失败不影响其它条。"""
    return [run(hook, payload) for hook in store.list() if hook.event == event]
