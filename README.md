# 生产异常排查与建议处理 Agent · Demo

一个“**教学型但工程化**”的最小 Agent 演示项目。
它不是一个普通的 if-else 脚本，也不是一次性 LLM 问答，而是一个真正带
**Think → Act → Observe** 主循环、可插拔工具、可插拔 LLM 的 Agent 骨架。

---

## 1. 项目设计说明

**主题**：生产现场异常排查与处置建议 Agent。
用户用自然语言描述现场问题，Agent：

1. 理解问题（intent）
2. 决定要调用哪些工具
3. 逐步调用 mock 工具拿数据
4. 汇总证据
5. 给出诊断结论 + 处置建议 + 可执行的低风险动作
6. 所有步骤都有清晰可观察的日志

**核心特征**：
- **Agent loop**：`plan → tool_call → observe → plan …` 直到 `final`
- **Tool registry**：OpenAI-function / MCP 风格的 schema，方便以后替换
- **Planner 可替换**：`MockLLM`（规则）和真实 LLM 只需实现同一个 `plan()` 方法
- **高风险动作 policy**：`restart_service` 默认 `dry_run=True`，由 agent 主循环统一拦截
- **Trace 事件流**：CLI 和 Web UI 都展示每一步

为什么这不是普通脚本：
- 工具调用序列不是写死的，而是由 planner 基于 query + 已观察结果决定
- 循环会根据 observations 增量决定下一步；换 LLM 后就变成真动态规划
- 与“纯 LLM 问答”的区别：**它会主动拉数据、基于真实观测下结论**，而不是凭空回答

---

## 2. 项目目录结构

```
agentDemo/
├── app/
│   ├── agent/
│   │   ├── core.py           # Agent 主循环（Think-Act-Observe）
│   │   └── llm.py            # LLM 接口 + MockLLM（可替换真实 LLM）
│   ├── tools/
│   │   └── registry.py       # 工具定义 + schema + call_tool 分发器
│   ├── mock_data/
│   │   ├── cameras.json
│   │   ├── logs.json
│   │   ├── kafka.json
│   │   ├── metrics.json
│   │   ├── runbook.json
│   │   └── heartbeat.json
│   ├── web/
│   │   └── server.py         # FastAPI + 最小 HTML 前端（可选）
│   └── main.py               # CLI 入口
├── requirements.txt
└── README.md
```

---

## 3. 运行步骤

### 安装依赖
```powershell
cd f:\code\myCode\agentDemo
python -m pip install -r requirements.txt
```

### 方式 A：CLI（推荐先跑这个）
```powershell
# 一次性
python -m app.main "2号相机掉线了，最近10分钟没有图像"

# 交互式
python -m app.main
you> OCR识别成功率突然下降
```

### 方式 B：Web 页面
```powershell
python -m uvicorn app.web.server:app --reload --port 8000
```
浏览器打开 http://localhost:8000/ 。

支持的四种问题类型（关键词触发）：
- 相机 / camera / cam-02 / 掉线 / 无图像 → `camera_offline`
- OCR / 识别 / 成功率 → `ocr_quality_drop`
- Kafka / 堆积 / lag / 消费 → `kafka_backlog`
- 推理 / inference / 延迟 / p99 → `inference_latency_high`

---

## 4. 示例运行效果

### 示例 1：相机掉线
输入：`2号相机掉线了，最近10分钟没有图像`

Agent 行为：
```
Step 1 PLAN  → get_camera_status(camera_id=cam-02)
        OBSERVE ✅ cam-02 status=offline last_frame=612s fps=0
Step 2 PLAN  → get_recent_logs(service_name=camera-service)
        OBSERVE ✅ camera-service: 5 lines, 3 ERROR, 1 WARN
Step 3 PLAN  → query_runbook(issue_type=camera_offline)
        OBSERVE ✅ runbook: 相机掉线处置流程 (4 steps)
Step 4 FINAL
```
结论：`相机 10.0.0.12 已离线，最近 612s 无帧，RTSP 多次重连失败，初判链路/设备故障。`
处置建议：检查网络、RTSP 日志、重启 `camera-service`、派工单。
可执行低风险动作：`restart_service:camera-service`。

### 示例 2：OCR 质量下降
输入：`OCR识别成功率突然下降`

Agent 行为：
```
→ get_model_metrics(model_name=ocr-v3)
  ✅ success=0.82 (baseline=0.98, drop=0.16) p99=260ms
→ get_recent_logs(service_name=ocr-service)
  ✅ 4 lines, 0 ERROR, 3 WARN (亮度偏低告警)
→ query_runbook(issue_type=ocr_quality_drop)
```
结论：`OCR 成功率 0.82 低于基线 0.98，伴随图像亮度告警，初判上游图像质量下降。`

### 示例 3：Kafka 堆积
输入：`Kafka 消费堆积报警很多`
```
→ get_kafka_backlog(topic=vision.events)
  ✅ lag=42100 consumers=2 rate=350/s
→ get_recent_logs(service_name=kafka-consumer)
  ✅ 1 ERROR, 2 WARN (rebalance)
→ query_runbook(issue_type=kafka_backlog)
```
结论：`lag=42100 且有 rebalance，初判消费能力不足 + 消费者抖动。`
建议：扩容消费者、关注 rebalance。

### 示例 4：高风险动作被 policy 拦截
若未来 planner 决定调用 `restart_service`，主循环会强制注入 `dry_run=True` 并在 trace 中标注 `policy: high-risk -> dry_run`。设置环境变量 `AGENT_ALLOW_RESTART=1` 才会真正（模拟）执行。

---

## 5. 架构解析（工程师视角）

