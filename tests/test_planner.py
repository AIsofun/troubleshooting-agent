"""
Phase 5 — 通用规划器测试。

覆盖：
  - IntentRegistry.from_config()   从 dict 构建
  - IntentRegistry.match()         关键词匹配
  - IntentRegistry.default()       内置默认注册表
  - ReactPlanner.system_prompt()   包含全部 intent / 参数对照
  - ReactPlanner.reminder_msg()    缺少取证时返回提示，齐全时返回 None
  - ReactPlanner.extract_camera_id / extract_alarm_code
  - ReactPlanner.resolve_step_args
  - MockLLM（注入 custom registry）
      - 匹配到 intent → plan_step 序列正确
      - 未知 intent → final + 列出已知 intent
      - 动态参数提取 cam_id / alarm_code
      - 全步骤完成 → action=final
  - config/base.yaml intents 节点可被正确解析
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import patch

import pytest

from app.agent.intent import IntentDef, IntentRegistry, PlanStep
from app.agent.planner import ReactPlanner


# ── Fixtures ─────────────────────────────────────────────────


def _make_registry() -> IntentRegistry:
    """构建包含两个 intent 的测试注册表。"""
    r = IntentRegistry()
    r.register(IntentDef(
        name="camera_offline",
        description="相机掉线",
        keywords=r"(相机|camera|cam-\d+|掉线)",
        plan_steps=[
            PlanStep("get_camera_status", {"camera_id": "cam-02"}, {"camera_id": "cam_id"}),
            PlanStep("get_recent_logs", {"service_name": "camera-service", "limit": 5}),
            PlanStep("query_runbook", {"issue_type": "camera_offline"}),
        ],
        param_hints="get_camera_status(camera_id=用户提到的相机)\nget_recent_logs(service_name='camera-service')",
    ))
    r.register(IntentDef(
        name="kafka_backlog",
        description="Kafka 堆积",
        keywords=r"(kafka|堆积|lag)",
        plan_steps=[
            PlanStep("get_kafka_backlog", {"topic": "vision.events"}),
            PlanStep("get_recent_logs", {"service_name": "kafka-consumer", "limit": 5}),
            PlanStep("query_runbook", {"issue_type": "kafka_backlog"}),
        ],
        param_hints="get_kafka_backlog(topic='vision.events')",
    ))
    return r


# ── IntentRegistry ────────────────────────────────────────────


def test_registry_match_camera():
    r = _make_registry()
    defn = r.match("cam-03 号相机掉线了")
    assert defn is not None
    assert defn.name == "camera_offline"


def test_registry_match_kafka():
    r = _make_registry()
    defn = r.match("kafka 消费 lag 突然升高")
    assert defn is not None
    assert defn.name == "kafka_backlog"


def test_registry_match_unknown():
    r = _make_registry()
    assert r.match("今天天气怎么样") is None


def test_registry_names():
    r = _make_registry()
    assert set(r.names()) == {"camera_offline", "kafka_backlog"}


def test_registry_from_config():
    configs = [
        {
            "name": "test_intent",
            "description": "测试意图",
            "keywords": "(测试|test)",
            "param_hints": "tool_a(param=value)",
            "plan_steps": [
                {"tool": "tool_a", "args": {"x": 1}},
                {"tool": "tool_b", "args": {}, "extract": {"y": "cam_id"}},
            ],
        }
    ]
    r = IntentRegistry.from_config(configs)
    assert r.get("test_intent") is not None
    defn = r.get("test_intent")
    assert len(defn.plan_steps) == 2
    assert defn.plan_steps[1].extract == {"y": "cam_id"}


def test_registry_default_has_builtin_intents():
    r = IntentRegistry.default()
    names = r.names()
    assert "camera_offline" in names
    assert "ocr_quality_drop" in names
    assert "kafka_backlog" in names
    assert "inference_latency_high" in names
    assert "algorithm_false_reject" in names


# ── ReactPlanner.system_prompt ────────────────────────────────


def test_system_prompt_contains_all_intents():
    r = _make_registry()
    planner = ReactPlanner(registry=r)
    prompt = planner.system_prompt()

    assert "camera_offline" in prompt
    assert "kafka_backlog" in prompt
    assert "get_camera_status" in prompt
    assert "get_kafka_backlog" in prompt


def test_system_prompt_contains_json_spec():
    r = _make_registry()
    planner = ReactPlanner(registry=r)
    prompt = planner.system_prompt()

    assert '"intent"' in prompt
    assert '"conclusion"' in prompt
    assert '"suggestions"' in prompt
    assert '"safe_actions"' in prompt


def test_system_prompt_extra_context_injected():
    r = _make_registry()
    planner = ReactPlanner(registry=r)
    prompt = planner.system_prompt(extra_context="【参考案例】曝光时间过长导致误杀")
    assert "参考案例" in prompt
    assert "曝光时间过长" in prompt


# ── ReactPlanner.reminder_msg ─────────────────────────────────


def test_reminder_msg_missing_all():
    planner = ReactPlanner(registry=_make_registry())
    msg = planner.reminder_msg(called_tools=set())
    assert msg is not None
    assert "取证" in msg


def test_reminder_msg_missing_runbook_only():
    planner = ReactPlanner(registry=_make_registry())
    msg = planner.reminder_msg(called_tools={"get_camera_status", "get_recent_logs"})
    assert msg is not None
    assert "runbook" in msg


def test_reminder_msg_all_present():
    planner = ReactPlanner(registry=_make_registry())
    # 覆盖三类取证
    msg = planner.reminder_msg(called_tools={
        "get_camera_status",
        "get_recent_logs",
        "query_runbook",
    })
    assert msg is None


# ── ReactPlanner dynamic extraction ──────────────────────────


def test_extract_camera_id_from_hyphen():
    assert ReactPlanner.extract_camera_id("cam-07 掉线") == "cam-07"


def test_extract_camera_id_chinese():
    assert ReactPlanner.extract_camera_id("3 号相机没有图像") == "cam-03"


def test_extract_camera_id_default():
    assert ReactPlanner.extract_camera_id("相机掉线了") == "cam-02"


def test_extract_alarm_code_found():
    code = ReactPlanner.extract_alarm_code("报警 ALG_FALSE_REJECT_HIGH 持续触发")
    assert code == "ALG_FALSE_REJECT_HIGH"


def test_extract_alarm_code_not_found():
    assert ReactPlanner.extract_alarm_code("相机掉线") is None


def test_resolve_step_args_cam_id():
    planner = ReactPlanner(registry=_make_registry())
    base = {"camera_id": "cam-02"}
    extract = {"camera_id": "cam_id"}
    resolved = planner.resolve_step_args(base, extract, "cam-05 掉线了")
    assert resolved["camera_id"] == "cam-05"


def test_resolve_step_args_no_extract():
    planner = ReactPlanner(registry=_make_registry())
    base = {"topic": "vision.events"}
    resolved = planner.resolve_step_args(base, {}, "kafka 堆积")
    assert resolved == {"topic": "vision.events"}


# ── MockLLM with injected registry ────────────────────────────


def _make_mock_llm(registry=None):
    from app.agent.llm import MockLLM
    return MockLLM(registry=registry or _make_registry())


def test_mock_llm_first_step_camera():
    llm = _make_mock_llm()
    decision = llm.plan("相机掉线", "", [])
    assert decision["action"] == "tool_call"
    assert decision["tool"] == "get_camera_status"


def test_mock_llm_camera_dynamic_cam_id():
    llm = _make_mock_llm()
    decision = llm.plan("cam-07 相机掉线了", "", [])
    assert decision["tool"] == "get_camera_status"
    assert decision["args"]["camera_id"] == "cam-07"


def test_mock_llm_second_step_after_first_done():
    llm = _make_mock_llm()
    obs = [{"tool": "get_camera_status", "args": {}, "result": {"ok": True, "summary": "offline"}}]
    decision = llm.plan("相机掉线", "", obs)
    assert decision["tool"] == "get_recent_logs"


def test_mock_llm_final_after_all_steps():
    llm = _make_mock_llm()
    obs = [
        {"tool": "get_camera_status", "args": {}, "result": {"ok": True, "summary": "offline"}},
        {"tool": "get_recent_logs", "args": {}, "result": {"ok": True, "summary": "RTSP失败"}},
        {"tool": "query_runbook", "args": {}, "result": {"ok": True, "summary": "查到runbook",
                                                          "data": {"steps": ["步骤1"], "safe_actions": []}}},
    ]
    decision = llm.plan("相机掉线", "", obs)
    assert decision["action"] == "final"
    assert "answer" in decision


def test_mock_llm_unknown_intent():
    llm = _make_mock_llm()
    decision = llm.plan("今天天气怎么样", "", [])
    assert decision["action"] == "final"
    assert "unknown" in decision["answer"]["intent"]
    # 应包含已知 intent 提示
    assert "camera_offline" in decision["answer"]["conclusion"]


def test_mock_llm_kafka_intent():
    llm = _make_mock_llm()
    decision = llm.plan("kafka 堆积很严重", "", [])
    assert decision["action"] == "tool_call"
    assert decision["tool"] == "get_kafka_backlog"


# ── config/base.yaml intents integration ─────────────────────


def test_config_intents_loaded():
    """config/base.yaml 的 intents 节点应被正确解析为 IntentRegistry。"""
    from app.config import get_settings
    get_settings.cache_clear()
    settings = get_settings()
    intent_configs = settings.get("intents")
    assert intent_configs is not None, "base.yaml 中缺少 intents 节点"
    assert len(intent_configs) >= 4

    names = [c["name"] for c in intent_configs]
    assert "camera_offline" in names
    assert "algorithm_false_reject" in names


def test_global_registry_from_config():
    """全局 intent registry 应从 config 加载（而非只用内置默认值）。"""
    import app.agent.intent as _intent_mod
    # 重置全局单例
    _intent_mod._REGISTRY = None

    with patch("app.config.get_settings") as mock_settings:
        mock_settings.return_value = {
            "intents": [
                {
                    "name": "custom_intent",
                    "description": "自定义意图",
                    "keywords": "(自定义|custom)",
                    "plan_steps": [{"tool": "get_recent_logs", "args": {}}],
                }
            ]
        }
        from app.agent.intent import get_intent_registry
        # 重置让 lazy-init 触发
        _intent_mod._REGISTRY = None
        registry = get_intent_registry()

    assert registry.get("custom_intent") is not None
    # 还原
    _intent_mod._REGISTRY = None


# ── Full Agent run with custom registry ──────────────────────


def test_agent_run_with_mock_llm_camera():
    """端对端：Agent 使用 MockLLM + 注入的 registry 完整运行相机掉线场景。"""
    from app.agent.core import Agent
    from app.agent.llm import MockLLM

    llm = MockLLM(registry=_make_registry())
    agent = Agent(llm=llm, max_steps=8)
    result = agent.run("相机 cam-03 掉线了，请排查")

    assert result.answer is not None
    # 应完成全部三步工具调用
    tool_calls = [e for e in result.trace if e.kind == "tool_call"]
    tools_used = {e.payload["tool"] for e in tool_calls}
    assert "get_camera_status" in tools_used
    assert "get_recent_logs" in tools_used
    assert "query_runbook" in tools_used
    # 相机 ID 应被正确提取
    cam_call = next(e for e in tool_calls if e.payload["tool"] == "get_camera_status")
    assert cam_call.payload["args"]["camera_id"] == "cam-03"
