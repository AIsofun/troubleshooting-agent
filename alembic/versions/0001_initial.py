"""Initial schema: agent_traces and agent_cases tables.

Revision ID: 0001
Revises:
Create Date: 2026-05-09 00:00:00.000000

"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── agent_traces ──────────────────────────────────────────
    op.create_table(
        "agent_traces",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column("site_id", sa.String(64), nullable=True),
        sa.Column("user_query", sa.Text, nullable=False),
        sa.Column("intent", sa.String(128), nullable=True),
        sa.Column("retrieved_cases", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("tool_calls", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("agent_suggestion", sa.Text, nullable=True),
        sa.Column("final_answer", postgresql.JSONB, nullable=True),
        sa.Column("elapsed_sec", sa.Float, nullable=True),
        sa.Column("engineer_action", sa.Text, nullable=True),
        sa.Column("final_outcome", sa.String(32), nullable=True, server_default="pending"),
        sa.Column("feedback_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("human_verified", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("candidate_generated", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("candidate_path", sa.String(512), nullable=True),
    )
    op.create_index("ix_agent_traces_trace_id", "agent_traces", ["trace_id"], unique=True)
    op.create_index("ix_agent_traces_intent", "agent_traces", ["intent"])
    op.create_index("ix_agent_traces_outcome", "agent_traces", ["final_outcome"])
    op.create_index("ix_agent_traces_created_at", "agent_traces", ["created_at"])

    # ── agent_cases ───────────────────────────────────────────
    op.create_table(
        "agent_cases",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=False),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
        ),
        sa.Column("case_id", sa.String(64), nullable=False),
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
        sa.Column("source_trace_id", sa.String(64), nullable=True),
        sa.Column("case_status", sa.String(32), nullable=False, server_default="candidate"),
        sa.Column("site_type", sa.String(64), nullable=True),
        sa.Column("station_type", sa.String(64), nullable=True),
        sa.Column("product_type", sa.String(64), nullable=True),
        sa.Column("symptom", sa.Text, nullable=False),
        sa.Column("alarm_code", sa.String(64), nullable=True),
        sa.Column("device_context", postgresql.JSONB, nullable=True),
        sa.Column("evidence", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("root_cause", sa.Text, nullable=True),
        sa.Column("solution", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("verified_result", sa.Text, nullable=True),
        sa.Column("applicability", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("tags", postgresql.JSONB, nullable=False, server_default="[]"),
        sa.Column("risk_level", sa.String(16), nullable=False, server_default="medium"),
        sa.Column("human_verified", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("sensitive_level", sa.String(32), nullable=False, server_default="internal"),
        sa.Column("knowledge_pack_version", sa.String(32), nullable=True),
        sa.Column("full_doc", postgresql.JSONB, nullable=True),
    )
    op.create_index("ix_agent_cases_case_id", "agent_cases", ["case_id"], unique=True)
    op.create_index("ix_agent_cases_status", "agent_cases", ["case_status"])
    op.create_index("ix_agent_cases_alarm_code", "agent_cases", ["alarm_code"])
    op.create_index("ix_agent_cases_site_type", "agent_cases", ["site_type"])


def downgrade() -> None:
    op.drop_table("agent_cases")
    op.drop_table("agent_traces")
