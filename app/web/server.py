"""
运维助手 Agent — FastAPI Web 服务。

端点：
  GET  /                              Web UI
  GET  /health                        健康检查
  POST /api/run                       执行一次 Agent 排查（返回 trace_id）
  GET  /api/traces                    列出历史排查记录（分页）
  GET  /api/traces/{trace_id}         查询单条排查记录详情
  POST /api/traces/{trace_id}/feedback  工程师反馈（触发候选经验生成）
  GET  /api/candidates                列出 cases/pending/ 中的候选经验文件
  GET  /api/cases                     列出正式案例库
  POST /api/cases/promote/{id}        候选经验晚升
  POST /api/cases/reject/{id}         拒绝候选经验

MCP 端点（Phase 6）：
  POST /mcp/v1                        MCP JSON-RPC 2.0 主入口
  GET  /mcp/v1/tools                  列出所有工具（REST）
  POST /mcp/v1/tools/{tool_name}      调用指定工具（REST）

数据库初始化采用 lifespan，不可用时降级运行（Agent 功能不受影响）。
"""
from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.agent.core import Agent
from app.cases.candidate import CandidateEngine
from app.cases.schema import ToolCallRecord, TraceRecord
from app.config import get_llm, get_settings
from app.observability.logging import get_logger, setup_logging

_s = get_settings()
setup_logging(
    log_level=_s.get("obs", {}).get("log_level", "INFO"),
    log_format=_s.get("obs", {}).get("log_format", "json"),
)
log = get_logger(__name__)

# ── 全局单例（避免每次请求重建）──────────────────────────
_candidate_engine = CandidateEngine()


# ── Lifespan：启动时初始化 DB（不可用则降级）───────────
@asynccontextmanager
async def _lifespan(application: FastAPI):
    from app.persistence.db import init_db
    init_db()   # 连接失败时打 warning，不抛异常
    # Phase 6: 初始化 MCP adapter（连接配置的远程 MCP 服务器）
    from app.mcp.adapter import init_adapter_from_config
    init_adapter_from_config()
    yield       # 应用运行中
    # shutdown 时无需额外清理


app = FastAPI(title="运维助手 Agent", lifespan=_lifespan)

# ── Mount MCP router (Phase 6) ─────────────────────
from app.mcp.server import mcp_router
app.include_router(mcp_router, prefix="/mcp")


# ── Request / Response Models ──────────────────────────
class Query(BaseModel):
    query: str
    site_id: Optional[str] = None   # 可选：现场标识


class FeedbackRequest(BaseModel):
    engineer_action: Optional[str] = None   # 工程师实际采取的措施
    final_outcome: str = "resolved"         # resolved | unresolved | partial | pending
    human_verified: bool = True


# ── 辅助函数 ─────────────────────────────────────────

def _build_trace_record(
    trace_id: str,
    query: str,
    res,
    elapsed: float,
    site_id: Optional[str] = None,
) -> TraceRecord:
    """从 AgentResult 构建 TraceRecord Pydantic 对象。"""
    tool_calls: List[ToolCallRecord] = []
    for ev in res.trace:
        if ev.kind == "tool_call":
            # 找到对应的 tool_result 事件来填充 output/summary
            tool_name = ev.payload.get("tool", "")
            tool_args = ev.payload.get("args", {})
            # 向后找匹配的 tool_result
            result_payload = {}
            for rev in res.trace:
                if rev.kind == "tool_result" and rev.payload.get("tool") == tool_name:
                    result_payload = rev.payload
                    break
            tool_calls.append(ToolCallRecord(
                tool=tool_name,
                input=tool_args,
                output=result_payload.get("data"),
                ok=result_payload.get("ok"),
                summary=result_payload.get("summary"),
            ))

    answer = res.answer or {}
    return TraceRecord(
        trace_id=trace_id,
        site_id=site_id,
        user_query=query,
        intent=answer.get("intent"),
        tool_calls=tool_calls,
        agent_suggestion=answer.get("conclusion"),
        final_answer=answer,
        elapsed_sec=elapsed,
    )


def _try_save_trace(trace_record: TraceRecord) -> bool:
    """持久化 trace 到 Postgres（不可用时静默降级）。"""
    from app.persistence.db import is_db_available, get_session
    from app.persistence.trace_repo import TraceRepository

    if not is_db_available():
        return False
    try:
        with get_session() as session:
            TraceRepository.save(session, trace_record)
        return True
    except Exception as exc:
        log.warning("trace_save_failed", error=str(exc), trace_id=trace_record.trace_id)
        return False


