# Project Aether — Gap Analysis
**Date:** 2026-06-29  
**Source documents checked:** `PblmStmnt.md`, `idea_v4.md`, `idea (2).md`

---

## Topology bug fixed in this session

The dashboard topology canvas was drawing a direct **PE1 ↔ PE2** link that does not exist physically.  
PE1 and PE2 are only connected **through P1** (the provider core router).  
The SD-WAN backup tunnel overlay is a logical overlay that lives in `graph_model.py` as a high-cost
backup edge used only for rerouting decisions — it is not a physical wire and should not appear on the canvas.  
**Fixed:** removed `['pe1','pe2']` from the JS `LINKS` array in the dashboard.

---

## Problem Statement — Phase-by-Phase Status

### Phase 1 — Network Simulation
| Requirement | Status | Notes |
|---|---|---|
| Multi-site topology: branch, hub, DC, CE/PE/P roles | **DONE** | 7-node Containerlab: pe1, p1, pe2, ce-branch1, ce-branch2, ce-hub, ce-dc |
| MPLS forwarding plane | **DONE** | FRR LDP + MPLS label bindings configured |
| VPN segmentation | **DONE** | BGP VPNv4 L3VPN, VRF CUST, RD/RT 65000:1 |
| Dynamic routing: BGP + OSPF | **DONE** | FRR BGP 65001 + OSPF area 0 |
| SD-WAN IPSec overlay tunnels | **PARTIAL** | Modelled as a high-cost backup graph edge in `graph_model.py`; no actual IPSec tunnel configured in Containerlab |
| QoS policies | **PARTIAL** | QoS shaping exists as a remediation command (`tc htb`) but is not pre-configured on the topology at startup |
| Realistic application traffic flows | **DONE** | `traffic_generator.py`: VoIP 1.5M UDP, DB 8M TCP, bulk 25M TCP |
| Configurable fault injection | **DONE** | `fault_streamer.py --inject <fault>`, NetFlow `/inject?fault=<type>`, tc-netem in topology |

### Phase 2 — Telemetry Pipeline
| Requirement | Status | Notes |
|---|---|---|
| Interface utilisation, latency, jitter | **DONE** | `exporter.py` scrapes FRR + ping RTT/jitter per link |
| BGP/OSPF adjacency events | **DONE** | `syslog_parser.py` handles both native FRR format and ADJCHANGE syslog format |
| NetFlow/IPFIX flow records | **DONE** | `netflow_simulator.py` on port 9995 with 11 synthetic flows + fault injection |
| Tunnel statistics | **DONE** | `graph_model.get_tunnel_health()` → `/api/tunnel-health` endpoint |
| Streaming telemetry | **PARTIAL** | Scrape-based (pull) only; no push/streaming telemetry from a real SD-WAN controller |
| Time-series dataset stays inside air-gap | **DONE** | All data stays local; no egress |
| InfluxDB / Telegraf | **NOT DONE** | The idea doc specifies Telegraf + InfluxDB as the TSDB. The current implementation uses flat JSON files and in-memory scraping. There is no InfluxDB instance. The Time-Travel playback was designed to query InfluxDB range queries — it currently uses in-memory ACP snapshots instead |

