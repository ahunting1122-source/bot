"""原子批处理与熔断的契约测试（`app/memory_batch.py`）。

最重要的一条是 **全有或全无**：故意让第 3 条非法，断言前 2 条**也没有落盘**。
没有这条测试，"原子性"就只是 docstring 里的一句话。
"""

from __future__ import annotations

import json

from app.config import Settings
from app.main import migrate
from app.memory_batch import (
    KEY_LIMIT,
    MAX_OPERATIONS,
    VALUE_LIMIT,
    ConsolidationBreaker,
    SqlMemoryBackend,
    apply_batch,
    consolidate,
    consolidate_guarded,
    plan_batch,
)
from app.memory_prompt import MEMORY_MAX_CHARS
from app.storage import Store


def _store(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    return settings, Store(settings.data_dir / "app.sqlite")


async def _entries(store) -> dict[str, str]:
    return await SqlMemoryBackend(store).all_entries()


# ── 全有或全无 ───────────────────────────────────────────────────────────────


async def test_apply_batch_is_all_or_nothing(tmp_path):
    """第 3 条非法 → 前 2 条也不能落盘。这是整个模块的核心承诺。"""
    _, store = _store(tmp_path)
    await store.set_memory("k1", "v1")

    result = await apply_batch(
        store,
        [
            {"op": "add", "key": "k2", "value": "v2"},
            {"op": "update", "key": "k1", "value": "v1-changed"},
            {"op": "add", "key": "k1", "value": "撞了已存在的 key"},
        ],
    )
    assert result.ok is False
    assert result.failed_op == 3
    assert result.wrote is False  # 一个字节都没写
    assert "已存在" in result.error
    # 关键断言：前两条的痕迹一点都不该有
    assert await _entries(store) == {"k1": "v1"}


async def test_apply_batch_applies_mixed_operations(tmp_path):
    _, store = _store(tmp_path)
    await store.set_memory("k1", "v1")
    await store.set_memory("k2", "v2")

    result = await apply_batch(
        store,
        [
            {"op": "delete", "key": "k2"},
            {"op": "update", "key": "k1", "value": "v1b"},
            {"op": "add", "key": "k3", "value": "v3"},
        ],
    )
    assert result.ok is True
    assert result.applied == 3
    assert result.wrote is True
    assert await _entries(store) == {"k1": "v1b", "k3": "v3"}


async def test_apply_batch_rejects_empty_and_unknown_operations(tmp_path):
    _, store = _store(tmp_path)
    assert (await apply_batch(store, [])).ok is False
    assert (await apply_batch(store, "不是列表")).ok is False
    unknown = await apply_batch(store, [{"op": "upsert", "key": "k", "value": "v"}])
    assert unknown.ok is False and "未知操作" in unknown.error


async def test_apply_batch_requires_existing_key_for_update(tmp_path):
    _, store = _store(tmp_path)
    result = await apply_batch(store, [{"op": "update", "key": "不存在", "value": "v"}])
    assert result.ok is False
    assert "改用 add" in result.error


# ── 长度与预算上限 ───────────────────────────────────────────────────────────


async def test_apply_batch_rejects_overlong_key(tmp_path):
    _, store = _store(tmp_path)
    result = await apply_batch(store, [{"op": "add", "key": "k" * (KEY_LIMIT + 1), "value": "v"}])
    assert result.ok is False
    assert str(KEY_LIMIT) in result.error
    assert await _entries(store) == {}


async def test_apply_batch_accepts_key_at_the_limit(tmp_path):
    _, store = _store(tmp_path)
    result = await apply_batch(store, [{"op": "add", "key": "k" * KEY_LIMIT, "value": "v"}])
    assert result.ok is True


async def test_apply_batch_rejects_overlong_value(tmp_path):
    _, store = _store(tmp_path)
    result = await apply_batch(store, [{"op": "add", "key": "k", "value": "v" * (VALUE_LIMIT + 1)}])
    assert result.ok is False
    assert str(VALUE_LIMIT) in result.error
    assert await _entries(store) == {}


async def test_apply_batch_rejects_too_many_operations(tmp_path):
    _, store = _store(tmp_path)
    operations = [{"op": "add", "key": f"k{index}", "value": "v"} for index in range(MAX_OPERATIONS + 1)]
    result = await apply_batch(store, operations)
    assert result.ok is False
    assert str(MAX_OPERATIONS) in result.error


async def test_apply_batch_rejects_total_over_budget(tmp_path):
    """总字符按**最终态**算：单条都在限内，加起来超 2,200 就整批拒绝。"""
    _, store = _store(tmp_path)
    operations = [
        {"op": "add", "key": f"记忆{index}", "value": "长" * 500} for index in range(5)
    ]
    result = await apply_batch(store, operations)
    assert result.ok is False
    assert "预算" in result.error
    assert await _entries(store) == {}


async def test_apply_batch_budget_check_uses_final_state_only(tmp_path):
    """中间态超限无所谓：先删后加正是批处理存在的理由。"""
    _, store = _store(tmp_path)
    for index in range(4):
        await store.set_memory(f"旧记忆{index}", "长" * 500)
    assert len(await _entries(store)) == 4

    result = await apply_batch(
        store,
        [
            {"op": "delete", "key": "旧记忆0"},
            {"op": "delete", "key": "旧记忆1"},
            {"op": "delete", "key": "旧记忆2"},
            {"op": "delete", "key": "旧记忆3"},
            {"op": "add", "key": "新记忆", "value": "短"},
        ],
    )
    assert result.ok is True
    assert await _entries(store) == {"新记忆": "短"}


# ── 回滚 ─────────────────────────────────────────────────────────────────────


class _FlakyBackend(SqlMemoryBackend):
    """先真的写一半，再抛异常——模拟"后端只提交了一部分"。

    SQLite 的单事务不会这样，但将来换 HTTP / 文件后端就会，所以补偿路径必须被覆盖。
    """

    def __init__(self, store):
        super().__init__(store)
        self.calls = 0

    async def commit(self, mutations):
        self.calls += 1
        if self.calls == 1:
            await super().commit(mutations[:1])
            raise RuntimeError("模拟写入中途失败")
        await super().commit(mutations)


async def test_apply_batch_rolls_back_when_write_fails(tmp_path):
    _, store = _store(tmp_path)
    await store.set_memory("k1", "v1")

    result = await apply_batch(
        store,
        [
            {"op": "update", "key": "k1", "value": "改了一半"},
            {"op": "add", "key": "k9", "value": "新建的"},
        ],
        backend=_FlakyBackend(store),
    )
    assert result.ok is False
    assert result.wrote is True
    assert result.rolled_back is True
    assert "已回滚" in result.error
    # 补偿真的生效：旧值回来了，新建的 key 没留下
    assert await _entries(store) == {"k1": "v1"}


# ── plan_batch：纯函数 ───────────────────────────────────────────────────────


def test_plan_batch_is_pure_and_reports_final_state():
    current = {"k1": "v1"}
    result = plan_batch(current, [{"op": "add", "key": "k2", "value": "v2"}])
    assert result.ok is True
    assert result.final == {"k1": "v1", "k2": "v2"}
    # 入参没被就地改动
    assert current == {"k1": "v1"}


def test_plan_batch_failure_reports_pre_batch_state():
    """失败时回显的必须是**批处理前**的状态，否则调用方会基于幻觉改数据。"""
    current = {"k1": "v1"}
    result = plan_batch(
        current,
        [
            {"op": "add", "key": "k2", "value": "v2"},
            {"op": "update", "key": "缺失", "value": "v"},
        ],
    )
    assert result.ok is False
    assert result.failed_op == 2
    assert result.final == {"k1": "v1"}


# ── 熔断 ─────────────────────────────────────────────────────────────────────


def test_breaker_trips_after_three_failures_in_one_turn(tmp_path):
    breaker = ConsolidationBreaker(tmp_path / "data")
    assert breaker.should_attempt("turn-1") is True
    for expected in (1, 2):
        state = breaker.record_failure("turn-1")
        assert state["failures"] == expected
        assert state["tripped"] is False
        assert breaker.should_attempt("turn-1") is True
    state = breaker.record_failure("turn-1")
    assert state["failures"] == 3
    assert state["tripped"] is True
    assert breaker.should_attempt("turn-1") is False


def test_breaker_state_persists_to_disk(tmp_path):
    breaker = ConsolidationBreaker(tmp_path / "data")
    breaker.record_failure("t")
    breaker.record_failure("t")
    path = tmp_path / "data" / "memory" / "breaker.json"
    assert path.exists()
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["failures"] == 2
    # 新实例（= 新进程）读到同一份状态
    assert ConsolidationBreaker(tmp_path / "data").state("t")["failures"] == 2


def test_breaker_resets_on_next_turn(tmp_path):
    breaker = ConsolidationBreaker(tmp_path / "data")
    for _ in range(3):
        breaker.record_failure("turn-1")
    assert breaker.should_attempt("turn-1") is False
    # 下一轮自动重置
    assert breaker.should_attempt("turn-2") is True
    assert breaker.state("turn-2")["failures"] == 0


def test_breaker_success_clears_counters(tmp_path):
    breaker = ConsolidationBreaker(tmp_path / "data")
    breaker.record_failure("t")
    breaker.record_failure("t")
    breaker.record_success("t")
    state = breaker.state("t")
    assert state["failures"] == 0 and state["tripped"] is False


async def test_guarded_consolidation_stops_calling_when_tripped(tmp_path):
    """熔断期间"不再尝试"必须是**不调用**，所以数调用次数。"""
    breaker = ConsolidationBreaker(tmp_path / "data")
    calls = {"n": 0}

    def always_fails(pairs, *, limit=MEMORY_MAX_CHARS):
        calls["n"] += 1
        raise RuntimeError("整理失败")

    for _ in range(3):
        result = await consolidate_guarded(
            breaker, [("k", "v")], turn="t", merge=always_fails
        )
        assert result.error == "整理失败"
    assert calls["n"] == 3

    # 第 4、5 次：熔断已打开，worker 一次都不该被调用
    for _ in range(2):
        result = await consolidate_guarded(breaker, [("k", "v")], turn="t", merge=always_fails)
        assert "熔断" in result.skipped
    assert calls["n"] == 3

    # 下一轮恢复
    await consolidate_guarded(breaker, [("k", "v")], turn="t2", merge=always_fails)
    assert calls["n"] == 4


async def test_guarded_consolidation_resets_after_success(tmp_path):
    breaker = ConsolidationBreaker(tmp_path / "data")
    breaker.record_failure("t")
    breaker.record_failure("t")
    result = await consolidate_guarded(breaker, [("k", "v")], turn="t")
    assert result.skipped == ""
    assert breaker.state("t")["failures"] == 0


# ── consolidate ──────────────────────────────────────────────────────────────


def test_consolidate_dedupes_same_key():
    result = consolidate([("k", "旧的"), ("k", "新的")])
    assert result.kept == [("k", "新的")]
    assert result.merged == 1
    assert result.dropped == 0


def test_consolidate_leaves_roomy_memory_alone():
    """有空间时不动数据：整理不该悄悄改 key。"""
    pairs = [("项目/a", "一"), ("项目/b", "二")]
    result = consolidate(pairs)
    assert result.kept == pairs
    assert result.merged == 0 and result.dropped == 0


def test_consolidate_merges_same_prefix_before_dropping():
    pairs = [(f"项目/条目{index}", "长" * 300) for index in range(6)]
    result = consolidate(pairs, limit=1000)
    assert result.merged == 5  # 6 条合成 1 条
    assert result.dropped == 0  # 合并就够，不必丢
    assert len(result.kept) == 1
    assert result.used <= 1000


def test_consolidate_drops_oldest_when_nothing_can_merge():
    pairs = [(f"独立键{index}", "长" * 300) for index in range(6)]
    result = consolidate(pairs, limit=1000)
    assert result.dropped >= 1
    assert result.merged == 0  # 扁平 key 没有同前缀可合
    # 丢的是**最旧**的（列表头部）
    assert all(key != "独立键0" for key, _ in result.kept)
    assert result.used <= 1000


def test_consolidate_empty_input():
    result = consolidate([])
    assert result.kept == [] and result.dropped == 0 and result.used == 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def _cli_env(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    return settings


def test_cli_memory_batch_and_breaker(tmp_path, capsys):
    import app.cli.commands.memory_extra  # noqa: F401 —— 接线
    from app.cli.main import main

    settings = _cli_env(tmp_path)
    data_dir = str(settings.data_dir)
    ops = json.dumps([{"op": "add", "key": "偏好/语言", "value": "中文"}])

    # dry-run 不落盘
    assert main(["--data-dir", data_dir, "memory", "batch", "--ops", ops, "--dry-run"]) == 0
    assert "dry-run" in capsys.readouterr().out
    assert main(["--data-dir", data_dir, "memory", "graph"]) == 0
    assert "0 个节点" in capsys.readouterr().out

    # 真写
    assert main(["--data-dir", data_dir, "memory", "batch", "--ops", ops]) == 0
    assert "已应用 1 条操作" in capsys.readouterr().out
    assert main(["--data-dir", data_dir, "memory", "graph"]) == 0
    assert "偏好/语言" in capsys.readouterr().out

    # 非法批处理：返回码 1，且错误里说清第几条
    bad = json.dumps([{"op": "add", "key": "偏好/语言", "value": "撞了"}])
    assert main(["--data-dir", data_dir, "memory", "batch", "--ops", bad]) == 1

    assert main(["--data-dir", data_dir, "memory", "breaker"]) == 0
    assert "熔断：未打开" in capsys.readouterr().out
    assert main(["--data-dir", data_dir, "memory", "breaker", "--reset"]) == 0
    assert "已复位" in capsys.readouterr().out


def test_cli_memory_consolidate_dry_run(tmp_path, capsys):
    import app.cli.commands.memory_extra  # noqa: F401
    from app.cli.main import main

    settings = _cli_env(tmp_path)
    assert main(["--data-dir", str(settings.data_dir), "memory", "consolidate", "--dry-run"]) == 0
    assert "保留 0 条" in capsys.readouterr().out
