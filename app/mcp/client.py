"""
MCP Client — 连接远程 MCP 服务器，获取工具列表并调用工具。

支持的传输层：
  HTTP JSON-RPC  (http:// 或 https://)  ← 目前实现
  SSE 传输层待 P9 扩展

工作流：
  client = MCPClient("http://other-agent:8080/mcp/v1", name="camera-server")
  await client.connect()              # initialize + tools/list
  result = await client.call("get_camera_status", {"camera_id": "cam-01"})

同步版本（适合非 async 调用方）：
  client = MCPClientSync("http://...", name="camera-server")
  client.connect()
  result = client.call("get_camera_status", {"camera_id": "cam-01"})

降级设计：
  - connect() 失败时打 warning，不抛异常（client.is_available() 返回 False）
  - call() 时不可用则返回 {ok:False, summary:"MCP server unavailable"}
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional

from app.observability.logging import get_logger

log = get_logger(__name__)

_PROTOCOL_VERSION = "2024-11-05"
_DEFAULT_TIMEOUT = 10.0   # seconds


class MCPClientSync:
    """
    同步 MCP 客户端（基于 urllib / requests，零额外依赖）。

    优先使用 requests（如已安装），降级使用 urllib.request。
    """

    def __init__(
        self,
        base_url: str,
        name: str = "remote",
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        # 规范化 URL：去掉末尾 /
        self.base_url = base_url.rstrip("/")
        self.name = name
        self.timeout = timeout

        self._available: bool = False
        self._tools: List[Dict[str, Any]] = []
        self._tool_index: Dict[str, Dict[str, Any]] = {}
        self._req_counter: int = 0

    # ── 连接 / 握手 ────────────────────────────────────────

    def connect(self) -> bool:
        """
        执行 initialize + tools/list 握手。
        成功返回 True，失败返回 False（已降级）。
        """
        try:
            # initialize
            init_resp = self._jsonrpc("initialize", {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agentdemo-client", "version": "1.0.0"},
            })
            if "error" in init_resp:
                log.warning("mcp_init_error", server=self.name, error=init_resp["error"])
                return False

            # tools/list
            list_resp = self._jsonrpc("tools/list", {})
            if "error" in list_resp:
                log.warning("mcp_list_error", server=self.name, error=list_resp["error"])
                return False

            self._tools = list_resp.get("result", {}).get("tools", [])
            self._tool_index = {t["name"]: t for t in self._tools}
            self._available = True
            log.info(
                "mcp_connected",
                server=self.name,
                url=self.base_url,
                tools=len(self._tools),
            )
            return True

        except Exception as exc:
            log.warning(
                "mcp_connect_failed server=%s url=%s error=%s",
                self.name, self.base_url, exc,
            )
            self._available = False
            return False

    def is_available(self) -> bool:
        return self._available

    def tools(self) -> List[Dict[str, Any]]:
        """返回从远端获取的工具列表（MCP 格式）。"""
        return list(self._tools)

    def has_tool(self, name: str) -> bool:
        return name in self._tool_index

    # ── 工具调用 ────────────────────────────────────────────

    def call(self, tool_name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """
        调用远程工具。
        返回本地统一格式：{ok, summary, data}。
        """
        if not self._available:
            return {
                "ok": False,
                "summary": f"MCP server '{self.name}' is unavailable",
                "data": None,
            }
        if not self.has_tool(tool_name):
            return {
                "ok": False,
                "summary": f"Tool '{tool_name}' not found on MCP server '{self.name}'",
                "data": None,
            }

        try:
            resp = self._jsonrpc("tools/call", {
                "name": tool_name,
                "arguments": arguments,
            })
        except Exception as exc:
            log.warning("mcp_call_error", server=self.name, tool=tool_name, error=str(exc))
            return {
                "ok": False,
                "summary": f"MCP call failed: {exc}",
                "data": None,
            }

        if "error" in resp:
            return {
                "ok": False,
                "summary": resp["error"].get("message", "Remote tool error"),
                "data": None,
            }

        result = resp.get("result", {})
        is_error = result.get("isError", False)

        # 提取 text content
        content_text = ""
        for c in result.get("content", []):
            if c.get("type") == "text":
                content_text = c.get("text", "")
                break

        # 优先使用服务器扩展的 _structured 字段（agentdemo MCP server 提供）
        structured = result.get("_structured")
        if structured:
            return structured

        # 尝试从 content_text 解析 JSON
        data = None
        lines = content_text.split("\n", 1)
        summary = lines[0]
        if len(lines) > 1:
            try:
                data = json.loads(lines[1])
            except Exception:
                pass

        return {
            "ok": not is_error,
            "summary": summary,
            "data": data,
        }

    # ── 底层 HTTP JSON-RPC ───────────────────────────────────

    def _jsonrpc(self, method: str, params: Any) -> Dict[str, Any]:
        self._req_counter += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._req_counter,
            "method": method,
            "params": params,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        url = f"{self.base_url}/v1"
        return self._http_post(url, body)

    def _http_post(self, url: str, body: bytes) -> Dict[str, Any]:
        """HTTP POST，优先 requests，降级 urllib。"""
        try:
            import requests as _req
            resp = _req.post(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            return resp.json()
        except ImportError:
            pass

        # urllib fallback
        import urllib.request
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
