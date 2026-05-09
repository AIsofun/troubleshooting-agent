"""
Phase 6 — MCP 工具层测试。

覆盖：
  MCP Server (mcp/server.py):
    - _to_input_schema: 各种参数类型（string/int/bool/nullable）
    - _get_tool_list: 包含所有 TOOLS 条目
    - _handle_initialize: 返回 serverInfo + capabilities
    - _handle_tools_list: 返回 tools 列表
    - _handle_tools_call: 成功调用 / 未知工具 / 工具错误
    - POST /mcp/v1 JSON-RPC 端点（TestClient）
    - GET  /mcp/v1/tools REST 端点
    - POST /mcp/v1/tools/{name} REST 端点（成功 / 404）

  MCP Client (mcp/client.py):
    - MCPClientSync.connect() 失败时 is_available() = False
    - MCPClientSync.call() 不可用时返回 ok=False
    - MCPClientSync._http_post mock: 测试 tools/list 响应解析

  MCP Adapter (mcp/adapter.py):
    - 无远程服务器时 call_tool 路由到本地 TOOLS
    - 注册 mock MCPClientSync: 远程工具命中
    - 本地工具名优先于远程工具名
    - describe_all_tools 包含远程工具信息
    - list_all_tools 合并本地 + 远程

  Integration:
    - registry.call_tool 在无远程服务器时正常工作
    - registry.describe_tools 格式正确
"""
from __future__ import annotations

import json
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


# ── MCP Server: _to_input_schema ─────────────────────────────


def test_to_input_schema_string():
    from app.mcp.server import _to_input_schema

    schema = _to_input_schema({"camera_id": "string, e.g. cam-01"})
    assert schema["properties"]["camera_id"]["type"] == "string"
    assert "camera_id" in schema["required"]


def test_to_input_schema_int_with_default():
    from app.mcp.server import _to_input_schema

    schema = _to_input_schema({"limit": "int, default 5"})
    assert schema["properties"]["limit"]["type"] == "integer"
    # default → not required
    assert "limit" not in schema["required"]


def test_to_input_schema_nullable():
    from app.mcp.server import _to_input_schema

    schema = _to_input_schema({"alarm_code": "string|null, 精确匹配报警码"})
    prop = schema["properties"]["alarm_code"]
    assert "anyOf" in prop
    types = [t["type"] for t in prop["anyOf"]]
    assert "string" in types
    assert "null" in types
    assert "alarm_code" not in schema["required"]


def test_to_input_schema_bool():
    from app.mcp.server import _to_input_schema

    schema = _to_input_schema({"dry_run": "bool, default True"})
    assert schema["properties"]["dry_run"]["type"] == "boolean"


# ── MCP Server: _get_tool_list ────────────────────────────────


def test_get_tool_list_contains_all_tools():
    from app.mcp.server import _get_tool_list
    from app.tools.registry import TOOLS

    tools = _get_tool_list()
    names = {t["name"] for t in tools}
    assert names == set(TOOLS.keys())


def test_get_tool_list_has_input_schema():
    from app.mcp.server import _get_tool_list

    tools = _get_tool_list()
    for t in tools:
        assert "inputSchema" in t
        assert t["inputSchema"]["type"] == "object"


# ── MCP Server: _handle_* ─────────────────────────────────────


def test_handle_initialize():
    from app.mcp.server import _handle_initialize

    resp = _handle_initialize(1, {"protocolVersion": "2024-11-05"})
    assert resp["id"] == 1
    assert "result" in resp
    assert resp["result"]["protocolVersion"] == "2024-11-05"
    assert "tools" in resp["result"]["capabilities"]
    assert resp["result"]["serverInfo"]["name"] == "agentdemo-mcp"


def test_handle_tools_list():
    from app.mcp.server import _handle_tools_list

    resp = _handle_tools_list(2, {})
    assert "tools" in resp["result"]
    assert len(resp["result"]["tools"]) > 0


def test_handle_tools_call_success():
    from app.mcp.server import _handle_tools_call

    resp = _handle_tools_call(3, {
        "name": "get_camera_status",
        "arguments": {"camera_id": "cam-01"},
    })
    assert "result" in resp
    result = resp["result"]
    assert result.get("isError") is False
    assert len(result["content"]) > 0


def test_handle_tools_call_missing_name():
    from app.mcp.server import _handle_tools_call

    resp = _handle_tools_call(4, {"arguments": {}})
    assert "error" in resp
    assert resp["error"]["code"] == -32602


