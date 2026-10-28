"""bundles（技能别名集合）的测试：增删查、reload、以及「引用不存在的技能必须报错」。

最后一条是本模块的核心纪律：静默少装一个技能，用户看到的只是「这个 /bundle 好像
没起作用」，而真正的原因（名字打错了）永远不会被说出来。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.bundles import BUNDLES_FILENAME, MAX_BUNDLE_SKILLS, Bundle, Bundles, BundlesError
from app.cli.commands import skill_extra  # noqa: F401  （导入即接线）
from app.cli.main import main

SKILL_TEXT = (
    "---\n"
    "name: {name}\n"
    "description: {name} 的用途。\n"
    "version: 1\n"
    "---\n\n"
    "# 用法\n\n1. 做{name}这件事\n"
)


def write_skill(data: Path, name: str, *, body_extra: str = "") -> Path:
    directory = data / "skills" / name
    directory.mkdir(parents=True, exist_ok=True)
    text = SKILL_TEXT.format(name=name) + body_extra
    (directory / "SKILL.md").write_text(text, encoding="utf-8")
    return directory


def make_bundles(tmp_path) -> Bundles:
    return Bundles(tmp_path / "data")


# ── create / list / show / delete ───────────────────────────────────────────


def test_create_and_list(tmp_path):
    data = tmp_path / "data"
    write_skill(data, "alpha")
    write_skill(data, "beta")
    bundles = Bundles(data)

    bundle = bundles.create("daily", ["alpha", "beta"], description="日常两件套")
    assert bundle.name == "daily"
    assert bundle.skills == ("alpha", "beta")
    assert bundle.created_at, "created_at 必须落盘，否则「什么时候建的」没人知道"

    items = bundles.list()
    assert [item.name for item in items] == ["daily"]
    assert items[0].description == "日常两件套"
    assert (data / BUNDLES_FILENAME).is_file()


def test_create_rejects_duplicate_without_force(tmp_path):
    data = tmp_path / "data"
    write_skill(data, "alpha")
    bundles = Bundles(data)
    bundles.create("daily", ["alpha"])
    with pytest.raises(BundlesError, match="已存在"):
        bundles.create("daily", ["alpha"])
    # force 覆盖，且 created_at 保持不变（覆盖不是重建）
    first = bundles.show("daily")
    again = bundles.create("daily", ["alpha"], force=True)
    assert again.created_at == first.created_at


def test_create_lists_every_missing_skill(tmp_path):
    data = tmp_path / "data"
    write_skill(data, "alpha")
    bundles = Bundles(data)
    with pytest.raises(BundlesError) as excinfo:
        bundles.create("daily", ["alpha", "ghost-one", "ghost-two"])
    message = str(excinfo.value)
    assert "ghost-one" in message and "ghost-two" in message, "缺失要一次全列出来"
    assert "alpha" not in message.split("：")[-1], "存在的技能不该被列进缺失清单"


def test_create_rejects_illegal_name(tmp_path):
    data = tmp_path / "data"
    write_skill(data, "alpha")
    bundles = Bundles(data)
    with pytest.raises(BundlesError, match="不合规"):
        bundles.create("Daily Report", ["alpha"])


def test_create_requires_at_least_one_skill(tmp_path):
    with pytest.raises(BundlesError, match="至少要引用一个技能"):
        make_bundles(tmp_path).create("daily", [])


def test_create_rejects_too_many_skills(tmp_path):
    data = tmp_path / "data"
    names = [f"s{index}" for index in range(MAX_BUNDLE_SKILLS + 1)]
    for name in names:
        write_skill(data, name)
    with pytest.raises(BundlesError, match="上限"):
        Bundles(data).create("everything", names)


def test_show_unknown_bundle_lists_known(tmp_path):
    data = tmp_path / "data"
    write_skill(data, "alpha")
    bundles = Bundles(data)
    bundles.create("daily", ["alpha"])
    with pytest.raises(BundlesError) as excinfo:
        bundles.show("ghost")
    assert "daily" in str(excinfo.value), "报错要顺手告诉人现在有什么"


def test_delete(tmp_path):
    data = tmp_path / "data"
    write_skill(data, "alpha")
    bundles = Bundles(data)
    bundles.create("daily", ["alpha"])
    assert bundles.delete("daily") is True
    assert bundles.list() == []
    assert bundles.delete("daily") is False
    assert (data / "skills" / "alpha" / "SKILL.md").is_file(), "删 bundle 不该删技能本身"


def test_instruction_is_persisted(tmp_path):
    """`--instruction` 是 Hermes 的选项，必须真的落盘（而不是被静默丢掉）。"""
    data = tmp_path / "data"
    write_skill(data, "alpha")
    bundles = Bundles(data)
    bundles.create("daily", ["alpha"], instruction="先读 README")
    payload = json.loads((data / BUNDLES_FILENAME).read_text(encoding="utf-8"))
    assert payload["bundles"]["daily"]["instruction"] == "先读 README"
    assert bundles.show("daily").instruction == "先读 README"


def test_broken_bundle_file_degrades_to_empty(tmp_path):
    data = tmp_path / "data"
    data.mkdir(parents=True)
    (data / BUNDLES_FILENAME).write_text("{ not json", encoding="utf-8")
    assert Bundles(data).list() == []


# ── reload ──────────────────────────────────────────────────────────────────


def test_reload_rereads_skill_files(tmp_path):
    data = tmp_path / "data"
    write_skill(data, "alpha")
    write_skill(data, "beta")
    bundles = Bundles(data)
    bundles.create("daily", ["alpha", "beta"])

    # 手工改文件（模拟用户直接编辑 SKILL.md）
    (data / "skills" / "alpha" / "SKILL.md").write_text(
        SKILL_TEXT.format(name="alpha") + "\n## 新增一节\n\n多了三行\n", encoding="utf-8"
    )
    report = bundles.reload("daily")
    assert report.missing == []
    by_skill = {item["skill"]: item for item in report.reloaded}
    assert set(by_skill) == {"alpha", "beta"}
    assert by_skill["alpha"]["body_lines"] > by_skill["beta"]["body_lines"]


def test_reload_reports_missing_skills_without_raising(tmp_path):
    data = tmp_path / "data"
    write_skill(data, "alpha")
    write_skill(data, "beta")
    bundles = Bundles(data)
    bundles.create("daily", ["alpha", "beta"])

    import shutil

    shutil.rmtree(data / "skills" / "beta")
    report = bundles.reload("daily")
    assert report.missing == ["daily:beta"], "reload 是观察动作，必须能报告「少了什么」"
    assert [item["skill"] for item in report.reloaded] == ["alpha"]


def test_reload_all_bundles(tmp_path):
    data = tmp_path / "data"
    write_skill(data, "alpha")
    bundles = Bundles(data)
    bundles.create("one", ["alpha"])
    bundles.create("two", ["alpha"])
    report = bundles.reload()
    assert {item["bundle"] for item in report.reloaded} == {"one", "two"}


# ── load（斜杠命令装载）─────────────────────────────────────────────────────


def test_load_returns_instruction_and_bodies(tmp_path):
    data = tmp_path / "data"
    write_skill(data, "alpha")
    bundles = Bundles(data)
    bundles.create("daily", ["alpha"], instruction="先读 README")
    payload = bundles.load("daily")
    assert payload["instruction"] == "先读 README"
    assert [item["name"] for item in payload["skills"]] == ["alpha"]
    assert "做alpha这件事" in payload["skills"][0]["body"]


def test_load_reports_missing_instead_of_silently_loading_fewer(tmp_path):
    data = tmp_path / "data"
    write_skill(data, "alpha")
    write_skill(data, "beta")
    bundles = Bundles(data)
    bundles.create("daily", ["alpha", "beta"])

    import shutil

    shutil.rmtree(data / "skills" / "beta")
    with pytest.raises(BundlesError, match="beta"):
        bundles.load("daily")


def test_bundle_dataclass_round_trip():
    bundle = Bundle(name="daily", description="d", skills=("a", "b"), created_at="2026-01-01")
    assert Bundle.from_dict(bundle.as_dict()) == bundle


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_bundles_lifecycle(tmp_path, capsys):
    data = tmp_path / "data"
    write_skill(data, "alpha")
    write_skill(data, "beta")

    assert main(["--data-dir", str(data), "bundles", "list"]) == 0
    assert "还没有技能包" in capsys.readouterr().out

    assert main(
        [
            "--data-dir", str(data), "bundles", "create", "daily",
            "--skill", "alpha", "--skill", "beta", "-d", "日常两件套",
        ]
    ) == 0
    assert "已创建技能包" in capsys.readouterr().out

    assert main(["--data-dir", str(data), "bundles", "list"]) == 0
    assert "daily" in capsys.readouterr().out

    assert main(["--data-dir", str(data), "bundles", "show", "daily"]) == 0
    out = capsys.readouterr().out
    assert "alpha" in out and "beta" in out

    assert main(["--data-dir", str(data), "bundles", "reload"]) == 0
    assert "重载 2 个技能引用" in capsys.readouterr().out

    assert main(["--data-dir", str(data), "bundles", "delete", "daily"]) == 0
    assert "已删除技能包" in capsys.readouterr().out
    assert (data / "skills" / "alpha" / "SKILL.md").is_file()


def test_cli_bundles_create_requires_skill_flag(tmp_path, capsys):
    data = tmp_path / "data"
    write_skill(data, "alpha")
    code = main(["--data-dir", str(data), "bundles", "create", "daily"])
    assert code == 1
    assert "--skill" in capsys.readouterr().err


def test_cli_bundles_create_lists_missing_skills(tmp_path, capsys):
    data = tmp_path / "data"
    write_skill(data, "alpha")
    code = main(["--data-dir", str(data), "bundles", "create", "daily", "-s", "ghost"])
    assert code == 1
    assert "ghost" in capsys.readouterr().err


def test_cli_bundles_delete_unknown(tmp_path, capsys):
    data = tmp_path / "data"
    code = main(["--data-dir", str(data), "bundles", "delete", "ghost"])
    assert code == 1
    assert "没有这个技能包" in capsys.readouterr().err


def test_cli_bundles_reload_reports_missing(tmp_path, capsys):
    data = tmp_path / "data"
    write_skill(data, "alpha")
    assert main(["--data-dir", str(data), "bundles", "create", "daily", "-s", "alpha"]) == 0
    capsys.readouterr()

    import shutil

    shutil.rmtree(data / "skills" / "alpha")
    code = main(["--data-dir", str(data), "bundles", "reload"])
    assert code == 1
    captured = capsys.readouterr()
    assert "daily:alpha" in captured.err


def test_cli_bundles_json_payload(tmp_path, capsys):
    data = tmp_path / "data"
    write_skill(data, "alpha")
    assert main(["--data-dir", str(data), "bundles", "create", "daily", "-s", "alpha"]) == 0
    capsys.readouterr()
    assert main(["--data-dir", str(data), "--json", "bundles", "list"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["bundles"][0]["name"] == "daily"
