"""
Expected Value Engine
=====================
EV calculations, line shopping, and value identification.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import math


@dataclass
class EVResult:
    ev: float               # expected value per $1 staked
    ev_pct: float           # as percentage
    edge: float             # our_prob - implied_prob
    break_even_prob: float  # probability needed to break even
    our_prob: float
    book_odds_dec: float
    positive: bool
    confidence: str


def calculate_ev(our_prob: float, decimal_odds: float) -> EVResult:
    """
    EV = (P_win × Profit) – (P_lose × Stake)
    where Profit = decimal_odds - 1, Stake = 1
    
    EV > 0 = profitable over the long run.
    """
    profit = decimal_odds - 1.0
    loss = 1.0
    ev = (our_prob * profit) - ((1.0 - our_prob) * loss)
    
    break_even = 1.0 / decimal_odds
    edge = our_prob - break_even
    
    if ev > 0.10:
        confidence = "ELITE (+10%)"
    elif ev > 0.05:
        confidence = "STRONG (+5%)"
    elif ev > 0.02:
        confidence = "VALUE (+2%)"
    elif ev > 0:
        confidence = "MARGINAL"
    else:
        confidence = "NEGATIVE EV — FADE"
    
    return EVResult(
        ev=ev,
        ev_pct=ev * 100,
        edge=edge,
        break_even_prob=break_even,
        our_prob=our_prob,
        book_odds_dec=decimal_odds,
        positive=ev > 0,
        confidence=confidence,
    )


def line_shop_best(lines: list[dict]) -> dict:
    """
    Find the best available line across multiple books.
    lines: [{"book": "DraftKings", "odds": 1.91, "side": "home"}, ...]
    Returns the best EV line for each side.
    """
    by_side: dict[str, list] = {}
    for line in lines:
        side = line.get("side", "unknown")
        by_side.setdefault(side, []).append(line)
    
    best = {}
    for side, entries in by_side.items():
        best[side] = max(entries, key=lambda x: x["odds"])
    
    return best


def closing_line_value(open_odds: float, close_odds: float) -> float:
    """
    Closing Line Value (CLV) — the gold standard for bet quality.
    Positive CLV = beat the market, long-run winner.
    
    CLV = (1/open - 1/close) — measures how much you beat the close.
    """
    return (1.0 / open_odds) - (1.0 / close_odds)


def true_probability_no_vig(
    home_odds: float, away_odds: float, draw_odds: Optional[float] = None
) -> dict:
    """
    Remove the bookmaker's vig to get the true market probability.
    Works for two-way and three-way markets (soccer draws).
    """
    probs = [1.0 / home_odds, 1.0 / away_odds]
    if draw_odds:
        probs.append(1.0 / draw_odds)
    
    total_overround = sum(probs)
    true_probs = [p / total_overround for p in probs]
    
    labels = ["home", "away"]
    if draw_odds:
        labels.append("draw")
    
    return {
        "overround": total_overround,
        "vig_pct": (total_overround - 1.0) * 100,
        "true_probs": dict(zip(labels, true_probs)),
    }


def implied_to_american(decimal_odds: float) -> int:
    """Convert decimal odds to American odds."""
    if decimal_odds >= 2.0:
        return int((decimal_odds - 1.0) * 100)
    else:
        return int(-100 / (decimal_odds - 1.0))


def compound_win_rate(
    primary_prob: float = 0.60,
    live_prob: float = 0.65,
    prop_prob: float = 0.70,
    hedge_prob: float = 0.75,
    arb_prob: float = 0.85,
    weights: Optional[list[float]] = None,
) -> float:
    """
    Profit Machine Protocol 2.0 compound win rate.
    Weighted geometric average of all bet probabilities.
    """
    probs = [primary_prob, live_prob, prop_prob, hedge_prob, arb_prob]
    if weights is None:
        weights = [0.50, 0.15, 0.20, 0.10, 0.05]  # 50/20/20/10 split
    
    # Weighted geometric mean
    log_sum = sum(w * math.log(p) for w, p in zip(weights, probs))
    return math.exp(log_sum)


def acts_of_god_adjustment(
    base_prob: float,
    weather_impact: float = 0.0,    # -0.05 to +0.05
    travel_impact: float = 0.0,     # -0.03 to +0.03 (back-to-back, timezone)
    injury_impact: float = 0.0,     # -0.10 to +0.10
    altitude_impact: float = 0.0,   # -0.02 to +0.02
    rest_impact: float = 0.0,       # -0.04 to +0.04
) -> float:
    """
    Data-Driven Betting Empire 'Acts of God' adjustment.
    Adjusts raw model probability for exogenous factors.
    """
    adjusted = base_prob + weather_impact + travel_impact + injury_impact + altitude_impact + rest_impact
    return max(0.01, min(0.99, adjusted))
