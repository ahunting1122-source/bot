"""skills 表加 source 列 + (name, source) 复合唯一（Skill 多源化）。

本地文件技能 source="local"（现状），MCP prompts 技能 source="mcp:<服务>"。
同名不同来源的技能不再撞车：原来 `name` 唯一，本地技能与外部技能同名会互相覆盖。

不抢 `0008`（审计时间线预定），编号 `0011`。
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"


def upgrade():
    op.add_column(
        "skills",
        sa.Column("source", sa.String(160), nullable=False, server_default="local"),
    )
    op.drop_index("ix_skills_name", table_name="skills")
    op.create_index("ix_skills_name_source", "skills", ["name", "source"], unique=True)


def downgrade():
    op.drop_index("ix_skills_name_source", table_name="skills")
    op.create_index("ix_skills_name", "skills", ["name"], unique=True)
    op.drop_column("skills", "source")
