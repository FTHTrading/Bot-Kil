"""
engine/regime.py — Market regime classification
================================================
Classifies each betting opportunity into a regime snapshot used by
the ensemble, calibration, and abstain modules.

Regime dimensions:
  - vol_regime   : "low" | "normal" | "high"  (vs historical vol)
  - trend        : "trending" | "flat" | "mean_reverting"
  - ttc_bucket   : "le15m" | "le1h" | "1-6h" | "6-24h" | "gt24h"
  - asset        : "BTC" | "ETH" | "SOL" | "DOGE" | "XRP"

Usage:
    from engine.regime import classify_regime, RegimeSnapshot
    snap = classify_regime(momentum_signals, hours_to_close, asset)
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# ── Per-asset config (thresholds tuned to each asset's microstructure) ──────
# BTC/ETH: tighter vol range, finer momentum thresholds
# DOGE/XRP: wider vol range, coarser momentum before calling a trend

from dataclasses import dataclass as _dc

@_dc
class _AssetConfig:
    vol_low:        float   # daily vol < this → "low" regime
    vol_high:       float   # daily vol > this → "high" regime
    trend_threshold: float  # |momentum| ≥ this → trending
    flat_threshold:  float  # |momentum| < this → flat
    mr_rsi_low:     float   # RSI ≤ this → MR signal (oversold)
    mr_rsi_high:    float   # RSI ≥ this → MR signal (overbought)
    mr_bollpb_low:  float   # Bollinger %B ≤ this → MR signal
    mr_bollpb_high: float   # Bollinger %B ≥ this → MR signal

_ASSET_CONFIGS: dict[str, _AssetConfig] = {
    "BTC":     _AssetConfig(0.022, 0.052, 0.0005, 0.0002, 22.0, 78.0, 0.15, 0.85),
    "ETH":     _AssetConfig(0.025, 0.058, 0.0006, 0.0002, 22.0, 78.0, 0.15, 0.85),
    "SOL":     _AssetConfig(0.035, 0.080, 0.0008, 0.0003, 20.0, 80.0, 0.12, 0.88),
    "DOGE":    _AssetConfig(0.045, 0.100, 0.0010, 0.0004, 20.0, 80.0, 0.12, 0.88),
    "XRP":     _AssetConfig(0.030, 0.075, 0.0008, 0.0003, 20.0, 80.0, 0.12, 0.88),
    "_default":_AssetConfig(0.025, 0.055, 0.0005, 0.0002, 22.0, 78.0, 0.15, 0.85),
}

def _cfg(asset: str) -> _AssetConfig:
    return _ASSET_CONFIGS.get(asset, _ASSET_CONFIGS["_default"])


@dataclass
class RegimeSnapshot:
    """Fully serialisable regime classification for one market opportunity."""
    asset:            str
    vol_regime:       str           # "low" | "normal" | "high"
    trend:            str           # "trending" | "flat" | "mean_reverting"
    ttc_bucket:       str           # time-to-close bucket
    hours_to_close:   float
    # raw signals for downstream weights
    realized_vol_daily: float       # annualised daily vol (fraction)
    combined_momentum:  float       # weighted 5m+15m momentum (fraction)
    rsi:              Optional[float]   # None if not computed
    bollinger_pb:     Optional[float]   # Bollinger %B, None if not computed
    reason:           str           # human-readable classification reason
    # quality + confidence fields
    trend_confidence: float = 1.0   # 0–1 confidence in the trend label
    data_quality:     str   = "ok"  # "ok" | "thin" | "stale"
    stale_data:       bool  = False # True → downstream should abstain

    def to_dict(self) -> dict:
        return {
            "asset":               self.asset,
            "vol_regime":          self.vol_regime,
            "trend":               self.trend,
            "trend_confidence":    round(self.trend_confidence, 3),
            "ttc_bucket":          self.ttc_bucket,
            "hours_to_close":      round(self.hours_to_close, 3),
            "realized_vol_daily":  round(self.realized_vol_daily, 5),
            "combined_momentum":   round(self.combined_momentum, 6),
            "rsi":                 round(self.rsi, 1) if self.rsi is not None else None,
            "bollinger_pb":        round(self.bollinger_pb, 3) if self.bollinger_pb is not None else None,
            "data_quality":        self.data_quality,
            "stale_data":          self.stale_data,
            "reason":              self.reason,
        }

    @property
    def key(self) -> tuple[str, str, str, str]:
        """(asset, vol_regime, ttc_bucket, trend) — calibration/ensemble routing key.
        Trend is included so trending and mean-reverting markets are never
        blended in the same calibration bucket.
        """
        return (self.asset, self.vol_regime, self.ttc_bucket, self.trend)


def _ttc_bucket(hours: float) -> str:
    if hours <= 0.25:
        return "le15m"
    if hours <= 1.0:
        return "le1h"
    if hours <= 6.0:
        return "1-6h"
    if hours <= 24.0:
        return "6-24h"
    return "gt24h"


def _annualised_vol(realized_vol_per5m: float) -> float:
    """Convert 5-minute realised vol to daily annualised vol fraction."""
    # There are 288 5-min bars in 24 hours.
    # daily_vol = 5m_vol * sqrt(288)
    if realized_vol_per5m <= 0:
        return 0.0
    return realized_vol_per5m * math.sqrt(288)


def _classify_vol(daily_vol: float, cfg: _AssetConfig) -> str:
    if daily_vol < cfg.vol_low:
        return "low"
    if daily_vol > cfg.vol_high:
        return "high"
    return "normal"


def _compute_bollinger_pb(closes: list[float], current: float) -> Optional[float]:
    """Bollinger Band %B = (price − lower) / (upper − lower). Returns None if insufficient data."""
    if len(closes) < 4 or current <= 0:
        return None
    n   = min(len(closes), 8)
    sma = sum(closes[-n:]) / n
    std = math.sqrt(sum((x - sma) ** 2 for x in closes[-n:]) / n)
    if std == 0:
        return 0.5
    upper = sma + 2 * std
    lower = sma - 2 * std
    if upper == lower:
        return 0.5
    pb = (current - lower) / (upper - lower)
    return max(0.0, min(1.0, pb))


def _assess_data_quality(rv_5m: float, closes: list[float]) -> str:
    """"ok" | "thin" | "stale" depending on data richness."""
    if not closes:
        return "stale"
    if len(closes) < 3 and rv_5m <= 1e-7:
        return "thin"
    if rv_5m <= 1e-7 and len(closes) < 6:
        return "thin"
    return "ok"


def _classify_trend(
    mom_5m: float,
    mom_15m: float,
    trend_str: str,
    closes: list[float],
    rsi: Optional[float],
    bollinger_pb: Optional[float],
    cfg: _AssetConfig,
) -> tuple[str, float]:
    """
    Classify trend state using multi-factor evidence.
    Returns (label, confidence) where confidence is 0–1.

    Priority:
    1. Multi-factor mean-reversion (RSI + Bollinger %B + momentum divergence)
    2. Momentum magnitude (with candle-slope noise filter)
    3. Feed-supplied label as weak tiebreaker
    """
    combined = 0.70 * mom_5m + 0.30 * mom_15m

    # ── 1. Mean-reversion: score from multiple signals ───────────────────────
    mr_score = 0.0

    # RSI extremes
    if rsi is not None:
        if rsi <= cfg.mr_rsi_low or rsi >= cfg.mr_rsi_high:
            mr_score += 0.40
        elif rsi <= 30 or rsi >= 70:
            mr_score += 0.20

    # Bollinger %B extremes
    if bollinger_pb is not None:
        if bollinger_pb <= cfg.mr_bollpb_low or bollinger_pb >= cfg.mr_bollpb_high:
            mr_score += 0.40
        elif bollinger_pb <= 0.20 or bollinger_pb >= 0.80:
            mr_score += 0.20

    # Short-horizon direction divergence: 5m and 15m pulling opposite ways
    if (abs(mom_5m) >= cfg.trend_threshold and
            abs(mom_15m) >= cfg.trend_threshold and
            (mom_5m > 0) != (mom_15m > 0)):
        mr_score += 0.30

    # Threshold-proximity: price near recent S/R inflection (candle slope reversal)
    if len(closes) >= 6:
        recent_direction = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
        reversals = sum(
            1 for i in range(1, len(recent_direction))
            if (recent_direction[i] > 0) != (recent_direction[i - 1] > 0)
        )
        if reversals >= len(recent_direction) * 0.55:
            mr_score += 0.20   # choppy / mean-reverting candle pattern

    if mr_score >= 0.60:
        return "mean_reverting", min(mr_score, 1.0)

    # ── 2. Flat ──────────────────────────────────────────────────────────────
    if abs(combined) < cfg.flat_threshold:
        conf = 0.70
        if len(closes) >= 4:
            slope = (closes[-1] - closes[-4]) / (closes[-4] if closes[-4] != 0 else 1)
            if abs(slope) < cfg.flat_threshold:
                conf = 0.90
            elif abs(slope) >= cfg.trend_threshold:
                conf = 0.40   # candle slope contradicts flat momentum
        return "flat", conf

    # ── 3. Trending ──────────────────────────────────────────────────────────
    if abs(combined) >= cfg.trend_threshold:
        # Base confidence from momentum magnitude
        conf = min(abs(combined) / (cfg.trend_threshold * 3.0), 1.0)

        # RSI near exhaustion → reduce confidence
        if rsi is not None and (rsi > 65 or rsi < 35):
            conf *= 0.80

        # Candle-slope confirmation / contradiction
        if len(closes) >= 4:
            slope = (closes[-1] - closes[-4]) / (closes[-4] if closes[-4] != 0 else 1)
            if (slope > 0) == (combined > 0) and abs(slope) >= cfg.trend_threshold:
                conf = min(conf * 1.20, 1.0)   # confirmed
            elif (slope > 0) != (combined > 0):
                conf *= 0.75                    # contradicted

        return "trending", round(min(conf, 1.0), 3)

    # ── 4. Weak-signal tiebreaker ─────────────────────────────────────────────
    if trend_str in ("up", "down"):
        return "trending", 0.30
    return "flat", 0.40


def _compute_rsi(closes: list[float], periods: int = 14) -> Optional[float]:
    """RSI from recent close prices. Returns None if insufficient data."""
    if len(closes) < periods + 1:
        return None
    changes = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    relevant = changes[-periods:]
    gains  = [max(c, 0.0) for c in relevant]
    losses = [abs(min(c, 0.0)) for c in relevant]
    ag = sum(gains)  / len(gains)
    al = sum(losses) / len(losses)
    if al == 0:
        return 100.0
    rs = ag / al
    return round(100.0 - 100.0 / (1.0 + rs), 1)


def classify_regime(
    momentum_signals: dict,
    hours_to_close: float,
    asset: str,
    default_daily_vol: float | None = None,
) -> RegimeSnapshot:
    """
    Classify the market regime from live momentum signals.

    Parameters
    ----------
    momentum_signals : output from btc_momentum.get_momentum_signals()[asset]
                       Keys: mom_5m, mom_15m, realized_vol, trend, closes, current
    hours_to_close   : hours until market settlement
    asset            : "BTC" | "ETH" | "SOL" | "DOGE" | "XRP"
    default_daily_vol: fallback if realized_vol is 0
    """
    sig = momentum_signals or {}
    cfg = _cfg(asset)

    # Pull raw signals
    mom_5m  = float(sig.get("mom_5m",  0.0) or 0.0)
    mom_15m = float(sig.get("mom_15m", 0.0) or 0.0)
    rv_5m   = float(sig.get("realized_vol", 0.0) or 0.0)
    trend_s = str(sig.get("trend", "flat"))
    closes  = list(sig.get("closes") or [])
    current = float(sig.get("current", 0.0) or 0.0)
    combined = 0.70 * mom_5m + 0.30 * mom_15m

    # Vol
    from engine.intraday_ev import _DAILY_VOL as _DFLT_VOL
    fallback_vol = default_daily_vol or _DFLT_VOL.get(asset, 0.038)
    daily_vol = max(_annualised_vol(rv_5m), fallback_vol * 0.5) if rv_5m > 1e-6 else fallback_vol
    vol_regime = _classify_vol(daily_vol, cfg)

    # RSI
    rsi = _compute_rsi(closes) if closes else None

    # Bollinger %B
    bollinger_pb = _compute_bollinger_pb(closes, current) if closes else None

    # Trend with confidence
    trend, trend_conf = _classify_trend(mom_5m, mom_15m, trend_s, closes, rsi, bollinger_pb, cfg)

    # Time bucket
    bucket = _ttc_bucket(hours_to_close)

    # Data quality
    dq = _assess_data_quality(rv_5m, closes)
    is_stale = dq in ("stale",)

    # Human reason
    reason_parts = [
        f"vol={daily_vol*100:.2f}%/day({vol_regime})",
        f"mom={combined*100:+.3f}%",
        f"trend={trend}(conf={trend_conf:.2f})",
        f"ttc={hours_to_close:.2f}h({bucket})",
        f"dq={dq}",
    ]
    if rsi is not None:
        reason_parts.append(f"rsi={rsi:.1f}")
    if bollinger_pb is not None:
        reason_parts.append(f"pb={bollinger_pb:.2f}")
    reason = "  ".join(reason_parts)

    return RegimeSnapshot(
        asset=asset,
        vol_regime=vol_regime,
        trend=trend,
        ttc_bucket=bucket,
        hours_to_close=hours_to_close,
        realized_vol_daily=daily_vol,
        combined_momentum=combined,
        rsi=rsi,
        bollinger_pb=bollinger_pb,
        reason=reason,
        trend_confidence=trend_conf,
        data_quality=dq,
        stale_data=is_stale,
    )
