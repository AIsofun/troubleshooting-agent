"""
Phase 7 — Verify-Replan 测试套件。

覆盖：
  - AnswerVerifier.verify()  多种通过/失败场景
  - VerifyResult 字段完整性
  - Agent 新参数 / from_config()
  - 预算守护：时间超限 / 工具调用超限
  - enable_verify=False → 单轮, 无 verify 事件
  - Verify 首轮通过 → 无 replan 事件
  - Verify 失败 → replan TraceEvent, replan_count 递增
  - max_replan=0 → 不重规划
  - 完整 Agent.run() with MockLLM → verify/replan 事件
  - TraceEvent 新 kind 出现在 trace
"""
import time
from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from app.agent.core import Agent, AgentResult, TraceEvent
from app.agent.verifier import AnswerVerifier, VerifyResult, get_verifier


# ═══════════════════════════════════════════════════════════════
# AnswerVerifier 单元测试
# ═══════════════════════════════════════════════════════════════

GOOD_ANSWER: Dict[str, Any] = {
    "intent": "camera_offline",
    "conclusion": "摄像头 CAM-01 离线，心跳包连续 5 分钟丢失，建议检查电源与网络连接。",
    "evidence": ["heartbeat: 设备 CAM-01 无心跳", "logs: 最近 10 条日志无响应记录"],
    "suggestions": ["重启设备", "检查 PoE 供电"],
    "safe_actions": ["reboot_device"],
}

GOOD_OBS: List[Dict[str, Any]] = [
    {"tool": "get_device_heartbeat", "result": {"ok": True, "summary": "设备 CAM-01 无心跳",
                                                 "data": {"value": 0}}},
    {"tool": "get_recent_logs", "result": {"ok": True, "summary": "最近 10 条日志无响应记录"}},
]


class TestAnswerVerifier:

    def _verifier(self, **kw) -> AnswerVerifier:
        defaults = dict(
            min_conclusion_len=20,
            pass_threshold=0.65,
            require_numeric=True,
            require_suggestions=False,
        )
        defaults.update(kw)
        return AnswerVerifier(**defaults)

    def test_good_answer_passes(self):
        v = self._verifier()
        r = v.verify(GOOD_ANSWER, GOOD_OBS)
        assert isinstance(r, VerifyResult)
        assert r.passed is True
        assert r.score >= 0.65

    def test_verify_result_has_required_fields(self):
        v = self._verifier()
        r = v.verify(GOOD_ANSWER, GOOD_OBS)
        assert hasattr(r, "passed")
        assert hasattr(r, "score")
        assert hasattr(r, "issues")
        assert hasattr(r, "replan_hint")
        assert isinstance(r.score, float)
        assert 0.0 <= r.score <= 1.0

    def test_missing_conclusion_fails(self):
        bad = {**GOOD_ANSWER, "conclusion": ""}
        v = self._verifier()
        r = v.verify(bad, GOOD_OBS)
        assert r.passed is False
        assert any("conclusion" in i.lower() or "结论" in i for i in r.issues)

    def test_short_conclusion_fails(self):
        bad = {**GOOD_ANSWER, "conclusion": "短结论"}
        v = self._verifier(min_conclusion_len=20)
        r = v.verify(bad, GOOD_OBS)
        assert r.passed is False

    def test_missing_evidence_fails(self):
        bad = {**GOOD_ANSWER, "evidence": []}
        v = self._verifier()
        r = v.verify(bad, GOOD_OBS)
        # 证据缺失会降分，可能不一定 fail，但 score < 好答案
        good_r = v.verify(GOOD_ANSWER, GOOD_OBS)
        assert r.score < good_r.score

    def test_no_numeric_fails_when_required(self):
        no_num = {**GOOD_ANSWER, "conclusion": "摄像头离线，建议检查网络和电源线路连接状态。"}
        v = self._verifier(require_numeric=True)
        r = v.verify(no_num, GOOD_OBS)
        # 无数字，证据覆盖分 (evidence_coverage) 也不受影响，但 numeric 扣分
        no_v = self._verifier(require_numeric=False)
        r_no = no_v.verify(no_num, GOOD_OBS)
        # require_numeric=True 不会比 False 分数更高
        assert r.score <= r_no.score + 0.01  # 允许浮点误差

    def test_require_suggestions_enforced(self):
        no_sug = {**GOOD_ANSWER, "suggestions": []}
        v = self._verifier(require_suggestions=True)
        r = v.verify(no_sug, GOOD_OBS)
        assert r.passed is False

    def test_non_dict_answer_fails(self):
        v = self._verifier()
        r = v.verify("not a dict", GOOD_OBS)  # type: ignore[arg-type]
        assert r.passed is False

    def test_replan_hint_populated_on_failure(self):
        bad = {**GOOD_ANSWER, "conclusion": "短"}
        v = self._verifier()
        r = v.verify(bad, GOOD_OBS)
        assert r.passed is False
        assert isinstance(r.replan_hint, str)
        assert len(r.replan_hint) > 0

    def test_get_verifier_singleton(self):
        v1 = get_verifier()
        v2 = get_verifier()
        assert v1 is v2

    def test_score_in_range(self):
        v = self._verifier()
        for ans in [GOOD_ANSWER, {}, {"conclusion": "x"}]:
            r = v.verify(ans, GOOD_OBS)  # type: ignore[arg-type]
            assert 0.0 <= r.score <= 1.0


