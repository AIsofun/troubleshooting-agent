"""
Tests for the tool registry (mock data, no external services).
"""
import pytest

from app.tools.registry import call_tool, describe_tools, TOOLS


def test_describe_tools_returns_string():
    desc = describe_tools()
    assert isinstance(desc, str)
    assert "get_camera_status" in desc


def test_all_tools_registered():
    expected = {
        "get_camera_status",
        "get_recent_logs",
        "get_kafka_backlog",
        "get_model_metrics",
        "get_device_heartbeat",
        "query_runbook",
        "restart_service",
        "search_knowledge",
    }
    assert expected.issubset(set(TOOLS.keys()))


def test_get_camera_status_known():
    result = call_tool("get_camera_status", {"camera_id": "cam-02"})
    assert result["ok"] is True
    assert "cam-02" in result["summary"]


def test_get_camera_status_unknown():
    result = call_tool("get_camera_status", {"camera_id": "cam-99"})
    assert result["ok"] is False


def test_get_recent_logs():
    result = call_tool("get_recent_logs", {"service_name": "camera-service", "limit": 3})
    assert result["ok"] is True
    assert "camera-service" in result["summary"]


def test_query_runbook_known():
    result = call_tool("query_runbook", {"issue_type": "camera_offline"})
    assert result["ok"] is True
    assert "steps" in str(result["data"])


def test_query_runbook_unknown():
    result = call_tool("query_runbook", {"issue_type": "nonexistent_issue"})
    assert result["ok"] is False


def test_restart_service_is_dry_run_by_default():
    result = call_tool("restart_service", {"service_name": "camera-service"})
    assert result["ok"] is True
    assert result["data"]["dry_run"] is True


def test_call_unknown_tool():
    result = call_tool("nonexistent_tool", {})
    assert result["ok"] is False


def test_get_kafka_backlog():
    result = call_tool("get_kafka_backlog", {"topic": "vision.events"})
    assert result["ok"] is True
    assert "lag" in result["summary"]
