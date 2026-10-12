"""skills 表：技能索引与统计（Skill 层二期的落点，一期先建表）。

`data/skills/<name>/SKILL.md` 是正文真值，本表是索引：name 唯一、triggers 存 JSON。
usage_count / success_rate / failures / status 是二期治理（写入查重、注入择优、降级淘汰）
的输入；一期只随 catalog 同步（幂等 upsert），不做治理逻辑。
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"


def upgrade():
    op.create_table(
        "skills",
        sa.Column("id", sa.String, primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("scope", sa.String, nullable=False, server_default="global"),
        sa.Column("triggers", sa.JSON, nullable=False),
        sa.Column("version", sa.Integer, nullable=False, server_default="0"),
        sa.Column("body", sa.Text, nullable=False, server_default=""),
        sa.Column("path", sa.String, nullable=False, server_default=""),
        sa.Column("usage_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("success_rate", sa.Float, nullable=False, server_default="0"),
        sa.Column("failures", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String, nullable=False, server_default="unreviewed"),
        sa.Column("created_at", sa.String, nullable=False, server_default=""),
        sa.Column("updated_at", sa.String, nullable=False, server_default=""),
    )
    op.create_index("ix_skills_name", "skills", ["name"], unique=True)


def downgrade():
    op.drop_index("ix_skills_name", table_name="skills")
    op.drop_table("skills")
