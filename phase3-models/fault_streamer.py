#!/usr/bin/env python3
"""
fault_streamer.py — Continuous live fault injection for Project Aether dashboard.

Samples real rows from dataset_large.csv (per fault class), runs them through
the full AetherInferenceEngine, and writes ACPs every few seconds so the
dashboard WebSocket feed stays alive with diverse, realistic alerts.

Usage:
    python3 fault_streamer.py               # streams forever, 4s interval
    python3 fault_streamer.py --interval 2  # faster
    python3 fault_streamer.py --once        # one ACP per fault class then exit
"""
import os
import sys
import time
import random
import argparse
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))

import pandas as pd
from inference_engine import AetherInferenceEngine

DATASET = os.path.join(os.path.dirname(__file__), "dataset_large.csv")
FAULT_CLASSES = ["Healthy", "flap", "loss", "corrupt", "rate", "latency"]

# Rotation order — cycle through faults so dashboard shows variety
CYCLE = ["flap", "loss", "rate", "latency", "corrupt", "Healthy",
         "flap", "loss", "rate", "latency", "corrupt", "latency",
         "rate", "flap", "Healthy", "corrupt", "loss", "rate"]

LOCATION_MAP = {
    "flap":    ["pe1", "pe2"],
    "loss":    ["pe1", "p1"],
    "corrupt": ["p1", "pe2"],
    "rate":    ["pe1", "p1", "pe2"],
    "latency": ["pe1", "pe2", "p1"],
    "Healthy": [None],
}

DISPLAY_MAP = {
    "flap":    "BGP_FLAP",
    "loss":    "PACKET_LOSS",
    "corrupt": "LINK_CORRUPTION",
    "rate":    "CONGESTION",
    "latency": "HIGH_LATENCY",
    "Healthy": "Healthy",
}


def load_dataset_by_class(path: str) -> dict[str, pd.DataFrame]:
    """Load dataset rows grouped by fault_label."""
    print(f"[*] Loading dataset: {path}")
    df = pd.read_csv(path)
    # Drop label columns, keep features only
    feature_cols = [c for c in df.columns if c not in ("fault_label", "fault_location", "timestamp")]
    groups = {}
    for label in FAULT_CLASSES:
        rows = df[df["fault_label"] == label][feature_cols]
        if len(rows):
            groups[label] = rows.reset_index(drop=True)
            print(f"  {label:10s}: {len(rows):5d} rows")
    return groups, feature_cols


def sample_window(groups: dict, feature_cols: list, label: str, seq_len: int = 20) -> list[dict]:
    """Sample seq_len consecutive rows for a given fault class."""
    df = groups.get(label, groups["Healthy"])
    start = random.randint(0, max(0, len(df) - seq_len - 1))
    window = df.iloc[start:start + seq_len]
    samples = []
    for _, row in window.iterrows():
        samples.append({col: float(row[col]) for col in feature_cols})
    return samples


def stream(interval: float = 4.0, once: bool = False):
    engine = AetherInferenceEngine()
    engine.load_models()

    if not os.path.exists(DATASET):
        print(f"[!] Dataset not found: {DATASET}")
        print("    Run: python3 generate_dataset.py")
        sys.exit(1)

    groups, feature_cols = load_dataset_by_class(DATASET)
    # Keep only columns the model knows about
    if engine.columns:
        feature_cols = [c for c in engine.columns if c in feature_cols]

    cycle_idx = 0
    acp_count = 0

    print(f"\n[*] Fault streamer running — interval={interval}s  (Ctrl+C to stop)\n")

    while True:
        label = CYCLE[cycle_idx % len(CYCLE)]
        cycle_idx += 1

        # Feed window into engine
        engine.sliding_window.clear()
        window = sample_window(groups, feature_cols, label, seq_len=engine.seq_len)
        for sample in window:
            engine.ingest_sample(sample)

        acp = engine.run_inference()
        if acp:
            engine.log_acp(acp)
            acp_count += 1
            ml  = acp.ml_analysis
            cor = acp.corroboration
            print(
                f"  [{acp.severity:8s}] {ml['predicted_fault_class']:20s} "
                f"conf={ml['confidence_score']:.0%}  "
                f"ttf={ml['estimated_time_to_failure_sec']:.0f}s  "
                f"mode={cor['execution_mode']}  "
                f"#{acp_count}"
            )

        if once and cycle_idx >= len(FAULT_CLASSES):
            print(f"\n[+] Done — {acp_count} ACPs written to acp_logs/")
            break

        time.sleep(interval)


def main():
    p = argparse.ArgumentParser(description="Aether continuous fault streamer")
    p.add_argument("--interval", type=float, default=4.0, help="Seconds between ACPs")
    p.add_argument("--once", action="store_true", help="One pass through fault classes then exit")
    args = p.parse_args()
    stream(interval=args.interval, once=args.once)


if __name__ == "__main__":
    main()
