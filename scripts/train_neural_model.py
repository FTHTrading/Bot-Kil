"""
train_neural_model.py
=====================
Train KalshiNet on historical data using the 5090 GPU.

  python scripts/train_neural_model.py

Reads:   data/training_data.jsonl
Writes:  models/kalshi_net.pt

Training loop:
  - 80/20 train/val split, stratified by asset
  - BCE loss with class balancing
  - AdamW + cosine LR schedule
  - Early stopping on val loss (patience=15)
  - Prints accuracy + calibration stats at end
"""
from __future__ import annotations

import json
import math
import random
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import torch
import torch.nn as nn
from torch.utils.data import DataLoader, TensorDataset, WeightedRandomSampler

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))
from engine.neural_model import KalshiNet, ASSET_IDX, SCHEMA_HASH, encode_features, gpu_startup_banner, save_model, MODEL_PATH

DATA_FILE = ROOT / "data" / "training_data.jsonl"

# ── Hypers ────────────────────────────────────────────────────────────────────
EPOCHS      = 120
BATCH_SIZE  = 512
LR          = 3e-4
WEIGHT_DECAY= 1e-4
PATIENCE    = 15
VAL_FRAC    = 0.20
SEED        = 42


def load_dataset(path: Path):
    rows = [json.loads(l) for l in path.read_text(encoding="utf-8-sig").splitlines() if l.strip()]
    print(f"Loaded {len(rows)} rows from {path}")

    # balance YES/NO
    yes_rows = [r for r in rows if r["label"] == 1]
    no_rows  = [r for r in rows if r["label"] == 0]
    print(f"  YES: {len(yes_rows)}  NO: {len(no_rows)}")

    Xs, As, Ys = [], [], []
    for r in rows:
        asset_id = ASSET_IDX.get(r.get("asset", "BTC"), 0)
        feats    = encode_features(r)
        Xs.append(feats)
        As.append(asset_id)
        Ys.append(float(r["label"]))

    X = torch.tensor(Xs, dtype=torch.float32)
    A = torch.tensor(As, dtype=torch.long)
    Y = torch.tensor(Ys, dtype=torch.float32)
    return X, A, Y, rows