### 5.1 Agent 核心循环
`app/agent/core.py` 的 `Agent.run()`：
```python
while step < max_steps:
    decision = self.llm.plan(query, tools_desc, observations)  # THINK
    if decision.action == "final": return ...                  # STOP
    result = call_tool(decision.tool, decision.args)           # ACT
    observations.append(...)                                   # OBSERVE
```
这就是 **ReAct / OpenAI function calling / MCP client** 的同一套骨架。
真实项目里 `llm.plan()` 不会只返回一步，也不会是规则实现，但循环形状不变。

### 5.2 Tool calling 怎么实现
- `app/tools/registry.py` 的 `TOOLS` dict 即“工具注册表”，每项有 `fn / description / parameters / risk`
- `call_tool(name, args)` 是统一分发器，做参数检查 + 异常捕获 + 返回统一结构 `{ok, summary, data}`
- 这种“结构化返回 + human summary”的设计非常关键：
  - `summary` 直接喂给 LLM 做下一步规划，不会污染上下文
  - `data` 给下游或 UI 使用

### 5.3 规划与执行如何分开
- **Planner（llm.py）**：无副作用，只产出 `{action, tool, args}` 或 `{action:"final"}`
- **Executor（core.py）**：负责执行工具、维护 observations、做 policy 检查
这种分层让你能做：把 planner 换成真 LLM、把 executor 换成异步/重试/审计版本，互不影响。

### 5.4 接入真实大模型应该改哪里
只需新建一个类实现 `LLM` 协议，并在 `Agent(llm=...)` 注入：
```python
class OpenAILLM:
    def plan(self, user_query, tools_desc, observations):
        # 1. 把 TOOLS 转成 OpenAI function/tool schema
        # 2. messages = [system, user_query, *observations_as_tool_messages]
        # 3. response = openai.chat.completions.create(..., tools=schemas)
        # 4. 如果 response 含 tool_call → 返回 {"action":"tool_call", ...}
        #    否则                        → 返回 {"action":"final", "answer":...}
```
其余代码（主循环、工具、web、CLI）**一行不用动**。

### 5.5 接入 MCP 应该改哪里
- `app/tools/registry.py` 是 MCP 切入点。把 `TOOLS` 改成从 MCP server 动态 `list_tools()` 拿来
- `call_tool()` 改成 MCP `call_tool` RPC
- `description/parameters` 本来就是 MCP/JSON-Schema 风格，天然兼容

### 5.6 接入真实生产系统必须加的东西
| 维度 | 必须加 |
|---|---|
| 权限 | 每个工具打 `risk` 标签（已示例），高风险要人审批或二次确认 |
| 审计 | 所有 `tool_call / tool_result` 落持久化 trace（当前只在内存） |
| 回滚 | 写操作必须幂等、可撤销，或生成 change-ticket |
| 限流 | 同 query 多次调用去重、整体 step/时间上限（已有 `max_steps`）|
| 数据边界 | 日志/PII 脱敏后才能进 LLM |
| 沙箱 | 所有 side-effect 工具默认 dry-run，经策略/人工放行后才真执行 |

---

## 6. 如何把这个 Demo 演进成真正工业可用的 Agent

1. **接入真实 LLM**：实现 `OpenAILLM / AzureLLM / LocalLLM`，走 function calling；把 `MockLLM` 留作离线回归测试。
2. **替换 mock tools**：
   - 相机 → 真实 VMS / ONVIF API
   - 日志 → Loki / ES 查询
   - Kafka → Kafka Admin API
   - 指标 → Prometheus PromQL
   - runbook → 企业知识库 / RAG
   - 通过 **MCP server** 暴露这些能力，实现跨团队复用。
3. **增加 memory**：
   - 短期：每次 run 的 trace
   - 长期：同类 incident 的历史结论、向量库里的 runbook 片段（RAG）
   - 会话级：同一个值班人员的上下文（谁、管哪些线、偏好）
4. **计划-执行-验证闭环**：
   - Plan：让 LLM 先产出完整处置计划（多步 + 预期结果）
   - Execute：逐步执行，每步校验 precondition
   - Verify：执行后重新观察指标，验证问题是否缓解；没缓解则重规划
5. **监控/日志/告警对接**：
   - 每次 run 发 OpenTelemetry span（`agent.step`, `tool.call`）
   - Agent 自身指标：成功率、平均步数、工具错误率
   - 关键事件（high-risk action、fallback）触发告警
6. **高风险动作治理**：
   - 所有写操作走独立“action service”，Agent 只能**提议**，不能直接执行
   - 提议 → 审批工单 → 人工点击 → 带审计执行 → 回写结果给 Agent
   - 代码里的 `risk="high"` 分支就是这一层的雏形

---

## 7. 快速回顾：Agent vs 普通脚本 vs 普通 LLM 问答

| | 普通脚本 | 纯 LLM 问答 | 本 Demo (Agent) |
|---|---|---|---|
| 行为是否固定 | 固定 if-else | 一次性输入输出 | 动态多步 |
| 能否调用工具 | 手工写 | ❌ 或非结构化 | ✅ 结构化 tool calling |
| 能否基于观察迭代 | ❌ | ❌ | ✅ plan→observe→plan |
| 证据是否来自真实系统 | 取决于实现 | ❌（易幻觉） | ✅ 来自工具返回 |
| 是否可插拔 LLM/工具 | ❌ | ❌ | ✅ |
| 是否能做安全约束 | 取决于实现 | 难 | ✅ policy layer |

跑一下 `python -m app.main "2号相机掉线了"`，看终端里每一步 PLAN/ACT/OBSERVE，就能直观感受到这个差异。
