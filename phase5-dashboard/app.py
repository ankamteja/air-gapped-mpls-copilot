#!/usr/bin/env python3
# =============================================================================
# app.py — Project Aether NOC Dashboard (FastAPI)  v5.0
#
# Endpoints:
#   GET  /                    HTML dashboard (hamburger sidebar SPA)
#   GET  /api/status          System health + model status
#   GET  /api/topology        NetworkX graph as JSON (nodes + edges)
#   GET  /api/acps            Recent ACPs from acp_logs/
#   POST /api/nlq             Natural language query → LLM answer
#   POST /api/feedback        Operator accept/reject ACP feedback
#   GET  /api/compliance      Air-gap compliance report
#   GET  /api/benchmark       Lead-time benchmark results
#   GET  /api/policy          Current autonomy policy matrix
#   PUT  /api/policy          Update a row of the autonomy policy matrix
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

IKB_LOG          = os.path.join(REPO_ROOT, "..", "phase3-models", "ikb", "incidents.jsonl")
ACP_DIR          = os.path.join(REPO_ROOT, "..", "phase3-models", "acp_logs")
SAVE_DIR         = os.path.join(REPO_ROOT, "..", "phase3-models", "saved")
POLICY_OVERRIDE  = os.path.join(REPO_ROOT, "..", "phase3-models", "policy_overrides.json")

app = FastAPI(title="Project Aether NOC Copilot", version="5.0.0")

_graph_engine = ClonalGraphEngine()
_copilot = None
_connected_ws: list[WebSocket] = []
_llm_lock = asyncio.Lock()  # serialize LLM calls — prevents concurrent Ollama contention


def _get_copilot():
    global _copilot
    if _copilot is None and HAS_LLM:
        try:
            _copilot = AetherCopilot(auto_seed=True)
        except Exception as e:
            print(f"[!] Copilot init failed: {e}")
    return _copilot


def _load_policy_overrides() -> dict:
    if os.path.exists(POLICY_OVERRIDE):
        try:
            with open(POLICY_OVERRIDE) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