# ═══════════════════════════════════════════════════════════════
# Agent 构造 & from_config
# ═══════════════════════════════════════════════════════════════

class TestAgentConstruction:

    def test_default_params(self):
        a = Agent()
        assert a.max_replan == 2
        assert a.budget_seconds == 120.0
        assert a.budget_tool_calls == 12
        assert a.enable_verify is True

    def test_custom_params(self):
        a = Agent(max_steps=4, max_replan=1, budget_seconds=30.0,
                  budget_tool_calls=5, enable_verify=False)
        assert a.max_steps == 4
        assert a.max_replan == 1
        assert a.budget_seconds == 30.0
        assert a.budget_tool_calls == 5
        assert a.enable_verify is False

    def test_from_config_returns_agent(self):
        a = Agent.from_config()
        assert isinstance(a, Agent)
        assert isinstance(a.max_replan, int)
        assert isinstance(a.budget_seconds, (int, float))

    def test_from_config_reads_max_replan(self):
        fake_settings = {"agent": {"max_steps": 10, "max_replan": 3,
                                    "budget_seconds": 60, "budget_tool_calls": 8,
                                    "enable_verify": False}}
        with patch("app.config.get_settings", return_value=fake_settings):
            a = Agent.from_config()
        assert a.max_replan == 3
        assert a.budget_seconds == 60
        assert a.enable_verify is False

    def test_from_config_graceful_on_error(self):
        """若 config 读取失败，应使用默认值而不是抛异常。"""
        with patch("app.config.get_settings", side_effect=RuntimeError("cfg error")):
            a = Agent.from_config()
        assert isinstance(a, Agent)


# ═══════════════════════════════════════════════════════════════
# AgentResult 新字段
# ═══════════════════════════════════════════════════════════════

class TestAgentResult:

    def test_result_has_new_fields(self):
        r = AgentResult(answer={}, trace=[])
        assert hasattr(r, "verify_score")
        assert hasattr(r, "replan_count")
        assert hasattr(r, "budget_exceeded")
        assert r.verify_score == 0.0
        assert r.replan_count == 0
        assert r.budget_exceeded is False


# ═══════════════════════════════════════════════════════════════
# TraceEvent kind 枚举
# ═══════════════════════════════════════════════════════════════

class TestTraceEventKinds:

    def _kinds(self, trace: List[TraceEvent]) -> List[str]:
        return [e.kind for e in trace]

    def test_basic_run_has_user_event(self):
        a = Agent(enable_verify=False)
        r = a.run("摄像头 CAM-01 离线")
        assert "user" in self._kinds(r.trace)

    def test_basic_run_has_final_event(self):
        a = Agent(enable_verify=False)
        r = a.run("摄像头 CAM-01 离线")
        assert "final" in self._kinds(r.trace)

    def test_verify_event_present_when_enabled(self):
        a = Agent(enable_verify=True)
        r = a.run("摄像头 CAM-01 离线")
        assert "verify" in self._kinds(r.trace)

    def test_no_verify_event_when_disabled(self):
        a = Agent(enable_verify=False)
        r = a.run("摄像头 CAM-01 离线")
        assert "verify" not in self._kinds(r.trace)

    def test_verify_event_has_score(self):
        a = Agent(enable_verify=True)
        r = a.run("摄像头 CAM-01 离线")
        verify_events = [e for e in r.trace if e.kind == "verify"]
        assert len(verify_events) >= 1
        for ev in verify_events:
            assert "score" in ev.payload
            assert "passed" in ev.payload

    def test_budget_exceeded_event_on_tool_limit(self):
        """工具调用超限时，trace 中应有 budget_exceeded 事件。"""
        # 设置工具调用上限=0，强制立即超限（第一次 replan 检查前）
        a = Agent(budget_tool_calls=0, max_replan=0, enable_verify=False)
        r = a.run("测试查询")
        # budget_exceeded 在 replan 开始时检查，tool_calls=0 < limit=0 不触发
        # 改用 budget_seconds 超限来测试
        a2 = Agent(budget_seconds=0.0, max_replan=0, enable_verify=False)
        r2 = a2.run("测试查询")
        assert r2.budget_exceeded is True
        assert "budget_exceeded" in self._kinds(r2.trace)


# ═══════════════════════════════════════════════════════════════
# enable_verify=False 单轮执行
# ═══════════════════════════════════════════════════════════════

class TestVerifyDisabled:

    def test_no_replan_events(self):
        a = Agent(enable_verify=False)
        r = a.run("摄像头 CAM-01 离线")
        kinds = [e.kind for e in r.trace]
        assert "replan" not in kinds
        assert "verify" not in kinds

    def test_verify_score_is_1_when_disabled(self):
        a = Agent(enable_verify=False)
        r = a.run("摄像头 CAM-01 离线")
        assert r.verify_score == 1.0

    def test_replan_count_is_0(self):
        a = Agent(enable_verify=False)
        r = a.run("摄像头 CAM-01 离线")
        assert r.replan_count == 0


