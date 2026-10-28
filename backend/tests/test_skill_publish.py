"""`orbit skills publish`（矩阵 7.13 的最后一条）的契约测试。

锁住四件事：

1. **默认 dry_run 不写盘**——发布是对外可见的动作，默认就该先看后做；
2. `--apply` 写出的 `manifest.json` 里，`content_hash` 必须与源目录内容对得上，
   而且**发布物自己的指纹**不能被 `manifest.json` 影响（否则 `skills update` 会对
   每个从 hub 装的技能永久显示"有更新"）；
3. **lint 有 ERROR 就拒发**——发布的门槛比安装严（别人没有机会在你的隔离区复查）；
4. 远程目标（`github` / `clawhub` / URL）**如实报未实现**，并给出本地替代。
"""

from __future__ import annotations

import json

import pytest

from app.skill_hub import (
    MANIFEST_FILENAME,
    SkillHub,
    SkillHubError,
    dir_hash,
    is_remote_target,
)

GOOD_SKILL = """---
name: demo
scope: global
version: 3
description: "演示技能：把 x 变成 y"
triggers:
  - 演示
---

# 演示

把 x 变成 y 的固定流程。
"""

BAD_SKILL = """这段没有 frontmatter，linter 会报 frontmatter-invalid（ERROR）。
"""


def _hub(tmp_path) -> SkillHub:
    return SkillHub(tmp_path / "data")


def _skill(tmp_path, name: str = "demo", *, text: str = GOOD_SKILL, extra=()):
    """在 `<data>/skills/<name>/` 造一个已安装技能（发布的最常见来源）。"""
    folder = tmp_path / "data" / "skills" / name
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(text, encoding="utf-8")
    for filename, body in extra:
        path = folder / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return folder


# ── 1. dry_run 不写盘 ───────────────────────────────────────────────────────


def test_dry_run_reports_everything_and_writes_nothing(tmp_path):
    hub = _hub(tmp_path)
    _skill(tmp_path)
    report = hub.publish("demo")
    assert report["dry_run"] is True and report["wrote"] is False
    # 报告里要有"将发布什么、hash 是多少、写到哪"——缺一样用户就没法判断
    assert report["content_hash"] == dir_hash(tmp_path / "data" / "skills" / "demo")
    assert report["target"] == str(hub.hub_root / "demo")
    assert report["manifest"]["name"] == "demo"
    assert report["manifest"]["version"] == 3
    assert report["manifest"]["content_hash"] == report["content_hash"]
    assert report["manifest"]["triggers"] == ["演示"]
    assert report["lint"]["errors"] == 0
    # 一个字都不该落盘（连 hub 目录都不该被创建）
    assert not (hub.hub_root / "demo").exists()


# ── 2. --apply 写出 manifest，hash 正确 ─────────────────────────────────────


