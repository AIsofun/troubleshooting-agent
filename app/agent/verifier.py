"""
AnswerVerifier — 在 Agent 给出最终答案后进行自我核验。

核验目标：
  1. 必填字段完整性 (intent / conclusion / evidence / suggestions)
  2. conclusion 质量（不是乱码、长度够、引用了工具数据）
  3. evidence 与 observations 的覆盖率（避免"空证据"的结论）
  4. 关键数值引用（conclusion 里应引用至少一个工具返回的具体数据）

核验结果：VerifyResult
  passed      : bool — 是否通过
  score       : float [0,1] — 综合评分
  issues      : List[str] — 失败原因列表
  replan_hint : str — 重规划时给 LLM 的补充提示（说明还缺什么）

Replan 触发条件（任一满足）：
  - 必填字段缺失
  - conclusion 长度 < MIN_CONCLUSION_LEN
  - evidence 为空（工具从未调用 or 全部失败）
  - score < PASS_THRESHOLD（综合评分不达标）

设计原则：
  - 纯函数，无 I/O，便于测试
  - 不依赖 LLM（确定性规则，零延迟）
  - 阈值全部可通过 config 覆盖（见 get_verifier()）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 从工具结果中提取的"具体数值"特征：数字 + 单位 / 百分号 / ms / fps / lag=...
_NUMERIC_RE = re.compile(
    r"\d+(?:\.\d+)?\s*(?:%|ms|fps|μs|MB|GB|msg/s|秒|分钟|min|次|条|台|帧|毫秒)"
    r"|lag=\d+"
    r"|p99=\d+"
    r"|status=\w+"
    r"|\d+"   # 任何独立数字也算
)

MIN_CONCLUSION_LEN = 20
PASS_THRESHOLD = 0.65          # 低于此分数触发重规划


@dataclass
class VerifyResult:
    passed: bool
    score: float                        # [0, 1]
    issues: List[str] = field(default_factory=list)
    replan_hint: str = ""               # 追加给 LLM 的重规划提示


class AnswerVerifier:
    """
    对 Agent 给出的最终答案进行确定性核验。

    参数：
        min_conclusion_len  : 最小结论长度
        pass_threshold      : 通过分数线
        require_numeric     : 是否要求结论中包含具体数值
        require_suggestions : 是否要求 suggestions 非空
    """

    def __init__(
        self,
        min_conclusion_len: int = MIN_CONCLUSION_LEN,
        pass_threshold: float = PASS_THRESHOLD,
        require_numeric: bool = True,
        require_suggestions: bool = False,
    ) -> None:
        self.min_conclusion_len = min_conclusion_len
        self.pass_threshold = pass_threshold
        self.require_numeric = require_numeric
        self.require_suggestions = require_suggestions

    def verify(
        self,
        answer: Dict[str, Any],
        observations: List[Dict[str, Any]],
    ) -> VerifyResult:
        """
        核验答案质量。

        answer       : LLM 给出的最终答案 dict
        observations : Agent 执行期间的全部工具调用及结果
        """
        issues: List[str] = []
        score_parts: Dict[str, float] = {}
        hard_fail = False  # 硬失败：不论综合分数如何都强制 passed=False

        # 非 dict 快速失败
        if not isinstance(answer, dict):
            return VerifyResult(
                passed=False, score=0.0,
                issues=["answer 不是合法 dict"],
                replan_hint="请确保最终答案为包含 intent/conclusion/evidence/suggestions 字段的 JSON 对象。",
            )

        # ── 1. 必填字段完整性 (权重 0.30) ─────────────────────
        required = {"intent": 0.08, "conclusion": 0.15, "evidence": 0.07}
        field_score = 0.0
        for fname, weight in required.items():
            val = answer.get(fname)
            if val:
                field_score += weight
            else:
                issues.append(f"缺少必填字段: {fname}")
        score_parts["fields"] = field_score / sum(required.values())

        # ── 2. conclusion 质量 (权重 0.30) ────────────────────
        conclusion = (answer.get("conclusion") or "").strip()
        c_score = 0.0
        if len(conclusion) >= self.min_conclusion_len:
            c_score += 0.5
        else:
            issues.append(
                f"conclusion 过短 ({len(conclusion)} 字 < {self.min_conclusion_len})"
            )
            hard_fail = True  # 结论太短必须重规划
        if self.require_numeric and _NUMERIC_RE.search(conclusion):
            c_score += 0.5
        elif self.require_numeric:
            issues.append("conclusion 未引用具体数值（数字+单位/lag/p99/status）")
        else:
            c_score += 0.5   # 不要求数值时直接给分
        score_parts["conclusion"] = c_score

        # ── 3. evidence 覆盖率 (权重 0.25) ────────────────────
        evidence = answer.get("evidence") or []
        obs_count = len(observations)
        if obs_count == 0:
            e_score = 0.0
            issues.append("无工具调用记录，evidence 无法验证")
        elif len(evidence) == 0:
            e_score = 0.0
            issues.append("evidence 为空（工具已调用但未引用结果）")
        else:
            # evidence 条数与实际工具调用数的覆盖比
            coverage = min(len(evidence) / max(obs_count, 1), 1.0)
            e_score = coverage
            if coverage < 0.5:
                issues.append(
                    f"evidence 覆盖率偏低 ({len(evidence)}/{obs_count} 工具结果)"
                )
        score_parts["evidence"] = e_score

        # ── 4. suggestions 完整性 (权重 0.15) ─────────────────
        suggestions = answer.get("suggestions") or []
        if self.require_suggestions and not suggestions:
            issues.append("suggestions 为空（缺少处置建议）")
            s_score = 0.0
            hard_fail = True
        else:
            s_score = 1.0 if suggestions else 0.5
        score_parts["suggestions"] = s_score

        # ── 综合评分（加权平均）────────────────────────────────
        weights = {"fields": 0.30, "conclusion": 0.30, "evidence": 0.25, "suggestions": 0.15}
        score = sum(score_parts[k] * weights[k] for k in weights)
        passed = (
            score >= self.pass_threshold
            and not hard_fail
            and not any("缺少必填字段" in i for i in issues)
        )

        # ── 构建重规划提示 ─────────────────────────────────────
        replan_hint = ""
        if not passed and issues:
            replan_hint = (
                "上一轮回答存在以下问题，请针对性地补充取证后重新给出结论：\n"
                + "\n".join(f"  - {i}" for i in issues)
            )

        return VerifyResult(
            passed=passed,
            score=round(score, 3),
            issues=issues,
            replan_hint=replan_hint,
        )


# ── 全局单例（从 config 初始化）────────────────────────────


_VERIFIER: Optional[AnswerVerifier] = None


def get_verifier() -> AnswerVerifier:
    """从 config 获取 AnswerVerifier 单例。"""
    global _VERIFIER
    if _VERIFIER is None:
        try:
            from app.config import get_settings
            cfg = get_settings().get("agent", {})
            _VERIFIER = AnswerVerifier(
                min_conclusion_len=cfg.get("verify_min_conclusion_len", MIN_CONCLUSION_LEN),
                pass_threshold=cfg.get("verify_pass_threshold", PASS_THRESHOLD),
                require_numeric=cfg.get("verify_require_numeric", True),
                require_suggestions=cfg.get("verify_require_suggestions", False),
            )
        except Exception:
            _VERIFIER = AnswerVerifier()
    return _VERIFIER
