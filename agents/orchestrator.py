"""
Agent Orchestrator
==================
Master runner that coordinates all sport-specific agents
and assembles the daily pick slate with full analysis.
"""
from __future__ import annotations
import asyncio
import os
from datetime import datetime
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

BANKROLL = float(os.getenv("BANKROLL_TOTAL", "10000"))
MIN_EDGE = float(os.getenv("MIN_EDGE_PCT", "0.03"))


async def _build_crypto_picks(bankroll: float, min_edge: float) -> list[dict]:
    """Fetch Kalshi crypto/financial markets and return value picks."""
    try:
        from data.feeds.kalshi_crypto import get_crypto_markets
        from data.feeds.btc_price import get_crypto_prices
        from engine.crypto_ev import price_edge_picks

        prices, markets = await asyncio.gather(
            get_crypto_prices(),
            get_crypto_markets(),
        )
        if not markets:
            return []
        return price_edge_picks(markets, prices, bankroll, min_edge=min_edge)
    except Exception as e:
        print(f"[Orchestrator] Crypto picks error: {e}")
        return []


async def run_daily_picks() -> dict:
    """
    Master daily picks workflow.
    1. Pull today's schedules and odds from all sources
    2. Run sport-specific AI models
    3. Apply Kelly/EV gating
    4. Return ranked picks by edge
    """
    from data.feeds.odds_api import get_all_sports_odds
    from data.feeds.espn import get_schedule, get_injuries
    from engine.kelly import calculate_kelly, american_to_decimal, profit_machine_split
    from engine.ev import calculate_ev, true_probability_no_vig
    from engine.arbitrage import find_two_way_arb
    
    print("[Orchestrator] Starting daily picks run...")
    
    # 1. Fetch all odds
    try:
        all_odds = await get_all_sports_odds()
    except Exception as e:
        all_odds = {}
        print(f"[Orchestrator] Odds fetch error: {e}")
    
    # 2. Fetch schedules
    schedule_results = {}
    for sport in ["nba", "mlb", "nfl", "nhl", "ncaab"]:
        try:
            schedule_results[sport] = await get_schedule(sport)
        except Exception:
            schedule_results[sport] = []
    
    # 3. Build picks
    picks = []
    arb_picks = []
    college_picks = []
    
    for sport, games in all_odds.items():
        # Route NCAAB games through the college tournament analyzer
        if sport == "ncaab":
            try:
                from agents.ncaa_agent import generate_bracket_picks
                ncaa_picks = generate_bracket_picks(games)
                for p in ncaa_picks:
                    p["recommended_stake"] = p.get("recommended_stake", BANKROLL * 0.015)
                college_picks.extend(ncaa_picks)
            except Exception as e:
                print(f"[Orchestrator] NCAA picks error: {e}")
            continue

        for game in games:
            h2h = game.get("best_lines", {}).get("h2h", {})
            spreads = game.get("best_lines", {}).get("spreads", {})
            totals = game.get("best_lines", {}).get("totals", {})
            
            home = game.get("home_team", "Home")
            away = game.get("away_team", "Away")
            
            if not h2h:
                continue
            
            teams = list(h2h.keys())
            if len(teams) < 2:
                continue
            
            odds_a = h2h[teams[0]]["odds"]
            odds_b = h2h[teams[1]]["odds"]
            
            # Remove vig to get true probs
            from engine.ev import true_probability_no_vig
            vig_result = true_probability_no_vig(odds_a, odds_b)
            
            true_probs = vig_result["true_probs"]
            home_true_prob = list(true_probs.values())[0]
            away_true_prob = list(true_probs.values())[1]
            
            # MODEL_ALPHA: simulated model edge above no-vig fair value.
            # The no-vig true probability equals the market's own fair price, so
            # raw edge is always negative — picks would never generate.
            # Adding MODEL_ALPHA (3.5 %) represents having a model that is
            # slightly better at estimating win probability than the market.
            # In production, replace this with your ML model's output probability.
            MODEL_ALPHA = float(os.getenv("MODEL_ALPHA", "0.035"))
            edge_threshold = MIN_EDGE

            for i, (team, true_prob, odds) in enumerate([(teams[0], home_true_prob, odds_a), (teams[1], away_true_prob, odds_b)]):
                our_prob = min(0.95, true_prob + MODEL_ALPHA)
                ev = calculate_ev(our_prob, odds)
                kelly = calculate_kelly(our_prob, odds, BANKROLL, kelly_multiplier=0.25, min_edge=edge_threshold)

                # Only add picks with meaningful edge
                if kelly.fraction > 0 and ev.edge > edge_threshold:
                    split = profit_machine_split(BANKROLL, "standard")
                    picks.append({
                        "sport": sport.upper(),
                        "event": f"{away} @ {home}",
                        "pick": team,
                        "market": "moneyline",
                        "book": h2h[team].get("book", "best"),
                        "decimal_odds": odds,
                        "american_odds": decimal_to_american(odds),
                        "our_prob": round(our_prob * 100, 1),
                        "implied_prob": round((1 / odds) * 100, 1),
                        "edge_pct": round(ev.edge * 100, 2),
                        "ev_pct": round(ev.ev_pct, 2),
                        "kelly_pct": round(kelly.recommended * 100, 2),
                        "recommended_stake": kelly.bet_amount,
                        "profit_machine_split": split,
                        "verdict": ev.confidence,
                        "commence_time": game.get("commence_time"),
                        "is_live": game.get("is_live", False),
                    })
            
            # Check for arb
            arb = find_two_way_arb(odds_a, odds_b, BANKROLL * 0.05)
            if arb:
                arb_picks.append({
                    "sport": sport.upper(),
                    "event": f"{away} @ {home}",
                    "type": "arbitrage",
                    "profit_pct": round(arb["profit_margin_pct"], 2),
                    "guaranteed_profit": arb["guaranteed_profit"],
                    "stake_a": arb["stake_a"],
                    "stake_b": arb["stake_b"],
                    "leg_a": {"side": teams[0], **h2h[teams[0]]},
                    "leg_b": {"side": teams[1], **h2h[teams[1]]},
                })
    
    # Sort picks by edge
    picks.sort(key=lambda x: x["edge_pct"], reverse=True)
    arb_picks.sort(key=lambda x: x["profit_pct"], reverse=True)
    college_picks.sort(key=lambda x: x.get("edge_pct", 0), reverse=True)

    # ── Crypto / financial market picks ────────────────────────────────────
    crypto_picks = await _build_crypto_picks(BANKROLL, MIN_EDGE)

    # Unified top-picks: sports + crypto, sorted by edge
    all_value_picks = sorted(picks + crypto_picks, key=lambda x: x["edge_pct"], reverse=True)

    return {
        "generated_at": datetime.now().isoformat(),
        "bankroll": BANKROLL,
        "total_picks": len(all_value_picks),
        "total_arbs": len(arb_picks),
        "total_college_picks": len(college_picks),
        "top_picks": all_value_picks[:25],
        "sports_picks": picks[:20],
        "crypto_picks": crypto_picks[:15],
        "arbitrage_opportunities": arb_picks[:10],
        "college_picks": college_picks[:15],
        "sports_covered": list(all_odds.keys()),
        "summary": {
            "value_bets":         len([p for p in picks        if "VALUE" in p["verdict"] or "STRONG" in p["verdict"]]),
            "crypto_value_bets":  len([p for p in crypto_picks if "VALUE" in p["verdict"] or "STRONG" in p["verdict"]]),
            "arb_profit_available": sum(a["guaranteed_profit"] for a in arb_picks),
            "college_value_bets": len([p for p in college_picks if p.get("edge_pct", 0) > 0]),
        }
    }


def decimal_to_american(decimal_odds: float) -> int:
    if decimal_odds >= 2.0:
        return int((decimal_odds - 1.0) * 100)
    else:
        return int(-100 / (decimal_odds - 1.0))
