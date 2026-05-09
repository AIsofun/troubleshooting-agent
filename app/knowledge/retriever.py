"""
HybridRetriever — 向量检索 + 关键词检索 + Rerank 的统一入口。

检索流程（三层）：

  ┌───────────────────────────────────────────────────────┐
  │ 1. 强关键词精确检索（alarm_code / device_model）        │
  │    → 命中则 exact_hit=True，后续 rerank 优先排名        │
  └──────────────────────────────┬────────────────────────┘
                                 │
  ┌──────────────────────────────▼────────────────────────┐
  │ 2. 并行检索（同时执行，合并去重）                       │
  │    2a. Qdrant 向量检索（top_k=20）                     │
  │    2b. PG 全文 + ILIKE 关键词检索（top_k=20）           │
  └──────────────────────────────┬────────────────────────┘
                                 │
  ┌──────────────────────────────▼────────────────────────┐
  │ 3. Rerank（ScoreReranker / BGE-Reranker）              │
  │    → 返回 top_k=5 最终结果                              │
  └───────────────────────────────────────────────────────┘

降级策略：
  - Qdrant 不可达 → 仅用关键词检索
  - PG 不可达    → 仅用向量检索
  - 两者均不可达 → 返回空列表（原有 query_runbook 工具仍兜底）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.knowledge.embedder import Embedder
from app.knowledge.keyword_store import KeywordStore
from app.knowledge.reranker import Reranker
from app.knowledge.vector_store import VectorStore
from app.observability.logging import get_logger

log = get_logger(__name__)


class HybridRetriever:
    """
    混合检索器（向量 + 关键词 + Rerank）。
    适合智能制造场景：型号/报警码用关键词精确命中，
    语义相似度用向量兜底，最终用 rerank 综合排序。
    """

    def __init__(
        self,
        embedder: Optional[Embedder] = None,
        vector_store: Optional[VectorStore] = None,
        keyword_store: Optional[KeywordStore] = None,
        reranker: Optional[Reranker] = None,
        retrieval_top_k: int = 20,
        rerank_top_k: int = 5,
    ):
        from app.config import get_settings
        cfg = get_settings().get("knowledge", {})

        self.embedder = embedder or Embedder()
        self.vector_store = vector_store or VectorStore()
        self.keyword_store = keyword_store or KeywordStore()
        self.reranker = reranker or Reranker()
        self.retrieval_top_k = int(cfg.get("retrieval_top_k", retrieval_top_k))
        self.rerank_top_k = int(cfg.get("rerank_top_k", rerank_top_k))

    def search(
        self,
        query: str,
        top_k: Optional[int] = None,
        alarm_code: Optional[str] = None,
        device_model: Optional[str] = None,
        doc_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        混合检索入口。

        参数：
          query:        自然语言查询
          top_k:        最终返回条数（默认取 config.knowledge.rerank_top_k）
          alarm_code:   报警码（强关键词，精确匹配）
          device_model: 设备型号（强关键词，精确匹配）
          doc_type:     文档类型过滤（"runbook" | "sop" | "case" | "manual"）

        返回：
          [{
            "doc_id", "title", "content", "source", "doc_type",
            "tags", "alarm_code", "device_model",
            "vector_score", "kw_score", "exact_hit",
            "rerank_score",   ← 综合排序分数
          }, ...]
        """
        final_top_k = top_k or self.rerank_top_k
        candidates: Dict[str, Dict[str, Any]] = {}  # doc_id → candidate

        # ── 1. 强关键词精确检索 ──────────────────────────
        if alarm_code or device_model:
            exact_results = self.keyword_store.exact_keyword_search(
                alarm_code=alarm_code,
                device_model=device_model,
                top_k=self.retrieval_top_k,
            )
            for r in exact_results:
                doc_id = r["doc_id"]
                candidates[doc_id] = {
                    "doc_id": doc_id,
                    "title": r.get("title", ""),
                    "content": r.get("content", ""),
                    "source": r.get("source", ""),
                    "doc_type": r.get("doc_type", ""),
                    "tags": r.get("tags", []),
                    "alarm_code": r.get("alarm_code"),
                    "device_model": r.get("device_model"),
                    "product_type": r.get("product_type"),
                    "vector_score": 0.0,
                    "kw_score": float(r.get("kw_score", 1.0)),
                    "exact_hit": True,
                    "payload": r,
                }
            if exact_results:
                log.info(
                    "exact_keyword_hit",
                    alarm_code=alarm_code,
                    device_model=device_model,
                    count=len(exact_results),
                )

        # ── 2a. 向量检索 ─────────────────────────────────
        query_vector = self.embedder.embed(query)
        if query_vector:
            filter_cond = {}
            if doc_type:
                filter_cond["doc_type"] = doc_type
            vec_results = self.vector_store.search(
                query_vector=query_vector,
                top_k=self.retrieval_top_k,
                score_threshold=0.3,
                filter_conditions=filter_cond or None,
            )
            for r in vec_results:
                doc_id = r["doc_id"]
                payload = r.get("payload", {})
                if doc_id in candidates:
                    candidates[doc_id]["vector_score"] = r["score"]
                else:
                    candidates[doc_id] = {
                        "doc_id": doc_id,
                        "title": payload.get("title", ""),
                        "content": payload.get("content", ""),
                        "source": payload.get("source", ""),
                        "doc_type": payload.get("doc_type", ""),
                        "tags": payload.get("tags", []),
                        "alarm_code": payload.get("alarm_code"),
                        "device_model": payload.get("device_model"),
                        "product_type": payload.get("product_type"),
                        "vector_score": r["score"],
                        "kw_score": 0.0,
                        "exact_hit": False,
                        "payload": payload,
                    }
        else:
            log.warning("embedding_unavailable", msg="Falling back to keyword-only search")

        # ── 2b. 关键词全文检索 ────────────────────────────
        kw_results = self.keyword_store.search(
            query=query,
            top_k=self.retrieval_top_k,
            alarm_code=alarm_code,
            device_model=device_model,
        )
        for r in kw_results:
            doc_id = r["doc_id"]
            if doc_id in candidates:
                candidates[doc_id]["kw_score"] = max(
                    candidates[doc_id]["kw_score"],
                    float(r.get("kw_score", 0.0)),
                )
            else:
                candidates[doc_id] = {
                    "doc_id": doc_id,
                    "title": r.get("title", ""),
                    "content": r.get("content", ""),
                    "source": r.get("source", ""),
                    "doc_type": r.get("doc_type", ""),
                    "tags": r.get("tags", []),
                    "alarm_code": r.get("alarm_code"),
                    "device_model": r.get("device_model"),
                    "product_type": r.get("product_type"),
                    "vector_score": 0.0,
                    "kw_score": float(r.get("kw_score", 0.0)),
                    "exact_hit": False,
                    "payload": r,
                }

        if not candidates:
            log.info("hybrid_retrieval_no_results", query=query[:80])
            return []

        # ── 3. Rerank ────────────────────────────────────
        candidate_list = list(candidates.values())
        ranked = self.reranker.rerank(candidate_list, query, top_k=final_top_k)

        log.info(
            "hybrid_retrieval_complete",
            query=query[:80],
            total_candidates=len(candidate_list),
            returned=len(ranked),
        )
        return ranked

    def format_for_llm(self, results: List[Dict[str, Any]], max_chars: int = 2000) -> str:
        """
        将检索结果格式化为 LLM 可读的字符串（注入 prompt 用）。
        限制总字符数避免 context 爆炸。
        """
        if not results:
            return "（知识库中未找到相关内容）"

        lines = ["【检索到的相关知识】"]
        total_chars = len(lines[0])

        for i, r in enumerate(results, 1):
            title = r.get("title", "无标题")
            content = r.get("content", "")
            source = r.get("source", "")
            score = r.get("rerank_score", r.get("vector_score", 0))
            alarm = r.get("alarm_code", "")
            exact = "⭐精确命中" if r.get("exact_hit") else ""

            snippet = content[:300] if content else ""
            entry = (
                f"\n[{i}] {title} {exact} (相关度:{score:.2f})"
                + (f" | 报警码:{alarm}" if alarm else "")
                + (f"\n来源: {source}" if source else "")
                + f"\n{snippet}"
            )
            if total_chars + len(entry) > max_chars:
                lines.append(f"\n... (还有 {len(results) - i + 1} 条结果未显示)")
                break
            lines.append(entry)
            total_chars += len(entry)

        return "\n".join(lines)
