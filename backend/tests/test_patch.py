"""结构化补丁（`app/patch.py` + `apply_patch` 工具）的契约测试。

**这份测试的核心不是"补丁能打上"，而是"打不上时磁盘一个字节都没动"。**
半个补丁比没打补丁更糟：文件停在一个既不是旧版也不是新版的状态，而模型以为自己
改成功了。所以下面有一条专门的原子性用例——同一次补丁里 A 文件合法、B 文件上下文
不匹配，断言 A **没有被写**。

另外三条也必须锁死：

- 路径逃逸被拒（补丁是批量写入，越界比单次写文件危险一个量级）；
- 报错要指到"第几个 hunk、第几行"（只说"补丁失败"，模型只能整段重试）；
- `@@` 缺失时的行为（模型常忘，得有个确定答案而不是崩溃）。
"""

from __future__ import annotations

import pytest

from app import patch as patch_module
from app.config import Settings
from app.main import migrate
from app.patch import PatchError, apply, parse, parse_and_apply
from app.storage import Conversation, Store, Task
from app.tools import ToolRegistry


def _write(root, name: str, text: str):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ── 解析 ────────────────────────────────────────────────────────────────────


def test_parse_reads_the_three_operation_types():
    text = (
        "*** Begin Patch\n"
        "*** Update File: notes.md\n"
        "@@ 项目\n"
        " # 项目\n"
        "-使用 FastAPI\n"
        "+使用 FastAPI 与 SQLAlchemy\n"
        "*** Add File: docs/new.md\n"
        "+# 新文档\n"
        "*** Delete File: docs/old.md\n"
        "*** End Patch\n"
    )
    patch = parse(text)
    assert [op.op for op in patch.ops] == ["update", "add", "delete"]
    assert patch.ops[0].path == "notes.md"
    assert patch.ops[0].hunks[0].context_hint == "项目"
    assert patch.ops[0].hunks[0].before == ("# 项目", "使用 FastAPI")
    assert patch.ops[0].hunks[0].after == ("# 项目", "使用 FastAPI 与 SQLAlchemy")
    assert patch.ops[1].hunks[0].after == ("# 新文档",)


def test_parse_splits_multiple_hunks_on_double_at():
    text = (
        "*** Begin Patch\n"
        "*** Update File: notes.md\n"
        "@@ 第一段\n"
        " 一\n"
        "-二\n"
        "+二改\n"
        "@@ 第三段\n"
        " 三\n"
        "-四\n"
        "+四改\n"
        "*** End Patch\n"
    )
    patch = parse(text)
    assert len(patch.ops[0].hunks) == 2


def test_parse_without_double_at_makes_one_implicit_hunk(tmp_path):
    """`@@` 缺失：所有行归入同一个隐式 hunk（模型常忘，这是确定行为，不是崩溃）。"""
    text = (
        "*** Begin Patch\n"
        "*** Update File: notes.md\n"
        " # 标题\n"
        "-旧\n"
        "+新\n"
        "*** End Patch\n"
    )
    patch = parse(text)
    assert len(patch.ops[0].hunks) == 1
    _write(tmp_path, "notes.md", "# 标题\n旧\n")
    result = apply(patch, tmp_path)
    assert result.ok, result.error
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "# 标题\n新\n"


def test_parse_accepts_crlf_and_a_missing_envelope():
    """CRLF 补丁体与"没写 Begin/End"都认：前者是 Windows 剪贴板常态，后者是模型常犯。"""
    text = "*** Update File: notes.md\r\n-旧\r\n+新\r\n"
    patch = parse(text)
    assert patch.ops[0].hunks[0].before == ("旧",)
    assert patch.ops[0].hunks[0].after == ("新",)


def test_parse_rejects_an_empty_patch():
    with pytest.raises(PatchError) as excinfo:
        parse("*** Begin Patch\n*** End Patch\n")
    assert "没有任何文件操作" in str(excinfo.value)


def test_parse_rejects_content_before_any_file_header():
    with pytest.raises(PatchError) as excinfo:
        parse("*** Begin Patch\n+孤儿新增行\n*** End Patch\n")
    assert "文件头" in str(excinfo.value)


def test_parse_rejects_an_update_with_no_changes():
    with pytest.raises(PatchError) as excinfo:
        parse("*** Begin Patch\n*** Update File: notes.md\n*** End Patch\n")
    assert "没有任何改动行" in str(excinfo.value)


# ── 应用：三种 op ───────────────────────────────────────────────────────────


