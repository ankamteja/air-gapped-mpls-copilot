#!/usr/bin/env python3
# =============================================================================
# train_models.py — End-to-End Training Pipeline for Aether ML Models
#
# Trains all three models (Autoencoder, Classifier, TTF Regressor) on
# collected telemetry data from data_collector.py.
#
# Usage:
#   python3 train_models.py --data dataset.csv --epochs 50 --seq-len 30
#
# Outputs:
#   phase3-models/saved/autoencoder.pt
#   phase3-models/saved/classifier.pt
#   phase3-models/saved/regressor.pt
# =============================================================================
import os
import sys
import csv
import argparse
import numpy as np

# Check torch availability early
try:
    import torch
    import torch.nn as nn
    from torch.utils.data import DataLoader, TensorDataset
except ImportError:
    print("[-] PyTorch not installed. Run: pip install torch")
    sys.exit(1)

from predictive_engine import (
    LSTMAutoencoder,
    LSTMAttentionClassifier,
    TimeToFailureRegressor,
    create_sequences
)
from taxonomy import LABEL_TO_ID as FAULT_CLASSES, NUM_CLASSES

# Device selection: CUDA (RTX 4060) > CPU
DEVICE = torch.device("cuda" if torch.cuda.is_available() else "cpu")

SAVE_DIR = os.path.join(os.path.dirname(__file__), "saved")


def load_dataset(csv_path):
    """
    Loads the CSV dataset produced by data_collector.py.
    Returns: feature_matrix (np.array), labels (np.array), column_names (list)
    """
    print(f"[*] Loading dataset from {csv_path}...")
    rows = []
    labels = []
    columns = None

    with open(csv_path, 'r') as f:
        reader = csv.reader(f)
        header = next(reader)
        # Find feature columns (skip timestamp, fault_label, fault_location)
        feat_start = 3
        columns = header[feat_start:]

        for row in reader:
            if len(row) < feat_start + 1:
                continue

            fault_label = row[1]
            label_int = FAULT_CLASSES.get(fault_label, 0)
            labels.append(label_int)

            features = []
            for val in row[feat_start:]:
                try:
                    features.append(float(val))
                except (ValueError, IndexError):
                    features.append(0.0)

            rows.append(features)

    X = np.array(rows, dtype=np.float32)
    y = np.array(labels, dtype=np.int64)
    print(f"    Loaded {X.shape[0]} samples × {X.shape[1]} features")
    print(f"    Class distribution: { {k: int((y == v).sum()) for k, v in FAULT_CLASSES.items()} }")
    return X, y, columns


def normalize(X):
    """Min-max normalization to [0, 1]."""
    mins = X.min(axis=0)
    maxs = X.max(axis=0)
    ranges = maxs - mins
    ranges[ranges == 0] = 1.0  # Avoid division by zero
    return (X - mins) / ranges, mins, maxs


def train_autoencoder(X_healthy, seq_len, epochs, batch_size, lr):
    """
    Trains the LSTM Autoencoder on HEALTHY data only.
    Anomaly = high reconstruction loss on unseen data.
    """
    print(f"\n{'='*60}")
    print(f"[*] Training LSTM Autoencoder (unsupervised, healthy data only)")
    print(f"    Samples: {len(X_healthy)}, SeqLen: {seq_len}, Epochs: {epochs}")

    num_features = X_healthy.shape[1]

    # Create sequences from healthy-only data (label doesn't matter for autoencoder)
    dummy_labels = np.zeros(len(X_healthy))
    X_seq, _ = create_sequences(X_healthy, dummy_labels, seq_len)

    tensor_X = torch.FloatTensor(X_seq).to(DEVICE)
    dataset = TensorDataset(tensor_X, tensor_X)  # Input = Target for autoencoder
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=True)

    model = LSTMAutoencoder(seq_len, num_features, hidden_dim=64).to(DEVICE)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    criterion = nn.MSELoss()

    model.train()
    for epoch in range(epochs):
        total_loss = 0.0
        for batch_x, batch_target in loader:
            optimizer.zero_grad()
            reconstructed = model(batch_x)
            loss = criterion(reconstructed, batch_target)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        avg_loss = total_loss / len(loader)
        if (epoch + 1) % 10 == 0 or epoch == 0:
            print(f"    Epoch {epoch+1}/{epochs} — Loss: {avg_loss:.6f}")

    # Save
    os.makedirs(SAVE_DIR, exist_ok=True)
    save_path = os.path.join(SAVE_DIR, "autoencoder.pt")
    torch.save({
        "model_state": model.state_dict(),
        "seq_len": seq_len,
        "num_features": num_features,
        "hidden_dim": 64,
        "threshold": avg_loss * 3.0  # Anomaly threshold = 3x average healthy loss
    }, save_path)
    print(f"    [+] Saved to {save_path} (threshold: {avg_loss * 3.0:.6f})")
    return model, avg_loss


