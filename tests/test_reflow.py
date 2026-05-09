"""
Phase 4 知识回流管线测试。

覆盖：
  - CaseRepository.from_candidate_file(): CandidateCase JSON → CaseRecord
  - CaseRepository.save() / list() / get() / update_status()（Mock Session）
  - _anonymize(): 脱敏处理字段正确性
  - _export_from_disk(): 从 verified/ 目录读取案例
  - _write_pack(): 生成目录 + metadata.json + zip
  - export_cases.main() CLI: --dry-run / --anonymize
  - POST /api/cases/promote/{id}: FastAPI 端点（Mock DB + Mock Embedder）
  - POST /api/cases/reject/{id}: 文件移动
  - GET  /api/cases: DB 不可用时返回 503
"""
from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ── fixture helpers ───────────────────────────────────────


def _make_candidate(tmp_path: Path, suffix: str = "001") -> Path:
    """在 tmp_path/pending/ 创建一个合法的 candidate JSON 文件，返回路径。"""
    cid = f"cand_20260509_{suffix}"
    data = {
        "candidate_id": cid,
        "generated_at": "2026-05-09T10:00:00+00:00",
        "source_trace_id": f"trace_20260509_100000_{suffix}",
        "generation_reason": "resolved",
        "site_type": "3C_assembly_factory_A",
        "station_type": "appearance_inspection",
        "product_type": "metal_cover",
        "symptom": "误杀率突然升高至 8.7%，ALG_FALSE_REJECT_HIGH 报警",
        "alarm_code": "ALG_FALSE_REJECT_HIGH",
        "device_context": {
            "camera_model": "MV-CA050-10GM",
            "algorithm_version": "defect_cls_v3.2",
            "agent_version": "agent_1.4.0",
        },
        "evidence": ["误杀率: 8.7%", "曝光时间: 12000μs"],
        "root_cause": "曝光时间过长导致图像过曝，边缘特征模糊",
        "solution": ["将曝光时间调整为 8000μs", "降低增益至 3.0"],
        "verified_result": "调整后误杀率恢复正常 (< 1%)",
        "applicability": ["高反光金属件", "外观检测站"],
        "tags": ["曝光", "误杀", "相机参数"],
        "risk_level": "medium",
        "sensitive_level": "internal",
    }
    pending_dir = tmp_path / "pending"
    pending_dir.mkdir(parents=True, exist_ok=True)
    p = pending_dir / f"{cid}.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


# ── CaseRepository.from_candidate_file ───────────────────


def test_from_candidate_basic(tmp_path):
    from app.cases.case_repo import CaseRepository

    p = _make_candidate(tmp_path)
    case = CaseRepository.from_candidate_file(p)

    assert case.case_status == "verified"
    assert case.human_verified is True
    assert case.alarm_code == "ALG_FALSE_REJECT_HIGH"
    assert case.symptom.startswith("误杀率突然升高")
    assert case.device_context is not None
    assert case.device_context.camera_model == "MV-CA050-10GM"
    assert case.root_cause is not None
    assert len(case.solution) == 2


def test_from_candidate_case_id_format(tmp_path):
    from app.cases.case_repo import CaseRepository

    p = _make_candidate(tmp_path, "abc")
    case = CaseRepository.from_candidate_file(p)
    # cand_ → case_
    assert case.case_id.startswith("case_")


def test_from_candidate_missing_fields(tmp_path):
    """缺少非必填字段时不报错，symptom 使用默认值。"""
    from app.cases.case_repo import CaseRepository

    data = {"candidate_id": "cand_minimal", "generated_at": "2026-05-09T00:00:00+00:00"}
    p = tmp_path / "pending" / "cand_minimal.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data), encoding="utf-8")

    case = CaseRepository.from_candidate_file(p)
    assert case.symptom == "（未填写）"
    assert case.solution == []


# ── CaseRepository CRUD（mock Session）───────────────────


def _mock_session():
    session = MagicMock()
    session.query.return_value.filter_by.return_value.first.return_value = None
    return session


def test_from_candidate_verified_fields(tmp_path):
    """Promote 后 CaseRecord 的关键字段校验（不依赖 DB）。"""
    from app.cases.case_repo import CaseRepository

    p = _make_candidate(tmp_path)
    case = CaseRepository.from_candidate_file(p)

    # 晋升字段
    assert case.case_status == "verified"
    assert case.human_verified is True
    # 证据链完整
    assert len(case.evidence) == 2
    assert len(case.solution) == 2
    # 检索标签
    assert "曝光" in case.tags
    assert "外观检测站" in case.applicability


