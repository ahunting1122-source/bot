"""权限级别（仅工作文件夹 / 完全访问权限）——**整个权限面的安全边界**。

两边都要钉死：
  - 窄档不能因为任何路径写法（`../`、绝对路径、符号化的相对路径）越出授权目录；
    文件不能写出去、命令的工作目录不能出去、不可逆命令要被拦。
  - 完全访问档必须真的够到工作目录之外、并执行任意命令——否则这个级别就是空壳，
    用户切了也没用（这正是它存在的理由）。
"""

import json

import pytest

import app.connectors as connector_module
from app.config import Settings, normalize_file_scope
from app.connectors import ConnectorConfig
from app.main import migrate
from app.storage import Conversation, Store, Task
from app.tools import (
    ToolRegistry,
    dangerous_command,
    display_path,
    machine_roots,
    resolve_path,
)


@pytest.fixture
def config(tmp_path, monkeypatch):
    secrets = {}
    monkeypatch.setattr(
        connector_module.keyring, "set_password", lambda service, key, value: secrets.update({key: value})
    )
    monkeypatch.setattr(
        connector_module.keyring, "get_password", lambda service, key: secrets.get(key)
    )
    return ConnectorConfig(Settings(data_dir=tmp_path, workspace=tmp_path / "workspace"))


async def build(tmp_path, scope: str):
    settings = Settings(
        data_dir=tmp_path / "data", workspace=tmp_path / "files", file_scope=scope
    )
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="读一个文件"))
    return settings, store, task, ToolRegistry(settings, store)


def test_normalize_file_scope_degrades_to_the_narrow_side():
    """坏值永远退回更窄的一档——配置损坏不能反向放开权限。"""
    assert normalize_file_scope("machine") == "machine"
    assert normalize_file_scope("workspace") == "workspace"
    for bad in ("MACHINE", "full", "", None, 0, ["machine"]):
        assert normalize_file_scope(bad) == "workspace"


def test_machine_wide_只有严格等于_machine_才为真(tmp_path):
    for value, expected in (("machine", True), ("workspace", False), ("Machine", False), ("", False)):
        settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files", file_scope=value)
        assert settings.machine_wide is expected, value