def train_classifier(X_all, y_all, seq_len, epochs, batch_size, lr):
    """
    Trains the Bidirectional LSTM + Attention Classifier on ALL data (healthy + faulty).
    """
    print(f"\n{'='*60}")
    print(f"[*] Training LSTM Attention Classifier (supervised, all data)")
    print(f"    Samples: {len(X_all)}, Classes: {NUM_CLASSES}, Epochs: {epochs}")

    num_features = X_all.shape[1]
    X_seq, y_seq = create_sequences(X_all, y_all, seq_len)

    # Train/val split (80/20)
    split = int(len(X_seq) * 0.8)
    X_train, X_val = X_seq[:split], X_seq[split:]
    y_train, y_val = y_seq[:split], y_seq[split:]

    train_dataset = TensorDataset(
        torch.FloatTensor(X_train).to(DEVICE),
        torch.LongTensor(y_train).to(DEVICE)
    )
    val_dataset = TensorDataset(
        torch.FloatTensor(X_val).to(DEVICE),
        torch.LongTensor(y_val).to(DEVICE)
    )
    train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True)
    val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False)

    model = LSTMAttentionClassifier(num_features, hidden_dim=64, num_classes=NUM_CLASSES).to(DEVICE)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    criterion = nn.CrossEntropyLoss()

    best_val_acc = 0.0
    model.train()
    for epoch in range(epochs):
        total_loss = 0.0
        correct = 0
        total = 0

        for batch_x, batch_y in train_loader:
            optimizer.zero_grad()
            logits, _ = model(batch_x)
            loss = criterion(logits, batch_y)
            loss.backward()
            optimizer.step()

            total_loss += loss.item()
            preds = logits.argmax(dim=1)
            correct += (preds == batch_y).sum().item()
            total += batch_y.size(0)

        train_acc = correct / max(total, 1)

        # Validation
        model.eval()
        val_correct = 0
        val_total = 0
        with torch.no_grad():
            for batch_x, batch_y in val_loader:
                logits, _ = model(batch_x)
                preds = logits.argmax(dim=1)
                val_correct += (preds == batch_y).sum().item()
                val_total += batch_y.size(0)
        val_acc = val_correct / max(val_total, 1)
        model.train()

        if val_acc > best_val_acc:
            best_val_acc = val_acc

        if (epoch + 1) % 10 == 0 or epoch == 0:
            print(f"    Epoch {epoch+1}/{epochs} — Loss: {total_loss/len(train_loader):.4f} | Train Acc: {train_acc:.2%} | Val Acc: {val_acc:.2%}")

    # Save
    save_path = os.path.join(SAVE_DIR, "classifier.pt")
    torch.save({
        "model_state": model.state_dict(),
        "seq_len": seq_len,
        "num_features": num_features,
        "hidden_dim": 64,
        "num_classes": NUM_CLASSES,
        "fault_classes": FAULT_CLASSES,
        "best_val_acc": best_val_acc
    }, save_path)
    print(f"    [+] Saved to {save_path} (best val accuracy: {best_val_acc:.2%})")
    return model


