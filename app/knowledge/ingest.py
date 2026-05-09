"""
知识库导入 CLI。

用法：
    # 导入目录下所有支持格式的文件
    python -m app.knowledge.ingest ./docs/

    # 导入单文件
    python -m app.knowledge.ingest ./app/mock_data/runbook.json

    # 导入并指定文档类型
    python -m app.knowledge.ingest ./docs/sop/ --doc-type sop

    # 导入并指定知识包版本（用于 P4 版本管理）
    python -m app.knowledge.ingest ./knowledge_packs/kp_2026_05/ --kp-version kp_2026_05

支持的文件格式：
    .json    — runbook、案例、结构化知识（自动解析字段）
    .md      — SOP、维修手册、工艺文档（按 ## 标题分段）
    .txt     — 纯文本日志、报警说明
    .csv     — 质检数据、工艺参数表（每行一条记录）

导入流程：
    文件解析 → 分段/分块 → 生成 doc_id → 并行写入 Qdrant + PG
"""
from __future__ import annotations

import csv
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from app.observability.logging import get_logger, setup_logging

setup_logging(log_level="INFO", log_format="console")
log = get_logger(__name__)

# 支持的文件扩展名
SUPPORTED_EXT = {".json", ".md", ".txt", ".csv"}
# 单块最大字符数（超过则分块）
CHUNK_SIZE = 800
CHUNK_OVERLAP = 100


# ── 文档块数据结构 ─────────────────────────────────────────────
# {
#   "doc_id": str,       # sha256(content)[:16] + index
#   "title": str,
#   "content": str,
#   "source": str,       # 原始文件路径
#   "doc_type": str,     # runbook | sop | manual | case | log
#   "tags": list[str],
#   "alarm_code": str | None,
#   "device_model": str | None,
#   "product_type": str | None,
#   "knowledge_pack_version": str | None,
# }


def _make_doc_id(content: str, index: int = 0) -> str:
    h = hashlib.sha256(content.encode()).hexdigest()[:12]
    return f"doc_{h}_{index:04d}"


def _chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """按字符数分块，保留 overlap 避免语义截断。"""
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        start = end - overlap
    return chunks


# ── 各格式解析器 ────────────────────────────────────────────────

def parse_json(path: Path, doc_type: str, kp_version: Optional[str]) -> Iterator[Dict[str, Any]]:
    """
    解析 JSON 文件：
      - runbook.json：{issue_type: {title, steps, safe_actions}}
      - CaseRecord JSON：{symptom, root_cause, solution, ...}
      - 通用 JSON 列表：[{title, content, ...}, ...]
      - 通用 JSON 对象：{title, content, ...}
    """
    data = json.loads(path.read_text(encoding="utf-8"))

    # runbook 格式检测（顶层 key 下有 title + steps）
    if isinstance(data, dict):
        first_val = next(iter(data.values()), None)
        if isinstance(first_val, dict) and "steps" in first_val:
            # runbook 格式
            for issue_type, rb in data.items():
                title = rb.get("title", issue_type)
                steps_text = "\n".join(rb.get("steps", []))
                content = f"问题类型: {issue_type}\n{steps_text}"
                doc_id = _make_doc_id(content)
                yield {
                    "doc_id": doc_id,
                    "title": title,
                    "content": content,
                    "source": str(path),
                    "doc_type": "runbook",
                    "tags": [issue_type],
                    "alarm_code": None,
                    "device_model": None,
                    "product_type": None,
                    "knowledge_pack_version": kp_version,
                }
            return

        # CaseRecord 格式（有 symptom 字段）
        if "symptom" in data:
            content = (
                f"症状: {data.get('symptom', '')}\n"
                f"根因: {data.get('root_cause', '')}\n"
                f"解决方案: {'; '.join(data.get('solution', []))}\n"
                f"证据: {'; '.join(data.get('evidence', []))}"
            )
            doc_id = _make_doc_id(content)
            yield {
                "doc_id": doc_id,
                "title": data.get("symptom", "")[:80],
                "content": content,
                "source": str(path),
                "doc_type": "case",
                "tags": data.get("applicability", []) + data.get("tags", []),
                "alarm_code": data.get("alarm_code"),
                "device_model": (
                    data.get("device_context", {}).get("camera_model")
                    if isinstance(data.get("device_context"), dict) else None
                ),
                "product_type": data.get("product_type"),
                "knowledge_pack_version": kp_version,
            }
            return

        # 通用 dict：把整个 JSON 当一条文档
        content = json.dumps(data, ensure_ascii=False, indent=2)
        doc_id = _make_doc_id(content)
        yield {
            "doc_id": doc_id,
            "title": data.get("title", path.stem),
            "content": content,
            "source": str(path),
            "doc_type": doc_type,
            "tags": [],
            "alarm_code": None,
            "device_model": None,
            "product_type": None,
            "knowledge_pack_version": kp_version,
        }
        return

    # JSON 列表：每条一个文档
    if isinstance(data, list):
        for i, item in enumerate(data):
            if isinstance(item, dict):
                content = item.get("content") or json.dumps(item, ensure_ascii=False)
                doc_id = _make_doc_id(content, i)
                yield {
                    "doc_id": doc_id,
                    "title": item.get("title", f"{path.stem}_{i}"),
                    "content": content,
                    "source": str(path),
                    "doc_type": doc_type,
                    "tags": item.get("tags", []),
                    "alarm_code": item.get("alarm_code"),
                    "device_model": item.get("device_model"),
                    "product_type": item.get("product_type"),
                    "knowledge_pack_version": kp_version,
                }


