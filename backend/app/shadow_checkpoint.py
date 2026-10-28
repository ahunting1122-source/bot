"""工作目录的影子 git 仓库：写文件前可快照、可回滚（MATRIX 10.9）。

对标 ``handoff/hermes-parity/research/03-persistence-layer.md`` §6.4：Hermes 在动用户的
工作目录之前，先把整个目录做成一个**影子仓库**快照，于是"模型改坏了"可以回滚到任何
一次快照。Orbit 目前只有 LangGraph 的 ``checkpoints.sqlite``——那记的是**图状态**
（走到哪个节点、消息列表），不是**磁盘上的文件**。两者互补，不能互相替代：图状态能
恢复到"第 3 步"，但第 3 步之后被写坏的那三个文件，只有影子仓库能还回来。

## 为什么不引 gitpython

一条硬理由：**本机没有网络**，装不了新依赖（本轮铁律）。第二条是"能用 20 行解决就
别拉一个库"：本模块要的只有 ``init`` / ``add`` / ``commit`` / ``log`` / ``diff`` /
``read-tree`` 六条命令，全部是 ``git`` CLI 的稳定接口，包一层 subprocess 就够。
代价如实标注：**依赖本机装了 git**。检测不到就抛 :class:`GitUnavailable`——明确报
"这台机器没有 git，快照不可用"，而不是静默降级成"什么都没做"（那会让用户以为文件
有保护）。

## 怎么保证**绝不污染**用户的 ``.git``

这是本模块最要紧的一条。三个机制叠起来：

1. **每次调用都显式传 ``GIT_DIR=<data>/shadow/<hash>/``**。git 的仓库发现逻辑是"从
   cwd 往上找 ``.git``"，而 ``GIT_DIR`` 一旦设置就**跳过发现**，直接用给定的目录。
   于是工作目录里那个真正的 ``.git`` 从头到尾没被读过、也没被写过。
2. **``GIT_WORK_TREE`` 指工作目录**。这样 ``git add -A`` 收集的是工作目录的文件，
   索引与对象却落在影子目录里。
3. **影子仓库落点在工作目录之外**（``<data_dir>/shadow/…``）。用户 ``git status``
   看不到它，``git add -A`` 也不会把它收进去——它压根不在工作树里。

``tests/test_shadow_checkpoint.py`` 有一条用例先在 ``root`` 里建一个**真的** git 仓库
（有提交、有分支），跑完快照/回滚后断言那个仓库的 ``HEAD``、``refs``、``index`` 逐字节
未变。

## 为什么排除表写进 ``info/exclude`` 而不是 ``.gitignore``

写 ``.gitignore`` 会**污染用户的工作目录**：多出一个文件、进他们的 ``git status``、
可能覆盖他们已有的同名文件。``<shadow>/info/exclude`` 是 git 提供的"本仓库私有忽略
表"，效果完全一样，而且完全不可见。

第一次快照就必须排除掉 ``<data_dir>`` 自身与 ``node_modules`` / ``.venv`` 这类目录，
否则一次快照就能把磁盘写满（实测本机 ``node_modules`` 单个就上 GB）。

## 回滚为什么必须先自动快照 + 必须支持 ``--dry-run``

:meth:`ShadowRepo.restore` 会**覆盖工作目录**，这是本模块唯一不可逆的动作。所以：

- **先快照"当前状态"**：回滚本身也可能是个错误（选错了 sha），那时唯一能救回来的
  就是"回滚之前长什么样"。这一步不是可选项，没有开关。
- **``--dry-run`` 列出将被改动的文件**，一个字节都不写。判据是"这次回滚会改哪些
  文件"，而不是"这个快照里有什么"——后者对决策没用。
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: 影子仓库的落点目录名：``<data_dir>/shadow/<hash-of-root>/``。
SHADOW_DIRNAME = "shadow"

#: root 路径哈希取多少位。16 位十六进制足够区分本机上同时存在的几个工作目录。
ROOT_HASH_CHARS = 16

#: 默认提交信息前缀。带时间戳，``git log`` 里一眼能看出是快照还是手工提交。
SNAPSHOT_PREFIX = "orbit snapshot"

#: 快照必须排除的东西（gitignore 语法，落在 ``<shadow>/info/exclude``）。
#:
#: 分三类，缺任何一类都会出事：
#: 1. **大目录**：``node_modules`` / ``.venv`` / ``target`` —— 一次快照能写满磁盘；
#: 2. **缓存与派生物**：``__pycache__`` / ``.mypy_cache`` / ``dist`` —— 无意义且量大；
#: 3. **仓库自身**：工作目录里那个 ``.git`` —— 不能把别人的仓库当普通目录收进来。
DEFAULT_EXCLUDES: tuple[str, ...] = (
    "# Orbit 影子仓库私有排除表（由 app/shadow_checkpoint.py 生成，改它没用）",
    "# 用户的 git 仓库：绝不收进影子仓库",
    ".git/",
    "# 大目录：第一次快照就能写满磁盘的那几个",
    "node_modules/",
    ".venv/",
    "venv/",
    "env/",
    "site-packages/",
    ".pnpm-store/",
    "target/",
    "dist/",
    "build/",
    ".next/",
    ".nuxt/",
    ".turbo/",
    "# 缓存与派生物",
    "__pycache__/",
    "*.pyc",
    ".mypy_cache/",
    ".ruff_cache/",
    ".pytest_cache/",
    ".cache/",
    "# 超大与不可快照的文件",
    "*.iso",
    "*.dmg",
    "*.vmdk",
)


class GitUnavailable(RuntimeError):
    """本机没有可用的 git CLI。

    **不做静默降级**：影子仓库的全部价值是"文件真的被保护了"。悄悄变成空操作，用户
    会在文件被改坏之后才发现没有快照——那时已经晚了。
    """


class ShadowError(RuntimeError):
    """可预期的影子仓库错误（root 不存在、sha 不存在、工作目录不是目录等）。

    与"真 bug"区分开：CLI 侧把它翻成 ``CliError``（一行消息 + 返回码），不打栈。
    """


def now_iso(moment: float | None = None) -> str:
    """带时区的 ISO 时间串（与 ``app.storage.now()`` / ``app.session_plane.now_iso`` 同格式）。"""
    if moment is None:
        return datetime.now(UTC).isoformat()
    return datetime.fromtimestamp(moment, UTC).isoformat()


def root_digest(root: Path | str) -> str:
    """工作目录路径 → 影子仓库目录名。

    ``casefold`` 是 Windows 必需的：同一个目录可能被写成 ``E:\\Foo`` 和 ``e:\\foo``，
    不折大小写就会给同一个工作目录建两个影子仓库，快照历史当场分裂。
    """
    text = str(Path(root).resolve()).casefold()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:ROOT_HASH_CHARS]


@dataclass(frozen=True)
class Snapshot:
    """一条快照记录。``created=False`` 表示"没有改动，返回的是当前 HEAD"。"""

    sha: str
    message: str
    ts: str
    created: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "sha": self.sha,
            "message": self.message,
            "ts": self.ts,
            "created": self.created,
        }


class ShadowRepo:
    """某个工作目录的影子 git 仓库。

    ``root`` 是**被保护的工作目录**，``data_dir`` 是 Orbit 的数据目录（影子仓库落在
    它下面）。两者刻意分开：影子仓库绝不能出现在工作目录里（见模块 docstring）。
    """

    def __init__(self, root: Path | str, *, data_dir: Path | str, git: str = "git") -> None:
        self.root = Path(root)
        self.data_dir = Path(data_dir)
        self.git = git

    # ── 路径 ──
    @property
    def shadow_dir(self) -> Path:
        """``<data_dir>/shadow/<hash-of-root>/`` —— 影子仓库的 ``GIT_DIR``。"""
        return self.data_dir / SHADOW_DIRNAME / root_digest(self.root)

    @property
    def excludes_path(self) -> Path:
        """本仓库私有的忽略表（**不在**工作目录里，所以不污染用户文件）。"""
        return self.shadow_dir / "info" / "exclude"

    @property
    def initialized(self) -> bool:
        return (self.shadow_dir / "HEAD").exists()

    # ── 可用性 ──
    def available(self) -> bool:
        """本机 git 是否可用（CLI 的 ``--json`` 要能报出这个事实）。"""
        return shutil.which(self.git) is not None

    def _require(self) -> None:
        if not self.available():
            raise GitUnavailable(
                f"找不到 git（{self.git!r}）：影子仓库需要本机装 git，"
                "本轮不引 gitpython（无网络）。装上 git 后重试，或改用 `orbit backup`。"
            )
        if not self.root.exists():
            raise ShadowError(f"工作目录不存在：{self.root}")
        if not self.root.is_dir():
            raise ShadowError(f"不是目录：{self.root}")

    # ── subprocess 包装 ──
    def _env(self) -> dict[str, str]:
        """每次调用都带的三个环境变量。``GIT_DIR`` 是"不污染用户 .git"的关键。"""
        import os

        env = dict(os.environ)
        env["GIT_DIR"] = str(self.shadow_dir)
        env["GIT_WORK_TREE"] = str(self.root)
        # 交互式凭据提示在自动化里只会挂住进程；影子仓库本来也不联网。
        env["GIT_TERMINAL_PROMPT"] = "0"
        return env

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        """跑一条 git 命令。**输出一律按 UTF-8 解码并替换坏字节**。

        Windows 上 git 的默认输出编码随 ``chcp`` 变（中文路径常被转成 GBK 或加引号
        转义），所以 ``core.quotepath=false`` 在 :meth:`init` 里关掉转义，这里统一按
        UTF-8 解码——非 ASCII 路径在 ``log`` / ``diff`` 里才不会变成 ``\\346\\226\\207``。
        """
        self._require()
        try:
            result = subprocess.run(
                [self.git, *args],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                env=self._env(),
                cwd=str(self.root),
                check=False,
            )
        except FileNotFoundError as exc:  # 路径写错 / 装的是别名
            raise GitUnavailable(f"无法执行 git（{self.git!r}）：{exc}") from exc
        except OSError as exc:  # pragma: no cover - 权限之类
            raise ShadowError(f"执行 git 失败（{self.git!r}）：{exc}") from exc
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise ShadowError(f"git {' '.join(args)} 失败（{result.returncode}）：{detail[:400]}")
        return result

    # ── 初始化 ──
    def init(self) -> Path:
        """建（或复用）影子仓库，返回它的 ``GIT_DIR``。幂等。

        ``core.*`` 那几行不是可有可无的：``autocrlf`` 开着会让快照/回滚**改掉文件的
        换行符**（回滚后文件内容与原来不等），``quotepath`` 开着会让中文路径在输出里
        变成八进制转义。这两件事都属于"看起来成功了、其实数据变了"。
        """
        self._require()
        if not self.initialized:
            self.shadow_dir.mkdir(parents=True, exist_ok=True)
            self._run("init", "--quiet")
        for key, value in (
            ("core.bare", "false"),
            ("core.worktree", str(self.root)),
            ("core.autocrlf", "false"),
            ("core.safecrlf", "false"),
            ("core.filemode", "false"),
            ("core.quotepath", "false"),
            ("commit.gpgsign", "false"),
            # 身份写在**影子仓库自己的** config 里：本机可能没有全局 user.name，
            # 没有它 commit 会直接失败（"Please tell me who you are"）。
            ("user.name", "Orbit Shadow"),
            ("user.email", "orbit-shadow@localhost"),
        ):
            self._run("config", key, value)
        self._write_excludes()
        return self.shadow_dir

    def _write_excludes(self) -> None:
        """重写 ``info/exclude``（含 ``<data_dir>`` 自身与常见大目录）。

        每次 :meth:`snapshot` 前都写一遍：这个文件是"第一次快照不会写满磁盘"的唯一
        依据，被人删掉或仓库被重建过之后，一次 ``git add -A`` 就能把整个数据目录收进
        对象库。重写是毫秒级的，值得。
        """
        self.excludes_path.parent.mkdir(parents=True, exist_ok=True)
        lines = list(DEFAULT_EXCLUDES)
        inside = self._worktree_relative(self.data_dir)
        if inside is not None:
            lines.append("# Orbit 自己的数据目录：快照里绝不能有它（对象库会自我复制）")
            lines.append(f"/{inside}/")
        else:
            # 数据目录在工作树之外：git 本来就不会收它，无需（也无法）用相对模式表达。
            lines.append("# 数据目录在工作树之外，无需排除")
        self.excludes_path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")

    def _worktree_relative(self, path: Path) -> str | None:
        """``path`` 相对工作树的 posix 路径；不在工作树内返回 ``None``。

        gitignore 的模式只能是相对路径，所以"在工作树之外"这件事必须显式判出来——
        硬塞一个绝对路径进 ``info/exclude`` 是不生效的，而它不生效的表现是"数据目录
        被收进了快照"，不是报错。
        """
        try:
            relative = Path(path).resolve().relative_to(self.root.resolve())
        except (ValueError, OSError):
            return None
        return relative.as_posix() or None

    # ── 快照 ──
    def snapshot(self, message: str = "") -> Snapshot:
        """把工作目录的当前状态提交成一个快照，返回它。

        **没有改动时不产生新提交**，直接返回当前 ``HEAD``（``created=False``）：
        每一轮都无条件提交会让 ``git log`` 里塞满空提交，"什么时候真的变了"反而看不
        出来。第一次快照（还没有 ``HEAD``）照常提交。
        """
        self.init()
        self._write_excludes()
        self._run("add", "-A")
        dirty = bool(self._run("status", "--porcelain").stdout.strip())
        head = self._head()
        if not dirty and head:
            return Snapshot(sha=head, message="（无改动）", ts=now_iso(), created=False)
        text = str(message or "").strip() or f"{SNAPSHOT_PREFIX} {now_iso()}"
        self._run("commit", "--quiet", "--allow-empty", "-m", text)
        return Snapshot(sha=self._head(), message=text, ts=now_iso(), created=True)

    def _head(self) -> str:
        result = self._run("rev-parse", "--verify", "HEAD", check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    # ── 读 ──
    def log(self, limit: int = 20) -> list[Snapshot]:
        """最近 ``limit`` 条快照（新→旧）。没有快照时返回空列表，不报错。"""
        if not self.initialized:
            return []
        result = self._run(
            "log", f"-n{max(1, int(limit))}", "--format=%H%x1f%cI%x1f%s", check=False
        )
        if result.returncode != 0:
            return []
        snapshots: list[Snapshot] = []
        for line in result.stdout.splitlines():
            parts = line.split("\x1f")
            if len(parts) < 3 or not parts[0].strip():
                continue
            snapshots.append(
                Snapshot(sha=parts[0].strip(), ts=parts[1].strip(), message=parts[2].strip())
            )
        return snapshots

    def status(self) -> list[str]:
        """相对最新快照有改动的文件（``git status --porcelain`` 原样）。"""
        if not self.initialized:
            return []
        return [line for line in self._run("status", "--porcelain").stdout.splitlines() if line]

    def resolve(self, sha: str) -> str:
        """把短 sha / 引用解析成完整提交 sha；不存在就报错（**不要**猜一个最近的）。"""
        text = str(sha or "").strip()
        if not text:
            raise ShadowError("需要一个快照 sha（用 `orbit sessions shadow log` 查）")
        result = self._run("rev-parse", "--verify", f"{text}^{{commit}}", check=False)
        if result.returncode != 0 or not result.stdout.strip():
            raise ShadowError(f"快照不存在：{sha}（用 `orbit sessions shadow log` 查可用 sha）")
        return result.stdout.strip()

    def diff(self, sha: str) -> dict[str, Any]:
        """工作目录相对某个快照的差异（``--stat`` + 逐文件状态）。

        **未跟踪文件也算差异**（状态 ``??``）：``git diff <commit>`` 只看已跟踪文件，
        而"模型刚新建了一个文件"恰恰是最需要看见的那一类改动——只报已跟踪文件会让
        用户在 ``restore --dry-run`` 里以为"没什么要改的"，然后那些文件被清掉。
        """
        target = self.resolve(sha)
        stat = self._run("diff", "--stat", target, check=False).stdout.strip()
        names = self._run("diff", "--name-status", target, check=False).stdout.strip()
        changed: list[dict[str, str]] = []
        for line in names.splitlines():
            parts = line.split("\t")
            if len(parts) < 2:
                continue
            changed.append({"status": parts[0].strip(), "path": parts[-1].strip()})
        untracked = self._run("ls-files", "--others", "--exclude-standard", check=False)
        for line in untracked.stdout.splitlines():
            path = line.strip()
            if path:
                changed.append({"status": "??", "path": path})
        return {
            "sha": target,
            "stat": stat,
            "changed": changed,
            "empty": not changed,
        }

    # ── 回滚 ──
    def restore(self, sha: str, *, dry_run: bool = False, clean: bool = True) -> dict[str, Any]:
        """把工作目录恢复成某个快照的样子。

        **先自动快照当前状态**（不可关闭）：回滚也可能选错 sha，那时唯一能救回来的就是
        "回滚之前长什么样"。

        ``dry_run=True`` 只算不动，一个字节都不写；``changed`` 列出会被改动的文件
        （含未跟踪文件，状态 ``??``）。

        回滚本身用 ``git read-tree --reset -u <sha>``——它是 ``reset --hard`` 的管道版：
        索引与工作树一起对齐到目标，且**不动 HEAD**。所以回滚之后工作目录**就是**那个
        快照的样子：目标里没有的文件会消失，但它们在刚才那个安全快照里，可以再回滚
        回来。``clean=True`` 额外跑一次 ``git clean -fdq``（不带 ``-x``），清掉
        ``read-tree`` 之后仍然未被跟踪的文件（例如快照与回滚之间被别的进程新建的）。
        被 ``info/exclude`` 排除的目录（``node_modules`` / ``<data_dir>``）不受影响。
        """
        self.init()
        target = self.resolve(sha)
        safety = self.snapshot(f"restore 前的安全快照（准备回滚到 {target[:12]}）")
        diff = self.diff(target)
        payload: dict[str, Any] = {
            "sha": target,
            "safety_sha": safety.sha,
            "dry_run": bool(dry_run),
            "applied": False,
            "clean": bool(clean),
            "changed": diff["changed"],
            "stat": diff["stat"],
        }
        if dry_run:
            payload["note"] = "dry-run：只列出会被改动的文件，工作目录一个字节都没动"
            return payload
        self._run("read-tree", "--reset", "-u", target)
        if clean:
            self._run("clean", "-fdq")
        payload["applied"] = True
        payload["head"] = self._head()
        return payload


__all__ = [
    "DEFAULT_EXCLUDES",
    "ROOT_HASH_CHARS",
    "SHADOW_DIRNAME",
    "SNAPSHOT_PREFIX",
    "GitUnavailable",
    "ShadowError",
    "ShadowRepo",
    "Snapshot",
    "now_iso",
    "root_digest",
]
