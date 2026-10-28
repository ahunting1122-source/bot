"""审批三态的**交互通道**（MATRIX 14.1）：`ask` 之后总得有人回答。

## 这个模块补的是哪一段

`app/approvals.py` 只做到"分类"：`off` 放行、`smart` / `manual` 判出 `ask`。
判出 `ask` 之后**没有人去问**——于是 `ask` 在 Orbit 里的实际语义退化成
"拒绝，你自己去做"。那不是 Hermes 的 ask，只是把拒绝换了句措辞。

本模块补的就是这一步：`ApprovalChannel.ask(request) -> Decision`。

## 三个通道，各自的适用场景

| 通道 | 用在哪 | 关键取舍 |
|---|---|---|
| :class:`ConsoleChannel` | 人在终端里跑 `orbit chat` / `orbit run` | **无 TTY 必须超时返回**，不许挂死 |
| :class:`FileChannel` | gateway / cron / 面板：没有终端，但有另一个进程能写文件 | 请求落盘 + 轮询响应，跨进程 |
| :class:`AutoDenyChannel` | 无人值守的默认 | **deny 而不是 allow** |

### 为什么 AutoDenyChannel 是 deny 而不是 allow

无人值守时"没人回答"和"有人同意"在结果上必须不同，否则整条审批链就变成了
一个摆设。让它 allow 等于把 `smart` / `manual` 悄悄降级成 `off`——而用户选
`manual` 的**全部理由**就是"别自己乱跑"。所以默认拒绝，并把理由写清楚
（"没有人值守"），让人回来时能一眼看出是"没问成"而不是"被否决了"。

## 超时语义：超时 = deny，但**必须说清是超时**

这是本模块最容易做错的一处。把超时和拒绝混成同一句话，用户会以为自己点过
"拒绝"，然后去翻日志找那条并不存在的操作记录，最后怀疑是自己记错了。
所以 :data:`Decision.outcome` 是**三态**（`allow` / `deny` / `timeout`），
不是两态；两种"不放行"的 `reason` 措辞完全不同，且 `timeout` 里带上了等了多久。

## 接线点（`app/tools.py` 是禁改区，所以这里只提供一行能调的 API）

`tools.py` 的 `_approve_command` 现在的形状是：

    verdict = approvals.classify(...)
    if verdict.verdict != approvals.VERDICT_ALLOW:
        raise ValueError(_refusal(...))

`classify` 返回 `ask` 时，它应当改成调本模块的
:func:`approve_or_refuse`（一行）：

    if verdict.verdict == approvals.VERDICT_ASK:
        decision = approve_or_refuse(
            text, mode=mode, settings=self.settings, task_id=task_id, reason=verdict.reason
        )
        if not decision.allowed:
            raise ValueError(_refusal(verdict, ..., decision.reason))

`approve_or_refuse` 已经把所有分支（模式 off / 通道缺失 / 超时 / 拒绝）都收进
一个 :class:`Decision` 里，接线方不需要再判一次。`verdict.verdict == deny`
（硬地板）**不走这条**——硬地板没有"问一句"的余地。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.approvals import APPROVAL_MODES

#: 裁决三态。`allow` / `deny` 与 `app/approvals.py` 的 `VERDICT_*` 同名同义，
#: `timeout` 是**本模块新增的第三态**——见模块 docstring 的"超时语义"。
ALLOW, DENY, TIMEOUT = "allow", "deny", "timeout"
DECISION_OUTCOMES: tuple[str, ...] = (ALLOW, DENY, TIMEOUT)

#: 队列条目的四种状态。`expired` 与 `denied` 分开，同样是为了不把"没人回答"
#: 读成"被否决"。
PENDING, APPROVED, DENIED, EXPIRED = "pending", "approved", "denied", "expired"

#: 默认等待上限（秒）。60 秒是个折中：够人读完一条命令，又不至于让一条卡住的
#: 命令把整个任务拖到超时上限（`settings.command_timeout` 默认 120）。
DEFAULT_TIMEOUT = 60.0

#: 无 TTY 时的说明。**必须点名"超时"**：这句话会直接出现在拒绝信息里。
NO_TTY_REASON = (
    "没有可交互的终端（stdin 不是 TTY），按超时处理："
    "这次没有人被问到，不是有人点了拒绝。"
    "要真正参与审批，请在终端里运行，或配置 `approval_channel=file` 让别的进程回答。"
)

#: 无人值守通道的说明。
AUTO_DENY_REASON = (
    "无人值守审批通道（AutoDenyChannel）：没有人在等这条命令，按拒绝处理。"
    "这不是人工否决——想让它通过，请加白（`orbit approvals allow add <规则>`）、"
    "在终端里重跑一次，或配置 `approval_channel` 让某个进程来回答。"
)


def _now() -> str:
    """统一的时间戳形状（UTC ISO8601）。"""
    return datetime.now(UTC).isoformat()


def _atomic_write(path: Path, payload: Any) -> None:
    """原子写：临时文件 + `os.replace`。

    为什么必须原子：队列是**跨进程**共享的（gateway 写、CLI 读）。半截 JSON 会让
    读取方把整份队列当成坏文件丢掉——也就是"待审批列表突然空了"，而人只会以为
    "没有待审批"。临时文件名带 pid，避免两个进程撞同一个 `.tmp`。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


