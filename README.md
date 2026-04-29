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

## 7. 从 Demo 到通用 Agent：穷举到自适应的演进路线

### 7.1 当前限制与突破方向

当前 Demo 是"**枚举式 Agent**"——4 个 intent + 固定取证序列，用于教学和快速验证。但真实生产环境的问题是无法穷尽的，必须转向"**通用式 Agent**"。

| 维度 | 当前 Demo（枚举式） | 通用 Agent（自适应） |
|---|---|---|
| 问题边界 | 4 个写死的 intent | 不预设 intent，LLM 自主判断 |
| 工具发现 | 启动时静态注册 7 个 | 运行时动态发现（MCP），数百到数千个 |
| 取证序列 | system prompt 写死三步 | LLM 根据观察自适应规划 |
| 知识来源 | runbook.json 闭集（4 条） | 向量库 RAG，按需检索企业全部经验 |
| 异常恢复 | 一次性给出建议就结束 | Plan-Execute-Verify 闭环，失败重规划 |
| 学习能力 | 无 | 案例库沉淀，新问题复用旧经验 |

**核心思维转变**：不是"教 Agent 怎么解决 4 类问题"，而是"教 Agent 怎么思考问题、怎么用工具、怎么从经验中学习"。

---

### 7.2 五个关键改造点（按 ROI 排序）

#### ① 知识层：RAG 替代闭集 runbook（优先级最高）

**问题**：`runbook.json` 只有 4 条规则，新问题无对应 entry。

**改造方案**：
- 把企业 wiki / 历史工单 / 故障复盘 / 操作 SOP 全部入向量库（Chroma / Milvus / pgvector）
- 新工具 `search_knowledge(query, top_k=5)`：LLM 用自己的话查"有没有类似情况"
- 检索结果作为 few-shot 上下文喂给 LLM

**代码示例**：
```python
def search_knowledge(query: str, top_k: int = 5):
    embeddings = embed_model.encode(query)
    docs = vector_db.search(embeddings, k=top_k)
    summaries = [f"{d['title']}: {d['abstract']}" for d in docs]
    return {
        "ok": True,
        "summary": f"找到 {len(docs)} 条相关经验",
        "data": {"documents": docs, "summaries": summaries}
    }
```

**效果**：从"只懂 4 类问题" → "懂企业内部所有沉淀过的经验"

**工作量**：半天（接入向量库）+ 1 周（数据清洗和入库）

---

#### ② 工具层：从静态注册到动态发现（MCP）

**问题**：7 个工具写死在 `TOOLS` dict，新增能力要改代码。

**改造方案**：
- 按领域拆成多个 **MCP server**（监控团队管 Prometheus MCP，存储团队管 K8s MCP，日志团队管 Loki MCP）
- Agent 启动时 `list_tools()` 动态拉取，不限定工具数量
- 每个工具的 `description` 写得足够详细，让 LLM 看描述就能选对
- 工具超过 50 个时，加 **tool retrieval**：先用 RAG 召回相关工具子集再喂给 LLM

**效果**：从"7 个工具" → "理论上无上限的能力组合"

**工作量**：1-2 天（MCP 协议接入）+ 各团队持续贡献工具

---

#### ③ 规划层：移除硬编码 prompt，改用通用规划框架

**问题**：当前 `system_prompt` 里写死了"camera_offline 必须调这三个工具"，是穷举式。

**改造方案**：换成**开放式规划 prompt**：

```
你是工业系统的排查 Agent。流程：

1. 理解问题，但不要预先分类——保持开放态度
2. 查看可用工具列表，选一个最可能提供线索的
3. 看到结果后判断：
   - 是否已定位根因？
   - 还需要什么证据来排除其他可能？
4. 重复步骤 2-3，直到形成可信结论
5. 调用 search_knowledge 找类似案例佐证
6. 输出：根因 + 证据链 + 处置建议（区分诊断动作/恢复动作/根治动作）

【判断"证据充分"的启发式】：
- 至少一个指标类工具 + 一个日志类工具 + 一个知识类查询
- 证据之间能互相印证（时间戳对齐、因果链完整）
- 能回答"为什么发生"而不只是"发生了什么"

【禁止行为】：
- 在没有足够证据时凭直觉下结论
- 忽略工具返回的异常信号
- 跳过知识库查询就给出标准答案
```