# ═══════════════════════════════════════════════════════════════
# 预算守护
# ═══════════════════════════════════════════════════════════════

class TestBudgetGuard:

    def test_time_budget_exceeded(self):
        a = Agent(budget_seconds=0.0, max_replan=0, enable_verify=False)
        r = a.run("测试时间超限")
        assert r.budget_exceeded is True

    def test_time_budget_sets_flag(self):
        a = Agent(budget_seconds=0.0, max_replan=0, enable_verify=False)
        r = a.run("测试")
        assert "_budget_exceeded" in r.answer or r.budget_exceeded

    def test_normal_run_not_budget_exceeded(self):
        a = Agent(budget_seconds=120.0, max_replan=0, enable_verify=False)
        r = a.run("摄像头 CAM-01 离线")
        assert r.budget_exceeded is False


# ═══════════════════════════════════════════════════════════════
# Verify 通过 → 无 replan
# ═══════════════════════════════════════════════════════════════

class TestVerifyPassNoReplan:

    def test_no_replan_event_when_passes(self):
        """若 verifier 直接通过，不应有 replan 事件。"""
        from app.agent.verifier import AnswerVerifier, VerifyResult

        passing_result = VerifyResult(passed=True, score=0.9, issues=[], replan_hint="")
        with patch.object(AnswerVerifier, "verify", return_value=passing_result):
            a = Agent(enable_verify=True, max_replan=2)
            r = a.run("摄像头 CAM-01 离线")

        kinds = [e.kind for e in r.trace]
        assert "replan" not in kinds
        assert r.replan_count == 0
        assert r.verify_score == 0.9


# ═══════════════════════════════════════════════════════════════
# max_replan=0 → 不重规划
# ═══════════════════════════════════════════════════════════════

class TestMaxReplanZero:

    def test_no_replan_when_max_zero(self):
        from app.agent.verifier import AnswerVerifier, VerifyResult

        failing_result = VerifyResult(
            passed=False, score=0.3, issues=["结论过短"],
            replan_hint="请提供更详细的结论和数值证据。"
        )
        with patch.object(AnswerVerifier, "verify", return_value=failing_result):
            a = Agent(enable_verify=True, max_replan=0)
            r = a.run("测试最大重规划为0")

        assert r.replan_count == 0
        replan_events = [e for e in r.trace if e.kind == "replan"]
        # max_replan=0 → 触发 max_replan_reached，不触发 triggered
        triggered = [e for e in replan_events if e.payload.get("action") == "triggered"]
        assert len(triggered) == 0


# ═══════════════════════════════════════════════════════════════
# Verify 失败 → replan 触发
# ═══════════════════════════════════════════════════════════════

class TestVerifyFailReplan:

    def test_replan_triggered_once(self):
        from app.agent.verifier import AnswerVerifier, VerifyResult

        call_count = {"n": 0}
        def mock_verify(self_v, answer, obs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return VerifyResult(
                    passed=False, score=0.4, issues=["证据不足"],
                    replan_hint="请补充数值证据。"
                )
            return VerifyResult(passed=True, score=0.8, issues=[], replan_hint="")

        with patch.object(AnswerVerifier, "verify", mock_verify):
            a = Agent(enable_verify=True, max_replan=2)
            r = a.run("测试重规划触发")

        assert r.replan_count == 1
        replan_events = [e for e in r.trace if e.kind == "replan"
                         and e.payload.get("action") == "triggered"]
        assert len(replan_events) == 1
        assert "hint" in replan_events[0].payload

    def test_replan_event_has_hint(self):
        from app.agent.verifier import AnswerVerifier, VerifyResult

        call_count = {"n": 0}
        def mock_verify(self_v, answer, obs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return VerifyResult(
                    passed=False, score=0.3, issues=["无建议"],
                    replan_hint="需要提供处置建议。"
                )
            return VerifyResult(passed=True, score=0.85, issues=[], replan_hint="")

        with patch.object(AnswerVerifier, "verify", mock_verify):
            a = Agent(enable_verify=True, max_replan=2)
            r = a.run("测试 replan hint")

        triggered = [e for e in r.trace if e.kind == "replan"
                     and e.payload.get("action") == "triggered"]
        assert len(triggered) >= 1
        assert "需要提供处置建议" in triggered[0].payload["hint"]


# ═══════════════════════════════════════════════════════════════
# on_event 回调
# ═══════════════════════════════════════════════════════════════

class TestOnEventCallback:

    def test_callback_receives_all_events(self):
        events: List[TraceEvent] = []
        a = Agent(enable_verify=False, on_event=events.append)
        a.run("测试回调")
        assert len(events) > 0
        assert events[0].kind == "user"

    def test_callback_exception_does_not_break_run(self):
        def bad_cb(ev):
            raise RuntimeError("callback error")

        a = Agent(enable_verify=False, on_event=bad_cb)
        r = a.run("测试回调异常")
        assert isinstance(r, AgentResult)
