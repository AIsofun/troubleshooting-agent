"""
CaseRepository — agent_cases 表的 CRUD 操作。

职责：
  - save()            : 将 CaseRecord (Pydantic) 持久化到 agent_cases 表
  - get()             : 按 case_id 查询单条记录
  - list()            : 分页列表 + 过滤（status / alarm_code / site_type）
  - update_status()   : 更新 case_status 和 knowledge_pack_version
  - from_candidate()  : 将磁盘 candidate JSON 转换为 CaseRecord

调用方：
  - POST /api/cases/promote/{candidate_id}  → save() + update knowledge stores
  - POST /api/cases/reject/{candidate_id}   → 仅磁盘移动，不写 DB
  - GET  /api/cases                         → list()
  - export_cases CLI                        → list(status="verified")
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.cases.schema import CaseRecord, DeviceContext
from app.observability.logging import get_logger

log = get_logger(__name__)


class CaseRepository:
    """agent_cases 表的数据访问对象。"""

    # ── 写入 ──────────────────────────────────────────────

    @staticmethod
    def save(session, case: CaseRecord) -> "AgentCase":  # noqa: F821
        """将 CaseRecord 持久化到 agent_cases 表。"""
        from app.persistence.models import AgentCase

        # 构建供向量化使用的 full_doc 快照
        full_doc = case.model_dump(mode="json")

        row = AgentCase(
            case_id=case.case_id,
            created_at=case.created_at,
            updated_at=case.updated_at,
            source_trace_id=case.source_trace_id,
            case_status=case.case_status,
            site_type=case.site_type,
            station_type=case.station_type,
            product_type=case.product_type,
            symptom=case.symptom,
            alarm_code=case.alarm_code,
            device_context=case.device_context.model_dump() if case.device_context else None,
            evidence=case.evidence,
            root_cause=case.root_cause,
            solution=case.solution,
            verified_result=case.verified_result,
            applicability=case.applicability,
            tags=case.tags,
            risk_level=case.risk_level,
            human_verified=case.human_verified,
            sensitive_level=case.sensitive_level,
            knowledge_pack_version=case.knowledge_pack_version,
            full_doc=full_doc,
        )
        session.add(row)
        session.flush()
        log.info("case_saved", case_id=case.case_id, status=case.case_status)
        return row

    @staticmethod
    def update_status(
        session,
        case_id: str,
        *,
        status: str,
        kp_version: Optional[str] = None,
    ) -> bool:
        """
        更新 case_status（和可选的 knowledge_pack_version）。
        返回 True 表示成功，False 表示 case_id 不存在。
        """
        from app.persistence.models import AgentCase

        row: Optional[AgentCase] = (
            session.query(AgentCase).filter_by(case_id=case_id).first()
        )
        if row is None:
            log.warning("case_not_found", case_id=case_id)
            return False

        row.case_status = status
        row.updated_at = datetime.now(timezone.utc)
        if kp_version:
            row.knowledge_pack_version = kp_version
        session.flush()
        log.info("case_status_updated", case_id=case_id, status=status)
        return True

    # ── 查询 ──────────────────────────────────────────────

    @staticmethod
    def get(session, case_id: str) -> Optional[Dict[str, Any]]:
        """按 case_id 查询，返回字典（不暴露 ORM 对象）。"""
        from app.persistence.models import AgentCase

        row: Optional[AgentCase] = (
            session.query(AgentCase).filter_by(case_id=case_id).first()
        )
        return CaseRepository._to_dict(row) if row else None

    @staticmethod
    def list(
        session,
        *,
        limit: int = 20,
        offset: int = 0,
        case_status: Optional[str] = None,
        alarm_code: Optional[str] = None,
        site_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """分页列出 cases，支持多维过滤。"""
        from app.persistence.models import AgentCase

        q = session.query(AgentCase)
        if case_status:
            q = q.filter(AgentCase.case_status == case_status)
        if alarm_code:
            q = q.filter(AgentCase.alarm_code == alarm_code)
        if site_type:
            q = q.filter(AgentCase.site_type == site_type)
        rows = (
            q.order_by(AgentCase.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return [CaseRepository._to_dict(r) for r in rows]

    # ── 候选转换 ──────────────────────────────────────────

    @staticmethod
    def from_candidate_file(candidate_path: Path) -> CaseRecord:
        """
        将磁盘上的 CandidateCase JSON 转换为可持久化的 CaseRecord。
        候选经验 promote 时调用。
        """
        data = json.loads(candidate_path.read_text(encoding="utf-8"))

        # 提取 candidate_id 作为 case_id 基础
        cid = data.get("candidate_id", "")
        case_id = cid.replace("cand_", "case_", 1) if cid.startswith("cand_") else f"case_{cid}"

        device_ctx = None
        if data.get("device_context"):
            try:
                device_ctx = DeviceContext(**data["device_context"])
            except Exception:
                device_ctx = None

        return CaseRecord(
            case_id=case_id,
            case_status="verified",
            source_trace_id=data.get("source_trace_id"),
            site_type=data.get("site_type"),
            station_type=data.get("station_type"),
            product_type=data.get("product_type"),
            symptom=data.get("symptom", "（未填写）"),
            alarm_code=data.get("alarm_code"),
            device_context=device_ctx,
            evidence=data.get("evidence", []),
            root_cause=data.get("root_cause"),
            solution=data.get("solution", []),
            verified_result=data.get("verified_result"),
            applicability=data.get("applicability", []),
            tags=data.get("tags", []),
            risk_level=data.get("risk_level", "medium"),
            human_verified=True,                          # promote 即代表已审核
            sensitive_level=data.get("sensitive_level", "internal"),
        )

    # ── 内部工具 ──────────────────────────────────────────

    @staticmethod
    def _to_dict(row: "AgentCase") -> Dict[str, Any]:  # noqa: F821
        return {
            "case_id": row.case_id,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            "source_trace_id": row.source_trace_id,
            "case_status": row.case_status,
            "site_type": row.site_type,
            "station_type": row.station_type,
            "product_type": row.product_type,
            "symptom": row.symptom,
            "alarm_code": row.alarm_code,
            "device_context": row.device_context,
            "evidence": row.evidence,
            "root_cause": row.root_cause,
            "solution": row.solution,
            "verified_result": row.verified_result,
            "applicability": row.applicability,
            "tags": row.tags,
            "risk_level": row.risk_level,
            "human_verified": row.human_verified,
            "sensitive_level": row.sensitive_level,
            "knowledge_pack_version": row.knowledge_pack_version,
        }