def _read_json(path: Path) -> Any:
    """读 JSON。读不出来返回 `None`（调用方决定怎么兜底，本模块一律当"空"）。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ── 请求与裁决 ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ApprovalRequest:
    """一条待确认的动作。

    `options` 是**展示用**的候选答案（`("allow", "deny")`），不是权限声明——
    通道不能因为 `options` 里写了 `allow` 就自动放行。
    """

    id: str
    task_id: str = ""
    command: str = ""
    reason: str = ""
    mode: str = "smart"
    created_at: str = ""
    options: tuple[str, ...] = (ALLOW, DENY)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "command": self.command,
            "reason": self.reason,
            "mode": self.mode,
            "created_at": self.created_at,
            "options": list(self.options),
        }

    @classmethod
    def from_dict(cls, data: Any) -> ApprovalRequest | None:
        """从落盘的字典还原。缺 `id` 或形状不对时返回 `None`（坏条目不该炸整次读）。"""
        if not isinstance(data, dict):
            return None
        identifier = str(data.get("id") or "").strip()
        if not identifier:
            return None
        raw_options = data.get("options")
        options = (
            tuple(str(item) for item in raw_options)
            if isinstance(raw_options, list) and raw_options
            else (ALLOW, DENY)
        )
        return cls(
            id=identifier,
            task_id=str(data.get("task_id") or ""),
            command=str(data.get("command") or ""),
            reason=str(data.get("reason") or ""),
            mode=str(data.get("mode") or "smart"),
            created_at=str(data.get("created_at") or ""),
            options=options,
        )


def make_request(
    command: str,
    *,
    task_id: str = "",
    reason: str = "",
    mode: str = "smart",
    identifier: str | None = None,
) -> ApprovalRequest:
    """造一条请求。`id` 默认取 uuid 前 12 位——短到能手工抄进 `approvals decide`。"""
    return ApprovalRequest(
        id=identifier or uuid.uuid4().hex[:12],
        task_id=task_id,
        command=command,
        reason=reason,
        mode=mode,
        created_at=_now(),
    )


@dataclass(frozen=True)
class Decision:
    """一条请求的裁决结果。

    `reason` 是**给人看的完整说明**，超时与拒绝的措辞必须不同（见模块 docstring）。
    `decided_by` 记的是通道名，出问题时能回答"这句话是谁说的"。
    """

    outcome: str = DENY
    note: str = ""
    reason: str = ""
    decided_at: str = ""
    decided_by: str = ""

    @property
    def allowed(self) -> bool:
        return self.outcome == ALLOW

    @property
    def timed_out(self) -> bool:
        return self.outcome == TIMEOUT

    @classmethod
    def allow(cls, *, note: str = "", reason: str = "", by: str = "") -> Decision:
        return cls(
            outcome=ALLOW,
            note=note,
            reason=reason or f"人工确认：允许（{by or '通道'}）。",
            decided_at=_now(),
            decided_by=by,
        )

    @classmethod
    def deny(cls, *, note: str = "", reason: str = "", by: str = "") -> Decision:
        return cls(
            outcome=DENY,
            note=note,
            reason=reason or f"人工确认：拒绝（{by or '通道'}）。",
            decided_at=_now(),
            decided_by=by,
        )

    @classmethod
    def timed_out_at(cls, seconds: float, *, by: str = "", reason: str = "") -> Decision:
        """超时。**措辞里必须出现"超时"且明说不是拒绝**。"""
        text = reason or (
            f"等待人工确认超时（{seconds:g} 秒内没有人回答），按拒绝处理——"
            "这是超时，不是你点了拒绝。"
        )
        return cls(
            outcome=TIMEOUT, note="", reason=text, decided_at=_now(), decided_by=by
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "allowed": self.allowed,
            "timed_out": self.timed_out,
            "note": self.note,
            "reason": self.reason,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
        }


# ── 队列 ────────────────────────────────────────────────────────────────────


@dataclass
class ApprovalQueue:
    """待审批队列，落 `<data>/approvals/queue.json`（**原子写**）。

    为什么要有队列而不是"通道自己记着"：`ConsoleChannel` 是一次性的问答，
    人不在终端前的时候那条请求就消失了。队列把"问过什么、等了多久、谁答的"
    留下来，`orbit approvals queue` 才能列出真正待办的东西。

    队列**不是**权限来源：`decide` 只改记录状态，不会让任何命令被执行。
    """

    path: Path
    entries: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def path_for(cls, data_dir: str | Path) -> Path:
        return Path(data_dir) / "approvals" / "queue.json"

    @classmethod
    def load(cls, data_dir: str | Path) -> ApprovalQueue:
        path = cls.path_for(data_dir)
        instance = cls(path=path)
        raw = _read_json(path)
        if isinstance(raw, dict):
            entries = raw.get("requests")
            if isinstance(entries, list):
                instance.entries = [item for item in entries if isinstance(item, dict)]
        elif isinstance(raw, list):
            instance.entries = [item for item in raw if isinstance(item, dict)]
        return instance

    # ── 写 ──────────────────────────────────────────────────────────────────
    def submit(self, request: ApprovalRequest) -> dict[str, Any]:
        """登记一条请求。同 `id` 重复提交时**保留原条目**（幂等，不覆盖已有裁决）。"""
        for entry in self.entries:
            if entry.get("id") == request.id:
                return entry
        entry = request.as_dict()
        entry["status"] = PENDING
        entry["decided_at"] = ""
        entry["note"] = ""
        self.entries.append(entry)
        self._save()
        return entry

    def decide(self, identifier: str, approved: bool, note: str = "") -> bool:
        """记录一条裁决。找不到或已有裁决时返回 `False`（不覆盖，不猜）。"""
        status = APPROVED if approved else DENIED
        return self._set_status(identifier, status, note=note)

    def expire_one(self, identifier: str, note: str = "") -> bool:
        """把**一条**待审批标成超时。给"通道等超时了"这条路径用。"""
        return self._set_status(
            identifier, EXPIRED, note=note or "通道等待超时，标记为超时（不是拒绝）"
        )

    def expire(self, max_age: float) -> list[str]:
        """把等待超过 `max_age` 秒的 `pending` 条目标成 `expired`，返回被标记的 id。

        `expired` 与 `denied` 分开是刻意的：超时不是有人拒绝（见模块 docstring）。
        """
        now = time.time()
        expired: list[str] = []
        for entry in self.entries:
            if entry.get("status") != PENDING:
                continue
            created = _parse_epoch(entry.get("created_at"))
            if created is None:
                continue
            if now - created >= max_age:
                entry["status"] = EXPIRED
                entry["decided_at"] = _now()
                entry["note"] = f"等待超过 {max_age:g} 秒，标记为超时（不是拒绝）"
                expired.append(str(entry.get("id") or ""))
        if expired:
            self._save()
        return expired

    # ── 读 ──────────────────────────────────────────────────────────────────
    def pending(self) -> list[dict[str, Any]]:
        """还没裁决的条目。"""
        return [entry for entry in self.entries if entry.get("status") == PENDING]

    def get(self, identifier: str) -> dict[str, Any] | None:
        for entry in self.entries:
            if entry.get("id") == identifier:
                return entry
        return None

    def as_dict(self) -> dict[str, Any]:
        return {"path": str(self.path), "requests": list(self.entries)}

    # ── 内部 ────────────────────────────────────────────────────────────────
    def _set_status(self, identifier: str, status: str, *, note: str = "") -> bool:
        entry = self.get(identifier)
        if entry is None:
            return False
        if entry.get("status") != PENDING:
            return False
        entry["status"] = status
        entry["decided_at"] = _now()
        entry["note"] = note
        self._save()
        return True

    def _save(self) -> None:
        _atomic_write(self.path, self.as_dict())


def _parse_epoch(value: Any) -> float | None:
    """把 ISO 时间串转成 epoch 秒。解析不了返回 `None`（**不猜**，不当作已过期）。"""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


# ── 通道 ────────────────────────────────────────────────────────────────────


@runtime_checkable
class ApprovalChannel(Protocol):
    """交互通道。`ask` 必须**有界返回**——挂死的通道等于把 agent 卡住。"""

    name: str

    def ask(self, request: ApprovalRequest) -> Decision:  # pragma: no cover - 协议声明
        ...


def _is_tty(stream: Any) -> bool:
    """`isatty()` 说真才算 TTY。任何异常都当"不是"（保守）。"""
    try:
        return bool(stream is not None and stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


class ConsoleChannel:
    """在终端里问一句。

    **无 TTY 立刻返回超时**，这是本类最重要的性质：`orbit gateway` / cron / CI 里
    `stdin` 通常是个已关闭的管道，直接 `readline()` 会拿到 EOF（看起来像"用户拒绝"）
    或者干脆永久阻塞（看起来像"agent 挂了"）。两者都是错的，所以先判 TTY。

    等待用**独立线程 + join 超时**实现，而不是 `select`：Windows 上 `select` 只
    支持 socket，不支持 stdin。
    """

    name = "console"

    def __init__(
        self,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        stdin: Any = None,
        stdout: Any = None,
    ) -> None:
        self.timeout = timeout
        self._stdin = stdin
        self._stdout = stdout

    def ask(self, request: ApprovalRequest) -> Decision:
        stream = self._stdin if self._stdin is not None else sys.stdin
        if not _is_tty(stream):
            return Decision.timed_out_at(
                self.timeout, by=self.name, reason=NO_TTY_REASON
            )
        out = self._stdout if self._stdout is not None else sys.stdout
        try:
            out.write(self.prompt_for(request))
            out.flush()
        except (OSError, ValueError):
            return Decision.timed_out_at(
                self.timeout, by=self.name, reason="写不出提示语（终端不可写），按超时处理。"
            )
        answer, answered = self._read_line(stream)
        if not answered:
            return Decision.timed_out_at(self.timeout, by=self.name)
        text = (answer or "").strip().casefold()
        if text in {"y", "yes", "allow", "a", "1", "ok"}:
            return Decision.allow(note=answer.strip(), by=self.name)
        if text in {"n", "no", "deny", "d", "0"}:
            return Decision.deny(note=answer.strip(), by=self.name)
        # 空行与看不懂的回答都按拒绝：默认必须是"不放行"。
        return Decision.deny(
            note=answer.strip(),
            by=self.name,
            reason=(
                f"回答「{answer.strip() or '（空）'}」不是允许（y）也不是拒绝（n），"
                "按拒绝处理。"
            ),
        )

    def prompt_for(self, request: ApprovalRequest) -> str:
        """提示语。把命令**原样**打出来——人要知道自己在批准什么。"""
        lines = [f"需要确认（审批模式 {request.mode}）：", f"  命令：{request.command}"]
        if request.reason:
            lines.append(f"  原因：{request.reason}")
        lines.append(f"  允许执行？[y/N]（{self.timeout:g} 秒内不回答视为超时拒绝）")
        return "\n".join(lines) + "\n"

    def _read_line(self, stream: Any) -> tuple[str, bool]:
        """读一行，最多等 `timeout` 秒。返回 `(内容, 是否读到)`。"""
        result: dict[str, Any] = {}

        def reader() -> None:
            try:
                result["line"] = stream.readline()
            except (OSError, ValueError):
                result["line"] = ""

        thread = threading.Thread(target=reader, name="orbit-approval-read", daemon=True)
        thread.start()
        thread.join(self.timeout)
        if thread.is_alive():
            # 线程是 daemon：读不到就走，不 join 死。人后来敲的那个键会被下一层忽略。
            return "", False
        if "line" not in result:
            return "", False
        return str(result.get("line") or ""), True


class FileChannel:
    """跨进程通道：把请求写成文件，轮询响应文件。

    给 gateway / cron / 面板这类**没有终端**的地方用：它自己不会回答，但可以让
    另一个进程（人或另一个 CLI）写响应。

    文件约定（`directory` 下）：

    - `<id>.request.json`：请求正文（原子写，避免读到半截）
    - `<id>.response.json`：`{"approved": true, "note": "..."}`
    - 响应**不删除**：它是"谁在什么时候答的"的唯一证据。
    """

    name = "file"

    def __init__(
        self,
        directory: str | Path,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        poll_interval: float = 0.2,
    ) -> None:
        self.directory = Path(directory)
        self.timeout = timeout
        self.poll_interval = poll_interval

    def request_path(self, identifier: str) -> Path:
        return self.directory / f"{identifier}.request.json"

    def response_path(self, identifier: str) -> Path:
        return self.directory / f"{identifier}.response.json"

    def ask(self, request: ApprovalRequest) -> Decision:
        try:
            _atomic_write(self.request_path(request.id), request.as_dict())
        except OSError as exc:
            # 写不出请求就等于问不到人。**不能**当成 allow，也不能挂死。
            return Decision.timed_out_at(
                self.timeout,
                by=self.name,
                reason=f"写不出审批请求文件（{exc}），没有人被问到，按超时处理。",
            )
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            decision = self._read_response(request.id)
            if decision is not None:
                return decision
            time.sleep(self.poll_interval)
        return Decision.timed_out_at(self.timeout, by=self.name)

    def _read_response(self, identifier: str) -> Decision | None:
        path = self.response_path(identifier)
        if not path.is_file():
            return None
        payload = _read_json(path)
        if not isinstance(payload, dict):
            # 半截/坏响应：当作"还没答"，继续等（而不是猜一个结论）。
            return None
        approved = payload.get("approved")
        if not isinstance(approved, bool):
            return None
        note = str(payload.get("note") or "")
        if approved:
            return Decision.allow(note=note, by=self.name)
        return Decision.deny(note=note, by=self.name)

    @staticmethod
    def respond(
        directory: str | Path, identifier: str, approved: bool, note: str = ""
    ) -> Path:
        """写下响应。给"回答方"用的公开入口（人、另一个 CLI、面板都调它）。"""
        path = Path(directory) / f"{identifier}.response.json"
        _atomic_write(path, {"approved": bool(approved), "note": note, "at": _now()})
        return path


class AutoDenyChannel:
    """无人值守的默认通道：**一律拒绝**。

    理由见模块 docstring 的"为什么 AutoDenyChannel 是 deny 而不是 allow"。
    一句话：让它 allow 等于把 `smart` / `manual` 静默降级成 `off`。
    """

    name = "auto-deny"

    def ask(self, request: ApprovalRequest) -> Decision:
        return Decision.deny(by=self.name, reason=AUTO_DENY_REASON)


# ── 选通道 ──────────────────────────────────────────────────────────────────

#: 配置文件里认的通道名。
CHANNEL_KINDS: tuple[str, ...] = ("console", "file", "auto-deny")


def _runtime_settings(settings: Any) -> dict[str, Any]:
    """读 `<data_dir>/settings.json`（与 `app/approvals.py` 同一套兜底）。

    为什么运行时文件优先：审批通道是"当场改、当场生效"的东西，只放在启动期
    的 pydantic `Settings` 上就意味着改完必须重启。
    """
    data_dir = getattr(settings, "data_dir", None)
    if data_dir is None:
        return {}
    payload = _read_json(Path(data_dir) / "settings.json")
    return payload if isinstance(payload, dict) else {}


def _pick(settings: Any, key: str, default: Any = None) -> Any:
    """先读运行时 settings.json，再读 settings 字段，最后用默认值。"""
    runtime = _runtime_settings(settings)
    if key in runtime and runtime[key] is not None:
        return runtime[key]
    return getattr(settings, key, default)


def channel_timeout(settings: Any = None) -> float:
    """等待上限（秒）。坏值退回 :data:`DEFAULT_TIMEOUT`，**不抛**。"""
    raw = _pick(settings, "approval_channel_timeout", DEFAULT_TIMEOUT)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT
    return value if value > 0 else DEFAULT_TIMEOUT


def channel_directory(settings: Any = None) -> Path:
    """`FileChannel` 的落点。默认 `<data>/approvals/channel`。"""
    raw = _pick(settings, "approval_channel_dir", "")
    if raw:
        return Path(str(raw))
    data_dir = getattr(settings, "data_dir", None) or Path.cwd()
    return Path(data_dir) / "approvals" / "channel"


def resolve_channel(
    mode: str, settings: Any = None, *, stdin: Any = None
) -> ApprovalChannel | None:
    """按审批模式与配置选一个通道。**`off` 返回 `None`**（不需要问任何人）。

    `smart` / `manual` 的默认选择：

    - stdin 是 TTY → :class:`ConsoleChannel`（有人就在终端前，问一句最直接）
    - 否则 → :class:`AutoDenyChannel`（没人可问，拒绝并说明）

    显式配置 `approval_channel=console|file|auto-deny` 可以覆盖默认；未知值退回
    `auto-deny` 并在 :func:`describe_channel` 里如实报出来（**fail closed**：
    配置写错时不能变成"随便放行"）。
    """
    normalized = str(mode or "off").strip().lower()
    if normalized not in APPROVAL_MODES:
        normalized = "off"
    if normalized == "off":
        return None

    timeout = channel_timeout(settings)
    raw = str(_pick(settings, "approval_channel", "") or "").strip().lower()
    kind = raw or ("console" if _is_tty(stdin if stdin is not None else sys.stdin) else "auto-deny")
    if kind == "console":
        return ConsoleChannel(timeout=timeout, stdin=stdin)
    if kind == "file":
        return FileChannel(channel_directory(settings), timeout=timeout)
    # `auto-deny` 与任何未知值都落到这里：宁可拒绝，不可放行。
    return AutoDenyChannel()


def describe_channel(mode: str, settings: Any = None, *, stdin: Any = None) -> dict[str, Any]:
    """诊断用：当前模式会选哪个通道、等多久、配置有没有写错。**不提问**。"""
    normalized = str(mode or "off").strip().lower()
    raw = str(_pick(settings, "approval_channel", "") or "").strip().lower()
    channel = resolve_channel(normalized, settings, stdin=stdin)
    return {
        "mode": normalized if normalized in APPROVAL_MODES else "off",
        "needs_channel": channel is not None,
        "channel": channel.name if channel is not None else "",
        "configured": raw,
        "configured_known": raw in CHANNEL_KINDS if raw else True,
        "timeout": channel_timeout(settings),
        "directory": str(channel_directory(settings)) if raw == "file" else "",
        "note": (
            "审批模式 off 不需要交互通道"
            if channel is None
            else "超时 = 拒绝，且原因会写清是超时不是拒绝"
        ),
    }


# ── 接线用的唯一入口 ────────────────────────────────────────────────────────


def approve_or_refuse(
    command: str,
    *,
    mode: str,
    settings: Any = None,
    task_id: str = "",
    reason: str = "",
    channel: ApprovalChannel | None = None,
    queue: ApprovalQueue | None = None,
) -> Decision:
    """`classify` 判出 `ask` 之后调这一个函数。返回 :class:`Decision`。

    行为矩阵（全在这里，接线方不用再判）：

    - `mode=off` → allow（分类层已经放行了，不该在这里再拦一次）
    - `mode=smart|manual` 且没有通道 → allow（不该发生；`resolve_channel` 至少给
      `AutoDenyChannel`，这里只是防御）
    - 有通道 → `ask`，并把请求登记进队列、把结果回写队列

    队列的读写**全部容错**：磁盘写不进去时照常返回通道的裁决——队列是审计凭据，
    不是权限来源，它的失败不该把"人已经点了允许"变成"拒绝"。
    """
    normalized = str(mode or "off").strip().lower()
    if normalized not in APPROVAL_MODES:
        normalized = "off"
    if normalized == "off":
        return Decision.allow(
            by="mode-off",
            reason="审批模式 off：不做交互确认（分类层已经放行）。",
        )
    active = channel if channel is not None else resolve_channel(normalized, settings)
    if active is None:
        return Decision.allow(
            by="no-channel",
            reason="没有可用的审批通道，按放行处理（分类层已经放行）。",
        )

    request = make_request(command, task_id=task_id, reason=reason, mode=normalized)
    store = queue
    if store is None:
        data_dir = getattr(settings, "data_dir", None)
        if data_dir is not None:
            try:
                store = ApprovalQueue.load(data_dir)
            except OSError:
                store = None
    if store is not None:
        try:
            store.submit(request)
        except OSError:
            store = None

    decision = active.ask(request)

    if store is not None:
        try:
            if decision.timed_out:
                store.expire_one(request.id, note=decision.reason)
            else:
                store.decide(request.id, decision.allowed, note=decision.note or decision.reason)
        except OSError:
            pass
    return decision
