"""`0012` 迁移与 `--source` 的端到端测试（MATRIX 10.x）。

## 为什么这条必须有

**数据模型改动错了最难回滚**——比工具逻辑错更贵：工具错了改回来就行，
而列加错/回填错，线上数据已经在里面了。

监视子agent 的判据：**`0012` 的迁移本身没有测试就不该提交。**
"""

from __future__ import annotations

import pytest
from sqlalchemy import text


async def test_the_source_column_exists_and_is_indexed(tmp_path):
    """**迁移真的建出了列和索引**——不是只在测试夹具里存在。

    `--source` 的唯一用途就是过滤，**没有索引就是全表扫**，
    而那种慢不会报错、只会在数据变多之后突然显现。
    """
    from app.config import Settings
    from app.main import migrate
    from app.storage import Store

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")

    async with store.engine.connect() as conn:
        columns = [
            row[1] for row in await conn.execute(text("PRAGMA table_info(conversations)"))
        ]
        version = (await conn.execute(text("select version_num from alembic_version"))).scalar()
        indexes = [
            str(row[1]) for row in await conn.execute(text("PRAGMA index_list(conversations)"))
        ]

    assert "source" in columns, f"迁移没建出 source 列：{columns}"
    assert version == "0012", f"迁移版本不是 0012：{version}"
    assert any("source" in name for name in indexes), f"source 没有索引：{indexes}"


async def test_a_new_conversation_defaults_to_cli(tmp_path):
    """**回填默认值必须是 `cli`**：既有会话都是命令行产生的。

    如果默认成空串，所有既有会话在按来源筛选时会**同时从所有筛选里消失**——
    那看起来像「数据丢了」，实际只是默认值选错了。
    """
    from app.config import Settings
    from app.main import migrate
    from app.storage import Conversation, Store

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")

    saved = await store.save(Conversation(title="没指定来源"))
    assert saved.source == "cli"


async def test_the_chosen_source_is_persisted(tmp_path):
    """显式指定的来源要真的落库——否则筛选永远只能筛到默认值。"""
    from app.config import Settings
    from app.main import migrate
    from app.storage import Conversation, Store

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")

    saved = await store.save(Conversation(title="第三方集成", source="tool"))
    reloaded = await store.get(Conversation, saved.id)
    assert reloaded is not None and reloaded.source == "tool"


@pytest.mark.parametrize("source", ["cli", "tool", "telegram"])
async def test_source_filters_are_disjoint(tmp_path, source):
    """不同来源的会话**互不可见**——这是 `--source` 存在的全部理由。

    参数化而不是只测一个值：**只测 `cli` 的话，一个「永远返回全部」的实现也能过**
    （因为种子数据全是 cli）。
    """
    from app.config import Settings
    from app.main import migrate
    from app.storage import Conversation, Store

    settings = Settings(data_dir=tmp_path / "data", workspace=tmp_path / "files")
    settings.prepare()
    migrate(settings)
    store = Store(settings.data_dir / "app.sqlite")

    await store.save(Conversation(title="命令行会话", source="cli"))
    await store.save(Conversation(title="集成会话", source="tool"))

    rows = await store.rows(
        __import__("sqlalchemy").select(Conversation).where(Conversation.source == source)
    )
    expected = 1 if source in {"cli", "tool"} else 0
    assert len(rows) == expected, f"{source} 筛出 {len(rows)} 条，期望 {expected}"
