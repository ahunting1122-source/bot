"""备份 / 恢复的契约测试（`app/backup.py` + `orbit backup|import|checkpoints`）。

这套测试盯的是"备份到底能不能救你"这件事，所以重点全在**失败模式**上：

- SQLite 直接复制文件 → 备份里丢掉 WAL 中已提交的事务（用户以为备份了，其实没有）
- 恢复时信 zip 里的路径 → zip slip，任意文件写入
- 覆盖已有数据前不问 → 不可逆地毁掉当前状态
- 清单校验和不对却照样恢复 → 恢复出一个半坏的数据目录

每条都在下面有对应用例。
"""

from __future__ import annotations

import json
import sqlite3
import zipfile

import pytest

from app import backup as backup_module
from app.cli.main import main


def _make_data_dir(tmp_path, *, with_cache: bool = False):
    data = tmp_path / "data"
    data.mkdir()
    # 一个"有 WAL 未合并"的库：只写不 checkpoint，模拟后端正在跑的现场。
    connection = sqlite3.connect(str(data / "app.sqlite"))
    connection.execute("pragma journal_mode=WAL")
    connection.execute("create table tasks (id text primary key, goal text)")
    connection.execute("insert into tasks values ('t1', '查一下键盘')")
    connection.commit()
    connection.close()

    (data / "settings.json").write_text(json.dumps({"model": "x"}), encoding="utf-8")
    (data / "approvals.json").write_text(json.dumps({"allow": []}), encoding="utf-8")
    skills = data / "skills" / "demo"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# demo", encoding="utf-8")
    if with_cache:
        cache = data / "fastembed_cache" / "model"
        cache.mkdir(parents=True)
        (cache / "weights.onnx").write_bytes(b"x" * 64)
    return data


# ── 打包 ────────────────────────────────────────────────────────────────────


def test_backup_captures_committed_rows_through_wal(tmp_path):
    """核心保证：库正在 WAL 模式下、事务只在 WAL 里，也要备份到。

    直接复制 `app.sqlite` 会漏掉这些行——这是"备份看起来成功、恢复后数据没了"的
    典型成因，所以单独锁一条用例。
    """
    data = _make_data_dir(tmp_path)
    result = backup_module.create_backup(data, out=tmp_path / "b.zip")
    assert result.files >= 3

    extract = tmp_path / "restore"
    backup_module.restore_backup(tmp_path / "b.zip", extract, overwrite=True)
    connection = sqlite3.connect(str(extract / "app.sqlite"))
    try:
        rows = connection.execute("select goal from tasks").fetchall()
    finally:
        connection.close()
    assert rows == [("查一下键盘",)]


def test_backup_writes_a_manifest_and_excludes_caches(tmp_path):
    data = _make_data_dir(tmp_path, with_cache=True)
    result = backup_module.create_backup(data, out=tmp_path / "b.zip")
    with zipfile.ZipFile(tmp_path / "b.zip") as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
    assert "manifest.json" in names
    assert "skills/demo/SKILL.md" in names
    assert manifest["manifest_version"] == backup_module.MANIFEST_VERSION
    # 缓存目录默认排除：它可以从网络重建，不该让备份膨胀几 GB。
    assert not any(name.startswith("fastembed_cache/") for name in names)
    assert "fastembed_cache" in result.excluded


def test_backup_can_include_caches_when_asked(tmp_path):
    data = _make_data_dir(tmp_path, with_cache=True)
    backup_module.create_backup(data, out=tmp_path / "b.zip", include_cache=True)
    with zipfile.ZipFile(tmp_path / "b.zip") as archive:
        assert any(name.startswith("fastembed_cache/") for name in archive.namelist())


