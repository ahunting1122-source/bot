"""MCP 目录（矩阵 8.10）的契约测试。

三件事必须钉住：**抄全了**（65 条、字段完整或明确 ``unknown``）、**写盘能被既有入口
接受**（``render`` 产出的记录必须过得了 ``app/mcp.py`` 的 ``save_servers`` 校验）、
**不假装装好了**（``install`` 的结果必须带 ``needs_network=True`` 与那句如实说明）。
"""

from __future__ import annotations

import json

import pytest

from app import mcp_catalog
from app.config import Settings
from app.mcp import MCPManager

#: Hermes 0.21.0 的 ``optional-mcps`` 目录里实际有 65 份 manifest。
#: 任务书写的是 63 条——**以真实目录为准**：少列两条等于两个服务"在目录里但装不了"。
CATALOG_SIZE = 65


def _settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


def _run_cli(tmp_path, capsys, *argv):
    """跑一次真 CLI（先导入 mcp_extra：生产接线里由 commands/__init__.py 负责）。"""
    import app.cli.commands.mcp_extra  # noqa: F401 - 导入即接线
    from app.cli.main import main

    code = main(["--data-dir", str(tmp_path / "data"), *argv])
    return code, capsys.readouterr()


# ── 数据完整性 ──────────────────────────────────────────────────────────────


def test_catalog_has_every_manifest():
    assert len(mcp_catalog.CATALOG) == CATALOG_SIZE
    names = [entry.name for entry in mcp_catalog.CATALOG]
    assert len(names) == len(set(names)), "目录里有重名条目"
    assert names == sorted(names), "目录条目应按 name 排序（表格/编号才稳定）"


def test_every_entry_is_complete_or_explicitly_unknown():
    """每条要么字段齐全，要么 ``unknown=True`` 并写清为什么抄不到。"""
    for entry in mcp_catalog.CATALOG:
        assert entry.name and entry.label and entry.description, entry.name
        assert entry.transport in mcp_catalog.TRANSPORTS, entry.name
        assert entry.auth in mcp_catalog.AUTH_TYPES, entry.name
        assert entry.docs.startswith("http"), f"{entry.name} 缺 docs"
        if entry.transport == "http":
            assert entry.url.startswith("http"), f"{entry.name} 远程条目缺 url"
            assert not entry.command, f"{entry.name} 远程条目不该有 command"
        else:
            assert entry.command, f"{entry.name} 本地条目缺 command"
            assert not entry.url, f"{entry.name} 本地条目不该有 url"
        if entry.unknown:
            assert entry.note.strip(), f"{entry.name} 标了 unknown 却没说原因"


def test_no_invented_placeholders_leak_into_known_entries():
    """已知条目里不能留安装期占位符：那说明我们抄了一条自己跑不起来的配置。"""
    for entry in mcp_catalog.CATALOG:
        if entry.unknown:
            continue
        assert "${" not in entry.command, entry.name
        assert all("${" not in arg for arg in entry.args), entry.name


def test_unknown_entries_are_the_install_placeholder_ones():
    unknown = [entry.name for entry in mcp_catalog.CATALOG if entry.unknown]
    assert unknown == ["n8n"], unknown
    entry = mcp_catalog.show("n8n")
    assert entry is not None
    assert "${INSTALL_DIR}" in entry.command
    assert "git 安装" in entry.note


def test_summary_counts_match_the_data():
    stats = mcp_catalog.summary()
    assert stats["count"] == CATALOG_SIZE
    assert stats["http"] + stats["stdio"] == CATALOG_SIZE
    assert stats["unknown"] == 1


def test_label_is_derived_mechanically():
    """label 由 name 机械派生（manifest 里没有 label），规则只有一条。"""
    assert mcp_catalog.label_of("twilio-docs") == "Twilio Docs"
    assert mcp_catalog.label_of("hugging_face") == "Hugging Face"
    assert mcp_catalog.label_of("stripe") == "Stripe"


def test_show_accepts_official_prefix_and_misses_cleanly():
    assert mcp_catalog.show("stripe") is not None
    assert mcp_catalog.show("official/stripe") is not None
    assert mcp_catalog.show("definitely-not-a-service") is None
    assert mcp_catalog.list_catalog()[0]["name"] == "airtable"


def test_catalog_rows_are_json_safe():
    payload = json.dumps(mcp_catalog.list_catalog(), ensure_ascii=False)
    assert '"airtable"' in payload


# ── render ──────────────────────────────────────────────────────────────────


def test_render_remote_entry_produces_writable_record(tmp_path):
    config = mcp_catalog.render("stripe")
    assert config.name == "stripe"
    assert config.url == "https://mcp.stripe.com"
    assert config.auth == "oauth"
    record = config.as_record()
    # 关键：这份记录必须过得了 `mcp add` 用的同一套校验。
    manager = MCPManager(_settings(tmp_path))
    manager.save_servers([record])
    saved = manager.servers()
    assert [entry["name"] for entry in saved] == ["stripe"]
    assert saved[0]["url"] == "https://mcp.stripe.com"


