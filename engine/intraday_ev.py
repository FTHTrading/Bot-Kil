"""
intraday_ev.py — Edge model for Kalshi 15-minute directional markets
====================================================================

These markets resolve YES if:
    BRTI_average(last_60_sec_at_close) >= BRTI_average(last_60_sec_at_open)

i.e. "did BTC (or ETH/SOL/etc.) GO UP over this 15-minute window?"

The model blends two signals:
─────────────────────────────────────────────────────────────────────────────
1. POSITION SIGNAL  — where is current price vs floor_strike (opening price)?
   Uses log-normal diffusion with remaining-time σ.
   Stronger as market approaches expiry (current price IS the answer)

2. MOMENTUM SIGNAL  — what direction has price been moving recently?
   5-min and 15-min momentum from Binance candles.
   Stronger when plenty of time remains (market can still move with trend)

Blend:
   t_frac = minutes_remaining / 15                   # 1 = fresh, 0 = expired
   w_mom  = t_frac * 0.65                            # max 65% momentum at open
   w_pos  = 1 - w_mom                                # always a position component
   prob   = w_mom * prob_momentum + w_pos * prob_position

Then:
   edge  = our_prob - market_price
   Kelly = 0.10-Kelly (conservative; intraday = higher uncertainty per dollar)
─────────────────────────────────────────────────────────────────────────────

Usage:
    from engine.intraday_ev import intraday_edge_picks
    picks = intraday_edge_picks(markets, momentum_signals, bankroll)
"""
from __future__ import annotations

import math
from typing import Optional

# ---------------------------------------------------------------------------
# Per-asset daily σ (annualised base, same as crypto_ev.py)
# 5-min σ ≈ daily_σ * sqrt( 5 / 1440 )
# ---------------------------------------------------------------------------
_DAILY_VOL: dict[str, float] = {
    "BTC":  0.038,
    "ETH":  0.042,
    "SOL":  0.065,
    "DOGE": 0.082,
    "XRP":  0.058,
    "BNB":  0.042,
}

# Kelly fraction — smaller than daily picks (more noise, less model edge)
_KELLY_FRACTION = 0.10

# Minimum edge to surface a pick (3% = lower than daily 5% because
# intraday markets reset every 15 min — frequency compensates)
_MIN_EDGE = 0.04

# Momentum persistence factor αk:
# If BTC moved +1% in the last 5 min, our model says P(up) = 50 + α*100*mom_5m
# Empirical for crypto 15-min windows: ~55-60% of short momentum persists.
# α controls how strongly we shade probability from a momentum reading.
# Calibrated conservatively; raise if backtesting shows stronger persistence.
_MOMENTUM_K = 0.15    # 1% momentum move → +1.5% probability shift

# Maximum probability we'll predict (avoid over-confidence)
_PROB_CAP = 0.82
_PROB_FLOOR = 0.18


# ---------------------------------------------------------------------------
# Helpers (shared with crypto_ev — duplicated here to keep module standalone)
# ---------------------------------------------------------------------------

def _ndcdf(x: float) -> float:
    """Standard normal CDF (Abramowitz & Stegun approximation)."""
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    p = (
        0.319381530 * t
        - 0.356563782 * t**2
        + 1.781477937 * t**3
        - 1.821255978 * t**4
        + 1.330274429 * t**5
    )
    cdf = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x**2) * p
    return cdf if x >= 0 else 1.0 - cdf


def _position_prob(
    current: float,
    floor_strike: float,
    minutes_remaining: float,
    daily_vol: float,
) -> float:
    """
    P(final_price >= floor_strike) given current price, using log-normal diffusion.

    The settlement uses 60-second BRTI average, which smooths out the very last
    tick. We reduce the effective vol slightly (by sqrt(1-1/60) ≈ 0.992) to
    account for this smoothing. Effect is small but directionally correct.
    """
    t = minutes_remaining / (24 * 60)   # convert minutes → fraction of day
    if t <= 0:
        return 1.0 if current >= floor_strike else 0.0

    sigma = daily_vol * math.sqrt(t) * 0.992   # BRTI smoothing correction
    # Include negative drift (-0.5σ²t) from log-normal Ito correction
    drift = -0.5 * (daily_vol**2) * t
    if floor_strike <= 0:
        return 1.0

    d = (math.log(current / floor_strike) + drift) / sigma
    return _ndcdf(d)


