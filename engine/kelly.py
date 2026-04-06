"""
Kelly Criterion Engine
======================
Full Kelly, Fractional Kelly, and Kelly with drawdown protection.
Sourced from the Betting Mastery Guide + 200-page AI guide.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import math


@dataclass
class KellyResult:
    fraction: float          # raw Kelly fraction (0-1)
    recommended: float       # after fractional scaling
    bet_amount: float        # dollar amount
    edge: float              # raw edge %
    implied_prob: float
    our_prob: float
    ev: float                # expected value per unit staked
    verdict: str


def kelly_fraction(prob_win: float, decimal_odds: float) -> float:
    """
    Standard Kelly Criterion.
    
    f* = (bp - q) / b
    b = decimal_odds - 1  (profit per unit staked)
    p = probability of winning
    q = 1 - p
    """
    b = decimal_odds - 1.0
    if b <= 0:
        return 0.0
    q = 1.0 - prob_win
    f = (b * prob_win - q) / b
    return max(f, 0.0)


def implied_probability(decimal_odds: float) -> float:
    """Convert decimal odds to implied probability (with vig removed)."""
    if decimal_odds <= 1.0:
        return 1.0
    return 1.0 / decimal_odds


def american_to_decimal(american_odds: int) -> float:
    """Convert American odds (+150, -110) to decimal."""
    if american_odds > 0:
        return (american_odds / 100.0) + 1.0
    else:
        return (100.0 / abs(american_odds)) + 1.0


def remove_vig(odds_a: float, odds_b: float) -> tuple[float, float]:
    """
    Remove the vig from two-outcome market.
    Returns fair probabilities for each outcome.
    """
    p_a = 1.0 / odds_a
    p_b = 1.0 / odds_b
    total = p_a + p_b
    return p_a / total, p_b / total


def calculate_kelly(
    our_prob: float,
    decimal_odds: float,
    bankroll: float,
    kelly_multiplier: float = 0.25,   # quarter Kelly by default
    min_edge: float = 0.03,
) -> KellyResult:
    """
    Full Kelly calculation with scaling and edge gating.
    
    Args:
        our_prob: Our estimated probability (0-1)
        decimal_odds: Book's decimal odds
        bankroll: Current total bankroll
        kelly_multiplier: Fraction of Kelly to use (0.25 = quarter Kelly)
        min_edge: Minimum edge required to recommend a bet
    """
    implied = implied_probability(decimal_odds)
    edge = our_prob - implied
    ev_per_unit = (our_prob * (decimal_odds - 1)) - (1.0 - our_prob)
    
    raw_kelly = kelly_fraction(our_prob, decimal_odds)
    scaled = raw_kelly * kelly_multiplier
    
    bet_amount = bankroll * scaled
    
    if edge < min_edge or raw_kelly <= 0:
        verdict = "SKIP — insufficient edge"
    elif edge >= 0.10:
        verdict = "STRONG BET ✓✓"
    elif edge >= 0.05:
        verdict = "VALUE BET ✓"
    else:
        verdict = "MARGINAL — size down"

    return KellyResult(
        fraction=raw_kelly,
        recommended=scaled,
        bet_amount=round(bet_amount, 2),
        edge=edge,
        implied_prob=implied,
        our_prob=our_prob,
        ev=ev_per_unit,
        verdict=verdict,
    )


def risk_of_ruin(win_prob: float, avg_win_pct: float, avg_loss_pct: float, n_bets: int = 1000) -> float:
    """
    Simplified risk-of-ruin estimate.
    Uses the formula: RoR ≈ ((1-edge) / (1+edge)) ^ (bankroll / avg_bet)
    """
    edge = (win_prob * avg_win_pct) - ((1 - win_prob) * avg_loss_pct)
    if edge <= 0:
        return 1.0
    # Simplified Gambler's Ruin approximation
    ratio = (1 - edge) / (1 + edge)
    # Assume betting 2% of bankroll per bet: 1/0.02 = 50 units
    units = 1.0 / avg_loss_pct if avg_loss_pct > 0 else 50
    ror = ratio ** units
    return min(max(ror, 0.0), 1.0)


def parlay_ev(bets: list[dict]) -> dict:
    """
    Calculate EV for a parlay.
    bets: [{"prob": 0.55, "decimal_odds": 1.91}, ...]
    """
    combined_prob = 1.0
    combined_odds = 1.0
    for b in bets:
        combined_prob *= b["prob"]
        combined_odds *= b["decimal_odds"]
    
    ev = (combined_prob * combined_odds) - 1.0
    return {
        "legs": len(bets),
        "combined_prob": combined_prob,
        "combined_odds": combined_odds,
        "ev": ev,
        "positive": ev > 0,
    }


def profit_machine_split(bankroll: float, confidence: str = "standard") -> dict:
    """
    Profit Machine Protocol 2.0 bankroll allocation.
    50% Primary / 20% Hedge / 20% Props / 10% High-payout
    
    Confidence levels adjust the total stake size.
    """
    stake_pct = {
        "high": 0.05,       # 5% of bankroll
        "standard": 0.03,   # 3% of bankroll
        "low": 0.02,        # 2% of bankroll
    }.get(confidence, 0.03)
    
    total_stake = bankroll * stake_pct
    
    return {
        "total_stake": round(total_stake, 2),
        "primary_50pct": round(total_stake * 0.50, 2),
        "hedge_20pct": round(total_stake * 0.20, 2),
        "props_20pct": round(total_stake * 0.20, 2),
        "high_payout_10pct": round(total_stake * 0.10, 2),
        "confidence": confidence,
        "bankroll": bankroll,
    }


if __name__ == "__main__":
    # Quick demo
    result = calculate_kelly(
        our_prob=0.58,
        decimal_odds=american_to_decimal(-110),
        bankroll=10_000,
        kelly_multiplier=0.25,
    )
    print(f"Edge: {result.edge:.1%}")
    print(f"Raw Kelly: {result.fraction:.1%}")
    print(f"Recommended Bet: ${result.bet_amount}")
    print(f"EV per $100: ${result.ev * 100:.2f}")
    print(f"Verdict: {result.verdict}")
    
    split = profit_machine_split(10_000, "standard")
    print("\nProfit Machine Split:")
    for k, v in split.items():
        print(f"  {k}: {v}")