**关键差异**：不再告诉 LLM"camera 问题要这么查"，而是告诉它"什么样的证据链是充分的"。

**工作量**：半天（改 prompt）+ 1 周（微调和验证）

---

#### ④ 闭环层：Plan-Execute-Verify-Replan

**问题**：当前是单次 run，给完建议就结束，无法验证处置是否有效。

**改造方案**：增加**验证回路**——

```
┌─────────────────────────────────────────────┐
│  1. Diagnose Phase（诊断）                  │
│     → 调用工具，给出根因假设                │
└────────────┬────────────────────────────────┘
             ▼
┌─────────────────────────────────────────────┐
│  2. Propose Phase（提议）                   │
│     → 列出处置动作 + 预期指标变化            │
│     → 标注风险等级，高风险需人工审批          │
└────────────┬────────────────────────────────┘
             ▼
┌─────────────────────────────────────────────┐
│  3. Execute Phase（执行）                   │
│     → dry-run 或人工放行后真实执行           │
└────────────┬────────────────────────────────┘
             ▼
┌─────────────────────────────────────────────┐
│  4. Verify Phase（验证）                    │
│     → 重新观察指标，假设是否被证实？         │
│     → 问题是否缓解？                         │
└────────────┬────────────────────────────────┘
             ▼
             ┌──────────────┐
             │ 已解决？     │
             └──┬────────┬──┘
                │ Yes    │ No
                ▼        ▼
             结束   Replan（排除失败方案，回到 1）
```

**代码框架**：
```python
def run_with_verification(user_query: str):
    replan_count = 0
    while replan_count < MAX_REPLAN:
        diagnosis = run_diagnosis_loop(user_query)
        actions = run_propose_loop(diagnosis)
        results = execute_with_approval(actions)
        if verify_problem_resolved(results):
            return {"status": "resolved", "trace": ...}
        replan_count += 1
        user_query = f"{user_query}\n上次尝试：{actions}，但问题未解决。"
    return {"status": "unresolved_after_replan", "trace": ...}
```

**效果**：从"建议型 Agent" → "自愈型 Agent"

**工作量**：3-5 天（核心循环改造）+ 2 周（验证逻辑）

---

#### ⑤ 记忆层：案例库 + 长期学习

**问题**：每次 run 都从零开始，相同问题反复推理。

**改造方案**：
- 每次 run 完成后，把 `(问题描述, trace, 最终结论, 是否成功)` 存入案例库（向量库）
- 新问题来时，先在案例库检索 top-3 相似案例，作为 few-shot 注入
- 累积 100+ 案例后，Agent 表现会显著超过纯 prompt 模式

**进阶**：周期性让 LLM 扫描案例库，自动提炼"经验规则"加到 system prompt（self-improving）。

**代码示例**：
```python
def enhance_with_cases(user_query: str) -> str:
    similar_cases = case_db.search(user_query, k=3)
    few_shot = "\n".join([
        f"【案例 {i+1}】问题：{c['query']} 结论：{c['conclusion']}"
        for i, c in enumerate(similar_cases)
    ])
    return f"{user_query}\n\n【参考案例】\n{few_shot}"
```

**效果**：越用越聪明，复杂问题首次解决率提升 30-50%

**工作量**：2-3 天（接入案例库）+ 持续沉淀

---

### 7.3 通用化后的架构图

