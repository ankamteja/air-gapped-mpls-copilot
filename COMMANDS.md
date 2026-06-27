# Project Aether — Ready-to-Execute Commands

All commands run from the repo root: `/home/charan/air-gapped-mpls-copilot/`

---

## 0. Install Dependencies

```bash
# PyTorch with CUDA 12.8 (RTX 4060) — do this FIRST
pip install torch --index-url https://download.pytorch.org/whl/cu128

# All other dependencies
pip install -r requirements.txt
```

---

## 1. Phase 1 — Deploy MPLS Lab (needs Docker + Containerlab)

```bash
# Deploy 7-node topology
cd phase1-simulation/topology
sudo clab deploy -t chunk3.clab.yml

# Configure MPLS — OSPF, LDP, BGP VPNv4, VRFs
sudo bash chunk3-setup.sh

# Start traffic (VoIP + DB + HTTP + SSH) — runs in background
bash traffic_generator.sh &

# Inject faults continuously (30+ variants) — runs in background
bash continuous_fault_loop.sh &

# Return to repo root
cd ../..
```

---

## 2. Phase 2 — Telemetry

```bash
# Start Prometheus exporter (binds port 8000)
python3 phase2-telemetry/exporter.py &

# Start Prometheus + Grafana via Docker Compose
cd phase2-telemetry && docker compose up -d && cd ..
# Prometheus: http://localhost:9090
# Grafana:    http://localhost:3000  (admin / admin)

# Collect labeled dataset from live lab (4 hours = 14400s)
python3 phase3-models/data_collector.py \
    --duration 14400 \
    --output phase3-models/dataset_large.csv
```

---

## 3. Phase 3 — Dataset & Training

```bash
cd phase3-models

# Option A: Generate synthetic dataset (no lab needed — 100k rows, 78MB)
python3 generate_dataset.py
# Output: dataset_large.csv

# Option B: Generate custom size
python3 generate_dataset.py --rows 50000 --out small.csv

# Train all 3 models (Autoencoder + Classifier + TTF Regressor)
# CUDA auto-detected, falls back to CPU
python3 train_models.py \
    --data dataset_large.csv \
    --epochs 35 \
    --seq-len 20 \
    --batch-size 64

# Sign models with Ed25519
python3 model_integrity.py --sign

# Verify signatures
python3 model_integrity.py --verify

cd ..
```

---

## 4. Phase 3 — Run Inference & Corroboration

```bash
cd phase3-models

# Live inference demo (synthetic sliding window, generates ACPs)
python3 inference_engine.py --demo

# Dual-model corroboration demo (Scenario 1: AUTO_EXECUTE + Scenario 2: SAFETY TRIP)
python3 aether_corroborator.py

# Lead-time benchmark (100k dataset — 741/741 detected)
python3 benchmark_harness.py --data dataset_large.csv --seq-len 20

# Fault taxonomy
python3 taxonomy.py

# Air-gap compliance check
python3 airgap_compliance.py

# Operator feedback stats
python3 feedback_cli.py --stats

cd ..
```

---

## 5. Phase 4 — LLM Copilot Setup (one-time, needs internet)

```bash
# Full automated setup: Ollama + Mistral 7B + deps + IKB seed
bash phase4-llm/setup_llm.sh

# After setup — everything below runs 100% offline

# Seed ChromaDB runbooks (idempotent)
python3 phase4-llm/ikb_manager.py --seed

# Sync ACP incident log → ChromaDB
python3 phase4-llm/ikb_manager.py --ingest-acps

# Search the knowledge base
python3 phase4-llm/ikb_manager.py --query "BGP flap pe1 recovery"

# Interactive NLQ terminal
python3 phase4-llm/nlq_interface.py

# Single-shot NLQ query
python3 phase4-llm/nlq_interface.py --once "How do I fix packet loss on pe1?"
```

---

## 6. Phase 5 — NOC Dashboard

```bash
# Start the dashboard
python3 phase5-dashboard/app.py
# Dashboard: http://localhost:8080

# Check system status
curl -s http://localhost:8080/api/status | python3 -m json.tool

# Get topology graph
curl -s http://localhost:8080/api/topology | python3 -m json.tool

# Get last 10 ACPs
curl -s "http://localhost:8080/api/acps?limit=10" | python3 -m json.tool

# Natural language query
curl -s -X POST http://localhost:8080/api/nlq \
  -H "Content-Type: application/json" \
  -d '{"question": "How do I fix BGP flap on pe1?"}' | python3 -m json.tool

# Air-gap compliance report
curl -s http://localhost:8080/api/compliance | python3 -m json.tool

# Submit operator feedback for an ACP
curl -s -X POST http://localhost:8080/api/feedback \
  -H "Content-Type: application/json" \
  -d '{"acp_id": "<acp-id-here>", "feedback": "accepted"}' | python3 -m json.tool
```

---

## 7. Full System (All Phases Together)

Open 3 terminals:

**Terminal 1 — Dashboard**
```bash
cd /home/charan/air-gapped-mpls-copilot
python3 phase5-dashboard/app.py
```

**Terminal 2 — Inference engine (pushes live ACPs to dashboard)**
```bash
cd /home/charan/air-gapped-mpls-copilot/phase3-models
python3 inference_engine.py --demo
```

**Terminal 3 — NLQ copilot**
```bash
cd /home/charan/air-gapped-mpls-copilot
python3 phase4-llm/nlq_interface.py
```

Then open **http://localhost:8080** in a browser.

---

## 8. Ollama / Mistral — Manual Control

```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh

# Start Ollama server
ollama serve &

# Pull Mistral 7B (4-bit quantized, ~4.1 GB)
ollama pull mistral:7b-instruct-q4_K_M

# List downloaded models
ollama list

# Test Mistral directly
ollama run mistral:7b-instruct-q4_K_M "What is MPLS?"

# Stop Ollama
pkill ollama
```

---

## 9. Git Status & Commit

```bash
# Check what's changed
git status && git diff --stat

# Stage everything except secrets
git add README.md requirements.txt docs/ \
    phase3-models/acp_manager.py \
    phase3-models/aether_corroborator.py \
    phase3-models/generate_dataset.py \
    phase3-models/train_models.py \
    phase3-models/inference_engine.py \
    phase3-models/keys/aether_model_key.pub.pem \
    phase4-llm/ikb_manager.py phase4-llm/llm_copilot.py \
    phase4-llm/nlq_interface.py phase4-llm/setup_llm.sh \
    phase4-llm/runbooks/ \
    phase5-dashboard/app.py \
    phase1-simulation/topology/continuous_fault_loop.sh

# DO NOT commit:
#   phase3-models/keys/aether_model_key.pem  ← private key
#   phase3-models/dataset_large.csv          ← 78MB binary
#   phase3-models/saved/*.pt                 ← model weights
#   phase4-llm/chroma_db/                    ← vector DB
```

---

## 10. Useful One-Liners

```bash
# Watch ACPs arriving in real time
tail -f phase3-models/ikb/incidents.jsonl | python3 -m json.tool

# Count ACPs by severity
grep -o '"severity": "[^"]*"' phase3-models/ikb/incidents.jsonl | sort | uniq -c

# Check GPU usage during training
watch -n1 nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv

# Kill the dashboard
pkill -f "phase5-dashboard/app.py"

# Kill all training jobs
pkill -f "train_models.py"

# Verify all model files exist and are signed
python3 phase3-models/model_integrity.py --verify
```
