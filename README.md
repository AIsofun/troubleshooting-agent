# 生产异常排查与建议处理 Agent · Demo

一个"**教学型但工程化**"的最小 Agent 演示项目，真正带
**Think → Act → Observe** 主循环、可插拔工具、可插拔 LLM 的 Agent 骨架。

支持两种运行模式：
- **MockLLM 模式**：内置规则引擎，无需大模型，零配置即可运行
- **OllamaLLM 模式**：接入本地 Ollama / 任何 OpenAI-compatible API，使用真实大模型动态规划

---

## 1. 项目目录结构

```
agentDemo/
├── app/
│   ├── agent/
│   │   ├── core.py       # Agent 主循环（Think-Act-Observe）
│   │   └── llm.py        # LLM 接口 + MockLLM + OllamaLLM
│   ├── tools/
│   │   └── registry.py   # 工具注册表（7 个 mock tools）+ call_tool 分发器
│   ├── mock_data/
│   │   ├── cameras.json
│   │   ├── logs.json
│   │   ├── kafka.json
│   │   ├── metrics.json
│   │   ├── runbook.json
│   │   └── heartbeat.json
│   ├── web/
│   │   └── server.py     # FastAPI + 最小 HTML 前端（可选）
│   ├── config.py         # 配置加载器 + get_llm() 工厂函数
│   └── main.py           # CLI 入口
├── config.yaml           # 唯一配置入口（LLM 地址、模型名、mock 开关）
├── requirements.txt
└── README.md
```

---

## 2. 快速开始

### 第一步：安装依赖

```powershell
python -m pip install -r requirements.txt
```

### 第二步：配置 LLM（编辑 config.yaml）

```yaml
llm:
  use_mock: false                           # true → MockLLM（无需大模型）; false → OllamaLLM
  base_url: "http://127.0.0.1:11434/v1"    # Ollama 地址，改成你的服务 IP
  model: "qwen2.5:14b"                     # 已下载的模型名（需支持 tool calling）
  api_key: "ollama"                         # Ollama 不校验，随便填；接 OpenAI 时填真实 key
  temperature: 0.0                          # 0.0 = 确定性输出，适合工具调用场景
```

不想配置大模型？把 `use_mock: true`，无需 Ollama 也能完整体验 Agent 流程。

推荐支持 tool calling 的本地模型：`qwen2.5:14b`、`qwen2.5:7b`、`llama3.1:8b`、`mistral`

### 第三步：运行

**CLI 模式（推荐先跑这个）：**
```powershell
# 一次性
python -m app.main "2号相机掉线了，最近10分钟没有图像"

# 交互式
python -m app.main
you> OCR识别成功率突然下降
```

**Web 页面模式：**
```powershell
python -m uvicorn app.web.server:app --reload --port 8000
# 浏览器打开 http://localhost:8000/
```

---

## 3. 支持的问题类型

| 关键词示例 | 触发 intent | 调用工具序列 |
|---|---|---|
| 相机 / cam-02 / 掉线 / 无图像 | `camera_offline` | `get_camera_status` → `get_recent_logs` → `query_runbook` |
| OCR / 识别 / 成功率 / 准确率 | `ocr_quality_drop` | `get_model_metrics` → `get_recent_logs` → `query_runbook` |
| Kafka / 堆积 / lag / 消费 | `kafka_backlog` | `get_kafka_backlog` → `get_recent_logs` → `query_runbook` |
| 推理 / inference / 延迟 / p99 / 慢 | `inference_latency_high` | `get_model_metrics` → `get_recent_logs` → `query_runbook` |

---

## 4. 示例运行效果

### 示例 1：相机掉线
输入：`2号相机掉线了，最近10分钟没有图像`

