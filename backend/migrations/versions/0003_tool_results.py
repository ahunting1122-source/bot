"""Tool result原文落库与观测（checksum 幂等）。

控制流仍由 tool_calls 承载，本表只回答"模型看到了什么、多大、来自哪个站点"。
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"


def upgrade():
    op.create_table(
        "tool_results",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("operation_id", sa.String(160), nullable=False),
        sa.Column("task_id", sa.String, sa.ForeignKey("tasks.id"), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String, nullable=False, server_default="ok"),
        sa.Column("host", sa.String, nullable=False, server_default=""),
        sa.Column("text", sa.Text, nullable=False, server_default=""),
        sa.Column("bytes", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("truncated", sa.Integer, nullable=False, server_default="0"),
        sa.Column("hash", sa.String, nullable=False, server_default=""),
        sa.Column("created_at", sa.String, nullable=False, server_default=""),
    )
    op.create_index("ix_tool_results_operation_id", "tool_results", ["operation_id"], unique=True)
    op.create_index("ix_tool_results_task_id", "tool_results", ["task_id"])


def downgrade():
    op.drop_index("ix_tool_results_task_id", table_name="tool_results")
    op.drop_index("ix_tool_results_operation_id", table_name="tool_results")
    op.drop_table("tool_results")
