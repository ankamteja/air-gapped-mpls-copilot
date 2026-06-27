# Phase 3 — Predictive Engine & Corroboration Framework

**Goal:** Train ML models on labeled telemetry to detect, classify, and time faults before SLA breach. Corroborate ML predictions with a deterministic graph model before any autonomous action is taken.

**Status:** ✅ Done — all 3 LSTM models trained and verified (synthetic + real data). Graph clonal search verified. Full corroboration pipeline (Scenarios 1 and 2) demonstrated. ACP emit/load round-trip verified. Taxonomy single-source-of-truth enforced across all consumer files.

---

## Design Goals

1. **Unsupervised Anomaly Detection:** LSTM Autoencoder learns healthy baseline; high reconstruction loss = anomaly.
2. **Supervised Fault Classification:** BiLSTM + Attention Classifier identifies the specific fault class (6 classes).
3. **Dual-Model Corroboration Gate:** A stochastic neural network never directly touches production configs; it must agree with the deterministic graph model first.
4. **Edge Policy Engine (EPE) & Autonomy:** Consult an operator-defined Autonomy Policy Matrix and compile an Anomaly Context Packet (ACP) containing CLI configuration fixes.

---

## File Structure

```
phase3-models/
├── taxonomy.py            # Single source of truth — 6 fault classes, action policy matrix
├── predictive_engine.py   # PyTorch: LSTMAutoencoder, LSTMAttentionClassifier, TimeToFailureRegressor
├── graph_model.py         # NetworkX Graph-Analytical Engine & Clonal State-Space Search
├── acp_manager.py         # Anomaly Context Packet (ACP) JSON schema & class
├── aether_corroborator.py # Dual-model corroboration & Edge Policy Engine
├── data_collector.py      # Telemetry → CSV bridge (polls exporter, labels with fault state)
├── train_models.py        # End-to-end training pipeline (autoencoder + classifier + regressor)
└── inference_engine.py    # Live inference with sliding window + ACP emit
```

---

## Technical Details

### 1. Clonal State-Space Search

Instead of fixed failover tables, [`graph_model.py`](../../phase3-models/graph_model.py) generates virtual copies ("clones") of the topology:

- `CLONE_BASELINE`: Standard routing, unmodified.
- `CLONE_REROUTED_OVERLAY`: Deprioritises the degraded link (cost → 999999), shifting traffic onto the SD-WAN backup tunnel (pe1↔pe2 direct, 40ms, 2Mbps).
- `CLONE_QOS_THROTTLED`: Primary path stays active, but non-critical DB bulk traffic is throttled from 4.5Mbps to 1.15Mbps, preserving VoIP headroom.

Each clone is evaluated against the live traffic matrix (VoIP + DB + SSH). The engine scores each clone on total path delay + capacity saturation penalties, selecting the minimum-score clone as the recommended action.

### 2. Dual-Model Safety Gate

[`aether_corroborator.py`](../../phase3-models/aether_corroborator.py) is the decision gateway:

```
+------------+       +-------------+
| LSTM (ML)  |       |   NetworkX  |
| Prediction |       | Projection  |
+------------+       +-------------+
      |                     |
      +----------+----------+
                 |
                 v
      +---------------------+
      |  Corroboration Loop |
      +---------------------+
                 |
        Is there agreement?
       /                 \
     Yes                  No
     /                      \
+------------------+    +-------------------------+
| Consult Autonomy |    | SAFETY TRIP:            |
|  Policy Matrix   |    | Downgrade execution     |
+------------------+    | to RECOMMEND_ONLY       |
                        +-------------------------+
```

- **Agreement:** LSTM predicts congestion on pe1→p1 AND NetworkX graph projects link saturation → both agree → check autonomy policy.
- **Disagreement (Safety Trip):** LSTM predicts fault but graph shows the physical interface can carry the traffic with zero saturation (transient noise) → EPE trips to `RECOMMEND_ONLY`. No autonomous action.

### 3. Operator-Configurable Autonomy Matrix

Defined in [`taxonomy.py`](../../phase3-models/taxonomy.py) as `ACTION_POLICY`. Operators control per-action-class confidence thresholds and auto-execute permission:

```python
ACTION_POLICY = {
    "REROUTE_BRANCH":    {"min_conf": 0.80, "auto_execute": True},   # Auto-reroutes around branch failures
    "QOS_SHAPE_QUEUE":   {"min_conf": 0.75, "auto_execute": True},   # Auto-throttles non-critical queues
    "CORE_PATH_FAILOVER":{"min_conf": 0.90, "auto_execute": False},  # Recommend only — never auto on core
    "NODE_ISOLATION":    {"min_conf": 0.99, "auto_execute": False},  # Operator must confirm
}
```

Safety floors enforced in code regardless of this table: model disagreement always downgrades to `RECOMMEND_ONLY`; every auto-executed action is logged and reversible.

### 4. Anomaly Context Packet (ACP)

The structured handoff between models, graph engine, and the EPE. An ACP JSON contains:

1. **Severity Level** — escalated to `CRITICAL` for control-plane flaps or TTF < 30s.
2. **Telemetry Snapshot** — per-node interface counters at time of detection.
3. **ML Analysis** — anomaly flag, reconstruction loss, predicted fault class + confidence, estimated TTF.
4. **Graph Analysis** — saturated links list, impacted paths.
5. **Corroboration** — agreement flag, rationale string, recommended action, execution mode.
6. **Mitigation Commands** — exact `docker exec vtysh` / `tc` commands ready to paste or auto-execute.

### 5. Fault Taxonomy

All class IDs, labels, display names, and action mappings live in a single file — [`taxonomy.py`](../../phase3-models/taxonomy.py). Every other file imports from it; no file redefines these mappings.

| ID | Label | Display Name | Action Class |
|---|---|---|---|
| 0 | `Healthy` | Healthy | NO_ACTION |
| 1 | `latency` | Latency Drift | REROUTE_BRANCH |
| 2 | `loss` | Packet Loss | REROUTE_BRANCH |
| 3 | `corrupt` | Frame Corruption | REROUTE_BRANCH |
| 4 | `rate` | Congestion / Saturation | QOS_SHAPE_QUEUE |
| 5 | `flap` | Control-Plane Flap | CORE_PATH_FAILOVER |

---

## Running

```bash
# Train on synthetic data (no live lab needed)
python3 phase3-models/train_models.py --epochs 50

# Train on real collected data
python3 phase3-models/train_models.py --data phase3-models/dataset.csv --epochs 50

# Run corroboration demo (no trained models needed)
python3 phase3-models/aether_corroborator.py

# Live inference demo with synthetic sliding window
python3 phase3-models/inference_engine.py --demo
```
