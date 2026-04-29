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
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.agent.llm import LLM, MockLLM
from app.tools.registry import call_tool, describe_tools, TOOLS


# ---------- 答案健康检查 & 兜底合成 ----------
# 本地模型偶尔会在"最终总结"环节产出乱码片段（例如 'iNdEx="'）。
# 此时工具数据已经采集齐全，丢弃太可惜——直接基于 observations 合成一份结构化答案。

_MIN_CONCLUSION_LEN = 12        # 少于这个长度基本可以判定不是有效结论
_GIBBERISH_RE = re.compile(     # 明显乱码特征：含未闭合引号 / 全是符号 / 形如 key=" 这种残片
    r'^[^\u4e00-\u9fff\w]*$'    # 没有任何中文 / 字母数字
    r'|^.{0,20}["\'=]\s*$'      # 短文本以 = 或未闭合引号结尾
)


def _answer_looks_broken(ans: Dict[str, Any]) -> bool:
    if not isinstance(ans, dict):
        return True
    conclusion = (ans.get("conclusion") or "").strip()
    if len(conclusion) < _MIN_CONCLUSION_LEN:
        return True
    if _GIBBERISH_RE.match(conclusion):
        return True
    return False


def _synthesize_from_observations(
    user_query: str, observations: List[Dict[str, Any]]
) -> Dict[str, Any]:
    """模型最终总结失败时的确定性兜底：把工具结果整合成结构化答案。"""
    evidence = []
    suggestions = []
    safe_actions = []
    for o in observations:
        summary = o.get("result", {}).get("summary", "")
        if summary:
            evidence.append(f"{o['tool']}: {summary}")
        # runbook 工具自带处置建议，直接抽取
        data = o.get("result", {}).get("data") or {}
        if isinstance(data, dict):
            suggestions.extend(data.get("steps", []) or [])
            safe_actions.extend(data.get("safe_actions", []) or [])

    conclusion = (
        f"针对“{user_query}”，已通过 {len(observations)} 次工具调用采集到证据。"
        "（模型最终总结环节输出异常，已根据工具结果自动整合结论）"
    )
    return {
        "intent": "synthesized",
        "conclusion": conclusion,
        "evidence": evidence,
        "suggestions": suggestions,
        "safe_actions": safe_actions,
    }


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
                # 健康检查：模型有时会在最终总结时产出乱码（例如 'iNdEx="'）。
                # 此时工具数据已经齐全，自动合成一份结构化结论作为兜底。
                if _answer_looks_broken(answer) and observations:
                    answer = _synthesize_from_observations(user_query, observations)
                    self._emit(TraceEvent(step, "plan", {
                        "action": "fallback_synthesize",
                        "thought": "LLM final answer looked broken; synthesized from observations",
                    }), trace)
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
