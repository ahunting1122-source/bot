"""影子 git 仓库（``app/shadow_checkpoint.py``，MATRIX 10.9）的契约测试。

钉住五件事，每件都对应一个"看起来能用、实际会坏"的失败模式：

1. **绝不污染用户的 ``.git``**：先在 ``root`` 里建一个**真的** git 仓库（有提交），
   跑完 init/snapshot 之后那个仓库的 ``HEAD`` / ``refs`` / ``index`` 逐字节未变。
   这是本模块最要紧的一条——把别人的仓库当成自己的，一次快照就能毁掉工作区。
2. **快照/回滚往返**：改过的文件回到旧内容，新建的文件被清掉，内容逐字节相同
   （``core.autocrlf=false`` 就是为了这条，否则回滚会悄悄改掉换行符）。
3. **回滚前自动快照**：``restore`` 会覆盖工作目录，所以它必须先把"当前状态"存下来，
   且这个行为**不可关闭**。
4. **``--dry-run`` 一个字节都不写**。
5. **大目录与数据目录被排除**：``node_modules`` / ``.venv`` / ``<data_dir>`` 不进快照，
   回滚也不会删它们（``git clean`` 不带 ``-x`` 不碰 ignored 文件）。

外加**没有 git 时如实报 ``GitUnavailable``**：静默降级成空操作等于让用户以为文件被
保护了。
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import app.cli.commands.session_extra2  # noqa: F401 —— 导入即接线
from app.cli.main import main
from app.cli.spec import REGISTRY
from app.config import Settings
from app.shadow_checkpoint import (
    DEFAULT_EXCLUDES,
    GitUnavailable,
    ShadowError,
    ShadowRepo,
    root_digest,
)

# ── 工具 ────────────────────────────────────────────────────────────────────


def _clean_env() -> dict[str, str]:
    """跑**用户那个** git 仓库时用的环境：把影子仓库的 GIT_DIR/GIT_WORK_TREE 摘掉。"""
    return {k: v for k, v in os.environ.items() if k not in {"GIT_DIR", "GIT_WORK_TREE"}}


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(root),
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=_clean_env(),
        check=False,
    )
    assert result.returncode == 0, f"git {' '.join(args)} 失败：{result.stderr}"
    return result.stdout


def _user_repo(root: Path) -> None:
    """在 root 里建一个**真的** git 仓库（用户的仓库，不是影子仓库）。"""
    _git(root, "init", "--quiet")
    _git(root, "config", "user.name", "Real User")
    _git(root, "config", "user.email", "user@example.com")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "--quiet", "-m", "base")


def _repo(tmp_path: Path) -> tuple[ShadowRepo, Path, Path]:
    root = tmp_path / "work"
    data = tmp_path / "data"
    root.mkdir()
    data.mkdir()
    return ShadowRepo(root, data_dir=data), root, data


# ── 1. 不污染用户的 .git ────────────────────────────────────────────────────


def test_snapshot_does_not_touch_the_users_git(tmp_path):
    repo, root, _ = _repo(tmp_path)
    _user_repo(root)
    (root / "tracked.txt").write_text("changed by the agent\n", encoding="utf-8")

    head_before = _git(root, "rev-parse", "HEAD").strip()
    index_before = (root / ".git" / "index").read_bytes()
    refs_before = sorted(p.name for p in (root / ".git" / "refs" / "heads").iterdir())

    repo.init()
    repo.snapshot("agent 动手之前")

    assert _git(root, "rev-parse", "HEAD").strip() == head_before, "用户仓库的 HEAD 被改了"
    assert (root / ".git" / "index").read_bytes() == index_before, "用户仓库的索引被改了"
    assert sorted(p.name for p in (root / ".git" / "refs" / "heads").iterdir()) == refs_before
    # 用户的 `git status` 仍然只看得到自己的改动，看不到影子仓库的任何痕迹
    assert "tracked.txt" in _git(root, "status", "--porcelain")
    assert ".orbit" not in _git(root, "status", "--porcelain")


def test_shadow_dir_lives_under_data_and_outside_the_worktree(tmp_path):
    repo, root, data = _repo(tmp_path)
    shadow = repo.init()
    assert shadow.is_relative_to(data / "shadow")
    assert not shadow.is_relative_to(root), "影子仓库绝不能落在工作目录里"
    assert not (root / ".git").exists(), "工作目录里不许出现 .git"


def test_root_digest_folds_windows_path_case(tmp_path):
    """同一个目录被写成 ``E:\\Foo`` / ``e:\\foo`` 时必须是同一个影子仓库。"""
    root = tmp_path / "work"
    root.mkdir()
    assert root_digest(root) == root_digest(str(root).upper()) == root_digest(str(root).lower())


# ── 2. 快照/回滚往返 ────────────────────────────────────────────────────────


def test_snapshot_and_restore_round_trip(tmp_path):
    repo, root, _ = _repo(tmp_path)
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    first = repo.snapshot("v1")

    (root / "a.txt").write_text("v2\n", encoding="utf-8")
    (root / "b.txt").write_text("new\n", encoding="utf-8")
    second = repo.snapshot("v2")
    assert second.sha != first.sha and second.created

    payload = repo.restore(first.sha)
    assert payload["applied"] is True
    assert (root / "a.txt").read_text(encoding="utf-8") == "v1\n", "回滚后内容必须逐字节相同"
    assert not (root / "b.txt").exists(), "快照里不存在的未跟踪文件应被清掉"


def test_restore_makes_the_worktree_match_the_snapshot_exactly(tmp_path):
    """回滚之后工作目录**就是**那个快照的样子：多出来的文件会消失。

    但它们不是"丢了"——自动安全快照把它们存下来了，再回滚一次就能拿回来。这条用例
    同时钉住"安全快照真的含那些文件"。
    """
    repo, root, _ = _repo(tmp_path)
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    first = repo.snapshot("v1")
    (root / "b.txt").write_text("new\n", encoding="utf-8")

    payload = repo.restore(first.sha)
    assert not (root / "b.txt").exists(), "快照里没有的文件不该留在工作目录里"

    repo.restore(payload["safety_sha"])
    assert (root / "b.txt").read_text(encoding="utf-8") == "new\n", "安全快照必须含回滚前的文件"


def test_no_change_returns_the_head_without_a_new_commit(tmp_path):
    repo, root, _ = _repo(tmp_path)
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    first = repo.snapshot("v1")
    again = repo.snapshot("什么都没改")
    assert again.sha == first.sha
    assert again.created is False, "没有改动就不该产生空提交（log 里塞满空提交等于没信息）"
    assert len(repo.log()) == 1


# ── 3. 回滚前自动快照 ───────────────────────────────────────────────────────


def test_restore_snapshots_the_current_state_first(tmp_path):
    """回滚也可能选错 sha；那时唯一能救回来的就是"回滚之前长什么样"。"""
    repo, root, _ = _repo(tmp_path)
    (root / "a.txt").write_text("old\n", encoding="utf-8")
    old = repo.snapshot("old")
    (root / "a.txt").write_text("当前这版\n", encoding="utf-8")

    payload = repo.restore(old.sha)
    safety = payload["safety_sha"]
    assert safety and safety != old.sha, "必须先给当前状态存一个安全快照"
    messages = [item.message for item in repo.log()]
    assert any("安全快照" in message for message in messages)

    # 用安全快照回滚回去，能拿回"回滚之前"的内容
    repo.restore(safety)
    assert (root / "a.txt").read_text(encoding="utf-8") == "当前这版\n"


# ── 4. dry-run 一个字节都不写 ───────────────────────────────────────────────


def test_restore_dry_run_writes_nothing(tmp_path):
    repo, root, _ = _repo(tmp_path)
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    first = repo.snapshot("v1")
    (root / "a.txt").write_text("v2\n", encoding="utf-8")
    (root / "b.txt").write_text("new\n", encoding="utf-8")

    payload = repo.restore(first.sha, dry_run=True)
    assert payload["applied"] is False
    assert payload["dry_run"] is True
    assert {item["path"] for item in payload["changed"]} == {"a.txt", "b.txt"}
    assert (root / "a.txt").read_text(encoding="utf-8") == "v2\n", "dry-run 不许改文件"
    assert (root / "b.txt").exists(), "dry-run 不许删文件"


# ── 5. 大目录与数据目录被排除 ───────────────────────────────────────────────


def test_big_directories_are_ignored(tmp_path):
    repo, root, _ = _repo(tmp_path)
    (root / "keep.txt").write_text("keep\n", encoding="utf-8")
    for name in ("node_modules", ".venv", "__pycache__"):
        target = root / name
        target.mkdir()
        (target / "blob.bin").write_text("x" * 4096, encoding="utf-8")

    repo.init()
    repo.snapshot("排除大目录")

    tracked = repo._run("ls-files").stdout.splitlines()
    assert "keep.txt" in tracked
    assert not [path for path in tracked if path.startswith(("node_modules/", ".venv/"))]
    assert DEFAULT_EXCLUDES, "排除表不能是空的"


def test_data_dir_inside_the_worktree_is_excluded(tmp_path):
    """``<data_dir>`` 在工作目录里时必须排除：否则快照会把自己的对象库收进去。"""
    root = tmp_path / "work"
    root.mkdir()
    data = root / "data"
    data.mkdir()
    (data / "app.sqlite").write_text("db\n", encoding="utf-8")
    (root / "a.txt").write_text("a\n", encoding="utf-8")

    repo = ShadowRepo(root, data_dir=data)
    repo.init()
    repo.snapshot("数据目录在树里")

    tracked = repo._run("ls-files").stdout.splitlines()
    assert tracked == ["a.txt"], f"数据目录被收进快照了：{tracked}"
    assert "/data/" in repo.excludes_path.read_text(encoding="utf-8")


def test_excludes_are_private_and_never_written_into_the_worktree(tmp_path):
    repo, root, _ = _repo(tmp_path)
    repo.init()
    assert not (root / ".gitignore").exists(), "不许往用户工作目录里写 .gitignore"
    assert repo.excludes_path.is_file()


# ── 6. 没有 git 时如实报错 ─────────────────────────────────────────────────


def test_git_unavailable_is_reported_not_swallowed(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    repo = ShadowRepo(root, data_dir=tmp_path / "data", git="definitely-not-a-git-binary")
    assert repo.available() is False
    with pytest.raises(GitUnavailable) as excinfo:
        repo.init()
    assert "git" in str(excinfo.value)


def test_missing_root_is_reported(tmp_path):
    repo = ShadowRepo(tmp_path / "nope", data_dir=tmp_path / "data")
    with pytest.raises(ShadowError):
        repo.init()


def test_unknown_sha_is_rejected(tmp_path):
    repo, root, _ = _repo(tmp_path)
    (root / "a.txt").write_text("a\n", encoding="utf-8")
    repo.snapshot("a")
    with pytest.raises(ShadowError):
        repo.restore("deadbeef" * 5)


# ── 7. log / diff / status ──────────────────────────────────────────────────


def test_log_is_newest_first_and_limited(tmp_path):
    repo, root, _ = _repo(tmp_path)
    for index in range(3):
        (root / f"f{index}.txt").write_text(str(index), encoding="utf-8")
        repo.snapshot(f"第 {index} 次")
    snapshots = repo.log(2)
    assert [item.message for item in snapshots] == ["第 2 次", "第 1 次"]
    assert snapshots[0].ts


def test_diff_lists_changed_files(tmp_path):
    repo, root, _ = _repo(tmp_path)
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    first = repo.snapshot("v1")
    (root / "a.txt").write_text("v2\n", encoding="utf-8")
    (root / "b.txt").write_text("new\n", encoding="utf-8")

    payload = repo.diff(first.sha)
    assert payload["empty"] is False
    assert {item["path"] for item in payload["changed"]} == {"a.txt", "b.txt"}
    assert payload["stat"]

    repo.snapshot("v2")
    assert repo.diff(repo.log(1)[0].sha)["empty"] is True


def test_status_reports_pending_changes(tmp_path):
    repo, root, _ = _repo(tmp_path)
    (root / "a.txt").write_text("v1\n", encoding="utf-8")
    repo.snapshot("v1")
    assert repo.status() == []
    (root / "a.txt").write_text("v2\n", encoding="utf-8")
    assert any("a.txt" in line for line in repo.status())


# ── 8. CLI：sessions shadow 五条 ───────────────────────────────────────────


def _settings(tmp_path) -> Settings:
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "work")
    settings.prepare()
    Path(settings.workspace).mkdir(parents=True, exist_ok=True)
    return settings


def _argv(settings: Settings, *rest: str) -> list[str]:
    """`--root` 是 shadow 子命令自己的选项，只能放在子命令**之后**；
    而 `main()` 会新建一个 `Settings()`（`--data-dir` 不改 workspace），
    所以每条用例都必须显式给 `--root`，否则影子仓库会落到默认工作目录里。"""
    return [
        "--data-dir",
        str(settings.data_dir),
        *rest,
        "--root",
        str(settings.workspace),
    ]


def test_shadow_commands_are_registered():
    sessions = REGISTRY.get("sessions")
    assert sessions is not None
    group = sessions.child("shadow")
    assert group is not None and group.state == "done"
    assert {sub.name for sub in group.subcommands} == {
        "init",
        "snapshot",
        "restore",
        "log",
        "diff",
    }
    assert all(sub.help.strip() for sub in group.subcommands)


def test_cli_shadow_init(tmp_path, capsys):
    settings = _settings(tmp_path)
    assert main(_argv(settings, "sessions", "shadow", "init")) == 0
    out = capsys.readouterr().out
    assert "影子仓库已就绪" in out
    assert (settings.data_dir / "shadow").is_dir()


def test_cli_shadow_snapshot(tmp_path, capsys):
    settings = _settings(tmp_path)
    Path(settings.workspace, "a.txt").write_text("v1\n", encoding="utf-8")
    assert main(
        _argv(
            settings,
            "--json",
            "sessions",
            "shadow",
            "snapshot",
            "-m",
            "第一次",
        )
    ) == 0
    out = capsys.readouterr().out
    assert '"created": true' in out
    assert "第一次" in out


def test_cli_shadow_log(tmp_path, capsys):
    settings = _settings(tmp_path)
    Path(settings.workspace, "a.txt").write_text("v1\n", encoding="utf-8")
    assert main(_argv(settings, "sessions", "shadow", "snapshot")) == 0
    capsys.readouterr()
    assert main(_argv(settings, "sessions", "shadow", "log")) == 0
    assert "快照 1 条" in capsys.readouterr().out


def test_cli_shadow_diff(tmp_path, capsys):
    settings = _settings(tmp_path)
    Path(settings.workspace, "a.txt").write_text("v1\n", encoding="utf-8")
    assert main(_argv(settings, "sessions", "shadow", "snapshot")) == 0
    capsys.readouterr()
    sha = ShadowRepo(settings.workspace, data_dir=settings.data_dir).log(1)[0].sha
    Path(settings.workspace, "a.txt").write_text("v2\n", encoding="utf-8")
    assert main(_argv(settings, "sessions", "shadow", "diff", sha)) == 0
    assert "a.txt" in capsys.readouterr().out


def test_cli_shadow_restore_dry_run_then_real(tmp_path, capsys):
    settings = _settings(tmp_path)
    Path(settings.workspace, "a.txt").write_text("v1\n", encoding="utf-8")
    assert main(_argv(settings, "sessions", "shadow", "snapshot", "-m", "v1")) == 0
    capsys.readouterr()
    sha = ShadowRepo(settings.workspace, data_dir=settings.data_dir).log(1)[0].sha
    Path(settings.workspace, "a.txt").write_text("v2\n", encoding="utf-8")

    assert main(_argv(settings, "sessions", "shadow", "restore", sha, "--dry-run")) == 0
    assert "dry-run" in capsys.readouterr().out
    assert Path(settings.workspace, "a.txt").read_text(encoding="utf-8") == "v2\n"

    assert main(_argv(settings, "sessions", "shadow", "restore", sha)) == 0
    assert "已回滚" in capsys.readouterr().out
    assert Path(settings.workspace, "a.txt").read_text(encoding="utf-8") == "v1\n"


def test_cli_shadow_restore_unknown_sha_is_a_clean_error(tmp_path, capsys):
    settings = _settings(tmp_path)
    Path(settings.workspace, "a.txt").write_text("v1\n", encoding="utf-8")
    assert main(_argv(settings, "sessions", "shadow", "snapshot")) == 0
    capsys.readouterr()
    assert main(_argv(settings, "sessions", "shadow", "restore", "deadbeef" * 5)) == 1
    assert "快照不存在" in capsys.readouterr().err
