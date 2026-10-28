"""linter 12 条规则 + AST 审计 + staging 审批门的测试。

测试策略：**每条规则一个命中样本**（而不是一份"什么都写错"的技能）。理由与
`tests/test_skill_index.py` 的 11 条硬校验同：混在一起只能得到「12 条里有一条不对」，
而真正需要知道的是「哪一条」。

另外两个必测的门：
- **正常技能零误报**。一条永远为真的告警会让所有告警一起被无视，所以「干净样本
  必须干净」比「坏样本能被抓到」更重要。
- **语法错误的代码块报成 lint 问题、不抛异常**。抛异常会让整份报告消失。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli.commands import skill_extra  # noqa: F401  （导入即接线）
from app.cli.main import main
from app.skill_lint import (
    CODE_AST_CALL,
    CODE_AST_SYNTAX,
    CODE_BODY_LENGTH,
    CODE_DANGEROUS,
    CODE_DESCRIPTION,
    CODE_DUPLICATE_TRIGGERS,
    CODE_ENCODING,
    CODE_FRONTMATTER,
    CODE_HEADING,
    CODE_NAME_FORMAT,
    CODE_PLACEHOLDER,
    CODE_SCOPE_DIR,
    CODE_TRIGGERS,
    CODE_UNKNOWN_FIELD,
    CODE_URL,
    MAX_SKILL_CONTENT_CHARS,
    MAX_TRIGGERS,
    RULE_COUNT,
    RULES,
    SEVERITY_ERROR,
    STAGING_DIRNAME,
    SkillLintError,
    SkillStaging,
    audit_python_blocks,
    lint,
)

#: 一份**干净**的技能：12 条规则全过。任何一条误报都会在这里暴露。
CLEAN_BODY = "# 用法\n\n1. 打开页面\n2. 导出表格\n"
CLEAN_TEXT = (
    "---\n"
    "name: demo\n"
    "description: 导出表格并归档。\n"
    "version: 1\n"
    "scope: global\n"
    "triggers:\n"
    "  - 导出表格\n"
    "  - 归档附件\n"
    "---\n\n"
    f"{CLEAN_BODY}"
)


def skill_text(
    *,
    name: str = "demo",
    description: str = "导出表格并归档。",
    extra_frontmatter: str = "",
    body: str = CLEAN_BODY,
) -> str:
    """拼一份 SKILL.md 全文（默认干净）。"""
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"{extra_frontmatter}"
        "---\n\n"
        f"{body}"
    )


def codes(text: str, *, name: str = "demo", path: Path | None = None) -> set[str]:
    return {finding.code for finding in lint(name, text, path=path)}


# ── 规则集本身 ──────────────────────────────────────────────────────────────


def test_there_are_exactly_twelve_rules():
    """条数、code 集合、以及「2 条 ERROR」都要钉死。

    只断言长度是不够的：把两条规则合并、再加一条新的，长度照样是 12，
    而矩阵上写的「linter 12 规则」就变成了另一个东西。
    """
    assert RULE_COUNT == 12
    assert len(RULES) == 12
    assert [rule.code for rule in RULES] == [
        CODE_NAME_FORMAT,
        CODE_SCOPE_DIR,
        CODE_DESCRIPTION,
        CODE_TRIGGERS,
        CODE_BODY_LENGTH,
        CODE_DANGEROUS,
        CODE_URL,
        CODE_PLACEHOLDER,
        CODE_UNKNOWN_FIELD,
        CODE_HEADING,
        CODE_DUPLICATE_TRIGGERS,
        CODE_ENCODING,
    ]
    errors = [rule.code for rule in RULES if rule.severity == SEVERITY_ERROR]
    assert errors == [CODE_NAME_FORMAT, CODE_SCOPE_DIR], "与 Hermes 的「12 条 / 2 条 ERROR」同构"
    assert all(rule.summary.strip() for rule in RULES), "每条规则都要有中文说明"


# ── 零误报 ──────────────────────────────────────────────────────────────────


def test_clean_skill_has_no_findings(tmp_path):
    path = tmp_path / "skills" / "demo" / "SKILL.md"
    assert lint("demo", CLEAN_TEXT, path=path) == []


def test_clean_skill_keeps_orbit_scope_and_status_fields(tmp_path):
    """`scope` / `status` 是 Orbit 自己写的字段，不该被报成「未知字段」。"""
    text = skill_text(extra_frontmatter="status: active\n")
    assert codes(text) == set()


# ── 12 条规则：各一个命中样本 ───────────────────────────────────────────────


def test_rule_name_format_rejects_uppercase_and_spaces():
    text = skill_text(name="Bad Name")
    assert codes(text, name="Bad Name") == {CODE_NAME_FORMAT}


def test_rule_scope_mismatch_against_directory(tmp_path):
    text = skill_text(extra_frontmatter="scope: global\n")
    path = tmp_path / "proj" / ".orbit" / "skills" / "demo" / "SKILL.md"
    findings = lint("demo", text, path=path)
    assert [finding.code for finding in findings] == [CODE_SCOPE_DIR]
    assert findings[0].severity == SEVERITY_ERROR


def test_rule_scope_unknown_value():
    text = skill_text(extra_frontmatter="scope: everywhere\n")
    assert codes(text) == {CODE_SCOPE_DIR}


def test_rule_description_too_long():
    text = skill_text(description="很长的描述" * 20)
    assert codes(text) == {CODE_DESCRIPTION}


def test_rule_description_missing():
    text = "---\nname: demo\nversion: 1\n---\n\n# 用法\n\n1. 做点事\n"
    findings = lint("demo", text)
    assert CODE_DESCRIPTION in {finding.code for finding in findings}


def test_rule_triggers_too_many():
    triggers = "".join(f"  - 触发词{i}\n" for i in range(MAX_TRIGGERS + 1))
    text = skill_text(extra_frontmatter=f"triggers:\n{triggers}")
    findings = lint("demo", text)
    assert [finding.code for finding in findings] == [CODE_TRIGGERS]
    assert str(MAX_TRIGGERS) in findings[0].message


def test_rule_triggers_too_short():
    text = skill_text(extra_frontmatter="triggers:\n  - a\n")
    assert codes(text) == {CODE_TRIGGERS}


def test_rule_body_too_long():
    body = "# 用法\n\n" + ("步骤。" * (MAX_SKILL_CONTENT_CHARS // 3 + 10))
    assert len(body) > MAX_SKILL_CONTENT_CHARS
    assert codes(skill_text(body=body)) == {CODE_BODY_LENGTH}


def test_rule_dangerous_command_in_code_block():
    body = "# 用法\n\n清空临时目录：\n\n```bash\nrm -rf /\n```\n"
    findings = lint("demo", skill_text(body=body))
    assert [finding.code for finding in findings] == [CODE_DANGEROUS]
    assert findings[0].line > 1, "危险命令必须给出行号，否则没人会去修"


def test_rule_untrusted_url():
    body = "# 用法\n\n参考 https://totally-untrusted.example.com/guide 里的步骤。\n"
    assert codes(skill_text(body=body)) == {CODE_URL}


def test_rule_trusted_url_is_not_flagged():
    body = "# 用法\n\n参考 https://docs.python.org/3/library/ast.html 。\n"
    assert codes(skill_text(body=body)) == set()


def test_rule_placeholder():
    body = "# 用法\n\n1. 打开页面\n2. TODO: 补充导出步骤\n"
    findings = lint("demo", skill_text(body=body))
    assert [finding.code for finding in findings] == [CODE_PLACEHOLDER]
    assert findings[0].line == 9


def test_rule_unknown_frontmatter_field():
    text = skill_text(extra_frontmatter="weird_field: 1\n")
    findings = lint("demo", text)
    assert [finding.code for finding in findings] == [CODE_UNKNOWN_FIELD]
    assert "weird_field" in findings[0].message


def test_rule_missing_heading():
    body = "1. 打开页面\n2. 导出表格\n"
    assert codes(skill_text(body=body)) == {CODE_HEADING}


def test_rule_duplicate_triggers():
    text = skill_text(extra_frontmatter="triggers:\n  - 导出表格\n  - 导出表格\n")
    assert codes(text) == {CODE_DUPLICATE_TRIGGERS}


def test_rule_bom_encoding():
    text = "\ufeff" + CLEAN_TEXT
    findings = lint("demo", text)
    assert [finding.code for finding in findings] == [CODE_ENCODING]
    assert "BOM" in findings[0].message


def test_rule_zero_width_characters():
    body = "# 用法\n\n1. 打开页面\u200b\u200b\u200b\n"
    assert codes(skill_text(body=body)) == {CODE_ENCODING}


def test_broken_frontmatter_reports_invalid_instead_of_crashing():
    """frontmatter 读不出来 → 一条 frontmatter-invalid，规则照跑，绝不抛异常。"""
    findings = lint("demo", "这不是 frontmatter\n\n# 标题\n")
    assert findings[0].code == CODE_FRONTMATTER
    assert findings[0].severity == SEVERITY_ERROR


# ── AST 审计 ────────────────────────────────────────────────────────────────


def test_ast_audit_finds_eval_in_python_block():
    body = "# 用法\n\n```python\nvalue = eval(user_input)\n```\n"
    findings = audit_python_blocks(body)
    assert [finding.code for finding in findings] == [CODE_AST_CALL]
    assert "eval" in findings[0].message
    assert findings[0].line == 4


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("import os\nos.system('dir')\n", "os.system"),
        ("import subprocess\nsubprocess.run(['ls'])\n", "subprocess.run"),
        ("exec('print(1)')\n", "exec"),
    ],
)
def test_ast_audit_finds_forbidden_calls(code: str, expected: str):
    findings = audit_python_blocks(f"```python\n{code}```\n")
    assert [finding.code for finding in findings] == [CODE_AST_CALL]
    assert expected in findings[0].message


def test_ast_audit_reports_syntax_error_as_finding():
    """解析失败要报 lint 问题，**不能抛异常**——抛了整份报告就没了。"""
    findings = audit_python_blocks("```python\ndef broken(:\n```\n")
    assert [finding.code for finding in findings] == [CODE_AST_SYNTAX]
    assert "语法错误" in findings[0].message


def test_ast_audit_ignores_non_python_blocks():
    body = "```bash\neval \"$1\"\n```\n\n```text\nos.system('x')\n```\n"
    assert audit_python_blocks(body) == []


def test_ast_audit_merges_into_lint_result():
    body = "# 用法\n\n```python\nprint(eval('1+1'))\n```\n"
    assert codes(skill_text(body=body)) == {CODE_AST_CALL}


# ── staging 审批门 ──────────────────────────────────────────────────────────


def make_staging(tmp_path) -> SkillStaging:
    return SkillStaging(tmp_path / "data")


def test_stage_writes_to_staging_not_to_skills(tmp_path):
    staging = make_staging(tmp_path)
    staging_id = staging.stage("demo", CLEAN_TEXT, note="模型提议")
    assert (tmp_path / "data" / STAGING_DIRNAME / staging_id / "SKILL.md").is_file()
    assert not (tmp_path / "data" / "skills" / "demo").exists(), "写出来不该直接生效"


def test_stage_is_idempotent_by_content(tmp_path):
    staging = make_staging(tmp_path)
    first = staging.stage("demo", CLEAN_TEXT)
    second = staging.stage("demo", CLEAN_TEXT)
    assert first == second
    assert len(staging.list()) == 1


def test_stage_then_approve_moves_into_skills(tmp_path):
    staging = make_staging(tmp_path)
    staging_id = staging.stage("demo", CLEAN_TEXT)
    result = staging.approve(staging_id)
    target = tmp_path / "data" / "skills" / "demo" / "SKILL.md"
    assert Path(result["path"]) == target
    assert target.is_file()
    assert not (tmp_path / "data" / STAGING_DIRNAME / staging_id).exists()
    assert staging.list() == []


def test_stage_then_reject_removes_it(tmp_path):
    staging = make_staging(tmp_path)
    staging_id = staging.stage("demo", CLEAN_TEXT)
    assert staging.reject(staging_id) is True
    assert not (tmp_path / "data" / STAGING_DIRNAME / staging_id).exists()
    assert not (tmp_path / "data" / "skills" / "demo").exists()


def test_approve_refuses_content_that_fails_hard_validation(tmp_path):
    staging = make_staging(tmp_path)
    staging_id = staging.stage("demo", "没有 frontmatter 的内容\n")
    with pytest.raises(SkillLintError, match="审批被拒"):
        staging.approve(staging_id)
    assert not (tmp_path / "data" / "skills" / "demo").exists()


def test_approve_refuses_lint_errors(tmp_path):
    """lint ERROR（名字不合规）也要拦住——放进去的技能名会与目录名对不上。"""
    staging = make_staging(tmp_path)
    staging_id = staging.stage("Bad Name", skill_text(name="Bad Name"))
    with pytest.raises(SkillLintError, match="ERROR"):
        staging.approve(staging_id)


def test_approve_refuses_overwriting_existing_skill(tmp_path):
    staging = make_staging(tmp_path)
    existing = tmp_path / "data" / "skills" / "demo"
    existing.mkdir(parents=True)
    (existing / "SKILL.md").write_text(CLEAN_TEXT, encoding="utf-8")
    staging_id = staging.stage("demo", CLEAN_TEXT)
    with pytest.raises(SkillLintError, match="已有同名技能"):
        staging.approve(staging_id)


def test_unknown_staging_id_raises(tmp_path):
    staging = make_staging(tmp_path)
    with pytest.raises(SkillLintError, match="待审区里没有"):
        staging.approve("deadbeef")
    with pytest.raises(SkillLintError, match="待审区里没有"):
        staging.reject("deadbeef")


def test_stage_rejects_empty_content(tmp_path):
    staging = make_staging(tmp_path)
    with pytest.raises(SkillLintError, match="内容为空"):
        staging.stage("demo", "   ")


# ── CLI ─────────────────────────────────────────────────────────────────────


def write_skill(data: Path, name: str = "demo", text: str = CLEAN_TEXT) -> Path:
    directory = data / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    return directory


def test_cli_skills_lint_clean(tmp_path, capsys):
    data = tmp_path / "data"
    write_skill(data)
    assert main(["--data-dir", str(data), "skills", "lint"]) == 0
    out = capsys.readouterr().out
    assert "干净" in out
    assert f"{RULE_COUNT} 条规则" in out


def test_cli_skills_lint_flags_problems(tmp_path, capsys):
    data = tmp_path / "data"
    write_skill(data, text=skill_text(name="Bad Name"))
    code = main(["--data-dir", str(data), "skills", "lint"])
    assert code == 1
    assert "Bad Name" in capsys.readouterr().out


def test_cli_skills_lint_unknown_skill(tmp_path, capsys):
    data = tmp_path / "data"
    write_skill(data)
    code = main(["--data-dir", str(data), "skills", "lint", "ghost"])
    assert code == 1
    assert "技能不存在" in capsys.readouterr().err


def test_cli_skills_stage_lists_and_creates(tmp_path, capsys):
    data = tmp_path / "data"
    assert main(["--data-dir", str(data), "skills", "stage"]) == 0
    assert "没有待审技能" in capsys.readouterr().out

    source = tmp_path / "new.md"
    source.write_text(CLEAN_TEXT, encoding="utf-8")
    assert main(
        ["--data-dir", str(data), "skills", "stage", "demo", "--file", str(source), "--note", "初稿"]
    ) == 0
    out = capsys.readouterr().out
    assert "已暂存" in out
    assert (data / STAGING_DIRNAME).is_dir()


def test_cli_skills_stage_then_approve(tmp_path, capsys):
    data = tmp_path / "data"
    source = tmp_path / "new.md"
    source.write_text(CLEAN_TEXT, encoding="utf-8")
    assert main(["--data-dir", str(data), "skills", "stage", "demo", "--file", str(source)]) == 0
    staging_id = next(path.name for path in (data / STAGING_DIRNAME).iterdir() if path.is_dir())
    capsys.readouterr()

    assert main(["--data-dir", str(data), "skills", "approve", staging_id]) == 0
    assert "已批准" in capsys.readouterr().out
    assert (data / "skills" / "demo" / "SKILL.md").is_file()


def test_cli_skills_stage_then_reject(tmp_path, capsys):
    data = tmp_path / "data"
    source = tmp_path / "new.md"
    source.write_text(CLEAN_TEXT, encoding="utf-8")
    assert main(["--data-dir", str(data), "skills", "stage", "demo", "--file", str(source)]) == 0
    staging_id = next(path.name for path in (data / STAGING_DIRNAME).iterdir() if path.is_dir())
    capsys.readouterr()

    assert main(["--data-dir", str(data), "skills", "approve", staging_id, "--reject"]) == 0
    assert "已驳回" in capsys.readouterr().out
    assert not (data / STAGING_DIRNAME / staging_id).exists()
    assert not (data / "skills" / "demo").exists()


def test_cli_skills_stage_requires_content(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"

    class _Tty:
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr("sys.stdin", _Tty())
    code = main(["--data-dir", str(data), "skills", "stage", "demo"])
    assert code == 1
    assert "--file" in capsys.readouterr().err


def test_cli_skills_stage_reads_stdin(tmp_path, monkeypatch, capsys):
    data = tmp_path / "data"
    import io

    class _Pipe(io.StringIO):
        def isatty(self) -> bool:
            return False

    monkeypatch.setattr("sys.stdin", _Pipe(CLEAN_TEXT))
    assert main(["--data-dir", str(data), "skills", "stage", "demo"]) == 0
    assert "已暂存" in capsys.readouterr().out


def test_cli_skills_approve_unknown_id(tmp_path, capsys):
    data = tmp_path / "data"
    code = main(["--data-dir", str(data), "skills", "approve", "deadbeef"])
    assert code == 1
    assert "待审区里没有" in capsys.readouterr().err


def test_cli_skills_stage_list_shows_json_payload(tmp_path, capsys):
    data = tmp_path / "data"
    source = tmp_path / "new.md"
    source.write_text(CLEAN_TEXT, encoding="utf-8")
    assert main(["--data-dir", str(data), "skills", "stage", "demo", "--file", str(source)]) == 0
    capsys.readouterr()
    assert main(["--data-dir", str(data), "--json", "skills", "stage"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 1
    assert payload["staged"][0]["name"] == "demo"
