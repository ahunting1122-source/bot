import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: 同义 key 判定的相似度阈值（字符 bigram Jaccard）。
#: 实测样本：「报告格式偏好」vs「报告偏好与校验码」= 0.43、「报告偏好与校验码」
#: vs「报告校验码与格式偏好」= 0.375——措辞相近但在阈值以下，所以不只靠相似度，
#: 还叠加"核心词包含"这一条（见 `synonym_key`）。
SYNONYM_SIMILARITY = 0.6


def _grams(value: str, size: int = 2) -> set[str]:
    value = (value or "").strip().casefold()
    if len(value) <= size:
        return {value} if value else set()
    return {value[index : index + size] for index in range(len(value) - size + 1)}


def _normalize(value: str) -> str:
    """归一化 key 用于全等比较：只保留字母数字并折叠大小写。

    `casefold()` 不能省：`ReportFormat` 与 `reportformat` 是同一个键，
    漏掉会让英文键重复写入。
    """
    return "".join(ch for ch in (value or "") if ch.isalnum()).casefold()


def similarity(left: str, right: str, size: int = 2) -> float:
    """字符 bigram 的 Jaccard 相似度（不引入分词依赖，中英文都适用）。"""
    first, second = _grams(left, size), _grams(right, size)
    union = first | second
    return len(first & second) / len(union) if union else 0.0


def synonym_key(candidate: str, existing: list[str]) -> str:
    """找出与 `candidate` 同义的现有 key，没有则返回空串。

    判定两条（任一成立即算同义）：
      1. 归一化后完全相等（去掉空格、标点、大小写差异）；
      2. 字符 bigram 相似度 ≥ `SYNONYM_SIMILARITY`。

    这样「报告格式偏好」「报告 格式 偏好」会被合并；而语义相近但措辞差得远的
    （如「报告格式偏好」vs「报告偏好与校验码」）不会被误合并——后者交给
    提示词层面约束（见 `tools.MemoryArgs.key`），不做激进归一，避免把不同
    事实错误地合并成一条。
    """
    candidate = (candidate or "").strip()
    if not candidate:
        return ""
    normalized = _normalize(candidate)

    best, best_score = "", 0.0
    for name in existing:
        if not name:
            continue
        if _normalize(name) == normalized:
            return name
        score = similarity(candidate, name)
        if score > best_score:
            best, best_score = name, score
    return best if best_score >= SYNONYM_SIMILARITY else ""


def uid() -> str:
    return str(uuid4())


def now() -> str:
    return datetime.now(UTC).isoformat()


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(default="local")
    title: Mapped[str] = mapped_column(default="新对话")
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(default=now)
    updated_at: Mapped[str] = mapped_column(default=now)
    message_revision: Mapped[int] = mapped_column(Integer, default=0)
    summary_status: Mapped[str] = mapped_column(default="empty")
    summary_error: Mapped[str] = mapped_column(Text, default="")
    summary_updated_at: Mapped[str] = mapped_column(default="")
    summary_message_id: Mapped[str] = mapped_column(default="")
    summary_message_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 摘要层累计开销（M5）。此前摘要调模型的 token 与耗时完全在三层预算之外，
    #: 任务判"成功"而真实花销已经越线。这两列让它可汇总、可见、可断言。
    summary_tokens: Mapped[int] = mapped_column(Integer, default=0)
    summary_cost_seconds: Mapped[float] = mapped_column(default=0.0)


class Message(Base):
    __tablename__ = "messages"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    role: Mapped[str]
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[str] = mapped_column(default=now)


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(default="PENDING")
    waiting: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(default=now)
    updated_at: Mapped[str] = mapped_column(default=now)


class TaskEvent(Base):
    __tablename__ = "task_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    event_type: Mapped[str]
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[str] = mapped_column(default=now)


