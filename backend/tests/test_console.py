"""`app/console.py`（安全命令台）与 `app/cli/commands/ui.py`（UI 命令面）的契约测试。

本文件承担两块：

1. **安全命令台**——重点是"拒绝得对不对"，不是"跑得通不通"：
   白名单外的命令必须有理由地拒、shell 注入面（`;` `|` `&&` `$()` 反引号）必须
   一律拒、每次执行（含被拒）都必须落审计。
2. **UI 命令面**——`serve` 的**参数拼装纯函数** `serve_plan` 有独立单测，
   于是"起服务"这件事不需要真起服务（本机无网络，单测也不该占端口）。
   为什么这些 CLI 用例放在 `test_console.py` 而不是 `test_ui.py`：本轮只允许新建
   `test_metrics.py` / `test_console.py` / `test_skin.py` 三个测试文件，没有
   `test_ui.py` 的位置；`serve`/`dashboard` 与 `console` 同属"UI 命令面"，
   放一起比塞进 skin/metrics 更贴题。
"""

from __future__ import annotations

import json
import socket

import pytest

import app.cli.commands.ui as ui
from app.cli.main import main
from app.config import Settings
from app.console import ALLOWED, ConsolePolicy, audit_path, execute, read_audit

# ── 白名单 ──────────────────────────────────────────────────────────────────


def test_whitelist_contains_only_read_only_commands():
    """白名单里不许出现"会改机器状态"的命令——这是整个控制台的安全边界。"""
    forbidden = {
        "chat",
        "setup",
        "serve",
        "dashboard",
        "backup",
        "restore",
        "cron run",
        "sessions prune",
        "config set",
        "config unset",
        "skills install",
        "plugins install",
    }
    assert forbidden.isdisjoint(set(ALLOWED)), f"白名单混进了写命令：{forbidden & set(ALLOWED)}"
    assert len(ALLOWED) >= 6, "白名单太窄，远程操作会没法用"


@pytest.mark.parametrize(
    "argv",
    [
        ["orbit", "status"],
        ["orbit", "doctor"],
        ["orbit", "config", "show"],
        ["orbit", "config", "get", "browser_url"],
        ["orbit", "logs"],
        ["orbit", "insights", "--days", "7"],
        ["orbit", "monitoring", "status"],
        ["orbit", "monitoring", "metrics"],
        ["orbit", "skin", "list"],
        ["status"],  # 不带 orbit 前缀也认
    ],
)
def test_whitelisted_commands_are_allowed(argv):
    allowed, reason = ConsolePolicy.default().check(argv)
    assert allowed is True, f"{argv} 应放行，理由：{reason}"
    assert "白名单" in reason


@pytest.mark.parametrize(
    "argv",
    [
        ["orbit", "chat", "你好"],
        ["orbit", "config", "set", "model_api_key", "sk-x"],
        ["orbit", "serve"],
        ["orbit", "sessions", "prune"],
        ["orbit", "rm"],
    ],
)
def test_non_whitelisted_commands_are_refused_with_reason(argv):
    allowed, reason = ConsolePolicy.default().check(argv)
    assert allowed is False
    assert "不在白名单" in reason
    # 理由要能指导人：列出可用命令，而不是一句"拒绝"。
    assert "orbit status" in reason


# ── shell 注入面 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        "orbit status; rm -rf /",
        "orbit status | more",
        "orbit status && rm -rf /",
        "orbit status $(whoami)",
        "orbit status `whoami`",
        "orbit status > /etc/passwd",
        "orbit status\nrm -rf /",
        "orbit status || true",
    ],
)
def test_shell_injection_payloads_are_refused(payload):
    """注入面**先于**白名单判定：`status; rm -rf /` 不能因为前缀是 status 就放行。

    传**原文**（远程消息的真实形态）而不是先 `.split()` 的列表：换行这类分隔符
    一分词就消失，先分词再检查会漏掉 ``status\\nrm -rf /``。
    """
    allowed, reason = ConsolePolicy.default().check(payload)
    assert allowed is False
    assert "注入面" in reason, f"拒绝理由应指向注入面，实际：{reason}"


def test_injection_in_single_token_is_refused():
    """整条字符串当 token 传进来（远程消息最常见）也必须被拒。"""
    allowed, reason = ConsolePolicy.default().check(["orbit status; whoami"])
    assert allowed is False
    assert "注入面" in reason


def test_empty_command_is_refused():
    allowed, reason = ConsolePolicy.default().check([])
    assert allowed is False
    assert "空命令" in reason


def test_help_and_version_flags_are_allowed():
    for flag in ("--help", "--version"):
        allowed, _ = ConsolePolicy.default().check([flag])
        assert allowed is True


