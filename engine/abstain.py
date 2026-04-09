"""
engine/abstain.py — Abstention decision engine
===============================================
Determines whether the agent should skip a candidate pick entirely.
This is the "ruthless" layer that sits between the ensemble output and
the order execution path.  A single affirmative abstain decision blocks
the bet regardless of what the probability models say.

Design principle: it is always cheaper to miss a trade than to take a
bad one.  Abstain early; abstain loudly; log the reason.

Usage:
    from engine.abstain import should_abstain, NoTradeReason

    abstain, reason, detail = should_abstain(
        pick=pick_dict,
        regime=regime_snapshot,
        ensemble=ensemble_result,
        calibrated_edge_pct=edge_pct,
        portfolio=open_positions,
    )
    if abstain:
        log.info("[abstain] %s — %s", reason.value, detail)
        return
"""
from __future__ import annotations

import logging
from enum import Enum
from typing import Optional

from engine.regime import RegimeSnapshot
from engine.ensemble import EnsembleResult

log = logging.getLogger(__name__)

# ── Thresholds ──────────────────────────────────────────────────────────────

# Minimum calibrated edge to place a bet (after all calibration)
_MIN_EDGE_PCT_INTRADAY = 0.10    # 10% intraday
_MIN_EDGE_PCT_DAILY    = 0.05    # 5%  daily / multi-hour

# Ensemble confidence floor
_MIN_CONFIDENCE        = 0.40

# Model disagreement ceiling (std-dev of raw model probs)
_MAX_DISAGREEMENT      = 0.15

# Trend confidence floor: below this and intraday → abstain (noise zone)
_TREND_CONF_NOISE      = 0.35

# Mean-reverting regime requires higher confidence to trade
_MR_MIN_CONFIDENCE     = 0.60

# Maximum correlation exposure: if this many open bets already share the
# same asset, skip the new one to avoid correlated loss.
_MAX_CORRELATED_BETS   = 2

# Minimum ensemble models available
_MIN_MODELS            = 2


# ── No-trade reason enum ────────────────────────────────────────────────────

class NoTradeReason(Enum):
    STALE_DATA          = "stale_data"
    THIN_DATA           = "thin_data"
    LOW_CONFIDENCE      = "low_confidence"
    MODEL_DISAGREEMENT  = "model_disagreement"
    INSUFFICIENT_MODELS = "insufficient_models"
    EDGE_BELOW_MIN      = "edge_below_min"
    FLAT_TREND_INTRADAY = "flat_trend_intraday"
    NOISE_ZONE          = "noise_zone"
    REGIME_UNFAVORABLE  = "regime_unfavorable"
    CORRELATED_EXPOSURE = "correlated_exposure"


# ── Core abstention logic ────────────────────────────────────────────────────

