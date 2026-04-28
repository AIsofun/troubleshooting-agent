"""
Agent core loop.

This is the heart of the demo. The loop is intentionally simple so you can
see every piece of a real agent:

    while not done and step < max_steps:
        decision = llm.plan(query, tools, observations)   # THINK
        if decision.action == "final": break              # STOP
        result = call_tool(decision.tool, decision.args)  # ACT
        observations.append(...)                          # OBSERVE

This is the same Think-Act-Observe loop used by ReAct / OpenAI function calling
/ MCP clients. Only the planner (LLM) and tool backend differ.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.agent.llm import LLM, MockLLM
from app.tools.registry import call_tool, describe_tools, TOOLS


@dataclass
class TraceEvent:
    step: int
    kind: str          # "user" | "plan" | "tool_call" | "tool_result" | "final" | "error"
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    answer: Dict[str, Any]
    trace: List[TraceEvent]


class Agent:
    def __init__(self, llm: Optional[LLM] = None, max_steps: int = 6,
                 on_event: Optional[Callable[[TraceEvent], None]] = None):
        self.llm = llm or MockLLM()
        self.max_steps = max_steps
        self.on_event = on_event or (lambda e: None)

    def _emit(self, ev: TraceEvent, trace: List[TraceEvent]) -> None:
        trace.append(ev)
        try:
            self.on_event(ev)
        except Exception:  # noqa: BLE001
            pass

    def run(self, user_query: str) -> AgentResult:
        trace: List[TraceEvent] = []
        observations: List[Dict[str, Any]] = []

        self._emit(TraceEvent(0, "user", {"query": user_query}), trace)

        tools_desc = describe_tools()

        for step in range(1, self.max_steps + 1):
            decision = self.llm.plan(user_query, tools_desc, observations)
            self._emit(TraceEvent(step, "plan", {
                "action": decision.get("action"),
                "tool": decision.get("tool"),
                "args": decision.get("args"),
                "thought": decision.get("thought"),
            }), trace)

            if decision.get("action") == "final":
                answer = decision.get("answer", {})
                self._emit(TraceEvent(step, "final", {"answer": answer}), trace)
                return AgentResult(answer=answer, trace=trace)

            if decision.get("action") != "tool_call":
                self._emit(TraceEvent(step, "error", {"msg": "invalid plan"}), trace)
                break

            tool = decision["tool"]
            args = decision.get("args", {})

            # --- policy check: block high-risk tools unless explicitly allowed ---
            risk = TOOLS.get(tool, {}).get("risk", "low")
            if risk == "high":
                args = {**args, "dry_run": True}
                self._emit(TraceEvent(step, "tool_call", {
                    "tool": tool, "args": args, "policy": "high-risk -> dry_run"
                }), trace)
            else:
                self._emit(TraceEvent(step, "tool_call", {"tool": tool, "args": args}), trace)

            result = call_tool(tool, args)
            observations.append({"tool": tool, "args": args, "result": result})
            self._emit(TraceEvent(step, "tool_result", {
                "tool": tool,
                "ok": result.get("ok"),
                "summary": result.get("summary"),
            }), trace)

        # max steps reached
        fallback = {
            "intent": "unknown",
            "conclusion": "达到最大步数仍未得到结论。",
            "evidence": [o["result"].get("summary") for o in observations],
            "suggestions": [],
            "safe_actions": [],
        }
        self._emit(TraceEvent(self.max_steps, "final", {"answer": fallback}), trace)
        return AgentResult(answer=fallback, trace=trace)
