"""
Mock tools for the agent. Each tool:
  - has a clear name, description, and parameter schema (OpenAI/MCP-compatible style)
  - returns a dict with structured data AND a short human summary
Real-world replacement: swap these functions with real API/CLI/MCP calls.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from typing import Any, Callable, Dict, Optional

DATA_DIR = Path(__file__).resolve().parent.parent / "mock_data"


def _load(name: str) -> Any:
    with open(DATA_DIR / name, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------- Tool implementations ----------

def get_camera_status(camera_id: str) -> Dict[str, Any]:
    data = _load("cameras.json")
    info = data.get(camera_id)
    if not info:
        return {"ok": False, "summary": f"camera {camera_id} not found", "data": None}
    summary = (
        f"{camera_id} status={info['status']} "
        f"last_frame={info['last_frame_sec']}s fps={info['fps']}"
    )
    return {"ok": True, "summary": summary, "data": info}


def get_recent_logs(service_name: str, limit: int = 5) -> Dict[str, Any]:
    data = _load("logs.json")
    logs = data.get(service_name, [])
    tail = logs[-limit:]
    errs = sum(1 for l in tail if "ERROR" in l)
    warns = sum(1 for l in tail if "WARN" in l)
    summary = f"{service_name}: {len(tail)} lines, {errs} ERROR, {warns} WARN"
    return {"ok": True, "summary": summary, "data": tail}


def get_kafka_backlog(topic: str) -> Dict[str, Any]:
    data = _load("kafka.json")
    info = data.get(topic)
    if not info:
        return {"ok": False, "summary": f"topic {topic} not found", "data": None}
    summary = (
        f"topic={topic} lag={info['lag']} consumers={info['consumers']} "
        f"rate={info['rate_msg_s']}/s"
    )
    return {"ok": True, "summary": summary, "data": info}


def get_model_metrics(model_name: str) -> Dict[str, Any]:
    data = _load("metrics.json")
    info = data.get(model_name)
    if not info:
        return {"ok": False, "summary": f"model {model_name} not found", "data": None}
    drop = info["baseline"] - info["success_rate"]
    summary = (
        f"{model_name}: success={info['success_rate']:.2f} "
        f"(baseline={info['baseline']:.2f}, drop={drop:.2f}) "
        f"p99={info['p99_latency_ms']}ms"
    )
    return {"ok": True, "summary": summary, "data": info}


def get_device_heartbeat(device_id: str) -> Dict[str, Any]:
    data = _load("heartbeat.json")
    info = data.get(device_id)
    if not info:
        return {"ok": False, "summary": f"device {device_id} not found", "data": None}
    summary = (
        f"{device_id}: status={info['status']} last_seen={info['last_seen_sec']}s "
        f"cpu={info['cpu']}% mem={info['mem']}%"
    )
    return {"ok": True, "summary": summary, "data": info}


def query_runbook(issue_type: str) -> Dict[str, Any]:
    data = _load("runbook.json")
    rb = data.get(issue_type)
    if not rb:
        return {"ok": False, "summary": f"no runbook for {issue_type}", "data": None}
    summary = f"runbook: {rb['title']} ({len(rb['steps'])} steps)"
    return {"ok": True, "summary": summary, "data": rb}


def restart_service(service_name: str, dry_run: bool = True) -> Dict[str, Any]:
    """High-risk action. In the demo it's always dry-run."""
    if dry_run or os.getenv("AGENT_ALLOW_RESTART") != "1":
        return {
            "ok": True,
            "summary": f"[DRY-RUN] would restart {service_name} (blocked by policy)",
            "data": {"service": service_name, "executed": False, "dry_run": True},
        }
    return {
        "ok": True,
        "summary": f"restart {service_name} executed (simulated)",
        "data": {"service": service_name, "executed": True, "dry_run": False},
    }


def search_knowledge(
    query: str,
    top_k: int = 5,
    alarm_code: Optional[str] = None,
    device_model: Optional[str] = None,
    doc_type: Optional[str] = None,
) -> Dict[str, Any]:
    """
    混合检索知识库（向量 + 关键词 + Rerank）。

    适用场景：
      - 查询企业内部经验、runbook、SOP、维修手册
      - 通过 alarm_code / device_model 精确匹配强关键词
      - 当 query_runbook 无法覆盖时，使用此工具做开放式知识检索

    降级：知识库不可用时自动返回空结果，不阻断 Agent 流程。
    """
    try:
        from app.knowledge.retriever import HybridRetriever
        retriever = HybridRetriever()
        results = retriever.search(
            query=query,
            top_k=top_k,
            alarm_code=alarm_code,
            device_model=device_model,
            doc_type=doc_type,
        )
        if not results:
            return {
                "ok": True,
                "summary": "知识库未找到与 [{}] 相关的内容".format(query[:40]),
                "data": {"results": [], "total": 0},
            }

        summary_items = [
            f"[{i+1}] {r.get('title', '无标题')} (相关度:{r.get('rerank_score', 0):.2f})"
            + (" ⭐精确命中" if r.get("exact_hit") else "")
            for i, r in enumerate(results)
        ]
        # 格式化为 LLM 可读摘要
        llm_text = retriever.format_for_llm(results)

        return {
            "ok": True,
            "summary": f"找到 {len(results)} 条相关知识: {'; '.join(summary_items[:3])}",
            "data": {
                "results": results,
                "total": len(results),
                "llm_context": llm_text,
            },
        }
    except Exception as exc:
        return {
            "ok": False,
            "summary": f"知识库检索失败: {exc}",
            "data": None,
        }