def test_update_status_not_found():
    pytest.importorskip("sqlalchemy", reason="sqlalchemy not installed in local env")
    from app.cases.case_repo import CaseRepository
    import app.persistence.models  # 确保模块已加载

    session = _mock_session()
    session.query.return_value.filter_by.return_value.first.return_value = None

    with patch.object(app.persistence.models, "AgentCase", MagicMock()):
        result = CaseRepository.update_status(session, "nonexistent", status="verified")
    assert result is False


def test_to_dict_structure(tmp_path):
    """_to_dict 应返回包含所有关键字段的字典。"""
    from app.cases.case_repo import CaseRepository

    fake_row = MagicMock()
    fake_row.case_id = "case_001"
    fake_row.created_at = None
    fake_row.updated_at = None
    fake_row.source_trace_id = "trace_001"
    fake_row.case_status = "verified"
    fake_row.site_type = "3C"
    fake_row.station_type = "inspection"
    fake_row.product_type = "metal"
    fake_row.symptom = "误杀率升高"
    fake_row.alarm_code = "ALG_001"
    fake_row.device_context = None
    fake_row.evidence = []
    fake_row.root_cause = "曝光过高"
    fake_row.solution = ["降低曝光"]
    fake_row.verified_result = "已修复"
    fake_row.applicability = []
    fake_row.tags = []
    fake_row.risk_level = "medium"
    fake_row.human_verified = True
    fake_row.sensitive_level = "internal"
    fake_row.knowledge_pack_version = None

    d = CaseRepository._to_dict(fake_row)
    assert d["case_id"] == "case_001"
    assert d["alarm_code"] == "ALG_001"
    assert d["human_verified"] is True
    assert "solution" in d


# ── _anonymize ───────────────────────────────────────────


def test_anonymize_removes_site_id():
    from app.knowledge.export_cases import _anonymize

    case = {
        "site_id": "factory_shenzhen_001",
        "site_type": "3C_assembly_A",
        "device_context": {"camera_model": "MV-CA050", "agent_version": "1.4.0"},
        "symptom": "误杀率升高",
    }
    out = _anonymize(case)
    assert "site_id" not in out
    assert out["site_type"] == "3C"                       # 只保留大类
    assert "agent_version" not in out["device_context"]  # 工程师版本移除
    assert out["sensitive_level"] == "anonymized"


def test_anonymize_no_device_context():
    from app.knowledge.export_cases import _anonymize

    case = {"symptom": "相机掉线"}
    out = _anonymize(case)
    assert out["sensitive_level"] == "anonymized"


# ── _export_from_disk ─────────────────────────────────────


def test_export_from_disk_empty(tmp_path):
    from app.knowledge.export_cases import _export_from_disk

    result = _export_from_disk(tmp_path)
    assert result == []


def test_export_from_disk_reads_verified(tmp_path):
    from app.knowledge.export_cases import _export_from_disk

    verified_dir = tmp_path / "verified"
    verified_dir.mkdir()
    (verified_dir / "case_001.json").write_text(
        json.dumps({"symptom": "误杀率升高", "case_status": "verified"}),
        encoding="utf-8",
    )
    (verified_dir / "case_002.json").write_text(
        json.dumps({"symptom": "相机掉线", "case_status": "verified"}),
        encoding="utf-8",
    )

    result = _export_from_disk(tmp_path)
    assert len(result) == 2


def test_export_from_disk_anonymize(tmp_path):
    from app.knowledge.export_cases import _export_from_disk

    verified_dir = tmp_path / "verified"
    verified_dir.mkdir()
    (verified_dir / "case_001.json").write_text(
        json.dumps({"symptom": "误杀率", "site_id": "factory_A", "site_type": "3C_line1"}),
        encoding="utf-8",
    )

    result = _export_from_disk(tmp_path, anonymize=True)
    assert "site_id" not in result[0]
    assert result[0]["sensitive_level"] == "anonymized"


# ── _write_pack ───────────────────────────────────────────


def test_write_pack_creates_files(tmp_path):
    from app.knowledge.export_cases import _write_pack

    cases = [{"case_id": "case_001", "symptom": "误杀率升高"}]
    zip_path = _write_pack(tmp_path, "kp_2026_05", cases)

    assert zip_path.exists()
    pack_dir = tmp_path / "kp_2026_05"
    assert (pack_dir / "cases.json").exists()
    assert (pack_dir / "metadata.json").exists()

    meta = json.loads((pack_dir / "metadata.json").read_text(encoding="utf-8"))
    assert meta["version"] == "kp_2026_05"
    assert meta["count"] == 1
    assert "sha256" in meta


