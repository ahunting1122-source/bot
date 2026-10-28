"""`hook_output_spill`（MATRIX 5.7）的契约测试：阈值 / 文件布局 / 提示行 / 接进 `as_tool_text`。

对齐 Hermes `tools/hook_output_spill.py`：**10,000 字符**阈值、头尾各 **500**、
文件按任务分组、落盘失败绝不抛异常。每个用例盯住一个"为什么这么设计"的点：

1. 不超阈值时**逐字返回**（向后兼容的底线）；
2. 超阈值时**文件真的在盘上**，且提示行同时给出**路径**与**原始字符数**——
   这两样缺任何一个，模型都会把预览当成完整内容；
3. 头尾**恰好** 500，不是"大约"；
4. `spill_safe` 不破坏已有格式（JSON 进 JSON 出），落盘失败时退化而不是抛；
5. 接进 `as_tool_text`（唯一出口），且与三层治理叠加时**治理在前、spill 在后**。
"""

from __future__ import annotations

import json

import pytest

from app.budget import (
    DEFAULT_SPILL_HEAD,
    DEFAULT_SPILL_LIMIT,
    DEFAULT_SPILL_TAIL,
    BudgetConfig,
    govern,
    spill_notice,
    spill_output,
    spill_safe,
)
from app.config import Settings
from app.main import migrate
from app.tools import TOOL_SPILL_KEY, as_tool_text


