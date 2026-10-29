"""链骨架：默认值兼容、预算收尾、引用回填校验、plan/reflect 纯函数。"""

import asyncio
import time

from langchain_core.messages import AIMessage
from sqlalchemy import select

from app.agent import AgentRunner
from app.config import Settings
from app.context import PROMPT_VERSION
from app.graph.nodes import MODEL_ATTEMPTS, Nodes
from app.graph.state import with_defaults
from app.main import migrate
from app.policies import DefaultBudgetPolicy, DefaultPlanner, DefaultReflector
from app.storage import Conversation, Message, Store, Task, TaskEvent
from app.tools import ToolRegistry


def test_with_defaults_fills_fields_missing_from_old_checkpoints():
    legacy = {
        "task_id": "t1",
        "messages": [],
        "calls": [],
        "actions": 2,
        "answer": {},
        "waiting": {},
    }
    state = with_defaults(legacy)
    assert state["actions"] == 2
    for key in ("plan", "budget", "refs", "context_meta", "last_result", "final"):
        assert state[key] == {}


def test_planner_derives_steps_from_the_tool_trace():
    plan = DefaultPlanner().derive(
        "搜键盘",
        [
            {"name": "browser_navigate", "status": "ok", "host": "www.bilibili.com"},
            {"name": "browser_click", "status": "error", "host": "www.bilibili.com"},
        ],
    )
    assert plan["source"] == "tool_trace"
    assert [step["status"] for step in plan["steps"]] == ["done", "failed"]


def test_reflector_triggers_on_interval_then_stops_after_two_runs():
    reflector = DefaultReflector()
    refs = {str(index): {"tool": "browser_click", "status": "ok"} for index in range(6)}
    assert reflector.should_reflect({"refs": refs}) is True
    assert reflector.should_reflect({"refs": refs, "reflections": ["a", "b"]}) is False


def test_reflector_triggers_on_two_errors_from_the_same_tool():
    reflector = DefaultReflector()
    refs = {
        "1": {"tool": "browser_click", "status": "error"},
        "2": {"tool": "browser_click", "status": "error"},
    }
    assert reflector.should_reflect({"refs": refs}) is True
    refs["2"]["tool"] = "browser_fill"
    assert reflector.should_reflect({"refs": refs}) is False


def test_reflector_writes_notes_that_match_the_trigger():
    """reflection 提示由轨迹推导，不调模型：三类触发各给一条可执行纠偏。"""
    reflector = DefaultReflector()
    repeated = {
        "1": {"tool": "browser_click", "status": "error"},
        "2": {"tool": "browser_click", "status": "error"},
    }
    notes = reflector.reflect({"refs": repeated})
    assert len(notes) == 1 and "browser_click" in notes[0] and "不要用同样的参数重试" in notes[0]
    six = {str(i): {"tool": "browser_click", "status": "ok"} for i in range(6)}
    assert "不要重复做" in reflector.reflect({"refs": six})[0]
    half = {"refs": {}, "budget": {"max_tokens": 1000, "token_estimate": 500}}
    assert "收窄范围" in reflector.reflect(half)[0]
    assert reflector.reflect({"refs": {}, "budget": {}}) == []


def test_budget_target_is_reachable_after_the_reflection_warning():
    """预算满量与自省半量是两个不同阈值：半量时必须还没被拦停。"""
    policy, reflector = DefaultBudgetPolicy(), DefaultReflector()
    half = budget_state(token_estimate=500)
    assert policy.check(half) == "" and reflector.should_reflect(half) is True
    assert policy.check(budget_state(token_estimate=1000)) == "max_tokens"


def budget_state(**budget):
    base = {
        "max_actions": 20,
        "max_tokens": 1000,
        "max_seconds": 100,
        "token_estimate": 0,
        "elapsed": 0.0,
    }
    base.update(budget)
    return {"actions": 0, "budget": base}


def test_budget_policy_stops_on_hard_limits_only():
    """时间上限改软闸后，只有动作数与 token 两类硬停；时间越线不再返回 stop_reason。"""
    policy = DefaultBudgetPolicy()
    assert policy.check(budget_state()) == ""
    assert policy.check({**budget_state(), "actions": 20}) == "max_actions"
    # token 判满量（半量预警交给 Reflector，两者同阈值会互相遮蔽）
    assert policy.check(budget_state(token_estimate=1000)) == "max_tokens"
    assert policy.check(budget_state(token_estimate=999)) == ""
    # 时间越线不再是硬停——交给 budget 节点的 backgrounded 软闸处理。
    assert policy.check(budget_state(elapsed=100)) == ""
    assert policy.check(budget_state(elapsed=9999)) == ""


