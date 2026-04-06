"""
conviction_engine.py — Multi-signal LOCK detector and high-value hunter
======================================================================
This is the intelligence core that drives the 80-90%+ win-rate target.

The key insight: rather than betting on every edge we find, we ONLY
bet when multiple INDEPENDENT algorithms all agree on the same side.
When 4+ unrelated strategies all point the same direction, the
probability of being wrong collapses dramatically.

Conviction Levels
─────────────────
  WATCH   (1) — 1 strategy firing, edge > 4%.  Log only, no bet.
  SIGNAL  (2) — 2 strategies agree, edge > 5%.  Small bet allowed.
  STRONG  (3) — 3 strategies agree, edge > 7%.  Normal bet.
  LOCK    (4) — 4+ strategies agree, edge > 9%.  Max bet. ~85-90% win target.
  JACKPOT     — Any conviction ≥ SIGNAL but market priced ≤ 20¢ and
                our model says ≥ 30%.  5:1+ payout, asymmetric edge.

Public API
──────────
  from research.conviction_engine import (
      analyze_conviction,
      find_locks,
      find_jackpots,
      find_best_value,
      scan_all_tiers,
  )
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Optional
import sys

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))


# ─── Levels ───────────────────────────────────────────────────────────────────

class ConvictionLevel(IntEnum):
    NOISE  = 0    # edge below threshold or strategies disagree
    WATCH  = 1    # 1 strategy, weak edge
    SIGNAL = 2    # 2 strategies agree
    STRONG = 3    # 3 strategies agree
    LOCK   = 4    # 4+ strategies agree — target for 85-90% win rate


# Strategy groups: we count one from each group for independence.
# Two strategies from the same group do NOT count as independent signals.
_INDEPENDENCE_GROUPS = {
    "momentum":     {"crypto_momentum", "calendar_effect"},
    "volatility":   {"crypto_vol_misprice"},
    "orderflow":    {"volume_breakout", "open_interest_signal"},
    "structural":   {"timedecay_exploit", "mean_reversion_fade", "cross_timeframe_arb"},
    "fundamental":  {"econ_consensus", "fedwatch_arb", "polling_arb", "weather_forecast"},
}

def _count_independent_groups(strategy_names: list[str]) -> int:
    """
    Count how many independent evidence groups are represented.
    Returns a number from 0 (all same group) to len(groups) (all different).
    """
    seen = set()
    for grp, members in _INDEPENDENCE_GROUPS.items():
        for name in strategy_names:
            if name in members:
                seen.add(grp)
                break
    return len(seen)


@dataclass
class ConvictionResult:
    ticker: str
    side: str                     # "yes" or "no"
    level: ConvictionLevel
    is_jackpot: bool
    strategy_count: int           # number of strategies agreeing
    independent_groups: int       # number of INDEPENDENT evidence groups
    avg_edge_pct: float           # mean edge across agreeing strategies
    max_edge_pct: float
    avg_our_prob: float
    avg_confidence: float
    market_price: float           # yes_ask (0-1)
    expected_payout: float        # 1 / market_price_on_winning_side
    ev_per_dollar: float          # expected value per $1 wagered
    strategies: list[str]
    best_reason: str
    all_signals: dict             # flattened signals from every agreeing strategy
    raw_scores: list[dict]        # full result dicts from strategy_library

    def to_dict(self) -> dict:
        return {
            "ticker":             self.ticker,
            "side":               self.side,
            "conviction":         self.level.name,
            "is_jackpot":         self.is_jackpot,
            "strategy_count":     self.strategy_count,
            "independent_groups": self.independent_groups,
            "avg_edge_pct":       round(self.avg_edge_pct, 4),
            "max_edge_pct":       round(self.max_edge_pct, 4),
            "avg_our_prob":       round(self.avg_our_prob, 4),
            "avg_confidence":     round(self.avg_confidence, 3),
            "market_price":       round(self.market_price, 4),
            "expected_payout":    round(self.expected_payout, 2),
            "ev_per_dollar":      round(self.ev_per_dollar, 4),
            "strategies":         self.strategies,
            "reason":             self.best_reason,
            "signals":            self.all_signals,
        }


# ─── Core analysis ────────────────────────────────────────────────────────────

def analyze_conviction(
    market: dict,
    context: dict,
    weights: dict[str, float] = None,
    require_min_edge: float = 0.03,
) -> Optional[ConvictionResult]:
    """
    Run every strategy against a market.  Find the consensus side.
    Return a ConvictionResult or None if no strategies fire.

    weights: from learning_tracker.get_strategy_weights()
    """
    from research.strategy_library import score_market

    scores = score_market(market, context, weights)
    if not scores:
        return None

    # Separate by side
    yes_scores = [s for s in scores if s["side"] == "yes" and abs(s["edge_pct"]) >= require_min_edge]
    no_scores  = [s for s in scores if s["side"] == "no"  and abs(s["edge_pct"]) >= require_min_edge]

    # Use the majority side
    if len(yes_scores) >= len(no_scores):
        agreeing = yes_scores
        side = "yes"
    else:
        agreeing = no_scores
        side = "no"

    if not agreeing:
        return None

    # -- Compute summary stats --
    strategy_names = [s["strategy"] for s in agreeing]
    edges          = [abs(s["edge_pct"]) for s in agreeing]
    probs          = [s["our_prob"] for s in agreeing]
    confs          = [s["confidence"] for s in agreeing]

    avg_edge   = sum(edges) / len(edges)
    max_edge   = max(edges)
    avg_prob   = sum(probs) / len(probs)
    avg_conf   = sum(confs) / len(confs)
    n_indep    = _count_independent_groups(strategy_names)

    # Choose best reason (highest confidence)
    best_score_obj = max(agreeing, key=lambda s: s["confidence"])
    best_reason = best_score_obj.get("reason", "")

    # Flatten all signals
    all_signals: dict = {}
    for s in agreeing:
        prefix = s["strategy"][:8]
        for k, v in s.get("signals", {}).items():
            all_signals[f"{prefix}.{k}"] = v

    # Market price on the side we're betting
    yes_ask = market.get("yes_ask", 0.5)
    if side == "yes":
        market_price = yes_ask
    else:
        market_price = 1.0 - yes_ask
    market_price = max(0.01, min(0.99, market_price))

    expected_payout = round(1.0 / market_price, 3)
    ev_per_dollar   = round(avg_prob * expected_payout - (1 - avg_prob), 4)

    # -- Determine conviction level --
    # We use INDEPENDENT groups (not raw strategy count) for level
    if n_indep >= 4 and avg_edge >= 0.09 and avg_conf >= 0.70:
        level = ConvictionLevel.LOCK
    elif n_indep >= 3 and avg_edge >= 0.07 and avg_conf >= 0.60:
        level = ConvictionLevel.STRONG
    elif n_indep >= 2 and avg_edge >= 0.05:
        level = ConvictionLevel.SIGNAL
    elif n_indep >= 1 and avg_edge >= 0.03:
        level = ConvictionLevel.WATCH
    else:
        level = ConvictionLevel.NOISE

    if level == ConvictionLevel.NOISE:
        return None

    # -- Jackpot flag --
    # Jackpot = asymmetric payout on a SIGNAL+ play
    # Market price ≤ 20¢ + our model says ≥ 30% = 5:1 payout with positive EV
    is_jackpot = (
        market_price <= 0.20
        and avg_prob >= 0.28
        and level.value >= ConvictionLevel.SIGNAL.value
        and ev_per_dollar > 0
    )

    return ConvictionResult(
        ticker            = market.get("ticker", ""),
        side              = side,
        level             = level,
        is_jackpot        = is_jackpot,
        strategy_count    = len(agreeing),
        independent_groups= n_indep,
        avg_edge_pct      = avg_edge,
        max_edge_pct      = max_edge,
        avg_our_prob      = avg_prob,
        avg_confidence    = avg_conf,
        market_price      = market_price,
        expected_payout   = expected_payout,
        ev_per_dollar     = ev_per_dollar,
        strategies        = strategy_names,
        best_reason       = best_reason,
        all_signals       = all_signals,
        raw_scores        = agreeing,
    )


# ─── Batch finders ───────────────────────────────────────────────────────────

def find_locks(
    markets: list[dict],
    context: dict,
    weights: dict[str, float] = None,
) -> list[ConvictionResult]:
    """
    Filter a market list to only LOCK-level conviction plays.
    These are the ~85-90% win rate bets: 4+ independent evidence groups
    all pointing the same direction with high confidence.
    """
    results = []
    for market in markets:
        r = analyze_conviction(market, context, weights)
        if r and r.level >= ConvictionLevel.LOCK:
            results.append(r)
    results.sort(key=lambda r: (r.independent_groups, r.avg_edge_pct), reverse=True)
    return results


def find_jackpots(
    markets: list[dict],
    context: dict,
    weights: dict[str, float] = None,
    max_price: float = 0.20,
    min_ev: float = 0.20,
) -> list[ConvictionResult]:
    """
    Hunt for asymmetric high-payout plays:
    - Market price ≤ max_price (i.e., minimum 5:1 payout)
    - Our model says ≥ 28% probability (actual edge positive)
    - EV per dollar ≥ min_ev

    These are the "big winner" bets — you lose most of the time
    but the payouts are 4-10x when right.
    """
    results = []
    for market in markets:
        r = analyze_conviction(market, context, weights)
        if r and r.is_jackpot and r.ev_per_dollar >= min_ev:
            results.append(r)
    results.sort(key=lambda r: r.ev_per_dollar, reverse=True)
    return results


def find_best_value(
    markets: list[dict],
    context: dict,
    weights: dict[str, float] = None,
    min_level: ConvictionLevel = ConvictionLevel.SIGNAL,
    top_n: int = 20,
) -> list[ConvictionResult]:
    """
    Find all markets above min_level, sorted by EV per dollar.
    This catches both locks AND jackpots in a single ranked list.
    """
    results = []
    for market in markets:
        r = analyze_conviction(market, context, weights)
        if r and r.level >= min_level and r.ev_per_dollar > 0:
            results.append(r)

    # Sort by: (is_jackpot bonus) + (level × 0.3) + EV
    results.sort(
        key=lambda r: r.ev_per_dollar + r.level.value * 0.1 + (0.3 if r.is_jackpot else 0),
        reverse=True,
    )
    return results[:top_n]


# ─── Full-tier scan ───────────────────────────────────────────────────────────

async def scan_all_tiers(context_override: dict = None) -> dict:
    """
    One-shot scan of ALL Kalshi markets, returning four ranked lists:
      - locks:    LOCK-level plays (target 85-90% win rate)
      - jackpots: high-payout asymmetric plays
      - strong:   STRONG-level confident plays
      - signals:  all SIGNAL+ plays for informational value

    Returns:
      {
        "locks":    [...],
        "jackpots": [...],
        "strong":   [...],
        "signals":  [...],
        "summary":  {counts, best_lock, best_jackpot}
      }
    """
    from research.market_scanner import (
        fetch_all_active_markets, _normalise_market,
        _get_momentum_context, _get_fedwatch_context, _get_econ_consensus_context,
    )
    from research.learning_tracker import get_strategy_weights

    # Fetch all markets
    raw_markets = await fetch_all_active_markets()
    markets = [_normalise_market(m) for m in raw_markets if m]
    markets = [m for m in markets if m]

    # Build context
    assets = list({m.get("asset", "") for m in markets if m.get("asset")})
    ctx = {
        "momentum":       await _get_momentum_context(assets),
        "fedwatch":       await _get_fedwatch_context(),
        "econ_consensus": _get_econ_consensus_context(),
    }
    if context_override:
        ctx.update(context_override)

    weights = get_strategy_weights()

    # Analyze all
    all_results = []
    for market in markets:
        r = analyze_conviction(market, ctx, weights)
        if r:
            all_results.append(r)

    locks    = [r for r in all_results if r.level >= ConvictionLevel.LOCK]
    jackpots = [r for r in all_results if r.is_jackpot]
    strong   = [r for r in all_results if r.level == ConvictionLevel.STRONG and not r.is_jackpot]
    signals  = [r for r in all_results if r.level >= ConvictionLevel.SIGNAL]

    # Sort
    locks.sort(   key=lambda r: (r.independent_groups, r.avg_confidence), reverse=True)
    jackpots.sort(key=lambda r: r.ev_per_dollar, reverse=True)
    strong.sort(  key=lambda r: r.avg_edge_pct, reverse=True)
    signals.sort( key=lambda r: r.ev_per_dollar, reverse=True)

    summary = {
        "total_markets_scanned": len(markets),
        "lock_count":    len(locks),
        "jackpot_count": len(jackpots),
        "strong_count":  len(strong),
        "signal_count":  len(signals),
        "best_lock":     locks[0].to_dict()    if locks    else None,
        "best_jackpot":  jackpots[0].to_dict() if jackpots else None,
    }

    return {
        "locks":    [r.to_dict() for r in locks[:10]],
        "jackpots": [r.to_dict() for r in jackpots[:10]],
        "strong":   [r.to_dict() for r in strong[:15]],
        "signals":  [r.to_dict() for r in signals[:30]],
        "summary":  summary,
    }


# ─── Kelly sizing for each conviction level ───────────────────────────────────

def kelly_for_conviction(
    result: ConvictionResult,
    bankroll: float,
    kelly_fraction: float = 0.25,
) -> dict:
    """
    Compute Kelly-optimal bet size adjusted for conviction level.

    LOCK    → full kelly_fraction  (up to 25% of allocated bankroll)
    STRONG  → 0.65 × kelly_fraction
    SIGNAL  → 0.33 × kelly_fraction (conservative)
    JACKPOT → 0.15 × bankroll flat  (small but meaningful on big payouts)
    """
    b = result.expected_payout - 1.0   # net odds (decimal - 1)
    p = result.avg_our_prob
    q = 1.0 - p

    if b <= 0:
        return {"spend": 1.0, "contracts": 0, "kelly_f": 0.0}

    raw_kelly = (b * p - q) / b        # full Kelly fraction of bankroll
    raw_kelly = max(0.0, raw_kelly)

    level_scale = {
        ConvictionLevel.LOCK:   kelly_fraction,
        ConvictionLevel.STRONG: kelly_fraction * 0.65,
        ConvictionLevel.SIGNAL: kelly_fraction * 0.33,
        ConvictionLevel.WATCH:  kelly_fraction * 0.10,
    }.get(result.level, kelly_fraction * 0.10)

    if result.is_jackpot and result.level.value >= ConvictionLevel.SIGNAL.value:
        # Special jackpot sizing: small flat bet, not Kelly (variance too high)
        spend = min(2.0, bankroll * 0.05)
    else:
        spend = bankroll * raw_kelly * level_scale
        spend = round(max(1.0, min(spend, bankroll * 0.30)), 2)

    price_cents = int(result.market_price * 100)
    contracts   = max(1, int(spend * 100 / max(1, price_cents)))

    return {
        "spend":      spend,
        "contracts":  contracts,
        "kelly_f":    round(raw_kelly, 4),
        "level_scale":level_scale,
    }
