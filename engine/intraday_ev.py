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

# Minimum edge to surface a pick — net of fees.  V5 restores 8% after post-mortem
# showed 5% allowed too many marginal bets (4/22 win rate).  Combined with 2% fee
# this means 10% gross edge minimum.
_MIN_EDGE = 0.08

# Minimum bet price — floor at 10¢.  Post-mortem: cheap contracts (4-9¢) look like
# huge edges but are lottery tickets the model can't reliably predict.
_MIN_BET_PRICE = 0.10

# Maximum bet price — cap at 65¢.  V6: expensive contracts (>65¢) have thin margins
# and need very high accuracy.  Two trend losses at 82¢ and 47¢ showed that even
# correct direction calls can lose when buying at high implied probability.
_MAX_BET_PRICE = 0.65

# Minimum |gap| (current vs floor_strike) to place a bet.
# Scales with time remaining — tighter filter early (noise), looser late (signal).
_MIN_GAP_PCT_EARLY = 0.08   # >5 min remaining: need 0.08% gap (stricter)
_MIN_GAP_PCT_LATE  = 0.02   # ≤2 min remaining: 0.02% is enough

# Kalshi fee rate — contracts are fee'd roughly 1-3¢ per contract on settlement.
# We estimate ~2% of contract value as average round-trip fee.
_FEE_RATE = 0.02

# Momentum persistence factor αk:
# Post-mortem V4: 0.15 was far too conservative — momentum barely registered,
# letting the position signal dominate with inflated vol.  In 15-min crypto
# windows, short momentum is the dominant signal.  0.50 means a 0.1% move
# shifts probability by 5 percentage points.
_MOMENTUM_K = 0.50    # 1% momentum move → +5% probability shift

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

    # V5 POST-MORTEM FIX: Momentum should STRENGTHEN near expiry, not decay.
    # With 3 min left, the direction is established — momentum is the strongest
    # signal.  With 15 min left, anything can happen — momentum is weaker.
    # Old: decay = t_min/15 (killed 80% of signal at 3 min — catastrophic)
    # New: inv_decay = 1 - t_min/15 (momentum at full strength near close)
    inv_decay = 1.0 - min(minutes_remaining, 15) / 15.0  # 0 min = 1.0, 15 min = 0.0
    # Clamp to [0.30, 1.0] so momentum always has at least 30% strength
    inv_decay = max(0.30, inv_decay)
    prob = 0.50 + (prob - 0.50) * inv_decay

    return max(_PROB_FLOOR, min(_PROB_CAP, prob))


def _blend_prob(
    prob_position: float,
    prob_momentum: float,
    minutes_remaining: float,
) -> float:
    """
    Blend position and momentum signals weighted by time remaining.

    V5 POST-MORTEM FIX: Reversed the weighting scheme.
    - Early: position is accurate (price ≈ open, gap is noise)
    - Late:  momentum dominates (direction is established, position unreliable
             because vol floor inflates recovery chances)

    At 3 min left:  momentum 75%, position 25%
    At 15 min left: momentum 25%, position 75%
    """
    t_frac = min(minutes_remaining, 15) / 15.0   # 1=just opened, 0=expiring

    # Momentum weight: HIGH near expiry (when direction is established)
    # LOW early (when price hasn't moved yet)
    w_momentum = 0.25 + (1.0 - t_frac) * 0.50   # range: 0.25 (early) to 0.75 (late)
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
        # V5 POST-MORTEM FIX: The 50% vol floor was 2-3x above reality, making the
        # position signal think recovery was likely when it wasn't.  Now we trust
        # realized vol much more aggressively — floor at only 15% of default.
        # If realized vol is very low, the model correctly says "gap is permanent"
        # and avoids phantom recovery edges.
        default_vol = _DAILY_VOL.get(asset, 0.05)
        vol_floor   = default_vol * 0.15          # minimal floor — trust realized vol
        realized_vol_5m = sig.get("realized_vol", 0.0)
        if realized_vol_5m and realized_vol_5m > 0.00005:
            daily_vol = max(realized_vol_5m * (288 ** 0.5), vol_floor)
        else:
            daily_vol = default_vol
        p_pos = _position_prob(current, floor, t_min, daily_vol)

        # ── Signal 2: Momentum (recent price direction) ──
        mom_5m  = sig.get("mom_5m", 0.0)
        mom_15m = sig.get("mom_15m", 0.0)
        mom_1m  = sig.get("mom_1m", 0.0)
        mom_3m  = sig.get("mom_3m", 0.0)
        trend   = sig.get("trend", "flat")

        # Late-window boost: use 1-min candle data when ≤3 min remain
        # This gives much more responsive direction signal near expiry.
        if t_min <= 3.0 and (mom_1m != 0.0 or mom_3m != 0.0):
            # Blend: 50% 1-min, 30% 3-min, 20% 5-min (recency wins late)
            effective_mom_5m = 0.50 * mom_1m + 0.30 * mom_3m + 0.20 * mom_5m
            effective_mom_15m = mom_5m  # use 5m as the "longer" signal
        else:
            effective_mom_5m = mom_5m
            effective_mom_15m = mom_15m

        p_mom = _momentum_prob(effective_mom_5m, effective_mom_15m, trend, t_min)

        # ── Gap gate: time-adaptive — strict early, loose late ──
        gap_pct = (current - floor) / floor * 100 if floor > 0 else 0.0
        # Linearly interpolate gap threshold: strict at 15 min, loose at ≤2 min
        if t_min <= 2.0:
            gap_thresh = _MIN_GAP_PCT_LATE
        elif t_min >= 5.0:
            gap_thresh = _MIN_GAP_PCT_EARLY
        else:
            # Linear blend between 5 min and 2 min
            frac = (t_min - 2.0) / 3.0
            gap_thresh = _MIN_GAP_PCT_LATE + frac * (_MIN_GAP_PCT_EARLY - _MIN_GAP_PCT_LATE)
        if abs(gap_pct) < gap_thresh:
            continue

        # ── Blend ──
        model_prob = _blend_prob(p_pos, p_mom, t_min)

        # ── Edge calculation (fee-adjusted) ──
        # Subtract estimated fee from raw edge so we only bet when net-of-fee edge > threshold
        edge_yes = model_prob - yes_ask - _FEE_RATE
        edge_no  = (1.0 - model_prob) - no_ask - _FEE_RATE

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

        # Gate: skip lottery-ticket prices (< 10¢) — model unreliable at extremes
        if bet_price < _MIN_BET_PRICE:
            continue

        # Gate: skip expensive contracts (> 65¢) — thin margins, need extreme accuracy
        if bet_price > _MAX_BET_PRICE:
            continue

        # Gate: signal agreement — TREND-ONLY strategy.
        # V6 POST-MORTEM: Contrarian plays went 1W/15L (6% win rate) = -$12.03 net.
        # Trend plays went 3W/2L (60% win rate) = +$3.22 net.  Contrarian is a losing
        # strategy in 15-min crypto windows.  The market prices cheap contracts cheaply
        # for a reason — the position signal's "recovery edge" is an illusion from
        # inflated vol.  BAN ALL CONTRARIAN PLAYS.  No exceptions.
        if trend == "up" and side == "NO":
            continue
        if trend == "down" and side == "YES":
            continue
        # If trend is flat, only allow if momentum agrees with chosen side
        if trend == "flat":
            combined_mom_check = 0.70 * effective_mom_5m + 0.30 * effective_mom_15m
            if side == "YES" and combined_mom_check < 0:
                continue
            if side == "NO" and combined_mom_check > 0:
                continue

        # EV per dollar
        ev_pct = (our_prob * (1.0 / bet_price - 1.0) - (1.0 - our_prob)) * 100

        # Kelly stake — let executor floor to its MIN_SPEND_USD ($2)
        # For small bankrolls (<$200), Kelly produces tiny numbers; just pass them through.
        stake = _kelly_stake(our_prob, bet_price, bankroll)
        if stake <= 0:
            continue

        # ── Confidence score (0-100): composite of gap strength, momentum, trend, time ──
        # Higher = more reliable signal. Scale each factor 0-1 then combine.
        combined_mom = 0.70 * effective_mom_5m + 0.30 * effective_mom_15m
        _gap_score = min(abs(gap_pct) / 0.20, 1.0)              # |gap| of 0.20% = max score
        _mom_score = min(abs(combined_mom) * 100 / 0.10, 1.0)   # 0.10% combined momentum = max
        _trend_score = 1.0 if trend in ("up", "down") else 0.3  # consistent trend = full marks
        # Time bonus: late-window positions are more certain
        _time_score = 1.0 - min(t_min, 10) / 10.0               # 0 min = 1.0, 10 min = 0.0
        confidence = round(
            (_gap_score * 0.35 + _mom_score * 0.25 + _trend_score * 0.20 + _time_score * 0.20) * 100
        )

        # Verdict
        if edge >= 0.10 and confidence >= 60:
            verdict = "STRONG VALUE"
        elif edge >= 0.06:
            verdict = "VALUE"
        else:
            verdict = "MARGINAL"

        # ── Signal breakdown for display ──
        gap_pct = (current - floor) / floor * 100
        signal_str = (
            f"pos={p_pos:.2f}  mom={p_mom:.2f}  "
            f"gap={gap_pct:+.3f}%  5m={mom_5m*100:+.3f}%  trend={trend}  conf={confidence}"
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
                "confidence":     confidence,
                "signal_str":     signal_str,
            },
        })

    picks.sort(key=lambda p: p["edge_pct"], reverse=True)
    return picks