@pytest.fixture()
def data_dir(tmp_path):
    """隔离的数据目录（spill 落在 `<data>/spill/<task>/` 下）。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    return settings.data_dir


def _big(size: int = 20_000) -> str:
    """一段长度精确为 `size` 的正文（内容单调，便于断言头尾）。"""
    return "".join(chr(ord("a") + index % 26) for index in range(size))


def _many_lines(size: int = 20_000, width: int = 60) -> str:
    """多行正文（约 `size` 字符）。

    为什么不用 `_big`：三层治理的第一层有**单行宽度上限 2,000**，一行 20,000 字符的
    正文会被它先截掉，spill 层就永远看不到超限输入。真实工具结果本来就是多行的
    （日志、JSON、文件内容），所以"多行"才是这一层要面对的形态。
    """
    lines: list[str] = []
    total = 0
    index = 0
    while total < size:
        line = f"{index:05d} " + "x" * (width - 6)
        lines.append(line)
        total += len(line) + 1
        index += 1
    return "\n".join(lines)


# ── 1. 不超阈值：逐字返回 ───────────────────────────────────────────────────


def test_under_limit_returns_text_unchanged_and_writes_nothing(data_dir):
    """没超阈值 → `spilled=False`、正文逐字相同、**一个文件都不写**。"""
    text = "短输出，不需要落盘"
    result = spill_output(text, task_id="t1", tool="local_read_file", data_dir=data_dir)
    assert result.spilled is False
    assert result.preview == text
    assert result.path is None
    assert result.chars == len(text)
    assert not (data_dir / "spill").exists()


def test_limit_is_10000_chars_matching_hermes(data_dir):
    """阈值口径：10,000 字符（照 Hermes `hooks.output_spill.max_chars` 默认值）。"""
    assert (DEFAULT_SPILL_LIMIT, DEFAULT_SPILL_HEAD, DEFAULT_SPILL_TAIL) == (10_000, 500, 500)
    # 恰好等于阈值 → 不落盘（`<=`，不是 `<`）：正好 10,000 字符的结果没必要多一次 IO。
    exact = spill_output("x" * 10_000, task_id="t", tool="t", data_dir=data_dir)
    assert exact.spilled is False
    over = spill_output("x" * 10_001, task_id="t", tool="t", data_dir=data_dir)
    assert over.spilled is True


# ── 2. 超阈值：落盘 + 头尾预览 ──────────────────────────────────────────────


def test_over_limit_writes_file_under_spill_task_dir(data_dir):
    """文件落在 `<data>/spill/<task>/<tool>-<n>.txt`，内容就是原文（多一个收尾换行）。"""
    text = _big(20_000)
    result = spill_output(text, task_id="task-1", tool="run_command", data_dir=data_dir)

    assert result.spilled is True
    assert result.path is not None
    assert result.path.is_file()
    assert result.path.parent == data_dir / "spill" / "task-1"
    assert result.path.name == "run_command-1.txt"
    # 原文完整落盘（末尾补一个换行，方便 tail/编辑器读最后一行）
    assert result.path.read_text(encoding="utf-8") == text + "\n"
    # 原子写不留临时文件
    assert [item.name for item in result.path.parent.iterdir()] == ["run_command-1.txt"]


def test_preview_keeps_exactly_head_500_and_tail_500(data_dir):
    """头 500 + 提示行 + 尾 500，**中间内容不在上下文里**。"""
    text = _big(20_000)
    result = spill_output(text, task_id="t", tool="tool", data_dir=data_dir)
    assert result.cut.head_kept == 500
    assert result.cut.tail_kept == 500
    assert result.preview.startswith(text[:500])
    assert result.preview.endswith(text[-500:])
    assert text[500:1000] not in result.preview  # 中间真的被截了
    # 预览远小于原文（否则"按上下文预算裁剪"这件事没发生）
    assert len(result.preview) < 2_000


def test_notice_contains_path_and_original_char_count(data_dir):
    """提示行必须同时给出**文件路径**与**原始字符数**，并点明中间被截。"""
    text = _big(12_345)
    result = spill_output(text, task_id="t", tool="tool", data_dir=data_dir)
    notice = spill_notice(
        result.chars, result.cut.head_kept, result.cut.tail_kept, result.path
    )
    assert str(result.path) in result.preview
    assert "12345" in result.preview
    assert "已截断" in notice
    assert "不在上下文里" in notice
    # 告诉模型"去哪看"以及"用什么读"——只说"已截断"模型会放弃
    assert "local_read_file" in notice
    assert "原文共 12345 字符" in notice
    # 中间被截掉的字符数也算得出来
    assert str(12345 - 1_000) in notice


def test_spill_files_are_numbered_per_task_and_tool(data_dir):
    """同一任务里同名工具依次 `-1`、`-2`；换个任务另起目录。"""
    first = spill_output(_big(), task_id="t1", tool="run_command", data_dir=data_dir)
    second = spill_output(_big(30_000), task_id="t1", tool="run_command", data_dir=data_dir)
    other = spill_output(_big(), task_id="t2", tool="run_command", data_dir=data_dir)
    assert first.path is not None and second.path is not None and other.path is not None
    assert first.path.name == "run_command-1.txt"
    assert second.path.name == "run_command-2.txt"
    assert other.path.parent.name == "t2"
    assert other.path.name == "run_command-1.txt"


def test_task_id_cannot_escape_data_dir(data_dir):
    """`task_id` 里的 `..` / 斜杠被收敛掉：落盘不能写到 data_dir 之外。"""
    result = spill_output(_big(), task_id="../../evil", tool="tool", data_dir=data_dir)
    assert result.path is not None
    assert data_dir.resolve() in result.path.resolve().parents


# ── 3. spill_safe：不破坏已有格式、永不抛 ───────────────────────────────────


def test_spill_safe_returns_small_text_verbatim():
    """小的、非 JSON 的正文原样返回（不解析、不改写、不落盘）。"""
    assert spill_safe("hello") == "hello"
    assert spill_safe("") == ""


def test_spill_safe_keeps_json_parseable(data_dir):
    """超限的 JSON 结果 → 仍然合法的 JSON 信封（不把对象从中间劈开）。"""
    payload = {"status": "ok", "data": {"text": _big(20_000)}}
    text = json.dumps(payload, ensure_ascii=False)
    out = spill_safe(text, task_id="t", tool="local_read_file", data_dir=data_dir)
    parsed = json.loads(out)  # 关键：仍然是合法 JSON
    assert parsed["_spilled"] is True
    assert parsed["_chars"] == len(text)
    assert parsed["_spill_path"]
    assert "已截断" in parsed["_notice"]
    assert len(parsed["head"]) == 500
    assert len(parsed["tail"]) == 500


def test_spill_safe_degrades_instead_of_raising(data_dir):
    """没有 data_dir 时不抛异常，而是内联截断并**明说找不回来**。"""
    payload = {"data": {"text": _big(20_000)}}
    text = json.dumps(payload, ensure_ascii=False)
    parsed = json.loads(spill_safe(text, task_id="t", tool="tool", data_dir=None))
    assert parsed["_spilled"] is True
    assert parsed["_spill_path"] is None
    assert "找不回来" in parsed["_notice"]


def test_spill_safe_keeps_non_object_json_as_plain_preview(data_dir):
    """JSON 数组不被包成对象（那会改变语义），退回纯文本预览。"""
    text = json.dumps([_big(20_000)], ensure_ascii=False)
    out = spill_safe(text, task_id="t", tool="tool", data_dir=data_dir)
    assert out.startswith('["a')
    assert "已截断" in out


# ── 4. 接进 as_tool_text（唯一出口）────────────────────────────────────────


def test_as_tool_text_spills_oversized_result(data_dir):
    """`as_tool_text` 是模型侧唯一出口：超限结果在这里落盘，输出带路径。"""
    text = _big(20_000)
    result = {
        "status": "ok",
        "data": {"text": text},
        "_tool": "local_read_file",  # 非高风险来源，不包"不可信数据"标记
        TOOL_SPILL_KEY: {"task_id": "task-9", "data_dir": str(data_dir)},
    }
    out = as_tool_text(result)
    parsed = json.loads(out)
    assert parsed["_spilled"] is True
    path = parsed["_spill_path"]
    assert path and (data_dir / "spill" / "task-9").is_dir()
    # 落盘文件里是**完整**原文（含被截掉的中段）
    assert text in (data_dir / "spill" / "task-9" / "local_read_file-1.txt").read_text(
        encoding="utf-8"
    )
    # 内部字段不进模型上下文
    assert TOOL_SPILL_KEY not in parsed and "_tool" not in parsed


def test_as_tool_text_without_spill_context_is_unchanged(data_dir):
    """没有 spill 上下文（`errors_retry` 自造的载荷）→ 一个字节都不动。"""
    result = {"status": "ok", "data": {"text": _big(20_000)}}
    out = as_tool_text(result)
    assert json.loads(out) == result
    assert not (data_dir / "spill").exists()


def test_small_result_is_not_spilled_through_as_tool_text(data_dir):
    """小结果照常直出：spill 不能变成"每条结果都包一层信封"。"""
    result = {
        "status": "ok",
        "data": {"path": "a.txt", "text": "两行\n文本"},
        TOOL_SPILL_KEY: {"task_id": "t", "data_dir": str(data_dir)},
    }
    parsed = json.loads(as_tool_text(result))
    assert "_spilled" not in parsed
    assert parsed["data"]["text"] == "两行\n文本"


# ── 5. 与三层治理的叠加顺序：治理在前，spill 在后 ──────────────────────────


def test_govern_spills_first_then_hook_spill_does_nothing(data_dir):
    """三层治理先落盘，spill 层看到的是预览，不再截一次。

    顺序反了的症状很具体：spill 先把正文截成"头尾 + 路径"，治理层再按阈值判一次，
    第二次截掉的是第一次的提示行与路径——模型既看不到"原文多大"，也读不到文件。
    这里把第二层阈值压到 5,000（默认 100K 在 `cap_output` 的 50K 字节上限之下，
    正常永远轮不到它），逼出"治理层先落盘"的状态。
    """
    text = _many_lines(20_000)
    config = BudgetConfig(default_result_size=5_000)
    governed = govern("local_read_file", text, data_dir=data_dir, config=config)
    # 第一层治理已经把它落盘（`cache/spillover`），正文变成 1,500 字符的预览
    assert governed.spill is not None
    assert len(governed.text) < DEFAULT_SPILL_LIMIT
    assert "完整内容已落盘" in governed.text

    out = as_tool_text(
        {
            "status": "ok",
            "data": {"text": governed.text},
            TOOL_SPILL_KEY: {"task_id": "t", "data_dir": str(data_dir)},
        }
    )
    # 治理层的提示行**完整保留**（spill 层没有第二次裁剪）
    assert "完整内容已落盘" in out
    assert json.loads(out)["data"]["text"] == governed.text
    # spill 层没有额外落盘
    assert not (data_dir / "spill").exists()


def test_medium_result_is_caught_by_hook_spill_not_by_govern(data_dir):
    """20,000 字符：三层治理（100K）看不见，spill 层（10K）接住——两层分工明确。"""
    text = _many_lines(20_000)
    governed = govern("local_read_file", text, data_dir=data_dir)
    assert governed.spill is None
    assert governed.text == text

    out = as_tool_text(
        {
            "status": "ok",
            "data": {"text": governed.text},
            TOOL_SPILL_KEY: {"task_id": "t", "data_dir": str(data_dir)},
        }
    )
    assert json.loads(out)["_spilled"] is True
    assert (data_dir / "spill" / "t").is_dir()


async def test_registry_execute_attaches_spill_context(tmp_path):
    """`ToolRegistry.execute` 挂上 spill 上下文，`as_tool_text` 才有的可落。

    这两个字段是**成对**出现的：execute 挂、as_tool_text 剥。少了任何一半，
    大结果就只能靠"截断"而不是"落盘"。
    """
    from app.storage import Conversation, Store, Task
    from app.tools import ToolRegistry

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    try:
        conversation = await store.save(Conversation())
        task = await store.save(Task(conversation_id=conversation.id, goal="spill"))
        (settings.workspace).mkdir(parents=True, exist_ok=True)
        (settings.workspace / "big.txt").write_text(_many_lines(20_000), encoding="utf-8")
        registry = ToolRegistry(settings, store)
        result = await registry.execute(
            task.id, "op-spill", "local_read_file", {"path": "big.txt"}
        )
        assert result[TOOL_SPILL_KEY]["task_id"] == task.id
        assert result[TOOL_SPILL_KEY]["data_dir"] == str(settings.data_dir)
        parsed = json.loads(as_tool_text(result))
        assert parsed["_spilled"] is True
        assert (settings.data_dir / "spill" / task.id).is_dir()
    finally:
        await store.engine.dispose()