class Memory(Base):
    """跨对话长期记忆（key-value）。

    `source` 记录写入来源（用户的 task_id 或 "user"），`hit_count` / `last_used_at`
    记录这条记忆有没有真的被注入进上下文——没有这两个字段就无法回答"哪条记忆是
    死数据"。清理依据也来自它们（长期 0 命中的记忆可以安全删除）。
    """

    __tablename__ = "memories"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(default="local")
    key: Mapped[str] = mapped_column(String(160), unique=True)
    value: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(default="user")
    #: 被注入进上下文的次数（`build_context` 每次注入时累加）。
    hit_count: Mapped[int] = mapped_column(Integer, default=0)
    #: 上次被注入的时间；空串表示从未被注入过。
    last_used_at: Mapped[str] = mapped_column(default="")
    updated_at: Mapped[str] = mapped_column(default=now)


class Profile(Base):
    """用户画像索引（正文真值在 `data/profile/USER.md`，本表只镜像元数据）。

    与 `Memory` 的分工：`memories` 是**按条召回**的事实/偏好（key-value 合适），
    本表服务**整段注入**的画像（"这个人是谁、在做什么、习惯怎么协作"）。
    两者混在一张表里会互相污染——画像塞进任何一条 key-value 都会丢上下文。

    单行表（`user_id` 唯一）：v1 只有一个本地用户，不需要多行；留 `user_id`
    是为了将来多 profile 时不必改表结构。
    """

    __tablename__ = "profiles"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(default="local", index=True)
    #: 冗余一份正文，便于画像页不读文件就知道还剩多少空间。
    body: Mapped[str] = mapped_column(Text, default="")
    entries: Mapped[int] = mapped_column(Integer, default=0)
    chars: Mapped[int] = mapped_column(Integer, default=0)
    max_chars: Mapped[int] = mapped_column(Integer, default=1600)
    updated_at: Mapped[str] = mapped_column(default="")


