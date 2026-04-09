"""
engine/calibration.py — Probability calibration layer
======================================================
Maps raw ensemble probabilities to calibrated ones using per-regime
learned calibrators.  Falls back to identity (no-op) when insufficient
training data is present.

Calibrators are stored in: models/calibrators/{asset}_{vol}_{bucket}.pkl
Each calibrator is an sklearn IsotonicRegression or (if sklearn unavailable)
a simple Platt sigmoid + bin-count fallback.

Usage:
    from engine.calibration import CalibrationStore
    store = CalibrationStore()
    cal_p, meta = store.calibrate(raw_p, asset="BTC", vol_regime="normal", ttc_bucket="1-6h")
"""
from __future__ import annotations

import json
import math
import pickle
import logging
from pathlib import Path
from typing import Optional, Tuple

log = logging.getLogger(__name__)

_CALIBRATOR_DIR = Path(__file__).parent.parent / "models" / "calibrators"
_MIN_SAMPLES_ISOTONIC = 30   # need at least N samples to fit isotonic regression
_PLATT_MIN_SAMPLES = 10      # need at least N samples to fit Platt sigmoid


# ── Pure-Python Platt sigmoid calibration (no sklearn required) ──────────────

def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


class PlattCalibrator:
    """
    Minimal Platt scaling calibrator.
    Fits logistic regression: P_cal = sigmoid(A * logit(p) + B)
    via gradient descent on (raw_prob, outcome) pairs.
    """
    def __init__(self):
        self.A: float = 1.0
        self.B: float = 0.0
        self.fitted: bool = False
        self.n_samples: int = 0

    def _logit(self, p: float) -> float:
        p = max(1e-6, min(1 - 1e-6, p))
        return math.log(p / (1 - p))

    def fit(
        self,
        probs: list[float],
        outcomes: list[float],
        lr: float = 0.01,
        epochs: int = 300,
        lambda_reg: float = 0.01,
    ):
        """Fit A, B via gradient descent with L2 regularisation and early stopping."""
        if len(probs) < _PLATT_MIN_SAMPLES:
            self.fitted = False
            return self
        self.n_samples = len(probs)

        # Hold out 20% for early stopping when enough samples exist
        if len(probs) >= 20:
            n_val = max(2, len(probs) // 5)
            trn_p, val_p = probs[n_val:], probs[:n_val]
            trn_y, val_y = outcomes[n_val:], outcomes[:n_val]
        else:
            trn_p, val_p = probs, []
            trn_y, val_y = outcomes, []

        A, B = 1.0, 0.0
        best_A, best_B, best_loss = A, B, float("inf")
        patience, no_improve = 20, 0

        for _ in range(epochs):
            dA = dB = 0.0
            for p, y in zip(trn_p, trn_y):
                logit = self._logit(p)
                pred  = _sigmoid(A * logit + B)
                err   = pred - y
                dA   += err * logit
                dB   += err
            n = len(trn_p)
            A -= lr * (dA / n + lambda_reg * A)   # L2 on A; B is intercept, no penalty
            B -= lr * dB / n

            if val_p:
                val_loss = sum(
                    -(y * math.log(max(1e-9, _sigmoid(A * self._logit(p) + B)))
                      + (1 - y) * math.log(max(1e-9, 1 - _sigmoid(A * self._logit(p) + B))))
                    for p, y in zip(val_p, val_y)
                ) / len(val_p)
                if val_loss < best_loss - 1e-5:
                    best_loss = val_loss
                    best_A, best_B = A, B
                    no_improve = 0
                else:
                    no_improve += 1
                    if no_improve >= patience:
                        A, B = best_A, best_B
                        break

        self.A = A
        self.B = B
        self.fitted = True
        return self

    def predict(self, p: float) -> float:
        if not self.fitted:
            return p
        logit = self._logit(p)
        return max(0.01, min(0.99, _sigmoid(self.A * logit + self.B)))


class BinCalibrator:
    """
    Frequency calibrator: bins raw probs 0..1 into N_BINS buckets,
    replaces each bin with empirical win frequency.
    Acts as fallback when Platt fails.
    N_BINS=10 requires at least 5+ samples per bin (50 total ideally).
    """
    N_BINS = 10

    def __init__(self):
        self.bin_probs: list[float] = []
        self.fitted: bool = False
        self.n_samples: int = 0

    # Laplace smoothing: nudge sparse bins toward the neutral prior instead of
    # using raw empirical frequency.  alpha=0.5 is a half-count (weak prior).
    _LAPLACE_ALPHA = 0.5
    _LAPLACE_PRIOR = 0.5

    def fit(self, probs: list[float], outcomes: list[float]):
        counts  = [0] * self.N_BINS
        correct = [0.0] * self.N_BINS
        for p, y in zip(probs, outcomes):
            idx = min(int(p * self.N_BINS), self.N_BINS - 1)
            counts[idx] += 1
            correct[idx] += y
        self.bin_probs = []
        alpha  = self._LAPLACE_ALPHA
        prior  = self._LAPLACE_PRIOR
        for i in range(self.N_BINS):
            # Laplace-smoothed empirical frequency
            smoothed = (correct[i] + alpha * prior) / (counts[i] + alpha)
            self.bin_probs.append(smoothed)
        self.fitted = True
        self.n_samples = len(probs)
        return self

    def predict(self, p: float) -> float:
        if not self.fitted or not self.bin_probs:
            return p
        idx = min(int(p * self.N_BINS), self.N_BINS - 1)
        return max(0.01, min(0.99, self.bin_probs[idx]))


# ── Isotonic wrapper (sklearn optional) ──────────────────────────────────────

class IsotonicCalibrator:
    """Thin wrapper around sklearn IsotonicRegression, if available."""

    def __init__(self):
        self._ir = None
        self.fitted = False
        self.n_samples = 0

    def fit(self, probs: list[float], outcomes: list[float]):
        if len(probs) < _MIN_SAMPLES_ISOTONIC:
            return self
        try:
            from sklearn.isotonic import IsotonicRegression
            ir = IsotonicRegression(out_of_bounds="clip")
            ir.fit(probs, outcomes)
            self._ir = ir
            self.fitted = True
            self.n_samples = len(probs)
        except ImportError:
            log.debug("[calibration] sklearn not available — isotonic not fitted")
        return self

    def predict(self, p: float) -> float:
        if not self.fitted or self._ir is None:
            return p
        return float(self._ir.predict([p])[0])


# ── Calibration store ─────────────────────────────────────────────────────────

_CALIBRATOR_CACHE: dict[str, object] = {}


def _trend_slug(trend: str) -> str:
    """Filesystem-safe slug for trend names (avoids underscore ambiguity in stems)."""
    return {"mean_reverting": "mr"}.get(trend, trend)


def _calib_path(asset: str, vol_regime: str, ttc_bucket: str, trend: str = "flat") -> Path:
    fname = f"{asset}_{vol_regime}_{ttc_bucket}_{_trend_slug(trend)}.pkl"
    return _CALIBRATOR_DIR / fname


def _load_calibrator(key: str, path: Path):
    """Load calibrator from disk if not already cached."""
    if key in _CALIBRATOR_CACHE:
        return _CALIBRATOR_CACHE[key]
    if path.exists():
        try:
            with open(path, "rb") as f:
                cal = pickle.load(f)
            _CALIBRATOR_CACHE[key] = cal
            log.debug("[calibration] Loaded calibrator %s (n=%d)", key, getattr(cal, "n_samples", -1))
            return cal
        except Exception as e:
            log.warning("[calibration] Failed to load %s: %s", path, e)
    return None


def _save_calibrator(key: str, path: Path, cal):
    _CALIBRATOR_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "wb") as f:
            pickle.dump(cal, f)
        _CALIBRATOR_CACHE[key] = cal
        log.info("[calibration] Saved calibrator %s (n=%d)", key, getattr(cal, "n_samples", -1))
    except Exception as e:
        log.warning("[calibration] Failed to save %s: %s", path, e)