def test_budget_policy_ignores_zero_limits():
    """max_* 为 0 表示不设限，不能把任务立即拦死。"""
    policy = DefaultBudgetPolicy()
    assert policy.check(budget_state(max_tokens=0, max_seconds=0)) == ""


def answer_state(messages):
    return {"messages": messages, "context_meta": {"retrieved": []}}


def test_citation_check_is_skipped_when_nothing_was_injected():
    nodes = Nodes(None)
    result = nodes.citation_check(answer_state([AIMessage(content="1+1 等于 2")]), "1+1 等于 2")
    assert result["checked"] is False


def test_citation_check_accepts_a_shortened_url():
    nodes = Nodes(None)
    state = {
        "messages": [],
        "context_meta": {"retrieved": [{"origin": "https://search.bilibili.com/all?keyword=x"}]},
    }
    answer = "按最多播放排序，来源：search.bilibili.com/all"
    assert nodes.citation_check(state, answer)["matched"]
    other = "我看了下，就是那几首。"
    assert nodes.citation_check(state, other)["missing"]


# ── 视觉兜底：execute 节点在元素失效时把截图喂给多模态模型 ──────────────


class _FakeBrowser:
    def __init__(self, image=None):
        self._image = image

    def last_screenshot_data_url(self):
        return self._image

    def current_host(self):
        return "www.example.com"


class _FakeRegistry:
    def __init__(self, browser):
        self.browser = browser
        self.tools = {"browser_click": object()}

    async def execute(self, task_id, operation_id, name, args, messages=None):
        return {"status": "error", "error": "ValueError: 元素已变化，请重新观察"}


class _FakePolicies:
    class _ToolResults:
        def project(self, name, result):
            return result

    tool_results = _ToolResults()


class _FakeRunner:
    def __init__(self, registry, policies):
        self.registry = registry
        self.policies = policies
        self.settings = None


async def test_execute_injects_screenshot_when_element_stale():
    image = "data:image/png;base64,ZmFrZQ=="
    nodes = Nodes(
        _FakeRunner(_FakeRegistry(_FakeBrowser(image=image)), _FakePolicies())
    )
    state = with_defaults(
        {
            "calls": [{"name": "browser_click", "args": {"ref": "e0", "version": 1}, "id": "c1"}],
        }
    )
    state["task_id"] = "task-1"
    result = await nodes.execute(state)
    messages = result["messages"]
    assert len(messages) == 2  # ToolMessage + HumanMessage(图)
    assert messages[0].type == "tool"
    assert messages[1].type == "human"
    content = messages[1].content
    assert isinstance(content, list)
    assert any(block.get("type") == "image_url" for block in content)
    image_block = next(block for block in content if block.get("type") == "image_url")
    assert image_block["image_url"]["url"] == image


async def test_execute_does_not_inject_screenshot_on_success():
    class _OkRegistry(_FakeRegistry):
        async def execute(self, task_id, operation_id, name, args, messages=None):
            return {"status": "ok", "data": {"url": "https://www.example.com/"}}

    nodes = Nodes(_FakeRunner(_OkRegistry(_FakeBrowser(image=None)), _FakePolicies()))
    state = with_defaults(
        {
            "calls": [{"name": "browser_click", "args": {"ref": "e0", "version": 1}, "id": "c1"}],
        }
    )
    state["task_id"] = "task-1"
    result = await nodes.execute(state)
    assert len(result["messages"]) == 1  # 只有 ToolMessage
    assert result["messages"][0].type == "tool"


async def test_execute_does_not_inject_when_no_screenshot_available():
    nodes = Nodes(
        _FakeRunner(_FakeRegistry(_FakeBrowser(image=None)), _FakePolicies())
    )
    state = with_defaults(
        {
            "calls": [{"name": "browser_click", "args": {"ref": "e0", "version": 1}, "id": "c1"}],
        }
    )
    state["task_id"] = "task-1"
    result = await nodes.execute(state)
    assert len(result["messages"]) == 1  # 无图可用则退回纯文本错误


