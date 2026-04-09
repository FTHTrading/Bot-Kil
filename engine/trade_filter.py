"""
engine/trade_filter.py — Single-entry-point trade gating pipeline
=================================================================
Chains regime → ensemble → calibration → abstain into one call that
returns a `TradeFilterResult`.  This is the only object `_tool_place_bet`
needs to consult before submitting an order.

Usage:
    from engine.trade_filter import TradeFilter, TradeFilterResult

    tf = TradeFilter()
    result = tf.evaluate(
        pick=pick_dict,
        momentum_signals=momentum_dict,
        model_probs={"diffusion": 0.62, "neural": 0.58, "technical": 0.55},
    )

    if not result.approved:
        return {"status": "skipped", "reason": result.abstain_reason, "detail": result.detail}

    # safe to place bet
    stake = result.recommended_stake
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Optional

from engine.regime      import classify_regime, RegimeSnapshot
from engine.ensemble    import WeightedEnsemble, EnsembleResult
from engine.calibration import CalibrationStore
from engine.abstain     import should_abstain, NoTradeReason, abstain_summary

log = logging.getLogger(__name__)

# ── Kelly sizing constants ────────────────────────────────────────────────────
_KELLY_FRACTION  = 0.10   # fractional Kelly
_MIN_STAKE       = 1.0    # minimum dollar stake
_MAX_STAKE       = 25.0   # hard cap per bet


# ── Result dataclass ─────────────────────────────────────────────────────────

@dataclass
class TradeFilterResult:
    """
    Everything the placing layer needs to know about whether and how to bet.

    Fields
    ------
    approved            : True if all gates passed and bet is recommended.
    abstain_reason      : NoTradeReason enum value (None when approved).
    detail              : Human-readable summary of the gate decision.
    regime              : RegimeSnapshot used in the decision.
    ensemble            : EnsembleResult (always populated).
    calibrated_prob     : Calibration-adjusted probability (0–1).
    calibrated_edge_pct : Expected edge after calibration and fees (fraction).
    raw_edge_pct        : Pre-calibration edge (fraction).
    recommended_stake   : Kelly-fraction stake in dollars (0 when not approved).
    calib_meta          : Calibration metadata dict.
    notes               : Collated notes from regime + ensemble.
    """
    approved:            bool
    abstain_reason:      Optional[NoTradeReason]
    detail:              str
    regime:              Optional[RegimeSnapshot]
    ensemble:            Optional[EnsembleResult]
    calibrated_prob:     float
    calibrated_edge_pct: float
    raw_edge_pct:        float
    recommended_stake:   float
    calib_meta:          dict = field(default_factory=dict)
    notes:               list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "approved":            self.approved,
            "abstain_reason":      self.abstain_reason.value if self.abstain_reason else None,
            "detail":              self.detail,
            "regime":              self.regime.to_dict() if self.regime else None,
            "ensemble":            self.ensemble.to_dict() if self.ensemble else None,
            "calibrated_prob":     round(self.calibrated_prob, 4),
            "calibrated_edge_pct": round(self.calibrated_edge_pct * 100, 2),
            "raw_edge_pct":        round(self.raw_edge_pct * 100, 2),
            "recommended_stake":   round(self.recommended_stake, 2),
            "calib_meta":          self.calib_meta,
            "notes":               self.notes,
        }


# ── Pipeline ─────────────────────────────────────────────────────────────────

class TradeFilter:
    """
    Stateless (except for CalibrationStore) pipeline that chains:
      classify_regime → WeightedEnsemble → CalibrationStore → should_abstain

    A single `TradeFilter` instance is safe to reuse across calls.
    """

    def __init__(self, calib_store: Optional[CalibrationStore] = None):
        self._ensemble = WeightedEnsemble()
        self._calib    = calib_store or CalibrationStore()

    def evaluate(
        self,
        pick: dict,
        momentum_signals: dict,
        model_probs: dict[str, float],
        bankroll: float = 200.0,
        fee_rate: float = 0.02,
        portfolio: Optional[list[dict]] = None,
        default_daily_vol: Optional[float] = None,
    ) -> TradeFilterResult:
        """
        Run the full gating pipeline for one candidate pick.

        Parameters
        ----------
        pick             : pick dict; must contain 'asset', 'side', 'yes_price'
                           (or 'price'), and 'ttc' (hours to close).
        momentum_signals : live momentum snapshot dict for this asset from
                           btc_momentum.get_momentum_signals()[asset].
        model_probs      : {model_name: probability}.  Valid model names:
                           "diffusion", "monte_carlo", "neural", "technical".
        bankroll         : current balance in dollars for Kelly sizing.
        fee_rate         : round-trip fee rate fraction (default 2%).
        portfolio        : list of open positions for correlation check.
        default_daily_vol: fallback daily vol if momentum feed is stale.
        """
        notes: list[str] = []
        asset     = pick.get("asset", "BTC")
        ttc       = float(pick.get("ttc") or pick.get("hours_to_close") or 24.0)
        price_raw = pick.get("yes_price") or pick.get("price") or 0.50
        bet_price = float(price_raw)

        # ── 1. Regime ──────────────────────────────────────────────────────
        try:
            regime = classify_regime(
                momentum_signals=momentum_signals,
                hours_to_close=ttc,
                asset=asset,
                default_daily_vol=default_daily_vol,
            )
            notes.append(f"regime: {regime.reason}")
        except Exception as exc:
            log.warning("[trade_filter] regime classification failed: %s", exc)
            return _reject(
                reason=None,
                detail=f"regime classification error: {exc}",
                notes=notes,
            )

        # ── 2. Ensemble ────────────────────────────────────────────────────
        try:
            ensemble = self._ensemble.run(
                model_probs=model_probs,
                regime=regime,
                calib_meta=None,   # calib_meta filled after calibration below
            )
            notes.extend(ensemble.notes)
        except Exception as exc:
            log.warning("[trade_filter] ensemble failed: %s", exc)
            return _reject(
                reason=None,
                detail=f"ensemble error: {exc}",
                notes=notes,
                regime=regime,
            )

        raw_prob     = ensemble.weighted_prob
        raw_edge_pct = raw_prob - bet_price - fee_rate

        # ── 3. Calibration ─────────────────────────────────────────────────
        try:
            cal_prob, calib_meta = self._calib.calibrate(
                raw_p=raw_prob,
                asset=asset,
                vol_regime=regime.vol_regime,
                ttc_bucket=regime.ttc_bucket,
                trend=regime.trend,
            )
        except Exception as exc:
            log.warning("[trade_filter] calibration failed: %s", exc)
            cal_prob   = raw_prob
            calib_meta = {"method": "identity", "reason": str(exc)}
            notes.append(f"calibration error — using raw prob: {exc}")

        # Re-run ensemble confidence update with calib_meta (non-destructive path)
        # We replicate the calib penalty inline rather than re-invoking ensemble.run()
        cal_edge_pct = cal_prob - bet_price - fee_rate
        notes.append(
            f"prob raw={raw_prob:.3f} cal={cal_prob:.3f}  "
            f"edge cal={cal_edge_pct*100:+.1f}%  "
            f"calib={calib_meta.get('method','?')}(n={calib_meta.get('n_samples',0)})"
        )

        # ── 4. Abstain gate ────────────────────────────────────────────────
        abstain, reason, detail = should_abstain(
            pick=pick,
            regime=regime,
            ensemble=ensemble,
            calibrated_edge_pct=cal_edge_pct,
            portfolio=portfolio,
        )

        if abstain:
            notes.append(f"ABSTAIN: {reason.value if reason else 'unknown'} — {detail}")
            log.info("[trade_filter] ABSTAIN %s/%s — %s", asset, regime.trend, detail)
            return TradeFilterResult(
                approved=False,
                abstain_reason=reason,
                detail=detail,
                regime=regime,
                ensemble=ensemble,
                calibrated_prob=cal_prob,
                calibrated_edge_pct=cal_edge_pct,
                raw_edge_pct=raw_edge_pct,
                recommended_stake=0.0,
                calib_meta=calib_meta,
                notes=notes,
            )

        # ── 5. Kelly stake ─────────────────────────────────────────────────
        stake = _kelly_stake(
            prob=cal_prob,
            price=bet_price,
            fee_rate=fee_rate,
            bankroll=bankroll,
        )
        notes.append(f"Kelly stake=${stake:.2f}")

        log.info(
            "[trade_filter] APPROVED %s/%s  cal_p=%.3f  edge=%.1f%%  stake=$%.2f",
            asset, regime.trend, cal_prob, cal_edge_pct * 100, stake,
        )
        return TradeFilterResult(
            approved=True,
            abstain_reason=None,
            detail="all gates passed",
            regime=regime,
            ensemble=ensemble,
            calibrated_prob=cal_prob,
            calibrated_edge_pct=cal_edge_pct,
            raw_edge_pct=raw_edge_pct,
            recommended_stake=stake,
            calib_meta=calib_meta,
            notes=notes,
        )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _kelly_stake(prob: float, price: float, fee_rate: float, bankroll: float) -> float:
    """Fractional Kelly criterion stake.  Returns 0 if edge is negative."""
    if price <= 0 or price >= 1:
        return 0.0
    # Payout: win (1-price)/price; lose -1 net (Kalshi model)
    b = (1.0 - price) / price
    q = 1.0 - prob
    kelly = (b * prob - q) / b
    if kelly <= 0:
        return 0.0
    stake = kelly * _KELLY_FRACTION * bankroll
    return max(_MIN_STAKE, min(_MAX_STAKE, round(stake, 2)))


def _reject(
    reason: Optional[NoTradeReason],
    detail: str,
    notes: list[str],
    regime: Optional[RegimeSnapshot] = None,
    ensemble: Optional[EnsembleResult] = None,
) -> TradeFilterResult:
    """Convenience constructor for rejected results with no probability data."""
    return TradeFilterResult(
        approved=False,
        abstain_reason=reason,
        detail=detail,
        regime=regime,
        ensemble=ensemble,
        calibrated_prob=0.0,
        calibrated_edge_pct=0.0,
        raw_edge_pct=0.0,
        recommended_stake=0.0,
        notes=notes,
    )
