# Project Aether — Air-Gapped Predictive Copilot for Secure MPLS Operations

> Edge-Assisted Predictive Resilience and Heuristic Mitigation Framework for Air-Gapped Networks

**Hackathon:** Bharatiya Antariksh Hackathon 2026 (ISRO × Hack2skill)  
**Problem Statement 13:** Air-Gapped Predictive Copilot for Secure MPLS Operations

An autonomous, offline AI NOC Copilot that predicts network failures before SLA breach, mathematically validates those predictions, selects optimal mitigation from first principles, and explains everything in plain English — with **zero cloud dependency**.

---

## Team
- Charan Teja 
- Yogeshwar
- Aranya Roy
- Pradyumna

---

## The Core Insight

Two independently computed predictions agreeing is stronger evidence than either alone:

| Layer | Type | What it answers |
|---|---|---|
| LSTM (stochastic) | ML | "Does this telemetry pattern resemble past failures?" |
| NetworkX graph (deterministic) | Analytical | "Will projected traffic mathematically saturate this queue?" |

**Both agree → fast action (confidence-gated auto-execute).**  
**Disagree → safety trip, operator review — no autonomous action.**

A stochastic model never directly controls infrastructure. Only the deterministic Policy Engine executes changes, and only above the operator-configured confidence threshold.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           PROJECT AETHER                                │
│                                                                         │
│  Phase 1: Simulation          Phase 2: Telemetry                        │
│  ┌──────────────────┐         ┌─────────────────┐                       │
│  │ Containerlab     │──exec──▶│ exporter.py     │──scrape──▶ Prometheus │
│  │ 7-Node MPLS L3VPN│         │ (threaded, 8000)│           + Grafana   │
│  │ pe1,p1,pe2       │         └────────┬────────┘                       │
│  │ ce-branch1/2     │                  │ dataset.csv                    │
│  │ ce-hub, ce-dc    │◀──tc netem──┐    ▼                                │
│  └──────────────────┘            │  data_collector.py                  │
│                                  │  (labels rows with fault_type)       │
│  fault_injector.py ──────────────┘                                      │
│  scenario_runner.sh                                                     │
│                                                                         │
│  Phase 3: Predictive Engine                                             │
│  ┌───────────────────────────────────────────────────────────────┐      │
│  │  taxonomy.py (single source of truth — 6 classes, policy)    │      │
│  │                                                               │      │
│  │  LSTM Autoencoder   ──▶ anomaly score                        │      │
│  │  LSTM Classifier    ──▶ fault class + confidence             │      │
│  │  TTF Regressor      ──▶ seconds to SLA breach                │      │
│  │         │                        │                            │      │
│  │         ▼                        ▼                            │      │
│  │  acp_manager.py         graph_model.py                       │      │
│  │  (Anomaly Context       (Clonal State-Space Search)          │      │
│  │   Packet — JSON)        BASELINE / REROUTED / QOS_THROTTLED  │      │
│  │         │                        │                            │      │
│  │         └──────┬─────────────────┘                            │      │
│  │                ▼                                               │      │
│  │    aether_corroborator.py                                     │      │
│  │    AGREE  → Autonomy Policy → AUTO_EXECUTE                    │      │
│  │    DISAGREE → SAFETY TRIP  → RECOMMEND_ONLY                   │      │
│  └───────────────────────────────────────────────────────────────┘      │
│                                                                         │
│  Phase 4: Offline LLM                     Phase 5: NOC UI                │
│  ┌──────────────────────┐                ┌───────────────────────┐      │
│  │ Ollama + Mistral 7B  │──Q1/Q2/Q3──▶  │ FastAPI app.py        │      │
│  │ ChromaDB RAG (IKB)   │                │ WebSocket alerts      │      │
│  │ ikb_manager.py       │                │ NLQ chatbox           │      │
│  │ llm_copilot.py       │                │ Topology SVG          │      │
│  │ nlq_interface.py     │                │ Autonomy dial         │      │
│  └──────────────────────┘                └───────────────────────┘      │
│                                                                         │
│  Cross-cutting (v4):                                                    │
│  EMA threshold │ Syslog parser │ Digital Twin │ Attention heatmap       │
│  Ed25519 signing │ Air-gap compliance │ Benchmark harness │ Feedback CLI│                                               │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Phase Status