# ── API 端点 ─────────────────────────────────────────

@app.get("/health")
def health() -> dict:
    from app.persistence.db import is_db_available
    return {
        "status": "ok",
        "db": "connected" if is_db_available() else "unavailable",
    }


@app.post("/api/run")
def run(q: Query):
    """
    执行一次 Agent 排查。
    返回 trace_id 供后续 feedback 接口使用。
    """
    import uuid as _uuid
    from datetime import datetime, timezone

    t0 = time.time()
    trace_id = f"trace_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{_uuid.uuid4().hex[:6]}"

    agent = Agent(llm=get_llm())
    res = agent.run(q.query)
    elapsed = round(time.time() - t0, 2)

    # 构建并持久化 TraceRecord
    trace_record = _build_trace_record(trace_id, q.query, res, elapsed, q.site_id)
    saved = _try_save_trace(trace_record)

    log.info(
        "agent_run_complete",
        trace_id=trace_id,
        intent=trace_record.intent,
        elapsed_sec=elapsed,
        persisted=saved,
    )

    return {
        "trace_id": trace_id,
        "answer": res.answer,
        "trace": [asdict(ev) for ev in res.trace],
        "elapsed_sec": elapsed,
        "persisted": saved,
    }


@app.get("/api/traces")
def list_traces(
    limit: int = 20,
    offset: int = 0,
    intent: Optional[str] = None,
    outcome: Optional[str] = None,
):
    """列出历史排查记录（分页 + 可按 intent / outcome 过滤）。"""
    from app.persistence.db import is_db_available, get_session
    from app.persistence.trace_repo import TraceRepository

    if not is_db_available():
        raise HTTPException(503, detail="数据库不可用，持久化功能已降级")

    with get_session() as session:
        rows = TraceRepository.list(session, limit=limit, offset=offset,
                                    intent=intent, outcome=outcome)
    return {"items": rows, "limit": limit, "offset": offset}


@app.get("/api/traces/{trace_id}")
def get_trace(trace_id: str):
    """查询单条排查记录详情。"""
    from app.persistence.db import is_db_available, get_session
    from app.persistence.trace_repo import TraceRepository

    if not is_db_available():
        raise HTTPException(503, detail="数据库不可用，持久化功能已降级")

    with get_session() as session:
        row = TraceRepository.get(session, trace_id)

    if row is None:
        raise HTTPException(404, detail=f"trace_id={trace_id} 不存在")
    return row


@app.post("/api/traces/{trace_id}/feedback")
def submit_feedback(trace_id: str, feedback: FeedbackRequest):
    """
    工程师提交排查反馈。
    final_outcome=resolved 时自动触发候选经验生成并写入 cases/pending/。
    """
    from app.persistence.db import is_db_available, get_session
    from app.persistence.trace_repo import TraceRepository

    if not is_db_available():
        raise HTTPException(503, detail="数据库不可用，持久化功能已降级")

    with get_session() as session:
        ok = TraceRepository.update_feedback(
            session,
            trace_id=trace_id,
            engineer_action=feedback.engineer_action,
            final_outcome=feedback.final_outcome,
            human_verified=feedback.human_verified,
        )
        if not ok:
            raise HTTPException(404, detail=f"trace_id={trace_id} 不存在")

        # 构建 TraceRecord 用于候选生成
        trace_record = TraceRepository.get_as_trace_record(session, trace_id)

    candidate_path: Optional[str] = None
    if trace_record and _candidate_engine.should_generate(trace_record):
        try:
            path = _candidate_engine.run(trace_record)
            if path:
                candidate_path = str(path)
                # 回写候选路径到 DB
                with get_session() as session:
                    TraceRepository.mark_candidate_generated(session, trace_id, candidate_path)
        except Exception as exc:
            log.warning("candidate_gen_failed", trace_id=trace_id, error=str(exc))

    log.info(
        "feedback_submitted",
        trace_id=trace_id,
        outcome=feedback.final_outcome,
        candidate_generated=candidate_path is not None,
    )
    return {
        "trace_id": trace_id,
        "final_outcome": feedback.final_outcome,
        "human_verified": feedback.human_verified,
        "candidate_generated": candidate_path is not None,
        "candidate_path": candidate_path,
    }


