# Phase 3 — Predictive Engine & Corroboration Framework

**Goal:** Train ML models on labeled telemetry to detect, classify, and time faults before SLA breach. Corroborate ML predictions with a deterministic graph model before any autonomous action is taken.

**Status:** ✅ Done — all 3 LSTM models trained and verified (synthetic + real data). Graph clonal search verified. Full corroboration pipeline (Scenarios 1 and 2) demonstrated. ACP emit/load round-trip verified. Taxonomy single-source-of-truth enforced across all 5 consumer files.
3. **Dual-Model Corroboration Gate:** Enforce that a stochastic neural network can never directly touch production configs; it must be corroboratively verified by the deterministic graph model.
4. **Edge Policy Engine (EPE) & Autonomy:** Consult an operator-defined Autonomy Policy Matrix and compile an Anomaly Context Packet (ACP) containing CLI configuration fixes.

---

## File Structure

```
phase3-models/
├── predictive_engine.py                   # PyTorch LSTM, Attention, and TTF Regressor models
├── acp_manager.py                         # Anomaly Context Packet (ACP) JSON Schema & class
├── graph_model.py                         # NetworkX Graph-Analytical & Clonal State Search engine
└── aether_corroborator.py                 # Corroborator & Edge Policy Engine controller
```

---

## Technical Details

### 1. Clonal State-Space Search
Instead of using fixed failover routing tables, the [graph_model.py](file:///home/charan/air-gapped-mpls-copilot/phase3-models/graph_model.py) script generates virtual copies ("clones") of your topology:
*   `CLONE_BASELINE`: Standard routing.
*   `CLONE_REROUTED_OVERLAY`: Reroutes traffic by disabling/deprioritizing the degraded link, shifting traffic to the SD-WAN backup tunnel.
*   `CLONE_QOS_THROTTLED`: Reduces non-critical traffic flows (like database backups) by 75% to preserve priority video/VoIP lines.

Each clone is evaluated against the current live traffic matrix (e.g., VoIP streams vs DB backups). The engine scores each clone based on total path delay and link capacity breaches, choosing the one with the lowest score (minimum SLA violations) as the winner.

### 2. Dual-Model Safety Gate
The [aether_corroborator.py](file:///home/charan/air-gapped-mpls-copilot/phase3-models/aether_corroborator.py) script acts as the decision gateway:

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

*   **Agreement:** If the LSTM predicts an anomaly (e.g., *Congestion* on link PE1-CE1) and the NetworkX graph model projects link saturation on that link, they **agree**.
*   **Disagreement (Safety Trip):** If the LSTM predicts a failure signature but the graph model calculates that the physical interface can mathematically carry the traffic with zero saturation (e.g., due to temporary transient packet noise), they **disagree**. The EPE instantly overrides the system, tripping a safety flag that drops the action to `RECOMMEND_ONLY` to prevent erroneous routing flaps.

### 3. Operator-Configurable Autonomy Matrix
The Autonomy Policy allows operators to define what the system can solve autonomously vs. what it must recommend:

```python
AUTONOMY_POLICY = {
    "REROUTE_BRANCH": {"min_conf": 0.80, "auto_execute": True},     # Auto-routes around branch failures
    "QOS_SHAPE_QUEUE": {"min_conf": 0.75, "auto_execute": True},    # Auto-throttles queues
    "CORE_PATH_FAILOVER": {"min_conf": 0.90, "auto_execute": False} # Recommend only (never auto-execute)
}
```

If the action is authorized for auto-execution and the ML confidence score exceeds the target threshold, the EPE issues an `AUTO_EXECUTE` code inside the ACP.

### 4. Anomaly Context Packet (ACP)
The output of the engine is an ACP JSON log containing:
1.  **Severity Level** (escalated to `CRITICAL` or `HIGH` based on ML/Graph analysis).
2.  **Telemetry Snapshot** (active snapshot of bandwidth/losses).
3.  **Corroboration Metadata** (reasons for the decision, agreement flags).
4.  **Mitigation Commands** (specific shell commands to inject rate-limiting QoS queues or adjust BGP path attributes).
