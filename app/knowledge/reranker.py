"""
Reranker — 对混合检索结果做二次精排。

两种实现（自动选择）：
  1. FlagEmbedding BGE-Reranker（本地 GPU/CPU，最优精度）
       安装：pip install FlagEmbedding
       首次运行自动下载模型（~500MB）
  2. ScoreReranker（内置，零依赖，纯分数线性融合）
       作为降级方案，不需要额外安装

自动选择规则：
  - 优先尝试 BGE-Reranker（如已安装 FlagEmbedding）
  - 失败或未安装则使用 ScoreReranker

ScoreReranker 融合策略（RRF + 归一化）：
  final_score = α × vector_score_norm + β × keyword_score_norm + γ × exact_boost
  α=0.6  β=0.3  γ=0.1（exact_boost 对强关键词精确命中额外加分）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from app.observability.logging import get_logger

log = get_logger(__name__)

# ── 权重配置 ────────────────────────────────────────────────
_ALPHA = 0.6    # 向量分数权重
_BETA = 0.3     # 关键词分数权重
_GAMMA = 0.1    # 强关键词精确命中 boost


# ── 候选文档结构 ─────────────────────────────────────────────
# {
#   "doc_id": str,
#   "title": str,
#   "content": str,       # 原始文本（rerank 用）
#   "vector_score": float | None,
#   "kw_score": float | None,
#   "exact_hit": bool,    # alarm_code / device_model 精确命中
#   "payload": dict,      # Qdrant payload 或 PG 行
# }


class ScoreReranker:
    """
    内置分数融合 Reranker（无额外依赖）。
    使用 RRF（Reciprocal Rank Fusion）+ 归一化分数加权。
    """

    @staticmethod
    def rerank(
        candidates: List[Dict[str, Any]],
        query: str,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        if not candidates:
            return []

        # 归一化向量分数
        vec_scores = [c.get("vector_score") or 0.0 for c in candidates]
        kw_scores = [c.get("kw_score") or 0.0 for c in candidates]

        def _norm(scores: list) -> list:
            mx = max(scores) if scores else 1.0
            mn = min(scores) if scores else 0.0
            rng = mx - mn or 1.0
            return [(s - mn) / rng for s in scores]

        vec_norm = _norm(vec_scores)
        kw_norm = _norm(kw_scores)

        ranked = []
        for i, c in enumerate(candidates):
            exact_boost = float(c.get("exact_hit", False))
            score = (
                _ALPHA * vec_norm[i]
                + _BETA * kw_norm[i]
                + _GAMMA * exact_boost
            )
            ranked.append({**c, "rerank_score": round(score, 4)})

        ranked.sort(key=lambda x: x["rerank_score"], reverse=True)
        return ranked[:top_k]


class BGEReranker:
    """
    BGE Cross-Encoder Reranker（需要安装 FlagEmbedding）。
    首次实例化时加载模型（~500MB，之后缓存在内存）。
    """

    _model = None

    @classmethod
    def _load_model(cls):
        if cls._model is None:
            from FlagEmbedding import FlagReranker
            cls._model = FlagReranker(
                "BAAI/bge-reranker-base",
                use_fp16=True,   # 降低内存占用
            )
            log.info("bge_reranker_loaded")
        return cls._model

    def rerank(
        self,
        candidates: List[Dict[str, Any]],
        query: str,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        if not candidates:
            return []
        try:
            model = self._load_model()
            pairs = [(query, c.get("content", c.get("title", ""))) for c in candidates]
            scores = model.compute_score(pairs, normalize=True)
            if not isinstance(scores, list):
                scores = [scores]

            ranked = []
            for c, s in zip(candidates, scores):
                ranked.append({**c, "rerank_score": round(float(s), 4)})

            ranked.sort(key=lambda x: x["rerank_score"], reverse=True)
            return ranked[:top_k]
        except Exception as exc:
            log.warning("bge_reranker_failed", error=str(exc))
            # 降级到 ScoreReranker
            return ScoreReranker.rerank(candidates, query, top_k)


class Reranker:
    """
    统一 Reranker 接口。自动选择 BGE-Reranker（优先）或 ScoreReranker（降级）。
    """

    def __init__(self, prefer_bge: bool = False):
        """
        prefer_bge: 设为 True 时主动尝试加载 BGE 模型（需已安装 FlagEmbedding）。
        默认使用 ScoreReranker（零依赖，适合生产低延迟场景）。
        """
        self._backend: Optional[BGEReranker | ScoreReranker] = None
        if prefer_bge:
            try:
                import FlagEmbedding  # noqa: F401
                self._backend = BGEReranker()
                log.info("reranker_backend", backend="BGE-Reranker")
            except ImportError:
                log.info("reranker_backend", backend="ScoreReranker (FlagEmbedding not installed)")
                self._backend = ScoreReranker()
        else:
            self._backend = ScoreReranker()
            log.info("reranker_backend", backend="ScoreReranker")

    def rerank(
        self,
        candidates: List[Dict[str, Any]],
        query: str,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        return self._backend.rerank(candidates, query, top_k)
