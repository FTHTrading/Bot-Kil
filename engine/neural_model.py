"""
neural_model.py
===============
PyTorch neural network for Kalshi 15-min crypto directional markets.

Architecture: Feature MLP with residual connections + asset embedding.
Input features (13):
  gap_pct, mom_1m, mom_3m, mom_5m, mom_15m, realized_vol,
  t_remaining_norm, hour_sin, hour_cos, trend_up, trend_down,
  gap_pos (binary), gap_neg (binary)
Plus: asset embedding (5 assets → 8-dim)

Output: sigmoid probability that YES wins (price goes up).

Usage:
    from engine.neural_model import KalshiNet, load_model, predict_prob
    model = load_model("models/kalshi_net.pt")
    prob  = predict_prob(model, features_dict, asset="BTC")
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import torch
import torch.nn as nn

# ── Constants ─────────────────────────────────────────────────────────────────
ASSETS      = ["BTC", "ETH", "SOL", "DOGE", "XRP"]
ASSET_IDX   = {a: i for i, a in enumerate(ASSETS)}
N_FEATURES  = 13
EMBED_DIM   = 8
HIDDEN      = 128
MODEL_PATH  = Path(__file__).parent.parent / "models" / "kalshi_net.pt"

# Feature schema — canonical ordered list; hash used to detect training-serving skew
FEATURE_SCHEMA = [
    "gap_pct", "mom_1m", "mom_3m", "mom_5m", "mom_15m",
    "realized_vol", "t_remaining_norm", "hour_sin", "hour_cos",
    "trend_up", "trend_down", "gap_pos", "gap_neg",
]
SCHEMA_HASH = hashlib.md5(",".join(FEATURE_SCHEMA).encode()).hexdigest()[:12]

_startup_done = False


# ── Model ─────────────────────────────────────────────────────────────────────
class KalshiNet(nn.Module):
    """
    Feed-forward net with residual connections.
    Input: flat feature vector (N_FEATURES) + asset embedding (EMBED_DIM)
    Output: single sigmoid value = P(YES wins)
    """
    def __init__(self, n_features: int = N_FEATURES, embed_dim: int = EMBED_DIM, hidden: int = HIDDEN):
        super().__init__()
        in_dim = n_features + embed_dim
        self.asset_emb = nn.Embedding(len(ASSETS), embed_dim)

        self.fc1  = nn.Linear(in_dim, hidden)
        self.fc2  = nn.Linear(hidden, hidden)
        self.fc3  = nn.Linear(hidden, hidden // 2)
        self.fc4  = nn.Linear(hidden // 2, 1)

        self.res_proj = nn.Linear(in_dim, hidden)   # residual projection
        self.bn1  = nn.BatchNorm1d(hidden)
        self.bn2  = nn.BatchNorm1d(hidden)
        self.drop = nn.Dropout(0.25)
        self.act  = nn.GELU()

    def forward(self, x: torch.Tensor, asset_ids: torch.Tensor) -> torch.Tensor:
        emb  = self.asset_emb(asset_ids)         # (B, embed_dim)
        inp  = torch.cat([x, emb], dim=-1)        # (B, N_FEATURES + embed_dim)

        h1   = self.act(self.bn1(self.fc1(inp)))
        h1   = h1 + self.res_proj(inp)            # residual
        h1   = self.drop(h1)

        h2   = self.act(self.bn2(self.fc2(h1)))
        h2   = h2 + h1                            # residual
        h2   = self.drop(h2)

        h3   = self.act(self.fc3(h2))
        out  = self.fc4(h3)                       # (B, 1)
        return torch.sigmoid(out).squeeze(-1)     # (B,)


# ── Feature encoding ──────────────────────────────────────────────────────────
def encode_features(row: dict) -> list[float]:
    """
    Convert a feature dict (from training data or live signals) to a
    fixed-length float vector of length N_FEATURES=13.
    """
    t_rem       = float(row.get("t_remaining", 7.5))
    t_norm      = min(t_rem, 15.0) / 15.0         # 0=expiring, 1=fresh

    hour        = int(row.get("hour_utc", 12))
    hour_sin    = math.sin(2 * math.pi * hour / 24)
    hour_cos    = math.cos(2 * math.pi * hour / 24)

    trend       = row.get("trend_at_snap", row.get("trend", "flat"))
    trend_up    = 1.0 if trend == "up"   else 0.0
    trend_down  = 1.0 if trend == "down" else 0.0

    gap         = float(row.get("gap_pct", row.get("gap_pct", 0.0)))
    gap_pos     = 1.0 if gap > 0 else 0.0
    gap_neg     = 1.0 if gap < 0 else 0.0

    # clip to avoid extreme outliers blowing up gradients
    def c(v, lo=-0.05, hi=0.05):
        return max(lo, min(hi, float(v)))

    return [
        c(gap,  -0.10,  0.10),            # gap_pct
        c(row.get("mom_1m",  0.0)),        # mom_1m
        c(row.get("mom_3m",  0.0)),        # mom_3m
        c(row.get("mom_5m",  0.0)),        # mom_5m
        c(row.get("mom_15m", 0.0)),        # mom_15m
        min(float(row.get("realized_vol", 0.001)), 0.05),  # vol
        t_norm,                            # time remaining
        hour_sin,                          # seasonality
        hour_cos,
        trend_up,
        trend_down,
        gap_pos,
        gap_neg,
    ]


# ── GPU Diagnostics ──────────────────────────────────────────────────────────
def gpu_startup_banner() -> dict:
    """
    Print GPU / environment diagnostics once at startup.
    Returns info dict with torch_version, cuda_available, device_name, vram_gb, schema_hash.
    """
    global _startup_done
    cuda = torch.cuda.is_available()
    info = {
        "torch_version":  torch.__version__,
        "cuda_available": cuda,
        "device_count":   torch.cuda.device_count() if cuda else 0,
        "device_name":    torch.cuda.get_device_name(0) if cuda else "cpu",
        "vram_gb":        round(torch.cuda.get_device_properties(0).total_memory / 1e9, 1) if cuda else 0.0,
        "schema_hash":    SCHEMA_HASH,
    }
    if not _startup_done:
        _startup_done = True
        print(f"[KalshiNet] ─── GPU STARTUP ─────────────────────────────────")
        print(f"[KalshiNet]   torch:       {info['torch_version']}")
        print(f"[KalshiNet]   cuda:        {info['cuda_available']}  (devices: {info['device_count']})")
        print(f"[KalshiNet]   device[0]:   {info['device_name']}")
        print(f"[KalshiNet]   VRAM:        {info['vram_gb']} GB")
        print(f"[KalshiNet]   schema_hash: {info['schema_hash']}")
        print(f"[KalshiNet] ─────────────────────────────────────────────────")
    return info


# ── Save / Load ───────────────────────────────────────────────────────────────
def save_model(model: KalshiNet, path: Path = MODEL_PATH, meta: dict = None):
    path.parent.mkdir(exist_ok=True)
    full_meta = {"schema_hash": SCHEMA_HASH, **(meta or {})}
    torch.save({"state_dict": model.state_dict(), "meta": full_meta}, path)
    print(f"[KalshiNet] Saved → {path}  schema={SCHEMA_HASH}")


def load_model(path: Path = MODEL_PATH, device: str = "cpu") -> Optional[KalshiNet]:
    if not Path(path).exists():
        return None
    ckpt  = torch.load(path, map_location=device, weights_only=False)
    meta  = ckpt.get("meta", {})
    saved_hash = meta.get("schema_hash")
    if saved_hash and saved_hash != SCHEMA_HASH:
        print(f"[KalshiNet] WARNING: schema_hash mismatch — "
              f"model trained on schema={saved_hash}, current={SCHEMA_HASH}. "
              f"Retrain (python scripts/train_neural_model.py) before live use.")
    model = KalshiNet()
    model.load_state_dict(ckpt["state_dict"])
    model.eval()
    model.to(device)
    td = meta.get("training_date", "unknown")
    va = meta.get("val_acc", "?")
    sh = saved_hash or "none"
    print(f"[KalshiNet] Loaded → device={device}  trained={td}  val_acc={va}%  schema={sh}")
    return model


# ── Inference ─────────────────────────────────────────────────────────────────
_cached_model: Optional[KalshiNet] = None
_cached_device: str = "cpu"

def get_model(path: Path = MODEL_PATH) -> Optional[KalshiNet]:
    global _cached_model, _cached_device
    if _cached_model is not None:
        return _cached_model
    gpu_startup_banner()
    device = "cuda" if torch.cuda.is_available() else "cpu"
    _cached_device = device
    if device == "cuda":
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        print(f"[KalshiNet] TF32 matmul+cudnn enabled")
    _cached_model = load_model(path, device)
    return _cached_model


def predict_prob(model: KalshiNet, features: dict, asset: str, device: str = None) -> float:
    """
    Return P(YES wins) in [0, 1].

    features: dict with keys: gap_pct, mom_1m, mom_3m, mom_5m, mom_15m,
              realized_vol, t_remaining, trend_at_snap/trend, hour_utc
    asset: "BTC", "ETH", "SOL", "DOGE", "XRP"
    """
    if device is None:
        device = _cached_device
    with torch.inference_mode():
        x = torch.tensor([encode_features(features)], dtype=torch.float32).to(device)
        a = torch.tensor([ASSET_IDX.get(asset, 0)], dtype=torch.long).to(device)
        return float(model(x, a).item())


def predict_prob_batch(
    model: KalshiNet,
    feature_rows: list[dict],
    assets: list[str],
    device: str = None,
) -> list[float]:
    """
    Batch inference for multiple market snapshots at once — more efficient for GPU.
    Returns list of P(YES wins) values, one per input row.
    """
    if not feature_rows:
        return []
    if device is None:
        device = _cached_device
    with torch.inference_mode():
        xs  = torch.tensor(
            [encode_features(r) for r in feature_rows], dtype=torch.float32
        ).to(device)
        as_ = torch.tensor(
            [ASSET_IDX.get(a, 0) for a in assets], dtype=torch.long
        ).to(device)
        return model(xs, as_).tolist()