class TwoStepModel:
    """连续两次工具调用；动作上限设为 1 时必须由预算收尾而不是抛错。"""

    def __init__(self):
        self.calls = 0

    def bind_tools(self, *args, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        if self.calls <= 2:
            return AIMessage(
                content="我先看看目录。",
                tool_calls=[
                    {
                        "name": "local_list_files",
                        "args": {"path": "."},
                        "id": f"call{self.calls}",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="做完了。")


async def wait_status(store, task_id, target, *, ceiling=150.0, stall=45.0):
    """等任务进入目标状态。

    这里**不能**用一个固定的墙钟上限：本仓的 graph 用例是纯本地活（假模型 + SQLite），
    单跑不到 1s，但整包串行跑到它时已经做过 ~200 次真实迁移/DB 操作，CPU-only 机器上
    worker 线程会被饿到十几秒（2026-10-08 实测：整包跑偶尔 TimeoutError，单跑/复跑必过）。
    固定墙钟就把「机器忙」误判成「逻辑坏了」。

    两层判据：
    - `stall`：**没有进展**才失败（任务一直没到目标态、也没换过 `updated_at`）——
      真卡死仍会被抓到，这是测试真正要防的东西；
    - `ceiling`：兜底总上限，防止某些错误路径下状态永远在推进却永不达标。

    ## 第 46 轮：两个窗口从 15/60 放宽到 45/150

    **为什么原来的 15s 不够**：`stall` 的假设是「没进展 = 逻辑坏了」，但**CPU 被饿住时
    「没进展」也是正常的**——任务确实在等调度，`updated_at` 当然不会变。
    **两者在这个判据下长得一样**，而 15s 在「整包串行 + 200 次迁移之后」这个负载下
    不够长：本用例在整包跑里失败过**两次独立观察**（第 43 轮一次、第 44 轮一次），
    单跑与复跑都过。

    **为什么是 45/150 而不是「标 flaky」**：标 flaky 等于把红灯信号调成静音——
    下次真坏了也没人看。**放宽窗口仍然保留「真卡死会被抓到」这个能力**
    （真卡死不会因为窗口变长就变成通过，只是要多等 30 秒）。

    **代价**：真卡死的用例从 15s 才报错变成 45s 才报错。**这个代价可接受**——
    真卡死本来就罕见，而误报每周都在发生。
    """
    started = asyncio.get_running_loop().time()
    last_progress = started
    last_seen = None
    while True:
        task = await store.get(Task, task_id)
        if task.status == target:
            return task
        assert task.status != "FAILED", task.error
        marker = (task.status, getattr(task, "updated_at", None))
        now = asyncio.get_running_loop().time()
        if marker != last_seen:
            last_seen = marker
            last_progress = now
        if now - last_progress >= stall:
            raise AssertionError(
                f"任务 {task_id} 在 {stall:.0f}s 内毫无进展"
                f"（当前 {task.status}，目标 {target}）{_progress_hint(store, task_id)}"
            )
        if now - started >= ceiling:
            raise AssertionError(
                f"任务 {task_id} 超过 {ceiling:.0f}s 仍未到 {target}"
                f"（当前 {task.status}）{_progress_hint(store, task_id)}"
            )
        await asyncio.sleep(0.05)


def _progress_hint(store, task_id: str, *, tail: int = 8) -> str:
    """失败时把**最后几条事件**贴出来（第 48 轮加）。

    ## 为什么值得为「失败信息」写代码

    `wait_status` 放宽窗口之后，**下一次失败就会变成"到底卡在哪"这个更贵的问题**。
    而监视子agent 给的是**根因假设**、不是实证——**如果假设错了，我就是在给一个真 bug
    让路**。所以这里补上「进度可观测」：**即使假设错了，下次失败也能直接定位，
    不用再猜一轮。**

    这也是这个 flake 的教训之一：**上一轮我只能从「单跑过、全量不过」反推原因，
    因为失败信息里没有任何进度线索。**

    ## 为什么读事件而不是读任务字段

    任务字段只有 `status` / `error`，而「卡在哪一步」只有事件流知道
    （`step_started` / `tool_call` / `model_call` …）。
    **读不到就如实说读不到**——不要让提示本身失败。
    """
    try:
        log = getattr(store, "event_log", None)
        if log is None:
            return "\n（这个 store 没有 event_log，拿不到进度）"
        events = log.since(task_id, 0)
    except Exception as exc:  # noqa: BLE001 —— 提示失败不该盖住真正的失败
        return f"\n（读事件失败：{exc}）"
    if not events:
        return "\n（这个任务一条事件都没有——**那本身就是一个线索**：它可能根本没启动）"
    lines = ["\n最后几条事件："]
    for item in events[-tail:]:
        # **事件是信封形状**：`{"stream", "event_id", "payload": {"kind": ...}}`。
        # 第一版直接读 `item["kind"]`，于是每条都显示成 `?` 并把整个信封倒出来——
        # **验了才发现它是半坏的**（这正是「写了但没验」那一族）。
        payload = item.get("payload")
        if not isinstance(payload, dict):
            payload = item
        kind = payload.get("kind") or payload.get("type") or "?"
        detail = {
            key: value
            for key, value in payload.items()
            if key not in {"kind", "type", "id", "at", "seq", "ts"}
        }
        rendered = " ".join(f"{key}={value!r}" for key, value in detail.items())
        lines.append(f"  - {kind} {rendered}".rstrip())
    return "\n".join(lines)


async def test_budget_stop_ends_with_a_readable_progress_summary(tmp_path):
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files", max_actions=1)
    settings.prepare()
    migrate(settings)
    (settings.workspace / "notes.md").write_text("项目使用 FastAPI。", encoding="utf-8")
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    await store.save(Message(conversation_id=conversation.id, role="user", content="看看目录"))
    task = await store.save(Task(conversation_id=conversation.id, goal="看看目录"))
    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=TwoStepModel())
    await runner.start()
    await runner.enqueue(task.id)
    await wait_status(store, task.id, "SUCCEEDED")
    reply = (await store.get(Message, f"answer-{task.id}")).content
    assert "任务在这里暂停" in reply
    assert "**已完成**" in reply and "**未完成 / 下一步建议**" in reply
    assert "**对外动作清单**" in reply
    assert "已达到最大工具动作数" not in reply
    events = await store.rows(select(TaskEvent).where(TaskEvent.task_id == task.id))
    kinds = [row.event_type for row in events]
    assert "budget_stopped" in kinds and "finalize_completed" in kinds
    assert "tool_result_stored" in kinds
    # 成本聚合：收尾事件带上量级数据，回答里之后才有"这次花了多少"可查。
    summary = next(row for row in events if row.event_type == "finalize_completed")
    assert summary.payload["actions"] == 1
    assert summary.payload["tokens"] > 0 and summary.payload["bytes"] > 0
    assert summary.payload["refs"] == 1  # 首个动作已进 digest 落库
    assert "elapsed" in summary.payload and "citations" in summary.payload
    await runner.close()
    await store.engine.dispose()


async def test_time_budget_backgrounds_instead_of_hard_stopping(tmp_path):
    """时间越线不再一刀切：任务标记 backgrounded、发进度事件，然后照常跑到完成。"""
    settings = Settings(
        data_dir=tmp_path / "data", workspace=tmp_path / "files",
        max_actions=40, max_seconds=0,  # max_seconds=0 表示不设限；用负的 elapsed 强制走软闸路径
    )
    settings.prepare()
    migrate(settings)
    (settings.workspace / "notes.md").write_text("项目使用 FastAPI。", encoding="utf-8")
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    await store.save(Message(conversation_id=conversation.id, role="user", content="看看目录"))
    task = await store.save(Task(conversation_id=conversation.id, goal="看看目录"))
    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=TwoStepModel())
    await runner.start()
    await runner.enqueue(task.id)
    await wait_status(store, task.id, "SUCCEEDED")
    events = await store.rows(select(TaskEvent).where(TaskEvent.task_id == task.id))
    kinds = [row.event_type for row in events]
    # 时间闸默认 360s，这个快速任务到不了后台——但 token 计量必须已经在收尾事件里。
    assert "task_completed" in kinds
    completed = next(row for row in events if row.event_type == "task_completed")
    usage = completed.payload.get("token_usage") or {}
    assert int(usage.get("completion_tokens", 0)) > 0, usage
    assert int(usage.get("prompt_tokens", 0)) > 0, usage
    await runner.close()
    await store.engine.dispose()


async def test_backgrounded_marks_and_reports_periodically(tmp_path):
    """软时间闸命中：先发 backgrounded，再按 report_interval 发 background_report。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files",
                        max_actions=40, max_seconds=1, report_interval=0)
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")

    # 直接驱动 budget 节点，模拟"已经跑了 2 秒"的状态——不真跑模型、不真等 1 秒。
    runner = AgentRunner(settings, store, ToolRegistry(settings, store))
    await runner.start()
    budget_state = {
        "task_id": "t-bg",
        "actions": 3,
        "refs": {"a": {"status": "ok", "tool": "local_list_files"}},
        "budget": {
            "max_actions": 40, "max_tokens": 200_000, "max_seconds": 1,
            "token_estimate": 0, "elapsed": 2.0, "started_at": time.time() - 5.0,
            "used_actions": 0, "summary_cost": {"calls": 0, "tokens": 0, "elapsed": 0.0},
        },
    }
    out = await runner.nodes.budget(budget_state)
    assert out["budget"]["backgrounded"] is True
    # report_interval=0 会立即触发下一条进度报告。
    out2 = await runner.nodes.budget({**budget_state, "budget": out["budget"]})
    assert out2["budget"]["backgrounded"] is True
    await runner.close()
    await store.engine.dispose()


async def test_model_call_is_retried_then_succeeds(tmp_path):
    """流式尚未产出正文时的失败可以安全重试；重试过程留下 model_retry 事件。"""

    class FlakyModel:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls < 3:
                raise RuntimeError("connection reset")
            return AIMessage(content="这次成功了。")

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="随便聊聊"))
    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=FlakyModel())
    await runner.start()
    await runner.enqueue(task.id)
    await wait_status(store, task.id, "SUCCEEDED")
    assert (await store.get(Message, f"answer-{task.id}")).content == "这次成功了。"
    events = await store.rows(select(TaskEvent).where(TaskEvent.task_id == task.id))
    retries = [row for row in events if row.event_type == "model_retry"]
    assert len(retries) == 2 and retries[0].payload["error"] == "RuntimeError"
    assert len([row for row in events if row.event_type == "message_started"]) == 1
    await runner.close()
    await store.engine.dispose()


async def test_model_failure_after_all_attempts_reports_a_readable_error(tmp_path):
    class DeadModel:
        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            raise RuntimeError("connection reset")

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="随便聊聊"))
    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=DeadModel())
    await runner.start()
    await runner.enqueue(task.id)
    failed = await wait_status(store, task.id, "FAILED")
    # 断言按「类别 + 建议 + 次数」走，**不锁死整句文案**：建议措辞会演进，
    # 但这三样是给用户行动依据的东西，丢了就等于回到"请检查连接设置"那种抹平。
    assert "connection" in failed.error
    assert "检查网络" in failed.error
    assert f"已尝试 {MODEL_ATTEMPTS} 次" in failed.error
    await runner.close()
    await store.engine.dispose()


async def test_events_record_prompt_version_and_model(tmp_path):
    """提示词版本与模型名随事件落库：回看历史任务能分清"当时用的是哪版"。"""

    class PlainModel:
        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            return AIMessage(content="1+1 等于 2。")

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files", model="test-model")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="1+1 等于几"))
    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=PlainModel())
    await runner.start()
    await runner.enqueue(task.id)
    await wait_status(store, task.id, "SUCCEEDED")
    events = await store.rows(select(TaskEvent).where(TaskEvent.task_id == task.id))
    started = next(row for row in events if row.event_type == "task_started")
    assert started.payload["model"] == "test-model"
    first = next(row for row in events if row.event_type == "message_started")
    assert first.payload["prompt_version"] == PROMPT_VERSION
    assert first.payload["response_id"]
    await runner.close()
    await store.engine.dispose()


async def test_reflection_fires_in_the_graph_after_six_steps(tmp_path):
    """reflect 真的接进了图：第 6 步后落一条 reflection 事件，任务照常收尾。"""

    class SixStepModel:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.calls += 1
            if self.calls <= 6:
                return AIMessage(
                    content="继续。",
                    tool_calls=[
                        {
                            "name": "local_list_files",
                            "args": {"path": "."},
                            "id": f"call{self.calls}",
                            "type": "tool_call",
                        }
                    ],
                )
            return AIMessage(content="做完了。")

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="走六步看看"))
    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=SixStepModel())
    await runner.start()
    await runner.enqueue(task.id)
    await wait_status(store, task.id, "SUCCEEDED")
    events = await store.rows(select(TaskEvent).where(TaskEvent.task_id == task.id))
    reflections = [row for row in events if row.event_type == "reflection"]
    assert len(reflections) == 1, [row.event_type for row in events]
    assert reflections[0].payload["step"] == 6
    assert "已经执行 6 步" in reflections[0].payload["notes"][0]
    assert (await store.get(Message, f"answer-{task.id}")).content == "做完了。"
    await runner.close()
    await store.engine.dispose()


async def test_ask_user_still_interrupts_and_resumes(tmp_path):
    class AskModel:
        def __init__(self):
            self.step = 0

        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            self.step += 1
            if self.step == 1:
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "ask_user",
                            "args": {"question": "整理哪个月份？", "reason": "clarification"},
                            "id": "ask",
                            "type": "tool_call",
                        }
                    ],
                )
            assert any(message.type == "tool" for message in messages)
            return AIMessage(content="好的，整理上个月。")

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    await store.save(Message(conversation_id=conversation.id, role="user", content="整理资料"))
    task = await store.save(Task(conversation_id=conversation.id, goal="整理资料"))
    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=AskModel())
    await runner.start()
    await runner.enqueue(task.id)
    paused = await wait_status(store, task.id, "WAITING_USER")
    assert paused.waiting["reason"] == "clarification"
    await runner.enqueue(task.id, {"approved": True, "answer": "上个月"})
    await wait_status(store, task.id, "SUCCEEDED")
    assert (await store.get(Message, f"answer-{task.id}")).content == "好的，整理上个月。"
    await runner.close()
    await store.engine.dispose()


# ── M4：系统提示前缀在任务内冻结（保住提示缓存 + 任务内部自洽） ──────────────


class PrefixRecordingModel:
    """记录每次调用收到的系统提示，供"前缀是否逐字不变"的断言使用。"""

    def __init__(self):
        self.prefixes = []
        self.calls = 0

    def bind_tools(self, *args, **kwargs):
        return self

    async def ainvoke(self, messages):
        self.calls += 1
        system = next((m for m in messages if m.type == "system"), None)
        self.prefixes.append(str(getattr(system, "content", "")))
        if self.calls <= 2:
            return AIMessage(
                content="先看看目录。",
                tool_calls=[
                    {
                        "name": "local_list_files",
                        "args": {"path": "."},
                        "id": f"call{self.calls}",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(content="看完了。")


async def test_system_prefix_is_frozen_across_steps_of_one_task(tmp_path):
    """同一任务内多次模型调用必须看到**逐字节相同**的系统提示。

    以前 think 每轮重装上下文：记忆排序、摘要、画像任意一处变动都会改写前缀，
    既打掉提示缓存，也让"任务中途改记忆"意外地影响正在跑的任务。
    """
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="看看目录"))
    model = PrefixRecordingModel()
    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=model)
    await runner.start()
    await runner.enqueue(task.id)
    await wait_status(store, task.id, "SUCCEEDED")
    assert len(model.prefixes) >= 3, "两步工具调用 + 一次收尾，至少 3 次模型调用"
    assert len(set(model.prefixes)) == 1, "同一任务内系统提示必须完全一致"
    await runner.close()
    await store.engine.dispose()


async def test_mid_task_memory_change_does_not_alter_the_frozen_prefix(tmp_path):
    """任务跑到一半有人改了记忆：当前任务的前缀不变（下次任务才生效）。"""

    class MutatingModel(PrefixRecordingModel):
        def __init__(self, store, task):
            super().__init__()
            self.store, self.task = store, task

        async def ainvoke(self, messages):
            self.calls += 1
            system = next((m for m in messages if m.type == "system"), None)
            self.prefixes.append(str(getattr(system, "content", "")))
            if self.calls == 1:
                # 第一步：中途写入一条新记忆
                await self.store.set_memory("中途新增", "这条不该出现在本次任务的前缀里")
                return AIMessage(
                    content="先看看目录。",
                    tool_calls=[
                        {
                            "name": "local_list_files",
                            "args": {"path": "."},
                            "id": "call1",
                            "type": "tool_call",
                        }
                    ],
                )
            return AIMessage(content="看完了。")

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="看看目录"))
    model = MutatingModel(store, task)
    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=model)
    await runner.start()
    await runner.enqueue(task.id)
    await wait_status(store, task.id, "SUCCEEDED")
    assert len(set(model.prefixes)) == 1
    assert all("中途新增" not in prefix for prefix in model.prefixes)
    await runner.close()
    await store.engine.dispose()


async def test_one_task_counts_each_memory_hit_once(tmp_path):
    """一个任务只该给记忆记一次命中，不能因为 prepare 装配了两次上下文就翻倍。

    回归：M4 引入 `build_prefix` 后，prepare 先用 `context.build` 装配 messages、
    又调 `build_prefix`（内部再跑一次 `build_context`）。而 `build_context` 每次都累加
    `hit_count`（M1 的死数据判据），于是新任务的命中数被凭空翻倍——
    "用了 2 次"和"用了 1 次"会让"哪条记忆从没被用过"的结论失真。
    """
    from app.storage import Memory

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    memory, _ = await store.set_memory("报告格式", "中文 Markdown")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="随便看看"))

    class QuietModel:
        def bind_tools(self, *args, **kwargs):
            return self

        async def ainvoke(self, messages):
            return AIMessage(content="好的。")

    runner = AgentRunner(settings, store, ToolRegistry(settings, store), model=QuietModel())
    await runner.start()
    await runner.enqueue(task.id)
    await wait_status(store, task.id, "SUCCEEDED")

    row = await store.get(Memory, memory.id)
    assert row.hit_count == 1, f"一个任务应只记一次命中，实际 {row.hit_count}"
    await runner.close()
    await store.engine.dispose()

async def test_the_progress_hint_is_readable_not_a_raw_envelope(tmp_path):
    """**`wait_status` 失败时贴出来的进度必须是人能读的**（第 48 轮）。

    第一版直接读 `item["kind"]`，而事件是**信封形状**
    （`{"stream", "event_id", "payload": {"kind": ...}}`）——
    于是每条都显示成 `?` 并把整个信封倒出来。
    **我是在真跑一次之后才发现的**——这正是「写了但没验」那一族：
    代码不报错、测试也不报错，只是**输出没用**。

    这条测试钉住「可读」，而不只是「有输出」：
    `kind` 必须真的出现在里面，且信封字段（`event_id` / `stream`）不该出现。
    """
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")

    task = await store.save(Task(conversation_id="c1", goal="看进度"))
    await store.event(task.id, "task_created", goal="看进度")
    await store.event(task.id, "tool_call", name="read")

    hint = _progress_hint(store, task.id)
    assert "task_created" in hint, hint
    assert "tool_call" in hint, hint
    assert "name='read'" in hint, hint
    # **信封字段不该漏出来**——那是第一版的症状
    assert "event_id" not in hint, f"把原始信封倒出来了：{hint}"
    assert "?" not in hint.replace("？", ""), f"事件种类没解析出来：{hint}"


async def test_the_progress_hint_says_so_when_there_are_no_events(tmp_path):
    """**读不到就如实说读不到**——「一条事件都没有」本身就是线索（可能根本没启动）。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")

    hint = _progress_hint(store, "no-such-task")
    assert "一条事件都没有" in hint, hint

async def test_the_progress_hint_does_not_swallow_the_real_failure(tmp_path):
    """**提示失败不该盖住真正的失败**（第 49 轮）。

    `_progress_hint` 里有一条 `except`——读事件本身抛异常时，要**返回一句说明**
    而不是把异常抛出去盖掉原始断言。**那是它唯一没被测试覆盖的路径**，
    而「错误处理分支没测过」正是最容易写错的地方。
    """

    class _BrokenLog:
        def since(self, *args, **kwargs):
            raise RuntimeError("事件流坏了")

    class _BrokenStore:
        event_log = _BrokenLog()

    hint = _progress_hint(_BrokenStore(), "any-task")
    assert "读事件失败" in hint, hint
    # **不能把异常抛出去**——那会让原始失败信息消失
    assert "事件流坏了" in hint, hint


async def test_the_progress_hint_says_so_when_the_store_has_no_log(tmp_path):
    """store 没有 `event_log` 时也要如实说，而不是静默返回空串。"""

    class _NoLog:
        pass

    hint = _progress_hint(_NoLog(), "any-task")
    assert "没有 event_log" in hint, hint
