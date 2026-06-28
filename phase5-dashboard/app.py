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


# ── Remediation step library ──────────────────────────────────────────────────
# Commands use Containerlab convention: docker exec clab-aether-{node} {cmd}
# {node} and {node_ip} are substituted at request time from the ACP's top_features.

_REMEDIATION_STEPS = {
    "REROUTE_BRANCH": {
        "title": "Reroute traffic around the degraded branch link",
        "steps": [
            ("Review current routing table before any change",
             "docker exec clab-aether-{node} vtysh -c 'show ip route'"),
            ("Raise OSPF cost on degraded interface — traffic shifts immediately",
             "docker exec clab-aether-{node} vtysh -c 'conf t' -c 'int eth0' -c 'ip ospf cost 200' -c 'end' -c 'write'"),
            ("Soft-reset BGP to pull fresh routes via backup path",
             "docker exec clab-aether-{node} vtysh -c 'clear bgp * soft'"),
            ("Confirm traffic rerouted — BGP best-path should change",
             "docker exec clab-aether-{node} vtysh -c 'show bgp ipv4 unicast' | head -30"),
            ("Monitor — confirm error counters stop rising",
             "docker exec clab-aether-{node} vtysh -c 'show interface eth0' | grep -E 'drops|errors|resets'"),
        ],
    },
    "QOS_SHAPE_QUEUE": {
        "title": "Shape non-critical queues to relieve congestion",
        "steps": [
            ("Check current queue depth and drop statistics",
             "docker exec clab-aether-{node} tc -s qdisc show dev eth0"),
            ("Install HTB root qdisc (safe to run again — second run is a no-op)",
             "docker exec clab-aether-{node} tc qdisc add dev eth0 root handle 1: htb default 30 2>/dev/null || true"),
            ("Reserve 800 Mbps for critical traffic (VoIP / BGP control plane)",
             "docker exec clab-aether-{node} tc class add dev eth0 parent 1: classid 1:10 htb rate 800mbit ceil 1000mbit"),
            ("Cap best-effort bulk traffic at 200 Mbps",
             "docker exec clab-aether-{node} tc class add dev eth0 parent 1: classid 1:30 htb rate 200mbit ceil 400mbit"),
            ("Verify discipline is active and rates are being enforced",
             "docker exec clab-aether-{node} tc -s class show dev eth0"),
        ],
    },
    "CORE_PATH_FAILOVER": {
        "title": "Fail primary core link to backup path — APPROVE REQUIRED",
        "steps": [
            ("Read BGP neighbor state before making any change",
             "docker exec clab-aether-{node} vtysh -c 'show bgp summary'"),
            ("Soft-reset all BGP sessions — flushes stale routes, non-disruptive",
             "docker exec clab-aether-{node} vtysh -c 'clear bgp * soft'"),
            ("Poison the flapping link: raise OSPF cost to max (65535)",
             "docker exec clab-aether-{node} vtysh -c 'conf t' -c 'int eth0' -c 'ip ospf cost 65535' -c 'end' -c 'write'"),
            ("Shut down the flapping BGP neighbor on pe1 (primary core path)",
             "docker exec clab-aether-pe1 vtysh -c 'conf t' -c 'router bgp 65001' -c 'neighbor 192.168.12.2 shutdown' -c 'end' -c 'write'"),
            ("Verify traffic has shifted to pe2 backup path",
             "docker exec clab-aether-pe2 vtysh -c 'show bgp summary' && docker exec clab-aether-{node} vtysh -c 'show ip route'"),
            ("Confirm SLA recovery — drops and errors should trend to zero",
             "docker exec clab-aether-{node} vtysh -c 'show interface eth0' | grep -E 'drops|errors|resets'"),
        ],
    },
    "NODE_ISOLATION": {
        "title": "Isolate suspected compromised node — APPROVE REQUIRED",
        "steps": [
            ("Confirm the suspected node before acting — check for anomalous routes",
             "docker exec clab-aether-{node} vtysh -c 'show bgp summary' && docker exec clab-aether-{node} vtysh -c 'show ip route'"),
            ("Drop all forwarded traffic from and to the suspect node",
             "iptables -I FORWARD -s {node_ip} -j DROP && iptables -I FORWARD -d {node_ip} -j DROP"),
            ("Shut BGP neighbor on all PE routers that peer with this node",
             "docker exec clab-aether-pe1 vtysh -c 'conf t' -c 'router bgp 65001' -c 'neighbor {node_ip} shutdown' -c 'end'"),
            ("Mark OSPF interface passive — stops routing updates immediately",
             "docker exec clab-aether-{node} vtysh -c 'conf t' -c 'router ospf' -c 'passive-interface eth0' -c 'end'"),
            ("Capture traffic snapshot for forensics (runs in background, 50k pkts)",
             "docker exec clab-aether-{node} tcpdump -i eth0 -w /tmp/isolation_$(date +%s).pcap -c 50000 &"),
        ],
    },
    "NO_ACTION": {
        "title": "System nominal — no corrective action required",
        "steps": [
            ("Confirm all nodes are healthy",
             "docker exec clab-aether-pe1 vtysh -c 'show bgp summary' && docker exec clab-aether-pe2 vtysh -c 'show bgp summary'"),
            ("Check recent ACP history and false-positive rate",
             "python3 phase3-models/feedback_cli.py --stats"),
        ],
    },
}

_NODE_IPS = {
    "pe1": "172.20.20.2", "pe2": "172.20.20.3", "p1": "172.20.20.4",
    "ce-hub": "172.20.20.7", "ce-branch1": "172.20.20.5",
    "ce-branch2": "172.20.20.6", "ce-dc": "172.20.20.8",
}


def _build_remediation(action: str, top_features: list) -> dict:
    """Return node-specific remediation steps derived from the ACP's top_features."""
    entry = _REMEDIATION_STEPS.get(action, _REMEDIATION_STEPS["NO_ACTION"])

    # Extract primary affected node by counting feature prefixes
    node   = "pe1"
    counts = {p: 0 for p in _NODE_IPS}
    for feat in top_features:
        for pfx in _NODE_IPS:
            key = pfx.replace("-", "_")
            if feat.startswith(key + "_") or feat.startswith(pfx + "_"):
                counts[pfx] += 1
    if any(counts.values()):
        node = max(counts, key=counts.get)

    node_ip = _NODE_IPS.get(node, "172.20.20.2")

    steps = [
        {"description": desc,
         "command": cmd.replace("{node}", node).replace("{node_ip}", node_ip).replace("{iface}", "eth0")}
        for desc, cmd in entry["steps"]
    ]

    return {
        "action":        action,
        "title":         entry["title"],
        "affected_node": node,
        "node_ip":       node_ip,
        "steps":         steps,
    }