### Phase 3 — Predictive Modelling
| Requirement | Status | Notes |
|---|---|---|
| BiLSTM anomaly detection | **DONE** | `LSTMAttentionClassifier` in `predictive_engine.py`, trained, saved |
| LSTM Autoencoder (reconstruction loss) | **DONE** | Runs alongside BiLSTM, feeds anomaly score |
| Time-to-Failure (TTF) regressor | **DONE** | `TimeToFailureRegressor` in `predictive_engine.py` |
| Attention / top-features explainability | **DONE** | `top_features` field in ACP; attention weights exposed |
| EMA self-calibrating threshold | **DONE** | `EMAThreshold` class (alpha=0.05, k=3.0, warmup=50) in inference engine |
| Holt-Winters slow-trend forecaster | **DONE** | `trend_forecaster.py` with `update_batch()` and `forecast_all()` |
| Prophet ensemble | **NOT DONE** | Explicitly deferred in idea_v4 — only if hours of cyclical history exist. Correct decision. |
| Digital Twin divergence | **DONE** | `DigitalTwin` class, wired into inference engine, divergence in ACP |
| Per-service SLA YAML | **DONE** | `config/sla_config.yaml` with voip/database/bulk_transfer/default |
| Model integrity (Ed25519 signing) | **DONE** | `model_integrity.py`, signs `.pt` files with Ed25519; EPE refuses AUTO_EXECUTE on mismatch |
| Lead-time benchmark harness | **DONE** | `benchmark_harness.py` — 5/5 detected, avg 503s before SLA breach |
| Clonal state-space search | **DONE** | `graph_model.py` ClonalGraphEngine — BASELINE, REROUTED_OVERLAY, QOS_THROTTLED |
| Rollback policy if winning permutation worsens congestion | **NOT DONE** | The idea doc explicitly asks for this. Currently there is no post-action telemetry check that reverts a change if it made things worse |