def _momentum_prob(
    mom_5m: float,
    mom_15m: float,
    trend: str,
    minutes_remaining: float,
) -> float:
    """
    P(price goes up from now through expiry) based on momentum signals.

    - Combines 5-min (stronger) and 15-min (weaker) momentum
    - Applies a trend consistency bonus
    - Returns [_PROB_FLOOR, _PROB_CAP]
    """
    # Weighted momentum: 70% weight on 5-min (more recent = more predictive)
    combined_mom = 0.70 * mom_5m + 0.30 * mom_15m

    # Base probability from momentum persistence
    prob = 0.50 + _MOMENTUM_K * combined_mom * 100   # mom_5m in decimal, scale to %

    # Trend consistency bonus: if 3 consecutive candles agree, add a small boost
    trend_bonus = 0.025 if trend in ("up", "down") else 0.0
    if trend == "up":
        prob += trend_bonus
    elif trend == "down":
        prob -= trend_bonus

    # Momentum decays with time remaining (less predictive once 10+ min have passed)
    # At 15 min remaining: full signal. At 3 min remaining: half signal (already moved).
    decay = min(minutes_remaining, 15) / 15.0
    prob = 0.50 + (prob - 0.50) * decay

    return max(_PROB_FLOOR, min(_PROB_CAP, prob))


def _blend_prob(
    prob_position: float,
    prob_momentum: float,
    minutes_remaining: float,
) -> float:
    """
    Blend position and momentum signals weighted by time remaining.

    Early in the window: momentum drives prediction (price hasn't moved yet).
    Late in the window: position dominates (current vs floor_strike is the answer).
    """
    t_frac = min(minutes_remaining, 15) / 15.0   # 1=just opened, 0=expiring

    # Momentum weight peaks at 65% when market is freshly opened
    w_momentum = t_frac * 0.65
    w_position = 1.0 - w_momentum

    return w_momentum * prob_momentum + w_position * prob_position


def _kelly_stake(prob: float, market_price: float, bankroll: float) -> float:
    """Fractional Kelly stake in USD."""
    if market_price <= 0 or market_price >= 1:
        return 0.0
    b = (1.0 / market_price) - 1.0
    f = (prob * b - (1.0 - prob)) / b
    f = max(0.0, min(f, 0.25))
    # Cap at $150 per intraday bet regardless of bankroll
    return min(f * _KELLY_FRACTION * bankroll, 150.0)


# ---------------------------------------------------------------------------
# Main pick generator
# ---------------------------------------------------------------------------

