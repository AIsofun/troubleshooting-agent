"""
Tests for the knowledge layer — no external services required.
Uses mock/stub to avoid Qdrant/PG/Ollama dependencies.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.knowledge.ingest import (
    _chunk_text,
    _make_doc_id,
    parse_json,
    parse_markdown,
    parse_txt,
    parse_csv,
)
from app.knowledge.reranker import ScoreReranker


# ── Ingest: chunking ────────────────────────────────────────

def test_chunk_text_short():
    text = "这是一段很短的文字"
    chunks = _chunk_text(text, size=800)
    assert chunks == [text]


def test_chunk_text_long():
    text = "A" * 1000
    chunks = _chunk_text(text, size=800, overlap=100)
    assert len(chunks) == 2
    # overlap: second chunk starts at 700
    assert chunks[1].startswith("A")


def test_make_doc_id_deterministic():
    doc_id1 = _make_doc_id("same content", 0)
    doc_id2 = _make_doc_id("same content", 0)
    assert doc_id1 == doc_id2
    assert doc_id1.startswith("doc_")


def test_make_doc_id_different():
    assert _make_doc_id("content A", 0) != _make_doc_id("content B", 0)


# ── Ingest: JSON parser ──────────────────────────────────────

def test_parse_json_runbook(tmp_path):
    runbook = {
        "camera_offline": {
            "title": "相机掉线处置流程",
            "steps": ["1. 确认网络", "2. 检查日志"],
            "safe_actions": ["restart_service:camera-service"],
        }
    }
    f = tmp_path / "runbook.json"
    f.write_text(json.dumps(runbook, ensure_ascii=False), encoding="utf-8")

    docs = list(parse_json(f, "runbook", "kp_test"))
    assert len(docs) == 1
    doc = docs[0]
    assert doc["doc_type"] == "runbook"
    assert "camera_offline" in doc["tags"]
    assert doc["knowledge_pack_version"] == "kp_test"
    assert "确认网络" in doc["content"]


def test_parse_json_case(tmp_path):
    case = {
        "symptom": "误杀率突然升高",
        "alarm_code": "ALG_FALSE_REJECT_HIGH",
        "root_cause": "曝光过高",
        "solution": ["降低曝光", "降低亮度"],
        "evidence": ["误杀率8.7%"],
        "applicability": ["高反光金属件"],
        "product_type": "metal_cover",
    }
    f = tmp_path / "case.json"
    f.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")

    docs = list(parse_json(f, "case", None))
    assert len(docs) == 1
    doc = docs[0]
    assert doc["doc_type"] == "case"
    assert doc["alarm_code"] == "ALG_FALSE_REJECT_HIGH"
    assert doc["product_type"] == "metal_cover"
    assert "误杀率突然升高" in doc["title"]


def test_parse_json_list(tmp_path):
    data = [
        {"title": "文档1", "content": "内容1"},
        {"title": "文档2", "content": "内容2", "alarm_code": "E001"},
    ]
    f = tmp_path / "list.json"
    f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    docs = list(parse_json(f, "manual", None))
    assert len(docs) == 2
    assert docs[1]["alarm_code"] == "E001"


def test_parse_markdown(tmp_path):
    md = """# 设备手册

## 相机配置
曝光时间建议设置 8000。增益建议 3.5。

