"""conversations 表加 source 列（Hermes `--source` 的会话来源标签，MATRIX 10.x）。

Hermes 语义（调研 `01-cli-surface.md:181`）：
> `--source SOURCE` | 文本 | `cli` | **会话来源标签，用于过滤**。
> 第三方集成可用 `tool`，**使其不出现在用户会话列表**。

Orbit 之前**没有这个字段**，所以 `--source` 只能记进任务事件——那是**有真实效果但语义不完整**。
这条补上数据模型，让「按来源筛选」成立。
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"


def upgrade():
    # 默认 `cli`：既有会话都是命令行产生的，回填这个值不会改变它们的语义。
    op.add_column(
        "conversations",
        sa.Column("source", sa.String(80), nullable=False, server_default="cli"),
    )
    # 加索引是因为它的**唯一用途就是过滤**——不加索引，筛选会全表扫。
    op.create_index("ix_conversations_source", "conversations", ["source"])


def downgrade():
    op.drop_index("ix_conversations_source", table_name="conversations")
    op.drop_column("conversations", "source")
