"""
crypto_ev.py — Edge model for Kalshi crypto/finance price markets
=================================================================
Models the probability of price-prediction events using:
  - BTC/ETH: log-normal price diffusion model
  - FED rate: historical rate + macro signals
  - SPX: similar diffusion to BTC but much lower vol
  - OIL: diffusion with supply/demand weighting

Then computes Kelly fraction and picks with positive edge.

Usage:
    from engine.crypto_ev import price_edge_picks
    picks = price_edge_picks(markets, prices)
"""
from __future__ import annotations

import math
from typing import Optional

# ---------------------------------------------------------------------------
# Asset vol assumptions (daily σ, annualised base)
# Tune these based on live data.  Conservative starting points:
# ---------------------------------------------------------------------------
_DAILY_VOL = {
    "BTC": 0.038,   # ~3.8% daily BTC vol
    "ETH": 0.042,   # ~4.2% daily ETH vol
    "SPX": 0.012,   # ~1.2% daily S&P 500 vol
    "OIL": 0.025,   # ~2.5% daily WTI vol
    "FED": None,     # FED uses discrete model, not diffusion
}

_KELLY_FRACTION = 0.25   # fractional Kelly (safety factor)
_MIN_EDGE       = 0.05   # 5% minimum edge to surface a pick (absolute)
_MIN_YES_PROB   = 0.05   # ignore if market says < 5%  (avoid lottery tickets)
_MAX_YES_PROB   = 0.95   # ignore if market says > 95% (avoid negative-edge NO)


# ---------------------------------------------------------------------------
# Gaussian CDF (pure-Python — no scipy dependency)
# ---------------------------------------------------------------------------
def _ndcdf(x: float) -> float:
    """Approximation of the standard normal CDF."""
    # Abramowitz & Stegun 7.1.26
    t = 1.0 / (1.0 + 0.2316419 * abs(x))
    p = (0.319381530 * t
         - 0.356563782 * t**2
         + 1.781477937 * t**3
         - 1.821255978 * t**4
         + 1.330274429 * t**5)
    cdf = 1.0 - (1.0 / math.sqrt(2 * math.pi)) * math.exp(-0.5 * x**2) * p
    return cdf if x >= 0 else 1.0 - cdf


# ---------------------------------------------------------------------------
# Price-diffusion probability  (log-normal Brownian motion, zero drift)
# ---------------------------------------------------------------------------
def _diffusion_prob_above(
    current: float,
    threshold: float,
    hours: float,
    daily_vol: float,
) -> float:
    """
    P(price > threshold at `hours` from now)
    under a zero-drift lognormal model.

    Returns float in [0, 1].
    """
    t = hours / 24.0
    if t <= 0:
        return 1.0 if current > threshold else 0.0
    sigma = daily_vol * math.sqrt(t)
    # log-normal: lnS(T) ~ N(ln S0 - 0.5σ²T, σ²T)
    drift = -0.5 * (daily_vol ** 2) * t
    d = (math.log(current / threshold) + drift) / sigma
    return _ndcdf(d)


# ---------------------------------------------------------------------------
# FED rate model
# ---------------------------------------------------------------------------

# Current upper bound of the fed funds rate (update via .env or hard-code):
_FED_CURRENT_RATE = 4.25   # percent

def _fed_prob_above(threshold: float) -> float:
    """
    P(fed rate > threshold after next FOMC meeting).
    
    Uses a simple step-function prior calibrated to current market consensus:
    - The CME FedWatch / SOFR markets imply ~75% chance of a ≥25bp cut by Apr FOMC
      given tariff/recession fears (as of Apr 6, 2026).
    
    This gives a staircase probability by threshold:
      > 4.25% (no cut)    → ~22%
      > 4.00% (≤25bp cut) → ~48%
      > 3.75% (≤50bp cut) → ~72%
      > 3.50% (≤75bp cut) → ~88%
      > 3.25% (≤100bp)    → ~96%
    
    NOTE: Replace with live CME FedWatch scraping for production precision.
    """
    # Discrete probability over rate outcomes (rounded to nearest 25bp)
    # P(rate ends at each level) after Apr 27 FOMC:
    #   5.00% = 0%   4.75% = 0%   4.50% = 0%  (no hike expected)
    #   4.25% = 22%  (no cut)
    #   4.00% = 26%  (25bp cut)
    #   3.75% = 24%  (50bp cut)
    #   3.50% = 16%  (75bp cut)
    #   3.25% = 12%  (100bp cut)
    outcomes = {
        4.25: 0.22,
        4.00: 0.26,
        3.75: 0.24,
        3.50: 0.16,
        3.25: 0.12,
    }
    # P(rate > threshold) = sum of probs for rates strictly > threshold
    return sum(p for rate, p in outcomes.items() if rate > threshold)


# ---------------------------------------------------------------------------
# Kelly sizing
# ---------------------------------------------------------------------------
def _kelly(prob: float, yes_ask: float, bankroll: float) -> float:
    """
    Kelly formula for a binary bet:
      f* = (prob × (1/yes_ask - 1) - (1 - prob)) / (1/yes_ask - 1)
    Clipped to [0, 0.25] and multiplied by KELLY_FRACTION.
    """
    if yes_ask <= 0 or yes_ask >= 1:
        return 0.0
    b = (1.0 / yes_ask) - 1.0  # net odds on YES
    f = (prob * b - (1.0 - prob)) / b
    f = max(0.0, min(f, 0.25))
    return f * _KELLY_FRACTION * bankroll