```
Step 0  USER    │ 2号相机掉线了，最近10分钟没有图像
Step 1  PLAN    │ → call get_camera_status  args={'camera_id': 'cam-02'}
        ACT     │ ⚙ get_camera_status(...)
        OBSERVE │ ✅ cam-02 status=offline last_frame=612s fps=0
Step 2  PLAN    │ → call get_recent_logs  args={'service_name': 'camera-service', 'limit': 5}
        ACT     │ ⚙ get_recent_logs(...)
        OBSERVE │ ✅ camera-service: 5 lines, 3 ERROR, 1 WARN
Step 3  PLAN    │ → call query_runbook  args={'issue_type': 'camera_offline'}
        ACT     │ ⚙ query_runbook(...)
        OBSERVE │ ✅ runbook: 相机掉线处置流程 (4 steps)
Step 4  FINAL
```

最终答案（JSON）：
```json
{
  "intent": "camera_offline",
  "conclusion": "2号相机（cam-02）当前状态为离线，最近一帧时间已达612秒，FPS为0。日志中存在3条ERROR和1条WARN，RTSP多次重连失败，初判为链路或设备侧故障。",
  "evidence": [
    "get_camera_status: cam-02 status=offline last_frame=612s fps=0",
    "get_recent_logs: camera-service: 5 lines, 3 ERROR, 1 WARN",
    "query_runbook: runbook: 相机掉线处置流程 (4 steps)"
  ],
  "suggestions": [
    "1. 确认相机网络可达（ping / 交换机端口）",
    "2. 检查 RTSP 拉流服务日志，看是否认证失败或链路抖动",
    "3. 尝试重启 camera-service（低风险）",
    "4. 如仍失败，派发现场工单检查供电与网线"
  ],
  "safe_actions": ["restart_service:camera-service"]
}
```

### 示例 2：OCR 质量下降
输入：`OCR识别成功率突然下降`
```
→ get_model_metrics(ocr-v3)    ✅ success=0.82 (baseline=0.98, drop=0.16) p99=260ms
→ get_recent_logs(ocr-service) ✅ 4 lines, 0 ERROR, 3 WARN（亮度偏低告警）
→ query_runbook(ocr_quality_drop)
```
结论：`OCR成功率0.82低于基线0.98，日志显示图像亮度偏低，初判上游图像质量下降。`

### 示例 3：Kafka 堆积
输入：`Kafka消费堆积报警很多`
```
→ get_kafka_backlog(vision.events) ✅ lag=42100 consumers=2 rate=350/s
→ get_recent_logs(kafka-consumer)  ✅ 1 ERROR, 2 WARN（rebalance 事件）
→ query_runbook(kafka_backlog)
```
结论：`topic消费堆积lag=42100，消费者数=2，出现rebalance，初判消费能力不足+消费者抖动。`

### 示例 4：高风险动作被 policy 拦截
当 Agent 规划调用 `restart_service` 时，主循环自动强制注入 `dry_run=True`，trace 标注：
```
⚙ ACT restart_service(...) (high-risk -> dry_run)
```
设置环境变量 `AGENT_ALLOW_RESTART=1` 才会真正（模拟）执行。

---

## 5. 架构解析

### 5.1 Agent 核心循环（app/agent/core.py）

```python
while step < max_steps:
    decision = self.llm.plan(query, tools_desc, observations)  # THINK
    if decision["action"] == "final": break                    # STOP
    result = call_tool(decision["tool"], decision["args"])     # ACT
    observations.append({"tool": ..., "result": result})      # OBSERVE
```

与 ReAct / OpenAI function calling / MCP client 是同一套骨架，只有 planner 和工具后端不同。

### 5.2 LLM 层（app/agent/llm.py）

| 类 | 说明 |
|---|---|
| `LLM` | Protocol 接口，只要求实现 `plan()` 方法 |
| `MockLLM` | 规则引擎，deterministic，用于离线测试 |
| `OllamaLLM` | 真实大模型，支持 Ollama / OpenAI-compatible API |

OllamaLLM 的三层健壮性设计（针对真实本地模型的实测问题）：