### Phase 4 — Offline LLM Deployment
| Requirement | Status | Notes |
|---|---|---|
| Local quantized LLM | **DONE** | Mistral 7B Q4_K_M via Ollama on port 11434 |
| Zero outbound dependency | **DONE** | All inference is local; Ollama serves from local model files |
| RAG pipeline with local vector DB | **DONE** | ChromaDB + `ikb_manager.py` indexing runbooks + ACP logs |
| RAG over topology metadata | **PARTIAL** | Runbooks and incident history are in ChromaDB; live topology state is not dynamically indexed into ChromaDB (it's passed via ACP context in the prompt) |
| Air-gap compliance report | **DONE** | `airgap_compliance.py` — attempts outbound connections, confirms all fail, signs report |

### Phase 5 — Copilot Integration & Decision Support
| Requirement | Status | Notes |
|---|---|---|
| Structured alert responses: fault type, confidence, root cause, affected sites, TTF | **DONE** | ACP carries all fields; dashboard modal shows them; NLQ uses them as context |
| NLQ natural-language query interface | **DONE** | `/api/nlq` → Ollama Mistral 7B with RAG |
| NLQ conversation manager with intent parsing | **NOT DONE** | idea_v4 specifies a multi-turn conversation manager with intent parser, context state, schema validator, and confirmation gate. Current implementation is single-shot: one question → one answer. No multi-turn state tracking |
| Path Blast Simulator (quick-form what-if) | **NOT DONE** | Specified in idea_v4 as a form-based what-if engine. Not implemented |
| Automated playbook suggestion | **PARTIAL** | Remediation steps are generated per action class (`_build_remediation` in app.py); they are not dynamically ranked or sequenced by the LLM against the current ACP |

### Phase 6 — Scenario Validation
| Requirement | Status | Notes |
|---|---|---|
| Scenario 1: Gradual link degradation | **DONE** | `run_scenarios.py` Scenario 1, `fault_streamer.py --inject LINK_DEGRADATION` |
| Scenario 2: BGP route flap / reroute cascade | **DONE** | `run_scenarios.py` Scenario 2, `fault_streamer.py --inject BGP_ROUTE_FLAP` |
| Scenario 3: Intermittent MPLS failure + tunnel degradation | **DONE** | `run_scenarios.py` Scenario 3 |
| Scenario 4: Controller misconfiguration / policy drift | **DONE** | `run_scenarios.py` Scenario 4 |
| Quantified lead time per scenario printed | **DONE** | Benchmark harness prints Δt before SLA breach |

---

## Idea v4 — Feature-by-Feature Status

| # | Feature | Status | Notes |
|---|---|---|---|
| 1 | Lead-time benchmark harness | **DONE** | `benchmark_harness.py` |
| 2 | Syslog parser (FRR BGP/OSPF) | **DONE** | `syslog_parser.py` handles both formats |
| 3 | Holt-Winters slow-trend forecaster | **DONE** | `trend_forecaster.py` |
| 4 | EMA self-calibrating threshold | **DONE** | `EMAThreshold` class |
| 5 | Ollama + Mistral 7B + ChromaDB | **DONE** | All running locally |
| 6 | Attention heatmap / top-features | **DONE** | `top_features` in ACP schema |
| 7 | Incident KB auto-logging | **DONE** | Every ACP written to `ikb/incidents.jsonl` on arrival |
| 8 | Model integrity check (Ed25519) | **DONE** | `model_integrity.py` |
| 9 | Air-gap compliance report | **DONE** | `airgap_compliance.py` |
| 10 | Digital twin divergence | **DONE** | `DigitalTwin` class + wired into inference engine |
| 11 | Per-service SLA YAML | **DONE** | `config/sla_config.yaml` |
| 12 | Operator feedback loop (CLI) | **DONE** | `feedback_cli.py` accept/reject |
| 13 | Time-Travel Topology Playback | **PARTIAL** | Done in dashboard using in-memory ACP snapshots. The idea doc specifies InfluxDB range queries — InfluxDB is not installed. Visual result is the same but the backing data is ACP snapshots, not full per-second telemetry |

---

## What is genuinely missing (critical gaps)

These are things specified in the problem statement or idea doc that are not implemented,
not partial, and would matter to a judge:

### 1. InfluxDB / Telegraf not running
The idea doc consistently specifies Telegraf + InfluxDB as the telemetry stack.
The current system scrapes metrics via Python directly and stores them in flat JSON files + in-memory.
The Time-Travel playback was designed to query InfluxDB range queries; it currently uses
in-memory ACP snapshots, which only have per-alert granularity (not per-second telemetry).

**Impact:** Time-Travel playback cannot replay per-second link utilisation changes between
alerts — only the moments when an ACP was generated.

**Fix:** Install InfluxDB v2 locally, configure Telegraf to scrape FRR metrics every 1–5s,
point the time-travel slider at real InfluxDB range queries.

### 2. Rollback policy not implemented
The idea doc states: *"A rollback policy reverts a permutation if post-change telemetry
shows it worsened congestion."* This is listed in the autonomy safety floors.

Currently, once a remediation command runs (AUTO_EXECUTE or operator-approved), the system
does not monitor post-action telemetry and does not revert.

**Impact:** If a reroute worsens congestion, Aether will not notice unless a new ACP fires.

**Fix:** After executing a remediation, schedule a telemetry re-check at t+60s. If the same
fault class reappears at equal or higher severity, issue a rollback command and log it.

### 3. Multi-turn NLQ conversation manager
The idea doc specifies a stateful conversation manager: intent parser → context state →
schema validator → confirmation gate → graph model → response synthesiser.

Current implementation is single-shot: one message in, one message out, no state between turns.

**Impact:** Operators cannot do iterative what-if queries ("what if I also increase traffic to DC?").

**Fix:** Add a server-side conversation session store (dict keyed by session ID) and a
multi-turn loop in `/api/nlq` that tracks filled schema fields before calling the graph model.

### 4. Path Blast Simulator not built
The idea doc specifies a quick-form what-if: Source / Dest / Traffic / RUN fields that
pre-fill the NLQ operation schema and run the same graph model call.

**Impact:** Evaluators may specifically look for this — it is shown in the idea doc mockup.

**Fix:** Add a new page/tab in the dashboard with 4 form fields that POST to a new
`/api/what-if` endpoint that runs the graph model on the specified degradation and returns
projected SLA impact.

### 5. IPSec overlay tunnels not actually configured
The SD-WAN IPSec overlay is modelled only as a high-cost graph edge. There is no actual
IPSec/GRE tunnel between PE1 and PE2 in the Containerlab topology.

**Impact:** When REROUTE_BRANCH executes, the vtysh OSPF cost command changes routing decisions
but there is no actual tunnel for traffic to traverse — packets would use direct routing,
not an overlay.

**Fix (demo-safe):** Add an explicit comment in the topology and remediation steps that the
overlay is a logical model. A real fix requires adding a GRE/VXLAN tunnel interface in
`topology/aether-lab.clab.yml`.

### 6. Live topology state not in ChromaDB
RAG retrieves from runbooks and past incidents. Live topology state (current utilisation,
active faults, which links are degraded) is passed in the prompt as plain text but is not
indexed into ChromaDB as a queryable document.

**Impact:** The LLM cannot answer questions that require looking up historical topology states
across multiple past incidents (e.g. "has pe1-p1 been the bottleneck before?").

**Fix:** Periodically write a topology snapshot document into ChromaDB (every 5 minutes or
on every ACP), keyed by timestamp. The NLQ retrieval then finds relevant past topology states.

---

## What is synthetic / not real (honest accounting)

| Component | Real or synthetic | Detail |
|---|---|---|
| Link utilisation in topology overlay | **SYNTHETIC** | `/api/metrics/live` uses a Gaussian random walk, not actual FRR counters |
| NetFlow records | **SYNTHETIC** | `netflow_simulator.py` generates fictional flows; no actual IPFIX capture |
| Traffic flows | **SYNTHETIC FALLBACK** | `traffic_generator.py` uses iperf3 if Containerlab is running, else synthetic JSON |
| Tunnel health | **DERIVED** | `graph_model.get_tunnel_health()` computes health from graph edge state, not MPLS OAM probes |
| Fault timing | **REAL** | `fault_streamer.py` uses natural QUIET→FAULT→RESOLVE state machine with real timing |
| ML inference | **REAL** | BiLSTM + Autoencoder + TTF regressor run real PyTorch inference on every scrape cycle |
| LLM responses | **REAL** | Mistral 7B generates real tokens locally; ChromaDB RAG retrieves real ACP documents |
| Remediation commands | **REAL ATTEMPTS** | `/api/execute-action` actually runs `docker exec` commands; if Containerlab is not running, the log shows the real Docker error |
| Action log | **REAL** | `action_log.jsonl` is written on every auto-execute and operator approval |
| Air-gap compliance | **REAL** | Attempts real outbound connections and confirms failure |
| Model integrity | **REAL** | Ed25519 signatures verified against real private key at startup |

---

## Dashboard: what each section shows

| View | Data source | Real? |
|---|---|---|
| Network → topology | ACP fault events + Gaussian random walk | Links are real ACP data; link utilisation overlay is synthetic |
| Network → Detection pipeline | Static explainer | N/A |
| Alerts | `acp_logs/*.json` via WebSocket | Real ACP files from inference engine |
| Ask Aether | Ollama Mistral 7B + ChromaDB | Real LLM inference |
| History | In-memory ACP snapshots | Real ACP events, not per-second telemetry |
| Policy Matrix | `phase3-models/policy_overrides.json` | Real — edits take effect immediately |
| Remediation Log | `phase3-models/action_log.jsonl` | Real — actual command output |

---

## Commands to run everything

```bash
# Terminal 1 — Ollama (LLM backend)
ollama serve

# Terminal 2 — NOC Dashboard  (port 8080)
cd ~/air-gapped-mpls-copilot
python3 phase5-dashboard/app.py

# Terminal 3 — Fault streamer + inference engine (generates real ACPs)
cd ~/air-gapped-mpls-copilot
python3 phase3-models/fault_streamer.py

# Terminal 4 — NetFlow simulator (port 9995)
cd ~/air-gapped-mpls-copilot
python3 phase2-telemetry/netflow_simulator.py

# Terminal 5 — Traffic generator
cd ~/air-gapped-mpls-copilot
python3 phase2-telemetry/traffic_generator.py

# Optional — run the Phase 6 scenario validation suite
cd ~/air-gapped-mpls-copilot
python3 phase6-validation/run_scenarios.py --no-containerlab

# Optional — run the lead-time benchmark
cd ~/air-gapped-mpls-copilot
python3 phase3-models/benchmark_harness.py

# Optional — view/label past ACPs from CLI
cd ~/air-gapped-mpls-copilot
python3 phase3-models/feedback_cli.py

# Optional — Containerlab (real network, needs root + Docker)
cd ~/air-gapped-mpls-copilot
sudo containerlab deploy -t topology/aether-lab.clab.yml
```

All 5 core processes are already running. Dashboard is at http://localhost:8080.
