"""`app/approval_channel.py` 的契约测试（MATRIX 14.1）。

四组：

1. **三态各一条**：`off` 不需要通道；`smart` / `manual` 需要有通道（且默认是
   **拒绝**的 `auto-deny`，不是放行）。
2. **无 TTY 不挂死**：`ConsoleChannel` 在非交互环境里必须**超时返回**。这条是
   本模块最重要的性质——挂死看起来像"agent 卡了"，EOF 看起来像"用户拒绝了"，
   两者都会把排查方向带偏。
3. **超时 ≠ 拒绝**：两种"不放行"的 `outcome` 与 `reason` 都能区分。混成一句话，
   用户会以为自己点过拒绝，然后去翻一条并不存在的操作记录。
4. **FileChannel 往返**：写请求 → 别人写响应 → 读到裁决；以及没人回答时的超时。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import app.cli.commands.security_extra  # noqa: F401 —— 导入即挂上 security / approvals 的新子命令
from app import approval_channel
from app.approval_channel import (
    ALLOW,
    DENY,
    EXPIRED,
    TIMEOUT,
    ApprovalQueue,
    ApprovalRequest,
    AutoDenyChannel,
    ConsoleChannel,
    Decision,
    FileChannel,
    approve_or_refuse,
    make_request,
    resolve_channel,
)
from app.cli.main import main


class _FakeStdin:
    """假 stdin。`isatty` 可控，`readline` 可以返回一行、也可以**永久阻塞**。"""

    def __init__(self, line: str = "", *, tty: bool = True, block: threading.Event | None = None):
        self._line = line
        self._tty = tty
        self._block = block

    def isatty(self) -> bool:
        return self._tty

    def readline(self) -> str:
        if self._block is not None:
            self._block.wait(10)  # 模拟"人一直没敲键盘"
        return self._line


class _FakeResolver:
    """假 settings：只有 `data_dir` 与几个运行时字段。"""

    def __init__(self, data_dir: Path, **fields):
        self.data_dir = data_dir
        for key, value in fields.items():
            setattr(self, key, value)


@pytest.fixture()
def data_dir(tmp_path) -> Path:
    path = tmp_path / "data"
    path.mkdir()
    return path


# ── 1. 三态 ────────────────────────────────────────────────────────────────


def test_off_mode_needs_no_channel(data_dir):
    """`off` 不提问——分类层已经放行了，这里再问一次只会打扰人。"""
    settings = _FakeResolver(data_dir)
    assert resolve_channel("off", settings) is None
    decision = approve_or_refuse("rm -rf build", mode="off", settings=settings)
    assert decision.outcome == ALLOW
    assert decision.decided_by == "mode-off"
    # 没有提问，就不该在队列里留下条目
    assert ApprovalQueue.load(data_dir).entries == []


def test_smart_mode_resolves_a_channel_that_defaults_to_deny(data_dir):
    """没有终端时 `smart` 默认落到 `auto-deny`：**无人值守 = 拒绝**。"""
    settings = _FakeResolver(data_dir)
    channel = resolve_channel("smart", settings, stdin=_FakeStdin(tty=False))
    assert channel is not None
    assert channel.name == "auto-deny"
    decision = approve_or_refuse("rm -rf build", mode="smart", settings=settings, channel=channel)
    assert decision.outcome == DENY
    assert "无人值守" in decision.reason


def test_manual_mode_also_resolves_a_channel(data_dir):
    settings = _FakeResolver(data_dir)
    channel = resolve_channel("manual", settings, stdin=_FakeStdin(tty=False))
    assert channel is not None
    assert channel.name == "auto-deny"


def test_unknown_mode_falls_back_to_off(data_dir):
    """非法模式一律退回 `off`（与 `app/approvals.py::resolve_mode` 同一条）。"""
    assert resolve_channel("yolo-ish", _FakeResolver(data_dir)) is None


def test_unknown_channel_name_fails_closed(data_dir):
    settings = _FakeResolver(data_dir, approval_channel="telepathy")
    channel = resolve_channel("smart", settings, stdin=_FakeStdin(tty=False))
    assert channel is not None and channel.name == "auto-deny"
    info = approval_channel.describe_channel("smart", settings)
    assert info["configured_known"] is False


def test_configured_channel_overrides_the_default(data_dir):
    settings = _FakeResolver(data_dir, approval_channel="file")
    channel = resolve_channel("smart", settings, stdin=_FakeStdin(tty=True))
    assert isinstance(channel, FileChannel)
    # 目录默认落在 `<data>/approvals/channel`
    assert channel.directory == data_dir / "approvals" / "channel"


# ── 2. 无 TTY 不挂死 ───────────────────────────────────────────────────────


def test_console_channel_without_a_tty_times_out_instead_of_hanging():
    """非交互环境必须**立刻**返回超时。挂死等于把 agent 卡住。"""
    channel = ConsoleChannel(timeout=30.0, stdin=_FakeStdin(tty=False))
    started = time.monotonic()
    decision = channel.ask(make_request("rm -rf build"))
    elapsed = time.monotonic() - started
    assert decision.outcome == TIMEOUT
    assert elapsed < 1.0, f"没有 TTY 却等了 {elapsed:.2f} 秒，说明它没走快速返回路径"
    assert "没有可交互的终端" in decision.reason


def test_console_channel_times_out_when_nobody_answers():
    """有 TTY 但人一直不敲键盘：等满 timeout 之后返回超时，而不是永远阻塞。"""
    block = threading.Event()
    channel = ConsoleChannel(timeout=0.2, stdin=_FakeStdin(block=block), stdout=_Sink())
    started = time.monotonic()
    decision = channel.ask(make_request("rm -rf build"))
    elapsed = time.monotonic() - started
    block.set()  # 放掉那条后台线程，别让它挂到用例结束
    assert decision.outcome == TIMEOUT
    assert 0.15 <= elapsed < 3.0, f"等待时长不合理：{elapsed:.2f}s"


class _Sink:
    """吞掉提示语（不污染 pytest 的输出捕获）。"""

    def write(self, _text: str) -> None:
        return None

    def flush(self) -> None:
        return None


def test_console_channel_reads_an_answer():
    out = _Sink()
    allow = ConsoleChannel(stdin=_FakeStdin("y\n"), stdout=out).ask(make_request("ls"))
    assert allow.outcome == ALLOW
    deny = ConsoleChannel(stdin=_FakeStdin("n\n"), stdout=out).ask(make_request("ls"))
    assert deny.outcome == DENY
    # 空行与看不懂的回答都必须落到拒绝（默认必须是"不放行"）
    empty = ConsoleChannel(stdin=_FakeStdin("\n"), stdout=out).ask(make_request("ls"))
    assert empty.outcome == DENY
    weird = ConsoleChannel(stdin=_FakeStdin("maybe\n"), stdout=out).ask(make_request("ls"))
    assert weird.outcome == DENY and "不是允许" in weird.reason


def test_prompt_shows_the_command_and_the_timeout():
    channel = ConsoleChannel(timeout=7.5)
    text = channel.prompt_for(make_request("rm -rf build", reason="命中 rm_recursive"))
    assert "rm -rf build" in text
    assert "rm_recursive" in text
    assert "7.5" in text


# ── 3. 超时 ≠ 拒绝 ─────────────────────────────────────────────────────────


def test_timeout_and_deny_are_distinguishable():
    """这是本模块最容易做错的一处：混成一句话，用户会以为是自己点了拒绝。"""
    timed_out = Decision.timed_out_at(12.0, by="console")
    denied = Decision.deny(by="console")
    assert timed_out.outcome == TIMEOUT
    assert denied.outcome == DENY
    assert timed_out.outcome != denied.outcome
    assert "超时" in timed_out.reason
    assert "不是你点了拒绝" in timed_out.reason
    assert "12" in timed_out.reason
    assert "拒绝" in denied.reason
    assert timed_out.reason != denied.reason
    # 两者都"不放行"，但原因必须不同
    assert timed_out.allowed is False and denied.allowed is False
    assert timed_out.as_dict()["timed_out"] is True
    assert denied.as_dict()["timed_out"] is False


def test_auto_deny_reason_is_not_a_human_refusal():
    decision = AutoDenyChannel().ask(make_request("rm -rf build"))
    assert decision.outcome == DENY
    assert "无人值守" in decision.reason
    assert "不是人工否决" in decision.reason


# ── 4. FileChannel 往返 ────────────────────────────────────────────────────


def test_file_channel_round_trip(tmp_path):
    """写请求 → 别人写响应 → 读到裁决。这条给 gateway / cron 这种没有终端的地方用。"""
    directory = tmp_path / "channel"
    channel = FileChannel(directory, timeout=10.0, poll_interval=0.01)
    request = make_request("rm -rf build", reason="需要确认")
    result: dict[str, Decision] = {}

    def ask() -> None:
        result["decision"] = channel.ask(request)

    worker = threading.Thread(target=ask, daemon=True)
    worker.start()
    # 等请求文件出现，再"回答"
    deadline = time.monotonic() + 5
    while not channel.request_path(request.id).is_file() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert channel.request_path(request.id).is_file(), "请求文件没有落盘"
    stored = json.loads(channel.request_path(request.id).read_text(encoding="utf-8"))
    assert stored["command"] == "rm -rf build"
    FileChannel.respond(directory, request.id, True, note="我确认过了")
    worker.join(5)
    assert not worker.is_alive()
    decision = result["decision"]
    assert decision.outcome == ALLOW
    assert decision.note == "我确认过了"
    # 响应文件保留下来：它是"谁在什么时候答的"的唯一证据
    assert channel.response_path(request.id).is_file()


def test_file_channel_times_out_when_nobody_answers(tmp_path):
    channel = FileChannel(tmp_path / "channel", timeout=0.15, poll_interval=0.01)
    decision = channel.ask(make_request("rm -rf build"))
    assert decision.outcome == TIMEOUT
    assert "超时" in decision.reason


def test_file_channel_reports_a_corrupt_response_as_still_waiting(tmp_path):
    """半截/坏响应**不猜结论**：继续等，等到超时为止。"""
    directory = tmp_path / "channel"
    directory.mkdir(parents=True)
    request = make_request("ls")
    channel = FileChannel(directory, timeout=0.15, poll_interval=0.01)
    channel.response_path(request.id).write_text('{"approved": "maybe"}', encoding="utf-8")
    decision = channel.ask(request)
    assert decision.outcome == TIMEOUT


# ── 5. 队列 ────────────────────────────────────────────────────────────────


def test_queue_submit_pending_decide_and_persist(data_dir):
    queue = ApprovalQueue.load(data_dir)
    request = make_request("rm -rf build", task_id="t1", mode="manual")
    entry = queue.submit(request)
    assert entry["status"] == approval_channel.PENDING
    assert [item["id"] for item in queue.pending()] == [request.id]
    assert queue.path == data_dir / "approvals" / "queue.json"
    assert queue.path.is_file()

    # 重新读一遍：队列是跨进程的，落盘必须真的能读回来
    reloaded = ApprovalQueue.load(data_dir)
    assert [item["id"] for item in reloaded.pending()] == [request.id]
    assert reloaded.decide(request.id, True, note="同意") is True
    assert reloaded.pending() == []
    # 已有裁决不覆盖
    assert reloaded.decide(request.id, False) is False
    assert reloaded.get(request.id)["status"] == approval_channel.APPROVED


def test_queue_submit_is_idempotent(data_dir):
    queue = ApprovalQueue.load(data_dir)
    request = make_request("ls")
    queue.submit(request)
    queue.decide(request.id, True)
    queue.submit(request)  # 重复提交不能把已有裁决冲掉
    assert queue.get(request.id)["status"] == approval_channel.APPROVED


def test_queue_expire_marks_timeout_not_denied(data_dir):
    """超时与拒绝分开记：`expired` 不是 `denied`。"""
    queue = ApprovalQueue.load(data_dir)
    request = make_request("ls")
    queue.submit(request)
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()
    queue.get(request.id)["created_at"] = old
    queue._save()  # noqa: SLF001 - 故意把时间改旧，模拟"等了两小时"
    expired = queue.expire(60)
    assert expired == [request.id]
    assert queue.get(request.id)["status"] == EXPIRED
    assert "不是拒绝" in queue.get(request.id)["note"]
    # 已裁决的不会被标超时
    assert queue.expire(60) == []


def test_queue_writes_atomically(data_dir):
    queue = ApprovalQueue.load(data_dir)
    queue.submit(make_request("ls"))
    leftovers = list((data_dir / "approvals").glob("*.tmp"))
    assert leftovers == [], f"留下了临时文件：{leftovers}"
    json.loads(queue.path.read_text(encoding="utf-8"))  # 必须是完整 JSON


def test_approve_or_refuse_records_the_decision(data_dir):
    settings = _FakeResolver(data_dir)
    channel = AutoDenyChannel()
    decision = approve_or_refuse(
        "rm -rf build", mode="smart", settings=settings, channel=channel, task_id="t9"
    )
    assert decision.outcome == DENY
    queue = ApprovalQueue.load(data_dir)
    entries = queue.entries
    assert len(entries) == 1
    assert entries[0]["command"] == "rm -rf build"
    assert entries[0]["task_id"] == "t9"
    assert entries[0]["status"] == approval_channel.DENIED


def test_approve_or_refuse_records_timeouts_as_expired(data_dir):
    settings = _FakeResolver(data_dir)
    channel = ConsoleChannel(timeout=0.05, stdin=_FakeStdin(tty=False))
    decision = approve_or_refuse("rm -rf build", mode="manual", settings=settings, channel=channel)
    assert decision.outcome == TIMEOUT
    entries = ApprovalQueue.load(data_dir).entries
    assert entries[0]["status"] == EXPIRED


def test_request_round_trips_through_a_dict():
    request = make_request("ls -la", task_id="t1", reason="r", mode="manual", identifier="abc123")
    restored = ApprovalRequest.from_dict(request.as_dict())
    assert restored == request
    assert ApprovalRequest.from_dict({"command": "x"}) is None


def test_describe_channel_explains_the_timeout_semantics(data_dir):
    info = approval_channel.describe_channel("smart", _FakeResolver(data_dir))
    assert info["needs_channel"] is True
    assert info["channel"] in {"console", "auto-deny"}
    assert "超时" in info["note"]
    off = approval_channel.describe_channel("off", _FakeResolver(data_dir))
    assert off["needs_channel"] is False


# ── 6. CLI：每条新命令一条 ─────────────────────────────────────────────────


def _data_dir(tmp_path) -> str:
    path = tmp_path / "data"
    path.mkdir(exist_ok=True)
    return str(path)


def test_cli_approvals_channel_reports_the_choice(tmp_path, capsys):
    code = main(["--data-dir", _data_dir(tmp_path), "approvals", "channel"])
    out = capsys.readouterr().out
    assert code == 0
    assert "审批模式" in out
    assert "超时 = 拒绝" in out


def test_cli_approvals_queue_lists_pending(tmp_path, capsys):
    data = Path(_data_dir(tmp_path))
    ApprovalQueue.load(data).submit(make_request("rm -rf build", mode="manual"))
    code = main(["--data-dir", str(data), "approvals", "queue"])
    out = capsys.readouterr().out
    assert code == 0
    assert "rm -rf build" in out
    assert "pending" in out


def test_cli_approvals_queue_shows_nothing_when_empty(tmp_path, capsys):
    assert main(["--data-dir", _data_dir(tmp_path), "approvals", "queue"]) == 0
    assert "没有待审批项" in capsys.readouterr().out


def test_cli_approvals_decide_approves(tmp_path, capsys):
    data = Path(_data_dir(tmp_path))
    request = make_request("rm -rf build")
    ApprovalQueue.load(data).submit(request)
    code = main(
        ["--data-dir", str(data), "approvals", "decide", request.id, "--approve", "--note", "同意"]
    )
    assert code == 0
    assert "approved" in capsys.readouterr().out
    assert ApprovalQueue.load(data).get(request.id)["status"] == approval_channel.APPROVED


def test_cli_approvals_decide_requires_exactly_one_flag(tmp_path):
    data = Path(_data_dir(tmp_path))
    request = make_request("ls")
    ApprovalQueue.load(data).submit(request)
    assert main(["--data-dir", str(data), "approvals", "decide", request.id]) == 1
    assert (
        main(["--data-dir", str(data), "approvals", "decide", request.id, "--approve", "--deny"]) == 1
    )


def test_cli_approvals_decide_reports_an_unknown_id(tmp_path, capsys):
    code = main(["--data-dir", _data_dir(tmp_path), "approvals", "decide", "nope", "--deny"])
    assert code == 1
    assert "没有这条" in capsys.readouterr().err


def test_cli_approvals_expire_marks_timeouts(tmp_path, capsys):
    data = Path(_data_dir(tmp_path))
    queue = ApprovalQueue.load(data)
    request = make_request("ls")
    queue.submit(request)
    queue.get(request.id)["created_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    queue._save()  # noqa: SLF001
    code = main(["--data-dir", str(data), "approvals", "expire", "--max-age", "60"])
    assert code == 0
    assert "超时（不是拒绝）" in capsys.readouterr().out
    assert ApprovalQueue.load(data).get(request.id)["status"] == EXPIRED