def test_handle_tools_call_unknown_tool():
    from app.mcp.server import _handle_tools_call

    resp = _handle_tools_call(5, {"name": "nonexistent_tool", "arguments": {}})
    # 工具执行失败 → isError=True（不是 JSON-RPC error）
    result = resp["result"]
    assert result["isError"] is True


def test_handle_single_unknown_method():
    from app.mcp.server import _handle_single

    resp = _handle_single({"jsonrpc": "2.0", "id": 9, "method": "no_such_method"})
    assert "error" in resp
    assert resp["error"]["code"] == -32601


# ── MCP Server: FastAPI endpoints ────────────────────────────


@pytest.fixture()
def mcp_client():
    with patch("app.persistence.db.init_db", return_value=False), \
         patch("app.mcp.adapter.init_adapter_from_config"):
        from app.web.server import app
        return TestClient(app, raise_server_exceptions=False)


def test_mcp_rest_list_tools(mcp_client):
    resp = mcp_client.get("/mcp/v1/tools")
    assert resp.status_code == 200
    data = resp.json()
    assert "tools" in data
    assert len(data["tools"]) >= 8


def test_mcp_jsonrpc_initialize(mcp_client):
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {}},
    }
    resp = mcp_client.post("/mcp/v1", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 1
    assert body["result"]["serverInfo"]["name"] == "agentdemo-mcp"


def test_mcp_jsonrpc_tools_list(mcp_client):
    resp = mcp_client.post("/mcp/v1", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert resp.status_code == 200
    body = resp.json()
    assert "tools" in body["result"]


def test_mcp_jsonrpc_tools_call(mcp_client):
    resp = mcp_client.post("/mcp/v1", json={
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "get_camera_status", "arguments": {"camera_id": "cam-01"}},
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["isError"] is False


def test_mcp_jsonrpc_batch(mcp_client):
    """批量请求：两个方法合并为一个请求。"""
    payload = [
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 2, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {}}},
    ]
    resp = mcp_client.post("/mcp/v1", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == 2


def test_mcp_rest_call_tool_success(mcp_client):
    resp = mcp_client.post(
        "/mcp/v1/tools/get_kafka_backlog",
        json={"arguments": {"topic": "vision.events"}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True


def test_mcp_rest_call_tool_not_found(mcp_client):
    resp = mcp_client.post(
        "/mcp/v1/tools/nonexistent_tool",
        json={"arguments": {}},
    )
    assert resp.status_code == 404


def test_mcp_ping(mcp_client):
    resp = mcp_client.post("/mcp/v1", json={"jsonrpc": "2.0", "id": 0, "method": "ping"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == {}


# ── MCP Client ────────────────────────────────────────────────


def test_mcp_client_connect_failure():
    from app.mcp.client import MCPClientSync

    client = MCPClientSync("http://nonexistent-host:9999/mcp", name="test", timeout=0.001)

    # Mock the actual HTTP call to raise immediately
    with patch.object(client, "_http_post", side_effect=ConnectionError("refused")):
        ok = client.connect()

    assert ok is False
    assert client.is_available() is False


def test_mcp_client_call_when_unavailable():
    from app.mcp.client import MCPClientSync

    client = MCPClientSync("http://nonexistent-host:9999/mcp", name="test")
    # 没有 connect()，直接调
    result = client.call("some_tool", {})
    assert result["ok"] is False
    assert "unavailable" in result["summary"]


def test_mcp_client_parses_tools_list():
    from app.mcp.client import MCPClientSync

    client = MCPClientSync("http://mock:8080/mcp", name="mock-server")

    def fake_jsonrpc(method, params):
        if method == "initialize":
            return {"result": {"protocolVersion": "2024-11-05", "capabilities": {}}}
        if method == "tools/list":
            return {"result": {"tools": [
                {"name": "remote_tool_a", "description": "Tool A", "inputSchema": {}},
                {"name": "remote_tool_b", "description": "Tool B", "inputSchema": {}},
            ]}}
        return {}

    with patch.object(client, "_jsonrpc", side_effect=fake_jsonrpc):
        ok = client.connect()

    assert ok is True
    assert client.has_tool("remote_tool_a")
    assert client.has_tool("remote_tool_b")
    assert len(client.tools()) == 2


def test_mcp_client_call_with_structured_result():
    from app.mcp.client import MCPClientSync

    client = MCPClientSync("http://mock:8080/mcp", name="mock-server")
    client._available = True
    client._tool_index = {"remote_tool_a": {"name": "remote_tool_a"}}

    mock_result = {
        "content": [{"type": "text", "text": "调用成功"}],
        "isError": False,
        "_structured": {"ok": True, "summary": "远程工具返回", "data": {"x": 1}},
    }

    with patch.object(client, "_jsonrpc", return_value={"result": mock_result}):
        result = client.call("remote_tool_a", {"param": "value"})

    assert result["ok"] is True
    assert result["data"]["x"] == 1


# ── MCP Adapter ───────────────────────────────────────────────


def _fresh_adapter():
    """每个测试用独立的 MCPAdapter 实例，避免全局单例污染。"""
    from app.mcp.adapter import MCPAdapter
    return MCPAdapter()


def test_adapter_local_tool_call():
    adapter = _fresh_adapter()
    result = adapter.call_tool("get_kafka_backlog", {"topic": "vision.events"})
    assert result["ok"] is True


def test_adapter_unknown_tool():
    adapter = _fresh_adapter()
    result = adapter.call_tool("totally_unknown_tool", {})
    assert result["ok"] is False


def test_adapter_remote_tool_routing():
    """远程 MCP 工具应正确路由到对应 client.call()。"""
    from app.mcp.client import MCPClientSync

    adapter = _fresh_adapter()
    mock_client = MagicMock(spec=MCPClientSync)
    mock_client.name = "mock-server"
    mock_client.base_url = "http://mock:8080/mcp"
    mock_client.is_available.return_value = True
    mock_client.tools.return_value = [
        {"name": "remote_sensor_read", "description": "读传感器"}
    ]
    mock_client.connect.return_value = True
    mock_client.call.return_value = {"ok": True, "summary": "传感器值 42", "data": {"val": 42}}

    adapter.register_server(mock_client)
    adapter.connect_all()

    assert adapter.remote_tool_count() == 1
    result = adapter.call_tool("remote_sensor_read", {"sensor_id": "s-01"})
    assert result["ok"] is True
    assert result["data"]["val"] == 42
    mock_client.call.assert_called_once_with("remote_sensor_read", {"sensor_id": "s-01"})


def test_adapter_local_tool_priority_over_remote():
    """本地工具名与远程工具名重叠时，本地优先。"""
    from app.mcp.client import MCPClientSync

    adapter = _fresh_adapter()
    mock_client = MagicMock(spec=MCPClientSync)
    mock_client.name = "mock-server"
    mock_client.base_url = "http://mock:8080/mcp"
    mock_client.is_available.return_value = True
    mock_client.tools.return_value = [
        {"name": "get_camera_status", "description": "远程相机"}  # 与本地重名
    ]
    mock_client.connect.return_value = True

    adapter.register_server(mock_client)
    adapter.connect_all()

    # get_camera_status 应走本地，而非远程
    result = adapter.call_tool("get_camera_status", {"camera_id": "cam-01"})
    assert result["ok"] is True
    mock_client.call.assert_not_called()


def test_adapter_describe_all_tools_includes_remote():
    from app.mcp.client import MCPClientSync

    adapter = _fresh_adapter()
    mock_client = MagicMock(spec=MCPClientSync)
    mock_client.name = "sensor-server"
    mock_client.base_url = "http://sensor:8080/mcp"
    mock_client.is_available.return_value = True
    mock_client.tools.return_value = [
        {"name": "read_temperature", "description": "读取温度",
         "inputSchema": {"type": "object", "properties": {"sensor_id": {}}, "required": []}}
    ]
    mock_client.connect.return_value = True
    adapter.register_server(mock_client)
    adapter.connect_all()

    desc = adapter.describe_all_tools()
    assert "read_temperature" in desc
    assert "sensor-server" in desc
    assert "get_camera_status" in desc  # 本地工具仍在


# ── registry.call_tool / describe_tools integration ──────────


def test_registry_call_tool_local_no_adapter():
    """在没有远程 MCP 时，registry.call_tool 直接调用本地 TOOLS。"""
    from app.tools.registry import call_tool

    result = call_tool("get_kafka_backlog", {"topic": "vision.events"})
    assert result["ok"] is True


def test_registry_describe_tools_format():
    from app.tools.registry import describe_tools

    desc = describe_tools()
    assert "get_camera_status" in desc
    assert "search_knowledge" in desc
    assert "risk=" in desc
