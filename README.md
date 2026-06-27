# Project Aether — Air-Gapped Predictive Copilot for Secure MPLS Operations

> Edge-Assisted Predictive Resilience and Heuristic Mitigation Framework for Air-Gapped Networks

**Hackathon:** Bharatiya Antariksh Hackathon 2026 (ISRO × Hack2skill)  
**Problem Statement 13:** Air-Gapped Predictive Copilot for Secure MPLS Operations

An autonomous, offline AI NOC Copilot that predicts network failures before SLA breach, mathematically validates those predictions, selects optimal mitigation from first principles, and explains everything in plain English — with **zero cloud dependency**.

---

## Team
- Charan Teja (Lead)
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
│  Phase 4: Offline LLM (planned)          Phase 5: NOC UI (planned)     │
│  ┌──────────────────────┐                ┌───────────────────────┐      │
│  │ Ollama + Mistral 7B  │                │ FastAPI dashboard      │      │
│  │ ChromaDB RAG         │                │ Natural-language query │      │
│  │ Incident Knowledge   │                │ Autonomy dial UI       │      │
│  │ Base (IKB)           │                └───────────────────────┘      │
│  └──────────────────────┘                                               │
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
| **4** | Offline LLM (Ollama + Mistral 7B / LLaMA 3 8B) | 🔲 Planned |
| **4** | ChromaDB RAG over ACP logs + topology runbooks | 🔲 Planned |
| **5** | FastAPI NOC dashboard + natural language query | 🔲 Planned |
| **6** | Benchmark validation — 4 ISRO scenarios, lead-time measurement | 🔲 Planned |

---

## Quick Start

```bash
# Install dependencies (CPU dev; use requirements.txt for GPU training)
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

# ── Collect labeled training data ──────────────────────────────────
python3 phase3-models/data_collector.py --duration 600 --output phase3-models/dataset.csv &
cd phase1-simulation/topology && bash scenario_runner.sh all   # inject all 4 scenarios

# ── Phase 3: Train & run ───────────────────────────────────────────
python3 phase3-models/train_models.py --data phase3-models/dataset.csv --epochs 50
python3 phase3-models/inference_engine.py --demo
python3 phase3-models/aether_corroborator.py    # full corroboration pipeline demo
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
| Offline LLM | Ollama + Mistral 7B / LLaMA 3 8B (GGUF Q4_K_M) | Phase 4 — not yet built |
| RAG / vector DB | ChromaDB (local) | Phase 4 — not yet built |
| Frontend | FastAPI + HTML/JS | Phase 5 — not yet built |
| Fault injection | `tc netem` / `tc tbf` / `ip link` via `docker exec` | No host kernel changes needed |
| Air-gap | Zero outbound deps at runtime | All images and models pre-loaded |

---

## Key Design Decisions

**Why two models instead of one?**  
A stochastic LSTM can detect *that* something is wrong; a deterministic graph model checks *whether the topology can mathematically support the predicted reroute*. Requiring agreement eliminates a whole class of false positives (ML fires on noise the graph shows is benign).

**Why taxonomy.py?**  
agy's original code defined fault class-id mappings in 5 separate files that all disagreed (id 4 meant "rate" in training but "Congestion" in inference, with a completely different display name). `taxonomy.py` is the single source of truth — all other files import from it.

**Why a custom Prometheus exporter instead of Telegraf?**  
Air-gap compliance. Telegraf has external plugin dependencies. The custom exporter is pure Python stdlib — zero egress, no package manager needed at runtime.

**Why `127.0.0.1` not `localhost` in the collector?**  
On Fedora / modern Linux, `localhost` resolves to `::1` (IPv6) but the exporter binds IPv4-only. urllib timeouts silently rather than refuse — fixed by using the explicit IPv4 loopback.

---

## Documentation

```
docs/
├── phase1-simulation-doc/
│   ├── chunk1.md    # Core node setup (pe1, p1, pe2)
│   ├── chunk2.md    # MPLS LDP core
│   ├── chunk3.md    # L3VPN (VRF + MP-BGP VPNv4) ← main topology doc
│   ├── chunk4.md    # Traffic generation
│   └── chunk5.md    # Fault injection + ground-truth labelling
├── phase2-telemetry-doc/
│   └── telemetry.md # Exporter, Prometheus, dataset schema, known gaps
├── phase3-models-doc/
│   └── aether_engine.md  # LSTM models, graph, corroboration, ACP schema
├── phase4-llm-doc/        # (planned)
└── phase5-integration-doc/ # (planned)
```
