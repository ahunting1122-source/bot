"""记忆供应商（memory provider）：接口、15 个实现、按优先级路由。

## 名单怎么来的：按可用生态选 + 补齐 Hermes 名单

Hermes `plugins/memory/` 里是 **8 个** provider：`byterover` / `hindsight` /
`holographic` / `honcho` / `mem0` / `openviking` / `retaindb` / `supermemory`。
Orbit 原来的 8 个是 `local` / `file` / `jsonl` + `mem0` / `zep` / `letta` /
`chroma` / `pgvector`——**不是照抄**，而是"这台机器上真能跑的东西"（SQLite 表、
分文件、追加日志）加几个社区里最常被问到的后端。

两条口径都留着，理由不同：

- `local` / `file` / `jsonl` / `pgvector` 是 **Orbit 本地生态**：`local` 就是
  `memories` 表（`memory_save` / `memory_search` 用的那个），删掉它等于把现有记忆
  从路由器里摘掉；`pgvector` 是"将来上 PostgreSQL 时的那条路"，留着当路标。
- `byterover` / `hindsight` / `holographic` / `honcho` / `openviking` / `retaindb` /
  `supermemory` 是**补齐 Hermes 名单**：用户从 Hermes 过来会问"我的 honcho 记忆呢"，
  报"未知的供应商"等于说这块没做，所以宁可登记接口 + 如实报不可用。

## 分级（这是本模块最重要的一条约定）

本机**没有网络、不许装依赖**，所以只有 4 个能真正跑起来并写出验证证据：

| 级别 | provider | 为什么是这个级别 |
|---|---|---|
| 真实现 | `local` / `file` / `jsonl` | 只用标准库 + 已装依赖，离线可完整验证 |
| 真实现 | `holographic` | Hermes 名单里**唯一能离线真跑**的一个：HRR 全息向量是纯本地算法（`app/holographic.py`，SHA-256 + FFT），不需要任何服务或模型 |
| 只有接口 | `mem0` / `zep` / `letta` / `chroma` / `pgvector` / `byterover` / `hindsight` / `honcho` / `openviking` / `retaindb` / `supermemory` | 依赖未装（且多数还要云端服务），**无法验证真实行为** |

对第二级的态度是**不假装支持**：`available()` 老实返回 `False`（即使 pip 包某天被装上，
只要 Orbit 没接最小路径，它就仍然是 `False`——否则路由器会选中一个一用就抛的 provider），
`describe()` 给出安装提示与"为什么不可用"，读写操作明确抛 `ProviderUnavailable` 并带原因。

**包名的可信度**：`byterover` / `hindsight` / `honcho` / `openviking` / `retaindb` /
`supermemory` 的探测包名与凭证变量按 Hermes 插件 README 记。本机无网络，**无法核对
PyPI 上的真实包名**，所以 `describe()` 里一律写着"以官方文档为准"——抄错一个包名会让
用户 `pip install` 到一个不存在的东西，那比报"没装"更难查。

## 路由语义：读第一个命中，写多写

- **读**：按优先级取第一个 `available()` 且**真的返回了命中**的 provider。为什么不是
  "第一个 available 的就读它"：本地表可能一条都搜不到，而此时文件 provider 里有——
  只读第一个会把"有记忆但读不到"变成默认结果。
- **写**（`fanout=True`）：落到**所有** available 的 provider。多写的代价是同一份数据
  存几处，收益是切换后端不必迁移数据，且任何一处坏了都还有备份。Hermes 是"同一时刻
  只激活 1 个"，Orbit 选多写是因为它的记忆规模小（几百条），多写的成本可以忽略。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from app.policies import DefaultMemoryProvider, MemoryProvider

log = logging.getLogger("orbit.memory_providers")

#: key 长度上限。与 `storage.Memory.key` 的 `String(160)` 保持一致——
#: 两处不一致会让"批处理校验通过、落库截断"这种事发生。
KEY_LIMIT = 160
#: 单条 value 上限。超长的 value 会把 2,200 字符预算一条吃光（见 `memory_prompt`）。
VALUE_LIMIT = 4000
#: `search` 的默认返回条数。
DEFAULT_LIMIT = 10
#: 记忆文件目录名（`<data_dir>/memory/`）。
MEMORY_DIR = "memory"
#: 路由器配置文件名（优先级 + 是否多写）。
CONFIG_NAME = "providers.json"
#: `holographic` 的命中门槛（总分）。理由见 `HolographicProvider` 的类 docstring：
#: HRR 余弦对无关文本会给 0.01 量级的正相关，不过滤就会"任何查询都有命中"。
HRR_MIN_SCORE = 0.05

#: 全部 provider 的固定顺序（= `memory providers` 的展示顺序，也是默认优先级）。
#:
#: 前四个是**真实现**（离线可跑），后面全部是"只有接口"。顺序刻意这样排：默认优先级
#: 取前 3 个（见 `BUILTIN_NAMES`），用户翻到表的后半段时看到的就是"要装依赖的那些"。
#: 新补的 7 个按 Hermes 名单的字母序插在 `mem0` 前后——不是美观问题：和 Hermes 的
#: `ls plugins/memory/` 输出顺序对齐，比对差异时一眼就能看出少了谁。
PROVIDER_NAMES: tuple[str, ...] = (
    "local",
    "file",
    "jsonl",
    "holographic",
    "byterover",
    "hindsight",
    "honcho",
    "mem0",
    "openviking",
    "retaindb",
    "supermemory",
    "zep",
    "letta",
    "chroma",
    "pgvector",
)

#: 不需要任何外部依赖的 provider：默认优先级只含这四个（`holographic` 是纯本地算法）。
BUILTIN_NAMES: tuple[str, ...] = ("local", "file", "jsonl", "holographic")


class ProviderUnavailable(NotImplementedError):
    """provider 不可用或尚未接线。

    继承 `NotImplementedError` 是刻意的：调用方既可以用 `except ProviderUnavailable`
    拿到结构化原因（`name` / `reason` / `install`），也可以只 `except NotImplementedError`
    兜住"这个功能还没做"这一大类。
    """

    def __init__(self, name: str, what: str, reason: str, install: str = "") -> None:
        self.name = name
        self.what = what
        self.reason = reason
        self.install = install
        hint = f"；安装提示：{install}" if install else ""
        super().__init__(f"{name}.{what} 不可用：{reason}{hint}")


def _has_module(module: str) -> bool:
    """探测某个顶层包是否可导入（不真的 import，避免副作用与耗时）。

    `find_spec` 对"父包不存在"会抛 `ModuleNotFoundError`，对空字符串抛 `ValueError`，
    两种都当成"没装"。
    """
    if not module:
        return False
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def memory_dir(data_dir: Any) -> Path | None:
    """`<data_dir>/memory`；没给 data_dir 时返回 None（= 该 provider 不可用）。"""
    if data_dir is None:
        return None
    return Path(data_dir) / MEMORY_DIR


def _atomic_write(path: Path, text: str) -> None:
    """原子写：临时文件 + `os.replace`，读者永远看到旧完整文件或新完整文件。

    照 Hermes 的 `atomic_write_text`（mkstemp + fsync + os.replace）。记忆是长期资产，
    半截文件会让下一次 search 读到坏内容，而这种损坏事后极难定位。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _slug(key: str) -> str:
    """把 key 变成安全文件名：保留字母数字与 `-_.`，其余折成 `-`。

    中文 key 会被整体折成 `-`（`\\w` 不含 CJK），所以文件名后面还要挂 key 的短哈希，
    否则「默认语言」和「报告格式」会撞成同一个文件。
    """
    cleaned = re.sub(r"[^\w.-]+", "-", key, flags=re.UNICODE).strip("-.")
    cleaned = re.sub(r"-{2,}", "-", cleaned)
    return (cleaned or "memory")[:48]