# ── 临时扩白名单 ────────────────────────────────────────────────────────────


def test_allow_extends_whitelist_for_this_session():
    """`--allow` 只扩**本次会话**，而且默认策略不受影响（不可变、无全局副作用）。"""
    policy = ConsolePolicy.default().with_extra("orbit sessions list")
    allowed, reason = policy.check(["orbit", "sessions", "list"])
    assert allowed is True
    assert "orbit sessions list" in reason
    assert ConsolePolicy.default().check(["orbit", "sessions", "list"])[0] is False


def test_allow_refuses_to_smuggle_shell_meta():
    policy = ConsolePolicy.default().with_extra("orbit status; rm -rf /")
    assert "orbit status; rm -rf /" not in policy.entries()
    assert policy.check(["orbit", "status; rm -rf /"])[0] is False


# ── 执行 + 审计 ─────────────────────────────────────────────────────────────


def test_execute_runs_whitelisted_command(tmp_path):
    result = execute(["orbit", "status"], data_dir=tmp_path)
    assert result.allowed is True
    assert result.code == 0
    assert "Orbit 状态" in result.output


def test_execute_refuses_and_does_not_run(tmp_path):
    result = execute(["orbit", "chat", "你好"], data_dir=tmp_path)
    assert result.allowed is False
    assert result.code == 126
    assert result.output == ""


def test_audit_records_both_allowed_and_denied(tmp_path):
    execute(["orbit", "status"], data_dir=tmp_path)
    execute(["orbit", "chat", "你好"], data_dir=tmp_path)
    execute(["orbit", "status; whoami"], data_dir=tmp_path)
    path = audit_path(tmp_path)
    assert path.exists(), "审计文件没写出来"
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 3, "拒绝也要记审计——只记成功的审计会抹掉攻击痕迹"
    assert [record["allowed"] for record in records] == [True, False, False]
    for record in records:
        assert set(record) == {"ts", "argv", "allowed", "reason"}
        assert record["ts"]
        assert record["reason"]
    assert records[1]["argv"] == ["chat", "你好"]
    assert read_audit(tmp_path)[-1]["allowed"] is False


def test_execute_never_uses_a_shell():
    """结构性断言：源码里不许出现 shell 执行面（列表形式直调 CLI 的 main）。"""
    import app.console as console_module

    source = console_module.execute.__module__
    assert source == "app.console"
    text = open(console_module.__file__, encoding="utf-8").read()
    for banned in ("shell=True", "os.system(", "os.popen(", "subprocess.call("):
        assert banned not in text, f"控制台里出现了 shell 执行面：{banned}"


# ── CLI：console ────────────────────────────────────────────────────────────


def test_cli_console_exec_runs_whitelisted(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path), "console", "--exec", "orbit status"])
    assert code == 0
    assert "Orbit 状态" in capsys.readouterr().out


def test_cli_console_exec_denied_returns_126(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path), "console", "--exec", "orbit chat 你好"])
    assert code == 126
    assert "不在白名单" in capsys.readouterr().err


