"""Track generated conversation summaries separately from legacy answer excerpts."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"


def upgrade():
    columns = [
        sa.Column("updated_at", sa.String, nullable=False, server_default=""),
        sa.Column("message_revision", sa.Integer, nullable=False, server_default="0"),
        sa.Column("summary_status", sa.String, nullable=False, server_default="empty"),
        sa.Column("summary_error", sa.Text, nullable=False, server_default=""),
        sa.Column("summary_updated_at", sa.String, nullable=False, server_default=""),
        sa.Column("summary_message_id", sa.String, nullable=False, server_default=""),
        sa.Column("summary_message_count", sa.Integer, nullable=False, server_default="0"),
    ]
    for column in columns:
        op.add_column("conversations", column)
    op.execute(
        sa.text(
            "UPDATE conversations SET updated_at = COALESCE("
            "(SELECT MAX(created_at) FROM messages WHERE conversation_id = conversations.id),"
            "created_at), message_revision = "
            "(SELECT COUNT(*) FROM messages WHERE conversation_id = conversations.id)"
        )
    )


def downgrade():
    with op.batch_alter_table("conversations") as batch:
        for name in [
            "summary_message_count", "summary_message_id", "summary_updated_at", "summary_error",
            "summary_status", "message_revision", "updated_at",
        ]:
            batch.drop_column(name)
