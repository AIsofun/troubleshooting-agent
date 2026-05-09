"""
Tests for the Agent core loop using MockLLM (no external dependencies).
"""
import pytest

from app.agent.core import Agent, AgentResult
from app.agent.llm import MockLLM


def _make_agent() -> Agent:
    return Agent(llm=MockLLM(), max_steps=8)


def test_camera_offline_completes():
    agent = _make_agent()
    result = agent.run("2号相机掉线了，最近10分钟没有图像")
    assert isinstance(result, AgentResult)
    assert result.answer.get("intent") == "camera_offline"
    assert len(result.trace) > 0


def test_ocr_quality_drop_completes():
    agent = _make_agent()
    result = agent.run("OCR识别成功率突然下降")
    assert result.answer.get("intent") == "ocr_quality_drop"


def test_kafka_backlog_completes():
    agent = _make_agent()
    result = agent.run("Kafka消费堆积报警很多")
    assert result.answer.get("intent") == "kafka_backlog"


def test_inference_latency_completes():
    agent = _make_agent()
    result = agent.run("推理服务延迟很高，p99超过阈值")
    assert result.answer.get("intent") == "inference_latency_high"


def test_unknown_intent_returns_final():
    agent = _make_agent()
    result = agent.run("今天天气怎么样")
    # MockLLM returns final immediately for unknown intent
    assert "intent" in result.answer


def test_trace_records_tool_calls():
    agent = _make_agent()
    result = agent.run("2号相机掉线了")
    tool_events = [e for e in result.trace if e.kind == "tool_call"]
    assert len(tool_events) >= 1


def test_max_steps_respected():
    agent = Agent(llm=MockLLM(), max_steps=1, max_replan=0, enable_verify=False)
    result = agent.run("2号相机掉线了")
    plan_events = [e for e in result.trace if e.kind == "plan"]
    # max_steps=1, max_replan=0: at most 1 plan→tool_call cycle
    assert len(plan_events) <= 2  # 1 tool call + possibly 1 final