def test_apply_add_update_delete(tmp_path):
    _write(tmp_path, "notes.md", "# 项目\n使用 FastAPI\n")
    _write(tmp_path, "docs/old.md", "要删掉的\n")
    text = (
        "*** Begin Patch\n"
        "*** Update File: notes.md\n"
        "-使用 FastAPI\n"
        "+使用 FastAPI 与 SQLAlchemy\n"
        "*** Add File: docs/new.md\n"
        "+# 新文档\n"
        "+正文\n"
        "*** Delete File: docs/old.md\n"
        "*** End Patch\n"
    )
    result = parse_and_apply(text, tmp_path)
    assert result.ok, result.error
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "# 项目\n使用 FastAPI 与 SQLAlchemy\n"
    assert (tmp_path / "docs/new.md").read_text(encoding="utf-8") == "# 新文档\n正文\n"
    assert not (tmp_path / "docs/old.md").exists()
    assert [item["op"] for item in result.applied] == ["update", "add", "delete"]
    assert result.summary().startswith("已应用：")


def test_apply_multiple_hunks_in_one_file(tmp_path):
    _write(tmp_path, "notes.md", "一\n二\n三\n四\n")
    text = (
        "*** Begin Patch\n"
        "*** Update File: notes.md\n"
        "@@ 一\n"
        " 一\n"
        "-二\n"
        "+二改\n"
        "@@ 三\n"
        " 三\n"
        "-四\n"
        "+四改\n"
        "*** End Patch\n"
    )
    result = parse_and_apply(text, tmp_path)
    assert result.ok, result.error
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "一\n二改\n三\n四改\n"


def test_apply_keeps_the_rest_of_the_file_byte_for_byte(tmp_path):
    """补丁只动被改的行：其余字节（含 CRLF 风格）一个不变。"""
    original = "# 标题\r\n\r\n保留这段很长的正文\r\n最后一行\r\n"
    # 用 write_bytes 写：`write_text` 在 Windows 上会把 \n 再翻译一次，夹具自己就先错了
    (tmp_path / "notes.md").write_bytes(original.encode("utf-8"))
    text = (
        "*** Begin Patch\n"
        "*** Update File: notes.md\n"
        "-最后一行\n"
        "+新的最后一行\n"
        "*** End Patch\n"
    )
    result = parse_and_apply(text, tmp_path)
    assert result.ok, result.error
    assert (tmp_path / "notes.md").read_bytes() == (
        "# 标题\r\n\r\n保留这段很长的正文\r\n新的最后一行\r\n".encode()
    )


# ── 拒绝：上下文不匹配（原子性）────────────────────────────────────────────


def test_context_mismatch_writes_nothing_at_all(tmp_path):
    """原子性：A 文件合法、B 文件上下文不匹配 → **A 也不能被写**。"""
    _write(tmp_path, "a.md", "A 原文\n")
    _write(tmp_path, "b.md", "B 原文\n")
    text = (
        "*** Begin Patch\n"
        "*** Update File: a.md\n"
        "-A 原文\n"
        "+A 改后\n"
        "*** Update File: b.md\n"
        "-这行在文件里根本不存在\n"
        "+B 改后\n"
        "*** End Patch\n"
    )
    result = parse_and_apply(text, tmp_path)
    assert result.ok is False
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == "A 原文\n", "A 不该被写"
    assert (tmp_path / "b.md").read_text(encoding="utf-8") == "B 原文\n"
    assert "b.md" in result.error


def test_mismatch_names_the_hunk_and_the_line(tmp_path):
    """报错必须指到"第几个 hunk、第几行、哪一行文本"。"""
    _write(tmp_path, "notes.md", "一\n二\n三\n")
    text = (
        "*** Begin Patch\n"
        "*** Update File: notes.md\n"
        " 一\n"
        "-二\n"
        "+二改\n"
        "@@ 三\n"
        " 三\n"
        "-不存在的行\n"
        "+四改\n"
        "*** End Patch\n"
    )
    result = parse_and_apply(text, tmp_path)
    assert result.ok is False
    assert "第 2 个 hunk" in result.error
    assert "第 2 行" in result.error  # before = ("三", "不存在的行")，缺的是第 2 行
    assert "不存在的行" in result.error
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "一\n二\n三\n"


def test_apply_is_idempotent_on_a_second_run(tmp_path):
    """打完补丁再打一次必须失败（上下文已变），而不是把文件改成别的东西。"""
    _write(tmp_path, "notes.md", "旧行\n")
    text = "*** Begin Patch\n*** Update File: notes.md\n-旧行\n+新行\n*** End Patch\n"
    first = parse_and_apply(text, tmp_path)
    assert first.ok, first.error
    second = parse_and_apply(text, tmp_path)
    assert second.ok is False
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "新行\n"


# ── 拒绝：路径逃逸 ──────────────────────────────────────────────────────────


