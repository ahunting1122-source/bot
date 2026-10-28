"""记忆供应商的契约测试（`app/memory_providers.py`）。

锁住的五件事：

1. 四个**真实现**（local / file / jsonl / holographic）的 save → search → list_keys →
   delete 往返；
2. 其余十一个**只有接口**的 provider 一律 `available() is False`，且 `describe()` 给出
   安装提示与"为什么不可用"——"不许假装支持"这条约定必须被测试钉住，否则下一次重构
   很容易顺手让它返回 True；
3. **Hermes 名单的 8 个 provider 一个不少**（矩阵 9.10 的判据）；
4. 路由器的三条语义：按优先级选第一个可用、读取第一个**命中**、写按 fanout 多写；
5. `orbit memory providers` 真的能用。
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.main import migrate
from app.memory_providers import (
    BUILTIN_NAMES,
    HRR_MIN_SCORE,
    PROVIDER_NAMES,
    FileProvider,
    HolographicProvider,
    JsonlProvider,
    LocalProvider,
    Mem0Provider,
    MemoryRouter,
    ProviderUnavailable,
    build_provider,
    build_router,
    default_providers,
    load_config,
    save_config,
)
from app.storage import Store

#: Hermes `plugins/memory/` 的 8 个 provider 目录（矩阵 9.10 的名单）。
HERMES_NAMES = (
    "byterover",
    "hindsight",
    "holographic",
    "honcho",
    "mem0",
    "openviking",
    "retaindb",
    "supermemory",
)

#: 本波次新增的、**只有接口**的 6 个（`holographic` 是真实现，`mem0` 本来就有）。
NEW_EXTERNAL_NAMES = ("byterover", "hindsight", "honcho", "openviking", "retaindb", "supermemory")

#: 全部"只有接口"的 provider：6 个新补的 + 5 个既有的社区后端。
EXTERNAL_NAMES = (
    *NEW_EXTERNAL_NAMES,
    "mem0",
    "zep",
    "letta",
    "chroma",
    "pgvector",
)


def _store(tmp_path):
    """建一个跑过迁移的真库（`memories` 表存在）。"""
    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    return settings, Store(settings.data_dir / "app.sqlite")


# ── 三个真实现：往返 ─────────────────────────────────────────────────────────


async def test_local_provider_round_trip(tmp_path):
    settings, store = _store(tmp_path)
    provider = LocalProvider(store)

    assert provider.available() is True
    saved = await provider.save("偏好/报告格式", "表格 + 结论先行")
    assert saved["saved"] is True and saved["provider"] == "local"

    hits = await provider.search("报告")
    assert [hit["key"] for hit in hits] == ["偏好/报告格式"]
    assert hits[0]["value"] == "表格 + 结论先行"

    assert await provider.list_keys() == ["偏好/报告格式"]
    assert await provider.delete("偏好/报告格式") is True
    assert await provider.list_keys() == []
    # 再删一次要老实返回 False，而不是静默成功
    assert await provider.delete("偏好/报告格式") is False


async def test_file_provider_round_trip(tmp_path):
    provider = FileProvider(tmp_path / "data")
    assert provider.available() is True

    saved = await provider.save("项目/Orbit/存储", "SQLite + aiosqlite")
    assert saved["saved"] is True
    # 一条记忆一个文件，首行是 `# key`（list_keys 的真值来源）
    assert saved["path"].endswith(".md")
    assert (tmp_path / "data" / "memory").is_dir()

    hits = await provider.search("aiosqlite")
    assert [hit["key"] for hit in hits] == ["项目/Orbit/存储"]
    assert await provider.list_keys() == ["项目/Orbit/存储"]

    assert await provider.delete("项目/Orbit/存储") is True
    assert await provider.list_keys() == []
    assert await provider.search("aiosqlite") == []


async def test_file_provider_does_not_collide_on_similar_keys(tmp_path):
    """两个只差一个字的中文 key 不能落到同一个文件上（slug 会退化，靠哈希兜底）。"""
    provider = FileProvider(tmp_path / "data")
    await provider.save("默认语言", "中文")
    await provider.save("默认语音", "女声")
    assert sorted(await provider.list_keys()) == ["默认语言", "默认语音"]


async def test_jsonl_provider_round_trip_and_audit(tmp_path):
    provider = JsonlProvider(tmp_path / "data")
    assert provider.available() is True

    await provider.save("k1", "v1")
    await provider.save("k1", "v2")  # 追加，不是覆盖
    hits = await provider.search("v2")
    assert hits == [{"key": "k1", "value": "v2", "provider": "jsonl"}]
    assert await provider.list_keys() == ["k1"]

    # 审计：两次写入都留在日志里（这正是它存在的理由）
    assert [record["value"] for record in provider.history("k1")] == ["v1", "v2"]

    assert await provider.delete("k1") is True
    assert await provider.list_keys() == []
    assert await provider.delete("k1") is False
    assert [record["op"] for record in provider.history("k1")] == ["save", "save", "delete"]


async def test_jsonl_provider_survives_a_corrupt_line(tmp_path):
    """追加日志的容错优势：一行坏了不该让整个库读不出来。"""
    provider = JsonlProvider(tmp_path / "data")
    await provider.save("k1", "v1")
    path = tmp_path / "data" / "memory" / "memory.jsonl"
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("{ 这不是 JSON\n")
    assert await provider.list_keys() == ["k1"]


# ── 真实现 4：全息（HRR）─────────────────────────────────────────────────────


async def test_holographic_provider_round_trip(tmp_path):
    """`holographic` 是 Hermes 名单里唯一能离线真跑的：必须真的存取往返。"""
    provider = HolographicProvider(tmp_path / "data")
    assert provider.available() is True

    saved = await provider.save("偏好/报告格式", "表格 + 结论先行")
    assert saved["saved"] is True and saved["provider"] == "holographic"
    assert saved["path"].endswith("holographic.json")

    hits = await provider.search("报告")
    assert [hit["key"] for hit in hits] == ["偏好/报告格式"]
    assert hits[0]["value"] == "表格 + 结论先行"
    # 三路分量一起回来：只报总分，用户没法回答"为什么这条排第一"
    for key in ("score", "fts", "jaccard", "hrr"):
        assert key in hits[0]

    assert await provider.list_keys() == ["偏好/报告格式"]
    assert await provider.delete("偏好/报告格式") is True
    assert await provider.list_keys() == []
    assert await provider.delete("偏好/报告格式") is False


async def test_holographic_provider_filters_noise(tmp_path):
    """无关查询不能返回命中：否则路由器里"第一个 available 的永远有命中"。"""
    provider = HolographicProvider(tmp_path / "data")
    await provider.save("项目/Orbit", "SQLite + aiosqlite 本地存储")
    assert await provider.search("完全无关的东西 zzz") == []
    # 阈值是"高于 HRR 噪声（~0.003）、低于最弱真实命中（≥0.2）"
    assert 0.0 < HRR_MIN_SCORE < 0.2


def test_holographic_provider_needs_data_dir():
    provider = build_provider("holographic")
    assert provider.available() is False
    assert "data_dir" in provider.describe()["reason"]


# ── 名单：Hermes 8 个一个不少（矩阵 9.10）────────────────────────────────────


def test_every_hermes_provider_is_registered():
    missing = [name for name in HERMES_NAMES if name not in PROVIDER_NAMES]
    assert not missing, f"缺 Hermes 名单里的 provider：{missing}"


def test_provider_list_keeps_the_local_ecosystem():
    """Orbit 原有的本地生态（file/jsonl/pgvector 等）不能被"对齐 Hermes"顶掉。"""
    for name in ("local", "file", "jsonl", "pgvector", "chroma"):
        assert name in PROVIDER_NAMES
    assert len(PROVIDER_NAMES) == 15
    assert set(BUILTIN_NAMES) == {"local", "file", "jsonl", "holographic"}
    assert set(PROVIDER_NAMES) - set(BUILTIN_NAMES) == set(EXTERNAL_NAMES)


@pytest.mark.parametrize("name", EXTERNAL_NAMES)
def test_unimplemented_provider_is_never_available(name):
    """未接线的 provider 必须**永远** available() is False。

    即使某天有人把 pip 包装上了，只要 Orbit 没接最小路径，它就得是 False——
    否则路由器会选中一个一用就抛的 provider（那等于把"没实现"伪装成"运行时故障"）。
    """
    provider = build_provider(name)
    assert provider.available() is False
    info = provider.describe()
    assert info["available"] is False
    assert info["install"], f"{name} 必须给出安装提示"
    assert info["reason"], f"{name} 必须说明为什么不可用"
    assert info["note"]


@pytest.mark.parametrize("name", EXTERNAL_NAMES)
async def test_unimplemented_provider_refuses_with_reason(name):
    provider = build_provider(name)
    with pytest.raises(ProviderUnavailable) as failure:
        await provider.search("随便什么")
    assert name in str(failure.value)
    assert failure.value.install
    # 也是 NotImplementedError：调用方只兜这一大类也能拦住
    with pytest.raises(NotImplementedError):
        await provider.save("k", "v")
    with pytest.raises(NotImplementedError):
        await provider.delete("k")
    with pytest.raises(NotImplementedError):
        await provider.list_keys()


def test_mem0_describe_reports_dependency_probe():
    info = Mem0Provider().describe()
    assert info["kind"] == "external"
    assert info["dependency_installed"] is False  # 本机没装 mem0ai
    assert "mem0ai" in info["install"]


@pytest.mark.parametrize("name", NEW_EXTERNAL_NAMES)
def test_new_hermes_providers_explain_what_is_missing(name):
    """新补的 6 个必须说清"缺哪个包 / 缺哪个凭证"，而不是一句"未实现"。"""
    info = build_provider(name).describe()
    assert info["available"] is False
    assert info["install"] and "pip install" in info["install"]
    assert info["requires"], f"{name} 必须列出需要的凭证/端点"
    assert any("API_KEY" in item or "BASE_URL" in item for item in info["requires"])
    assert "网络" in info["reason"]


def test_unknown_provider_name_is_rejected():
    with pytest.raises(ValueError, match="未知的记忆供应商"):
        build_provider("nope")


# ── 路由器 ───────────────────────────────────────────────────────────────────


class _FakeProvider:
    """最小 provider：能控可用性、命中与失败。"""

    def __init__(self, name, *, available=True, hits=None, fail=False):
        self.name = name
        self._available = available
        self._hits = hits or []
        self.fail = fail
        self.saved = []
        self.deleted = []

    def available(self):
        return self._available

    async def search(self, query, limit=10):
        return list(self._hits)

    async def save(self, key, value):
        if self.fail:
            raise ProviderUnavailable(self.name, "save", "假失败")
        self.saved.append((key, value))
        return {"key": key, "saved": True, "provider": self.name}

    async def delete(self, key):
        if self.fail:
            raise ProviderUnavailable(self.name, "delete", "假失败")
        self.deleted.append(key)
        return True

    async def list_keys(self):
        return [key for key, _ in self.saved]

    def describe(self):
        return {"name": self.name, "available": self.available(), "kind": "fake"}


def test_router_picks_first_available_by_priority():
    unavailable = _FakeProvider("a", available=False)
    first = _FakeProvider("b")
    second = _FakeProvider("c")
    router = MemoryRouter([unavailable, first, second])
    assert router.active() is first
    assert [provider.name for provider in router.available()] == ["b", "c"]
    assert [row["active"] for row in router.describe()] == [False, True, False]


async def test_router_read_takes_first_provider_with_a_hit():
    """第一个可用但没命中的 provider 不能让检索就此结束。"""
    empty = _FakeProvider("empty", hits=[])
    later = _FakeProvider("later", hits=[{"key": "k", "value": "v"}])
    router = MemoryRouter([empty, later])
    result = await router.search("q")
    assert result["provider"] == "later"
    assert result["hits"][0]["provider"] == "later"


async def test_router_read_returns_empty_when_nobody_hits():
    router = MemoryRouter([_FakeProvider("a"), _FakeProvider("b")])
    assert await router.search("q") == {"provider": "", "hits": []}


async def test_router_fanout_writes_every_available_provider():
    one = _FakeProvider("one")
    two = _FakeProvider("two")
    dead = _FakeProvider("dead", available=False)
    router = MemoryRouter([one, dead, two], fanout=True)
    result = await router.save("k", "v")
    assert result["ok"] is True
    assert result["written"] == ["one", "two"]
    assert one.saved == [("k", "v")] and two.saved == [("k", "v")]


async def test_router_without_fanout_writes_only_the_first():
    one = _FakeProvider("one")
    two = _FakeProvider("two")
    router = MemoryRouter([one, two], fanout=False)
    result = await router.save("k", "v")
    assert result["written"] == ["one"]
    assert two.saved == []


async def test_router_fanout_records_errors_without_aborting():
    """一个后端失败不能让另外几个也不写——多写的意义就是"总有备份"。"""
    bad = _FakeProvider("bad", fail=True)
    good = _FakeProvider("good")
    router = MemoryRouter([bad, good], fanout=True)
    result = await router.save("k", "v")
    assert result["ok"] is True
    assert result["written"] == ["good"]
    assert "bad" in result["errors"]


async def test_router_delete_and_list_keys(tmp_path):
    _, store = _store(tmp_path)
    local = LocalProvider(store)
    router = MemoryRouter([local])
    await router.save("k1", "v1")
    assert await router.list_keys() == ["k1"]
    result = await router.delete("k1")
    assert result["deleted"] == ["local"]
    assert await router.list_keys() == []


# ── 配置与装配 ───────────────────────────────────────────────────────────────


def test_default_providers_is_all_eight(tmp_path):
    providers = default_providers(data_dir=tmp_path / "data")
    assert [provider.name for provider in providers] == list(PROVIDER_NAMES)


def test_router_config_round_trip(tmp_path):
    data_dir = tmp_path / "data"
    assert load_config(data_dir) == {"priority": list(BUILTIN_NAMES), "fanout": True}
    save_config(data_dir, priority=["jsonl", "local"], fanout=False)
    assert load_config(data_dir) == {"priority": ["jsonl", "local"], "fanout": False}
    # 未知名字不许写进去（否则路由器会静默忽略一个拼错的配置）
    with pytest.raises(ValueError, match="未知的记忆供应商"):
        save_config(data_dir, priority=["local", "nope"])


def test_router_config_order_wins(tmp_path):
    data_dir = tmp_path / "data"
    save_config(data_dir, priority=["jsonl", "file", "local"])
    router = build_router(data_dir=data_dir)
    assert [provider.name for provider in router.providers][:3] == ["jsonl", "file", "local"]
    # 没写进配置的 provider 排在后面，且不丢
    assert len(router.providers) == len(PROVIDER_NAMES) == 15


def test_corrupt_router_config_falls_back_to_default(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "memory").mkdir(parents=True)
    (data_dir / "memory" / "providers.json").write_text("{ 坏掉的 JSON", encoding="utf-8")
    assert load_config(data_dir) == {"priority": list(BUILTIN_NAMES), "fanout": True}


def test_build_router_with_store_marks_local_available(tmp_path):
    settings, store = _store(tmp_path)
    router = build_router(settings=settings, store=store)
    status = router.status()
    assert status["active"] == "local"
    # 四个真实现都可用（`holographic` 是纯本地算法，只需要 data_dir）
    assert status["available"] == ["local", "file", "jsonl", "holographic"]


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_memory_providers(tmp_path, capsys):
    import app.cli.commands.memory_extra  # noqa: F401 —— 接线（把命令挂到 memory 上）
    from app.cli.main import main

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    code = main(["--data-dir", str(settings.data_dir), "memory", "providers"])
    out = capsys.readouterr().out
    assert code == 0
    for name in PROVIDER_NAMES:
        assert name in out
    assert f"可用 {len(BUILTIN_NAMES)}/{len(PROVIDER_NAMES)}" in out
    assert "mem0ai" in out  # 安装提示必须真的打出来


def test_cli_memory_status_and_off(tmp_path, capsys):
    import app.cli.commands.memory_extra  # noqa: F401
    from app.cli.main import main

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    assert main(["--data-dir", str(settings.data_dir), "memory", "status"]) == 0
    assert "记忆预算" in capsys.readouterr().out
    assert main(["--data-dir", str(settings.data_dir), "memory", "off"]) == 0
    assert "已关闭外部供应商" in capsys.readouterr().out
    assert load_config(settings.data_dir)["priority"] == list(BUILTIN_NAMES)
