"""`app/fallback.py` 的契约测试。

两个判据必须钉死，因为它们是"容错"和"掩盖 bug"的分界线：

1. **429 / 529 / 503 / 连接失败 → 切换**；
2. **400 / 401 及其它 → 立刻抛**（换 provider 治不好写错的 key，只会把真 bug
   掩盖成"换个 provider 试试"）。

另外钉住凭证池的三件事：轮转、耗尽标记、`reset` 恢复。全部用注入的 fake，
**不发任何真实网络请求**，也不碰系统钥匙串。
"""

from __future__ import annotations

import httpx
import pytest

from app import fallback as fb

# ── fake ────────────────────────────────────────────────────────────────────


class FakeStatusError(Exception):
    """带状态码的假异常。形状对齐 httpx / openai 两类真实异常。"""

    def __init__(self, status_code: int, message: str = "") -> None:
        super().__init__(message or f"status {status_code}")
        self.status_code = status_code


class FakeModel:
    """可 `await .ainvoke(...)` 的假客户端。`error` 非空时抛出它。"""

    def __init__(self, label: str, error: BaseException | None = None) -> None:
        self.label = label
        self.error = error
        self.calls = 0

    async def ainvoke(self, messages, **kwargs):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return f"answer-from-{self.label}"


def _chain(errors: list[BaseException | None], names: list[str] | None = None):
    """按 errors 造一条链；返回 (chain, 造出来的模型列表)。"""
    names = names or [f"p{index}" for index in range(len(errors))]
    models: dict[str, FakeModel] = {}
    targets = [fb.FallbackTarget(provider=name, model=f"{name}-model") for name in names]
    for name, error in zip(names, errors, strict=True):
        models[name] = FakeModel(name, error)

    def factory(target: fb.FallbackTarget) -> FakeModel:
        return models[target.provider]

    return fb.FallbackChain(targets, factory), models


# ── 触发条件（白名单）───────────────────────────────────────────────────────


def test_status_code_of_reads_three_exception_shapes():
    assert fb.status_code_of(FakeStatusError(429)) == 429
    response = httpx.Response(503, request=httpx.Request("POST", "https://x.invalid"))
    assert fb.status_code_of(httpx.HTTPStatusError("boom", request=response.request, response=response)) == 503
    assert fb.status_code_of(ValueError("no code")) is None


@pytest.mark.parametrize("code", [429, 529, 503])
def test_rate_limit_overload_unavailable_trigger_failover(code):
    chain, models = _chain([FakeStatusError(code), None])
    assert chain.ainvoke  # 只是证明对象建起来了
    result = _run(chain)
    assert result == "answer-from-p1"
    assert [item.status for item in chain.attempts] == ["failover", "ok"]
    assert models["p0"].calls == 1 and models["p1"].calls == 1


def test_connection_error_triggers_failover():
    chain, _ = _chain([httpx.ConnectError("connection refused"), None])
    assert _run(chain) == "answer-from-p1"
    assert chain.attempts[0].status == "failover"


@pytest.mark.parametrize("code", [400, 401, 403, 404, 500])
def test_other_statuses_do_not_switch(code):
    """非白名单状态码必须立刻抛，且保留原始异常类型。

    500 也在内：它是"服务端 bug"，换 provider 会掩盖成随机失败；Hermes 的
    fallback 白名单里同样只有 429/529/503。
    """
    chain, models = _chain([FakeStatusError(code), None])
    with pytest.raises(FakeStatusError) as excinfo:
        _run(chain)
    assert excinfo.value.status_code == code
    assert models["p1"].calls == 0, "不该尝试下一站"
    assert [item.status for item in chain.attempts] == ["fatal"]


def test_value_error_is_not_retried():
    chain, _ = _chain([ValueError("bad request body"), None])
    with pytest.raises(ValueError):
        _run(chain)
    assert chain.attempts[0].status == "fatal"


def test_all_targets_failing_raises_with_every_reason():
    chain, _ = _chain([FakeStatusError(429, "slow down"), FakeStatusError(503, "down")])
    with pytest.raises(fb.FallbackExhausted) as excinfo:
        _run(chain)
    message = str(excinfo.value)
    assert "429" in message and "503" in message
    assert "p0" in message and "p1" in message
    assert len(excinfo.value.attempts) == 2


