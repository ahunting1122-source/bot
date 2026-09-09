import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


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
    __tablename__ = "memories"
    id: Mapped[str] = mapped_column(primary_key=True, default=uid)
    user_id: Mapped[str] = mapped_column(default="local")
    key: Mapped[str] = mapped_column(String(160), unique=True)
    value: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(default="user")
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


def serialize(row: Base) -> dict[str, Any]:
    return {column.name: getattr(row, column.name) for column in row.__table__.columns}


class Store:
    def __init__(self, path):
        self.engine = create_async_engine(
            f"sqlite+aiosqlite:///{path}", connect_args={"timeout": 20}
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.lock = asyncio.Lock()

    async def get(self, model, key):
        async with self.sessions() as session:
            return await session.get(model, key)

    async def rows(self, statement):
        async with self.sessions() as session:
            return list((await session.scalars(statement)).all())

    async def save(self, row):
        async with self.lock, self.sessions() as session:
            saved = await session.merge(row)
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
        async with self.lock, self.sessions() as session:
            memory = await session.scalar(select(Memory).where(Memory.key == key))
            if memory is None:
                memory = Memory(key=key, value=value, source=source)
                session.add(memory)
            else:
                memory.value, memory.source, memory.updated_at = value, source, now()
            await session.commit()
            return memory
