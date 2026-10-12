"""独立用户画像层 + 摘要开销入账。

## 1. 为什么画像要单独一层

原先把"事实/偏好"（报告用什么格式）和"这个人是谁"（在做什么项目、习惯怎么协作）
都塞进 `memories` 的 key-value，结果两类东西互相污染：画像需要**整段一起注入**才
有意义，而 key-value 天生是"按条召回"。Hermes 明确把 recall 与 user modeling 拆开，
并说混为一谈是很多 agent 设计出错的根源。本迁移落这一层。

`data/profile/USER.md` 是正文真值（人类可读可改可删），`profiles` 表只是**索引**：
条目数、字符数、上限、更新时间。

## 2. 为什么摘要开销要入账

摘要由 `SummaryService` 后台另起一次模型调用生成，其 token 与耗时此前完全游离在
三层预算（动作/token/时间）之外——任务按预算判"成功结束"，而真实花费已经越线。
两个字段让"预算看到的数"和"账单上的数"能对上。

不抢 `0008`（已被审计时间线/存储收敛预定），本迁移编号 `0010`。
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"


def upgrade():
    op.create_table(
        "profiles",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("user_id", sa.String, nullable=False, server_default="local", index=True),
        sa.Column("body", sa.Text, nullable=False, server_default=""),
        sa.Column("entries", sa.Integer, nullable=False, server_default="0"),
        sa.Column("chars", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_chars", sa.Integer, nullable=False, server_default="1600"),
        sa.Column("updated_at", sa.String, nullable=False, server_default=""),
    )
    op.add_column(
        "conversations",
        sa.Column("summary_tokens", sa.Integer, nullable=False, server_default="0"),
    )
    op.add_column(
        "conversations",
        sa.Column("summary_cost_seconds", sa.Float, nullable=False, server_default="0"),
    )


def downgrade():
    op.drop_column("conversations", "summary_cost_seconds")
    op.drop_column("conversations", "summary_tokens")
    op.drop_table("profiles")