def test_resolve_path_workspace_scope_rejects_escape(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    with pytest.raises(ValueError):
        resolve_path(root, "../outside.txt")
    with pytest.raises(ValueError):
        resolve_path(root, str(tmp_path / "elsewhere" / "x.txt"))
    assert resolve_path(root, "notes.md") == (root / "notes.md").resolve()
    # 授权目录自身是合法的，不能被判成越界。
    assert resolve_path(root, ".") == root.resolve()


def test_resolve_path_machine_scope_allows_absolute_anywhere(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    outside = tmp_path / "elsewhere" / "x.txt"
    assert resolve_path(root, str(outside), machine=True) == outside.resolve()
    # 相对路径在完全访问档下仍相对授权目录解析，行为不突变。
    assert resolve_path(root, "notes.md", machine=True) == (root / "notes.md").resolve()


def test_display_path_prefers_relative_and_falls_back_to_absolute(tmp_path):
    root = tmp_path / "files"
    root.mkdir()
    assert display_path(root / "a.md", root, False) == "a.md"
    outside = (tmp_path / "other.md").resolve()
    assert display_path(outside, root, True) == str(outside)


async def test_workspace_scope_still_refuses_to_read_outside(tmp_path):
    _settings, store, task, registry = await build(tmp_path, "workspace")
    (tmp_path / "outside.txt").write_text("机密", encoding="utf-8")
    denied = await registry.execute(
        task.id, "read-out", "local_read_document", {"path": "../outside.txt"}
    )
    assert denied["status"] == "error"
    assert "授权工作目录" in denied["error"]
    await store.engine.dispose()


async def test_machine_scope_reads_a_file_outside_the_workspace(tmp_path):
    _settings, store, task, registry = await build(tmp_path, "machine")
    target = tmp_path / "outside" / "notes.md"
    target.parent.mkdir()
    target.write_text("# 工作目录之外\n这里是外面。", encoding="utf-8")
    read = await registry.execute(
        task.id, "read-out", "local_read_document", {"path": str(target)}
    )
    assert read["status"] == "ok", read
    assert "工作目录之外" in read["data"]["text"]
    await store.engine.dispose()


async def test_machine_scope_reads_any_utf8_text_file(tmp_path):
    """完全访问档不再按扩展名设限：源码、配置都能读。"""
    _settings, store, task, registry = await build(tmp_path, "machine")
    target = tmp_path / "script.conf"
    target.write_text("key=value\n", encoding="utf-8")
    read = await registry.execute(task.id, "read-conf", "local_read_document", {"path": str(target)})
    assert read["status"] == "ok", read
    assert "key=value" in read["data"]["text"]
    await store.engine.dispose()


async def test_workspace_scope_still_limits_readable_types(tmp_path):
    _settings, store, task, registry = await build(tmp_path, "workspace")
    (tmp_path / "files" / "script.conf").write_text("key=value\n", encoding="utf-8")
    denied = await registry.execute(
        task.id, "read-conf", "local_read_document", {"path": "script.conf"}
    )
    assert denied["status"] == "error"
    await store.engine.dispose()


async def test_machine_scope_list_files_starts_from_drive_roots(tmp_path):
    _settings, store, task, registry = await build(tmp_path, "machine")
    listed = await registry.execute(task.id, "list-root", "local_list_files", {"path": "."})
    assert listed["status"] == "ok", listed
    assert [entry["path"] for entry in listed["data"]] == [str(d) for d in machine_roots()]
    await store.engine.dispose()


async def test_workspace_scope_list_files_stays_inside(tmp_path):
    _settings, store, task, registry = await build(tmp_path, "workspace")
    (tmp_path / "files" / "a.md").write_text("x", encoding="utf-8")
    (tmp_path / "outside.txt").write_text("x", encoding="utf-8")
    listed = await registry.execute(task.id, "list", "local_list_files", {"path": "."})
    assert listed["status"] == "ok"
    assert [entry["path"] for entry in listed["data"]] == ["a.md"]
    await store.engine.dispose()


async def test_machine_scope_search_requires_a_start_directory(tmp_path):
    """不让它在整机上无目的地遍历：系统目录几十万文件、还一路吃权限错误。"""
    _settings, store, task, registry = await build(tmp_path, "machine")
    refused = await registry.execute(task.id, "search-root", "local_search_text", {"query": "x"})
    assert refused["status"] == "error"
    assert "起始目录" in refused["error"]
    scoped = await registry.execute(
        task.id, "search-scoped", "local_search_text", {"query": "FastAPI", "path": str(tmp_path)}
    )
    assert scoped["status"] == "ok", scoped
    await store.engine.dispose()


async def test_write_file_works_inside_the_workspace_in_workspace_scope(tmp_path):
    """窄档不再只能写 .md 报告：工作目录内的普通文件也能落盘。"""
    _settings, store, task, registry = await build(tmp_path, "workspace")
    written = await registry.execute(
        task.id, "w-in", "local_write_file", {"path": "notes/a.txt", "content": "目录内"}
    )
    assert written["status"] == "ok", written
    assert (tmp_path / "files" / "notes" / "a.txt").read_text(encoding="utf-8") == "目录内"
    await store.engine.dispose()


async def test_write_file_is_refused_outside_workspace_in_workspace_scope(tmp_path):
    _settings, store, task, registry = await build(tmp_path, "workspace")
    target = tmp_path / "outside" / "x.txt"
    denied = await registry.execute(
        task.id, "w1", "local_write_file", {"path": str(target), "content": "hi"}
    )
    assert denied["status"] == "error"
    # 拒绝信息必须把"怎么才能做"讲清楚，否则模型会换个参数反复重试。
    assert "完全访问权限" in denied["error"]
    assert not target.exists()
    await store.engine.dispose()


async def test_machine_scope_write_file_is_unlimited(tmp_path):
    """完全访问档：整机任意路径，且**不加 overwrite 前置条件**——用户要的就是"不设限"。"""
    _settings, store, task, registry = await build(tmp_path, "machine")
    target = tmp_path / "outside" / "x.txt"
    created = await registry.execute(
        task.id, "w1", "local_write_file", {"path": str(target), "content": "第一版"}
    )
    assert created["status"] == "ok", created
    assert created["data"]["overwritten"] is False and created["data"]["verified"] is True
    assert target.read_text(encoding="utf-8") == "第一版"

    # 直接覆盖，不需要 overwrite=true（窄档才要，见下一个用例）。
    overwritten = await registry.execute(
        task.id, "w2", "local_write_file", {"path": str(target), "content": "第二版"}
    )
    assert overwritten["status"] == "ok", overwritten
    assert overwritten["data"]["overwritten"] is True
    assert target.read_text(encoding="utf-8") == "第二版"
    await store.engine.dispose()


async def test_workspace_scope_still_protects_the_workbench_own_database(tmp_path):
    """窄档保留"不碰工作台自身数据"的兜底。

    这里刻意把**授权目录设成数据目录的父级**，否则数据文件本来就在工作目录之外、
    会被"越界"先拦下，这条兜底分支根本走不到（默认部署下正是这种情况，属于双保险）。
    """
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path, file_scope="workspace")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="写"))
    registry = ToolRegistry(settings, store)
    for name in ("app.sqlite", "checkpoints.sqlite", "settings.json"):
        target = settings.data_dir / name
        denied = await registry.execute(
            task.id,
            f"w-{name}",
            "local_write_file",
            {"path": str(target), "content": "x", "overwrite": True},
        )
        assert denied["status"] == "error", name
        assert "自身" in denied["error"]
    await store.engine.dispose()


