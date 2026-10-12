"""skills 表加治理字段：consecutive_failures / last_used_at。

二期降级淘汰的输入：连续失败计数（满阈值标 stale）、上次使用时间（超期归档）。
`0006` 里 status 的 server_default 误写成 unreviewed，本迁移顺手对齐为 active
（一期语义：文件在盘即 active；治理审核态在应用层维护，不依赖 DB 默认值）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"


def upgrade():
    op.add_column(
        "skills",
        sa.Column("consecutive_failures", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "skills",
        sa.Column("last_used_at", sa.String, nullable=False, server_default=""),
    )


def downgrade():
    op.drop_column("skills", "last_used_at")
    op.drop_column("skills", "consecutive_failures")
