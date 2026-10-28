"""子任务委派：深度 / 并发 / 预算三道闸 + 「中断补位」。

为什么是这三道闸——每一条都对应一种「委派跑飞」的真实形态（Hermes 侧的证据见
``handoff/hermes-parity/research/09-core-loop-layer.md`` §1.1–1.4、§4.1）：

* **深度**：子任务还能再委派子任务，没有硬上限时一棵树可以指数膨胀。Hermes 用
  ``MAX_DEPTH``（默认 1 = 扁平）挡住；Orbit 放宽到 2 层（父 → 子 → 孙），第 3 层
  直接拒绝并说明。**递归委派是最容易失控的地方**，所以必须是硬上限，而不是写在
  prompt 里的"请不要委派太深"。
* **并发**：N 个 child 同时打同一份模型凭据，父任务反而被饿死。用
  ``asyncio.Semaphore`` 卡在"同一时刻真正在跑的子任务数"上。Hermes 的同名常量是
  ``_DEFAULT_MAX_CONCURRENT_CHILDREN = 10``（【实证】``delegate_tool.py:123``）；
  Orbit 默认 **4**：单机单凭据的本地部署，4 已经够把等待时间压下去，再高只会把
  限流打满。
* **预算**：子任务与父任务共用一个 ``TurnBudget`` 时，一个跑飞的子任务能把父预算
  烧光，父任务随后连"收尾那一步"都跑不了。所以每个子任务拿**独立**的
  ``TurnBudget`` 实例 + 独立的步数/token 账，父预算不因子任务而扣减。Hermes 源码
  注释把这条写明了："total iterations across parent + subagents can exceed the
  parent's max_iterations"（【实证】``delegate_tool.py:1946-1949``）——即**父与子
  是三套独立预算，不叠加换算**。

「中断补位」的语义（实证部分见 §1.4）：

* 【实证】``delegate_task action=stop`` = "提前结束并仍返回部分结果"（§4.1 表格）。
* 【实证】被中断的工具仍产出**一条正常的 tool result**：
  ``{"output": "[interrupted]", "returncode": 130}``（``interrupt.py:11-14``）——
  所以中断路径不能靠"跳过"，必须交出一个结果，否则消息配对当场非法。
* 【实证】父被中断时，仍在 pending 的 child 会被合成一条
  ``{"status": "interrupted", ...}`` 的结果条目，而不是被丢弃
  （``delegate_tool.py:4289-4327``）。
* 【推断】"父任务拿到**已完成部分**"这一步的具体文本形态：Hermes 的 live
  transcript（``delegation_live_log.py``，每个 child 一个 append-only 文件）是
  partial 的真实来源，但把 partial 汇总进返回值的那段代码在 PYZ 内，本机无法
  取证。本实现按"child 自己交出 partial"来落地：``ChildRunner.partial_output()``，
  拿不到任何东西时退回占位串 ``[interrupted]``（**非空**，保证父侧渲染不出现空洞）。

接线由谁做（本轮**不改**任何既有文件）：``app/policies.py`` 里
``Policies.subagents`` 现在是 ``DefaultSubAgentRunner``（``available = False``，
``run`` 直接 ``NotImplementedError``）。把它换成 ``DelegateRunner`` 是
``app/agent.py`` / ``app/policies.py`` 维护者的事——注入一个真的 ``runner_factory``
（父 agent → 子 turn 的那条链路）即可，本模块不假设自己怎么被实例化。没注入之前
默认工厂会给出"未注入子任务 runner"的失败结果，而不是假装跑过一次模型
（见 :attr:`DelegateRunner.available`）。
"""

from __future__ import annotations

import asyncio
import builtins
import dataclasses
import inspect
import logging
import sys
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app import policies as policies_module
from app.budget import BudgetConfig, TurnBudget

log = logging.getLogger("orbit.delegate")

#: 深度上限的默认值：父(0) → 子(1) → 孙(2)。第 3 层（depth=3）直接拒绝。
#:
#: 与 Hermes 的差别是刻意的：Hermes ``MAX_DEPTH = 1`` 默认"扁平"，只有
#: ``orchestrator`` 型子 agent 才能再派生（【实证】§4.1）。Orbit 这一轮要的是
#: "父 → 子 → 孙"这条链跑通，所以放宽一层，但仍然**有**硬上限。
DEFAULT_MAX_DEPTH = 2

