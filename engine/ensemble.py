"""
engine/ensemble.py — Calibrated weighted ensemble
==================================================
Replaces the naive arithmetic average in run_deep_analysis with a
regime-adaptive weighted ensemble.

Model slots:
  - "diffusion"  : log-normal price diffusion (crypto_ev)
  - "monte_carlo": GBM Monte Carlo (10k paths)
  - "neural"     : KalshiNet neural inference
  - "technical"  : RSI + Bollinger technical signals

Base weights vary by (asset, ttc_bucket, vol_regime).  The ensemble
also computes model disagreement (std-dev) and penalises confidence
when models diverge significantly.

Usage:
    from engine.ensemble import WeightedEnsemble, EnsembleResult
    e = WeightedEnsemble()
    result = e.run(model_probs, regime)
"""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from typing import Optional

from engine.regime import RegimeSnapshot

# ── Weight tables ─────────────────────────────────────────────────────────────
# Format: {(ttc_bucket, vol_regime): {model: weight}}
# Weights must sum to 1.0.  Models absent from a pick get their weight
# redistributed proportionally.

_BASE_WEIGHTS: dict[tuple[str, str], dict[str, float]] = {
    # Intraday (≤15m) — neural and technical dominate; diffusion is weak on intraday
    ("le15m", "low"):    {"diffusion": 0.10, "monte_carlo": 0.15, "neural": 0.55, "technical": 0.20},
    ("le15m", "normal"): {"diffusion": 0.10, "monte_carlo": 0.15, "neural": 0.55, "technical": 0.20},
    ("le15m", "high"):   {"diffusion": 0.05, "monte_carlo": 0.20, "neural": 0.55, "technical": 0.20},

    # Sub-hourly (15m–1h)
    ("le1h",  "low"):    {"diffusion": 0.25, "monte_carlo": 0.20, "neural": 0.40, "technical": 0.15},
    ("le1h",  "normal"): {"diffusion": 0.25, "monte_carlo": 0.20, "neural": 0.40, "technical": 0.15},
    ("le1h",  "high"):   {"diffusion": 0.20, "monte_carlo": 0.25, "neural": 0.40, "technical": 0.15},

    # 1–6h: diffusion + neural split evenly
    ("1-6h",  "low"):    {"diffusion": 0.35, "monte_carlo": 0.25, "neural": 0.30, "technical": 0.10},
    ("1-6h",  "normal"): {"diffusion": 0.35, "monte_carlo": 0.25, "neural": 0.30, "technical": 0.10},
    ("1-6h",  "high"):   {"diffusion": 0.30, "monte_carlo": 0.30, "neural": 0.30, "technical": 0.10},

    # 6–24h: diffusion dominates, technical still meaningful
    ("6-24h", "low"):    {"diffusion": 0.40, "monte_carlo": 0.25, "neural": 0.25, "technical": 0.10},
    ("6-24h", "normal"): {"diffusion": 0.40, "monte_carlo": 0.25, "neural": 0.25, "technical": 0.10},
    ("6-24h", "high"):   {"diffusion": 0.35, "monte_carlo": 0.30, "neural": 0.25, "technical": 0.10},

    # > 24h: diffusion dominant; neural less reliable far out
    ("gt24h", "low"):    {"diffusion": 0.50, "monte_carlo": 0.25, "neural": 0.20, "technical": 0.05},
    ("gt24h", "normal"): {"diffusion": 0.50, "monte_carlo": 0.25, "neural": 0.20, "technical": 0.05},
    ("gt24h", "high"):   {"diffusion": 0.45, "monte_carlo": 0.30, "neural": 0.20, "technical": 0.05},
}

# Disagreement threshold above which confidence is penalised
_DISAGREE_THRESHOLD = 0.12   # std-dev > 12% → penalise confidence
_DISAGREE_MAX_PENALTY = 0.30  # max confidence reduction (30%)

# Minimum number of models required for a valid ensemble
_MIN_MODELS = 2

