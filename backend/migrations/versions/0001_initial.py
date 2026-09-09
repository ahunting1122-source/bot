"""Initial Orbit schema."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None


def upgrade():
    op.create_table(
        "conversations",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("user_id", sa.String, nullable=False),
        sa.Column("title", sa.String, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
    )
    op.create_table(
        "messages",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("conversation_id", sa.String, sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("role", sa.String, nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
    )
    op.create_table(
        "tasks",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("conversation_id", sa.String, sa.ForeignKey("conversations.id"), nullable=False),
        sa.Column("goal", sa.Text, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("waiting", sa.JSON, nullable=False),
        sa.Column("error", sa.Text, nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
        sa.Column("updated_at", sa.String, nullable=False),
    )
    op.create_table(
        "task_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("task_id", sa.String, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("event_type", sa.String, nullable=False),
        sa.Column("payload", sa.JSON, nullable=False),
        sa.Column("created_at", sa.String, nullable=False),
    )
    op.create_table(
        "memories",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("user_id", sa.String, nullable=False),
        sa.Column("key", sa.String(160), nullable=False, unique=True),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("source", sa.String, nullable=False),
        sa.Column("updated_at", sa.String, nullable=False),
    )
    op.create_table(
        "tool_calls",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("task_id", sa.String, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("name", sa.String, nullable=False),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("args", sa.JSON, nullable=False),
        sa.Column("result", sa.JSON, nullable=False),
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("task_id", sa.String, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("kind", sa.String, nullable=False),
        sa.Column("location", sa.String, nullable=False),
        sa.Column("label", sa.String, nullable=False),
    )
    for table, column in [
        ("messages", "conversation_id"),
        ("tasks", "conversation_id"),
        ("task_events", "task_id"),
        ("tool_calls", "task_id"),
        ("artifacts", "task_id"),
    ]:
        op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade():
    for table in [
        "artifacts",
        "tool_calls",
        "memories",
        "task_events",
        "tasks",
        "messages",
        "conversations",
    ]:
        op.drop_table(table)
