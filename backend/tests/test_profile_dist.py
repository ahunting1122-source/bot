"""profile distribution（矩阵 9.16）的契约测试。

锁住六件事：

1. **13 条操作一条不少**（`OPERATIONS` 里的名字都能在 `ProfileDist` 上找到方法）；
2. `use()` **必须报代价**——配置项 from→to、画像条目增删、技能增删，安全边界项单独标出；
3. `install` **只接受本地目录**（远程如实报未实现），且**不覆盖生效中的那套**；
4. `update` **保留用户数据**（改过的 USER.md、用户自己的文件、清单外的文件都不动）；
5. `install` 的白名单真的拦住了会改沙箱边界的键（`data_dir` 之类）；
6. CLI 侧的 10 条 distribution 路径真的接上了（`orbit profile list/use/…`）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.config import Settings
from app.profile_dist import (
    DIST_SETTINGS_KEYS,
    OPERATIONS,
    ProfileDist,
    ProfileDistError,
    looks_like_remote_ref,
    looks_remote,
)

# ── 工具 ────────────────────────────────────────────────────────────────────


def _dist(tmp_path) -> ProfileDist:
    return ProfileDist(tmp_path / "data")


def _source(
    tmp_path, name: str = "src", *, settings=None, user_md: str = "", skills=()
) -> Path:
    """造一份"别人的发行包"（本地目录形态）。"""
    root = Path(tmp_path) / name
    root.mkdir(parents=True, exist_ok=True)
    if settings is not None:
        (root / "settings.json").write_text(
            json.dumps(settings, ensure_ascii=False), encoding="utf-8"
        )
    if user_md:
        (root / "USER.md").write_text(user_md, encoding="utf-8")
    for skill in skills:
        folder = root / "skills" / skill
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "SKILL.md").write_text("# x\n", encoding="utf-8")
    return root


#: 一份典型的发行包内容：两个配置项 + 两条画像 + 一个技能。
_SETTINGS = {"model": "gpt-x", "approval_mode": "manual", "file_scope": "workspace"}
_USER_MD = "# 用户画像\n\n§ 做短剧\n§ 喜欢中文\n"


# ── 13 条操作都在 ───────────────────────────────────────────────────────────


def test_all_thirteen_operations_exist():
    """13 条一条不少——这是"9.16 做完了"唯一可验证的判据。"""
    assert len(OPERATIONS) == 13
    dist = ProfileDist("data")
    for cli_name, method in OPERATIONS:
        assert callable(getattr(dist, method)), f"{cli_name} → {method} 不存在"
    # `import` 是 Python 关键字，所以方法名必须是 `import_profile`；两者都要能对上。
    assert dict(OPERATIONS)["import"] == "import_profile"


def test_dist_settings_keys_are_real_settings_fields():
    """白名单里的键必须都是 `Settings` 的真字段。

    抄错一个键名的后果是静默的：`install` 会把那一项当"未知键"忽略掉，用户看到
    "装上了但配置没变"，而真正的原因是一个拼写错误。
    """
    fields = set(Settings.model_fields)
    unknown = [key for key in DIST_SETTINGS_KEYS if key not in fields]
    assert not unknown, f"这些键不在 Settings 里：{unknown}"
    # 反过来：会改沙箱边界的键**必须**不在白名单里。
    for key in ("data_dir", "workspace", "model_api_key", "github_token"):
        assert key not in DIST_SETTINGS_KEYS


# ── 1. list ─────────────────────────────────────────────────────────────────


def test_list_is_empty_then_shows_installed(tmp_path):
    dist = _dist(tmp_path)
    assert dist.list() == []
    assert dist.status()["active"] == ""
    dist.install(_source(tmp_path, settings=_SETTINGS), name="work", yes=True)
    rows = dist.list()
    assert [row["name"] for row in rows] == ["work"]
    assert rows[0]["active"] is False
    assert rows[0]["source"]


# ── 2. use ──────────────────────────────────────────────────────────────────


def test_use_reports_what_will_change(tmp_path):
    """`use()` 必须报代价：配置项逐键、画像条目、技能清单。"""
    dist = _dist(tmp_path)
    dist.install(_source(tmp_path, "a", settings={"model": "gpt-a"}), name="a", yes=True)
    dist.install(
        _source(
            tmp_path,
            "b",
            settings={"model": "gpt-b", "approval_mode": "off"},
            user_md=_USER_MD,
            skills=("demo",),
        ),
        name="b",
        yes=True,
    )
    dist.use("a")
    result = dist.use("b")
    assert result["changed"] is True
    assert result["previous"] == "a"
    settings = {row["key"]: row for row in result["changes"]["settings"]}
    assert settings["model"]["from"] == "gpt-a" and settings["model"]["to"] == "gpt-b"
    # 安全边界项要单独标出来（`approval_mode: off` 等于以后不再问用户）
    assert settings["approval_mode"]["safety"] is True
    assert settings["model"]["safety"] is False
    assert result["changes"]["user_profile"]["added"] == ["做短剧", "喜欢中文"]
    assert result["changes"]["skills"]["added"] == ["demo"]
    # 代价之外还要说清"什么时候生效"和"怎么回退"
    joined = " ".join(result["warnings"])
    assert "装配" in joined
    assert "orbit profile use a" in joined
    assert any("安全边界" in item for item in result["warnings"])
    assert (dist.root / "active").read_text(encoding="utf-8").strip() == "b"


def test_use_is_idempotent_and_rejects_unknown(tmp_path):
    dist = _dist(tmp_path)
    dist.install(_source(tmp_path, settings=_SETTINGS), name="work", yes=True)
    dist.use("work")
    again = dist.use("work")
    assert again["changed"] is False
    assert again["warnings"]
    with pytest.raises(ProfileDistError, match="没有名为"):
        dist.use("nope")


# ── 3. create ───────────────────────────────────────────────────────────────


def test_create_makes_an_empty_bundle_and_clones_on_demand(tmp_path):
    dist = _dist(tmp_path)
    made = dist.create("scratch", description="试验用", alias=False)
    assert made["path"] and (tmp_path / "data" / "profiles" / "scratch" / "profile.json").is_file()
    assert dist.record("scratch").description == "试验用"
    assert dist.settings_of("scratch") == {}

    dist.install(_source(tmp_path, settings=_SETTINGS, skills=("demo",)), name="work", yes=True)
    dist.create("copy", clone_from="work", skills=True, alias=False)
    assert dist.settings_of("copy")["model"] == "gpt-x"
    assert dist.skills_of("copy") == ["demo"]

    with pytest.raises(ProfileDistError, match="已存在"):
        dist.create("scratch")


# ── 4. delete ───────────────────────────────────────────────────────────────


def test_delete_needs_yes_and_refuses_the_active_one(tmp_path):
    dist = _dist(tmp_path)
    dist.install(_source(tmp_path, settings=_SETTINGS), name="work", yes=True)
    with pytest.raises(ProfileDistError, match="--yes"):
        dist.delete("work")
    dist.use("work")
    with pytest.raises(ProfileDistError, match="正在生效"):
        dist.delete("work", yes=True)
    dist.create("other", alias=False)
    dist.use("other")
    assert dist.delete("work", yes=True)["removed"] is True
    assert dist.names() == ["other"]


# ── 5. describe ─────────────────────────────────────────────────────────────


def test_describe_reads_sets_and_needs_overwrite(tmp_path):
    dist = _dist(tmp_path)
    dist.create("work", alias=False)
    assert dist.describe("work")["description"] == ""
    dist.describe("work", text="日常用的那套")
    assert dist.describe("work")["description"] == "日常用的那套"
    with pytest.raises(ProfileDistError, match="--overwrite"):
        dist.describe("work", text="换一句")
    assert dist.describe("work", text="换一句", overwrite=True)["description"] == "换一句"
    # `--all` 要能列出全部（包括没写描述的，否则"哪些还没写"看不出来）
    dist.create("second", alias=False)
    rows = dist.describe(all=True)["profiles"]
    assert [row["name"] for row in rows] == ["second", "work"]
    assert rows[0]["description"] == ""


def test_describe_auto_counts_the_bundle(tmp_path):
    dist = _dist(tmp_path)
    dist.install(
        _source(tmp_path, settings=_SETTINGS, user_md=_USER_MD), name="work", yes=True
    )
    auto = dist.describe("work", auto=True)["description"]
    assert "3 项配置" in auto and "2 条画像" in auto


# ── 6. show ─────────────────────────────────────────────────────────────────


def test_show_summarises_the_bundle(tmp_path):
    dist = _dist(tmp_path)
    dist.install(
        _source(tmp_path, settings=_SETTINGS, user_md=_USER_MD, skills=("demo",)),
        name="work",
        yes=True,
    )
    info = dist.show("work")
    assert info["name"] == "work" and info["active"] is False
    assert info["settings_count"] == 3 and info["skills_count"] == 1
    assert info["user_entries"] == ["做短剧", "喜欢中文"]
    assert info["has_settings"] is True and info["has_user_profile"] is True


# ── 7. alias ────────────────────────────────────────────────────────────────


def test_alias_writes_wrapper_scripts_and_removes_them(tmp_path):
    dist = _dist(tmp_path)
    dist.install(_source(tmp_path, settings=_SETTINGS), name="work", yes=True)
    made = dist.alias("work")
    assert len(made["paths"]) == 2
    cmd = (tmp_path / "data" / "profiles" / "aliases" / "orbit-work.cmd").read_text(
        encoding="utf-8"
    )
    # 真机制：settings 片段被翻译成 ORBIT_<KEY>，pydantic-settings 的 env_prefix 认它
    assert "ORBIT_MODEL=gpt-x" in cmd
    assert "ORBIT_APPROVAL_MODE=manual" in cmd
    assert "orbit" in cmd and "--data-dir" in cmd
    # 覆盖范围必须如实：画像与技能不在环境变量能表达的范围内
    assert made["covers"] and made["not_covers"]
    removed = dist.alias("work", remove=True)
    assert removed["removed"] is True and removed["paths"]
    assert not (tmp_path / "data" / "profiles" / "aliases" / "orbit-work.cmd").exists()


# ── 8. rename ───────────────────────────────────────────────────────────────


def test_rename_moves_dir_record_and_active_marker(tmp_path):
    dist = _dist(tmp_path)
    dist.install(_source(tmp_path, settings=_SETTINGS), name="work", yes=True)
    dist.use("work")
    dist.alias("work")
    result = dist.rename("work", "office")
    assert result["active"] is True
    assert dist.names() == ["office"]
    assert (dist.root / "active").read_text(encoding="utf-8").strip() == "office"
    assert dist.record("office").name == "office"
    # 旧名的包装脚本要跟着改，否则 `orbit-work` 指向一个不存在的 profile
    assert not (tmp_path / "data" / "profiles" / "aliases" / "orbit-work.cmd").exists()
    assert (tmp_path / "data" / "profiles" / "aliases" / "orbit-office.cmd").is_file()
    with pytest.raises(ProfileDistError, match="已被占用"):
        dist.rename("office", "office")


# ── 9. export ───────────────────────────────────────────────────────────────


def test_export_writes_a_manifest_with_hashes(tmp_path):
    dist = _dist(tmp_path)
    dist.install(
        _source(tmp_path, settings=_SETTINGS, user_md=_USER_MD, skills=("demo",)),
        name="work",
        yes=True,
    )
    out = tmp_path / "out" / "work"
    result = dist.export("work", output=out)
    assert "manifest.json" in result["files"]
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "work"
    assert set(manifest["files"]) == {"settings.json", "USER.md", "skills.json"}
    assert (out / "USER.md").is_file()
    with pytest.raises(ProfileDistError, match="非空"):
        dist.export("work", output=out)


# ── 10. import ──────────────────────────────────────────────────────────────


def test_import_verifies_the_manifest_and_installs(tmp_path):
    dist = _dist(tmp_path)
    dist.install(_source(tmp_path, settings=_SETTINGS), name="work", yes=True)
    archive = tmp_path / "out" / "work"
    dist.export("work", output=archive)
    result = dist.import_profile(archive, name="restored")
    assert result["verified"] is True
    assert result["name"] == "restored"
    assert dist.settings_of("restored")["model"] == "gpt-x"


def test_import_refuses_a_tampered_archive(tmp_path):
    dist = _dist(tmp_path)
    dist.install(_source(tmp_path, settings=_SETTINGS), name="work", yes=True)
    archive = tmp_path / "out" / "work"
    dist.export("work", output=archive)
    # 传递过程中被改过：settings.json 多了一行
    (archive / "settings.json").write_text(
        json.dumps({**_SETTINGS, "model": "evil"}, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(ProfileDistError, match="指纹与 manifest.json 不符"):
        dist.import_profile(archive, name="restored")


# ── 11. install ─────────────────────────────────────────────────────────────


def test_install_accepts_local_dir_only(tmp_path):
    dist = _dist(tmp_path)
    # 带协议头的远程 + Hermes 的 `owner/repo` 注册表写法：一律如实报未实现
    for remote in (
        "https://github.com/nous/hermes-profile",
        "git@github.com:nous/x.git",
        "nous/hermes-profile",
    ):
        with pytest.raises(ProfileDistError, match="远程安装未实现"):
            dist.install(remote, yes=True)
    assert looks_remote("https://github.com/nous/x") is True
    assert looks_remote("nous/hermes-profile") is False  # 只有形状，不算协议头远程
    assert looks_like_remote_ref("nous/hermes-profile") is True
    # 判定顺序是"**先看磁盘**"：磁盘上真有的目录一律当本地，不会被 owner/repo 形状误伤
    local = tmp_path / "profiles" / "work"
    local.mkdir(parents=True)
    (local / "settings.json").write_text(json.dumps({"model": "x"}), encoding="utf-8")
    assert dist.install(local, name="local-one", yes=True)["name"] == "local-one"
    with pytest.raises(ProfileDistError, match="找不到目录"):
        dist.install(tmp_path / "nope", yes=True)


def test_install_refuses_to_overwrite_the_active_profile(tmp_path):
    dist = _dist(tmp_path)
    source = _source(tmp_path, settings=_SETTINGS)
    dist.install(source, name="work", yes=True)
    dist.use("work")
    # 生效中的那套被就地覆盖 = 当前会话的安全边界在背后被换掉，force 也不行
    with pytest.raises(ProfileDistError, match="正在生效"):
        dist.install(source, name="work", yes=True, force=True)
    assert dist.settings_of("work")["model"] == "gpt-x"


def test_install_ignores_settings_outside_the_whitelist(tmp_path):
    dist = _dist(tmp_path)
    source = _source(
        tmp_path,
        settings={"model": "gpt-x", "data_dir": "/tmp/evil", "workspace": "/tmp/evil"},
    )
    result = dist.install(source, name="work", yes=True)
    assert result["ignored_settings"] == ["data_dir", "workspace"]
    assert dist.settings_of("work") == {"model": "gpt-x"}
    assert any("被忽略" in note for note in result["notes"])


# ── 12. update ──────────────────────────────────────────────────────────────


def test_update_preserves_user_data(tmp_path):
    dist = _dist(tmp_path)
    source = _source(tmp_path, settings={"model": "gpt-x"}, user_md="# 用户画像\n\n§ 原条目\n")
    dist.install(source, name="work", yes=True)
    bundle = tmp_path / "data" / "profiles" / "work"
    (bundle / "USER.md").write_text("# 用户画像\n\n§ 我改过的\n", encoding="utf-8")
    (bundle / "notes.md").write_text("我自己的笔记", encoding="utf-8")
    (source / "settings.json").write_text(
        json.dumps({"model": "gpt-y"}, ensure_ascii=False), encoding="utf-8"
    )
    result = dist.update("work")
    assert "settings.json" in result["refreshed"]
    assert result["preserved"] == ["USER.md"]
    assert dist.settings_of("work")["model"] == "gpt-y"
    # 用户数据一字不动
    assert "我改过的" in (bundle / "USER.md").read_text(encoding="utf-8")
    assert (bundle / "notes.md").read_text(encoding="utf-8") == "我自己的笔记"
    # 再跑一次仍然是"保留"，不是"第一次保留、第二次覆盖"
    assert dist.update("work")["preserved"] == ["USER.md"]
    assert "我改过的" in (bundle / "USER.md").read_text(encoding="utf-8")


def test_update_force_config_needs_yes_for_modified_settings(tmp_path):
    dist = _dist(tmp_path)
    source = _source(tmp_path, settings={"model": "gpt-x"})
    dist.install(source, name="work", yes=True)
    bundle = tmp_path / "data" / "profiles" / "work"
    (bundle / "settings.json").write_text(
        json.dumps({"model": "hand-edited"}, ensure_ascii=False), encoding="utf-8"
    )
    with pytest.raises(ProfileDistError, match="--yes"):
        dist.update("work", force_config=True)
    assert dist.settings_of("work")["model"] == "hand-edited"
    result = dist.update("work", force_config=True, yes=True)
    assert "settings.json" in result["refreshed"]


def test_update_without_source_is_refused(tmp_path):
    dist = _dist(tmp_path)
    dist.create("scratch", alias=False)
    with pytest.raises(ProfileDistError, match="没有来源"):
        dist.update("scratch")


# ── 13. info ────────────────────────────────────────────────────────────────


def test_info_lists_files_and_drift(tmp_path):
    dist = _dist(tmp_path)
    dist.install(_source(tmp_path, settings=_SETTINGS), name="work", yes=True)
    info = dist.info("work")
    assert info["format"] == 1
    # `install` 写出的两个发行文件都该被记录在册（这份来源没有 USER.md）
    assert set(info["managed"]) == {"settings.json", "skills.json"}
    assert info["drift"] == []
    (tmp_path / "data" / "profiles" / "work" / "settings.json").write_text(
        json.dumps({"model": "手改的"}, ensure_ascii=False), encoding="utf-8"
    )
    assert dist.info("work")["drift"] == ["settings.json"]


# ── 名字安全 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", ["../escape", "a/b", "a\\b", "..", "", "x" * 80])
def test_unsafe_profile_names_are_rejected(tmp_path, name):
    """profile 名会直接拼成目录路径：`../..` 能删掉整个数据目录，必须在这里拦死。"""
    dist = _dist(tmp_path)
    with pytest.raises(ProfileDistError):
        dist.dir_of(name)


# ── CLI：10 条 distribution 路径 ────────────────────────────────────────────


def _run_cli(tmp_path, capsys, *argv):
    """跑一次真 CLI（先导入 skill_extra3：生产接线里由 commands/__init__.py 负责）。"""
    import app.cli.commands.skill_extra3  # noqa: F401 - 导入即接线
    from app.cli.main import main

    code = main(["--data-dir", str(tmp_path / "data"), *argv])
    return code, capsys.readouterr()


def test_cli_profile_distribution_paths_are_wired(tmp_path, capsys):
    import app.cli.commands.skill_extra3 as extra
    from app.cli.spec import REGISTRY

    parent = REGISTRY.get("profile")
    assert parent is not None
    for path, _handler in extra.BINDINGS:
        if not path.startswith("profile "):
            continue
        name = path.split()[1]
        node = parent.child(name)
        assert node is not None, f"{path} 不在命令表里"
        assert node.state == "done", f"{path} 仍是 {node.state}"
    # 用户画像那三条不归本模块（`memory_extra2` 已接管）
    assert set(extra.USER_PROFILE_PATHS) == {"profile show", "profile export", "profile import"}


def test_cli_profile_install_list_use_info(tmp_path, capsys):
    source = _source(tmp_path, settings=_SETTINGS, user_md=_USER_MD)
    code, out = _run_cli(tmp_path, capsys, "profile", "install", str(source), "--name", "work", "--yes")
    assert code == 0
    assert "已安装 profile：work" in out.out
    assert "approval_mode" in out.out

    code, out = _run_cli(tmp_path, capsys, "profile", "list")
    assert code == 0 and "work" in out.out

    code, out = _run_cli(tmp_path, capsys, "profile", "use", "work")
    assert code == 0
    assert "配置项变化" in out.out
    assert "approval_mode" in out.out
    assert "画像条目" in out.out

    code, out = _run_cli(tmp_path, capsys, "profile", "info", "work")
    assert code == 0 and "settings.json" in out.out


def test_cli_profile_install_remote_exits_3(tmp_path, capsys):
    code, out = _run_cli(
        tmp_path, capsys, "profile", "install", "https://example.com/p.git", "--yes"
    )
    assert code == 3
    assert "远程安装未实现" in out.err
