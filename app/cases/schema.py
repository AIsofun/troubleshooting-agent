"""
标准 Case / Trace Pydantic 模型。

CaseRecord    — 可沉淀、可检索、可复用的完整经验案例（对应需求中的 Case Schema）
TraceRecord   — 每次 Agent 排查的完整轨迹（对应需求中的 Trace Schema）
CandidateCase — 系统自动从 Trace 生成的候选经验（待工程师确认后进入正式知识库）
DeviceContext — 设备上下文（相机、镜头、算法版本等智能制造典型元数据）

设计原则：
- 所有字段均为 Optional，允许逐步填写，不阻断流程。
- 关键字段（symptom / user_query）为必填，保证可检索性。
- 对应的 DB ORM 模型见 app/persistence/models.py。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


# ────────────────────────────────────────────────────────────
# 辅助模型
# ────────────────────────────────────────────────────────────

class DeviceContext(BaseModel):
    """设备上下文 — 智能制造现场的典型设备元数据。"""
    camera_brand: Optional[str] = None        # 例: "Hikrobot"
    camera_model: Optional[str] = None        # 例: "MV-CA050-10GM"
    lens: Optional[str] = None                # 例: "25mm"
    light_type: Optional[str] = None          # 例: "ring_light"
    algorithm_version: Optional[str] = None  # 例: "defect_cls_v3.2"
    agent_version: Optional[str] = None      # 例: "agent_1.4.0"
    knowledge_pack_version: Optional[str] = None  # 例: "kp_2026_05"
    # 其他自定义字段
    extra: Optional[Dict[str, Any]] = None


class ToolCallRecord(BaseModel):
    """单次工具调用记录。"""
    tool: str
    input: Optional[Dict[str, Any]] = None
    output: Optional[Dict[str, Any]] = None
    ok: Optional[bool] = None
    summary: Optional[str] = None


class RetrievedCase(BaseModel):
    """检索命中的历史案例引用（在 P3 知识层接入后填充）。"""
    case_id: str
    score: Optional[float] = None   # 向量相似度
    reason: Optional[str] = None    # 检索命中原因


# ────────────────────────────────────────────────────────────
# TraceRecord — 每次 Agent 排查的完整轨迹
# ────────────────────────────────────────────────────────────

class TraceRecord(BaseModel):
    """
    Agent 排查完整轨迹。每次 agent.run() 完成后自动生成并持久化到 Postgres。

    轨迹内容：
      - 用户原始问题
      - Agent 判断的问题类型
      - 检索命中的历史案例（P3 后填充）
      - 调用了哪些工具、返回了什么
      - Agent 给出的建议
      - 工程师是否执行、执行是否有效（通过 feedback API 填入）
      - 最终结论和解决方案
    """
    trace_id: str = Field(
        default_factory=lambda: f"trace_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # ── 请求信息 ──
    site_id: Optional[str] = None          # 现场/客户标识
    user_query: str                         # 用户原始问题（必填）

    # ── Agent 输出 ──
    intent: Optional[str] = None           # 问题类型（camera_offline / ocr_quality_drop …）
    retrieved_cases: List[RetrievedCase] = Field(default_factory=list)
    tool_calls: List[ToolCallRecord] = Field(default_factory=list)
    agent_suggestion: Optional[str] = None  # 主要建议文本（从 final_answer.conclusion 提取）
    final_answer: Optional[Dict[str, Any]] = None  # 原始 JSON 答案
    elapsed_sec: Optional[float] = None

    # ── 工程师反馈（通过 /api/traces/{id}/feedback 填入）──
    engineer_action: Optional[str] = None  # 工程师实际采取的措施
    final_outcome: Optional[Literal["resolved", "unresolved", "partial", "pending"]] = "pending"
    feedback_at: Optional[datetime] = None
    human_verified: bool = False           # 工程师确认了根因和解决方案

    # ── 候选经验 ──
    candidate_generated: bool = False      # 是否已自动生成候选经验 JSON
    candidate_path: Optional[str] = None  # cases/pending/ 中的文件路径


# ────────────────────────────────────────────────────────────
# CaseRecord — 正式经验案例
# ────────────────────────────────────────────────────────────

class CaseRecord(BaseModel):
    """
    可沉淀、可检索、可复用的完整经验案例。

    生命周期：
      candidate（系统自动生成）
        → verified（工程师确认）
        → 进入知识库向量索引（P3）
        → 发布到各现场（P4 知识包）

    该模型对应 agent_cases Postgres 表，同时也是写入磁盘 JSON 的格式。
    """
    case_id: str = Field(
        default_factory=lambda: f"case_{datetime.now(timezone.utc).strftime('%Y%m%d')}_{uuid.uuid4().hex[:6]}"
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # ── 状态 ──
    case_status: Literal["candidate", "verified", "rejected"] = "candidate"

    # ── 来源 ──
    source_trace_id: Optional[str] = None   # 关联的排查轨迹 ID

    # ── 现场上下文 ──
    site_type: Optional[str] = None         # 例: "3C_assembly"
    station_type: Optional[str] = None      # 例: "appearance_inspection"
    product_type: Optional[str] = None      # 例: "metal_cover"

    # ── 核心字段（必填）──
    symptom: str                            # 症状描述（可检索）

    # ── 可选业务字段 ──
    alarm_code: Optional[str] = None        # 报警码（强关键词，用于混合检索）
    device_context: Optional[DeviceContext] = None

    # ── 证据链 ──
    evidence: List[str] = Field(default_factory=list)

    # ── 根因 & 解决方案 ──
    root_cause: Optional[str] = None
    solution: List[str] = Field(default_factory=list)
    verified_result: Optional[str] = None  # 处置后验证结果

    # ── 适用性标签（用于混合检索 filter） ──
    applicability: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)

    # ── 风险 & 安全 ──
    risk_level: Literal["low", "medium", "high"] = "medium"
    human_verified: bool = False
    sensitive_level: Literal["public", "anonymized", "internal", "confidential"] = "internal"

    # ── 知识包版本（P4 发布时打标） ──
    knowledge_pack_version: Optional[str] = None

    # ── 完整文档（供重新向量化使用，P3 接入后填充） ──
    full_doc: Optional[Dict[str, Any]] = None


# ────────────────────────────────────────────────────────────
# CandidateCase — 自动生成的候选经验（等待工程师筛选）
# ────────────────────────────────────────────────────────────

class CandidateCase(BaseModel):
    """
    从 TraceRecord 自动生成的候选经验 JSON。

    写入 cases/pending/{candidate_id}.json，工程师手动审核后：
      - 确认 → 拷贝到 cases/exported/，上传总部经验中台
      - 拒绝 → 直接删除
    候选经验不会自动进入正式知识库（需人工确认）。
    """
    candidate_id: str = Field(
        default_factory=lambda: f"cand_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    )
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # ── 来源 ──
    source_trace_id: str
    generation_reason: str = "auto"  # 触发原因，例: "engineer_resolved" / "repeated_query"

    # ── 候选内容（对应 CaseRecord 字段，待工程师填写/核实）──
    symptom: str
    alarm_code: Optional[str] = None
    site_type: Optional[str] = None
    station_type: Optional[str] = None
    product_type: Optional[str] = None
    device_context: Optional[DeviceContext] = None
    evidence: List[str] = Field(default_factory=list)
    root_cause: Optional[str] = None
    solution: List[str] = Field(default_factory=list)
    verified_result: Optional[str] = None
    applicability: List[str] = Field(default_factory=list)
    risk_level: Literal["low", "medium", "high"] = "medium"
    sensitive_level: Literal["public", "anonymized", "internal", "confidential"] = "internal"

    # ── 工程师反馈原文（供填写参考）──
    engineer_action_raw: Optional[str] = None
    final_outcome_raw: Optional[str] = None

    # ── 元数据 ──
    agent_version: Optional[str] = None
    user_query_raw: str = ""   # 原始问题（脱敏前，供工程师参考）

    # ── 脱敏提示（工程师操作指引）──
    desensitization_hint: str = (
        "请检查并脱敏以下字段后再上传：symptom / evidence / user_query_raw / device_context。"
        "删除客户名称、IP地址、具体产品型号等敏感信息。"
    )
