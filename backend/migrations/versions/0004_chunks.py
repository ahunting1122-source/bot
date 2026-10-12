"""chunks 表与两路 FTS5 索引（trigram + 字符级）。

FTS5 虚拟表无法用 SQLAlchemy 建模，只能手写 SQL；external content 表必须自带
AI/AD/AU 三个同步触发器，否则索引与主表会静默失同步（查询慢慢变错却不报错）。
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"

TRIGRAM = "chunks_fts_trigram"
CHARS = "chunks_fts_chars"


def upgrade():
    op.create_table(
        "chunks",
        sa.Column("row_id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("id", sa.String(64), nullable=False, unique=True),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", sa.String, nullable=False, server_default=""),
        sa.Column("conversation_id", sa.String, nullable=False, server_default=""),
        sa.Column("task_id", sa.String, nullable=False, server_default=""),
        sa.Column("host", sa.String, nullable=False, server_default=""),
        sa.Column("ordinal", sa.Integer, nullable=False, server_default="0"),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("char_text", sa.Text, nullable=False, server_default=""),
        sa.Column("tokens", sa.Integer, nullable=False, server_default="0"),
        sa.Column("hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("origin", sa.String, nullable=False, server_default=""),
        sa.Column("title_path", sa.String, nullable=False, server_default=""),
        sa.Column("created_at", sa.String, nullable=False, server_default=""),
    )
    op.create_index("ix_chunks_id", "chunks", ["id"], unique=True)
    for name, columns in [
        ("ix_chunks_source_id", ["source_id"]),
        ("ix_chunks_conversation_id", ["conversation_id"]),
        ("ix_chunks_task_id", ["task_id"]),
        ("ix_chunks_host", ["host"]),
        ("ix_chunks_hash", ["hash"]),
    ]:
        op.create_index(name, "chunks", columns)

    op.execute(
        f"""CREATE VIRTUAL TABLE {TRIGRAM} USING fts5(
              text, content='chunks', content_rowid='row_id', tokenize='trigram'
            )"""
    )
    op.execute(
        f"""CREATE VIRTUAL TABLE {CHARS} USING fts5(
              char_text, content='chunks', content_rowid='row_id', tokenize='unicode61'
            )"""
    )
    # 注意：external content 的 FTS5 表自身行标识列恒为 rowid，
    # content_rowid='row_id' 只是把它映射到 chunks.row_id，触发器里必须写 rowid。
    op.execute(
        f"""CREATE TRIGGER chunks_ai AFTER INSERT ON chunks BEGIN
              INSERT INTO {TRIGRAM}(rowid, text) VALUES (new.row_id, new.text);
              INSERT INTO {CHARS}(rowid, char_text) VALUES (new.row_id, new.char_text);
            END"""
    )
    op.execute(
        f"""CREATE TRIGGER chunks_ad AFTER DELETE ON chunks BEGIN
              INSERT INTO {TRIGRAM}({TRIGRAM}, rowid, text)
                VALUES ('delete', old.row_id, old.text);
              INSERT INTO {CHARS}({CHARS}, rowid, char_text)
                VALUES ('delete', old.row_id, old.char_text);
            END"""
    )
    op.execute(
        f"""CREATE TRIGGER chunks_au AFTER UPDATE ON chunks BEGIN
              INSERT INTO {TRIGRAM}({TRIGRAM}, rowid, text)
                VALUES ('delete', old.row_id, old.text);
              INSERT INTO {CHARS}({CHARS}, rowid, char_text)
                VALUES ('delete', old.row_id, old.char_text);
              INSERT INTO {TRIGRAM}(rowid, text) VALUES (new.row_id, new.text);
              INSERT INTO {CHARS}(rowid, char_text) VALUES (new.row_id, new.char_text);
            END"""
    )


def downgrade():
    for trigger in ("chunks_au", "chunks_ad", "chunks_ai"):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.execute(f"DROP TABLE IF EXISTS {TRIGRAM}")
    op.execute(f"DROP TABLE IF EXISTS {CHARS}")
    for name in [
        "ix_chunks_hash",
        "ix_chunks_host",
        "ix_chunks_task_id",
        "ix_chunks_conversation_id",
        "ix_chunks_source_id",
    ]:
        op.drop_index(name, table_name="chunks")
    op.drop_index("ix_chunks_id", table_name="chunks")
    op.drop_table("chunks")
