#!/usr/bin/env python3
# =============================================================================
# app.py — Project Aether NOC Dashboard (FastAPI)
#
# Endpoints:
#   GET  /                    HTML dashboard
#   GET  /api/status          System health + model status
#   GET  /api/topology        NetworkX graph as JSON (nodes + edges)
#   GET  /api/acps            Recent ACPs from IKB log
#   POST /api/nlq             Natural language query → LLM answer
#   POST /api/feedback        Operator accept/reject ACP feedback
#   GET  /api/compliance      Air-gap compliance report
#   GET  /api/benchmark       Lead-time benchmark results
#   WS   /ws/alerts           WebSocket: live ACP stream
#
# Start: python3 phase5-dashboard/app.py
# Dashboard: http://localhost:8080
# =============================================================================
import os
import sys
import json
import time
import asyncio
import threading
from datetime import datetime

REPO_ROOT  = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(REPO_ROOT, "..", "phase3-models"))
sys.path.insert(0, os.path.join(REPO_ROOT, "..", "phase4-llm"))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Phase 3 imports
from graph_model import ClonalGraphEngine
from acp_manager import AnomalyContextPacket

# Phase 4 imports (graceful)
def _ollama_available():
    return False

try:
    from llm_copilot import AetherCopilot, _ollama_available
    HAS_LLM = True
except Exception:
    HAS_LLM = False

IKB_LOG  = os.path.join(REPO_ROOT, "..", "phase3-models", "ikb", "incidents.jsonl")
ACP_DIR  = os.path.join(REPO_ROOT, "..", "phase3-models", "acp_logs")
SAVE_DIR = os.path.join(REPO_ROOT, "..", "phase3-models", "saved")

app = FastAPI(title="Project Aether NOC Copilot", version="4.0.0")

# Singleton state
_graph_engine = ClonalGraphEngine()
_copilot = None
_connected_ws: list[WebSocket] = []
_acp_log_pos: int = 0  # byte offset for tailing incidents.jsonl


def _get_copilot():
    global _copilot
    if _copilot is None and HAS_LLM:
        try:
            _copilot = AetherCopilot(auto_seed=True)
        except Exception as e:
            print(f"[!] Copilot init failed: {e}")
    return _copilot