def test_apply_writes_manifest_with_a_correct_hash(tmp_path):
    hub = _hub(tmp_path)
    _skill(tmp_path, extra=(("references/note.md", "参考"),))
    expected = dir_hash(tmp_path / "data" / "skills" / "demo")
    report = hub.publish("demo", dry_run=False)
    assert report["wrote"] is True
    target = hub.hub_root / "demo"
    assert (target / "SKILL.md").is_file()
    manifest = json.loads((target / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["content_hash"] == expected
    assert manifest["files"] == 2  # SKILL.md + references/note.md
    assert manifest["published_at"]
    # 关键：发布物自己的指纹必须等于源目录的指纹。`manifest.json` 是发布元数据，
    # 不算内容——否则 `dir_hash(hub 包) != dir_hash(已安装副本)`，`skills update`
    # 会对每个从 hub 装的技能永久报"有更新"。
    assert dir_hash(target) == expected


def test_published_package_installs_back_without_a_false_update(tmp_path):
    hub = _hub(tmp_path)
    _skill(tmp_path)
    hub.publish("demo", dry_run=False)
    # 把源目录挪走，确保 install 真的从 hub 装
    source = tmp_path / "data" / "skills" / "demo"
    backup = tmp_path / "backup"
    source.rename(backup)
    result = hub.install("demo")
    assert result["ok"] is True and result["verdict"] == "safe"
    # 刚装完不该显示"有更新"（这正是排除 manifest.json 的理由）
    assert [row["outdated"] for row in hub.updates()] == [False]


def test_publish_uses_the_frontmatter_name_not_the_folder_name(tmp_path):
    """技能的身份是 frontmatter 的 `name`：目录叫 `my-work` 也要发成 `demo`。"""
    hub = _hub(tmp_path)
    folder = tmp_path / "somewhere" / "my-work"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(GOOD_SKILL, encoding="utf-8")
    report = hub.publish(folder, dry_run=False)
    assert report["name"] == "demo"
    assert (hub.hub_root / "demo" / "SKILL.md").is_file()


def test_republishing_reports_overwrite_and_change(tmp_path):
    hub = _hub(tmp_path)
    folder = _skill(tmp_path)
    hub.publish("demo", dry_run=False)
    again = hub.publish("demo", dry_run=False)
    assert again["overwrites"] is True and again["changed"] is False
    (folder / "SKILL.md").write_text(GOOD_SKILL + "\n加一行。\n", encoding="utf-8")
    changed = hub.publish("demo", dry_run=False)
    assert changed["overwrites"] is True and changed["changed"] is True
    # 整份替换：旧版本的残留文件不能留在发布物里（否则 hash 与源对不上）
    assert dir_hash(hub.hub_root / "demo") == changed["content_hash"]


def test_publish_to_an_explicit_local_directory(tmp_path):
    hub = _hub(tmp_path)
    _skill(tmp_path)
    out = tmp_path / "elsewhere"
    report = hub.publish("demo", to=out, dry_run=False)
    assert report["target"] == str(out / "demo")
    assert (out / "demo" / MANIFEST_FILENAME).is_file()


# ── 3. lint 门禁 ────────────────────────────────────────────────────────────


def test_lint_errors_block_the_publish(tmp_path):
    hub = _hub(tmp_path)
    _skill(tmp_path, text=BAD_SKILL)
    with pytest.raises(SkillHubError, match="lint 有 ERROR"):
        hub.publish("demo", dry_run=False)
    # 被拒的发布不该留下半份产物
    assert not (hub.hub_root / "demo").exists()


def test_lint_errors_block_even_the_dry_run(tmp_path):
    """dry-run 也要过门禁：先让你看到"这条根本发不出去"，而不是看完报告再撞墙。"""
    hub = _hub(tmp_path)
    _skill(tmp_path, text=BAD_SKILL)
    with pytest.raises(SkillHubError, match="lint 有 ERROR"):
        hub.publish("demo")


# ── 4. 远程目标如实报未实现 ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "target",
    ("github", "clawhub", "https://example.com/hub", "git@github.com:me/skills.git", "github/me/x"),
)
def test_remote_targets_are_reported_as_unimplemented(tmp_path, target):
    hub = _hub(tmp_path)
    _skill(tmp_path)
    assert is_remote_target(target) is True
    with pytest.raises(SkillHubError, match="远程发布未实现") as failure:
        hub.publish("demo", to=target)
    message = str(failure.value)
    assert "网络" in message and "社区索引" in message
    assert "--to <本地目录>" in message  # 必须给本地替代做法


def test_relative_local_target_is_not_mistaken_for_remote(tmp_path):
    """`mlops/eval-harness` 这种相对路径不是注册表写法，不能被误判成远程。"""
    assert is_remote_target("mlops/eval-harness") is False
    assert is_remote_target("") is False
    assert is_remote_target(None) is False


def test_missing_skill_is_reported_with_a_hint(tmp_path):
    hub = _hub(tmp_path)
    with pytest.raises(SkillHubError, match="本地 hub 里没有"):
        hub.publish("nope")


# ── CLI ─────────────────────────────────────────────────────────────────────


def _run_cli(tmp_path, capsys, *argv):
    """跑一次真 CLI（先导入 skill_extra3：生产接线里由 commands/__init__.py 负责）。"""
    import app.cli.commands.skill_extra3  # noqa: F401 - 导入即接线
    from app.cli.main import main

    code = main(["--data-dir", str(tmp_path / "data"), *argv])
    return code, capsys.readouterr()


def test_cli_publish_defaults_to_dry_run(tmp_path, capsys):
    _skill(tmp_path)
    code, captured = _run_cli(tmp_path, capsys, "skills", "publish", "demo")
    assert code == 0
    assert "将发布 demo" in captured.out
    assert "内容 hash" in captured.out
    assert "[dry-run] 未写盘" in captured.out
    assert not (tmp_path / "data" / "skills-hub" / "demo").exists()


def test_cli_publish_apply_writes_the_package(tmp_path, capsys):
    _skill(tmp_path)
    code, captured = _run_cli(tmp_path, capsys, "skills", "publish", "demo", "--apply")
    assert code == 0
    assert "已发布 demo" in captured.out
    assert (tmp_path / "data" / "skills-hub" / "demo" / MANIFEST_FILENAME).is_file()


def test_cli_publish_remote_exits_3(tmp_path, capsys):
    _skill(tmp_path)
    code, captured = _run_cli(tmp_path, capsys, "skills", "publish", "demo", "--to", "github")
    assert code == 3
    assert "远程发布未实现" in captured.err


def test_cli_publish_repo_flag_is_reported_as_unimplemented(tmp_path, capsys):
    _skill(tmp_path)
    code, captured = _run_cli(
        tmp_path, capsys, "skills", "publish", "demo", "--to", "out", "--repo", "me/x"
    )
    assert code == 3
    assert "未实现" in captured.err


def test_cli_publish_lint_failure_exits_1(tmp_path, capsys):
    _skill(tmp_path, text=BAD_SKILL)
    code, captured = _run_cli(tmp_path, capsys, "skills", "publish", "demo", "--apply")
    assert code == 1
    assert "lint 有 ERROR" in captured.err