#: 并发上限的默认值。可配（构造参数 / 将来接 settings）。
DEFAULT_MAX_CONCURRENT = 4

#: 父轮次迭代预算。Hermes ``agent.max_turns`` 默认 90（【实证】§1.1/1.2）。
#: 注意：这两个数字**不在** ``app/policies.py`` 里——那里只有 ``SubAgentRunner``
#: 这个 Protocol 的形状（``available`` 恒 False），没有数值常量。数值来自研究文档，
#: 所以这里显式落成常量并注明来源，避免"以为它从别处读来的"。
PARENT_BUDGET_STEPS = 90

#: 子任务迭代预算。Hermes ``delegation.max_iterations`` 默认 50（【实证】§1.2：
#: ``subagent_runner.py:16`` 签名默认 + 日志里 ``budget=…/50``）。
CHILD_BUDGET_STEPS = 50

#: 子任务能从"父剩余 token"里分到的比例。路线图 W11 判据："父剩余 50% 按批均分"。
PARENT_BUDGET_SHARE = 0.5

#: 子任务 token 预算的地板。低于这个数，子任务连"读一遍上下文"都不够，
#: 分配得再"公平"也没有意义（路线图 W11 判据：下限 2,000）。
MIN_CHILD_BUDGET_TOKENS = 2_000

#: 中断时交不出一句已完成部分时用的占位串。**必须非空**：对齐 Hermes
#: ``{"output": "[interrupted]"}`` 的语义——父任务永远拿到一条能渲染的结果。
INTERRUPTED_PLACEHOLDER = "[interrupted]"


class DelegateError(RuntimeError):
    """委派的**编程性**失败（参数非法、状态错乱）。

    业务性失败（子任务报错、超预算、被中断）不抛异常，走 ``DelegateResult``——
    父任务需要的是"结果 + 原因"，而不是一个把整批子任务带崩的异常。
    """


@dataclass(frozen=True)
class DelegateSpec:
    """一次委派请求。字段刻意少：每个字段都对应 Hermes 真实存在的一个开关。"""

    #: 子任务目标。空串在预检阶段就被拒（空目标跑一轮模型纯属烧钱）。
    goal: str
    #: 该子任务在委派树里的层级：父派出的子任务 = 1，孙 = 2。
    #: 语义是"我这一层"，不是"我还能再派几层"——后者由 runner 的上限决定。
    depth: int = 1
    #: 迭代步数预算。0 = 用 ``DelegateRunner.child_steps``（默认 50）。
    budget_steps: int = 0
    #: token 预算。0 = 按"父剩余 × 50% ÷ 同批兄弟数"算，地板 2000。
    budget_tokens: int = 0
    #: 非空 = 在 ``<data_dir>/worktrees/<worktree>`` 里跑（git worktree 隔离）。
    #: Hermes 侧对应 ``delegation.worktree_isolation``（bool，默认 false，
    #: 非 git 仓库时**静默忽略**）；Orbit 用名字而不是 bool，因为"隔离到哪个
    #: 工作树"是可观测的东西，`orbit worktree list` 能把它列出来。
    worktree: str = ""
    #: 允许子任务使用的工具名。空 = 不限制（继承父的工具集）。
    allowed_tools: tuple[str, ...] = ()
    #: 子任务用的模型。空 = 继承父。
    model: str = ""


@dataclass
class ChildBudget:
    """一份**独立**的子任务预算。

    "隔离"的全部含义就在这句话里：``DelegateRunner`` 从不在父 ``TurnBudget`` 上
    记子任务的账，子任务的工具结果只进自己这一份。于是子任务把预算烧光，父任务
    的 ``total`` 一分不少——这正是"一个跑飞的子任务不该把父任务饿死"。
    """

    #: 迭代步数上限（0 = 不限）。
    steps: int = CHILD_BUDGET_STEPS
    #: token 估算上限（0 = 不限）。
    tokens: int = MIN_CHILD_BUDGET_TOKENS
    #: 子任务自己的三层治理第三层（整轮工具结果预算）。**独立实例**。
    turn: TurnBudget = field(default_factory=TurnBudget)
    steps_used: int = 0
    tokens_used: int = 0

    @property
    def exhausted(self) -> bool:
        """是否已经用满预算。子任务 runner 拿它决定"该收尾了"。"""
        if self.steps and self.steps_used >= self.steps:
            return True
        return bool(self.tokens and self.tokens_used >= self.tokens)

    def charge_step(self, tokens: int = 0) -> bool:
        """记一步并返回"还能继续吗"。``False`` = 已超预算，请收尾。"""
        self.steps_used += 1
        self.tokens_used += max(0, int(tokens))
        return not self.exhausted

    def summary(self) -> dict[str, Any]:
        return {
            "steps": self.steps,
            "steps_used": self.steps_used,
            "tokens": self.tokens,
            "tokens_used": self.tokens_used,
            "turn_total": self.turn.total,
            "turn_spilled": self.turn.spilled,
            "exhausted": self.exhausted,
        }