# ---------- Registry (OpenAI-function / MCP style schema) ----------

TOOLS: Dict[str, Dict[str, Any]] = {
    "get_camera_status": {
        "fn": get_camera_status,
        "description": "获取指定相机的在线状态、FPS、最近一帧时间。",
        "parameters": {"camera_id": "string, e.g. cam-01"},
        "risk": "low",
    },
    "get_recent_logs": {
        "fn": get_recent_logs,
        "description": "拉取某个服务最近若干行日志。",
        "parameters": {"service_name": "string", "limit": "int, default 5"},
        "risk": "low",
    },
    "get_kafka_backlog": {
        "fn": get_kafka_backlog,
        "description": "查询指定 Kafka topic 的消费堆积情况。",
        "parameters": {"topic": "string"},
        "risk": "low",
    },
    "get_model_metrics": {
        "fn": get_model_metrics,
        "description": "查询模型的成功率、延迟等指标。",
        "parameters": {"model_name": "string"},
        "risk": "low",
    },
    "get_device_heartbeat": {
        "fn": get_device_heartbeat,
        "description": "查询边缘设备心跳与资源占用。",
        "parameters": {"device_id": "string"},
        "risk": "low",
    },
    "query_runbook": {
        "fn": query_runbook,
        "description": "按问题类型查询运维手册（处置步骤）。",
        "parameters": {"issue_type": "one of: camera_offline, ocr_quality_drop, kafka_backlog, inference_latency_high"},
        "risk": "low",
    },
    "restart_service": {
        "fn": restart_service,
        "description": "重启一个服务。高风险动作，默认 dry-run。",
        "parameters": {"service_name": "string", "dry_run": "bool, default True"},
        "risk": "high",
    },
    "search_knowledge": {
        "fn": search_knowledge,
        "description": (
            "混合检索知识库（向量语义 + 关键词精确 + Rerank）。"
            "用于查询企业内部经验、SOP、维修手册、历史案例。"
            "当 query_runbook 无覆盖或问题类型未知时优先调用此工具。"
            "支持通过 alarm_code / device_model 精确命中强关键词。"
        ),
        "parameters": {
            "query": "string, 自然语言查询",
            "top_k": "int, 返回条数，default 5",
            "alarm_code": "string|null, 精确匹配报警码，例: ALG_FALSE_REJECT_HIGH",
            "device_model": "string|null, 精确匹配设备型号，例: MV-CA050-10GM",
            "doc_type": "string|null, 文档类型过滤: runbook|sop|case|manual|log",
        },
        "risk": "low",
    },
}


def call_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """
    调用工具。当有远程 MCP 工具时优先走 MCPAdapter（支持远程路由），
    否则直接调用本地 TOOLS。
    """
    # Phase 6: 如果 adapter 已有远程工具，优先走 adapter（adapter 内部直接调用 fn，不会递归）
    try:
        from app.mcp.adapter import _ADAPTER
        if _ADAPTER is not None and _ADAPTER.remote_tool_count() > 0:
            return _ADAPTER.call_tool(name, args)
    except ImportError:
        pass

    # 本地直接调用
    if name not in TOOLS:
        return {"ok": False, "summary": f"unknown tool {name}", "data": None}
    fn: Callable = TOOLS[name]["fn"]
    try:
        return fn(**args)
    except TypeError as e:
        return {"ok": False, "summary": f"bad args for {name}: {e}", "data": None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "summary": f"tool {name} failed: {e}", "data": None}


def describe_tools() -> str:
    """
    生成所有工具描述（本地 + 远程 MCP）。
    供 LLM system prompt 使用。
    """
    try:
        from app.mcp.adapter import get_adapter
        adapter = get_adapter()
        if adapter.remote_tool_count() > 0:
            return adapter.describe_all_tools()
    except ImportError:
        pass

    lines = []
    for name, meta in TOOLS.items():
        lines.append(f"- {name}({meta['parameters']}) [risk={meta['risk']}]: {meta['description']}")
    return "\n".join(lines)
