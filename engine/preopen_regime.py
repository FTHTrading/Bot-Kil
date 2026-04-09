"""
engine/preopen_regime.py — Pre-open regime scoring
===================================================
Runs at 4:55 AM ET (warm-start) and on demand.  Aggregates momentum,
vol-regime classification, and live spread quality into a single
readiness score that tells the agent whether to trade at full size,
half size, or stand aside at open.

Usage
-----
    import asyncio
    from engine.preopen_regime import preopen_regime_score

    result = asyncio.run(preopen_regime_score("BTC"))
    print(result["trade_mode"])   # "full" | "half" | "no_trade"
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

# ── Spread quality thresholds (cents) ─────────────────────────────────────────
_SPREAD_TIGHT = 6     # ≤ 6 ¢ → "tight" (excellent)
_SPREAD_WIDE  = 14    # > 14 ¢ → "wide" (caution)

# ── Minimum orderbook depth (contracts) for a usable book ─────────────────────
_MIN_DEPTH    = 20

# ── Momentum alignment: how many timeframes must agree ────────────────────────
_ALIGNMENT_THRESHOLD = 0.60   # 60 % of non-zero timeframes in same direction

# ── Number of active market tickers to probe for spreads ─────────────────────
_SPREAD_SAMPLE_N = 4


async def preopen_regime_score(asset: str) -> dict:
    """
    Compute a pre-open regime snapshot for *asset* ("BTC" or "ETH").

    Returns a dict with:
        asset           str     — "BTC" or "ETH"
        trend           str     — "up" | "down" | "flat"
        vol_bucket      str     — "low" | "normal" | "high"
        momentum_alignment float — fraction of timeframes agreeing (0-1)
        mean_reversion_risk bool — True if momentum is stretched
        spread_env      str     — "tight" | "normal" | "wide"
        avg_spread_cents float  — average observed half-spread in cents
        depth_ok        bool    — True if orderbooks have usable depth
        confidence      float   — 0-1 composite confidence in the regime read
        trade_mode      str     — "full" | "half" | "no_trade"
        ts              str     — ISO-8601 UTC timestamp
        warnings        list[str]
    """
    warnings: list[str] = []

    # ── 1. Momentum signals ───────────────────────────────────────────────────
    mom_signals: dict = {}
    try:
        from engine.btc_momentum import get_momentum_signals
        mom_signals = await asyncio.get_event_loop().run_in_executor(
            None, get_momentum_signals, asset
        )
    except Exception as e:
        log.warning("[preopen_regime] get_momentum_signals failed: %s", e)
        warnings.append(f"momentum_fetch_failed: {e}")

    # ── 2. Regime classification ──────────────────────────────────────────────
    regime = None
    try:
        from engine.regime import classify_regime
        if mom_signals:
            regime = classify_regime(mom_signals, hours_to_close=24.0, asset=asset)
    except Exception as e:
        log.warning("[preopen_regime] classify_regime failed: %s", e)
        warnings.append(f"regime_classify_failed: {e}")

    trend      = getattr(regime, "trend",      "flat")    if regime else "flat"
    vol_bucket = getattr(regime, "vol_regime", "normal")  if regime else "normal"
    stale      = getattr(regime, "stale_data", False)     if regime else True

    if stale:
        warnings.append("stale_candle_data")

    # ── 3. Momentum alignment ─────────────────────────────────────────────────
    mom_alignment = _compute_alignment(mom_signals)
    mean_reversion_risk = _detect_exhaustion(mom_signals)

    # ── 4. Spread environment (sample live orderbooks) ────────────────────────
    avg_spread, depth_ok = await _sample_spread_env(asset, warnings)

    if avg_spread <= _SPREAD_TIGHT:
        spread_env = "tight"
    elif avg_spread <= _SPREAD_WIDE:
        spread_env = "normal"
    else:
        spread_env = "wide"

    # ── 5. Confidence score ───────────────────────────────────────────────────
    confidence = _compute_confidence(
        mom_alignment=mom_alignment,
        stale=stale,
        spread_env=spread_env,
        depth_ok=depth_ok,
    )

    # ── 6. Trade mode decision ────────────────────────────────────────────────
    trade_mode = _decide_trade_mode(
        vol_bucket=vol_bucket,
        spread_env=spread_env,
        depth_ok=depth_ok,
        mom_alignment=mom_alignment,
        mean_reversion_risk=mean_reversion_risk,
        confidence=confidence,
    )

    return {
        "asset":                asset,
        "trend":                trend,
        "vol_bucket":           vol_bucket,
        "momentum_alignment":   round(mom_alignment, 3),
        "mean_reversion_risk":  mean_reversion_risk,
        "spread_env":           spread_env,
        "avg_spread_cents":     round(avg_spread, 2),
        "depth_ok":             depth_ok,
        "confidence":           round(confidence, 3),
        "trade_mode":           trade_mode,
        "ts":                   datetime.now(timezone.utc).isoformat(),
        "warnings":             warnings,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _compute_alignment(mom: dict) -> float:
    """
    Fraction of nonzero momentum values that point in the same direction.
    Keys probed: mom_1m, mom_3m, mom_5m, mom_15m, combined_momentum.
    """
    keys = ["mom_1m", "mom_3m", "mom_5m", "mom_15m", "combined_momentum"]
    vals = [float(mom[k]) for k in keys if k in mom and mom[k] is not None]
    nonzero = [v for v in vals if abs(v) > 1e-9]
    if len(nonzero) < 2:
        return 0.5  # neutral when not enough data
    pos = sum(1 for v in nonzero if v > 0)
    neg = len(nonzero) - pos
    return max(pos, neg) / len(nonzero)


def _detect_exhaustion(mom: dict) -> bool:
    """
    Simple exhaustion heuristic:
    If 1-min momentum is > 3x the 15-min momentum in magnitude,
    assume the move is stretched and reversion risk is elevated.
    """
    m1  = abs(float(mom.get("mom_1m",  0) or 0))
    m15 = abs(float(mom.get("mom_15m", 0) or 0))
    if m15 < 1e-9:
        return False
    return m1 / m15 > 3.0


async def _sample_spread_env(asset: str, warnings: list) -> tuple[float, bool]:
    """
    Fetch orderbook for a handful of current-day BTC/ETH Kalshi market tickers
    and compute the average half-spread.  Returns (avg_spread_cents, depth_ok).
    """
    try:
        from data.feeds.kalshi import get_active_markets, get_market_orderbook
    except ImportError:
        warnings.append("kalshi_import_failed")
        return 8.0, True   # assume neutral

    try:
        markets = await asyncio.get_event_loop().run_in_executor(
            None, get_active_markets, asset
        )
        tickers = [m["ticker"] for m in markets[:_SPREAD_SAMPLE_N]]
    except Exception as e:
        warnings.append(f"active_markets_fetch_failed: {e}")
        return 8.0, True

    spreads: list[float] = []
    depths: list[int]   = []
    for ticker in tickers:
        try:
            ob = await asyncio.get_event_loop().run_in_executor(
                None, get_market_orderbook, ticker
            )
            yes_levels = ob.get("yes", [])
            no_levels  = ob.get("no",  [])
            if yes_levels and no_levels:
                best_yes_bid = max((p for p, _ in yes_levels), default=0)
                best_no_bid  = max((p for p, _ in no_levels),  default=0)
                yes_ask      = 100 - best_no_bid
                spread       = max(0, yes_ask - best_yes_bid)
                spreads.append(spread)
                depth_yes = sum(s for _, s in yes_levels[:3])
                depth_no  = sum(s for _, s in no_levels[:3])
                depths.append(depth_yes + depth_no)
        except Exception as e:
            log.debug("[preopen_regime] orderbook fetch %s: %s", ticker, e)

    if not spreads:
        warnings.append("no_orderbook_data")
        return 10.0, False

    avg_spread = sum(spreads) / len(spreads)
    depth_ok   = (sum(depths) / len(depths)) >= _MIN_DEPTH if depths else False
    return avg_spread, depth_ok


def _compute_confidence(
    mom_alignment: float,
    stale: bool,
    spread_env: str,
    depth_ok: bool,
) -> float:
    score = 1.0
    if stale:
        score -= 0.25
    if mom_alignment < _ALIGNMENT_THRESHOLD:
        score -= 0.20
    if spread_env == "wide":
        score -= 0.20
    elif spread_env == "normal":
        score -= 0.05
    if not depth_ok:
        score -= 0.15
    return max(0.0, min(1.0, score))


def _decide_trade_mode(
    vol_bucket: str,
    spread_env: str,
    depth_ok: bool,
    mom_alignment: float,
    mean_reversion_risk: bool,
    confidence: float,
) -> str:
    """
    no_trade  — too dangerous
    half      — tradeable but cautious
    full      — optimal environment
    """
    if not depth_ok and spread_env == "wide":
        return "no_trade"
    if vol_bucket == "high" and spread_env == "wide":
        return "no_trade"
    if confidence < 0.40:
        return "no_trade"
    if mean_reversion_risk and vol_bucket == "high":
        return "half"
    if spread_env == "wide" or vol_bucket == "high":
        return "half"
    if mom_alignment < _ALIGNMENT_THRESHOLD:
        return "half"
    if confidence >= 0.70:
        return "full"
    return "half"
