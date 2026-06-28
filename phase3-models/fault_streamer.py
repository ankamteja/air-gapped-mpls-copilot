#!/usr/bin/env python3
"""
fault_streamer.py — Natural fault injection for Project Aether dashboard.

State machine: QUIET (35-80s heartbeats) → FAULT burst (2-5 events, 10-20s each)
→ RESOLVE (1-2 Healthy events) → QUIET.  Matches real network fault patterns.

Usage:
    python3 fault_streamer.py               # natural mode (default)
    python3 fault_streamer.py --mode cycle  # old fixed-rotation mode
    python3 fault_streamer.py --once        # one pass through all classes then exit
    python3 fault_streamer.py --inject flap # inject single fault class then exit
"""
import os
import sys
import time
import random
import argparse

sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
from inference_engine import AetherInferenceEngine

DATASET = os.path.join(os.path.dirname(__file__), "dataset_large.csv")
FAULT_CLASSES = ["Healthy", "flap", "loss", "corrupt", "rate", "latency"]

# Natural fault probability distribution (BGP flap most common in MPLS)
FAULT_TYPES = ["flap", "loss", "rate", "latency", "corrupt"]
FAULT_PROBS = [0.30,   0.25,   0.20,   0.15,    0.10]

# Legacy fixed-cycle for --mode cycle
CYCLE = ["flap", "loss", "rate", "latency", "corrupt", "Healthy",
         "flap", "loss", "rate", "latency", "corrupt", "latency",
         "rate", "flap", "Healthy", "corrupt", "loss", "rate"]


def load_dataset_by_class(path: str):
    print(f"[*] Loading dataset: {path}")
    df = pd.read_csv(path)
    feature_cols = [c for c in df.columns
                    if c not in ("fault_label", "fault_location", "timestamp")]
    groups = {}
    for label in FAULT_CLASSES:
        rows = df[df["fault_label"] == label][feature_cols]
        if len(rows):
            groups[label] = rows.reset_index(drop=True)
            print(f"  {label:10s}: {len(rows):5d} rows")
    return groups, feature_cols


def sample_window(groups, feature_cols, label, seq_len=20):
    df = groups.get(label, groups["Healthy"])
    start = random.randint(0, max(0, len(df) - seq_len - 1))
    window = df.iloc[start:start + seq_len]
    return [{col: float(row[col]) for col in feature_cols} for _, row in window.iterrows()]


def _emit(engine, groups, feature_cols, label, acp_count):
    """Run one inference cycle for the given fault label. Returns updated count."""
    engine.sliding_window.clear()
    for sample in sample_window(groups, feature_cols, label, seq_len=engine.seq_len):
        engine.ingest_sample(sample)
    acp = engine.run_inference()
    if acp:
        engine.log_acp(acp)
        acp_count += 1
        ml  = acp.ml_analysis
        cor = acp.corroboration
        print(
            f"  [{acp.severity:8s}] {ml['predicted_fault_class']:24s} "
            f"conf={ml['confidence_score']:.0%}  "
            f"ttf={ml['estimated_time_to_failure_sec']:.0f}s  "
            f"mode={cor['execution_mode']}  "
            f"#{acp_count}"
        )
    return acp_count


def stream_natural(groups, feature_cols, engine):
    """
    State-machine stream that mimics real network fault patterns:
      QUIET   — Healthy heartbeat every 35-80s; after 2-4 events → FAULT
      FAULT   — same fault class, 2-5 events at 10-20s each → RESOLVE
      RESOLVE — 1-2 Healthy events at 8-15s each → QUIET
    """
    STATE_QUIET   = "quiet"
    STATE_FAULT   = "fault"
    STATE_RESOLVE = "resolve"

    state         = STATE_QUIET
    events_left   = random.randint(2, 4)
    current_fault = None
    acp_count     = 0

    print("\n[*] Natural fault streamer — state machine mode")
    print("    quiet: 35-80s/event  |  fault burst: 10-20s/event  |  resolve: 8-15s/event")
    print("    Ctrl+C to stop\n")
    print(f"  ── QUIET (first fault in ~{int(events_left * 57)}s) ──")

    while True:
        if state == STATE_QUIET:
            label    = "Healthy"
            interval = random.uniform(35, 80)
            events_left -= 1
            if events_left <= 0:
                current_fault = random.choices(FAULT_TYPES, weights=FAULT_PROBS)[0]
                events_left   = random.randint(2, 5)
                state = STATE_FAULT
                print(f"\n  ── FAULT BURST ─ {current_fault} ({events_left} events) ──")

        elif state == STATE_FAULT:
            label    = current_fault
            interval = random.uniform(10, 20)
            events_left -= 1
            if events_left <= 0:
                events_left = random.randint(1, 2)
                state = STATE_RESOLVE
                print(f"\n  ── RESOLVING ({events_left} event{'s' if events_left > 1 else ''}) ──")

        else:  # STATE_RESOLVE
            label    = "Healthy"
            interval = random.uniform(8, 15)
            events_left -= 1
            if events_left <= 0:
                events_left = random.randint(2, 4)
                state = STATE_QUIET
                print(f"\n  ── QUIET (next fault in ~{int(events_left * 57)}s) ──")

        acp_count = _emit(engine, groups, feature_cols, label, acp_count)
        time.sleep(interval)


def stream_cycle(groups, feature_cols, engine, interval=4.0, once=False):
    """Fixed-rotation mode — same behavior as original streamer."""
    print(f"\n[*] Cycle streamer — interval={interval}s  (Ctrl+C to stop)\n")
    cycle_idx = 0
    acp_count = 0
    while True:
        label = CYCLE[cycle_idx % len(CYCLE)]
        cycle_idx += 1
        acp_count = _emit(engine, groups, feature_cols, label, acp_count)
        if once and cycle_idx >= len(FAULT_CLASSES):
            print(f"\n[+] Done — {acp_count} ACPs written to acp_logs/")
            break
        time.sleep(interval)


def inject_single(label, groups, feature_cols, engine):
    """Inject exactly one ACP for the given fault class, then exit."""
    print(f"[*] Injecting single fault: {label}")
    _emit(engine, groups, feature_cols, label, 0)
    print("[+] Done — 1 ACP written to acp_logs/")


def main():
    p = argparse.ArgumentParser(description="Aether fault streamer")
    p.add_argument("--mode",     choices=["natural", "cycle"], default="natural",
                   help="natural=state machine (default) | cycle=fixed rotation")
    p.add_argument("--interval", type=float, default=4.0,
                   help="Seconds between events in cycle mode (ignored in natural mode)")
    p.add_argument("--once",     action="store_true",
                   help="Cycle mode only: one pass through fault classes then exit")
    p.add_argument("--inject",   choices=FAULT_CLASSES,
                   help="Inject a single fault class and exit immediately")
    args = p.parse_args()

    engine = AetherInferenceEngine()
    engine.load_models()

    if not os.path.exists(DATASET):
        print(f"[!] Dataset not found: {DATASET}")
        print("    Run: python3 phase3-models/generate_dataset.py")
        sys.exit(1)

    groups, feature_cols = load_dataset_by_class(DATASET)
    if engine.columns:
        feature_cols = [c for c in engine.columns if c in feature_cols]

    if args.inject:
        inject_single(args.inject, groups, feature_cols, engine)
    elif args.mode == "cycle":
        stream_cycle(groups, feature_cols, engine, args.interval, args.once)
    else:
        stream_natural(groups, feature_cols, engine)


if __name__ == "__main__":
    main()
