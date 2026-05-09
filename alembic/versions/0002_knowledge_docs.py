"""Add knowledge_docs table for full-text keyword search.

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-09 01:00:00.000000
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "knowledge_docs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        # 业务主键：由导入层用 sha256 生成，确保幂等
        sa.Column("doc_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        # ── 内容字段 ──
        sa.Column("title", sa.Text, nullable=False, server_default=""),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        sa.Column("source", sa.Text, nullable=True),
        sa.Column("doc_type", sa.String(32), nullable=False, server_default="manual"),
        sa.Column("tags", postgresql.JSONB, nullable=False, server_default="[]"),
        # ── 强关键词列（独立字段，支持精确 ILIKE 检索）──
        sa.Column("alarm_code", sa.String(128), nullable=True),
        sa.Column("device_model", sa.String(128), nullable=True),
        sa.Column("product_type", sa.String(128), nullable=True),
        # ── 知识包版本 ──
        sa.Column("knowledge_pack_version", sa.String(32), nullable=True),
        # ── 全文检索向量列（由 trigger 自动维护）──
        sa.Column(
            "search_vector",
            postgresql.TSVECTOR,
            nullable=True,
        ),
    )

    # 唯一约束
    op.create_index("uq_knowledge_docs_doc_id", "knowledge_docs", ["doc_id"], unique=True)

    # 检索索引
    op.create_index("ix_knowledge_docs_alarm_code", "knowledge_docs", ["alarm_code"])
    op.create_index("ix_knowledge_docs_device_model", "knowledge_docs", ["device_model"])
    op.create_index("ix_knowledge_docs_doc_type", "knowledge_docs", ["doc_type"])
    op.create_index("ix_knowledge_docs_kp_version", "knowledge_docs", ["knowledge_pack_version"])

    # GIN 全文检索索引（对 search_vector 列）
    op.execute(
        "CREATE INDEX ix_knowledge_docs_search_vector "
        "ON knowledge_docs USING gin(search_vector)"
    )

    # Trigger：自动更新 search_vector（title + content + alarm_code + device_model）
    op.execute("""
        CREATE OR REPLACE FUNCTION knowledge_docs_search_vector_update()
        RETURNS trigger AS $$
        BEGIN
            NEW.search_vector :=
                to_tsvector('simple',
                    coalesce(NEW.title, '') || ' ' ||
                    coalesce(NEW.content, '') || ' ' ||
                    coalesce(NEW.alarm_code, '') || ' ' ||
                    coalesce(NEW.device_model, '') || ' ' ||
                    coalesce(NEW.product_type, '')
                );
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    op.execute("""
        CREATE TRIGGER trig_knowledge_docs_search_vector
        BEFORE INSERT OR UPDATE ON knowledge_docs
        FOR EACH ROW EXECUTE FUNCTION knowledge_docs_search_vector_update();
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trig_knowledge_docs_search_vector ON knowledge_docs")
    op.execute("DROP FUNCTION IF EXISTS knowledge_docs_search_vector_update")
    op.drop_table("knowledge_docs")
