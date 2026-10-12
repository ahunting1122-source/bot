"""memories 表加可观测字段：hit_count / last_used_at。

动机：清理真库时发现 5 条同义 key 的记忆全是评测残留，而**没有任何字段能看出
一条记忆到底有没有被用过**——没有命中计数就没有删除依据，脏数据只能靠人工识别。

- `hit_count`：被注入进上下文的次数（`context.build_context` 累加）。
- `last_used_at`：上次注入时间，空串 = 从未命中。

不抢 `0008`（已被审计时间线/存储收敛预定），本迁移编号 `0009`。
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0007"


def upgrade():
    op.add_column(
        "memories",
        sa.Column("hit_count", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "memories",
        sa.Column("last_used_at", sa.String, nullable=False, server_default=""),
    )


def downgrade():
    op.drop_column("memories", "last_used_at")
    op.drop_column("memories", "hit_count")