def test_path_escape_is_rejected(tmp_path):
    """补丁里的 `../` 不能逃出授权目录（复用 resolve_path 的同一份判据）。"""
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("外部文件\n", encoding="utf-8")
    text = (
        "*** Begin Patch\n"
        "*** Update File: ../outside.md\n"
        "-外部文件\n"
        "+被改了\n"
        "*** End Patch\n"
    )
    result = parse_and_apply(text, root)
    assert result.ok is False
    assert "授权工作目录" in result.error
    assert outside.read_text(encoding="utf-8") == "外部文件\n"


def test_add_existing_file_is_rejected(tmp_path):
    _write(tmp_path, "exists.md", "已有内容\n")
    text = "*** Begin Patch\n*** Add File: exists.md\n+覆盖\n*** End Patch\n"
    result = parse_and_apply(text, tmp_path)
    assert result.ok is False and "已存在" in result.error
    assert (tmp_path / "exists.md").read_text(encoding="utf-8") == "已有内容\n"


def test_update_missing_file_is_rejected(tmp_path):
    text = "*** Begin Patch\n*** Update File: nope.md\n-甲\n+乙\n*** End Patch\n"
    result = parse_and_apply(text, tmp_path)
    assert result.ok is False and "Add File" in result.error


def test_deleting_a_missing_file_is_rejected(tmp_path):
    text = "*** Begin Patch\n*** Delete File: nope.md\n*** End Patch\n"
    result = parse_and_apply(text, tmp_path)
    assert result.ok is False and "不存在" in result.error


def test_the_same_file_twice_in_one_patch_is_rejected(tmp_path):
    _write(tmp_path, "notes.md", "甲\n")
    text = (
        "*** Begin Patch\n"
        "*** Update File: notes.md\n-甲\n+乙\n"
        "*** Update File: notes.md\n-乙\n+丙\n"
        "*** End Patch\n"
    )
    result = parse_and_apply(text, tmp_path)
    assert result.ok is False
    assert "改了两次" in result.error
    assert (tmp_path / "notes.md").read_text(encoding="utf-8") == "甲\n"


def test_apply_returns_a_result_instead_of_raising(tmp_path):
    """所有"补丁不适用"的情况都走 ApplyResult（模型侧只需要认一种错误形状）。"""
    result = apply(parse("*** Begin Patch\n*** Delete File: x\n*** End Patch\n"), tmp_path)
    assert isinstance(result, patch_module.ApplyResult)
    assert result.ok is False and result.error


# ── 接线：`apply_patch` 工具 ────────────────────────────────────────────────


@pytest.fixture()
async def env(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="改文件"))
    registry = ToolRegistry(settings, store)
    try:
        yield settings, registry, task.id
    finally:
        await store.engine.dispose()


async def test_apply_patch_tool_is_exposed_and_dispatches(env):
    """工具必须真的进了 `LOCAL_TOOLS` 与分发链（否则模型根本看不到它）。"""
    settings, registry, task_id = env
    assert "apply_patch" in registry.tools
    (settings.workspace / "notes.md").write_text("旧\n", encoding="utf-8")
    result = await registry.execute(
        task_id,
        "patch1",
        "apply_patch",
        {"patch": "*** Begin Patch\n*** Update File: notes.md\n-旧\n+新\n*** End Patch\n"},
    )
    assert result["status"] == "ok", result
    assert (settings.workspace / "notes.md").read_text(encoding="utf-8") == "新\n"
    assert result["data"]["verified"] is True


async def test_apply_patch_tool_reports_a_mismatch_as_an_error(env):
    """上下文不匹配 → `status=error`（不是 ok + 空 applied），且磁盘未动。"""
    settings, registry, task_id = env
    (settings.workspace / "notes.md").write_text("旧\n", encoding="utf-8")
    result = await registry.execute(
        task_id,
        "patch2",
        "apply_patch",
        {"patch": "*** Begin Patch\n*** Update File: notes.md\n-不存在\n+新\n*** End Patch\n"},
    )
    assert result["status"] == "error"
    assert "hunk" in result["error"]
    assert (settings.workspace / "notes.md").read_text(encoding="utf-8") == "旧\n"


async def test_apply_patch_tool_rejects_an_escaping_path(env):
    settings, registry, task_id = env
    result = await registry.execute(
        task_id,
        "patch3",
        "apply_patch",
        {"patch": "*** Begin Patch\n*** Add File: ../escape.md\n+坏\n*** End Patch\n"},
    )
    assert result["status"] == "error"
    assert "授权工作目录" in result["error"]
    assert not (settings.workspace.parent / "escape.md").exists()
