"""
TraceRepository — agent_traces 表的 CRUD 操作。

设计原则：
- 所有方法均接受 Session 参数（不持有 Session），便于测试和事务管理。
- 转换层：TraceRecord (Pydantic) ↔ AgentTrace (ORM)，不让 ORM 对象泄露到业务层。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.cases.schema import ToolCallRecord, TraceRecord
from app.observability.logging import get_logger

log = get_logger(__name__)


class TraceRepository:
    """agent_traces 表的数据访问对象。"""

    # ── 写入 ──────────────────────────────────────────────

    @staticmethod
    def save(session, trace: TraceRecord) -> "AgentTrace":  # noqa: F821
        """将 TraceRecord 持久化到 agent_traces 表。"""
        from app.persistence.models import AgentTrace

        row = AgentTrace(
            trace_id=trace.trace_id,
            created_at=trace.created_at,
            site_id=trace.site_id,
            user_query=trace.user_query,
            intent=trace.intent,
            retrieved_cases=[rc.model_dump() for rc in trace.retrieved_cases],
            tool_calls=[tc.model_dump() for tc in trace.tool_calls],
            agent_suggestion=trace.agent_suggestion,
            final_answer=trace.final_answer,
            elapsed_sec=trace.elapsed_sec,
            final_outcome=trace.final_outcome or "pending",
            human_verified=trace.human_verified,
            candidate_generated=trace.candidate_generated,
            candidate_path=trace.candidate_path,
        )
        session.add(row)
        session.flush()   # 获取 server-side generated id
        log.info("trace_saved", trace_id=trace.trace_id)
        return row

    @staticmethod
    def update_feedback(
        session,
        trace_id: str,
        *,
        engineer_action: Optional[str],
        final_outcome: str,
        human_verified: bool = True,
    ) -> bool:
        """
        更新工程师反馈字段。
        返回 True 表示更新成功，False 表示 trace_id 不存在。
        """
        from app.persistence.models import AgentTrace

        row: Optional[AgentTrace] = (
            session.query(AgentTrace).filter_by(trace_id=trace_id).first()
        )
        if row is None:
            log.warning("trace_not_found", trace_id=trace_id)
            return False

        row.engineer_action = engineer_action
        row.final_outcome = final_outcome
        row.human_verified = human_verified
        row.feedback_at = datetime.now(timezone.utc)
        session.flush()
        log.info(
            "feedback_updated",
            trace_id=trace_id,
            outcome=final_outcome,
            human_verified=human_verified,
        )
        return True

    @staticmethod
    def mark_candidate_generated(
        session, trace_id: str, candidate_path: str
    ) -> None:
        """标记候选经验已生成。"""
        from app.persistence.models import AgentTrace

        row = session.query(AgentTrace).filter_by(trace_id=trace_id).first()
        if row:
            row.candidate_generated = True
            row.candidate_path = candidate_path
            session.flush()

    # ── 查询 ──────────────────────────────────────────────

    @staticmethod
    def get(session, trace_id: str) -> Optional[Dict[str, Any]]:
        """按 trace_id 查询，返回 dict（前端友好），不存在返回 None。"""
        from app.persistence.models import AgentTrace

        row: Optional[AgentTrace] = (
            session.query(AgentTrace).filter_by(trace_id=trace_id).first()
        )
        if row is None:
            return None
        return TraceRepository._to_dict(row)

    @staticmethod
    def list(
        session,
        limit: int = 20,
        offset: int = 0,
        intent: Optional[str] = None,
        outcome: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """列出排查记录（分页 + 过滤）。"""
        from app.persistence.models import AgentTrace

        q = session.query(AgentTrace)
        if intent:
            q = q.filter(AgentTrace.intent == intent)
        if outcome:
            q = q.filter(AgentTrace.final_outcome == outcome)
        rows = (
            q.order_by(AgentTrace.created_at.desc())
            .limit(limit)
            .offset(offset)
            .all()
        )
        return [TraceRepository._to_dict(r) for r in rows]

    @staticmethod
    def get_as_trace_record(session, trace_id: str) -> Optional[TraceRecord]:
        """
        按 trace_id 查询并还原为 TraceRecord Pydantic 对象。
        用于传递给 CandidateEngine.run()。
        """
        from app.persistence.models import AgentTrace

        row: Optional[AgentTrace] = (
            session.query(AgentTrace).filter_by(trace_id=trace_id).first()
        )
        if row is None:
            return None
        return TraceRepository._to_trace_record(row)

    # ── 私有转换方法 ───────────────────────────────────────

    @staticmethod
    def _to_dict(row) -> Dict[str, Any]:
        return {
            "trace_id": row.trace_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "site_id": row.site_id,
            "user_query": row.user_query,
            "intent": row.intent,
            "agent_suggestion": row.agent_suggestion,
            "elapsed_sec": row.elapsed_sec,
            "final_outcome": row.final_outcome,
            "engineer_action": row.engineer_action,
            "human_verified": row.human_verified,
            "candidate_generated": row.candidate_generated,
            "candidate_path": row.candidate_path,
            "tool_calls": row.tool_calls or [],
            "final_answer": row.final_answer,
            "feedback_at": row.feedback_at.isoformat() if row.feedback_at else None,
        }

    @staticmethod
    def _to_trace_record(row) -> TraceRecord:
        tool_calls = [
            ToolCallRecord(**tc) if isinstance(tc, dict) else tc
            for tc in (row.tool_calls or [])
        ]
        return TraceRecord(
            trace_id=row.trace_id,
            created_at=row.created_at,
            site_id=row.site_id,
            user_query=row.user_query,
            intent=row.intent,
            tool_calls=tool_calls,
            agent_suggestion=row.agent_suggestion,
            final_answer=row.final_answer,
            elapsed_sec=row.elapsed_sec,
            engineer_action=row.engineer_action,
            final_outcome=row.final_outcome,
            feedback_at=row.feedback_at,
            human_verified=row.human_verified,
            candidate_generated=row.candidate_generated,
            candidate_path=row.candidate_path,
        )
