"""
LLM interface. The agent does not care whether it's a real LLM or a mock.

`plan(user_query, tools_desc, observations)` should return either:
  {"action": "tool_call", "tool": "...", "args": {...}, "thought": "short public reason"}
  {"action": "final",     "answer": {...}}

Swap `MockLLM` with a real implementation (OpenAI / Azure / local) by
implementing the same `plan` method.
"""
from __future__ import annotations
import json
import re
from typing import Any, Dict, List, Protocol


# ============================================================================
# 模型无关的通用解析工具
# ----------------------------------------------------------------------------
# 不同本地模型在 chat completion 的 content 字段里塞工具调用时，会用各自的
# 私有标记。如果针对每种模型写一段 if/elif，代码很快就会失控。
# 这里采用"剥离标签 + 括号配平扫描"的通用策略，使解析层不依赖任何具体模型方言。
# ============================================================================

# 涵盖以下常见格式（不区分大小写，不区分是否有 `|`、`/` 等修饰）:
#   <tool_call> ... </tool_call>           qwen2.5
#   <|tool_call|> ... <|/tool_call|>       一些 chatml 变体
#   <function_call> ...                    部分自研模型
#   <think> ... </think>                   含思考标签的模型
#   [TOOL_CALLS] ... [/TOOL_CALLS]         mistral 系
#   <|im_start|> / <|im_end|>              chatml 控制 token
_SPECIAL_TOKEN_RE = re.compile(
    r"<\s*\|?\s*/?\s*[A-Za-z_][\w\-]*\s*\|?\s*>"   # <tag> </tag> <|tag|> <|/tag|>
    r"|\[\s*/?[A-Z_][A-Z0-9_]*\s*\]",              # [TOOL_CALLS] [/TOOL_CALLS]
)


def _strip_special_tokens(s: str) -> str:
    """剥离任意 XML-like 标签和方括号特殊 token，返回清洗后的纯文本。"""
    if not s:
        return ""
    return _SPECIAL_TOKEN_RE.sub(" ", s).strip()


def _iter_json_objects(text: str):
    """
    在文本中按出现顺序扫描所有"括号配平"的 JSON 对象子串。

    比贪婪正则 `\\{.*\\}` 更可靠：
      - 能正确处理多个并列对象
      - 能正确跳过字符串字面量里的大括号
      - 不会把整段 markdown / 代码块吞掉
    """
    if not text:
        return
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start : i + 1]
                    start = -1


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


# ---------- Real LLM: Ollama / OpenAI-compatible ----------