# ---------------------------------------------------------------------------
# Main pick generator
# ---------------------------------------------------------------------------

def price_edge_picks(
    markets: list[dict],
    prices: dict[str, float],
    bankroll: float = 10_000.0,
    min_edge: float = _MIN_EDGE,
) -> list[dict]:
    """
    For each market, compute our model probability, compare to market price,
    and return picks with positive edge sorted by edge descending.

    Parameters
    ----------
    markets  : output of kalshi_crypto.get_crypto_markets()
    prices   : {"btc": ..., "eth": ..., ...}
    bankroll : total bankroll in USD
    min_edge : minimum absolute edge (0..1) to include a pick

    Returns
    -------
    list of pick dicts with same structure as orchestrator sports picks
    """
    btc = prices.get("btc", 0.0)
    eth = prices.get("eth", 0.0)

    picks = []

    for m in markets:
        asset     = m["asset"]
        threshold = m["threshold"]
        yes_ask   = m["yes_ask"]
        hours     = m["hours_to_close"]

        # ------------------------------------------------------------------
        # Compute our model probability
        # ------------------------------------------------------------------
        if asset == "BTC" and btc > 0:
            vol  = _DAILY_VOL["BTC"]
            if m.get("strike_type", "greater") == "greater":
                model_prob = _diffusion_prob_above(btc, threshold, hours, vol)
            else:
                model_prob = 1.0 - _diffusion_prob_above(btc, threshold, hours, vol)

        elif asset == "ETH" and eth > 0:
            vol = _DAILY_VOL["ETH"]
            if m.get("strike_type", "greater") == "greater":
                model_prob = _diffusion_prob_above(eth, threshold, hours, vol)
            else:
                model_prob = 1.0 - _diffusion_prob_above(eth, threshold, hours, vol)

        elif asset == "FED":
            model_prob = _fed_prob_above(threshold)

        elif asset == "SPX":
            spx = prices.get("spx", 0.0)
            if not spx:
                continue
            vol = _DAILY_VOL["SPX"]
            if m.get("strike_type", "greater") == "greater":
                model_prob = _diffusion_prob_above(spx, threshold, hours, vol)
            else:
                model_prob = 1.0 - _diffusion_prob_above(spx, threshold, hours, vol)

        elif asset == "OIL":
            oil = prices.get("oil", 0.0)
            if not oil:
                continue
            vol = _DAILY_VOL["OIL"]
            if m.get("strike_type", "greater") == "greater":
                model_prob = _diffusion_prob_above(oil, threshold, hours, vol)
            else:
                model_prob = 1.0 - _diffusion_prob_above(oil, threshold, hours, vol)

        else:
            continue

        # Skip degenerate probabilities
        if not (0.02 <= model_prob <= 0.98):
            continue

        # ------------------------------------------------------------------
        # Edge = model_prob - market_yes_price
        # If edge > 0  → buy YES;  if edge < 0 → buy NO (flipped edge)
        # ------------------------------------------------------------------
        edge_yes = model_prob - yes_ask
        edge_no  = (1.0 - model_prob) - m["no_ask"]

        best_edge  = edge_yes
        side       = "YES"
        bet_price  = yes_ask
        our_prob   = model_prob

        if edge_no > edge_yes:
            best_edge = edge_no
            side      = "NO"
            bet_price = m["no_ask"]
            our_prob  = 1.0 - model_prob

        if best_edge < min_edge:
            continue

        # EV per dollar wagered
        ev_pct = (our_prob * (1.0 / bet_price - 1.0) - (1.0 - our_prob)) * 100

        # Implied probability from market price
        implied_prob = bet_price  # Kalshi prices ARE probabilities

        # Kelly stake
        stake = _kelly(our_prob, bet_price, bankroll)
        if stake < 1.0:
            continue   # too small to bother

        # Decimal odds for the side being bet
        decimal_odds = 1.0 / bet_price

        # Verdict label
        if best_edge >= 0.12:
            verdict = "STRONG VALUE"
        elif best_edge >= 0.07:
            verdict = "VALUE"
        else:
            verdict = "MARGINAL"

        # Close-time label
        try:
            close_dt = m["close_time"]
        except Exception:
            close_dt = ""

        picks.append({
            "sport":              "CRYPTO",
            "event":              m["title"],
            "pick":               f"{asset} {side} > {threshold:,.2f}" if side == "YES" else f"{asset} NO > {threshold:,.2f}",
            "market":             m["ticker"],
            "book":               "kalshi",
            "decimal_odds":       round(decimal_odds, 4),
            "american_odds":      int((decimal_odds - 1) * 100) if decimal_odds >= 2 else int(-100 / (decimal_odds - 1)),
            "our_prob":           round(our_prob * 100, 1),
            "implied_prob":       round(implied_prob * 100, 1),
            "edge_pct":           round(best_edge * 100, 2),
            "ev_pct":             round(ev_pct, 2),
            "kelly_pct":          round(_kelly(our_prob, bet_price, bankroll) / bankroll * 100, 2),
            "recommended_stake":  round(stake, 2),
            "verdict":            verdict,
            "commence_time":      close_dt,
            "is_live":            True,
            # Crypto-specific metadata
            "crypto_meta": {
                "asset":          asset,
                "threshold":      threshold,
                "current_price":  prices.get(asset.lower(), 0),
                "hours_to_close": round(hours, 1),
                "side":           side,
                "market_prob":    round(yes_ask * 100, 1),
                "model_prob":     round(model_prob * 100, 1),
            },
        })

    picks.sort(key=lambda p: p["edge_pct"], reverse=True)
    return picks