@dataclass
class DelegateResult:
    """一次委派的结果。父任务只该看这一个对象。

    ``status`` / ``exit_reason`` 的口径照抄 Hermes（【实证】§4.1）：
    ``status ∈ {completed, interrupted, failed}``、
    ``exit_reason ∈ {completed, max_iterations, interrupted, error}``，
    且 ``truncated ≡ (exit_reason == "max_iterations")``。
    Orbit 多两个"委派层自己的"终态：``depth_exceeded``（深度被拒）与
    ``rejected``（预检被拒），因为这两件事在 Hermes 侧是直接抛 tool_error，
    而在 Orbit 里它们是"父任务需要知道的一次失败"，走结果对象更顺。
    """

    ok: bool
    output: str = ""
    steps: int = 0
    tokens: int = 0
    error: str = ""
    child_task_id: str = ""
    #: 中断/失败时父任务能拿到的**已完成部分**。成功时留空。
    partial_output: str = ""
    status: str = "completed"
    exit_reason: str = "completed"

    @property
    def truncated(self) -> bool:
        """是否因为预算耗尽被截断（对齐 Hermes 的 ``truncated`` 定义）。"""
        return self.exit_reason == "max_iterations"

    def to_dict(self) -> dict[str, Any]:
        """给 ``SubAgentRunner`` 那个 ``-> dict`` 形状用的转换（见 satisfies_protocol）。"""
        return {
            "ok": self.ok,
            "output": self.output,
            "steps": self.steps,
            "tokens": self.tokens,
            "error": self.error,
            "child_task_id": self.child_task_id,
            "partial_output": self.partial_output,
            "status": self.status,
            "exit_reason": self.exit_reason,
            "truncated": self.truncated,
        }


@runtime_checkable
class ChildRunner(Protocol):
    """**子任务**的 runner 形状。由 ``runner_factory`` 造出来，一次委派一个实例。

    刻意只有两个方法：``run`` 负责跑，``partial_output`` 负责"被取消时还能交出
    什么"。后者是可选的（用 ``getattr`` 取），因为不是每个假 runner 都关心中断。
    """

    async def run(self, goal: str, **kwargs: Any) -> dict[str, Any]: ...

    def partial_output(self) -> str: ...


#: ``runner_factory(spec, budget) -> ChildRunner``。测试注入假 runner 的口子。
ChildRunnerFactory = Callable[[DelegateSpec, ChildBudget], ChildRunner]


def _missing_factory(spec: DelegateSpec, budget: ChildBudget) -> ChildRunner:
    """默认工厂：明确报"没接线"，而不是返回一个假成功的 runner。

    为什么默认不内置一个"真跑模型"的 runner：那需要父 agent → 子 turn 的整条链路
    （store 建任务、图里跑一轮、审批通道、心跳），属于接线方的职责，塞进本模块会
    把"委派策略"和"agent 装配"搅在一起，也让本模块无法在不加载模型的测试里跑。
    """
    raise DelegateError(
        "未注入子任务 runner：请构造 DelegateRunner 时传 runner_factory=... "
        "（接线在 app/agent.py / app/policies.py，见模块 docstring）"
    )


def _safe_partial(child: Any) -> str:
    """取子任务已交出的部分结果；拿不到就空串（绝不因为取 partial 而炸掉父任务）。"""
    getter = getattr(child, "partial_output", None)
    if not callable(getter):
        return ""
    try:
        return str(getter() or "")
    except Exception as exc:  # partial 是"尽力而为"，取不到不该升级成错误
        log.warning("读取子任务 partial_output 失败（按空处理）：%s", exc)
        return ""