# Trend-specific weight multipliers (applied on top of base weights, then re-normalised).
# mean_reverting  → boost technical (RSI/BB most informative), cut diffusion
# flat            → reduce neural (no trend signal to exploit), slight technical bump
# trending        → favour neural + diffusion, reduce technical noise on longer buckets
_TREND_WEIGHT_MODS: dict[str, dict[str, float]] = {
    "mean_reverting": {"diffusion": 0.55, "monte_carlo": 0.80, "neural": 1.00, "technical": 1.70},
    "flat":           {"diffusion": 0.90, "monte_carlo": 1.05, "neural": 0.82, "technical": 1.25},
    "trending":       {"diffusion": 1.10, "monte_carlo": 1.00, "neural": 1.20, "technical": 0.65},
}


@dataclass
class EnsembleResult:
    """Output of WeightedEnsemble.run()."""
    weighted_prob:    float                # calibrated ensemble probability
    raw_probs:        dict[str, float]     # {model: raw_prob}
    weights_used:     dict[str, float]     # {model: final_weight}
    disagreement:     float                # std-dev of model probs
    confidence:       float                # 0–1 quality score
    n_models:         int
    ttc_bucket:       str
    vol_regime:       str
    trend:            str
    notes:            list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "weighted_prob":  round(self.weighted_prob, 4),
            "raw_probs":      {k: round(v, 4) for k, v in self.raw_probs.items()},
            "weights_used":   {k: round(v, 4) for k, v in self.weights_used.items()},
            "disagreement":   round(self.disagreement, 4),
            "confidence":     round(self.confidence, 4),
            "n_models":       self.n_models,
            "ttc_bucket":     self.ttc_bucket,
            "vol_regime":     self.vol_regime,
            "trend":          self.trend,
            "notes":          self.notes,
        }