def parse_markdown(path: Path, doc_type: str, kp_version: Optional[str]) -> Iterator[Dict[str, Any]]:
    """按 ## 标题分段，每段作为一条文档。"""
    text = path.read_text(encoding="utf-8")
    sections: List[tuple] = []   # [(title, content)]
    current_title = path.stem
    current_lines: List[str] = []

    for line in text.splitlines():
        if line.startswith("## "):
            if current_lines:
                sections.append((current_title, "\n".join(current_lines).strip()))
            current_title = line.lstrip("#").strip()
            current_lines = []
        else:
            current_lines.append(line)
    if current_lines:
        sections.append((current_title, "\n".join(current_lines).strip()))

    for i, (title, content) in enumerate(sections):
        for j, chunk in enumerate(_chunk_text(content)):
            if not chunk.strip():
                continue
            doc_id = _make_doc_id(chunk, i * 100 + j)
            yield {
                "doc_id": doc_id,
                "title": title if j == 0 else f"{title} (续{j})",
                "content": chunk,
                "source": str(path),
                "doc_type": doc_type,
                "tags": [],
                "alarm_code": None,
                "device_model": None,
                "product_type": None,
                "knowledge_pack_version": kp_version,
            }


def parse_txt(path: Path, doc_type: str, kp_version: Optional[str]) -> Iterator[Dict[str, Any]]:
    """纯文本：按段落分块。"""
    text = path.read_text(encoding="utf-8")
    for i, chunk in enumerate(_chunk_text(text)):
        if not chunk.strip():
            continue
        doc_id = _make_doc_id(chunk, i)
        yield {
            "doc_id": doc_id,
            "title": f"{path.stem} (块{i+1})",
            "content": chunk,
            "source": str(path),
            "doc_type": doc_type,
            "tags": [],
            "alarm_code": None,
            "device_model": None,
            "product_type": None,
            "knowledge_pack_version": kp_version,
        }


def parse_csv(path: Path, doc_type: str, kp_version: Optional[str]) -> Iterator[Dict[str, Any]]:
    """
    CSV：每行一条记录。
    期望列：title, content, tags, alarm_code, device_model（可选）。
    若无 content 列，则把所有列值拼接为 content。
    """
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            content = row.get("content") or " | ".join(
                f"{k}:{v}" for k, v in row.items() if v
            )
            if not content.strip():
                continue
            tags_raw = row.get("tags", "")
            tags = [t.strip() for t in tags_raw.split(",")] if tags_raw else []
            doc_id = _make_doc_id(content, i)
            yield {
                "doc_id": doc_id,
                "title": row.get("title", f"{path.stem}_row{i}"),
                "content": content,
                "source": str(path),
                "doc_type": doc_type,
                "tags": tags,
                "alarm_code": row.get("alarm_code"),
                "device_model": row.get("device_model"),
                "product_type": row.get("product_type"),
                "knowledge_pack_version": kp_version,
            }


