#!/usr/bin/env bash
# =============================================================================
# run.sh — Start (or stop) the Project Aether stack (PS-13)
#
# Usage:
#   ./run.sh            Start everything in SYNTHETIC mode (no sudo, no
#                       Containerlab) — always works. This is the demo path.
#   ./run.sh --clab     Also deploy the real Containerlab topology + MPLS/VRF +
#                       SD-WAN overlay + QoS + Prometheus/Grafana (needs sudo +
#                       Docker + containerlab). Falls back to synthetic if any
#                       of that is unavailable.
#   ./run.sh --airgap   Run the whole stack inside a zero-egress network
#                       namespace (loopback only). Proves true air-gap: the
#                       compliance probe reports COMPLIANT and a signed report is
#                       written to .logs/airgap_compliance.json. The dashboard is
#                       then reachable only from inside that namespace.
#   ./run.sh stop       Stop all Aether processes.
#
# Always started:  Ollama (LLM) · NetFlow sim · traffic gen · fault streamer
#                  + inference · NOC dashboard (:8080)
# -----------------------------------------------------------------------------
set -uo pipefail   # NOTE: no -e; we want graceful fallback, not hard exits

REPO="$(cd "$(dirname "$0")" && pwd)"
LOGS="$REPO/.logs"
TOPOLOGY="$REPO/phase1-simulation/topology/aether-lab.clab.yml"
TELEMETRY="$REPO/phase2-telemetry"
mkdir -p "$LOGS"

log()  { echo ""; echo "==> $*"; }
info() { echo "    $*"; }

kill_proc() {
  local pattern="$1" pids
  pids=$(pgrep -f "$pattern" 2>/dev/null || true)
  if [ -n "$pids" ]; then
    info "stopping $pattern (PIDs $pids)"
    kill $pids 2>/dev/null || true
  fi
}

stop_all() {
  log "Stopping Project Aether…"
  kill_proc "phase5-dashboard/app.py"
  kill_proc "phase3-models/fault_streamer.py"
  kill_proc "phase2-telemetry/netflow_simulator.py"
  kill_proc "phase2-telemetry/traffic_generator.py"
  kill_proc "phase2-telemetry/exporter.py"
  info "Ollama left running (shared service). Stop it with: pkill -f 'ollama serve'"
  info "done."
}

start_bg() {
  local name="$1"; shift
  nohup "$@" > "$LOGS/${name}.log" 2>&1 &
  info "[$!] $name  →  $LOGS/${name}.log"
}

wait_http() {  # url label tries
  local url="$1" label="$2" tries="${3:-25}" i
  for ((i=1; i<=tries; i++)); do
    curl -s "$url" >/dev/null 2>&1 && { info "$label ready"; return 0; }
    sleep 1
  done
  info "!! $label not responding at $url after ${tries}s"
  return 1
}

# ── arg parsing ───────────────────────────────────────────────────────────────
MODE="synthetic"
for arg in "$@"; do
  case "$arg" in
    stop)            stop_all; exit 0 ;;
    --clab|--clab=1) MODE="clab" ;;
    --no-clab)       MODE="synthetic" ;;
    --airgap)        MODE="airgap" ;;
    -h|--help)       grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  esac
done

# ── [--airgap] re-launch inside a zero-egress network namespace ────────────────
# A real air-gapped deployment has no outbound route. We reproduce that here with
# a rootless user+network namespace that has only loopback — localhost services
# keep working, but every external host is unreachable, so the compliance probe
# genuinely reports COMPLIANT. NOTE: the dashboard is then reachable only from
# *inside* the namespace (that is the air gap); use the in-namespace verifier
# below, or enter the ns to browse it.
if [ "$MODE" = "airgap" ] && [ -z "${AETHER_NETNS:-}" ]; then
  if ! command -v unshare >/dev/null 2>&1; then
    echo "!! 'unshare' not available — cannot create an air-gapped namespace."; exit 1
  fi
  echo "==> --airgap: re-launching the stack inside a loopback-only network namespace…"
  exec unshare -rn env AETHER_NETNS=1 bash "$0" --airgap
fi
if [ -n "${AETHER_NETNS:-}" ]; then
  ip link set lo up 2>/dev/null || true
fi

# ── clean previous run ────────────────────────────────────────────────────────
log "Stopping any previous Aether processes…"
kill_proc "phase5-dashboard/app.py"
kill_proc "phase3-models/fault_streamer.py"
kill_proc "phase2-telemetry/netflow_simulator.py"
kill_proc "phase2-telemetry/traffic_generator.py"
kill_proc "phase2-telemetry/exporter.py"
sleep 1

CLAB_UP=0

