"""`app/compaction.py` + `nodes` 提前压缩的契约测试（MATRIX 10.4 / 2.7）。

## 这里为什么不起模型、不起数据库

压缩是**纯算术 + 一次可选摘要**。算术部分（阈值、分组、计划）全部是纯函数，
摘要那一步用注入的假实现替掉（`Nodes(summarizer=...)`）——于是这一层能只测
"判得对不对、配对有没有破、失败有没有退回截断"，不必为一条规则付一次网络往返。

## 三条不能破的口径

1. **正好 50% 就压**（`>=`，不是 `>`）；
2. **系统提示一字不动**（冻结前缀：提示缓存 + 任务内自洽）；
3. **`tool_calls` 与 `ToolMessage` 的配对永不拆开**（拆了就是供应商 400）。
"""

from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from app import compaction
from app import errors_retry as retry
from app.graph.nodes import Nodes
from app.heartbeat import Heartbeat
from app.policies import estimate_tokens as policies_estimate_tokens
from app.session_plane import (
    COMPACTION_TARGET_RATIO,
    COMPACTION_THRESHOLD,
    describe,
)

# ── 夹具 ────────────────────────────────────────────────────────────────────


class _Store:
    """只记事件的最小 store：`think` 只用 `get` 与 `event`。"""

    def __init__(self, task) -> None:
        self.task = task
        self.events: list[dict] = []

    async def get(self, model, key):
        return self.task

    async def event(self, task_id, kind, **payload):
        self.events.append({"task_id": task_id, "kind": kind, **payload})

    def kinds(self) -> list[str]:
        return [item["kind"] for item in self.events]

    def find(self, kind: str) -> dict:
        return next(item for item in self.events if item["kind"] == kind)


class _Registry:
    browser = None

    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def specs(self) -> list:
        return []


class _Policies:
    class _Exposer:
        def expose(self, specs):
            return list(specs)

    class _Planner:
        def derive(self, goal, refs):
            return {"source": "tool_trace", "steps": []}

    class _Results:
        def project(self, name, result):
            return result

    exposer = _Exposer()
    planner = _Planner()
    tool_results = _Results()


