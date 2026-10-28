"""归档不删 + 系统提示词内容寻址（MATRIX 10.3 / 10.5）。

对标 ``handoff/hermes-parity/research/03-persistence-layer.md`` §1.1 / §5.4 / §6.2：

- ``sessions.archived`` / ``hidden`` / ``pinned`` 是**标记位**，不是删除；
- ``messages.active=0, compacted=1`` 是压缩归档（内容保留，只是移出活动上下文）；
- ``system_prompts(hash TEXT PRIMARY KEY, prompt TEXT NOT NULL)`` 是内容寻址表：
  本机实测 106 个会话里有 27 个带 ``system_prompt_hash``，而 ``system_prompts``
  只有 22 行——**同一份提示被 5 个会话复用**，逐会话存一份要 894,988 字节，
  内容寻址后只要 717,973 字节（省 177,015 字节、5 份）。

## 一、归档为什么只打标记，绝不删消息

删掉的会话在"上个月那个决定是怎么定的"这类问题上是**不可恢复的损失**：会话是用户
唯一的原始记录，Orbit 没有备份命令，也没有 Hermes 的 ``sessions recover`` 能从
派生索引重建正文。Hermes 自己也从不删——归档位、隐藏位、压缩位全都留着正文
（``session_search_tool.py`` 的注释原话：compaction archives are
``active=0, compacted=1`` — the content was kept）。所以本模块的 ``archive()``
只往平面日志追加一条 ``session.archived`` 事件，一条消息都不碰；
``unarchive()`` 追加反向事件。**没有 ``delete()``，这是刻意的。**

状态怎么读：平面日志是只追加的，同一会话可能被归档、取消归档、再归档。所以
"当前是否归档"= **最后一条归档/取消归档事件说了算**（:meth:`SessionArchive.entries`
逐条扫、后写覆盖先写）。这与 ``app/session_plane.py`` 的谱系取值口径一致
（``lineage`` 里同一子会话被压缩两次时取最后一次）。

## 二、提示词为什么按 sha256 落盘，而不是塞进任务行

``tasks`` 表（``app/storage.py``）是禁改区，加不了列；而系统提示词动辄 20–30 KB
（Hermes 实测单份最大 31,481 字节），塞进 ``tasks.waiting`` 这种 JSON 列会让每次读
任务都顺带把几十 KB 的提示词读进内存，也会让 ``sessions export`` 的输出里混进
与对话无关的大段机器文本。所以按内容寻址：

- 正文落 ``<data_dir>/system-prompts/<sha256>.txt``（一个 hash 一个文件）；
- 任务只记 hash（记在平面日志的 ``prompt.frozen`` 事件里，``task_id`` → ``hash``）；
- 同一份提示被多个任务复用时**只存一份**（``put()`` 幂等）。

为什么正文落文件而不是另开一张 SQLite 表：``app/storage.py`` 与 alembic 迁移是
禁改区（加表就要改迁移脚本），而提示词是"一次写、偶尔读"的冷数据，文件正合适；
它还能被 ``grep``、被编辑器直接打开——排障时"模型当时到底看到了什么"是可以肉眼核对的。

**hash 不匹配必须报错，不能返回内容**：文件名就是内容摘要，两者不符说明文件被手改、
被截断或落了半行（``os.replace`` 之前进程被杀）。这时悄悄返回内容等于把一个
"当时模型看到的提示"变成一句谎话——而这一层存在的全部意义就是可追溯。所以
:meth:`PromptStore.read` 重算 sha256，不符就抛 :class:`PromptMismatch`。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.session_plane import (
    KIND_ARCHIVED,
    KIND_PROMPT_FROZEN,
    KIND_UNARCHIVED,
    SessionPlane,
)

#: 提示词正文的落点目录名（与 ``data_dir`` 同级拼接）。
PROMPTS_DIRNAME = "system-prompts"

#: 提示词文件的扩展名。带扩展名是为了让人一眼看出这是纯文本。
PROMPT_SUFFIX = ".txt"


class ArchiveError(RuntimeError):
    """可预期的归档/提示词错误（会话 id 为空、提示词不存在等）。

    与"真 bug"区分开：CLI 侧把它翻成 ``CliError``（一行消息 + 返回码），不打栈。
    """


class PromptMismatch(ArchiveError):
    """文件内容与文件名（sha256）不符。

    单独一个异常类型，是为了让调用方能把"文件被改过"与"文件不存在"分开处理——
    前者意味着数据完整性已经出问题，后者只是还没存过。
    """


def _require_id(value: Any, what: str = "会话") -> str:
    """校验 id 非空。空 id 会被当成"整库"的过滤器，那是最危险的一类输入。"""
    text = str(value or "").strip()
    if not text:
        raise ArchiveError(f"{what} id 不能为空")
    return text


# ── 归档标记 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ArchiveEntry:
    """一条**当前生效**的归档记录。

    ``ref`` 是平面事件 id：报告里能回跳到具体那一条事件（"谁在什么时候归档的"）。
    """

    session_id: str
    archived_at: str
    reason: str = ""
    ref: str = ""
    messages: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "archived_at": self.archived_at,
            "reason": self.reason,
            "ref": self.ref,
            "messages": self.messages,
        }


class SessionArchive:
    """会话归档（只打标记）——不删任何消息。

    ``plane`` 可注入：测试里想复用同一个平面实例，或者只想观察日志而不落盘。
    """

    def __init__(self, data_dir: Path, *, plane: SessionPlane | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.plane = plane or SessionPlane(self.data_dir)

    # ── 写 ──
    def archive(
        self,
        session_id: str,
        *,
        reason: str = "",
        messages: int = 0,
        ts: str = "",
    ) -> dict[str, Any]:
        """给会话打归档标记，返回**落盘的那条平面事件**。

        幂等：已经归档时返回既有事件、不再追加（否则反复 ``sessions archive`` 会把
        平面日志撑成一堆同义事件，"最近一次归档是什么时候"反而看不出来）。

        ``messages`` 只用于审计（"归档时共有 N 条消息"），由调用方数好传进来——
        本方法**不读也不写 ``messages`` 表**，这是"归档不删"的硬保证。
        """
        target = _require_id(session_id)
        current = self.entry(target)
        if current is not None:
            return self._event(target, KIND_ARCHIVED) or current.as_dict()
        event: dict[str, Any] = {
            "kind": KIND_ARCHIVED,
            "session_id": target,
            "archived_at": ts or "",
            "reason": str(reason or ""),
            "messages": int(messages or 0),
            "summary": f"归档会话 {target}" + (f"（{reason}）" if reason else ""),
        }
        if not event["archived_at"]:
            # 时间由平面在追加时补（它写的 ts 与 messages.created_at 同格式）。
            event.pop("archived_at")
        return self.plane.append(event)

    def unarchive(self, session_id: str, *, reason: str = "", ts: str = "") -> dict[str, Any]:
        """取消归档标记（同样不碰消息）。未归档的会话调用它是无操作。"""
        target = _require_id(session_id)
        if self.entry(target) is None:
            raise ArchiveError(f"会话未归档，无需取消：{target}")
        event: dict[str, Any] = {
            "kind": KIND_UNARCHIVED,
            "session_id": target,
            "reason": str(reason or ""),
            "summary": f"取消归档 {target}" + (f"（{reason}）" if reason else ""),
        }
        if ts:
            event["ts"] = ts
        return self.plane.append(event)

    # ── 读 ──
    def entry(self, session_id: str) -> ArchiveEntry | None:
        """该会话**当前**的归档记录（未归档返回 ``None``）。"""
        return self.entries().get(str(session_id or "").strip())

    def is_archived(self, session_id: str) -> bool:
        return self.entry(session_id) is not None

    def entries(self) -> dict[str, ArchiveEntry]:
        """全部**当前生效**的归档记录，``session_id`` → 记录。

        后写覆盖先写：``session.unarchived`` 会把该会话从字典里摘掉。只追加日志里
        没有"更新"语义，所以"当前状态"必须由最后一条事件推导——这正是把状态读成
        投影而不是写成字段的原因（写字段就得多一次 UPDATE 和一把写锁）。
        """
        state: dict[str, ArchiveEntry] = {}
        for item in self.plane.log.read_all():
            kind = str(item.get("kind") or "")
            session_id = str(item.get("session_id") or "").strip()
            if not session_id:
                continue
            if kind == KIND_ARCHIVED:
                state[session_id] = ArchiveEntry(
                    session_id=session_id,
                    archived_at=str(item.get("archived_at") or item.get("ts") or ""),
                    reason=str(item.get("reason") or ""),
                    ref=str(item.get("id") or ""),
                    messages=_as_int(item.get("messages")),
                )
            elif kind == KIND_UNARCHIVED:
                state.pop(session_id, None)
        return state

    def list_archived(self, *, limit: int = 0) -> list[ArchiveEntry]:
        """已归档会话，**最近归档的在前**（按 ``archived_at`` 倒序，同刻按事件 id 倒序）。"""
        items = list(self.entries().values())
        items.sort(key=lambda item: (item.archived_at, _as_int(item.ref)), reverse=True)
        if limit and limit > 0:
            return items[:limit]
        return items

    def _event(self, session_id: str, kind: str) -> dict[str, Any] | None:
        """该会话最后一条指定类型的事件（幂等返回既有事件时用）。"""
        found: dict[str, Any] | None = None
        for item in self.plane.events(session_id):
            if str(item.get("kind") or "") == kind:
                found = item
        return found


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# ── 系统提示词内容寻址 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class PromptStats:
    """内容寻址的账：多少份文件、多少次绑定、省下多少。"""

    files: int = 0
    bindings: int = 0
    saved_files: int = 0
    saved_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "bindings": self.bindings,
            "saved_files": self.saved_files,
            "saved_bytes": self.saved_bytes,
        }


class PromptStore:
    """``system_prompts`` 的 Orbit 版：``sha256`` → 文件，任务只记 hash。

    ``plane`` 可注入（理由同 :class:`SessionArchive`）。任务 → hash 的映射记在平面
    日志的 ``prompt.frozen`` 事件里：追加日志天然"后写覆盖先写"（同一任务重跑时新
    hash 生效），而且不占禁改区的表结构。
    """

    def __init__(self, data_dir: Path, *, plane: SessionPlane | None = None) -> None:
        self.data_dir = Path(data_dir)
        self.plane = plane or SessionPlane(self.data_dir)

    # ── 路径 ──
    @property
    def root(self) -> Path:
        return self.data_dir / PROMPTS_DIRNAME

    def path_for(self, digest: str) -> Path:
        """hash → 文件路径。hash 必须先过格式校验（见 :func:`digest_of` 的注释）。"""
        clean = str(digest or "").strip().lower()
        if len(clean) != 64 or any(char not in "0123456789abcdef" for char in clean):
            raise ArchiveError(f"不是合法的 sha256：{digest!r}（应为 64 位十六进制）")
        return self.root / f"{clean}{PROMPT_SUFFIX}"

    # ── 写 ──
    def put(self, prompt: str) -> str:
        """存一份提示词正文，返回 sha256。**同一内容只落一份文件**。

        hash 算在 UTF-8 字节上（不是字符串）：``read`` 会重新读字节再算一遍，
        只有这样才可能发现"文件被编辑器改成了另一套换行"这种改动。
        """
        data = (prompt or "").encode("utf-8")
        if not data:
            raise ArchiveError("提示词正文不能为空（空提示词存下来只会制造一个空文件）")
        digest = hashlib.sha256(data).hexdigest()
        target = self.path_for(digest)
        if target.exists():
            # 内容寻址的幂等性就落在这三行：同名文件已在盘上，直接复用。
            if target.read_bytes() != data:
                raise PromptMismatch(
                    f"{target} 的内容与文件名（sha256）不符：文件被改过或写到一半"
                )
            return digest
        target.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换：直接写目标文件时若进程被杀，会留下一个"内容不完整
        # 但名字已经存在"的文件，之后 put() 会误判为"已存过"而永远返回坏内容。
        tmp = target.with_name(f".{digest}{PROMPT_SUFFIX}.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)
        return digest

    def bind(
        self,
        task_id: str,
        prompt: str,
        *,
        session_id: str = "",
        model: str = "",
    ) -> str:
        """冻结某任务的系统提示：正文落盘 + 平面日志记 ``task_id → hash``。"""
        target = _require_id(task_id, "任务")
        digest = self.put(prompt)
        self._record(target, digest, session_id=session_id, model=model, size=len(prompt.encode("utf-8")))
        return digest

    def bind_hash(self, task_id: str, digest: str, *, session_id: str = "", model: str = "") -> str:
        """只登记"某任务用了某份已存在的提示词"（正文已由别的任务存过）。

        这条路径是给"从 Hermes 的 ``system_prompts`` 表导入"准备的：那边正文已经
        按 hash 存好了，导过来只需把 hash 挂到任务上。
        """
        target = _require_id(task_id, "任务")
        path = self.path_for(digest)
        if not path.exists():
            raise ArchiveError(f"提示词不存在：{path}（先 put() 正文，或检查 hash 是否写错）")
        clean = str(digest).strip().lower()
        self._record(target, clean, session_id=session_id, model=model, size=path.stat().st_size)
        return clean

    def _record(
        self,
        task_id: str,
        digest: str,
        *,
        session_id: str,
        model: str,
        size: int,
    ) -> dict[str, Any]:
        return self.plane.append(
            {
                "kind": KIND_PROMPT_FROZEN,
                "task_id": task_id,
                "hash": digest,
                "bytes": int(size),
                "session_id": str(session_id or ""),
                "model": str(model or ""),
                "summary": f"冻结系统提示 {digest[:12]}（{int(size)} 字节）",
            }
        )

    # ── 读 ──
    def read(self, digest: str) -> str:
        """按 hash 取回正文，并**校验内容摘要**（不符抛 :class:`PromptMismatch`）。"""
        path = self.path_for(digest)
        if not path.exists():
            raise ArchiveError(f"提示词不存在：{path}")
        data = path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != path.stem:
            raise PromptMismatch(
                f"{path.name} 的内容摘要对不上：文件名 {path.stem}，实际 {actual}"
                "（文件被手改 / 被截断 / 落到一半）。拒绝返回内容——这一层的意义就是可追溯。"
            )
        return data.decode("utf-8", errors="replace")

    def hash_for(self, task_id: str) -> str:
        """该任务冻结的提示词 hash（没冻结过返回空串，不报错）。"""
        target = str(task_id or "").strip()
        if not target:
            return ""
        found = ""
        for item in self.plane.log.read_all():
            if str(item.get("kind") or "") != KIND_PROMPT_FROZEN:
                continue
            if str(item.get("task_id") or "") != target:
                continue
            candidate = str(item.get("hash") or "").strip()
            if candidate:
                found = candidate  # 后写覆盖先写：重跑一次任务时新 hash 才是当前值
        return found

    def prompt_for(self, task_id: str) -> str:
        """按任务 id 取回它当时冻结的系统提示。"""
        digest = self.hash_for(task_id)
        if not digest:
            raise ArchiveError(f"任务没有冻结过系统提示：{task_id}（先用 bind() 存一次）")
        return self.read(digest)

    def tasks(self) -> dict[str, str]:
        """``task_id → hash`` 的当前投影（同任务取最后一条）。"""
        mapping: dict[str, str] = {}
        for item in self.plane.log.read_all():
            if str(item.get("kind") or "") != KIND_PROMPT_FROZEN:
                continue
            task_id = str(item.get("task_id") or "").strip()
            digest = str(item.get("hash") or "").strip()
            if task_id and digest:
                mapping[task_id] = digest
        return mapping

    def files(self) -> list[Path]:
        """盘上现有的全部提示词文件（按名字排序，便于测试断言）。"""
        if not self.root.exists():
            return []
        return sorted(self.root.glob(f"*{PROMPT_SUFFIX}"))

    def stats(self) -> PromptStats:
        """省了多少：``bindings`` 次绑定 vs ``files`` 份文件。"""
        mapping = self.tasks()
        files = {path.stem: path.stat().st_size for path in self.files()}
        bound_bytes = 0
        for digest in mapping.values():
            bound_bytes += files.get(digest, 0)
        saved_files = max(0, len(mapping) - len(set(mapping.values())))
        saved_bytes = max(0, bound_bytes - sum(files.values()))
        return PromptStats(
            files=len(files),
            bindings=len(mapping),
            saved_files=saved_files,
            saved_bytes=saved_bytes,
        )


__all__ = [
    "PROMPTS_DIRNAME",
    "PROMPT_SUFFIX",
    "ArchiveEntry",
    "ArchiveError",
    "PromptMismatch",
    "PromptStats",
    "PromptStore",
    "SessionArchive",
]