```
                      用户问题
                        │
                        ▼
        ┌───────────────────────────────┐
        │   案例库检索（向量库）         │
        │   → 找到 3 条最相似历史案例    │
        └───────────┬───────────────────┘
                    ▼
        ┌───────────────────────────────┐
        │   Tool Retrieval（工具>50时） │
        │   → 从 N 个工具召回 top-10     │
        └───────────┬───────────────────┘
                    ▼
        ┌───────────────────────────────────────┐
        │  Agent 主循环（开放式规划）            │
        │  ┌─────────────────────────────────┐  │
        │  │ 1. 分析观察 + 知识 + 案例        │  │
        │  │ 2. LLM 选择：调工具/查知识/结论  │  │
        │  │ 3. 执行 → 观察 → 回到 1          │  │
        │  └─────────────────────────────────┘  │
        └───────────────┬───────────────────────┘
                        ▼
        ┌───────────────────────────────────────┐
        │  Verify-Replan 回路                   │
        │  执行处置 → 重观察 → 未解决则重规划    │
        └───────────────┬───────────────────────┘
                        ▼
        ┌───────────────────────────────────────┐
        │  写入案例库（用于下次复用）            │
        └───────────────────────────────────────┘
```

---

### 7.4 务实建议：演进优先级

如果今天就要让 Demo 向通用化迈进，按 ROI 推荐顺序：

| 优先级 | 改造点 | 工作量 | 收益 | 何时做 |
|---|---|---|---|---|
| 🔥 P0 | ① 知识层 RAG + 通用 prompt | 半天 | 立刻能处理 runbook 没覆盖的问题 | **现在** |
| ⭐ P1 | ② 工具层 MCP 动态加载 | 1-2 天 | 解耦工具开发与 Agent 开发 | 工具超过 10 个时 |
| ⭐ P1 | ⑤ 记忆层案例库 | 2-3 天 | 越用越聪明，显著提升首次解决率 | 有 50+ 真实 case 时 |
| 🎯 P2 | ④ Verify-Replan 闭环 | 3-5 天 | 从"建议"变"自愈" | 准备上生产时 |
| 📦 P3 | Tool retrieval | 后期 | 工具上百个时才必要 | 按需 |

---

### 7.5 通用化的边界：必须保留的硬约束

放开 ≠ 放任。以下三点必须保留，否则就是"放任 LLM 在生产环境瞎搞"：

| 约束 | 当前实现 | 生产强化 |
|---|---|---|
| **工具风险分级** | `risk="high"` 强制 dry-run | 高风险工具必须经过人工审批 + 回滚预案 |
| **执行预算** | `max_steps=6` | 每次 run 最多调用 N 次工具、M 分钟、影响 K 个实例 |
| **黑名单** | 无 | 某些动作（删数据、停核心服务）永远禁止 Agent 直接触发，只能"提议" |
| **可观测性** | 内存 trace | 所有决策 + 工具调用持久化，支持事后审计和回放 |
| **沙箱隔离** | Mock 数据 | 真实执行前在隔离环境预演，确认无副作用 |

---

### 7.6 一句话总结

> **当前 Demo 是"教 Agent 怎么解决 4 类问题"，生产 Agent 要做的是"教 Agent 怎么思考、怎么用工具、怎么从经验中学习"——前者枚举具体场景，后者沉淀通用方法论。**

---

## 8. Agent vs 普通脚本 vs 纯 LLM 问答

| | 普通脚本 | 纯 LLM 问答 | 本项目 Agent |
|---|---|---|---|
| 行为是否固定 | ✅ 固定 if-else | 一次性 | ✅ 动态多步 |
| 能否调用工具 | 手工写死 | ❌ 或非结构化 | ✅ 结构化 tool calling |
| 能否基于观察迭代 | ❌ | ❌ | ✅ plan→observe→plan |
| 证据来源 | 取决于实现 | ❌ 易幻觉 | ✅ 工具真实返回 |
| LLM / 工具是否可插拔 | ❌ | ❌ | ✅ |
| 是否有安全约束层 | 取决于实现 | 难 | ✅ policy + dry_run |

运行 `python -m app.main "2号相机掉线了"` 看终端里逐步出现的 **PLAN / ACT / OBSERVE**，是感受 Agent 与上述两者区别最直接的方式。
