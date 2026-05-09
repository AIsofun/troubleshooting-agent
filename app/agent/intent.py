"""
Intent Registry — 从 config 加载的问题类型定义。

每个 IntentDef 描述：
  - name          : intent 标识符（作为 LLM 输出枚举值）
  - keywords      : 中英文关键词正则，用于 MockLLM / 快速意图识别
  - plan_steps    : 有序工具步骤列表 (tool, default_args)
  - description   : 给 LLM system prompt 使用的描述文字

设计原则：
  - 新增业务场景只需在 config/base.yaml 的 intents 节点添加记录，
    不修改任何 Python 文件。
  - 所有 OllamaLLM / MockLLM 都通过 get_intent_registry() 读取定义。
  - 支持热更新：测试可通过替换 _REGISTRY 全局对象实现注入。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class PlanStep:
    tool: str
    args: Dict[str, Any] = field(default_factory=dict)
    # 如果某个参数需要从用户 query 中动态提取，列在这里
    # 例: extract={"camera_id": "cam_id_extractor"}
    extract: Dict[str, str] = field(default_factory=dict)


@dataclass
class IntentDef:
    name: str
    description: str
    keywords: str              # Python 正则，用于 MockLLM intent 识别
    plan_steps: List[PlanStep]
    # 参数提取提示：给 LLM system prompt 注入的参数→服务名对照
    param_hints: str = ""


class IntentRegistry:
    """
    运行时意图注册表。
    支持从 config 批量导入，也支持代码注册（用于测试和扩展）。
    """

    def __init__(self) -> None:
        self._intents: Dict[str, IntentDef] = {}

    def register(self, defn: IntentDef) -> None:
        self._intents[defn.name] = defn

    def get(self, name: str) -> Optional[IntentDef]:
        return self._intents.get(name)

    def all(self) -> List[IntentDef]:
        return list(self._intents.values())

    def names(self) -> List[str]:
        return list(self._intents.keys())

    def match(self, query: str) -> Optional[IntentDef]:
        """返回第一个关键词命中的 IntentDef，无匹配返回 None。"""
        ql = query.lower()
        for defn in self._intents.values():
            if re.search(defn.keywords, ql):
                return defn
        return None

    # ── 工厂方法：从 config dict 批量构建 ─────────────────

    @classmethod
    def from_config(cls, intent_configs: List[Dict[str, Any]]) -> "IntentRegistry":
        """
        从 config/base.yaml 的 intents 节点构建注册表。

        yaml 格式示例：
          intents:
            - name: camera_offline
              description: "相机/视频流异常"
              keywords: "(相机|camera|cam-\\d+|掉线|无图像)"
              param_hints: "get_camera_status(camera_id=用户提到的相机, 默认 cam-02)"
              plan_steps:
                - tool: get_camera_status
                  args: {camera_id: cam-02}
                  extract: {camera_id: cam_id}
                - tool: get_recent_logs
                  args: {service_name: camera-service, limit: 5}
                - tool: query_runbook
                  args: {issue_type: camera_offline}
        """
        registry = cls()
        for cfg in (intent_configs or []):
            steps = []
            for s in cfg.get("plan_steps", []):
                steps.append(PlanStep(
                    tool=s["tool"],
                    args=s.get("args", {}),
                    extract=s.get("extract", {}),
                ))
            registry.register(IntentDef(
                name=cfg["name"],
                description=cfg.get("description", cfg["name"]),
                keywords=cfg.get("keywords", cfg["name"]),
                plan_steps=steps,
                param_hints=cfg.get("param_hints", ""),
            ))
        return registry

    # ── 内置默认注册表（不依赖 config 的降级方案）──────────

    @classmethod
    def default(cls) -> "IntentRegistry":
        """
        内置默认注册表。
        当 config 中无 intents 节点时使用，保持向后兼容。
        """
        r = cls()
        _BUILTIN = [
            {
                "name": "camera_offline",
                "description": "相机/视频流异常（掉线、无图像、帧率下降）",
                "keywords": r"(相机|camera|cam-\d+|掉线|没有图像|无图像)",
                "param_hints": "get_camera_status(camera_id=用户提到的相机, 默认 cam-02)\n"
                               "get_recent_logs(service_name='camera-service', limit=5)\n"
                               "query_runbook(issue_type='camera_offline')",
                "plan_steps": [
                    {"tool": "get_camera_status",
                     "args": {"camera_id": "cam-02"}, "extract": {"camera_id": "cam_id"}},
                    {"tool": "get_recent_logs",
                     "args": {"service_name": "camera-service", "limit": 5}},
                    {"tool": "query_runbook",
                     "args": {"issue_type": "camera_offline"}},
                ],
            },
            {
                "name": "ocr_quality_drop",
                "description": "OCR / 识别质量下降（成功率、准确率下降）",
                "keywords": r"(ocr|识别|成功率|准确率)",
                "param_hints": "get_model_metrics(model_name='ocr-v3')\n"
                               "get_recent_logs(service_name='ocr-service', limit=5)\n"
                               "query_runbook(issue_type='ocr_quality_drop')",
                "plan_steps": [
                    {"tool": "get_model_metrics", "args": {"model_name": "ocr-v3"}},
                    {"tool": "get_recent_logs",
                     "args": {"service_name": "ocr-service", "limit": 5}},
                    {"tool": "query_runbook",
                     "args": {"issue_type": "ocr_quality_drop"}},
                ],
            },
            {
                "name": "kafka_backlog",
                "description": "Kafka 消息堆积（lag 升高、消费能力不足）",
                "keywords": r"(kafka|堆积|lag|消费)",
                "param_hints": "get_kafka_backlog(topic='vision.events')\n"
                               "get_recent_logs(service_name='kafka-consumer', limit=5)\n"
                               "query_runbook(issue_type='kafka_backlog')",
                "plan_steps": [
                    {"tool": "get_kafka_backlog", "args": {"topic": "vision.events"}},
                    {"tool": "get_recent_logs",
                     "args": {"service_name": "kafka-consumer", "limit": 5}},
                    {"tool": "query_runbook",
                     "args": {"issue_type": "kafka_backlog"}},
                ],
            },
            {
                "name": "inference_latency_high",
                "description": "推理服务延迟升高（p99 超标、GPU 饱和）",
                "keywords": r"(推理|inference|延迟|latency|p99|慢)",
                "param_hints": "get_model_metrics(model_name='inference-gw')\n"
                               "get_recent_logs(service_name='inference-gateway', limit=5)\n"
                               "query_runbook(issue_type='inference_latency_high')",
                "plan_steps": [
                    {"tool": "get_model_metrics", "args": {"model_name": "inference-gw"}},
                    {"tool": "get_recent_logs",
                     "args": {"service_name": "inference-gateway", "limit": 5}},
                    {"tool": "query_runbook",
                     "args": {"issue_type": "inference_latency_high"}},
                ],
            },
            {
                "name": "algorithm_false_reject",
                "description": "算法误杀率升高（false reject / 过杀）",
                "keywords": r"(误杀|false.?reject|过杀|误报|alg_false)",
                "param_hints": "get_model_metrics(model_name='defect-cls')\n"
                               "get_recent_logs(service_name='algorithm-service', limit=5)\n"
                               "search_knowledge(query=用户问题, alarm_code=报警码)\n"
                               "query_runbook(issue_type='algorithm_false_reject')",
                "plan_steps": [
                    {"tool": "get_model_metrics", "args": {"model_name": "defect-cls"}},
                    {"tool": "get_recent_logs",
                     "args": {"service_name": "algorithm-service", "limit": 5}},
                    {"tool": "search_knowledge",
                     "args": {"query": "算法误杀率升高", "top_k": 5},
                     "extract": {"query": "user_query", "alarm_code": "alarm_code"}},
                    {"tool": "query_runbook",
                     "args": {"issue_type": "algorithm_false_reject"}},
                ],
            },
        ]
        return cls.from_config(_BUILTIN)


# ── 全局单例 ─────────────────────────────────────────────────
# 启动时由 config.py 调用 _init_registry() 初始化，此后只读。
_REGISTRY: Optional[IntentRegistry] = None


def _init_registry(intent_configs: Optional[List[Dict]] = None) -> IntentRegistry:
    """初始化全局 intent 注册表（仅调用一次）。"""
    global _REGISTRY
    if intent_configs:
        _REGISTRY = IntentRegistry.from_config(intent_configs)
    else:
        _REGISTRY = IntentRegistry.default()
    return _REGISTRY


def get_intent_registry() -> IntentRegistry:
    """获取全局 intent 注册表（延迟初始化）。"""
    global _REGISTRY
    if _REGISTRY is None:
        try:
            from app.config import get_settings
            settings = get_settings()
            intent_configs = settings.get("intents")
            _REGISTRY = _init_registry(intent_configs)
        except Exception:
            _REGISTRY = IntentRegistry.default()
    return _REGISTRY
