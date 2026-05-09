"""
CandidateEngine — 从 TraceRecord 自动生成候选经验。

触发条件（P2 实现，P4 可扩展）：
  1. 工程师通过 feedback API 标记 final_outcome == "resolved"（主触发）
  2. 工程师提供了 engineer_action 说明（有明确的处置记录）

候选经验的处理流程：
  - 写入 cases/pending/{candidate_id}.json（本地磁盘）
  - 工程师手工审核 → 拷贝到 cases/exported/ → 上传总部经验中台
  - 不会自动进入正式知识库（human_verified=False 守门）

调用时机：
  - POST /api/traces/{trace_id}/feedback 接口成功后，由 server.py 调用
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from app.cases.schema import CandidateCase, DeviceContext, TraceRecord
from app.observability.logging import get_logger

log = get_logger(__name__)

# 项目根目录（用于解析相对路径 cases_dir）
_ROOT = Path(__file__).resolve().parent.parent.parent


class CandidateEngine:
    """从 TraceRecord 生成候选经验并写入磁盘。"""

    def __init__(self, cases_dir: Optional[str] = None):
        """
        cases_dir: 候选经验输出目录。
          - None → 从 config 读取 knowledge.cases_dir，默认 ./cases
          - 字符串路径（相对或绝对）
        """
        if cases_dir is None:
            try:
                from app.config import get_settings
                cases_dir = get_settings().get("knowledge", {}).get("cases_dir", "./cases")
            except Exception:
                cases_dir = "./cases"

        p = Path(cases_dir)
        self.cases_dir: Path = p if p.is_absolute() else (_ROOT / cases_dir.lstrip("./"))
        self.pending_dir = self.cases_dir / "pending"
        self.pending_dir.mkdir(parents=True, exist_ok=True)

    # ── 触发条件判断 ────────────────────────────────────────

    @staticmethod
    def should_generate(trace: TraceRecord) -> bool:
        """
        判断是否应为该 trace 生成候选经验。

        条件（满足其一即触发）：
          - 工程师标记 resolved 且提供了处置说明
          - 工程师标记 resolved（即使没有详细说明）
        """
        if trace.final_outcome == "resolved":
            return True
        if trace.human_verified and trace.engineer_action:
            return True
        return False

    # ── 核心生成逻辑 ────────────────────────────────────────

    @staticmethod
    def generate(trace: TraceRecord) -> CandidateCase:
        """从 TraceRecord 提取关键信息，生成 CandidateCase。"""
        evidence = CandidateEngine._extract_evidence(trace)
        root_cause, solution = CandidateEngine._extract_root_cause_and_solution(trace)
        symptom = CandidateEngine._build_symptom(trace)

        candidate = CandidateCase(
            source_trace_id=trace.trace_id,
            generation_reason=CandidateEngine._detect_reason(trace),
            symptom=symptom,
            alarm_code=CandidateEngine._extract_alarm_code(trace),
            evidence=evidence,
            root_cause=root_cause,
            solution=solution,
            applicability=CandidateEngine._build_applicability(trace),
            risk_level=CandidateEngine._assess_risk(trace),
            engineer_action_raw=trace.engineer_action,
            final_outcome_raw=trace.final_outcome,
            user_query_raw=trace.user_query,
        )
        return candidate

    def write_to_disk(self, candidate: CandidateCase) -> Path:
        """将候选经验写入 cases/pending/{candidate_id}.json。"""
        filename = f"{candidate.candidate_id}.json"
        dest = self.pending_dir / filename

        with open(dest, "w", encoding="utf-8") as f:
            json.dump(
                candidate.model_dump(mode="json"),
                f,
                ensure_ascii=False,
                indent=2,
            )

        log.info(
            "candidate_written",
            candidate_id=candidate.candidate_id,
            source_trace_id=candidate.source_trace_id,
            path=str(dest),
        )
        return dest

    def run(self, trace: TraceRecord) -> Optional[Path]:
        """
        完整流程：判断 → 生成 → 写盘。
        返回写入的文件路径，若未触发则返回 None。
        """
        if not self.should_generate(trace):
            return None
        candidate = self.generate(trace)
        return self.write_to_disk(candidate)

    # ── 私有辅助方法 ────────────────────────────────────────

    @staticmethod
    def _detect_reason(trace: TraceRecord) -> str:
        if trace.final_outcome == "resolved":
            return "engineer_resolved"
        if trace.human_verified:
            return "human_verified"
        return "auto"

    @staticmethod
    def _build_symptom(trace: TraceRecord) -> str:
        """优先用 final_answer 的 conclusion，降级到 user_query。"""
        if trace.final_answer:
            conclusion = trace.final_answer.get("conclusion", "")
            if conclusion and len(conclusion) > 10:
                return conclusion
        return trace.user_query

    @staticmethod
    def _extract_evidence(trace: TraceRecord) -> List[str]:
        """从 tool_calls 的 summary 字段提取证据列表。"""
        items: List[str] = []
        for tc in trace.tool_calls:
            if tc.summary:
                items.append(f"[{tc.tool}] {tc.summary}")
            elif tc.output:
                # 将 output dict 转为简短文本
                try:
                    short = json.dumps(tc.output, ensure_ascii=False)[:200]
                    items.append(f"[{tc.tool}] {short}")
                except Exception:
                    pass
        # 补充 final_answer 中的 evidence 列表（如果有）
        if trace.final_answer:
            for ev in trace.final_answer.get("evidence", []) or []:
                if ev and ev not in items:
                    items.append(str(ev))
        return items

    @staticmethod
    def _extract_root_cause_and_solution(trace: TraceRecord):
        """从 final_answer 和 engineer_action 提取根因与解决方案。"""
        root_cause: Optional[str] = None
        solution: List[str] = []

        if trace.final_answer:
            root_cause = trace.final_answer.get("conclusion") or None
            solution = trace.final_answer.get("suggestions", []) or []

        # 如果工程师提供了更准确的处置描述，追加到 solution
        if trace.engineer_action and trace.engineer_action not in solution:
            solution = [trace.engineer_action] + list(solution)

        return root_cause, solution

    @staticmethod
    def _extract_alarm_code(trace: TraceRecord) -> Optional[str]:
        """从 tool_calls 中寻找报警码（如果工具有返回）。"""
        for tc in trace.tool_calls:
            if tc.output and isinstance(tc.output, dict):
                code = tc.output.get("alarm_code") or tc.output.get("error_code")
                if code:
                    return str(code)
        return None

    @staticmethod
    def _build_applicability(trace: TraceRecord) -> List[str]:
        """基于 intent 和 tool_calls 生成适用性标签。"""
        tags: List[str] = []
        if trace.intent:
            tags.append(trace.intent)
        tools_used = [tc.tool for tc in trace.tool_calls]
        if "get_camera_status" in tools_used:
            tags.append("相机异常")
        if "get_model_metrics" in tools_used:
            tags.append("算法质量")
        if "get_kafka_backlog" in tools_used:
            tags.append("消息队列")
        return tags

    @staticmethod
    def _assess_risk(trace: TraceRecord) -> str:
        """根据 final_answer 的 safe_actions 评估风险等级。"""
        if trace.final_answer:
            safe_actions = trace.final_answer.get("safe_actions") or []
            if any("restart" in a for a in safe_actions):
                return "medium"
        return "low"
