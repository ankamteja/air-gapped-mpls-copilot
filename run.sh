#!/usr/bin/env bash
# =============================================================================
# run.sh — Start (or stop) the Project Aether stack (PS-13)
#
# Usage:
#   ./run.sh                  SYNTHETIC mode (no sudo, no Containerlab) — always
#                             works. This is the default demo path.
#   ./run.sh --clab           Deploy the real Containerlab topology + MPLS/VRF +
#                             SD-WAN overlay + QoS + Prometheus/Grafana (needs
#                             sudo + Docker + containerlab). Falls back to
#                             synthetic if any of that is unavailable.
#   ./run.sh --airgap         Run the SYNTHETIC stack inside a zero-egress
#                             network namespace (loopback only). Proves true
#                             air-gap: the compliance probe reports COMPLIANT and
#                             a signed report is written to
#                             .logs/airgap_compliance.json. The dashboard is then
#                             reachable only from inside that namespace.
#   ./run.sh --clab --airgap  Real Containerlab, but the lab data-plane is put on
#                             an INTERNAL docker network (no internet egress), so
#                             the simulated network is genuinely air-gapped.
#   ./run.sh stop             Stop all Aether processes (and the clab topology).
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
  kill_proc "ollama serve"   # includes any namespace-local instance from --airgap
  if command -v containerlab >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    if sudo containerlab inspect -t "$TOPOLOGY" >/dev/null 2>&1; then
      info "destroying Containerlab topology…"
      sudo containerlab destroy -t "$TOPOLOGY" --cleanup >/dev/null 2>&1 || true
    fi
  fi
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

# ── arg parsing (flags compose: --clab and --airgap can both be passed) ────────
WANT_CLAB=0
WANT_AIRGAP=0
for arg in "$@"; do
  case "$arg" in
    stop)            stop_all; exit 0 ;;
    --clab|--clab=1) WANT_CLAB=1 ;;
    --no-clab)       WANT_CLAB=0 ;;
    --airgap)        WANT_AIRGAP=1 ;;
    -h|--help)       grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
  esac
done

# ── [--airgap, synthetic] re-launch inside a zero-egress network namespace ─────
# A real air-gapped deployment has no outbound route. For the synthetic stack we
# reproduce that with a rootless user+network namespace that has only loopback —
# localhost services keep working, but every external host is unreachable, so the
# compliance probe genuinely reports COMPLIANT. (Docker/Containerlab can't run in
# this rootless ns, so --clab --airgap uses an internal docker network instead.)
if [ "$WANT_AIRGAP" = 1 ] && [ "$WANT_CLAB" = 0 ] && [ -z "${AETHER_NETNS:-}" ]; then
  if ! command -v unshare >/dev/null 2>&1; then
    echo "!! 'unshare' not available — cannot create an air-gapped namespace."; exit 1
  fi
  echo "==> --airgap: re-launching the synthetic stack inside a loopback-only namespace…"
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
AIRGAP_DATAPLANE=0

# ── [optional] Containerlab + telemetry stack ────────────────────────────────
if [ "$WANT_CLAB" = 1 ]; then
  log "Containerlab requested — checking prerequisites…"
  if command -v containerlab >/dev/null 2>&1 && command -v docker >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
    # --clab --airgap: put the lab management network on an INTERNAL docker bridge
    # (no NAT, no gateway) so the simulated nodes have zero internet egress.
    if [ "$WANT_AIRGAP" = 1 ]; then
      if ! sudo docker network inspect clab >/dev/null 2>&1; then
        if sudo docker network create --internal --subnet 172.20.20.0/24 clab >/dev/null 2>&1; then
          AIRGAP_DATAPLANE=1
          info "air-gap: created INTERNAL 'clab' mgmt network (no egress) for the lab"
        else
          info "!! could not create internal 'clab' network — lab may retain egress"
        fi
      else
        # network already exists; report whether it is internal
        if [ "$(sudo docker network inspect clab -f '{{.Internal}}' 2>/dev/null)" = "true" ]; then
          AIRGAP_DATAPLANE=1
          info "air-gap: existing 'clab' network is internal (no egress)"
        else
          info "!! existing 'clab' network is NOT internal — run './run.sh stop' first to reset"
        fi
      fi
    fi
    info "Deploying topology (sudo containerlab)…"
    sudo containerlab destroy -t "$TOPOLOGY" --cleanup >/dev/null 2>&1 || true
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
elif [ -n "${AETHER_NETNS:-}" ]; then
  log "Air-gapped synthetic mode (zero-egress namespace)."
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
# Check *reachability*, not just whether a process named 'ollama serve' exists —
# a stale or namespace-local instance can hold the process name without actually
# serving :11434 on this network namespace.
if curl -s -m 3 http://localhost:11434/api/tags >/dev/null 2>&1; then
  info "Ollama already reachable on :11434"
elif command -v ollama >/dev/null 2>&1; then
  info "Starting Ollama…"
  start_bg "ollama" ollama serve
  wait_http "http://localhost:11434/api/tags" "Ollama" 25 || true
else
  info "!! ollama not installed — the copilot will use its offline RAG fallback"
fi

log "Starting NOC Dashboard…"
sleep 2
start_bg "dashboard" python3 "$REPO/phase5-dashboard/app.py"

echo ""
info "waiting for dashboard on :8080 (loads ML models, ~5-15s)…"
if wait_http "http://localhost:8080/api/status" "Dashboard" 30; then
  if   [ -n "${AETHER_NETNS:-}" ];   then _mode="air-gapped synthetic (zero-egress namespace)"
  elif [ "$CLAB_UP" = 1 ] && [ "$AIRGAP_DATAPLANE" = 1 ]; then _mode="Containerlab (air-gapped data-plane)"
  elif [ "$CLAB_UP" = 1 ]; then _mode="Containerlab + synthetic"
  else _mode="synthetic data"; fi
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  Project Aether — up ($_mode)"
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

  # ── air-gap reporting ────────────────────────────────────────────────────────
  if [ -n "${AETHER_NETNS:-}" ]; then
    echo ""
    log "Air-gap verification (running inside zero-egress namespace)…"
    ( cd "$REPO/phase3-models" && python3 airgap_compliance.py --out "$LOGS/airgap_compliance.json" ) \
      | sed 's/^/    /' || true
    info "Signed report → $LOGS/airgap_compliance.json"
    info "The dashboard is namespace-local (the air gap). Browse it from inside"
    info "the namespace, or capture it headless from within."
  elif [ "$CLAB_UP" = 1 ] && [ "$AIRGAP_DATAPLANE" = 1 ]; then
    echo ""
    log "Air-gapped data-plane: lab nodes are on an internal docker network (no egress)."
    info "The simulated MPLS/SD-WAN network has no internet route."
    info "Note: a fully air-gapped *host* is the deployment target — run on an"
    info "offline machine, or use './run.sh --airgap' for the zero-egress synthetic demo."
  fi
else
  echo "!! Dashboard did not come up — check $LOGS/dashboard.log"
  exit 1
fi
