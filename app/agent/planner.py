"""
通用 ReAct Planner — 将 IntentRegistry 渲染成 LLM system prompt。

职责：
  1. 渲染 system_prompt   — 覆盖工具说明 + intent 枚举 + 参数对照 + 强制取证要求
  2. 渲染 reminder_msg    — 当取证不足时附加的提醒消息
  3. 提取 camera_id 等动态参数（供 MockLLM 使用）

设计原则：
  - Planner 不依赖任何具体 intent / 工具名，完全由 IntentRegistry 驱动。
  - OllamaLLM / MockLLM 各自持有一个 Planner 实例，调用时机不同。
  - 测试可直接传入自定义 registry，不需要 mock config。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Set

from app.agent.intent import IntentDef, IntentRegistry, get_intent_registry


class ReactPlanner:
    """
    通用 ReAct prompt 渲染器。

    用法：
        planner = ReactPlanner()                   # 使用全局 registry
        planner = ReactPlanner(registry=custom_r)  # 注入自定义 registry（测试用）

        system_prompt = planner.system_prompt()
        reminder = planner.reminder_msg(called_tools={"get_camera_status"})
    """

    # 必须覆盖的取证类别 → 工具集合
    # 每个类别只要有一个工具被调用即视为"已取证"
    _REQUIRED_CATEGORIES: Dict[str, Set[str]] = {
        "status_or_metrics": {
            "get_camera_status", "get_model_metrics",
            "get_kafka_backlog", "get_device_heartbeat",
        },
        "logs":    {"get_recent_logs"},
        "runbook": {"query_runbook"},
    }

    def __init__(self, registry: Optional[IntentRegistry] = None) -> None:
        self._registry = registry  # None → lazy-load global

    @property
    def registry(self) -> IntentRegistry:
        if self._registry is None:
            self._registry = get_intent_registry()
        return self._registry

    # ── System Prompt ────────────────────────────────────────

    def system_prompt(self, extra_context: str = "") -> str:
        """
        渲染完整的 system prompt。
        extra_context: 可注入当前检索到的知识摘要（P3 集成后使用）。
        """
        lines: List[str] = [
            "你是一个工业生产系统的【异常排查 Agent】。",
            "你必须严格遵循 ReAct 风格的 Plan→Act→Observe 循环：",
            "每一轮只调用一个工具，拿到结果后再决定下一步，直到收集到足够证据，才给出最终结论。",
            "",
            "【核心准则】禁止在没有调用足够工具的情况下直接下结论。",
            "",
            "【强制取证流程】对每一类问题，必须依次完成下面三类取证，缺一不可：",
            "  1) 现状指标：调用对应的 status/metrics/backlog 工具拿到当前数值",
            "  2) 日志佐证：调用 get_recent_logs 获取相关服务的最近日志，作为根因判断依据",
            "  3) 处置依据：调用 query_runbook 获取该问题类型对应的标准处置流程",
            "  4) 知识检索（可选）：若已有报警码或设备型号，可调用 search_knowledge 精确命中历史案例",
            "只有当上述取证类别都已被调用，才能给出最终回答。",
            "",
            "【问题类型 intent 枚举】（必须使用其中之一，不要自创）：",
        ]

        for defn in self.registry.all():
            lines.append(f"  - {defn.name:<35} （{defn.description}）")

        lines += [
            "",
            "【工具→参数 对照表】（避免猜错参数）：",
        ]
        for defn in self.registry.all():
            if defn.param_hints:
                lines.append(f"  {defn.name}:")
                for hint_line in defn.param_hints.strip().splitlines():
                    lines.append(f"    {hint_line.strip()}")

        if extra_context:
            lines += [
                "",
                "【参考知识】（来自知识库的检索结果，可作为诊断参考，不能直接作为结论）：",
                extra_context,
            ]

        lines += [
            "",
            "【最终回答输出规范】当三类工具都已调用完毕，请直接输出一个合法 JSON 对象",
            "（不要附加任何说明文字、不要用 Markdown 代码块包裹），结构如下：",
            "{",
            '  "intent": "上述枚举之一",',
            '  "conclusion": "用中文给出诊断结论，必须引用工具返回的具体数值/日志关键词",',
            '  "evidence": ["每条形如：工具名: 摘要", ...],',
            '  "suggestions": ["来自 query_runbook 的步骤，逐条列出"],',
            '  "safe_actions": ["来自 runbook 的 safe_actions 字段；若无则空数组"]',
            "}",
            "全部内容必须使用中文。",
        ]

        return "\n".join(lines)

    # ── Reminder Message ─────────────────────────────────────

    def reminder_msg(self, called_tools: Set[str]) -> Optional[str]:
        """
        当检测到取证不足时，返回应追加给 LLM 的提醒消息。
        取证充足时返回 None。
        """
        missing = [
            cat for cat, tools in self._REQUIRED_CATEGORIES.items()
            if not (called_tools & tools)
        ]
        if not missing:
            return None
        return (
            f"提醒：你还没有完成必要的取证步骤，缺少 {missing}。"
            "请继续调用对应工具，不要急于给出最终答案。"
        )

    # ── 动态参数提取 ─────────────────────────────────────────

    @staticmethod
    def extract_camera_id(query: str) -> str:
        """从用户 query 中提取相机 ID，用于 MockLLM 参数填充。"""
        m = re.search(r"cam-?(\d+)", query, re.I)
        if m:
            return f"cam-{int(m.group(1)):02d}"
        m = re.search(r"(\d+)\s*号\s*相机", query)
        if m:
            return f"cam-{int(m.group(1)):02d}"
        return "cam-02"

    @staticmethod
    def extract_alarm_code(query: str) -> Optional[str]:
        """从 query 中提取报警码（大写下划线格式）。"""
        m = re.search(r"[A-Z]{2,}(?:_[A-Z0-9]+){1,}", query)
        return m.group(0) if m else None

    def resolve_step_args(
        self, step_args: Dict[str, Any], extract_hints: Dict[str, str], query: str
    ) -> Dict[str, Any]:
        """
        根据 extract 字段把动态参数从 query 中填充进 step_args。

        extract_hints 例:
          {"camera_id": "cam_id", "alarm_code": "alarm_code"}
        """
        resolved = dict(step_args)
        extractors = {
            "cam_id":    lambda q: self.extract_camera_id(q),
            "alarm_code": lambda q: self.extract_alarm_code(q) or "",
            "user_query": lambda q: q[:100],
        }
        for param, extractor_name in extract_hints.items():
            fn = extractors.get(extractor_name)
            if fn:
                val = fn(query)
                if val:
                    resolved[param] = val
        return resolved