# ── HTML Dashboard ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Project Aether — NOC Copilot</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden}
body{background:#0a0e1a;color:#c9d1d9;font-family:'Consolas','Courier New',monospace;font-size:14px;display:flex;flex-direction:column}
/* ── Header ── */
#app-header{background:#161b27;border-bottom:1px solid #30363d;padding:0 16px;display:flex;align-items:center;gap:14px;flex-shrink:0;height:48px;z-index:10}
#hamburger{background:none;border:none;color:#58a6ff;font-size:20px;cursor:pointer;padding:4px 6px;border-radius:4px;line-height:1;flex-shrink:0}
#hamburger:hover{background:#1c2230}
#app-title{color:#58a6ff;font-size:15px;letter-spacing:1px;white-space:nowrap}
.badge{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:bold;white-space:nowrap}
.badge-green{background:#1a3a2a;color:#3fb950}
.badge-red{background:#3a1a1a;color:#f85149}
.badge-yellow{background:#3a2e1a;color:#e3b341}
.badge-blue{background:#1a2540;color:#58a6ff}
#header-right{margin-left:auto;display:flex;align-items:center;gap:10px}
#clock{color:#484f58;font-size:11px}
/* ── App body ── */
#app-body{display:flex;flex:1;overflow:hidden}
/* ── Sidebar ── */
#sidebar{width:220px;min-width:220px;background:#0d1117;border-right:1px solid #30363d;display:flex;flex-direction:column;transition:width .2s ease,min-width .2s ease;overflow:hidden;flex-shrink:0}
#sidebar.collapsed{width:48px;min-width:48px}
.sidebar-section{padding:10px 14px 2px;color:#484f58;font-size:10px;text-transform:uppercase;letter-spacing:1px;white-space:nowrap;overflow:hidden;opacity:1;transition:opacity .1s}
#sidebar.collapsed .sidebar-section{opacity:0;height:0;padding:0}
.nav-item{padding:9px 14px;cursor:pointer;display:flex;align-items:center;gap:12px;color:#8b949e;border-left:3px solid transparent;transition:color .1s,background .1s,border-color .1s;white-space:nowrap;user-select:none}
.nav-item:hover{background:#161b27;color:#c9d1d9}
.nav-item.active{color:#58a6ff;border-left-color:#58a6ff;background:#161b27}
.nav-icon{font-size:15px;flex-shrink:0;width:20px;text-align:center}
.nav-label{font-size:13px;overflow:hidden;opacity:1;transition:opacity .15s}
#sidebar.collapsed .nav-label{opacity:0;width:0;overflow:hidden}
/* sidebar footer */
#sidebar-footer{margin-top:auto;padding:10px 14px;border-top:1px solid #21262d;font-size:11px;color:#484f58;white-space:nowrap;overflow:hidden}
#sidebar.collapsed #sidebar-footer{display:none}
/* ── Main content ── */
#main-content{flex:1;overflow:hidden;display:flex;flex-direction:column}
.view{display:none;flex:1;overflow:hidden}
.view.active{display:flex}
/* ── Layouts ── */
.split-h{display:flex;flex-direction:row;gap:10px;padding:10px;flex:1;overflow:hidden}
.split-v{display:flex;flex-direction:column;gap:10px;padding:10px;flex:1;overflow:hidden}
.col-60{flex:0 0 60%;overflow:hidden;display:flex;flex-direction:column}
.col-40{flex:0 0 40%;overflow:hidden;display:flex;flex-direction:column}
/* ── Panels ── */
.panel{background:#161b27;border:1px solid #30363d;border-radius:6px;display:flex;flex-direction:column;overflow:hidden;flex:1;min-height:0}
.panel-header{background:#1c2230;padding:8px 14px;border-bottom:1px solid #30363d;font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;display:flex;justify-content:space-between;align-items:center;flex-shrink:0}
.panel-body{flex:1;overflow-y:auto;padding:14px;min-height:0}
.panel-body-nopad{flex:1;overflow:hidden;display:flex;flex-direction:column;min-height:0}
/* ── Alerts ── */
.alert{border-left:3px solid;margin-bottom:10px;padding:12px 16px;border-radius:0 5px 5px 0;font-size:13px;cursor:pointer;transition:background .1s}
.alert:hover{background:#1c2230}
.alert-CRITICAL{border-color:#f85149;background:#1e1014}
.alert-HIGH{border-color:#e3b341;background:#1e1a10}
.alert-MEDIUM{border-color:#58a6ff;background:#10161e}
.alert-LOW{border-color:#3fb950;background:#101e12}
.alert-title{color:#e6edf3;font-weight:bold;margin-bottom:3px}
.alert-meta{color:#8b949e;font-size:11px}
.alert-rationale{margin-top:4px;color:#6e7681;font-size:11px}
.severity-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px}
.dot-CRITICAL{background:#f85149}
.dot-HIGH{background:#e3b341}
.dot-MEDIUM{background:#58a6ff}
.dot-LOW{background:#3fb950}
/* ── Topology SVG ── */
.topo-wrap{flex:1;min-height:0;position:relative;overflow:hidden}
svg.topo-svg{width:100%;height:100%}
.node{fill:#1c2230;stroke:#58a6ff;stroke-width:1.5}
.node-pe{fill:#1a2e4a;stroke:#58a6ff}
.node-p{fill:#2e1a4a;stroke:#a371f7}
.node-ce{fill:#1a2e1a;stroke:#3fb950}
.link{stroke:#30363d;stroke-width:1.5}
.link-degraded{stroke:#f85149;stroke-width:3;stroke-dasharray:6,3;animation:dash 1s linear infinite}
@keyframes dash{to{stroke-dashoffset:-9}}
.node-label{fill:#c9d1d9;font-size:10px;font-family:monospace;pointer-events:none}
.topo-legend{position:absolute;bottom:6px;left:10px;font-size:10px;color:#484f58;display:flex;gap:12px}
.legend-item{display:flex;align-items:center;gap:4px}
.legend-line{width:18px;height:2px;display:inline-block}
.legend-deg{background:#f85149}
.legend-ok{background:#30363d}
/* ── Time-travel ── */
#tt-slider{width:100%;margin:6px 0;accent-color:#58a6ff;cursor:pointer}
#tt-controls{display:flex;align-items:center;gap:8px;padding:6px 10px;border-top:1px solid #21262d;flex-shrink:0}
#tt-live-btn{background:#238636;color:white;border:none;padding:4px 10px;border-radius:4px;cursor:pointer;font-family:inherit;font-size:11px}
#tt-live-btn:hover{background:#2ea043}
#tt-live-btn.dimmed{background:#21262d;color:#8b949e}
#tt-time-label{color:#8b949e;font-size:11px;flex:1;text-align:center}
#tt-event{font-size:12px;color:#c9d1d9}
#tt-event .ev-fault{font-size:14px;font-weight:bold;color:#e6edf3;margin-bottom:6px}
#tt-event .ev-meta{color:#8b949e;margin-bottom:6px;font-size:11px}
#tt-event .ev-rationale{color:#6e7681;font-size:11px}
/* ── Autonomy Matrix ── */
.matrix-table{width:100%;border-collapse:collapse;font-size:12px}
.matrix-table th{background:#1c2230;color:#8b949e;padding:8px 12px;text-align:left;font-weight:normal;text-transform:uppercase;font-size:10px;letter-spacing:1px;border-bottom:1px solid #30363d}
.matrix-table td{padding:10px 12px;border-bottom:1px solid #21262d;vertical-align:middle}
.matrix-table tr:hover td{background:#1c2230}
.matrix-action{color:#e6edf3;font-weight:bold}
.matrix-desc{color:#6e7681;font-size:11px;margin-top:2px}
.matrix-conf input[type=number]{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;padding:3px 6px;border-radius:4px;width:70px;font-family:inherit;font-size:12px}
.toggle-wrap{display:flex;align-items:center;gap:8px}
.toggle{position:relative;width:40px;height:20px;cursor:pointer}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;top:0;left:0;right:0;bottom:0;background:#21262d;border-radius:10px;transition:.2s}
.toggle-slider:before{position:absolute;content:'';height:14px;width:14px;left:3px;bottom:3px;background:#8b949e;border-radius:50%;transition:.2s}
.toggle input:checked + .toggle-slider{background:#238636}
.toggle input:checked + .toggle-slider:before{transform:translateX(20px);background:white}
.toggle-locked{opacity:0.35;cursor:not-allowed}
.matrix-save-btn{background:#238636;color:white;border:none;padding:4px 12px;border-radius:4px;cursor:pointer;font-family:inherit;font-size:12px}
.matrix-save-btn:hover{background:#2ea043}
.matrix-save-btn:disabled{background:#21262d;color:#484f58;cursor:not-allowed}
.matrix-save-status{font-size:11px;color:#3fb950;margin-left:8px}
.matrix-header-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.matrix-header-row h3{color:#e6edf3;font-size:14px}
.safety-notice{background:#1c2230;border:1px solid #30363d;border-radius:4px;padding:8px 12px;font-size:11px;color:#8b949e;margin-bottom:14px}
.safety-notice span{color:#e3b341}
/* ── NLQ ── */
#nlq-input{width:calc(100% - 70px);background:#0d1117;border:1px solid #30363d;color:#c9d1d9;padding:6px 10px;border-radius:4px;font-family:inherit;font-size:13px}
#nlq-btn{background:#238636;color:white;border:none;padding:6px 14px;border-radius:4px;cursor:pointer;font-family:inherit;font-size:13px}
#nlq-btn:hover{background:#2ea043}
#nlq-output{padding:10px;background:#0d1117;border-radius:4px;margin-top:8px;min-height:80px;white-space:pre-wrap;color:#c9d1d9;font-size:12px;line-height:1.6}
.quick-btn{background:#1c2230;color:#58a6ff;border:1px solid #30363d;padding:3px 8px;border-radius:4px;cursor:pointer;font-size:11px;font-family:inherit}
.quick-btn:hover{background:#21262d}
/* ── Stat rows ── */
.stat-row{display:flex;justify-content:space-between;margin-bottom:6px;padding:4px 0;border-bottom:1px solid #21262d}
.stat-label{color:#8b949e}
.stat-val{color:#e6edf3;font-weight:bold}
/* ── Incident Modal (slide-in from right) ── */
#incident-modal{display:none;position:fixed;right:0;top:48px;bottom:0;width:460px;background:#0d1117;border-left:2px solid #30363d;z-index:60;flex-direction:column;overflow:hidden;box-shadow:-8px 0 32px rgba(0,0,0,.7)}
#incident-modal.open{display:flex}
#modal-header{padding:14px 18px;border-bottom:1px solid #30363d;display:flex;justify-content:space-between;align-items:center;flex-shrink:0;background:#161b27;gap:10px}
#modal-title{color:#e6edf3;font-size:14px;font-weight:bold;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.modal-close{background:none;border:none;color:#8b949e;font-size:18px;cursor:pointer;padding:2px 6px;border-radius:4px;line-height:1;flex-shrink:0}
.modal-close:hover{color:#e6edf3;background:#21262d}
#modal-body{flex:1;overflow-y:auto;padding:18px}
#modal-footer{padding:12px 18px;border-top:1px solid #30363d;display:flex;gap:8px;flex-shrink:0;background:#0d1117}
.q-section{margin-bottom:14px;padding:12px 14px;background:#161b27;border-radius:6px;border-left:3px solid #30363d}
.q-section .q-label{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:#8b949e;margin-bottom:6px}
.q-section .q-text{color:#c9d1d9;line-height:1.65;font-size:12px;white-space:pre-wrap}
.pending-action-box{background:#1c2118;border:1px solid #2d3a1a;border-left:3px solid #e3b341;border-radius:5px;padding:12px 14px;margin-bottom:16px}
.approve-btn{flex:1;background:#238636;color:white;border:none;padding:10px;border-radius:5px;cursor:pointer;font-family:inherit;font-size:13px;font-weight:bold}
.approve-btn:hover{background:#2ea043}
.approve-btn:disabled{background:#21262d;color:#484f58;cursor:not-allowed}
.reject-btn{background:#3a0f0f;color:#f85149;border:1px solid #f85149;padding:10px 18px;border-radius:5px;cursor:pointer;font-family:inherit;font-size:13px}
.reject-btn:hover{background:#4a1a1a}
/* ── Proactive action notification banner ── */
#action-notif{display:none;padding:10px 18px;background:#1e1a10;border-bottom:2px solid #e3b341;flex-shrink:0;align-items:center;gap:12px}
#action-notif.visible{display:flex}
.notif-label{color:#e3b341;font-size:12px;font-weight:bold;white-space:nowrap}
.notif-text{color:#c9d1d9;font-size:12px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.notif-btn{background:#238636;color:white;border:none;padding:5px 12px;border-radius:4px;cursor:pointer;font-family:inherit;font-size:12px;white-space:nowrap}
.notif-btn:hover{background:#2ea043}
.notif-dismiss{background:none;border:none;color:#8b949e;cursor:pointer;font-size:16px;padding:2px 6px;line-height:1}
.notif-dismiss:hover{color:#e6edf3}
</style>
</head>
<body>

<!-- ── Header ─────────────────────────────────────────────────────────── -->
<div id="app-header">
  <button id="hamburger" onclick="toggleSidebar()" title="Toggle menu">&#9776;</button>
  <span id="app-title">&#11041; PROJECT AETHER &mdash; NOC COPILOT</span>
  <span class="badge badge-green" id="status-badge">ONLINE</span>
  <span class="badge" id="compliance-badge">CHECKING&hellip;</span>
  <div id="header-right">
    <span class="badge badge-blue" id="alert-count-badge">0 alerts</span>
    <span id="clock"></span>
  </div>
</div>

<!-- ── App body ──────────────────────────────────────────────────────── -->
<div id="app-body">

  <!-- Sidebar -->
  <nav id="sidebar">
    <div class="sidebar-section">Views</div>

    <div class="nav-item active" data-nav="network" onclick="showPanel('network')">
      <span class="nav-icon">&#128302;</span>
      <span class="nav-label">Live Network</span>
    </div>
    <div class="nav-item" data-nav="alerts" onclick="showPanel('alerts')">
      <span class="nav-icon">&#128276;</span>
      <span class="nav-label">Alert Feed</span>
    </div>
    <div class="nav-item" data-nav="copilot" onclick="showPanel('copilot')">
      <span class="nav-icon">&#129302;</span>
      <span class="nav-label">NLQ Copilot</span>
    </div>

    <div class="sidebar-section">Tools</div>

    <div class="nav-item" data-nav="timetravel" onclick="showPanel('timetravel')">
      <span class="nav-icon">&#9197;</span>
      <span class="nav-label">Time-Travel</span>
    </div>
    <div class="nav-item" data-nav="matrix" onclick="showPanel('matrix')">
      <span class="nav-icon">&#9881;</span>
      <span class="nav-label">Autonomy Matrix</span>
    </div>

    <div id="sidebar-footer">
      <div class="stat-row" style="border:0;margin:0;padding:2px 0">
        <span class="stat-label">Models</span>
        <span class="stat-val" id="sb-models" style="font-size:11px">&#8230;</span>
      </div>
      <div class="stat-row" style="border:0;margin:0;padding:2px 0">
        <span class="stat-label">LLM</span>
        <span class="stat-val" id="sb-llm" style="font-size:11px">&#8230;</span>
      </div>
      <div class="stat-row" style="border:0;margin:0;padding:2px 0">
        <span class="stat-label">IKB docs</span>
        <span class="stat-val" id="sb-ikb" style="font-size:11px">&#8230;</span>
      </div>
    </div>
  </nav>

  <!-- Main content -->
  <div id="main-content">

    <!-- ── Proactive action notification banner ────────────────── -->
    <div id="action-notif">
      <span class="notif-label">⚡ Action Required</span>
      <span class="notif-text">
        <span id="notif-action-name" style="font-weight:bold"></span> &mdash;
        <span id="notif-fault"></span>
      </span>
      <button class="notif-btn" onclick="openNotifModal()">Review &amp; Approve</button>
      <button class="notif-dismiss" onclick="dismissNotif()" title="Dismiss">✕</button>
    </div>

    <!-- ── VIEW: Live Network ─────────────────────────────────────── -->
    <div class="view active split-h" id="view-network">
      <div class="col-60">
        <div class="panel" style="height:100%">
          <div class="panel-header">
            Live Topology
            <span id="topo-live-dot" style="color:#3fb950;font-size:14px">&#9679;</span>
          </div>
          <div class="panel-body-nopad">
            <div class="topo-wrap">
              <svg id="topo-canvas" class="topo-svg" viewBox="0 0 460 280" preserveAspectRatio="xMidYMid meet"></svg>
              <div class="topo-legend">
                <span class="legend-item"><span class="legend-line legend-ok"></span> OK</span>
                <span class="legend-item"><span class="legend-line legend-deg" style="height:3px"></span> Degraded</span>
              </div>
            </div>
            <div id="topo-status-bar" style="padding:6px 10px;border-top:1px solid #21262d;font-size:11px;color:#484f58;flex-shrink:0">
              Monitoring live &mdash; waiting for fault events&hellip;
            </div>
          </div>
        </div>
      </div>
      <div class="col-40">
        <div class="panel" style="height:100%">
          <div class="panel-header">
            Recent Alerts
            <span id="mini-feed-count" class="badge badge-yellow">0</span>
          </div>
          <div class="panel-body" id="mini-feed"></div>
        </div>
      </div>
    </div>

    <!-- ── VIEW: Alert Feed ──────────────────────────────────────── -->
    <div class="view split-v" id="view-alerts">
      <div class="panel" style="flex:1">
        <div class="panel-header">
          Alert Feed &mdash; All Events
          <span id="full-feed-count" class="badge badge-yellow">0</span>
        </div>
        <div class="panel-body" id="full-feed"></div>
      </div>
    </div>

    <!-- ── VIEW: NLQ Copilot ─────────────────────────────────────── -->
    <div class="view split-v" id="view-copilot">
      <div class="panel" style="flex:1">
        <div class="panel-header">Copilot &mdash; Natural Language Query (Mistral 7B &bull; Offline)</div>
        <div class="panel-body">
          <div style="display:flex;gap:6px;margin-bottom:8px">
            <input id="nlq-input" placeholder="Ask: 'What will fail next?' / 'How do I fix BGP flap on pe1?'" onkeydown="if(event.key==='Enter')nlqSend()">
            <button id="nlq-btn" onclick="nlqSend()">Ask</button>
          </div>
          <div id="nlq-output">Type a question above or click a quick query. Powered by Mistral 7B (offline).</div>
          <div style="margin-top:12px;border-top:1px solid #21262d;padding-top:10px">
            <div style="color:#8b949e;font-size:11px;margin-bottom:6px;text-transform:uppercase;letter-spacing:1px">Quick Queries</div>
            <div style="display:flex;flex-wrap:wrap;gap:6px">
              <button class="quick-btn" onclick="quickQ('What is likely to fail next and when?')">What fails next?</button>
              <button class="quick-btn" onclick="quickQ('Why is risk elevated on the network?')">Why elevated?</button>
              <button class="quick-btn" onclick="quickQ('How do I fix BGP neighbor flap on pe1?')">Fix BGP flap?</button>
              <button class="quick-btn" onclick="quickQ('Show me mitigation steps for packet loss on the pe1-p1 link')">Fix packet loss?</button>
              <button class="quick-btn" onclick="quickQ('What is the autonomy policy and what actions are auto-executed?')">Autonomy policy?</button>
              <button class="quick-btn" onclick="quickQ('Show the most recent anomaly context packet and explain it')">Last ACP?</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ── VIEW: Time-Travel ─────────────────────────────────────── -->
    <div class="view split-h" id="view-timetravel">
      <div class="col-60">
        <div class="panel" style="height:100%">
          <div class="panel-header">
            Topology Playback
            <span id="tt-snapshot-count" style="font-size:11px;color:#8b949e">0 snapshots</span>
          </div>
          <div class="panel-body-nopad">
            <div class="topo-wrap">
              <svg id="tt-canvas" class="topo-svg" viewBox="0 0 460 280" preserveAspectRatio="xMidYMid meet"></svg>
              <div class="topo-legend">
                <span class="legend-item"><span class="legend-line legend-ok"></span> OK</span>
                <span class="legend-item"><span class="legend-line legend-deg" style="height:3px"></span> Degraded</span>
              </div>
            </div>
            <div id="tt-controls">
              <button id="tt-live-btn" class="dimmed" onclick="resumeLive()">&#9654; Live</button>
              <input type="range" id="tt-slider" min="0" max="0" value="0" style="flex:1" oninput="scrubHistory(+this.value)">
              <span id="tt-time-label">No snapshots yet</span>
            </div>
          </div>
        </div>
      </div>
      <div class="col-40">
        <div class="panel" style="height:100%">
          <div class="panel-header">Event at Selected Time</div>
          <div class="panel-body" id="tt-event">
            <div style="color:#484f58;font-size:12px;margin-top:20px;text-align:center">
              Drag the slider to replay historical topology states.<br><br>
              Each fault event is captured as a snapshot.
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ── VIEW: Autonomy Matrix ─────────────────────────────────── -->
    <div class="view split-v" id="view-matrix">
      <div class="panel" style="flex:1">
        <div class="panel-header">
          Autonomy Policy Matrix
          <span style="font-size:11px;color:#8b949e">Controls what the Edge Policy Engine executes vs. recommends</span>
        </div>
        <div class="panel-body" id="matrix-panel">
          <div class="matrix-header-row">
            <h3>Operator Autonomy Configuration</h3>
            <div>
              <button class="matrix-save-btn" id="matrix-save-btn" onclick="saveMatrix()">Save Changes</button>
              <span class="matrix-save-status" id="matrix-save-status"></span>
            </div>
          </div>
          <div class="safety-notice">
            <span>&#9888; Safety floors enforced in code regardless of this table:</span>
            model disagreement always downgrades to RECOMMEND_ONLY &bull;
            all auto-executed actions are logged and reversible &bull;
            locked rows require operator escalation.
          </div>
          <div id="matrix-table-wrap">Loading policy&hellip;</div>
        </div>
      </div>
    </div>

  </div><!-- #main-content -->

  <!-- ── Incident Modal ─────────────────────────────────────────── -->
  <div id="incident-modal">
    <div id="modal-header">
      <span id="modal-title">Incident Report</span>
      <button class="modal-close" onclick="closeModal()" title="Close">✕</button>
    </div>
    <div id="modal-body">
      <div style="color:#484f58;padding:30px 0;text-align:center">Click an alert to load the incident report.</div>
    </div>
    <div id="modal-footer" style="display:none">
      <button class="approve-btn" id="modal-approve-btn" onclick="approveCurrentAcp()">✓ Execute Action</button>
      <button class="reject-btn" id="modal-reject-btn" onclick="rejectCurrentAcp()">✗ Reject</button>
    </div>
  </div>

</div><!-- #app-body -->

<script>
// ─────────────────────────────────────────────────────────────────────────────
// Navigation / Sidebar
// ─────────────────────────────────────────────────────────────────────────────
function toggleSidebar() {
  document.getElementById('sidebar').classList.toggle('collapsed');
}

function showPanel(name) {
  document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
  document.getElementById('view-' + name).classList.add('active');
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelector('[data-nav="' + name + '"]').classList.add('active');
  if (name === 'matrix') loadMatrix();
  if (name === 'timetravel') refreshTTCanvas();
}

// ─────────────────────────────────────────────────────────────────────────────
// Clock
// ─────────────────────────────────────────────────────────────────────────────
function updateClock() {
  document.getElementById('clock').textContent =
    new Date().toISOString().replace('T', ' ').slice(0, 19) + ' UTC';
}
setInterval(updateClock, 1000); updateClock();

// ─────────────────────────────────────────────────────────────────────────────
// Topology
// ─────────────────────────────────────────────────────────────────────────────
const NODES = {
  'pe1':       {x:160, y:130, cls:'node-pe', label:'PE1'},
  'p1':        {x:230, y:80,  cls:'node-p',  label:'P1'},
  'pe2':       {x:300, y:130, cls:'node-pe', label:'PE2'},
  'ce-branch1':{x:80,  y:210, cls:'node-ce', label:'Branch1'},
  'ce-hub':    {x:155, y:220, cls:'node-ce', label:'Hub'},
  'ce-branch2':{x:310, y:210, cls:'node-ce', label:'Branch2'},
  'ce-dc':     {x:385, y:220, cls:'node-ce', label:'DC'},
};
const LINKS = [
  ['pe1','p1'], ['p1','pe2'], ['pe1','pe2'],
  ['pe1','ce-branch1'], ['pe1','ce-hub'],
  ['pe2','ce-branch2'], ['pe2','ce-dc'],
];
const LINK_SET = new Set(LINKS.map(([a,b]) => a+'→'+b));

// Map feature prefix → node id
const FEAT_PREFIX = [
  ['pe1_','pe1'], ['pe2_','pe2'], ['p1_','p1'],
  ['ce_hub_','ce-hub'], ['ce_branch1_','ce-branch1'],
  ['ce_branch2_','ce-branch2'], ['ce_dc_','ce-dc'],
];

function featuresToNodes(features) {
  const found = new Set();
  for (const feat of (features || [])) {
    for (const [prefix, node] of FEAT_PREFIX) {
      if (feat.startsWith(prefix)) { found.add(node); break; }
    }
  }
  return [...found];
}

function deriveAffectedLinks(acp) {
  // Start with explicit paths_impacted (contains CE-to-CE paths, not PE links)
  const links = new Set();

  // Parse top_features to find affected PE/P nodes
  const affectedNodes = featuresToNodes(acp.top_features || []);

  // Find existing links between those nodes
  for (let i = 0; i < affectedNodes.length; i++) {
    for (let j = i + 1; j < affectedNodes.length; j++) {
      const fwd = affectedNodes[i] + '→' + affectedNodes[j];
      const rev = affectedNodes[j] + '→' + affectedNodes[i];
      if (LINK_SET.has(fwd)) links.add(fwd);
      if (LINK_SET.has(rev)) links.add(rev);
    }
  }

  // pe1→p1 is always highlighted for any non-Healthy fault
  // (the fault streamer targets the pe1-p1 segment)
  if (acp.fault_class && acp.fault_class !== 'Healthy') {
    links.add('pe1→p1');
  }

  return links;
}

function drawTopoOnSvg(svgId, degradedLinks) {
  const svg = document.getElementById(svgId);
  if (!svg) return;
  svg.innerHTML = '';
  LINKS.forEach(([a, b]) => {
    const na = NODES[a], nb = NODES[b];
    const isDeg = degradedLinks.has(a+'→'+b) || degradedLinks.has(b+'→'+a);
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', na.x); line.setAttribute('y1', na.y);
    line.setAttribute('x2', nb.x); line.setAttribute('y2', nb.y);
    line.setAttribute('class', isDeg ? 'link link-degraded' : 'link');
    svg.appendChild(line);
  });
  Object.entries(NODES).forEach(([id, n]) => {
    const g = document.createElementNS('http://www.w3.org/2000/svg', 'g');
    const c = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    c.setAttribute('cx', n.x); c.setAttribute('cy', n.y); c.setAttribute('r', 18);
    c.setAttribute('class', 'node ' + n.cls);
    g.appendChild(c);
    const t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    t.setAttribute('x', n.x); t.setAttribute('y', n.y + 4);
    t.setAttribute('text-anchor', 'middle');
    t.setAttribute('class', 'node-label');
    t.textContent = n.label;
    g.appendChild(t);
    svg.appendChild(g);
  });
}

let currentDegradedLinks = new Set();

function drawTopo() { drawTopoOnSvg('topo-canvas', currentDegradedLinks); }
drawTopo();

function applyFaultToTopo(acp) {
  const newLinks = (acp.fault_class && acp.fault_class !== 'Healthy')
    ? deriveAffectedLinks(acp)
    : new Set();
  currentDegradedLinks = newLinks;
  drawTopo();

  const bar = document.getElementById('topo-status-bar');
  if (bar) {
    if (newLinks.size > 0) {
      const linkList = [...newLinks].join(', ');
      bar.textContent = `DEGRADED: ${acp.fault_class} — affected links: ${linkList}`;
      bar.style.color = '#f85149';
    } else {
      bar.textContent = 'All links nominal';
      bar.style.color = '#3fb950';
    }
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Time-Travel
// ─────────────────────────────────────────────────────────────────────────────
const topoHistory = [];
let ttMode = 'live'; // 'live' | 'scrub'
let ttIdx = 0;

function addTopoSnapshot(acp) {
  const snap = {
    timestamp  : acp.timestamp || acp.acp_id,
    acp_id     : acp.acp_id,
    fault_class: acp.fault_class || 'Unknown',
    severity   : acp.severity || 'MEDIUM',
    confidence : acp.confidence,
    rationale  : acp.rationale || '',
    ttf        : acp.ttf,
    degradedLinks: deriveAffectedLinks(acp),
  };
  topoHistory.push(snap);

  const slider = document.getElementById('tt-slider');
  if (slider) {
    slider.max = topoHistory.length - 1;
    if (ttMode === 'live') {
      slider.value = topoHistory.length - 1;
      ttIdx = topoHistory.length - 1;
      refreshTTCanvas();
    }
  }
  document.getElementById('tt-snapshot-count').textContent =
    topoHistory.length + ' snapshot' + (topoHistory.length === 1 ? '' : 's');
}

function scrubHistory(idx) {
  ttMode = 'scrub';
  ttIdx = +idx;
  document.getElementById('tt-live-btn').classList.remove('dimmed');
  refreshTTCanvas();
}

function resumeLive() {
  ttMode = 'live';
  ttIdx = topoHistory.length - 1;
  const slider = document.getElementById('tt-slider');
  if (slider) slider.value = ttIdx;
  document.getElementById('tt-live-btn').classList.add('dimmed');
  refreshTTCanvas();
}

function refreshTTCanvas() {
  if (topoHistory.length === 0) {
    drawTopoOnSvg('tt-canvas', new Set());
    document.getElementById('tt-time-label').textContent = 'No snapshots yet';
    return;
  }
  const snap = topoHistory[ttIdx] || topoHistory[topoHistory.length - 1];
  drawTopoOnSvg('tt-canvas', snap.degradedLinks);

  const ts = (snap.timestamp || '').slice(11, 19);
  document.getElementById('tt-time-label').textContent =
    (ttMode === 'live' ? '▶ Live: ' : '') + (snap.timestamp || '').slice(0, 19) + ' UTC';

  const conf = snap.confidence != null ? (snap.confidence * 100).toFixed(0) + '%' : '?';
  const ttf  = snap.ttf != null && snap.ttf >= 0 ? snap.ttf.toFixed(0) + 's' : '?';
  const sevColor = {CRITICAL:'#f85149',HIGH:'#e3b341',MEDIUM:'#58a6ff',LOW:'#3fb950'}[snap.severity] || '#8b949e';
  document.getElementById('tt-event').innerHTML = `
    <div class="ev-fault" style="color:${sevColor}">${snap.fault_class}</div>
    <div class="ev-meta">
      <span class="badge badge-${snap.severity === 'CRITICAL' ? 'red' : snap.severity === 'HIGH' ? 'yellow' : 'blue'}">${snap.severity}</span>
      &nbsp;conf=${conf} &nbsp;ttf=${ttf}<br>
      <span style="color:#484f58">${(snap.timestamp||'').replace('T',' ').slice(0,19)} UTC</span>
    </div>
    <div class="ev-rationale">${snap.rationale.slice(0, 300)}</div>
    ${snap.degradedLinks.size > 0
      ? '<div style="margin-top:10px;font-size:11px;color:#8b949e">Degraded links: <span style="color:#f85149">' + [...snap.degradedLinks].join(', ') + '</span></div>'
      : '<div style="margin-top:10px;font-size:11px;color:#3fb950">All links nominal</div>'}
  `;
}

// ─────────────────────────────────────────────────────────────────────────────
// Alert feed
// ─────────────────────────────────────────────────────────────────────────────
let totalAlertCount = 0;
const seenAcpIds = new Set();

function acpToAlert(entry) {
  const ml  = entry.ml_analysis  || {};
  const cor = entry.corroboration || {};
  return {
    acp_id        : entry.acp_id,
    timestamp     : entry.timestamp,
    severity      : entry.severity || 'MEDIUM',
    fault_class   : ml.predicted_fault_class || entry.fault_class || 'Unknown',
    confidence    : ml.confidence_score != null ? ml.confidence_score : entry.confidence,
    ttf           : ml.estimated_time_to_failure_sec != null ? ml.estimated_time_to_failure_sec : entry.ttf,
    execution_mode: cor.execution_mode || entry.execution_mode || 'RECOMMEND_ONLY',
    rationale     : cor.rationale || entry.rationale || '',
    action        : cor.recommended_action || entry.action || 'NO_ACTION',
    top_features  : entry.top_features || [],
    paths_impacted: (entry.graph_analysis || {}).paths_impacted || entry.paths_impacted || [],
  };
}

function renderAlert(acp, prepend) {
  totalAlertCount++;
  document.getElementById('alert-count-badge').textContent = totalAlertCount + ' alert' + (totalAlertCount === 1 ? '' : 's');
  document.getElementById('mini-feed-count').textContent = totalAlertCount;
  document.getElementById('full-feed-count').textContent = totalAlertCount;

  const conf = acp.confidence != null ? (acp.confidence * 100).toFixed(0) + '%' : '?';
  const ttf  = acp.ttf != null && acp.ttf >= 0 ? acp.ttf.toFixed(0) + 's' : '?';
  const ts   = (acp.timestamp || '').slice(11, 19);

  function makeDiv() {
    const div = document.createElement('div');
    div.className = 'alert alert-' + (acp.severity || 'MEDIUM');
    div.innerHTML = `
      <div class="alert-title">
        <span class="severity-dot dot-${acp.severity||'MEDIUM'}"></span>
        ${acp.fault_class || 'Unknown'} &mdash; ${acp.severity || '?'}
      </div>
      <div class="alert-meta">conf=${conf} | ttf=${ttf} | ${acp.execution_mode||'?'} | ${ts} UTC</div>
      <div class="alert-rationale">${(acp.rationale || '').slice(0, 120)}</div>`;
    div.onclick = () => openIncidentModal(acp);
    return div;
  }

  const miniDiv = makeDiv();
  const fullDiv = makeDiv();
  const mini = document.getElementById('mini-feed');
  const full = document.getElementById('full-feed');
  if (prepend) {
    mini.prepend(miniDiv);
    full.prepend(fullDiv);
  } else {
    mini.appendChild(miniDiv);
    full.appendChild(fullDiv);
  }

  // Update live topology and snapshot history
  applyFaultToTopo(acp);
  addTopoSnapshot(acp);

  // Proactive prompt for RECOMMEND_ONLY actions on live alerts
  if (prepend && acp.execution_mode === 'RECOMMEND_ONLY' && acp.action && acp.action !== 'NO_ACTION') {
    showActionNotif(acp);
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Load history on startup
// ─────────────────────────────────────────────────────────────────────────────
async function loadHistory() {
  try {
    const r = await fetch('/api/acps?limit=50');
    const d = await r.json();
    if (!d.acps || !d.acps.length) return;
    // Render oldest→newest (they're sorted ascending) to build correct snapshot history
    for (const entry of d.acps) {
      if (!seenAcpIds.has(entry.acp_id)) {
        seenAcpIds.add(entry.acp_id);
        renderAlert(acpToAlert(entry), false);
      }
    }
    // After loading, scroll both feeds to top (newest is what the operator wants to see)
    document.getElementById('mini-feed').scrollTop = 0;
    document.getElementById('full-feed').scrollTop = 0;
  } catch(e) { console.error('loadHistory:', e); }
}

// ─────────────────────────────────────────────────────────────────────────────
// Poll /api/acps every 4 s (fallback for missed WS messages)
// ─────────────────────────────────────────────────────────────────────────────
async function pollAlerts() {
  try {
    const r = await fetch('/api/acps?limit=30');
    const d = await r.json();
    if (!d.acps) return;
    // Walk newest-first; break once we hit a seen id
    const newest = [...d.acps].reverse();
    const batch = [];
    for (const entry of newest) {
      if (seenAcpIds.has(entry.acp_id)) break;
      seenAcpIds.add(entry.acp_id);
      batch.push(acpToAlert(entry));
    }
    // Render in chronological order (oldest first so snapshot history is correct)
    for (const acp of batch.reverse()) {
      renderAlert(acp, true);
    }
  } catch(e) {}
}

// ─────────────────────────────────────────────────────────────────────────────
// WebSocket — primary live delivery path
// ─────────────────────────────────────────────────────────────────────────────
function connectWS() {
  const ws = new WebSocket('ws://' + location.host + '/ws/alerts');
  ws.onmessage = e => {
    try {
      const acp = JSON.parse(e.data);
      if (!seenAcpIds.has(acp.acp_id)) {
        seenAcpIds.add(acp.acp_id);
        renderAlert(acp, true);
      }
    } catch(err) {}
  };
  ws.onclose = () => setTimeout(connectWS, 3000);
}

loadHistory();
connectWS();
setInterval(pollAlerts, 4000);
loadMatrix();  // Pre-load policy so the matrix is ready before user navigates to it

// ─────────────────────────────────────────────────────────────────────────────
// NLQ Copilot
// ─────────────────────────────────────────────────────────────────────────────
async function nlqSend() {
  const q = document.getElementById('nlq-input').value.trim();
  if (!q) return;
  const out = document.getElementById('nlq-output');
  out.textContent = '⏳ Thinking…';
  try {
    const r = await fetch('/api/nlq', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question: q}),
    });
    const d = await r.json();
    out.textContent = d.answer || d.error || 'No response';
  } catch(e) { out.textContent = 'Error: ' + e; }
}

function quickQ(q) {
  document.getElementById('nlq-input').value = q;
  nlqSend();
}

// ─────────────────────────────────────────────────────────────────────────────
// Incident Modal
// ─────────────────────────────────────────────────────────────────────────────
let currentModalAcp = null;

function openIncidentModal(acp) {
  currentModalAcp = acp;
  const modal = document.getElementById('incident-modal');
  modal.classList.add('open');

  const action = acp.action || 'NO_ACTION';
  const isRecommend = acp.execution_mode === 'RECOMMEND_ONLY' && action && action !== 'NO_ACTION';
  const conf = acp.confidence != null ? (acp.confidence * 100).toFixed(0) + '%' : '?';
  const ttf  = acp.ttf != null && acp.ttf >= 0 ? acp.ttf.toFixed(0) + 's' : '?';
  const ts   = (acp.timestamp || '').replace('T', ' ').slice(0, 19);
  const sevColor = {CRITICAL:'#f85149',HIGH:'#e3b341',MEDIUM:'#58a6ff',LOW:'#3fb950'}[acp.severity] || '#8b949e';
  const sevBadge = acp.severity === 'CRITICAL' ? 'red' : acp.severity === 'HIGH' ? 'yellow' : 'blue';

  document.getElementById('modal-title').textContent = (acp.fault_class || 'Incident') + ' — ' + (acp.severity || '?');

  const footer = document.getElementById('modal-footer');
  const approveBtn = document.getElementById('modal-approve-btn');
  const rejectBtn  = document.getElementById('modal-reject-btn');
  footer.style.display = isRecommend ? 'flex' : 'none';
  approveBtn.textContent = '✓ Execute: ' + action;
  approveBtn.disabled = false;
  rejectBtn.disabled = false;
  rejectBtn.textContent = '✗ Reject';

  const pendingBox = isRecommend ? `
    <div class="pending-action-box">
      <div style="font-size:10px;color:#e3b341;text-transform:uppercase;letter-spacing:1px;margin-bottom:6px">⚡ Awaiting Operator Approval</div>
      <div style="color:#e6edf3;font-weight:bold;font-size:14px;margin-bottom:4px">${action}</div>
      <div style="color:#8b949e;font-size:11px;line-height:1.5">${(acp.rationale || '').slice(0, 250)}</div>
    </div>` : '';

  document.getElementById('modal-body').innerHTML = `
    <div style="margin-bottom:16px">
      <div style="font-size:16px;font-weight:bold;color:${sevColor};margin-bottom:8px">${acp.fault_class || 'Unknown'}</div>
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:6px">
        <span class="badge badge-${sevBadge}">${acp.severity || '?'}</span>
        <span class="badge badge-blue">conf ${conf}</span>
        <span class="badge badge-yellow">TTF ${ttf}</span>
        <span class="badge" style="background:#1c2230;color:#8b949e">${acp.execution_mode || '?'}</span>
      </div>
      <div style="font-size:11px;color:#484f58">${ts} UTC &bull; ${acp.acp_id || ''}</div>
    </div>
    ${pendingBox}
    <div id="modal-explain-body">
      <div style="color:#484f58;text-align:center;padding:24px 0">⏳ Fetching incident analysis…</div>
    </div>`;

  fetchExplanation(acp.acp_id);
}

async function fetchExplanation(acp_id) {
  if (!acp_id) return;
  try {
    const r = await fetch('/api/explain/' + encodeURIComponent(acp_id));
    const d = await r.json();
    const el = document.getElementById('modal-explain-body');
    if (!el) return;
    if (d.error) {
      el.innerHTML = '<div style="color:#f85149;padding:10px">' + d.error + '</div>';
      return;
    }
    const srcTag = d.source === 'ollama' ? '🤖 Mistral 7B' : '📋 Structured fallback';
    el.innerHTML = `
      <div class="q-section">
        <div class="q-label">Q1 — What is likely to fail next?</div>
        <div class="q-text">${d.q1_what_fails || '—'}</div>
      </div>
      <div class="q-section">
        <div class="q-label">Q2 — Why is risk elevated?</div>
        <div class="q-text">${d.q2_why_risk || '—'}</div>
      </div>
      <div class="q-section">
        <div class="q-label">Q3 — Corrective action to take</div>
        <div class="q-text">${d.q3_action || '—'}</div>
      </div>
      <div style="margin-top:12px;padding:8px 10px;background:#161b27;border-radius:4px;font-size:10px;color:#484f58">
        ${srcTag} &bull; ${d.acp_id || acp_id}
      </div>`;
  } catch(e) {
    const el = document.getElementById('modal-explain-body');
    if (el) el.innerHTML = '<div style="color:#f85149;padding:10px">Error: ' + e + '</div>';
  }
}

function closeModal() {
  document.getElementById('incident-modal').classList.remove('open');
  currentModalAcp = null;
}

async function approveCurrentAcp() {
  if (!currentModalAcp) return;
  const btn = document.getElementById('modal-approve-btn');
  btn.disabled = true; btn.textContent = 'Executing…';
  try {
    const r = await fetch('/api/feedback', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({acp_id: currentModalAcp.acp_id, feedback: 'accepted'}),
    });
    const d = await r.json();
    btn.textContent = '✓ Approved — Action Queued';
    btn.style.background = '#0f3a1a';
    setTimeout(() => closeModal(), 2200);
  } catch(e) {
    btn.textContent = '✓ Execute: ' + (currentModalAcp.action || '?');
    btn.disabled = false;
  }
}

async function rejectCurrentAcp() {
  if (!currentModalAcp) return;
  const btn = document.getElementById('modal-reject-btn');
  btn.disabled = true; btn.textContent = 'Rejecting…';
  try {
    await fetch('/api/feedback', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({acp_id: currentModalAcp.acp_id, feedback: 'rejected'}),
    });
    closeModal();
  } catch(e) {
    btn.disabled = false; btn.textContent = '✗ Reject';
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// Proactive action notification
// ─────────────────────────────────────────────────────────────────────────────
let pendingNotifAcp = null;

function showActionNotif(acp) {
  pendingNotifAcp = acp;
  document.getElementById('notif-action-name').textContent = acp.action || '?';
  document.getElementById('notif-fault').textContent =
    (acp.fault_class || 'Fault') + ' — conf ' +
    (acp.confidence != null ? (acp.confidence * 100).toFixed(0) + '%' : '?');
  document.getElementById('action-notif').classList.add('visible');
}

function dismissNotif() {
  document.getElementById('action-notif').classList.remove('visible');
  pendingNotifAcp = null;
}

function openNotifModal() {
  if (!pendingNotifAcp) return;
  const acp = pendingNotifAcp;
  dismissNotif();
  openIncidentModal(acp);
}

// ─────────────────────────────────────────────────────────────────────────────
// Autonomy Matrix
// ─────────────────────────────────────────────────────────────────────────────
const LOCKED_ACTIONS = new Set(['CORE_PATH_FAILOVER', 'NODE_ISOLATION', 'NO_ACTION']);
let currentPolicy = {};

async function loadMatrix() {
  const wrap = document.getElementById('matrix-table-wrap');
  if (wrap) wrap.innerHTML = '<div style="color:#484f58;padding:8px">Loading policy…</div>';
  try {
    const r = await fetch('/api/policy');
    if (!r.ok) throw new Error('HTTP ' + r.status);
    currentPolicy = await r.json();
    renderMatrix(currentPolicy);
  } catch(e) {
    console.error('[Aether] loadMatrix failed:', e);
    const el = document.getElementById('matrix-table-wrap');
    if (el) el.innerHTML = '<div style="color:#f85149;padding:8px">⚠ Failed to load policy: ' + e.message + '<br><button onclick="loadMatrix()" style="margin-top:8px;padding:4px 10px;background:#238636;color:white;border:none;border-radius:4px;cursor:pointer">Retry</button></div>';
  }
}

function renderMatrix(policy) {
  const ACTION_ORDER = [
    'REROUTE_BRANCH', 'QOS_SHAPE_QUEUE', 'CORE_PATH_FAILOVER', 'NODE_ISOLATION', 'NO_ACTION'
  ];
  let html = `<table class="matrix-table">
    <thead><tr>
      <th>Action Class</th>
      <th>Description</th>
      <th>Min Confidence</th>
      <th>Auto-Execute</th>
      <th></th>
    </tr></thead><tbody>`;

  for (const action of ACTION_ORDER) {
    const pol = policy[action] || {};
    const locked = LOCKED_ACTIONS.has(action);
    const conf = (pol.min_conf != null ? (pol.min_conf * 100).toFixed(0) : '100');
    const auto = pol.auto_execute || false;
    html += `<tr id="row-${action}">
      <td>
        <div class="matrix-action">${action}</div>
        ${locked ? '<div style="font-size:10px;color:#484f58;margin-top:2px">&#128274; LOCKED</div>' : ''}
      </td>
      <td><div class="matrix-desc">${pol.description || '—'}</div></td>
      <td class="matrix-conf">
        <input type="number" id="conf-${action}" value="${conf}" min="0" max="100" step="5"
          ${locked ? 'disabled' : ''}
          style="${locked ? 'opacity:0.35;cursor:not-allowed' : ''}">
        <span style="color:#8b949e;font-size:11px">%</span>
      </td>
      <td>
        <div class="toggle-wrap ${locked ? 'toggle-locked' : ''}">
          <label class="toggle">
            <input type="checkbox" id="auto-${action}" ${auto ? 'checked' : ''} ${locked ? 'disabled' : ''}>
            <span class="toggle-slider"></span>
          </label>
          <span style="font-size:11px;color:#8b949e" id="auto-label-${action}">${auto ? 'AUTO' : 'RECOMMEND'}</span>
        </div>
      </td>
      <td>
        ${!locked ? `<button class="matrix-save-btn" onclick="saveRow('${action}')" id="save-${action}">Apply</button>` : ''}
      </td>
    </tr>`;
  }

  html += '</tbody></table>';
  document.getElementById('matrix-table-wrap').innerHTML = html;

  // Wire toggle labels
  for (const action of ACTION_ORDER) {
    if (LOCKED_ACTIONS.has(action)) continue;
    const cb = document.getElementById('auto-' + action);
    if (cb) cb.addEventListener('change', () => {
      document.getElementById('auto-label-' + action).textContent = cb.checked ? 'AUTO' : 'RECOMMEND';
    });
  }
}

async function saveRow(action) {
  const confInput = document.getElementById('conf-' + action);
  const autoInput = document.getElementById('auto-' + action);
  const btn = document.getElementById('save-' + action);
  if (!confInput || !autoInput) return;

  const minConf = parseFloat(confInput.value) / 100;
  const autoExec = autoInput.checked;

  btn.disabled = true;
  btn.textContent = 'Saving…';

  try {
    const r = await fetch('/api/policy', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, min_conf: minConf, auto_execute: autoExec}),
    });
    const d = await r.json();
    if (d.status === 'ok') {
      btn.textContent = '✓ Saved';
      btn.style.background = '#0f3a1a';
      btn.style.color = '#3fb950';
      setTimeout(() => {
        btn.textContent = 'Apply'; btn.disabled = false;
        btn.style.background = ''; btn.style.color = '';
      }, 2000);
    } else {
      throw new Error(d.detail || 'Unknown error');
    }
  } catch(e) {
    btn.textContent = 'Error'; btn.style.background = '#3a0f0f'; btn.style.color = '#f85149';
    setTimeout(() => { btn.textContent = 'Apply'; btn.disabled = false; btn.style.background=''; btn.style.color=''; }, 2500);
  }
}

async function saveMatrix() {
  // Save all non-locked rows
  const ALL_ACTIONS = ['REROUTE_BRANCH', 'QOS_SHAPE_QUEUE'];
  for (const action of ALL_ACTIONS) await saveRow(action);
}

// ─────────────────────────────────────────────────────────────────────────────
// Status polling (updates sidebar footer + header badge)
// ─────────────────────────────────────────────────────────────────────────────
async function pollStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    document.getElementById('sb-models').textContent = s.models_loaded ? '✓ loaded' : '✗ missing';
    document.getElementById('sb-llm').textContent    = s.llm_online   ? '✓ online' : '⚠ offline';
    document.getElementById('sb-ikb').textContent    = s.ikb_docs > 0 ? s.ikb_docs + ' docs' : '✗ empty';
    const cb = document.getElementById('compliance-badge');
    if (s.air_gap_compliant === true) {
      cb.textContent = '✓ AIR-GAPPED'; cb.className = 'badge badge-green';
    } else if (s.air_gap_compliant === false) {
      cb.textContent = '⚠ NOT COMPLIANT'; cb.className = 'badge badge-red';
    }
  } catch(e) {}
}
setInterval(pollStatus, 5000); pollStatus();
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

    from airgap_compliance import _probe
    dns_result = _probe("8.8.8.8", 53)
    compliant = not dns_result["reachable"]

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
        for path in files[-limit * 2:]:
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


# ── Autonomy Policy Matrix ────────────────────────────────────────────────────

@app.get("/api/policy")
async def get_policy():
    from taxonomy import ACTION_POLICY
    overrides = _load_policy_overrides()
    result = {}
    for action, pol in ACTION_POLICY.items():
        merged = {**pol}
        if action in overrides:
            merged.update(overrides[action])
        result[action] = merged
    return result


class PolicyUpdate(BaseModel):
    action: str
    min_conf: float
    auto_execute: bool


@app.put("/api/policy")
async def update_policy(req: PolicyUpdate):
    from taxonomy import ACTION_POLICY
    LOCKED = {"CORE_PATH_FAILOVER", "NODE_ISOLATION", "NO_ACTION"}
    if req.action in LOCKED:
        raise HTTPException(status_code=403, detail=f"{req.action} is a locked policy row")
    if req.action not in ACTION_POLICY:
        raise HTTPException(status_code=404, detail=f"Unknown action: {req.action}")
    if not (0.0 <= req.min_conf <= 1.0):
        raise HTTPException(status_code=422, detail="min_conf must be between 0 and 1")

    overrides = _load_policy_overrides()
    overrides[req.action] = {
        "min_conf": req.min_conf,
        "auto_execute": req.auto_execute,
    }
    with open(POLICY_OVERRIDE, "w") as f:
        json.dump(overrides, f, indent=2)

    # Apply to running taxonomy in this process
    import taxonomy
    taxonomy.ACTION_POLICY[req.action]["min_conf"] = req.min_conf
    taxonomy.ACTION_POLICY[req.action]["auto_execute"] = req.auto_execute

    return {
        "status": "ok",
        "action": req.action,
        "policy": overrides[req.action],
    }


# ── NLQ ──────────────────────────────────────────────────────────────────────

class NLQRequest(BaseModel):
    question: str


@app.post("/api/nlq")
async def nlq(req: NLQRequest):
    copilot = _get_copilot()
    if copilot is None:
        return {"answer": "LLM copilot unavailable. Check Ollama installation.", "source": "error"}
    async with _llm_lock:
        answer = await asyncio.get_event_loop().run_in_executor(
            None, copilot.query, req.question
        )
    return {"answer": answer, "source": "copilot"}


@app.get("/api/explain/{acp_id}")
async def explain_acp(acp_id: str):
    """Load the full ACP JSON by ID and generate a structured Q1/Q2/Q3 incident report."""
    import glob
    import types
    copilot = _get_copilot()
    if copilot is None:
        return {"error": "LLM copilot unavailable — start Ollama and try again", "acp_id": acp_id}

    # Scan acp_logs/ for the file whose acp_id field matches (filenames include timestamps, not just the id)
    acp_entry = None
    if os.path.exists(ACP_DIR):
        for path in sorted(glob.glob(os.path.join(ACP_DIR, "*.json")), reverse=True):
            try:
                with open(path) as f:
                    entry = json.load(f)
                if entry.get("acp_id") == acp_id:
                    acp_entry = entry
                    break
            except Exception:
                pass

    if acp_entry is None:
        return {"error": f"ACP {acp_id!r} not found in logs", "acp_id": acp_id}

    acp_obj = types.SimpleNamespace(**acp_entry)
    async with _llm_lock:
        result = await asyncio.get_event_loop().run_in_executor(
            None, copilot.explain, acp_obj
        )
    return result


# ── Feedback ─────────────────────────────────────────────────────────────────

class FeedbackRequest(BaseModel):
    acp_id: str
    feedback: str


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
            await websocket.receive_text()
    except WebSocketDisconnect:
        if websocket in _connected_ws:
            _connected_ws.remove(websocket)


async def broadcast_acp(acp_dict: dict):
    dead = []
    for ws in _connected_ws:
        try:
            await ws.send_text(json.dumps(acp_dict))
        except Exception:
            dead.append(ws)
    for ws in dead:
        if ws in _connected_ws:
            _connected_ws.remove(ws)


async def _tail_acp_log():
    """Watch acp_logs/ for new JSON files, broadcast to WebSocket clients."""
    import glob
    seen = set()
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
                        "top_features"  : entry.get("top_features", []),
                        "paths_impacted": (entry.get("graph_analysis") or {}).get("paths_impacted", []),
                    }
                    await broadcast_acp(ws_msg)
                    seen.add(path)
                except Exception:
                    pass
        except Exception:
            pass


@app.on_event("startup")
async def startup():
    asyncio.create_task(_tail_acp_log())
    print("[*] Aether NOC Dashboard v5.0 — http://localhost:8080")
    print("[*] Watching acp_logs/ → WebSocket live feed active")


if __name__ == "__main__":
    import uvicorn
    sys.path.insert(0, os.path.join(REPO_ROOT, "..", "phase3-models"))
    sys.path.insert(0, os.path.join(REPO_ROOT, "..", "phase4-llm"))
    uvicorn.run(app, host="0.0.0.0", port=8080, reload=False, log_level="info")
