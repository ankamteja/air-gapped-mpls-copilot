# Phase 5 — NOC Dashboard (FastAPI)

**Goal:** Provide a single-page NOC operator interface that brings together live alert streaming, topology visualization, ACP history, NLQ chatbox, operator feedback, and compliance reporting — all served locally with zero cloud dependency.

**Status:** ✅ Complete — FastAPI server with inline dark-theme HTML dashboard, WebSocket live alert stream, 8 REST endpoints, NLQ chatbox wired to Phase 4 LLM copilot.

---

## File Structure

```
phase5-dashboard/
└── app.py       # FastAPI server: inline HTML, all endpoints, WebSocket manager
```

The dashboard HTML, CSS, and JavaScript are all embedded as a Python string in `app.py` (no separate templates directory). This keeps the deployment a single file with no static-file serving requirements.

---

## Starting the Dashboard

```bash
python3 phase5-dashboard/app.py
```

Dashboard is available at: **http://localhost:8080**

The server auto-initializes the `AetherCopilot` on startup (seeds IKB runbooks and ingests incident logs if not already done).

---

## Dashboard Layout

A 2×2 grid panel layout on a dark terminal-style background:

```
┌─────────────────────────────────────────────────────────────┐
│  PROJECT AETHER — NOC COPILOT        [LIVE] [MODELS OK]     │
├──────────────────────────┬──────────────────────────────────┤
│  LIVE ALERTS             │  NETWORK TOPOLOGY                 │
│  ────────────────────    │  ──────────────────────────────   │
│  🔴 CRITICAL             │  SVG force-directed graph         │
│  BiLSTM: Control-Plane   │  Nodes colored by role:           │
│  Flap 91% | TTF: 28s     │    PE: blue  P: orange           │
│  RECOMMEND_ONLY          │    CE: green  backup: dashed      │
│  ────────────────────    │                                   │
│  🟡 HIGH                 │                                   │
│  ...                     │                                   │
├──────────────────────────┼──────────────────────────────────┤
│  ACP HISTORY             │  COPILOT / NLQ                   │
│  ────────────────────    │  ──────────────────────────────   │
│  Last 50 ACPs from       │  [Ask a question about the net.] │
│  ikb/incidents.jsonl     │  > How do I fix BGP flap on pe1? │
│  Click to expand JSON    │                                   │
│                          │  Aether: The BGP neighbor on pe1  │
│                          │  dropped because...               │
└──────────────────────────┴──────────────────────────────────┘
```

**Severity color coding:**

| Severity | Border color | Background |
|---|---|---|
| CRITICAL | `#f85149` (red) | `#1e1014` |
| HIGH | `#e3b341` (amber) | `#1e1a10` |
| MEDIUM | `#58a6ff` (blue) | `#10161e` |
| LOW | `#3fb950` (green) | `#101e12` |

---

## API Reference

### `GET /`
Returns the full dashboard HTML page. No authentication.

---

### `GET /api/status`

System health check. Called by the dashboard header badge on load.

**Response:**
```json
{
  "models_loaded": true,
  "llm_online": true,
  "ikb_docs": 47,
  "acp_count": 120,
  "air_gap_compliant": true
}
```

| Field | What it checks |
|---|---|
| `models_loaded` | `saved/autoencoder.pt`, `classifier.pt`, `regressor.pt` all exist |
| `llm_online` | Ollama is reachable on port 11434 (3s probe timeout) |
| `ikb_docs` | ChromaDB `runbooks` collection document count |
| `acp_count` | Line count of `ikb/incidents.jsonl` |
| `air_gap_compliant` | DNS probe to `8.8.8.8:53` is **not** reachable (expected in air-gap) |

---

### `GET /api/topology`

Returns the NetworkX graph as JSON for the SVG renderer.

**Response:**
```json
{
  "nodes": [
    {"id": "pe1", "role": "pe"},
    {"id": "p1",  "role": "p"},
    ...
  ],
  "edges": [
    {"source": "pe1", "target": "p1", "capacity": 10000000, "delay": 5, "cost": 10, "is_backup": false},
    ...
  ]
}
```

---

### `GET /api/acps?limit=50`

