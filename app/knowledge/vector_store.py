"""
VectorStore — Qdrant 向量存储封装。

集合设计：
  knowledge  — 企业知识文档（runbook / SOP / 维修手册 / 工艺参数）
  cases      — 已验证的经验案例（由 P4 知识回流写入）

每条文档的 payload（Qdrant JSON 字段，用于 keyword filter + rerank）：
  {
    "doc_id":      "唯一 ID（与 PG knowledge_docs 对应）",
    "title":       "文档标题",
    "content":     "原始文本（用于 rerank 计算相关度）",
    "source":      "来源文件路径 / 知识包版本",
    "doc_type":    "runbook | sop | case | manual | log",
    "tags":        ["camera", "高反光", "曝光"],
    "alarm_code":  "ALG_FALSE_REJECT_HIGH",  # 强关键词
    "device_model": "MV-CA050-10GM",          # 强关键词
    "product_type": "metal_cover",
    "knowledge_pack_version": "kp_2026_05",
  }
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from app.observability.logging import get_logger

log = get_logger(__name__)

# Qdrant collection 名称
COLLECTION_KNOWLEDGE = "knowledge"
COLLECTION_CASES = "cases"


class VectorStore:
    """
    Qdrant 向量存储操作封装。
    不可达时方法返回空列表或 False，不阻断 Agent 流程。
    """

    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        collection: str = COLLECTION_KNOWLEDGE,
        dimension: int = 1024,
    ):
        from app.config import get_settings
        cfg = get_settings().get("qdrant", {})
        self.host = host or cfg.get("host", "qdrant")
        self.port = int(port or cfg.get("port", 6333))
        self.collection = collection
        self.dimension = dimension
        self._client = None

    def _get_client(self):
        if self._client is None:
            from qdrant_client import QdrantClient
            self._client = QdrantClient(host=self.host, port=self.port, timeout=10)
        return self._client

    # ── Collection 管理 ──────────────────────────────────

    def ensure_collection(self) -> bool:
        """
        确保 collection 存在。不存在则创建。
        返回 True 成功，False 失败（Qdrant 不可达）。
        """
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.http.models import Distance, VectorParams

            client = self._get_client()
            existing = [c.name for c in client.get_collections().collections]
            if self.collection not in existing:
                client.create_collection(
                    collection_name=self.collection,
                    vectors_config=VectorParams(
                        size=self.dimension,
                        distance=Distance.COSINE,
                    ),
                )
                log.info("qdrant_collection_created", collection=self.collection)
            return True
        except Exception as exc:
            log.warning("qdrant_unavailable", error=str(exc))
            return False

    def is_available(self) -> bool:
        try:
            self._get_client().get_collections()
            return True
        except Exception:
            return False

    # ── 写入 ──────────────────────────────────────────────

    def upsert(
        self,
        doc_id: str,
        vector: List[float],
        payload: Dict[str, Any],
    ) -> bool:
        """插入或更新单条文档向量。"""
        try:
            from qdrant_client.http.models import PointStruct

            client = self._get_client()
            # 使用 doc_id 的 UUID5 作为 Qdrant point ID（确保幂等）
            point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, doc_id))
            client.upsert(
                collection_name=self.collection,
                points=[PointStruct(id=point_id, vector=vector, payload=payload)],
            )
            return True
        except Exception as exc:
            log.warning("qdrant_upsert_failed", doc_id=doc_id, error=str(exc))
            return False

    def upsert_batch(
        self,
        records: List[Dict[str, Any]],  # [{doc_id, vector, payload}, ...]
    ) -> int:
        """批量写入，返回成功条数。"""
        try:
            from qdrant_client.http.models import PointStruct

            client = self._get_client()
            points = []
            for r in records:
                point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, r["doc_id"]))
                points.append(PointStruct(
                    id=point_id,
                    vector=r["vector"],
                    payload=r["payload"],
                ))
            client.upsert(collection_name=self.collection, points=points)
            return len(points)
        except Exception as exc:
            log.warning("qdrant_batch_upsert_failed", error=str(exc))
            return 0

    # ── 检索 ──────────────────────────────────────────────

    def search(
        self,
        query_vector: List[float],
        top_k: int = 20,
        score_threshold: float = 0.3,
        filter_conditions: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """
        向量相似度检索。
        返回 [{doc_id, score, payload}, ...]，按 score 降序。

        filter_conditions 示例（精确过滤强关键词）：
          {"alarm_code": "ALG_FALSE_REJECT_HIGH"}
          {"doc_type": "runbook"}
          {"tags": "camera"}
        """
        try:
            from qdrant_client.http.models import Filter, FieldCondition, MatchValue

            client = self._get_client()

            qdrant_filter = None
            if filter_conditions:
                must = []
                for field, value in filter_conditions.items():
                    must.append(FieldCondition(
                        key=field,
                        match=MatchValue(value=value),
                    ))
                qdrant_filter = Filter(must=must)

            results = client.search(
                collection_name=self.collection,
                query_vector=query_vector,
                limit=top_k,
                score_threshold=score_threshold,
                with_payload=True,
                query_filter=qdrant_filter,
            )

            return [
                {
                    "doc_id": r.payload.get("doc_id", str(r.id)),
                    "score": round(r.score, 4),
                    "payload": r.payload,
                }
                for r in results
            ]
        except Exception as exc:
            log.warning("qdrant_search_failed", error=str(exc))
            return []

    def delete(self, doc_id: str) -> bool:
        """按 doc_id 删除向量（用于知识包更新时去旧版本）。"""
        try:
            from qdrant_client.http.models import PointIdsList
            point_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, doc_id))
            self._get_client().delete(
                collection_name=self.collection,
                points_selector=PointIdsList(points=[point_id]),
            )
            return True
        except Exception as exc:
            log.warning("qdrant_delete_failed", doc_id=doc_id, error=str(exc))
            return False

    def count(self) -> int:
        """返回 collection 中的文档数量。"""
        try:
            return self._get_client().count(
                collection_name=self.collection, exact=True
            ).count
        except Exception:
            return -1