# ── 真实现 1：本地表 ─────────────────────────────────────────────────────────


class LocalProvider(DefaultMemoryProvider):
    """真实现之一：`memories` 表（Orbit 现状的 key-value 记忆）。

    它就是把 `policies.DefaultMemoryProvider` 取个 provider 的名字挂进路由器，
    **不复制任何逻辑**——同一个后端两套实现，迟早会走偏。
    """

    name = "local"

    def describe(self) -> dict:
        info = super().describe()
        info["note"] = "跨对话长期记忆的默认后端，`memory_save` / `memory_search` 用的就是它"
        return info


# ── 真实现 2：分文件 ─────────────────────────────────────────────────────────


class FileProvider:
    """真实现之二：`<data_dir>/memory/*.md`，**一条记忆一个文件**。

    为什么分文件而不是像 Hermes 那样一个 `MEMORY.md` 加分节符：单文件每次写入都要
    重写整库（Hermes 为此专门加了文件锁 + 原子写 + 漂移检测）；分文件后单条记忆的
    增删改是 O(1)，而且天然可 diff、可人工编辑、可单独备份。

    文件格式是"首行 `# key`，空行，正文"——首行既是给人看的标题，也是
    `list_keys` 的唯一真值来源（不靠文件名反解，因为中文 slug 会退化）。
    """

    name = "file"
    kind = "builtin"

    def __init__(self, data_dir: Any = None) -> None:
        self.root = memory_dir(data_dir)

    def available(self) -> bool:
        return self.root is not None

    def _path_for(self, key: str) -> Path:
        assert self.root is not None  # available() 保证
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
        return self.root / f"{_slug(key)}-{digest}.md"

    def _read(self, path: Path) -> tuple[str, str] | None:
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            # 读不出来的文件不当成"空记忆"：跳过并在日志里留痕，
            # 否则一次 save 就会把它覆盖成别的内容（Hermes #26045 的教训）。
            log.warning("记忆文件读失败，已跳过：%s", path)
            return None
        if not text.startswith("# "):
            return None
        head, _, body = text.partition("\n")
        key = head[2:].strip()
        if not key:
            return None
        return key, body.lstrip("\n").rstrip("\n")

    async def search(self, query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        if self.root is None or not self.root.is_dir():
            return []
        needle = (query or "").strip().lower()
        found: list[tuple[float, dict]] = []
        for path in self.root.glob("*.md"):
            parsed = self._read(path)
            if parsed is None:
                continue
            key, value = parsed
            if needle and needle not in f"{key} {value}".lower():
                continue
            found.append(
                (
                    path.stat().st_mtime,
                    {"key": key, "value": value, "provider": self.name, "path": str(path)},
                )
            )
        found.sort(key=lambda item: item[0], reverse=True)  # 新改的排前面
        return [hit for _, hit in found[: max(1, int(limit))]]

    async def save(self, key: str, value: str) -> dict:
        if self.root is None:
            return {"key": key, "saved": False, "provider": self.name, "reason": "未绑定 data_dir"}
        path = self._path_for(key)
        _atomic_write(path, f"# {key}\n\n{value}\n")
        return {"key": key, "saved": True, "provider": self.name, "path": str(path)}

    async def delete(self, key: str) -> bool:
        if self.root is None:
            return False
        path = self._path_for(key)
        if not path.exists():
            return False
        path.unlink()
        return True

    async def list_keys(self) -> list[str]:
        if self.root is None or not self.root.is_dir():
            return []
        keys: list[str] = []
        for path in sorted(self.root.glob("*.md")):
            parsed = self._read(path)
            if parsed is not None:
                keys.append(parsed[0])
        return keys

    def describe(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "available": self.available(),
            "backend": f"files:{self.root}" if self.root else "files:",
            "install": "",
            "requires": [],
            "reason": "" if self.available() else "未绑定 data_dir",
            "note": "一条记忆一个 .md 文件，可 diff / 可人工编辑",
        }


# ── 真实现 3：JSONL 追加日志 ─────────────────────────────────────────────────


class JsonlProvider:
    """真实现之三：`<data_dir>/memory/memory.jsonl`，**追加写、永不原地改**。

    为什么还要一个追加日志：审计与回放。文件 provider 与本地表都只保留"当前值"，
    一旦被覆盖或删除，就再也答不出"这条记忆以前是什么、什么时候变的"。
    JSONL 每次操作追加一行，当前状态由**回放**得出——代价是文件只增不减，
    收益是任何一次写入都可追溯（`memory graph` / 未来的人工治理台都靠它）。

    行格式：`{"ts": 秒级浮点, "op": "save"|"delete", "key": ..., "value": ...}`
    """

    name = "jsonl"
    kind = "builtin"

    def __init__(self, data_dir: Any = None) -> None:
        self.root = memory_dir(data_dir)

    @property
    def path(self) -> Path | None:
        return None if self.root is None else self.root / "memory.jsonl"

    def available(self) -> bool:
        return self.path is not None

    def _append(self, record: dict) -> None:
        path = self.path
        assert path is not None  # available() 保证
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _state(self) -> dict[str, str]:
        """回放日志得到当前状态：同一个 key 以**最后一条**为准，delete 即移除。"""
        path = self.path
        state: dict[str, str] = {}
        if path is None or not path.exists():
            return state
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            log.warning("记忆日志读失败，按空库处理：%s", path)
            return state
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                # 单行坏了不影响其余行：追加日志的容错优势就在这里，
                # 不像整文件 JSON 那样一行坏就全丢。
                log.warning("记忆日志有一行无法解析，已跳过：%s", line[:80])
                continue
            if not isinstance(record, dict):
                continue
            key = str(record.get("key") or "")
            if not key:
                continue
            if str(record.get("op") or "save") == "delete":
                state.pop(key, None)
            else:
                state[key] = str(record.get("value") or "")
        return state

    async def search(self, query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        needle = (query or "").strip().lower()
        hits: list[dict] = []
        for key, value in self._state().items():
            if needle and needle not in f"{key} {value}".lower():
                continue
            hits.append({"key": key, "value": value, "provider": self.name})
            if len(hits) >= max(1, int(limit)):
                break
        return hits

    async def save(self, key: str, value: str) -> dict:
        if not self.available():
            return {"key": key, "saved": False, "provider": self.name, "reason": "未绑定 data_dir"}
        self._append({"ts": time.time(), "op": "save", "key": key, "value": value})
        return {"key": key, "saved": True, "provider": self.name, "path": str(self.path)}

    async def delete(self, key: str) -> bool:
        if not self.available():
            return False
        if key not in self._state():
            return False
        self._append({"ts": time.time(), "op": "delete", "key": key})
        return True

    async def list_keys(self) -> list[str]:
        return list(self._state())

    def history(self, key: str = "") -> list[dict]:
        """回放出的原始记录（审计用）。给 key 就只看那一条。"""
        path = self.path
        if path is None or not path.exists():
            return []
        records: list[dict] = []
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            return records
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and (not key or record.get("key") == key):
                records.append(record)
        return records

    def describe(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "available": self.available(),
            "backend": f"jsonl:{self.path}" if self.path else "jsonl:",
            "install": "",
            "requires": [],
            "reason": "" if self.available() else "未绑定 data_dir",
            "note": "追加日志：写入不可变，可回放/审计（`memory.jsonl`）",
        }


# ── 真实现 4：全息（HRR）─────────────────────────────────────────────────────


class HolographicProvider:
    """真实现之四：`<data_dir>/memory/holographic.json` + HRR 全息向量检索。

    ## 为什么这个 provider 值得真接

    Hermes 名单里 8 个 provider，只有 `holographic` 是**纯本地算法**：它的"记忆"
    就是 key → value 的库，检索靠三路信号（字面 fts 0.4 / 字符 jaccard 0.3 /
    HRR 叠加向量余弦 0.3）。没有服务、没有 API key、没有 embedding 模型——
    `app/holographic.py` 已经把这套东西实现好了（`MemoryGraph` / `score` /
    `explain`），所以这里**只是给它接一个持久化载体 + provider 接口**，
    一行算法都不重写。

    ## 为什么不复用一个已有的 provider 存

    `file`（一条一个 .md）与 `jsonl`（追加日志）是"给人看的载体"，而全息库要的是
    "能一次读出整库再算叠加向量"——一次 `search` 要读全库，分散在 200 个文件里
    会让检索退化成 200 次 open。所以这里用**单个 JSON 文件**（整库一个原子写），
    与 `MemoryGraph(entries)` 的入参形状一一对应。

    ## 检索语义

    `search` 走 `MemoryGraph.query`（按总分降序），并且**只返回 total ≥ `HRR_MIN_SCORE`
    的条目**。为什么必须过滤：

    1. 路由器把"返回了非空 hits"当成命中。若把全库原样返回，第一个 available 的
       provider 就永远"有命中"，后面的 provider 再也轮不到——那会把"读第一个有命中的"
       这条语义彻底废掉。
    2. HRR 那一路是余弦相似度，**无关文本之间也会有 0.01 量级的正相关**（相位随机
       重叠，dim=1024 时约 1/√1024 ≈ 0.03 量级）。实测"完全无关的查询"能拿到总分
       0.003——它不是命中，是噪声。`HRR_MIN_SCORE = 0.05` 卡在噪声（~0.003）与最弱
       的真实命中（部分字面重合 ≥ 0.2）之间。
    """

    name = "holographic"
    kind = "builtin"

    def __init__(self, data_dir: Any = None, *, dim: int | None = None) -> None:
        self.root = memory_dir(data_dir)
        self.dim = dim

    @property
    def path(self) -> Path | None:
        return None if self.root is None else self.root / "holographic.json"

    def available(self) -> bool:
        return self.path is not None

    # ── 载体 ──
    def _state(self) -> dict[str, str]:
        """读整库。坏文件按空库处理——一份坏 JSON 不该让记忆检索整体不可用。"""
        path = self.path
        if path is None or not path.exists():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("全息记忆库读失败，按空库处理：%s", path)
            return {}
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if not isinstance(entries, dict):
            return {}
        return {str(key): str(value) for key, value in entries.items()}

    def _write(self, entries: dict[str, str]) -> None:
        path = self.path
        assert path is not None  # available() 保证
        payload = {"version": 1, "entries": entries}
        _atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")

    def graph(self) -> Any:
        """整库 → `MemoryGraph`（CLI 的 `memory holographic` 也能直接用它）。

        延迟 import：`app/holographic.py` 顶部会 import numpy，而 `memory providers`
        这种只列表格的命令不该为此付一次 numpy 导入的钱。
        """
        from app import holographic as hrr

        kwargs = {} if self.dim is None else {"dim": int(self.dim)}
        return hrr.MemoryGraph(self._state(), **kwargs)

    # ── 接口 ──
    async def search(self, query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        needle = (query or "").strip()
        if not needle or not self.available():
            return []
        rows = self.graph().query(needle, limit=max(1, int(limit)) * 4)
        hits: list[dict] = []
        for row in rows:
            if float(row.get("total") or 0.0) < HRR_MIN_SCORE:
                continue
            hits.append(
                {
                    "key": row["key"],
                    "value": row["value"],
                    "provider": self.name,
                    # 三路分量一起带上：只报总分，用户没法回答"为什么这条排第一"。
                    "score": row["total"],
                    "fts": row.get("fts"),
                    "jaccard": row.get("jaccard"),
                    "hrr": row.get("hrr"),
                }
            )
            if len(hits) >= max(1, int(limit)):
                break
        return hits

    async def save(self, key: str, value: str) -> dict:
        if not self.available():
            return {"key": key, "saved": False, "provider": self.name, "reason": "未绑定 data_dir"}
        name = (key or "").strip()
        if not name:
            return {"key": key, "saved": False, "provider": self.name, "reason": "key 不能为空"}
        entries = self._state()
        entries[name] = "" if value is None else str(value)
        self._write(entries)
        return {"key": name, "saved": True, "provider": self.name, "path": str(self.path)}

    async def delete(self, key: str) -> bool:
        if not self.available():
            return False
        entries = self._state()
        name = (key or "").strip()
        if name not in entries:
            return False
        entries.pop(name, None)
        self._write(entries)
        return True

    async def list_keys(self) -> list[str]:
        return list(self._state())

    def describe(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "available": self.available(),
            "backend": f"hrr:{self.path}" if self.path else "hrr:",
            "install": "",
            "requires": [],
            "reason": "" if self.available() else "未绑定 data_dir",
            "note": "HRR 全息检索（fts0.4/jaccard0.3/hrr0.3），纯本地算法，无需服务或模型",
        }


# ── 只有接口的 11 个 ─────────────────────────────────────────────────────────


class _ExternalProvider:
    """需要额外依赖或外部服务的 provider 的共同骨架：接口完整、能力诚实。

    `_implemented` 恒为 `False`：`available()` 因此恒为 `False`，与"依赖是否装了"无关。
    为什么这么设计：`available()` 是路由器选路的唯一依据。若一个 provider 因为
    "pip 包装上了"就自称可用，路由器会选中它，然后第一次 `search` 抛
    `NotImplementedError`——那等于把"没实现"伪装成"运行时故障"。
    依赖装没装只作为 `dependency_installed` 如实报出来，供人判断下一步做什么。
    """

    name = ""
    kind = "external"
    #: 探测用的顶层包名（与 pip 包名不同：`mem0ai` 包导入名是 `mem0`）。
    module = ""
    #: 安装提示（本机无网络，所以提示里必须写明"需要网络"）。
    install = ""
    #: 还需要的环境变量或服务。
    requires: tuple[str, ...] = ()
    #: 后端定位（抄 Hermes 的 README 口径，不是猜的）。
    backend = ""
    #: 为什么不可用。
    reason = ""
    _implemented = False

    def __init__(self, data_dir: Any = None, config: Any = None) -> None:
        self.data_dir = data_dir
        self.config = config

    def available(self) -> bool:
        # `_implemented` 在左边：短路之后连探测都不做，语义也更清楚
        # ——"没接线"是比"依赖没装"更根本的原因。
        return self._implemented and _has_module(self.module)

    def _refuse(self, what: str) -> ProviderUnavailable:
        return ProviderUnavailable(self.name, what, self.reason, self.install)

    async def search(self, query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        raise self._refuse("search")

    async def save(self, key: str, value: str) -> dict:
        raise self._refuse("save")

    async def delete(self, key: str) -> bool:
        raise self._refuse("delete")

    async def list_keys(self) -> list[str]:
        raise self._refuse("list_keys")

    def describe(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "available": self.available(),
            "backend": self.backend,
            "install": self.install,
            "requires": list(self.requires),
            "dependency_installed": _has_module(self.module),
            "reason": self.reason,
            "note": "只有接口：读写会抛 ProviderUnavailable（带安装提示）",
        }


class Mem0Provider(_ExternalProvider):
    """mem0：服务端 LLM 事实抽取 + 语义检索 + 自动去重（Hermes 的 4 个工具之一）。"""

    name = "mem0"
    module = "mem0"
    install = "pip install 'mem0ai>=2.0,<3'（需要网络）"
    requires = ("MEM0_API_KEY",)
    backend = "mem0 platform / 自建 dashboard / oss（进程内 LLM+embedder+vector）"
    reason = "依赖未安装，且本机无网络无法验证真实行为；Orbit 只登记了接口"


class ByteroverProvider(_ExternalProvider):
    """byterover：Hermes 名单里的第 1 个（上下文/记忆服务，走 API）。"""

    name = "byterover"
    module = "byterover"
    install = "pip install byterover（需要网络；包名以官方文档为准）"
    requires = ("BYTEROVER_API_KEY",)
    backend = "ByteRover 云端记忆服务"
    reason = "依赖未安装，且需要云端账号与网络；Orbit 只登记了接口"


class HindsightProvider(_ExternalProvider):
    """hindsight：Hermes 名单里的第 2 个（带时间维度的回顾式记忆）。"""

    name = "hindsight"
    module = "hindsight"
    install = "pip install hindsight-memory（需要网络；包名以官方文档为准）"
    requires = ("HINDSIGHT_API_KEY", "HINDSIGHT_BASE_URL")
    backend = "Hindsight 服务（自建或云）"
    reason = "依赖未安装，且需要服务端点与网络；Orbit 只登记了接口"


class HonchoProvider(_ExternalProvider):
    """honcho：Hermes 名单里的第 4 个（Plastic Labs 的用户状态/会话推理服务）。"""

    name = "honcho"
    module = "honcho"
    install = "pip install honcho（需要网络）"
    requires = ("HONCHO_API_KEY",)
    backend = "Honcho（用户建模 + 会话级记忆）"
    reason = "依赖未安装，且需要云端账号与网络；Orbit 只登记了接口"


class OpenvikingProvider(_ExternalProvider):
    """openviking：Hermes 名单里的第 6 个（Viking 记忆服务客户端）。"""

    name = "openviking"
    module = "openviking"
    install = "pip install openviking（需要网络；包名以官方文档为准）"
    requires = ("OPENVIKING_API_KEY", "OPENVIKING_BASE_URL")
    backend = "OpenViking 记忆服务"
    reason = "依赖未安装，且需要服务端点与网络；Orbit 只登记了接口"


class RetaindbProvider(_ExternalProvider):
    """retaindb：Hermes 名单里的第 7 个（可查询的记忆数据库）。"""

    name = "retaindb"
    module = "retaindb"
    install = "pip install retaindb（需要网络；包名以官方文档为准）"
    requires = ("RETAINDB_API_KEY",)
    backend = "RetainDB 记忆数据库"
    reason = "依赖未安装，且需要数据库端点与网络；Orbit 只登记了接口"


class SupermemoryProvider(_ExternalProvider):
    """supermemory：Hermes 名单里的第 8 个（云端个人记忆 API）。"""

    name = "supermemory"
    module = "supermemory"
    install = "pip install supermemory（需要网络）"
    requires = ("SUPERMEMORY_API_KEY",)
    backend = "Supermemory API（云）"
    reason = "依赖未安装，且需要云端账号与网络；Orbit 只登记了接口"


class ZepProvider(_ExternalProvider):
    """zep：会话级记忆 + 知识图谱（服务端）。"""

    name = "zep"
    module = "zep_cloud"
    install = "pip install zep-cloud（需要网络）"
    requires = ("ZEP_API_KEY",)
    backend = "Zep Cloud"
    reason = "依赖未安装，且本机无网络无法验证真实行为；Orbit 只登记了接口"


class LettaProvider(_ExternalProvider):
    """letta（前 MemGPT）：有状态 agent 记忆，带自管理记忆块。"""

    name = "letta"
    module = "letta"
    install = "pip install letta（需要网络）"
    requires = ("LETTA_API_KEY", "LETTA_BASE_URL")
    backend = "Letta server（自建或云）"
    reason = "依赖未安装，且本机无网络无法验证真实行为；Orbit 只登记了接口"


class ChromaProvider(_ExternalProvider):
    """chroma：本地向量库（embedding 检索）。

    与 `file` / `jsonl` 的关键差别是**要 embedding 模型**：Orbit 已装 fastembed，
    但"记忆检索"目前走 FTS/子串，接 chroma 等于新开一条向量路，属于独立决策。
    """

    name = "chroma"
    module = "chromadb"
    install = "pip install chromadb（需要网络）"
    requires = ("embedding 模型",)
    backend = "本地/服务端 Chroma（向量）"
    reason = "依赖未安装；且向量检索需要 embedding 模型，属于尚未拍板的独立决策"


class PgvectorProvider(_ExternalProvider):
    """pgvector：PostgreSQL 的向量扩展（多用户/服务端部署时才需要）。"""

    name = "pgvector"
    module = "pgvector"
    install = "pip install 'psycopg[binary]' pgvector（需要网络）+ 一个 PostgreSQL 实例"
    requires = ("PGVECTOR_DSN",)
    backend = "PostgreSQL + pgvector 扩展"
    reason = "依赖与数据库实例都不可用（本机只有 SQLite），Orbit 只登记了接口"


#: provider 名字 → 构造函数。新增 provider 只需在这里挂一行。
PROVIDER_TYPES: dict[str, type] = {
    "local": LocalProvider,
    "file": FileProvider,
    "jsonl": JsonlProvider,
    "holographic": HolographicProvider,
    "byterover": ByteroverProvider,
    "hindsight": HindsightProvider,
    "honcho": HonchoProvider,
    "mem0": Mem0Provider,
    "openviking": OpenvikingProvider,
    "retaindb": RetaindbProvider,
    "supermemory": SupermemoryProvider,
    "zep": ZepProvider,
    "letta": LettaProvider,
    "chroma": ChromaProvider,
    "pgvector": PgvectorProvider,
}


def build_provider(name: str, *, store: Any = None, data_dir: Any = None) -> MemoryProvider:
    """按名字造一个 provider。`local` 需要 store，其余（含 `holographic`）需要 data_dir。"""
    kind = PROVIDER_TYPES.get(name)
    if kind is None:
        raise ValueError(f"未知的记忆供应商：{name}（可选：{', '.join(PROVIDER_NAMES)}）")
    if kind is LocalProvider:
        return LocalProvider(store)
    return kind(data_dir)  # type: ignore[call-arg]


def default_providers(*, store: Any = None, data_dir: Any = None) -> list[MemoryProvider]:
    """全部 provider 的默认实例（顺序 = `PROVIDER_NAMES`）。"""
    return [build_provider(name, store=store, data_dir=data_dir) for name in PROVIDER_NAMES]


# ── 路由器 ───────────────────────────────────────────────────────────────────


def config_path(data_dir: Any) -> Path | None:
    root = memory_dir(data_dir)
    return None if root is None else root / CONFIG_NAME


def load_config(data_dir: Any) -> dict:
    """读 `<data_dir>/memory/providers.json`；坏文件退回默认（不让配置坏了就不可用）。"""
    path = config_path(data_dir)
    if path is None or not path.exists():
        return {"priority": list(BUILTIN_NAMES), "fanout": True}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.warning("记忆路由配置读失败，退回默认：%s", path)
        return {"priority": list(BUILTIN_NAMES), "fanout": True}
    if not isinstance(data, dict):
        return {"priority": list(BUILTIN_NAMES), "fanout": True}
    priority = [str(name) for name in (data.get("priority") or []) if str(name) in PROVIDER_TYPES]
    return {
        "priority": priority or list(BUILTIN_NAMES),
        "fanout": bool(data.get("fanout", True)),
    }


def save_config(data_dir: Any, *, priority: list[str] | None = None, fanout: bool | None = None) -> dict:
    """写路由配置。只覆盖传进来的字段，其余保持原样。"""
    path = config_path(data_dir)
    if path is None:
        raise ValueError("未绑定 data_dir，无法保存记忆路由配置")
    current = load_config(data_dir)
    if priority is not None:
        unknown = [name for name in priority if name not in PROVIDER_TYPES]
        if unknown:
            raise ValueError(f"未知的记忆供应商：{', '.join(unknown)}")
        current["priority"] = list(priority)
    if fanout is not None:
        current["fanout"] = bool(fanout)
    _atomic_write(path, json.dumps(current, ensure_ascii=False, indent=2) + "\n")
    return current


class MemoryRouter:
    """按优先级路由：读取第一个命中，写按 fanout 多写。"""

    def __init__(self, providers: list[MemoryProvider], *, fanout: bool = True) -> None:
        self.providers = list(providers)
        self.fanout = bool(fanout)

    # ── 观察 ──
    def available(self) -> list[MemoryProvider]:
        return [provider for provider in self.providers if provider.available()]

    def active(self) -> MemoryProvider | None:
        """优先级最高的可用 provider（没有则 None）。"""
        for provider in self.providers:
            if provider.available():
                return provider
        return None

    def describe(self) -> list[dict]:
        active = self.active()
        rows: list[dict] = []
        for provider in self.providers:
            info = provider.describe()
            info["active"] = provider is active
            rows.append(info)
        return rows

    def status(self) -> dict:
        active = self.active()
        return {
            "active": active.name if active is not None else "",
            "fanout": self.fanout,
            "priority": [provider.name for provider in self.providers],
            "available": [provider.name for provider in self.available()],
            "providers": self.describe(),
        }

    # ── 读 ──
    async def search(self, query: str, limit: int = DEFAULT_LIMIT) -> dict:
        """按优先级找**第一个有命中的** provider。

        返回 `{"provider": 名字, "hits": [...]}`；全都没命中时 `provider` 是空串——
        调用方据此区分"没人有"与"第一个没有"。
        """
        for provider in self.available():
            try:
                hits = await provider.search(query, limit)
            except NotImplementedError as exc:  # 未接线的 provider 不该拖垮整次检索
                log.debug("provider %s 未接线：%s", provider.name, exc)
                continue
            if hits:
                for hit in hits:
                    hit.setdefault("provider", provider.name)
                return {"provider": provider.name, "hits": hits}
        return {"provider": "", "hits": []}

    async def list_keys(self) -> list[str]:
        """所有可用 provider 的 key 并集（保序：先出现的先返回）。"""
        seen: dict[str, None] = {}
        for provider in self.available():
            try:
                keys = await provider.list_keys()
            except NotImplementedError:
                continue
            for key in keys:
                seen.setdefault(key, None)
        return list(seen)

    # ── 写 ──
    async def save(self, key: str, value: str) -> dict:
        """写记忆。`fanout=True` 时落到所有可用 provider，否则只写第一个。"""
        targets = self.available()
        if not self.fanout:
            targets = targets[:1]
        written: list[str] = []
        results: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for provider in targets:
            try:
                result = await provider.save(key, value)
            except NotImplementedError as exc:
                errors[provider.name] = str(exc)
                continue
            results[provider.name] = result
            if result.get("saved"):
                written.append(provider.name)
        return {
            "ok": bool(written),
            "key": key,
            "written": written,
            "results": results,
            "errors": errors,
        }

    async def delete(self, key: str) -> dict:
        targets = self.available()
        if not self.fanout:
            targets = targets[:1]
        deleted: list[str] = []
        errors: dict[str, str] = {}
        for provider in targets:
            try:
                if await provider.delete(key):
                    deleted.append(provider.name)
            except NotImplementedError as exc:
                errors[provider.name] = str(exc)
        return {"ok": bool(deleted), "key": key, "deleted": deleted, "errors": errors}


def build_router(
    *,
    settings: Any = None,
    store: Any = None,
    providers: list[MemoryProvider] | None = None,
    data_dir: Any = None,
) -> MemoryRouter:
    """按 `<data_dir>/memory/providers.json` 的优先级装一台路由器。

    没在配置里出现的 provider 排在最后（保持 `PROVIDER_NAMES` 的相对顺序），
    这样新增 provider 不需要改任何配置文件就会出现在列表里。
    """
    root = data_dir if data_dir is not None else getattr(settings, "data_dir", None)
    config = load_config(root)
    items = (
        list(providers)
        if providers is not None
        else default_providers(store=store, data_dir=root)
    )
    order = [str(name) for name in (config.get("priority") or [])]
    fallback = {provider.name: index for index, provider in enumerate(items)}

    def rank(provider: MemoryProvider) -> tuple[int, int]:
        if provider.name in order:
            return (order.index(provider.name), 0)
        return (len(order), fallback.get(provider.name, 99))

    return MemoryRouter(sorted(items, key=rank), fanout=bool(config.get("fanout", True)))