class DelegateRunner:
    """子任务委派器：满足 ``policies.SubAgentRunner`` 的形状（见 ``satisfies_protocol``）。

    **接线由 ``app/agent.py`` / ``app/policies.py`` 的维护者做**：把
    ``Policies.subagents`` 换成 ``DelegateRunner(..., runner_factory=真链路)``。
    本模块不 import ``app.agent``，避免与 agent 侧成环。

    生命周期（对应 Hermes 的异步委派，但 Orbit 这轮只做进程内）：

    * :meth:`submit` 起一个子任务，立刻拿到 ``child_task_id``（可以据此 ``cancel``）；
    * :meth:`run` 是"起 + 等"的便捷写法，父任务日常用它；
    * :meth:`cancel` 请求中断：子任务交出 partial，父任务拿到 ``status="interrupted"``。
    """

    def __init__(
        self,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_concurrent: int = DEFAULT_MAX_CONCURRENT,
        runner_factory: ChildRunnerFactory | None = None,
        parent_budget: TurnBudget | None = None,
        data_dir: Any = None,
        parent_steps: int = PARENT_BUDGET_STEPS,
        child_steps: int = CHILD_BUDGET_STEPS,
        child_tokens: int = MIN_CHILD_BUDGET_TOKENS,
        child_budget_config: BudgetConfig | None = None,
    ) -> None:
        if int(max_depth) < 1:
            raise DelegateError(f"max_depth 必须 ≥ 1（收到 {max_depth}）：深度上限是硬闸，不能关掉")
        if int(max_concurrent) < 1:
            raise DelegateError(f"max_concurrent 必须 ≥ 1（收到 {max_concurrent}）")
        self.max_depth = int(max_depth)
        self.max_concurrent = int(max_concurrent)
        self.runner_factory: ChildRunnerFactory = runner_factory or _missing_factory
        #: 父任务的整轮预算。**只读**：用来算子任务能分多少，绝不往上记账。
        self.parent_budget = parent_budget
        self.data_dir = data_dir
        self.parent_steps = int(parent_steps)
        self.child_steps = int(child_steps)
        self.child_tokens = int(child_tokens)
        #: 子任务 TurnBudget 的阈值。不传就沿用父的配置（单位是字符，别拿 token 数硬套）。
        self.child_budget_config = child_budget_config
        self._tasks: dict[str, asyncio.Task[DelegateResult]] = {}
        self._inner: dict[str, asyncio.Task[Any]] = {}
        self._children: dict[str, Any] = {}
        self._results: dict[str, DelegateResult] = {}
        self._cancel_requested: set[str] = set()
        #: 父任务自己被取消时，各子任务已经交出的部分（父可以在收尾时读它）。
        self.interrupted_partials: dict[str, str] = {}
        #: 每事件循环一个信号量：``asyncio.Semaphore`` 首次 await 时绑定循环，
        #: 跨 ``asyncio.run`` 复用同一个实例会抛 "bound to a different event loop"。
        self._semaphores: dict[int, asyncio.Semaphore] = {}

    # ── 能力自述 ────────────────────────────────────────────────────────────
    @property
    def available(self) -> bool:
        """``DefaultSubAgentRunner.available`` 恒 False；这里如实反映"有没有真 runner"。"""
        return self.runner_factory is not _missing_factory

    @property
    def active(self) -> list[str]:
        """还在跑的子任务 id（诊断/CLI 用）。"""
        return [cid for cid, task in self._tasks.items() if not task.done()]

    def result_of(self, child_task_id: str) -> DelegateResult | None:
        """已完成子任务的结果（没有就 ``None``）。"""
        return self._results.get(child_task_id)

    def budget_preview(self, spec: DelegateSpec, siblings: int = 1) -> ChildBudget:
        """预演用：算出这个 spec 会拿到什么预算，但**不跑**任何东西。

        给 ``orbit delegate run --dry-run`` 用。公开这个方法而不是让 CLI 去碰
        ``_child_budget``：预演和真跑必须走同一条预算解析路径，否则预演就会
        慢慢变成"另一份实现"，也就失去意义。
        """
        return self._child_budget(spec, siblings)

    # ── 起 / 等 / 停 ────────────────────────────────────────────────────────
    def submit(self, spec: DelegateSpec, *, siblings: int = 1) -> str:
        """起一个子任务并返回 ``child_task_id``。**必须在事件循环内调用**。

        ``siblings`` 是"同一批一起跑的子任务个数"，只影响 token 预算的均分
        （父剩余 × 50% ÷ siblings，地板 2000）——所以并发上限与预算分配是两件事，
        这里分开传。
        """
        child_id = f"child-{uuid.uuid4().hex[:12]}"
        self._tasks[child_id] = asyncio.ensure_future(self._execute(child_id, spec, siblings))
        return child_id

    async def wait(self, child_task_id: str) -> DelegateResult:
        """等一个子任务的结果。重复等同一个 id 会拿到缓存的结果。"""
        cached = self._results.get(child_task_id)
        if cached is not None:
            return cached
        task = self._tasks.get(child_task_id)
        if task is None:
            raise DelegateError(f"没有这个子任务：{child_task_id}")
        result = await task
        self._results[child_task_id] = result
        return result

    def cancel(self, child_task_id: str) -> bool:
        """请求中断某个子任务（对齐 Hermes ``delegate_task action=stop``）。

        只取消**内层**的 runner 任务，不取消自己那个 ``_execute`` 任务——因为
        "被取消"这件事必须变成一条**结果**交回父任务，而不是把父任务的 await
        一起炸掉。返回 ``False`` 表示它已经结束/不存在。
        """
        inner = self._inner.get(child_task_id)
        if inner is None or inner.done():
            return False
        self._cancel_requested.add(child_task_id)
        inner.cancel()
        return True

    async def run(self, goal: str | DelegateSpec, **kwargs: Any) -> DelegateResult:
        """跑一个子任务并等结果。

        签名照 ``policies.SubAgentRunner.run(self, goal, **kwargs)``：既接受
        ``DelegateSpec``（Orbit 的完整形态），也接受裸 ``goal`` 字符串 + 关键字
        （Protocol 的形状）。未识别的关键字按 debug 记一条日志后忽略——Hermes 的
        ``delegate_task`` 也保留了一堆"旧参数仍被接受但不写进 schema"的字段。
        """
        siblings = int(kwargs.pop("siblings", 1) or 1)
        spec = self._coerce_spec(goal, kwargs)
        return await self.wait(self.submit(spec, siblings=siblings))

    def _coerce_spec(self, goal: str | DelegateSpec, kwargs: dict[str, Any]) -> DelegateSpec:
        if isinstance(goal, DelegateSpec):
            return goal
        known_names = {item.name for item in dataclasses.fields(DelegateSpec)}
        known = {key: value for key, value in kwargs.items() if key in known_names}
        unknown = sorted(set(kwargs) - set(known))
        if unknown:
            log.debug("忽略未识别的委派参数：%s", unknown)
        return DelegateSpec(goal=str(goal), **known)

    # ── 内部：执行 ──────────────────────────────────────────────────────────
    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        key = id(loop)
        semaphore = self._semaphores.get(key)
        if semaphore is None:
            semaphore = asyncio.Semaphore(self.max_concurrent)
            self._semaphores[key] = semaphore
        return semaphore

    def _preflight(self, child_id: str, spec: DelegateSpec) -> DelegateResult | None:
        """跑之前的硬检查：空目标、深度超限。返回 ``None`` = 放行。"""
        if not str(spec.goal or "").strip():
            return DelegateResult(
                ok=False,
                error="委派被拒：goal 为空",
                child_task_id=child_id,
                status="failed",
                exit_reason="rejected",
            )
        depth = max(1, int(spec.depth or 1))
        if depth > self.max_depth:
            return DelegateResult(
                ok=False,
                error=(
                    f"委派深度超限：depth={depth} 超过上限 {self.max_depth}"
                    f"（父 → 子 → 孙最多 {self.max_depth} 层）。"
                    "递归委派必须有硬上限：再往下派，任务树会指数膨胀且没人能停住它。"
                ),
                child_task_id=child_id,
                status="failed",
                exit_reason="depth_exceeded",
            )
        return None

    def _child_budget(self, spec: DelegateSpec, siblings: int) -> ChildBudget:
        """给子任务造一份**独立**预算。父预算只被读，不被扣。"""
        steps = int(spec.budget_steps or 0) or self.child_steps
        tokens = int(spec.budget_tokens or 0) or self._share_tokens(siblings)
        config = self.child_budget_config
        if config is None and self.parent_budget is not None:
            config = self.parent_budget.config
        return ChildBudget(
            steps=steps,
            tokens=tokens,
            turn=TurnBudget(data_dir=self.data_dir, config=config),
        )

    def _share_tokens(self, siblings: int) -> int:
        """父剩余 × 50% ÷ 兄弟数，地板 ``MIN_CHILD_BUDGET_TOKENS``。

        没有父预算时直接给地板：给不出"剩余"就没有"公平分配"可言，地板至少让
        子任务能跑起来（并且**允许**总量超过父预算——这是 Hermes 的既定设计，
        见模块 docstring 引的那句源码注释）。
        """
        if self.parent_budget is None:
            return max(MIN_CHILD_BUDGET_TOKENS, self.child_tokens)
        remaining = max(0, int(self.parent_budget.config.turn_budget) - int(self.parent_budget.total))
        share = int(remaining * PARENT_BUDGET_SHARE / max(1, int(siblings)))
        return max(MIN_CHILD_BUDGET_TOKENS, share)

    async def _execute(self, child_id: str, spec: DelegateSpec, siblings: int) -> DelegateResult:
        rejection = self._preflight(child_id, spec)
        if rejection is not None:
            log.warning("委派被拒（%s）：%s", child_id, rejection.error)
            return rejection
        budget = self._child_budget(spec, siblings)
        async with self._semaphore():
            try:
                child = self.runner_factory(spec, budget)
            except DelegateError:
                raise
            except Exception as exc:  # 工厂自己炸了：变成一条失败结果，不牵连父任务
                log.warning("子任务 runner 创建失败：%s", exc)
                return self._failed(child_id, f"子任务 runner 创建失败：{exc}")
            self._children[child_id] = child
            inner: asyncio.Task[Any] = asyncio.ensure_future(
                child.run(spec.goal, budget=budget, spec=spec)
            )
            self._inner[child_id] = inner
            try:
                payload = await inner
            except asyncio.CancelledError:
                partial = _safe_partial(child)
                current = asyncio.current_task()
                outer_cancelled = current is not None and current.cancelling() > 0
                if child_id not in self._cancel_requested and outer_cancelled:
                    # 父任务自己被取消：把已完成部分留档（父在收尾时能读到），
                    # 然后**继续向上传播**——吞掉取消信号会让父任务停不下来。
                    self.interrupted_partials[child_id] = partial or INTERRUPTED_PLACEHOLDER
                    raise
                return self._interrupted(child_id, partial, budget)
            except Exception as exc:
                log.warning("子任务异常（%s）：%s", child_id, exc)
                return self._failed(child_id, f"子任务异常：{exc}", budget=budget)
            finally:
                self._inner.pop(child_id, None)
            return self._finish(child_id, payload, budget)

    def _failed(self, child_id: str, error: str, *, budget: ChildBudget | None = None) -> DelegateResult:
        return DelegateResult(
            ok=False,
            error=error,
            steps=budget.steps_used if budget else 0,
            tokens=budget.tokens_used if budget else 0,
            child_task_id=child_id,
            status="failed",
            exit_reason="error",
        )

    def _interrupted(
        self, child_id: str, partial: str, budget: ChildBudget
    ) -> DelegateResult:
        """中断补位：把已完成部分交回父任务，**永不**返回空结果。"""
        body = partial.strip() or INTERRUPTED_PLACEHOLDER
        log.info("子任务被中断（%s），补位返回 %d 字符", child_id, len(body))
        return DelegateResult(
            ok=False,
            output=body,
            steps=budget.steps_used,
            tokens=budget.tokens_used,
            error="子任务被中断：父任务拿到已完成部分，未完成的部分不再继续",
            child_task_id=child_id,
            partial_output=body,
            status="interrupted",
            exit_reason="interrupted",
        )

    def _finish(
        self, child_id: str, payload: Any, budget: ChildBudget
    ) -> DelegateResult:
        data = dict(payload or {}) if isinstance(payload, dict) else {}
        output = str(data.get("output") or "")
        error = str(data.get("error") or "")
        raw_reason = str(data.get("exit_reason") or "")
        if raw_reason:
            exit_reason = raw_reason
        elif error:
            exit_reason = "error"
        elif budget.exhausted:
            # 子任务没自报终态但预算已满：按 Hermes 的口径记 max_iterations
            # （它同时也是 truncated 的定义）。
            exit_reason = "max_iterations"
        else:
            exit_reason = "completed"
        ok = exit_reason == "completed" and not error
        return DelegateResult(
            ok=ok,
            output=output,
            steps=int(data.get("steps") or budget.steps_used),
            tokens=int(data.get("tokens") or budget.tokens_used),
            error=error,
            child_task_id=child_id,
            partial_output="" if ok else output,
            status="completed" if ok else "failed",
            exit_reason=exit_reason,
        )