| Phase | Description | Status |
|---|---|---|
| **1** | 7-node MPLS L3VPN (Containerlab + FRR) — CE/PE/P, OSPF/LDP/VPNv4 | ✅ Complete |
| **1** | Traffic generation (VoIP, DB, HTTP, SSH across L3VPN) | ✅ Complete |
| **1** | Fault injection — 5 types (latency, loss, corrupt, rate, flap) via tc-netem | ✅ Complete |
| **1** | Scenario runner (all 4 validation scenarios, timed) | ✅ Complete |
| **2** | Custom threaded Prometheus exporter — 350+ features at 1 s | ✅ Complete |
| **2** | Prometheus + Grafana via Docker Compose | ✅ Complete |
| **2** | Labeled dataset collection (fault_injector → dataset.csv) | ✅ Complete |
| **3** | LSTM Autoencoder (unsupervised anomaly) | ✅ Complete |
| **3** | LSTM Attention Classifier (6-class fault) | ✅ Complete |
| **3** | TTF Regressor (time-to-SLA-breach) | ✅ Complete |
| **3** | NetworkX Clonal State-Space Search | ✅ Complete |
| **3** | Dual-model corroboration gate + autonomy policy matrix | ✅ Complete |
| **3** | Anomaly Context Packet (ACP) JSON schema | ✅ Complete |
| **3** | Fault taxonomy single source of truth (taxonomy.py) | ✅ Complete |
| **3** | EMA self-calibrating threshold (Bollinger-band, replaces fixed 3×) | ✅ Complete |
| **3** | Attention heatmap linear head (per-feature explainability) | ✅ Complete |
| **3** | Digital Twin (Holt-Winters forecast vs actual graph divergence) | ✅ Complete |
| **3** | Ed25519 model signing + air-gap compliance reporter | ✅ Complete |
| **3** | Per-service SLA YAML + graph scoring (voip / database / bulk) | ✅ Complete |
| **3** | FRR syslog parser (BGP/OSPF adjacency events) | ✅ Complete |
| **3** | Lead-time benchmark harness (9/9 detected, avg 343s before breach) | ✅ Complete |
| **3** | Operator feedback CLI (accept/reject → IKB false-positive rate) | ✅ Complete |
| **3** | Continuous fault loop (30+ variants, gradual ramp, BGP cascade) | ✅ Complete |
| **4** | Offline LLM copilot (Ollama + Mistral 7B, graceful offline fallback) | ✅ Complete |
| **4** | ChromaDB RAG over ACP logs + topology runbooks (ikb_manager.py) | ✅ Complete |
| **4** | NLQ interface (intent detection, IKB retrieval, fallback answers) | ✅ Complete |
| **4** | Runbooks: BGP flap, congestion, packet loss, topology reference | ✅ Complete |
| **5** | FastAPI NOC dashboard (app.py — topology, alerts, NLQ, compliance) | ✅ Complete |
| **5** | WebSocket live alert stream + 4s polling dual-path fallback | ✅ Complete |
| **5** | Left-sidebar SPA — 5 views: Live Network, Alert Feed, NLQ, Time-Travel, Autonomy Matrix | ✅ Complete |
| **5** | Live topology SVG — pe1↔p1 link highlighted on fault (state-based, no flicker) | ✅ Complete |
| **5** | Time-Travel Topology Playback — slider scrubs ACP snapshot history | ✅ Complete |
| **5** | Autonomy Matrix editor — live-editable policy table with per-row PUT to `/api/policy` | ✅ Complete |
| **5** | Fault streamer (fault_streamer.py) — real dataset rows, diverse cycle, 4s interval | ✅ Complete |
| **6** | Benchmark validation — 4 ISRO scenarios, lead-time measurement | ✅ Complete |

---

## Dashboard Features (v5.0)

The NOC dashboard (`http://localhost:8080`) is a single-page application with a collapsible left sidebar:

| Panel | Description |
|---|---|
| **Live Network** | MPLS topology SVG with animated red dashed links for active faults. pe1↔p1 highlighted whenever any non-Healthy ACP is active — state-driven, no flicker. Recent alerts alongside topology. |
| **Alert Feed** | Full scrollable history of every ACP: fault class, severity, confidence, TTF, execution mode, rationale. Click any alert to load explanation in the Copilot panel. |
| **NLQ Copilot** | Natural-language interface to Mistral 7B (offline). Ask free-form questions; the RAG pipeline retrieves runbook context from ChromaDB before generating. Graceful offline fallback if Ollama is unavailable. |
| **Time-Travel** | Slider scrubs backward through every ACP snapshot captured since the dashboard started. Left side re-renders the topology at that historical state; right side shows the ACP event details (fault class, severity, conf, TTF, rationale, degraded links). |
| **Autonomy Matrix** | Live editor for the operator autonomy policy. Editable rows: `REROUTE_BRANCH`, `QOS_SHAPE_QUEUE`. Locked rows (grey lock icon): `CORE_PATH_FAILOVER`, `NODE_ISOLATION`, `NO_ACTION`. Changes persisted to `phase3-models/policy_overrides.json` and applied to the running corroborator immediately. |

### Autonomy Policy Matrix

The matrix controls when the Edge Policy Engine may act autonomously vs. surface a recommendation:

| Action Class | Default Min Confidence | Default Mode | Editable |
|---|---|---|---|
| `REROUTE_BRANCH` | 80% | AUTO_EXECUTE | ✅ |
| `QOS_SHAPE_QUEUE` | 75% | AUTO_EXECUTE | ✅ |
| `CORE_PATH_FAILOVER` | 90% | RECOMMEND_ONLY | 🔒 locked |
| `NODE_ISOLATION` | 99% | RECOMMEND_ONLY | 🔒 locked |
| `NO_ACTION` | 100% | — | 🔒 locked |

Safety floors enforced in code regardless of operator settings: model disagreement always downgrades to `RECOMMEND_ONLY`; hub/DC-scope actions are never auto-executed.

### Time-Travel Topology Playback

Every ACP that arrives (via WebSocket or poll) is captured as a topology snapshot in the browser's in-memory history buffer. The time-travel slider indexes this buffer. Dragging left replays older states — the topology re-renders showing which links were degraded at that moment. Clicking **▶ Live** returns to the latest state.

---

## Quick Start

```bash
# Install PyTorch with CUDA 12.8 (RTX 4060) — skip second line for CPU-only
pip install torch --index-url https://download.pytorch.org/whl/cu128
pip install -r requirements.txt

# ── Phase 1: Deploy lab ─────────────────────────────────────────────
cd phase1-simulation/topology
sudo clab deploy -t chunk3.clab.yml
sudo ./chunk3-setup.sh           # MPLS kernel modules + OSPF/LDP/BGP/VRF
bash traffic_generator.sh &      # VoIP / DB / HTTP / SSH flows

# ── Phase 2: Telemetry ─────────────────────────────────────────────
cd ../../
python3 phase2-telemetry/exporter.py &          # binds port 8000
cd phase2-telemetry && docker compose up -d     # Prometheus:9090, Grafana:3000

# ── Dataset: generate synthetic (no lab needed) ────────────────────
python3 phase3-models/generate_dataset.py       # → phase3-models/dataset_large.csv (100k rows)

# ── OR collect real telemetry (requires lab running) ──────────────
bash phase1-simulation/topology/continuous_fault_loop.sh &   # 30+ fault variants
python3 phase3-models/data_collector.py --duration 14400 --output phase3-models/dataset_large.csv

# ── Phase 3: Train ─────────────────────────────────────────────────
python3 phase3-models/train_models.py \
    --data phase3-models/dataset_large.csv \
    --epochs 35 --seq-len 20 --batch-size 64
python3 phase3-models/model_integrity.py --sign   # Ed25519 sign all models
python3 phase3-models/model_integrity.py --verify # Verify signatures

# ── Phase 3: Run inference ─────────────────────────────────────────
python3 phase3-models/inference_engine.py --demo
python3 phase3-models/aether_corroborator.py

# ── Phase 4: LLM Copilot (one-time setup, needs internet) ─────────
bash phase4-llm/setup_llm.sh                     # install Ollama + pull Mistral 7B + seed IKB

# After setup, all of these run 100% offline:
python3 phase4-llm/ikb_manager.py --seed         # re-seed ChromaDB runbooks (idempotent)
python3 phase4-llm/ikb_manager.py --ingest-acps  # sync incident log → ChromaDB
python3 phase4-llm/nlq_interface.py              # interactive NLQ terminal

# ── Phase 5: NOC Dashboard ─────────────────────────────────────────
python3 phase5-dashboard/app.py                  # http://localhost:8080
```

