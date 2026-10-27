"""工具结果三层治理：阈值解析 / 输出层裁剪 / 落盘 / 整轮预算 / 门面串联。

对齐 Hermes 的 `budget_config.py` + `tool_output_limits.py` + `tool_result_storage.py`。
每个用例都盯住一个"为什么这么设计"的点，而不只是数值：头尾比例为什么不同、
`read_file` 为什么钉死、预览为什么是头尾拼接、降级为什么必须如实记账。
"""

import os
import time
from pathlib import Path

import pytest

from app.budget import (
    PINNED_THRESHOLDS,
    BudgetConfig,
    TurnBudget,
    cap_output,
    cleanup_spillover,
    govern,
    resolve_threshold,
    spill,
    spillover_dir,
)
from app.config import Settings
from app.main import migrate
from app.policies import DefaultToolResultPolicy
from app.storage import Conversation, Store, Task, ToolResult

# ── 阈值解析 ────────────────────────────────────────────────────────────────


def test_default_config_matches_hermes_constants():
    """默认值即对齐：三层各自的数值必须与 Hermes 逐字一致。"""
    cfg = BudgetConfig()
    assert cfg.default_result_size == 100_000
    assert cfg.turn_budget == 200_000
    assert cfg.preview_size == 1_500
    assert cfg.mcp_result_size == 50_000
    assert (cfg.max_bytes, cfg.max_lines, cfg.max_line_length) == (50_000, 2_000, 2_000)
    assert PINNED_THRESHOLDS["read_file"] == float("inf")
    # 零参也能解析（调用方不必先造 config）
    assert resolve_threshold("browser_observe") == 100_000
    assert resolve_threshold("read_file") == float("inf")


def test_resolve_threshold_priority_pinned_over_overrides_over_mcp_over_default():
    """四档优先级：pinned > overrides > mcp_ 前缀 > default。"""
    cfg = BudgetConfig(
        default_result_size=100_000,
        mcp_result_size=50_000,
        # 故意给 read_file 也写了 override：pinned 必须压过它
        tool_overrides={"read_file": 1, "mcp_foo": 7_000, "web_search": 30_000},
    )
    assert resolve_threshold("read_file", config=cfg) == float("inf")  # ① pinned
    assert resolve_threshold("mcp_foo", config=cfg) == 7_000  # ② overrides 压过 mcp_
    assert resolve_threshold("mcp_bar", config=cfg) == 50_000  # ③ mcp_ 前缀
    assert resolve_threshold("web_search", config=cfg) == 30_000  # ④ overrides
    assert resolve_threshold("browser_observe", config=cfg) == 100_000  # ⑤ default


def test_mcp_threshold_is_capped_by_default_result_size():
    """小窗口模型缩了 default 之后，MCP 的固定值不能把上限顶回去。"""
    cfg = BudgetConfig(default_result_size=20_000, mcp_result_size=50_000)
    assert resolve_threshold("mcp_huge", config=cfg) == 20_000
    # 反过来（default 更大）时仍取 mcp 自己的紧阈值
    cfg = BudgetConfig(default_result_size=200_000, mcp_result_size=50_000)
    assert resolve_threshold("mcp_huge", config=cfg) == 50_000


# ── 第一层：输出层上限 ──────────────────────────────────────────────────────


def test_cap_output_leaves_small_results_byte_identical():
    """没超任何上限时逐字不变——这是向后兼容的底线。"""
    text = "第一行\n第二行\n"
    out, cut = cap_output(text, tool_name="browser_observe")
    assert out == text
    assert not cut.touched()
    assert cut.head_kept == len(text) and cut.tail_kept == 0


def test_cap_output_trims_lines_keeping_head_and_tail():
    """行数超限：头尾保留、中间用一行标记代替（不是砍尾巴）。"""
    text = "".join(f"L{i}\n" for i in range(100))
    cfg = BudgetConfig(max_lines=10, max_bytes=10_000_000, max_line_length=10_000)
    out, cut = cap_output(text, tool_name="browser_observe", config=cfg)
    assert cut.lines_cut == 90
    # 普通工具 70/30：头 7 行、尾 3 行
    assert "L0\n" in out and "L6\n" in out
    assert "L7\n" not in out
    assert "L97\n" in out and "L99\n" in out
    assert "L96\n" not in out
    assert "省略 90 行" in out


