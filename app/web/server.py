"""
Optional FastAPI web UI. Run:
    uvicorn app.web.server:app --reload --port 8000
Open http://localhost:8000/
"""
from __future__ import annotations
from dataclasses import asdict

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app.agent.core import Agent
from app.config import get_llm
from app.main import render_event

app = FastAPI(title="Production Incident Agent Demo")


class Query(BaseModel):
    query: str


@app.post("/api/run")
def run(q: Query):
    agent = Agent(llm=get_llm(), on_event=render_event)
    res = agent.run(q.query)
    return {
        "answer": res.answer,
        "trace": [asdict(ev) for ev in res.trace],
    }


INDEX_HTML = """
<!doctype html><html><head><meta charset="utf-8"/>
<title>Agent Demo</title>
<style>
body{font-family:ui-monospace,Menlo,Consolas,monospace;margin:24px;max-width:960px}
textarea{width:100%;height:70px}
.box{border:1px solid #ccc;border-radius:6px;padding:10px;margin:10px 0;white-space:pre-wrap}
.user{background:#eef7ff} .plan{background:#fffbe6} .call{background:#eef}
.obs{background:#efffef} .final{background:#e8ffe8;border-color:#2b8}
.err{background:#ffecec}
button{padding:8px 18px;font-size:14px}
</style></head><body>
<h2>🛠 Production Incident Agent Demo</h2>
<p>试试： <i>2号相机掉线了，最近10分钟没有图像</i> / <i>OCR识别成功率突然下降</i> / <i>Kafka消费堆积</i> / <i>推理延迟突然升高</i></p>
<textarea id="q"></textarea><br/>
<button onclick="run()">Run Agent</button>
<div id="out"></div>
<script>
async function run(){
  const q = document.getElementById('q').value;
  const out = document.getElementById('out'); out.innerHTML='running...';
  const r = await fetch('/api/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query:q})});
  const j = await r.json();
  let html='';
  for(const ev of j.trace){
    const cls = ({user:'user',plan:'plan',tool_call:'call',tool_result:'obs',final:'final',error:'err'})[ev.kind]||'box';
    html += `<div class="box ${cls}"><b>step ${ev.step} · ${ev.kind}</b>\\n${JSON.stringify(ev.payload,null,2)}</div>`;
  }
  out.innerHTML = html;
}
</script></body></html>
"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML
