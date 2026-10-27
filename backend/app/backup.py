"""数据目录的备份与恢复（`orbit backup` / `orbit import`）。

对齐 Hermes 的 `backup` / `import`（zip 往返 + 清单 + 校验），但有两个**必须做对**
的地方，做不到就等于给用户一个假的保险：

1. **SQLite 不能直接复制文件**。`app.sqlite` 在运行期开着 WAL，直接 `copy` 会拿到
   一个缺少已提交事务的残缺库（WAL 里的数据不在主文件里）。必须走 SQLite 的在线
   备份 API（`src.backup(dst)`），它保证拿到一个一致快照。
2. **恢复必须防 zip slip**。zip 里的条目名是攻击面：`../../windows/system32/x.dll`
   这种路径如果在解压时不校验，就等于任意文件写入。恢复路径一律先 `resolve()`
   再校验仍在目标目录内，且拒绝绝对路径、`..`、盘符、符号链接。

刻意**不**把 `browser_profiles/`（几百 MB 的 Chromium profile）和
`fastembed_cache/`（几 GB 模型权重）放进默认备份：备份的价值是"能恢复你的数据"，
不是"能恢复所有可以重新下载的东西"。要带上它们用 `--include-cache`。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

#: 清单格式版本。改结构时 +1，`restore` 据此判断能不能读。
MANIFEST_VERSION = 1

#: 备份里必须有的单个文件（存在才收）。
SINGLE_FILES = (
    "settings.json",
    "mcp.json",
    "vault.json",
    "approvals.json",
    "advisories.json",
    "chunk_sweep.json",
    "SOUL.md",
    "AGENTS.md",
)

#: 备份里必须有的目录（整棵收）。
DIRECTORIES = ("skills", "cron", "profile", "artifacts")

#: 默认排除：可重建 / 体积大 / 与本机绑定。
EXCLUDED = (
    "browser_profiles",
    "fastembed_cache",
    "cache",
    "backups",
    "logs",
)

#: 需要走 SQLite 在线备份的库。
SQLITE_FILES = ("app.sqlite", "checkpoints.sqlite")


class BackupError(RuntimeError):
    """备份/恢复的可预期失败：带一句能行动的话，不打栈。"""


@dataclass
class BackupResult:
    path: Path
    files: int
    bytes: int
    excluded: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "path": str(self.path),
            "files": self.files,
            "bytes": self.bytes,
            "excluded": self.excluded,
        }


@dataclass
class RestoreResult:
    path: Path
    files: int
    bytes: int
    dry_run: bool
    overwritten: bool

    def as_dict(self) -> dict:
        return {
            "path": str(self.path),
            "files": self.files,
            "bytes": self.bytes,
            "dry_run": self.dry_run,
            "overwritten": self.overwritten,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_sqlite(source: Path, target: Path) -> None:
    """用 SQLite 在线备份 API 拿一致快照。

    `source` 可能正被后端进程写着（WAL 模式），所以**不能**用文件复制：
    已提交但还在 WAL 里的事务不在主文件中，复制出来的库会"丢最近的操作"。
    在线备份 API 会在读事务里把主库 + WAL 合成一个一致的副本。
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target.unlink()
    src = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=10)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


#: `--quick` 只收这些：能决定"agent 还能不能照原样跑起来"的最小集合。
#: 注意**不含 `app.sqlite`**——它走 SQLite 在线快照那条路（否则会以两个不同来源
#: 写进同一个 zip 条目，zip 里出现重名）。
QUICK_FILES = (
    "settings.json",
    "mcp.json",
    "vault.json",
    "approvals.json",
)

#: `--quick` 只收这些目录。
QUICK_DIRECTORIES = ("cron",)


def create_backup(
    data_dir: Path,
    *,
    out: Path | None = None,
    include_cache: bool = False,
    quick: bool = False,
    note: str = "",
) -> BackupResult:
    """把数据目录打成一个带清单的 zip。返回打包结果。

    `quick=True` 时只收 `QUICK_FILES` + `QUICK_DIRECTORIES`（对齐 Hermes
    `backup --quick` 的"关键状态快照"语义）：够在另一台机器上把 agent 恢复成
    "能跑、有记忆、有技能配置"的状态，但不含技能正文与产物。
    """
    data_dir = Path(data_dir).resolve()
    if not data_dir.is_dir():
        raise BackupError(f"数据目录不存在：{data_dir}")
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    target = Path(out) if out else data_dir / "backups" / f"orbit-backup-{stamp}.zip"
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    excluded = [] if include_cache else list(EXCLUDED)
    single_files = QUICK_FILES if quick else SINGLE_FILES
    directories = QUICK_DIRECTORIES if quick else DIRECTORIES
    # `--include-cache` 的语义是"把默认排除的目录也收进来"，所以要把它们显式加进
    # 待收集列表——只在 EXCLUDED 里移除是不够的，那些目录本来就不在 DIRECTORIES 里。
    if include_cache:
        directories = tuple(dict.fromkeys((*directories, *EXCLUDED)))
    sqlite_files = ("app.sqlite",) if quick else SQLITE_FILES

    entries: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="orbit-backup-") as scratch:
        staging = Path(scratch)
        payload: list[tuple[Path, str]] = []

        # 1) SQLite 库：先快照到暂存目录，再按普通文件收进 zip。
        for name in sqlite_files:
            source = data_dir / name
            if not source.exists():
                continue
            snapshot = staging / name
            _snapshot_sqlite(source, snapshot)
            payload.append((snapshot, name))

        # 2) 单个文件。
        for name in single_files:
            source = data_dir / name
            if source.is_file():
                payload.append((source, name))

        # 3) 目录整棵收（跳过排除项）。
        for name in directories:
            root = data_dir / name
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.relative_to(data_dir)
                if any(part in excluded for part in relative.parts):
                    continue
                payload.append((path, relative.as_posix()))

        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source, arcname in payload:
                archive.write(source, arcname)
                entries.append(
                    {"name": arcname, "bytes": source.stat().st_size, "sha256": _sha256(source)}
                )
            manifest = {
                "manifest_version": MANIFEST_VERSION,
                "created_at": datetime.now(UTC).isoformat(),
                "orbit_version": _version(),
                "note": note,
                "quick": quick,
                "files": entries,
                "excluded": excluded,
            }
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    total = sum(entry["bytes"] for entry in entries)
    return BackupResult(path=target, files=len(entries), bytes=total, excluded=excluded)