---

## Tech Stack

| Layer | Technology | Notes |
|---|---|---|
| Network sim | Containerlab 0.76 + FRRouting (FRR) | 7 nodes, Alpine-based |
| Telemetry | Custom Python exporter (stdlib only) | Threaded, 350+ features/node |
| Time-series DB | Prometheus 2.x + Grafana | Docker Compose, local only |
| ML models | PyTorch 2.x — LSTM Autoencoder, BiLSTM+Attention, TTF Regressor | CUDA (RTX 4060) / CPU fallback |
| Graph engine | NetworkX + Clonal State-Space Search | Millisecond-range for ≤50 nodes |
| Offline LLM | Ollama + Mistral 7B (mistral:7b-instruct-q4_K_M) | Graceful offline fallback |
| RAG / vector DB | ChromaDB (local persistent) + sentence-transformers | all-MiniLM-L6-v2, 100% offline |
| Frontend | FastAPI + inline HTML/JS | WebSocket alerts, NLQ chatbox, topology SVG |
| Trend forecasting | statsmodels Holt-Winters + linear slope fallback | Digital twin divergence signal |
| Model integrity | Ed25519 (cryptography library) | Private key offline, .pub.pem committed |
| Fault injection | `tc netem` / `tc tbf` / `ip link` via `docker exec` | No host kernel changes needed |
| Air-gap | Zero outbound deps at runtime | All images and models pre-loaded |

---

## Key Design Decisions

**Why two models instead of one?**  
A stochastic LSTM can detect *that* something is wrong; a deterministic graph model checks *whether the topology can mathematically support the predicted reroute*. Requiring agreement eliminates a whole class of false positives (ML fires on noise the graph shows is benign).

**Why taxonomy.py?**  
The original codebase defined fault class-id mappings in 5 separate files that disagreed with each other (id 4 meant "rate" in training but "Congestion" in inference, with a completely different display name). `taxonomy.py` is the single source of truth — all other files import from it.

**Why a custom Prometheus exporter instead of Telegraf?**  
Air-gap compliance. Telegraf has external plugin dependencies. The custom exporter is pure Python stdlib — zero egress, no package manager needed at runtime.

**Why `127.0.0.1` not `localhost` in the collector?**  
On Fedora / modern Linux, `localhost` resolves to `::1` (IPv6) but the exporter binds IPv4-only. urllib timeouts silently rather than refuse — fixed by using the explicit IPv4 loopback.

---

## Repository Structure