def train():
    torch.manual_seed(SEED)
    random.seed(SEED)

    device = "cuda" if torch.cuda.is_available() else "cpu"
    gpu_startup_banner()
    if device == "cuda":
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        print(f"[train] TF32 enabled for matmul+cudnn")
    print(f"\nDevice: {device}")
    if device == "cuda":
        print(f"GPU: {torch.cuda.get_device_name(0)}")
        print(f"VRAM: {torch.cuda.get_device_properties(0).total_memory / 1e9:.1f} GB")

    if not DATA_FILE.exists():
        print(f"[!] Training data not found: {DATA_FILE}")
        print("    Run:  python scripts/fetch_training_data.py  first")
        sys.exit(1)

    X, A, Y, rows = load_dataset(DATA_FILE)
    n = len(X)
    idx = list(range(n))
    random.shuffle(idx)
    split      = int(n * (1 - VAL_FRAC))
    tr_idx     = idx[:split]
    val_idx    = idx[split:]

    X_tr, A_tr, Y_tr = X[tr_idx], A[tr_idx], Y[tr_idx]
    X_val, A_val, Y_val = X[val_idx], A[val_idx], Y[val_idx]

    # Weighted sampler to handle class imbalance
    class_counts = Counter(Y_tr.tolist())
    weights      = [1.0 / class_counts[y.item()] for y in Y_tr]
    sampler      = WeightedRandomSampler(weights, len(weights), replacement=True)

    tr_ds  = TensorDataset(X_tr, A_tr, Y_tr)
    val_ds = TensorDataset(X_val, A_val, Y_val)
    tr_dl  = DataLoader(tr_ds, batch_size=BATCH_SIZE, sampler=sampler)
    val_dl = DataLoader(val_ds, batch_size=1024, shuffle=False)

    model  = KalshiNet().to(device)
    opt    = torch.optim.AdamW(model.parameters(), lr=LR, weight_decay=WEIGHT_DECAY)
    sched  = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=EPOCHS, eta_min=LR * 0.05)
    crit   = nn.BCELoss()

    best_val_loss = float("inf")
    best_epoch    = 0
    patience_ctr  = 0
    best_state    = None

    print(f"\nTraining {len(tr_idx)} / Val {len(val_idx)} samples")
    print(f"Epochs={EPOCHS}  Batch={BATCH_SIZE}  LR={LR}  Device={device}\n")

    for epoch in range(1, EPOCHS + 1):
        # ── Train ──
        model.train()
        tr_loss = 0.0
        for xb, ab, yb in tr_dl:
            xb, ab, yb = xb.to(device), ab.to(device), yb.to(device)
            opt.zero_grad()
            pred = model(xb, ab)
            loss = crit(pred, yb)
            loss.backward()
            nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            opt.step()
            tr_loss += loss.item() * len(xb)
        tr_loss /= len(tr_idx)
        sched.step()

        # ── Val ──
        model.eval()
        val_loss = 0.0
        correct  = 0
        with torch.no_grad():
            for xb, ab, yb in val_dl:
                xb, ab, yb = xb.to(device), ab.to(device), yb.to(device)
                pred     = model(xb, ab)
                val_loss += crit(pred, yb).item() * len(xb)
                correct  += ((pred > 0.5).float() == yb).sum().item()
        val_loss /= len(val_idx)
        val_acc   = correct / len(val_idx) * 100

        if epoch % 10 == 0 or epoch <= 5:
            lr_now = sched.get_last_lr()[0]
            print(f"Ep {epoch:>3}/{EPOCHS}  tr={tr_loss:.4f}  val={val_loss:.4f}  "
                  f"acc={val_acc:.1f}%  lr={lr_now:.2e}")

        # Early stopping
        if val_loss < best_val_loss - 1e-5:
            best_val_loss = val_loss
            best_epoch    = epoch
            best_state    = {k: v.clone() for k, v in model.state_dict().items()}
            patience_ctr  = 0
        else:
            patience_ctr += 1
            if patience_ctr >= PATIENCE:
                print(f"\nEarly stop at epoch {epoch} (best={best_epoch})")
                break

    # Restore best
    model.load_state_dict(best_state)
    model.eval()

    # ── Final validation stats ──
    all_preds, all_labels = [], []
    model.eval()
    with torch.no_grad():
        for xb, ab, yb in val_dl:
            xb, ab, yb = xb.to(device), ab.to(device), yb.to(device)
            preds = model(xb, ab)
            all_preds.extend(preds.cpu().tolist())
            all_labels.extend(yb.cpu().tolist())

    # Accuracy
    correct = sum((p > 0.5) == (l == 1) for p, l in zip(all_preds, all_labels))
    acc = correct / len(all_preds) * 100

    # Calibration buckets
    buckets = {i: [0, 0] for i in range(5)}  # [correct, total] per 20% bucket
    for p, l in zip(all_preds, all_labels):
        b = min(int(p * 5), 4)
        buckets[b][1] += 1
        if (p > 0.5) == (l == 1):
            buckets[b][0] += 1

    print(f"\n{'='*50}")
    print(f"TRAINING COMPLETE — Best epoch: {best_epoch}  Val loss: {best_val_loss:.4f}")
    print(f"Val accuracy: {acc:.1f}%  ({correct}/{len(all_preds)})")
    print(f"\nCalibration (confidence bucket → accuracy):")
    for b, (c, t) in buckets.items():
        lo = b * 20
        if t > 0:
            print(f"  {lo}-{lo+20}%:  {c}/{t} = {c/t*100:.0f}%")

    meta = {
        "best_epoch":    best_epoch,
        "val_loss":      round(best_val_loss, 5),
        "val_acc":       round(acc, 2),
        "n_train":       len(tr_idx),
        "n_val":         len(val_idx),
        "device":        device,
        "training_date": datetime.now(timezone.utc).isoformat()[:10],
        "schema_hash":   SCHEMA_HASH,
    }
    save_model(model, MODEL_PATH, meta)
    print(f"\nModel saved → {MODEL_PATH}")
    print("Now run paper bot with neural model — it will auto-load from models/kalshi_net.pt")


if __name__ == "__main__":
    train()
