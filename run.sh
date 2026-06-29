#!/usr/bin/env bash
# run.sh — Start the full Project Aether stack (PS-13)
#
# Layers (in order):
#   1. Containerlab  — deploy 7-node FRR topology (needs sudo + Docker)
#   2. MPLS/VRF/BGP  — post-deploy live config via chunk3-setup.sh
#   3. Telemetry     — Prometheus + Grafana (docker compose) + exporter.py
#   4. NetFlow sim   — synthetic IPFIX flow records
#   5. Traffic gen   — iperf3 application traffic
#   6. Fault stream  — inference engine + ACP generator
#   7. Ollama        — local LLM (Mistral 7B)
#   8. NOC Dashboard — FastAPI SPA on :8080
#
# Usage:
#   ./run.sh            — full stack (Containerlab + everything)
#   ./run.sh --no-clab  — skip Containerlab (AI/dashboard only, synthetic data)

set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
LOGS="$REPO/.logs"
TOPOLOGY="$REPO/phase1-simulation/topology/aether-lab.clab.yml"
SETUP_SH="$REPO/phase1-simulation/topology/chunk3-setup.sh"
TELEMETRY="$REPO/phase2-telemetry"

mkdir -p "$LOGS"

NO_CLAB=0
for arg in "$@"; do
  [[ "$arg" == "--no-clab" ]] && NO_CLAB=1
done

# ── helpers ───────────────────────────────────────────────────────────────────

log()  { echo ""; echo "==> $*"; }
info() { echo "    $*"; }

kill_proc() {
  local pattern="$1"
  local pids
  pids=$(pgrep -f "$pattern" 2>/dev/null || true)
  if [ -n "$pids" ]; then
    info "stopping: $pattern (PIDs $pids)"
    kill $pids 2>/dev/null || true
    sleep 1
  fi
}

start_bg() {
  local name="$1"; shift
  local logfile="$LOGS/${name}.log"
  nohup "$@" > "$logfile" 2>&1 &
  info "[$!] $name  →  $logfile"
}

wait_http() {
  local url="$1" label="$2" tries="${3:-20}"
  for i in $(seq 1 "$tries"); do
    if curl -s "$url" > /dev/null 2>&1; then
      info "$label ready"
      return 0
    fi
    sleep 1
  done
  echo "  !! $label did not respond at $url after ${tries}s"
  return 1
}

# ── stop anything from a previous run ────────────────────────────────────────

log "Stopping previous Aether processes…"
kill_proc "phase5-dashboard/app.py"
kill_proc "phase3-models/fault_streamer.py"
kill_proc "phase2-telemetry/netflow_simulator.py"
kill_proc "phase2-telemetry/traffic_generator.py"
kill_proc "phase2-telemetry/exporter.py"

# ── [1] Containerlab ──────────────────────────────────────────────────────────

if [ "$NO_CLAB" -eq 0 ]; then
  log "[1/8] Deploying Containerlab topology (needs sudo + Docker)…"
  if ! command -v containerlab &>/dev/null; then
    echo "  !! containerlab not found. Install: https://containerlab.dev/install/"
    echo "     Re-run with --no-clab to skip (AI runs on synthetic data)."
    exit 1
  fi
  if ! sudo docker info &>/dev/null; then
    echo "  !! Docker is not running or you lack sudo. Start Docker first."
    exit 1
  fi
  # Tear down any existing deployment of the same lab
  sudo containerlab destroy -t "$TOPOLOGY" --cleanup 2>/dev/null || true
  sudo containerlab deploy  -t "$TOPOLOGY"
  info "Containerlab deployed"

  # ── [2] Post-deploy MPLS/VRF/BGP + SD-WAN overlay + baseline QoS ─────────────
  log "[2/8] Applying MPLS / VRF / L3VPN config + SD-WAN overlay + QoS…"
  pushd "$REPO/phase1-simulation/topology" > /dev/null
    LAB=aether bash chunk3-setup.sh
    LAB=aether bash overlay-setup.sh
    LAB=aether bash qos-setup.sh
  popd > /dev/null

else
  log "[1/8] Containerlab — SKIPPED (--no-clab flag set, using synthetic data)"
  log "[2/8] MPLS/VRF config — SKIPPED"
fi

# ── [3] Telemetry: Prometheus + Grafana + exporter ───────────────────────────

log "[3/8] Starting Prometheus + Grafana (docker compose)…"
if ! command -v docker &>/dev/null; then
  info "Docker not found — skipping Prometheus/Grafana"
else
  pushd "$TELEMETRY" > /dev/null
    docker compose up -d
  popd > /dev/null
  info "Prometheus  →  http://localhost:9090"
  info "Grafana     →  http://localhost:3000  (admin / admin)"

  log "    Starting Prometheus exporter (FRR metrics on :8000)…"
  start_bg "exporter" python3 "$TELEMETRY/exporter.py"
  wait_http "http://localhost:8000/metrics" "Prometheus exporter" 15 || true
fi

# ── [4] NetFlow simulator ─────────────────────────────────────────────────────

log "[4/8] Starting NetFlow simulator (UDP :9995)…"
start_bg "netflow_simulator" python3 "$REPO/phase2-telemetry/netflow_simulator.py"

# ── [5] Traffic generator ─────────────────────────────────────────────────────

log "[5/8] Starting traffic generator (iperf3 application flows)…"
start_bg "traffic_generator" python3 "$REPO/phase2-telemetry/traffic_generator.py"

# ── [6] Fault streamer / inference engine ────────────────────────────────────

log "[6/8] Starting fault streamer + inference engine…"
start_bg "fault_streamer" python3 "$REPO/phase3-models/fault_streamer.py"

# ── [7] Ollama LLM ───────────────────────────────────────────────────────────

log "[7/8] Checking Ollama (Mistral 7B)…"
if pgrep -f "ollama serve" > /dev/null 2>&1; then
  info "Ollama already running"
else
  start_bg "ollama" ollama serve
  info "waiting for Ollama…"
  wait_http "http://localhost:11434/api/tags" "Ollama" 20 || true
fi

# ── [8] NOC Dashboard ────────────────────────────────────────────────────────

log "[8/8] Starting NOC Dashboard…"
sleep 2
start_bg "dashboard" python3 "$REPO/phase5-dashboard/app.py"

echo ""
info "waiting for dashboard on :8080…"
if wait_http "http://localhost:8080/api/status" "Dashboard" 20; then
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  Project Aether — all systems up"
  echo ""
  echo "  NOC Dashboard   →  http://localhost:8080"
  echo "  Prometheus      →  http://localhost:9090"
  echo "  Grafana         →  http://localhost:3000  (admin/admin)"
  echo "  LLM (Ollama)    →  http://localhost:11434"
  echo "  Exporter        →  http://localhost:8000/metrics"
  echo ""
  echo "  Logs            →  $LOGS/"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  curl -s http://localhost:8080/api/status | python3 -m json.tool 2>/dev/null || true
else
  echo "!! Dashboard did not come up. Check $LOGS/dashboard.log"
  exit 1
fi