```
air-gapped-mpls-copilot/
├── README.md
├── requirements.txt
│
├── phase1-simulation/
│   └── topology/
│       ├── chunk3.clab.yml          # Containerlab topology (7 nodes)
│       ├── chunk3-setup.sh          # MPLS kernel + FRR config (OSPF/LDP/BGP/VRF)
│       ├── traffic_generator.sh     # VoIP + DB + HTTP + SSH flows via iperf3/hping3
│       ├── fault_injector.py        # tc netem/tbf fault injection (5 types)
│       ├── scenario_runner.sh       # Timed validation scenarios
│       └── continuous_fault_loop.sh # 30+ fault variants, 4-hour data collection
│
├── phase2-telemetry/
│   ├── exporter.py                  # Custom Prometheus exporter (stdlib only, 350+ metrics)
│   ├── docker-compose.yml           # Prometheus + Grafana stack
│   └── dashboards/                  # Grafana dashboard JSON exports
│
├── phase3-models/
│   ├── taxonomy.py            # SINGLE SOURCE OF TRUTH — 6 fault classes + action matrix
│   ├── generate_dataset.py    # Synthetic dataset generator (100k rows, no lab needed)
│   ├── data_collector.py      # Live telemetry → labeled CSV bridge
│   ├── train_models.py        # Training script (all 3 models, CUDA, CosineAnnealingLR)
│   ├── predictive_engine.py   # Model definitions (Autoencoder, BiLSTM+Attention, Regressor)
│   ├── graph_model.py         # NetworkX clonal state-space search (SLA-aware)
│   ├── acp_manager.py         # Anomaly Context Packet schema + IKB audit log
│   ├── aether_corroborator.py # Dual-model corroboration gate + EPE + digital twin PSF
│   ├── inference_engine.py    # Live inference: EMA threshold, 3-model pipeline
│   ├── digital_twin.py        # Holt-Winters forecast → graph divergence
│   ├── trend_forecaster.py    # EMA + Holt-Winters per-channel forecasting
│   ├── model_integrity.py     # Ed25519 model signing + verification
│   ├── airgap_compliance.py   # Network probe + signed compliance report
│   ├── syslog_parser.py       # FRR syslog → BGP/OSPF event + instability score
│   ├── benchmark_harness.py   # Lead-time benchmark (9/9 scenarios, avg 343s)
│   ├── feedback_cli.py        # Operator accept/reject ACP → IKB false-positive stats
│   ├── dataset_large.csv      # 100k-row synthetic training dataset (78 MB)
│   ├── saved/                 # Trained model weights + normalization params + signatures
│   ├── keys/                  # aether_model_key.pub.pem (private key stays offline)
│   └── ikb/incidents.jsonl    # Append-only ACP audit log
│
├── phase4-llm/
│   ├── ikb_manager.py         # ChromaDB CRUD (seed runbooks, ingest ACPs, query)
│   ├── llm_copilot.py         # AetherCopilot: RAG pipeline + Mistral 7B + fallback
│   ├── nlq_interface.py       # Interactive NLQ terminal (intent detection + clarification)
│   ├── setup_llm.sh           # One-time setup: Ollama + Mistral + deps + IKB seed
│   ├── chroma_db/             # ChromaDB persistent vector store
│   └── runbooks/
│       ├── topology.md              # Node/interface/VRF reference
│       ├── mpls_bgp_flap.md         # BGP/OSPF flap diagnosis + recovery
│       ├── packet_loss_corruption.md # Loss + corruption runbook
│       └── congestion_saturation.md  # Rate limiting + QoS runbook
│
├── phase5-dashboard/
│   └── app.py                 # FastAPI NOC dashboard v5.0 — sidebar SPA, 10 endpoints, WebSocket
│                              #   GET  /api/policy    — autonomy matrix (ACTION_POLICY + overrides)
│                              #   PUT  /api/policy    — operator live-edit (locked: CORE/NODE/NONE)
│                              #   GET  /api/acps      — ACP history from acp_logs/ (full JSON)
│                              #   WS   /ws/alerts     — live ACP stream (top_features included)
│
└── docs/
    ├── phase1-simulation-doc/
    │   ├── chunk1.md    # Core node setup (pe1, p1, pe2)
    │   ├── chunk2.md    # MPLS LDP core
    │   ├── chunk3.md    # L3VPN (VRF + MP-BGP VPNv4) ← main topology doc
    │   ├── chunk4.md    # Traffic generation
    │   └── chunk5.md    # Fault injection + ground-truth labelling
    ├── phase2-telemetry-doc/
    │   └── telemetry.md       # Exporter, Prometheus, dataset schema
    ├── phase3-models-doc/
    │   └── aether_engine.md   # Models, graph engine, corroboration, ACP schema, metrics
    ├── phase4-llm-doc/
    │   └── llm_copilot.md     # IKB manager, RAG pipeline, NLQ interface, offline setup
    └── phase5-integration-doc/
        └── noc_dashboard.md   # FastAPI endpoints, WebSocket protocol, UI layout
```