def test_cap_output_uses_40_60_split_for_terminal_tools():
    """终端类 40/60：构建日志的结论在尾部，所以尾部留得比头部多。"""
    text = "".join(f"L{i}\n" for i in range(100))
    cfg = BudgetConfig(max_lines=10, max_bytes=10_000_000, max_line_length=10_000)
    out, _ = cap_output(text, tool_name="run_command", config=cfg)
    assert "L3\n" in out and "L4\n" not in out  # 头 4 行
    assert "L94\n" in out and "L93\n" not in out  # 尾 6 行


def test_cap_output_truncates_overlong_single_line():
    """单行超宽：只截这一行并留标记，其它行不受影响。"""
    text = "x" * 5_000
    cfg = BudgetConfig(max_line_length=100, max_lines=2_000, max_bytes=10_000_000)
    out, cut = cap_output(text, tool_name="local_read_document", config=cfg)
    assert out == "x" * 100 + "…[本行已截断]"
    # 行宽截断不单列字段，但必须体现在 bytes_cut 上，否则会被当成"没裁"
    assert cut.touched() and cut.bytes_cut > 0


def test_cap_output_trims_bytes_after_lines():
    """字节超限：头尾保留（普通工具 70/30），中间标记。"""
    text = "y" * 200_000
    cfg = BudgetConfig(max_bytes=1_000, max_lines=2_000, max_line_length=200_000)
    out, cut = cap_output(text, tool_name="browser_observe", config=cfg)
    assert len(out.encode("utf-8")) < 2_000
    assert "已按字节上限 1000 截断" in out
    assert cut.bytes_cut > 190_000
    assert out.startswith("y") and out.endswith("y")


def test_cap_output_never_splits_a_multibyte_character():
    """字节裁剪按 UTF-8 边界切，不能产出半个汉字（那会是非法编码）。"""
    text = "汉" * 30_000  # 90_000 字节
    cfg = BudgetConfig(max_bytes=1_000, max_lines=2_000, max_line_length=200_000)
    out, _ = cap_output(text, tool_name="browser_observe", config=cfg)
    out.encode("utf-8").decode("utf-8")  # 不抛异常即合法
    assert "汉" in out


# ── 第二层：落盘 ────────────────────────────────────────────────────────────


def test_spill_writes_inside_data_dir_and_keeps_full_content(tmp_path):
    """落盘路径必须在 data_dir 内，且文件内容与原文逐字一致。"""
    data_dir = tmp_path / "data"
    text = "头" * 5_000 + "尾" * 5_000
    result = spill(text, tool_name="browser_observe", data_dir=data_dir)
    root = data_dir.resolve()
    assert root in result.path.resolve().parents
    assert result.path.read_text(encoding="utf-8") == text
    assert result.bytes == len(text.encode("utf-8"))
    # 预览 = 头 1_000 + 提示 + 尾 500
    assert result.preview.startswith(text[:1_000])
    assert result.preview.endswith(text[-500:])
    assert "完整内容已落盘" in result.preview
    assert str(result.path) in result.preview


def test_spill_filename_is_content_addressed_and_stable(tmp_path):
    """文件名 = 工具名 + 内容哈希：同样的内容重复落盘只会有一个文件。"""
    data_dir = tmp_path / "data"
    first = spill("同一份内容" * 500, tool_name="terminal", data_dir=data_dir)
    second = spill("同一份内容" * 500, tool_name="terminal", data_dir=data_dir)
    assert first.path == second.path
    assert first.path.parent == spillover_dir(data_dir)
    assert first.path.name.startswith("terminal-") and first.path.suffix == ".txt"


def test_spill_write_is_atomic_and_leaves_no_temp_file(tmp_path):
    """原子写：同目录临时文件 + os.replace，落盘后不留残文件。"""
    data_dir = tmp_path / "data"
    spill("内容" * 1_000, tool_name="terminal", data_dir=data_dir)
    entries = [p.name for p in spillover_dir(data_dir).iterdir()]
    assert len(entries) == 1
    assert entries[0].endswith(".txt") and not entries[0].startswith(".")