| 层 | 问题现象 | 解决方式 |
|---|---|---|
| System Prompt | 模型只调 1 个工具就给结论 | 强化 prompt：明确 ReAct 流程 + 强制三类取证（状态/日志/runbook）+ intent 枚举 + 中文输出约束 |
| 软兜底 | 模型不遵循 prompt 偷懒 | 每次 `plan()` 前检测取证完整性，缺失时追加 user 提醒消息 |
| 输出兼容 | qwen2.5 等把工具调用写进 content 而非 tool_calls | `_coerce_tool_call_from_content()` 识别并转回标准格式 |

### 5.3 工具层（app/tools/registry.py）

7 个工具，统一返回 `{ok, summary, data}`：

| 字段 | 用途 |
|---|---|
| `summary` | 单行摘要，喂给 LLM，不污染上下文 |
| `data` | 完整原始对象，供最终答案合成和 UI 展示 |

高风险工具 `restart_service` 打 `risk="high"`，`core.py` 执行前统一拦截并注入 `dry_run=True`。

### 5.4 配置层（config.yaml + app/config.py）

- 所有 LLM 配置集中在 `config.yaml`
- `app/config.py` 的 `get_llm()` 统一构造 LLM 实例，`main.py` 和 `server.py` 都调用它
- 只改 `config.yaml` 一处即可切换模型 / 地址 / mock 模式
- `_load_config()` 用 `@lru_cache` 装饰，进程内只读一次文件

### 5.5 接入其他 LLM

实现 `LLM` Protocol，在 `config.py` 的 `get_llm()` 加一个分支即可，其余代码零修改：

```python
class AzureOpenAILLM:
    def plan(self, user_query, tools_desc, observations) -> dict:
        # 参考 OllamaLLM，只需改 client 初始化和 endpoint
        ...
```

### 5.6 接入 MCP

`app/tools/registry.py` 是 MCP 切入点：
- `TOOLS` dict → 改为从 MCP server `list_tools()` 动态拉取
- `call_tool()` → 改为 MCP `call_tool` RPC
- `description / parameters` 已是 JSON-Schema 风格，天然兼容

---

## 6. 工业落地扩展建议

| 方向 | 当前状态 | 扩展方式 |
|---|---|---|
| 真实工具 | Mock JSON 文件 | 替换函数体，接真实 API / CLI / MCP（相机/Kafka/Prometheus/Loki） |
| 记忆 | 无 | 短期 trace；长期向量库 + RAG runbook |
| 计划验证 | 单次运行 | Plan → Execute → Verify 闭环，失败重规划 |
| 监控 | 无 | 每步发 OpenTelemetry span（`agent.step`、`tool.call`） |
| 高风险治理 | dry_run 拦截 | 写操作走独立 action service，人工审批后执行 |
| 数据安全 | 无 | 日志 / PII 脱敏后才进 LLM 上下文 |
| 审计 | 内存 trace | trace 持久化到数据库，保留完整调用链路 |

---

## 7. Agent vs 普通脚本 vs 纯 LLM 问答

| | 普通脚本 | 纯 LLM 问答 | 本项目 Agent |
|---|---|---|---|
| 行为是否固定 | ✅ 固定 if-else | 一次性 | ✅ 动态多步 |
| 能否调用工具 | 手工写死 | ❌ 或非结构化 | ✅ 结构化 tool calling |
| 能否基于观察迭代 | ❌ | ❌ | ✅ plan→observe→plan |
| 证据来源 | 取决于实现 | ❌ 易幻觉 | ✅ 工具真实返回 |
| LLM / 工具是否可插拔 | ❌ | ❌ | ✅ |
| 是否有安全约束层 | 取决于实现 | 难 | ✅ policy + dry_run |

运行 `python -m app.main "2号相机掉线了"` 看终端里逐步出现的 **PLAN / ACT / OBSERVE**，是感受 Agent 与上述两者区别最直接的方式。
