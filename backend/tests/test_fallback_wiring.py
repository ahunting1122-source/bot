"""证明回退链与凭证池**真的接进了** `AgentRunner.get_model()`。

判据不是"grep 到了 `FallbackChain` 这个词"（那样的话把 import 删掉也能过），而是：
**调 `get_model()`，用假工厂观察它到底换没换站、换的是 key 还是 provider。**

所有用例零网络：工厂是注入的假函数，钥匙池是 `MemoryStore`，模型客户端是假对象。
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest
from pydantic import SecretStr

from app import fallback as fb
from app.agent import AgentRunner, Route, RoutedModel
from app.config import Settings
from app.storage import Store
from app.tools import ToolRegistry

# ── 假件 ────────────────────────────────────────────────────────────────────


class FakeStatusError(Exception):
    """带 HTTP 状态码的假异常（与 `openai.APIStatusError` 的形状一致：挂 `status_code`）。"""

    def __init__(self, code: int, message: str = "") -> None:
        super().__init__(message or f"status {code}")
        self.status_code = code


class FakeClient:
    """可 `await ainvoke()` 的最小客户端。`fail` 非空时调用即抛。"""

    def __init__(self, label: str, *, fail: BaseException | None = None, answer: str = "") -> None:
        self.label = label
        self.fail = fail
        self.answer = answer or f"answer-from-{label}"

    def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN201 —— 形状照 langchain
        return self

    async def ainvoke(self, messages, **kwargs):  # noqa: ANN001, ANN003, ANN201
        if self.fail is not None:
            raise self.fail
        return self.answer


@pytest.fixture
def settings(tmp_path):
    """一份"看起来配好了"的配置：主模型 = openai/gpt-5.4。"""
    data = Settings(
        model="gpt-5.4",
        model_provider="openai",
        model_base_url="https://api.openai.com/v1",
        model_api_key=SecretStr("sk-primary-123456"),
        data_dir=tmp_path / "data",
        workspace=tmp_path / "files",
    )
    data.prepare()
    return data


@pytest.fixture
def runner(settings):
    """真 `AgentRunner`（`get_model` 是它的方法，不能绕开它测）。

    `_key_ring` 被换成内存池：生产实现走系统 keyring，测试里不能碰真钥匙串。
    """
    store = Store(settings.data_dir / "app.sqlite")
    runner = AgentRunner(settings, store, ToolRegistry(settings, store))
    yield runner
    asyncio.run(store.engine.dispose())


def use_pool(runner, pool: fb.CredentialPool) -> None:
    """把 runner 的钥匙来源换成内存凭证池（生产是 `KeyringStore`）。"""
    runner._key_ring = lambda: fb.PoolKeyRing(pool, primary=runner._primary_slot)  # noqa: SLF001


def pool_with(*keys: str) -> fb.CredentialPool:
    pool = fb.CredentialPool(fb.MemoryStore())
    for key in keys:
        pool.add("openai", key)
    return pool


def write_chain(settings, *targets: dict) -> None:
    """把回退链写进 `<data_dir>/settings.json`——与 `orbit fallback add` 同一个文件同一个键。"""
    path = settings.data_dir / "settings.json"
    path.write_text(json.dumps({"fallback_chain": list(targets)}), encoding="utf-8")


class Recorder:
    """假工厂：记录每次被要求造哪个候选，并按脚本决定成功或抛错。"""

    def __init__(self, script) -> None:  # noqa: ANN001
        self.script = script
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, candidate: fb.Candidate):  # noqa: ANN204
        self.calls.append(
            (candidate.target.provider, candidate.target.model, candidate.key.label)
        )
        outcome = self.script(candidate, len(self.calls))
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    @property
    def providers(self) -> list[str]:
        return [item[0] for item in self.calls]

    @property
    def key_labels(self) -> list[str]:
        return [item[2] for item in self.calls]


# ── 1. 主目标正常时不动 ─────────────────────────────────────────────────────


def test_primary_target_is_used_when_it_builds(settings, runner):
    recorder = Recorder(lambda candidate, n: FakeClient("primary"))
    model = runner.get_model(factory=recorder)
    assert isinstance(model, RoutedModel)
    assert model.route.provider == "openai"
    assert model.route.model == "gpt-5.4"
    assert model.route.attempt == 0
    assert model.route.key_label == "primary"
    assert len(recorder.calls) == 1


# ── 2. 401 → 换 key，不换 provider ──────────────────────────────────────────


def test_401_switches_key_within_same_provider(settings, runner):
    """401 是**凭据**问题：同一家的另一把钥匙有意义，整家放弃太早。"""
    use_pool(runner, pool_with("sk-key-A", "sk-key-B"))
    recorder = Recorder(
        lambda candidate, n: FakeStatusError(401, "invalid api key")
        if n == 1
        else FakeClient(candidate.key.label)
    )
    model = runner.get_model(factory=recorder)

    assert recorder.providers == ["openai", "openai"], "401 不该换 provider"
    assert recorder.key_labels == ["pool#0", "pool#1"], "应当换到同一家的下一把 key"
    assert model.route.key_label == "pool#1"
    assert model.route.attempt == 1


def test_429_switches_key_within_same_provider(settings, runner):
    """429 是**配额**问题：配额按 key 计，换 key 比换家更可能直接成功。"""
    use_pool(runner, pool_with("sk-key-A", "sk-key-B"))
    recorder = Recorder(
        lambda candidate, n: FakeStatusError(429, "rate limited")
        if n == 1
        else FakeClient(candidate.key.label)
    )
    model = runner.get_model(factory=recorder)
    assert recorder.providers == ["openai", "openai"]
    assert recorder.key_labels == ["pool#0", "pool#1"]
    assert model.route.provider == "openai"


# ── 3. 400 → 换 provider ────────────────────────────────────────────────────


def test_400_switches_provider(settings, runner):
    """400 是**这家特有**的拒绝（参数/模型不兼容）：同家换 key 会一模一样地再错一次。"""
    write_chain(settings, {"provider": "deepseek", "model": "deepseek-v4-flash"})
    use_pool(runner, pool_with("sk-key-A", "sk-key-B"))
    recorder = Recorder(
        lambda candidate, n: FakeStatusError(400, "tools not supported")
        if candidate.target.provider == "openai"
        else FakeClient(candidate.target.provider)
    )
    model = runner.get_model(factory=recorder)

    assert recorder.providers == ["openai", "deepseek"], "400 应当直接换家，不在同家换 key"
    assert model.route.provider == "deepseek"
    assert model.route.model == "deepseek-v4-flash"


def test_503_switches_provider(settings, runner):
    """503 是这家整体挂了，换 key 治不好。"""
    write_chain(settings, {"provider": "deepseek", "model": "deepseek-v4-flash"})
    recorder = Recorder(
        lambda candidate, n: FakeStatusError(503, "unavailable")
        if candidate.target.provider == "openai"
        else FakeClient("deepseek")
    )
    runner.get_model(factory=recorder)
    assert recorder.providers == ["openai", "deepseek"]


# ── 4. 未知错误不切换 ───────────────────────────────────────────────────────


def test_unknown_error_aborts_instead_of_switching(settings, runner):
    """编程错误原样抛：重试只会把真 bug 掩盖成"所有 provider 都挂了"。"""
    write_chain(settings, {"provider": "deepseek", "model": "deepseek-v4-flash"})
    recorder = Recorder(lambda candidate, n: ValueError("bad body"))
    with pytest.raises(ValueError, match="bad body"):
        runner.get_model(factory=recorder)
    assert recorder.providers == ["openai"], "ABORT 之后不该再去试下一家"


# ── 5. 全挂 → 明确错误含整条链 ─────────────────────────────────────────────


def test_everything_failing_reports_the_whole_chain(settings, runner):
    write_chain(settings, {"provider": "deepseek", "model": "deepseek-v4-flash"})
    use_pool(runner, pool_with("sk-key-A", "sk-key-B"))
    recorder = Recorder(lambda candidate, n: FakeStatusError(429, "slow down"))

    with pytest.raises(fb.FallbackExhausted) as excinfo:
        runner.get_model(factory=recorder)
    message = str(excinfo.value)
    # 两站都要出现，且要带上原因（429），而不是一句"全部失败"。
    assert "openai/gpt-5.4" in message
    assert "deepseek/deepseek-v4-flash" in message
    assert "429" in message
    # 池里两把 + 主钥匙 + 回退站的 1 把 = 4 次尝试
    assert len(excinfo.value.attempts) == 4


# ── 6. 切换必须留日志 ──────────────────────────────────────────────────────


def test_switches_are_logged_with_from_to_and_reason(settings, runner, caplog):
    write_chain(settings, {"provider": "deepseek", "model": "deepseek-v4-flash"})
    use_pool(runner, pool_with("sk-key-A", "sk-key-B"))
    recorder = Recorder(
        lambda candidate, n: FakeStatusError(429, "rate limited")
        if candidate.key.label == "pool#0"
        else (
            FakeStatusError(400, "bad param")
            if candidate.target.provider == "openai"
            else FakeClient("deepseek")
        )
    )
    with caplog.at_level(logging.WARNING, logger="orbit.fallback"):
        runner.get_model(factory=recorder)

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "换该家的下一把 key" in text, text
    assert "openai/gpt-5.4 → deepseek/deepseek-v4-flash" in text, text
    assert "429" in text and "400" in text


# ── 7. 调用时（不是构造时）失败也能切换 ────────────────────────────────────


async def test_invoke_time_failure_switches_to_next_candidate(settings, runner):
    """401 只有**真调一次**才知道。只在构造时挑一次，等于回退链只在配置错时有用。"""
    use_pool(runner, pool_with("sk-key-A", "sk-key-B"))
    recorder = Recorder(
        lambda candidate, n: FakeClient(
            candidate.key.label,
            fail=FakeStatusError(401, "invalid api key") if n == 1 else None,
        )
    )
    model = runner.get_model(factory=recorder)
    assert model.route.key_label == "pool#0"

    answer = await model.ainvoke([{"role": "user", "content": "hi"}])
    assert answer == "answer-from-pool#1"
    assert model.route.key_label == "pool#1", "切换后 route 必须跟着更新"
    assert recorder.key_labels == ["pool#0", "pool#1"]


async def test_invoke_time_failure_on_last_candidate_raises_chain_error(settings, runner):
    write_chain(settings, {"provider": "deepseek", "model": "deepseek-v4-flash"})
    recorder = Recorder(
        lambda candidate, n: FakeClient(candidate.target.provider, fail=FakeStatusError(429))
    )
    model = runner.get_model(factory=recorder)
    with pytest.raises(fb.FallbackExhausted) as excinfo:
        await model.ainvoke([{"role": "user", "content": "hi"}])
    assert "deepseek/deepseek-v4-flash" in str(excinfo.value)


async def test_bind_tools_survives_a_switch(settings, runner):
    """`nodes.py` 先 `bind_tools` 再调用；换站后新客户端必须重新绑一次工具。"""
    use_pool(runner, pool_with("sk-key-A", "sk-key-B"))
    bound: list[str] = []

    class Bound(FakeClient):
        def bind_tools(self, tools, **kwargs):  # noqa: ANN001, ANN201
            bound.append(self.label)
            return self

    recorder = Recorder(
        lambda candidate, n: Bound(
            candidate.key.label,
            fail=FakeStatusError(429) if n == 1 else None,
        )
    )
    model = runner.get_model(factory=recorder).bind_tools([{"name": "noop"}])
    assert bound == ["pool#0"]
    answer = await model.ainvoke([{"role": "user", "content": "hi"}])
    assert answer == "answer-from-pool#1"
    assert bound == ["pool#0", "pool#1"], "换站后必须对新客户端重新 bind_tools"


# ── 8. 既有行为不许变 ──────────────────────────────────────────────────────


def test_injected_model_is_returned_unchanged(settings):
    """测试替身 / 外部覆盖的模型原样返回：不包一层、不卷入回退逻辑。"""
    fake = FakeClient("injected")
    store = Store(settings.data_dir / "app.sqlite")
    try:
        real = AgentRunner(settings, store, ToolRegistry(settings, store), model=fake)
        assert real.get_model() is fake
    finally:
        asyncio.run(store.engine.dispose())


def test_missing_model_still_raises_value_error(tmp_path):
    """没配模型时的报错口径不变（`agent.run` 依赖它给用户一句能看懂的话）。"""
    # 显式传空值：`Settings` 会读 `backend/.env` 里的 `ORBIT_MODEL`，
    # 不显式清空就根本测不到"没配模型"这条路径。
    settings = Settings(
        model="",
        model_provider="",
        model_api_key=SecretStr(""),
        data_dir=tmp_path / "data",
        workspace=tmp_path / "files",
    )
    settings.prepare()
    store = Store(settings.data_dir / "app.sqlite")
    try:
        runner = AgentRunner(settings, store, ToolRegistry(settings, store))
        with pytest.raises(ValueError, match="模型尚未配置"):
            runner.get_model()
    finally:
        asyncio.run(store.engine.dispose())


def test_route_is_visible_to_the_caller(settings, runner):
    """"这次实际用的是谁"必须能从返回值读到，而不是只能去翻日志。"""
    recorder = Recorder(lambda candidate, n: FakeClient("primary"))
    model = runner.get_model(factory=recorder)
    assert isinstance(model.route, Route)
    payload = model.route.to_dict()
    assert payload["provider"] == "openai"
    assert payload["switched"] is False
    assert runner.last_route is not None and runner.last_route.provider == "openai"
    assert runner.last_attempts and runner.last_attempts[-1].status == "ok"