def test_render_local_entry_produces_command_record(tmp_path):
    """本地条目（n8n 之外没有，用不存在的名字构造）走 command 分支。"""
    entry = mcp_catalog.CatalogEntry(
        name="local-demo",
        label="Local Demo",
        description="本地 stdio 服务",
        transport="stdio",
        command="python",
        args=("-m", "demo"),
        env=("DEMO_TOKEN",),
        auth="api_key",
        docs="https://example.com",
    )
    monkeypatched = (*mcp_catalog.CATALOG, entry)
    original = mcp_catalog.CATALOG
    mcp_catalog.CATALOG = monkeypatched  # type: ignore[misc]
    try:
        config = mcp_catalog.render("local-demo", overrides={"env": {"DEMO_TOKEN": "keyring:t"}})
        assert config.command == "python"
        assert config.args == ("-m", "demo")
        assert config.env == {"DEMO_TOKEN": "keyring:t"}
        record = config.as_record()
        assert record["command"] == "python"
        manager = MCPManager(_settings(tmp_path))
        manager.save_servers([record])
        assert manager.servers()[0]["args"] == ["-m", "demo"]
    finally:
        mcp_catalog.CATALOG = original  # type: ignore[misc]


def test_render_overrides_name_and_flags(tmp_path):
    config = mcp_catalog.render(
        "linear",
        overrides={"name": "my-linear", "auto_approve": True, "timeout": 42},
    )
    assert config.name == "my-linear"
    assert config.auto_approve is True
    assert config.timeout == 42.0
    record = config.as_record()
    manager = MCPManager(_settings(tmp_path))
    manager.save_servers([record])
    # timeout 是 save_servers 白名单外的字段，这里只验 save_servers 接受它。
    assert manager.servers()[0]["name"] == "my-linear"


def test_render_refuses_unknown_entry():
    with pytest.raises(mcp_catalog.CatalogError, match="抄不到"):
        mcp_catalog.render("n8n")


def test_render_refuses_missing_entry():
    with pytest.raises(mcp_catalog.CatalogError, match="目录里没有"):
        mcp_catalog.render("nope")


# ── install：只写配置、不装包 ────────────────────────────────────────────────


def test_install_writes_config_and_says_it_needs_network(tmp_path):
    settings = _settings(tmp_path)
    result = mcp_catalog.install(settings, "context7")
    assert result["ok"] is True
    assert result["name"] == "context7"
    assert result["needs_network"] is True
    assert "网络" in result["message"] and "依赖" in result["message"]
    assert "不会下载任何包" in result["message"]
    written = json.loads((settings.data_dir / "mcp.json").read_text(encoding="utf-8"))
    assert [entry["name"] for entry in written["servers"]] == ["context7"]
    assert written["servers"][0]["url"] == "https://mcp.context7.com/mcp"


def test_install_refuses_duplicates(tmp_path):
    settings = _settings(tmp_path)
    mcp_catalog.install(settings, "context7")
    with pytest.raises(mcp_catalog.CatalogError, match="已在"):
        mcp_catalog.install(settings, "context7")


def test_install_refuses_unknown_entry(tmp_path):
    with pytest.raises(mcp_catalog.CatalogError, match="抄不到"):
        mcp_catalog.install(_settings(tmp_path), "n8n")


def test_install_preserves_oauth_flag(tmp_path):
    """``auth: oauth`` 必须落进配置——`mcp reauth --all` 就是靠这个字段找服务的。"""
    settings = _settings(tmp_path)
    mcp_catalog.install(settings, "official/stripe")
    written = json.loads((settings.data_dir / "mcp.json").read_text(encoding="utf-8"))
    assert written["servers"][0]["auth"] == "oauth"


# ── CLI：catalog / picker / install ─────────────────────────────────────────


def test_cli_catalog_lists_every_entry(tmp_path, capsys):
    code, captured = _run_cli(tmp_path, capsys, "mcp", "catalog")
    assert code == 0
    assert "airtable" in captured.out
    assert "wordpress-com" in captured.out
    assert "抄不全" in captured.out  # n8n 如实标出来
    assert f"共 {CATALOG_SIZE} 条" in captured.out


def test_cli_catalog_json_is_machine_readable(tmp_path, capsys):
    code, captured = _run_cli(tmp_path, capsys, "--json", "mcp", "catalog")
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["count"] == CATALOG_SIZE
    assert len(payload["entries"]) == CATALOG_SIZE


def test_cli_picker_lists_numbered_entries_and_says_not_interactive(tmp_path, capsys):
    code, captured = _run_cli(tmp_path, capsys, "mcp", "picker")
    assert code == 0
    assert "1." in captured.out
    assert "orbit mcp install" in captured.out
    assert "交互式选择器未实现" in captured.err


def test_cli_install_writes_and_warns(tmp_path, capsys):
    code, captured = _run_cli(tmp_path, capsys, "mcp", "install", "linear")
    assert code == 0
    assert "已把 linear 的配置写入" in captured.out
    assert "没有安装任何依赖" in captured.err
    servers = json.loads((tmp_path / "data" / "mcp.json").read_text(encoding="utf-8"))["servers"]
    assert servers[0]["name"] == "linear"


def test_cli_install_unknown_entry_exits_3(tmp_path, capsys):
    code, captured = _run_cli(tmp_path, capsys, "mcp", "install", "n8n")
    assert code == 3
    assert "抄不到" in captured.err


def test_cli_install_missing_name_exits_1(tmp_path, capsys):
    code, captured = _run_cli(tmp_path, capsys, "mcp", "install", "nope")
    assert code == 1
    assert "目录里没有" in captured.err
