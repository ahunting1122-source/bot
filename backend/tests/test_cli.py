"""CLI 命令面的契约测试。

只测"命令面"本身：注册表是否自洽、帮助能否生成、命令能否在隔离数据目录上跑通、
``--json`` 是否真的是机器可读。**不测**每条命令的业务细节——那些由各功能域自己的
测试覆盖，混在一起会让这里的失败难以定位。
"""

from __future__ import annotations

import json

import pytest

from app.cli.main import build_parser, main
from app.cli.spec import GROUPS, PARITY_LEVELS, PARITY_STATES, REGISTRY


def test_registry_is_self_consistent():
    """注册表自洽：名字唯一、归类/优先级/状态都在枚举里、子命令不重名。"""
    names = [command.name for command in REGISTRY.all()]
    assert len(names) == len(set(names)), f"命令名重复：{names}"
    assert names, "注册表为空——commands 包没被导入？"
    for command in REGISTRY.all():
        assert command.group in GROUPS, command.name
        assert command.parity in PARITY_LEVELS, command.name
        assert command.state in PARITY_STATES, command.name
        subs = [sub.name for sub in command.subcommands]
        assert len(subs) == len(set(subs)), f"{command.name} 子命令重复"
        if command.handler is None:
            assert command.subcommands, f"{command.name} 既无 handler 也无子命令"


def test_every_command_has_help():
    for command in REGISTRY.all():
        assert command.help.strip(), f"{command.name} 缺 help——--help 会显示空白"
        for sub in command.subcommands:
            assert sub.help.strip(), f"{command.name} {sub.name} 缺 help"


def test_parser_builds_and_lists_commands():
    parser = build_parser()
    help_text = parser.format_help()
    for command in REGISTRY.all():
        assert command.name in help_text


def test_parity_rows_cover_subcommands():
    rows = REGISTRY.parity_rows()
    paths = {row["command"] for row in rows}
    assert "config" in paths
    assert "config get" in paths
    assert all(row["hermes"] for row in rows)


def test_no_arguments_prints_help(capsys):
    assert main([]) == 1
    assert "orbit" in capsys.readouterr().out


def test_unknown_command_exits_2():
    with pytest.raises(SystemExit) as excinfo:
        main(["definitely-not-a-command"])
    assert excinfo.value.code == 2


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])
    assert excinfo.value.code == 0
    assert "orbit" in capsys.readouterr().out


def test_status_json_is_machine_readable(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "--json", "status"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["data_dir"].endswith("data")


def test_doctor_json_reports_checks(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "--json", "doctor"])
    payload = json.loads(capsys.readouterr().out)
    names = {check["name"] for check in payload["checks"]}
    assert "Python 版本" in names
    assert "数据库" in names
    # 空数据目录下数据库检查应是"注意"而不是"失败"：没建库不算故障。
    assert code in (0, 1)


def test_config_round_trip_on_runtime_key(tmp_path, capsys):
    data_dir = str(tmp_path / "data")
    assert main(["--data-dir", data_dir, "config", "set", "browser_url", "https://example.com"]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "config", "get", "browser_url"]) == 0
    assert "https://example.com" in capsys.readouterr().out
    assert main(["--data-dir", data_dir, "config", "unset", "browser_url"]) == 0
    capsys.readouterr()
    assert main(["--data-dir", data_dir, "config", "get", "browser_url"]) == 0
    assert "https://example.com" not in capsys.readouterr().out


def test_config_rejects_secret_on_command_line(tmp_path, capsys):
    """密钥不走命令行明文——它会进 shell 历史。"""
    code = main(["--data-dir", str(tmp_path / "data"), "config", "set", "model_api_key", "sk-x"])
    assert code == 1
    assert "keyring" in capsys.readouterr().err


def test_config_unknown_key(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "config", "get", "nope"])
    assert code == 1
    assert "未知配置键" in capsys.readouterr().err


def test_completion_scripts_mention_orbit(capsys):
    for shell in ("bash", "zsh", "fish", "powershell"):
        assert main(["completion", shell]) == 0
        assert "orbit" in capsys.readouterr().out


def test_prompt_size_counts_tools(capsys):
    assert main(["--json", "prompt-size"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["system_bytes"] > 0
    assert payload["tool_bytes"] > 0
    assert len(payload["tools"]) >= 20


def test_logs_without_files_is_not_an_error(tmp_path, capsys):
    code = main(["--data-dir", str(tmp_path / "data"), "logs"])
    assert code == 0
    assert "没有 .log" in capsys.readouterr().out


# ── 命令面覆盖度 ────────────────────────────────────────────────────────────

#: Hermes 的顶层独立命令数（74 个名字含 2 组别名 → 77 个名字）。
#: 依据 `handoff/hermes-parity/research/01-cli-surface.md` §0。
HERMES_TOP_LEVEL = 74


def test_top_level_surface_covers_hermes():
    """顶层命令面必须覆盖 Hermes 的全部独立命令。

    这条测试是"命令面即功能面"的门禁：少一个命令就等于少一块功能，
    而 `--help` 里看不见的东西，没人会想起来补。
    """
    names = {command.name for command in REGISTRY.all()}
    assert len(names) >= HERMES_TOP_LEVEL, f"只有 {len(names)} 条顶层命令，少于 Hermes 的 {HERMES_TOP_LEVEL}"


def test_every_command_help_renders():
    """每条命令（含子命令）的 `--help` 都必须能渲染。

    比"命令登记了"更强的一层保证：登记了但 `--help` 崩掉，等于没有。
    """
    parser = build_parser()
    for command in REGISTRY.all():
        with pytest.raises(SystemExit) as excinfo:
            parser.parse_args([command.name, "--help"])
        assert excinfo.value.code == 0, command.name


def test_option_help_is_documented():
    """所有选项都要有中文说明。

    `--help` 里只有 `--source SOURCE` 这种光秃秃的选项，等于抄了命令名没抄语义——
    这是本项目实际踩过的坑（884 条选项一开始全是空的）。
    """
    from app.cli.commands._table import undocumented_options
    from app.cli.commands.hermes_parity import SPECS

    missing = undocumented_options(SPECS)
    assert missing == [], f"{len(missing)} 条选项缺说明，例如：{missing[:10]}"