def train_regressor(X_all, y_all, seq_len, epochs, batch_size, lr):
    """
    Trains the Time-To-Failure Regressor.
    TTF label: number of seconds until the next fault event.
    For healthy periods, TTF = max horizon. For faults, TTF = 0.
    """
    print(f"\n{'='*60}")
    print(f"[*] Training Time-To-Failure Regressor")

    num_features = X_all.shape[1]

    # TTF label = seconds until the next fault event (capped at a 300s horizon).
    # Scan backwards: remember the index of the most recent upcoming fault and
    # measure the countdown to it for every healthy sample.
    ttf_labels = np.full(len(y_all), 300.0, dtype=np.float32)
    next_fault_at = len(y_all) + 300  # far future sentinel
    for i in range(len(y_all) - 1, -1, -1):
        if y_all[i] != 0:
            next_fault_at = i
        ttf_labels[i] = min(float(next_fault_at - i), 300.0)

    X_seq, ttf_seq = create_sequences(X_all, ttf_labels, seq_len)

    tensor_X = torch.FloatTensor(X_seq).to(DEVICE)
    tensor_y = torch.FloatTensor(ttf_seq).unsqueeze(1).to(DEVICE)
    dataset = TensorDataset(tensor_X, tensor_y)
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=True)

    model = TimeToFailureRegressor(num_features, hidden_dim=64).to(DEVICE)
    optimizer = torch.optim.Adam(model.parameters(), lr=lr)
    criterion = nn.MSELoss()

    model.train()
    for epoch in range(epochs):
        total_loss = 0.0
        for batch_x, batch_y in loader:
            optimizer.zero_grad()
            pred = model(batch_x)
            loss = criterion(pred, batch_y)
            loss.backward()
            optimizer.step()
            total_loss += loss.item()

        avg_loss = total_loss / len(loader)
        if (epoch + 1) % 10 == 0 or epoch == 0:
            print(f"    Epoch {epoch+1}/{epochs} — MSE: {avg_loss:.4f} | RMSE: {avg_loss**0.5:.2f}s")

    # Save
    save_path = os.path.join(SAVE_DIR, "regressor.pt")
    torch.save({
        "model_state": model.state_dict(),
        "seq_len": seq_len,
        "num_features": num_features,
        "hidden_dim": 64,
    }, save_path)
    print(f"    [+] Saved to {save_path}")
    return model


def generate_synthetic_dataset(output_path, num_samples=2000):
    """
    Generates a synthetic training dataset when no real telemetry is available.
    Simulates healthy baseline with injected fault windows.
    """
    print(f"[*] Generating synthetic training dataset ({num_samples} samples)...")
    np.random.seed(42)

    # 12 features: per-node interface stats + routing state
    feature_names = [
        "pe1_eth1_rx_bytes", "pe1_eth1_tx_bytes",
        "pe1_eth1_rx_drops", "pe1_eth1_tx_drops",
        "p1_eth1_rx_bytes", "p1_eth1_tx_bytes",
        "pe1_frr_ospf_neighbors_total", "pe1_frr_ldp_session_operational",
        "pe1_frr_bgp_vpn_established", "pe1_frr_bgp_vpn_prefixes_received",
        "pe2_frr_bgp_vpn_established", "pe2_frr_bgp_vpn_prefixes_received"
    ]

    rate_names = [f"{n}_rate" for n in feature_names[:6]]
    header = ["timestamp", "fault_label", "fault_location"] + feature_names + rate_names

    with open(output_path, 'w', newline='') as f:
        writer = csv.writer(f)
        writer.writerow(header)

        for i in range(num_samples):
            ts = f"2026-06-27T19:{i//60:02d}:{i%60:02d}"

            # Determine fault state (inject faults in windows)
            fault_label = "Healthy"
            fault_loc = ""

            # Fault window 1: latency injection (samples 400-500)
            if 400 <= i < 500:
                fault_label = "latency"
                fault_loc = "pe1_eth1"
            # Fault window 2: packet loss (samples 800-900)
            elif 800 <= i < 900:
                fault_label = "loss"
                fault_loc = "pe1_eth1"
            # Fault window 3: link flap (samples 1200-1250)
            elif 1200 <= i < 1250:
                fault_label = "flap"
                fault_loc = "p1_eth1"
            # Fault window 4: congestion/rate limit (samples 1600-1700)
            elif 1600 <= i < 1700:
                fault_label = "rate"
                fault_loc = "pe1_eth1"
            # Fault window 5: corruption (samples 1900-1950)
            elif 1900 <= i < 1950:
                fault_label = "corrupt"
                fault_loc = "pe2_eth1"

            # Base healthy metrics
            rx_bytes = 5_000_000 + np.random.normal(0, 200_000)
            tx_bytes = 4_500_000 + np.random.normal(0, 180_000)
            rx_drops = max(0, np.random.normal(0, 1))
            tx_drops = max(0, np.random.normal(0, 0.5))
            p1_rx = 9_000_000 + np.random.normal(0, 300_000)
            p1_tx = 8_500_000 + np.random.normal(0, 280_000)
            ospf_nbrs = 2.0
            ldp_oper = 1.0
            bgp_est_1 = 1.0
            bgp_pfx_1 = 2.0
            bgp_est_2 = 1.0
            bgp_pfx_2 = 2.0

            # Apply fault effects
            if fault_label == "latency":
                # Gradual degradation pattern
                progress = (i - 400) / 100.0
                rx_drops += progress * 5
                tx_drops += progress * 3
            elif fault_label == "loss":
                rx_drops += np.random.uniform(5, 20)
                tx_drops += np.random.uniform(3, 15)
                rx_bytes *= 0.85
            elif fault_label == "flap":
                if np.random.random() > 0.5:
                    ospf_nbrs = 1.0
                    ldp_oper = 0.0
                    bgp_est_1 = 0.0
                    bgp_pfx_1 = 0.0
                rx_bytes *= 0.3
                tx_bytes *= 0.3
            elif fault_label == "rate":
                rx_bytes = min(rx_bytes, 1_000_000)
                tx_bytes = min(tx_bytes, 1_000_000)
            elif fault_label == "corrupt":
                rx_drops += np.random.uniform(2, 10)

            features = [rx_bytes, tx_bytes, rx_drops, tx_drops, p1_rx, p1_tx,
                        ospf_nbrs, ldp_oper, bgp_est_1, bgp_pfx_1, bgp_est_2, bgp_pfx_2]
            rates = [abs(np.random.normal(0, f * 0.01)) for f in features[:6]]

            row = [ts, fault_label, fault_loc] + [f"{v:.2f}" for v in features] + [f"{v:.2f}" for v in rates]
            writer.writerow(row)

    print(f"    [+] Synthetic dataset written to {output_path}")
    print(f"    Fault windows: latency[400-500], loss[800-900], flap[1200-1250], rate[1600-1700], corrupt[1900-1950]")


