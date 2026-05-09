"""
Agent core loop — Phase 7: Verify-Replan 状态机。

完整执行流程：
  outer: replan loop (max_replan 次)
    inner: ReAct loop (max_steps 步)
      THINK → ACT → OBSERVE → (repeat) → FINAL answer
    VERIFY: AnswerVerifier.verify(answer, obs)
      passed → return AgentResult
      failed → append replan_hint → continue outer

预算守护（任一触发则终止）：
  - 总 wall-clock 时间 > budget_seconds
  - 累计工具调用次数 > budget_tool_calls

TraceEvent kinds：
  user / plan / tool_call / tool_result / final /
  verify / replan / budget_exceeded / error
"""
from __future__ import annotations
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.agent.llm import LLM, MockLLM
from app.tools.registry import call_tool, describe_tools, TOOLS


# ── 答案健康检查 & 兜底合成（向后兼容）────────────────────

_MIN_CONCLUSION_LEN = 12
_GIBBERISH_RE = re.compile(
    r'^[^\u4e00-\u9fff\w]*$'
    r'|^.{0,20}["\'=]\s*$'
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
    """模型最终总结失败时的确定性兜底。"""
    evidence, suggestions, safe_actions = [], [], []
    for o in observations:
        summary = o.get("result", {}).get("summary", "")
        if summary:
            evidence.append(f"{o['tool']}: {summary}")
        data = o.get("result", {}).get("data") or {}
        if isinstance(data, dict):
            suggestions.extend(data.get("steps", []) or [])
            safe_actions.extend(data.get("safe_actions", []) or [])
    conclusion = (
        "针对\u201c{}\u201d，已通过 {} 次工具调用采集到证据。"
        "（模型最终总结环节输出异常，已根据工具结果自动整合结论）"
    ).format(user_query, len(observations))
    return {
        "intent": "synthesized",
        "conclusion": conclusion,
        "evidence": evidence,
        "suggestions": suggestions,
        "safe_actions": safe_actions,
    }


# ── 数据模型 ──────────────────────────────────────────────

@dataclass
class TraceEvent:
    step: int
    kind: str
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class AgentResult:
    answer: Dict[str, Any]
    trace: List[TraceEvent]
    verify_score: float = 0.0
    replan_count: int = 0
    budget_exceeded: bool = False


# ── Agent ────────────────────────────────────────────────

class Agent:
    """
    ReAct + Verify-Replan Agent。

    参数：
        llm              : LLM 实例（默认 MockLLM）
        max_steps        : 每轮 ReAct 的最大步数
        max_replan       : Verify-Replan 外循环最大重规划次数（默认 2）
        budget_seconds   : 总执行时间上限（秒）
        budget_tool_calls: 总工具调用次数上限
        enable_verify    : 是否开启 Verify-Replan
        on_event         : 事件回调（供 SSE / websocket 推送）
    """

    def __init__(
        self,
        llm: Optional[LLM] = None,
        max_steps: int = 6,
        max_replan: int = 2,
        budget_seconds: float = 120.0,
        budget_tool_calls: int = 12,
        enable_verify: bool = True,
        on_event: Optional[Callable[[TraceEvent], None]] = None,
    ):
        self.llm = llm or MockLLM()
        self.max_steps = max_steps
        self.max_replan = max_replan
        self.budget_seconds = budget_seconds
        self.budget_tool_calls = budget_tool_calls
        self.enable_verify = enable_verify
        self.on_event = on_event or (lambda e: None)

    @classmethod
    def from_config(cls, llm: Optional[LLM] = None) -> "Agent":
        """从 config/base.yaml 读取参数构建 Agent。"""
        try:
            from app.config import get_settings
            cfg = get_settings().get("agent", {})
            return cls(
                llm=llm,
                max_steps=cfg.get("max_steps", 8),
                max_replan=cfg.get("max_replan", 2),
                budget_seconds=cfg.get("budget_seconds", 120.0),
                budget_tool_calls=cfg.get("budget_tool_calls", 12),
                enable_verify=cfg.get("enable_verify", True),
            )
        except Exception:
            return cls(llm=llm)

    def _emit(self, ev: TraceEvent, trace: List[TraceEvent]) -> None:
        trace.append(ev)
        try:
            self.on_event(ev)
        except Exception:
            pass

    # ── 主入口 ─────────────────────────────────────────────

    def run(self, user_query: str) -> AgentResult:
        trace: List[TraceEvent] = []
        all_observations: List[Dict[str, Any]] = []
        total_tool_calls = 0
        t_start = time.monotonic()
        replan_count = 0
        last_answer: Dict[str, Any] = {}
        last_verify_score: float = 0.0
        accumulated_hint = ""

        self._emit(TraceEvent(0, "user", {"query": user_query}), trace)

        for replan_round in range(self.max_replan + 1):
            # 预算检查
            elapsed = time.monotonic() - t_start
            if elapsed >= self.budget_seconds:
                self._emit(TraceEvent(len(trace), "budget_exceeded", {
                    "reason": "time",
                    "elapsed_sec": round(elapsed, 2),
                }), trace)
                return AgentResult(
                    answer=last_answer or self._timeout_answer(user_query, all_observations),
                    trace=trace, verify_score=last_verify_score,
                    replan_count=replan_count, budget_exceeded=True,
                )
            if total_tool_calls >= self.budget_tool_calls:
                self._emit(TraceEvent(len(trace), "budget_exceeded", {
                    "reason": "tool_calls",
                    "total_tool_calls": total_tool_calls,
                }), trace)
                return AgentResult(
                    answer=last_answer or self._timeout_answer(user_query, all_observations),
                    trace=trace, verify_score=last_verify_score,
                    replan_count=replan_count, budget_exceeded=True,
                )

            effective_query = user_query
            if accumulated_hint:
                effective_query = f"{user_query}\n\n{accumulated_hint}"

            answer, round_obs, tc = self._react_round(
                effective_query, all_observations, trace,
                budget_seconds_left=self.budget_seconds - elapsed,
                budget_tool_calls_left=self.budget_tool_calls - total_tool_calls,
                step_offset=len(trace),
            )
            all_observations.extend(round_obs)
            total_tool_calls += tc
            last_answer = answer

            # Verify
            if not self.enable_verify:
                return AgentResult(answer=answer, trace=trace,
                                   verify_score=1.0, replan_count=replan_count)

            from app.agent.verifier import get_verifier
            vr = get_verifier().verify(answer, all_observations)
            last_verify_score = vr.score

            self._emit(TraceEvent(len(trace), "verify", {
                "passed": vr.passed, "score": vr.score,
                "issues": vr.issues, "replan_round": replan_round,
            }), trace)

            if vr.passed:
                return AgentResult(answer=answer, trace=trace,
                                   verify_score=vr.score, replan_count=replan_count)

            if replan_round >= self.max_replan:
                self._emit(TraceEvent(len(trace), "replan", {
                    "action": "max_replan_reached", "replan_round": replan_round,
                }), trace)
                return AgentResult(answer=answer, trace=trace,
                                   verify_score=vr.score, replan_count=replan_count)

            replan_count += 1
            accumulated_hint = vr.replan_hint
            self._emit(TraceEvent(len(trace), "replan", {
                "action": "triggered", "replan_round": replan_round,
                "hint": accumulated_hint, "score": vr.score,
            }), trace)

        return AgentResult(answer=last_answer, trace=trace,
                           verify_score=last_verify_score, replan_count=replan_count)

    # ── ReAct 内循环 ────────────────────────────────────────

    def _react_round(
        self,
        query: str,
        prior_observations: List[Dict[str, Any]],
        trace: List[TraceEvent],
        budget_seconds_left: float,
        budget_tool_calls_left: int,
        step_offset: int,
    ):
        """执行一轮 ReAct 内循环。返回 (answer, new_observations, tool_call_count)。"""
        observations = list(prior_observations)
        new_observations: List[Dict[str, Any]] = []
        tool_call_count = 0
        t_round = time.monotonic()
        tools_desc = describe_tools()

        for step in range(1, self.max_steps + 1):
            if time.monotonic() - t_round >= budget_seconds_left:
                break
            if tool_call_count >= budget_tool_calls_left:
                break

            decision = self.llm.plan(query, tools_desc, observations)
            self._emit(TraceEvent(step_offset + step, "plan", {
                "action": decision.get("action"),
                "tool": decision.get("tool"),
                "args": decision.get("args"),
                "thought": decision.get("thought"),
            }), trace)

            if decision.get("action") == "final":
                answer = decision.get("answer", {})
                if _answer_looks_broken(answer) and observations:
                    answer = _synthesize_from_observations(query, observations)
                    self._emit(TraceEvent(step_offset + step, "plan", {
                        "action": "fallback_synthesize",
                        "thought": "LLM final answer looked broken; synthesized from observations",
                    }), trace)
                self._emit(TraceEvent(step_offset + step, "final", {"answer": answer}), trace)
                return answer, new_observations, tool_call_count

            if decision.get("action") != "tool_call":
                self._emit(TraceEvent(step_offset + step, "error",
                                      {"msg": "invalid plan"}), trace)
                break

            tool = decision["tool"]
            args = decision.get("args", {})
            risk = TOOLS.get(tool, {}).get("risk", "low")
            if risk == "high":
                args = {**args, "dry_run": True}
                self._emit(TraceEvent(step_offset + step, "tool_call", {
                    "tool": tool, "args": args, "policy": "high-risk -> dry_run"
                }), trace)
            else:
                self._emit(TraceEvent(step_offset + step, "tool_call",
                                      {"tool": tool, "args": args}), trace)

            result = call_tool(tool, args)
            obs = {"tool": tool, "args": args, "result": result}
            observations.append(obs)
            new_observations.append(obs)
            tool_call_count += 1

            self._emit(TraceEvent(step_offset + step, "tool_result", {
                "tool": tool, "ok": result.get("ok"), "summary": result.get("summary"),
            }), trace)

        fallback = {
            "intent": "unknown",
            "conclusion": "达到最大步数仍未得到结论。",
            "evidence": [o["result"].get("summary") for o in new_observations],
            "suggestions": [],
            "safe_actions": [],
        }
        self._emit(TraceEvent(step_offset + self.max_steps, "final",
                              {"answer": fallback}), trace)
        return fallback, new_observations, tool_call_count

    @staticmethod
    def _timeout_answer(query: str, observations: List[Dict[str, Any]]) -> Dict[str, Any]:
        evidence = [
            f"{o['tool']}: {o['result'].get('summary', '')}"
            for o in observations if o.get("result", {}).get("ok")
        ]
        return {
            "intent": "unknown",
            "conclusion": "执行超出时间/调用预算，\u201c{}\u201d未能完成完整排查。".format(query[:40]),
            "evidence": evidence,
            "suggestions": ["请稍后重试，或缩短问题范围"],
            "safe_actions": [],
            "_budget_exceeded": True,
        }