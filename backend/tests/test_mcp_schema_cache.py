"""MCP schema 缓存（矩阵 8.7）的契约测试。

覆盖的是**失效规则**，因为缓存出错的代价不是"慢一点"，而是"模型拿到一份不存在的工具
清单"。六条判据逐条钉住：往返、TTL、服务版本、schema 指纹、配置指纹、损坏文件降级。
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pytest

from app.config import Settings
from app.mcp import MCPManager
from app.mcp_schema_cache import (
    DEFAULT_TTL_SECONDS,
    SCHEMA_VERSION,
    CachedSchema,
    SchemaCache,
    config_fingerprint,
    schema_fingerprint,
)

# ``app.mcp`` 上面那个 import **不能删，也不能挪到函数里**（哪怕本文件的用例几乎都用
# 不到它）：mcp SDK 的 ``mcp/os/win32/utilities.py`` 把 ``errlog: TextIO = sys.stderr``
# 写成**默认参数**，它在模块被导入的那一刻求值。如果 ``app.mcp`` 第一次是在某个
# ``capsys`` 用例里被导入的，那个默认值就会绑到 pytest 的捕获对象上，之后任何一次起
# stdio 子进程都会在 ``subprocess.Popen(stderr=...)`` 里炸
# ``UnsupportedOperation: fileno``——现象是"服务启动失败"，原因离现场十万八千里。

#: 固定"现在"。用常量而不是 time.time()，是因为 TTL 断言必须能精确到边界。
NOW = 1_700_000_000.0

TOOLS = [
    {
        "name": "echo",
        "description": "回显文本",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
    {
        "name": "add",
        "description": "相加两个数",
        "input_schema": {"type": "object", "properties": {"a": {"type": "number"}}},
    },
]
ENTRY = {"name": "demo", "command": "npx", "args": ["-y", "demo-mcp"], "enabled": True}


def make_cache(tmp_path, **kwargs) -> SchemaCache:
    return SchemaCache(tmp_path / "mcp-schema-cache.json", **kwargs)


# ── 往返 ────────────────────────────────────────────────────────────────────


def test_round_trip_writes_atomic_file_with_schema_version(tmp_path):
    cache = make_cache(tmp_path)
    written = cache.put("demo", TOOLS, server_version="1.0.0", config=ENTRY, now=NOW)
    assert written.server_version == "1.0.0"
    got = cache.get("demo", config=ENTRY, server_version="1.0.0", now=NOW + 1)
    assert got is not None
    assert [tool["name"] for tool in got.tools] == ["echo", "add"]
    # schema 原样往返（不是被摊平成名字列表）。
    assert got.tools[0]["input_schema"]["properties"]["text"] == {"type": "string"}
    payload = json.loads(cache.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == SCHEMA_VERSION
    assert "demo" in payload["servers"]
    # 原子写：临时文件不该留在盘上。
    assert not cache.path.with_name(cache.path.name + ".tmp").exists()


def test_for_settings_puts_cache_under_data_dir(tmp_path):
    settings = Settings(data_dir=tmp_path / "data")
    cache = SchemaCache.for_settings(settings)
    assert cache.path == (tmp_path / "data" / "mcp-schema-cache.json")


def test_unknown_server_is_a_miss(tmp_path):
    assert make_cache(tmp_path).get("nope") is None


# ── TTL ─────────────────────────────────────────────────────────────────────


def test_server_ttl_expires_the_entry(tmp_path):
    cache = make_cache(tmp_path)
    cache.put("demo", TOOLS, server_version="1", config=ENTRY, ttl_ms=1000, now=NOW)
    assert cache.get("demo", now=NOW + 0.5) is not None
    assert cache.get("demo", now=NOW + 1.5) is None


def test_zero_ttl_falls_back_to_default_ttl(tmp_path):
    """``ttlMs: 0`` 默认按"没给提示"处理。

    这不是偷懒：mcp 2.x 的 ``MCPServer`` **总是**发 ``ttlMs: 0``，严格照 SEP-2549 读
    会让所有用它实现的服务永远命中不了缓存（见 `app/mcp_schema_cache.py` 第 5 条）。
    """
    cache = make_cache(tmp_path)
    cache.put("demo", TOOLS, server_version="1", config=ENTRY, ttl_ms=0, now=NOW)
    assert cache.get("demo", now=NOW) is not None
    assert cache.get("demo", now=NOW + DEFAULT_TTL_SECONDS + 1) is None


def test_zero_ttl_can_be_read_strictly(tmp_path):
    """``strict_ttl=True`` 恢复 SEP-2549 原义：服务说 0 就是"别缓存"。"""
    cache = make_cache(tmp_path, strict_ttl=True)
    cache.put("demo", TOOLS, server_version="1", config=ENTRY, ttl_ms=0, now=NOW)
    assert cache.get("demo", now=NOW) is None


def test_missing_ttl_falls_back_to_default_ttl(tmp_path):
    cache = make_cache(tmp_path)
    cache.put("demo", TOOLS, server_version="1", config=ENTRY, now=NOW)
    assert cache.get("demo", now=NOW + DEFAULT_TTL_SECONDS - 1) is not None
    assert cache.get("demo", now=NOW + DEFAULT_TTL_SECONDS + 1) is None


def test_default_ttl_none_restores_hermes_never_expire_semantics(tmp_path):
    cache = make_cache(tmp_path, default_ttl=None)
    cache.put("demo", TOOLS, server_version="1", config=ENTRY, now=NOW)
    assert cache.get("demo", now=NOW + 10 * 365 * 86400) is not None


# ── 服务版本 / schema 指纹 ──────────────────────────────────────────────────


def test_server_version_change_invalidates(tmp_path):
    cache = make_cache(tmp_path)
    cache.put("demo", TOOLS, server_version="1.0.0", config=ENTRY, now=NOW)
    assert cache.get("demo", server_version="1.0.0", now=NOW) is not None
    assert cache.get("demo", server_version="2.0.0", now=NOW) is None


def test_unreported_version_does_not_invalidate(tmp_path):
    """服务这次没报版本（空串）时不能判死——2026 之后的服务可以不报身份。"""
    cache = make_cache(tmp_path)
    cache.put("demo", TOOLS, server_version="1.0.0", config=ENTRY, now=NOW)
    assert cache.get("demo", server_version="", now=NOW) is not None


def test_schema_fingerprint_change_invalidates(tmp_path):
    cache = make_cache(tmp_path)
    cache.put("demo", TOOLS, server_version="1", config=ENTRY, now=NOW)
    same = schema_fingerprint(TOOLS)
    assert cache.get("demo", schema_fingerprint=same, now=NOW) is not None
    assert cache.get("demo", schema_fingerprint="deadbeefdeadbeef", now=NOW) is None


def test_schema_fingerprint_ignores_key_order():
    """同一个清单换个键顺序不该被当成"变了"（否则每次重连都白丢缓存）。"""
    reordered = [dict(reversed(list(tool.items()))) for tool in TOOLS]
    assert schema_fingerprint(reordered) == schema_fingerprint(TOOLS)


def test_put_overwrites_when_tools_change(tmp_path):
    cache = make_cache(tmp_path)
    cache.put("demo", TOOLS, server_version="1", config=ENTRY, now=NOW)
    fewer = TOOLS[:1]
    cache.put("demo", fewer, server_version="1", config=ENTRY, now=NOW + 5)
    got = cache.get("demo", config=ENTRY, now=NOW + 6)
    assert got is not None and [tool["name"] for tool in got.tools] == ["echo"]


# ── 配置指纹 ────────────────────────────────────────────────────────────────


def test_changing_the_command_misses_the_old_entry(tmp_path):
    cache = make_cache(tmp_path)
    cache.put("demo", TOOLS, server_version="1", config=ENTRY, now=NOW)
    changed = {**ENTRY, "command": "uvx"}
    assert cache.get("demo", config=changed, now=NOW) is None


def test_fingerprint_covers_env_and_header_key_names():
    """换密钥/换 header 键等于换身份：键名进指纹，值不进（值可能是秘密）。"""
    base = {"name": "demo", "url": "https://example.com/mcp"}
    with_env = {**base, "env": {"TOKEN": "keyring:a"}}
    other_env = {**base, "env": {"OTHER": "keyring:a"}}
    assert config_fingerprint(with_env) != config_fingerprint(other_env)
    # 只改值不改键名 → 指纹不变（值是运行时才解析的秘密，缓存不该跟着抖）。
    same_key = {**base, "env": {"TOKEN": "keyring:b"}}
    assert config_fingerprint(with_env) == config_fingerprint(same_key)
    with_header = {**base, "headers": {"Authorization": "keyring:h"}}
    assert config_fingerprint(with_header) != config_fingerprint(base)


def test_approval_policy_changes_do_not_invalidate(tmp_path):
    """``enabled`` / ``auto_approve`` 不改变服务声明什么工具，不该丢缓存。"""
    cache = make_cache(tmp_path)
    cache.put("demo", TOOLS, server_version="1", config=ENTRY, now=NOW)
    changed = {**ENTRY, "enabled": False, "auto_approve": True}
    assert cache.get("demo", config=changed, now=NOW) is not None


# ── 降级 ────────────────────────────────────────────────────────────────────


def test_corrupted_file_degrades_to_miss_without_raising(tmp_path, caplog):
    cache = make_cache(tmp_path)
    cache.path.write_text("{ 这不是 JSON", encoding="utf-8")
    with caplog.at_level("WARNING"):
        assert cache.get("demo") is None
    assert any("损坏" in record.message for record in caplog.records)


def test_unknown_schema_version_is_ignored(tmp_path, caplog):
    cache = make_cache(tmp_path)
    cache.path.write_text(
        json.dumps({"schema_version": 999, "servers": {"demo": {"tools": []}}}),
        encoding="utf-8",
    )
    with caplog.at_level("WARNING"):
        assert cache.get("demo") is None
    assert any("格式版本" in record.message for record in caplog.records)


def test_broken_entry_is_a_miss_not_a_crash(tmp_path):
    cache = make_cache(tmp_path)
    cache.path.write_text(
        json.dumps({"schema_version": SCHEMA_VERSION, "servers": {"demo": {"tools": "nope"}}}),
        encoding="utf-8",
    )
    assert cache.get("demo") is None
    # 坏条目在 entries() 里仍可见（`mcp cache prune` 靠它清理）。
    rows = cache.entries()
    assert rows == [
        {
            "server": "demo",
            "tools": 0,
            "age": None,
            "fresh": False,
            "version": "",
            "ttl_ms": None,
            "broken": True,
        }
    ]


def test_unwritable_path_does_not_raise(tmp_path):
    """写不下去（只读盘/权限）不该让连接失败——缓存是优化，不是依赖。"""
    blocker = tmp_path / "blocked"
    blocker.write_text("我是文件，不是目录", encoding="utf-8")
    cache = SchemaCache(blocker / "cache.json")
    cache.put("demo", TOOLS, server_version="1", config=ENTRY, now=NOW)  # 不抛
    assert cache.get("demo") is None


# ── invalidate / clear / prune ──────────────────────────────────────────────


def test_invalidate_removes_only_that_server(tmp_path):
    cache = make_cache(tmp_path)
    cache.put("a", TOOLS, server_version="1", now=NOW)
    cache.put("b", TOOLS, server_version="1", now=NOW)
    assert cache.invalidate("a") is True
    assert cache.invalidate("a") is False
    assert cache.get("a", now=NOW) is None
    assert cache.get("b", now=NOW) is not None


def test_clear_empties_everything(tmp_path):
    cache = make_cache(tmp_path)
    cache.put("a", TOOLS, server_version="1", now=NOW)
    cache.put("b", TOOLS, server_version="1", now=NOW)
    assert cache.clear() == 2
    assert cache.entries() == []


def test_prune_drops_only_old_entries(tmp_path):
    cache = make_cache(tmp_path)
    cache.put("old", TOOLS, server_version="1", now=NOW - 10 * 86400)
    cache.put("new", TOOLS, server_version="1", now=NOW)
    removed = cache.prune(7 * 86400, now=NOW)
    assert removed == ["old"]
    assert [row["server"] for row in cache.entries(now=NOW)] == ["new"]


def test_entries_reports_freshness(tmp_path):
    cache = make_cache(tmp_path)
    cache.put("demo", TOOLS, server_version="9.9", config=ENTRY, now=NOW)
    rows = cache.entries(now=NOW + 60)
    assert rows[0]["tools"] == 2
    assert rows[0]["fresh"] is True
    assert rows[0]["version"] == "9.9"
    assert rows[0]["config_fingerprint"] == config_fingerprint(ENTRY)


def test_cached_schema_is_frozen():
    entry = CachedSchema(
        server_key="demo",
        config_fingerprint="f",
        schema_fingerprint="s",
        server_version="1",
        tools=(),
        ttl_ms=None,
        written_at=NOW,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.server_version = "2"  # type: ignore[misc]


# ── CLI：mcp cache {show,clear,prune} ───────────────────────────────────────


def _settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path / "data")


def _run_cli(tmp_path, capsys, *argv):
    """跑一次真 CLI（先导入 mcp_extra：生产接线里由 commands/__init__.py 负责）。"""
    import app.cli.commands.mcp_extra  # noqa: F401 - 导入即接线
    from app.cli.main import main

    code = main(["--data-dir", str(tmp_path / "data"), *argv])
    return code, capsys.readouterr()


def test_cli_cache_show_lists_entries(tmp_path, capsys):
    cache = SchemaCache.for_settings(_settings(tmp_path))
    cache.put("demo", TOOLS, server_version="1.0.0", config=ENTRY)
    code, captured = _run_cli(tmp_path, capsys, "mcp", "cache", "show")
    assert code == 0
    assert "demo" in captured.out
    assert "mcp-schema-cache.json" in captured.out


def test_cli_cache_clear_removes_entries(tmp_path, capsys):
    cache = SchemaCache.for_settings(_settings(tmp_path))
    cache.put("demo", TOOLS, server_version="1", config=ENTRY)
    code, captured = _run_cli(tmp_path, capsys, "mcp", "cache", "clear")
    assert code == 0
    assert "已清空" in captured.out
    assert cache.entries() == []


def test_cli_cache_prune_reports_nothing_to_do(tmp_path, capsys):
    code, captured = _run_cli(tmp_path, capsys, "mcp", "cache", "prune", "--max-age", "60")
    assert code == 0
    assert "没有超过" in captured.out


def test_cli_cache_prune_rejects_bad_max_age(tmp_path, capsys):
    code, _captured = _run_cli(tmp_path, capsys, "mcp", "cache", "prune", "--max-age", "abc")
    assert code == 1


# ── 接线：第二次装配真的跳过 tools/list ────────────────────────────────────
#
# 这是整个矩阵 8.7 的**唯一**验收点：缓存写了但装配时不用，等于没做。所以这里起一个
# 真的 stdio 服务（tests/fixtures/echo_mcp_server.py），数 `list_tools` 被调了几次。

FIXTURE = Path(__file__).parent / "fixtures" / "echo_mcp_server.py"


async def test_second_start_skips_tools_list(tmp_path, monkeypatch):
    from mcp.client import Client

    from app.tools import ToolRegistry

    settings = Settings(data_dir=tmp_path, workspace=tmp_path / "files")
    settings.prepare()
    entry = {"name": "echo", "command": sys.executable, "args": [str(FIXTURE)]}

    calls: list[int] = []
    original = Client.list_tools

    async def spy(self, *args, **kwargs):
        calls.append(1)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(Client, "list_tools", spy)

    for round_index in range(2):
        manager = MCPManager(settings)
        manager.save_servers([entry])
        registry = ToolRegistry(settings, None)
        await manager.start(registry)
        try:
            assert manager.status["echo"]["state"] == "ready", manager.status["echo"]["error"]
            names = sorted(name for name in registry.tools if name.startswith("mcp__echo__"))
            assert names == ["mcp__echo__add", "mcp__echo__echo"]
        finally:
            await manager.close(registry)
        # 关键断言：两次装配**总共只列一次**工具清单——第二次是缓存命中的。
        assert len(calls) == 1, f"第 {round_index + 1} 次装配又列了一次工具清单（缓存没生效）"

    # 两次装配拿到的是同一份 schema（缓存往返没有把 inputSchema 弄丢）。
    cached = SchemaCache.for_settings(settings).get("echo", config=entry)
    assert cached is not None
    assert cached.tools[0]["input_schema"]["title"] == "addArguments"