class _CountingModel:
    """记录每次调用收到的消息；正文超过 `limit_chars` 就抛 `context_overflow`。"""

    def __init__(self, *, limit_chars: int | None = None, answer: str = "好的，继续。") -> None:
        self.calls: list[list] = []
        self.limit_chars = limit_chars
        self.answer = answer

    def bind_tools(self, *args, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.calls.append(list(messages))
        size = sum(len(str(getattr(message, "content", ""))) for message in messages)
        if self.limit_chars is not None and size > self.limit_chars:
            raise RuntimeError("This model's maximum context length is 128000 tokens")
        return AIMessage(content=self.answer)

    def sizes(self) -> list[int]:
        return [
            sum(len(str(getattr(message, "content", ""))) for message in call)
            for call in self.calls
        ]


class _Runner:
    """`Nodes` 需要的最小 runner：模型、store、policies、registry、partial。"""

    def __init__(self, model, *, window_tokens: int = 20_000) -> None:
        self.task = SimpleNamespace(id="t1", conversation_id="c1", goal="分析日志")
        self.store = _Store(self.task)
        self.model = model
        self.policies = _Policies()
        self.registry = _Registry()
        self.heartbeat = Heartbeat()
        self.partial: dict[str, str] = {}
        self._token_usage: dict[str, dict] = {}
        self.settings = SimpleNamespace(context_length=window_tokens)

    def get_model(self, reasoning_effort=None):
        # **签名要与真实 AgentRunner.get_model 一致**（MATRIX 4.4 之后它多了一个
        # 可选参数）。假件签名不准会让「产品真的传了参数」变成测试报错——
        # 那不是产品的错，是假件过时了。
        self.reasoning_effort_seen = reasoning_effort
        return self.model

    def is_interrupted(self, thread_id: str) -> bool:
        return False


def _rounds(count: int, *, chars: int = 5_000, marker: str = "ROUND") -> list:
    """造 `count` 轮"assistant 带 tool_calls + tool 结果"，正文带可检索的标记。"""
    history: list = [HumanMessage(content="目标：分析日志")]
    for index in range(count):
        history.append(
            AIMessage(
                content=f"第 {index} 步",
                tool_calls=[
                    {
                        "name": "run_command",
                        "args": {"command": f"cat log{index}"},
                        "id": f"call-{index}",
                    }
                ],
            )
        )
        history.append(
            ToolMessage(
                content=f"{marker}{index}:" + "日志" * (chars // 2), tool_call_id=f"call-{index}"
            )
        )
    return history


def _state(history: list, prefix: str = "SYS-PREFIX") -> dict:
    return {
        "task_id": "t1",
        "messages": [SystemMessage(content=prefix), *history],
        "context_meta": {"system_prefix": prefix},
        "refs": {},
        "actions": 0,
        "calls": [],
        "answer": {},
        "waiting": {},
    }


def _fake_summary(text: str = "摘" * 250):
    """注入的假摘要实现：记下被要求压缩的原文，返回一段够长的摘要。"""
    seen: list[list] = []

    def summarize(messages, prior=""):
        seen.append(list(messages))
        return text

    summarize.seen = seen  # type: ignore[attr-defined]
    return summarize


# ── 1. 阈值：0.50 / 0.20（与 session_plane 同一组数）────────────────────────


def test_should_compact_triggers_exactly_at_half():
    """**正好 50% 就压**：到一半已经"再塞一个结果就可能溢出"，压是廉价操作。"""
    assert compaction.should_compact(500, 1000) is True
    assert compaction.should_compact(501, 1000) is True
    assert compaction.should_compact(499, 1000) is False
    assert compaction.should_compact(1, 2) is True
    assert compaction.should_compact(0, 1000) is False


def test_should_compact_refuses_to_guess_without_a_window():
    """窗口未知（0 / 负数 / 坏值）不触发：算不出比例就不该假装算得出。"""
    assert compaction.should_compact(10_000, 0) is False
    assert compaction.should_compact(10_000, -5) is False
    assert compaction.should_compact(10_000, "坏值") is False


def test_target_is_twenty_percent():
    assert compaction.target_tokens(1000) == 200
    assert compaction.target_tokens(128_000) == 25_600
    assert compaction.target_tokens(0) == 0
    assert compaction.target_tokens("坏值") == 0


def test_thresholds_match_session_plane_and_policies():
    """三处口径必须一致：本模块 / session_plane / policies.estimate_tokens。"""
    assert compaction.COMPACT_THRESHOLD == COMPACTION_THRESHOLD == 0.50
    assert compaction.COMPACT_TARGET_RATIO == COMPACTION_TARGET_RATIO == 0.20
    for sample in ("", "hello world", "中文混排 with english", "日志正文" * 100):
        assert compaction.estimate_tokens(sample) == policies_estimate_tokens(sample)


def test_prefixes_are_the_hermes_strings():
    """前缀是跨模块契约：`session_search` 靠它剔除压缩载荷。"""
    assert compaction.COMPACTION_PREFIX == "[CONTEXT COMPACTION"
    assert compaction.SUMMARY_PREFIX == "[CONTEXT SUMMARY]:"


# ── 2. plan 是纯函数：不改输入、不拆配对 ────────────────────────────────────


def test_plan_leaves_the_input_untouched():
    history = _rounds(3)
    snapshot = list(history)
    plan = compaction.plan(history, window=20_000)
    assert list(history) == snapshot, "plan 不许改传进来的列表"
    assert len(history) == len(snapshot)
    assert plan.needed is True


def test_plan_is_idle_below_the_threshold():
    history = _rounds(1, chars=100)
    plan = compaction.plan(history, window=128_000)
    assert plan.needed is False
    assert plan.kept == tuple(range(len(history)))
    assert plan.dropped == ()
    assert plan.summarized == ()
    assert plan.projected_tokens == plan.used_tokens


def test_plan_never_splits_a_tool_call_from_its_result():
    """核心不变量：丢掉的下标里，每个 `tool_calls` 都带齐它的 `tool` 结果。"""
    history = _rounds(6)
    plan = compaction.plan(history, window=20_000)
    assert plan.dropped, "这么长的历史应该要丢东西"
    dropped = set(plan.dropped)
    kept = set(plan.kept)
    for index, message in enumerate(history):
        calls = getattr(message, "tool_calls", None) or []
        if not calls:
            continue
        results = {
            other_index
            for other_index, other in enumerate(history)
            if str(getattr(other, "type", "")) == "tool"
            and other.tool_call_id in {call["id"] for call in calls}
        }
        assert results <= dropped or results <= kept, (
            f"第 {index} 条的 tool_calls 与它的结果被拆到了两边：{results & dropped} / {results & kept}"
        )


def test_plan_always_keeps_the_last_round():
    """最后一组是"当前正在做的事"，丢了它模型就不知道自己刚才在干嘛。"""
    history = _rounds(5)
    plan = compaction.plan(history, window=20_000)
    assert len(history) - 1 in plan.kept
    assert plan.dropped[0] == 0, "从最老的开始丢"


def test_plan_keeps_the_system_prompt_and_counts_carried_tokens():
    system = SystemMessage(content="S" * 400)
    history = [system, *_rounds(4)]
    plan = compaction.plan(history, window=20_000)
    assert 0 in plan.kept, "系统提示恒保留"
    # carried_tokens（系统提示 + 已有摘要）计入"压完还剩多少"
    carried = compaction.plan(
        [item for item in history if item.type != "system"],
        window=20_000,
        carried_tokens=compaction.tokens_for([system]),
    )
    assert carried.projected_tokens == plan.projected_tokens


def test_plan_admits_it_cannot_compress_a_single_round():
    """只有一组时如实报"压不动"，而不是假装压过（丢掉它等于丢掉当前步骤）。"""
    history = [
        AIMessage(
            content="一步",
            tool_calls=[{"name": "run_command", "args": {"command": "ls"}, "id": "c1"}],
        ),
        ToolMessage(content="日志" * 10_000, tool_call_id="c1"),
    ]
    plan = compaction.plan(history, window=20_000)
    assert plan.needed is True
    assert plan.groups == 1
    assert plan.summarized == ()
    assert plan.kept == (0, 1)
    assert "压不动" in plan.reason


def test_plan_reports_what_it_drops_and_what_it_keeps():
    history = _rounds(4)
    plan = compaction.plan(history, window=20_000)
    payload = plan.as_dict()
    assert payload["needed"] is True
    assert set(payload["kept"]).isdisjoint(payload["dropped"])
    assert sorted(payload["kept"] + payload["dropped"]) == list(range(len(history)))
    assert payload["projected_tokens"] < payload["used_tokens"]
    assert payload["used_ratio"] > compaction.COMPACT_THRESHOLD


def test_group_messages_pairs_each_assistant_with_its_results():
    history = _rounds(2)
    groups = compaction.group_messages(history)
    assert [len(group) for group in groups] == [1, 2, 2], groups
    flat = [index for group in groups for index in group]
    assert flat == list(range(len(history)))


# ── 3. 摘要产物：前缀 + 合并 ────────────────────────────────────────────────


def test_summary_message_carries_both_prefixes():
    text = compaction.summary_message("摘要正文", folded=7, window=1000, target=200)
    assert text.startswith("[CONTEXT COMPACTION")
    assert "[CONTEXT SUMMARY]:" in text
    assert text.endswith("摘要正文")
    assert compaction.is_summary_payload(text) is True
    assert compaction.is_summary_payload("普通正文") is False


def test_summary_prompt_merges_the_prior_summary():
    """已有摘要必须一并交给模型合并，否则每压一次就丢一段更早的历史。"""
    prompt = compaction.summary_prompt(_rounds(1), prior="早先的摘要：用户要看日志")
    assert "早先的摘要" in prompt
    assert "合并成**一份**摘要" in prompt
    assert "ROUND0" in prompt, "待压缩的正文要带进请求"


def test_summary_prompt_caps_each_message():
    """摘要请求自己也会溢出：单条正文必须截断。"""
    huge = [ToolMessage(content="X" * 50_000, tool_call_id="c1")]
    prompt = compaction.summary_prompt(huge)
    assert len(prompt) < 10_000
    assert "此处省略" in prompt


def test_usable_summary_rejects_noise():
    assert compaction.usable_summary("") is False
    assert compaction.usable_summary("好的") is False
    assert compaction.usable_summary("摘" * 200) is True


# ── 4. nodes：提前判定接上了（describe 的验收判据）──────────────────────────


def test_describe_reports_the_proactive_trigger_as_matching():
    """验收判据：`nodes_py.trigger_matches` 必须是 True（读源码核出来的）。"""
    policy = describe(128_000)
    assert policy["nodes_py"]["trigger_matches"] is True
    assert policy["nodes_py"]["threshold_ratio"] == 0.50
    assert "proactive" in policy["nodes_py"]["trigger"]


# ── 5. nodes.think：提前压完再请求，不再白跑一次溢出往返 ─────────────────────


async def test_proactive_compaction_avoids_the_overflow_round_trip():
    """假模型"超过 N 字符就报溢出"：提前压缩之后**只调一次**，不再重试。"""
    model = _CountingModel(limit_chars=12_000)
    runner = _Runner(model)
    nodes = Nodes(runner, summarizer=_fake_summary())
    history = _rounds(3)

    updates = await nodes.think(_state(history))

    assert len(model.calls) == 1, "提前压过了，不该再有一次溢出 + 重试"
    assert "model_retry" not in runner.store.kinds()
    compacted = runner.store.find("context_compacted")
    assert compacted["reason"] == "proactive"
    assert compacted["dropped"] == 5
    sent = "".join(str(getattr(m, "content", "")) for m in model.calls[0])
    assert "[CONTEXT COMPACTION" in sent and "[CONTEXT SUMMARY]:" in sent
    assert "ROUND0" not in sent, "被折叠的原文不该再进上下文"
    assert "ROUND2" in sent, "最后一组（当前这一步）必须留着"
    assert updates["messages"][0].type == "ai"
    # 折叠进度写回 state，下一轮不必重新摘一遍
    assert updates["context_meta"]["compacted_messages"] == 5
    assert updates["context_meta"]["compaction_summary"]


async def test_proactive_compaction_keeps_the_frozen_prefix_byte_identical():
    model = _CountingModel()
    runner = _Runner(model)
    nodes = Nodes(runner, summarizer=_fake_summary())
    await nodes.think(_state(_rounds(3), prefix="SYS-PREFIX-逐字节不变"))
    system = model.calls[0][0]
    assert system.type == "system"
    assert system.content == "SYS-PREFIX-逐字节不变"


async def test_proactive_compaction_only_summarizes_the_dropped_rounds():
    """交给摘要的只能是"要丢的那几组"，不能把当前这一步也摘进去。"""
    model = _CountingModel()
    runner = _Runner(model)
    summarizer = _fake_summary()
    nodes = Nodes(runner, summarizer=summarizer)
    await nodes.think(_state(_rounds(3)))
    assert summarizer.seen, "摘要实现应该被调用过"
    summarized = "".join(str(getattr(m, "content", "")) for m in summarizer.seen[0])
    assert "ROUND0" in summarized and "ROUND1" in summarized
    assert "ROUND2" not in summarized


async def test_proactive_compaction_keeps_tool_pairs_legal():
    """压完之后发出去的消息里，每个 tool_calls 都必须有对应的 ToolMessage。"""
    model = _CountingModel()
    runner = _Runner(model)
    nodes = Nodes(runner, summarizer=_fake_summary())
    await nodes.think(_state(_rounds(4)))
    assert retry.missing_tool_results(model.calls[0]) == []


async def test_failed_summary_falls_back_to_deterministic_truncation():
    """摘要失败**不能让任务失败**：退回确定性截断，任务照常往下走。"""
    model = _CountingModel()
    runner = _Runner(model)

    def broken(messages, prior=""):
        raise RuntimeError("摘要模型挂了")

    nodes = Nodes(runner, summarizer=broken)
    await nodes.think(_state(_rounds(3)))

    assert len(model.calls) == 1
    compacted = runner.store.find("context_compacted")
    assert compacted["reason"] == "summary_failed"
    sent = "".join(str(getattr(m, "content", "")) for m in model.calls[0])
    assert "[CONTEXT SUMMARY]:" not in sent, "摘要没生成就不该出现摘要载荷"
    # 确定性截断留下了痕迹：正文被打上截断标记，或整组被丢掉
    assert "此处已截断" in sent or "ROUND0" not in sent
    assert retry.missing_tool_results(model.calls[0]) == []


async def test_empty_summary_also_falls_back():
    """模型回一句"好的"也算失败：短于 MIN_SUMMARY_CHARS 的摘要不许冒充摘要。"""
    model = _CountingModel()
    runner = _Runner(model)
    nodes = Nodes(runner, summarizer=lambda messages, prior="": "好的")
    await nodes.think(_state(_rounds(3)))
    assert runner.store.find("context_compacted")["reason"] == "summary_failed"


async def test_summary_call_restores_the_partial_answer_state():
    """摘要那次流式调用不能污染 `runner.partial`（它是"已吐正文"的判据）。

    不还原的后果很具体：主调用一旦失败，`has_partial` 为真 → 本该重试的失败被
    直接判死（`errors_retry.decide` 的第一条规则）。
    """
    model = _CountingModel(answer="这是一段摘要。" * 60)
    runner = _Runner(model)
    runner.partial["t1"] = "用户已经看到的半截正文"
    nodes = Nodes(runner)  # 不注入 summarizer：走真实模型路径
    text = await nodes.summarize([HumanMessage(content="要被压的几轮")], runner.task)
    assert text.startswith("这是一段摘要。")
    assert runner.partial["t1"] == "用户已经看到的半截正文"


async def test_no_compaction_below_the_threshold():
    model = _CountingModel()
    runner = _Runner(model)
    nodes = Nodes(runner, summarizer=_fake_summary())
    await nodes.think(_state(_rounds(1, chars=200)))
    assert "context_compacted" not in runner.store.kinds()


async def test_repeated_summary_failures_stop_burning_model_calls():
    """摘要连败几次后不再重试：模型挂着的时候"每轮都试一次"等于把失败成本翻倍。

    上限是 `COMPACT_SUMMARY_ATTEMPTS`（2）。第 3 轮起直接走确定性截断，并留一条
    `summary_skipped` 事件——省下的调用是看得见的，不是静默跳过。
    """
    model = _CountingModel()
    runner = _Runner(model)
    attempts = {"count": 0}

    def broken(messages, prior=""):
        attempts["count"] += 1
        raise RuntimeError("摘要模型一直挂着")

    nodes = Nodes(runner, summarizer=broken)
    state = _state(_rounds(3))
    for _ in range(3):
        updates = await nodes.think(state)
        state = {**state, "context_meta": updates.get("context_meta", state["context_meta"])}

    assert attempts["count"] == 2, f"最多试 2 次，实际 {attempts['count']}"
    assert len(model.calls) == 3, "主调用每轮照常一次（压缩失败不拦任务）"
    reasons = [
        item["reason"]
        for item in runner.store.events
        if item["kind"] == "context_compacted"
    ]
    assert reasons == ["summary_failed", "summary_failed", "summary_skipped"], reasons


async def test_successful_summary_resets_the_failure_counter():
    """失败过之后模型恢复了：计数归零，下一次还能走摘要。"""
    model = _CountingModel()
    runner = _Runner(model)
    calls = {"count": 0}

    def flaky(messages, prior=""):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("第一次挂了")
        return "摘" * 250

    nodes = Nodes(runner, summarizer=flaky)
    state = _state(_rounds(3))
    first = await nodes.think(state)
    assert first["context_meta"]["compaction_summary_failures"] == 1
    state = {**state, "context_meta": first["context_meta"]}
    second = await nodes.think(state)
    assert second["context_meta"]["compaction_summary_failures"] == 0
    assert second["context_meta"]["compaction_summary"]


# ── 6. 被动路径（供应商已经报溢出）也走同一套压缩 ────────────────────────────


async def test_reactive_overflow_uses_the_summary_when_it_works():
    model = _CountingModel(limit_chars=12_000)
    runner = _Runner(model)
    nodes = Nodes(runner, summarizer=_fake_summary())
    history = _rounds(3)
    answer = await nodes.ask_model(
        model, history, runner.task, {"conversation_id": "c1", "message_id": "m1"}
    )
    assert answer.text == "好的，继续。"
    assert len(model.calls) == 2, "一次溢出 + 一次压缩后重试"
    event = runner.store.find("context_compacted")
    assert event["path"] == "summary"
    sent = "".join(str(getattr(m, "content", "")) for m in model.calls[1])
    assert "[CONTEXT SUMMARY]:" in sent
    assert retry.missing_tool_results(model.calls[1]) == []


async def test_reactive_overflow_falls_back_when_summary_fails():
    model = _CountingModel(limit_chars=12_000)
    runner = _Runner(model)
    nodes = Nodes(runner, summarizer=lambda messages, prior="": "")
    await nodes.ask_model(
        model, _rounds(3), runner.task, {"conversation_id": "c1", "message_id": "m1"}
    )
    event = runner.store.find("context_compacted")
    assert event["path"] == "truncate"
    assert event["after_chars"] < event["before_chars"]


async def test_reactive_overflow_without_a_model_degrades_to_truncation():
    """没有 `get_model`（纯替身）时也必须能压：这条路以前就是这样，不能退步。"""

    class _NoModelRunner(_Runner):
        def get_model(self, reasoning_effort=None):  # type: ignore[override]
            raise AssertionError("没有模型时不该被调用")

    model = _CountingModel(limit_chars=12_000)
    runner = _NoModelRunner(model)
    nodes = Nodes(runner)
    await nodes.ask_model(
        model, _rounds(3), runner.task, {"conversation_id": "c1", "message_id": "m1"}
    )
    assert runner.store.find("context_compacted")["path"] == "truncate"


async def test_summary_request_never_recurses_into_compaction():
    """摘要请求自己溢出时按失败处理（`allow_compact=False`），不能递归压缩。"""

    class _AlwaysOverflow(_CountingModel):
        async def ainvoke(self, messages):
            self.calls.append(list(messages))
            raise RuntimeError("maximum context length exceeded")

    model = _AlwaysOverflow()
    runner = _Runner(model)
    nodes = Nodes(runner)
    with_messages = await nodes.summarize([HumanMessage(content="x" * 100)], runner.task)
    assert with_messages == "", "失败必须返回空串（调用方据此退回截断）"
    assert len(model.calls) == 1, "只试一次，不许递归"


def test_compaction_module_does_not_drag_in_langgraph():
    """`app/compaction.py` 不能把 langgraph / langchain 拉起来。

    `app/session_plane.py` 要能被**纯文件命令**使用（`orbit plane ...`），而
    `app.graph.nodes` 一 import 就会拉起整条图链——所以判据必须在一个**干净的
    解释器**里验，不能在已经 import 过 langchain 的本进程里验。
    """
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import sys, app.compaction;"
        "print('langgraph' in sys.modules or 'langchain_core' in sys.modules)"
    )
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=180,
        cwd=str(Path(__file__).resolve().parents[1]),
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "False", out.stdout