class CalibrationStore:
    """
    Load/save and apply per-regime probability calibrators.

    Hierarchy (tries in order):
      IsotonicRegression (≥30 samples) → PlattCalibrator (≥10) → BinCalibrator → identity
    """

    def calibrate(
        self,
        raw_p: float,
        asset: str,
        vol_regime: str,
        ttc_bucket: str,
        trend: str = "flat",
    ) -> Tuple[float, dict]:
        """
        Apply calibration to a raw ensemble probability.

        Returns (calibrated_p, metadata_dict).  Calibrators are keyed on
        (asset, vol_regime, ttc_bucket, trend) so trending and mean-reverting
        markets never share the same calibration bucket.
        """
        key  = f"{asset}_{vol_regime}_{ttc_bucket}_{_trend_slug(trend)}"
        path = _calib_path(asset, vol_regime, ttc_bucket, trend)
        cal  = _load_calibrator(key, path)

        if cal is None:
            return raw_p, {"method": "identity", "reason": "no calibrator fitted yet"}

        method = type(cal).__name__
        try:
            cal_p = float(cal.predict(raw_p))
            cal_p = max(0.01, min(0.99, cal_p))
            delta = cal_p - raw_p
            return cal_p, {
                "method":   method,
                "raw_p":    round(raw_p, 4),
                "cal_p":    round(cal_p, 4),
                "delta":    round(delta, 4),
                "n_samples": getattr(cal, "n_samples", 0),
            }
        except Exception as e:
            log.warning("[calibration] predict failed for %s: %s", key, e)
            return raw_p, {"method": "identity", "reason": str(e)}

    def update(
        self,
        probs: list[float],
        outcomes: list[float],
        asset: str,
        vol_regime: str,
        ttc_bucket: str,
        trend: str = "flat",
    ) -> dict:
        """
        Re-fit a calibrator from (prob, outcome) pairs and persist to disk.
        Outcomes must be 0.0 or 1.0.

        Returns dict with fit stats.
        """
        if len(probs) != len(outcomes):
            return {"error": "probs and outcomes length mismatch"}
        if len(probs) < 5:
            return {"error": f"too few samples ({len(probs)} < 5)"}

        key  = f"{asset}_{vol_regime}_{ttc_bucket}_{_trend_slug(trend)}"
        path = _calib_path(asset, vol_regime, ttc_bucket, trend)

        # Try isotonic first
        if len(probs) >= _MIN_SAMPLES_ISOTONIC:
            cal = IsotonicCalibrator()
            cal.fit(probs, outcomes)
            if cal.fitted:
                _save_calibrator(key, path, cal)
                return {"method": "isotonic", "n_samples": len(probs), "key": key}

        # Fall back to Platt
        if len(probs) >= _PLATT_MIN_SAMPLES:
            cal = PlattCalibrator()
            cal.fit(probs, outcomes)
            if cal.fitted:
                _save_calibrator(key, path, cal)
                return {"method": "platt", "n_samples": len(probs), "key": key}

        # Final fallback: bin calibrator
        cal = BinCalibrator()
        cal.fit(probs, outcomes)
        _save_calibrator(key, path, cal)
        return {"method": "bin", "n_samples": len(probs), "key": key}

    def list_calibrators(self) -> list[dict]:
        """Return metadata for all fitted calibrators on disk."""
        if not _CALIBRATOR_DIR.exists():
            return []
        out = []
        for pkl in sorted(_CALIBRATOR_DIR.glob("*.pkl")):
            try:
                with open(pkl, "rb") as f:
                    cal = pickle.load(f)
                parts = pkl.stem.split("_")
                # stem format: {asset}_{vol}_{bucket}_{trend_slug}  (4 parts)
                out.append({
                    "file":      pkl.name,
                    "asset":     parts[0] if len(parts) > 0 else "?",
                    "vol":       parts[1] if len(parts) > 1 else "?",
                    "bucket":    parts[2] if len(parts) > 2 else "?",
                    "trend":     parts[3] if len(parts) > 3 else "?",
                    "method":    type(cal).__name__,
                    "n_samples": getattr(cal, "n_samples", 0),
                })
            except Exception:
                out.append({"file": pkl.name, "error": "corrupt"})
        return out