def parse_file(
    path: Path, doc_type: str = "manual", kp_version: Optional[str] = None
) -> Iterator[Dict[str, Any]]:
    ext = path.suffix.lower()
    if ext == ".json":
        yield from parse_json(path, doc_type, kp_version)
    elif ext == ".md":
        yield from parse_markdown(path, doc_type, kp_version)
    elif ext == ".txt":
        yield from parse_txt(path, doc_type, kp_version)
    elif ext == ".csv":
        yield from parse_csv(path, doc_type, kp_version)
    else:
        log.warning("unsupported_file_ext", path=str(path), ext=ext)


# ── 主导入函数 ────────────────────────────────────────────────

def ingest(
    path: str | Path,
    doc_type: str = "manual",
    kp_version: Optional[str] = None,
    dry_run: bool = False,
) -> Dict[str, int]:
    """
    导入文件或目录到知识库（Qdrant + PG）。
    返回统计: {total, success_vec, success_kw, skipped, failed}
    """
    from app.knowledge.embedder import Embedder
    from app.knowledge.keyword_store import KeywordStore
    from app.knowledge.vector_store import VectorStore
    from app.persistence.db import init_db

    p = Path(path)
    files: List[Path] = []
    if p.is_dir():
        for ext in SUPPORTED_EXT:
            files.extend(p.rglob(f"*{ext}"))
    elif p.is_file():
        files = [p]
    else:
        log.error("path_not_found", path=str(p))
        return {}

    log.info("ingest_start", path=str(p), files=len(files), dry_run=dry_run)

    # 初始化存储（不可达时降级）
    if not dry_run:
        init_db()

    embedder = Embedder()
    vector_store = VectorStore()
    keyword_store = KeywordStore()

    if not dry_run:
        vec_ok = vector_store.ensure_collection()
        if vec_ok:
            log.info("qdrant_ready", collection=vector_store.collection)

    stats = {"total": 0, "success_vec": 0, "success_kw": 0, "skipped": 0, "failed": 0}

    for file_path in files:
        log.info("processing_file", file=str(file_path))
        try:
            docs = list(parse_file(file_path, doc_type=doc_type, kp_version=kp_version))
        except Exception as exc:
            log.error("parse_failed", file=str(file_path), error=str(exc))
            stats["failed"] += 1
            continue

        for doc in docs:
            stats["total"] += 1
            content = doc.get("content", "")
            if not content.strip():
                stats["skipped"] += 1
                continue

            if dry_run:
                print(f"  [DRY-RUN] doc_id={doc['doc_id']} title={doc['title'][:60]}")
                continue

            # 写入 Qdrant
            vec = embedder.embed(content)
            if vec:
                payload = {k: v for k, v in doc.items() if k != "content"}
                payload["content"] = content[:500]  # payload 只存摘要，节省内存
                ok = vector_store.upsert(doc["doc_id"], vec, payload)
                if ok:
                    stats["success_vec"] += 1
                else:
                    stats["failed"] += 1
            else:
                log.warning("embed_skipped", doc_id=doc["doc_id"])

            # 写入 PG（全文检索）
            kw_ok = keyword_store.upsert(doc)
            if kw_ok:
                stats["success_kw"] += 1

            time.sleep(0.05)   # 避免 Ollama 被打爆

    log.info("ingest_complete", **stats)
    return stats


# ── CLI 入口 ─────────────────────────────────────────────────

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(description="导入文档到知识库（Qdrant + PG）")
    parser.add_argument("path", help="文件或目录路径")
    parser.add_argument("--doc-type", default="manual",
                        choices=["runbook", "sop", "case", "manual", "log"],
                        help="文档类型（默认: manual）")
    parser.add_argument("--kp-version", default=None,
                        help="知识包版本标签，例: kp_2026_05")
    parser.add_argument("--dry-run", action="store_true",
                        help="仅解析，不写入存储")
    args = parser.parse_args()

    stats = ingest(
        path=args.path,
        doc_type=args.doc_type,
        kp_version=args.kp_version,
        dry_run=args.dry_run,
    )
    print(f"\n导入完成: {stats}")


if __name__ == "__main__":
    main()