def should_abstain(
    pick: dict,
    regime: RegimeSnapshot,
    ensemble: EnsembleResult,
    calibrated_edge_pct: float,
    portfolio: Optional[list[dict]] = None,
) -> tuple[bool, Optional[NoTradeReason], str]:
    """
    Decide whether to skip a candidate pick.

    Parameters
    ----------
    pick                : pick dict from neural_edge_picks or intraday picks.
                          Must contain at least: {"asset": str, "ttc": float}
    regime              : RegimeSnapshot for this market.
    ensemble            : EnsembleResult from WeightedEnsemble.run().
    calibrated_edge_pct : edge as a fraction AFTER calibration (e.g. 0.12 = 12%).
    portfolio           : list of current open position dicts; each must
                          contain {"asset": str, "status": str}.
                          Pass None to skip correlation check.

    Returns
    -------
    (abstain: bool, reason: NoTradeReason | None, detail: str)
    """
    asset     = pick.get("asset", "unknown")
    ttc_hours = float(pick.get("ttc") or pick.get("hours_to_close") or 24.0)
    is_intraday = ttc_hours <= 6.0

    # ── 1. Data quality gates (highest priority) ──────────────────────────
    if regime.data_quality == "stale":
        return True, NoTradeReason.STALE_DATA, (
            f"asset={asset} data_quality=stale — no reliable price signal"
        )

    if regime.data_quality == "thin" and is_intraday:
        return True, NoTradeReason.THIN_DATA, (
            f"asset={asset} data_quality=thin on intraday pick — insufficient candles"
        )

    # ── 2. Model availability ─────────────────────────────────────────────
    if ensemble.n_models < _MIN_MODELS:
        return True, NoTradeReason.INSUFFICIENT_MODELS, (
            f"only {ensemble.n_models} model(s) available — need {_MIN_MODELS}"
        )

    # ── 3. Model disagreement ─────────────────────────────────────────────
    if ensemble.disagreement > _MAX_DISAGREEMENT:
        return True, NoTradeReason.MODEL_DISAGREEMENT, (
            f"model std-dev={ensemble.disagreement:.3f} > {_MAX_DISAGREEMENT:.3f}"
            f" ({ensemble.n_models} models)"
        )

    # ── 4. Ensemble confidence ────────────────────────────────────────────
    if ensemble.confidence < _MIN_CONFIDENCE:
        return True, NoTradeReason.LOW_CONFIDENCE, (
            f"ensemble confidence={ensemble.confidence:.3f} < {_MIN_CONFIDENCE}"
        )

    # ── 5. Calibrated edge floor ──────────────────────────────────────────
    min_edge = _MIN_EDGE_PCT_INTRADAY if is_intraday else _MIN_EDGE_PCT_DAILY
    if calibrated_edge_pct < min_edge:
        return True, NoTradeReason.EDGE_BELOW_MIN, (
            f"calibrated edge={calibrated_edge_pct*100:.1f}% < {min_edge*100:.0f}%"
            f" ({'intraday' if is_intraday else 'daily'} threshold)"
        )

    # ── 6. Trend / regime gates (only meaningful for intraday) ────────────
    if is_intraday:
        # Flat market with low trend confidence → pure noise
        if regime.trend == "flat" and regime.trend_confidence < _TREND_CONF_NOISE:
            return True, NoTradeReason.FLAT_TREND_INTRADAY, (
                f"flat regime, trend_confidence={regime.trend_confidence:.2f} < {_TREND_CONF_NOISE}"
                " — likely noise"
            )

        # Any trend with very weak confidence → noise zone
        if regime.trend_confidence < _TREND_CONF_NOISE:
            return True, NoTradeReason.NOISE_ZONE, (
                f"trend_confidence={regime.trend_confidence:.2f} < {_TREND_CONF_NOISE}"
                f" (trend={regime.trend}) — signal ambiguous"
            )

    # Mean-reverting: need higher confidence before trusting a directional bet
    if regime.trend == "mean_reverting" and ensemble.confidence < _MR_MIN_CONFIDENCE:
        return True, NoTradeReason.REGIME_UNFAVORABLE, (
            f"mean_reverting regime but confidence={ensemble.confidence:.3f} < {_MR_MIN_CONFIDENCE}"
            " — too uncertain to bet against prevailing reversion"
        )

    # ── 7. Portfolio correlation check ────────────────────────────────────
    if portfolio:
        open_same_asset = sum(
            1 for pos in portfolio
            if pos.get("asset") == asset and pos.get("status") not in ("settled", "closed")
        )
        if open_same_asset >= _MAX_CORRELATED_BETS:
            return True, NoTradeReason.CORRELATED_EXPOSURE, (
                f"already {open_same_asset} open bets on {asset} "
                f"(limit={_MAX_CORRELATED_BETS}) — correlated exposure"
            )

    return False, None, "all checks passed"


def abstain_summary(reason: Optional[NoTradeReason], detail: str) -> dict:
    """Serialisable abstain summary for audit log / explain output."""
    if reason is None:
        return {"abstained": False, "reason": None, "detail": detail}
    return {
        "abstained": True,
        "reason":    reason.value,
        "detail":    detail,
    }
