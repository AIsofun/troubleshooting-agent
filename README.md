# 生产异常排查与建议处理 Agent

一个**教学型但工程化**的最小 Agent 演示项目，带完整的 Think → Act → Observe 主循环、Verify-Replan 自校验机制、混合检索知识库、MCP 工具层与 Docker 部署支持。

支持两种运行模式：
- **MockLLM 模式**：内置规则引擎，无需大模型，零配置即可运行
- **OllamaLLM 模式**：接入本地 Ollama / 任何 OpenAI-compatible API，使用真实大模型动态规划

---

## 目录

1. [项目目录结构](#1-项目目录结构)
2. [快速开始（本地）](#2-快速开始本地)
3. [Docker 部署](#3-docker-部署)
4. [配置说明](#4-配置说明)
5. [支持的问题类型](#5-支持的问题类型)
6. [API 参考](#6-api-参考)
7. [测试](#7-测试)
8. [架构解析](#8-架构解析)
9. [从 Demo 到通用 Agent](#9-从-demo-到通用-agent)

---

## 1. 项目目录结构

```
agentDemo/
├── app/
│   ├── agent/
│   │   ├── core.py          # Agent 主循环 + Verify-Replan 外循环
│   │   ├── llm.py           # LLM 接口 + MockLLM + OllamaLLM
│   │   ├── intent.py        # IntentRegistry：意图匹配
│   │   ├── planner.py       # ReactPlanner：system prompt 生成 + 工具参数解析
│   │   └── verifier.py      # AnswerVerifier：答案质量自校验
│   ├── tools/
│   │   └── registry.py      # 工具注册表（8 个 mock tools）+ MCP 路由
│   ├── mcp/
│   │   ├── server.py        # MCP JSON-RPC 2.0 服务端（FastAPI router）
│   │   ├── client.py        # MCP HTTP 客户端（连接远程 MCP 服务器）
│   │   └── adapter.py       # MCPAdapter：本地工具 + 远程 MCP 统一路由
│   ├── knowledge/
│   │   ├── embedder.py      # 文本向量化（bge-m3 / mock）
│   │   ├── vector_store.py  # Qdrant 向量存储
│   │   ├── keyword_store.py # BM25 关键词索引
│   │   ├── reranker.py      # 交叉编码器重排序
│   │   ├── retriever.py     # 混合检索（向量 + BM25 + 重排）
│   │   └── ingest.py        # 知识入库 CLI
│   ├── cases/
│   │   ├── schema.py        # TraceRecord / ToolCallRecord / CaseRecord Pydantic 模型
│   │   ├── candidate.py     # CandidateEngine：从 trace 自动生成候选经验
│   │   └── case_repo.py     # CaseRepository：案例库 CRUD（支持 DB / 文件降级）
│   ├── persistence/
│   │   └── db.py            # SQLAlchemy 2.0 ORM + 初始化（graceful degradation）
│   ├── observability/
│   │   └── logging.py       # structlog 结构化日志配置
│   ├── mock_data/           # 本地 mock JSON 文件（cameras / logs / kafka …）
│   ├── web/
│   │   └── server.py        # FastAPI 服务 + 所有 HTTP 端点
│   ├── config.py            # 配置加载器（多层合并）+ get_llm() 工厂
│   └── main.py              # CLI 入口
├── config/
│   ├── base.yaml            # 基础配置（所有环境共享）
│   ├── dev.yaml             # 开发环境覆盖（use_mock: true 等）
│   └── prod.yaml            # 生产环境覆盖
├── tests/                   # 171 个测试用例
├── alembic/                 # DB 迁移脚本（Phase 2-3）
├── scripts/
│   ├── up.sh / up.ps1       # 一键启动脚本
│   └── pull_models.ps1      # 拉取 Ollama 模型
├── docker-compose.yml       # 服务编排（core + obs profiles）
├── docker-compose.override.yml
├── Dockerfile
├── Makefile                 # 常用命令封装
├── pyproject.toml           # 项目元信息 + 依赖
├── .env.example             # 环境变量模板
└── config.yaml              # 旧版单文件配置（向后兼容）
```

---

## 2. 快速开始（本地）

### 前置要求

- Python 3.10+（推荐 3.12）
- （可选）Ollama，并已拉取支持 tool calling 的模型

### 第一步：安装依赖

```powershell
# 开发模式安装（含 dev extras）
pip install -e ".[dev]"

# 或仅安装运行依赖
pip install -r requirements.txt
```

### 第二步：配置运行环境

复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

**最简配置（MockLLM，无需大模型）** — 编辑 `config/dev.yaml`：

```yaml
llm:
  use_mock: true    # 使用内置规则引擎，无需 Ollama
```

或者设置环境变量：

```powershell
$env:LLM__USE_MOCK = "true"
$env:APP_ENV = "dev"
```

**接入 Ollama**（有真实大模型时）— 编辑 `.env`：

```env
LLM__USE_MOCK=false
LLM__BASE_URL=http://127.0.0.1:11434/v1
LLM__MODEL=qwen2.5:14b
```

推荐模型（需支持 tool calling）：`qwen2.5:14b`、`qwen2.5:7b`、`llama3.1:8b`

### 第三步：运行

**CLI 模式（推荐先跑这个）：**

```powershell
# 单次查询
APP_ENV=dev python -m app.main "2号相机掉线了，最近10分钟没有图像"

# 或用 Makefile
make run
```

**Web 服务模式：**

```powershell
# 启动 FastAPI 服务，浏览器打开 http://localhost:8000
make serve

# 或直接
APP_ENV=dev uvicorn app.web.server:app --reload --port 8000
```

---

## 3. Docker 部署

### 前置要求

- Docker Desktop 4.x+
- （推荐）Ollama 已在宿主机运行

### 核心服务（agent-api + qdrant + postgres）

```powershell
# 1. 复制并填写环境变量
Copy-Item .env.example .env

# 2. 构建镜像
make build

# 3. 启动核心服务
make up
# 等价于：docker compose --profile core up -d
```

Agent API 将在 `http://localhost:8000` 可用。

### 含可观测性（+ prometheus + loki + grafana）

```powershell
make up-obs
# 等价于：docker compose --profile core --profile obs up -d
```

| 服务 | 地址 |
|------|------|
| Agent API | http://localhost:8000 |
| Grafana   | http://localhost:3000 （admin / 见 .env） |
| Prometheus | http://localhost:9090 |

### 数据库迁移

```powershell
# 初始化 / 升级到最新
make db-migrate

# 回滚一步
make db-rollback
```

### 停止服务

```powershell
make down        # 停止容器（保留 volume）
make down-v      # 停止并删除所有 volume（数据清空）
```

### 查看日志

```powershell
make logs        # 跟踪 agent-api 日志
```

---

## 4. 配置说明

### 配置层级（优先级从低到高）

```
config/base.yaml          ← 基础默认值（提交到 Git）
config/dev.yaml           ← 开发环境覆盖（提交到 Git）
config/prod.yaml          ← 生产环境覆盖（提交到 Git，不含秘钥）
.env                      ← 本地/容器环境变量（不提交 Git）
```

环境变量命名规则：`SECTION__KEY`，例如 `LLM__BASE_URL`、`AGENT__MAX_STEPS`。

### 关键配置项

```yaml
llm:
  use_mock: false                    # true = MockLLM（无需 Ollama）
  base_url: "http://ollama:11434/v1" # LLM API 地址
  model: "qwen2.5:14b"              # 模型名
  temperature: 0.0                   # 0.0 = 确定性输出

agent:
  max_steps: 8           # ReAct 内循环最大步数
  max_replan: 2          # Verify-Replan 最大重规划次数
  enable_verify: true    # 是否开启答案自校验
  budget_seconds: 120    # 单次排查总时间上限（秒）
  budget_tool_calls: 12  # 单次排查工具调用次数上限
  # Verifier 参数
  verify_pass_threshold: 0.65     # 综合评分低于此值则重规划
  verify_min_conclusion_len: 20   # 结论最短字数
  verify_require_numeric: true    # 结论中须引用具体数值
  verify_require_suggestions: false

mcp:
  servers: []            # 远程 MCP 服务器列表（Phase 6）
  # 示例：
  # - name: "my-tools"
  #   url: "http://my-mcp-server:8080/mcp/v1"
  #   timeout: 5.0
```

---

## 5. 支持的问题类型

| 关键词示例 | 触发 intent | 调用工具序列 |
|---|---|---|
| 相机 / cam-02 / 掉线 / 无图像 | `camera_offline` | `get_camera_status` → `get_recent_logs` → `query_runbook` |
| OCR / 识别 / 成功率 / 准确率 | `ocr_quality_drop` | `get_model_metrics` → `get_recent_logs` → `query_runbook` |
| Kafka / 堆积 / lag / 消费 | `kafka_backlog` | `get_kafka_backlog` → `get_recent_logs` → `query_runbook` |
| 推理 / inference / 延迟 / p99 / 慢 | `inference_latency_high` | `get_model_metrics` → `get_recent_logs` → `query_runbook` |
| 算法 / 误报 / 误检 | `algorithm_false_reject` | `get_model_metrics` → `get_recent_logs` → `query_runbook` |

使用 OllamaLLM 时，意图由大模型自由判断，不受此列表限制。

---

## 6. API 参考

服务启动后访问 `http://localhost:8000/docs` 查看完整 Swagger UI。

### Agent 排查

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/run` | 执行一次 Agent 排查，返回 trace_id 和完整结果 |
| `GET`  | `/api/traces` | 列出历史排查记录（分页，`?limit=20&offset=0`） |
| `GET`  | `/api/traces/{trace_id}` | 查询单条排查记录详情 |
| `POST` | `/api/traces/{trace_id}/feedback` | 工程师反馈（触发候选经验生成） |

**POST /api/run 请求示例：**

```bash
curl -X POST http://localhost:8000/api/run \
  -H "Content-Type: application/json" \
  -d '{"query": "2号相机掉线了"}'
```

**响应示例：**

```json
{
  "trace_id": "abc123",
  "answer": {
    "intent": "camera_offline",
    "conclusion": "cam-02 离线，最后一帧 612 秒前，FPS=0，RTSP 连接多次失败。",
    "evidence": ["get_camera_status: cam-02 status=offline last_frame=612s fps=0", "..."],
    "suggestions": ["检查网络连通性", "重启 camera-service（低风险）", "派发现场工单"],
    "safe_actions": ["restart_service:camera-service"]
  },
  "trace": [...],
  "verify_score": 0.82,
  "replan_count": 0,
  "budget_exceeded": false
}
```

### 候选经验 & 案例库

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET`  | `/api/candidates` | 列出待审核的候选经验文件 |
| `GET`  | `/api/cases` | 列出已入库的正式案例 |
| `POST` | `/api/cases/promote/{id}` | 审核通过，晋升候选经验为正式案例 |
| `POST` | `/api/cases/reject/{id}` | 拒绝候选经验 |

### MCP 工具层

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/mcp/v1` | MCP JSON-RPC 2.0 主入口（initialize / tools/list / tools/call） |
| `GET`  | `/mcp/v1/tools` | REST 方式列出所有可用工具 |
| `POST` | `/mcp/v1/tools/{tool_name}` | REST 方式调用指定工具 |

### 其他

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/` | Web UI（HTML 交互页面） |
| `GET` | `/health` | 健康检查 |

---

## 7. 测试

```powershell
# 运行全部测试
make test
# 等价于：pytest tests/ -v --tb=short

# 带覆盖率报告
make test-cov

# 运行特定测试文件
pytest tests/test_verify_replan.py -v
```

**当前测试状态：171 passed, 1 skipped**（skipped = SQLAlchemy 未安装时跳过 DB 测试，Docker 中全绿）

测试文件覆盖：

| 文件 | 覆盖内容 |
|------|----------|
| `test_agent_loop.py` | Agent ReAct 主循环、工具调用、高风险 dry_run |
| `test_cases_schema.py` | TraceRecord / CaseRecord 数据模型 |
| `test_config.py` | 配置加载、多层合并、环境变量覆盖 |
| `test_knowledge.py` | 混合检索、向量存储、BM25、重排序 |
| `test_mcp.py` | MCP JSON-RPC 端点、工具路由 |
| `test_planner.py` | IntentRegistry、ReactPlanner |
| `test_reflow.py` | 案例库 API、候选经验晋升/拒绝 |
| `test_tools.py` | 8 个工具函数 |
| `test_verify_replan.py` | AnswerVerifier、Verify-Replan 外循环、预算守护 |

---

## 8. 架构解析

### 8.1 Agent 执行流程

```
用户问题
  │
  ▼
┌─────────────────────────────────────────────────────────┐
│  Verify-Replan 外循环（最多 max_replan 次）              │
│  ┌──────────────────────────────────────────────────┐   │
│  │  ReAct 内循环（最多 max_steps 步）               │   │
│  │    THINK（LLM 规划）                             │   │
│  │      → ACT（工具调用 / policy 过滤）             │   │
│  │        → OBSERVE（结果追加到上下文）              │   │
│  │          → （循环）→ FINAL answer                │   │
│  └──────────────────────────────────────────────────┘   │
│  ↓                                                       │
│  VERIFY（AnswerVerifier 自校验，0-1 评分）               │
│    score ≥ 0.65 → return AgentResult ✅                  │
│    score < 0.65 → 追加 replan_hint → 继续外循环          │
│                                                          │
│  预算守护（任一触发立即返回）：                           │
│    wall-clock 时间 > budget_seconds                       │
│    累计工具调用 > budget_tool_calls                       │
└─────────────────────────────────────────────────────────┘
  │
  ▼
AgentResult { answer, trace, verify_score, replan_count, budget_exceeded }
```

### 8.2 主要模块

#### LLM 层（app/agent/llm.py）

| 类 | 说明 |
|---|---|
| `LLM` | Protocol 接口，只要求实现 `plan()` |
| `MockLLM` | 规则引擎，deterministic，无需大模型 |
| `OllamaLLM` | 真实大模型，支持 Ollama / OpenAI-compatible API |

OllamaLLM 的三层健壮性设计：

| 层 | 问题现象 | 解决方式 |
|---|---|---|
| System Prompt | 模型只调 1 个工具就给结论 | 强化 prompt：要求完整证据链 + intent 枚举 + 中文输出约束 |
| 软兜底 | 模型不遵从 prompt | 每次 plan() 前检测取证完整性，缺失时追加 user 提醒 |
| 输出兼容 | 工具调用写进 content 而非 tool_calls | `_coerce_tool_call_from_content()` 自动识别并转换 |

#### 工具层（app/tools/registry.py + app/mcp/）

8 个内置工具，统一返回 `{ok, summary, data}`：

| 工具 | 类型 | 说明 |
|------|------|------|
| `get_camera_status` | 查询 | 摄像头在线状态 |
| `get_recent_logs` | 查询 | 服务近期日志 |
| `get_kafka_backlog` | 查询 | Kafka topic 消费堆积 |
| `get_model_metrics` | 查询 | 模型推理指标（accuracy / p99） |
| `get_device_heartbeat` | 查询 | 设备心跳检测 |
| `query_runbook` | 知识 | 故障处置 runbook 检索 |
| `search_knowledge` | 知识 | 向量知识库语义检索 |
| `restart_service` | **高风险** | 服务重启（自动强制 dry_run） |

高风险工具 `restart_service` 打 `risk="high"`，Agent 主循环执行前统一注入 `dry_run=True`。设置 `AGENT_ALLOW_RESTART=1` 才会真实（模拟）执行。

MCP 适配层（`app/mcp/adapter.py`）在本地工具基础上支持挂接远程 MCP 服务器，工具数量无上限。

#### AnswerVerifier（app/agent/verifier.py）

确定性评分（无 LLM，零延迟）：

| 维度 | 权重 | 说明 |
|------|------|------|
| 必填字段完整性 | 30% | intent / conclusion / evidence 是否存在 |
| 结论质量 | 30% | 长度是否足够 + 是否引用具体数值 |
| 证据覆盖率 | 25% | evidence 条数与工具调用数的比值 |
| 处置建议 | 15% | suggestions 是否非空 |

综合评分 < `verify_pass_threshold`（默认 0.65）或触发硬失败（结论过短、缺少建议）时，生成 `replan_hint` 追加到下一轮查询。

#### 配置层（config/ + app/config.py）

- 多层 YAML 合并：`base.yaml` → `dev/prod.yaml` → 环境变量
- `get_settings()` 带 `@lru_cache`，进程内只读一次
- `get_llm()` 工厂函数，按配置返回 MockLLM 或 OllamaLLM

### 8.3 接入其他 LLM

实现 `LLM` Protocol，在 `config.py` 的 `get_llm()` 加一个分支即可，其余代码零修改：

```python
class AzureOpenAILLM:
    def plan(self, user_query, tools_desc, observations) -> dict:
        # 参考 OllamaLLM，只需改 client 初始化和 endpoint
        ...
```

### 8.4 接入 MCP 远程服务器

在 `config/base.yaml` 的 `mcp.servers` 中添加服务器配置，Agent 启动时自动连接：

```yaml
mcp:
  servers:
    - name: "prometheus-tools"
      url: "http://prom-mcp:8080/mcp/v1"
      timeout: 5.0
    - name: "k8s-tools"
      url: "http://k8s-mcp:8080/mcp/v1"
```

---

## 9. 从 Demo 到通用 Agent

### 9.1 当前实现与生产扩展

| 维度 | 当前 Demo | 生产扩展方向 |
|---|---|---|
| 问题边界 | 5 个配置化 intent | LLM 自主判断，intent 不预设上限 |
| 工具发现 | 启动时静态注册 8 个 | MCP 动态发现，数百至数千个工具 |
| 知识来源 | `runbook.json` 闭集（4 条） | 向量库 RAG，接入企业全量 wiki / 工单 |
| 答案质量 | AnswerVerifier 规则评分 | 追加 LLM-as-Judge 语义评判 |
| 闭环能力 | Verify-Replan 重规划 | Plan-Execute-Verify 自愈闭环 |
| 学习能力 | 案例晋升（人工审核） | 案例库自动反哺向量知识库 |

### 9.2 工业落地扩展建议

| 方向 | 扩展方式 |
|---|---|
| **真实工具** | 替换 mock 函数体，接真实 API / CLI / MCP（相机/Kafka/Prometheus/Loki） |
| **知识库** | 把企业 wiki / 历史工单 / SOP 全部入 Qdrant，`search_knowledge` 即可检索 |
| **监控** | 每步发 OpenTelemetry span（`agent.step`、`tool.call`），Grafana 可视化 |
| **高风险治理** | 写操作走独立 action service，需人工审批后执行；永久黑名单禁止直接触发 |
| **数据安全** | 日志 / PII 脱敏后才进 LLM 上下文 |
| **审计** | trace 已持久化到 Postgres，支持完整调用链路回放 |

### 9.3 硬约束：放开不等于放任

| 约束 | 当前实现 | 生产强化 |
|---|---|---|
| **工具风险分级** | `risk="high"` 强制 dry_run | 高风险工具必须经过人工审批 + 回滚预案 |
| **执行预算** | `budget_seconds` + `budget_tool_calls` | 还需限制影响实例数、变更窗口 |
| **黑名单** | 无 | 删数据、停核心服务永远只能"提议"，不能直接执行 |
| **可观测性** | structlog + trace 持久化 | 接 OTel Collector → Loki + Tempo |
| **沙箱隔离** | Mock 数据 | 生产写操作前在隔离环境预演 |

---

## Agent vs 普通脚本 vs 纯 LLM 问答

| | 普通脚本 | 纯 LLM 问答 | 本项目 Agent |
|---|---|---|---|
| 行为是否固定 | ✅ 固定 if-else | 一次性 | 动态多步 |
| 能否调用工具 | 手工写死 | ❌ 或非结构化 | ✅ 结构化 tool calling |
| 能否基于观察迭代 | ❌ | ❌ | ✅ plan→observe→plan |
| 证据来源 | 取决于实现 | ❌ 易幻觉 | ✅ 工具真实返回 |
| 答案自校验 | ❌ | ❌ | ✅ Verify-Replan |
| LLM / 工具是否可插拔 | ❌ | ❌ | ✅ |
| 安全约束层 | 取决于实现 | 难 | ✅ policy + dry_run + 预算守护 |

运行 `python -m app.main "2号相机掉线了"` 看终端里逐步出现的 **PLAN / ACT / OBSERVE / VERIFY**，是感受 Agent 与上述两者区别最直接的方式。