# ── 接口自检 ────────────────────────────────────────────────────────────────


def _resolve_annotation(func: Any) -> Any:
    """把（可能是字符串的）返回注解解析成对象；解析不了就原样返回字符串。

    为什么要这一步：两个文件都用了 ``from __future__ import annotations``，注解在
    运行时是字符串。判"返回值形状"时必须真的拿到类，否则 ``"dict" != dict`` 会让
    自检得出反向结论。解析顺序是"函数所在模块 → builtins → 放弃"：
    ``dict`` / ``object`` 这类内建名字不在模块字典里，只能去 builtins 捞。
    """
    try:
        annotation = inspect.signature(func).return_annotation
    except (TypeError, ValueError):
        return None
    if not isinstance(annotation, str):
        return annotation
    module = sys.modules.get(getattr(func, "__module__", ""), None)
    if module is not None and hasattr(module, annotation):
        return getattr(module, annotation)
    if hasattr(builtins, annotation):
        return getattr(builtins, annotation)
    return annotation


def _return_shape_ok(expected: Any, actual: Any) -> bool:
    """返回值形状：Protocol 写 ``-> dict``，实现可以返回 ``dict``，或任何带
    ``to_dict()`` 的结果对象（本模块的 ``DelegateResult`` 就是后者）。

    这是"形状"里唯一**有意**的差异，所以判据写在这里而不是靠肉眼：
    接线方若真要一个裸 dict，``result.to_dict()`` 一下就得到。
    """
    want = _resolve_annotation(expected)
    if want in (inspect.Signature.empty, None, Any):
        return True
    if want is not dict:
        return True  # Protocol 将来若换成别的类型，本函数不越权判断
    have = _resolve_annotation(actual)
    if have is dict:
        return True
    return callable(getattr(have, "to_dict", None))