Returns the last N ACP entries from `ikb/incidents.jsonl`.

**Response:**
```json
{
  "acps": [ { ...acp json... }, ... ],
  "total": 120
}
```

---

### `POST /api/nlq`

Routes an operator question to the LLM copilot.

**Request:**
```json
{ "question": "Why is pe1 showing high reconstruction loss?" }
```

**Response:**
```json
{
  "answer": "The high reconstruction loss on pe1 indicates the autoencoder...",
  "source": "ollama"
}
```

`source` is `"ollama"` when Mistral answered, `"structured_fallback"` when Ollama was offline, or `"error"` if the copilot failed to initialize.

---

### `POST /api/feedback`

Records operator accept/reject decision for an ACP. Updates `ikb/incidents.jsonl` and re-ingests into ChromaDB.

**Request:**
```json
{ "acp_id": "0588d40f-...", "feedback": "accepted" }
```

**Response:**
```json
{ "status": "ok", "acp_id": "0588d40f-...", "feedback": "accepted" }
```

Returns HTTP 404 if the ACP ID is not found in the log.

---

### `GET /api/compliance`

Runs the air-gap compliance checker (`airgap_compliance.py`) and returns a signed JSON report.

**Response (excerpt):**
```json
{
  "status": "COMPLIANT",
  "checks": {
    "dns_probe":    {"reachable": false, "expected": false, "pass": true},
    "http_probe":   {"reachable": false, "expected": false, "pass": true},
    "model_sigs":   {"verified": true,   "pass": true}
  },
  "signature": "2b96278b..."
}
```

---

### `GET /api/benchmark`

Runs the lead-time benchmark against `phase3-models/dataset.csv` (blocking, may take 30–60 seconds).

**Response:**
```json
{
  "results": {
    "detected": 9,
    "total": 9,
    "avg_lead_time_sec": 343,
    "per_scenario": [ ... ]
  }
}
```

---

### `WebSocket /ws/alerts`

Live alert stream. Connect once; the server pushes new ACP JSON objects as they are written.

**Message format:** same as ACP JSON schema (see `docs/phase3-models-doc/aether_engine.md`, section 4).

**JavaScript example:**
```js
const ws = new WebSocket("ws://localhost:8080/ws/alerts");
ws.onmessage = (e) => {
  const acp = JSON.parse(e.data);
  renderAlert(acp);
};
```

The server calls `broadcast_acp(acp_dict)` from the inference engine side whenever a new ACP is emitted. Disconnected clients are cleaned up automatically.

---

## Dependencies

| Package | Version | Purpose |
|---|---|---|
| `fastapi` | ≥0.104.0 | ASGI web framework |
| `uvicorn[standard]` | ≥0.24.0 | ASGI server (includes WebSocket support) |
| `websockets` | ≥12.0 | WebSocket library used by uvicorn |
| `pydantic` | ≥2.0.0 | Request/response model validation |

All Phase 3 and Phase 4 packages are also required (see `requirements.txt`).

---

## Integration

```
phase3-models/graph_model.py        → GET /api/topology (ClonalGraphEngine base_graph)
phase3-models/acp_manager.py        → GET /api/acps, WS /ws/alerts
phase3-models/feedback_cli.py       → POST /api/feedback (_apply_feedback)
phase3-models/airgap_compliance.py  → GET /api/compliance (run_compliance_check)
phase3-models/benchmark_harness.py  → GET /api/benchmark (run_benchmark)
phase4-llm/llm_copilot.py          → POST /api/nlq (AetherCopilot.query)
```

The dashboard imports all Phase 3 and Phase 4 modules via `sys.path` injection at the top of `app.py`. No packaging or installation of the project as a module is required.

---

## Production Notes

- The inline HTML is intentional — it keeps deployment to a single file, which is appropriate for an air-gapped edge appliance where no npm/CDN access is available.
- The WebSocket alert stream requires the inference engine (`inference_engine.py`) to call `broadcast_acp()` by importing the `_connected_ws` list from `app.py`. In the current implementation these are run in the same process or wired via a shared queue.
- The `/api/benchmark` endpoint is blocking and runs synchronously in a thread pool executor to avoid blocking the event loop during the ~60-second benchmark run.