def test_empty_chain_is_a_programming_error():
    chain = fb.FallbackChain([], lambda target: FakeModel("x"))
    with pytest.raises(ValueError) as excinfo:
        _run(chain)
    assert "回退链为空" in str(excinfo.value)


def test_is_failover_error_matrix():
    assert fb.is_failover_error(FakeStatusError(429)) is True
    assert fb.is_failover_error(FakeStatusError(529)) is True
    assert fb.is_failover_error(FakeStatusError(503)) is True
    assert fb.is_failover_error(httpx.ConnectTimeout("timeout")) is True
    assert fb.is_failover_error(FakeStatusError(401)) is False
    assert fb.is_failover_error(FakeStatusError(400)) is False
    assert fb.is_failover_error(ValueError("nope")) is False


def test_attempts_are_reported_through_the_hook():
    seen: list[fb.Attempt] = []
    chain, _ = _chain([FakeStatusError(429), None])
    chain._on_event = seen.append  # noqa: SLF001 —— 这里就是要验观测钩子
    _run(chain)
    assert [item.status for item in seen] == ["failover", "ok"]


# ── 链的序列化（settings.json 里的形状）─────────────────────────────────────


def test_targets_round_trip_through_config():
    targets = [
        fb.FallbackTarget(provider="deepseek", model="deepseek-v4-flash"),
        fb.FallbackTarget(
            provider="custom",
            model="qwen2.5",
            base_url="http://127.0.0.1:8000/v1",
            key_env="MY_KEY",
        ),
    ]
    config = fb.targets_to_config(targets)
    assert config[0] == {"provider": "deepseek", "model": "deepseek-v4-flash"}
    assert config[1]["base_url"] == "http://127.0.0.1:8000/v1"
    assert fb.targets_from_config(config) == targets


def test_bad_config_entries_are_dropped_not_fatal():
    """settings.json 被手改坏时，坏条目该被丢掉，而不是让整条命令失败。"""
    raw = [{"provider": "deepseek"}, "junk", {"model": "no-provider"}, {"provider": ""}, None]
    assert fb.targets_from_config(raw) == [fb.FallbackTarget(provider="deepseek")]
    assert fb.targets_from_config("not-a-list") == []


# ── 凭证池 ──────────────────────────────────────────────────────────────────


def _pool() -> tuple[fb.CredentialPool, fb.MemoryStore]:
    store = fb.MemoryStore()
    return fb.CredentialPool(store), store


def test_pool_add_and_list_keeps_metadata_in_order():
    pool, _ = _pool()
    pool.add("deepseek", "sk-first-0000000000")
    pool.add("deepseek", "sk-second-1111111111", label="backup")
    entries = pool.entries("deepseek")
    assert [item.index for item in entries] == [0, 1]
    assert entries[1].label == "backup"
    assert entries[0].fingerprint.endswith("0000")
    assert pool.known_providers() == ["deepseek"]


def test_pool_never_stores_plaintext_in_metadata():
    """元数据里不能有明文：它会进 `auth list`、JSON 输出、日志。"""
    pool, store = _pool()
    pool.add("deepseek", "sk-very-secret-value-9999")
    meta = store.get("pool-meta:deepseek") or ""
    assert "sk-very-secret-value-9999" not in meta
    assert "9999" in meta  # 只留末 4 位指纹
    lease = pool.acquire("deepseek")
    assert "sk-very-secret-value-9999" not in repr(lease)


def test_pool_rotates_between_keys():
    pool, _ = _pool()
    pool.add("deepseek", "sk-aaaa-0000000001")
    pool.add("deepseek", "sk-bbbb-0000000002")
    indices = [pool.acquire("deepseek").credential.index for _ in range(4)]
    # 均匀轮转：否则第一把会先把配额烧光，池退化成单 key。
    assert indices == [0, 1, 0, 1]


def test_exhausted_key_is_skipped_until_reset():
    pool, _ = _pool()
    pool.add("deepseek", "sk-aaaa-0000000001")
    pool.add("deepseek", "sk-bbbb-0000000002")
    lease = pool.acquire("deepseek")
    pool.report_failure("deepseek", lease.credential.index, message="429")
    # 接下来只能拿到另一把（被标记的那把被跳过）。
    for _ in range(3):
        assert pool.acquire("deepseek").credential.index == 1
    assert pool.entries("deepseek")[0].last_status == "exhausted"

    cleared = pool.reset("deepseek")
    assert cleared == 1
    assert {pool.acquire("deepseek").credential.index for _ in range(4)} == {0, 1}


