"""
MCP Server — 将本地 TOOLS 注册表暴露为 MCP 兼容的 JSON-RPC 2.0 端点。

协议参考：Model Context Protocol 2024-11-05 规范
  https://spec.modelcontextprotocol.io/

实现的 JSON-RPC 方法：
  initialize      协议握手，返回 serverInfo + capabilities
  tools/list      列出所有工具及其 inputSchema
  tools/call      调用指定工具

额外 REST 便捷端点（无需 JSON-RPC 封装，方便 curl 调试）：
  GET  /mcp/v1/tools              → tools/list 结果
  POST /mcp/v1/tools/{tool_name}  → 调用工具（body = arguments JSON）

挂载方式（在 app/web/server.py 中）：
  from app.mcp.server import mcp_router
  app.include_router(mcp_router, prefix="/mcp")
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from app.observability.logging import get_logger

log = get_logger(__name__)

mcp_router = APIRouter(tags=["mcp"])

# MCP 协议版本
_PROTOCOL_VERSION = "2024-11-05"
_SERVER_NAME = "agentdemo-mcp"
_SERVER_VERSION = "1.0.0"


# ── JSON-RPC helpers ─────────────────────────────────────────


def _ok(request_id: Any, result: Any) -> Dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id: Any, code: int, message: str, data: Any = None) -> Dict:
    err: Dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": err}


# ── Tool schema conversion ────────────────────────────────────

# 参数类型简单映射（与 llm.py 中 _build_tool_schemas 保持一致）
_TYPE_MAP = {
    "string": "string",
    "int": "integer",
    "integer": "integer",
    "bool": "boolean",
    "boolean": "boolean",
    "float": "number",
}


def _to_input_schema(parameters: Dict[str, str]) -> Dict[str, Any]:
    """将 TOOLS 的 parameters dict 转换为 JSON Schema inputSchema。"""
    properties: Dict[str, Any] = {}
    required: List[str] = []

    for param_name, param_desc in parameters.items():
        # param_desc 形如 "string, e.g. cam-01" / "int, default 5" / "string|null, ..."
        raw_type = param_desc.split(",")[0].split("|")[0].strip().lower()
        json_type = _TYPE_MAP.get(raw_type, "string")
        nullable = "|null" in param_desc.lower() or "| null" in param_desc.lower()

        prop: Dict[str, Any] = {"description": param_desc}
        if nullable:
            prop["anyOf"] = [{"type": json_type}, {"type": "null"}]
        else:
            prop["type"] = json_type

        properties[param_name] = prop
        if "default" not in param_desc.lower() and not nullable:
            required.append(param_name)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }


def _get_tool_list() -> List[Dict[str, Any]]:
    """将 TOOLS 注册表转换为 MCP tools/list 格式。"""
    from app.tools.registry import TOOLS

    result = []
    for name, meta in TOOLS.items():
        result.append({
            "name": name,
            "description": meta["description"],
            "inputSchema": _to_input_schema(meta["parameters"]),
            # 扩展字段：risk level（MCP spec 允许 vendor extensions）
            "_risk": meta.get("risk", "low"),
        })
    return result


# ── JSON-RPC 2.0 主端点 ───────────────────────────────────────


@mcp_router.post("/v1")
async def mcp_jsonrpc(request: Request) -> JSONResponse:
    """
    MCP JSON-RPC 2.0 统一入口。
    支持单个请求对象，也支持批量请求数组。
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            _err(None, -32700, "Parse error"),
            status_code=400,
        )

    # 批量请求
    if isinstance(body, list):
        responses = [_handle_single(item) for item in body]
        return JSONResponse(responses)

    return JSONResponse(_handle_single(body))


def _handle_single(msg: Any) -> Dict:
    """处理单个 JSON-RPC 请求对象。"""
    if not isinstance(msg, dict):
        return _err(None, -32600, "Invalid Request")

    req_id = msg.get("id")
    method = msg.get("method", "")
    params = msg.get("params") or {}

    log.debug("mcp_jsonrpc", method=method, req_id=req_id)

    try:
        if method == "initialize":
            return _handle_initialize(req_id, params)
        elif method == "tools/list":
            return _handle_tools_list(req_id, params)
        elif method == "tools/call":
            return _handle_tools_call(req_id, params)
        elif method == "ping":
            return _ok(req_id, {})
        elif method.startswith("notifications/"):
            # 通知消息：无需返回（但按规范不响应通知）
            return {}
        else:
            return _err(req_id, -32601, f"Method not found: {method}")
    except Exception as exc:
        log.warning("mcp_handler_error", method=method, error=str(exc))
        return _err(req_id, -32603, "Internal error", str(exc))


def _handle_initialize(req_id: Any, params: Dict) -> Dict:
    client_version = params.get("protocolVersion", "unknown")
    return _ok(req_id, {
        "protocolVersion": _PROTOCOL_VERSION,
        "capabilities": {
            "tools": {"listChanged": False},
        },
        "serverInfo": {
            "name": _SERVER_NAME,
            "version": _SERVER_VERSION,
        },
        "instructions": (
            "这是一个工业运维 Agent 工具服务器。"
            "所有工具返回 {ok, summary, data} 结构，summary 为中文摘要。"
        ),
    })


def _handle_tools_list(req_id: Any, params: Dict) -> Dict:
    tools = _get_tool_list()
    return _ok(req_id, {"tools": tools})


def _handle_tools_call(req_id: Any, params: Dict) -> Dict:
    from app.tools.registry import call_tool

    name = params.get("name")
    arguments = params.get("arguments") or {}

    if not name:
        return _err(req_id, -32602, "Invalid params: 'name' is required")

    result = call_tool(name, arguments)

    if not result.get("ok"):
        # MCP 规范：工具执行错误用 isError=true，不用 JSON-RPC 的 error 字段
        return _ok(req_id, {
            "content": [{"type": "text", "text": result.get("summary", "工具调用失败")}],
            "isError": True,
        })

    # 把完整结果序列化为 text content
    content_text = result.get("summary", "")
    data = result.get("data")
    if data is not None:
        try:
            content_text += "\n" + json.dumps(data, ensure_ascii=False, default=str)
        except Exception:
            pass

    return _ok(req_id, {
        "content": [{"type": "text", "text": content_text}],
        "isError": False,
        # 扩展：把结构化 data 也放进去，方便 client 直接解析
        "_structured": result,
    })


# ── REST 便捷端点 ────────────────────────────────────────────


@mcp_router.get("/v1/tools")
def list_tools_rest() -> Dict:
    """列出所有工具（REST 风格，无需 JSON-RPC 包装）。"""
    return {"tools": _get_tool_list()}


class ToolCallBody(BaseModel):
    arguments: Dict[str, Any] = {}


@mcp_router.post("/v1/tools/{tool_name}")
def call_tool_rest(tool_name: str, body: ToolCallBody) -> Dict:
    """直接调用工具（REST 风格）。返回 {ok, summary, data}。"""
    from app.tools.registry import call_tool, TOOLS

    if tool_name not in TOOLS:
        raise HTTPException(404, detail=f"工具 '{tool_name}' 不存在")

    result = call_tool(tool_name, body.arguments)
    if not result.get("ok"):
        raise HTTPException(422, detail=result.get("summary", "工具调用失败"))
    return result