@app.get("/api/candidates")
def list_candidates():
    """列出 cases/pending/ 中的候选经验文件（供工程师审核）。"""
    items = []
    for p in sorted(_candidate_engine.pending_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            items.append({
                "filename": p.name,
                "candidate_id": data.get("candidate_id"),
                "generated_at": data.get("generated_at"),
                "source_trace_id": data.get("source_trace_id"),
                "symptom": data.get("symptom", "")[:120],
                "generation_reason": data.get("generation_reason"),
            })
        except Exception:
            items.append({"filename": p.name, "error": "无法解析"})
    return {"items": items, "total": len(items)}


# ── Phase 4: 知识回流端点 ─────────────────────────────────


class PromoteRequest(BaseModel):
    """候选经验晋升请求（可携带工程师补充的修订字段）。"""
    root_cause: Optional[str] = None
    solution: Optional[List[str]] = None
    verified_result: Optional[str] = None
    knowledge_pack_version: Optional[str] = None


@app.get("/api/cases")
def list_cases(
    limit: int = 20,
    offset: int = 0,
    case_status: Optional[str] = "verified",
    alarm_code: Optional[str] = None,
    site_type: Optional[str] = None,
):
    """
    列出正式案例库（来自 agent_cases 表）。
    默认只返回 status=verified 的案例，可通过 case_status= 参数过滤。
    """
    from app.persistence.db import is_db_available, get_session
    from app.cases.case_repo import CaseRepository

    if not is_db_available():
        raise HTTPException(503, detail="数据库不可用，案例库查询已降级")

    with get_session() as session:
        rows = CaseRepository.list(
            session,
            limit=limit,
            offset=offset,
            case_status=case_status,
            alarm_code=alarm_code,
            site_type=site_type,
        )
    return {"items": rows, "total": len(rows), "limit": limit, "offset": offset}


@app.post("/api/cases/promote/{candidate_id}")
def promote_candidate(candidate_id: str, req: PromoteRequest = PromoteRequest()):
    """
    将 cases/pending/{candidate_id}.json 晋升为正式经验案例。

    流程：
      1. 读取磁盘候选文件
      2. 转换为 CaseRecord（human_verified=True）
      3. 工程师补充字段覆盖
      4. 持久化到 agent_cases（DB 可用时）
      5. 同步写入知识库向量/关键词索引（Qdrant + PG，可降级）
      6. 将文件移至 cases/verified/
    """
    from app.cases.case_repo import CaseRepository

    # 定位候选文件
    pending_dir = _candidate_engine.pending_dir
    candidate_path = pending_dir / f"{candidate_id}.json"
    if not candidate_path.exists():
        # 兼容带 cand_ 前缀和不带的情况
        alt = pending_dir / candidate_id if not candidate_id.endswith(".json") else None
        if alt and alt.with_suffix(".json").exists():
            candidate_path = alt.with_suffix(".json")
        else:
            raise HTTPException(404, detail=f"候选文件不存在: {candidate_id}.json")

    # 转换为 CaseRecord
    try:
        case_record = CaseRepository.from_candidate_file(candidate_path)
    except Exception as exc:
        raise HTTPException(422, detail=f"候选文件解析失败: {exc}")

    # 工程师补充字段覆盖
    if req.root_cause is not None:
        case_record.root_cause = req.root_cause
    if req.solution is not None:
        case_record.solution = req.solution
    if req.verified_result is not None:
        case_record.verified_result = req.verified_result
    if req.knowledge_pack_version is not None:
        case_record.knowledge_pack_version = req.knowledge_pack_version

    # 持久化到 DB（可降级）
    saved_to_db = False
    from app.persistence.db import is_db_available, get_session
    if is_db_available():
        try:
            with get_session() as session:
                CaseRepository.save(session, case_record)
            saved_to_db = True
        except Exception as exc:
            log.warning("case_save_failed", case_id=case_record.case_id, error=str(exc))

    # 写入知识库索引（向量 + 关键词，可降级）
    ingested = _try_ingest_case(case_record)

    # 移动文件到 verified/
    verified_dir = _candidate_engine.cases_dir / "verified"
    verified_dir.mkdir(parents=True, exist_ok=True)
    dest = verified_dir / candidate_path.name
    candidate_path.rename(dest)

    log.info(
        "case_promoted",
        case_id=case_record.case_id,
        candidate_id=candidate_id,
        saved_to_db=saved_to_db,
        ingested=ingested,
    )
    return {
        "case_id": case_record.case_id,
        "symptom": case_record.symptom,
        "saved_to_db": saved_to_db,
        "ingested_to_kb": ingested,
        "verified_path": str(dest),
    }


@app.post("/api/cases/reject/{candidate_id}")
def reject_candidate(candidate_id: str):
    """
    拒绝候选经验：将文件从 cases/pending/ 移至 cases/rejected/。
    不写入 DB，不触发知识库更新。
    """
    pending_dir = _candidate_engine.pending_dir
    candidate_path = pending_dir / f"{candidate_id}.json"
    if not candidate_path.exists():
        raise HTTPException(404, detail=f"候选文件不存在: {candidate_id}.json")

    rejected_dir = _candidate_engine.cases_dir / "rejected"
    rejected_dir.mkdir(parents=True, exist_ok=True)
    dest = rejected_dir / candidate_path.name
    candidate_path.rename(dest)

    log.info("case_rejected", candidate_id=candidate_id)
    return {"candidate_id": candidate_id, "rejected_path": str(dest)}


def _try_ingest_case(case_record) -> bool:
    """
    将 CaseRecord 同步写入 Qdrant 向量索引 + PG 关键词索引。
    任何错误均静默降级，返回 False。
    """
    try:
        from app.knowledge.embedder import get_embedder
        from app.knowledge.vector_store import VectorStore
        from app.knowledge.keyword_store import KeywordStore

        # 构建用于向量化的文本
        parts = [case_record.symptom]
        if case_record.root_cause:
            parts.append(case_record.root_cause)
        if case_record.solution:
            parts.extend(case_record.solution)
        doc_text = "\n".join(parts)

        embedder = get_embedder()
        vector = embedder.embed(doc_text)
        if vector is None:
            return False   # Ollama 不可用

        payload = {
            "title": case_record.symptom[:80],
            "content": doc_text[:1000],
            "doc_type": "case",
            "alarm_code": case_record.alarm_code,
            "device_model": (
                case_record.device_context.camera_model
                if case_record.device_context else None
            ),
            "source": f"case:{case_record.case_id}",
            "tags": case_record.tags,
            "knowledge_pack_version": case_record.knowledge_pack_version,
        }

        vs = VectorStore()
        vs.upsert(case_record.case_id, vector, payload)

        ks = KeywordStore()
        ks.upsert({
            "doc_id": case_record.case_id,
            "title": case_record.symptom[:80],
            "content": doc_text,
            "doc_type": "case",
            "alarm_code": case_record.alarm_code,
            "device_model": payload["device_model"],
            "source": payload["source"],
            "tags": case_record.tags,
        })
        return True
    except Exception as exc:
        log.warning("case_ingest_failed", case_id=case_record.case_id, error=str(exc))
        return False


INDEX_HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>生产异常排查 Agent</title>
<style>
  :root{
    --bg:#f5f7fa; --card:#fff; --border:#e3e8ef;
    --text:#1f2937; --muted:#6b7280;
    --primary:#2563eb; --primary-hover:#1d4ed8;
    --success:#059669; --warning:#d97706; --danger:#dc2626;
    --plan:#7c3aed; --tool:#0891b2; --obs:#16a34a;
  }
  *{box-sizing:border-box}
  body{
    font-family:-apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif;
    margin:0;background:var(--bg);color:var(--text);
  }
  .wrap{max-width:1100px;margin:0 auto;padding:24px}
  h1{font-size:22px;margin:0 0 4px}
  .subtitle{color:var(--muted);font-size:13px;margin-bottom:20px}

  /* 输入区 */
  .input-card{
    background:var(--card);border:1px solid var(--border);border-radius:10px;
    padding:16px;margin-bottom:16px;
  }
  textarea{
    width:100%;height:70px;padding:10px;border:1px solid var(--border);
    border-radius:6px;font-size:14px;resize:vertical;font-family:inherit;
  }
  textarea:focus{outline:none;border-color:var(--primary)}
  .examples{margin:10px 0 6px;font-size:12px;color:var(--muted)}
  .chip{
    display:inline-block;margin:4px 6px 0 0;padding:4px 10px;
    background:#eef2ff;color:var(--primary);border-radius:14px;
    font-size:12px;cursor:pointer;border:1px solid #dbeafe;
  }
  .chip:hover{background:#dbeafe}
  .btn{
    margin-top:10px;padding:9px 22px;font-size:14px;font-weight:600;
    background:var(--primary);color:#fff;border:none;border-radius:6px;cursor:pointer;
  }
  .btn:hover{background:var(--primary-hover)}
  .btn:disabled{background:#9ca3af;cursor:not-allowed}

  /* 状态栏 */
  .statusbar{
    display:none;background:#fff;border:1px solid var(--border);border-radius:10px;
    padding:12px 16px;margin-bottom:14px;font-size:13px;
    display:flex;gap:18px;align-items:center;flex-wrap:wrap;
  }
  .statusbar.hidden{display:none}
  .badge{
    display:inline-block;padding:2px 10px;border-radius:12px;font-size:12px;font-weight:600;
  }
  .badge.intent{background:#ede9fe;color:#6d28d9}
  .badge.steps{background:#dbeafe;color:#1e40af}
  .badge.time{background:#dcfce7;color:#166534}

  /* 时间线 */
  .timeline{position:relative;padding-left:30px;margin-top:8px}
  .timeline::before{
    content:"";position:absolute;left:11px;top:8px;bottom:8px;
    width:2px;background:var(--border);
  }
  .step{position:relative;margin-bottom:14px}
  .step-dot{
    position:absolute;left:-23px;top:14px;width:14px;height:14px;
    border-radius:50%;border:3px solid #fff;box-shadow:0 0 0 2px var(--border);
  }
  .step-dot.plan{background:var(--plan)}
  .step-dot.tool_call{background:var(--tool)}
  .step-dot.tool_result{background:var(--obs)}
  .step-dot.error{background:var(--danger)}
  .step-card{
    background:var(--card);border:1px solid var(--border);border-radius:8px;
    padding:11px 14px;
  }
  .step-head{
    display:flex;align-items:center;gap:8px;font-size:13px;
  }
  .step-no{color:var(--muted);font-weight:600;font-size:12px}
  .step-kind{
    padding:1px 8px;border-radius:10px;font-size:11px;font-weight:600;letter-spacing:.3px;
  }
  .step-kind.plan{background:#ede9fe;color:var(--plan)}
  .step-kind.tool_call{background:#cffafe;color:var(--tool)}
  .step-kind.tool_result{background:#dcfce7;color:var(--obs)}
  .step-kind.error{background:#fee2e2;color:var(--danger)}

  .step-body{margin-top:6px;font-size:13.5px;line-height:1.55}
  .tool-name{
    font-family:ui-monospace,Menlo,Consolas,monospace;
    color:var(--tool);font-weight:600;
  }
  .args{
    font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12.5px;
    color:#475569;background:#f8fafc;padding:2px 6px;border-radius:4px;
  }
  .policy-tag{
    display:inline-block;margin-left:6px;padding:1px 7px;font-size:11px;
    background:#fef3c7;color:#92400e;border-radius:10px;
  }
  .summary{
    margin-top:6px;padding:8px 10px;background:#f8fafc;border-left:3px solid var(--obs);
    font-size:13px;color:#334155;border-radius:0 4px 4px 0;
  }
  .summary.fail{border-left-color:var(--danger);background:#fef2f2}
  .ok-icon{color:var(--success);font-weight:700}
  .fail-icon{color:var(--danger);font-weight:700}

  /* 最终答案卡 */
  .final-card{
    margin-top:18px;background:var(--card);border:1px solid var(--border);
    border-radius:10px;padding:20px;
    border-left:4px solid var(--success);
  }
  .final-card h2{margin:0 0 6px;font-size:18px;display:flex;align-items:center;gap:8px}
  .conclusion{
    margin:12px 0;padding:12px 14px;background:#f0fdf4;
    border-radius:6px;line-height:1.7;font-size:14.5px;
  }
  .section-title{
    margin:18px 0 8px;font-size:14px;font-weight:700;color:#374151;
    display:flex;align-items:center;gap:6px;
  }
  .section-title .icon{font-size:16px}
  .ev-list, .sg-list{margin:0;padding-left:0;list-style:none}
  .ev-list li{
    padding:8px 12px;border:1px solid var(--border);border-radius:6px;
    margin-bottom:6px;font-size:13px;background:#fafbfc;
  }
  .ev-list .ev-tool{
    display:inline-block;font-family:ui-monospace,monospace;color:var(--tool);
    font-weight:600;margin-right:8px;
  }
  .sg-list li{
    padding:8px 12px;border-left:3px solid var(--primary);
    background:#eff6ff;margin-bottom:6px;border-radius:0 4px 4px 0;font-size:14px;
  }
  .actions-row{display:flex;flex-wrap:wrap;gap:8px}
  .action-btn{
    padding:6px 14px;background:#fff7ed;color:#c2410c;border:1px solid #fed7aa;
    border-radius:6px;font-size:13px;font-family:ui-monospace,monospace;
    cursor:default;
  }
  .empty{color:var(--muted);font-size:13px;font-style:italic}

  /* 加载状态 */
  .loading{
    display:none;text-align:center;padding:24px;color:var(--muted);font-size:14px;
  }
  .loading.show{display:block}
  .spinner{
    display:inline-block;width:18px;height:18px;border:2px solid #e5e7eb;
    border-top-color:var(--primary);border-radius:50%;
    animation:spin .8s linear infinite;vertical-align:middle;margin-right:8px;
  }
  @keyframes spin{to{transform:rotate(360deg)}}

  /* 折叠原始 JSON */
  details.raw{margin-top:18px;font-size:12px}
  details.raw summary{cursor:pointer;color:var(--muted);user-select:none}
  details.raw pre{
    background:#0f172a;color:#e2e8f0;padding:12px;border-radius:6px;
    overflow:auto;font-size:12px;line-height:1.5;
  }
</style>
</head>
<body>
<div class="wrap">
  <h1>🛠️ 生产异常排查 Agent</h1>
  <div class="subtitle">输入现场异常描述，Agent 会自动规划工具调用、收集证据、给出诊断与处置建议。</div>

  <div class="input-card">
    <textarea id="q" placeholder="例如：2号相机掉线了，最近10分钟没有图像"></textarea>
    <div class="examples">
      点击示例快速填入：
      <span class="chip" onclick="fill('2号相机掉线了，最近10分钟没有图像')">📹 相机掉线</span>
      <span class="chip" onclick="fill('OCR识别成功率突然下降')">🔍 OCR识别下降</span>
      <span class="chip" onclick="fill('Kafka 消费堆积报警很多')">📨 Kafka堆积</span>
      <span class="chip" onclick="fill('推理服务延迟突然升高')">⚡ 推理延迟</span>
    </div>
    <button class="btn" id="runBtn" onclick="run()">▶ 开始排查</button>
  </div>

  <div id="status" class="statusbar hidden"></div>
  <div id="loading" class="loading"><span class="spinner"></span>Agent 正在排查中…</div>
  <div id="timeline"></div>
  <div id="final"></div>

  <!-- 工程师反馈区 — 排查完成后显示 -->
  <div id="feedbackArea" style="display:none;margin-top:16px;background:#fff;border:1px solid #e3e8ef;border-radius:10px;padding:16px;">
    <div style="font-size:14px;font-weight:600;margin-bottom:10px">📋 工程师反馈 — 帮助积累排查经验</div>
    <textarea id="engineerAction" placeholder="（可选）描述实际采取的处置措施，例如：将曝光恢复到 8000，光源亮度从 80 降至 65"
      style="width:100%;height:60px;padding:8px;border:1px solid #e3e8ef;border-radius:6px;font-size:13px;resize:vertical;font-family:inherit;"></textarea>
    <div style="display:flex;gap:10px;margin-top:10px;">
      <button onclick="submitFeedback('resolved')"
        style="padding:7px 18px;background:#059669;color:#fff;border:none;border-radius:6px;font-size:13px;cursor:pointer;">
        ✅ 问题已解决
      </button>
      <button onclick="submitFeedback('unresolved')"
        style="padding:7px 18px;background:#dc2626;color:#fff;border:none;border-radius:6px;font-size:13px;cursor:pointer;">
        ❌ 问题未解决
      </button>
      <button onclick="submitFeedback('partial')"
        style="padding:7px 18px;background:#d97706;color:#fff;border:none;border-radius:6px;font-size:13px;cursor:pointer;">
        ⚠ 部分解决
      </button>
    </div>
    <div id="feedbackStatus" style="margin-top:8px;font-size:13px;"></div>
  </div>
</div>

<script>
function fill(s){ document.getElementById('q').value = s; }

function escapeHtml(s){
  if(s===null||s===undefined) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  }[c]));
}

function fmtArgs(args){
  if(!args || Object.keys(args).length===0) return '';
  const parts = Object.entries(args).map(([k,v]) =>
    `${k}=${typeof v==='string' ? '"'+v+'"' : JSON.stringify(v)}`
  );
  return parts.join(', ');
}

function renderStep(ev){
  // user 事件不在时间线展示，已经在输入框
  if(ev.kind === 'user' || ev.kind === 'final') return '';
  const p = ev.payload || {};

  // PLAN 事件：plan→final 不展示（最终答案区已有）；plan→tool_call 才展示
  if(ev.kind === 'plan'){
    if(p.action === 'final') return '';
    const reason = p.thought ? `<div style="color:#6b7280;font-size:12.5px;margin-top:4px">💭 ${escapeHtml(p.thought)}</div>` : '';
    return `
      <div class="step">
        <div class="step-dot plan"></div>
        <div class="step-card">
          <div class="step-head">
            <span class="step-no">Step ${ev.step}</span>
            <span class="step-kind plan">PLAN · 规划</span>
          </div>
          <div class="step-body">
            决定调用 <span class="tool-name">${escapeHtml(p.tool)}</span>
            ${p.args ? `<span class="args">${escapeHtml(fmtArgs(p.args))}</span>` : ''}
            ${reason}
          </div>
        </div>
      </div>`;
  }

  if(ev.kind === 'tool_call'){
    const policy = p.policy
      ? `<span class="policy-tag">⚠ ${escapeHtml(p.policy)}</span>` : '';
    return `
      <div class="step">
        <div class="step-dot tool_call"></div>
        <div class="step-card">
          <div class="step-head">
            <span class="step-no">Step ${ev.step}</span>
            <span class="step-kind tool_call">ACT · 执行</span>
            ${policy}
          </div>
          <div class="step-body">
            <span class="tool-name">${escapeHtml(p.tool)}</span>(<span class="args">${escapeHtml(fmtArgs(p.args))}</span>)
          </div>
        </div>
      </div>`;
  }

  if(ev.kind === 'tool_result'){
    const ok = p.ok;
    const icon = ok ? '<span class="ok-icon">✓</span>' : '<span class="fail-icon">✗</span>';
    return `
      <div class="step">
        <div class="step-dot tool_result"></div>
        <div class="step-card">
          <div class="step-head">
            <span class="step-no">Step ${ev.step}</span>
            <span class="step-kind tool_result">OBSERVE · 观察</span>
          </div>
          <div class="summary ${ok?'':'fail'}">${icon} ${escapeHtml(p.summary || '(无摘要)')}</div>
        </div>
      </div>`;
  }

  if(ev.kind === 'error'){
    return `
      <div class="step">
        <div class="step-dot error"></div>
        <div class="step-card">
          <div class="step-head">
            <span class="step-no">Step ${ev.step}</span>
            <span class="step-kind error">ERROR · 错误</span>
          </div>
          <div class="summary fail">${escapeHtml(JSON.stringify(p))}</div>
        </div>
      </div>`;
  }
  return '';
}

function renderFinal(ans, raw){
  if(!ans) return '';
  const intent = ans.intent || 'unknown';
  const conclusion = ans.conclusion || '(无结论)';
  const evidence = ans.evidence || [];
  const suggestions = ans.suggestions || [];
  const safeActions = ans.safe_actions || [];

  const evHtml = evidence.length
    ? `<ul class="ev-list">${evidence.map(e => {
        // 形如 "tool_name: summary"，分离工具名增强可读性
        const m = String(e).match(/^([\w_]+):\s*(.+)$/);
        if(m) return `<li><span class="ev-tool">${escapeHtml(m[1])}</span>${escapeHtml(m[2])}</li>`;
        return `<li>${escapeHtml(e)}</li>`;
      }).join('')}</ul>`
    : '<div class="empty">(无证据)</div>';

  const sgHtml = suggestions.length
    ? `<ol class="sg-list">${suggestions.map(s => `<li>${escapeHtml(s)}</li>`).join('')}</ol>`
    : '<div class="empty">(无建议)</div>';

  const actHtml = safeActions.length
    ? `<div class="actions-row">${safeActions.map(a => `<span class="action-btn">▶ ${escapeHtml(a)}</span>`).join('')}</div>`
    : '<div class="empty">(无可执行动作)</div>';

  return `
    <div class="final-card">
      <h2>✅ 排查结论</h2>
      <div style="margin-top:8px">
        <span class="badge intent">问题类型: ${escapeHtml(intent)}</span>
      </div>
      <div class="conclusion">${escapeHtml(conclusion)}</div>

      <div class="section-title"><span class="icon">📋</span>证据链</div>
      ${evHtml}

      <div class="section-title"><span class="icon">🛠</span>处置建议</div>
      ${sgHtml}

      <div class="section-title"><span class="icon">⚡</span>可执行的低风险动作</div>
      ${actHtml}

      <details class="raw">
        <summary>查看原始 JSON</summary>
        <pre>${escapeHtml(JSON.stringify(raw, null, 2))}</pre>
      </details>
    </div>`;
}

let _currentTraceId = null;

async function run(){
  const q = document.getElementById('q').value.trim();
  if(!q){ alert('请输入问题描述'); return; }

  const btn = document.getElementById('runBtn');
  const status = document.getElementById('status');
  const loading = document.getElementById('loading');
  const timeline = document.getElementById('timeline');
  const final = document.getElementById('final');

  btn.disabled = true; btn.textContent = '排查中…';
  status.className = 'statusbar hidden';
  timeline.innerHTML = '';
  final.innerHTML = '';
  loading.className = 'loading show';
  _currentTraceId = null;

  try {
    const r = await fetch('/api/run', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({query:q})
    });
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const j = await r.json();

    _currentTraceId = j.trace_id || null;

    // 状态栏
    const intent = (j.answer && j.answer.intent) || 'unknown';
    const toolSteps = j.trace.filter(e => e.kind === 'tool_call').length;
    const persistedBadge = j.persisted
      ? '<span class="badge" style="background:#dcfce7;color:#166534">已记录</span>'
      : '<span class="badge" style="background:#fef9c3;color:#854d0e">未持久化</span>';
    status.className = 'statusbar';
    status.innerHTML = `
      <span class="badge intent">问题类型: ${escapeHtml(intent)}</span>
      <span class="badge steps">工具调用: ${toolSteps} 次</span>
      <span class="badge time">耗时: ${j.elapsed_sec}s</span>
      ${persistedBadge}
      ${j.trace_id ? `<span style="font-size:11px;color:var(--muted)">trace: ${escapeHtml(j.trace_id)}</span>` : ''}
    `;

    // 时间线（只渲染 plan/tool_call/tool_result/error）
    timeline.innerHTML = '<div class="timeline">' +
      j.trace.map(renderStep).join('') + '</div>';

    // 最终答案
    final.innerHTML = renderFinal(j.answer, j);

    // 工程师反馈区（仅持久化成功时显示）
    if(j.persisted && j.trace_id){
      document.getElementById('feedbackArea').style.display = 'block';
    }

  } catch(e) {
    final.innerHTML = `<div class="final-card" style="border-left-color:var(--danger)">
      <h2>❌ 请求失败</h2>
      <div class="conclusion" style="background:#fef2f2">${escapeHtml(e.message || String(e))}</div>
    </div>`;
  } finally {
    loading.className = 'loading';
    btn.disabled = false; btn.textContent = '▶ 开始排查';
  }
}

async function submitFeedback(outcome){
  if(!_currentTraceId){ alert('无 trace_id'); return; }
  const action = document.getElementById('engineerAction').value.trim();
  const r = await fetch('/api/traces/' + _currentTraceId + '/feedback', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      engineer_action: action || null,
      final_outcome: outcome,
      human_verified: true
    })
  });
  const j = await r.json();
  const fbStatus = document.getElementById('feedbackStatus');
  if(r.ok){
    const cand = j.candidate_generated
      ? '✅ 已自动生成候选经验，路径: ' + (j.candidate_path || '').split('/').pop()
      : '（未生成候选经验）';
    fbStatus.innerHTML = `<span style="color:var(--success)">反馈已提交 · 结果: ${outcome} · ${cand}</span>`;
    document.getElementById('feedbackArea').style.display = 'none';
  } else {
    fbStatus.innerHTML = `<span style="color:var(--danger)">提交失败: ${j.detail || ''}</span>`;
  }
}

// Ctrl+Enter 快捷提交
document.getElementById('q').addEventListener('keydown', (e) => {
  if((e.ctrlKey || e.metaKey) && e.key === 'Enter') run();
});
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML
