"""`app/osv.py` 的契约测试（MATRIX 14.5）。

四组：

1. **query 形状**：依赖声明（含范围约束）→ OSV query。没有确切版本时**不带**
   `version` 键——带一个假版本比不带更糟。
2. **`parse_response`**：对**真实形状**的 OSV 载荷（`query` 的 `vulns[]`、
   `querybatch` 的 `results[].vulns[]`、导出文件的数组）都能解析出
   id / summary / severity / affected ranges / fixed。
3. **无网络**：`audit_local()` 报 `NetworkUnavailable` 的说明、`passed()` 为 False，
   而且**输出里绝不出现"未发现漏洞"**；`strict=True` 直接抛。
4. **有本地库**：`apply_offline_db` 之后**真的**比对出漏洞（含"修复版本之后不命中"
   这个反向用例，否则"命中"可能只是无脑全命中）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.cli.commands.security_extra  # noqa: F401 —— 导入即挂上 security / approvals 的新子命令
from app import osv
from app.cli.main import main

#: 一份**照真实 OSV 响应抄的**载荷（GHSA-9wx4-h78v-vm56 / CVE-2024-35195）。
#: 用真实形状而不是编一个最简结构，是因为解析器最容易在"真实字段嵌套"上出错。
OSV_QUERY_PAYLOAD = {
    "vulns": [
        {
            "id": "GHSA-9wx4-h78v-vm56",
            "summary": (
                "Requests Session object does not verify requests after making "
                "first request with verify=False"
            ),
            "details": "### Impact\nWhen a `Session` is used ...",
            "aliases": ["CVE-2024-35195"],
            "modified": "2024-05-21T14:39:54.557686Z",
            "published": "2024-05-20T21:15:09Z",
            "severity": [
                {"type": "CVSS_V3", "score": "CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:H/I:H/A:N"}
            ],
            "database_specific": {"severity": "MODERATE", "cwe_ids": ["CWE-670"]},
            "affected": [
                {
                    "package": {
                        "name": "requests",
                        "ecosystem": "PyPI",
                        "purl": "pkg:pypi/requests",
                    },
                    "ranges": [
                        {
                            "type": "ECOSYSTEM",
                            "events": [{"introduced": "0"}, {"fixed": "2.32.0"}],
                        }
                    ],
                    "versions": ["2.0.0", "2.31.0", "2.31.1"],
                }
            ],
            "references": [{"type": "ADVISORY", "url": "https://github.com/advisories/x"}],
        }
    ]
}


@pytest.fixture(autouse=True)
def _clean_db():
    """每个用例前后都卸掉离线库：它是**模块级全局**，泄漏会让别的用例假阳性。"""
    osv.clear_offline_db()
    yield
    osv.clear_offline_db()


def _project(tmp_path: Path, body: str) -> Path:
    """造一个只有 `pyproject.toml` 的"项目"。"""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text(body, encoding="utf-8")
    return root


def _empty_site_packages(tmp_path: Path) -> Path:
    """空的 site-packages：这样"实际安装版本"查不到，只能靠声明里钉死的版本。"""
    path = tmp_path / "site-packages"
    path.mkdir()
    return path


# ── 1. query 形状 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("line", "name", "version"),
    [
        ("fastapi>=0.115,<1", "fastapi", ""),
        ("sqlite-vec==0.1.9", "sqlite-vec", "0.1.9"),
        ("requests==2.31.0", "requests", "2.31.0"),
        ("pydantic-settings>=2.7,<3", "pydantic-settings", ""),
        ("uvicorn[standard]>=0.34", "uvicorn", ""),
        ("keyring", "keyring", ""),
        ('numpy>=2; python_version >= "3.12"', "numpy", ""),
    ],
)
def test_parse_requirement(line, name, version):
    dependency = osv.parse_requirement(line)
    assert dependency is not None
    assert dependency.name == name
    assert dependency.version == version


def test_parse_requirement_skips_comments_and_flags():
    assert osv.parse_requirement("# 注释") is None
    assert osv.parse_requirement("-r other.txt") is None
    assert osv.parse_requirement("") is None


def test_query_shape_matches_osv_contract():
    pinned = osv.parse_requirement("requests==2.31.0")
    assert pinned is not None
    assert pinned.query() == {
        "package": {"name": "requests", "ecosystem": "PyPI"},
        "version": "2.31.0",
    }
    ranged = osv.parse_requirement("fastapi>=0.115,<1")
    assert ranged is not None
    # 范围约束不是版本：**不带** `version` 键，而不是塞一个假的。
    assert ranged.query() == {"package": {"name": "fastapi", "ecosystem": "PyPI"}}


def test_osv_queries_covers_the_repository_dependencies():
    queries = osv.osv_queries()
    assert queries, "仓库里应当读得到依赖声明"
    names = {query["package"]["name"] for query in queries}
    assert {"fastapi", "sqlalchemy", "httpx"} <= names
    assert all(query["package"]["ecosystem"] == "PyPI" for query in queries)
    # 模块级常量是同一份形状的快照
    assert len(osv.OSV_QUERIES) == len(queries)


def test_dependency_specs_reads_requirements_txt_too(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "requirements.txt").write_text("flask==3.0.0\n# x\n", encoding="utf-8")
    found = {item.name: item.version for item in osv.dependency_specs(root)}
    assert found["flask"] == "3.0.0"


# ── 2. parse_response ──────────────────────────────────────────────────────


def test_parse_response_reads_a_real_osv_payload():
    vulns = osv.parse_response(OSV_QUERY_PAYLOAD)
    assert len(vulns) == 1
    vuln = vulns[0]
    assert vuln.id == "GHSA-9wx4-h78v-vm56"
    assert vuln.severity == "moderate"  # database_specific.severity = MODERATE
    assert "CVE-2024-35195" in vuln.aliases
    assert vuln.fixed == ("2.32.0",)
    assert vuln.affected[0].package == "requests"
    assert vuln.affected[0].ecosystem == "PyPI"
    assert vuln.affected[0].introduced == "0"
    assert vuln.affected[0].fixed == "2.32.0"
    assert "2.31.0" in vuln.affected[0].versions
    assert "Requests Session" in vuln.summary


def test_parse_response_handles_querybatch_and_bare_lists():
    batch = {"results": [{"vulns": OSV_QUERY_PAYLOAD["vulns"]}, {}]}
    assert [vuln.id for vuln in osv.parse_response(batch)] == ["GHSA-9wx4-h78v-vm56"]
    assert [vuln.id for vuln in osv.parse_response(OSV_QUERY_PAYLOAD["vulns"])] == [
        "GHSA-9wx4-h78v-vm56"
    ]


def test_parse_response_skips_records_without_an_id():
    """一条坏记录不该让整次审计变成空。"""
    payload = {"vulns": [{"summary": "没有 id"}, OSV_QUERY_PAYLOAD["vulns"][0]]}
    assert [vuln.id for vuln in osv.parse_response(payload)] == ["GHSA-9wx4-h78v-vm56"]


def test_parse_response_does_not_guess_severity_without_a_label():
    """`severity[]` 是 CVSS 向量，算分要额外逻辑；没有明确等级就记 unknown，不猜。"""
    payload = {
        "vulns": [
            {
                "id": "OSV-1",
                "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
                "affected": [
                    {"package": {"name": "x", "ecosystem": "PyPI"}, "ranges": [{"events": [{"introduced": "0"}]}]}
                ],
            }
        ]
    }
    vuln = osv.parse_response(payload)[0]
    assert vuln.severity == "unknown"
    # unknown 仍然算命中（不知道严重度不等于不严重）
    assert vuln.rank == 0


def test_parse_response_reads_explicit_version_lists():
    payload = {
        "vulns": [
            {
                "id": "GHSA-x",
                "database_specific": {"severity": "HIGH"},
                "affected": [
                    {
                        "package": {"name": "y", "ecosystem": "PyPI"},
                        "versions": ["1.0.0", "1.0.1"],
                    }
                ],
            }
        ]
    }
    vuln = osv.parse_response(payload)[0]
    assert vuln.severity == "high"
    assert vuln.affected[0].versions == ("1.0.0", "1.0.1")


# ── 3. 无网络：明确报 NetworkUnavailable ───────────────────────────────────


def test_audit_local_without_a_database_reports_network_required(tmp_path):
    root = _project(
        tmp_path,
        '[project]\nname = "x"\nversion = "0"\ndependencies = ["requests==2.31.0"]\n',
    )
    report = osv.audit_local(root=root, site_packages=_empty_site_packages(tmp_path))
    assert report.network_required is True
    assert report.passed() is False, "没比对过就不能算通过"
    assert report.headline() == "未审计（需要联网或本地通告库）"
    assert "OSV.dev" in report.note
    # 第一原则：输出现里绝不能出现"未发现漏洞"这五个字
    assert "未发现漏洞" not in report.note
    assert "一次通告比对都没有发生" in report.note


def test_audit_local_strict_raises_network_unavailable(tmp_path):
    root = _project(
        tmp_path, '[project]\nname = "x"\nversion = "0"\ndependencies = ["requests"]\n'
    )
    with pytest.raises(osv.NetworkUnavailable) as excinfo:
        osv.audit_local(
            root=root, site_packages=_empty_site_packages(tmp_path), strict=True
        )
    assert "OSV.dev" in str(excinfo.value)


def test_audit_local_lists_dependencies_even_offline(tmp_path):
    root = _project(
        tmp_path,
        '[project]\nname = "x"\nversion = "0"\ndependencies = ["requests==2.31.0", "flask>=3"]\n',
    )
    report = osv.audit_local(root=root, site_packages=_empty_site_packages(tmp_path))
    assert [item.name for item in report.dependencies] == ["requests", "flask"]
    assert report.scanned == 2


def test_audit_local_marks_unpinned_dependencies_as_incomparable(tmp_path):
    """只有范围约束、环境里又查不到安装版本 → 无法比对。**不猜**。"""
    root = _project(
        tmp_path, '[project]\nname = "x"\nversion = "0"\ndependencies = ["flask>=3"]\n'
    )
    database = osv.load_offline_db(_write_db(tmp_path))
    report = osv.audit_local(
        root=root, site_packages=_empty_site_packages(tmp_path), database=database
    )
    assert report.incomparable == ["flask"]
    assert "没有确切版本" in report.note


def test_audit_local_with_no_declarations_is_not_a_pass(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    report = osv.audit_local(root=empty, site_packages=_empty_site_packages(tmp_path))
    assert report.network_required is False
    assert report.passed() is False
    assert "没有东西可比" in report.note


# ── 4. 有本地库：真的能比对出漏洞 ──────────────────────────────────────────


def _write_db(tmp_path: Path, *, fixed: str = "2.32.0") -> Path:
    """写一份本地 OSV 导出（JSON 数组，OSV 官方导出就是这个形状）。"""
    record = json.loads(json.dumps(OSV_QUERY_PAYLOAD["vulns"][0]))
    record["affected"][0]["ranges"][0]["events"] = [{"introduced": "0"}, {"fixed": fixed}]
    record["affected"][0].pop("versions", None)
    path = tmp_path / "osv-export.json"
    path.write_text(json.dumps([record], ensure_ascii=False), encoding="utf-8")
    return path


def test_offline_database_finds_a_real_vulnerability(tmp_path):
    root = _project(
        tmp_path,
        '[project]\nname = "x"\nversion = "0"\ndependencies = ["requests==2.31.0"]\n',
    )
    database = osv.apply_offline_db(_write_db(tmp_path))
    assert osv.active_db() is database
    report = osv.audit_local(root=root, site_packages=_empty_site_packages(tmp_path))
    assert report.network_required is False
    assert report.passed() is False
    assert [finding.advisory for finding in report.findings] == ["GHSA-9wx4-h78v-vm56"]
    assert report.findings[0].target == "requests==2.31.0"
    assert report.findings[0].severity == "moderate"
    assert "2.32.0" in report.findings[0].detail
    assert report.headline() == "命中 1 条"
    assert report.worst() == "moderate"


def test_offline_database_does_not_flag_a_fixed_version(tmp_path):
    """反向用例：不命中才说明比对是真的，而不是无脑全命中。"""
    root = _project(
        tmp_path,
        '[project]\nname = "x"\nversion = "0"\ndependencies = ["requests==2.32.0"]\n',
    )
    osv.apply_offline_db(_write_db(tmp_path))
    report = osv.audit_local(root=root, site_packages=_empty_site_packages(tmp_path))
    assert report.findings == []
    assert report.passed() is True
    assert report.headline().startswith("未命中")


def test_offline_database_ignores_another_ecosystem_and_package(tmp_path):
    root = _project(
        tmp_path,
        '[project]\nname = "x"\nversion = "0"\ndependencies = ["flask==2.31.0"]\n',
    )
    osv.apply_offline_db(_write_db(tmp_path))
    report = osv.audit_local(root=root, site_packages=_empty_site_packages(tmp_path))
    assert report.findings == []


def test_offline_database_is_cleared_between_tests():
    assert osv.active_db() is None


def test_load_offline_db_reads_ndjson(tmp_path):
    path = tmp_path / "export.ndjson"
    path.write_text(
        "\n".join(json.dumps(item) for item in OSV_QUERY_PAYLOAD["vulns"]),
        encoding="utf-8",
    )
    database = osv.load_offline_db(path)
    assert database.advisories == 1
    assert database.vulns[0].id == "GHSA-9wx4-h78v-vm56"


def test_load_offline_db_rejects_a_missing_file(tmp_path):
    with pytest.raises(ValueError, match="不存在"):
        osv.load_offline_db(tmp_path / "nope.json")


def test_load_offline_db_rejects_an_unparsable_file(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("这不是 JSON", encoding="utf-8")
    with pytest.raises(ValueError, match="无法解析"):
        osv.load_offline_db(path)


def test_advisories_for_matches_by_normalized_name():
    database = osv.OsvDatabase(vulns=tuple(osv.parse_response(OSV_QUERY_PAYLOAD)))
    # PEP 503：`Requests` 与 `requests` 是同一个包
    assert database.advisories_for("Requests", "2.31.0")
    assert database.advisories_for("requests", "2.32.0") == []


def test_describe_states_the_boundary():
    text = osv.describe()
    assert "离线" in text and "OSV.dev" in text
    assert "没做的" in text


# ── 5. CLI：每条新命令一条 ─────────────────────────────────────────────────


def _data_dir(tmp_path: Path) -> str:
    path = tmp_path / "data"
    path.mkdir()
    return str(path)


def test_cli_security_osv_without_a_database_exits_2(tmp_path, capsys):
    """需要联网时**不能返回 0**：0 会被脚本读成"审过了、没问题"。"""
    root = _project(
        tmp_path,
        '[project]\nname = "x"\nversion = "0"\ndependencies = ["requests==2.31.0"]\n',
    )
    code = main(
        [
            "--data-dir",
            _data_dir(tmp_path),
            "security",
            "osv",
            "--root",
            str(root),
            "--site-packages",
            str(_empty_site_packages(tmp_path)),
        ]
    )
    out = capsys.readouterr().out
    assert code == 2
    assert "requests" in out
    assert "未审计（需要联网或本地通告库）" in out


def test_cli_security_osv_with_a_local_db_finds_the_vulnerability(tmp_path, capsys):
    root = _project(
        tmp_path,
        '[project]\nname = "x"\nversion = "0"\ndependencies = ["requests==2.31.0"]\n',
    )
    code = main(
        [
            "--data-dir",
            _data_dir(tmp_path),
            "security",
            "osv",
            "--root",
            str(root),
            "--site-packages",
            str(_empty_site_packages(tmp_path)),
            "--offline-db",
            str(_write_db(tmp_path)),
        ]
    )
    out = capsys.readouterr().out
    assert code == 1, out
    assert "GHSA-9wx4-h78v-vm56" in out
    assert "命中 1 条" in out


def test_cli_security_osv_strict_fails_hard(tmp_path):
    root = _project(
        tmp_path, '[project]\nname = "x"\nversion = "0"\ndependencies = ["requests"]\n'
    )
    code = main(
        [
            "--data-dir",
            _data_dir(tmp_path),
            "security",
            "osv",
            "--root",
            str(root),
            "--site-packages",
            str(_empty_site_packages(tmp_path)),
            "--strict",
        ]
    )
    assert code == 3


def test_cli_security_osv_rejects_a_missing_offline_db(tmp_path):
    code = main(
        [
            "--data-dir",
            _data_dir(tmp_path),
            "security",
            "osv",
            "--offline-db",
            str(tmp_path / "nope.json"),
        ]
    )
    assert code == 3