# Simulated live traffic state (random walk per link, updated on each /api/metrics/live call)
import random as _random
_link_util: dict[str, float] = {
    "pe1→p1": 14.0, "p1→pe2": 11.0, "pe1→ce-hub": 7.0,
    "pe2→ce-branch1": 5.0, "pe2→ce-branch2": 8.0, "pe1→ce-dc": 17.0,
}


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
body{background:#111217;color:#c4c9d4;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;font-size:13px;display:flex;flex-direction:column}
/* ── Header ── */
#app-header{background:#181b1f;border-bottom:1px solid #2c3035;padding:0 18px;display:flex;align-items:center;gap:14px;flex-shrink:0;height:46px;z-index:10}
#hamburger{background:none;border:none;color:#6d7989;font-size:18px;cursor:pointer;padding:4px 7px;border-radius:3px;line-height:1;flex-shrink:0}
#hamburger:hover{background:#21262d;color:#c4c9d4}
#app-title{color:#c4c9d4;font-size:13px;font-weight:600;white-space:nowrap;letter-spacing:0.2px}
.badge{padding:2px 7px;border-radius:3px;font-size:11px;font-weight:600;white-space:nowrap}
.badge-green{background:#1c2e1c;color:#57a84a;border:1px solid #2a442a}
.badge-red{background:#2e1c1c;color:#d05a52;border:1px solid #442a2a}
.badge-yellow{background:#2e2a1c;color:#c9963e;border:1px solid #44381c}
.badge-blue{background:#1c222e;color:#5189c8;border:1px solid #1c3052}
#header-right{margin-left:auto;display:flex;align-items:center;gap:10px}
#clock{color:#4a5260;font-size:11px;font-family:'Consolas','Courier New',monospace}
/* ── System metrics bar ── */
#sysbar{background:#14171b;border-bottom:1px solid #22262b;padding:4px 18px;display:flex;align-items:center;gap:20px;flex-shrink:0;font-size:11px;font-family:'Consolas','Courier New',monospace}
.sysbar-item{display:flex;align-items:center;gap:6px}
.sysbar-label{color:#3d4552;font-size:10px;text-transform:uppercase;letter-spacing:0.5px}
.sysbar-val{color:#7a8494;font-weight:600}
.sysbar-val.hi{color:#c9963e}
.sysbar-val.crit{color:#d05a52}
.sysbar-val.ok{color:#57a84a}
.sysbar-bar{width:44px;height:4px;background:#22262b;border-radius:2px;overflow:hidden}
.sysbar-fill{height:100%;transition:width .5s ease}
.sysbar-fill.ok{background:#3a7a35}
.sysbar-fill.hi{background:#8a6a20}
.sysbar-fill.crit{background:#8a3030}
#sysbar-sep{flex:1}
/* ── App body ── */
#app-body{display:flex;flex:1;overflow:hidden}
/* ── Sidebar ── */
#sidebar{width:220px;min-width:220px;background:#111217;border-right:1px solid #2c3035;display:flex;flex-direction:column;transition:width .18s,min-width .18s;overflow:hidden;flex-shrink:0}
#sidebar.collapsed{width:48px;min-width:48px}
.sidebar-section{padding:16px 14px 4px;color:#3d4552;font-size:10px;text-transform:uppercase;letter-spacing:0.7px;white-space:nowrap;overflow:hidden;opacity:1;transition:opacity .1s}
#sidebar.collapsed .sidebar-section{opacity:0;height:0;padding:0}
.nav-item{padding:9px 14px;cursor:pointer;display:flex;align-items:center;gap:11px;color:#6d7989;border-left:2px solid transparent;transition:color .1s,background .1s,border-color .1s;white-space:nowrap;user-select:none}
.nav-item:hover{background:#181b1f;color:#c4c9d4}
.nav-item.active{color:#5189c8;border-left-color:#4070a8;background:#181b1f}
.nav-icon{flex-shrink:0;width:16px;height:16px;display:flex;align-items:center;justify-content:center;opacity:0.7}
.nav-item.active .nav-icon{opacity:1}
.nav-label{font-size:13px;overflow:hidden;opacity:1;transition:opacity .15s}
#sidebar.collapsed .nav-label{opacity:0;width:0;overflow:hidden}
#sidebar-footer{margin-top:auto;padding:12px 14px;border-top:1px solid #22262b;font-size:11px;color:#3d4552;white-space:nowrap;overflow:hidden}
#sidebar.collapsed #sidebar-footer{display:none}
/* ── Main content ── */
#main-content{flex:1;overflow:hidden;display:flex;flex-direction:column}
.view{display:none;flex:1;overflow-y:auto;overflow-x:hidden;flex-direction:column}
.view.active{display:flex}
/* ── Full-page layout ── */
.page-wrap{padding:28px 36px;width:100%;max-width:1400px;margin:0 auto;display:flex;flex-direction:column;gap:20px;flex:1}
.page-header{display:flex;align-items:flex-start;justify-content:space-between}
.page-title{font-size:16px;font-weight:600;color:#c4c9d4}
.page-subtitle{color:#4a5566;font-size:12px;margin-top:4px}
.page-actions{display:flex;gap:8px;align-items:center;flex-shrink:0}
/* ── Panels (Grafana-style, no colored backgrounds) ── */
.card{background:#181b1f;border:1px solid #2c3035;border-radius:3px;overflow:hidden}
.card-header{padding:10px 16px;border-bottom:1px solid #22262b;display:flex;justify-content:space-between;align-items:center}
.card-title{font-size:11px;font-weight:600;color:#6d7989;text-transform:uppercase;letter-spacing:0.7px}
.card-body{padding:16px}
/* ── Topology panel ── */
.topo-card{background:#181b1f;border:1px solid #2c3035;border-radius:3px;overflow:hidden}
.topo-card-header{padding:10px 16px;border-bottom:1px solid #22262b;display:flex;justify-content:space-between;align-items:center}
.topo-svg-wrap{position:relative;height:400px}
svg.topo-svg{width:100%;height:100%}
.topo-status-bar{padding:7px 16px;border-top:1px solid #22262b;font-size:11px;color:#4a5566;background:#14171b;font-family:'Consolas','Courier New',monospace}
.topo-legend{position:absolute;bottom:8px;left:12px;font-size:10px;color:#4a5566;display:flex;gap:12px}
.legend-item{display:flex;align-items:center;gap:5px}
.legend-line{width:16px;height:2px;display:inline-block}
/* ── Alert rows (no tinted backgrounds, just a left border) ── */
.alert{border-left:3px solid;margin-bottom:1px;padding:12px 16px;cursor:pointer;transition:background .08s;background:#181b1f}
.alert:hover{background:#1e2226}
.alert-CRITICAL{border-left-color:#c0392b}
.alert-HIGH{border-left-color:#b8860b}
.alert-MEDIUM{border-left-color:#2e6da4}
.alert-LOW{border-left-color:#2d7a3a}
.alert-ttf{font-size:11px;color:#7a8494;margin-bottom:4px;font-family:'Consolas','Courier New',monospace}
.alert-ttf strong{color:#c4c9d4}
.alert-CRITICAL .alert-ttf strong{color:#e07070}
.alert-HIGH .alert-ttf strong{color:#c9963e}
.alert-title{color:#c4c9d4;font-weight:600;margin-bottom:4px;font-size:13px}
.alert-meta{color:#6d7989;font-size:11px;font-family:'Consolas','Courier New',monospace;margin-bottom:3px}
.alert-rationale{color:#4a5566;font-size:11px;line-height:1.55}
.severity-dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:5px;flex-shrink:0}
.dot-CRITICAL{background:#c0392b}
.dot-HIGH{background:#b8860b}
.dot-MEDIUM{background:#2e6da4}
.dot-LOW{background:#2d7a3a}
/* ── Stat rows (sidebar footer) ── */
.stat-row{display:flex;justify-content:space-between;margin-bottom:6px;padding:4px 0;border-bottom:1px solid #22262b}
.stat-label{color:#6d7989;font-size:11px}
.stat-val{color:#c4c9d4;font-weight:600;font-size:11px}
/* ── Pipeline explainer ── */
.pipe-step{border:1px solid #2c3035;border-radius:3px;padding:12px 14px;display:flex;flex-direction:column;gap:6px;background:#14171b}
.pipe-num{font-size:10px;font-weight:700;color:#4a5566;font-family:'Consolas','Courier New',monospace;margin-bottom:2px}
.pipe-title{font-size:12px;font-weight:600;color:#c4c9d4}
.pipe-body{font-size:11px;color:#6d7989;line-height:1.55}
.pipe-arrow{display:flex;align-items:center;justify-content:center;font-size:14px;color:#2c3035;padding:0;padding-top:30px}
details.card>summary{padding:10px 16px;cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px}
details.card>summary::-webkit-details-marker{display:none}
details.card[open]>summary{border-bottom:1px solid #22262b}
/* ── Action explainer ── */
.action-explain{border:1px solid #2c3035;border-radius:3px;padding:12px 14px;background:#14171b}
.action-explain.ae-locked{opacity:.6;border-style:dashed}
.ae-name{font-size:11px;font-weight:700;color:#5189c8;margin-bottom:6px;font-family:'Consolas','Courier New',monospace}
.ae-body{font-size:11px;color:#6d7989;line-height:1.6}
/* ── Topology SVG ── */
.topo-wrap{flex:1;min-height:0;position:relative;overflow:hidden}
svg.topo-svg{width:100%;height:100%}
.node{fill:#1e2226;stroke:#3d5a7a;stroke-width:1.5}
.node-pe{fill:#1c2430;stroke:#3d6090}
.node-p{fill:#221c30;stroke:#6050a0}
.node-ce{fill:#1c2a1c;stroke:#3a6a3a}
.link{stroke:#2c3035;stroke-width:1.5}
.link-degraded{stroke:#c0392b;stroke-width:2.5;stroke-dasharray:5,3;animation:dash 1.2s linear infinite}
@keyframes dash{to{stroke-dashoffset:-8}}
.node-label{fill:#8a9aaa;font-size:10px;font-family:monospace;pointer-events:none}
.legend-deg{background:#c0392b}
.legend-ok{background:#2c3035}
/* ── Time-travel ── */
#tt-slider{width:100%;accent-color:#4070a8;cursor:pointer}
#tt-live-btn{background:#1c2e1c;color:#57a84a;border:1px solid #2a442a;padding:4px 10px;border-radius:3px;cursor:pointer;font-family:inherit;font-size:11px}
#tt-live-btn:hover{background:#223822}
#tt-live-btn.dimmed{background:#1e2226;color:#4a5566;border-color:#2c3035}
#tt-event{font-size:12px;color:#c4c9d4}
#tt-event .ev-fault{font-size:13px;font-weight:600;color:#c4c9d4;margin-bottom:4px}
#tt-event .ev-meta{color:#6d7989;margin-bottom:4px;font-size:11px}
#tt-event .ev-rationale{color:#4a5566;font-size:11px}
/* ── Autonomy Matrix ── */
.matrix-table{width:100%;border-collapse:collapse;font-size:12px}
.matrix-table th{background:#14171b;color:#6d7989;padding:9px 14px;text-align:left;font-weight:600;text-transform:uppercase;font-size:10px;letter-spacing:0.7px;border-bottom:1px solid #2c3035}
.matrix-table td{padding:11px 14px;border-bottom:1px solid #22262b;vertical-align:middle}
.matrix-table tr:hover td{background:#1e2226}
.matrix-action{color:#c4c9d4;font-weight:600;font-size:13px;font-family:'Consolas','Courier New',monospace}
.matrix-desc{color:#4a5566;font-size:11px;margin-top:3px}
.matrix-conf input[type=number]{background:#111217;border:1px solid #2c3035;color:#c4c9d4;padding:4px 7px;border-radius:3px;width:68px;font-family:inherit;font-size:12px}
.toggle-wrap{display:flex;align-items:center;gap:8px}
.toggle{position:relative;width:38px;height:20px;cursor:pointer}
.toggle input{opacity:0;width:0;height:0}
.toggle-slider{position:absolute;top:0;left:0;right:0;bottom:0;background:#22262b;border-radius:10px;transition:.15s}
.toggle-slider:before{position:absolute;content:'';height:14px;width:14px;left:3px;bottom:3px;background:#6d7989;border-radius:50%;transition:.15s}
.toggle input:checked + .toggle-slider{background:#2a5a2a}
.toggle input:checked + .toggle-slider:before{transform:translateX(18px);background:#57a84a}
.toggle-locked{opacity:.35;cursor:not-allowed}
.matrix-save-btn{background:#1c2e1c;color:#57a84a;border:1px solid #2a442a;padding:5px 14px;border-radius:3px;cursor:pointer;font-family:inherit;font-size:12px}
.matrix-save-btn:hover{background:#223822}
.matrix-save-btn:disabled{background:#1e2226;color:#4a5566;border-color:#2c3035;cursor:not-allowed}
.matrix-save-status{font-size:11px;color:#57a84a;margin-left:8px}
.matrix-header-row{display:flex;align-items:center;justify-content:space-between;margin-bottom:14px}
.matrix-header-row h3{color:#c4c9d4;font-size:13px}
.safety-notice{border:1px solid #2c3035;border-left:2px solid #b8860b;padding:10px 14px;font-size:11px;color:#6d7989;margin-bottom:14px;background:#1a1a14}
/* ── NLQ / Ask ── */
#nlq-input{background:#111217;border:1px solid #2c3035;color:#c4c9d4;padding:7px 10px;border-radius:3px;font-family:'Consolas','Courier New',monospace;font-size:13px;flex:1}
#nlq-input:focus{outline:1px solid #4070a8}
#nlq-btn{background:#1c2e1c;color:#57a84a;border:1px solid #2a442a;padding:7px 16px;border-radius:3px;cursor:pointer;font-family:inherit;font-size:13px;font-weight:500;white-space:nowrap}
#nlq-btn:hover{background:#223822}
#nlq-output{padding:14px;background:#111217;border:1px solid #22262b;border-radius:3px;min-height:100px;white-space:pre-wrap;color:#c4c9d4;font-family:'Consolas','Courier New',monospace;font-size:12px;line-height:1.75}
.quick-btn{background:#1e2226;color:#5189c8;border:1px solid #2c3035;padding:4px 10px;border-radius:3px;cursor:pointer;font-size:11px;font-family:inherit}
.quick-btn:hover{background:#232830}
/* ── Incident Modal ── */
#incident-modal{display:none;position:fixed;right:0;top:46px;bottom:0;width:500px;background:#111217;border-left:1px solid #2c3035;z-index:60;flex-direction:column;overflow:hidden;box-shadow:-6px 0 24px rgba(0,0,0,.6)}
#incident-modal.open{display:flex}
#modal-header{padding:14px 18px;border-bottom:1px solid #2c3035;display:flex;justify-content:space-between;align-items:center;flex-shrink:0;background:#181b1f;gap:10px}
#modal-title{color:#c4c9d4;font-size:13px;font-weight:600;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.modal-close{background:none;border:none;color:#6d7989;font-size:18px;cursor:pointer;padding:2px 7px;border-radius:3px;line-height:1;flex-shrink:0}
.modal-close:hover{color:#c4c9d4;background:#22262b}
#modal-body{flex:1;overflow-y:auto;padding:18px}
#modal-footer{padding:12px 18px;border-top:1px solid #2c3035;display:flex;gap:8px;flex-shrink:0;background:#111217}
.q-section{margin-bottom:14px;padding:12px 14px;background:#181b1f;border-radius:3px;border-left:2px solid #2c3035}
.q-section .q-label{font-size:10px;font-weight:600;color:#4a5566;margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px}
.q-section .q-text{color:#c4c9d4;line-height:1.65;font-size:12px;white-space:pre-wrap}
.pending-action-box{border:1px solid #2c3035;border-left:2px solid #b8860b;padding:10px 12px;margin-bottom:12px;background:#1a1a14}
.approve-btn{flex:1;background:#1c2e1c;color:#57a84a;border:1px solid #2a442a;padding:8px;border-radius:3px;cursor:pointer;font-family:inherit;font-size:12px;font-weight:600}
.approve-btn:hover{background:#223822}
.approve-btn:disabled{background:#1e2226;color:#4a5566;border-color:#2c3035;cursor:not-allowed}
.reject-btn{background:#2e1c1c;color:#d05a52;border:1px solid #442a2a;padding:8px 16px;border-radius:3px;cursor:pointer;font-family:inherit;font-size:12px}
.reject-btn:hover{background:#381c1c}
/* ── Proactive action banner ── */
#action-notif{display:none;padding:8px 16px;background:#1a1a14;border-bottom:1px solid #b8860b;flex-shrink:0;align-items:center;gap:10px}
#action-notif.visible{display:flex}
.notif-label{color:#c9963e;font-size:11px;font-weight:600;white-space:nowrap}
.notif-text{color:#c4c9d4;font-size:11px;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.notif-btn{background:#1c2e1c;color:#57a84a;border:1px solid #2a442a;padding:4px 10px;border-radius:3px;cursor:pointer;font-family:inherit;font-size:11px;white-space:nowrap}
.notif-btn:hover{background:#223822}
.notif-dismiss{background:none;border:none;color:#4a5566;cursor:pointer;font-size:14px;padding:2px 5px;line-height:1}
.notif-dismiss:hover{color:#c4c9d4}
</style>
</head>
<body>

<!-- ── Header ─────────────────────────────────────────────────────────── -->
<div id="app-header">
  <button id="hamburger" onclick="toggleSidebar()" title="Toggle menu">&#9776;</button>
  <span id="app-title">Aether <span style="color:#3d4552;font-weight:400;font-size:11px">/ NOC Dashboard</span></span>
  <span class="badge badge-green" id="status-badge">Online</span>
  <span class="badge" id="compliance-badge">CHECKING&hellip;</span>
  <div id="header-right">
    <span class="badge badge-blue" id="alert-count-badge">0 alerts</span>
    <span id="clock"></span>
  </div>
</div>

<!-- ── System metrics bar ─────────────────────────────────────────── -->
<div id="sysbar">
  <div class="sysbar-item">
    <span class="sysbar-label">CPU</span>
    <div class="sysbar-bar"><div class="sysbar-fill ok" id="sb-cpu-bar" style="width:0%"></div></div>
    <span class="sysbar-val ok" id="sb-cpu-val">—</span>
  </div>
  <div class="sysbar-item">
    <span class="sysbar-label">RAM</span>
    <div class="sysbar-bar"><div class="sysbar-fill ok" id="sb-ram-bar" style="width:0%"></div></div>
    <span class="sysbar-val ok" id="sb-ram-val">—</span>
  </div>
  <div class="sysbar-item">
    <span class="sysbar-label">GPU</span>
    <div class="sysbar-bar"><div class="sysbar-fill ok" id="sb-gpu-bar" style="width:0%"></div></div>
    <span class="sysbar-val ok" id="sb-gpu-val">—</span>
  </div>
  <div class="sysbar-item">
    <span class="sysbar-label">VRAM</span>
    <div class="sysbar-bar"><div class="sysbar-fill ok" id="sb-vram-bar" style="width:0%"></div></div>
    <span class="sysbar-val ok" id="sb-vram-val">—</span>
  </div>
  <div class="sysbar-item">
    <span class="sysbar-label">GPU TEMP</span>
    <span class="sysbar-val ok" id="sb-gputemp-val">—</span>
  </div>
  <div id="sysbar-sep"></div>
  <div class="sysbar-item">
    <span class="sysbar-label">LLM</span>
    <span class="sysbar-val" id="sb-llm-inline">—</span>
  </div>
  <div class="sysbar-item">
    <span class="sysbar-label">ACPs</span>
    <span class="sysbar-val ok" id="sb-acps-val">—</span>
  </div>
</div>

<!-- ── App body ──────────────────────────────────────────────────────── -->
<div id="app-body">

  <!-- Sidebar -->
  <nav id="sidebar">
    <div class="sidebar-section">Views</div>

    <div class="nav-item active" data-nav="network" onclick="showPanel('network')">
      <span class="nav-icon"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="8" cy="4" r="1.8" fill="currentColor" stroke="none"/><circle cx="2.5" cy="13" r="1.8" fill="currentColor" stroke="none"/><circle cx="13.5" cy="13" r="1.8" fill="currentColor" stroke="none"/><line x1="8" y1="5.8" x2="3.5" y2="11.2"/><line x1="8" y1="5.8" x2="12.5" y2="11.2"/><line x1="4.3" y1="13" x2="11.7" y2="13"/></svg></span>
      <span class="nav-label">Network</span>
    </div>
    <div class="nav-item" data-nav="alerts" onclick="showPanel('alerts')">
      <span class="nav-icon"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><path d="M8 2a5 5 0 0 0-5 5v3.5L1.5 13h13L13 10.5V7a5 5 0 0 0-5-5z"/><path d="M6.5 13a1.5 1.5 0 0 0 3 0"/></svg></span>
      <span class="nav-label">Alerts</span>
    </div>
    <div class="nav-item" data-nav="copilot" onclick="showPanel('copilot')">
      <span class="nav-icon"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M2 3h12a1 1 0 0 1 1 1v7a1 1 0 0 1-1 1H5l-3 2.5V4a1 1 0 0 1 1-1z"/></svg></span>
      <span class="nav-label">Ask Aether</span>
    </div>

    <div class="sidebar-section">Tools</div>

    <div class="nav-item" data-nav="timetravel" onclick="showPanel('timetravel')">
      <span class="nav-icon"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><circle cx="9" cy="9" r="6"/><path d="M9 6v3l2 1.5"/><path d="M4 3l-2.5 2.5 2.5 2"/></svg></span>
      <span class="nav-label">History</span>
    </div>
    <div class="nav-item" data-nav="matrix" onclick="showPanel('matrix')">
      <span class="nav-icon"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><line x1="2" y1="6" x2="14" y2="6"/><line x1="2" y1="11" x2="14" y2="11"/><circle cx="6" cy="6" r="2" fill="currentColor" stroke="none"/><circle cx="11" cy="11" r="2" fill="currentColor" stroke="none"/></svg></span>
      <span class="nav-label">Policy Matrix</span>
    </div>
    <div class="nav-item" data-nav="logs" onclick="showPanel('logs')">
      <span class="nav-icon"><svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"><rect x="2" y="2" width="12" height="12" rx="1"/><line x1="5" y1="6" x2="11" y2="6"/><line x1="5" y1="9" x2="11" y2="9"/><line x1="5" y1="12" x2="8" y2="12"/></svg></span>
      <span class="nav-label">Remediation Log</span>
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
      <span class="notif-label">Action pending:</span>
      <span class="notif-text">
        <span id="notif-action-name" style="font-weight:bold"></span> &mdash;
        <span id="notif-fault"></span>
      </span>
      <button class="notif-btn" onclick="openNotifModal()">Review &amp; Approve</button>
      <button class="notif-dismiss" onclick="dismissNotif()" title="Dismiss">✕</button>
    </div>

    <!-- ── VIEW: Live Network ─────────────────────────────────────── -->
    <div class="view active" id="view-network">
      <div class="page-wrap">
        <div>
          <div class="page-header">
            <div>
              <div class="page-title">Live Network
                <span id="topo-live-dot" style="color:#3fb950;font-size:16px;margin-left:8px">&#9679;</span>
              </div>
              <div class="page-subtitle">7-node MPLS L3VPN topology — real-time fault monitoring</div>
            </div>
          </div>
        </div>

        <!-- Topology card -->
        <div class="topo-card">
          <div class="topo-card-header">
            <span class="card-title">Topology</span>
            <div style="display:flex;gap:10px;align-items:center">
              <span class="topo-legend" style="position:static;font-size:12px;color:#6b788e;display:flex;gap:16px">
                <span class="legend-item"><span class="legend-line legend-ok"></span> OK</span>
                <span class="legend-item"><span class="legend-line legend-deg" style="height:3px"></span> Degraded</span>
              </span>
            </div>
          </div>
          <div class="topo-svg-wrap">
            <svg id="topo-canvas" class="topo-svg" viewBox="0 0 560 340" preserveAspectRatio="xMidYMid meet"></svg>
          </div>
          <div class="topo-status-bar" id="topo-status-bar">Monitoring live — waiting for fault events…</div>
        </div>

        <!-- How Aether works pipeline -->
        <details class="card" style="overflow:visible">
          <summary>
            <span class="card-title" style="margin:0">Detection &amp; remediation pipeline</span>
            <span style="color:#3d4552;font-size:11px;margin-left:auto">expand</span>
          </summary>
          <div style="padding:16px 18px;display:grid;grid-template-columns:repeat(5,1fr);gap:0;align-items:start">
            <div class="pipe-step">
              <div class="pipe-num">01 / TELEMETRY</div>
              <div class="pipe-title">Scrape</div>
              <div class="pipe-body">FRR metrics every 30s — interface counters, BGP session state, packet loss, RTT jitter, OSPF LSAs, MPLS label bindings.</div>
            </div>
            <div class="pipe-arrow">→</div>
            <div class="pipe-step">
              <div class="pipe-num">02 / MODEL</div>
              <div class="pipe-title">BiLSTM + Attention</div>
              <div class="pipe-body">Last 20 scrapes fed as a sequence. Outputs: fault class, confidence score (0–100%), Time-To-Failure in seconds.</div>
            </div>
            <div class="pipe-arrow">→</div>
            <div class="pipe-step">
              <div class="pipe-num">03 / GRAPH</div>
              <div class="pipe-title">Clonal Search</div>
              <div class="pipe-body">NetworkX runs 3 routing permutations (baseline, rerouted, QoS-throttled), picks lowest SLA violation score as the recommended action.</div>
            </div>
            <div class="pipe-arrow">→</div>
            <div class="pipe-step">
              <div class="pipe-num">04 / TWIN</div>
              <div class="pipe-title">Digital Twin</div>
              <div class="pipe-body">Simulated replica runs in parallel. High divergence between twin and live state corroborates the anomaly.</div>
            </div>
            <div class="pipe-arrow">→</div>
            <div class="pipe-step">
              <div class="pipe-num">05 / POLICY</div>
              <div class="pipe-title">Edge Policy Engine</div>
              <div class="pipe-body">conf ≥ threshold + models agree → AUTO_EXECUTE (FRR commands issued). Otherwise → RECOMMEND_ONLY (Approve button shown).</div>
            </div>
          </div>
        </details>

        <!-- Recent alerts -->
        <div class="card">
          <div class="card-header">
            <span class="card-title">Recent Alerts</span>
            <span id="mini-feed-count" class="badge badge-yellow">0</span>
          </div>
          <div class="card-body" id="mini-feed" style="max-height:480px;overflow-y:auto"></div>
        </div>
      </div>
    </div>

    <!-- ── VIEW: Alert Feed ──────────────────────────────────────── -->
    <div class="view" id="view-alerts">
      <div class="page-wrap">
        <div>
          <div class="page-header">
            <div>
              <div class="page-title">Alert Feed</div>
              <div class="page-subtitle">All ACP events — click any alert to open incident report</div>
            </div>
            <div class="page-actions">
              <span id="full-feed-count" class="badge badge-yellow">0 events</span>
            </div>
          </div>
        </div>
        <div id="full-feed"></div>
      </div>
    </div>

    <!-- ── VIEW: NLQ Copilot ─────────────────────────────────────── -->
    <div class="view" id="view-copilot">
      <div class="page-wrap" style="max-width:900px">
        <div>
          <div class="page-title">Ask Aether</div>
          <div class="page-subtitle">Offline LLM copilot — Mistral 7B · ChromaDB RAG · zero cloud dependency</div>
        </div>

        <div class="card">
          <div class="card-body">
            <div style="display:flex;gap:10px;margin-bottom:14px">
              <input id="nlq-input" placeholder="e.g. What will fail next?  /  How do I fix BGP flap on pe1?" onkeydown="if(event.key==='Enter')nlqSend()" style="flex:1">
              <button id="nlq-btn" onclick="nlqSend()">Ask</button>
            </div>
            <div id="nlq-output" style="min-height:120px">Type a question above or use a quick query below.</div>
          </div>
        </div>

        <div class="card">
          <div class="card-header"><span class="card-title">Quick queries</span></div>
          <div class="card-body" style="display:flex;flex-wrap:wrap;gap:10px">
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

    <!-- ── VIEW: Time-Travel ─────────────────────────────────────── -->
    <div class="view" id="view-timetravel">
      <div class="page-wrap">
        <div>
          <div class="page-title">Topology History</div>
          <div class="page-subtitle">Scrub back through past fault states — each ACP is captured as a snapshot</div>
        </div>

        <div class="topo-card">
          <div class="topo-card-header">
            <span class="card-title">Playback</span>
            <span id="tt-snapshot-count" style="font-size:13px;color:#8b949e">0 snapshots</span>
          </div>
          <div class="topo-svg-wrap">
            <svg id="tt-canvas" class="topo-svg" viewBox="0 0 560 340" preserveAspectRatio="xMidYMid meet"></svg>
          </div>
          <div id="tt-controls" style="padding:14px 22px;border-top:1px solid #21262d;display:flex;align-items:center;gap:12px;background:#111620">
            <button id="tt-live-btn" class="dimmed" onclick="resumeLive()">&#9654; Live</button>
            <input type="range" id="tt-slider" min="0" max="0" value="0" style="flex:1;accent-color:#58a6ff" oninput="scrubHistory(+this.value)">
            <span id="tt-time-label" style="color:#8b949e;font-size:13px;min-width:160px;text-align:right">No snapshots yet</span>
          </div>
        </div>

        <div class="card">
          <div class="card-header"><span class="card-title">Event at selected time</span></div>
          <div class="card-body" id="tt-event" style="min-height:120px">
            <div style="color:#6b788e;font-size:14px;padding:20px 0;text-align:center">
              Drag the slider above to replay historical topology states.
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- ── VIEW: Autonomy Matrix ─────────────────────────────────── -->
    <div class="view" id="view-matrix">
      <div class="page-wrap" style="max-width:1100px">
        <div>
          <div class="page-header">
            <div>
              <div class="page-title">Autonomy Policy Matrix</div>
              <div class="page-subtitle">Control when Aether acts on its own vs. asks for your approval</div>
            </div>
            <div class="page-actions">
              <button class="matrix-save-btn" id="matrix-save-btn" onclick="saveMatrix()">Save Changes</button>
              <span class="matrix-save-status" id="matrix-save-status"></span>
            </div>
          </div>
        </div>

        <!-- Execution mode reference -->
        <div class="card">
          <div class="card-header"><span class="card-title">Execution modes</span></div>
          <div style="display:grid;grid-template-columns:1fr 1fr">
            <div style="padding:16px;border-right:1px solid #22262b">
              <div style="font-size:11px;font-weight:700;color:#57a84a;font-family:'Consolas','Courier New',monospace;margin-bottom:8px">AUTO_EXECUTE</div>
              <div style="font-size:11px;color:#6d7989;line-height:1.65;margin-bottom:10px">
                EPE issues FRR vtysh commands immediately — no operator click required.
                Every auto-action is written to the audit log with full ACP context and is reversible.
                Apply to routine, low-blast-radius actions where speed matters more than confirmation latency.
              </div>
              <div style="font-size:10px;color:#4a5566;border-top:1px solid #22262b;padding-top:8px;font-family:'Consolas','Courier New',monospace;line-height:1.7">
                e.g. conf=91% &gt;= threshold=75% → REROUTE_BRANCH executes &lt;2s<br>
                alert card shows: "Auto-fixed"
              </div>
            </div>
            <div style="padding:16px">
              <div style="font-size:11px;font-weight:700;color:#c9963e;font-family:'Consolas','Courier New',monospace;margin-bottom:8px">RECOMMEND_ONLY</div>
              <div style="font-size:11px;color:#6d7989;line-height:1.65;margin-bottom:10px">
                System surfaces a recommendation and waits. Approve/Reject buttons appear on the alert.
                Triggered when confidence is below threshold, the two ML models disagree,
                or the action class is locked (high blast radius).
              </div>
              <div style="font-size:10px;color:#4a5566;border-top:1px solid #22262b;padding-top:8px;font-family:'Consolas','Courier New',monospace;line-height:1.7">
                e.g. conf=62% &lt; threshold=75% → "Recommended: REROUTE_BRANCH"<br>
                operator clicks Approve to execute
              </div>
            </div>
          </div>
        </div>

        <!-- Confidence threshold + action glossary side by side -->
        <div style="display:grid;grid-template-columns:1fr 2fr;gap:16px;align-items:start">
          <div class="card">
            <div class="card-header"><span class="card-title">Confidence threshold</span></div>
            <div class="card-body" style="font-size:11px;color:#6d7989;line-height:1.65">
              The BiLSTM classifier outputs a probability score (0–100%) per prediction.
              The <strong style="color:#c4c9d4">min confidence</strong> per row is the floor below which
              that action class falls back to RECOMMEND_ONLY regardless of the toggle.<br><br>
              <span style="color:#4a5566">Higher = safer, more approvals.<br>Lower = more autonomous.</span><br><br>
              Typical values: 75% for reroutes, 90%+ for core or isolation actions.
            </div>
          </div>
          <div class="card">
            <div class="card-header"><span class="card-title">Action reference</span></div>
            <table style="width:100%;border-collapse:collapse;font-size:11px">
              <thead><tr>
                <th style="padding:8px 14px;text-align:left;color:#4a5566;border-bottom:1px solid #22262b;font-weight:600;background:#14171b">Action</th>
                <th style="padding:8px 14px;text-align:left;color:#4a5566;border-bottom:1px solid #22262b;font-weight:600;background:#14171b">What it does</th>
                <th style="padding:8px 14px;text-align:left;color:#4a5566;border-bottom:1px solid #22262b;font-weight:600;background:#14171b">Locked?</th>
              </tr></thead>
              <tbody>
                <tr><td style="padding:9px 14px;border-bottom:1px solid #22262b;color:#5189c8;font-family:monospace">REROUTE_BRANCH</td><td style="padding:9px 14px;border-bottom:1px solid #22262b;color:#6d7989">Shifts branch traffic to SD-WAN backup tunnel via OSPF cost manipulation. Preserves VoIP SLA.</td><td style="padding:9px 14px;border-bottom:1px solid #22262b;color:#4a5566">No</td></tr>
                <tr><td style="padding:9px 14px;border-bottom:1px solid #22262b;color:#5189c8;font-family:monospace">QOS_SHAPE_QUEUE</td><td style="padding:9px 14px;border-bottom:1px solid #22262b;color:#6d7989">Rate-limits bulk DB backup from 4M to 1.15 Mbps, freeing headroom for interactive traffic. Session-safe.</td><td style="padding:9px 14px;border-bottom:1px solid #22262b;color:#4a5566">No</td></tr>
                <tr><td style="padding:9px 14px;border-bottom:1px solid #22262b;color:#5189c8;font-family:monospace">CORE_PATH_FAILOVER</td><td style="padding:9px 14px;border-bottom:1px solid #22262b;color:#6d7989">Fails over P1 core path. Affects all customer VRFs simultaneously — high blast radius.</td><td style="padding:9px 14px;border-bottom:1px solid #22262b;color:#c9963e">Yes — human-only</td></tr>
                <tr><td style="padding:9px 14px;border-bottom:1px solid #22262b;color:#5189c8;font-family:monospace">NODE_ISOLATION</td><td style="padding:9px 14px;border-bottom:1px solid #22262b;color:#6d7989">Admin-shuts all interfaces on a node. Removes it from the MPLS fabric entirely.</td><td style="padding:9px 14px;border-bottom:1px solid #22262b;color:#c9963e">Yes — human-only</td></tr>
                <tr><td style="padding:9px 14px;color:#5189c8;font-family:monospace">NO_ACTION</td><td style="padding:9px 14px;color:#6d7989">Log and observe only. Used for LOW events, healthy confirmations, or no applicable remediation.</td><td style="padding:9px 14px;color:#4a5566">No</td></tr>
              </tbody>
            </table>
          </div>
        </div>

        <!-- Safety notice -->
        <div class="safety-notice">
          Safety floors enforced in code regardless of this table:
          model disagreement always downgrades to <strong style="color:#c9963e">RECOMMEND_ONLY</strong> &bull;
          all auto-executed actions are logged and reversible &bull;
          locked rows cannot be auto-executed regardless of settings.
        </div>

        <!-- The actual policy table -->
        <div class="card" id="matrix-panel">
          <div class="card-header">
            <span class="card-title">Policy Configuration</span>
            <span style="color:#6b788e;font-size:12px">Changes take effect immediately — no restart needed</span>
          </div>
          <div id="matrix-table-wrap" class="card-body">Loading policy&hellip;</div>
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

    <!-- ── VIEW: Remediation Log ─────────────────────────────────── -->
    <div class="view" id="view-logs">
      <div class="page-wrap">
        <div>
          <div class="page-header">
            <div>
              <div class="page-title">Remediation Log</div>
              <div class="page-subtitle">Every action Aether took — auto-executed and operator-approved — with real command output</div>
            </div>
            <div class="page-actions">
              <button onclick="loadActionLog()" class="matrix-save-btn">Refresh</button>
            </div>
          </div>
        </div>

        <div class="card" style="border-left:2px solid #4a5566">
          <div class="card-body" style="font-size:11px;color:#4a5566;line-height:1.65;padding:12px 16px">
            Commands are run with <code style="color:#5189c8">docker exec clab-aether-*</code>.
            If Containerlab is not running, commands will fail with "No such container" — that failure is the real outcome and is logged here.
            REJECTED entries mean the operator clicked Reject — no commands were attempted.
          </div>
        </div>

        <div id="action-log-table">
          <div style="color:#4a5566;font-size:12px;padding:20px 0">Loading action log&hellip;</div>
        </div>
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
  if (name === 'logs') loadActionLog();
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
  'pe1':       {x:190, y:155, cls:'node-pe', label:'PE1'},
  'p1':        {x:280, y:90,  cls:'node-p',  label:'P1'},
  'pe2':       {x:370, y:155, cls:'node-pe', label:'PE2'},
  'ce-branch1':{x:95,  y:255, cls:'node-ce', label:'Branch1'},
  'ce-hub':    {x:190, y:270, cls:'node-ce', label:'Hub'},
  'ce-branch2':{x:370, y:255, cls:'node-ce', label:'Branch2'},
  'ce-dc':     {x:465, y:270, cls:'node-ce', label:'DC'},
};
const LINKS = [
  ['pe1','p1'], ['p1','pe2'],
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
let _liveMetrics = {};

function drawTopo() {
  drawTopoOnSvg('topo-canvas', currentDegradedLinks);
  _overlayMetrics('topo-canvas', _liveMetrics);
}
drawTopo();

// ── Live traffic metrics overlay ─────────────────────────────────────────────
function _overlayMetrics(svgId, links) {
  const svg = document.getElementById(svgId);
  if (!svg || !links || !Object.keys(links).length) return;
  svg.querySelectorAll('.metric-label').forEach(el => el.remove());
  const defs = {
    'pe1→p1':          ['pe1','p1'],
    'p1→pe2':          ['p1','pe2'],
    'pe1→ce-hub':      ['pe1','ce-hub'],
    'pe2→ce-branch1':  ['pe2','ce-branch1'],
    'pe2→ce-branch2':  ['pe2','ce-branch2'],
    'pe1→ce-dc':       ['pe1','ce-dc'],
  };
  for (const [key, [a, b]] of Object.entries(defs)) {
    const m = links[key];
    if (!m || !NODES[a] || !NODES[b]) continue;
    const mx = (NODES[a].x + NODES[b].x) / 2;
    const my = (NODES[a].y + NODES[b].y) / 2;
    const t = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    t.setAttribute('x', mx); t.setAttribute('y', my - 5);
    t.setAttribute('text-anchor', 'middle');
    t.setAttribute('class', 'metric-label');
    t.setAttribute('fill', m.util_pct > 70 ? '#f85149' : m.util_pct > 40 ? '#e3b341' : '#3fb950');
    t.setAttribute('font-size', '9');
    t.setAttribute('font-family', "'Consolas','Courier New',monospace");
    t.textContent = m.util_pct + '% · ' + m.mbps + 'M';
    svg.appendChild(t);
  }
}

async function pollLiveMetrics() {
  try {
    const r = await fetch('/api/metrics/live');
    const d = await r.json();
    _liveMetrics = d.links || {};
    _overlayMetrics('topo-canvas', _liveMetrics);
    // Also update time-travel canvas if it is the current topology snapshot
    _overlayMetrics('tt-canvas', _liveMetrics);
  } catch(e) {}
}
pollLiveMetrics();
setInterval(pollLiveMetrics, 15000);

// ── System metrics bar ─────────────────────────────────────────────────────
function _sysbarClass(pct) {
  if (pct === null || pct === undefined) return 'ok';
  if (pct >= 85) return 'crit';
  if (pct >= 60) return 'hi';
  return 'ok';
}
function _sysbarUpdate(barId, valId, pct, label) {
  const bar = document.getElementById(barId);
  const val = document.getElementById(valId);
  if (!bar || !val) return;
  const cls = _sysbarClass(pct);
  bar.style.width = (pct ?? 0) + '%';
  bar.className = 'sysbar-fill ' + cls;
  val.textContent = label;
  val.className = 'sysbar-val ' + cls;
}
async function pollSysMetrics() {
  try {
    const r = await fetch('/api/system-metrics');
    const d = await r.json();
    _sysbarUpdate('sb-cpu-bar','sb-cpu-val', d.cpu_pct,
      d.cpu_pct != null ? d.cpu_pct.toFixed(1)+'%' : '—');
    _sysbarUpdate('sb-ram-bar','sb-ram-val', d.ram_pct,
      d.ram_used_gb != null ? d.ram_used_gb.toFixed(1)+'/'+ d.ram_total_gb.toFixed(0)+'GB' : '—');
    // Show current GPU util + 60s peak (GPU idles at 0% between ~50ms inference bursts)
    const gpuLabel = d.gpu_pct != null
      ? d.gpu_pct.toFixed(0)+'% (peak '+( d.gpu_peak_pct||0).toFixed(0)+'%)'
      : 'N/A';
    _sysbarUpdate('sb-gpu-bar','sb-gpu-val', d.gpu_peak_pct ?? d.gpu_pct, gpuLabel);
    const vramPct = d.vram_total_mb ? (d.vram_used_mb / d.vram_total_mb * 100) : null;
    _sysbarUpdate('sb-vram-bar','sb-vram-val', vramPct,
      d.vram_used_mb != null
        ? (d.vram_used_mb/1024).toFixed(1)+'/'+(d.vram_total_mb/1024).toFixed(0)+'GB'
        : 'N/A');
    const tempEl = document.getElementById('sb-gputemp-val');
    if (tempEl) {
      tempEl.textContent = d.gpu_temp_c != null ? d.gpu_temp_c.toFixed(0)+'°C' : 'N/A';
      tempEl.className = 'sysbar-val ' + (d.gpu_temp_c > 80 ? 'crit' : d.gpu_temp_c > 65 ? 'hi' : 'ok');
    }
  } catch(e) {}
}
pollSysMetrics();
setInterval(pollSysMetrics, 4000);

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
  const ttfSec = acp.ttf != null && acp.ttf >= 0 ? acp.ttf : null;
  const ttfLabel = ttfSec != null
    ? (ttfSec < 60 ? ttfSec.toFixed(0) + 's' : (ttfSec / 60).toFixed(1) + 'min')
    : null;
  const ts   = (acp.timestamp || '').slice(11, 19);
  const modeLabel = acp.execution_mode === 'AUTO_EXECUTE'
    ? 'AUTO_EXECUTE' : acp.execution_mode === 'RECOMMEND_ONLY'
    ? 'RECOMMEND_ONLY' : (acp.execution_mode || '?');
  const modeColor = acp.execution_mode === 'AUTO_EXECUTE' ? '#3fb950'
    : acp.execution_mode === 'RECOMMEND_ONLY' ? '#e3b341' : '#8b949e';

  function makeDiv() {
    const div = document.createElement('div');
    div.className = 'alert alert-' + (acp.severity || 'MEDIUM');
    div.innerHTML = `
      ${ttfLabel ? `<div class="alert-ttf">TTF <strong>${ttfLabel}</strong> before SLA breach</div>` : ''}
      <div class="alert-title">
        <span class="severity-dot dot-${acp.severity||'MEDIUM'}"></span>
        ${acp.fault_class || 'Unknown'} &mdash; ${acp.severity || '?'}
      </div>
      <div class="alert-meta">
        <span style="color:#8b949e">conf</span> <strong style="color:#e6edf3">${conf}</strong>
        &nbsp;·&nbsp;
        <span style="color:${modeColor}">${modeLabel}</span>
        &nbsp;·&nbsp; ${ts} UTC
      </div>
      <div class="alert-rationale">${(acp.rationale || '').slice(0, 140)}</div>`;
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
        // Fire real commands for AUTO_EXECUTE mode — no human click needed
        if (acp.execution_mode === 'AUTO_EXECUTE' && acp.action && acp.action !== 'NO_ACTION') {
          autoExecuteAcp(acp);
        }
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
      <div style="font-size:11px;font-weight:600;color:#e3b341;margin-bottom:6px">Awaiting approval</div>
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
    const srcTag = d.source === 'ollama' ? 'Mistral 7B' : 'Structured fallback';

    // Build remediation commands section
    let remHtml = '';
    if (d.remediation && d.remediation.steps && d.remediation.steps.length) {
      const rem = d.remediation;
      const stepsHtml = rem.steps.map((s, i) => `
        <div style="margin-bottom:10px">
          <div style="font-size:11px;color:#6b788e;margin-bottom:4px">${i+1}. ${s.description}</div>
          <code style="display:block;font-family:'Consolas','Courier New',monospace;font-size:11px;
                       color:#a5d6ff;background:#0d1117;border:1px solid #2d3040;border-radius:3px;
                       padding:8px 10px;cursor:pointer;word-break:break-all;white-space:pre-wrap"
                title="Click to copy"
                onclick="navigator.clipboard.writeText(this.textContent).then(()=>{this.style.borderColor='#2ea043';setTimeout(()=>this.style.borderColor='#2d3040',900)})">${s.command}</code>
        </div>`).join('');
      remHtml = `
        <div id="remediation-section" style="margin-top:16px;border-top:1px solid #2d3040;padding-top:14px">
          <div style="font-size:11px;font-weight:600;color:#6b788e;margin-bottom:2px">Remediation commands</div>
          <div style="font-size:11px;color:#545f75;margin-bottom:10px">${rem.title}
            <span style="float:right">target: <code style="color:#8b949e;font-size:11px">${rem.affected_node}</code> (${rem.node_ip})</span>
          </div>
          <div style="background:#0e1117;border-radius:4px;padding:12px">
            ${stepsHtml}
            <div style="font-size:10px;color:#484f58;margin-top:4px">Click any command to copy to clipboard</div>
          </div>
        </div>`;
    }

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
      ${remHtml}
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
  btn.disabled = true;
  btn.textContent = 'Executing…';

  // Fetch the remediation steps first so we can send real commands
  let steps = [];
  try {
    const r = await fetch('/api/explain/' + encodeURIComponent(currentModalAcp.acp_id));
    const d = await r.json();
    if (d.remediation && d.remediation.steps) steps = d.remediation.steps;
  } catch(e) {}

  try {
    const r = await fetch('/api/execute-action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        acp_id:      currentModalAcp.acp_id,
        action:      currentModalAcp.action || 'NO_ACTION',
        fault_class: currentModalAcp.fault_class || 'Unknown',
        severity:    currentModalAcp.severity || 'MEDIUM',
        steps:       steps,
        executed_by: 'OPERATOR',
      }),
    });
    const result = await r.json();
    const ok = result.overall === 'SUCCESS';
    const partial = result.overall === 'PARTIAL';
    btn.textContent = ok ? '✓ Executed' : partial ? '⚠ Partial' : '✗ Commands failed (logged)';
    btn.style.background = ok ? '#1c2e1c' : '#2e2218';
    btn.style.color = ok ? '#57a84a' : '#c9963e';

    // Show real output in the modal
    const el = document.getElementById('modal-explain-body');
    if (el && result.steps) {
      const rows = result.steps.map(s => {
        const icon = s.skipped ? '—' : s.success ? '✓' : '✗';
        const color = s.skipped ? '#4a5566' : s.success ? '#57a84a' : '#d05a52';
        const out = (s.stdout || '') + (s.stderr ? '\n' + s.stderr : '');
        return `<div style="margin-bottom:12px;border-left:2px solid ${color};padding-left:10px">
          <div style="font-size:10px;color:${color};margin-bottom:4px">${icon} ${s.description || s.command}</div>
          <code style="display:block;font-size:10px;color:#6d7989;background:#111217;padding:6px 8px;border-radius:2px;white-space:pre-wrap;word-break:break-all">${s.command}</code>
          ${out ? `<code style="display:block;font-size:10px;color:#c4c9d4;background:#14171b;padding:6px 8px;margin-top:4px;border-radius:2px;white-space:pre-wrap">${out}</code>` : ''}
        </div>`;
      }).join('');
      el.innerHTML = `<div style="font-size:11px;font-weight:600;color:#4a5566;margin-bottom:10px;text-transform:uppercase;letter-spacing:0.5px">Execution result — ${result.overall}</div>` + rows;
    }
  } catch(e) {
    btn.textContent = 'Error'; btn.disabled = false;
  }
}

async function rejectCurrentAcp() {
  if (!currentModalAcp) return;
  const btn = document.getElementById('modal-reject-btn');
  btn.disabled = true; btn.textContent = 'Logging rejection…';
  try {
    await fetch('/api/reject-action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        acp_id:   currentModalAcp.acp_id,
        feedback: currentModalAcp.action || 'NO_ACTION',
      }),
    });
    closeModal();
  } catch(e) {
    btn.disabled = false; btn.textContent = '✗ Reject';
  }
}

// Auto-execute when the system delivers an AUTO_EXECUTE ACP
async function autoExecuteAcp(acp) {
  let steps = [];
  try {
    const r = await fetch('/api/explain/' + encodeURIComponent(acp.acp_id));
    const d = await r.json();
    if (d.remediation && d.remediation.steps) steps = d.remediation.steps;
  } catch(e) {}

  try {
    await fetch('/api/execute-action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        acp_id:      acp.acp_id,
        action:      acp.action || 'NO_ACTION',
        fault_class: acp.fault_class || 'Unknown',
        severity:    acp.severity || 'MEDIUM',
        steps:       steps,
        executed_by: 'AUTO',
      }),
    });
  } catch(e) { console.error('[Aether] autoExecute failed:', e); }
}

// ─────────────────────────────────────────────────────────────────────────────
// Remediation Log view
// ─────────────────────────────────────────────────────────────────────────────
async function loadActionLog() {
  const el = document.getElementById('action-log-table');
  if (!el) return;
  try {
    const r = await fetch('/api/action-log?limit=100');
    const d = await r.json();
    if (!d.entries || d.entries.length === 0) {
      el.innerHTML = `<div style="color:#4a5566;font-size:12px;padding:24px 0;text-align:center">
        No actions logged yet. Actions appear here when AUTO_EXECUTE fires or you click Approve on an alert.
      </div>`;
      return;
    }
    el.innerHTML = d.entries.map(e => {
      const ts = (e.timestamp||'').replace('T',' ').slice(0,19);
      const overallColor = {SUCCESS:'#57a84a', PARTIAL:'#c9963e', FAILED:'#d05a52', REJECTED:'#4a5566'}[e.overall] || '#4a5566';
      const modeColor = e.executed_by === 'AUTO' ? '#5189c8' : '#c9963e';
      const stepRows = (e.steps||[]).map(s => {
        if (s.skipped) return '';
        const icon = s.success ? '✓' : '✗';
        const c = s.success ? '#57a84a' : '#d05a52';
        const out = ((s.stdout||'') + (s.stderr ? '\n' + s.stderr : '')).trim().slice(0,500);
        return `<tr>
          <td style="padding:6px 10px;border-bottom:1px solid #22262b;color:${c};font-family:monospace;font-size:10px;width:14px">${icon}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #22262b;font-size:10px;color:#6d7989">${s.description||''}</td>
          <td style="padding:6px 10px;border-bottom:1px solid #22262b">
            <code style="font-size:10px;color:#5189c8;font-family:monospace">${s.command||''}</code>
            ${out ? `<pre style="font-size:9px;color:#4a5566;margin-top:4px;white-space:pre-wrap;word-break:break-all;max-height:80px;overflow:hidden">${out}</pre>` : ''}
          </td>
        </tr>`;
      }).join('');

      return `<div class="card" style="margin-bottom:10px">
        <div class="card-header" style="padding:10px 14px">
          <div style="display:flex;align-items:center;gap:12px;flex:1;min-width:0">
            <span style="font-size:10px;font-weight:700;color:${overallColor};font-family:monospace;flex-shrink:0">${e.overall}</span>
            <span style="font-size:11px;font-weight:600;color:#c4c9d4;font-family:monospace">${e.action||'?'}</span>
            <span style="font-size:10px;color:#4a5566;font-family:monospace">${e.fault_class||''}</span>
          </div>
          <div style="display:flex;gap:10px;align-items:center;flex-shrink:0;font-size:10px">
            <span style="color:${modeColor};font-family:monospace">${e.executed_by}</span>
            <span style="color:#3d4552;font-family:monospace">${ts}</span>
            <span style="color:#3d4552;font-family:monospace">${(e.acp_id||'').slice(0,8)}</span>
          </div>
        </div>
        ${stepRows ? `<table style="width:100%;border-collapse:collapse">${stepRows}</table>` : ''}
      </div>`;
    }).join('');
  } catch(err) {
    el.innerHTML = `<div style="color:#d05a52;font-size:12px;padding:20px 0">Failed to load log: ${err}</div>`;
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
    // Sysbar inline LLM + ACP count
    const llmEl = document.getElementById('sb-llm-inline');
    if (llmEl) {
      llmEl.textContent = s.llm_online ? '● Online' : '○ Offline';
      llmEl.className = 'sysbar-val ' + (s.llm_online ? 'ok' : 'hi');
    }
    const acpEl = document.getElementById('sb-acps-val');
    if (acpEl) acpEl.textContent = (s.acp_count ?? '—').toLocaleString();
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

    # Attach node-specific remediation commands
    action       = acp_entry.get("corroboration", {}).get("recommended_action", "NO_ACTION")
    top_features = acp_entry.get("top_features", [])
    result["remediation"] = _build_remediation(action, top_features)

    return result


_gpu_peak_window: list[float] = []  # rolling 60-second peak tracker (15 samples × 4s)
_GPU_PEAK_MAX = 15

@app.get("/api/system-metrics")
async def system_metrics():
    """Returns CPU, RAM, GPU, and VRAM usage for the system metrics bar."""
    import subprocess as _sp
    result: dict = {}
    try:
        import psutil
        result["cpu_pct"]      = round(psutil.cpu_percent(interval=0.3), 1)
        vm = psutil.virtual_memory()
        result["ram_pct"]      = round(vm.percent, 1)
        result["ram_used_gb"]  = round(vm.used / 1e9, 1)
        result["ram_total_gb"] = round(vm.total / 1e9, 1)
    except ImportError:
        result["cpu_pct"] = result["ram_pct"] = result["ram_used_gb"] = result["ram_total_gb"] = None

    # GPU via nvidia-smi (RTX 4060)
    try:
        out = _sp.check_output(
            ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu",
             "--format=csv,noheader,nounits"],
            timeout=3, text=True
        ).strip().split(",")
        gpu_now = float(out[0].strip())
        result["gpu_pct"]       = gpu_now
        result["vram_used_mb"]  = float(out[1].strip())
        result["vram_total_mb"] = float(out[2].strip())
        result["gpu_temp_c"]    = float(out[3].strip())
        # Rolling 60-second peak (inference bursts are ~50ms so instant reads show 0%)
        _gpu_peak_window.append(gpu_now)
        if len(_gpu_peak_window) > _GPU_PEAK_MAX:
            _gpu_peak_window.pop(0)
        result["gpu_peak_pct"] = max(_gpu_peak_window)
    except Exception:
        result["gpu_pct"] = result["vram_used_mb"] = result["vram_total_mb"] = result["gpu_temp_c"] = None
        result["gpu_peak_pct"] = None

    return result


@app.get("/api/metrics/live")
async def live_metrics():
    """Simulated real-time link utilization for topology overlays. Updates via random walk."""
    result = {}
    for link in list(_link_util):
        _link_util[link] = max(1.0, min(92.0, _link_util[link] + _random.gauss(0, 1.8)))
        result[link] = {
            "util_pct": round(_link_util[link], 1),
            "mbps":     round(_link_util[link] * 10, 1),
        }
    return {"links": result, "ts": datetime.utcnow().isoformat()}


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


class ExecuteActionRequest(BaseModel):
    acp_id: str
    action: str
    fault_class: str = "Unknown"
    severity: str = "MEDIUM"
    steps: list[dict] = []
    executed_by: str = "OPERATOR"  # "AUTO" or "OPERATOR"


@app.post("/api/execute-action")
async def execute_action(req: ExecuteActionRequest):
    """
    Actually runs the remediation commands and writes the result to action_log.jsonl.
    Returns the full execution record including real stdout/stderr from each command.
    """
    sys.path.insert(0, os.path.join(REPO_ROOT, "..", "phase3-models"))
    from action_log import execute_and_log
    from feedback_cli import _apply_feedback

    # Mark as accepted in IKB
    _apply_feedback(req.acp_id, "accepted")

    # Execute for real and log
    entry = execute_and_log(
        acp_id=req.acp_id,
        action=req.action,
        steps=req.steps,
        executed_by=req.executed_by,
        fault_class=req.fault_class,
        severity=req.severity,
    )
    return entry


@app.post("/api/reject-action")
async def reject_action(req: FeedbackRequest):
    """Logs a rejection without running any commands."""
    sys.path.insert(0, os.path.join(REPO_ROOT, "..", "phase3-models"))
    from action_log import log_rejected
    from feedback_cli import _apply_feedback

    _apply_feedback(req.acp_id, "rejected")
    entry = log_rejected(
        acp_id=req.acp_id,
        action=req.feedback,  # reuse field for action name
        fault_class="",
        severity="",
    )
    return {"status": "rejected", "logged": True}


@app.get("/api/action-log")
async def action_log_endpoint(limit: int = 100):
    """Returns the real remediation execution log — every AUTO and operator-approved action."""
    sys.path.insert(0, os.path.join(REPO_ROOT, "..", "phase3-models"))
    from action_log import read_log
    return {"entries": read_log(limit=limit)}


@app.get("/api/compliance")
async def compliance():
    from airgap_compliance import run_compliance_check, _sign_report
    report = run_compliance_check()
    report = _sign_report(report)
    return report


@app.get("/api/tunnel-health")
async def tunnel_health():
    """Returns per-LSP MPLS tunnel health derived from the graph model state."""
    try:
        sys.path.insert(0, os.path.join(REPO_ROOT, "..", "phase3-models"))
        from graph_model import ClonalGraphEngine
        engine = ClonalGraphEngine()
        # Apply live link utilization as synthetic delay proxy
        for link_key, util in _link_util.items():
            parts = link_key.split("→")
            if len(parts) == 2:
                u, v = parts[0].strip(), parts[1].strip()
                synth_delay = max(1, int(util / 10))  # util% → roughly delay ms
                if engine.base_graph.has_edge(u, v):
                    engine.base_graph[u][v]["delay"] = synth_delay
        return {"tunnels": engine.get_tunnel_health(), "source": "graph_model"}
    except Exception as e:
        return {"tunnels": [], "error": str(e)}


@app.get("/api/netflow")
async def netflow_summary():
    """Returns a summary of NetFlow/IPFIX records from the flow simulator."""
    try:
        import urllib.request
        with urllib.request.urlopen("http://localhost:9995/summary", timeout=2) as resp:
            import json as _json
            data = _json.loads(resp.read())
        return data
    except Exception:
        return {
            "total_flows": 11,
            "total_bytes": 0,
            "fault_flows": 0,
            "source": "unavailable — start netflow_simulator.py",
        }


@app.get("/api/benchmark")
async def benchmark():
    from benchmark_harness import run_benchmark
    data_path = os.path.join(REPO_ROOT, "..", "phase3-models", "dataset_large.csv")
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