class WeightedEnsemble:
    """
    Regime-adaptive weighted ensemble of probability models.

    Call run(model_probs, regime) with a dict of {model_name: probability}
    and a RegimeSnapshot.  Missing models are redistributed.
    """

    def run(
        self,
        model_probs: dict[str, float],
        regime: RegimeSnapshot,
        calib_meta: Optional[dict] = None,
    ) -> EnsembleResult:
        """
        Compute weighted ensemble probability.

        Parameters
        ----------
        model_probs : dict mapping model name → probability (0–1).
                      Valid keys: "diffusion", "monte_carlo", "neural", "technical"
        regime      : RegimeSnapshot from engine.regime
        calib_meta  : optional metadata dict returned by CalibrationStore.calibrate(),
                      used to penalise confidence when the calibrator is sparse.

        Returns
        -------
        EnsembleResult
        """
        notes: list[str] = []

        # Clamp probs to [0.01, 0.99]
        cleaned = {k: max(0.01, min(0.99, float(v))) for k, v in model_probs.items()
                   if v is not None and not math.isnan(float(v))}

        if len(cleaned) < _MIN_MODELS:
            # Fall back to simple average with warning
            if cleaned:
                avg = sum(cleaned.values()) / len(cleaned)
            else:
                avg = 0.5
            notes.append(f"Only {len(cleaned)} model(s) — falling back to simple average")
            return EnsembleResult(
                weighted_prob=avg,
                raw_probs=cleaned,
                weights_used={k: 1.0 / len(cleaned) for k in cleaned} if cleaned else {},
                disagreement=0.0,
                confidence=0.3,
                n_models=len(cleaned),
                ttc_bucket=regime.ttc_bucket,
                vol_regime=regime.vol_regime,
                trend=regime.trend,
                notes=notes,
            )

        # Fetch base weights for this regime
        wkey = (regime.ttc_bucket, regime.vol_regime)
        base_w = dict(_BASE_WEIGHTS.get(wkey, _BASE_WEIGHTS[("1-6h", "normal")]))

        # Apply asset-specific neural weight bump for BTC/ETH (best trained)
        if regime.asset in ("BTC", "ETH") and "neural" in cleaned:
            if regime.ttc_bucket not in ("gt24h",):
                boost = 0.05
                base_w["neural"] = base_w.get("neural", 0.0) + boost
                # Redistribute by reducing diffusion
                base_w["diffusion"] = max(0.05, base_w.get("diffusion", 0.0) - boost)
                notes.append(f"neural weight +{boost:.0%} for {regime.asset}")

        # Apply trend-based weight multipliers before filtering to active models
        trend_mods = _TREND_WEIGHT_MODS.get(regime.trend, {})
        if trend_mods:
            for k in list(base_w):
                if k in trend_mods:
                    base_w[k] = base_w[k] * trend_mods[k]
            notes.append(f"trend={regime.trend} weight mods applied")

        # Only keep weights for models we actually have
        active_w = {k: base_w[k] for k in cleaned if k in base_w}

        # Fill in any models present but without an explicit weight
        for k in cleaned:
            if k not in active_w:
                active_w[k] = 0.05
                notes.append(f"model '{k}' not in weight table — assigned 5%")

        # Re-normalise
        total_w = sum(active_w.values())
        if total_w <= 0:
            active_w = {k: 1.0 / len(cleaned) for k in cleaned}
        else:
            active_w = {k: w / total_w for k, w in active_w.items()}

        # Weighted probability
        weighted_p = sum(cleaned[k] * active_w[k] for k in cleaned)

        # Disagreement (population std-dev of model probs)
        vals = list(cleaned.values())
        disagree = statistics.pstdev(vals) if len(vals) >= 2 else 0.0

        # Confidence score: start high, penalise disagreement
        conf = 1.0
        if disagree > _DISAGREE_THRESHOLD:
            penalty = min(
                _DISAGREE_MAX_PENALTY,
                (disagree - _DISAGREE_THRESHOLD) / _DISAGREE_THRESHOLD * _DISAGREE_MAX_PENALTY,
            )
            conf -= penalty
            notes.append(f"disagreement={disagree:.3f} > {_DISAGREE_THRESHOLD} -> conf -{penalty:.2f}")

        # Penalise if too few models
        if len(cleaned) == 2:
            conf *= 0.85
            notes.append("only 2 models -> conf x0.85")

        # Penalise explosive vol with trending mean-reversion mismatch
        if regime.vol_regime == "high" and regime.trend == "mean_reverting":
            conf *= 0.80
            notes.append("high-vol + mean_reverting -> conf x0.80")

        # Regime trend confidence (from classifier)
        trend_conf = getattr(regime, "trend_confidence", 1.0)
        if trend_conf < 0.40:
            conf *= 0.72
            notes.append(f"trend_confidence={trend_conf:.2f} < 0.40 -> conf x0.72")
        elif trend_conf < 0.60:
            conf *= 0.88
            notes.append(f"trend_confidence={trend_conf:.2f} moderate -> conf x0.88")

        # Data quality from regime snapshot
        dq = getattr(regime, "data_quality", "ok")
        if dq == "stale":
            conf *= 0.50
            notes.append("stale data -> conf x0.50")
        elif dq == "thin":
            conf *= 0.72
            notes.append("thin data -> conf x0.72")

        # Calibrator quality: fewer samples → less reliable probability mapping
        if calib_meta:
            cal_method = calib_meta.get("method", "identity")
            n_cal = int(calib_meta.get("n_samples", 0))
            if cal_method == "identity":
                conf *= 0.80
                notes.append("no calibrator fitted -> conf x0.80")
            elif n_cal < 10:
                conf *= 0.85
                notes.append(f"calibrator n={n_cal} (very sparse) -> conf x0.85")
            elif n_cal < 25:
                conf *= 0.93
                notes.append(f"calibrator n={n_cal} (thin) -> conf x0.93")

        conf = max(0.10, min(1.0, conf))

        return EnsembleResult(
            weighted_prob=round(weighted_p, 6),
            raw_probs=cleaned,
            weights_used=active_w,
            disagreement=round(disagree, 6),
            confidence=round(conf, 4),
            n_models=len(cleaned),
            ttc_bucket=regime.ttc_bucket,
            vol_regime=regime.vol_regime,
            trend=regime.trend,
            notes=notes,
        )