def test_cli_console_allow_adds_entry(tmp_path, capsys):
    code = main(
        [
            "--data-dir",
            str(tmp_path),
            "console",
            "--allow",
            "orbit skin list",
            "--exec",
            "orbit skin list",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "default" in out and "solarized" in out


def test_cli_console_non_interactive_prints_whitelist(tmp_path, capsys):
    """stdin 不是终端（管道 / 远程桥 / 测试）时不读 stdin，只把策略打出来。"""
    code = main(["--data-dir", str(tmp_path), "console"])
    assert code == 0
    out = capsys.readouterr().out
    assert "白名单" in out
    assert "orbit status" in out
    assert "不是终端" in out


# ── serve：参数拼装纯函数 ───────────────────────────────────────────────────


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_serve_plan_shape_and_defaults():
    plan = ui.serve_plan("127.0.0.1", 9119, probe=False)
    assert plan["host"] == "127.0.0.1"
    assert plan["port"] == 9119
    assert plan["auto_port"] is False
    assert plan["url"] == "http://127.0.0.1:9119/"
    assert plan["target"] == "app.main:app"
    assert plan["loopback"] is True
    assert plan["available"] is True
    assert plan["reason"] == ""
    assert plan["uvicorn"] == {"host": "127.0.0.1", "port": 9119, "log_level": "info"}


def test_serve_plan_reports_free_port_available():
    plan = ui.serve_plan("127.0.0.1", _free_port())
    assert plan["available"] is True
    assert plan["reason"] == ""


def test_serve_plan_detects_occupied_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = int(busy.getsockname()[1])
        plan = ui.serve_plan("127.0.0.1", port)
    assert plan["available"] is False
    assert "已被占用" in plan["reason"]
    assert str(port) in plan["reason"]


def test_serve_plan_port_zero_means_auto_assign():
    plan = ui.serve_plan("127.0.0.1", 0, probe=False)
    assert plan["auto_port"] is True
    assert "系统分配" in plan["url"]
    assert plan["uvicorn"]["port"] == 0


def test_serve_plan_insecure_is_a_noop_with_notice():
    """Hermes 的 `--insecure` 是 NO-OP，Orbit 照抄并**如实提示**。"""
    plan = ui.serve_plan("127.0.0.1", 9119, insecure=True, probe=False)
    notices = " ".join(plan["notices"])
    assert "无效果" in notices
    assert "--insecure" in notices
    # NO-OP 就是"不改任何参数"：uvicorn 的 kwargs 与不带该 flag 时完全一致。
    assert plan["uvicorn"] == ui.serve_plan("127.0.0.1", 9119, probe=False)["uvicorn"]


def test_serve_plan_warns_on_public_bind():
    plan = ui.serve_plan("0.0.0.0", 9119, probe=False)
    assert plan["loopback"] is False
    notices = " ".join(plan["notices"])
    assert "403" in notices, "非回环绑定会被 local_session 中间件挡掉，必须提前说明"


def test_serve_plan_skip_build_and_isolated_notices():
    notices = " ".join(
        ui.serve_plan("127.0.0.1", 9119, skip_build=True, isolated=True, probe=False)["notices"]
    )
    assert "skip-build" in notices
    assert "isolated" in notices


def test_cli_serve_help_lists_options(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["serve", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "--port" in out and "--host" in out and "--insecure" in out


# ── dashboard ──────────────────────────────────────────────────────────────


def test_cli_dashboard_insecure_says_noop_and_hints_serve(tmp_path, capsys, monkeypatch):
    """`dashboard --insecure` 必须如实说"该 flag 无效果"，并提示先起 serve。"""
    opened: list[str] = []
    monkeypatch.setattr(ui.webbrowser, "open", lambda url: opened.append(url) or True)
    code = main(
        ["--data-dir", str(tmp_path), "dashboard", "--insecure", "--port", str(_free_port())]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "无效果" in captured.out
    assert "orbit serve" in captured.out
    assert opened == [], "面板没在监听时不该打开浏览器（会打开一个连不上的页面）"


def test_cli_dashboard_opens_browser_when_port_is_listening(tmp_path, capsys, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(ui.webbrowser, "open", lambda url: opened.append(url) or True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = int(busy.getsockname()[1])
        code = main(["--data-dir", str(tmp_path), "dashboard", "--port", str(port)])
    assert code == 0
    assert opened == [f"http://127.0.0.1:{port}/"]
    assert "已在浏览器打开" in capsys.readouterr().out


def test_cli_dashboard_no_open_does_not_open(tmp_path, capsys, monkeypatch):
    opened: list[str] = []
    monkeypatch.setattr(ui.webbrowser, "open", lambda url: opened.append(url) or True)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen(1)
        port = int(busy.getsockname()[1])
        code = main(["--data-dir", str(tmp_path), "dashboard", "--no-open", "--port", str(port)])
    assert code == 0
    assert opened == []


def test_cli_serve_status_does_not_start_server(tmp_path, capsys):
    """`--status` 只报告端口，不起服务（这条用例能在单测里跑就是证据）。"""
    code = main(["--data-dir", str(tmp_path), "serve", "--status", "--port", str(_free_port())])
    assert code == 0
    out = capsys.readouterr().out
    assert "端口状态" in out
    assert "进程登记表" in out


def test_cli_serve_stop_refuses_honestly(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path), "serve", "--stop"])
    assert code == 3
    assert "未实现" in capsys.readouterr().err


def test_skin_show_and_console_are_wired_into_the_table():
    """接线断言：表里的路径确实被本模块接管（state=done 且有 handler）。"""
    from app.cli.spec import REGISTRY

    for path in ("serve", "dashboard", "console"):
        command = REGISTRY.get(path)
        assert command is not None and command.handler is not None, path
        assert command.state == "done", path
    skin = REGISTRY.get("skin")
    assert skin is not None
    for name in ("list", "use", "set", "show", "reset"):
        child = skin.child(name)
        assert child is not None and child.handler is not None, name
    monitoring = REGISTRY.get("monitoring")
    assert monitoring is not None and monitoring.child("metrics") is not None


def test_settings_data_dir_is_used_for_audit(tmp_path):
    """控制台的审计必须落在**当前数据目录**上，而不是默认仓库目录。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    execute(["orbit", "status"], settings=settings)
    assert (tmp_path / "data" / "console-audit.jsonl").exists()
