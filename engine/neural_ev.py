"""
neural_ev.py — Neural-net edge model for Kalshi 15-minute directional markets
==============================================================================

Drop-in replacement for intraday_ev.py.  Same output dict shape, same gates,
same Kelly sizing.  Model inference replaces _blend_prob().

If models/kalshi_net.pt does not exist, falls back to the math model.

Usage:
    from engine.neural_ev import neural_edge_picks
    picks = neural_edge_picks(markets, momentum_signals, bankroll)
"""
from __future__ import annotations

import json
import math
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# ── Re-use all constants + helpers from intraday_ev ───────────────────────────
from engine.intraday_ev import (
    _KELLY_FRACTION,
    _MIN_EDGE,
    _MIN_BET_PRICE,
    _MAX_BET_PRICE,
    _FEE_RATE,
    _DAILY_VOL,
    _kelly_stake,
    _position_prob,
    _blend_prob,
)

# ── Neural model (lazy-loaded so import doesn't fail if torch is missing) ─────
_model = None
_model_loaded = False
_neural_first_call = True  # log device on first successful inference

_PRED_LOG = Path(__file__).parent.parent / "logs" / "predictions.jsonl"


def _write_pred_log(entry: dict):
    """Append a prediction audit record to logs/predictions.jsonl."""
    try:
        _PRED_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(_PRED_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _get_neural_model():
    """Lazy-load model on first call. Returns None if unavailable."""
    global _model, _model_loaded
    if _model_loaded:
        return _model
    _model_loaded = True
    try:
        from engine.neural_model import get_model
        _model = get_model()
        if _model is not None:
            log.info("[neural_ev] KalshiNet loaded — using neural predictions")
        else:
            log.warning("[neural_ev] models/kalshi_net.pt not found — falling back to math model")
    except ImportError as e:
        log.warning(f"[neural_ev] PyTorch not available ({e}) — falling back to math model")
        _model = None
    return _model


def _neural_prob(
    sig: dict,
    floor: float,
    t_min: float,
    asset: str,
) -> tuple[float, bool]:
    """
    Run KalshiNet inference for one market snapshot.

    Returns (probability, used_neural) where used_neural=False means we fell back
    to the math model blend.
    """
    model = _get_neural_model()
    if model is None:
        return None, False

    try:
        from engine.neural_model import predict_prob
        import torch

        global _neural_first_call
        current  = sig.get("current", floor)
        gap_pct = (current - floor) / floor * 100 if floor > 0 else 0.0
        # Use wall-clock UTC hour — sig doesn't carry hour_utc
        hour_utc = datetime.now(timezone.utc).hour

        row = {
            "gap_pct":       gap_pct,
            "mom_1m":        sig.get("mom_1m", 0.0),
            "mom_3m":        sig.get("mom_3m", 0.0),
            "mom_5m":        sig.get("mom_5m", 0.0),
            "mom_15m":       sig.get("mom_15m", 0.0),
            "realized_vol":  sig.get("realized_vol", 0.0),
            "t_remaining":   t_min,
            "hour_utc":      hour_utc,
            "trend":         sig.get("trend", "flat"),
            "asset":         asset,
        }
        # IMPORTANT: pass the feature dict directly — predict_prob calls encode_features internally
        prob = predict_prob(model, row, asset)
        if _neural_first_call:
            _neural_first_call = False
            dev = next(model.parameters()).device
            log.info(f"[neural_ev] First inference on device={dev}  asset={asset}  prob={prob:.4f}")
            print(f"  [neural_ev] Neural inference active on {dev}")
        return prob, True
    except Exception as e:
        log.warning(f"[neural_ev] inference error ({type(e).__name__}: {e}) — falling back to math model")
        return None, False


# ── Main pick generator ───────────────────────────────────────────────────────

def neural_edge_picks(
    markets: list[dict],
    momentum: dict[str, dict],
    bankroll: float = 10_000.0,
    min_edge: float = _MIN_EDGE,
) -> list[dict]:
    """
    Evaluate current 15-min Kalshi directional markets using KalshiNet.

    Parameters
    ----------
    markets  : output of kalshi_intraday.get_intraday_markets()
    momentum : output of btc_momentum.get_momentum_signals()
    bankroll : total bankroll in USD
    min_edge : minimum net-of-fee edge to place a bet

    Returns
    -------
    list of pick dicts sorted by edge descending (same shape as intraday_ev)
    """
    picks = []

    for m in markets:
        asset   = m["asset"]
        floor   = m["floor_strike"]
        yes_ask = m["yes_ask"]
        no_ask  = m["no_ask"]
        t_min   = m["minutes_remaining"]

        sig     = momentum.get(asset, {})
        current = sig.get("current", floor)
        if not current or current <= 0:
            continue

        # ── Momentum helpers (shared with math model) ──────────────────────
        mom_5m  = sig.get("mom_5m", 0.0)
        mom_15m = sig.get("mom_15m", 0.0)
        mom_1m  = sig.get("mom_1m", 0.0)
        mom_3m  = sig.get("mom_3m", 0.0)
        trend   = sig.get("trend", "flat")

        if t_min <= 3.0 and (mom_1m != 0.0 or mom_3m != 0.0):
            eff_mom5  = 0.50 * mom_1m + 0.30 * mom_3m + 0.20 * mom_5m
            eff_mom15 = mom_5m
        else:
            eff_mom5  = mom_5m
            eff_mom15 = mom_15m

        combined_mom = 0.70 * eff_mom5 + 0.30 * eff_mom15

        # ── Gap gate (time-adaptive) ────────────────────────────────────────
        gap_pct = (current - floor) / floor * 100 if floor > 0 else 0.0
        if t_min <= 2.0:
            gap_thresh = 0.02
        elif t_min >= 5.0:
            gap_thresh = 0.08
        else:
            frac = (t_min - 2.0) / 3.0
            gap_thresh = 0.02 + frac * (0.08 - 0.02)
        if abs(gap_pct) < gap_thresh:
            continue

        # ── Probability ────────────────────────────────────────────────────
        model_prob, used_neural = _neural_prob(sig, floor, t_min, asset)

        if model_prob is None:
            # Math model fallback
            default_vol  = _DAILY_VOL.get(asset, 0.05)
            vol_floor    = default_vol * 0.15
            realized_vol = sig.get("realized_vol", 0.0)
            if realized_vol and realized_vol > 0.00005:
                daily_vol = max(realized_vol * (288 ** 0.5), vol_floor)
            else:
                daily_vol = default_vol

            from engine.intraday_ev import _momentum_prob
            p_pos  = _position_prob(current, floor, t_min, daily_vol)
            p_mom  = _momentum_prob(eff_mom5, eff_mom15, trend, t_min)
            model_prob = _blend_prob(p_pos, p_mom, t_min)
            p_pos_disp = round(p_pos, 4)
            p_mom_disp = round(p_mom, 4)
        else:
            p_pos_disp = None
            p_mom_disp = None

        # ── Edge ────────────────────────────────────────────────────────────
        edge_yes = model_prob - yes_ask - _FEE_RATE
        edge_no  = (1.0 - model_prob) - no_ask - _FEE_RATE

        if edge_yes >= edge_no and edge_yes >= min_edge:
            side, edge, bet_price, our_prob = "YES", edge_yes, yes_ask, model_prob
        elif edge_no > edge_yes and edge_no >= min_edge:
            side, edge, bet_price, our_prob = "NO", edge_no, no_ask, 1.0 - model_prob
        else:
            continue

        # Price gates
        if bet_price < _MIN_BET_PRICE:
            continue
        if bet_price > _MAX_BET_PRICE:
            continue

        # Contrarian ban (V6 post-mortem)
        if trend == "up" and side == "NO":
            continue
        if trend == "down" and side == "YES":
            continue
        if trend == "flat":
            if side == "YES" and combined_mom < 0:
                continue
            if side == "NO" and combined_mom > 0:
                continue

        # Kelly stake
        stake = _kelly_stake(our_prob, bet_price, bankroll)
        if stake <= 0:
            continue

        # EV
        ev_pct = (our_prob * (1.0 / bet_price - 1.0) - (1.0 - our_prob)) * 100

        # Confidence score
        _gap_s  = min(abs(gap_pct) / 0.20, 1.0)
        _mom_s  = min(abs(combined_mom) * 100 / 0.10, 1.0)
        _tr_s   = 1.0 if trend in ("up", "down") else 0.3
        _t_s    = 1.0 - min(t_min, 10) / 10.0
        confidence = round((_gap_s * 0.35 + _mom_s * 0.25 + _tr_s * 0.20 + _t_s * 0.20) * 100)

        if edge >= 0.10 and confidence >= 60:
            verdict = "STRONG VALUE"
        elif edge >= 0.06:
            verdict = "VALUE"
        else:
            verdict = "MARGINAL"

        signal_str = (
            f"{'neural' if used_neural else 'math'}  "
            f"gap={gap_pct:+.3f}%  5m={mom_5m*100:+.3f}%  trend={trend}  conf={confidence}"
        )

        # ── Prediction audit log (every pick, win or lose) ──────────────────────
        _write_pred_log({
            "ts":           datetime.now(timezone.utc).isoformat(),
            "ticker":       m["ticker"],
            "asset":        asset,
            "side":         side,
            "prob":         round(model_prob, 4),
            "edge":         round(edge, 4),
            "confidence":   confidence,
            "used_neural":  used_neural,
            "verdict":      verdict,
            "features": {
                "gap_pct":   round(gap_pct, 4),
                "mom_5m":    round(mom_5m * 100, 4),
                "mom_15m":   round(mom_15m * 100, 4),
                "trend":     trend,
                "t_min":     round(t_min, 1),
                "price":     round(current, 2),
            },
        })

        picks.append({
            "sport":             "INTRADAY",
            "event":             m["title"],
            "pick":              f"{asset} {side} (15m directional)",
            "market":            m["ticker"],
            "book":              "kalshi",
            "decimal_odds":      round(1.0 / bet_price, 4),
            "american_odds":     int((1.0/bet_price - 1)*100) if bet_price <= 0.5 else int(-100/(1.0/bet_price - 1)),
            "our_prob":          round(our_prob * 100, 1),
            "implied_prob":      round(bet_price * 100, 1),
            "edge_pct":          round(edge * 100, 2),
            "ev_pct":            round(ev_pct, 1),
            "recommended_stake": round(stake, 2),
            "verdict":           verdict,
            "close_time":        m["close_time"],
            "minutes_remaining": round(t_min, 1),
            "side":              side.lower(),
            "intraday_meta": {
                "asset":          asset,
                "floor_strike":   floor,
                "current_price":  current,
                "gap_pct":        round(gap_pct, 4),
                "prob_position":  p_pos_disp,
                "prob_momentum":  p_mom_disp,
                "prob_model":     round(model_prob, 4),
                "used_neural":    used_neural,
                "mom_5m_pct":    round(mom_5m * 100, 4),
                "mom_15m_pct":   round(mom_15m * 100, 4),
                "trend":          trend,
                "confidence":     confidence,
                "signal_str":     signal_str,
            },
        })

    picks.sort(key=lambda p: p["edge_pct"], reverse=True)
    return picks