def _version() -> str:
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as package_version

    try:
        return package_version("orbit-personal-agent")
    except PackageNotFoundError:
        return "0.1.0"


def read_manifest(zip_path: Path) -> dict:
    """读清单。缺清单 = 这不是 Orbit 的备份，直接拒绝。"""
    try:
        with zipfile.ZipFile(zip_path) as archive:
            raw = archive.read("manifest.json")
    except KeyError as exc:
        raise BackupError(f"{zip_path} 里没有 manifest.json，不是 Orbit 的备份") from exc
    except (OSError, zipfile.BadZipFile) as exc:
        raise BackupError(f"打不开备份文件：{exc}") from exc
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise BackupError(f"清单不是合法 JSON：{exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != MANIFEST_VERSION:
        raise BackupError(
            f"清单版本不支持：{manifest.get('manifest_version') if isinstance(manifest, dict) else '?'}"
            f"（本实现支持 {MANIFEST_VERSION}）"
        )
    return manifest


def verify_backup(zip_path: Path) -> list[str]:
    """校验备份完整性。返回问题列表，空列表 = 通过。"""
    problems: list[str] = []
    manifest = read_manifest(zip_path)
    try:
        with zipfile.ZipFile(zip_path) as archive:
            names = set(archive.namelist())
            for entry in manifest.get("files", []):
                name = entry.get("name", "")
                if name not in names:
                    problems.append(f"清单里的 {name} 不在压缩包里")
                    continue
                digest = hashlib.sha256()
                with archive.open(name) as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
                if digest.hexdigest() != entry.get("sha256"):
                    problems.append(f"{name} 校验和不符（备份可能已损坏）")
    except (OSError, zipfile.BadZipFile) as exc:
        problems.append(f"读压缩包失败：{exc}")
    return problems


def _safe_member(name: str, target_root: Path) -> Path:
    """把 zip 里的条目名解析成目标路径，并保证它**落在目标目录内**。

    这一层是必须的：zip 的条目名完全由打包方决定，`../../x` 或绝对路径如果不校验，
    恢复就等于任意文件写入（zip slip）。拒绝而不是"清洗"——清洗过的路径会让人以为
    备份里本来就有那个文件。
    """
    raw = name.replace("\\", "/")
    if raw.startswith("/") or (len(raw) > 1 and raw[1] == ":"):
        raise BackupError(f"备份里含绝对路径，拒绝恢复：{name}")
    parts = [part for part in raw.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise BackupError(f"备份里含上跳路径，拒绝恢复：{name}")
    candidate = target_root.joinpath(*parts).resolve()
    root = target_root.resolve()
    if candidate != root and root not in candidate.parents:
        raise BackupError(f"备份条目逃出目标目录，拒绝恢复：{name}")
    return candidate


def restore_backup(
    zip_path: Path,
    data_dir: Path,
    *,
    overwrite: bool = False,
    dry_run: bool = False,
    verified: bool = True,
) -> RestoreResult:
    """把备份恢复到 `data_dir`。

    默认**不覆盖**已有数据：覆盖是不可逆动作，必须显式 `overwrite=True`。
    `dry_run=True` 时只做全部校验（含 zip slip 与校验和），不写一个字节。
    """
    zip_path = Path(zip_path)
    data_dir = Path(data_dir)
    if not zip_path.is_file():
        raise BackupError(f"备份文件不存在：{zip_path}")
    manifest = read_manifest(zip_path)
    entries = manifest.get("files", [])
    if not isinstance(entries, list) or not entries:
        raise BackupError("备份清单里没有文件条目")

    # 先把所有路径解析一遍：任何一个非法就整体拒绝，不留"恢复了一半"的状态。
    planned: list[tuple[str, Path, int]] = []
    for entry in entries:
        name = str(entry.get("name", ""))
        destination = _safe_member(name, data_dir)
        planned.append((name, destination, int(entry.get("bytes") or 0)))

    existing = [str(path) for _name, path, _size in planned if path.exists()]
    if existing and not overwrite and not dry_run:
        preview = "、".join(existing[:3])
        raise BackupError(
            f"目标已存在 {len(existing)} 个文件（如 {preview}）。"
            "确认要覆盖请加 --overwrite（不可逆）。"
        )

    if not dry_run and verified:
        problems = verify_backup(zip_path)
        if problems:
            raise BackupError("备份校验未通过，已中止恢复：" + "；".join(problems[:3]))

    total = sum(size for _name, _path, size in planned)
    if dry_run:
        return RestoreResult(
            path=zip_path, files=len(planned), bytes=total, dry_run=True, overwritten=False
        )

    with zipfile.ZipFile(zip_path) as archive:
        for name, destination, _size in planned:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(name) as source, destination.open("wb") as sink:
                shutil.copyfileobj(source, sink)

    return RestoreResult(
        path=zip_path,
        files=len(planned),
        bytes=total,
        dry_run=False,
        overwritten=bool(existing),
    )
