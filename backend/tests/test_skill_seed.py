"""技能库规模的测试（MATRIX 7.18）+ 7.13 三条 partial 收尾的命令。

必测的五条：

1. ``SEED_CATALOG`` 的真实条数：**20 类目 / 91 技能**（源库目录 21 个，减去
   ``index-cache``），其中 3 组重名 → 扁平落盘后是 88 个目录；
2. ``body`` 是**抄的**，不是编的：逐条与真实 SKILL.md 的正文比对 sha256
   （源库不在本机时跳过，而不是假装通过）；
3. ``seed()`` **默认 dry_run**：不写盘；
4. 落盘的每一个技能 **lint 零 ERROR**（种一批坏技能等于把技能库变脏）；
5. ``coverage()`` 前后数字对得上。

末尾一节是 7.13 的 CLI：``audit`` / ``diff`` / ``config`` / ``tap`` /
``list-modified`` / ``reset`` / ``opt-out`` / ``opt-in`` / ``repair-official``。
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

from app import skill_seed
from app.cli.commands import skill_extra2  # noqa: F401  （导入即接线）
from app.cli.main import main
from app.skill_hub import SkillHub
from app.skill_index import SkillIndex, read_disabled, read_taps
from app.skill_lint import lint, lint_file

#: 真实 Hermes 安装目录（只在本机存在）。逐字节比对用它。
SOURCE_ROOT = Path(skill_seed.SEED_SOURCE_ROOT)
_FRONTMATTER = re.compile(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", re.DOTALL)

CLEAN = (
    "---\n"
    "name: {name}\n"
    "description: 一句话描述。\n"
    "version: 1\n"
    "---\n\n"
    "# 步骤\n\n1. 打开页面\n"
)


def write_skill(root: Path, name: str, body: str = "1. 打开页面") -> Path:
    directory = root / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(CLEAN.format(name=name), encoding="utf-8")
    return directory


# ── 目录规模：21 类目 / 91 技能的真实口径 ───────────────────────────────────


def test_catalog_has_91_skills_and_20_categories():
    """91 条技能、20 个类目（``<skills>/`` 下 21 个目录减去 ``index-cache``）。"""
    assert len(skill_seed.SEED_CATALOG) == 91
    assert len(skill_seed.SEED_CATEGORIES) == 20
    assert "index-cache" not in skill_seed.SEED_CATEGORIES
    # 名字去重后 88 条：3 组重名（源库里同名技能存在两份）
    assert len(skill_seed.seed_names()) == 88
    assert len(skill_seed.duplicates()) == 3
    assert {row["name"] for row in skill_seed.duplicates()} == {
        "computer-use",
        "dogfood",
        "codebase-inspection",
    }


def test_catalog_entries_have_required_fields():
    for skill in skill_seed.SEED_CATALOG:
        assert skill.name and skill.name == skill.name.lower()
        assert re.fullmatch(r"[a-z0-9][a-z0-9_-]*", skill.name), skill.name
        assert skill.category in skill_seed.SEED_CATEGORIES
        assert skill.description, skill.name
        assert skill.source.endswith("SKILL.md"), skill.source
        # 正文是真实文档，不是占位符：最短的那条也有几百字符
        assert len(skill.body) > 200, (skill.name, len(skill.body))


def test_catalog_covers_every_category():
    grouped = skill_seed.by_category()
    assert set(grouped) == set(skill_seed.SEED_CATEGORIES)
    assert all(grouped[category] for category in skill_seed.SEED_CATEGORIES)


@pytest.mark.skipif(
    not SOURCE_ROOT.is_dir(), reason="真实 Hermes 技能库不在本机，无法逐字节复核"
)
def test_catalog_bodies_are_verbatim_copies():
    """**这一条是"抄的而不是编的"的机器证据**：逐条比正文 sha256。"""
    mismatched: list[str] = []
    for skill in skill_seed.SEED_CATALOG:
        path = SOURCE_ROOT / skill.source
        assert path.is_file(), skill.source
        text = path.read_text(encoding="utf-8")
        match = _FRONTMATTER.match(text)
        assert match is not None, skill.source
        expected = text[match.end() :]
        if hashlib.sha256(expected.encode("utf-8")).hexdigest() != hashlib.sha256(
            skill.body.encode("utf-8")
        ).hexdigest():
            mismatched.append(skill.source)
    assert mismatched == []


def test_requires_reflect_real_frontmatter():
    """依赖声明来自真实 frontmatter（不是白名单猜的）。"""
    with_requires = [skill for skill in skill_seed.SEED_CATALOG if skill.requires]
    assert len(with_requires) == 23
    notes = skill_seed.find_seed("apple-notes")
    assert notes is not None
    assert notes.requires["commands"] == ("memo",)
    vllm = skill_seed.find_seed("serving-llms-vllm")
    assert vllm is not None
    assert "vllm" in vllm.requires["dependencies"]


# ── 渲染 / 校验 ────────────────────────────────────────────────────────────


def test_render_roundtrips_through_both_parsers():
    """渲染结果要同时被 yaml（索引侧）与手写解析器（注入侧）读懂。"""
    from app.skills import parse_skill

    skill = skill_seed.find_seed("apple-notes")
    assert skill is not None
    text = skill_seed.render_skill_md(skill)
    hand = parse_skill(text, skill.name)
    assert hand["name"] == skill.name
    assert hand["scope"] == "global"
    # 带 `: ` 的描述会被加引号，注入侧必须把引号剥掉（否则提示里会带引号）
    assert hand["description"] == skill.description
    assert list(hand["triggers"]) == list(skill.triggers)


def test_every_rendered_skill_lints_without_error():
    """91 份渲染结果全部零 ERROR——这条是"播进去不会变脏"的前置保证。"""
    bad: dict[str, list[str]] = {}
    for skill in skill_seed.SEED_CATALOG:
        findings = lint(skill.name, skill_seed.render_skill_md(skill))
        errors = [item.format() for item in findings if item.severity == "error"]
        if errors:
            bad[skill.name] = errors
    assert bad == {}


# ── seed：默认 dry_run / 落盘 / 幂等 ────────────────────────────────────────


def test_seed_default_is_dry_run_and_writes_nothing(tmp_path):
    data = tmp_path / "data"
    result = skill_seed.seed(data)
    assert result["dry_run"] is True
    assert result["written"] == []
    assert len(result["would_write"]) == 88
    assert not (data / "skills").exists()
    assert not (data / skill_seed.MANIFEST_FILENAME).exists()


def test_seed_writes_every_skill_and_all_lint_clean(tmp_path):
    data = tmp_path / "data"
    result = skill_seed.seed(data, dry_run=False)
    assert result["ok"] is True
    assert result["blocked"] == []
    assert len(result["written"]) == 88

    files = sorted((data / "skills").glob("*/SKILL.md"))
    assert len(files) == 88
    dirty = {
        path.parent.name: [item.format() for item in lint_file(path) if item.severity == "error"]
        for path in files
    }
    assert {name: errors for name, errors in dirty.items() if errors} == {}


def test_seed_reports_oversized_skill(tmp_path):
    """超过索引 100k 上限的那条要明说：索引不会加载它，得人工拆 references/。"""
    data = tmp_path / "data"
    result = skill_seed.seed(data, only=["research-paper-writing"], dry_run=False)
    assert [row["name"] for row in result["oversized"]] == ["research-paper-writing"]
    assert result["written"] == ["research-paper-writing"]


def test_seed_is_idempotent_and_respects_force(tmp_path):
    data = tmp_path / "data"
    skill_seed.seed(data, dry_run=False, only=["airtable"])
    again = skill_seed.seed(data, dry_run=False, only=["airtable"])
    assert again["written"] == []
    assert [row["action"] for row in again["rows"]] == ["exists"]
    forced = skill_seed.seed(data, dry_run=False, only=["airtable"], overwrite=True)
    assert forced["written"] == ["airtable"]


def test_seed_unknown_name_is_reported(tmp_path):
    result = skill_seed.seed(tmp_path / "data", only=["no-such-skill"])
    assert result["unknown"] == ["no-such-skill"]
    assert result["would_write"] == []


def test_seed_skips_unavailable_when_asked(tmp_path):
    """``--skip-unavailable`` 用真实探测（注入 ``which``/``env``/``find_spec``）。"""
    data = tmp_path / "data"
    baseline = skill_seed.seed(data, only=["apple-notes"])
    assert baseline["would_write"] == ["apple-notes"]  # 不探测就照写
    skipped = skill_seed.seed(
        data,
        only=["apple-notes"],
        skip_unavailable=True,
        which=lambda _cmd: None,
        env={},
        find_spec=lambda _module: None,
    )
    assert skipped["would_write"] == []
    assert [row["action"] for row in skipped["rows"]] == ["unavailable"]
    assert "memo" in skipped["rows"][0]["reason"]


def test_unavailable_uses_real_probes():
    """三个探测点全给"有"→ 一条都不算缺失；给"没有"→ 只有真声明过依赖的才报。"""
    env = {var: "1" for skill in skill_seed.SEED_CATALOG for var in skill.env_vars}
    assert skill_seed.unavailable(which=lambda cmd: f"/bin/{cmd}", env=env,
                                  find_spec=lambda m: object()) == []

    rows = skill_seed.unavailable(which=lambda _cmd: None, env={}, find_spec=lambda _m: None)
    assert len(rows) == len([skill for skill in skill_seed.SEED_CATALOG if skill.requires])


def test_opt_out_marker_blocks_seeding(tmp_path):
    data = tmp_path / "data"
    assert skill_seed.is_opt_out(data) is False
    assert skill_seed.set_opt_out(data, True) is True
    assert skill_seed.is_opt_out(data) is True
    assert skill_seed.set_opt_out(data, True) is False  # 幂等

    blocked = skill_seed.seed(data, dry_run=False, only=["airtable"])
    assert blocked["ok"] is False
    assert blocked["opted_out"] is True
    assert "opt-out" in blocked["error"]
    assert not (data / "skills" / "airtable").exists()

    # force 可以绕过 opt-out（但不能绕过 lint 门）
    forced = skill_seed.seed(data, dry_run=False, only=["airtable"], force=True)
    assert forced["written"] == ["airtable"]

    assert skill_seed.set_opt_out(data, False) is True
    assert skill_seed.is_opt_out(data) is False


# ── 播种清单 / 覆盖度 ──────────────────────────────────────────────────────


def test_manifest_records_hash_and_category(tmp_path):
    data = tmp_path / "data"
    skill_seed.seed(data, dry_run=False, only=["airtable", "arxiv"])
    manifest = skill_seed.read_manifest(data)
    assert sorted(manifest) == ["airtable", "arxiv"]
    entry = manifest["airtable"]
    assert entry["category"] == "productivity"
    assert len(entry["hash"]) == 64
    assert entry["source"].endswith("SKILL.md")
    assert skill_seed.seeded_names(data) == ("airtable", "arxiv")
    assert skill_seed.builtin_names(data) == frozenset({"airtable", "arxiv"})


def test_modified_detects_local_edits(tmp_path):
    data = tmp_path / "data"
    skill_seed.seed(data, dry_run=False, only=["airtable", "arxiv"])
    assert skill_seed.modified(data) == []

    path = data / "skills" / "airtable" / "SKILL.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n我加了一行\n", encoding="utf-8")
    rows = skill_seed.modified(data)
    assert [(row["name"], row["state"]) for row in rows] == [("airtable", "modified")]

    # 删掉一个 → 报 missing（不是"没改过"）
    (data / "skills" / "arxiv" / "SKILL.md").unlink()
    states = {row["name"]: row["state"] for row in skill_seed.modified(data)}
    assert states == {"airtable": "modified", "arxiv": "missing"}


def test_coverage_numbers_move_together(tmp_path):
    data = tmp_path / "data"
    before = skill_seed.coverage(data)
    assert before["skills"] == (0, 91)
    assert before["categories"] == (0, 20)
    assert before["duplicates"] == 3

    skill_seed.seed(data, dry_run=False, only=["airtable"])
    one = skill_seed.coverage(data)
    assert one["skills"] == (1, 91)
    assert one["categories"] == (1, 20)

    skill_seed.seed(data, dry_run=False)
    full = skill_seed.coverage(data)
    assert full["skills"] == (88, 91)
    assert full["categories"] == (20, 20)
    # 分母保持 91：让"源库 91 条、扁平落盘只能放 88 条"这个差额一直可见
    assert full["skills"][1] == len(skill_seed.SEED_CATALOG)


def test_seeded_skills_are_visible_to_the_injection_path(tmp_path):
    """播种的技能必须真的能被 `app/skills.py::catalog` 读到（系统提示注入的唯一来源）。"""
    from app.skills import catalog

    data = tmp_path / "data"
    skill_seed.seed(data, dry_run=False, only=["airtable"])
    rows = catalog(data / "skills", limit=100)
    assert [row["name"] for row in rows] == ["airtable"]
    assert rows[0]["description"] == skill_seed.find_seed("airtable").description


# ── CLI：seed / coverage（7.18）─────────────────────────────────────────────


def test_cli_skills_seed_previews_then_applies(tmp_path):
    data = tmp_path / "data"
    assert main(["--data-dir", str(data), "skills", "seed"]) == 0
    assert not (data / "skills").exists()

    assert main(["--data-dir", str(data), "skills", "seed", "--apply", "airtable", "arxiv"]) == 0
    assert (data / "skills" / "airtable" / "SKILL.md").is_file()
    assert (data / "skills" / "arxiv" / "SKILL.md").is_file()


def test_cli_skills_coverage(tmp_path):
    data = tmp_path / "data"
    assert main(["--data-dir", str(data), "skills", "coverage"]) == 0
    assert main(["--data-dir", str(data), "skills", "seed", "--apply", "airtable"]) == 0
    assert main(["--data-dir", str(data), "skills", "coverage"]) == 0


# ── CLI：7.13 的三条 partial 收尾 ──────────────────────────────────────────


def test_cli_skills_diff_and_list_modified(tmp_path):
    data = tmp_path / "data"
    assert main(["--data-dir", str(data), "skills", "seed", "--apply", "airtable"]) == 0
    # 未改过：无差异
    assert main(["--data-dir", str(data), "skills", "diff", "airtable"]) == 0
    path = data / "skills" / "airtable" / "SKILL.md"
    path.write_text(path.read_text(encoding="utf-8") + "\n本地补充一行\n", encoding="utf-8")
    assert main(["--data-dir", str(data), "skills", "diff", "airtable"]) == 0
    assert main(["--data-dir", str(data), "skills", "list-modified"]) == 0


def test_cli_skills_diff_rejects_non_builtin(tmp_path):
    data = tmp_path / "data"
    write_skill(data, "my-flow")
    assert main(["--data-dir", str(data), "skills", "diff", "my-flow"]) == 1


def test_cli_skills_reset_restore(tmp_path):
    data = tmp_path / "data"
    assert main(["--data-dir", str(data), "skills", "seed", "--apply", "airtable"]) == 0
    path = data / "skills" / "airtable" / "SKILL.md"
    original = path.read_text(encoding="utf-8")
    path.write_text(original + "\n脏改动\n", encoding="utf-8")

    # 不给 -y 时拒绝覆盖（不可逆动作要人点头）
    assert main(["--data-dir", str(data), "skills", "reset", "airtable", "--restore"]) == 1
    assert main(
        ["--data-dir", str(data), "skills", "reset", "airtable", "--restore", "-y"]
    ) == 0
    assert path.read_text(encoding="utf-8") == original


def test_cli_skills_repair_official_backfills_and_restores(tmp_path):
    data = tmp_path / "data"
    assert main(["--data-dir", str(data), "skills", "seed", "--apply", "airtable"]) == 0
    # 清单丢了（换机器拷过来的场景）→ 默认只回填，不动文件
    (data / skill_seed.MANIFEST_FILENAME).unlink()
    assert main(["--data-dir", str(data), "skills", "repair-official", "airtable"]) == 0
    assert "airtable" in skill_seed.read_manifest(data)

    # 被改过 + --restore → 覆盖回原版（要 -y）
    path = data / "skills" / "airtable" / "SKILL.md"
    path.write_text("---\nname: airtable\ndescription: 改了。\n---\n\n# x\n", encoding="utf-8")
    assert main(["--data-dir", str(data), "skills", "repair-official", "airtable"]) == 0
    assert main(
        ["--data-dir", str(data), "skills", "repair-official", "airtable", "--restore"]
    ) == 1
    assert main(
        ["--data-dir", str(data), "skills", "repair-official", "airtable", "--restore", "-y"]
    ) == 0
    assert "改了。" not in path.read_text(encoding="utf-8")


def test_cli_skills_config_disable_really_unloads(tmp_path):
    data = tmp_path / "data"
    write_skill(data, "flow")
    assert main(["--data-dir", str(data), "skills", "config"]) == 0
    assert main(["--data-dir", str(data), "skills", "config", "--disable", "flow"]) == 0
    assert read_disabled(data) == frozenset({"flow"})
    assert [record.name for record in SkillIndex(data).index().records] == []

    assert main(["--data-dir", str(data), "skills", "config", "--enable", "flow"]) == 0
    assert read_disabled(data) == frozenset()
    assert [record.name for record in SkillIndex(data).index().records] == ["flow"]


def test_cli_skills_config_rejects_unknown_skill(tmp_path):
    data = tmp_path / "data"
    write_skill(data, "flow")
    assert main(["--data-dir", str(data), "skills", "config", "--disable", "nope"]) == 1


def test_cli_skills_tap_roundtrip(tmp_path):
    data = tmp_path / "data"
    source = tmp_path / "shared-skills"
    write_skill(source.parent / "shared-skills", "tapped-flow")
    # write_skill 会落到 <dir>/skills/<name>，这里改造成 tap 目录本身的形状
    tapped = source / "tapped-flow"
    tapped.mkdir(parents=True, exist_ok=True)
    (tapped / "SKILL.md").write_text(CLEAN.format(name="tapped-flow"), encoding="utf-8")

    assert main(["--data-dir", str(data), "skills", "tap", "list"]) == 0
    assert main(["--data-dir", str(data), "skills", "tap", "add", str(source)]) == 0
    assert [row["name"] for row in read_taps(data)] == ["shared-skills"]
    # tap 真的参与扫描：索引里能看到它
    assert [record.name for record in SkillIndex(data).index().records] == ["tapped-flow"]

    assert main(["--data-dir", str(data), "skills", "tap", "remove", "shared-skills"]) == 0
    assert read_taps(data) == []
    assert SkillIndex(data).index().records == []


def test_cli_skills_tap_rejects_remote_repo(tmp_path):
    """远端仓库必须明确拒绝（无网络），并给出"先 clone 到本地"的做法。"""
    data = tmp_path / "data"
    assert main(["--data-dir", str(data), "skills", "tap", "add", "owner/repo"]) == 1


def test_cli_skills_audit_scans_hub_skills(tmp_path):
    data = tmp_path / "data"
    package = data / "skills-hub" / "demo"
    package.mkdir(parents=True)
    (package / "SKILL.md").write_text(CLEAN.format(name="demo"), encoding="utf-8")
    report = SkillHub(data).install("demo")
    assert report["ok"] is True
    assert main(["--data-dir", str(data), "skills", "audit"]) == 0
    assert main(["--data-dir", str(data), "skills", "audit", "--deep", "demo"]) == 0


def test_cli_skills_audit_without_hub_skills(tmp_path):
    assert main(["--data-dir", str(tmp_path / "data"), "skills", "audit"]) == 0


def test_cli_skills_opt_out_then_opt_in_sync(tmp_path):
    data = tmp_path / "data"
    assert main(["--data-dir", str(data), "skills", "seed", "--apply", "airtable"]) == 0
    assert main(["--data-dir", str(data), "skills", "opt-out"]) == 0
    assert skill_seed.is_opt_out(data) is True
    assert main(["--data-dir", str(data), "skills", "opt-out", "--remove"]) == 1  # 缺 -y
    assert main(["--data-dir", str(data), "skills", "opt-out", "--remove", "-y"]) == 0
    assert not (data / "skills" / "airtable").exists()

    assert main(["--data-dir", str(data), "skills", "opt-in"]) == 0
    assert skill_seed.is_opt_out(data) is False
    assert main(["--data-dir", str(data), "skills", "opt-in", "--sync"]) == 0
    assert (data / "skills" / "airtable" / "SKILL.md").is_file()
