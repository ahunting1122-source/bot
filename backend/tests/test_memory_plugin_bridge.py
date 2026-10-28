"""插件记忆 provider 的**读 / 写 / 记账**三条硬要求（MATRIX 13.4）。

## 为什么是「三条」而不是「一条」

`app/plugin_capabilities.py` 之前把 `register_memory_provider` 列为 **exposed**：
插件能注册一个 provider 对象，但**没有任何生产代码去读它或写它**。
更要紧的是 `app/memory_providers.py::build_router()` **零生产调用方**——
那套「15 个 provider + 路由器」的抽象从来没被装配过。

监视子agent 给的判据很硬，**缺一条就只能算「一半」，而一半比没有更糟**：

1. **写路径**要真的写进插件 provider（在 `MEMORY.md` 过完三段闸**之后**）；
2. **读路径**要真的查它——**只写不读是半个功能，比没有更糟**
   （用户以为记住了，模型永远看不到）；
3. **失败要隔离，但要记账**——静默吞掉失败会让「记忆坏了」变成不可诊断。

## 顺序不能反

**`MEMORY.md` 是权威，provider 是镜像。** 先写权威（过闸）→ 成功之后才镜像 →
**镜像失败只记账，不回滚**。反过来等于让一个可选插件能阻止核心记忆写入。
"""

from __future__ import annotations

import pytest


class _Provider:
    """最小的插件 provider（`MemoryProvider` 的契约：save / search / name）。"""

    def __init__(self, name: str = "plug", *, save_error: str = "", search_error: str = ""):
        self.name = name
        self.saved: list[tuple[str, str]] = []
        self.queries: list[str] = []
        self._save_error = save_error
        self._search_error = search_error

    def save(self, key, value):
        if self._save_error:
            raise RuntimeError(self._save_error)
        self.saved.append((key, value))

    def search(self, query, limit=5):
        self.queries.append(query)
        if self._search_error:
            raise RuntimeError(self._search_error)
        return [f"{self.name} 的记忆片段"]


@pytest.fixture()
def bridge():
    from app import memory_plugin_bridge

    memory_plugin_bridge.reset()
    yield memory_plugin_bridge
    memory_plugin_bridge.reset()


def test_write_path_really_writes_to_the_plugin_provider(bridge):
    """**要求 ①**：`mirror()` 必须真的调 provider 的 `save()`。"""
    provider = _Provider()
    bridge.configure(object(), plugin_providers=[provider])

    bridge.mirror("favorite_fruit", "用户在做水果拟人短剧")

    assert provider.saved == [("favorite_fruit", "用户在做水果拟人短剧")]
    assert bridge.STATS["write_ok"] == 1


def test_read_path_really_queries_the_plugin_provider(bridge):
    """**要求 ②**：只写不读是半个功能，**比没有更糟**。

    这条断言的是「检索结果真的进了可注入的文本」，不是「方法被调用了」。
    """
    provider = _Provider()
    bridge.configure(object(), plugin_providers=[provider])

    text = bridge.recall("水果短剧")

    assert provider.queries == ["水果短剧"], "没拿任务目标去查插件 provider"
    assert "plug 的记忆片段" in text, f"检索结果没进注入文本：{text!r}"
    assert bridge.STATS["read_ok"] == 1


def test_write_failure_is_isolated_and_accounted(bridge):
    """**要求 ③**：一个 provider 炸了，不影响其它，但**必须记账**。"""
    broken = _Provider("broken", save_error="磁盘满了")
    healthy = _Provider("healthy")
    bridge.configure(object(), plugin_providers=[broken, healthy])

    bridge.mirror("k", "v")  # 不该抛

    assert healthy.saved == [("k", "v")], "一个 provider 炸了把另一个也带崩了"
    assert bridge.STATS["write_failed"] == 1
    assert bridge.STATS["write_ok"] == 1
    assert "磁盘满了" in bridge.STATS["last_write_error"], (
        f"失败原因没记账：{bridge.STATS['last_write_error']!r}"
    )


def test_read_failure_is_isolated_and_accounted(bridge):
    """读侧同样：一个 provider 检索失败，别的照常返回，失败要记账。"""
    broken = _Provider("broken", search_error="连接超时")
    healthy = _Provider("healthy")
    bridge.configure(object(), plugin_providers=[broken, healthy])

    text = bridge.recall("q")  # 不该抛

    assert "healthy 的记忆片段" in text
    assert bridge.STATS["read_failed"] == 1
    assert "连接超时" in bridge.STATS["last_read_error"]


def test_no_provider_configured_is_a_silent_no_op(bridge):
    """没配插件 provider 时，读写都是零开销的空操作——**不能报错**。

    绝大多数部署没有记忆插件，这条路径必须是完全透明的。
    """
    assert bridge.recall("q") == ""
    bridge.mirror("k", "v")  # 不该抛
    assert bridge.STATS["write_ok"] == 0
    assert bridge.STATS["write_failed"] == 0


def test_plugin_providers_must_be_declared_not_guessed(bridge):
    """**反证**：只装路由器、不声明插件 provider → 一个都不读不写。

    早先想用 `getattr(provider, "plugin", False)` 自动识别，但插件 provider 没有
    这个属性——**那个过滤条件会让所有插件 provider 都被排除，功能静默失效**。
    所以现在必须由装配层显式声明。这条钉住「不许靠猜」。
    """
    provider = _Provider()
    bridge.configure(object())  # 只给 router，不给 plugin_providers

    bridge.mirror("k", "v")
    assert provider.saved == []
    assert bridge.recall("q") == ""


def test_the_write_path_is_wired_after_the_gate():
    """**防回归**：`memory_save` 必须在 `store.set_memory` **之后**才镜像。

    顺序反了（先镜像后写权威）等于让一个可选插件能阻止核心记忆写入。
    这条检查的是**源码里两行的先后**，因为那个顺序是契约，不是实现细节。
    """
    import pathlib

    from app import tools as tools_module

    source = pathlib.Path(tools_module.__file__).read_text(encoding="utf-8")
    set_memory_at = source.find("await self.store.set_memory(")
    mirror_at = source.find("memory_plugin_bridge.mirror(")
    assert set_memory_at != -1, "找不到 `store.set_memory`——测试失效了"
    assert mirror_at != -1, "`memory_save` 不再镜像到插件 provider 了"
    assert set_memory_at < mirror_at, (
        "镜像是写在 `store.set_memory` **之前**的——顺序反了："
        "`MEMORY.md` 是权威，必须先写成功再镜像"
    )
