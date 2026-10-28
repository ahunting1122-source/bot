"""git worktree 隔离：创建 / 列表 / 删除 / 回收，以及三种"删错了不可恢复"的防护。

测试**真跑 git**（临时仓库 ``git init`` + 一次空提交），不联网、不引第三方库。
这样测的是"git 实际怎么表现"，而不是"我以为 git 会怎么表现"——比如
``prunable`` 的判据、``worktree remove`` 遇到未提交改动时返回 128、以及
``rev-parse --is-inside-work-tree`` 对"仓库子目录"也返回 true，都是实测出来的。

**临时目录为什么落在仓库内部**：本机的文件沙箱禁止在 ``E:\\bbbooottt`` 之外写盘
（``os.makedirs('E:/x')`` 直接 WinError 5），所以临时目录只能落在仓库里面；而仓库
里面的普通目录天然"在一个 git 仓库里"，``git status`` 会答非所问（报出外层的未
跟踪文件）。解决办法是用 git 自己的**天花板**机制 ``GIT_CEILING_DIRECTORIES`` 把
sandbox 划成"仓库发现的上界"——于是 sandbox 下的普通目录就是真正的"非 git 目录"。
这一条是踩过才知道的。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from app.worktree import Worktree, WorktreeError


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _force_rmtree(path: Path) -> None:
    """Windows 上 git 会留下只读文件，普通 rmtree 清不掉。"""

    def _onexc(func, target, _exc):  # noqa: ANN001 —— shutil 的回调签名
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    shutil.rmtree(path, onexc=_onexc)


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """仓库发现的上界（见模块 docstring）：sandbox 内没有 .git 的目录就是"非 git 目录"。"""
    path = tmp_path / "sandbox"
    path.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(path))
    return path


@pytest.fixture
def repo(sandbox: Path) -> Path:
    """一个有一次提交的临时仓库（``worktree add`` 需要一个起始 commit）。"""
    path = sandbox / "repo"
    path.mkdir()
    assert _git(path, "init", "-q").returncode == 0
    commit = _git(
        path, "-c", "user.email=t@example.com", "-c", "user.name=tester",
        "commit", "-q", "--allow-empty", "-m", "init",
    )
    assert commit.returncode == 0, commit.stderr
    return path


def _manager(sandbox: Path, repo: Path) -> Worktree:
    return Worktree(sandbox / "data", repo)


# ── 创建 / 列表 / 删除 ──────────────────────────────────────────────────────


def test_create_then_list_then_remove(sandbox: Path, repo: Path):
    manager = _manager(sandbox, repo)
    info = manager.create(None, "task-1")
    assert info.path == manager.root / "task-1"
    assert info.path.is_dir()
    assert info.branch == "orbit/task-1"
    assert info.managed is True
    assert info.exists is True

    listed = manager.list()
    assert "task-1" in {item.name for item in listed}
    managed = [item for item in listed if item.managed]
    assert [item.name for item in managed] == ["task-1"]
    assert managed[0].verdict == "干净"

    manager.remove("task-1")
    assert not (manager.root / "task-1").exists()
    assert [item for item in manager.list() if item.managed] == []
    # 分支保留：worktree 只是工作区，分支里的提交不该跟着消失
    assert _git(repo, "rev-parse", "--verify", "refs/heads/orbit/task-1").returncode == 0


def test_create_uses_requested_branch_and_base(sandbox: Path, repo: Path):
    manager = _manager(sandbox, repo)
    info = manager.create(None, "b1", branch="feature/x", base="HEAD")
    assert info.branch == "feature/x"
    assert _git(repo, "rev-parse", "--verify", "refs/heads/feature/x").returncode == 0


def test_remove_unknown_name_is_a_clear_error(sandbox: Path, repo: Path):
    manager = _manager(sandbox, repo)
    with pytest.raises(WorktreeError, match="没有名为"):
        manager.remove("nope")


# ── 防护一：目标路径已存在 ──────────────────────────────────────────────────


def test_create_refuses_when_target_path_already_exists(sandbox: Path, repo: Path):
    manager = _manager(sandbox, repo)
    target = manager.root / "dup"
    target.mkdir(parents=True)
    (target / "重要文件.txt").write_text("别覆盖我", encoding="utf-8")

    with pytest.raises(WorktreeError) as excinfo:
        manager.create(None, "dup")
    assert "已存在" in str(excinfo.value)
    assert "拒绝覆盖" in str(excinfo.value)
    # 原内容一个字没动
    assert (target / "重要文件.txt").read_text(encoding="utf-8") == "别覆盖我"


def test_create_refuses_existing_branch(sandbox: Path, repo: Path):
    manager = _manager(sandbox, repo)
    manager.create(None, "one")
    with pytest.raises(WorktreeError, match="分支已存在"):
        manager.create(None, "two", branch="orbit/one")


def test_create_needs_a_commit_to_start_from(sandbox: Path):
    empty = sandbox / "empty"
    empty.mkdir()
    assert _git(empty, "init", "-q").returncode == 0
    manager = Worktree(sandbox / "data", empty)
    with pytest.raises(WorktreeError, match="还没有任何提交"):
        manager.create(None, "x")


# ── 防护二：不在 git 仓库里 / 指错地方 ─────────────────────────────────────


def test_non_git_directory_is_rejected_loudly(sandbox: Path):
    plain = sandbox / "plain"
    plain.mkdir()
    manager = Worktree(sandbox / "data", plain)
    with pytest.raises(WorktreeError, match="不是 git 仓库"):
        manager.create(None, "x")
    with pytest.raises(WorktreeError, match="不是 git 仓库"):
        manager.list()


def test_repo_subdirectory_is_rejected_with_the_real_toplevel(sandbox: Path, repo: Path):
    """指向仓库**子目录**时必须拒：否则 worktree 会挂到外层仓库上（动了另一个仓库）。"""
    inner = repo / "sub"
    inner.mkdir()
    manager = Worktree(sandbox / "data", inner)
    with pytest.raises(WorktreeError) as excinfo:
        manager.list()
    message = str(excinfo.value)
    assert "顶层" in message
    assert str(repo) in message


def test_missing_repo_path_is_rejected(sandbox: Path):
    manager = Worktree(sandbox / "data", sandbox / "nope")
    with pytest.raises(WorktreeError, match="不存在"):
        manager.list()


def test_manager_without_repo_says_so(sandbox: Path):
    manager = Worktree(sandbox / "data")
    with pytest.raises(WorktreeError, match="没有指定仓库"):
        manager.list()


@pytest.mark.parametrize("bad", ["", "  ", "../escape", "a/b", "a\\b", ".hidden", "x" * 80])
def test_bad_names_are_rejected(sandbox: Path, repo: Path, bad: str):
    manager = _manager(sandbox, repo)
    with pytest.raises(WorktreeError):
        manager.path_for(bad)


# ── 防护三：有未提交改动的树 ───────────────────────────────────────────────


def test_remove_refuses_dirty_tree_unless_forced(sandbox: Path, repo: Path):
    manager = _manager(sandbox, repo)
    info = manager.create(None, "dirty")
    (info.path / "草稿.txt").write_text("还没提交", encoding="utf-8")

    listed = {item.name: item for item in manager.list()}
    assert listed["dirty"].dirty is True
    assert "有未提交改动" in listed["dirty"].verdict

    with pytest.raises(WorktreeError) as excinfo:
        manager.remove("dirty")
    assert "未提交改动" in str(excinfo.value)
    assert (info.path / "草稿.txt").exists(), "拒绝之后文件必须还在"

    manager.remove("dirty", force=True)
    assert not info.path.exists()


def test_cleanup_stale_only_reclaims_prunable_and_keeps_dirty_tree(sandbox: Path, repo: Path):
    """回收只碰 ``prunable``（目录已不在）的树；有改动的那棵一根汗毛都不动。"""
    manager = _manager(sandbox, repo)
    gone = manager.create(None, "gone")
    dirty = manager.create(None, "dirty")
    (dirty.path / "草稿.txt").write_text("还没提交", encoding="utf-8")

    # 手动删掉一棵树的目录 → git 会把它标成 prunable
    _force_rmtree(gone.path)
    prunable = {item.name: item for item in manager.list()}
    assert prunable["gone"].prunable, "删掉目录后 git 必须把它标成 prunable"
    assert not prunable["gone"].exists

    removed = manager.cleanup_stale()
    assert removed == ["gone"]
    # 有改动的那棵树还在，文件还在
    assert dirty.path.is_dir()
    assert (dirty.path / "草稿.txt").exists()
    assert {item.name for item in manager.list() if item.managed} == {"dirty"}
    # admin 侧也清干净了（不再有 prunable 条目）
    assert not [item for item in manager.list() if item.prunable]


def test_cleanup_stale_dry_run_deletes_nothing(sandbox: Path, repo: Path):
    manager = _manager(sandbox, repo)
    gone = manager.create(None, "gone")
    _force_rmtree(gone.path)
    planned = manager.cleanup_stale(dry_run=True)
    assert planned == ["gone"]
    assert [item for item in manager.list() if item.prunable], "dry-run 不许改动 git 的账"


def test_cleanup_keeps_prunable_tree_whose_state_cannot_be_verified(sandbox: Path, repo: Path):
    """双保险：prunable 但目录还在、且状态读不出来 → 不删（读不出就当脏）。"""
    manager = _manager(sandbox, repo)
    info = manager.create(None, "ghost")
    _force_rmtree(info.path)
    info.path.mkdir()  # 目录又"回来"了，但里面没有 .git
    (info.path / "疑似重要.txt").write_text("x", encoding="utf-8")

    listed = {item.name: item for item in manager.list()}
    assert listed["ghost"].prunable
    assert listed["ghost"].status_error, "没有 .git 的目录，git status 必须失败"
    assert "保留" in listed["ghost"].verdict

    assert manager.cleanup_stale() == []
    assert (info.path / "疑似重要.txt").exists()


def test_cleanup_without_worktree_root_is_a_noop(sandbox: Path, repo: Path):
    manager = _manager(sandbox, repo)
    assert not manager.root.exists()
    assert manager.cleanup_stale() == []


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_worktree_list_json(sandbox: Path, repo: Path, capsys):
    from app.cli.commands import delegate as delegate_cli  # noqa: F401 —— 导入即接线
    from app.cli.main import main

    data_dir = sandbox / "data"
    Worktree(data_dir, repo).create(None, "cli-one")

    code = main(["--data-dir", str(data_dir), "--json", "worktree", "list", "--repo", str(repo)])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["ok"] is True
    managed = [item for item in payload["worktrees"] if item["managed"]]
    assert [item["name"] for item in managed] == ["cli-one"]
    assert managed[0]["verdict"] == "干净"


def test_cli_worktree_create_and_remove(sandbox: Path, repo: Path, capsys):
    from app.cli.commands import delegate as delegate_cli  # noqa: F401
    from app.cli.main import main

    data_dir = sandbox / "data"
    code = main(
        ["--data-dir", str(data_dir), "worktree", "create", "cli-two", "--repo", str(repo)]
    )
    assert code == 0
    assert (data_dir / "worktrees" / "cli-two").is_dir()
    assert "已创建 worktree cli-two" in capsys.readouterr().out

    code = main(
        ["--data-dir", str(data_dir), "worktree", "remove", "cli-two", "--repo", str(repo)]
    )
    assert code == 0
    assert not (data_dir / "worktrees" / "cli-two").exists()


def test_cli_worktree_cleanup_dry_run(sandbox: Path, repo: Path, capsys):
    from app.cli.commands import delegate as delegate_cli  # noqa: F401
    from app.cli.main import main

    data_dir = sandbox / "data"
    manager = Worktree(data_dir, repo)
    info = manager.create(None, "stale")
    _force_rmtree(info.path)

    code = main(
        ["--data-dir", str(data_dir), "worktree", "cleanup", "--repo", str(repo), "--dry-run"]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "stale" in out
    assert "预览" in out
