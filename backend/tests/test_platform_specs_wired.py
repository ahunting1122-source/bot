"""工具暴露面按**任务来源平台**过滤（MATRIX 6.4 的验收判据）。

## 之前缺的是什么

`ToolRegistry.specs(platform=...)` 与 `toolsets.resolve(platform=...)` **都支持平台**，
`platforms.<name>.disabled/enabled` 那套配置也读得好好的。但 `graph/nodes.py` 调
`specs()` 时**不传 platform**，默认 `cli`——于是**多平台差异化从没生效过**：
接口成立、全仓零非 `cli` 调用点。

## 为什么平台必须由「驱动任务的那一层」告诉运行时

`Task` 与 `Conversation` **都没有平台字段**。运行时自己猜不出来。所以：
`enqueue(..., platform=...)` 记录 → `runner.platform_for(task_id)` 取回 →
`nodes.py` 传给 `specs()`。

**没登记就是 `cli`**——既有行为逐字不变（这是本条的硬要求：过滤层必须先是「能关」）。

## 判据
1. **同一份 `tools.json`，`cli` 与 `api` 暴露的工具集真的不同**；
2. `enqueue(..., platform="api")` 之后 `platform_for` 真的返回 `api`；
3. 没登记时返回 `cli`；
4. 源码层：`nodes.py` 里 `specs(...)` 必须真的带 platform。
"""

from __future__ import annotations

import json
import pathlib

import pytest


@pytest.fixture()
def registry(tmp_path):
    from app.config import Settings
    from app.storage import Store
    from app.tools import ToolRegistry

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    # 给 api 平台禁用两个工具：这样两个平台的暴露面**必然不同**
    (settings.data_dir / "tools.json").write_text(
        json.dumps({"platforms": {"api": {"disabled": ["history_search_tasks", "history_get_task"]}}}),
        encoding="utf-8",
    )
    store = Store(settings.data_dir / "app.sqlite")
    reg = ToolRegistry(settings, store)
    return reg, settings


def test_the_same_config_gives_different_tools_per_platform(registry):
    """**核心判据**：同一份配置下，`cli` 与 `api` 暴露的工具集真的不同。"""
    reg, _settings = registry

    def _names(specs):
        """`specs()` 返回的是 schema 对象（或 dict），两种都要认。"""
        out = set()
        for spec in specs:
            if isinstance(spec, dict):
                out.add(spec.get("name") or (spec.get("function") or {}).get("name"))
            else:
                out.add(getattr(spec, "name", None) or getattr(spec, "get", lambda *_: None)("name"))
        return {name for name in out if name}

    cli = _names(reg.specs("cli"))
    api = _names(reg.specs("api"))

    assert "history_search_tasks" in cli, f"前置条件不成立：cli 平台本该有它（现有 {len(cli)} 个）"
    assert "history_search_tasks" not in api, "api 平台禁用了它，却还在暴露面里"
    assert cli - api, "两个平台的暴露面完全相同——平台层没生效"


def test_platform_defaults_to_cli_when_nothing_is_registered(tmp_path):
    """没登记平台时返回 `cli`——**既有行为逐字不变**。"""
    from app.agent import AgentRunner
    from app.config import Settings
    from app.storage import Store
    from app.tools import ToolRegistry

    settings = Settings(data_dir=tmp_path / "d2", workspace=tmp_path / "f2")
    settings.prepare()
    store = Store(settings.data_dir / "app.sqlite")
    runner = AgentRunner(settings, store, ToolRegistry(settings, store))
    assert runner.platform_for("never-seen-task") == "cli"


async def test_enqueue_records_the_platform(tmp_path):
    """`enqueue(..., platform="api")` 之后 `platform_for` 必须返回 `api`。"""
    from app.agent import AgentRunner
    from app.config import Settings
    from app.main import migrate
    from app.storage import Conversation, Store, Task
    from app.tools import ToolRegistry

    settings = Settings(data_dir=tmp_path / "d3", workspace=tmp_path / "f3")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")
    conversation = await store.save(Conversation())
    task = await store.save(Task(conversation_id=conversation.id, goal="x"))
    runner = AgentRunner(settings, store, ToolRegistry(settings, store))
    try:
        await runner.enqueue(task.id, platform="api")
        assert runner.platform_for(task.id) == "api"
    finally:
        # 不要调 `runner.close()`：runner 没 `start()` 过，
        # `close()` 会去 `self.worker.cancel()` 而 worker 还不存在。
        # 只释放数据库连接就够。
        await store.engine.dispose()