def test_pool_raises_when_everything_exhausted():
    pool, _ = _pool()
    pool.add("deepseek", "sk-aaaa-0000000001")
    pool.report_failure("deepseek", 0)
    with pytest.raises(fb.CredentialPoolExhausted) as excinfo:
        pool.acquire("deepseek")
    message = str(excinfo.value)
    assert "orbit auth reset deepseek" in message


def test_missing_secret_is_marked_and_skipped():
    """keyring 里被外部删掉的 key：标成 missing 并换下一把，不能死循环重试空槽。"""
    pool, store = _pool()
    pool.add("deepseek", "sk-aaaa-0000000001")
    pool.add("deepseek", "sk-bbbb-0000000002")
    store.delete("pool:deepseek:0")
    lease = pool.acquire("deepseek")
    assert lease.credential.index == 1
    assert pool.entries("deepseek")[0].last_status == "missing"


def test_report_success_clears_exhaustion_and_counts():
    pool, _ = _pool()
    pool.add("deepseek", "sk-aaaa-0000000001")
    pool.report_failure("deepseek", 0, message="429")
    updated = pool.report_success("deepseek", 0)
    assert updated is not None
    assert updated.last_status == ""
    assert updated.request_count == 1


def test_pool_remove_by_index_and_label():
    pool, store = _pool()
    pool.add("deepseek", "sk-aaaa-0000000001")
    pool.add("deepseek", "sk-bbbb-0000000002", label="backup")
    assert pool.remove("deepseek", "backup") is not None
    assert store.get("pool:deepseek:1") is None
    assert [item.index for item in pool.entries("deepseek")] == [0]
    assert pool.remove("deepseek", 0) is not None
    assert pool.entries("deepseek") == []
    assert pool.known_providers() == []
    assert pool.remove("deepseek", 99) is None


def test_pool_clear_removes_keys_and_roster():
    pool, store = _pool()
    pool.add("deepseek", "sk-aaaa-0000000001")
    pool.add("openai", "sk-bbbb-0000000002")
    assert pool.clear("deepseek") == 1
    assert store.get("pool:deepseek:0") is None
    assert pool.known_providers() == ["openai"]


def test_pool_rejects_empty_secret():
    pool, _ = _pool()
    with pytest.raises(ValueError):
        pool.add("deepseek", "   ")


def test_fingerprint_never_exposes_short_secrets():
    assert fb.fingerprint("abc") == "…***"
    assert fb.fingerprint("") == "…***"
    assert fb.fingerprint("sk-1234567890") == "…7890"


# ── 一个"注入 fake transport"的端到端：HTTP 429 从真实客户端一路触发切换 ────


@pytest.mark.asyncio
async def test_httpx_429_from_lightweight_client_triggers_chain_failover():
    """把 providers 的轻量客户端与 fallback 链接起来跑一遍（仍然零网络）。"""
    from app import providers as pv

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": "rate limited"})

    anthropic = pv.AnthropicMessagesClient(
        base_url="https://api.anthropic.com",
        api_key="k",
        model="claude-sonnet-5",
        transport=httpx.MockTransport(handler),
    )
    backup = FakeModel("backup")

    def factory(target: fb.FallbackTarget):
        return anthropic if target.provider == "anthropic" else backup

    chain = fb.FallbackChain(
        [
            fb.FallbackTarget(provider="anthropic", model="claude-sonnet-5"),
            fb.FallbackTarget(provider="deepseek", model="deepseek-v4-flash"),
        ],
        factory,
    )
    result = await chain.ainvoke([{"role": "user", "content": "hi"}])
    assert result == "answer-from-backup"
    assert [item.status for item in chain.attempts] == ["failover", "ok"]


def _run(chain: fb.FallbackChain):
    """把 async 的 `ainvoke` 跑成同步，便于纯函数式断言。

    用 `asyncio.run` 而不是 pytest 的 async 插件：这里测的是"按序切换"的纯逻辑，
    每个用例一条独立事件循环最干净，也不会把 fake 的状态串到下一个用例。
    """
    import asyncio

    return asyncio.run(chain.ainvoke([{"role": "user", "content": "hi"}]))