def _build_tool_schemas() -> list:
    """
    把 TOOLS 注册表转换成 OpenAI JSON Schema 格式的工具列表。
    Ollama 和 OpenAI 都使用同一套格式。
    """
    # 延迟导入避免循环依赖
    from app.tools.registry import TOOLS

    # 参数类型映射（简单规则，生产中可改成 pydantic schema 自动生成）
    _type_map = {
        "string": "string",
        "int": "integer",
        "bool": "boolean",
    }

    schemas = []
    for name, meta in TOOLS.items():
        properties = {}
        required = []
        for param_name, param_desc in meta["parameters"].items():
            # param_desc 形如 "string, e.g. cam-01" 或 "int, default 5"
            raw_type = param_desc.split(",")[0].strip().lower()
            json_type = _type_map.get(raw_type, "string")
            properties[param_name] = {"type": json_type, "description": param_desc}
            # 没有 "default" 字样的参数视为必填
            if "default" not in param_desc:
                required.append(param_name)

        schemas.append({
            "type": "function",
            "function": {
                "name": name,
                "description": meta["description"],
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        })
    return schemas


class OllamaLLM:
    """
    接入 Ollama（或任何 OpenAI-compatible API）的真实 LLM 规划器。

    使用方法：
        llm = OllamaLLM(base_url="http://192.168.5.107:11434/v1", model="qwen2.5:14b")
        agent = Agent(llm=llm)

    对话轮次的消息结构（必须严格遵守）：
        round 0:  system + user
        round 1:  assistant(tool_calls=[...])
                  tool(tool_call_id=..., content=结果)
        round 2:  assistant(tool_calls=[...])
                  tool(tool_call_id=..., content=结果)
        ...
        final:    assistant(content=最终文字回答)
    """

    def __init__(
        self,
        base_url: str = "http://192.168.5.107:11434/v1",
        model: str = "qwen2.5:14b",
        api_key: str = "ollama",      # Ollama 不校验 key，随便填非空字符串即可
        temperature: float = 0.0,     # 0 = 确定性输出，适合工具调用场景
    ):
        try:
            from openai import OpenAI  # pip install openai
        except ImportError as e:
            raise ImportError("请先安装: pip install openai") from e

        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model = model
        self.temperature = temperature
        self.tool_schemas = _build_tool_schemas()

        # 系统提示：明确角色 + 强制工作流 + 输出格式约束
        self.system_prompt = (
            "你是一个工业生产系统的【异常排查 Agent】。\n"
            "你必须严格遵循 ReAct 风格的 Plan→Act→Observe 循环：每一轮只调用一个工具，"
            "拿到结果后再决定下一步，直到收集到足够证据，才给出最终结论。\n"
            "\n"
            "【核心准则】禁止在没有调用足够工具的情况下直接下结论。\n"
            "\n"
            "【强制取证流程】对每一类问题，必须依次完成下面三类取证，缺一不可：\n"
            "  1) 现状指标：调用对应的 status/metrics/backlog 工具拿到当前数值\n"
            "  2) 日志佐证：调用 get_recent_logs 获取相关服务的最近日志，作为根因判断依据\n"
            "  3) 处置依据：调用 query_runbook 获取该问题类型对应的标准处置流程\n"
            "只有当上述三类工具都已被调用、结果都已观察到，才能给出最终回答。\n"
            "\n"
            "【问题类型 intent 枚举】（必须使用其中之一，不要自创）：\n"
            "  - camera_offline           （相机/视频流异常）\n"
            "  - ocr_quality_drop         （OCR/识别质量下降）\n"
            "  - kafka_backlog            （Kafka 消息堆积）\n"
            "  - inference_latency_high   （推理服务延迟升高）\n"
            "\n"
            "【工具→服务名/参数 对照表】（避免你猜错参数）：\n"
            "  camera_offline:\n"
            "    get_camera_status(camera_id=用户提到的相机, 默认 cam-02)\n"
            "    get_recent_logs(service_name='camera-service', limit=5)\n"
            "    query_runbook(issue_type='camera_offline')\n"
            "  ocr_quality_drop:\n"
            "    get_model_metrics(model_name='ocr-v3')\n"
            "    get_recent_logs(service_name='ocr-service', limit=5)\n"
            "    query_runbook(issue_type='ocr_quality_drop')\n"
            "  kafka_backlog:\n"
            "    get_kafka_backlog(topic='vision.events')\n"
            "    get_recent_logs(service_name='kafka-consumer', limit=5)\n"
            "    query_runbook(issue_type='kafka_backlog')\n"
            "  inference_latency_high:\n"
            "    get_model_metrics(model_name='inference-gw')\n"
            "    get_recent_logs(service_name='inference-gateway', limit=5)\n"
            "    query_runbook(issue_type='inference_latency_high')\n"
            "\n"
            "【最终回答输出规范】当三类工具都已调用完毕，请直接输出一个合法 JSON 对象（不要附加任何说明文字、不要用 Markdown 代码块包裹），结构如下：\n"
            "{\n"
            '  "intent": "上述枚举之一",\n'
            '  "conclusion": "用中文给出一段诊断结论，必须引用工具返回的具体数值/日志关键词作为依据",\n'
            '  "evidence": ["每条形如：工具名: 摘要", ...],\n'
            '  "suggestions": ["来自 query_runbook 的步骤，逐条列出"],\n'
            '  "safe_actions": ["来自 runbook 的 safe_actions 字段，是可执行命令而非工具名；若无则空数组"]\n'
            "}\n"
            "全部内容必须使用中文。"
        )

    def _build_messages(
        self, user_query: str, observations: List[Dict[str, Any]]
    ) -> list:
        """
        把 agent observations 还原成符合 OpenAI 规范的多轮对话消息列表。

        每一轮工具调用的消息顺序必须是：
          assistant  (包含 tool_calls)
          tool       (包含 tool_call_id + content)

        observations 里没有保存 assistant 消息，所以这里用伪造的 tool_call_id 重建。
        """
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user",   "content": user_query},
        ]

        for i, obs in enumerate(observations):
            fake_call_id = f"call_{i}"   # 伪造 ID，Ollama 不校验具体值

            # ① 必须先有 assistant 消息，声明它调用了哪个工具
            messages.append({
                "role": "assistant",
                "content": None,          # 有 tool_calls 时 content 设为 None
                "tool_calls": [{
                    "id": fake_call_id,
                    "type": "function",
                    "function": {
                        "name": obs["tool"],
                        "arguments": json.dumps(obs["args"], ensure_ascii=False),
                    },
                }],
            })

            # ② 然后才是工具返回结果，tool_call_id 必须与上面的 id 匹配
            messages.append({
                "role": "tool",
                "tool_call_id": fake_call_id,   # 必填，与 assistant.tool_calls[].id 对应
                "content": obs["result"].get("summary", ""),
            })

        return messages

    def plan(
        self,
        user_query: str,
        tools_desc: str,                   # 这个参数保留是为了满足 LLM Protocol 接口
        observations: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        messages = self._build_messages(user_query, observations)

        # 软兜底：取证不足时，追加一条 user 提醒，强制模型继续调工具
        # （不是所有模型都会严格遵循 system prompt，再加一道防线）
        called = {o["tool"] for o in observations}
        required_categories = {
            "status_or_metrics": {"get_camera_status", "get_model_metrics",
                                  "get_kafka_backlog", "get_device_heartbeat"},
            "logs":              {"get_recent_logs"},
            "runbook":           {"query_runbook"},
        }
        missing = [
            cat for cat, tools in required_categories.items()
            if not (called & tools)
        ]
        if missing:
            messages.append({
                "role": "user",
                "content": (
                    f"提醒：你还没有完成必要的取证步骤，缺少 {missing}。"
                    "请继续调用对应工具，不要急于给出最终答案。"
                ),
            })

        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self.tool_schemas,
                tool_choice="auto",        # 让模型自己决定调工具还是直接回答
                temperature=self.temperature,
            )
        except Exception as e:
            # 网络错误、模型不存在等，返回 final 让 agent 优雅降级
            return {
                "action": "final",
                "answer": {
                    "intent": "unknown",
                    "conclusion": f"LLM 调用失败：{e}",
                    "evidence": [],
                    "suggestions": ["检查 Ollama 服务是否启动", f"确认模型 {self.model} 已下载"],
                    "safe_actions": [],
                },
            }

        choice = response.choices[0]
        finish_reason = choice.finish_reason   # "tool_calls" 或 "stop"

        # --- 模型决定调用工具（标准 tool_calls 字段） ---
        if finish_reason == "tool_calls" and choice.message.tool_calls:
            tool_call = choice.message.tool_calls[0]   # 我们每次只处理第一个
            fn_name = tool_call.function.name
            try:
                fn_args = json.loads(tool_call.function.arguments)
            except json.JSONDecodeError:
                fn_args = {}

            return {
                "action": "tool_call",
                "tool": fn_name,
                "args": fn_args,
                "thought": f"model decided to call {fn_name}",
            }

        raw_content = choice.message.content or ""

        # --- 兼容：某些本地模型（如 qwen2.5）把 tool_call 写进 content 而非 tool_calls 字段 ---
        # 形如: {"name":"get_recent_logs","arguments":{...}} 或 {"tool":"...","args":{...}}
        coerced = self._coerce_tool_call_from_content(raw_content)
        if coerced is not None:
            return {
                "action": "tool_call",
                "tool": coerced["tool"],
                "args": coerced["args"],
                "thought": "tool_call recovered from content (non-standard model output)",
            }

        # --- 模型决定直接回答（所有工具调用完毕） ---
        answer = self._parse_final_answer(raw_content)
        return {"action": "final", "answer": answer}

    @staticmethod
    def _coerce_tool_call_from_content(raw: str):
        """
        某些本地模型不会用标准的 tool_calls 字段，而是把工具调用 JSON 塞进 content。
        这里尝试识别并还原成 {tool, args} 结构。识别不到返回 None。

        策略（与具体模型无关）：
          1. 剥离所有 XML-like 标签 / 特殊 token 包装（<tool_call>、<|...|>、
             [TOOL_CALLS]、<think> 等等，无需逐个枚举模型方言）
          2. 用括号配平扫描器找出所有合法 JSON 对象（避免贪婪正则切错）
          3. 逐个尝试解析；若结构形如 {name, arguments} 且 name 在已注册工具集中
             则认定为工具调用
        """
        if not raw or not raw.strip():
            return None

        from app.tools.registry import TOOLS

        cleaned = _strip_special_tokens(raw)

        for blob in _iter_json_objects(cleaned):
            try:
                obj = json.loads(blob)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue

            # 兼容多种字段命名习惯：{name|tool|function, arguments|args|parameters}
            name = obj.get("name") or obj.get("tool") or obj.get("function")
            args = obj.get("arguments") or obj.get("args") or obj.get("parameters")

            if isinstance(name, str) and name in TOOLS:
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except json.JSONDecodeError:
                        args = {}
                if not isinstance(args, dict):
                    args = {}
                return {"tool": name, "args": args}

        return None

    @staticmethod
    def _parse_final_answer(raw: str) -> Dict[str, Any]:
        """
        尝试从模型回复中提取 JSON。
        模型有时会在 JSON 前后加说明文字 / 私有标记 token，这里统一用：
          1. 剥离 XML-like 标签和特殊 token
          2. 括号配平扫描器找所有 JSON 对象，逐个尝试
          3. 都失败则把"清洗后"的纯文本作为 conclusion 兜底（不会再出现 <tool_call> 之类原始标签）
        """
        if not raw:
            raw = ""

        cleaned = _strip_special_tokens(raw)

        # 优先：cleaned 整体直接是 JSON
        try:
            obj = json.loads(cleaned)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

        # 其次：从 cleaned 中扫描所有合法 JSON 对象，挑第一个看起来像最终答案的
        for blob in _iter_json_objects(cleaned):
            try:
                obj = json.loads(blob)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict) and (
                "conclusion" in obj or "intent" in obj or "suggestions" in obj
            ):
                return obj

        # 兜底：把已经清洗过的纯文本作为 conclusion，发挥大模型自由表达能力
        # （即使没匹配预设 intent，也能把模型的合理回答展示给用户，而不是乱码）
        return {
            "intent": "unknown",
            "conclusion": cleaned or "(模型未返回有效内容)",
            "evidence": [],
            "suggestions": [],
            "safe_actions": [],
        }