def main():
    parser = argparse.ArgumentParser(description="Aether ML Training Pipeline")
    _default_data = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dataset.csv")
    parser.add_argument("--data", default=_default_data, help="Path to training CSV (default: phase3-models/dataset.csv)")
    parser.add_argument("--epochs", type=int, default=50, help="Training epochs (default: 50)")
    parser.add_argument("--seq-len", type=int, default=30, help="Sequence length for LSTM (default: 30)")
    parser.add_argument("--batch-size", type=int, default=32, help="Batch size (default: 32)")
    parser.add_argument("--lr", type=float, default=0.001, help="Learning rate (default: 0.001)")
    parser.add_argument("--synthetic", action="store_true", help="Generate and train on synthetic data")
    args = parser.parse_args()

    data_path = args.data

    # Generate synthetic data if requested or if no dataset exists
    if args.synthetic or not os.path.exists(data_path):
        if not args.synthetic:
            print(f"[!] Dataset {data_path} not found. Generating synthetic data...")
        generate_synthetic_dataset(data_path)

    # Load
    X, y, columns = load_dataset(data_path)

    if len(X) < args.seq_len + 10:
        print(f"[-] Not enough data ({len(X)} samples) for seq_len={args.seq_len}. Need at least {args.seq_len + 10}.")
        sys.exit(1)

    # Normalize
    X_norm, mins, maxs = normalize(X)

    # Save normalization params for inference
    os.makedirs(SAVE_DIR, exist_ok=True)
    np.save(os.path.join(SAVE_DIR, "norm_mins.npy"), mins)
    np.save(os.path.join(SAVE_DIR, "norm_maxs.npy"), maxs)
    with open(os.path.join(SAVE_DIR, "columns.txt"), 'w') as f:
        f.write('\n'.join(columns))

    print(f"\n[*] Device: {DEVICE}")
    if DEVICE.type == "cuda":
        print(f"    GPU: {torch.cuda.get_device_name(0)}")
        print(f"    VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")

    # 1. Train Autoencoder (healthy data only)
    healthy_mask = (y == 0)
    X_healthy = X_norm[healthy_mask]
    if len(X_healthy) > args.seq_len:
        train_autoencoder(X_healthy, args.seq_len, args.epochs, args.batch_size, args.lr)
    else:
        print("[!] Not enough healthy samples for autoencoder training.")

    # 2. Train Classifier (all data)
    train_classifier(X_norm, y, args.seq_len, args.epochs, args.batch_size, args.lr)

    # 3. Train TTF Regressor (all data)
    train_regressor(X_norm, y, args.seq_len, args.epochs, args.batch_size, args.lr)

    print(f"\n{'='*60}")
    print(f"[+] All models saved to {SAVE_DIR}/")
    print(f"    autoencoder.pt — Unsupervised anomaly detection")
    print(f"    classifier.pt  — Supervised fault classification")
    print(f"    regressor.pt   — Time-to-failure regression")


if __name__ == "__main__":
    main()