# ── HTML Dashboard ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Project Aether — NOC Copilot</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0a0e1a;color:#c9d1d9;font-family:'Consolas','Courier New',monospace;font-size:13px}
  header{background:#161b27;border-bottom:1px solid #30363d;padding:12px 20px;display:flex;align-items:center;gap:16px}
  header h1{color:#58a6ff;font-size:16px;letter-spacing:1px}
  .badge{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:bold}
  .badge-green{background:#1a3a2a;color:#3fb950}
  .badge-red{background:#3a1a1a;color:#f85149}
  .badge-yellow{background:#3a2e1a;color:#e3b341}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding:12px;height:calc(100vh - 50px)}
  .panel{background:#161b27;border:1px solid #30363d;border-radius:6px;overflow:hidden;display:flex;flex-direction:column}
  .panel-header{background:#1c2230;padding:8px 14px;border-bottom:1px solid #30363d;font-size:12px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;display:flex;justify-content:space-between;align-items:center}
  .panel-body{flex:1;overflow-y:auto;padding:10px}
  .alert{border-left:3px solid;margin-bottom:8px;padding:8px 10px;border-radius:0 4px 4px 0;font-size:12px;cursor:pointer;transition:background 0.1s}
  .alert:hover{background:#1c2230}
  .alert-CRITICAL{border-color:#f85149;background:#1e1014}
  .alert-HIGH{border-color:#e3b341;background:#1e1a10}
  .alert-MEDIUM{border-color:#58a6ff;background:#10161e}
  .alert-LOW{border-color:#3fb950;background:#101e12}
  .alert-title{color:#e6edf3;font-weight:bold;margin-bottom:4px}
  .alert-meta{color:#8b949e;font-size:11px}
  #topo-canvas{width:100%;height:100%;min-height:200px}
  #nlq-input{width:calc(100% - 70px);background:#0d1117;border:1px solid #30363d;color:#c9d1d9;padding:6px 10px;border-radius:4px;font-family:inherit;font-size:13px}
  #nlq-btn{background:#238636;color:white;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-family:inherit;font-size:13px}
  #nlq-btn:hover{background:#2ea043}
  #nlq-output{padding:10px;background:#0d1117;border-radius:4px;margin-top:8px;min-height:60px;white-space:pre-wrap;color:#c9d1d9;font-size:12px;line-height:1.6}
  .stat-row{display:flex;justify-content:space-between;margin-bottom:6px;padding:4px 0;border-bottom:1px solid #21262d}
  .stat-label{color:#8b949e}
  .stat-val{color:#e6edf3;font-weight:bold}
  .node{fill:#1c2230;stroke:#58a6ff;stroke-width:1.5}
  .node-pe{fill:#1a2e4a;stroke:#58a6ff}
  .node-p{fill:#2e1a4a;stroke:#a371f7}
  .node-ce{fill:#1a2e1a;stroke:#3fb950}
  .link{stroke:#30363d;stroke-width:1.5}
  .link-degraded{stroke:#f85149;stroke-width:3;stroke-dasharray:5,3}
  .node-label{fill:#c9d1d9;font-size:10px;font-family:monospace}
  .severity-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px}
  .dot-CRITICAL{background:#f85149}
  .dot-HIGH{background:#e3b341}
  .dot-MEDIUM{background:#58a6ff}
  .dot-LOW{background:#3fb950}
  #compliance-badge{font-size:11px}
</style>
</head>
<body>
<header>
  <h1>⬡ PROJECT AETHER — NOC COPILOT</h1>
  <span class="badge badge-green" id="status-badge">ONLINE</span>
  <span style="color:#8b949e;font-size:11px">Air-Gapped MPLS Predictive Operations</span>
  <span class="badge" id="compliance-badge">CHECKING...</span>
  <span style="margin-left:auto;color:#8b949e;font-size:11px" id="clock"></span>
</header>

<div class="grid">
  <!-- Topology -->
  <div class="panel">
    <div class="panel-header">Live Topology <span id="topo-status" style="color:#3fb950">●</span></div>
    <div class="panel-body" style="padding:0">
      <svg id="topo-canvas" viewBox="0 0 460 280"></svg>
    </div>
  </div>

  <!-- Alerts -->
  <div class="panel">
    <div class="panel-header">Alert Feed <span id="alert-count" class="badge badge-yellow">0</span></div>
    <div class="panel-body" id="alert-feed"></div>
  </div>

  <!-- LLM Copilot -->
  <div class="panel">
    <div class="panel-header">Copilot — Natural Language Query</div>
    <div class="panel-body">
      <div style="display:flex;gap:6px;margin-bottom:8px">
        <input id="nlq-input" placeholder="Ask anything: 'What is wrong with pe1?' / 'How do I fix BGP flap?'" onkeydown="if(event.key==='Enter')nlqSend()">
        <button id="nlq-btn" onclick="nlqSend()">Ask</button>
      </div>
      <div id="nlq-output">Type a question above. Powered by Mistral 7B (offline).</div>
      <div style="margin-top:10px;border-top:1px solid #21262d;padding-top:8px">
        <div style="color:#8b949e;font-size:11px;margin-bottom:6px">QUICK QUERIES</div>
        <div style="display:flex;flex-wrap:wrap;gap:4px">
          <button onclick="quickQ('What is likely to fail next?')" style="background:#1c2230;color:#58a6ff;border:1px solid #30363d;padding:3px 8px;border-radius:4px;cursor:pointer;font-size:11px">What fails next?</button>
          <button onclick="quickQ('Why is risk elevated?')" style="background:#1c2230;color:#58a6ff;border:1px solid #30363d;padding:3px 8px;border-radius:4px;cursor:pointer;font-size:11px">Why elevated?</button>
          <button onclick="quickQ('How do I fix BGP neighbor flap on pe1?')" style="background:#1c2230;color:#58a6ff;border:1px solid #30363d;padding:3px 8px;border-radius:4px;cursor:pointer;font-size:11px">Fix BGP flap?</button>
          <button onclick="quickQ('Show me the mitigation steps for packet loss')" style="background:#1c2230;color:#58a6ff;border:1px solid #30363d;padding:3px 8px;border-radius:4px;cursor:pointer;font-size:11px">Fix packet loss?</button>
        </div>
      </div>
    </div>
  </div>

  <!-- System Stats -->
  <div class="panel">
    <div class="panel-header">System Status</div>
    <div class="panel-body" id="stats-panel">
      <div class="stat-row"><span class="stat-label">Models</span><span class="stat-val" id="stat-models">loading...</span></div>
      <div class="stat-row"><span class="stat-label">LLM Copilot</span><span class="stat-val" id="stat-llm">checking...</span></div>
      <div class="stat-row"><span class="stat-label">IKB (ChromaDB)</span><span class="stat-val" id="stat-ikb">checking...</span></div>
      <div class="stat-row"><span class="stat-label">ACPs logged</span><span class="stat-val" id="stat-acps">0</span></div>
      <div class="stat-row"><span class="stat-label">Uptime</span><span class="stat-val" id="stat-uptime">0s</span></div>
      <div style="margin-top:10px;border-top:1px solid #21262d;padding-top:8px;color:#8b949e;font-size:11px">RECENT ALERTS</div>
      <div id="recent-summary" style="margin-top:6px;font-size:11px;color:#8b949e">No alerts yet</div>
    </div>
  </div>
</div>

<script>
// ── Clock ──────────────────────────────────────────────────────────────────
function updateClock() {
  document.getElementById('clock').textContent = new Date().toISOString().replace('T',' ').slice(0,19)+' UTC';
}
setInterval(updateClock, 1000); updateClock();

// ── Topology SVG ───────────────────────────────────────────────────────────
const NODES = {
  'pe1':       {x:160,y:130,cls:'node-pe',label:'PE1'},
  'p1':        {x:230,y:80, cls:'node-p', label:'P1'},
  'pe2':       {x:300,y:130,cls:'node-pe',label:'PE2'},
  'ce-branch1':{x:80, y:200,cls:'node-ce',label:'Branch1'},
  'ce-hub':    {x:155,y:220,cls:'node-ce',label:'Hub'},
  'ce-branch2':{x:310,y:200,cls:'node-ce',label:'Branch2'},
  'ce-dc':     {x:380,y:220,cls:'node-ce',label:'DC'},
};
const LINKS = [
  ['pe1','p1'],['p1','pe2'],['pe1','ce-branch1'],
  ['pe1','ce-hub'],['pe2','ce-branch2'],['pe2','ce-dc'],
  ['pe1','pe2'],
];
const svg = document.getElementById('topo-canvas');
let degradedLinks = new Set();

function drawTopo() {
  svg.innerHTML = '';
  // Links
  LINKS.forEach(([a,b]) => {
    const na=NODES[a],nb=NODES[b];
    const isDeg = degradedLinks.has(a+'→'+b)||degradedLinks.has(b+'→'+a);
    const line = document.createElementNS('http://www.w3.org/2000/svg','line');
    Object.assign(line,{});
    line.setAttribute('x1',na.x); line.setAttribute('y1',na.y);
    line.setAttribute('x2',nb.x); line.setAttribute('y2',nb.y);
    line.setAttribute('class', isDeg ? 'link link-degraded' : 'link');
    svg.appendChild(line);
  });
  // Nodes
  Object.entries(NODES).forEach(([id,n]) => {
    const g = document.createElementNS('http://www.w3.org/2000/svg','g');
    const c = document.createElementNS('http://www.w3.org/2000/svg','circle');
    c.setAttribute('cx',n.x); c.setAttribute('cy',n.y); c.setAttribute('r',18);
    c.setAttribute('class','node '+n.cls); g.appendChild(c);
    const t = document.createElementNS('http://www.w3.org/2000/svg','text');
    t.setAttribute('x',n.x); t.setAttribute('y',n.y+4);
    t.setAttribute('text-anchor','middle'); t.setAttribute('class','node-label');
    t.textContent=n.label; g.appendChild(t);
    svg.appendChild(g);
  });
}
drawTopo();

// ── Alert renderer (shared by history load + WebSocket) ───────────────────
let alertCount = 0;
const feed = document.getElementById('alert-feed');

function renderAlert(acp, prepend=true) {
  alertCount++;
  document.getElementById('alert-count').textContent = alertCount;
  const div = document.createElement('div');
  div.className = 'alert alert-'+(acp.severity||'MEDIUM');
  const conf = acp.confidence != null ? ((acp.confidence||0)*100).toFixed(0)+'%' : '?';
  const ttf  = acp.ttf != null && acp.ttf >= 0 ? acp.ttf.toFixed(0)+'s' : '?';
  div.innerHTML = `<div class="alert-title">
    <span class="severity-dot dot-${acp.severity||'MEDIUM'}"></span>
    ${acp.fault_class||'Unknown'} &mdash; ${acp.severity||'?'}
  </div>
  <div class="alert-meta">
    conf=${conf} | ttf=${ttf} | mode=${acp.execution_mode||'?'} | ${(acp.timestamp||'').slice(11,19)} UTC
  </div>
  <div style="margin-top:4px;color:#8b949e;font-size:11px">${(acp.rationale||'').slice(0,130)}</div>`;
  div.onclick = () => loadExplanation(acp.acp_id);
  if (prepend) feed.prepend(div); else feed.appendChild(div);
  // Mark affected links as degraded in topology
  if (acp.fault_class && acp.fault_class !== 'Healthy') {
    degradedLinks.add('pe1→p1');
    drawTopo();
    setTimeout(() => { degradedLinks.delete('pe1→p1'); drawTopo(); }, 30000);
  }
}

// ── Load historical ACPs on page open ─────────────────────────────────────
async function loadHistory() {
  try {
    const r = await fetch('/api/acps?limit=50');
    const d = await r.json();
    if (!d.acps || !d.acps.length) return;
    // Render oldest-first so newest appears at top after prepend
    const sorted = [...d.acps].reverse();
    // Build compact payload matching WebSocket format
    sorted.forEach(entry => {
      const ml  = entry.ml_analysis || {};
      const cor = entry.corroboration || {};
      renderAlert({
        acp_id        : entry.acp_id,
        timestamp     : entry.timestamp,
        severity      : entry.severity || 'MEDIUM',
        fault_class   : ml.predicted_fault_class || entry.trigger_source || 'Unknown',
        confidence    : ml.confidence_score,
        ttf           : ml.estimated_time_to_failure_sec,
        execution_mode: cor.execution_mode || 'RECOMMEND_ONLY',
        rationale     : cor.rationale || '',
        action        : cor.recommended_action || 'NO_ACTION',
      }, true);
    });
  } catch(e) { console.error('loadHistory:', e); }
}

// ── WebSocket for live alerts ──────────────────────────────────────────────
function connectWS() {
  const ws = new WebSocket('ws://'+location.host+'/ws/alerts');
  ws.onmessage = e => { renderAlert(JSON.parse(e.data), true); };
  ws.onclose = () => setTimeout(connectWS, 3000);
}
loadHistory();
connectWS();

// ── NLQ ───────────────────────────────────────────────────────────────────
async function nlqSend() {
  const q = document.getElementById('nlq-input').value.trim();
  if (!q) return;
  const out = document.getElementById('nlq-output');
  out.textContent = '⟳ Thinking...';
  try {
    const r = await fetch('/api/nlq', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({question: q})
    });
    const d = await r.json();
    out.textContent = d.answer || d.error || 'No response';
  } catch(e) { out.textContent = 'Error: '+e; }
}
function quickQ(q) {
  document.getElementById('nlq-input').value = q;
  nlqSend();
}

// ── Status polling ─────────────────────────────────────────────────────────
let startTime = Date.now();
async function pollStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    document.getElementById('stat-models').textContent = s.models_loaded ? '✓ loaded' : '✗ missing';
    document.getElementById('stat-llm').textContent = s.llm_online ? '✓ Mistral online' : '⚠ offline (fallback)';
    document.getElementById('stat-ikb').textContent = s.ikb_docs > 0 ? `✓ ${s.ikb_docs} docs` : '✗ empty';
    document.getElementById('stat-acps').textContent = s.acp_count || alertCount;
    const up = Math.floor((Date.now()-startTime)/1000);
    document.getElementById('stat-uptime').textContent = up+'s';
    const cb = document.getElementById('compliance-badge');
    if (s.air_gap_compliant === true) {
      cb.textContent = '✓ AIR-GAPPED'; cb.className='badge badge-green';
    } else if (s.air_gap_compliant === false) {
      cb.textContent = '⚠ NOT COMPLIANT'; cb.className='badge badge-red';
    }
  } catch(e) {}
}
setInterval(pollStatus, 5000); pollStatus();

async function loadExplanation(acp_id) {
  if (!acp_id) return;
  const out = document.getElementById('nlq-output');
  out.textContent = '⟳ Generating incident report...';
  try {
    const r = await fetch('/api/nlq', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({question: `Explain ACP ${acp_id} — what failed, why, and what to do?`})
    });
    const d = await r.json();
    out.textContent = d.answer || d.error;
  } catch(e) { out.textContent = 'Error: '+e; }
}
</script>
</body>
</html>"""


# ── FastAPI routes ────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return DASHBOARD_HTML


@app.get("/api/status")
async def status():
    models_ok = all(
        os.path.exists(os.path.join(SAVE_DIR, f))
        for f in ["autoencoder.pt", "classifier.pt", "regressor.pt"]
    )
    acp_count = 0
    if os.path.exists(IKB_LOG):
        with open(IKB_LOG) as f:
            acp_count = sum(1 for l in f if l.strip())

    # IKB doc count
    ikb_docs = 0
    try:
        import chromadb
        client = chromadb.PersistentClient(
            path=os.path.join(REPO_ROOT, "..", "phase4-llm", "chroma_db")
        )
        try:
            col = client.get_collection("runbooks")
            ikb_docs = col.count()
        except Exception:
            pass
    except Exception:
        pass

    # Compliance (quick check)
    from airgap_compliance import _probe
    dns_result = _probe("8.8.8.8", 53)
    compliant = not dns_result["reachable"]  # in real air-gap, this would be True

    return {
        "models_loaded": models_ok,
        "llm_online": _ollama_available() if HAS_LLM else False,
        "ikb_docs": ikb_docs,
        "acp_count": acp_count,
        "air_gap_compliant": compliant,
    }


@app.get("/api/topology")
async def topology():
    nodes = []
    edges = []
    for node, data in _graph_engine.base_graph.nodes(data=True):
        nodes.append({"id": node, "role": data.get("role", "unknown")})
    for u, v, data in _graph_engine.base_graph.edges(data=True):
        edges.append({
            "source": u, "target": v,
            "capacity": data.get("capacity", 0),
            "delay": data.get("delay", 0),
            "cost": data.get("cost", 0),
            "is_backup": data.get("is_backup", False),
        })
    return {"nodes": nodes, "edges": edges}


def _load_acp_files(limit: int = 50) -> list[dict]:
    """Load full ACP JSON files from acp_logs/, sorted by timestamp, newest-last."""
    import glob
    entries = []
    if os.path.exists(ACP_DIR):
        files = sorted(glob.glob(os.path.join(ACP_DIR, "*.json")))
        for path in files[-limit * 2:]:  # over-fetch, trim after sort
            try:
                with open(path) as f:
                    entries.append(json.load(f))
            except Exception:
                pass
        entries.sort(key=lambda x: x.get("timestamp", ""))
    return entries[-limit:]


@app.get("/api/acps")
async def get_acps(limit: int = 50):
    import glob
    total = len(glob.glob(os.path.join(ACP_DIR, "*.json"))) if os.path.exists(ACP_DIR) else 0
    entries = _load_acp_files(limit)
    return {"acps": entries, "total": total}


class NLQRequest(BaseModel):
    question: str


@app.post("/api/nlq")
async def nlq(req: NLQRequest):
    copilot = _get_copilot()
    if copilot is None:
        return {"answer": "LLM copilot unavailable. Check Ollama installation.", "source": "error"}
    answer = await asyncio.get_event_loop().run_in_executor(
        None, copilot.query, req.question
    )
    return {"answer": answer, "source": "copilot"}


class FeedbackRequest(BaseModel):
    acp_id: str
    feedback: str  # "accepted" | "rejected"


@app.post("/api/feedback")
async def feedback(req: FeedbackRequest):
    sys.path.insert(0, os.path.join(REPO_ROOT, "..", "phase3-models"))
    from feedback_cli import _apply_feedback
    ok = _apply_feedback(req.acp_id, req.feedback)
    if not ok:
        raise HTTPException(status_code=404, detail="ACP not found")
    return {"status": "ok", "acp_id": req.acp_id, "feedback": req.feedback}


@app.get("/api/compliance")
async def compliance():
    from airgap_compliance import run_compliance_check, _sign_report
    report = run_compliance_check()
    report = _sign_report(report)
    return report


@app.get("/api/benchmark")
async def benchmark():
    from benchmark_harness import run_benchmark
    data_path = os.path.join(REPO_ROOT, "..", "phase3-models", "dataset.csv")
    if not os.path.exists(data_path):
        return {"error": "dataset.csv not found"}
    results = await asyncio.get_event_loop().run_in_executor(
        None, run_benchmark, data_path
    )
    return {"results": results}


# ── WebSocket live alert stream ───────────────────────────────────────────────

@app.websocket("/ws/alerts")
async def ws_alerts(websocket: WebSocket):
    await websocket.accept()
    _connected_ws.append(websocket)
    try:
        while True:
            await websocket.receive_text()  # keep-alive
    except WebSocketDisconnect:
        _connected_ws.remove(websocket)


async def broadcast_acp(acp_dict: dict):
    dead = []
    for ws in _connected_ws:
        try:
            await ws.send_text(json.dumps(acp_dict))
        except Exception:
            dead.append(ws)
    for ws in dead:
        _connected_ws.remove(ws)


# ── Background: tail incidents.jsonl → broadcast to WebSocket ────────────────

async def _tail_acp_log():
    """Polls acp_logs/ every 2s for new JSON files, broadcasts to all WebSocket clients."""
    import glob
    seen = set()
    # Pre-populate seen with existing files so we only push NEW ones going forward
    if os.path.exists(ACP_DIR):
        for f in glob.glob(os.path.join(ACP_DIR, "*.json")):
            seen.add(f)
    while True:
        await asyncio.sleep(2)
        if not os.path.exists(ACP_DIR):
            continue
        try:
            current = set(glob.glob(os.path.join(ACP_DIR, "*.json")))
            new_files = sorted(current - seen)
            for path in new_files:
                try:
                    with open(path) as f:
                        entry = json.load(f)
                    ml  = entry.get("ml_analysis", {})
                    cor = entry.get("corroboration", {})
                    ws_msg = {
                        "acp_id"        : entry.get("acp_id", ""),
                        "timestamp"     : entry.get("timestamp", ""),
                        "severity"      : entry.get("severity", "MEDIUM"),
                        "fault_class"   : ml.get("predicted_fault_class", "Unknown"),
                        "confidence"    : ml.get("confidence_score", 0),
                        "ttf"           : ml.get("estimated_time_to_failure_sec", -1),
                        "execution_mode": cor.get("execution_mode", "RECOMMEND_ONLY"),
                        "rationale"     : cor.get("rationale", ""),
                        "action"        : cor.get("recommended_action", "NO_ACTION"),
                    }
                    await broadcast_acp(ws_msg)
                    seen.add(path)
                except Exception:
                    pass
        except Exception:
            pass


# ── Startup event ─────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    asyncio.create_task(_tail_acp_log())
    print("[*] Aether NOC Dashboard ready — http://localhost:8080")
    print("[*] Tailing incidents.jsonl → WebSocket live feed active")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    sys.path.insert(0, os.path.join(REPO_ROOT, "..", "phase3-models"))
    sys.path.insert(0, os.path.join(REPO_ROOT, "..", "phase4-llm"))
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False, log_level="info")
