"""
KeywordStore — PostgreSQL 全文检索（关键词检索层）。

为什么不能只用向量检索？
  工业场景中存在大量"强关键词"：
    - 型号：MV-CA050-10GM、qwen2.5:14b
    - 料号：PN-2024-XZ-001
    - 报警码：ALG_FALSE_REJECT_HIGH、E_TIMEOUT_0x03
    - 工艺参数：曝光8000、增益3.5
    - 设备编号：cam-02、station-A3
  这些字符串的语义相似度不够区分（"曝光8000"和"曝光12000"向量很近，
  但业务含义完全不同），必须用精确关键词匹配。

实现方案：
  - PG knowledge_docs 表，字段包含原始内容 + alarm_code + device_model 等强关键词列
  - 全文检索用 tsvector（支持中英文 unaccent 预处理）
  - LIKE 精确匹配强关键词列
  - 返回文档 ID 列表，与向量结果合并后一起 rerank

降级：PG 不可达时返回空列表，不阻断 Agent 流程。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from app.observability.logging import get_logger

log = get_logger(__name__)


# ── Alembic 迁移（Step 5）会建表，这里只定义 SQL 逻辑 ──────

_KEYWORD_SEARCH_SQL = """
SELECT
    doc_id,
    title,
    content,
    source,
    doc_type,
    tags,
    alarm_code,
    device_model,
    product_type,
    knowledge_pack_version,
    ts_rank_cd(search_vector, plainto_tsquery('simple', :query)) AS kw_score
FROM knowledge_docs
WHERE
    -- 全文检索（涵盖中英文词）
    search_vector @@ plainto_tsquery('simple', :query)
    -- 强关键词精确匹配（ILIKE 兜底，不依赖分词）
    OR alarm_code ILIKE :alarm_pattern
    OR device_model ILIKE :device_pattern
ORDER BY kw_score DESC
LIMIT :limit
"""

# 仅按强关键词列精确检索（alarm_code / device_model 必须存在时优先用这个）
_EXACT_KEYWORD_SQL = """
SELECT
    doc_id, title, content, source, doc_type, tags,
    alarm_code, device_model, product_type, knowledge_pack_version,
    1.0 AS kw_score
FROM knowledge_docs
WHERE
    alarm_code = :alarm_code
    OR device_model = :device_model
ORDER BY created_at DESC
LIMIT :limit
"""


class KeywordStore:
    """
    PostgreSQL 关键词检索封装。
    """

    def __init__(self):
        pass

    @staticmethod
    def _get_session():
        from app.persistence.db import get_session, is_db_available
        if not is_db_available():
            return None
        return get_session()

    def search(
        self,
        query: str,
        top_k: int = 20,
        alarm_code: Optional[str] = None,
        device_model: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        关键词检索。
        先做全文检索，再对 alarm_code / device_model 强关键词做精确 ILIKE。
        返回 [{doc_id, title, content, kw_score, ...}]。
        """
        from app.persistence.db import is_db_available
        if not is_db_available():
            return []

        try:
            from sqlalchemy import text
            from app.persistence.db import get_session

            # 构建 ILIKE pattern（%query%）
            esc_query = query.replace("%", "\\%").replace("_", "\\_")
            alarm_pattern = f"%{alarm_code or esc_query}%"
            device_pattern = f"%{device_model or esc_query}%"

            with get_session() as session:
                rows = session.execute(
                    text(_KEYWORD_SEARCH_SQL),
                    {
                        "query": query,
                        "alarm_pattern": alarm_pattern,
                        "device_pattern": device_pattern,
                        "limit": top_k,
                    },
                ).mappings().all()

            return [dict(r) for r in rows]

        except Exception as exc:
            log.warning("keyword_search_failed", error=str(exc))
            return []

    def exact_keyword_search(
        self,
        alarm_code: Optional[str] = None,
        device_model: Optional[str] = None,
        top_k: int = 5,
    ) -> List[Dict[str, Any]]:
        """
        强关键词精确检索（报警码 / 型号）。
        用于：检索到 alarm_code 时，优先拉取有该报警码的文档排在最前。
        """
        if not alarm_code and not device_model:
            return []
        from app.persistence.db import is_db_available
        if not is_db_available():
            return []
        try:
            from sqlalchemy import text
            from app.persistence.db import get_session

            with get_session() as session:
                rows = session.execute(
                    text(_EXACT_KEYWORD_SQL),
                    {
                        "alarm_code": alarm_code or "",
                        "device_model": device_model or "",
                        "limit": top_k,
                    },
                ).mappings().all()
            return [dict(r) for r in rows]
        except Exception as exc:
            log.warning("exact_keyword_search_failed", error=str(exc))
            return []

    def upsert(self, doc: Dict[str, Any]) -> bool:
        """
        插入或更新 knowledge_docs 表中的文档。
        doc 字段：doc_id, title, content, source, doc_type, tags,
                  alarm_code, device_model, product_type, knowledge_pack_version
        """
        from app.persistence.db import is_db_available
        if not is_db_available():
            return False
        try:
            from sqlalchemy import text
            from app.persistence.db import get_session

            sql = text("""
                INSERT INTO knowledge_docs
                    (doc_id, title, content, source, doc_type, tags,
                     alarm_code, device_model, product_type, knowledge_pack_version)
                VALUES
                    (:doc_id, :title, :content, :source, :doc_type, :tags::jsonb,
                     :alarm_code, :device_model, :product_type, :kp_version)
                ON CONFLICT (doc_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    content = EXCLUDED.content,
                    source = EXCLUDED.source,
                    doc_type = EXCLUDED.doc_type,
                    tags = EXCLUDED.tags,
                    alarm_code = EXCLUDED.alarm_code,
                    device_model = EXCLUDED.device_model,
                    product_type = EXCLUDED.product_type,
                    knowledge_pack_version = EXCLUDED.knowledge_pack_version,
                    updated_at = NOW(),
                    search_vector = to_tsvector('simple',
                        coalesce(:title,'') || ' ' || coalesce(:content,'') || ' ' ||
                        coalesce(:alarm_code,'') || ' ' || coalesce(:device_model,'')
                    )
            """)
            import json as _json
            with get_session() as session:
                session.execute(sql, {
                    "doc_id": doc["doc_id"],
                    "title": doc.get("title", ""),
                    "content": doc.get("content", ""),
                    "source": doc.get("source", ""),
                    "doc_type": doc.get("doc_type", "manual"),
                    "tags": _json.dumps(doc.get("tags", []), ensure_ascii=False),
                    "alarm_code": doc.get("alarm_code"),
                    "device_model": doc.get("device_model"),
                    "product_type": doc.get("product_type"),
                    "kp_version": doc.get("knowledge_pack_version"),
                })
            return True
        except Exception as exc:
            log.warning("keyword_upsert_failed", doc_id=doc.get("doc_id"), error=str(exc))
            return False
