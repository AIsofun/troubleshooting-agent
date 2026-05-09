"""
MCP Adapter — 统一工具分发层。

将本地 TOOLS 注册表与零个或多个远程 MCP 客户端合并，提供统一的
call_tool / describe_tools / list_all_tools 接口。

Agent / LLM 层完全不感知工具是"本地"还是"远程 MCP"。

架构：
  ┌─────────────────────────────────────────┐
  │              MCPAdapter                  │
  │                                          │
  │  local TOOLS (always available)          │
  │  + [MCPClientSync, ...]  (optional)      │
  │                                          │
  │  call_tool(name, args)                   │
  │    ├─ name in local TOOLS → local call   │
  │    └─ name in remote MCP → remote call   │
  └─────────────────────────────────────────┘

初始化（由 app/web/server.py lifespan 调用）：
  adapter = get_adapter()  # 全局单例
  adapter.register_server(MCPClientSync(url, name))
  adapter.connect_all()

配置驱动（从 config/base.yaml）：
  mcp:
    servers:
      - name: camera-server
        url: http://192.168.1.100:8080/mcp
      - name: plc-server
        url: http://192.168.1.101:8080/mcp
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.observability.logging import get_logger

log = get_logger(__name__)


class MCPAdapter:
    """
    统一工具分发层。
    本地 TOOLS 优先；本地没有的工具名，按注册顺序查询远程 MCP 服务器。
    """

    def __init__(self) -> None:
        self._remote_clients: List["MCPClientSync"] = []  # type: ignore[name-defined]
        # tool_name → MCPClientSync 的路由表（connect_all 后填充）
        self._remote_tool_index: Dict[str, "MCPClientSync"] = {}  # type: ignore[name-defined]

    # ── 服务器管理 ───────────────────────────────────────────

    def register_server(self, client) -> None:
        """注册一个远程 MCP 客户端（未连接，connect_all 后才生效）。"""
        self._remote_clients.append(client)
        log.info("mcp_server_registered", name=client.name, url=client.base_url)

    def connect_all(self) -> Dict[str, bool]:
        """
        尝试连接所有已注册的远程 MCP 服务器。
        返回 {server_name: success} 字典。
        连接失败不抛异常（降级运行）。
        """
        results: Dict[str, bool] = {}
        self._remote_tool_index.clear()

        for client in self._remote_clients:
            ok = client.connect()
            results[client.name] = ok
            if ok:
                # 将远端工具名注册到路由表（本地重名时本地优先）
                from app.tools.registry import TOOLS
                for tool in client.tools():
                    tool_name = tool["name"]
                    if tool_name not in TOOLS and tool_name not in self._remote_tool_index:
                        self._remote_tool_index[tool_name] = client

        log.info(
            "mcp_connect_all_done",
            total=len(self._remote_clients),
            connected=sum(1 for v in results.values() if v),
            remote_tools=len(self._remote_tool_index),
        )
        return results

    def remote_tool_count(self) -> int:
        return len(self._remote_tool_index)

    def connected_servers(self) -> List[str]:
        return [c.name for c in self._remote_clients if c.is_available()]

    # ── 工具调用 ────────────────────────────────────────────

    def call_tool(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        调用工具（本地优先，远程 MCP 兜底）。
        与原 registry.call_tool 返回格式完全一致：{ok, summary, data}。
        """
        from app.tools.registry import TOOLS

        # 1. 本地 TOOLS 优先（直接调用 fn，避免经过 registry.call_tool 造成递归）
        if name in TOOLS:
            fn = TOOLS[name]["fn"]
            try:
                return fn(**args)
            except TypeError as e:
                return {"ok": False, "summary": f"bad args for {name}: {e}", "data": None}
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "summary": f"tool {name} failed: {e}", "data": None}

        # 2. 远程 MCP 工具
        client = self._remote_tool_index.get(name)
        if client:
            log.debug("mcp_remote_call", tool=name, server=client.name)
            return client.call(name, args)

        return {"ok": False, "summary": f"unknown tool: {name}", "data": None}

    # ── 工具发现 ────────────────────────────────────────────

    def describe_all_tools(self) -> str:
        """
        生成所有工具的文本描述（供 LLM system prompt 使用）。
        格式与原 registry.describe_tools() 一致。
        """
        from app.tools.registry import TOOLS

        lines = []
        # 本地工具
        for name, meta in TOOLS.items():
            lines.append(
                f"- {name}({meta['parameters']}) "
                f"[risk={meta['risk']}]: {meta['description']}"
            )
        # 远程 MCP 工具（追加，标注来源）
        for name, client in self._remote_tool_index.items():
            tool_meta = next(
                (t for t in client.tools() if t["name"] == name), {}
            )
            desc = tool_meta.get("description", "")
            schema = tool_meta.get("inputSchema", {})
            props = list(schema.get("properties", {}).keys())
            lines.append(
                f"- {name}({', '.join(props)}) "
                f"[risk=low, source=mcp:{client.name}]: {desc}"
            )
        return "\n".join(lines)

    def list_all_tools(self) -> List[Dict[str, Any]]:
        """
        列出所有工具（本地 + 远程），MCP 格式。
        供 /mcp/v1/tools 端点使用。
        """
        from app.mcp.server import _get_tool_list, _to_input_schema
        from app.tools.registry import TOOLS

        tools = _get_tool_list()   # 本地工具（MCP 格式）

        # 追加远程工具
        for name, client in self._remote_tool_index.items():
            tool_meta = next(
                (t for t in client.tools() if t["name"] == name), {}
            )
            tools.append({
                "name": name,
                "description": tool_meta.get("description", ""),
                "inputSchema": tool_meta.get("inputSchema", {"type": "object", "properties": {}}),
                "_risk": "low",
                "_source": f"mcp:{client.name}",
            })
        return tools


# ── 全局单例 ─────────────────────────────────────────────────

_ADAPTER: Optional[MCPAdapter] = None


def get_adapter() -> MCPAdapter:
    """获取全局 MCPAdapter 单例（延迟初始化）。"""
    global _ADAPTER
    if _ADAPTER is None:
        _ADAPTER = MCPAdapter()
    return _ADAPTER


def init_adapter_from_config() -> MCPAdapter:
    """
    从 config/base.yaml 的 mcp.servers 节点初始化 adapter。
    在 lifespan 中调用一次。
    """
    from app.mcp.client import MCPClientSync

    adapter = get_adapter()

    try:
        from app.config import get_settings
        settings = get_settings()
        servers = settings.get("mcp", {}).get("servers", [])
    except Exception:
        servers = []

    for srv in servers:
        url = srv.get("url", "")
        name = srv.get("name", url)
        if not url:
            continue
        client = MCPClientSync(url, name=name)
        adapter.register_server(client)

    if adapter._remote_clients:
        adapter.connect_all()
    else:
        log.info("mcp_no_remote_servers_configured")

    return adapter