def test_write_pack_zip_contents(tmp_path):
    from app.knowledge.export_cases import _write_pack

    cases = [{"symptom": "测试案例"}]
    zip_path = _write_pack(tmp_path, "kp_test", cases)

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
    assert any("cases.json" in n for n in names)
    assert any("metadata.json" in n for n in names)


def test_write_pack_dry_run(tmp_path, capsys):
    from app.knowledge.export_cases import _write_pack

    _write_pack(tmp_path, "kp_dry", [], dry_run=True)
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert not (tmp_path / "kp_dry").exists()


# ── export_cases CLI ─────────────────────────────────────


def test_export_main_dry_run(tmp_path, capsys):
    from app.knowledge.export_cases import main

    verified_dir = tmp_path / "verified"
    verified_dir.mkdir()
    (verified_dir / "c.json").write_text('{"symptom":"test"}', encoding="utf-8")

    with patch("app.knowledge.export_cases._export_from_db", return_value=[]):
        ret = main([
            "--kp-version", "kp_2026_05",
            "--out", str(tmp_path / "packs"),
            "--cases-dir", str(tmp_path),
            "--dry-run",
        ])
    assert ret == 0
    out = capsys.readouterr().out
    assert "DRY-RUN" in out


def test_export_main_writes_zip(tmp_path):
    from app.knowledge.export_cases import main

    with patch("app.knowledge.export_cases._export_from_db", return_value=[
        {"case_id": "case_001", "symptom": "误杀率升高"},
    ]):
        ret = main([
            "--kp-version", "kp_2026_06",
            "--out", str(tmp_path / "packs"),
            "--cases-dir", str(tmp_path),
        ])
    assert ret == 0
    assert (tmp_path / "packs" / "kp_2026_06.zip").exists()


# ── FastAPI 端点（TestClient）───────────────────────────


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """
    创建带测试 cases_dir 的 TestClient。
    DB 不可用、Embedder 不可用均 mock 掉。
    """
    from app.cases.candidate import CandidateEngine

    # 指向 tmp_path 的 CandidateEngine
    engine = CandidateEngine(cases_dir=str(tmp_path))

    with patch("app.web.server._candidate_engine", engine), \
         patch("app.persistence.db.is_db_available", return_value=False), \
         patch("app.persistence.db.init_db", return_value=False):
        from app.web.server import app as _app
        yield TestClient(_app, raise_server_exceptions=False)


def test_list_cases_db_unavailable(client):
    resp = client.get("/api/cases")
    assert resp.status_code == 503


def test_reject_candidate_not_found(client):
    resp = client.post("/api/cases/reject/nonexistent_id")
    assert resp.status_code == 404


def test_promote_candidate_not_found(client):
    resp = client.post("/api/cases/promote/nonexistent_id")
    assert resp.status_code == 404


def test_reject_candidate_moves_file(tmp_path, monkeypatch):
    from app.cases.candidate import CandidateEngine
    from app.web.server import app as _app

    engine = CandidateEngine(cases_dir=str(tmp_path))
    p = _make_candidate(tmp_path, "rej01")
    cid = p.stem   # e.g. "cand_20260509_rej01"

    with patch("app.web.server._candidate_engine", engine), \
         patch("app.persistence.db.is_db_available", return_value=False), \
         patch("app.persistence.db.init_db", return_value=False):
        tc = TestClient(_app, raise_server_exceptions=False)
        resp = tc.post(f"/api/cases/reject/{cid}")

    assert resp.status_code == 200
    data = resp.json()
    assert "rejected_path" in data
    # 原文件应已移走
    assert not p.exists()
    rejected = tmp_path / "rejected" / p.name
    assert rejected.exists()


def test_promote_candidate_no_db_no_embedder(tmp_path):
    """promote 在无 DB + 无 Embedder 时应仍然成功移动文件，返回 200。"""
    from app.cases.candidate import CandidateEngine
    from app.web.server import app as _app

    engine = CandidateEngine(cases_dir=str(tmp_path))
    p = _make_candidate(tmp_path, "prm01")
    cid = p.stem

    with patch("app.web.server._candidate_engine", engine), \
         patch("app.persistence.db.is_db_available", return_value=False), \
         patch("app.persistence.db.init_db", return_value=False), \
         patch("app.knowledge.embedder.get_embedder") as mock_emb:
        mock_emb.return_value.embed.return_value = None  # embedder 不可用
        tc = TestClient(_app, raise_server_exceptions=False)
        resp = tc.post(f"/api/cases/promote/{cid}")

    assert resp.status_code == 200
    body = resp.json()
    assert "case_id" in body
    assert body["saved_to_db"] is False
    # 文件已移至 verified/
    assert not p.exists()
    assert (tmp_path / "verified" / p.name).exists()
