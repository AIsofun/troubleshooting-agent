"""
LLM interface. The agent does not care whether it's a real LLM or a mock.

`plan(user_query, tools_desc, observations)` should return either:
  {"action": "tool_call", "tool": "...", "args": {...}, "thought": "short public reason"}
  {"action": "final",     "answer": {...}}

Swap `MockLLM` with a real implementation (OpenAI / Azure / local) by
implementing the same `plan` method.
"""
from __future__ import annotations
import re
from typing import Any, Dict, List, Protocol


class LLM(Protocol):
    def plan(self, user_query: str, tools_desc: str,
             observations: List[Dict[str, Any]]) -> Dict[str, Any]: ...


# ---------- Mock LLM: rule-based, deterministic, easy to read ----------

class MockLLM:
    """
    A deterministic 'planner' that fakes an LLM.
    It inspects the user query + previous tool observations and decides
    the next step. Replace with a real LLM later.
    """

    # --- intent detection ---
    @staticmethod
    def _intent(q: str) -> str:
        ql = q.lower()
        if re.search(r"(相机|camera|cam-\d+|掉线|没有图像|无图像)", ql):
            return "camera_offline"
        if re.search(r"(ocr|识别|成功率|准确率)", ql):
            return "ocr_quality_drop"
        if re.search(r"(kafka|堆积|lag|消费)", ql):
            return "kafka_backlog"
        if re.search(r"(推理|inference|延迟|latency|p99|慢)", ql):
            return "inference_latency_high"
        return "unknown"

    @staticmethod
    def _extract_camera_id(q: str) -> str:
        m = re.search(r"cam-?(\d+)", q, re.I)
        if m:
            return f"cam-{int(m.group(1)):02d}"
        m = re.search(r"(\d+)\s*号\s*相机", q)
        if m:
            return f"cam-{int(m.group(1)):02d}"
        return "cam-02"  # reasonable default in this demo

    def plan(self, user_query: str, tools_desc: str,
             observations: List[Dict[str, Any]]) -> Dict[str, Any]:
        intent = self._intent(user_query)
        done_tools = {o["tool"] for o in observations}

        # Build a small plan per intent. Each step picks ONE tool.
        if intent == "camera_offline":
            cam = self._extract_camera_id(user_query)
            plan_steps = [
                ("get_camera_status",  {"camera_id": cam}),
                ("get_recent_logs",    {"service_name": "camera-service", "limit": 5}),
                ("query_runbook",      {"issue_type": "camera_offline"}),
            ]
        elif intent == "ocr_quality_drop":
            plan_steps = [
                ("get_model_metrics",  {"model_name": "ocr-v3"}),
                ("get_recent_logs",    {"service_name": "ocr-service", "limit": 5}),
                ("query_runbook",      {"issue_type": "ocr_quality_drop"}),
            ]
        elif intent == "kafka_backlog":
            plan_steps = [
                ("get_kafka_backlog",  {"topic": "vision.events"}),
                ("get_recent_logs",    {"service_name": "kafka-consumer", "limit": 5}),
                ("query_runbook",      {"issue_type": "kafka_backlog"}),
            ]
        elif intent == "inference_latency_high":
            plan_steps = [
                ("get_model_metrics",  {"model_name": "inference-gw"}),
                ("get_recent_logs",    {"service_name": "inference-gateway", "limit": 5}),
                ("query_runbook",      {"issue_type": "inference_latency_high"}),
            ]
        else:
            return {
                "action": "final",
                "answer": {
                    "conclusion": "无法识别该问题类型，请补充关键词（相机/OCR/Kafka/推理延迟）。",
                    "evidence": [],
                    "suggestions": [],
                    "intent": intent,
                },
            }

        # pick first step not yet executed
        for tool, args in plan_steps:
            if tool not in done_tools:
                return {
                    "action": "tool_call",
                    "tool": tool,
                    "args": args,
                    "thought": f"intent={intent}; need data from {tool}",
                }

        # all steps done -> synthesize a final answer
        return {"action": "final", "answer": self._synthesize(intent, observations)}

    # --- final answer synthesis ---
    @staticmethod
    def _synthesize(intent: str, obs: List[Dict[str, Any]]) -> Dict[str, Any]:
        by_tool = {o["tool"]: o["result"] for o in obs}
        evidence = [f"{o['tool']}: {o['result'].get('summary')}" for o in obs]
        rb = by_tool.get("query_runbook", {}).get("data") or {}
        suggestions = rb.get("steps", [])
        safe_actions = rb.get("safe_actions", [])

        if intent == "camera_offline":
            cam = by_tool.get("get_camera_status", {}).get("data") or {}
            if cam.get("status") == "offline":
                conclusion = (
                    f"相机 {cam.get('ip','?')} 已离线，最近 {cam.get('last_frame_sec')}s 无帧，"
                    "日志显示 RTSP 连接被重置且多次重连失败。初判为链路或设备侧故障。"
                )
            elif cam.get("status") == "degraded":
                conclusion = f"相机处于降级状态（fps={cam.get('fps')}），疑似链路抖动。"
            else:
                conclusion = "相机当前在线，问题可能已自行恢复，建议继续观察。"
        elif intent == "ocr_quality_drop":
            m = by_tool.get("get_model_metrics", {}).get("data") or {}
            conclusion = (
                f"OCR 成功率 {m.get('success_rate')} 明显低于基线 {m.get('baseline')}，"
                "日志同时出现输入图像亮度偏低告警。初判为上游图像质量下降导致。"
            )
        elif intent == "kafka_backlog":
            k = by_tool.get("get_kafka_backlog", {}).get("data") or {}
            conclusion = (
                f"topic 消费堆积 lag={k.get('lag')}，消费者数={k.get('consumers')}，"
                "并出现 rebalance 事件。初判为消费能力不足 + 消费者抖动。"
            )
        elif intent == "inference_latency_high":
            m = by_tool.get("get_model_metrics", {}).get("data") or {}
            conclusion = (
                f"推理 p99={m.get('p99_latency_ms')}ms 明显升高，"
                "GPU 利用率接近饱和，队列深度增长。初判为容量瓶颈。"
            )
        else:
            conclusion = "未知问题。"

        return {
            "intent": intent,
            "conclusion": conclusion,
            "evidence": evidence,
            "suggestions": suggestions,
            "safe_actions": safe_actions,
        }