async def test_machine_scope_write_file_touches_its_own_database(tmp_path):
    settings, store, task, registry = await build(tmp_path, "machine")
    target = settings.data_dir / "settings.json"
    written = await registry.execute(
        task.id, "w-own", "local_write_file", {"path": str(target), "content": "{}"}
    )
    assert written["status"] == "ok", written
    assert target.read_text(encoding="utf-8") == "{}"
    await store.engine.dispose()


async def test_write_report_is_unaffected_by_machine_scope(tmp_path):
    """报告工具在两种级别下语义一致：还是 .md、还是不覆盖不同内容。"""
    _settings, store, task, registry = await build(tmp_path, "machine")
    args = {"path": "reports/a.md", "content": "# 报告"}
    first = await registry.execute(task.id, "r1", "local_write_report", args)
    assert first["status"] == "ok" and first["data"]["verified"] is True
    clash = await registry.execute(
        task.id, "r2", "local_write_report", {"path": "reports/a.md", "content": "# 换了内容"}
    )
    assert clash["status"] == "error"
    await store.engine.dispose()


def test_file_scope_persists_and_rejects_unknown_value(config):
    assert config.public()["file_scope"] == "workspace"
    assert config.public()["file_root"] == str(config.settings.workspace.resolve())

    saved = config.update({"file_scope": "machine"})
    assert saved["file_scope"] == "machine"
    # 完全访问档没有单一根目录，前端据此显示"整机"而不是一个具体路径。
    assert saved["file_root"] == ""
    assert json.loads(config.path.read_text(encoding="utf-8"))["file_scope"] == "machine"

    with pytest.raises(ValueError):
        config.update({"file_scope": "everything"})

    # 换一个实例重新加载：级别是持久化设置，不是进程内状态。
    reloaded = ConnectorConfig(Settings(data_dir=config.settings.data_dir))
    reloaded.load()
    assert reloaded.settings.file_scope == "machine"
    assert reloaded.settings.machine_wide is True


def test_corrupt_stored_scope_degrades_instead_of_opening_up(config):
    config.path.write_text(json.dumps({"file_scope": "MACHINE"}), encoding="utf-8")
    config.load()
    assert config.settings.file_scope == "workspace"
    assert config.settings.machine_wide is False


# ── 命令执行（run_command）────────────────────────────────────────────────


def test_dangerous_command_flags_irreversible_actions():
    """拦截表**只**该命中"把机器搞坏且不可恢复"的那一类，日常命令一律放行。"""
    for command in (
        "format C:",
        "Format D: /q",
        "shutdown /s /t 0",
        "mkfs.ext4 /dev/sda1",
        "dd if=/dev/zero of=/dev/sda",
        "reg delete HKLM\\SOFTWARE\\Foo /f",
        "rm -rf /",
        "rm -rf ~",
        "vssadmin delete shadows /all",
        ":(){ :|:& };:",
    ):
        assert dangerous_command(command), command
    for command in (
        "echo hello",
        "npm install",
        "git status",
        "python -m pytest",
        "dir C:\\Users",
        "mkdir build && cd build",
    ):
        assert not dangerous_command(command), command


