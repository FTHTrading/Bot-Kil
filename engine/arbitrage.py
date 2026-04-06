"""
Arbitrage Detection Engine
==========================
Cross-book and cross-game arbitrage finder.
From the PDF: 80-90% no-loss outcome when discrepancies exist.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import itertools


@dataclass
class ArbOpportunity:
    sport: str
    event: str
    market: str
    legs: list[dict]           # [{book, side, odds_dec, stake}]
    total_stake: float
    guaranteed_profit: float
    profit_pct: float
    is_live: bool
    recommended: bool


def find_two_way_arb(
    side_a_odds: float,   # decimal odds for outcome A (best available)
    side_b_odds: float,   # decimal odds for outcome B (best available)
    stake: float = 100.0,
) -> Optional[dict]:
    """
    Two-way arbitrage calculator (moneyline, no-draw markets).
    
    An arb exists when: 1/odds_A + 1/odds_B < 1
    
    Returns optimal stake split for guaranteed profit.
    """
    total_implied = (1.0 / side_a_odds) + (1.0 / side_b_odds)
    
    if total_implied >= 1.0:
        return None   # no arb
    
    profit_margin = 1.0 - total_implied
    
    # Optimal stakes to guarantee equal profit regardless of outcome
    stake_a = stake * (1.0 / side_a_odds) / total_implied
    stake_b = stake * (1.0 / side_b_odds) / total_implied
    
    profit_if_a = stake_a * side_a_odds - stake
    profit_if_b = stake_b * side_b_odds - stake
    
    return {
        "arb_exists": True,
        "profit_margin_pct": profit_margin * 100,
        "total_stake": round(stake, 2),
        "stake_a": round(stake_a, 2),
        "stake_b": round(stake_b, 2),
        "profit_if_a_wins": round(profit_if_a, 2),
        "profit_if_b_wins": round(profit_if_b, 2),
        "guaranteed_profit": round(min(profit_if_a, profit_if_b), 2),
    }


def find_three_way_arb(
    odds_home: float,
    odds_draw: float,
    odds_away: float,
    stake: float = 100.0,
) -> Optional[dict]:
    """Three-way arb for soccer (home/draw/away)."""
    total_implied = (1.0 / odds_home) + (1.0 / odds_draw) + (1.0 / odds_away)
    
    if total_implied >= 1.0:
        return None
    
    profit_margin = 1.0 - total_implied
    
    stake_home = stake / (odds_home * total_implied)
    stake_draw = stake / (odds_draw * total_implied)
    stake_away = stake / (odds_away * total_implied)
    
    return {
        "arb_exists": True,
        "profit_margin_pct": profit_margin * 100,
        "total_stake": round(stake, 2),
        "stake_home": round(stake_home, 2),
        "stake_draw": round(stake_draw, 2),
        "stake_away": round(stake_away, 2),
        "guaranteed_profit": round(profit_margin * stake, 2),
    }


def scan_multibook_lines(games: list[dict], bankroll: float = 10_000, min_profit_pct: float = 0.5) -> list[ArbOpportunity]:
    """
    Scan multiple sportsbooks for arbitrage opportunities.
    
    games format:
    [
      {
        "event": "Lakers vs Warriors",
        "sport": "NBA",
        "market": "moneyline",
        "is_live": False,
        "outcomes": [
          {"name": "Lakers", "odds_by_book": {"DraftKings": 2.10, "FanDuel": 2.05, "BetMGM": 2.15}},
          {"name": "Warriors", "odds_by_book": {"DraftKings": 1.75, "FanDuel": 1.80, "BetMGM": 1.72}},
        ]
      }
    ]
    """
    opportunities = []
    
    for game in games:
        outcomes = game.get("outcomes", [])
        if len(outcomes) < 2:
            continue
        
        # Find best odds for each outcome across all books
        best_odds = []
        best_legs = []
        for outcome in outcomes:
            odds_map = outcome.get("odds_by_book", {})
            if not odds_map:
                continue
            best_book = max(odds_map, key=odds_map.get)
            best_line = odds_map[best_book]
            best_odds.append(best_line)
            best_legs.append({
                "side": outcome["name"],
                "book": best_book,
                "odds_dec": best_line,
            })
        
        if len(best_odds) == 2:
            arb = find_two_way_arb(best_odds[0], best_odds[1], stake=bankroll * 0.05)
        elif len(best_odds) == 3:
            arb = find_three_way_arb(best_odds[0], best_odds[1], best_odds[2], stake=bankroll * 0.05)
        else:
            continue
        
        if arb and arb["profit_margin_pct"] >= min_profit_pct:
            # Add stake amounts to legs
            for i, leg in enumerate(best_legs):
                stake_key = f"stake_{['a','b','c'][i]}" if len(best_legs) <= 3 else f"stake_{i}"
                # normalize key lookup
                for k in arb:
                    if "stake_" in k and k != "total_stake":
                        pass
                leg["stake"] = list(arb.values())[i + 2] if i < len(best_legs) else 0
            
            opportunities.append(ArbOpportunity(
                sport=game.get("sport", ""),
                event=game.get("event", ""),
                market=game.get("market", "moneyline"),
                legs=best_legs,
                total_stake=arb["total_stake"],
                guaranteed_profit=arb["guaranteed_profit"],
                profit_pct=arb["profit_margin_pct"],
                is_live=game.get("is_live", False),
                recommended=arb["profit_margin_pct"] >= 1.0,
            ))
    
    return sorted(opportunities, key=lambda x: x.profit_pct, reverse=True)


def midline_value(book_a_odds: float, book_b_odds: float) -> dict:
    """
    Find EV when one book has a stale line vs another.
    Used for 'middling' — betting both sides hoping for the spread to land in between.
    """
    mid = (book_a_odds + book_b_odds) / 2.0
    spread = abs(book_a_odds - book_b_odds)
    return {
        "midline": mid,
        "spread_diff": spread,
        "value": spread > 0.1,  # meaningful spread difference
    }