# ── [optional] Containerlab + telemetry stack ────────────────────────────────
if [ "$MODE" = "clab" ]; then
  log "Containerlab requested — checking prerequisites…"
  if command -v containerlab >/dev/null 2>&1 && command -v docker >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    info "Deploying topology (sudo containerlab)…"
    if sudo containerlab destroy -t "$TOPOLOGY" --cleanup >/dev/null 2>&1; true; then :; fi
    if sudo containerlab deploy -t "$TOPOLOGY"; then
      CLAB_UP=1
      info "Applying MPLS / VRF / overlay / QoS…"
      ( cd "$REPO/phase1-simulation/topology" \
        && LAB=aether bash chunk3-setup.sh \
        && LAB=aether bash overlay-setup.sh \
        && LAB=aether bash qos-setup.sh ) || info "!! post-deploy config had errors (continuing)"
      info "Starting Prometheus + Grafana…"
      ( cd "$TELEMETRY" && docker compose up -d ) || info "!! docker compose failed (continuing)"
      start_bg "exporter" python3 "$TELEMETRY/exporter.py"
      wait_http "http://localhost:8000/metrics" "exporter" 15 || true
    else
      info "!! containerlab deploy failed — falling back to synthetic mode"
    fi
  else
    info "!! containerlab/docker/sudo not available — falling back to synthetic mode"
    info "   (the AI stack runs fine on synthetic data; topology is optional)"
  fi
else
  log "Synthetic mode (no Containerlab). Use './run.sh --clab' for the real lab."
fi

# ── always-on services ───────────────────────────────────────────────────────
log "Starting NetFlow simulator (UDP :9995)…"
start_bg "netflow_simulator" python3 "$REPO/phase2-telemetry/netflow_simulator.py"

log "Starting traffic generator…"
start_bg "traffic_generator" python3 "$REPO/phase2-telemetry/traffic_generator.py"

log "Starting fault streamer + inference engine…"
start_bg "fault_streamer" python3 "$REPO/phase3-models/fault_streamer.py"

log "Checking Ollama (Mistral 7B)…"
if [ -n "${AETHER_NETNS:-}" ] && command -v ollama >/dev/null 2>&1; then
  # In the air-gapped namespace the host's Ollama is unreachable (separate netns);
  # start a namespace-local instance — models load from disk, no egress needed.
  info "Starting namespace-local Ollama (air-gap mode)…"
  start_bg "ollama" ollama serve
  wait_http "http://localhost:11434/api/tags" "Ollama" 25 || true
elif pgrep -f "ollama serve" >/dev/null 2>&1; then
  info "Ollama already running"
elif command -v ollama >/dev/null 2>&1; then
  start_bg "ollama" ollama serve
  wait_http "http://localhost:11434/api/tags" "Ollama" 20 || true
else
  info "!! ollama not installed — the copilot will use its offline RAG fallback"
fi

log "Starting NOC Dashboard…"
sleep 2
start_bg "dashboard" python3 "$REPO/phase5-dashboard/app.py"

echo ""
info "waiting for dashboard on :8080 (loads ML models, ~5-15s)…"
if wait_http "http://localhost:8080/api/status" "Dashboard" 30; then
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  Project Aether — up ($([ "$CLAB_UP" = 1 ] && echo 'Containerlab + synthetic' || echo 'synthetic data'))"
  echo ""
  echo "  NOC Dashboard   →  http://localhost:8080"
  [ "$CLAB_UP" = 1 ] && echo "  Prometheus      →  http://localhost:9090"
  [ "$CLAB_UP" = 1 ] && echo "  Grafana         →  http://localhost:3000  (admin/admin)"
  [ "$CLAB_UP" = 1 ] && echo "  Exporter        →  http://localhost:8000/metrics"
  echo "  LLM (Ollama)    →  http://localhost:11434"
  echo ""
  echo "  Logs   →  $LOGS/      Stop   →  ./run.sh stop"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo ""
  curl -s http://localhost:8080/api/status | python3 -m json.tool 2>/dev/null || true

  # ── air-gap mode: prove zero egress with a signed compliance report ──────────
  if [ -n "${AETHER_NETNS:-}" ]; then
    echo ""
    log "Air-gap verification (running inside zero-egress namespace)…"
    ( cd "$REPO/phase3-models" && python3 airgap_compliance.py --out "$LOGS/airgap_compliance.json" ) \
      | sed 's/^/    /' || true
    info "Signed report → $LOGS/airgap_compliance.json"
    info "The dashboard is namespace-local (the air gap). To view it from this"
    info "namespace, run a browser here, or capture it headless from within."
  fi
else
  echo "!! Dashboard did not come up — check $LOGS/dashboard.log"
  exit 1
fi
