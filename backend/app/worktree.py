"""git worktree 隔离：用 ``git worktree`` 子命令，不引第三方库。

对应 Hermes 的 ``delegation.worktree_isolation``（bool，默认 false：开启后每个
child 从父当前 commit 拉一份独立 worktree；非 git 仓库或非本地后端时**静默忽略**，
【实证】``_get_worktree_isolation()`` 945-956）与 ``hermes worktree {list,audit,
prune}``（"Attended reclaim for the .worktrees/ 目录……**Never deletes uncommitted
tracked changes, unique unpushed commits, or in-use trees**"，【实证】``cmd-05-worktree.txt``）。

Orbit 的路径约定：``<data_dir>/worktrees/<name>``。这样 ``orbit worktree list``
能一眼看出"哪些树是我起的"，而不会把用户自己 ``git worktree add`` 出来的目录
（可能在仓库旁边任意位置）误当成可回收对象。

**必须防的三件事**（每一条都是"删错了不可恢复"）：

1. 目标路径已存在 → 拒绝。``git worktree add`` 自己也会拒，但它的报错是
   ``fatal: '<path>' already exists``——没告诉用户"这个路径是本工具的约定位置"。
2. 不在 git 仓库里 → 明确报错。Hermes 在这里选择"静默忽略"（因为它是可选特性），
   Orbit 的 CLI 是**显式**动作，静默忽略等于骗人。
3. ``cleanup_stale()`` 只清 ``prunable`` 的条目，并且**不删有未提交改动的树**
   （用 ``git status --porcelain`` 判断）。

关于第 3 条的实测细节（本机 git 2.55）：``prunable`` 的判据是"admin 侧 gitdir 文件
指向的位置不存在"，也就是**目录已经没了**——所以"prunable 且有改动"在当前 git
版本下不会同时成立。改动检查仍然保留，理由是：① ``remove()`` 的主要防护就是它；
② ``cleanup_stale`` 里对"prunable 但目录还在"的竞态（git 版本差异、并发
``git worktree add``）做双保险——删目录之前再查一次，脏就不动。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("orbit.worktree")

#: ``list[str]`` 的模块级别名。
#:
#: 为什么需要它：`Worktree` 里有一个叫 ``list`` 的方法，而 Python 的类体是**同一个
#: 作用域**——在类体里写 ``list[str]`` 会被解析成那个方法而不是内置类型，mypy 直接报
#: ``Function "Worktree.list" is not valid as a type``。用模块级别名绕开，比把方法改名
#: 更小：改名要动 13 处调用点，而 ``manager.list()`` 读起来本来就很自然。
Names = list[str]

#: worktree 的落点（相对 ``data_dir``）。与 Hermes 的 ``.worktrees/`` 同构。
WORKTREE_SUBDIR = "worktrees"

#: 新建工作树时默认分支名前缀。带前缀是为了让 ``git branch`` 一眼分辨
#: "agent 起的分支"和人的分支。
BRANCH_PREFIX = "orbit/"

#: 名字长度上限：Windows 路径 + git 的 admin 目录都会跟着变长，没必要放太宽。
MAX_NAME_LENGTH = 64

#: 名字里不允许出现的字符：路径分隔符、Windows 盘符冒号、以及任何空白。
_FORBIDDEN = set('\\/:*?"<>|')


class WorktreeError(RuntimeError):
    """worktree 操作失败。消息面向用户，必须包含"下一步能做什么"。"""


@dataclass(frozen=True)
class WorktreeInfo:
    """一棵工作树的现状（``list()`` 的返回值，也是 ``create()`` 的返回值）。"""

    name: str
    path: Path
    branch: str = ""
    head: str = ""
    bare: bool = False
    #: 非空 = 被 ``git worktree lock`` 锁住（有人在用），原因在字符串里。
    locked: str = ""
    #: 非空 = git 认为这棵树该被 prune（通常是目录已不存在），原因在字符串里。
    prunable: str = ""
    #: 有未提交改动（含未跟踪文件）。
    dirty: bool = False
    #: ``git status`` 读不出来时的错误。**保守处理**：读不出就不许删。
    status_error: str = ""
    #: 是否落在 ``<data_dir>/worktrees/`` 下（= 本工具管理的树）。
    managed: bool = False
    exists: bool = True

    @property
    def verdict(self) -> str:
        """一句话结论，给 ``orbit worktree list`` 用。"""
        if self.prunable:
            return "可回收（prunable）" if not (self.dirty or self.status_error) else "保留（prunable 但有改动）"
        if self.locked:
            return "在用（locked）"
        if self.status_error:
            return "保留（状态未知）"
        if self.dirty:
            return "保留（有未提交改动）"
        return "干净"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": str(self.path),
            "branch": self.branch,
            "head": self.head,
            "bare": self.bare,
            "locked": self.locked,
            "prunable": self.prunable,
            "dirty": self.dirty,
            "status_error": self.status_error,
            "managed": self.managed,
            "exists": self.exists,
            "verdict": self.verdict,
        }


class Worktree:
    """一个仓库的工作树管理器。``repo`` 可在构造时给定，也可逐次传。

    为什么不用 ``gitpython``：本机无网络、不许装依赖，而且这里只用得到
    ``worktree add/remove/list/prune`` 与 ``status --porcelain`` 五条命令——
    包一层 ``subprocess`` 比引一个库更小的面积，也更好测（测试就是真跑 git）。
    """

    def __init__(self, data_dir: Any, repo: Any = None, *, git: str = "git") -> None:
        self.data_dir = Path(data_dir)
        self.repo = Path(repo) if repo is not None else None
        self.git = git

    # ── 路径 ────────────────────────────────────────────────────────────────
    @property
    def root(self) -> Path:
        """``<data_dir>/worktrees``。所有名字都相对它解析。"""
        return self.data_dir / WORKTREE_SUBDIR

    def path_for(self, name: str) -> Path:
        return self.root / self.check_name(name)

    @staticmethod
    def check_name(name: str) -> str:
        """名字白名单：只允许"一个目录名"，不允许任何路径成分。"""
        raw = str(name or "").strip()
        if not raw:
            raise WorktreeError("worktree 名字不能为空")
        if len(raw) > MAX_NAME_LENGTH:
            raise WorktreeError(f"worktree 名字太长（>{MAX_NAME_LENGTH} 字符）：{raw[:20]}…")
        if ".." in raw:
            raise WorktreeError(f"worktree 名字不允许包含 '..'：{raw}")
        bad = sorted(set(raw) & _FORBIDDEN)
        if bad:
            raise WorktreeError(
                f"worktree 名字不允许包含 {'、'.join(bad)}（它只能是单层目录名，不能是路径）：{raw}"
            )
        if raw.startswith("."):
            raise WorktreeError(f"worktree 名字不能以 '.' 开头（会和 git 的隐藏目录混淆）：{raw}")
        return raw

    # ── git 调用 ────────────────────────────────────────────────────────────
    def _git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        """跑一条 git 命令。**不抛**：调用方按 ``returncode`` 判断。

        注意 git 会把进度信息（"Preparing worktree…"）写到 stderr 而 ``returncode``
        仍是 0——所以判成败只看返回码，不看 stderr 是否为空。
        """
        command = [self.git, *args]
        try:
            return subprocess.run(
                command,
                cwd=str(cwd) if cwd is not None else None,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except FileNotFoundError as exc:
            raise WorktreeError(
                f"找不到 git 可执行文件（{self.git}）：worktree 隔离依赖本机 git"
            ) from exc

    def _resolve_repo(self, repo: Any = None) -> Path:
        """解析并校验仓库路径：必须存在，且**它就是**一个 git 工作区的顶层。

        为什么要额外比 ``--show-toplevel``：``git rev-parse --is-inside-work-tree``
        对一个"碰巧在某个仓库子目录里"的普通目录也返回 true。那种情况下
        ``git worktree add`` 会把树挂到**外层**仓库上——用户以为在操作 A，实际动了 B。
        所以要求路径 == 顶层；不相等就把真实的顶层报出来，让人自己决定。
        """
        candidate = Path(repo) if repo is not None else self.repo
        if candidate is None:
            raise WorktreeError("没有指定仓库：传 --repo，或在构造 Worktree 时给 repo")
        path = candidate.expanduser()
        if not path.exists():
            raise WorktreeError(f"仓库路径不存在：{path}")
        probe = self._git("rev-parse", "--is-inside-work-tree", cwd=path)
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            raise WorktreeError(
                f"不是 git 仓库：{path}（`git rev-parse --is-inside-work-tree` 失败；"
                "worktree 隔离只对 git 仓库有意义，非 git 目录不会静默降级）"
            )
        top = self._git("rev-parse", "--show-toplevel", cwd=path)
        toplevel = Path(top.stdout.strip()) if top.returncode == 0 and top.stdout.strip() else None
        if toplevel is None or os.path.normcase(os.path.abspath(str(toplevel))) != os.path.normcase(
            os.path.abspath(str(path))
        ):
            raise WorktreeError(
                f"{path} 不是一个 git 工作区的顶层"
                + (f"（它属于仓库 {toplevel}）" if toplevel is not None else "")
                + "：请直接指向仓库根目录。worktree 会挂到顶层仓库上，"
                "指向子目录会让人以为在操作 A、实际动了 B"
            )
        return path

    # ── create ──────────────────────────────────────────────────────────────
    def create(
        self,
        repo: Any = None,
        name: str = "",
        *,
        branch: str = "",
        base: str = "",
    ) -> WorktreeInfo:
        """新建一棵工作树并返回它的现状。

        ``repo`` 可省略（用构造时给的）。``base`` 默认 ``HEAD``——"从父当前 commit
        拉一份"是 Hermes 的语义（【实证】§4.1），所以默认就跟当前 HEAD 走。
        """
        root_repo = self._resolve_repo(repo)
        clean = self.check_name(name)
        target = self.root / clean
        if target.exists():
            raise WorktreeError(
                f"目标路径已存在：{target}。拒绝覆盖——先 `orbit worktree remove {clean}` "
                "（或自己确认那目录是什么再手动处理）"
            )
        head = self._git("rev-parse", "--verify", "HEAD", cwd=root_repo)
        if head.returncode != 0:
            raise WorktreeError(
                f"仓库还没有任何提交：{root_repo}。worktree 需要一个起始 commit"
                "（先 `git commit` 一次，或 `git commit --allow-empty -m init`）"
            )
        new_branch = str(branch or f"{BRANCH_PREFIX}{clean}")
        existing = self._git("rev-parse", "--verify", f"refs/heads/{new_branch}", cwd=root_repo)
        if existing.returncode == 0:
            raise WorktreeError(
                f"分支已存在：{new_branch}。换个分支名（--branch），"
                "或先删掉引用它的旧工作树"
            )
        self.root.mkdir(parents=True, exist_ok=True)
        args = ["worktree", "add", str(target), "-b", new_branch]
        if base:
            args.append(str(base))
        added = self._git(*args, cwd=root_repo)
        if added.returncode != 0:
            raise WorktreeError(
                f"`git worktree add` 失败（退出码 {added.returncode}）："
                f"{(added.stderr or added.stdout or '').strip() or '无输出'}"
            )
        log.info("已创建 worktree %s → %s（分支 %s）", clean, target, new_branch)
        return self._info_for(root_repo, target, new_branch)

    # ── list ────────────────────────────────────────────────────────────────
    def list(self) -> list[WorktreeInfo]:
        """列出该仓库的全部工作树（含主工作区与别人手动建的），并标注归属。"""
        repo = self._resolve_repo()
        listing = self._git("worktree", "list", "--porcelain", cwd=repo)
        if listing.returncode != 0:
            raise WorktreeError(
                f"`git worktree list` 失败：{(listing.stderr or listing.stdout or '').strip()}"
            )
        infos = [self._build_info(entry) for entry in _parse_porcelain(listing.stdout)]
        # 名字在 managed 树里必须唯一（同名不同路径只可能是历史残留），
        # 非 managed 的树用目录名兜底，重名也无所谓——它们不参与 remove/cleanup。
        return infos

    def get(self, name: str) -> WorktreeInfo | None:
        """按名字取一棵**本工具管理**的树。"""
        clean = self.check_name(name)
        for info in self.list():
            if info.managed and info.name == clean:
                return info
        return None

    # ── remove ──────────────────────────────────────────────────────────────
    def remove(self, name: str, *, force: bool = False) -> WorktreeInfo:
        """删掉一棵工作树。有未提交改动时**必须** ``force=True`` 才动。"""
        repo = self._resolve_repo()
        info = self.get(name)
        if info is None:
            raise WorktreeError(f"没有名为 {self.check_name(name)} 的工作树（用 `orbit worktree list` 看现有的）")
        if info.exists and (info.dirty or info.status_error) and not force:
            detail = "有未提交改动（git status --porcelain 非空）" if info.dirty else (
                f"状态读不出来（{info.status_error}）"
            )
            raise WorktreeError(
                f"工作树 {info.name} {detail}：拒绝删除。"
                "确认这些改动可以丢，再用 --force（这是不可逆动作）"
            )
        args = ["worktree", "remove"]
        if force:
            args.append("--force")
        args.append(str(info.path))
        removed = self._git(*args, cwd=repo)
        if removed.returncode != 0:
            raise WorktreeError(
                f"`git worktree remove` 失败（退出码 {removed.returncode}）："
                f"{(removed.stderr or removed.stdout or '').strip() or '无输出'}"
            )
        log.info("已删除 worktree %s", info.name)
        return info

    # ── cleanup ─────────────────────────────────────────────────────────────
    def cleanup_stale(self, *, dry_run: bool = False) -> Names:
        """回收**本工具管理**且 ``prunable`` 的工作树；返回被（将要）回收的名字。

        三道过滤，顺序有意义：``prunable`` → ``managed`` → 无改动。最后一道是
        "删目录之前再查一次"的双保险（见模块 docstring 第 3 条）。
        """
        repo = self._resolve_repo()
        if not self.root.exists():
            return []
        removed: Names = []
        for info in self.list():
            if not info.prunable or not info.managed:
                continue
            if info.locked:
                log.info("跳过 %s：被 git lock（有人在用）", info.name)
                continue
            if info.exists and (info.dirty or info.status_error):
                log.info("跳过 %s：目录还在且有未提交改动/状态未知", info.name)
                continue
            removed.append(info.name)
            if dry_run:
                continue
            if info.exists:
                shutil.rmtree(info.path, ignore_errors=True)
            log.info("已回收 worktree %s（prunable）", info.name)
        if removed and not dry_run:
            # git 自己的 prune 会清掉 admin 侧残留；它**不会**碰还活着的树，
            # 所以这里用它而不是手动删 .git/worktrees/<name>（那才是危险的写法）。
            pruned = self._git("worktree", "prune", cwd=repo)
            if pruned.returncode != 0:
                log.warning("`git worktree prune` 失败：%s", (pruned.stderr or "").strip())
        return removed

    # ── 内部：把 porcelain 文本变成 WorktreeInfo ────────────────────────────
    def _info_for(self, repo: Path, path: Path, branch: str) -> WorktreeInfo:
        """刚创建完直接构造（不必再跑一次 list，省一次 git 调用）。"""
        head = self._git("rev-parse", "HEAD", cwd=path)
        return WorktreeInfo(
            name=path.name,
            path=path,
            branch=branch,
            head=head.stdout.strip() if head.returncode == 0 else "",
            managed=_is_under(path, self.root),
            exists=path.exists(),
        )

    def _build_info(self, entry: dict[str, str]) -> WorktreeInfo:
        path = Path(entry["worktree"])
        managed = _is_under(path, self.root)
        name = path.name
        if managed:
            try:
                name = path.resolve().relative_to(self.root.resolve()).as_posix()
            except ValueError:  # 理论上到不了；保底用目录名
                name = path.name
        # `locked` / `prunable` 在 porcelain 里可能**没有值**（`git worktree lock`
        # 不带 --reason 就是这样）。所以判"有没有"必须看键在不在，而不是看值真不真
        # ——按值判会把"锁住但没写原因"当成"没锁"，然后把它删掉。
        prunable = entry.get("prunable", "")
        if "prunable" in entry and not prunable:
            prunable = "git 认为该树已失效（未给原因）"
        locked = entry.get("locked", "")
        if "locked" in entry and not locked:
            locked = "（未给原因）"
        exists = path.exists()
        dirty = False
        status_error = ""
        # prunable 的树也要查一次状态：目录可能还在（git 版本差异/并发 add 的竞态），
        # 而"删目录之前再查一次"正是最后那道防护。查不出来 → 记 status_error，
        # 调用方按"当脏处理"拒绝删除。
        if exists:
            status = self._git("status", "--porcelain", cwd=path)
            if status.returncode != 0:
                status_error = (status.stderr or status.stdout or "").strip() or "git status 失败"
            else:
                dirty = bool(status.stdout.strip())
        return WorktreeInfo(
            name=name,
            path=path,
            branch=entry.get("branch", ""),
            head=entry.get("HEAD", ""),
            bare="bare" in entry,
            locked=locked,
            prunable=prunable,
            dirty=dirty,
            status_error=status_error,
            managed=managed,
            exists=exists,
        )


def _parse_porcelain(text: str) -> list[dict[str, str]]:
    """解析 ``git worktree list --porcelain``：空行分块，每块是 ``key [value]``。

    单值键（``HEAD`` / ``branch`` / ``prunable`` / ``locked``）直接存；无值键
    （``bare`` / ``detached``）存空串——"键存在"本身就是信息，所以不丢。
    """
    entries: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for line in (text or "").splitlines():
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            if current:  # 容错：git 不空行分隔时也不至于把两棵树粘一起
                entries.append(current)
            current = {"worktree": value}
        else:
            current[key] = value
    if current:
        entries.append(current)
    return entries


def _is_under(path: Path, parent: Path) -> bool:
    """``path`` 是否落在 ``parent`` 之下。Windows 下大小写不敏感，必须 normcase。"""
    try:
        target = os.path.normcase(os.path.abspath(str(path)))
        base = os.path.normcase(os.path.abspath(str(parent)))
    except (OSError, ValueError):
        return False
    return target == base or target.startswith(base.rstrip(os.sep) + os.sep)