class Skill(Base):
    """技能索引与统计（二期沉淀/治理的落点）。

    `data/skills/<name>/SKILL.md` 是正文真值，本表只是索引：name 唯一、triggers 存 JSON。
    body 冗余一份正文便于统计与查重，path 指向文件。usage_count / success_rate / failures
    是二期的治理输入，一期先把表建好、随 catalog 同步（幂等 upsert）。
    status 一期只标文件存活态（active/archived）；治理审核态等二期再单独加字段。
    """

    __tablename__ = "skills"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    name: Mapped[str] = mapped_column(String(120), index=True)
    #: 来源：local（文件技能，默认）或 mcp:<服务>（外部 prompts）。与 name 组成复合唯一。
    source: Mapped[str] = mapped_column(String(160), default="local")
    scope: Mapped[str] = mapped_column(default="global")
    triggers: Mapped[list] = mapped_column(JSON, default=list)
    version: Mapped[int] = mapped_column(Integer, default=0)
    body: Mapped[str] = mapped_column(Text, default="")
    path: Mapped[str] = mapped_column(default="")
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    success_rate: Mapped[float] = mapped_column(default=0.0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    # 二期治理：连续失败计数（满阈值标 stale）、上次使用时间（超期归档）。
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[str] = mapped_column(default="")
    # status 状态机（一期只标文件存活态 active/archived，二期扩成治理态）：
    #   unreviewed（新沉淀待确认，不注入）→ active（在用）→ stale（连续失败降权，排序垫底）→ archived（文件删/超期/低成功率，不注入）
    status: Mapped[str] = mapped_column(default="active")
    created_at: Mapped[str] = mapped_column(default=now)
    updated_at: Mapped[str] = mapped_column(default=now)


class Operation(Base):
    __tablename__ = "tool_calls"
    id: Mapped[str] = mapped_column(primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    name: Mapped[str]
    status: Mapped[str] = mapped_column(default="RUNNING")
    args: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict] = mapped_column(JSON, default=dict)


class Artifact(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    kind: Mapped[str]
    location: Mapped[str]
    label: Mapped[str]


class ToolResult(Base):
    """原文落地与观测：控制流仍归 tool_calls，这里只存"模型看到了什么、多大"。

    operation_id 唯一：同一 operation_id 重跑（恢复、重试）只落一条，checksum 判重。
    """

    __tablename__ = "tool_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(default="ok")
    host: Mapped[str] = mapped_column(default="")
    text: Mapped[str] = mapped_column(Text, default="")
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    truncated: Mapped[int] = mapped_column(Integer, default=0)
    hash: Mapped[str] = mapped_column(default="")
    created_at: Mapped[str] = mapped_column(default=now)


class Chunk(Base):
    """可检索片段。row_id 自增主键兼作 FTS5 external content 的 rowid。

    char_text 是逐字空格切分的副本，给 unicode61 那一路用：trigram 对 2 字中文查询
    返回 0 条（'键盘' 命中不了），字符级那一路能命中任意长度子串。
    """

    __tablename__ = "chunks"
    row_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source_type: Mapped[str] = mapped_column(String(32))
    source_id: Mapped[str] = mapped_column(default="", index=True)
    conversation_id: Mapped[str] = mapped_column(default="", index=True)
    task_id: Mapped[str] = mapped_column(default="", index=True)
    host: Mapped[str] = mapped_column(default="", index=True)
    ordinal: Mapped[int] = mapped_column(Integer, default=0)
    text: Mapped[str] = mapped_column(Text)
    char_text: Mapped[str] = mapped_column(Text, default="")
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    hash: Mapped[str] = mapped_column(String(64), default="", index=True)
    origin: Mapped[str] = mapped_column(default="")
    title_path: Mapped[str] = mapped_column(default="")
    created_at: Mapped[str] = mapped_column(default=now)


def serialize(row: Base) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


class Store:
    def __init__(self, path):
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{path}", connect_args={"timeout": 20}
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.lock = asyncio.Lock()
        self.wal_enabled = False

    async def tune(self):
        """WAL lets the long-lived SSE reader run while tasks keep writing.

        Without it a read transaction and a write commit collide and every later write
        fails with "database is locked".
        """
        async with self.sessions() as session:
            await session.execute(text("PRAGMA journal_mode=WAL"))
            await session.execute(text("PRAGMA synchronous=NORMAL"))
            await session.execute(text("PRAGMA busy_timeout=20000"))
        self.wal_enabled = True

    async def get(self, model, key):
        async with self.sessions() as session:
            return await session.get(model, key)

    async def rows(self, statement):
        async with self.sessions() as session:
            return list((await session.scalars(statement)).all())

    async def save(self, row):
        async with self.lock, self.sessions() as session:
            message_changed = False
            if isinstance(row, Message):
                previous = await session.get(Message, row.id) if row.id else None
                message_changed = previous is None or (
                    previous.content != row.content or previous.role != row.role
                )
            saved = await session.merge(row)
            if message_changed:
                conversation = await session.get(Conversation, row.conversation_id)
                if conversation:
                    conversation.updated_at = now()
                    conversation.message_revision += 1
                    if conversation.summary_message_count or conversation.summary_status == "pending":
                        conversation.summary_status = "pending"
                    conversation.summary_error = ""
            await session.commit()
            return saved

    async def event(self, task_id: str, kind: str, **payload):
        return await self.save(TaskEvent(task_id=task_id, event_type=kind, payload=payload))

    async def transition(self, task_id: str, status: str, **fields):
        async with self.lock, self.sessions() as session:
            task = await session.get(Task, task_id)
            if not task or task.status == "CANCELLED":
                return
            task.status, task.updated_at = status, now()
            for key, value in fields.items():
                setattr(task, key, value)
            await session.commit()

    async def set_memory(self, key: str, value: str, source: str = "user"):
        """写入或更新一条长期记忆。

        除精确 key 命中外，还会用 `synonym_key` 找同义 key 并**更新那一条**——
        模型给的 key 措辞每次可能不同（实测出现过 7 种写法指同一条信息），
        只靠 key 唯一约束挡不住，所以这里做兜底合并。
        返回值是 (记忆行, 是否合并到了别的 key)。
        """
        async with self.lock, self.sessions() as session:
            memory = await session.scalar(select(Memory).where(Memory.key == key))
            merged_from = ""
            if memory is None:
                existing = list((await session.scalars(select(Memory.key))).all())
                target = synonym_key(key, existing)
                if target:
                    memory = await session.scalar(select(Memory).where(Memory.key == target))
                    merged_from = target
            if memory is None:
                memory = Memory(key=key, value=value, source=source)
                session.add(memory)
            else:
                # 保留原有稳定 key；只在完全没有旧值时更新其余字段
                memory.value, memory.source, memory.updated_at = value, source, now()
            await session.commit()
            return memory, merged_from