def intraday_edge_picks(
    markets: list[dict],
    momentum: dict[str, dict],
    bankroll: float = 10_000.0,
    min_edge: float = _MIN_EDGE,
) -> list[dict]:
    """
    Evaluate current 15-min Kalshi directional markets and return edge picks.

    Parameters
    ----------
    markets  : output of kalshi_intraday.get_intraday_markets()
    momentum : output of btc_momentum.get_momentum_signals()
    bankroll : total bankroll in USD
    min_edge : minimum absolute edge to include (default 4%)

    Returns
    -------
    list of pick dicts, same structure as orchestrator sports picks,
    sorted by edge descending.
    """
    picks = []

    for m in markets:
        asset      = m["asset"]
        floor      = m["floor_strike"]
        yes_ask    = m["yes_ask"]
        no_ask     = m["no_ask"]
        t_min      = m["minutes_remaining"]

        # Get momentum signals for this asset
        sig = momentum.get(asset, {})
        current = sig.get("current", floor)   # fallback to floor if no data

        if not current or current <= 0:
            continue

        # ── Signal 1: Position (where is price now vs opening price?) ──
        # Use realized vol from momentum feed when available (more accurate than
        # the fixed daily vol assumption, which is typically 2-4x too high intraday).
        # realized_vol is per-5-min period; convert to equivalent daily vol for _position_prob.
        realized_vol_5m = sig.get("realized_vol", 0.0)
        if realized_vol_5m and realized_vol_5m > 0.00005:
            # 288 five-minute periods in a day → daily_vol = vol_5m * sqrt(288)
            daily_vol = realized_vol_5m * (288 ** 0.5)
        else:
            daily_vol = _DAILY_VOL.get(asset, 0.05)
        p_pos = _position_prob(current, floor, t_min, daily_vol)

        # ── Signal 2: Momentum (recent price direction) ──
        mom_5m  = sig.get("mom_5m", 0.0)
        mom_15m = sig.get("mom_15m", 0.0)
        trend   = sig.get("trend", "flat")
        p_mom = _momentum_prob(mom_5m, mom_15m, trend, t_min)

        # ── Blend ──
        model_prob = _blend_prob(p_pos, p_mom, t_min)

        # ── Edge calculation ──
        edge_yes = model_prob - yes_ask
        edge_no  = (1.0 - model_prob) - no_ask

        if edge_yes >= edge_no and edge_yes >= min_edge:
            side       = "YES"
            edge       = edge_yes
            bet_price  = yes_ask
            our_prob   = model_prob
        elif edge_no > edge_yes and edge_no >= min_edge:
            side       = "NO"
            edge       = edge_no
            bet_price  = no_ask
            our_prob   = 1.0 - model_prob
        else:
            continue

        # EV per dollar
        ev_pct = (our_prob * (1.0 / bet_price - 1.0) - (1.0 - our_prob)) * 100

        # Kelly stake — let executor floor to its MIN_SPEND_USD ($2)
        # For small bankrolls (<$200), Kelly produces tiny numbers; just pass them through.
        stake = _kelly_stake(our_prob, bet_price, bankroll)
        if stake <= 0:
            continue

        # Verdict
        if edge >= 0.10:
            verdict = "STRONG VALUE"
        elif edge >= 0.06:
            verdict = "VALUE"
        else:
            verdict = "MARGINAL"

        # ── Signal breakdown for display ──
        gap_pct = (current - floor) / floor * 100
        signal_str = (
            f"pos={p_pos:.2f}  mom={p_mom:.2f}  "
            f"gap={gap_pct:+.3f}%  5m={mom_5m*100:+.3f}%  trend={trend}"
        )

        picks.append({
            "sport":            "INTRADAY",
            "event":            m["title"],
            "pick":             f"{asset} {side} (15m directional)",
            "market":           m["ticker"],
            "book":             "kalshi",
            "decimal_odds":     round(1.0 / bet_price, 4),
            "american_odds":    int((1.0/bet_price - 1)*100) if bet_price <= 0.5 else int(-100/(1.0/bet_price - 1)),
            "our_prob":         round(our_prob * 100, 1),
            "implied_prob":     round(bet_price * 100, 1),
            "edge_pct":         round(edge * 100, 2),
            "ev_pct":           round(ev_pct, 1),
            "recommended_stake": round(stake, 2),
            "verdict":          verdict,
            "close_time":       m["close_time"],
            "minutes_remaining": round(t_min, 1),
            # Extra context for executor and display
            "side":             side.lower(),
            "intraday_meta": {
                "asset":          asset,
                "floor_strike":   floor,
                "current_price":  current,
                "gap_pct":        round(gap_pct, 4),
                "prob_position":  round(p_pos, 4),
                "prob_momentum":  round(p_mom, 4),
                "mom_5m_pct":    round(mom_5m * 100, 4),
                "mom_15m_pct":   round(mom_15m * 100, 4),
                "trend":          trend,
                "signal_str":     signal_str,
            },
        })

    picks.sort(key=lambda p: p["edge_pct"], reverse=True)
    return picks
