"""
Optional FastAPI web UI. Run:
    uvicorn app.web.server:app --reload --port 8000
Open http://localhost:8000/
"""
from __future__ import annotations
import time
from dataclasses import asdict

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.agent.core import Agent
from app.config import get_llm

app = FastAPI(title="Production Incident Agent Demo")


class Query(BaseModel):
    query: str


@app.post("/api/run")
def run(q: Query):
    t0 = time.time()
    agent = Agent(llm=get_llm())
    res = agent.run(q.query)
    elapsed = round(time.time() - t0, 2)
    return {
        "answer": res.answer,
        "trace": [asdict(ev) for ev in res.trace],
        "elapsed_sec": elapsed,
    }


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

  try {
    const r = await fetch('/api/run', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({query:q})
    });
    if(!r.ok) throw new Error('HTTP ' + r.status);
    const j = await r.json();

    // 状态栏
    const intent = (j.answer && j.answer.intent) || 'unknown';
    const toolSteps = j.trace.filter(e => e.kind === 'tool_call').length;
    status.className = 'statusbar';
    status.innerHTML = `
      <span class="badge intent">问题类型: ${escapeHtml(intent)}</span>
      <span class="badge steps">工具调用: ${toolSteps} 次</span>
      <span class="badge time">耗时: ${j.elapsed_sec}s</span>
    `;

    // 时间线（只渲染 plan/tool_call/tool_result/error）
    timeline.innerHTML = '<div class="timeline">' +
      j.trace.map(renderStep).join('') + '</div>';

    // 最终答案
    final.innerHTML = renderFinal(j.answer, j);

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
