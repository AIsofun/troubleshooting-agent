"""
Tests for Case Schema and CandidateEngine (no external services required).
"""
import json
import tempfile
from pathlib import Path

import pytest

from app.cases.schema import (
    CandidateCase,
    CaseRecord,
    DeviceContext,
    RetrievedCase,
    ToolCallRecord,
    TraceRecord,
)
from app.cases.candidate import CandidateEngine


# ── Schema Tests ─────────────────────────────────────────────

def test_trace_record_defaults():
    tr = TraceRecord(user_query="2号相机掉线了")
    assert tr.user_query == "2号相机掉线了"
    assert tr.trace_id.startswith("trace_")
    assert tr.final_outcome == "pending"
    assert tr.human_verified is False
    assert tr.tool_calls == []


def test_trace_record_with_tool_calls():
    tr = TraceRecord(
        user_query="OCR成功率下降",
        intent="ocr_quality_drop",
        tool_calls=[
            ToolCallRecord(
                tool="get_model_metrics",
                input={"model_name": "ocr-v3"},
                output={"success_rate": 0.82},
                ok=True,
                summary="ocr-v3: success=0.82",
            )
        ],
    )
    assert len(tr.tool_calls) == 1
    assert tr.tool_calls[0].tool == "get_model_metrics"
    assert tr.tool_calls[0].ok is True


def test_case_record_defaults():
    c = CaseRecord(symptom="误杀率突然升高")
    assert c.case_id.startswith("case_")
    assert c.case_status == "candidate"
    assert c.human_verified is False
    assert c.sensitive_level == "internal"
    assert c.evidence == []


def test_case_record_full():
    c = CaseRecord(
        symptom="良品被误判为划伤，误杀率突然升高",
        alarm_code="ALG_FALSE_REJECT_HIGH",
        site_type="3C_assembly",
        station_type="appearance_inspection",
        product_type="metal_cover",
        device_context=DeviceContext(
            camera_brand="Hikrobot",
            camera_model="MV-CA050-10GM",
            algorithm_version="defect_cls_v3.2",
        ),
        evidence=["误杀率从1.2%上升到8.7%", "曝光时间从8000被改为12000"],
        root_cause="曝光过高导致金属边缘高反光",
        solution=["将曝光恢复到8000", "光源亮度从80降到65"],
        verified_result="误杀率恢复到1.5%",
        applicability=["高反光金属件", "外观检测工位"],
        risk_level="medium",
        human_verified=True,
        sensitive_level="anonymized",
    )
    assert c.device_context.camera_brand == "Hikrobot"
    assert c.risk_level == "medium"
    assert c.human_verified is True
    assert len(c.evidence) == 2


def test_case_record_serializes_to_json():
    c = CaseRecord(symptom="测试案例")
    data = json.loads(c.model_dump_json())
    assert "case_id" in data
    assert "symptom" in data
    assert data["symptom"] == "测试案例"


def test_device_context_optional_fields():
    d = DeviceContext(camera_brand="Hikrobot")
    assert d.camera_model is None
    assert d.algorithm_version is None


def test_candidate_case_has_desensitization_hint():
    cand = CandidateCase(
        source_trace_id="trace_test_001",
        symptom="误检率升高",
        user_query_raw="今天误检率突然升高",
    )
    assert "脱敏" in cand.desensitization_hint
    assert cand.candidate_id.startswith("cand_")


# ── CandidateEngine Tests ─────────────────────────────────────

def _make_trace(outcome="resolved", human_verified=True, engineer_action="将曝光恢复到8000") -> TraceRecord:
    return TraceRecord(
        user_query="误检率突然升高，帮我排查",
        intent="vision_false_reject_analysis",
        tool_calls=[
            ToolCallRecord(
                tool="get_camera_status",
                input={"camera_id": "cam-01"},
                output={"status": "online", "fps": 25},
                ok=True,
                summary="cam-01 status=online fps=25",
            ),
            ToolCallRecord(
                tool="get_model_metrics",
                input={"model_name": "ocr-v3"},
                output={"success_rate": 0.82, "false_reject_rate": "8.7%"},
                ok=True,
                summary="ocr-v3: success=0.82",
            ),
        ],
        agent_suggestion="优先检查曝光和光源亮度",
        final_answer={
            "intent": "ocr_quality_drop",
            "conclusion": "曝光过高导致误检率升高，建议降低曝光时间",
            "evidence": ["ocr-v3: success=0.82", "cam-01 status=online fps=25"],
            "suggestions": ["将曝光恢复到8000", "光源亮度从80降到65"],
            "safe_actions": [],
        },
        engineer_action=engineer_action,
        final_outcome=outcome,
        human_verified=human_verified,
        elapsed_sec=3.2,
    )


def test_should_generate_resolved():
    engine = CandidateEngine.__new__(CandidateEngine)
    trace = _make_trace(outcome="resolved")
    assert CandidateEngine.should_generate(trace) is True


def test_should_generate_not_pending():
    engine = CandidateEngine.__new__(CandidateEngine)
    trace = _make_trace(outcome="pending", human_verified=False, engineer_action=None)
    assert CandidateEngine.should_generate(trace) is False


def test_should_generate_human_verified_with_action():
    trace = _make_trace(outcome="unresolved", human_verified=True, engineer_action="重启了服务")
    assert CandidateEngine.should_generate(trace) is True


def test_generate_extracts_evidence():
    trace = _make_trace()
    candidate = CandidateEngine.generate(trace)
    assert len(candidate.evidence) >= 2
    assert any("get_camera_status" in e or "get_model_metrics" in e for e in candidate.evidence)


def test_generate_extracts_solution():
    trace = _make_trace()
    candidate = CandidateEngine.generate(trace)
    assert len(candidate.solution) >= 1
    assert "将曝光恢复到8000" in candidate.solution


def test_generate_sets_source_trace_id():
    trace = _make_trace()
    candidate = CandidateEngine.generate(trace)
    assert candidate.source_trace_id == trace.trace_id


def test_write_to_disk_creates_json():
    with tempfile.TemporaryDirectory() as tmp:
        engine = CandidateEngine(cases_dir=tmp)
        trace = _make_trace()
        candidate = engine.generate(trace)
        path = engine.write_to_disk(candidate)

        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["source_trace_id"] == trace.trace_id
        assert "symptom" in data
        assert "desensitization_hint" in data


def test_run_generates_candidate_when_resolved():
    with tempfile.TemporaryDirectory() as tmp:
        engine = CandidateEngine(cases_dir=tmp)
        trace = _make_trace(outcome="resolved")
        path = engine.run(trace)
        assert path is not None
        assert path.exists()
        pending = list(Path(tmp).glob("pending/*.json"))
        assert len(pending) == 1


def test_run_skips_candidate_when_pending():
    with tempfile.TemporaryDirectory() as tmp:
        engine = CandidateEngine(cases_dir=tmp)
        trace = _make_trace(outcome="pending", human_verified=False, engineer_action=None)
        path = engine.run(trace)
        assert path is None
        pending = list(Path(tmp).glob("pending/*.json"))
        assert len(pending) == 0


def test_candidate_json_is_valid_schema():
    """验证生成的 JSON 可以被 CandidateCase 模型重新加载。"""
    with tempfile.TemporaryDirectory() as tmp:
        engine = CandidateEngine(cases_dir=tmp)
        trace = _make_trace(outcome="resolved")
        path = engine.run(trace)
        data = json.loads(path.read_text(encoding="utf-8"))
        # 必须能用 Pydantic 模型重新解析（格式验证）
        reloaded = CandidateCase(**data)
        assert reloaded.source_trace_id == trace.trace_id