def test_the_real_runner_has_platform_for():
    """**对真实类**的反证断言：`AgentRunner.platform_for` 必须存在。

    `nodes.py` 用 `getattr(runner, "platform_for", None)` 兜底，而 `"cli"` 是
    **合法默认值**（Hermes 的 `--platform` 默认也是 cli），所以那个兜底不算
    「静默降级」。

    **但它确实掩盖「真类把这个方法弄丢了」**：删掉 `platform_for` 之后，
    所有任务会静默退回 cli，而**全量测试仍然全绿**——因为假件本来就没有这个方法。

    所以这条只钉**真实类**，假件保持宽松（不动那 9 个假 registry）。
    """
    from app.agent import AgentRunner

    assert callable(getattr(AgentRunner, "platform_for", None)), (
        "`AgentRunner` 丢了 `platform_for`——所有任务会静默退回 cli，且测试查不出来"
    )


async def test_a_deferred_reenqueue_keeps_the_platform(tmp_path):
    """**时序判据**：任务在跑时延后入队，平台不能被降级成 `cli`。

    ## 这是一个真 bug 的回归测试

    原实现的时序：

    1. `enqueue(platform="api")` **最前面**记下 `"api"`；
    2. 发现任务在跑 → 走延后分支；
    3. 原 job 收尾时 `finally` 里 `pop` 掉记录；
    4. `_enqueue_after` **才**重新入队，而且**不传 platform** → 默认 `"cli"`。

    **「把信息放在最前面」只解决「早退时丢信息」，不解决「重新入队时用什么值」。**

    后果不是理论上的：**延后入队正是 `WAITING_USER` 恢复的主路径**——
    用户在 API 上回答澄清 → 恢复后平台被降级成 cli → 工具集跟着换掉，
    而且**发生在任务中途**。

    **这条测试故意在「记录被 pop 之后」才让 job 收尾**，否则它证明不了任何事
    （记录还在的时候读当然是对的）。
    """
    import asyncio

    from app.agent import AgentRunner
    from app.config import Settings
    from app.storage import Store
    from app.tools import ToolRegistry

    settings = Settings(data_dir=tmp_path / "d4", workspace=tmp_path / "f4")
    settings.prepare()
    store = Store(settings.data_dir / "app.sqlite")
    runner = AgentRunner(settings, store, ToolRegistry(settings, store))
    task_id = "task-deferred"

    seen: list[str] = []

    async def _spy_after(_job, _task_id, _resume, platform="cli"):
        # 与真实 `_enqueue_after` 一样**先等 job 收尾**再记录——
        # 这样记录发生在「平台记录已被 pop 之后」，才真的测到那个时序。
        await asyncio.gather(_job, return_exceptions=True)
        seen.append(platform)

    # 一个**还没收尾**的 job，逼 `enqueue` 走延后分支
    gate = asyncio.Event()
    job = asyncio.create_task(gate.wait())
    runner.current[task_id] = job
    # **只换延后那一步**，不换 `enqueue` 本身——平台记录是 `enqueue` 写的，
    # 换掉它前置条件就不成立了（第一版就栽在这里）。
    runner._enqueue_after = _spy_after  # type: ignore[method-assign]

    await runner.enqueue(task_id, platform="api")
    assert runner._task_platform[task_id] == "api", "前置条件：记录应已写入"

    # **模拟原 job 收尾时的 pop**（`agent.py` 的 `finally` 做的就是这个）
    runner._task_platform.pop(task_id, None)

    gate.set()
    for _ in range(30):
        await asyncio.sleep(0.01)
        if seen:
            break

    assert seen, "延后入队根本没发生——测试没构造出那个时序"
    assert seen[-1] == "api", (
        f"延后重新入队把平台降级成了 {seen[-1]!r}——"
        "WAITING_USER 恢复后工具集会跟着换掉，而且发生在任务中途"
    )


def test_nodes_passes_the_platform_to_specs():
    """**防回归**：`nodes.py` 里 `specs(...)` 必须真的带 platform。

    6.4 的虚报形状是「接口成立、零非 cli 调用点」——
    只验 `specs(platform=...)` 能用是不够的，必须钉住调用点传了值。
    """
    from app.graph import nodes as nodes_module

    source = pathlib.Path(nodes_module.__file__).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines() if not line.strip().startswith("#")
    )
    assert "self.registry.specs(platform)" in code, (
        "`nodes.py` 不再把 platform 传给 `specs()` 了——多平台差异化又变成死接口"
    )