## 故障排查
如果出现误检，首先检查曝光参数。
"""
    f = tmp_path / "manual.md"
    f.write_text(md, encoding="utf-8")

    docs = list(parse_markdown(f, "manual", "kp_v1"))
    # Should have sections for each ## heading
    assert len(docs) >= 2
    titles = [d["title"] for d in docs]
    assert any("相机配置" in t for t in titles)
    assert any("故障排查" in t for t in titles)


def test_parse_txt(tmp_path):
    text = "报警码 E001 表示曝光超时。" * 10
    f = tmp_path / "alarms.txt"
    f.write_text(text, encoding="utf-8")

    docs = list(parse_txt(f, "log", None))
    assert len(docs) >= 1
    assert all(d["doc_type"] == "log" for d in docs)


def test_parse_csv(tmp_path):
    csv_content = "title,content,alarm_code,device_model\n故障1,曝光过高,ALG_001,MV-CA050\n故障2,温度异常,ALG_002,\n"
    f = tmp_path / "faults.csv"
    f.write_text(csv_content, encoding="utf-8")

    docs = list(parse_csv(f, "manual", None))
    assert len(docs) == 2
    assert docs[0]["alarm_code"] == "ALG_001"
    assert docs[0]["device_model"] == "MV-CA050"
    assert docs[1]["alarm_code"] == "ALG_002"


# ── Reranker ─────────────────────────────────────────────────

def _make_candidates(n: int):
    return [
        {
            "doc_id": f"doc_{i}",
            "title": f"文档 {i}",
            "content": f"内容 {i}",
            "vector_score": 0.9 - i * 0.1,
            "kw_score": 0.5 - i * 0.05,
            "exact_hit": (i == 2),
        }
        for i in range(n)
    ]


def test_score_reranker_returns_top_k():
    candidates = _make_candidates(10)
    ranked = ScoreReranker.rerank(candidates, "测试查询", top_k=3)
    assert len(ranked) == 3


def test_score_reranker_exact_hit_boosts():
    candidates = _make_candidates(5)
    ranked = ScoreReranker.rerank(candidates, "查询", top_k=5)
    # doc_2 has exact_hit=True, should appear in top results
    ids = [r["doc_id"] for r in ranked]
    assert "doc_2" in ids[:3]


def test_score_reranker_empty():
    ranked = ScoreReranker.rerank([], "查询", top_k=5)
    assert ranked == []


def test_score_reranker_adds_rerank_score():
    candidates = _make_candidates(3)
    ranked = ScoreReranker.rerank(candidates, "查询", top_k=3)
    for r in ranked:
        assert "rerank_score" in r
        assert 0.0 <= r["rerank_score"] <= 1.0 + 0.1  # gamma can push above 1


# ── search_knowledge tool (mocked retriever) ─────────────────

def test_search_knowledge_tool_no_results():
    """search_knowledge 无结果时应返回 ok=True 而非失败。"""
    with patch("app.knowledge.retriever.HybridRetriever") as MockRetriever:
        instance = MockRetriever.return_value
        instance.search.return_value = []

        from app.tools.registry import search_knowledge
        result = search_knowledge(query="从未有过的奇怪问题")
        assert result["ok"] is True
        assert result["data"]["total"] == 0


def test_search_knowledge_tool_with_results():
    """search_knowledge 有结果时应返回正确摘要。"""
    mock_results = [
        {
            "doc_id": "doc_001",
            "title": "相机掉线处置流程",
            "content": "1.确认网络 2.检查日志",
            "rerank_score": 0.85,
            "exact_hit": False,
        }
    ]
    with patch("app.knowledge.retriever.HybridRetriever") as MockRetriever:
        instance = MockRetriever.return_value
        instance.search.return_value = mock_results
        instance.format_for_llm.return_value = "【检索到的相关知识】\n[1] 相机掉线处置流程"

        from app.tools.registry import search_knowledge
        # 清除模块级缓存（如有）
        result = search_knowledge(query="相机掉线", top_k=5)
        assert result["ok"] is True
        assert result["data"]["total"] == 1
        assert "相机掉线" in result["summary"]


def test_search_knowledge_registered_in_tools():
    """确保 search_knowledge 已注册到 TOOLS registry。"""
    from app.tools.registry import TOOLS
    assert "search_knowledge" in TOOLS
    meta = TOOLS["search_knowledge"]
    assert meta["risk"] == "low"
    assert "query" in meta["parameters"]


def test_search_knowledge_callable_via_call_tool():
    """call_tool 能正常调用 search_knowledge（异常时返回 ok=False）。"""
    with patch("app.knowledge.retriever.HybridRetriever") as MockRetriever:
        instance = MockRetriever.return_value
        instance.search.side_effect = Exception("Qdrant unavailable")

        from app.tools.registry import call_tool
        result = call_tool("search_knowledge", {"query": "测试"})
        assert result["ok"] is False


# ── HybridRetriever format_for_llm ───────────────────────────

def test_format_for_llm():
    from app.knowledge.retriever import HybridRetriever

    retriever = HybridRetriever.__new__(HybridRetriever)
    results = [
        {
            "title": "相机配置手册",
            "content": "曝光时间建议8000，增益建议3.5。",
            "source": "manual.md",
            "rerank_score": 0.9,
            "exact_hit": True,
            "alarm_code": "ALG_001",
        }
    ]
    text = retriever.format_for_llm(results)
    assert "相机配置手册" in text
    assert "⭐精确命中" in text
    assert "ALG_001" in text


def test_format_for_llm_empty():
    from app.knowledge.retriever import HybridRetriever
    retriever = HybridRetriever.__new__(HybridRetriever)
    text = retriever.format_for_llm([])
    assert "未找到" in text