def test_quick_backup_only_takes_critical_state(tmp_path):
    data = _make_data_dir(tmp_path)
    backup_module.create_backup(data, out=tmp_path / "q.zip", quick=True, note="测试")
    with zipfile.ZipFile(tmp_path / "q.zip") as archive:
        names = set(archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
    assert "app.sqlite" in names
    assert "settings.json" in names
    # quick 不抓技能正文——它要的是"能跑起来"，不是"完整归档"。
    assert not any(name.startswith("skills/") for name in names)
    assert manifest["quick"] is True
    assert manifest["note"] == "测试"


# ── 校验 ────────────────────────────────────────────────────────────────────


def test_verify_passes_on_a_fresh_backup(tmp_path):
    data = _make_data_dir(tmp_path)
    backup_module.create_backup(data, out=tmp_path / "b.zip")
    assert backup_module.verify_backup(tmp_path / "b.zip") == []


def test_verify_reports_a_tampered_entry(tmp_path):
    data = _make_data_dir(tmp_path)
    backup_module.create_backup(data, out=tmp_path / "b.zip")
    # 重写 settings.json 的内容但保留清单里的旧校验和 → 必须被查出来。
    with zipfile.ZipFile(tmp_path / "b.zip") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        others = {
            name: archive.read(name) for name in archive.namelist() if name != "settings.json"
        }
    with zipfile.ZipFile(tmp_path / "b.zip", "w") as archive:
        for name, payload in others.items():
            archive.writestr(name, payload)
        archive.writestr("settings.json", '{"model": "被改过了"}'.encode())
    problems = backup_module.verify_backup(tmp_path / "b.zip")
    assert any("settings.json" in problem for problem in problems)
    assert manifest["files"]  # 清单确实有内容，不是空跑


def test_missing_manifest_is_rejected(tmp_path):
    target = tmp_path / "not-a-backup.zip"
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("hello.txt", "x")
    with pytest.raises(backup_module.BackupError, match="manifest"):
        backup_module.read_manifest(target)


# ── 恢复：zip slip / 覆盖 / 预演 ────────────────────────────────────────────


def _write_zip(path, entries: dict[str, bytes], manifest_files: list[dict] | None = None):
    with zipfile.ZipFile(path, "w") as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
        manifest = {
            "manifest_version": backup_module.MANIFEST_VERSION,
            "created_at": "2026-10-27T00:00:00+00:00",
            "orbit_version": "0.1.0",
            "files": manifest_files
            if manifest_files is not None
            else [{"name": n, "bytes": len(p), "sha256": "0" * 64} for n, p in entries.items()],
            "excluded": [],
        }
        archive.writestr("manifest.json", json.dumps(manifest))


@pytest.mark.parametrize(
    "evil",
    ["../escape.txt", "../../windows/system32/x.dll", "/etc/passwd", "C:/Windows/x.dll", "a/../../b"],
)
def test_restore_rejects_path_traversal(tmp_path, evil):
    archive = tmp_path / "evil.zip"
    _write_zip(archive, {evil: b"pwn"})
    with pytest.raises(backup_module.BackupError, match="拒绝恢复"):
        backup_module.restore_backup(archive, tmp_path / "target", overwrite=True)


def test_restore_refuses_to_overwrite_without_force(tmp_path):
    data = _make_data_dir(tmp_path)
    backup_module.create_backup(data, out=tmp_path / "b.zip")
    with pytest.raises(backup_module.BackupError, match="--overwrite"):
        backup_module.restore_backup(tmp_path / "b.zip", data)


def test_restore_dry_run_writes_nothing(tmp_path):
    data = _make_data_dir(tmp_path)
    backup_module.create_backup(data, out=tmp_path / "b.zip")
    target = tmp_path / "fresh"
    target.mkdir()
    result = backup_module.restore_backup(tmp_path / "b.zip", target, dry_run=True)
    assert result.dry_run is True
    assert result.files > 0
    assert list(target.iterdir()) == []


def test_restore_refuses_a_corrupt_archive(tmp_path):
    data = _make_data_dir(tmp_path)
    backup_module.create_backup(data, out=tmp_path / "b.zip")
    # 破坏一个条目，但保留清单里的原校验和。
    with zipfile.ZipFile(tmp_path / "b.zip") as archive:
        manifest = json.loads(archive.read("manifest.json"))
        others = {
            name: archive.read(name) for name in archive.namelist() if name != "settings.json"
        }
    with zipfile.ZipFile(tmp_path / "b.zip", "w") as archive:
        for name, payload in others.items():
            archive.writestr(name, payload)
        archive.writestr("settings.json", b"broken")
    with pytest.raises(backup_module.BackupError, match="校验"):
        backup_module.restore_backup(tmp_path / "b.zip", tmp_path / "fresh", overwrite=True)
    assert manifest["files"]


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_backup_and_dry_run_import(tmp_path, capsys):
    data = _make_data_dir(tmp_path)
    out = tmp_path / "cli.zip"
    assert main(["--data-dir", str(data), "backup", "-o", str(out)]) == 0
    assert out.exists()
    capsys.readouterr()
    target = tmp_path / "elsewhere"
    assert main(["--data-dir", str(target), "import", str(out), "--dry-run"]) == 0
    assert "预演通过" in capsys.readouterr().out


def test_cli_backup_label_requires_quick(tmp_path, capsys):
    data = _make_data_dir(tmp_path)
    code = main(["--data-dir", str(data), "backup", "-l", "标签"])
    assert code == 1
    assert "--quick" in capsys.readouterr().err


def test_cli_checkpoints_prune_requires_force(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    connection = sqlite3.connect(str(data / "checkpoints.sqlite"))
    connection.execute(
        "create table checkpoints (thread_id text, checkpoint_ns text, checkpoint_id text, checkpoint blob)"
    )
    connection.execute("create table writes (thread_id text, checkpoint_ns text, checkpoint_id text)")
    connection.execute("insert into checkpoints values ('t','', 'c1', ?)", (b"ts2026-01-01T00:00:00+00:00",))
    connection.commit()
    connection.close()

    code = main(["--data-dir", str(data), "checkpoints", "prune"])
    assert code == 1
    assert "-f" in capsys.readouterr().err


def test_cli_checkpoints_status_lists_threads(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    connection = sqlite3.connect(str(data / "checkpoints.sqlite"))
    connection.execute(
        "create table checkpoints (thread_id text, checkpoint_ns text, checkpoint_id text, checkpoint blob)"
    )
    connection.execute("create table writes (thread_id text, checkpoint_ns text, checkpoint_id text)")
    connection.execute("insert into checkpoints values ('thread-a','', 'c1', ?)", (b"x" * 100,))
    connection.commit()
    connection.close()

    assert main(["--data-dir", str(data), "--json", "checkpoints", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["threads"] == 1
    assert payload["checkpoints"] == 1
    assert payload["per_thread"][0]["thread_id"] == "thread-a"
