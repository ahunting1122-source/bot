"""`orbit setup` 的契约测试。

向导最容易出两类错，两类都在下面锁住：

1. **非交互环境里假装交互**：`input()` 拿到 EOF 后用一串默认值把配置写歪。
   所以 stdin 不是 TTY 时必须直接拒绝并给出等价命令。
2. **不实现的 section 静默通过**：跑完显示"完成"，其实什么都没配。
   所以 `tts` / `gateway` / `tools` / `telemetry` 必须明确报"未实现 + 原因"。
"""

from __future__ import annotations

import json

import pytest

import app.cli.commands.setup as setup_cli
from app.cli.main import main


@pytest.fixture
def no_env_model(monkeypatch):
    """清掉 `.env` 带进来的模型配置，模拟真正的首次安装。"""
    from app.config import Settings

    monkeypatch.setattr(Settings, "model", "", raising=False)


def _stub_keyring(monkeypatch):
    stored: dict[str, str] = {}

    class FakeKeyring:
        @staticmethod
        def set_password(service, account, value):
            stored[f"{service}|{account}"] = value

        @staticmethod
        def get_password(service, account):
            return stored.get(f"{service}|{account}")

    monkeypatch.setattr("keyring.set_password", FakeKeyring.set_password)
    monkeypatch.setattr("keyring.get_password", FakeKeyring.get_password)
    return stored


def _feed(monkeypatch, answers: list[str]):
    """把一串回答喂给 input() 与 getpass()；用完后抛 EOFError（说明问的次数超了）。

    `getpass.getpass` **不经过** `builtins.input`（它直接从 stdin 读），所以必须单独
    打桩，否则测试会在 pytest 的输出捕获下抛 "reading from stdin while output is
    captured"。这个坑值得留一条注释：光打 `input` 是不够的。
    """
    queue = list(answers)

    def fake_read(prompt: str = "", *args, **kwargs) -> str:
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr("builtins.input", fake_read)
    monkeypatch.setattr(setup_cli.getpass, "getpass", fake_read)


def test_non_interactive_without_a_model_is_refused(tmp_path, monkeypatch, capsys):
    # 环境变量优先于 .env，把它置空才能模拟"真的没配模型"。
    monkeypatch.setenv("ORBIT_MODEL", "")
    code = main(["--data-dir", str(tmp_path / "data"), "setup", "model", "--non-interactive"])
    assert code == 1
    assert "orbit config set model" in capsys.readouterr().err


def test_interactive_wizard_refuses_when_stdin_is_not_a_tty(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup_cli, "_is_interactive", lambda: False)
    code = main(["--data-dir", str(tmp_path / "data"), "setup"])
    assert code == 1
    # 必须给出等价命令，否则用户卡在这里没有出路。
    assert "--non-interactive" in capsys.readouterr().err


def test_unimplemented_section_reports_a_reason(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "setup", "gateway"])
    assert code == 1
    message = capsys.readouterr().err
    assert "未实现" in message
    assert "W9" in message


def test_portal_is_refused_with_an_alternative(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "setup", "--portal"])
    assert code == 1
    assert "orbit setup model" in capsys.readouterr().err


def test_unknown_section_lists_the_valid_ones(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "setup", "nope"])
    assert code == 1
    assert "model" in capsys.readouterr().err


def test_interactive_model_section_writes_settings_and_keyring(tmp_path, monkeypatch, capsys):
    stored = _stub_keyring(monkeypatch)
    monkeypatch.setattr(setup_cli, "_is_interactive", lambda: True)
    # 供应商 / API 地址 / 模型名 / API Key
    _feed(monkeypatch, ["deepseek", "https://api.example/v1", "demo-model", "sk-test-1234"])

    code = main(["--data-dir", str(tmp_path / "data"), "setup", "model"])
    assert code == 0, capsys.readouterr().err

    settings = json.loads((tmp_path / "data" / "settings.json").read_text(encoding="utf-8"))
    assert settings["model_provider"] == "deepseek"
    assert settings["model_base_url"] == "https://api.example/v1"
    assert settings["model"] == "demo-model"
    # 密钥**不进** settings.json —— 这是硬规矩。
    assert "sk-test-1234" not in json.dumps(settings)
    assert any(value == "sk-test-1234" for value in stored.values())


def test_unknown_provider_is_refused(tmp_path, monkeypatch, capsys):
    _stub_keyring(monkeypatch)
    monkeypatch.setattr(setup_cli, "_is_interactive", lambda: True)
    _feed(monkeypatch, ["不存在的供应商"])
    code = main(["--data-dir", str(tmp_path / "data"), "setup", "model"])
    assert code == 1
    assert "未知供应商" in capsys.readouterr().err


def test_terminal_section_requires_explicit_yes_for_machine_scope(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup_cli, "_is_interactive", lambda: True)
    # 权限级别选 machine，但确认时输入 no → 必须退回 workspace
    _feed(monkeypatch, ["machine", "no", "90"])
    code = main(["--data-dir", str(tmp_path / "data"), "setup", "terminal"])
    assert code == 0, capsys.readouterr().err
    settings = json.loads((tmp_path / "data" / "settings.json").read_text(encoding="utf-8"))
    assert settings["file_scope"] == "workspace"
    assert settings["command_timeout"] == "90"


def test_reset_clears_runtime_settings(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(setup_cli, "_is_interactive", lambda: True)
    data = tmp_path / "data"
    data.mkdir()
    (data / "settings.json").write_text(json.dumps({"model": "old"}), encoding="utf-8")
    _feed(monkeypatch, [])  # 不给任何回答；三个 section 都会走到 EOF，但 --reset 先清空
    main(["--data-dir", str(data), "setup", "--reset", "--non-interactive"])
    settings = json.loads((data / "settings.json").read_text(encoding="utf-8"))
    assert settings == {}