def satisfies_protocol(
    candidate: Any = None,
    protocol: Any = None,
) -> bool:
    """自检：``candidate`` 是否覆盖 ``policies.SubAgentRunner`` 的 Protocol 方法。

    为什么需要它：``SubAgentRunner`` 是**结构**类型，只有真的把它赋给
    ``Policies.subagents`` 才会被 mypy 检查——而那个赋值发生在只读的
    ``app/policies.py`` 里。把"接口对不对"变成一条可执行的判据，才不用靠肉眼，
    也不会出现"改了签名、没人发现"的静默漂移。

    判据（四条，全过才返回 True）：

    1. Protocol 里每个公开方法，``candidate`` 上都有同名可调用属性；
    2. 那个属性是**协程函数**——同步实现会让 ``await`` 当场炸；
    3. 它接受 Protocol 声明的每个具名参数（``goal``），且能吞 ``**kwargs``；
    4. 返回形状兼容：``dict`` 或带 ``to_dict()`` 的结果对象（见
       :func:`_return_shape_ok`）。

    默认参数就是"自检自己"：``satisfies_protocol()`` 为 True 才算接线安全。
    传别的 candidate（哪怕是个故意缺方法的空类）时同样给出可判定的布尔值——
    这也是测试里"去掉一个方法必须返回 False"那条用例的入口。
    """
    target = DelegateRunner if candidate is None else candidate
    proto = policies_module.SubAgentRunner if protocol is None else protocol
    holder = target if inspect.isclass(target) else type(target)
    for name, raw in vars(proto).items():
        if name.startswith("_") or not inspect.isfunction(raw):
            continue
        method = getattr(holder, name, None)
        if method is None or not callable(method):
            return False
        if not inspect.iscoroutinefunction(method):
            return False
        wanted = inspect.signature(raw).parameters
        have = inspect.signature(method).parameters
        has_kwargs = any(p.kind is p.VAR_KEYWORD for p in have.values())
        for pname, param in wanted.items():
            if pname in {"self", "cls"}:
                continue
            if param.kind is param.VAR_KEYWORD:
                if not has_kwargs:
                    return False
                continue
            if pname not in have and not has_kwargs:
                return False
        if not _return_shape_ok(raw, method):
            return False
    return True