async def test_workspace_scope_runs_a_command_in_the_workspace(tmp_path):
    _settings, store, task, registry = await build(tmp_path, "workspace")
    ran = await registry.execute(task.id, "c1", "run_command", {"command": "echo orbit-ok"})
    assert ran["status"] == "ok", ran
    assert "orbit-ok" in ran["data"]["stdout"]
    assert ran["data"]["exit_code"] == 0
    # 窄档默认工作目录就是授权目录——文件范围与命令工作目录是同一条线。
    assert ran["data"]["cwd"] == str((tmp_path / "files").resolve())
    await store.engine.dispose()


async def test_machine_scope_runs_a_command_in_an_absolute_cwd(tmp_path):
    _settings, store, task, registry = await build(tmp_path, "machine")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    ran = await registry.execute(
        task.id, "c1", "run_command", {"command": "echo machine-ok", "cwd": str(outside)}
    )
    assert ran["status"] == "ok", ran
    assert "machine-ok" in ran["data"]["stdout"]
    assert ran["data"]["cwd"] == str(outside.resolve())
    await store.engine.dispose()


async def test_workspace_scope_command_cwd_cannot_escape(tmp_path):
    _settings, store, task, registry = await build(tmp_path, "workspace")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    denied = await registry.execute(
        task.id, "c1", "run_command", {"command": "echo x", "cwd": str(outside)}
    )
    assert denied["status"] == "error"
    assert "授权工作目录" in denied["error"]
    await store.engine.dispose()


async def test_workspace_scope_refuses_an_irreversible_command(tmp_path):
    _settings, store, task, registry = await build(tmp_path, "workspace")
    refused = await registry.execute(task.id, "c1", "run_command", {"command": "shutdown /s /t 0"})
    assert refused["status"] == "error"
    # 拒绝要给出"怎么才能做"，否则模型会换个写法反复重试。
    assert "完全访问权限" in refused["error"]
    await store.engine.dispose()


async def test_machine_scope_ignores_the_guard_entirely(tmp_path, monkeypatch):
    """完全访问档不设限：连不可逆命令的拦截表都不生效（这里用替身验证，绝不真跑）。"""
    import app.tools as tools_module

    monkeypatch.setattr(tools_module, "dangerous_command", lambda command: "格式化磁盘")
    _settings, store, task, registry = await build(tmp_path, "machine")
    ran = await registry.execute(task.id, "c1", "run_command", {"command": "echo escaped-guard"})
    assert ran["status"] == "ok", ran
    assert "escaped-guard" in ran["data"]["stdout"]
    await store.engine.dispose()


async def test_run_command_reports_nonzero_exit_code(tmp_path):
    _settings, store, task, registry = await build(tmp_path, "machine")
    failed = await registry.execute(task.id, "c1", "run_command", {"command": "cmd /c exit 3"})
    assert failed["status"] == "ok", failed
    assert failed["data"]["exit_code"] == 3
    await store.engine.dispose()


async def test_run_command_timeout_kills_the_process(tmp_path):
    import sys

    _settings, store, task, registry = await build(tmp_path, "machine")
    slow = f'"{sys.executable}" -c "import time; time.sleep(30)"'
    killed = await registry.execute(
        task.id, "c1", "run_command", {"command": slow, "timeout": 1}
    )
    assert killed["status"] == "ok", killed
    assert killed["data"]["timed_out"] is True
    assert killed["data"]["exit_code"] is None
    assert killed["data"]["seconds"] < 20
    await store.engine.dispose()


async def test_workspace_scope_write_file_needs_overwrite_for_existing(tmp_path):
    _settings, store, task, registry = await build(tmp_path, "workspace")
    target = tmp_path / "files" / "note.txt"
    target.write_text("旧内容", encoding="utf-8")
    blocked = await registry.execute(
        task.id, "w1", "local_write_file", {"path": "note.txt", "content": "新内容"}
    )
    assert blocked["status"] == "error"
    assert target.read_text(encoding="utf-8") == "旧内容"
    replaced = await registry.execute(
        task.id, "w2", "local_write_file",
        {"path": "note.txt", "content": "新内容", "overwrite": True},
    )
    assert replaced["status"] == "ok", replaced
    assert target.read_text(encoding="utf-8") == "新内容"
    await store.engine.dispose()