def test_spill_refuses_escape_through_a_symlinked_directory(tmp_path, monkeypatch):
    """目录被换成指向外部的符号链接时必须拒绝（本机无建链接权限，直接模拟解析结果）。

    这是 `spill_safety.py` 的威胁模型：落盘目录可预测、世界可发现，
    一旦被换成指向 `~/.ssh` 的链接，一次普通写入就能覆盖用户的关键文件。
    """
    data_dir = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    real_resolve = Path.resolve

    def fake_resolve(self, *args, **kwargs):
        resolved = real_resolve(self, *args, **kwargs)
        if resolved.name == "spillover":
            return outside  # 模拟 spillover 目录是一条指向外部的符号链接
        return resolved

    monkeypatch.setattr(Path, "resolve", fake_resolve)
    with pytest.raises(PermissionError):
        spill("x" * 1_000, tool_name="terminal", data_dir=data_dir)
    assert list(outside.iterdir()) == []


def test_spill_refuses_escape_through_a_real_symlink(tmp_path):
    """真符号链接版（需要建链接权限；Windows 默认普通用户没有 → 跳过）。"""
    data_dir = tmp_path / "data"
    outside = tmp_path / "outside"
    outside.mkdir()
    (data_dir / "cache").mkdir(parents=True)
    try:
        os.symlink(outside, data_dir / "cache" / "spillover", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("本机不允许创建符号链接")
    with pytest.raises(PermissionError):
        spill("x" * 1_000, tool_name="terminal", data_dir=data_dir)
    assert list(outside.iterdir()) == []


def test_cleanup_spillover_removes_expired_and_keeps_fresh(tmp_path):
    """过期删、未过期留：落盘文件是可回收的中间产物，不能变成磁盘泄漏。"""
    data_dir = tmp_path / "data"
    old = spill("旧" * 1_000, tool_name="terminal", data_dir=data_dir).path
    fresh = spill("新" * 1_000, tool_name="terminal", data_dir=data_dir).path
    stale = time.time() - 48 * 3600
    os.utime(old, (stale, stale))
    assert cleanup_spillover(data_dir) == 1
    assert not old.exists() and fresh.exists()
    assert cleanup_spillover(data_dir) == 0


# ── 第三层：整轮预算 ────────────────────────────────────────────────────────


def test_turn_budget_passes_through_when_under_budget(tmp_path):
    turn = TurnBudget(data_dir=tmp_path, config=BudgetConfig(turn_budget=1_000))
    assert turn.add("browser_observe", "小结果") == "小结果"
    assert turn.total == 3
    assert turn.spilled == 0 and turn.degraded is False


def test_turn_budget_spills_the_largest_entry_first(tmp_path):
    """超预算时按大小降序落盘：最大那条先走，而不是"刚进来的那条"。"""
    turn = TurnBudget(data_dir=tmp_path, config=BudgetConfig(turn_budget=10_000))
    assert turn.add("first", "a" * 8_000) == "a" * 8_000
    second = turn.add("second", "b" * 6_000)  # 合计 14_000 > 10_000
    assert turn.spilled == 1
    assert turn.total <= 10_000
    assert second == "b" * 6_000  # 小的那条原样保留
    spilled = list(spillover_dir(tmp_path).iterdir())
    assert len(spilled) == 1
    assert spilled[0].name.startswith("first-")
    assert spilled[0].read_text(encoding="utf-8") == "a" * 8_000


def test_turn_budget_degrades_to_truncation_without_data_dir():
    """落盘不可用时退化为硬截断，并如实记 degraded——不能假装存下来了。"""
    turn = TurnBudget(config=BudgetConfig(turn_budget=1_000))
    out = turn.add("terminal", "z" * 20_000)
    assert turn.degraded is True
    assert turn.spilled == 0
    assert len(out) < 20_000
    assert "硬截断" in out


def test_turn_budget_reset_clears_the_round():
    turn = TurnBudget(config=BudgetConfig(turn_budget=100))
    turn.add("terminal", "z" * 5_000)
    assert turn.degraded is True
    turn.reset()
    assert turn.total == 0 and turn.spilled == 0 and turn.degraded is False


# ── 门面：三层串联 ──────────────────────────────────────────────────────────


def test_govern_runs_output_layer_before_turn_layer(tmp_path):
    """三层顺序：先输出层裁行，裁完不超阈值就不落盘，最后才进整轮累加。"""
    cfg = BudgetConfig(
        max_bytes=100_000, max_lines=10, max_line_length=100_000,
        default_result_size=5_000, turn_budget=1_000_000,
    )
    turn = TurnBudget(data_dir=tmp_path, config=cfg)
    text = "".join(f"L{i}\n" for i in range(100))
    governed = govern("terminal", text, turn=turn, config=cfg)
    assert governed.cut.lines_cut == 90  # 第一层动过
    assert governed.spill is None  # 裁完只剩 10 行 → 第二层不触发
    assert turn.total == len(governed.text)  # 第三层收下的是治理后的正文


def test_govern_spills_when_capped_text_is_still_over_threshold(tmp_path):
    """输出层裁完仍超单条阈值 → 落盘，上下文只留预览，落库的也是预览。"""
    cfg = BudgetConfig(
        max_bytes=10_000_000, max_lines=10_000, max_line_length=10_000_000,
        default_result_size=5_000, turn_budget=1_000_000,
    )
    turn = TurnBudget(data_dir=tmp_path, config=cfg)
    governed = govern("browser_observe", "字" * 20_000, turn=turn, config=cfg)
    assert governed.spill is not None
    assert governed.text == governed.spill.preview
    assert "完整内容已落盘" in governed.text
    assert governed.spill.path.read_text(encoding="utf-8") == "字" * 20_000
    assert governed.degraded is False


def test_govern_never_spills_read_file(tmp_path):
    """read_file 阈值钉死 inf：否则「落盘 → 模型读文件 → 又落盘」是死循环。"""
    # 阈值小到必然超；行宽/字节上限放开，好让这一条只测"钉死"这一件事
    cfg = BudgetConfig(default_result_size=10, max_bytes=10_000_000, max_line_length=1_000_000)
    governed = govern("read_file", "x" * 50_000, data_dir=tmp_path, config=cfg)
    assert governed.spill is None
    assert governed.degraded is False
    assert governed.text == "x" * 50_000


def test_govern_degrades_inline_when_spill_is_unavailable():
    """没给 data_dir 且超阈值：内联截断 + degraded，并明确告诉模型内容找不回来。"""
    cfg = BudgetConfig(default_result_size=1_000)
    governed = govern("browser_observe", "y" * 10_000, config=cfg)
    assert governed.degraded is True
    assert governed.spill is None
    assert "无法落盘" in governed.text


# ── 回归：既有调用点的行为一字不变 ──────────────────────────────────────────


def test_project_still_only_strips_off_context_keys():
    """回归：`project` 行为不变——只剥 OFF_CONTEXT_KEYS（structured），别的一概不碰。"""
    policy = DefaultToolResultPolicy()
    result = {
        "status": "ok",
        "data": {"text": "x", "structured": "# 标题\n正文", "browser_pid": 1},
    }
    projected = policy.project("browser_observe", result)
    assert "structured" not in projected["data"]
    assert projected["data"]["browser_pid"] == 1
    assert projected["data"]["text"] == "x"
    # 原对象不能被动过：digest 还要拿它去索引。
    assert result["data"]["structured"] == "# 标题\n正文"


def test_zero_arg_policy_keeps_the_legacy_single_threshold():
    """零参构造 = 现有调用点：limit 仍是旧的 16000，不悄悄换成 100_000。"""
    assert DefaultToolResultPolicy().limit == 16_000
    assert DefaultToolResultPolicy(config=BudgetConfig()).limit == 100_000


async def test_record_stores_governed_text_and_reports_spill(tmp_path):
    """接上三层之后：落库的是模型看到的那份正文，并带回 spill_path/cut/degraded。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="大结果"))
    cfg = BudgetConfig(
        max_bytes=10_000_000, max_lines=10_000, max_line_length=10_000_000,
        default_result_size=5_000,
    )
    policy = DefaultToolResultPolicy(data_dir=settings.data_dir, config=cfg)
    meta = await policy.record(
        store,
        task_id=task.id,
        operation_id="op-big",
        name="browser_observe",
        status="ok",
        host="",
        text="字" * 20_000,
    )
    assert meta["truncated"] is True
    assert meta["degraded"] is False
    assert meta["spill_path"] is not None
    assert meta["cut"]["bytes_cut"] >= 0
    row = await store.get(ToolResult, meta["id"])
    assert "完整内容已落盘" in row.text
    # 预算账按原文体积记：存的是预览，但 bytes/tokens 不能跟着缩水（否则预算被低估）
    assert row.bytes == len(("字" * 20_000).encode("utf-8"))
    assert row.tokens >= 20_000
    assert len(row.text) < row.bytes
    assert Path(meta["spill_path"]).read_text(encoding="utf-8") == "字" * 20_000
    await store.engine.dispose()
