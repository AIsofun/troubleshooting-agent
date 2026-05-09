"""
SQLAlchemy ORM 模型。

对应 Postgres 表：
  agent_traces  — 每次 Agent 排查的完整轨迹
  agent_cases   — 已验证的正式经验案例（P4 知识包入库后使用）

与 app/cases/schema.py 的 Pydantic 模型一一对应：
  AgentTrace ↔ TraceRecord
  AgentCase  ↔ CaseRecord
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


# ────────────────────────────────────────────────────────────
# AgentTrace — 对应 agent_traces 表
# ────────────────────────────────────────────────────────────

class AgentTrace(Base):
    __tablename__ = "agent_traces"

    # ── 主键 ──
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    # 业务 ID，格式: trace_YYYYMMDD_HHMMSS_xxxxxx
    trace_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=func.now(),
    )

    # ── 请求信息 ──
    site_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_query: Mapped[str] = mapped_column(Text, nullable=False)

    # ── Agent 输出 ──
    intent: Mapped[str | None] = mapped_column(String(128), nullable=True)
    retrieved_cases: Mapped[dict] = mapped_column(JSONB, nullable=False, default=list)
    tool_calls: Mapped[dict] = mapped_column(JSONB, nullable=False, default=list)
    agent_suggestion: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_answer: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    elapsed_sec: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ── 工程师反馈 ──
    engineer_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    final_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True, default="pending")
    feedback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    human_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # ── 候选经验 ──
    candidate_generated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    candidate_path: Mapped[str | None] = mapped_column(String(512), nullable=True)

    __table_args__ = (
        Index("ix_agent_traces_intent", "intent"),
        Index("ix_agent_traces_outcome", "final_outcome"),
        Index("ix_agent_traces_created_at", "created_at"),
    )


# ────────────────────────────────────────────────────────────
# AgentCase — 对应 agent_cases 表（P4 知识包入库后使用）
# ────────────────────────────────────────────────────────────

class AgentCase(Base):
    __tablename__ = "agent_cases"

    # ── 主键 ──
    id: Mapped[str] = mapped_column(
        UUID(as_uuid=False),
        primary_key=True,
        server_default=func.gen_random_uuid(),
    )
    case_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now(),
    )

    # ── 来源 ──
    source_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    case_status: Mapped[str] = mapped_column(String(32), nullable=False, default="candidate")

    # ── 现场上下文 ──
    site_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    station_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    product_type: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ── 核心字段 ──
    symptom: Mapped[str] = mapped_column(Text, nullable=False)
    alarm_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    device_context: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # ── 证据链 ──
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False, default=list)

    # ── 根因 & 解决方案 ──
    root_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    solution: Mapped[dict] = mapped_column(JSONB, nullable=False, default=list)
    verified_result: Mapped[str | None] = mapped_column(Text, nullable=True)

    # ── 检索标签 ──
    applicability: Mapped[dict] = mapped_column(JSONB, nullable=False, default=list)
    tags: Mapped[dict] = mapped_column(JSONB, nullable=False, default=list)

    # ── 风险 & 安全 ──
    risk_level: Mapped[str] = mapped_column(String(16), nullable=False, default="medium")
    human_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sensitive_level: Mapped[str] = mapped_column(String(32), nullable=False, default="internal")

    # ── 知识包版本 ──
    knowledge_pack_version: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # ── 全文档（向量化用） ──
    full_doc: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    __table_args__ = (
        Index("ix_agent_cases_status", "case_status"),
        Index("ix_agent_cases_alarm_code", "alarm_code"),
        Index("ix_agent_cases_site_type", "site_type"),
    )
