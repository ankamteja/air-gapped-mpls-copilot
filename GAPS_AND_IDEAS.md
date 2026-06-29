# Project Aether — Gaps & Ideas (parking lot)

**Purpose:** a forward-looking list to review — current weaknesses + future ideas.
Nothing here is committed work. `GAP_ANALYSIS.md` is the *implementation-status audit*
(what the problem statement asks vs. what's built); **this file is the *backlog*.**

Last updated: 2026-06-29.

Legend — **Impact:** how much a judge / real operator would care · **Effort:** rough size.

---

## A. Gaps / weaknesses (honest)

### A1. Open-loop remediation — no verify, no rollback  ·  Impact: HIGH · Effort: M
The system predicts a fault and acts (reroute / QoS), but never checks whether the
action *worked*, and never undoes it when the fault clears. The ACP schema already
reserves an `execution_mode: "ROLLBACK"` (`acp_manager.py:98`) — it's anticipated but
unwired. This is the single biggest credibility gap: "autonomous NOC" implies a
closed loop (act → observe → confirm/rollback), and right now it's act-and-forget.

### A2. Recovery is timeline-driven, not remediation-driven  ·  Impact: HIGH · Effort: M
In synthetic mode the network "returns to normal" because the `fault_streamer` state
machine cycles `FAULT → RESOLVE → QUIET` on a clock — *not* because the remediation
fixed anything. In a real lab, `REROUTE_BRANCH` routes *around* a degraded link (SLA
recovers) but the link stays degraded and the reroute is never reverted. Tightly
coupled to A1.

### A3. Synthetic data throughout  ·  Impact: MED-HIGH · Effort: L
Models train and run on a generated CSV (`generate_dataset.py`). NetFlow records and
traffic flows are simulated. The dev verification never had a live Containerlab data
plane (the `--clab` path exists but wasn't the demo path). Defensible for a 30-hour
build, but be ready to say so plainly. The honest accounting is in `GAP_ANALYSIS.md`.

### A4. Early-onset classification vs. lead-time tradeoff  ·  Impact: MED · Effort: M
To report lead time, the streamer feeds onset/early-plateau windows. The earlier the
window (more lead time), the subtler the signal and the more the classifier slips
(e.g. latency ~6/10 in one sample). There's an inherent tension: big lead time ⇔
ambiguous metrics. Could be improved with a dedicated precursor-vs-fault head.

### A5. No real TSDB (InfluxDB/Telegraf)  ·  Impact: LOW-MED · Effort: M
Metrics live in-memory + flat JSONL; Time-Travel replays per-ACP snapshots, not
per-second telemetry. Prometheus exporter satisfies the problem statement's "options"
list, but per-second historical replay needs a real time-series store.

### A6. NLQ answers can drift generic  ·  Impact: MED · Effort: S-M
The copilot retrieves runbooks + incidents, but answers aren't always tightly bound to
the *current* ACP's numbers. Risk of plausible-but-unspecific responses — exactly what
the "no hallucination, grounded in local retrieval" criterion probes.

### A7. No dashboard auth / RBAC  ·  Impact: MED (security) · Effort: S-M
Anyone who can reach `:8080` can approve remediations that run real `docker exec` /
`vtysh` commands. For a *secure* MPLS ops tool this is a real hole. The action log
exists but isn't access-controlled or tamper-evident (not signed).

### A8. Overlay is GRE, not actually IPSec-encrypted  ·  Impact: LOW-MED · Effort: M
`overlay-setup.sh` builds a real GRE tunnel over the core, but the "IPSec" part is
documented, not configured (no strongSwan/xfrm ESP). Fine as a model; a real ESP wrap
would strengthen the security story.

### A9. Live topology state not in the RAG store  ·  Impact: LOW · Effort: S
Current utilisation / active faults are passed in the prompt as text, not indexed into
ChromaDB — so the LLM can't answer "has pe1-p1 been the bottleneck before?" across
history.

### A10. Inference is bursty; GPU "peak" is a workaround  ·  Impact: LOW · Effort: S
Inference runs in ~50ms bursts, so instant GPU reads show 0% and we track a rolling
peak. A continuous/batched inference loop would use the GPU more honestly and enable
higher sample rates.

---

## B. New ideas (brainstorm — not scoped)

### B1. Closed-loop remediation (verify + rollback)  ·  ★ top pick
After acting, schedule a telemetry re-check at t+30/60s. Fault gone → emit a `ROLLBACK`
ACP and revert the OSPF cost / QoS change, logged. Fault worse → escalate severity /
try the next clonal permutation. Directly fixes A1+A2 and turns "back to normal" into
something the *system does*. This is the highest-leverage next build.

### B2. Lead-time as a confidence interval, not a point
Show "breach in 40s ± 15s (p80)" instead of a single number. Quantile regression or MC
dropout on the TTF head. Operators trust ranges; point estimates invite "it was wrong."

### B3. Incident correlation (root cause vs. symptoms)
When several ACPs fire close together, cluster them into one incident and have the graph
model + LLM name the *root* (e.g. "pe1-p1 degradation") vs. downstream symptoms. Cuts
alert fatigue — an Objective-4 goal.

### B4. Path Blast / what-if simulator
A small form (Source / Dest / Traffic / Run) that feeds the *existing* clonal graph
engine and returns projected SLA impact + best permutation. The engine already exists;
this is mostly a form + endpoint. (Was in idea_v4.)

### B5. Operator feedback → online calibration
`feedback_cli` already records accept/reject. Use it to nudge per-action confidence
thresholds automatically (lower the bar for actions operators always approve, raise it
for ones they reject). A visible "the copilot learned from you" loop.

### B6. Model-drift detector
Watch the live inference feature distribution vs. the training distribution; alert when
they diverge (the network changed, retrain due). Cheap (KL / population stability index)
and a strong "production-aware" signal.

### B7. LLM-drafted runbooks for novel faults
When a fault appears with no close runbook match in ChromaDB, have Mistral draft a
runbook, mark it "AI-suggested, unverified," and index it. The KB grows itself, fully
offline.

### B8. Tamper-evident action log
Hash-chain + Ed25519-sign each `action_log.jsonl` entry (we already sign models and the
air-gap report). Gives a verifiable audit trail — pairs with A7 (auth) for the security
criterion.

### B9. Cost-aware action ranking
Rank candidate remediations by (SLA benefit ÷ blast radius). Makes the autonomy matrix
smarter than a fixed per-action toggle and gives the LLM a real basis for "why this
action."

### B10. Chaos campaign mode
Scheduled, randomized fault-injection campaigns that run the full predict→act→verify
loop unattended and produce a nightly signed report. Turns Phase 6 from 4 scripted
scenarios into continuous validation.

### B11. Per-VRF / per-tenant SLA views
Break the dashboard down per customer VRF (CUST today; imagine many). Each tenant sees
their own SLA, faults, and lead times — closer to a real managed-MPLS NOC.

### B12. Synthetic→real bridge
A mode that collects real Containerlab telemetry over a session and retrains on it, so
the same pipeline graduates from synthetic to real data without code changes. Directly
answers the "is this real?" question (A3).

---

## C. Quick reference — where things live
- Implementation status audit → `GAP_ANALYSIS.md`
- Phase 5 integration data flow → `phase5-integration/README.md`
- Fault lifecycle (the timeline state machine) → `phase3-models/fault_streamer.py` `stream_natural()`
- Remediation commands (real `docker exec`/`vtysh`) → `phase5-dashboard/app.py` `_REMEDIATION_STEPS`
- Clonal graph engine (backs any what-if) → `phase3-models/graph_model.py`
